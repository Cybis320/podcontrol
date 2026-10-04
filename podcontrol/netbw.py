"""Per-camera stream bandwidth, from the kernel's own TCP counters.

RMS pulls every camera over RTSP on TCP 554, so each stream is a socket whose
cumulative bytes_received the kernel already keeps. One `ss` call reads all of
them and the rate is the delta between two calls: nothing is asked of a camera
and no frame is decoded.

Worth having because the failure it catches is invisible otherwise. On
2026-10-03 the link renegotiated from 1000 to 100 Mb/s; at ~56 Mb/s per camera
six streams need ~337, so only a couple could run, frame availability fell to
21% and the shared AE went blind for four hours. The diagnosis took an
afternoon. "337 / 100 Mb/s" would have taken a second.

STATE IS BOUNDED, because this runs 24/7: one previous reading per camera IP,
so the dict can never hold more than the pod. Nothing accumulates per sample.
"""
import re
import subprocess
import time

_PEER = re.compile(r'(\d+\.\d+\.\d+\.\d+):554\s*$')
_RECV = re.compile(r'bytes_received:(\d+)')
MIN_DT = 1.0          # ignore a pair of samples closer than this: the delta is noise
SAMPLE_COST_MS = 14   # measured; the caller may sample every Nth cycle instead


def _read():
    """{camera_ip: total bytes received over its RTSP sockets}, or {} on failure.

    Summed per IP, because a camera that reconnects leaves the old socket draining
    for a while and RMS can briefly hold two."""
    try:
        out = subprocess.run(["ss", "-tin", "state", "established"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=5).stdout.decode("latin1")
    except (OSError, subprocess.SubprocessError):
        return {}
    acc, ip = {}, None
    for line in out.splitlines():
        m = _PEER.search(line.rstrip())
        if m:
            ip = m.group(1)
            continue
        if ip:
            r = _RECV.search(line)
            if r:
                acc[ip] = acc.get(ip, 0) + int(r.group(1))
            ip = None
    return acc


def device(ip="192.168.42.201"):
    """The interface that routes to the cameras, or None. A subprocess, so this
    is resolved once: the route does not change under us, the SPEED does."""
    try:
        o = subprocess.run(["ip", "route", "get", ip], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=3).stdout.decode()
        return re.search(r"dev (\S+)", o).group(1)
    except Exception:
        return None


def speed_of(dev):
    """Current link speed in Mb/s, or None.

    Re-read every cycle on purpose, and cheap enough to be (one sysfs read,
    ~0.08 ms): a link that renegotiates from 1000 to 100 mid-run is the exact
    fault this feature exists to show, and reading it once at startup would miss
    precisely that."""
    if not dev:
        return None
    try:
        with open("/sys/class/net/%s/speed" % dev) as f:
            v = int(f.read().strip())
        return v if v > 0 else None       # -1 while a link is down or renegotiating
    except (OSError, ValueError):
        return None


def link(ip="192.168.42.201"):
    d = device(ip)
    return d, speed_of(d)


class Meter:
    """Rates between successive calls. Holds one reading per camera, nothing more."""

    def __init__(self, stations):
        self.by_ip = {st.ip: st.id for st in stations}
        self._prev = {}          # ip -> (epoch, bytes); at most one entry per camera
        self.dev = device(next(iter(self.by_ip), "192.168.42.201"))
        self.speed = speed_of(self.dev)

    def rates(self):
        """{station_id: Mb/s} for cameras seen in BOTH samples, plus the total.

        A camera missing from either sample, or whose counter went backwards
        (the socket was replaced), yields no rate this cycle rather than a
        fabricated one."""
        now = time.time()
        self.speed = speed_of(self.dev)       # a renegotiation shows the same cycle
        cur = _read()
        out = {}
        for ip, b in cur.items():
            sid = self.by_ip.get(ip)
            if sid is None:
                continue                      # not one of ours
            prev = self._prev.get(ip)
            if prev is not None:
                t0, b0 = prev
                dt = now - t0
                if dt >= MIN_DT and b >= b0:
                    out[sid] = 8.0 * (b - b0) / dt / 1e6
        # keep ONLY the cameras we know: a stray peer can never grow this dict
        self._prev = {ip: (now, b) for ip, b in cur.items() if ip in self.by_ip}
        total = sum(out.values()) if out else None
        return out, total

    def refresh_device(self):
        """Re-resolve the interface; only needed if routing changes."""
        self.dev = device(next(iter(self.by_ip), "192.168.42.201"))
        self.speed = speed_of(self.dev)
