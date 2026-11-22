#!/usr/bin/env bash
# report-to-cos.sh — one plain line from a worker to the surface that asked.
#
#   report-to-cos.sh --task-id ID [--attempt-id A] [--origin cos|launchpad]
#       [--workstream SLUG] [--thread-id T-...|<Claude session id>] [--workspace-ref workspace:N]
#       [--status done|blocked|failed|cancelled|deferred|active|routing]
#       [--delivery auto|direct|digest] [--urgent] [--needs-ruling]
#       [--evidence URL]... [--handles rt-...]... [--create-if-missing] '<one-line summary>'
#
# --handles rt-...: an earlier task row whose Needs-you ask this done report
# settles (the owner ruled in chat); its CoS Review rows move to Past as handled.
# The row this task continued (continuation_of) is handled without the flag.
#
# What the origin sees is exactly one line, led by the workstream's name
# (names, never bare numbers): "[🛠️ PM OS Maintenance · rt-xxxx] accepted; worker starting",
# "[… · rt-xxxx] done: …", "[… · rt-xxxx] waiting on <name> (rt-yyyy) because …",
# "[… · rt-xxxx] failed: …" or "[… · rt-xxxx] blocked: …". A done report from
# a worktree worker first lands its branch on main (dispatch.py finish); a real
# rebase conflict turns the report into "failed: rebase conflict on branch …".
# Output: one JSON line; delivered=true means the origin received the line,
# batched=true means it is queued for the CoS next-up digest.
#
# A summary that mentions a draft for the owner carries the full draft text as a
# ``` fenced code block (external-replies.md Rule 8). Write
# its line breaks as real newlines in the quoted argument or as " ⏎ "; the
# SUMMARY normalisation below turns real newlines into " ⏎ " (never spaces),
# review_surface.report_text lifts fenced blocks out whole and exempts them
# from the prose clip, and the newlines are re-encoded as " ⏎ " for the
# one-line transport, so the origin can print the draft verbatim. Runs of
# spaces inside a draft line do collapse to one (indentation is not kept).
#
# Images (external-replies Rule 14): every image path becomes a file:// link on
# main, and a summary that names 2 or more local images with no compare-page
# link gets one built by image_compare.py, whose link leads the image links.
#
# A terminal line is sent only while the destination tab is idle (proven
# delivery). If the tab is busy the row stays pending and one detached waiter
# (report-waiter.sh) replays this command the moment the turn ends; nothing is
# ever pasted into a live composer and nothing is routed to another surface.
#
# Quiet workstreams (router.yaml dispatch.quiet_done_workstreams): a done, cancelled or deferred cos-origin report
# from a listed workstream is logged and its row marked delivered, but no line
# reaches CoS, directly or through the digest Updates section. blocked, failed,
# --urgent and --needs-ruling still go out. Output carries quiet=true,
# routed_to=log, delivered=false.
#
# Env: REPORT_TO_COS_NO_HOOKS=1 skips finish/release/reap (tests, repair);
# REPORT_TO_COS_NO_WAITER=1 skips the waiter; PM_OS_STATE_DIR redirects the
# feed; REPORT_TO_COS_WAIT_SECONDS bounds the inline idle wait;
# REPORT_TO_COS_ROUTER_CONFIG points at another router.yaml (tests).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPTS="$REPO_ROOT/system/scripts"
STATE_DIR="${PM_OS_STATE_DIR:-$REPO_ROOT/system/state}"
ROUTER_CONFIG="${REPORT_TO_COS_ROUTER_CONFIG:-$REPO_ROOT/system/config/router.yaml}"
LEDGER="$SCRIPTS/running-task-state.py"
WAIT_SECONDS="${REPORT_TO_COS_WAIT_SECONDS:-5}"
NOTIFICATION_LOCK_DIR="${REPORT_TO_COS_LOCK_DIR:-$REPO_ROOT/system/state/next-up-post.json.lockdir}"
# Test sandbox: a test fixture must never reach the real CoS tab. A run whose ledger or state dir is not main's (tests, repair
# copies) is sandboxed: lib/mux.sh, the inject guard, lib/mux.py and the wmux
# shim then refuse every live cmux or wmux tab, so a row that exists only in a
# test ledger can never be delivered anywhere real. A task id with no row in
# the ledger in use is refused below (the ledger update exits 2, nothing sent).
if [[ "${PM_OS_TEST_SANDBOX:-0}" != "1" && ( -n "${RUNNING_TASKS_FILE:-}" || -n "${PM_OS_STATE_DIR:-}" ) ]]; then
    if ! python3 - "$SCRIPTS" "$REPO_ROOT" "${RUNNING_TASKS_FILE:-}" "${PM_OS_STATE_DIR:-}" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from repo_paths import main_checkout
state = (main_checkout(Path(sys.argv[2])) / "system" / "state").resolve()
same = lambda a, b: os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b))
ok = (not sys.argv[3] or same(sys.argv[3], state / "running-tasks.json")) and (not sys.argv[4] or same(sys.argv[4], state))
raise SystemExit(0 if ok else 1)
PY
    then
        export PM_OS_TEST_SANDBOX=1
    fi
fi
ORIGINAL_ARGS=("$@")   # a deferred terminal report is replayed verbatim by its waiter

TASK_ID="" ATTEMPT_ID="" ORIGIN="" WORKSTREAM="" THREAD_ID="" WORKSPACE_REF="" STATUS="done" DELIVERY="auto"
URGENT="false" NEEDS_RULING="false" CREATE_IF_MISSING="false" REPORT_KIND=""
EVIDENCE=()
HANDLES=()   # --handles rt-...: earlier task rows whose Needs-you ask this report settles (the owner ruled)
while (( $# )); do
    case "$1" in
        --task-id) TASK_ID="$2"; shift 2 ;;
        --handles) HANDLES+=("$2"); shift 2 ;;
        --attempt-id) ATTEMPT_ID="$2"; shift 2 ;;
        --origin) ORIGIN="$2"; shift 2 ;;
        --workstream) WORKSTREAM="$2"; shift 2 ;;
        --thread-id) THREAD_ID="$2"; shift 2 ;;
        --workspace-ref) WORKSPACE_REF="$2"; shift 2 ;;
        --status) STATUS="$2"; shift 2 ;;
        --delivery) DELIVERY="$2"; shift 2 ;;
        --urgent) URGENT="true"; shift ;;
        --needs-ruling) NEEDS_RULING="true"; shift ;;
        --evidence) EVIDENCE+=("$2"); shift 2 ;;
        --create-if-missing) CREATE_IF_MISSING="true"; shift ;;
        --report-kind) REPORT_KIND="$2"; shift 2 ;;   # "workspace": an owner-tab turn mirror (digest by default)
        --source-workspace|--wait-seconds|--expected-report-revision) shift 2 ;;  # legacy, ignored
        --replace-report) shift ;;  # legacy, ignored
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        --*) echo "error: unknown option: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done
# Real newlines become " ⏎ " (not spaces) so a fenced draft block keeps its
# line breaks through the one-line transport; ⏎ is not whitespace, so the
# collapse below leaves it alone and report_text re-expands it.
SUMMARY="$(printf '%s' "${*:-}" | tr -d '\r' | awk 'NR>1{printf " ⏎ "} {printf "%s", $0}' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"

# The ledger needs the literal cmux ref (workspace:N); reap matches tabs on it.
# Workers may pass titles or tab UUIDs, which reap cannot match, so anything else is resolved here from the worker's own CMUX_WORKSPACE_ID.
if [[ -n "$WORKSPACE_REF" && ! "$WORKSPACE_REF" =~ ^workspace:[0-9]+$ ]] || [[ -z "$WORKSPACE_REF" && -n "${CMUX_WORKSPACE_ID:-}" ]]; then
    cmux_bin="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
    resolved="$(CMUX_QUIET=1 "$cmux_bin" workspace list --json 2>/dev/null \
        | python3 -c 'import json,os,sys
want=os.environ.get("CMUX_WORKSPACE_ID") or ""
given=sys.argv[1]
for w in json.load(sys.stdin).get("workspaces", []):
    if w.get("id") in (want, given):
        print(w.get("ref") or ""); break' "$WORKSPACE_REF" 2>/dev/null || true)"
    if [[ "$resolved" =~ ^workspace:[0-9]+$ ]]; then
        [[ -n "$WORKSPACE_REF" ]] && echo "note: --workspace-ref '$WORKSPACE_REF' is not a cmux ref; using $resolved from CMUX_WORKSPACE_ID" >&2
        WORKSPACE_REF="$resolved"
    elif [[ -n "$WORKSPACE_REF" ]]; then
        echo "warning: --workspace-ref '$WORKSPACE_REF' is not workspace:N and could not be resolved; the tab may escape reap" >&2
    fi
