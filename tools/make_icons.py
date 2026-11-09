"""Regenerate the brand assets in qbill_app/static/icons/.

The real QBill logo (chosen by the owner on 2026-11-09: the orange
receipt-Q mark, "QBill — by Abdul Qudoos") is stored as base64 text
files, because binary files cannot travel through the GitHub snapshot
pushes:
  tools/icon-master.b64 -> icon-192.png + icon-512.png (the mark)
  tools/logo-full.b64   -> logo-full.png (the full logo lockup)

The Docker build runs this script to recreate the PNGs. If the .b64
files are missing, it falls back to a simple drawn placeholder
(deep-navy square, white Q ring) so the build never breaks.
"""
import base64
import io
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(__file__)
OUT_DIR = os.path.join(HERE, "..", "qbill_app", "static", "icons")
NAVY = (10, 37, 64)
WHITE = (255, 255, 255)


def _from_b64(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, "r") as fh:
        raw = base64.b64decode(fh.read())
    return Image.open(io.BytesIO(raw)).convert("RGB")


def draw_placeholder(size):
    img = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(img)
    cx = cy = size / 2
    r_out = size * 0.31
    ring_w = max(2, int(size * 0.085))
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], outline=WHITE, width=ring_w)
    x0, y0 = cx + r_out * 0.45, cy + r_out * 0.45
    x1, y1 = cx + r_out * 1.05, cy + r_out * 1.05
    d.line([x0, y0, x1, y1], fill=WHITE, width=ring_w)
    for x, y in ((x0, y0), (x1, y1)):
        d.ellipse([x - ring_w / 2, y - ring_w / 2, x + ring_w / 2, y + ring_w / 2], fill=WHITE)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    master = _from_b64("icon-master.b64")
    for size in (192, 512):
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        if master is not None:
            master.resize((size, size), Image.LANCZOS).save(path, "PNG")
        else:
            draw_placeholder(size).save(path, "PNG")
        print("wrote", path)
    full = _from_b64("logo-full.b64")
    if full is not None:
        path = os.path.join(OUT_DIR, "logo-full.png")
        full.save(path, "PNG")
        print("wrote", path)


if __name__ == "__main__":
    main()
