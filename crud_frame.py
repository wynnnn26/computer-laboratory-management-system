"""
crud_frame.py
A generic, reusable "table + form" CRUD component built on top of a single
SQLite table. Every management tab in the admin dashboard (students,
computers, inventory, borrowing, maintenance, announcements, OJT interns,
tasks, attendance) is built from this one class, configured differently.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from database import get_connection, hash_password
from utils import export_rows_to_csv, FONT_HEADER, ACCENT


class CRUDFrame(ttk.Frame):
    def __init__(self, parent, table, fields, title,
                 fixed_values=None, where_clause=None, order_by=None,
                 on_change=None, id_field="id", allow_delete=True,
                 search_field=None, defaults=None):
        """
        table         : SQL table name
        fields        : list of dicts: {name, label, type: entry|combobox|readonly, options?}
        title         : heading text shown at the top of the tab
        fixed_values  : dict of extra column->value always applied on INSERT (e.g. role='student')
        where_clause  : SQL WHERE clause (without 'WHERE') restricting rows shown, e.g. "role='student'"
        order_by      : SQL ORDER BY column
        on_change     : optional callback() fired after any insert/update/delete (e.g. refresh dropdowns)
        search_field  : field name to filter the table live via a search box
        """
        super().__init__(parent)
        self.table = table
        self.fields = fields
        self.fixed_values = fixed_values or {}
        self.where_clause = where_clause
        self.order_by = order_by or "id DESC"
        self.on_change = on_change
        self.id_field = id_field
        self.allow_delete = allow_delete
        self.search_field = search_field
        self.defaults = defaults or {}
        self.selected_id = None
        self.selected_row = None

        self._build_ui(title)
        self.refresh()
        self._apply_defaults()

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
        ttk.Label(self, text=title, font=FONT_HEADER).pack(anchor="w", padx=12, pady=(10, 4))

        # ---- form area ----
        form = ttk.LabelFrame(self, text="Record details")
        form.pack(fill="x", padx=12, pady=6)
        self.entries = {}
        cols_per_row = 3
        for idx, f in enumerate(self.fields):
            r, c = divmod(idx, cols_per_row)
            cell = ttk.Frame(form)
            cell.grid(row=r, column=c, padx=8, pady=6, sticky="w")
            ttk.Label(cell, text=f["label"] + ":").pack(anchor="w")
            if f["type"] == "combobox":
                var = tk.StringVar()
                widget = ttk.Combobox(cell, textvariable=var, values=f.get("options", []),
                                       state="readonly", width=24)
                widget.pack()
            elif f["type"] == "text":
                widget = tk.Text(cell, width=26, height=3)
                widget.pack()
                var = None
            else:
                var = tk.StringVar()
                widget = ttk.Entry(cell, textvariable=var, width=27)
                widget.pack()
            self.entries[f["name"]] = (widget, var, f["type"])

        # ---- buttons ----
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(btns, text="Add New", command=self.add_record).pack(side="left", padx=4)
        ttk.Button(btns, text="Update Selected", command=self.update_record).pack(side="left", padx=4)
        if self.allow_delete:
            ttk.Button(btns, text="Delete Selected", command=self.delete_record).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear Form", command=self.clear_form).pack(side="left", padx=4)
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(btns, text="Export to CSV", command=self.export_csv).pack(side="right", padx=4)

        if self.search_field:
            search_frame = ttk.Frame(self)
            search_frame.pack(fill="x", padx=12, pady=(0, 4))
            ttk.Label(search_frame, text="Search:").pack(side="left")
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write", lambda *a: self.refresh())
            ttk.Entry(search_frame, textvariable=self.search_var, width=30).pack(side="left", padx=6)

        # ---- table ----
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)
        columns = ["id"] + [f["name"] for f in self.fields]
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        for col in columns:
            label = "ID" if col == "id" else next((f["label"] for f in self.fields if f["name"] == col), col)
            self.tree.heading(col, text=label)
            self.tree.column(col, width=110, anchor="w")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

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
        columns = ["id"] + [f["name"] for f in self.fields]
        row = dict(zip(columns, vals))
        self.selected_id = row["id"]
        self.selected_row = row
        self._set_form_values(row)

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
        columns = ["id"] + [f["name"] for f in self.fields]
        for row in rows:
            values = [row[c] if c in row.keys() else "" for c in columns]
            self.tree.insert("", "end", values=values)

    def add_record(self):
        values = self._get_form_values()
        if not values:
            return
        missing = [f["label"] for f in self.fields if f.get("required") and not values.get(f["name"])]
        if missing:
            messagebox.showwarning("Missing information", "Please fill in: " + ", ".join(missing))
            return
        all_values = {**values, **self.fixed_values}
        cols = ", ".join(all_values.keys())
        placeholders = ", ".join(["?"] * len(all_values))
        conn = get_connection()
        try:
            conn.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({placeholders})", list(all_values.values()))
            conn.commit()
        except Exception as e:
            messagebox.showerror("Error", f"Could not add record:\n{e}")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("add", all_values)

    def update_record(self):
        if not self.selected_id:
            messagebox.showwarning("No selection", "Select a record from the table first.")
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
            messagebox.showerror("Error", f"Could not update record:\n{e}")
            conn.close()
            return
        conn.close()
        self.clear_form()
        self.refresh()
        self._notify_change("update", {**values, "_old_student_id": old_student_id})

    def delete_record(self):
        if not self.selected_id:
            messagebox.showwarning("No selection", "Select a record from the table first.")
            return
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
        columns = ["id"] + [f["name"] for f in self.fields]
        headers = ["ID"] + [f["label"] for f in self.fields]
        rows = [self.tree.item(i)["values"] for i in self.tree.get_children()]
        export_rows_to_csv(headers, rows, default_name=f"{self.table}_export.csv", parent=self)


class UserCRUDFrame(CRUDFrame):
    """
    Specialized CRUD frame for the 'users' table (students or admins).
    Hashes the password field on add. On update, leaving the password
    field blank keeps the existing password unchanged.
    """

    def add_record(self):
        values = self._get_form_values()
        if values.get("password"):
            values["password"] = hash_password(values["password"])
        # Temporarily inject hashed password back through the normal flow
        self._pending_values = values
        if self._add_record_with(values):
            self._notify_change("add", {k: v for k, v in values.items()
                                        if k != "password"})

    def _add_record_with(self, values):
        missing = [f["label"] for f in self.fields if f.get("required") and not values.get(f["name"])]
        if missing:
            messagebox.showwarning("Missing information", "Please fill in: " + ", ".join(missing))
            return False
        all_values = {**values, **self.fixed_values}
        cols = ", ".join(all_values.keys())
        placeholders = ", ".join(["?"] * len(all_values))
        conn = get_connection()
        try:
            conn.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({placeholders})", list(all_values.values()))
            conn.commit()
        except Exception as e:
            messagebox.showerror("Error", f"Could not add account:\n{e}")
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
            messagebox.showwarning("No selection", "Select an account from the table first.")
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
            messagebox.showerror("Error", f"Could not update account:\n{e}")
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
        columns = ["id"] + [f["name"] for f in self.fields]
        for row in rows:
            values = []
            for c in columns:
                v = row[c] if c in row.keys() else ""
                if c == "password":
                    v = "********"
                values.append(v)
            self.tree.insert("", "end", values=values)