fi
[[ -n "$SUMMARY" ]] || { echo "error: a one-line summary is required" >&2; exit 2; }
case "$STATUS" in done|blocked|failed|cancelled|deferred|active|routing) ;; *) echo "error: bad --status $STATUS" >&2; exit 2 ;; esac
case "$DELIVERY" in auto|direct|digest) ;; *) echo "error: --delivery must be auto, direct, or digest" >&2; exit 2 ;; esac
terminal="false"
case "$STATUS" in done|blocked|failed|cancelled|deferred) terminal="true" ;; esac

json_field() { python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else (str(v).lower() if isinstance(v,bool) else v))' "$1"; }

# A replay: the row is already terminal, so the ledger keeps its report unchanged and the send
# below uses that text. A loop pass or waiter replaying it must not run the checks again, because
# each pass would write another options_missing event. A report a
# continuation or a handled mark has made moot is settled here with nothing sent, so a waiter
# cannot type it into a CoS that just reopened.
replay="false"
if [[ "$terminal" == "true" && -n "$TASK_ID" ]] \
    && replay_row="$(python3 "$LEDGER" show --task-id "$TASK_ID" 2>/dev/null)" \
    && [[ "$(printf '%s' "$replay_row" | json_field _terminal)" == "true" ]] \
    && [[ -z "$ATTEMPT_ID" || "$ATTEMPT_ID" == "$(printf '%s' "$replay_row" | json_field attempt_id)" ]]; then
    replay="true"
    if moot_note="$(python3 "$SCRIPTS/report_settlement.py" check --task-id "$TASK_ID" --state-dir "$STATE_DIR" 2>/dev/null)"; then
        python3 "$LEDGER" report-delivered --task-id "$TASK_ID" --note "$moot_note" >/dev/null
        printf '%s' "$replay_row" | MOOT_NOTE="$moot_note" python3 -c '
import json, os, sys
t = json.load(sys.stdin)
print(json.dumps({"task_id": t.get("id"), "status": t.get("status"), "origin": t.get("origin") or "cos", "delivered": True,
                  "duplicate": True, "batched": False, "ignored": True, "reason": os.environ["MOOT_NOTE"]}))'
        exit 0
    fi
fi

# Review-surface gate: a deliverable the owner must review has to be a note inside the
# vault (or an approved live URL). Fails closed before any ledger write. The
# task id lets the gate accept a note that so far exists only in this task's
# worktree (finish lands it on main right after). A replay writes nothing new, so it skips
# the gate and every warning below that reads the gate's verdict.
gate_args=(check --status "$STATUS" --text "$SUMMARY")
[[ "$NEEDS_RULING" == "true" ]] && gate_args+=(--needs-ruling)
[[ -n "$TASK_ID" ]] && gate_args+=(--task-id "$TASK_ID")
if [[ "$replay" == "true" ]]; then
    gate_json='{}'
elif ! gate_json="$(python3 "$SCRIPTS/review_surface.py" "${gate_args[@]}")"; then
    GATE_JSON="$gate_json" GATE_TASK_ID="$TASK_ID" GATE_WORKSTREAM="$WORKSTREAM" GATE_STATUS="$STATUS" \
    python3 - "$STATE_DIR" "$SCRIPTS/events_stream.py" <<'PY' || true
import json, os, sys, subprocess
from datetime import datetime, timezone
verdict = json.loads(os.environ["GATE_JSON"])
event = {"type": "review_surface_rejected", "boundary": "report-to-cos",
         "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
         "task_id": os.environ.get("GATE_TASK_ID") or None, "workstream": os.environ.get("GATE_WORKSTREAM") or None,
         "status": os.environ["GATE_STATUS"], "reason": verdict.get("reason"), "offending": verdict.get("offending", [])}
subprocess.run([sys.executable, sys.argv[2], "append", "--state-dir", sys.argv[1], "--json", json.dumps(event)], check=False)
print(json.dumps({"task_id": event["task_id"], "status": "rejected", "delivered": False, "duplicate": False,
                  "batched": False, "review_gate": verdict}))
PY
    exit 2
fi
review_url="$(printf '%s' "$gate_json" | json_field review_url)"
# File before you promise (external-replies Rule 9): a report
# or the draft it carries that says PM OS is filing, tracking, or logging a
# record must carry that record's URL. The gate names the unbacked promise
# phrases; the report still goes through, with the warning in the summary so
# CoS does not send the draft and steers the worker to file first.
filing_warning="$(printf '%s' "$gate_json" | PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, sys
phrases = json.load(sys.stdin).get("filing_unbacked") or []
if phrases:
    from review_surface import filing_warning_text
    print(filing_warning_text(phrases))' 2>/dev/null || true)"
if [[ -n "$filing_warning" ]]; then
    echo "warning: $filing_warning" >&2
    SUMMARY="$SUMMARY ⏎ $filing_warning"
    FILING_TASK_ID="$TASK_ID" FILING_WORKSTREAM="$WORKSTREAM" FILING_STATUS="$STATUS" FILING_JSON="$gate_json" \
    python3 - "$STATE_DIR" "$SCRIPTS/events_stream.py" <<'PY' >/dev/null 2>&1 || true
import json, os, sys, subprocess
from datetime import datetime, timezone
verdict = json.loads(os.environ["FILING_JSON"])
event = {"type": "filing_promise_unbacked", "boundary": "report-to-cos",
         "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
         "task_id": os.environ.get("FILING_TASK_ID") or None, "workstream": os.environ.get("FILING_WORKSTREAM") or None,
         "status": os.environ["FILING_STATUS"], "phrases": verdict.get("filing_unbacked", [])}
subprocess.run([sys.executable, sys.argv[2], "append", "--state-dir", sys.argv[1], "--json", json.dumps(event)], check=False)
PY
fi
# Options and lean on decision reports: a
# blocked or --needs-ruling report lists its ways forward as
# "Options: (a) …; (b) …; (c) … Lean: <letter> because <reason>" so the CoS
# Review card and CoS show the same choice. The gate names what is missing
# (Options, Lean); the report still goes through, with the warning in the
# summary.
options_warning="$(printf '%s' "$gate_json" | PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, sys
missing = json.load(sys.stdin).get("options_missing") or []
if missing:
    from review_surface import options_warning_text
    print(options_warning_text(missing))' 2>/dev/null || true)"
if [[ -n "$options_warning" ]]; then
    echo "warning: $options_warning" >&2
    SUMMARY="$SUMMARY ⏎ $options_warning"
    OPTIONS_TASK_ID="$TASK_ID" OPTIONS_WORKSTREAM="$WORKSTREAM" OPTIONS_STATUS="$STATUS" OPTIONS_JSON="$gate_json" \
    python3 - "$STATE_DIR" "$SCRIPTS/events_stream.py" <<'PY' >/dev/null 2>&1 || true
import json, os, sys, subprocess
from datetime import datetime, timezone
verdict = json.loads(os.environ["OPTIONS_JSON"])
event = {"type": "options_missing", "boundary": "report-to-cos",
         "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
         "task_id": os.environ.get("OPTIONS_TASK_ID") or None, "workstream": os.environ.get("OPTIONS_WORKSTREAM") or None,
         "status": os.environ["OPTIONS_STATUS"], "missing": verdict.get("options_missing", [])}
subprocess.run([sys.executable, sys.argv[2], "append", "--state-dir", sys.argv[1], "--json", json.dumps(event)], check=False)
PY
fi
# Follow-up on done reports: a done report that leaves work open names it as
# "Follow-up: <what stays open>" so cos_review_feed builds a Needs You item.
# Open-work prose with no such line gets a warning in the summary; the report
# still goes through.
followup_warning="$(printf '%s' "$gate_json" | PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, sys
found = json.load(sys.stdin).get("followup_missing") or []
if found:
    from review_surface import followup_warning_text
    print(followup_warning_text(found))' 2>/dev/null || true)"
if [[ -n "$followup_warning" ]]; then
    echo "warning: $followup_warning" >&2
    SUMMARY="$SUMMARY ⏎ $followup_warning"
    FOLLOWUP_TASK_ID="$TASK_ID" FOLLOWUP_WORKSTREAM="$WORKSTREAM" FOLLOWUP_STATUS="$STATUS" FOLLOWUP_JSON="$gate_json" \
    python3 - "$STATE_DIR" "$SCRIPTS/events_stream.py" <<'PY' >/dev/null 2>&1 || true
