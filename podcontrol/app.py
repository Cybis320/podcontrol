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

import math, time, threading, queue, os, signal
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
from podcontrol import version as VERSION
from podcontrol import vignette as VIGNETTE
from podcontrol.colour import cct_from_gains, gains_from_cct
from podcontrol import skymap

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
MONO = "JetBrains Mono"          # replaced at startup by the first family actually installed


def pick_mono(preferred=("JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono",
                         "Noto Sans Mono", "Ubuntu Mono", "Courier New")):
    """The first installed family from `preferred`, else Tk's own fixed font.

    Naming a family Tk cannot find does not raise and does not warn: Tk
    substitutes silently, and on a machine without JetBrains Mono it chose Noto
    Sans, which is PROPORTIONAL ("iiii" 12 px, "WWWW" 44 px against a monospace
    font's 28 and 28). Every column in the telemetry and the status bar is laid
    out by character count, and the options panel sizes its wrapping from the
    width of a digit, so the whole layout garbles -- while looking perfect on
    any machine that happens to have the font."""
    import tkinter.font as tkfont
    try:
        fams = set(tkfont.families())
    except Exception:
        return preferred[0]
    for f in preferred:
        if f in fams:
            return f
    try:
        return tkfont.nametofont("TkFixedFont").cget("family")
    except Exception:
        return preferred[0]
SIDE_W = 470         # width of the options column on the right (px)
SKY_SIZE = 900        # internal size of the sky composite (px); scaled to the canvas
# Degrees of sun altitude ABOVE RMS's day/night switch at which podcontrol
# stops touching colour. RMS owns colour across its switches, so going quiet
# early (about four minutes here) means a push can never land between its
# night line and our next poll.
COLOUR_QUIET_MARGIN_DEG = 1.0


def _fit_to(rgb, wh):
    """rgb scaled to fit a (w, h) box, keeping its aspect; the array itself
    when it already fits exactly."""
    cw, ch = max(32, wh[0]), max(32, wh[1])
    h, w = rgb.shape[:2]
    sc = min(cw / w, ch / h)
    if abs(sc - 1.0) < 0.005:
        return rgb
    dw, dh = max(1, int(w * sc)), max(1, int(h * sc))
    return cv2.resize(rgb, (dw, dh), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)


