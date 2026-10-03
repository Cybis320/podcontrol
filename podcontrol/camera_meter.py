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
RB_MARGIN = 0.72                 # a zone whose post-WB red or blue MEAN is above this fraction of
                                 # full scale is counted as at risk of gain clipping (zone means
                                 # hide the brightest pixels; no per-channel histogram on the
                                 # GK7205V200). 20x pod 2026-09-28: the two cameras with R/B
                                 # clipping in the frame had brightest zones at 0.76-0.78, the
                                 # four without at <= 0.65
PEAK_ZONE_FACTOR = 1.06          # frame-meter peak (max channel, p99.9) / brightest clean zone mean:
                                 # 0.99-1.11 over the six 20x cameras, median 1.044 (2026-09-28);
                                 # set a little high so the controller errs towards not brightening
MIN_CLIP_PX = 25                 # never count fewer saturated pixels than this in a zone: a glint
                                 # or a few hot pixels (US05C1: 4 and 8 px the frame meter never saw)
RB_ZONE_TO_PX = 0.01             # an at-risk zone counts as ~1% clipped pixels, so rb_only is on the
                                 # frame meter's pixel-fraction scale (the controller's clip step is
                                 # proportional to it: a raw zone fraction stepped up to 10x harder)
ZONE_PX = 60 * 34                # pixels in one WB statistics zone (32x32 over 1920x1080)
MASK_TTL_S = 30.0                # clean-zone grids are recomputed this often (the sun mask moves
                                 # ~0.1 deg in 30 s; computing it costs ~1.4 s per camera)
FULL = 65535.0
HIST_FULL = 1023                 # the AE histogram's top bin: 10-bit full scale
SAT_CAL = 2.34                   # frame-meter raw_sat_all / histogram top-bin fraction.
                                 # They count different things: the histogram counts RAW
                                 # pixels at full scale, the frame counts OUTPUT green at
                                 # 255, and gamma expands near-saturation so the frame's
                                 # set is the larger one. Measured 2026-10-03 on the two
                                 # saturated cameras over 8 samples: median 2.34, spread
                                 # 2.17-2.40. Thin (two cameras, one minute) but steady;
                                 # revisit if the gamma or the ISP gain changes.


def _hist_top(text):
    """Fraction of the frame in the histogram's top bin: raw sensor saturation.

    This replaces a count derived from the WB statistics' zone_count, which was
    wrong in both directions when checked against the pixels of the same frames
    on 2026-10-03 -- it invented 0.00121 on a camera with no saturated green
    pixel at all, and read 0.00000 on one where 4.2% of them were saturated. The
    invented reading is what crossed the 0.0002 threshold that gates the WB rung
    and stranded the pod with blown highlights it would not correct. The top bin
    agreed with the frames on all six cameras."""
    line = next((l for l in (text or "").splitlines() if l.startswith("hist ")), None)
    if not line:
        return None
    total = top = 0.0
    for pair in line[5:].split(","):
        if ":" not in pair:
            continue
        b, _, c = pair.partition(":")
        try:
            b, c = int(b), float(c)
        except ValueError:
            continue
        total += c
        if b >= HIST_FULL:
            top += c
    return (top / total) if total > 0 else None
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
    min_frac = max(frames.CLIP_MIN_BLOB_PX[0], MIN_CLIP_PX) / float(ZONE_PX)
    sat_c = sat[wb_ok]
    # zone_count still gives the only MASKED-AWARE saturation the camera has, so it
    # keeps driving `clip` and the peak-pins-at-255 rule, where counting a
    # streetlight under the mask would be a regression. It is no longer trusted
    # for raw saturation, which is what gates the WB rung.
    clip_sensor = float(np.where(sat_c >= min_frac, sat_c, 0.0).mean())
    top = _hist_top(ae_t)
    if top is None:
        return None                           # no histogram: fall back to the frame meter
    raw_sat = raw_sat_all = SAT_CAL * top     # whole frame; the camera cannot split it by mask
    # red/blue gain clipping risk: clean zones whose post-WB R or B mean is above the margin
    rb_zone = (np.maximum(zr, zb) >= RB_MARGIN * FULL) & ae_ok
    rb_only = RB_ZONE_TO_PX * float(rb_zone.sum()) / float(ae_ok.sum())
    # peak (0-255, max channel): the brightest CLEAN zone mean in any channel x PEAK_ZONE_FACTOR.
    # Not the AE histogram: it covers the whole frame, masked areas included (US05C1 read 255
    # from something under its mask while its sky peaked at 188). 255 when a zone clips.
    hdr = dict(x.split("=", 1) for x in ae_hdr.split()[1:] if "=" in x)
    zmax = np.maximum(np.maximum(zr, zg), zb)[ae_ok]
    peak = min(255.0, PEAK_ZONE_FACTOR * 255.0 * math.sqrt(min(1.0, float(zmax.max()) / FULL)))
    if clip_sensor > 0:
        peak = 255.0
    return {"mean": mean, "clip": max(clip_sensor, rb_only), "clip_raw": max(raw_sat, rb_only),
            "rb_only": rb_only, "raw_sat": raw_sat, "raw_sat_all": raw_sat_all, "peak": float(peak),
            "peak_luma": float(peak), "sun_in_fov": sun, "t": t, "src": "camera",
            "n_zones": int(ae_ok.sum()), "exp_us": int(hdr.get("exp_us", 0))}


