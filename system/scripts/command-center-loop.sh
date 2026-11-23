#!/usr/bin/env bash
#
# command-center-loop.sh
#
# Background loop: launchd ticks every 10 minutes. Each tick generates the
# CoS command-center digest and:
#   1. Writes it to system/state/command-center-latest.md   (always, passive)
#   2. Updates the CoS tab status badge / log line          (always, passive)
#
# PASSIVE BY DEFAULT (automation-routing policy): deterministic
# freshness lives in the file and badge. One user-approved exception exists:
# during working hours, post-next-up-digest.sh may send one compact, deduped
# digest every 30 minutes through the shared inject guard. A busy CoS defers
# delivery to a later loop tick. Pending direct-origin callbacks also retry
# through report-to-cos.sh. No other loop output enters dispatch-surface chat.
#
# Usage:
#   nohup bash system/scripts/command-center-loop.sh > /tmp/cmd-center-loop.log 2>&1 &
#
# Stop:
#   pkill -f command-center-loop.sh
#
# Status:
#   ps aux | grep command-center-loop | grep -v grep
#
# Optional: install as a launchd job (more reliable across reboots) — see
# `system/launchd/com.switchboard.command-center.plist` once written.

set -euo pipefail

# REPO_ROOT / CMUX / DIGEST_SCRIPT are env-overridable so the focused test
# suite (test-command-center-inject.sh) can run hermetically against a fixture.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DIGEST_SCRIPT="${DIGEST_SCRIPT:-$REPO_ROOT/system/scripts/command-center-digest.py}"
STATE_DIR="${STATE_DIR:-$REPO_ROOT/system/state}"
LATEST_FILE="$STATE_DIR/command-center-latest.md"
HISTORY_DIR="$STATE_DIR/command-center-history"
CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

INTERVAL_SECONDS="${INTERVAL_SECONDS:-600}"   # 10 min default (refresh cadence)
HISTORY_RETENTION_COUNT="${HISTORY_RETENTION_COUNT:-144}" # 24h at 10m cadence
COS_NAME_MATCH="🧭 CoS"

mkdir -p "$STATE_DIR" "$HISTORY_DIR"

# Timestamp for the tick's log lines. On Git Bash the $(date) form is two leaked
# processes per line; printf has it built in there.
tick_stamp() {
  if [[ -n "${MSYSTEM:-}" ]]; then
    TZ=UTC printf '%(%Y-%m-%dT%H:%M:%SZ)T ' -1
  else
    printf '%s ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
}

