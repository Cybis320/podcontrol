#!/usr/bin/env bash
#
# cc-utils-update -- keep every CC utility under ~/source/CC_Utils current.
#
# Runs hourly from the user's crontab, as the user. The copy cron executes
# lives in ~/.local/share/cc-utils, outside every checkout: an updater inside
# the repo it updates cannot rescue that repo once it jams.
#
# A checkout takes part when it has an executable scripts/post-update.sh. Tools
# with their own root updater (MQTT_monitor, chrony_monitor) have none and are
# skipped. For each participating checkout:
#   1. fetch its branch and fast-forward; if local edits or commits block that,
#      keep them (rescue branch + stash) and reset to origin, like the MQTT
#      monitor's updater. Opt out per checkout: touch <checkout>/.no-autoupdate
#   2. run scripts/post-update.sh whenever HEAD differs from the last commit it
#      succeeded on (.git/cc-utils-applied), so a failed hook retries next hour.
#
# Afterwards the installed copy of this script is replaced by the newest kit
# found in any checkout (higher CC_KIT_VERSION wins).
#
CC_KIT_VERSION=1

set -uo pipefail

ROOT="${CC_UTILS_DIR:-$HOME/source/CC_Utils}"
SELF="${XDG_DATA_HOME:-$HOME/.local/share}/cc-utils/cc-utils-update.sh"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/cc-utils-update.log"
PATH="$PATH:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
# Keep the log bounded.
if [ "$(wc -l <"$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$LOG" >"$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

log() { echo "$(date -u +%FT%TZ) $*"; }

# One run at a time.
exec 9>"$(dirname "$LOG")/cc-utils-update.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
    exit 0
fi

update_one() {
    local dir="$1" name branch before after applied ts
    name="$(basename "$dir")"
    branch="$(git -C "$dir" symbolic-ref --short -q HEAD)" || {
        log "$name: detached HEAD, skipped"; return 0; }
    before="$(git -C "$dir" rev-parse HEAD)"

    if ! timeout 120 git -C "$dir" fetch --quiet origin "$branch"; then
        log "$name: fetch failed (offline?)"
    elif ! git -C "$dir" merge --ff-only --quiet "origin/$branch" >/dev/null 2>&1; then
        ts="$(date -u +%Y%m%d-%H%M%S)"
        git -C "$dir" branch --force "cc-autoupdate-rescue-$ts" HEAD >/dev/null 2>&1 || true
        if ! git -C "$dir" diff --quiet 2>/dev/null; then
            git -C "$dir" stash push -m "cc-autoupdate rescue $ts" >/dev/null 2>&1 || true
        fi
        if git -C "$dir" reset --hard --quiet "origin/$branch"; then
            log "$name: local changes blocked the update; kept on branch cc-autoupdate-rescue-$ts (+ stash) and reset to origin/$branch"
        else
            log "$name: reset to origin/$branch failed"
            return 1
        fi
    fi
    after="$(git -C "$dir" rev-parse HEAD)"
    [ "$before" != "$after" ] && log "$name: updated ${before:0:8} -> ${after:0:8}"

    applied="$(cat "$dir/.git/cc-utils-applied" 2>/dev/null || true)"
    if [ "$applied" != "$after" ]; then
        if (cd "$dir" && timeout 600 ./scripts/post-update.sh); then
            echo "$after" >"$dir/.git/cc-utils-applied"
            log "$name: post-update applied at ${after:0:8}"
        else
            log "$name: post-update FAILED at ${after:0:8}; will retry"
        fi
    fi
}

kit_version() {
    sed -n 's/^CC_KIT_VERSION=\([0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n1 | grep . || echo 0
}

newest="" newest_v="$(kit_version "$SELF")"
for dir in "$ROOT"/*/; do
    dir="${dir%/}"
    [ -d "$dir/.git" ] || continue
    if [ -x "$dir/scripts/post-update.sh" ] && [ ! -e "$dir/.no-autoupdate" ]; then
        update_one "$dir" || true
    fi
    v="$(kit_version "$dir/cc-utils/cc-utils-update.sh")"
    if [ "$v" -gt "$newest_v" ]; then
        newest="$dir/cc-utils/cc-utils-update.sh" newest_v="$v"
    fi
done

# Self-refresh, atomically: the running shell keeps reading the old inode.
if [ -n "$newest" ]; then
    tmp="$(mktemp "$SELF.XXXXXX")" \
        && cp "$newest" "$tmp" && chmod 0755 "$tmp" && mv -f "$tmp" "$SELF" \
        && log "updater refreshed to kit v$newest_v from $newest"
fi
exit 0
