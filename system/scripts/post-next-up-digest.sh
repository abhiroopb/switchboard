#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CONFIG_FILE="${NEXT_UP_CONFIG_FILE:-$REPO_ROOT/system/config/router.yaml}"
STATE_FILE="${NEXT_UP_STATE_FILE:-$REPO_ROOT/system/state/next-up-post.json}"
# This also serializes direct Router callbacks, so one terminal result cannot
# land through both the callback and digest paths at the same instant.
LOCK_DIR="${NEXT_UP_LOCK_DIR:-$REPO_ROOT/system/state/next-up-post.json.lockdir}"
CMUX_BIN="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
SURFACE="next-up"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --surface)
            SURFACE="${2:-}"
            shift 2
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done
[[ "$SURFACE" == "next-up" || "$SURFACE" == "start-day" ]] || {
    printf 'invalid surface: %s\n' "$SURFACE" >&2
    exit 2
}

read_config() {
    python3 - "$CONFIG_FILE" <<'PY'
import json
import sys
try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as handle:
        cfg = (yaml.safe_load(handle) or {}).get("next-up") or {}
except Exception:
    cfg = {}
print(json.dumps({
    "cadence": int(cfg.get("cadence_minutes", 30)),
    "start": str(cfg.get("working_hours_start", "08:00")),
    "end": str(cfg.get("working_hours_end", "18:00")),
    "timezone": str(cfg.get("timezone", "America/Los_Angeles")),
    "post_unchanged": bool(cfg.get("post_unchanged", False)),
    "act_limit": int(cfg.get("act_now_limit", 3)),
    "important_limit": int(cfg.get("important_limit", 3)),
    # Bounded guarded-send window per tick. 0 keeps the legacy single attempt.
    "send_wait_seconds": max(0, int(cfg.get("send_wait_seconds", 45))),
    "send_poll_seconds": max(1, int(cfg.get("send_poll_seconds", 5))),
    "queue_after_busy_deferrals": max(0, int(cfg.get("queue_after_busy_deferrals", 3))),
    # How long an outcome-unknown queue submission keeps queue escalation off.
    "unknown_hold_minutes": max(0, int(cfg.get("unknown_hold_minutes", 120))),
}))
PY
}

config="$(read_config)"
gate="$(python3 - "$config" "${NEXT_UP_NOW:-}" <<'PY'
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

cfg = json.loads(sys.argv[1])
zone = ZoneInfo(cfg["timezone"])
now = datetime.fromisoformat(sys.argv[2]).astimezone(zone) if sys.argv[2] else datetime.now(zone)
start_h, start_m = map(int, cfg["start"].split(":"))
end_h, end_m = map(int, cfg["end"].split(":"))
minute = now.hour * 60 + now.minute
in_hours = now.isoweekday() <= 5 and start_h * 60 + start_m <= minute < end_h * 60 + end_m
cadence = max(1, cfg["cadence"])
slot = int(now.timestamp()) // (cadence * 60)
print(json.dumps({"in_hours": in_hours, "slot": slot, "local_date": now.date().isoformat(), **cfg}))
PY
)"

in_hours="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["in_hours"]).lower())')"
if [[ "$SURFACE" == "next-up" && "$in_hours" != "true" ]]; then
    # Evening ticks refresh the state file too, so a stale daytime pending count
    # never outlives the reports it counted. Refresh the pending count from the ledger with the digest's own predicate
    # (next-up-digest.py --pending-report-ids, the same rows the render carries)
    # and clear the deferral bookkeeping once nothing is waiting. Nothing is
    # typed; no send is attempted.
    pending_now="$(python3 "$REPO_ROOT/system/scripts/next-up-digest.py" --pending-report-ids 2>/dev/null \
        | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
    if [[ -f "$STATE_FILE" ]]; then
        python3 - "$STATE_FILE" "$pending_now" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
path = Path(sys.argv[1]); pending = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {"version": 1}
payload["last_status"] = "outside-working-hours"
payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
payload["pending_report_count"] = pending
if pending == 0:
    payload["deferred_streak"] = 0
    payload.pop("deferred_reason", None)
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
    fi
    printf '{"status":"outside-working-hours","pending_reports":%s}\n' "$pending_now"
    exit 0
fi

