"""Lens vignetting for the sky composite: the model, and a fit from the pod.

WHY THIS EXISTS. The composite applies no photometric correction, deliberately,
so a camera that drifts out of the pod's shared settings shows up as a step at
its seams. But every seam already carries a step that is nothing to do with
exposure: overlaps sit at the EDGE of both fields by construction and rarely at
the same edge distance in each, so one camera images a shared patch nearer its
optical axis than the other and reads brighter. Measured 2026-09-28, that alone
made pairs disagree by up to 11%, which is far larger than the pod's real
mismatch of about 5%. Correcting it is what lets the eye judge the rest.

THE MODEL is RMS's, so the parameterisation is the one the field already uses:

    V(r) = cos(k * r) ** 4          k in rad/px, r from the optical axis in px

and a frame is flattened by dividing by V. RMS carries a coefficient of its own
in the platepar, but it is fitted against STAR PHOTOMETRY -- integrated flux of
point sources -- which absorbs focus softening and extinction as well as
illumination falloff, and on this pod five of six platepars were never fitted at
all and hold RMS's default. Measured, that default over-corrects by 92%: it asks
for 3.29x at the corner where 1.35x is needed. So podcontrol keeps its own
coefficient and does not touch the platepar, whose value RMS still needs for
meteor magnitudes.

THE FIT uses the overlaps, which make it well posed without a sky model at all.
For a direction p seen by cameras i and j:

    obs_i(p) = S(p) * V(r_i) * g_i

so obs_i/obs_j cancels S(p) exactly -- same direction, same sky, same air mass.
Time-averaging cannot do this: the cameras do not move, so horizon glow sits at
a fixed place in every frame and survives any stack.

ONE COEFFICIENT FOR THE POD. All six cameras are the same module with the same
lens, so the falloff is one shared number and only the per-camera gains differ.
That is not just tidier, it is what makes the fit work: overlaps never reach
inside r~0.35 of any frame, so per-camera curves extrapolate blind to the centre
where the gain normalisation is anchored, and each camera's shape trades against
its own gain. Fitted that way the residual was 18%, one camera came out
BRIGHTENING toward its edge, and the gains spread 249% on a pod matched to a few
percent. Shared, the residual is 5.1%, the gains land within 5%, and the
residual's correlation with radius falls to -0.019.
"""
import math
import numpy as np

# Fitted 2026-09-28 from 14 frame sets, ~300k overlap samples, residual 5.1%.
# For scale: RMS's default is 0.000667 and the one platepar RMS actually fitted
# (US05E1) holds 0.000398, which is 15% from this -- an independent check, since
# that came from star photometry and this from sky background.
DEFAULT_COEFF = 0.000347        # rad/px
MAX_COEFF = 0.0015              # refuse anything past roughly twice RMS's default
MAX_GAIN = 4.0                  # clamp the corner boost, so a bad coefficient cannot blow up


def response(r_px, coeff):
    """V(r): the fraction of light that reaches the sensor at radius r_px."""
    if not coeff:
        return np.ones_like(np.asarray(r_px, np.float32))
    x = np.clip(np.asarray(r_px, np.float32) * float(coeff), 0.0, 1.45)
    return np.cos(x) ** 4


def gain(r_px, coeff):
    """1/V(r): what a frame is multiplied by to flatten it. Clamped, so a
    coefficient typed in by hand cannot turn the corners into noise."""
    if not coeff:
        return None
    return np.clip(1.0 / np.maximum(response(r_px, coeff), 1e-3), 1.0, MAX_GAIN).astype(np.float32)


def corner_gain(coeff, width=1920, height=1080):
    """The correction at the corner of a full frame, for the UI to show."""
    r = math.hypot(width / 2.0, height / 2.0)
    v = float(np.cos(min(r * float(coeff), 1.45)) ** 4) if coeff else 1.0
    return 1.0 / max(v, 1e-3)


def clamp(coeff):
    try:
        c = float(coeff)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if c <= 0 else min(c, MAX_COEFF)


