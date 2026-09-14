# podcontrol

Control a **pod of RMS meteor cameras as one**. Live preview + telemetry for
every camera, coordinated exposure/gain (the shared-AE engine), and a white-balance
"cloud-gray" calibrator. Runs **parallel to RMS** and works on both camera
platforms — **Hi3516CV300 / IMX291** and **Goke GK7205V200 / IMX307**.

> Status: Phases 1–3 done (preview + telemetry, shared AE, WB cloud-gray).
> 2026-09-14: re-targeted at the production Goke/OpenIPC pod (.201–.206), whose
> daemon now speaks the same `wb` / `venc_qp` / `venc_cqp` / `venc_gop` vocabulary
> as the IMX291 one — see the roadmap below.

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

## Installing into the RMS venv

Install **editable** so the `podcontrol` command tracks the checkout, and with
`--no-deps` so pip never touches RMS's pinned numpy/OpenCV (an unconstrained
install once upgraded numpy to 2.x and broke RMS's compiled extensions):

```
~/vRMS/bin/pip install --no-deps -e ~/source/podcontrol
```

## Frame source (RMS-safe) — what changed 2026-09-14

- RMS now saves **PNG** frames in 10-frame blocks (`<id>_YYYYMMDD_HHMMSS_mmm_d.png`,
  flushed every ~50 s). The frame source reads PNG or JPG, and treats a frame as
  fresh for 60 s (the block period), so previews on a capturing pod are RMS's
  own frames, up to ~50 s old, at zero extra camera load.
- A second guard: a running `RMS.StartCapture` whose command line names the
  station means RMS is ACTIVE even if no saved frame is found — the app will
  never open a second RTSP session on such a camera.
- `frames.fresh_frame(station, after)` waits for a frame **captured after** a
  given time (from the filename's UTC stamp). The WB calibrator uses it so each
  iteration measures a frame taken after the WB push (up to ~1 min per iteration
  on a capturing pod; instant when RMS is idle and a grab is allowed).
- The pod is discovered from `~/source/Stations` (RMS `.config` → camera IP +
  `data_dir`) when present, so the production pod works out of the box.

## Roadmap

- ✅ Phase 1 (preview + telemetry), ✅ Phase 2 (shared AE), ✅ Phase 3 (WB cloud-gray).
- ✅ **Parity closed in firmware (2026-09).** Both daemons speak the same
  `wb` / `venc_qp` / `venc_cqp` / `venc_gop` / `persist` / `manual -a/-i/-e` /
  `auto --min/max-exptime` syntax (x256 WB gains, 256 = 1.0x); the Goke fleet
  runs it on OpenIPC. `podctl` polls the encoder state on both and exposes
  `venc_qp_all` / `venc_cqp_all` / `venc_gop_all`. See [docs/PARITY.md](docs/PARITY.md).
- Shared AE on the Goke pod: validated on the IMX291 bench only. On the Goke
  science config, night exposure/gain is already pinned identically by
  `camera_settings.json`; shared AE matters at twilight/day where the cameras'
  own AE diverges (e.g. AGain 1.0–4.6x across the pod at dawn). The secondary
  gain stage is the ISP digital gain (`-i`) on both platforms (sensor DGain
  stays 1x, a science invariant).
- Next: (a) run the GUI against the production pod and exercise shared AE at
  twilight on the test unit first; (b) WB cloud-gray on the Goke pod to refine
  the fixed day WB (460/256/490); (c) daemon-side clip metering (ISP histogram)
  so AE does not depend on ~50 s-old saved frames.

## Layout

```
podcontrol/
  stations.py   pod definition (IPs, data dirs) from config/CLI/RMS Stations
  podctl.py     PodController: platform-aware :9600 client + unified telemetry
  frames.py     RMS-safe frame source + luma/clip metering
  app.py        Tkinter UI (preview tiles + telemetry + controls)
```
