#!/usr/bin/env bash
# report-waiter.sh — the delivery queue for one destination tab (CoS, Launchpad).
#
#   report-waiter.sh <task-id> <workspace-ref> <original report-to-cos.sh args...>
#
# Spawned (detached) by report-to-cos.sh when a terminal line could not be sent
# because the destination was busy or held a draft. Every waiter first saves its
# original report-to-cos.sh arguments (report_queue.py save). Then one waiter per
# destination holds that tab's queue and every later one exits at once, so many
# waiters never poll one busy CoS together. The holder settles superseded
# system notices (report_queue.py fold), then delivers every pending report for
# its origin, owner task reports first and system notices last (report_queue.py
# next), replaying each saved command, which takes the proven send path (never a
# composer paste over a draft, never another surface). While the tab cannot take a
# line it raises one wmux/cmux notification naming what waits, again only when that
# set changes, so the owner learns of it without a line typed into the turn. It exits
# when nothing is pending, or after REPORT_TO_COS_WAITER_MAX_SECONDS (6 h), leaving
# the rest for the command-center loop.
set -uo pipefail

TASK_ID="${1:?task id}"; DEST_REF="${2:?workspace ref}"; shift 2
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPTS="$REPO_ROOT/system/scripts"
STATE_DIR="${SWITCHBOARD_STATE_DIR:-$REPO_ROOT/system/state}"
export SWITCHBOARD_STATE_DIR="$STATE_DIR"
POLL="${REPORT_TO_COS_WAITER_POLL:-15}"
MAX_SECONDS="${REPORT_TO_COS_WAITER_MAX_SECONDS:-21600}"
# Windows (Git Bash): each poll forks about 15 processes. One queue per tab replaces
# one waiter per report, so a 30 s poll stays cheap and a short idle gap between
# owner turns is caught.
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        POLL="${REPORT_TO_COS_WAITER_POLL:-30}" ;;
esac
ORIGIN="cos"
prev=""
for arg in "$@"; do
    [[ "$prev" == "--origin" ]] && ORIGIN="$arg"
    prev="$arg"
done
log() { printf '%s waiter %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TASK_ID" "$*"; }

python3 "$SCRIPTS/report_queue.py" save --task-id "$TASK_ID" -- "$@" >/dev/null 2>&1 \
    || log "could not save the report arguments; the queue rebuilds them from the ledger"
# Two waiters that start in the same instant can both take the queue. Each replay still
# runs under report-to-cos.sh's notification lock and its seen check, so a line is
# never sent twice; the pair only polls double until one empties the queue.

mkdir -p "$STATE_DIR/worker-dispatch"
QUEUE_PIDFILE="$STATE_DIR/worker-dispatch/report-queue-${DEST_REF//[^A-Za-z0-9]/_}.pid"
if old="$(cat "$QUEUE_PIDFILE" 2>/dev/null)" && [[ -n "$old" && "$old" != "$$" ]] && kill -0 "$old" 2>/dev/null; then
    log "queued: the $DEST_REF queue (pid $old) delivers it"
    exit 0
fi
echo $$ > "$QUEUE_PIDFILE"
PIDFILE="$STATE_DIR/worker-dispatch/report-waiter-$TASK_ID.pid"
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; [[ "$(cat "$QUEUE_PIDFILE" 2>/dev/null)" == "$$" ]] && rm -f "$QUEUE_PIDFILE"' EXIT

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
json_field() { python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else (str(v).lower() if isinstance(v,bool) else v))' "$1" 2>/dev/null; }
log() { printf '%s waiter %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TASK_ID" "$*"; }

# dest_ready: may the replay send now? A Claude tab on wmux (the shim reports its
# composer) takes a typed line mid-turn and queues it, so the gate there is an
# empty composer with no dialog open, not an idle turn: a CoS the owner keeps
# busy would otherwise hold a finished report for hours. Anything else keeps the idle-only guard.
# After an unproven send (a busy Claude queued the line, no prompt-log entry
# yet) the gate also waits for the turn to end, so the queued line has been
# taken and logged before the replay's seen check decides to send it again.
# A pane with no Claude composer ("none", e.g. an Amp CoS) keeps the idle guard.
dest_ready() {
    local st verdict unproven="${1:-}"
    # macOS cmux has no `agent state` composer, so macOS keeps the idle-only guard (Mac kit v8).
    # Only Darwin skips: Git Bash and a WSL shell on Windows both talk to the wmux shim.
    case "$(uname -s 2>/dev/null)" in Darwin) cig_safe "$DEST_REF"; return ;; esac
    if st="$(cmux_cmd agent state "$DEST_REF" --json 2>/dev/null)" && [[ -n "$st" ]]; then
        verdict="$(printf '%s' "$st" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except ValueError:
    print("unknown"); raise SystemExit
c = v.get("composer")
if c not in ("claude-empty", "claude-text"):
    print("unknown")
elif c == "claude-empty" and v.get("state") != "blocked" and (not sys.argv[1] or v.get("state") == "idle"):
    print("ready")
else:
    print("wait")' "$unproven" 2>/dev/null)"
        case "$verdict" in ready) return 0 ;; wait) return 1 ;; esac
    fi
    cig_safe "$DEST_REF"
}