# One tick at a time. Under memory pressure a tick can run for hours while
# Task Scheduler keeps starting new ones every 10 minutes, and overlapping ticks
# starve the machine. A --once tick exits while a
# younger tick still holds the lock; a holder that is gone or older than
# CC_TICK_STALE_SECONDS (30 min) is taken over. CC_TICK_GUARD_DISABLED=1 skips it.
TICK_LOCK="$STATE_DIR/command-center-tick.lock"
# A skipped tick still rewrites the action map: one listing, no render, so
# `close <n>` never resolves against a map that misses tabs opened since the
# last render. wmux only:
# cmux reuses numbers, so a rewrite between renders would let a number from the
# digest the owner is reading pass the identity check for a newer tab. On
# Windows the user environment and switchboard-env.sh export SWITCHBOARD_MUX=wmux.
# CC_SKIP_MAP_REFRESH_DISABLED=1 turns it off.
refresh_actions_map_on_skip() {
  [[ "${CC_SKIP_MAP_REFRESH_DISABLED:-0}" == "1" ]] && return 0
  [[ "${SWITCHBOARD_MUX:-}" == "wmux" ]] || return 0
  if CMUX_BIN="${CMUX_BIN:-$CMUX}" python3 "$DIGEST_SCRIPT" --actions-map-only \
      --actions-file "$STATE_DIR/command-center-actions.json" >/dev/null 2>&1; then
    echo "command-center tick: action map refreshed"
  else
    echo "command-center tick: action map refresh failed; the last map stays"
  fi
}
if [[ " $* " == *" --once "* && "${CC_TICK_GUARD_DISABLED:-0}" != "1" ]]; then
  stale_after="${CC_TICK_STALE_SECONDS:-1800}"
  tick_now="$(date +%s)"
  if ! mkdir "$TICK_LOCK" 2>/dev/null; then
    # The owner file is written whole (temp file, then mv). While it is missing
    # or unreadable the holder is mid-write, so the lock counts as held until
    # the directory itself is older than the stale limit.
    read -r holder since 2>/dev/null < "$TICK_LOCK/owner" || { holder=""; since=""; }
    if [[ -z "$holder" || -z "$since" ]]; then
      # GNU stat first (-c %Y), then BSD/macOS (-f %m): macOS `date -r` reads epoch seconds, not a file.
      lock_mtime="$(stat -c %Y "$TICK_LOCK" 2>/dev/null || stat -f %m "$TICK_LOCK" 2>/dev/null || echo "$tick_now")"
      dir_age=$(( tick_now - lock_mtime ))
      if (( dir_age < stale_after )); then
        echo "command-center tick: another tick is starting; skipping"; refresh_actions_map_on_skip; exit 0
      fi
    elif kill -0 "$holder" 2>/dev/null && (( tick_now - since < stale_after )); then
      echo "command-center tick: previous tick (pid $holder) still running; skipping"
      refresh_actions_map_on_skip
      exit 0
    fi
    echo "command-center tick: taking over a stale tick lock (pid ${holder:-?})"
  fi
  mkdir -p "$TICK_LOCK" 2>/dev/null || true
  if echo "$$ $tick_now" > "$TICK_LOCK/owner.$$" 2>/dev/null; then
    mv -f "$TICK_LOCK/owner.$$" "$TICK_LOCK/owner" 2>/dev/null || true
  fi
  trap '[[ "$(cut -d" " -f1 "$TICK_LOCK/owner" 2>/dev/null)" == "$$" ]] && rm -rf "$TICK_LOCK" || true' EXIT
fi

# A redirected STATE_DIR (fixtures, tests) must redirect every side effect of
# the sweep too, or prune_orphan_worktrees can park a live worker's checkout
# while a test runs against a fixture ledger.
if [[ "$STATE_DIR" != "$REPO_ROOT/system/state" ]]; then
  export SWITCHBOARD_WORKTREES_DIR="${SWITCHBOARD_WORKTREES_DIR:-$STATE_DIR/worktrees}"
  export SWITCHBOARD_LOCKS_DIR="${SWITCHBOARD_LOCKS_DIR:-$STATE_DIR/locks}"
fi

# Find the CoS workspace ref each loop (in case it changes)
find_cos_ws() {
  "$CMUX" list-workspaces 2>/dev/null | awk -v pat="$COS_NAME_MATCH" '
    {
      gsub(/^[ \t*]+/, "", $0)
      if (match($0, /^workspace:[0-9]+/)) {
        ref = substr($0, RSTART, RLENGTH)
        rest = substr($0, RSTART+RLENGTH)
        if (index(rest, pat)) {
          print ref
          exit
        }
      }
    }
  '
}

# When a refresh fails we keep the last good digest but stamp it STALE at the
# top, so nobody (CoS included) trusts its agent states or numeric handles.
# The next successful refresh rewrites the file wholesale, clearing the banner.
mark_latest_stale() {
  local ts="$1"
  [[ -f "$LATEST_FILE" ]] || return 0
  grep -q '^> ⚠️ STALE' "$LATEST_FILE" && return 0
  local tmp="$LATEST_FILE.stale.tmp"
  {
    echo "> ⚠️ STALE — refresh failed at $ts; showing the last good digest. Agent states and numeric open/close handles may be wrong (workspace numbers get reused) — ask for a fresh digest before acting."
    echo
    cat "$LATEST_FILE"
  } > "$tmp"
  mv "$tmp" "$LATEST_FILE"
}

