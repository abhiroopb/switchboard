#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export REPO_ROOT
ROUTER_CONFIG_FILE="${ROUTER_CONFIG_FILE:-$REPO_ROOT/system/config/router.yaml}"
ROUTER_LOCK_DIR="${ROUTER_LOCK_DIR:-${TMPDIR:-/tmp}/switchboard-router-dispatch.lock}"
CMUX_BIN="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
AMP_READY_TIMEOUT="${ROUTER_AMP_READY_TIMEOUT:-25}"

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

if (( $# == 0 )) || [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: router-dispatch.sh [--origin cos|launchpad] [--workstream <slug>] [--task-id rt-...] [--request-id <source-event-id>] [--max-attempts N] [--force-redispatch] '<prompt>'"
    echo "       router-dispatch.sh [--origin cos|launchpad] --steer rt-... [--no-fallback-dispatch] '<steer text>'"
    echo "  --steer rt-x       same-task steer (correction, answer, scope change). Prints status message with the live"
    echo "                     thread id when the row is non-terminal and has a thread (send the text there by native"
    echo "                     message); dispatches a --continuation-of rt-x worker when the row is terminal: its tab is"
    echo "                     reaped within reap_grace_seconds and a message to that thread is lost;"
    echo "                     queues the steer on the task when its worker has not started (routing, queued, launching,"
    echo "                     or cmux says the recipient runs no agent that reads messages yet): status steer-queued,"
    echo "                     message 'steer queued for <workstream> worker (rt-x); delivers when it starts'. Queued"
    echo "                     steers deliver in order at the worker's ack or first heartbeat; one still queued after"
    echo "                     dispatch.steer_queue_timeout_minutes sends the origin one callback. Nothing is spawned"
    echo ". The surface never parks a steer until the owner's next message."
    echo "  --no-fallback-dispatch  with --steer: keep the old 'still starting; retry in about 30 s' message instead of"
    echo "                     queuing (for a caller that will message the thread itself)"
    echo "  --workstream slug  pin the owner (same as a 'Workstream: <slug>' first line; wins over one). Use it when the"
    echo "                     ask names a known workstream, its path, or a note in it. An unknown slug is created as a"
    echo "                     proposed new workstream"
    echo "  --parallel-worker  open a separate worker even when the owner has an idle open tab (the owner's explicit ask only)"
    echo "  --task-id          re-dispatch an existing ledger task as its next bounded attempt"
    echo "  --max-attempts N   accepted for compatibility; dispatch.py bounds attempts itself"
    echo "  --request-id       upstream source-event id; if a non-terminal task already carries it, print that task (deduplicated:true) and exit 0 without dispatching"
    echo "  --force-redispatch start a new attempt even while the current one is awaiting-ack/live inside its fence"
    echo "  --delivery direct|digest  completion policy; use digest for automated fan-out/resolver work"
    echo "  --depends-on rt-a[,rt-b]  prerequisite task id(s) (repeatable); persisted on the ledger task. While any is"
    echo "                     routing/active the Router's route is queued (status queued, depends_on [...]) instead of"
    echo "                     spawning a worker; release-queued starts it when they report done"
    echo "  --continuation-of rt-x  this dispatch continues a worker that stopped at a context limit; the route reuses"
    echo "                     that worker's terminal tab (fresh Amp thread) and inherits its declared write targets"
    exit $(( $# == 0 ? 2 : 0 ))
fi
ORIGIN="${SWITCHBOARD_DISPATCH_ORIGIN:-cos}"
REUSE_TASK_ID=""
REQUEST_ID=""
MAX_ATTEMPTS=""
FORCE_REDISPATCH=""
COMPLETION_DELIVERY=""
DEPENDS_ON=()
CONTINUATION_OF=""
STEER_TASK_ID=""
STEER_NO_FALLBACK=""
WORKSTREAM="${SWITCHBOARD_DISPATCH_WORKSTREAM:-}"
PARALLEL_WORKER=""
while (( $# > 0 )); do
    case "$1" in
        --origin) ORIGIN="${2:-}"; shift 2 ;;
        --workstream)
            WORKSTREAM="${2:-}"
            [[ "$WORKSTREAM" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "error: --workstream needs a kebab-case slug: $WORKSTREAM" >&2; exit 2; }
            shift 2 ;;
        --parallel-worker) PARALLEL_WORKER=1; shift ;;
        --steer)
            STEER_TASK_ID="${2:-}"
            [[ "$STEER_TASK_ID" =~ ^rt-[0-9a-f]{12}$ ]] || { echo "error: --steer needs a ledger task id (rt-xxxxxxxxxxxx)" >&2; exit 2; }
            shift 2 ;;
        --no-fallback-dispatch) STEER_NO_FALLBACK=1; shift ;;
        --delivery) COMPLETION_DELIVERY="${2:-}"; shift 2 ;;
        --task-id) REUSE_TASK_ID="${2:-}"; shift 2 ;;
        --request-id) REQUEST_ID="${2:-}"; shift 2 ;;
        --depends-on)
            IFS=',' read -r -a _deps <<<"${2:-}"
            for _d in "${_deps[@]}"; do
                [[ -z "$_d" ]] && continue
                [[ "$_d" =~ ^rt-[0-9a-f]{12}$ ]] || { echo "error: --depends-on needs ledger task ids (rt-xxxxxxxxxxxx): $_d" >&2; exit 2; }
                DEPENDS_ON+=("$_d")
            done
            shift 2 ;;
        --continuation-of)
            CONTINUATION_OF="${2:-}"
            [[ "$CONTINUATION_OF" =~ ^rt-[0-9a-f]{12}$ ]] || { echo "error: --continuation-of needs a ledger task id (rt-xxxxxxxxxxxx)" >&2; exit 2; }
            shift 2 ;;
        --max-attempts) MAX_ATTEMPTS="${2:-}"; [[ "$MAX_ATTEMPTS" =~ ^[0-9]+$ ]] || { echo "error: --max-attempts needs an integer" >&2; exit 2; }; shift 2 ;;
        --force-redispatch) FORCE_REDISPATCH=1; shift ;;
        --) shift; break ;;
        --*) echo "error: unknown option: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done
