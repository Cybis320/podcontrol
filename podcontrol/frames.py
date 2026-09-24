"""Frame source for previews + metering, designed to NOT disturb RMS.

Golden rule: never pull an RTSP stream while RMS is capturing. A second stream
doubles bandwidth (the Goke femac's ~21 Mbps ceiling -> drops on BOTH streams)
and a second RTSP session can reset RMS's own session. So:

  * RMS ACTIVE  (fresh FramesFiles appearing)  -> read the saved JPGs only.
  * RMS IDLE    (no recent frames)             -> one-shot ffmpeg grab is safe.

We decide per camera from frame recency, so the app self-adjusts as RMS starts
or stops. A hard 'allow_grab=False' forces read-only no matter what.
"""
import os, re, glob, time, math, calendar, subprocess, cv2
import numpy as np

SCRATCH = "/tmp/podcontrol"
os.makedirs(SCRATCH, exist_ok=True)

# If a FramesFiles image appeared within this window, treat RMS as ACTIVE and
# never grab (even if the newest frame is a few seconds old).
RMS_ACTIVE_WINDOW = 60.0
# RMS's raw-frame saver writes frames in blocks of 10 (one every ~5 s, flushed
# together), so the newest file is anywhere from 0 to ~50 s old during normal
# capture. Only call a frame 'stale' beyond that block period.
RMS_FRESH_WINDOW = 60.0

# RMS saves frames as JPG (legacy) or PNG (raw-frame-save branch, e.g.
# US05A1_20260914_103750_010_n.png). Accept both -- missing the PNGs made the
# frame source think RMS was idle and fall back to RTSP grabs on a capturing pod.
FRAME_EXTS = ("*.png", "*.jpg", "*.jpeg")

# Second guard, independent of saved frames: an RMS StartCapture process that
# was started with a config pointing at this camera means RMS is ACTIVE even if
# frame saving is off or lagging.
_PROC_CACHE = {"t": 0.0, "cmds": ""}


def _capture_cmdlines(max_age=5.0):
    """Concatenated cmdlines of running RMS StartCapture processes (cached)."""
    now = time.time()
    if now - _PROC_CACHE["t"] < max_age:
        return _PROC_CACHE["cmds"]
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ")
            except Exception:
                continue
            if b"StartCapture" in cmd:
                out.append(cmd.decode("latin1"))
    except Exception:
        pass
    _PROC_CACHE.update(t=now, cmds="\n".join(out))
    return _PROC_CACHE["cmds"]


def rms_process_active(station):
    """True if a running StartCapture references this station's config/data_dir
    (by station id or data_dir path in its command line)."""
    cmds = _capture_cmdlines()
    if not cmds:
        return False
    if station.id and station.id in cmds:
        return True
    if station.data_dir and station.data_dir.rstrip("/") in cmds:
        return True
    return False


# One scan of the newest two hour-directories per station, cached for a few
# seconds and shared by every consumer (latest frame, age, complete sets).
# The previous code ran four recursive globs per station per cycle over
# ~20k files (~1.8 s of CPU every 5 s).
_DIR_CACHE = {}          # station.id -> (t_scan, [(capture_epoch, path)] newest first)
DIR_SCAN_TTL = 4.0
_EXT_TUPLE = tuple(e.lstrip("*") for e in FRAME_EXTS)


def _newest_subdirs(d, n):
    try:
        subs = sorted(e.name for e in os.scandir(d) if e.is_dir())
    except OSError:
        return []
    return [os.path.join(d, x) for x in subs[-n:]]


def scan_station(station, ttl=DIR_SCAN_TTL):
    """[(capture_epoch, path)] newest first for this station's most recent
    frames (RMS layout FramesFiles/YYYY/YYYYMMDD-DDD/YYYYMMDD-DDD_HH/), from a
    cheap scandir of the newest two hour directories; cached ttl seconds."""
    now = time.time()
    c = _DIR_CACHE.get(station.id)
    if c and now - c[0] < ttl:
        return c[1]
    out = []
    root = station.frames_dir
    if root and os.path.isdir(root):
        hourdirs = []
        for y in _newest_subdirs(root, 1):
            for d in _newest_subdirs(y, 2):
                hourdirs += _newest_subdirs(d, 2)
        hourdirs = hourdirs[-2:]
        if not hourdirs:                    # legacy / flat layout
            hourdirs = [root]
        prefix = station.id + "_"
        for hd in hourdirs:
            try:
                with os.scandir(hd) as it:
                    for e in it:
                        n = e.name
                        if n.startswith(prefix) and n.endswith(_EXT_TUPLE):
                            t = frame_capture_time(n)
                            if t is None:
                                try:
                                    t = e.stat().st_mtime - _BLOCK_SPAN_S
                                except OSError:
                                    continue
                            out.append((t, e.path))
            except OSError:
                continue
    out.sort(reverse=True)
    _DIR_CACHE[station.id] = (now, out)
    return out


