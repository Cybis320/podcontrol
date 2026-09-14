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

The RMS venv (`~/vRMS`) is created with `--system-site-packages` and uses the
system `python3-opencv` (4.6) and `python3-numpy` (1.26). Install podcontrol
**editable** so the `podcontrol` command tracks the checkout, and with
`--no-deps` so pip never adds an `opencv-python`/`numpy` of its own (a pip
opencv-python 5 drags in numpy 2, which breaks RMS's compiled extensions):

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

## RMS mask = metering exclusion zone

Each station's RMS mask (`~/source/Stations/<ID>/mask.bmp`, or the `mask`
entry in its `.config`; 0 = excluded, same convention as RMS) is applied to
every measurement podcontrol makes:

- shared-AE metering (mean luma, clip fraction, 99.9th-percentile peak) counts
  only unmasked pixels, so a street lamp or a lit roof edge inside the mask can
  never pull the whole pod's exposure down;
- the WB cloud-gray region mean ignores masked pixels inside the drawn box;
- preview tiles dim the masked area and show `maskNN%` in the telemetry line.

A station without a mask file is metered over the full frame.

## Sun exclusion zone

The sun (and its glare) is kept out of every measurement too. Each station's
RMS platepar (`platepar_cmn2010.cal`, or `platepar_name` in `.config`) gives
the alt/az of every pixel, computed once per station on an 8-px grid; at
measurement time the sun's alt/az (ephem, from the platepar's site) gives each
pixel's angular distance to the sun, and everything within the **sun radius**
is excluded — exactly like the static mask, and combined with it. It applies
whenever the circle can touch the sky (sun altitude above minus the radius),
so the glow around a just-set sun is excluded as well.

- GUI: the **overlay** checkbox tints the excluded zones on the tiles
  (red = RMS mask, orange = sun zone, small white circle = the sun) and the
  **sun r°** spinbox sets the radius live (0 disables it; default **25°**).
  The telemetry line shows `maskNN%` (static + sun) and `sun 23° IN FOV` when
  it is in the frame.
- The right radius depends on the lens, haze and exposure. Measure it on real
  frames — it prints the mean luma and clipped fraction per 2° annulus around
  the sun and suggests a radius:

  ```
  ~/vRMS/bin/python -m podcontrol.sunmask --measure US05B1
  ~/vRMS/bin/python -m podcontrol.sunmask --where        # sun in each camera now
  ```

  First measurement (US05B1, sun only 6° up, 2026-09-14): clipping reached
  12–14° from the sun; the default is **25°** (chosen after watching the
  live pod); re-measure with the sun high
  (B1 ~15:40 UTC, F1 ~19:20 UTC, D1 ~23:20 UTC at this site) and raise it if
  the halo is larger. Lens-flare ghosts elsewhere in the frame are not covered
  by the circle; the overlay and the clip% show whether anything leaks.

## Moon exclusion zone

Same machinery as the sun zone, from ephem's topocentric Moon: a disc of
**moon r°** (spinbox, default 10°, 0 = off) is excluded around the Moon while
the sun is below the horizon (a daytime Moon cannot clip). Tinted pale blue in
the overlay, with a "moon" marker; the telemetry line shows its altitude and
illuminated fraction. The radius is a placeholder until measured on frames
with the Moon in the field: `python -m podcontrol.sunmask --measure STATION
--body moon` prints the luma/clip profile per annulus on the newest `_n`
frames. No flare model for the Moon yet.

## Lens-flare model

Internal-reflection ghosts are images mirrored through the lens's
**principal point** on the sensor. That is the platepar's fitted radial
distortion centre, not the geometric image centre: on US05B1 it is 36 px
below the centre, and the ghost drifted off the image-centre line during the
afternoon exactly as that offset predicts (on the line when the sun was
straight below the centre, 1.7° off when it was upper-right). A radial
distortion keeps the sun-to-principal-point line straight, so ghosts sit on
it at fixed fractions *k* of the sun's distance from that point, with a
roughly constant angular size. Measured on US05B1 (2026-09-14, 14:10 to
15:57): one pale disc ~5° in radius at k = −0.57, 60–90 luma above the sky;
with the sun ~10° outside the field (US05F1) a smaller ghost at k ≈ −0.20.

