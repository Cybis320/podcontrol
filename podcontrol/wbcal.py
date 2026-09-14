"""Cloud-gray white-balance calibration.

Point at a region that SHOULD be neutral (a grey cloud) and solve for the WB
gains that make it neutral in the output -- so clouds render grey while the sky
keeps its blue. One WB is pushed to the whole pod (calibrated on one reference
camera's cloud), matching the "act as one" goal.

The output is gamma-encoded (sensor gamma ~0.5), so a WB gain factor k on the
linear raw shows up as ~k**gamma in the displayed channel. We iterate a damped
correction (robust to the exact gamma and to CCM cross-talk) until the region's
R:G:B is neutral. G is the reference; only R and B gains move.
"""
UNITY = 256          # x256 gains: 256 = 1.00x (both platforms)
GAIN_MIN, GAIN_MAX = 128, 1024


def _clamp(v, lo=GAIN_MIN, hi=GAIN_MAX):
    return max(lo, min(hi, v))


def region_mean_rgb(bgr, box):
    """Mean (R,G,B) over box=(x0,y0,x1,y1) of a BGR frame (cv2 order)."""
    x0, y0, x1, y1 = box
    x0, x1 = sorted((max(0, int(x0)), max(0, int(x1))))
    y0, y1 = sorted((max(0, int(y0)), max(0, int(y1))))
    roi = bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    b, g, r = roi[:, :, 0].mean(), roi[:, :, 1].mean(), roi[:, :, 2].mean()
    return float(r), float(g), float(b)


def correction_step(rgb, gains, gamma_exp=0.55, damp=0.8):
    """One GENTLE WB update toward neutral, G held as the reference channel; only
    R and B move. Small damp -> monotonic, no limit cycle. Meant for a near-neutral
    reference (a grey cloud): the correction is small and converges. gains x256."""
    r, g, b = rgb
    R, G, B = gains
    if min(r, g, b) <= 0:
        return gains
    R2 = _clamp(R * (g / r) ** (gamma_exp * damp))
    B2 = _clamp(B * (g / b) ** (gamma_exp * damp))
    return R2, G, B2


def error(rgb):
    """Neutrality error: max fractional deviation of R and B from G."""
    r, g, b = rgb
    if min(r, g, b) <= 0:
        return 1.0
    return max(abs(g / r - 1.0), abs(g / b - 1.0))


def calibrate(measure, apply_wb, gains0=(UNITY, UNITY, UNITY),
              tol=0.02, max_iter=8, gamma_exp=0.55, damp=0.8, on_step=None):
    """Iterate to neutralize the reference region.

    measure()  -> (R,G,B) region mean from a FRESH frame (after WB took effect)
    apply_wb(R,G,B) -> push WB to the pod (x256 gains)
    Returns (gains, final_error, iters).
    """
    gains = tuple(gains0)
    apply_wb(*gains)
    last_err = 1.0
    for i in range(max_iter):
        rgb = measure()
        if rgb is None:
            break
        last_err = error(rgb)
        if on_step:
            on_step(i, rgb, gains, last_err)
        if last_err <= tol:
            break
        gains = correction_step(rgb, gains, gamma_exp, damp)
        apply_wb(*gains)
    return gains, last_err, i + 1


def run_pod_calibration(pod, ref_id, box, frame_fn=None, settle_s=2.5, start_unity=True,
                        on_step=None, fresh_fn=None, **kw):
    """Calibrate on ref camera's `box`, push the WB to the whole pod.

    fresh_fn(station, after_epoch) -> BGR frame captured AFTER the last WB push
        (preferred; see frames.fresh_frame -- on a capturing camera this waits
        for RMS's next saved block, ~50 s, instead of measuring a pre-change frame)
    frame_fn(station) -> BGR frame, sampled settle_s after the push (legacy)."""
    import time
    ref = next(s for s in pod.stations if s.id == ref_id)
    state = {"t_apply": 0.0}

    def measure():
        if fresh_fn is not None:
            img = fresh_fn(ref, state["t_apply"])
        else:
            time.sleep(settle_s)             # let the WB + a fresh frame land
            img = frame_fn(ref)
        return region_mean_rgb(img, box) if img is not None else None

    def apply_wb(R, G, B):
        pod.wb_all(R, G, B)
        state["t_apply"] = time.time()

    g0 = (UNITY, UNITY, UNITY)
    if not start_unity:
        w = pod.wb_read(ref_id)
        if w and w.get("gains"):
            g0 = (int(w["gains"][0] * 256), int(w["gains"][1] * 256), int(w["gains"][-1] * 256))

    # No exposure freeze: R:G:B ratios are exposure-independent, so we let the AE
    # run and only avoid clipping (max-normalized gains + starting from the
    # near-neutral AWB keep the region mid-range). measure() waits `settle_s` for
    # the WB *and* any AE re-exposure to land before sampling.
    return calibrate(measure, apply_wb, gains0=g0, on_step=on_step, **kw)


if __name__ == "__main__":
    import argparse
    from podcontrol.stations import get_pod
    from podcontrol.podctl import PodController
    from podcontrol.frames import fresh_frame
    ap = argparse.ArgumentParser(description="Cloud-gray WB calibration (pushes to whole pod).")
    ap.add_argument("--camera", required=True, help="reference station id (e.g. cam101)")
    ap.add_argument("--box", required=True, help="x0,y0,x1,y1 region (full-frame px) that should be grey")
    ap.add_argument("--tol", type=float, default=0.02)
    args = ap.parse_args()
    pod = PodController(get_pod())
    box = tuple(int(v) for v in args.box.split(","))

    def step(i, rgb, g, err):
        print("  iter %d: region R=%.0f G=%.0f B=%.0f  err=%.3f  gains=%d/%d/%d"
              % (i, rgb[0], rgb[1], rgb[2], err, g[0], g[1], g[2]))

    print("calibrating on %s box=%s ..." % (args.camera, box))
    gains, err, n = run_pod_calibration(
        pod, args.camera, box, tol=args.tol, on_step=step,
        fresh_fn=lambda s, after: fresh_frame(s, after, allow_grab=False)[0])
    print("DONE: gains R=%d G=%d B=%d (%.2f/%.2f/%.2fx)  err=%.3f  in %d iters -> pushed to pod"
          % (gains[0], gains[1], gains[2], gains[0] / 256, gains[1] / 256, gains[2] / 256, err, n))