mkdir -p "$(dirname "$LOCK_DIR")"
# A lock with no pid file older than NEXT_UP_LOCK_STALE_SECONDS is stale too: its
# holder died between mkdir and the pid write, or Windows kept the empty folder when
# the exit trap's rmdir lost a race with a file sync client. Left alone, such a
# folder answers "locked" to every later tick.
lock_age_seconds() {
    python3 -c 'import os,sys,time
try: print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
except OSError: print(-1)' "$1"
}
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    lock_owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    stale="false"
    if [[ -n "$lock_owner" ]]; then
        kill -0 "$lock_owner" 2>/dev/null || stale="true"
    elif (( $(lock_age_seconds "$LOCK_DIR") > ${NEXT_UP_LOCK_STALE_SECONDS:-120} )); then
        stale="true"
    fi
    if [[ "$stale" == "true" ]]; then
        rm -rf "$LOCK_DIR" 2>/dev/null || true
        mkdir "$LOCK_DIR" 2>/dev/null || { printf '{"status":"locked"}\n'; exit 0; }
    else
        printf '{"status":"locked"}\n'
        exit 0
    fi
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

slot="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slot"])')"
local_date="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["local_date"])')"
last_processed_slot="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("last_processed_slot", -1))
except Exception:
    print(-1)
PY
)"
last_start_day_date="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("last_start_day_date", ""))
except Exception:
    print("")
PY
)"
if [[ "$SURFACE" == "next-up" && "$last_processed_slot" == "$slot" ]]; then
    printf '{"status":"cadence-skip"}\n'
    exit 0
fi
if [[ "$SURFACE" == "start-day" && "$last_start_day_date" == "$local_date" ]]; then
    printf '{"status":"start-day-skip"}\n'
    exit 0
fi

act_limit="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["act_limit"])')"
important_limit="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["important_limit"])')"
rendered="$(python3 "$REPO_ROOT/system/scripts/next-up-digest.py" --json \
    --surface "$SURFACE" --act-now-limit "$act_limit" --important-limit "$important_limit")"
digest="$(printf '%s' "$rendered" | python3 -c 'import json,sys; print(json.load(sys.stdin)["message"])')"
# Meeting-ingest lines are stamped as shown after a post and drop out of the
# next render, so they are excluded from the dedupe hash (hash_text); a new
# ingest triggers a post through meeting_ingest_count below instead.
hash_input="$(printf '%s' "$rendered" | python3 -c 'import json,sys; p=json.load(sys.stdin); print(p.get("hash_text") or p["message"])')"
# python3 rather than shasum: Git Bash keeps shasum in /usr/bin/core_perl, which the Windows
# scheduled task's PATH leaves out.
# Same SHA-256 hex digest, so the stored last_hash still matches.
digest_hash="$(printf '%s' "$hash_input" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
# The guard flattens newlines to spaces (a raw newline is Enter in the Amp
# TUI), which turned every autopost into one wrapped line. Send the renderer's
# single-line form, where " ⏎ " marks each line break, so CoS can present one
# topic per line. The hash above stays on the multi-line message.
injection="$(printf '%s' "$rendered" | python3 -c 'import json,sys; p=json.load(sys.stdin); print(p.get("injection_text") or p["message"])')"
pending_draft_count="$(printf '%s' "$rendered" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("pending_draft_ids", [])))')"
post_unchanged="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["post_unchanged"]).lower())')"
last_hash="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("last_post_hash", ""))
except Exception:
    print("")
PY
)"

pending_report_count="$(printf '%s' "$rendered" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("pending_report_ids", [])))')"
meeting_ingest_count="$(printf '%s' "$rendered" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("meeting_ingest_keys", [])))')"

