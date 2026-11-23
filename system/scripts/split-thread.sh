#!/usr/bin/env bash
# split-thread.sh — thread split via `amp threads handoff` (v2 hybrid).
#
# Diffs from v1:
#   * Uses `amp threads handoff` to create the new thread server-side. The
#     parent thread stays linked (so @@-style search across the full
#     non-compacted parent works) and the recall pack + prompt become the
#     first user turn of the new thread, all in one server round-trip.
#   * Opens a new cmux workspace that runs `amp threads continue <NEW_ID>`
#     instead of seeding via `cmux send` after a sleep.
#
# Usage:
#   bash system/scripts/split-thread.sh "<short task>" "<continuation prompt>" [--dry-run]
#       [--parent <T-id>] [--name <workspace title>] [--cwd <dir>] [--mode <agent mode>]
#       [--workstream <slug>]
#
#   --parent      parent thread id (default: $AMP_CURRENT_THREAD_ID)
#   --name        cmux workspace title (default: the short task)
#   --cwd         working directory of the new workspace (default: repo root)
#   --mode        agent mode for `amp threads continue -m` (default: Amp's own)
#   --workstream  passed to generate-recall-pack.py --workstream
#
# Only run on explicit user request, or from rotate-cos-thread.sh at start of
# day. Never call on agent initiative otherwise.

set -euo pipefail

# Derive the repo root from the cwd first (worktree-safe), then from this
# file's location; never from an absolute ~/Development/switchboard path.
if [[ -z "${REPO_ROOT:-}" ]]; then
    REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/system/scripts/generate-recall-pack.py" ]] \
        || REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
RECALL_SCRIPT="$REPO_ROOT/system/scripts/generate-recall-pack.py"

DRY_RUN=0
ARGS=()
OPT_PARENT=""
OPT_NAME=""
OPT_CWD=""
OPT_MODE=""
OPT_WORKSTREAM=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --parent) OPT_PARENT="${2:-}"; shift 2 ;;
        --name) OPT_NAME="${2:-}"; shift 2 ;;
        --cwd) OPT_CWD="${2:-}"; shift 2 ;;
        --mode) OPT_MODE="${2:-}"; shift 2 ;;
        --workstream) OPT_WORKSTREAM="${2:-}"; shift 2 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

if [[ ${#ARGS[@]} -lt 2 ]]; then
    echo "usage: split-thread.sh \"<short task>\" \"<continuation prompt>\" [--dry-run] [--parent T-id] [--name title] [--cwd dir] [--mode mode] [--workstream slug]" >&2
    exit 2
fi

TASK="${ARGS[0]}"
PROMPT="${ARGS[1]}"
PARENT_ID="${OPT_PARENT:-${AMP_CURRENT_THREAD_ID:-}}"
WS_NAME="${OPT_NAME:-$TASK}"
WS_CWD="${OPT_CWD:-$REPO_ROOT}"
CONTINUE_CMD="amp threads continue"
[[ -n "$OPT_MODE" ]] && CONTINUE_CMD+=" -m $OPT_MODE"
RECALL_ARGS=(--task "$TASK" --no-debug-write)
[[ -n "$OPT_WORKSTREAM" ]] && RECALL_ARGS+=(--workstream "$OPT_WORKSTREAM")

log() { echo "[$(date '+%H:%M:%S')] split-v2: $*" >&2; }

if [[ -z "$PARENT_ID" ]]; then
    log "WARN: AMP_CURRENT_THREAD_ID not set — handoff will use 'last thread'"
fi

# 1. Recall pack
log "generating recall pack for: $TASK"
PACK_FILE="$(mktemp -t split-v2-pack.XXXXXX.md)"
SEED_FILE="$(mktemp -t split-v2-seed.XXXXXX.md)"
trap 'rm -f "$PACK_FILE" "$SEED_FILE"' EXIT

if ! python3 "$RECALL_SCRIPT" "${RECALL_ARGS[@]}" > "$PACK_FILE" 2>/dev/null; then
    log "recall pack generation failed; continuing with task + prompt only"
    : > "$PACK_FILE"
fi

# 2. Compose seed (recall pack + continuation prompt)
{
    echo "Continuation of parent Amp thread ${PARENT_ID:-<unknown>}. Use the recall pack below for bounded context; the full parent thread is searchable via @@."
    echo
    echo "=== RECALL PACK ==="
    cat "$PACK_FILE"
    echo
    echo "=== CONTINUATION PROMPT ==="
    echo "$PROMPT"
} > "$SEED_FILE"

PACK_BYTES=$(wc -c < "$PACK_FILE" | tr -d ' ')
SEED_BYTES=$(wc -c < "$SEED_FILE" | tr -d ' ')
log "recall pack: ${PACK_BYTES}B, full seed: ${SEED_BYTES}B"

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY RUN — would invoke:"
    echo "  amp threads handoff ${PARENT_ID:-<last>} --print  < $SEED_FILE" >&2
    echo "  $CMUX_BIN new-workspace --name \"$WS_NAME\" --cwd \"$WS_CWD\" --focus false --command \"$CONTINUE_CMD <NEW_ID>\"" >&2
    echo
    echo "----- SEED PREVIEW (first 60 lines) -----" >&2
    head -60 "$SEED_FILE" >&2
    echo "----- END PREVIEW -----" >&2
    exit 0
fi

# 3. Create handoff thread server-side
log "creating handoff thread"
HANDOFF_ARGS=(threads handoff --print)
[[ -n "$PARENT_ID" ]] && HANDOFF_ARGS+=("$PARENT_ID")

NEW_ID="$(amp "${HANDOFF_ARGS[@]}" < "$SEED_FILE" | tail -1 | tr -d '[:space:]')"
if [[ -z "$NEW_ID" || ! "$NEW_ID" =~ ^T- ]]; then
    log "ERROR: handoff did not return a thread ID (got: '$NEW_ID')"
    exit 1
fi
log "new thread: $NEW_ID"

# 4. New cmux workspace continuing the new thread
log "opening new cmux workspace"
# Background by default: rotation runs from start-day and must never steal focus.
"$CMUX_BIN" new-workspace --name "$WS_NAME" --cwd "$WS_CWD" --focus false --command "$CONTINUE_CMD $NEW_ID" >&2

log "done — new thread: $NEW_ID"
echo "$NEW_ID"
