"""Night analog-gain calibration: the lowest analog gain that loses no sensitivity.

At night RMS pins exposure (40 ms), analog gain and ISP digital gain. Above some
analog gain the sky's own shot noise, not the sensor's read noise, sets the
noise floor; any gain beyond that point adds no sensitivity and only spends
highlight headroom (bright meteors clip sooner). This finds that point.

For each camera it sweeps the analog gain down from the top. The figure of
merit is the NOISE-EQUIVALENT FLUX measured end to end: the frame-to-frame noise
of the sky divided by the camera's MEASURED response to light at that operating
point. Both are read on the same output, so everything after the sensor -- the
gamma table's toe, black-level offsets, the ISP gain, 8-bit coding -- scales
signal and noise alike and cancels. The response is measured, not assumed: each
step is read at the night exposure, at EXP_STEP (0.7x) of it, and at the night
exposure again (the bracket cancels a drifting sky); exposure is exactly
proportional to the sky flux, so the mean's change per unit of exposure IS the
response. noise / response is the noise as a fraction of the sky signal: for an
ideal sensor 1/sqrt(S), S = sky electrons per pixel per frame, and anything the
camera adds (read noise rising at low analog gain, losses near black) shows as
an excess over that. Every step runs at the PRODUCTION ISP gain (the camera's
floor), i.e. on the frames RMS gets.

Why (2026-10-05). Earlier versions divided the noise by the REPORTED total gain,
assuming the response tracks it. That was checked on the CV300 (.102,
2026-09-26: within 2% from 5.6x to 22x) but does not hold on the Goke/IMX307 at
low output levels: on .205 the measured response per unit gain fell 1% at 11x,
4% at 8x and 8% at 5.66x while the noise rose. The 10-03 variant that held the
TOTAL gain constant with a compensating ISP gain lifted every step clear of that
region and proposed 5.66x on a sky that needs 16x. Measured on .205 that night
(moon down; noise/response, two repeats within 0.6%): 22.4x +1.6%, 16x best,
11.3x +10.6%, 8x +17%, 5.66x +26%. The photon-transfer split (change of variance
per change of mean = linear units per electron) gave the sky as 10.7-11.0
e-/pixel/frame at 16-22x: sky shot noise 3.3 e-, so to stay within 2% of it
everything else must be under ~0.2*sqrt(S) = 0.7 e-, which this camera reaches
only at >= 16x. The knee moves with the sky: a darker sky (fewer electrons)
needs MORE gain, a brighter or moonlit one less (roughly as 1/sqrt(S)).

At every step the camera measures the statistics itself (`noise_stats`, on the
uncompressed picture), so no second RTSP stream is opened while RMS captures.

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
  - the black clip: a sky within BLACK_SIGMA (2.5) sigma of zero has its lower
    tail clipped. The sigma is the one the higher-gain steps PREDICT (their
    noise-equivalent flux times this step's response), not the measured one:
    clipping shrinks the measured noise, so a clipped step judged by its own
    noise passes its own test. (3 sigma until 2026-10-05; the darkest pod skies
    sit at ~3.2 sigma at every gain, and the guard flipped 16x/22x on them);
  - a dip: the noise-equivalent flux falling more than DIP below the steps
    above it. Less gain can never make a camera more sensitive, so such a drop
    is the clip under-reading the noise. The 2026-09-29 moonlit run on .102
    picked exactly that: flat within ~5% from 22x down to 8x on every camera,
    then 20-40% "better" at the one or two lowest steps, each passing the old
    measured-sigma guard at 3.0-3.3 sigma.
A step is also not eligible when its gain or exposure was not applied, or when
its two night-exposure reads differ by more than STEP_DRIFT; such a step is
measured again (STEP_RETRIES) rather than lost -- a lost best step pushes that
camera's choice up a whole step (D1, 2026-10-05: 16x lost -> 22.39x for the
pod). The bracketed measurement repeats within ~0.6%, so steps are compared with
the BEST eligible noise-equivalent flux.

THE POD'S GAIN comes from all cameras at once (pod_decision). Converted to
electrons at the sensor, the noise a gain adds beyond the best step is a
property of the CAMERA, not of the sky: x = sqrt(S*((nef/best)^2 - 1)) came out
the same on S = 18.5 and S = 10.5 e- skies (2026-10-05: 11.3x 1.4-1.7 e-, 8x
2.0-2.2, 5.66x 2.3-2.6, 4x 3.2-3.3 on all six Goke cameras). So x per gain is the
MEDIAN over the pod's cameras of one platform, and the darkest sky -- the fewest
electrons, the most to lose -- sets the allowance: within tol of sky-limited
needs x^2 <= S*((1+tol)^2 - 1), ~0.65 e- at S = 10.5. One disturbed step on one
camera cannot move the pod. Each camera's own choice is still shown.

ISP digital gain is not swept. It comes after the ADC, so it adds no signal; it
only has to restore full scale after the black-level subtraction:
4095 / (4095 - 240) = 1.0625x (1088). Below that a saturated pixel never
reaches code 255 (measured on the Moon, 2026-09-25).

Guards: the sun must be well below the horizon; each camera's sweep ends by
re-measuring its first step, and a sky whose FLUX moved by more than DRIFT_MAX
(clouds, the Moon rising) invalidates that camera's run. Flux, not level: the
comparison is on the response (the exposure bracket's difference), because on
the Goke the black level moves ~1 DN at every gain change and the same setting
revisited reads up to +-1 linear unit off (LEDGER goke_black_wander) -- that,
not the sky, was the 6% 'drift' of 2026-10-05 07:18; the Moon's altitude and phase are recorded,
because a moonlit sky favours a lower gain than a dark one. Sweep steps are
LIVE only (never saved); every camera is handed back its saved state
(`ae_restore`) when the sweep ends. Nothing persists until apply().

    python -m podcontrol.nightcal                 # measure + propose (dry run)
    python -m podcontrol.nightcal --apply         # ... then set cameras + JSON
"""
import json, math, os, re, shutil, threading, time
from concurrent.futures import ThreadPoolExecutor

