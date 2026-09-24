# cc-utils kit

Shared by every CC RMS utility; this folder is an identical copy in each repo
(`CC_KIT_VERSION` in `cc-utils-update.sh` says which kit it is).

| Tool | Repo | Checkout | Updated by |
|---|---|---|---|
| RMS Config Editor | cc-rms-config-editor | `~/source/CC_Utils/config_editor` | cc-utils (hourly) |
| Frames Dashboard | cc-rms-frames-dashboard | `~/source/CC_Utils/frames_dashboard` | cc-utils (hourly) |
| rms_update_cron | cc-rms-update-cron | `~/source/CC_Utils/rms_update_cron` | cc-utils (hourly) |
| Pod Control | podcontrol | `~/source/CC_Utils/podcontrol` | cc-utils (hourly) |
| RMS window layout | RMS-WindowLayout | `~/source/CC_Utils/window_layout` | cc-utils (hourly) |
| MQTT monitor | cc-rms-mqtt-monitor | `~/source/CC_Utils/MQTT_monitor` | its own root timer (15 min) |
| Chrony Monitor | chrony_monitor | `~/source/CC_Utils/chrony_monitor` (+ root-owned `/opt/chrony_monitor`) | its own root timer (daily) |

Every tool installs the same way:

    curl -fsSL https://raw.githubusercontent.com/Cybis320/<repo>/<branch>/install.sh | bash

## Files

- `lib.sh`: installer helpers (pick the venv, `pip install --no-deps` with
  import-checked dependencies, launchers, updater registration).
- `cc-utils-update.sh`: the hourly updater. `install.sh` copies it to
  `~/.local/share/cc-utils/` and adds one crontab line tagged
  `# cc-utils-update`. It updates each checkout that has an executable
  `scripts/post-update.sh`, then runs that hook. Its log is
  `~/.local/state/cc-utils-update.log`. To opt one checkout out, run
  `touch <checkout>/.no-autoupdate`.
- `make_icon.py`: renders the shared icon family. Run
  `python3 cc-utils/make_icon.py --all /tmp/icons` to preview them.

## No pip dependency resolution in vRMS

`~/vRMS` is a `--system-site-packages` venv, so it sees the apt copies of
numpy, OpenCV, Pillow and, on GPS stations, matplotlib (`gpsd-clients` depends
on `python3-matplotlib`). If pip's resolver decides one of those is "too old",
it installs a second copy into vRMS on top of the system one. So the tools
install themselves with `--no-deps`. Each dependency is checked by importing
it, and only a dependency that is missing entirely gets pip-installed.

## Changing the kit

Edit it in one repo, bump `CC_KIT_VERSION`, and copy the folder to the other
repos. Installed updaters switch to the highest version they find in any
checkout.