# write_state <status> <processed> [reason]
# Deferral bookkeeping: a bare "deferred" hides a long run of undelivered
# results behind one word. The state file names the final probe state (busy / draft / unreadable / no-target /
# transport-unknown), counts the consecutive deferred streak, and records how
# many terminal reports are still waiting, so the digest itself can be audited.
write_state() {
    local status="$1" processed="$2" reason="${3:-}"
    mkdir -p "$(dirname "$STATE_FILE")"
    NEXT_UP_DEFER_REASON="$reason" NEXT_UP_PENDING_REPORTS="$pending_report_count" \
    python3 - "$STATE_FILE" "$slot" "$digest_hash" "$status" "$processed" "$SURFACE" "$local_date" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {"version": 1}
surface = sys.argv[6]
status = sys.argv[4]
payload["last_status"] = status
payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
if surface == "next-up":
    payload["last_attempt_slot"] = int(sys.argv[2])
    payload["last_attempt_hash"] = sys.argv[3]
    payload["pending_report_count"] = int(os.environ.get("NEXT_UP_PENDING_REPORTS") or 0)
    if sys.argv[5] == "true":
        payload["last_processed_slot"] = int(sys.argv[2])
    if status == "posted":
        payload["last_post_hash"] = sys.argv[3]
        payload["last_posted_at"] = payload["updated_at"]
    if status in ("posted", "unchanged"):
        payload["deferred_streak"] = 0
        payload["busy_deferred_streak"] = 0
        payload.pop("deferred_reason", None)
    elif status in ("deferred", "delivery-unknown"):
        old_busy = int(payload.get("busy_deferred_streak", payload.get("deferred_streak", 0)
                                   if payload.get("deferred_reason") == "busy" else 0))
        payload["deferred_streak"] = int(payload.get("deferred_streak") or 0) + 1
        payload["deferred_reason"] = os.environ.get("NEXT_UP_DEFER_REASON") or "unknown"
        # Busy and draft deferrals both mean "CoS is occupied", so a draft keeps
        # the busy streak and queue escalation (3 busy in a row) still engages
        # while the owner types between turns. Only an
        # unreadable/no-target/unknown-transport probe resets it.
        if payload["deferred_reason"] == "busy":
            payload["busy_deferred_streak"] = old_busy + 1
        elif payload["deferred_reason"] == "draft":
            payload["busy_deferred_streak"] = old_busy
        else:
            payload["busy_deferred_streak"] = 0
        if payload["deferred_reason"] == "queue-outcome-unknown":
            payload["queue_outcome_unknown"] = True
        payload["last_deferred_at"] = payload["updated_at"]
elif status == "posted":
    payload["last_start_day_date"] = sys.argv[7]
    payload["last_start_day_posted_at"] = payload["updated_at"]
tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

if [[ "$SURFACE" == "next-up" && "$pending_draft_count" == "0" \
      && "$pending_report_count" == "0" \
      && "$meeting_ingest_count" == "0" \
      && "$post_unchanged" != "true" && -n "$last_hash" && "$last_hash" == "$digest_hash" ]]; then
    write_state unchanged true
    printf '{"status":"unchanged"}\n'
    exit 0
fi

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
target_surface="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" list --where next_up_target | head -1)"
target_source_type="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get "$target_surface" source_type)"
target_source_key="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get "$target_surface" source_key)"
# The live CoS tab and its agent: the same
# resolver report-to-cos.sh uses (owner_tabs.py cos-target: the claim-cos
# registration, then a CoS-titled tab running router.yaml's agent). A Claude
# CoS gets the digest by cmux agent message below; the title lookup and the
# guarded paste stay for an Amp CoS. NEXT_UP_NO_OWNER_TABS=1 skips it (tests).
target_ref="${COS_WORKSPACE_REF:-}"; target_agent=""
if [[ "$target_source_key" == "switchboard" && "${NEXT_UP_NO_OWNER_TABS:-0}" != "1" ]] \
    && cos_target_json="$(python3 "$REPO_ROOT/system/scripts/owner_tabs.py" cos-target 2>/dev/null)"; then
    cos_ref="$(printf '%s' "$cos_target_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ref") or "")')"
    if [[ -z "$target_ref" || "$target_ref" == "$cos_ref" ]]; then
        target_ref="$cos_ref"
        target_agent="$(printf '%s' "$cos_target_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("agent") or "")')"
    fi
fi
[[ -n "$target_ref" ]] || target_ref="$(find_workspace_ref_for_source_key "$target_source_type" "$target_source_key" 2>/dev/null || true)"
send_wait="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["send_wait_seconds"])')"
send_poll="$(printf '%s' "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["send_poll_seconds"])')"
queue_ready="$(python3 - "$STATE_FILE" "$gate" "${RUNNING_TASKS_FILE:-$REPO_ROOT/system/state/running-tasks.json}" <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    state = {}
# Unknown submissions are held on their task rows and excluded by the renderer.
# A historical uncertain batch must not block newer, cleanly deferred reports.
# The hold is DERIVED from those rows, not from a sticky state flag that could
# stay set and keep queue escalation off indefinitely. Once every held
# row is reconciled (report-delivered / report-not-delivered), queueing resumes.
try:
    tasks = json.load(open(sys.argv[3])).get("tasks", [])
except (OSError, ValueError):
    tasks = []
