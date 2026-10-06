"""
admin_dashboard.py
The full administrator dashboard: sidebar navigation + paged content with
overview, live Client PCs (LAN monitoring & remote control), student/staff
accounts, computer management, inventory, borrowing, maintenance,
announcements, messaging, sessions, and audit trail.

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

import os
import io
import time
import base64
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont

from database import get_connection
from crud_frame import (CRUDFrame, UserCRUDFrame, InventoryCRUDFrame,
                        WebsiteRulesFrame, SessionsFrame, AccountsFrame,
                        StaffAccountsFrame)
from utils import (now_date, now_time, now_datetime, FONT_TITLE,
                   FONT_HEADER, center_window, set_app_icon, get_logo,
                   STATUS_LABELS, STATUS_COLORS, STATUS_ORDER, status_key,
                   get_status_icon, BORDER, SUBTLE, MUTED, TEXT, TEXT_LIGHT,
                   FONT_SMALL, FONT_BODY, FONT_CARD_NAME, FONT_STAT_NUM,
                   STAT_TILE_H, DETAIL_W, SEARCH_HINT, CARD,
                   BG_DARK, BG_LIGHT, ACCENT, ACCENT_DARK, SUCCESS, DANGER,
                   WARN, OFFLINE, ORANGE, PURPLE, CRIMSON, SKY,
                    configure_theme)
import customtkinter as ctk
from components import CardFrame, PaddedFrame

# ---------------------------------------------------------------------------
# Audit Trail taxonomy: groups every action the system can record so the
# Type filter can show one family at a time, plus the human-readable text
# shown in the Action column.  Category = {exact actions}, (action prefixes).
# ---------------------------------------------------------------------------
AUDIT_TAXONOMY = {
    "Login / Logout": (
        {"login_success", "login_failed", "client_login", "client_relogin",
         "client_login_failed", "client_logout", "client_admin_login",
         "client_admin_logout", "password_changed", "session_start_blocked",
         "force_logout"},
        ()),
    "Connection": (
        {"network_disconnect", "client_reconnect", "client_crash",
         "client_closed"},
        ()),
    "Security": (
        {"hotkey_force_unlock", "webfilter_change",
         # P4: a batch that would have driven a PC for someone who is not
         # an administrator - refused, and kept as the security event it is
         # rather than as a piece of monitoring.
         "remote_input_reject",
         # an attempt to push a file to a lab PC that the Server refused
         # (non-administrator role) - same reasoning as above.
         "send_file_refuse"},
        ()),
    "Website Access": (
        {"web_access_detected", "web_access_unresolved",
         "web_browser_closed", "web_close_skipped", "web_close_failed"},
        ()),
    "Power": (
        {"cmd_restart", "cmd_shutdown"},
        ("command:cmd_restart", "command:cmd_shutdown")),
    "Monitoring": (
        {"screenshot", "screen_observe_start", "screen_observe_stop",
         "remote_control_start", "remote_control_stop",
         "screen_share_start", "screen_share_stop"},
        ()),
    "Admin Commands": (
        set(), ("command:", "BULK_")),
}

AUDIT_LABELS = {
    "login_success": "Server Login",
    "login_failed": "Server Login Failed",
    "client_login": "Client Login",
    "client_relogin": "Client Re-Login",
    "client_login_failed": "Client Login Failed",
    "client_logout": "Client Logout",
    "client_admin_login": "Admin Login (Client PC)",
    "client_admin_logout": "Admin Logout (Client PC)",
    "password_changed": "Password Changed",
    "session_start_blocked": "Session Blocked",
    "force_logout": "Forced Logout",
    "network_disconnect": "Network Disconnect",
    "client_reconnect": "Client Reconnected",
    "client_crash": "Client Crash",
    "client_closed": "Client Window Closed",
    "hotkey_force_unlock": "Emergency Hotkey Unlock",
    "webfilter_change": "Website Filter Changed",
    "cmd_restart": "Restart Result",
    "cmd_shutdown": "Shutdown Result",
    "screenshot": "Screenshot Captured",
    "screen_observe_start": "Screen Observe Start",
    "screen_observe_stop": "Screen Observe Stop",
    "screen_share_start": "Screen Share Start",
    "screen_share_stop": "Screen Share Stop",
    "remote_control_start": "Remote Control Start",
    "remote_control_stop": "Remote Control Stop",
    "remote_input_reject": "Remote Input Refused",
    "send_file_refuse": "Send File Refused",
    "web_access_detected": "Website Access Detected",
    "web_access_unresolved": "Website Access Unresolved",
    "web_browser_closed": "Browser Closed (Website Access)",
    "web_close_skipped": "Website Access Close Skipped",
    "web_close_failed": "Website Access Close Failed",
}


def audit_action_label(action):
    """Human-readable Action text (the raw action when no label is known)."""
    if action in AUDIT_LABELS:
        return AUDIT_LABELS[action]
    if action.startswith("command:"):
        return "Command: " + action[8:]
    if action.startswith("BULK_"):
        return "Bulk " + action[5:].replace("_", " ").title()
    return action


def audit_action_category(action):
    """Category name of an action ('Other' when outside the taxonomy)."""
    for _cat, (exact, prefixes) in AUDIT_TAXONOMY.items():
        if action in exact or any(action.startswith(p) for p in prefixes):
            return _cat
    return "Other"


# sidebar entries: (page_key, label) - None key = section header.
# Grouped navigation (spec: MAIN / ACCOUNTS / MANAGEMENT / SYSTEM) - the
# Internet Cafe PC management dashboard keeps every page reachable, but
# related pages now sit under their own heading instead of one flat list.
MENU = [
    (None, "MAIN"),
    ("overview", "\U0001f4ca  Dashboard"),
    ("clients", "\U0001f5a5  Client PCs"),
    ("sessions", "\U0001f552  Sessions"),
    (None, "ACCOUNTS"),
    ("students", "\U0001f393  Accounts"),
    ("staff", "\U0001f464  Staff & Admin"),
    (None, "MANAGEMENT"),
    ("inventory", "\U0001f4e6  Inventory"),
    ("websites", "\U0001f310  Website Access"),
    ("messages", "\u2709  Messages"),
    ("announcements", "\U0001f4e2  Announcements"),
    (None, "SYSTEM"),
    ("activity", "\U0001f5c2  Audit Trail"),
    ("maintenance", "\U0001f527  Maintenance"),
    ("settings", "\u2699  Settings"),
]
# ... plus a collapsed "More" group at the bottom: the two legacy pages
# stay fully functional in the code and reachable in the UI, but out of
# the way by default (spec: unrelated features are hidden, not removed).
MORE_MENU = [
    ("computers", "\U0001f4bb  Computers"),
    ("borrow", "\U0001f516  Borrowing"),
]
MORE_KEYS = tuple(k for k, _ in MORE_MENU)


class AdminDashboard(ctk.CTkToplevel):
    def __init__(self, master, admin_row, on_logout,
                 server=None, server_events=None):
        # Dark mode has to be set before the first CustomTkinter widget is
        # built, so a window opened straight from a test harness or a
        # frozen exe gets the same palette as every other entry point.
        configure_theme()
        super().__init__(master)
        self.admin = admin_row
        self.on_logout = on_logout
        self.server = server
        self.server_events = server_events or queue.Queue()
        self.results = queue.Queue()
        self._viewer = None
        self._observing = None
        # Task 5: remote-control state.  `_remote_pc` is the PC currently
        # being driven (None = no session), `_remote_streaming_pc` the PC
        # whose screen stream THIS session opened (Observe owns its own).
        self._remote_pc = None
        self._remote_streaming_pc = None
        self._remote_btn = None
        self._remote_win = None
        self._remote_conn_lbl = None
        self._remote_info_lbl = None

        self.pages = {}                    # key -> frame
        self.page_refresh = {}             # key -> callable (auto refresh)
        self.current_page = None
        self._sidebar_btns = {}
        self._toasts = []
        self._ctrl_btns = []
        self._unlock_btns = []            # M1: Force Unlock never needs a selection
        self._sel_labels = []            # "Selected: N PCs" labels (both pages)
        self.detail_panels = []          # PC / Session details panels
        self._selected_pcs = []          # unified selection (tree + cards)
        self._state_by_name = {}         # last server-pushed PC states
        self._pc_cards = {}              # overview grid: name -> widget info
        self._pc_sig = None              # grid rebuild signature
        self._pc_status_filter = None    # legend chip filter (None = All)
        self._after_pump = None
        self._after_refresh = None
        self._after_ui = None

        role = self._role()
        self.is_admin = role == "admin"
        # P4: the raw role travels with every Observe / Remote call so the
        # Server can enforce ADMINISTRATOR-only itself - this window is
        # trusted for nothing, it only carries who is asking.
        self.role_name = str(role or "")
        self.title({"admin": "Laboratory System - Administrator",
                    "maintenance": "Laboratory System - Maintenance"}.get(
                        role, "Laboratory System - Staff"))
        center_window(self, 1280, 780)
        self.minsize(1080, 700)
        self.configure(fg_color=BG_LIGHT)
        set_app_icon(self)
        # Closing or minimizing must NOT log the admin out (Phase E/#4-5).
        # Only the Logout button does that.
        self.protocol("WM_DELETE_WINDOW", self.iconify)
        # Ctrl+A selects every PC of the current grid (bulk selection)
        self.bind("<Control-a>", self._on_ctrl_a)
        self.bind("<Control-A>", self._on_ctrl_a)

        self._build_header()

        self.body = ctk.CTkFrame(self, fg_color=BG_LIGHT)
        self.body.pack(fill="both", expand=True)
        self.sidebar = ctk.CTkFrame(self.body, fg_color=BG_DARK, width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        # scrollable menu host: every entry (including the More group)
        # stays reachable at any window height - nothing is ever cut off
        self._menu_canvas = ctk.CTkCanvas(self.sidebar, bg=BG_DARK,
                                      highlightthickness=0, width=204)
        self._sb_vsb = ctk.CTkScrollbar(self.sidebar, height=51, orientation="vertical",
                                     command=self._menu_canvas.yview)
        self._menu_canvas.configure(yscrollcommand=self._sb_vsb.set)
        self._sb_vsb.pack(side="right", fill="y")
        self._menu_canvas.pack(side="left", fill="both", expand=True)
        self._menu_host = ctk.CTkFrame(self._menu_canvas, fg_color=BG_DARK)
        self._menu_win = self._menu_canvas.create_window(
            (0, 0), window=self._menu_host, anchor="nw")
        self._menu_host.bind("<Configure>",
                             lambda e: self._menu_canvas.configure(
                                 scrollregion=self._menu_canvas.bbox("all")))
        self._menu_canvas.bind("<Configure>", self._on_menu_canvas_configure)
        wheel = lambda e: self._menu_canvas.yview_scroll(
            -1 * (e.delta // 120), "units")
        for w in (self.sidebar, self._menu_canvas, self._menu_host):
            w.bind("<MouseWheel>", wheel)
        self.content = ctk.CTkFrame(self.body, fg_color=BG_LIGHT)
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
        self._build_websites_tab()
        self._build_announcements_tab()
        self._build_messages_tab()
        self._build_sessions_tab()
        self._build_activity_tab()
        self._build_settings_tab()

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
        # Spec 3 / 11: two compact rows, never one shared lane, so the logo
        # can never be stacked over the title (each row re-flows on its own
        # and the logo keeps its own 30px box):
        #     [LOGO]  Computer Laboratory Management System
        #             [Name · Role] [● Local Network Server :8443]  [Logout]
        header = ctk.CTkFrame(self, fg_color=BG_DARK)
        header.pack(fill="x")
        header.pack_propagate(True)          # size to content: nothing clips

        # --- row 1: official logo + product title -----------------------
        row1 = ctk.CTkFrame(header, fg_color=BG_DARK)
        row1.pack(fill="x", padx=14, pady=(7, 0))

        # Official logo - one source asset for the whole product.
        self._hdr_logo = get_logo(30, self)
        if self._hdr_logo is not None:
            ctk.CTkLabel(row1, image=self._hdr_logo, fg_color=BG_DARK,
                         width=30, height=30).pack(side="left")
            title = "Computer Laboratory Management System"
        else:
            title = "\U0001f5a5  Computer Laboratory Management System"
        ctk.CTkLabel(row1, text=title,
                     text_color="white", fg_color=BG_DARK,
                     font=FONT_TITLE).pack(side="left", padx=(10, 0))

        # --- row 2: who / where, and the only way out -------------------
        row2 = ctk.CTkFrame(header, fg_color=BG_DARK)
        row2.pack(fill="x", padx=14, pady=(3, 7))

        role_text = {"admin": "Administrator",
                     "maintenance": "Maintenance"}.get(self._role(), "Staff")
        ctk.CTkLabel(row2,
                     text=f" {self._adm('full_name', '')} · {role_text} ",
                     text_color="white", fg_color=ACCENT if self.is_admin else "#44507a",
                     font=("Segoe UI", 9, "bold"), padx=8, pady=2).pack(side="left")

        if self.server:
            try:
                lbl = f"Local Network Server ● :{self.server.port}"
            except Exception:
                lbl = "Local Network Server ●"
            status_color = "#7ee787"
        else:
            lbl = "Local Network Server ○"
            status_color = "#e5a83d"
        ctk.CTkLabel(row2, text=lbl, text_color=status_color, fg_color=BG_DARK,
                     font=("Segoe UI", 9, "bold")).pack(side="left", padx=10)

        # Logout is the ONLY way out of the session - there is no Minimize button (X only iconifies, never logs out).
        ctk.CTkButton(row2, text="  Logout  ", command=self._logout, fg_color=DANGER,
                      text_color="white", font=("Segoe UI", 9, "bold"),
                      width=86, height=26, cursor="hand2").pack(side="right")

    def _logout(self):
        self.destroy()
        self.on_logout()

    def _on_menu_canvas_configure(self, event):
        """Keep the menu entries at the sidebar's width and show the
        scrollbar only while the entries don't fit."""
        try:
            self._menu_canvas.itemconfigure(self._menu_win, width=event.width)
        except Exception:
            pass
        self._sync_sidebar_scroll(event.height)

    def _sync_sidebar_scroll(self, avail=None):
        """Scrollbar appears only when the entries overflow the sidebar
        height (expanded More group on small screens)."""
        try:
            if avail is None:
                avail = self._menu_canvas.winfo_height()
            needed = self._menu_host.winfo_reqheight() > avail + 1
            mapped = bool(self._sb_vsb.winfo_ismapped())
            if needed and not mapped:
                self._sb_vsb.pack(side="right", fill="y")
            elif not needed and mapped:
                self._sb_vsb.pack_forget()
                self._menu_canvas.yview_moveto(0)
        except Exception:
            pass

    # ------------------------------------------------------------ sidebar
    def _build_menu(self):
        # The sidebar starts directly with MENU - the duplicated logo /
        # "LABORATORY SYSTEM" branding block is gone (that was a second
        # copy of the same asset the header already shows).  The MENU
        # label absorbs the old block's top spacing so no empty gap is
        # left behind; navigation itself is untouched.
        ctk.CTkLabel(self._menu_host, text="MENU", text_color="#8e9bc4", fg_color=BG_DARK,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16,
                                                    pady=(16, 6))
        for key, label in MENU:
            if key == "clients" and not self.server:
                continue
            if key == "staff" and not self.is_admin:
                continue
            if key is None:
                ctk.CTkLabel(self._menu_host, text=label, text_color="#5f6c94", fg_color=BG_DARK,
                         font=("Segoe UI", 8, "bold")).pack(
                    anchor="w", padx=16, pady=(10, 2))
            else:
                btn = self._make_sidebar_btn(key, label)
                btn.pack(fill="x", padx=6, pady=1)
                self._sidebar_btns[key] = btn

        # collapsed "More" group: legacy pages stay functional + reachable
        self._more_expanded = False
        self._more_toggle = ctk.CTkButton(
            self._menu_host, text=" \u25b8  MORE", anchor="w", 
            fg_color=BG_DARK, text_color="#8e9bc4", hover_color="#2a3760",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2", command=self._toggle_more)
        self._more_toggle.pack(fill="x", padx=6, pady=(10, 1))
        self._more_btns = []
        for key, label in MORE_MENU:
            btn = self._make_sidebar_btn(key, label)
            self._sidebar_btns[key] = btn      # registered, not packed yet
            self._more_btns.append(btn)

    def _make_sidebar_btn(self, key, label):
        return ctk.CTkButton(self._menu_host, text=label, anchor="w",
                         fg_color=BG_DARK, text_color="#c7d2fe",
                         hover_color="#2a3760", 
                         font=("Segoe UI", 10), cursor="hand2",
                         
                         command=lambda k=key: self.show_page(k))

    def _toggle_more(self):
        """Expand / collapse the legacy "More" group (collapsed by default)."""
        self._more_expanded = not self._more_expanded
        if self._more_expanded:
            for b in self._more_btns:
                b.pack(fill="x", padx=6, pady=1)
            self._more_toggle.configure(text=" \u25be  MORE")
        else:
            # leaving a legacy page visible -> return to the dashboard
            if self.current_page in MORE_KEYS:
                self.show_page("overview")
            for b in self._more_btns:
                b.pack_forget()
            self._more_toggle.configure(text=" \u25b8  MORE")
        # reveal the freshly shown entries on short screens
        try:
            self._menu_host.update_idletasks()
            self._sync_sidebar_scroll()
            if self._more_expanded \
                    and self._menu_host.winfo_reqheight() \
                    > self._menu_canvas.winfo_height() + 1:
                self._menu_canvas.yview_moveto(1.0)
            elif not self._more_expanded:
                self._menu_canvas.yview_moveto(0)
        except Exception:
            pass

    def _new_page(self, key):
        frame = ctk.CTkFrame(self.content, fg_color=BG_LIGHT)
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
            btn.configure(fg_color=ACCENT if active else BG_DARK,
                       text_color="white" if active else "#c7d2fe")
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
            win = ctk.CTkToplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(fg_color=bg)
            w = max(280, min(560, 40 + 7 * len(text)))
            ctk.CTkLabel(win, text=text, fg_color=bg,
                     text_color=("#1a1405" if kind == "warn" else "white"),
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
            win.update_idletasks()
            h = win.winfo_reqheight()
            # snackbar: stack upward from the bottom-right corner
            y = (self.winfo_rooty() + self.winfo_height() - h - 18
                 - idx * (h + 6))
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
    CARD_W = 160                      # min card width for the PC grid

    def _build_overview_tab(self):
        """Responsive main dashboard (spec mockup):
        compact stats strip | search/sort toolbar | status filter chips |
        scrollable PC card grid + right-side PC details panel |
        bulk action bar | per-PC results box."""
        page = self._new_page("overview")
        # grid layout: column 0 = main content (absorbs width), row 3 = the
        # PC grid (absorbs height), column 1 = fixed right-side details panel
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)

        # 1) compact stats strip - exactly the 6 useful PC statistics
        #    (Total PCs, Online, In Use, Available, Paused, Locked)
        self.cards_frame = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        self.cards_frame.grid(row=0, column=0, sticky="ew",
                              padx=16, pady=(12, 6))

        # 2) toolbar: search + sort (left) | Select All / Clear (right)
        tb = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        tb.grid(row=1, column=0, sticky="ew", padx=16)
        self.pc_search_var = tk.StringVar()
        self._pc_search_ph = True              # showing the placeholder text
        self.pc_search_var.set(SEARCH_HINT)
        self.pc_search_var.trace_add("write", lambda *a: self._refresh_pc_grid())
        self.pc_search = ctk.CTkEntry(tb, textvariable=self.pc_search_var, width=138)
        self.pc_search.pack(side="left")
        self.pc_search.bind("<FocusIn>", self._search_focus_in)
        self.pc_search.bind("<FocusOut>", self._search_focus_out)
        ctk.CTkLabel(tb, text="Sort:", font=("Segoe UI", 9, "bold"),
                 fg_color=BG_LIGHT).pack(side="left", padx=(10, 4))
        self.pc_sort_var = tk.StringVar(value="Name")
        sort_cb = ctk.CTkComboBox(tb, variable=self.pc_sort_var, width=88,
                               state="readonly", values=["Name", "Status", "CPU %"])
        sort_cb.pack(side="left")
        sort_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_pc_grid())
        ctk.CTkButton(tb, text="Select All", command=self._select_all,
                  fg_color="#dde3f0", text_color=BG_DARK, cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right", padx=(6, 0))
        ctk.CTkButton(tb, text="Clear", command=self._clear_selection,
                  fg_color="#dde3f0", text_color=BG_DARK, cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right")

        # 3) status filter chips - counts update automatically from the
        #    server-pushed states (All + 8 statuses).  The row WRAPS onto
        #    extra lines at narrow widths (_reflow_chips) so no chip is
        #    ever hidden under the right-hand details panel.
        leg = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        leg.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 2))
        self._legend_host = leg
        self._legend_label = ctk.CTkLabel(leg, text="Filter:",
                                      font=("Segoe UI", 9, "bold"), fg_color=BG_LIGHT)
        self._legend_btns = {}
        # width=height=0 -> CTkButton shrinks to its caption the way a
        # tk.Button did.  Its default size (140x28) ignores the text, which
        # would push this tightly wrapped row past the details panel.
        all_btn = ctk.CTkButton(leg, text="All", cursor="hand2",
                            width=0, height=0,
                            font=("Segoe UI", 9, "bold"), 
                            command=lambda: self._set_status_filter(None))
        self._legend_btns[None] = all_btn
        for key in STATUS_ORDER:
            b = ctk.CTkButton(leg, text=STATUS_LABELS.get(key, key.upper()),
                          cursor="hand2",
                          width=0, height=0,
                          font=("Segoe UI", 9, "bold"), 
                          command=lambda k=key: self._set_status_filter(k))
            self._legend_btns[key] = b
        self._chips_sig = None
        leg.bind("<Configure>", self._reflow_chips)

        # 4) scrollable, adaptive PC card grid (icons never distort).
        #    NOTE: the canvas height option is its REQUESTED height - keep it
        #    modest so the stats/toolbar/grid/results/details stack never
        #    overflows the page (an overflowing pack shrinks the last-packed
        #    details panel and hides its bottom rows).  The grid still
        #    EXPANDS to fill all leftover space, so this value is only the
        #    floor - the viewport grows to whatever the window can spare.
        grid_box = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        grid_box.grid(row=3, column=0, sticky="nsew", padx=16, pady=(2, 4))
        self.pc_canvas = ctk.CTkCanvas(grid_box, bg=BG_LIGHT, highlightthickness=0,
                                   height=70)
        vsb = ctk.CTkScrollbar(grid_box, height=51, orientation="vertical",
                            command=self.pc_canvas.yview)
        self.pc_canvas.configure(yscrollcommand=vsb.set)
        self.pc_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.pc_grid = ctk.CTkFrame(self.pc_canvas, fg_color=BG_LIGHT)
        self._grid_win = self.pc_canvas.create_window(
            (0, 0), window=self.pc_grid, anchor="nw")
        self.pc_grid.bind("<Configure>", lambda e: self.pc_canvas.configure(
            scrollregion=self.pc_canvas.bbox("all")))
        self.pc_canvas.bind("<Configure>", lambda e: self._layout_cards())
        wheel = lambda e: self.pc_canvas.yview_scroll(-1 * (e.delta // 120),
                                                      "units")
        self.pc_canvas.bind("<MouseWheel>", wheel)
        self.pc_grid.bind("<MouseWheel>", wheel)
        self._grid_empty = None

        # 5) compact bulk toolbar: "Selected: X PCs" over the 7 actions
        #    (per-PC results appear in the box below)
        bulk = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        bulk.grid(row=4, column=0, sticky="ew", padx=16, pady=(2, 4))
        head = ctk.CTkFrame(bulk, fg_color=BG_LIGHT)
        head.pack(fill="x")
        cnt = ctk.CTkLabel(head, text="Selected: 0 PCs",
                       font=("Segoe UI", 10, "bold"), text_color=ACCENT, fg_color=BG_LIGHT)
        cnt.pack(side="left")
        self._sel_labels.append(cnt)
        ctk.CTkLabel(head,
                 text="Applies to every selected PC "
                      "(Force Unlock: all PCs)",
                 font=FONT_SMALL, text_color=MUTED, fg_color=BG_LIGHT).pack(side="right")
        btnrow = ctk.CTkFrame(bulk, fg_color=BG_LIGHT)
        btnrow.pack(fill="x", pady=(3, 0))
        # CTkButton keeps a fixed 140px width that ignores its caption, so
        # this weight-driven grid would ask for 7 x 140px, overflow the row
        # and shrink every button until "Force Unlock" was cut off.  Size
        # each one from its caption - a tk.Button did that by itself - and
        # DPI scaling is pinned to 1.0 in theme.py, so a font pixel and a
        # widget pixel are the same thing.
        btn_fnt = tkfont.Font(font=("Segoe UI", 9, "bold"))
        for i, (text, action, color) in enumerate([
                ("\u23f8 Pause", "pause", WARN),
                ("\u25b6 Resume", "resume", SUCCESS),
                ("\u23ce Logout", "logout", ORANGE),
                ("\U0001f512 Lock", "lock", DANGER),
                ("\U0001f513 Force Unlock", "unlock", SUCCESS),
                ("\U0001f504 Restart", "restart", PURPLE),
                ("\u23fb Shutdown", "shutdown", CRIMSON)]):
            # M1: Force Unlock is the one action that never waits for a
            # selection - it releases every lab PC (see _unlock).
            cmd = self._unlock if action == "unlock" \
                else lambda a=action: self._bulk(a)
            b = ctk.CTkButton(btnrow, text=text,
                          width=btn_fnt.measure(text) + 11,
                          command=cmd,
                          fg_color=color, text_color="white", cursor="hand2",
                          font=("Segoe UI", 9, "bold"), 
                          # disabled text must stay readable ON the colour
                          # (the theme gray scored 1.1 against these fills)
                          text_color_disabled="#000000",
                          state="disabled")
            b.grid(row=0, column=i, sticky="ew", padx=3)
            self._ctrl_btns.append(b)
            if action == "unlock":
                self._unlock_btns.append(b)
        for c in range(7):
            btnrow.columnconfigure(c, weight=1)

        # 6) compact per-PC result box: SUCCESS / FAILED / OFFLINE / TIMEOUT
        #    (plain frame - the "Bulk results" caption was removed from the
        #     dashboard; 2 visible lines keep the page within one screen
        #     height so the details panel / grid never gets clipped)
        resf = ctk.CTkFrame(page)
        resf.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.bulk_results = ctk.CTkTextbox(resf, height=30, state="disabled",
                                    font=("Consolas", 9), fg_color="#0d1117",
                                    text_color="#c9d1d9", wrap="none")
        self.bulk_results.pack(side="left", fill="both", expand=True,
                               padx=4, pady=4)
        rsb = ctk.CTkScrollbar(resf, height=51, orientation="vertical",
                            command=self.bulk_results.yview)
        self.bulk_results.configure(yscrollcommand=rsb.set)
        rsb.pack(side="left", fill="y", pady=4)

        # 7) right-side PC INFORMATION + CURRENT USER panel (persistent,
        #    updates on every selection - no popups, never passwords)
        self._build_details_panel(page, row=0, rowspan=6)

        self.page_refresh["overview"] = self.refresh_overview
        self.refresh_overview()

    def _search_focus_in(self, event=None):
        """Search box shows the gray 'Search PCs...' hint until used."""
        if self._pc_search_ph:
            self._pc_search_ph = False
            self.pc_search_var.set("")

    def _search_focus_out(self, event=None):
        if not self.pc_search_var.get().strip():
            self._pc_search_ph = True
            self.pc_search_var.set(SEARCH_HINT)

    def _search_text(self):
        """Active search text ('' while only the placeholder is showing)."""
        if getattr(self, "_pc_search_ph", False):
            return ""
        return self.pc_search_var.get().strip().lower()

    def _client_search_focus_in(self, event=None):
        """Client PCs filter box uses the same placeholder pattern (spec 4/16)."""
        if getattr(self, "_client_search_ph", False):
            self._client_search_ph = False
            self.client_search_var.set("")

    def _client_search_focus_out(self, event=None):
        if not self.client_search_var.get().strip():
            self._client_search_ph = True
            self.client_search_var.set(SEARCH_HINT)

    def _client_search_text(self):
        """Active Client-PCs filter text ('' while the placeholder shows)."""
        if getattr(self, "_client_search_ph", False):
            return ""
        return self.client_search_var.get().strip().lower()

    # stats strip: exactly the 7 PC statistics (spec)
    STAT_TILES = ("Total PCs", "Online", "Offline", "In Use", "Available",
                  "Paused", "Locked")

    def _status_counts(self):
        """(counts_by_status_key, total_pc) - server-pushed states first,
        same definitions from the server's own DB rows when none exist yet.

        Total PCs = PCs that ever connected (real registered rows); the
        sub-counts therefore always add up to the total."""
        counts = {k: 0 for k in STATUS_ORDER}
        states = self._pc_states()
        if states:
            for s in states:
                k = status_key(s.get("state"))
                counts[k] = counts.get(k, 0) + 1
            return counts, len(states)
        total = 0
        try:
            conn = get_connection()
            total = conn.execute(
                "SELECT COUNT(*) c FROM computers "
                "WHERE last_heartbeat IS NOT NULL OR is_online=1"
            ).fetchone()["c"]
            counts["online"] = conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE is_online=1"
            ).fetchone()["c"]
            counts["in_use"] = conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE status='In Use' "
                "AND (last_heartbeat IS NOT NULL OR is_online=1)"
            ).fetchone()["c"]
            counts["available"] = conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE status='Available' "
                "AND (last_heartbeat IS NOT NULL OR is_online=1)"
            ).fetchone()["c"]
            counts["paused"] = conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE admin_state LIKE ?",
                ('%"pause"%',)).fetchone()["c"]
            counts["locked"] = conn.execute(
                "SELECT COUNT(*) c FROM computers WHERE admin_state LIKE ?",
                ('%"lock"%',)).fetchone()["c"]
            counts["offline"] = max(0, total - counts["online"])
            conn.close()
        except Exception:
            pass
        return counts, total

    def _refresh_stats(self):
        """Compact white tiles: status-colored top bar + number + label
        (same palette as the icons/cards/tree - no oversized cards)."""
        if not hasattr(self, "cards_frame") or not self.cards_frame.winfo_exists():
            return
        for widget in self.cards_frame.winfo_children():
            widget.destroy()
        counts, total = self._status_counts()
        tiles = [
            ("Total PCs", total, ACCENT),
            ("Online", counts.get("online", 0), STATUS_COLORS["online"]),
            ("Offline", counts.get("offline", 0), STATUS_COLORS["offline"]),
            ("In Use", counts.get("in_use", 0), STATUS_COLORS["in_use"]),
            ("Available", counts.get("available", 0), STATUS_COLORS["available"]),
            ("Paused", counts.get("paused", 0), STATUS_COLORS["paused"]),
            ("Locked", counts.get("locked", 0), STATUS_COLORS["locked"]),
        ]
        self._stat_tiles = [label for label, _, _ in tiles]
        for idx, (label, value, color) in enumerate(tiles):
            tile = ctk.CTkFrame(self.cards_frame, fg_color="white", height=STAT_TILE_H,
                            border_color=BORDER, border_width=1)
            tile.grid(row=0, column=idx, sticky="nsew", padx=4, pady=4)
            tile.grid_propagate(False)
            ctk.CTkFrame(tile, fg_color=color, height=4).pack(fill="x")
            ctk.CTkLabel(tile, text=str(value), font=FONT_STAT_NUM, text_color=BG_DARK,
                     fg_color="white", anchor="w").pack(fill="x", padx=10,
                                                  pady=(3, 0))
            ctk.CTkLabel(tile, text=label, font=FONT_SMALL, text_color="#5a6480", fg_color="white",
                     anchor="w").pack(fill="x", padx=10, pady=(0, 4))
            self.cards_frame.columnconfigure(idx, weight=1)

    def refresh_overview(self):
        self._refresh_stats()
        self._refresh_pc_grid()

    # ---- server-pushed PC states + filtering/sorting ------------------
    def _pc_states(self):
        """Read ONLY the server-pushed states - the dashboard never guesses."""
        if not self.server:
            self._state_by_name = {}
            return []
        try:
            states = self.server.list_pc_states()
        except Exception:
            states = []
        self._state_by_name = {str(s.get("pc_name")): s for s in states}
        return states

    def _filter_sort(self, states):
        search = self._search_text() if hasattr(self, "pc_search_var") \
            else ""
        status_f = self._pc_status_filter
        vis = []
        for s in states:
            if status_f is not None and status_key(s.get("state")) != status_f:
                continue
            if search and search not in str(s.get("pc_name", "")).lower() \
                    and search not in str(s.get("hostname", "")).lower():
                continue
            vis.append(s)
        sort = self.pc_sort_var.get() if hasattr(self, "pc_sort_var") else "Name"
        if sort == "Status":
            vis.sort(key=lambda s: (STATUS_ORDER.index(status_key(s.get("state"))),
                                    str(s.get("pc_name", "")).lower()))
        elif sort == "CPU %":
            vis.sort(key=lambda s: (-float(s.get("cpu_percent") or 0),
                                    str(s.get("pc_name", "")).lower()))
        else:
            vis.sort(key=lambda s: str(s.get("pc_name", "")).lower())
        return vis

    def _set_status_filter(self, key):
        self._pc_status_filter = key
        self._refresh_pc_grid()

    def _refresh_pc_grid(self):
        """Rebuild cards only when the server data / filter / sort changed
        (keeps clicks and highlights stable between refreshes)."""
        if not hasattr(self, "pc_grid") or not self.pc_grid.winfo_exists():
            return
        all_states = self._pc_states()
        vis = self._filter_sort(all_states)
        sig = (tuple((str(s.get("pc_name")), status_key(s.get("state")),
                      str(s.get("logged_in_user") or ""),
                      str(s.get("account_full_name") or "")) for s in vis),
               self.pc_search_var.get() if hasattr(self, "pc_search_var") else "",
               self.pc_sort_var.get() if hasattr(self, "pc_sort_var") else "",
               self._pc_status_filter)
        if sig != self._pc_sig:
            self._pc_sig = sig
            for w in self.pc_grid.winfo_children():
                w.destroy()
            self._pc_cards.clear()
            self._grid_empty = None
            if not vis:
                # the message is 586px on one line but its grid cell is ~490px,
                # so it clipped mid-sentence (it did in the Tk build too); wrap
                # it so the whole hint stays readable at every window size.
                self._grid_empty = ctk.CTkLabel(
                    self.pc_grid, fg_color=BG_LIGHT, text_color=SUBTLE,
                    font=("Segoe UI", 10), justify="left", wraplength=460,
                    text="No PCs have connected to this server yet - a PC "
                         "appears here automatically the first time it connects.")
                self._grid_empty.grid(row=0, column=0, sticky="w",
                                      padx=8, pady=12)
            else:
                for s in vis:
                    self._make_pc_card(s)
            # selection is filter/sort-safe: keep only what is visible
            vis_names = {str(s.get("pc_name")) for s in vis}
            pruned = [n for n in self._selected_pcs if n in vis_names]
            if pruned != self._selected_pcs:
                self._select_pcs(pruned)           # also syncs the tree
            self._layout_cards()
        self._update_legend(all_states)
        self._update_selection_ui()

    def _layout_cards(self, event=None):
        if not hasattr(self, "pc_canvas") or not hasattr(self, "pc_grid"):
            return
        try:
            cw = self.pc_canvas.winfo_width()
        except Exception:
            cw = 0
        if cw < 50:
            cw = 900
        cols = max(1, (cw - 12) // self.CARD_W)
        try:
            self.pc_canvas.itemconfigure(self._grid_win, width=max(cw - 6, 1))
        except Exception:
            pass
        for c in range(cols):
            self.pc_grid.columnconfigure(c, weight=1, uniform="pcgrid")
        for idx, info in enumerate(self._pc_cards.values()):
            r, c = divmod(idx, cols)
            info["frame"].grid(row=r, column=c, padx=5, pady=6, sticky="nsew")
        if self._grid_empty is not None and self._grid_empty.winfo_exists():
            self._grid_empty.grid(row=0, column=0, columnspan=max(1, cols),
                                  sticky="w")
        try:
            self.pc_canvas.configure(scrollregion=self.pc_canvas.bbox("all"))
        except Exception:
            pass

    def _pc_user_label(self, info):
        """Who is at this PC, shown as a NAME (e.g. "Juan Dela Cruz"),
        not the login id.

        Sources in trust order: the server-authoritative account full
        name on the state entry (set at session start), then the users
        row for the last login (covers offline rows that only know the
        id), then the raw id / "None" - an honest fallback is better
        than a blank line.  Never raises."""
        full = str(info.get("account_full_name") or "").strip()
        uid = str(info.get("logged_in_user") or "").strip()
        if not full and uid:
            try:
                conn = get_connection()
                row = conn.execute(
                    "SELECT full_name FROM users WHERE student_id=?",
                    (uid,)).fetchone()
                conn.close()
                full = str(row["full_name"] or "") if row else ""
            except Exception:
                full = ""
        return full or uid or "None"

    def _make_pc_card(self, info):
        """One PC card - the status monitor icon is the centerpiece
        (spec example):

            [icon]
            PC-01
            ONLINE
            IP: 192.168.0.101
            User: Juan Dela Cruz

        The selection highlight is a ring - it never touches the status
        color or the icon (no icon distortion)."""
        name = str(info.get("pc_name", ""))
        state = status_key(info.get("state"))
        color = STATUS_COLORS.get(state, STATUS_COLORS["unknown"])
        card = PaddedFrame(self.pc_grid, fg_color="white",
                        border_color=BORDER, border_width=1,
                        cursor="hand2",
                        padx=10, pady=9)
        icon = get_status_icon(state, 44)
        lbl_icon = ctk.CTkLabel(card, fg_color="white")
        if icon is not None:
            lbl_icon.configure(image=icon)
            lbl_icon.image = icon                 # keep a reference
        else:
            lbl_icon.configure(text="\u25a0", text_color=color, font=("Segoe UI", 16))
        lbl_icon.pack(pady=(0, 6))
        ctk.CTkLabel(card, text=name, font=FONT_CARD_NAME, fg_color="white",
                 text_color=BG_DARK, justify="center").pack()
        ctk.CTkLabel(card, text=STATUS_LABELS.get(state, "UNKNOWN"),
                 font=("Segoe UI", 9, "bold"), text_color=color, fg_color="white",
                 justify="center").pack(pady=(1, 0))
        ip = str(info.get("ip") or "\u2014")
        ctk.CTkLabel(card, text=f"IP: {ip}", font=FONT_SMALL,
                 text_color="#5a6480", fg_color="white", justify="center").pack(pady=(5, 0))
        user = self._pc_user_label(info)
        ctk.CTkLabel(card, text=f"User: {user}", font=FONT_SMALL,
                 text_color="#5a6480", fg_color="white", justify="center",
                 wraplength=140).pack()
        self._pc_cards[name] = {"frame": card, "icon": icon, "state": state,
                                "info": info}
        if name in self._selected_pcs:
            self._set_card_highlight(card, True)

        def on_click(event, n=name):
            ctrl = bool(event.state & 0x0004)        # Ctrl held
            sel = list(self._selected_pcs)
            if ctrl:
                if n in sel:
                    sel.remove(n)
                else:
                    sel.append(n)
            else:
                sel = [n]
            self._select_pcs(sel)

        wheel = lambda e: self.pc_canvas.yview_scroll(-1 * (e.delta // 120),
                                                      "units")

        def wire(w):
            w.bind("<Button-1>", on_click)
            w.bind("<MouseWheel>", wheel)
        wire(card)
        for child in card.winfo_children():
            wire(child)
            for grand in child.winfo_children():
                wire(grand)

    def _set_card_highlight(self, card, on):
        try:
            card.configure(border_color=ACCENT if on else BORDER,
                        border_width=2 if on else 1)
        except Exception:
            pass

    def _apply_card_highlights(self):
        for name, c in self._pc_cards.items():
            self._set_card_highlight(c["frame"], name in self._selected_pcs)

    def _reflow_chips(self, event=None):
        """Lay the status chips out in as many rows as the available width
        needs so none is ever cut off by the details panel.  The chips stay
        children of the row host and are placed into per-row frames with
        pack's -in option.  Widths come from font metrics (no event
        pumping), and a re-entrancy guard + early signature keep nested
        Configure events from destroying rows mid-build."""
        if getattr(self, "_reflowing_chips", False):
            return
        try:
            host = self._legend_host
            avail = host.winfo_width()
            if avail < 80:
                return                  # not realized yet -> Configure retries
            items = [self._legend_label, self._legend_btns.get(None)] + \
                [self._legend_btns.get(k) for k in STATUS_ORDER]
            items = [w for w in items if w is not None and w.winfo_exists()]
            sig = (avail,) + tuple(str(w.cget("text")) for w in items)
            if sig == self._chips_sig:
                return
            self._reflowing_chips = True
            self._chips_sig = sig       # set early: re-entry is a no-op
            try:
                fnt = tkfont.Font(font=("Segoe UI", 9, "bold"))
                # Re-affirm text-fitted chips (see the construction site):
                # CTkButton's default size ignores its caption.  The request
                # is only recomputed once the idle queue runs, so settle it
                # here - the guard above already keeps this re-entrant safe.
                # CustomTkinter's width=0 auto-fit measures the caption with
                # its own metrics, which come out ~15% narrower than the Tk
                # font actually used to draw it ("AVAILABLE (0)" - CTk says
                # 67, Tk draws 80), so the tail of every chip would be
                # clipped.  Size from the real font measurement instead;
                # DPI scaling is pinned to 1.0 in theme.py, so a font pixel
                # and a widget pixel are the same thing.
                for w in items:
                    txt = str(w.cget("text") or "")
                    if isinstance(w, ctk.CTkButton):
                        w.configure(width=fnt.measure(txt) + 14, height=0)
                    else:
                        w.configure(width=fnt.measure(txt) + 6)
                host.update_idletasks()
                # Measure what each chip ACTUALLY renders.  CustomTkinter
                # scales its geometry, so font metrics alone under-count and
                # the tail of the row would slide under the details panel.
                reqs = []
                for w in items:
                    is_lbl = (w is self._legend_label)
                    req = w.winfo_reqwidth()
                    if req < 4:            # not laid out yet -> font fallback
                        req = fnt.measure(str(w.cget("text"))) + \
                            (12 if is_lbl else 26)
                    reqs.append(req)
                for w in items:                    # detach from any old rows
                    w.pack_forget()
                for w in list(host.winfo_children()):   # drop empty row frames
                    if w not in items:
                        w.destroy()
                row = ctk.CTkFrame(host, fg_color=BG_LIGHT)
                row.pack(side="top", fill="x")
                x = 0
                for i, w in enumerate(items):
                    gap = 8 if i == 0 else 6
                    need = reqs[i] + gap
                    if x and x + need > avail:        # wrap to the next line
                        row = ctk.CTkFrame(host, fg_color=BG_LIGHT)
                        row.pack(side="top", fill="x", pady=(2, 0))
                        x = 0
                    w.pack(in_=row, side="left", padx=(0, gap))
                    # the chips stay children of the host while rows are
                    # created after them: raise above the row frames or the
                    # opaque frames would paint over the buttons
                    w.tkraise()
                    x += need
            except Exception:
                self._chips_sig = None  # allow the next Configure to retry
            finally:
                self._reflowing_chips = False
        except Exception:
            pass

    def _update_legend(self, all_states=None):
        if not hasattr(self, "_legend_btns"):
            return
        all_states = self._pc_states() if all_states is None else all_states
        counts = {k: 0 for k in STATUS_ORDER}
        for s in all_states:
            k = status_key(s.get("state"))
            counts[k] = counts.get(k, 0) + 1
        for key, btn in self._legend_btns.items():
            if not btn.winfo_exists():
                continue
            active = (self._pc_status_filter == key)
            if key is None:
                text, color = f"All ({len(all_states)})", ACCENT
            else:
                text = (f"{STATUS_LABELS.get(key, key.upper())} "
                        f"({counts.get(key, 0)})")
                color = STATUS_COLORS.get(key, "#666666")
            btn.configure(text=text,
                       fg_color=color if active else "white",
                       text_color="white" if active else color,
                       hover_color="white")
        # counts change the chip widths -> re-wrap the row if needed
        self._reflow_chips()

    # ---- unified selection (tree + cards) -----------------------------
    def _select_pcs(self, names, sync_tree=True):
        names = [str(n) for n in dict.fromkeys([n for n in names if n])]
        self._selected_pcs = names
        if sync_tree and hasattr(self, "client_tree") \
                and self.client_tree.winfo_exists():
            want = set(names)
            items = [i for i in self.client_tree.get_children()
                     if str(self.client_tree.item(i, "values")[0]) in want]
            try:
                self.client_tree.selection_set(items)
            except Exception:
                pass
        self._update_selection_ui()

    def _select_all(self):
        if self.current_page == "clients" and hasattr(self, "client_tree") \
                and self.client_tree.winfo_exists():
            names = [str(self.client_tree.item(i, "values")[0])
                     for i in self.client_tree.get_children()]
        else:
            names = [str(s.get("pc_name"))
                     for s in self._filter_sort(self._pc_states())]
        if not names:
            self.toast("No PCs to select.", "warn")
            return
        self._select_pcs(names)

    def _clear_selection(self):
        self._select_pcs([])

    def _on_ctrl_a(self, event=None):
        if self.current_page not in ("overview", "clients"):
            return None
        try:
            w = self.focus_get()
            if isinstance(w, (ttk.Entry, tk.Entry, tk.Text)):
                return None            # let the widget select its own text
        except Exception:
            pass
        self._select_all()
        return "break"

    def _update_selection_ui(self):
        has = bool(self._selected_pcs)
        state = "normal" if has else "disabled"
        for b in self._ctrl_btns:
            try:
                b.configure(state=state)
            except Exception:
                pass
        # M1: Force Unlock applies to every PC, so it stays enabled with
        # an empty selection - it is the one control that never waits.
        for b in self._unlock_btns:
            try:
                b.configure(state="normal")
            except Exception:
                pass
        # Task 5: Remote is the ONE control that can drive a machine, so it
        # never enables just because something is selected - it needs a
        # single PC AND that PC to be online.
        rb = self._remote_btn
        if rb is not None:
            try:
                rb.configure(state="normal" if self._remote_usable()
                          else "disabled")
            except Exception:
                pass
        text = f"Selected: {len(self._selected_pcs)} PCs"
        for lbl in self._sel_labels:
            try:
                if lbl.winfo_exists():
                    lbl.configure(text=text)
            except Exception:
                pass
        self._apply_card_highlights()
        self._update_details()

    # ---- right-side PC details panel (no popups, never passwords) -----
    def _build_details_panel(self, parent, row=0, rowspan=1):
        """Persistent right-side details column (spec: a right-side panel
        instead of popup windows) showing PC INFORMATION and CURRENT USER.
        The field area scrolls, so nothing is clipped at the minimum window
        size and the panel resizes with the page."""
        panel = ctk.CTkFrame(parent, fg_color=BG_LIGHT)
        panel.grid(row=row, column=1, rowspan=rowspan, sticky="nsew",
                   padx=(4, 16), pady=(12, 12))
        hint = ctk.CTkLabel(panel, text="Select a PC to see details",
                        font=FONT_SMALL, text_color=MUTED, fg_color=BG_LIGHT)
        hint.pack(anchor="w", padx=2, pady=(0, 6))
        box = ctk.CTkFrame(panel, fg_color=BG_LIGHT)
        box.pack(fill="both", expand=True)
        canvas = ctk.CTkCanvas(box, bg=BG_LIGHT, highlightthickness=0,
                           width=DETAIL_W - 14)
        vsb = ctk.CTkScrollbar(box, height=51, orientation="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner = ctk.CTkFrame(canvas, fg_color=BG_LIGHT)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            win, width=e.width))
        wheel = lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units")
        for w in (panel, box, canvas, inner):
            w.bind("<MouseWheel>", wheel)

        left = CardFrame(inner, text="PC INFORMATION")
        right = CardFrame(inner, text="CURRENT USER")
        left.pack(fill="x", padx=6, pady=(0, 6))
        right.pack(fill="x", padx=6)
        labels = {}

        def add_fields(lf, specs):
            for i, (key, caption) in enumerate(specs):
                ctk.CTkLabel(lf, text=caption, font=("Segoe UI", 9, "bold"),
                         anchor="w").grid(row=i, column=0, sticky="w",
                                          padx=(10, 4), pady=1)
                v = ctk.CTkLabel(lf, text="\u2014", font=("Segoe UI", 9),
                             anchor="w", justify="left", wraplength=130)
                v.grid(row=i, column=1, sticky="ew", padx=(0, 10), pady=1)
                lf.columnconfigure(1, weight=1)
                labels[key] = v

        add_fields(left, [("pc", "PC Name:"), ("ip", "IP Address:"),
                          ("host", "Hostname:"), ("state", "Status:"),
                          ("cpu", "CPU:"), ("ram", "RAM:"),
                          ("seen", "Last Seen:"), ("hb", "Last Heartbeat:"),
                          ("cver", "Client Version:"),
                          ("conn", "Connection:")])
        add_fields(right, [("user", "Username:"), ("fullname", "Full Name:"),
                           ("fname", "First Name:"), ("lname", "Last Name:"),
                           ("role", "Role:"), ("login", "Login Time:"),
                           ("logout", "Logout Time:"),
                           ("sstat", "Session Status:"),
                           ("curpc", "Current PC:")])
        d = {"frame": panel, "labels": labels, "hint": hint, "visible": True}
        self.detail_panels.append(d)
        return d

    def _update_details(self):
        show = bool(self._selected_pcs)
        for d in self.detail_panels:
            if not d["frame"].winfo_exists():
                continue
            hint = d.get("hint")
            try:
                if hint is not None and hint.winfo_exists():
                    if show:
                        hint.pack_forget()
                    else:
                        hint.pack(anchor="w", padx=2, pady=(0, 6))
            except Exception:
                pass
            if not show:
                # empty state: every field back to a dash
                for k, v in d["labels"].items():
                    try:
                        if v.winfo_exists():
                            v.configure(text="No active session"
                                     if k == "sstat" else "\u2014")
                    except Exception:
                        pass
                continue
            name = str(self._selected_pcs[0])
            info = self._state_by_name.get(name, {})
            state = status_key(info.get("state"))
            vals = {
                "pc": name or "\u2014",
                "state": STATUS_LABELS.get(state, "UNKNOWN"),
                "ip": str(info.get("ip") or "\u2014"),
                "host": str(info.get("hostname") or "\u2014"),
                "cpu": f"{float(info.get('cpu_percent') or 0):.0f}%",
                "ram": f"{float(info.get('ram_percent') or 0):.0f}%",
                "seen": str(info.get("last_heartbeat") or "\u2014"),
                "hb": str(info.get("last_heartbeat") or "\u2014"),
                "cver": str(info.get("client_version") or "\u2014"),
                "conn": str(info.get("connection_type") or "\u2014"),
                "user": "\u2014", "fullname": "\u2014",
                "fname": "\u2014", "lname": "\u2014",
                "role": "\u2014", "login": "\u2014",
                "logout": "\u2014", "curpc": name or "\u2014",
                "sstat": "No active session",
            }
            # identity comes from the server-recorded session + users row
            # (never a password, never the client's own claim)
            try:
                conn = get_connection()
                crow = conn.execute(
                    "SELECT last_heartbeat, client_version, connection_type "
                    "FROM computers WHERE pc_name=?", (name,)).fetchone()
                if crow:
                    vals["hb"] = str(crow["last_heartbeat"] or "\u2014")
                    vals["cver"] = str(crow["client_version"] or "\u2014")
                    vals["conn"] = str(crow["connection_type"] or "\u2014")
                row = conn.execute(
                    "SELECT student_id, full_name, login_time, status "
                    "FROM client_sessions "
                    "WHERE pc_name=? AND logout_time IS NULL "
                    "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
                sess_full = ""
                if row:
                    vals["user"] = row["student_id"] \
                        or info.get("logged_in_user") or "\u2014"
                    sess_full = row["full_name"] or ""
                    vals["login"] = row["login_time"] or "\u2014"
                    vals["sstat"] = row["status"] or "\u2014"
                    # active session: still logged in, so no logout time yet
                else:
                    last = conn.execute(
                        "SELECT login_time, logout_time FROM client_sessions "
                        "WHERE pc_name=? ORDER BY id DESC LIMIT 1",
                        (name,)).fetchone()
                    if last:
                        vals["login"] = last["login_time"] or "\u2014"
                        vals["logout"] = last["logout_time"] or "\u2014"
                    if info.get("logged_in_user"):
                        vals["user"] = str(info.get("logged_in_user"))
                role = str(info.get("account_role") or "")
                uid = str(vals["user"]) if vals["user"] != "\u2014" else ""
                db_full = ""
                if uid:
                    u = conn.execute(
                        "SELECT full_name, role FROM users WHERE student_id=?",
                        (uid,)).fetchone()
                    if u:
                        db_full = u["full_name"] or ""
                        role = role or (u["role"] or "")
                full = sess_full or db_full \
                    or str(info.get("account_full_name") or "")
                vals["fullname"] = full or "\u2014"
                if full:                      # derive first / last name
                    parts = full.split(None, 1)
                    vals["fname"] = parts[0]
                    vals["lname"] = parts[1] if len(parts) > 1 else "\u2014"
                vals["role"] = role.upper() if role else "\u2014"
                conn.close()
            except Exception:
                pass
            for k, v in d["labels"].items():
                try:
                    if v.winfo_exists():
                        v.configure(text=str(vals.get(k, "\u2014")))
                except Exception:
                    pass

    # --------------------------------------------------------- Client PCs (LAN)
    def _build_clients_tab(self):
        page = self._new_page("clients")
        # column 0 = table + controls (absorbs width), row 2 = the table
        # (absorbs height), column 1 = right-side details panel
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        # ---- toolbar ----
        bar = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))

        # spec item 4: instant filtering - the placeholder search box
        # filters the list on every keystroke (trace_add), same pattern as
        # the Dashboard's Search PCs box; no Enter or button press needed.
        # (The server address lives in the header; "Live updates" moved to
        # the selection row so this row fits the 1080 px minimum width.)
        self.client_search_var = tk.StringVar()
        self._client_search_ph = True              # showing the placeholder
        self.client_search_var.set(SEARCH_HINT)
        self.client_search_var.trace_add(
            "write", lambda *a: self._refresh_clients())
        self.client_search = ctk.CTkEntry(
            bar, textvariable=self.client_search_var, width=114)
        self.client_search.pack(side="left")
        self.client_search.bind("<FocusIn>", self._client_search_focus_in)
        self.client_search.bind("<FocusOut>", self._client_search_focus_out)

        ctk.CTkLabel(bar, text="Broadcast:", font=("Segoe UI", 9, "bold"),
                 fg_color=BG_LIGHT).pack(side="right", padx=(10, 4))
        self.broadcast_var = tk.StringVar()
        ctk.CTkEntry(bar, textvariable=self.broadcast_var, width=210).pack(side="right")
        ctk.CTkButton(bar, text="Send to All", command=self._broadcast,
                  fg_color=ACCENT, text_color="white", 
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  ).pack(side="right", padx=(4, 6))
        # Teaching share: THIS machine's screen pushed to every online lab
        # PC (PowerPoint, live coding).  Toggles in place so the operator
        # can always see whether the class is currently being shared to.
        self.share_btn = ctk.CTkButton(
            bar, text="📺 Share Screen", command=self._toggle_share,
            fg_color=ACCENT, text_color="white",
            font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.share_btn.pack(side="right", padx=(4, 0))

        # ---- selection row (Ctrl+A / Select All / Clear / counter) ----
        sel_row = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        sel_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        ctk.CTkButton(sel_row, text="Select All", command=self._select_all,
                  fg_color="#dde3f0", text_color=BG_DARK, cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(side="left")
        ctk.CTkButton(sel_row, text="Clear", command=self._clear_selection,
                  fg_color="#dde3f0", text_color=BG_DARK, cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="left", padx=(6, 0))
        cnt_lbl = ctk.CTkLabel(sel_row, text="Selected: 0 PCs",
                           font=("Segoe UI", 10, "bold"), text_color=ACCENT,
                           fg_color=BG_LIGHT)
        cnt_lbl.pack(side="left", padx=14)
        self._sel_labels.append(cnt_lbl)
        ctk.CTkLabel(sel_row,
                 text="Live updates every 2 s · Ctrl+A selects all PCs",
                 font=("Segoe UI", 8), text_color=MUTED, fg_color=BG_LIGHT).pack(
            side="right")

        # ---- table ----
        cols = ("pc_name", "status", "user", "ip", "hostname", "cpu", "ram",
                "hb", "online")
        frame = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 4))
        # height=10 (not 12): with toolbar + controls + right-side details,
        # a taller request overflows one screen and grid would CLIP the
        # last row.  The frame expands, so it still fills any leftover
        # space at larger window sizes.
        self.client_tree = ttk.Treeview(frame, columns=cols,
                                        show="tree headings", height=10)
        self.client_tree.heading("#0", text="")
        self.client_tree.column("#0", width=44, stretch=False, anchor="center")
        heads = [("pc_name", "PC Name", 120), ("status", "Status", 105),
                 ("user", "Current User", 130), ("ip", "IP Address", 115),
                 ("hostname", "Hostname", 125), ("cpu", "CPU %", 62),
                 ("ram", "RAM %", 62), ("hb", "Last Seen", 95),
                 ("online", "Online/Offline", 105)]
        for cid, text, w in heads:
            self.client_tree.heading(cid, text=text)
            self.client_tree.column(cid, width=w, anchor="center" if w < 120 else "w")
        vsb = ctk.CTkScrollbar(frame, height=51, orientation="vertical", command=self.client_tree.yview)
        self.client_tree.configure(yscrollcommand=vsb.set)
        self.client_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        # one tag color per authoritative status (utils.STATUS_*)
        for key in STATUS_ORDER:
            self.client_tree.tag_configure(key,
                                           foreground=STATUS_COLORS.get(key, OFFLINE))
        self.client_tree.bind("<Double-1>", lambda e: self._screenshot())
        self.client_tree.bind("<<TreeviewSelect>>", self._on_client_select)

        # ---- control buttons (grid: never cut off at any window size,
        #      disabled until a PC is selected - no "select first" popups) ----
        btns = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        btns.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 6))
        controls = [
            ("\U0001f512 Lock", self._lock, DANGER),
            ("\U0001f513 Force Unlock", self._unlock, SUCCESS),
            ("\u23ce Logout User", self._logout_client, ORANGE),
            ("\u23f8 Pause", self._pause, WARN),
            ("\u25b6 Resume", self._resume, SUCCESS),
            ("\U0001f504 Restart", self._restart, PURPLE),
            ("\u23fb Shutdown", self._shutdown, CRIMSON),
            ("\U0001f4f7 Screenshot", self._screenshot, ACCENT),
            ("\U0001f441 Observe", self._observe, SKY),
            # Task 5: view-only Observe stays exactly as it was; Remote is
            # the only control that forwards input, and only while a PC is
            # selected AND online (see _update_selection_ui).
            ("\U0001f5b1 Remote", self._remote, "#a33b00"),
            # one file, picked once, pushed to every selected PC's Desktop
            # (documents only - executables are refused at the Server).
            ("\U0001f4e4 Send File", self._send_file, "#0f766e"),
        ]
        if not self.is_admin:
            # P4: watching or driving a machine is ADMINISTRATOR-only, so
            # no other role is even offered the two controls.  This is the
            # convenience layer - the Server refuses the same calls for a
            # non-admin role, so hiding the buttons is defence in depth
            # rather than the access control itself.
            controls = [c for c in controls
                        if not c[0].endswith(("Observe", "Remote",
                                              "Send File"))]
        for i, (text, cmd, color) in enumerate(controls):
            r, c = divmod(i, 5)
            b = ctk.CTkButton(btns, text=text, command=cmd, fg_color=color, text_color="white",
                          font=("Segoe UI", 9, "bold"),
                          text_color_disabled="#000000",
                          cursor="hand2", state="disabled")
            b.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            self._ctrl_btns.append(b)
            if text.endswith("Remote"):
                self._remote_btn = b
            if text.endswith("Force Unlock"):
                self._unlock_btns.append(b)
        for c in range(5):
            btns.columnconfigure(c, weight=1)

        # ---- inline message row (no dialogs for normal events) ----
        # row 3: the control grid now has 3 rows of buttons (11 for an
        # administrator), so the message row sits underneath, never on top.
        msg_row = ctk.CTkFrame(btns, fg_color=BG_LIGHT)
        msg_row.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        msg_row.columnconfigure(1, weight=1)
        ctk.CTkLabel(msg_row, text="Message to selected PC:",
                 font=("Segoe UI", 9, "bold"), fg_color=BG_LIGHT).grid(
            row=0, column=0, sticky="w", padx=(2, 6))
        self.single_msg_var = tk.StringVar()
        ctk.CTkEntry(msg_row, textvariable=self.single_msg_var).grid(
            row=0, column=1, sticky="ew")
        send_btn = ctk.CTkButton(msg_row, text="Send", command=self._send_msg,
                             fg_color=ACCENT, text_color="white", 
                             font=("Segoe UI", 9, "bold"), cursor="hand2",
                             state="disabled")
        send_btn.grid(row=0, column=2, padx=(6, 0))
        self._ctrl_btns.append(send_btn)

        self.client_status_lbl = ctk.CTkLabel(
            page, text="Select a client PC to enable the controls "
                       "(Force Unlock = all PCs, no selection; "
                       "double-click for a screenshot).",
            font=FONT_BODY, text_color=SUBTLE, fg_color=BG_LIGHT, anchor="w")
        self.client_status_lbl.grid(row=4, column=0, sticky="ew",
                                    padx=16, pady=(0, 8))

        # right-side PC + Current User details panel (no popups)
        self._build_details_panel(page, row=0, rowspan=5)

        self._refresh_clients()
        self._auto_refresh_clients()

    # ---- client list -------------------------------------------------
    def _on_client_select(self, event=None):
        """Treeview selection -> the unified selection model."""
        names = []
        if hasattr(self, "client_tree") and self.client_tree.winfo_exists():
            for item in self.client_tree.selection():
                vals = self.client_tree.item(item, "values")
                if vals:
                    names.append(str(vals[0]))
        self._select_pcs(names, sync_tree=False)

    def _refresh_clients(self):
        if not hasattr(self, "client_tree") or not self.client_tree.winfo_exists():
            return
        # ONLY server-pushed states - the dashboard never guesses a status
        states = self._pc_states()
        # instant filter (spec item 4): hide rows that don't match the
        # typed text across name / user / IP / hostname ('' while the
        # placeholder hint is showing)
        q = self._client_search_text() \
            if hasattr(self, "client_search_var") else ""
        if q:
            states = [c for c in states if any(
                q in str(c.get(k, "") or "").lower()
                for k in ("pc_name", "logged_in_user", "ip", "hostname"))]
        # keep whatever was already selected (the tree is a source of truth too)
        prev = set(self._selected_pcs)
        for item in self.client_tree.get_children():
            vals = self.client_tree.item(item, "values")
            if vals and str(vals[0]) in prev:
                prev.add(str(vals[0]))
        try:
            for item in self.client_tree.selection():
                vals = self.client_tree.item(item, "values")
                if vals:
                    prev.add(str(vals[0]))
        except Exception:
            pass
        for item in self.client_tree.get_children():
            self.client_tree.delete(item)

        for c in states:
            name = str(c.get("pc_name", ""))
            state = status_key(c.get("state"))
            online = bool(c.get("is_online"))
            last = c.get("last_heartbeat", "") or "-"
            if len(str(last)) > 8:            # full datetime -> short time
                last = str(last)[-8:]
            kwargs = {"tags": (state,), "values": (
                name,
                STATUS_LABELS.get(state, "UNKNOWN"),
                c.get("logged_in_user") or "",
                c.get("ip", ""),
                c.get("hostname", ""),
                f"{float(c.get('cpu_percent') or 0):.0f}",
                f"{float(c.get('ram_percent') or 0):.0f}",
                last,
                "\u25cf Online" if online else "\u25cb Offline",
            )}
            icon = get_status_icon(state, 16)
            if icon is not None:
                kwargs["image"] = icon
            self.client_tree.insert("", "end", **kwargs)
        # re-apply the selection to the rebuilt tree (filter/sort-safe)
        items = [i for i in self.client_tree.get_children()
                 if str(self.client_tree.item(i, "values")[0]) in prev]
        try:
            self.client_tree.selection_set(items)
        except Exception:
            pass
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
        if self._selected_pcs:
            return self._selected_pcs[0]
        sel = self.client_tree.selection() if hasattr(self, "client_tree") else ()
        if not sel:
            if not quiet:
                self.toast("Select a client PC first.", "warn")
            return None
        return self.client_tree.item(sel[0], "values")[0]

    # ---- remote control (Task 5) ----------------------------------------
    def _remote_usable(self):
        """Remote may only be opened for exactly ONE selected, ONLINE PC."""
        if len(self._selected_pcs) != 1 or not self.server:
            return False
        entry = self.server.get_client(str(self._selected_pcs[0]))
        return bool(entry and entry.is_online)

    def _pc_online(self, pc):
        entry = self.server.get_client(pc) if self.server else None
        return bool(entry and entry.is_online)

    def _remote_owns_stream(self, pc):
        """True when THIS remote session opened `pc`'s screen stream.

        Observe and Remote share the one stream per PC, so whoever did NOT
        open it must leave it alone when they stop - otherwise ending one
        would silently blank the other's window.
        """
        return bool(pc) and self._remote_streaming_pc == pc

    def _remote(self):
        """`[ 🖱 Remote ]` - start (or stop) a remote mouse/keyboard session."""
        if self._remote_pc:
            # Ending a session is always allowed: only STARTING the
            # takeover is reserved for the ADMINISTRATOR role (P4), so a
            # machine can never be left under remote control by someone
            # who is no longer allowed to hold it.
            self._stop_remote(self._remote_pc, "stopped by administrator")
            return
        if not self.is_admin:
            self.toast("Remote Control requires the ADMINISTRATOR role.",
                       "error")
            return
        pc = self._selected_pc(quiet=True)
        if not pc:
            self.toast("Select a client PC first.", "warn")
            return
        if len(self._selected_pcs) > 1:
            self.toast("Remote Control works on ONE PC at a time.", "warn")
            return
        if not self._pc_online(pc):
            msg = f"{pc} is offline"
            if hasattr(self, "client_status_lbl"):
                self.client_status_lbl.configure(text=msg, text_color=DANGER)
            self.toast(msg, "error")
            return
        info = self._state_by_name.get(pc) or {}
        user = str(info.get("logged_in_user") or "nobody")
        ip = str(info.get("ip") or "-")
        # explicit confirmation: PC + current user + IP + what it means
        if not messagebox.askyesno(
                "Remote Control",
                f"Start REMOTE CONTROL of {pc}?\n\n"
                f"PC name       : {pc}\n"
                f"Current user  : {user}\n"
                f"IP address    : {ip}\n\n"
                "WARNING: the mouse and keyboard of that PC will be "
                "controlled from this console. Every movement, click and "
                "key press you make in the Remote Control window is "
                "forwarded to that PC while the session is active.\n\n"
                "The person at that PC sees a persistent "
                "'REMOTE CONTROL ACTIVE' indicator. Continue?",
                parent=self, icon="warning"):
            return
        res = self.server.start_remote_control(pc, self._admin_name(),
                                              role=self.role_name)
        if not res.get("success"):
            err = res.get("error") or "could not start"
            if hasattr(self, "client_status_lbl"):
                self.client_status_lbl.configure(text=f"Remote → {pc}: {err}",
                                               text_color=DANGER)
            self.toast(f"Remote Control: {err}", "error")
            return
        self._remote_pc = pc
        self._remote_streaming_pc = pc
        self._open_remote_viewer(pc, user, ip)
        if hasattr(self, "client_status_lbl"):
            self.client_status_lbl.configure(
                text=f"REMOTE CONTROL ACTIVE · {pc} "
                     f"(session {res.get('session_id', '')})",
                text_color="#a33b00")
        self.toast(f"Remote control of {pc} started", "warn")

    def _stop_remote(self, pc, reason="stopped by administrator"):
        """End the remote session NOW: audit, input gate, stream, window.

        Only talks to the Server when this window still OWNS the session -
        after a drop the Server has already ended (and audited) it, so
        closing the leftover window must not write a second end-of-session
        row for the same session.
        """
        if not pc:
            return
        owned = self._remote_pc == pc
        if owned and self.server:
            try:
                # the stream is owned by the CONSOLE (see the guard below),
                # so the Server only ends the control session here.
                self.server.stop_remote_control(pc, self._admin_name(),
                                                reason=reason,
                                                stop_stream=False)
            except Exception:
                pass
        was_streaming = owned and self._remote_streaming_pc == pc
        if was_streaming:
            self._remote_streaming_pc = None
        if was_streaming and self._observing != pc and self.server:
            try:
                self.server.stop_screen_observe(pc, self._admin_name())
            except Exception:
                pass
        self._remote_pc = None
        self._destroy_remote_window()
        if hasattr(self, "client_status_lbl"):
            try:
                self.client_status_lbl.configure(
                    text=f"Remote control stopped · {pc}", text_color=SUBTLE)
            except Exception:
                pass
        self.toast(f"Remote control of {pc} stopped", "info")
        self._update_selection_ui()

    # ---- Remote Control window ------------------------------------------
    def _open_remote_viewer(self, pc, user="", ip=""):
        """Build the Remote Control window (Task 5).

        Header line with the PC / current user / IP, the live screen, a
        `[Stop Remote Control]` button and a `Connection: LIVE` badge.
        Deliberately a separate window from the Observe viewer so closing
        one never disturbs the other (and `[Observe]` stays view-only).
        """
        if self._remote_win is not None and self._remote_win.winfo_exists():
            self._remote_win.deiconify()
            self._remote_win.lift()
            self._update_remote_header(pc, user, ip)
            return
        win = ctk.CTkToplevel(self)
        win.title(f"Remote Control · {pc}")
        set_app_icon(win)
        win.configure(fg_color="#0d1117")
        center_window(win, 1150, 780)
        win.minsize(860, 560)
        win._remote_pc = pc

        top = ctk.CTkFrame(win, fg_color="#161b22")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=f"\U0001f5b1  Remote Control · {pc}",
                 fg_color="#161b22", text_color="white",
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=12,
                                                     pady=8)
        info = ctk.CTkLabel(top, text="", fg_color="#161b22", text_color="#9fb4ff",
                        font=("Segoe UI", 9))
        info.pack(side="left", padx=10)
        self._remote_conn_lbl = ctk.CTkLabel(top, text="Connection: LIVE",
                                         fg_color="#161b22", text_color=SUCCESS,
                                         font=("Segoe UI", 9, "bold"))
        self._remote_conn_lbl.pack(side="right", padx=10)
        ctk.CTkButton(top, text="Stop Remote Control",
                  command=lambda: self._stop_remote(
                      pc, "stopped by administrator"),
                  fg_color=DANGER, text_color="white", 
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  ).pack(side="right", padx=6, pady=6)

        win.label = ctk.CTkLabel(win, fg_color="#0d1117", text="Waiting for frames…",
                             text_color="#8b949e", font=("Segoe UI", 11))
        win.label.pack(fill="both", expand=True, padx=8, pady=8)
        win.protocol("WM_DELETE_WINDOW",
                     lambda: self._stop_remote(pc, "window closed"))
        self._remote_win = win
        self._remote_info_lbl = info
        self._update_remote_header(pc, user, ip)
        self._bind_remote_input(win)
        try:
            win.after(250, win.focus_force)
        except Exception:
            pass

    def _update_remote_header(self, pc, user="", ip=""):
        lbl = self._remote_info_lbl
        if lbl is None:
            return
        info = self._state_by_name.get(pc) or {}
        u = user or str(info.get("logged_in_user") or "nobody")
        a = ip or str(info.get("ip") or "-")
        try:
            lbl.configure(text=f"  PC: {pc}   |   User: {u}   |   IP: {a}")
        except Exception:
            pass

    def _destroy_remote_window(self, pc=None):
        v = self._remote_win
        self._remote_win = None
        self._remote_conn_lbl = None
        self._remote_info_lbl = None
        if v is not None:
            try:
                v.destroy()
            except Exception:
                pass

    def _set_remote_connection(self, live, text=None):
        lbl = self._remote_conn_lbl
        if lbl is None:
            return
        try:
            lbl.configure(text=text or ("Connection: LIVE" if live
                                     else "Connection: LOST"),
                       text_color=SUCCESS if live else DANGER)
        except Exception:
            pass

    def _handle_remote_dropped(self, pc, reason="connection lost"):
        """The Server force-ended a session this window was driving (Task 5).

        Nothing is sent back - the Client is gone or has already been told -
        so the console only closes its own state and says so clearly.  A
        later reconnect can NEVER walk back into this session: the Server
        removed it from its table before raising the event.
        """
        if self._remote_pc != pc:
            return                          # not ours (or already closed)
        self._remote_pc = None
        self._remote_streaming_pc = None
        self._set_remote_connection(False, f"Connection: LOST ({reason})")
        if hasattr(self, "client_status_lbl"):
            try:
                self.client_status_lbl.configure(
                    text=f"Remote control ended · {pc} · {reason}",
                    text_color=DANGER)
            except Exception:
                pass
        self.toast(f"Remote control of {pc} ended: {reason}", "warn")
        self._update_selection_ui()

    # ---- Remote Control input forwarding --------------------------------
    def _bind_remote_input(self, win):
        """Forward ONLY mouse move/click/scroll and key down/up (Task 5)."""
        lab = win.label
        lab.bind("<Motion>", self._on_remote_motion)
        for btn, num in ((1, 1), (2, 2), (3, 3)):
            lab.bind(f"<ButtonPress-{num}>",
                     lambda e, n=num: self._on_remote_button(e, n, "down"))
            lab.bind(f"<ButtonRelease-{num}>",
                     lambda e, n=num: self._on_remote_button(e, n, "up"))
        lab.bind("<MouseWheel>", self._on_remote_wheel)
        win.bind("<KeyPress>", self._on_remote_key_press)
        win.bind("<KeyRelease>", self._on_remote_key_release)
        win.bind("<ButtonPress>", lambda e: win.focus_set())

    @staticmethod
    def _remote_norm(win, event):
        """Viewer pointer -> 0..1 position inside the streamed frame.

        CTkLabel.bind() delivers events from the label's INTERNAL tk
        widgets - the image-sized label under the pointer, or the full
        size canvas in the margins - so event.x/event.y live in two
        different coordinate spaces and can never be compared against
        the outer label geometry directly (that double-subtracts the
        image centring offset and lands the remote cursor up to ~10 %
        off).  Normalising the ABSOLUTE pointer position (x_root/y_root)
        against the photo's absolute origin is correct no matter which
        widget delivered the event: the photo is always grid-centred in
        the label, so its top-left is label_root + (label - photo) / 2.
        Returns None for anything outside the picture (never forwarded).
        """
        photo = getattr(win.label, "image", None)
        if photo is None:
            return None
        try:
            iw, ih = int(photo.width()), int(photo.height())
        except Exception:
            # ctk.CTkImage has no width()/height() (a PIL PhotoImage
            # does), and the AttributeError used to swallow every mouse
            # event.  Ask the inner tk label for the photo it is actually
            # drawing - same pixel space as winfo_width() - then fall
            # back to the image's own logical size.
            try:
                inner = str(win.label._label.cget("image"))
                iw = int(win.label._label.tk.call("image", "width", inner))
                ih = int(win.label._label.tk.call("image", "height", inner))
            except Exception:
                try:
                    iw, ih = int(photo._size[0]), int(photo._size[1])
                except Exception:
                    return None
        if iw <= 0 or ih <= 0:
            return None
        try:
            lw, lh = win.label.winfo_width(), win.label.winfo_height()
            ox = win.label.winfo_rootx() + (lw - iw) // 2
            oy = win.label.winfo_rooty() + (lh - ih) // 2
            nx = (event.x_root - ox) / float(iw)
            ny = (event.y_root - oy) / float(ih)
        except Exception:
            return None
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            return None
        return nx, ny

    def _remote_send(self, event):
        """Send one whitelisted event - only while a session is running."""
        if not self._remote_pc or not self.server:
            return False
        try:
            return bool(self.server.forward_remote_input(
                self._remote_pc, [event],
                admin=self._admin_name(), role=self.role_name))
        except Exception:
            return False

    def _on_remote_motion(self, event):
        if not self._remote_pc:
            return
        # throttle: a screen of Motion events would flood the LAN for no
        # gain (the client applies absolute positions anyway)
        now = time.time()
        if now - getattr(self, "_remote_last_move", 0.0) < 0.03:
            return "break"
        self._remote_last_move = now
        pt = self._remote_norm(self._remote_win, event)
        if pt:
            self._remote_send({"kind": "mouse", "action": "move",
                               "x": pt[0], "y": pt[1], "button": 1,
                               "delta": 1.0})
        return "break"

    def _on_remote_button(self, event, button, action):
        if not self._remote_pc:
            return
        pt = self._remote_norm(self._remote_win, event)
        if pt:
            self._remote_send({"kind": "mouse", "action": action,
                               "x": pt[0], "y": pt[1], "button": button,
                               "delta": 1.0})
        return "break"

    def _on_remote_wheel(self, event):
        if not self._remote_pc:
            return
        pt = self._remote_norm(self._remote_win, event)
        if pt:
            self._remote_send({"kind": "mouse", "action": "scroll",
                               "x": pt[0], "y": pt[1], "button": 1,
                               "delta": 1.0 if event.delta > 0 else -1.0})
        return "break"

    def _on_remote_key_press(self, event):
        if self._remote_pc:
            self._remote_send({"kind": "key", "action": "down",
                               "keysym": event.keysym,
                               "char": event.char or ""})
        return "break"

    def _on_remote_key_release(self, event):
        if self._remote_pc:
            self._remote_send({"kind": "key", "action": "up",
                               "keysym": event.keysym,
                               "char": event.char or ""})
        return "break"

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
        self.client_status_lbl.configure(text=f"{label} → {pc} …", text_color=SUBTLE)

    def _pump_results(self):
        try:
            while True:
                label, pc, resp = self.results.get_nowait()
                if label == "bulk":
                    self._show_bulk_results(str(pc), resp)
                    continue
                ok = resp.get("success")
                txt = f"{label} → {pc}: " + ("done" if ok
                                             else (resp.get("error") or "failed"))
                if hasattr(self, "client_status_lbl"):
                    self.client_status_lbl.configure(
                        text=txt, text_color=SUCCESS if ok else DANGER)
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
                self._handle_server_event(kind, data)
        except queue.Empty:
            pass

    def _handle_server_event(self, kind, data):
        """Apply one server-pushed event (runs from the pump)."""
        if kind == "screen_frame":
            frame = data.get("data", {})
            pcn = data.get("pc_name", "")
            if self._viewer_alive():
                self._viewer_show(frame, pcn)
            # Task 5: the Remote Control window has its own stream target,
            # so closing or observing never disturbs it (and vice versa).
            v = self._remote_win
            if v is not None and v.winfo_exists() and \
                    (not pcn or pcn == getattr(v, "_remote_pc", None)):
                self._viewer_show(frame, pcn, target=v)
        elif kind == "remote_stopped":
            # Task 5: the Server force-ended a session (Client dropped its
            # heartbeat or disconnected).  Bring the console back to a
            # clean, closed state - the Server has already audited it.
            self._handle_remote_dropped(data.get("pc_name", "?"),
                                        data.get("reason", "connection lost"))
        elif kind == "client_online":
            self.toast(f"{data.get('pc_name', '?')} is online", "info")
        elif kind == "client_offline":
            self.toast(f"{data.get('pc_name', '?')} went offline", "warn")
            # P1-7: the server stops the stream the moment the PC drops
            # (heartbeat sweep), so keep the toolbar honest instead of
            # leaving a frozen "Observing ..." state behind a viewer that
            # can no longer receive frames.
            if getattr(self, "_observing", None) == data.get("pc_name"):
                self._observing = None
                lbl = getattr(self, "client_status_lbl", None)
                if lbl is not None:
                    try:
                        lbl.configure(
                            text=f"Observation stopped · "
                                 f"{data.get('pc_name', '?')} went offline",
                            text_color=DANGER)
                    except Exception:
                        pass
            # Task 5: an offline PC can no longer be driven - close the
            # session before the frozen window looks like it still works.
            self._handle_remote_dropped(data.get("pc_name", "?"),
                                        "client went offline")
            # P1-7: the server stops the stream the moment the PC drops
            # (heartbeat sweep), so keep the toolbar honest instead of
            # leaving a frozen "Observing ..." state behind a viewer that
            # can no longer receive frames.
            if getattr(self, "_observing", None) == data.get("pc_name"):
                self._observing = None
                lbl = getattr(self, "client_status_lbl", None)
                if lbl is not None:
                    try:
                        lbl.configure(
                            text=f"Observation stopped · "
                                 f"{data.get('pc_name', '?')} went offline",
                            text_color=DANGER)
                    except Exception:
                        pass
        elif kind == "status_changed":
            # server-pushed transition -> refresh immediately
            self._refresh_clients()
            self._refresh_pc_grid()
        elif kind == "account_changed":
            # account add/update/delete -> live-sync every account view
            # now instead of waiting for the 3 s polling tick
            for key in ("students", "staff", "sessions", "activity"):
                fn = self.page_refresh.get(key)
                if fn:
                    try:
                        fn()
                    except Exception:
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
        # Task 5: closing the console must never leave a live remote-control
        # session behind - the end-of-session audit row is written here too.
        _remote_pc = getattr(self, "_remote_pc", None)
        _remote_stream = getattr(self, "_remote_streaming_pc", None)
        if _remote_pc and getattr(self, "server", None):
            try:
                self.server.stop_remote_control(_remote_pc,
                                                self._admin_name(),
                                                reason="console closed",
                                                stop_stream=False)
            except Exception:
                pass
            # the stream only stops here if Observe is not the one using it
            if _remote_stream and self._observing != _remote_stream:
                try:
                    self.server.stop_screen_observe(_remote_stream,
                                                    self._admin_name())
                except Exception:
                    pass
            self._remote_pc = None
            self._remote_streaming_pc = None
        self._destroy_remote_window()
        # stop any live screen observation owned by this window
        if getattr(self, "_observing", None) and getattr(self, "server", None):
            try:
                self.server.stop_screen_observe(self._observing,
                                                self._admin_name())
            except Exception:
                pass
            self._observing = None
        super().destroy()

    # ---- individual controls (all route through _bulk) -----------------
    def _lock(self):
        self._bulk("lock")

    def _unlock(self):
        # M1: Admin Force Login releases EVERY lab PC - one click, no
        # selection, and it works even where no user is logged in (force
        # login just lands that kiosk on its login screen).  Offline PCs
        # are targeted too: the Server clears their persisted lock so a
        # disconnected kiosk cannot re-lock itself on reconnect.
        if not self.server:
            self.toast("Server not connected.", "warn")
            return
        names = [str(s.get("pc_name", "")) for s in self._pc_states()
                 if str(s.get("pc_name", ""))]
        if not names:
            self.toast("No PCs registered yet.", "warn")
            return
        self._bulk("unlock", pcs=names)

    def _logout_client(self):
        self._bulk("logout")

    def _pause(self):
        self._bulk("pause")

    def _resume(self):
        self._bulk("resume")

    def _restart(self):
        self._bulk("restart")

    def _shutdown(self):
        self._bulk("shutdown")

    # ---- bulk actions (per-PC SUCCESS/FAILED/OFFLINE/TIMEOUT) ----------
    def _set_status_text(self, text, color=SUBTLE):
        if hasattr(self, "client_status_lbl") \
                and self.client_status_lbl.winfo_exists():
            self.client_status_lbl.configure(text=text, text_color=color)

    def _append_results(self, text):
        if not hasattr(self, "bulk_results") or not self.bulk_results.winfo_exists():
            return
        try:
            self.bulk_results.configure(state="normal")
            self.bulk_results.insert("end", text + "\n")
            lines = int(self.bulk_results.index("end-1c").split(".")[0])
            if lines > 40:                       # keep the box bounded
                self.bulk_results.delete("1.0", f"{lines - 40}.0")
            self.bulk_results.see("end")
            self.bulk_results.configure(state="disabled")
        except Exception:
            pass

    def _show_bulk_results(self, action, results):
        counts = {}
        lines = [f"[BULK {action.upper()}] {now_time()} \u00b7 "
                 f"{len(results)} PC(s)"]
        for name in sorted(results):
            r = results[name] or {}
            kind = str(r.get("result") or "FAILED")
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "SUCCESS":
                # e.g. "PC1: SUCCESS  (login_allowed)" - the client's own
                # ack makes an unlock's outcome visible per PC
                detail = str(r.get("detail") or "")
                extra = f"  ({detail})" if detail else ""
            else:
                extra = f"  ({r.get('error') or ''})"
            lines.append(f"  {name}: {kind}{extra}")
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        lines.append(f"  \u2192 {summary}")
        self._append_results("\n".join(lines))
        if counts.get("SUCCESS") == len(results):
            kind2, color = "success", SUCCESS
        elif counts.get("SUCCESS"):
            kind2, color = "warn", WARN
        else:
            kind2, color = "error", DANGER
        self.toast(f"{action.upper()}: {summary}", kind2)
        self._set_status_text(f"{action.upper()} \u2192 {summary}", color)

    def _bulk(self, action, pcs=None):
        """Run one action across every selected PC with honest per-PC
        results. Only Restart/Shutdown ask for a confirmation dialog
        (listing the affected PCs); everything else is toast-confirmed."""
        if not self.server:
            self.toast("Server not connected.", "warn")
            return
        names = [str(p) for p in (self._selected_pcs if pcs is None else pcs)]
        if not names:
            self.toast("Select at least one PC first.", "warn")
            return
        params = {}
        if action == "lock":
            params = {"message": "Locked by the administrator."}
        elif action == "pause":
            params = {"seconds": 0,
                      "message": "Paused by administrator. Please wait."}
        if action in ("restart", "shutdown"):
            verb = "Restart" if action == "restart" else "Shut down"
            listed = "\n".join(f"  \u2022 {n}" for n in names[:15])
            if len(names) > 15:
                listed += f"\n  \u2026 and {len(names) - 15} more"
            if not messagebox.askyesno(
                    f"{verb} {len(names)} PC(s)?",
                    f"{verb} the following PC(s)? The user sessions will "
                    f"be lost.\n\n{listed}", parent=self):
                return

        def worker():
            try:
                res = self.server.bulk_command(
                    action, names, admin_user=self._admin_name(),
                    params=params, timeout=8)
            except Exception as e:
                res = {n: {"success": False, "result": "FAILED", "error": str(e)}
                       for n in names}
            self.results.put(("bulk", action, res))

        threading.Thread(target=worker, daemon=True, name="bulk").start()
        self._set_status_text(f"{action.upper()} \u2192 {len(names)} PC(s) \u2026")

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
        self.client_status_lbl.configure(text="Broadcasting…", text_color=SUBTLE)

    def _toggle_share(self):
        """Start/stop the teaching screen share (Server screen -> all PCs).

        Synchronous on the Tk thread, exactly like `_observe` - the calls
        only send small START/STOP messages (the heavy frame fan-out runs
        on the Server's own worker thread).
        """
        if self.server.share_stop is not None:
            resp = self.server.stop_screen_share(self._admin_name(),
                                                 role=self.role_name)
        else:
            resp = self.server.start_screen_share(self._admin_name(),
                                                  role=self.role_name)
        if not resp.get("success"):
            self.toast(resp.get("error") or "Screen share failed.", "error")
            return
        sharing = self.server.share_stop is not None
        self.share_btn.configure(
            text="⏹ Stop Sharing" if sharing else "📺 Share Screen",
            fg_color=DANGER if sharing else ACCENT)
        if resp.get("already") or resp.get("not_sharing"):
            return                      # state merely re-synced; no news
        if sharing:
            n = len(resp.get("pcs") or [])
            self.client_status_lbl.configure(
                text=f"Sharing screen → {n} PCs", text_color=SKY)
            self.toast(f"Sharing your screen to {n} PC(s)", "success")
        else:
            self.client_status_lbl.configure(
                text="Screen share stopped", text_color=SUBTLE)
            self.toast("Screen share stopped", "info")

    def _screenshot(self):
        pc = self._selected_pc()
        if not pc:
            return

        def worker():
            resp = self.server.request_screenshot(pc, self._admin_name())
            self.results.put(("Screenshot", pc, resp))

        threading.Thread(target=worker, daemon=True).start()
        self.client_status_lbl.configure(text=f"Requesting screenshot from {pc}…",
                                      text_color=SUBTLE)

    def _send_file(self):
        """Push the picked file to the Desktop of every selected PC."""
        if not self.server:
            self.toast("Server not connected.", "warn")
            return
        names = [str(p) for p in (self._selected_pcs or [])]
        if not names:
            self.toast("Select at least one PC first.", "warn")
            return
        path = filedialog.askopenfilename(
            title=("Send file to " + names[0] if len(names) == 1
                   else f"Send file to {len(names)} PCs"),
            parent=self)
        if not path:
            return

        def worker():
            try:
                res = self.server.push_desktop_file(
                    names, path, admin=self._admin_name(),
                    role=self.role_name)
            except Exception as e:
                res = {n: {"success": False, "result": "FAILED",
                           "error": str(e)} for n in names}
            # one PC: a plain toast; several: the existing per-PC
            # SUCCESS / OFFLINE / FAILED result list.
            if len(names) == 1:
                self.results.put(("Send File", names[0],
                                  res.get(names[0]) or
                                  {"success": False, "error": "failed"}))
            else:
                self.results.put(("bulk", "send_file", res))

        threading.Thread(target=worker, daemon=True, name="sendfile").start()
        self._set_status_text(
            f"SEND FILE → {len(names)} PC(s) …", SUBTLE)

    def _observe(self):
        pc = self._selected_pc()
        if not pc:
            return
        if self._observing == pc:
            # [Observe] stays view-only and unchanged - but it must not kill
            # a screen stream that a Remote session opened for this PC.
            if not self._remote_owns_stream(pc):
                self.server.stop_screen_observe(pc, self._admin_name())
            self._observing = None
            self.client_status_lbl.configure(text=f"Observation stopped · {pc}",
                                          text_color=SUBTLE)
            self.toast(f"Stopped observing {pc}", "info")
            return
        if not self.is_admin:
            # P4: same rule as Remote - a non-administrator may end a
            # stream but may never open one.
            self.toast("Observe requires the ADMINISTRATOR role.", "error")
            return
        if self._observing and not self._remote_owns_stream(self._observing):
            self.server.stop_screen_observe(self._observing, self._admin_name())
        ok = self.server.start_screen_observe(pc, self._admin_name(),
                                              role=self.role_name)
        if ok:
            self._observing = pc
            self._open_viewer(f"Live Screen · {pc}", {}, single=False)
            self.client_status_lbl.configure(text=f"Observing {pc} live…", text_color=SKY)
            self.toast(f"Observing {pc} live", "info")
        else:
            self.client_status_lbl.configure(text=f"{pc} is offline", text_color=DANGER)
            self.toast(f"{pc} is offline", "error")

    # ---- screen viewer ---------------------------------------------------
    def _open_viewer(self, title, data, single=True):
        if self._viewer and self._viewer.winfo_exists():
            self._viewer.title(title)
            self._viewer.single = single
            self._viewer.deiconify()
            self._viewer.lift()
        else:
            win = ctk.CTkToplevel(self)
            win.title(title)
            set_app_icon(win)
            win.configure(fg_color="#0d1117")
            center_window(win, 1150, 780)
            win.minsize(900, 620)
            win.single = single
            win.label = ctk.CTkLabel(win, fg_color="#0d1117", text="Waiting for frames…",
                                 text_color="#8b949e", font=("Segoe UI", 11))
            win.label.pack(fill="both", expand=True, padx=8, pady=8)
            win.protocol("WM_DELETE_WINDOW", self._close_viewer)
            self._viewer = win
        if data:
            self._viewer_show(data, "")

    def _viewer_alive(self):
        return bool(self._viewer and self._viewer.winfo_exists())

    def _close_viewer(self):
        if self._observing and self.server:
            # never tear down a stream a Remote session owns (Task 5)
            if not self._remote_owns_stream(self._observing):
                self.server.stop_screen_observe(self._observing,
                                                self._admin_name())
            self._observing = None
        if self._viewer:
            try:
                self._viewer.destroy()
            except Exception:
                pass
            self._viewer = None

    def _viewer_show(self, data, pc_name, target=None):
        """Decode a base64 JPEG frame and fit it to the viewer window so the
        preview is large and readable at any window size (Phase G/#2).

        `target` lets the Remote Control window render into its own window
        instead of the Observe viewer; left unset, behaviour is exactly the
        original one for `[Observe]`.
        """
        win = target if target is not None else self._viewer
        if win is None or not win.winfo_exists():
            return
        img_b64 = data.get("image") if isinstance(data, dict) else None
        if not img_b64:
            return
        try:
            from PIL import Image
            raw = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(raw))
            # scale the frame down only if it does not fit the viewer.
            # Measure the LABEL the picture is drawn in (not the window):
            # the label is shorter than the window by the header bar, so
            # sizing against the window could overflow the label and clip
            # the bottom of the stream.
            lbl = getattr(win, "label", None)
            avail_w = (lbl.winfo_width() - 4) if lbl is not None else -1
            avail_h = (lbl.winfo_height() - 4) if lbl is not None else -1
            if avail_w < 100:
                avail_w = win.winfo_width() - 24
            if avail_h < 100:
                avail_h = win.winfo_height() - 24
            if avail_w < 100:
                avail_w = 1150 - 24
            if avail_h < 100:
                avail_h = 780 - 24
            if img.width > avail_w or img.height > avail_h:
                resample = getattr(Image, "Resampling", Image)
                img.thumbnail((avail_w, avail_h), resample.LANCZOS)
            photo = ctk.CTkImage(light_image=img,
                                 size=(img.width, img.height))
            win.label.configure(image=photo, text="")
            win.label.image = photo            # keep a reference
            if target is None and (pc_name or data.get("pc_name")):
                win.title(f"Screen · {pc_name or data.get('pc_name')}")
        except Exception as e:
            win.label.configure(text=f"Frame decode error: {e}")

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
        elif action == "password":
            # M2: the password dialog already showed its own feedback -
            # no session teardown here; the roster push below is what
            # makes the new password apply to every Client PC at once.
            pass
        else:
            self.toast("Account created", "success")
        # Live-sync: push the change through the server event channel so
        # every account-bearing view refreshes at once (and any other
        # listener sees it) instead of waiting for the 3 s polling tick.
        if self.server:
            try:
                self.server._emit("account_changed",
                                  {"action": action,
                                   "student_id": sid_new or sid_old})
            except Exception:
                pass
            # M2: tell every online Client PC to re-pull the auth roster,
            # so an offline sign-in can never accept an old password.
            try:
                self.server.notify_roster_changed()
            except Exception:
                pass

    def _build_students_tab(self):
        """Accounts = search -> select -> Account Details window: summary
        cards, instant filters, the account table (never the password) and
        the contextual dialogs for add/edit/delete."""
        page = self._new_page("students")
        ctk.CTkLabel(page, text="Accounts", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14,
                                               pady=(12, 4))
        ctk.CTkLabel(page,
                 text="Student accounts. Search, select a row and open "
                      "Account Details for its sessions, activity and PC "
                      "usage - passwords are never shown.",
                 fg_color=BG_LIGHT, text_color=SUBTLE, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=14, pady=(0, 6))
        frame = AccountsFrame(page, on_change=self._on_account_change,
                              notify=self.toast)
        frame.pack(fill="both", expand=True)
        self.students_frame = frame       # reachable for live tests
        # live-sync: account_changed refreshes this list immediately
        self.page_refresh["students"] = frame.refresh

    def _build_staff_tab(self):
        page = self._new_page("staff")
        # The list is in TABLE order (spec: ID, Username, Full Name,
        # Role, Email, Contact, Status, Last Login); `pos` gives the
        # FORM order inside ACCOUNT INFORMATION (Username, Full Name,
        # Email, Contact, Role, Status).  password is table-only-hidden
        # (`table: False` - never a column, never exported); last_login
        # is display-only (`form: False` - derived from the audit trail,
        # so no form can ever write it).
        fields = [
            {"name": "student_id", "label": "Username", "type": "entry",
             "required": True, "group": "ACCOUNT INFORMATION", "pos": 0},
            {"name": "full_name", "label": "Full Name", "type": "entry",
             "required": True, "group": "ACCOUNT INFORMATION", "pos": 1},
            {"name": "role", "label": "Role", "type": "combobox",
             "options": ["staff", "admin", "maintenance"],
             "required": True, "group": "ACCOUNT INFORMATION", "pos": 4},
            {"name": "email", "label": "Email", "type": "entry",
             "group": "ACCOUNT INFORMATION", "pos": 2},
            {"name": "contact", "label": "Contact", "type": "entry",
             "group": "ACCOUNT INFORMATION", "pos": 3},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": ["Active", "Inactive"],
             "group": "ACCOUNT INFORMATION", "pos": 5},
            {"name": "password",
             "label": "Password (required on add, blank keeps)",
             "type": "entry", "group": "PASSWORD MANAGEMENT", "pos": 0,
             "table": False},
            {"name": "last_login", "label": "Last Login", "type": "readonly",
             "form": False, "width": 145},
        ]
        # P2-9: still a UserCRUDFrame (same write rules) that additionally
        # opens the same Account Details window the student Accounts page
        # uses - Overview / Sessions / Activity Logs / PC Usage.
        frame = StaffAccountsFrame(page, table="users", fields=fields,
                      title="Manage Staff, Maintenance & Administrator Accounts",
                      where_clause="role IN ('staff','admin','maintenance')",
                      search_field="full_name",
                      defaults={"status": "Active", "role": "staff"},
                      on_change=self._on_account_change,
                      on_revoke=self._revoke_staff_sessions,
                      notify=self.toast
                      )
        frame.pack(fill="both", expand=True)
        self.staff_frame = frame        # reachable for live tests
        # live-sync: account_changed refreshes this list immediately
        self.page_refresh["staff"] = frame.refresh

    def _revoke_staff_sessions(self, sid):
        """Revoke Sessions button on the Staff & Admin page: the single
        place that knows the server, so the frame stays server-free.
        force_logout_user closes the sessions, audits the action and
        emits session_forced."""
        if not self.server:
            return {"success": False, "forced": 0,
                    "error": "no server connection"}
        try:
            return self.server.force_logout_user(
                sid, admin=self._admin_name(),
                reason="revoked from the Staff & Admin page")
        except Exception as e:
            return {"success": False, "forced": 0, "error": str(e)}

    # -------------------------------------------------------- computers
    def _build_computers_tab(self):
        """Simplified computer management: only the 8 useful live fields
        (PC Name, IP, Hostname, Online/Offline, Current User, CPU, RAM,
        Last Seen) - no hardware specification clutter (Phase H/#9)."""
        page = self._new_page("computers")
        ctk.CTkLabel(page, text="Manage Computers", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14, pady=(12, 4))

        # ---- form ----
        form = CardFrame(page, text="Computer details")
        form.pack(fill="x", padx=14, pady=4)
        row = ctk.CTkFrame(form)
        row.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(row, text="PC Name:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.comp_pc_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.comp_pc_var, width=114).grid(
            row=0, column=1, padx=(0, 12))

        ctk.CTkLabel(row, text="Location:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.comp_loc_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.comp_loc_var, width=114).grid(
            row=0, column=3, padx=(0, 12))

        ctk.CTkLabel(row, text="Status:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.comp_status_var = tk.StringVar(value="Available")
        ctk.CTkComboBox(row, variable=self.comp_status_var, width=124,
                     state="readonly", values=["Available", "In Use",
                                               "Maintenance", "Reserved"]).grid(
            row=0, column=5, padx=(0, 14))

        ctk.CTkButton(row, text="Save", command=self._computer_save).grid(
            row=0, column=6, padx=3)
        ctk.CTkButton(row, text="Delete", command=self._computer_delete).grid(
            row=0, column=7, padx=3)
        ctk.CTkButton(row, text="Clear", command=self._clear_comp_form).grid(
            row=0, column=8, padx=3)

        # Spec item 7: minimal PC group tag - the Website Access scope
        # selector targets "Group" PCs by this value.  Free text with the
        # existing distinct values offered as suggestions.
        ctk.CTkLabel(row, text="Group:").grid(row=1, column=0, sticky="w",
                                          padx=(0, 4), pady=(6, 0))
        self.comp_group_var = tk.StringVar()
        self.comp_group_cb = ctk.CTkComboBox(
            row, variable=self.comp_group_var, width=102)
        self.comp_group_cb.grid(row=1, column=1, padx=(0, 12), pady=(6, 0))
        ctk.CTkLabel(row,
                 text="e.g. Lab A / Lab B - used by Website Access scopes",
                 text_color=MUTED, font=("Segoe UI", 8)).grid(
            row=1, column=2, columnspan=3, sticky="w", pady=(6, 0))

        search_row = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        search_row.pack(fill="x", padx=14, pady=(6, 2))
        ctk.CTkLabel(search_row, text="Search:", fg_color=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left")
        self.comp_search_var = tk.StringVar()
        self.comp_search_var.trace_add("write", lambda *a: self._refresh_computers())
        ctk.CTkEntry(search_row, textvariable=self.comp_search_var,
                  width=162).pack(side="left", padx=6)
        ctk.CTkLabel(search_row, text="Auto-refreshes every 2 s",
                 fg_color=BG_LIGHT, text_color=MUTED,
                 font=("Segoe UI", 9)).pack(side="right")

        # ---- table (the 8 live fields + the spec-7 group tag) ----
        cols = ("id", "pc_name", "group", "ip", "hostname", "online", "user",
                "cpu", "ram", "last_seen")
        frame = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.comp_tree = ttk.Treeview(frame, columns=cols,
                                      show="tree headings", height=12)
        self.comp_tree.heading("#0", text="")
        self.comp_tree.column("#0", width=44, stretch=False, anchor="center")
        self.comp_tree.heading("id", text="ID")
        self.comp_tree.column("id", width=0, stretch=False, anchor="center")
        heads = [("pc_name", "PC Name", 120, "w"),
                 ("group", "Group", 90, "w"),
                 ("ip", "IP Address", 110, "center"),
                 ("hostname", "Hostname", 135, "w"),
                 ("online", "Online/Offline", 105, "center"),
                 ("user", "Current User", 125, "w"),
                 ("cpu", "CPU %", 60, "center"),
                 ("ram", "RAM %", 60, "center"),
                 ("last_seen", "Last Seen", 95, "center")]
        for cid, text, w, anchor in heads:
            self.comp_tree.heading(cid, text=text)
            self.comp_tree.column(cid, width=w, anchor=anchor)
        vsb = ctk.CTkScrollbar(frame, height=51, orientation="vertical", command=self.comp_tree.yview)
        self.comp_tree.configure(yscrollcommand=vsb.set)
        self.comp_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        # one palette everywhere: the same status colors as the icons/cards
        self.comp_tree.tag_configure("online",
                                     foreground=STATUS_COLORS["online"])
        self.comp_tree.tag_configure("offline",
                                     foreground=STATUS_COLORS["offline"])
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
            # never-connected spec-bearing rows (old demo data) stay hidden
            for r in conn.execute(
                    "SELECT id, pc_name, group_name, ip_address, hostname, "
                    "status, assigned_to, cpu_percent, ram_percent, "
                    "last_heartbeat, is_online FROM computers "
                    "WHERE NOT (specs IS NOT NULL AND last_heartbeat IS NULL) "
                    "ORDER BY pc_name").fetchall():
                d = dict(r)
                db_rows[d["pc_name"]] = d
                by_name[d["pc_name"]] = d
            conn.close()
        except Exception:
            pass
        self._refresh_group_options()

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
            # authoritative state: server-pushed entry first, else derived
            # from the server's own DB record (UNKNOWN when nothing is known)
            if name in self._state_by_name:
                state = status_key(self._state_by_name[name].get("state"))
            elif online:
                state = "online"
            elif db.get("last_heartbeat"):
                state = "offline"
            else:
                state = "unknown"
            kwargs = {"tags": tags, "values": (
                db.get("id", ""),
                name,
                db.get("group_name") or "",
                lv.get("ip") or db.get("ip_address") or "",
                lv.get("hostname") or db.get("hostname") or "",
                "\u25cf Online" if online else "\u25cb Offline",
                user,
                f"{float(cpu):.0f}",
                f"{float(ram):.0f}",
                last,
            )}
            icon = get_status_icon(state, 16)
            if icon is not None:
                kwargs["image"] = icon
            self.comp_tree.insert("", "end", **kwargs)
        if selected_id:
            for item in self.comp_tree.get_children():
                if str(self.comp_tree.item(item, "values")[0]) == str(selected_id):
                    self.comp_tree.selection_set(item)
                    break

    def _refresh_group_options(self):
        """Distinct PC group tags for the Group combobox (spec item 7)."""
        if not hasattr(self, "comp_group_cb"):
            return
        try:
            conn = get_connection()
            vals = [r[0] for r in conn.execute(
                "SELECT DISTINCT group_name FROM computers "
                "WHERE group_name IS NOT NULL AND group_name != '' "
                "ORDER BY group_name").fetchall()]
            conn.close()
            self.comp_group_cb.configure(values=vals)
        except Exception:
            pass

    def _on_comp_select(self, event=None):
        sel = self.comp_tree.selection()
        if not sel:
            return
        vals = self.comp_tree.item(sel[0], "values")
        self._comp_selected_id = vals[0] or None
        self.comp_pc_var.set(vals[1])
        # location/status/group come from the DB row
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT location, status, group_name FROM computers WHERE id=?",
                (self._comp_selected_id,)).fetchone()
            conn.close()
            self.comp_loc_var.set(row["location"] if row else "")
            self.comp_status_var.set(row["status"] if row else "Available")
            self.comp_group_var.set(
                (row["group_name"] if row else "") or "")
        except Exception:
            pass

    def _clear_comp_form(self):
        self._comp_selected_id = None
        self.comp_pc_var.set("")
        self.comp_loc_var.set("")
        self.comp_status_var.set("Available")
        self.comp_group_var.set("")
        self.comp_tree.selection_remove(self.comp_tree.selection())

    def _computer_save(self):
        name = self.comp_pc_var.get().strip()
        if not name:
            self.toast("PC name is required.", "warn")
            return
        loc = self.comp_loc_var.get().strip()
        status = self.comp_status_var.get() or "Available"
        group = self.comp_group_var.get().strip()
        conn = get_connection()
        try:
            if self._comp_selected_id:
                conn.execute(
                    "UPDATE computers SET pc_name=?, location=?, status=?, "
                    "group_name=? WHERE id=?",
                    (name, loc, status, group, self._comp_selected_id))
                msg = f"Computer {name} updated"
            else:
                conn.execute(
                    "INSERT INTO computers (pc_name, location, status, "
                    "group_name) VALUES (?,?,?,?)",
                    (name, loc, status, group))
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
        # Spec item 5: full item lifecycle - integer quantities with the
        # available/assigned split, status, bookkeeping dates + notes.
        fields = [
            {"name": "item_name", "label": "Item Name", "type": "entry",
             "required": True},
            {"name": "category", "label": "Category", "type": "entry"},
            {"name": "quantity", "label": "Quantity", "type": "entry"},
            {"name": "available_qty", "label": "Available Qty", "type": "entry"},
            {"name": "assigned_qty", "label": "Assigned Qty", "type": "entry"},
            {"name": "status", "label": "Status", "type": "combobox",
             "options": InventoryCRUDFrame.STATUSES},
            {"name": "condition_status", "label": "Condition", "type": "combobox",
             "options": ["New", "Good", "Fair", "Damaged", "For Disposal"]},
            {"name": "location", "label": "Location", "type": "entry"},
            {"name": "notes", "label": "Notes", "type": "entry"},
            {"name": "date_added", "label": "Date Added", "type": "entry"},
            {"name": "last_updated", "label": "Last Updated", "type": "entry"},
        ]
        # The 11-field form + stats strip + full-height table ask 735px,
        # but the content area only offers ~625px at the 1080x700 floor
        # (the sweep's "inventory: overflow").  Only this page overflows,
        # so let it scroll at its natural height instead of squeezing the
        # table: fill="x" keeps the frame's requested height intact.
        _inv_scroll = ctk.CTkScrollableFrame(page, fg_color=BG_LIGHT)
        _inv_scroll.pack(fill="both", expand=True)
        self.inventory_frame = InventoryCRUDFrame(
            _inv_scroll, table="inventory", fields=fields,
            title="Computer Lab Inventory",
            search_field="item_name", notify=self.toast,
            defaults={"date_added": now_date, "last_updated": now_date,
                      "status": "AVAILABLE"},
        )
        self.inventory_frame.pack(fill="x")

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
                  defaults={"borrow_date": now_date, "status": "Pending Approval"},
                  notify=self.toast
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
                  defaults={"date_reported": now_date, "status": "Pending"},
                  notify=self.toast).pack(fill="both", expand=True)

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
                            "date_posted": now_date},
                  notify=self.toast
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
                            "timestamp": now_datetime, "is_read": "No"},
                  notify=self.toast
                  ).pack(fill="both", expand=True)

    # --------------------------------------------------------- sessions
    # ------------------------------------------------------ website access
    # Website Access (spec items 6/7/8) --------------------------------
    WEB_MODE_LABELS = {"allow_all": "ALLOW ALL",
                       "block_list": "BLOCK LIST",
                       "allow_only": "ALLOW ONLY"}
    # short, plain-language description shown under each option card
    WEB_MODE_DESCS = {
        "allow_all": "No restrictions - every website opens.",
        "block_list": "Everything opens except the blocked domains.",
        "allow_only": "Only the allowed domains open; all others are "
                      "blocked.",
    }
    # compact per-PC policy status shown in the status table (display only)
    WEB_STATUS_LABELS = {"": "Not set", "SYNCED": "Synced",
                         "SYNCING": "Syncing", "OFFLINE": "Offline",
                         "FAILED": "Failed"}

    def _build_websites_tab(self):
        """Website Access as a simple admin control panel: pick a mode
        (three option cards), choose the scope, Apply Policy, manage every
        rule in ONE unified table with instant filters, and watch the
        per-PC policy status underneath (spec items 6, 7, 8)."""
        page = self._new_page("websites")
        ctk.CTkLabel(page, text="Website Access", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14,
                                               pady=(12, 4))
        ctk.CTkLabel(page,
                 text="Rules and modes are versioned and pushed to every "
                      "Client PC automatically.",
                 fg_color=BG_LIGHT, text_color=SUBTLE, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))

        # ---- row 1: mode as three option cards (spec item 6) ----
        mrow = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        mrow.pack(fill="x", padx=14, pady=(0, 4))
        # equal-weight columns keep the three cards exactly the same size
        # however their descriptions happen to wrap
        for _col in range(3):
            mrow.columnconfigure(_col, weight=1)
        default_mode = "allow_all"
        try:
            m = get_connection().execute(
                "SELECT value FROM system_settings WHERE key='web_mode'"
            ).fetchone()
            if m and m["value"] in self.WEB_MODE_LABELS:
                default_mode = m["value"]
        except Exception:
            pass
        self.web_mode_var = tk.StringVar(value=default_mode)
        self._mode_cards = {}
        pickables = []                # (widget, mode) that selects a mode
        for _col, mode in enumerate(("allow_all", "block_list", "allow_only")):
            card = PaddedFrame(mrow, fg_color=CARD, border_color=BORDER,
                            border_width=1, padx=10, pady=6)
            card.grid(row=0, column=_col, sticky="nsew", padx=(0, 8))
            ctk.CTkRadioButton(card, text=self.WEB_MODE_LABELS[mode],
                           variable=self.web_mode_var, value=mode,
                           fg_color=ACCENT, border_color=MUTED,
                           hover_color=ACCENT, text_color=TEXT,
                           font=("Segoe UI", 9, "bold"), 
                           
                           command=self._sync_mode_cards).pack(
                fill="x")
            # width == wraplength: every desc requests the same width, so the
            # equal-weight grid columns come out exactly the same size
            desc = ctk.CTkLabel(card, text=self.WEB_MODE_DESCS[mode], fg_color=CARD,
                            text_color=SUBTLE, font=("Segoe UI", 8), justify="left",
                            anchor="w", wraplength=230, width=230)
            desc.pack(fill="x", pady=(2, 0))
            self._mode_cards[mode] = card
            pickables += [(card, mode), (desc, mode)]
        for widget, mode in pickables:      # clicking the card picks it
            widget.bind("<Button-1>",
                        lambda _e, m=mode: self._pick_mode(m))
        # a programmatic set() keeps the highlight in sync too
        self.web_mode_var.trace_add(
            "write", lambda *_a: self._sync_mode_cards())
        self._sync_mode_cards()

        # ---- row 2: scope + prominent Apply (spec item 7) ----
        srow = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        srow.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkLabel(srow, text="Apply to:", fg_color=BG_LIGHT,
                 text_color=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.web_scope_var = tk.StringVar(value="all")
        for scope, label in (("all", "All PCs"),
                             ("selected", "Selected PCs"),
                             ("individual", "Individual PC"),
                             ("group", "Group")):
            ctk.CTkRadioButton(srow, text=label, variable=self.web_scope_var,
                           value=scope, fg_color=ACCENT, border_color=MUTED,
                           hover_color=ACCENT, text_color=TEXT,
                           font=("Segoe UI", 9)).pack(side="left", padx=4)
        self.web_individual_cb = ctk.CTkComboBox(srow, width=120,
                                              state="readonly")
        self.web_individual_cb.pack(side="left", padx=4)
        self.web_group_cb = ctk.CTkComboBox(srow, width=78, state="readonly")
        self.web_group_cb.pack(side="left", padx=4)
        ctk.CTkButton(srow, text="Apply Policy", command=self._apply_web_mode,
                  fg_color=ACCENT, text_color="white", hover_color=ACCENT_DARK,
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right", padx=(10, 0))

        # ---- unified rules table with instant filters (spec item 6) ----
        rules_lf = CardFrame(page, text="Website rules")
        rules_lf.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.web_rules = WebsiteRulesFrame(
            rules_lf, on_change=self._on_website_change, notify=self.toast)
        self.web_rules.pack(fill="both", expand=True, padx=4, pady=4)

        # ---- per-PC policy status, below the rules (spec items 7 + 8) ----
        st_frame = CardFrame(page, text="Policy status by PC")
        st_frame.pack(fill="x", padx=14, pady=(0, 4))
        self.web_status_tree = ttk.Treeview(
            st_frame, columns=("pc", "mode", "status"),
            show="headings", height=4)
        for cid, text, w in (("pc", "PC", 200),
                             ("mode", "Mode", 160),
                             ("status", "Policy Status", 160)):
            self.web_status_tree.heading(cid, text=text)
            self.web_status_tree.column(cid, width=w, anchor="w")
        vsb = ctk.CTkScrollbar(st_frame, height=51, orientation="vertical",
                            command=self.web_status_tree.yview)
        self.web_status_tree.configure(yscrollcommand=vsb.set)
        self.web_status_tree.pack(side="left", fill="both", expand=True,
                                  padx=(6, 0), pady=4)
        vsb.pack(side="left", fill="y", pady=4)
        # statuses get the same palette as the rest of the app; the tag
        # stays the raw status so the colors keep matching
        self.web_status_tree.tag_configure("SYNCED", foreground=SUCCESS)
        self.web_status_tree.tag_configure("SYNCING", foreground=WARN)
        self.web_status_tree.tag_configure("FAILED", foreground=DANGER)
        self.web_status_tree.tag_configure("OFFLINE", foreground=OFFLINE)

        self._refresh_web_options()
        self._refresh_web_status()
        self.page_refresh["websites"] = self._refresh_websites_page

    def _pick_mode(self, mode):
        """An option card was clicked: select the mode + re-highlight."""
        if mode in self.WEB_MODE_LABELS:
            self.web_mode_var.set(mode)     # trace keeps the cards in sync
            self._sync_mode_cards()

    def _sync_mode_cards(self):
        """Outline the selected mode card with the accent color."""
        cards = getattr(self, "_mode_cards", None) or {}
        selected = getattr(self, "web_mode_var", None)
        selected = selected.get() if selected is not None else None
        for mode, card in cards.items():
            card.configure(
                border_color=ACCENT if mode == selected else BORDER)

    def _refresh_websites_page(self):
        for fn in (self._refresh_web_options, self._refresh_web_status,
                   getattr(self, "web_rules", None) and
                   self.web_rules.refresh):
            try:
                if callable(fn):
                    fn()
            except Exception:
                pass

    def _refresh_web_options(self):
        """PC + group combo options for the scope selector (spec 7)."""
        if not hasattr(self, "web_individual_cb"):
            return
        try:
            conn = get_connection()
            pcs = [r[0] for r in conn.execute(
                "SELECT pc_name FROM computers ORDER BY pc_name").fetchall()]
            groups = [r[0] for r in conn.execute(
                "SELECT DISTINCT group_name FROM computers "
                "WHERE group_name IS NOT NULL AND group_name != '' "
                "ORDER BY group_name").fetchall()]
            conn.close()
            self.web_individual_cb.configure(values=pcs)
            self.web_group_cb.configure(values=groups)
        except Exception:
            pass

    def _refresh_web_status(self):
        """PC | Mode | Policy Status table (spec items 7-8): desired mode
        and the current sync state of exactly that PC's policy."""
        tree = getattr(self, "web_status_tree", None)
        if tree is None:
            return
        try:
            conn = get_connection()
            rows = conn.execute(
                "SELECT pc_name, mode, sync_status FROM ("
                "  SELECT c.pc_name AS pc_name,"
                "         COALESCE(p.mode,"
                "                  (SELECT value FROM system_settings"
                "                   WHERE key='web_mode'),"
                "                  'allow_all') AS mode,"
                "         COALESCE(p.sync_status, '') AS sync_status"
                "  FROM computers c"
                "  LEFT JOIN web_pc_policy p ON p.pc_name = c.pc_name"
                "  UNION ALL"
                "  SELECT p.pc_name, p.mode, p.sync_status"
                "  FROM web_pc_policy p"
                "  WHERE p.pc_name NOT IN (SELECT pc_name FROM computers)"
                ") ORDER BY pc_name").fetchall()
            conn.close()
        except Exception:
            return
        for item in tree.get_children():
            tree.delete(item)
        for r in rows:
            status = r["sync_status"] or ""       # "" = never pushed yet
            mode = r["mode"] or "allow_all"
            tree.insert("", "end",
                        values=(r["pc_name"],
                                self.WEB_MODE_LABELS.get(mode, mode),
                                self.WEB_STATUS_LABELS.get(status, status)),
                        tags=(status,))

    def _apply_web_mode(self):
        """Spec item 7: push the selected mode to the selected scope."""
        if not self.server:
            self.toast("Connect the server to push Website Access policy.",
                       "warn")
            return
        mode = self.web_mode_var.get()
        scope = self.web_scope_var.get()
        targets = None                     # None = every PC ("All PCs")
        label = "All PCs"
        if scope == "selected":
            sel = [self.web_status_tree.item(i, "values")[0]
                   for i in self.web_status_tree.selection()]
            if not sel:
                self.toast("Select one or more PCs in the status table "
                           "first.", "warn")
                return
            targets, label = sel, f"{len(sel)} selected PC(s)"
        elif scope == "individual":
            pc = self.web_individual_cb.get().strip()
            if not pc:
                self.toast("Choose an individual PC first.", "warn")
                return
            targets, label = [pc], pc
        elif scope == "group":
            group = self.web_group_cb.get().strip()
            if not group:
                self.toast("Choose a group first.", "warn")
                return
            conn = get_connection()
            members = [r[0] for r in conn.execute(
                "SELECT pc_name FROM computers WHERE group_name=? "
                "ORDER BY pc_name", (group,)).fetchall()]
            conn.close()
            if not members:
                self.toast(f"No PCs carry the group '{group}'.", "warn")
                return
            targets, label = members, f"group '{group}'"
        # applying to several PCs at once is worth a confirmation (spec
        # item 7 / UX rule: dialogs only for bulk + destructive actions)
        if targets is None:
            pc_count = len(self.web_status_tree.get_children())
            if pc_count <= 0:
                try:
                    conn = get_connection()
                    pc_count = conn.execute(
                        "SELECT COUNT(*) FROM computers").fetchone()[0]
                    conn.close()
                except Exception:
                    pc_count = 0
        else:
            pc_count = len(targets)
        if pc_count > 1 and not messagebox.askyesno(
                "Apply website policy",
                f"Apply {self.WEB_MODE_LABELS.get(mode, mode)} to "
                f"{label} ({pc_count} PCs)?\n"
                f"The new policy will be pushed to every listed PC."):
            return
        try:
            res = self.server.set_web_mode(mode, pc_names=targets,
                                           admin_user=self._admin_name())
        except Exception as e:
            self.toast(f"Could not apply policy: {e}", "error")
            return
        if not res.get("success"):
            self.toast(res.get("error") or "Could not apply policy.",
                       "error")
            return
        version = res.get("version", 0)
        sent = res.get("sent", 0)
        offline = res.get("offline", 0)
        try:
            self.server._log_activity(
                self._admin_name(), "webfilter_change", label,
                f"mode {self.WEB_MODE_LABELS.get(mode, mode)} applied; "
                f"policy v{version}; pushed to {sent} online PC(s), "
                f"{offline} offline")
        except Exception:
            pass
        self._refresh_web_status()
        self.toast(f"Policy v{version} applied to {label} \u2192 {sent} "
                   f"online PC(s), {offline} offline.", "success")

    def _on_website_change(self, action, data):
        """Website Access hook: every rule change creates a new policy
        version (spec item 8), re-pushes it to all Client PCs and is
        audited (who, what, how many PCs received it)."""
        domain = str((data or {}).get("domain", "")).strip()
        sent = offline = 0
        version = 0
        if self.server:
            try:
                version = self.server.bump_web_policy_version()
                pushed = self.server.push_web_filter(self._admin_name(),
                                                     version=version)
                sent = pushed.get("sent", 0)
                offline = pushed.get("offline", 0)
            except Exception:
                sent = offline = 0
            try:
                self.server._log_activity(
                    self._admin_name(), "webfilter_change", domain,
                    f"{action}; policy v{version}; pushed to {sent} "
                    f"online PC(s), {offline} offline")
            except Exception:
                pass
        self._refresh_web_status()
        rules = getattr(self, "web_rules", None)
        if rules is not None:
            try:
                rules.refresh()          # keep the unified table in sync
            except Exception:
                pass
        past = {"add": "added", "update": "updated",
                "delete": "deleted"}.get(action, action)
        if sent or offline:
            self.toast(f"Website {past}: '{domain}' \u2192 policy v{version} "
                       f"sent to {sent} online PC(s)",
                       "success" if sent else "warn")
        else:
            self.toast(f"Website {past}: '{domain}'", "success")

    def _build_sessions_tab(self):
        """Sessions = read-only monitor: summary cards, instant filters,
        the server-recorded history with a Session Details panel and the
        two contextual actions (Force Logout, View Activity)."""
        page = self._new_page("sessions")
        ctk.CTkLabel(page, text="Sessions", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14,
                                               pady=(12, 4))
        ctk.CTkLabel(page,
                 text="Server-recorded session history. Sessions are never "
                      "edited here - the Server writes them; select a row "
                      "to inspect it or act on the account.",
                 fg_color=BG_LIGHT, text_color=SUBTLE, font=("Segoe UI", 9),
                 justify="left").pack(anchor="w", padx=14, pady=(0, 6))
        frame = SessionsFrame(page, on_force_logout=self._session_force_logout,
                              on_view_activity=self._session_view_activity,
                              notify=self.toast)
        frame.pack(fill="both", expand=True)
        self.sessions_frame = frame       # reachable for live tests
        # sessions auto-refresh so forced logouts show up immediately
        self.page_refresh["sessions"] = frame.refresh

    def _session_force_logout(self, student_id):
        """Sessions page -> server-authoritative Force Logout."""
        if not self.server:
            return {"success": False, "error": "The Server is not running."}
        try:
            return self.server.force_logout_user(
                student_id, admin=self._admin_name(),
                reason="Forced from the Sessions page")
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _session_view_activity(self, needle):
        """Sessions page -> Audit Trail pre-filtered on that account."""
        try:
            self.audit_mode = "events"
            self.audit_search_var.set(str(needle or ""))
            self.audit_date_var.set("")
            self.audit_status_var.set("All")
            self.audit_type_var.set("All Types")
            self._set_audit_mode("events")
            self.show_page("activity")
        except Exception:
            pass

    # ------------------------------------------------------ activity log
    def _build_activity_tab(self):
        """Audit trail with filtering/searching over both client events and
        server commands (Phase J/#10-14)."""
        page = self._new_page("activity")
        ctk.CTkLabel(page, text="Activity Log / Audit Trail", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14, pady=(12, 4))

        # ---- filter row 1: mode toggles + Refresh (right) ----
        bar = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        bar.pack(fill="x", padx=14, pady=(0, 4))

        self.audit_mode = "events"
        self._audit_btns = {}
        for key, text in (("events", "Client / User Events"),
                          ("commands", "Server Commands")):
            b = ctk.CTkButton(bar, text=text, cursor="hand2",
                          font=("Segoe UI", 9, "bold"), 
                          command=lambda k=key: self._set_audit_mode(k))
            b.pack(side="left", padx=(0, 6))
            self._audit_btns[key] = b

        ctk.CTkButton(bar, text="Refresh", command=self._refresh_activity,
                  fg_color=ACCENT, text_color="white", cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right")

        # ---- filter row 2: Search / Date / Result / Type ----
        # (split over two rows so every filter fits one 1280-px screen)
        bar2 = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        bar2.pack(fill="x", padx=14, pady=(0, 4))

        ctk.CTkLabel(bar2, text="Search:", fg_color=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.audit_search_var = tk.StringVar()
        self.audit_search_var.trace_add("write", lambda *a: self._refresh_activity())
        ctk.CTkEntry(bar2, textvariable=self.audit_search_var, width=138).pack(side="left")

        ctk.CTkLabel(bar2, text="Date (YYYY-MM-DD):", fg_color=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_date_var = tk.StringVar()
        self.audit_date_var.trace_add("write", lambda *a: self._refresh_activity())
        ctk.CTkEntry(bar2, textvariable=self.audit_date_var, width=78).pack(side="left")

        ctk.CTkLabel(bar2, text="Result:", fg_color=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_status_var = tk.StringVar(value="All")
        self.audit_status_cb = ctk.CTkComboBox(
            bar2, variable=self.audit_status_var, width=84, state="readonly",
            values=["All", "Sent", "Done", "Failed"])
        self.audit_status_cb.pack(side="left")
        self.audit_status_cb.bind("<<ComboboxSelected>>",
                                  lambda e: self._refresh_activity())

        ctk.CTkLabel(bar2, text="Type:", fg_color=BG_LIGHT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 4))
        self.audit_type_var = tk.StringVar(value="All Types")
        self.audit_type_cb = ctk.CTkComboBox(
            bar2, variable=self.audit_type_var, width=154, state="readonly",
            values=["All Types"] + list(AUDIT_TAXONOMY) + ["Other"])
        self.audit_type_cb.pack(side="left")
        self.audit_type_cb.bind("<<ComboboxSelected>>",
                                lambda e: self._refresh_activity())

        # ---- table ----
        frame = ctk.CTkFrame(page, fg_color=BG_LIGHT)
        frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.audit_tree = ttk.Treeview(frame, show="tree headings")
        self.audit_tree.heading("#0", text="")
        self.audit_tree.column("#0", width=44, stretch=False, anchor="center")
        vsb = ctk.CTkScrollbar(frame, height=51, orientation="vertical", command=self.audit_tree.yview)
        hsb = ctk.CTkScrollbar(frame, width=51, orientation="horizontal", command=self.audit_tree.xview)
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
            b.configure(fg_color=ACCENT if active else "#dde3f0",
                     text_color="white" if active else BG_DARK)
        # (re)create columns
        for col in self.audit_tree["columns"]:
            self.audit_tree.heading(col, text="")
        if mode == "events":
            cols = ("timestamp", "admin_user", "action", "target", "details")
            heads = [("timestamp", "Timestamp", 150), ("admin_user", "User", 120),
                     ("action", "Action", 160), ("target", "Target PC", 130),
                     ("details", "Details / Result", 460)]
            self.audit_status_cb.configure(state="disabled")
            self.audit_type_cb.configure(state="normal")   # taxonomy = events only
        else:
            cols = ("created_at", "admin_user", "command_type", "target_pc",
                    "status", "result")
            heads = [("created_at", "Timestamp", 150), ("admin_user", "Admin", 120),
                     ("command_type", "Command", 150), ("target_pc", "Target PC", 130),
                     ("status", "Result", 90), ("result", "Details", 380)]
            self.audit_status_cb.configure(state="normal")
            self.audit_type_cb.configure(state="disabled")
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
                # event-type (taxonomy) filter
                cat = self.audit_type_var.get()
                if cat and cat != "All Types":
                    if cat == "Other":
                        # everything the taxonomy does NOT know
                        known, all_pfx = set(), []
                        for e, pf in AUDIT_TAXONOMY.values():
                            known |= e
                            all_pfx += list(pf)
                        sub = []
                        if known:
                            sub.append("action NOT IN (%s)"
                                       % ",".join("?" * len(known)))
                            params += sorted(known)
                        for pf in all_pfx:
                            sub.append("action NOT LIKE ?")
                            params.append(pf + "%")
                        clauses.append("(" + " AND ".join(sub) + ")")
                    else:
                        exact, prefixes = AUDIT_TAXONOMY.get(cat, (set(), ()))
                        sub = []
                        if exact:
                            sub.append("action IN (%s)"
                                       % ",".join("?" * len(exact)))
                            params += sorted(exact)
                        for pf in prefixes:
                            sub.append("action LIKE ?")
                            params.append(pf + "%")
                        clauses.append("(" + " OR ".join(sub) + ")")
                if clauses:
                    q += " WHERE " + " AND ".join(clauses)
                q += " ORDER BY id DESC LIMIT 400"
                rows = conn.execute(q, params).fetchall()
                data = [(r["timestamp"], r["admin_user"],
                         audit_action_label(r["action"]),
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
            kw = {"values": row, "tags": tags}
            # status icon of the target PC when it is a known PC
            tgt = str(row[3] if len(row) > 3 else "")
            st = self._state_by_name.get(tgt)
            if st is not None:
                icon = get_status_icon(status_key(st.get("state")), 16)
                if icon is not None:
                    kw["image"] = icon
            self.audit_tree.insert("", "end", **kw)

    # -------------------------------------------------------- settings
    # Which keys live under which collapsible card (spec: settings grouped
    # by area instead of one long flat list).  Any key not listed here
    # falls into a trailing "GENERAL" card, so a setting added later is
    # still editable instead of silently disappearing from the page.
    SETTINGS_GROUPS = (
        ("NETWORK", ("server_port", "heartbeat_interval", "command_timeout")),
        ("SECURITY", ("max_failed_logins", "lockout_duration",
                      "max_offline_days")),
        ("SCREEN OBSERVATION", ("screen_observe_interval",
                                "screenshot_quality", "screenshot_scale")),
        ("WEBSITE ACCESS", ("upstream_dns", "web_mode",
                            "web_policy_version")),
        ("INVENTORY", ("low_stock_threshold",)),
    )

    # Save-time validation: key -> (kind, bound_a, bound_b).
    #   int    whole number inside [bound_a, bound_b]
    #   float  number inside [bound_a, bound_b]
    #   choice exactly one of the values in bound_a
    #   text   non-empty, at most bound_b characters
    # A key with no rule (a setting the seeds don't know about) is saved
    # verbatim - there is nothing to validate it against.
    SETTINGS_RULES = {
        "server_port": ("int", 1, 65535),
        "heartbeat_interval": ("int", 1, 86400),
        "command_timeout": ("int", 1, 86400),
        "max_failed_logins": ("int", 1, 1000),
        "lockout_duration": ("int", 0, 86400),
        "max_offline_days": ("int", 0, 36500),
        "screen_observe_interval": ("float", 0.1, 86400.0),
        "screenshot_quality": ("int", 1, 100),
        "screenshot_scale": ("float", 0.1, 1.0),
        "low_stock_threshold": ("int", 0, 1000000),
        "web_mode": ("choice", ("allow_all", "block_list", "allow_only"), None),
        "web_policy_version": ("int", 0, 10 ** 9),
        "upstream_dns": ("text", 1, 255),
    }

    def _build_settings_tab(self):
        """System settings, grouped into collapsible cards with validated
        inputs and one Save Changes action.

        The body scrolls: the five groups ask for more height than the
        1080x700 minimum window has, and section 15 wants a scrollable
        frame rather than clipped rows - the old flat list used to push
        its own save button off the bottom edge."""
        page = self._new_page("settings")
        ctk.CTkLabel(page, text="System Settings", font=FONT_HEADER,
                 fg_color=BG_LIGHT, text_color=TEXT_LIGHT).pack(anchor="w", padx=14,
                                                pady=(12, 2))
        ctk.CTkLabel(page,
                 text="Values are stored centrally in the server database. "
                      "A port change applies after the server restarts. "
                      "Invalid fields are outlined in red and nothing is "
                      "saved until they are fixed.",
                 font=("Segoe UI", 9), text_color=SUBTLE, fg_color=BG_LIGHT,
                 # one line is 884px - wider than the 832px the minimum
                 # 1080x700 window leaves, so wrap it (spec: no clipped text)
                 wraplength=790).pack(
            anchor="w", padx=14, pady=(0, 6))
        # Fixed requested height + expand: the page never demands more than
        # the minimum window gives it, while a larger window simply shows
        # more of the list at once.
        body = ctk.CTkScrollableFrame(page, fg_color=BG_LIGHT, height=430)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self._settings_vars = {}
        self._settings_labels = {}
        self._settings_fields = {}
        self._settings_borders = {}
        try:
            conn = get_connection()
            rows = conn.execute(
                "SELECT key, value, description FROM system_settings "
                "ORDER BY key").fetchall()
            conn.close()
        except Exception:
            rows = []
        by_key = {r["key"]: r for r in rows}
        groups = [(name, [k for k in keys if k in by_key])
                  for name, keys in self.SETTINGS_GROUPS]
        grouped = {k for _, keys in groups for k in keys}
        extras = sorted(k for k in by_key if k not in grouped)
        if extras:
            groups.append(("GENERAL", extras))
        for name, keys in groups:
            if keys:
                self._build_settings_group(body, name,
                                           [by_key[k] for k in keys])
        ctk.CTkButton(page, text="Save Changes", command=self._save_settings,
                  fg_color=ACCENT, text_color="white", cursor="hand2",
                  font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=10)

    def _build_settings_group(self, parent, name, rows):
        """One collapsible group: a full-width header button (▾ / ▸) over
        its setting rows.  The rows keep the exact white-pill styling of
        the old flat list, so the contrast verified for this page still
        holds; only the grouping and the toggle are new."""
        host = ctk.CTkFrame(parent, fg_color=BG_LIGHT)
        rows_box = ctk.CTkFrame(host, fg_color=BG_LIGHT)
        header = ctk.CTkButton(
            host, text=f" \u25be  {name}", anchor="w",
            fg_color=BG_DARK, text_color=SUBTLE, hover_color="#2a3760",
            font=("Segoe UI", 9, "bold"), cursor="hand2")

        def toggle():
            if rows_box.winfo_ismapped():
                rows_box.pack_forget()
                header.configure(text=f" \u25b8  {name}")
            else:
                rows_box.pack(fill="x", pady=(0, 6))
                header.configure(text=f" \u25be  {name}")

        header.configure(command=toggle)
        host.pack(fill="x", pady=(0, 4))
        header.pack(fill="x", pady=(8, 3))
        rows_box.pack(fill="x", pady=(0, 6))
        for r in rows:
            key = r["key"]
            rowf = ctk.CTkFrame(rows_box, fg_color="white")
            rowf.pack(fill="x", pady=3)
            # CTkEntry asks for 28px against the 21px tk.Entry it replaced, so
            # the row padding drops 6 -> 3 to keep each row at the 33px the Tk
            # version had (the old list had already pushed its save button
            # off-screen before these cards were introduced).
            ctk.CTkLabel(rowf, text=r["description"] or key, fg_color="white",
                     text_color=BG_DARK,
                     font=("Segoe UI", 9, "bold"), width=318, anchor="w",
                     justify="left", wraplength=430).pack(
                side="left", padx=(10, 6), pady=3)
            var = tk.StringVar(
                value=str(r["value"] if r["value"] is not None else ""))
            rule = self.SETTINGS_RULES.get(key)
            if rule and rule[0] == "choice":
                # a fixed set of modes reads better than free text here
                widget = ctk.CTkComboBox(rowf, variable=var,
                                         values=list(rule[1]),
                                         width=138, state="readonly")
            else:
                widget = ctk.CTkEntry(rowf, textvariable=var, width=138)
            widget.pack(side="left", padx=8, pady=3)
            self._settings_vars[key] = var
            self._settings_labels[key] = str(r["description"] or key)
            self._settings_fields[key] = widget
            try:
                self._settings_borders[key] = (widget.cget("border_width"),
                                               widget.cget("border_color"))
            except Exception:
                self._settings_borders[key] = None

    @staticmethod
    def _validate_setting(raw, rule):
        """Return None when `raw` is acceptable for `rule`, otherwise a
        plain sentence saying what the field needs (section 27: errors say
        what happened and how to fix it)."""
        if not rule:
            return None
        kind, bound_a, bound_b = rule
        if kind in ("int", "float"):
            try:
                number = int(raw, 10) if kind == "int" else float(raw)
            except (TypeError, ValueError):
                return (("must be a whole number" if kind == "int"
                         else "must be a number") + f", not {raw!r}")
            if not bound_a <= number <= bound_b:
                return f"must be between {bound_a} and {bound_b}, not {number}"
            return None
        if kind == "choice":
            if raw not in tuple(bound_a):
                return "must be one of: " + ", ".join(bound_a) + f", not {raw!r}"
            return None
        if not raw:
            return "cannot be empty"
        if bound_b and len(raw) > bound_b:
            return f"must be at most {bound_b} characters, not {len(raw)}"
        return None

    def _paint_field(self, key, bad):
        """Outline a rejected field in the danger colour, and restore its
        original border as soon as the value validates again."""
        widget = getattr(self, "_settings_fields", {}).get(key)
        base = getattr(self, "_settings_borders", {}).get(key)
        if widget is None or not widget.winfo_exists() or not base:
            return
        try:
            if bad:
                widget.configure(border_width=2, border_color=DANGER)
            else:
                widget.configure(border_width=base[0], border_color=base[1])
        except Exception:
            pass

    def _save_settings(self):
        """Validate every field first and write only when all of them are
        correct: one bad number must never leave half the settings saved,
        and the message names the setting, what it got and what it takes."""
        from database import set_setting
        problems, clean, first_bad = [], {}, None
        for key, var in getattr(self, "_settings_vars", {}).items():
            raw = str(var.get()).strip()
            reason = self._validate_setting(raw, self.SETTINGS_RULES.get(key))
            self._paint_field(key, reason is not None)
            if reason:
                if first_bad is None:
                    first_bad = key
                problems.append(f"{self._settings_labels.get(key, key)} {reason}")
            else:
                clean[key] = raw
        if problems:
            # nothing is written until every field is valid
            field = getattr(self, "_settings_fields", {}).get(first_bad)
            if field is not None and field.winfo_exists():
                try:
                    field.focus_set()
                except Exception:
                    pass
            self.toast(
                "Not saved - " + problems[0]
                + (f" ({len(problems) - 1} more invalid)"
                   if len(problems) > 1 else "")
                + ". Correct it, then press Save Changes.",
                "error", duration=6000)
            return
        for key, value in clean.items():
            set_setting(key, value)
        self.toast("Settings saved", "success")