import json, os, sys, subprocess
from datetime import datetime, timezone
verdict = json.loads(os.environ["FOLLOWUP_JSON"])
event = {"type": "followup_missing", "boundary": "report-to-cos",
         "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
         "task_id": os.environ.get("FOLLOWUP_TASK_ID") or None, "workstream": os.environ.get("FOLLOWUP_WORKSTREAM") or None,
         "status": os.environ["FOLLOWUP_STATUS"], "found": verdict.get("followup_missing", [])}
subprocess.run([sys.executable, sys.argv[2], "append", "--state-dir", sys.argv[1], "--json", json.dumps(event)], check=False)
PY
fi
# Images are links, never descriptions (external-replies Rule 14): report_text below turns every image path into a clickable file://
# link. A report that talks about a screenshot, contact sheet, cover option, or
# crop preview with no image link at all gets a warning; it still goes through.
image_warning="$(printf '%s' "$gate_json" | PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, sys
found = json.load(sys.stdin).get("image_unlinked") or []
if found:
    from review_surface import image_warning_text
    print(image_warning_text(found))' 2>/dev/null || true)"
if [[ -n "$image_warning" ]]; then
    echo "warning: $image_warning" >&2
    SUMMARY="$SUMMARY ⏎ $image_warning"
fi
# One compare page per set of images: a
# report that names 2 or more local images with no compare-page link gets one
# built by image_compare.py in this checkout (the worktree lands it on main) and
# its one link added; when the images cannot be found, a warning instead. The
# report still goes through. Kill switch: IMAGE_COMPARE_DISABLED=1.
compare_missing="$(printf '%s' "$gate_json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("compare_missing") or []))' 2>/dev/null || echo 0)"
if [[ "$compare_missing" =~ ^[0-9]+$ && "$compare_missing" -gt 0 ]]; then
    compare_json="$(PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 "$SCRIPTS/image_compare.py" from-report \
        --text="$SUMMARY" --workstream="$WORKSTREAM" --task-id "$TASK_ID" 2>/dev/null || true)"
    compare_link="$(printf '%s' "$compare_json" | json_field link 2>/dev/null || true)"
    if [[ -n "$compare_link" ]]; then
        echo "note: built compare page $(printf '%s' "$compare_json" | json_field path)" >&2
        SUMMARY="$SUMMARY ⏎ $compare_link"
    elif [[ "$(printf '%s' "$compare_json" | json_field reason 2>/dev/null)" != "disabled" ]]; then
        compare_warning="$(printf '%s' "$gate_json" | PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import json, sys
from review_surface import compare_warning_text
print(compare_warning_text(json.load(sys.stdin).get("compare_missing") or []))' 2>/dev/null || true)"
        if [[ -n "$compare_warning" ]]; then
            echo "warning: $compare_warning" >&2
            SUMMARY="$SUMMARY ⏎ $compare_warning"
        fi
    fi
fi
# Trash instead of delete: pm-os-guardrails Rule 3
# rewrites an outside `rm -rf` to pm-os-trash.py, which logs each move with the
# worker's task id. The final report lists them so nothing goes missing quietly.
if [[ "$replay" != "true" ]] && [[ "$STATUS" == "done" || "$STATUS" == "blocked" || "$STATUS" == "failed" ]]; then
    trash_line="$(python3 "$SCRIPTS/pm-os-trash.py" --pm-os-list --task-id "$TASK_ID" --format line 2>/dev/null || true)"
    if [[ -n "$trash_line" ]]; then
        SUMMARY="$SUMMARY ⏎ $trash_line"
    fi
fi
# One-line transport: the ledger summary keeps the review pointer as a
# protected " ⏎ "-separated suffix (review_surface.report_text re-expands it).
# Local links resolve when clicked: a done report lands its
# branch before delivery, so its worktree paths name main ("landing"); any other
# report (a parked ask, blocked, failed) does not land, so its links name the
# copy that exists now, the live worktree page included ("resolve").
link_mode="resolve"; [[ "$STATUS" == "done" ]] && link_mode="landing"
SUMMARY="$(PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import sys
try:
    from review_surface import report_text
    print(report_text(sys.argv[1], review_url=sys.argv[2], link_mode=sys.argv[3], task_id=sys.argv[4] or None).replace("\n", " ⏎ "))
except Exception:
    print(sys.argv[1])' "$SUMMARY" "$review_url" "$link_mode" "$TASK_ID")"

