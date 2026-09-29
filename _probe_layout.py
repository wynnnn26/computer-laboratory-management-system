"""Layout probe (Phase 6): measure overview + Client PCs at the minimum
window size (1080x700) and at the default (1280x780).  Fails loudly when
any page's demand exceeds its height (clipped rows), when column-0
content (stats/toolbar/chips/bulk bar/selection row) runs under the
right-side details panel, when a grid button's label is clipped, or when
the details panel is missing/unmapped.  Run:  python _probe_layout.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import tkinter as tk
from tkinter import font as _tkfont
import customtkinter as _ctk
import database
from admin_dashboard import AdminDashboard

database.init_db()
root = tk.Tk()
root.withdraw()
events = __import__("queue").Queue()
srv = None
try:
    import server as server_mod
    srv = server_mod.LabServer(port=18443)
    srv.start()
except Exception as e:
    print(f"(no live server for probe: {e})")

dash = AdminDashboard(root,
                      {"id": 1, "student_id": "admin",
                       "full_name": "Test Admin", "role": "admin"},
                      lambda: None, server=srv, server_events=events)
root.update()

FAIL = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def _is_button(w):
    """CustomTkinter widgets all report winfo_class() == 'Frame', so the
    widget type - not the Tk class name - is what identifies a button."""
    return (w.winfo_class() in ("Button", "TButton")
            or isinstance(w, _ctk.CTkButton))


def _label_px(w):
    """Rendered width of a button's caption.

    tk/ttk buttons answer that through winfo_reqwidth(); CustomTkinter
    buttons carry a fixed default width instead, so the caption is measured
    against the button's own font."""
    text = str(w.cget("text") or "")
    if not text:
        return 0
    try:
        f = w.cget("font") or ("Segoe UI", 10)
        return _tkfont.Font(font=f).measure(text)
    except Exception:
        return w.winfo_reqwidth()


def probe(label):
    root.update()
    out = {}
    for key in ("overview", "clients"):
        if key not in dash.pages:
            continue
        dash.show_page(key)
        root.update()
        page = dash.pages[key]
        req, have = page.winfo_reqheight(), page.winfo_height()
        # grid rows: sum of row heights + the weight row's floor
        rows = sorted({int(k.split(".")[1]) for k in page.grid_info().keys()
                       if k.startswith("row")} |
                      set(page.grid_info()))
        demand = 0
        for r in range(10):
            gi = page.grid_info().get(f"row.{r}")
            if gi:
                demand += int(gi["height"] or 0)
        # details panel
        panels = [p for p in dash.detail_panels
                  if p["frame"].winfo_manager() == "grid"
                  and p["frame"].master is page]
        d_ok = bool(panels) and panels[0]["frame"].winfo_ismapped()
        d_h = panels[0]["frame"].winfo_height() if panels else 0
        # bottom-most direct child vs page height
        lowest = 0
        for sl in page.pack_slaves() + page.grid_slaves():
            gi = sl.grid_info() if sl.winfo_manager() == "grid" else None
            y = sl.winfo_y() if not gi else gi.get("y")
            h = sl.winfo_height()
            lowest = max(lowest, (int(y or 0)) + h)
        out[key] = dict(req=req, have=have, demand=demand, d_ok=d_ok,
                        d_h=d_h, lowest=lowest)
        overflow = max(req, demand) - have
        check(f"[{label}] {key}: demand <= height (no clipped rows)",
              overflow <= 4,
              f"req={req} demand={demand} have={have} overflow={overflow}")
        check(f"[{label}] {key}: right-side details panel mapped",
              d_ok, f"h={d_h}")
        check(f"[{label}] {key}: last widget within the page",
              lowest <= have + 4, f"lowest={lowest} have={have}")
        # ---- horizontal: no column-0 content may run under the
        #      right-hand details panel, and no grid button label may clip
        if panels:
            edge = panels[0]["frame"].winfo_rootx() - 1
            over, clipped = [], []

            def watch(w, name):
                if w is None or not w.winfo_exists() or not w.winfo_ismapped():
                    return
                r = w.winfo_rootx() + w.winfo_width()
                if r > edge:
                    over.append(f"{name}:{r}>{edge}")
                if _is_button(w):
                    want, have = _label_px(w), w.winfo_width()
                    if want > have + 2:
                        clipped.append(f"{name}:{want}>{have}")

            if key == "overview":
                watch(dash.cards_frame, "stats")
                for t in dash.cards_frame.winfo_children():
                    watch(t, "tile")
                for w in dash.pc_search.master.pack_slaves():
                    watch(w, "tool")
                head = dash._sel_labels[0].master
                for w in head.pack_slaves():
                    watch(w, "head")
                bulk_row = dash._ctrl_btns[0].master
                for w in bulk_row.grid_slaves():
                    watch(w, "bulk")
                watch(dash.bulk_results.master, "results")
            else:
                for lbl in dash._sel_labels:      # clients selection row
                    if lbl.winfo_exists() and lbl.master.master is page:
                        for w in lbl.master.pack_slaves():
                            watch(w, "selrow")
                cbtns = [b for b in dash._ctrl_btns
                         if b.winfo_exists() and b.master.master is page]
                if cbtns:
                    for w in cbtns[0].master.grid_slaves():
                        watch(w, "ctrl")
            for w in [dash._legend_label] + list(dash._legend_btns.values()):
                watch(w, "chip")
            check(f"[{label}] {key}: nothing runs under the details panel",
                  not over, str(over))
            check(f"[{label}] {key}: button labels are not clipped",
                  not clipped, str(clipped))
    dash.show_page("overview")
    root.update()
    # stats strip / chips / bulk / search sanity at this size
    check(f"[{label}] stats strip = 7 tiles",
          len(dash.cards_frame.winfo_children()) == 7)
    check(f"[{label}] search placeholder intact",
          dash._pc_search_ph and str(dash.pc_search_var.get()) == "Search PCs...")
    # sidebar: every entry reachable (scrollable + More collapsed)
    check(f"[{label}] More collapsed", not dash._more_expanded)
    root.update()
    sb_need = dash._menu_host.winfo_reqheight()
    sb_have = dash._menu_canvas.winfo_height()
    dash._toggle_more()
    root.update()
    check(f"[{label}] More group reachable after expand",
          all(b.winfo_ismapped() for b in dash._more_btns))
    check(f"[{label}] sidebar scrollbar appears when expanded",
          sb_need <= sb_have or dash._sb_vsb.winfo_ismapped(),
          f"need={sb_need} have={sb_have}")
    dash._toggle_more()
    root.update()
    return out


