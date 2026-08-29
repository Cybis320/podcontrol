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

Cross-platform: the ladder is in stops; the secondary gain stage maps to the
right daemon knob per platform (IMX291 -> ISP-digital -i, Goke -> digital -d).
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import math, time


class AEConfig:
    line_us = 29.63             # sensor LINE time (25 fps, VMAX~1350) -- exposure is
                                # quantized to whole multiples of this on both platforms
    exp_max_us = 40000          # ~40 ms at 25 fps: meteor frame-time cap
    analog_max_x = 22.0         # common-safe analog ceiling (Goke ~22x, IMX291 ~31x)
    boost_max_x = 16.0          # secondary stage cap (IMX291 ISP-dig 16x; within Goke digital)
    target_luma = 140.0         # brighten UP TO this mean when well clear of clipping
    clip_limit = 0.002          # HIGHLIGHT PRIORITY: clip above this (0.2%) -> reduce
    kp_clip = 0.5               # clip-reduction gain (stops per octave of excess clip)
    kp = 0.6                    # proportional gain (stops per stop of error)
    max_step = 0.5              # max stops changed per cycle (gentle)
    deadband = 0.15             # no change if |error| below this (stops)
    gamma = 0.5                 # sensor gamma: luma ~ light**gamma
    period_s = 6.0              # loop cadence


class SharedAE:
    def __init__(self, pod, cfg=None):
        self.pod = pod
        self.cfg = cfg or AEConfig()
        self.li = self._exp_stops() * 0.5   # start mid-exposure, gain=1x
        self.last = {}

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
        ct = c.clip_limit
        if pod_clip > ct:
            # HIGHLIGHT PRIORITY: reduce whenever clipping exceeds the tolerance.
            # Log-proportional in the clip excess so it eases in and SETTLES at ct
            # (instead of holding at a fixed threshold or hunting).
            d = -min(c.max_step, c.kp_clip * math.log2(pod_clip / ct))
            reason = "clip\u2193"
        elif pod_lum < c.target_luma and pod_clip < ct * 0.5:
            # dark and well clear of clipping -> brighten toward the mean cap
            err = math.log2(c.target_luma / max(pod_lum, 1)) / c.gamma
            d = min(c.max_step, c.kp * err) if err > c.deadband else 0.0
            reason = "brighten" if d > 0 else "hold"
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
        boost = int(round(self.last["boost_x"] * 1024))
        kw = {"again": analog, "exp_us": exp}
        if self.last["boost_x"] > 1.001:
            if platform == "goke":
                kw["dgain"] = boost          # Goke big secondary = digital (-d)
            else:
                kw["ispdgain"] = boost       # IMX291 secondary = ISP-digital (-i)
        return self.pod.manual_all(timeout=timeout, **kw)

    def release(self, timeout=5.0):
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
    try:
        while not stop():
            poll = pod.poll_all(timeout=4)
            m = {sid: v for sid, v in meter_fn().items() if poll.get(sid, {}).get("online")}
            info = ae.step(m)
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
    from podcontrol.frames import frame_for, luma_stats
    from concurrent.futures import ThreadPoolExecutor
    pod = PodController(get_pod())
    pool = ThreadPoolExecutor(max_workers=8)

    def meter():
        sts = pod.stations
        imgs = dict(zip([s.id for s in sts], pool.map(lambda s: frame_for(s)[0], sts)))
        return {sid: luma_stats(img) for sid, img in imgs.items()}

    def tick(info, poll):
        print("li=%.2f  lum=%.0f clip=%.1f%%  -> exp=%dus gain=%.1fx  (%s %+.2f)" % (
            info["li"], info["pod_lum"], info["pod_clip"] * 100, info["exp_us"],
            info["total_gain_x"], info["reason"], info["d_stops"]))

    print("shared-AE headless (Ctrl-C to stop, releases to auto)…")
    try:
        run(pod, meter, on_tick=tick)
    except KeyboardInterrupt:
        print("\nreleasing to auto")
