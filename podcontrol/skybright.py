"""How fast the sky changes brightness, as a function of sun altitude.

The shared AE meters frames that are up to ~50 s old and judges each at the
light index that was in force when it was captured. That gives a correct
ABSOLUTE target for the sky as it was, not as it is -- fine when the sky is
steady, a growing lag when it is not. Measured at dusk on 2026-09-24, the
reported error grew from 0.001 stop at sun -2 deg to 0.11 stop at -4.4, and the
true lag is that plus rate x latency, so roughly 0.28 stop and rising. Below
-6 deg the rate roughly doubles again.

So this module supplies the one thing metering cannot: how much the sky has
changed between two sun altitudes. It is a PRIOR, not a measurement. The AE
still takes its absolute level entirely from the frames; the prior only says
how stale a frame is in stops, and how far the sky will move during the next
cycle. A wrong prior costs a little extra work for the closed loop; it can
never set the exposure on its own.

The curve is the standard twilight one: sky brightness falls roughly 0.6-0.8
magnitudes per degree of solar depression through civil and nautical twilight,
steepest around -8 deg, flattening towards the night floor past -18. One
magnitude is 1.329 stops. The table below is in STOPS PER DEGREE of depression
and brackets what this pod actually showed at dusk on 2026-09-24:

    sun -1.5 deg   measured 0.31 stop/deg   table 0.36
    sun -2.5 deg   measured 0.45            table 0.50
    sun -3.75 deg  measured 0.87            table 0.75

Daylight is deliberately shallow: with the sun well up, sky brightness changes
slowly with altitude and the closed loop has no trouble keeping up.
"""

# (sun altitude deg, stops of extra light needed per degree the sun descends)
SLOPE = [
    (+90.0, 0.01), (+30.0, 0.01), (+10.0, 0.02), (+5.0, 0.06), (+2.0, 0.15),
    (0.0, 0.25), (-2.0, 0.40), (-4.0, 0.80), (-6.0, 1.20), (-8.0, 1.35),
    (-10.0, 1.25), (-12.0, 0.90), (-14.0, 0.55), (-16.0, 0.30), (-18.0, 0.12),
    (-25.0, 0.02), (-90.0, 0.0),
]


def slope(alt_deg):
    """Stops of extra light per degree of solar DESCENT at this altitude."""
    a = float(alt_deg)
    if a >= SLOPE[0][0]:
        return SLOPE[0][1]
    for i in range(len(SLOPE) - 1):
        a0, s0 = SLOPE[i]
        a1, s1 = SLOPE[i + 1]
        if a1 <= a <= a0:
            if a0 == a1:
                return s0
            f = (a0 - a) / (a0 - a1)
            return s0 + f * (s1 - s0)
    return SLOPE[-1][1]


def li_delta(alt_from, alt_to, steps=8):
    """Stops of extra light the sky needs going from `alt_from` to `alt_to`.

    Positive when the sun has descended (the sky got darker), negative when it
    has risen. Trapezoidal over the piecewise-linear slope, which matters over
    a latency window where the altitude moves a fifth of a degree and the slope
    itself is changing."""
    a0, a1 = float(alt_from), float(alt_to)
    if a0 == a1:
        return 0.0
    n = max(1, int(steps))
    h = (a1 - a0) / n
    total = 0.0
    for i in range(n):
        s0 = slope(a0 + i * h)
        s1 = slope(a0 + (i + 1) * h)
        total += 0.5 * (s0 + s1) * h
    return -total          # descending (h < 0) must give a POSITIVE delta


if __name__ == "__main__":
    print("slope, stops per degree of solar descent:")
    for a in (20, 10, 5, 2, 0, -2, -4, -6, -8, -10, -12, -15, -18, -25):
        print("   sun %+5.1f deg  %.2f stop/deg" % (a, slope(a)))
    print("\nstops the sky moves per degree, and over a 50 s frame latency")
    print("(sun descends ~0.21 deg/min at this latitude in September):")
    for a in (0, -2, -4, -6, -8, -10, -12, -15):
        d = li_delta(a, a - 0.21 * 50 / 60.0)
        print("   sun %+5.1f deg  %.3f stop stale after 50 s   (%.2f stop/min)"
              % (a, d, li_delta(a, a - 0.21) ))