from podcontrol.podctl import send, send_live

BLACK_LEVEL = 240                                          # 12-bit pedestal, both sensors
ISPD_FULL_SCALE = int(round(4095 * 1024 / (4095 - BLACK_LEVEL)))   # 1088 = 1.0625x
STEPS = [22924, 16384, 11585, 8192, 5793, 4096]            # x1024, 3 dB apart; 22924 = IMX307 max
EXP_STEP = 0.7                                             # response = mean's change from this x exposure
STEP_DRIFT = 0.03                                          # a step's two night-exposure reads must agree
BLACK_SIGMA = 2.5                                          # sky this many sigma above black (clip guard)
STEP_RETRIES = 2                                           # re-measure a step whose sky moved, this many times
DRIFT_MAX = 0.05                                           # sky flux change over the sweep (response-based)
DIP = 0.10                                                 # nef this far below the steps above = artifact
SETTLE_S = 2.0                                             # s after each setting change (see _measure)
BLACK_SETTLE_MIN_S = 12.0                                  # after a GAIN change: watch the black this long ...
BLACK_SETTLE_MAX_S = 20.0                                  # ... and at most this long (_black_settle)
SUN_MAX_ALT = -12.0                                        # deg: nautical night or darker
LOG = os.path.expanduser("~/.config/podcontrol/nightcal.jsonl")
# Every run, dry or applied, writes every raw camera read here (one JSON line each), so an odd
# row can be traced to what the camera actually measured instead of argued about.
RUN_DIR = os.path.expanduser("~/.config/podcontrol/nightcal_runs")
_run = {"path": None, "lock": threading.Lock(), "t0": None}