_IMG_CACHE = {}          # path -> decoded BGR (frames are immutable once written)
IMG_CACHE_MAX = 8


def imread_cached(path):
    img = _IMG_CACHE.get(path)
    if img is not None:
        return img
    img = _imread_ok(path)
    if img is not None:
        if len(_IMG_CACHE) >= IMG_CACHE_MAX:
            _IMG_CACHE.pop(next(iter(_IMG_CACHE)))
        _IMG_CACHE[path] = img
    return img


def _newest(paths, n=1):
    """Newest path (n=1) or the n newest, newest first."""
    ts = []
    for f in paths:
        try:
            ts.append((os.path.getmtime(f), f))
        except OSError:
            continue
    ts.sort(reverse=True)
    if n == 1:
        return ts[0][1] if ts else None
    return [f for _, f in ts[:n]]


def _imread_ok(path):
    """cv2.imread that treats a half-written file (RMS is still flushing the
    block) as missing instead of raising in cvtColor later."""
    try:
        img = cv2.imread(path)
    except Exception:
        return None
    return img if img is not None and img.size else None


def latest_rms_path(station):
    """Newest saved RMS frame for this station (PNG or JPG), or None."""
    sc = scan_station(station)
    return sc[0][1] if sc else None


def latest_rms_frame(station):
    """(bgr, path) of the newest READABLE saved frame; falls back to the
    previous file if the newest is still being written."""
    now = time.time()
    # RMS flushes a 10-frame block at once: right after a flush the newest
    # files may still be being written, so look back past the whole block
    for t, p in scan_station(station)[:14]:
        try:
            if now - os.path.getmtime(p) < 1.5:     # RMS may still be writing it
                continue
        except OSError:
            continue
        img = imread_cached(p)
        if img is not None:
            return img, p
    return None, None


def rms_frame_age(station):
    """Seconds since the newest saved frame was captured, or None if none."""
    sc = scan_station(station)
    return (time.time() - sc[0][0]) if sc else None


def grab_rtsp(ip, timeout_s=12):
    """One-shot single-frame grab from the main stream via ffmpeg. BGR or None.
    Only call when RMS is idle -- this opens a transient RTSP session."""
    url = "rtsp://%s:554/user=admin&password=&channel=1&stream=0.sdp" % ip
    out = os.path.join(SCRATCH, "grab_%s.jpg" % ip.replace(".", "_"))
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
             "-i", url, "-frames:v", "1", "-q:v", "3", "-y", out],
            timeout=timeout_s, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(out):
            return cv2.imread(out)
    except Exception:
        pass
    return None


def rms_active(station):
    """RMS is capturing this camera: fresh saved frames OR a live StartCapture."""
    age = rms_frame_age(station)
    if age is not None and age < RMS_ACTIVE_WINDOW:
        return True
    return rms_process_active(station)


def frame_for(station, allow_grab=True, with_time=False, with_path=False):
    """(bgr_or_None, source) where source in {'rms','grab','stale','none'};
    with_time=True appends the frame's CAPTURE epoch (None if unknown);
    with_path=True (implies with_time) appends the saved file's path or None.

    'stale' = RMS is active but its latest frame is older than we'd like (or
    it saves no frames at all); we return what we have and DO NOT grab
    (protecting RMS). 'none' = nothing to show without grabbing.
    """
    age = rms_frame_age(station)
    if rms_active(station):
        img, p = latest_rms_frame(station)
        src = "rms" if (age is not None and age < RMS_FRESH_WINDOW) else "stale"
        t = None
        if p:
            t = frame_capture_time(p)
            if t is None:
                t = os.path.getmtime(p) - _BLOCK_SPAN_S        # pessimistic
        if with_path:
            return img, src, t, p
        return (img, src, t) if with_time else (img, src)
    if allow_grab:
        img = grab_rtsp(station.ip)
        if with_path:
            return img, "grab", time.time(), None
        return (img, "grab", time.time()) if with_time else (img, "grab")
    if with_path:
        return None, "none", None, None
    return (None, "none", None) if with_time else (None, "none")


