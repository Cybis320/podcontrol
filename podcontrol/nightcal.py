"""Night analog-gain calibration: the lowest analog gain that loses no sensitivity.

At night RMS pins exposure (40 ms), analog gain and ISP digital gain. Above some
analog gain the sky's own shot noise, not the sensor's read noise, sets the
noise floor; any gain beyond that point adds no sensitivity and only spends
highlight headroom (bright meteors clip sooner). This finds that point.

For each camera it sweeps the analog gain down from the top at fixed exposure
and ISP gain, and at every step asks the camera for `noise_stats` -- venc
measures the sky background and its frame-to-frame noise itself, on the
uncompressed picture, so no second RTSP stream is opened while RMS captures.
The figure of merit is the noise-equivalent flux (noise divided by total gain):
how faint a signal each gain can still separate from the sky. The reported gain
is the right divisor: on .102 (CV300, 2026-09-26) the response measured as the
40 ms - 20 ms background difference tracked the reported gain within 2% from
5.6x to 22x. The background itself does NOT scale with gain at the low end,
because the black subtraction takes ~6 linear codes too many (a 0.1 ms frame
reads 0 with no noise); that is an offset, not lost sensitivity, so the
background/noise ratio (snr) is not a fair score across gains.

Two things do corrupt a step's noise figure, and such steps are not eligible:
  - quantization: sky noise under ~1 code of the 8-bit output is under-read
    (lin = code^2, so one code is 2*code*4095/65025 linear units);
  - the black clip: a sky within 3 sigma of zero has its lower tail clipped.
The noise-equivalent flux does NOT fall monotonically toward the top gain (it
rose ~15% from 11x to 22x on every 10x camera), so steps are compared with the
BEST eligible step, not the top one. The chosen gain is the lowest eligible
step within `tol` of that best. One value goes to the whole pod: the HIGHEST of the per-camera choices,
i.e. the darkest sky's need, so no camera loses sensitivity.

ISP digital gain is not swept. It comes after the ADC, so it adds no signal; it
only has to restore full scale after the black-level subtraction:
4095 / (4095 - 240) = 1.0625x (1088). Below that a saturated pixel never
reaches code 255 (measured on the Moon, 2026-09-25).

Guards: the sun must be well below the horizon; each camera's sweep ends by
re-measuring its first step, and a sky that moved (clouds, the Moon rising)
invalidates that camera's run; the Moon's altitude and phase are recorded,
because a moonlit sky favours a lower gain than a dark one. Sweep steps are
LIVE only (never saved); every camera is handed back its saved state
(`ae_restore`) when the sweep ends. Nothing persists until apply().

    python -m podcontrol.nightcal                 # measure + propose (dry run)
    python -m podcontrol.nightcal --apply         # ... then set cameras + JSON
"""
import json, math, os, re, shutil, time
from concurrent.futures import ThreadPoolExecutor

from podcontrol.podctl import send, send_live

BLACK_LEVEL = 240                                          # 12-bit pedestal, both sensors
ISPD_FULL_SCALE = int(round(4095 * 1024 / (4095 - BLACK_LEVEL)))   # 1088 = 1.0625x
STEPS = [22924, 16384, 11585, 8192, 5793, 4096]            # x1024, 3 dB apart; 22924 = IMX307 max
SUN_MAX_ALT = -12.0                                        # deg: nautical night or darker
LOG = os.path.expanduser("~/.config/podcontrol/nightcal.jsonl")


def parse_noise(text):
    """`noise_stats ...` key=value line -> dict of numbers; None on error/absence."""
    if not text or not text.startswith("noise_stats") or "ERROR" in text:
        return None
    d = {}
    for k, v in re.findall(r"(\w+)=([-\d.,]+)", text):
        if k == "region":
            continue
        try:
            d[k] = float(v)
        except ValueError:
            pass
    return d if "mean_lin" in d and "std_lin" in d else None