# Fields the CAMERA may supply. Everything else comes from the frame, because
# the camera's own statistics proved unreliable in both directions on 2026-10-03:
# against the pixels of the same saved frames, it invented raw saturation of
# 0.00126 on a camera where no green pixel reached 255, and reported 0.00000 on
# one where 6.1% of them did. The frame meter matched the pixels to four decimals
# on every camera. That number gates the WB rung at a threshold of 0.0002, so an
# invented reading permanently disqualifies the one mechanism that recovers
# red/blue clipping, which is how the pod sat at 3.6% of pixels blown while the
# controller reported 0.00006 and asked to brighten.
#
# Level is different: it was calibrated against 290 daylight frame pairs
# (LEVEL_CAL) and it is fresher than any saved frame, so the camera keeps it.
# raw_sat_all joins `mean` now that it comes from the histogram top bin rather
# than zone_count: calibrated it agrees with the frame meter to within 3%
# (C1 0.01925 vs 0.01976, F1 0.04524 vs 0.04410) and the rung gate agrees on all
# six cameras, where the old path disagreed on three. It is worth taking fresh,
# because sensor saturation can appear in seconds when the sun breaks through
# cloud, and the frame it would otherwise be read from can be ~50 s old.
#
# Red/blue clipping is NOT here and cannot be: it is produced by the WB gains,
# downstream of where the camera's statistics are taken. On A1 the raw green
# 99.9th percentile sits at 186 of 255 while red and blue are pinned at full
# scale, so no camera-side measurement can see it.
CAMERA_FIELDS = ("mean", "raw_sat_all")


def meter_set_hybrid(stations, allow_grab=False, wb_scale_fn=None):
    """Frame metering, with the LEVEL taken from the camera where it can supply one.

    The frame is authoritative for peak, clipping and saturation, and its capture
    time is the sample's `t`: those are what the corrections are computed from,
    and SharedAE._need judges a sample at the light index in force when it was
    taken, so the timestamp has to belong to the measurements that drive it.

    A camera with no frame this cycle falls back to its own full reading rather
    than dropping out, since a stale opinion still beats none, and says so in
    `src` so the caller can tell them apart. Returns (met, newest_t)."""
    from podcontrol import frames
    fm, fslot = frames.meter_set(stations, allow_grab=allow_grab, wb_scale_fn=wb_scale_fn)
    met, newest = {}, fslot
    for st in stations:
        try:
            cam = controller_stats(st)
        except Exception:
            cam = None
        f = fm.get(st.id)
        if f is not None:
            m = dict(f, src="frames")
            if cam:
                for k in CAMERA_FIELDS:
                    if cam.get(k) is not None:
                        m[k] = cam[k]
                m["src"] = "frames+camera"
                m["mean_frames"] = f.get("mean")     # kept for the comparison tools
            met[st.id] = m
        elif cam:
            met[st.id] = dict(cam, src="camera")     # no frame: the camera alone
            newest = cam["t"] if newest is None else max(newest, cam["t"])
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
