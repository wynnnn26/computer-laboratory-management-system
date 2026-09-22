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
    cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.crt")
    key = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.key")
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
        self.mac = ""
        self.specs = {}
        self.status = "locked"            # locked / logged_in / idle / maintenance / paused
        self.cpu_percent = 0.0
        self.ram_percent = 0.0
        self.logged_in_user = None
        self.session_id = None
        self.is_online = True
        self.last_heartbeat = time.time()
        self.authenticated = False
        self.auth_role = None
        self.screen_streaming = False
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        return {
            "pc_name": self.pc_name,
            "ip": self.ip,
            "mac": self.mac,
            "specs": self.specs,
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
            for entry in list(self.clients.values()):
                entry.close()
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
        entry.mac = p.get("mac", "")
        entry.specs = p.get("specs", {})
        entry.is_online = True
        entry.last_heartbeat = time.time()

        with self.clients_lock:
            # Replace stale entry for same pc_name
            old = self.clients.get(entry.pc_name)
            if old and old is not entry:
                old.close()
            self.clients[entry.pc_name] = entry

        self._persist_computer(entry, online=1)
        self._emit("client_online", entry.snapshot())

    def _on_heartbeat(self, entry, p):
        if not entry.pc_name:
            entry.pc_name = p.get("pc_name", entry.ip)
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
            "status=CASE WHEN status IN ('Available','In Use') "
            "  THEN CASE WHEN ?='logged_in' THEN 'In Use' ELSE 'Available' END ELSE status END "
            "WHERE pc_name=?",
            (entry.cpu_percent, entry.ram_percent, now_datetime(), entry.status, entry.pc_name),
        )
        conn.commit()
        conn.close()

        self._emit("client_heartbeat", entry.snapshot())

    def _persist_computer(self, entry: ClientEntry, online: int):
        conn = get_connection()
        row = conn.execute("SELECT id FROM computers WHERE pc_name=?", (entry.pc_name,)).fetchone()
        specs_str = json.dumps(entry.specs) if entry.specs else ""
        if row:
            conn.execute(
                "UPDATE computers SET ip_address=?, mac_address=?, client_version=?, "
                "is_online=?, last_heartbeat=? WHERE pc_name=?",
                (entry.ip, entry.mac, CLIENT_VERSION, online, now_datetime(), entry.pc_name),
            )
        else:
            conn.execute(
                "INSERT INTO computers (pc_name, specs, location, status, ip_address, mac_address, "
                "client_version, is_online, last_heartbeat) VALUES (?,?,?,?,?,?,?,?,?)",
                (entry.pc_name, specs_str, "Lab", "Available",
                 entry.ip, entry.mac, CLIENT_VERSION, online, now_datetime()),
            )
        conn.commit()
        conn.close()

    def _unregister(self, entry: ClientEntry):
        with self.clients_lock:
            if entry.pc_name and self.clients.get(entry.pc_name) is entry:
                del self.clients[entry.pc_name]
        if entry.is_online and entry.pc_name:
            conn = get_connection()
            conn.execute("UPDATE computers SET is_online=0 WHERE pc_name=?", (entry.pc_name,))
            # Close any open session on this PC
            if entry.session_id:
                conn.execute(
                    "UPDATE client_sessions SET logout_time=?, status='Disconnected' "
                    "WHERE session_id=? AND logout_time IS NULL",
                    (now_datetime(), entry.session_id),
                )
            conn.commit()
            conn.close()
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

        # Record client info
        if client_info:
            entry.specs.update(client_info)

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
        conn = get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO client_sessions "
            "(session_id, pc_name, student_id, full_name, login_time, ip_address, status) "
            "VALUES (?,?,?,?,?,?, 'Active')",
            (sid, p.get("pc_name", entry.pc_name), p.get("student_id", ""),
             p.get("full_name", ""), datetime.fromtimestamp(p.get("start_time", time.time()))
                 .strftime("%Y-%m-%d %H:%M:%S"),
             entry.ip),
        )
        # Also create attendance record (In Lab)
        conn.execute(
            "INSERT INTO attendance (student_id, full_name, pc_name, date, time_in, status) "
            "VALUES (?,?,?,?,?, 'In Lab')",
            (p.get("student_id", ""), p.get("full_name", ""),
             p.get("pc_name", entry.pc_name), now_date(), now_time()),
        )
        conn.execute(
            "UPDATE computers SET status='In Use', assigned_to=? WHERE pc_name=?",
            (p.get("student_id", ""), p.get("pc_name", entry.pc_name)),
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
        # Attendance time-out (close the most recent open record for today)
        conn.execute(
            "UPDATE attendance SET time_out=?, status='Completed' "
            "WHERE id=(SELECT id FROM attendance WHERE student_id=? AND date=? AND status='In Lab' "
            "ORDER BY id DESC LIMIT 1)",
            (now_time(), row["student_id"] if row else "", now_date()),
        )
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
                rows = conn.execute(
                    "SELECT * FROM attendance WHERE student_id=? ORDER BY id DESC",
                    (d.get("student_id", ""),),
                ).fetchall()
                data = [dict(r) for r in rows]

            elif kind == "attendance_time_in":
                sid = d.get("student_id", "")
                existing = conn.execute(
                    "SELECT id FROM attendance WHERE student_id=? AND date=? AND status='In Lab'",
                    (sid, now_date()),
                ).fetchone()
                if existing:
                    ok, err = False, "You already have an active lab session today."
                else:
                    pc = d.get("pc_name", "")
                    pc_row = conn.execute(
                        "SELECT status FROM computers WHERE pc_name=?", (pc,)
                    ).fetchone()
                    if not pc_row or pc_row["status"] != "Available":
                        ok, err = False, f"{pc} is not available."
                    else:
                        conn.execute(
                            "INSERT INTO attendance (student_id, full_name, pc_name, date, "
                            "time_in, status) VALUES (?,?,?,?,?, 'In Lab')",
                            (sid, d.get("full_name", ""), pc, now_date(), now_time()),
                        )
                        conn.execute(
                            "UPDATE computers SET status='In Use', assigned_to=? WHERE pc_name=?",
                            (sid, pc),
                        )
                        conn.commit()

            elif kind == "attendance_time_out":
                sid = d.get("student_id", "")
                row = conn.execute(
                    "SELECT id, pc_name FROM attendance WHERE student_id=? AND date=? "
                    "AND status='In Lab' ORDER BY id DESC LIMIT 1",
                    (sid, now_date()),
                ).fetchone()
                if not row:
                    ok, err = False, "No active session to time out from."
                else:
                    conn.execute(
                        "UPDATE attendance SET time_out=?, status='Completed' WHERE id=?",
                        (now_time(), row["id"]),
                    )
                    conn.execute(
                        "UPDATE computers SET status='Available', assigned_to='' WHERE pc_name=?",
                        (row["pc_name"],),
                    )
                    conn.commit()

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
        conn = get_connection()
        conn.execute(
            "UPDATE client_commands SET status=?, result=?, executed_at=? WHERE command_id=?",
            ("Done" if p.get("success") else "Failed",
             json.dumps(p.get("result") if p.get("result") is not None else p.get("error", "")),
             now_datetime(), cid),
        )
        conn.commit()
        conn.close()
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
                    return {"success": True, "data": data}
                self._pending_cv.wait(0.3)
        self._audit_done(cid, False, "timeout")
        return {"success": False, "error": "Screenshot timeout"}

    def start_screen_observe(self, pc, admin="", interval=1.0,
                             quality=30, scale=0.4) -> bool:
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