class Tip:
    """Delayed hover help for one widget: a small borderless popup, plain Tk.

    The toolbar carries a lot of levers whose meaning is not obvious from a
    four-character label -- what `fast` bounds, why `satu` can be inert -- so
    each control explains itself on hover instead of growing its label."""

    ACTIVE = None
    DELAY_MS = 450

    def __init__(self, widget, text, delay=None):
        self.w, self.text = widget, text
        self.delay = Tip.DELAY_MS if delay is None else delay
        self._job = self._win = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        try:
            self._job = self.w.after(self.delay, self._show)
        except Exception:
            self._job = None

    def _leave(self, _e=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._job:
            try:
                self.w.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._win is not None:
            return
        try:
            if not self.w.winfo_exists():
                return
            if Tip.ACTIVE is not None and Tip.ACTIVE is not self:
                Tip.ACTIVE._hide()
            t = tk.Toplevel(self.w)
            t.wm_overrideredirect(True)
            try:
                t.wm_attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(t, text=self.text, justify="left", bg="#1b1810", fg="#d8cfb4",
                     font=(MONO, 9), bd=1, relief="solid", padx=7, pady=5).pack()
            t.update_idletasks()
            x = self.w.winfo_rootx() + 10
            y = self.w.winfo_rooty() + self.w.winfo_height() + 6
            sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
            x = max(4, min(x, sw - t.winfo_width() - 4))            # keep it on screen
            if y + t.winfo_height() > sh - 4:
                y = self.w.winfo_rooty() - t.winfo_height() - 6     # flip above
            t.wm_geometry("+%d+%d" % (x, y))
            self._win = t
            Tip.ACTIVE = self
        except Exception:
            self._hide()

    def _hide(self):
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None
        if Tip.ACTIVE is self:
            Tip.ACTIVE = None


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
            g = wb["gains"]                     # R, Gr[, Gb], B as the daemon reports them
            line3 += "wb %s%.2f/%.2f/%.2f " % ("auto " if wb.get("op") == "auto" else "", g[0], g[1], g[-1])
        if qp and qp.get("maxqp") is not None:
            line3 += "qp %s " % qp["maxqp"]
        sa, cm = tel.get("satu"), tel.get("ccm")
        if sa and sa.get("value") is not None:
            line3 += "satu x%.2f " % (sa["value"] / 128.0)
        if cm:
            line3 += "ccm %s " % ("off" if cm.get("stage") == "bypassed" else (cm.get("mode") or "?"))
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


_DRY_READS = ("wb", "query", "sysinfo", "ae_stats", "wb_stats", "af_stats", "noise_stats", "satu", "ccm",
              "venc_qp", "venc_cqp", "venc_gop", "persist", "status")


def _dry_pod(pod):
    """PODCONTROL_DRY: every camera-WRITING path of a PodController becomes a no-op -- the
    high-level setters AND the low-level broadcast senders (the venc_* setters go through
    _bcast_venc), so nothing added later can slip past. one() still answers read queries
    (bare read commands only); anything else is dropped."""
    for name in ("manual_all", "release", "auto_all", "wb_all", "wb_auto_all", "satu_all", "ccm_all",
                 "one_live", "_bcast", "_bcast_live", "_bcast_venc", "venc_qp_all", "venc_cqp_all",
                 "venc_gop_all"):
        setattr(pod, name, lambda *a, **k: {})
    real_one = pod.one

    def one(station_id, cmd, timeout=5.0):
        p = (cmd or "").split()
        if len(p) == 1 and p[0] in _DRY_READS:
            return real_one(station_id, cmd, timeout)
        return None
    pod.one = one
    return pod


class App(tk.Tk):
    def __init__(self, allow_grab=True):
        super().__init__()
        # Before any widget: every font tuple below reads MONO, and a family Tk
        # cannot find is substituted silently with whatever it likes.
        global MONO
        MONO = pick_mono()
        self.title("Pod Control %s — pod as one camera" % VERSION.short())
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
        # each camera's real code<->light curve (podcontrol.decode), fetched in the
        # background and refreshed every 30 min (a reflash or gamma change shows up)
        from podcontrol import decode as _decode
        def _decode_loop(stations=self.stations):
            while True:
                try:
                    _decode.fetch_all(stations)
                except Exception:
                    pass
                time.sleep(1800)
        threading.Thread(target=_decode_loop, daemon=True).start()
        # PODCONTROL_DRY=1: never send exposure commands (demo / UI testing on
        # a live pod); the loop still meters and shows what it WOULD do
        self.dry = bool(os.environ.get("PODCONTROL_DRY"))
        if self.dry:
            self.title("Pod Control %s — DRY RUN (no camera writes)" % VERSION.short())
            _dry_pod(self.pod)
        self.allow_grab = allow_grab
        self.ae = self._make_ae(bool(self._saved_setting("individual_ae", False)))
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
        self.const_exp = tk.BooleanVar(value=sv("sky_constant_exposure", False))
        _pj = saved.get("sky_projection", "equidistant")       # a string: sv() takes numbers only
        self.sky_proj = tk.StringVar(value=_pj if _pj in skymap.RADIALS else "equidistant")
        self.sun_radius = tk.DoubleVar(value=sv("sun_radius_deg", F.SUN_RADIUS_DEG[0]))
        self.slew = tk.DoubleVar(value=sv("slew", self.ae.cfg.slew))
        self.slew_fast = tk.DoubleVar(value=sv("slew_fast", self.ae.cfg.slew_fast))
        self.sun_votes = tk.BooleanVar(value=sv("sun_cam_votes", self.ae.cfg.sun_cam_votes))
        self.individual_ae = tk.BooleanVar(value=sv("individual_ae", False))
        self.magenta_ok = tk.BooleanVar(value=sv("wb_rung_magenta_ok", self.ae.cfg.wb_rung_magenta_ok))
        self.camera_meter = tk.BooleanVar(value=sv("camera_meter", False))
        self.flare_w = tk.DoubleVar(value=sv("flare_radius_deg", F.FLARE_HALF_WIDTH_DEG[0]))
        self.min_blob = tk.IntVar(value=int(sv("clip_min_blob_px", F.CLIP_MIN_BLOB_PX[0])))
        self.moon_radius = tk.DoubleVar(value=sv("moon_radius_deg", F.MOON_RADIUS_DEG[0]))
        self.clip_pct = tk.DoubleVar(value=sv("clip_limit_pct", 100.0 * self.ae.cfg.clip_limit))
        # colour: matrix mode + CCM-domain saturation, pushed pod-wide by Apply;
        # `hold` re-asserts them after RMS's dawn replay (its day line says
        # `ccm off` / `satu 128` unless camera_settings is updated)
        # sky-view flat field: raw by default, so a camera that drifts out of the
        # pod's shared settings still shows as a step at its seams. The lens
        # falloff is not that, and is the larger signal (up to 11% across a seam
        # against the pod's real 5%), so correcting it is what lets the eye judge
        # the rest. Off by default all the same: it changes displayed pixels.
        self.vig_on = tk.BooleanVar(value=bool(sv("vignette_on", False)))
        self.vig_coeff = tk.DoubleVar(value=float(sv("vignette_coeff", VIGNETTE.DEFAULT_COEFF)))
        self._vig_busy = False
        self.satu = tk.IntVar(value=int(sv("satu", 128)))
        cm = saved.get("ccm_mode")
        self.ccm_mode = tk.StringVar(value=cm if cm in ("identity", "auto", "off") else "identity")
        self.colour_hold = tk.BooleanVar(value=bool(sv("colour_hold", False)))
        self._colour_asserted = False    # have we pushed colour this session?
        self._colour_force_t = 0.0       # second-click window for an Apply at night
        self._colour_note = ""
        self._colour_note_t = 0.0
        for var in (self.satu, self.ccm_mode, self.colour_hold):
            var.trace_add("write", lambda *_: (self._update_satu_label(), self._schedule_save()))
        self.wb_r = tk.DoubleVar(value=sv("wb_r", 1.0))
        self.wb_g = tk.DoubleVar(value=sv("wb_g", 1.0))
        self.wb_b = tk.DoubleVar(value=sv("wb_b", 1.0))
        self._wb_seeded = "wb_r" in saved          # else seed from the first camera poll
        # colour temperature: a readout of the R/G/B gains, and an input that
        # sets R and B on the daylight locus (G kept). _wb_sync breaks the
        # loop between the two directions.
        self.wb_k = tk.IntVar(value=6500)
        self._wb_sync = False
        self.wb_k.trace_add("write", lambda *_: self._kelvin_edited())
        self._saved = saved
        self._save_job = None
        for var in (self.wb_r, self.wb_g, self.wb_b):
            var.trace_add("write", lambda *_: (self._update_kelvin(), self._schedule_save()))
        self.individual_ae.trace_add("write", lambda *_: self._switch_ae_mode())
        for var in (self.interval, self.overlay, self.const_exp, self.sun_radius, self.slew, self.slew_fast, self.sun_votes,
                    self.magenta_ok, self.camera_meter, self.individual_ae, self.flare_w, self.min_blob, self.moon_radius, self.clip_pct):
            var.trace_add("write", lambda *_: self._schedule_save())
        self.running = True
        self._pool = ThreadPoolExecutor(max_workers=12)

        # tiles scale with the window: equal-weight grid cells, tiles sticky
        # ---- top strip: what code is running, in the top right corner ------
        # Packed first, so both views (which pack before the toolbar) sit under
        # it. Shows the revision THIS process loaded, so after the hourly
        # updater pulls it keeps reading the old commit until the app restarts.
        # That is the whole point: it answers "did the update get picked up?".
        top = tk.Frame(self, bg=BG); top.pack(fill="x", padx=8, pady=(4, 0))
        self.ver_lbl = tk.Label(top, text=VERSION.short(), fg="#6d6350", bg=BG,
                                font=(MONO, 8), anchor="e")
        self.ver_lbl.pack(side="right")
        Tip(self.ver_lbl, VERSION.detail())

        # ---- body: the image (tiles / sky map) on the left, all options in a column on the right
        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True, padx=8, pady=(4, 4))
        self.side = tk.Frame(body, bg=BG); self.side.pack(side="right", fill="y", padx=(8, 0))
        self.view_area = tk.Frame(body, bg=BG); self.view_area.pack(side="left", fill="both", expand=True)
        self._side_groups = []

        grid = tk.Frame(self.view_area, bg=BG); grid.pack(fill="both", expand=True)
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
        self.grid_frame = grid

        # ---- alternate view: the whole pod on one sky map (skymap) ----
        # Rendered in the updater thread from the same frames, only while it
        # is shown (the hidden tiles are then not drawn at all); the Tk thread
        # only scales the finished image to the canvas.
        self.sky_canvas = tk.Canvas(self.view_area, bg=BG, highlightthickness=0)
        self.sky_canvas.bind("<Configure>", self._on_sky_resize)
        self._sky_proj_live = self.sky_proj.get()      # the projection sky_r was built for
        self._sky_proj_busy = False
        self.sky_r = skymap.SkyRenderer(self.stations, skymap.SkyGrid(SKY_SIZE, radial=self._sky_proj_live),
                                        vig_coeff=(VIGNETTE.clamp(self.vig_coeff.get())
                                                   if self.vig_on.get() else 0.0))
        self._sky_rgb = None
        self._sky_msg = None
        self._sky_img_id = None
        self._sky_photo = None
        self._sky_job = None
        self._sky_wh = None                 # canvas size, for pre-scaling in the worker
        self._last_cycle = None
        self._last_poll = {}
        v = saved.get("view")
        self.view = v if v in ("tiles", "sky") else "tiles"

        # ---- toolbar: grouped controls on two rows, status on its own row ----
        tb = self.side
        self.toolbar = tb
        from podcontrol.sunstrip import SunStrip
        c_ = self.ae.cfg
        sg = tk.LabelFrame(tb, text="sun (UTC)", fg="#a4967c", bg=BG, bd=1, relief="groove",
                           font=(MONO, 8), padx=4, pady=2)
        sg.pack(side="top", fill="x", pady=(0, 6))
        self.sunstrip = SunStrip(sg, self.stations[0], width=SIDE_W - 20, height=190, bg=BG, levels=[
            (0.0, "sunrise/set", "#f0c040"), (-6.0, "civil", "#c89a50"),
            (float(c_.night_switch_deg), "RMS switch", "#7fc776"),
            (float(c_.latch_deg), "night latch", "#6fa8dc"), (-18.0, "astro", "#8a7fb0")])
        self.sunstrip.pack(fill="x")
        Tip(self.sunstrip, "The sun's altitude over the UTC day around now (yellow), with the lines podcontrol\n"
                           "and RMS act on: 0 deg sunrise/sunset, -6 civil twilight, the RMS day/night switch,\n"
                           "the night latch, -18 astronomical twilight. Each crossing is marked with its UTC\n"
                           "time; the white line is now. Recomputed every 10 minutes.")
        row1 = row2 = tb

        def group(parent, title, tip=None):
            g = tk.LabelFrame(parent, text=title, fg="#a4967c", bg=BG, bd=1, relief="groove",
                              font=(MONO, 8), padx=6, pady=2)
            g.pack(side="top", fill="x", pady=(0, 6))
            self._side_groups.append(g)
            if tip:
                Tip(g, tip)
            return g

        def lab(parent, txt, tip=None, **kw):
            w = tk.Label(parent, text=txt, fg="#c8bfa8", bg=BG)
            w.pack(side="left", **kw)
            if tip:
                Tip(w, tip)
            return w

        def spin(parent, var, lo, hi, inc, width, fmt=None, tip=None):
            kw = {"format": fmt} if fmt else {}
            w = tk.Spinbox(parent, from_=lo, to=hi, increment=inc, width=width, textvariable=var, **kw)
            w.pack(side="left")
            if tip:
                Tip(w, tip)
            return w

        def check(parent, txt, var, tip=None, **kw):
            w = tk.Checkbutton(parent, text=txt, variable=var, fg="#c8bfa8", bg=BG, selectcolor=BG,
                               activebackground=BG)
            w.pack(side="left", **kw)
            if tip:
                Tip(w, tip)
            return w

        def btn(parent, txt, cmd, tip=None, **kw):
            w = tk.Button(parent, text=txt, command=cmd)
            w.pack(side="left", **kw)
            if tip:
                Tip(w, tip)
            return w

        def pair(parent, txt, var, lo, hi, inc, width, fmt=None, tip=None, unit=None, unit_pad=0):
            """label + spinbox (+ unit) sharing one tip, so hovering any of them helps. Built in
            one sub-frame: the side column flows each group's children as embedded windows, and
            Tk may break a line between any two of them, which split labels from their boxes."""
            f = unit_box(parent)
            lab(f, txt, tip=tip)
            spin(f, var, lo, hi, inc, width, fmt, tip=tip)
            if unit is not None:
                lab(f, unit, tip=tip, padx=(0, unit_pad))
            return f

        def unit_box(parent, **kw):
            """a frame whose contents flow (and wrap) as one piece in the side column."""
            f = tk.Frame(parent, bg=BG)
            f.pack(side="left", **kw)
            return f

        g = group(row1, "control", "Pod-wide actions. Everything here acts on all six cameras at once.")
        btn(g, "Auto All", self.auto_all,
            "Hand every camera back to RMS's own day exposure line and switch AE off.\n"
            "Never sends a bare `auto`: on the Goke that would reset the AE ranges and break\n"
            "the science config's fixed sensor digital gain.")
        self.ae_btn = btn(g, "AE: OFF", self.toggle_ae,
            "Switch podcontrol's exposure control on or off. How it drives the cameras is the\n"
            "choice right next to it (shared / individual). Off leaves each camera on whatever\n"
            "it currently holds.", padx=(6, 2))
        mode_tip = ("shared: ONE exposure and gain for the whole pod; the darkest need wins, so if any\n"
                    "camera clips, everyone backs off (the sun camera darkens all six).\n"
                    "individual: every camera driven by its own controller with the same logic (clean-zone\n"
                    "metering, highlight priority, slow slews, its own WB rung); saved frames carry their\n"
                    "exposure and the sky view's 'const exp' reunifies the pod. At night all cameras latch\n"
                    "to the same RMS night line either way. Changing it with AE on hands the cameras back\n"
                    "and takes over again at once.")
        for val, txt in ((False, "shared"), (True, "individual")):
            w = tk.Radiobutton(g, text=txt, variable=self.individual_ae, value=val, fg="#c8bfa8", bg=BG,
                               selectcolor=BG, activebackground=BG)
            w.pack(side="left", padx=(0, 2 if not val else 6))
            Tip(w, mode_tip)
        self.cal_btn = btn(g, "Calibrate WB (cloud)", self.calibrate_wb,
            "Drag a box over a grey cloud on one tile, then click. Iterates the white-balance\n"
            "gains until that region is neutral and pushes the result to every camera.\n"
            "Takes minutes on a capturing pod: each step waits for a fresh RMS frame.", padx=(0, 6))
        self.ncal_btn = btn(g, "Calibrate night gain", self.calibrate_night,
            "Night only. Sweeps each camera's analog gain down from the top (exposure and\n"
            "ISP gain fixed), measuring sky and noise ON the camera (no extra stream), and\n"
            "proposes the lowest gain that loses no sensitivity -- the darkest sky's need.\n"
            "Shows the table; nothing is saved until you click Apply, which sets the cameras\n"
            "and RMS's night line in the settings JSON. Switch AE off first.", padx=(0, 6))
        btn(g, "History", self.toggle_history,
            "Open the 12-hour chart: exposure and total gain per camera, pod luma against the\n"
            "clipping ceiling, and the clipped fraction, with night and latched shading.")
        self.view_btn = btn(g, "View: tiles", self.toggle_view,
            "Switch between the six preview tiles and one all-sky composite of the whole pod,\n"
            "projected from the stations' RMS platepars.", padx=(6, 0))
        proj_tip = ("Projection of the sky view, the same four as Janus's ground-truth editor (same\n"
                    "constants), all centred on the zenith; only the radius changes:\n"
                    "  equidistant    radius ~ zenith angle: the RMS base projection, the all-sky look\n"
                    "  stereographic  radius ~ tan(zenith/2): shapes kept true near the horizon\n"
                    "  aerial         the 10 km contrail layer seen from 30 km above it: to scale near\n"
                    "                 the centre, compressed smoothly outward, the whole dome fits\n"
                    "  ground         the contrail layer as a flat map, distances to scale; stops at\n"
                    "                 10 deg altitude (it runs to infinity at the horizon)\n"
                    "The first switch to a projection builds its lookup tables (~10 s, the old view\n"
                    "stays up meanwhile); after that they come from the disk cache.")
        f = unit_box(g, padx=(4, 0))
        om = tk.OptionMenu(f, self.sky_proj, *skymap.RADIALS, command=lambda _v: self._sky_proj_changed())
        om.config(width=12, bg=BG, fg="#c8bfa8", activebackground=BG, highlightthickness=0)
        om.pack(side="left")
        Tip(om, proj_tip)

        g = group(row1, "loop", "Loop timing and how fast the shared AE is allowed to move.")
        pair(g, "refresh", self.interval, 2, 60, 1, 4, tip=
             "Seconds between cycles. RMS saves a frame about every 5 s and flushes in blocks,\n"
             "so below ~5 s this costs CPU without seeing new data.", unit="s", unit_pad=8)
        pair(g, "slew", self.slew, 0.01, 0.5, 0.01, 5, "%.2f", tip=
             "Maximum stops the pod exposure may move per cycle while tracking normally.\n"
             "0.05 stop is about 3.5% brightness per frame, invisible in a timelapse.\n"
             "Moves against the diurnal trend run slower still, to ride out passing clouds.")
        pair(g, "fast", self.slew_fast, 0.1, 4.0, 0.1, 4, "%.1f", tip=
             "Maximum stops per cycle in the FAST regime, which applies only to a gross error:\n"
             "a frame more than 10% clipped, or a dark seed just after takeover. Mild errors\n"
             "keep the smooth slew. This is what recovers 16 stops in minutes instead of half\n"
             "an hour when a camera is handed the night line in daylight.",
             unit="stop/cycle", unit_pad=8)
        pair(g, "clip ≤", self.clip_pct, 0.0, 5.0, 0.01, 6, "%.3f", tip=
             "Clipped fraction the AE aims to stay under. Above it the pod asks for less light.\n"
             "0.005% is roughly 100 pixels of the frame. Raise it for a timelapse if cloud\n"
             "edges make the pod pump.", unit="%", unit_pad=8)
        pair(g, "pt-src <", self.min_blob, 0, 5000, 50, 5, tip=
             "Clipped blobs smaller than this many pixels do not count as clipping: a street\n"
             "lamp, a planet, headlights. They clip at any night-worthy exposure, so dimming\n"
             "the whole pod for them is pointless. 0 counts every clipped pixel.", unit="px")

        g = group(row2, "masks & overlay",
                  "What the metering ignores, and how it is shown. These zones are excluded from\n"
                  "every measurement podcontrol makes, on top of each station's RMS mask.")
        check(g, "overlay", self.overlay, tip=
              "Tint the excluded zones and what drives the exposure, on the tiles and the sky view:\n"
              "red = RMS mask, orange = sun zone, violet = lens flare, blue = moon zone,\n"
              "magenta = clipped pixels, cyan = the peak pixels on the driving camera.\n"
              "On the sky view it also governs the FOV overlay: with it off you get the bare\n"
              "composite, no camera footprints, labels, telemetry or sun/moon markers. The\n"
              "alt/az grid and the caption stay, so the frame time is always readable.", padx=(0, 8))
        check(g, "const exp", self.const_exp, tip=
              "DEMO: draw the sky view with every camera at ONE common exposure. Each tile is\n"
              "scaled in linear light by (median exposure / that camera's exposure when the frame\n"
              "was taken), from the history's 5 s polls. Meant for trying free per-camera exposure\n"
              "(switch AE off): the raw mosaic is patchy, this one should be continuous. Seams that\n"
              "remain are uncalibrated per-camera sensitivity/vignetting. View only.", padx=(0, 8))
        pair(g, "sun r", self.sun_radius, 0, 45, 1, 4, tip=
             "Radius of the exclusion disc around the sun, from the platepar and an ephemeris.\n"
             "Applied whenever the disc can touch the sky, so the glow around a just-set sun is\n"
             "excluded too. 0 disables it. Measure the right value with `sunmask --measure`.",
             unit="°", unit_pad=8)
        pair(g, "moon r", self.moon_radius, 0, 30, 1, 4, tip=
             "Radius of the exclusion disc around the moon, applied only while the sun is below\n"
             "the horizon: a daytime moon cannot clip. 0 disables it.", unit="°", unit_pad=8)
        pair(g, "flare r", self.flare_w, 0, 15, 0.5, 4, "%.1f", tip=
             "Radius of the lens-flare ghost discs, which sit on the line from the sun through\n"
             "the lens's principal point at fixed fractions of its distance. 0 disables the\n"
             "whole flare model.", unit="°")

        g = group(row2, "sky flat field",
                  "Lens vignetting correction for the sky view only. Nothing is sent to a camera\n"
                  "and no other measurement changes: this is purely how the composite is drawn.")
        check(g, "flatten", self.vig_on, tip=
              "Off (the default) draws the raw composite, so a camera that has drifted out of\n"
              "the pod's shared exposure shows as a step at its seams.\n\n"
              "The trouble is the lens puts a step there too. Overlaps sit at the EDGE of both\n"
              "fields, but rarely at the same edge distance in each, so one camera images a\n"
              "shared patch nearer its axis and reads brighter. Measured 2026-09-28 that alone\n"
              "made pairs disagree by up to 11%, against a real pod mismatch of about 5%.\n"
              "Flattening removes it, so what is left at a seam is a genuine mismatch.\n\n"
              "Costs nothing per frame: the correction is folded into the blend weights, which\n"
              "are static.", padx=(0, 6))
        spin(g, self.vig_coeff, 0.0, VIGNETTE.MAX_COEFF, 0.00001, 9, "%.6f", tip=
             "The coefficient k in RMS's own model, V(r) = cos(k*r)^4, in radians per pixel,\n"
             "r measured from the optical axis. Auto sets it from your own overlaps; type a\n"
             "value to override.\n\n"
             "Podcontrol keeps its own rather than using the platepar's, for two reasons. RMS\n"
             "fits that one against STAR PHOTOMETRY, which also absorbs focus softening and\n"
             "extinction, and on this pod five of six platepars were never fitted at all and\n"
             "hold RMS's default -- which over-corrects by 92%, asking 3.29x at the corner\n"
             "where 1.35x is needed. Writing this value back would shift RMS's meteor\n"
             "magnitudes, so it is deliberately kept separate.")
        self.vig_note = lab(g, "", padx=(6, 0))
        btn(g, "auto", self._vig_autotune, tip=
            "Fit the coefficient from the pod's own overlapping fields, over the last few\n"
            "minutes of saved frames. Takes a few seconds and touches no camera.\n\n"
            "Where two cameras see one sky direction the sky itself cancels exactly in their\n"
            "ratio, so no sky model is needed -- and time-averaging could not do this, because\n"
            "the cameras do not move and horizon glow sits in the same place in every frame.\n"
            "One shared coefficient is fitted for all six, since they are the same lens: fitted\n"
            "per camera it is degenerate, because overlaps never reach inside r~0.35 and each\n"
            "camera's curve then trades against its own gain.\n\n"
            "It also reports each camera's gain, which IS the pod's exposure mismatch with the\n"
            "lens taken out.", padx=(6, 0))
        # registered HERE, not beside the other traces: these handlers touch
        # sky_r and vig_note, both of which are built after the variables are.
        for var in (self.vig_on, self.vig_coeff):
            var.trace_add("write", lambda *_: (self._vig_apply(), self._schedule_save()))
        self._vig_label()

        g = group(row2, "policy")
        check(g, "sun cam votes", self.sun_votes, tip=
              "Checked: a camera with the sun in its field votes on the pod exposure like any\n"
              "other, so the sun-zone radius is your only lever. Cleared: it follows the pod\n"
              "without voting. If every camera sees the sun they all vote regardless.")
        check(g, "RMS fixes magenta", self.magenta_ok, tip=
              "Checked: raw (sensor) saturation no longer stops or reverses the WB rung -- it keeps\n"
              "recovering gain-induced R/B clipping down to its floor (largest WB gain 1.0x) and\n"
              "holds once recovered. Only with RMS day_highlight_rebuild: true on the stations,\n"
              "which rebuilds the magenta areas in the saved day frames. Cleared: the rung backs\n"
              "off to s = 1 whenever a frame shows raw saturation (no magenta at all).")
        check(g, "camera meter", self.camera_meter, tip=
              "Checked: meter each camera from its own ISP statistics (ae_stats + wb_stats: the\n"
              "current frame, linear, zones touching no mask only) instead of RMS's saved frames,\n"
              "which can be ~50 s old. Cameras without those commands (older isp_ctl) keep using\n"
              "the frame meter. Cleared: frame meter for every camera.")

        g = group(row2, "white balance (x gains, pod-wide)",
                  "Manual white balance for the whole pod. RMS owns colour at its day/night\n"
                  "switches, so anything set here is replaced at the next switch unless it is\n"
                  "also in the station's camera_settings file.")
        wb_tip = ("Per-channel white-balance gain as a multiplier: 1.00 is the daemon's 256.\n"
                  "Applied in 12-bit before demosaic, so gains above 1x can clip red or blue\n"
                  "before green. The shared AE's WB rung attenuates all three together below\n"
                  "the exposure floor.")
        for txt, var in (("R", self.wb_r), ("G", self.wb_g), ("B", self.wb_b)):
            pair(g, txt, var, 0.25, 4.0, 0.01, 5, "%.2f", tip=wb_tip, unit="", unit_pad=4)
        btn(g, "Apply", self.apply_wb,
            "Push these three gains to every camera as a manual white balance.", padx=(4, 4))
        btn(g, "Auto", self.wb_auto,
            "Hand white balance back to each camera's own AWB.")
        k_tip = ("Colour temperature, both ways. As a readout it estimates the temperature of the\n"
                 "gains on the left. Type one and R and B are set to neutralise a daylight-locus\n"
                 "illuminant of that temperature, with G kept as it is.\n"
                 "One axis only: the green-magenta tint is fixed to the locus, so this cannot\n"
                 "reproduce every balance. The cloud calibrator stays the source of truth.")
        f = unit_box(g, padx=(8, 0))
        lab(f, "≈", tip=k_tip)
        spin(f, self.wb_k, 3800, 20000, 100, 6, tip=k_tip)
        lab(f, "K", tip=k_tip)
        self._update_kelvin()

        g = group(row2, "colour (pod-wide)",
                  "Colour matrix and saturation. Both act in the ISP's ColorMatrix stage, in\n"
                  "linear RGB before gamma and before the 8-bit conversion, which is the least\n"
                  "destructive place to change colour.")
        ccm_tip = ("Colour matrix stage, and what `satu` next to it multiplies:\n"
                   "  identity  a neutral matrix, so satu is a PURE chroma gain around the luma\n"
                   "            axis. Grey stays grey, hue is kept, each channel keeps its own\n"
                   "            sensor response. The least destructive boost.\n"
                   "  auto      the sensor's IQ colour-temperature table. A real colour\n"
                   "            correction, but it mixes channels, so it amplifies chroma noise\n"
                   "            and follows the AWB's temperature estimate. Measured on E1 in\n"
                   "            daylight, auto at 1.00x gives about the same chroma as identity\n"
                   "            at 1.99x (55.1 against 56.5).\n"
                   "  off       the stage is bypassed, which makes satu INERT. Raw sensor colour.\n"
                   "Both identity and auto reach TRUE mono at satu 0 (measured chroma exactly\n"
                   "0.00), so RMS's night line gives mono science frames either way.")
        f = unit_box(g)
        lab(f, "ccm", tip=ccm_tip)
        om = tk.OptionMenu(f, self.ccm_mode, "identity", "auto", "off")
        om.config(width=7, bg=BG, fg="#c8bfa8", activebackground=BG, highlightthickness=0)
        om.pack(side="left", padx=(0, 6))
        Tip(om, ccm_tip)
        satu_tip = ("Chroma gain applied inside the colour matrix, in linear RGB before gamma and\n"
                    "before the 8-bit conversion. 128 = 1.00x; 0 is true greyscale (measured\n"
                    "chroma exactly 0.00), which is how RMS makes the night frames mono.\n"
                    "255 = 1.99x is the ceiling because the ISP register is 8-bit: 2.00x would\n"
                    "need 256, which does not fit, so 1.99x is as high as the hardware goes.\n"
                    "Does nothing while the matrix on the left is `off`.")
        f = unit_box(g)
        lab(f, "satu", tip=satu_tip)
        spin(f, self.satu, 0, 255, 8, 4, tip=satu_tip)
        self.satu_x = tk.Label(f, text="", fg="#f0a830", bg=BG, font=(MONO, 9, "bold"))
        self.satu_x.pack(side="left", padx=(4, 6))
        Tip(self.satu_x, satu_tip)
        btn(g, "Apply", self.apply_colour,
            "Push the matrix mode and the saturation to every camera.", padx=(0, 6))
        check(g, "hold", self.colour_hold, tip=
              "Keep these settings on the pod unattended, checked once a minute.\n"
              "  DAY    re-asserts the matrix and saturation on any camera that lost them.\n"
              "         RMS replays `ccm off` / `satu 128` at every dawn switch, and a\n"
              "         rebooted camera comes up on the science baseline, so without this\n"
              "         the group silently stops applying.\n"
              "  DUSK   goes quiet one degree of sun altitude BEFORE RMS's night switch,\n"
              "         about four minutes, so a push can never land after RMS's night line.\n"
              "  NIGHT  silent. RMS sets `satu 0` for mono science frames and that stands.\n"
              "  DAWN   re-asserts by itself once the sun is back above that altitude.\n"
              "Writes are live only and never reach camera flash, so a reboot always comes\n"
              "up on the science baseline. The durable place for a permanent change is the\n"
              "day entry of camera_settings.json.")
        self._update_satu_label()

        self._flow_groups()

        self.status = tk.Label(self, text="starting\u2026", fg="#a4967c", bg=BG, font=(MONO, 9), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 6))
        self.hist_win = None
        self.hist_canvas = None
        self.history = HistoryLog()
        if self.view == "sky":
            self._apply_view(initial=True)

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
                self.ae.cfg.slew_fast = max(0.05, float(self.slew_fast.get()))
                self.ae.cfg.sun_cam_votes = bool(self.sun_votes.get())
                self.ae.cfg.wb_rung_magenta_ok = bool(self.magenta_ok.get())
            except Exception:
                pass
            # one daemon connection per camera per cycle; the encoder/WB
            # fields (which only change when something writes them) are read
            # in full once a minute or right after we pushed something
            self._cycle_n = getattr(self, "_cycle_n", 0) + 1
            full = (self._cycle_n % 12 == 1) or (time.time() - self.ae.t_apply < 15)
            poll = self.pod.poll_all(timeout=4, full=full)
            sun = feed_sun(self.ae, self.pod)      # colour discipline needs the sun first
            if full:
                self._colour_keep(poll)
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
                if self.camera_meter.get():
                    from podcontrol.camera_meter import meter_set_hybrid
                    met, slot = meter_set_hybrid(self.stations, allow_grab=self.allow_grab,
                                                 wb_scale_fn=self.ae.wb_scale_at)
                else:
                    met, slot = F.meter_set(self.stations, allow_grab=self.allow_grab,
                                            wb_scale_fn=self.ae.wb_scale_at)
                ctl = {sid: m for sid, m in met.items() if poll.get(sid, {}).get("online")}
                self.ae_slot = slot
                # RMS's dawn `auto` ends our control silently: give up the latch
                # before stepping, or we hold the night ceiling while the
                # cameras fan out on their own AE.
                self.ae.note_cameras(poll)
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
            self._last_poll = poll
            sky = None
            if self.view == "sky":
                try:
                    sky = self._render_sky(poll, frames, layers)
                except Exception as e:
                    sky = ("sky: %s" % e,)
            self.q.put((frames, poll, lumas, layers, time.time() - t0, sky))
            for _ in range(int(self.interval.get() * 10)):
                if not self.running:
                    return
                time.sleep(0.1)

    def _drain(self):
        try:
            self._drain_once()
        except Exception as e:                  # never let a display error stop the loop
            # Say WHERE. "display error: '<' not supported between NoneType and
            # float" names a Python rule, not a line, and the status bar is the
            # only place it ever appeared: the traceback went nowhere. The last
            # in-app frame is the one worth reading, and the full trace goes to
            # stderr once per distinct site so a terminal or log has it all.
            import traceback
            tb = traceback.extract_tb(e.__traceback__)
            here = [f for f in tb if "podcontrol" in (f.filename or "")]
            where = ("%s:%d in %s" % (os.path.basename(here[-1].filename), here[-1].lineno,
                                      here[-1].name)) if here else "?"
            seen = getattr(self, "_drain_errs", None)
            if seen is None:
                seen = self._drain_errs = set()
            if where not in seen:
                seen.add(where)
                traceback.print_exc()
            try:
                self.status.config(text="display error at %s: %s" % (where, e), fg="#b3402a")
            except Exception:
                pass
        self.after(200, self._drain)

    def _drain_once(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item[0], str):            # ("sky", rgb): on-demand render
                    self._show_sky(item[1])
                    continue
                frames, poll, lumas, layers, dt, sky = item
                self._last_cycle = (frames, poll, lumas, layers)
                self._hist_dirty = True
                if self.view == "tiles":
                    self._render_tiles(frames, poll, lumas, layers)
                else:
                    self._show_sky(sky)
                brights = []
                for sid in self.tiles:
                    b = (poll.get(sid) or {}).get("avelum")
                    if b is None and lumas.get(sid):
                        b = lumas[sid]["mean"]
                    if b is not None:
                        brights.append(b)
                info = self.ae_info if self.ae_on else None
                daemons = sum(1 for v in poll.values() if v.get("online"))
                if not self._wb_seeded:
                    for v in poll.values():
                        gs = (v.get("wb") or {}).get("gains")
                        if v.get("online") and gs and len(gs) >= 3:
                            self.wb_r.set(round(gs[0], 2)); self.wb_g.set(round(gs[1], 2)); self.wb_b.set(round(gs[-1], 2))
                            self._wb_seeded = True
                            break
                ae = ""
                if info:
                    ae = "  AE[%s] %s exp=%dus gain=%.2fx%s (to go %+.2f stop; set %s)" % (
                        info.get("state", "?"), info["reason"], info["exp_us"], info["total_gain_x"],
                        (" wb\u00d7%.2f" % info["wb_scale"]) if (info.get("wb_scale") or 1.0) < 0.999 else "",
                        info.get("to_go", 0.0),
                        ("%.0fs old" % (time.time() - self.ae_slot)) if self.ae_slot else "none")
                note = ""
                if (info or {}).get("wb_unconfirmed"):
                    note = "  [WB not confirmed on %s -- retrying]" % ",".join((info or {})["wb_unconfirmed"])
                if self._colour_note and time.time() - self._colour_note_t < 90:
                    note = "  [%s]" % self._colour_note
                elif self.colour_hold.get() and self._colour_quiet():
                    note = "  [colour: RMS owns it until dawn]"
                self.status.config(text="%d/%d daemon  bright %s%s  (%.1fs)  %s%s" % (
                    daemons, len(self.stations),
                    ("%d–%d" % (int(min(brights)), int(max(brights)))) if brights else "—",
                    ae, dt, time.strftime("%H:%M:%S"), note))
        except queue.Empty:
            pass
        # live chart: redraw once per completed cycle (the queue drained)
        if getattr(self, "_hist_dirty", False):
            self._hist_dirty = False
            self._draw_history()

    def _render_tiles(self, frames, poll, lumas, layers):
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

    # ---- sky view ------------------------------------------------------------
    def _render_sky(self, poll, cyc_frames=None, cyc_layers=None):
        """The sky composite (RGB, pre-scaled to the canvas) of the newest
        complete frame set, with the overlay's exclusion zones and clipped
        pixels when the overlay is on. Runs in a worker thread. Everything
        static is cached in the renderer; a cycle with the same frames costs
        a few ms, a new set ~60 ms. cyc_frames / cyc_layers are this cycle's
        tile frames and overlay layers: when a camera's set frame is the very
        file its tile shows (the usual case), its layers are reused as they
        are, so the overlay adds no per-cycle work."""
        r = self.sky_r
        # ensure() every cycle, not just until ready: it is how an edited mask or
        # platepar reaches the sky view. Steady state it stats two files per
        # station and returns, so the cost is noise against the ~5 ms cycle.
        if not r.ensure():
            return ("no platepar found: the sky view needs the stations' RMS platepars",)
        overlay_on = self.overlay.get()
        imgs, paths, t, spread = skymap.pod_frames(self.stations, decode=False)
        if not paths:
            return None
        layers = None
        if overlay_on:
            layers = {}
            for st in self.stations:
                p = paths.get(st.id)
                if not p:
                    continue
                cf = (cyc_frames or {}).get(st.id)
                cl = (cyc_layers or {}).get(st.id)
                if cl is not None and cf is not None and cf[2] == p:
                    layers[st.id] = cl                 # the tile's own file: reuse its layers
                    continue
                img = imgs.get(st.id)
                if img is None:
                    img = imgs[st.id] = F.imread_cached(p)
                if img is None:
                    continue
                keep, lay = mask_for(st, img, t, layers=True)
                lay = dict(lay or {})
                st_ = stats_for_path(st, p, t)         # cached per file
                if st_ is not None:
                    lay["clip"], _ = highlight_maps_cached(p, img, keep, st_["peak"])
                layers[st.id] = lay
        info = self.ae_info if self.ae_on else None
        drive = None
        if info:
            driver = info.get("driver")
            drive = {sid: (sid == driver, (n[1] if n else None), (n[0] if n else None))
                     for sid, n in (info.get("needs") or {}).items()}
        # the overlay checkbox governs the FOV overlay too: with it off the sky
        # view is the bare composite (plus the alt/az grid and the caption), no
        # footprints, camera labels, telemetry or sun/moon markers
        if self.const_exp.get():
            # DEMO: every tile brought to one common exposure (podcontrol.radiance)
            from podcontrol import radiance
            imgs, paths, cinfo = radiance.constant_exposure(paths, list(self.history.records))
            self._const_info = cinfo
        bgr, _ = r.render(imgs, paths, t, spread, telem=poll, drive=drive, layers=layers,
                          outlines=overlay_on, labels=overlay_on, bodies=overlay_on)
        if self.const_exp.get() and getattr(self, "_const_info", None) and self._const_info.get("e_ref"):
            ks = self._const_info["k"]
            txt = "constant exposure %.0f us-x   k: %s" % (self._const_info["e_ref"],
                   " ".join("%s %.2f" % (sid[-2], ks[sid]) for sid in sorted(ks)))
            # one line above the renderer's own caption (time, cameras, coverage) at h - 8
            from podcontrol.skymap import _text
            _text(bgr, txt, (6, bgr.shape[0] - 26), 0.45)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        wh = self._sky_wh                              # scale here, not in the Tk thread
        return _fit_to(rgb, wh) if wh else rgb

    def _sky_now(self):
        """On-demand render (the view was just switched): via the queue."""
        try:
            fr, po, lu, la = self._last_cycle or (None, None, None, None)
            sky = self._render_sky(po or self._last_poll, fr, la)
        except Exception as e:
            sky = ("sky: %s" % e,)
        self.q.put(("sky", sky))

    def _show_sky(self, sky):
        """sky: an RGB array, None (nothing to show yet) or (message,)."""
        if isinstance(sky, tuple):
            self._sky_rgb, self._sky_msg = None, sky[0]
        elif sky is None:
            self._sky_rgb = None
            self._sky_msg = ("no complete frame set yet" if self.sky_r.ready
                             else "building the sky map (first time: ~10 s)…")
        else:
            self._sky_rgb, self._sky_msg = sky, None
        self._fit_sky()

    def _fit_sky(self):
        c = self.sky_canvas
        cw, ch = max(32, c.winfo_width()), max(32, c.winfo_height())
        if self._sky_rgb is None:
            c.delete("all"); self._sky_img_id = None
            c.create_text(cw // 2, ch // 2, text=self._sky_msg or "…", fill="#726650", font=(MONO, 14))
            return
        disp = _fit_to(self._sky_rgb, (cw, ch))       # a no-op when pre-scaled to this size
        dh, dw = disp.shape[:2]
        self._sky_photo = ImageTk.PhotoImage(Image.fromarray(disp))
        x, y = (cw - dw) // 2, (ch - dh) // 2
        if self._sky_img_id:
            c.itemconfig(self._sky_img_id, image=self._sky_photo)
            c.coords(self._sky_img_id, x, y)
        else:
            c.delete("all")
            self._sky_img_id = c.create_image(x, y, anchor="nw", image=self._sky_photo)

    def _on_sky_resize(self, _e=None):
        c = self.sky_canvas
        self._sky_wh = (c.winfo_width(), c.winfo_height())
        if self._sky_job:
            self.after_cancel(self._sky_job)
        self._sky_job = self.after(80, self._fit_sky)

    def _sky_proj_changed(self):
        self._schedule_save()
        if not self._sky_proj_busy:
            self._sky_proj_busy = True
            self._pool.submit(self._sky_proj_build)

    def _sky_proj_build(self):
        """Pool thread: build a renderer for the chosen projection (its LUTs take ~1.5 s per
        camera the first time) and swap it in only once it is ready, so neither the AE loop
        (which renders the sky view) nor the display stalls meanwhile. Loops if the choice
        changed again during the build."""
        try:
            while True:
                want = self.sky_proj.get()
                if want == self._sky_proj_live:
                    return
                old = self.sky_r
                r = skymap.SkyRenderer(self.stations, skymap.SkyGrid(SKY_SIZE, radial=want),
                                       old.feather_deg, old.mask_weight, vig_coeff=old.vig_coeff)
                r.ensure()
                r.set_vig_coeff(old.vig_coeff)         # a flat-field change during the build
                self.sky_r, self._sky_proj_live = r, want
                if self.view == "sky":
                    self._sky_now()
        except Exception as e:
            print("sky projection %s failed: %s" % (self.sky_proj.get(), e))
        finally:
            self._sky_proj_busy = False

    def toggle_view(self):
        self.view = "sky" if self.view == "tiles" else "tiles"
        self._schedule_save()
        self._apply_view()

    # ---- sky flat field ------------------------------------------------------
    def _vig_apply(self, *_):
        """Push the toggle/coefficient into the renderer and redraw if needed."""
        want = VIGNETTE.clamp(self.vig_coeff.get()) if self.vig_on.get() else 0.0
        if self.sky_r.set_vig_coeff(want) and self.view == "sky":
            self._pool.submit(self._sky_now)          # do not wait for the next cycle
        self._vig_label()

    def _vig_label(self):
        c = VIGNETTE.clamp(self.vig_coeff.get())
        if not self.vig_on.get():
            self.vig_note.config(text="raw", fg="#6d6350")
        elif not c:
            self.vig_note.config(text="off (0)", fg="#6d6350")
        else:
            self.vig_note.config(text="%.2fx corner" % VIGNETTE.corner_gain(c), fg="#c8bfa8")

    def _vig_autotune(self):
        """Fit the coefficient from the pod's own overlaps, in a worker thread."""
        if self._vig_busy:
            return
        self._vig_busy = True
        self.vig_note.config(text="fitting…", fg="#f0a830")

        def work():
            try:
                sets = F.recent_sets(self.stations, want=12)
                if len(sets) < 3:
                    raise RuntimeError("only %d usable frame sets; needs a few minutes of"
                                       " frames from every camera" % len(sets))
                res = VIGNETTE.fit(self.sky_r, sets)
            except Exception as e:
                res = {"error": str(e)}
            self.after(0, lambda: self._vig_autotune_done(res))
        threading.Thread(target=work, daemon=True).start()

    def _vig_autotune_done(self, res):
        from tkinter import messagebox
        self._vig_busy = False
        if res.get("error") or not res.get("coeff"):
            self._vig_label()
            messagebox.showwarning("Sky flat field", "Could not fit:\n\n%s" % res.get("error", "no result"))
            return
        self.vig_coeff.set(round(res["coeff"], 6))
        self.vig_on.set(True)
        self._vig_apply()
        gains = res.get("gains") or {}
        spread = (100 * (max(gains.values()) / min(gains.values()) - 1)) if gains else 0.0
        lines = ["k = %.6f rad/px  (%.2fx at the corner)" % (res["coeff"], res["corner"]),
                 "residual %.1f%% over %d overlap samples from %d frame sets"
                 % (100 * res["residual"], res["samples"], res["sets"]), "",
                 "Per-camera gain, which is the pod's exposure mismatch with the",
                 "lens taken out:"]
        for sid in sorted(gains):
            lines.append("    %-8s %.3fx" % (sid, gains[sid]))
        lines += ["", "spread %.1f%%" % spread]
        messagebox.showinfo("Sky flat field", "\n".join(lines))

    def _apply_view(self, initial=False):
        self.view_btn.config(text="View: %s" % self.view)
        if self.view == "sky":
            self.grid_frame.pack_forget()
            self.sky_canvas.pack(fill="both", expand=True)
            if self._sky_rgb is None:
                self._show_sky(None)                   # placeholder until the render lands
            if not initial:                            # at start the first cycle renders it anyway
                self._pool.submit(self._sky_now)       # do not wait for the next cycle
        else:
            self.sky_canvas.pack_forget()
            self.grid_frame.pack(fill="both", expand=True)
            if self._last_cycle:
                self._render_tiles(*self._last_cycle)  # the tiles were not drawn while hidden

    def _update_kelvin(self):
        """Gains -> the Kelvin box (readout); skipped while the box is driving."""
        if self._wb_sync:
            return
        try:
            c = cct_from_gains(self.wb_r.get(), self.wb_g.get(), self.wb_b.get())
        except Exception:
            return
        if c:
            self._wb_sync = True
            try:
                self.wb_k.set(int(round(c / 50) * 50))
            finally:
                self._wb_sync = False

    def _kelvin_edited(self):
        """The Kelvin box -> R and B on the daylight locus (G kept)."""
        if self._wb_sync:
            return
        try:
            k = int(self.wb_k.get())            # partial input while typing raises
            g = float(self.wb_g.get())
        except Exception:
            return
        gains = gains_from_cct(k, g) if 3800 <= k <= 20000 else None
        if not gains:
            return
        r, _, b = (min(4.0, max(0.25, v)) for v in gains)
        self._wb_sync = True
        try:
            self.wb_r.set(round(r, 2)); self.wb_b.set(round(b, 2))
        finally:
            self._wb_sync = False

    def _colour_quiet(self):
        """True when RMS owns colour and podcontrol must not touch it: from
        COLOUR_QUIET_MARGIN_DEG above its night switch, through the night, until
        the sun climbs back above the same altitude at dawn. A pod with no
        platepar has no sun, and is never quiet, so it still works."""
        alt = getattr(self.ae, "sun_alt", None)
        if alt is None:
            return False
        return alt <= self.ae.cfg.night_switch_deg + COLOUR_QUIET_MARGIN_DEG

    def _note_colour(self, msg):
        self._colour_note, self._colour_note_t = msg, time.time()

    def _colour_keep(self, poll):
        """Unattended colour discipline, once per full poll (about a minute).

        RMS owns colour at its switches: the night line sets `satu 0` for mono
        science frames, the day line `ccm off` / `satu 128`. So podcontrol
        asserts the toolbar colour only in DAY mode, and only while `hold` is on:

          dusk   it goes quiet a degree of sun altitude BEFORE RMS switches, so
                 a push can never land between RMS's night line and the next
                 poll. That race held the pod in colour all night on 2026-09-22.
          night  silent. Whatever RMS set stands.
          dawn   once the sun is back above that altitude, RMS has already
                 replayed its day line, so the next poll sees the drift and
                 re-asserts the operator's settings unattended.

        The writes are live-only, so nothing reaches camera flash and any
        reboot comes up on the science baseline."""
        if self._colour_quiet():
            if self._colour_asserted:
                self._colour_asserted = False
                self._note_colour("colour handed to RMS for the night")
            return
        if not self.colour_hold.get():
            return
        drifted = [sid for sid, t in poll.items()
                   if t.get("online") and self._colour_matches(t) is False]
        if not drifted:
            return
        try:
            mode, v = self._colour_target()
            self.pod.ccm_all(mode)
            self.pod.satu_all(v)
            self._colour_asserted = True
            self._note_colour("colour re-asserted on %s" % ",".join(sorted(drifted)))
        except Exception as e:
            self._note_colour("colour re-assert failed: %s" % e)

    def _restore_colour(self):
        """Hand colour back exactly as RMS wants it for the current mode, from
        the station's own camera_settings file. Only when we actually asserted
        something, so a run that never touched colour changes nothing."""
        if not self._colour_asserted:
            return
        mode = "night" if self._colour_quiet() else "day"
        for st in self.stations:
            for cmd in st.mode_colour_cmds(mode):
                try:
                    self.pod.one_live(st.id, cmd, timeout=3)
                except Exception:
                    pass
        self._colour_asserted = False

    def _update_satu_label(self):
        """The multiplier beside the satu box -- or "inert", because with the
        colour matrix bypassed the ISP saturation does nothing at all."""
        try:
            v, mode = int(self.satu.get()), self.ccm_mode.get()
            if mode == "off":
                self.satu_x.config(text="inert", fg="#726650")
            else:
                self.satu_x.config(text="x%.2f" % (v / 128.0), fg="#f0a830")
        except Exception:
            pass

    def _colour_target(self):
        return self.ccm_mode.get(), max(0, min(255, int(self.satu.get())))

    def _colour_matches(self, tel):
        """True when a camera's polled ccm/satu equal the toolbar setting
        (None when the daemon does not report them)."""
        mode, v = self._colour_target()
        sa, cm = tel.get("satu"), tel.get("ccm")
        if not sa or not cm or sa.get("value") is None:
            return None
        if sa["value"] != v:
            return False
        if mode == "off":
            return cm.get("stage") == "bypassed"
        return cm.get("stage") == "active" and cm.get("mode") == mode

    def apply_colour(self):
        mode, v = self._colour_target()
        if self._colour_quiet():
            # RMS owns colour now; its night line sets satu 0 for mono frames.
            # Require a deliberate second click so this cannot happen by reflex.
            now = time.time()
            if now - self._colour_force_t > 10:
                self._colour_force_t = now
                self.status.config(
                    text="NIGHT: RMS owns colour (its night line sets satu 0 for mono science "
                         "frames). Click Apply again within 10 s to override anyway.", fg="#f0a830")
                return
            self._colour_force_t = 0.0
        self.status.config(text="colour: ccm %s, satu %d (x%.2f) pushed to the pod"
                                % (mode, v, v / 128.0), fg="#a4967c")
        def _push():
            try:
                self.pod.ccm_all(mode)
                self.pod.satu_all(v)
                self._colour_asserted = True
            except Exception as e:
                self.status.config(text="colour push failed: %s" % e)
        threading.Thread(target=_push, daemon=True).start()

    def apply_wb(self):
        try:
            r, g, b = (int(round(self.wb_r.get() * 256)), int(round(self.wb_g.get() * 256)),
                       int(round(self.wb_b.get() * 256)))
        except Exception:
            return
        self.status.config(text="WB %d %d %d pushed to the pod" % (r, g, b))
        if self.ae_on and self.ae.wb_base:
            self.ae.wb_base = (r / 256.0, g / 256.0, b / 256.0)     # new base for the WB rung
            self.ae._applied_wb_scale = 1.0
        threading.Thread(target=lambda: self.pod.wb_all(r, g, b), daemon=True).start()

    def wb_auto(self):
        self.status.config(text="WB auto pushed to the pod")
        threading.Thread(target=lambda: self.pod.wb_auto_all(), daemon=True).start()

    def _settings_dict(self):
        d = {"refresh_s": float(self.interval.get()), "overlay": bool(self.overlay.get()),
             "sky_constant_exposure": bool(self.const_exp.get()),
             "sun_radius_deg": float(self.sun_radius.get()), "slew": float(self.slew.get()),
             "slew_fast": float(self.slew_fast.get()),
             "satu": int(self.satu.get()), "ccm_mode": self.ccm_mode.get(),
             "vignette_on": bool(self.vig_on.get()), "vignette_coeff": float(self.vig_coeff.get()),
             "colour_hold": bool(self.colour_hold.get()),
             "sun_cam_votes": bool(self.sun_votes.get()), "wb_rung_magenta_ok": bool(self.magenta_ok.get()),
             "camera_meter": bool(self.camera_meter.get()), "individual_ae": bool(self.individual_ae.get()),
             "flare_radius_deg": float(self.flare_w.get()),
             "clip_min_blob_px": int(self.min_blob.get()), "moon_radius_deg": float(self.moon_radius.get()),
             "clip_limit_pct": float(self.clip_pct.get()), "ae_on": bool(self.ae_on),
             "wb_r": float(self.wb_r.get()), "wb_g": float(self.wb_g.get()), "wb_b": float(self.wb_b.get()),
             "history_open": bool(self.hist_win and self.hist_win.winfo_exists()),
             "geometry": self.geometry(), "view": self.view, "sky_projection": self.sky_proj.get()}
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

    def _flow_groups(self):
        """Wrap each option group's controls to the side panel's width: the widgets (built
        packed in one row) become embedded windows of a Text in their own group, which lays
        them out left to right and wraps between them. Nothing is re-created, so every
        control, variable and tip is untouched."""
        import tkinter.font as tkfont
        cw = max(1, tkfont.nametofont("TkDefaultFont").measure("0"))
        self._flows = []
        for g in self._side_groups:
            kids = g.pack_slaves()
            for k in kids:
                k.pack_forget()
            t = tk.Text(g, bg=BG, fg=BG, bd=0, highlightthickness=0, wrap="word", cursor="arrow",
                        width=max(20, (SIDE_W - 40) // cw), height=1, padx=0, pady=0, takefocus=0,
                        font=("TkDefaultFont", 2))
            for i, k in enumerate(kids):
                t.window_create("end", window=k, padx=2, pady=2, align="center")
                k.lift(t)                  # created before the Text: raise it or the Text hides it
                # a (tiny) space is where a line may break: never after a label, so a label
                # stays on the same line as the control it names
                if i + 1 < len(kids) and not isinstance(k, tk.Label):
                    t.insert("end", " ")
            t.configure(state="disabled")
            t.pack(fill="x")
            self._flows.append(t)
        # One pass is not enough. The embedded widgets have no size until Tk has
        # laid them out, so an early measurement returns almost nothing and the
        # group collapses to a bar -- which is what the busiest groups (control,
        # loop, masks & overlay, white balance) did, while the short ones
        # happened to be ready in time. Re-fit on a few delays AND whenever a
        # flow's width changes, since wrapping, and so height, depends on it.
        for ms in (300, 800, 1500, 3000):
            self.after(ms, self._fit_flows)
        self.bind("<Map>", lambda e: self.after(100, self._fit_flows), add="+")
        for t in self._flows:
            t.bind("<Configure>", self._flow_reflow, add="+")

    def _flow_reflow(self, _e=None):
        """Re-fit soon after a flow changes width, debounced: setting a height
        is itself a Configure, so an immediate re-fit would feed itself."""
        if getattr(self, "_flow_job", None):
            try:
                self.after_cancel(self._flow_job)
            except Exception:
                pass
        self._flow_job = self.after(120, self._fit_flows)

    def _fit_flows(self):
        """Size each flow Text to the height of its wrapped content."""
        import tkinter.font as tkfont
        self._flow_job = None
        for t in getattr(self, "_flows", []):
            try:
                ls = max(1, tkfont.Font(font=t.cget("font")).metrics("linespace"))   # the Text's own font
                t.update_idletasks()
                px = t.count("1.0", "end", "ypixels")
                px = px[0] if isinstance(px, tuple) else px
                if not px:
                    continue
                want = max(1, int(math.ceil(px / float(ls))))
                # only when it actually changes: a no-op configure still emits a
                # Configure event, and that would loop through _flow_reflow
                if int(t.cget("height")) != want:
                    t.configure(height=want)
            except Exception:
                pass

    def _saved_setting(self, key, default):
        try:
            return SETTINGS.load().get(key, default)
        except Exception:
            return default

    def _make_ae(self, individual):
        """A shared (one exposure for the pod) or individual (one controller per camera) AE,
        with the RMS night line as its top rung. The config object is carried over."""
        cfg = getattr(getattr(self, "ae", None), "cfg", None)
        if individual:
            from podcontrol.individualae import IndividualAE
            ae = IndividualAE(self.pod, cfg=cfg)
            if getattr(self, "dry", False):
                # each camera's controller has its OWN one-camera PodController: stub those too,
                # or a dry run writes to the cameras (it did, 2026-09-29, during a layout test)
                for sub in ae.subs.values():
                    _dry_pod(sub.pod)
        else:
            ae = SharedAE(self.pod, cfg=cfg) if cfg is not None else SharedAE(self.pod)
        configure_from_pod(ae, self.pod)
        return ae

    def _switch_ae_mode(self):
        """The individual-AE box, ticked or cleared: with AE running, hand the cameras back,
        build the other kind of controller and take over again at once."""
        want = bool(self.individual_ae.get())
        if want == bool(getattr(self.ae, "individual", False)):
            return
        if not self.ae_on:
            self.ae = self._make_ae(want)
            return
        old = self.ae
        def _swap():
            try:
                old.release()
            except Exception:
                pass
            new = self._make_ae(want)
            feed_sun(new, self.pod)
            new.takeover(self.pod.poll_all(timeout=4))
            self.ae, self.ae_info = new, None
        threading.Thread(target=_swap, daemon=True).start()

    def _ae_label(self):
        return "%s AE" % ("Individual" if getattr(self.ae, "individual", False) else "Shared")

    def toggle_ae(self):
        self.ae_on = not self.ae_on
        self._schedule_save()
        if self.ae_on and bool(self.individual_ae.get()) != bool(getattr(self.ae, "individual", False)):
            self.ae = self._make_ae(bool(self.individual_ae.get()))    # the mode switch takes effect here
        self.ae_btn.config(text="AE: %s" % ("ON" if self.ae_on else "OFF"),
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
        self.ae_btn.config(text="AE: OFF", fg="#000")
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
                # keep the result where it can be seen and re-applied: the
                # toolbar R/G/B boxes (persisted, with the Kelvin estimate);
                # the status line is overwritten by the next cycle
                r, g, b = (round(v / 256.0, 2) for v in gains[:3])
                self.after(0, lambda: (self.wb_r.set(r), self.wb_g.set(g), self.wb_b.set(b)))
                if self.ae_on and self.ae.wb_base:
                    self.ae.wb_base = (gains[0] / 256.0, gains[1] / 256.0, gains[2] / 256.0)   # new base for the WB rung
                    self.ae._applied_wb_scale = 1.0
            except Exception as e:
                self.status.config(text="WB cal failed: %s" % e)
            finally:
                self.calibrating = False
                self.cal_btn.config(text="Calibrate WB (cloud)", state="normal")

        threading.Thread(target=worker, daemon=True).start()

    def calibrate_night(self):
        """Night analog-gain calibration (podcontrol.nightcal): measure, show the
        table and the proposal, apply only on the operator's click. Everything the
        operator must see goes to the button text or a popup -- the status line is
        overwritten by the refresh loop within a second."""
        from tkinter import messagebox
        if self.calibrating:
            messagebox.showinfo("Night gain calibration", "A calibration is already running.", parent=self)
            return
        # Shared AE stays as it is. At night it is LATCHED at RMS's night line and
        # silent (it unlatches only at dawn, sun rising above -12 deg), and the
        # calibration itself refuses to run unless the sun is at or below -12 deg,
        # so the two never drive the pod at the same time. Only an AE that is on
        # and still driving (not latched) would fight the sweep.
        if self.ae_on and not getattr(self.ae, "latched", False):
            messagebox.showinfo(
                "Night gain calibration",
                "AE is still driving the pod (not yet latched at the night line).\n\n"
                "Calibrate at night, once it has latched.", parent=self)
            return
        self.calibrating = True
        self.ncal_btn.config(text="Night cal: starting…", state="disabled")
        ui = lambda f: self.after(0, f)

        def worker():
            from podcontrol import nightcal
            res, err = None, None
            try:
                def on_row(st, r):
                    t = "Night cal: %s %.1fx…" % (st.id, r["again_set"] / 1024.0)
                    ui(lambda: self.ncal_btn.config(text=t))
                res = nightcal.calibrate_pod(self.pod, on_row=on_row)
            except Exception as e:
                err = str(e)
            finally:
                self.calibrating = False
                ui(lambda: self.ncal_btn.config(text="Calibrate night gain", state="normal"))
            if res is not None:
                ui(lambda: self._night_cal_dialog(res))
            else:
                def fail():
                    messagebox.showerror("Night gain calibration", "Calibration failed:\n\n%s" % err, parent=self)
                ui(fail)

        threading.Thread(target=worker, daemon=True).start()

    def _night_cal_dialog(self, res):
        from tkinter import messagebox
        from podcontrol import nightcal
        n_ok = sum(1 for c in res["cameras"].values() if c.get("ok"))
        n_all = len(res["cameras"])
        w = tk.Toplevel(self); w.title("Pod Control — night gain calibration"); w.configure(bg=BG)

        def close():
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", close)
        head = ("Measured on %d of %d cameras." % (n_ok, n_all) +
                ("  The proposal can only reflect the measured cameras' skies." if n_ok < n_all else ""))
        tk.Label(w, text=head, bg=BG, fg="#f0a830" if n_ok < n_all else "#c8bfa8",
                 anchor="w", justify="left").pack(fill="x", padx=8, pady=(8, 2))
        txt = tk.Text(w, width=118, height=min(40, 4 + 8 * n_all), font=("Courier", 9))
        txt.insert("1.0", nightcal.format_table(res)); txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=4)
        row = tk.Frame(w, bg=BG); row.pack(fill="x", padx=8, pady=(2, 8))

        def do_apply():
            ab.config(state="disabled"); cb.config(state="disabled")
            def work():
                try:
                    notes = nightcal.apply(self.pod, res)
                    # Shared AE latches onto RMS's night line at dusk: reload it from
                    # the file just rewritten, or its next latch re-pins the old gains
                    # Shared AE latches onto RMS's night line: reload it from the file
                    # just rewritten, and if latched move its position to the new top,
                    # or its dawn unlatch would compare against the old night gains
                    configure_from_pod(self.ae, self.pod)
                    if getattr(self.ae, "latched", False):
                        self.ae.li = self.ae.target = self.ae._max_li()
                    msg = "Applied %.2fx analog, ISP 1.0625x.\n\n%s" % (res["pod_again"] / 1024.0, "\n".join(notes))
                    self.after(0, lambda: (messagebox.showinfo("Night gain calibration", msg, parent=w), close()))
                except Exception as e:
                    self.after(0, lambda: (messagebox.showerror("Night gain calibration", "Apply failed:\n\n%s" % e, parent=w), close()))
            threading.Thread(target=work, daemon=True).start()

        ab = tk.Button(row, text=("Apply %.2fx to all %d cameras + settings JSON" % (res["pod_again"] / 1024.0, n_all))
                       if res["pod_again"] else "Nothing to apply", command=do_apply,
                       state="normal" if res["pod_again"] else "disabled")
        ab.pack(side="left")
        cb = tk.Button(row, text="Cancel", command=close); cb.pack(side="left", padx=6)

    def _close(self):
        self.running = False
        try:
            SETTINGS.save(self._settings_dict())
        except Exception:
            pass
        try:
            self._restore_colour()
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
