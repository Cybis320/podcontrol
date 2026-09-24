#!/bin/bash
# Install the Pod Control launcher onto the Desktop and into the app menu.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=podcontrol
# shellcheck source=../cc-utils/lib.sh
. "$PROJECT_DIR/cc-utils/lib.sh"

CC_VENV_SYSTEM_SITE=1
PY="$(cc_python "$PROJECT_DIR")"
# Runs in the checkout so a ./pod.json there is picked up.
cc_desktop_entry podcontrol.desktop "Pod Control" \
    "\"$PY\" -m podcontrol" "$PROJECT_DIR/icon.png" \
    "Control a pod of RMS cameras as one: preview, shared exposure, white balance" \
    "System;Video;" false "" "$PROJECT_DIR"
cc_info "Installed the Pod Control launcher (Desktop + app menu)"
