"""Camera-side metering: the ISP's own statistics instead of RMS's saved frames.

Used by the controller when "camera meter" is on (meter_set_hybrid): every camera that has
the commands is metered here, the others by the frame meter. Off by default.

Two read-only commands on the camera's control server (:9600), a few ms each, measured
on the frame just captured, in linear light:
  ae_stats  15x17 zone means (green), 0-65535 linear          -> LEVEL
  wb_stats  32x32 zone counts: fraction (65535 = 100%) of the zone's pixels at or below
            the white level (and inside the WB colour gates)    -> CLIPPING
With the white level at WHITE (98% of full scale) a zone's clipped fraction is
1 - count/65535.

Only CLEAN zones count: zones that touch no masked pixel, static RMS mask or the dynamic
sun/moon/flare exclusion (the same mask the frame meter uses). On the 20x pod that keeps
~87-92% of each camera's unmasked pixels (2026-09-28).

What it cannot do that the frame meter does (so it is a candidate for the fast loop, not
a replacement): point-source tolerance (a clipped star counts), clipping CAUSED BY THE WB
GAINS (the statistics are before white balance), a strongly coloured source (outside the
WB colour gates it reads as clipped), and the grids assume the camera does not flip or
mirror (the fleet runs FLIP=0 MIRROR=0).

    python -m podcontrol.camera_meter US05E1        # compare with the frame meter, live
"""
import math
import time

import numpy as np

from podcontrol.podctl import send

WHITE = 64000                    # wb_stats white level: 98% of linear full scale
LEVEL_CAL = 0.9614               # frame-meter mean / camera mean: 290 daylight pairs on US05E1,
                                 # 2026-09-28, p10-p90 0.958-0.964, flat from mean 60 to 200
                                 # (zone gamma of the mean vs mean of gamma, green vs luma)
RB_MARGIN = 0.80                 # a zone whose post-WB red or blue MEAN is above this fraction of
                                 # full scale is counted as at risk of gain clipping (zone means
                                 # hide the brightest pixels; the AE stats have no per-channel
                                 # histogram on the GK7205V200)
ZONE_PX = 60 * 34                # pixels in one WB statistics zone (32x32 over 1920x1080)
MASK_TTL_S = 30.0                # clean-zone grids are recomputed this often (the sun mask moves
                                 # ~0.1 deg in 30 s; computing it costs ~1.4 s per camera)
FULL = 65535.0
H, W = 1080, 1920

# AE grid, read from `ae_stats full` on the Goke (uniform, 113 x 72 px)
AE_XE = [0, 113, 226, 339, 452, 565, 678, 791, 904, 1017, 1130, 1243, 1356, 1469, 1582, 1695, 1808, 1920]
AE_YE = [0, 72, 144, 216, 288, 360, 432, 504, 576, 648, 720, 792, 864, 936, 1008, 1080]


def _uniform(n, size):
    return [int(round(i * size / n)) for i in range(n + 1)]


def _grid(text, key):
    for line in (text or "").splitlines():
        if line.startswith(key + " "):
            return np.array([[int(x) for x in r.split(",")] for r in line.split(" ", 1)[1].split(";")], float)
    return None


def _edges(text, n_rows, n_cols):
    """WB grid edges from the Goke `grid ... x=.. y=..` line; uniform otherwise (CV300)."""
    for line in (text or "").splitlines():
        if line.startswith("grid "):
            kv = dict(t.split("=", 1) for t in line.split()[1:] if "=" in t)
            try:
                xe = [int(v) for v in kv["x"].split(",")]
                ye = [int(v) for v in kv["y"].split(",")]
                if len(xe) == n_cols + 1 and len(ye) == n_rows + 1:
                    xe[-1], ye[-1] = W, H          # last edge is inclusive (1919/1079)
                    return xe, ye
            except (KeyError, ValueError):
                pass
    return _uniform(n_cols, W), _uniform(n_rows, H)


def clean_zones(keep, xe, ye):
    """Boolean grid: True where every pixel of the zone counts (keep = True)."""
    if keep is None:
        return np.ones((len(ye) - 1, len(xe) - 1), bool)
    return np.array([[bool(keep[ye[r]:ye[r + 1], xe[c]:xe[c + 1]].all()) for c in range(len(xe) - 1)]
                     for r in range(len(ye) - 1)])


