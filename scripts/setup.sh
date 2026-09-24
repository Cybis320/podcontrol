#!/usr/bin/env bash
#
# setup.sh -- install podcontrol from this checkout. Run it through install.sh:
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/podcontrol/master/install.sh | bash
#       -- or, from a clone --
#   ./install.sh
#
# Idempotent. Installs the package editable into the RMS virtualenv (~/vRMS)
# with --no-deps, checks the apt-only modules it needs, adds a Pod Control
# launcher, and schedules the shared hourly cc-utils updater.
#
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=podcontrol
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

# OpenCV, numpy and Tk come from apt (the RMS venv sees system site-packages).
# A pip opencv-python drags in numpy 2 and breaks RMS's compiled extensions, so
# they are checked here, never pip-installed.
export CC_VENV_SYSTEM_SITE=1
PY="$(cc_python "$DEST")"
cc_info "Installing package into $PY"
cc_pip_install "$PY" "$DEST" "PIL:Pillow"

missing=()
need() { "$PY" -c "import $1" 2>/dev/null || missing+=("$2"); }
need tkinter python3-tk
need PIL.ImageTk python3-pil.imagetk
need cv2 python3-opencv
need numpy python3-numpy
command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
    cc_warn "Missing system packages; install them with:"
    cc_warn "    sudo apt install ${missing[*]}"
fi
# The sky map and sun/moon mask use RMS itself (and its ephem): present when
# installed into the RMS venv, which is the supported setup.
if ! "$PY" -c "import RMS, ephem" 2>/dev/null; then
    cc_warn "RMS/ephem not importable from $PY -- sky map and sun mask are disabled."
    cc_warn "Install into the RMS venv (CC_VENV=~/vRMS) to enable them."
fi

if command -v xdg-user-dir >/dev/null 2>&1 || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    "$DEST/scripts/install-desktop.sh"
else
    cc_warn "No graphical session detected -- skipped the desktop icon."
    cc_warn "Install it later from the desktop session:  $DEST/scripts/install-desktop.sh"
fi

cc_install_updater "$DEST/cc-utils"
cc_mark_applied "$DEST"

echo
cc_info "Done. Click the 'Pod Control' icon, or run:"
cc_info "    cd $DEST && $PY -m podcontrol"
cc_info "Updates arrive hourly (cc-utils updater)."