def night_line(station):
    """(again, ispdgain, exp_us) from RMS's night `manual` line, or None."""
    cmd = station.mode_cmd("night") or ""
    if not cmd.startswith("manual"):
        return None
    get = lambda f: (int(re.search(r"-%s\s+(\d+)" % f, cmd).group(1))
                     if re.search(r"-%s\s+(\d+)" % f, cmd) else None)
    return get("a"), get("i"), get("e")


def _measure(ip, again, ispd, exp_us, frames, settle_s):
    send_live(ip, "manual -a %d -i %d -e %d" % (again, ispd, exp_us))
    time.sleep(settle_s)                       # the sensor applies gain within ~2 frames
    r = parse_noise(send(ip, "noise_stats %d" % frames, timeout=frames / 5.0 + 20))
    if r is None:
        return None
    g = (r.get("again") or again) / 1024.0 * (r.get("ispdgain") or ispd) / 1024.0
    r["again_set"] = again
    r["total_gain"] = g
    r["nef"] = r["std_lin"] / g                # noise-equivalent flux: lower = more sensitive
    r["sky"] = r["mean_lin"] / g               # sky brightness in gain-free units
    r["std_code"] = r["std_lin"] / code_step(r["mean_lin"])
    r["eligible"], r["why"] = eligible(r)
    return r


def code_step(mean_lin):
    """Linear units per 8-bit output code at this level (lin = code^2 * 4095/65025)."""
    code = math.sqrt(max(mean_lin, 0.0) * 65025.0 / 4095.0)
    return max(2.0 * code, 1.0) * 4095.0 / 65025.0


def eligible(r, min_std_code=1.0, min_sigma=3.0):
    """Is this step's noise figure trustworthy? (ok, reason)"""
    if r["std_code"] < min_std_code:
        return False, "quantized (noise %.2f code)" % r["std_code"]
    if r["mean_lin"] < min_sigma * r["std_lin"]:
        return False, "black clip (sky %.1f sigma)" % (r["mean_lin"] / max(r["std_lin"], 1e-9))
    return True, ""


def sweep_camera(station, exp_us, ispd=ISPD_FULL_SCALE, steps=STEPS, frames=25,
                 settle_s=1.0, on_row=None):
    """Sweep one camera. Returns {rows, drift, chosen?, ok, reason}; never saves."""
    ip = station.ip
    out = {"rows": [], "drift": None, "ok": False, "reason": ""}
    try:
        for a in steps:
            r = _measure(ip, a, ispd, exp_us, frames, settle_s)
            if r is None:
                probe = send(ip, "noise_stats 2", timeout=10) or "no answer"
                out["reason"] = ("camera has no noise_stats (needs the 2026-09-25 image)"
                                 if "unknown command" in probe else "noise_stats failed: " + probe[:80])
                return out
            out["rows"].append(r)
            if on_row:
                on_row(station, r)
        # stability: the sky must not have moved during the sweep
        rep = _measure(ip, steps[0], ispd, exp_us, frames, settle_s)
        if rep is not None:
            s0 = out["rows"][0]["sky"]
            out["drift"] = abs(rep["sky"] - s0) / s0 if s0 else None
        out["ok"] = True
    finally:
        send(ip, "ae_restore", timeout=10)     # back to the saved (RMS) state
    return out


def best(rows):
    """The eligible step with the lowest noise-equivalent flux, or None."""
    ok = [r for r in rows if r.get("eligible", True)]
    return min(ok, key=lambda r: r["nef"]) if ok else None


def choose(rows, tol):
    """Lowest eligible analog gain whose noise-equivalent flux is within tol of the best's."""
    b = best(rows)
    if b is None:
        return None
    ok = [r for r in rows if r.get("eligible", True) and r["nef"] <= b["nef"] * (1.0 + tol)]
    return min(ok, key=lambda r: r["again_set"])["again_set"]


