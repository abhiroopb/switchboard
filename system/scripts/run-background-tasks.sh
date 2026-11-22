#!/usr/bin/env bash
# run-background-tasks.sh — Execute due command and model-backed background tasks
#
# Reads background-tasks.yaml, checks which tasks are due based on
# background-task-state.json, and runs each task with its declared executor.
#
# Runs every 30 min via launchd (com.pm-os.background-tasks).
# Tasks are run synchronously, one at a time, inside a tick bounded by
# MAX_TICK_SECONDS so one tick never overlaps the next launchd start.
#
# Long tasks: a task whose declared
# timeout_seconds exceeds MAX_TICK_SECONDS can never finish inside a tick, so
# it is launched DETACHED in its own session (`--detached-task` mode below),
# outside the tick budget and the scheduler lock, with its full declared
# timeout. The tick returns at once and the remaining tasks run as before.
# While the detached run is alive (pid file under .state/background-long-tasks/)
# later ticks skip the task instead of starting a second copy. Every task with
# timeout_seconds <= MAX_TICK_SECONDS keeps the inline, tick-capped path.

set -euo pipefail
export PATH="/Applications/cmux.app/Contents/Resources/bin:/opt/homebrew/bin:{{HOME}}/.local/bin:{{HOME}}/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Load secrets needed by headless collectors (webhook credentials, etc.)
# launchd's plist EnvironmentVariables only carries PATH/HOME/locale, so any
# API tokens consumed by collect-* scripts must come from this file. Mode 0600.
if [[ -f "$HOME/.config/amp/secrets.env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.config/amp/secrets.env"
fi

REPO_ROOT="${REPO_ROOT:-{{PM_OS_ROOT}}}"
# Task Scheduler on Windows gives a task the PATH from sign-in, so pm-os-env.sh
# exports PM_OS_PYTHON and PM_OS_RUBY from ~/.pm-os/env. Elsewhere both stay unset.
PY="${PM_OS_PYTHON:-python3}"
RUBY="${PM_OS_RUBY:-ruby}"
RUNTIME_STATE_DIR="$REPO_ROOT/.state"
TASKS_FILE="$REPO_ROOT/system/background-tasks.yaml"
STATE_FILE="$REPO_ROOT/system/background-task-state.json"
ATTEMPT_STATE_FILE="$REPO_ROOT/system/background-task-attempts.json"
SCHEDULER="$REPO_ROOT/system/scripts/background_task_scheduler.py"
LOG_FILE="$REPO_ROOT/system/logs/background-tasks.log"
LOCK_DIR="$RUNTIME_STATE_DIR/background-tasks.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
# Longest sane lock lifetime. If a lock dir is older than this, treat it as
# stale (the trap-based cleanup was bypassed by SIGKILL, sleep, or crash) and
# reclaim it. Set comfortably above MAX_TICK_SECONDS plus the end-of-tick
# refresh bound; detached long tasks run outside the lock and do not count.
MAX_LOCK_AGE_SECONDS="${MAX_LOCK_AGE_SECONDS:-1800}"
# One pid file per detached long task; see the header note.
LONG_TASK_PID_DIR="$RUNTIME_STATE_DIR/background-long-tasks"
AMP="$(command -v amp 2>/dev/null || true)"
if [[ -z "$AMP" ]]; then
    for candidate in "$HOME/bin/amp" /opt/homebrew/bin/amp "$HOME/.local/bin/amp"; do
        if [[ -x "$candidate" ]]; then
            AMP="$candidate"
            break
        fi
    done
fi
FAILURE_MARKER="$RUNTIME_STATE_DIR/background-tasks-failing"
REFRESH_PENDING_MARKER="$RUNTIME_STATE_DIR/background-refresh-pending"
TASK_TIMEOUT_SECONDS="${TASK_TIMEOUT_SECONDS:-900}"
# Bound for each post-task / end-of-tick refresh-knowledge.sh hook. An
# unbounded refresh can hold the scheduler lock for an hour, so launchd
# starts no later tick and every due task silently skips. A healthy refresh
# pass takes 1-3 minutes.
REFRESH_TIMEOUT_SECONDS="${REFRESH_TIMEOUT_SECONDS:-300}"
# The enabled task mix requests about 11 executions per 30-minute tick during
# working hours. A cap of 4 starved later config entries for days. Fair
# attempt ordering keeps failures from monopolizing slots; 12 clears the
# normal load while preserving a hard bound for unusually large backlogs.
MAX_TASKS_PER_RUN="${MAX_TASKS_PER_RUN:-12}"
MAX_TICK_SECONDS="${MAX_TICK_SECONDS:-1500}"
TICK_STARTED_AT=$(date +%s)

mkdir -p "$RUNTIME_STATE_DIR" "$(dirname "$LOG_FILE")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"; }

fail_prerequisite() {
    local reason="$1"
    log "ERROR: $reason"
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$reason" > "$FAILURE_MARKER"
    exit 1
}

run_with_timeout() {
    local timeout_seconds="$1"
    shift

    if (( timeout_seconds <= 0 )); then
        echo "Refusing non-positive timeout: $timeout_seconds" >&2
        return 125
    fi

    "${PY:-python3}" - "$timeout_seconds" "$@" <<'PYEOF'
import os
import signal
import subprocess
import sys

timeout = int(sys.argv[1])
cmd = sys.argv[2:]

process = subprocess.Popen(cmd, start_new_session=True)
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    # A shell task can have children. Killing only the shell leaves its work
    # running after the scheduler releases the lock and starts another tick.
    if os.name == "nt":
        # Windows has no process groups: taskkill /T ends the whole child tree.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdin=subprocess.DEVNULL, capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()
    print(f"Timed out after {timeout}s: {' '.join(cmd[:3])}...", file=sys.stderr)
    raise SystemExit(124)

raise SystemExit(returncode)
PYEOF
}

# --- Update success or attempt state for a task ---
# A detached long task finishes while a later tick may be writing the same
# file, so the read-modify-write holds an flock on
# .state/background-task-locks/<state-file basename>.lock (under .state, which
# git ignores, so a lock file can never be committed beside the JSON).
update_timestamp_state() {
    local state_file="$1"
    local task_name="$2"
    local now_iso
    now_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local lock_file="$RUNTIME_STATE_DIR/background-task-locks/$(basename "$state_file").lock"
    mkdir -p "$(dirname "$lock_file")"

    PM_OS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" \
    "${PY:-python3}" - "$state_file" "$task_name" "$now_iso" "$lock_file" <<'PYEOF'
import json
import os
import sys

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    sys.path.insert(0, os.environ["PM_OS_SCRIPTS_DIR"])
    import flock_compat as fcntl

state_file, task_name, now_iso, lock_file = sys.argv[1:5]
with open(lock_file, "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = {}
    if os.path.exists(state_file):
        with open(state_file) as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}
    state[task_name] = now_iso
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    # Windows: a reader or a file-sync client holding the target open makes
    # os.replace fail with WinError 5 for a moment; retry briefly.
    import time
    for attempt in range(10):
        try:
            os.replace(tmp, state_file)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)
PYEOF
}

