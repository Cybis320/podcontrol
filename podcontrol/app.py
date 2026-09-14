"""Pod Control -- desktop app to drive a camera pod (IMX291 or Goke) as one.

Live preview tiles + unified telemetry for every camera, Auto/Manual control,
the shared-AE engine and the WB cloud-gray calibrator. The frame source is
RMS-safe (reads RMS's saved frames while RMS captures; grabs only when RMS
is idle). Tiles scale with the window; the overlay shows what the metering
ignores (red = RMS mask, orange = sun zone) and what drives the exposure
(magenta = clipped pixels, cyan = the peak pixels of the driving camera;
violet = the lens-flare corridor along the sun-to-centre axis). The small
orange circle labelled "sun" only marks the computed sun position.

Run:  python -m podcontrol            (pod from ~/source/Stations if present,
                                       else 192.168.42.101-.106)
      python -m podcontrol --stations-dir ~/source/Stations
      python -m podcontrol --cameras 192.168.42.101-106
      python -m podcontrol --pod pod.json
"""

if __name__ == "__main__" and not __package__:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import time, threading, queue, os, signal
import tkinter as tk
import cv2
import numpy as np
from PIL import Image, ImageTk
from concurrent.futures import ThreadPoolExecutor

from podcontrol.stations import get_pod
from podcontrol.podctl import PodController
from podcontrol import frames as F
from podcontrol.frames import (frame_for, fresh_frame, luma_stats, mask_for, highlight_maps,
                               highlight_maps_cached, stats_for_path)
from podcontrol.sharedae import SharedAE, _pod_platform, configure_from_pod, feed_sun
from podcontrol.history import HistoryLog, make_record, draw_history
from podcontrol import settings as SETTINGS

COLS = 3
BG, PANEL = "#0f0d08", "#14110c"
SRC_COLOR = {"rms": "#7fc776", "stale": "#f0a830", "grab": "#5aa9e6", "none": "#726650"}
# translucent overlay tints (RGB)
TINT_STATIC, TINT_SUN = (220, 60, 60), (255, 190, 40)     # excluded zones
TINT_FLARE = (170, 110, 255)                              # flare corridor
TINT_MOON = (140, 200, 255)                               # moon zone
TINT_CLIP, TINT_HOT = (255, 0, 255), (0, 255, 255)        # what drives the AE
OVERLAY_ALPHA = 0.45
DRIVE_COLOR = {"clipping": "#ff5ad6", "headroom": "#5ae0ff", "at target": "#7fc776"}
MONO = "JetBrains Mono"


def _tint(small, mask_full, tint, alpha, size):
    m = cv2.resize(mask_full.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)
    if m.any():
        small[m] = ((1 - alpha) * small[m] + alpha * np.array(tint)).astype(np.uint8)


