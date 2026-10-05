"""Pixel-level readability check for every window type the app shows:
server login, admin dashboard (all pages), admin dialogs, student dashboard.

For each heading, crop its box out of a real PrintWindow capture and compare
the crop's p99 vs p1 luminance.  Invisible text yields ~1.0.

    python contrast_all.py
"""
import ctypes
import os
import sys
import tempfile
import tkinter as tk
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
LAB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LAB)
os.chdir(tempfile.mkdtemp(prefix="contrastall_"))

import theme            # noqa: E402
import customtkinter as ctk  # noqa: E402
from database import init_db  # noqa: E402

init_db()
import main             # noqa: E402
from admin_dashboard import AdminDashboard        # noqa: E402
from student_dashboard import StudentDashboard    # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def capture(hwnd):
    # PrintWindow only: the desktop here has foreign windows docked over
    # the app (a "Links" toolbar, static controls at widget coordinates),
    # so a screen grab measures somebody else's pixels.  WM_PRINT reads
    # the window's own backing - immune to what covers it.
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None, r
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, hbmp)
    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT) or \
        user32.PrintWindow(hwnd, mdc, 0)
    gdi32.SelectObject(mdc, old)
    if not ok:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mdc)
        return None, r
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mdc, hbmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    if not got:
        return None, r
    from PIL import Image
    return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA",
                            0, 1).convert("RGB"), r


def hwnd_for(win):
    wid = int(win.winfo_id())
    parent = user32.GetParent(wid)
    return parent or wid