# Done is derived from git, not self-reported: a worktree worker's done report
# first lands its branch on main (dispatch.py finish). Only a landed branch may
# flip the row to done; a real rebase conflict opens the grace window below
# (dispatch.conflict_grace_seconds; "failed: …" with the branch name only when it
# lapses, or at once when the window is 0); anything else keeps the task in
# progress with the report as its summary (the ledger refuses the flip with
# _not_landed).
hooks="true"; [[ "${REPORT_TO_COS_NO_HOOKS:-0}" == "1" ]] && hooks="false"
landed_json=""; grace_until=""
if [[ "$hooks" == "true" && "$STATUS" == "done" && -n "$TASK_ID" ]] \
    && pre_json="$(python3 "$LEDGER" show --task-id "$TASK_ID" 2>/dev/null)" \
    && [[ -n "$(printf '%s' "$pre_json" | json_field branch)$(printf '%s' "$pre_json" | json_field worker_id)" && "$(printf '%s' "$pre_json" | json_field _terminal)" != "true" ]] \
    && [[ -z "$ATTEMPT_ID" || "$ATTEMPT_ID" == "$(printf '%s' "$pre_json" | json_field attempt_id)" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    landed_json="$(env -u PM_OS_WORKER_ID python3 "$SCRIPTS/dispatch.py" finish --task-id "$TASK_ID" 2>>"$STATE_DIR/worker-dispatch/finish.log" || true)"
    if [[ -n "$landed_json" && "$(printf '%s' "$landed_json" | json_field grace)" == "true" ]]; then
        # Rebase-conflict grace window: finish held the
        # row active with landing.state == "conflict" and told the worker in its tab.
        # The done report is stored by the ledger's _not_landed path below and
        # nothing reaches the origin now: a landing inside the window delivers it
        # once as done; a lapse makes dispatch.py sweep deliver the failed line.
        grace_until="$(printf '%s' "$landed_json" | json_field grace_until)"
    elif [[ -n "$landed_json" && "$(printf '%s' "$landed_json" | json_field landed)" != "true" && "$(printf '%s' "$landed_json" | json_field skipped)" != "true" ]]; then
        # The worker's deliverable stands (a Doc it updated is still updated); only
        # the landing on main failed. Say both, and keep the worker result parseable
        # so dispatch.py finish can correct the row to done once the branch lands.
        STATUS="failed"; terminal="true"
        SUMMARY="failed: could not land on main ($(printf '%s' "$landed_json" | json_field reason)). Worker result stands: $SUMMARY"
    fi
fi

# Ledger write (fenced by attempt id; stale or late reports are ignored, never sent).
state_args=(update --status "$STATUS" --summary "$SUMMARY")
[[ -n "$TASK_ID" ]] && state_args+=(--task-id "$TASK_ID")
[[ -n "$ATTEMPT_ID" ]] && state_args+=(--attempt-id "$ATTEMPT_ID")
[[ -n "$WORKSTREAM" ]] && state_args+=(--workstream "$WORKSTREAM")
[[ -n "$THREAD_ID" ]] && state_args+=(--thread-id "$THREAD_ID")
[[ -n "$WORKSPACE_REF" ]] && state_args+=(--workspace-ref "$WORKSPACE_REF")
[[ -n "$ORIGIN" ]] && state_args+=(--origin "$ORIGIN")
[[ "$URGENT" == "true" ]] && state_args+=(--urgent)
[[ "$NEEDS_RULING" == "true" ]] && state_args+=(--needs-ruling)
[[ "$CREATE_IF_MISSING" == "true" || -z "$TASK_ID" ]] && state_args+=(--create-if-missing)
# An explicit delivery policy (digest or direct) stays on the ledger so the
# loop's retry pass (which calls back with --delivery auto) keeps it: a
# digest-only row is never pasted directly, and a direct row is still pasted
# when the retry runs after hours (done reports otherwise batch after hours).
[[ "$DELIVERY" == "digest" || "$DELIVERY" == "direct" ]] && state_args+=(--delivery "$DELIVERY")
for e in ${EVIDENCE[@]+"${EVIDENCE[@]}"}; do state_args+=(--evidence "$e"); done
task_json="$(python3 "$LEDGER" "${state_args[@]}")"
TASK_ID="$(printf '%s' "$task_json" | json_field id)"
if [[ "$(printf '%s' "$task_json" | json_field _stale_attempt)" == "true" ]]; then
    printf '%s' "$task_json" | python3 -c '
import json, sys
t = json.load(sys.stdin)
late = bool(t.get("_late_active_report"))
print(json.dumps({"task_id": t.get("id"), "status": t.get("status"), "origin": t.get("origin") or "cos", "delivered": False,
                  "duplicate": False, "batched": False, "ignored": True, "stale": not late, "late_report": late,
                  "current_attempt_id": t.get("attempt_id"),
                  "reason": "task already settled; late report ignored" if late else "attempt superseded; result ignored"}))'
    exit 0
fi
if [[ "$(printf '%s' "$task_json" | json_field _live_worker)" == "true" ]]; then
    # Another caller tried to settle a row whose worker is live. Nothing was written.
    printf '%s' "$task_json" | python3 -c '
import json, sys
t = json.load(sys.stdin)
print(json.dumps({"task_id": t.get("id"), "status": t.get("status"), "origin": t.get("origin") or "cos", "delivered": False,
                  "duplicate": False, "batched": False, "ignored": True, "live_worker": True,
                  "live_thread_id": t.get("live_thread_id"), "live_workspace_ref": t.get("live_workspace_ref"),
                  "reason": "worker %s is live in %s; message it, report as that worker (--thread-id), or overrule with running-task-state.py update --replace-report"
                            % (t.get("live_thread_id"), t.get("live_workspace_ref") or "its tab")}))'
    exit 0
fi
if [[ "$(printf '%s' "$task_json" | json_field _not_landed)" == "true" ]]; then
    # Done is observable: the branch has not landed, so the row stays in progress
    # (the summary is kept as the worker's report) and nothing reaches the origin.
    printf '%s' "$task_json" | GRACE_UNTIL="$grace_until" LANDED_JSON="$landed_json" python3 -c '
import json, os, sys
t = json.load(sys.stdin)
out = {"task_id": t.get("id"), "status": t.get("status"), "origin": t.get("origin") or "cos", "delivered": False,
       "duplicate": False, "batched": False, "ignored": True, "not_landed": True, "branch": t.get("branch"),
       "git_state": t.get("git_state"),
       "reason": "branch %s has not landed on main; commit your work in the worktree and report done again" % t.get("branch")}
if os.environ.get("GRACE_UNTIL"):
    landed = json.loads(os.environ.get("LANDED_JSON") or "{}")
    out.update(landing="conflict", grace_until=os.environ["GRACE_UNTIL"],
               reason="rebase conflict; grace window open until %s UTC, report held: %s" % (os.environ["GRACE_UNTIL"], landed.get("reason")))
print(json.dumps(out))'
    exit 0
fi
# Parked drafts become tracked items. Every "Draft for the owner to send"
# in a terminal report becomes a triage-ledger draft (held, parked_by_task) with
# a mirrored P1 todo due the next business day, so `send N`, reply_check.py
# --pending, the next-up digest, start-day seeds and the CoS Review Needs-you
# pane all carry it until it is sent or dropped. Never blocks the report.
if [[ "$hooks" == "true" && "${REPORT_TO_COS_NO_TRACKER:-0}" != "1" && -n "$TASK_ID" && -f "$SCRIPTS/parked_draft_tracker.py" ]] \
    && [[ "$STATUS" == "done" || "$STATUS" == "blocked" || "$STATUS" == "deferred" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    tracker_summary="$(mktemp "${TMPDIR:-/tmp}/parked-draft.XXXXXX")"
    printf '%s' "$SUMMARY" >"$tracker_summary"
    { printf '%s %s ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TASK_ID";
      python3 "$SCRIPTS/parked_draft_tracker.py" track --task-id "$TASK_ID" --workstream "$WORKSTREAM" \
        --thread-id "$THREAD_ID" --workspace-ref "$WORKSPACE_REF" --status "$STATUS" \
        --summary-file "$tracker_summary" --json 2>&1 || true; printf '\n'; } >>"$STATE_DIR/worker-dispatch/parked-drafts.log"
    rm -f "$tracker_summary"
fi
duplicate="$(printf '%s' "$task_json" | json_field _duplicate_terminal)"
already_delivered="$(printf '%s' "$task_json" | json_field report_delivered)"
attempted_at="$(printf '%s' "$task_json" | json_field report_attempted_at)"
# One message per task (Phase 1 rule). A retry of a report the ledger already
# holds as terminal is re-sent at most once, only while it is fresh (< 24 h) and
# post-dates the slim-ledger migration, and only through the idle path; anything
# older, or already pasted once, is settled as delivered without a send.
# This keeps old rows from replaying into CoS.
# A report that waited on a closed CoS has not had its chance, so it stays pending
# for 7 days rather than being marked delivered unseen.
if [[ "$duplicate" == "true" && "$already_delivered" != "true" ]]; then
    stale="$(printf '%s' "$task_json" | REPORT_TO_COS_NOW="${REPORT_TO_COS_NOW:-}" python3 -c '
import json, os, sys
from datetime import datetime, timezone, timedelta
t = json.load(sys.stdin)
cutoff = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
def iso(v):
    try: return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception: return None
now = iso(os.environ.get("REPORT_TO_COS_NOW")) or datetime.now(timezone.utc)   # test seam, same as in_hours
finished = iso(t.get("finished_at") or t.get("completed_at") or t.get("updated_at"))
# Only an owner task report (rt-…) keeps 7 days: system notices (auto-push, skill-sync,
# digest-stale, capacity) keep 24 hours, so they never pile up beside fresh results; the
# newest of each kind is delivered first anyway (report_queue.py fold).
owner_task = str(t.get("id") or "").startswith("rt-")
window = timedelta(days=7) if t.get("report_no_destination_at") and owner_task else timedelta(hours=24)
old = finished is not None and (finished < cutoff or now - finished > window)
print(str(old).lower())')"
    if [[ "$stale" == "true" ]]; then
        python3 "$LEDGER" report-delivered --task-id "$TASK_ID" --note "settled without resend: stale duplicate terminal report" >/dev/null 2>&1 \
            || python3 "$LEDGER" report-delivered --task-id "$TASK_ID" >/dev/null
        printf '{"task_id":"%s","status":"%s","origin":"%s","delivered":true,"duplicate":true,"batched":false,"ignored":true,"reason":"stale duplicate terminal report settled; nothing sent"}\n' "$TASK_ID" "$STATUS" "${origin:-cos}"
        exit 0
    fi
fi
origin="$(printf '%s' "$task_json" | json_field origin)"; origin="${origin:-cos}"
slug="$(printf '%s' "$task_json" | json_field workstream)"
if [[ "$duplicate" == "true" ]]; then
    # A retry of a settled row is labelled by the row (done stays done even if a
    # stale caller still says blocked) and carries the row's summary.
    STATUS="$(printf '%s' "$task_json" | json_field status)"
    row_summary="$(printf '%s' "$task_json" | json_field summary)"; [[ -n "$row_summary" ]] && SUMMARY="$row_summary"
fi

# The one line the origin sees. Its lead names the workstream (named_refs.py
# lead → "[<emoji> <Name> · rt-…]"); the bare "[rt-… slug]" shape is the
# fallback only when the helper cannot run.
lead="$(python3 "$SCRIPTS/named_refs.py" lead "$TASK_ID" "$slug" 2>/dev/null || true)"
[[ -z "$lead" ]] && lead="[$TASK_ID${slug:+ $slug}]"
case "$STATUS" in
    done|blocked|failed|cancelled|deferred)
        body="$SUMMARY"
        shopt -s nocasematch
        [[ "$body" =~ ^\[?(ROUTER|WORKSPACE)\ REPORT\]?\ * ]] && body="${body#*] }"
        [[ "$body" =~ ^$STATUS: ]] && body="${body#*: }"
        shopt -u nocasematch
        report_line="$lead $STATUS: $body" ;;
    *)  report_line="$lead $SUMMARY" ;;
esac

# Destination and delivery policy.
# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
source_type="routine"; source_key="megadesk"
if normalized="$(python3 "$SCRIPTS/surface-registry.py" normalize "$origin" 2>/dev/null)"; then
    origin="$normalized"
    source_type="$(python3 "$SCRIPTS/surface-registry.py" get "$origin" source_type 2>/dev/null || echo routine)"
    source_key="$(python3 "$SCRIPTS/surface-registry.py" get "$origin" source_key 2>/dev/null || echo megadesk)"
fi
# The live CoS tab: every "delivered" report that day went to
# the tab titled 🧭 CoS, which still ran Amp, while the owner's CoS was a Claude tab
# titled "Start of day". owner_tabs.py cos-target reads the claim-cos
# registration first, then prefers a CoS-titled tab running router.yaml's agent.
# destination_agent decides the transport below: a Claude tab gets the report by
# cmux agent message, an Amp tab keeps the guarded idle-only paste.
destination_ref=""; destination_agent=""; destination_warning=""
if [[ "$source_key" == "megadesk" && "${REPORT_TO_COS_NO_OWNER_TABS:-0}" != "1" ]] \
    && cos_target_json="$(python3 "$SCRIPTS/owner_tabs.py" cos-target 2>/dev/null)"; then
    destination_ref="$(printf '%s' "$cos_target_json" | json_field ref)"
    destination_agent="$(printf '%s' "$cos_target_json" | json_field agent)"
    destination_warning="$(printf '%s' "$cos_target_json" | json_field warning)"
    [[ -n "$destination_warning" ]] && echo "warning: $destination_warning" >&2
fi
if [[ -z "$destination_ref" ]]; then
    destination_ref="$(find_workspace_ref_for_source_key "$source_type" "$source_key" 2>/dev/null || true)"
    if [[ -n "$destination_ref" && "${REPORT_TO_COS_NO_OWNER_TABS:-0}" != "1" ]]; then
        destination_agent="$(python3 "$SCRIPTS/owner_tabs.py" agent --ref "$destination_ref" 2>/dev/null | json_field agent 2>/dev/null || true)"
    fi
fi
# in_hours: the next-up digest only runs inside router.yaml next-up working
# hours. stale_batched: the row went terminal
# inside those hours more than one digest cadence ago, so the digest had its
# turn and missed it (a loop outage, or CoS busy through every tick). Such a
# row stays batched for the next digest's Updates section; it is never
# replayed as a standalone line after hours.
hours_json="$(python3 - "$ROUTER_CONFIG" "${REPORT_TO_COS_NOW:-}" "$(printf '%s' "$task_json" | json_field finished_at)" <<'PY'
import json, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
try:
    import yaml
    cfg = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("next-up") or {}
except Exception:
    cfg = {}
zone = ZoneInfo(str(cfg.get("timezone", "America/Los_Angeles")))
now = datetime.fromisoformat(sys.argv[2]).astimezone(zone) if sys.argv[2] else datetime.now(zone)
sh, sm = map(int, str(cfg.get("working_hours_start", "08:00")).split(":"))
eh, em = map(int, str(cfg.get("working_hours_end", "18:00")).split(":"))
def in_hours(t):
    m = t.hour * 60 + t.minute
    return t.isoweekday() <= 5 and sh * 60 + sm <= m < eh * 60 + em
stale = False
try:
    finished = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00"))
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=ZoneInfo("UTC"))
    finished = finished.astimezone(zone)
    cadence = timedelta(minutes=int(cfg.get("cadence_minutes", 30) or 30))
    stale = in_hours(finished) and now - finished > cadence
except (ValueError, TypeError, IndexError):
    pass
try:
    dcfg = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("dispatch") or {}
except Exception:
    dcfg = {}
terminal_delivery = str(dcfg.get("terminal_report_delivery") or "direct").lower()
try:
    alert_minutes = int(dcfg.get("report_undelivered_alert_minutes", 10) or 0)
except (TypeError, ValueError):
    alert_minutes = 10   # a bad value never stops a report
print(json.dumps({"in_hours": in_hours(now), "stale_batched": stale,
                  "terminal_delivery": terminal_delivery if terminal_delivery in ("direct", "auto") else "direct",
                  "alert_minutes": alert_minutes}))
PY
)"
in_hours="$(printf '%s' "$hours_json" | json_field in_hours)"
stale_batched="$(printf '%s' "$hours_json" | json_field stale_batched)"
terminal_delivery="$(printf '%s' "$hours_json" | json_field terminal_delivery)"; terminal_delivery="${terminal_delivery:-direct}"
alert_minutes="$(printf '%s' "$hours_json" | json_field alert_minutes)"; alert_minutes="${alert_minutes:-10}"
[[ "$(printf '%s' "$task_json" | json_field urgent)" == "true" ]] && URGENT="true"
[[ "$(printf '%s' "$task_json" | json_field needs_ruling)" == "true" ]] && NEEDS_RULING="true"
stored_delivery="$(printf '%s' "$task_json" | json_field delivery)"
[[ "$origin" == "cos" && "$STATUS" == "done" && "$DELIVERY" == "auto" && -n "$stored_delivery" ]] && DELIVERY="$stored_delivery"
# Claude CoS: a worker's done report with no stored policy goes direct
# when CoS runs Claude. cmux agent message never touches the composer, so the
# reason done reports batched (a paste could land in the owner's draft) is gone,
# and the digest that carried them deferred on every draft. A stored or
# explicit digest policy (start-of-day fan-out, automation) and owner-tab turn
# mirrors (--report-kind workspace, wr-* rows) still batch.
#
# Every terminal worker report goes direct: nonurgent, after hours and to an Amp CoS too. The
# next-up digest may still summarize it, never in its place. router.yaml
# dispatch.terminal_report_delivery: auto restores the old Claude-only rule.
if [[ "$origin" == "cos" && "$terminal" == "true" && "$DELIVERY" == "auto" \
      && ( "$terminal_delivery" == "direct" || "$destination_agent" == "claude" ) \
      && "$REPORT_KIND" != "workspace" && "$TASK_ID" != wr-* \
      && "$(printf '%s' "$task_json" | json_field kind)" != "workspace-report" ]]; then
    DELIVERY="direct"
