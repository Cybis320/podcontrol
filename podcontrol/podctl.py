"""PodController: talk to every camera's :9600 ISP daemon, platform-agnostically.

Two firmware families, one control layer:
  * Goke GK7205V200 (isp_ctl):  query / manual -a/-d/-i/-e / auto
        query -> rich telemetry incl. AveLum + ChipTemp; NO wb/venc in daemon.
  * Hi3516CV300 IMX291 (hisp_ctl): ae / wb / venc_qp / venc_cqp / gain / exp /
        drc / nr / ... plus the SAME query/manual/auto vocab for parity.
        Rich control, but query has NO AveLum/ChipTemp -> meter from the frame.

We detect the platform per camera and normalize telemetry into one schema.
Control uses manual/auto (both families support it); wb/qp are IMX291-only for
now (see the parity audit -- adding them to the Goke daemon closes the gap).

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
    again = n("AGain")
    return {
        "again_x": (again / 1024.0) if again else None,
        "sysgain_x": None,   # derivable if needed
        "exp_us": n("ExpTime"), "iso": n("ISO"),
        "avelum": n("AveLum"), "chiptemp": n("ChipTemp"),
        "optype": _f(q, r"OpType:\s*(\w+)"),
        "exp_max": (_f(q, r"ExposureMAX:\s*(\w+)") or "").lower() == "yes",
    }


def _parse_ae_line(text):
    def x(k):
        v = _f(text, k + r"=([\d.]+)x")
        return float(v) if v else None
    us = _f(text, r"~(\d+)\s*us")
    return {
        "again_x": x("AGain"), "sysgain_x": x("SysGain"),
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
    return {"minqp": int(mn) if mn else None, "maxqp": int(mx) if mx else None}


def poll(ip, timeout=5.0):
    """Unified telemetry for one camera. platform in {goke, imx291, None}."""
    q = send(ip, "query", timeout)
    if q is None:
        return {"online": False, "platform": None}
    if "Exposure Info" in q:                     # Goke
        return {"online": True, "platform": "goke", "wb": None, "qp": None, **_parse_goke(q)}
    if "ae:" in q or "AGain=" in q:              # IMX291 hisp_ctl
        t = {"online": True, "platform": "imx291", **_parse_ae_line(q)}
        t["wb"] = _parse_wb(send(ip, "wb", timeout))
        t["qp"] = _parse_qp(send(ip, "venc_qp", timeout))
        return t
    return {"online": True, "platform": "unknown", "raw": q}


class PodController:
    def __init__(self, stations):
        self.stations = list(stations)
        self._pool = ThreadPoolExecutor(max_workers=max(4, 2 * len(self.stations)))

    def poll_all(self, timeout=5.0):
        futs = {s.id: self._pool.submit(poll, s.ip, timeout) for s in self.stations}
        return {sid: f.result() for sid, f in futs.items()}

    def _bcast(self, cmd, timeout=5.0):
        futs = {s.id: self._pool.submit(send, s.ip, cmd, timeout) for s in self.stations}
        return {sid: f.result() for sid, f in futs.items()}

    def manual_all(self, again=None, dgain=None, ispdgain=None, exp_us=None, timeout=5.0):
        p = ["manual"]
        if again is not None:    p += ["-a", str(int(again))]
        if dgain is not None:    p += ["-d", str(int(dgain))]
        if ispdgain is not None: p += ["-i", str(int(ispdgain))]
        if exp_us is not None:   p += ["-e", str(int(exp_us))]
        return self._bcast(" ".join(p), timeout)

    def auto_all(self, timeout=5.0):
        return self._bcast("auto", timeout)

    def one(self, station_id, cmd, timeout=5.0):
        st = next((s for s in self.stations if s.id == station_id), None)
        return send(st.ip, cmd, timeout) if st else None


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    pod = PodController(get_pod())
    print("%-8s %-7s %6s %6s %7s %5s %s" % ("cam","plat","again","expus","avelum","iso","op/temp"))
    for sid, d in pod.poll_all(timeout=4).items():
        if not d.get("online"):
            print("%-8s OFFLINE" % sid); continue
        print("%-8s %-7s %6s %6s %7s %5s %s" % (
            sid, d.get("platform"), d.get("again_x"), d.get("exp_us"),
            d.get("avelum"), d.get("iso"), d.get("optype") or d.get("chiptemp")))
