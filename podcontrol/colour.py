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


def locus_xy(cct):
    """Chromaticity (x, y) of the illuminant at cct K: the CIE daylight locus
    from 4000 K up, the Planckian locus (Kim et al. 2002 approximation)
    below. None outside 1667-25000 K."""
    T = float(cct)
    if not (1667.0 <= T <= 25000.0):
        return None
    if T >= 4000.0:
        if T <= 7000.0:
            x = -4.6070e9 / T ** 3 + 2.9678e6 / T ** 2 + 0.09911e3 / T + 0.244063
        else:
            x = -2.0064e9 / T ** 3 + 1.9018e6 / T ** 2 + 0.24748e3 / T + 0.237040
        y = -3.000 * x * x + 2.870 * x - 0.275
    else:
        x = -0.2661239e9 / T ** 3 - 0.2343589e6 / T ** 2 + 0.8776956e3 / T + 0.179910
        if T <= 2222.0:
            y = -1.1063814 * x ** 3 - 1.34811020 * x * x + 2.18555832 * x - 0.20219683
        else:
            y = -0.9549476 * x ** 3 - 1.37418593 * x * x + 2.09137015 * x - 0.16748867
    return x, y


def gains_from_cct(cct, g=1.0, ref=DAYLIGHT_REF_GAINS):
    """WB gain multipliers (r, g, b) that neutralise a daylight-locus
    illuminant of cct K -- the inverse of cct_from_gains, anchored the same
    way (6504 K -> the daylight reference gains). The green gain is kept at
    g and R/B are set relative to it. One axis only: the green-magenta tint
    is fixed to the locus, so a calibrated balance is not reproducible from
    its Kelvin readout alone. None outside 1667-25000 K."""
    xy = locus_xy(cct)
    if xy is None:
        return None
    x, y = xy
    if y <= 0:
        return None
    X, Y, Z = x / y, 1.0, (1.0 - x - y) / y
    # XYZ -> linear sRGB (D65): the illuminant's relative RGB
    R = 3.2406 * X - 1.5372 * Y - 0.4986 * Z
    G = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    B = 0.0557 * X - 0.2040 * Y + 1.0570 * Z
    if not (R > 0 and G > 0 and B > 0):
        return None
    R, B = R / G, B / G
    return (float(g) * ref[0] / ref[1] / R, float(g), float(g) * ref[2] / ref[1] / B)


if __name__ == "__main__":
    for k in (2500, 3200, 4000, 5000, 5600, 6504, 8000, 10000, 15000, 20000):
        r, g, b = gains_from_cct(k)
        back = cct_from_gains(r, g, b)
        print("%6d K -> gains R%.3f G%.3f B%.3f (x256 %d/%d/%d) -> reads %s K" % (
            k, r, g, b, round(r * 256), round(g * 256), round(b * 256), "%.0f" % back if back else "n/a"))
    for gains in ((460 / 256, 1, 490 / 256), (1, 1, 1), (2.2, 1, 1.5), (1.5, 1, 2.3)):
        c = cct_from_gains(*gains)
        print("gains R%.2f G%.2f B%.2f -> %s" % (gains[0], gains[1], gains[2], "%.0f K" % c if c else "n/a"))
