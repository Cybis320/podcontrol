# shellcheck shell=bash
# cc-utils/lib.sh -- shared installer helpers for the CC RMS utilities.
#
# Identical copy in every CC utility repo (CC_KIT_VERSION in cc-utils-update.sh
# says which kit it is). Sourced by each repo's installer; never executed.
#
#   CC_Utils layout:   ~/source/CC_Utils/<tool>/        one git checkout per tool
#   Updater:           ~/.local/share/cc-utils/cc-utils-update.sh   (cron, hourly)
#   Desktop entries:   ~/.local/share/applications/<entry>.desktop (+ Desktop copy)

CC_UTILS_DIR="${CC_UTILS_DIR:-$HOME/source/CC_Utils}"
CC_SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/cc-utils"
CC_UPDATER="$CC_SHARE/cc-utils-update.sh"
CC_CRON_TAG="cc-utils-update"

cc_info() { printf '\033[32m[%s]\033[0m %s\n' "${CC_TOOL:-cc-utils}" "$1"; }
cc_warn() { printf '\033[33m[%s]\033[0m %s\n' "${CC_TOOL:-cc-utils}" "$1" >&2; }

# Echo the interpreter to install into: the RMS virtualenv when present,
# otherwise a private .venv inside the checkout (created on demand; set
# CC_VENV_SYSTEM_SITE=1 for a tool that needs apt-only modules such as cv2).
cc_python() {
    local dest="$1" venv="${CC_VENV:-$HOME/vRMS}" opts=""
    [ "${CC_VENV_SYSTEM_SITE:-0}" = "1" ] && opts="--system-site-packages"
    if [ -x "$venv/bin/python" ]; then
        echo "$venv/bin/python"
        return 0
    fi
    if [ ! -x "$dest/.venv/bin/python" ]; then
        cc_warn "No virtualenv at $venv; creating one at $dest/.venv"
        # shellcheck disable=SC2086
        if ! python3 -m venv $opts "$dest/.venv" >&2; then
            rm -rf "$dest/.venv"
            cc_warn "python3 -m venv failed (python3-venv not installed?). Either:"
            cc_warn "    sudo apt install python3-venv     # then re-run this installer"
            cc_warn "or point CC_VENV at an existing virtualenv and re-run."
            return 1
        fi
    fi
    echo "$dest/.venv/bin/python"
}

# Install the checkout as an editable package WITHOUT letting pip resolve
# dependencies. The RMS venv sees the system site-packages (python3-numpy,
# python3-opencv, python3-pil, and on GPS stations python3-matplotlib); a
# resolver pass that finds one of those "too old" would drop a second, pip copy
# into vRMS on top of it -- the system-vs-pip clash RMS keeps tripping over.
# Instead each dependency is checked by import, and only a missing one is
# pip-installed, alone:
#
#   cc_pip_install "$PY" "$DEST" "PIL:Pillow" "yaml:pyyaml"
cc_pip_install() {
    local py="$1" dest="$2" spec mod pkg
    shift 2
    # Editable installs of a pyproject-only package need pip >= 21.3 (PEP 660);
    # old venvs (python3-venv on Buster/Bullseye) ship an older one.
    if ! "$py" -c 'import pip, sys; sys.exit(tuple(map(int, pip.__version__.split(".")[:2])) < (21, 3))' 2>/dev/null; then
        cc_info "Upgrading pip in $(dirname "$(dirname "$py")") (too old for editable installs)"
        "$py" -m pip install --quiet --upgrade pip || cc_warn "pip upgrade failed"
    fi
    # --no-build-isolation reuses the env's setuptools instead of downloading
    # one per install; fall back to isolation where setuptools is absent.
    "$py" -m pip install --quiet --no-deps --no-build-isolation -e "$dest" 2>/dev/null \
        || "$py" -m pip install --quiet --no-deps -e "$dest" || return 1
    for spec in "$@"; do
        mod="${spec%%:*}"
        pkg="${spec#*:}"
        if ! "$py" -c "import $mod" 2>/dev/null; then
            cc_info "Installing missing dependency $pkg"
            "$py" -m pip install --quiet "$pkg" || cc_warn "could not install $pkg"
        fi
    done
}

# Point DBUS at the user's session bus when run from cron/systemd, so gio can
# still mark launchers trusted while the user is logged in.
cc_session_env() {
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    fi
}

# Write a file only when its content changes, in place (same inode), so a
# Desktop launcher keeps the per-path "trusted" metadata GNOME stored for it.
cc_write_if_changed() {
    local path="$1" tmp
    tmp="$(mktemp)"
    cat > "$tmp"
    if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
        rm -f "$tmp"
        return 1
    fi
    cat "$tmp" > "$path"
    rm -f "$tmp"
    return 0
}

