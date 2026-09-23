"""
admin_dashboard.py
The full administrator dashboard: sidebar navigation + paged content with
overview, live Client PCs (LAN monitoring & remote control), student/staff
accounts, computer management, inventory, borrowing, maintenance,
announcements, messaging, sessions, audit trail, and OJT intern / task
management.

When launched from the Server it receives the running LabServer instance
and can lock/unlock/logout/pause/resume/restart/shutdown client PCs and
watch their screens live over the LAN.

UI rules (v2.1):
  * sidebar navigation instead of crowded tabs, responsive layouts;
  * small non-blocking toasts for normal events (auto-dismiss);
  * dialogs only for critical confirmations (Restart/Shutdown/Delete);
  * minimize/close NEVER logs the admin out - only the Logout button does;
  * every page auto-refreshes (no manual refresh required).
"""

import io
import base64
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

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

# sidebar entries: (page_key, label) - None key = section header
MENU = [
    (None, "MONITORING"),
    ("overview", "\U0001f4ca  Overview"),
    ("clients", "\U0001f5a5  Client PCs"),
    ("sessions", "\U0001f552  Sessions"),
    (None, "COMPUTERS"),
    ("computers", "\U0001f4bb  Computers"),
    ("inventory", "\U0001f4e6  Inventory"),
    ("maintenance", "\U0001f527  Maintenance"),
    ("borrow", "\U0001f516  Borrowing"),
    (None, "PEOPLE"),
    ("students", "\U0001f393  Student Accounts"),
    ("staff", "\U0001f464  Staff & Admin"),
    (None, "COMMUNICATION"),
    ("announcements", "\U0001f4e2  Announcements"),
    ("messages", "✉  Messages"),
    (None, "AUDIT"),
    ("activity", "\U0001f9fe  Activity Log"),
    (None, "OJT"),
    ("ojt", "\U0001f468\u200d\U0001f4bb  OJT Interns"),
    ("tasks", "\u2705  OJT Tasks"),
]


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

        self.pages = {}                    # key -> frame
        self.page_refresh = {}             # key -> callable (auto refresh)
        self.current_page = None
        self._sidebar_btns = {}
        self._toasts = []
        self._ctrl_btns = []
        self._after_pump = None
        self._after_refresh = None
        self._after_ui = None

        self.is_admin = self._role() == "admin"
        self.title("Laboratory System - Administrator"
                   if self.is_admin else "Laboratory System - Staff")
        center_window(self, 1280, 780)
        self.minsize(1080, 700)
        self.configure(bg=BG_LIGHT)
        # Closing or minimizing must NOT log the admin out (Phase E/#4-5).
        # Only the Logout button does that.
        self.protocol("WM_DELETE_WINDOW", self.iconify)

        self._build_header()

        self.body = tk.Frame(self, bg=BG_LIGHT)
        self.body.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(self.body, bg=BG_DARK, width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = tk.Frame(self.body, bg=BG_LIGHT)
        self.content.pack(side="left", fill="both", expand=True)

        self._build_menu()

        # ---- pages ----
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
        self._build_announcements_tab()
        self._build_messages_tab()
        self._build_sessions_tab()
        self._build_activity_tab()
        self._build_ojt_tab()
        self._build_tasks_tab()

        self.show_page("overview")
        self._pump()
        self._auto_ui()

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

        tk.Label(header, text="\U0001f5a5  Computer Laboratory Management System",
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

        # Logout is the ONLY way out of the session.
        tk.Button(header, text="  Logout  ", command=self._logout, bg=DANGER,
                  fg="white", relief="flat", font=("Segoe UI", 10, "bold"),
                  cursor="hand2", padx=14, pady=6).pack(side="right", padx=16)
        # Explicit minimize control (window buttons work too).
        tk.Button(header, text=" \u2500 Minimize ", command=self.iconify,
                  bg="#44507a", fg="white", relief="flat",
                  font=("Segoe UI", 10), cursor="hand2",
                  padx=10, pady=6).pack(side="right", padx=(0, 4))

    def _logout(self):
        self.destroy()
        self.on_logout()

    # ------------------------------------------------------------ sidebar
    def _build_menu(self):
        tk.Label(self.sidebar, text="MENU", fg="#8e9bc4", bg=BG_DARK,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16,
                                                    pady=(14, 4))
        for key, label in MENU:
            if key == "clients" and not self.server:
                continue
            if key == "staff" and not self.is_admin:
                continue
            if key is None:
                tk.Label(self.sidebar, text=label, fg="#5f6c94", bg=BG_DARK,
                         font=("Segoe UI", 8, "bold")).pack(
                    anchor="w", padx=16, pady=(12, 2))
            else:
                btn = tk.Button(self.sidebar, text=label, anchor="w",
                                relief="flat", bg=BG_DARK, fg="#c7d2fe",
                                activebackground="#2a3760", activeforeground="white",
                                font=("Segoe UI", 10), cursor="hand2",
                                padx=16, pady=5,
                                command=lambda k=key: self.show_page(k))
                btn.pack(fill="x", padx=6, pady=1)
                self._sidebar_btns[key] = btn

    def _new_page(self, key):
        frame = tk.Frame(self.content, bg=BG_LIGHT)
        self.pages[key] = frame
        return frame

    def show_page(self, key):
        frame = self.pages.get(key)
        if frame is None:
            return
        if self.current_page and self.pages.get(self.current_page):
            self.pages[self.current_page].pack_forget()
        frame.pack(fill="both", expand=True)
        self.current_page = key
        for k, btn in self._sidebar_btns.items():
            active = (k == key)
            btn.config(bg=ACCENT if active else BG_DARK,
                       fg="white" if active else "#c7d2fe")
        fn = self.page_refresh.get(key)
        if fn:
            try:
                fn()
            except Exception:
                pass

    # -------------------------------------------------------------- toast
    def toast(self, text, kind="info", duration=3200):
        """Small non-blocking snackbar that disappears by itself."""
        colors = {"info": ACCENT, "success": SUCCESS, "error": DANGER,
                  "warn": WARN}
        bg = colors.get(kind, ACCENT)
        text = str(text)
        try:
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=bg)
            w = max(280, min(560, 40 + 7 * len(text)))
            tk.Label(win, text=text, bg=bg, fg="white",
                     font=("Segoe UI", 10, "bold"), wraplength=w - 24,
                     justify="left", anchor="w").pack(
                fill="both", expand=True, padx=12, pady=8)

            self._toasts = [t for t in self._toasts if t.winfo_exists()]
            while len(self._toasts) >= 4:
                old = self._toasts.pop(0)
                try:
                    old.destroy()
                except Exception:
                    pass
            idx = len(self._toasts)
            x = self.winfo_rootx() + self.winfo_width() - w - 18
            y = self.winfo_rooty() + 78 + idx * 60
            win.update_idletasks()
            h = win.winfo_reqheight()
            win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
            self._toasts.append(win)
            win.after(duration, lambda: self._kill_toast(win))
        except Exception:
            pass

    def _kill_toast(self, win):
        try:
            if win in self._toasts:
                self._toasts.remove(win)
            if win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------- overview
    def _build_overview_tab(self):
        page = self._new_page("overview")
        tk.Label(page, text="System Overview", font=FONT_TITLE,
                 bg=BG_LIGHT, fg=BG_DARK).pack(anchor="w", padx=16, pady=14)
        self.cards_frame = tk.Frame(page, bg=BG_LIGHT)
        self.cards_frame.pack(fill="both", expand=True, padx=16)
        self.page_refresh["overview"] = self.refresh_overview
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
            card = tk.Frame(self.cards_frame, bg=color, height=92)
            card.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")
            card.grid_propagate(False)
            tk.Label(card, text=str(value), font=("Segoe UI", 24, "bold"),
                     fg="white", bg=color).pack(anchor="w", padx=14, pady=(12, 0))
            tk.Label(card, text=label, font=("Segoe UI", 10), fg="white",
                     bg=color, anchor="w", justify="left").pack(anchor="w", padx=14)
        for c in range(4):
            self.cards_frame.columnconfigure(c, weight=1)

    # ------------------------------------------------- Client PCs (LAN)
    def _build_clients_tab(self):
        page = self._new_page("clients")

        # ---- toolbar ----
        bar = tk.Frame(page, bg=BG_LIGHT)
        bar.pack(fill="x", padx=12, pady=(10, 6))

        info = self.server._lan_ip() if self.server else "?"
        tk.Label(bar, text=f"Server {info}:{self.server.port}   |   "
                           f"Live updates every 2 s (automatic)",
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
        cols = ("pc_name", "status", "user", "ip", "hostname", "cpu", "ram",
                "hb", "online")
        frame = tk.Frame(page, bg=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=12)
        self.client_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                        height=12)
        heads = [("pc_name", "PC Name", 120), ("status", "Status", 105),
                 ("user", "Current User", 130), ("ip", "IP Address", 115),
                 ("hostname", "Hostname", 125), ("cpu", "CPU %", 62),
                 ("ram", "RAM %", 62), ("hb", "Last Seen", 95),
                 ("online", "Online/Offline", 105)]
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
        self.client_tree.bind("<<TreeviewSelect>>", self._on_client_select)

        # ---- control buttons (grid: never cut off at any window size,
        #      disabled until a PC is selected - no "select first" popups) ----
        btns = tk.Frame(page, bg=BG_LIGHT)
        btns.pack(fill="x", padx=12, pady=8)
        controls = [
            ("\U0001f512 Lock", self._lock, DANGER),
            ("\U0001f513 Unlock (Force Login)", self._unlock, SUCCESS),
            ("\u23ce Logout User", self._logout_client, "#dc6803"),
            ("\u23f8 Pause", self._pause, WARN),
            ("\u25b6 Resume", self._resume, SUCCESS),
            ("\U0001f504 Restart", self._restart, "#8b5cf6"),
            ("\u23fb Shutdown", self._shutdown, "#be123c"),
            ("\U0001f4f7 Screenshot", self._screenshot, ACCENT),
            ("\U0001f441 Observe", self._observe, "#0ea5e9"),
        ]
        for i, (text, cmd, color) in enumerate(controls):
            r, c = divmod(i, 5)
            b = tk.Button(btns, text=text, command=cmd, bg=color, fg="white",
                          relief="flat", font=("Segoe UI", 9, "bold"),
                          cursor="hand2", pady=5, state="disabled")
            b.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            self._ctrl_btns.append(b)
        for c in range(5):
            btns.columnconfigure(c, weight=1)

        # ---- inline message row (no dialogs for normal events) ----
        msg_row = tk.Frame(btns, bg=BG_LIGHT)
        msg_row.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        msg_row.columnconfigure(1, weight=1)
        tk.Label(msg_row, text="Message to selected PC:",
                 font=("Segoe UI", 9, "bold"), bg=BG_LIGHT).grid(
            row=0, column=0, sticky="w", padx=(2, 6))
        self.single_msg_var = tk.StringVar()
        ttk.Entry(msg_row, textvariable=self.single_msg_var).grid(
            row=0, column=1, sticky="ew")
        send_btn = tk.Button(msg_row, text="Send", command=self._send_msg,
                             bg="#6366f1", fg="white", relief="flat",
                             font=("Segoe UI", 9, "bold"), cursor="hand2",
                             padx=12, state="disabled")
        send_btn.grid(row=0, column=2, padx=(6, 0))
        self._ctrl_btns.append(send_btn)

        self.client_status_lbl = tk.Label(
            page, text="Select a client PC to enable the controls "
                       "(double-click for a screenshot).",
            font=("Segoe UI", 9), fg="#5a6480", bg=BG_LIGHT, anchor="w")
        self.client_status_lbl.pack(fill="x", padx=14, pady=(0, 8))

        self._refresh_clients()
        self._auto_refresh_clients()

    # ---- client list -------------------------------------------------
    def _on_client_select(self, event=None):
        has = bool(self.client_tree.selection())
        state = "normal" if has else "disabled"
        for b in self._ctrl_btns:
            try:
                b.config(state=state)
            except Exception:
                pass

    def _refresh_clients(self):
        if not hasattr(self, "client_tree") or not self.client_tree.winfo_exists():
            return
        snap = {c["pc_name"]: c for c in self.server.list_clients()}
        # include registered-but-offline PCs from the database
        try:
            conn = get_connection()
            for r in conn.execute(
                    "SELECT pc_name, ip_address, hostname, cpu_percent, "
                    "ram_percent, last_heartbeat, is_online, assigned_to "
                    "FROM computers").fetchall():
                if r["pc_name"] not in snap:
                    snap[r["pc_name"]] = {
                        "pc_name": r["pc_name"], "status": "offline",
                        "logged_in_user": r["assigned_to"] or "",
                        "ip": r["ip_address"] or "",
                        "hostname": r["hostname"] or "",
                        "cpu_percent": r["cpu_percent"] or 0,
                        "ram_percent": r["ram_percent"] or 0,
                        "last_heartbeat": r["last_heartbeat"] or "-",
                        "is_online": r["is_online"] or 0,
                    }
            conn.close()
        except Exception:
            pass

        selected = self._selected_pc(quiet=True)
        for item in self.client_tree.get_children():
            self.client_tree.delete(item)

        status_map = {"locked": "\U0001f512 Locked", "logged_in": "\U0001f7e2 In Session",
                      "paused": "\u23f8 Paused", "maintenance": "\U0001f6e0 Maintenance",
                      "idle": "\U0001f4a4 Idle", "offline": "\u25cb Offline"}
        for name in sorted(snap.keys()):
            c = snap[name]
            online = bool(c.get("is_online"))
            if not online:
                tags = ("offline",)
            elif c.get("status") == "paused":
                tags = ("paused",)
            elif c.get("status") == "logged_in":
                tags = ("online",)
            else:
                tags = ("locked",)
            last = c.get("last_heartbeat", "") or "-"
            if len(str(last)) > 8:            # full datetime -> short time
                last = str(last)[-8:]
            self.client_tree.insert("", "end", tags=tags, values=(
                c.get("pc_name", ""),
                status_map.get(c.get("status"), c.get("status", "")),
                c.get("logged_in_user") or "",
                c.get("ip", ""),
                c.get("hostname", ""),
                f"{c.get('cpu_percent', 0):.0f}",
                f"{c.get('ram_percent', 0):.0f}",
                last,
                "\u25cf Online" if online else "\u25cb Offline",
            ))
        if selected:
            for item in self.client_tree.get_children():
                if self.client_tree.item(item, "values")[0] == selected:
                    self.client_tree.selection_set(item)
                    break
        self._on_client_select()

    def _auto_refresh_clients(self):
        if not self.winfo_exists():
            return
        self._collapse_timer("_after_refresh")
        self._refresh_clients()
        self._pump_results()
        try:
            self._after_refresh = self.after(2000, self._auto_refresh_clients)
        except Exception:
            self._after_refresh = None

    def _selected_pc(self, quiet=False):
        sel = self.client_tree.selection() if hasattr(self, "client_tree") else ()
        if not sel:
            if not quiet:
                self.toast("Select a client PC first.", "warn")
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
                txt = f"{label} → {pc}: " + ("done" if ok
                                             else (resp.get("error") or "failed"))
                if hasattr(self, "client_status_lbl"):
                    self.client_status_lbl.config(
                        text=txt, fg=SUCCESS if ok else DANGER)
                # non-blocking notification for every command result
                self.toast(txt, "success" if ok else "error")
                if label == "Screenshot" and ok:
                    self._open_viewer(f"Screenshot · {pc}",
                                      resp.get("data", {}), single=True)
        except queue.Empty:
            pass
        # server events (screen frames, client online/offline ...)
        try:
            while True:
                kind, data = self.server_events.get_nowait()
                if kind == "screen_frame" and self._viewer_alive():
                    self._viewer_show(data.get("data", {}), data.get("pc_name", ""))
                elif kind == "client_online":
                    self.toast(f"{data.get('pc_name', '?')} is online", "info")
                elif kind == "client_offline":
                    self.toast(f"{data.get('pc_name', '?')} went offline", "warn")
        except queue.Empty:
            pass

    def _collapse_timer(self, attr):
        """Cancel a pending Tk timer stored in `attr` (see ClientApp) - keeps
        every periodic refresh to a single chain so nothing can fire after
        this window is destroyed."""
        aid = getattr(self, attr, None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            setattr(self, attr, None)

    def _pump(self):
        if not self.winfo_exists():
            return
        self._collapse_timer("_after_pump")
        self._pump_results()
        try:
            self._after_pump = self.after(250, self._pump)
        except Exception:
            self._after_pump = None

    def _auto_ui(self):
        """Periodic refresh of whichever page is visible (Phase E/#3, #6)."""
        if not self.winfo_exists():
            return
        self._collapse_timer("_after_ui")
        fn = self.page_refresh.get(self.current_page)
        if fn:
            try:
                fn()
            except Exception:
                pass
        try:
            self._after_ui = self.after(3000, self._auto_ui)
        except Exception:
            self._after_ui = None

    def destroy(self):
        """Cancel pending Tk timers before teardown."""
        for attr in ("_after_pump", "_after_refresh", "_after_ui"):
            after_id = getattr(self, attr, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        for t in list(getattr(self, "_toasts", [])):
            try:
                t.destroy()
            except Exception:
                pass
        self._toasts = []
        # stop any live screen observation owned by this window
        if getattr(self, "_observing", None) and getattr(self, "server", None):
            try:
                self.server.stop_screen_observe(self._observing,
                                                self._admin_name())
            except Exception:
                pass
            self._observing = None
        super().destroy()

    # ---- individual controls -------------------------------------------
    def _lock(self):
        # no popup dialog - a normal event confirmed by toast
        self._run_cmd("Lock", self.server.lock_client,
                      msg="Locked by the administrator.",
                      admin=self._admin_name())

    def _unlock(self):
        # Admin Force Login: restores the open session without credentials
        self._run_cmd("Unlock/Force Login", self.server.unlock_client,
                      admin=self._admin_name())

    def _logout_client(self):
        self._run_cmd("Logout", self.server.logout_client,
                      admin=self._admin_name())

    def _pause(self):
        self._run_cmd("Pause", self.server.pause_client, admin=self._admin_name(),
                      seconds=0, msg="Paused by administrator. Please wait.")

    def _resume(self):
        self._run_cmd("Resume", self.server.resume_client,
                      admin=self._admin_name())

    def _restart(self):
        pc = self._selected_pc()
        if not pc:
            return
        # critical confirmation dialog (allowed - destructive action)
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
        # critical confirmation dialog (allowed - destructive action)
        if not messagebox.askyesno("Shutdown client",
                                   f"Shut down {pc}?", parent=self):
            return
        self._run_cmd("Shutdown", self.server.shutdown_client, pc=pc,
                      admin=self._admin_name())

    def _send_msg(self):
        pc = self._selected_pc()
        if not pc:
            return
        text = self.single_msg_var.get().strip()
        if not text:
            self.toast("Type a message first.", "warn")
            return
        self.single_msg_var.set("")
        self._run_cmd("Message", self.server.send_client_message, pc=pc,
                      text=text, admin=self._admin_name())

    def _broadcast(self):
        text = self.broadcast_var.get().strip()
        if not text:
            self.toast("Type a broadcast message first.", "warn")
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
            self.toast(f"Stopped observing {pc}", "info")
            return
        if self._observing:
            self.server.stop_screen_observe(self._observing, self._admin_name())
        ok = self.server.start_screen_observe(pc, self._admin_name())
        if ok:
            self._observing = pc
            self._open_viewer(f"Live Screen · {pc}", {}, single=False)
            self.client_status_lbl.config(text=f"Observing {pc} live…", fg="#0ea5e9")
            self.toast(f"Observing {pc} live", "info")
        else:
            self.client_status_lbl.config(text=f"{pc} is offline", fg=DANGER)
            self.toast(f"{pc} is offline", "error")

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
            center_window(win, 1150, 780)
            win.minsize(900, 620)
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
        """Decode a base64 JPEG frame and fit it to the viewer window so the
        preview is large and readable at any window size (Phase G/#2)."""
        if not self._viewer_alive():
            return
        img_b64 = data.get("image") if isinstance(data, dict) else None
        if not img_b64:
            return
        try:
            from PIL import Image, ImageTk
            raw = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(raw))
            # scale the frame down only if it does not fit the viewer
            avail_w = self._viewer.winfo_width() - 24
            avail_h = self._viewer.winfo_height() - 24
            if avail_w < 100:
                avail_w = 1150 - 24
            if avail_h < 100:
                avail_h = 780 - 24
            if img.width > avail_w or img.height > avail_h:
                resample = getattr(Image, "Resampling", Image)
                img.thumbnail((avail_w, avail_h), resample.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._viewer.label.config(image=photo, text="")
            self._viewer.label.image = photo          # keep a reference
            if pc_name or data.get("pc_name"):
                self._viewer.title(f"Screen · {pc_name or data.get('pc_name')}")
        except Exception as e:
            self._viewer.label.config(text=f"Frame decode error: {e}")

    # --------------------------------------------------------- accounts
    def _on_account_change(self, action, data):
        """Account add/update/delete hook: automatically logs the user out
        of any active client session (Phase I/#1)."""
        data = data or {}
        sid_new = str(data.get("student_id") or "").strip()
        sid_old = str(data.get("_old_student_id") or "").strip() or sid_new

        if action in ("update", "delete"):
            targets = {t for t in (sid_new, sid_old) if t}
            total = 0
            if self.server:
                for sid in targets:
                    try:
                        res = self.server.force_logout_user(
                            sid, admin=self._admin_name(),
                            reason=f"account {action}")
                        total += res.get("forced", 0)
                    except Exception:
                        pass
            if total:
                self.toast(f"Account {action}: logged out {total} active "
                           f"session(s)", "warn")
            else:
                self.toast(f"Account {action}: saved (no active sessions)",
                           "success")
        else:
            self.toast("Account created", "success")
        # sessions page refreshes automatically via the 3 s UI refresh

    def _build_students_tab(self):
        page = self._new_page("students")
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
        UserCRUDFrame(page, table="users", fields=fields,
                      title="Manage Student Accounts",
                      fixed_values={"role": "student"},
                      where_clause="role='student'",
                      search_field="full_name", defaults={"status": "Active"},
                      on_change=self._on_account_change
                      ).pack(fill="both", expand=True)

    def _build_staff_tab(self):
        page = self._new_page("staff")
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
        UserCRUDFrame(page, table="users", fields=fields,
                      title="Manage Staff & Administrator Accounts",
                      where_clause="role IN ('staff','admin')",
                      search_field="full_name",
                      defaults={"status": "Active", "role": "staff"},
                      on_change=self._on_account_change
                      ).pack(fill="both", expand=True)

    # -------------------------------------------------------- computers
    def _build_computers_tab(self):
        """Simplified computer management: only the 8 useful live fields
        (PC Name, IP, Hostname, Online/Offline, Current User, CPU, RAM,
        Last Seen) - no hardware specification clutter (Phase H/#9)."""
        page = self._new_page("computers")
        tk.Label(page, text="Manage Computers", font=FONT_HEADER,
                 bg=BG_LIGHT, fg=BG_DARK).pack(anchor="w", padx=14, pady=(12, 4))

        # ---- form ----
        form = ttk.LabelFrame(page, text="Computer details")
        form.pack(fill="x", padx=14, pady=4)
        row = tk.Frame(form)
        row.pack(fill="x", padx=10, pady=8)

        tk.Label(row, text="PC Name:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.comp_pc_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.comp_pc_var, width=18).grid(
            row=0, column=1, padx=(0, 12))

        tk.Label(row, text="Location:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.comp_loc_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.comp_loc_var, width=18).grid(
            row=0, column=3, padx=(0, 12))

        tk.Label(row, text="Status:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.comp_status_var = tk.StringVar(value="Available")
        ttk.Combobox(row, textvariable=self.comp_status_var, width=14,
                     state="readonly", values=["Available", "In Use",
                                               "Maintenance", "Reserved"]).grid(
            row=0, column=5, padx=(0, 14))

        ttk.Button(row, text="Save", command=self._computer_save).grid(
            row=0, column=6, padx=3)
        ttk.Button(row, text="Delete", command=self._computer_delete).grid(
            row=0, column=7, padx=3)
        ttk.Button(row, text="Clear", command=self._clear_comp_form).grid(
            row=0, column=8, padx=3)

        search_row = tk.Frame(page, bg=BG_LIGHT)
        search_row.pack(fill="x", padx=14, pady=(6, 2))
        tk.Label(search_row, text="Search:", bg=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left")
        self.comp_search_var = tk.StringVar()
        self.comp_search_var.trace_add("write", lambda *a: self._refresh_computers())
        ttk.Entry(search_row, textvariable=self.comp_search_var,
                  width=26).pack(side="left", padx=6)
        tk.Label(search_row, text="Auto-refreshes every 2 s",
                 bg=BG_LIGHT, fg="#5a6480",
                 font=("Segoe UI", 9)).pack(side="right")

        # ---- table (only the 8 useful fields) ----
        cols = ("id", "pc_name", "ip", "hostname", "online", "user", "cpu",
                "ram", "last_seen")
        frame = tk.Frame(page, bg=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.comp_tree = ttk.Treeview(frame, columns=cols, show="headings",
                                      height=12)
        self.comp_tree.heading("id", text="ID")
        self.comp_tree.column("id", width=0, stretch=False, anchor="center")
        heads = [("pc_name", "PC Name", 130, "w"),
                 ("ip", "IP Address", 125, "center"),
                 ("hostname", "Hostname", 150, "w"),
                 ("online", "Online/Offline", 110, "center"),
                 ("user", "Current User", 140, "w"),
                 ("cpu", "CPU %", 70, "center"),
                 ("ram", "RAM %", 70, "center"),
                 ("last_seen", "Last Seen", 110, "center")]
        for cid, text, w, anchor in heads:
            self.comp_tree.heading(cid, text=text)
            self.comp_tree.column(cid, width=w, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.comp_tree.yview)
        self.comp_tree.configure(yscrollcommand=vsb.set)
        self.comp_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.comp_tree.tag_configure("online", foreground="#1a7f37")
        self.comp_tree.tag_configure("offline", foreground=OFFLINE)
        self.comp_tree.bind("<<TreeviewSelect>>", self._on_comp_select)

        self._comp_selected_id = None
        self.page_refresh["computers"] = self._refresh_computers
        self._refresh_computers()

    def _refresh_computers(self):
        if not hasattr(self, "comp_tree") or not self.comp_tree.winfo_exists():
            return
        selected_id = self._comp_selected_id
        search = (self.comp_search_var.get() if hasattr(self, "comp_search_var")
                  else "").strip().lower()

        db_rows, by_name = {}, {}
        try:
            conn = get_connection()
            for r in conn.execute(
                    "SELECT id, pc_name, ip_address, hostname, status, "
                    "assigned_to, cpu_percent, ram_percent, last_heartbeat, "
                    "is_online FROM computers ORDER BY pc_name").fetchall():
                d = dict(r)
                db_rows[d["pc_name"]] = d
                by_name[d["pc_name"]] = d
            conn.close()
        except Exception:
            pass

        live = {}
        if self.server:
            live = {c["pc_name"]: c for c in self.server.list_clients()}
        for name in live:
            by_name.setdefault(name, {"pc_name": name})

        for item in self.comp_tree.get_children():
            self.comp_tree.delete(item)

        for name in sorted(by_name.keys()):
            db = by_name[name] if name in db_rows else {}
            lv = live.get(name, {})
            if search and search not in name.lower():
                continue
            online = bool(lv.get("is_online")) or bool(db.get("is_online"))
            user = (lv.get("logged_in_user")
                    or (db.get("assigned_to") if db.get("status") == "In Use" else "")
                    or "")
            cpu = lv.get("cpu_percent", db.get("cpu_percent") or 0) or 0
            ram = lv.get("ram_percent", db.get("ram_percent") or 0) or 0
            last = lv.get("last_heartbeat") or db.get("last_heartbeat") or "-"
            if len(str(last)) > 8:
                last = str(last)[-8:]
            tags = ("online",) if online else ("offline",)
            self.comp_tree.insert("", "end", tags=tags, values=(
                db.get("id", ""),
                name,
                lv.get("ip") or db.get("ip_address") or "",
                lv.get("hostname") or db.get("hostname") or "",
                "\u25cf Online" if online else "\u25cb Offline",
                user,
                f"{float(cpu):.0f}",
                f"{float(ram):.0f}",
                last,
            ))
        if selected_id:
            for item in self.comp_tree.get_children():
                if str(self.comp_tree.item(item, "values")[0]) == str(selected_id):
                    self.comp_tree.selection_set(item)
                    break

    def _on_comp_select(self, event=None):
        sel = self.comp_tree.selection()
        if not sel:
            return
        vals = self.comp_tree.item(sel[0], "values")
        self._comp_selected_id = vals[0] or None
        self.comp_pc_var.set(vals[1])
        # location/status come from the DB row
        try:
            conn = get_connection()
            row = conn.execute("SELECT location, status FROM computers WHERE id=?",
                               (self._comp_selected_id,)).fetchone()
            conn.close()
            self.comp_loc_var.set(row["location"] if row else "")
            self.comp_status_var.set(row["status"] if row else "Available")
        except Exception:
            pass

    def _clear_comp_form(self):
        self._comp_selected_id = None
        self.comp_pc_var.set("")
        self.comp_loc_var.set("")
        self.comp_status_var.set("Available")
        self.comp_tree.selection_remove(self.comp_tree.selection())

    def _computer_save(self):
        name = self.comp_pc_var.get().strip()
        if not name:
            self.toast("PC name is required.", "warn")
            return
        loc = self.comp_loc_var.get().strip()
        status = self.comp_status_var.get() or "Available"
        conn = get_connection()
        try:
            if self._comp_selected_id:
                conn.execute(
                    "UPDATE computers SET pc_name=?, location=?, status=? WHERE id=?",
                    (name, loc, status, self._comp_selected_id))
                msg = f"Computer {name} updated"
            else:
                conn.execute(
                    "INSERT INTO computers (pc_name, location, status) VALUES (?,?,?)",
                    (name, loc, status))
                msg = f"Computer {name} added"
            conn.commit()
            conn.close()
        except Exception as e:
            conn.close()
            self.toast(f"Could not save computer: {e}", "error")
            return
        self._clear_comp_form()
        self._refresh_computers()
        self.toast(msg, "success")

    def _computer_delete(self):
        if not self._comp_selected_id:
            self.toast("Select a computer first.", "warn")
            return
        name = self.comp_pc_var.get() or "?"
        # critical confirmation (destructive action)
        if not messagebox.askyesno("Delete computer",
                                   f"Delete {name}?", parent=self):
            return
        conn = get_connection()
        conn.execute("DELETE FROM computers WHERE id=?", (self._comp_selected_id,))
        conn.commit()
        conn.close()
        self._clear_comp_form()
        self._refresh_computers()
        self.toast(f"Computer {name} deleted", "success")

    # -------------------------------------------------------- inventory
    def _build_inventory_tab(self):
        page = self._new_page("inventory")
        fields = [
            {"name": "item_name", "label": "Item Name", "type": "entry", "required": True},
            {"name": "category", "label": "Category", "type": "entry"},
            {"name": "quantity", "label": "Quantity", "type": "entry"},
            {"name": "condition_status", "label": "Condition", "type": "combobox",
             "options": ["New", "Good", "Fair", "Damaged", "For Disposal"]},
            {"name": "location", "label": "Location", "type": "entry"},
            {"name": "remarks", "label": "Remarks", "type": "entry"},
        ]
        CRUDFrame(page, table="inventory", fields=fields, title="Computer Lab Inventory",
                  search_field="item_name").pack(fill="both", expand=True)

    # ----------------------------------------------------------- borrow
    def _build_borrow_tab(self):
        page = self._new_page("borrow")
        fields = [
            {"name": "student_id", "label": "Student ID", "type": "entry", "required": True},
            {"name": "item_name", "label": "Item Name", "type": "entry", "required": True},
            {"name": "quantity", "label": "Quantity", "type": "entry"},
            {"name": "borrow_date", "label": "Borrow Date", "type": "entry"},
            {"name": "return_date", "label": "Return Date", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Pending Approval", "Approved", "Borrowed", "Returned", "Overdue"]},
        ]
        CRUDFrame(page, table="borrow_records", fields=fields,
                  title="Equipment Borrowing Records",
                  search_field="student_id",
                  defaults={"borrow_date": now_date, "status": "Pending Approval"}
                  ).pack(fill="both", expand=True)

    # ------------------------------------------------------- maintenance
    def _build_maintenance_tab(self):
        page = self._new_page("maintenance")
        fields = [
            {"name": "asset_name", "label": "PC / Equipment", "type": "entry", "required": True},
            {"name": "issue", "label": "Issue Description", "type": "entry", "required": True},
            {"name": "date_reported", "label": "Date Reported", "type": "entry"},
            {"name": "date_resolved", "label": "Date Resolved", "type": "entry"},
            {"name": "technician", "label": "Technician", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Pending", "In Progress", "Resolved"]},
        ]
        CRUDFrame(page, table="maintenance", fields=fields, title="Maintenance Records",
                  search_field="asset_name",
                  defaults={"date_reported": now_date, "status": "Pending"}
                  ).pack(fill="both", expand=True)

    # (Lab Attendance page intentionally removed - Phase K/#14)

    # ---------------------------------------------------- announcements
    def _build_announcements_tab(self):
        page = self._new_page("announcements")
        fields = [
            {"name": "title", "label": "Title", "type": "entry", "required": True},
            {"name": "message", "label": "Message", "type": "text", "required": True},
            {"name": "posted_by", "label": "Posted By", "type": "entry"},
            {"name": "date_posted", "label": "Date Posted", "type": "entry"},
        ]
        CRUDFrame(page, table="announcements", fields=fields,
                  title="Post Laboratory Announcements",
                  defaults={"posted_by": self._adm("full_name", "admin"),
                            "date_posted": now_date}
                  ).pack(fill="both", expand=True)

    # -------------------------------------------------------- messages
    def _build_messages_tab(self):
        page = self._new_page("messages")
        fields = [
            {"name": "sender", "label": "From", "type": "entry"},
            {"name": "receiver", "label": "To (Student ID)", "type": "entry",
             "required": True},
            {"name": "message", "label": "Message", "type": "text", "required": True},
            {"name": "timestamp", "label": "Sent At", "type": "entry"},
            {"name": "is_read", "label": "Read?", "type": "combobox",
             "options": ["Yes", "No"]},
        ]
        CRUDFrame(page, table="messages", fields=fields, title="Messages with Students",
                  search_field="receiver",
                  defaults={"sender": self._admin_name(),
                            "timestamp": now_datetime, "is_read": "No"}
                  ).pack(fill="both", expand=True)

    # --------------------------------------------------------- sessions
    def _build_sessions_tab(self):
        page = self._new_page("sessions")
        fields = [
            {"name": "session_id", "label": "Session ID", "type": "entry"},
            {"name": "pc_name", "label": "PC Name", "type": "entry"},
            {"name": "student_id", "label": "Student ID", "type": "entry"},
            {"name": "full_name", "label": "Full Name", "type": "entry"},
            {"name": "login_time", "label": "Login Time", "type": "entry"},
            {"name": "logout_time", "label": "Logout Time", "type": "entry"},
            {"name": "duration_seconds", "label": "Duration (s)", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Completed", "Disconnected", "Forced Logout"]},
        ]
        frame = CRUDFrame(page, table="client_sessions", fields=fields,
                          title="User Session History (Server-recorded)",
                          search_field="student_id", order_by="id DESC",
                          allow_delete=False)
        frame.pack(fill="both", expand=True)
        # sessions auto-refresh so forced logouts show up immediately
        self.page_refresh["sessions"] = frame.refresh

    # ------------------------------------------------------ activity log
    def _build_activity_tab(self):
        """Audit trail with filtering/searching over both client events and
        server commands (Phase J/#10-14)."""
        page = self._new_page("activity")
        tk.Label(page, text="Activity Log / Audit Trail", font=FONT_HEADER,
                 bg=BG_LIGHT, fg=BG_DARK).pack(anchor="w", padx=14, pady=(12, 4))

        # ---- filter bar ----
        bar = tk.Frame(page, bg=BG_LIGHT)
        bar.pack(fill="x", padx=14, pady=(0, 4))

        self.audit_mode = "events"
        self._audit_btns = {}
        for key, text in (("events", "Client / User Events"),
                          ("commands", "Server Commands")):
            b = tk.Button(bar, text=text, relief="flat", cursor="hand2",
                          font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                          command=lambda k=key: self._set_audit_mode(k))
            b.pack(side="left", padx=(0, 6))
            self._audit_btns[key] = b

        tk.Label(bar, text="Search:", bg=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_search_var = tk.StringVar()
        self.audit_search_var.trace_add("write", lambda *a: self._refresh_activity())
        ttk.Entry(bar, textvariable=self.audit_search_var, width=24).pack(side="left")

        tk.Label(bar, text="Date (YYYY-MM-DD):", bg=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_date_var = tk.StringVar()
        self.audit_date_var.trace_add("write", lambda *a: self._refresh_activity())
        ttk.Entry(bar, textvariable=self.audit_date_var, width=12).pack(side="left")

        tk.Label(bar, text="Result:", bg=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_status_var = tk.StringVar(value="All")
        self.audit_status_cb = ttk.Combobox(
            bar, textvariable=self.audit_status_var, width=10, state="readonly",
            values=["All", "Sent", "Done", "Failed"])
        self.audit_status_cb.pack(side="left")
        self.audit_status_cb.bind("<<ComboboxSelected>>",
                                  lambda e: self._refresh_activity())

        tk.Button(bar, text="Refresh", command=self._refresh_activity,
                  bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=4).pack(
            side="right")

        # ---- table ----
        frame = tk.Frame(page, bg=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.audit_tree = ttk.Treeview(frame, show="headings")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.audit_tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.audit_tree.xview)
        self.audit_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.audit_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self._set_audit_mode("events")
        self.page_refresh["activity"] = self._refresh_activity

    def _set_audit_mode(self, mode):
        self.audit_mode = mode
        for key, b in self._audit_btns.items():
            active = (key == mode)
            b.config(bg=ACCENT if active else "#dde3f0",
                     fg="white" if active else BG_DARK)
        # (re)create columns
        for col in self.audit_tree["columns"]:
            self.audit_tree.heading(col, text="")
        if mode == "events":
            cols = ("timestamp", "admin_user", "action", "target", "details")
            heads = [("timestamp", "Timestamp", 150), ("admin_user", "User", 120),
                     ("action", "Action", 160), ("target", "Target PC", 130),
                     ("details", "Details / Result", 460)]
            self.audit_status_cb.state(["disabled"])
        else:
            cols = ("created_at", "admin_user", "command_type", "target_pc",
                    "status", "result")
            heads = [("created_at", "Timestamp", 150), ("admin_user", "Admin", 120),
                     ("command_type", "Command", 150), ("target_pc", "Target PC", 130),
                     ("status", "Result", 90), ("result", "Details", 380)]
            self.audit_status_cb.state(["!disabled"])
        self.audit_tree.configure(columns=cols)
        for cid, text, w in heads:
            self.audit_tree.heading(cid, text=text)
            self.audit_tree.column(cid, width=w, anchor="w")
        self.audit_tree.tag_configure("failed", foreground=DANGER)
        self.audit_tree.tag_configure("done", foreground="#1a7f37")
        self._refresh_activity()

    def _refresh_activity(self):
        if not hasattr(self, "audit_tree") or not self.audit_tree.winfo_exists():
            return
        search = self.audit_search_var.get().strip()
        date = self.audit_date_var.get().strip()
        conn = get_connection()
        try:
            if self.audit_mode == "events":
                q = ("SELECT admin_user, action, target, details, timestamp "
                     "FROM admin_activity_log")
                clauses, params = [], []
                if search:
                    clauses.append("(admin_user LIKE ? OR action LIKE ? OR "
                                   "target LIKE ? OR details LIKE ?)")
                    params += [f"%{search}%"] * 4
                if date:
                    clauses.append("timestamp LIKE ?")
                    params.append(f"{date}%")
                if clauses:
                    q += " WHERE " + " AND ".join(clauses)
                q += " ORDER BY id DESC LIMIT 400"
                rows = conn.execute(q, params).fetchall()
                data = [(r["timestamp"], r["admin_user"], r["action"],
                         r["target"], r["details"]) for r in rows]
            else:
                q = ("SELECT admin_user, command_type, target_pc, status, "
                     "result, created_at FROM client_commands")
                clauses, params = [], []
                if search:
                    clauses.append("(admin_user LIKE ? OR command_type LIKE ? OR "
                                   "target_pc LIKE ? OR result LIKE ?)")
                    params += [f"%{search}%"] * 4
                if date:
                    clauses.append("created_at LIKE ?")
                    params.append(f"{date}%")
                st = self.audit_status_var.get()
                if st and st != "All":
                    clauses.append("status = ?")
                    params.append(st)
                if clauses:
                    q += " WHERE " + " AND ".join(clauses)
                q += " ORDER BY id DESC LIMIT 400"
                rows = conn.execute(q, params).fetchall()
                data = [(r["created_at"], r["admin_user"], r["command_type"],
                         r["target_pc"], r["status"],
                         (r["result"] or "")[:300]) for r in rows]
        finally:
            conn.close()

        for item in self.audit_tree.get_children():
            self.audit_tree.delete(item)
        for row in data:
            tags = ()
            if self.audit_mode == "commands":
                tags = ("failed",) if row[4] == "Failed" else \
                       ("done",) if row[4] == "Done" else ()
            self.audit_tree.insert("", "end", values=row, tags=tags)

    # ---------------------------------------------------------- ojt tab
    def _build_ojt_tab(self):
        page = self._new_page("ojt")
        fields = [
            {"name": "name", "label": "Intern Name", "type": "entry", "required": True},
            {"name": "skills", "label": "Skills", "type": "entry"},
            {"name": "contact", "label": "Contact", "type": "entry"},
            {"name": "school", "label": "School / University", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Completed", "Inactive"]},
        ]
        CRUDFrame(page, table="ojt_interns", fields=fields, title="Manage OJT Interns",
                  search_field="name", defaults={"status": "Active"}
                  ).pack(fill="both", expand=True)

    # --------------------------------------------------------- tasks tab
    def _build_tasks_tab(self):
        page = self._new_page("tasks")
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
        CRUDFrame(page, table="tasks", fields=fields, title="Assign & Track OJT Tasks",
                  search_field="intern_name",
                  defaults={"assigned_date": now_date, "status": "Not Started",
                            "progress": "0%"}
                  ).pack(fill="both", expand=True)
