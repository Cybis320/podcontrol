"""Sun exclusion: keep the sun and its glare out of every measurement.

The RMS platepar (astrometric calibration) gives the alt/az of every pixel;
the camera does not move, so that is computed ONCE per station on a coarse
grid. At measurement time the sun's alt/az (ephem, from the platepar's site)
gives each pixel's angular distance to the sun, and pixels within
`radius_deg` are excluded. The static RMS mask.bmp handles fixed
obstructions; this handles the one bright thing that moves.

The radius is the saturated blob + flare around the sun, which depends on the
lens, haze and exposure. Measure it on real frames:

    python -m podcontrol.sunmask --measure US05B1      # profile around the sun
    python -m podcontrol.sunmask --where               # sun in each camera now

The exclusion is applied whenever the circle can touch the sky (sun altitude
above -radius), so the bright twilight glow around a just-set sun is also
kept out of the metering.
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os, math, time, glob, datetime
import numpy as np
import cv2

DEFAULT_RADIUS_DEG = 25.0
GRID_STEP = 8            # px between alt/az samples; upsampled nearest to the frame

# Lens flare. Internal-reflection ghosts lie on the line through the sun's
# image and the optical centre (a radial distortion keeps that line straight)
# at FIXED fractions k of the sun-to-centre distance: k = 1 is the sun, 0 the
# centre, k < 0 the mirrored side; their angular size is ~constant. Measured
# on US05B1 (2026-09-14, sun 11-17 deg from the centre, 7 clean frames): a
# pale disc, radius 2-5 deg (larger as the sun nears the centre), centred at
# k = -0.52..-0.58, 60-90 luma above the sky, within 2 deg of the axis. With
# the sun ~12 deg OUTSIDE the field (US05F1) a smaller ghost shows at
# k ~ -0.20 (mirrored, ~10 deg from the centre). Model = ghost DISCS at
# (k, radius scale) x ghost_radius_deg, applied while the sun is within
# FLARE_MAX_SEP_DEG of the pixel grid, plus a narrow corridor along the whole
# axis (rim streaks) only while the sun is inside the field.
# (k, radius scale, max sun separation from the field in deg for this ghost)
FLARE_GHOSTS = [(-0.55, 1.0, 5.0), (-0.20, 1.0, 30.0)]
DEFAULT_GHOST_RADIUS_DEG = 6.0
FLARE_K_MIN, FLARE_K_MAX = -1.6, 1.0
FLARE_CORRIDOR_HALF_WIDTH_DEG = 3.0
FLARE_MAX_SEP_DEG = 30.0                      # ghosts appear with the sun well outside the field

_STATE = {}              # station.id -> precomputed grid (or None if unavailable)
_CACHE = {}              # (station, 30 s bucket, radius, shape) -> (excl, info)
_WARNED = set()


def _utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).replace(tzinfo=None)


def _load(station):
    """Precompute the alt/az of a pixel grid from the station platepar."""
    sid = station.id
    if sid in _STATE:
        return _STATE[sid]
    st = None
    pp_path = getattr(station, "platepar_path", "") or ""
    if pp_path and os.path.isfile(pp_path):
        try:
            import ephem
            from RMS.Formats.Platepar import Platepar
            from RMS.Astrometry.ApplyAstrometry import xyToRaDecPP
            from RMS.Astrometry.Conversions import raDec2AltAz
            pp = Platepar()
            pp.read(pp_path, use_flat=None)
            h, w = int(pp.Y_res), int(pp.X_res)
            ys, xs = np.mgrid[0:h:GRID_STEP, 0:w:GRID_STEP]
            gh, gw = ys.shape
            xs = xs.ravel().astype(float); ys = ys.ravel().astype(float)
            jd = float(pp.JD)                   # any epoch: pixel alt/az is fixed
            _, ra, dec, _ = xyToRaDecPP(np.full(xs.size, jd), xs, ys, np.ones(xs.size), pp,
                                        extinction_correction=False, jd_time=True,
                                        precompute_pointing_corr=True)
            az, alt = raDec2AltAz(ra, dec, jd, pp.lat, pp.lon)
            obs = ephem.Observer()
            obs.lat, obs.lon, obs.elevation = str(pp.lat), str(pp.lon), float(pp.elev)
            obs.pressure = 0                    # unrefracted, like the platepar alt/az
            st = {"alt": np.radians(alt).reshape(gh, gw), "az": np.radians(az).reshape(gh, gw),
                  "shape": (h, w), "pp": pp, "obs": obs}
        except Exception as e:
            if sid not in _WARNED:
                _WARNED.add(sid)
                print("sunmask: %s disabled (%s)" % (sid, e))
    _STATE[sid] = st
    return st


def available(station):
    return _load(station) is not None


def sun_altaz(station, t=None):
    """Sun alt/az (deg, unrefracted) + astrometric J2000 RA/Dec at epoch t."""
    st = _load(station)
    if not st:
        return None
    import ephem
    st["obs"].date = _utc(time.time() if t is None else t)
    s = ephem.Sun(st["obs"])
    return {"alt": math.degrees(s.alt), "az": math.degrees(s.az),
            "ra": math.degrees(s.a_ra), "dec": math.degrees(s.a_dec)}


# RMS switches day/night capture modes when the sun crosses this altitude
# (RMS.CaptureModeSwitcher.SWITCH_HORIZON_DEG)
SWITCH_HORIZON_DEG = -9.0


def mode_for(station, t=None):
    """'day' or 'night' as RMS would have it for this station at epoch t, or
    None when the station has no platepar to locate the sun."""
    sa = sun_altaz(station, t)
    if sa is None:
        return None
    return "day" if sa["alt"] > SWITCH_HORIZON_DEG else "night"


def _sun_pixel(station, sa, t):
    from RMS.Astrometry.ApplyAstrometry import raDecToXYPP
    from RMS.Astrometry.Conversions import datetime2JD
    x, y = raDecToXYPP(np.array([sa["ra"]]), np.array([sa["dec"]]), datetime2JD(_utc(t)),
                       _load(station)["pp"])
    return float(x[0]), float(y[0])


def separation_map(station, sa):
    """Angular distance (deg) of every grid pixel from the sun direction."""
    st = _load(station)
    a1, z1 = st["alt"], st["az"]
    a2, z2 = math.radians(sa["alt"]), math.radians(sa["az"])
    cosd = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(z1 - z2)
    return np.degrees(np.arccos(np.clip(cosd, -1.0, 1.0)))


def optical_centre(station):
    """The lens's principal point on the sensor = the platepar's fitted radial
    distortion centre (x_poly_rev[0], [1] in units of the half-width/height,
    as RMS's cyXYToRADec applies them), NOT the geometric image centre.
    Flare ghosts mirror through this point; on US05B1 it is 36 px below the
    image centre, which is exactly the drift the ghost showed as the sun moved.
    Falls back to the image centre for non-radial models or forced centres."""
    pp = _load(station)["pp"]
    cx, cy = pp.X_res / 2.0, pp.Y_res / 2.0
    try:
        if str(pp.distortion_type).startswith("radial") and not getattr(pp, "force_distortion_centre", False):
            cx += float(pp.x_poly_rev[0]) * pp.X_res / 2.0
            cy += float(pp.x_poly_rev[1]) * pp.Y_res / 2.0
    except Exception:
        pass
    return (cx, cy)


def flare_map(station, sx, sy, shape, ghost_radius_deg=DEFAULT_GHOST_RADIUS_DEG,
              corridor_half_width_deg=FLARE_CORRIDOR_HALF_WIDTH_DEG,
              ghosts=FLARE_GHOSTS, k_min=FLARE_K_MIN, k_max=FLARE_K_MAX, sun_sep_deg=0.0):
    """Bool map (True = excluded) of the lens-flare model for a sun imaged at
    (sx, sy) (may be outside the frame): ghost discs at k * (sun - centre)
    with radius ghost_radius_deg * scale, plus a corridor (capsule) along the
    axis from k_min to k_max, corridor_half_width_deg wide. F_scale px/deg."""
    pp = _load(station)["pp"]
    cx, cy = optical_centre(station)
    h, w = shape
    Fs = float(pp.F_scale)
    m = np.zeros((h, w), np.uint8)
    in_frame = 0 <= sx < w and 0 <= sy < h
    if corridor_half_width_deg > 0 and in_frame:
        p1 = (int(round(cx + k_min * (sx - cx))), int(round(cy + k_min * (sy - cy))))
        p2 = (int(round(cx + k_max * (sx - cx))), int(round(cy + k_max * (sy - cy))))
        cv2.line(m, p1, p2, 1, max(1, int(round(2 * corridor_half_width_deg * Fs))))
    if ghost_radius_deg > 0:
        for k, scale, max_sep in ghosts:
            if sun_sep_deg > max_sep:
                continue
            c = (int(round(cx + k * (sx - cx))), int(round(cy + k * (sy - cy))))
            cv2.circle(m, c, max(1, int(round(ghost_radius_deg * scale * Fs))), 1, -1)
    return m.astype(bool)


def exclusion(station, t=None, radius_deg=DEFAULT_RADIUS_DEG, shape=None,
              flare_half_width_deg=DEFAULT_GHOST_RADIUS_DEG):
    # flare_half_width_deg is the GHOST RADIUS knob (name kept for callers);
    # 0 disables the whole flare model
    """(excluded, info): excluded = full-res bool (True = drop the pixel) of the
    sun zone UNION the flare corridor, or None when nothing is excluded; info
    = sun alt/az, in_fov, x/y, fractions, plus the two maps ("sun_map",
    "flare_map") for display."""
    st = _load(station)
    if not st:
        return None, None
    t = time.time() if t is None else t
    key = (station.id, int(t // 30), round(float(radius_deg), 2),
           round(float(flare_half_width_deg), 2), shape)
    hit = _CACHE.get(key)
    if hit:
        return hit
    sa = sun_altaz(station, t)
    info = {"alt": sa["alt"], "az": sa["az"], "radius_deg": float(radius_deg),
            "in_fov": False, "x": None, "y": None, "frac": 0.0, "min_sep_deg": None,
            "sun_map": None, "flare_map": None, "flare_frac": 0.0}
    excl = None
    if sa["alt"] > -radius_deg:
        ang = separation_map(station, sa)
        info["min_sep_deg"] = float(ang.min())
        info["in_fov"] = bool(ang.min() < 1.0)           # sun direction inside the grid
        h, w = shape or st["shape"]
        if ang.min() < FLARE_MAX_SEP_DEG + 30.0:
            info["x"], info["y"] = _sun_pixel(station, sa, t)   # may be off-frame
        coarse = ang <= radius_deg
        if coarse.any():
            excl = cv2.resize(coarse.astype(np.uint8), (w, h),
                              interpolation=cv2.INTER_NEAREST).astype(bool)
            info["sun_map"] = excl
        if (flare_half_width_deg > 0 and info["x"] is not None
                and ang.min() < FLARE_MAX_SEP_DEG):
            fl = flare_map(station, info["x"], info["y"], (h, w), flare_half_width_deg,
                           sun_sep_deg=float(ang.min()))
            if fl.any():
                info["flare_map"] = fl
                info["flare_frac"] = float(fl.mean())
                excl = fl if excl is None else (excl | fl)
        if excl is not None:
            info["frac"] = float(excl.mean())
    if len(_CACHE) > 24:                 # entries hold full-res maps: keep it small
        _CACHE.clear()
    _CACHE[key] = (excl, info)
    return excl, info


def axis_blobs(station, img, keep, sa, t, level=235, min_area=15):
    """Bright blobs (>= level, unmasked) with their (k, perp_deg) relative to
    the sun-centre axis -- for finding a lens's ghost positions."""
    pp = _load(station)["pp"]
    cx, cy = optical_centre(station)
    sx, sy = _sun_pixel(station, sa, t)
    d = math.hypot(sx - cx, sy - cy)
    if d < 1:
        return []
    ux, uy = (sx - cx) / d, (sy - cy) / d
    y = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    k = np.ones(y.shape, bool) if keep is None else keep
    n, lab, stats, cents = cv2.connectedComponentsWithStats(((y >= level) & k).astype(np.uint8), 8)
    out = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        bx, by = cents[i]
        kk = ((bx - cx) * ux + (by - cy) * uy) / d
        perp = abs((bx - cx) * uy - (by - cy) * ux) / float(pp.F_scale)
        out.append({"area": area, "x": float(bx), "y": float(by), "k": float(kk), "perp_deg": float(perp)})
    return sorted(out, key=lambda b: -b["area"])


# ---------------------------------------------------------------------------
# Measuring the glare radius on real frames
def _day_frames(station, n):
    if not station.data_dir:
        return []
    root = os.path.join(station.data_dir, "FramesFiles")
    days = sorted(glob.glob(os.path.join(root, "[0-9]" * 4, "*")))[-2:]
    files = []
    for d in days:
        files += glob.glob(os.path.join(d, "**", station.id + "_*_d.png"), recursive=True)
    return sorted(files)[-n:]


def measure(station, n_frames=3, max_deg=40, step=2, clip_level=250, clip_thresh=0.005):
    """Print mean luma / clipped fraction per annulus around the sun on the
    newest day frames with the sun above the horizon; suggest a radius."""
    from podcontrol.frames import frame_capture_time
    if not available(station):
        print("%s: no platepar -> cannot locate the sun" % station.id); return None
    suggested = []
    for f in _day_frames(station, n_frames):
        t = frame_capture_time(f) or os.path.getmtime(f)
        sa = sun_altaz(station, t)
        if sa["alt"] < 0:
            continue
        ang = separation_map(station, sa)
        img = cv2.imread(f)
        if img is None:
            continue
        y = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)[::GRID_STEP, ::GRID_STEP]
        gh, gw = ang.shape
        y = y[:gh, :gw]
        xy = _sun_pixel(station, sa, t)
        print("%s  sun alt=%.1f az=%.1f -> px (%.0f,%.0f) %s  min sep %.1f deg" % (
            os.path.basename(f), sa["alt"], sa["az"], xy[0], xy[1],
            "IN FOV" if ang.min() < 1.0 else "outside", ang.min()))
        try:
            from podcontrol.frames import static_mask_for
            keep = static_mask_for(station, img)
            sun_excl = cv2.resize((ang <= DEFAULT_RADIUS_DEG).astype(np.uint8), (img.shape[1], img.shape[0]),
                                  interpolation=cv2.INTER_NEAREST).astype(bool)
            keep = ~sun_excl if keep is None else (keep & ~sun_excl)
            for b in axis_blobs(station, img, keep, sa, t)[:6]:
                print("   bright blob %5d px at (%4.0f,%4.0f)  k=%+.2f  %4.1f deg off axis%s" % (
                    b["area"], b["x"], b["y"], b["k"], b["perp_deg"],
                    "  <- ON AXIS (flare ghost?)" if b["perp_deg"] < 3 else ""))
        except Exception as e:
            print("   (axis analysis skipped: %s)" % e)
        last_clip = None
        for r in range(0, max_deg, step):
            sel = (ang >= r) & (ang < r + step)
            if not sel.any():
                continue
            clip = float((y[sel] >= clip_level).mean())
            print("   %2d-%2d deg: n=%6d  mean=%3.0f  clipped=%.4f" % (r, r + step, sel.sum(), y[sel].mean(), clip))
            if clip > clip_thresh:
                last_clip = r + step
        if last_clip is not None:
            suggested.append(last_clip + step)
            print("   -> clipping (>%.1f%%) reaches %d deg; suggest radius >= %d deg" % (
                100 * clip_thresh, last_clip, last_clip + step))
        else:
            print("   -> no clipped annulus above %.1f%%" % (100 * clip_thresh))
    if suggested:
        print("suggested radius for %s: %d deg (max over %d frames)" % (station.id, max(suggested), len(suggested)))
    return max(suggested) if suggested else None


if __name__ == "__main__":
    import argparse
    from podcontrol.stations import get_pod
    ap = argparse.ArgumentParser(description="Sun exclusion mask tools.")
    ap.add_argument("--where", action="store_true", help="sun position in every camera now")
    ap.add_argument("--measure", metavar="STATION", help="glare profile around the sun on recent frames")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_DEG)
    args = ap.parse_args()
    pod = get_pod()
    if args.measure:
        st = next((s for s in pod if s.id == args.measure), None)
        if not st:
            raise SystemExit("unknown station %s (have %s)" % (args.measure, [s.id for s in pod]))
        measure(st, n_frames=args.frames)
    else:
        now = time.time()
        for s in pod:
            excl, info = exclusion(s, now, args.radius)
            if info is None:
                print("%-7s no platepar" % s.id); continue
            print("%-7s sun alt %5.1f az %5.1f  %s  excluded %.1f%% (r=%.0f deg)%s" % (
                s.id, info["alt"], info["az"],
                "IN FOV px (%.0f,%.0f)" % (info["x"], info["y"]) if info["in_fov"] else "outside (sep %.0f deg)" % (info["min_sep_deg"] or -1),
                100 * info["frac"], args.radius, ""))
