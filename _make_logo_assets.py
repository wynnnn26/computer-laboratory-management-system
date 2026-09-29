"""Regenerate app_icon.ico from the single official logo.

Source of truth:  assets/images/logo.png  (the provided seal, never edited).
Output:           app_icon.ico            (multi-size, needed by PyInstaller
                                           and by the Windows title bar).

Run once:  python _make_logo_assets.py

The logo is centred on a square canvas and scaled with LANCZOS - the aspect
ratio and transparency of the original are preserved (never stretched).
"""
import os

from PIL import Image

# Sizes Windows/PyInstaller actually ask for.
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]


def build_square(src_path):
    """Centre the logo on a square canvas - aspect ratio untouched."""
    im = Image.open(src_path).convert("RGBA")
    # Trim fully-transparent margins first so the seal sits centred and
    # uses every pixel of every icon size.
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(im, ((side - w) // 2, (side - h) // 2))
    return square


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "assets", "images", "logo.png")
    dst = os.path.join(here, "app_icon.ico")
    square = build_square(src)
    # Pillow generates every requested size from this one square image,
    # which keeps all entries pixel-identical to each other.
    square.save(dst, format="ICO", sizes=ICO_SIZES)
    print(f"wrote {dst} from {src} sizes={[s[0] for s in ICO_SIZES]}")


if __name__ == "__main__":
    main()
