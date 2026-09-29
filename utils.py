"""
utils.py
Shared helpers: CSV export, styling constants, small UI helpers.
"""

import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

# ---- Color / style constants -------------------------------------------------
# Design tokens now live in theme.py - the single source of truth for the
# whole product (colours, typography, spacing, radius, status tones).  They
# are re-exported here so every existing `from utils import ACCENT, ...`
# reference keeps working without touching hundreds of call sites.
from theme import (
    BG_DARK, BG_DEEP, BG_LIGHT, CARD, CARD_ALT, SIDEBAR, SIDEBAR_HOVER,
    SIDEBAR_ACTIVE, ACCENT, ACCENT_DARK, ACCENT_SOFT, DANGER, DANGER_DARK,
    SUCCESS, SUCCESS_BRIGHT, WARN, ORANGE, PURPLE, CRIMSON, SKY, ONLINE,
    TEXT_LIGHT, TEXT, SUBTLE, MUTED, BORDER, BORDER_SOFT, OFFLINE,
    PLACEHOLDER, FONT_FAMILY, FONT_TITLE, FONT_SUBTITLE, FONT_HEADER,
    FONT_CARD_NAME, FONT_UI, FONT_LABEL, FONT_BODY, FONT_SMALL,
    FONT_STAT_NUM, FONT_STAT_LABEL, FONT_MONO, FONT_SIZES,
    RADIUS, RADIUS_SM, RADIUS_LG, BORDER_WIDTH,
    SPACE_XS, SPACE_S, SPACE_M, SPACE_L, PAD_PAGE, PAD_CARD, GAP,
    STAT_TILE_H, SIDEBAR_W, HEADER_H, DETAIL_W, MIN_W, MIN_H,
    BTN_H, BTN_H_SM, SEARCH_HINT, BUTTON_KINDS, button,
    STATUS_LABELS, STATUS_COLORS, STATUS_ORDER, status_key,
    SERVER_UP, SERVER_DOWN, TOAST_COLORS, configure_theme,
    APPEARANCE, COLOR_THEME,
)

# ---- PC status system ----------------------------------------------------
# Kept in theme.py (imported above) so the Server and the Client always
# share one set of labels, colours and icons.  The SERVER remains the
# single authoritative source (server.derive_status); these only render it.


def render_status_tile(state, size=256):
    """Draw the PC status tile used everywhere (matches the reference
    artwork): dark rounded tile, white monitor outline, colored screen and
    the colored status dot below the monitor. Square canvas -> resizing to
    any square size never distorts the monitor shape."""
    from PIL import Image, ImageDraw
    key = status_key(state)
    color = STATUS_COLORS[key]
    TILE = (13, 17, 23, 255)          # dark tile background
    WHITE = (255, 255, 255, 255)      # monitor outline
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 255, 255], radius=54, fill=TILE)
    # monitor body + colored screen
    d.rounded_rectangle([40, 34, 216, 150], radius=18,
                        fill=TILE, outline=WHITE, width=7)
    d.rounded_rectangle([56, 50, 200, 128], radius=8, fill=color)
    # small circle under the screen (power LED)
    d.ellipse([118, 129, 138, 149], fill=TILE, outline=WHITE, width=4)
    # neck + base
    d.line([(112, 152), (104, 178)], fill=WHITE, width=6)
    d.line([(144, 152), (152, 178)], fill=WHITE, width=6)
    d.rounded_rectangle([80, 178, 176, 192], radius=7,
                        fill=TILE, outline=WHITE, width=6)
    # status dot below the monitor
    d.ellipse([114, 210, 142, 238], fill=color)
    if size != 256:
        resample = getattr(Image, "Resampling", Image)
        img = img.resize((int(size), int(size)), resample.LANCZOS)
    return img


_ICON_CACHE = {}


def get_status_icon(state, size=48):
    """Tk PhotoImage of the monitor status icon.

    Reads pc_icons/<state>.png (bundled with --add-data, see build_exe.bat)
    and falls back to the generated artwork if the file is missing. Results
    are cached per (state, size); the square source keeps the icon
    distortion-free at every size."""
    key = status_key(state)
    ck = (key, int(size))
    if ck in _ICON_CACHE:
        return _ICON_CACHE[ck]
    try:
        import os
        import sys
        if getattr(sys, "frozen", False):
            search = [getattr(sys, "_MEIPASS", ""),
                      os.path.dirname(os.path.abspath(sys.executable))]
        else:
            search = [os.path.dirname(os.path.abspath(__file__))]
        path = None
        for d in search:
            if not d:
                continue
            p = os.path.join(d, "pc_icons", f"{key}.png")
            if os.path.exists(p):
                path = p
                break
        from PIL import Image, ImageTk
        if path:
            img = Image.open(path).convert("RGBA")
        else:
            img = render_status_tile(key, 256)
        if img.size != (size, size):
            resample = getattr(Image, "Resampling", Image)
            img = img.resize((int(size), int(size)), resample.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        _ICON_CACHE[ck] = photo
        return photo
    except Exception:
        return None


def get_logo(size=96, master=None):
    """Tk PhotoImage of the official logo, scaled to `size` px.

    Reads assets/images/logo.png (the single source asset, bundled with
    --add-data - see build_exe.bat) and resizes it with LANCZOS on a
    square canvas, so the seal keeps its aspect ratio and transparency at
    any size.

    PhotoImages belong to one Tk interpreter only, and this app creates
    several (Launcher -> LoginWindow -> ClientApp are all tk.Tk), so the
    cache is keyed by the caller's interpreter via `master`.  The caller
    must keep the returned reference (Tk does not GC PhotoImages)."""
    tki = None
    try:
        if master is not None:
            tki = master.tk
    except Exception:
        tki = None
    if tki is None:
        root = getattr(tk, "_default_root", None)
        tki = getattr(root, "tk", None) if root is not None else None

    ck = ("logo", int(size), id(tki))
    hit = _ICON_CACHE.get(ck)
    # identity check: an id() can be reused once a dead interpreter is
    # collected - never hand back an image from a previous Tk.
    if hit is not None and hit[0] is tki:
        return hit[1]

    try:
        import os
        import sys
        if getattr(sys, "frozen", False):
            search = [getattr(sys, "_MEIPASS", ""),
                      os.path.dirname(os.path.abspath(sys.executable))]
        else:
            search = [os.path.dirname(os.path.abspath(__file__))]
        path = None
        for d in search:
            if not d:
                continue
            p = os.path.join(d, "assets", "images", "logo.png")
            if os.path.exists(p):
                path = p
                break
        if not path:
            return None
        from PIL import Image, ImageTk
        img = Image.open(path).convert("RGBA")
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
        w, h = img.size
        side = max(w, h)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - w) // 2, (side - h) // 2))
        resample = getattr(Image, "Resampling", Image)
        photo = ImageTk.PhotoImage(square.resize((int(size), int(size)),
                                                 resample.LANCZOS),
                                   master=tki)
        _ICON_CACHE[ck] = (tki, photo)
        return photo
    except Exception:
        return None