# resolve_dest: the origin's tab now, by the resolver report-to-cos.sh uses (the
# claim-cos registration via owner_tabs.py cos-target for CoS, else the title).
# A waiter that kept the ref it was spawned with for its whole 6 h life would,
# after the owner starts a new CoS tab, poll and notify the old tab, which never
# goes idle, and hold the queue the new CoS should get.
SOURCE_TYPE="$(python3 "$SCRIPTS/surface-registry.py" get "$ORIGIN" source_type 2>/dev/null || echo routine)"
SOURCE_KEY="$(python3 "$SCRIPTS/surface-registry.py" get "$ORIGIN" source_key 2>/dev/null || echo switchboard)"
resolve_dest() {
    local ref=""
    if [[ "$SOURCE_KEY" == "switchboard" && "${REPORT_TO_COS_NO_OWNER_TABS:-0}" != "1" ]]; then
        ref="$(python3 "$SCRIPTS/owner_tabs.py" cos-target 2>/dev/null | json_field ref)"
    fi
    [[ -z "$ref" ]] && ref="$(find_workspace_ref_for_source_key "$SOURCE_TYPE" "$SOURCE_KEY" 2>/dev/null || true)"
    printf '%s' "$ref"
}
# Each resolve forks a few processes, so a busy destination re-checks at most this often.
RESOLVE_EVERY="${REPORT_TO_COS_WAITER_RESOLVE_SECONDS:-60}"
last_resolve=0
# follow_dest: when the origin's tab moved, hand the queue to the new tab (or to the
# waiter that already holds it). Returns 0 when DEST_REF changed.
follow_dest() {
    local now_ref new_pidfile holder
    now_ref="$(resolve_dest)"
    [[ -z "$now_ref" || "$now_ref" == "$DEST_REF" ]] && return 1
    new_pidfile="$STATE_DIR/worker-dispatch/report-queue-${now_ref//[^A-Za-z0-9]/_}.pid"
    if holder="$(cat "$new_pidfile" 2>/dev/null)" && [[ -n "$holder" && "$holder" != "$$" ]] && kill -0 "$holder" 2>/dev/null; then
        log "$ORIGIN moved from $DEST_REF to $now_ref; the $now_ref queue (pid $holder) delivers; exiting"
        exit 0
    fi
    echo $$ > "$new_pidfile"
    [[ "$(cat "$QUEUE_PIDFILE" 2>/dev/null)" == "$$" ]] && rm -f "$QUEUE_PIDFILE"
    log "$ORIGIN moved from $DEST_REF to $now_ref; following it"
    DEST_REF="$now_ref"; QUEUE_PIDFILE="$new_pidfile"; notified=""
    return 0
}

