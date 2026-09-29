#!/usr/bin/env bash
#
# Restart a running Pod Control, preserving exactly how it was launched.
#
# A git pull cannot change a running process: podcontrol holds its Python in
# memory, so an updated station keeps driving the pod with the old controller
# until somebody logs in and restarts it by hand. On 2026-09-26 that gap left
# the fix for a dawn handover fault sitting in the checkout while the running
# app still had the bug. scripts/post-update.sh calls this after every pull.
#
#   ./scripts/restart-app.sh              restart whatever is running
#   ./scripts/restart-app.sh --pid 1234   restart only that process
#   ./scripts/restart-app.sh --dry-run    report what it would do
#
# Shutdown is graceful: podcontrol handles SIGTERM like a window close, so it
# saves its settings, restores the cameras' colour settings and hands the pod
# back before exiting. SIGKILL follows only if it will not go.
#
# CC_NO_RESTART=1 skips the restart entirely.
set -uo pipefail

TERM_WAIT=20          # seconds to allow for the graceful close
START_WAIT=15         # seconds to wait for the replacement to appear
ONLY_PID=""
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --pid)
            # An empty or non-numeric --pid must be an error, never a silent
            # fall-through to "restart everything": passing --pid "$EMPTY" from a
            # test script did exactly that on 2026-09-26 and restarted the live
            # app it was meant to leave alone.
            case "${2:-}" in
                "" | *[!0-9]*) echo "$0: --pid needs a numeric pid, got '${2:-}'" >&2; exit 2 ;;
            esac
            ONLY_PID="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "usage: $0 [--pid N] [--dry-run]" >&2; exit 2 ;;
    esac
done

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=podcontrol
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

[ "${CC_NO_RESTART:-0}" = "1" ] && { cc_info "CC_NO_RESTART=1 -- not restarting"; exit 0; }

# A GUI relaunched from cron needs the session's display and bus, and cron
# has neither: the crontab sets no DISPLAY/XAUTHORITY. On 2026-09-26 the first
# post-update restart stopped the app and relaunched it without a display; Tk
# died on the spot and the pod was left with no controller at all. So the
# replacement inherits these from the process it replaces (session_env_from),
# and cc_session_env fills in DBUS if that did not.
SESSION_VARS="DISPLAY XAUTHORITY WAYLAND_DISPLAY XDG_RUNTIME_DIR XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS"
LAUNCH_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/podcontrol-restart.log"

# Every way podcontrol is started: the console script, or `python -m podcontrol`
# (what the autostart entry and the desktop launcher use).
#
# This inspects the argument VECTOR, never the flattened command line. Matching
# a substring of the whole line looked fine and was badly wrong: any process
# whose arguments merely mention podcontrol matched too, and the first dry run
# duly offered to restart the shell that was writing this script, because the
# text "-m podcontrol" appeared inside its -c argument. A wrapper shell, an
# editor, a grep or this script's own parent would all have qualified.
is_podcontrol() {
    local pid="$1" base one
    local -a a
    [ -r "/proc/$pid/cmdline" ] || return 1
    mapfile -d '' -t a < "/proc/$pid/cmdline"
    [ "${#a[@]}" -gt 0 ] || return 1
    base="$(basename -- "${a[0]}")"
    # exec'd with podcontrol as argv[0]
    [ "$base" = "podcontrol" ] && return 0
    case "$base" in
        python|python[0-9]*)
            # the module form: python -m podcontrol [args]
            [ "${#a[@]}" -ge 3 ] && [ "${a[1]}" = "-m" ] && [ "${a[2]}" = "podcontrol" ] && return 0
            # the console script: its #! line makes the kernel run
            # python /path/bin/podcontrol [args], so podcontrol lands in argv[1]
            if [ "${#a[@]}" -ge 2 ]; then
                one="$(basename -- "${a[1]}")"
                [ "$one" = "podcontrol" ] && return 0
            fi
            ;;
    esac
    return 1
}

find_pids() {
    local p
    for p in $(pgrep -u "$(id -u)" -f podcontrol 2>/dev/null || true); do
        [ "$p" = "$$" ] && continue
        is_podcontrol "$p" && printf '%s\n' "$p"
    done
}

