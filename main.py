"""
main.py
Entry point for the Computer Laboratory Management System (LAN Edition).

Run modes:
  1. Server + Admin Console  – starts the central TCP/TLS LAN server and
     the original login window / dashboards on this (admin) PC.
  2. Client (Lab PC) Mode    – fullscreen mandatory-login kiosk that
     connects to the server, reports status, and obeys remote controls.

Distribution (frozen builds - see build_exe.bat):
  * server.exe  -> starts directly in Server/Admin mode (no mode prompt)
  * client.exe  -> starts directly in Client mode
Running from source (`python main.py`) shows the mode launcher; you can
also force a mode with `python main.py --server` / `--client`.
"""

import os
import sys
import queue
import tkinter as tk
from tkinter import ttk

from database import init_db, get_connection, verify_password
from utils import (style_app, center_window, FONT_TITLE, FONT_LABEL,
                   set_app_icon, get_logo, BG_DARK, CARD, CARD_ALT, BORDER,
                   TEXT, SUBTLE)
from components import eye_icon, image_master
from student_dashboard import StudentDashboard
from admin_dashboard import AdminDashboard
from server import LabServer, get_local_ip
import customtkinter as ctk


def _detected_mode():
    """Choose the run mode without asking: command line first, then the
    frozen executable name (server.exe / client.exe)."""
    for arg in sys.argv[1:]:
        if arg.lower() in ("--server", "-s"):
            return "server"
        if arg.lower() in ("--client", "-c"):
            return "client"
    if getattr(sys, "frozen", False):
        name = os.path.splitext(os.path.basename(sys.executable))[0].lower()
        if "server" in name:
            return "server"
        if "client" in name:
            return "client"
    return None


# ==========================================================================
# Mode launcher
# ==========================================================================
class Launcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Computer Laboratory Management System")
        center_window(self, 720, 530)
        self.resizable(False, False)
        self.configure(fg_color="#1f2a44")
        style_app(self)
        set_app_icon(self)
        self.mode = None

        # Official logo - one source asset for the whole product.
        self._logo = get_logo(96, self)
        if self._logo is not None:
            ctk.CTkLabel(self, image=self._logo, fg_color="#1f2a44").pack(pady=(24, 0))
        else:
            ctk.CTkLabel(self, text="🖥", font=("Segoe UI", 44), text_color="white",
                     fg_color="#1f2a44").pack(pady=(30, 0))
        ctk.CTkLabel(self, text="Computer Laboratory", font=FONT_TITLE, text_color="white",
                 fg_color="#1f2a44").pack()
        ctk.CTkLabel(self, text="Management System · LAN Edition",
                 font=("Segoe UI", 12), text_color="#c7d2fe", fg_color="#1f2a44").pack(
            pady=(0, 4))
        ctk.CTkLabel(self, text="Choose how this PC will be used",
                 font=("Segoe UI", 9), text_color="#8e9bc4", fg_color="#1f2a44").pack(
            pady=(0, 18))

        cards = ctk.CTkFrame(self, fg_color="#1f2a44")
        cards.pack()

        self._card(cards, "🖥", "Server + Admin Console",
                   "Run the central LAN server and manage\n"
                   "all client PCs from this computer.",
                   "#2f6fed", lambda: self.choose("server"))
        self._card(cards, "💻", "Client (Lab PC) Mode",
                   "Lock this PC at a login screen and\n"
                   "connect it to the server over the LAN.",
                   "#44507a", lambda: self.choose("client"))

        ctk.CTkButton(self, text="Exit", command=self.destroy, fg_color="#e5484d",
                  text_color="white", font=("Segoe UI", 10, "bold"),
                  cursor="hand2", ).pack(pady=(20, 0))

        ctk.CTkLabel(self, text="v2.0  |  LAN-Only · No cloud · No REST API",
                 font=("Segoe UI", 8), text_color="#8e9bc4", fg_color="#1f2a44").pack(
            side="bottom", pady=10)

    def _card(self, parent, icon, title, body, color, command):
        card = ctk.CTkFrame(parent, fg_color="white", border_color=color,
                        border_width=2, cursor="hand2")
        card.pack(side="left", padx=12, ipadx=2, ipady=2)
        card.bind("<Button-1>", lambda e: command())
        inner = ctk.CTkFrame(card, fg_color="white")
        inner.pack(padx=24, pady=18)
        ctk.CTkLabel(inner, text=icon, font=("Segoe UI", 30), fg_color="white").pack()
        ctk.CTkLabel(inner, text=title, font=("Segoe UI", 13, "bold"),
                 fg_color="white", text_color=color).pack(pady=(6, 4))
        ctk.CTkLabel(inner, text=body, font=("Segoe UI", 9), fg_color="white",
                 text_color="#5a6480", justify="center").pack()
        ctk.CTkButton(inner, text="Open", command=command, fg_color=color, text_color="white",
                  font=("Segoe UI", 10, "bold"),
                  cursor="hand2", ).pack(pady=(12, 0))

    def choose(self, mode):
        self.mode = mode
        self.destroy()


