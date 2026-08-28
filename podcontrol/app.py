"""Pod Control -- desktop app to drive a camera pod (IMX291 or Goke) as one.

Phase 1: live preview tiles + unified telemetry for every camera, slow refresh,
and Auto/Manual control across all cameras. Frame source is RMS-safe (reads
saved FramesFiles while RMS captures; grabs only when RMS is idle). The
shared-AE engine and WB cloud-gray calibrator plug in on top (podctl).

Run:  python -m podcontrol            (default pod 192.168.42.101-.106)
      python -m podcontrol --cameras 192.168.42.101-106
      python -m podcontrol --pod pod.json
"""
import time, threading, queue, os
import tkinter as tk
import cv2
from PIL import Image, ImageTk
from concurrent.futures import ThreadPoolExecutor

from podcontrol.stations import get_pod
from podcontrol.podctl import PodController
from podcontrol.frames import frame_for, luma_stats

TILE_W, TILE_H = 448, 252
COLS = 3
SRC_COLOR = {"rms": "#7fc776", "stale": "#f0a830", "grab": "#5aa9e6", "none": "#726650"}


class Tile(tk.Frame):
    def __init__(self, master, station):
        super().__init__(master, bg="#14110c", bd=1, relief="solid")
        self.station = station
        self.canvas = tk.Label(self, bg="#000", width=TILE_W, height=TILE_H)
        self.canvas.pack()
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

    def render(self, img, source, tel, luma):
        # image
        if img is None:
            self.canvas.config(image="", text="no frame", fg="#726650",
                               font=("JetBrains Mono", 16))
        else:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb).resize((TILE_W, TILE_H), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(im)
            self.canvas.config(image=self._photo, text="")
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
        self.tele.config(fg=bcol, text="%s  lum %s%s  exp %sus\nAGain %.2fx  ISO %s  %s\n%s" % (
            tel.get("platform", "?"), int(bright) if bright is not None else "-",
            (" clip%.0f%%" % (clip * 100)) if clip else "",
            tel.get("exp_us"), tel.get("again_x") or 0, tel.get("iso"),
            ("%dC" % tel["chiptemp"]) if tel.get("chiptemp") else (tel.get("optype") or ""),
            line2))


class App(tk.Tk):
    def __init__(self, allow_grab=True):
        super().__init__()
        self.title("Pod Control — pod as one camera")
        self.configure(bg="#0f0d08")
        self.stations = get_pod()
        self.pod = PodController(self.stations)
        self.allow_grab = allow_grab
        self.q = queue.Queue()
        self.interval = tk.DoubleVar(value=5.0)
        self.running = True
        self._pool = ThreadPoolExecutor(max_workers=12)

        grid = tk.Frame(self, bg="#0f0d08"); grid.pack(padx=8, pady=8)
        self.tiles = {}
        for i, s in enumerate(self.stations):
            t = Tile(grid, s)
            t.grid(row=i // COLS, column=i % COLS, padx=4, pady=4)
            self.tiles[s.id] = t

        bar = tk.Frame(self, bg="#0f0d08"); bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(bar, text="Auto All", command=self.auto_all).pack(side="left")
        tk.Label(bar, text="  refresh", fg="#c8bfa8", bg="#0f0d08").pack(side="left")
        tk.Spinbox(bar, from_=2, to=60, width=4, textvariable=self.interval).pack(side="left")
        tk.Label(bar, text="s", fg="#c8bfa8", bg="#0f0d08").pack(side="left")
        self.status = tk.Label(bar, text="starting…", fg="#a4967c", bg="#0f0d08",
                               font=("JetBrains Mono", 9), anchor="e")
        self.status.pack(side="right")

        threading.Thread(target=self._updater, daemon=True).start()
        self.after(200, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _updater(self):
        while self.running:
            t0 = time.time()
            poll = self.pod.poll_all(timeout=4)
            futs = {self._pool.submit(frame_for, s, self.allow_grab): s.id for s in self.stations}
            frames, lumas = {}, {}
            for f in futs:
                sid = futs[f]
                try:
                    img, src = f.result(timeout=18)
                except Exception:
                    img, src = None, "none"
                frames[sid] = (img, src)
                lumas[sid] = luma_stats(img)
            self.q.put((frames, poll, lumas, time.time() - t0))
            for _ in range(int(self.interval.get() * 10)):
                if not self.running:
                    return
                time.sleep(0.1)

    def _drain(self):
        try:
            while True:
                frames, poll, lumas, dt = self.q.get_nowait()
                brights = []
                for sid, t in self.tiles.items():
                    img, src = frames.get(sid, (None, "none"))
                    t.render(img, src, poll.get(sid), lumas.get(sid))
                    b = (poll.get(sid) or {}).get("avelum")
                    if b is None and lumas.get(sid):
                        b = lumas[sid]["mean"]
                    if b is not None:
                        brights.append(b)
                daemons = sum(1 for v in poll.values() if v.get("online"))
                self.status.config(text="%d/%d daemon  bright %s  (%.1fs)  %s" % (
                    daemons, len(self.stations),
                    ("%d–%d" % (int(min(brights)), int(max(brights)))) if brights else "—",
                    dt, time.strftime("%H:%M:%S")))
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def auto_all(self):
        threading.Thread(target=lambda: self.pod.auto_all(), daemon=True).start()

    def _close(self):
        self.running = False
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
    App(allow_grab=not args.no_grab).mainloop()


if __name__ == "__main__":
    main()
