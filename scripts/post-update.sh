#!/usr/bin/env bash
# Run by the cc-utils hourly updater after it pulls new code: as the user,
# unattended, possibly with no display. Refreshes what a pull alone does not.
# Launchers are left alone: their Icon= points at the checkout's icon.png, so a
# pull already brings new artwork, and a launcher someone edited stays edited.
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=podcontrol
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

# Package metadata and entry points (the code itself is an editable install).
export CC_VENV_SYSTEM_SITE=1
PY="$(cc_python "$DEST")"
cc_pip_install "$PY" "$DEST" "PIL:Pillow"
