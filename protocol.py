"""
protocol.py
Secure TCP communication protocol for Lab System Client-Server architecture.
Uses TLS/SSL with JSON message framing.
"""

import json
import ssl
import socket
import struct
import threading
import time
import uuid
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, asdict
from enum import Enum


class MessageType(Enum):
    # Authentication
    AUTH_REQUEST = "auth_request"
    AUTH_RESPONSE = "auth_response"
    AUTH_CHALLENGE = "auth_challenge"
    # First-login password change (fresh client accounts)
    PASSWORD_CHANGE_REQUEST = "password_change_request"
    PASSWORD_CHANGE_RESPONSE = "password_change_response"
    
    # Client Registration & Heartbeat
    CLIENT_REGISTER = "client_register"
    CLIENT_HEARTBEAT = "client_heartbeat"
    CLIENT_STATUS = "client_status"
    CLIENT_DISCONNECT = "client_disconnect"
    
    # Commands (Server -> Client)
    CMD_LOCK = "cmd_lock"
    CMD_UNLOCK = "cmd_unlock"
    CMD_LOGOUT = "cmd_logout"
    CMD_RESTART = "cmd_restart"
    CMD_SHUTDOWN = "cmd_shutdown"
    CMD_PAUSE = "cmd_pause"
    CMD_RESUME = "cmd_resume"
    CMD_SCREEN_OBSERVE_START = "cmd_screen_observe_start"
    CMD_SCREEN_OBSERVE_STOP = "cmd_screen_observe_stop"
    # Screen share (Server -> Client): the Server machine's own screen
    # pushed to lab PCs for teaching (slides, live coding).  Display-only
    # by construction - the frame payload is a base64 JPEG string with
    # nothing the Client could execute, and frames deliberately never
    # carry a command_id (no ack, no audit row per frame).
    CMD_SCREEN_SHARE_START = "cmd_screen_share_start"
    CMD_SCREEN_SHARE_FRAME = "cmd_screen_share_frame"
    CMD_SCREEN_SHARE_STOP = "cmd_screen_share_stop"
    CMD_SCREENSHOT = "cmd_screenshot"
    CMD_SEND_MESSAGE = "cmd_send_message"
    CMD_EXECUTE = "cmd_execute"
    CMD_WEB_FILTER = "cmd_web_filter"
    # Single file push (Server -> Client): the client saves the payload on
    # its Desktop - documents/handouts only, executables are refused on
    # both ends.  CMD_EXECUTE above stays deliberately unhandled.
    CMD_SEND_FILE = "cmd_send_file"
    # Remote control (Server -> Client) - Task 5.  These three are the ONLY
    # messages that can ever move the remote machine's mouse or keyboard.
    # CMD_REMOTE_INPUT carries a whitelisted list of input primitives
    # (mouse move/click/scroll, key down/up) - there is deliberately no
    # "type a string", no "run" and no shell/execute path behind it, so a
    # compromised session still cannot inject commands.
    CMD_REMOTE_START = "cmd_remote_start"
    CMD_REMOTE_STOP = "cmd_remote_stop"
    CMD_REMOTE_INPUT = "cmd_remote_input"
    
    # Responses (Client -> Server)
    CMD_RESPONSE = "cmd_response"

    # Website Access policy ack (Client -> Server): "policy version N was
    # applied" (or why it could not be) - the server only shows SYNCED
    # after this arrives (spec item 8).
    WEB_POLICY_ACK = "web_policy_ack"
    
    # Session Management
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SESSION_UPDATE = "session_update"
    
    # Admin Activity
    ACTIVITY_LOG = "activity_log"
    
    # Student-facing queries (Client -> Server, Server -> Client response)
    STU_REQUEST = "stu_request"
    STU_RESPONSE = "stu_response"

    # Sync
    SYNC_REQUEST = "sync_request"
    SYNC_RESPONSE = "sync_response"
    
    # Error
    ERROR = "error"
    PONG = "pong"