# --- Detached long-task mode ---
# Invoked only by a tick of this same script (never by launchd directly):
#   run-background-tasks.sh --detached-task NAME TIMEOUT_SECONDS CMD...
# Runs CMD under its full declared timeout, records success state, touches
# the refresh obligation so the next tick runs refresh-knowledge, and clears
# its pid file. It never touches the scheduler lock, so ticks keep running
# beside it. A live copy is detected by the pid file; the launcher skips the
# task while the pid is alive and the file is younger than its timeout.
if [[ "${1:-}" == "--detached-task" ]]; then
    shift
    DETACHED_NAME="${1:?task name}"
    DETACHED_TIMEOUT="${2:?timeout}"
    shift 2
    DETACHED_PID_FILE="$LONG_TASK_PID_DIR/$DETACHED_NAME.pid"
    mkdir -p "$LONG_TASK_PID_DIR"
    echo "$$" > "$DETACHED_PID_FILE"
    trap 'rm -f "$DETACHED_PID_FILE" >/dev/null 2>&1 || true' EXIT
    cd "$REPO_ROOT" || exit 1
    detached_rc=0
    run_with_timeout "$DETACHED_TIMEOUT" "$@" >> "$LOG_FILE" 2>&1 || detached_rc=$?
    # The task may have changed source files; the next tick refreshes.
    : > "$REFRESH_PENDING_MARKER"
    if [[ "$detached_rc" -eq 0 ]]; then
        update_timestamp_state "$STATE_FILE" "$DETACHED_NAME"
        log "OK: $DETACHED_NAME complete (detached, $(( $(date +%s) - TICK_STARTED_AT ))s)"
    elif [[ "$detached_rc" -eq 124 ]]; then
        log "ERROR: $DETACHED_NAME timed out after ${DETACHED_TIMEOUT}s (detached, non-fatal)"
    else
        log "ERROR: $DETACHED_NAME failed with exit $detached_rc (detached, non-fatal)"
    fi
    exit 0