# Export the running app's session variables, so its replacement opens on
# the same display. Fails when the app has a display but we still cannot reach
# one -- the caller must then leave the app running rather than stop it.
session_env_from() {
    local pid="$1" line v
    while IFS= read -r -d '' line; do
        for v in $SESSION_VARS; do
            case "$line" in "$v="*) export "$line" ;; esac
        done
    done < "/proc/$pid/environ"
    cc_session_env
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]
}

restart_one() {
    local pid="$1" i cwd exe now_pid
    local -a argv
    [ -r "/proc/$pid/cmdline" ] || { cc_warn "pid $pid is gone"; return 0; }
    # Check the target even when it was named explicitly: --pid used to restart
    # whatever pid it was handed, which is how a test aimed at a decoy restarted
    # the live app instead.
    if ! is_podcontrol "$pid"; then
        cc_warn "pid $pid is not Pod Control ($(tr '\0' ' ' < "/proc/$pid/cmdline")) -- refusing"
        return 1
    fi
    mapfile -d '' -t argv < "/proc/$pid/cmdline"
    [ "${#argv[@]}" -gt 0 ] || { cc_warn "pid $pid has no command line"; return 1; }
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo "$DEST")"
    [ -d "$cwd" ] || cwd="$DEST"
    exe="${argv[0]}"
    command -v "$exe" >/dev/null 2>&1 || [ -x "$exe" ] || {
        cc_warn "pid $pid: cannot re-exec ${exe} -- leaving it running"; return 1; }

    if ! session_env_from "$pid"; then
        cc_warn "pid $pid: no display to relaunch on -- leaving it running"; return 1
    fi

    if [ "$DRY" = "1" ]; then
        cc_info "would restart pid $pid in $cwd on ${DISPLAY:-$WAYLAND_DISPLAY}: ${argv[*]}"
        return 0
    fi

    cc_info "Restarting Pod Control (pid $pid): ${argv[*]}"
    kill -TERM "$pid" 2>/dev/null || { cc_warn "pid $pid: could not signal it"; return 1; }
    for ((i = 0; i < TERM_WAIT; i++)); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        cc_warn "pid $pid ignored SIGTERM after ${TERM_WAIT}s -- sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 2
    fi

    # setsid so the new app outlives this script and its cron parent.
    # Its output goes to a log, not /dev/null: a relaunch that dies at startup
    # otherwise leaves nothing to say why.
    # 9>&- because fd 9 is the cc-utils updater's run lock: inherited, the app
    # held it for its whole life and every later hourly run bailed out as "already
    # running". On 2026-09-29 a commit pushed at 18:00 was not pulled until 23:07.
    mkdir -p "$(dirname "$LAUNCH_LOG")"
    echo "$(date -u +%FT%TZ) relaunch: ${argv[*]} (DISPLAY=${DISPLAY:-})" >> "$LAUNCH_LOG"
    ( cd "$cwd" && setsid nohup "${argv[@]}" >>"$LAUNCH_LOG" 2>&1 < /dev/null & ) 9>&- || {
        cc_warn "relaunch failed: ${argv[*]}"; return 1; }
    for ((i = 0; i < START_WAIT; i++)); do
        sleep 1
        # a pid that is not the one just stopped: "any podcontrol is running"
        # would be satisfied by a second instance that never went away
        for now_pid in $(find_pids); do
            if [ "$now_pid" != "$pid" ]; then
                cc_info "Pod Control is back up (pid $now_pid)"
                return 0
            fi
        done
    done
    cc_warn "Pod Control did not come back within ${START_WAIT}s: ${argv[*]} (see $LAUNCH_LOG)"
    return 1
}

if [ -n "$ONLY_PID" ]; then
    restart_one "$ONLY_PID"
    exit $?
fi

pids="$(find_pids)"
if [ -z "$pids" ]; then
    cc_info "Pod Control is not running -- nothing to restart"
    exit 0
fi
rc=0
for p in $pids; do
    restart_one "$p" || rc=1
done
exit $rc
