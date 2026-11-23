#!/usr/bin/env bash
# route-to-workspace.sh: compatibility shim over dispatch.py.
#
# Every ask becomes one worker in its own git worktree; there is no owner-tab
# probe, admission claim, write reservation or Router hand-off any more.
# The old flags are accepted so CoS, Routers and automation keep working:
#
#   route-to-workspace.sh "<the ask text>" [--name kebab-slug] [--handoff "<context>"]
#       [--priority high|medium|low] [--require-existing] [--focus|--no-focus]
#       [--task-id rt-… --attempt-id …] [--write-target <path>]... [--read-only]
#       [--force-parallel] [--depends-on <task-id>]... [--continuation-of <task-id>]
#       [--request-kind new|follow-up]
#
# Output (last line): JSON {slug, emoji, workspace_ref, created, seeded, seed_status,
# thread_id, local, task_id, attempt_id, status, depends_on, route_kind, branch, worktree}.
# status is routed | queued | duplicate | already-live | failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export REPO_ROOT

ASK="" NAME="" HANDOFF="" PRIORITY="medium" TASK_ID="" CONTINUATION_OF="" REQUIRE_EXISTING=false
WRITE_TARGETS=() DEPENDS_ON=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)     NAME="$2"; shift 2 ;;
        --handoff)  HANDOFF="$2"; shift 2 ;;
        --priority) PRIORITY="$2"; shift 2 ;;
        --task-id)  TASK_ID="$2"; shift 2 ;;
        --write-target) WRITE_TARGETS+=("$2"); shift 2 ;;
        --depends-on)
            IFS=',' read -r -a _deps <<<"$2"
            for _d in "${_deps[@]}"; do [[ -n "$_d" ]] && DEPENDS_ON+=("$_d"); done
            shift 2 ;;
        --continuation-of) CONTINUATION_OF="$2"; shift 2 ;;
        --require-existing) REQUIRE_EXISTING=true; shift ;;
        --attempt-id|--request-kind) shift 2 ;;                       # ignored (legacy)
        --focus|--no-focus|--read-only|--force-parallel) shift ;;      # ignored (legacy)
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) if [[ -z "$ASK" ]]; then ASK="$1"; else ASK="$ASK $1"; fi; shift ;;
    esac
done
[[ -n "${ASK//[[:space:]]/}" ]] || { echo "error: ask text is required" >&2; exit 2; }
if [[ "$REQUIRE_EXISTING" == "true" && -n "$NAME" && ! -d "$REPO_ROOT/workstreams/$NAME" ]]; then
    printf '{"slug":"%s","status":"failed","created":false,"seeded":false,"seed_status":"no-such-workstream","local":false}\n' "$NAME"
    exit 1
fi

prompt="$ASK"
[[ -n "$HANDOFF" ]] && prompt+=$'\n\n'"Handoff from CoS: $HANDOFF"
[[ "$PRIORITY" != "medium" ]] && prompt+=$'\n\n'"Priority: $PRIORITY."
external=()
for t in ${WRITE_TARGETS[@]+"${WRITE_TARGETS[@]}"}; do
    case "$t" in "$REPO_ROOT"/*|workstreams/*|system/*|docs/*|notes/*|outputs/*|knowledge/*|people/*|projects/*) ;; *) external+=("$t") ;; esac
done
if (( ${#external[@]} )); then
    prompt+=$'\n\n'"Declared write targets outside the repo (take the path lock before writing: python3 $REPO_ROOT/system/scripts/dispatch.py lock --task-id <task> --path <path>): $(IFS=', '; printf '%s' "${external[*]}")"
fi

args=(dispatch --origin "${SWITCHBOARD_DISPATCH_ORIGIN:-cos}")
[[ -n "$NAME" ]] && args+=(--workstream "$NAME")
[[ -n "$CONTINUATION_OF" ]] && args+=(--continuation-of "$CONTINUATION_OF")
for _d in ${DEPENDS_ON[@]+"${DEPENDS_ON[@]}"}; do args+=(--depends-on "$_d"); done
if [[ -n "$TASK_ID" ]]; then
    # A row a Router already holds (no worker yet) is re-dispatched as its next attempt.
    args+=(--task-id "$TASK_ID")
    if row="$(python3 "$REPO_ROOT/system/scripts/running-task-state.py" show --task-id "$TASK_ID" 2>/dev/null)" \
        && [[ "$(printf '%s' "$row" | python3 -c 'import json,sys; t=json.load(sys.stdin); print("hold" if t.get("status") in ("routing","queued") and not t.get("worker_id") else "")')" == "hold" ]]; then
        args+=(--force-redispatch)
    fi
fi

rc=0
result="$(python3 "$REPO_ROOT/system/scripts/dispatch.py" "${args[@]}" -- "$prompt")" || rc=$?
python3 - "$result" "$REPO_ROOT" <<'PY'
import json, sys, pathlib, re
r = json.loads(sys.argv[1] or "{}")
slug = r.get("workstream") or ""
emoji = ""
cfg = pathlib.Path(sys.argv[2]) / "workstreams" / slug / "config.yaml"
if slug and cfg.exists():
    m = re.search(r"^emoji:\s*['\"]?(\S+?)['\"]?\s*$", cfg.read_text(encoding="utf-8"), re.M)
    emoji = m.group(1) if m else ""
status = r.get("status")
out = {
    "slug": slug, "emoji": emoji, "workspace_ref": r.get("workspace_ref"), "local": bool(r.get("workspace_ref")),
    "created": status == "routed", "seeded": status == "routed", "seed_status": "inline" if status == "routed" else status,
    "thread_id": None, "task_id": r.get("task_id"), "attempt_id": r.get("attempt_id"), "status": status,
    "depends_on": r.get("depends_on") or [], "continuation_of": r.get("continuation_of"),
    "route_kind": "queued-dependency" if status == "queued" else "worker",
    "branch": r.get("branch"), "worktree": r.get("worktree"), "reason": r.get("reason"), "message": r.get("message"),
}
print(json.dumps(out, ensure_ascii=False))
PY
exit "$rc"