fi
priority="false"
[[ "$STATUS" == "blocked" || "$STATUS" == "failed" || "$URGENT" == "true" || "$NEEDS_RULING" == "true" ]] && priority="true"
# Quiet workstream (router.yaml dispatch.quiet_done_workstreams): a non-priority
# terminal cos report is logged, never sent, never batched into the digest.
quiet="false"; quiet_reason=""
effective_workstream="${WORKSTREAM:-$(printf '%s' "$task_json" | json_field workstream)}"
if [[ "$origin" == "cos" && "$terminal" == "true" && "$priority" == "false" && -n "$effective_workstream" ]] \
    && python3 - "$ROUTER_CONFIG" "$effective_workstream" <<'PY'
import sys
try:
    import yaml
    cfg = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("dispatch") or {}
except Exception:
    cfg = {}
quiet = cfg.get("quiet_done_workstreams") or []
try:  # organisation profile additions (system/config/router.profile.yaml)
    import os
    extra_path = os.path.join(os.path.dirname(sys.argv[1]), "router.profile.yaml")
    if os.path.exists(extra_path):
        extra = (yaml.safe_load(open(extra_path, encoding="utf-8")) or {}).get("dispatch") or {}
        quiet = list(quiet) + list(extra.get("quiet_done_workstreams") or [])
except Exception:
    pass
raise SystemExit(0 if isinstance(quiet, list) and sys.argv[2] in quiet else 1)
PY
then
    quiet="true"; quiet_reason="quiet-workstream"
fi
# Locality acknowledgment: an owner tab's reply
# to the start-of-day seed ("Understood. Staying local on this machine,
# waiting for the workstream instructions."), mirrored by the workspace-report
# plugin as a done report. No deliverable, so same treatment as a quiet
# workstream: logged, settled, never sent and never batched.
if [[ "$quiet" != "true" && "$origin" == "cos" && "$terminal" == "true" && "$priority" == "false" ]] \
    && python3 "$SCRIPTS/report_quiet.py" locality-ack "$SUMMARY"; then
    quiet="true"; quiet_reason="locality-ack"
fi
direct="false"; routed_to="$origin"
if [[ "$quiet" == "true" ]]; then
    routed_to="log"
elif [[ "$origin" == "launchpad" || "$priority" == "true" || "$DELIVERY" == "direct" ]]; then
    direct="true"
elif [[ "$origin" == "cos" && "$terminal" != "true" ]]; then
    routed_to="log"   # routine progress stays off the CoS composer; the report log keeps it
fi
# Everything else is a non-priority terminal CoS report: batched for the
# next-up digest. After-hours done reports: a
# report outside next-up working hours is batched like an in-hours one. The
# row stays pending, the running-task badge shows it, and the next in-hours
# digest's Updates section carries it. Blocked, failed, urgent, needs-ruling,
# --delivery direct and a stored direct policy still go direct above.
# stale_batched stays in the JSON for callers that read it.

