#!/usr/bin/env python3
"""Render the CC RMS utility icons in one shared style.

    python3 cc-utils/make_icon.py <glyph> icon.png      # glyph: see GLYPHS
    python3 cc-utils/make_icon.py --all <dir>           # every glyph, for review

Every icon is the same dark rounded tile with a small meteor streak in the top
left corner (the family mark); only the centre glyph differs. Drawn at 4x and
downsampled, so edges are antialiased without an SVG renderer. Needs Pillow.
"""
import math
import os
import sys

from PIL import Image, ImageDraw

S = 1024                       # working canvas; output is S // 4
OUT = 256

BG_TOP = (27, 35, 48)
BG_BOT = (13, 17, 23)
BORDER = (48, 57, 70)
INK = (230, 237, 243)          # near-white
MUTED = (51, 59, 71)           # tracks, empty panels
PANEL = (30, 38, 50)
AMBER = (227, 165, 44)
GREEN = (63, 185, 80)
BLUE = (88, 166, 255)
RED = (248, 81, 73)

STROKE = 44                    # the one line weight every glyph uses


def tile():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(
            round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)) + (255,))
    mask = Image.new("L", (S, S), 0)
    m = 40
    ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=210, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([m, m, S - m, S - m], radius=210, outline=BORDER, width=12)
    meteor(d)
    return img, d


def meteor(d):
    """Family mark: a short streak with a bright head, top-left."""
    x0, y0, x1, y1 = 120, 120, 230, 230
    n = 24
    for i in range(n):
        t0, t1 = i / n, (i + 1) / n
        a = int(40 + 180 * t1)
        w = int(4 + 12 * t1)
        d.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)],
               fill=(255, 214, 120, a), width=w)
    d.ellipse([x1 - 13, y1 - 13, x1 + 13, y1 + 13], fill=(255, 236, 190, 255))


def rline(d, pts, fill, width=STROKE):
    """Polyline with round caps and joins."""
    d.line(pts, fill=fill, width=width, joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def disc(d, cx, cy, r, fill, outline=None, width=0):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=outline, width=width)


# --- glyphs -----------------------------------------------------------------

def g_config(d):
    """Config Editor: three sliders."""
    rows = [(360, 0.30, AMBER), (512, 0.72, GREEN), (664, 0.52, BLUE)]
    x0, x1 = 250, 800
    for y, t, c in rows:
        rline(d, [(x0, y), (x1, y)], MUTED)
        xk = x0 + (x1 - x0) * t
        rline(d, [(x0, y), (xk, y)], c)
        disc(d, xk, y, 62, INK, outline=c, width=18)


def g_frames(d):
    """Frames Dashboard: a 3x2 wall of camera tiles with status dots."""
    cols, rows = 3, 2
    w, h, gx, gy = 222, 172, 34, 48
    left = (S - (cols * w + (cols - 1) * gx)) / 2
    top = (S - (rows * h + (rows - 1) * gy)) / 2 + 30
    status = [GREEN, GREEN, AMBER, GREEN, GREEN, GREEN]
    stars = [(0.30, 0.30), (0.55, 0.22), (0.78, 0.42), (0.45, 0.55)]
    for r in range(rows):
        for c in range(cols):
            x = left + c * (w + gx)
            y = top + r * (h + gy)
            d.rounded_rectangle([x, y, x + w, y + h], radius=26, fill=PANEL,
                                outline=(70, 82, 98), width=8)
            for sx, sy in stars:
                disc(d, x + w * sx, y + h * sy, 10, INK)
            disc(d, x + 38, y + h - 36, 19, status[r * cols + c])


def g_chrony(d):
    """Chrony Monitor: a clock locked to a pulse-per-second train."""
    cx, cy, R = 512, 450, 230
    disc(d, cx, cy, R, None, outline=BLUE, width=STROKE)
    for k in range(12):
        a = math.radians(k * 30)
        r0 = R - 60 if k % 3 == 0 else R - 40
        d.line([(cx + r0 * math.sin(a), cy - r0 * math.cos(a)),
                (cx + (R - 26) * math.sin(a), cy - (R - 26) * math.cos(a))],
               fill=INK if k % 3 == 0 else (120, 132, 148), width=14)
    rline(d, [(cx, cy), (cx, cy - 150)], INK)                     # minute hand at 12
    rline(d, [(cx, cy), (cx + 105, cy + 60)], INK)                # hour hand
    disc(d, cx, cy, 30, AMBER)
    # PPS pulse train under the clock
    y_lo, y_hi = 800, 740
    xs = [230, 330, 330, 380, 380, 500, 500, 550, 550, 670, 670, 720, 720, 800]
    pts = []
    for i, x in enumerate(xs):
        pts.append((x, y_lo if (i // 2) % 2 == 0 else y_hi))
    d.line(pts, fill=GREEN, width=22, joint="curve")


def g_windows(d):
    """Realign Windows: four terminal windows snapped into a grid."""
    w, h, g = 290, 220, 36
    left = (S - (2 * w + g)) / 2
    top = (S - (2 * h + g)) / 2 + 20
    bars = [BLUE, BLUE, AMBER, BLUE]
    for r in range(2):
        for c in range(2):
            x = left + c * (w + g)
            y = top + r * (h + g)
            d.rounded_rectangle([x, y, x + w, y + h], radius=24, fill=PANEL,
                                outline=(70, 82, 98), width=8)
            d.rounded_rectangle([x, y, x + w, y + 46], radius=24, fill=bars[r * 2 + c])
            d.rectangle([x, y + 24, x + w, y + 46], fill=bars[r * 2 + c])
            for k, frac in enumerate((0.75, 0.55, 0.65)):
                yy = y + 90 + k * 38
                d.line([(x + 30, yy), (x + 30 + (w - 60) * frac, yy)],
                       fill=(120, 132, 148), width=14)


def g_pod(d):
    """Pod Control: six cameras around one shared control dial."""
    cx, cy, R = 512, 530, 250
    for k in range(6):
        a = math.radians(90 + k * 60)
        x, y = cx + R * math.cos(a), cy - R * math.sin(a)
        rline(d, [(cx, cy), (x, y)], MUTED, width=18)
    for k in range(6):
        a = math.radians(90 + k * 60)
        x, y = cx + R * math.cos(a), cy - R * math.sin(a)
        disc(d, x, y, 78, PANEL, outline=BLUE, width=20)
        disc(d, x, y, 34, (8, 10, 14))
        disc(d, x - 12, y - 12, 9, INK)
    disc(d, cx, cy, 104, INK)
    a = math.radians(-50)
    rline(d, [(cx + 30 * math.cos(a), cy + 30 * math.sin(a)),
              (cx + 74 * math.cos(a), cy + 74 * math.sin(a))], AMBER, width=26)


GLYPHS = {
    "config": g_config,
    "frames": g_frames,
    "chrony": g_chrony,
    "windows": g_windows,
    "pod": g_pod,
}


def render(glyph, path):
    img, d = tile()
    GLYPHS[glyph](d)
    img.resize((OUT, OUT), Image.LANCZOS).save(path, optimize=True)


def main(argv):
    if len(argv) == 3 and argv[1] == "--all":
        os.makedirs(argv[2], exist_ok=True)
        for g in GLYPHS:
            render(g, os.path.join(argv[2], g + ".png"))
        return 0
    if len(argv) != 3 or argv[1] not in GLYPHS:
        print(__doc__.strip() + "\n\nglyphs: " + ", ".join(GLYPHS), file=sys.stderr)
        return 2
    render(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
