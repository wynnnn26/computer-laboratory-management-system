"""
theme.py
Single source of truth for the whole product's look and feel.

Every colour, font, radius, spacing and status tone used by the Server /
Admin console, the Client kiosk and the Student dashboard lives here, so
the UI never hardcodes styling in a page file.  `utils.py` re-exports
these names, which keeps the hundreds of existing `from utils import
ACCENT, ...` references working unchanged.

Design direction: clean, modern, professional and dark-mode focused -
a computer-laboratory / network-management aesthetic with rounded cards,
clear typography and minimal clutter.

Nothing in this module imports from the application (it is imported BY
everything), so it is always safe to import first.
"""

import customtkinter as ctk

# ==========================================================================
# 0. DPI - set before the first CustomTkinter window exists
# ==========================================================================
# CustomTkinter makes the process DPI-aware the instant its first window is
# built and then multiplies every width, height, window geometry and font
# size by the monitor's scale factor (150% here: a request for 1080x700
# came back as a 1620x1050 window, minsize 1620x1050 - larger than the
# display - with 14px fonts).  The Tk application this replaces ran
# DPI-unaware, where a pixel meant a pixel, so every size in this codebase
# still has that meaning.  Re-pinning the factor to 1.0 keeps the migration
# UI-only: same layout, same window sizes, same fit on 1080p/1366x768.
# It has to happen before any CTk window is created - this module is
# imported by utils, which every entry point imports at module level.
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

ScalingTracker.deactivate_automatic_dpi_awareness = True

# ---------------------------------------------------------------------------
# Font sizes: Tk means POINTS, CustomTkinter means PIXELS
# ---------------------------------------------------------------------------
# Tk resolves ("Segoe UI", 9) as 9 points = 12px at 96 DPI.  CustomTkinter's
# _apply_font_scaling() does only `-round(size * widget_scaling)`, i.e. it
# reads 9 as 9 PIXELS, so every caption migrated from the Tk app rendered at
# 75% of its original size (and 9px is smaller than the native widgets next
# to it - Treeview, Listbox and Text never go through this method, so the
# two could disagree on one screen).  Re-implementing that single method
# with the missing 96/72 step restores the original rendering exactly.
# Only the font path is touched: widget widths, heights, window geometry and
# minsize stay literal pixels, and cget("font") still returns the value the
# caller passed in, so layout probes keep measuring what they measured.
from customtkinter.windows.widgets.scaling.scaling_base_class import (
    CTkScalingBaseClass,
)
from customtkinter.windows.widgets.font import CTkFont

_PT_TO_PX = 96.0 / 72.0


def _apply_font_scaling(self, font):
    """Drop-in for CTkScalingBaseClass._apply_font_scaling that applies
    Tk's point-to-pixel conversion.  Widget scaling still drives geometry;
    only fonts gain the extra 96/72 factor."""
    if type(font) == tuple:
        scale = self._get_widget_scaling() * _PT_TO_PX
        if len(font) == 1:
            return font
        elif len(font) == 2:
            return font[0], -abs(round(font[1] * scale))
        elif 3 <= len(font) <= 6:
            return font[0], -abs(round(font[1] * scale)), font[2:]
        else:
            raise ValueError(f"Can not scale font {font}. font needs to be "
                             f"tuple of len 1, 2 or 3")
    elif isinstance(font, CTkFont):
        return font.create_scaled_tuple(self._get_widget_scaling() * _PT_TO_PX)
    else:
        raise ValueError(f"Can not scale font '{font}' of type {type(font)}. "
                         f"font needs to be tuple or instance of CTkFont")


CTkScalingBaseClass._apply_font_scaling = _apply_font_scaling

# ---------------------------------------------------------------------------
# Label heights: native tk.Label sized itself to its font, CTkLabel hardcodes 28
# ---------------------------------------------------------------------------
# tk.Label computes its request from the font it draws - a 9pt caption asked for
# 21px and an 18pt header 38px.  CTkLabel instead defaults to a fixed 28px, so
# every stacked caption in a form row grew by 7px and pushed the three most
# form-heavy pages past the window at the documented 1080x700 minimum:
# System Settings 635 -> 722 (its Save Settings button fell off the bottom and
# rendered 1px tall), Website Access 629 -> 688 and Inventory 689 -> 812.
# Defaulting the height to 0 - CustomTkinter's "size me to my text" - restores
# the Tk behaviour on every label: 9pt -> 20px, 18pt header -> 32px.  Callers
# that pass height explicitly (badge/pill geometry) keep it untouched.
from customtkinter.windows.widgets.ctk_label import CTkLabel as _CTkLabel