def calibrate_pod(pod, tol=0.02, frames=25, drift_max=0.05, force=False, on_row=None):
    """Measure every camera in parallel and propose one pod-wide night gain."""
    st0 = pod.stations[0]
    res = {"t": time.time(), "tol": tol, "frames": frames, "ispdgain": ISPD_FULL_SCALE,
           "cameras": {}, "pod_again": None, "sun_alt": None, "moon": None, "exp_us": None}
    try:
        from podcontrol.sunmask import body_altaz
        sun = body_altaz(st0, "sun"); moon = body_altaz(st0, "moon")
        res["sun_alt"] = sun and round(sun["alt"], 1)
        res["moon"] = moon and {"alt": round(moon["alt"], 1), "phase": round(moon["phase"], 1)}
    except Exception:
        pass
    if res["sun_alt"] is not None and res["sun_alt"] > SUN_MAX_ALT and not force:
        raise RuntimeError("sun at %.1f deg -- night gain calibration needs <= %.0f deg"
                           % (res["sun_alt"], SUN_MAX_ALT))
    nl = night_line(st0)
    if not nl or not nl[2]:
        raise RuntimeError("no RMS night `manual` line in %s" % (st0.settings_path or "(no settings file)"))
    res["exp_us"] = nl[2]
    with ThreadPoolExecutor(max_workers=len(pod.stations)) as ex:
        futs = {s.id: ex.submit(sweep_camera, s, nl[2], ISPD_FULL_SCALE, STEPS, frames, 1.0, on_row)
                for s in pod.stations}
        for sid, f in futs.items():
            try:
                c = f.result()
            except Exception as e:
                c = {"rows": [], "ok": False, "reason": str(e)}
            if c["ok"]:
                if c["drift"] is None or c["drift"] > drift_max:
                    c["ok"] = False
                    c["reason"] = ("sky changed %.0f%% during the sweep -- rerun" % (100 * c["drift"])
                                   if c["drift"] is not None else "stability re-measure failed")
                else:
                    c["chosen"] = choose(c["rows"], tol)
                    if c["chosen"] is None:
                        c["ok"] = False
                        c["reason"] = "no step with a trustworthy noise figure"
            res["cameras"][sid] = c
    good = [c["chosen"] for c in res["cameras"].values() if c.get("ok")]
    res["pod_again"] = max(good) if good else None     # the darkest sky's need
    return res


def format_table(res):
    L = ["night gain calibration  sun %s deg  moon %s  tol %.0f%%  exp %s us  ISP %d (1.0625x)" % (
        res["sun_alt"], ("alt %.0f deg, %.0f%% lit" % (res["moon"]["alt"], res["moon"]["phase"])) if res["moon"] else "?",
        100 * res["tol"], res["exp_us"], res["ispdgain"])]
    for sid, c in sorted(res["cameras"].items()):
        if not c.get("rows"):
            L.append("  %-8s  -- %s" % (sid, c.get("reason", "no data")))
            continue
        top = max(c["rows"], key=lambda r: r["again_set"])
        ref = best(c["rows"]) or top
        L.append("  %-8s  %s  sky %.2f  drift %s" % (
            sid, ("-> %.2fx" % (c["chosen"] / 1024.0)) if c.get("ok") else "INVALID: " + c.get("reason", ""),
            top["sky"], ("%.1f%%" % (100 * c["drift"])) if c.get("drift") is not None else "?"))
        for r in sorted(c["rows"], key=lambda r: -r["again_set"]):
            L.append("      again %6.2fx  bg %8.1f  noise %7.2f (%4.2f code)  nef %+5.1f%%  headroom %6.1fx  clip %.3f%%%s" % (
                r["again_set"] / 1024.0, r["mean_lin"], r["std_lin"], r.get("std_code", float("nan")),
                100 * (r["nef"] / ref["nef"] - 1), 4095.0 / max(r["mean_lin"], 1e-6), r.get("clip_pct", 0),
                ("  " + r["why"]) if r.get("why") else ("  <- best" if r is ref else "")))
    L.append("PROPOSED pod night analog gain: %s" % (
        ("%d (%.2fx)" % (res["pod_again"], res["pod_again"] / 1024.0)) if res["pod_again"] else "none (no valid camera)"))
    return "\n".join(L)


