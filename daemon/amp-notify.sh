#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin:$PATH"
export HOME="${HOME:-{{HOME}}}"

PM_OS_DIR="${PM_OS_ROOT:-$HOME/Development/pm-os}"
STATE_DIR="$PM_OS_DIR/.state"
QUEUE_FILE="$STATE_DIR/pending-amp-notifications.jsonl"
LOCK_DIR="$STATE_DIR/pending-amp-notifications.lock"
LOG_FILE="$STATE_DIR/daemon.log"
MEGADESK_EVENTS="$PM_OS_DIR/system/state/events.jsonl"
# Passive per-target notification inbox (automation-routing policy):
# flush appends entries here and logs a passive line on the target workspace's
# chrome instead of injecting a prompt/user turn into its Amp session.
INBOX_DIR="${AMP_NOTIFY_INBOX_DIR:-$PM_OS_DIR/outputs/automation-inbox}"
CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

# shellcheck source=../system/cmux-helpers.sh
source "$PM_OS_DIR/system/cmux-helpers.sh"

mkdir -p "$STATE_DIR"
touch "$QUEUE_FILE"
touch "$LOG_FILE"

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [amp-notify] $*" >> "$LOG_FILE"
}

acquire_lock() {
  local attempts=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 20 ]; then
      log "Failed to acquire notification lock"
      return 1
    fi
    sleep 0.1
  done
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
}

# Resolve a notification target to a workspace ref for PASSIVE chrome only
# (a cmux log line — never an injected prompt/agent turn; automation-routing
# policy). "meetings"/"" → Meetings workspace; any other
# target is an ERE matched against workspace titles (e.g. "Beta & UXR").
# No idle check needed: workspace logs are safe while an agent is running.
resolve_target_workspace() {
  local target="${1:-meetings}"
  local listing
  listing=$(cmux_cmd list-workspaces 2>/dev/null || true)

  case "$target" in
    meetings|"")
      echo "$listing" | grep -iE 'Meetings|📅 Meetings' | grep -oE 'workspace:[0-9]+' | head -1 || true
      ;;
    *)
      echo "$listing" | grep -iE "$target" | grep -oE 'workspace:[0-9]+' | head -1 || true
      ;;
  esac
}

