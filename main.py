"""
main.py
Entry point for the Computer Laboratory Management System (LAN Edition).

Launcher offers two modes:
  1. Server + Admin Console  – starts the central TCP/TLS LAN server and
     the original login window / dashboards on this (admin) PC.
  2. Client (Lab PC) Mode    – fullscreen mandatory-login kiosk that
     connects to the server, reports status, and obeys remote controls.
"""

import queue
import tkinter as tk
from tkinter import ttk, messagebox

from database import init_db, get_connection, verify_password
from utils import style_app, center_window, FONT_TITLE, FONT_LABEL
from student_dashboard import StudentDashboard
from admin_dashboard import AdminDashboard
from server import LabServer, get_local_ip


# ==========================================================================
# Mode launcher
# ==========================================================================
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Computer Laboratory Management System")
        center_window(self, 720, 470)
        self.resizable(False, False)
        self.configure(bg="#1f2a44")
        style_app(self)
        self.mode = None

        tk.Label(self, text="🖥", font=("Segoe UI", 44), fg="white",
                 bg="#1f2a44").pack(pady=(30, 0))
        tk.Label(self, text="Computer Laboratory", font=FONT_TITLE, fg="white",
                 bg="#1f2a44").pack()
        tk.Label(self, text="Management System · LAN Edition",
                 font=("Segoe UI", 12), fg="#c7d2fe", bg="#1f2a44").pack(
            pady=(0, 4))
        tk.Label(self, text="Choose how this PC will be used",
                 font=("Segoe UI", 9), fg="#8e9bc4", bg="#1f2a44").pack(
            pady=(0, 18))

        cards = tk.Frame(self, bg="#1f2a44")
        cards.pack()

        self._card(cards, "🖥", "Server + Admin Console",
                   "Run the central LAN server and manage\n"
                   "all client PCs from this computer.",
                   "#2f6fed", lambda: self.choose("server"))
        self._card(cards, "💻", "Client (Lab PC) Mode",
                   "Lock this PC at a login screen and\n"
                   "connect it to the server over the LAN.",
                   "#44507a", lambda: self.choose("client"))

        tk.Button(self, text="Exit", command=self.destroy, bg="#e5484d",
                  fg="white", relief="flat", font=("Segoe UI", 10, "bold"),
                  cursor="hand2", padx=26, pady=6).pack(pady=(20, 0))

        tk.Label(self, text="v2.0  |  LAN-Only · No cloud · No REST API",
                 font=("Segoe UI", 8), fg="#8e9bc4", bg="#1f2a44").pack(
            side="bottom", pady=10)

    def _card(self, parent, icon, title, body, color, command):
        card = tk.Frame(parent, bg="white", highlightbackground=color,
                        highlightthickness=2, cursor="hand2")
        card.pack(side="left", padx=12, ipadx=2, ipady=2)
        card.bind("<Button-1>", lambda e: command())
        inner = tk.Frame(card, bg="white")
        inner.pack(padx=24, pady=18)
        tk.Label(inner, text=icon, font=("Segoe UI", 30), bg="white").pack()
        tk.Label(inner, text=title, font=("Segoe UI", 13, "bold"),
                 bg="white", fg=color).pack(pady=(6, 4))
        tk.Label(inner, text=body, font=("Segoe UI", 9), bg="white",
                 fg="#5a6480", justify="center").pack()
        tk.Button(inner, text="Open", command=command, bg=color, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"),
                  cursor="hand2", padx=24, pady=5).pack(pady=(12, 0))

    def choose(self, mode):
        self.mode = mode
        self.destroy()


# ==========================================================================
# Original login window (now with Server attached)
# ==========================================================================
class LoginWindow(tk.Tk):
    def __init__(self, server=None, server_events=None):
        super().__init__()
        self.server = server
        self.server_events = server_events
        self.title("Computer Laboratory Management System - Login")
        center_window(self, 430, 500)
        self.resizable(False, False)
        self.configure(bg="#1f2a44")
        style_app(self)

        first_time = init_db()

        tk.Label(self, text="🖥", font=("Segoe UI", 40), fg="white",
                 bg="#1f2a44").pack(pady=(30, 0))
        tk.Label(self, text="Computer Laboratory", font=FONT_TITLE, fg="white",
                 bg="#1f2a44").pack()
        tk.Label(self, text="Management System", font=("Segoe UI", 12),
                 fg="#c7d2fe", bg="#1f2a44").pack(pady=(0, 6))

        if self.server:
            tk.Label(self, text=f"LAN server ● online  ({get_local_ip()}:"
                                f"{self.server.port})",
                     font=("Segoe UI", 9, "bold"), fg="#7ee787",
                     bg="#1f2a44").pack(pady=(0, 12))
        else:
            tk.Label(self, text="LAN server ○ offline (running standalone)",
                     font=("Segoe UI", 9), fg="#e5a83d", bg="#1f2a44").pack(
                pady=(0, 12))

        card = tk.Frame(self, bg="white")
        card.pack(padx=30, fill="x")

        tk.Label(card, text="Student ID / Username", font=FONT_LABEL,
                 bg="white").pack(anchor="w", padx=20, pady=(18, 2))
        self.id_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.id_var,
                  font=("Segoe UI", 11)).pack(fill="x", padx=20)

        tk.Label(card, text="Password", font=FONT_LABEL,
                 bg="white").pack(anchor="w", padx=20, pady=(12, 2))
        self.pw_var = tk.StringVar()
        pw_entry = ttk.Entry(card, textvariable=self.pw_var,
                             font=("Segoe UI", 11), show="*")
        pw_entry.pack(fill="x", padx=20)
        pw_entry.bind("<Return>", lambda e: self.attempt_login())

        tk.Button(card, text="Login", command=self.attempt_login, bg="#2f6fed",
                  fg="white", font=("Segoe UI", 11, "bold"), relief="flat",
                  pady=8, cursor="hand2").pack(fill="x", padx=20, pady=(20, 18))

        if first_time:
            tk.Label(self,
                     text="Server logins -> admin / admin123 · staff / staff123\n"
                          "Demo student -> 2023-00001 / student123",
                     font=("Segoe UI", 8), fg="#c7d2fe", bg="#1f2a44",
                     justify="center").pack(pady=(8, 0))

        tk.Label(self, text="v2.0  |  LAN Laboratory Attendance & Resource Management",
                 font=("Segoe UI", 8), fg="#8e9bc4", bg="#1f2a44").pack(
            side="bottom", pady=8)

    def attempt_login(self):
        student_id = self.id_var.get().strip()
        password = self.pw_var.get()
        if not student_id or not password:
            messagebox.showwarning("Missing information",
                                   "Please enter both ID and password.")
            return

        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE student_id=?",
                           (student_id,)).fetchone()
        conn.close()

        if not row or not verify_password(password, row["password"]):
            messagebox.showerror("Login failed", "Invalid ID or password.")
            return
        if row["status"] and row["status"].lower() == "inactive":
            messagebox.showerror(
                "Account inactive",
                "This account has been deactivated. Contact the administrator.")
            return

        self.withdraw()
        if row["role"] in ("admin", "staff"):
            AdminDashboard(self, row, on_logout=self.show_again,
                           server=self.server, server_events=self.server_events)
        else:
            StudentDashboard(self, row, on_logout=self.show_again)

    def show_again(self):
        self.id_var.set("")
        self.pw_var.set("")
        self.deiconify()


# ==========================================================================
# Entry point
# ==========================================================================
def run_server_mode():
    init_db()
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
