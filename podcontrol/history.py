"""Exposure history: a per-cycle log of what the pod did, and a canvas chart.

Records (JSON lines, one per app cycle) are appended to
~/.local/share/podcontrol/history.jsonl ($PODCONTROL_HISTORY overrides) and
the last `hours` are kept in memory, so a restart keeps the history. Each
record carries the shared-AE state (light index, target, exposure, gain,
state, driver), the pod metering (mean, peak, clip), the sun altitude and
each camera's own exposure/gain (so the chart is meaningful with Shared AE
off as well: the per-camera band shows how far apart the cameras' own AEs
sit).

draw_history() renders three strips on a tk.Canvas with no extra
dependencies:
  1. light index (stops above 1 line @ 1x): the pod (when driven) and the
     per-camera min/max band, the night-line top rung, latched/night shading,
     plus the sun altitude on a second axis;
  2. pod mean and 99.9th-percentile peak luma against the 234 ceiling;
  3. clipped fraction (%), with a tick in the driving camera's colour.
"""
import os, json, math, time
from collections import deque

LINE_US = 29.63
DEFAULT_PATH = os.path.expanduser("~/.local/share/podcontrol/history.jsonl")
CAM_COLORS = ["#f0a830", "#5ae0ff", "#7fc776", "#ff7355", "#c8a0ff", "#ffe066", "#ff9de2", "#9ad0a0"]


def cam_li(d):
    """Light index of a camera from its telemetry (exp x total gain)."""
    e = d.get("exp_us")
    if not e:
        return None
    g = (d.get("again_x") or 1.0) * (d.get("dgain_x") or 1.0) * (d.get("ispdgain_x") or 1.0)
    return math.log2(max(1.0, e / LINE_US) * max(1.0, g))


class HistoryLog:
    def __init__(self, path=None, hours=12.0):
        self.path = path or os.environ.get("PODCONTROL_HISTORY") or DEFAULT_PATH
        self.hours = hours
        self.records = deque()
        self._load()

    def _load(self):
        cutoff = time.time() - self.hours * 3600
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("t", 0) >= cutoff:
                        self.records.append(r)
        except OSError:
            pass

    def append(self, rec):
        rec = dict(rec)
        rec.setdefault("t", time.time())
        self.records.append(rec)
        cutoff = rec["t"] - self.hours * 3600
        while self.records and self.records[0]["t"] < cutoff:
            self.records.popleft()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass
        # keep the file from growing forever: rewrite it when it holds > 2x the window
        try:
            if os.path.getsize(self.path) > 2.5 * 1024 * 1024 and len(self.records) > 100:
                with open(self.path + ".tmp", "w") as f:
                    for r in self.records:
                        f.write(json.dumps(r, separators=(",", ":")) + "\n")
                os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass


def make_record(poll, info, metering, sun, ae_on, max_li=None, slot=None):
    """Build one history record from the app's per-cycle data."""
    cams = {}
    for sid, d in (poll or {}).items():
        if d.get("online"):
            cams[sid] = {"exp_us": d.get("exp_us"), "again_x": d.get("again_x"),
                         "dgain_x": d.get("dgain_x"), "ispdgain_x": d.get("ispdgain_x"),
                         "op": d.get("optype"), "li": cam_li(d)}
    lums = [m["mean"] for m in (metering or {}).values() if m]
    peaks = [m.get("peak") for m in (metering or {}).values() if m and m.get("peak") is not None]
    clips = [m["clip"] for m in (metering or {}).values() if m]
    rec = {"t": time.time(), "ae_on": bool(ae_on), "cams": cams,
           "lum": max(lums) if lums else None, "peak": max(peaks) if peaks else None,
           "clip": max(clips) if clips else None, "slot": slot,
           "sun_alt": sun[0] if sun else None, "max_li": max_li}
    if info:
        rec.update({"li": info.get("li"), "target": info.get("target"), "exp_us": info.get("exp_us"),
                    "gain": info.get("total_gain_x"), "state": info.get("state"),
                    "reason": info.get("reason"), "driver": info.get("driver"),
                    "why": info.get("driver_why")})
    return rec