# Serialize with the next-up auto-post so one terminal result is accepted once.
have_lock="false"
mkdir -p "$(dirname "$NOTIFICATION_LOCK_DIR")"
for _ in $(seq 1 100); do
    if mkdir "$NOTIFICATION_LOCK_DIR" 2>/dev/null; then echo $$ > "$NOTIFICATION_LOCK_DIR/pid"; have_lock="true"; break; fi
    owner="$(cat "$NOTIFICATION_LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then rm -rf "$NOTIFICATION_LOCK_DIR"; continue; fi
    # No pid file and older than 2 minutes: a holder that died before writing it, or an
    # empty folder Windows kept. Same rule as post-next-up-digest.sh.
    if [[ -z "$owner" ]] && python3 -c 'import os,sys,time; sys.exit(0 if time.time() - os.stat(sys.argv[1]).st_mtime > int(sys.argv[2]) else 1)' \
        "$NOTIFICATION_LOCK_DIR" "${NEXT_UP_LOCK_STALE_SECONDS:-120}" 2>/dev/null; then
        rm -rf "$NOTIFICATION_LOCK_DIR"; continue
    fi
    sleep 0.05
done
trap '[[ "$have_lock" == "true" ]] && rm -rf "$NOTIFICATION_LOCK_DIR"' EXIT

# A waiter and a loop pass can both reach this point for the same row; the
# re-read under the lock is what keeps it to one send: whoever sent first has
# marked the row delivered (or attempted/unknown) by the time the lock frees.
if [[ "$terminal" == "true" && -n "$TASK_ID" && "$already_delivered" != "true" ]] \
    && locked_json="$(python3 "$LEDGER" show --task-id "$TASK_ID" 2>/dev/null)"; then
    task_json="$locked_json"
    already_delivered="$(printf '%s' "$task_json" | json_field report_delivered)"
    attempted_at="$(printf '%s' "$task_json" | json_field report_attempted_at)"
    if [[ "$duplicate" == "true" && "$already_delivered" != "true" && -n "$attempted_at" ]]; then
        python3 "$LEDGER" report-delivered --task-id "$TASK_ID" --note "settled without resend: report already pasted once at $attempted_at" >/dev/null
        printf '{"task_id":"%s","status":"%s","origin":"%s","delivered":true,"duplicate":true,"batched":false,"ignored":true,"reason":"report already pasted once; never pasted twice"}\n' "$TASK_ID" "$STATUS" "$origin"
        exit 0
    fi
fi

# A deferred terminal report does not wait for the next loop pass: one detached
# waiter per task polls the destination tab and replays this exact command the
# moment the turn ends (idle-only send, so delivery is proven, never a composer
# paste). The waiter exits once the row is delivered, settled, or unknown.
waiter="false"
spawn_waiter() {
    [[ "${REPORT_TO_COS_NO_WAITER:-0}" == "1" ]] && return 0
    local dir="$STATE_DIR/worker-dispatch" pidfile old
    pidfile="$dir/report-waiter-$TASK_ID.pid"
    mkdir -p "$dir"
    old="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then waiter="true"; return 0; fi
    # Own session: a worker's shell tool kills its whole process group when the
    # call returns, which took the first live waiter down with it (nohup alone
    # only ignores HUP). macOS has no setsid binary, so Python does it.
    env -u PM_OS_WORKER_ID -u PM_OS_WORKER_TASK_ID -u PM_OS_WORKER_ATTEMPT_ID -u PM_OS_WORKSTREAM_SLUG \
        python3 - "$dir/report-waiter.log" bash "$SCRIPTS/report-waiter.sh" "$TASK_ID" "$destination_ref" "${ORIGINAL_ARGS[@]}" <<'PY' && waiter="true" || true
import os, shutil, subprocess, sys
log = open(sys.argv[1], "ab")
# PATH first: CreateProcess searches System32, where bash.exe is the WSL launcher.
cmd = [shutil.which(sys.argv[2]) or sys.argv[2], *sys.argv[3:]]
subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                 **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
PY
}

delivered="false"; outcome="not-attempted"
[[ "$(printf '%s' "$task_json" | json_field report_delivery_unknown)" == "true" ]] && outcome="unknown"
if [[ "$terminal" == "true" && "$direct" == "true" && "$quiet" != "true" && -z "$destination_ref" && -n "$TASK_ID" \
      && "$already_delivered" != "true" && "$outcome" != "unknown" ]]; then
    python3 "$LEDGER" report-not-attempted --task-id "$TASK_ID" --no-destination >/dev/null 2>&1 || true
fi

# Claude destination: proven delivery. On wmux the shim types the line, so
# "accepted" is not "arrived": a typed line can arrive cut short. The row is marked
# delivered only when the line is proven on the CoS side (wmux prompt log or
# screen; cmux's own acceptance on macOS). Every pass asks first whether CoS
# already holds the line, so a retry never sends it twice. A wmux refusal (a
# draft in the box, a dialog) never falls back to the raw paste below, which
# would type the full text and truncate it again; the waiter retries instead.
claude_result=""; claude_note=""; report_marker="$lead $STATUS:"
if [[ "$quiet" != "true" && "$already_delivered" != "true" && "$direct" == "true" && "$outcome" != "unknown" \
      && -n "$destination_ref" && "$destination_agent" == "claude" ]]; then
    if [[ "$terminal" == "true" ]] \
        && seen_json="$(python3 "$SCRIPTS/owner_tabs.py" seen --ref "$destination_ref" --marker "$report_marker" 2>/dev/null)"; then
        claude_result="seen"
        claude_note="already on $destination_ref ($(printf '%s' "$seen_json" | json_field where)); not sent again"
    else
        am_rc=0
        am_json="$(python3 "$SCRIPTS/owner_tabs.py" message --ref "$destination_ref" --from "PM OS report" --decode-marks \
            --require-verified --text "$report_line" 2>/dev/null)" || am_rc=$?
        am_id="$(printf '%s' "$am_json" | json_field id 2>/dev/null || true)"
        am_backend="$(printf '%s' "$am_json" | json_field backend 2>/dev/null || true)"
        am_proof="$(printf '%s' "$am_json" | json_field proof 2>/dev/null || true)"
        am_state="$(printf '%s' "$am_json" | json_field state 2>/dev/null || true)"
        am_reason="$(printf '%s' "$am_json" | json_field reason 2>/dev/null || true)"
        am_spill="$(printf '%s' "$am_json" | json_field spilled_to 2>/dev/null || true)"
        case "$am_rc" in
            0) claude_result="delivered"
               claude_note="cmux agent message ${am_id:-?} to $destination_ref${am_proof:+, proof $am_proof}${am_spill:+, full text in $am_spill}" ;;
            3) claude_result="unproven"
               claude_note="agent message ${am_id:-?} to $destination_ref was $am_state, not yet proven on screen" ;;
            # wmux (a reply that says so, or this run's backend when the shim timed
            # out or failed before replying): never the raw paste below, which
            # would type the full text; the waiter and the loop retry.
            *) if [[ "$am_backend" == "wmux" || "${PM_OS_MUX:-}" == "wmux" ]]; then
                   claude_result="refused"
                   claude_note="${am_reason:-agent message refused}"
               fi ;;   # cmux refusal: the guarded idle-only paste below
        esac
    fi
fi
if [[ "$quiet" == "true" ]]; then
    # Quiet workstream: nothing is pasted and nothing is batched. The row is
    # marked delivered so the next-up digest Updates section and the reap
    # hook treat it as settled; the report log and CONTEXT close-out below
    # keep the full text.
    outcome="quiet"
    if [[ "$already_delivered" != "true" && -n "$TASK_ID" ]]; then
        if [[ "$quiet_reason" == "locality-ack" ]]; then
            quiet_note="locality acknowledgment: logged, not sent to CoS"
        else
            quiet_note="quiet workstream ($effective_workstream): logged, not sent to CoS"
        fi
        python3 "$LEDGER" report-delivered --task-id "$TASK_ID" --note "$quiet_note" >/dev/null
        task_json="$(python3 "$LEDGER" show --task-id "$TASK_ID" 2>/dev/null || printf '%s' "$task_json")"
    fi
elif [[ "$already_delivered" == "true" ]]; then
    delivered="true"; outcome="delivered"
elif [[ "$claude_result" == "seen" || "$claude_result" == "delivered" ]]; then
    # Claude destination: cmux agent message goes through the
    # session's hooks, never the composer, so it cannot land in a draft; an idle
    # session wakes on it and a busy one reads it at its next step. On wmux the
    # shim proves it on the prompt log. A cmux refusal (no agent, unreachable)
    # falls through to the guarded idle-only paste below.
    delivered="true"; outcome="delivered"
    [[ "$terminal" == "true" ]] && python3 "$LEDGER" report-delivered --task-id "$TASK_ID" --note "$claude_note" >/dev/null