# --------------------------------------------------------------------------
def fit(renderer, sets, progress=None):
    """Fit the shared coefficient from overlapping fields.

    `sets` is [{station_id: path}] -- frame sets, each as near one instant as
    the source allows. Returns a dict with `coeff`, per-camera `gains`, the
    residual and the sample count, or an `error`.

    Solves, in logs and least squares:
        log obs_i - log obs_j = -c (r_i^2 - r_j^2) + b_i - b_j
    r^2 rather than cos^4 because that is the leading term of it (log cos^4(x)
    = -2x^2 - x^4/3 - ...) and the two differ by under 0.2% over the radius
    range that exists, well inside the fit's own 5% scatter. Fitting the linear
    form avoids an iterative solve; the result is converted back to rad/px so
    what is stored and displayed is RMS's parameterisation.
    """
    import cv2
    ids = [sid for sid in renderer.by_id if renderer.luts.get(sid) is not None
           and renderer.luts[sid].r_px is not None]
    if len(ids) < 2:
        return {"error": "need at least two cameras with a lookup table"}
    n = len(ids)
    pos = {sid: k for k, sid in enumerate(ids)}
    grid = renderer.grid
    from podcontrol import frames as _frames
    rad, cov, rmax = {}, {}, {}
    for sid in ids:
        lut = renderer.luts[sid]
        R = np.zeros((grid.h, grid.w), np.float32)
        C = np.zeros((grid.h, grid.w), bool)
        y0, y1, x0, x1 = lut.bbox
        R[y0:y1, x0:x1] = lut.r_px
        # A masked pixel is a building or a tree, not sky, and the LUT keeps it
        # at a small weight rather than dropping it. If a direction is blocked in
        # one camera and clear in the other their ratio is meaningless, and those
        # pixels sit near the horizon, which is the frame edge, exactly where the
        # radial signal lives. So the fit uses clear sky only.
        ok = np.ones(lut.weight.shape, bool)
        keep = _frames.load_mask(renderer.by_id[sid])
        if keep is not None:
            sm = cv2.resize(keep.astype(np.uint8), lut.small, interpolation=cv2.INTER_NEAREST)
            xi = np.clip(np.rint(lut.map_x).astype(int), 0, lut.small[0] - 1)
            yi = np.clip(np.rint(lut.map_y).astype(int), 0, lut.small[1] - 1)
            ok = sm[yi, xi] > 0
        C[y0:y1, x0:x1] = (lut.weight > 0) & ok
        rad[sid], cov[sid] = R, C
        rmax[sid] = float(lut.r_px.max()) or 1.0
    scale = max(rmax.values())          # normalise so the solve is well scaled

    rows, rhs = [], []
    for si, paths in enumerate(sets):
        if progress:
            progress(si, len(sets))
        warp = {}
        for sid in ids:
            p = paths.get(sid)
            if not p:
                continue
            lut = renderer.luts[sid]
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            small = cv2.resize(img, lut.small, interpolation=cv2.INTER_AREA).astype(np.float32)
            w = cv2.remap(small, lut.map_x, lut.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            F = np.zeros((grid.h, grid.w), np.float32)
            y0, y1, x0, x1 = lut.bbox
            F[y0:y1, x0:x1] = w
            warp[sid] = F
        for a in range(n):
            for b in range(a + 1, n):
                ia, ib = ids[a], ids[b]
                if ia not in warp or ib not in warp:
                    continue
                # skip the floor (noise dominates a log) and anything clipped
                m = (cov[ia] & cov[ib] & (warp[ia] > 12) & (warp[ib] > 12)
                     & (warp[ia] < 230) & (warp[ib] < 230))
                ys, xs = np.nonzero(m)
                if len(ys) < 200:
                    continue
                ys, xs = ys[::3], xs[::3]
                row = np.zeros((len(ys), 1 + n), np.float32)
                row[:, 0] = -((rad[ia][ys, xs] / scale) ** 2 - (rad[ib][ys, xs] / scale) ** 2)
                row[:, 1 + a] = 1.0
                row[:, 1 + b] = -1.0
                rows.append(row)
                rhs.append(np.log(warp[ia][ys, xs]) - np.log(warp[ib][ys, xs]))
    if not rows:
        return {"error": "no usable overlap samples (too dark, clipped, or no shared sky)"}
    A = np.vstack(rows)
    y = np.concatenate(rhs)
    # gauge: the gains are only defined up to a common factor, so pin their sum
    g = np.zeros((1, 1 + n), np.float32)
    g[0, 1:] = 1.0
    sol, *_ = np.linalg.lstsq(np.vstack([A, g * 50.0]), np.concatenate([y, [0.0]]), rcond=None)
    res = float(np.sqrt(np.mean((A @ sol - y) ** 2)))
    c_norm = float(sol[0])
    if c_norm <= 0:
        return {"error": "fit produced a brightening lens (%.3f): not enough overlap signal" % c_norm,
                "residual": res, "samples": int(len(y))}
    # exp(-c r_norm^2) -> cos(k r_px)^4 matched at the corner
    v_corner = math.exp(-c_norm)
    k = math.acos(min(1.0, v_corner ** 0.25)) / scale
    return {"coeff": clamp(k), "residual": res, "samples": int(len(y)),
            "corner": 1.0 / v_corner, "sets": len(sets),
            "gains": {sid: float(np.exp(sol[1 + pos[sid]])) for sid in ids}}
