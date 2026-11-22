#!/usr/bin/env bash
# Rebuild the CoS Review feed (system/state/cos-review-feed.json) once, under a
# lock, optionally throttled. Two callers share it, so the feed stays within
# 5 minutes of current:
#   command-center-loop.sh   every tick (10-15 min), unthrottled
#   run-comms-fastpath.sh    every 60 s with --min-age 300, so the feed is at
#                            most five minutes old inside the fastpath hours
# Read only apart from the feed file; the builder does at most a few bounded
# reply_check reads (COS_REVIEW_REPLY_BUDGET). Kill switch:
# COS_REVIEW_FEED_DISABLED=1. Never fails the caller.
#
# Usage: cos-review-feed-rebuild.sh [--min-age SECONDS] [--reason LABEL]
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# shellcheck source=lib/mux.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/mux.sh"
STATE_DIR="${STATE_DIR:-$REPO_ROOT/system/state}"
FEED_FILE="${COS_REVIEW_FEED_FILE:-$STATE_DIR/cos-review-feed.json}"
LOG_DIR="$STATE_DIR/worker-dispatch"
LOG="$LOG_DIR/cos-review-feed.log"
LOCK_DIR="${COS_REVIEW_FEED_LOCK:-$STATE_DIR/worker-dispatch/cos-review-feed.lock}"

MIN_AGE=0
REASON="loop"
while [ $# -gt 0 ]; do
  case "$1" in
    --min-age) MIN_AGE="${2:-0}"; shift 2 ;;
    --reason) REASON="${2:-loop}"; shift 2 ;;
    *) shift ;;
  esac
done

[ "${COS_REVIEW_FEED_DISABLED:-0}" = "1" ] && exit 0
[ -f "$REPO_ROOT/system/scripts/cos_review_feed.py" ] || exit 0
mkdir -p "$LOG_DIR"

now_epoch=$(date +%s)
if [ "$MIN_AGE" -gt 0 ] && [ -f "$FEED_FILE" ]; then
  feed_mtime=$(mux_mtime "$FEED_FILE" || echo 0)
  if [ $(( now_epoch - feed_mtime )) -lt "$MIN_AGE" ]; then
    exit 0
  fi
fi

# mkdir is the atomic lock on macOS (no flock). A lock older than 5 min is a
# crashed builder; reclaim it.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_age=$(( now_epoch - $(mux_mtime "$LOCK_DIR" || echo 0) ))
  if [ "$lock_age" -gt 300 ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

{
  printf '%s [%s] ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REASON"
  RUNNING_TASKS_FILE="${RUNNING_TASKS_FILE:-$STATE_DIR/running-tasks.json}" \
  COS_REVIEW_FEED_FILE="$FEED_FILE" \
  COS_REVIEW_ACTIONS_FILE="${COS_REVIEW_ACTIONS_FILE:-$STATE_DIR/cos-review-actions.json}" \
  python3 "$REPO_ROOT/system/scripts/cos_review_feed.py" 2>&1 || true
} >>"$LOG"
exit 0
