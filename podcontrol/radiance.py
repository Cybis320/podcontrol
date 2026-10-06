"""Constant-exposure sky view (DEMO): every camera's frame brought to one common exposure.

With the cameras exposing freely (podcontrol's AE off -> each camera's own AE), the raw
mosaic is patchy: every tile has its own brightness. The camera pipeline is linear up to its
gamma table (podcontrol.decode: each camera's live table, pure 0.5 only as a fallback), so a
pixel converts to light on a fixed scale:

    linear = decode(v),  light = linear / E,  E = exposure x analog x sensor-digital x ISP-digital gain x WB-rung

and back to a display value at a reference exposure E_ref. Each tile is therefore scaled in
linear light by k = E_ref / E_camera. E_camera comes from the metadata RMS embeds in each saved
frame (exact; verified 2026-10-06: gain steps hold G/E within 0.5%, exposure steps within ~2%
with no integration-time offset), else from podcontrol's history (one poll per ~5 s) at the
frame's capture time; E_ref is the median of the cameras' exposures unless given. The lens
flat (podcontrol.vignette, LINEAR coefficient) is applied here too, before the rolloff.

COLOUR. The cameras run the ISP saturation (`satu`, 128 = 1.00x; the pod uses 255 = 1.99x)
inside the colour-matrix stage, i.e. in linear light: out = Y + s (in - Y). It commutes with
the exposure scale, but it multiplies every small difference between units by ~s and more:
on blue sky red comes out as ~2R - Y, so a few percent of red sensitivity becomes tens of
percent of R/G. Measured 2026-10-06 on the overlaps: R/G between cameras 0.69-1.12 as
captured, 0.87-1.05 with the saturation undone. The correction (chroma_matrix) undoes s,
applies per-camera R and B gains fitted on the overlaps (fit_chroma: chroma ratios only, so
brightness and glare cannot leak into them), and re-applies s for display -- one 3x3 matrix
per camera. Luma is Rec.709; the overlaps cannot tell it from Rec.601 (residual 0.083 both).

Limits (a demo, not the product): clipped pixels stay clipped (only a lower bound on the
light). The green of a raw-saturated pixel on a WB-rung camera is RMS's to repair (its
day_highlight_rebuild, in the saved frame); fit_chroma leaves those pixels out either way,
since a rebuilt green is itself derived from the scene's colour ratios. For display,
highlights above ~60% of white roll off smoothly (rolloff), the same for every tile.
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
    cam = (best[1].get("cams") or {}).get(sid) or {}
    e = cam_exposure(cam)
    if e is None:
        return None
    # the camera's OWN rung scale when the record has one (individual AE: the pod-level
    # field is the median of the cameras' scales, wrong for any camera on another rung)
    return e * float(cam.get("wb_scale") or best[1].get("wb_scale") or 1.0)


KNEE = 0.36    # linear level (= 60% of display white after gamma 0.5) above which highlights roll off


def rolloff(lin, knee=KNEE):
    """Soft highlight compression in linear light, identical for every tile: unchanged below the
    knee, then an exponential shoulder that approaches 1 without ever clipping. A common exposure
    spans more than the display can show (the sun camera, brought to the pod's median exposure,
    lands up to ~4x above white); a hard clip blew those tiles out (US05B1: 9% clipped as
    captured, 50% in the merged view), this keeps their detail and the mosaic seamless."""
    over = np.maximum(lin - knee, 0.0)
    return np.where(lin <= knee, lin, knee + (1.0 - knee) * (1.0 - np.exp(-over / (1.0 - knee))))


_FLAT = {}          # (h, w, coeff) -> per-pixel 1/V(r); static, so built once


def vignette_linear_default():
    from podcontrol import vignette
    return vignette.LINEAR_COEFF


def flat_field(shape, coeff=None):
    """Per-pixel lens correction 1/V(r) for a full frame, in LINEAR light.

    Uses vignette.LINEAR_COEFF, not DEFAULT_COEFF: this path works in light, and
    the two coefficients differ by a factor of two in log terms because the
    display one was fitted on gamma-encoded codes. Radius is measured from the
    frame centre, which is how the coefficient was fitted."""
    from podcontrol import vignette
    c = vignette.LINEAR_COEFF if coeff is None else coeff
    if not c:
        return None
    h, w = shape[:2]
    key = (int(h), int(w), round(float(c), 8))
    g = _FLAT.get(key)
    if g is None:
        yy, xx = np.mgrid[0:h, 0:w]
        g = vignette.gain(np.hypot(xx - w / 2.0, yy - h / 2.0), c)
        if len(_FLAT) > 4:
            _FLAT.clear()
        _FLAT[key] = g
    return g


# --- colour: the ISP saturation and per-camera chroma gains ---------------------------------
LUMA_BGR = np.array([0.0722, 0.7152, 0.2126])   # Rec.709, in the frames' cv2 BGR order
MIN_SAT = 0.25      # below this (night: satu 0) the chroma is gone and cannot be undone
_SAT = {}           # sid -> saturation factor, from the cameras' polled `satu`


def note_saturation(poll):
    """Remember each camera's ISP saturation from a poll ({sid: telemetry}). The full poll
    that carries `satu` runs about once a minute, so the last value is kept between."""
    for sid, d in (poll or {}).items():
        sa = (d or {}).get("satu") if isinstance(d, dict) else None
        if sa and sa.get("value") is not None:
            _SAT[sid] = float(sa["value"]) / 128.0


def saturation(sid):
    """The camera's saturation factor (1.0 = none), or None when it has not been polled."""
    return _SAT.get(sid)


