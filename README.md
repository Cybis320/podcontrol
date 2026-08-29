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

The two firmware families expose different `:9600` daemon vocabularies. See **[docs/PARITY.md](docs/PARITY.md)** for the full empirical matrix (exposure/gain, telemetry, WB, encoder, ISP blocks, timing) and the prioritized parity work.

TL;DR of the gaps that matter for the pod goals:
- **Goke needs arbitrary WB gains in the daemon** (`wb <R> <G> <B>`) — blocks the cloud-gray calibrator.
- **Goke needs the encoder in the daemon** (bitrate/GOP/QP/chroma-QP; today it's the separate `venc_ioctl`).
- **IMX291 has no daemon AveLum/ChipTemp** — the app meters from the frame instead (already handled).
- Control (`manual`/`auto`) and most ISP-block toggles exist on both; only the vocabulary differs.

## Shared AE + WB calibration (in the app)

- **Shared AE** (`Shared AE: ON`) — a slow loop meters every camera from the
  frame and drives ONE exposure+gain onto all of them; if the brightest clips,
  everyone backs off. Exposure moves in whole sensor lines with gain filling the
  fractional line (smooth, no hunting). Headless: `python -m podcontrol.sharedae`.
- **WB cloud-gray** (`Calibrate WB (cloud)`) — drag a box over a grey cloud in
  one camera, click the button; it iterates the WB gains until that region is
  neutral (~3 iters) and pushes the same WB to the whole pod. Clouds go grey,
  the sky keeps its chroma. Headless:
  `python -m podcontrol.wbcal --camera cam101 --box x0,y0,x1,y1`

## Roadmap

- ✅ Phase 1 (preview + telemetry), ✅ Phase 2 (shared AE), ✅ Phase 3 (WB cloud-gray).
- **Parity work.** The Goke daemon now has `wb`; still to fold in: the encoder
  (bitrate/GOP/QP) and optional metering. See [docs/PARITY.md](docs/PARITY.md).
- ⚠ The Goke `wb` addition must ship in a fresh Goke firmware build (current
  libs) — the old .201–.206 pod runs older libs (see PARITY.md).

## Layout

```
podcontrol/
  stations.py   pod definition (IPs, data dirs) from config/CLI/RMS Stations
  podctl.py     PodController: platform-aware :9600 client + unified telemetry
  frames.py     RMS-safe frame source + luma/clip metering
  app.py        Tkinter UI (preview tiles + telemetry + controls)
```