# The hold is bounded (unknown_hold_minutes, default 120): a held row is only
# reconciled by hand or by the 24 h sweep, so an unbounded hold switched the
# queue off for a day per incident, and every later tick fell back to the 45 s
# idle window.
from datetime import datetime, timedelta, timezone
cfg = json.loads(sys.argv[2])
hold = timedelta(minutes=cfg["unknown_hold_minutes"])
now = datetime.now(timezone.utc)
def stamp(t):
    for key in ("report_attempted_at", "finished_at", "updated_at"):
        try:
            return datetime.fromisoformat(str(t[key]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
    return now
held = any(isinstance(t, dict) and t.get("report_delivery_unknown") is True
           and str(t.get("origin") or "cos") == "cos" and now - stamp(t) < hold for t in tasks)
streak = int(state.get("busy_deferred_streak", state.get("deferred_streak", 0)
                       if state.get("deferred_reason") == "busy" else 0))
print("unknown" if held else
      "true" if streak >= cfg["queue_after_busy_deferrals"] else "false")
PY
)"
if [[ "$queue_ready" == "unknown" ]]; then
    if [[ "$pending_report_count" == "0" ]]; then
        printf '{"status":"delivery-unknown","reason":"queue-outcome-unknown","next_action":"reconcile the held reports; no new reports are pending"}\n'
        exit 0
    fi
    queue_ready="false"
fi

# One-word probe of the target's live TUI state. Distinguishes the deferral
# causes the old code collapsed into "deferred": no-target (registry/cmux
# could not resolve CoS), unreadable (cmux read failed), busy (active Amp
# turn), draft (user text in the box), idle (safe now).
probe_target_state() {
    local ref="$1" screen
    [[ -z "$ref" ]] && { printf 'no-target'; return; }
    screen="$(cig_read "$ref")"
    [[ -z "$screen" ]] && { printf 'unreadable'; return; }
    if ! printf '%s\n' "$screen" | input_box_is_empty; then printf 'draft'; return; fi
    if printf '%s\n' "$screen" | screen_is_busy; then printf 'busy'; return; fi
    printf 'idle'
}

# A deferred working-hours digest does not wait for the next tick's 45 s
# window: one detached waiter (next-up-waiter.sh) polls CoS and re-runs this
# poster the moment the turn ends, as report-waiter.sh does for direct reports.
# Sampling 45 s per tick alone can defer for hours.
# NEXT_UP_NO_WAITER=1 (set by the waiter itself and by tests) skips the spawn.
waiter="false"
spawn_waiter() {
    [[ "${NEXT_UP_NO_WAITER:-0}" == "1" || -z "$target_ref" ]] && return 0
    local dir="${SWITCHBOARD_STATE_DIR:-$REPO_ROOT/system/state}/worker-dispatch" pidfile old
    pidfile="$dir/next-up-waiter.pid"
    mkdir -p "$dir"
    old="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then waiter="true"; return 0; fi
    # Own session (start_new_session): a caller's shell tool kills its process
    # group when the call returns; macOS has no setsid binary, so Python does it.
    env -u SWITCHBOARD_WORKER_ID -u SWITCHBOARD_WORKER_TASK_ID -u SWITCHBOARD_WORKER_ATTEMPT_ID -u SWITCHBOARD_WORKSTREAM_SLUG \
        python3 - "$dir/next-up-waiter.log" bash "$REPO_ROOT/system/scripts/next-up-waiter.sh" "$target_ref" <<'PY' && waiter="true" || true
import os, shutil, subprocess, sys
log = open(sys.argv[1], "ab")
# PATH first: CreateProcess searches System32, where bash.exe is the WSL launcher.
cmd = [shutil.which(sys.argv[2]) or sys.argv[2], *sys.argv[3:]]
subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                 **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
PY
}

send_rc=0
transport_retry="false"
transport="paste"
if [[ -n "$target_ref" && "$target_agent" == "claude" ]] \
    && am_json="$(python3 "$REPO_ROOT/system/scripts/owner_tabs.py" message --ref "$target_ref" --from "switchboard next-up" \
        --text "$digest" 2>/dev/null)"; then
    # Claude CoS: cmux agent message goes through the session's hooks, never the
    # composer, so a draft in CoS or a running turn never defers the digest.
    # Accepted is posted. A refusal falls
    # through to the guarded paste.
    transport="agent-message"
elif [[ -n "$target_ref" ]]; then
    if [[ "$queue_ready" == "true" ]]; then
        cig_safe_queue "$target_ref" "$injection" || send_rc=$?
    else
        cig_safe_send "$target_ref" "$injection" "$send_wait" "$send_poll" || send_rc=$?
    fi
else
    send_rc=2
fi
if (( send_rc == 3 )); then
    # cmux's exit status is not the delivery outcome; the screen is
    # (cig_verify_sent, as report-to-cos.sh does). Seen on screen -> posted.
    # Idle, empty box, text absent -> nothing was pasted: a clean deferral the
    # waiter may retry. Anything else stays unknown.
    verify_rc=0
    cig_verify_sent "$target_ref" "$injection" || verify_rc=$?
    if (( verify_rc == 0 )); then
        send_rc=0
    elif (( verify_rc == 2 )); then
        send_rc=2
        transport_retry="true"
    fi
fi
if (( send_rc == 0 )); then
    ack_args=()
    while IFS= read -r task_id; do
        [[ -n "$task_id" ]] && ack_args+=(--task-id "$task_id")
    done < <(printf '%s' "$rendered" | python3 -c '
import json, sys
for task_id in json.load(sys.stdin).get("pending_report_ids", []):
    print(task_id)
' | tr -d '\r')
    if (( ${#ack_args[@]} > 0 )); then
        python3 "$REPO_ROOT/system/scripts/running-task-state.py" ack-delivered \
            "${ack_args[@]}" >/dev/null
        bash "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" >/dev/null 2>&1 || true
    fi
    inbound_ack_args=()
    while IFS= read -r source_ref; do
        [[ -n "$source_ref" ]] && inbound_ack_args+=(--ref "$source_ref")
    done < <(printf '%s' "$rendered" | python3 -c '
import json, sys
for source_ref in json.load(sys.stdin).get("inbound_refs", []):
    print(source_ref)
' | tr -d '\r')
    if (( ${#inbound_ack_args[@]} > 0 )); then
        python3 "$REPO_ROOT/system/scripts/triage_ledger.py" ack-inbound \
            "${inbound_ack_args[@]}" --surface "$SURFACE" >/dev/null
    fi
    # The feedback console reminder was just shown: record its waiting set so
    # the next-up line stays quiet until a new draft is waiting.
    # The meeting ingests in this digest were just shown: stamp them so the
    # next render carries only newer ingests.
    printf '%s' "$rendered" | python3 "$REPO_ROOT/system/scripts/meeting_ingests.py" --stamp-shown >/dev/null 2>&1 || true
    acked_report_count="$pending_report_count"
    pending_report_count="$(python3 "$REPO_ROOT/system/scripts/next-up-digest.py" --pending-report-ids 2>/dev/null \
        | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
    write_state posted true
    printf '{"status":"posted","surface":"%s","destination_surface":"%s","workspace_ref":"%s","pending_reports_acked":%s,"transport":"%s"}\n' \
        "$SURFACE" "$target_surface" "$target_ref" "$acked_report_count" "$transport"
elif (( send_rc == 3 )); then
    # A queue stage was attempted without confirmation. Hold its reports
    # across ticks; neither callback retries nor the digest may retype them.
    if [[ "$queue_ready" == "true" ]]; then
        while IFS= read -r task_id; do
            task_id="${task_id%$'\r'}"   # python prints CRLF on Windows and read keeps the CR
            [[ -n "$task_id" ]] && python3 "$REPO_ROOT/system/scripts/running-task-state.py" report-unknown --task-id "$task_id" >/dev/null
        done < <(printf '%s' "$rendered" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("pending_report_ids", [])))')
        write_state delivery-unknown true queue-outcome-unknown
        printf '{"status":"delivery-unknown","reason":"queue-outcome-unknown","next_action":"inspect CoS; queued reports and digest are held until reconciled"}\n'
    else
        write_state delivery-unknown true transport-unknown
        printf '{"status":"delivery-unknown","reason":"transport-unknown","surface":"%s","workspace_ref":"%s","pending_reports":%s,"next_action":"no retry this slot; pending reports re-enter the next slot"}\n' \
            "$SURFACE" "$target_ref" "$pending_report_count"
    fi
else
    defer_reason="$(probe_target_state "$target_ref")"
    [[ "$defer_reason" == "idle" ]] && defer_reason="idle-after-window"
    [[ "$transport_retry" == "true" ]] && defer_reason="transport-retry"
    write_state deferred false "$defer_reason"
    [[ "$SURFACE" == "next-up" ]] && spawn_waiter
    deferred_streak="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("deferred_streak", 1))
except Exception:
    print(1)
PY
)"
    printf '{"status":"deferred","reason":"%s","surface":"%s","workspace_ref":"%s","wait_seconds":%s,"deferred_streak":%s,"pending_reports":%s,"waiter":%s,"next_action":"%s"}\n' \
        "$defer_reason" "$SURFACE" "$target_ref" "$send_wait" "$deferred_streak" "$pending_report_count" "$waiter" \
        "$([[ "$waiter" == "true" ]] && printf 'waiter posts when CoS goes idle; nothing was typed' || printf 'retry next tick; nothing was typed')"
fi
