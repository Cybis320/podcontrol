"""Each camera's real code <-> linear-light curve, from the camera itself.

The cameras encode light with an exact gamma 0.5 only AT THE NODES of their gamma table;
the ISP draws straight lines between nodes (CV300: one node per 16 linear units, Goke:
one per 4). Below the first node the curve is linear, not a square root -- CV300 codes
0-16, Goke 0-8 -- so decoding a frame as (code/255)^2 is wrong in the darkest codes by up
to 2x. Above ~code 20 the two agree within 1%.

Since 2026-10-03 every camera answers `gamma decode` with the linear value (12-bit,
4095 = full scale) each 8-bit code stands for, inverted from its LIVE table. This module
fetches that once per camera, caches it, and offers both directions. A camera that does
not answer (an older image, or unreachable) falls back to the pure 0.5 curve, and
`source(sid)` says so.
"""
import threading

import numpy as np

from podcontrol.podctl import send

_PURE = (np.arange(256, dtype=np.float64) ** 2) * (4095.0 / 65025.0)
_cache = {}                 # sid -> (np.array(256), source)
_lock = threading.Lock()


def fetch(station, timeout=5.0):
    """Ask one camera for its table (blocking). Returns the source: 'table', 'identity',
    'pow2' (camera fell back) or 'assumed' (no answer: pure 0.5 assumed here)."""
    r = send(station.ip, "gamma decode", timeout=timeout) or ""
    parts = r.split()
    table, src = None, "assumed"
    if len(parts) >= 3 + 256 and parts[0] == "gamma_decode" and parts[1].startswith("decode="):
        try:
            v = np.array([float(x) for x in parts[3:3 + 256]], dtype=np.float64)
            if np.all(np.diff(v) >= 0) and v[-1] > 0:
                table, src = v, parts[1].split("=", 1)[1]
        except ValueError:
            pass
    with _lock:
        _cache[station.id] = (table if table is not None else _PURE, src)
    return src


def fetch_all(stations, timeout=5.0):
    """Fetch every camera's table in parallel; {sid: source}."""
    out = {}
    ths = [threading.Thread(target=lambda s=s: out.__setitem__(s.id, fetch(s, timeout))) for s in stations]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout + 2)
    return out


def table(sid):
    with _lock:
        return _cache.get(sid, (_PURE, "assumed"))[0]


def source(sid):
    with _lock:
        return _cache.get(sid, (_PURE, "assumed"))[1]


def to_linear(img, sid):
    """uint8 codes -> linear light 0..1 through the camera's real curve."""
    return (table(sid)[img] / 4095.0).astype(np.float32)


_inv_cache = {}


def inverse(sid):
    """4096-entry linear-12-bit -> 8-bit code table, the inverse of this camera's
    curve. Built once: np.interp over a whole frame was the single slowest step
    in the constant-exposure view at ~1.5 s per camera, because it searches the
    knots for every one of 6.2M samples. Inverting onto a 4096-entry grid costs
    that search 4096 times instead, and the frame then becomes an array index."""
    with _lock:
        inv = _inv_cache.get(sid)
    if inv is None:
        t = table(sid)
        inv = (np.interp(np.arange(4096.0), t, np.arange(256)) + 0.5).astype(np.uint8)
        with _lock:
            _inv_cache[sid] = inv
    return inv


def to_code(lin, sid):
    """linear light 0..1 -> 8-bit codes through the inverse of the camera's curve."""
    idx = np.clip(lin, 0.0, 1.0) * 4095.0
    return inverse(sid)[idx.astype(np.int32)]