class CameraMeter(object):
    def __init__(self, station, white=WHITE):
        self.st = station
        self.white = white
        self._blank = np.zeros((H, W, 3), np.uint8)

    def _ensure_white(self, wb_text):
        line = (wb_text or "").splitlines()[0] if wb_text else ""
        if "white=%d " % self.white not in line + " ":
            # persisted on the camera (boot replay); statistics config only, never the picture
            send(self.st.ip, "wb_stats set white %d" % self.white, timeout=5)
            return False
        return True

    def meter(self, t=None):
        """{mean, clip, n_ae, n_wb, t, ...} or None. mean: 0-255 gamma-0.5 luma equivalent
        of the clean zones' linear mean (comparable with the frame meter's 'mean');
        clip: clipped fraction over the clean WB zones."""
        from podcontrol import frames
        t = time.time() if t is None else t
        ae_t = send(self.st.ip, "ae_stats", timeout=5)
        wb_t = send(self.st.ip, "wb_stats", timeout=5)
        ae, cnt = _grid(ae_t, "zone_g"), _grid(wb_t, "zone_count")
        if ae is None or cnt is None:
            return None
        white_ok = self._ensure_white(wb_t)
        keep = frames.mask_for(self.st, self._blank, t)
        ae_ok = clean_zones(keep, AE_XE, AE_YE)
        xe, ye = _edges(wb_t, cnt.shape[0], cnt.shape[1])
        wb_ok = clean_zones(keep, xe, ye)
        if not ae_ok.any() or not wb_ok.any():
            return None
        y_z = 255.0 * np.sqrt(np.clip(ae[ae_ok], 0, FULL) / FULL)     # per-zone luma equivalent
        clip_z = 1.0 - cnt[wb_ok] / FULL
        hdr = dict(x.split("=", 1) for x in next(l for l in ae_t.splitlines() if l.startswith("ae_stats ")).split()[1:] if "=" in x)
        return {"mean": float(y_z.mean()), "clip": float(clip_z.mean()), "clip_zone_max": float(clip_z.max()),
                "n_ae": int(ae_ok.sum()), "n_wb": int(wb_ok.sum()), "white_ok": white_ok, "t": t,
                "exp_us": int(hdr.get("exp_us", 0)), "again": int(hdr.get("again", 0)), "src": "camera"}


_CLEAN = {}          # station id -> (t, key, ae_ok, wb_ok, sun_in_fov)


def _clean_grids(st, t, wb_text, n_rows, n_cols):
    """(ae_ok, wb_ok, sun_in_fov) for station st at t, cached for MASK_TTL_S."""
    from podcontrol import frames
    key = (round(frames.SUN_RADIUS_DEG[0], 2), round(frames.MOON_RADIUS_DEG[0], 2),
           round(frames.FLARE_HALF_WIDTH_DEG[0], 2), n_rows, n_cols)
    hit = _CLEAN.get(st.id)
    if hit and hit[1] == key and abs(t - hit[0]) < MASK_TTL_S:
        return hit[2], hit[3], hit[4]
    keep, lay = frames.mask_for(st, np.zeros((H, W, 3), np.uint8), t, layers=True)
    ae_ok = clean_zones(keep, AE_XE, AE_YE)
    xe, ye = _edges(wb_text, n_rows, n_cols)
    wb_ok = clean_zones(keep, xe, ye)
    sun = bool(((lay or {}).get("sun_info") or {}).get("in_fov"))
    _CLEAN[st.id] = (t, key, ae_ok, wb_ok, sun)
    return ae_ok, wb_ok, sun