# RMS frame names carry the UTC capture time: <id>_YYYYMMDD_HHMMSS_mmm_<d|n>.png
_TS_RE = re.compile(r"_(\d{8})_(\d{6})_(\d{3})_")
# a saved block spans ~50 s before its flush; used only when the name won't parse
_BLOCK_SPAN_S = 55.0


def frame_capture_time(path):
    """Epoch (UTC) when the saved frame was captured, from its filename; None
    if the name does not carry a timestamp."""
    m = _TS_RE.search(os.path.basename(path or ""))
    if not m:
        return None
    t = time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return calendar.timegm(t) + int(m.group(3)) / 1000.0


def fresh_frame(station, after, allow_grab=True, settle_s=2.5, max_wait=120.0, poll_s=2.0):
    """A frame CAPTURED at least `settle_s` after epoch `after` (e.g. after a
    WB/exposure change), so measurements reflect the new setting.

    RMS active -> wait for RMS to save one (its block cadence is ~50 s, so this
    can take up to a minute); never grabs. RMS idle -> one-shot grab after
    settle_s. Returns (bgr_or_None, source, captured_epoch_or_None); source is
    'timeout' if no suitable frame appeared within max_wait."""
    deadline = time.time() + max_wait
    while True:
        if rms_active(station):
            img, p = latest_rms_frame(station)
            if p:
                ct = frame_capture_time(p)
                if ct is None:
                    ct = os.path.getmtime(p) - _BLOCK_SPAN_S     # pessimistic
                if ct >= after + settle_s:
                    return img, "rms", ct
        elif allow_grab:
            time.sleep(settle_s)
            return grab_rtsp(station.ip), "grab", time.time()
        else:
            return None, "none", None
        if time.time() > deadline:
            return None, "timeout", None
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# RMS mask: <station dir>/mask.bmp, 0 = excluded, >0 = kept (same convention as
# RMS.Routines.MaskImage). Metering skips excluded pixels so a lamp or roof
# edge inside the mask cannot drive the shared AE or the WB calibration.
_MASK_CACHE = {}


def load_mask(station):
    """Boolean array (True = pixel counts) from the station's mask file, or
    None when the station has no mask. Cached; reloads if the file changes."""
    path = getattr(station, "mask_path", "") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _MASK_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    m = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if m is None:
        return None
    if m.ndim == 3:
        m = m[:, :, 0]
    keep = m > 0
    _MASK_CACHE[path] = (mt, keep)
    return keep


def static_mask_for(station, img):
    """The station's RMS mask sized to img (True = keep), or None."""
    keep = load_mask(station)
    if keep is None or img is None:
        return None
    h, w = img.shape[:2]
    if keep.shape != (h, w):
        keep = cv2.resize(keep.astype(np.uint8), (w, h),
                          interpolation=cv2.INTER_NEAREST).astype(bool)
    return keep


# Sun exclusion radius (deg) around the sun; the app's spinbox sets it.
SUN_RADIUS_DEG = [25.0]


def set_sun_radius(deg):
    SUN_RADIUS_DEG[0] = max(0.0, float(deg))


# Moon exclusion radius (deg); 0 = off. Same geometry as the sun zone.
MOON_RADIUS_DEG = [10.0]


def set_moon_radius(deg):
    MOON_RADIUS_DEG[0] = max(0.0, float(deg))


# Flare ghost radius (deg) for the lens-flare model (ghost discs on the
# sun-centre axis + a narrow corridor); 0 = off.
FLARE_HALF_WIDTH_DEG = [6.0]


def set_flare_width(deg):
    FLARE_HALF_WIDTH_DEG[0] = max(0.0, float(deg))


