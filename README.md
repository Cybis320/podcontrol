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

Two distinct ghosts, each at a fixed fraction *k* of the sun's distance from
the principal point, on the mirrored side: **A** at k = −0.55 (B1, sun 11–17°
out; a pale disc ~5° in radius that is gone once the sun is ~30° out) and
**B** at k = −0.20 (F1/C1, sun 32–49° out; ~4° radius; near the axis it falls
inside the sun zone anyway). The model excludes a disc for each (radius
**flare r°** = 6° × 1.0 / 0.7, spinbox, 0 disables; A only while the sun is
within 22° of the principal point) while the sun is within 30° of the field,
plus a 3° half-width **corridor** along the whole axis while the sun is
inside the frame. Violet in the overlay. Other lenses: `python -m
podcontrol.sunmask --measure STATION` lists the bright blobs with their *k*
and off-axis angle; edit `sunmask.FLARE_GHOSTS` (k, radius scale, max D).

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

## What counts as clipped

A pixel is clipped when its **brightest channel** is at 250 or above, and the
headroom peak is the 99.9th percentile of the per-pixel maximum channel. The
earlier luma rule (0.299R + 0.587G + 0.114B ≥ 250) was effectively "green
clipped" and ignored red or blue at 255 when green sat lower; on 2026-09-14
afternoon frames the any-channel count was 2–7× the luma count. The pod mean
stays luma. On top of that, `raw_sat` / `rb_only` say which stage clipped.

## The WB rung: a lever below the exposure floor

At the 30 µs / 1x floor the sensor cannot be darkened further, but the WB
gains (R 1.8x, B 1.9x, G 1.0x) are applied in the 12-bit stage before
demosaic, so clipping *they* cause is recoverable: scaling R, G, B together
keeps the balance and lowers the values. Measured 2026-09-14: in the late
afternoon 88–99% of the clipped pixels had green below saturation with red
and blue at 255 (gain-induced), while under direct-sun clouds green itself
was at 254 (raw saturation, unrecoverable).

So the ladder extends below the floor with a **WB rung**: light index < 0 is
a WB attenuation of 2^li, bottoming where the largest gain reaches 1.0x
(1/1.914 here, −0.94 stop): below that every channel is under 1x and only
raw data would be darkened. The metering splits clipped pixels into
`rb_only` (red/blue at 255 with green below its plateau) and `raw_sat`
(green at its plateau, 255·√scale when attenuated). On the rung the
controller goes down for `rb_only`, holds for `raw_sat`, and does not climb
back until there is clear headroom. The base WB is captured at takeover
(manual WB only), the attenuated `wb` is pushed with each step, restored on
release, re-asserted if RMS rewrites WB, and the History records `wb_scale`.
**Magenta guard.** A raw-saturated pixel is R 1.8s / G 1.0s / B 1.9s after
WB: at s = 1 it clips to white, at any s < 1 it turns magenta (the classic
raw-highlight problem; the gain-clipped pixels have that cast *today* and the
rung removes it). So the rung is only used while raw saturation is below
`wb_rung_raw_sat_max` (0.02% of the frame), and the pod climbs back to s = 1
as soon as a raw-saturated zone appears. `wb_lever = False` disables it.
Two blind spots closed 2026-09-16, after the pod ran to the rung's bottom
with a violet sun halo (gains 1.0/0.555/1.06): the guard counted raw
saturation over the *unmasked* pixels only, and the halo, the raw-saturated
zone par excellence, sits inside the sun-zone mask, so it now counts raw
saturation anywhere in the frame (`raw_sat_all`); and it ran only for frames
captured *at* the floor, while heavily clipped frames from just above it
drove the target straight past it, so a target that would cross below the
floor is now checked too. Two more closed 2026-09-17, after C1 and F1 went
magenta again at midday (R 240 G 178 B 251, pod at the rung bottom): the
guard was per camera while the WB is pod-wide, so a camera whose clipping
was purely gain-induced kept asking for the rung and darkest-need-wins
took it while another camera's halo went violet; now raw saturation on
**any** frame of the set pins every need at the floor, and a pod already on
the rung climbs out at the full slew (a magenta fix is not a cloud
transient). And the plateau was *predicted* from the WB scale the AE assumed
in effect, which differed from the cameras' actual scale (0.568 vs 0.527)
by more than the 8-level margin, so the guard saw nothing; the plateau is
now *detected* (`frames.g_plateau`): the most populated green level near
the top of the histogram, accepted when it stands out as a spike above the
levels just below it, which a clipped highlight always produces and a
smooth sky never does. Note the rung is armed only when the takeover finds a
manual WB with gains above 1x; an app armed at night (`wb unity`) never
attenuates, which is why the cast appeared only after the daytime restart.

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

