"""
student_dashboard.py
The window a logged-in student sees: announcements, messaging with the
administrator, and equipment borrowing.

(Lab Attendance was intentionally removed - the system now focuses on
Internet Cafe / Computer Lab PC management.)

All data flows through client_api, which talks to the central Server over
the LAN when running in Client Mode, or falls back to the original direct
SQLite access when running standalone on the server PC.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import client_api
from utils import (FONT_TITLE, FONT_HEADER, center_window, set_app_icon,
                   get_logo, configure_theme)
from components import TabHost
import customtkinter as ctk

BG_DARK = "#1f2a44"
BG_LIGHT = "#f4f6fb"
ACCENT = "#2f6fed"
SUCCESS = "#2fa84f"
DANGER = "#e5484d"


def g(row, key, default=""):
    """Dict-safe row access (server rows are dicts, local rows too)."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        v = default
    return v if v is not None else default


class StudentDashboard(ctk.CTkToplevel):
    def __init__(self, master, student_row, on_logout, on_close=None,
                 minimize_to_tray=False):
        # Dark mode must precede the first CustomTkinter widget (see
        # AdminDashboard.__init__ for the same reasoning).
        configure_theme()
        super().__init__(master)
        self.student = student_row
        self.on_logout = on_logout
        self.on_close = on_close
        # P0-1: when a notification-area icon is available, minimizing the
        # Dashboard parks it there instead of on the taskbar.  Only the
        # Client sets this - Server-mode lookalikes keep a plain window.
        self._minimize_to_tray = bool(minimize_to_tray)
        self.title(f"Laboratory System - {self.get('full_name', 'Student')}")
        center_window(self, 980, 660)
        self.minsize(880, 580)
        self.configure(fg_color=BG_LIGHT)
        set_app_icon(self)
        # P0-1: an ordinary window - never the always-on-top behaviour of
        # the locked kiosk, so it cannot sit on top of other applications.
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass
        # Title-bar X closes ONLY this window - it is NOT a logout (P3
        # spec): the kiosk session keeps running and the server records a
        # distinct `client_closed` event.  The Logout button below remains
        # the single explicit way to end the session.
        self.protocol("WM_DELETE_WINDOW", self._close)
        if self._minimize_to_tray:
            self.bind("<Unmap>", self._on_unmap, add="+")

        self._build_header()
        notebook = TabHost(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # (Lab Attendance tab removed - Phase K/#14)
        self.announce_tab = notebook.add("Announcements")
        self.messages_tab = notebook.add("Messages")
        self.borrow_tab = notebook.add("Borrow Equipment")
        notebook.set("Announcements")

        self._build_announcements_tab()
        self._build_messages_tab()
        self._build_borrow_tab()

    # ------------------------------------------------------------- helpers
    def get(self, key, default=""):
        try:
            v = self.student[key]
        except (KeyError, TypeError):
            v = default
        return v if v is not None else default

    # ------------------------------------------------------------ header
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=BG_DARK, height=72)
        header.pack(fill="x")
        header.pack_propagate(False)

        # Official logo (the SAME asset as the login card - one source,
        # assets/images/logo.png).  Kept on self so Tk cannot GC it.
        self._hdr_logo = get_logo(44, self)
        if self._hdr_logo is not None:
            ctk.CTkLabel(header, image=self._hdr_logo, fg_color=BG_DARK).pack(
                side="left", padx=(16, 10), pady=14)
        else:
            avatar = ctk.CTkLabel(
                header, text=(self.get("full_name", "S") or "S")[:1].upper(),
                font=("Segoe UI", 20, "bold"), text_color="white", fg_color=ACCENT,
                width=18, height=15)
            avatar.pack(side="left", padx=(16, 10), pady=14)

        info = ctk.CTkFrame(header, fg_color=BG_DARK)
        info.pack(side="left", pady=14)
        ctk.CTkLabel(info, text=f"Welcome, {self.get('full_name', 'Student')}",
                 text_color="white", fg_color=BG_DARK, font=FONT_TITLE).pack(anchor="w")
        sub = f"ID: {self.get('student_id', '')}"
        if self.get("course"):
            sub += f"  |  {self.get('course')}"
            if self.get("year_level"):
                sub += f"  •  {self.get('year_level')}"
        ctk.CTkLabel(info, text=sub, text_color="#c7d2fe", fg_color=BG_DARK,
                 font=("Segoe UI", 10)).pack(anchor="w")

        ctk.CTkButton(header, text="  Logout  ", command=self._logout, fg_color=DANGER, text_color="white",
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  ).pack(side="right", padx=16)

        # TASK-2: Server IP/Port configuration no longer has a ⚙ gear on
        # the main Client display.  The feature still exists and is now
        # reachable only from secondary places - the LOGIN screen gear, or
        # this role-gated "Maintenance" entry (administrator / maintenance
        # accounts only; a student account never sees it).  The server-side
        # look-alike dashboard has no Client network to configure, so the
        # button is not built there either.
        role = str(self.get("role", "") or "")
        if role in ("admin", "maintenance") and \
                hasattr(self.master, "_ask_server_config"):
            self.maint_btn = ctk.CTkButton(
                header, text="  Maintenance  ", command=self._open_config,
                fg_color="#44507a", text_color="white", 
                font=("Segoe UI", 10, "bold"), cursor="hand2",
                )
            self.maint_btn.pack(side="right", padx=10)

    def _open_config(self):
        """Authorized secondary access to Server IP/Port settings (TASK-2)."""
        fn = getattr(self.master, "_ask_server_config", None)
        if fn is not None:
            fn()

    def _logout(self):
        """Normal Logout: the ONLY path that ends the session."""
        self.destroy()
        if self.on_logout:
            self.on_logout()

    def _close(self):
        """Client Closed: closing the window never logs the user out.

        The session stays active on this PC (reopen this dashboard from
        the session bar); only a `client_closed` event goes to the server
        so a closed window is never recorded as a logout (P3 spec).
        """
        cb, self.on_close = self.on_close, None     # fire exactly once
        self.destroy()
        if cb:
            try:
                cb()
            except Exception:
                pass

    # ------------------------------------------------------- P0-1 tray
    def _on_unmap(self, event):
        """Minimizing parks the Dashboard in the system tray (P0-1).

        Bound only when the Client has a notification-area icon, so a
        failed/absent tray can never leave the window with nowhere to go.
        Only `event.widget is self` counts (children unmap on every
        notebook tab switch) and only an *iconified* window is parked -
        the state check also stops the `withdraw()` below from re-entering,
        and makes teardown a no-op.
        """
        if event.widget is not self:
            return
        try:
            if self.state() not in ("iconic", "icon"):
                return
            self.withdraw()          # parked; tray / open_dashboard restores
        except Exception:
            pass

    # (Lab Attendance feature removed - Phase K/#14)

    # ----------------------------------------------------- announcements
    def _build_announcements_tab(self):
        bar = ctk.CTkFrame(self.announce_tab, fg_color=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(bar, text="Laboratory Announcements",
                  font=FONT_HEADER, text_color=BG_DARK).pack(side="left")
        ctk.CTkButton(bar, text="Refresh", command=self.refresh_announcements,
                  fg_color=ACCENT, text_color="white", font=("Segoe UI", 9, "bold"),
                  cursor="hand2", ).pack(side="right")

        frame = ctk.CTkFrame(self.announce_tab, fg_color="white",
                         border_color="#dde3f0", border_width=1)
        frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.announce_list = ctk.CTkTextbox(frame, wrap="word", state="disabled",
                                     font=("Segoe UI", 10), fg_color="white",
                                     padx=14, pady=12)
        sb = ctk.CTkScrollbar(frame, height=51, orientation="vertical", command=self.announce_list.yview)
        self.announce_list.configure(yscrollcommand=sb.set)
        self.announce_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.refresh_announcements()

    def refresh_announcements(self):
        resp = client_api.fetch_announcements()
        rows = resp.get("data", []) if resp.get("ok") else []
        self.announce_list.configure(state="normal")
        self.announce_list.delete("1.0", "end")
        if not rows:
            self.announce_list.insert("end", "No announcements yet.")
        for r in rows:
            self.announce_list.insert(
                "end", f"📌 {g(r, 'title')}   ({g(r, 'date_posted')})\n", "title")
            self.announce_list.insert("end", f"{g(r, 'message')}\n")
            self.announce_list.insert("end", f"- Posted by {g(r, 'posted_by')}\n")
            self.announce_list.insert("end", "-" * 80 + "\n\n")
        # CTkTextbox forbids `font` on a text tag (it would break HighDPI
        # scaling), so the title keeps its hierarchy through colour + gap.
        self.announce_list.tag_config("title", foreground=ACCENT, spacing1=6)
        self.announce_list.configure(state="disabled")

    # --------------------------------------------------------- messages
    def _build_messages_tab(self):
        top = ctk.CTkFrame(self.messages_tab, fg_color=BG_LIGHT)
        top.pack(fill="both", expand=True, padx=12, pady=10)

        cols = ("sender", "receiver", "message", "timestamp")
        self.msg_tree = ttk.Treeview(top, columns=cols, show="headings", height=13)
        for c, label in zip(cols, ["From", "To", "Message", "Sent"]):
            self.msg_tree.heading(c, text=label)
            self.msg_tree.column(c, width=180)
        vsb = ctk.CTkScrollbar(top, height=51, orientation="vertical", command=self.msg_tree.yview)
        self.msg_tree.configure(yscrollcommand=vsb.set)
        self.msg_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        form = ctk.CTkFrame(self.messages_tab, fg_color="white",
                        border_color="#dde3f0", border_width=1)
        form.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkLabel(form, text="Message to Administrator:", font=("Segoe UI", 10, "bold"),
                 fg_color="white", text_color=BG_DARK, anchor="w").pack(
            anchor="w", padx=12, pady=(10, 2))
        mid = ctk.CTkFrame(form, fg_color="white")
        mid.pack(fill="x", padx=12, pady=(0, 12))
        self.msg_entry = ctk.CTkTextbox(mid, height=45, font=("Segoe UI", 10),
                                 border_width=1)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(mid, text="Send ➤", command=self.send_message, fg_color=ACCENT, text_color="white",
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  ).pack(side="left")
        self.refresh_messages()

    def refresh_messages(self):
        resp = client_api.fetch_messages(self.get("student_id"))
        rows = resp.get("data", []) if resp.get("ok") else []
        for i in self.msg_tree.get_children():
            self.msg_tree.delete(i)
        for r in rows:
            self.msg_tree.insert("", "end", values=(
                g(r, "sender"), g(r, "receiver"), g(r, "message"), g(r, "timestamp")))

    def send_message(self):
        text = self.msg_entry.get("1.0", "end").strip()
        if not text:
            return
        resp = client_api.send_message(self.get("student_id"), text)
        if not resp.get("ok"):
            messagebox.showerror("Send failed", resp.get("error", "Unknown error"),
                                 parent=self)
            return
        self.msg_entry.delete("1.0", "end")
        self.refresh_messages()

    # ------------------------------------------------------------ borrow
    def _build_borrow_tab(self):
        bar = ctk.CTkFrame(self.borrow_tab, fg_color=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(bar, text="Request to Borrow Equipment",
                  font=FONT_HEADER, text_color=BG_DARK).pack(side="left")

        form = ctk.CTkFrame(self.borrow_tab, fg_color="white",
                        border_color="#dde3f0", border_width=1)
        form.pack(fill="x", padx=12, pady=6)
        row = ctk.CTkFrame(form, fg_color="white")
        row.pack(fill="x", padx=14, pady=12)

        ctk.CTkLabel(row, text="Item name:", fg_color="white",
                 font=("Segoe UI", 10), text_color=BG_DARK).pack(side="left")
        self.item_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.item_var, width=186,
                  font=("Segoe UI", 10)).pack(side="left", padx=8)
        ctk.CTkLabel(row, text="Qty:", fg_color="white",
                 text_color=BG_DARK, font=("Segoe UI", 10)).pack(side="left")
        self.qty_var = tk.StringVar(value="1")
        ctk.CTkEntry(row, textvariable=self.qty_var, width=54,
                  font=("Segoe UI", 10)).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Submit Request", command=self.submit_borrow,
                  fg_color=SUCCESS, text_color="white", 
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  ).pack(side="left", padx=8)

        ctk.CTkLabel(self.borrow_tab, text="My Borrow Requests",
                  font=FONT_HEADER).pack(anchor="w", padx=14, pady=(10, 4))
        cols = ("item_name", "quantity", "borrow_date", "return_date", "status")
        self.borrow_tree = ttk.Treeview(self.borrow_tab, columns=cols,
                                        show="headings", height=9)
        for c, label in zip(cols, ["Item", "Qty", "Borrow Date", "Return Date", "Status"]):
            self.borrow_tree.heading(c, text=label)
            self.borrow_tree.column(c, width=150)
        vsb = ctk.CTkScrollbar(self.borrow_tab, height=51, orientation="vertical",
                            command=self.borrow_tree.yview)
        self.borrow_tree.configure(yscrollcommand=vsb.set)
        self.borrow_tree.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=8)
        vsb.pack(side="left", fill="y", padx=(0, 14), pady=8)
        self.refresh_borrow()

    def submit_borrow(self):
        item = self.item_var.get().strip()
        qty = self.qty_var.get().strip() or "1"
        if not item:
            messagebox.showwarning("Missing item", "Please enter the item name.",
                                   parent=self)
            return
        resp = client_api.submit_borrow(self.get("student_id"), item, qty)
        if not resp.get("ok"):
            messagebox.showerror("Request failed", resp.get("error", "Unknown error"),
                                 parent=self)
            return
        self.item_var.set("")
        self.qty_var.set("1")
        self.refresh_borrow()
        messagebox.showinfo("Request submitted",
                            "Your borrow request was submitted for admin approval.",
                            parent=self)

    def refresh_borrow(self):
        resp = client_api.fetch_borrow(self.get("student_id"))
        rows = resp.get("data", []) if resp.get("ok") else []
        for i in self.borrow_tree.get_children():
            self.borrow_tree.delete(i)
        for r in rows:
            self.borrow_tree.insert("", "end", values=(
                g(r, "item_name"), g(r, "quantity"), g(r, "borrow_date"),
                g(r, "return_date") or "-", g(r, "status")))