def mask_for(station, img, t=None, layers=False):
    """Combined measurement mask for img (True = pixel counts), or None when
    nothing is excluded: the station's static RMS mask AND the sun exclusion
    zone (platepar + ephemeris, radius SUN_RADIUS_DEG) at time t (now if None).
    With layers=True returns (keep, {"static": excluded_bool_or_None,
    "sun": excluded_bool_or_None, "sun_info": dict_or_None})."""
    if img is None:
        return (None, None) if layers else None
    keep = static_mask_for(station, img)
    static_excl = None if keep is None else ~keep
    sun_excl, sun_info = None, None
    if SUN_RADIUS_DEG[0] > 0 or MOON_RADIUS_DEG[0] > 0:
        from podcontrol import sunmask
        sun_excl, sun_info = sunmask.exclusion(station, t, SUN_RADIUS_DEG[0], img.shape[:2],
                                               FLARE_HALF_WIDTH_DEG[0], MOON_RADIUS_DEG[0])
    if sun_excl is not None:
        keep = ~sun_excl if keep is None else (keep & ~sun_excl)
    if layers:
        si = sun_info or {}
        return keep, {"static": static_excl, "sun": si.get("sun_map"),
                      "flare": si.get("flare_map"), "moon": si.get("moon_map"),
                      "sun_info": sun_info, "moon_info": si.get("moon")}
    return keep


# ---------------------------------------------------------------------------
# Complete frame SETS: RMS saves every camera on the same aligned 5 s slots
# (frame_save_aligned_interval), so the frames sharing a slot were captured
# together (sub-second offsets of ~10 ms). Metering the pod on the newest
# COMPLETE slot gives one coherent measurement with one capture time, instead
# of six frames of different ages (each station flushes its 10-frame block at
# a different moment).
SET_SLOT_S = 5.0
SET_MAX_AGE_S = 180.0
_STATS_CACHE = {}          # path -> luma stats (frames are immutable once written)


def recent_rms_frames(station, max_age=SET_MAX_AGE_S):
    """[(capture_epoch, path)] newest first, within max_age, for this station."""
    now = time.time()
    return [(t, p) for t, p in scan_station(station) if now - t <= max_age]


def newest_complete_set(stations, slot_s=SET_SLOT_S, max_age=SET_MAX_AGE_S):
    """(slot_epoch, {station_id: path}) for the newest slot where EVERY active
    station (one with any frame within max_age) has a frame; (None, {}) if
    no such slot. Stations without saved frames are not part of sets."""
    per = {}
    for st in stations:
        fr = recent_rms_frames(st, max_age)
        if fr:
            per[st.id] = {round(t / slot_s) * slot_s: p for t, p in fr}
    if not per:
        return None, {}
    common = set.intersection(*(set(d.keys()) for d in per.values()))
    if not common:
        return None, {}
    slot = max(common)
    return slot, {sid: per[sid][slot] for sid in per}


def stats_for_path(station, path, t, wb_scale=1.0):
    """luma_stats of a saved frame (masked, sun at t), cached by path."""
    key = (path, round(F_SUN_RADIUS(), 2), round(FLARE_HALF_WIDTH_DEG[0], 2), CLIP_MIN_BLOB_PX[0],
           round(MOON_RADIUS_DEG[0], 2), round(float(wb_scale), 3))
    hit = _STATS_CACHE.get(key)
    if hit is not None:
        return hit
    img = imread_cached(path)
    st = None
    if img is not None:
        keep, lay = mask_for(station, img, t, layers=True)
        st = luma_stats(img, keep, wb_scale=wb_scale)
        if st is not None:
            si = (lay or {}).get("sun_info") or {}
            st["sun_in_fov"] = bool(si.get("in_fov"))
    if len(_STATS_CACHE) > 400:
        _STATS_CACHE.clear()
    _STATS_CACHE[key] = st
    return st


def F_SUN_RADIUS():
    return SUN_RADIUS_DEG[0]


def meter_set(stations, allow_grab=False, wb_scale_fn=None):
    """Pod metering on the newest complete frame set: ({station_id: stats+t},
    slot_epoch). Stations that save no frames (RMS idle) are metered from a
    one-shot grab (t = now) only if allow_grab; otherwise skipped.
    wb_scale_fn(slot_epoch) -> the WB attenuation in effect when the set was
    captured (shared AE's WB rung), for channel-aware clipping."""
    slot, paths = newest_complete_set(stations)
    ws = 1.0
    if slot and wb_scale_fn:
        try:
            ws = float(wb_scale_fn(slot))
        except Exception:
            ws = 1.0
    out = {}
    for st in stations:
        if st.id in paths:
            s = stats_for_path(st, paths[st.id], slot, ws)
            if s is not None:
                out[st.id] = dict(s, t=slot, path=paths[st.id])
        elif allow_grab and not rms_active(st):
            img = grab_rtsp(st.ip)
            keep, lay = mask_for(st, img, layers=True)
            s = luma_stats(img, keep)
            if s is not None:
                si = (lay or {}).get("sun_info") or {}
                out[st.id] = dict(s, t=time.time(), sun_in_fov=bool(si.get("in_fov")))
    return out, slot


