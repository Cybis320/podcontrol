"""PodController: talk to every camera's :9600 ISP daemon, platform-agnostically.

Two firmware families, one control layer, ONE command vocabulary:
  * Goke GK7205V200 (isp_ctl, OpenIPC fleet .201-.206): query / manual
        -a/-d/-i/-e / auto / wb / venc_qp / venc_cqp / venc_gop / persist ...
        query -> rich telemetry incl. AveLum (+ChipTemp on XM builds).
  * Hi3516CV300 IMX291 (hisp_ctl, .101-.106): ae / wb / venc_qp / venc_cqp /
        gain / exp / drc / nr / ... plus the SAME query/manual/auto vocab.
        query has NO AveLum/ChipTemp -> meter from the frame.

Since the 2026-09 parity port both daemons speak the same wb/venc_* syntax
(x256 WB gains, 256 = 1.0x), so telemetry and control are parsed identically.

One command per connection; an offline camera returns None and reconnects next
cycle -- naturally resilient to the day/night SwitchMode reboots.
"""
import socket, re
from concurrent.futures import ThreadPoolExecutor

PORT = 9600


def send(ip, cmd, timeout=5.0):
    try:
        s = socket.create_connection((ip, PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall((cmd + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            try:
                d = s.recv(4096)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
        s.close()
        return buf.decode("latin1").strip()
    except Exception:
        return None


def _f(text, pat):
    m = re.search(pat, text)
    return m.group(1) if m else None


def _parse_goke(q):
    def n(k):
        v = _f(q, k + r":\s*(-?\d+)")
        return int(v) if v is not None else None
    again = n("AGain"); dg = n("DGain"); ispd = n("ISPDGain")
    # "Auto ranges" block: ExpTime: [30 .. 39970] / AGain: [1024 .. 22924] ...
    ranges = {}
    for k in ("ExpTime", "AGain", "DGain", "ISPDGain", "SysGain"):
        m = re.search(k + r":\s*\[(\d+)\s*\.\.\s*(\d+)\]", q)
        if m:
            ranges[k] = (int(m.group(1)), int(m.group(2)))
    # per-stage op types + pinned values: "AGain: OpType=MANUAL val=1513"
    stages = {}
    for k in ("ExpTime", "AGain", "DGain", "ISPDGain"):
        m = re.search(k + r":\s*OpType=(\w+)\s+val=(\d+)", q)
        if m:
            stages[k] = (m.group(1), int(m.group(2)))
    return {
        "again_x": (again / 1024.0) if again else None,
        "dgain_x": (dg / 1024.0) if dg else None,
        "ispdgain_x": (ispd / 1024.0) if ispd else None,
        "sysgain_x": None,   # derivable if needed
        "exp_us": n("ExpTime"), "iso": n("ISO"),
        "avelum": n("AveLum"), "chiptemp": n("ChipTemp"),
        "optype": _f(q, r"OpType:\s*(\w+)"),
        "exp_max": (_f(q, r"ExposureMAX:\s*(\w+)") or "").lower() == "yes",
        "ranges": ranges, "stages": stages,
    }


def _parse_ae_line(text):
    def x(k):
        v = _f(text, k + r"=([\d.]+)x")
        return float(v) if v else None
    us = _f(text, r"~(\d+)\s*us")
    return {
        "again_x": x("AGain"), "sysgain_x": x("SysGain"), "dgain_x": None, "ispdgain_x": None,
        "exp_us": int(us) if us else None,
        "iso": int(_f(text, r"ISO=(\d+)") or 0) or None,
        "avelum": None, "chiptemp": None, "optype": None, "exp_max": None,
    }


def _parse_wb(text):
    if not text:
        return None
    gains = re.findall(r"[RGB][rb]?=([\d.]+)", text)
    return {"op": _f(text, r"op=(\w+)"), "gains": [float(g) for g in gains] or None}


def _parse_qp(text):
    if not text:
        return None
    mn = _f(text, r"MinQp=(\d+)"); mx = _f(text, r"MaxQp=(\d+)")
    rc = _f(text, r"rc=(\d+)")
    return {"minqp": int(mn) if mn else None, "maxqp": int(mx) if mx else None,
            "rc": int(rc) if rc else None}


def _parse_cqp(text):
    if not text:
        return None
    v = _f(text, r"chroma_qp_index_offset=(-?\d+)")
    return int(v) if v is not None else None


def _parse_gop(text):
    if not text:
        return None
    g = _f(text, r"Gop=(\d+)"); br = _f(text, r"BitRate=(\d+)")
    return {"gop": int(g) if g else None, "bitrate_kbps": int(br) if br else None,
            "rcmode": _f(text, r"RcMode=(\w+)")}


def _encoder_telemetry(ip, timeout):
    """wb / venc_qp / venc_cqp / venc_gop -- same syntax on both daemons.
    A daemon without one of them just returns an ERROR line -> None field."""
    return {
        "wb": _parse_wb(send(ip, "wb", timeout)),
        "qp": _parse_qp(send(ip, "venc_qp", timeout)),
        "cqp": _parse_cqp(send(ip, "venc_cqp", timeout)),
        "gop": _parse_gop(send(ip, "venc_gop", timeout)),
    }


def poll(ip, timeout=5.0, full=True):
    """Unified telemetry for one camera. platform in {goke, imx291, None}.
    full=False asks only `query` (exposure) and skips the wb/venc_* reads --
    those never change on their own, so the app reads them once a minute and
    keeps the per-cycle load on the camera daemons to one connection."""
    q = send(ip, "query", timeout)
    if q is None:
        return {"online": False, "platform": None}
    enc = _encoder_telemetry(ip, timeout) if full else {}
    if "Exposure Info" in q:                     # Goke isp_ctl
        return {"online": True, "platform": "goke", **_parse_goke(q), **enc}
    if "ae:" in q or "AGain=" in q:              # IMX291 hisp_ctl
        return {"online": True, "platform": "imx291", **_parse_ae_line(q), **enc}
    return {"online": True, "platform": "unknown", "raw": q}


class PodController:
    def __init__(self, stations):
        self.stations = list(stations)
        self._pool = ThreadPoolExecutor(max_workers=max(4, 2 * len(self.stations)))

    def __init_cache(self):
        if not hasattr(self, "_enc_cache"):
            self._enc_cache = {}

    def poll_all(self, timeout=5.0, full=True):
        """full=False: exposure only per camera; the encoder/WB fields are
        filled from the last full poll so callers see a complete record."""
        self.__init_cache()
        futs = {s.id: self._pool.submit(poll, s.ip, timeout, full) for s in self.stations}
        out = {sid: f.result() for sid, f in futs.items()}
        for sid, d in out.items():
            if full and d.get("online"):
                self._enc_cache[sid] = {k: d.get(k) for k in ("wb", "qp", "cqp", "gop")}
            elif not full and d.get("online"):
                for k, v in (self._enc_cache.get(sid) or {}).items():
                    d.setdefault(k, v)
        return out

    def _bcast(self, cmd, timeout=5.0):
        futs = {s.id: self._pool.submit(send, s.ip, cmd, timeout) for s in self.stations}
        return {sid: f.result() for sid, f in futs.items()}

    def manual_all(self, again=None, dgain=None, ispdgain=None, exp_us=None, timeout=5.0):
        """Pin exposure/gains on every camera. Any stage left None stays in
        AUTO on the camera and keeps floating per camera (seen live: sensor
        DGain 1.0x..3.4x across the pod at the same -a/-e) -- callers that
        want the pod in sync must pass ALL of again/dgain/ispdgain/exp_us."""
        p = ["manual"]
        if again is not None:    p += ["-a", str(int(again))]
        if dgain is not None:    p += ["-d", str(int(dgain))]
        if ispdgain is not None: p += ["-i", str(int(ispdgain))]
        if exp_us is not None:   p += ["-e", str(int(exp_us))]
        return self._bcast(" ".join(p), timeout)

    # --- handing cameras back --------------------------------------------
    @staticmethod
    def restore_cmd(station, tel):
        """The command that puts a camera back exactly as it was before we took
        over, from its telemetry snapshot `tel` (poll()). Prefers RMS's own
        day/night line from the station's camera_settings file (day if the
        camera was in AUTO, night if MANUAL); otherwise rebuilds it from the
        reported auto ranges / pinned values. A bare 'auto' is NEVER used: on
        the Goke it resets every AE range to the driver default (sensor DGain
        max 126x), silently breaking the science config's DGain=1x."""
        # which mode RMS is in: from the sun (its -9 deg rule), NOT from the
        # camera's op type -- a camera we pinned looks "manual" either way
        mode = None
        try:
            from podcontrol import sunmask
            mode = sunmask.mode_for(station)
        except Exception:
            mode = None
        auto = (tel or {}).get("optype", "AUTO") != "MANUAL" if mode is None else (mode == "day")
        line = station.mode_cmd("day" if auto else "night") if hasattr(station, "mode_cmd") else None
        if line:
            return line
        r = (tel or {}).get("ranges") or {}
        st = (tel or {}).get("stages") or {}
        if auto:
            cmd = "auto"
            if "ExpTime" in r:
                cmd += " --min-exptime %d --max-exptime %d" % r["ExpTime"]
            if "AGain" in r:
                cmd += " --max-again %d" % r["AGain"][1]
            cmd += " --max-dgain %d" % (r["DGain"][1] if "DGain" in r else 1024)
            if "SysGain" in r:
                cmd += " --max-sysgain %d" % r["SysGain"][1]
            return cmd
        if st:
            a = st.get("AGain", (None, 1024))[1]; e = st.get("ExpTime", (None, 39941))[1]
            i = st.get("ISPDGain", (None, 1024))[1]
            return "manual -a %d -d 1024 -i %d -e %d" % (a, i, e)
        return "auto --max-dgain 1024"

    def snapshot(self, poll):
        """{station_id: telemetry} of every online camera at takeover. The
        restore command is built at RELEASE time (restore_cmd), because the
        right hand-back line depends on the sun THEN, not at takeover: on
        2026-09-16 the snapshot was taken at night and replayed at 09:00
        local when the app was closed -> the night line (40 ms, 45x) in full
        sun, every frame white, and the restarted shared AE needed 30 min to
        slew the 16 stops back down."""
        out = {}
        for s in self.stations:
            t = poll.get(s.id) or {}
            if t.get("online"):
                out[s.id] = dict(t)
        return out

    def release(self, snap=None, timeout=5.0):
        """Hand every camera back with RMS's line for the CURRENT day/night
        mode (restore_cmd on the takeover telemetry), else the station's day
        line, else a DGain-safe auto. A snapshot entry may also be a ready
        command string (older callers)."""
        futs = {}
        for s in self.stations:
            tel = (snap or {}).get(s.id)
            if isinstance(tel, str):
                cmd = tel
            elif tel:
                cmd = self.restore_cmd(s, tel)
            else:
                cmd = None
            cmd = cmd or s.mode_cmd("day") or "auto --max-dgain 1024"
            futs[s.id] = self._pool.submit(send, s.ip, cmd, timeout)
        return {sid: f.result() for sid, f in futs.items()}

    def auto_all(self, timeout=5.0):
        """'Auto All': every camera to RMS's DAY exposure line (never bare auto)."""
        return self.release(None, timeout)

    def wb_all(self, R, G, B, timeout=5.0):
        """Set the same manual WB (x256 gains, 256=1.0x) on every camera."""
        return self._bcast("wb %d %d %d" % (int(R), int(G), int(B)), timeout)

    def wb_auto_all(self, timeout=5.0):
        return self._bcast("wb auto", timeout)

    def wb_read(self, station_id, timeout=5.0):
        return _parse_wb(self.one(station_id, "wb", timeout))

    # --- encoder (same syntax both platforms; chn 0 = "the" channel) --------
    def venc_qp_all(self, maxqp, minqp, timeout=5.0):
        """Luma QP window on every camera (min==max pins a constant QP)."""
        return self._bcast("venc_qp 0 cap %d %d" % (int(maxqp), int(minqp)), timeout)

    def venc_cqp_all(self, offset, timeout=5.0):
        """Chroma QP index offset [-12..12] on every camera."""
        return self._bcast("venc_cqp 0 %d" % int(offset), timeout)

    def venc_gop_all(self, gop, bitrate_kbps=None, timeout=5.0):
        cmd = "venc_gop 0 %d" % int(gop)
        if bitrate_kbps is not None:
            cmd += " %d" % int(bitrate_kbps)
        return self._bcast(cmd, timeout)

    def one(self, station_id, cmd, timeout=5.0):
        st = next((s for s in self.stations if s.id == station_id), None)
        return send(st.ip, cmd, timeout) if st else None


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    pod = PodController(get_pod())
    print("%-8s %-7s %6s %6s %7s %5s %-6s %-16s %-9s %-4s %s" % (
        "cam","plat","again","expus","avelum","iso","op","wb(R/G/B)","qp","cqp","gop/kbps"))
    for sid, d in pod.poll_all(timeout=4).items():
        if not d.get("online"):
            print("%-8s OFFLINE" % sid); continue
        wb = d.get("wb") or {}; qp = d.get("qp") or {}; gop = d.get("gop") or {}
        g = wb.get("gains") or []
        wbs = ("%s %s" % (wb.get("op"), "/".join("%.2f" % x for x in (g[0], g[1], g[-1])) if len(g) >= 3 else "")).strip()
        print("%-8s %-7s %6.2f %6s %7s %5s %-6s %-16s %-9s %-4s %s  D%.2f I%.2f" % (
            sid, d.get("platform"), d.get("again_x") or 0, d.get("exp_us"),
            d.get("avelum"), d.get("iso"), d.get("optype") or d.get("chiptemp") or "",
            wbs, "%s/%s" % (qp.get("maxqp"), qp.get("minqp")), d.get("cqp"),
            "%s/%s" % (gop.get("gop"), gop.get("bitrate_kbps")),
            d.get("dgain_x") or 0, d.get("ispdgain_x") or 0))
