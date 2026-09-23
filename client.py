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
from tkinter import ttk, messagebox, simpledialog

import psutil
import platform

from database import get_setting, get_connection  # noqa: F401 (local fallback only)
from utils import style_app, center_window, FONT_TITLE, FONT_LABEL, FONT_HEADER
import client_api
from protocol import (
    Message, MessageType, TLSSocketWrapper, create_ssl_context,
    build_client_register, build_client_heartbeat, build_auth_request,
    build_session_start, build_session_end, build_command_response,
    build_activity_log, build_error,
)

CONFIG_NAME = "lab_config.json"
CLIENT_VERSION = "2.0"
BG_DARK = "#1f2a44"
ACCENT = "#2f6fed"
DANGER = "#e5484d"
SUCCESS = "#2fa84f"
WARN = "#e5a83d"


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
    """Low-level keyboard hook (Windows) that swallows dangerous hotkeys."""

    _VK_LWIN, _VK_RWIN = 0x5B, 0x5C
    _VK_TAB, _VK_ESC, _VK_F4, _VK_DELETE, _VK_SPACE = 0x09, 0x1B, 0x73, 0x2E, 0x20
    _VK_LMENU, _VK_RMENU, _VK_CONTROL = 0xA4, 0xA5, 0x11

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105

    def __init__(self):
        self.active = False
        self._thread = None
        self._hook_id = None
        self._tid = None
        self._ready = threading.Event()

    def _key_down(self, vk):
        return bool(ctypes_GetAsyncKeyState(vk) & 0x8000)

    def _should_block(self, vk) -> bool:
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
    def start(self, server_ip: str, server_port: int):
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

    def _drop_connection(self, reason):
        """Force-close a dead link and tell the UI to show the lock/reconnect
        screen exactly once."""
        if not self.connected:
            return
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
                    self.wrapper = TLSSocketWrapper(tls)
                    self.last_recv = time.time()
                    # Register immediately (re-registers after every
                    # server restart / reconnect).
                    reg = build_client_register(self.pc_name, get_local_ip(),
                                                get_hostname())
                    self.wrapper.send_message(reg)
                    self.connected = True
                    client_api.set_link(self)
                    self.events.put({"kind": "net_status", "text": "Connected to server ✓"})
                    self.events.put({"kind": "net_connected"})
                    if self._recv_thread is None or not self._recv_thread.is_alive():
                        self._recv_thread = threading.Thread(target=self._recv_loop,
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

    def _recv_loop(self):
        while not self._stop.is_set() and self.connected:
            wrapper = self.wrapper
            if wrapper is None:                 # closed concurrently
                break
            msg = wrapper.recv_message(timeout=0.5)
            if msg is None:
                # distinguish timeout vs disconnect
                try:
                    wrapper.sock.getpeername()
                    continue
                except Exception:
                    pass
                self._drop_connection("Connection lost. Reconnecting…")
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
# Client application (fullscreen kiosk + floating session bar)
# ==========================================================================
class ClientApp(tk.Tk):
    STATE_LOGIN = "login"
    STATE_UNLOCKED = "unlocked"
    STATE_PAUSED = "paused"
    STATE_ADMIN_LOCK = "admin_lock"

    def __init__(self):
        super().__init__()
        self.state = self.STATE_LOGIN
        self.events = queue.Queue()
        self.net = ClientNetwork(self.events)
        self.hotkeys = HotkeyBlocker()

        self.user = None                 # dict of logged-in user
        self.session_id = None
        self.session_start = None
        self.observe_stop = None
        self.lock_reason = ""
        self.pause_win = None
        self.pause_count_lbl = None
        self._pause_msg_lbl = None
        self.pause_message = ""
        self._power_token = None         # armed only by an admin power command
        self._after_verify = None

        # --- root/kiosk window ------------------------------------------
        self.overrideredirect(True)
        self.configure(bg=BG_DARK)
        self.attributes("-topmost", True)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        self.protocol("WM_DELETE_WINDOW", self._ignore_close)
        # dedicated host so rebuilding the kiosk never destroys Toplevels
        # (session bar / pause overlay)
        self.kiosk_host = tk.Frame(self, bg=BG_DARK)
        self.kiosk_host.pack(fill="both", expand=True)

        style_app(self)
        self._build_kiosk()
        self._build_bar()

        # start networking (server IP is saved once and reused automatically)
        cfg = load_config()
        if cfg.get("server_ip"):
            self.net.start(cfg["server_ip"], int(cfg.get("server_port", 8443)))
        else:
            # First run: stay visible and locked, ask for the server IP only
            # when the operator clicks the settings button (no modal loop,
            # no hidden window - closing/cancelling never traps the kiosk).
            self.after(200, self._announce_unconfigured)

        self.hotkeys.start()
        self._pump()
        self._tick()

    # ----------------------------------------------------------------- UI
    def _ignore_close(self):
        pass        # kiosk can never be closed by the user

    def _build_kiosk(self):
        for w in self.kiosk_host.winfo_children():
            w.destroy()

        wrap = tk.Frame(self.kiosk_host, bg=BG_DARK)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(wrap, text="🖥", font=("Segoe UI", 46), fg="white",
                 bg=BG_DARK).pack(pady=(0, 6))
        tk.Label(wrap, text="Computer Laboratory", font=FONT_TITLE,
                 fg="white", bg=BG_DARK).pack()
        tk.Label(wrap, text="Management System", font=("Segoe UI", 13),
                 fg="#c7d2fe", bg=BG_DARK).pack(pady=(0, 6))

        self.status_lbl = tk.Label(wrap, text="Starting…", font=("Segoe UI", 10),
                                   fg=WARN, bg=BG_DARK)
        self.status_lbl.pack(pady=(0, 8))

        # Shown only on first run (no saved server IP yet).
        self.config_btn = tk.Button(wrap, text="⚙  Server Settings",
                                    command=self._ask_server_config,
                                    bg="#44507a", fg="white", relief="flat",
                                    font=("Segoe UI", 10, "bold"),
                                    cursor="hand2", padx=14, pady=4)
        # not packed until _announce_unconfigured()

        card = tk.Frame(wrap, bg="white", padx=2, pady=2)
        card.pack(fill="x")

        inner = tk.Frame(card, bg="white")
        inner.pack(padx=28, pady=(24, 10))

        tk.Label(inner, text="User ID", font=FONT_LABEL, bg="white",
                 anchor="w").pack(anchor="w")
        self.id_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.id_var, font=("Segoe UI", 12),
                  width=28).pack(pady=(2, 12))

        tk.Label(inner, text="Password", font=FONT_LABEL, bg="white",
                 anchor="w").pack(anchor="w")
        self.pw_var = tk.StringVar()
        pw = ttk.Entry(inner, textvariable=self.pw_var, font=("Segoe UI", 12),
                       show="●", width=28)
        pw.pack(pady=(2, 16))
        pw.bind("<Return>", lambda e: self.attempt_login())
        self.id_var.set("")
        self.pw_var.set("")

        self.login_btn = tk.Button(inner, text="Log In", command=self.attempt_login,
                                   bg=ACCENT, fg="white", font=("Segoe UI", 12, "bold"),
                                   relief="flat", activebackground="#1e4fbf",
                                   activeforeground="white", cursor="hand2", pady=8)
        self.login_btn.pack(fill="x")

        self.msg_lbl = tk.Label(wrap, text="", font=("Segoe UI", 10, "bold"),
                                fg="#ff8585", bg=BG_DARK, wraplength=360,
                                justify="center")
        self.msg_lbl.pack(pady=(14, 0))

        tk.Label(wrap,
                 text="This PC is locked. Only authorized accounts may log in.\n"
                      "All activity is recorded.",
                 font=("Segoe UI", 9), fg="#8e9bc4", bg=BG_DARK,
                 justify="center").pack(side="bottom", pady=18)

        # First run (no saved server IP): always offer the settings button,
        # including after every relock, so the kiosk is never stuck.
        if not load_config().get("server_ip"):
            self.config_btn.pack(pady=(0, 14))
            self.status_lbl.config(
                text="Server IP not configured — open Server Settings below.")

    def _build_bar(self):
        """Floating session bar shown while the PC is unlocked."""
        self.bar = tk.Toplevel(self)
        self.bar.withdraw()
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)
        self.bar.configure(bg="#16203a")
        self.bar.protocol("WM_DELETE_WINDOW", self._ignore_close)

        sw = self.winfo_screenwidth()
        bw = 560
        self.bar.geometry(f"{bw}x52+{(sw - bw) // 2}+0")

        row = tk.Frame(self.bar, bg="#16203a")
        row.pack(fill="both", expand=True, padx=10, pady=6)

        self.bar_user = tk.Label(row, text="", fg="white", bg="#16203a",
                                 font=("Segoe UI", 10, "bold"), width=18, anchor="w")
        self.bar_user.pack(side="left")

        self.bar_time = tk.Label(row, text="00:00:00", fg="#7ee787", bg="#16203a",
                                 font=("Consolas", 12, "bold"))
        self.bar_time.pack(side="left", padx=8)

        self.bar_metrics = tk.Label(row, text="CPU 0%  RAM 0%", fg="#9fb4ff",
                                    bg="#16203a", font=("Segoe UI", 9))
        self.bar_metrics.pack(side="left", padx=8)

        tk.Button(row, text="My Dashboard", command=self.open_dashboard,
                  bg=ACCENT, fg="white", relief="flat", font=("Segoe UI", 9, "bold"),
                  cursor="hand2").pack(side="right", padx=(6, 0))
        tk.Button(row, text="Re-lock", command=self.user_logout,
                  bg="#44507a", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="right")

    # ------------------------------------------------------- config dialog
    def _announce_unconfigured(self):
        """First-run state: locked, visible, waiting for the server IP.
        (_build_kiosk already shows the button; this is just the nudge.)"""
        if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
            self.status_lbl.config(
                text="Server IP not configured — open Server Settings below.")

    def _ask_server_config(self):
        """One-time server IP configuration. The value is saved to disk and
        reused automatically on every start; cancelling just returns to the
        locked screen (it never loops or hides the kiosk)."""
        cfg = load_config()
        ip = simpledialog.askstring(
            "Server Connection",
            "Enter the Server (Admin PC) IP address on the LAN:",
            initialvalue=cfg.get("server_ip", "192.168.1.100"), parent=self)
        if not ip:
            self._announce_unconfigured()
            return
        port = simpledialog.askstring(
            "Server Connection", "Server port:",
            initialvalue=str(cfg.get("server_port", "8443")), parent=self) or "8443"
        try:
            port_val = int(port)
        except ValueError:
            port_val = 8443
        cfg.update({"server_ip": ip.strip(), "server_port": port_val})
        save_config(cfg)
        cfg_btn = getattr(self, "config_btn", None)
        if cfg_btn is not None:
            cfg_btn.pack_forget()
        self.net.start(cfg["server_ip"], port_val)

    # ------------------------------------------------------------ login
    def attempt_login(self):
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
            self._login_error("Not connected to the server yet. Please wait…")
            return
        self.login_btn.config(state="disabled", text="Verifying…")
        self.msg_lbl.config(text="", fg="#ff8585")
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

    def _verify_timeout(self):
        self._after_verify = None
        btn_text = str(self.login_btn.cget("text"))
        if btn_text == "Verifying…":
            self.login_btn.config(state="normal", text="Log In")
            self.msg_lbl.config(
                text="Server did not respond. Please try again.", fg="#ff8585")

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
        self.msg_lbl.config(text=text, fg="#ff8585")
        self.login_btn.config(state="normal", text="Log In")

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
        self.msg_lbl.config(text="")
        self.withdraw()                       # hide kiosk -> Windows desktop visible
        self.bar.deiconify()
        self.bar.lift()
        self.bar_user.config(
            text=f"👤 {self.user.get('full_name', '')} ({self.user.get('student_id', '')})"
            if self.user else "")
        self._pulse_metrics()

    def user_logout(self, reason=""):
        """End the session locally and return to the login screen."""
        self._cancel_verify_timeout()
        self._close_pause_win()          # never leave a pause overlay behind
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
        self._close_dashboard()
        self.bar.withdraw()
        self._show_lock(f"Logged out{': ' + reason if reason else ''}", error=False)

    def _show_lock(self, reason="", error=True):
        self.state = self.STATE_LOGIN
        self._build_kiosk()
        self.deiconify()
        self.lift()
        self.focus_force()
        self.hotkeys.start()
        if reason:
            self.msg_lbl.config(text=reason, fg="#ff8585" if error else "#7ee787")
        self.id_var.set("")
        self.pw_var.set("")
        try:
            self.grab_set()
        except Exception:
            pass

    # ------------------------------------------------------ pause / lock
    def _show_pause(self, message, seconds=0):
        """Pause stays active until the admin explicitly sends Resume -
        there is NO automatic expiry (Phase B/#6)."""
        self.pause_message = message or "Paused by administrator"
        if self.state == self.STATE_PAUSED:
            # Update the message on a repeated pause instead of leaking
            # a second overlay window.
            if hasattr(self, "_pause_msg_lbl"):
                self._pause_msg_lbl.config(text=self.pause_message)
            return
        self.state = self.STATE_PAUSED
        self.pause_until = 0
        self.bar.withdraw()
        self.withdraw()

        self.pause_win = tk.Toplevel(self)
        self.pause_win.overrideredirect(True)
        self.pause_win.attributes("-topmost", True)
        self.pause_win.configure(bg="#2b1a06")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.pause_win.geometry(f"{sw}x{sh}+0+0")
        tk.Label(self.pause_win, text="⏸", font=("Segoe UI", 70),
                 fg=WARN, bg="#2b1a06").pack(pady=(sh // 5, 10))
        tk.Label(self.pause_win, text="SESSION PAUSED", font=("Segoe UI", 34, "bold"),
                 fg="white", bg="#2b1a06").pack()
        self._pause_msg_lbl = tk.Label(
            self.pause_win, text=self.pause_message, font=("Segoe UI", 16),
            fg=WARN, bg="#2b1a06", wraplength=700, justify="center")
        self._pause_msg_lbl.pack(pady=14)
        self.pause_count_lbl = tk.Label(self.pause_win, text="",
                                        font=("Consolas", 18, "bold"),
                                        fg="#ffd79a", bg="#2b1a06")
        self.pause_count_lbl.pack(pady=8)
        tk.Label(self.pause_win,
                 text="The administrator must press Resume to continue.",
                 font=("Segoe UI", 11), fg="#c9a86a", bg="#2b1a06").pack(pady=6)
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
            self._show_pause(p.get("params", {}).get("message"))
            self._send_cmd_response(cid, True)

        elif t == MessageType.CMD_RESUME.value:
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
        win = tk.Toplevel(self)
        win.title("Message from Administrator")
        win.attributes("-topmost", True)
        win.configure(bg=BG_DARK)
        center_window(win, 480, 220)
        tk.Label(win, text="📢", font=("Segoe UI", 30), bg=BG_DARK,
                 fg=colors.get(mtype, ACCENT)).pack(pady=(18, 4))
        tk.Label(win, text=text, font=("Segoe UI", 12), bg=BG_DARK, fg="white",
                 wraplength=420, justify="center").pack(pady=8)
        tk.Button(win, text="OK", command=win.destroy, bg=ACCENT, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"), width=12,
                  cursor="hand2").pack(pady=14)

    # ------------------------------------------------- screen observation
    def start_observation(self, ref_id, interval, quality, scale):
        self.stop_observation()
        self.observe_stop = threading.Event()

        def worker():
            while not self.observe_stop.is_set():
                try:
                    img = capture_screen_b64(quality, scale)
                    resp = Message(type=MessageType.CMD_SCREEN_OBSERVE_START.value,
                                   payload={"image": img, "pc_name": self.net.pc_name,
                                            "ref": ref_id})
                    resp.msg_id = ref_id          # reuse ref so frames group per request
                    self.net.send(resp)
                except Exception:
                    pass
                self.observe_stop.wait(max(0.3, interval))

        threading.Thread(target=worker, daemon=True, name="observe").start()

    def stop_observation(self):
        if self.observe_stop:
            self.observe_stop.set()
            self.observe_stop = None

    # ---------------------------------------------------------- dashboard
    def open_dashboard(self):
        if not self.user:
            return
        if getattr(self, "_dash", None) and self._dash.winfo_exists():
            self._dash.deiconify()
            self._dash.lift()
            return
        from student_dashboard import StudentDashboard
        self._dash = StudentDashboard(self, self.user, on_logout=self.user_logout)

    def _close_dashboard(self):
        dash = getattr(self, "_dash", None)
        if dash:
            try:
                dash.destroy()
            except Exception:
                pass
            self._dash = None

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
        if kind == "net_status":
            if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
                self.status_lbl.config(text=ev["text"])
        elif kind == "net_disconnected":
            # Preserve an admin lock across server outages so a server
            # restart can never be used to bypass it (Phase C/#1-2).
            was_admin_locked = self.state == self.STATE_ADMIN_LOCK
            if self.state in (self.STATE_UNLOCKED, self.STATE_PAUSED):
                self.user_logout("Connection to server lost")
            self._close_dashboard()
            self.stop_observation()
            self._show_lock("Connection to server lost. Reconnecting…",
                            error=True)
            if was_admin_locked:
                self.state = self.STATE_ADMIN_LOCK
        elif kind == "message":
            msg = ev["msg"]
            if msg.type == MessageType.AUTH_RESPONSE.value:
                self._handle_auth_response(msg.payload)
            elif msg.type in (MessageType.STU_RESPONSE.value,
                              MessageType.PONG.value):
                pass
            else:
                try:
                    self.handle_command(msg)
                except Exception:
                    traceback.print_exc()

    def _handle_auth_response(self, p):
        self._cancel_verify_timeout()
        self.login_btn.config(state="normal", text="Log In")
        if p.get("success"):
            self._on_auth_ok(p.get("user_data") or {})
        else:
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
        self.net.send(build_client_heartbeat(
            pc_name=self.net.pc_name, status=status, cpu=cpu, ram=ram,
            logged_in_user=self.user.get("student_id") if self.user else None,
            session_id=self.session_id,
            ip=get_local_ip(), hostname=get_hostname()))

    def _tick(self):
        """1-second UI ticker: clock, pause text, heartbeat."""
        self._collapse_timer("_after_tick")   # single chain, never orphaned
        try:
            cpu, ram = snapshot_metrics()
        except Exception:
            cpu, ram = 0.0, 0.0

        if self.state == self.STATE_UNLOCKED and self.user:
            el = int(time.time() - (self.session_start or time.time()))
            h, m, s = el // 3600, (el % 3600) // 60, el % 60
            self.bar_time.config(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.bar_metrics.config(text=f"CPU {cpu:.0f}%  RAM {ram:.0f}%")

        if self.state == self.STATE_PAUSED:
            # Pause NEVER expires on its own - only CMD_RESUME clears it.
            if self.pause_count_lbl:
                self.pause_count_lbl.config(
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
# Entry point
# ==========================================================================
def run_client():
    app = ClientApp()
    try:
        app.mainloop()
    finally:
        app.hotkeys.shutdown()
        app.net.stop()


if __name__ == "__main__":
    run_client()