def controller_stats(st, t=None):
    """The frame meter's fields for the controller (mean, clip, rb_only, peak, raw_sat,
    raw_sat_all, sun_in_fov, t), measured by the camera; None when the camera lacks the
    commands or a reading fails -- the caller falls back to the frame meter."""
    from podcontrol import frames
    t = time.time() if t is None else t
    ae_t = send(st.ip, "ae_stats full", timeout=5) or ""
    wb_t = send(st.ip, "wb_stats", timeout=5) or ""
    ae_hdr = next((l for l in ae_t.splitlines() if l.startswith("ae_stats ")), None)
    wb_hdr = next((l for l in wb_t.splitlines() if l.startswith("wb_cfg ")), None)
    if ae_hdr is None or wb_hdr is None:
        return None
    zg, zr, zb, cnt = _grid(ae_t, "zone_g"), _grid(ae_t, "zone_r"), _grid(ae_t, "zone_b"), _grid(wb_t, "zone_count")
    if zg is None or zr is None or zb is None or cnt is None:
        return None
    if "white=%d " % WHITE not in wb_hdr + " ":
        send(st.ip, "wb_stats set white %d" % WHITE, timeout=5)   # persisted; statistics only
    ae_ok, wb_ok, sun = _clean_grids(st, t, wb_t, cnt.shape[0], cnt.shape[1])
    if not ae_ok.any() or not wb_ok.any():
        return None
    # level: per-zone luma equivalent of the post-WB green mean, calibrated to the frame meter
    mean = LEVEL_CAL * float((255.0 * np.sqrt(np.clip(zg[ae_ok], 0, FULL) / FULL)).mean())
    # sensor saturation: fraction of pixels above the white level; zones with fewer clipped
    # pixels than one blob (the frame meter's point-source tolerance) do not count
    sat = 1.0 - cnt / FULL
    min_frac = frames.CLIP_MIN_BLOB_PX[0] / float(ZONE_PX)
    sat_c = sat[wb_ok]
    clip_sensor = float(np.where(sat_c >= min_frac, sat_c, 0.0).mean())
    raw_sat = float(sat_c.mean())
    raw_sat_all = float(sat.mean())
    # red/blue gain clipping risk: clean zones whose post-WB R or B mean is above the margin
    rb_zone = (np.maximum(zr, zb) >= RB_MARGIN * FULL) & ae_ok
    rb_only = float(rb_zone.sum()) / float(ae_ok.sum())
    # peak (0-255): the AE histogram's 99.9th percentile (green, post-WB, full scale = bin 963
    # after the black-level subtraction; 208 vs the frame meter's 210 on US05E1) or the
    # brightest clean zone mean in any channel, whichever is higher; 255 when a zone clips
    hdr = dict(x.split("=", 1) for x in ae_hdr.split()[1:] if "=" in x)
    zmax = np.maximum(np.maximum(zr, zg), zb)[ae_ok]
    peak = 255.0 * math.sqrt(min(1.0, float(zmax.max()) / FULL))
    try:
        peak = max(peak, 255.0 * math.sqrt(min(1.0, int(hdr["p999"]) / 963.0)))
    except (KeyError, ValueError):
        pass
    if clip_sensor > 0:
        peak = 255.0
    return {"mean": mean, "clip": max(clip_sensor, rb_only), "clip_raw": max(raw_sat, rb_only),
            "rb_only": rb_only, "raw_sat": raw_sat, "raw_sat_all": raw_sat_all, "peak": float(peak),
            "peak_luma": float(peak), "sun_in_fov": sun, "t": t, "src": "camera",
            "n_zones": int(ae_ok.sum()), "exp_us": int(hdr.get("exp_us", 0))}


def meter_set_hybrid(stations, allow_grab=False, wb_scale_fn=None):
    """Like frames.meter_set, but each camera that supports it is metered by the camera
    (current frame, t = now); the rest by the frame meter. Returns (met, newest_t)."""
    from podcontrol import frames
    met, newest, rest = {}, None, []
    for st in stations:
        try:
            m = controller_stats(st)
        except Exception:
            m = None
        if m is None:
            rest.append(st)
        else:
            met[st.id] = m
            newest = m["t"] if newest is None else max(newest, m["t"])
    if rest:
        fm, fslot = frames.meter_set(rest, allow_grab=allow_grab, wb_scale_fn=wb_scale_fn)
        for sid, m in fm.items():
            met[sid] = dict(m, src="frames")
        if fslot is not None:
            newest = fslot if newest is None else max(newest, fslot)
    return met, newest


def _compare(sid, period=5.0):
    """Print the camera meter next to the frame meter for each new RMS frame."""
    from podcontrol.stations import get_pod
    from podcontrol import frames
    st = next(s for s in get_pod() if s.id == sid)
    cm = CameraMeter(st)
    hist, last = [], None
    print("camera meter vs frame meter for %s (%s); Ctrl-C to stop" % (sid, st.ip), flush=True)
    while True:
        m = cm.meter()
        if m:
            hist.append(m)
            hist = hist[-60:]
        fr = frames.recent_rms_frames(st)
        if fr and fr[0][1] != last and hist:
            t, path = fr[0]
            last = path
            c = min(hist, key=lambda x: abs(x["t"] - t))
            f = frames.stats_for_path(st, path, t, 1.0) or {}
            if not f or abs(c["t"] - t) > 6:
                print("%s  skipped: frame meter %s, nearest camera reading %+.1f s" % (
                    time.strftime("%H:%M:%S", time.gmtime(t)), "ok" if f else "none", c["t"] - t), flush=True)
            else:
                print("%s  exp %6d again %5d | camera mean %6.1f clip %.4f (%d/%d zones) | frame mean %6.1f clip %.4f clip_raw %.4f" % (
                    time.strftime("%H:%M:%S", time.gmtime(t)), c["exp_us"], c["again"], c["mean"], c["clip"],
                    c["n_ae"], c["n_wb"], f.get("mean", float("nan")), f.get("clip", float("nan")),
                    f.get("clip_raw", float("nan"))), flush=True)
        time.sleep(period)


if __name__ == "__main__":
    import sys
    _compare(sys.argv[1] if len(sys.argv) > 1 else "US05E1")
