"""
client.py
LAN Client (kiosk) for the Computer Laboratory Management System.
Runs on every client/lab PC:

  - Connects to the central Server over TLS TCP and auto-registers
    (PC name, IP, MAC, specs via psutil).
  - MANDATORY LOGIN SCREEN: fullscreen, always-on-top window that cannot
    be bypassed (Alt+Tab, Win key, Ctrl+Esc, Alt+F4... are suppressed by a
    low-level keyboard hook while locked).  The PC stays locked until a
    valid account is authenticated against the Server's central database.
  - Reports heartbeats (status / CPU / RAM / logged-in user).
  - Executes remote commands: lock, unlock, logout, pause, resume,
    restart, shutdown, message popup, screenshot and live screen
    observation.
  - Floating session bar (PanCafe-Pro style) shows the logged-in user and
    elapsed session time while the PC is unlocked.
"""

import io
import os
import sys
import json
import ssl
import time
import queue
import base64
import uuid
import socket
import threading
import subprocess
import traceback

import tkinter as tk
from tkinter import ttk, messagebox

import psutil
import platform

from database import get_setting, get_connection, DEFAULT_CLIENT_PASSWORD  # noqa: F401 (local fallback only)
from utils import (style_app, center_window, FONT_TITLE, FONT_LABEL,
                   FONT_HEADER, set_app_icon, get_logo, BG_DARK, ACCENT,
                   DANGER, WARN, ONLINE, CARD, CARD_ALT, BORDER, TEXT,
                   SUBTLE)
import client_api
import local_store          # durable local logs + offline auth roster
try:
    import dns_filter           # adapter-DNS repair (P1.0) only
except Exception:               # a missing module must never break the kiosk
    dns_filter = None
try:
    import web_access           # Website Access: connection-based detection
except Exception:               # a missing module must never break the kiosk
    web_access = None
from protocol import (
    Message, MessageType, TLSSocketWrapper, create_ssl_context,
    build_client_register, build_client_heartbeat, build_auth_request,
    build_session_start, build_session_end, build_command_response,
    build_activity_log, build_error, build_password_change,
    build_web_policy_ack, sanitize_remote_events,
    DISCOVER_MAGIC, REPLY_MAGIC, discovery_udp_port,
)
import customtkinter as ctk
from components import PaddedFrame, eye_icon, image_master

CONFIG_NAME = "lab_config.json"
CLIENT_VERSION = "2.0"
# BG_DARK/ACCENT/DANGER/WARN/ONLINE come from utils (shared palette)
# server-set role -> session-bar display (spec: "Role: ADMINISTRATOR")
ROLE_LABELS = {"admin": "ADMINISTRATOR", "staff": "STAFF",
               "student": "STUDENT", "customer": "CUSTOMER",
               "maintenance": "MAINTENANCE"}

# ==========================================================================
# Task 5 - remote control keyboard/mouse translation (CLIENT side)
# ==========================================================================
# Tk keysym -> Windows virtual-key code.  Together with the printable
# characters VkKeyScanW can resolve, this is the ENTIRE keyboard vocabulary
# a remote session may inject: individual key down/up events only.  There
# is no "type this string", no "run this" and no shell path anywhere in
# this translation layer.
SPECIAL_VK = {
    "BackSpace": 0x08, "Tab": 0x09, "Return": 0x0D, "KP_Enter": 0x0D,
    "Pause": 0x13, "Caps_Lock": 0x14, "Escape": 0x1B, "Esc": 0x1B,
    "space": 0x20, "Prior": 0x21, "Next": 0x22, "End": 0x23, "Home": 0x24,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
    "Print": 0x2C, "Insert": 0x2D, "Delete": 0x2E,
    "Super_L": 0x5B, "Super_R": 0x5C, "Menu": 0x5D,
    "Num_Lock": 0x90, "Scroll_Lock": 0x91,
    "Shift_L": 0xA0, "Shift_R": 0xA1, "Control_L": 0xA2, "Control_R": 0xA3,
    "Alt_L": 0xA4, "Alt_R": 0xA5, "shift": 0x10, "Shift": 0x10,
    "control": 0x11, "Control": 0x11, "ctrl": 0x11, "Ctrl": 0x11,
    "alt": 0x12, "Alt": 0x12, "Meta_L": 0x5B, "Meta_R": 0x5C,
}

# mouse_event flags used by the input injector
MOUSE_MOVE = 0x0001
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004
MOUSE_RIGHTDOWN = 0x0008
MOUSE_RIGHTUP = 0x0010
MOUSE_MIDDLEDOWN = 0x0020
MOUSE_MIDDLEUP = 0x0040
MOUSE_WHEEL = 0x0800
MOUSE_ABSOLUTE = 0x8000
MOUSE_VIRTUALDESK = 0x4000
KEYEVENT_KEYUP = 0x0002
WHEEL_DELTA = 120


# ==========================================================================
# Configuration helpers
# ==========================================================================
def config_path():
    """Config lives next to the running app (works as .py AND frozen .exe,
    where __file__ points into the temp extraction folder)."""
    import sys
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, CONFIG_NAME)


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[CLIENT] Could not save config: {e}")


# ==========================================================================
# System info (psutil)
# ==========================================================================
def get_mac_address() -> str:
    try:
        mac = uuid.getnode()
        return ":".join(f"{(mac >> ele) & 0xFF:02x}" for ele in range(40, -1, -8))
    except Exception:
        return ""


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _classify_lan(names) -> str:
    """Pure classifier: pick the LAN medium from lowercased interface
    names already filtered to active, non-loopback adapters."""
    for n in names:
        if any(k in n for k in ("wi-fi", "wifi", "wireless", "wlan")):
            return "Wi-Fi"
    for n in names:
        if any(k in n for k in ("ethernet", "eth", "local area connection")):
            return "Ethernet"
    return "LAN"


def get_connection_type() -> str:
    """Best-effort LAN medium label: 'Ethernet', 'Wi-Fi' or 'LAN'.

    Spec item 14 (Wi-Fi + Ethernet support): the networking itself works
    over either medium; this only reports which one is in use. Skips
    loopback/virtual adapters and adapters without an IPv4 address."""
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        names = []
        for name, st in stats.items():
            if not st.isup:
                continue
            low = name.lower()
            if low.startswith(("lo", "loopback", "vethernet", "isatap",
                               "teredo", "tap-windows", "wg")):
                continue
            if not any(a.family == socket.AF_INET
                       and not a.address.startswith("127.")
                       for a in addrs.get(name, [])):
                continue
            names.append(low)
        return _classify_lan(names)
    except Exception:
        return "LAN"


def discover_server_ip(tcp_port=8443, timeout=1.5):
    """UDP LAN discovery: broadcast a probe on the discovery port
    (TCP port + 1, spec: 8444) and return the first server address that
    answers, or None so callers can fall back to the saved server_ip."""
    probe = json.dumps({"type": DISCOVER_MAGIC,
                        "client": get_hostname()}).encode("utf-8")
    udp_port = discovery_udp_port(tcp_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        for target in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(probe, (target, udp_port))
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(reply, dict) and reply.get("type") == REPLY_MAGIC:
                ip = str(reply.get("server_ip") or "").strip()
                if ip:
                    return ip
        return None
    finally:
        sock.close()


def snapshot_metrics() -> tuple:
    return (psutil.cpu_percent(interval=0.1), psutil.virtual_memory().percent)


# ==========================================================================
# Screen capture (screenshot / live observation)
# ==========================================================================
def capture_screen_b64(quality: int = 50, scale: float = 0.5) -> str:
    import PIL.Image as PILImage
    from PIL import ImageGrab
    img = ImageGrab.grab()
    if scale and scale != 1.0:
        w, h = int(img.width * scale), int(img.height * scale)
        resample = getattr(PILImage, "Resampling", PILImage)
        img = img.resize((w, h), resample.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(10, min(95, quality)))
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ==========================================================================
# Keyboard hook: suppress bypass hotkeys while the kiosk is locked
# ==========================================================================
class HotkeyBlocker:
    """Low-level keyboard hook (Windows) that swallows dangerous hotkeys.

    It also RAISES the P1-5 panic stop (Ctrl+Shift+Alt+K): the kiosk can
    lose focus to another window, so an emergency chord cannot depend on
    a Tk binding alone.  That path deliberately does not swallow the key
    - both deliveries are allowed because `_panic_stop` is idempotent.
    """

    _VK_LWIN, _VK_RWIN = 0x5B, 0x5C
    _VK_TAB, _VK_ESC, _VK_F4, _VK_DELETE, _VK_SPACE = 0x09, 0x1B, 0x73, 0x2E, 0x20
    _VK_LMENU, _VK_RMENU, _VK_CONTROL = 0xA4, 0xA5, 0x11
    _VK_SHIFT = 0x10
    _VK_K = 0x4B                                  # P1-5 panic stop

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105

    def __init__(self, on_panic=None):
        self.active = False
        self._thread = None
        self._hook_id = None
        self._tid = None
        self._ready = threading.Event()
        # P1-5: zero-argument callback, invoked from the HOOK THREAD when
        # Ctrl+Shift+Alt+K is seen.  The owner must marshal it to Tk.
        self.on_panic = on_panic

    def _key_down(self, vk):
        return bool(ctypes_GetAsyncKeyState(vk) & 0x8000)

    def _panic_chord(self) -> bool:
        """Ctrl + Shift + Alt all held right now - read straight from
        GetAsyncKeyState, so it does not depend on window focus."""
        return (self._key_down(self._VK_CONTROL)
                and self._key_down(self._VK_SHIFT)
                and (self._key_down(self._VK_LMENU)
                     or self._key_down(self._VK_RMENU)))

    def _should_block(self, vk) -> bool:
        # P1-5: raise the panic stop BEFORE the `active` check - it is
        # needed exactly while the kiosk is UNlocked (active=False) and
        # while another window has focus.  Never swallowed: Tk gets its
        # own copy through bind_all and _panic_stop only acts once.
        if vk == self._VK_K and self.on_panic is not None:
            try:
                if self._panic_chord():
                    self.on_panic()
            except Exception:
                pass
        if not self.active:
            return False
        alt = self._key_down(self._VK_LMENU) or self._key_down(self._VK_RMENU)
        ctrl = self._key_down(self._VK_CONTROL)
        if vk in (self._VK_LWIN, self._VK_RWIN):        # Win / Win+X ...
            return True
        if vk == self._VK_TAB and (alt or ctrl):        # Alt+Tab / Ctrl+Tab
            return True
        if vk == self._VK_ESC and (ctrl or alt):        # Ctrl+Esc / Alt+Esc
            return True
        if vk == self._VK_F4 and alt:                   # Alt+F4
            return True
        if vk == self._VK_SPACE and alt:                # Alt+Space
            return True
        if vk == self._VK_DELETE and ctrl and alt:      # Ctrl+Alt+Del (best effort)
            return True
        return False

    def start(self):
        if platform.system() != "Windows":
            return
        if self._thread and self._thread.is_alive():
            self.active = True
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey-hook")
        self._thread.start()
        self._ready.wait(3)
        self.active = True

    def stop(self):
        self.active = False

    def shutdown(self):
        self.active = False
        if self._tid:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0010, 0, 0)  # WM_QUIT
            except Exception:
                pass

    # ---- hook plumbing ---------------------------------------------------
    def _run(self):
        import ctypes
        from ctypes import wintypes

        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD),
                        ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def proc(nCode, wParam, lParam):
            try:
                if nCode >= 0 and wParam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN,
                                             self.WM_KEYUP, self.WM_SYSKEYUP):
                    vk = ctypes.cast(lParam,
                                     ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                    if self._should_block(vk):
                        return 1                       # swallow
            except Exception:
                pass
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._cmp = CMPFUNC(proc)
        try:
            self._hook_id = ctypes.windll.user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL, self._cmp, ctypes.windll.kernel32.GetModuleHandleW(None), 0)
            self._ready.set()
            msg = wintypes.MSG()
            while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook_id:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(self._hook_id)
                except Exception:
                    pass
            self._ready.set()


def ctypes_GetAsyncKeyState(vk):
    import ctypes
    return ctypes.windll.user32.GetAsyncKeyState(vk)


# ==========================================================================
# Client network (TLS)
# ==========================================================================
class ClientNetwork:
    """Maintains the TLS connection to the server and routes messages.

    Reliability rules (Phase A / #12):
      * every heartbeat is acknowledged by the server (PONG), so a dead or
        restarted server is detected within a few seconds;
      * a failed send or a stale link (no traffic for 20 s) forces an
        immediate close, and the connect loop retries every 2.5 s;
      * every (re)connection re-registers the PC with the server.
    """

    STALE_LINK_SECONDS = 20

    def __init__(self, events: "queue.Queue"):
        self.events = events
        self.wrapper: TLSSocketWrapper = None
        self.connected = False
        self.server_ip = ""
        self.server_port = 8443
        self.pc_name = socket.gethostname()
        self.last_recv = time.time()
        self.last_admin_state = ""
        self._stop = threading.Event()
        self._recv_thread = None
        self._conn_thread = None
        self._pending = {}                  # ref -> response payload
        self._pending_cv = threading.Condition()

    # ------------------------------------------------------------- connect
    def start(self, server_ip: str, server_port: int, discover: bool = True):
        # UDP LAN discovery first (broadcast on TCP port + 1); the saved
        # server_ip is the fallback when nobody answers (spec).
        if discover:
            try:
                found = discover_server_ip(server_port, timeout=1.5)
            except Exception:
                found = None
            if found:
                if found != server_ip:
                    # remember the address the server announced so the
                    # next start is instant
                    cfg = load_config()
                    cfg["server_ip"] = found
                    save_config(cfg)
                server_ip = found
        self.server_ip = server_ip
        self.server_port = server_port
        self._stop.clear()
        if self._conn_thread is None or not self._conn_thread.is_alive():
            self._conn_thread = threading.Thread(target=self._connect_loop,
                                                 daemon=True, name="net-connect")
            self._conn_thread.start()

    def stop(self):
        self._stop.set()
        self._close_socket()
        client_api.set_link(None)

    def _close_socket(self):
        self.connected = False
        if self.wrapper:
            self.wrapper.close()
            self.wrapper = None
        client_api.set_link(None)

    def _drop_connection(self, reason, wrapper=None):
        """Force-close a dead link and tell the UI to show the lock/reconnect
        screen exactly once.

        `wrapper` identifies the connection the caller saw die: a leftover
        thread from the PREVIOUS link must never kill the freshly
        reconnected one (spec item 13 - no duplicate/stomped connections)."""
        if not self.connected:
            return
        if wrapper is not None and wrapper is not self.wrapper:
            return                      # stale thread of an older socket
        self._close_socket()
        self.events.put({"kind": "net_disconnected"})
        self.events.put({"kind": "net_status", "text": reason})

    def _connect_loop(self):
        while not self._stop.is_set():
            if not self.connected:
                self.events.put({"kind": "net_status",
                                 "text": f"Connecting to server {self.server_ip}:{self.server_port}…"})
                try:
                    raw = socket.create_connection((self.server_ip, self.server_port), timeout=5)
                    ctx = create_ssl_context(is_server=False)
                    tls = ctx.wrap_socket(raw, server_hostname=self.server_ip)
                    wrapper = TLSSocketWrapper(tls)
                    self.wrapper = wrapper
                    self.last_recv = time.time()
                    # Register immediately (re-registers after every
                    # server restart / reconnect).
                    reg = build_client_register(self.pc_name, get_local_ip(),
                                                get_hostname())
                    wrapper.send_message(reg)
                    self.connected = True
                    client_api.set_link(self)
                    self.events.put({"kind": "net_status", "text": "Connected to server ✓"})
                    self.events.put({"kind": "net_connected"})
                    # One recv thread PER connection: it reads only the
                    # wrapper it was started with, and the loop condition
                    # ends it as soon as a newer connection replaces it -
                    # so reconnects can never double-read or duplicate
                    # message handling (spec item 13).
                    self._recv_thread = threading.Thread(
                        target=self._recv_loop, args=(wrapper,),
                        daemon=True, name="net-recv")
                    self._recv_thread.start()
                except Exception as e:
                    self.events.put({"kind": "net_status",
                                     "text": f"Server unreachable ({e}). Retrying…"})
            else:
                # Liveness: the server ACKs every heartbeat (<=5 s apart).
                # No traffic for 20 s means the link/server is gone.
                if time.time() - self.last_recv > self.STALE_LINK_SECONDS:
                    self._drop_connection(
                        "Server stopped responding. Reconnecting…")
            time.sleep(2.5)

    def _recv_loop(self, wrapper):
        # `wrapper` is captured for the whole thread: only THIS connection's
        # socket is read, and the loop exits once a reconnect swaps it out.
        while (not self._stop.is_set() and self.connected
               and self.wrapper is wrapper):
            msg = wrapper.recv_message(timeout=0.5)
            if msg is None:
                # distinguish timeout vs disconnect
                try:
                    wrapper.sock.getpeername()
                    continue
                except Exception:
                    pass
                self._drop_connection("Connection lost. Reconnecting…",
                                      wrapper)
                break
            self.last_recv = time.time()
            # Heartbeat acknowledgement / admin state sync
            if msg.type == MessageType.PONG.value:
                self.last_admin_state = msg.payload.get("admin_state", "")
                continue
            # Route responses for blocking .request() calls
            if msg.type == MessageType.STU_RESPONSE.value:
                ref = msg.payload.get("ref") or msg.msg_id
                with self._pending_cv:
                    self._pending[ref] = msg.payload
                    self._pending_cv.notify_all()
                continue
            self.events.put({"kind": "message", "msg": msg})

    # ---------------------------------------------------------------- send
    def send(self, msg: Message) -> bool:
        if not self.connected or not self.wrapper:
            return False
        ok = self.wrapper.send_message(msg)
        if not ok:
            # A failed write means the server is gone - reconnect at once
            # instead of pretending to stay connected.
            self._drop_connection("Connection lost. Reconnecting…")
        return ok

    def request(self, kind: str, payload: dict, timeout: float = 8.0) -> dict:
        """Blocking purpose-built query; waits for the server's response."""
        from protocol import build_stu_request
        msg = build_stu_request(kind, payload)
        if not self.send(msg):
            raise ConnectionError("Not connected to server")
        deadline = time.time() + timeout
        with self._pending_cv:
            while time.time() < deadline:
                if msg.msg_id in self._pending:
                    resp = self._pending.pop(msg.msg_id)
                    if not resp.get("ok"):
                        return {"ok": False, "error": resp.get("error", "Query failed"),
                                "data": []}
                    return resp
                self._pending_cv.wait(min(0.4, max(0.05, deadline - time.time())))
        raise TimeoutError(f"Server did not answer '{kind}' in {timeout}s")


