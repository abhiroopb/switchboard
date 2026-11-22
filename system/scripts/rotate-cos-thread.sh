#!/usr/bin/env bash
# rotate-cos-thread.sh — rotate the CoS Amp thread once per day, at start of day.
#
# Rotate CoS at start of day only, through the existing split-thread.sh path
# with a seeded recall pack. No compaction-count trigger
# (docs/runbooks/cos-latency-trial.md).
#
# What it does (guarded, non-fatal, once per PT date):
#   1. Finds the live CoS workspace (source key `megadesk`) and its thread id.
#   2. Calls split-thread.sh: `amp threads handoff <old>` seeds a NEW thread with
#      a bounded recall pack + the CoS continuation prompt, and opens a new
#      cmux workspace running `amp threads continue -m <mode> <new>`.
#      Nothing is ever typed into the old CoS session (an invariant), so
#      rotation always uses a fresh workspace, not an in-place relaunch.
#   3. Makes the new workspace the CoS surface: title "🧭 CoS", pinned, top.
#      The old workspace is renamed "📜 CoS (rotated <date>)" and unpinned so
#      no resolver (find_workspace_ref_for_key megadesk) matches it again.
#   4. Announces the rotation in both places the owner looks: a guarded
#      [COS ROTATION] line into the old thread (cig_safe_send; deferred sends
#      fall back to a sidebar status badge) and a status badge on the new tab.
#   5. Records system/state/cos-rotation.json {date, old/new thread, refs}.
#
# Usage:
#   bash system/scripts/rotate-cos-thread.sh [--dry-run] [--force]
#     --dry-run  print what would happen, change nothing
#     --force    rotate even if the stamp says today already rotated
# Output: one JSON line {status: rotated|skipped|failed, reason?, ...}.
# Kill switch: PM_OS_COS_ROTATION_DISABLED=1.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" || ! -f "$REPO_ROOT/system/cmux-helpers.sh" ]]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
export REPO_ROOT
SYSTEM_DIR="$REPO_ROOT/system"
STAMP_FILE="${COS_ROTATION_STAMP:-$SYSTEM_DIR/state/cos-rotation.json}"
LOG_FILE="${COS_ROTATION_LOG:-$SYSTEM_DIR/state/logs/cos-rotation.log}"
COS_TITLE="🧭 CoS"

DRY_RUN=0
FORCE=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        *) echo "usage: rotate-cos-thread.sh [--dry-run] [--force]" >&2; exit 2 ;;
    esac
done

# shellcheck source=../cmux-helpers.sh
source "$SYSTEM_DIR/cmux-helpers.sh"

TODAY_PT="$(TZ=America/Los_Angeles date +%Y-%m-%d)"

logline() {
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE" 2>/dev/null || true
    echo "rotate-cos: $*" >&2
}

emit() {
    # emit <status> <reason> [k=v ...]
    local status="$1" reason="${2:-}"; shift 2 || true
    python3 - "$status" "$reason" "$TODAY_PT" "$@" <<'PY'
import json, sys
status, reason, today, *kv = sys.argv[1:]
out = {"status": status, "date": today}
if reason:
    out["reason"] = reason
for item in kv:
    k, _, v = item.partition("=")
    out[k] = v
print(json.dumps(out))
PY
}

skip() { logline "skipped: $1"; emit skipped "$1" "${@:2}"; exit 0; }

if [[ "${PM_OS_COS_ROTATION_DISABLED:-0}" == "1" ]]; then
    skip "disabled (PM_OS_COS_ROTATION_DISABLED=1)"
fi
# Rotation rides `amp threads handoff`, which has no Claude Code equivalent:
# with the knob on claude (router.yaml `agent:`) CoS keeps its
# session and Claude's own auto-compaction handles length.
if [[ "$(pm_os_agent)" != "amp" ]]; then
    skip "agent is $(pm_os_agent): rotation needs amp threads handoff"
fi

if [[ "$FORCE" -ne 1 && -f "$STAMP_FILE" ]]; then
    last_date="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("date",""))' "$STAMP_FILE" 2>/dev/null || true)"
    if [[ "$last_date" == "$TODAY_PT" ]]; then
        skip "already rotated today" "last=$last_date"
    fi
fi

OLD_REF="$(find_workspace_ref_for_key megadesk 2>/dev/null || true)"
[[ -n "$OLD_REF" ]] || skip "no live CoS workspace"

OLD_THREAD="$(amp_thread_for_workspace "$OLD_REF" 2>/dev/null | tr -d '[:space:]' || true)"
[[ "$OLD_THREAD" =~ ^T- ]] || skip "CoS workspace $OLD_REF has no bound Amp thread" "old_ref=$OLD_REF"

MODE="$(resolve_agent_mode megadesk 2>/dev/null || echo ultra)"

PROMPT="You are the Chief of Staff (CoS) surface for PM OS, rotated at start of day ${TODAY_PT} from thread ${OLD_THREAD}. Read routines/megadesk/CONTEXT.md and system/prompts/dispatch-core.md, apply the recall pack above as bounded context (the full previous thread is searchable via @@), and then wait for the [START-DAY REVIEW] digest. Do not re-run start of day, do not redo work the previous thread finished, and do not reply to this seed with a summary."

