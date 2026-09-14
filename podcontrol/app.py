"""Pod Control -- desktop app to drive a camera pod (IMX291 or Goke) as one.

Phase 1: live preview tiles + unified telemetry for every camera, slow refresh,
and Auto/Manual control across all cameras. Frame source is RMS-safe (reads
saved FramesFiles while RMS captures; grabs only when RMS is idle). The
shared-AE engine and WB cloud-gray calibrator plug in on top (podctl).

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
from podcontrol.frames import frame_for, fresh_frame, luma_stats, mask_for
from podcontrol.sharedae import SharedAE, _pod_platform

TILE_W, TILE_H = 448, 252
COLS = 3
SRC_COLOR = {"rms": "#7fc776", "stale": "#f0a830", "grab": "#5aa9e6", "none": "#726650"}
# translucent overlay tints (RGB) for the excluded zones
TINT_STATIC, TINT_SUN = (220, 60, 60), (255, 190, 40)
OVERLAY_ALPHA = 0.45


class Tile(tk.Frame):
    def __init__(self, master, station, on_select=None):
        super().__init__(master, bg="#14110c", bd=1, relief="solid")
        self.station = station
        self.on_select = on_select
        self.canvas = tk.Canvas(self, width=TILE_W, height=TILE_H, bg="#000",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.frame_wh = (1920, 1080)
        self.sel = None                     # selection box in tile coords
        self._img_id = self._rect_id = self._txt_id = None
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        head = tk.Frame(self, bg="#14110c"); head.pack(fill="x", padx=6, pady=(2, 0))
        self.title = tk.Label(head, text=station.id, fg="#f0a830", bg="#14110c",
                              font=("JetBrains Mono", 11, "bold"))
        self.title.pack(side="left")
        self.badge = tk.Label(head, text="", fg="#726650", bg="#14110c",
                              font=("JetBrains Mono", 8))
        self.badge.pack(side="right")
        self.tele = tk.Label(self, text="…", fg="#c8bfa8", bg="#14110c",
                             font=("JetBrains Mono", 9), anchor="w", justify="left")
        self.tele.pack(fill="x", padx=6, pady=(0, 4))
        self._photo = None

    # --- region selection (drag a box on a grey cloud) --------------------
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
        fw, fh = self.frame_wh
        sx, sy = fw / TILE_W, fh / TILE_H
        x0, y0, x1, y1 = self.sel
        return (int(min(x0, x1) * sx), int(min(y0, y1) * sy),
                int(max(x0, x1) * sx), int(max(y0, y1) * sy))

    def render(self, img, source, tel, luma, layers=None, overlay=True):
        # image (Canvas: keep image + selection rectangle); with overlay on,
        # excluded zones are tinted (red = RMS mask, orange = sun zone) so
        # what the metering ignores is visible
        if img is None:
            if self._img_id:
                self.canvas.delete(self._img_id); self._img_id = None
            if not self._txt_id:
                self._txt_id = self.canvas.create_text(
                    TILE_W // 2, TILE_H // 2, text="no frame", fill="#726650",
                    font=("JetBrains Mono", 16))
        else:
            if self._txt_id:
                self.canvas.delete(self._txt_id); self._txt_id = None
            self.frame_wh = (img.shape[1], img.shape[0])
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            small = cv2.resize(rgb, (TILE_W, TILE_H), interpolation=cv2.INTER_AREA)
            if overlay and layers:
                for key, tint in (("static", TINT_STATIC), ("sun", TINT_SUN)):
                    ex = layers.get(key)
                    if ex is None:
                        continue
                    m = cv2.resize(ex.astype(np.uint8), (TILE_W, TILE_H),
                                   interpolation=cv2.INTER_NEAREST).astype(bool)
                    small[m] = ((1 - OVERLAY_ALPHA) * small[m] + OVERLAY_ALPHA * np.array(tint)).astype(np.uint8)
                si = layers.get("sun_info")
                if si and si.get("in_fov") and si.get("x") is not None:
                    fw, fh = self.frame_wh
                    cx, cy = int(si["x"] * TILE_W / fw), int(si["y"] * TILE_H / fh)
                    cv2.circle(small, (cx, cy), 6, (255, 255, 255), 1)
            im = Image.fromarray(small)
            self._photo = ImageTk.PhotoImage(im)
            if self._img_id:
                self.canvas.itemconfig(self._img_id, image=self._photo)
            else:
                self._img_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
            if self._rect_id:
                self.canvas.tag_raise(self._rect_id)
        self.badge.config(text=source, fg=SRC_COLOR.get(source, "#726650"))
        # telemetry
        if not tel or not tel.get("online"):
            self.title.config(fg="#b3402a")
            self.tele.config(text="no daemon (preview only)" if img is not None else "offline",
                             fg="#b3402a")
            return
        self.title.config(fg="#f0a830")
        # brightness: daemon AveLum (Goke) or frame mean (IMX291)
        bright = tel.get("avelum")
        clip = None
        if bright is None and luma:
            bright = luma["mean"]; clip = luma["clip"]
        bcol = "#7fc776" if (bright or 0) < 170 else ("#f0a830" if bright < 220 else "#ff7355")
        wb = tel.get("wb"); qp = tel.get("qp")
        line2 = ""
        if wb and wb.get("gains"):
            line2 += "wb %s " % ("auto" if wb.get("op") == "auto" else "%.2f/%.2f" % (wb["gains"][0], wb["gains"][-1]))
        if qp and qp.get("maxqp") is not None:
            line2 += "qp %s" % qp["maxqp"]
        masked = (" mask%.0f%%" % (100 * luma["masked"])) if luma and luma.get("masked") else ""
        si = (layers or {}).get("sun_info")
        if si and si.get("alt") is not None and si["alt"] > -si["radius_deg"]:
            masked += "  sun %.0f\u00b0%s" % (si["alt"], " IN FOV" if si.get("in_fov") else "")
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
            line2))


class App(tk.Tk):
    def __init__(self, allow_grab=True):
        super().__init__()
        self.title("Pod Control — pod as one camera")
        self.configure(bg="#0f0d08")
        self.stations = get_pod()
        self.by_id = {s.id: s for s in self.stations}
        self.pod = PodController(self.stations)
        self.allow_grab = allow_grab
        self.ae = SharedAE(self.pod)
        self.ae_on = False
        self.ae_info = None
        self.selected = None      # station id with an active region selection
        self.calibrating = False
        self.q = queue.Queue()
        self.interval = tk.DoubleVar(value=5.0)
        self.overlay = tk.BooleanVar(value=True)
        self.sun_radius = tk.DoubleVar(value=F.SUN_RADIUS_DEG[0])
        self.running = True
        self._pool = ThreadPoolExecutor(max_workers=12)

        grid = tk.Frame(self, bg="#0f0d08"); grid.pack(padx=8, pady=8)
        self.tiles = {}
        for i, s in enumerate(self.stations):
            t = Tile(grid, s, on_select=self._on_select)
            t.grid(row=i // COLS, column=i % COLS, padx=4, pady=4)
            self.tiles[s.id] = t

        bar = tk.Frame(self, bg="#0f0d08"); bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(bar, text="Auto All", command=self.auto_all).pack(side="left")
        self.ae_btn = tk.Button(bar, text="Shared AE: OFF", command=self.toggle_ae)
        self.ae_btn.pack(side="left", padx=6)
        self.cal_btn = tk.Button(bar, text="Calibrate WB (cloud)", command=self.calibrate_wb)
        self.cal_btn.pack(side="left", padx=6)
        tk.Label(bar, text="  refresh", fg="#c8bfa8", bg="#0f0d08").pack(side="left")
        tk.Spinbox(bar, from_=2, to=60, width=4, textvariable=self.interval).pack(side="left")
        tk.Label(bar, text="s", fg="#c8bfa8", bg="#0f0d08").pack(side="left")
        tk.Checkbutton(bar, text="mask overlay", variable=self.overlay, fg="#c8bfa8", bg="#0f0d08",
                       selectcolor="#0f0d08", activebackground="#0f0d08").pack(side="left", padx=(12, 0))
        tk.Label(bar, text="sun r", fg="#c8bfa8", bg="#0f0d08").pack(side="left", padx=(8, 0))
        tk.Spinbox(bar, from_=0, to=45, increment=1, width=4, textvariable=self.sun_radius).pack(side="left")
        tk.Label(bar, text="\u00b0", fg="#c8bfa8", bg="#0f0d08").pack(side="left")
        self.status = tk.Label(bar, text="starting…", fg="#a4967c", bg="#0f0d08",
                               font=("JetBrains Mono", 9), anchor="e")
        self.status.pack(side="right")

        threading.Thread(target=self._updater, daemon=True).start()
        self.after(200, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _updater(self):
        while self.running:
            t0 = time.time()
            try:
                F.set_sun_radius(self.sun_radius.get())
            except Exception:
                pass
            poll = self.pod.poll_all(timeout=4)
            futs = {self._pool.submit(frame_for, s, self.allow_grab, True): s.id
                    for s in self.stations}
            frames, lumas, layers = {}, {}, {}
            for f in futs:
                sid = futs[f]
                try:
                    img, src, tcap = f.result(timeout=18)
                except Exception:
                    img, src, tcap = None, "none", None
                frames[sid] = (img, src)
                keep, layers[sid] = mask_for(self.by_id[sid], img, tcap, layers=True)
                lumas[sid] = luma_stats(img, keep)
                if lumas[sid] is not None:
                    lumas[sid]["t"] = tcap
            if self.ae_on:
                ctl = {sid: lumas[sid] for sid in lumas if poll.get(sid, {}).get("online")}
                ctl = self.ae.fresh(ctl)       # only frames newer than the last change
                info = self.ae.step(ctl) if ctl else None
                if info:
                    try:
                        self.ae.apply(platform=_pod_platform(poll))
                    except Exception:
                        pass
                    self.ae_info = info
                elif self.ae_info:
                    self.ae_info = dict(self.ae_info, reason="waiting for fresh frames")
            self.q.put((frames, poll, lumas, layers, time.time() - t0))
            for _ in range(int(self.interval.get() * 10)):
                if not self.running:
                    return
                time.sleep(0.1)

    def _drain(self):
        try:
            while True:
                frames, poll, lumas, layers, dt = self.q.get_nowait()
                brights = []
                for sid, t in self.tiles.items():
                    img, src = frames.get(sid, (None, "none"))
                    t.render(img, src, poll.get(sid), lumas.get(sid),
                             layers.get(sid), self.overlay.get())
                    b = (poll.get(sid) or {}).get("avelum")
                    if b is None and lumas.get(sid):
                        b = lumas[sid]["mean"]
                    if b is not None:
                        brights.append(b)
                daemons = sum(1 for v in poll.values() if v.get("online"))
                ae = ""
                if self.ae_on and self.ae_info:
                    i = self.ae_info
                    ae = "  AE %s exp=%dus gain=%.1fx" % (i["reason"], i["exp_us"], i["total_gain_x"])
                self.status.config(text="%d/%d daemon  bright %s%s  (%.1fs)  %s" % (
                    daemons, len(self.stations),
                    ("%d–%d" % (int(min(brights)), int(max(brights)))) if brights else "—",
                    ae, dt, time.strftime("%H:%M:%S")))
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def toggle_ae(self):
        self.ae_on = not self.ae_on
        self.ae_btn.config(text="Shared AE: %s" % ("ON" if self.ae_on else "OFF"),
                           fg=("#7fc776" if self.ae_on else "#000"))
        if self.ae_on:
            # start from the cameras' current (darkest) exposure, not a fixed
            # mid-ladder guess that blows out a daytime scene
            self.ae_info = None
            threading.Thread(target=lambda: self.ae.takeover(self.pod.poll_all(timeout=4)),
                             daemon=True).start()
        else:
            threading.Thread(target=lambda: self.ae.release(), daemon=True).start()

    def auto_all(self):
        self.ae_on = False
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