## White balance controls

The **white balance** group holds R, G, B gain multipliers (the daemons' ×256
gains shown as ×1.00; seeded from the first camera's current WB at start,
then remembered), **Apply** pushes the same `wb` to every camera, **Auto**
hands WB back to the cameras' AWB. A **Calibrate WB (cloud)** run leaves its
result in these boxes (so it is visible, persisted and re-applicable) and
every tile's third line shows the gains the camera actually holds, R/G/B,
in auto mode too. The **≈ K** box is a colour
temperature that works both ways. As a readout it is the estimated correlated
colour temperature of the R/G/B gains: the gains are inverted to an
illuminant colour, taken through the sRGB matrix to chromaticity and
McCamy's formula, anchored so the config's daylight preset (460/256/490)
reads as D65. Edit it (3800–20000 K, steps of 100) and R and B are set to
neutralise a daylight-locus illuminant of that temperature, G kept as it is,
with the same anchor (6500 K gives 461/256/491; 8000 K gives 515/256/410,
next to the 523/256/411 a cloud calibration found on 2026-09-16). It is one
axis only: the green–magenta tint is fixed to the locus, so a calibrated
balance is not reproducible from its Kelvin readout alone, and the calibrator
stays the source of truth. Relative shifts are meaningful (more R gain =
bluer light = higher K); absolute values are approximate, not a calibrated
colorimeter, and the readout drifts above ~12000 K.

## Sky view (the pod on one sky map)

The **View: tiles / sky** button swaps the tile grid for one all-sky
composite of the whole pod (`skymap.py`), built from the station
platepars: for every map pixel each platepar says which sensor pixel looks
that way (RMS `raDecToXYPP` at the platepar's own epoch, so a fixed
camera's pixel to alt/az map is computed once and never involves an
ephemeris; directions behind the camera, which the gnomonic projection
folds back into the frame, are rejected by a round trip through
`xyToRaDecPP`). The map is zenith-centred azimuthal equidistant, north up
and east *left* like an all-sky camera, with altitude circles at 30° and
60°, the cardinal points, each camera's footprint outlined in its colour
with its id, exposure and total gain (and the `<< DRIVING` badge), and the
sun/moon markers. With the **overlay** on, the same exclusion zones as on
the tiles are tinted on the map (red RMS mask, orange sun zone, violet
flare, blue moon, magenta clipped pixels), warped through the same lookup.
The composite always uses the **newest complete frame set** (the six
coherent frames the shared AE meters); the caption shows its capture time
and age. The chosen view is remembered across restarts.

First render (2026-09-16): 93% of the sky above the horizon is covered; the
four uncovered wedges sit at the diagonal azimuths below ~8° altitude, where
adjacent horizon cameras' bottom corners do not quite meet.

