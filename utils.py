"""
utils.py
Shared helpers: CSV export, styling constants, small UI helpers.
"""

import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

# ---- Color / style constants -------------------------------------------------
BG_DARK = "#1f2a44"
BG_LIGHT = "#f4f6fb"
ACCENT = "#2f6fed"
ACCENT_DARK = "#1e4fbf"
DANGER = "#e5484d"
SUCCESS = "#2fa84f"
TEXT_LIGHT = "#ffffff"
FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_LABEL = ("Segoe UI", 10)
FONT_HEADER = ("Segoe UI", 12, "bold")


def style_app(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Treeview", rowheight=26, font=("Segoe UI", 9))
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
    style.configure("TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(14, 8))
    style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
    return style


def export_rows_to_csv(headers, rows, default_name="export.csv", parent=None):
    """rows: list of tuples/lists in the same order as headers."""
    if not rows:
        messagebox.showinfo("Export CSV", "There is no data to export.", parent=parent)
        return
    path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("CSV files", "*.csv")],
        initialfile=default_name,
        title="Save report as CSV",
    )
    if not path:
        return
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        messagebox.showinfo("Export CSV", f"Report exported successfully to:\n{path}", parent=parent)
    except Exception as e:
        messagebox.showerror("Export CSV", f"Failed to export CSV:\n{e}", parent=parent)


def now_date():
    return datetime.now().strftime("%Y-%m-%d")


def now_time():
    return datetime.now().strftime("%H:%M:%S")


def now_datetime():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def center_window(win, width, height):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - width) // 2
    y = (sh - height) // 2
    win.geometry(f"{width}x{height}+{x}+{y}")
