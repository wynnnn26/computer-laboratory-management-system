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
import base64
uuid = __import__("uuid")
import socket
import threading
import traceback
from datetime import datetime

from database import (get_connection, verify_password, get_setting,
                      hash_password, DEFAULT_CLIENT_PASSWORD)
from utils import now_date, now_time, now_datetime
from protocol import (
    Message, MessageType, TLSSocketWrapper, create_ssl_context,
    generate_self_signed_cert, DISCOVER_MAGIC, REPLY_MAGIC,
    discovery_udp_port, DENY_FILE_EXTS, MAX_PUSH_FILE_BYTES,
)

CLIENT_VERSION = "2.0"

# Seconds without a reconnect after a network disconnect before the event
# is escalated to a Client Crash in the audit trail (P3 event taxonomy:
# Normal Logout / Client Closed / Client Crash / Network Disconnect).
CRASH_GRACE_SECONDS = 30


# --------------------------------------------------------------------------
# Authoritative PC status engine (single source of truth for every UI)
# --------------------------------------------------------------------------
# Priority (highest first): OFFLINE > VERIFYING > LOCKED > PAUSED >
# IN USE > AVAILABLE > ONLINE, with UNKNOWN as the final fallback.
# Keys match utils.STATUS_LABELS / pc_icons/<key>.png.
STATUS_PRIORITY = ("offline", "verifying", "locked", "paused", "in_use",
                   "available", "online", "unknown")