append_megadesk_event() {
  local level="$1"
  local event_summary="$2"
  local message="$3"
  local line

  mkdir -p "$(dirname "$MEGADESK_EVENTS")"
  line=$(jq -nc \
    --arg timestamp "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg level "$level" \
    --arg event_summary "$event_summary" \
    --arg message "$message" \
    '{timestamp: $timestamp, source: "collector:amp-notify", type: "notification", level: $level, event_summary: $event_summary, message: $message}')
  # Locked append coordinates with the rotate-logs events trim
  # (system/scripts/events_stream.py). Degraded direct-append fallback: only
  # reachable when the locked helper itself cannot run (python3 or the script
  # unavailable) — and since the ONLY trimmer is that same helper, no trim can
  # be racing this append. The line is jq-built, so it is always valid JSON.
  local append_err
  if ! append_err=$(printf '%s\n' "$line" \
      | python3 "$PM_OS_DIR/system/scripts/events_stream.py" append \
          --state-dir "$(dirname "$MEGADESK_EVENTS")" 2>&1); then
    log "events_stream append failed (${append_err:-unknown error}); using direct append fallback"
    printf '%s\n' "$line" >> "$MEGADESK_EVENTS"
  fi
}

enqueue_notification() {
  local level="$1"
  local event_summary="$2"
  local message="$3"
  local target="${4:-meetings}"

  acquire_lock || return 1

  # Stable per-notification id so inbox appends stay idempotent across
  # repeated flush attempts.
  local created_at nid
  created_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  nid=$(printf '%s|%s|%s|%s|%s' "$created_at" "$level" "$event_summary" "$message" "$target" \
    | shasum | awk '{print $1}' | head -c 16)

  jq -nc \
    --arg id "$nid" \
    --arg created_at "$created_at" \
    --arg level "$level" \
    --arg event_summary "$event_summary" \
    --arg message "$message" \
    --arg target "$target" \
    '{id: $id, created_at: $created_at, level: $level, event_summary: $event_summary, message: $message, target: $target}' >> "$QUEUE_FILE"

  log "Queued Amp notification for '$event_summary' ($level)"
  append_megadesk_event "$level" "$event_summary" "$message"
  bash "$PM_OS_DIR/system/scripts/publish-megadesk-status.sh" >/dev/null 2>&1 || true
}

# Flush queued notifications PASSIVELY: append each to the per-target inbox
# file under outputs/automation-inbox/ (idempotent by notification id) and put
# a passive log line on the target workspace's chrome when it is open. Never
# injects a prompt or creates an agent turn (automation-routing policy). Delivery is local-file based, so nothing stays queued waiting for
# a workspace to be open/idle.
flush_notifications() {
  acquire_lock || return 1

  if [ ! -s "$QUEUE_FILE" ]; then
    return 0
  fi

  mkdir -p "$INBOX_DIR"

  # Append every queued item to its target inbox and rewrite the queue with
  # only the rows that failed to persist (should be none). Prints one summary
  # line per target: "<target>\t<inbox_file>\t<count>" for passive chrome.
  local summary
  summary=$(python3 - "$QUEUE_FILE" "$INBOX_DIR" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

queue_path = Path(sys.argv[1])
inbox_dir = Path(sys.argv[2])

levels = {"success": "completed", "warning": "needs attention", "error": "failed"}
kept = []
per_target = {}

for line in queue_path.read_text().splitlines():
    stripped = line.strip()
    if not stripped:
        continue
    try:
        item = json.loads(stripped)
    except json.JSONDecodeError:
        continue

    target = item.get("target") or "meetings"
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", target.lower())).strip("-") or "misc"
    inbox = inbox_dir / f"{slug}.md"

    nid = item.get("id") or hashlib.sha1(stripped.encode()).hexdigest()[:16]
    created = item.get("created_at") or ""
    label = levels.get(item.get("level", "success"), item.get("level") or "update")
    event = item.get("event_summary") or "Update"
    message = item.get("message") or "No details"

    try:
        existing = inbox.read_text() if inbox.exists() else ""
        if f"id: {nid}" not in existing:
            entry = (
                f"## {created} — {event}\n"
                f"- id: {nid}\n"
                f"- status: {label}\n"
                f"- target: {target}\n"
                f"- source: daemon/amp-notify.sh (background automation)\n\n"
                f"{message}\n\n"
            )
            with inbox.open("a") as fh:
                fh.write(entry)
        per_target.setdefault(target, [str(inbox), 0])
        per_target[target][1] += 1
    except OSError:
        kept.append(stripped)

tmp = queue_path.with_suffix(".tmp")
tmp.write_text(("\n".join(kept) + "\n") if kept else "")
os.replace(tmp, queue_path)

for target, (inbox, count) in per_target.items():
    print(f"{target}\t{inbox}\t{count}")
PY
) || {
    log "Notification inbox flush failed; queue untouched"
    return 1
  }

  [ -z "$summary" ] && return 0

  # Passive destination chrome: a log line on the target workspace if open.
  local target inbox count ws_ref
  while IFS=$'\t' read -r target inbox count; do
    [ -z "$target" ] && continue
    log "Delivered $count notification(s) for target '$target' to $inbox"
    ws_ref=$(resolve_target_workspace "$target" || true)
    if [ -n "$ws_ref" ]; then
      "$CMUX" log --source amp-notify --workspace "$ws_ref" \
        "$count background update(s) ready — $inbox" >/dev/null 2>&1 || true
    fi
  done <<< "$summary"

  bash "$PM_OS_DIR/system/scripts/publish-megadesk-status.sh" >/dev/null 2>&1 || true
}

case "${1:-}" in
  enqueue)
    enqueue_notification "${2:-success}" "${3:-Meeting update}" "${4:-}" "${5:-meetings}"
    ;;
  flush)
    flush_notifications
    ;;
  *)
    echo "Usage: $0 enqueue <level> <event-summary> <message> | flush" >&2
    exit 1
    ;;
esac