ORIGIN="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" normalize "$ORIGIN")"
(( $# > 0 )) || { echo "error: prompt is required" >&2; exit 2; }
PROMPT="$*"
[[ -n "${PROMPT//[[:space:]]/}" ]] || { echo "error: prompt is required" >&2; exit 2; }
case "$COMPLETION_DELIVERY" in
    direct|digest|'') ;;
    *) echo "error: --delivery must be direct or digest" >&2; exit 2 ;;
esac

# --- Same-task steer -------------------------------------------------------
# A steer is never new work, so it skips the closeout guard and dedupe below.
# dispatch.py decides: a non-terminal row prints {"status":"message",
# "thread_id":...} and the surface sends the text to that thread by native
# message; a terminal row (done/failed/blocked/cancelled) is reaped within
# reap_grace_seconds and a message to its thread has no executor, so dispatch.py
# starts a --continuation-of worker instead.
# Not-ready target: a non-terminal row with no thread yet (async launch
# window, queued) gets its steer queued on the row (status steer-queued,
# steer.action queue), so one task never spawns several supplemental workers.
# Queued steers deliver in order when the worker acks or heartbeats; a worker
# that never comes up sends the origin one callback. --no-fallback-dispatch
# keeps the old "still starting; retry" message for a caller that will message
# the thread itself.
if [[ -n "$STEER_TASK_ID" ]]; then
    rc=0
    steer_args=(steer --origin "$ORIGIN" --task-id "$STEER_TASK_ID")
    [[ -n "$STEER_NO_FALLBACK" ]] && steer_args+=(--no-fallback-dispatch)
    result="$(python3 "$REPO_ROOT/system/scripts/dispatch.py" "${steer_args[@]}" -- "$PROMPT")" || rc=$?
    printf '%s\n' "$result"
    bash "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" >/dev/null 2>&1 || true
    exit "$rc"
fi
# The interactive dispatch boundary represents the owner's asks, so every one goes
# direct. Startup fan-out bypasses it; automation using it must pass --delivery
# digest. Task retries keep their stored policy. Start of day is an owner ask too,
# so its report reaches CoS directly instead of waiting for the digest.
if [[ -z "$COMPLETION_DELIVERY" && -z "$REUSE_TASK_ID" ]]; then
    COMPLETION_DELIVERY="direct"
fi