def _sat_matrix(f):
    """3x3 (BGR) chroma gain f about the Rec.709 luma axis: v -> Y + f (v - Y)."""
    return f * np.eye(3) + (1.0 - f) * np.outer(np.ones(3), LUMA_BGR)


def chroma_matrix(s, gains):
    """The per-camera colour correction in linear light, as one 3x3 BGR matrix: undo the
    camera's saturation s, apply its (r, b) gains relative to green, re-apply s. None when
    there is nothing to do or the saturation is too low to undo."""
    if not gains or s is None or s < MIN_SAT:
        return None
    r, b = float(gains[0]), float(gains[1])
    if abs(r - 1.0) < 1e-4 and abs(b - 1.0) < 1e-4:
        return None
    return (_sat_matrix(s) @ np.diag([b, 1.0, r]) @ _sat_matrix(1.0 / s)).astype(np.float32)


def scale(img, k, knee=KNEE, sid=None, flat=None, cmat=None):
    """img (uint8 camera codes) scaled by k in linear light, highlights rolled off, back to
    uint8. With sid, through that camera's real curve (podcontrol.decode: linear below the
    gamma table's first node, not code^2); without, the pure 0.5 curve.

    `flat` is an optional per-pixel gain applied in linear light alongside k, for
    the lens falloff. It belongs HERE rather than at composite time because the
    rolloff below is non-linear: correcting the corners after their highlights
    have been compressed would boost an already-compressed value, where doing it
    first lets the rolloff see the light that actually arrived.

    `cmat` is the camera's chroma_matrix (3x3, BGR), also before the rolloff, which is
    per channel. Negative results are clipped to 0, as the ISP clips its own."""
    def _apply(lin):
        # in place: each of these is a ~25 MB float32 array per camera, and six
        # cameras a cycle made the allocations, not the arithmetic, the cost
        lin *= k
        if flat is not None:
            lin *= (flat[:, :, None] if lin.ndim == 3 else flat)
        if cmat is not None and lin.ndim == 3:
            import cv2
            lin = cv2.transform(lin, cmat)
            np.maximum(lin, 0.0, out=lin)
        return rolloff(lin, knee)
    if sid is not None:
        from podcontrol import decode
        return decode.to_code(_apply(decode.to_linear(img, sid)), sid)
    return (255.0 * np.sqrt(np.clip(_apply((img.astype(np.float32) / 255.0) ** 2), 0.0, 1.0))
            + 0.5).astype(np.uint8)


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