# ==========================================================================
# Original login window (now with Server attached)
# ==========================================================================
class LoginWindow(ctk.CTk):
    def __init__(self, server=None, server_events=None):
        super().__init__()
        self.server = server
        self.server_events = server_events
        self.title("Computer Laboratory Management System - Login")
        center_window(self, 430, 545)
        self.resizable(False, False)
        # dark-mode chrome: same BG_DARK canvas the Client login uses, so
        # both login pages read as one product.
        self.configure(fg_color=BG_DARK)
        style_app(self)
        set_app_icon(self)

        first_time = init_db()

        # Official logo - the same asset the Launcher and Client use.
        self._logo = get_logo(84, self)
        if self._logo is not None:
            ctk.CTkLabel(self, image=self._logo, fg_color=BG_DARK).pack(pady=(24, 0))
        else:
            ctk.CTkLabel(self, text="🖥", font=("Segoe UI", 40), text_color="white",
                     fg_color=BG_DARK).pack(pady=(30, 0))
        ctk.CTkLabel(self, text="Computer Laboratory", font=FONT_TITLE, text_color="white",
                 fg_color=BG_DARK).pack()
        ctk.CTkLabel(self, text="Management System", font=("Segoe UI", 12),
                 text_color="#c7d2fe", fg_color=BG_DARK).pack(pady=(0, 6))

        if self.server:
            ctk.CTkLabel(self, text=f"Local Network Server ● online  "
                                f"({get_local_ip()}:{self.server.port})",
                     font=("Segoe UI", 9, "bold"), text_color="#7ee787",
                     fg_color=BG_DARK).pack(pady=(0, 12))
        else:
            ctk.CTkLabel(self, text="Local Network Server ○ offline "
                                "(running standalone)",
                     font=("Segoe UI", 9), text_color="#e5a83d", fg_color=BG_DARK).pack(
                pady=(0, 12))

        card = ctk.CTkFrame(self, fg_color=CARD, border_color=BORDER,
                        border_width=1)
        card.pack(padx=30, fill="x")

        ctk.CTkLabel(card, text="Username", font=FONT_LABEL,
                 fg_color=CARD, text_color=TEXT).pack(anchor="w", padx=20, pady=(18, 2))
        self.id_var = tk.StringVar()
        ctk.CTkEntry(card, textvariable=self.id_var,
                  font=("Segoe UI", 11)).pack(fill="x", padx=20)

        ctk.CTkLabel(card, text="Password", font=FONT_LABEL,
                 fg_color=CARD, text_color=TEXT).pack(anchor="w", padx=20, pady=(12, 2))
        self.pw_var = tk.StringVar()
        # Entry + visibility eye in one row, mirroring the client login
        # card (spec: a visibility eye on every password field).  The
        # toggle only flips the display mask - the value lives in
        # self.pw_var and is never touched, so authentication and the
        # audit trail see exactly what was typed.  Both glyphs are built
        # once and kept on self so Tk never garbage-collects them; the
        # button's `_eye_state` mirrors the old Show/Hide text for tests.
        pw_row = ctk.CTkFrame(card, fg_color=CARD)
        pw_row.pack(fill="x", padx=20)
        self.pw_entry = ctk.CTkEntry(pw_row, textvariable=self.pw_var,
                                  font=("Segoe UI", 11), show="*")
        self.pw_entry.pack(side="left", fill="x", expand=True)
        self.pw_entry.bind("<Return>", lambda e: self.attempt_login())
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

        # Inline feedback instead of popup dialogs (non-blocking toasts /
        # labels for normal events; dialogs are reserved for critical
        # confirmations only).
        self.err_lbl = ctk.CTkLabel(card, text="", font=("Segoe UI", 9, "bold"),
                                text_color="#ff8585", fg_color=CARD, wraplength=340,
                                justify="center")
        self.err_lbl.pack(fill="x", padx=20)

        ctk.CTkButton(card, text="Login", command=self.attempt_login, fg_color="#2f6fed",
                  text_color="white", font=("Segoe UI", 11, "bold"), 
                  cursor="hand2").pack(fill="x", padx=20, pady=(8, 18))

        if first_time:
            ctk.CTkLabel(self,
                     text="Server logins -> admin / admin123 · staff / staff123\n"
                          "Demo student -> 2023-00001 / student123",
                     font=("Segoe UI", 8), text_color="#c7d2fe", fg_color=BG_DARK,
                     justify="center").pack(pady=(8, 0))

        ctk.CTkLabel(self, text="v2.1  |  LAN Internet Cafe & Computer Lab PC Management",
                 font=("Segoe UI", 8), text_color="#8e9bc4", fg_color=BG_DARK).pack(
            side="bottom", pady=8)

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
            entry.configure(show="*")
            if btn is not None:
                btn.configure(image=self._eye_show)
                btn._eye_state = "show"
        try:
            entry.focus_set()
        except Exception:
            pass

    def _login_error(self, text):
        self.err_lbl.configure(text=text)

    def attempt_login(self):
        student_id = self.id_var.get().strip()
        password = self.pw_var.get()
        if not student_id or not password:
            self._login_error("Please enter both username and password.")
            return

        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE student_id=?",
                           (student_id,)).fetchone()
        conn.close()

        if not row or not verify_password(password, row["password"]):
            self._login_error("Invalid username or password.")
            return
        if row["status"] and row["status"].lower() == "inactive":
            self._login_error("This account has been deactivated. "
                              "Contact the administrator.")
            return

        # Opens directly into the dashboard - no client-PC selection or
        # other popups during login (Phase D/#1).
        self._login_error("")
        # Audit trail: record the console sign-in.  Nothing else in the
        # system writes these rows (the TCP side only logs client-PC
        # logins), so without this the Staff & Admin "Last Login" column
        # and the Audit Trail page would have no server-console entries.
        # Failure to log must never block the login itself.
        try:
            from utils import now_datetime
            conn = get_connection()
            conn.execute(
                "INSERT INTO admin_activity_log (admin_user, action, target, "
                "details, timestamp, ip_address) VALUES (?,?,?,?,?,?)",
                (student_id, "login_success", "Server Console",
                 f"{row['full_name']} signed in to the server console "
                 f"({row['role']})", now_datetime(), ""))
            conn.commit()
            conn.close()
        except Exception:
            pass
        self.withdraw()
        if row["role"] in ("admin", "staff", "maintenance"):
            AdminDashboard(self, row, on_logout=self.show_again,
                           server=self.server, server_events=self.server_events)
        else:
            StudentDashboard(self, row, on_logout=self.show_again)

    def show_again(self):
        self.id_var.set("")
        self.pw_var.set("")
        self._login_error("")
        self.deiconify()


