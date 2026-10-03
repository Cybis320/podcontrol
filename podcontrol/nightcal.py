"""Night analog-gain calibration: the lowest analog gain that loses no sensitivity.

At night RMS pins exposure (40 ms), analog gain and ISP digital gain. Above some
analog gain the sky's own shot noise, not the sensor's read noise, sets the
noise floor; any gain beyond that point adds no sensitivity and only spends
highlight headroom (bright meteors clip sooner). This finds that point.

For each camera it sweeps the analog gain down from the top at fixed exposure,
holding the TOTAL gain constant with the ISP digital gain (compensating_ispd,
2026-10-03): every step then lands at the same output level, so the 8-bit
output's quantization -- which made the CV300's low-noise steps read low and
"improve" with less gain -- affects every step alike and drops out. At every
step it asks the camera for `noise_stats` -- venc
measures the sky background and its frame-to-frame noise itself, on the
uncompressed picture, so no second RTSP stream is opened while RMS captures.
The figure of merit is the noise-equivalent flux (noise divided by total gain):
how faint a signal each gain can still separate from the sky. The reported gain
is the right divisor: on .102 (CV300, 2026-09-26) the response measured as the
40 ms - 20 ms background difference tracked the reported gain within 2% from
5.6x to 22x. On CV300 images before 971faffb the background did not scale with
gain at the low end either: the ISP subtracted 240 while the sensor's black sits
at ~236 (measured 2026-10-03), an offset rather than lost sensitivity, so the
background/noise ratio (snr) is not a fair score across gains.

CODE -> LINEAR. The camera reports `decode=table` (2026-10-03 venc): it converts
each 8-bit code through its LIVE gamma node table. The ISP draws straight lines
between nodes (CV300: one per 16 linear units, Goke: one per 4), so codes 0-16 on
the CV300 are LINEAR in light, not quadratic; the old pure-0.5 decode (code^2)
mis-read them by up to 2x in noise variance and made the noise per unit gain look
gain-dependent when it only tracked the output level (the "11.31x anomaly"). A
camera on an older image (no `decode=` in the reply) still decodes code^2: its
steps below code 16 are marked untrustworthy.

Three things corrupt a step's noise figure, and such steps are not eligible:
  - quantization: sky noise under ~1.5 codes of the 8-bit output is under-read
    (the width of one code in linear units is what the camera reports as
    lin_per_code; older images: assumed 2*code*4095/65025). Under a 0.5 gamma
    the shot noise is the same number of codes at every level, ~2*sqrt(K*gain):
    K ~0.025 on the CV300/IMX291, so below ~16x its noise is under 1.5 codes and
    reads LOW (2026-10-03: nef "improving" as gain fell -- impossible). K ~0.13 on
    the Goke/IMX307 keeps it above 1.5 codes down to 4x;
  - the black clip: a sky within 3 sigma of zero has its lower tail clipped.
    The sigma is the one the higher-gain steps PREDICT (their noise-equivalent
    flux times this gain), not the measured one: clipping shrinks the measured
    noise, so a clipped step judged by its own noise passes its own test;
  - a dip: the noise-equivalent flux falling more than DIP below the steps
    above it. Less gain can never make a camera more sensitive, so such a drop
    is the clip under-reading the noise. The 2026-09-29 moonlit run on .102
    picked exactly that: flat within ~5% from 22x down to 8x on every camera,
    then 20-40% "better" at the one or two lowest steps, each passing the old
    measured-sigma guard at 3.0-3.3 sigma.
On the sky-limited plateau the noise-equivalent flux scatters by a few percent
from step to step, so steps are compared with the plateau's MEDIAN (over the
eligible steps), not its single lowest point. The chosen gain is the lowest
eligible step within `tol` of that median. One value goes to the whole pod: the
HIGHEST of the per-camera choices, i.e. the darkest sky's need, so no camera
loses sensitivity.

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
ISPD_MAX = 16384                                           # 16x: the compensating ISP gain's ceiling
DIP = 0.10                                                 # nef this far below the steps above = artifact
SETTLE_S = 2.0                                             # s after each gain change (see _measure)
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
    m = re.search(r"\bdecode=(\w+)", text)
    d["decode"] = m.group(1) if m else None      # None: an older image that decodes code^2
    return d if "mean_lin" in d and "std_lin" in d else None


def night_line(station):
    """(again, ispdgain, exp_us) from RMS's night `manual` line, or None."""
    cmd = station.mode_cmd("night") or ""
    if not cmd.startswith("manual"):
        return None
    get = lambda f: (int(re.search(r"-%s\s+(\d+)" % f, cmd).group(1))
                     if re.search(r"-%s\s+(\d+)" % f, cmd) else None)
    return get("a"), get("i"), get("e")