# Retry pending terminal callbacks (every origin; see routing note below). The callback
# remains guarded and idempotent: report-to-cos.sh sends only while the origin
# is safe, and a concurrent/delivered duplicate becomes a no-op.
retry_pending_direct_reports() {
  local ts="$1"
  local state_file="$STATE_DIR/running-tasks.json"
  local report_script="$REPO_ROOT/system/scripts/report-to-cos.sh"
  [[ -f "$state_file" && -x "$report_script" ]] || return 0
  # Of each system-notice kind (auto-push, skill-sync, digest-stale, capacity) only the
  # newest pending one is delivered; older ones settle as superseded before the retry
  # pass sends any.
  python3 "$REPO_ROOT/system/scripts/report_queue.py" fold --origin cos >/dev/null 2>&1 || true

  local rows
  if ! rows="$(python3 - "$state_file" "$REPO_ROOT/system/scripts" "$STATE_DIR" <<'PY'
import json
import os
import sys
import time
from pathlib import Path

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as error:
    print(f"error: cannot read pending task reports: {error}", file=sys.stderr)
    raise SystemExit(1)

# A row a continuation took over, or one marked handled, has nothing left to say. It costs no
# report-to-cos.sh pass: the row is settled below. If this check cannot run, retries go on as before.
try:
    sys.path.insert(0, sys.argv[2])
    import report_settlement
    actions = report_settlement.load_actions(Path(sys.argv[3]))
    all_rows = [t for t in payload.get("tasks", []) if isinstance(t, dict)]
    moot_note = lambda row: report_settlement.moot_note(row, all_rows, actions)
except Exception as error:
    print(f"warning: report settlement check unavailable: {error}", file=sys.stderr)
    moot_note = lambda row: None

terminal = {"blocked", "deferred", "done", "failed", "cancelled"}
for task in payload.get("tasks", []):
    if not isinstance(task, dict):
        continue
    origin = str(task.get("origin") or "cos")
    if ((task.get("status") not in terminal and not task.get("admission_blocked"))
            or task.get("report_delivered") is not False):
        continue
    if task.get("report_delivery_unknown") is True:
        continue
    note = moot_note(task)
    if note:
        print("\x1f".join(str(value).replace("\n", " ") for value in (task.get("id"), "moot", origin, "", "", note, "", "")))
        continue
    # A plain done CoS row stored as digest normally comes back batched, so most retries
    # change nothing, and the digest reads the ledger itself. On Windows each retry leaks
    # many processes. Those rows retry once an hour, in the tick at the top
    # of the hour, so the first full report pass and the 24-hour settle still happen. An
    # urgent, needs-ruling or empty row is not in the digest, so it keeps every tick.
    if (os.name == "nt" and origin == "cos" and task.get("status") == "done" and task.get("delivery") == "digest"
            and task.get("summary") and not task.get("urgent") and not task.get("needs_ruling")
            and time.localtime().tm_min >= 15):
        continue
    # A terminal row reports its own status; admission_blocked is only meaningful
    # (and only reported as "blocked") while the row is still non-terminal.
    fields = (
        task.get("id"),
        task.get("status") if task.get("status") in terminal else "blocked",
        origin,
        task.get("workstream"),
        task.get("kind"),
        task.get("summary"),
        task.get("attempt_id"),
        task.get("thread_id") or (task.get("executor") or {}).get("thread_id"),
    )
    print("\x1f".join(str(value or "").replace("\x1f", " ").replace("\n", " ") for value in fields))
PY
  )"; then
    echo "[loop $ts] direct callback retry scan failed; pending reports preserved" >&2
    return 1
  fi

  local task_id status origin workstream kind summary attempt_id thread_id output
  local -a report_args
  local rc=0
  # A CoS report that is still batched for the next digest changes nothing this
  # tick; one summary line replaces its full JSON, so the log stays small.
  local batched_count=0
  while IFS=$'\x1f' read -r task_id status origin workstream kind summary attempt_id thread_id; do
    # Python prints CRLF on Windows and read keeps the CR on the last field, which
    # each retry would write back to the row.
    thread_id="${thread_id%$'\r'}"
    [[ -n "$task_id" ]] || continue
    # A cancelled row has no report (the waiter and reap agree): settle it and send
    # nothing. Otherwise each tick retries it and spawns a waiter for it, and the
    # first idle CoS takes every stale line.
    if [[ "$status" == "cancelled" ]]; then
      RUNNING_TASKS_FILE="$state_file" python3 "$REPO_ROOT/system/scripts/running-task-state.py" report-delivered         --task-id "$task_id" --note "settled without send: a cancelled row has no report" >/dev/null 2>&1         || echo "[loop $ts] could not settle cancelled row $task_id" >&2
      continue
    fi
    if [[ "$status" == "moot" ]]; then
      RUNNING_TASKS_FILE="$state_file" python3 "$REPO_ROOT/system/scripts/running-task-state.py" report-delivered --task-id "$task_id" --note "$summary" >/dev/null 2>&1 \
        || echo "[loop $ts] could not settle moot report $task_id" >&2
      continue
    fi
    report_args=(
      --task-id "$task_id"
      --origin "$origin"
      --status "$status"
      --wait-seconds 0
    )
    # Non-CoS origins have no digest channel: always force a direct callback.
    # With router.yaml terminal_report_delivery: direct, report-to-cos.sh sends every terminal worker report direct, so
    # this pass retries each one until it is proven in CoS; only explicit digest
    # policy rows still batch. Earlier rule, kept for terminal_report_delivery:
    # auto: CoS-origin pending reports keep report-to-cos.sh's own routing: a
    # non-priority done report returns batched:true without sending, in or
    # out of working hours (after-hours batching), so this never bypasses the
    # digest. Blocked, failed, urgent, needs-ruling and stored --delivery direct
    # rows are pasted here when CoS is idle.
    [[ "$origin" != "cos" ]] && report_args+=(--delivery direct)
    [[ "$kind" == "workspace-report" ]] && report_args+=(--report-kind workspace)
    [[ -n "$workstream" ]] && report_args+=(--workstream "$workstream")
    [[ -n "$attempt_id" ]] && report_args+=(--attempt-id "$attempt_id")
    [[ -n "$thread_id" ]] && report_args+=(--thread-id "$thread_id")
    if output="$(bash "$report_script" "${report_args[@]}" "$summary" 2>&1)"; then
      output="${output//$'\n'/ }"
      if [[ "$output" == *'"delivered": false'* && "$output" == *'"batched": true'* ]]; then
        ((batched_count += 1))
      else
        echo "[loop $ts] direct callback retry: $output"
      fi
    else
      output="${output//$'\n'/ }"
      echo "[loop $ts] direct callback retry failed for $task_id; pending report preserved: $output" >&2
      rc=1
    fi
  done <<< "$rows"
  (( batched_count == 0 )) || echo "[loop $ts] direct callback retry: $batched_count report(s) still batched for the next digest"
  return "$rc"
}