def constant_exposure(paths, records, e_ref=None, flat=False, chroma=None):
    """({sid: scaled image}, {sid: cache path tagged with k}, info) for the sky compositor.
    Cameras whose exposure cannot be found are left out of the scaling (drawn as they are).

    `chroma` is {sid: (r_gain, b_gain)} from fit_chroma, or None for no colour correction.
    A camera whose saturation is unknown or too low (night) is drawn uncorrected and listed
    in info["chroma_skipped"].

    `flat` divides out the lens falloff, in linear light, before the highlight
    rolloff: pass a LINEAR coefficient (auto-tune fits one), or True for the
    module default. The caller must then tell the compositor NOT to apply its
    own display-space correction, or the frames are flattened twice."""
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
    imgs, tagged, ks, cskip = {}, {}, {}, []
    for sid, p in paths.items():
        img = frames.imread_cached(p)
        if img is None:
            continue
        k = ref / es[sid] if sid in es else 1.0
        # flat may be True (use the module default) or a coefficient from auto-tune
        flat_coeff = (vignette_linear_default() if flat is True else float(flat)) if flat else 0.0
        ff = flat_field(img.shape, flat_coeff) if flat_coeff else None
        cm = None
        if chroma and chroma.get(sid):
            cm = chroma_matrix(saturation(sid), chroma[sid])
            if cm is None and (saturation(sid) is None or saturation(sid) < MIN_SAT):
                cskip.append(sid)
        imgs[sid] = scale(img, k, sid=sid, flat=ff, cmat=cm)   # through that camera's own curve
        # the tag keys the compositor's frame cache AND the downscale cache, so it
        # must carry everything that changed the pixels. The COEFFICIENT has to be
        # in it, not just the fact of a flat: tagging only "#flat" made a changed
        # coefficient reuse the previous composite, so auto-tune appeared to do
        # nothing.
        tagged[sid] = "%s#k=%.4f%s%s" % (p, k, ("#flat%.6f" % flat_coeff) if ff is not None else "",
                                         ("#c" + ",".join("%.5f" % x for x in cm.ravel())) if cm is not None else "")
        ks[sid] = k
    return imgs, tagged, {"e_ref": ref, "k": ks, "exposure": es, "source": src,
                          "flat": bool(flat), "flat_coeff": flat_coeff if flat else 0.0,
                          "chroma": bool(chroma), "chroma_skipped": cskip}