def _measure(ip, again, ispd, exp_us, frames, settle_s, platform=None, prod_ispd=ISPD_FULL_SCALE):
    send_live(ip, "manual -a %d -i %d -e %d" % (again, ispd, exp_us))
    # The picture reads ~5% low for ~1.2 s after a gain change (Goke .206, ae_stats sampled
    # every 0.4 s, 2026-10-03); a 1 s settle sometimes measured inside that window, which the
    # drift check then blamed on the sky. 2 s clears it.
    time.sleep(settle_s)
    r = parse_noise(send(ip, "noise_stats %d" % frames, timeout=frames / 5.0 + 20))
    if r is None:
        return None
    g = (r.get("again") or again) / 1024.0 * (r.get("ispdgain") or ispd) / 1024.0
    r["again_set"] = again
    r["ispd_set"] = ispd
    r["platform"] = platform
    # the same step at the PRODUCTION ISP gain: background and noise scale with the ISP gain
    k = prod_ispd / float(r.get("ispdgain") or ispd)
    r["prod_bg"], r["prod_std"] = r["mean_lin"] * k, r["std_lin"] * k
    r["prod_std_code"] = r["prod_std"] / prod_code_width(r["prod_bg"], platform)
    r["total_gain"] = g
    r["nef"] = r["std_lin"] / g                # noise-equivalent flux: lower = more sensitive
    r["sky"] = r["mean_lin"] / g               # sky brightness in gain-free units
    lpc = r.get("lin_per_code") if r.get("decode") == "table" else None
    r["std_code"] = r["std_lin"] / (lpc if lpc else code_step(r["mean_lin"]))
    r["eligible"], r["why"] = eligible(r)
    return r


def prod_code_width(lin, platform):
    """Linear width of one output code at this level in a real frame: the gamma table's
    straight first segment (CV300 below 16 linear units: 1.0; Goke below 4: 0.5), else the
    pure 0.5 curve, which the nodes follow within ~1% above it."""
    if platform == "cv300" and lin < 16.0:
        return 16.0 / 15.94
    if platform == "goke" and lin < 4.0:
        return 4.0 / 7.97
    return code_step(lin)


def code_step(mean_lin):
    """Linear units per 8-bit output code at this level, ASSUMING a pure 0.5 decode
    (lin = code^2 * 4095/65025). Only for older images; a camera that reports
    decode=table gives the true width as lin_per_code."""
    code = math.sqrt(max(mean_lin, 0.0) * 65025.0 / 4095.0)
    return max(2.0 * code, 1.0) * 4095.0 / 65025.0


# Below this code an older image's code^2 decode is wrong: the gamma table's straight first
# segment (CV300: 257 nodes, first at 16 linear units = code 16; Goke: 1025, first at 4 = code 8).
OLD_DECODE_MIN_CODE = {"cv300": 16, "goke": 8}


def platform_of(ip):
    """'cv300' | 'goke' from the camera's sysinfo, None if it does not say."""
    s = send(ip, "sysinfo", timeout=8) or ""
    if "hi3516cv300" in s:
        return "cv300"
    if "gk7205" in s:
        return "goke"
    return None


MIN_STD_CODE = 1.5         # noise below this many output codes is under-read (see the docstring)


def eligible(r, min_std_code=MIN_STD_CODE, min_sigma=3.0):
    """Is this step's noise figure trustworthy? (ok, reason)"""
    lim = OLD_DECODE_MIN_CODE.get(r.get("platform"), 16)
    if r.get("decode") != "table" and r.get("mean_code", 99) < lim:
        return False, "old decode (code %.0f < %d: update the camera image)" % (r.get("mean_code", 0), lim)
    if r["std_code"] < min_std_code:
        return False, "quantized (noise %.2f code)" % r["std_code"]
    if r.get("prod_std_code") is not None and r["prod_std_code"] < min_std_code:
        # measured fine at the boosted level, but at night settings this gain's frames would
        # carry under 1.5 codes of noise: the 8-bit output itself would cost sensitivity
        return False, "night frames quantized (noise %.2f code)" % r["prod_std_code"]
    if r["mean_lin"] < min_sigma * r["std_lin"]:
        return False, "black clip (sky %.1f sigma)" % (r["mean_lin"] / max(r["std_lin"], 1e-9))
    return True, ""


def screen(rows, min_sigma=3.0, dip=DIP):
    """Mark the steps that only the whole sweep can expose (see the module
    docstring): a black clip judged by the predicted sigma, and a dip. Walks
    down from the top gain; each step is judged against the mean noise-
    equivalent flux of the eligible steps above it."""
    above = []
    for r in sorted(rows, key=lambda r: -r["again_set"]):
        if not r.get("eligible", True):
            continue
        if above:
            ref = sum(above) / len(above)
            sigma = ref * r["total_gain"]
            if r["mean_lin"] < min_sigma * sigma:
                r["eligible"], r["why"] = False, "black clip (sky %.1f predicted sigma)" % (r["mean_lin"] / sigma)
                continue
            if r["nef"] < ref * (1.0 - dip):
                r["eligible"], r["why"] = False, "dip (nef %.0f%% below the steps above)" % (100 * (1 - r["nef"] / ref))
                continue
        above.append(r["nef"])
    return rows


