"""
crud_frame.py
A generic, reusable "table + form" CRUD component built on top of a single
SQLite table. Every management tab in the admin dashboard (students,
computers, inventory, borrowing, maintenance, announcements, sessions)
is built from this one class, configured differently.
"""

from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox
from database import (get_connection, hash_password, get_setting,
                      DEFAULT_CLIENT_PASSWORD)
from utils import (export_rows_to_csv, FONT_HEADER, ACCENT, ORANGE, now_date,
                   FONT_SMALL, FONT_STAT_NUM, STAT_TILE_H, BG_DARK, SUBTLE,
                   MUTED, BORDER, SUCCESS, CRIMSON, DANGER, center_window,
                   set_app_icon, TEXT)
import customtkinter as ctk
from components import CardFrame, PaddedFrame, TabHost, Divider


class CRUDFrame(ctk.CTkFrame):
    # --- form/table projection (spec: the Staff & Admin form is split
    # into sections and shows columns that are not writable inputs) ---
    # A field dict may opt OUT of the table ({"table": False}), opt OUT
    # of the form ({"form": False}), carry an explicit form order
    # ({"pos": n} - so table order and form order can differ without
    # either drifting) and belong to a form section header
    # ({"group": "NAME"}).  Frames without those keys build exactly the
    # plain grid + table they always did.
    form_title = "Record details"

    def __init__(self, parent, table, fields, title,
                 fixed_values=None, where_clause=None, order_by=None,
                 on_change=None, id_field="id", allow_delete=True,
                 search_field=None, defaults=None, notify=None):
        """
        table         : SQL table name
        fields        : list of dicts: {name, label, type: entry|combobox|readonly, options?}
        title         : heading text shown at the top of the tab
        fixed_values  : dict of extra column->value always applied on INSERT (e.g. role='student')
        where_clause  : SQL WHERE clause (without 'WHERE') restricting rows shown, e.g. "role='student'"
        order_by      : SQL ORDER BY column
        on_change     : optional callback() fired after any insert/update/delete (e.g. refresh dropdowns)
        search_field  : field name to filter the table live via a search box
        notify        : optional toast callback(text, kind) - when provided,
                        normal events (hints, save results, export) never open
                        a dialog; destructive deletes still confirm.
        """
        super().__init__(parent)
        self.table = table
        self.fields = fields
        # Projection: which fields reach the table and which reach the
        # form.  Every zip between tree values and field names goes
        # through these two lists, so a derived/non-column field (e.g.
        # the staff Last Login) and a hidden field (e.g. password) stay
        # aligned everywhere.
        self.table_fields = [f for f in fields if f.get("table", True)]
        self.form_fields = [f for f in fields if f.get("form", True)]
        self.fixed_values = fixed_values or {}
        self.where_clause = where_clause
        self.order_by = order_by or "id DESC"
        self.on_change = on_change
        self.id_field = id_field
        self.allow_delete = allow_delete
        self.search_field = search_field
        self.defaults = defaults or {}
        self.notify = notify
        self.selected_id = None
        self.selected_row = None

        self._build_ui(title)
        self.refresh()
        self._apply_defaults()

    def _feedback(self, text, kind="warn"):
        """Normal events go to the owner's toast (no popup); the dialog is
        only a fallback when no toast channel was provided."""
        if self.notify:
            try:
                self.notify(text, kind)
                return
            except Exception:
                pass
        if kind == "error":
            messagebox.showerror("Error", text, parent=self)
        else:
            messagebox.showwarning("Notice", text, parent=self)

    def _notify_change(self, action, data):
        """Inform the owner of this CRUD frame that data changed.
        on_change(action, data_dict) - data contains the record values and,
        for updates/deletes, '_old_student_id' from the selected row."""
        if self.on_change:
            try:
                self.on_change(action, data or {})
            except TypeError:
                self.on_change()

    # ------------------------------------------------------------------ UI
    def _build_ui(self, title):
        ctk.CTkLabel(self, text=title, font=FONT_HEADER).pack(anchor="w", padx=12, pady=(10, 4))
        # Subclass hook: extra header content between the title and the
        # form (used by InventoryCRUDFrame for its stats strip, low-stock
        # warning and stock actions).
        if hasattr(self, "_build_subheader"):
            self._build_subheader()

        # ---- form area ----
        form = CardFrame(self, text=self.form_title)
        form.pack(fill="x", padx=12, pady=6)
        self.entries = {}
        cols_per_row = 3
        if any(f.get("group") for f in self.form_fields):
            self._build_sectioned_form(form, cols_per_row)
        else:
            for idx, f in enumerate(self.form_fields):
                r, c = divmod(idx, cols_per_row)
                self._form_cell(form, f, r, c)

        # ---- buttons ----
        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(btns, text="Add New", command=self.add_record).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Update Selected", command=self.update_record).pack(side="left", padx=4)
        if self.allow_delete:
            ctk.CTkButton(btns, text="Delete Selected", command=self.delete_record).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Clear Form", command=self.clear_form).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Refresh", command=self.refresh).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Export to CSV", command=self.export_csv).pack(side="right", padx=4)
        # Keep a handle on the bar: subclasses add their own actions to it
        # (StaffAccountsFrame's "View Details", P2-9).
        self._btn_bar = btns

        # Keep a handle on the search row too: row-scoped actions are
        # right-packed here (the staff Enable/Disable + Revoke Sessions)
        # so the button bar's width budget never grows.
        self._search_row = None
        if self.search_field:
            search_frame = ctk.CTkFrame(self)
            search_frame.pack(fill="x", padx=12, pady=(0, 4))
            ctk.CTkLabel(search_frame, text="Search:").pack(side="left")
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write", lambda *a: self.refresh())
            ctk.CTkEntry(search_frame, textvariable=self.search_var, width=186).pack(side="left", padx=6)
            self._search_row = search_frame

        # ---- table ----
        table_frame = ctk.CTkFrame(self)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)
        columns = ["id"] + [f["name"] for f in self.table_fields]
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        for col in columns:
            label = "ID" if col == "id" else next((f["label"] for f in self.table_fields if f["name"] == col), col)
            self.tree.heading(col, text=label)
            width = next((f.get("width") for f in self.table_fields
                          if f["name"] == col and f.get("width")), 110)
            self.tree.column(col, width=width, anchor="w")
        vsb = ctk.CTkScrollbar(table_frame, height=51, orientation="vertical", command=self.tree.yview)
        hsb = ctk.CTkScrollbar(table_frame, width=51, orientation="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

    def _form_cell(self, form, f, r, c):
        """One label+widget cell of the form grid.  Rendering is exactly
        what it always was - only the placement (row/col) is decided by
        the caller, so sectioned and plain forms share one cell."""
        cell = ctk.CTkFrame(form)
        cell.grid(row=r, column=c, padx=8, pady=6, sticky="w")
        ctk.CTkLabel(cell, text=f["label"] + ":").pack(anchor="w")
        if f["type"] == "combobox":
            var = tk.StringVar()
            widget = ctk.CTkComboBox(cell, variable=var, values=f.get("options", []),
                                   state="readonly", width=150)
            widget.pack()
        elif f["type"] == "text":
            widget = ctk.CTkTextbox(cell, width=162, height=45)
            widget.pack()
            var = None
        else:
            var = tk.StringVar()
            widget = ctk.CTkEntry(cell, textvariable=var, width=168)
            widget.pack()
        self.entries[f["name"]] = (widget, var, f["type"])

    def _build_sectioned_form(self, form, cols_per_row):
        """Sectioned form layout (spec: the Staff & Admin form is split
        into ACCOUNT INFORMATION and PASSWORD MANAGEMENT).

        Each group's fields are placed in `pos` order, so the form order
        (Username, Full Name, Email, Contact, Role, Status) can differ
        from the table order without either one drifting.  After a group
        is placed, the `_form_section_extra` hook (if the frame defines
        one) may add widgets to the group's rows - the staff Change
        Password action rides along that way at zero added height."""
        order, groups = [], {}
        for f in self.form_fields:
            g = f.get("group") or ""
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(f)
        grid_row = 0
        for gname in order:
            items = [f for _, f in sorted(
                enumerate(groups[gname]),
                key=lambda t: (t[1].get("pos", t[0]), t[0]))]
            if gname:
                self._form_section(form, gname, grid_row, cols_per_row)
                grid_row += 1
            sec_first = grid_row
            for i, f in enumerate(items):
                r, c = divmod(i, cols_per_row)
                self._form_cell(form, f, grid_row + r, c)
            nrows = (len(items) + cols_per_row - 1) // cols_per_row
            grid_row += nrows
            extra = getattr(self, "_form_section_extra", None)
            if extra is not None:
                extra(form, gname, sec_first, nrows)

    def _form_section(self, form, name, row, cols):
        """A section header row spanning the whole form."""
        ctk.CTkLabel(form, text=name, font=("Segoe UI", 9, "bold"),
                     text_color=TEXT).grid(row=row, column=0, columnspan=cols,
                                           sticky="w", padx=8, pady=(8, 0))

    # ------------------------------------------------------------- helpers
    def _get_form_values(self):
        values = {}
        for name, (widget, var, ftype) in self.entries.items():
            if ftype == "text":
                values[name] = widget.get("1.0", "end").strip()
            else:
                values[name] = var.get().strip()
        return values

    def _set_form_values(self, row):
        for name, (widget, var, ftype) in self.entries.items():
            val = row[name] if name in row.keys() else ""
            if ftype == "text":
                widget.delete("1.0", "end")
                widget.insert("1.0", val or "")
            else:
                var.set(val or "")

    def clear_form(self):
        for widget, var, ftype in self.entries.values():
            if ftype == "text":
                widget.delete("1.0", "end")
            else:
                var.set("")
        self.selected_id = None
        self.selected_row = None
        self.tree.selection_remove(self.tree.selection())
        self._apply_defaults()

    def _apply_defaults(self):
        for name, value in self.defaults.items():
            if name not in self.entries:
                continue
            widget, var, ftype = self.entries[name]
            resolved = value() if callable(value) else value
            if ftype == "text":
                widget.delete("1.0", "end")
                widget.insert("1.0", resolved)
            else:
                var.set(resolved)

    def on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.tree.item(sel[0])
        vals = item["values"]
        columns = ["id"] + [f["name"] for f in self.table_fields]
        row = dict(zip(columns, vals))
        self.selected_id = row["id"]
        self.selected_row = row
        self._set_form_values(row)

    # ------------------------------------------------- derived table cells
    def _prefetch_derived(self, rows):
        """Called once per refresh with the fetched rows, before the table
        is rebuilt.  Subclasses load the data for table fields that are
        not columns of the SQL table (e.g. the staff Last Login) here so
        _derived_value never queries per row."""
        pass

    def _derived_value(self, name, row):
        """Value for a table field that is not a column of the SQL table.
        None keeps the default empty cell."""
        return None

    # -------------------------------------------------------------- CRUD
    def refresh(self):
        conn = get_connection()
        cur = conn.cursor()
        query = f"SELECT * FROM {self.table}"
        clauses = []
        params = []
        if self.where_clause:
            clauses.append(self.where_clause)
        if self.search_field and getattr(self, "search_var", None) and self.search_var.get().strip():
            clauses.append(f"{self.search_field} LIKE ?")
            params.append(f"%{self.search_var.get().strip()}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += f" ORDER BY {self.order_by}"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        for item in self.tree.get_children():
            self.tree.delete(item)
        columns = ["id"] + [f["name"] for f in self.table_fields]
        self._prefetch_derived(rows)
        for row in rows:
            values = []
            for c in columns:
                if c in row.keys():
                    v = row[c]
                else:
                    dv = self._derived_value(c, row)
                    v = "" if dv is None else dv
                values.append(v)
            self.tree.insert("", "end", values=values)

    def add_record(self):
        values = self._get_form_values()
        if not values:
            return
        missing = [f["label"] for f in self.fields if f.get("required") and not values.get(f["name"])]
        if missing:
            self._feedback("Please fill in: " + ", ".join(missing), "warn")
            return
        all_values = {**values, **self.fixed_values}
        cols = ", ".join(all_values.keys())
        placeholders = ", ".join(["?"] * len(all_values))
        conn = get_connection()
        try:
            conn.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({placeholders})", list(all_values.values()))
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not add record: {e}", "error")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("add", all_values)

    def update_record(self):
        if not self.selected_id:
            self._feedback("Select a record from the table first.", "warn")
            return
        values = self._get_form_values()
        set_clause = ", ".join([f"{k} = ?" for k in values.keys()])
        old_student_id = (self.selected_row or {}).get("student_id", "")
        conn = get_connection()
        try:
            conn.execute(f"UPDATE {self.table} SET {set_clause} WHERE {self.id_field} = ?",
                         list(values.values()) + [self.selected_id])
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not update record: {e}", "error")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("update", {**values, "_old_student_id": old_student_id})

    def delete_record(self):
        if not self.selected_id:
            self._feedback("Select a record from the table first.", "warn")
            return
        # destructive action -> confirmation dialog stays (spec allows it)
        if not messagebox.askyesno("Confirm delete", "Delete the selected record? This cannot be undone."):
            return
        deleted = dict(self.selected_row or {})
        conn = get_connection()
        conn.execute(f"DELETE FROM {self.table} WHERE {self.id_field} = ?", (self.selected_id,))
        conn.commit()
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("delete", deleted)

    def export_csv(self):
        # table_fields only: a hidden field (password) is never exported
        # and a derived field (Last Login) still reaches the file.
        columns = ["id"] + [f["name"] for f in self.table_fields]
        headers = ["ID"] + [f["label"] for f in self.table_fields]
        rows = [self.tree.item(i)["values"] for i in self.tree.get_children()]
        export_rows_to_csv(headers, rows, default_name=f"{self.table}_export.csv",
                           parent=self, notify=self.notify)


class UserCRUDFrame(CRUDFrame):
    """
    Specialized CRUD frame for the 'users' table (students or admins).
    Hashes the password field on add. On update, leaving the password
    field blank keeps the existing password unchanged.
    """

    def add_record(self):
        values = self._get_form_values()
        role = values.get("role") or self.fixed_values.get("role") or ""
        # P2-10: a blank password is NEVER written as "" - that used to
        # insert an account nobody could ever log into.  A fresh STUDENT
        # account falls back to the factory default (which the server
        # still refuses as a chosen password and forces through a change
        # before any session starts).  Every other role has no forced
        # change available, so it must be given a real one here.
        if not values.get("password"):
            if role == "student":
                values["password"] = DEFAULT_CLIENT_PASSWORD
            else:
                self._feedback("Enter a password for the new account.",
                               "warn")
                return
        values["password"] = hash_password(values["password"])
        # FIRST LOGIN PASSWORD: every freshly created client (student)
        # account starts flagged - its first login is forced through a
        # password change on the Client kiosk.  Admin/staff/maintenance
        # are never flagged here (scope: fresh client accounts only).
        if role == "student":
            values["must_change_password"] = 1
        # Temporarily inject hashed password back through the normal flow
        self._pending_values = values
        if self._add_record_with(values):
            self._notify_change("add", {k: v for k, v in values.items()
                                        if k != "password"})

    def _add_record_with(self, values):
        missing = [f["label"] for f in self.fields if f.get("required") and not values.get(f["name"])]
        if missing:
            self._feedback("Please fill in: " + ", ".join(missing), "warn")
            return False
        all_values = {**values, **self.fixed_values}
        cols = ", ".join(all_values.keys())
        placeholders = ", ".join(["?"] * len(all_values))
        conn = get_connection()
        try:
            conn.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({placeholders})", list(all_values.values()))
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not add account: {e}", "error")
            conn.close()
            return False
        conn.close()
        self.clear_form()
        self.refresh()
        if self.on_change:
            pass   # notified by add_record with the plain values
        return True

    def update_record(self):
        if not self.selected_id:
            self._feedback("Select an account from the table first.", "warn")
            return
        values = self._get_form_values()
        if values.get("password"):
            values["password"] = hash_password(values["password"])
        else:
            values.pop("password", None)  # keep existing password
        if not values:
            return
        old_student_id = (self.selected_row or {}).get("student_id", "")
        set_clause = ", ".join([f"{k} = ?" for k in values.keys()])
        conn = get_connection()
        try:
            conn.execute(f"UPDATE {self.table} SET {set_clause} WHERE {self.id_field} = ?",
                         list(values.values()) + [self.selected_id])
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not update account: {e}", "error")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("update", {**values, "_old_student_id": old_student_id})

    def _set_form_values(self, row):
        for name, (widget, var, ftype) in self.entries.items():
            if name == "password":
                continue  # never populate password field from stored hash
            val = row[name] if name in row.keys() else ""
            if ftype == "text":
                widget.delete("1.0", "end")
                widget.insert("1.0", val or "")
            else:
                var.set(val or "")

    def refresh(self):
        """Same as CRUDFrame.refresh, but masks the password hash in the table."""
        conn = get_connection()
        cur = conn.cursor()
        query = f"SELECT * FROM {self.table}"
        clauses = []
        params = []
        if self.where_clause:
            clauses.append(self.where_clause)
        if self.search_field and getattr(self, "search_var", None) and self.search_var.get().strip():
            clauses.append(f"{self.search_field} LIKE ?")
            params.append(f"%{self.search_var.get().strip()}%")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += f" ORDER BY {self.order_by}"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        for item in self.tree.get_children():
            self.tree.delete(item)
        columns = ["id"] + [f["name"] for f in self.table_fields]
        self._prefetch_derived(rows)
        for row in rows:
            values = []
            for c in columns:
                if c in row.keys():
                    v = row[c]
                else:
                    dv = self._derived_value(c, row)
                    v = "" if dv is None else dv
                if c == "password":
                    v = "********"
                values.append(v)
            self.tree.insert("", "end", values=values)


class InventoryCRUDFrame(CRUDFrame):
    """Specialized CRUD frame for the 'inventory' table (spec item 5).

    Integer stock quantities with an available/assigned split, a status
    column, stock actions (increase / decrease / assign / return / mark
    damaged / mark lost), a statistics strip and a low-stock warning.
    Normal feedback goes to the owner's toast; only Delete (inherited)
    keeps its confirmation dialog."""

    STATUSES = ["AVAILABLE", "IN USE", "BORROWED", "DAMAGED",
                "LOST", "MAINTENANCE", "OUT OF STOCK"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 12 columns at the base 110px would scroll forever: narrow the
        # numerics and widen the identifying ones (compact table spec).
        try:
            for c in self.tree["columns"]:
                self.tree.column(c, width=84, anchor="w")
            self.tree.column("item_name", width=150, anchor="w")
            self.tree.column("category", width=105, anchor="w")
            self.tree.column("status", width=105, anchor="w")
            self.tree.column("location", width=95, anchor="w")
            self.tree.column("notes", width=130, anchor="w")
        except Exception:
            pass

    # ------------------------------------------------------- header strip
    def _build_subheader(self):
        # statistics: Total / Available / In Use / Borrowed / Damaged / Low
        strip = ctk.CTkFrame(self)
        strip.pack(fill="x", padx=12, pady=(0, 4))
        self.stat_labels = {}
        for key, caption in (("total", "Total Items"), ("available", "Available"),
                             ("inuse", "In Use"), ("borrowed", "Borrowed"),
                             ("damaged", "Damaged"), ("low", "Low Stock")):
            tile = ctk.CTkFrame(strip)
            tile.pack(side="left", padx=(0, 16))
            val = ctk.CTkLabel(tile, text="0", font=("Segoe UI", 14, "bold"),
                            text_color=ACCENT)
            val.pack(anchor="w")
            ctk.CTkLabel(tile, text=caption, font=("Segoe UI", 8),
                      text_color=MUTED).pack(anchor="w")
            self.stat_labels[key] = val

        # low-stock warning: "⚠ Low Stock: Keyboard — 2 remaining"
        self.low_stock_lbl = ctk.CTkLabel(self, text="",
                                       font=("Segoe UI", 9, "bold"),
                                       text_color="#b8860b")
        self.low_stock_lbl.pack(fill="x", padx=12, pady=(0, 2))

        # stock actions on the selected row (amount from the small entry)
        actions = ctk.CTkFrame(self)
        actions.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkLabel(actions, text="Stock actions:").pack(side="left")
        self.act_qty = tk.StringVar(value="1")
        ctk.CTkEntry(actions, textvariable=self.act_qty, width=36).pack(
            side="left", padx=(4, 6))
        for text, cmd in (("Increase Stock", self.action_increase),
                          ("Decrease Stock", self.action_decrease),
                          ("Assign Item", self.action_assign),
                          ("Return Item", self.action_return),
                          ("Mark Damaged", self.action_damaged),
                          ("Mark Lost", self.action_lost)):
            ctk.CTkButton(actions, text=text, command=cmd).pack(side="left", padx=2)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _to_int(value, default=0):
        try:
            return int(str(value).strip() or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _row(item_id):
        conn = get_connection()
        r = conn.execute("SELECT * FROM inventory WHERE id=?",
                         (item_id,)).fetchone()
        conn.close()
        return r

    @staticmethod
    def _today():
        return now_date() if callable(now_date) else now_date

    def _amount(self):
        """Positive integer from the actions-row quantity entry."""
        try:
            n = int(str(self.act_qty.get()).strip() or "0")
        except ValueError:
            n = 0
        if n <= 0:
            self._feedback("Enter a quantity of 1 or more.", "warn")
            return None
        return n

    def _need_selection(self):
        if not self.selected_id:
            self._feedback("Select an item from the table first.", "warn")
            return None
        return self.selected_id

    def _after_action(self, item_id, msg):
        self.refresh()
        self._notify_change("update", {"id": item_id,
                                       "item_name": (self._row(item_id) or
                                                     {}).get("item_name", "")})
        self._feedback(msg, "success")

    # ------------------------------------------------- stock math (SQL core)
    def _stock_change(self, item_id, amount, increase):
        """Increase/decrease total + available stock (never below zero).
        OUT OF STOCK flips back as stock returns; DAMAGED/LOST/MAINTENANCE
        keep their status until changed explicitly."""
        conn = get_connection()
        row = conn.execute("SELECT * FROM inventory WHERE id=?",
                           (item_id,)).fetchone()
        if not row:
            conn.close()
            return False
        qty = self._to_int(row["quantity"])
        avail = self._to_int(row["available_qty"])
        assigned = self._to_int(row["assigned_qty"])
        if increase:
            qty += amount
            avail += amount
        else:
            amount = min(amount, avail)     # can't remove what isn't there
            qty = max(qty - amount, 0)
            avail = max(avail - amount, 0)
        status = row["status"] or "AVAILABLE"
        if status not in ("DAMAGED", "LOST", "MAINTENANCE"):
            if avail <= 0:
                status = "OUT OF STOCK"
            elif status == "OUT OF STOCK":
                status = "BORROWED" if assigned > 0 else "AVAILABLE"
        conn.execute(
            "UPDATE inventory SET quantity=?, available_qty=?, status=?, "
            "last_updated=date('now') WHERE id=?",
            (qty, avail, status, item_id))
        conn.commit()
        conn.close()
        return True

    def _assign_qty(self, item_id, amount):
        """Move amount from available to assigned; status -> BORROWED."""
        conn = get_connection()
        row = conn.execute("SELECT * FROM inventory WHERE id=?",
                           (item_id,)).fetchone()
        if not row:
            conn.close()
            return False
        avail = self._to_int(row["available_qty"])
        if amount > avail:
            conn.close()
            return False
        conn.execute(
            "UPDATE inventory SET available_qty=?, assigned_qty=?, "
            "status=CASE WHEN status IN ('DAMAGED','LOST','MAINTENANCE') "
            "  THEN status ELSE 'BORROWED' END, "
            "last_updated=date('now') WHERE id=?",
            (avail - amount, self._to_int(row["assigned_qty"]) + amount,
             item_id))
        conn.commit()
        conn.close()
        return True

    def _return_qty(self, item_id, amount):
        """Move amount back from assigned to available; returns the amount
        actually returned (0 when nothing is assigned)."""
        conn = get_connection()
        row = conn.execute("SELECT * FROM inventory WHERE id=?",
                           (item_id,)).fetchone()
        if not row:
            conn.close()
            return 0
        assigned = self._to_int(row["assigned_qty"])
        got = min(amount, assigned)
        if got <= 0:
            conn.close()
            return 0
        assigned -= got
        avail = self._to_int(row["available_qty"]) + got
        qty = max(self._to_int(row["quantity"]), avail + assigned)
        status = row["status"] or "AVAILABLE"
        if status not in ("DAMAGED", "LOST", "MAINTENANCE") and assigned == 0:
            status = "AVAILABLE" if avail > 0 else "OUT OF STOCK"
        conn.execute(
            "UPDATE inventory SET quantity=?, available_qty=?, assigned_qty=?, "
            "status=?, last_updated=date('now') WHERE id=?",
            (qty, avail, assigned, status, item_id))
        conn.commit()
        conn.close()
        return got

    def _set_status(self, item_id, status):
        conn = get_connection()
        conn.execute("UPDATE inventory SET status=?, last_updated=date('now') "
                     "WHERE id=?", (status, item_id))
        conn.commit()
        conn.close()
        return True

    def _stats(self):
        """Aggregate counts + the low-stock list (threshold from
        system_settings.low_stock_threshold, default 3)."""
        try:
            thr = int(get_setting("low_stock_threshold", "3"))
        except (TypeError, ValueError):
            thr = 3
        conn = get_connection()
        r = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='AVAILABLE' THEN 1 ELSE 0 END) AS available,
                   SUM(CASE WHEN status='IN USE' THEN 1 ELSE 0 END) AS inuse,
                   SUM(CASE WHEN status='BORROWED' THEN 1 ELSE 0 END) AS borrowed,
                   SUM(CASE WHEN status='DAMAGED' THEN 1 ELSE 0 END) AS damaged,
                   SUM(CASE WHEN available_qty <= ? THEN 1 ELSE 0 END) AS low
            FROM inventory""", (thr,)).fetchone()
        items = conn.execute(
            "SELECT item_name, available_qty FROM inventory "
            "WHERE available_qty <= ? "
            "ORDER BY available_qty ASC, item_name ASC LIMIT 3",
            (thr,)).fetchall()
        conn.close()
        stats = {k: int(r[k] or 0)
                 for k in ("total", "available", "inuse",
                           "borrowed", "damaged", "low")}
        stats["low_items"] = [(i["item_name"], int(i["available_qty"]))
                              for i in items]
        stats["threshold"] = thr
        return stats

    def _update_stats(self):
        st = self._stats()
        for key in ("total", "available", "inuse", "borrowed", "damaged", "low"):
            self.stat_labels[key].configure(text=str(st[key]))
        self.stat_labels["low"].configure(
            text_color=ORANGE if st["low"] else ACCENT)
        if st["low_items"]:
            shown = "  \u00b7  ".join(
                f"{name} \u2014 {qty} remaining"
                for name, qty in st["low_items"])
            more = st["low"] - len(st["low_items"])
            if more > 0:
                shown += f"  (+{more} more)"
            self.low_stock_lbl.configure(
                text=f"\u26a0 Low Stock: {shown}")
        else:
            self.low_stock_lbl.configure(text="")

    def refresh(self):
        super().refresh()
        self._update_stats()

    # ----------------------------------------------------------- add/edit
    def _coerced_values(self):
        """Form values with quantity/available/assigned coerced to
        non-negative integers and status derived when blank.
        Returns (values, error_message)."""
        values = self._get_form_values()
        missing = [f["label"] for f in self.fields
                   if f.get("required") and not values.get(f["name"])]
        if missing:
            return None, "Please fill in: " + ", ".join(missing)

        def _num(name, default):
            raw = (values.get(name) or "").strip()
            if raw == "":
                return default, True
            try:
                n = int(raw)
            except ValueError:
                return None, False
            return (n, True) if n >= 0 else (None, False)

        assigned, ok = _num("assigned_qty", 0)
        if not ok:
            return None, "Assigned Qty must be a whole number of 0 or more."
        available, ok = _num("available_qty", -1)
        if not ok:
            return None, "Available Qty must be a whole number of 0 or more."
        quantity, ok = _num("quantity", -1)
        if not ok:
            return None, "Quantity must be a whole number of 0 or more."
        if quantity < 0:                       # blank: total = avail + assigned
            quantity = max(available, 0) + assigned
        if available < 0:                      # blank: available = total - assigned
            available = max(quantity - assigned, 0)
        if available + assigned > quantity:
            return None, "Available + Assigned cannot exceed Quantity."
        values["quantity"] = quantity
        values["available_qty"] = available
        values["assigned_qty"] = assigned
        status = (values.get("status") or "").strip().upper()
        if status not in self.STATUSES:
            status = ("OUT OF STOCK" if available <= 0 else
                      "BORROWED" if assigned > 0 else "AVAILABLE")
        # reconcile obvious contradictions with the stock split
        if status == "AVAILABLE" and assigned > 0:
            status = "BORROWED"
        if status in ("AVAILABLE", "IN USE", "BORROWED") and available <= 0:
            status = "OUT OF STOCK"
        values["status"] = status
        values["date_added"] = (values.get("date_added") or "").strip() \
            or self._today()
        values["last_updated"] = self._today()
        return values, None

    def add_record(self):
        values, err = self._coerced_values()
        if err:
            self._feedback(err, "warn")
            return
        all_values = {**values, **self.fixed_values}
        cols = ", ".join(all_values.keys())
        placeholders = ", ".join(["?"] * len(all_values))
        conn = get_connection()
        try:
            conn.execute(f"INSERT INTO {self.table} ({cols}) "
                         f"VALUES ({placeholders})",
                         list(all_values.values()))
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not add item: {e}", "error")
            conn.close()
            return
        conn.close()
        name = values.get("item_name", "item")
        self.clear_form()
        self.refresh()
        self._notify_change("add", all_values)
        self._feedback(f"Added {name}.", "success")

    def update_record(self):
        if not self.selected_id:
            self._feedback("Select a record from the table first.", "warn")
            return
        values, err = self._coerced_values()
        if err:
            self._feedback(err, "warn")
            return
        set_clause = ", ".join(f"{k} = ?" for k in values)
        conn = get_connection()
        try:
            conn.execute(f"UPDATE {self.table} SET {set_clause} "
                         f"WHERE {self.id_field} = ?",
                         list(values.values()) + [self.selected_id])
            conn.commit()
        except Exception as e:
            self._feedback(f"Could not update record: {e}", "error")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("update", values)
        self._feedback("Item updated.", "success")

    # ------------------------------------------------------ stock actions
    def action_increase(self):
        item_id = self._need_selection()
        n = self._amount()
        if item_id is None or n is None:
            return
        self._stock_change(item_id, n, True)
        self._after_action(item_id, f"Stock increased by {n}.")

    def action_decrease(self):
        item_id = self._need_selection()
        n = self._amount()
        if item_id is None or n is None:
            return
        self._stock_change(item_id, n, False)
        self._after_action(item_id, f"Stock decreased by {n}.")

    def action_assign(self):
        item_id = self._need_selection()
        n = self._amount()
        if item_id is None or n is None:
            return
        row = self._row(item_id)
        avail = self._to_int((row or {}).get("available_qty"))
        if n > avail:
            self._feedback(f"Only {avail} available to assign.", "warn")
            return
        self._assign_qty(item_id, n)
        self._after_action(item_id, f"Assigned {n}.")

    def action_return(self):
        item_id = self._need_selection()
        n = self._amount()
        if item_id is None or n is None:
            return
        got = self._return_qty(item_id, n)
        if got <= 0:
            self._feedback("Nothing is assigned on this item.", "warn")
            return
        self._after_action(item_id, f"Returned {got} to stock.")

    def action_damaged(self):
        item_id = self._need_selection()
        if item_id is None:
            return
        self._set_status(item_id, "DAMAGED")
        self._after_action(item_id, "Marked DAMAGED.")

    def action_lost(self):
        item_id = self._need_selection()
        if item_id is None:
            return
        self._set_status(item_id, "LOST")
        self._after_action(item_id, "Marked LOST.")


def normalize_web_domain(raw):
    """Bare lowercase domain from a possibly pasted URL: scheme, path,
    query, port and wildcard prefix are dropped (``*.x.com`` -> ``x.com``)."""
    d = str(raw or "").strip().lower()
    if "//" in d:
        d = d.split("//", 1)[1]
    d = d.split("/", 1)[0].split("?")[0].split("#")[0].split(":")[0]
    if d.startswith("*."):
        d = d[2:]
    return d.strip(".")


def audit_action_text(action):
    """Readable Action text for the Account Details 'Activity Logs' tab
    (kept local so crud_frame never imports admin_dashboard)."""
    a = str(action or "")
    if a.startswith("command:"):
        return "Command: " + a[8:]
    if a.startswith("BULK_"):
        return "Bulk " + a[5:].replace("_", " ").title()
    return a.replace("_", " ").title() if a else "—"


class WebsiteRulesFrame(ctk.CTkFrame):
    """Unified Website Rules table (Website Access control panel): one
    list for every rule - Domain | Category | Action | Status | Notes -
    with instant Search + Category/Action/Status filters, an Add/Edit
    dialog and a confirmed delete.  Same `websites` table and semantics
    as the old two-list frame: pasted URLs are normalized to bare
    domains, `enabled` is stored as 1/0 while the UI shows
    Enabled/Disabled, and every change is reported through
    on_change(action, data) so the owner can version/push/audit it."""

    COLUMNS = (("domain", "Domain", 230), ("category", "Category", 110),
               ("action", "Action", 90), ("status", "Status", 90),
               ("notes", "Notes", 250))

    def __init__(self, parent, on_change=None, notify=None):
        super().__init__(parent)
        self.on_change = on_change
        self.notify = notify
        self.selected_id = None
        self._dialog = None
        self._dlg_vars = {}
        self._dlg_id = None
        self._busy = False
        # instant filtering (spec item 4): every control re-runs refresh
        self.search_var = tk.StringVar()
        self.cat_var = tk.StringVar(value="All")
        self.action_var = tk.StringVar(value="All")
        self.status_var = tk.StringVar(value="All")
        self.search_var.trace_add("write", lambda *_: self.refresh())
        for var in (self.cat_var, self.action_var, self.status_var):
            var.trace_add("write", lambda *_: self.refresh())
        self._build_ui()
        self.refresh()                  # first fill (trace fires too)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        bar = ctk.CTkFrame(self)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkLabel(bar, text="Search:").pack(side="left")
        ctk.CTkEntry(bar, textvariable=self.search_var,
                  width=126).pack(side="left", padx=(4, 10))
        ctk.CTkLabel(bar, text="Category:").pack(side="left")
        self.cat_cb = ctk.CTkComboBox(bar, variable=self.cat_var, width=84,
                                   state="readonly", values=["All"])
        self.cat_cb.pack(side="left", padx=(4, 10))
        ctk.CTkLabel(bar, text="Action:").pack(side="left")
        ctk.CTkComboBox(bar, variable=self.action_var, width=84,
                     state="readonly",
                     values=["All", "Allow", "Block"]).pack(
            side="left", padx=(4, 10))
        ctk.CTkLabel(bar, text="Status:").pack(side="left")
        ctk.CTkComboBox(bar, variable=self.status_var, width=100,
                     state="readonly",
                     values=["All", "Enabled", "Disabled"]).pack(
            side="left", padx=(4, 10))
        ctk.CTkButton(bar, text="+ Add Website", command=self._open_dialog,
                  fg_color=ACCENT, text_color="white", hover_color="#1e4fbf",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right")

        box = ctk.CTkFrame(self)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tree = ttk.Treeview(
            box, columns=[c for c, _, _ in self.COLUMNS],
            show="headings", height=7)
        for cid, text, w in self.COLUMNS:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w",
                             stretch=cid in ("domain", "notes"))
        vsb = ctk.CTkScrollbar(box, height=51, orientation="vertical", command=self.tree.yview)
        hsb = ctk.CTkScrollbar(box, width=51, orientation="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(btns, text="Edit Selected",
                   command=self._edit_selected).pack(side="left",
                                                     padx=(0, 4))
        ctk.CTkButton(btns, text="Delete Selected",
                   command=self.delete_selected).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Refresh",
                   command=self.refresh).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Export CSV",
                   command=self.export_csv).pack(side="right")

    # ------------------------------------------------------------ feedback
    def _feedback(self, text, kind="warn"):
        """Normal events go to the owner's toast (no popup); the dialog is
        only a fallback when no toast channel was provided."""
        if self.notify:
            try:
                self.notify(text, kind)
                return
            except Exception:
                pass
        if kind == "error":
            messagebox.showerror("Error", text, parent=self)
        else:
            messagebox.showwarning("Notice", text, parent=self)

    def _notify_change(self, action, data):
        """Report a data change to the owner (versioning/push/audit)."""
        if self.on_change:
            try:
                self.on_change(action, data or {})
            except TypeError:
                self.on_change()

    # ------------------------------------------------------------- reading
    def refresh(self):
        """Reload the table through the current instant filters."""
        if self._busy:
            return
        self._busy = True
        try:
            conn = get_connection()
            cats = ["All"] + [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM websites "
                "WHERE category IS NOT NULL AND category != '' "
                "ORDER BY category").fetchall()]
            if list(self.cat_cb.cget("values")) != cats:
                self.cat_cb.configure(values=cats)
                if self.cat_var.get() not in cats:
                    self.cat_var.set("All")   # trace re-enters, _busy blocks
            clauses, params = [], []
            term = self.search_var.get().strip()
            if term:
                clauses.append("(domain LIKE ? OR "
                               "COALESCE(category,'') LIKE ? OR "
                               "COALESCE(notes,'') LIKE ?)")
                params.extend([f"%{term}%"] * 3)
            cat = self.cat_var.get()
            if cat and cat != "All":
                clauses.append("category = ?")
                params.append(cat)
            action = self.action_var.get()
            if action in ("Allow", "Block"):
                clauses.append("list_type = ?")
                params.append("allowed" if action == "Allow" else "blocked")
            status = self.status_var.get()
            if status in ("Enabled", "Disabled"):
                clauses.append("enabled = ?")
                params.append(1 if status == "Enabled" else 0)
            query = ("SELECT id, domain, category, list_type, enabled, "
                     "notes FROM websites")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY domain ASC"
            rows = conn.execute(query, params).fetchall()
            conn.close()
        except Exception:
            rows = []
        finally:
            self._busy = False
        for item in self.tree.get_children():
            self.tree.delete(item)
        for r in rows:
            # the tree shows Allow/Block and Enabled/Disabled labels
            self.tree.insert("", "end", iid=str(r["id"]), values=(
                r["domain"], r["category"] or "",
                "Allow" if r["list_type"] == "allowed" else "Block",
                "Enabled" if r["enabled"] else "Disabled",
                r["notes"] or ""))

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        try:
            self.selected_id = int(sel[0]) if sel else None
        except (ValueError, TypeError):
            self.selected_id = None

    def _selection_id(self):
        return self.selected_id if self.selected_id is not None \
            else (int(self.tree.selection()[0])
                  if self.tree.selection() else None)

    def export_csv(self):
        headers = [text for _, text, _ in self.COLUMNS]
        rows = [list(self.tree.item(i, "values"))
                for i in self.tree.get_children()]
        export_rows_to_csv(headers, rows,
                           default_name="website_rules_export.csv",
                           parent=self, notify=self.notify or self._feedback)

    # ------------------------------------------------------- add / edit
    def _open_dialog(self, rule=None):
        """Compact form for Domain / Category / Action / Status / Notes."""
        self._close_dialog()
        win = ctk.CTkToplevel(self)
        win.title("Edit Website" if rule else "Add Website")
        set_app_icon(win)
        win.configure(fg_color="#f4f6fb")
        try:
            win.transient(self.winfo_toplevel())
        except Exception:
            pass
        self._dialog = win
        self._dlg_id = rule.get("id") if rule else None
        self._dlg_vars = {
            "domain": tk.StringVar(value=(rule or {}).get("domain", "")),
            "category": tk.StringVar(value=(rule or {}).get("category", "")),
            "action": tk.StringVar(value="Allow" if (rule or {}).get(
                "list_type") == "allowed" else "Block"),
            "enabled": tk.StringVar(value="Enabled" if str(
                (rule or {}).get("enabled", 1)) in ("1", "Enabled", "True",
                                                    "true") else "Disabled"),
            "notes": tk.StringVar(value=(rule or {}).get("notes", "")),
        }
        body = PaddedFrame(win, padding=12)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Edit website rule" if rule
                  else "Add website rule",
                  font=FONT_HEADER).grid(row=0, column=0, columnspan=2,
                                         sticky="w", pady=(0, 10))
        try:
            conn = get_connection()
            cat_values = [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM websites "
                "WHERE category IS NOT NULL AND category != '' "
                "ORDER BY category").fetchall()]
            conn.close()
        except Exception:
            cat_values = []
        fields = (
            ("Domain:", "domain", None),
            ("Category:", "category", cat_values),
            ("Action:", "action", ["Block", "Allow"]),
            ("Status:", "enabled", ["Enabled", "Disabled"]),
            ("Notes:", "notes", None),
        )
        domain_entry = None
        for row, (label, key, options) in enumerate(fields, start=1):
            ctk.CTkLabel(body, text=label).grid(row=row, column=0, sticky="w",
                                             padx=(0, 8), pady=4)
            if options is None:
                entry = ctk.CTkEntry(body, textvariable=self._dlg_vars[key],
                                  width=186)
                entry.grid(row=row, column=1, sticky="ew", pady=4)
                if key == "domain":
                    domain_entry = entry
            else:
                ctk.CTkComboBox(body, variable=self._dlg_vars[key],
                             values=options, width=174,
                             state="normal" if key == "category"
                             else "readonly").grid(
                    row=row, column=1, sticky="ew", pady=4)
        body.grid_columnconfigure(1, weight=1)
        btns = ctk.CTkFrame(body)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e",
                  pady=(12, 0))
        ctk.CTkButton(btns, text="Cancel",
                   command=self._close_dialog).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Save", command=self._save, fg_color=ACCENT,
                  text_color="white", hover_color="#1e4fbf",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="left")
        win.bind("<Return>", lambda _e: self._save())
        win.bind("<Escape>", lambda _e: self._close_dialog())
        try:
            win.update_idletasks()
            win.grab_set()
        except Exception:
            pass
        if domain_entry is not None:
            try:
                domain_entry.focus_set()
            except Exception:
                pass

    def _close_dialog(self):
        if self._dialog is not None:
            try:
                self._dialog.destroy()
            except Exception:
                pass
        self._dialog = None
        self._dlg_vars = {}
        self._dlg_id = None

    def _save(self):
        """Validate + normalize the dialog values, INSERT/UPDATE the rule
        and report the change to the owner."""
        values = {k: v.get().strip() for k, v in self._dlg_vars.items()}
        domain = normalize_web_domain(values.get("domain"))
        if not domain:
            self._feedback("Enter a website domain first.", "warn")
            return False
        list_type = "allowed" if values.get("action") == "Allow" else "blocked"
        enabled = 1 if values.get("enabled") != "Disabled" else 0
        category = values.get("category", "")
        notes = values.get("notes", "")
        conn = get_connection()
        try:
            if self._dlg_id is None:
                conn.execute(
                    "INSERT INTO websites (domain, list_type, enabled, "
                    "category, notes, created_at) VALUES (?,?,?,?,?,?)",
                    (domain, list_type, enabled, category, notes,
                     now_date()))
                action = "add"
            else:
                conn.execute(
                    "UPDATE websites SET domain=?, list_type=?, enabled=?, "
                    "category=?, notes=? WHERE id=?",
                    (domain, list_type, enabled, category, notes,
                     self._dlg_id))
                action = "update"
            conn.commit()
        except Exception as e:
            conn.close()
            if "UNIQUE constraint" in str(e):
                self._feedback(f"'{domain}' is already in the rules list.",
                               "warn")
            else:
                self._feedback(f"Could not save the rule: {e}", "error")
            return False
        conn.close()
        self._close_dialog()
        self.refresh()
        self._notify_change(action, {"domain": domain})
        return True

    def _edit_selected(self):
        rule_id = self._selection_id()
        if rule_id is None:
            self._feedback("Select a website rule first.", "warn")
            return
        conn = get_connection()
        row = conn.execute("SELECT * FROM websites WHERE id=?",
                           (rule_id,)).fetchone()
        conn.close()
        if row:
            self._open_dialog(dict(row))

    def delete_selected(self):
        rule_id = self._selection_id()
        if rule_id is None:
            self._feedback("Select a website rule first.", "warn")
            return False
        conn = get_connection()
        row = conn.execute("SELECT domain FROM websites WHERE id=?",
                           (rule_id,)).fetchone()
        if row is None:                    # stale selection after a refresh
            conn.close()
            self.refresh()
            self._feedback("That website rule no longer exists.", "warn")
            return False
        domain = row["domain"]
        if not messagebox.askyesno(
                "Delete website rule",
                f"Delete '{domain}' from the website rules? "
                "This cannot be undone."):
            conn.close()
            return False
        conn.execute("DELETE FROM websites WHERE id=?", (rule_id,))
        conn.commit()
        conn.close()
        self.selected_id = None
        self.refresh()
        self._notify_change("delete", {"domain": domain})
        return True


class SessionsFrame(ctk.CTkFrame):
    """Read-only session monitor (Sessions page): four summary cards,
    instant Search / Status / PC / Student / Date filters, the
    server-recorded table with a Session Details panel underneath and two
    contextual actions - a confirmed Force Logout and View Activity (the
    Audit Trail pre-filtered on that account).  Sessions are NEVER
    editable from the UI: only the Server writes them."""

    COLUMNS = (("pc", "PC", 125), ("student", "Student ID", 115),
               ("name", "Full Name", 165), ("login", "Login", 150),
               ("logout", "Logout", 150), ("duration", "Duration", 95),
               ("conn", "Connection", 110), ("status", "Status", 140))
    # raw status -> compact display text (DB keeps the raw value)
    STATUS_DISPLAY = {"Active": "ACTIVE", "Completed": "COMPLETED",
                      "Forced Logout": "FORCED LOGOUT",
                      "Disconnected": "TERMINATED"}
    STATUS_RAW = {v: k for k, v in STATUS_DISPLAY.items()}
    CARDS = (("active", "Active Now", SUCCESS),
             ("today", "Today's Sessions", ACCENT),
             ("total", "Total Sessions", ORANGE),
             ("dead", "Terminated", MUTED))
    FIELDS = (("Session ID:", "session_id"), ("PC:", "pc_name"),
              ("Student ID:", "student_id"), ("Full Name:", "full_name"),
              ("Status:", "_status"), ("Login:", "login_time"),
              ("Logout:", "logout_time"), ("Duration:", "_duration"),
              ("Connection:", "connection_type"), ("IP Address:", "ip_address"))

    def __init__(self, parent, on_force_logout=None, on_view_activity=None,
                 notify=None):
        super().__init__(parent)
        self.on_force_logout = on_force_logout
        self.on_view_activity = on_view_activity
        self.notify = notify
        self.selected_id = None
        self._rows = {}
        self._busy = False
        # instant filtering (spec item 4): every control re-runs refresh
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="All")
        self.pc_var = tk.StringVar(value="All")
        self.student_var = tk.StringVar(value="All")
        self.date_var = tk.StringVar()          # YYYY-MM-DD (login date)
        for var in (self.search_var, self.status_var, self.pc_var,
                    self.student_var, self.date_var):
            var.trace_add("write", lambda *_: self.refresh())
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        cards = ctk.CTkFrame(self)
        cards.pack(fill="x", padx=8, pady=(8, 4))
        self._card_vals = {}
        for idx, (key, label, color) in enumerate(self.CARDS):
            tile = ctk.CTkFrame(cards, fg_color="white", height=STAT_TILE_H,
                            border_color=BORDER, border_width=1)
            tile.grid(row=0, column=idx, sticky="nsew", padx=(0, 6))
            tile.grid_propagate(False)
            ctk.CTkFrame(tile, fg_color=color, height=4).pack(fill="x")
            val = ctk.CTkLabel(tile, text="0", font=FONT_STAT_NUM, text_color=BG_DARK,
                           fg_color="white", anchor="w")
            val.pack(fill="x", padx=10, pady=(3, 0))
            ctk.CTkLabel(tile, text=label, font=FONT_SMALL, text_color="#5a6480", fg_color="white",
                     anchor="w").pack(fill="x", padx=10, pady=(0, 4))
            self._card_vals[key] = val
            cards.columnconfigure(idx, weight=1)

        bar = ctk.CTkFrame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(bar, text="Search:").pack(side="left")
        ctk.CTkEntry(bar, textvariable=self.search_var, width=126).pack(
            side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="Status:").pack(side="left")
        self.status_cb = ctk.CTkComboBox(
            bar, variable=self.status_var, width=150, state="readonly",
            values=["All"] + list(self.STATUS_DISPLAY.values()))
        self.status_cb.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="PC:").pack(side="left")
        self.pc_cb = ctk.CTkComboBox(bar, variable=self.pc_var, width=120,
                                  state="readonly", values=["All"])
        self.pc_cb.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="Student:").pack(side="left")
        self.student_cb = ctk.CTkComboBox(bar, variable=self.student_var,
                                       width=118, state="readonly",
                                       values=["All"])
        self.student_cb.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="Date:").pack(side="left")
        ctk.CTkEntry(bar, textvariable=self.date_var, width=72).pack(
            side="left", padx=(4, 8))
        ctk.CTkButton(bar, text="Clear", command=self.clear_filters).pack(
            side="left")

        actions = ctk.CTkFrame(self)
        actions.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkButton(actions, text="Force Logout", command=self.force_logout,
                  fg_color=CRIMSON, text_color="white", hover_color="#9f1239",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="left", padx=(0, 4))
        ctk.CTkButton(actions, text="View Activity", command=self.view_activity,
                  fg_color=ACCENT, text_color="white", hover_color="#1e4fbf",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="left")
        ctk.CTkButton(actions, text="Refresh",
                   command=self.refresh).pack(side="right", padx=(4, 0))
        ctk.CTkButton(actions, text="Export CSV",
                   command=self.export_csv).pack(side="right")

        box = ctk.CTkFrame(self)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tree = ttk.Treeview(
            box, columns=[c for c, _, _ in self.COLUMNS],
            show="headings", height=7)
        for cid, text, w in self.COLUMNS:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w",
                             stretch=cid in ("name", "login", "logout"))
        vsb = ctk.CTkScrollbar(box, height=51, orientation="vertical", command=self.tree.yview)
        hsb = ctk.CTkScrollbar(box, width=51, orientation="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        # color the rows by raw status (tags stay the raw DB value)
        for status, color in (("Active", SUCCESS), ("Completed", "#2f6fed"),
                              ("Forced Logout", CRIMSON),
                              ("Disconnected", MUTED)):
            self.tree.tag_configure(status, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        det = CardFrame(self, text="Session Details")
        det.pack(fill="x", padx=8, pady=(0, 8))
        body = PaddedFrame(det, padding=(8, 6))
        body.pack(fill="x")
        self._det = {}
        for idx, (label, key) in enumerate(self.FIELDS):
            cell = ctk.CTkFrame(body)
            cell.grid(row=idx // 5, column=idx % 5, sticky="w",
                      padx=(0, 16), pady=2)
            ctk.CTkLabel(cell, text=label, font=FONT_SMALL,
                      text_color=SUBTLE).pack(anchor="w")
            value = ctk.CTkLabel(cell, text="\u2014",
                              font=("Segoe UI", 9, "bold"))
            value.pack(anchor="w")
            self._det[key] = value
        for col in range(5):
            body.columnconfigure(col, weight=1)
        ctk.CTkLabel(det,
                  text="Select a session above - Force Logout and View "
                       "Activity act on the selected account.",
                  font=FONT_SMALL, text_color=MUTED).pack(
            anchor="w", padx=8, pady=(0, 6))

    # ------------------------------------------------------------ feedback
    def _feedback(self, text, kind="warn"):
        """Normal events go to the owner's toast (no popup); the dialog is
        only a fallback when no toast channel was provided."""
        if self.notify:
            try:
                self.notify(text, kind)
                return
            except Exception:
                pass
        if kind == "error":
            messagebox.showerror("Error", text, parent=self)
        else:
            messagebox.showwarning("Notice", text, parent=self)

    # ------------------------------------------------------------- reading
    @staticmethod
    def _fmt_secs(secs):
        """Seconds -> '1h 05m' / '4m 12s' ('-' when unknown/zero)."""
        if not secs or int(secs) <= 0:
            return "\u2014"
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"

    @classmethod
    def _fmt_duration(cls, row):
        secs = row["duration_seconds"]
        if not secs and row["status"] == "Active" and row["login_time"]:
            try:                       # live length of a session still open
                start = datetime.strptime(row["login_time"],
                                          "%Y-%m-%d %H:%M:%S")
                secs = int((datetime.now() - start).total_seconds())
            except Exception:
                secs = None
        return cls._fmt_secs(secs)

    def refresh(self):
        """Reload the table + cards through the current instant filters."""
        if self._busy:
            return
        self._busy = True
        try:
            conn = get_connection()
            pcs = ["All"] + [r[0] for r in conn.execute(
                "SELECT DISTINCT pc_name FROM client_sessions "
                "ORDER BY pc_name").fetchall()]
            studs = ["All"] + [r[0] for r in conn.execute(
                "SELECT DISTINCT student_id FROM client_sessions "
                "WHERE student_id IS NOT NULL AND student_id != '' "
                "ORDER BY student_id").fetchall()]
            cnt = conn.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(CASE WHEN status='Active' AND logout_time IS"
                " NULL THEN 1 ELSE 0 END), 0) AS active,"
                " COALESCE(SUM(CASE WHEN login_time LIKE ? THEN 1 ELSE 0"
                " END), 0) AS today,"
                " COALESCE(SUM(CASE WHEN status='Disconnected' THEN 1 ELSE 0"
                " END), 0) AS dead"
                " FROM client_sessions", (now_date() + "%",)).fetchone()
            # keep the PC/Student combos current without dropping the choice
            if list(self.pc_cb.cget("values")) != pcs:
                self.pc_cb.configure(values=pcs)
                if self.pc_var.get() not in pcs:
                    self.pc_var.set("All")     # trace re-enters, _busy blocks
            if list(self.student_cb.cget("values")) != studs:
                self.student_cb.configure(values=studs)
                if self.student_var.get() not in studs:
                    self.student_var.set("All")
            clauses, params = [], []
            term = self.search_var.get().strip()
            if term:
                clauses.append("(session_id LIKE ? OR pc_name LIKE ? OR "
                               "student_id LIKE ? OR "
                               "COALESCE(full_name,'') LIKE ?)")
                params.extend([f"%{term}%"] * 4)
            status = self.status_var.get()
            if status and status != "All":
                clauses.append("status = ?")
                params.append(self.STATUS_RAW.get(status, status))
            pc = self.pc_var.get()
            if pc and pc != "All":
                clauses.append("pc_name = ?")
                params.append(pc)
            stud = self.student_var.get()
            if stud and stud != "All":
                clauses.append("student_id = ?")
                params.append(stud)
            date = self.date_var.get().strip()
            if date:
                clauses.append("login_time LIKE ?")
                params.append(date + "%")
            query = ("SELECT id, session_id, pc_name, student_id, full_name,"
                     " login_time, logout_time, duration_seconds, ip_address,"
                     " status, connection_type FROM client_sessions")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC"
            rows = conn.execute(query, params).fetchall()
            conn.close()
        except Exception:
            rows, cnt = [], None
        finally:
            self._busy = False
        if cnt is not None:
            self._card_vals["active"].configure(text=str(cnt["active"]))
            self._card_vals["today"].configure(text=str(cnt["today"]))
            self._card_vals["total"].configure(text=str(cnt["total"]))
            self._card_vals["dead"].configure(text=str(cnt["dead"]))
        prev = str(self.selected_id) if self.selected_id is not None else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows = {}
        keep = None
        for r in rows:
            iid = str(r["id"])
            self._rows[iid] = r
            status = r["status"] or ""
            self.tree.insert("", "end", iid=iid, values=(
                r["pc_name"], r["student_id"] or "", r["full_name"] or "",
                r["login_time"] or "", r["logout_time"] or "\u2014",
                self._fmt_duration(r), r["connection_type"] or "\u2014",
                self.STATUS_DISPLAY.get(status, status)), tags=(status,))
            if iid == prev:
                keep = iid
        if keep:
            self.tree.selection_set(keep)
            self._show_details(self._rows.get(keep))
        else:
            self.selected_id = None
            self._show_details(None)

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        try:
            self.selected_id = int(sel[0]) if sel else None
        except (ValueError, TypeError):
            self.selected_id = None
        self._show_details(self._rows.get(sel[0]) if sel else None)

    def _selection_row(self):
        if self.selected_id is not None:
            row = self._rows.get(str(self.selected_id))
            if row is not None:
                return row
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _show_details(self, row):
        """Fill the Session Details panel (read-only mirror of the row)."""
        for _label, key in self.FIELDS:
            text = "\u2014"
            if row is not None:
                if key == "_status":
                    text = self.STATUS_DISPLAY.get(row["status"],
                                                   row["status"] or "\u2014")
                elif key == "_duration":
                    text = self._fmt_duration(row)
                else:
                    text = str(row[key] or "\u2014")
            self._det[key].configure(text=text)

    def clear_filters(self):
        """Reset every filter in one click (instant refresh included)."""
        self._busy = True                      # one refresh, not five
        try:
            self.search_var.set("")
            self.status_var.set("All")
            self.pc_var.set("All")
            self.student_var.set("All")
            self.date_var.set("")
        finally:
            self._busy = False
        self.refresh()

    def export_csv(self):
        headers = [text for _, text, _ in self.COLUMNS]
        rows = [list(self.tree.item(i, "values"))
                for i in self.tree.get_children()]
        export_rows_to_csv(headers, rows, default_name="sessions_export.csv",
                           parent=self, notify=self.notify or self._feedback)

    # ------------------------------------------------------------- actions
    def force_logout(self):
        """Confirmed Force Logout of the selected account (spec: dialogs
        are reserved for this - it kicks a live user off the PC)."""
        row = self._selection_row()
        if row is None:
            self._feedback("Select a session first.", "warn")
            return None
        if not self.on_force_logout:
            self._feedback("Force logout is not available.", "error")
            return None
        name = row["full_name"] or row["student_id"] or "?"
        if not messagebox.askyesno(
                "Force Logout",
                f"Force {name} ({row['student_id']}) out of every active "
                f"session?\nAny open session closes immediately and the PC "
                f"returns to the login screen."):
            return None
        try:
            res = self.on_force_logout(row["student_id"]) or {}
        except Exception as e:
            res = {"success": False, "error": str(e)}
        self.refresh()
        if res.get("success"):
            n = int(res.get("forced", 0) or 0)
            self._feedback(
                f"Force logout: {n} session(s) closed for {name}.",
                "success" if n else "warn")
        else:
            self._feedback(res.get("error") or "Could not force logout.",
                           "error")
        return res

    def view_activity(self):
        """Jump to the Audit Trail pre-filtered on the selected account."""
        row = self._selection_row()
        if row is None:
            self._feedback("Select a session first.", "warn")
            return False
        if not self.on_view_activity:
            self._feedback("Audit Trail is not available.", "error")
            return False
        self.on_view_activity(row["student_id"] or row["pc_name"] or "")
        return True


class AccountsFrame(ctk.CTkFrame):
    """Accounts page: search -> select -> Account Details window.

    Four summary cards (Total / Active / Disabled / Accounts in Session),
    instant Search / Status / Course filters, the account table WITHOUT the
    password hash, an Add/Edit dialog that keeps the UserCRUDFrame write
    rules (hash on save, blank keeps the password, fresh students are
    flagged must_change_password) and the Account Details Toplevel with
    Overview / Sessions / Activity Logs / PC Usage tabs."""

    COLUMNS = (("student_id", "Student ID", 115),
               ("full_name", "Full Name", 175),
               ("course", "Course", 110),
               ("year_level", "Year", 95),
               ("email", "Email", 155),
               ("contact", "Contact", 110),
               ("status", "Status", 95),
               ("session", "Session", 135))
    CARDS = (("total", "Total Accounts", ACCENT),
             ("active", "Active", SUCCESS),
             ("disabled", "Disabled", MUTED),
             ("insession", "Accounts in Session", ORANGE))
    STATUS_DISPLAY = {"Active": "ACTIVE", "Inactive": "DISABLED"}
    STATUS_FILTERS = ("All", "ACTIVE", "DISABLED")
    YEARS = ("1st Year", "2nd Year", "3rd Year", "4th Year")
    OVERVIEW = (("Student ID:", "student_id"), ("Full Name:", "full_name"),
                ("Role:", "role"), ("Course:", "course"),
                ("Year Level:", "year_level"), ("Email:", "email"),
                ("Contact No.:", "contact"), ("Status:", "_status"),
                ("Must Change Password:", "_mcp"),
                ("Created:", "created_at"))

    def __init__(self, parent, on_change=None, notify=None):
        super().__init__(parent)
        self.on_change = on_change
        self.notify = notify
        self.selected_id = None
        self._rows = {}
        self._busy = False
        self._form = None
        self._form_vars = {}
        self._form_id = None
        self.details_win = None
        # instant filtering (spec item 4): every control re-runs refresh
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="All")
        self.course_var = tk.StringVar(value="All")
        for var in (self.search_var, self.status_var, self.course_var):
            var.trace_add("write", lambda *_: self.refresh())
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        cards = ctk.CTkFrame(self)
        cards.pack(fill="x", padx=8, pady=(8, 4))
        self._card_vals = {}
        for idx, (key, label, color) in enumerate(self.CARDS):
            tile = ctk.CTkFrame(cards, fg_color="white", height=STAT_TILE_H,
                            border_color=BORDER, border_width=1)
            tile.grid(row=0, column=idx, sticky="nsew", padx=(0, 6))
            tile.grid_propagate(False)
            ctk.CTkFrame(tile, fg_color=color, height=4).pack(fill="x")
            val = ctk.CTkLabel(tile, text="0", font=FONT_STAT_NUM, text_color=BG_DARK,
                           fg_color="white", anchor="w")
            val.pack(fill="x", padx=10, pady=(3, 0))
            ctk.CTkLabel(tile, text=label, font=FONT_SMALL, text_color="#5a6480", fg_color="white",
                     anchor="w").pack(fill="x", padx=10, pady=(0, 4))
            self._card_vals[key] = val
            cards.columnconfigure(idx, weight=1)

        bar = ctk.CTkFrame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkLabel(bar, text="Search:").pack(side="left")
        ctk.CTkEntry(bar, textvariable=self.search_var, width=138).pack(
            side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="Status:").pack(side="left")
        self.status_cb = ctk.CTkComboBox(
            bar, variable=self.status_var, width=108, state="readonly",
            values=list(self.STATUS_FILTERS))
        self.status_cb.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(bar, text="Course:").pack(side="left")
        self.course_cb = ctk.CTkComboBox(bar, variable=self.course_var,
                                      width=170, state="readonly",
                                      values=["All"])
        self.course_cb.pack(side="left", padx=(4, 8))
        ctk.CTkButton(bar, text="Clear",
                   command=self.clear_filters).pack(side="left")
        ctk.CTkButton(bar, text="+ Add Account", command=self.open_add,
                  fg_color=ACCENT, text_color="white", hover_color="#1e4fbf",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="right")

        actions = ctk.CTkFrame(self)
        actions.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkButton(actions, text="View Details",
                   command=self.open_details).pack(side="left", padx=(0, 4))
        ctk.CTkButton(actions, text="Edit Selected",
                   command=self.open_edit).pack(side="left", padx=4)
        ctk.CTkButton(actions, text="Delete Selected",
                   command=self.delete_selected).pack(side="left", padx=4)
        ctk.CTkButton(actions, text="Refresh",
                   command=self.refresh).pack(side="right", padx=(4, 0))
        ctk.CTkButton(actions, text="Export CSV",
                   command=self.export_csv).pack(side="right")

        box = ctk.CTkFrame(self)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree = ttk.Treeview(
            box, columns=[c for c, _, _ in self.COLUMNS],
            show="headings", height=8)
        for cid, text, w in self.COLUMNS:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w",
                             stretch=cid in ("full_name", "email"))
        vsb = ctk.CTkScrollbar(box, height=51, orientation="vertical", command=self.tree.yview)
        hsb = ctk.CTkScrollbar(box, width=51, orientation="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        for status, color in (("Active", SUCCESS), ("Inactive", MUTED)):
            self.tree.tag_configure(status, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self.open_details())

    # ------------------------------------------------------------ feedback
    def _feedback(self, text, kind="warn"):
        """Normal events go to the owner's toast (no popup); the dialog is
        only a fallback when no toast channel was provided."""
        if self.notify:
            try:
                self.notify(text, kind)
                return
            except Exception:
                pass
        if kind == "error":
            messagebox.showerror("Error", text, parent=self)
        else:
            messagebox.showwarning("Notice", text, parent=self)

    def _notify_change(self, action, data):
        """Report a data change to the owner (logout/audit/live-sync)."""
        if self.on_change:
            try:
                self.on_change(action, data or {})
            except TypeError:
                self.on_change()

    # ------------------------------------------------------------- reading
    def refresh(self):
        """Reload the cards + table through the current instant filters."""
        if self._busy:
            return
        self._busy = True
        try:
            conn = get_connection()
            courses = ["All"] + [r[0] for r in conn.execute(
                "SELECT DISTINCT course FROM users "
                "WHERE role='student' AND course IS NOT NULL "
                "AND course != '' ORDER BY course").fetchall()]
            cnt = conn.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END), 0)"
                " AS active FROM users WHERE role='student'").fetchone()
            in_session = {r[0]: r[1] for r in conn.execute(
                "SELECT student_id, pc_name FROM client_sessions "
                "WHERE status='Active'").fetchall()}
            n_in = conn.execute(
                "SELECT COUNT(DISTINCT student_id) FROM client_sessions "
                "WHERE status='Active'").fetchone()[0]
            # keep the Course combo current without dropping the choice
            if list(self.course_cb.cget("values")) != courses:
                self.course_cb.configure(values=courses)
                if self.course_var.get() not in courses:
                    self.course_var.set("All")   # trace re-enters, busy blocks
            clauses, params = ["role='student'"], []
            term = self.search_var.get().strip()
            if term:
                clauses.append("(student_id LIKE ? OR full_name LIKE ? OR "
                               "COALESCE(course,'') LIKE ? OR "
                               "COALESCE(email,'') LIKE ?)")
                params.extend([f"%{term}%"] * 4)
            status = self.status_var.get()
            if status == "ACTIVE":
                clauses.append("status = 'Active'")
            elif status == "DISABLED":
                clauses.append("status != 'Active'")
            course = self.course_var.get()
            if course and course != "All":
                clauses.append("course = ?")
                params.append(course)
            rows = conn.execute(
                "SELECT id, student_id, full_name, course, year_level, email,"
                " contact, status FROM users WHERE "
                + " AND ".join(clauses) + " ORDER BY student_id", params
            ).fetchall()
            conn.close()
        except Exception:
            rows, cnt, in_session, n_in = [], None, {}, 0
        finally:
            self._busy = False
        if cnt is not None:
            total, active = int(cnt["total"]), int(cnt["active"])
            self._card_vals["total"].configure(text=str(total))
            self._card_vals["active"].configure(text=str(active))
            self._card_vals["disabled"].configure(text=str(total - active))
            self._card_vals["insession"].configure(text=str(int(n_in)))
        prev = str(self.selected_id) if self.selected_id is not None else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows = {}
        keep = None
        for r in rows:
            iid = str(r["id"])
            self._rows[iid] = {k: r[k] for k in r.keys()}
            status = r["status"] or ""
            self.tree.insert("", "end", iid=iid, values=(
                r["student_id"], r["full_name"] or "", r["course"] or "",
                r["year_level"] or "", r["email"] or "", r["contact"] or "",
                self.STATUS_DISPLAY.get(status, status.upper() or "-"),
                in_session.get(r["student_id"], "—")), tags=(status,))
            if iid == prev:
                keep = iid
        if keep:
            self.tree.selection_set(keep)
        else:
            self.selected_id = None

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        try:
            self.selected_id = int(sel[0]) if sel else None
        except (ValueError, TypeError):
            self.selected_id = None

    def _selection_row(self):
        if self.selected_id is not None:
            row = self._rows.get(str(self.selected_id))
            if row is not None:
                return row
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def clear_filters(self):
        """Reset every filter in one click (instant refresh included)."""
        self._busy = True                      # one refresh, not three
        try:
            self.search_var.set("")
            self.status_var.set("All")
            self.course_var.set("All")
        finally:
            self._busy = False
        self.refresh()

    def export_csv(self):
        headers = [text for _, text, _ in self.COLUMNS]
        rows = [list(self.tree.item(i, "values"))
                for i in self.tree.get_children()]
        export_rows_to_csv(headers, rows, default_name="accounts_export.csv",
                           parent=self, notify=self.notify or self._feedback)

    # ----------------------------------------------------- add / edit form
    def open_add(self):
        return self._open_form(None)

    def open_edit(self, row=None):
        row = row or self._selection_row()
        if row is None:
            self._feedback("Select an account first.", "warn")
            return None
        return self._open_form(row)

    def _open_form(self, row):
        """Compact Account form (the write rules stay UserCRUDFrame's)."""
        self._close_form()
        win = ctk.CTkToplevel(self)
        win.title("Edit Account" if row else "Add Account")
        set_app_icon(win)
        win.configure(fg_color="#f4f6fb")
        try:
            win.transient(self.winfo_toplevel())
        except Exception:
            pass
        self._form = win
        self._form_id = row.get("id") if row else None
        src = dict(row) if row else {}
        self._form_vars = {
            "student_id": tk.StringVar(value=str(src.get("student_id") or "")),
            "password": tk.StringVar(value=""),
            "full_name": tk.StringVar(value=str(src.get("full_name") or "")),
            "course": tk.StringVar(value=str(src.get("course") or "")),
            "year_level": tk.StringVar(value=str(src.get("year_level") or "")),
            "email": tk.StringVar(value=str(src.get("email") or "")),
            "contact": tk.StringVar(value=str(src.get("contact") or "")),
            "status": tk.StringVar(value=str(src.get("status") or "Active")),
        }
        body = PaddedFrame(win, padding=12)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Edit account" if row else "Add account",
                  font=FONT_HEADER).grid(row=0, column=0, columnspan=2,
                                         sticky="w", pady=(0, 10))
        fields = (
            ("Student ID:", "student_id", None),
            ("Password:", "password", None),
            ("Full Name:", "full_name", None),
            ("Course:", "course", None),
            ("Year Level:", "year_level", self.YEARS),
            ("Email:", "email", None),
            ("Contact No.:", "contact", None),
            ("Status:", "status", ("Active", "Inactive")),
        )
        id_entry = None
        for r, (label, key, options) in enumerate(fields, start=1):
            ctk.CTkLabel(body, text=label).grid(row=r, column=0, sticky="w",
                                             padx=(0, 8), pady=4)
            if options is None:
                entry = ctk.CTkEntry(body, textvariable=self._form_vars[key],
                                  width=186)
                entry.grid(row=r, column=1, sticky="ew", pady=4)
                if key == "student_id":
                    id_entry = entry
                elif key == "password":
                    entry.configure(show="•")   # never echo a secret
            else:
                ctk.CTkComboBox(body, variable=self._form_vars[key],
                             values=list(options), width=174,
                             state="readonly").grid(row=r, column=1,
                                                    sticky="ew", pady=4)
        ctk.CTkLabel(body,
                  text=("Password: leave blank to keep the current one."
                        if row else
                        "Password: leave blank to use the default"
                        f" ({DEFAULT_CLIENT_PASSWORD}); the student"
                        " must change it at first login."),
                  font=FONT_SMALL, text_color=MUTED).grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w",
            pady=(6, 0))
        btns = ctk.CTkFrame(body)
        btns.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e",
                  pady=(12, 0))
        ctk.CTkButton(btns, text="Cancel",
                   command=self._close_form).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Save", command=self._save_form, fg_color=ACCENT,
                  text_color="white", hover_color="#1e4fbf",
                  cursor="hand2",
                  font=("Segoe UI", 9, "bold"), ).pack(
            side="left")
        body.grid_columnconfigure(1, weight=1)
        win.bind("<Return>", lambda _e: self._save_form())
        win.bind("<Escape>", lambda _e: self._close_form())
        try:
            win.update_idletasks()
            win.grab_set()
        except Exception:
            pass
        if id_entry is not None:
            try:
                id_entry.focus_set()
            except Exception:
                pass
        center_window(win, 470, 440)
        return win

    def _close_form(self):
        if self._form is not None:
            try:
                self._form.destroy()
            except Exception:
                pass
        self._form = None
        self._form_vars = {}
        self._form_id = None

    def _save_form(self):
        """Hash on save / blank keeps the password on edit / blank falls
        back to the factory default on add / students flagged
        must_change_password - the exact UserCRUDFrame semantics."""
        if self._form is None:
            return False
        values = {k: v.get().strip() for k, v in self._form_vars.items()}
        password = values.pop("password", "")
        missing = [lbl for lbl, key in (("Student ID", "student_id"),
                                        ("Full Name", "full_name"))
                   if not values.get(key)]
        if missing:
            self._feedback("Please fill in: " + ", ".join(missing), "warn")
            return False
        conn = get_connection()
        try:
            if self._form_id is None:
                if not password:
                    # P2-10: blank falls back to the factory default, so
                    # the new account is always usable.  The account is
                    # flagged AND the server treats that exact plaintext
                    # as still-default, so the very first login on the
                    # kiosk is forced through a password change.
                    password = DEFAULT_CLIENT_PASSWORD
                values["password"] = hash_password(password)
                values["role"] = "student"
                values["must_change_password"] = 1
                cols = ", ".join(values.keys())
                ph = ", ".join(["?"] * len(values))
                conn.execute(f"INSERT INTO users ({cols}) VALUES ({ph})",
                             list(values.values()))
                action = "add"
                payload = {k: v for k, v in values.items()
                           if k != "password"}
            else:
                old = conn.execute("SELECT student_id FROM users WHERE id=?",
                                   (self._form_id,)).fetchone()
                old_sid = old["student_id"] if old else ""
                if password:
                    values["password"] = hash_password(password)
                sets = ", ".join(f"{k}=?" for k in values.keys())
                conn.execute(f"UPDATE users SET {sets} WHERE id=?",
                             list(values.values()) + [self._form_id])
                action = "update"
                payload = {**values, "_old_student_id": old_sid}
            conn.commit()
        except Exception as e:
            conn.close()
            if "UNIQUE constraint" in str(e):
                self._feedback(
                    f"'{values.get('student_id')}' is already in use.", "warn")
            else:
                self._feedback(f"Could not save the account: {e}", "error")
            return False
        conn.close()
        self._close_form()
        self.refresh()
        self._notify_change(action, payload)
        return True

    def delete_selected(self):
        row = self._selection_row()
        if row is None:
            self._feedback("Select an account first.", "warn")
            return False
        # destructive action -> confirmation dialog stays (spec allows it)
        if not messagebox.askyesno(
                "Delete account",
                f"Delete '{row['student_id']}' ({row['full_name'] or 'no name'})"
                " and every credential it has? This cannot be undone."):
            return False
        conn = get_connection()
        conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        self.selected_id = None
        self.refresh()
        self._notify_change("delete", {k: row[k] for k in row.keys()})
        return True

    # ------------------------------------------------- Account Details win
    def open_details(self, row=None):
        """The 'select -> Account Details window' step: Overview, Sessions,
        Activity Logs and PC Usage for the selected account."""
        row = row or self._selection_row()
        if row is None:
            self._feedback("Select an account first.", "warn")
            return None
        self.close_details()
        conn = get_connection()
        acc = conn.execute(
            "SELECT id, student_id, full_name, role, course, year_level,"
            " email, contact, status, must_change_password, created_at"
            " FROM users WHERE id=?", (row["id"],)).fetchone()
        if acc is None:                          # stale selection
            conn.close()
            self.refresh()
            self._feedback("That account no longer exists.", "warn")
            return None
        sid = acc["student_id"]
        sessions = conn.execute(
            "SELECT pc_name, login_time, logout_time, duration_seconds,"
            " status, connection_type FROM client_sessions WHERE student_id=?"
            " ORDER BY id DESC LIMIT 100", (sid,)).fetchall()
        admin_rows = conn.execute(
            "SELECT timestamp, admin_user, action, target, details FROM"
            " admin_activity_log WHERE target=? OR details LIKE ?"
            " ORDER BY timestamp DESC LIMIT 100",
            (sid, f"%{sid}%")).fetchall()
        client_rows = conn.execute(
            "SELECT created_at, severity, category, message FROM client_logs"
            " WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
            (sid,)).fetchall()
        usage = conn.execute(
            "SELECT pc_name, COUNT(*) AS n,"
            " COALESCE(SUM(duration_seconds), 0) AS secs, MAX(login_time)"
            " AS last FROM client_sessions WHERE student_id=? GROUP BY"
            " pc_name ORDER BY n DESC, pc_name", (sid,)).fetchall()
        conn.close()

        win = ctk.CTkToplevel(self)
        win.title(f"Account Details - {sid}")
        set_app_icon(win)
        win.configure(fg_color="#f4f6fb")
        try:
            win.transient(self.winfo_toplevel())
        except Exception:
            pass
        self.details_win = win
        # Spec 7: window title + hairline above the tab strip, so the dialog
        # reads like the rest of the application instead of a bare form.
        ctk.CTkLabel(win, text="Account Details",
                     font=("Segoe UI", 15, "bold"),
                     text_color="#1f2a44", fg_color="#f4f6fb",
                     anchor="w").pack(fill="x", padx=14, pady=(12, 0))
        Divider(win, fg_color="#d7ddea").pack(fill="x", padx=14, pady=(6, 0))
        nb = TabHost(win)
        nb.pack(fill="both", expand=True, padx=8, pady=(6, 8))
        self.details_tabs = nb

        # ---- Overview
        ov = PaddedFrame(nb.add("Overview"), padding=12)
        ov.pack(fill="both", expand=True)
        self._detail_labels = {}
        for idx, (label, key) in enumerate(self.OVERVIEW):
            cell = ctk.CTkFrame(ov)
            cell.grid(row=idx // 2, column=idx % 2, sticky="w",
                      padx=(0, 26), pady=5)
            ctk.CTkLabel(cell, text=label, font=FONT_SMALL,
                      text_color=SUBTLE).pack(anchor="w")
            if key == "_status":
                text = self.STATUS_DISPLAY.get(acc["status"],
                                               (acc["status"] or "—").upper())
            elif key == "_mcp":
                text = "Yes" if acc["must_change_password"] else "No"
            else:
                text = str(acc[key] or "—")
            value = ctk.CTkLabel(cell, text=text, font=("Segoe UI", 9, "bold"))
            value.pack(anchor="w")
            self._detail_labels[key] = value
        for col in range(2):
            ov.columnconfigure(col, weight=1)

        # ---- Sessions
        st = PaddedFrame(nb.add("Sessions"), padding=8)
        st.pack(fill="both", expand=True)
        self._detail_session_tree = self._detail_tree(
            st, (("pc", "PC", 130), ("login", "Login", 150),
                 ("logout", "Logout", 150), ("duration", "Duration", 95),
                 ("status", "Status", 130), ("conn", "Connection", 105)))
        for r in sessions:
            display = SessionsFrame.STATUS_DISPLAY.get(r["status"],
                                                       r["status"] or "—")
            self._detail_session_tree.insert("", "end", values=(
                r["pc_name"], r["login_time"] or "—",
                r["logout_time"] or "—",
                SessionsFrame._fmt_duration(r), display,
                r["connection_type"] or "—"), tags=(r["status"] or "",))

        # ---- Activity Logs (server audit + client-forwarded events)
        at = PaddedFrame(nb.add("Activity Logs"), padding=8)
        at.pack(fill="both", expand=True)
        self._detail_activity_tree = self._detail_tree(
            at, (("time", "Timestamp", 150), ("source", "Source", 95),
                 ("action", "Action", 165), ("details", "Details", 430)))
        combined = [(r["timestamp"], "Audit",
                     audit_action_text(r["action"]),
                     f"{r['admin_user']} → {r['target'] or '—'}: "
                     f"{r['details'] or ''}".strip(": "))
                    for r in admin_rows]
        combined += [(r["created_at"], "Client",
                      f"{r['category'] or r['severity']}",
                      r["message"] or "") for r in client_rows]
        combined.sort(key=lambda t: t[0] or "", reverse=True)
        for ts, source, action, details in combined[:200]:
            self._detail_activity_tree.insert("", "end", values=(
                ts or "—", source, action, details))

        # ---- PC Usage
        pt = PaddedFrame(nb.add("PC Usage"), padding=8)
        pt.pack(fill="both", expand=True)
        self._detail_usage_tree = self._detail_tree(
            pt, (("pc", "PC", 160), ("sessions", "Sessions", 110),
                 ("time", "Total Time", 140), ("last", "Last Used", 170)))
        for r in usage:
            self._detail_usage_tree.insert("", "end", values=(
                r["pc_name"], int(r["n"]),
                SessionsFrame._fmt_secs(r["secs"]), r["last"] or "—"))

        ctk.CTkButton(win, text="Close",
                   command=self.close_details).pack(pady=(0, 10))
        win.bind("<Escape>", lambda _e: self.close_details())
        try:
            win.update_idletasks()
        except Exception:
            pass
        center_window(win, 780, 520)
        return win

    @staticmethod
    def _detail_tree(parent, columns):
        box = ctk.CTkFrame(parent)
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=[c for c, _, _ in columns],
                            show="headings", height=10)
        for cid, text, w in columns:
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor="w",
                        stretch=cid in ("details", "action"))
        vsb = ctk.CTkScrollbar(box, height=51, orientation="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        return tree

    def close_details(self):
        # Tolerant by design: this also doubles as the initializer for
        # frames that REUSE this window (StaffAccountsFrame), so it must be
        # safe to call before the attributes have ever been set.
        win = getattr(self, "details_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self.details_win = None
        self.details_tabs = None
        self._detail_labels = {}
        self._detail_session_tree = None
        self._detail_activity_tree = None
        self._detail_usage_tree = None


class StaffAccountsFrame(UserCRUDFrame):
    """Staff & Admin accounts page (P2-9): the normal UserCRUDFrame - same
    write rules (maintenance role, hash on add, blank password keeps the
    stored hash) - plus the very same Account Details window the student
    Accounts page opens.

    The window is REUSED, not re-implemented: the members below are the
    exact objects AccountsFrame defines, so a fix there is automatically a
    fix here and the two pages can never drift apart.  Passwords are never
    shown in either one.

    R3: the form is sectioned (ACCOUNT INFORMATION / PASSWORD
    MANAGEMENT), the table shows a derived Last Login column instead of
    the password, and the row-scoped actions live in the search row -
    [Enable / Disable] and [Revoke Sessions] - plus [Change Password]
    inside the password section."""

    # --- the shared Account Details machinery (crud_frame.AccountsFrame) --
    open_details = AccountsFrame.open_details
    close_details = AccountsFrame.close_details
    _detail_tree = staticmethod(AccountsFrame._detail_tree)
    STATUS_DISPLAY = AccountsFrame.STATUS_DISPLAY
    OVERVIEW = AccountsFrame.OVERVIEW

    # no card title: the two section headers ARE the form's titles
    form_title = ""
    PASSWORD_GROUP = "PASSWORD MANAGEMENT"

    def __init__(self, *args, on_revoke=None, **kwargs):
        # Session revocation needs the server, which the frame must not
        # know about: the dashboard passes this callback.  Popped before
        # super().__init__ so the base constructor never sees it.
        self.on_revoke = on_revoke
        self._last_login_map = {}
        super().__init__(*args, **kwargs)
        # close_details() is the single place that declares the window
        # attributes, so reuse it as the initializer too - zero duplication.
        self.close_details()
        # Row-scoped actions ride in the search row (right end): the
        # button bar is already at its width budget at the 1080x700
        # minimum window - its 6 buttons + Export fill the row - and all
        # of these act on the selected row just like Search does.
        if self._search_row is not None:
            ctk.CTkButton(self._search_row, text="[Revoke Sessions]",
                       command=self.revoke_sessions).pack(
                side="right", padx=(0, 6))
            ctk.CTkButton(self._search_row, text="[Enable / Disable]",
                       command=self.toggle_status).pack(
                side="right", padx=(0, 6))
            ctk.CTkButton(self._search_row, text="[View Details]",
                       command=self.open_details).pack(
                side="right", padx=(0, 6))

    def _selection_row(self):
        """The selected staff/admin/maintenance row, read straight from the
        table so it never depends on the select event having been pumped."""
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0])["values"]
        columns = ["id"] + [f["name"] for f in self.table_fields]
        return dict(zip(columns, vals))

    # ---------------------------------------------------- form section
    def _form_section_extra(self, form, group, first_row, nrows):
        """[Change Password] sits on the password field's own grid row
        (section column 1), so the action adds no height at all."""
        if group != self.PASSWORD_GROUP or not nrows:
            return
        ctk.CTkButton(form, text="Change Password",
                   command=self.change_password,
                   font=("Segoe UI", 9, "bold"), cursor="hand2").grid(
            row=first_row, column=1, sticky="w", padx=8, pady=(24, 6))

    # -------------------------------------------------- change password
    def change_password(self):
        """R3: new + confirm dialog for the selected account.  Nothing is
        written until the pair is non-empty and matching; the value is
        hashed with the same hash_password() every login path uses."""
        row = self._selection_row()
        if not row:
            self._feedback("Select an account from the table first.", "warn")
            return
        sid = str(row.get("student_id") or "")
        name = str(row.get("full_name") or "")
        self._pw_row = row
        win = ctk.CTkToplevel(self)
        win.title("Change Password")
        set_app_icon(win)
        win.configure(fg_color="#f4f6fb")
        try:
            win.transient(self.winfo_toplevel())
        except Exception:
            pass
        self._pw_win = win
        body = PaddedFrame(win, padding=14)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Change Password",
                  font=FONT_HEADER).grid(row=0, column=0, columnspan=2,
                                         sticky="w", pady=(0, 10))
        ctk.CTkLabel(body, text="Account:").grid(row=1, column=0, sticky="w",
                                              padx=(0, 8), pady=4)
        ctk.CTkLabel(body, text=f"{name} ({sid})",
                  font=("Segoe UI", 9, "bold")).grid(row=1, column=1,
                                                     sticky="w", pady=4)
        self._pw_vars = {"new": tk.StringVar(), "conf": tk.StringVar()}

        def _pw_row_grid(r, label, key):
            ctk.CTkLabel(body, text=label).grid(row=r, column=0, sticky="w",
                                             padx=(0, 8), pady=4)
            holder = ctk.CTkFrame(body, fg_color="transparent")
            holder.grid(row=r, column=1, sticky="ew", pady=4)
            ent = ctk.CTkEntry(holder, textvariable=self._pw_vars[key],
                           width=180, show="\u25cf")
            ent.pack(side="left", fill="x", expand=True)
            state = {"shown": False}

            def _toggle():
                state["shown"] = not state["shown"]
                ent.configure(show="" if state["shown"] else "\u25cf")
                btn.configure(text="Hide" if state["shown"] else "Show")

            btn = ctk.CTkButton(holder, text="Show", command=_toggle,
                           fg_color="#eef2fb", text_color="#374151",
                           hover_color="#dde3f0", font=("Segoe UI", 8,
                                                         "bold"),
                           cursor="hand2")
            btn.pack(side="left", padx=(6, 0))
            return ent

        new_ent = _pw_row_grid(2, "New password:", "new")
        _pw_row_grid(3, "Confirm password:", "conf")
        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ctk.CTkButton(btns, text="Cancel",
                   command=self._close_password_dialog).pack(
            side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="Save Password", command=self._save_password,
                   fg_color=ACCENT, text_color="white",
                   hover_color="#1e4fbf", font=("Segoe UI", 9, "bold"),
                   cursor="hand2").pack(side="left")
        win.bind("<Return>", lambda _e: self._save_password())
        win.bind("<Escape>", lambda _e: self._close_password_dialog())
        try:
            # two passes: CTk canvases settle one cycle late, and a shy
            # reqheight would clip the Cancel / Save row at the bottom
            win.update_idletasks()
            w = max(win.winfo_reqwidth(), 420)
            h = win.winfo_reqheight()
            win.update_idletasks()
            w = max(w, win.winfo_reqwidth(), 420)
            h = max(h, win.winfo_reqheight())
            center_window(win, w, h + 6)
            win.grab_set()
            new_ent.focus_set()
        except Exception:
            pass

    def _save_password(self):
        new = self._pw_vars["new"].get()
        conf = self._pw_vars["conf"].get()
        if not new:
            self._feedback("Enter a new password.", "warn")
            return False
        if new != conf:
            self._feedback("The two passwords do not match - type them "
                           "again.", "warn")
            return False
        row = self._pw_row
        conn = get_connection()
        try:
            conn.execute(
                f"UPDATE {self.table} SET password = ? "
                f"WHERE {self.id_field} = ?",
                (hash_password(new), row["id"]))
            conn.commit()
        except Exception as e:
            conn.close()
            self._feedback(f"Could not change the password: {e}", "error")
            return False
        conn.close()
        self._close_password_dialog()
        self.refresh()
        self._feedback(f"Password changed for "
                       f"{row.get('student_id') or row.get('full_name')}.",
                       "success")
        return True

    def _close_password_dialog(self):
        win = getattr(self, "_pw_win", None)
        self._pw_win = None
        self._pw_vars = None
        self._pw_row = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    # ------------------------------------------------ enable / disable
    def toggle_status(self):
        """[Enable / Disable]: flip the selected account's status.
        Disabling confirms first; afterwards the dashboard's
        account-change hook logs the account out of any live session and
        pushes the refresh to every view."""
        row = self._selection_row()
        if not row:
            self._feedback("Select an account from the table first.", "warn")
            return
        sid = str(row.get("student_id") or "")
        new = ("Inactive"
               if str(row.get("status") or "Active").lower() == "active"
               else "Active")
        if new == "Inactive" and not messagebox.askyesno(
                "Confirm disable",
                f"Disable {sid}?\n\nThe account cannot sign in and any "
                "active session is logged out.",
                parent=self):
            return
        conn = get_connection()
        try:
            conn.execute(f"UPDATE {self.table} SET status = ? "
                         f"WHERE {self.id_field} = ?",
                         (new, row["id"]))
            conn.commit()
        except Exception as e:
            conn.close()
            self._feedback(f"Could not change the account status: {e}",
                           "error")
            return
        conn.close()
        self.refresh()
        self._notify_change("update", {"student_id": sid, "status": new})

    # ------------------------------------------------- revoke sessions
    def revoke_sessions(self):
        """[Revoke Sessions]: force the selected account out of every
        live client session.  The dashboard callback does the server work
        (the frame holds no server reference); the audit entry and the
        session_forced event come from force_logout_user itself."""
        row = self._selection_row()
        if not row:
            self._feedback("Select an account from the table first.", "warn")
            return
        sid = str(row.get("student_id") or "")
        if not self.on_revoke:
            self._feedback("Revoke Sessions needs a server connection.",
                           "warn")
            return
        try:
            res = self.on_revoke(sid) or {}
        except Exception as e:
            self._feedback(f"Could not revoke sessions: {e}", "error")
            return
        if not res.get("success", True):
            self._feedback(f"Could not revoke sessions: "
                           f"{res.get('error') or 'unknown error'}.", "error")
            return
        forced = int(res.get("forced", 0) or 0)
        if forced:
            self._feedback(f"Revoked {forced} active session(s) for {sid}.",
                           "warn")
        else:
            self._feedback(f"{sid} has no active session to revoke.", "info")

    # -------------------------------------------------- derived column
    def _prefetch_derived(self, rows):
        """Last Login is not a users column: it is the newest sign-in the
        audit trail recorded for that account - login_success is written
        by TCP client logins AND server console sign-ins (R1),
        client_admin_login by an admin signing in on a kiosk PC."""
        self._last_login_map = {}
        try:
            conn = get_connection()
            rs = conn.execute(
                "SELECT admin_user, MAX(timestamp) AS ts FROM "
                "admin_activity_log WHERE action IN "
                "('login_success', 'client_admin_login') "
                "GROUP BY admin_user").fetchall()
            conn.close()
            self._last_login_map = {str(r["admin_user"]): str(r["ts"])
                                    for r in rs}
        except Exception:
            self._last_login_map = {}

    def _derived_value(self, name, row):
        if name != "last_login":
            return None
        sid = (str(row["student_id"])
               if "student_id" in row.keys() else "")
        return self._last_login_map.get(sid) or "\u2014"