**Cost** (measured in the running app, 2026-09-16). Lookup tables take
~1.5 s per camera (900 px map) the first time and are cached in
`~/.cache/podcontrol/skymap/` (keyed by platepar, mask and map parameters).
The renderer then keeps three cache levels: the blended composite with its
static decorations (grid, outlines) is redone only when the frame set
changes (~60 ms, every ~50 s on a capturing pod: six INTER_AREA downscales
and six remaps over each camera's bounding box, with normalised blend
weights precomputed so there is no division); the overlay tints are redone
only when a zone map changes (the sun zone moves in 30 s buckets, the clip
map per frame), the per-camera warps being reused while their maps are the
same arrays; every other cycle costs a copy plus the labels: **~5 ms**
without the overlay, **~10–15 ms** with it. The render runs in the updater
thread from the same cached frames as the tiles, only while the sky view is
shown, and reuses the tiles' own zone maps whenever a camera's set frame is
the file its tile shows; the hidden tiles are not drawn at all. The finished
map is pre-scaled to the canvas in the worker, so the Tk thread only converts
it to a photo (~14 ms per cycle; a full redraw of the six tiles is ~150 ms,
which the tile view pays once per new frame per camera). Frames are
downscaled once per saved file; the decoded-frame cache holds both the
tiles' newest frames and the set's frames, and the zone / highlight caches
evict oldest-first (they used to clear themselves at the limit, which with
two consumers meant recomputing every zone every cycle).

Headless / for a timelapse:

```
~/vRMS/bin/python -m podcontrol.skymap                     # -> /tmp/podcontrol/skymap.png
~/vRMS/bin/python -m podcontrol.skymap --size 1000 --loop 5 --out sky.png
~/vRMS/bin/python -m podcontrol.skymap --proj pano --size 1440   # az/alt panorama (CLI only)
```

## Colour: identity matrix + saturation (pod-wide)

The least destructive place to boost colour is the ISP's ColorMatrix stage,
which sits in linear RGB before gamma and before the 8-bit RGB→YUV
conversion, and whose saturation attribute (`satu`, 128 = 1.0x, 255 ≈ 2x) is
applied *inside* it (active CCM = saturation matrix × CCM). With the stage
bypassed, as the science day line had it (`ccm off`), `satu` is inert and
daytime colour is the raw sensor colour, muted by the Bayer filters'
spectral overlap. With the IQ colour-temperature table (`ccm auto`) it is
colour correction plus chroma gain, but the table is strong (diagonals
1.85–1.92 on the IMX307), doubles chroma noise and follows the AWB's
temperature estimate. The daemon (`isp_ctl`, 2026-09-17) therefore gained
**`ccm identity`**: a manual identity matrix with saturation enabled, so that
`satu` becomes a pure chroma gain around the luma axis: grey stays grey, hue
is kept, each channel keeps its sensor response, and the transform is
invertible while nothing clips. `ccm manual <9 signed 8.8 values>` and
`ccm auto` are there too; `ccm on|off` is still the stage bypass. Setting a
matrix switches the stage on. The matrix persists under its own key
(`matrix …`) beside the bypass line, and `ccm` / `pipeline` report it.

The **colour (pod-wide)** toolbar group holds the matrix mode (identity /
auto / off) and the saturation (0–255, shown as a multiplier); **Apply**
pushes both to every camera, each tile's third line shows what the camera
holds (`satu x1.25 ccm identity`), and the values are remembered. RMS's day
line replays `ccm off` / `satu 128` at every dawn switch: either update the
`day` entry of `camera_settings_openipc.json` to `["Isp","ccm","identity"]`
and `["Isp","satu","<v>"]` (durable), or tick **hold**, which re-asserts the
setting on a camera that lost it once a minute. Expect strongly coloured
highlights to clip in one channel sooner, which the shared AE's any-channel
clip metric answers with a slightly darker pod on colourful skies; the WB
rung's magenta guard, the cloud-grey calibrator and the Kelvin readout are
unaffected because the matrix is neutral-preserving.


## Settings

Toolbar values (refresh, slew, clip threshold, point-source tolerance,
overlay, sun / moon / flare radii, sun-cam-votes), the Shared AE on/off state,
the view (tiles or sky) and the window geometries are saved to `~/.config/podcontrol/settings.json`
(`$PODCONTROL_SETTINGS` overrides) on every change and on close, and restored
at start. If Shared AE was on at the last exit the pod is taken over again
after the first poll, so an armed pod survives a restart; the History window
reopens if it was open.

## History

