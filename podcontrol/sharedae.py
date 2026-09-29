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
    per frame, invisible in a 30 fps timelapse of 5 s frames;
  * two exceptions run at `slew_fast` (1 stop/cycle): the STARTUP phase after
    a takeover (until the pod first reaches its target) and a GROSS error --
    a frame more than `clip_gross` clipped may move the target up to
    `max_step_gross` stops at once, since a white frame only says "far too
    bright" and 16 stops at 0.5 per ~50 s set took 30 min (2026-09-16).

Cross-platform: the ladder is in stops; the secondary gain stage is the ISP
digital gain (-i) on BOTH platforms. The sensor digital gain (-d) is left at
unity: on the IMX291 it is inert, and on the Goke science config it is a
mode-independent invariant (sensor DGain 1x) -- the night config uses -i 2048.
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import math, re, time

from podcontrol import skybright


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
    slew_against = 0.015        # max stops per cycle for a move AGAINST the diurnal trend
                                # (less light while the sun sets, more while it rises):
                                # such moves are cloud transients more often than not,
                                # so they are taken slowly; a persistent change still
                                # gets there in minutes (a soft "high-water mark")
    clip_emergency = 0.01       # clipped fraction above which an against-trend
                                # reduction runs at the full slew (extended blow-out)
    # Fast regime (2026-09-16: the pod was handed the night line at 09:00
    # local; at 0.05 stop/cycle and 0.5 stop per frame set the shared AE
    # needed 30 min to bring the 16 stops back, every frame white meanwhile).
    slew_fast = 1.0             # max stops per cycle while STARTING UP (after a takeover,
                                # until the pod first reaches its target) or correcting a
                                # GROSS error; the smooth `slew` applies otherwise
    clip_gross = 0.10           # clipped fraction above which a frame is a gross error: it
                                # may move the target max_step .. max_step_gross at once
                                # (0.5 stop at 10 % clipped, 3 stops at 100 %)
    max_step_gross = 3.0        # stops a fully blown-out frame moves the target
    startup_calib_peak = 180.0  # startup only: while the 99.9th-pctile luma is below this the
                                # headroom step is CALIBRATED (log2 of ceiling/peak, up to
                                # max_step_gross) instead of kp * error; above it the normal
                                # law settles the last fraction of a stop
    startup_max_s = 600.0       # the startup phase ends when the pod first reaches a
                                # post-takeover target, or after this long
    # Sky prior (skybright). Metering is up to ~50 s stale, so on a moving sky
    # the pod lags by rate x latency however fast the slew is: measured 0.28
    # stop at sun -4.4 deg on 2026-09-24, growing. The prior says how much the
    # sky moved between a frame's capture and now (so a stale frame is read
    # correctly) and how far it will move this cycle (so the pod travels with
    # it). The ABSOLUTE level still comes only from the frames; the prior only
    # ever supplies a DIFFERENCE, capped, and never acts while latched.
    sky_feedforward = True
    ff_max_step = 0.25          # stops the prior may move the pod in one cycle
    night_switch_deg = -9.0     # RMS CaptureModeSwitcher SWITCH_HORIZON_DEG (colour/mono, _d/_n);
                                # RMS writes its night line here -- we re-pin and keep driving.
                                # Overwritten from RMS at import (see below).
    latch_deg = -12.0           # dusk: latch at the night line here or when the ladder reaches
                                # the top rung, whichever comes first (operator: -9 is still twilight)
    dawn_unlatch_deg = -12.0    # may unlatch once the sun is rising above this
    dusk_ramp_deg = 0.0         # >0: over the last N deg before latch_deg force the pod up to the
                                # night line so the latch has no jump; 0 = pure AE until the latch
    night_line = None           # RMS night exposure line, e.g. "manual -a 22924 -i 2048 -e 39941";
                                # the ladder's top rung is made identical to it
    sun_cam_votes = True        # EVERY unmasked pixel on EVERY camera counts
                                # (operator decision 2026-09-14): the sun camera
                                # votes like any other; the sun zone radius is
                                # the operator's lever. False = it follows only.
    history_s = 300.0           # how long we remember our applies (frame latency)
    wb_lever = True             # below the exposure floor, attenuate the WB gains
                                # (R,G,B together, balance kept): the gains (1.8-1.9x)
                                # are applied in 12-bit before demosaic, so clipping
                                # they cause is recoverable; raw (green) saturation
                                # is not, and the controller holds instead
    wb_min_scale = 0.25         # safety floor; the real bottom is 1/max(R,G,B) gains:
                                # once every channel is below 1.0x nothing gain-induced
                                # is left to recover, only uniform darkening of raw data
    wb_rung_magenta_ok = False  # True when RMS rebuilds raw-saturated day highlights
                                # (RMS day_highlight_rebuild): raw saturation then no
                                # longer stops or reverses the WB rung -- it keeps
                                # recovering gain-induced R/B clipping down to its floor
                                # (largest WB gain 1.0x) and holds once recovered
    wb_rung_raw_sat_max = 0.0002  # the rung is used only while raw (green) saturation is
                                # below this fraction: attenuated WB turns raw-saturated
                                # zones magenta (R 1.8s, G s, B 1.9s no longer clip to
                                # white), so with a blown-out halo we stay at s = 1

    def set_night_line(self, cmd):
        """Derive the ladder caps from RMS's night line so the top rung IS the
        night line (exp_max_us, analog max, ISP-digital max)."""
        if not cmd:
            return False
        m = dict(re.findall(r"-(a|d|i|e)\s+(\d+)", cmd))
        if "e" not in m or "a" not in m:
            return False
        self.night_line = cmd
        self.exp_max_us = int(m["e"])
        self.analog_max_x = int(m["a"]) / 1024.0
        self.boost_max_x = int(m.get("i", 1024)) / 1024.0
        return True


