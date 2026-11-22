#!/usr/bin/env bash
# amp-mem Weekly Distillation — runs the kb-distill workflow via headless Amp
# Scheduled Sundays 09:00 local via launchd (com.pm-os.amp-mem-weekly-distill.plist)
#
# Flow:
#   1. Skip unless enough pending observations have accumulated (threshold below)
#   2. Take a WAL-safe amp-mem backup before any writes
#   3. Call headless amp ONCE with a tightly scoped kb-distill prompt
#   4. Verify the run actually recorded (distill-status pending reset); log outcome
#
# Skips (exit 0, one log line) on a machine whose memory backend is not amp-mem:
# PM_OS_MEMORY in the environment or in local/machines/<MACHINE>.env set to
# builtin or claude-mem (Claude Code machines). Unset keeps the Amp default.
# A distill-status failure is logged with its error text and exits 1, so
# set -e never kills the script on it with nothing in the log.
#
# The script never calls distill-record itself. Only the AI workflow does,
# and only after successful analysis, so a failed run stays "pending".

set -euo pipefail
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PM_OS_DIR="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$PM_OS_DIR/.state"
LOG_FILE="$STATE_DIR/amp-mem-weekly-distill.log"
LOCK_DIR="$STATE_DIR/amp-mem-weekly-distill.lock"
AMP_MEM="${AMP_MEM_BIN:-$HOME/bin/amp-mem}"
AMP="$(command -v amp || echo "$HOME/.local/bin/amp")"
AMP_HEADLESS="$AMP --settings-file $HOME/.config/amp/settings-headless.json"

# Minimum pending observations before a run is worth the tokens (env-overridable for testing).
MIN_PENDING="${MIN_PENDING:-30}"
# Hard wall-clock cap for the amp call (30 min).
AMP_TIMEOUT=1800

mkdir -p "$STATE_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# --- Atomic lock (mkdir is atomic; stale locks >2h are reclaimed) ---
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    log "Reclaiming stale lock (>2h old)"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || { log "Lock contention after reclaim; exiting"; exit 0; }
  else
    log "Another distill run is in progress; exiting"
    exit 0
  fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

log "=== Weekly distill tick ==="

# --- Memory backend: amp-mem only ---
# Same lookup as system/scripts/lib/comms-triage.sh: the environment wins, then
# local/machines/<MACHINE>.env (host name up to its first dot, upper case).
memory_backend() {
  local value="${PM_OS_MEMORY:-}" name file
  if [ -z "$value" ]; then
    name="${PM_OS_MACHINE:-${HOSTNAME:-$(hostname)}}"
    name="$(printf '%s' "${name%%.*}" | tr '[:lower:]' '[:upper:]')"
    file="${PM_OS_LOCAL_DIR:-$PM_OS_DIR/local}/machines/$name.env"
    if [ -f "$file" ]; then
      value="$(sed -n 's/^[^A-Za-z#]*PM_OS_MEMORY[[:space:]]*=\(.*\)$/\1/p' "$file" | tail -n 1)"
    fi
  fi
  printf '%s' "$value" | tr -d "\"' \t\r" | tr '[:upper:]' '[:lower:]'
}
BACKEND="$(memory_backend)"
case "${BACKEND:-amp-mem}" in
  amp-mem|amp) ;;
  *)
    log "Skipped: memory backend on this machine is '$BACKEND' (PM_OS_MEMORY), not amp-mem. Done."
    [ "${1:-}" = "--dry-run" ] && echo "dry-run ok: skipped, memory backend is $BACKEND (not amp-mem)"
    exit 0
    ;;
esac

# --- Preconditions ---
if [ ! -x "$AMP_MEM" ]; then
  log "amp-mem not found/executable at $AMP_MEM; exiting"
  exit 0
fi

# Capture the exit code and stderr: under set -e a failing pipeline here used to
# end the script before anything reached the log.
STATUS_RC=0
STATUS_OUT=$("$AMP_MEM" distill-status 2>&1) || STATUS_RC=$?
if [ "$STATUS_RC" -ne 0 ]; then
  log "ERROR: amp-mem distill-status failed (exit $STATUS_RC): $(printf '%s' "$STATUS_OUT" | tr '\r\n' '  ' | cut -c1-500)"
  log "Fix the amp-mem database or set PM_OS_MEMORY in local/machines/<MACHINE>.env if this machine does not use amp-mem. Aborting."
  echo "amp-mem-weekly-distill: distill-status failed (exit $STATUS_RC); see $LOG_FILE" >&2
  exit 1
fi
PENDING=$(printf '%s\n' "$STATUS_OUT" | awk -F': *' '/Pending observations/ {print $2}' | tr -dc '0-9')
if [ -z "$PENDING" ]; then
  log "ERROR: could not read 'Pending observations' from amp-mem distill-status output: $(printf '%s' "$STATUS_OUT" | tr '\r\n' '  ' | cut -c1-300)"
  echo "amp-mem-weekly-distill: unreadable distill-status output; see $LOG_FILE" >&2
  exit 1
fi
log "Pending observations: $PENDING (threshold: $MIN_PENDING)"

if [ "$PENDING" -lt "$MIN_PENDING" ]; then
  log "Below threshold; nothing to distill. Done."
  exit 0
fi

# --- Backup before any writes ---
"$AMP_MEM" backup >> "$LOG_FILE" 2>&1 || {
  log "Backup failed; aborting distill (will retry next week)"
  exit 1
}

# --- Dry-run mode: everything above, no amp call ---
HEADLESS_SETTINGS="$HOME/.config/amp/settings-headless.json"
if [ ! -f "$HEADLESS_SETTINGS" ]; then
  log "ERROR: $HEADLESS_SETTINGS is missing; the headless amp call cannot run. Aborting (will retry next week)."
  echo "amp-mem-weekly-distill: missing $HEADLESS_SETTINGS" >&2
  exit 1
fi

if [ "${1:-}" = "--dry-run" ]; then
  log "Dry run: would call headless amp for $PENDING pending observations. Done."
  echo "dry-run ok: pending=$PENDING (>=$MIN_PENDING), backup ok, would invoke amp"
  exit 0
fi

# --- Headless amp call ---
PROMPT="Load the kb-distill skill and execute its full distillation workflow now: \
run ~/bin/amp-mem distill-dump, cluster the pending observations, save only \
high-quality synthesized notes via 'amp-mem save distilled', and record the run \
with 'amp-mem distill-record'. Do NOT record the run if analysis or saves fail. \
Do not modify AGENTS.md or any skill files; list rule candidates in your output \
instead. Do not send messages, create calendar events, or touch anything outside \
amp-mem. Note: avoid unescaped shell quoting issues by keeping summaries free of \
double quotes."

log "Calling headless amp (cap: ${AMP_TIMEOUT}s)..."
if timeout "$AMP_TIMEOUT" $AMP_HEADLESS -x "$PROMPT" >> "$LOG_FILE" 2>&1; then
  log "Amp call completed"
else
  log "Amp call failed or timed out (exit $?)"
fi

# --- Verify: did the workflow actually record? ---
AFTER=$("$AMP_MEM" distill-status 2>/dev/null | awk -F': *' '/Pending observations/ {print $2}' | tr -dc '0-9')
AFTER=${AFTER:-unknown}
if [ "$AFTER" = "0" ]; then
  log "Verified: distillation recorded, pending reset to 0. Done."
else
  log "WARNING: pending still $AFTER after run — distillation likely incomplete; will retry next week."
fi