def update_settings(path, again, ispd):
    """Rewrite the -a/-i values of every Isp `manual` line (RMS init + night) in
    place, keeping the file's hand formatting. Backup first, atomic replace,
    re-parse and verify. Returns the backup path."""
    txt = open(path).read()
    data = json.loads(txt)
    if any(isinstance(e, list) and len(e) > 1 and e[0] == "Isp" and e[1] == "manual"
           for e in data.get("day", []) or []):
        raise RuntimeError("%s has a manual line in DAY -- refusing to guess which to change" % path)
    out, n = [], 0
    for line in txt.splitlines(True):
        if re.search(r'"Isp"\s*,\s*"manual"', line):
            line = re.sub(r'("-a"\s*,\s*")\d+(")', r"\g<1>%d\g<2>" % again, line)
            line = re.sub(r'("-i"\s*,\s*")\d+(")', r"\g<1>%d\g<2>" % ispd, line)
            n += 1
        out.append(line)
    new = "".join(out)
    chk = json.loads(new)                                  # must still be valid JSON
    for mode in ("init", "night"):
        for e in chk.get(mode, []) or []:
            if isinstance(e, list) and len(e) > 1 and e[0] == "Isp" and e[1] == "manual":
                if str(again) not in e or str(ispd) not in e:
                    raise RuntimeError("verification failed for %s line %s" % (mode, e))
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(new)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    return bak, n


def apply(pod, res):
    """Set the proposed gain on every camera (saved, as RMS would) and in each
    station's settings file; log the calibration. Returns a list of notes."""
    a, i, e = res["pod_again"], res["ispdgain"], res["exp_us"]
    if not a:
        raise RuntimeError("nothing to apply")
    notes = []
    for s in pod.stations:
        r = send(s.ip, "manual -a %d -i %d -e %d" % (a, i, e), timeout=10)
        notes.append("%s: %s" % (s.id, "set" if r and "ERROR" not in r else "FAILED (%s)" % (r or "no answer")))
    for path in sorted({s.settings_path for s in pod.stations if s.settings_path}):
        bak, n = update_settings(path, a, i)
        notes.append("%s: %d manual line(s) -> -a %d -i %d (backup %s)" % (path, n, a, i, os.path.basename(bak)))
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"applied": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **res}) + "\n")
    return notes


if __name__ == "__main__":
    import argparse
    from podcontrol.stations import get_pod
    from podcontrol.podctl import PodController
    ap = argparse.ArgumentParser(description="Night analog-gain calibration (dry run unless --apply).")
    ap.add_argument("--tol", type=float, default=0.02, help="allowed sensitivity loss (default 0.02 = 2%%)")
    ap.add_argument("--frames", type=int, default=25)
    ap.add_argument("--only", help="comma-separated camera IPs to include (default: the whole pod)")
    ap.add_argument("--force", action="store_true", help="skip the sun-altitude guard (testing)")
    ap.add_argument("--apply", action="store_true", help="set the cameras and the settings JSON")
    args = ap.parse_args()
    stations = get_pod()
    if args.only:
        keep = set(args.only.split(","))
        stations = [s for s in stations if s.ip in keep]
    pod = PodController(stations)
    res = calibrate_pod(pod, tol=args.tol, frames=args.frames, force=args.force,
                        on_row=lambda st, r: print("  %s again %.2fx  nef %.3f  sky %.2f" % (
                            st.id, r["again_set"] / 1024.0, r["nef"], r["sky"]), flush=True))
    print(format_table(res))
    if args.apply:
        for n in apply(pod, res):
            print("  " + n)
