#!/usr/bin/env bash
# Execute queued meeting jobs that are due. pm-sync invokes this every 15 minutes,
# so meeting automation intentionally has up to 15-minute scheduling granularity.
set -euo pipefail

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
SWITCHBOARD_DIR="$(dirname "$DAEMON_DIR")"
STATE_DIR="$SWITCHBOARD_DIR/.state"
QUEUE_FILE="$STATE_DIR/due-jobs.json"
LOCK_DIR="$STATE_DIR/due-jobs.lock"
LOG_FILE="$STATE_DIR/scheduled-jobs.log"

mkdir -p "$STATE_DIR"
touch "$LOG_FILE"
[ -f "$QUEUE_FILE" ] || printf '[]\n' > "$QUEUE_FILE"

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

# Preserve legacy locks during migration; age does not prove owner death.
if [ -e "$LOCK_DIR" ]; then
  log "Legacy due-jobs lock requires owner verification"
  exit 75
fi
# shellcheck source=../system/scripts/lib/dir-lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/system/scripts/lib/dir-lock.sh"
if switchboard_is_windows_shell; then
  if ! switchboard_dir_lock_acquire "$LOCK_DIR.win" 0; then
    log "Another due-jobs invocation holds the lock"
    exit 75
  fi
  trap switchboard_dir_lock_release EXIT
else
exec 9>>"$LOCK_DIR.flock"
if ! python3 - <<'PY'
import fcntl
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
PY
then
  log "Another due-jobs invocation holds the lock"
  exit 75
fi
fi

python3 - "$QUEUE_FILE" "$LOG_FILE" <<'PY'
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

queue_path = Path(sys.argv[1])
log_path = Path(sys.argv[2])
now = datetime.now(timezone.utc)

def parse_iso(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def log(message):
    with log_path.open("a") as handle:
        handle.write(f"[{timestamp()}] {message}\n")

def save(jobs):
    fd, tmp_name = tempfile.mkstemp(prefix="due-jobs.", suffix=".tmp", dir=queue_path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(jobs, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, queue_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

try:
    jobs = json.loads(queue_path.read_text())
    if not isinstance(jobs, list):
        raise ValueError("queue root must be an array")
except (json.JSONDecodeError, OSError, ValueError) as exc:
    log(f"ERROR: Could not read due-jobs queue: {exc}")
    raise SystemExit(1)

for job in jobs:
    if job.get("executed_at") is not None:
        continue
    try:
        due_at = parse_iso(job.get("due_at"))
    except (TypeError, ValueError):
        log(f"ERROR: Invalid due_at for job {job.get('id', '<unknown>')}")
        continue
    if due_at > now:
        continue

    # Pre-briefs are due 10 minutes before the meeting starts. A brief that is
    # more than 20 minutes late lands after the meeting is underway, so expire it
    # instead of posting stale prep to Slack after a daemon outage.
    job_id = str(job.get("id", ""))
    max_lateness = timedelta(minutes=20) if job_id.startswith("pre:") else timedelta(hours=3)
    if now - due_at > max_lateness:
        job["executed_at"] = timestamp()
        job["exit_code"] = -1
        if not str(job.get("label", "")).endswith(" (expired)"):
            job["label"] = f"{job.get('label', job.get('id', 'job'))} (expired)"
        log(f"Expired stale job {job.get('id')}: {job.get('label')}")
        save(jobs)
        continue

    log(f"Running due job {job.get('id')}: {job.get('label', '')}")
    try:
        completed = subprocess.run(str(job.get("command", "")), shell=True)
        exit_code = completed.returncode
    except Exception as exc:
        log(f"ERROR: Failed to launch job {job.get('id')}: {exc}")
        exit_code = 127
    job["executed_at"] = timestamp()
    job["exit_code"] = exit_code
    log(f"Completed due job {job.get('id')} with exit code {exit_code}")
    save(jobs)

cutoff = now - timedelta(days=7)
kept = []
for job in jobs:
    executed_at = job.get("executed_at")
    if executed_at:
        try:
            if parse_iso(executed_at) < cutoff:
                continue
        except (TypeError, ValueError):
            pass
    kept.append(job)
if len(kept) != len(jobs):
    log(f"Pruned {len(jobs) - len(kept)} executed job(s) older than 7 days")
    save(kept)
PY