def _trace(rec):
    """Append one record to the current run's trace (no-op outside calibrate_pod)."""
    if not _run["path"]:
        return
    rec = dict(rec, t_rel=round(time.time() - _run["t0"], 3))
    try:
        with _run["lock"], open(_run["path"], "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


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


def _measure(ip, again, ispd, exp_us, frames, settle_s, platform=None, prod_ispd=ISPD_FULL_SCALE,
             tag=None):
    tag = dict(tag or {}, ip=ip, req_again=again, req_ispd=ispd, req_exp=exp_us)
    t_set = time.time()
    reply = send_live(ip, "manual -a %d -i %d -e %d" % (again, ispd, exp_us))
    _trace(dict(tag, ev="set", t=t_set, dt_set=round(time.time() - t_set, 3), reply=(reply or "")[:120]))
    gain_changed = _last_gain.get(ip) != (again, ispd)
    _last_gain[ip] = (again, ispd)
    # The picture reads ~5% low for ~1.2 s after a gain change (Goke .206, ae_stats sampled
    # every 0.4 s, 2026-10-03); a 1 s settle sometimes measured inside that window, which the
    # drift check then blamed on the sky. 2 s clears it -- except the black level after a GAIN
    # change, which is waited for explicitly (_black_settle).
    time.sleep(settle_s)
    if gain_changed:
        _black_settle(ip, tag)
    # The step must be measured at the gain that was set. 2026-10-05 (.205) a sweep's 11.31x
    # row carried the 16x row's background and noise to 1% -- the measured frames were not at
    # 11.31x -- and was chosen. noise_stats reports the gains in effect over its frames:
    # re-measure until they match the request, else the step is not trusted.
    for attempt in range(3):
        t_rd = time.time()
        raw = send(ip, "noise_stats %d" % frames, timeout=frames / 5.0 + 20)
        r = parse_noise(raw)
        _trace(dict(tag, ev="read", read_try=attempt + 1, t=t_rd, since_set=round(t_rd - t_set, 3),
                    dt_read=round(time.time() - t_rd, 3), raw=(raw or "").strip()[:400],
                    gain_ok=bool(r and _gain_applied(r, again, ispd, exp_us))))
        if r is None:
            return None
        if _gain_applied(r, again, ispd, exp_us):
            break
        t_set = time.time()
        send_live(ip, "manual -a %d -i %d -e %d" % (again, ispd, exp_us))
        time.sleep(settle_s)
    r["gain_ok"] = _gain_applied(r, again, ispd, exp_us)
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


_last_gain = {}                 # ip -> (again, ispd) last set by this module (a gain change -> _black_settle)


def _black_settle(ip, tag, min_s=BLACK_SETTLE_MIN_S, max_s=BLACK_SETTLE_MAX_S, frames=5):
    """After an analog-gain change, watch the picture's level until the black has re-settled.
    On some IMX307s the black level JUMPS by ~2 DN some 5-10 s after a gain change (2026-10-05
    trace 075630: F1's first step read -2.0 units at +2 s and still at +5.7 s, settled by +9.4 s;
    D1 11.3x jumped during its +5.7 s read, A1 during its +9.4 s read; the noise is unchanged,
    so it is an offset; E1 and B1 settle within 2 s). A read before the jump is offset by ~2
    units: that tripped 'sky moved during the step' and made F1's 21% 'drift'. Two quick reads
    agreeing is not enough (they agree before the jump), so the level is watched for at least
    min_s, then until the last two quick reads agree within 0.25 units (or 0.4%). Every quick
    read is traced (ev "settle"), so a run shows when each camera's black moved. Exposure
    changes do not do this. Returns the seconds waited."""
    t0 = time.time(); prev = None
    while True:
        raw = send(ip, "noise_stats %d" % frames, timeout=frames / 5.0 + 20)
        r = parse_noise(raw)
        m = r["mean_lin"] if r else None
        el = time.time() - t0
        _trace(dict(tag, ev="settle", since_gain=round(el, 3), mean=m))
        if (el >= min_s and m is not None and prev is not None
                and abs(m - prev) <= max(0.25, 0.004 * prev)):
            return el
        if el > max_s:
            _trace(dict(tag, ev="settle_timeout", waited=round(el, 3)))
            return el
        prev = m
        time.sleep(max(0.0, (t0 + el + 1.0) - time.time()))      # ~1 read/s: light on the camera


def _gain_applied(r, again, ispd, exp_us=None, tol=0.03):
    """Do the gains and exposure noise_stats measured at match the request? (the sensor rounds
    the analog gain to its own steps: 16384 -> 16229 on the IMX307, hence the tolerance)"""
    ra, ri, re_ = r.get("again"), r.get("ispdgain"), r.get("exp_us")
    return ((ra is None or abs(ra - again) <= tol * again) and
            (ri is None or abs(ri - ispd) <= tol * ispd) and
            (exp_us is None or re_ is None or abs(re_ - exp_us) <= tol * exp_us))


def _measure_step(ip, again, ispd, exp_us, frames, settle_s, platform=None, tag=None):
    """One sweep step at the production ISP gain: the night exposure, EXP_STEP of it, the
    night exposure again. Returns the row (None if the camera has no noise_stats): the sky's
    mean and noise at the night exposure, and the MEASURED response `resp` -- the mean's
    change per unit of sky flux -- so nef = noise / response (see the module docstring)."""
    lo_us = int(round(exp_us * EXP_STEP))
    tag = dict(tag or {}, step=again)
    hi1 = _measure(ip, again, ispd, exp_us, frames, settle_s, platform, prod_ispd=ispd, tag=dict(tag, role="hi1"))
    if hi1 is None:
        return None
    lo = _measure(ip, again, ispd, lo_us, frames, settle_s, platform, prod_ispd=ispd, tag=dict(tag, role="lo"))
    hi2 = _measure(ip, again, ispd, exp_us, frames, settle_s, platform, prod_ispd=ispd, tag=dict(tag, role="hi2"))
    if lo is None or hi2 is None:
        return None
    r = dict(hi1)
    mh = (hi1["mean_lin"] + hi2["mean_lin"]) / 2.0
    vh = (hi1["std_lin"] ** 2 + hi2["std_lin"] ** 2) / 2.0
    dm = mh - lo["mean_lin"]
    r.update(mean_lin=mh, std_lin=math.sqrt(vh), lo_mean=lo["mean_lin"], lo_std=lo["std_lin"],
             exp_lo_us=lo_us, resp=dm / (1.0 - lo_us / float(exp_us)),
             step_drift=(abs(hi1["mean_lin"] - hi2["mean_lin"]) / mh) if mh > 0 else None,
             gain_ok=bool(hi1.get("gain_ok") and lo.get("gain_ok") and hi2.get("gain_ok")))
    r["nef"] = r["std_lin"] / r["resp"] if r["resp"] > 0 else float("inf")
    # photon transfer: variance per unit of mean = linear units per electron. Meaningful where
    # the camera behaves as an ideal photon counter (the top gains); reported, never used to choose
    k = (vh - lo["std_lin"] ** 2) / dm if dm > 0 else None
    r["sky_e"] = (r["resp"] / k) if (k and k > 0) else None
    r["sky"] = mh / r["total_gain"]                # gain-free sky, for the sweep's drift check
    lpc = r.get("lin_per_code") if r.get("decode") == "table" else None
    r["std_code"] = r["std_lin"] / (lpc if lpc else code_step(mh))
    r["prod_bg"], r["prod_std"] = mh, r["std_lin"]
    r["prod_std_code"] = r["prod_std"] / prod_code_width(mh, platform)
    r["eligible"], r["why"] = eligible(r)
    _trace(dict(tag, ev="step", ip=ip, resp=r["resp"], nef=r["nef"], step_drift=r["step_drift"],
                mean_hi1=hi1["mean_lin"], mean_lo=lo["mean_lin"], mean_hi2=hi2["mean_lin"], std=r["std_lin"],
                sky_e=r["sky_e"], eligible=r["eligible"], why=r["why"]))
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
    """'cv300' | 'goke' from the camera's sysinfo, None if it does not say. The K662
    (GK7605V100) has the Goke ISP and its 1025-node gamma table: 'goke' here."""
    s = send(ip, "sysinfo", timeout=8) or ""
    if "hi3516cv300" in s:
        return "cv300"
    if "gk7205" in s or "gk7605" in s:
        return "goke"
    return None


def isp_floor(ip):
    """The camera's ISP digital gain floor (x1024): the black-level restoration
    4095/(4095 - pedestal), what `manual -i restore` sets (silicon_research science_gain.h:
    G3S 240 -> 1088, CV300 236 -> 1087, K662 200 -> 1077). None if the camera cannot say."""
    m = re.search(r"levels=\[(\d+)", send(ip, "blacklevel", timeout=8) or "")
    if not m:
        return None
    p = int(m.group(1))
    return (1024 * 4095 + (4095 - p) // 2) // (4095 - p) if 0 < p < 4095 else None


# ---- the night ISP gain recommendation ------------------------------------------------
# ISP gain comes after the ADC: it adds no signal-to-noise, so the ANALOG gain is chosen for
# sensitivity (choose()). What ISP gain still decides is how the night sky is REPRESENTED in
# the 8-bit gamma-0.5 video. Two failures, both cured only by output scale:
#  * quantization -- sky noise under ~1.5 codes stops dithering the 8-bit steps;
#  * the gamma table's straight first segment -- below code 16 (CV300) / 8 (Goke) the curve
#    is linear, and RMS's code^2 decode misreads it: faint-star photometry ~0.2-0.5 mag too
#    bright on a CV300 sky at codes 10-14 (model, 2026-10-05).
# The price is highlight headroom: everything above 4095/gain of the linear range clips.
ISPD_REC_MAX = 2048                         # the AUTO ceiling of the gain policy (science_gain.h)
TOE_CODE = {"cv300": 16, "goke": 8}         # end of the straight segment, in output codes
# Keeping the sky out of the straight segment only HEDGES RMS's code^2 decode: decoded with the
# camera's own table (`gamma decode`) the segment is good data -- finer, not coarser. Set False
# once RMS decodes with the table; quantization is then the only criterion. Both are reported.
AVOID_TOE = True


def lin_to_code(lin, platform):
    """Output code of a linear (12-bit) level under the camera's table (toe, then pure 0.5)."""
    if platform == "cv300" and lin < 16.0:
        return lin * 15.94 / 16.0
    if platform == "goke" and lin < 4.0:
        return lin * 7.97 / 4.0
    return math.sqrt(max(lin, 0.0) * 65025.0 / 4095.0)


def recommend_ispd(row, floor, platform, min_std_code=None, cap=ISPD_REC_MAX, avoid_toe=True):
    """Lowest ISP gain in [floor, cap] at which this measured sky (one sweep row) carries
    >= min_std_code of noise (and, with avoid_toe, sits above the straight segment). -> dict
    with the gain and what it achieves; 'met' False when even the cap is not enough (the cap
    is returned)."""
    if min_std_code is None:
        min_std_code = MIN_STD_CODE
    meas = float(row.get("ispdgain") or row.get("ispd_set") or floor)
    toe = TOE_CODE.get(platform, 16)
    best = None
    for f in list(range(int(floor), int(cap) + 1, 8)) + [int(cap)]:
        bg, sd = row["mean_lin"] * f / meas, row["std_lin"] * f / meas
        code = lin_to_code(bg, platform)
        sc = sd / prod_code_width(bg, platform)
        cand = {"ispd": f, "sky_code": code, "std_code": sc, "headroom_pct": 100.0 * 1024.0 / f}
        if (code > toe or not avoid_toe) and sc >= min_std_code:
            cand["met"] = True
            return cand
        best = cand
    best["met"] = False
    return best


MIN_STD_CODE = 1.5         # noise below this many output codes is under-read (see the docstring)


def eligible(r, min_std_code=MIN_STD_CODE, min_sigma=BLACK_SIGMA):
    """Is this step's noise figure trustworthy? (ok, reason)"""
    if r.get("gain_ok") is False:
        return False, "gain/exposure not applied (measured again %s ispd %s exp %s)" % (
            r.get("again"), r.get("ispdgain"), r.get("exp_us"))
    if r.get("step_drift") is not None and r["step_drift"] > STEP_DRIFT:
        return False, "sky moved during the step (%.1f%%)" % (100 * r["step_drift"])
    if r.get("resp") is not None and r["resp"] <= 0:
        return False, "no response to the exposure step"
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


def screen(rows, min_sigma=BLACK_SIGMA, dip=DIP):
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
            sigma = ref * (r.get("resp") or r["total_gain"])
            if r["mean_lin"] < min_sigma * sigma:
                r["eligible"], r["why"] = False, "black clip (sky %.1f predicted sigma)" % (r["mean_lin"] / sigma)
                continue
            if r["nef"] < ref * (1.0 - dip):
                r["eligible"], r["why"] = False, "dip (nef %.0f%% below the steps above)" % (100 * (1 - r["nef"] / ref))
                continue
        above.append(r["nef"])
    return rows


def sweep_camera(station, exp_us, ispd=None, steps=STEPS, frames=25,
                 settle_s=SETTLE_S, on_row=None):
    """Sweep one camera. Returns {rows, drift, chosen?, ok, reason}; never saves.
    Each analog step is measured by _measure_step at the production ISP gain `ispd`
    (default: the camera's own floor, which differs by pedestal: 1088 / 1087 / 1077)."""
    ip = station.ip
    _last_gain.pop(ip, None)                   # the camera's gain is whatever RMS left: a change
    out = {"rows": [], "drift": None, "ok": False, "reason": ""}
    plat = platform_of(ip)
    ispd = ispd or isp_floor(ip) or ISPD_FULL_SCALE
    out["isp_floor"] = ispd
    out["platform"] = plat
    try:
        for a in steps:
            # a step whose two night-exposure reads disagree caught the sky moving (a cloud edge,
            # a plane): measure it again rather than lose it -- a lost best step pushes the choice
            # up a whole step (D1, 2026-10-05: 16x lost to a 5.4% jump -> 22.39x for the pod)
            for attempt in range(1 + STEP_RETRIES):
                r = _measure_step(ip, a, ispd, exp_us, frames, settle_s, plat,
                                  tag={"cam": station.id, "step_try": attempt + 1})
                if r is None or r.get("step_drift") is None or r["step_drift"] <= STEP_DRIFT:
                    break
            if r is not None:
                r["attempts"], r["t"] = attempt + 1, time.time()
            if r is None:
                probe = send(ip, "noise_stats 2", timeout=10) or "no answer"
                out["reason"] = ("camera has no noise_stats (needs the 2026-09-25 image)"
                                 if "unknown command" in probe else "noise_stats failed: " + probe[:80])
                return out
            out["rows"].append(r)
            if on_row:
                on_row(station, r)
        # stability: the sky must not have moved during the sweep
        # Did the SKY change during the sweep? Compared on the RESPONSE (the mean's change across
        # the exposure bracket), which is proportional to the sky flux and free of the black
        # level. The level itself is not a sky measure on the Goke: the IMX307's black moves
        # ~1 DN at every gain change, so the same setting revisited a minute later reads up to
        # +-1 linear unit off -- 6% of the darkest skies (2026-10-05, LEDGER goke_black_wander).
        rep = _measure_step(ip, steps[0], ispd, exp_us, frames, settle_s, plat,
                            tag={"cam": station.id, "final": True})
        if rep is not None:
            r0 = out["rows"][0]
            out["drift"] = (abs(rep["resp"] - r0["resp"]) / r0["resp"]) if r0.get("resp") else None
        out["ok"] = True
    finally:
        send(ip, "ae_restore", timeout=10)     # back to the saved (RMS) state
    return out


def sky_electrons(rows):
    """Sky electrons per pixel per frame: the photon-transfer value of the two highest eligible
    gains, where the camera behaves as an ideal photon counter (they agree within ~3%). Both
    inputs are differences across the exposure bracket, so the black level drops out."""
    v = [r["sky_e"] for r in sorted(rows, key=lambda r: -r["again_set"])
         if r.get("eligible", True) and r.get("sky_e") and r["sky_e"] > 0][:2]
    return sum(v) / len(v) if v else None


def excess_electrons(rows, sky_e):
    """Per step: the camera's noise beyond the best step's, in electrons at the sensor. The
    measured noise-equivalent flux is sqrt(S + x^2)/S against sqrt(S)/S at the best step, so
    x = sqrt(S * ((nef/best)^2 - 1)). It is a property of the camera at that gain, not of the
    sky: 2026-10-05 it came out the same on S = 18.5 and S = 10.5 e- skies (11.3x 1.6-1.7 e-,
    8x 2.0-2.1, 5.66x 2.6, 4x 3.2-3.4)."""
    b = best_nef(rows)
    for r in rows:
        r["excess_e"] = (math.sqrt(max(0.0, sky_e * ((r["nef"] / b) ** 2 - 1.0)))
                         if (b and sky_e and r.get("eligible", True)) else None)


def pod_decision(cameras, tol):
    """One analog gain for the pod, from all cameras at once: the camera's excess noise per
    gain is the MEDIAN over the pod's cameras of the same platform (the same sensor), and the
    darkest sky -- the fewest electrons, the most to lose -- sets the allowance: within tol of
    sky-limited needs x^2 <= S * ((1 + tol)^2 - 1). A single disturbed step on one camera then
    cannot move the pod. Returns (again, detail) or (None, reason)."""
    out, detail = [], {}
    by_plat = {}
    for sid, c in cameras.items():
        if c.get("ok") and c.get("sky_e"):
            by_plat.setdefault(c.get("platform") or "?", []).append((sid, c))
    for plat, cams in by_plat.items():
        dark_sid, dark = min(cams, key=lambda sc: sc[1]["sky_e"])
        allow = math.sqrt(dark["sky_e"] * ((1.0 + tol) ** 2 - 1.0))
        pooled = {}
        for g in sorted({r["again_set"] for _, c in cams for r in c["rows"]}):
            v = sorted(r["excess_e"] for _, c in cams for r in c["rows"]
                       if r["again_set"] == g and r.get("excess_e") is not None)
            if v:
                pooled[g] = v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2.0
        ok = [g for g, x in pooled.items() if x <= allow]
        if not ok:
            continue
        g = min(ok)
        out.append(g)
        detail[plat] = {"again": g, "allow_e": allow, "dark_sky_e": dark["sky_e"], "dark": dark_sid,
                        "n": len(cams), "pooled_excess_e": pooled}
    return (max(out), detail) if out else (None, detail)


def best_nef(rows):
    """The best (lowest) noise-equivalent flux among the eligible steps, or None."""
    v = [r["nef"] for r in rows if r.get("eligible", True)]
    return min(v) if v else None


def choose(rows, tol):
    """Lowest eligible analog gain whose noise-equivalent flux is within tol of the best."""
    p = best_nef(rows)
    if p is None:
        return None
    ok = [r for r in rows if r.get("eligible", True) and r["nef"] <= p * (1.0 + tol)]
    return min(ok, key=lambda r: r["again_set"])["again_set"]


def calibrate_pod(pod, tol=0.02, frames=25, drift_max=DRIFT_MAX, force=False, on_row=None):
    """Measure every camera in parallel and propose one pod-wide night gain."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        _run["path"] = os.path.join(RUN_DIR, time.strftime("nightcal_%Y%m%d_%H%M%S.jsonl", time.gmtime()))
    except OSError:
        _run["path"] = None
    _run["t0"] = time.time()
    try:
        res = _calibrate_pod(pod, tol, frames, drift_max, force, on_row)
        res["trace"] = _run["path"]
        _trace({"ev": "result", "pod_again": res.get("pod_again"), "pooled": res.get("pooled"),
                "cameras": {sid: {k: c.get(k) for k in ("ok", "reason", "drift", "chosen", "sky_e")}
                            for sid, c in res["cameras"].items()}})
        return res
    finally:
        _run["path"] = None


def _calibrate_pod(pod, tol, frames, drift_max, force, on_row):
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
    _trace({"ev": "start", "sun_alt": res["sun_alt"], "moon": res["moon"], "exp_us": nl[2], "tol": tol,
            "frames": frames, "settle_s": SETTLE_S, "cameras": [(s.id, s.ip) for s in pod.stations]})
    with ThreadPoolExecutor(max_workers=len(pod.stations)) as ex:
        futs = {s.id: ex.submit(sweep_camera, s, nl[2], None, STEPS, frames, SETTLE_S, on_row)
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
                    c["sky_e"] = sky_electrons(c["rows"])
                    excess_electrons(c["rows"], c["sky_e"])
                    c["chosen"] = choose(c["rows"], tol)
                    if c["chosen"] is None:
                        c["ok"] = False
                        c["reason"] = "no step with a trustworthy noise figure"
                    else:
                        row = next(r for r in c["rows"] if r["again_set"] == c["chosen"])
                        fl = c.get("isp_floor") or ISPD_FULL_SCALE
                        c["isp_q"] = recommend_ispd(row, fl, c.get("platform"), avoid_toe=False)
                        c["isp_t"] = recommend_ispd(row, fl, c.get("platform"), avoid_toe=True)
                        c["isp"] = c["isp_t"] if AVOID_TOE else c["isp_q"]
            res["cameras"][sid] = c
    good = [c["chosen"] for c in res["cameras"].values() if c.get("ok")]
    res["per_camera_max"] = max(good) if good else None
    res["pod_again"], res["pooled"] = pod_decision(res["cameras"], tol)
    if res["pod_again"] is None:
        res["pod_again"] = res["per_camera_max"]      # no sky electrons: the darkest camera's own choice
    # ISP gain: the pod's highest need too (the darkest sky); "restore" when every camera is
    # served by its own floor, so the settings file keeps the per-camera floor
    recs = [c for c in res["cameras"].values() if c.get("ok") and c.get("isp")]
    if recs:
        need = max(c["isp"]["ispd"] for c in recs)
        at_floor = all(c["isp"]["ispd"] <= (c.get("isp_floor") or ISPD_FULL_SCALE) for c in recs)
        res["pod_ispd"] = "restore" if at_floor else need
    else:
        res["pod_ispd"] = None
    return res


def format_table(res):
    L = ["night gain calibration  sun %s deg  moon %s  tol %.0f%%  exp %s us  ISP %d (1.0625x)" % (
        res["sun_alt"], ("alt %.0f deg, %.0f%% lit" % (res["moon"]["alt"], res["moon"]["phase"])) if res["moon"] else "?",
        100 * res["tol"], res["exp_us"], res["ispdgain"]),
        "  nef = noise / measured response (exposure step): the noise as a fraction of the sky signal; lower is better"]
    for sid, c in sorted(res["cameras"].items()):
        if not c.get("rows"):
            L.append("  %-8s  -- %s" % (sid, c.get("reason", "no data")))
            continue
        top = max(c["rows"], key=lambda r: r["again_set"])
        ref_nef = best_nef(c["rows"]) or top["nef"]
        pick = c.get("chosen") if c.get("ok") else None
        decs = set(r.get("decode") for r in c["rows"])
        dec = ("decode table" if decs == {"table"} else
               "decode code^2 (old image)" if decs == {None} else
               "decode " + "/".join(sorted(str(d or "code^2") for d in decs)))
        se = c.get("sky_e") or top.get("sky_e")
        L.append("  %-8s  %s  sky %s  drift %s  %s" % (
            sid, ("-> %.2fx" % (c["chosen"] / 1024.0)) if c.get("ok") else "INVALID: " + c.get("reason", ""),
            ("%.1f e-/px/frame" % se) if se else "?",
            ("%.1f%%" % (100 * c["drift"])) if c.get("drift") is not None else "?", dec))
        for r in sorted(c["rows"], key=lambda r: -r["again_set"]):
            x = r.get("excess_e")
            L.append("      again %6.2fx  bg %7.1f  noise %6.2f (%4.2f code)  response %7.2f  nef %.4f %+6.1f%%  excess %s  headroom %6.1fx  clip %.3f%%%s%s" % (
                r["again_set"] / 1024.0, r["mean_lin"], r["std_lin"], r.get("std_code", float("nan")),
                r.get("resp", float("nan")), r["nef"], 100 * (r["nef"] / ref_nef - 1),
                ("%4.2f e-" % x) if x is not None else "   --  ",
                4095.0 / max(r["mean_lin"], 1e-6), r.get("clip_pct", 0),
                ("  (%d tries)" % r["attempts"]) if r.get("attempts", 1) > 1 else "",
                ("  " + r["why"]) if r.get("why") else ("  <- chosen" if r["again_set"] == pick else "")))
    for plat, d in sorted((res.get("pooled") or {}).items()):
        L.append("  pod (%s, %d cameras): camera excess noise per gain (median) %s" % (
            plat, d["n"], "  ".join("%.2fx %.2f" % (g / 1024.0, x) for g, x in sorted(d["pooled_excess_e"].items(), reverse=True))))
        L.append("  darkest sky %s: %.1f e-/px/frame -> within %.0f%% of sky-limited needs excess <= %.2f e- -> %.2fx" % (
            d["dark"], d["dark_sky_e"], 100 * res["tol"], d["allow_e"], d["again"] / 1024.0))
    if res.get("trace"):
        L.append("  trace: %s" % res["trace"])
    L.append("PROPOSED pod night analog gain: %s" % (
        ("%d (%.2fx)" % (res["pod_again"], res["pod_again"] / 1024.0)) if res["pod_again"] else "none (no valid camera)"))
    # ISP gain: what each camera needs for quantization alone, and with the sky kept out of the
    # gamma table's straight segment (only a hedge for RMS's code^2 decode -- AVOID_TOE)
    fmt = lambda i: "%4d (%.2fx) sky code %5.1f  noise %4.2f code  keeps %3.0f%% headroom%s" % (
        i["ispd"], i["ispd"] / 1024.0, i["sky_code"], i["std_code"], i["headroom_pct"], "" if i["met"] else "  NOT MET at the cap")
    for sid, c in sorted(res["cameras"].items()):
        if c.get("isp_q"):
            L.append("  ISP %-8s floor %4d | quantization only: %s" % (sid, c.get("isp_floor") or 0, fmt(c["isp_q"])))
            L.append("  %-12s %4s | + out of straight seg: %s" % ("", "", fmt(c["isp_t"])))
    pi = res.get("pod_ispd")
    L.append("PROPOSED pod night ISP gain: %s   (%s)" % (
        "restore (each camera's own floor)" if pi == "restore" else ("%d (%.2fx)" % (pi, pi / 1024.0)) if pi else "none",
        "quantization + straight segment avoided (AVOID_TOE: RMS still decodes code^2)" if AVOID_TOE
        else "quantization only (RMS decodes with the camera table)"))
    return "\n".join(L)


def update_settings(path, again, ispd):
    """Rewrite the -a/-i values of every Isp `manual` line (RMS init + night) in
    place, keeping the file's hand formatting. Backup first, atomic replace,
    re-parse and verify. Returns the backup path.

    ispd may be "restore" (the firmware computes the black-level restoration floor
    4095/(4095-pedestal) per camera, silicon_research science_gain.h); a number or
    "restore" already in the file is replaced either way."""
    txt = open(path).read()
    data = json.loads(txt)
    if any(isinstance(e, list) and len(e) > 1 and e[0] == "Isp" and e[1] == "manual"
           for e in data.get("day", []) or []):
        raise RuntimeError("%s has a manual line in DAY -- refusing to guess which to change" % path)
    out, n = [], 0
    for line in txt.splitlines(True):
        if re.search(r'"Isp"\s*,\s*"manual"', line):
            line = re.sub(r'("-a"\s*,\s*")\d+(")', r"\g<1>%d\g<2>" % again, line)
            line = re.sub(r'("-i"\s*,\s*")(?:\d+|restore)(")', r"\g<1>%s\g<2>" % ispd, line)
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
    # ISP gain: calibrate_pod's proposal -- "restore" (each camera's own black-level floor,
    # 1088 / 1087 / 1077 by pedestal) unless the night sky needs more for quantization (or, while
    # AVOID_TOE, to stay out of the gamma table's straight segment); then that number.
    a, i, e = res["pod_again"], res.get("pod_ispd") or "restore", res["exp_us"]
    if not a:
        raise RuntimeError("nothing to apply")
    notes = []
    for s in pod.stations:
        r = send(s.ip, "manual -a %d -i %s -e %d" % (a, i, e), timeout=10)
        notes.append("%s: %s" % (s.id, "set" if r and "ERROR" not in r else "FAILED (%s)" % (r or "no answer")))
    for path in sorted({s.settings_path for s in pod.stations if s.settings_path}):
        bak, n = update_settings(path, a, i)
        notes.append("%s: %d manual line(s) -> -a %d -i %s (backup %s)" % (path, n, a, i, os.path.basename(bak)))
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