def compensating_ispd(again, top, ispd=ISPD_FULL_SCALE):
    """ISP digital gain (x1024) that holds the TOTAL gain at the top step's: every step then
    lands at the same output level, with the same noise in codes and the same code width,
    so the 8-bit output's quantization affects every step alike and drops out of the
    comparison. ISP gain comes after the ADC, so it scales signal and noise together and
    leaves the analog stage's noise -- what is being compared -- unchanged."""
    return int(min(ISPD_MAX, max(ispd, round(top * ispd / float(again)))))


def sweep_camera(station, exp_us, ispd=ISPD_FULL_SCALE, steps=STEPS, frames=25,
                 settle_s=SETTLE_S, on_row=None):
    """Sweep one camera. Returns {rows, drift, chosen?, ok, reason}; never saves.
    Each analog step is measured at the CONSTANT total gain of the top step
    (compensating_ispd); the production ISP gain is `ispd`."""
    ip = station.ip
    out = {"rows": [], "drift": None, "ok": False, "reason": ""}
    plat = platform_of(ip)
    try:
        for a in steps:
            r = _measure(ip, a, compensating_ispd(a, steps[0], ispd), exp_us, frames, settle_s, plat, prod_ispd=ispd)
            if r is None:
                probe = send(ip, "noise_stats 2", timeout=10) or "no answer"
                out["reason"] = ("camera has no noise_stats (needs the 2026-09-25 image)"
                                 if "unknown command" in probe else "noise_stats failed: " + probe[:80])
                return out
            out["rows"].append(r)
            if on_row:
                on_row(station, r)
        # stability: the sky must not have moved during the sweep
        rep = _measure(ip, steps[0], compensating_ispd(steps[0], steps[0], ispd), exp_us, frames, settle_s,
                       plat, prod_ispd=ispd)
        if rep is not None:
            s0 = out["rows"][0]["sky"]
            out["drift"] = abs(rep["sky"] - s0) / s0 if s0 else None
        out["ok"] = True
    finally:
        send(ip, "ae_restore", timeout=10)     # back to the saved (RMS) state
    return out


def plateau(rows):
    """Median noise-equivalent flux of the eligible steps, or None."""
    v = sorted(r["nef"] for r in rows if r.get("eligible", True))
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def choose(rows, tol):
    """Lowest eligible analog gain whose noise-equivalent flux is within tol of the plateau's."""
    p = plateau(rows)
    if p is None:
        return None
    ok = [r for r in rows if r.get("eligible", True) and r["nef"] <= p * (1.0 + tol)]
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
        futs = {s.id: ex.submit(sweep_camera, s, nl[2], ISPD_FULL_SCALE, STEPS, frames, SETTLE_S, on_row)
                for s in pod.stations}
        for sid, f in futs.items():
            try:
                c = f.result()
            except Exception as e:
                c = {"rows": [], "ok": False, "reason": str(e)}
            screen(c["rows"])                      # also for an invalid camera's table
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
        ref_nef = plateau(c["rows"]) or top["nef"]
        pick = c.get("chosen") if c.get("ok") else None
        decs = set(r.get("decode") for r in c["rows"])
        dec = ("decode table" if decs == {"table"} else
               "decode code^2 (old image)" if decs == {None} else
               "decode " + "/".join(sorted(str(d or "code^2") for d in decs)))
        L.append("  %-8s  %s  sky %.2f  drift %s  %s" % (
            sid, ("-> %.2fx" % (c["chosen"] / 1024.0)) if c.get("ok") else "INVALID: " + c.get("reason", ""),
            top["sky"], ("%.1f%%" % (100 * c["drift"])) if c.get("drift") is not None else "?", dec))
        for r in sorted(c["rows"], key=lambda r: -r["again_set"]):
            L.append("      again %6.2fx  isp %5.2fx  bg %8.1f  noise %7.2f (%4.2f code)  nef %+5.1f%%  night bg %6.1f (%4.2f code)  headroom %6.1fx  clip %.3f%%%s" % (
                r["again_set"] / 1024.0, r.get("ispd_set", res["ispdgain"]) / 1024.0, r["mean_lin"], r["std_lin"],
                r.get("std_code", float("nan")), 100 * (r["nef"] / ref_nef - 1),
                r.get("prod_bg", r["mean_lin"]), r.get("prod_std_code", float("nan")),
                4095.0 / max(r.get("prod_bg", r["mean_lin"]), 1e-6), r.get("clip_pct", 0),
                ("  " + r["why"]) if r.get("why") else ("  <- chosen" if r["again_set"] == pick else "")))
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
