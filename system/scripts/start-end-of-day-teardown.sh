#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CMUX_BIN="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
TEARDOWN_SCRIPT="${EOD_TEARDOWN_SCRIPT:-$REPO_ROOT/system/scripts/end-of-day-close.sh}"
REPORT_SCRIPT="${EOD_REPORT_SCRIPT:-$REPO_ROOT/system/scripts/report-to-cos.sh}"
LOG_DIR="${EOD_LOG_DIR:-$REPO_ROOT/system/state/logs}"
# The child inherits the kernel lock across exec. Acquisition and spawn are
# one critical section, including concurrent CoS and legacy Router callers.
LOCK_FILE="${EOD_LOCK_FILE:-$REPO_ROOT/system/state/eod-teardown.lock.json}"
INVOKING_WORKSPACE="${SWITCHBOARD_INVOKING_WORKSPACE_REF:-}"
TASK_ID=""
ORIGIN="cos"
SUMMARY="End-of-day state is saved."
KEEP_APP=false
DRY_RUN=false

while (( $# > 0 )); do
    case "$1" in
        --invoking-workspace|--workspace) INVOKING_WORKSPACE="${2:-}"; shift 2 ;;
        --task-id) TASK_ID="${2:-}"; shift 2 ;;
        --origin) ORIGIN="${2:-}"; shift 2 ;;
        --summary) SUMMARY="${2:-}"; shift 2 ;;
        --keep-app) KEEP_APP=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help)
            cat <<'USAGE'
Usage: start-end-of-day-teardown.sh [--invoking-workspace workspace:N] [--task-id ID] [--origin cos|launchpad] [--summary TEXT] [--keep-app|--dry-run]

CoS-owned close-of-day (default): CoS runs the closing-day durable
steps itself, prints its closeout summary, then calls this launcher with NO
--task-id. The invoking workspace resolves to CoS, so the detached teardown
closes every other workspace first and CoS last, then quits cmux.

Legacy Router path: a queued Router close passes its own --invoking-workspace
and --task-id; the launcher reports "Teardown started" to the origin. A second
call while a teardown child is alive returns status duplicate-suppressed.
USAGE
            exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$INVOKING_WORKSPACE" ]]; then
    INVOKING_WORKSPACE="$($CMUX_BIN identify 2>/dev/null | "${SWITCHBOARD_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/lib/json_get.py" caller.workspace_ref)"
fi
ORIGIN="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" normalize "$ORIGIN")"
[[ "$INVOKING_WORKSPACE" =~ ^workspace:[0-9]+$ ]] || {
    echo "error: could not resolve the invoking workspace" >&2
    exit 2
}

if [[ "$DRY_RUN" != "true" && -z "$TASK_ID" ]]; then
    cos_title="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get cos title)"
    listing="$("$CMUX_BIN" list-workspaces)" || exit 1
    cos_ref="$(printf '%s\n' "$listing" | awk -v t="$cos_title" '{
        sub(/^[ *]+/, ""); ref=$1; sub(/^[^ ]+[ ]+/, ""); sub(/[ ]+\[selected\]$/, "")
        if ($0 == t) print ref
    }')"
    [[ "$cos_ref" == "$INVOKING_WORKSPACE" ]] || {
        echo 'error: new EOD teardown must follow durable closeout in CoS; use close-all --request from another workspace' >&2
        exit 2
    }
fi

if [[ "$DRY_RUN" == "true" ]]; then
    exec env SWITCHBOARD_INVOKING_WORKSPACE_REF="$INVOKING_WORKSPACE" \
        bash "$TEARDOWN_SCRIPT" --dry-run --invoking-workspace "$INVOKING_WORKSPACE"
fi

umask 077
mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")"
timestamp="$(date '+%Y%m%dT%H%M%S%z')"
log_file="$LOG_DIR/end-of-day-teardown-$timestamp-$$.log"

teardown_args=(env "SWITCHBOARD_INVOKING_WORKSPACE_REF=$INVOKING_WORKSPACE" \
    bash "$TEARDOWN_SCRIPT" --invoking-workspace "$INVOKING_WORKSPACE")
[[ "$KEEP_APP" == "true" ]] && teardown_args+=(--keep-app)

# Never unlink the guard inode: waiters and children must lock the same file.
# The OS releases it after the child exits, including crashes. No age-based
# expiry can admit a duplicate while a slow teardown is still running.
launch_json="$(python3 - "$LOCK_FILE" "$log_file" "$INVOKING_WORKSPACE" "$TASK_ID" "$ORIGIN" "${teardown_args[@]}" <<'PY'
import json, os, signal, subprocess, sys, time
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("SWITCHBOARD_SCRIPTS_DIR") or _os.path.join(_os.environ.get("SWITCHBOARD_ROOT") or _os.path.expanduser("~/Development/switchboard"), "system", "scripts"))
    import flock_compat as fcntl
path, log, ws, task_id, origin = sys.argv[1:6]
with open(path + '.guard', 'a') as guard:
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({'status': 'duplicate-suppressed', 'invoking_workspace': ws}))
        sys.exit(0)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with open(log, 'ab') as output:
        child = subprocess.Popen(sys.argv[6:], stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=output,
                                 **({'creationflags': 0x08000200} if os.name == 'nt' else
                                    {'start_new_session': True, 'pass_fds': (guard.fileno(),)}))
    result = dict(status='teardown-started', pid=child.pid, log=log,
                  invoking_workspace=ws, task_id=task_id or None, origin=origin,
                  started_at_epoch=int(time.time()))
    with open(path + '.tmp', 'w') as fh:
        json.dump(result, fh)
    os.replace(path + '.tmp', path)
    print(json.dumps(result))
PY
)"
launch_status="$(printf '%s' "$launch_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
if [[ "$launch_status" == "duplicate-suppressed" ]]; then
    # A duplicate request is not proof its own closeout completed. Leave any
    # legacy task held/active rather than silently marking it done.
    printf '%s\n' "$launch_json"
    exit 0
fi
teardown_pid="$(printf '%s' "$launch_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pid"])')"

report_delivered=""
report_status="not-requested"
if [[ -n "$TASK_ID" ]]; then
    report_status="failed"
    report_json="$(bash "$REPORT_SCRIPT" --task-id "$TASK_ID" --origin "$ORIGIN" --status done \
        "Teardown started: $SUMMARY" 2>>"$log_file")" && report_status="sent"
    if [[ -n "${report_json:-}" ]]; then
        report_delivered="$(printf '%s' "$report_json" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get("delivered")
    print("true" if value is True else "false" if value is False else "")
except Exception:
    print("")
')"
    fi
fi

python3 - "$teardown_pid" "$log_file" "$INVOKING_WORKSPACE" "$report_status" "$report_delivered" <<'PY'
import json
import sys

print(json.dumps({
    "status": "teardown-started",
    "pid": int(sys.argv[1]),
    "log": sys.argv[2],
    "invoking_workspace": sys.argv[3],
    "report_status": sys.argv[4],
    "report_delivered": None if not sys.argv[5] else sys.argv[5] == "true",
}))
PY

[[ "$report_status" != "failed" ]]