for size in ("1080x700", "1280x780", "1440x900"):
    w, h = map(int, size.split("x"))
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    dash.geometry(size)
    dash.minsize(1, 1)          # allow the probe below the real min
    root.update()
    if w <= sw and h <= sh:     # bigger than the screen: OS clamps it
        check(f"[{size}] window honors requested size",
              abs(dash.winfo_width() - w) <= 8
              and abs(dash.winfo_height() - h) <= 8,
              f"got {dash.winfo_width()}x{dash.winfo_height()}")
    else:
        print(f"NOTE  [{size}] larger than screen {sw}x{sh} - "
              f"clamped to {dash.winfo_width()}x{dash.winfo_height()}, "
              "size check skipped")
    probe(size)
    dash.minsize(1080, 700)

# ---- P4: Observe / Remote Control are offered to the ADMINISTRATOR only --
# The Server refuses the calls for anyone else; this checks the UI half -
# the two controls must not even be built for a non-administrator role.


def _btext(b):
    try:
        return str(b.cget("text"))
    except Exception:
        return ""


check("[p4] the ADMINISTRATOR keeps both controls",
      dash._remote_btn is not None
      and any(_btext(b).endswith("Observe") for b in dash._ctrl_btns),
      f"remote={dash._remote_btn is not None}")

staff_dash = AdminDashboard(root,
                            {"id": 2, "student_id": "staff1",
                             "full_name": "Test Staff", "role": "staff"},
                            lambda: None, server=srv, server_events=events)
staff_dash.withdraw()
root.update()
check("[p4] a STAFF dashboard carries its real role",
      staff_dash.role_name == "staff" and staff_dash.is_admin is False,
      f"{staff_dash.role_name},{staff_dash.is_admin}")
check("[p4] Observe and Remote are not offered to STAFF",
      staff_dash._remote_btn is None
      and not [b for b in staff_dash._ctrl_btns
               if _btext(b).endswith(("Observe", "Remote"))],
      str([_btext(b) for b in staff_dash._ctrl_btns]))
check("[p4] STAFF still keeps the ordinary controls",
      any(_btext(b).endswith("Lock")
          for b in staff_dash._ctrl_btns),
      str([_btext(b) for b in staff_dash._ctrl_btns]))
try:
    staff_dash.destroy()
except Exception:
    pass

if srv:
    try:
        srv.stop()
    except Exception:
        pass
root.destroy()

print()
if FAIL:
    print(f"*** {len(FAIL)} LAYOUT FAILURES: {FAIL}")
    sys.exit(1)
print("ALL LAYOUT PROBES PASSED")