try:
    from podcontrol.sunmask import SWITCH_HORIZON_DEG as _RMS_SWITCH
    AEConfig.night_switch_deg = float(_RMS_SWITCH)
except Exception:
    pass


def _wb_ints_from_reply(text):
    """The x256 (R, G, B) a `wb` reply reports, or None. Every wb command --
    set or read -- echoes the resulting gains, so the reply is the camera's own
    confirmation and needs no extra round trip."""
    m = re.search(r"R=([\d.]+)\s+Gr=([\d.]+)\s+Gb=([\d.]+)\s+B=([\d.]+)", text or "")
    if not m:
        return None
    r, gr, _gb, b = (float(x) for x in m.groups())
    return (int(round(r * 256)), int(round(gr * 256)), int(round(b * 256)))


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
        self.wb_base = None                  # (R,G,B) multipliers the pod had at takeover
        self._applied_wb_scale = 1.0
        self._wb_target = None               # x256 triple every camera must hold
        self._wb_force = False               # a camera missed it: push again next cycle
        self.wb_unconfirmed = []             # cameras that did not echo the WB we sent
        self.latched = False                 # night: pinned at the night line, silent
        self.sun_alt = None                  # deg, updated by update_sun()
        self.sun_rising = None
        self.state = "day"                   # day | dusk | night | dawn (for display)
        self.driver = None                   # cam whose need set the target
        self.driver_why = None               # 'clipping' | 'headroom' | 'at target' | 'gross ...'
        self.startup = False                 # fast phase after a takeover (see step)
        self.fast = False                    # last step ran at slew_fast
        self.driver_li_f = None              # light index the driver's frame was captured at
        self._sun_t = 0.0                    # epoch of the last sun update
        self.sun_rate = 0.0                  # deg/s, smoothed, for dead-reckoning the sun
        self._ff_alt = None                  # sun altitude at the previous feed-forward

    # --- ladder geometry (all in stops above (exp_min, gain 1x)) -----------
    def _exp_stops(self):
        return math.log2(self.cfg.exp_max_us / self.cfg.line_us)

    def _max_li(self):
        c = self.cfg
        return self._exp_stops() + math.log2(c.analog_max_x) + math.log2(c.boost_max_x)

    def _min_li(self):
        """Ladder bottom: 0 = the exposure floor; below it the WB rung, down to
        the scale that brings the LARGEST WB gain to 1.0x (1/max gain)."""
        c = self.cfg
        if c.wb_lever and self.wb_base:
            return math.log2(max(c.wb_min_scale, 1.0 / max(self.wb_base)))
        return 0.0

    def wb_scale_for(self, li):
        return 2.0 ** min(0.0, li) if (self.cfg.wb_lever and self.wb_base) else 1.0

    def wb_ints(self, scale):
        """The exact x256 triple sent for a WB attenuation `scale`. Comparing
        against these integers rather than a percentage catches a single 1/256
        step, which is what a camera drifts by when it misses one push."""
        if not self.wb_base:
            return None
        R, G, B = self.wb_base
        return (int(round(R * scale * 256)), int(round(G * scale * 256)), int(round(B * scale * 256)))

    def wb_scale_at(self, t):
        li = self.li_at(t)
        return self.wb_scale_for(self.li if li is None else li)

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
        if li >= self._max_li() - 1e-6:
            # the top rung IS the night line (byte-identical hand-over to RMS)
            return int(c.exp_max_us), c.analog_max_x, c.boost_max_x
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
        self._ff_alt = None                  # no feed-forward across a takeover
        self.last = {}
        self.startup = True                  # converge fast from wherever we start
        self.t_seed = time.time()
        self.hist = [(self.t_seed, self.li)]
        self._applied_li = None
        return self.li

    def sun_alt_at(self, t):
        """Sun altitude at epoch t, dead-reckoned from the last update. Over a
        frame-latency window (under a minute) a straight line is well inside
        the accuracy the prior needs."""
        if self.sun_alt is None or not self._sun_t or t is None:
            return None
        return self.sun_alt + self.sun_rate * (t - self._sun_t)

    def sky_drift(self, t_from, t_to=None):
        """Stops the sky has moved between two epochs, per the prior. 0 when
        the prior is off, the sun is unknown, or we are latched (RMS owns the
        night and nothing here may move the pod)."""
        if not self.cfg.sky_feedforward or self.latched:
            return 0.0
        a0 = self.sun_alt_at(t_from)
        a1 = self.sun_alt if t_to is None else self.sun_alt_at(t_to)
        if a0 is None or a1 is None:
            return 0.0
        return skybright.li_delta(a0, a1)

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
        """Seed the ladder AND remember how to hand every camera back. At
        night (sun below the RMS switch) we latch immediately and touch
        nothing: RMS's night line is in place and is exactly our top rung."""
        self.restore = self.pod.snapshot(poll) if hasattr(self.pod, "snapshot") else None
        # wb_base must be the UNATTENUATED white balance, because everything the
        # rung does is relative to it. Reading it from the cameras breaks on any
        # restart that happens while the rung is engaged: the attenuated gains
        # become the new base, the AE then believes it is already at scale 1, it
        # never restores them, and the pod is left permanently darkened and
        # magenta -- with a second restart ratcheting it down again. That is what
        # happened on 2026-09-24: a restart at scale 0.566 turned 1.80/1.00/1.92
        # into 1.02/0.57/1.09 and called it unity.
        # RMS's own day line is the authoritative unattenuated value, so prefer
        # it and fall back to the cameras only when the station has none.
        self.wb_base = None
        for st in getattr(self.pod, "stations", []) or []:
            if not hasattr(st, "mode_colour_cmds"):
                continue
            for cmd in st.mode_colour_cmds("day", keys=("wb",)):
                p_ = cmd.split()
                if len(p_) == 4 and all(x.isdigit() for x in p_[1:]):
                    self.wb_base = tuple(int(x) / 256.0 for x in p_[1:])
                    break
            if self.wb_base:
                break
        if self.wb_base is None:
            for d in poll.values():
                wb = d.get("wb") or {}
                g = wb.get("gains") or []
                if d.get("online") and wb.get("op") == "manual" and len(g) >= 3 and min(g) > 0:
                    self.wb_base = (float(g[0]), float(g[1]), float(g[-1]))
                    break
        self._applied_wb_scale = 1.0
        li = self.seed(poll)
        if self.sun_alt is not None and self.sun_alt < self.cfg.night_switch_deg:
            self.li = self.target = self._max_li()
            self.latched = True
            self.startup = False                 # dawn must come down at the smooth slew
            self.state = "night"
            self._applied_li = self.li           # nothing to push
        else:
            # By day, push the base WB on the first apply even though the scale
            # has not "changed": the cameras may still be carrying an attenuation
            # from a previous run that died on the rung, and without this they
            # would keep it until RMS's next switch. At night RMS owns WB (it
            # sets `wb unity`), so nothing is forced there.
            self._wb_force = True
        return li

    def update_sun(self, alt_deg, rising):
        """Feed the sun altitude (deg) and whether it is rising; sets state.
        Also keeps a smoothed altitude rate so the sun can be dead-reckoned
        back to a frame's capture time without another ephemeris call."""
        now = time.time()
        if self._sun_t and now > self._sun_t and self.sun_alt is not None:
            r = (alt_deg - self.sun_alt) / (now - self._sun_t)
            self.sun_rate = r if not self.sun_rate else 0.8 * self.sun_rate + 0.2 * r
        self._sun_t = now
        self.sun_alt, self.sun_rising = alt_deg, bool(rising)
        c = self.cfg
        if self.latched:
            self.state = "night" if alt_deg < c.latch_deg else "dawn"
        elif alt_deg < 0:
            self.state = "dawn" if rising else "dusk"
        else:
            self.state = "day"

    def _dusk_ramp_target(self):
        """During the last dusk_ramp_deg before RMS's night switch, the light
        index the pod should be at so that it reaches the top rung exactly at
        the switch (linear in sun altitude from where the ramp starts)."""
        c = self.cfg
        if c.dusk_ramp_deg <= 0 or self.sun_alt is None or self.sun_rising:
            return None
        start = c.latch_deg + c.dusk_ramp_deg
        if self.sun_alt > start:
            self._ramp_from = None
            return None
        if getattr(self, "_ramp_from", None) is None:
            self._ramp_from = self.li        # where the AE was when the ramp began
        frac = min(1.0, max(0.0, (start - self.sun_alt) / c.dusk_ramp_deg))
        return self._ramp_from + frac * (self._max_li() - self._ramp_from)

    def _maybe_latch(self):
        """Latch at the night line: when the ladder reaches the top rung, or
        when RMS switches to night (sun below the switch and setting)."""
        c = self.cfg
        if self.latched or self.sun_alt is None:
            return False
        at_top = self.li >= self._max_li() - 1e-6
        deep = self.sun_alt < c.latch_deg and not self.sun_rising
        if at_top or deep:
            self.li = self.target = self._max_li()
            self.latched = True
            self.startup = False
            self.state = "night"
            return True
        return False

    def _maybe_unlatch(self, metering):
        """Dawn: unlatch once the sun is rising above dawn_unlatch_deg AND a
        fresh set asks for less light than the night line."""
        c = self.cfg
        if not self.latched or self.sun_alt is None:
            return False
        if not (self.sun_rising and self.sun_alt > c.dawn_unlatch_deg):
            return False
        needs = [n for n in (self._need(m) for m in metering.values() if m) if n is not None]
        if needs and min(n[0] for n in needs) < self.li - c.deadband:
            self.latched = False
            self.state = "dawn"
            return True
        return False

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
        at_floor = li_f < 0.1                  # 1 line and analog gain within ~7% of 1x
        if at_floor and self.cfg.wb_lever and self.wb_base:
            # at/below the exposure floor only WB attenuation is left: it fixes
            # gain-induced R/B clipping but not raw (green) saturation
            # raw saturation counts wherever it is in the frame (masked zones
            # included): the magenta it would take is visible there all the same
            rb, rs = m.get("rb_only", clip), max(m.get("raw_sat", 0.0), m.get("raw_sat_all", 0.0))
            if c.wb_rung_magenta_ok:
                # raw saturation is RMS's to repair; only gain-induced R/B clipping moves the rung.
                # Only the CLEAN sky decides whether we may brighten: saturation under a mask (the
                # sun zone) is excluded like every other masked pixel.
                rs = m.get("raw_sat", 0.0)
                if rb > c.clip_limit:
                    return min(li_f, 0.0) - min(c.max_step, max(0.05, rb * c.kp_clip)), "R/B gain clipping (WB rung)"
                if li_f < -1e-6 or rs > c.clip_limit:
                    # recovered (or only raw saturation left, which the rung cannot fix): hold
                    return min(li_f, 0.0), "at target (WB rung)" if li_f < -1e-6 else "raw-saturated at the floor"
            if rs > c.wb_rung_raw_sat_max:
                # raw-saturated zones would go magenta under attenuation: stay
                # (or go back) to s = 1 where they clip to white
                return max(0.0, li_f), ("raw-saturated: leaving WB rung" if li_f < -1e-6
                                        else "raw-saturated at the floor")
            if rb > c.clip_limit:
                return min(li_f, 0.0) - min(c.max_step, max(0.05, rb * c.kp_clip)), "R/B gain clipping (WB rung)"
            if rs > c.clip_limit:
                return max(0.0, li_f), "raw-saturated at the floor"
            if li_f < -1e-6 and peak >= c.peak_ceiling - 20:
                # on the rung with the clipping just gone: hold (hysteresis),
                # climbing back would only re-clip the R/B channels
                return li_f, "at target (WB rung)"
        if clip > c.clip_gross:
            # GROSS blow-out: a mostly-white frame only says "far too bright",
            # and trailing it max_step per ~50 s frame set is hopeless (30 min
            # for 16 stops on 2026-09-16). Step max_step at clip_gross rising
            # linearly to max_step_gross at 100 % clipped; monotone in the
            # clip fraction, so successive sets converge without pumping.
            frac = (clip - c.clip_gross) / max(1e-6, 1.0 - c.clip_gross)
            d = -min(c.max_step_gross, c.max_step + (c.max_step_gross - c.max_step) * frac)
            why = "gross clipping %.0f%%" % (100 * clip)
        elif clip > c.clip_limit:
            # AIM FOR 0% CLIP: any clipping -> less light. Scales with severity
            # (gentle near zero so it settles, hard when badly blown out).
            d, why = -min(c.max_step, max(0.05, clip * c.kp_clip)), "clipping"
        elif self.startup and peak < c.startup_calib_peak and mean < c.target_luma:
            # startup from a dark seed: a CALIBRATED jump toward the ceiling
            # (log2 of the ceiling/peak ratio, gamma taken as 1; the real
            # sensor gamma 0.5 makes the true need larger, so this never
            # overshoots) instead of kp * error per ~50 s frame set
            d = min(c.max_step_gross, math.log2(c.peak_ceiling / max(peak, 4.0)))
            why = "gross dark (peak %.0f)" % peak
        elif peak < c.peak_ceiling and mean < c.target_luma:
            # headroom below saturation AND not over-bright -> more light,
            # tapering as the peak nears the ceiling
            d, why = min(c.max_step, c.kp * (c.peak_ceiling - peak) / c.peak_ceiling), "headroom"
        else:
            d, why = 0.0, "at target"
        # The frame is up to ~50 s old: correct its target for how much the sky
        # has moved since it was captured, so the pod aims at the sky NOW.
        t_new = li_f + d + self.sky_drift(t)
        if t_new < 0.0 and c.wb_lever and self.wb_base:
            # about to enter the WB rung from above the floor: the same guard
            # as on the rung. Until 2026-09-16 only frames captured AT the
            # floor were checked, so heavily clipped frames from just above it
            # drove the target straight to the rung's bottom past the guard.
            rs = max(m.get("raw_sat", 0.0), m.get("raw_sat_all", 0.0))
            if rs > c.wb_rung_raw_sat_max and not c.wb_rung_magenta_ok:
                return 0.0, "raw-saturated: staying at the floor"
        return t_new, why

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
        # POD-WIDE magenta guard. The WB is shared, so the per-frame guard in
        # _need is not enough: a camera without a sun halo asks for the rung
        # (its clipping is gain-induced), darkest-need-wins takes it, and the
        # shared attenuation turns ANOTHER camera's raw-saturated halo magenta
        # (2026-09-17 11:50 local: C1/F1 halos at R 240 G 178 B 251 with the
        # pod at the rung bottom, driven by A1/B1/D1/E1). If any frame of the
        # set shows raw saturation, nobody may ask for less than the floor.
        c = self.cfg
        if c.wb_lever and self.wb_base and not c.wb_rung_magenta_ok:
            sat = {cam: max(m.get("raw_sat", 0.0), m.get("raw_sat_all", 0.0))
                   for cam, m in metering.items() if m}
            worst = max(sat, key=sat.get) if sat else None
            if worst is not None and sat[worst] > c.wb_rung_raw_sat_max:
                for cam in list(needs):
                    if needs[cam][0] < 0.0:
                        needs[cam] = (0.0, "raw-saturated on %s: floor, no WB rung" % worst)
                for cam in list(followers):
                    if followers[cam][0] < 0.0:
                        followers[cam] = (0.0, followers[cam][1])
        self.followers = followers
        if not needs:
            self.waiting += 1
            return None
        self.waiting = 0
        self.needs = needs
        self.driver = min(needs, key=lambda k: needs[k][0])
        self.driver_why = needs[self.driver][1]
        self.driver_li_f = self.li_at((metering.get(self.driver) or {}).get("t"))
        self.target = max(self._min_li(), min(needs[self.driver][0], self._max_li()))
        return self.target

    def step(self, metering):
        """One cycle: fold the frames into the target, then SLEW the pod one
        small step toward it. Always returns the state; 'changed' says whether
        apply() has something new to push."""
        c = self.cfg
        lums = [m["mean"] for m in metering.values() if m]
        clips = [m["clip"] for m in metering.values() if m]
        peaks = [m.get("peak") for m in metering.values() if m and m.get("peak") is not None]
        prev = self.li
        if self.latched:
            self._maybe_unlatch(metering)
        if self.latched:
            # night: pinned at the night line, silent (nothing can move the pod)
            exp, analog, boost = self._li_to_exp_gain(self.li)
            self.last = {"pod_lum": max(lums) if lums else None, "pod_clip": max(clips) if clips else None,
                         "pod_peak": max(peaks) if peaks else None, "d_stops": 0.0,
                         "reason": "latched at night line", "li": self.li, "target": self.target,
                         "to_go": 0.0, "exp_us": exp, "analog_x": analog, "boost_x": boost,
                         "total_gain_x": analog * boost, "driver": None, "driver_why": None,
                         "needs": {}, "changed": self._applied_li is None, "state": self.state}
            return self.last
        self.evaluate(metering)
        # FEED-FORWARD: travel with the sky so the slew only has to correct the
        # residual. Capped, and skipped entirely while latched or sunless.
        ff = 0.0
        if c.sky_feedforward and self.sun_alt is not None:
            if self._ff_alt is not None:
                ff = skybright.li_delta(self._ff_alt, self.sun_alt)
                ff = max(-c.ff_max_step, min(c.ff_max_step, ff))
                if ff:
                    self.li = max(self._min_li(), min(self.li + ff, self._max_li()))
            self._ff_alt = self.sun_alt
        target = self.target
        ramp = self._dusk_ramp_target()
        if ramp is not None and ramp > target:
            target = ramp                    # dusk: climb to the night line by the switch
        err = target - self.li
        # FAST regime: correcting a GROSS error -- the driver's frame was mostly
        # clipped, or (startup only) far too dark. Only these big calibrated
        # steps are worth taking at once: with frame sets ~50 s old, jumping to
        # a small step's target and waiting is no faster than creeping there,
        # so the smooth slew keeps the mild cases. The startup phase ends when
        # a frame taken at the current setting says "at target", or
        # startup_max_s after the takeover; a takeover at night never starts one.
        gross = bool(self.driver_why) and self.driver_why.startswith("gross")
        if gross and err > 0 and not self.startup:
            err = 0.0            # a blown-out frame is a lower bound on the excess, never a reason to climb
        if self.startup:
            lf = self.driver_li_f
            if (self.needs and not gross and abs(err) <= c.deadband
                    and lf is not None and abs(lf - self.li) <= c.deadband):
                self.startup = False         # a frame taken at THIS setting says: at target
            elif self.t_seed and time.time() - self.t_seed > c.startup_max_s:
                self.startup = False
        fast = gross
        self.fast = fast
        rate = c.slew_fast if fast else c.slew
        against = False
        if not fast and self.sun_rising is not None and abs(err) > 1e-3:
            # diurnal trend: sun rising -> the pod should need LESS light over
            # time; sun setting -> MORE. A move the other way is suspect.
            trend_up = not self.sun_rising
            against = (err > 0) != trend_up
            pod_clip = max(clips) if clips else 0.0
            # leaving the WB rung because a halo is raw-saturated is a magenta
            # fix, not a cloud transient: full slew even against the trend
            leaving_rung = self.li < 0 and err > 0 and (self.driver_why or "").startswith("raw-saturated")
            if leaving_rung:
                against = False
            if against and not (err < 0 and pod_clip > c.clip_emergency):
                rate = c.slew_against
        d = max(-rate, min(rate, err))
        if abs(err) < 1e-3:
            d = 0.0
        self._against = against and d != 0.0
        self.li = max(self._min_li(), min(self.li + d, self._max_li()))
        just_latched = self._maybe_latch()
        exp, analog, boost = self._li_to_exp_gain(self.li)
        if just_latched:
            reason = "reached night line -> latched"
        elif ramp is not None and ramp > self.target and d > 0:
            reason = "dusk ramp to night line \u2191"
        elif d and fast:
            reason = ("slew\u2191" if d > 0 else "slew\u2193") + " (fast: %s)" % (
                "startup" if self.startup else "gross error")
        elif d and getattr(self, "_against", False):
            reason = ("slew\u2191" if d > 0 else "slew\u2193") + " (against trend, slow)"
        elif d:
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
                     "state": self.state, "ramp": ramp, "wb_scale": self.wb_scale_for(self.li),
                     "fast": fast, "startup": self.startup, "ff": ff,
                     "wb_unconfirmed": list(self.wb_unconfirmed),
                     "changed": abs(self.li - prev) > 1e-6 or self._applied_li is None or just_latched}
        return self.last

    def note_cameras(self, poll):
        """Call with each fresh poll, before step(). Notices RMS taking the pod
        back and gives up the latch so we start driving again.

        At the dawn switch (sun crossing -9 rising) RMS writes its day line,
        which is `auto`: control passes to each camera's own AE and the pod
        fans out, bounded by the camera's own route (44.8x here) rather than by
        the night-calibrated ceiling. We were still latched, and while latched
        repin_needed returned early, so nothing reclaimed the cameras -- the pod
        free-ran from 12:38 to 12:55 on 2026-09-26 with gains from 3.8x to 32x.

        Deep night is left alone deliberately. A camera in AUTO there is an
        anomaly (a reboot, say) and the right answer is to re-pin it to the
        night line, which is what staying latched makes apply() do. Only a
        handover at dawn should drop the latch."""
        if not self.latched:
            return False
        if not any(d.get("online") and (d.get("optype") or "").upper() == "AUTO"
                   for d in poll.values()):
            return False
        c = self.cfg
        if self.sun_alt is not None and not self.sun_rising and self.sun_alt < c.latch_deg:
            return False
        self.latched = False
        self.state = "dawn"
        self._ramp_from = None
        return True

    def repin_needed(self, poll, tol=0.06):
        """True if a camera no longer holds what we last applied: RMS's -9 deg
        night write (MANUAL, night line), its dawn `auto`, a reboot... The
        caller re-applies so at most one frame deviates. Compares op type AND
        live exposure/gain values."""
        if self._applied_li is None:
            return False
        # A camera in AUTO is not under our control whatever the latch state.
        # This test used to sit behind an early `if self.latched: return False`,
        # so the dawn `auto` this docstring names was the one case it could
        # never catch: at dawn we are latched by definition until metering
        # releases us. note_cameras() drops the latch first, and this keeps the
        # night case working, where re-pinning the night line is the right fix.
        for d in poll.values():
            if d.get("online") and (d.get("optype") or "").upper() == "AUTO":
                return True
        if self.latched:
            return False
        exp, analog, boost = self._li_to_exp_gain(self._applied_li)
        ws = self._applied_wb_scale
        for d in poll.values():
            if not d.get("online"):
                continue
            # While the rung is engaged, WB is ours and must be EXACT on every
            # camera: the old test allowed 5% on red, about thirteen 1/256 steps,
            # so a camera one or two steps behind never looked wrong and never got
            # corrected. Off the rung, WB belongs to RMS and we leave it alone;
            # a camera stranded at an old attenuation is then picked up by the
            # confirmed push that apply() makes on the way back to scale 1.0.
            if ws < 0.999 and self.wb_base and self._wb_target:
                g = (d.get("wb") or {}).get("gains") or []
                if len(g) >= 3:
                    got = (int(round(g[0] * 256)), int(round(g[1] * 256)), int(round(g[-1] * 256)))
                    if got != self._wb_target:
                        self._wb_force = True        # force the WB push on the next apply
                        return True
            e, a = d.get("exp_us"), d.get("again_x")
            if e is not None and abs(e - exp) > max(30, tol * exp):
                return True
            if a is not None and abs(a - analog) > tol * max(analog, 1.0):
                return True
        return False

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
        ws = self.wb_scale_for(self.li)
        if (self.wb_base and hasattr(self.pod, "wb_all")
                and (abs(ws - self._applied_wb_scale) > 0.01 or self._wb_force)):
            # CONFIRM EVERY CAMERA. The scale used to be marked applied the moment
            # the broadcast returned, without reading the per-camera replies, so a
            # camera that timed out kept an older attenuation for good and nothing
            # ever retried: on 2026-09-24 the pod sat on three different scales at
            # once (.206 two 1/256 steps behind .201), identical in balance and
            # visibly different in level. The daemon echoes the resulting gains,
            # so the reply confirms it; anything else is re-sent to that camera,
            # and if it still will not take, the push is repeated next cycle.
            ints = self.wb_ints(ws)
            res = self.pod.wb_all(*ints, timeout=timeout) or {}
            bad = [sid for sid, rep_ in res.items() if _wb_ints_from_reply(rep_) != ints]
            if bad and hasattr(self.pod, "one_live"):
                still = []
                for sid in bad:
                    try:
                        again = self.pod.one_live(sid, "wb %d %d %d" % ints, timeout=timeout)
                    except Exception:
                        again = None
                    if _wb_ints_from_reply(again) != ints:
                        still.append(sid)
                bad = still
            self._applied_wb_scale = ws
            self._wb_target = ints
            self._wb_force = bool(bad)
            self.wb_unconfirmed = bad
        self.t_apply = time.time()
        self._applied_li = self.li
        self.hist.append((self.t_apply, self.li))
        cutoff = self.t_apply - self.cfg.history_s
        while len(self.hist) > 2 and self.hist[1][0] < cutoff:
            self.hist.pop(0)
        return r

    def release(self, timeout=5.0):
        """Hand the cameras back exactly as they were (snapshot), never a bare
        'auto' (it resets the Goke AE ranges, e.g. sensor DGain max -> 126x).
        A WB attenuation in force is undone first."""
        if self._applied_wb_scale < 0.999 and self.wb_base and hasattr(self.pod, "wb_all"):
            R, G, B = self.wb_base
            self.pod.wb_all(int(round(R * 256)), int(round(G * 256)), int(round(B * 256)), timeout=timeout)
            self._applied_wb_scale = 1.0
        if hasattr(self.pod, "release"):
            return self.pod.release(self.restore, timeout=timeout)
        return self.pod.auto_all(timeout=timeout)


