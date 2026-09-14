"""Frame source for previews + metering, designed to NOT disturb RMS.

Golden rule: never pull an RTSP stream while RMS is capturing. A second stream
doubles bandwidth (the Goke femac's ~21 Mbps ceiling -> drops on BOTH streams)
and a second RTSP session can reset RMS's own session. So:

  * RMS ACTIVE  (fresh FramesFiles appearing)  -> read the saved JPGs only.
  * RMS IDLE    (no recent frames)             -> one-shot ffmpeg grab is safe.

We decide per camera from frame recency, so the app self-adjusts as RMS starts
or stops. A hard 'allow_grab=False' forces read-only no matter what.
"""
import os, re, glob, time, calendar, subprocess, cv2
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
    """Newest saved RMS frame for this station (PNG or JPG), or None.
    Only the two most recent day directories are scanned to keep this cheap."""
    if not station.data_dir or not os.path.isdir(station.frames_dir):
        return None
    root = station.frames_dir
    # RMS layout: FramesFiles/YYYY/YYYYMMDD-DDD/YYYYMMDD-DDD_HH/<id>_*.png
    days = sorted(glob.glob(os.path.join(root, "[0-9]" * 4, "*")))[-2:]
    cands = []
    for d in days:
        for ext in FRAME_EXTS:
            cands += glob.glob(os.path.join(d, "**", station.id + "_" + ext), recursive=True)
    if not cands:                      # legacy/flat layouts
        for ext in FRAME_EXTS:
            cands += glob.glob(os.path.join(root, "**", station.id + "_" + ext), recursive=True)
    return _newest(cands)


def latest_rms_frame(station):
    """(bgr, path) of the newest READABLE saved frame; falls back to the
    previous file if the newest is still being written."""
    if not station.data_dir or not os.path.isdir(station.frames_dir):
        return None, None
    root = station.frames_dir
    days = sorted(glob.glob(os.path.join(root, "[0-9]" * 4, "*")))[-2:]
    cands = []
    for d in days:
        for ext in FRAME_EXTS:
            cands += glob.glob(os.path.join(d, "**", station.id + "_" + ext), recursive=True)
    for p in _newest(cands, n=3) if cands else []:
        img = _imread_ok(p)
        if img is not None:
            return img, p
    return None, None


def rms_frame_age(station):
    """Seconds since the newest saved frame, or None if none exist."""
    p = latest_rms_path(station)
    return (time.time() - os.path.getmtime(p)) if p else None


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


def frame_for(station, allow_grab=True, with_time=False):
    """(bgr_or_None, source) where source in {'rms','grab','stale','none'};
    with_time=True appends the frame's CAPTURE epoch (None if unknown).

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
        return (img, src, t) if with_time else (img, src)
    if allow_grab:
        img = grab_rtsp(station.ip)
        return (img, "grab", time.time()) if with_time else (img, "grab")
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
SUN_RADIUS_DEG = [20.0]


def set_sun_radius(deg):
    SUN_RADIUS_DEG[0] = max(0.0, float(deg))


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
    if SUN_RADIUS_DEG[0] > 0:
        from podcontrol import sunmask
        sun_excl, sun_info = sunmask.exclusion(station, t, SUN_RADIUS_DEG[0], img.shape[:2])
    if sun_excl is not None:
        keep = ~sun_excl if keep is None else (keep & ~sun_excl)
    if layers:
        return keep, {"static": static_excl, "sun": sun_excl, "sun_info": sun_info}
    return keep


def luma_stats(bgr, mask=None):
    """Metering from a frame: mean luma (0-255), clipped-pixel fraction (0-1)
    and the 99.9th-percentile peak -- over the UNMASKED pixels only when a mask
    (True = count) of the same size is given. The universal exposure signal:
    works on IMX291 (no daemon AveLum) and Goke alike."""
    if bgr is None:
        return None
    y = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    masked = 0.0
    if mask is not None and mask.shape == y.shape:
        masked = float(1.0 - mask.mean())
        y = y[mask]
        if y.size == 0:
            return None
    return {"mean": float(y.mean()), "clip": float((y >= 250).mean()),
            "peak": float(np.percentile(y, 99.9)), "masked": masked}


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    import sys
    allow = "--grab" in sys.argv          # default: never open RTSP from the CLI
    for s in get_pod():
        img, src = frame_for(s, allow_grab=allow)
        st = luma_stats(img, mask_for(s, img))
        print("%-8s %-15s %-6s %s  luma=%s" % (
            s.id, s.ip, src, None if img is None else img.shape,
            None if st is None else "mean=%.0f clip=%.4f peak=%.0f masked=%.0f%%" % (
                st["mean"], st["clip"], st["peak"], 100 * st["masked"])))
