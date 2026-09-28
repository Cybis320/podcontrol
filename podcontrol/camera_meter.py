"""Camera-side metering: the ISP's own statistics instead of RMS's saved frames.

EXPERIMENTAL -- not wired into the controller. It exists to be compared, live, against the
frame meter (podcontrol.frames.luma_stats) before anything switches over.

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
        hdr = dict(x.split("=", 1) for x in (ae_t.splitlines()[0].split()[1:]) if "=" in x)
        return {"mean": float(y_z.mean()), "clip": float(clip_z.mean()), "clip_zone_max": float(clip_z.max()),
                "n_ae": int(ae_ok.sum()), "n_wb": int(wb_ok.sum()), "white_ok": white_ok, "t": t,
                "exp_us": int(hdr.get("exp_us", 0)), "again": int(hdr.get("again", 0)), "src": "camera"}


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
