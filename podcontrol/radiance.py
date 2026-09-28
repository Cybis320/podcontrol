"""Constant-exposure sky view (DEMO): every camera's frame brought to one common exposure.

With the cameras exposing freely (podcontrol's AE off -> each camera's own AE), the raw
mosaic is patchy: every tile has its own brightness. The camera pipeline is linear up to a
pure gamma 0.5 (full range), so a pixel converts exactly to light on a fixed scale:

    linear = (v / 255)^2,  light = linear / E,  E = exposure x analog x sensor-digital x ISP-digital gain

and back to a display value at a reference exposure E_ref: v' = 255 * sqrt(min(1, light * E_ref)).
Each tile is therefore scaled in linear light by k = E_ref / E_camera. E_camera is looked up
in podcontrol's history (one poll per ~5 s) at the frame's capture time; E_ref is the median
of the cameras' exposures unless given.

Limits (a demo, not the product): the 5 s poll can miss an exposure change between poll and
frame; per-camera sensitivity (~5% in red between units) and vignetting are not calibrated
and will show as seams; clipped pixels stay clipped (only a lower bound on the light).
"""
import numpy as np

from podcontrol import frames


def cam_exposure(rec_cam):
    """Total exposure (us x gains) of one camera from a history record, or None."""
    try:
        return (float(rec_cam["exp_us"]) * float(rec_cam["again_x"]) * float(rec_cam.get("dgain_x") or 1.0)
                * float(rec_cam.get("ispdgain_x") or 1.0))
    except (KeyError, TypeError, ValueError):
        return None


def exposure_at(records, sid, t, max_gap=15.0):
    """The camera's total exposure (x the pod WB scale) in the history record nearest t."""
    best = None
    for r in reversed(records):
        dt = abs(r.get("t", 0) - t)
        if best is None or dt < best[0]:
            best = (dt, r)
        if r.get("t", 0) < t - max_gap:
            break
    if best is None or best[0] > max_gap:
        return None
    e = cam_exposure((best[1].get("cams") or {}).get(sid) or {})
    if e is None:
        return None
    return e * float(best[1].get("wb_scale") or 1.0)


def scale(img, k):
    """img (uint8, gamma 0.5) scaled by k in linear light, back to uint8 gamma 0.5."""
    lin = (img.astype(np.float32) / 255.0) ** 2
    return (255.0 * np.sqrt(np.clip(lin * k, 0.0, 1.0)) + 0.5).astype(np.uint8)


def constant_exposure(paths, records, e_ref=None):
    """({sid: scaled image}, {sid: cache path tagged with k}, info) for the sky compositor.
    Cameras whose exposure cannot be found are left out of the scaling (drawn as they are)."""
    es = {}
    for sid, p in paths.items():
        t = frames.frame_capture_time(p)
        e = exposure_at(records, sid, t) if t is not None else None
        if e:
            es[sid] = e
    if not es:
        return {}, dict(paths), {"e_ref": None, "k": {}}
    ref = float(e_ref) if e_ref else float(np.median(list(es.values())))
    imgs, tagged, ks = {}, {}, {}
    for sid, p in paths.items():
        img = frames.imread_cached(p)
        if img is None:
            continue
        k = ref / es[sid] if sid in es else 1.0
        imgs[sid] = scale(img, k) if abs(k - 1.0) > 1e-3 else img
        tagged[sid] = "%s#k=%.4f" % (p, k)
        ks[sid] = k
    return imgs, tagged, {"e_ref": ref, "k": ks, "exposure": es}
