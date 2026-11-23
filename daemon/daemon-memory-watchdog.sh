#!/usr/bin/env bash
# Daemon memory watchdog — kills runaway switchboard daemon processes before they OOM macOS.
#
# Why: macOS does not enforce ulimit -v/-d/-m for user processes, so a bash
# `$(...)` capture of an amp/gdrive/etc. command that streams unbounded output
# will grow until the box swaps. This script is the per-RSS enforcer that
# substitutes for setrlimit on macOS.
#
# Strategy: scan bash processes whose argv contains "Development/switchboard/daemon/"
# or whose parent is atrun. If RSS > THRESHOLD_KB, SIGKILL and log.
#
# Runs every 5 minutes via com.switchboard.daemon-memory-watchdog.plist.

set -euo pipefail
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/bin"

THRESHOLD_KB="${SWITCHBOARD_WATCHDOG_THRESHOLD_KB:-1048576}"   # 1 GB
SWITCHBOARD_DIR="${SWITCHBOARD_ROOT:-$HOME/Development/switchboard}"
LOG_FILE="$SWITCHBOARD_DIR/.state/daemon-memory-watchdog.log"
SLACK_WEBHOOK_LIB="$SWITCHBOARD_DIR/daemon/slack-webhook.sh"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"
}

# Notify Slack only on actual kill events (silent in the happy path).
notify_slack() {
  local msg="$1"
  if [ -f "$SLACK_WEBHOOK_LIB" ]; then
    # shellcheck disable=SC1090
    source "$SLACK_WEBHOOK_LIB"
    if command -v slack_webhook_post_text >/dev/null 2>&1; then
      slack_webhook_post_text ":rotating_light: daemon-memory-watchdog: $msg" >/dev/null 2>&1 || true
      return 0
    fi
  fi
  # Fallback: macOS notification banner.
  osascript -e "display notification \"$msg\" with title \"switchboard watchdog\"" 2>/dev/null || true
}

killed_any=0

# Build a candidate list: any shell whose argv references a daemon script OR
# whose parent process is atrun. `at` jobs can show up as plain `sh`/`bash`
# with the real daemon command hidden in the job file, so parent=atrun is the
# important match for the runaway-meeting-job case.
atrun_pids=$(pgrep -x atrun 2>/dev/null || true)

while IFS= read -r line; do
  [ -z "$line" ] && continue
  pid=$(echo "$line" | awk '{print $1}')
  ppid=$(echo "$line" | awk '{print $2}')
  rss_kb=$(echo "$line" | awk '{print $3}')
  argv=$(echo "$line" | cut -d' ' -f4-)

  # Match condition: argv references a switchboard daemon script, OR parent is atrun
  is_daemon=0
  case "$argv" in
    *"Development/switchboard/daemon/"*) is_daemon=1 ;;
  esac
  if [ "$is_daemon" = "0" ] && [ -n "$atrun_pids" ]; then
    for ap in $atrun_pids; do
      [ "$ppid" = "$ap" ] && is_daemon=1 && break
    done
  fi
  [ "$is_daemon" = "0" ] && continue

  if [ "$rss_kb" -gt "$THRESHOLD_KB" ]; then
    rss_gb=$(awk -v k="$rss_kb" 'BEGIN { printf "%.2f", k/1024/1024 }')
    threshold_gb=$(awk -v k="$THRESHOLD_KB" 'BEGIN { printf "%.2f", k/1024/1024 }')
    log "KILL pid=$pid ppid=$ppid rss=${rss_gb}GB threshold=${threshold_gb}GB argv=$argv"
    kill -9 "$pid" 2>/dev/null || log "  (kill -9 $pid failed; process may have exited)"
    notify_slack "Killed pid $pid (${rss_gb}GB) — daemon process exceeded ${threshold_gb}GB RSS. argv: $argv"
    killed_any=1
  fi
done < <(ps -eo pid,ppid,rss,command -ww | awk '$4 ~ /(sh|bash|zsh)$/ || /\/(sh|bash|zsh)( |$)/ {print}')

if [ "$killed_any" = "0" ]; then
  # Silent heartbeat — only writes a line every 12 ticks (≈ hourly) to avoid log bloat.
  minute=$(date +%M)
  case "$minute" in
    00|05) log "tick: no runaway daemon processes (>${THRESHOLD_KB}KB)" ;;
  esac
fi
