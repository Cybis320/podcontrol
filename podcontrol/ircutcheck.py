"""IR-cut polarity check: does each camera's filter go where it is told?

The filter is a latching solenoid with no position feedback, and the modules are
wired both ways: .123 (G3S, XM V5.00.R02) put its filter OUT on the "day" pulse
(2026-10-08), found only by eye from a magenta daytime sky. The fix lives on the
camera, not in the pod's camera_settings JSON: each unit's own `ircut_swap` flag
(silicon_research 6667ac1) makes its /usr/bin/ircut pulse the other pin. This
module MEASURES which way each camera's filter actually moves, end to end through
that flag, and can flip the flag of a camera found reversed.

Method. With the exposure held, pulse `ircut day` and `ircut night` alternately
(D N D N D) and read the camera's own pre-white-balance colour statistics after
each (`wb_stats` -> wb_global r/g/b: linear frame means taken BEFORE the WB
gains, every frame -- no second stream, no RMS frame). Filter OUT passes near-
infrared: the red-to-green ratio rises (the NIR leaks mostly through the red
filters) and every channel gains signal. Measured: IMX307 R/G 0.35 -> 0.73
(.205/.123, daylight, pre-WB), IMX662 0.89 -> 1.37 (.111), IMX291 0.93 -> 1.76
(.101); the .123 night sky read ~2x brighter with the filter out.
  - each "night" reads a higher R/G than its two "day" neighbours  -> correct
  - lower                                                         -> REVERSED
  - no change -> the filter does not move (stuck, unwired) or the scene has no NIR
Each N is compared with the mean of its two D neighbours, so a sky that brightens
or darkens steadily (dawn, dusk) cancels -- read each sample against its
neighbours (2026-09-14). The verdict is the R/G ratio, which does not depend on
exposure, so an AE step mid-test cannot fake it; the green level is reported as
a second, exposure-dependent witness.

The pulses are `transient` (never saved) and the camera ends at its LAST
COMMANDED position, so nothing is left behind. But the filter moves 5-6 times in
~10-15 s and RMS records that. Best in daylight; at night the sky has less NIR.
"""
import json, math, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor

from podcontrol.podctl import send, send_live, _parse_query

RG_STEP = 0.08        # night/day R/G change that counts as the filter moving (daylight ~1.5-2x)
FLAT = 0.03           # both cycles within +-3%: the filter did not move (or no NIR in the scene)
CYCLES = 2            # D N D N D
READS = 4             # wb_stats reads averaged per state (one frame each, 80 ms apart)
READ_GAP_S = 0.08
SETTLE_S = 0.6        # after the pulse has finished (G3S 1 s, K662/CV300 200 ms) -> stats on new frames
CLIP_WARN = 0.90      # wb_stats count_all below this: part of the frame clipped, ratios less exact

RUN_DIR = os.path.expanduser("~/.config/podcontrol/ircut_checks")
_run = {"path": None, "lock": threading.Lock(), "t0": None}