# ==========================================================================
# Website Access - the legacy hosts-file block (cleared, never written)
# ==========================================================================
HOSTS_BLOCK_BEGIN = "# LAB_SYSTEM WEB FILTER BEGIN (managed by server)"
HOSTS_BLOCK_END = "# LAB_SYSTEM WEB FILTER END"


def apply_web_filter(domains, hosts_path=None):
    """Remove the Website Access block from the Windows hosts file.

    Website Access is enforced by CONNECTION DETECTION now (P1.4), so a
    block is never written any more - `domains` is accepted only for the
    legacy CMD_WEB_FILTER shape and is deliberately ignored.  Every call
    therefore means "make sure none of our block is still there", which
    is exactly what ALLOW ALL, startup and every migration path need.

    The entries live between two managed marker comments so everything
    outside the block is preserved.  Returns True when the file was
    verified clear of our markers (or had nothing to clear), False when
    a leftover block could not be removed (the hosts file needs
    administrator rights) - never raises, so the kiosk is never disturbed.
    """
    path = hosts_path or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "drivers", "etc", "hosts")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        text = ""
    begin = text.find(HOSTS_BLOCK_BEGIN)
    if begin == -1:
        return True                      # nothing of ours: nothing to write
    end = text.find(HOSTS_BLOCK_END, begin)
    tail = text[end + len(HOSTS_BLOCK_END):] if end != -1 else ""
    text = (text[:begin] + tail).rstrip("\n")
    if text:
        text += "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError:
        return False


# ==========================================================================
# Website Access - versioned policy application (spec items 6/7/8)
# ==========================================================================
# Website Access is enforced by DETECTION (P1.1-P1.4): the detector
# mirrors the very snapshot normalize_web_policy produced, so the
# version, mode and rule lists it acts on are the ones the server
# versioned - never a re-derived copy.  The DNS proxy and the hosts
# block that used to enforce it are gone.
_web_lock = threading.RLock()
_web_detector = None             # web_access.WebAccessDetector while running

_WEB_SCAN_EVERY = 1              # seconds between detection scans

_WEB_MODES = ("allow_all", "block_list", "allow_only")


def normalize_web_policy(policy, domains=None):
    """Canonical snapshot {version, mode, blocked, allowed, upstream_dns}
    from a new-style policy dict (or a legacy plain domain list)."""
    if isinstance(policy, dict):
        mode = policy.get("mode") or "allow_all"
        if mode not in _WEB_MODES:
            mode = "allow_all"
        try:
            version = int(policy.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        return {"version": version, "mode": mode,
                "blocked": [str(d) for d in policy.get("blocked") or []],
                "allowed": [str(d) for d in policy.get("allowed") or []],
                "upstream_dns": str(policy.get("upstream_dns") or "")}
    return {"version": 0, "mode": "block_list",
            "blocked": [str(d) for d in (domains or [])],
            "allowed": [], "upstream_dns": ""}


def _load_dns_snapshot():
    """The DNS state an OLDER build recorded before repointing the
    adapter.  P1.4 never repoints any more, so this is read-only
    history - but it is the only honest "what to write back" for a PC
    a killed run left stuck, and repair_adapter_dns() falls back to
    dropping the loopback entry (then a DHCP reset) without it."""
    try:
        snap = load_config().get("web_dns_snapshot")
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None


def repair_local_dns(probe_timeout=0.6):
    """Heal a leftover 127.0.0.1 repoint.  Returns (ok, error).

    A previous run killed by the watchdog, a crash or a power cut never
    executes its restore, so the adapter is left pointing at a resolver
    that no longer exists and the PC has NO name resolution at all.
    This runs on every startup and on every ALLOW ALL - even when no
    filter object is alive in this process, which is the usual state
    after such a restart.  Never raises."""
    if dns_filter is None:
        return True, ""
    try:
        acted, ok, err = dns_filter.repair_adapter_dns(
            _load_dns_snapshot(), probe_timeout=probe_timeout)
    except Exception as e:
        return False, str(e) or "DNS repair raised"
    if acted:
        try:
            local_store.log_event(
                "INFO" if ok else "WARN", "policy",
                "Repaired a leftover 127.0.0.1 DNS repoint" if ok
                else "Could not repair the adapter's DNS",
                err or "")
        except Exception:
            pass
    return ok, err or ""


def _hosts_block_present(hosts_path=None):
    """True when our managed block is actually in the hosts file, so a
    write failure only counts as an error when there was something to
    clear (a read-only file with no block of ours is not a failure)."""
    path = hosts_path or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "drivers", "etc", "hosts")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return HOSTS_BLOCK_BEGIN in f.read()
    except OSError:
        return False


def _web_ensure(policy):
    """Install the snapshot into the Website Access detector (P1.1).

    The detector mirrors exactly what normalize_web_policy produced, so
    the version, mode and rule lists it acts on are the ones the server
    versioned - never a re-derived copy.  Returns (ok, error).
    Never raises: a missing module or a bad snapshot must not disturb
    the kiosk."""
    global _web_detector
    if web_access is None:
        return False, "Website Access detection module unavailable"
    try:
        with _web_lock:
            det = _web_detector
            if det is None:
                det = web_access.WebAccessDetector()
                _web_detector = det
            det.set_policy(policy if isinstance(policy, dict) else {})
        return True, ""
    except Exception as e:
        return False, f"detection failed to start ({e})"


def _web_forget():
    """Stop the detector (client shutdown).  Returns (ok, error)."""
    global _web_detector
    with _web_lock:
        det, _web_detector = _web_detector, None
    if det is None:
        return True, ""
    try:
        det.stop()
        return True, ""
    except Exception as e:
        return False, str(e) or "detection failed to stop"


def _web_probe():
    """(ok, error): can the detector actually see who owns a socket?

    Without that, no connection can ever be attributed to a browser and
    nothing at all is enforced - so the policy has to be reported FAILED
    instead of SYNCED.  This is the honesty the old DNS path owed the
    server.  Never raises."""
    with _web_lock:
        det = _web_detector
    if det is None:
        return False, "Website Access detection is not running"
    try:
        state, detail = det.status()
    except Exception as e:
        return False, str(e) or "Website Access detection is not running"
    if state != "OK":
        return False, detail or "Website Access detection is unavailable"
    return True, ""


def _web_note():
    """The detector's soft diagnostic (e.g. 'no policy domain resolved').

    Surfaced in the policy event so a resolution gap is visible in the
    durable log and on the server without failing a policy whose
    mechanism is demonstrably working.  Never raises."""
    try:
        with _web_lock:
            det = _web_detector
        return str((det.last_error if det is not None else "") or "").strip()
    except Exception:
        return ""


def apply_web_policy(policy, hosts_path=None, manage_dns=True):
    """Apply a Website Access policy snapshot.  Returns (ok, enforced,
    error) - enforced is "detect" | "none".

    Enforcement is DETECTION (P1.4): the snapshot is installed in the
    detector, which proves access with a real connection, attributes it
    to a domain and then to the responsible browser, and closes only
    that browser.  Nothing is written to the hosts file any more and the
    adapter's DNS is never repointed - the proxy that used to do that is
    gone, because a run killed before its restore left the PC with no
    name resolution at all.  `manage_dns` therefore only gates the P1.0
    REPAIR of such a leftover, never an enforcement change.

      allow_all  -> repair a stuck adapter and clear a legacy block;
                    nothing is enforced, and success is claimed only
                    after those steps really succeeded
      other mode -> install the snapshot, clear any legacy block that
                    would silently defeat detection, then PROBE that the
                    detector can read the socket table; if it cannot the
                    answer is FAILED, never "enforced"

    The snapshot is persisted to lab_config.json so it survives restarts
    and offline runs.  Never raises: the kiosk is never disturbed."""
    pol = normalize_web_policy(policy)
    try:
        cfg = load_config()
        cfg["web_policy"] = pol
        save_config(cfg)
    except Exception:
        pass
    # the detector mirrors the snapshot in every mode (allow_all makes it
    # a no-op), so the version it acts on is always the one just stored
    det_ok, det_err = _web_ensure(pol)

    def _done(out):
        # every application becomes a durable local event (spec item 9):
        # version, mode and how it ended up being enforced (or why not)
        detail = out[2] or ""
        if out[0] and out[1] == "detect":
            detail = _web_note()          # soft detector note, if any
        local_store.log_event(
            "INFO" if out[0] else "WARN", "policy",
            (f"Policy v{pol['version']} applied: {pol['mode']} "
             f"-> {out[1]} enforced") if out[0] else
            (f"Policy v{pol['version']} NOT applied: {pol['mode']}"),
            detail)
        return out

    # a block written by an older build would keep the connection from
    # ever happening, which makes detection blind - it has to go
    had_block = _hosts_block_present(hosts_path)
    hosts_ok = apply_web_filter([], hosts_path=hosts_path)

    if pol["mode"] == "allow_all":
        # Every step must actually succeed before we claim the policy is
        # off: a repair that failed - or a leftover block inherited from
        # an older build - means the PC is still restricted, and the
        # server has to see FAILED instead of SYNCED.
        repair_ok, repair_err = (True, "")
        if manage_dns:
            # runs even when no snapshot is loaded here (the usual state
            # after a restart or a watchdog kill)
            repair_ok, repair_err = repair_local_dns()
        if not repair_ok:
            return _done((False, "none", repair_err))
        if had_block and not hosts_ok:
            return _done((False, "none",
                          "hosts file not writable - the leftover block "
                          "could not be cleared"))
        return _done((True, "none", None))

    if had_block and not hosts_ok:
        return _done((False, "none",
                      "hosts file not writable - a leftover block could "
                      "not be cleared and would defeat detection"))
    if not det_ok:
        return _done((False, "none", det_err))
    det_ok, det_err = _web_probe()
    if not det_ok:
        return _done((False, "none", det_err))
    return _done((True, "detect", None))


def _startup_web_policy(saved_policy=None):
    """Startup order for Website Access: REPAIR first, then re-apply.

    A run killed by the watchdog, a crash or a power cut never executes
    its restore, so the adapter is left pointed at a resolver that no
    longer exists and the PC has no name resolution at all.  Repointing
    the adapter again before that is repaired would record 127.0.0.1 as
    the "previous" value all over again.  Never raises: this runs on the
    kiosk's init path."""
    try:
        repair_local_dns()
    except Exception:
        pass
    if isinstance(saved_policy, dict) and saved_policy.get("mode"):
        try:
            apply_web_policy(saved_policy)
        except Exception:
            pass


# ==========================================================================
# P0-1: one instance per PC + notification-area (system tray) presence
# ==========================================================================
SINGLE_INSTANCE_MUTEX = "Local\\ComputerLaboratoryClient"
_SINGLE_INSTANCE_HANDLE = None


def acquire_single_instance():
    """Prevent a second Client from starting on this PC (P0-1).

    A named Windows mutex lives exactly as long as this process holds it,
    so a second `client.exe` sees ERROR_ALREADY_EXISTS and exits instead
    of stacking a second fullscreen lock screen over the first one.
    Returns True when this process now owns the mutex.

    A broken guard must never stop the kiosk from starting: any failure
    (or a non-Windows platform) falls through to True.
    """
    global _SINGLE_INSTANCE_HANDLE
    if platform.system() != "Windows":
        return True
    if _SINGLE_INSTANCE_HANDLE is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        # use_last_error=True + explicit prototypes: without them the
        # 64-bit handle would be truncated to 32 bits and GetLastError
        # would report the wrong value.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True                    # cannot tell - let it run
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_HANDLE = handle
        return True
    except Exception:
        return True


def release_single_instance():
    """Free the single-instance mutex.  Idempotent, never raises."""
    global _SINGLE_INSTANCE_HANDLE
    handle, _SINGLE_INSTANCE_HANDLE = _SINGLE_INSTANCE_HANDLE, None
    if handle is None or platform.system() != "Windows":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _tray_logo(size=64):
    """PIL RGBA of the official logo - square, centred, for the tray.

    Same single source asset as every other surface
    (assets/images/logo.png), resolved from sys._MEIPASS when frozen.
    Returns None if the asset is missing or unreadable; the caller then
    simply runs without a tray icon instead of failing to start.
    """
    try:
        import sys as _sys
        from PIL import Image
        if getattr(_sys, "frozen", False):
            search = [getattr(_sys, "_MEIPASS", ""),
                      os.path.dirname(os.path.abspath(_sys.executable))]
        else:
            search = [os.path.dirname(os.path.abspath(__file__))]
        path = None
        for d in search:
            if not d:
                continue
            p = os.path.join(d, "assets", "images", "logo.png")
            if os.path.exists(p):
                path = p
                break
        if not path:
            return None
        im = Image.open(path).convert("RGBA")
        bbox = im.getchannel("A").getbbox()
        if bbox:
            im = im.crop(bbox)
        w, h = im.size
        side = max(w, h)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - w) // 2, (side - h) // 2))
        resample = getattr(Image, "Resampling", Image)
        return square.resize((int(size), int(size)), resample.LANCZOS)
    except Exception:
        return None


# P0-3: offline-login roster refresh interval, in seconds of 1 s ticker
# ticks.  Comfortably inside the default max_offline_days (7) and cheap -
# one small TLS round trip - so a long-lived connection can never leave
# the cached roster stamped older than the offline window allows.
ROSTER_REFRESH_EVERY = 6 * 3600


# ==========================================================================
# Client application (fullscreen kiosk + floating session bar)
# ==========================================================================
class _KioskState(str):
    """The kiosk state value: a plain string that can also be *called*.

    `ClientApp.state` has always carried the state-machine text ("login",
    "unlocked", "admin_lock", ...) and shares its name with Tk's `wm state`
    command.  Under plain Tk that collision never mattered, because nothing in
    the event loop ever called `state()`.  CustomTkinter does - three ways:

      * `CTk.mainloop` -> `_windows_set_titlebar_color` reads and restores the
        saved window state (`ctk_tk.py:279/323/325`)
      * `ScalingTracker.check_dpi_scaling` queries `window.state()`
        (`scaling_tracker.py:178`)
      * `CTkToplevel` does the same for its own windows

    A plain `str` therefore raised `TypeError: 'str' object is not callable`
    on the first mainloop tick and took client.exe down at launch.  The test
    suites stayed green because they drive the widgets without ever entering
    `mainloop`, which is exactly why the packaged executable had to be run to
    see it.  Re-wrapping the identical string so it is callable leaves every
    existing read (`self.state == STATE_LOGIN`) byte-for-byte compatible while
    letting Tk's calls through to the real `wm state` method.
    """

    def __new__(cls, window, value):
        obj = str.__new__(cls, value)
        obj._window = window
        return obj

    def __call__(self, newstate=None, *args, **kwargs):
        # Resolve through the *class*: the instance attribute is this very
        # string, so a plain `self._window.state(...)` would recurse into it.
        return type(self._window).state(self._window, newstate)


