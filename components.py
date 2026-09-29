"""
components.py
Reusable CustomTkinter building blocks for the whole product.

These give every page the same rhythm - identical corner radius, spacing,
typography and colour hierarchy - so page files describe WHAT is on screen
rather than restyling it every time.  Nothing here talks to the network,
the database or the application; they are pure presentation.

Used by: the Server/Admin console, the Client kiosk and the Student
dashboard.
"""

import contextlib
import tkinter as tk

import customtkinter as ctk

from theme import (
    ACCENT, ACCENT_SOFT, BG_LIGHT, BORDER, BORDER_SOFT, CARD, CARD_ALT,
    DANGER, FONT_BODY, FONT_CARD_NAME, FONT_HEADER, FONT_LABEL, FONT_SMALL,
    FONT_STAT_LABEL, FONT_STAT_NUM, FONT_SUBTITLE, FONT_TITLE, GAP, MUTED,
    PAD_CARD, RADIUS, RADIUS_SM, SPACE_S, SPACE_XS, SUBTLE, TEXT, TEXT_LIGHT,
    TOAST_COLORS, WARN, button,
)


# ==========================================================================
# Surfaces
# ==========================================================================
class Card(ctk.CTkFrame):
    """A raised, rounded surface that other widgets are placed into.

    The workhorse behind 'card' styling: pass children through `children`
    or pack/grid them into the instance afterwards."""

    def __init__(self, master, children=(), pad=PAD_CARD, **kw):
        kw.setdefault("fg_color", CARD)
        kw.setdefault("corner_radius", RADIUS)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", BORDER)
        super().__init__(master, **kw)
        self._pad = pad
        for child in children:
            child.pack(in_=self, fill="x", padx=pad, pady=(pad, 0))

    def add(self, widget, fill="x", pady=(0, 0), padx=None, side=None, **kw):
        """Pack a child with the card's standard padding."""
        opts = dict(in_=self, fill=fill, pady=pady, **kw)
        if padx is None:
            padx = self._pad
        if padx:
            opts["padx"] = padx
        if side:
            opts["side"] = side
        widget.pack(**opts)
        return widget


