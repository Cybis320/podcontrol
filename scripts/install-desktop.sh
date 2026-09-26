#!/bin/bash
# Install the Pod Control launcher onto the Desktop, into the app menu, and
# into ~/.config/autostart so the pod comes up with the session -- the same
# mechanism RMS uses for its own US05*_StartCap.desktop entries.
#
#   CC_NO_AUTOSTART=1              install the launcher only, no autostart
#   PODCONTROL_AUTOSTART_DELAY=90  seconds to wait after login (default 60)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=podcontrol
# shellcheck source=../cc-utils/lib.sh
. "$PROJECT_DIR/cc-utils/lib.sh"

CC_VENV_SYSTEM_SITE=1
PY="$(cc_python "$PROJECT_DIR")"

# RMS's StartCapture entries autostart with no delay, so RMS is already opening
# its RTSP sessions when the session comes up. Pod Control waits, then reads the
# frames RMS saves. Long enough that RMS owns the cameras first; the app is
# tolerant of arriving before any frame exists.
DELAY="${PODCONTROL_AUTOSTART_DELAY:-60}"
[ "${CC_NO_AUTOSTART:-0}" = "1" ] && DELAY=""

# --no-grab: read RMS's saved frames and never open an RTSP session. At login
# RMS has not written a frame yet, so frame auto-detect would judge every camera
# idle and fire an ffmpeg grab on all six exactly while RMS is opening its own
# streams. Unattended, reading what RMS already saves is the safe side.
# Runs in the checkout so a ./pod.json there is picked up.
cc_desktop_entry podcontrol.desktop "Pod Control" \
    "\"$PY\" -m podcontrol --no-grab" "$PROJECT_DIR/icon.png" \
    "Control a pod of RMS cameras as one: preview, shared exposure, white balance" \
    "System;Video;" false "$DELAY" "$PROJECT_DIR"

if [ -n "$DELAY" ]; then
    cc_info "Installed the Pod Control launcher (Desktop + app menu + autostart, ${DELAY}s delay)"
else
    cc_info "Installed the Pod Control launcher (Desktop + app menu; autostart skipped)"
fi
