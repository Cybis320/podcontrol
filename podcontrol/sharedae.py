"""Shared auto-exposure: drive a whole pod as one photometric instrument.

The app takes over AE from the cameras and runs one slow loop:

  meter every camera (frame mean-luma + clip fraction)
    -> the BRIGHTEST / most-clipped camera sets the ceiling
    -> nudge one exposure+gain and apply it to ALL cameras.

If any camera clips, everyone backs off (highlight priority). Control is on a
log "light ladder": exposure fills first (up to the ~40 ms frame cap), then
analog gain, then a digital boost -- so a single scalar spans day to night and
steps stay smooth. Adjustments are gentle (small stops/cycle, deadband) so it
tracks slow sky changes without hunting.

Loop discipline: a step is only taken on frames CAPTURED after the previous
change landed (each metering entry may carry its capture epoch as "t"). On a
capturing pod the frames come from RMS's saved blocks and can be ~50 s old;
stepping every few seconds on such frames cuts exposure ~10 times before
any effect is visible and rails the pod to the floor (seen live 2026-09-14).
With the gate, the loop runs at the frame cadence and stays stable.

Cross-platform: the ladder is in stops; the secondary gain stage is the ISP
digital gain (-i) on BOTH platforms. The sensor digital gain (-d) is left at
unity: on the IMX291 it is inert, and on the Goke science config it is a
mode-independent invariant (sensor DGain 1x) -- the night config uses -i 2048.
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import math, time


class AEConfig:
    line_us = 29.63             # sensor LINE time (25 fps, VMAX~1350) -- exposure is
                                # quantized to whole multiples of this on both platforms
    exp_max_us = 39941          # 1348 lines at 25 fps = the max both daemons accept
                                # (Goke auto range tops at 39970; night configs use 39941)
    analog_max_x = 22.0         # common-safe analog ceiling (Goke ~22x, IMX291 ~31x)
    boost_max_x = 16.0          # ISP-digital cap (IMX291 16x; Goke science night uses 2x)
    target_luma = 170.0         # mean cap so a flat/featureless scene isn't over-amplified
    clip_limit = 0.00005        # AIM FOR 0% CLIP: >~100 clipped px (>=CLIP_LEVEL) -> reduce
                                # (small floor ignores a handful of stuck hot pixels)
    peak_ceiling = 234.0        # brighten only while the 99.9th-pctile luma is below this
                                # (a margin under 250 so we approach but never cross into clip)
    kp_clip = 30.0              # clip-reduction gain (stops per unit clip fraction)
    kp = 0.6                    # proportional gain (stops per stop of error)
    max_step = 0.5              # max stops changed per cycle (gentle)
    deadband = 0.15             # no change if |error| below this (stops)
    gamma = 0.5                 # sensor gamma: luma ~ light**gamma
    period_s = 6.0              # loop cadence (a step still waits for fresh frames)
    settle_s = 1.5              # frames must be captured this long after an apply


class SharedAE:
    def __init__(self, pod, cfg=None):
        self.pod = pod
        self.cfg = cfg or AEConfig()
        self.li = self._exp_stops() * 0.5   # start mid-exposure, gain=1x
        self.last = {}
        self.t_apply = 0.0                   # epoch of the last change pushed
        self.waiting = 0                     # consecutive cycles held for fresh frames
        self.restore = None                  # {cam: cmd} taken at takeover

    # --- ladder geometry (all in stops above (exp_min, gain 1x)) -----------
    def _exp_stops(self):
        return math.log2(self.cfg.exp_max_us / self.cfg.line_us)

    def _max_li(self):
        c = self.cfg
        return self._exp_stops() + math.log2(c.analog_max_x) + math.log2(c.boost_max_x)

    def _li_to_exp_gain(self, li):
        """light_index (stops above 1 line @ 1x) -> (exp_us on a LINE boundary,
        analog_x, boost_x). Exposure is quantized to whole lines; GAIN fills the
        fractional line so total light stays finely controllable -- the sensor only
        honors line-granular exposure, so fine µs steps otherwise did nothing then
        jumped a whole line (the hunting you saw)."""
        c = self.cfg
        li = max(0.0, min(li, self._max_li()))
        target = c.line_us * (2 ** li)                 # desired exp_us * total_gain_x
        max_lines = max(1, round(c.exp_max_us / c.line_us))
        exp_cap = max_lines * c.line_us
        if target <= exp_cap:
            n = min(max_lines, max(1, int(target / c.line_us + 1e-9)))
            exp = n * c.line_us
        else:
            exp = exp_cap
        g = max(1.0, target / exp)                      # gain makes up the rest
        analog = min(g, c.analog_max_x)
        boost = min(c.boost_max_x, max(1.0, g / analog))
        return int(round(exp)), analog, boost

    # --- seeding -----------------------------------------------------------
    def seed(self, poll):
        """Start the ladder at the DARKEST current setting among the online
        cameras (poll = PodController.poll_all()). The camera whose own AE
        chose the least light sees the brightest scene, so starting there
        cannot blow anything out; the loop then brightens gently if allowed.
        Returns the seeded light index, or None if nothing usable."""
        c = self.cfg
        lis = []
        for d in poll.values():
            if not d.get("online") or not d.get("exp_us"):
                continue
            g = (d.get("again_x") or 1.0) * (d.get("ispdgain_x") or 1.0)
            lis.append(math.log2(max(1.0, d["exp_us"] / c.line_us) * max(1.0, g)))
        if not lis:
            return None
        self.li = max(0.0, min(min(lis), self._max_li()))
        self.last = {}
        return self.li

    def takeover(self, poll):
        """Seed the ladder AND remember how to hand every camera back."""
        self.restore = self.pod.snapshot(poll) if hasattr(self.pod, "snapshot") else None
        return self.seed(poll)

    # --- freshness gate ----------------------------------------------------
    def fresh(self, metering):
        """Subset of metering captured after the last apply (+settle). Entries
        without a "t" (direct grabs, sims) count as fresh. None if nothing is
        fresh yet -> the caller must HOLD (no step, no apply)."""
        need = self.t_apply + self.cfg.settle_s
        out = {k: m for k, m in metering.items()
               if m and (m.get("t") is None or m["t"] >= need)}
        if not out:
            self.waiting += 1
            return None
        self.waiting = 0
        return out

    # --- controller --------------------------------------------------------
    def step(self, metering):
        """metering: {cam_id: {'mean':.., 'clip':..} or None}. Returns info dict or None."""
        c = self.cfg
        lums = [m["mean"] for m in metering.values() if m]
        clips = [m["clip"] for m in metering.values() if m]
        if not lums:
            return None
        pod_lum = max(lums)          # brightest camera drives
        pod_clip = max(clips)
        peaks = [m.get("peak") for m in metering.values() if m and m.get("peak") is not None]
        pod_peak = max(peaks) if peaks else pod_lum
        if pod_clip > c.clip_limit:
            # AIM FOR 0% CLIP: any clipping -> reduce. Step scales with severity
            # (gentle near zero so it settles, hard when badly blown out).
            d = -min(c.max_step, max(0.05, pod_clip * c.kp_clip))
            reason = "clip\u2193"
        elif pod_peak < c.peak_ceiling and pod_lum < c.target_luma:
            # headroom below saturation AND not over-bright -> brighten gently,
            # slowing as the peak nears the ceiling so it never oversteps into clip
            room = (c.peak_ceiling - pod_peak) / c.peak_ceiling
            d = min(c.max_step, c.kp * room)
            reason = "brighten" if d > 0.01 else "hold"
        else:
            d, reason = 0.0, "hold"
        self.li = max(0.0, min(self.li + d, self._max_li()))
        exp, analog, boost = self._li_to_exp_gain(self.li)
        self.last = {"pod_lum": pod_lum, "pod_clip": pod_clip, "d_stops": d,
                     "reason": reason, "li": self.li, "exp_us": exp,
                     "analog_x": analog, "boost_x": boost,
                     "total_gain_x": analog * boost}
        return self.last

    def apply(self, platform="imx291", timeout=5.0):
        """Push the last-computed exp+gain to all cameras (platform-aware split)."""
        if not self.last:
            return None
        exp = int(self.last["exp_us"])
        analog = int(round(self.last["analog_x"] * 1024))
        boost = int(round(max(1.0, self.last["boost_x"]) * 1024))
        # pin ALL four stages: any stage left in AUTO keeps floating per camera
        # (sensor DGain went 1.0x..3.4x across the pod at the same -a/-e)
        kw = {"again": analog, "dgain": 1024, "ispdgain": boost, "exp_us": exp}
        r = self.pod.manual_all(timeout=timeout, **kw)
        self.t_apply = time.time()
        return r

    def release(self, timeout=5.0):
        """Hand the cameras back exactly as they were (snapshot), never a bare
        'auto' (it resets the Goke AE ranges, e.g. sensor DGain max -> 126x)."""
        if hasattr(self.pod, "release"):
            return self.pod.release(self.restore, timeout=timeout)
        return self.pod.auto_all(timeout=timeout)


def _pod_platform(poll):
    plats = [d.get("platform") for d in poll.values() if d.get("online")]
    for p in ("goke", "imx291"):
        if p in plats:
            return p
    return "imx291"


def run(pod, meter_fn, cfg=None, on_tick=None, stop=lambda: False):
    """Headless shared-AE loop. meter_fn() -> {cam:{mean,clip}}. Releases to auto on exit."""
    ae = SharedAE(pod, cfg)
    cfg = ae.cfg
    ae.takeover(pod.poll_all(timeout=4))     # start from where the cameras are
    try:
        while not stop():
            poll = pod.poll_all(timeout=4)
            m = {sid: v for sid, v in meter_fn().items() if poll.get(sid, {}).get("online")}
            m = ae.fresh(m)                  # hold until frames post-date the last change
            info = ae.step(m) if m else None
            if info:
                ae.apply(platform=_pod_platform(poll))
                if on_tick:
                    on_tick(info, poll)
            for _ in range(int(cfg.period_s * 10)):
                if stop():
                    break
                time.sleep(0.1)
    finally:
        ae.release()
    return ae


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    from podcontrol.podctl import PodController
    from podcontrol.frames import frame_for, luma_stats, mask_for
    from concurrent.futures import ThreadPoolExecutor
    pod = PodController(get_pod())
    pool = ThreadPoolExecutor(max_workers=8)

    def meter():
        sts = pod.stations
        got = dict(zip(sts, pool.map(lambda s: frame_for(s, with_time=True), sts)))
        out = {}
        for s, (img, src, t) in got.items():
            # masked pixels (RMS mask + sun zone) never count -- a lamp behind
            # the mask or the sun's glare cannot pull the pod's exposure down
            st = luma_stats(img, mask_for(s, img, t))
            if st is not None:
                st["t"] = t                  # capture epoch -> freshness gate
            out[s.id] = st
        return out

    def tick(info, poll):
        print("li=%.2f  lum=%.0f clip=%.1f%%  -> exp=%dus gain=%.1fx  (%s %+.2f)" % (
            info["li"], info["pod_lum"], info["pod_clip"] * 100, info["exp_us"],
            info["total_gain_x"], info["reason"], info["d_stops"]))

    print("shared-AE headless (Ctrl-C to stop, releases to auto)…")
    try:
        run(pod, meter, on_tick=tick)
    except KeyboardInterrupt:
        print("\nreleasing to auto")
