"""Sun elevation strip for the side panel: the sun's altitude over a UTC day around now, the
twilight / switch altitudes as lines, and every crossing labelled with its UTC time.

The curve (145 points, ~1.4 s of ephemeris) is recomputed in a worker thread every
RECOMPUTE_S; the "now" marker and the current altitude are redrawn every minute. Events come
from the sampled curve by linear interpolation between 10-minute samples (well under a
minute of error at these slopes).
"""
import threading
import time

import tkinter as tk

HOURS_BEFORE, HOURS_AFTER = 12.0, 12.0
STEP_S = 600.0
RECOMPUTE_S = 600.0
ALT_LO, ALT_HI = -30.0, 90.0
SPLIT_LO = -24.0      # bottom of the (expanded) below-horizon part of the axis


class SunStrip(tk.Canvas):
    def __init__(self, parent, station, levels, bg="#1b1a17", fg="#c8bfa8", height=150, **kw):
        """levels: [(altitude_deg, label, colour)], e.g. [(0, "sunrise/set", ...), (-9, "RMS switch", ...)]."""
        super().__init__(parent, bg=bg, height=height, highlightthickness=0, **kw)
        self.station, self.levels, self.bgc, self.fgc = station, levels, bg, fg
        self._samples = None          # [(t, alt)]
        self._t_calc = 0.0
        self._busy = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.after(100, self._tick)

    # ---- data
    def _compute(self):
        from podcontrol import sunmask
        t0 = time.time() - HOURS_BEFORE * 3600.0
        n = int((HOURS_BEFORE + HOURS_AFTER) * 3600.0 / STEP_S) + 1
        s = []
        for i in range(n):
            t = t0 + i * STEP_S
            a = sunmask.sun_altaz(self.station, t)
            if a is not None:
                s.append((t, float(a["alt"])))
        self._samples, self._t_calc, self._busy = s, time.time(), False
        self.after(0, self.redraw)

    def _tick(self):
        if not self._busy and time.time() - self._t_calc > RECOMPUTE_S:
            self._busy = True
            threading.Thread(target=self._compute, daemon=True).start()
        self.redraw()
        self.after(60000, self._tick)

    def events(self):
        """[(t, altitude_level, label, rising)] crossings of every level in the window."""
        out, s = [], self._samples or []
        for lvl, lab, _ in self.levels:
            for (t1, a1), (t2, a2) in zip(s, s[1:]):
                if (a1 - lvl) * (a2 - lvl) < 0:
                    t = t1 + (lvl - a1) / (a2 - a1) * (t2 - t1)
                    out.append((t, lvl, lab, a2 > a1))
        return sorted(out)

    # ---- drawing
    def redraw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        L, R, T, B = 34, w - 6, 16, h - 50          # the bottom 50 px list the events
        s = self._samples
        if not s:
            self.create_text(w / 2, h / 2, text="computing the sun's path…", fill=self.fgc, font=("TkDefaultFont", 8))
            return
        ta, tb = s[0][0], s[-1][0]
        X = lambda t: L + (t - ta) / (tb - ta) * (R - L)
        # split axis: 0..peak on the upper 60%, SPLIT_LO..0 on the lower 40%, so the twilight and
        # switch lines (all within 18 deg below the horizon) do not crowd into a few pixels
        top = max(10.0, max(a for _, a in s) + 5.0)
        ymid = T + 0.6 * (B - T)

        def Y(a):
            a = max(SPLIT_LO, min(top, a))
            return (T + (top - a) / top * (ymid - T)) if a >= 0 else (ymid + a / SPLIT_LO * (B - ymid))
        # daylight / twilight / night background bands
        for lo, hi, col in ((0, top, "#2a3140"), (-18, 0, "#22252d"), (SPLIT_LO, -18, "#16161a")):
            self.create_rectangle(L, Y(hi), R, Y(lo), fill=col, outline="")
        # hour grid (UTC)
        t = (int(ta // 3600) + 1) * 3600
        while t < tb:
            hh = time.gmtime(t).tm_hour
            x = X(t)
            self.create_line(x, T, x, B, fill="#34322c" if hh % 6 else "#4a463c")
            if hh % 6 == 0:
                self.create_text(x, B + 2, text="%02d" % hh, fill="#8a8170", anchor="n", font=("TkDefaultFont", 7))
            t += 3600
        # level lines
        for lvl, lab, col in self.levels:
            y = Y(lvl)
            self.create_line(L, y, R, y, fill=col, dash=(3, 3))
            self.create_text(L - 3, y, text="%+g°" % lvl if lvl else "0°", fill=col, anchor="e", font=("TkDefaultFont", 7))
        # the curve
        pts = []
        for t, a in s:
            pts += [X(t), Y(a)]
        self.create_line(*pts, fill="#f0c040", width=2, smooth=True)
        # events: a dot on the curve; the times are listed below the plot (they bunch up
        # within an hour of sunset and sunrise, too close to label on the curve)
        col_of = {lvl: col for lvl, _, col in self.levels}
        evs = self.events()
        for t, lvl, lab, rising in evs:
            x, y = X(t), Y(lvl)
            self.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=col_of[lvl], outline="")
        # two rows: the next dusk (setting crossings) and the next dawn (rising), in time order
        now_ = time.time()
        for row, rising in enumerate((False, True)):
            seq = [e for e in evs if e[3] == rising and e[0] > now_]
            if not seq:
                seq = [e for e in evs if e[3] == rising]
            if not seq:
                continue
            first = seq[0][0]
            seq = [e for e in seq if e[0] - first < 4 * 3600][:5]
            y = B + 16 + row * 16
            x = 4
            lbl = self.create_text(x, y, text="dawn" if rising else "dusk", fill=self.fgc, anchor="w",
                                   font=("TkDefaultFont", 8, "bold"))
            x = self.bbox(lbl)[2] + 6
            for t, lvl, lab, _ in seq:
                it = self.create_text(x, y, text="%s %s" % (lab, time.strftime("%H:%M", time.gmtime(t))),
                                      fill=col_of[lvl], anchor="w", font=("TkDefaultFont", 8))
                x = self.bbox(it)[2] + 8
        # now
        now = time.time()
        if ta <= now <= tb:
            alt_now = min(s, key=lambda p: abs(p[0] - now))[1]
            x = X(now)
            self.create_line(x, T, x, B, fill="#ffffff", width=1)
            self.create_oval(x - 4, Y(alt_now) - 4, x + 4, Y(alt_now) + 4, outline="#ffffff", width=2)
        # caption: now + the next events
        nxt = [e for e in self.events() if e[0] > now][:2]
        cap = "sun %+.1f°  %s UTC" % (min(s, key=lambda p: abs(p[0] - now))[1], time.strftime("%H:%M", time.gmtime(now)))
        if nxt:
            cap += "   next: " + ", ".join("%s %s %s" % (lab, "↑" if r else "↓", time.strftime("%H:%M", time.gmtime(t)))
                                          for t, _, lab, r in nxt)
        self.create_text(4, 2, text=cap, fill=self.fgc, anchor="nw", font=("TkDefaultFont", 8))