def fit_chroma(renderer, sets, progress=None):
    """Per-camera (r, b) chroma gains relative to green, from overlapping fields.

    `sets` is [{station_id: path}], as for vignette.fit. Each frame is decoded through its
    camera's curve and its ISP saturation undone (so the gains are fitted on what the
    sensor + WB delivered); then, where two cameras see one sky direction:

        log(R/G)_a - log(R/G)_b = rho_a - rho_b        (likewise B/G with beta)

    Only ratios between channels enter, so exposure, the lens flat, and any brightness
    difference (veiling glare in a sun camera) cancel and cannot pull the gains. Each
    pair in each set contributes one equation, its median over the shared pixels; the
    gauge pins the mean log gain to 0, so the gains bring every camera to the pod's mean
    colour. Pixels excluded: the RMS mask, the sun/moon/flare zones, clipped or near-black
    in any channel, and a WB-rung camera's green plateau.

    Returns {"gains": {sid: (r, b)}, "before", "after" (RMS log mismatch over the pairs),
    "pairs", "sets", "sat": {sid: s}}, or {"error": ...}."""
    import cv2
    from podcontrol import decode as _decode
    try:
        from RMS.FrameMetadata import readImageMeta
    except Exception:
        readImageMeta = None
    grid = renderer.grid
    ids = [sid for sid in renderer.by_id if renderer.luts.get(sid) is not None]
    sat = {sid: saturation(sid) for sid in ids}
    ids = [sid for sid in ids if sat[sid] is not None and sat[sid] >= MIN_SAT]
    if len(ids) < 2:
        return {"error": "need at least two cameras with a known saturation >= %.2f (none is"
                         " polled yet, or it is night: satu 0 leaves no colour to fit)" % MIN_SAT}
    n = len(ids)
    pos = {sid: i for i, sid in enumerate(ids)}
    eqs = []                                    # (a, b, d_r, d_b)
    for si, paths in enumerate(sets):
        if progress:
            progress(si, len(sets))
        warp = {}
        for sid in ids:
            p = paths.get(sid)
            img = cv2.imread(p) if p else None
            if img is None:
                continue
            st, lut = renderer.by_id[sid], renderer.luts[sid]
            lin = cv2.transform(_decode.to_linear(img, sid), _sat_matrix(1.0 / sat[sid]).astype(np.float32))
            bad = (img.max(axis=2) >= 245) | (img.min(axis=2) <= 8)
            m = readImageMeta(p, 65536) if readImageMeta else None
            try:
                gceil = 255.0 * min(1.0, float(m["ispdgain"]) * (3855.0 / 4095.0) * float(m["wb_g"])) ** 0.5
                bad |= img[..., 1] >= gceil - 4
            except (KeyError, TypeError, ValueError):
                pass
            keep = frames.mask_for(st, img, frames.frame_capture_time(p))
            if keep is not None:
                bad |= ~keep
            small = cv2.resize(lin, lut.small, interpolation=cv2.INTER_AREA)
            sbad = cv2.resize(bad.astype(np.float32), lut.small, interpolation=cv2.INTER_AREA)
            w = cv2.remap(small, lut.map_x, lut.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            wb = cv2.remap(sbad, lut.map_x, lut.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                           borderValue=1.0)
            y0, y1, x0, x1 = lut.bbox
            ok = np.zeros((grid.h, grid.w), bool)
            ok[y0:y1, x0:x1] = (lut.weight > 0) & (wb < 0.01) & (w.min(axis=2) > 1e-5)
            F = np.ones((grid.h, grid.w, 3), np.float32)
            F[y0:y1, x0:x1] = np.maximum(w, 1e-5)
            warp[sid] = (np.log(F[..., 2] / F[..., 1]), np.log(F[..., 0] / F[..., 1]), ok)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if a not in warp or b not in warp:
                    continue
                m = warp[a][2] & warp[b][2]
                if m.sum() < 300:
                    continue
                eqs.append((pos[a], pos[b], float(np.median(warp[a][0][m] - warp[b][0][m])),
                            float(np.median(warp[a][1][m] - warp[b][1][m]))))
    if not eqs:
        return {"error": "no usable overlap samples (too dark, clipped, or no shared sky)"}
    A = np.zeros((len(eqs) + 1, n))
    for row, (a, b, _, _) in enumerate(eqs):
        A[row, a], A[row, b] = 1.0, -1.0
    A[-1, :] = 1.0                              # gauge: mean log gain 0
    sol = {}
    for col in (2, 3):
        y = np.array([e[col] for e in eqs] + [0.0])
        sol[col], *_ = np.linalg.lstsq(A, y, rcond=None)
    d = np.array([[e[2], e[3]] for e in eqs])
    res = d - np.column_stack([A[:-1] @ sol[2], A[:-1] @ sol[3]])
    seen = set(e[0] for e in eqs) | set(e[1] for e in eqs)
    # gain = exp(-rho): the camera's own excess colour is divided out
    gains = {sid: (float(np.exp(-sol[2][pos[sid]])), float(np.exp(-sol[3][pos[sid]])))
             for sid in ids if pos[sid] in seen}
    return {"gains": gains, "before": float(np.sqrt(np.mean(d ** 2))),
            "after": float(np.sqrt(np.mean(res ** 2))), "pairs": len(eqs), "sets": len(sets),
            "sat": {sid: sat[sid] for sid in gains}}
