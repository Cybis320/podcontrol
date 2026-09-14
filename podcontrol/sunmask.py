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

DEFAULT_RADIUS_DEG = 20.0
GRID_STEP = 8            # px between alt/az samples; upsampled nearest to the frame

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


def exclusion(station, t=None, radius_deg=DEFAULT_RADIUS_DEG, shape=None):
    """(excluded, info): excluded = full-res bool (True = drop the pixel) or
    None when nothing is excluded; info = sun alt/az, in_fov, x/y, fraction."""
    st = _load(station)
    if not st:
        return None, None
    t = time.time() if t is None else t
    key = (station.id, int(t // 30), round(float(radius_deg), 2), shape)
    hit = _CACHE.get(key)
    if hit:
        return hit
    sa = sun_altaz(station, t)
    info = {"alt": sa["alt"], "az": sa["az"], "radius_deg": float(radius_deg),
            "in_fov": False, "x": None, "y": None, "frac": 0.0, "min_sep_deg": None}
    excl = None
    if sa["alt"] > -radius_deg:
        ang = separation_map(station, sa)
        info["min_sep_deg"] = float(ang.min())
        info["in_fov"] = bool(ang.min() < 1.0)           # sun direction inside the grid
        if info["in_fov"]:
            info["x"], info["y"] = _sun_pixel(station, sa, t)
        coarse = ang <= radius_deg
        if coarse.any():
            h, w = shape or st["shape"]
            excl = cv2.resize(coarse.astype(np.uint8), (w, h),
                              interpolation=cv2.INTER_NEAREST).astype(bool)
            info["frac"] = float(excl.mean())
    if len(_CACHE) > 128:
        _CACHE.clear()
    _CACHE[key] = (excl, info)
    return excl, info


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