The model excludes ghost **discs** at those *k* (radius **flare r°** = 6°,
spinbox; 0 disables the model; the far ghost only while the sun is within 5°
of the field, the near one out to 30°) plus a 3° half-width **corridor**
along the whole axis while the sun is inside the field. It is tinted violet
in the overlay. Other lenses: `python -m podcontrol.sunmask --measure
STATION` lists the bright blobs with their *k* and off-axis angle; add
`(k, scale, max_sep)` entries to `sunmask.FLARE_GHOSTS`.

## What is driving the exposure (GUI)

With Shared AE on, the tile of the camera whose need set the pod target gets
a coloured border and a `◀ DRIVING: clipping | headroom | at target` badge;
every tile shows its own `need ±x.xx stop (why)` so you can see who is
limiting and by how much. With the overlay on, **clipped** unmasked pixels
are tinted magenta on every tile (the highlight-priority rule reacts to
these), and on the driving camera the **peak** pixels (top 0.1 %, what the
headroom rule looks at) are tinted cyan. The window is resizable; tiles keep
16:9 and scale with it.

**Every unmasked pixel on every camera counts (default).** The sun camera
votes like any other; the sun zone radius is the operator's lever. Measured
2026-09-14 with the sun 27° up: at the minimum exposure B1 still clipped out
to 32° from the sun, so a zone of 34° or more is needed for it not to pin the
pod at the floor (that removes ~70% of B1's frame) — see `--measure`. The
**sun cam votes** checkbox, when cleared, makes a camera with the sun in its
field follow the pod exposure without voting (badge `☀ following (no
vote)`); if every camera sees the sun they all vote regardless.

`PODCONTROL_DRY=1 podcontrol --no-grab` runs the whole loop (metering,
targets, driver display) without ever sending an exposure command — for
demos and UI testing on a live pod.

## RMS ↔ podcontrol: who owns what

RMS owns colour and the night. At every −9° sun crossing it replays
camera_settings (`SwitchMode day|night`): day = `auto …` + daylight WB, colour;
night = the fixed science exposure line + `wb unity`, mono. The FramesFiles
frames carry `_d`/`_n` accordingly, and the colour→mono cut sits on that
boundary on purpose. podcontrol never touches WB, CCM, saturation or IR-cut;
it owns **exposure only, and only while the sun is above the switch**:

- **Day**: Shared AE slews the pod as described above.
- **Dusk**: the AE keeps driving through RMS's −9° switch (which is still
  twilight): RMS writes its night line there, podcontrol notices the changed
  values and re-pins within a cycle, so only the colour→mono cut happens at the
  boundary. The ladder's top rung is made byte-identical to RMS's night line
  (from camera_settings) and the pod **latches** there when the AE reaches it
  or when the sun passes `latch_deg` (−12°), whichever comes first. With
  `dusk_ramp_deg` = 0 (default) that latch is a step of whatever gap remains;
  set it to e.g. 3° to ramp the last degrees so the latch is seamless.
- **Night**: latched and silent. Nothing podcontrol sees (moon, lit clouds,
  headlights) can move the pod; RMS owns it.
- **Dawn**: unlatch once the sun is rising above −12° and a fresh set asks for
  less light; slew down. When RMS's day switch sends `auto`, podcontrol re-pins
  on the next cycle, so at most one frame shows a camera's own AE.

Switching Shared AE on at night latches immediately and sends nothing.
Still open: a lease in the camera daemon so a dead podcontrol can never
strand the pod (today a hard kill leaves it pinned until the next switch), and
daemon-side metering to drop the ~50 s frame latency.

