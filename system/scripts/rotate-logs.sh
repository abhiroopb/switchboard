#!/usr/bin/env bash

set -euo pipefail

# shellcheck source=lib/mux.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/mux.sh"

readonly SWITCHBOARD="${SWITCHBOARD_DIR:-${SWITCHBOARD_ROOT:-$HOME/Development/switchboard}}"
readonly STATE_DIR="$SWITCHBOARD/.state"
readonly SYSTEM_LOG_DIR="$SWITCHBOARD/system/logs"
# What this script deletes under .state on purpose is declared, with every other
# scheduled pruner's paths, in system/config/state-pruned.txt: the context
# snapshots dir (newest 14 kept) and the rotated copy of each *.log (`<log>.1`,
# gzipped to `<log>.1.gz`). check-state-inventory.py reads the same file and
# leaves those paths out of its .state count. The dir and the suffix are derived
# from our rows there so the file stays the only place they are named; if the
# rows are missing or malformed we stop before touching anything.
readonly STATE_PRUNED_FILE="$SWITCHBOARD/system/config/state-pruned.txt"
snapshot_rel=''
rotated_glob=''
gz_glob=''
while IFS= read -r pruned; do
    case "$pruned" in
        */) [[ -z "$snapshot_rel" ]] || { echo "rotate-logs: more than one dir row in $STATE_PRUNED_FILE" >&2; exit 1; }
            snapshot_rel="${pruned%/}" ;;
        \*.log*.gz) gz_glob="$pruned" ;;
        \*.log*) rotated_glob="$pruned" ;;
    esac
done < <(awk -v me="rotate-logs.sh" '$1 == me {print $2}' "$STATE_PRUNED_FILE" 2>/dev/null)
if [[ -z "$snapshot_rel" || -z "$rotated_glob" || "$gz_glob" != "$rotated_glob.gz" ]]; then
    echo "rotate-logs: $STATE_PRUNED_FILE must list for rotate-logs.sh one dir row, one '*.log<suffix>' row and its '.gz' twin; nothing rotated" >&2
    exit 1
fi
readonly ROTATED_SUFFIX="${rotated_glob#\*.log}"
readonly SNAPSHOT_DIR="$STATE_DIR/$snapshot_rel"
readonly PLUGIN_LOG_DIR="$HOME/.agents/.amp-plugin-logs"
readonly PLUGIN_EVENTS="$PLUGIN_LOG_DIR/switchboard-events.jsonl"
readonly AMP_MEM_DIR="$HOME/.amp/memory"
readonly AMP_MEM_LIVE_DB="$AMP_MEM_DIR/amp-mem.db"
readonly MAX_BYTES=$((2 * 1024 * 1024))

rotated=0
snapshots_deleted=0
telemetry_deleted=0
backups_deleted=0

rotate_if_needed() {
    local log_file="$1"
    local size mode

    [[ -f "$log_file" ]] || return 0
    size=$(mux_size "$log_file")
    (( size > MAX_BYTES )) || return 0

    mode=$(mux_mode "$log_file")
    rm -f "$log_file$ROTATED_SUFFIX" "$log_file$ROTATED_SUFFIX.gz"
    mv "$log_file" "$log_file$ROTATED_SUFFIX"
    gzip -f "$log_file$ROTATED_SUFFIX"
    : > "$log_file"
    chmod "$mode" "$log_file"
    printf 'Rotated %s (%s bytes)\n' "$log_file" "$size"
    ((rotated += 1))
}

# system/state/events.jsonl is an append-only fact stream (workspace updates,
# queue refreshes, notifications) consumed by the switchboard dispatcher, so —
# unlike a plain log — recent history must stay in place after rotation.
# events_stream.py archives the full file, keeps the newest lines, and holds
# an exclusive flock the writers share, so no concurrently appended event can
# land in the loss window between reading the tail and replacing the file.
trim_events_stream() {
    local stream="$1"
    local out

    [[ -f "$stream" ]] || return 0
    if ! out=$(python3 "$SWITCHBOARD/system/scripts/events_stream.py" trim \
        --state-dir "$(dirname "$stream")" --max-bytes "$MAX_BYTES"); then
        printf 'WARNING: events stream trim failed for %s\n' "$stream" >&2
        return 0
    fi
    if [[ -n "$out" ]]; then
        printf '%s\n' "$out"
        ((rotated += 1))
    fi
}