log "holding the $DEST_REF queue for origin $ORIGIN (poll ${POLL}s, max ${MAX_SECONDS}s)"
fold="$(python3 "$SCRIPTS/report_queue.py" fold --origin "$ORIGIN" 2>/dev/null | json_field folded)"
[[ -n "$fold" && "$fold" != "0" ]] && log "settled $fold superseded system notice(s)"
start="$(date +%s)"
skip=(); notified=""; head_tries=0; last_head=""
while :; do
    # A sandboxed waiter outlives its test: Windows cannot delete a temp tree a
    # live process holds, so the ledger stays while the stub cmux is gone, and the
    # real shim must never take its place. Under the sandbox, no stub means stop.
    if [[ "${SWITCHBOARD_TEST_SANDBOX:-0}" == "1" && ! -f "${CMUX_BIN:-}" ]]; then
        log "sandboxed and the stub cmux is gone; exiting"; exit 0
    fi
    skip_args=()
    for s_id in ${skip[@]+"${skip[@]}"}; do skip_args+=(--skip "$s_id"); done
    head_json="$(python3 "$SCRIPTS/report_queue.py" next --origin "$ORIGIN" ${skip_args[@]+"${skip_args[@]}"} 2>/dev/null)"
    head_id="$(printf '%s' "$head_json" | json_field task_id)"
    if [[ -z "$head_id" ]]; then
        log "queue for $DEST_REF is empty; exiting"; exit 0
    fi
    [[ "$head_id" != "$last_head" ]] && { head_tries=0; last_head="$head_id"; }
    if dest_ready "$(printf '%s' "$head_json" | json_field unproven)"; then
        replay=()
        while IFS= read -r -d '' part; do replay+=("$part"); done < <(printf '%s' "$head_json" | python3 -c '
import json, sys
for a in json.load(sys.stdin).get("args") or []:
    sys.stdout.write(str(a) + "\0")')
        out="$(REPORT_TO_COS_NO_WAITER=1 bash "$SCRIPTS/report-to-cos.sh" "${replay[@]}" 2>&1)" || true
        line="$(printf '%s
' "$out" | tail -n 1)"
        delivered="$(printf '%s' "$line" | json_field delivered)"
        ignored="$(printf '%s' "$line" | json_field ignored)"
        outcome="$(printf '%s' "$line" | json_field delivery_outcome)"
        batched="$(printf '%s' "$line" | json_field batched)"
        if [[ "$delivered" == "true" || "$ignored" == "true" || "$outcome" == "unknown" ]]; then
            log "$head_id: delivered=$delivered ignored=$ignored outcome=$outcome"
            rm -f "$STATE_DIR/worker-dispatch/report-queue/$head_id.args"
            notified=""; continue
        fi
        # The replay decided the row belongs to
        # the next-up digest (stale_batched) or was settled quietly; never paste
        # it as a standalone line.
        if [[ "$batched" == "true" || "$outcome" == "quiet" ]]; then
            log "$head_id: batched=$batched outcome=$outcome; the digest carries it"
            skip+=("$head_id"); continue
        fi
        head_tries=$((head_tries + 1))
        if (( head_tries >= 3 )); then
            log "$head_id: not delivered after 3 tries while the tab was ready; left for the command-center loop"
            skip+=("$head_id"); continue
        fi
        log "$head_id: replay deferred (tab became busy); keep waiting"
    elif (( $(date +%s) - last_resolve >= RESOLVE_EVERY )) && { last_resolve="$(date +%s)"; follow_dest; }; then
        continue   # the origin's tab moved: check the new one now
    else
        waiting="$(python3 "$SCRIPTS/report_queue.py" waiting --origin "$ORIGIN" ${skip_args[@]+"${skip_args[@]}"} 2>/dev/null)"
        if [[ -n "$waiting" && "$waiting" != "$notified" ]]; then
            cmux_cmd notify --workspace "$DEST_REF" --title "switchboard" --body "$waiting" >/dev/null 2>&1                 && log "notified $DEST_REF: $waiting"
            notified="$waiting"
        fi
    fi
    if (( $(date +%s) - start >= MAX_SECONDS )); then
        log "gave up after ${MAX_SECONDS}s; the command-center loop keeps retrying"; exit 0
    fi
    sleep "$POLL"
done