Every cycle is logged to `~/.local/share/podcontrol/history.jsonl`
(`$PODCONTROL_HISTORY` overrides; the last 12 h are kept and reloaded on
start): shared-AE state (light index, target, exposure, gain, state, driver),
pod metering (mean, peak, clip), sun altitude, and every camera's own
exposure/gain. The **History** button opens a window with four strips over
the last 12 h: exposure time per camera (log scale, 30 µs to 40 ms; green =
the pod while Shared AE drives) with night/latched/AE-on shading and the sun
altitude on the right axis; total gain per camera (log, analog × sensor-digital
× ISP-digital) with ISO = 100 × gain on the right axis; pod mean and 99.9%
peak luma against the 234 ceiling; clipped fraction with a tick in the driving
camera's colour. Plain Tk canvas, no extra dependencies.

## Footprint

The app is meant to sit beside six RMS captures. Per 5 s cycle it does one
cached directory scan of each station's newest two hour-directories (never a
recursive glob over FramesFiles), decodes a frame only when a new file
appears (~every 50 s per camera, cached), caches statistics and overlay maps
per file, and re-renders a tile only when its frame or overlay settings
change. Measured: ~10 ms of CPU per cycle headless, ~6% of one core for the
GUI (2026-09-14, six cameras).

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
  by the sun altitude with RMS's -9° rule **at the moment of the hand-back**;
  without a settings file it is rebuilt from the camera's reported ranges.
  `Auto All` sends the day line. (Until 2026-09-16 the hand-back line was
  fixed at takeover: an app armed at night and closed at 09:00 local replayed
  the night line, 40 ms at 45x, into full sun; every frame was white and the
  restarted shared AE needed 30 min to slew the 16 stops back down. The
  takeover now stores the cameras' telemetry and the line is chosen when the
  cameras are released.)
- **Fast slew at startup and on gross errors (2026-09-16).** The smooth
  slew is right for tracking the sky but hopeless for a gross error: when the
  pod was handed the night line in full sun, every frame was white and the
  AE needed 30 min to bring 16 stops back (0.5 stop per ~50 s frame set,
  trailed at 0.05 stop/cycle). Two exceptions now run at **fast** (toolbar,
  default 1 stop/cycle): the **startup phase** after a takeover, which ends
  when the pod first reaches a post-takeover target (or after 10 min), and a
  **gross error**, a frame more than 10 % clipped, which may move the target
  from 0.5 stop (at 10 %) up to 3 stops (at 100 %) at once, monotone in the
  clip fraction so successive sets converge without pumping, and never
  upward (a white frame is a lower bound on the excess). During the startup
  phase a dark seed (99.9 % peak under 180) gets a calibrated jump toward the
  ceiling (log2 of ceiling/peak, gamma taken as 1 so it never overshoots)
  instead of the proportional nudge. Mild errors keep the smooth slew on
  purpose: with frame sets ~50 s old, jumping to a small step's target and
  waiting is no faster than creeping there. A takeover at night latches and
  starts no fast phase, so dawn still comes down at the smooth slew.
  Replayed in simulation against the recorded incident: 16 stops in 5 min
  instead of 30; a 2-stop dark seed in 2.5 min instead of 12; a 1-stop bright
  seed and a one-minute cloud transient unchanged.
- **Clouds and the diurnal trend.** A passing cloud that clips asks for less
  light, then more again a minute later, which pumps the pod. Rather than a
  hard "high-water mark" (which fails when an overcast morning clears), moves
  *against* the diurnal trend (less light while the sun sets, more while it
  rises) run at `slew_against` (0.015 stop/cycle, a third of the normal rate)
  unless clipping is extended (`clip_emergency`, 1%); moves with the trend run
  at the full slew. A one-minute cloud then costs ~0.1 stop; a real change still
  arrives within minutes. The clip threshold is also a toolbar knob
  (**clip ≤ %**, default 0.005% = ~100 px of the frame; try 0.05% for a
  timelapse if cloud edges still pump it).
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
  skymap.py     all-sky composite of the pod from the platepars (the Sky view)
  app.py        Tkinter UI (preview tiles + telemetry + controls)
```
