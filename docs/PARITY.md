# Firmware parity audit — IMX291 vs Goke

Some sites are all **Hi3516CV300 / IMX291** (our `hisp_ctl` daemon), some are all
**Goke GK7205V200 / IMX307** (the `isp_ctl` daemon). `podcontrol` must drive
both. This is the empirical audit (probed live on .101 and .203–.206, plus the
firmware sources) of what each daemon exposes on **TCP :9600**, so we know what
the app can use today and what has to be added to reach parity.

Legend: ✅ in the :9600 daemon · 🔶 exists on the platform but via a separate
tool/patch (not the daemon) · ✗ not available.

## Command sets (as deployed)

- **IMX291 `hisp_ctl`:** `ae` `gain` `exp` `manual` `auto` `aelock` `query`
  `wb` `venc_qp` `venc_cqp` `venc_gop` `drc` `nr` `sharpen` `dpc` `vpss_nr`
  `persist` `raw` `status`
- **Goke `isp_ctl`:** `query` `manual -a/-d/-i/-e` `auto` (+ `--max-*`,
  `--strategy`, `--compensation`, `--evbias`) `pipeline` `modctrl`
  `bypass <block> on|off` `dpc/dehaze/ldci on|off` `gamma` `raw` · built-in SEI
  timestamp injection (`-T`) + VENC-PTS stream.
  Separate Goke tools (not in the daemon): `wb_ctl`, `venc_ioctl`.

## Exposure & gain

| Capability | IMX291 | Goke |
|---|---|---|
| Read state | ✅ `ae` / `query` | ✅ `query` (richer) |
| Manual analog/digital/ISP-dig/exp | ✅ `manual -a/-d/-i/-e`, `gain`, `exp` | ✅ `manual -a/-d/-i/-e` |
| Restore auto | ✅ `auto` | ✅ `auto` |
| Hard lock all | ✅ `aelock min` | ✅ (pin via `manual`) |
| Gain/exp ceilings | 🔶 (via `gain` ranges) | ✅ `--max-again/dgain/sysgain/exptime` |
| AE strategy / target brightness / EV bias | ✗ | ✅ `--strategy` `--compensation` `--evbias` |

**Common control layer the app uses now: `manual -a/-d/-i/-e` + `auto` — works on both.**

## Telemetry / metering

| Field | IMX291 | Goke |
|---|---|---|
| Gains, exposure, ISO | ✅ `ae` | ✅ `query` |
| **AveLum** (frame brightness) | ✗ | ✅ |
| **ChipTemp** | ✗ (CV300 has no die-temp) | ✅ |
| AE ranges / Route / HistError / ExposureMAX | 🔶 partial | ✅ |

**Gap handled in-app:** metering (mean-luma + clip fraction) is computed from the
frame, so the shared-AE signal is uniform across platforms regardless of AveLum.

## White balance

| Capability | IMX291 | Goke |
|---|---|---|
| Read WB gains / op | ✅ `wb` | 🔶 `wb_ctl` (not in daemon) |
| **Arbitrary manual R/G/B gains** | ✅ `wb <R> <G> <B>` | ✗ **← needed for cloud-gray** |
| Unity WB | ✅ `wb unity` | 🔶 `wb_ctl unity` |
| Auto WB | ✅ `wb auto` | 🔶 `wb_ctl auto` / `bypass wb` |
| Unity base | 256 (Q4.8) | 128 |
| Auto-persist / persist | ✅ `persist` + jffs2 | 🔶 config |

## Encoder

| Capability | IMX291 | Goke |
|---|---|---|
| Bitrate | ✅ `venc_gop <chn> <gop> <br>` | 🔶 `venc_ioctl -b` |
| GOP | ✅ `venc_gop` | 🔶 `venc_ioctl -g` |
| Constant luma QP | ✅ `venc_qp` (CBR min==max) | 🔶 FIXQP mode (`venc_ioctl`) |
| **Chroma QP offset** | ✅ `venc_cqp` (chroma_qp_index_offset) | ✗ |
| RC modes | CBR (+QP-pin) | CBR/VBR/AVBR/QVBR/CVBR/FIXQP/QPMAP |
| Exposed in the :9600 daemon? | ✅ | ✗ (separate `venc_ioctl`) |

## ISP pipeline blocks

IMX291 uses named commands; Goke uses `bypass <block> on|off` over a 17-block set:
`dpc, dehaze, ldci, antifc, crosstalk, lcac, de, drc, nr, sharpen, clut, hlc,
pregamma, meshshading, radialshading, ca, gcac`.

| Block | IMX291 | Goke |
|---|---|---|
| DRC | ✅ `drc off` (+ Sofia NOP patch) | ✅ `bypass drc on` |
| Spatial NR | ✅ `nr off` | ✅ `bypass nr on` |
| Temporal 3DNR | ✅ `vpss_nr off` | 🔶 (VPSS) |
| Sharpen | ✅ `sharpen off` | ✅ `bypass sharpen on` |
| DPC (eats point sources) | ✅ `dpc off` / `static` / `twinkle` | ✅ `bypass dpc on` / `dpc off` |
| Dehaze | ✗ (CV300 lacks) | ✅ `dehaze off` / `bypass dehaze` |
| LDCI | ✗ (CV300 lacks) | ✅ `ldci off` / `bypass ldci` |
| Gamma | 🔶 (Sofia LUT patch) | ✅ `gamma` (query) / `bypass pregamma` |
| LSC (mesh/radial shading) | ✗ | ✅ `bypass meshshading/radialshading` |
| CA / gcac / lcac / crosstalk | 🔶 `ca` status | ✅ `bypass …` |

## Timing (SEI / PTS)

| Capability | IMX291 | Goke |
|---|---|---|
| Per-frame SEI timestamp inject | 🔶 `rtp_patch`/`sei_metadata` (separate) | ✅ `isp_ctl -T` built-in |
| VENC-PTS transition stream | 🔶 separate | ✅ built-in |

---

## What `podcontrol` uses today (works on both)

- **Telemetry:** `query`/`ae`, normalized; brightness from the frame.
- **Control:** `manual -a/-d/-i/-e` + `auto`.

## Parity work items (to fully drive both from the app)

1. **Goke — arbitrary WB gains in the daemon.** `wb_ctl` only does unity/auto/
   bypass; the cloud-gray calibrator needs `wb <R> <G> <B>`. Add a set-gains
   command (the `libgk_awb` / `GK_API_ISP_*WB*` infra is already in `wb_ctl`).
   **Blocks Phase 3.**
2. **Goke — encoder in the daemon.** Fold `venc_ioctl` (bitrate/GOP/FIXQP) into
   `isp_ctl` as `venc_*` commands; add a chroma-QP-offset equivalent of
   `venc_cqp`. Needed for unified QP/GOP/color across a Goke pod.
3. **App — ISP-block abstraction.** Map one vocabulary to both: e.g. `block drc
   off` → IMX291 `drc off` / Goke `bypass drc on`. (Different names, same intent.)
4. **IMX291 — optional metering command.** Add AveLum/clip (ISP AE stats) to
   `hisp_ctl` for daemon-side metering parity with Goke's `query`. Optional —
   frame metering already covers it.
5. **Units normalization (app-side).** WB unity base differs (256 vs 128); gains
   are ×1024 both. The controller must scale per platform.

Priority for the pod goals: **#1 (WB) and #2 (encoder)** are the real firmware
gaps; #3 is app code; #4/#5 are polish.