fi

# Launch a long task detached from this tick. Returns at once; the child is a
# new session so launchd's end-of-job process-group cleanup (AbandonProcessGroup
# is unset) and this tick's exit cannot kill it.
launch_detached_task() {
    local task_name="$1"
    local task_timeout="$2"
    shift 2
    mkdir -p "$LONG_TASK_PID_DIR"
    "${PY:-python3}" - "$BASH_SOURCE" "$LOG_FILE" "$task_name" "$task_timeout" "$@" <<'PYEOF'
import subprocess
import os, shutil
import sys

script, log_file, name, timeout, *cmd = sys.argv[1:]
with open(log_file, "ab") as log:
    subprocess.Popen(
        [shutil.which("bash") or "/bin/bash", script, "--detached-task", name, timeout, *cmd],
        **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
PYEOF
}

# Print the pid of a live detached run of the task, or nothing.
detached_task_live_pid() {
    local task_name="$1"
    local task_timeout="$2"
    local pid_file="$LONG_TASK_PID_DIR/$task_name.pid"
    [[ -f "$pid_file" ]] || return 0
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        return 0
    fi
    # A pid file older than the task's own timeout (plus grace) cannot belong
    # to a run that is still honouring it; treat a reused pid as stale.
    local age_min=$(( (task_timeout + 300 + 59) / 60 ))
    if find "$pid_file" -maxdepth 0 -mmin "+$age_min" -print 2>/dev/null | grep -q .; then
        rm -f "$pid_file"
        return 0
    fi
    printf '%s\n' "$pid"
}

# --- Stale lock recovery ---
# If a lock directory exists but the recorded PID isn't running, OR the lock
# has been held longer than MAX_LOCK_AGE_SECONDS, remove it before trying to
# acquire. This protects against trap misses caused by SIGKILL, laptop sleep,
# or the headless amp child detaching from the parent shell.
if [[ -d "$LOCK_DIR" ]]; then
    stale_reason=""
    if [[ -f "$LOCK_PID_FILE" ]]; then
        old_pid=$(cat "$LOCK_PID_FILE" 2>/dev/null || true)
        if [[ -n "$old_pid" ]] && ! kill -0 "$old_pid" 2>/dev/null; then
            stale_reason="pid $old_pid no longer running"
        fi
    else
        stale_reason="no pid file"
    fi
    if [[ -z "$stale_reason" ]]; then
        # Age check (find -mmin needs minutes; convert seconds → minutes, min 1)
        age_min=$(( (MAX_LOCK_AGE_SECONDS + 59) / 60 ))
        if find "$LOCK_DIR" -maxdepth 0 -mmin "+$age_min" -print 2>/dev/null | grep -q .; then
            stale_reason="lock dir older than ${MAX_LOCK_AGE_SECONDS}s"
        fi
    fi
    if [[ -n "$stale_reason" ]]; then
        log "Stale lock detected ($stale_reason); reclaiming"
        rm -rf "$LOCK_DIR" 2>/dev/null || true
    fi
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another background-tasks run is already in progress, skipping"
    exit 0
fi

echo "$$" > "$LOCK_PID_FILE" 2>/dev/null || true

trap 'rm -rf "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

# --- Prerequisites ---
if [[ ! -f "$TASKS_FILE" ]]; then
    fail_prerequisite "Tasks file not found: $TASKS_FILE"
fi

if [[ ! -f "$SCHEDULER" ]]; then
    fail_prerequisite "Scheduler not found: $SCHEDULER"
fi

if ! command -v "$PY" &>/dev/null; then
    fail_prerequisite "python3 not found ($PY)"
fi

if ! command -v "$RUBY" &>/dev/null; then
    fail_prerequisite "ruby not found ($RUBY)"
fi

cd "$REPO_ROOT" || fail_prerequisite "Cannot enter repo root: $REPO_ROOT"

TASK_CONFIG_JSON=$("$RUBY" -ryaml -rjson -e 'print JSON.generate(YAML.load_file(ARGV[0]))' "$TASKS_FILE" 2>>"$LOG_FILE") || {
    fail_prerequisite "Failed to parse tasks config: $TASKS_FILE"
}

# Prerequisites are healthy for this tick.
rm -f "$FAILURE_MARKER"

# --- Determine due tasks ---
get_due_tasks() {
    TASK_CONFIG_JSON="$TASK_CONFIG_JSON" "$PY" "$SCHEDULER" \
        --success-state "$STATE_FILE" \
        --attempt-state "$ATTEMPT_STATE_FILE" \
        --default-timeout "$TASK_TIMEOUT_SECONDS"
}

# --- Main ---
if ! DUE_TASKS=$(get_due_tasks 2>>"$LOG_FILE"); then
    fail_prerequisite "Background task scheduler rejected the task configuration"
fi

if [[ -z "$DUE_TASKS" ]]; then
    log "No tasks due"
fi

DUE_COUNT="$(printf '%s\n' "$DUE_TASKS" | grep -c . || true)"
if (( DUE_COUNT > MAX_TASKS_PER_RUN )); then
    log "WARN: $DUE_COUNT tasks are due; this tick will run $MAX_TASKS_PER_RUN and defer $((DUE_COUNT - MAX_TASKS_PER_RUN))."
fi

# Execute each due task with its declared executor (max N per run).
TASK_COUNT=0
while IFS= read -r TASK_LINE; do
    [[ -z "$TASK_LINE" ]] && continue
    [[ "$TASK_COUNT" -ge "$MAX_TASKS_PER_RUN" ]] && break
    elapsed=$(( $(date +%s) - TICK_STARTED_AT ))
    if (( elapsed >= MAX_TICK_SECONDS )); then
        log "WARN: tick elapsed-time bound reached (${MAX_TICK_SECONDS}s); remaining tasks deferred"
        break
    fi

    TASK_NAME=$(echo "$TASK_LINE" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['name'])")
    TASK_EXECUTOR=$(echo "$TASK_LINE" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['executor'])")
    TASK_TIMEOUT=$(echo "$TASK_LINE" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['timeout_seconds'])")

    case "$TASK_EXECUTOR" in
        command)
            TASK_COMMAND=$(echo "$TASK_LINE" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['command'])")
            TASK_RUN=(/bin/bash -c "$TASK_COMMAND")
            ;;
        amp)
            TASK_PROMPT=$(echo "$TASK_LINE" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['prompt'])")
            if [[ -z "$AMP" || ! -x "$AMP" ]]; then
                log "ERROR: $TASK_NAME requires amp, but the CLI was not found (non-fatal)"
                update_timestamp_state "$ATTEMPT_STATE_FILE" "$TASK_NAME"
                TASK_COUNT=$((TASK_COUNT + 1))
                continue
            fi
            TASK_RUN=(
                "$AMP"
                --settings-file "$HOME/.config/amp/settings-headless.json"
                -x "$TASK_PROMPT"
            )
            ;;
        *)
            log "ERROR: $TASK_NAME has unknown executor '$TASK_EXECUTOR' (non-fatal)"
            update_timestamp_state "$ATTEMPT_STATE_FILE" "$TASK_NAME"
            TASK_COUNT=$((TASK_COUNT + 1))
            continue
            ;;
    esac

    if (( TASK_TIMEOUT > MAX_TICK_SECONDS )); then
        # Declared timeout cannot fit inside a tick: run detached with the
        # full timeout, outside the tick budget (see header). One copy at a
        # time; a live run is neither re-attempted nor counted.
        live_pid=$(detached_task_live_pid "$TASK_NAME" "$TASK_TIMEOUT")
        if [[ -n "$live_pid" ]]; then
            log "SKIP: $TASK_NAME still running detached (pid $live_pid); not relaunched"
            continue
        fi
        log "Running task: $TASK_NAME ($TASK_EXECUTOR, detached, timeout ${TASK_TIMEOUT}s > tick bound ${MAX_TICK_SECONDS}s)"
        TASK_COUNT=$((TASK_COUNT + 1))
        update_timestamp_state "$ATTEMPT_STATE_FILE" "$TASK_NAME"
        : > "$REFRESH_PENDING_MARKER"
        launch_detached_task "$TASK_NAME" "$TASK_TIMEOUT" "${TASK_RUN[@]}"
        continue
    fi

    log "Running task: $TASK_NAME ($TASK_EXECUTOR)"
    TASK_COUNT=$((TASK_COUNT + 1))
    update_timestamp_state "$ATTEMPT_STATE_FILE" "$TASK_NAME"

    elapsed=$(( $(date +%s) - TICK_STARTED_AT ))
    remaining=$(( MAX_TICK_SECONDS - elapsed ))
    effective_timeout="$TASK_TIMEOUT"
    (( effective_timeout > remaining )) && effective_timeout="$remaining"
    if (( effective_timeout <= 0 )); then
        log "WARN: no positive timeout remains for $TASK_NAME; task deferred"
        break
    fi
    # Record the refresh obligation before a task can change source files.
    # A crash after the task writes must not lose the obligation on restart.
    : > "$REFRESH_PENDING_MARKER"
    if run_with_timeout "$effective_timeout" "${TASK_RUN[@]}" >> "$LOG_FILE" 2>&1; then
        update_timestamp_state "$STATE_FILE" "$TASK_NAME"
        log "OK: $TASK_NAME complete"
    else
        status=$?
        if [[ "$status" -eq 124 ]]; then
            log "ERROR: $TASK_NAME timed out after ${effective_timeout}s (non-fatal)"
        else
            log "ERROR: $TASK_NAME failed with exit $status (non-fatal)"
        fi
    fi