# --------------------------------------------------------------------------
# Pushed-file policy (CMD_SEND_FILE): shared by both ends - the Server
# refuses before the wire, the Client re-refuses even if a rogue Server
# asks.  Documents/handouts only: these suffixes run or auto-start by
# double-click, so a pushed file can never be one of them.
# --------------------------------------------------------------------------
DENY_FILE_EXTS = (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js",
                  ".msi", ".scr", ".lnk")
MAX_PUSH_FILE_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# UDP LAN discovery: a Client broadcasts DISCOVER_MAGIC on
# discovery_udp_port(tcp_port) (spec: 8444 = 8443 + 1); the Server answers
# unicast with REPLY_MAGIC + its address. Pure convenience - the framed
# JSON TCP channel stays the only data path.
# --------------------------------------------------------------------------
DISCOVER_MAGIC = "LAB_SYSTEM_DISCOVER"
REPLY_MAGIC = "LAB_SYSTEM_SERVER"


def discovery_udp_port(tcp_port) -> int:
    """UDP discovery port for a TCP port (8443 -> 8444, spec)."""
    return int(tcp_port) + 1


@dataclass
class Message:
    type: str
    payload: Dict[str, Any]
    msg_id: str = ""
    timestamp: float = 0.0
    
    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = time.time()
    
    def to_bytes(self) -> bytes:
        data = json.dumps({
            "type": self.type,
            "payload": self.payload,
            "msg_id": self.msg_id,
            "timestamp": self.timestamp
        }).encode('utf-8')
        # Frame: 4-byte length (big-endian) + JSON data
        return struct.pack('>I', len(data)) + data
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'Message':
        obj = json.loads(data.decode('utf-8'))
        return cls(
            type=obj["type"],
            payload=obj["payload"],
            msg_id=obj.get("msg_id", ""),
            timestamp=obj.get("timestamp", 0.0)
        )
    
    @classmethod
    def create(cls, msg_type: MessageType, payload: Dict[str, Any]) -> 'Message':
        return cls(type=msg_type.value, payload=payload)


class ProtocolError(Exception):
    pass


class TLSSocketWrapper:
    """Wrapper for TLS socket with framed message reading/writing."""
    
    def __init__(self, sock: ssl.SSLSocket):
        self.sock = sock
        self._buffer = b""
        self._lock = threading.Lock()
    
    def send_message(self, msg: Message) -> bool:
        """Send a framed message. Returns True on success.

        Sends always run on a BLOCKING socket: recv_message() parks the
        shared socket in a short poll timeout (0.5 s in the client and
        server loops), and since Python 3.5 that timeout is the MAXIMUM
        TOTAL DURATION of a sendall() - so a large screen frame that
        cannot be written inside the window on a momentarily congested
        link would raise socket.timeout mid-send and be misread as a
        dead connection, dropping the link (and any active remote-
        control session) for no reason.  The reader's timeout is saved
        and restored around the send; recv_message() re-asserts its own
        timeout at the top of every call anyway.
        """
        try:
            data = msg.to_bytes()
            with self._lock:
                old_timeout = self.sock.gettimeout()
                self.sock.settimeout(None)
                try:
                    self.sock.sendall(data)
                finally:
                    self.sock.settimeout(old_timeout)
            return True
        except Exception as e:
            print(f"Send error: {e}")
            return False
    
    def recv_message(self, timeout: Optional[float] = None) -> Optional[Message]:
        """Receive a complete framed message. Returns None on timeout/disconnect."""
        self.sock.settimeout(timeout)
        try:
            while True:
                # Need at least 4 bytes for length header
                if len(self._buffer) < 4:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return None
                    self._buffer += chunk
                    continue
                
                # Read message length
                msg_len = struct.unpack('>I', self._buffer[:4])[0]
                
                # Sanity check
                if msg_len > 10_000_000:  # 10MB max
                    raise ProtocolError(f"Message too large: {msg_len}")
                
                # Wait for full message
                while len(self._buffer) < 4 + msg_len:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return None
                    self._buffer += chunk
                
                # Extract message
                msg_data = self._buffer[4:4 + msg_len]
                self._buffer = self._buffer[4 + msg_len:]
                
                return Message.from_bytes(msg_data)
                
        except socket.timeout:
            return None
        except (ConnectionResetError, ConnectionAbortedError, ssl.SSLError, OSError):
            return None
        except Exception as e:
            print(f"Receive error: {e}")
            return None
    
    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def create_ssl_context(is_server: bool, certfile: str = "server.crt", keyfile: str = "server.key") -> ssl.SSLContext:
    """Create SSL context for server or client."""
    if is_server:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        # Require client certificate verification (optional - can be disabled for ease of deployment)
        context.verify_mode = ssl.CERT_NONE  # Set to CERT_REQUIRED for mutual TLS
    else:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Accept self-signed certs
    
    # Modern TLS settings
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20')
    return context