class PaddedFrame(ctk.CTkFrame):
    """Container that preserves ``tk.Frame(padx=, pady=)`` / the ``padding``
    option of ``ttk.Frame``.

    CustomTkinter's frame has no padding option, so the padding is applied
    to each child's own geometry the first time the frame is mapped.  The
    result is identical to the native option - children are inset by the
    same amount whether they were gridded or packed - so existing forms,
    dialogs and tabs keep their spacing after the migration."""

    def __init__(self, master, padx=0, pady=0, padding=None, **kw):
        if padding is not None:
            if isinstance(padding, (tuple, list)):
                if len(padding) == 4:          # (left, top, right, bottom)
                    padx = (padding[0] + padding[2]) / 2
                    pady = (padding[1] + padding[3]) / 2
                elif len(padding) == 2:        # (horizontal, vertical)
                    padx, pady = padding[0], padding[1]
                elif padding:
                    padx = pady = padding[0]
            else:
                padx = pady = padding
        super().__init__(master, **kw)
        self._pad_x = padx or 0
        self._pad_y = pady or 0
        self._title_height = 0       # set by CardFrame when it has a title
        self._title_lbl = None
        # child -> (padx, pady) exactly as the caller authored them, so the
        # geometry can be rewritten from the original on every pass instead
        # of accumulating on top of our own previous edits.
        self._orig = {}
        if self._pad_x or self._pad_y:
            self.bind("<Map>", self._apply_padding, add="+")
            self.bind("<Configure>", self._apply_padding, add="+")

    @staticmethod
    def _num(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _apply_padding(self, _event=None):
        """Inset this frame's content region by its padding.

        ``tk.Frame(padx=, pady=)`` shrinks the *region* the geometry manager
        lays children out in; it never touches the children's own padding.
        Adding it to every child instead - what this did originally - also
        added it *between* children: a 12px form padding turned each 4px grid
        gap into 28px, so the eight-row Add Account form needed 630px inside
        a 440px window and landed its Save/Cancel row 190px below the bottom
        edge, unreachable.

        Padding now goes only to the children that sit on the frame's outer
        edges - the first/last grid row and column, the first and last child
        of each pack chain - which is exactly what the native option does.
        Each child's authored padx/pady is captured the first time it is seen
        and every pass rewrites the geometry from that original, so the result
        is idempotent: late-added children are re-edged correctly instead of
        accumulating, and unchanged widgets are not re-configured at all (no
        <Configure> feedback loop)."""
        if not (self._pad_x or self._pad_y or self._title_height):
            return
        try:
            kids = [ch for ch in self.winfo_children()
                    if ch.winfo_exists()
                    and ch.winfo_manager() in ("grid", "pack")
                    and ch is not getattr(self, "_title_lbl", None)]
        except Exception:
            return
        if not kids:
            return

        # ---- grid: only the outer rows and columns carry the padding -----
        grid_kids = [ch for ch in kids if ch.winfo_manager() == "grid"]
        if grid_kids:
            spans = {}
            for ch in grid_kids:
                try:
                    info = ch.grid_info()
                    r = int(info.get("row") or 0)
                    c = int(info.get("column") or 0)
                    rs = max(1, int(info.get("rowspan") or 1))
                    cs = max(1, int(info.get("columnspan") or 1))
                except Exception:
                    r = c = 0
                    rs = cs = 1
                spans[ch] = (r, r + rs - 1, c, c + cs - 1)
            min_r = min(v[0] for v in spans.values())
            max_r = max(v[1] for v in spans.values())
            min_c = min(v[2] for v in spans.values())
            max_c = max(v[3] for v in spans.values())
            for ch in grid_kids:
                try:
                    info = ch.grid_info()
                    r0, r1, c0, c1 = spans[ch]
                    orig = self._orig.get(ch)
                    if orig is None:
                        orig = (self._split(info.get("padx")),
                                self._split(info.get("pady")))
                        self._orig[ch] = orig
                    (ol, orr), (ot, ob) = orig
                    first = (r0 == min_r)
                    want_x = self._pair(
                        ol + (self._pad_x if c0 == min_c else 0),
                        orr + (self._pad_x if c1 == max_c else 0))
                    want_y = self._pair(
                        ot + ((self._pad_y + self._title_height)
                              if first else 0),
                        ob + (self._pad_y if r1 == max_r else 0))
                    if (self._pair(*self._split(info.get("padx"))) != want_x
                            or self._pair(*self._split(info.get("pady")))
                            != want_y):
                        ch.grid_configure(padx=want_x, pady=want_y)
                except Exception:
                    continue

        # ---- pack: each side chain is inset at its own two edges ---------
        pack_kids = [ch for ch in kids if ch.winfo_manager() == "pack"]
        if pack_kids:
            groups = {}
            for ch in pack_kids:
                try:
                    side = ch.pack_info().get("side") or "top"
                except Exception:
                    side = "top"
                groups.setdefault(side, []).append(ch)
            first_edge = {"top": "tp", "bottom": "bt",
                          "left": "lt", "right": "rt"}
            last_edge = {"top": "bt", "bottom": "tp",
                         "left": "rt", "right": "lt"}
            perp = {"top": ("lt", "rt"), "bottom": ("lt", "rt"),
                    "left": ("tp", "bt"), "right": ("tp", "bt")}
            title_child = pack_kids[0]     # CardFrame: one title inset, top
            for side, chs in groups.items():
                fe = first_edge.get(side, "tp")
                le = last_edge.get(side, "bt")
                pe = perp.get(side, ("lt", "rt"))
                for idx, ch in enumerate(chs):
                    try:
                        info = ch.pack_info()
                        orig = self._orig.get(ch)
                        if orig is None:
                            orig = (self._split(info.get("padx")),
                                    self._split(info.get("pady")))
                            self._orig[ch] = orig
                        (ol, orr), (ot, ob) = orig
                        lt, rt, tp, bt = ol, orr, ot, ob
                        if "lt" in pe:
                            lt += self._pad_x
                        if "rt" in pe:
                            rt += self._pad_x
                        if "tp" in pe:
                            tp += self._pad_y
                        if "bt" in pe:
                            bt += self._pad_y
                        for edge, at_edge in ((fe, idx == 0),
                                              (le, idx == len(chs) - 1)):
                            if not at_edge:
                                continue
                            if edge == "lt":
                                lt += self._pad_x
                            elif edge == "rt":
                                rt += self._pad_x
                            elif edge == "tp":
                                tp += self._pad_y
                            elif edge == "bt":
                                bt += self._pad_y
                        if ch is title_child:
                            tp += self._title_height
                        want_x = self._pair(lt, rt)
                        want_y = self._pair(tp, bt)
                        if (self._pair(*self._split(info.get("padx")))
                                != want_x
                                or self._pair(*self._split(info.get("pady")))
                                != want_y):
                            ch.pack_configure(padx=want_x, pady=want_y)
                    except Exception:
                        continue

    @staticmethod
    def _split(value):
        """padx/pady may be a scalar or a (start, end) tuple."""
        if isinstance(value, (tuple, list)) and value:
            if len(value) >= 2:
                return PaddedFrame._num(value[0]), PaddedFrame._num(value[1])
            return PaddedFrame._num(value[0]), PaddedFrame._num(value[0])
        return PaddedFrame._num(value), PaddedFrame._num(value)

    @staticmethod
    def _pair(first, second):
        def r(v):
            v = float(v)
            return int(v) if v.is_integer() else v
        return (r(first), r(second))


class CardFrame(PaddedFrame):
    """Labelled rounded container - the CustomTkinter stand-in for
    `ttk.LabelFrame`.

    Children pack/grid into the instance itself exactly as they did into a
    LabelFrame, so `ttk.LabelFrame(parent, text="PC INFORMATION")` becomes
    `CardFrame(parent, text="PC INFORMATION")` with no other change."""

    def __init__(self, master, text="", **kw):
        kw.setdefault("fg_color", CARD)
        kw.setdefault("corner_radius", RADIUS)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", BORDER)
        super().__init__(master, **kw)
        if text:
            # The title is PLACED, not packed: a packed slave would make
            # the frame unusable for grid()-managed children, and these
            # panels mix both across their call sites.  place() is
            # geometry-manager independent, so grid and pack children
            # keep working exactly as they did in ttk.LabelFrame.
            lbl = ctk.CTkLabel(self, text=text, font=FONT_HEADER,
                               text_color=TEXT, anchor="w")
            lbl.place(x=PAD_CARD, y=PAD_CARD - 6, anchor="nw")
            self._title_lbl = lbl
            try:
                self.update_idletasks()
                self._title_height = (lbl.winfo_reqheight()
                                      + (PAD_CARD - 6) + SPACE_S)
            except Exception:
                self._title_height = 30
            self.bind("<Map>", self._apply_padding, add="+")
            self.bind("<Configure>", self._apply_padding, add="+")
            self.after_idle(self._apply_padding)
            self._title = text


class TabHost(ctk.CTkTabview):
    """Tabbed container that keeps the small `ttk.Notebook` read API.

    `ttk.Notebook.tabs()` returned widget ids and `tab(id, "text")` the
    label; CustomTkinter's tabview has neither, so they are re-expressed
    in terms of the tab *name* (which is the label).  Callers that only
    ask for the tab labels therefore keep working unchanged.
    """

    def tabs(self):
        return tuple(getattr(self, "_name_list", ()) or ())

    def tab(self, name, option=None, **_kw):
        if option is None:
            return super().tab(name)
        if option in ("text", "textvariable"):
            return name
        raise ValueError(f"unsupported ttk.Notebook tab option: {option!r}")

    def set(self, name):
        """Select a tab by name, without CustomTkinter's delayed hide.

        `CTkTabview.set` hides the other tabs with a *delayed*
        `after(100, ...)` closure.  A second `set()` inside that window
        (programmatic switches, scripted UI passes, or a click right
        after a `set`) gets the tab it just selected un-gridded by the
        first call's stale timer: the tab label stays highlighted while
        the content area renders empty.  Grid the selection and forget
        the rest in the same event instead - same visible behaviour,
        no timer to race with.
        """
        if name not in getattr(self, "_tab_dict", {}):
            raise ValueError(f"CTkTabview has no tab named '{name}'")
        self._current_name = name
        self._segmented_button.set(name)
        self._set_grid_current_tab()
        self._grid_forget_all_tabs(exclude_name=name)


class SectionLabel(ctk.CTkLabel):
    """Small uppercase section heading (sidebar groups, form groups)."""

    def __init__(self, master, text, **kw):
        kw.setdefault("font", FONT_SMALL)
        kw.setdefault("text_color", MUTED)
        kw.setdefault("anchor", "w")
        super().__init__(master, text=text.upper(), **kw)


class Divider(ctk.CTkFrame):
    """A one-pixel hairline separator."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", BORDER_SOFT)
        kw.setdefault("height", 1)
        kw.setdefault("corner_radius", 0)
        super().__init__(master, **kw)


def eye_icon(slashed=False, color=SUBTLE, gap=CARD, size=24):
    """Password-visibility eye glyph as a smooth RGBA image (no font deps).

    Open eye = "click to reveal", slashed eye = "click to re-mask".  The
    slash is painted twice - first a wider line in the card colour
    (`gap`), then the ink line - so it stays legible where it crosses
    the eye.  Drawn 4x and downsampled with LANCZOS for clean edges at
    any DPI; pass the result to `ctk.CTkImage(light_image=..., size=...)`.
    """
    from PIL import Image, ImageDraw

    def _rgba(c):
        c = str(c).lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (255,)

    s = 4                                   # supersample factor
    k = size * s / 24.0                     # 24-px design box -> any size
    ink, back = _rgba(color), _rgba(gap)
    img = Image.new("RGBA", (int(size * s), int(size * s)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = max(2, int(round(1.5 * k)))
    d.ellipse((2 * k, 6 * k, 22 * k, 18 * k), outline=ink, width=stroke)
    d.ellipse((9 * k, 9 * k, 15 * k, 15 * k), fill=ink)
    if slashed:
        seg = (4 * k, 20 * k, 20 * k, 4 * k)
        d.line(seg, fill=back, width=int(round(stroke + 2 * k)))
        d.line(seg, fill=ink, width=stroke)
    return img.resize((size, size), Image.LANCZOS)


def image_master(root):
    """Context manager binding lazily-created Tk images to `root`.

    ``ImageTk.PhotoImage`` without an explicit master lands on
    ``tkinter._default_root``, which stays on the FIRST Tk window until
    that window is destroyed.  A second window (a test harness keeping
    the login page alive, or any future multi-root flow) would therefore
    cache its CTkImage bitmaps on the wrong interpreter and fail with
    "image ... doesn't exist".  Swapping the default root for the
    duration of the widget build keeps the images on the window that
    will actually display them."""
    @contextlib.contextmanager
    def _bind():
        prev = tk._default_root
        tk._default_root = root
        try:
            yield
        finally:
            tk._default_root = prev
    return _bind()


# ==========================================================================
# Controls
# ==========================================================================
def ActionButton(master, text, command=None, kind="primary", small=False,
                 **kw):
    """Standard button with the design system's colour hierarchy.

    kind: primary | secondary | danger | warning | success | restart |
          shutdown | observe | remote | ghost
    Normal actions use `primary`/`secondary`, warning actions `warning`,
    destructive actions `danger` - one place decides, so the hierarchy
    cannot drift between pages."""
    style = button(kw.pop("kind", kind))
    kw.setdefault("fg_color", style.get("fg_color", ACCENT))
    kw.setdefault("hover_color", style.get("hover_color"))
    kw.setdefault("text_color", style.get("text_color", TEXT_LIGHT))
    kw.setdefault("corner_radius", RADIUS_SM)
    kw.setdefault("height", 26 if small else 30)
    kw.setdefault("font", FONT_LABEL)
    kw.setdefault("cursor", "hand2")
    kw.setdefault("border_width", 0)
    return ctk.CTkButton(master, text=text, command=command, **kw)


def SearchBox(master, hint="Search...", textvariable=None, command=None,
              width=220, **kw):
    """Compact search field with placeholder text.

    `command` is invoked (debounced by the caller if needed) on every
    keystroke via the virtual ``<<Modified>>``-style binding the owner
    sets up; here we only expose a consistent look and a clear button."""
    kw.setdefault("width", width)
    kw.setdefault("height", 30)
    kw.setdefault("corner_radius", RADIUS_SM)
    kw.setdefault("placeholder_text", hint)
    kw.setdefault("font", FONT_BODY)
    e = ctk.CTkEntry(master, textvariable=textvariable, **kw)
    if command is not None:
        e.bind("<KeyRelease>", lambda _e: command(), add="+")
    return e


def OptionField(master, values=(), width=140, variable=None, **kw):
    """Dropdown using the design system's sizing."""
    kw.setdefault("width", width)
    kw.setdefault("height", 30)
    kw.setdefault("corner_radius", RADIUS_SM)
    kw.setdefault("font", FONT_BODY)
    kw.setdefault("state", "readonly")
    return ctk.CTkOptionMenu(master, values=list(values), variable=variable,
                             **kw)


class Toolbar(ctk.CTkFrame):
    """A single horizontal strip for search / filters / actions.

    Keeps every page's controls on one baseline with consistent gaps."""

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("corner_radius", 0)
        super().__init__(master, **kw)

    def add(self, widget, padx=(0, SPACE_S), side="left", **kw):
        widget.pack(side=side, padx=padx, **kw)
        return widget

    def spacer(self):
        """Push subsequent children to the right edge."""
        f = ctk.CTkFrame(self, fg_color="transparent", width=1)
        f.pack(side="left", fill="x", expand=True)
        return f


# ==========================================================================
# Data display
# ==========================================================================
class StatTile(ctk.CTkFrame):
    """Compact dashboard statistic: icon, number, label, status dot.

    Deliberately small (never a hero-sized card) so seven of them fit the
    first screen without scrolling."""

    def __init__(self, master, label, value="0", icon="", color=ACCENT,
                 hint="", **kw):
        kw.setdefault("fg_color", CARD)
        kw.setdefault("corner_radius", RADIUS)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", BORDER)
        super().__init__(master, height=64, **kw)
        self.pack_propagate(False)
        self.color = color
        self._value = str(value)

        left = ctk.CTkFrame(self, fg_color="transparent", width=26)
        left.pack(side="left", padx=(10, 0), pady=8)
        left.pack_propagate(False)
        self.icon_lbl = ctk.CTkLabel(left, text=icon or "•",
                                     font=("Segoe UI", 15),
                                     text_color=color)
        self.icon_lbl.pack(expand=True)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(8, 8), pady=6)
        self.num_lbl = ctk.CTkLabel(body, text=self._value,
                                    font=FONT_STAT_NUM, text_color=TEXT_LIGHT,
                                    anchor="w")
        self.num_lbl.pack(anchor="w")
        self.text_lbl = ctk.CTkLabel(body, text=label.upper(),
                                     font=FONT_STAT_LABEL, text_color=MUTED,
                                     anchor="w")
        self.text_lbl.pack(anchor="w")

        # status indicator dot - the tile's live signal
        self.dot = ctk.CTkLabel(self, text="●", font=("Segoe UI", 11),
                                text_color=color, width=16)
        self.dot.pack(side="right", padx=(0, 10))

        if hint:
            for w in (self, self.num_lbl, self.text_lbl):
                w.bind("<Enter>", lambda _e, h=hint: self._hint(h), add="+")
        self._tip_after = None

    def set(self, value, color=None):
        """Update the number (and optionally the status colour)."""
        self._value = str(value)
        try:
            self.num_lbl.configure(text=self._value)
            if color:
                self.color = color
                self.dot.configure(text_color=color)
        except Exception:
            pass

    def _hint(self, text):
        try:
            self.tooltip = ctk.CTkToplevel(self)
            self.tooltip.withdraw()
            self.tooltip.overrideredirect(True)
            ctk.CTkLabel(self.tooltip, text=text, font=FONT_SMALL,
                         fg_color=CARD_ALT, text_color=TEXT,
                         corner_radius=6, padx=8, pady=4).pack()
            x = self.winfo_rootx() + 8
            y = self.winfo_rooty() + self.winfo_height() + 4
            self.tooltip.geometry(f"+{x}+{y}")
            self.tooltip.deiconify()
            self.tooltip.after(2600, self._kill_hint)
        except Exception:
            pass

    def _kill_hint(self):
        try:
            if getattr(self, "tooltip", None) is not None:
                self.tooltip.destroy()
                self.tooltip = None
        except Exception:
            pass