if [[ "$DRY_RUN" -eq 1 ]]; then
    logline "[DRY RUN] would rotate CoS $OLD_REF thread $OLD_THREAD (mode $MODE)"
    bash "$SYSTEM_DIR/scripts/split-thread.sh" "chief of staff start of day" "$PROMPT" \
        --dry-run --parent "$OLD_THREAD" --name "$COS_TITLE" --cwd "$REPO_ROOT" --mode "$MODE" \
        --workstream megadesk >&2 || true
    emit dry-run "" "old_thread=$OLD_THREAD" "old_ref=$OLD_REF" "mode=$MODE"
    exit 0
fi

before_listing="$(cmux_cmd list-workspaces 2>/dev/null || true)"

logline "rotating CoS $OLD_REF thread $OLD_THREAD (mode $MODE)"
NEW_THREAD="$(bash "$SYSTEM_DIR/scripts/split-thread.sh" "chief of staff start of day" "$PROMPT" \
    --parent "$OLD_THREAD" --name "$COS_TITLE" --cwd "$REPO_ROOT" --mode "$MODE" \
    --workstream megadesk 2>>"$LOG_FILE" | tail -1 | tr -d '[:space:]')"
if [[ ! "$NEW_THREAD" =~ ^T- ]]; then
    logline "failed: split-thread.sh returned no thread id"
    emit failed "split-thread.sh returned no thread id" "old_thread=$OLD_THREAD" "old_ref=$OLD_REF"
    exit 1
fi

# Find the new workspace: newest "🧭 CoS" that is not the old one.
NEW_REF=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    listing="$(cmux_cmd list-workspaces 2>/dev/null || true)"
    candidate="$(find_newest_workspace_by_name_in_listing "$listing" "$COS_TITLE" 2>/dev/null || true)"
    if [[ -n "$candidate" && "$candidate" != "$OLD_REF" ]]; then
        NEW_REF="$candidate"
        break
    fi
    sleep 1
done
if [[ -z "$NEW_REF" ]]; then
    # Fallback: any ref present now that was absent before.
    after_listing="$(cmux_cmd list-workspaces 2>/dev/null || true)"
    NEW_REF="$(comm -13 <(workspace_order_signature "$before_listing" | sort) \
                        <(workspace_order_signature "$after_listing" | sort) | tail -1)"
fi

if [[ -z "$NEW_REF" ]]; then
    logline "failed: new thread $NEW_THREAD created but its workspace was not found"
    emit failed "new workspace not found" "old_thread=$OLD_THREAD" "new_thread=$NEW_THREAD" "old_ref=$OLD_REF"
    exit 1
fi

# Retire the old surface first so resolvers stop matching it, then promote the new one.
cmux_cmd rename-workspace --workspace "$OLD_REF" "📜 CoS (rotated $TODAY_PT)" &>/dev/null || \
    logline "warn: could not rename old CoS workspace $OLD_REF"
cmux_cmd workspace-action --workspace "$OLD_REF" --action unpin &>/dev/null || true
style_megadesk_workspace "$NEW_REF"
set_workspace_status "$NEW_REF" "role" "Chief of Staff" "" "#4A90D9"
set_workspace_status "$NEW_REF" "rotation" "new ${TODAY_PT} ← ${OLD_THREAD}" "" "#8E44AD"

# Record the stamp before announcing: the announcement is best effort.
python3 - "$STAMP_FILE" "$TODAY_PT" "$OLD_THREAD" "$NEW_THREAD" "$OLD_REF" "$NEW_REF" "$MODE" <<'PY'
import json, os, sys, datetime
path, today, old_t, new_t, old_r, new_r, mode = sys.argv[1:]
history = []
if os.path.exists(path):
    try:
        prev = json.load(open(path))
        history = prev.get("history", [])
        history.append({k: prev.get(k) for k in ("date", "old_thread", "new_thread", "old_ref", "new_ref", "mode", "rotated_at")})
    except Exception:
        history = []
data = {
    "date": today,
    "rotated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "old_thread": old_t, "new_thread": new_t,
    "old_ref": old_r, "new_ref": new_r, "mode": mode,
    "history": history[-14:],
}
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
json.dump(data, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY

# Announce in the old thread (guarded; never clobbers a draft or a running turn).
ANNOUNCE="[COS ROTATION] This CoS thread rotated at start of day ${TODAY_PT}. The new CoS thread is https://ampcode.com/threads/${NEW_THREAD} in cmux workspace ${NEW_REF} (${COS_TITLE}). Reply there from now on; this thread stays readable as history. Do not route or act on this line."
announce_rc=0
cig_safe_send "$OLD_REF" "$ANNOUNCE" 45 || announce_rc=$?
if (( announce_rc == 0 )); then
    announced="old-thread"
else
    announced="badge-only"
    set_workspace_status "$OLD_REF" "rotation" "→ ${NEW_THREAD}" "" "#8E44AD"
    logline "announce deferred (rc $announce_rc); set badge on $OLD_REF instead"
fi

logline "rotated: old $OLD_THREAD ($OLD_REF) -> new $NEW_THREAD ($NEW_REF), announced=$announced"
emit rotated "" "old_thread=$OLD_THREAD" "new_thread=$NEW_THREAD" "old_ref=$OLD_REF" "new_ref=$NEW_REF" "mode=$MODE" "announced=$announced"