# Run CoS batch delivery independently from command-center rendering. A stale
# or failed command-center refresh must never suppress terminal task results.
post_next_up_digest() {
  local ts="$1"
  local post_script="$REPO_ROOT/system/scripts/post-next-up-digest.sh"
  [[ -x "$post_script" ]] || return 0

  local output
  if output="$(CMUX_BIN="$CMUX" bash "$post_script" 2>&1)"; then
    output="${output//$'\n'/ }"
    echo "[loop $ts] next-up delivery: $output"
    return 0
  else
    local rc=$?
    output="${output//$'\n'/ }"
    echo "[loop $ts] next-up delivery failed; pending reports preserved: $output" >&2
    return "$rc"
  fi
}

# The drafted-responses review posts only into the 📨 Comms Triage workspace
# (never CoS), on the same 30-minute working-hours cadence.
post_comms_review() {
  local ts="$1"
  local post_script="$REPO_ROOT/system/scripts/post-comms-review.sh"
  [[ -x "$post_script" ]] || return 0
  # shellcheck source=lib/comms-triage.sh
  source "$REPO_ROOT/system/scripts/lib/comms-triage.sh"
  switchboard_comms_triage_enabled || return 0

  local output
  if output="$(CMUX_BIN="$CMUX" bash "$post_script" 2>&1)"; then
    output="${output//$'\n'/ }"
    echo "[loop $ts] comms-review delivery: $output"
    return 0
  else
    local rc=$?
    output="${output//$'\n'/ }"
    echo "[loop $ts] comms-review delivery failed; held drafts preserved: $output" >&2
    return "$rc"
  fi
}