# End-of-day guard: an actual full-closeout request is a CoS
# closing-day action, never a Router task. Refuse it BEFORE dedupe, lock,
# ledger, Router selection, or any send, and return the same guidance that
# command-center-action.sh close-all gives. Aliases: "close" with no target,
# "close everything/all/out", "close of day", "end of (the) day", "EOD",
# "wrap up the day", "shut everything down", "call it a day", "sign off".
#
# Decision rule: the request is a full
# closeout when, after stripping politeness lead-ins ("can you", "please",
# "let's" ...), it LEADS with a closeout phrase and the remainder is empty, a
# tail ("now", "please", "for today"), or a condition/continuation clause
# ("after tests finish", ", then ...", "and ..."). Length and code words are
# not used: a long genuine closeout is still refused, and "close everything
# after tests finish" is refused. A remainder that names something else
# ("close workspace 4", "eod guard should refuse ...", "shut down the watcher",
# "close out the thread status task") is a targeted or about-EOD request and
# is routed normally, as is any prompt that does not lead with the intent
# ("implement the EOD guard", "review the close everything path"). Exit 4 is
# reserved for this refusal; 3 stays capacity-reached.
CLOSEOUT_REFUSED_EXIT=4
CLOSEOUT_LEAD_PHRASES=(
    "please" "pls" "ok" "okay" "now" "let's" "lets" "go ahead and" "go ahead"
    "can you" "could you" "would you" "will you" "hey" "hi" "so" "yes" "yeah" "just"
    "i want to" "i'd like to" "i would like to" "time to" "we should" "you can" "you should"
)
CLOSEOUT_CORE_PHRASES=(
    "eod" "end of day" "end of the day" "end the day" "end my day" "end day"
    "close of day" "close of the day" "closing day" "close the day" "close for the day" "close for today"
    "close it all" "close all" "close everything" "close every thing" "close out" "close up" "close down" "close it" "close"
    "wrap it up" "wrap up" "wrap up the day" "wrap up for the day" "wrap it up for the day" "wrap the day" "wrap the day up"
    "shut it down" "shut everything down" "shut it all down" "shut all down" "shut down"
    "call it a day" "call it a night" "done for the day" "done for today" "i'm done for the day" "i'm done for today" "i'm done"
    "end session" "end my session" "end the session" "finish the day" "finish up for the day" "finish for the day"
    "sign off" "signing off" "log off" "logging off" "that's it for today" "that's all for today" "that is it for today"
)
# First word of a remainder that keeps the request a closeout (a condition,
# tail, or continuation), rather than naming a different target/subject.
CLOSEOUT_CONDITION_WORDS=(
    after once when whenever before then and but at in by so since because if until unless
    now please pls today tonight for asap immediately first too also as
    "i'm" i we "we're" you
)
is_full_closeout_request() {
    local text stripped alt best="" rest first
    text="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -s '[:space:]' ' ')"
    text="${text# }"; text="${text% }"
    # Trailing sentence punctuation never changes intent ("close everything?").
    while [[ -n "$text" && "$text" =~ [.!?[:space:]]$ ]]; do text="${text%?}"; done
    # Strip politeness/lead-in phrases, longest first, repeatedly.
    while :; do
        stripped=""
        for alt in "${CLOSEOUT_LEAD_PHRASES[@]}"; do
            if [[ "$text" == "$alt "* || "$text" == "$alt, "* ]] && (( ${#alt} > ${#stripped} )); then stripped="$alt"; fi
        done
        [[ -n "$stripped" ]] || break
        text="${text#"$stripped"}"; text="${text#,}"; text="${text# }"
    done
    [[ -n "$text" ]] || return 1
    # Leading closeout intent: the longest core phrase that is the whole text or
    # is followed by a word/punctuation boundary. Explicit longest-match avoids
    # POSIX alternation ambiguity ("close" vs "close everything").
    for alt in "${CLOSEOUT_CORE_PHRASES[@]}"; do
        if [[ "$text" == "$alt" || "$text" == "$alt "* || "$text" == "$alt,"* || "$text" == "$alt;"* || "$text" == "$alt:"* || "$text" == "$alt -"* || "$text" == "$alt —"* ]] \
            && (( ${#alt} > ${#best} )); then best="$alt"; fi
    done
    [[ -n "$best" ]] || return 1
    rest="${text#"$best"}"
    rest="${rest# }"
    # Bare closeout → refuse.
    [[ -n "$rest" ]] || return 0
    # Continuation punctuation (", then save notes" / "- I'm done") → still a closeout.
    [[ "$rest" =~ ^[,\;—-] ]] && return 0
    # Label colon: "eod: implement guard" is about EOD (routed); "eod: close
    # everything and save notes" leads with intent again (refused).
    if [[ "$rest" == :* ]]; then
        rest="${rest#:}"; rest="${rest# }"
        [[ -n "$rest" ]] || return 0
        is_full_closeout_request "$rest"; return $?
    fi
    first="${rest%% *}"
    first="${first%%[.,;:!?]*}"
    for alt in "${CLOSEOUT_CONDITION_WORDS[@]}"; do
        [[ "$first" == "$alt" ]] && return 0
    done
    # Remainder names a target or subject → targeted/about-EOD, route normally.
    return 1
}
if is_full_closeout_request "$PROMPT"; then
    python3 - "$ORIGIN" "$PROMPT" <<'PY'
import json, sys
print(json.dumps({
    "status": "closeout-refused",
    "owner": "cos",
    "skill": "closing-day",
    "origin": sys.argv[1],
    "prompt": sys.argv[2],
    "instruction": "Run closing-day directly in CoS. Save and re-read the daily note and per-workstream EOD context. Print the summary, then call command-center-action.sh close-all --closeout-saved. Do not create a Router or a task for a full closeout.",
}, ensure_ascii=False))
PY
    echo "error: full closeout request refused by router-dispatch; use CoS closing-day (no Router)" >&2
    exit "$CLOSEOUT_REFUSED_EXIT"
fi

# Source-event dedupe runs BEFORE any Router selection, creation, or boot
# (busy-owner contract). The same upstream request (Slack event, CoS
# ask, palette command) that already has a non-terminal task is returned as-is;

# --- Dispatch ---------------------------------------------------------------
# No Router Amp per task. dispatch.py picks the
# workstream, creates a git worktree, opens one cmux worker tab and writes the
# ledger row. This shim keeps the old CLI and result line so Launchpad and CoS
# need no change. Result statuses: routed | queued | duplicate | already-live |
# failed. Exit 0 for routed/queued/duplicate/already-live, 1 for failed.
#
# One-call ack (CoS latency): with router.yaml
# dispatch.async_launch true (the default) dispatch.py writes the ledger row,
# spawns `dispatch.py launch --task-id <rt-id>` detached and returns in about
# a second with `[rt-x slug] accepted; worker starting`; the worktree and tab
# (16 to 30 s) are built in the background and a launch failure reaches the
# origin as one failed line through the usual callback. The status publish
# below is detached too (it costs ~0.5 to 1 s and the launcher republishes).
# `SWITCHBOARD_DISPATCH_SYNC=1` (or `dispatch.py dispatch --sync`) restores the old
# inline launch, which is what the test suites use.
dispatch_args=(dispatch --origin "$ORIGIN")
[[ -n "$REUSE_TASK_ID" ]] && dispatch_args+=(--task-id "$REUSE_TASK_ID")
[[ -n "$REQUEST_ID" ]] && dispatch_args+=(--request-id "$REQUEST_ID")
[[ -n "$COMPLETION_DELIVERY" ]] && dispatch_args+=(--delivery "$COMPLETION_DELIVERY")
[[ -n "$CONTINUATION_OF" ]] && dispatch_args+=(--continuation-of "$CONTINUATION_OF")
[[ -n "$FORCE_REDISPATCH" ]] && dispatch_args+=(--force-redispatch)
for _d in ${DEPENDS_ON[@]+"${DEPENDS_ON[@]}"}; do dispatch_args+=(--depends-on "$_d"); done
[[ -n "$WORKSTREAM" ]] && dispatch_args+=(--workstream "$WORKSTREAM")
[[ -n "$PARALLEL_WORKER" ]] && dispatch_args+=(--parallel-worker)

rc=0
result="$(python3 "$REPO_ROOT/system/scripts/dispatch.py" "${dispatch_args[@]}" -- "$PROMPT")" || rc=$?
printf '%s\n' "$result"
if [[ "${SWITCHBOARD_DISPATCH_SYNC:-}" == "1" ]]; then
    bash "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" >/dev/null 2>&1 || true
else
    # Detached with a hidden console on Windows (no_window.py), so nothing it runs opens a window.
    python3 "$REPO_ROOT/system/scripts/no_window.py" spawn -- \
        bash "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" >/dev/null 2>&1 </dev/null || true
fi
exit "$rc"
