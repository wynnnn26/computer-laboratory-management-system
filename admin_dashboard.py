"""
admin_dashboard.py
The full administrator dashboard: overview, live Client PCs (LAN monitoring
& remote control), student/staff accounts, computer management, inventory,
borrowing, maintenance, attendance, announcements, messaging, sessions,
activity logs, and OJT intern / task management.

When launched from the Server it receives the running LabServer instance
and can lock/unlock/logout/pause/resume/restart/shutdown client PCs and
watch their screens live over the LAN.
"""

import io
import base64
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from database import get_connection
from crud_frame import CRUDFrame, UserCRUDFrame
from utils import (now_date, now_time, now_datetime, FONT_TITLE,
                   FONT_HEADER, center_window)

BG_DARK = "#1f2a44"
BG_LIGHT = "#f4f6fb"
ACCENT = "#2f6fed"
SUCCESS = "#2fa84f"
DANGER = "#e5484d"
WARN = "#e5a83d"
OFFLINE = "#8a93ad"


class AdminDashboard(tk.Toplevel):
    def __init__(self, master, admin_row, on_logout,
                 server=None, server_events=None):
        super().__init__(master)
        self.admin = admin_row
        self.on_logout = on_logout
        self.server = server
        self.server_events = server_events or queue.Queue()
        self.results = queue.Queue()
        self._viewer = None
        self._observing = None

        self.is_admin = self._role() == "admin"
        self.title("Laboratory System - Administrator"
                   if self.is_admin else "Laboratory System - Staff")
        center_window(self, 1240, 760)
        self.minsize(1040, 680)
        self.configure(bg=BG_LIGHT)
        self.protocol("WM_DELETE_WINDOW", self._logout)

        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self._build_overview_tab()
        if self.server:
            self._build_clients_tab()
        self._build_students_tab()
        if self.is_admin:
            self._build_staff_tab()
        self._build_computers_tab()
        self._build_inventory_tab()
        self._build_borrow_tab()
        self._build_maintenance_tab()
        self._build_attendance_tab()
        self._build_announcements_tab()
        self._build_messages_tab()
        self._build_sessions_tab()
        self._build_activity_tab()
        self._build_ojt_tab()
        self._build_tasks_tab()

        self.notebook.bind("<<NotebookTabChanged>>",
                           lambda e: self.refresh_overview())
        self._pump()

    def _role(self):
        try:
            return self.admin["role"]
        except (KeyError, TypeError):
            return "admin"

    def _adm(self, key, default=""):
        """sqlite3.Row-safe access for the logged-in admin row."""
        try:
            v = self.admin[key]
        except (KeyError, TypeError):
            v = default
        return v if v is not None else default

    def _admin_name(self):
        return self._adm("student_id", "admin")

    # ------------------------------------------------------------ header
    def _build_header(self):
        header = tk.Frame(self, bg=BG_DARK, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="🖥  Computer Laboratory Management System",
                 fg="white", bg=BG_DARK, font=FONT_TITLE).pack(
            side="left", padx=16, pady=14)

        role_text = "Administrator" if self.is_admin else "Staff"
        badge = tk.Label(header,
                         text=f" {self._adm('full_name', '')} · {role_text} ",
                         fg="white", bg=ACCENT if self.is_admin else "#44507a",
                         font=("Segoe UI", 10, "bold"), padx=8, pady=3)
        badge.pack(side="left", padx=8)

        if self.server:
            try:
                lbl = f"LAN Server ● :{self.server.port}"
            except Exception:
                lbl = "LAN Server ●"
            tk.Label(header, text=lbl, fg="#7ee787", bg=BG_DARK,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)

        tk.Button(header, text="  Logout  ", command=self._logout, bg=DANGER, fg="white",
                  relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2",
                  padx=14, pady=6).pack(side="right", padx=16)

    def _logout(self):
        self.destroy()
        self.on_logout()

    # ---------------------------------------------------------- overview
    def _build_overview_tab(self):
        self.overview_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.overview_tab, text="  Overview  ")
        tk.Label(self.overview_tab, text="System Overview", font=FONT_TITLE,
                 bg=BG_LIGHT, fg=BG_DARK).pack(anchor="w", padx=16, pady=14)
        self.cards_frame = tk.Frame(self.overview_tab, bg=BG_LIGHT)
        self.cards_frame.pack(fill="both", expand=True, padx=16)
        self.refresh_overview()

    def refresh_overview(self):
        if not hasattr(self, "cards_frame") or not self.cards_frame.winfo_exists():
            return
        for widget in self.cards_frame.winfo_children():
            widget.destroy()
        conn = get_connection()
        stats = {
            "Total Students": conn.execute(
                "SELECT COUNT(*) c FROM users WHERE role='student'").fetchone()["c"],
            "Computers Available": conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE status='Available'").fetchone()["c"],
            "Computers In Use": conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE status='In Use'").fetchone()["c"],
            "Students In Lab Now": conn.execute(
                "SELECT COUNT(*) c FROM attendance WHERE status='In Lab'").fetchone()["c"],
            "Pending Borrow Requests": conn.execute(
                "SELECT COUNT(*) c FROM borrow_records WHERE status='Pending Approval'"
            ).fetchone()["c"],
            "Pending Maintenance": conn.execute(
                "SELECT COUNT(*) c FROM maintenance WHERE status!='Resolved'").fetchone()["c"],
            "Active OJT Interns": conn.execute(
                "SELECT COUNT(*) c FROM ojt_interns WHERE status='Active'").fetchone()["c"],
            "Open Tasks": conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE status!='Completed'").fetchone()["c"],
            "Active Sessions": conn.execute(
                "SELECT COUNT(*) c FROM client_sessions WHERE logout_time IS NULL"
            ).fetchone()["c"],
            "Logged Activity": conn.execute(
                "SELECT COUNT(*) c FROM admin_activity_log").fetchone()["c"],
        }
        conn.close()

        if self.server:
            online = sum(1 for c in self.server.list_clients() if c["is_online"])
            stats["Clients Online"] = f"{online}/{len(self.server.list_clients())}"

        colors = ["#2f6fed", "#2fa84f", "#e5a83d", "#e5484d", "#8b5cf6",
                  "#0ea5e9", "#f43f9d", "#14b8a6", "#6366f1", "#84cc16"]
        for idx, (label, value) in enumerate(stats.items()):
            r, c = divmod(idx, 4)
            color = colors[idx % len(colors)]
            card = tk.Frame(self.cards_frame, bg=color, width=230, height=92)
            card.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")
            card.grid_propagate(False)
            tk.Label(card, text=str(value), font=("Segoe UI", 24, "bold"), fg="white",
                     bg=color).pack(anchor="w", padx=14, pady=(12, 0))
            tk.Label(card, text=label, font=("Segoe UI", 10), fg="white",
                     bg=color).pack(anchor="w", padx=14)
        for c in range(4):
            self.cards_frame.columnconfigure(c, weight=1)

    # ------------------------------------------------- Client PCs (LAN)
    def _build_clients_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Client PCs  ")

        # ---- toolbar ----
        bar = tk.Frame(tab, bg=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 6))

        info = self.server._lan_ip() if self.server else "?"
        tk.Label(bar, text=f"Server {info}:{self.server.port}   |   "
                           f"Clients update automatically every 2 s",
                 font=("Segoe UI", 9), fg="#5a6480", bg=BG_LIGHT).pack(side="left")

        tk.Label(bar, text="Broadcast:", font=("Segoe UI", 9, "bold"),
                 bg=BG_LIGHT).pack(side="right", padx=(10, 4))
        self.broadcast_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.broadcast_var, width=34).pack(side="right")
        tk.Button(bar, text="Send to All", command=self._broadcast,
                  bg=ACCENT, fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=8, pady=3).pack(side="right", padx=(4, 6))

        # ---- table ----
        cols = ("pc_name", "status", "user", "ip", "cpu", "ram", "hb", "online")
        frame = tk.Frame(tab, bg=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=12)
        self.client_tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
        heads = [("pc_name", "PC Name", 130), ("status", "Status", 110),
                 ("user", "Logged-in User", 150), ("ip", "IP Address", 130),
                 ("cpu", "CPU %", 70), ("ram", "RAM %", 70),
                 ("hb", "Last Heartbeat", 120), ("online", "Connection", 100)]
        for cid, text, w in heads:
            self.client_tree.heading(cid, text=text)
            self.client_tree.column(cid, width=w, anchor="center" if w < 120 else "w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.client_tree.yview)
        self.client_tree.configure(yscrollcommand=vsb.set)
        self.client_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.client_tree.tag_configure("online", foreground="#1a7f37")
        self.client_tree.tag_configure("offline", foreground=OFFLINE)
        self.client_tree.tag_configure("paused", foreground=WARN)
        self.client_tree.tag_configure("locked", foreground="#6b7280")
        self.client_tree.bind("<Double-1>", lambda e: self._screenshot())

        # ---- control buttons ----
        btns = tk.Frame(tab, bg=BG_LIGHT)
        btns.pack(fill="x", padx=12, pady=8)
        controls = [
            ("🔒 Lock", self._lock, DANGER),
            ("🔓 Unlock", self._unlock, SUCCESS),
            ("⎋ Logout", self._logout_client, "#dc6803"),
            ("⏸ Pause", self._pause, WARN),
            ("▶ Resume", self._resume, SUCCESS),
            ("🔄 Restart", self._restart, "#8b5cf6"),
            ("⏻ Shutdown", self._shutdown, "#be123c"),
            ("📷 Screenshot", self._screenshot, ACCENT),
            ("👁 Observe", self._observe, "#0ea5e9"),
            ("💬 Message", self._send_msg, "#6366f1"),
        ]
        for text, cmd, color in controls:
            tk.Button(btns, text=text, command=cmd, bg=color, fg="white",
                      relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2",
                      padx=9, pady=5).pack(side="left", padx=3)

        self.client_status_lbl = tk.Label(
            tab, text="Select a client PC (double-click for screenshot).",
            font=("Segoe UI", 9), fg="#5a6480", bg=BG_LIGHT, anchor="w")
        self.client_status_lbl.pack(fill="x", padx=14, pady=(0, 8))

        self._refresh_clients()
        self._auto_refresh_clients()

    # ---- client list -------------------------------------------------
    def _refresh_clients(self):
        if not hasattr(self, "client_tree") or not self.client_tree.winfo_exists():
            return
        snap = {c["pc_name"]: c for c in self.server.list_clients()}
        # include registered-but-offline PCs from the database
        try:
            conn = get_connection()
            for r in conn.execute(
                    "SELECT pc_name, ip_address, cpu_percent, ram_percent, last_heartbeat "
                    "FROM computers").fetchall():
                if r["pc_name"] not in snap:
                    snap[r["pc_name"]] = {
                        "pc_name": r["pc_name"], "status": "offline",
                        "user": "", "ip": r["ip_address"] or "",
                        "cpu_percent": r["cpu_percent"] or 0,
                        "ram_percent": r["ram_percent"] or 0,
                        "last_heartbeat": r["last_heartbeat"] or "-",
                        "is_online": 0,
                    }
            conn.close()
        except Exception:
            pass

        selected = self._selected_pc()
        for item in self.client_tree.get_children():
            self.client_tree.delete(item)

        status_map = {"locked": "🔒 Locked", "logged_in": "🟢 In Session",
                      "paused": "⏸ Paused", "maintenance": "🛠 Maintenance",
                      "idle": "💤 Idle", "offline": "○ Offline"}
        for name in sorted(snap.keys()):
            c = snap[name]
            online = bool(c.get("is_online"))
            tags = ()
            if not online:
                tags = ("offline",)
            elif c.get("status") == "paused":
                tags = ("paused",)
            elif c.get("status") == "logged_in":
                tags = ("online",)
            else:
                tags = ("locked",)
            self.client_tree.insert("", "end", tags=tags, values=(
                c.get("pc_name", ""),
                status_map.get(c.get("status"), c.get("status", "")),
                c.get("logged_in_user") or c.get("user") or "",
                c.get("ip", ""),
                f"{c.get('cpu_percent', 0):.0f}",
                f"{c.get('ram_percent', 0):.0f}",
                c.get("last_heartbeat", ""),
                "● Online" if online else "○ Offline",
            ))
        if selected:
            for item in self.client_tree.get_children():
                if self.client_tree.item(item, "values")[0] == selected:
                    self.client_tree.selection_set(item)
                    break

    def _auto_refresh_clients(self):
        if not self.winfo_exists():
            return
        self._refresh_clients()
        self._pump_results()
        self.after(2000, self._auto_refresh_clients)

    def _selected_pc(self):
        sel = self.client_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a client PC first.",
                                parent=self)
            return None
        return self.client_tree.item(sel[0], "values")[0]

    # ---- command helpers (run off the GUI thread) ----------------------
    def _run_cmd(self, label, fn, *args, **kwargs):
        pc = kwargs.pop("pc", None) or self._selected_pc()
        if not pc:
            return

        def worker():
            try:
                resp = fn(pc, *args, **kwargs)
            except Exception as e:
                resp = {"success": False, "error": str(e)}
            self.results.put((label, pc, resp))

        threading.Thread(target=worker, daemon=True, name="cmd").start()
        self.client_status_lbl.config(text=f"{label} → {pc} …", fg="#5a6480")

    def _pump_results(self):
        try:
            while True:
                label, pc, resp = self.results.get_nowait()
                ok = resp.get("success")
                txt = f"{label} → {pc}: " + ("✓ done" if ok
                                             else resp.get("error", "failed"))
                self.client_status_lbl.config(
                    text=txt, fg=SUCCESS if ok else DANGER)
                if label == "Screenshot" and ok:
                    self._open_viewer(f"Screenshot · {pc}",
                                      resp.get("data", {}), single=True)
                self._log_local(f"{label}:{pc}", resp)
        except queue.Empty:
            pass
        # server events (screen frames, status changes…)
        try:
            while True:
                kind, data = self.server_events.get_nowait()
                if kind == "screen_frame" and self._viewer_alive():
                    self._viewer_show(data.get("data", {}), data.get("pc_name", ""))
        except queue.Empty:
            pass

    def _pump(self):
        if not self.winfo_exists():
            return
        self._pump_results()
        self.after(250, self._pump)

    def _log_local(self, action, resp):
        if not resp.get("success"):
            return
        try:
            conn = get_connection()
            conn.execute(
                "INSERT INTO admin_activity_log (admin_user, action, target, details, "
                "timestamp) VALUES (?,?,?,?,?)",
                (self._admin_name(), action.split(":")[0],
                 action.split(":")[-1],
                 str(resp.get("result", resp.get("error", "")))[:200],
                 now_datetime()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ---- individual controls -------------------------------------------
    def _lock(self):
        msg = simpledialog.askstring(
            "Lock client", "Lock message (Cancel for default):",
            initialvalue="Locked by the administrator.", parent=self)
        if msg is None:
            msg = "Locked by the administrator."
        self._run_cmd("Lock", self.server.lock_client, msg=msg,
                      admin=self._admin_name())

    def _unlock(self):
        self._run_cmd("Unlock", self.server.unlock_client, admin=self._admin_name())

    def _logout_client(self):
        if not messagebox.askyesno("Remote logout",
                                   "Log the user out of this client PC "
                                   "(returns it to the login screen)?",
                                   parent=self):
            return
        self._run_cmd("Logout", self.server.logout_client, admin=self._admin_name())

    def _pause(self):
        msg = simpledialog.askstring(
            "Pause client", "Pause message (Cancel for default):",
            initialvalue="Paused by administrator. Please wait.", parent=self)
        if msg is None:
            msg = "Paused by administrator. Please wait."
        self._run_cmd("Pause", self.server.pause_client, admin=self._admin_name(),
                      seconds=0, msg=msg)

    def _resume(self):
        self._run_cmd("Resume", self.server.resume_client, admin=self._admin_name())

    def _restart(self):
        pc = self._selected_pc()
        if not pc:
            return
        if not messagebox.askyesno("Restart client",
                                   f"Restart {pc}? The user will lose their session.",
                                   parent=self):
            return
        self._run_cmd("Restart", self.server.restart_client, pc=pc,
                      admin=self._admin_name())

    def _shutdown(self):
        pc = self._selected_pc()
        if not pc:
            return
        if not messagebox.askyesno("Shutdown client",
                                   f"Shut down {pc}?", parent=self):
            return
        self._run_cmd("Shutdown", self.server.shutdown_client, pc=pc,
                      admin=self._admin_name())

    def _send_msg(self):
        pc = self._selected_pc()
        if not pc:
            return
        text = simpledialog.askstring("Message to client",
                                      f"Message for {pc}:", parent=self)
        if not text:
            return
        self._run_cmd("Message", self.server.send_client_message, pc=pc,
                      text=text, admin=self._admin_name())

    def _broadcast(self):
        text = self.broadcast_var.get().strip()
        if not text:
            return
        def worker():
            resp = self.server.broadcast_message(text, self._admin_name())
            ok = sum(1 for v in resp.values() if v.get("success"))
            self.results.put(("Broadcast", f"{ok}/{len(resp)} PCs",
                              {"success": ok > 0,
                               "error": None if ok else "No clients reachable"}))
        threading.Thread(target=worker, daemon=True).start()
        self.broadcast_var.set("")
        self.client_status_lbl.config(text="Broadcasting…", fg="#5a6480")

    def _screenshot(self):
        pc = self._selected_pc()
        if not pc:
            return

        def worker():
            resp = self.server.request_screenshot(pc, self._admin_name())
            self.results.put(("Screenshot", pc, resp))

        threading.Thread(target=worker, daemon=True).start()
        self.client_status_lbl.config(text=f"Requesting screenshot from {pc}…",
                                      fg="#5a6480")

    def _observe(self):
        pc = self._selected_pc()
        if not pc:
            return
        if self._observing == pc:
            self.server.stop_screen_observe(pc, self._admin_name())
            self._observing = None
            self.client_status_lbl.config(text=f"Observation stopped · {pc}",
                                          fg="#5a6480")
            return
        if self._observing:
            self.server.stop_screen_observe(self._observing, self._admin_name())
        ok = self.server.start_screen_observe(pc, self._admin_name())
        if ok:
            self._observing = pc
            self._open_viewer(f"Live Screen · {pc}", {}, single=False)
            self.client_status_lbl.config(text=f"Observing {pc} live…", fg="#0ea5e9")
        else:
            self.client_status_lbl.config(text=f"{pc} is offline", fg=DANGER)

    # ---- screen viewer ---------------------------------------------------
    def _open_viewer(self, title, data, single=True):
        if self._viewer and self._viewer.winfo_exists():
            self._viewer.title(title)
            self._viewer.single = single
            self._viewer.deiconify()
            self._viewer.lift()
        else:
            win = tk.Toplevel(self)
            win.title(title)
            win.configure(bg="#0d1117")
            center_window(win, 900, 600)
            win.single = single
            win.label = tk.Label(win, bg="#0d1117", text="Waiting for frames…",
                                 fg="#8b949e", font=("Segoe UI", 11))
            win.label.pack(fill="both", expand=True, padx=8, pady=8)
            win.protocol("WM_DELETE_WINDOW", self._close_viewer)
            self._viewer = win
        if data:
            self._viewer_show(data, "")

    def _viewer_alive(self):
        return bool(self._viewer and self._viewer.winfo_exists())

    def _close_viewer(self):
        if self._observing and self.server:
            self.server.stop_screen_observe(self._observing, self._admin_name())
            self._observing = None
        if self._viewer:
            try:
                self._viewer.destroy()
            except Exception:
                pass
            self._viewer = None

    def _viewer_show(self, data, pc_name):
        """Decode a base64 JPEG frame into the viewer window."""
        if not self._viewer_alive():
            return
        img_b64 = data.get("image") if isinstance(data, dict) else None
        if not img_b64:
            return
        try:
            from PIL import Image, ImageTk
            raw = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(raw))
            photo = ImageTk.PhotoImage(img)
            self._viewer.label.config(image=photo, text="")
            self._viewer.label.image = photo          # keep a reference
            if pc_name or data.get("pc_name"):
                self._viewer.title(f"Screen · {pc_name or data.get('pc_name')}")
        except Exception as e:
            self._viewer.label.config(text=f"Frame decode error: {e}")

    # --------------------------------------------------------- accounts
    def _build_students_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Student Accounts  ")
        fields = [
            {"name": "student_id", "label": "Student ID", "type": "entry", "required": True},
            {"name": "password", "label": "Password (leave blank to keep)", "type": "entry"},
            {"name": "full_name", "label": "Full Name", "type": "entry", "required": True},
            {"name": "course", "label": "Course", "type": "entry"},
            {"name": "year_level", "label": "Year Level", "type": "combobox",
             "options": ["1st Year", "2nd Year", "3rd Year", "4th Year"]},
            {"name": "email", "label": "Email", "type": "entry"},
            {"name": "contact", "label": "Contact No.", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Inactive"]},
        ]
        UserCRUDFrame(tab, table="users", fields=fields, title="Manage Student Accounts",
                      fixed_values={"role": "student"}, where_clause="role='student'",
                      search_field="full_name", defaults={"status": "Active"}
                      ).pack(fill="both", expand=True)

    def _build_staff_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Staff & Admin Accounts  ")
        fields = [
            {"name": "student_id", "label": "Username", "type": "entry", "required": True},
            {"name": "password", "label": "Password (leave blank to keep)", "type": "entry"},
            {"name": "full_name", "label": "Full Name", "type": "entry", "required": True},
            {"name": "role", "label": "Role", "type": "combobox",
             "options": ["staff", "admin"], "required": True},
            {"name": "email", "label": "Email", "type": "entry"},
            {"name": "contact", "label": "Contact No.", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Inactive"]},
        ]
        UserCRUDFrame(tab, table="users", fields=fields,
                      title="Manage Staff & Administrator Accounts",
                      where_clause="role IN ('staff','admin')",
                      search_field="full_name",
                      defaults={"status": "Active", "role": "staff"}
                      ).pack(fill="both", expand=True)

    # -------------------------------------------------------- computers
    def _build_computers_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Computer Management  ")
        fields = [
            {"name": "pc_name", "label": "PC Name/No.", "type": "entry", "required": True},
            {"name": "specs", "label": "Specifications", "type": "entry"},
            {"name": "location", "label": "Location", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Available", "In Use", "Maintenance", "Reserved"]},
            {"name": "assigned_to", "label": "Assigned Student ID", "type": "entry"},
            {"name": "ip_address", "label": "IP Address", "type": "entry"},
            {"name": "mac_address", "label": "MAC Address", "type": "entry"},
        ]
        CRUDFrame(tab, table="computers", fields=fields,
                  title="Manage Computers & PC Assignment",
                  search_field="pc_name",
                  defaults={"status": "Available"}).pack(fill="both", expand=True)

    # -------------------------------------------------------- inventory
    def _build_inventory_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Inventory  ")
        fields = [
            {"name": "item_name", "label": "Item Name", "type": "entry", "required": True},
            {"name": "category", "label": "Category", "type": "entry"},
            {"name": "quantity", "label": "Quantity", "type": "entry"},
            {"name": "condition_status", "label": "Condition", "type": "combobox",
             "options": ["New", "Good", "Fair", "Damaged", "For Disposal"]},
            {"name": "location", "label": "Location", "type": "entry"},
            {"name": "remarks", "label": "Remarks", "type": "entry"},
        ]
        CRUDFrame(tab, table="inventory", fields=fields, title="Computer Lab Inventory",
                  search_field="item_name").pack(fill="both", expand=True)

    # ----------------------------------------------------------- borrow
    def _build_borrow_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Equipment Borrowing  ")
        fields = [
            {"name": "student_id", "label": "Student ID", "type": "entry", "required": True},
            {"name": "item_name", "label": "Item Name", "type": "entry", "required": True},
            {"name": "quantity", "label": "Quantity", "type": "entry"},
            {"name": "borrow_date", "label": "Borrow Date", "type": "entry"},
            {"name": "return_date", "label": "Return Date", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Pending Approval", "Approved", "Borrowed", "Returned", "Overdue"]},
        ]
        CRUDFrame(tab, table="borrow_records", fields=fields,
                  title="Equipment Borrowing Records",
                  search_field="student_id",
                  defaults={"borrow_date": now_date, "status": "Pending Approval"}
                  ).pack(fill="both", expand=True)

    # ------------------------------------------------------- maintenance
    def _build_maintenance_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Maintenance  ")
        fields = [
            {"name": "asset_name", "label": "PC / Equipment", "type": "entry", "required": True},
            {"name": "issue", "label": "Issue Description", "type": "entry", "required": True},
            {"name": "date_reported", "label": "Date Reported", "type": "entry"},
            {"name": "date_resolved", "label": "Date Resolved", "type": "entry"},
            {"name": "technician", "label": "Technician", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Pending", "In Progress", "Resolved"]},
        ]
        CRUDFrame(tab, table="maintenance", fields=fields, title="Maintenance Records",
                  search_field="asset_name",
                  defaults={"date_reported": now_date, "status": "Pending"}
                  ).pack(fill="both", expand=True)

    # ------------------------------------------------------- attendance
    def _build_attendance_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Lab Attendance  ")
        fields = [
            {"name": "student_id", "label": "Student ID", "type": "entry", "required": True},
            {"name": "full_name", "label": "Full Name", "type": "entry"},
            {"name": "pc_name", "label": "PC Used", "type": "entry"},
            {"name": "date", "label": "Date", "type": "entry"},
            {"name": "time_in", "label": "Time In", "type": "entry"},
            {"name": "time_out", "label": "Time Out", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["In Lab", "Completed"]},
        ]
        CRUDFrame(tab, table="attendance", fields=fields,
                  title="Laboratory Attendance & Computer Usage Records",
                  search_field="student_id",
                  defaults={"date": now_date, "time_in": now_time}
                  ).pack(fill="both", expand=True)

    # ---------------------------------------------------- announcements
    def _build_announcements_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Announcements  ")
        fields = [
            {"name": "title", "label": "Title", "type": "entry", "required": True},
            {"name": "message", "label": "Message", "type": "text", "required": True},
            {"name": "posted_by", "label": "Posted By", "type": "entry"},
            {"name": "date_posted", "label": "Date Posted", "type": "entry"},
        ]
        CRUDFrame(tab, table="announcements", fields=fields,
                  title="Post Laboratory Announcements",
                  defaults={"posted_by": self._adm("full_name", "admin"),
                            "date_posted": now_date}
                  ).pack(fill="both", expand=True)

    # -------------------------------------------------------- messages
    def _build_messages_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Messages  ")
        fields = [
            {"name": "sender", "label": "From", "type": "entry"},
            {"name": "receiver", "label": "To (Student ID)", "type": "entry",
             "required": True},
            {"name": "message", "label": "Message", "type": "text", "required": True},
            {"name": "timestamp", "label": "Sent At", "type": "entry"},
            {"name": "is_read", "label": "Read?", "type": "combobox",
             "options": ["Yes", "No"]},
        ]
        CRUDFrame(tab, table="messages", fields=fields, title="Messages with Students",
                  search_field="receiver",
                  defaults={"sender": self._admin_name(),
                            "timestamp": now_datetime, "is_read": "No"}
                  ).pack(fill="both", expand=True)

    # --------------------------------------------------------- sessions
    def _build_sessions_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Sessions  ")
        fields = [
            {"name": "session_id", "label": "Session ID", "type": "entry"},
            {"name": "pc_name", "label": "PC Name", "type": "entry"},
            {"name": "student_id", "label": "Student ID", "type": "entry"},
            {"name": "full_name", "label": "Full Name", "type": "entry"},
            {"name": "login_time", "label": "Login Time", "type": "entry"},
            {"name": "logout_time", "label": "Logout Time", "type": "entry"},
            {"name": "duration_seconds", "label": "Duration (s)", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Completed", "Disconnected"]},
        ]
        CRUDFrame(tab, table="client_sessions", fields=fields,
                  title="User Session History (Server-recorded)",
                  search_field="student_id", order_by="id DESC"
                  ).pack(fill="both", expand=True)

    # ------------------------------------------------------ activity log
    def _build_activity_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Activity Log  ")
        fields = [
            {"name": "admin_user", "label": "User", "type": "entry"},
            {"name": "action", "label": "Action", "type": "entry"},
            {"name": "target", "label": "Target", "type": "entry"},
            {"name": "details", "label": "Details", "type": "text"},
            {"name": "timestamp", "label": "Timestamp", "type": "entry"},
            {"name": "ip_address", "label": "IP", "type": "entry"},
        ]
        CRUDFrame(tab, table="admin_activity_log", fields=fields,
                  title="Admin / Staff Activity Audit Trail",
                  search_field="action", order_by="id DESC", allow_delete=False
                  ).pack(fill="both", expand=True)

    # ---------------------------------------------------------- ojt tab
    def _build_ojt_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  OJT Interns  ")
        fields = [
            {"name": "name", "label": "Intern Name", "type": "entry", "required": True},
            {"name": "skills", "label": "Skills", "type": "entry"},
            {"name": "contact", "label": "Contact", "type": "entry"},
            {"name": "school", "label": "School / University", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Completed", "Inactive"]},
        ]
        CRUDFrame(tab, table="ojt_interns", fields=fields, title="Manage OJT Interns",
                  search_field="name", defaults={"status": "Active"}
                  ).pack(fill="both", expand=True)

    # --------------------------------------------------------- tasks tab
    def _build_tasks_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  OJT Tasks  ")
        fields = [
            {"name": "intern_name", "label": "Intern Name", "type": "entry",
             "required": True},
            {"name": "task_title", "label": "Task Title", "type": "entry",
             "required": True},
            {"name": "description", "label": "Description", "type": "text"},
            {"name": "assigned_date", "label": "Assigned Date", "type": "entry"},
            {"name": "due_date", "label": "Due Date", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Not Started", "In Progress", "Completed", "On Hold"]},
            {"name": "progress", "label": "Progress (%)", "type": "combobox",
             "options": ["0%", "25%", "50%", "75%", "100%"]},
        ]
        CRUDFrame(tab, table="tasks", fields=fields, title="Assign & Track OJT Tasks",
                  search_field="intern_name",
                  defaults={"assigned_date": now_date, "status": "Not Started",
                            "progress": "0%"}
                  ).pack(fill="both", expand=True)