run_delivery_sweep() {
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local rc=0
  retry_pending_direct_reports "$ts" || rc=1
  post_next_up_digest "$ts" || rc=1
  post_comms_review "$ts" || rc=1
  return "$rc"
}

run_once() {
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local snapshot="$HISTORY_DIR/$ts.md"

  if ! python3 "$DIGEST_SCRIPT" --format markdown > "$snapshot.tmp"; then
    echo "[loop $ts] digest script failed" >&2
    rm -f "$snapshot.tmp"
    mark_latest_stale "$ts"
    return 1
  fi
  mv "$snapshot.tmp" "$snapshot"
  cp "$snapshot" "$LATEST_FILE"

  # dispatch.py sweep below owns worker liveness.

  # State-inventory shrink check: counts people/,
  # .state/, .firecrawl/, system/archive/ and the two JSON ledgers against the
  # previous tick; a drop, a missing path or a symlink sends one urgent CoS line
  # (at most hourly) and shows in the digest. Holds nothing. Runs before the
  # sweep so a deletion is noticed before anything else acts on the tree.
  # Kill switch: INVENTORY_CHECK_DISABLED=1. Never breaks the loop.
  if [[ "${INVENTORY_CHECK_DISABLED:-0}" != "1" && -f "$REPO_ROOT/system/scripts/check-state-inventory.py" ]]; then
    [[ -d "$STATE_DIR/worker-dispatch" ]] || mkdir -p "$STATE_DIR/worker-dispatch"
    { tick_stamp; INVENTORY_REPO_ROOT="$REPO_ROOT" \
      python3 "$REPO_ROOT/system/scripts/check-state-inventory.py" 2>&1 || true; } >>"$STATE_DIR/worker-dispatch/inventory.log"
  fi

  # Python dependency check: the PATH python3 must
  # import everything in system/requirements.txt. A package manager can re-point
  # python3 on each minor bump and drop installed modules; the checker is stdlib-only so it still runs on the broken interpreter and sends
  # one CoS line (at most hourly) with the reinstall command. Holds nothing.
  # Kill switch: PYTHON_DEPS_CHECK_DISABLED=1. Never breaks the loop.
  if [[ "${PYTHON_DEPS_CHECK_DISABLED:-0}" != "1" && -f "$REPO_ROOT/system/scripts/check-python-deps.py" ]]; then
    [[ -d "$STATE_DIR/worker-dispatch" ]] || mkdir -p "$STATE_DIR/worker-dispatch"
    { tick_stamp; PYDEPS_REPO_ROOT="$REPO_ROOT" \
      python3 "$REPO_ROOT/system/scripts/check-python-deps.py" 2>&1 || true; } >>"$STATE_DIR/worker-dispatch/python-deps.log"
  fi

  # Stale next-up digest alarm: reports pending
  # for the digest and no digest posted for more than one working day sends
  # one direct CoS line per day through the same report-to-cos.sh path as the
  # inventory check, so a stalled digest is never silent. Kill switch: DIGEST_STALE_CHECK_DISABLED=1.
  # Never breaks the loop.
  if [[ "${DIGEST_STALE_CHECK_DISABLED:-0}" != "1" && -f "$REPO_ROOT/system/scripts/check-digest-staleness.py" ]]; then
    [[ -d "$STATE_DIR/worker-dispatch" ]] || mkdir -p "$STATE_DIR/worker-dispatch"
    { tick_stamp; DIGEST_STALE_REPO_ROOT="$REPO_ROOT" \
      RUNNING_TASKS_FILE="$STATE_DIR/running-tasks.json" \
      NEXT_UP_STATE_FILE="$STATE_DIR/next-up-post.json" DIGEST_STALE_ALARM_FILE="$STATE_DIR/next-up-stale-alarm.json" \
      python3 "$REPO_ROOT/system/scripts/check-digest-staleness.py" 2>&1 || true; } >>"$STATE_DIR/worker-dispatch/digest-stale.log"
  fi

  # Profile tick jobs: a profile-specific job lives in its
  # profile as system/scripts/profile-tick.d/<job>.sh, which apply-profile.py copies
  # here only while that profile is active, so it cannot run on any other machine.
  # The check skips the fork where no profile has any. Never breaks the loop.
  if [[ -d "$REPO_ROOT/system/scripts/profile-tick.d" ]]; then
    REPO_ROOT="$REPO_ROOT" STATE_DIR="$STATE_DIR"       bash "$REPO_ROOT/system/scripts/run-profile-tick-hooks.sh" || true
  fi


  # Task-ledger liveness: expire lapsed leases in
  # running-tasks.json (marks liveness=stale only when cmux does not show the
  # executor Running; never re-dispatches) and append a "stale tasks" section
  # so a dead worker is visible with its recovery action. Kill switch:
  # LEDGER_LIVENESS_DISABLED=1. Never breaks the loop.
  if [[ "${LEDGER_LIVENESS_DISABLED:-0}" != "1" ]]; then
    local ledger_md=""
    # dispatch.py sweep: expire leases, release
    # waiting tasks, reap finished worker tabs whose report landed, reconcile
    # blocked receipts, and nudge idle workers whose branch has not landed
    # (one resume, then one line to the origin). Never relaunches a worker.
    { tick_stamp; RUNNING_TASKS_FILE="$STATE_DIR/running-tasks.json" \
      python3 "$REPO_ROOT/system/scripts/dispatch.py" sweep 2>&1 || true; } >>"$STATE_DIR/worker-dispatch/sweep.log"
    # Parked-draft reconcile: a sent or dismissed
    # ledger draft completes its mirrored todo, a done/dropped todo settles the
    # draft, snoozes travel both ways, so Needs-you reads one state.
    if [[ "${PARKED_DRAFT_RECONCILE_DISABLED:-0}" != "1" && -f "$REPO_ROOT/system/scripts/parked_draft_tracker.py" ]]; then
      { tick_stamp; printf 'reconcile ';
        (cd "$REPO_ROOT" && python3 system/scripts/parked_draft_tracker.py reconcile --create-missing --json) 2>&1 || true; printf '\n'; } \
        >>"$STATE_DIR/worker-dispatch/parked-drafts.log"
    fi
    # Owner-action to-dos: an answered question, or a task that ended with no report, closes
    # the to-do it raised. Starts no to-do CLI unless one needs closing.
    { tick_stamp; printf 'reconcile ';
      (cd "$REPO_ROOT" && python3 system/scripts/owner_action_capture.py reconcile) 2>&1 || true; printf '\n'; } \
      >>"$STATE_DIR/worker-dispatch/owner-actions.log"
    # Already-answered reconcile (messages that already have
    # responses are never shown as asks): a held draft
    # that now classifies no_ask, settled, stale, or answered is superseded
    # in the triage ledger with the classification as the reason and its
    # mirror todo is completed, so --pending, the digests, start-day seeds,
    # and the CoS Review feed agree. Logs the ids it closes.
    if [[ "${ALREADY_ANSWERED_RECONCILE_DISABLED:-0}" != "1" ]]; then
      { tick_stamp; printf 'reconcile ';
        (cd "$REPO_ROOT" && python3 system/scripts/reply_check.py reconcile --budget 20 --json) 2>&1 || true; printf '\n'; } \
        >>"$STATE_DIR/worker-dispatch/already-answered.log"
    fi
    ledger_md="$(RUNNING_TASKS_FILE="$STATE_DIR/running-tasks.json" \
      python3 "$REPO_ROOT/system/scripts/running-task-state.py" summary 2>/dev/null \
      | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
stale = data.get("stale") or []
if not stale:
    sys.exit(0)
print("### ⏳ Stale routed tasks — verify before retry (" + str(len(stale)) + ")")
print()
for t in stale:
    who = t.get("workstream") or t.get("origin") or "?"
    where = t.get("workstream_workspace_ref") or t.get("router_workspace_ref") or ""
    since = t.get("stale_since") or t.get("updated_at") or ""
    line = "- `" + str(t.get("id") or t.get("task_id") or "?") + "` " + who
    if where:
        line += " (" + where + ")"
    line += " — attempt " + str(t.get("attempt") or 1) + ", stale since " + since
    print(line)
    action = t.get("recovery_action")
    if action:
        print("  - " + action)
' 2>/dev/null || true)"
    if [[ -n "$ledger_md" ]]; then
      { echo; echo "$ledger_md"; } >> "$LATEST_FILE"
    fi
  fi

  # CoS Review feed: rebuilds the one JSON the
  # CoS Review menu bar app reads (system/state/cos-review-feed.json) from the
  # ledger, held triage drafts and the reply_check cache. Read only apart from
  # that file; no network. Runs after the sweep so a report that just landed
  # shows in the same tick. Kill switch: COS_REVIEW_FEED_DISABLED=1. Never
  # breaks the loop. The same wrapper runs from run-comms-fastpath.sh with a
  # 300 s throttle, so between ticks the feed stays under 5 min old.
  if [[ -f "$REPO_ROOT/system/scripts/cos-review-feed-rebuild.sh" ]]; then
    REPO_ROOT="$REPO_ROOT" STATE_DIR="$STATE_DIR" bash "$REPO_ROOT/system/scripts/cos-review-feed-rebuild.sh" --reason loop || true
  fi

  # Hourly push of main. dispatch.py auto-push holds the whole gate: 60 min
  # since the last attempt, no landing lock or rebase in flight, fetch, main
  # strictly ahead of origin/main, no tracked sensitive path; then
  # `git push origin main:main`, never --force, never another branch. Success
  # is the log line plus one "pushed N commits" line in the next-up Updates
  # section; failure or divergence sends one CoS line. Runs after the sweep so
  # a landing the sweep just finished is pushed in the same tick. Only the
  # real state dir pushes: a redirected STATE_DIR is a fixture.
  # Kill switch: AUTO_PUSH_DISABLED=1. Never breaks the loop.
  # Off by default: switchboard stays local and has no origin remote;
  # only SWITCHBOARD_AUTO_PUSH=on turns the hourly push back on.
  if [[ "${SWITCHBOARD_AUTO_PUSH:-off}" == "on" && "${AUTO_PUSH_DISABLED:-0}" != "1" && "$STATE_DIR" == "$REPO_ROOT/system/state" ]]; then
    [[ -d "$STATE_DIR/logs" ]] || mkdir -p "$STATE_DIR/logs"
    { tick_stamp; REPO_ROOT="$REPO_ROOT" \
      python3 "$REPO_ROOT/system/scripts/dispatch.py" auto-push 2>&1 || true; } >>"$STATE_DIR/logs/auto-push.tick.log"
  fi

  # Skill sync:
  # copies changed skills from .agents/skills, local/skills, system/vendor/skills
  # and any catalog skills into ~/.claude/skills, which Claude and Amp read; applies
  # the todo override first; saves an edited installed copy to local/skill-edits
  # before replacing it; never deletes more than 5 at once. sync-skills.py always
  # uses the main checkout, skips while a landing or rebase is in flight, writes
  # settings.json only when the manifest's listing changes, and sends CoS one line
  # for news or three failed runs in a row. Only the real state dir syncs: a
  # redirected STATE_DIR is a fixture. Kill switch: SKILL_SYNC_DISABLED=1.
  if [[ "${SKILL_SYNC_DISABLED:-0}" != "1" && "$STATE_DIR" == "$REPO_ROOT/system/state" \
        && -f "$REPO_ROOT/system/scripts/sync-skills.py" ]]; then
    [[ -d "$STATE_DIR/logs" ]] || mkdir -p "$STATE_DIR/logs"
    { tick_stamp; PYTHONIOENCODING=utf-8 python3 "$REPO_ROOT/system/scripts/sync-skills.py" --reason tick --notify 2>&1 || true; } \
      >>"$STATE_DIR/logs/skill-sync.log"
  fi

  # Plugin copy refresh: Claude Code runs the switchboard plugin from a copy made at install time,
  # so hook fixes on main never reach it on their own. sync-claude-plugin.py copies what changed,
  # holds an installed file edited by hand, keeps the Windows hook profile, and skips during a
  # rebase. Same fixture rule as skill sync. Kill switch: PLUGIN_SYNC_DISABLED=1.
  if [[ "${PLUGIN_SYNC_DISABLED:-0}" != "1" && "$STATE_DIR" == "$REPO_ROOT/system/state" \
        && -f "$REPO_ROOT/system/scripts/sync-claude-plugin.py" ]]; then
    [[ -d "$STATE_DIR/logs" ]] || mkdir -p "$STATE_DIR/logs"
    { tick_stamp; PYTHONIOENCODING=utf-8 python3 "$REPO_ROOT/system/scripts/sync-claude-plugin.py" --repo "$REPO_ROOT" 2>&1 || true; } \
      >>"$STATE_DIR/logs/plugin-sync.log"
  fi

  local cos_ref
  cos_ref="$(find_cos_ws || true)"
  if [[ -n "$cos_ref" ]]; then
    # Update passive workspace chrome (badge, log, description) — never breaks.
    local p0_count p1_count hhmm
    p0_count=$(grep -c "🔴 P0" "$LATEST_FILE" || true)
    p1_count=$(grep -c "🟠 P1" "$LATEST_FILE" || true)
    hhmm="$(date '+%H:%M')"
    "$CMUX" set-status command-center "${hhmm} · ${p0_count} P0 · ${p1_count} P1" \
      --workspace "$cos_ref" --icon compass --color "#3b82f6" \
      >/dev/null 2>&1 || true
    "$CMUX" log --source command-center --workspace "$cos_ref" \
      "${hhmm} · ${p0_count} P0 / ${p1_count} P1 — digest refreshed" \
      >/dev/null 2>&1 || true

    # Keep the Router task count durable in CoS chrome. This is passive UI
    # state only; report-to-cos.sh owns guarded chat delivery.
    if [[ -x "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" ]]; then
      CMUX_BIN="$CMUX" bash "$REPO_ROOT/system/scripts/publish-running-tasks-status.sh" \
        >/dev/null 2>&1 || true
    fi

    echo "[loop $ts] chrome updated (passive) for $cos_ref"
  else
    echo "[loop $ts] CoS workspace not open — wrote $LATEST_FILE only"
  fi

  # Prune old snapshots after a full day at the current 10-minute cadence.
  ls -1t "$HISTORY_DIR"/*.md 2>/dev/null \
    | tail -n "+$((HISTORY_RETENTION_COUNT + 1))" \
    | xargs -I {} rm -f {}
}

if [[ "${1:-}" == "--retry-reports-only" ]]; then
  retry_pending_direct_reports "$(date -u +%Y%m%dT%H%M%SZ)"
  exit $?
fi

# --once is one launchd or Task Scheduler tick. A failed digest or delivery sweep
# exits nonzero so the scheduler records the failure. launchd StartInterval jobs
# just run again at the next interval, so a nonzero exit causes no throttling.
# The long-running loop below never exits on a failed tick.
if [[ "${1:-}" == "--once" ]]; then
  rc=0
  run_once || rc=$?
  delivery_rc=0
  run_delivery_sweep || delivery_rc=$?
  if (( rc == 0 && delivery_rc != 0 )); then
    rc="$delivery_rc"
  fi
  exit "$rc"
fi

echo "[loop] starting; cadence ${INTERVAL_SECONDS}s; PID $$"
while true; do
  run_once || echo "[loop] run_once failed; continuing"
  run_delivery_sweep || echo "[loop] delivery sweep failed; continuing"
  sleep "$INTERVAL_SECONDS"
done