# --------------------------------------------------------------------------
def draw_history(canvas, records, hours=12.0, now=None, cam_order=None, night_deg=-9.0):
    """Render the strips on a tk.Canvas (cleared first)."""
    canvas.delete("all")
    W = max(200, canvas.winfo_width()); H = max(200, canvas.winfo_height())
    now = now or time.time()
    t0 = now - hours * 3600
    recs = [r for r in records if r.get("t", 0) >= t0]
    L, R, T, B = 58, 58, 16, 30
    gap = 14
    ph = (H - T - B - 2 * gap) / 3.0
    panels = [(T + i * (ph + gap), T + i * (ph + gap) + ph) for i in range(3)]
    fg, dim, grid = "#c8bfa8", "#726650", "#2a2418"
    cams = cam_order or sorted({sid for r in recs for sid in (r.get("cams") or {})})
    colors = {sid: CAM_COLORS[i % len(CAM_COLORS)] for i, sid in enumerate(cams)}

    def X(t):
        return L + (t - t0) / (hours * 3600) * (W - L - R)

    # time grid: every hour
    first_hour = math.ceil(t0 / 3600) * 3600
    for panel_y0, panel_y1 in panels:
        canvas.create_rectangle(L, panel_y0, W - R, panel_y1, outline=grid, fill="#0c0a06")
    h = first_hour
    while h <= now:
        x = X(h)
        for panel_y0, panel_y1 in panels:
            canvas.create_line(x, panel_y0, x, panel_y1, fill=grid)
        canvas.create_text(x, H - B + 10, text=time.strftime("%H:%M", time.gmtime(h)), fill=dim, font=("JetBrains Mono", 8))
        h += 3600
    canvas.create_text(W - R, H - B + 22, text="UTC", fill=dim, anchor="e", font=("JetBrains Mono", 8))
    if not recs:
        canvas.create_text(W / 2, H / 2, text="no history yet", fill=dim, font=("JetBrains Mono", 14))
        return

    # night / latched shading on all panels
    def shade(pred, color):
        run = None
        for r in recs + [None]:
            on = bool(r and pred(r))
            if on and run is None:
                run = r["t"]
            elif not on and run is not None:
                for panel_y0, panel_y1 in panels:
                    canvas.create_rectangle(X(run), panel_y0, X((r or {"t": now})["t"]), panel_y1, fill=color, outline="")
                run = None
    shade(lambda r: r.get("sun_alt") is not None and r["sun_alt"] < night_deg, "#141a26")
    shade(lambda r: r.get("state") == "night", "#1a1430")
    shade(lambda r: r.get("ae_on"), "#10200f")

    # ---- panel 1: light index + sun altitude ----
    y0, y1 = panels[0]
    max_li = max([r.get("max_li") or 0 for r in recs] + [16.0])
    def Y1(li): return y1 - (li / max_li) * (y1 - y0)
    for v in range(0, int(max_li) + 1, 4):
        canvas.create_line(L, Y1(v), W - R, Y1(v), fill=grid); canvas.create_text(L - 6, Y1(v), text="%d" % v, fill=dim, anchor="e", font=("JetBrains Mono", 8))
    canvas.create_text(L - 6, y0 + 2, text="stops", fill=dim, anchor="ne", font=("JetBrains Mono", 8))
    top = [r.get("max_li") for r in recs if r.get("max_li")]
    if top:
        canvas.create_line(L, Y1(top[-1]), W - R, Y1(top[-1]), fill="#4a3f2a", dash=(3, 3))
        canvas.create_text(W - R - 4, Y1(top[-1]) - 6, text="night line", fill="#7a6a4a", anchor="e", font=("JetBrains Mono", 8))
    # per-camera band (own exposure)
    band_lo = [(r["t"], min(v["li"] for v in r["cams"].values() if v.get("li") is not None)) for r in recs if r.get("cams") and any(v.get("li") is not None for v in r["cams"].values())]
    band_hi = [(r["t"], max(v["li"] for v in r["cams"].values() if v.get("li") is not None)) for r in recs if r.get("cams") and any(v.get("li") is not None for v in r["cams"].values())]
    if len(band_lo) > 1:
        pts = [(X(t), Y1(v)) for t, v in band_lo] + [(X(t), Y1(v)) for t, v in reversed(band_hi)]
        canvas.create_polygon(*[c for p in pts for c in p], fill="#2c3a4a", outline="")
    # pod li when driven
    seg = []
    for r in recs:
        if r.get("ae_on") and r.get("li") is not None:
            seg.append((X(r["t"]), Y1(r["li"])))
        else:
            if len(seg) > 1: canvas.create_line(*[c for p in seg for c in p], fill="#7fc776", width=2)
            seg = []
    if len(seg) > 1: canvas.create_line(*[c for p in seg for c in p], fill="#7fc776", width=2)
    # sun altitude (right axis, -30..90)
    def Ys(a): return y1 - ((a + 30.0) / 120.0) * (y1 - y0)
    for a in (-9, 0, 30, 60):
        canvas.create_text(W - R + 6, Ys(a), text="%d°" % a, fill="#5a6a7a", anchor="w", font=("JetBrains Mono", 8))
    sp = [(X(r["t"]), Ys(r["sun_alt"])) for r in recs if r.get("sun_alt") is not None]
    if len(sp) > 1: canvas.create_line(*[c for p in sp for c in p], fill="#e8c060", dash=(2, 3))
    canvas.create_text(L + 6, y0 + 2, text="pod light index (green) · cameras' own (blue band) · sun altitude (dashed)", fill=fg, anchor="nw", font=("JetBrains Mono", 8))

    # ---- panel 2: luma ----
    y0, y1 = panels[1]
    def Y2(v): return y1 - (v / 255.0) * (y1 - y0)
    for v in (0, 64, 128, 192, 255):
        canvas.create_line(L, Y2(v), W - R, Y2(v), fill=grid); canvas.create_text(L - 6, Y2(v), text="%d" % v, fill=dim, anchor="e", font=("JetBrains Mono", 8))
    canvas.create_line(L, Y2(234), W - R, Y2(234), fill="#4a3f2a", dash=(3, 3))
    for key, col, w in (("peak", "#f0a830", 1), ("lum", "#c8bfa8", 2)):
        pts = [(X(r["t"]), Y2(r[key])) for r in recs if r.get(key) is not None]
        if len(pts) > 1: canvas.create_line(*[c for p in pts for c in p], fill=col, width=w)
    canvas.create_text(L + 6, y0 + 2, text="pod mean luma (white) · 99.9% peak (orange) · 234 ceiling", fill=fg, anchor="nw", font=("JetBrains Mono", 8))

    # ---- panel 3: clip % + driver ----
    y0, y1 = panels[2]
    cmax = max([r.get("clip") or 0 for r in recs] + [0.005])
    cmax = min(5.0, max(0.05, cmax * 100 * 1.2))
    def Y3(pct): return y1 - min(1.0, pct / cmax) * (y1 - y0)
    for v in (0, cmax / 2, cmax):
        canvas.create_line(L, Y3(v), W - R, Y3(v), fill=grid); canvas.create_text(L - 6, Y3(v), text="%.2f%%" % v, fill=dim, anchor="e", font=("JetBrains Mono", 8))
    pts = [(X(r["t"]), Y3(100 * r["clip"])) for r in recs if r.get("clip") is not None]
    if len(pts) > 1: canvas.create_line(*[c for p in pts for c in p], fill="#ff5ad6", width=1)
    for r in recs:
        if r.get("driver"):
            canvas.create_line(X(r["t"]), y1 - 2, X(r["t"]), y1 - 8, fill=colors.get(r["driver"], "#fff"))
    canvas.create_text(L + 6, y0 + 2, text="clipped fraction (magenta) · driver ticks: " + "  ".join(cams), fill=fg, anchor="nw", font=("JetBrains Mono", 8))
    lx = L + 6
    for sid in cams:
        canvas.create_rectangle(lx, y1 - 14, lx + 8, y1 - 6, fill=colors[sid], outline="")
        canvas.create_text(lx + 11, y1 - 10, text=sid, fill=colors[sid], anchor="w", font=("JetBrains Mono", 8))
        lx += 11 + 7 * len(sid) + 10