def _field(obj, key, default=None):
    """Read `key` from a ClientEntry, dict or sqlite3.Row."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (IndexError, KeyError, TypeError):
        return getattr(obj, key, default)


def derive_status(entry=None, row=None) -> str:
    """Compute the single authoritative status for one PC.

    A live ClientEntry (or a dict with the same fields) always takes
    precedence; the database row is the fallback when no connection exists
    (ever-connected rows -> OFFLINE, never-connected rows -> UNKNOWN).

    Priority: OFFLINE > VERIFYING > LOCKED > PAUSED > IN USE > AVAILABLE >
    ONLINE, UNKNOWN only when nothing at all can be determined.  The
    dashboard renders exactly this value - it never guesses a status.
    """
    if entry is None:
        if row is None:
            return "unknown"
        if not _field(row, "is_online", 0) and not _field(row, "last_heartbeat", None):
            return "unknown"              # never connected - state unknown
        return "offline"                  # known PC, but no live connection
    # 1) OFFLINE wins over every other state
    if not bool(_field(entry, "is_online", False)):
        return "offline"
    # 2) VERIFYING: connecting / reconnecting / authenticating
    if bool(_field(entry, "verifying", False)):
        return "verifying"
    desired = _field(entry, "desired", None) or {}
    if isinstance(desired, str):
        try:
            desired = json.loads(desired) if desired else {}
        except Exception:
            desired = {}
    cmd = desired.get("cmd") if isinstance(desired, dict) else None
    # 3) LOCKED: an admin lock is outstanding (Unlock/Force Login clears it)
    if cmd == "lock":
        return "locked"
    status = str(_field(entry, "status", "") or "")
    # 4) PAUSED: admin pause (never auto-expires) or client-reported pause
    if cmd == "pause" or status == "paused":
        return "paused"
    # 5) IN USE: a user session is active on this PC
    if _field(entry, "logged_in_user", None) or status == "logged_in":
        return "in_use"
    # 6) AVAILABLE: connected, nobody logged in, kiosk ready for login
    if status in ("locked", "idle", ""):
        return "available"
    # 7) ONLINE: connected, state not further classified
    if bool(_field(entry, "is_online", False)):
        return "online"
    # 8) UNKNOWN fallback
    return "unknown"


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
        # True once CLIENT_REGISTER completed - the minimum trust level for
        # the pre-login flows (web policy + log flush); AUTH goes further.
        self.registered = False
        self.authenticated = False
        self.auth_role = None
        # Server-validated identity of the last successful AUTH (never
        # client-claimed) - used to harden session_start and to display the
        # account type of the current session.
        self.auth_user = None
        self.auth_full_name = None
        # FIRST LOGIN PASSWORD: while True, this entry's account must
        # still replace its default password - session_start is refused
        # until the change is confirmed by the server.
        self.must_change_password = False
        # Active-session account info, refreshed from the DB at session_start
        self.account_role = None
        self.account_full_name = None
        # VERIFYING: set at (re)register and during auth, cleared by the
        # first heartbeat (or when the auth exchange finishes).
        self.verifying = True
        # Last state emitted through _emit_state() -> status_changed events
        self._last_state = ""
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
            # Authoritative display state + session account info (set by the
            # server only; never client-claimed; no passwords anywhere).
            "state": derive_status(self),
            "account_role": self.account_role or self.auth_role or "",
            "account_full_name": self.account_full_name or self.auth_full_name or "",
            "cpu_percent": round(self.cpu_percent, 1),
            "ram_percent": round(self.ram_percent, 1),
            "logged_in_user": self.logged_in_user or "",
            "session_id": self.session_id or "",
            "is_online": self.is_online,
            "last_heartbeat": datetime.fromtimestamp(self.last_heartbeat).strftime("%H:%M:%S"),
            "verifying": self.verifying,
            "admin_cmd": (self.desired or {}).get("cmd", "") if isinstance(self.desired, dict) else "",
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
        # username -> [fail_count, locked_until_ts]: account lockout state
        self._login_fail = {}
        self.running = False
        self.listener = None
        self.server_ssl = None
        self.threads = []
        self.screen_watchers = {}         # pc_name -> stop Event
        # Screen share (teaching): ONE active share to ALL online lab PCs.
        # `share_stop` is the capture worker's stop Event (None = idle);
        # `share_targets` is every PC that was sent a START or a frame -
        # the worker's exit path delivers the audited STOP to exactly those.
        self.share_stop = None
        self.share_targets = set()
        # Task 5: one ACTIVE remote-control session per PC at most.
        # pc_name -> {"admin", "session_id", "started_at"}; guarded by its
        # own lock because input forwarding happens on reader threads.
        self.remote_sessions = {}
        self._remote_lock = threading.Lock()
        # P4: which (pc, role) refusals have already been audited, so the
        # hot path - one input batch per mouse move - cannot drown the
        # trail the way a row per movement would.  Bounded, and never
        # trusted for anything: refusing does not depend on it.
        self._remote_reject_seen = set()
        self._pending_screens = {}        # msg_id -> latest frame (bytes)
        self._pending_results = {}        # command_id -> response dict
        self._pending_cv = threading.Condition()
        self._crash_timers = {}           # pc_name -> pending crash Timer
        self._timer_lock = threading.Lock()
        self._udp_sock = None             # UDP LAN discovery responder

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

        # UDP LAN discovery responder (broadcast probes -> our address)
        self._start_discovery()

        self._emit("server_started", {"host": self._lan_ip(), "port": self.port})
        print(f"[SERVER] Listening on {self.host}:{self.port} (TLS) "
              f"+ UDP discovery {discovery_udp_port(self.port)}")

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
        # Screen share: end the capture worker too; clients learn the link
        # is gone from their own disconnect handler and close the overlay.
        if self.share_stop is not None:
            self.share_stop.set()
        # Task 5: nothing survives a server shutdown - end every remote
        # control session (audited) rather than leaving one dangling.
        for name in list(getattr(self, "remote_sessions", {}) or {}):
            try:
                self.end_remote_control_on_drop(name, "server shutting down")
            except Exception:
                traceback.print_exc()
        # Never escalate a disconnect to a crash after shutdown (P3).
        with self._timer_lock:
            for t in self._crash_timers.values():
                t.cancel()
            self._crash_timers.clear()
        try:
            if self.listener:
                self.listener.close()
        except Exception:
            pass
        try:
            if self._udp_sock:
                self._udp_sock.close()
                self._udp_sock = None
        except Exception:
            pass
        self._emit("server_stopped", {})

    def _start_discovery(self):
        """UDP LAN discovery responder: answer broadcast probes with this
        server's address so Client PCs can find it without a configured
        (or still valid) server_ip - spec: broadcast on TCP port + 1 = 8444.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", discovery_udp_port(self.port)))
            sock.settimeout(0.5)
            self._udp_sock = sock
        except OSError as e:
            print(f"[SERVER] UDP discovery disabled: {e}")
            return

        def _responder():
            while self.running:
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    # Windows surfaces stray ICMP "port unreachable"
                    # errors here whenever a probe socket closed while
                    # one of our replies was still in flight - keep
                    # serving; only a real shutdown ends the loop.
                    if not self.running or sock.fileno() < 0:
                        break
                    time.sleep(0.05)   # never busy-spin on stray errors
                    continue
                try:
                    probe = json.loads(data.decode("utf-8", "replace"))
                    if (not isinstance(probe, dict)
                            or probe.get("type") != DISCOVER_MAGIC):
                        continue
                    reply = {"type": REPLY_MAGIC,
                             "server_ip": (self._lan_ip()
                                           if self.host in ("0.0.0.0", "", "::")
                                           else self.host),
                             "tcp_port": self.port}
                    sock.sendto(json.dumps(reply).encode("utf-8"), addr)
                except Exception:
                    continue        # malformed probes / stray resets ignored

        t = threading.Thread(target=_responder, daemon=True,
                             name="udp-discovery")
        t.start()
        self.threads.append(t)

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

    def _emit_state(self, entry):
        """Push a status_changed event whenever the derived state differs
        from the last one emitted - the dashboard refreshes on these and
        never guesses a status of its own."""
        if entry is None:
            return
        try:
            state = derive_status(entry)
        except Exception:
            state = "unknown"
        prev = getattr(entry, "_last_state", "")
        if state == prev:
            return
        entry._last_state = state
        self._emit("status_changed", {
            "pc_name": entry.pc_name or entry.ip or "?",
            "state": state,
            "previous": prev,
            "is_online": bool(getattr(entry, "is_online", False)),
            "account_role": (getattr(entry, "account_role", None)
                             or getattr(entry, "auth_role", None) or ""),
            "account_full_name": (getattr(entry, "account_full_name", None)
                                  or getattr(entry, "auth_full_name", None) or ""),
        })

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
                t0 = time.time()
                msg = entry.wrapper.recv_message(timeout=1.0)
                if msg is None:
                    # recv_message returns None on timeout OR disconnect.
                    # A genuine timeout waits the full second, so a fast
                    # None means EOF/reset - the client is gone (a
                    # getpeername check cannot see a half-closed socket
                    # and would spin here forever).  Exit so the finally
                    # block unregisters the PC, closes its sessions and
                    # records the Network Disconnect (P3 taxonomy).
                    if time.time() - t0 < 0.5:
                        break
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
            self._on_connection_lost(entry)
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
            dropped = []
            with self.clients_lock:
                for name, entry in list(self.clients.items()):
                    if now - entry.last_heartbeat > 15:
                        if entry.is_online:
                            entry.is_online = False
                            self._persist_computer(entry, online=0)
                            self._emit("client_offline", {"pc_name": name})
                            self._emit_state(entry)
                        # Task 5: a PC that stopped heartbeating can no
                        # longer be controlled safely - flag its remote
                        # session for an immediate, audited end.  Collected
                        # here and processed BELOW so we never take
                        # clients_lock twice on the same thread.
                        if name in self.remote_sessions:
                            dropped.append(name)
                        # Stop screen streaming
                        if entry.screen_streaming:
                            entry.screen_streaming = False
                            ev = self.screen_watchers.pop(name, None)
                            if ev:
                                ev.set()
            for name in dropped:
                try:
                    self.end_remote_control_on_drop(name,
                                                   "no heartbeat for 15s")
                except Exception:
                    traceback.print_exc()

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
        elif t == MessageType.PASSWORD_CHANGE_REQUEST.value:
            self._on_password_change(entry, p)
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
        elif t == MessageType.WEB_POLICY_ACK.value:
            # Website Access (spec 8): the client confirms which policy
            # version it actually applied (or why it could not).
            self._on_web_policy_ack(entry, p)
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
        entry.registered = True
        entry.ip = p.get("ip", entry.ip)
        entry.hostname = p.get("hostname", "") or socket.gethostname()
        entry.is_online = True
        entry.last_heartbeat = time.time()

        # Reconnect taxonomy (P3): a returning client cancels its pending
        # crash escalation, and the earlier drop is reclassified as a
        # Network Disconnect + Reconnect pair instead of a crash.
        with self._timer_lock:
            pending = self._crash_timers.pop(entry.pc_name, None)
        if pending:
            pending.cancel()
            self._log_activity("system", "client_reconnect", entry.pc_name,
                               "Client reconnected after a network disconnect",
                               entry.ip)

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
        self._emit_state(entry)           # VERIFYING until the first heartbeat

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
        entry.verifying = False           # first heartbeat proves the link

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
            "connection_type=COALESCE(NULLIF(?,''), connection_type), "
            "status=CASE WHEN status IN ('Available','In Use') "
            "  THEN CASE WHEN ?='logged_in' THEN 'In Use' ELSE 'Available' END ELSE status END "
            "WHERE pc_name=?",
            (entry.cpu_percent, entry.ram_percent, now_datetime(),
             entry.ip, entry.hostname, p.get("conn_type") or "",
             entry.status, entry.pc_name),
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
        self._emit_state(entry)

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
                # Resume reconciles a client-reported pause immediately so
                # the grid never lags behind an explicit Resume.
                if not desired and entry.status == "paused":
                    entry.status = "logged_in" if entry.logged_in_user else "locked"
                self._emit_state(entry)

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
        # Website Access: an unreachable PC can no longer hold a synced
        # policy - its row goes OFFLINE until the client reconnects and
        # re-acks (spec item 8 status list).
        conn.execute(
            "UPDATE web_pc_policy SET sync_status='OFFLINE', updated_at=? "
            "WHERE pc_name=?",
            (now_datetime(), entry.pc_name))
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
        # Task 5: a dropped connection can never leave a live remote-control
        # session behind.  The session table is cleared BEFORE any
        # reconnect can be noticed, so a returning Client always starts
        # from a clean state and can never be reconnected back into an
        # active session.  The end-of-session audit row is written here
        # too - the Client is gone, so no ack can ever arrive for it.
        try:
            self.end_remote_control_on_drop(entry.pc_name, "connection closed")
        except Exception:
            traceback.print_exc()
        if entry.is_online:
            entry.is_online = False
            self._emit("client_offline", {"pc_name": entry.pc_name})
            self._emit_state(entry)

    # ------------------------------------------------- disconnect taxonomy
    def _on_connection_lost(self, entry):
        """A dropped connection is a NETWORK DISCONNECT, not a logout.

        The client may be rebooting or merely re-establishing the link, so
        the event is recorded immediately and only escalated to a CLIENT
        CRASH if the PC never reconnects within the grace period.  Normal
        Logout (client_logout/session_end) and Client Closed arrive as
        explicit messages instead, so all four events stay distinct (P3).
        Skipped while the server is stopping - there the drop is ours.
        """
        name = entry.pc_name
        if not name or not self.running:
            return
        self._log_activity(
            "system", "network_disconnect", name,
            f"Connection lost from {entry.ip} "
            f"(user {entry.auth_user or entry.logged_in_user or '-'})",
            entry.ip)
        timer = threading.Timer(CRASH_GRACE_SECONDS,
                                self._escalate_disconnect, args=(name,))
        timer.daemon = True
        with self._timer_lock:
            old = self._crash_timers.pop(name, None)
            if old:
                old.cancel()
            self._crash_timers[name] = timer
        timer.start()

    def _escalate_disconnect(self, name):
        """Grace expired without a reconnect: the drop was a client crash
        (process died or PC shut down - it can no longer say otherwise)."""
        with self._timer_lock:
            self._crash_timers.pop(name, None)
        if not self.running:
            return
        with self.clients_lock:
            if name in self.clients:      # reconnected in time - not a crash
                return
        self._log_activity(
            "system", "client_crash", name,
            f"No reconnect {CRASH_GRACE_SECONDS}s after a network disconnect "
            "(presumed client crash or PC shutdown)")
        self._emit("client_crash", {"pc_name": name})

    # ------------------------------------------------------------- auth
    def _on_auth(self, entry: ClientEntry, msg: Message, p: dict):
        # Authenticating counts as VERIFYING until the exchange finishes.
        entry.verifying = True
        self._emit_state(entry)
        try:
            return self._do_auth(entry, p)
        finally:
            entry.verifying = False
            self._emit_state(entry)

    def _do_auth(self, entry: ClientEntry, p: dict):
        from protocol import build_auth_response
        username = (p.get("username") or "").strip()
        password = p.get("password") or ""
        client_info = p.get("client_info") or {}

        # Account lockout (security): max_failed_logins wrong attempts
        # lock the account for lockout_duration seconds (live settings).
        # ponytail: per-account in-memory counters - a server restart
        # clears them; DB-backed lockout if restarts must not release it.
        now = time.time()
        rec = self._login_fail.get(username) if username else None
        if rec and rec[1] > now:
            left = int(rec[1] - now) + 1
            self._log_activity(username, "login_locked",
                               entry.pc_name or entry.ip,
                               f"Locked out ({left}s left) from {entry.ip}",
                               entry.ip)
            entry.send(build_auth_response(
                False, error=f"Account locked. Try again in {left} seconds."))
            self._emit("auth_failed", {"pc": entry.pc_name, "user": username})
            return

        # NOTE: the client can send any role string it likes - it is
        # deliberately ignored here.  The role always comes from the
        # central DB row, so a client can never self-claim admin.
        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE student_id=?", (username,)).fetchone()
        conn.close()

        if not row or not verify_password(password, row["password"]):
            self._log_activity(username or "?", "login_failed",
                               entry.pc_name or entry.ip,
                               f"Failed login from {entry.ip}", entry.ip)
            if username:
                try:
                    max_fail = int(get_setting("max_failed_logins", "5"))
                except (TypeError, ValueError):
                    max_fail = 5
                cnt = self._login_fail.get(username, [0, 0.0])[0] + 1
                locked_until = 0.0
                if cnt >= max_fail:
                    try:
                        dur = int(get_setting("lockout_duration", "300"))
                    except (TypeError, ValueError):
                        dur = 300
                    locked_until = now + dur
                    self._log_activity(
                        username, "login_locked", entry.pc_name or entry.ip,
                        f"Locked for {dur}s after {cnt} failures "
                        f"from {entry.ip}", entry.ip)
                self._login_fail[username] = [cnt, locked_until]
            entry.send(build_auth_response(False, error="Invalid credentials"))
            self._emit("auth_failed", {"pc": entry.pc_name, "user": username})
            return

        if row["status"] and row["status"].lower() == "inactive":
            entry.send(build_auth_response(False, error="Account deactivated"))
            return

        # Role check: students can log into clients; admin/staff and the
        # maintenance technician do too
        if row["role"] not in ("student", "admin", "staff", "maintenance"):
            entry.send(build_auth_response(False, error="Role not permitted"))
            return

        # FIRST LOGIN PASSWORD: fresh client (student) accounts are
        # flagged when created, and the factory default password itself
        # always counts as still-default (covers accounts created before
        # the flag existed).  Admin/staff/maintenance accounts are never
        # forced.
        must_change = False
        if row["role"] == "student":
            flagged = (bool(row["must_change_password"])
                       if "must_change_password" in row.keys() else False)
            must_change = flagged or password == DEFAULT_CLIENT_PASSWORD
        entry.must_change_password = must_change
        self._login_fail.pop(username, None)   # success clears the counter

        token = uuid.uuid4().hex
        entry.authenticated = True
        entry.auth_role = row["role"]
        entry.auth_user = row["student_id"]        # server-validated identity
        entry.auth_full_name = row["full_name"]

        # Record client info (no hardware specs are collected)
        if client_info and client_info.get("hostname"):
            entry.hostname = client_info["hostname"]

        self._log_activity(username, "login_success", entry.pc_name or entry.ip,
                           f"{row['full_name']} logged in ({row['role']})", entry.ip)
        # Client-admin logins get their own audit rows (and so do their
        # session ends - see _on_session_end).
        if row["role"] == "admin":
            self._log_activity(username, "client_admin_login",
                               entry.pc_name or entry.ip,
                               f"{row['full_name']} logged in on a client PC",
                               entry.ip)

        user_data = {
            "id": row["id"],
            "student_id": row["student_id"],
            "full_name": row["full_name"],
            "role": row["role"],
            "course": row["course"],
            "year_level": row["year_level"],
            "must_change_password": must_change,
        }
        entry.send(build_auth_response(True, user_data=user_data, token=token))
        self._emit("auth_success", {"pc": entry.pc_name, "user": username, "role": row["role"]})

    def _on_password_change(self, entry, p):
        """FIRST LOGIN PASSWORD: a fresh client account replaces its
        default password.  The identity comes from the authenticated
        TLS entry (entry.auth_user), never from the payload - a client
        can only change its OWN password, only while authenticated."""
        from protocol import build_password_change_response
        user = entry.auth_user if getattr(entry, "authenticated", False) else None
        if not user:
            entry.send(build_password_change_response(
                False, error="Not authenticated. Please log in again."))
            return
        new_pw = p.get("new_password") or ""
        if len(new_pw) < 6:
            entry.send(build_password_change_response(
                False, error="Password must be at least 6 characters."))
            return
        if new_pw == DEFAULT_CLIENT_PASSWORD:
            entry.send(build_password_change_response(
                False,
                error="The new password cannot be the default password."))
            return
        conn = get_connection()
        row = conn.execute("SELECT password FROM users WHERE student_id=?",
                           (user,)).fetchone()
        if not row:
            conn.close()
            entry.send(build_password_change_response(
                False, error="Account not found."))
            return
        if verify_password(new_pw, row["password"]):
            conn.close()
            entry.send(build_password_change_response(
                False,
                error="Choose a password different from your current one."))
            return
        # PBKDF2 hash only - a plaintext password is never stored, nor
        # is the submitted password ever logged.
        conn.execute(
            "UPDATE users SET password=?, must_change_password=0 "
            "WHERE student_id=?",
            (hash_password(new_pw), user))
        conn.commit()
        conn.close()
        entry.must_change_password = False
        self._log_activity(user, "password_changed",
                           entry.pc_name or entry.ip,
                           "Default password replaced with a new password",
                           entry.ip)
        entry.send(build_password_change_response(True))
        # M2: every online Client PC re-pulls the roster at once, so the
        # new password applies to Client PCs immediately - including what
        # an offline sign-in will accept (6-hour pull is the backstop).
        self.notify_roster_changed()

    def notify_roster_changed(self):
        """M2: tell every ONLINE Client PC that accounts changed (password
        edit, new account, revocation) so it re-pulls the auth roster now.
        Fire-and-forget: no command_id, no audit row - the pull is the
        proof.  Offline PCs catch up on their next reconnect/refresh."""
        from protocol import Message, MessageType
        sent = 0
        with self.clients_lock:
            entries = [e for e in self.clients.values() if e and e.is_online]
        for e in entries:
            try:
                if e.send(Message.create(
                        MessageType.CMD_ROSTER_REFRESH, {})):
                    sent += 1
            except Exception:
                pass        # raced with a disconnect: the pull covers it
        return {"sent": sent}

    # ----------------------------------------------------------- sessions
    def _on_session_start(self, entry, p):
        # FIRST LOGIN PASSWORD: no session may start while the default
        # password is still in place.  The kiosk already blocks its own
        # UI until the server confirms the change - this is the
        # server-side guarantee that normal usage is impossible.
        if getattr(entry, "must_change_password", False):
            self._log_activity(entry.auth_user or "?", "session_start_blocked",
                               entry.pc_name or entry.ip,
                               "Session refused: default password not changed yet",
                               entry.ip)
            return
        sid = p.get("session_id") or uuid.uuid4().hex[:12]
        pc = p.get("pc_name", entry.pc_name)
        # Server-authoritative identity: after a successful AUTH the session
        # ALWAYS belongs to the authenticated account - the client cannot
        # claim another user's ID or name in this message.
        if getattr(entry, "authenticated", False) and entry.auth_user:
            user = entry.auth_user
            full_name = entry.auth_full_name or p.get("full_name", "")
        else:
            user = p.get("student_id", "")
            full_name = p.get("full_name", "")
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
        # the PC's reported network medium (heartbeat) as of session start
        medium = ""
        try:
            mrow = conn.execute(
                "SELECT connection_type FROM computers WHERE pc_name=?",
                (pc,)).fetchone()
            if mrow:
                medium = mrow["connection_type"] or ""
        except Exception:
            medium = ""
        conn.execute(
            "INSERT OR REPLACE INTO client_sessions "
            "(session_id, pc_name, student_id, full_name, login_time, "
            " ip_address, status, connection_type) "
            "VALUES (?,?,?,?,?,?, 'Active', ?)",
            (sid, pc, user, full_name,
             datetime.fromtimestamp(p.get("start_time", time.time()))
                 .strftime("%Y-%m-%d %H:%M:%S"),
             entry.ip, medium),
        )
        # NOTE: the Lab Attendance feature was intentionally removed - only
        # the PC session record is kept (Internet Cafe PC management focus).
        conn.execute(
            "UPDATE computers SET status='In Use', assigned_to=? WHERE pc_name=?",
            (user, pc),
        )
        # Account role/name for the CURRENT SESSION come from the central DB
        # row (never from the client payload).
        arow = None
        if user:
            arow = conn.execute(
                "SELECT role, full_name FROM users WHERE student_id=?",
                (user,)).fetchone()
        conn.commit()
        conn.close()
        entry.session_id = sid
        entry.status = "logged_in"
        entry.logged_in_user = user
        if arow:
            entry.account_role = arow["role"]
            entry.account_full_name = arow["full_name"]
        else:
            entry.account_role = entry.auth_role or ""
            entry.account_full_name = entry.auth_full_name or full_name
        self._emit("session_started",
                   dict(p, student_id=user, full_name=full_name))
        self._emit_state(entry)

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
            # A client-admin session ending gets its own audit row.
            if (entry.account_role or entry.auth_role) == "admin":
                self._log_activity(entry.auth_user or entry.logged_in_user or "?",
                                   "client_admin_logout",
                                   entry.pc_name or entry.ip,
                                   "Client-admin session ended", entry.ip)
            entry.session_id = None
            entry.status = "locked"
            entry.logged_in_user = None
            entry.account_role = None
            entry.account_full_name = None
        self._emit("session_ended", {"session_id": sid, "duration": duration})
        self._emit_state(entry)

    # ------------------------------------------------ student-facing queries
    def _on_stu_request(self, entry, msg: Message, p: dict):
        """Purpose-built parameterized queries against the central database."""
        from protocol import build_stu_response
        kind = p.get("kind", "")
        d = p.get("data", {}) or {}
        ok, data, err = True, [], None

        # Auth gate (security): anything touching accounts, messages or
        # records needs a successful AUTH on this connection.  The kiosk
        # legitimately runs two kinds BEFORE anyone logs in, so those stay
        # reachable to a registered-but-unauthenticated entry: the web
        # policy (website blocking applies to the machine, not the user)
        # and the log flush.  The auth roster feeds offline passwords, so
        # it needs a real login (usable right after the first sign-in).
        if kind == "auth_roster":
            if not getattr(entry, "authenticated", False):
                entry.send(build_stu_response(
                    msg.msg_id, kind, False, [],
                    "Not authenticated. Please log in again."))
                return
        elif kind in ("web_filter", "log_sync"):
            if not getattr(entry, "registered", False):
                entry.send(build_stu_response(
                    msg.msg_id, kind, False, [],
                    "Not registered with the server."))
                return
        elif not getattr(entry, "authenticated", False):
            entry.send(build_stu_response(
                msg.msg_id, kind, False, [],
                "Not authenticated. Please log in again."))
            return

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
                # Validation (spec: admin approves every request) - garbage
                # never reaches the table.
                sid = str(d.get("student_id", "")).strip()
                item = str(d.get("item_name", "")).strip()
                if not sid:
                    raise ValueError("Student ID is required")
                if not item:
                    raise ValueError("Item name is required")
                try:
                    qty = int(str(d.get("quantity", "1")).strip() or "1")
                except (TypeError, ValueError):
                    raise ValueError("Quantity must be a whole number")
                if qty < 1:
                    raise ValueError("Quantity must be at least 1")
                conn.execute(
                    "INSERT INTO borrow_records (student_id, item_name, quantity, borrow_date, "
                    "return_date, status) VALUES (?,?,?,?,?, 'Pending Approval')",
                    (sid, item, str(qty), now_date(), ""),
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

            elif kind == "web_filter":
                # Website Access: the full policy snapshot a client pulls
                # on every (re)connect (version + mode + rule lists);
                # client.apply_web_policy applies it and acks the version.
                data = self.get_web_policy(
                    getattr(entry, "pc_name", "") or None)

            elif kind == "auth_roster":
                # Offline login (spec items 10/12): the client caches this
                # roster over TLS and verifies offline logins against the
                # salted hashes - never plaintext.  max_offline_days caps
                # how long a roster may be used without a refresh.
                rows = conn.execute(
                    "SELECT student_id, full_name, role, password, status,"
                    " must_change_password FROM users WHERE status='Active'"
                    " ORDER BY student_id").fetchall()
                data = {"users": [
                    {"student_id": r["student_id"],
                     "full_name": r["full_name"],
                     "role": r["role"],
                     "password_hash": r["password"],
                     "status": r["status"],
                     "must_change_password":
                         1 if r["must_change_password"] else 0}
                    for r in rows],
                    "max_offline_days": get_setting("max_offline_days", "7")}

            elif kind == "log_sync":
                # Local event log flush (spec items 9/11/15/17): event_id
                # is the client's UUID and the PRIMARY KEY of client_logs,
                # so INSERT OR IGNORE dedupes a resent batch and every
                # submitted id comes back as accepted - the client marks
                # exactly those rows SYNCED (idempotent, never lost).
                events = d.get("events") or []
                stored = 0
                accepted = []
                now = now_datetime()
                pc = str(d.get("pc_name") or getattr(entry, "pc_name", "")
                         or "")
                for ev in events[:200]:
                    eid = str(ev.get("event_id") or "").strip()
                    if not eid:
                        continue
                    accepted.append(eid)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO client_logs"
                        " (event_id, pc_name, user_id, severity, category,"
                        "  message, detail, created_at, received_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (eid, str(ev.get("pc_name") or pc),
                         str(ev.get("user_id") or ""),
                         str(ev.get("severity") or "INFO"),
                         str(ev.get("category") or ""),
                         str(ev.get("message") or ""),
                         str(ev.get("detail") or ""),
                         str(ev.get("created_at") or ""), now))
                    stored += cur.rowcount or 0
                conn.commit()
                data = {"accepted": accepted, "stored": stored,
                        "duplicates": len(accepted) - stored}

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

    def list_pc_states(self) -> list:
        """Single source of truth for the dashboard PC grid: live entries
        merged with every PC that has EVER connected (last_heartbeat set).

        Never-connected rows (old demo seeds, manual inventory) are NOT
        included - no fake "offline" PCs are pre-seeded.  Every item
        carries the server-derived `state` plus session account info;
        passwords are never part of any payload.
        """
        with self.clients_lock:
            live = {n: e for n, e in self.clients.items() if n}
        db_rows = {}
        try:
            conn = get_connection()
            for r in conn.execute(
                    "SELECT pc_name, ip_address, hostname, status, assigned_to, "
                    "cpu_percent, ram_percent, last_heartbeat, is_online, location "
                    "FROM computers WHERE last_heartbeat IS NOT NULL OR is_online=1"
            ).fetchall():
                db_rows[r["pc_name"]] = r
            conn.close()
        except Exception:
            traceback.print_exc()

        out, seen = [], set()
        for name in sorted(live):
            seen.add(name)
            snap = live[name].snapshot()
            row = db_rows.get(name)
            if row is not None:
                snap["location"] = row["location"] or ""
            out.append(snap)
        # Offline PCs: known to the server (ever connected) but no live entry
        for name, row in db_rows.items():
            if name in seen:
                continue
            out.append({
                "pc_name": name,
                "ip": row["ip_address"] or "",
                "hostname": row["hostname"] or "",
                "status": row["status"] or "",
                "state": derive_status(None, row),
                "account_role": "",
                "account_full_name": "",
                "cpu_percent": row["cpu_percent"] or 0,
                "ram_percent": row["ram_percent"] or 0,
                "logged_in_user": row["assigned_to"] or "",
                "session_id": "",
                "is_online": False,
                "last_heartbeat": row["last_heartbeat"] or "",
                "verifying": False,
                "admin_cmd": "",
                "location": row["location"] or "",
            })
        out.sort(key=lambda s: str(s.get("pc_name") or ""))
        return out

    # ------------------------------------------------------------- bulk ops
    BULK_ACTIONS = {
        "pause": MessageType.CMD_PAUSE,
        "resume": MessageType.CMD_RESUME,
        "logout": MessageType.CMD_LOGOUT,
        "lock": MessageType.CMD_LOCK,
        "unlock": MessageType.CMD_UNLOCK,          # Unlock / Force Login
        "force_unlock": MessageType.CMD_UNLOCK,
        "restart": MessageType.CMD_RESTART,
        "shutdown": MessageType.CMD_SHUTDOWN,
        "send_file": MessageType.CMD_SEND_FILE,    # payload = filename + data
    }

    def bulk_command(self, action, pc_names, admin_user="", params=None,
                     timeout=None, audit_params=None) -> dict:
        """Run one action across many PCs with an honest per-PC result:

            SUCCESS  - the client ACKed the command
            FAILED   - send failed or the client rejected it
            OFFLINE  - no live connection (never sent)
            TIMEOUT  - no ACK within the timeout

        Writes one BULK_<ACTION> audit row per targeted PC.
        """
        action = (action or "").lower().strip()
        cmd = self.BULK_ACTIONS.get(action)
        results = {}
        if not cmd:
            return {str(pc): {"success": False, "result": "FAILED",
                              "error": f"unknown action '{action}'"}
                    for pc in (pc_names or [])}
        for pc in [str(x) for x in (pc_names or [])]:
            entry = self.get_client(pc)
            if not entry or not entry.is_online:
                if cmd == MessageType.CMD_UNLOCK:
                    # M1: force unlock releases every PC - clear the
                    # persisted desired lock even while offline, so a
                    # disconnected locked kiosk cannot re-lock itself on
                    # reconnect (forgetting the intent needs no link).
                    self._save_desired(pc, None)
                results[pc] = {"success": False, "result": "OFFLINE",
                               "error": f"{pc} is offline"}
            else:
                try:
                    resp = self.send_command(cmd, pc, dict(params or {}),
                                             admin_user=admin_user,
                                             wait_response=True, timeout=timeout,
                                             audit_params=audit_params)
                except Exception as e:
                    resp = {"success": False, "error": str(e)}
                if resp.get("success"):
                    # the client's own ack (e.g. unlock's "force_login"
                    # vs "login_allowed") rides along for the result line
                    results[pc] = {"success": True, "result": "SUCCESS",
                                   "command_id": resp.get("command_id", ""),
                                   "detail": str(resp.get("result") or "")}
                else:
                    err = str(resp.get("error") or "")
                    low = err.lower()
                    if "timeout" in low:
                        kind = "TIMEOUT"
                    elif "offline" in low:
                        kind = "OFFLINE"
                    else:
                        kind = "FAILED"
                    results[pc] = {"success": False, "result": kind,
                                   "error": err,
                                   "command_id": resp.get("command_id", "")}
            r = results[pc]
            self._log_activity(admin_user or "system", f"BULK_{action.upper()}",
                               pc, f"result={r['result']}"
                               + (f" {r.get('error')}" if r.get("error") else ""))
        self._emit("bulk_done", {"action": action, "count": len(results),
                                 "results": results})
        return results

    def get_client(self, pc_name):
        with self.clients_lock:
            return self.clients.get(pc_name)

    def send_command(self, cmd: MessageType, target_pc: str, params: dict = None,
                     admin_user: str = "", wait_response: bool = True,
                     timeout: float = None, audit_params: dict = None) -> dict:
        """Send a command to a client and optionally wait for its response.

        audit_params: what to RECORD about the command when the wire params
        carry a bulky payload (a pushed file's base64 data) - the audit row
        and client_commands keep the summary, the wire keeps the bytes.
        """
        from protocol import build_command
        entry = self.get_client(target_pc)
        if not entry or not entry.is_online:
            return {"success": False, "error": f"{target_pc} is offline"}

        command_id = uuid.uuid4().hex[:8]
        msg = build_command(cmd, target_pc, params or {}, admin_user, command_id)

        rec = json.dumps(audit_params if audit_params is not None
                         else (params or {}))
        conn = get_connection()
        conn.execute(
            "INSERT INTO client_commands (command_id, target_pc, command_type, params, admin_user, "
            "status, created_at) VALUES (?,?,?,?,?,'Sent',?)",
            (command_id, target_pc, cmd.value, rec, admin_user, now_datetime()),
        )
        conn.commit()
        conn.close()

        self._log_activity(admin_user, f"command:{cmd.value}", target_pc, rec)

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

    # Website Access -------------------------------------------------------
    WEB_MODES = ("allow_all", "block_list", "allow_only")

    @staticmethod
    def _web_mode_default(conn) -> str:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key='web_mode'").fetchone()
        mode = (row["value"] if row else "") or "allow_all"
        return mode if mode in LabServer.WEB_MODES else "allow_all"

    def get_web_filter(self) -> list:
        """Back-compat view: the enabled block-list domains only."""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT domain FROM websites WHERE list_type='blocked' "
                "AND enabled=1 ORDER BY domain").fetchall()
            return [r["domain"] for r in rows]
        finally:
            conn.close()

    def get_web_policy(self, pc_name: str = None) -> dict:
        """Full Website Access policy snapshot (spec items 6-8).

        {version, mode, blocked, allowed, upstream_dns} - mode is the
        per-PC override when pc_name has one, else the global default.
        Lists contain enabled rules only."""
        conn = get_connection()
        try:
            def setting(key, default):
                row = conn.execute(
                    "SELECT value FROM system_settings WHERE key=?",
                    (key,)).fetchone()
                return row["value"] if row and row["value"] not in (None, "") \
                    else default
            try:
                version = int(setting("web_policy_version", "0"))
            except (TypeError, ValueError):
                version = 0
            blocked = [r["domain"] for r in conn.execute(
                "SELECT domain FROM websites WHERE list_type='blocked' "
                "AND enabled=1 ORDER BY domain").fetchall()]
            allowed = [r["domain"] for r in conn.execute(
                "SELECT domain FROM websites WHERE list_type='allowed' "
                "AND enabled=1 ORDER BY domain").fetchall()]
            mode = None
            if pc_name:
                row = conn.execute(
                    "SELECT mode FROM web_pc_policy WHERE pc_name=?",
                    (pc_name,)).fetchone()
                if row and row["mode"] in self.WEB_MODES:
                    mode = row["mode"]
            if not mode:
                mode = self._web_mode_default(conn)
            return {"version": version, "mode": mode, "blocked": blocked,
                    "allowed": allowed,
                    "upstream_dns": setting("upstream_dns", "1.1.1.1")}
        finally:
            conn.close()

    def bump_web_policy_version(self) -> int:
        """Create a new policy version (spec item 8: every rules/mode
        change gets its own version number)."""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description) "
                "VALUES ('web_policy_version','0',"
                "'Monotonic Website Access policy version')")
            conn.execute(
                "UPDATE system_settings SET value = CAST(value AS INTEGER) + 1 "
                "WHERE key='web_policy_version'")
            conn.commit()
            row = conn.execute(
                "SELECT value FROM system_settings "
                "WHERE key='web_policy_version'").fetchone()
            return int(row["value"])
        finally:
            conn.close()

    def set_web_mode(self, mode, pc_names=None, admin_user="") -> dict:
        """Spec item 7: apply a Website Access mode to a scope of PCs.

        pc_names=None targets every registered PC (and re-saves the
        global default); a list targets exactly those PCs.  Bumps the
        policy version, records the desired mode per PC, then pushes."""
        if mode not in self.WEB_MODES:
            return {"success": False,
                    "error": f"Unknown mode: {mode!r} "
                             f"(expected one of {', '.join(self.WEB_MODES)})"}
        version = self.bump_web_policy_version()
        conn = get_connection()
        try:
            if pc_names is None:
                pc_names = [r["pc_name"] for r in conn.execute(
                    "SELECT pc_name FROM computers ORDER BY pc_name").fetchall()]
                # applying to every PC also re-saves the global default
                # so future/re-registered PCs inherit it (upsert: the old
                # REPLACE form dropped the description the settings page
                # shows as the field label)
                conn.execute(
                    "INSERT INTO system_settings (key, value) "
                    "VALUES ('web_mode', ?) ON CONFLICT(key) DO UPDATE "
                    "SET value = excluded.value", (mode,))
            for name in pc_names:
                if not name:
                    continue
                conn.execute(
                    "INSERT INTO web_pc_policy "
                    "(pc_name, mode, version, sync_status, enforced, "
                    " last_error, updated_at) "
                    "VALUES (?,?,?, 'OFFLINE', '', '', ?) "
                    "ON CONFLICT(pc_name) DO UPDATE SET "
                    "  mode=excluded.mode, version=excluded.version, "
                    "  updated_at=excluded.updated_at",
                    (name, mode, version, now_datetime()))
            conn.commit()
        finally:
            conn.close()
        pushed = self.push_web_filter(admin_user=admin_user,
                                      pc_names=pc_names, version=version)
        return {"success": True, "version": version,
                "targets": len(pc_names), "sent": pushed.get("sent", 0),
                "offline": pushed.get("offline", 0)}

    def push_web_filter(self, admin_user: str = "", pc_names=None,
                        version=None) -> dict:
        """Send the current policy snapshot to Client PCs (spec 6-8).

        Each target row in web_pc_policy records the desired mode and
        version with status SYNCING (online) or OFFLINE (offline).  A row
        only becomes SYNCED when the client's WEB_POLICY_ACK for exactly
        that version arrives - never before (spec item 8)."""
        conn = get_connection()
        try:
            if version is None:
                try:
                    row = conn.execute(
                        "SELECT value FROM system_settings "
                        "WHERE key='web_policy_version'").fetchone()
                    version = int(row["value"])
                except (TypeError, ValueError, AttributeError):
                    version = 0
            if pc_names is None:
                # every known PC: registered rows plus currently online
                # clients (an online client not yet in computers still
                # gets its policy)
                pc_names = {r["pc_name"] for r in conn.execute(
                    "SELECT pc_name FROM computers").fetchall()}
                with self.clients_lock:
                    pc_names |= {n for n, e in self.clients.items() if n}
                pc_names = sorted(pc_names)
            sent = offline = 0
            for name in pc_names:
                if not name:
                    continue
                # keep an existing per-PC mode override, else the default
                row = conn.execute(
                    "SELECT mode FROM web_pc_policy WHERE pc_name=?",
                    (name,)).fetchone()
                mode = row["mode"] if row and row["mode"] in self.WEB_MODES \
                    else self._web_mode_default(conn)
                online = False
                with self.clients_lock:
                    e = self.clients.get(name)
                    online = bool(e and e.is_online)
                status = "SYNCING" if online else "OFFLINE"
                conn.execute(
                    "INSERT INTO web_pc_policy "
                    "(pc_name, mode, version, sync_status, enforced, "
                    " last_error, updated_at) "
                    "VALUES (?,?,?,?,'','',?) "
                    "ON CONFLICT(pc_name) DO UPDATE SET "
                    "  mode=excluded.mode, version=excluded.version, "
                    "  sync_status=excluded.sync_status, "
                    "  updated_at=excluded.updated_at",
                    (name, mode, version, status, now_datetime()))
                conn.commit()
                if not online:
                    offline += 1
                    continue
                policy = {"version": version, "mode": mode,
                          "blocked": [], "allowed": []}
                pol = self.get_web_policy(name)
                policy.update({"blocked": pol["blocked"],
                               "allowed": pol["allowed"],
                               "upstream_dns": pol["upstream_dns"],
                               "version": version})
                res = self.send_command(
                    MessageType.CMD_WEB_FILTER, name, {"policy": policy},
                    admin_user, wait_response=False)
                if res.get("success"):
                    sent += 1
                else:
                    # raced with a disconnect: no chance of an ack
                    conn.execute(
                        "UPDATE web_pc_policy SET sync_status='OFFLINE', "
                        "updated_at=? WHERE pc_name=?",
                        (now_datetime(), name))
                    conn.commit()
                    offline += 1
            return {"sent": sent, "offline": offline,
                    "targets": len(pc_names), "version": version}
        finally:
            conn.close()

    def _on_web_policy_ack(self, entry, p: dict):
        """Spec item 8: record the client's answer for a policy version."""
        pc = (getattr(entry, "pc_name", "") or entry.ip or
              p.get("pc_name") or "")
        try:
            version = int(p.get("version"))
        except (TypeError, ValueError):
            version = None          # unverifiable ack: never flips a row
        success = bool(p.get("success"))
        enforced = str(p.get("enforced") or "")
        error = "" if success else str(p.get("error") or "apply failed")
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT version FROM web_pc_policy WHERE pc_name=?",
                (pc,)).fetchone()
            if row and (version is None
                        or version != int(row["version"])):
                # stale/unverifiable ack for another policy version - the
                # newer push owns the row's status, ignore it
                return
            status = "SYNCED" if success else "FAILED"
            now = now_datetime()
            if row:
                conn.execute(
                    "UPDATE web_pc_policy SET sync_status=?, enforced=?, "
                    "last_error=?, updated_at=? WHERE pc_name=?",
                    (status, enforced, error, now, pc))
            else:
                # client pulled and applied on reconnect before we ever
                # pushed to it - record its state now
                conn.execute(
                    "INSERT INTO web_pc_policy "
                    "(pc_name, mode, version, sync_status, enforced, "
                    " last_error, updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(pc_name) DO UPDATE SET "
                    "  version=excluded.version, sync_status=excluded.sync_status, "
                    "  enforced=excluded.enforced, last_error=excluded.last_error, "
                    "  updated_at=excluded.updated_at",
                    (pc, p.get("mode") or "allow_all",
                     version if version is not None else 0, status,
                     enforced, error, now))
            conn.commit()
        finally:
            conn.close()

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
            # Clear the LIVE entry immediately as well: the dashboard must
            # not keep showing a session that no longer exists.
            with self.clients_lock:
                e = self.clients.get(r["pc_name"])
            if e and (e.session_id == r["session_id"]
                      or e.logged_in_user == student_id):
                e.session_id = None
                e.logged_in_user = None
                e.status = "locked"
                e.account_role = None
                e.account_full_name = None
                self._emit_state(e)
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

    def push_desktop_file(self, pc_names, path, admin="", role="",
                          timeout=None) -> dict:
        """Push one file to every named PC's Desktop (ADMINISTRATOR only).

        Returns bulk_command's honest per-PC dict:
        SUCCESS / FAILED / OFFLINE / TIMEOUT.  The file is read and
        encoded once, then sent to each online PC; validation failures
        never reach the wire.
        """
        names = [str(p) for p in (pc_names or [])]
        if not names:
            return {}

        def _refused(err):
            return {pc: {"success": False, "result": "FAILED", "error": err}
                    for pc in names}

        # ADMINISTRATOR gate, refused AT THE SERVER (the hidden button is
        # only the convenience layer) - audited as a security event.
        if not self._observe_role_ok(role, admin, "send_file_refuse", names[0]):
            return _refused("ADMINISTRATOR only")

        path = str(path or "")
        if not os.path.isfile(path):
            return _refused("File not found")
        if os.path.splitext(path)[1].lower() in DENY_FILE_EXTS:
            return _refused("Executable file types are not allowed")
        size = os.path.getsize(path)
        if size > MAX_PUSH_FILE_BYTES:
            return _refused("File is too large (limit 5 MB)")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except Exception as e:
            return _refused(f"Could not read the file: {e}")

        payload = {"filename": os.path.basename(path),
                   "data": base64.b64encode(raw).decode("ascii")}
        # the audit rows record name + size only - never the payload bytes
        slim = {"filename": payload["filename"], "bytes": size}
        return self.bulk_command("send_file", names, admin_user=admin,
                                 params=payload, timeout=timeout or 60,
                                 audit_params=slim)

    # ------------------------------------------- P4: ADMINISTRATOR only
    def _observe_role_ok(self, role, admin, action, pc, once_key=None):
        """ADMINISTRATOR-only gate for Observe / Remote Control (P4).

        Watching a screen or driving a machine is reserved for the
        ADMINISTRATOR role, so every entry point - opening an Observe
        stream, starting a Remote session and forwarding a single input
        batch - passes through here and is refused AT THE SERVER, not
        merely hidden by the UI.  The UI filter is a convenience; this is
        the control.

        A refusal is worth keeping: an attempt to take over a lab PC is a
        security event, so it lands in the audit trail whether or not it
        succeeded.  `once_key` keeps a hot path (one call per mouse move)
        from drowning that trail - only the first refusal per key is
        written.

        Returns True when the role may proceed.  Never raises, and a
        failure to write the audit row never turns a refusal into a
        pass."""
        try:
            r = str(role or "").strip().lower()
        except Exception:
            r = ""
        if r == "admin":
            return True
        if once_key is not None:
            try:
                with self._remote_lock:
                    if once_key in self._remote_reject_seen:
                        return False
                    self._remote_reject_seen.add(once_key)
                    if len(self._remote_reject_seen) > 256:
                        self._remote_reject_seen.clear()
            except Exception:
                pass
        try:
            self._log_activity(admin or "-", action, pc,
                               f"REJECTED: {r or 'no role'} is not an "
                               f"administrator")
        except Exception:
            pass
        return False

    def start_screen_observe(self, pc, admin="", interval=1.0,
                             quality=50, scale=0.6, role="") -> bool:
        from protocol import build_screen_observe_start
        if not self._observe_role_ok(role, admin, "screen_observe_start",
                                     pc):
            return False
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
        # P1-8: hand the audit row's id to the client as its command_id, so
        # the ack it already sends updates THIS row from Sent -> Done or
        # Failed.  It used to be stamped "Done" on send, which claimed a
        # confirmation the client had not given yet; if the link drops, the
        # row now correctly stays at "Sent" for the Audit Trail to show.
        command_id = self._audit_command(
            pc, "cmd_screen_observe_start",
            {"interval": interval, "quality": quality, "scale": scale},
            admin, status="Sent")
        msg.payload["command_id"] = command_id
        entry.send(msg)
        self._log_activity(admin, "screen_observe_start", pc, f"interval={interval}s")
        return True

    def stop_screen_observe(self, pc, admin="") -> bool:
        from protocol import build_screen_observe_stop
        entry = self.get_client(pc)
        ev = self.screen_watchers.pop(pc, None)
        if ev:
            ev.set()
        # P1-8: audit FIRST so the client's ack (which already carries a
        # command_id) resolves this row from Sent -> Done/Failed, instead
        # of it being pre-stamped "Done" before the PC has been told.
        command_id = self._audit_command(pc, "cmd_screen_observe_stop", {},
                                         admin, status="Sent")
        if entry:
            entry.screen_streaming = False
            msg = build_screen_observe_stop()
            msg.payload["target_pc"] = pc
            msg.payload["command_id"] = command_id
            entry.send(msg)
        else:
            # Nothing could be delivered at all - record that honestly
            # instead of leaving an unresolved "Sent" row behind.
            self._audit_done(command_id, False, "target not connected")
        self._log_activity(admin, "screen_observe_stop", pc, "")
        return True

    # ---------------------------------------------------- screen share
    # Teaching share: THIS machine's screen pushed to every online lab PC
    # (PowerPoint, live coding).  Display-only by construction - a frame
    # is a base64 JPEG string with nothing executable, frames carry no
    # command_id (no per-frame ack/audit), and only ADMINISTRATOR may
    # start or stop a share (the same gate as Observe / Remote).
    #: classroom defaults: text must stay readable (scale 0.75 at 4:4:4
    #: quality 85), and one frame every 200 ms is plenty for slides and
    #: live typing while staying kind to a lab switch's uplink.
    SHARE_INTERVAL = 0.2
    SHARE_QUALITY = 85
    SHARE_SCALE = 0.75

    def start_screen_share(self, admin="", role="") -> dict:
        """Start streaming this machine's screen to every online client."""
        if not self._observe_role_ok(role, admin, "screen_share_start", "-"):
            return {"success": False, "error": "administrator only"}
        if self.share_stop is not None:
            return {"success": True, "already": True}
        from protocol import build_screen_share_start
        with self.clients_lock:
            names = [n for n, e in self.clients.items() if e.is_online]
        if not names:
            return {"success": False, "error": "no PC online"}
        stop = threading.Event()
        accepted = []
        for name in names:
            entry = self.get_client(name)
            if not entry or not entry.is_online:
                continue
            # P1-8 pattern: the ack the client already sends resolves this
            # row from Sent -> Done/Failed, never pre-stamped "Done".
            command_id = self._audit_command(
                name, "cmd_screen_share_start", {"admin": admin},
                admin, status="Sent")
            msg = build_screen_share_start(admin)
            msg.payload["command_id"] = command_id
            if entry.send(msg):
                accepted.append(name)
            else:
                self._audit_done(command_id, False, "send failed")
        if not accepted:
            return {"success": False, "error": "no PC accepted the share"}
        self.share_targets = set(accepted)
        self.share_stop = stop
        threading.Thread(target=self._share_worker, args=(stop, admin),
                         daemon=True, name="screen-share").start()
        self._log_activity(admin, "screen_share_start",
                           f"{len(accepted)} PCs", "")
        return {"success": True, "pcs": accepted}

    def stop_screen_share(self, admin="", role="") -> dict:
        """Stop the active share (idempotent; never raises)."""
        if not self._observe_role_ok(role, admin, "screen_share_stop", "-"):
            return {"success": False, "error": "administrator only"}
        stop = self.share_stop
        if stop is None:
            return {"success": True, "not_sharing": True}
        stop.set()
        self.share_stop = None
        self._log_activity(admin, "screen_share_stop", "-", "")
        # The worker's exit path audits and delivers the STOP to every PC
        # it streamed to, AFTER its last frame - same socket, TCP order,
        # so the Client can never reopen on a frame arriving post-STOP.
        return {"success": True}

    def _share_worker(self, stop, admin=""):
        """Capture -> fan-out loop; owns the STOP delivery on exit.

        A PC whose send fails is dropped from the fan-out (lagging) so one
        wedged client can never stall the class; it still gets a STOP
        attempt, audited honestly as Failed if the link is truly gone.
        """
        from protocol import build_screen_share_frame, build_screen_share_stop
        from utils import capture_screen_b64
        seq = 0
        lagging = set()
        try:
            while not stop.is_set():
                t0 = time.time()
                with self.clients_lock:
                    entries = [(n, e) for n, e in self.clients.items()
                               if e.is_online]
                if not entries:
                    # nobody is watching - don't burn CPU capturing
                    stop.wait(1.0)
                    continue
                try:
                    frame = capture_screen_b64(self.SHARE_QUALITY,
                                               self.SHARE_SCALE)
                except Exception:
                    # capture can fail (locked desktop, no interactive
                    # session) - retry quietly while the share is on
                    stop.wait(1.0)
                    continue
                seq += 1
                for name, entry in entries:
                    if name in lagging:
                        continue
                    self.share_targets.add(name)   # late joiners too
                    # ponytail: sequential blocking sends (same trust as
                    # the file push); per-PC worker + send timeout if a
                    # wedged peer ever proves to matter in practice.
                    if not entry.send(build_screen_share_frame(frame, seq)):
                        lagging.add(name)
                stop.wait(max(0.0, self.SHARE_INTERVAL - (time.time() - t0)))
        finally:
            if self.share_stop is stop:
                self.share_stop = None
            with self.clients_lock:
                names = [n for n in self.share_targets
                         if n in self.clients and self.clients[n].is_online]
            # Phase B: sorted so delivery order is deterministic, and one
            # PC per try/except - a wedged peer or a transient audit error
            # must never swallow the STOP of every PC behind it.
            for name in sorted(names):
                try:
                    entry = self.clients.get(name)
                    if not entry:
                        continue
                    command_id = self._audit_command(
                        name, "cmd_screen_share_stop", {}, admin, status="Sent")
                    msg = build_screen_share_stop()
                    msg.payload["command_id"] = command_id
                    if not entry.send(msg):
                        self._audit_done(command_id, False, "send failed")
                except Exception:
                    continue
            self._log_activity(admin or "-", "screen_share_stop",
                               f"{len(names)} PCs", "share ended")

    # ---------------------------------------------------- remote control
    def get_remote_session(self, pc):
        """The active remote-control session for `pc`, or None."""
        with self._remote_lock:
            sess = self.remote_sessions.get(pc)
            return dict(sess) if sess else None

    def start_remote_control(self, pc, admin="", interval=0.4,
                             quality=55, scale=0.7, role="") -> dict:
        """Start a remote mouse/keyboard session (Task 5).

        Reuses the existing command channel, the existing screen stream
        and the existing `client_commands` audit table - no second network
        stack.  The audit row's id becomes the SESSION id: it is handed to
        the client with CMD_REMOTE_START and has to be echoed back on every
        input batch, so input can never outlive the audited session that
        authorised it.

        P4: `role` must be the ADMINISTRATOR role.  This used to be a
        promise made by the caller; it is now enforced here, at the
        Server, so a STAFF or MAINTENANCE context is refused and audited
        even if it reaches this method by some other route.  A refused
        start writes exactly the same audit row as any other start that
        did not happen, and never opens a stream.
        """
        from protocol import build_command
        params = {"interval": interval, "quality": quality, "scale": scale,
                  "admin_user": admin}
        if not self._observe_role_ok(role, admin, "remote_control_start",
                                     pc):
            cid = self._audit_command(pc, "cmd_remote_start", params, admin)
            self._audit_done(cid, False, "administrator role required")
            return {"success": False,
                    "error": "Remote Control requires the ADMINISTRATOR "
                             "role",
                    "command_id": cid}
        entry = self.get_client(pc)
        if not entry or not entry.is_online:
            cid = self._audit_command(pc, "cmd_remote_start", params, admin)
            self._audit_done(cid, False, f"{pc} is offline")
            self._log_activity(admin, "remote_control_start", pc,
                               "FAILED: client offline")
            return {"success": False, "error": f"{pc} is offline",
                    "command_id": cid}
        # the person actually sitting at the PC - recorded in the audit row
        client_user = str(getattr(entry, "logged_in_user", "") or "-")
        with self._remote_lock:
            existing = self.remote_sessions.get(pc)
            if existing:
                # one session per PC: never stack a second controller onto
                # an already-controlled machine.
                return {"success": True, "already": True,
                        "command_id": existing.get("session_id"),
                        "session_id": existing.get("session_id")}

        # 1) the live screen the admin watches - the Observe stream itself.
        if not self.start_screen_observe(pc, admin, interval, quality, scale,
                                         role=role):
            cid = self._audit_command(pc, "cmd_remote_start", params, admin)
            self._audit_done(cid, False, f"{pc} is offline")
            self._log_activity(admin, "remote_control_start", pc,
                               "FAILED: client offline")
            return {"success": False, "error": f"{pc} is offline",
                    "command_id": cid}

        # 2) audit FIRST, then arm the client with that same id.
        cid = self._audit_command(pc, "cmd_remote_start", params, admin,
                                  status="Sent")
        msg = build_command(MessageType.CMD_REMOTE_START, pc, params,
                            admin, cid)
        if not entry.send(msg):
            self._audit_done(cid, False, "send failed")
            self.stop_screen_observe(pc, admin)
            self._log_activity(admin, "remote_control_start", pc,
                               "FAILED: send failed")
            return {"success": False, "error": "Send failed",
                    "command_id": cid}

        with self._remote_lock:
            self.remote_sessions[pc] = {
                "admin": admin,
                "session_id": cid,
                "client_user": client_user,
                "started_at": now_datetime(),
            }
        # explicit audit row for the Audit Trail's action column (the
        # client_commands row above records the command itself).  It names
        # every party and the connection type the spec asks for.
        self._log_activity(admin, "remote_control_start", pc,
                           f"session_id={cid} type=LAN "
                           f"client_user={client_user} admin={admin or '-'}")
        return {"success": True, "command_id": cid, "session_id": cid}

    def stop_remote_control(self, pc, admin="", reason="stopped",
                            stop_stream=True) -> dict:
        """End the remote-control session for `pc` (Task 5).

        Always writes the audit row - including when the Client is already
        unreachable, so a session that ended in a disconnect still has a
        start row, an end row, its id, the admin and a failure reason.
        """
        from protocol import build_command
        with self._remote_lock:
            sess = self.remote_sessions.pop(pc, None)
        params = {"reason": str(reason or "stopped")}
        if sess:
            params["session_id"] = sess.get("session_id", "")
        cid = self._audit_command(pc, "cmd_remote_stop", params,
                                  admin or (sess or {}).get("admin", ""),
                                  status="Sent")
        entry = self.get_client(pc)
        ok = False
        if entry and entry.is_online and sess:
            msg = build_command(MessageType.CMD_REMOTE_STOP, pc, {}, admin, cid)
            # the client only obeys the stop for the session it is holding
            msg.payload["session_id"] = sess.get("session_id", "")
            ok = bool(entry.send(msg))
        if not ok:
            if not entry or not entry.is_online:
                fail = "client offline - session force-ended"
            else:
                fail = "send failed"
            self._audit_done(cid, False, reason or fail)
        who = admin or (sess or {}).get("admin", "") or "system"
        self._log_activity(
            who, "remote_control_stop", pc,
            ("" if sess else "no active session; ") +
            f"result={'OK' if ok else 'FAILED'} reason={reason or 'stopped'} "
            f"type=LAN client_user={(sess or {}).get('client_user', '-')}" +
            (f" session_id={sess.get('session_id')}" if sess else ""))
        if stop_stream:
            self.stop_screen_observe(pc, who)
        return {"success": ok or not bool(sess), "command_id": cid,
                "session_id": (sess or {}).get("session_id", "")}

    def end_remote_control_on_drop(self, pc, reason="client disconnected") -> bool:
        """Force-end a remote session the moment its Client disappears.

        Called from the heartbeat sweep, the connection-closed path and
        server shutdown.  The Client can never be reconnected back INTO an
        active session: the session is gone from the table first, so a
        re-register starts from a clean state.  `reason` says WHICH way it
        died (no heartbeat vs socket closed vs shutdown) and is carried
        into the audit row, the activity log and the console's
        "Connection: LOST (...)" badge, so a lab retest can tell a stalled
        Client (heartbeats stopped) from a broken link at a glance.
        """
        with self._remote_lock:
            sess = self.remote_sessions.pop(pc, None)
        if not sess:
            return False
        cid = self._audit_command(pc, "cmd_remote_stop",
                                  {"reason": reason,
                                   "session_id": sess.get("session_id", "")},
                                  sess.get("admin", "") or "system",
                                  status="Sent")
        self._audit_done(cid, False, f"{reason} - session ended")
        self._log_activity(sess.get("admin") or "system",
                           "remote_control_stop", pc,
                           f"result=FAILED reason={reason} "
                           f"client_user={sess.get('client_user', '-')} "
                           f"session_id={sess.get('session_id', '')} type=LAN")
        ev = self.screen_watchers.pop(pc, None)
        if ev:
            ev.set()
        entry = self.get_client(pc)
        if entry:
            entry.screen_streaming = False
        self._emit("remote_stopped", {"pc_name": pc,
                                      "reason": reason})
        return True

    def forward_remote_input(self, pc, events, admin="", role="") -> bool:
        """Forward one whitelisted input batch to an ACTIVE remote session.

        Nothing is sent - and therefore nothing can be applied on the
        Client - unless ALL of these hold: the caller holds the
        ADMINISTRATOR role (P4), a session exists for this exact PC, the
        PC is online, and the batch survives
        `protocol.sanitize_remote_events` (mouse move/click/scroll and key
        down/up only).  This is a fire-and-forget data message on the
        existing command channel; it is deliberately NOT a
        `client_commands` row, since one audit row per mouse move would
        drown the audit trail.  A REFUSED batch is audited once per PC and
        role instead: a mouse move is not a security event, but an attempt
        to drive a machine is.
        """
        from protocol import build_remote_input
        if not self._observe_role_ok(role, admin, "remote_input_reject",
                                     pc, once_key=(pc, str(role or "").lower())):
            return False
        with self._remote_lock:
            sess = self.remote_sessions.get(pc)
        if not sess:
            return False
        entry = self.get_client(pc)
        if not entry or not entry.is_online:
            return False
        msg = build_remote_input(sess.get("session_id", ""), events)
        if not msg.payload.get("events"):
            return False                       # everything was filtered out
        return bool(entry.send(msg))

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