# Install a launcher into the app menu and onto the Desktop, in the shared
# style: the icon is the checkout's icon.png, so a git pull alone refreshes the
# artwork. Entry file names are kept stable per tool -- renaming one would make
# GNOME treat the Desktop copy as a new, untrusted launcher.
#
#   cc_desktop_entry <file.desktop> <Name> <Exec> <Icon> <Comment> <Categories> \
#                    [terminal=false] [autostart-delay] [working-dir]
cc_desktop_entry() {
    local entry="$1" name="$2" exec="$3" icon="$4" comment="$5" cats="$6"
    local terminal="${7:-false}" autostart="${8:-}" workdir="${9:-}"
    local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    local desk body
    desk="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    mkdir -p "$apps"
    body="[Desktop Entry]
Type=Application
Name=$name
Comment=$comment
Exec=$exec${workdir:+
Path=$workdir}
Icon=$icon
Terminal=$terminal
Categories=$cats
Hidden=false
NoDisplay=false"
    printf '%s\n' "$body" | cc_write_if_changed "$apps/$entry" || true

    if [ -d "$desk" ]; then
        # A symlinked launcher (older installs) is replaced by a real file.
        [ -L "$desk/$entry" ] && rm -f "$desk/$entry"
        printf '%s\n' "$body" | cc_write_if_changed "$desk/$entry" || true
        chmod +x "$desk/$entry"
        # GNOME refuses to run an untrusted Desktop launcher.
        cc_session_env
        command -v gio >/dev/null 2>&1 \
            && gio set "$desk/$entry" metadata::trusted true 2>/dev/null || true
    fi
    if [ -n "$autostart" ]; then
        mkdir -p "$HOME/.config/autostart"
        printf '%s\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=%s\n' \
            "$body" "$autostart" | cc_write_if_changed "$HOME/.config/autostart/$entry" || true
    fi
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$apps" 2>/dev/null || true
}

# Wrap a command so it opens in a terminal window (for TUI tools).
cc_terminal_exec() {
    local title="$1" cmd="$2"
    if command -v gnome-terminal >/dev/null 2>&1; then
        echo "gnome-terminal --title=\"$title\" --geometry=80x24 -- $cmd"
    elif command -v lxterminal >/dev/null 2>&1; then
        echo "lxterminal --title=\"$title\" --geometry=80x24 -e $cmd"
    elif command -v xfce4-terminal >/dev/null 2>&1; then
        echo "xfce4-terminal --title=\"$title\" --geometry=80x24 -e \"$cmd\""
    elif command -v xterm >/dev/null 2>&1; then
        echo "xterm -title \"$title\" -geometry 80x24 -e $cmd"
    else
        echo "$cmd"
    fi
}

# Install (or refresh) the shared updater outside every checkout and register
# its hourly cron line. The executed copy lives out of the tree on purpose: an
# updater inside the repo it updates cannot rescue that repo once it jams.
cc_install_updater() {
    local kit="$1" minute line current
    [ "${CC_NO_AUTOUPDATE:-0}" = "1" ] && { cc_warn "CC_NO_AUTOUPDATE=1 -- auto-update not installed"; return 0; }
    mkdir -p "$CC_SHARE"
    # Never downgrade: another tool may already have installed a newer kit.
    if [ ! -x "$CC_UPDATER" ] \
       || [ "$(cc_kit_version "$kit/cc-utils-update.sh")" -gt "$(cc_kit_version "$CC_UPDATER")" ]; then
        install -m 0755 "$kit/cc-utils-update.sh" "$CC_UPDATER"
    fi
    if ! command -v crontab >/dev/null 2>&1; then
        cc_warn "crontab not found -- auto-update not scheduled"
        return 0
    fi
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -q "# $CC_CRON_TAG\$"; then
        return 0
    fi
    # A per-host minute spreads fleet fetches across the hour.
    minute=$(( $(cksum <<<"$(hostname)" | cut -d' ' -f1) % 60 ))
    line="$minute * * * * $CC_UPDATER >/dev/null 2>&1 # $CC_CRON_TAG"
    { [ -n "$current" ] && printf '%s\n' "$current"; printf '%s\n' "$line"; } | crontab -
    cc_info "Auto-update scheduled hourly (minute $minute): $CC_UPDATER"
}

cc_kit_version() {
    sed -n 's/^CC_KIT_VERSION=\([0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n1 | grep . || echo 0
}

# Record that this checkout's post-update hook has been applied at HEAD, so the
# updater does not re-run it until the next pull.
cc_mark_applied() {
    local dest="$1"
    git -C "$dest" rev-parse HEAD > "$dest/.git/cc-utils-applied" 2>/dev/null || true
}