class KeyValue(ctk.CTkFrame):
    """A label/value row for the details panels (readable, evenly spaced)."""

    def __init__(self, master, label, value="", **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("corner_radius", 0)
        super().__init__(master, **kw)
        self.label_lbl = ctk.CTkLabel(self, text=label, font=FONT_SMALL,
                                      text_color=MUTED, anchor="w",
                                      width=kw.pop("label_width", 0))
        self.label_lbl.pack(anchor="w")
        self.value_lbl = ctk.CTkLabel(self, text=str(value or "—"),
                                      font=FONT_LABEL, text_color=TEXT,
                                      anchor="w", justify="left")
        self.value_lbl.pack(anchor="w", pady=(1, 0))

    def set(self, value):
        try:
            self.value_lbl.configure(text=str(value if value else "—"))
        except Exception:
            pass


class PageHeader(ctk.CTkFrame):
    """Page title + optional description/actions row."""

    def __init__(self, master, title, subtitle="", actions=(), **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("corner_radius", 0)
        super().__init__(master, **kw)
        ctk.CTkLabel(self, text=title, font=FONT_SUBTITLE,
                     text_color=TEXT_LIGHT, anchor="w").pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=FONT_BODY,
                         text_color=SUBTLE, anchor="w").pack(anchor="w",
                                                             pady=(2, 0))
        for a in actions:
            a.pack(side="right", padx=(6, 0))


class StatusPill(ctk.CTkLabel):
    """Small rounded status pill (ONLINE / SYNCED / FAILED ...)."""

    def __init__(self, master, text, color=ACCENT, **kw):
        kw.setdefault("font", FONT_SMALL)
        super().__init__(master, text=f" {text} ", fg_color=color,
                         text_color=TEXT_LIGHT, corner_radius=9,
                         padx=6, pady=1, **kw)
        self.color = color

    def set(self, text, color=None):
        try:
            self.configure(text=f" {text} ")
            if color:
                self.configure(fg_color=color)
                self.color = color
        except Exception:
            pass


class Toast(ctk.CTkToplevel):
    """Compact snackbar for normal notifications (success / failure /
    connection / sync).

    Deliberately small and auto-dismissing: simple notifications must never
    become large popup windows.  Critical confirmations still use a real
    dialog."""

    def __init__(self, master, text, kind="info", duration=3200, y=24):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        color = TOAST_COLORS.get(kind, TOAST_COLORS["info"])
        ctk.CTkFrame(self, fg_color=CARD_ALT, corner_radius=10,
                     border_width=1, border_color=color).pack(
            fill="both", expand=True, padx=2, pady=2)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(row, text="●", text_color=color,
                     font=("Segoe UI", 11)).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(row, text=text, font=FONT_BODY, text_color=TEXT,
                     anchor="w", justify="left").pack(
            side="left", fill="x", expand=True)
        try:
            self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            yy = master.winfo_rooty() + y
            self.geometry(f"+{max(x, 8)}+{max(yy, 8)}")
        except Exception:
            pass
        self._after = self.after(duration, self._kill)

    def _kill(self):
        try:
            self.destroy()
        except Exception:
            pass