if not getattr(_CTkLabel, "_content_height_patched", False):
    _ctk_label_init = _CTkLabel.__init__

    def _content_height_init(self, *args, **kwargs):
        # signature is (master, width=0, height=28, ...) - height is only
        # positional when at least three positional arguments are supplied.
        if "height" not in kwargs and len(args) < 3:
            kwargs["height"] = 0
        _ctk_label_init(self, *args, **kwargs)
        # CustomTkinter 6.0 defaults `text` to the literal string "CTkLabel".
        # Every label built without text= therefore printed that class name
        # into the product UI - behind the transparent header/login/launcher
        # artwork and inside every PC card icon slot (6 sites, all of them
        # image labels).  `text` is only the default when the caller omitted
        # it: positional index 10, after master, width, height, corner_radius,
        # border_width, bg_color, fg_color, border_color, text_color and
        # text_color_disabled.  Dropping the placeholder leaves image-only
        # labels to be sized by their artwork, which is what they wanted.
        if self._text == "CTkLabel" and "text" not in kwargs and len(args) <= 10:
            self.configure(text="")

    _CTkLabel.__init__ = _content_height_init
    _CTkLabel._content_height_patched = True

# ==========================================================================
# 1. Appearance  - call once, as early as possible, before any CTk widget
# ==========================================================================
APPEARANCE = "dark"          # "dark" | "light" | "system"
COLOR_THEME = "dark-blue"    # CTk built-in theme we build our tokens on top of
SCALING = 1.0                # 1.0 == literal pixels (DPI auto-scale is off)

_configured = False


def configure_theme(scaling=None):
    """Apply the global CustomTkinter appearance.  Idempotent and safe to
    call from every entry point (Server, Client, tests, frozen exes)."""
    global _configured
    try:
        ctk.set_appearance_mode(APPEARANCE)
        ctk.set_default_color_theme(COLOR_THEME)
        if scaling is not None:
            ctk.set_widget_scaling(float(scaling))
        _configured = True
    except Exception:
        # A failure here must never stop the application from starting;
        # CTk then falls back to its own defaults.
        pass
    return _configured


def is_configured():
    return _configured


# ==========================================================================
# 2. Surfaces (dark-mode palette)
# ==========================================================================
BG_DARK = "#0b111c"          # header / sidebar / darkest chrome
BG_DEEP = "#070c15"          # window background behind the content
BG_LIGHT = "#0f1420"         # page background  (dark, replaces the old #f4f6fb)
CARD = "#171e2e"             # raised card / panel surface (replaces "white")
CARD_ALT = "#1d263a"         # alternating row / inset surface
SIDEBAR = "#0b111c"          # sidebar (same family as BG_DARK, kept named)
SIDEBAR_HOVER = "#16203a"    # sidebar hover
SIDEBAR_ACTIVE = "#2f6fed"   # sidebar active pill

# ==========================================================================
# 3. Brand / semantic colours
# ==========================================================================
ACCENT = "#2f6fed"           # primary action
ACCENT_DARK = "#1e4fbf"      # primary action, pressed/hover
ACCENT_SOFT = "#8fb2ff"      # primary text/icon on dark
DANGER = "#e5484d"           # destructive
DANGER_DARK = "#c62a2f"
SUCCESS = "#2fa84f"          # success
SUCCESS_BRIGHT = "#7ee787"   # success text on dark chrome
WARN = "#e5a83d"             # warning / syncing
ORANGE = "#dc6803"           # logout / low stock
PURPLE = "#8b5cf6"           # restart
CRIMSON = "#be123c"          # shutdown
SKY = "#0ea5e9"              # live observation
ONLINE = "#16a34a"           # connected / online (>=3:1 as chip text on white)

# ==========================================================================
# 4. Text + borders
# ==========================================================================
TEXT_LIGHT = "#ffffff"       # primary text
TEXT = "#e7ecf7"             # default body text on dark surfaces
SUBTLE = "#9aa5c4"           # secondary text (dark-mode adjusted)
MUTED = "#6f7a99"            # tertiary text
BORDER = "#27324a"           # hairline border on dark cards
BORDER_SOFT = "#1c2438"
OFFLINE = "#6f7a99"          # offline / inactive (same tone as MUTED)
PLACEHOLDER = "#5b667f"      # entry placeholder text

# ==========================================================================
# 5. Typography
# ==========================================================================
FONT_FAMILY = "Segoe UI"

FONT_TITLE = (FONT_FAMILY, 18, "bold")
FONT_SUBTITLE = (FONT_FAMILY, 13, "bold")
FONT_HEADER = (FONT_FAMILY, 12, "bold")
FONT_CARD_NAME = (FONT_FAMILY, 11, "bold")
FONT_UI = (FONT_FAMILY, 10)
FONT_LABEL = (FONT_FAMILY, 10)
FONT_BODY = (FONT_FAMILY, 9)
FONT_SMALL = (FONT_FAMILY, 8)
FONT_STAT_NUM = (FONT_FAMILY, 16, "bold")
FONT_STAT_LABEL = (FONT_FAMILY, 8, "bold")
FONT_MONO = ("Consolas", 9)          # logs, ids, timestamps