class Tile(tk.Frame):
    """One camera: a canvas that scales with its grid cell (16:9 image fitted
    and centred), a header (id, driving badge, frame source) and telemetry."""

    def __init__(self, master, station, on_select=None):
        super().__init__(master, bg=PANEL, bd=1, relief="solid")
        self.station = station
        self.on_select = on_select
        self.canvas = tk.Canvas(self, bg="#000", highlightthickness=0, cursor="crosshair",
                                width=320, height=180)
        self.canvas.pack(fill="both", expand=True)
        self.frame_wh = (1920, 1080)
        self._disp = (0, 0, 320, 180)       # where the image sits on the canvas
        self.sel = None                     # selection box in canvas coords
        self._img_id = self._rect_id = self._txt_id = None
        self._last = None                   # last render inputs (for redraw on resize)
        self._redraw_job = None
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Configure>", self._on_resize)
        head = tk.Frame(self, bg=PANEL); head.pack(fill="x", padx=6, pady=(2, 0))
        self.title = tk.Label(head, text=station.id, fg="#f0a830", bg=PANEL, font=(MONO, 11, "bold"))
        self.title.pack(side="left")
        self.drive = tk.Label(head, text="", fg="#ff5ad6", bg=PANEL, font=(MONO, 9, "bold"))
        self.drive.pack(side="left", padx=(10, 0))
        self.badge = tk.Label(head, text="", fg="#726650", bg=PANEL, font=(MONO, 8))
        self.badge.pack(side="right")
        self.tele = tk.Label(self, text="…", fg="#c8bfa8", bg=PANEL, font=(MONO, 9),
                             anchor="w", justify="left")
        self.tele.pack(fill="x", padx=6, pady=(0, 4))
        self._photo = None

    # --- geometry ------------------------------------------------------------
    def _fit(self):
        cw, ch = max(32, self.canvas.winfo_width()), max(18, self.canvas.winfo_height())
        fw, fh = self.frame_wh
        sc = min(cw / fw, ch / fh)
        dw, dh = max(1, int(fw * sc)), max(1, int(fh * sc))
        self._disp = ((cw - dw) // 2, (ch - dh) // 2, dw, dh)
        return self._disp

    def _on_resize(self, _e=None):
        if self._redraw_job:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(80, self._redraw)

    def _redraw(self):
        self._redraw_job = None
        if self._last:
            self.render(*self._last)

    # --- region selection (drag a box on a grey cloud) ---------------------
    def _press(self, e):
        self._x0, self._y0 = e.x, e.y

    def _drag(self, e):
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            self._x0, self._y0, e.x, e.y, outline="#f0a830", width=2)

    def _release(self, e):
        self.sel = (self._x0, self._y0, e.x, e.y)
        if self.on_select:
            self.on_select(self.station.id)

    def clear_selection(self):
        self.sel = None
        if self._rect_id:
            self.canvas.delete(self._rect_id); self._rect_id = None

    def frame_box(self):
        """Selected box in full-frame pixel coords, or None."""
        if not self.sel:
            return None
        ox, oy, dw, dh = self._disp
        fw, fh = self.frame_wh
        x0, y0, x1, y1 = self.sel
        def fx(x): return int(min(max(x - ox, 0), dw) * fw / dw)
        def fy(y): return int(min(max(y - oy, 0), dh) * fh / dh)
        return (fx(min(x0, x1)), fy(min(y0, y1)), fx(max(x0, x1)), fy(max(y0, y1)))

    # --- rendering -----------------------------------------------------------
    def render(self, img, source, tel, luma, layers=None, overlay=True, drive=None, sig=None):
        """drive: None, or (is_driver, why, need_stops) for the shared AE.
        sig: a hashable signature of the image content + overlay settings; when
        it matches the last render (and the canvas size did not change) the
        image is left alone and only the text is refreshed."""
        self._last = (img, source, tel, luma, layers, overlay, drive, None)
        is_driver, why, need = drive if drive else (False, None, None)
        same = (sig is not None and sig == getattr(self, "_sig", None)
                and img is not None and self._img_id is not None
                and (self.canvas.winfo_width(), self.canvas.winfo_height()) == getattr(self, "_sig_wh", None))
        self._sig = sig
        self._sig_wh = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if same or (img is None and self._img_id is not None):
            pass                                    # unchanged, or keep the last image
        elif img is None:
            if self._img_id:
                self.canvas.delete(self._img_id); self._img_id = None
            if not self._txt_id:
                cw, ch = max(32, self.canvas.winfo_width()), max(18, self.canvas.winfo_height())
                self._txt_id = self.canvas.create_text(cw // 2, ch // 2, text="no frame",
                                                       fill="#726650", font=(MONO, 16))
        else:
            if self._txt_id:
                self.canvas.delete(self._txt_id); self._txt_id = None
            self.frame_wh = (img.shape[1], img.shape[0])
            ox, oy, dw, dh = self._fit()
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            small = cv2.resize(rgb, (dw, dh), interpolation=cv2.INTER_AREA)
            if overlay and layers:
                for key, tint in (("static", TINT_STATIC), ("sun", TINT_SUN), ("flare", TINT_FLARE),
                                  ("moon", TINT_MOON)):
                    if layers.get(key) is not None:
                        _tint(small, layers[key], tint, OVERLAY_ALPHA, (dw, dh))
                if layers.get("clip") is not None:
                    _tint(small, layers["clip"], TINT_CLIP, 0.75, (dw, dh))
                if is_driver and layers.get("hot") is not None:
                    # the peak pixels (top 0.1 %) that decide headroom / at-target
                    _tint(small, layers["hot"], TINT_HOT, 0.75, (dw, dh))
                si = layers.get("sun_info")
                if si and si.get("in_fov") and si.get("x") is not None:
                    # the computed SUN position (centre of the exclusion zone);
                    # purely informational -- not an AE indicator
                    fw, fh = self.frame_wh
                    cx, cy = int(si["x"] * dw / fw), int(si["y"] * dh / fh)
                    r = max(4, dw // 60)
                    cv2.circle(small, (cx, cy), r, TINT_SUN, 2)
                    cv2.putText(small, "sun", (cx + r + 3, cy + 4), cv2.FONT_HERSHEY_SIMPLEX,
                                0.4, TINT_SUN, 1, cv2.LINE_AA)
                mi = layers.get("moon_info")
                if mi and mi.get("in_fov") and mi.get("x") is not None:
                    fw, fh = self.frame_wh
                    cx, cy = int(mi["x"] * dw / fw), int(mi["y"] * dh / fh)
                    r = max(4, dw // 60)
                    cv2.circle(small, (cx, cy), r, TINT_MOON, 2)
                    cv2.putText(small, "moon", (cx + r + 3, cy + 4), cv2.FONT_HERSHEY_SIMPLEX,
                                0.4, TINT_MOON, 1, cv2.LINE_AA)
            if is_driver:
                col = DRIVE_COLOR.get(why, "#ff5ad6")
                c = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
                cv2.rectangle(small, (0, 0), (dw - 1, dh - 1), c, 3)
            self._photo = ImageTk.PhotoImage(Image.fromarray(small))
            if self._img_id:
                self.canvas.itemconfig(self._img_id, image=self._photo)
                self.canvas.coords(self._img_id, ox, oy)
            else:
                self._img_id = self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
            if self._rect_id:
                self.canvas.tag_raise(self._rect_id)
        self.badge.config(text=source, fg=SRC_COLOR.get(source, "#726650"))
        if is_driver:
            self.drive.config(text="◀ DRIVING: %s" % (why or ""), fg=DRIVE_COLOR.get(why, "#ff5ad6"))
        elif why and why.startswith("sun in FOV"):
            self.drive.config(text="☀ following (no vote)", fg="#f0a830")
        else:
            self.drive.config(text="")
        # telemetry
        if not tel or not tel.get("online"):
            self.title.config(fg="#b3402a")
            self.tele.config(text="no daemon (preview only)" if img is not None else "offline",
                             fg="#b3402a")
            return
        self.title.config(fg="#f0a830")
        bright = tel.get("avelum")
        clip = None
        if luma:
            clip = luma["clip"]
            if bright is None:
                bright = luma["mean"]
        bcol = "#7fc776" if (bright or 0) < 170 else ("#f0a830" if bright < 220 else "#ff7355")
        wb = tel.get("wb"); qp = tel.get("qp")
        line3 = ""
        if wb and wb.get("gains"):
            line3 += "wb %s " % ("auto" if wb.get("op") == "auto" else "%.2f/%.2f" % (wb["gains"][0], wb["gains"][-1]))
        if qp and qp.get("maxqp") is not None:
            line3 += "qp %s" % qp["maxqp"]
        if need is not None:
            line3 += "   need %+.2f stop (%s)" % (need, why or "")
        masked = (" mask%.0f%%" % (100 * luma["masked"])) if luma and luma.get("masked") else ""
        si = (layers or {}).get("sun_info")
        if si and si.get("alt") is not None and si["alt"] > -si["radius_deg"]:
            masked += "  sun %.0f°%s" % (si["alt"], " IN FOV" if si.get("in_fov") else "")
        mi = (layers or {}).get("moon_info")
        if mi and mi.get("alt") is not None:
            masked += "  moon %.0f° %.0f%%%s" % (mi["alt"], mi.get("phase") or 0, " IN FOV" if mi.get("in_fov") else "")
        gains = "A%.2f" % (tel.get("again_x") or 0)
        if tel.get("dgain_x"):
            gains += " D%.2f" % tel["dgain_x"]
        if tel.get("ispdgain_x"):
            gains += " I%.2f" % tel["ispdgain_x"]
        self.tele.config(fg=bcol, text="%s  lum %s%s%s  exp %sus\n%s  ISO %s  %s\n%s" % (
            tel.get("platform", "?"), int(bright) if bright is not None else "-",
            (" clip%.2f%%" % (clip * 100)) if clip else "", masked,
            tel.get("exp_us"), gains, tel.get("iso"),
            ("%dC" % tel["chiptemp"]) if tel.get("chiptemp") else (tel.get("optype") or ""),
            line3))


class App(tk.Tk):
    def __init__(self, allow_grab=True):
        super().__init__()
        self.title("Pod Control — pod as one camera")
        self.configure(bg=BG)
        self.geometry("1400x820")
        self.minsize(720, 460)
        try:
            g = SETTINGS.load().get("geometry")
            if isinstance(g, str) and "x" in g:
                self.geometry(g)
        except Exception:
            pass
        self.stations = get_pod()
        self.by_id = {s.id: s for s in self.stations}
        self.pod = PodController(self.stations)
        # PODCONTROL_DRY=1: never send exposure commands (demo / UI testing on
        # a live pod); the loop still meters and shows what it WOULD do
        self.dry = bool(os.environ.get("PODCONTROL_DRY"))
        if self.dry:
            self.title("Pod Control — DRY RUN (no camera writes)")
            self.pod.manual_all = lambda *a, **k: {}
            self.pod.release = lambda *a, **k: {}
            self.pod.auto_all = lambda *a, **k: {}
        self.allow_grab = allow_grab
        self.ae = SharedAE(self.pod)
        configure_from_pod(self.ae, self.pod)      # top rung = RMS night line
        self.ae_on = False
        self.ae_info = None
        self.ae_slot = None
        self.selected = None      # station id with an active region selection
        self.calibrating = False
        self.q = queue.Queue()
        saved = SETTINGS.load()
        def sv(key, default):
            v = saved.get(key, default)
            return v if isinstance(v, (int, float, bool)) else default
        self.interval = tk.DoubleVar(value=sv("refresh_s", 5.0))
        self.overlay = tk.BooleanVar(value=sv("overlay", True))
        self.sun_radius = tk.DoubleVar(value=sv("sun_radius_deg", F.SUN_RADIUS_DEG[0]))
        self.slew = tk.DoubleVar(value=sv("slew", self.ae.cfg.slew))
        self.sun_votes = tk.BooleanVar(value=sv("sun_cam_votes", self.ae.cfg.sun_cam_votes))
        self.flare_w = tk.DoubleVar(value=sv("flare_radius_deg", F.FLARE_HALF_WIDTH_DEG[0]))
        self.min_blob = tk.IntVar(value=int(sv("clip_min_blob_px", F.CLIP_MIN_BLOB_PX[0])))
        self.moon_radius = tk.DoubleVar(value=sv("moon_radius_deg", F.MOON_RADIUS_DEG[0]))
        self.clip_pct = tk.DoubleVar(value=sv("clip_limit_pct", 100.0 * self.ae.cfg.clip_limit))
        self._saved = saved
        self._save_job = None
        for var in (self.interval, self.overlay, self.sun_radius, self.slew, self.sun_votes,
                    self.flare_w, self.min_blob, self.moon_radius, self.clip_pct):
            var.trace_add("write", lambda *_: self._schedule_save())
        self.running = True
        self._pool = ThreadPoolExecutor(max_workers=12)

        # tiles scale with the window: equal-weight grid cells, tiles sticky
        grid = tk.Frame(self, bg=BG); grid.pack(fill="both", expand=True, padx=8, pady=8)
        rows = (len(self.stations) + COLS - 1) // COLS
        for c in range(COLS):
            grid.columnconfigure(c, weight=1, uniform="tilecol")
        for r in range(rows):
            grid.rowconfigure(r, weight=1, uniform="tilerow")
        self.tiles = {}
        for i, s in enumerate(self.stations):
            t = Tile(grid, s, on_select=self._on_select)
            t.grid(row=i // COLS, column=i % COLS, padx=4, pady=4, sticky="nsew")
            self.tiles[s.id] = t

        # ---- toolbar: grouped controls on two rows, status on its own row ----
        tb = tk.Frame(self, bg=BG); tb.pack(fill="x", padx=8, pady=(0, 6))
        row1 = tk.Frame(tb, bg=BG); row1.pack(fill="x")
        row2 = tk.Frame(tb, bg=BG); row2.pack(fill="x", pady=(4, 0))

        def group(parent, title):
            g = tk.LabelFrame(parent, text=title, fg="#a4967c", bg=BG, bd=1, relief="groove",
                              font=(MONO, 8), padx=6, pady=2)
            g.pack(side="left", padx=(0, 8), fill="y")
            return g

        def lab(parent, txt, **kw):
            tk.Label(parent, text=txt, fg="#c8bfa8", bg=BG).pack(side="left", **kw)

        def spin(parent, var, lo, hi, inc, width, fmt=None):
            kw = {"format": fmt} if fmt else {}
            tk.Spinbox(parent, from_=lo, to=hi, increment=inc, width=width, textvariable=var, **kw).pack(side="left")

        def check(parent, txt, var, **kw):
            tk.Checkbutton(parent, text=txt, variable=var, fg="#c8bfa8", bg=BG, selectcolor=BG,
                           activebackground=BG).pack(side="left", **kw)

        g = group(row1, "control")
        tk.Button(g, text="Auto All", command=self.auto_all).pack(side="left")
        self.ae_btn = tk.Button(g, text="Shared AE: OFF", command=self.toggle_ae)
        self.ae_btn.pack(side="left", padx=6)
        self.cal_btn = tk.Button(g, text="Calibrate WB (cloud)", command=self.calibrate_wb)
        self.cal_btn.pack(side="left", padx=(0, 6))
        tk.Button(g, text="History", command=self.toggle_history).pack(side="left")

        g = group(row1, "loop")
        lab(g, "refresh"); spin(g, self.interval, 2, 60, 1, 4); lab(g, "s", padx=(0, 8))
        lab(g, "slew"); spin(g, self.slew, 0.01, 0.5, 0.01, 5, "%.2f"); lab(g, "stop/cycle", padx=(0, 8))
        lab(g, "clip \u2264"); spin(g, self.clip_pct, 0.0, 5.0, 0.01, 6, "%.3f"); lab(g, "%", padx=(0, 8))
        lab(g, "pt-src <"); spin(g, self.min_blob, 0, 5000, 100, 5); lab(g, "px")

        g = group(row2, "masks & overlay")
        check(g, "overlay", self.overlay, padx=(0, 8))
        lab(g, "sun r"); spin(g, self.sun_radius, 0, 45, 1, 4); lab(g, "\u00b0", padx=(0, 8))
        lab(g, "moon r"); spin(g, self.moon_radius, 0, 30, 1, 4); lab(g, "\u00b0", padx=(0, 8))
        lab(g, "flare r"); spin(g, self.flare_w, 0, 15, 0.5, 4, "%.1f"); lab(g, "\u00b0")

        g = group(row2, "policy")
        check(g, "sun cam votes", self.sun_votes)

        self.status = tk.Label(self, text="starting\u2026", fg="#a4967c", bg=BG, font=(MONO, 9), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 6))
        self.hist_win = None
        self.hist_canvas = None
        self.history = HistoryLog()

        threading.Thread(target=self._updater, daemon=True).start()
        self.after(200, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if self.dry:
            self.after(800, self.toggle_ae)
            self.after(1200, self.toggle_history)
        elif self._saved.get("ae_on"):
            # armed at last exit -> resume driving after the first poll
            self.after(1500, self.toggle_ae)
        if self._saved.get("history_open") and not self.dry:
            self.after(1800, self.toggle_history)

    def _updater(self):
        while self.running:
            t0 = time.time()
            try:
                F.set_sun_radius(self.sun_radius.get())
                F.set_flare_width(self.flare_w.get())
                F.set_clip_min_blob(self.min_blob.get())
                F.set_moon_radius(self.moon_radius.get())
                self.ae.cfg.clip_limit = max(0.0, float(self.clip_pct.get())) / 100.0
                self.ae.cfg.slew = max(0.005, float(self.slew.get()))
                self.ae.cfg.sun_cam_votes = bool(self.sun_votes.get())
            except Exception:
                pass
            poll = self.pod.poll_all(timeout=4)
            sun = feed_sun(self.ae, self.pod)
            futs = {self._pool.submit(frame_for, s, self.allow_grab, True, True): s.id
                    for s in self.stations}
            frames, lumas, layers = {}, {}, {}
            overlay_on = self.overlay.get()
            for f in futs:
                sid = futs[f]
                try:
                    img, src, tcap, path = f.result(timeout=18)
                except Exception:
                    img, src, tcap, path = None, "none", None, None
                frames[sid] = (img, src, path)
                st = self.by_id[sid]
                keep, lay = mask_for(st, img, tcap, layers=True)
                # stats are cached per saved file: a new frame only appears every ~50 s
                lumas[sid] = stats_for_path(st, path, tcap) if path else luma_stats(img, keep)
                if lumas[sid] is not None:
                    lumas[sid] = dict(lumas[sid], t=tcap)
                    lay = dict(lay or {})
                    if overlay_on:
                        lay["clip"], lay["hot"] = (highlight_maps_cached(path, img, keep, lumas[sid]["peak"])
                                                   if path else highlight_maps(img, keep, lumas[sid]["peak"]))
                layers[sid] = lay
            if self.ae_on and self.ae.t_seed:
                # meter the pod on the newest COMPLETE frame set (one capture
                # instant for all cameras), not six frames of different ages
                met, slot = F.meter_set(self.stations, allow_grab=self.allow_grab)
                ctl = {sid: m for sid, m in met.items() if poll.get(sid, {}).get("online")}
                self.ae_slot = slot
                info = self.ae.step(ctl)       # target from the set, one small slew step
                if info.get("changed") or self.ae.repin_needed(poll):
                    try:
                        self.ae.apply(platform=_pod_platform(poll))
                    except Exception:
                        pass
                self.ae_info = info
            try:
                met_for_log = ctl if (self.ae_on and self.ae.t_seed) else lumas
                self.history.append(make_record(poll, self.ae_info if self.ae_on else None, met_for_log,
                                                sun, self.ae_on, self.ae._max_li(), self.ae_slot))
            except Exception:
                pass
            self.q.put((frames, poll, lumas, layers, time.time() - t0))
            for _ in range(int(self.interval.get() * 10)):
                if not self.running:
                    return
                time.sleep(0.1)

    def _drain(self):
        try:
            while True:
                frames, poll, lumas, layers, dt = self.q.get_nowait()
                self._hist_dirty = True
                brights = []
                info = self.ae_info if self.ae_on else None
                driver = (info or {}).get("driver")
                needs = (info or {}).get("needs") or {}
                ov = (self.overlay.get(), float(self.sun_radius.get()), float(self.flare_w.get()),
                      float(self.moon_radius.get()))
                for sid, t in self.tiles.items():
                    img, src, path = frames.get(sid, (None, "none", None))
                    drive = None
                    if info:
                        n = needs.get(sid)
                        drive = (sid == driver, (n[1] if n else None), (n[0] if n else None))
                    t.render(img, src, poll.get(sid), lumas.get(sid),
                             layers.get(sid), ov[0], drive, sig=(path, ov, drive))
                    b = (poll.get(sid) or {}).get("avelum")
                    if b is None and lumas.get(sid):
                        b = lumas[sid]["mean"]
                    if b is not None:
                        brights.append(b)
                daemons = sum(1 for v in poll.values() if v.get("online"))
                ae = ""
                if info:
                    ae = "  AE[%s] %s exp=%dus gain=%.2fx (to go %+.2f stop; set %s)" % (
                        info.get("state", "?"), info["reason"], info["exp_us"], info["total_gain_x"],
                        info.get("to_go", 0.0),
                        ("%.0fs old" % (time.time() - self.ae_slot)) if self.ae_slot else "none")
                self.status.config(text="%d/%d daemon  bright %s%s  (%.1fs)  %s" % (
                    daemons, len(self.stations),
                    ("%d–%d" % (int(min(brights)), int(max(brights)))) if brights else "—",
                    ae, dt, time.strftime("%H:%M:%S")))
        except queue.Empty:
            pass
        # live chart: redraw once per completed cycle (the queue drained)
        if getattr(self, "_hist_dirty", False):
            self._hist_dirty = False
            self._draw_history()
        self.after(200, self._drain)

    def _settings_dict(self):
        d = {"refresh_s": float(self.interval.get()), "overlay": bool(self.overlay.get()),
             "sun_radius_deg": float(self.sun_radius.get()), "slew": float(self.slew.get()),
             "sun_cam_votes": bool(self.sun_votes.get()), "flare_radius_deg": float(self.flare_w.get()),
             "clip_min_blob_px": int(self.min_blob.get()), "moon_radius_deg": float(self.moon_radius.get()),
             "clip_limit_pct": float(self.clip_pct.get()), "ae_on": bool(self.ae_on),
             "history_open": bool(self.hist_win and self.hist_win.winfo_exists()),
             "geometry": self.geometry()}
        if self.hist_win and self.hist_win.winfo_exists():
            d["history_geometry"] = self.hist_win.geometry()
        return d

    def _schedule_save(self):
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(800, self._save_settings)

    def _save_settings(self):
        self._save_job = None
        try:
            SETTINGS.save(self._settings_dict())
        except Exception:
            pass

    def toggle_history(self):
        if self.hist_win and self.hist_win.winfo_exists():
            self.hist_win.destroy(); self.hist_win = None; self.hist_canvas = None
            return
        w = tk.Toplevel(self); w.title("Pod Control \u2014 last 12 h"); w.configure(bg=BG)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w.geometry("%dx%d" % (min(1200, sw - 40), min(420, int(sh * 0.4))))    # modest; resizable
        hg = self._saved.get("history_geometry")
        if isinstance(hg, str) and "x" in hg:
            w.geometry(hg)
        w.minsize(600, 320)
        c = tk.Canvas(w, bg=BG, highlightthickness=0); c.pack(fill="both", expand=True, padx=6, pady=6)
        self.hist_win, self.hist_canvas = w, c
        self._hist_job = None

        def on_resize(_e):
            # redraw once the drag settles (the chart has thousands of items)
            if self._hist_job:
                self.after_cancel(self._hist_job)
            self._hist_job = self.after(120, self._draw_history)
        c.bind("<Configure>", on_resize)
        w.protocol("WM_DELETE_WINDOW", self.toggle_history)
        self.after(50, self._draw_history)

    def _draw_history(self):
        if self.hist_canvas and self.hist_win and self.hist_win.winfo_exists():
            try:
                draw_history(self.hist_canvas, self.history.records, hours=self.history.hours,
                             cam_order=[s.id for s in self.stations])
            except Exception as e:
                self.hist_canvas.delete("all")
                self.hist_canvas.create_text(20, 20, text="history: %s" % e, fill="#b3402a", anchor="nw")

    def toggle_ae(self):
        self.ae_on = not self.ae_on
        self._schedule_save()
        self.ae_btn.config(text="Shared AE: %s" % ("ON" if self.ae_on else "OFF"),
                           fg=("#7fc776" if self.ae_on else "#000"))
        if self.ae_on:
            # start from the cameras' current (darkest) exposure, not a fixed
            # mid-ladder guess that blows out a daytime scene
            self.ae_info = None
            def _take():
                feed_sun(self.ae, self.pod)
                self.ae.takeover(self.pod.poll_all(timeout=4))
            threading.Thread(target=_take, daemon=True).start()
        else:
            threading.Thread(target=lambda: self.ae.release(), daemon=True).start()

    def auto_all(self):
        self.ae_on = False
        self._schedule_save()
        self.ae_btn.config(text="Shared AE: OFF", fg="#000")
        threading.Thread(target=lambda: self.pod.auto_all(), daemon=True).start()

    def _on_select(self, station_id):
        # single active selection: clear the others
        for sid, t in self.tiles.items():
            if sid != station_id:
                t.clear_selection()
        self.selected = station_id
        self.status.config(text="selected %s region — click Calibrate WB" % station_id)

    def calibrate_wb(self):
        if self.calibrating:
            return
        tile = self.tiles.get(self.selected)
        box = tile.frame_box() if tile else None
        if not box:
            self.status.config(text="draw a box on a grey cloud in one camera first")
            return
        poll = self.pod.poll_all(timeout=4)
        if not (poll.get(self.selected) or {}).get("online"):
            self.status.config(text="%s has no daemon — pick a controllable camera" % self.selected)
            return
        self.calibrating = True
        self.cal_btn.config(text="Calibrating…", state="disabled")

        def worker():
            from podcontrol.wbcal import run_pod_calibration
            def on_step(i, rgb, g, err):
                self.status.config(text="WB cal %s: iter %d err=%.3f gains=%.2f/%.2f/%.2fx (waiting for next RMS frame…)" % (
                    self.selected, i, err, g[0]/256, g[1]/256, g[2]/256))
            try:
                gains, err, n = run_pod_calibration(
                    self.pod, self.selected, box, on_step=on_step,
                    fresh_fn=lambda st, after: fresh_frame(st, after, self.allow_grab)[0],
                    mask_fn=mask_for)
                self.status.config(text="WB pushed to pod: %.2f/%.2f/%.2fx  err=%.3f (%d iters)" % (
                    gains[0]/256, gains[1]/256, gains[2]/256, err, n))
            except Exception as e:
                self.status.config(text="WB cal failed: %s" % e)
            finally:
                self.calibrating = False
                self.cal_btn.config(text="Calibrate WB (cloud)", state="normal")

        threading.Thread(target=worker, daemon=True).start()

    def _close(self):
        self.running = False
        try:
            SETTINGS.save(self._settings_dict())
        except Exception:
            pass
        if self.ae_on:
            try: self.ae.release()
            except Exception: pass
        self.destroy()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Control an RMS camera pod as one.")
    ap.add_argument("--cameras", help="IP list/range, e.g. 192.168.42.101-106")
    ap.add_argument("--pod", help="Path to a pod JSON file")
    ap.add_argument("--stations-dir", help="RMS Stations directory")
    ap.add_argument("--no-grab", action="store_true",
                    help="Never pull RTSP (read RMS FramesFiles only) -- safest with RMS running")
    args = ap.parse_args()
    if args.cameras:
        os.environ["PODCONTROL_POD"] = ""  # ignore file
    if args.pod:
        os.environ["PODCONTROL_POD"] = args.pod
    if args.stations_dir:
        os.environ["PODCONTROL_STATIONS_DIR"] = args.stations_dir
    # stash CLI ip spec for get_pod via env
    if args.cameras:
        os.environ["PODCONTROL_CAMERAS"] = args.cameras
    app = App(allow_grab=not args.no_grab)
    # a SIGTERM/SIGINT must release the cameras to auto like a window close
    # does; otherwise a killed app leaves the pod pinned at whatever exposure
    # the shared AE last set
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: app.after(0, app._close))
    app.mainloop()


if __name__ == "__main__":
    main()