def generate_self_signed_cert(certfile: str = "server.crt", keyfile: str = "server.key"):
    """Generate a self-signed certificate for testing."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime
    import ipaddress
    
    # Generate private key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    
    # Create certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u"LabSystem Server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Computer Lab Management"),
    ])
    
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=3650)
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName(u"localhost"),
            x509.IPAddress(ipaddress.IPv4Address(u"127.0.0.1")),
        ]),
        critical=False,
    ).sign(private_key, hashes.SHA256())
    
    # Write files
    with open(keyfile, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    
    print(f"Generated self-signed certificate: {certfile}, {keyfile}")


# Command payload builders
def build_auth_request(username: str, password: str, role: str, client_info: Dict = None) -> Message:
    payload = {
        "username": username,
        "password": password,  # Will be hashed on server
        "role": role,
        "client_info": client_info or {}
    }
    return Message.create(MessageType.AUTH_REQUEST, payload)


def build_auth_response(success: bool, user_data: Dict = None, token: str = None, error: str = None) -> Message:
    payload = {"success": success}
    if user_data:
        payload["user_data"] = user_data
    if token:
        payload["token"] = token
    if error:
        payload["error"] = error
    return Message.create(MessageType.AUTH_RESPONSE, payload)


def build_password_change(new_password: str) -> Message:
    """Client -> Server: replace a default/first-login password.
    The account identity is taken server-side from the authenticated TLS
    entry, never from this payload - a client can only change its OWN
    password, and only while authenticated on this link."""
    return Message.create(MessageType.PASSWORD_CHANGE_REQUEST,
                          {"new_password": new_password})


def build_password_change_response(success: bool, error: str = None) -> Message:
    payload = {"success": success}
    if error:
        payload["error"] = error
    return Message.create(MessageType.PASSWORD_CHANGE_RESPONSE, payload)


def build_client_register(pc_name: str, ip: str, hostname: str) -> Message:
    """Register a client PC. Only the fields the dashboard actually shows
    are transmitted (no bulky hardware specifications)."""
    return Message.create(MessageType.CLIENT_REGISTER, {
        "pc_name": pc_name,
        "ip": ip,
        "hostname": hostname,
    })


def build_client_heartbeat(pc_name: str, status: str, cpu: float, ram: float,
                           logged_in_user: str = None, session_id: str = None,
                           ip: str = None, hostname: str = None,
                           conn_type: str = None) -> Message:
    return Message.create(MessageType.CLIENT_HEARTBEAT, {
        "pc_name": pc_name,
        "status": status,  # "locked", "logged_in", "paused", "idle"
        "cpu_percent": cpu,
        "ram_percent": ram,
        "logged_in_user": logged_in_user,
        "session_id": session_id,
        "ip": ip,
        "hostname": hostname,
        # LAN medium ("Ethernet" / "Wi-Fi" / "LAN") - spec 14: the admin
        # can see how each client reaches the server.
        "conn_type": conn_type,
    })


def build_web_policy(version: int, mode: str, blocked=None, allowed=None,
                     upstream_dns: str = "") -> Message:
    """Server -> Client: the complete Website Access policy snapshot.

    mode: "allow_all" | "block_list" | "allow_only" (per-PC, default from
    system_settings).  Matching is suffix-based on the client
    (example.com covers www./sub.example.com but never notexample.com)."""
    return Message.create(MessageType.CMD_WEB_FILTER, {
        "policy": {
            "version": int(version),
            "mode": mode,
            "blocked": list(blocked or []),
            "allowed": list(allowed or []),
            "upstream_dns": upstream_dns,
        },
    })


def build_web_policy_ack(version: int, mode: str, success: bool,
                         enforced: str = "", error: str = None) -> Message:
    """Client -> Server: acknowledge a policy version.

    enforced: "detect" (connection detection attributes a live socket
    to a domain and to the responsible browser, and closes only that
    browser) or "none" (ALLOW ALL, or the policy could not be enforced
    - in which case success=False and error says why).  The server
    records SYNCED only when success=True (spec item 8 - never show
    applied before the ack)."""
    return Message.create(MessageType.WEB_POLICY_ACK, {
        "version": int(version),
        "mode": mode,
        "success": bool(success),
        "enforced": enforced,
        "error": error,
    })


def build_heartbeat_ack(admin_state: str = "") -> Message:
    """Server -> Client reply to every heartbeat.

    Serves two purposes:
      * liveness - the client knows the link is alive (it force-reconnects
        if no traffic arrives for several heartbeat intervals), which is what
        unsticks a client after the server restarts.
      * state sync - carries the server's authoritative admin state
        ("lock"/"pause" desired state JSON) so both sides stay synchronized.
    """
    return Message.create(MessageType.PONG, {
        "admin_state": admin_state or "",
        "ts": time.time(),
    })


def build_command(cmd_type: MessageType, target_pc: str, params: Dict = None, 
                  admin_user: str = None, command_id: str = None) -> Message:
    return Message.create(cmd_type, {
        "target_pc": target_pc,
        "params": params or {},
        "admin_user": admin_user,
        "command_id": command_id or str(uuid.uuid4())[:8]
    })


def build_command_response(command_id: str, success: bool, result: Any = None, error: str = None) -> Message:
    payload = {"command_id": command_id, "success": success}
    if result is not None:
        payload["result"] = result
    if error:
        payload["error"] = error
    return Message.create(MessageType.CMD_RESPONSE, payload)


def build_session_start(pc_name: str, student_id: str, full_name: str, session_id: str) -> Message:
    return Message.create(MessageType.SESSION_START, {
        "pc_name": pc_name,
        "student_id": student_id,
        "full_name": full_name,
        "session_id": session_id,
        "start_time": time.time()
    })


def build_session_end(session_id: str, end_time: float = None, duration: int = None) -> Message:
    return Message.create(MessageType.SESSION_END, {
        "session_id": session_id,
        "end_time": end_time or time.time(),
        "duration": duration
    })


def build_activity_log(admin_user: str, action: str, target: str, details: str = "") -> Message:
    return Message.create(MessageType.ACTIVITY_LOG, {
        "admin_user": admin_user,
        "action": action,
        "target": target,
        "details": details,
        "timestamp": time.time()
    })


def build_screenshot_request(quality: int = 70, scale: float = 0.75) -> Message:
    return Message.create(MessageType.CMD_SCREENSHOT, {
        "quality": quality,
        "scale": scale
    })


def build_screen_observe_start(interval: float = 1.0, quality: int = 50, scale: float = 0.6) -> Message:
    return Message.create(MessageType.CMD_SCREEN_OBSERVE_START, {
        "interval": interval,
        "quality": quality,
        "scale": scale
    })


def build_screen_observe_stop() -> Message:
    return Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP, {})


# ------------------------------------------------------------- screen share
#: hard ceiling on ONE pushed share frame (base64 chars); a full-HD JPEG
#: at quality 85 stays far below it, anything bigger is a corrupted or
#: hostile payload and is dropped by the Client before decoding.
MAX_SHARE_FRAME_CHARS = 12 * 1024 * 1024


def build_screen_share_start(admin: str = "") -> Message:
    return Message.create(MessageType.CMD_SCREEN_SHARE_START, {
        "admin": admin
    })


def build_screen_share_frame(image_b64: str, seq: int = 0) -> Message:
    return Message.create(MessageType.CMD_SCREEN_SHARE_FRAME, {
        "image": image_b64,
        "seq": seq
    })


def build_screen_share_stop() -> Message:
    return Message.create(MessageType.CMD_SCREEN_SHARE_STOP, {})


# --------------------------------------------------------------- remote I/O
#: every mouse button the remote-control channel is allowed to press
REMOTE_BUTTONS = (1, 2, 3)
#: hard ceiling on one batch of input events (a flood can never queue up)
REMOTE_MAX_EVENTS = 64
#: the only mouse actions and key actions that may ever be forwarded
REMOTE_MOUSE_ACTIONS = ("move", "down", "up", "scroll")
REMOTE_KEY_ACTIONS = ("down", "up")


def sanitize_remote_events(events) -> list:
    """Whitelist one batch of remote input events.

    This is THE security boundary of the remote-control feature: it is
    applied on the server before a batch is sent AND on the client before
    a batch is applied, so neither a buggy viewer nor a tampered message
    can smuggle anything through.  Only these primitives survive:

      * mouse  - move (to a NORMALISED x/y inside the streamed frame),
                 button down/up on button 1/2/3, scroll with a clamped
                 wheel delta;
      * key    - a single key down/up identified by its Tk keysym.

    There is no "type text", no "run", no shell/exec field - anything
    unknown is dropped rather than passed along.
    """
    out: list = []
    if not isinstance(events, (list, tuple)):
        return out
    for ev in events[:REMOTE_MAX_EVENTS]:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        action = ev.get("action")
        if kind == "mouse" and action in REMOTE_MOUSE_ACTIONS:
            try:
                nx = float(ev.get("x"))
                ny = float(ev.get("y"))
            except (TypeError, ValueError):
                continue
            nx = min(1.0, max(0.0, nx))
            ny = min(1.0, max(0.0, ny))
            try:
                button = int(ev.get("button", 1))
            except (TypeError, ValueError):
                button = 1
            if button not in REMOTE_BUTTONS:
                button = 1
            try:
                delta = float(ev.get("delta", 1.0))
            except (TypeError, ValueError):
                delta = 1.0
            delta = min(4.0, max(-4.0, delta))
            out.append({"kind": "mouse", "action": action,
                        "x": nx, "y": ny, "button": button,
                        "delta": delta})
        elif kind == "key" and action in REMOTE_KEY_ACTIONS:
            keysym = str(ev.get("keysym") or "")[:24]
            if not keysym:
                continue
            char = str(ev.get("char") or "")[:8]
            out.append({"kind": "key", "action": action,
                        "keysym": keysym, "char": char})
    return out[:REMOTE_MAX_EVENTS]


def build_remote_input(session_id: str, events) -> Message:
    """Forward a whitelisted batch of mouse/keyboard events.

    `session_id` is the id of the audited CMD_REMOTE_START command that
    armed this session; the client refuses every batch whose id it does
    not currently hold, so input can never outlive the session it belongs
    to.
    """
    return Message.create(MessageType.CMD_REMOTE_INPUT, {
        "session_id": str(session_id or ""),
        "events": sanitize_remote_events(events),
    })


def build_send_message(message: str, msg_type: str = "admin") -> Message:
    return Message.create(MessageType.CMD_SEND_MESSAGE, {
        "message": message,
        "msg_type": msg_type  # "admin", "warning", "info"
    })


def build_error(error: str, code: int = 0) -> Message:
    return Message.create(MessageType.ERROR, {"error": error, "code": code})


def build_stu_request(kind: str, payload: Dict[str, Any] = None) -> Message:
    """Purpose-built parameterized query from a client to the central DB."""
    return Message.create(MessageType.STU_REQUEST, {"kind": kind, "data": payload or {}})


def build_stu_response(ref_id: str, kind: str, ok: bool,
                       data: Any = None, error: str = None) -> Message:
    payload = {"ref": ref_id, "kind": kind, "ok": ok, "data": data if data is not None else []}
    if error:
        payload["error"] = error
    return Message.create(MessageType.STU_RESPONSE, payload)