done <<< "$DUE_TASKS"

# Coalesce all source changes into one canonical refresh per tick. A failed
# refresh stays visible as a failure; successful collectors keep their own
# success timestamps and are not hidden by that downstream failure.
if [[ -f "$REFRESH_PENDING_MARKER" ]]; then
    elapsed=$(( $(date +%s) - TICK_STARTED_AT ))
    remaining=$(( MAX_TICK_SECONDS - elapsed ))
    if [[ ! -f "$REPO_ROOT/system/scripts/refresh-knowledge.sh" ]]; then
        # The kit does not ship this publisher-only hook: nothing to refresh, so drop the obligation.
        rm -f "$REFRESH_PENDING_MARKER"
    elif (( remaining > 0 )); then
        refresh_timeout="$REFRESH_TIMEOUT_SECONDS"
        (( refresh_timeout > remaining )) && refresh_timeout="$remaining"
        if run_with_timeout "$refresh_timeout" bash "$REPO_ROOT/system/scripts/refresh-knowledge.sh" collector:background-tasks >> "$LOG_FILE" 2>&1; then
            rm -f "$REFRESH_PENDING_MARKER"
        else
            log "WARN: refresh-knowledge reported failures or timed out (${refresh_timeout}s) after background tasks; pending marker retained"
        fi
    else
        log "WARN: knowledge refresh deferred because the tick elapsed-time bound was reached"
    fi
fi

log "Done. Ran $TASK_COUNT task(s)."
