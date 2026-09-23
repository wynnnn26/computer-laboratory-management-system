"""
server.py
Central LAN server for the Computer Laboratory Management System.
Runs on the Admin/Server PC alongside the Tkinter admin dashboard.

- TLS-secured TCP socket listener for client PCs (PanCafe-Pro style).
- Authenticates clients/staff/admins against the central SQLite database.
- Receives heartbeats (status, IP, CPU, RAM, logged-in user via psutil).
- Distributes remote commands (pause/resume/lock/unlock/logout/restart/
  shutdown/screen observation) to the target client PCs.
- Records user sessions and admin activity logs in SQLite.
- Exposes an in-process API consumed by the AdminDashboard GUI.
"""

import os
import sys
import json
import ssl
import time
uuid = __import__("uuid")
import socket
import threading
import traceback
from datetime import datetime

from database import get_connection, verify_password, get_setting
from utils import now_date, now_time, now_datetime
from protocol import (
    Message, MessageType, TLSSocketWrapper, create_ssl_context,
    generate_self_signed_cert,
)

CLIENT_VERSION = "2.0"


# --------------------------------------------------------------------------
# Certificate management (self-signed, generated once next to the DB)
# --------------------------------------------------------------------------
def ensure_certificates():
    """Certificates live next to the running app (works both as .py and
    as a frozen .exe, where __file__ points into the temp build folder)."""
    import sys
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    cert = os.path.join(base_dir, "server.crt")
    key = os.path.join(base_dir, "server.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        generate_self_signed_cert(cert, key)
    return cert, key


# --------------------------------------------------------------------------
# Connected-client record
# --------------------------------------------------------------------------
class ClientEntry:
    """One connected client PC."""

    def __init__(self, sock, addr):
        self.wrapper = TLSSocketWrapper(sock)
        self.addr = addr
        self.pc_name = ""
        self.ip = addr[0]
        self.hostname = ""
        self.status = "locked"            # locked / logged_in / idle / paused
        self.cpu_percent = 0.0
        self.ram_percent = 0.0
        self.logged_in_user = None
        self.session_id = None
        self.is_online = True
        self.last_heartbeat = time.time()
        self.authenticated = False
        self.auth_role = None
        self.screen_streaming = False
        self.desired = None                # admin's desired state (dict) or None
        self._last_resync = 0.0
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        return {
            "pc_name": self.pc_name,
            "ip": self.ip,
            "hostname": self.hostname,
            "status": self.status,
            "cpu_percent": round(self.cpu_percent, 1),
            "ram_percent": round(self.ram_percent, 1),
            "logged_in_user": self.logged_in_user or "",
            "session_id": self.session_id or "",
            "is_online": self.is_online,
            "last_heartbeat": datetime.fromtimestamp(self.last_heartbeat).strftime("%H:%M:%S"),
        }

    def send(self, msg: Message) -> bool:
        with self.lock:
            return self.wrapper.send_message(msg)

    def close(self):
        self.is_online = False
        self.wrapper.close()


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class LabServer:
    """TCP+TLS server hosting the central database and client management."""

    def __init__(self, host="0.0.0.0", port=None, on_event=None):
        self.host = host
        self.port = int(port or get_setting("server_port", "8443"))
        self.on_event = on_event or (lambda kind, data: None)
        self.clients = {}                 # pc_name -> ClientEntry
        self.clients_lock = threading.Lock()
        self.running = False
        self.listener = None
        self.server_ssl = None
        self.threads = []
        self.screen_watchers = {}         # pc_name -> stop Event
        self._pending_screens = {}        # msg_id -> latest frame (bytes)
        self._pending_results = {}        # command_id -> response dict
        self._pending_cv = threading.Condition()

    # ---------------------------------------------------------- lifecycle
    def start(self):
        """Start listener in a background thread."""
        if self.running:
            return
        cert, key = ensure_certificates()
        context = create_ssl_context(is_server=True, certfile=cert, keyfile=key)

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(50)
        self.running = True

        # Startup healing: no client can be connected yet, so every PC marked
        # online and every session still open here is a leftover from a crash
        # or an unclean shutdown. Close them so the dashboard never shows
        # ghost "online" PCs or ghost "active" sessions.
        try:
            conn = get_connection()
            conn.execute("UPDATE computers SET is_online=0")
            conn.execute(
                "UPDATE client_sessions SET logout_time=?, status='Disconnected' "
                "WHERE logout_time IS NULL",
                (now_datetime(),))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[SERVER] startup healing failed: {e}")

        t = threading.Thread(target=self._accept_loop, daemon=True, name="accept-loop")
        t.start()
        self.threads.append(t)

        # Offline-detection sweep
        t2 = threading.Thread(target=self._sweep_loop, daemon=True, name="sweep")
        t2.start()
        self.threads.append(t2)

        self._emit("server_started", {"host": self._lan_ip(), "port": self.port})
        print(f"[SERVER] Listening on {self.host}:{self.port} (TLS)")

    def stop(self):
        self.running = False
        with self.clients_lock:
            entries = list(self.clients.values())
        # Unregister SYNCHRONOUSLY before closing the sockets: entry.close()
        # clears the in-memory online flag, which would otherwise make the
        # (daemon) connection thread skip the session/offline bookkeeping -
        # or never run it at all if the process exits first.
        for entry in entries:
            try:
                self._unregister(entry)
            except Exception:
                traceback.print_exc()
            entry.close()
        with self.clients_lock:
            self.clients.clear()
        for ev in list(self.screen_watchers.values()):
            ev.set()
        try:
            if self.listener:
                self.listener.close()
        except Exception:
            pass
        self._emit("server_stopped", {})

    def _lan_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _emit(self, kind, data):
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    # ---------------------------------------------------------- networking
    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.listener.accept()
            except OSError:
                break
            t = threading.Thread(target=self._client_thread, args=(sock, addr),
                                 daemon=True, name=f"client-{addr[0]}")
            t.start()
            self.threads.append(t)

    def _client_thread(self, sock, addr):
        """Perform the TLS handshake here (off the accept thread), then serve."""
        try:
            sock.settimeout(10)
            tls = self.server_ssl_ctx_wrap(sock)
            tls.settimeout(None)
        except Exception as e:
            print(f"[SERVER] TLS handshake failed from {addr}: {e}")
            try:
                sock.close()
            except Exception:
                pass
            return
        entry = ClientEntry(tls, addr)
        print(f"[SERVER] Connection from {addr}")
        try:
            while self.running:
                msg = entry.wrapper.recv_message(timeout=1.0)
                if msg is None:
                    # recv_message returns None on timeout OR disconnect;
                    # check whether the socket is actually still alive.
                    try:
                        entry.wrapper.sock.getpeername()
                    except Exception:
                        break
                    continue
                self._handle_message(entry, msg)
        except Exception:
            traceback.print_exc()
        finally:
            print(f"[SERVER] Disconnected {addr} pc={entry.pc_name}")
            self._unregister(entry)
            entry.close()

    def server_ssl_ctx_wrap(self, sock):
        cert, key = ensure_certificates()
        if self.server_ssl is None:
            self.server_ssl = create_ssl_context(is_server=True, certfile=cert, keyfile=key)
        return self.server_ssl.wrap_socket(sock, server_side=True)

    def _sweep_loop(self):
        """Mark clients offline if no heartbeat for 15 s."""
        while self.running:
            time.sleep(5)
            now = time.time()
            with self.clients_lock:
                for name, entry in list(self.clients.items()):
                    if now - entry.last_heartbeat > 15:
                        if entry.is_online:
                            entry.is_online = False
                            self._persist_computer(entry, online=0)
                            self._emit("client_offline", {"pc_name": name})
                        # Stop screen streaming
                        if entry.screen_streaming:
                            entry.screen_streaming = False
                            ev = self.screen_watchers.pop(name, None)
                            if ev:
                                ev.set()

    # ---------------------------------------------------------- dispatch
    def _handle_message(self, entry: ClientEntry, msg: Message):
        t = msg.type
        p = msg.payload

        if t == MessageType.CLIENT_REGISTER.value:
            self._on_register(entry, p)
        elif t == MessageType.CLIENT_HEARTBEAT.value:
            self._on_heartbeat(entry, p)
        elif t == MessageType.AUTH_REQUEST.value:
            self._on_auth(entry, msg, p)
        elif t == MessageType.SESSION_START.value:
            self._on_session_start(entry, p)
        elif t == MessageType.SESSION_END.value:
            self._on_session_end(entry, p)
        elif t == MessageType.ACTIVITY_LOG.value:
            self._on_activity(p)
        elif t == MessageType.STU_REQUEST.value:
            self._on_stu_request(entry, msg, p)
        elif t == MessageType.CMD_RESPONSE.value:
            self._on_cmd_response(p)
        elif t == MessageType.CMD_SCREENSHOT.value:
            self._on_screenshot(entry, msg, p)
        elif t == MessageType.CMD_SCREEN_OBSERVE_START.value:
            self._on_observe_frame(entry, msg, p)
        elif t == MessageType.ERROR.value:
            print(f"[SERVER] Client error: {p}")
        elif t == MessageType.PONG.value:
            pass

    # ---------------------------------------------------- register/heartbeat
    def _on_register(self, entry, p):
        entry.pc_name = p.get("pc_name", entry.ip)
        entry.ip = p.get("ip", entry.ip)
        entry.hostname = p.get("hostname", "") or socket.gethostname()
        entry.is_online = True
        entry.last_heartbeat = time.time()

        with self.clients_lock:
            # Replace stale entry for same pc_name (handles server/client
            # restarts and duplicate registrations cleanly).
            old = self.clients.get(entry.pc_name)
            if old and old is not entry:
                old.close()
            self.clients[entry.pc_name] = entry

        self._persist_computer(entry, online=1)
        entry.desired = self._load_desired(entry.pc_name)
        self._emit("client_online", entry.snapshot())

        # Re-apply an outstanding admin lock/pause after (re)connection so a
        # restarted or reconnected client returns to the state the admin set.
        if entry.desired:
            self._resync(entry, initial=True)

    def _on_heartbeat(self, entry, p):
        if not entry.pc_name:
            entry.pc_name = p.get("pc_name", entry.ip)
        if p.get("ip"):
            entry.ip = p["ip"]
        if p.get("hostname"):
            entry.hostname = p["hostname"]
        entry.status = p.get("status", entry.status)
        entry.cpu_percent = p.get("cpu_percent", 0)
        entry.ram_percent = p.get("ram_percent", 0)
        entry.logged_in_user = p.get("logged_in_user")
        entry.session_id = p.get("session_id")
        entry.last_heartbeat = time.time()
        was_offline = not entry.is_online
        entry.is_online = True

        if was_offline:
            with self.clients_lock:
                self.clients[entry.pc_name] = entry
            self._persist_computer(entry, online=1)
            self._emit("client_online", entry.snapshot())

        # Update computers table heartbeat
        conn = get_connection()
        conn.execute(
            "UPDATE computers SET cpu_percent=?, ram_percent=?, last_heartbeat=?, "
            "ip_address=COALESCE(NULLIF(?,''), ip_address), "
            "hostname=COALESCE(NULLIF(?,''), hostname), "
            "status=CASE WHEN status IN ('Available','In Use') "
            "  THEN CASE WHEN ?='logged_in' THEN 'In Use' ELSE 'Available' END ELSE status END "
            "WHERE pc_name=?",
            (entry.cpu_percent, entry.ram_percent, now_datetime(),
             entry.ip, entry.hostname, entry.status, entry.pc_name),
        )
        conn.commit()
        conn.close()

        # Acknowledge the heartbeat: keeps the link provably alive on the
        # client AND carries the authoritative admin state for synchronization.
        from protocol import build_heartbeat_ack
        entry.send(build_heartbeat_ack(
            json.dumps(entry.desired) if entry.desired else ""))

        # Reconcile desired state (server is the source of truth).
        if entry.desired:
            self._resync(entry)

        self._emit("client_heartbeat", entry.snapshot())

    # -------------------------------------------- desired admin state sync
    def _load_desired(self, pc_name):
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT admin_state FROM computers WHERE pc_name=?",
                (pc_name,)).fetchone()
            conn.close()
            if row and row["admin_state"]:
                return json.loads(row["admin_state"])
        except Exception:
            pass
        return None

    def _save_desired(self, pc_name, desired):
        """Persist the admin's desired lock/pause state for a PC (or clear it)."""
        self._persist_admin_state(pc_name, json.dumps(desired) if desired else "")
        with self.clients_lock:
            entry = self.clients.get(pc_name)
            if entry:
                entry.desired = desired or None

    def _persist_admin_state(self, pc_name, state_json):
        try:
            conn = get_connection()
            conn.execute(
                "UPDATE computers SET admin_state=? WHERE pc_name=?",
                (state_json, pc_name))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[SERVER] admin_state persist failed: {e}")

    def _resync(self, entry, initial=False):
        """Re-send the desired command if the client's state does not match.
        Throttled so a non-complying client cannot flood the audit log."""
        desired = entry.desired
        if not desired:
            return
        cmd = desired.get("cmd")
        status = entry.status
        matches = ((cmd == "lock" and status == "locked") or
                   (cmd == "pause" and status == "paused"))
        if matches and not initial:
            return
        if not initial:
            if time.time() - entry._last_resync < 15:
                return
            if matches:
                return
        entry._last_resync = time.time()
        mtype = {"lock": MessageType.CMD_LOCK,
                 "pause": MessageType.CMD_PAUSE}.get(cmd)
        if not mtype:
            return
        self.send_command(mtype, entry.pc_name, desired.get("params", {}),
                          admin_user="system", wait_response=False)
        print(f"[SERVER] Resync {cmd} -> {entry.pc_name}")

    def _persist_computer(self, entry: ClientEntry, online: int):
        conn = get_connection()
        row = conn.execute("SELECT id FROM computers WHERE pc_name=?", (entry.pc_name,)).fetchone()
        if row:
            conn.execute(
                "UPDATE computers SET ip_address=?, hostname=?, client_version=?, "
                "is_online=?, last_heartbeat=? WHERE pc_name=?",
                (entry.ip, entry.hostname, CLIENT_VERSION, online,
                 now_datetime(), entry.pc_name),
            )
        else:
            conn.execute(
                "INSERT INTO computers (pc_name, location, status, ip_address, hostname, "
                "client_version, is_online, last_heartbeat) VALUES (?,?,?,?,?,?,?,?)",
                (entry.pc_name, "Lab", "Available",
                 entry.ip, entry.hostname, CLIENT_VERSION, online, now_datetime()),
            )
        conn.commit()
        conn.close()

    def _unregister(self, entry: ClientEntry):
        """Detach a client: drop it from the registry, mark the PC offline in
        the DB and close any session it still owns.

        Idempotent (may be called from stop(), the connection thread and the
        sweep). Skips the DB writes when a newer connection has already
        replaced this entry for the same PC - that entry now owns the row.
        """
        with self.clients_lock:
            is_current = bool(entry.pc_name) and self.clients.get(entry.pc_name) is entry
            if is_current:
                del self.clients[entry.pc_name]
        if not entry.pc_name or not is_current:
            return
        conn = get_connection()
        conn.execute("UPDATE computers SET is_online=0 WHERE pc_name=?", (entry.pc_name,))
        # Close every session still open on this PC: the connection that
        # owned them is gone. Matched by pc_name (not entry.session_id)
        # because heartbeats continuously overwrite that field - a logged-out
        # client clears it while its session row may still be open.
        conn.execute(
            "UPDATE client_sessions SET logout_time=?, status='Disconnected' "
            "WHERE logout_time IS NULL AND pc_name=?",
            (now_datetime(), entry.pc_name),
        )
        entry.session_id = None
        conn.commit()
        conn.close()
        if entry.is_online:
            entry.is_online = False
            self._emit("client_offline", {"pc_name": entry.pc_name})

    # ------------------------------------------------------------- auth
    def _on_auth(self, entry: ClientEntry, msg: Message, p: dict):
        from protocol import build_auth_response
        username = (p.get("username") or "").strip()
        password = p.get("password") or ""
        role = p.get("role") or "student"
        client_info = p.get("client_info") or {}

        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE student_id=?", (username,)).fetchone()
        conn.close()

        if not row or not verify_password(password, row["password"]):
            self._log_activity(username or "?", "login_failed",
                               entry.pc_name or entry.ip,
                               f"Failed login from {entry.ip}", entry.ip)
            entry.send(build_auth_response(False, error="Invalid credentials"))
            self._emit("auth_failed", {"pc": entry.pc_name, "user": username})
            return

        if row["status"] and row["status"].lower() == "inactive":
            entry.send(build_auth_response(False, error="Account deactivated"))
            return

        # Role check: students can log into clients; admin/staff too
        if row["role"] not in ("student", "admin", "staff"):
            entry.send(build_auth_response(False, error="Role not permitted"))
            return

        token = uuid.uuid4().hex
        entry.authenticated = True
        entry.auth_role = row["role"]

        # Record client info (no hardware specs are collected)
        if client_info and client_info.get("hostname"):
            entry.hostname = client_info["hostname"]

        self._log_activity(username, "login_success", entry.pc_name or entry.ip,
                           f"{row['full_name']} logged in ({row['role']})", entry.ip)

        user_data = {
            "id": row["id"],
            "student_id": row["student_id"],
            "full_name": row["full_name"],
            "role": row["role"],
            "course": row["course"],
            "year_level": row["year_level"],
        }
        entry.send(build_auth_response(True, user_data=user_data, token=token))
        self._emit("auth_success", {"pc": entry.pc_name, "user": username, "role": row["role"]})

    # ----------------------------------------------------------- sessions
    def _on_session_start(self, entry, p):
        sid = p.get("session_id") or uuid.uuid4().hex[:12]
        pc = p.get("pc_name", entry.pc_name)
        user = p.get("student_id", "")
        conn = get_connection()
        # Self-heal: close any session left open for this PC or this user
        # (crash, power loss, unclean shutdown) so no ghost "Active" rows
        # survive into the new session.
        if user:
            conn.execute(
                "UPDATE client_sessions SET logout_time=?, status='Disconnected' "
                "WHERE logout_time IS NULL AND (pc_name=? OR student_id=?)",
                (now_datetime(), pc, user),
            )
        else:
            conn.execute(
                "UPDATE client_sessions SET logout_time=?, status='Disconnected' "
                "WHERE logout_time IS NULL AND pc_name=?",
                (now_datetime(), pc),
            )
        conn.execute(
            "INSERT OR REPLACE INTO client_sessions "
            "(session_id, pc_name, student_id, full_name, login_time, ip_address, status) "
            "VALUES (?,?,?,?,?,?, 'Active')",
            (sid, pc, user,
             p.get("full_name", ""), datetime.fromtimestamp(p.get("start_time", time.time()))
                 .strftime("%Y-%m-%d %H:%M:%S"),
             entry.ip),
        )
        # NOTE: the Lab Attendance feature was intentionally removed - only
        # the PC session record is kept (Internet Cafe PC management focus).
        conn.execute(
            "UPDATE computers SET status='In Use', assigned_to=? WHERE pc_name=?",
            (user, pc),
        )
        conn.commit()
        conn.close()
        entry.session_id = sid
        entry.status = "logged_in"
        entry.logged_in_user = p.get("student_id")
        self._emit("session_started", p)

    def _on_session_end(self, entry, p):
        sid = p.get("session_id") or entry.session_id
        if not sid:
            return
        end = datetime.fromtimestamp(p.get("end_time", time.time()))
        conn = get_connection()
        row = conn.execute(
            "SELECT login_time, pc_name, student_id FROM client_sessions WHERE session_id=?",
            (sid,),
        ).fetchone()
        duration = p.get("duration")
        if duration is None and row and row["login_time"]:
            try:
                start = datetime.strptime(row["login_time"], "%Y-%m-%d %H:%M:%S")
                duration = int((end - start).total_seconds())
            except Exception:
                duration = 0
        conn.execute(
            "UPDATE client_sessions SET logout_time=?, duration_seconds=?, status='Completed' "
            "WHERE session_id=?",
            (end.strftime("%Y-%m-%d %H:%M:%S"), duration or 0, sid),
        )
        # (Lab Attendance feature removed - no attendance bookkeeping.)
        if row:
            conn.execute(
                "UPDATE computers SET status='Available', assigned_to='' WHERE pc_name=?",
                (row["pc_name"],),
            )
        conn.commit()
        conn.close()
        if entry.session_id == sid:
            entry.session_id = None
            entry.status = "locked"
            entry.logged_in_user = None
        self._emit("session_ended", {"session_id": sid, "duration": duration})

    # ------------------------------------------------ student-facing queries
    def _on_stu_request(self, entry, msg: Message, p: dict):
        """Purpose-built parameterized queries against the central database."""
        from protocol import build_stu_response
        kind = p.get("kind", "")
        d = p.get("data", {}) or {}
        ok, data, err = True, [], None

        try:
            conn = get_connection()
            if kind == "announcements":
                rows = conn.execute("SELECT * FROM announcements ORDER BY id DESC").fetchall()
                data = [dict(r) for r in rows]

            elif kind == "messages_get":
                sid = d.get("student_id", "")
                rows = conn.execute(
                    "SELECT * FROM messages WHERE sender=? OR receiver=? ORDER BY id DESC",
                    (sid, sid),
                ).fetchall()
                data = [dict(r) for r in rows]

            elif kind == "messages_send":
                conn.execute(
                    "INSERT INTO messages (sender, receiver, message, timestamp, is_read) "
                    "VALUES (?,?,?,?, 'No')",
                    (d.get("student_id", ""), "admin", d.get("message", ""), now_datetime()),
                )
                conn.commit()

            elif kind == "borrow_list":
                rows = conn.execute(
                    "SELECT * FROM borrow_records WHERE student_id=? ORDER BY id DESC",
                    (d.get("student_id", ""),),
                ).fetchall()
                data = [dict(r) for r in rows]

            elif kind == "borrow_submit":
                if not d.get("item_name"):
                    raise ValueError("Item name is required")
                conn.execute(
                    "INSERT INTO borrow_records (student_id, item_name, quantity, borrow_date, "
                    "return_date, status) VALUES (?,?,?,?,?, 'Pending Approval')",
                    (d.get("student_id", ""), d.get("item_name", ""),
                     d.get("quantity", "1"), now_date(), ""),
                )
                conn.commit()

            elif kind == "attendance_list":
                # Lab Attendance feature removed - kept as a safe no-op.
                ok, err = False, "Lab Attendance is not available."

            elif kind == "attendance_time_in":
                ok, err = False, "Lab Attendance is not available."

            elif kind == "attendance_time_out":
                ok, err = False, "Lab Attendance is not available."

            elif kind == "pcs_available":
                rows = conn.execute(
                    "SELECT pc_name FROM computers WHERE status='Available' ORDER BY pc_name"
                ).fetchall()
                data = [r["pc_name"] for r in rows]

            elif kind == "profile":
                rows = conn.execute(
                    "SELECT * FROM users WHERE student_id=?", (d.get("student_id", ""),)
                ).fetchone()
                data = dict(rows) if rows else []

            elif kind == "computers_all":
                rows = conn.execute("SELECT * FROM computers ORDER BY pc_name").fetchall()
                data = [dict(r) for r in rows]

            else:
                ok, err = False, f"Unknown query kind: {kind}"

            conn.close()
        except Exception as e:
            ok, err, data = False, str(e), []

        resp = build_stu_response(msg.msg_id, kind, ok, data, err)
        entry.send(resp)

    # ------------------------------------------------- activity / commands
    def _on_activity(self, p):
        self._log_activity(p.get("admin_user", "?"), p.get("action", "?"),
                           p.get("target", ""), p.get("details", ""))

    def _log_activity(self, admin_user, action, target, details="", ip=""):
        conn = get_connection()
        conn.execute(
            "INSERT INTO admin_activity_log (admin_user, action, target, details, timestamp, ip_address) "
            "VALUES (?,?,?,?,?,?)",
            (admin_user, action, target, details, now_datetime(), ip),
        )
        conn.commit()
        conn.close()
        self._emit("activity", {"admin_user": admin_user, "action": action,
                                "target": target, "details": details})

    def _on_cmd_response(self, p):
        cid = p.get("command_id")
        if cid:
            with self._pending_cv:
                self._pending_results[cid] = p
                self._pending_cv.notify_all()
        success = bool(p.get("success"))
        conn = get_connection()
        row = None
        if cid:
            row = conn.execute(
                "SELECT command_type, target_pc, admin_user FROM client_commands "
                "WHERE command_id=?", (cid,)).fetchone()
        conn.execute(
            "UPDATE client_commands SET status=?, result=?, executed_at=? WHERE command_id=?",
            ("Done" if success else "Failed",
             json.dumps(p.get("result") if p.get("result") is not None else p.get("error", "")),
             now_datetime(), cid),
        )
        conn.commit()
        conn.close()

        # Explicit audit for the critical power actions: admin, target PC,
        # timestamp, command and result (Phase B - shutdown/restart safety).
        if row and row["command_type"] in ("cmd_restart", "cmd_shutdown"):
            self._log_activity(
                row["admin_user"] or "system", row["command_type"],
                row["target_pc"],
                f"result={'OK' if success else 'FAILED'} "
                f"{p.get('result') or p.get('error') or ''}".strip())
        self._emit("cmd_response", p)

    def _on_screenshot(self, entry, msg, p):
        # p carries base64 image data
        with self._pending_cv:
            self._pending_screens[msg.msg_id] = p
            self._pending_cv.notify_all()
        self._emit("screenshot", {"pc_name": entry.pc_name, "data": p})

    def _on_observe_frame(self, entry, msg, p):
        self._emit("screen_frame", {"pc_name": entry.pc_name, "data": p})

    # ------------------------------------------------------ public commands
    def list_clients(self) -> list:
        with self.clients_lock:
            return [e.snapshot() for e in self.clients.values()]

    def get_client(self, pc_name):
        with self.clients_lock:
            return self.clients.get(pc_name)

    def send_command(self, cmd: MessageType, target_pc: str, params: dict = None,
                     admin_user: str = "", wait_response: bool = True,
                     timeout: float = None) -> dict:
        """Send a command to a client and optionally wait for its response."""
        from protocol import build_command
        entry = self.get_client(target_pc)
        if not entry or not entry.is_online:
            return {"success": False, "error": f"{target_pc} is offline"}

        command_id = uuid.uuid4().hex[:8]
        msg = build_command(cmd, target_pc, params or {}, admin_user, command_id)

        conn = get_connection()
        conn.execute(
            "INSERT INTO client_commands (command_id, target_pc, command_type, params, admin_user, "
            "status, created_at) VALUES (?,?,?,?,?,'Sent',?)",
            (command_id, target_pc, cmd.value, json.dumps(params or {}), admin_user, now_datetime()),
        )
        conn.commit()
        conn.close()

        self._log_activity(admin_user, f"command:{cmd.value}", target_pc,
                           json.dumps(params or {}))

        if not entry.send(msg):
            return {"success": False, "error": "Send failed", "command_id": command_id}

        # Track the admin's desired state so it survives client restarts and
        # server/client reconnects (lock is cleared by unlock, pause only by
        # an explicit resume - never automatically).
        if cmd == MessageType.CMD_LOCK:
            self._save_desired(target_pc, {"cmd": "lock", "params": params or {}})
        elif cmd == MessageType.CMD_PAUSE:
            self._save_desired(target_pc, {"cmd": "pause", "params": params or {}})
        elif cmd in (MessageType.CMD_UNLOCK, MessageType.CMD_RESUME):
            self._save_desired(target_pc, None)

        if not wait_response:
            return {"success": True, "command_id": command_id}

        timeout = timeout or float(get_setting("command_timeout", "30"))
        deadline = time.time() + timeout
        with self._pending_cv:
            while time.time() < deadline:
                if command_id in self._pending_results:
                    return self._pending_results.pop(command_id)
                self._pending_cv.wait(min(0.5, max(0.05, deadline - time.time())))
        return {"success": False, "error": "Timeout waiting for response",
                "command_id": command_id}

    # Convenience wrappers -------------------------------------------------
    def _audit_command(self, target_pc, cmd_type, params, admin_user,
                       status="Sent") -> str:
        """Insert a client_commands audit row; returns the command_id."""
        command_id = uuid.uuid4().hex[:8]
        try:
            conn = get_connection()
            conn.execute(
                "INSERT INTO client_commands (command_id, target_pc, command_type, "
                "params, admin_user, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (command_id, target_pc, cmd_type, json.dumps(params or {}),
                 admin_user, status, now_datetime()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[SERVER] audit insert failed: {e}")
        return command_id

    def _audit_done(self, command_id, success, result=""):
        try:
            conn = get_connection()
            conn.execute(
                "UPDATE client_commands SET status=?, result=?, executed_at=? "
                "WHERE command_id=?",
                ("Done" if success else "Failed", str(result)[:500],
                 now_datetime(), command_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def lock_client(self, pc, admin="", msg=None):
        return self.send_command(MessageType.CMD_LOCK, pc,
                                 {"message": msg or "Locked by administrator"},
                                 admin)

    def unlock_client(self, pc, admin=""):
        return self.send_command(MessageType.CMD_UNLOCK, pc, {}, admin)

    def logout_client(self, pc, admin=""):
        return self.send_command(MessageType.CMD_LOGOUT, pc, {}, admin)

    def restart_client(self, pc, admin=""):
        return self.send_command(MessageType.CMD_RESTART, pc, {}, admin)

    def shutdown_client(self, pc, admin=""):
        return self.send_command(MessageType.CMD_SHUTDOWN, pc, {}, admin)

    def pause_client(self, pc, admin="", seconds=0, msg=None):
        return self.send_command(MessageType.CMD_PAUSE, pc,
                                 {"seconds": seconds, "message": msg or "Paused by administrator"},
                                 admin)

    def resume_client(self, pc, admin=""):
        return self.send_command(MessageType.CMD_RESUME, pc, {}, admin)

    def send_client_message(self, pc, text, admin="", msg_type="admin"):
        return self.send_command(MessageType.CMD_SEND_MESSAGE, pc,
                                 {"message": text, "msg_type": msg_type}, admin)

    def force_logout_user(self, student_id, admin="", reason="") -> dict:
        """Log a user out of any active client session.

        Called automatically when an account is deleted, disabled or
        updated so stale sessions never survive account changes.
        """
        if not student_id:
            return {"success": True, "forced": 0}
        conn = get_connection()
        rows = conn.execute(
            "SELECT session_id, pc_name, login_time FROM client_sessions "
            "WHERE student_id=? AND logout_time IS NULL", (student_id,)).fetchall()
        conn.close()
        forced = 0
        for r in rows:
            # Tell the client to return to the login screen (no wait - the
            # PC may be offline; the session row is closed either way).
            self.send_command(MessageType.CMD_LOGOUT, r["pc_name"], {},
                              admin or "system", wait_response=False)
            duration = 0
            try:
                start = datetime.strptime(r["login_time"], "%Y-%m-%d %H:%M:%S")
                duration = int((datetime.now() - start).total_seconds())
            except Exception:
                pass
            conn = get_connection()
            conn.execute(
                "UPDATE client_sessions SET logout_time=?, duration_seconds=?, "
                "status='Forced Logout' WHERE session_id=?",
                (now_datetime(), duration, r["session_id"]))
            conn.execute(
                "UPDATE computers SET status='Available', assigned_to='' "
                "WHERE pc_name=? AND assigned_to=?", (r["pc_name"], student_id))
            conn.commit()
            conn.close()
            forced += 1
        if forced:
            self._log_activity(admin or "system", "force_logout", student_id,
                               f"{reason} ({forced} session(s))")
            self._emit("session_forced", {"student_id": student_id,
                                          "count": forced})
        return {"success": True, "forced": forced}

    def request_screenshot(self, pc, admin="", quality=None, scale=None) -> dict:
        from protocol import build_screenshot_request
        entry = self.get_client(pc)
        if not entry or not entry.is_online:
            return {"success": False, "error": f"{pc} is offline"}
        quality = quality or int(get_setting("screenshot_quality", "50"))
        scale = scale or float(get_setting("screenshot_scale", "0.5"))
        req = build_screenshot_request(quality, scale)
        req.payload["target_pc"] = pc
        cid = self._audit_command(pc, "cmd_screenshot", {"quality": quality,
                                                         "scale": scale}, admin)
        if not entry.send(req):
            self._audit_done(cid, False, "send failed")
            return {"success": False, "error": "Send failed"}
        with self._pending_cv:
            deadline = time.time() + 10
            while time.time() < deadline:
                if req.msg_id in self._pending_screens:
                    data = self._pending_screens.pop(req.msg_id)
                    self._audit_done(cid, True, "frame captured")
                    self._log_activity(admin, "screenshot", pc, "frame captured")
                    return {"success": True, "data": data}
                self._pending_cv.wait(0.3)
        self._audit_done(cid, False, "timeout")
        self._log_activity(admin, "screenshot", pc, "FAILED: timeout")
        return {"success": False, "error": "Screenshot timeout"}

    def start_screen_observe(self, pc, admin="", interval=1.0,
                             quality=50, scale=0.6) -> bool:
        from protocol import build_screen_observe_start
        entry = self.get_client(pc)
        if not entry or not entry.is_online:
            return False
        if pc in self.screen_watchers:
            return True
        ev = threading.Event()
        self.screen_watchers[pc] = ev
        entry.screen_streaming = True
        msg = build_screen_observe_start(interval, quality, scale)
        msg.payload["target_pc"] = pc
        entry.send(msg)
        self._audit_command(pc, "cmd_screen_observe_start",
                            {"interval": interval, "quality": quality,
                             "scale": scale}, admin, status="Done")
        self._log_activity(admin, "screen_observe_start", pc, f"interval={interval}s")
        return True

    def stop_screen_observe(self, pc, admin="") -> bool:
        from protocol import build_screen_observe_stop
        entry = self.get_client(pc)
        ev = self.screen_watchers.pop(pc, None)
        if ev:
            ev.set()
        if entry:
            entry.screen_streaming = False
            msg = build_screen_observe_stop()
            msg.payload["target_pc"] = pc
            entry.send(msg)
        self._audit_command(pc, "cmd_screen_observe_stop", {}, admin, status="Done")
        self._log_activity(admin, "screen_observe_stop", pc, "")
        return True

    def broadcast_message(self, text, admin="", msg_type="admin") -> dict:
        results = {}
        with self.clients_lock:
            names = list(self.clients.keys())
        for name in names:
            results[name] = self.send_client_message(name, text, admin, msg_type)
        return results


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"