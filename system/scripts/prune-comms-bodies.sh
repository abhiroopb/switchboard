#!/usr/bin/env bash
# Nightly cleanup for .state/comms-bodies/ side files written by daemon/comms-prompt.sh.
#
# Each body file is keyed on the sha hash of a Slack/Gmail permalink and lives
# longer than the 4h TTL on comms-prompt-recent.json. After ~7 days nothing
# references them anymore. This script deletes orphans older than the
# configured window.
#
# Defaults to dry-run; pass --apply to actually delete.
# Usage:
#   bash system/scripts/prune-comms-bodies.sh           # dry-run, 14 days
#   bash system/scripts/prune-comms-bodies.sh --apply   # actually delete
#   bash system/scripts/prune-comms-bodies.sh --apply --days 7
set -euo pipefail

PM_OS_DIR="${PM_OS_DIR:-${PM_OS_ROOT:-$HOME/Development/pm-os}}"
LOG="$PM_OS_DIR/system/logs/prune-comms-bodies.log"

DAYS=14
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift;;
    --days) DAYS="$2"; shift 2;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1;;
  esac
done

# The dir we prune is declared in system/config/state-pruned.txt together with
# every other scheduled deleter under .state; check-state-inventory.py reads the
# same file and leaves it out of its .state count, so a prune never trips
# "state inventory shrank". No row, no prune.
STATE_PRUNED_FILE="$PM_OS_DIR/system/config/state-pruned.txt"
declared=$(awk -v me="prune-comms-bodies.sh" '$1 == me && $2 ~ /\/$/ {print $2}' "$STATE_PRUNED_FILE" 2>/dev/null || true)
if [ "$(printf '%s' "$declared" | grep -c .)" -ne 1 ]; then
  echo "prune-comms-bodies: $STATE_PRUNED_FILE must list exactly one dir row for prune-comms-bodies.sh; nothing pruned" >&2
  exit 1
fi
BODIES_DIR="${COMMS_BODIES_DIR:-$PM_OS_DIR/.state/${declared%/}}"

mkdir -p "$(dirname "$LOG")"
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if [ ! -d "$BODIES_DIR" ]; then
  echo "[$(ts)] prune-comms-bodies: dir missing, nothing to do ($BODIES_DIR)" >> "$LOG"
  exit 0
fi

# Find orphan body files older than N days.
candidates=$(find "$BODIES_DIR" -maxdepth 1 -type f -name '*.txt' -mtime "+$DAYS" 2>/dev/null || true)
count=$(echo -n "$candidates" | grep -c . || true)

if [ "$count" -eq 0 ]; then
  echo "[$(ts)] prune-comms-bodies: 0 files older than ${DAYS}d" >> "$LOG"
  echo "Nothing to prune."
  exit 0
fi

if [ "$APPLY" -eq 1 ]; then
  # NUL-separated: plain `xargs` treats a backslash as an escape, so a Windows path
  # (C:\Users\...) lost its separators and `rm -f` silently deleted nothing.
  find "$BODIES_DIR" -maxdepth 1 -type f -name '*.txt' -mtime "+$DAYS" -print0 2>/dev/null | xargs -0 rm -f
  echo "[$(ts)] prune-comms-bodies: deleted $count files older than ${DAYS}d" >> "$LOG"
  echo "Deleted $count files."
else
  echo "[$(ts)] prune-comms-bodies: DRY-RUN would delete $count files older than ${DAYS}d" >> "$LOG"
  echo "DRY-RUN: would delete $count files. Re-run with --apply to delete."
  echo "$candidates" | head -10
  if [ "$count" -gt 10 ]; then
    echo "(+$((count - 10)) more)"
  fi
fi
