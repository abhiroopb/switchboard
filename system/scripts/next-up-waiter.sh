#!/usr/bin/env bash
# next-up-waiter.sh — post the deferred next-up digest the moment CoS goes idle.
#
#   next-up-waiter.sh <workspace-ref>
#
# Spawned (detached, at most one) by post-next-up-digest.sh when a working-hours
# digest could not be sent because CoS was mid-turn or held a draft. Sampling a
# short window once per loop tick never catches a CoS that streams most of the
# day idle. Direct reports have report-waiter.sh; this is the same shape for
# the digest. It polls the tab every NEXT_UP_WAITER_POLL seconds
# (15) and, once the tab is idle with an empty composer, re-runs the poster,
# which takes its own guarded idle-only path (nothing is ever pasted into a
# live composer). It exits once the poster reports anything other than a clean
# deferral (posted, unchanged, cadence-skip, delivery-unknown, outside working
# hours), or after NEXT_UP_WAITER_MAX_SECONDS (4 h), leaving the next tick to
# spawn a fresh waiter.
set -uo pipefail

DEST_REF="${1:?workspace ref}"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPTS="$REPO_ROOT/system/scripts"
STATE_DIR="${SWITCHBOARD_STATE_DIR:-$REPO_ROOT/system/state}"
POLL="${NEXT_UP_WAITER_POLL:-15}"
MAX_SECONDS="${NEXT_UP_WAITER_MAX_SECONDS:-14400}"
PIDFILE="$STATE_DIR/worker-dispatch/next-up-waiter.pid"

mkdir -p "$(dirname "$PIDFILE")"
if old="$(cat "$PIDFILE" 2>/dev/null)" && [[ -n "$old" && "$old" != "$$" ]] && kill -0 "$old" 2>/dev/null; then
    exit 0   # a digest waiter is already running
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
json_field() { python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else (str(v).lower() if isinstance(v,bool) else v))' "$1" 2>/dev/null; }
log() { printf '%s next-up waiter: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "waiting for $DEST_REF to go idle (poll ${POLL}s, max ${MAX_SECONDS}s)"
start="$(date +%s)"
while :; do
    if cig_safe "$DEST_REF"; then
        out="$(NEXT_UP_NO_WAITER=1 COS_WORKSPACE_REF="$DEST_REF" bash "$SCRIPTS/post-next-up-digest.sh" 2>&1)" || true
        line="$(printf '%s\n' "$out" | tail -n 1)"
        status="$(printf '%s' "$line" | json_field status)"
        case "$status" in
            deferred|locked|"") log "replay ${status:-unparseable} (tab became busy or poster locked); keep waiting" ;;
            *) log "replay result: $line; exiting"; exit 0 ;;
        esac
    fi
    if (( $(date +%s) - start >= MAX_SECONDS )); then
        log "gave up after ${MAX_SECONDS}s; the next loop tick spawns a fresh waiter"; exit 0
    fi
    sleep "$POLL"
done