def configure_from_pod(ae, pod):
    """Make the ladder's top rung RMS's night line (from the first station
    with a camera_settings file)."""
    for st in getattr(pod, "stations", []):
        line = st.mode_cmd("night") if hasattr(st, "mode_cmd") else None
        if line and ae.cfg.set_night_line(line):
            return line
    return None


def feed_sun(ae, pod, t=None):
    """Update the controller with the sun altitude / rising flag from the
    first station that has a platepar. Returns (alt, rising) or None."""
    try:
        from podcontrol import sunmask
    except Exception:
        return None
    t = time.time() if t is None else t
    for st in getattr(pod, "stations", []):
        sa = sunmask.sun_altaz(st, t)
        if sa is None:
            continue
        sa2 = sunmask.sun_altaz(st, t + 600)
        ae.update_sun(sa["alt"], sa2["alt"] > sa["alt"])
        return sa["alt"], sa2["alt"] > sa["alt"]
    return None


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
    configure_from_pod(ae, pod)
    ae.takeover(pod.poll_all(timeout=4))     # start from where the cameras are
    try:
        while not stop():
            poll = pod.poll_all(timeout=4)
            feed_sun(ae, pod)
            m = {sid: v for sid, v in meter_fn().items() if poll.get(sid, {}).get("online")}
            ae.note_cameras(poll)            # RMS's dawn `auto` ends our control
            info = ae.step(m)
            if info["changed"] or ae.repin_needed(poll):
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