mkdir -p "$SYSTEM_LOG_DIR"

while IFS= read -r -d '' log_file; do
    rotate_if_needed "$log_file"
done < <(find "$STATE_DIR" -maxdepth 1 -type f -name '*.log' -print0)

while IFS= read -r -d '' log_file; do
    rotate_if_needed "$log_file"
done < <(find "$SYSTEM_LOG_DIR" -maxdepth 1 -type f -name '*.log' -print0)

# launchd StandardOut/ErrorPath targets (command-center, auto-push, workspace
# reports). Those jobs are periodic, so launchd reopens the path on each run and
# a rotation between ticks loses nothing.
while IFS= read -r -d '' log_file; do
    rotate_if_needed "$log_file"
done < <(find "$SWITCHBOARD/system/state/logs" -maxdepth 1 -type f -name '*.log' -print0 2>/dev/null)

rotate_if_needed /tmp/crex-autosave.log

trim_events_stream "$SWITCHBOARD/system/state/events.jsonl"

if [[ -d "$SNAPSHOT_DIR" ]]; then
    while IFS= read -r old_snapshot; do
        [[ -n "$old_snapshot" ]] || continue
        printf 'Deleting old context snapshot %s\n' "$old_snapshot"
        rm -rf -- "$old_snapshot"
        ((snapshots_deleted += 1))
    done < <(find "$SNAPSHOT_DIR" -mindepth 1 -maxdepth 1 -print | sort -r | tail -n +15)
fi

if [[ -d "$PLUGIN_LOG_DIR" ]]; then
    rotate_if_needed "$PLUGIN_EVENTS"
    # Plugin append logs (slack-turn-bridge, slack-send, detached).
    while IFS= read -r -d '' log_file; do
        rotate_if_needed "$log_file"
    done < <(find "$PLUGIN_LOG_DIR" -maxdepth 1 -type f -name '*.log' -print0)
    while IFS= read -r -d '' old_telemetry; do
        [[ "$old_telemetry" == "$PLUGIN_EVENTS" ]] && continue
        printf 'Deleting old plugin telemetry %s\n' "$old_telemetry"
        rm -f -- "$old_telemetry"
        ((telemetry_deleted += 1))
    done < <(find "$PLUGIN_LOG_DIR" -type f -mtime +14 -print0)
fi

# ~/bin/amp-mem declares this exact live DB path.
#
# Policy: keeping only the single newest backup with no age check could
# destroy an irreplaceable archive, so the rules are:
#   - Only rotate `amp-mem.db.backup-*` files, and only when older than
#     14 days (amp-mem itself already rotates these at 7 days; this is a
#     backstop, not the primary policy).
#   - The newest backup is always kept regardless of age.
#   - Archival snapshots (pre-cleanup-*, pre-compaction-*, *.pre-cleanup-copy)
#     are NEVER deleted here. Removing a deliberate one-off archive is a
#     manual decision, not log rotation.
if [[ -f "$AMP_MEM_LIVE_DB" && -d "$AMP_MEM_DIR" ]]; then
    newest_backup=''
    while IFS= read -r backup; do
        [[ -n "$backup" ]] || continue
        if [[ -z "$newest_backup" || "$backup" -nt "$newest_backup" ]]; then
            newest_backup="$backup"
        fi
    done < <(find "$AMP_MEM_DIR" -maxdepth 1 -type f \
        -name 'amp-mem.db.backup-*' \
        ! -name '*-wal' ! -name '*-shm' -print)

    while IFS= read -r backup; do
        [[ -n "$backup" ]] || continue
        [[ "$backup" == "$newest_backup" ]] && continue
        printf 'Deleting old amp-mem backup %s\n' "$backup"
        rm -f -- "$backup"
        ((backups_deleted += 1))
    done < <(find "$AMP_MEM_DIR" -maxdepth 1 -type f \
        -name 'amp-mem.db.backup-*' \
        ! -name '*-wal' ! -name '*-shm' -mtime +14 -print)
fi

summary="$(date '+%Y-%m-%d %H:%M:%S') rotated=$rotated snapshots_deleted=$snapshots_deleted telemetry_deleted=$telemetry_deleted backups_deleted=$backups_deleted"
printf '%s\n' "$summary" | tee -a "$SYSTEM_LOG_DIR/rotate-logs.log"
