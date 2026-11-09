"""Regenerate the PWA install icons (icon-192.png, icon-512.png).

The icons are binary files, so they are not pushed through the GitHub
snapshot tool; the Docker build runs this script to recreate them.
Design: deep-navy background, white Q ring with a tail (drawn with
shapes only, so no font files are needed).
"""
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "qbill_app", "static", "icons")
NAVY = (10, 37, 64)
WHITE = (255, 255, 255)


def draw_icon(size):
    img = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(img)
    cx = cy = size / 2
    r_out = size * 0.31
    ring_w = max(2, int(size * 0.085))
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], outline=WHITE, width=ring_w)
    # Q tail: diagonal stroke from lower-right of the ring outward.
    x0, y0 = cx + r_out * 0.45, cy + r_out * 0.45
    x1, y1 = cx + r_out * 1.05, cy + r_out * 1.05
    d.line([x0, y0, x1, y1], fill=WHITE, width=ring_w)
    for x, y in ((x0, y0), (x1, y1)):
        d.ellipse([x - ring_w / 2, y - ring_w / 2, x + ring_w / 2, y + ring_w / 2], fill=WHITE)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (192, 512):
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        draw_icon(size).save(path, "PNG")
        print("wrote", path)


if __name__ == "__main__":
    main()
