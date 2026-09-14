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

Loop discipline (frames are late, output must be smooth): on a capturing pod
the frames come from RMS's saved blocks, up to ~50 s old and flushed at a
different moment per camera. Stepping on "whatever is fresh" made a different
camera drive every 5 s and the pod chased up and down. Instead:

  * every frame is used with the light index that was IN EFFECT when it was
    captured (a short history of our own applies), so a late frame yields an
    ABSOLUTE target ("at li 4.3 this camera needed -0.3 stop"), not a relative
    nudge that piles up while the loop is blind;
  * the pod target is the darkest need across cameras (highlight priority);
  * the pod SLEWS toward the target by at most `slew` stops per cycle (5 s,
    the RMS frame cadence): the default 0.05 stop is a 3.5% brightness change
    per frame, invisible in a 30 fps timelapse of 5 s frames.

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
    max_step = 0.5              # max stops a single frame may move the TARGET
    deadband = 0.15             # no change if |error| below this (stops)
    gamma = 0.5                 # sensor gamma: luma ~ light**gamma
    period_s = 5.0              # loop cadence = RMS frame cadence (timelapse frame)
    settle_s = 1.5              # an apply is in effect this long after it was sent
    slew = 0.05                 # max stops the POD moves per cycle (timelapse-smooth)
    sun_cam_votes = False       # a camera with the sun in its FOV takes the pod
                                # exposure but does not limit it (its unmasked
                                # glare ring otherwise pins the whole pod dark)
    history_s = 300.0           # how long we remember our applies (frame latency)