def _trace(rec):
    if not _run["path"]:
        return
    rec = dict(rec, t_rel=round(time.time() - _run["t0"], 3))
    try:
        with _run["lock"], open(_run["path"], "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def parse_ircut(text):
    """`ircut` report -> (last commanded 'day'|'night'|None, swap 0|1|None). swap None = an image
    from before ircut_swap (it cannot be set there)."""
    m = re.search(r"ircut: (DAY|NIGHT|UNKNOWN)", text or "")
    last = {"DAY": "day", "NIGHT": "night"}.get(m.group(1)) if m else None
    s = re.search(r"ircut_swap: ([01])", text or "")
    return last, (int(s.group(1)) if s else None)


def read_rgb(ip, reads=READS, gap=READ_GAP_S):
    """Mean pre-WB R, G, B of the frame from the camera's `wb_stats`, averaged over `reads` frames."""
    vals = []
    for i in range(reads):
        m = re.search(r"wb_global r=(\d+) g=(\d+) b=(\d+) count_all=(\d+)", send(ip, "wb_stats", timeout=5) or "")
        if m:
            vals.append([int(x) for x in m.groups()])
        if i + 1 < reads:
            time.sleep(gap)
    if not vals:
        return None
    r, g, b, c = (sum(v[k] for v in vals) / len(vals) for k in range(4))
    return {"r": r, "g": g, "b": b, "rg": r / g if g else None, "bg": b / g if g else None,
            "count": c / 65535.0, "n": len(vals)}


def verdict(rows, step=RG_STEP, flat=FLAT):
    """rows = the D N D N D reads. Returns (verdict, per-cycle R/G ratios, per-cycle G ratios)."""
    ks, gs = [], []
    for i in range(1, len(rows) - 1, 2):
        d0, n, d1 = rows[i - 1], rows[i], rows[i + 1]
        if not (d0 and n and d1) or None in (d0["rg"], n["rg"], d1["rg"]):
            return "no data", ks, gs
        ks.append(n["rg"] / ((d0["rg"] + d1["rg"]) / 2.0))
        gs.append(n["g"] / ((d0["g"] + d1["g"]) / 2.0) if d0["g"] + d1["g"] else float("nan"))
    if not ks:
        return "no data", ks, gs
    if all(k >= 1 + step for k in ks):
        return "correct", ks, gs
    if all(k <= 1 / (1 + step) for k in ks):
        return "REVERSED", ks, gs
    if all(abs(math.log(k)) < math.log(1 + flat) for k in ks):
        return "no movement", ks, gs
    return "inconclusive", ks, gs


def check_camera(station, cycles=CYCLES, reads=READS, settle_s=SETTLE_S):
    """Measure one camera's filter polarity. Never saves anything on the camera."""
    ip = station.ip
    out = {"id": station.id, "ip": ip, "verdict": None, "reason": "", "last": None, "swap": None,
           "rows": [], "rg_ratio": [], "g_ratio": [], "notes": []}
    rep = send(ip, "ircut", timeout=8)
    if rep is None:
        out["verdict"], out["reason"] = "offline", "no answer on :9600"
        return out
    out["last"], out["swap"] = parse_ircut(rep)
    q = send(ip, "query", timeout=8) or ""
    t = _parse_query(q) if "Exposure Info" in q else {}
    pinned = False
    if t and t.get("optype") != "MANUAL":
        # AE running: hold its live exposure for the test (the green witness needs it, R/G does
        # not); ae_restore at the end hands the camera back to its saved (RMS) state
        a = int(round((t.get("again_x") or 1.0) * 1024))
        i = int(round((t.get("ispdgain_x") or 1.0) * 1024))
        e = t.get("exp_us") or 10000
        send_live(ip, "manual -a %d -d 1024 -i %d -e %d" % (a, i, e), timeout=8)
        pinned = True
    _trace({"cam": station.id, "ip": ip, "ev": "start", "last": out["last"], "swap": out["swap"],
            "optype": t.get("optype"), "pinned": pinned})
    try:
        for pos in ["day", "night"] * cycles + ["day"]:
            r = send_live(ip, "ircut " + pos, timeout=10)
            if not r or "ERROR" in r or "ircut:" not in r:
                out["verdict"], out["reason"] = "error", "ircut %s: %s" % (pos, (r or "no answer")[:80])
                return out
            time.sleep(settle_s)
            rgb = read_rgb(ip, reads)
            if rgb is None:
                out["verdict"], out["reason"] = "error", "no wb_stats (image predates 2026-09-28?)"
                return out
            out["rows"].append(dict(rgb, pos=pos))
            _trace(dict(rgb, cam=station.id, ev="read", pos=pos))
        v, ks, gs = verdict(out["rows"])
        out["verdict"], out["rg_ratio"], out["g_ratio"] = v, ks, gs
        if v in ("correct", "REVERSED") and any(g == g and (g > 1) != (v == "correct") for g in gs):
            out["notes"].append("green level disagrees with R/G -- exposure moved mid-test?")
        if min(r["count"] for r in out["rows"]) < CLIP_WARN:
            out["notes"].append("up to %.0f%% of the frame clipped in one state -- ratios less exact"
                                % (100 * (1 - min(r["count"] for r in out["rows"]))))
        if v == "no movement":
            out["reason"] = "the filter did not move, or the scene has no near-infrared (night): rerun in daylight"
        elif v == "inconclusive":
            out["reason"] = "the two cycles disagree -- the scene changed (clouds?); rerun"
    finally:
        end = out["last"]
        if end is None:
            try:
                from podcontrol.sunmask import mode_for
                end = mode_for(station)
            except Exception:
                end = None
            out["notes"].append("no ircut command saved yet -- left at %s" % (end or "day (last pulse)"))
        if end:
            send_live(ip, "ircut " + end, timeout=10)
        if pinned:
            send(ip, "ae_restore", timeout=10)
        _trace({"cam": station.id, "ev": "end", "verdict": out["verdict"], "rg_ratio": out["rg_ratio"],
                "g_ratio": out["g_ratio"], "restored": end, "ae_restore": pinned})
    return out


def recommendation(c):
    """What to do about one camera's result (text), or None."""
    if c.get("verdict") != "REVERSED":
        return None
    if c.get("swap") is None:
        return "reversed, but this image has no ircut_swap: flash a 6667ac1+ science image"
    return "set ircut_swap %d (now %d)" % (1 - c["swap"], c["swap"])


def check_pod(stations, cycles=CYCLES, on_done=None):
    """Check every camera in parallel. {station_id: result}."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        _run["path"] = os.path.join(RUN_DIR, time.strftime("ircut_%Y%m%d_%H%M%S.jsonl", time.gmtime()))
    except OSError:
        _run["path"] = None
    _run["t0"] = time.time()
    res = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(stations))) as ex:
            futs = {s.id: ex.submit(check_camera, s, cycles) for s in stations}
            for sid, f in futs.items():
                try:
                    res[sid] = f.result()
                except Exception as e:
                    res[sid] = {"id": sid, "verdict": "error", "reason": str(e), "rows": [], "notes": []}
                if on_done:
                    on_done(res[sid])
    finally:
        _run["path"] = None
    return res


def apply_swaps(stations, res):
    """Flip ircut_swap on every camera found REVERSED (persisted on the camera; the camera re-applies
    its last commanded position through the new mapping). Returns notes."""
    notes = []
    for s in stations:
        c = res.get(s.id) or {}
        if c.get("verdict") != "REVERSED" or c.get("swap") is None:
            continue
        r = send(s.ip, "ircut_swap %d" % (1 - c["swap"]), timeout=15) or "no answer"
        ok = "ERROR" not in r and re.search(r"ircut_swap: %d" % (1 - c["swap"]), r)
        notes.append("%s %s: ircut_swap %d -> %s" % (s.id, s.ip, 1 - c["swap"], "ok" if ok else r[:100]))
    return notes


def format_table(res):
    """One line per camera: the measured day and night R/G, then night/day per cycle."""
    fmt = "%-8s %-15s %-5s %-4s %-12s %-7s %-9s %-13s %-13s %s"
    lines = [fmt % ("camera", "ip", "last", "swap", "verdict", "R/G day", "R/G night",
                    "R/G change", "green change", "notes")]
    for sid in sorted(res):
        c = res[sid]
        mean = lambda pos: [r["rg"] for r in c.get("rows", []) if r["pos"] == pos and r["rg"] is not None]
        avg = lambda xs: "%.3f" % (sum(xs) / len(xs)) if xs else "-"
        f = lambda xs: " ".join("x%.2f" % x for x in xs) or "-"
        extra = "; ".join([x for x in [c.get("reason"), recommendation(c)] + c.get("notes", []) if x])
        lines.append(fmt % (
            sid, c.get("ip", ""), c.get("last") or "?", "-" if c.get("swap") is None else c["swap"],
            c.get("verdict"), avg(mean("day")), avg(mean("night")),
            f(c.get("rg_ratio", [])), f(c.get("g_ratio", [])), extra))
    lines.append("")
    lines.append("R/G = pre-white-balance red/green. The night pulse lifts the filter, so near-infrared "
                 "reaches the red pixels and R/G must RISE: change = night / day, one per cycle; "
                 "correct = above x%.2f in every cycle, REVERSED = below x%.2f." % (1 + RG_STEP, 1 / (1 + RG_STEP)))
    return "\n".join(lines)

if __name__ == "__main__":
    import argparse
    from podcontrol.stations import get_pod
    ap = argparse.ArgumentParser(description="IR-cut polarity check (measures; changes nothing unless --apply).")
    ap.add_argument("--only", help="comma-separated camera IPs (default: the whole pod)")
    ap.add_argument("--cycles", type=int, default=CYCLES, help="day/night cycles (default %d)" % CYCLES)
    ap.add_argument("--apply", action="store_true",
                    help="flip ircut_swap on cameras found REVERSED, then check them again")
    args = ap.parse_args()
    stations = get_pod()
    if args.only:
        keep = set(args.only.split(","))
        stations = [s for s in stations if s.ip in keep]
    res = check_pod(stations, args.cycles,
                    on_done=lambda c: print("  %s %s: %s" % (c.get("id"), c.get("ip", ""), c.get("verdict")), flush=True))
    print(format_table(res))
    if args.apply:
        notes = apply_swaps(stations, res)
        for n in notes:
            print("  " + n)
        fixed = [s for s in stations if (res.get(s.id) or {}).get("verdict") == "REVERSED"]
        if fixed:
            print("re-checking the flipped cameras:")
            print(format_table(check_pod(fixed, args.cycles)))
