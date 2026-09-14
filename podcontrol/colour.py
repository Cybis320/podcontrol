"""Colour helpers: an approximate correlated colour temperature from WB gains.

A camera WB gain corrects an illuminant deficiency, so the illuminant's
relative RGB is ~ (1/R, 1/G, 1/B). Normalised to the daylight reference
gains (the config's day preset, taken as D65 / 6504 K), converted with the
sRGB matrix to xy and McCamy's approximation. Good for relative changes and
sanity ("is this 3000 K or 8000 K"); not a calibrated colorimeter.
"""
import math

DAYLIGHT_REF_GAINS = (460 / 256.0, 1.0, 490 / 256.0)   # camera_settings day preset, ~D65 by assumption


def cct_from_gains(r, g, b, ref=DAYLIGHT_REF_GAINS):
    """Approximate CCT (K) for WB gains (r, g, b) as multipliers, or None."""
    try:
        R = (ref[0] / float(r)) / (ref[1] / float(g))
        G = 1.0
        B = (ref[2] / float(b)) / (ref[1] / float(g))
    except (ZeroDivisionError, ValueError, TypeError):
        return None
    if not (R > 0 and B > 0):
        return None
    # linear sRGB -> XYZ (D65)
    X = 0.4124 * R + 0.3576 * G + 0.1805 * B
    Y = 0.2126 * R + 0.7152 * G + 0.0722 * B
    Z = 0.0193 * R + 0.1192 * G + 0.9505 * B
    s = X + Y + Z
    if s <= 0:
        return None
    x, y = X / s, Y / s
    if abs(0.1858 - y) < 1e-6:
        return None
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33
    if not (1000 <= cct <= 40000):
        return None
    return cct


if __name__ == "__main__":
    for gains in ((460 / 256, 1, 490 / 256), (1, 1, 1), (2.2, 1, 1.5), (1.5, 1, 2.3)):
        c = cct_from_gains(*gains)
        print("gains R%.2f G%.2f B%.2f -> %s" % (gains[0], gains[1], gains[2], "%.0f K" % c if c else "n/a"))