def rel_lum(c):
    def f(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def font_size(f):
    import re
    if isinstance(f, (list, tuple)) and len(f) >= 2:
        try:
            return int(f[1])
        except Exception:
            return 0
    if isinstance(f, str):
        nums = [int(x) for x in re.findall(r"\d+", f)]
        if nums:
            return max(nums) if max(nums) <= 40 else nums[0]
    return 0


def headings(w, out, root=None):
    for c in w.winfo_children():
        try:
            if not c.winfo_ismapped():
                continue
        except Exception:
            continue
        try:
            t = c.cget("text")
            f = c.cget("font")
        except Exception:
            t, f = None, None
        if c.winfo_class() == "Entry":
            continue
        if isinstance(t, str) and t.startswith("PY_VAR"):
            t = None
        if isinstance(t, str) and t.strip() and font_size(f) >= 9 and \
                len(t.strip()) >= 3:
            try:
                x, y, bw, bh = (c.winfo_rootx(), c.winfo_rooty(),
                                c.winfo_width(), c.winfo_height())
                if bw > 8 and bh > 8:
                    out.append((t.strip(), x, y, bw, bh, font_size(f), c))
            except Exception:
                pass
        headings(c, out, root)


def _is_ancestor(a, b):
    """widget a is b or an ancestor of b"""
    w = b
    while w is not None:
        if w is a:
            return True
        w = getattr(w, "master", None)
    return False


def dedupe(items):
    """A CTkButton reports its text AND its inner label does too.  Score the
    parent's whole-width bbox and the text share falls under the 1% percentile
    (a 1009px row with 77px of glyphs scores 2.30 while the glyphs themselves
    are a perfectly readable 7.7) - keep only the deepest widget per text."""
    keep = []
    for i, it in enumerate(items):
        dup = False
        for j, ot in enumerate(items):
            if i != j and ot[0] == it[0] and _is_ancestor(it[6], ot[6]):
                dup = True      # it is an ancestor of a same-text entry
                break
        if not dup:
            keep.append(it)
    return keep


TOTAL_BAD = 0       # gate fails when this is non-zero


def score_items(px, wrect, iw, ih, items):
    out = {}
    for text, x, y, bw, bh, size, _w in items:
        cx0, cy0 = x - wrect.left, y - wrect.top
        if cx0 < 0 or cy0 < 0 or cx0 + bw > iw or cy0 + bh > ih:
            continue
        lums = [rel_lum(px[xx, yy])
                for yy in range(cy0, min(cy0 + bh, ih))
                for xx in range(cx0, min(cx0 + bw, iw))]
        if len(lums) < 20:
            continue
        lums.sort()
        lo = lums[max(int(0.01 * len(lums)), 0)]
        hi = lums[min(int(0.99 * len(lums)), len(lums)) - 1]
        out[(text, size)] = ((hi + 0.05) / (lo + 0.05),
                             size, text[:52], bw, bh)
    return out


def measure(tag, win, tree=None):
    """capture `win` and score every heading under `tree` (default: win)

    A fresh CTkToplevel can capture black (fade-in/after() not settled) or a
    half-painted child frame - capture up to 3 times and keep each heading's
    BEST score: a genuine contrast failure fails every capture, an artifact
    only fails the ones it raced."""
    global TOTAL_BAD
    for _ in range(20):
        try:
            win.update_idletasks()
            win.update()
        except Exception:
            pass
    hwnd = hwnd_for(win)
    items = []
    headings(tree if tree is not None else win, items)
    items = dedupe(items)
    import time as _time
    best = {}
    img = wrect = None
    for attempt in range(3):
        img, wrect = capture(hwnd)
        if img is None:
            print("[%s] CAPTURE FAILED" % tag)
            TOTAL_BAD += 1
            return
        px = img.load()
        iw, ih = img.size
        for k, v in score_items(px, wrect, iw, ih, items).items():
            if k not in best or v[0] > best[k][0]:
                best[k] = v
        if all(v[0] >= 3.0 for v in best.values()) or attempt == 2:
            break
        _time.sleep(0.35)                       # let animations paint
        for _ in range(20):
            try:
                win.update_idletasks()
                win.update()
            except Exception:
                pass
    rows = sorted(best.values())
    bad = [r for r in rows if r[0] < 3.0]
    TOTAL_BAD += len(bad)
    print("\n[%s] %d headings | below 3.0: %d" % (tag, len(rows), len(bad)))
    for r, size, t, bw, bh in rows[:8]:
        flag = "  <== UNREADABLE" if r < 3.0 else ("  (low)" if r < 4.5 else "")
        print("   %6.2f size=%-3d %dx%d %r%s" % (r, size, bw, bh, t, flag))
    if bad:
        # dump the failing crops so a bad score can be LOOKED at, not guessed
        try:
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bad")
            os.makedirs(d, exist_ok=True)
            for i, (r, size, t, bw, bh) in enumerate(bad):
                for text, x, y, bw2, bh2, size2, _w in items:
                    if text == t and size == size2:
                        cx0, cy0 = x - wrect.left, y - wrect.top
                        if 0 <= cx0 and 0 <= cy0 and \
                                cx0 + bw2 <= iw and cy0 + bh2 <= ih:
                            img.crop((cx0, cy0, cx0 + bw2,
                                      cy0 + bh2)).save(
                                os.path.join(
                                    d, "%s_%d.png" % (tag.replace(":", "_"), i)))
                        break
        except Exception as e:
            print("crop dump failed:", e)


ROW = {"id": 1, "student_id": "admin", "full_name": "Test Admin",
       "role": "admin"}
SROW = {"id": 1, "student_id": "2023-00001", "full_name": "Juan Dela Cruz",
        "role": "student", "course": "BS Computer Science",
        "year_level": "3rd Year", "email": "juan@example.com",
        "contact": "09171234567", "status": "Active"}

# ---------------------------------------------------------------- server login
lw = main.LoginWindow(server=None)
measure("server login", lw)

# ------------------------------------------------------------- admin dashboard
dash = AdminDashboard(lw, ROW, lambda: None, server=None, server_events=None)
for p in list(dash.pages.keys()):
    dash.show_page(p)
    measure("admin:" + p, dash)

# dialogs ------------------------------------------------------------------
def find_owner(page_key, meth):
    try:
        fr = dash.pages[page_key]
    except Exception:
        return None
    hits = []

    def w(x):
        for c in x.winfo_children():
            if callable(getattr(c, meth, None)):
                hits.append(c)
            w(c)
    w(fr)
    return hits[0] if hits else None


def toplevels():
    out = []

    def w(x):
        for c in x.winfo_children():
            if type(c).__name__ in ("CTkToplevel", "Toplevel"):
                out.append(c)
            w(c)
    w(dash)
    return out


for key, meth, noarg in (("students", "_open_form", False),
                         ("staff", "open_add", True),
                         ("websites", "_open_dialog", True),
                         ("inventory", "add_record", True)):
    owner = find_owner(key, meth)
    if owner is None:
        print("dialog: no owner for %s.%s" % (key, meth))
        continue
    before = set(id(w2) for w2 in toplevels())
    try:
        res = meth() if False else (owner.__getattribute__(meth)()
                                    if noarg else owner._open_form({}))
    except Exception as e:
        print("dialog %s.%s -> %s: %s" % (key, meth, type(e).__name__,
                                          str(e)[:70]))
        continue
    new = [w2 for w2 in toplevels() if id(w2) not in before]
    target = None
    if res is not None and hasattr(res, "winfo_children") and \
            type(res).__name__ in ("CTkToplevel", "Toplevel"):
        target = res
    elif new:
        target = new[0]
    if target is not None:
        measure("dialog:%s.%s" % (key, meth), target)
        try:
            target.destroy()
        except Exception:
            pass
    else:
        print("dialog %s.%s: no toplevel (inline?)" % (key, meth))

dash.destroy()

# ------------------------------------------------------------ student dashboard
stu = StudentDashboard(lw, SROW, lambda: None)
notebook = None
for c in stu.winfo_children():
    if type(c).__name__ in ("TabHost", "CTkTabview"):
        notebook = c
        break
TAB_NAMES = ["Announcements", "Messages", "Borrow Equipment"]
for t in TAB_NAMES:
    if notebook is not None:
        try:
            notebook.set(t)
        except Exception as e:
            print("student tab %r -> %s" % (t, e))
            continue
    for _ in range(14):
        stu.update_idletasks()
        stu.update()
    measure("student:" + t, stu)
stu.destroy()

lw.destroy()
print("\nDONE - %d unreadable heading(s)" % TOTAL_BAD)
sys.exit(1 if TOTAL_BAD else 0)