class ClientApp(ctk.CTk):
    STATE_LOGIN = "login"
    STATE_UNLOCKED = "unlocked"
    STATE_PAUSED = "paused"
    STATE_ADMIN_LOCK = "admin_lock"

    def __setattr__(self, name, value):
        # keep `self.state = "..."` assigning a callable string (see _KioskState)
        if name == "state" and type(value) is str:
            value = _KioskState(self, value)
        super().__setattr__(name, value)

    def __init__(self):
        super().__init__()
        self.state = self.STATE_LOGIN
        self.events = queue.Queue()
        self.net = ClientNetwork(self.events)
        # P1-5: the keyboard hook raises the panic stop too, so it still
        # works when another window has stolen the kiosk's focus.
        self.hotkeys = HotkeyBlocker(on_panic=self._panic_from_hook)

        self.user = None                 # dict of logged-in user
        self.session_id = None
        self.session_start = None
        self.observe_stop = None
        # P1-7: which admin asked for the stream, and the clamped capture
        # settings actually in force.  (No on-screen badge is raised: the
        # client-side "OBSERVING" overlay was removed - observation stays
        # visible on the Server/Admin side and in the audit trail.)
        self.observe_ref = None
        self.observe_cfg = {}
        # Task 5: an ACTIVE remote-control session, if any.  `_remote_session`
        # holds the id of the CMD_REMOTE_START command that armed it; the
        # input gate only opens while that id AND a live server link are
        # both present.  It is cleared on stop, on disconnect and on exit,
        # so a reconnect can never walk back into an open session.
        self._remote_active = False
        self._remote_session = None
        self._remote_banner = None
        self._remote_keys = set()        # VKs currently held down remotely
        self.offline_session = False      # Local Mode sign-in (spec 10/12)
        self._log_flush_lock = threading.Lock()   # single-flight log sync
        self._log_flush_due = 0
        # P1.3: Website Access detection is single-flight too, and never
        # runs on the UI thread (a socket scan costs tens of ms).
        self._web_scan_lock = threading.Lock()
        self._web_scan_due = 0
        # P0-3: timer for the periodic offline-login roster refresh (see
        # _roster_refresh_due) - `synced_at` only advances on a pull.
        self._roster_due = 0
        self.lock_reason = ""
        self.pause_win = None
        self.pause_count_lbl = None
        self._pause_msg_lbl = None
        self.pause_message = ""
        self._power_token = None         # armed only by an admin power command
        self._after_verify = None
        # command_id de-duplication: a re-delivered command is never executed
        # twice (the first delivery was already acknowledged).
        self._seen_cmd_ids = set()
        # P0-1: notification-area icon (see start_tray/stop_tray).  None
        # until start_tray() runs - the kiosk itself works either way.
        self._tray = None
        self._tray_ready = None
        self._tray_ok = False
        # P0-2: Server Settings dialog (see _ask_server_config)
        self._settings_win = None

        # --- root/kiosk window ------------------------------------------
        self.overrideredirect(True)
        self.configure(fg_color=BG_DARK)
        set_app_icon(self)
        self.attributes("-topmost", True)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        self.protocol("WM_DELETE_WINDOW", self._ignore_close)
        # dedicated host so rebuilding the kiosk never destroys Toplevels
        # (session bar / pause overlay)
        self.kiosk_host = ctk.CTkFrame(self, fg_color=BG_DARK)
        self.kiosk_host.pack(fill="both", expand=True)

        style_app(self)
        self._build_kiosk()
        self._build_bar()

        # P1 emergency force-unlock: Ctrl+Shift+Alt+M releases a locked
        # kiosk locally (recovery when the Server is unreachable).  The
        # combo is deliberately NOT on the blocker's block list, so it
        # works even while the keyboard hook is active.
        self.bind_all("<Control-Shift-Alt-M>", self._emergency_force_unlock)

        # P1-5 panic stop: Ctrl+Shift+Alt+K ends the session and locks the
        # PC immediately - the exact counterpart to M above, and a different
        # key so the two can never be confused.  M works as a plain Tk
        # binding because the LOCKED kiosk is mapped and focused.  K is the
        # opposite case: while a session runs the kiosk is WITHDRAWN, so a
        # Tk binding would never see the chord - the global keyboard hook in
        # HotkeyBlocker is what actually delivers it (_panic_from_hook).
        # This binding is the belt-and-braces copy; _panic_stop acts only
        # once, so double delivery is harmless.
        self.bind_all("<Control-Shift-Alt-K>", self._panic_stop)

        # start networking (server IP is saved once and reused automatically)
        cfg = load_config()
        if cfg.get("server_ip"):
            self.net.start(cfg["server_ip"], int(cfg.get("server_port", 8443)))
        else:
            # First run: stay visible and locked, ask for the server IP only
            # when the operator clicks the settings button (no modal loop,
            # no hidden window - closing/cancelling never traps the kiosk).
            self.after(200, self._announce_unconfigured)

        # Website Access (spec 9/17): repair any 127.0.0.1 repoint a
        # killed run left behind FIRST, then re-apply the last received
        # policy so restarts and offline runs keep enforcing it.  Order
        # matters - repointing before the repair would record 127.0.0.1
        # as the "previous" value all over again.  Runs off the init path
        # (both steps can take a moment: PowerShell + the DNS bind).
        saved_policy = cfg.get("web_policy")
        threading.Thread(target=_startup_web_policy,
                         args=(saved_policy,), daemon=True).start()

        # Durable local event log (spec item 9): survives disconnects and
        # restarts, flushed to the server when the link comes back.
        try:
            local_store.init_local_db()
            local_store.log_event("INFO", "kiosk", "Client kiosk started",
                                  f"pc={self.net.pc_name}")
        except Exception:
            pass

        self.hotkeys.start()
        self._pump()
        self._tick()

    # ----------------------------------------------------------------- UI
    def _ignore_close(self):
        pass        # kiosk can never be closed by the user

    def _build_kiosk(self):
        """Clean centered login card (spec):

            Computer Laboratory / Management System
            ● Server Online  192.168.x.x:8443
            Student ID [____]  Password [____] [Show]
            [ LOGIN ]
            PC-01 • ONLINE                 LAN Connection

        Only the look changed: still fullscreen, still hotkey-blocked,
        still locked until the SERVER authenticates the credentials."""
        # any first-login change-screen state dies with the rebuild
        self._pw_change_active = False
        self._pending_pw_user = None
        for w in self.kiosk_host.winfo_children():
            w.destroy()

        wrap = ctk.CTkFrame(self.kiosk_host, fg_color=BG_DARK)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        # dark-mode card: same CARD/BORDER surface the rest of the app
        # uses, so the lock screen matches the Server login page.
        card = PaddedFrame(wrap, fg_color=CARD, border_color=BORDER,
                        border_width=1, padx=2, pady=2)
        card.pack(fill="x")

        inner = ctk.CTkFrame(card, fg_color=CARD)
        inner.pack(padx=42, pady=(22, 22))

        # Official logo - one source asset (assets/images/logo.png) used by
        # the whole product.  Kept on self so Tk never garbage-collects it.
        self._login_logo = get_logo(96, self)
        if self._login_logo is not None:
            ctk.CTkLabel(inner, image=self._login_logo, fg_color=CARD).pack(
                pady=(0, 12))

        ctk.CTkLabel(inner, text="Computer Laboratory",
                 font=("Segoe UI", 20, "bold"), fg_color=CARD,
                 text_color=TEXT).pack()
        ctk.CTkLabel(inner, text="Management System", font=("Segoe UI", 12),
                 fg_color=CARD, text_color=SUBTLE).pack(pady=(0, 14))

        # server status: clear, but small so it never dominates the card
        srv = PaddedFrame(inner, fg_color=CARD_ALT, border_color=BORDER,
                       border_width=1, padx=10, pady=4)
        srv.pack(pady=(0, 14))
        # remembered so _set_status_hint can restore the hint line directly
        # under it after it has been hidden (it must never reappear at the
        # bottom of the card, past the fields it explains).
        self._srv_strip = srv
        self.srv_state_lbl = ctk.CTkLabel(srv, text="● Server Online",
                                      font=("Segoe UI", 9, "bold"),
                                      text_color=ONLINE, fg_color=CARD_ALT)
        self.srv_state_lbl.pack(side="left")
        cfg0 = load_config()
        self.srv_addr_lbl = ctk.CTkLabel(
            srv,
            text=f"{cfg0.get('server_ip', '\u2014')}:{cfg0.get('server_port', 8443)}",
            font=("Consolas", 9), text_color=SUBTLE, fg_color=CARD_ALT)
        self.srv_addr_lbl.pack(side="left", padx=(8, 0))

        # Status line: ONLY actionable text lives here (the first-run
        # "IP not configured" hint, or why a link failed).  The chip above
        # is the single status indicator (spec 15) - echoing
        # "Connecting..."/"Connected to server" underneath it as a second
        # message was the duplicate-indicator defect - so this label is
        # born empty and unpaked: _set_status_hint packs it after the chip
        # only when there is something worth saying.
        self.status_lbl = ctk.CTkLabel(inner, text="",
                                   font=("Segoe UI", 9), text_color=WARN, fg_color=CARD,
                                   wraplength=300, justify="center")

        ctk.CTkLabel(inner, text="Student ID", font=("Segoe UI", 9, "bold"),
                 fg_color=CARD, text_color=TEXT, anchor="w").pack(fill="x")
        self.id_var = tk.StringVar()
        self.id_entry = ctk.CTkEntry(inner, textvariable=self.id_var,
                                  font=("Segoe UI", 12))
        self.id_entry.pack(fill="x", pady=(3, 12))

        ctk.CTkLabel(inner, text="Password", font=("Segoe UI", 9, "bold"),
                 fg_color=CARD, text_color=TEXT, anchor="w").pack(fill="x")
        pw_row = ctk.CTkFrame(inner, fg_color=CARD)
        pw_row.pack(fill="x", pady=(3, 18))
        self.pw_var = tk.StringVar()
        self.pw_entry = ctk.CTkEntry(pw_row, textvariable=self.pw_var,
                                  font=("Segoe UI", 12), show="\u25cf")
        self.pw_entry.pack(side="left", fill="x", expand=True)
        self.pw_entry.bind("<Return>", lambda e: self.attempt_login())
        # visibility eye instead of a text button (display only - the
        # password never leaves the normal auth path).  Both glyphs are
        # built once and kept on self so Tk never garbage-collects them;
        # `_eye_state` mirrors the old Show/Hide text for the tests.
        self._eye_show = ctk.CTkImage(light_image=eye_icon(False, SUBTLE, CARD),
                                      size=(20, 20))
        self._eye_hide = ctk.CTkImage(light_image=eye_icon(True, SUBTLE, CARD),
                                      size=(20, 20))
        with image_master(self):          # cached PhotoImage -> this window
            self.pw_show_btn = ctk.CTkButton(pw_row, text="", width=40,
                                         image=self._eye_show,
                                         command=self._toggle_pw_visibility,
                                         fg_color=CARD, text_color="white",
                                         corner_radius=6, cursor="hand2",
                                         hover_color=CARD_ALT)
        self.pw_show_btn._eye_state = "show"
        self.pw_show_btn.pack(side="right", padx=(6, 0))
        self.id_var.set("")
        self.pw_var.set("")

        self.login_btn = ctk.CTkButton(inner, text="LOGIN",
                                   command=self.attempt_login,
                                   fg_color=ACCENT, text_color="white",
                                   font=("Segoe UI", 12, "bold"),
                                   hover_color="#1e4fbf",
                                   cursor="hand2")
        self.login_btn.pack(fill="x")

        # footer: PC name + connection state | LAN Connection
        div = ctk.CTkFrame(inner, fg_color=BORDER, height=1)
        div.pack(fill="x", pady=(16, 8))
        div.pack_propagate(False)
        foot = ctk.CTkFrame(inner, fg_color=CARD)
        foot.pack(fill="x")
        self.foot_pc_lbl = ctk.CTkLabel(foot, text="", font=("Segoe UI", 9, "bold"),
                                    fg_color=CARD, text_color=ONLINE)
        self.foot_pc_lbl.pack(side="left")
        self.foot_conn_lbl = ctk.CTkLabel(foot, text="LAN Connection",
                                      font=("Segoe UI", 9), fg_color=CARD,
                                      text_color=SUBTLE)
        self.foot_conn_lbl.pack(side="right")

        # Server Settings gear (P0-2): always available - first run, after
        # a relock and mid-session, online or offline - so the address or
        # port can be corrected at any time instead of only once on the
        # very first start.
        self.config_btn = ctk.CTkButton(wrap, text="\u2699  Server Settings",
                                    command=self._ask_server_config,
                                    fg_color="#44507a", text_color="white", 
                                    font=("Segoe UI", 10, "bold"),
                                    cursor="hand2", )
        self.config_btn.pack(pady=(12, 0))
        if not cfg0.get("server_ip"):
            self._set_status_hint(
                "Server IP not configured — open Server Settings below.")

        self.msg_lbl = ctk.CTkLabel(wrap, text="", font=("Segoe UI", 10, "bold"),
                                text_color="#ff8585", fg_color=BG_DARK, wraplength=360,
                                justify="center")
        self.msg_lbl.pack(pady=(12, 0))

        ctk.CTkLabel(wrap,
                 text="This PC is locked. Only authorized accounts may log in.\n"
                      "All activity is recorded.",
                 font=("Segoe UI", 9), text_color="#8e9bc4", fg_color=BG_DARK,
                 justify="center").pack(pady=(14, 0))

        self._refresh_login_status()

    def _refresh_login_status(self):
        """Refresh the login card's server indicator + PC footer
        (called on every build and once per second by _tick).

        Spec item 10: the indicator reads `● Server Connected` while the
        link is up and `○ Server Offline — Local Mode` while the kiosk is
        running on its local cache (offline sign-in available)."""
        try:
            on = bool(self.net.connected)
            st = getattr(self, "srv_state_lbl", None)
            if st is not None and st.winfo_exists():
                st.configure(text="\u25cf Server Connected" if on else
                          "\u25cb Server Offline \u2014 Local Mode",
                          text_color=ONLINE if on else WARN)
            pc = getattr(self, "foot_pc_lbl", None)
            if pc is not None and pc.winfo_exists():
                name = self.net.pc_name or get_hostname() or "PC"
                pc.configure(text=f"{name} \u2022 {'ONLINE' if on else 'OFFLINE'}",
                          text_color=ONLINE if on else DANGER)
            addr = getattr(self, "srv_addr_lbl", None)
            if addr is not None and addr.winfo_exists():
                cfg = load_config()
                addr.configure(text=f"{cfg.get('server_ip', '\u2014')}:"
                                 f"{cfg.get('server_port', 8443)}")
        except Exception:
            pass

    def _set_status_hint(self, text):
        """Show the login card's status line, or hide it when there is
        nothing actionable to say.

        Spec 15 asks for ONE compact status indicator: the chip above
        already carries `● Server Connected` / `○ Server Offline — Local
        Mode` plus the address, so this line only adds what the chip
        cannot say - the first-run "not configured" nudge and the reason a
        link is down (spec 27: errors explain themselves).  Empty text
        hides the label entirely rather than leaving a blank strip, and
        unhiding always restores it directly after the chip so its
        position in the card never changes."""
        lbl = getattr(self, "status_lbl", None)
        if lbl is None or not lbl.winfo_exists():
            return
        text = str(text or "")
        if not text:
            try:
                lbl.configure(text="")
                lbl.pack_forget()
            except Exception:
                pass
            return
        lbl.configure(text=text)
        try:
            if not lbl.winfo_ismapped():
                strip = getattr(self, "_srv_strip", None)
                if strip is not None and strip.winfo_exists():
                    lbl.pack(after=strip, pady=(0, 12))
                else:
                    lbl.pack(pady=(0, 12))
        except Exception:
            pass

    def _toggle_pw_visibility(self):
        """Show / hide the password being typed (display only).

        The value lives in ``self.pw_var`` and is never touched here, so
        toggling cannot clear the field or change what authentication and
        the audit trail see.  Focus is handed straight back to the entry so
        the toggle cannot hijack navigation: after clicking it, Enter still
        submits the login and Tab still continues from the caret."""
        entry = getattr(self, "pw_entry", None)
        btn = getattr(self, "pw_show_btn", None)
        if entry is None or not entry.winfo_exists():
            return
        if str(entry.cget("show")):
            entry.configure(show="")
            if btn is not None:
                btn.configure(image=self._eye_hide)
                btn._eye_state = "hide"
        else:
            entry.configure(show="\u25cf")
            if btn is not None:
                btn.configure(image=self._eye_show)
                btn._eye_state = "show"
        try:
            entry.focus_set()
        except Exception:
            pass

    def _build_bar(self):
        """Session bar shown while the PC is unlocked.

        TASK-1 root cause: this used to be created with
        ``overrideredirect(True)`` + ``-topmost True`` - a frameless
        always-on-top strip pinned to the top of the screen that floated
        over File Explorer, browsers, Office and IDEs and could never be
        brought to the front or minimised like a real window.  It is now a
        NORMAL window: no override, no topmost, a title bar, a taskbar
        button and normal stacking, so other applications come in front of
        it exactly like any other desktop app.  It is never withdrawn or
        destroyed by minimising it, so nothing about the background duties
        (connection, heartbeat, session, commands, logging) is affected.
        """
        self.bar = ctk.CTkToplevel(self)
        self.bar.withdraw()
        # normal window: not topmost, not overrideredirect - it must not
        # cover or block the user's other applications.
        self.bar.overrideredirect(False)
        try:
            self.bar.attributes("-topmost", False)
        except Exception:
            pass
        self.bar.title("Laboratory Client")
        set_app_icon(self.bar)
        self.bar.configure(fg_color="#16203a")
        self.bar.resizable(False, False)
        # Closing the bar only hides it - it is never a logout and never
        # stops the Client process (P3: client_closed != client_logout).
        self.bar.protocol("WM_DELETE_WINDOW", self._minimize_bar)

        sw = self.winfo_screenwidth()
        bw = min(940, max(560, sw - 40))
        self.bar.geometry(f"{bw}x52+{(sw - bw) // 2}+0")

        row = ctk.CTkFrame(self.bar, fg_color="#16203a")
        row.pack(fill="both", expand=True, padx=10, pady=6)

        # "Current User: ..." - the identity validated BY THE SERVER
        self.bar_user = ctk.CTkLabel(row, text="", text_color="white", fg_color="#16203a",
                                 font=("Segoe UI", 10, "bold"), anchor="w")
        self.bar_user.pack(side="left")

        # "Role: ADMINISTRATOR / STAFF / STUDENT" (server-set role)
        self.bar_role = ctk.CTkLabel(row, text="", text_color="#7ee787", fg_color="#16203a",
                                 font=("Segoe UI", 9, "bold"), anchor="w")
        self.bar_role.pack(side="left", padx=(12, 0))

        self.bar_time = ctk.CTkLabel(row, text="00:00:00", text_color="#7ee787", fg_color="#16203a",
                                 font=("Consolas", 12, "bold"))
        self.bar_time.pack(side="left", padx=8)

        self.bar_metrics = ctk.CTkLabel(row, text="CPU 0%  RAM 0%", text_color="#9fb4ff",
                                    fg_color="#16203a", font=("Segoe UI", 9))
        self.bar_metrics.pack(side="left", padx=8)

        # Spec 14: show how this client reaches the LAN (Ethernet/Wi-Fi).
        self.bar_conn = ctk.CTkLabel(
            row, text=f"Connection: {get_connection_type()}",
            text_color="#9fb4ff", fg_color="#16203a", font=("Segoe UI", 9))
        self.bar_conn.pack(side="left", padx=8)

        # TASK-2: the ⚙ Server Settings gear was removed from this main
        # display.  Server IP/Port configuration still exists and is
        # reachable from the two secondary places instead:
        #   * the LOGIN screen gear (self.config_btn) - first run / kiosk;
        #   * "Maintenance" in the Client Dashboard header, which is only
        #     built for administrator / maintenance accounts.
        # Students deliberately never see a settings entry at all.

        self.dash_btn = ctk.CTkButton(row, text="My Dashboard", command=self.open_dashboard,
                                  fg_color=ACCENT, text_color="white", 
                                  font=("Segoe UI", 9, "bold"),
                                  cursor="hand2")
        self.dash_btn.pack(side="right", padx=(6, 0))
        # Spec 2: "Log Out" (was "Re-lock") - ends the session and returns
        # to the login screen. Closing the window is NOT a logout (that
        # keeps the session running, see _client_window_closed).
        self.logout_btn = ctk.CTkButton(row, text="Log Out", command=self.user_logout,
                                    fg_color="#44507a", text_color="white", 
                                    font=("Segoe UI", 9, "bold"),
                                    cursor="hand2")
        self.logout_btn.pack(side="right")

    # ---------------------------------------------------- bar (TASK-1) ----
    def _minimize_bar(self):
        """Title-bar X / minimise on the session bar.

        Because the bar is now a NORMAL window it has a real title bar,
        so the user can close it.  Closing it only hides it - it is
        NEVER a logout, it never stops the Client process and every
        background duty (server connection, heartbeat, session, website
        policy, lock/pause state, commands, local logging, offline sync,
        reconnection) keeps running.  Minimising (rather than destroying)
        keeps the bar restorable from the taskbar, so the Dashboard and
        the Log Out control can always be reached again.
        """
        try:
            self.bar.iconify()
        except Exception:
            pass

    # ---------------------------------------------------------- system tray
    def start_tray(self):
        """Show the Client in the notification area (P0-1).

        The tray icon is how a hidden Dashboard comes back, so it is
        started once the UI exists and always stopped on teardown -
        otherwise pystray's message-loop thread would keep the process
        alive after the kiosk closes.

        Menu callbacks arrive on the tray thread; tkinter is not
        thread-safe, so they are marshalled onto the Tk thread through
        the existing event queue (see _tray_call / the `ui_call` branch
        of _handle_event).  Returns False - never raises - when pystray
        or the logo asset is unavailable; the Client then behaves exactly
        as it did before (plain minimize, no tray).
        """
        if self._tray is not None:
            return True
        try:
            import pystray
        except Exception:
            return False
        try:
            img = _tray_logo(64)
            if img is None:
                return False
            # NOTE: pystray's picture argument is called `icon`, not
            # `image` - anything else is silently swallowed by **kwargs
            # and the icon ends up with no artwork.
            tray_icon = pystray.Icon(
                "lab_client", icon=img, title="Laboratory Client",
                menu=pystray.Menu(
                    pystray.MenuItem("Open Dashboard",
                                     self._tray_open_dashboard,
                                     default=True)))
            # The icon only marks itself "running" once its hidden window
            # exists; stopping before that would be a no-op and leave the
            # message-loop thread alive forever.  A custom setup callback
            # replaces pystray's default one, so it must also show the
            # icon - it fires strictly after that point.
            ready = threading.Event()

            def _setup(_i):
                try:
                    _i.visible = True
                finally:
                    ready.set()

            tray_icon.run_detached(setup=_setup)
            self._tray = tray_icon
            self._tray_ready = ready
            self._tray_ok = True
            return True
        except Exception:
            self._tray = None
            self._tray_ok = False
            return False

    def stop_tray(self):
        """Remove the notification-area icon.  Idempotent, never raises."""
        ready, icon = self._tray_ready, self._tray
        self._tray = None
        self._tray_ready = None
        self._tray_ok = False
        if icon is None:
            return
        if ready is not None:
            try:
                ready.wait(5.0)      # normally returns in ~1 ms
            except Exception:
                pass
        try:
            icon.stop()
        except Exception:
            pass

    def _tray_call(self, fn):
        """Run `fn` on the Tk thread (safe from a tray callback)."""
        try:
            self.events.put({"kind": "ui_call", "fn": fn})
        except Exception:
            pass

    def _tray_open_dashboard(self, icon=None, item=None):
        self._tray_call(self._restore_from_tray)

    def _restore_from_tray(self):
        """Tray action: bring back the Dashboard (or the lock screen)."""
        if self.state == self.STATE_UNLOCKED and self.user:
            self.open_dashboard()
        else:
            # no session to show - just surface the kiosk again
            try:
                self.deiconify()
                self.lift()
            except Exception:
                pass

    # ------------------------------------------------------- config dialog
    def _announce_unconfigured(self):
        """First-run state: locked, visible, waiting for the server IP.
        (_build_kiosk already shows the button; this is just the nudge.)"""
        self._set_status_hint(
            "Server IP not configured — open Server Settings below.")

    def _ask_server_config(self):
        """Server Settings dialog (P0-2).

        Always available - first run, after a relock, mid-session, online
        or offline.  It only ever writes `lab_config.json` and re-points
        the TCP link; the local client database is never touched, and
        cancelling simply closes the window (the kiosk is never hidden,
        looped or trapped).
        """
        win = getattr(self, "_settings_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass
        cfg = load_config()

        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.title("Server Settings")
        win.configure(fg_color="#1f2a44")
        win.resizable(False, False)
        set_app_icon(win)
        # The kiosk is fullscreen and always-on-top: without this the
        # dialog would open *behind* the lock screen.
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", self._cfg_close)

        card = PaddedFrame(win, fg_color="white", padx=26, pady=22)
        card.pack(fill="both", expand=True)

        ctk.CTkLabel(card, text="Server Settings",
                 font=("Segoe UI", 15, "bold"), fg_color="white",
                 text_color="#1f2a44").pack(anchor="w")
        ctk.CTkLabel(card, text="Address of the Server (Admin PC) on this LAN.",
                 font=("Segoe UI", 9), fg_color="white", text_color="#6b7280").pack(
            anchor="w", pady=(2, 0))

        ctk.CTkLabel(card, text="Server IP address or hostname",
                 font=("Segoe UI", 9, "bold"), fg_color="white",
                 text_color="#374151").pack(anchor="w", pady=(16, 4))
        self._cfg_ip_var = tk.StringVar(
            value=str(cfg.get("server_ip", "") or ""))
        ctk.CTkEntry(card, textvariable=self._cfg_ip_var,
                 font=("Segoe UI", 11), border_width=1).pack(
            fill="x", ipady=6)

        ctk.CTkLabel(card, text="Port",
                 font=("Segoe UI", 9, "bold"), fg_color="white",
                 text_color="#374151").pack(anchor="w", pady=(14, 4))
        self._cfg_port_var = tk.StringVar(
            value=str(cfg.get("server_port", 8443)))
        ctk.CTkEntry(card, textvariable=self._cfg_port_var,
                 font=("Segoe UI", 11), border_width=1).pack(
            fill="x", ipady=6)

        self._cfg_status_lbl = ctk.CTkLabel(card, text="", font=("Segoe UI", 9),
                                        fg_color="white", text_color="#2fa84f",
                                        anchor="w", justify="left",
                                        wraplength=330)
        self._cfg_status_lbl.pack(fill="x", pady=(10, 0))

        btns = ctk.CTkFrame(card, fg_color="white")
        btns.pack(fill="x", pady=(16, 0))
        self._cfg_test_btn = ctk.CTkButton(
            btns, text="Test Connection", command=self._cfg_test,
            fg_color="#44507a", text_color="white", 
            font=("Segoe UI", 9, "bold"), cursor="hand2", )
        self._cfg_test_btn.pack(side="left")

        ctk.CTkButton(btns, text="Cancel", command=self._cfg_close,
                  fg_color="#e5e9f2", text_color="#374151", 
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  ).pack(side="right")
        ctk.CTkButton(btns, text="Save", command=self._cfg_save,
                  fg_color=ACCENT, text_color="white", 
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  ).pack(side="right", padx=(0, 8))

        # P1-6: [Uninstall Client] - admin/maintenance only, and only once
        # somebody is actually signed in (the lock screen has no user, so
        # students never even see this section).
        self._cfg_uninstall_btn = None
        if str((self.user or {}).get("role", "") or "") in MAINTENANCE_ROLES:
            ctk.CTkFrame(card, fg_color="#e5e9f2", height=1).pack(fill="x",
                                                         pady=(16, 10))
            ctk.CTkLabel(card, text="MAINTENANCE",
                     font=("Segoe UI", 9, "bold"), fg_color="white",
                     text_color="#6b7280").pack(anchor="w")
            ctk.CTkLabel(card,
                     text="Exports the local log, then removes this PC "
                          "from Windows auto-start. The application "
                          "files and database are NOT deleted.",
                     font=("Segoe UI", 8), fg_color="white", text_color="#9ca3af",
                     justify="left", wraplength=330).pack(anchor="w",
                                                          pady=(2, 8))
            self._cfg_uninstall_btn = ctk.CTkButton(
                card, text="[Uninstall Client]",
                command=self._uninstall_client,
                fg_color="#7a1f1f", text_color="white", 
                font=("Segoe UI", 9, "bold"), cursor="hand2",
                )
            self._cfg_uninstall_btn.pack(anchor="w")

        # Centre on the screen using the size the content actually asked
        # for - the kiosk may be withdrawn here, so centring against it
        # would place the dialog somewhere the user cannot see it.
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _cfg_close(self):
        """Close the settings dialog.  Idempotent, never raises."""
        win = getattr(self, "_settings_win", None)
        self._settings_win = None
        self._cfg_status_lbl = None
        self._cfg_test_btn = None
        self._cfg_ip_var = None
        self._cfg_port_var = None
        self._cfg_uninstall_btn = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _uninstall_client(self):
        """P1-6 `[Uninstall Client]` - admin/maintenance + confirmation.

        The button is only ever built for an admin or maintenance account
        (and only while somebody is signed in), and the action itself is
        re-checked here so a stale widget or a direct call can never get
        past it.  The confirmation is explicit because this is the one
        action that removes the kiosk from Windows auto-start; nothing is
        deleted or uninstalled without the operator saying yes.
        """
        if str((self.user or {}).get("role", "") or "") not in MAINTENANCE_ROLES:
            return
        win = getattr(self, "_settings_win", None)
        parent = win if (win is not None and win.winfo_exists()) else self
        try:
            confirmed = messagebox.askyesno(
                "Uninstall Client",
                "Remove this PC from Windows auto-start?\n\n"
                "  • the Windows Startup entry is removed\n"
                "  • the watchdog tasks are removed\n"
                "  • the local log is exported first\n\n"
                "The application files and the database are NOT deleted.\n"
                "Afterwards this PC will no longer start the kiosk on "
                "its own.",
                parent=parent, icon="warning", default="no")
        except Exception:
            confirmed = False        # a refused/failed dialog = cancelled
        if not confirmed:
            self._cfg_status("Uninstall cancelled.")
            return
        lines = _uninstall_client_work()
        self._cfg_status("\n".join(lines), error=False)
        local_store.log_event(
            "INFO", "maintenance", "Uninstall result",
            " | ".join(lines),
            (self.user or {}).get("student_id", ""))

    def _cfg_status(self, text, error=False):
        lbl = getattr(self, "_cfg_status_lbl", None)
        if lbl is None:
            return
        try:
            if not lbl.winfo_exists():
                return
            lbl.configure(text=text, text_color="#e5484d" if error else "#2fa84f")
        except Exception:
            pass

    @staticmethod
    def _probe_server(host, port, timeout=4.0):
        """Reachability check behind the Test Connection button.

        Opens its OWN TCP + TLS socket to (host, port) and closes it
        again, so the live link (`self.net`) is never disturbed and no
        protocol message - and therefore no server-side state - is ever
        sent.  Returns (ok, message).
        """
        try:
            raw = socket.create_connection((host, int(port)), timeout=timeout)
        except Exception as e:
            return False, f"✗ Cannot reach {host}:{port} — {e}"
        try:
            try:
                # handshake only: create_ssl_context(is_server=False)
                # accepts the lab's self-signed certificate
                ctx = create_ssl_context(is_server=False)
                tls = ctx.wrap_socket(raw, server_hostname=host)
            except Exception as e:
                return False, (f"✗ {host}:{port} answered, but the TLS "
                               f"handshake failed — {e}")
            try:
                return True, f"✓ Connected to {host}:{port} (TLS OK)"
            finally:
                try:
                    tls.close()
                except Exception:
                    pass
        finally:
            try:
                raw.close()
            except Exception:
                pass

    def _cfg_test(self):
        """Validate the typed address on a background thread (never blocks
        the UI) and report the result in the dialog."""
        host = (self._cfg_ip_var.get() if self._cfg_ip_var else "").strip()
        try:
            port = int((self._cfg_port_var.get()
                        if self._cfg_port_var else "").strip())
        except (ValueError, TypeError):
            port = None
        if not host or port is None or not (1 <= port <= 65535):
            self._cfg_status("Enter a server address and a port "
                             "between 1 and 65535.", error=True)
            return
        self._cfg_status("Testing connection…")
        btn = getattr(self, "_cfg_test_btn", None)
        try:
            if btn is not None and btn.winfo_exists():
                btn.configure(state="disabled")
        except Exception:
            pass
        threading.Thread(target=self._cfg_test_worker, args=(host, port),
                         daemon=True, name="cfg-probe").start()

    def _cfg_test_worker(self, host, port):
        ok, msg = self._probe_server(host, port)
        # marshal back onto the Tk thread through the event queue (the
        # probe runs on a worker thread - tkinter is not thread-safe)
        try:
            self.events.put({"kind": "ui_call",
                             "fn": lambda: self._cfg_test_done(ok, msg)})
        except Exception:
            pass

    def _cfg_test_done(self, ok, msg):
        btn = getattr(self, "_cfg_test_btn", None)
        try:
            if btn is not None and btn.winfo_exists():
                btn.configure(state="normal")
        except Exception:
            pass
        self._cfg_status(msg, error=not ok)

    def _cfg_save(self):
        """Persist Server IP/port to `lab_config.json` and re-point the
        TCP link in place.  The local client database is untouched."""
        host = (self._cfg_ip_var.get() if self._cfg_ip_var else "").strip()
        port_txt = (self._cfg_port_var.get()
                    if self._cfg_port_var else "").strip()
        if not host:
            self._cfg_status("A server address is required.", error=True)
            return
        try:
            port = int(port_txt)
        except (ValueError, TypeError):
            self._cfg_status("The port must be a number.", error=True)
            return
        if not (1 <= port <= 65535):
            self._cfg_status("The port must be between 1 and 65535.",
                             error=True)
            return

        cfg = load_config()
        changed = (host != str(cfg.get("server_ip", "") or "")
                   or port != int(cfg.get("server_port", 8443) or 8443))
        cfg.update({"server_ip": host, "server_port": port})
        save_config(cfg)                     # lab_config.json only
        try:
            local_store.log_event("INFO", "config", "Server settings saved",
                                  f"server={host}:{port} "
                                  f"changed={'yes' if changed else 'no'}")
        except Exception:
            pass

        self._cfg_close()
        # discover=False: the operator typed and tested THIS address, so
        # it must be used verbatim instead of being overwritten by the
        # UDP auto-discovery answer.
        self.net.start(host, port, discover=False)
        if changed and self.net.connected:
            # Switch links immediately (the connect loop re-establishes to
            # the new address on its next pass).  Deliberately NOT
            # _drop_connection(): no `net_disconnected` event is raised,
            # so saving settings can never log a user out on its own.
            self.net._close_socket()
        self._refresh_login_status()
        # the address exists now, so the first-run "not configured" hint
        # is stale; clear it (connection events are mirrors and never
        # write this line, so this is the only thing that can retire it).
        self._set_status_hint("")

    # ------------------------------------------------------------ login
    def attempt_login(self):
        # the first-login change card owns the kiosk until it completes
        if getattr(self, "_pw_change_active", False):
            return
        uid = self.id_var.get().strip()
        pw = self.pw_var.get()
        if not uid or not pw:
            self._login_error("Please enter both User ID and password.")
            return
        if self.state == self.STATE_ADMIN_LOCK:
            # Admin Lock cannot be bypassed by typing credentials: only an
            # explicit Admin Unlock (force login) releases this PC.
            self._login_error("This PC is locked by the administrator. "
                              "Waiting for Admin Unlock…")
            return
        if not self.net.connected:
            # Spec item 10: the server is down - fall back to offline
            # sign-in against the last synced auth roster (item 12).
            self._offline_login(uid, pw)
            return
        self.login_btn.configure(state="disabled", text="Verifying…")
        self.msg_lbl.configure(text="", text_color="#ff8585")
        info = {"hostname": get_hostname(), "ip": get_local_ip()}
        self.net.send(build_auth_request(uid, pw, "student", info))
        # Never leave the button stuck on "Verifying…" if the server
        # does not answer (Phase A/#5 fix).
        try:
            if getattr(self, "_after_verify", None):
                self.after_cancel(self._after_verify)
            self._after_verify = self.after(15000, self._verify_timeout)
        except Exception:
            self._after_verify = None

    def _offline_login(self, uid, pw):
        """Offline sign-in (spec items 10/12).

        Verified entirely on this PC against the roster the server last
        pushed over TLS - salted hashes only, roster-synced accounts only,
        revoked the moment the roster refreshes, and expired after
        max_offline_days without a sync.  A granted session runs in Local
        Mode; everything it does is written to the durable local log and
        flushed to the server on reconnect (items 9/11/17)."""
        res = local_store.verify_offline_login(uid, pw)
        if not res.get("ok"):
            err = res.get("error") or "Offline sign-in unavailable."
            self._login_error(err)
            local_store.log_event(
                "WARN", "auth", "Offline sign-in denied",
                f"account={uid!r}: {err}")
            return
        user = res["user"]
        self.offline_session = True
        self.login_btn.configure(state="disabled", text="Verifying…")
        self.msg_lbl.configure(text="", text_color="#ff8585")
        local_store.log_event(
            "INFO", "auth",
            "Offline sign-in granted (Local Mode)",
            f"{user.get('student_id', '')} ({user.get('role', '')})")
        self._on_auth_ok(user)

    def _verify_timeout(self):
        self._after_verify = None
        btn_text = str(self.login_btn.cget("text"))
        if btn_text == "Verifying…":
            self.login_btn.configure(state="normal", text="LOGIN")
            self.msg_lbl.configure(
                text="Server did not respond. Please try again.", text_color="#ff8585")

    def _cancel_verify_timeout(self):
        aid = getattr(self, "_after_verify", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            self._after_verify = None

    def _login_error(self, text):
        self._cancel_verify_timeout()
        lbl = getattr(self, "msg_lbl", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=text, text_color="#ff8585")
        btn = getattr(self, "login_btn", None)
        if btn is not None and btn.winfo_exists():
            btn.configure(state="normal", text="LOGIN")

    # --------------------------------------------- first-login password
    def _show_password_change(self, user_data):
        """FIRST LOGIN PASSWORD (fresh client accounts): shown when the
        SERVER says the authenticated password is still the default.
        No session exists yet and the kiosk stays locked - normal usage
        is impossible until the server confirms a new password."""
        self._pending_pw_user = user_data or {}
        self._pw_change_active = True
        self.state = self.STATE_LOGIN
        for w in self.kiosk_host.winfo_children():
            w.destroy()

        wrap = ctk.CTkFrame(self.kiosk_host, fg_color=BG_DARK)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        card = PaddedFrame(wrap, fg_color=CARD, border_color=BORDER,
                        border_width=1, padx=2, pady=2)
        card.pack(fill="x")
        inner = ctk.CTkFrame(card, fg_color=CARD)
        inner.pack(padx=42, pady=(30, 24))

        ctk.CTkLabel(inner, text="Change Password", font=("Segoe UI", 20, "bold"),
                 fg_color=CARD, text_color=TEXT).pack()
        u = self._pending_pw_user
        ctk.CTkLabel(inner,
                 text=f"{u.get('full_name', '')} ({u.get('student_id', '')})",
                 font=("Segoe UI", 11), fg_color=CARD,
                 text_color=SUBTLE).pack(pady=(2, 0))
        ctk.CTkLabel(inner,
                 text="This account still uses the default password.\n"
                      "Set a new password to continue.",
                 font=("Segoe UI", 10), fg_color=CARD, text_color=SUBTLE,
                 justify="center").pack(pady=(6, 12))

        # server status strip (same widget names the 1 s tick keeps fresh)
        srv = PaddedFrame(inner, fg_color=CARD_ALT, border_color=BORDER,
                       border_width=1, padx=10, pady=4)
        srv.pack(pady=(0, 12))
        self._srv_strip = srv        # anchor for _set_status_hint
        self.srv_state_lbl = ctk.CTkLabel(srv, text="● Server Online",
                                      font=("Segoe UI", 9, "bold"),
                                      text_color=ONLINE, fg_color=CARD_ALT)
        self.srv_state_lbl.pack(side="left")
        cfg0 = load_config()
        self.srv_addr_lbl = ctk.CTkLabel(
            srv,
            text=f"{cfg0.get('server_ip', '\u2014')}:"
                 f"{cfg0.get('server_port', 8443)}",
            font=("Consolas", 9), text_color=SUBTLE, fg_color=CARD_ALT)
        self.srv_addr_lbl.pack(side="left", padx=(8, 0))
        # hint line for this card too - empty and unpaked until a real
        # message arrives (the chip above is the status indicator).
        self.status_lbl = ctk.CTkLabel(inner, text="", font=("Segoe UI", 9),
                                   text_color=WARN, fg_color=CARD, wraplength=300,
                                   justify="center")

        def _pw_row(label_text):
            ctk.CTkLabel(inner, text=label_text, font=("Segoe UI", 9, "bold"),
                     fg_color=CARD, text_color=TEXT, anchor="w").pack(fill="x")
            row = ctk.CTkFrame(inner, fg_color=CARD)
            row.pack(fill="x", pady=(3, 12))
            var = tk.StringVar()
            ent = ctk.CTkEntry(row, textvariable=var, font=("Segoe UI", 12),
                            show="\u25cf")
            ent.pack(side="left", fill="x", expand=True)
            with image_master(self):      # cached PhotoImage -> this window
                btn = ctk.CTkButton(row, text="", width=40,
                                image=self._eye_show,
                                command=lambda: self._toggle_pw_row(ent, btn),
                                fg_color=CARD, text_color="white",
                                corner_radius=6, cursor="hand2",
                                hover_color=CARD_ALT)
            btn._eye_state = "show"
            btn.pack(side="right", padx=(6, 0))
            return var, ent

        self.pwc_new_var, self.pwc_new_entry = _pw_row("New Password")
        self.pwc_conf_var, self.pwc_conf_entry = _pw_row("Confirm Password")

        self.pwc_err_lbl = ctk.CTkLabel(inner, text="", font=("Segoe UI", 9, "bold"),
                                    text_color=DANGER, fg_color=CARD, wraplength=320,
                                    justify="center")
        self.pwc_err_lbl.pack(fill="x", pady=(0, 8))

        self.pwc_btn = ctk.CTkButton(inner, text="CHANGE PASSWORD",
                                 command=self._submit_password_change,
                                 fg_color=ACCENT, text_color="white",
                                 font=("Segoe UI", 12, "bold"), 
                                 hover_color="#1e4fbf",
                                 cursor="hand2")
        self.pwc_btn.pack(fill="x")
        self.pwc_new_entry.bind("<Return>",
                                lambda e: self._submit_password_change())
        self.pwc_conf_entry.bind("<Return>",
                                 lambda e: self._submit_password_change())

        ctk.CTkButton(inner, text="\u2190 Back to login",
                  command=self._cancel_password_change, fg_color=CARD_ALT,
                  text_color=SUBTLE, font=("Segoe UI", 9),
                  cursor="hand2", hover_color=BORDER).pack(pady=(10, 0))

        # footer: PC name + connection state | LAN Connection (as login)
        div = ctk.CTkFrame(inner, fg_color=BORDER, height=1)
        div.pack(fill="x", pady=(14, 8))
        div.pack_propagate(False)
        foot = ctk.CTkFrame(inner, fg_color=CARD)
        foot.pack(fill="x")
        self.foot_pc_lbl = ctk.CTkLabel(foot, text="",
                                    font=("Segoe UI", 9, "bold"),
                                    fg_color=CARD, text_color=ONLINE)
        self.foot_pc_lbl.pack(side="left")
        self.foot_conn_lbl = ctk.CTkLabel(foot, text="LAN Connection",
                                      font=("Segoe UI", 9), fg_color=CARD,
                                      text_color=SUBTLE)
        self.foot_conn_lbl.pack(side="right")

        self.msg_lbl = ctk.CTkLabel(wrap, text="", font=("Segoe UI", 10, "bold"),
                                text_color="#ff8585", fg_color=BG_DARK, wraplength=360,
                                justify="center")
        self.msg_lbl.pack(pady=(12, 0))

        self.config_btn = None      # no first-run settings button here
        self._refresh_login_status()
        try:
            self.deiconify()
            self.lift()
            self.pwc_new_entry.focus_set()
            self.grab_set()
        except Exception:
            pass

    def _toggle_pw_row(self, entry, btn):
        """Show/hide one of the change-card password fields (display)."""
        if str(entry.cget("show")):
            entry.configure(show="")
            btn.configure(image=self._eye_hide)
            btn._eye_state = "hide"
        else:
            entry.configure(show="\u25cf")
            btn.configure(image=self._eye_show)
            btn._eye_state = "show"

    def _pwc_error(self, text):
        lbl = getattr(self, "pwc_err_lbl", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=text)

    def _submit_password_change(self):
        if not getattr(self, "_pw_change_active", False):
            return
        new = self.pwc_new_var.get()
        conf = self.pwc_conf_var.get()
        if not new or not conf:
            self._pwc_error("Please fill in both password fields.")
            return
        if len(new) < 6:
            self._pwc_error("Password must be at least 6 characters.")
            return
        if new != conf:
            self._pwc_error("Passwords do not match.")
            return
        if new == DEFAULT_CLIENT_PASSWORD:
            self._pwc_error("The new password cannot be the default password.")
            return
        if not self.net.connected:
            self._pwc_error("Not connected to the server.")
            return
        self.pwc_err_lbl.configure(text="")
        self.pwc_btn.configure(state="disabled", text="Saving…")
        self.net.send(build_password_change(new))

    def _handle_password_change_response(self, p):
        self._cancel_verify_timeout()
        if not p.get("success"):
            btn = getattr(self, "pwc_btn", None)
            if btn is not None and btn.winfo_exists():
                btn.configure(state="normal", text="CHANGE PASSWORD")
            self._pwc_error(p.get("error", "Could not change the password."))
            return
        user_data = self._pending_pw_user
        if user_data is None:
            return                     # Back was pressed: ignore late reply
        user_data["must_change_password"] = False
        self._pending_pw_user = None
        local_store.log_event(
            "INFO", "auth", "First-login password changed",
            f"account={user_data.get('student_id', '')}")
        self._on_auth_ok(user_data)

    def _cancel_password_change(self):
        """Back to the normal login card - still locked, nothing is
        unlocked: the account keeps its forced change on next login."""
        self._pending_pw_user = None
        self._show_lock("")

    # ------------------------------------------------------- session mgmt
    def _on_auth_ok(self, user_data):
        self._cancel_verify_timeout()
        # If a session is still open (e.g. after an admin lock), reuse it when
        # the same user returns; otherwise close the stale one first.
        if self.session_id:
            if self.user and self.user.get("student_id") != user_data.get("student_id"):
                dur = int(time.time() - (self.session_start or time.time()))
                self.net.send(build_session_end(self.session_id, time.time(), dur))
                self.session_id = None
        resumed = self.session_id is not None
        old_user = self.user
        self.user = user_data
        if not self.session_id:
            self.session_id = uuid.uuid4().hex[:12]
            self.session_start = time.time()
            self.net.send(build_session_start(
                pc_name=self.net.pc_name,
                student_id=user_data.get("student_id", ""),
                full_name=user_data.get("full_name", ""),
                session_id=self.session_id,
            ))
        client_api.set_link(self.net)
        self.net.send(build_activity_log(
            admin_user=user_data.get("student_id", ""),
            action="client_login" if not resumed else "client_relogin",
            target=self.net.pc_name,
            details=f"{user_data.get('full_name', '')} logged in ({user_data.get('role', '')})",
        ))
        self._unlock_ui()

    def _unlock_ui(self):
        self.state = self.STATE_UNLOCKED
        self.lock_reason = ""
        self.hotkeys.stop()
        self.msg_lbl.configure(text="")
        self.withdraw()                       # hide kiosk -> Windows desktop visible
        self.bar.deiconify()
        self.bar.lift()
        u = self.user or {}
        role = str(u.get("role", "") or "")
        self.bar_user.configure(
            text=f"Current User: {u.get('full_name', '')} "
                 f"({u.get('student_id', '')})" if self.user else "")
        self.bar_role.configure(
            text=f"Role: "
                 f"{ROLE_LABELS.get(role, role.upper())}" if role else "")
        # A client-side ADMIN login stays on the kiosk UI: the identity is
        # validated by the server, but it must never open the
        # Server/Admin Dashboard (that lives on the server machine only).
        dbtn = getattr(self, "dash_btn", None)
        if dbtn is not None:
            if role == "admin":
                dbtn.pack_forget()
            elif not dbtn.winfo_ismapped():
                dbtn.pack(side="right", padx=(6, 0))
        self._pulse_metrics()

    def user_logout(self, reason=""):
        """End the session locally and return to the login screen."""
        self._cancel_verify_timeout()
        self._close_pause_win()          # never leave a pause overlay behind
        local_store.log_event(
            "INFO", "session", "Session ended", reason or "User logged out",
            self.user.get("student_id", "") if self.user else "")
        if self.session_id:
            dur = int(time.time() - (self.session_start or time.time()))
            self.net.send(build_session_end(self.session_id, time.time(), dur))
            self.net.send(build_activity_log(
                admin_user=self.user.get("student_id", "?") if self.user else "?",
                action="client_logout", target=self.net.pc_name,
                details=reason or "User logged out"))
        self.session_id = None
        self.session_start = None
        self.user = None
        self.offline_session = False
        self._close_dashboard()
        self.bar.withdraw()
        self.bar_user.configure(text="")
        self.bar_role.configure(text="")
        self._show_lock(f"Logged out{': ' + reason if reason else ''}", error=False)

    def _show_lock(self, reason="", error=True):
        self.state = self.STATE_LOGIN
        self._build_kiosk()
        self.deiconify()
        self.lift()
        self.focus_force()
        self.hotkeys.start()
        if reason:
            self.msg_lbl.configure(text=reason, text_color="#ff8585" if error else "#7ee787")
        self.id_var.set("")
        self.pw_var.set("")
        try:
            self.grab_set()
        except Exception:
            pass

    def _emergency_force_unlock(self, event=None):
        """P1 emergency hotkey: Ctrl+Shift+Alt+M force-unlocks the kiosk.

        Recovery escape hatch for a locked Client PC whose Server is
        unreachable - otherwise the kiosk would be unusable.  Reuses the
        normal force-unlock UI path (_unlock_ui): a still-open session is
        restored with its session bar, without one the desktop simply
        becomes usable.  The unlock is audited as `hotkey_force_unlock`
        whenever the link is up (offline it stays a local-only unlock;
        the server remains authoritative for any future session).  It is
        a no-op while already unlocked, while paused (only the admin's
        Resume releases a pause) and while the forced first-login
        password card is up (the server gate still applies there).
        """
        if self.state not in (self.STATE_LOGIN, self.STATE_ADMIN_LOCK):
            return
        if getattr(self, "_pw_change_active", False):
            return
        self._cancel_verify_timeout()
        self._unlock_ui()
        self.net.send(build_activity_log(
            admin_user=(self.user or {}).get("student_id") or "-",
            action="hotkey_force_unlock",
            target=self.net.pc_name,
            details="Ctrl+Shift+Alt+M pressed at the kiosk "
                    "(local emergency force-unlock)"))
        return "break"

    def _panic_stop(self, event=None):
        """P1-5 emergency hotkey: Ctrl+Shift+Alt+K stops everything now.

        The counterpart to the Ctrl+Shift+Alt+M force-unlock: where M
        rescues a locked kiosk, K immediately ends the running session
        and locks the PC.

        Deliberately local-first - it never waits for the Server, so it
        keeps working with the Server offline.  The audit row goes into
        the durable local store (flushed to the Server later) and the
        activity log is only sent when the link is up, because `net.send`
        is fire-and-forget and returns False while disconnected.

        A no-op when there is nothing to stop: while a pause is in force
        only an admin's Resume releases it (Phase B/#6), and an already
        locked kiosk (login or admin lock) has no session to end.  That
        also keeps M and K on opposite sides of the same state machine,
        so neither can undo the other.
        """
        if self.state != self.STATE_UNLOCKED:
            return
        u = self.user or {}
        local_store.log_event(
            "WARN", "safety", "Panic stop activated",
            "Ctrl+Shift+Alt+K pressed at the kiosk - session stopped and "
            "PC locked immediately", str(u.get("student_id", "")))
        if self.net.connected:
            self.net.send(build_activity_log(
                admin_user=str(u.get("student_id", "") or "-"),
                action="panic_stop", target=self.net.pc_name,
                details="Ctrl+Shift+Alt+K - session stopped and PC locked"))
        self.stop_observation()      # never keep streaming after a panic
        self._remote_stop_now("panic stop")   # never stay under remote control
        self.user_logout("Panic stop (Ctrl+Shift+Alt+K)")
        return "break"

    def _panic_from_hook(self):
        """P1-5: called from the keyboard-hook thread - marshal onto Tk.

        Never touches a widget directly; `_handle_event`'s guarded
        `ui_call` branch runs it on the Tk thread.
        """
        self.events.put({"kind": "ui_call",
                         "fn": lambda: self._panic_stop()})

    # ------------------------------------------------------ pause / lock
    def _show_pause(self, message, seconds=0):
        """Pause stays active until the admin explicitly sends Resume -
        there is NO automatic expiry (Phase B/#6)."""
        self.pause_message = message or "Paused by administrator"
        if self.state == self.STATE_PAUSED:
            # Update the message on a repeated pause instead of leaking
            # a second overlay window.
            if hasattr(self, "_pause_msg_lbl"):
                self._pause_msg_lbl.configure(text=self.pause_message)
            return
        self.state = self.STATE_PAUSED
        self.pause_until = 0
        self.bar.withdraw()
        self.withdraw()

        self.pause_win = ctk.CTkToplevel(self)
        self.pause_win.overrideredirect(True)
        self.pause_win.attributes("-topmost", True)
        self.pause_win.configure(fg_color="#2b1a06")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.pause_win.geometry(f"{sw}x{sh}+0+0")
        ctk.CTkLabel(self.pause_win, text="⏸", font=("Segoe UI", 70),
                 text_color=WARN, fg_color="#2b1a06").pack(pady=(sh // 5, 10))
        ctk.CTkLabel(self.pause_win, text="SESSION PAUSED", font=("Segoe UI", 34, "bold"),
                 text_color="white", fg_color="#2b1a06").pack()
        self._pause_msg_lbl = ctk.CTkLabel(
            self.pause_win, text=self.pause_message, font=("Segoe UI", 16),
            text_color=WARN, fg_color="#2b1a06", wraplength=700, justify="center")
        self._pause_msg_lbl.pack(pady=14)
        self.pause_count_lbl = ctk.CTkLabel(self.pause_win, text="",
                                        font=("Consolas", 18, "bold"),
                                        text_color="#ffd79a", fg_color="#2b1a06")
        self.pause_count_lbl.pack(pady=8)
        ctk.CTkLabel(self.pause_win,
                 text="The administrator must press Resume to continue.",
                 font=("Segoe UI", 11), text_color="#c9a86a", fg_color="#2b1a06").pack(pady=6)
        self.pause_win.protocol("WM_DELETE_WINDOW", self._ignore_close)
        self.pause_win.grab_set()
        self.hotkeys.start()

    def _close_pause_win(self):
        """Destroy the pause overlay if it exists (no UI state change)."""
        win = getattr(self, "pause_win", None)
        if win:
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass
            self.pause_win = None
        self.pause_count_lbl = None
        self._pause_msg_lbl = None

    def _hide_pause(self):
        self._close_pause_win()
        if self.user:
            self._unlock_ui()
        else:
            self._show_lock("Resumed — please log in.", error=False)

    def _send_cmd_response(self, command_id, success, result=None, error=None):
        if command_id:
            self.net.send(build_command_response(command_id, success, result, error))

    # -------------------------------------------------- remote commands
    def handle_command(self, msg: Message):
        """Execute a server command and ALWAYS acknowledge it (Phase F/#7).

        Power actions (restart/shutdown) are the only commands that arm a
        one-shot token consumed by _os_power - nothing else in the app can
        trigger them (Phase B/#13).
        """
        t, p = msg.type, msg.payload
        cid = p.get("command_id")
        if cid:
            if cid in self._seen_cmd_ids:
                # Duplicate delivery: already processed AND already
                # acknowledged - ignore it completely (no second execution,
                # no re-ack, so the server's pending-ack table is not
                # confused by a repeated command_id).
                return
            self._seen_cmd_ids.add(cid)
            if len(self._seen_cmd_ids) > 256:
                # command ids are fresh uuids, so a reset can never re-run a
                # live command - this just bounds the memory.
                self._seen_cmd_ids.clear()
                self._seen_cmd_ids.add(cid)

        # P1-8: every remote-control action is recorded on THIS PC as well,
        # in the durable local store, so the trail still exists if the link
        # drops before the acknowledgement can be delivered.  It only ever
        # states what the client knows for certain - that the command
        # arrived, and whether the link was up to carry the reply back.
        _detail = (f"{t} command_id={cid or '-'} "
                   f"link={'up' if self.net.connected else 'down'}")
        if not local_store.log_event("INFO", "remote",
                                     "Remote command received", _detail):
            # Rare (the local store was briefly unavailable): try once more
            # instead of silently losing the only record that exists on
            # this PC - nothing else can reconstruct it after a drop.
            local_store.log_event("INFO", "remote",
                                  "Remote command received", _detail)

        if t == MessageType.CMD_LOCK.value:
            # keep the session; show the lock screen and block any login
            # until the admin explicitly unlocks (force login).
            self._cancel_verify_timeout()
            self._close_pause_win()
            self.bar.withdraw()
            self._close_dashboard()
            self._show_lock(p.get("params", {}).get("message",
                                                    "Locked by the administrator."),
                            error=True)
            self.state = self.STATE_ADMIN_LOCK
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_UNLOCK.value:
            # Admin Force Login: restore the still-open session directly -
            # no customer credentials are requested. Without a session the
            # PC simply returns to a normal (login allowed) lock screen.
            had_user = bool(self.user)
            self._cancel_verify_timeout()
            self._close_pause_win()
            if had_user:
                self._unlock_ui()
                self._send_cmd_response(cid, True, result="force_login")
            else:
                self._show_lock("Unlocked by administrator — please log in.",
                                error=False)
                self._send_cmd_response(cid, True, result="login_allowed")

        elif t == MessageType.CMD_LOGOUT.value:
            self.user_logout("Remote logout by administrator")
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_PAUSE.value:
            _pm = p.get("params", {}).get("message")
            local_store.log_event("INFO", "session", "Paused by server",
                                  str(_pm or ""))
            self._show_pause(_pm)
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_RESUME.value:
            local_store.log_event("INFO", "session", "Resumed by server")
            self._hide_pause()
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_RESTART.value:
            self._power_token = "restart"        # one-shot, admin-authenticated
            self._send_cmd_response(cid, True, result="restarting")
            self.after(1500, lambda: self._os_power("restart"))

        elif t == MessageType.CMD_SHUTDOWN.value:
            self._power_token = "shutdown"       # one-shot, admin-authenticated
            self._send_cmd_response(cid, True, result="shutting_down")
            self.after(1500, lambda: self._os_power("shutdown"))

        elif t == MessageType.CMD_SEND_MESSAGE.value:
            data = p.get("params", p)
            text = data.get("message", "")
            mtype = data.get("msg_type", "admin")
            self._show_popup(text, mtype)
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_SCREENSHOT.value:
            try:
                img = capture_screen_b64(p.get("quality", 50), p.get("scale", 0.75))
                resp = Message(type=MessageType.CMD_SCREENSHOT.value,
                               payload={"image": img, "pc_name": self.net.pc_name,
                                        "command_id": cid, "ref": msg.msg_id})
                resp.msg_id = msg.msg_id          # correlate with the request
                self.net.send(resp)
            except Exception as e:
                self._send_cmd_response(cid, False, error=str(e))

        elif t == MessageType.CMD_SCREEN_OBSERVE_START.value:
            params = p.get("params", p)
            self.start_observation(msg.msg_id,
                                   float(params.get("interval", 1.0)),
                                   int(params.get("quality", 50)),
                                   float(params.get("scale", 0.6)))
            self._send_cmd_response(cid, True, result="observing")

        elif t == MessageType.CMD_SCREEN_OBSERVE_STOP.value:
            self.stop_observation()
            self._send_cmd_response(cid, True, result="stopped")

        elif t == MessageType.CMD_REMOTE_START.value:
            # Task 5: the ONE thing that opens the input gate.  It can only
            # arrive from the Server's authenticated command channel (an
            # admin or authorised staff member signed in on the Server/Admin
            # Dashboard), and its command_id becomes the session id that
            # every later input batch has to echo back.
            if not cid:
                self._send_cmd_response(cid, False, error="no session id")
            else:
                params = p.get("params", {}) or {}
                self._remote_active = True
                self._remote_session = cid
                self._show_remote_banner(True)
                local_store.log_event(
                    "INFO", "remote", "Remote control started",
                    f"session_id={cid} "
                    f"admin={params.get('admin_user') or '-'} "
                    f"link={'up' if self.net.connected else 'down'}")
                self._send_cmd_response(cid, True, result="remote_active")

        elif t == MessageType.CMD_REMOTE_STOP.value:
            # Only a stop that names the session we are currently holding
            # (or an unqualified stop) may close the gate - a stale stop
            # for an already-ended session never disarms a newer one.
            wanted = str(p.get("session_id") or "")
            if self._remote_session and wanted and wanted != self._remote_session:
                self._send_cmd_response(cid, True, result="stale_stop")
            else:
                admin = (p.get("params", {}) or {}).get("admin_user") or \
                    "administrator"
                if self._remote_stop_now(f"stopped by {admin}",
                                         session=wanted or None):
                    self._send_cmd_response(cid, True, result="remote_stopped")
                else:
                    self._send_cmd_response(cid, True, result="stale_stop")

        elif t == MessageType.CMD_WEB_FILTER.value:
            # Website Access (spec 6-8): apply the versioned policy
            # snapshot, answer the command AND ack the policy version -
            # the server only shows SYNCED after that ack arrives.
            params = p.get("params", p)
            if isinstance(params.get("policy"), dict):
                pol = normalize_web_policy(params["policy"])
                ok, enforced, err = apply_web_policy(pol)
                self._send_cmd_response(
                    cid, ok,
                    result={"version": pol["version"], "mode": pol["mode"],
                            "enforced": enforced},
                    error=err)
                self.net.send(build_web_policy_ack(
                    pol["version"], pol["mode"], ok, enforced, err))
            else:
                # Legacy shape: a plain domain list for the hosts block.
                # That block is gone (P1.4) - Website Access is enforced
                # by connection detection - so the only honest action is
                # to clear anything an older build left behind and report
                # the detection state we are really in.
                domains = params.get("domains", [])
                apply_web_filter([], hosts_path=None)
                ok, err = _web_probe()
                self._send_cmd_response(
                    cid, ok, result={"domains": len(domains),
                                     "enforced": "detect" if ok else "none"},
                    error=None if ok else err)

    def _os_power(self, action):
        """Executes restart/shutdown.

        SAFETY (Phase B/#13): this is the ONLY place in the entire client
        that touches OS power, and it only runs when _power_token was armed
        by an explicit authenticated admin CMD_RESTART/CMD_SHUTDOWN message
        from the server. Heartbeat loss, timeouts, reconnect failures,
        exceptions, logout and window closing can NEVER reach it.
        """
        if self._power_token != action:
            print(f"[CLIENT] Blocked unsolicited power action: {action}")
            return
        self._power_token = None                # one-shot: consume the token
        print(f"[CLIENT] Executing admin-commanded {action}")
        try:
            if os.name == "nt":
                if action == "restart":
                    subprocess.Popen(["shutdown", "/r", "/t", "3"])
                else:
                    subprocess.Popen(["shutdown", "/s", "/t", "3"])
            else:
                subprocess.Popen(["sudo", "shutdown", "-h" if action == "shutdown" else "-r",
                                  "+1"])
        except Exception as e:
            print(f"[CLIENT] Power action failed: {e}")

    def _show_popup(self, text, mtype="admin"):
        colors = {"warning": WARN, "admin": ACCENT, "info": "#7ee787"}
        win = ctk.CTkToplevel(self)
        win.title("Message from Administrator")
        set_app_icon(win)
        win.attributes("-topmost", True)
        win.configure(fg_color=BG_DARK)
        center_window(win, 480, 220)
        ctk.CTkLabel(win, text="📢", font=("Segoe UI", 30), fg_color=BG_DARK,
                 text_color=colors.get(mtype, ACCENT)).pack(pady=(18, 4))
        ctk.CTkLabel(win, text=text, font=("Segoe UI", 12), fg_color=BG_DARK, text_color="white",
                 wraplength=420, justify="center").pack(pady=8)
        ctk.CTkButton(win, text="OK", command=win.destroy, fg_color=ACCENT, text_color="white",
                  font=("Segoe UI", 10, "bold"), width=78,
                  cursor="hand2").pack(pady=14)

    # ------------------------------------------------- screen observation
    def start_observation(self, ref_id, interval, quality, scale):
        """Stream the screen to an observing admin (spec 15, hardened P1-7).

        Three guarantees:
          * the worker owns a PRIVATE copy of its stop event, so a restart
            can never leave the previous thread running against the new
            one (and vice versa) - the previous stream stops for good and
            the thread can never dereference a cleared attribute;
          * every parameter is clamped before it reaches the capture loop,
            so a malformed command cannot peg the CPU or flood the wire;
          * the stream is recorded locally (and on the Server's audit
            trail) for exactly as long as frames are being sent, and both
            survive an offline Server.  Nothing is drawn on the Client's
            screen - the "OBSERVING" overlay was removed; the indication
            lives on the Server/Admin side only.
        """
        self.stop_observation()

        def _num(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        interval = min(10.0, max(0.3, _num(interval, 1.0)))
        quality = int(max(10, min(95, _num(quality, 50))))
        scale = min(1.0, max(0.1, _num(scale, 0.6)))

        stop = threading.Event()
        self.observe_stop = stop
        self.observe_ref = ref_id
        self.observe_cfg = {"interval": interval, "quality": quality,
                            "scale": scale}
        local_store.log_event(
            "INFO", "observe", "Screen observation started",
            f"ref={ref_id} interval={interval}s quality={quality} "
            f"scale={scale}")

        def worker(stop=stop, ref_id=ref_id, interval=interval,
                   quality=quality, scale=scale):
            while not stop.is_set():
                if not self.net.connected:
                    # Nothing to stream to - wait for the link instead of
                    # burning the CPU.  This only covers the gap before the
                    # disconnect event lands (that handler stops the stream
                    # outright); nothing here ever starts on its own.
                    stop.wait(1.0)
                    continue
                try:
                    img = capture_screen_b64(quality, scale)
                    resp = Message(
                        type=MessageType.CMD_SCREEN_OBSERVE_START.value,
                        payload={"image": img, "pc_name": self.net.pc_name,
                                 "ref": ref_id})
                    resp.msg_id = ref_id      # frames group per request
                    self.net.send(resp)
                except Exception:
                    pass
                stop.wait(interval)

        threading.Thread(target=worker, daemon=True, name="observe").start()

    def stop_observation(self):
        """Stop the capture stream.  Idempotent (P1-7): the event is taken
        out of the shared attribute first, so a second caller can never
        stop it twice, while the worker keeps its own reference and so
        always sees the real signal."""
        stop = self.observe_stop
        self.observe_stop = None
        self.observe_ref = None
        if stop is None:
            return
        stop.set()
        local_store.log_event("INFO", "observe", "Screen observation stopped")

    # -------------------------------------------- remote control (Task 5)
    def _remote_stop_now(self, reason="", session=None, audit=True):
        """Close the remote-control input gate (Task 5).

        Idempotent and never raises: it runs from the command path, from
        the disconnect handler and from shutdown.  When `session` is given,
        a stop naming a DIFFERENT session is refused outright, so a late
        or replayed stop message can never disarm a live session.  Held
        keys are released before the indicator goes away, so the PC is
        handed straight back to whoever is sitting in front of it.
        """
        sid = self._remote_session
        if session is not None and sid and str(session) != sid:
            return False
        self._remote_active = False
        self._remote_session = None
        self._release_remote_keys()
        self._show_remote_banner(False)
        if sid and audit:
            local_store.log_event("INFO", "remote", "Remote control stopped",
                                  f"session_id={sid} "
                                  f"reason={reason or 'stopped'}")
        return True

    def _release_remote_keys(self):
        """Release every key this session pressed (Task 5 'restores input')."""
        held = list(getattr(self, "_remote_keys", ()) or ())
        self._remote_keys.clear()
        if not held or os.name != "nt":
            return
        try:
            import ctypes
            u32 = ctypes.windll.user32
            for vk in held:
                u32.keybd_event(vk, u32.MapVirtualKeyW(vk, 0),
                                KEYEVENT_KEYUP, 0)
        except Exception:
            pass

    def _show_remote_banner(self, on):
        """Task 5: keep an ACTIVE remote-control session visible on the PC.

        Observation stays silent (its overlay source was deleted), but
        remote control must never be hidden from the Client: somebody is
        driving this machine's mouse and keyboard, so a persistent
        indicator stays up for exactly as long as the input gate is open.
        Idempotent and never raises - this sits on the command path.
        """
        win = getattr(self, "_remote_banner", None)
        if not on:
            if win is not None:
                self._remote_banner = None
                try:
                    win.destroy()
                except Exception:
                    pass
            return
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                return
            win = ctk.CTkToplevel(self)
            self._remote_banner = win
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
                win.attributes("-alpha", 0.97)
            except Exception:
                pass
            body = PaddedFrame(win, fg_color="#a33b00", padx=14, pady=6)
            body.pack()
            ctk.CTkLabel(body, text="\u25cf REMOTE CONTROL ACTIVE",
                     font=("Segoe UI", 12, "bold"), fg_color="#a33b00",
                     text_color="white").pack(side="left")
            ctk.CTkLabel(
                body,
                text="   your mouse and keyboard are being controlled",
                font=("Segoe UI", 9), fg_color="#a33b00",
                text_color="#ffe6cc").pack(side="left")
            win.update_idletasks()
            w = win.winfo_reqwidth()
            win.geometry(
                f"+{max(0, (win.winfo_screenwidth() - w) // 2)}+8")
        except Exception:
            self._remote_banner = None

    @staticmethod
    def _vk_for(keysym, char=""):
        """Tk keysym (+ optional character) -> (virtual-key, needs-Shift).

        Returns ``(None, False)`` when the key has no Windows equivalent,
        in which case the event is simply not injected - a remote session
        can only press keys it can name.
        """
        import ctypes
        k = str(keysym or "")
        if not k:
            return None, False
        if k in SPECIAL_VK:
            return SPECIAL_VK[k], False
        if len(k) > 1 and k[0] in ("F", "f") and k[1:].isdigit():
            n = int(k[1:])
            if 1 <= n <= 24:
                return 0x6F + n, False           # VK_F1 = 0x70
        ch = str(char or "")[:1] or (k if len(k) == 1 else "")
        if ch:
            try:
                res = int(ctypes.windll.user32.VkKeyScanW(ord(ch)))
                if res >= 0:
                    return res & 0xFF, bool(res & 0x1000)
            except Exception:
                pass
            if ch.isascii() and ch.isalnum():
                return ord(ch.upper()), False
        return None, False

    @staticmethod
    def _inject_mouse(u32, ev):
        """Translate one whitelisted mouse event into Win32 input.

        Coordinates arrive NORMALISED against the streamed frame (0.0-1.0),
        so they stay correct at any capture scale or viewer size.
        """
        try:
            dx = int(round(float(ev["x"]) * 65535))
            dy = int(round(float(ev["y"]) * 65535))
            action = ev.get("action")
            # park the cursor first so a click lands where the admin clicked
            u32.mouse_event(MOUSE_ABSOLUTE | MOUSE_VIRTUALDESK | MOUSE_MOVE,
                            dx, dy, 0, 0)
            if action == "move":
                return True
            if action == "scroll":
                u32.mouse_event(MOUSE_WHEEL, 0, 0,
                                int(round(float(ev.get("delta", 1.0))
                                          * WHEEL_DELTA)), 0)
                return True
            pair = {1: (MOUSE_LEFTDOWN, MOUSE_LEFTUP),
                    2: (MOUSE_MIDDLEDOWN, MOUSE_MIDDLEUP),
                    3: (MOUSE_RIGHTDOWN, MOUSE_RIGHTUP)}.get(
                        int(ev.get("button", 1)) or 1,
                        (MOUSE_LEFTDOWN, MOUSE_LEFTUP))
            u32.mouse_event(pair[0 if action == "down" else 1], 0, 0, 0, 0)
            return True
        except Exception:
            return False

    def _inject_key(self, u32, ev):
        """Translate one whitelisted key event into Win32 input."""
        try:
            vk, shift = self._vk_for(ev.get("keysym"), ev.get("char"))
            if vk is None:
                return False
            down = ev.get("action") == "down"
            scan = u32.MapVirtualKeyW(vk, 0)
            sscan = u32.MapVirtualKeyW(0x10, 0)
            if down:
                if shift:
                    u32.keybd_event(0x10, sscan, 0, 0)
                u32.keybd_event(vk, scan, 0, 0)
                self._remote_keys.add(vk)
            else:
                u32.keybd_event(vk, scan, KEYEVENT_KEYUP, 0)
                self._remote_keys.discard(vk)
                if shift:
                    u32.keybd_event(0x10, sscan, KEYEVENT_KEYUP, 0)
                    self._remote_keys.discard(0x10)
            return True
        except Exception:
            return False

    def _apply_remote_input(self, payload):
        """Apply one whitelisted batch of remote mouse/keyboard events.

        Four gates must all be open at once, and a failed gate applies
        NOTHING:
          1. a session is armed (an authenticated CMD_REMOTE_START),
          2. the batch echoes that exact session id,
          3. the server link is still up - a dropped link has already
             disarmed the session before another batch can be processed,
          4. every event survives protocol.sanitize_remote_events
             (mouse move/click/scroll, key down/up - nothing else).
        There is no shell, no "type text" and no arbitrary-command path
        anywhere behind this entry point.
        """
        if not self._remote_active or not self._remote_session:
            return 0
        if str(payload.get("session_id") or "") != self._remote_session:
            return 0                          # stale / forged session id
        if not self.net.connected:
            self._remote_stop_now("connection lost", audit=False)
            return 0
        events = sanitize_remote_events(payload.get("events"))
        if not events or os.name != "nt":
            return 0
        try:
            import ctypes
            u32 = ctypes.windll.user32
        except Exception:
            return 0
        applied = 0
        for ev in events:
            ok = (self._inject_mouse(u32, ev) if ev["kind"] == "mouse"
                  else self._inject_key(u32, ev) if ev["kind"] == "key"
                  else False)
            if ok:
                applied += 1
        return applied

    # ---------------------------------------------------------- dashboard
    def open_dashboard(self):
        if not self.user:
            return
        dash = getattr(self, "_dash", None)
        if dash is not None and dash.winfo_exists():
            # Already open (or parked in the tray by P0-1): pull it back
            # to normal size in front.  Opening it is never a logout.
            try:
                dash.state("normal")
            except Exception:
                pass
            try:
                dash.deiconify()
                dash.lift()
                dash.focus_force()
            except Exception:
                pass
            return
        from student_dashboard import StudentDashboard
        self._dash = StudentDashboard(self, self.user,
                                      on_logout=self.user_logout,
                                      on_close=self._client_window_closed,
                                      minimize_to_tray=self._tray_ok)

    def _close_dashboard(self):
        dash = getattr(self, "_dash", None)
        if dash:
            try:
                dash.destroy()
            except Exception:
                pass
            self._dash = None

    def _client_window_closed(self):
        """Client Closed event (P3 taxonomy): the user closed the
        dashboard window.  NOT a logout - the session keeps running on
        this PC; only the window disappears (reopen it from the session
        bar).  Distinct from `client_logout` (session_end), `client_crash`
        and `network_disconnect` (both raised server-side)."""
        try:
            self.net.send(build_activity_log(
                admin_user=(self.user or {}).get("student_id", ""),
                action="client_closed",
                target=self.net.pc_name,
                details="Client window closed by user (session continues)"))
        except Exception:
            pass

    # ------------------------------------------------------------ event pump
    def _collapse_timer(self, attr):
        """Cancel a pending Tk timer stored in `attr`.

        Called at the start of _pump/_tick so that however often the method
        runs (manual calls included) only ONE timer chain can exist - orphaned
        after-callbacks can therefore never fire on a destroyed window.
        """
        aid = getattr(self, attr, None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            setattr(self, attr, None)

    def _pump(self):
        self._collapse_timer("_after_pump")
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        try:
            self._after_pump = self.after(80, self._pump)
        except Exception:
            self._after_pump = None

    def _handle_event(self, ev):
        kind = ev.get("kind")
        if kind == "ui_call":
            # P0-1: work queued from a non-Tk thread (the system tray).
            # Never let it escape - an exception here would leave _pump
            # unscheduled and silently stop ALL event handling.
            try:
                ev["fn"]()
            except Exception:
                local_store.log_event("ERROR", "ui", "Deferred UI call failed",
                                      traceback.format_exc(limit=3))
        elif kind == "net_status":
            # Spec 15: ONE status indicator.  The chip above already says
            # Connected/Offline and shows the address, so connection-state
            # mirrors stay off this line - it carries only what the chip
            # cannot: the first-run "not configured" hint (kept until the
            # settings dialog clears it) or the reason a link failed.
            txt = str(ev.get("text") or "")
            low = txt.lower()
            lbl = getattr(self, "status_lbl", None)
            cur = (str(lbl.cget("text") or "")
                   if lbl is not None and lbl.winfo_exists() else "")
            if "not configured" in cur:
                pass                       # first-run guidance wins
            elif low.startswith("connecting to server"):
                pass                       # transient; the chip covers it
            elif low.startswith("connected to server"):
                self._set_status_hint("")  # link is healthy again
            else:
                self._set_status_hint(txt)
        elif kind == "net_disconnected":
            # Preserve an admin lock across server outages so a server
            # restart can never be used to bypass it (Phase C/#1-2).
            local_store.log_event("WARN", "net", "Server connection lost")
            was_admin_locked = self.state == self.STATE_ADMIN_LOCK
            if self.state in (self.STATE_UNLOCKED, self.STATE_PAUSED):
                self.user_logout("Connection to server lost")
            self._close_dashboard()
            self.stop_observation()
            # Task 5: a lost link ends the remote session on the spot -
            # input can never be applied while the server cannot see us,
            # and a later reconnect starts from a clean, closed session.
            self._remote_stop_now("connection lost")
            self._show_lock("Connection to server lost. Reconnecting…",
                            error=True)
            if was_admin_locked:
                self.state = self.STATE_ADMIN_LOCK
        elif kind == "net_connected":
            # Spec items 11/12/17: on every (re)connect refresh the
            # offline auth roster, re-apply Website Access, register an
            # in-flight Local Mode session, then flush the durable local
            # log queue - always off the UI thread.
            local_store.log_event("INFO", "net",
                                  "Server connection established")
            self._resume_offline_session()
            threading.Thread(target=self._sync_on_reconnect, daemon=True,
                             name="resync").start()
        elif kind == "message":
            msg = ev["msg"]
            if msg.type == MessageType.AUTH_RESPONSE.value:
                self._handle_auth_response(msg.payload)
            elif msg.type == MessageType.PASSWORD_CHANGE_RESPONSE.value:
                self._handle_password_change_response(msg.payload)
            elif msg.type in (MessageType.STU_RESPONSE.value,
                              MessageType.PONG.value):
                pass
            elif msg.type == MessageType.CMD_REMOTE_INPUT.value:
                # Task 5: input batches deliberately bypass handle_command.
                # They are not auditable commands - routing them through it
                # would write a local log row (and a client_commands row)
                # for every mouse move - and they never carry an ack.
                try:
                    self._apply_remote_input(msg.payload)
                except Exception:
                    local_store.log_event(
                        "ERROR", "remote", "Remote input batch failed",
                        traceback.format_exc(limit=3))
            else:
                try:
                    self.handle_command(msg)
                except Exception:
                    local_store.log_event(
                        "ERROR", "command",
                        "Command handling failed",
                        f"type={msg.type}: {traceback.format_exc(limit=3)}")
                    traceback.print_exc()

    def _roster_refresh_due(self):
        """P0-3: one tick of the offline-login roster refresh timer.

        `cache_auth_roster` stamps `synced_at` only when a pull actually
        succeeds, and until now that pull happened solely on reconnect.
        A client on a rock-solid link for weeks therefore carried a fresh
        roster with a stale timestamp - so the moment the server finally
        went down, every offline sign-in was refused as "expired" even
        though the cached accounts were current.  Re-pulling on a fixed
        interval keeps the stamp honest.

        Returns True when a refresh should be started right now (the
        caller runs it off the UI thread).
        """
        self._roster_due += 1
        if self._roster_due < ROSTER_REFRESH_EVERY:
            return False
        self._roster_due = 0
        return bool(self.net.connected)

    def _sync_on_reconnect(self):
        """Full resync after every (re)connect (spec items 11/12/17), in
        a fixed order: roster first (offline sign-in is only as fresh as
        its cache), then Website Access, then the local log queue."""
        for step in (self._sync_auth_roster, self._sync_web_filter,
                     self._flush_local_logs):
            try:
                step()
            except Exception:
                pass

    def _sync_auth_roster(self):
        """Pull the server's auth roster into the local cache (item 12).
        The wholesale replace is what revokes accounts the server deleted
        or disabled since the last sync."""
        resp = self.net.request("auth_roster", {}, timeout=8.0)
        if not resp or not resp.get("ok"):
            return
        data = resp.get("data") or {}
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, list):
            return
        try:
            days = int(data.get("max_offline_days") or 7)
        except (TypeError, ValueError):
            days = 7
        if local_store.cache_auth_roster(users, days):
            local_store.log_event(
                "INFO", "auth", "Auth roster synced",
                f"{len(users)} account(s); max_offline_days={days}")

    def _flush_local_logs(self):
        """Push PENDING local events to the server and mark exactly the
        acknowledged ids SYNCED (spec items 11/15/17).

        Single-flight: a second trigger while one is running just returns,
        so reconnect + timer can never double-send the same batch.  Rows
        the server did not ack stay PENDING and are retried later - they
        are never deleted or assumed delivered."""
        lock = getattr(self, "_log_flush_lock", None)
        if lock is None:                       # constructed in __init__
            lock = self._log_flush_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            return False
        try:
            if not self.net.connected:
                return False
            batch = local_store.pending_logs(100)
            if not batch:
                return True
            resp = self.net.request(
                "log_sync", {"events": batch, "pc_name": self.net.pc_name},
                timeout=8.0)
            if not resp or not resp.get("ok"):
                return False
            data = resp.get("data") or {}
            accepted = (data.get("accepted")
                        if isinstance(data, dict) else None)
            if isinstance(accepted, list) and accepted:
                local_store.mark_synced(accepted)
            return True
        except Exception:
            return False
        finally:
            lock.release()

    # ------------------------------------------------- Website Access (P1.3)
    def _web_access_scan(self):
        """One Website Access scan: detect -> queue -> notify -> close.

        Every event reaches all three sinks, in this order:

          1. the durable local queue (local_store, PENDING -> SYNCED) -
             written FIRST so it survives an offline link, a crash or a
             watchdog kill halfway through the scan
          2. a real-time ACTIVITY_LOG, which the server turns into the
             audit row the administrator sees
          3. the guarded close, whose OUTCOME goes through the same two
             sinks - a refusal is reported, never silently dropped

        Single-flight, so a slow scan cannot stack up on the next tick,
        and never raises: a dead detector must not disturb the kiosk.
        Returns the number of events handled (0 when nothing to do)."""
        with _web_lock:
            det = _web_detector
        if det is None:
            return 0
        lock = getattr(self, "_web_scan_lock", None)
        if lock is None:
            lock = self._web_scan_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            return 0
        try:
            events = det.poll()
        except Exception as e:
            try:
                local_store.log_event(
                    "WARN", "web", "Website Access scan failed", str(e))
            except Exception:
                pass
            return 0
        finally:
            lock.release()
        handled = 0
        for ev in events or []:
            try:
                self._web_handle_event(det, ev)
                handled += 1
            except Exception:
                pass                      # one bad event never stops the rest
        return handled

    def _web_handle_event(self, det, ev):
        """Queue + notify + guarded close for a single detection event."""
        try:
            user = (self.user or {}).get("student_id", "") or ""
        except Exception:
            user = ""
        try:
            pc = self.net.pc_name or ""
        except Exception:
            pc = ""
        domain = ev.get("domain") or "(unattributed)"
        status = str(ev.get("status") or "UNRESOLVED")
        reason = ev.get("reason") or ""
        who = ev.get("browser") or ev.get("process") \
            or f"pid {ev.get('pid')}"
        summary = (f"Website Access {status}: {domain} via {who}"
                   + (f" on {pc}" if pc else ""))
        detail = (f"status={status}; reason={reason}; "
                  f"ip={ev.get('ip')}; pid={ev.get('pid')}; "
                  f"process={ev.get('process')}; "
                  f"policy v{ev.get('policy_version')}; user={user or 'n/a'}")
        # 1. durable queue first: the student activity record has to
        #    survive a crash even if everything after this line does not
        local_store.log_event("WARN" if status == "DETECTED" else "INFO",
                              "web", summary, detail, user_id=user)
        # 2. real-time notification -> the server's audit row
        action = ("web_access_detected" if status == "DETECTED"
                  else "web_access_unresolved")
        self._web_notify(user, action, domain, detail)
        # 3. the guarded close - only ever through the five conditions
        if status != "DETECTED":
            return
        ok, why = det.close_browser(ev)
        outcome = f"domain={domain}; {why}"
        if ok:
            local_store.log_event("WARN", "web",
                                  "Browser closed after a Website Access "
                                  "violation", outcome, user_id=user)
            self._web_notify(user, "web_browser_closed", domain, outcome)
        else:
            # a cooldown is routine bookkeeping; anything else is a real
            # refusal the administrator has to be able to see
            local_store.log_event(
                "INFO" if "cooldown" in str(why) else "WARN",
                "web", "Website Access close not performed", outcome,
                user_id=user)
            self._web_notify(
                user, "web_close_skipped" if "cooldown" in str(why)
                else "web_close_failed", domain, outcome)

    def _web_notify(self, user, action, target, details):
        """Best-effort real-time report to the server (audit row).

        Returns whether the server took it.  When the link is down this
        simply reports False: step 1's durable queue is what carries the
        event across an outage, so the event is delayed, never lost, and
        never claimed as delivered."""
        try:
            return bool(self.net.send(build_activity_log(
                user or "system", action, target, details)))
        except Exception:
            return False

    def _resume_offline_session(self):
        """A Local Mode session still running when the server comes back
        is registered retroactively, so session history and the eventual
        session end always carry a real session id."""
        if not (getattr(self, "offline_session", False)
                and self.session_id and self.user):
            return
        try:
            ok = self.net.send(build_session_start(
                pc_name=self.net.pc_name,
                student_id=self.user.get("student_id", ""),
                full_name=self.user.get("full_name", ""),
                session_id=self.session_id))
            if ok:
                self.offline_session = False
                local_store.log_event(
                    "INFO", "session",
                    "Local Mode session registered with the server",
                    f"session={self.session_id}")
        except Exception:
            pass

    def _sync_web_filter(self):
        """Website Access (spec 6-8): on every (re)connect pull the full
        policy snapshot, install it in the detector and ack the version
        (with how it is really enforced) back so the server can show
        SYNCED (off the UI thread; never disturbs the kiosk)."""
        try:
            resp = self.net.request("web_filter", {}, timeout=6.0)
            if not resp or not resp.get("ok"):
                return
            data = resp.get("data")
            pol = normalize_web_policy(
                data if isinstance(data, dict) else None,
                domains=data if isinstance(data, list) else None)
            ok, enforced, err = apply_web_policy(pol)
            self.net.send(build_web_policy_ack(
                pol["version"], pol["mode"], ok, enforced, err))
        except Exception:
            pass

    def _handle_auth_response(self, p):
        self._cancel_verify_timeout()
        btn = getattr(self, "login_btn", None)
        if btn is not None and btn.winfo_exists():
            btn.configure(state="normal", text="LOGIN")
        if p.get("success"):
            user_data = p.get("user_data") or {}
            self.offline_session = False     # this one was verified online
            if user_data.get("must_change_password"):
                # FIRST LOGIN PASSWORD: the server authenticated the
                # account but its password is still the default - no
                # session and no normal usage until the change is done.
                local_store.log_event(
                    "INFO", "auth", "First-login password change required",
                    f"account={user_data.get('student_id', '')}")
                self._show_password_change(user_data)
            else:
                local_store.log_event(
                    "INFO", "auth", "Online sign-in granted",
                    f"{user_data.get('student_id', '')} "
                    f"({user_data.get('role', '')})")
                self._on_auth_ok(user_data)
        else:
            local_store.log_event(
                "WARN", "auth", "Sign-in rejected",
                f"account={self.id_var.get().strip() or '?'}: "
                f"{p.get('error', '')}")
            self._login_error(p.get("error", "Invalid ID or password."))
            self.net.send(build_activity_log(
                admin_user=self.id_var.get().strip() or "?",
                action="client_login_failed", target=self.net.pc_name,
                details=p.get("error", "")))

    # ------------------------------------------------------------ heartbeat
    def _send_heartbeat(self, cpu=None, ram=None):
        if cpu is None or ram is None:
            try:
                cpu, ram = snapshot_metrics()
            except Exception:
                cpu, ram = 0.0, 0.0
        status = {"login": "locked", "admin_lock": "locked",
                  "unlocked": "logged_in", "paused": "paused"}[self.state]
        # LAN medium (Ethernet/Wi-Fi): sent with every heartbeat (5 s) and
        # mirrored into the session bar so the user sees it too.
        conn_type = get_connection_type()
        try:
            if getattr(self, "bar_conn", None) and self.bar_conn.winfo_exists():
                self.bar_conn.configure(text=f"Connection: {conn_type}")
        except Exception:
            pass
        self.net.send(build_client_heartbeat(
            pc_name=self.net.pc_name, status=status, cpu=cpu, ram=ram,
            logged_in_user=self.user.get("student_id") if self.user else None,
            session_id=self.session_id,
            ip=get_local_ip(), hostname=get_hostname(),
            conn_type=conn_type))

    def _tick(self):
        """1-second UI ticker: clock, pause text, heartbeat."""
        self._collapse_timer("_after_tick")   # single chain, never orphaned
        # keep the login card's server indicator + PC footer current
        self._refresh_login_status()
        # local log queue: periodic flush while connected (spec item 11;
        # the reconnect flush covers the backlog the moment the link is
        # back, this one picks up anything logged since)
        self._log_flush_due += 1
        if self._log_flush_due >= 15:
            self._log_flush_due = 0
            if self.net.connected and local_store.log_counts()[0] > 0:
                threading.Thread(target=self._flush_local_logs,
                                 daemon=True, name="logflush").start()
        # P0-3: keep the offline-login roster fresh even on a link that
        # never drops (off the UI thread, same as the reconnect sync).
        if self._roster_refresh_due():
            threading.Thread(target=self._sync_auth_roster,
                             daemon=True, name="roster-refresh").start()
        # P1.3: Website Access detection.  The scan itself is a socket
        # table read, so it goes to its own thread - the kiosk UI must
        # never stutter because a student opened a blocked site.
        self._web_scan_due += 1
        if self._web_scan_due >= _WEB_SCAN_EVERY:
            self._web_scan_due = 0
            if web_access is not None and _web_detector is not None:
                threading.Thread(target=self._web_access_scan,
                                 daemon=True, name="web-scan").start()
        try:
            cpu, ram = snapshot_metrics()
        except Exception:
            cpu, ram = 0.0, 0.0

        if self.state == self.STATE_UNLOCKED and self.user:
            el = int(time.time() - (self.session_start or time.time()))
            h, m, s = el // 3600, (el % 3600) // 60, el % 60
            self.bar_time.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.bar_metrics.configure(text=f"CPU {cpu:.0f}%  RAM {ram:.0f}%")

        if self.state == self.STATE_PAUSED:
            # Pause NEVER expires on its own - only CMD_RESUME clears it.
            if self.pause_count_lbl:
                self.pause_count_lbl.configure(
                    text="Waiting for the administrator to resume…")

        # heartbeat every 5 s (server ACKs each one - that is also how we
        # detect a restarted/dead server and reconnect automatically)
        if int(time.time()) % 5 == 0 and getattr(self, "_last_hb", 0) != int(time.time()):
            self._last_hb = int(time.time())
            self._send_heartbeat(cpu, ram)

        try:
            self._after_tick = self.after(1000, self._tick)
        except Exception:
            self._after_tick = None

    def _pulse_metrics(self):
        pass    # metrics are refreshed in _tick

    def destroy(self):
        """Cancel pending Tk timers before teardown (avoids after-callback
        errors when the window is destroyed mid-tick)."""
        self._cancel_verify_timeout()
        # P0-1: drop the notification-area icon first so no menu callback
        # can arrive mid-teardown and the tray thread cannot keep the
        # process alive after the kiosk closes.
        try:
            self.stop_tray()
        except Exception:
            pass
        try:
            self.stop_observation()
        except Exception:
            pass
        # close the pause overlay first so any stray ticker callback that
        # still fires after teardown is harmless (labels are set to None).
        try:
            self._close_pause_win()
        except Exception:
            pass
        for attr in ("_after_pump", "_after_tick"):
            after_id = getattr(self, attr, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        # NOTE: never touches OS power - closing is always power-safe.
        try:
            super().destroy()
        except Exception:
            pass


# ==========================================================================
# Windows startup registration (P2): HKCU Run - per-user (no admin rights),
# idempotent, name "Computer Laboratory Client"
# ==========================================================================
STARTUP_VALUE_NAME = "Computer Laboratory Client"
STARTUP_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command():
    """Command line that relaunches this client on Windows sign-in."""
    if getattr(sys, "frozen", False):        # PyInstaller client.exe
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def register_startup():
    """Idempotently add this client to the current user's startup list.

    Rewriting the same name/value on every start is safe (that IS the
    idempotency) and keeps the entry correct after the folder moves.
    Never raises: a failed registration must not stop the kiosk.
    """
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_KEY_PATH,
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ,
                              _startup_command())
        return True
    except Exception as e:
        print(f"[CLIENT] Startup registration failed: {e}")
        return False


def remove_startup_registration():
    """`--uninstall-startup`: drop the Run entry and let the caller exit.

    Idempotent (a missing entry counts as success) and never raises.
    """
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY_PATH,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, STARTUP_VALUE_NAME)
        except FileNotFoundError:
            pass                                 # already gone
        return True
    except Exception as e:
        print(f"[CLIENT] Startup uninstall failed: {e}")
        return False


# ==========================================================================
# Windows watchdog (P1-4): Task Scheduler relaunches the kiosk.
#
# Two tasks, both best-effort and NEVER required for the client to run:
#   * a LOGON trigger - a second belt-and-braces start next to the HKCU
#     Run entry above,
#   * a REPEAT trigger - the actual watchdog: once a minute Windows runs
#     `client.exe --watchdog`, which checks the single-instance mutex and
#     exits straight away when the kiosk is already up.
#
# Reusing P0-1's mutex as the liveness test means no helper process, no
# service and no third-party binary - and a stray tick can never open a
# second fullscreen lock screen over the first one.
# ==========================================================================
WATCHDOG_TASK_LOGON = "Computer Laboratory Client Logon"
WATCHDOG_TASK_REPEAT = "Computer Laboratory Client Watchdog"
_SCHTASKS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _schtasks(args):
    """Run schtasks.exe once.  Returns (rc, output); never raises and
    never opens a console window (the kiosk may be a windowed exe)."""
    try:
        p = subprocess.run(["schtasks"] + list(args), capture_output=True,
                           text=True, timeout=20,
                           creationflags=_SCHTASKS_FLAGS)
        out = " ".join(x for x in ((p.stdout or "").strip(),
                                   (p.stderr or "").strip()) if x)
        return int(p.returncode), out
    except Exception as e:
        return 1, str(e)


def _watchdog_command():
    """The command Windows runs on every watchdog tick - this app itself."""
    return _startup_command() + " --watchdog"


def _task_exists(name):
    rc, _ = _schtasks(["/Query", "/TN", name])
    return rc == 0


def register_watchdog():
    """Best-effort: register the Task Scheduler watchdog for this client.

    Idempotent (existing tasks are reused), never raises and never blocks
    a start: the HKCU Run entry alone already brings the kiosk up, so a
    failed registration is logged and ignored.  Returns True when the
    repeating task is in place - that is the one that actually restarts a
    client that crashed, was closed or was killed.
    """
    if platform.system() != "Windows":
        return False
    cmd = _watchdog_command()
    if not _task_exists(WATCHDOG_TASK_LOGON):
        # A logon trigger can be refused without elevation; harmless, the
        # HKCU Run entry already covers signing in.
        _schtasks(["/Create", "/TN", WATCHDOG_TASK_LOGON, "/TR", cmd,
                   "/SC", "ONLOGON", "/F", "/RL", "LIMITED"])
    if _task_exists(WATCHDOG_TASK_REPEAT):
        return True
    rc, out = _schtasks(["/Create", "/TN", WATCHDOG_TASK_REPEAT, "/TR", cmd,
                         "/SC", "MINUTE", "/MO", "1", "/F", "/RL", "LIMITED"])
    if rc != 0:
        print(f"[CLIENT] Watchdog task not scheduled: {out}")
    return rc == 0


def remove_watchdog():
    """Drop both watchdog tasks (`--uninstall-startup`, Server mode).

    Idempotent (a missing task counts as success) and never raises.
    """
    if platform.system() != "Windows":
        return False
    removed = False
    for name in (WATCHDOG_TASK_LOGON, WATCHDOG_TASK_REPEAT):
        rc, _ = _schtasks(["/Delete", "/TN", name, "/F"])
        removed = removed or rc == 0
    return removed


def _register_watchdog_quiet():
    """Thread wrapper for register_watchdog(): it must never raise into
    the thread and never touch the Tk UI."""
    try:
        register_watchdog()
    except Exception as e:
        print(f"[CLIENT] Watchdog registration skipped: {e}")


def _watchdog_conflict(argv=None):
    """P1-4 gate: is this process allowed to start the kiosk?

    "start"    - nothing holds the single-instance mutex, proceed;
    "quiet"    - a scheduled `--watchdog` tick found the client already
                 running: exit silently, that is not an error;
    "conflict" - a human launched a second copy: tell them why nothing
                 opened.
    """
    argv = list(sys.argv if argv is None else argv)
    if acquire_single_instance():
        return "start"
    return "quiet" if any(str(a).lower() == "--watchdog" for a in argv) \
        else "conflict"


# P1-6: the only account types allowed to uninstall the Client from the
# kiosk.  Students and staff never see the button at all.
MAINTENANCE_ROLES = ("admin", "maintenance")


def _uninstall_client_work(dest=None):
    """P1-6: the non-UI half of `[Uninstall Client]`.

    Fixed order - drop the auto-start hooks, record what happened in the
    durable local store, then export that store so the audit trail is
    never lost.  `dest` is only ever passed by a test; the default puts a
    timestamped CSV next to the local database.

    Never raises: every step reports its own outcome, so the operator is
    told exactly what did and did not happen.
    """
    out = []
    ok1 = remove_startup_registration()
    out.append("Windows Startup entry removed" if ok1
               else "Windows Startup entry could not be removed")
    ok2 = remove_watchdog()
    out.append("Watchdog tasks removed" if ok2
               else "No watchdog tasks to remove")
    local_store.log_event(
        "WARN", "maintenance", "Client uninstalled",
        "Auto-start removed from this PC (Startup entry + watchdog "
        "tasks); application files and database were left in place.")
    path = local_store.export_logs(dest)
    out.append(f"Local log exported to: {path}" if path
               else "Local log export failed")
    return out


# ==========================================================================
# Entry point
# ==========================================================================
def _notify_already_running():
    """Explain why nothing opened (P0-1).  Never raises and never blocks
    silently - the message box is TOPMOST so it is not hidden behind the
    first instance's always-on-top lock screen."""
    try:
        import ctypes
        MB_ICONINFORMATION, MB_TOPMOST = 0x00000040, 0x00040000
        ctypes.windll.user32.MessageBoxW(
            None,
            "The Laboratory Client is already running on this PC.\n\n"
            "Use its window or the notification-area icon instead of "
            "starting a second copy.",
            "Computer Laboratory Management System",
            MB_ICONINFORMATION | MB_TOPMOST)
    except Exception:
        pass


def run_client():
    # P2/P1-4: `--uninstall-startup` removes the Windows startup entry
    # and both watchdog tasks, then exits without ever opening the kiosk.
    if "--uninstall-startup" in sys.argv:
        ok = remove_startup_registration()
        wok = remove_watchdog()
        print("[CLIENT] Windows startup entry removed" if ok
              else "[CLIENT] Windows startup entry could not be removed")
        print("[CLIENT] Watchdog tasks removed" if wok
              else "[CLIENT] No watchdog tasks to remove")
        return
    # P0-1/P1-4: exactly one Client per PC.  A scheduled `--watchdog`
    # tick that finds the kiosk already up just leaves (it is a routine
    # health check, not an error); a human launching a second copy is
    # told why nothing opened.  Neither ever stacks a second lock screen.
    outcome = _watchdog_conflict()
    if outcome == "quiet":
        return
    if outcome == "conflict":
        _notify_already_running()
        return
    try:
        register_startup()      # idempotent - runs on every client start
        app = ClientApp()
        # P1-4: schedule the watchdog only now that the kiosk exists, and
        # off the UI thread - schtasks takes a few hundred ms and must
        # never delay the lock screen.  Best effort by design.
        threading.Thread(target=_register_watchdog_quiet, daemon=True,
                         name="watchdog-reg").start()
        try:
            app.start_tray()    # notification-area icon (no-op if unsupported)
            app.mainloop()
        finally:
            try:
                app.stop_tray()
            except Exception:
                pass
            app.hotkeys.shutdown()
            app.stop_observation()   # P1-7: never leave a stream running
            app.net.stop()
    finally:
        release_single_instance()


if __name__ == "__main__":
    run_client()
