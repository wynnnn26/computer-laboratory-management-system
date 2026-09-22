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
    CMD_SCREENSHOT = "cmd_screenshot"
    CMD_SEND_MESSAGE = "cmd_send_message"
    CMD_EXECUTE = "cmd_execute"
    
    # Responses (Client -> Server)
    CMD_RESPONSE = "cmd_response"
    
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
        """Send a framed message. Returns True on success."""
        try:
            with self._lock:
                self.sock.sendall(msg.to_bytes())
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


def build_client_register(pc_name: str, ip: str, mac: str, specs: Dict) -> Message:
    return Message.create(MessageType.CLIENT_REGISTER, {
        "pc_name": pc_name,
        "ip": ip,
        "mac": mac,
        "specs": specs
    })


def build_client_heartbeat(pc_name: str, status: str, cpu: float, ram: float, 
                           logged_in_user: str = None, session_id: str = None) -> Message:
    return Message.create(MessageType.CLIENT_HEARTBEAT, {
        "pc_name": pc_name,
        "status": status,  # "locked", "logged_in", "idle", "maintenance"
        "cpu_percent": cpu,
        "ram_percent": ram,
        "logged_in_user": logged_in_user,
        "session_id": session_id
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


def build_screenshot_request(quality: int = 50, scale: float = 0.5) -> Message:
    return Message.create(MessageType.CMD_SCREENSHOT, {
        "quality": quality,
        "scale": scale
    })


def build_screen_observe_start(interval: float = 1.0, quality: int = 30, scale: float = 0.4) -> Message:
    return Message.create(MessageType.CMD_SCREEN_OBSERVE_START, {
        "interval": interval,
        "quality": quality,
        "scale": scale
    })


def build_screen_observe_stop() -> Message:
    return Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP, {})


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