class SharedAE:
    def __init__(self, pod, cfg=None):
        self.pod = pod
        self.cfg = cfg or AEConfig()
        self.li = self._exp_stops() * 0.5   # start mid-exposure, gain=1x
        self.target = self.li                # where the pod is heading
        self.last = {}
        self.t_apply = 0.0                   # epoch of the last change pushed
        self.t_seed = 0.0                    # epoch of the takeover
        self.hist = []                       # [(epoch, li)] of our applies
        self.waiting = 0                     # cycles with no usable frame
        self.restore = None                  # {cam: cmd} taken at takeover
        self._applied_li = None
        self.needs = {}                      # cam -> (li_needed, why) from the last set
        self.followers = {}                  # cam -> (li_needed, why) not voting (sun in FOV)
        self.driver = None                   # cam whose need set the target
        self.driver_why = None               # 'clipping' | 'headroom' | 'at target'

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
        self.li = self.target = max(0.0, min(min(lis), self._max_li()))
        self.last = {}
        self.t_seed = time.time()
        self.hist = [(self.t_seed, self.li)]
        self._applied_li = None
        return self.li

    def li_at(self, t):
        """Light index in effect when a frame was captured at epoch t (our last
        apply that had settled by then), or None if before the takeover."""
        if t is None:
            return self.li
        if self.t_seed and t < self.t_seed:
            return None
        li = None
        for ta, l in self.hist:
            if ta + self.cfg.settle_s <= t:
                li = l
        return li if li is not None else (self.hist[0][1] if self.hist else self.li)

    def takeover(self, poll):
        """Seed the ladder AND remember how to hand every camera back."""
        self.restore = self.pod.snapshot(poll) if hasattr(self.pod, "snapshot") else None
        return self.seed(poll)

    # --- freshness (kept for callers; the controller no longer needs it) ----
    def fresh(self, metering):
        """Entries with a usable capture time (after the takeover) or none."""
        out = {k: m for k, m in metering.items()
               if m and (m.get("t") is None or self.li_at(m["t"]) is not None)}
        return out or None

    # --- controller --------------------------------------------------------
    def _need(self, m):
        """Stops of change THIS frame asks for, judged at the light index that
        was in effect when it was captured. None if the frame is unusable."""
        c = self.cfg
        t = m.get("t")
        li_f = self.li_at(t)
        if li_f is None:
            return None
        if t is not None and t < time.time() - c.history_s:
            return None
        clip, peak, mean = m["clip"], m.get("peak", m["mean"]), m["mean"]
        if clip > c.clip_limit:
            # AIM FOR 0% CLIP: any clipping -> less light. Scales with severity
            # (gentle near zero so it settles, hard when badly blown out).
            d, why = -min(c.max_step, max(0.05, clip * c.kp_clip)), "clipping"
        elif peak < c.peak_ceiling and mean < c.target_luma:
            # headroom below saturation AND not over-bright -> more light,
            # tapering as the peak nears the ceiling
            d, why = min(c.max_step, c.kp * (c.peak_ceiling - peak) / c.peak_ceiling), "headroom"
        else:
            d, why = 0.0, "at target"
        return li_f + d, why

    def evaluate(self, metering):
        """Update the pod target from whatever frames we have: the darkest need
        wins (highlight priority) and that camera is the DRIVER. Returns the
        target, or None if no frame was usable (target unchanged)."""
        needs, followers = {}, {}
        for cam, m in metering.items():
            if not m:
                continue
            r = self._need(m)
            if r is None:
                continue
            if m.get("sun_in_fov") and not self.cfg.sun_cam_votes:
                followers[cam] = (r[0], "sun in FOV, following")
            else:
                needs[cam] = r
        if not needs and followers:          # every camera sees the sun: vote anyway
            needs, followers = followers, {}
        self.followers = followers
        if not needs:
            self.waiting += 1
            return None
        self.waiting = 0
        self.needs = needs
        self.driver = min(needs, key=lambda k: needs[k][0])
        self.driver_why = needs[self.driver][1]
        self.target = max(0.0, min(needs[self.driver][0], self._max_li()))
        return self.target

    def step(self, metering):
        """One cycle: fold the frames into the target, then SLEW the pod one
        small step toward it. Always returns the state; 'changed' says whether
        apply() has something new to push."""
        c = self.cfg
        lums = [m["mean"] for m in metering.values() if m]
        clips = [m["clip"] for m in metering.values() if m]
        peaks = [m.get("peak") for m in metering.values() if m and m.get("peak") is not None]
        self.evaluate(metering)
        err = self.target - self.li
        d = max(-c.slew, min(c.slew, err))
        if abs(err) < 1e-3:
            d = 0.0
        prev = self.li
        self.li = max(0.0, min(self.li + d, self._max_li()))
        exp, analog, boost = self._li_to_exp_gain(self.li)
        if d:
            reason = "slew\u2191" if d > 0 else "slew\u2193"
        elif self.waiting and not self.needs:
            reason = "awaiting first post-takeover set"   # sets so far predate our control
        elif self.waiting:
            reason = "hold (no usable set)"
        else:
            reason = "hold"
        self.last = {"pod_lum": max(lums) if lums else None,
                     "pod_clip": max(clips) if clips else None,
                     "pod_peak": max(peaks) if peaks else None,
                     "d_stops": self.li - prev, "reason": reason, "li": self.li,
                     "target": self.target, "to_go": self.target - self.li,
                     "exp_us": exp, "analog_x": analog, "boost_x": boost,
                     "total_gain_x": analog * boost,
                     "driver": self.driver, "driver_why": self.driver_why,
                     "needs": {k: (v[0] - self.li, v[1]) for k, v in
                               list(self.needs.items()) + list(self.followers.items())},
                     "changed": abs(self.li - prev) > 1e-6 or self._applied_li is None}
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
        self._applied_li = self.li
        self.hist.append((self.t_apply, self.li))
        cutoff = self.t_apply - self.cfg.history_s
        while len(self.hist) > 2 and self.hist[1][0] < cutoff:
            self.hist.pop(0)
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
            info = ae.step(m)
            if info["changed"]:
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
    pod = PodController(get_pod())

    def meter():
        # the newest COMPLETE frame set (one capture instant for all cameras);
        # masked pixels (RMS mask + sun zone) never count
        from podcontrol.frames import meter_set
        met, slot = meter_set(pod.stations, allow_grab=False)
        return met

    def tick(info, poll):
        print("li=%.2f target=%.2f  lum=%s clip=%s  -> exp=%dus gain=%.2fx  (%s %+.3f)" % (
            info["li"], info["target"],
            "-" if info["pod_lum"] is None else "%.0f" % info["pod_lum"],
            "-" if info["pod_clip"] is None else "%.2f%%" % (100 * info["pod_clip"]),
            info["exp_us"], info["total_gain_x"], info["reason"], info["d_stops"]))

    print("shared-AE headless (Ctrl-C to stop, releases to auto)…")
    try:
        run(pod, meter, on_tick=tick)
    except KeyboardInterrupt:
        print("\nreleasing to auto")
