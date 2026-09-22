"""
student_dashboard.py
The window a logged-in student sees: time in/out for lab attendance,
PC selection, personal attendance history, announcements, and messaging
with the administrator.

All data flows through client_api, which talks to the central Server over
the LAN when running in Client Mode, or falls back to the original direct
SQLite access when running standalone on the server PC.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import client_api
from utils import now_date, now_time, export_rows_to_csv, FONT_TITLE, FONT_HEADER, center_window

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


class StudentDashboard(tk.Toplevel):
    def __init__(self, master, student_row, on_logout):
        super().__init__(master)
        self.student = student_row
        self.on_logout = on_logout
        self.title(f"Laboratory System - {self.get('full_name', 'Student')}")
        center_window(self, 980, 660)
        self.minsize(880, 580)
        self.configure(bg=BG_LIGHT)
        self.protocol("WM_DELETE_WINDOW", self._logout)

        self._build_header()
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.attendance_tab = ttk.Frame(notebook)
        self.announce_tab = ttk.Frame(notebook)
        self.messages_tab = ttk.Frame(notebook)
        self.borrow_tab = ttk.Frame(notebook)

        notebook.add(self.attendance_tab, text="  Lab Attendance  ")
        notebook.add(self.announce_tab, text="  Announcements  ")
        notebook.add(self.messages_tab, text="  Messages  ")
        notebook.add(self.borrow_tab, text="  Borrow Equipment  ")

        self._build_attendance_tab()
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
        header = tk.Frame(self, bg=BG_DARK, height=72)
        header.pack(fill="x")
        header.pack_propagate(False)

        avatar = tk.Label(header, text=(self.get("full_name", "S") or "S")[:1].upper(),
                          font=("Segoe UI", 20, "bold"), fg="white", bg=ACCENT,
                          width=2, height=1)
        avatar.pack(side="left", padx=(16, 10), pady=14)

        info = tk.Frame(header, bg=BG_DARK)
        info.pack(side="left", pady=14)
        tk.Label(info, text=f"Welcome, {self.get('full_name', 'Student')}",
                 fg="white", bg=BG_DARK, font=FONT_TITLE).pack(anchor="w")
        sub = f"ID: {self.get('student_id', '')}"
        if self.get("course"):
            sub += f"  |  {self.get('course')}"
            if self.get("year_level"):
                sub += f"  •  {self.get('year_level')}"
        tk.Label(info, text=sub, fg="#c7d2fe", bg=BG_DARK,
                 font=("Segoe UI", 10)).pack(anchor="w")

        tk.Button(header, text="  Logout  ", command=self._logout, bg=DANGER, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2",
                  padx=14, pady=6).pack(side="right", padx=16)

    def _logout(self):
        self.destroy()
        if self.on_logout:
            self.on_logout()

    # ------------------------------------------------------- attendance
    def _build_attendance_tab(self):
        top = tk.Frame(self.attendance_tab, bg=BG_LIGHT)
        top.pack(fill="x", padx=12, pady=12)

        card = tk.Frame(top, bg="white", highlightbackground="#dde3f0",
                        highlightthickness=1)
        card.pack(fill="x")

        row = tk.Frame(card, bg="white")
        row.pack(fill="x", padx=14, pady=12)

        tk.Label(row, text="Select PC:", font=("Segoe UI", 10, "bold"),
                 bg="white").pack(side="left")
        self.pc_var = tk.StringVar()
        self.pc_combo = ttk.Combobox(row, textvariable=self.pc_var, state="readonly", width=24)
        self.pc_combo.pack(side="left", padx=8)
        self._load_available_pcs()

        for text, cmd, color in [
            ("Time In", self.time_in, ACCENT),
            ("Time Out", self.time_out, DANGER),
            ("Refresh", self.refresh_attendance, "#44507a"),
            ("Export CSV", self.export_my_attendance, SUCCESS),
        ]:
            tk.Button(row, text=text, command=cmd, bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 9, "bold"), cursor="hand2", padx=10,
                      pady=5).pack(side="left", padx=4)

        self.session_lbl = tk.Label(card, text="", font=("Segoe UI", 9),
                                    fg=SUCCESS, bg="white", anchor="w")
        self.session_lbl.pack(fill="x", padx=14, pady=(0, 10))

        ttk.Label(self.attendance_tab, text="My Attendance History",
                  font=FONT_HEADER).pack(anchor="w", padx=14, pady=(8, 2))

        cols = ("date", "time_in", "time_out", "pc_name", "status")
        self.att_tree = ttk.Treeview(self.attendance_tab, columns=cols,
                                     show="headings", height=13)
        for c, label in zip(cols, ["Date", "Time In", "Time Out", "PC Used", "Status"]):
            self.att_tree.heading(c, text=label)
            self.att_tree.column(c, width=150)
        vsb = ttk.Scrollbar(self.attendance_tab, orient="vertical",
                            command=self.att_tree.yview)
        self.att_tree.configure(yscrollcommand=vsb.set)
        self.att_tree.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=8)
        vsb.pack(side="left", fill="y", pady=8, padx=(0, 14))
        self.refresh_attendance()

    def _load_available_pcs(self):
        resp = client_api.fetch_available_pcs()
        pcs = resp.get("data", []) if resp.get("ok") else []
        self.pc_combo["values"] = pcs

    def _active_session(self):
        """Today's open session as reported by the server (or local DB)."""
        resp = client_api.fetch_attendance(self.get("student_id"))
        if not resp.get("ok"):
            return None
        today = now_date()
        for r in resp.get("data", []):
            if g(r, "date") == today and g(r, "status") == "In Lab":
                return r
        return None

    def time_in(self):
        if self._active_session():
            messagebox.showinfo("Already timed in",
                                "You already have an active lab session today.",
                                parent=self)
            return
        pc = self.pc_var.get()
        if not pc:
            messagebox.showwarning("Select a PC",
                                   "Please select an available PC before timing in.",
                                   parent=self)
            return
        resp = client_api.time_in(self.get("student_id"), self.get("full_name"), pc)
        if not resp.get("ok"):
            messagebox.showerror("Time In failed", resp.get("error", "Unknown error"),
                                 parent=self)
            return
        messagebox.showinfo("Timed in", f"Time in recorded for {pc} at {now_time()}.",
                            parent=self)
        self._load_available_pcs()
        self.refresh_attendance()

    def time_out(self):
        resp = client_api.time_out(self.get("student_id"))
        if not resp.get("ok"):
            messagebox.showinfo("No active session",
                                resp.get("error", "You have no active session."),
                                parent=self)
            return
        messagebox.showinfo("Timed out", f"Time out recorded at {now_time()}.",
                            parent=self)
        self._load_available_pcs()
        self.refresh_attendance()

    def refresh_attendance(self):
        resp = client_api.fetch_attendance(self.get("student_id"))
        rows = resp.get("data", []) if resp.get("ok") else []
        for i in self.att_tree.get_children():
            self.att_tree.delete(i)
        for r in rows:
            self.att_tree.insert("", "end", values=(
                g(r, "date"), g(r, "time_in"), g(r, "time_out") or "-",
                g(r, "pc_name"), g(r, "status")))
        active = self._active_session()
        if active:
            self.session_lbl.config(
                text=f"● Active session on {g(active, 'pc_name')} since "
                     f"{g(active, 'time_in')}", fg=SUCCESS)
        else:
            self.session_lbl.config(text="○ No active session today.", fg="#8a93ad")

    def export_my_attendance(self):
        rows = [self.att_tree.item(i)["values"] for i in self.att_tree.get_children()]
        export_rows_to_csv(["Date", "Time In", "Time Out", "PC Used", "Status"], rows,
                           default_name=f"attendance_{self.get('student_id')}.csv",
                           parent=self)

    # ----------------------------------------------------- announcements
    def _build_announcements_tab(self):
        bar = tk.Frame(self.announce_tab, bg=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(bar, text="Laboratory Announcements",
                  font=FONT_HEADER).pack(side="left")
        tk.Button(bar, text="Refresh", command=self.refresh_announcements,
                  bg=ACCENT, fg="white", relief="flat", font=("Segoe UI", 9, "bold"),
                  cursor="hand2", padx=10, pady=4).pack(side="right")

        frame = tk.Frame(self.announce_tab, bg="white",
                         highlightbackground="#dde3f0", highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.announce_list = tk.Text(frame, wrap="word", state="disabled",
                                     font=("Segoe UI", 10), bg="white",
                                     relief="flat", padx=14, pady=12)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.announce_list.yview)
        self.announce_list.configure(yscrollcommand=sb.set)
        self.announce_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.refresh_announcements()

    def refresh_announcements(self):
        resp = client_api.fetch_announcements()
        rows = resp.get("data", []) if resp.get("ok") else []
        self.announce_list.config(state="normal")
        self.announce_list.delete("1.0", "end")
        if not rows:
            self.announce_list.insert("end", "No announcements yet.")
        for r in rows:
            self.announce_list.insert(
                "end", f"📌 {g(r, 'title')}   ({g(r, 'date_posted')})\n", "title")
            self.announce_list.insert("end", f"{g(r, 'message')}\n")
            self.announce_list.insert("end", f"- Posted by {g(r, 'posted_by')}\n")
            self.announce_list.insert("end", "-" * 80 + "\n\n")
        self.announce_list.tag_config("title", font=("Segoe UI", 11, "bold"),
                                      foreground=ACCENT, spacing1=6)
        self.announce_list.config(state="disabled")

    # --------------------------------------------------------- messages
    def _build_messages_tab(self):
        top = tk.Frame(self.messages_tab, bg=BG_LIGHT)
        top.pack(fill="both", expand=True, padx=12, pady=10)

        cols = ("sender", "receiver", "message", "timestamp")
        self.msg_tree = ttk.Treeview(top, columns=cols, show="headings", height=13)
        for c, label in zip(cols, ["From", "To", "Message", "Sent"]):
            self.msg_tree.heading(c, text=label)
            self.msg_tree.column(c, width=180)
        vsb = ttk.Scrollbar(top, orient="vertical", command=self.msg_tree.yview)
        self.msg_tree.configure(yscrollcommand=vsb.set)
        self.msg_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        form = tk.Frame(self.messages_tab, bg="white",
                        highlightbackground="#dde3f0", highlightthickness=1)
        form.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(form, text="Message to Administrator:", font=("Segoe UI", 10, "bold"),
                 bg="white", anchor="w").pack(anchor="w", padx=12, pady=(10, 2))
        mid = tk.Frame(form, bg="white")
        mid.pack(fill="x", padx=12, pady=(0, 12))
        self.msg_entry = tk.Text(mid, height=3, font=("Segoe UI", 10),
                                 relief="solid", bd=1)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Button(mid, text="Send ➤", command=self.send_message, bg=ACCENT, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2",
                  padx=16, pady=6).pack(side="left")
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
        bar = tk.Frame(self.borrow_tab, bg=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(bar, text="Request to Borrow Equipment",
                  font=FONT_HEADER).pack(side="left")

        form = tk.Frame(self.borrow_tab, bg="white",
                        highlightbackground="#dde3f0", highlightthickness=1)
        form.pack(fill="x", padx=12, pady=6)
        row = tk.Frame(form, bg="white")
        row.pack(fill="x", padx=14, pady=12)

        tk.Label(row, text="Item name:", bg="white",
                 font=("Segoe UI", 10)).pack(side="left")
        self.item_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.item_var, width=30,
                  font=("Segoe UI", 10)).pack(side="left", padx=8)
        tk.Label(row, text="Qty:", bg="white", font=("Segoe UI", 10)).pack(side="left")
        self.qty_var = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self.qty_var, width=8,
                  font=("Segoe UI", 10)).pack(side="left", padx=8)
        tk.Button(row, text="Submit Request", command=self.submit_borrow,
                  bg=SUCCESS, fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=12, pady=5).pack(side="left", padx=8)

        ttk.Label(self.borrow_tab, text="My Borrow Requests",
                  font=FONT_HEADER).pack(anchor="w", padx=14, pady=(10, 4))
        cols = ("item_name", "quantity", "borrow_date", "return_date", "status")
        self.borrow_tree = ttk.Treeview(self.borrow_tab, columns=cols,
                                        show="headings", height=9)
        for c, label in zip(cols, ["Item", "Qty", "Borrow Date", "Return Date", "Status"]):
            self.borrow_tree.heading(c, text=label)
            self.borrow_tree.column(c, width=150)
        vsb = ttk.Scrollbar(self.borrow_tab, orient="vertical",
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