FONT_SIZES = {                       # named sizes for components
    "title": 18, "subtitle": 13, "header": 12, "body": 10,
    "small": 9, "tiny": 8, "stat": 16,
}

# ==========================================================================
# 6. Shape + spacing
# ==========================================================================
RADIUS = 8                # default corner radius (cards, buttons)
RADIUS_SM = 6             # small controls
RADIUS_LG = 14            # hero cards / dialogs
BORDER_WIDTH = 1          # default hairline

SPACE_XS, SPACE_S, SPACE_M, SPACE_L = 4, 8, 12, 16
PAD_PAGE = 16             # page padding
PAD_CARD = 14             # inner card padding
GAP = 10                  # gap between sibling cards

STAT_TILE_H = 52          # compact stats-strip tile height
SIDEBAR_W = 220           # sidebar width
HEADER_H = 56             # compact header height
DETAIL_W = 270            # right-side details panel width
MIN_W, MIN_H = 1080, 700  # minimum window size
BTN_H = 30                # standard button height
BTN_H_SM = 26             # compact button height
SEARCH_HINT = "Search PCs..."

# ==========================================================================
# 7. Button recipes  - one place decides how a button is coloured
# ==========================================================================
BUTTON_KINDS = ("primary", "secondary", "danger", "warning", "ghost",
                "success", "restart", "shutdown", "observe", "remote")


def button(kind="primary"):
    """Return the fg/hover/text colour triple for a named button kind.
    Keeps the visual hierarchy consistent everywhere:
        primary   -> normal actions
        warning   -> warning actions
        danger    -> destructive actions"""
    return {
        "primary":   {"fg_color": ACCENT,      "hover_color": ACCENT_DARK,
                      "text_color": TEXT_LIGHT},
        "secondary": {"fg_color": "#243050",    "hover_color": "#2c3a5f",
                      "text_color": TEXT_LIGHT},
        "danger":    {"fg_color": DANGER,       "hover_color": DANGER_DARK,
                      "text_color": TEXT_LIGHT},
        "warning":   {"fg_color": WARN,         "hover_color": "#c9902c",
                      "text_color": "#1a1405"},
        "success":   {"fg_color": SUCCESS,      "hover_color": "#268a43",
                      "text_color": TEXT_LIGHT},
        "restart":   {"fg_color": PURPLE,       "hover_color": "#7c4ddb",
                      "text_color": TEXT_LIGHT},
        "shutdown":  {"fg_color": CRIMSON,      "hover_color": "#9f0e2f",
                      "text_color": TEXT_LIGHT},
        "observe":   {"fg_color": SKY,          "hover_color": "#0b8ac2",
                      "text_color": TEXT_LIGHT},
        "remote":    {"fg_color": "#a33b00",    "hover_color": "#823000",
                      "text_color": TEXT_LIGHT},
        "ghost":     {"fg_color": "transparent", "hover_color": SIDEBAR_HOVER,
                      "text_color": ACCENT_SOFT},
    }.get(kind, {})


# ==========================================================================
# 8. PC status system  (SERVER remains the single authoritative source)
# ==========================================================================
STATUS_LABELS = {
    "online": "ONLINE",
    "offline": "OFFLINE",
    "in_use": "IN USE",
    "paused": "PAUSED",
    "locked": "LOCKED",
    "available": "AVAILABLE",
    "verifying": "VERIFYING",
    "unknown": "UNKNOWN",
}
STATUS_COLORS = {
    "online": ONLINE,            # green
    "offline": "#e42313",        # red
    "in_use": "#1e7ef0",         # blue
    "paused": "#b45309",         # yellow / orange (darkened for white chips)
    "locked": "#8b33d9",         # purple
    "available": "#6b7280",      # gray
    "verifying": "#0e7490",      # cyan
    "unknown": "#3a3a3a",        # dark gray (fallback)
}
STATUS_ORDER = ["online", "offline", "in_use", "paused", "locked",
                "available", "verifying", "unknown"]

# Server / connection indicators used by the header and Client dashboard
SERVER_UP = SUCCESS_BRIGHT
SERVER_DOWN = WARN
TOAST_COLORS = {"info": ACCENT, "success": SUCCESS, "error": DANGER,
                "warn": WARN}


def status_key(state) -> str:
    """Normalize any state/label spelling ("IN USE", "in-use", ...) to the
    canonical key used by STATUS_* and pc_icons/<key>.png."""
    if state is None:
        return "unknown"
    k = str(state).strip().lower().replace(" ", "_").replace("-", "_")
    return k if k in STATUS_LABELS else "unknown"
