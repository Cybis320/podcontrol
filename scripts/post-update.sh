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

# Restart a running Pod Control, or the pull changes nothing: the process holds
# its Python in memory and keeps driving the pod with the old controller until
# someone restarts it by hand. That gap is not theoretical -- on 2026-09-26 a
# fix for a dawn handover fault sat in the checkout while the running app still
# had the bug. The updater only runs this hook when HEAD actually moved, so this
# is one restart per new commit, not one per hour.
# A failed restart may have stopped the app without bringing it back, so fail
# the hook: the updater then retries next hour instead of marking this applied.
"$DEST/scripts/restart-app.sh" || { cc_warn "restart failed -- Pod Control may not be running"; exit 1; }