elif [[ "$claude_result" == "unproven" ]]; then
    # Typed and accepted (a busy Claude queues it) but not proven yet: not
    # delivered, so reap keeps the worker tab; the waiter's next pass finds it on
    # the prompt log (seen) or sends it again.
    outcome="unproven"
    if [[ "$terminal" == "true" ]]; then
        python3 "$LEDGER" report-unproven --task-id "$TASK_ID" --note "$claude_note" >/dev/null
        spawn_waiter
    fi
elif [[ "$claude_result" == "refused" ]]; then
    outcome="deferred"
    echo "warning: report to $destination_ref deferred: $claude_note" >&2
    if [[ "$terminal" == "true" ]]; then
        python3 "$LEDGER" report-not-attempted --task-id "$TASK_ID" >/dev/null
        spawn_waiter
    fi
elif [[ "$direct" == "true" && "$outcome" != "unknown" && -n "$destination_ref" ]]; then
    # Idle path only (cig_safe_send): it proves nothing was pasted when it defers.
    # A queue paste into a live composer can land mid-turn and be read as a
    # replay, so terminal lines never use it.
    [[ "$terminal" == "true" ]] && python3 "$LEDGER" report-attempted --task-id "$TASK_ID" >/dev/null
    rc=0
    cig_safe_send "$destination_ref" "$report_line" "$WAIT_SECONDS" 1 || rc=$?
    case "$rc" in
        0) delivered="true"; outcome="delivered"
           [[ "$terminal" == "true" ]] && python3 "$LEDGER" report-delivered --task-id "$TASK_ID" >/dev/null ;;
        3) # cmux returned nonzero. Its exit status is not the outcome: the screen is.
           # Seen on the screen -> delivered. Idle, empty box, line absent -> nothing
           # was pasted, so one more idle-path try is allowed (counted per row).
           # Anything else, or a second transport error, is unknown and final.
           verify_rc=0
           cig_verify_sent "$destination_ref" "$report_line" || verify_rc=$?
           transport_errors="$(printf '%s' "$task_json" | json_field report_transport_errors)"
           if [[ "$verify_rc" == 0 ]]; then
               delivered="true"; outcome="delivered"
               [[ "$terminal" == "true" ]] && python3 "$LEDGER" report-delivered --task-id "$TASK_ID" \
                   --note "cmux send returned an error; the line was verified on $destination_ref" >/dev/null
           elif [[ "$verify_rc" == 2 && "${transport_errors:-0}" -lt 1 ]]; then
               outcome="deferred"
               echo "warning: send to $destination_ref returned an error; screen shows nothing pasted, one retry allowed" >&2
               if [[ "$terminal" == "true" ]]; then
                   python3 "$LEDGER" report-not-attempted --task-id "$TASK_ID" --transport-error >/dev/null
                   spawn_waiter
               fi
           else
               outcome="unknown"
               [[ "$terminal" == "true" ]] && python3 "$LEDGER" report-unknown --task-id "$TASK_ID" >/dev/null
               echo "warning: send to $destination_ref returned an error; delivery outcome unknown" >&2
           fi ;;
        *) outcome="deferred"   # nothing was pasted: the waiter (and the next loop pass) try again
           if [[ "$terminal" == "true" ]]; then
               python3 "$LEDGER" report-not-attempted --task-id "$TASK_ID" >/dev/null
               spawn_waiter
           fi ;;
    esac
fi

# One CoS badge for a report that keeps waiting:
# the waiter and every loop tick keep retrying; after alert_minutes the CoS tab
# gets one notification naming the worker, so a draft left in the CoS box never
# hides a finished result. Once per row (report_alerted_at).
if [[ "$terminal" == "true" && "$delivered" != "true" && "$quiet" != "true" && "$direct" == "true" && -n "$TASK_ID" \
      && "${alert_minutes:-0}" -gt 0 && -z "$(printf '%s' "$task_json" | json_field report_alerted_at)" ]] \
    && python3 - "$(printf '%s' "$task_json" | json_field finished_at)" "$alert_minutes" "${REPORT_TO_COS_NOW:-}" <<'PY'
import sys
from datetime import datetime, timedelta, timezone
try:
    finished = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(1)
now = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00")) if sys.argv[3] else datetime.now(timezone.utc)
raise SystemExit(0 if now - finished >= timedelta(minutes=int(sys.argv[2])) else 1)
PY
then
    # Say why only as far as it is known: with no CoS tab there is no box to clear, so
    # "CoS busy" is never shown for a CoS that is not running.
    if [[ -z "$destination_ref" ]]; then
        alert_body="$lead finished; CoS is not open, so its report lands when CoS reopens. The worker tab stays open until it lands."
    else
        alert_body="$lead finished; its report is waiting on CoS (${claude_note:-CoS busy}). The worker tab stays open until it lands."
    fi
    if [[ "${REPORT_TO_COS_NO_NOTIFY:-0}" != "1" ]]; then
        cmux_cmd notify ${destination_ref:+--workspace "$destination_ref"} --title "PM OS report waiting" --body "$alert_body" >/dev/null 2>&1 || true
    fi
    python3 "$LEDGER" report-alerted --task-id "$TASK_ID" --note "$alert_body" >/dev/null 2>&1 || true
fi

# Open, never "click this": a delivered report that links a compare page opens it on the owner's machine
# once per page version (image_compare.py open-report; a done report has landed,
# so the page is on main; a parked ask opens the live worktree copy). The open
# runs in the background (`open -g`) and never steals focus. Kill switch IMAGE_COMPARE_OPEN_DISABLED=1.
opened_json=""
if [[ "${IMAGE_COMPARE_OPEN_DISABLED:-0}" != "1" && "$delivered" == "true" && "$outcome" == "delivered" && "$quiet" != "true" ]] \
    && PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import sys
from review_surface import has_compare_link
sys.exit(0 if has_compare_link(sys.argv[1]) else 1)' "$SUMMARY" 2>/dev/null; then
    opened_json="$(PYTHONPATH="$SCRIPTS${PYTHONPATH:+:$PYTHONPATH}" python3 "$SCRIPTS/image_compare.py" open-report \
        --text="$SUMMARY" --task-id "$TASK_ID" 2>/dev/null || true)"
    [[ -n "$opened_json" ]] && echo "note: compare page open: $opened_json" >&2
fi

# Report log: every report, delivered or not (append-only). Formerly the Thread Status feed;
# that surface is retired, the log stays as delivery evidence.
FEED_FILE="${REPORT_LOG_FILE:-${THREAD_STATUS_FEED_FILE:-$STATE_DIR/thread-status-feed.jsonl}}"
FEED_TASK_JSON="$task_json" FEED_SUMMARY="$SUMMARY" FEED_ORIGIN="$origin" FEED_ROUTED_TO="$routed_to" FEED_STATUS="$STATUS" \
FEED_OUTCOME="$outcome" FEED_DELIVERED="$delivered" FEED_THREAD_ID="$THREAD_ID" FEED_WORKSTREAM="$WORKSTREAM" \
FEED_URGENT="$URGENT" FEED_NEEDS_RULING="$NEEDS_RULING" FEED_REVIEW_URL="$review_url" FEED_DEST_REF="$destination_ref" \
python3 - "$FEED_FILE" <<'PY' || echo "warning: could not append to the report log" >&2
import json, os, sys
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("PM_OS_SCRIPTS_DIR") or _os.path.join(_os.environ.get("PM_OS_ROOT") or _os.path.expanduser("~/Development/pm-os"), "system", "scripts"))
    import flock_compat as fcntl
from datetime import datetime, timezone
path = sys.argv[1]
task = json.loads(os.environ.get("FEED_TASK_JSON") or "{}")
row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
       "task_id": task.get("id"), "attempt_id": task.get("attempt_id"),
       "thread_id": os.environ.get("FEED_THREAD_ID") or task.get("thread_id") or None,
       "workstream": os.environ.get("FEED_WORKSTREAM") or task.get("workstream") or None,
       "origin": os.environ.get("FEED_ORIGIN") or "cos", "report_kind": "worker",
       "status": os.environ.get("FEED_STATUS") or task.get("status"), "summary": os.environ.get("FEED_SUMMARY") or "",
       "deliverable": os.environ.get("FEED_REVIEW_URL") or None,
       "urgent": os.environ.get("FEED_URGENT") == "true" or os.environ.get("FEED_NEEDS_RULING") == "true",
       "routed_to": os.environ.get("FEED_ROUTED_TO") or "cos", "destination_workspace_ref": os.environ.get("FEED_DEST_REF") or None,
       "delivery_outcome": os.environ.get("FEED_OUTCOME") or "not-attempted", "delivered": os.environ.get("FEED_DELIVERED") == "true"}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a+", encoding="utf-8") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    if row["status"] in {"done", "blocked", "failed", "cancelled", "deferred"}:
        handle.seek(0)
        previous = None
        for line in handle:
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if all(candidate.get(k) == row.get(k) for k in ("task_id", "attempt_id", "status")):
                previous = candidate
        if previous and all(previous.get(k) == row.get(k) for k in ("delivered", "delivery_outcome", "routed_to")):
            raise SystemExit(0)
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
bash "$SCRIPTS/publish-running-tasks-status.sh" >/dev/null 2>&1 || true