_HL_CACHE = {}


def highlight_maps_cached(path, bgr, keep, peak_value):
    """highlight_maps for a saved frame, cached per (path, mask params)."""
    key = (path, round(F_SUN_RADIUS(), 2), round(FLARE_HALF_WIDTH_DEG[0], 2), round(MOON_RADIUS_DEG[0], 2))
    hit = _HL_CACHE.get(key)
    if hit is not None:
        return hit
    r = highlight_maps(bgr, keep, peak_value)
    while len(_HL_CACHE) >= 16:          # tiles + sky-set frames; evict the oldest, never all
        _HL_CACHE.pop(next(iter(_HL_CACHE)))
    _HL_CACHE[key] = r
    return r


def highlight_maps(bgr, keep, peak_value=None, clip_level=250):
    """(clipped, hot): full-res bool maps of the UNMASKED pixels that are
    clipped (>= clip_level) and that sit at/above the 99.9th-percentile peak
    (the pixels the highlight-priority AE reacts to)."""
    if bgr is None:
        return None, None
    y = bgr.max(axis=2) if bgr.ndim == 3 else bgr          # per-pixel max channel, like luma_stats
    k = np.ones(y.shape, bool) if keep is None else keep
    clipped = (y >= clip_level) & k
    if peak_value is None:
        peak_value = float(np.percentile(y[k], 99.9)) if k.any() else 255.0
    hot = (y >= peak_value) & k & ~clipped
    return clipped, hot


# Raw (green) saturation: a clipped highlight is a PLATEAU -- many pixels
# sharing one green level near the top of the histogram (255 with the WB
# unattenuated, ~255*sqrt(scale) under the WB rung). It is found as a spike
# (g_plateau), not predicted from the scale: predicting it missed by ~10
# levels on 2026-09-17 when the AE's assumed scale (0.568) and the cameras'
# actual one (0.527) differed, and the magenta guard went blind. The model
# only bounds the search window; RAW_SAT_G_MARGIN is the fallback when no
# plateau stands out.
RAW_SAT_G_MARGIN = 8.0
PLATEAU_WINDOW_BELOW = 25   # search this far below the predicted plateau
PLATEAU_SPIKE = 5.0         # the plateau level must hold > this x the levels 4-10 below it


def g_plateau(G, wb_scale=1.0):
    """Green level of the raw-saturation plateau in this frame, or None.
    Looks in [255*sqrt(scale) - PLATEAU_WINDOW_BELOW, top] where top is the
    99.99th percentile (hot pixels excluded), takes the most populated level,
    and accepts it only if it stands out as a spike above the histogram just
    below it and holds at least 0.01 % of the pixels."""
    h = np.bincount(G.ravel(), minlength=256)
    n = G.size
    if n == 0:
        return None
    cum = np.cumsum(h)
    top = int(np.searchsorted(cum, 0.9999 * n))
    lo = max(1, int(255.0 * math.sqrt(min(1.0, max(1e-3, float(wb_scale))))) - PLATEAU_WINDOW_BELOW, top - 12)
    if top <= lo:
        return None
    p = lo + int(np.argmax(h[lo:top + 1]))
    below = float(h[max(0, p - 10):max(1, p - 4)].mean()) + 1.0
    if h[p] > PLATEAU_SPIKE * below and h[p:top + 1].sum() >= 0.0001 * n:
        return p
    return None

# Point-source tolerance for the CLIP metric: clipped blobs smaller than this
# many pixels (a lamp, a planet, headlights) are not counted as clipping --
# they clip at any night-worthy exposure and dimming the whole pod for them
# is pointless. 0 = every clipped pixel counts. ~470 px = a 1-deg lamp on
# US05A1 at the night line (2026-09-13).
CLIP_MIN_BLOB_PX = [0]


def set_clip_min_blob(px):
    CLIP_MIN_BLOB_PX[0] = max(0, int(px))


