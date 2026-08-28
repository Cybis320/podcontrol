"""Frame source for previews + metering, designed to NOT disturb RMS.

Golden rule: never pull an RTSP stream while RMS is capturing. A second stream
doubles bandwidth (the Goke femac's ~21 Mbps ceiling -> drops on BOTH streams)
and a second RTSP session can reset RMS's own session. So:

  * RMS ACTIVE  (fresh FramesFiles appearing)  -> read the saved JPGs only.
  * RMS IDLE    (no recent frames)             -> one-shot ffmpeg grab is safe.

We decide per camera from frame recency, so the app self-adjusts as RMS starts
or stops. A hard 'allow_grab=False' forces read-only no matter what.
"""
import os, glob, time, subprocess, cv2

SCRATCH = "/tmp/podcontrol"
os.makedirs(SCRATCH, exist_ok=True)

# If a FramesFiles image appeared within this window, treat RMS as ACTIVE and
# never grab (even if the newest frame is a few seconds old).
RMS_ACTIVE_WINDOW = 60.0


def latest_rms_path(station):
    if not station.data_dir:
        return None
    files = glob.glob(os.path.join(station.frames_dir, "**", station.id + "_*.jp*g"),
                      recursive=True)
    return max(files, key=os.path.getmtime) if files else None


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


def frame_for(station, allow_grab=True):
    """(bgr_or_None, source) where source in {'rms','grab','stale','none'}.

    'stale' = RMS is active but its latest frame is older than we'd like; we
    still return it and DO NOT grab (protecting RMS). 'none' = read-only mode
    with nothing to show.
    """
    age = rms_frame_age(station)
    if age is not None and age < RMS_ACTIVE_WINDOW:
        img = cv2.imread(latest_rms_path(station))
        return img, ("rms" if age < 10 else "stale")
    if allow_grab:
        return grab_rtsp(station.ip), "grab"
    return None, "none"


def luma_stats(bgr):
    """Metering from a frame: (mean luma 0-255, clipped-pixel fraction 0-1).
    The universal exposure signal -- works on IMX291 (no daemon AveLum) and Goke."""
    if bgr is None:
        return None
    import numpy as np
    y = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return {"mean": float(y.mean()), "clip": float((y >= 250).mean())}


if __name__ == "__main__":
    from podcontrol.stations import get_pod
    for s in get_pod():
        img, src = frame_for(s)
        st = luma_stats(img)
        print("%-8s %-15s %-6s %s  luma=%s" % (
            s.id, s.ip, src, None if img is None else img.shape,
            None if st is None else "mean=%.0f clip=%.3f" % (st["mean"], st["clip"])))
