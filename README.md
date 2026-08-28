# podcontrol

Control a **pod of RMS meteor cameras as one**. Live preview + telemetry for
every camera, coordinated exposure/gain (the shared-AE engine), and a white-balance
"cloud-gray" calibrator. Runs **parallel to RMS** and works on both camera
platforms — **Hi3516CV300 / IMX291** and **Goke GK7205V200 / IMX307**.

> Status: **Phase 1** — preview tiles + unified telemetry + Auto/Manual control.
> Phase 2 (shared-AE engine) and Phase 3 (WB cloud-gray) are on the roadmap below.

## How it works

Each camera runs an ISP control daemon on TCP **:9600**. `podcontrol` is a
coordinator on top of all of them:

- **Telemetry & control** come from the daemon (`query`/`manual`/`auto` on both
  platforms; `ae`/`wb`/`venc_qp`/… additionally on IMX291). The controller
  detects the platform per camera and normalizes telemetry into one schema.
- **Previews & metering** come from frames — and never disturb RMS:
  - **RMS active** (fresh `FramesFiles` on disk) → read the saved JPGs only.
  - **RMS idle** → a one-shot `ffmpeg` grab is safe.
  This is auto-detected per camera from frame recency; `--no-grab` forces
  read-only. Metering (mean luma + clip fraction) is computed from the frame,
  so it works even on IMX291 (whose daemon has no AveLum).

## Install

```bash
git clone <this repo> && cd podcontrol
pip install .            # or: pip install -e .   for development
```

System packages (not pip-installable):

```bash
sudo apt install python3-tk ffmpeg
```

## Run

```bash
podcontrol                              # default pod: 192.168.42.101 .. .106
podcontrol --cameras 192.168.42.101-106
podcontrol --pod pod.json               # explicit camera list (see pod.example.json)
podcontrol --stations-dir ~/source/Stations   # discover from RMS station configs
podcontrol --no-grab                    # never pull RTSP (RMS-safe, read saved frames only)
```

### Defining the pod

Resolution order: `--cameras` → `--pod`/`$PODCONTROL_POD` → `./pod.json` →
`~/.config/podcontrol/pod.json` → `--stations-dir`/`$PODCONTROL_STATIONS_DIR`
→ built-in default (`192.168.42.101–.106`). Copy `pod.example.json` to
`pod.json` and edit for your site.

## Platform parity (audit)

The two firmware families expose different daemon vocabularies. The app targets
the common set and degrades gracefully; closing these gaps is firmware work.

| Capability          | IMX291 `hisp_ctl` | Goke `isp_ctl` |
|---------------------|-------------------|----------------|
| exposure/gain       | ✅ `manual`/`auto`/`gain`/`exp` | ✅ `manual`/`auto` |
| brightness (AveLum) | ✗ (metered from frame) | ✅ in `query` |
| chip temperature    | ✗ | ✅ in `query` |
| white balance       | ✅ `wb` | ✗ (needs adding) |
| QP / chroma QP      | ✅ `venc_qp`/`venc_cqp` | ✗ (needs adding) |
| pipeline blocks     | ✅ `drc`/`nr`/`sharpen`/`dpc` | ✗ |

## Roadmap

- **Phase 2 — shared-AE engine.** Slow loop: meter all cameras, and when the
  brightest clips, step exposure/gain down on **all** of them so the pod acts
  as one photometric instrument.
- **Phase 3 — WB cloud-gray calibrator.** Box a cloud in one FOV → compute the
  gains that make it neutral → push the same WB to all cameras (clouds gray,
  sky keeps its chroma).
- **Parity work.** Add `wb`/`venc_qp`/metering to the Goke daemon so both
  platforms are fully controllable.

## Layout

```
podcontrol/
  stations.py   pod definition (IPs, data dirs) from config/CLI/RMS Stations
  podctl.py     PodController: platform-aware :9600 client + unified telemetry
  frames.py     RMS-safe frame source + luma/clip metering
  app.py        Tkinter UI (preview tiles + telemetry + controls)
```
