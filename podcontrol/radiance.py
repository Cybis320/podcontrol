"""Constant-exposure sky view (DEMO): every camera's frame brought to one common exposure.

With the cameras exposing freely (podcontrol's AE off -> each camera's own AE), the raw
mosaic is patchy: every tile has its own brightness. The camera pipeline is linear up to a
pure gamma 0.5 (full range), so a pixel converts exactly to light on a fixed scale:

    linear = (v / 255)^2,  light = linear / E,  E = exposure x analog x sensor-digital x ISP-digital gain

and back to a display value at a reference exposure E_ref: v' = 255 * sqrt(min(1, light * E_ref)).
Each tile is therefore scaled in linear light by k = E_ref / E_camera. E_camera is looked up
from the metadata RMS embeds in each saved frame (exact, includes the WB-rung scale), else in
podcontrol's history (one poll per ~5 s) at the frame's capture time; E_ref is the median of
the cameras' exposures unless given.

Limits (a demo, not the product): the 5 s poll can miss an exposure change between poll and
frame; per-camera sensitivity (~5% in red between units) and vignetting are not calibrated
and will show as seams; clipped pixels stay clipped (only a lower bound on the light). For
display, highlights above ~60% of white roll off smoothly (rolloff), the same for every tile.
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


KNEE = 0.36    # linear level (= 60% of display white after gamma 0.5) above which highlights roll off


def rolloff(lin, knee=KNEE):
    """Soft highlight compression in linear light, identical for every tile: unchanged below the
    knee, then an exponential shoulder that approaches 1 without ever clipping. A common exposure
    spans more than the display can show (the sun camera, brought to the pod's median exposure,
    lands up to ~4x above white); a hard clip blew those tiles out (US05B1: 9% clipped as
    captured, 50% in the merged view), this keeps their detail and the mosaic seamless."""
    over = np.maximum(lin - knee, 0.0)
    return np.where(lin <= knee, lin, knee + (1.0 - knee) * (1.0 - np.exp(-over / (1.0 - knee))))


def scale(img, k, knee=KNEE):
    """img (uint8, gamma 0.5) scaled by k in linear light, highlights rolled off, back to uint8."""
    lin = (img.astype(np.float32) / 255.0) ** 2 * k
    return (255.0 * np.sqrt(np.clip(rolloff(lin, knee), 0.0, 1.0)) + 0.5).astype(np.uint8)


def frame_exposure(path):
    """The frame's own total exposure from the metadata RMS embeds (save_frame_metadata):
    exposure x analog x sensor-digital x ISP-digital gain x the WB scale (the green gain, 1.0 at
    the day base; the rung scales every gain by it). Exact per frame; None without metadata."""
    try:
        from RMS.FrameMetadata import readImageMeta
    except Exception:
        return None
    try:
        m = readImageMeta(path) or {}
        e = float(m["exp_us"]) * float(m["again"]) * float(m.get("dgain") or 1.0) * float(m.get("ispdgain") or 1.0)
        return e * float(m.get("wb_g") or 1.0)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def constant_exposure(paths, records, e_ref=None):
    """({sid: scaled image}, {sid: cache path tagged with k}, info) for the sky compositor.
    Cameras whose exposure cannot be found are left out of the scaling (drawn as they are)."""
    es, src = {}, {}
    for sid, p in paths.items():
        e = frame_exposure(p)                  # exact, from the frame itself
        src[sid] = "frame"
        if not e:
            t = frames.frame_capture_time(p)   # fallback: the history poll nearest the capture
            e = exposure_at(records, sid, t) if t is not None else None
            src[sid] = "poll"
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
        imgs[sid] = scale(img, k)                  # every tile through the same curve
        tagged[sid] = "%s#k=%.4f" % (p, k)
        ks[sid] = k
    return imgs, tagged, {"e_ref": ref, "k": ks, "exposure": es, "source": src}