# ==========================================================================
# Entry point
# ==========================================================================
def run_server_mode():
    from tkinter import messagebox
    init_db()
    # P1-4: this machine runs the Server, so it is NOT a Client PC -
    # make sure no client watchdog task left behind by a previous run
    # relaunches a kiosk on top of the Admin console.  Best effort.
    try:
        from client import remove_watchdog
        remove_watchdog()
    except Exception:
        pass
    events = queue.Queue()
    server = None
    try:
        server = LabServer(on_event=lambda kind, data: events.put((kind, data)))
        server.start()
    except OSError as e:
        messagebox.showwarning(
            "Server could not start",
            f"The LAN server could not bind its port:\n{e}\n\n"
            "Running in standalone mode (no Client PCs tab).")

    app = LoginWindow(server=server, server_events=events)
    try:
        app.mainloop()
    finally:
        if server:
            server.stop()


def main():
    # P2: `--uninstall-startup` (any run style) removes the Windows
    # startup entry and exits without opening any UI.
    if "--uninstall-startup" in sys.argv:
        from client import remove_startup_registration, remove_watchdog
        remove_startup_registration()
        remove_watchdog()
        return

    # Frozen server.exe / client.exe (and --server / --client) start
    # directly in their mode without asking (Phase K/#1-2).
    mode = _detected_mode()
    if mode == "server":
        run_server_mode()
        return
    if mode == "client":
        from client import run_client
        run_client()
        return

    launcher = Launcher()
    launcher.mainloop()
    mode = launcher.mode

    if mode == "server":
        run_server_mode()
    elif mode == "client":
        from client import run_client
        run_client()
    # mode None -> user closed the launcher


if __name__ == "__main__":
    main()