def luma_stats(bgr, mask=None, min_blob_px=None, wb_scale=1.0):
    """Metering from a frame: mean luma (0-255), clipped-pixel fraction (0-1)
    and the 99.9th-percentile peak -- over the UNMASKED pixels only when a mask
    (True = count) of the same size is given. The universal exposure signal:
    works on IMX291 (no daemon AveLum) and Goke alike. With a point-source
    tolerance (min_blob_px, default CLIP_MIN_BLOB_PX) clipped blobs smaller
    than that are excluded from the clip fraction ("clip_raw" keeps all)."""
    if bgr is None:
        return None
    y = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    # clipping and the headroom peak are judged on the per-pixel MAXIMUM
    # channel: a pixel whose red or blue is at 255 is clipped data even when
    # luma (green-weighted) reads 220. The mean stays luma.
    mx = bgr.max(axis=2) if bgr.ndim == 3 else bgr
    masked = 0.0
    keep = None
    if mask is not None and mask.shape == y.shape:
        masked = float(1.0 - mask.mean())
        keep = mask
    mb = CLIP_MIN_BLOB_PX[0] if min_blob_px is None else int(min_blob_px)
    clipped = (mx >= 250) if keep is None else ((mx >= 250) & keep)
    n_keep = int(keep.sum()) if keep is not None else y.size
    if n_keep == 0:
        return None
    clip_raw = float(clipped.sum()) / n_keep
    clip = clip_raw
    if mb > 0 and clipped.any():
        n, lab, stats, _ = cv2.connectedComponentsWithStats(clipped.astype(np.uint8), 8)
        big = sum(int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= mb)
        clip = float(big) / n_keep
    v = y if keep is None else y[keep]
    vm = mx if keep is None else mx[keep]
    out = {"mean": float(v.mean()), "clip": clip, "clip_raw": clip_raw,
           "peak": float(np.percentile(vm, 99.9)), "peak_luma": float(np.percentile(v, 99.9)),
           "masked": masked, "raw_sat": 0.0, "rb_only": 0.0}
    if bgr.ndim == 3:
        # Which stage clipped? Green carries WB gain ~1.0, so green at its
        # plateau means the SENSOR saturated (unrecoverable downstream); red
        # or blue at 255 while green is below means the WB gain (1.8-1.9x,
        # applied in 12-bit before demosaic) clipped it -- recoverable by
        # attenuating the WB gains. With a WB attenuation wb_scale < 1 the
        # green plateau of a raw-saturated pixel sits at 255*sqrt(wb_scale).
        G = bgr[:, :, 1]; R = bgr[:, :, 2]; B = bgr[:, :, 0]
        plateau = g_plateau(G, wb_scale)
        if plateau is not None:
            g_level = plateau - 3                     # the plateau's own spread
        else:
            g_level = max(60.0, 255.0 * math.sqrt(min(1.0, max(1e-3, float(wb_scale)))) - RAW_SAT_G_MARGIN)
        gs_all = G >= g_level
        out["g_plateau"] = plateau
        # raw saturation ANYWHERE in the frame, mask or not: an attenuated WB
        # turns every raw-saturated zone magenta, and the sun halo -- the
        # raw-saturated zone par excellence -- sits inside the sun-zone mask
        # (2026-09-16: the guard only looked at unmasked pixels, saw none, and
        # the rung ran to its bottom with the halo violet). Counted only when
        # a plateau stands out; without one the frame has no clipped green.
        out["raw_sat_all"] = float(gs_all.mean()) if plateau is not None else 0.0
        if keep is not None:
            G, R, B, gs = G[keep], R[keep], B[keep], gs_all[keep]
        else:
            gs = gs_all
        out["raw_sat"] = float(gs.mean())
        out["rb_only"] = float((((R >= 250) | (B >= 250)) & ~gs).mean())
    return out


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    import sys
    allow = "--grab" in sys.argv          # default: never open RTSP from the CLI
    pod = get_pod()
    for s in pod:
        img, src = frame_for(s, allow_grab=allow)
        st = luma_stats(img, mask_for(s, img))
        print("%-8s %-15s %-6s %s  luma=%s" % (
            s.id, s.ip, src, None if img is None else img.shape,
            None if st is None else "mean=%.0f clip=%.4f peak=%.0f masked=%.0f%%" % (
                st["mean"], st["clip"], st["peak"], 100 * st["masked"])))
    met, slot = meter_set(pod, allow_grab=allow)
    if slot:
        print("newest complete set: slot %s (%.0fs old), %d/%d cameras" % (
            time.strftime("%H:%M:%S", time.gmtime(slot)), time.time() - slot, len(met), len(pod)))