def set_app_icon(win):
    """Apply the official logo to a window's title bar and taskbar entry.

    One source asset - assets/images/logo.png (never duplicated) - plus the
    derived multi-size app_icon.ico that Windows needs for a crisp
    title bar.  Works both in dev (source) mode and inside the frozen
    .exe: both files are bundled with --add-data (see build_exe.bat) and
    resolved from sys._MEIPASS when frozen."""
    try:
        import os
        import sys
        if getattr(sys, "frozen", False):
            search = [getattr(sys, "_MEIPASS", ""),
                      os.path.dirname(os.path.abspath(sys.executable))]
        else:
            search = [os.path.dirname(os.path.abspath(__file__))]
        ico = png = None
        for d in search:
            if not d:
                continue
            if ico is None:
                p = os.path.join(d, "app_icon.ico")
                if os.path.exists(p):
                    ico = p
            if png is None:
                p = os.path.join(d, "assets", "images", "logo.png")
                if os.path.exists(p):
                    png = p
            if ico and png:
                break
        if png:
            img = tk.PhotoImage(file=png)
            win.iconphoto(True, img)
            win._app_icon_img = img  # keep a reference - Tk does not GC it
        if ico and os.name == "nt":
            win.iconbitmap(ico)
    except Exception:
        pass


def style_app(root):
    """Apply the global CustomTkinter theme plus the native ttk styling
    still needed by the widgets CTk does not provide (tables, dialogs).

    Called from every entry point; idempotent, so it is safe to call
    again per window."""
    configure_theme()                       # CTk appearance (dark) - one place
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    # Native tables are part of the design system too: dark surface, light
    # text, accent selection - so a Treeview never reads as a light island
    # in a dark UI.
    style.configure("Treeview", rowheight=26, font=FONT_BODY,
                    background=CARD, fieldbackground=CARD,
                    foreground=TEXT, borderwidth=0, relief="flat")
    style.configure("Treeview.Heading", font=(FONT_FAMILY, 9, "bold"),
                    background="#243050", foreground=TEXT_LIGHT,
                    relief="flat", padding=(6, 6))
    style.map("Treeview",
              background=[("selected", ACCENT)],
              foreground=[("selected", TEXT_LIGHT)])
    style.map("Treeview.Heading",
              background=[("active", "#2c3a5f")])
    style.configure("TNotebook", background=BG_LIGHT, borderwidth=0)
    style.configure("TNotebook.Tab", font=(FONT_FAMILY, 10, "bold"),
                    padding=(14, 8), background=CARD, foreground=SUBTLE)
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT)],
              foreground=[("selected", TEXT_LIGHT)])
    style.configure("TLabelframe", background=BG_LIGHT, foreground=TEXT,
                    bordercolor=BORDER, relief="solid")
    style.configure("TLabelframe.Label", background=BG_LIGHT,
                    foreground=SUBTLE, font=(FONT_FAMILY, 9, "bold"))
    style.configure("Accent.TButton", font=(FONT_FAMILY, 10, "bold"))
    return style


def export_rows_to_csv(headers, rows, default_name="export.csv", parent=None,
                       notify=None):
    """rows: list of tuples/lists in the same order as headers.

    notify(text, kind) routes the outcome to a small in-app toast when the
    owner provides one (normal events never open a dialog); otherwise the
    classic dialogs are used as a fallback."""

    def _say(text, kind):
        if notify:
            try:
                notify(text, kind)
                return
            except Exception:
                pass
        if kind == "error":
            messagebox.showerror("Export CSV", text, parent=parent)
        else:
            messagebox.showinfo("Export CSV", text, parent=parent)

    if not rows:
        _say("There is no data to export.", "warn")
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
        _say(f"Report exported successfully to:\n{path}", "success")
    except Exception as e:
        _say(f"Failed to export CSV:\n{e}", "error")


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
