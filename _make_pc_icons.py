"""Regenerate the 8 PC status icons in pc_icons/.

The artwork comes from utils.render_status_tile (white monitor outline on a
dark tile, screen + status dot colored per state - see the reference sheet).
Run once:  python _make_pc_icons.py
"""
import os

from utils import STATUS_ORDER, render_status_tile, STATUS_LABELS


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_icons")
    os.makedirs(out_dir, exist_ok=True)
    for key in STATUS_ORDER:
        path = os.path.join(out_dir, f"{key}.png")
        render_status_tile(key, 256).save(path)
        print(f"wrote {path}  ({STATUS_LABELS[key]})")


if __name__ == "__main__":
    main()
