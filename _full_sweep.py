"""Whole-UI layout sweep.

The regression gate only probes the overview and client pages, so this walks
every page of the Admin console at three common resolutions and reports:
  * captions wider than the widget they are drawn in (clipped text)
  * page content taller than the space it has (clipped rows)
  * direct children hanging outside the window
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import tkinter as tk
from tkinter import font as tkfont
import theme
import customtkinter as ctk
import database
database.init_db()
from admin_dashboard import AdminDashboard

root = tk.Tk()
root.withdraw()
conn = database.get_connection()
admin = dict(conn.execute("SELECT * FROM users WHERE role='admin'").fetchone())
conn.close()
dash = AdminDashboard(root, admin, lambda: None)

problems = []


def label_px(w):
    t = str(w.cget("text") or "")
    if not t:
        return 0
    try:
        f = w.cget("font") or ("Segoe UI", 10)
        return tkfont.Font(font=f).measure(t)
    except Exception:
        return w.winfo_reqwidth()


def wrapped_px(w):
    """Widest line the caption will actually draw.  When wraplength is set the
    text is broken into several lines, so measuring it on ONE line reports a
    phantom 'clip' that does not exist on screen."""
    t = str(w.cget("text") or "")
    if not t:
        return 0
    try:
        f = tkfont.Font(font=w.cget("font") or ("Segoe UI", 10))
        wl = int(w.cget("wraplength") or 0)
    except Exception:
        return label_px(w)
    if wl <= 0:
        return f.measure(t)
    line, widest = "", 0
    for word in t.split():
        trial = (line + " " + word).strip()
        if f.measure(trial) <= wl:
            line = trial
        else:
            widest = max(widest, f.measure(line))
            line = word
    return max(widest, f.measure(line))


def sweep(label):
    for page_name in list(dash.pages):
        try:
            dash.show_page(page_name)
            root.update()
            root.update()
            page = dash.pages[page_name]
        except Exception as e:
            problems.append(f"[{label}] {page_name}: show_page failed {e}")
            continue

        # (a) clipped captions anywhere under this page
        stack = [page]
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
            except Exception:
                continue
            if isinstance(w, (ctk.CTkButton, ctk.CTkLabel)):
                p = wrapped_px(w)
                have = w.winfo_width()
                if p > have + 2 and have > 4:
                    txt = str(w.cget("text") or w._name)[:26]
                    problems.append(
                        f"[{label}] {page_name}: clipped {txt!r} "
                        f"{p}>{have}")

        # (b) content taller than the page
        req, have = page.winfo_reqheight(), page.winfo_height()
        if req > have + 4 and have > 10:
            problems.append(f"[{label}] {page_name}: overflow "
                            f"req={req} have={have} ({req - have}px)")

        # (c) direct children outside the window
        wx = dash.winfo_rootx()
        wy = dash.winfo_rooty()
        right = wx + dash.winfo_width()
        bottom = wy + dash.winfo_height()
        for sl in page.pack_slaves() + page.grid_slaves():
            try:
                x0 = sl.winfo_rootx()
                y0 = sl.winfo_rooty()
                x1 = x0 + sl.winfo_width()
                y1 = y0 + sl.winfo_height()
            except Exception:
                continue
            if x1 > right + 4 or y1 > bottom + 4:
                problems.append(f"[{label}] {page_name}: child outside "
                                f"window ({x1}>{right} or {y1}>{bottom})")


for size in ("1080x700", "1366x768", "1920x1080"):
    w, h = map(int, size.split("x"))
    if w > root.winfo_screenwidth() or h > root.winfo_screenheight():
        print(f"skip {size} (screen {root.winfo_screenwidth()}x"
              f"{root.winfo_screenheight()})")
        continue
    dash.geometry(size)
    root.update()
    root.update()
    print(f"sweeping at {size} "
          f"(actual {dash.winfo_width()}x{dash.winfo_height()})")
    sweep(size)

# resize responsiveness: shrink hard, then restore
dash.geometry("900x600")
root.update()
root.update()
print(f"resizing down to 900x600 -> {dash.winfo_width()}x{dash.winfo_height()}")
sweep("900x600")
dash.geometry("1600x900")
root.update()
root.update()
print(f"resizing up to 1600x900 -> {dash.winfo_width()}x{dash.winfo_height()}")
sweep("1600x900")

print()
if problems:
    seen = set()
    uniq = [p for p in problems if not (p in seen or seen.add(p))]
    print(f"{len(uniq)} PROBLEM(S):")
    for p in uniq[:60]:
        print("  " + p)
else:
    print("NO LAYOUT PROBLEMS ACROSS ALL PAGES")
root.destroy()
sys.exit(1 if problems else 0)