# Close-out record: land, record, then reap. One
# line per finished worker lands in the owning workstream's CONTEXT.md on main
# (task id, tab title, status, result, thread link) before the tab can be
# reaped; dispatch.py reap repeats this idempotently so no tab closes without
# it. Cancelled rows record nothing. Synchronous: the reap hook below must not
# race it.
if [[ "$hooks" == "true" && "$terminal" == "true" && "$STATUS" != "cancelled" && -n "$TASK_ID" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    env -u PM_OS_WORKER_ID python3 "$SCRIPTS/dispatch.py" record --task-id "$TASK_ID" --status "$STATUS" \
        --result "$SUMMARY" ${THREAD_ID:+--thread-id "$THREAD_ID"} >>"$STATE_DIR/worker-dispatch/finish.log" 2>&1 || true
fi

# CoS Review: handled rows never linger in Needs you. A done report settles the asks of the row it continued and of every
# --handles row: those rows move to Past as "handled" with this report's line as
# the reason. Best effort; the review feed is a mirror of the ledger.
if [[ "$terminal" == "true" && "$STATUS" == "done" && -n "$TASK_ID" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    parent="$(printf '%s' "${task_json:-null}" | json_field continuation_of 2>/dev/null || true)"
    for handled in ${parent:+"$parent"} "${HANDLES[@]+"${HANDLES[@]}"}"; do
        [[ "$handled" == rt-* && "$handled" != "$TASK_ID" ]] || continue
        python3 "$SCRIPTS/cos_review_actions.py" --no-feed complete --task-id "$handled" --by "report:$TASK_ID" \
            --reason "handled: $TASK_ID done: ${SUMMARY:0:160}" >>"$STATE_DIR/worker-dispatch/finish.log" 2>&1 || true
    done
fi

# Result card. A finished
# worker's result also goes to its workstream's open owner tab (normal or
# "<emoji> P0 · <name>") by cmux agent message: summary, review-note link,
# drafts verbatim, follow-ups. With no open tab the card lands under
# "## Latest results" in main's CONTEXT.md so the next seed shows it. Once per
# row and status; skipped for rows an owner tab ran itself.
card_json=""
if [[ "$hooks" == "true" && "$terminal" == "true" && "$STATUS" != "cancelled" && -n "$TASK_ID" \
    && "${REPORT_TO_COS_NO_OWNER_TABS:-0}" != "1" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    card_json="$(env -u PM_OS_WORKER_ID python3 "$SCRIPTS/owner_tabs.py" result-card --task-id "$TASK_ID" \
        --status "$STATUS" --summary "$SUMMARY" --review-url "${review_url:-}" --reporter-ref "${WORKSPACE_REF:-}" \
        2>>"$STATE_DIR/worker-dispatch/finish.log" || true)"
fi

# Owner actions become to-dos: a Follow-up line or a "must" sentence aimed at the owner, a
# ruling, or a parked draft with no tracker gets a deduped, scrubbed to-do; the report's
# task closes the to-dos it has settled. After delivery and detached, because an add makes
# calendar calls and this script holds the notification lock. The subshell keeps a failure
# here from touching the report under set -e. A loop retry runs it too, so a pass that died
# during delivery still captures; the source_ref dedupes the repeat. A test names its stub
# in OWNER_ACTION_TODO_CLI, which runs in line, and keeps REPORT_TO_COS_NO_HOOKS=1, because
# the other hooks write main. See owner_action_capture.py.
if [[ ( "$hooks" == "true" || -n "${OWNER_ACTION_TODO_CLI:-}" ) && -n "$TASK_ID" ]]; then
    (
        mkdir -p "$STATE_DIR/worker-dispatch" || exit 0
        capture_summary="$(mktemp "${TMPDIR:-/tmp}/owner-action.XXXXXX")" || exit 0
        printf '%s' "$SUMMARY" >"$capture_summary" || exit 0
        capture_args=(report --task-id "$TASK_ID" --workstream "$WORKSTREAM" --status "$STATUS" --summary-file "$capture_summary")
        [[ "$NEEDS_RULING" == "true" ]] && capture_args+=(--needs-ruling)
        python3 "$SCRIPTS/owner_action_capture.py" "${capture_args[@]}" --detach --delete-summary \
            >>"$STATE_DIR/worker-dispatch/owner-actions.log" 2>&1 || true
    ) || true
fi

# Lifecycle hooks: a terminal report frees capacity (queued tasks start) and,
# once delivered, the worker tab is reaped after a short grace. Both detached,
# through no_window.py: its own session off Windows, a hidden console on it, so
# the git calls under release and reap open no terminal windows.
if [[ "$hooks" == "true" && "$terminal" == "true" ]]; then
    mkdir -p "$STATE_DIR/worker-dispatch"
    env -u PM_OS_WORKER_ID -u PM_OS_WORKER_TASK_ID -u PM_OS_WORKER_ATTEMPT_ID -u PM_OS_WORKSTREAM_SLUG \
        python3 "$SCRIPTS/no_window.py" spawn --log "$STATE_DIR/worker-dispatch/hooks.log" -- \
        bash -c "sleep 150; python3 '$SCRIPTS/dispatch.py' release; python3 '$SCRIPTS/dispatch.py' reap --task-id '$TASK_ID'" \
        >>"$STATE_DIR/worker-dispatch/hooks.log" 2>&1 </dev/null || true
fi

OPENED_JSON="$opened_json" CARD_JSON="$card_json" DEST_AGENT="$destination_agent" DEST_WARNING="$destination_warning" LANDED_JSON="$landed_json" WAITER="$waiter" QUIET="$quiet" QUIET_REASON="$quiet_reason" STALE_BATCHED="$stale_batched" python3 - "$task_json" "$origin" "$destination_ref" "$delivered" "$duplicate" "$outcome" "$routed_to" "$direct" "$report_line" <<'PY'
import json, os, sys
task = json.loads(sys.argv[1])
landed = json.loads(os.environ.get("LANDED_JSON") or "null")
quiet = os.environ.get("QUIET") == "true"
try:
    card = json.loads((os.environ.get("CARD_JSON") or "null").strip().splitlines()[-1] or "null")
except (ValueError, IndexError):
    card = None
try:
    opened_pages = [p["path"] for p in (json.loads(os.environ.get("OPENED_JSON") or "{}").get("pages") or []) if p.get("opened")]
except (ValueError, AttributeError, KeyError, TypeError):
    opened_pages = []
print(json.dumps({
    "task_id": task.get("id"), "status": task.get("status"), "phase": task.get("phase"), "origin": sys.argv[2],
    "destination_surface": sys.argv[2], "destination_workspace_ref": sys.argv[3] or None,
    "executor_thread_id": task.get("thread_id"), "executor_workspace_ref": task.get("workspace_ref"),
    "delivered": sys.argv[4] == "true", "duplicate": sys.argv[5] == "true", "delivery_outcome": sys.argv[6],
    "routed_to": sys.argv[7], "message": sys.argv[9], "landed": landed, "waiter": os.environ.get("WAITER") == "true",
    "quiet": quiet, "quiet_reason": os.environ.get("QUIET_REASON") or None,
    "destination_agent": os.environ.get("DEST_AGENT") or None,
    "destination_warning": os.environ.get("DEST_WARNING") or None,
    "result_card": card,
    "opened_pages": opened_pages,
    "stale_batched": os.environ.get("STALE_BATCHED") == "true",
    # batched: a CoS row waiting for the next digest. A direct row (priority
    # or a stored --delivery direct policy) that the busy screen deferred is
    # pending, not batched: the waiter and the loop retry paste it.
    "batched": sys.argv[2] == "cos" and not quiet and task.get("report_delivered") is False and sys.argv[4] != "true"
               and sys.argv[6] != "unknown" and sys.argv[8] != "true",
}))
PY