## Point sources at twilight and night

Checked on real frames (night of 2026-09-12/13 at the night line, and the
dawn of 09-14 from −9° to sunrise): with no sun in the field, five of six
cameras have **no** clipped pixels at the night line and their 99.9th
percentile sits at 70–140, so the highlight rule lets the pod climb to the
night line and does not fight it. The exception is a single ~1° street lamp
at US05A1's bottom edge (472 clipped px at the night line), which alone would
hold the whole pod dark through dusk. The dawn horizon glow on the east camera
at −9° is a genuine extended highlight and drives correctly.

Two levers for point sources: the station mask (a fixed lamp belongs there),
and the **pt-src < N px** spinbox: clipped blobs smaller than N pixels are not
counted as clipping (default 0 = every clipped pixel counts). 600 px turns the
A1 lamp's verdict from "darker" into "headroom" without touching the mask; it
also stops headlights or a planet from dipping the pod. The peak metric is
unaffected (a lamp is far below 0.1% of the frame).

## Roadmap

- ✅ Phase 1 (preview + telemetry), ✅ Phase 2 (shared AE), ✅ Phase 3 (WB cloud-gray).
- ✅ **Parity closed in firmware (2026-09).** Both daemons speak the same
  `wb` / `venc_qp` / `venc_cqp` / `venc_gop` / `persist` / `manual -a/-i/-e` /
  `auto --min/max-exptime` syntax (x256 WB gains, 256 = 1.0x); the Goke fleet
  runs it on OpenIPC. `podctl` polls the encoder state on both and exposes
  `venc_qp_all` / `venc_cqp_all` / `venc_gop_all`. See [docs/PARITY.md](docs/PARITY.md).
- **Shared-AE loop discipline (2026-09-14, after railing the live pod to the
  30 µs floor and then chasing every 5 s):** RMS frames are up to ~50 s old
  and each station flushes its block at a different moment, so any loop that
  steps on "whatever is fresh" is driven by a different camera every cycle.
  RMS saves every camera on the same aligned 5 s slots, so each cycle the
  app meters the **newest complete set** (the newest slot for which every
  active camera has a frame; the six frames are within ~20 ms of each other),
  one coherent measurement with one capture time. The controller remembers
  its own applies and judges that set at the light index that was in effect
  when it was captured, giving an *absolute* target per camera (darkest need
  wins); the pod then **slews** toward the
  target by at most `slew` stops per 5 s cycle (default 0.05 stop = 3.5% per
  frame, invisible in a 30 fps timelapse of 5 s frames; GUI spinbox). No
  step is ever larger than the slew, latency cannot pile steps up, and a late
  frame is still useful. Switching Shared AE on seeds the ladder from the
  cameras' current darkest exposure. A SIGTERM/SIGINT releases the cameras
  like a window close; if an app dies hard, click `Auto All` (sends the RMS
  day line).
- **All four exposure stages are pinned** on every shared-AE push (`manual -a
  … -d 1024 -i … -e …`). A stage left in AUTO keeps floating per camera: live
  on 2026-09-14 the sensor DGain sat at 1.0x on one camera and 1.7–3.4x on the
  others at the same `-a/-e`, which showed up as different ISOs (ISO = 100 ×
  total gain) and a darker sun camera. Tiles now show `A D I` gains.
- **Cameras are handed back with RMS's own mode line**, never a bare `auto`:
  on the Goke a bare `auto` resets every AE range to the driver default
  (sensor DGain max 126x), silently breaking the science config's DGain = 1x
  (and it would have carried into the night, where the night line leaves
  DGain in auto). The line comes from the station's `camera_settings*.json`
  (resolved like RMS does, relative to the RMS checkout), day vs night chosen
  by the sun altitude with RMS's -9° rule; without a settings file it is
  rebuilt from the camera's reported ranges. `Auto All` sends the day line.
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
