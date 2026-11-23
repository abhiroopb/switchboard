#!/usr/bin/env bash
# Clear cmux's automatic app-relaunch state after cmux has fully exited.
#
# This removes only the stable app's current/previous session snapshots and
# one-shot surface resume scripts. It deliberately leaves switchboard's manual
# "reopen last closed" stack intact.

set -euo pipefail

SESSION_DIR="${CMUX_SESSION_DIR:-$HOME/Library/Application Support/cmux}"
BUNDLE_ID="${CMUX_BUNDLE_ID:-com.cmuxterm.app}"
APP_EXECUTABLE="${CMUX_APP_EXECUTABLE:-/Applications/cmux.app/Contents/MacOS/cmux}"
RESUME_DIR="${CMUX_RESUME_DIR:-${TMPDIR:-/tmp}/cmux-surface-resume}"
WAIT_SECONDS=0

if [[ "${1:-}" == "--wait-seconds" ]]; then
  WAIT_SECONDS="${2:?missing wait duration}"
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--wait-seconds <seconds>]" >&2
  exit 2
fi

app_is_running() {
  pgrep -f "^${APP_EXECUTABLE}$" >/dev/null 2>&1
}

for ((i = 0; i < WAIT_SECONDS; i++)); do
  app_is_running || break
  sleep 1
done

if app_is_running; then
  echo "ERROR: cmux is still running; refusing to clear live session state" >&2
  exit 1
fi

current_session="$SESSION_DIR/session-${BUNDLE_ID}.json"
previous_session="$SESSION_DIR/session-${BUNDLE_ID}-previous.json"

rm -f "$current_session" "$previous_session"
rm -f "$RESUME_DIR"/*.zsh 2>/dev/null || true

echo "cleared cmux automatic restore state"
