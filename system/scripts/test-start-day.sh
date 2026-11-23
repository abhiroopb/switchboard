#!/usr/bin/env bash
# test-start-day.sh — regression harness for the 2026-08-06 start-day fixes.
#
# Covers:
#   1. Missing daily note  -> skeleton created exactly once, marker present
#   2. Rerun               -> existing note never overwritten
#   3. Hand-touched note   -> byte-for-byte unchanged
#   4. switchboard_state_valid / switchboard_state_fresh shape+freshness checks
#   5. No `amp upgrade` anywhere; update_amp wired in main
#   6. No 300s plan polling; phase_4_verify_state wired; fan-out reachable
#   7. Reused-switchboard refresh prompt is guarded and non-fatal on deferral
#   8. CoS launch prompts enforce list-with-headers digests, not chat tables
#   9. Scheduled/background startup preserves cmux focus
#   10. Start-day creates Launchpad and pins it directly below CoS
#   11. Dry-run logging and discovery do not mutate the fixture
#   12. Concurrent daily-note publication cannot replace a user note
#   13. Malformed now.json degrades the report without stopping startup
#   14. The report prints with and without workstream outcomes on current bash
#   15. Freshness rejects by comparing, never by erroring (GNU and BSD stat)
#
# Unit tests source start-day.sh with START_DAY_REPO_ROOT pointed at a
# throwaway fixture tree, so nothing touches real notes/ or system/state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
START_DAY="$REPO_ROOT/system/start-day.sh"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

FIXTURE="$(mktemp -d /tmp/test-start-day.XXXXXX)"
cleanup() { rm -rf "$FIXTURE"; }
trap cleanup EXIT

mkdir -p "$FIXTURE/notes/daily" "$FIXTURE/system/state" "$FIXTURE/system/logs" \
    "$FIXTURE/system/plans" "$FIXTURE/workstreams"

# Source the orchestrator inside a fixture-rooted subshell and run a snippet.
# The source guard at the bottom of start-day.sh prevents main() from running.
in_fixture() {
    (
        export START_DAY_REPO_ROOT="$FIXTURE"
        # shellcheck disable=SC1090
        source "$START_DAY"
        NOTE_PATH="$DAILY_DIR/$TODAY.md"
        eval "$1"
    )
}

echo "== 1. Missing note -> skeleton created with marker =="
rm -f "$FIXTURE/notes/daily/"*.md
if in_fixture 'ensure_daily_note' >/dev/null 2>&1; then
    note="$FIXTURE/notes/daily/$(date +%Y-%m-%d).md"
    [[ -f "$note" ]] && pass "skeleton file created" || fail "skeleton file missing"
    grep -q '<!-- start-day-skeleton:' "$note" \
        && pass "skeleton marker present" || fail "skeleton marker missing"
    grep -q "^# $(date +%Y-%m-%d)" "$note" \
        && pass "today's heading present" || fail "heading missing"
    grep -q '^## Priorities' "$note" && grep -q '^## Carry Forward' "$note" \
        && pass "canonical sections present" || fail "sections missing"
else
    fail "ensure_daily_note exited non-zero on missing note"
fi

echo "== 2. Rerun -> no overwrite =="
note="$FIXTURE/notes/daily/$(date +%Y-%m-%d).md"
before_hash=$(shasum "$note" | awk '{print $1}')
in_fixture 'ensure_daily_note' >/dev/null 2>&1 \
    && pass "rerun exits 0" || fail "rerun exited non-zero"
after_hash=$(shasum "$note" | awk '{print $1}')
[[ "$before_hash" == "$after_hash" ]] \
    && pass "note unchanged on rerun" || fail "note was rewritten on rerun"

echo "== 3. Hand-touched note -> untouched =="
echo "my private hand-written note" > "$note"
in_fixture 'ensure_daily_note' >/dev/null 2>&1 || true
[[ "$(cat "$note")" == "my private hand-written note" ]] \
    && pass "hand-touched note preserved" || fail "hand-touched note modified"
rm -f "$note"

echo "== 3b. Concurrent note creator wins without clobber =="
if in_fixture '
    ln() {
        printf "%s\n" "concurrent user note" > "$NOTE_PATH"
        command ln "$@"
    }
    ensure_daily_note
' >/dev/null 2>&1 \
    && [[ "$(cat "$note")" == "concurrent user note" ]]; then
    pass "concurrently created user note preserved"
else
    fail "daily-note publication clobbered or rejected concurrent user note"
fi
rm -f "$note"

echo "== 4. Queue state validity + freshness =="
NOW_F="$FIXTURE/system/state/now.json"
QUEUE_F="$FIXTURE/system/state/queue.json"
echo '{"now": {"focus": "x"}}' > "$NOW_F"
echo '{"queue": []}' > "$QUEUE_F"
in_fixture 'switchboard_state_valid' \
    && pass "valid shape accepted" || fail "valid shape rejected"
in_fixture 'START_EPOCH=$(date +%s); touch "$NOW_FILE" "$QUEUE_FILE"; switchboard_state_fresh' \
    && pass "fresh mtime accepted" || fail "fresh mtime rejected"
echo '{"wrong": true}' > "$QUEUE_F"
in_fixture 'switchboard_state_valid' \
    && fail "invalid queue shape accepted" || pass "invalid queue shape rejected"
echo '{"queue": []}' > "$QUEUE_F"
touch -t 202601010000 "$NOW_F" "$QUEUE_F"
# Reject means the comparison said no. A helper that errors also returns nonzero,
# which passed this case on Windows while stat was broken, so stderr must stay empty.
if in_fixture 'switchboard_state_fresh' 2>"$FIXTURE/fresh.err"; then
    fail "stale mtime accepted as fresh"
elif [[ -s "$FIXTURE/fresh.err" ]]; then
    fail "freshness check errored instead of comparing: $(cat "$FIXTURE/fresh.err")"
else
    pass "stale mtime rejected"
fi
touch "$QUEUE_F"
in_fixture 'START_EPOCH=$(date +%s); touch "$QUEUE_FILE"; switchboard_state_fresh' 2>/dev/null \
    && fail "one stale file accepted as fresh" || pass "one stale file rejected"
rm -f "$NOW_F"
in_fixture 'START_EPOCH=$(date +%s); switchboard_state_fresh' 2>"$FIXTURE/fresh.err" \
    && fail "missing state file accepted as fresh" || pass "missing state file rejected"
[[ ! -s "$FIXTURE/fresh.err" ]] \
    && pass "missing state file reads as age zero, no error" \
    || fail "missing state file errored: $(cat "$FIXTURE/fresh.err")"
echo '{"now": {"focus": "x"}}' > "$NOW_F"

echo "== 4b. Stale state warning names the last write =="
touch -t 202601010000 "$NOW_F" "$QUEUE_F"
if in_fixture '
    FLAG_DRY_RUN=false
    python3() { return 1; }
    derive_compat_plan() { :; }
    set_workspace_status() { :; }
    publish_switchboard_status() { :; }
    phase_4_verify_state
' > "$FIXTURE/verify.log" 2>&1 \
    && grep -Fq 'last write: 2026-01-01 00:00:00' "$FIXTURE/verify.log"; then
    pass "stale warning prints the queue file's real time"
else
    cat "$FIXTURE/verify.log"
    fail "stale warning lost the queue file's time"
fi

echo "== 5. Amp updater =="
grep -vE '^\s*#' "$START_DAY" | grep -q 'amp upgrade\|"\$AMP_CMD" upgrade' \
    && fail "'amp upgrade' still referenced" || pass "no executable 'amp upgrade' remains"
grep -q '"\$AMP_CMD" update' "$START_DAY" \
    && pass "update_amp runs 'amp update'" || fail "'amp update' not found"
grep -qE '^\s*update_amp\b' "$START_DAY" \
    && pass "update_amp wired in main" || fail "update_amp not called"
grep -qE '^\s*upgrade_amp\b' "$START_DAY" \
    && fail "stale upgrade_amp call remains" || pass "no stale upgrade_amp call"
grep -q 'cos_agent_mode="\$(resolve_agent_mode "\$COS_WORKSPACE_NAME")"' "$START_DAY" \
    && pass "CoS mode resolved via resolve_agent_mode (ultra floor, no -m high literal)" \
    || fail "CoS launch does not resolve its mode via resolve_agent_mode"

echo "== 6. No agent-turn polling; fan-out reachable =="
grep -q 'PLAN_WAIT\|phase_4_wait_for_plan' "$START_DAY" \
    && fail "old 300s polling phase still referenced" || pass "old polling phase gone"
grep -qE '^\s*phase_4_verify_state\b' "$START_DAY" \
    && pass "phase_4_verify_state wired in main" || fail "phase_4_verify_state not called"
grep -qE '^\s*ensure_daily_note\b' "$START_DAY" \
    && pass "ensure_daily_note wired in main" || fail "ensure_daily_note not called"
# phase_5 must follow phase_4 unconditionally (fan-out never skipped)
main_body=$(sed -n '/^main() {/,/^}/p' "$START_DAY")
p4=$(echo "$main_body" | grep -n 'phase_4_verify_state' | cut -d: -f1 | head -1)
p5=$(echo "$main_body" | grep -n 'phase_5_launch_workstreams' | cut -d: -f1 | head -1)
if [[ -n "$p4" && -n "$p5" ]] && (( p5 > p4 )); then
    pass "resolver fan-out follows state verification"
else
    fail "fan-out ordering broken in main()"
fi

echo "== 7. Reused switchboard gets a bounded, guarded review =="
reuse_block=$(sed -n '/Reusing existing.*workspace without reset/,/^    fi$/p' "$START_DAY")
echo "$reuse_block" | grep -q 'post_start_day_review' \
    && pass "reuse path delegates to the bounded review" || fail "reuse path misses the bounded review"
echo "$reuse_block" | grep -q 'exit ' \
    && fail "reuse path can exit on deferral" || pass "deferral is non-fatal"
echo "$reuse_block" | grep -q 'rotate_cos_thread_at_start_of_day' \
    && pass "reuse path rotates the CoS thread before the review" || fail "reuse path skips CoS thread rotation"
rotate_block=$(sed -n '/^rotate_cos_thread_at_start_of_day()/,/^}/p' "$START_DAY")
echo "$rotate_block" | grep -q 'rotate-cos-thread.sh' \
    && pass "rotation delegates to rotate-cos-thread.sh" || fail "rotation does not call rotate-cos-thread.sh"
echo "$rotate_block" | grep -q 'exit ' \
    && fail "rotation can exit start-day" || pass "rotation failure is non-fatal"
echo "$rotate_block" | grep -q 'COS_REF="\$new_ref"' \
    && pass "rotation repoints COS_REF at the new workspace" || fail "rotation leaves COS_REF on the old workspace"
rotate_script="$REPO_ROOT/system/scripts/rotate-cos-thread.sh"
bash -n "$rotate_script" && pass "rotate-cos-thread.sh parses" || fail "rotate-cos-thread.sh syntax error"
rot_out=$(SWITCHBOARD_COS_ROTATION_DISABLED=1 bash "$rotate_script" 2>/dev/null | tail -1)
[[ "$rot_out" == *'"status": "skipped"'* ]] \
    && pass "rotation kill switch skips cleanly" || fail "rotation kill switch did not skip: $rot_out"
grep -q 'split-thread.sh' "$rotate_script" \
    && pass "rotation goes through split-thread.sh" || fail "rotation bypasses split-thread.sh"
grep -q 'cig_safe_send "\$OLD_REF"' "$rotate_script" \
    && pass "rotation announces the new thread in the old one through the inject guard" || fail "rotation announcement bypasses the inject guard"
review_block=$(sed -n '/^post_start_day_review()/,/^}/p' "$START_DAY")
echo "$review_block" | grep -q 'post-next-up-digest.sh.*--surface start-day' \
    && pass "start-day uses the shared guarded digest path" || fail "start-day bypasses the shared digest path"
echo "$review_block" | grep -q 'return 0' \
    && pass "review failure returns softly" || fail "review failure can stop start-day"

echo "== 8. CoS review contract =="
grep -q '\[START-DAY REVIEW\]' "$REPO_ROOT/system/scripts/next-up-digest.py" \
    && pass "start-day review has a machine envelope" || fail "start-day review lacks a machine envelope"
grep -q 'Daily and command-center digests use lists in CoS chat, never tables' \
    "$REPO_ROOT/system/chief-of-staff-prompt.md" \
    && pass "CoS contract rejects chat tables" || fail "CoS contract allows chat tables"
grep -q 'Put one topic on each line' "$REPO_ROOT/system/chief-of-staff-prompt.md" \
    && pass "CoS contract requires one topic per line" || fail "CoS contract lacks one-topic rule"

echo "== 9. Syntax =="
bash -n "$START_DAY" && pass "bash -n clean" || fail "syntax error"

echo "== 10. Background startup preserves focus =="
grep -q 'open -g -a cmux' "$START_DAY" \
    && pass "cmux launches without foregrounding the app" || fail "cmux launch can foreground the app"
grep -q '^FLAG_FOCUS=false$' "$START_DAY" \
    && pass "focus is disabled by default" || fail "focus is not disabled by default"
fullscreen_block=$(sed -n '/Focusing and fullscreen transitions/,/^    fi$/p' "$START_DAY")
echo "$fullscreen_block" | grep -q 'if \[\[ "$FLAG_FOCUS" == "true" \]\]' \
    && echo "$fullscreen_block" | grep -q 'tell application "cmux" to activate' \
    && pass "app activation requires the explicit --focus flag" \
    || fail "app activation is not gated by explicit focus"

echo "== 11. Launchpad startup and pinned order =="
grep -q 'routines=(todo launchpad comms-triage scheduled-jobs meetings)' \
    "$REPO_ROOT/system/scripts/ensure-routine-workspaces.sh" \
    && pass "start-day ensures Launchpad" || fail "start-day does not ensure Launchpad"
grep -q 'top_pinned=("routine:switchboard" "routine:todo" "routine:launchpad" "routine:comms-triage")' \
    "$REPO_ROOT/system/cmux-helpers.sh" \
    && pass "pinned order is CoS, To Do, Launchpad" || fail "pinned order is wrong"
grep -q 'bottom_unpinned=("routine:scheduled-jobs" "routine:meetings")' \
    "$REPO_ROOT/system/cmux-helpers.sh" \
    && pass "Scheduled Jobs and Meetings sit unpinned at the bottom" || fail "Meetings is still pinned"
grep -q '\[\[ "${SWITCHBOARD_MUX:-}" == "wmux" \]\] && return 0' "$REPO_ROOT/system/cmux-helpers.sh" \
    && pass "the sidebar order is cmux only; wmux is left alone" || fail "wmux still gets an order"
! grep -q 'phase_5b_sidebar_order\|sidebar_order.py' "$REPO_ROOT/system/start-day.sh" \
    && pass "start of day logs no wmux drag plan" || fail "start of day still plans wmux drags"

echo "== 12. Optional launch failures do not stop later phases under set -e =="
mkdir -p "$FIXTURE/system/scripts" "$FIXTURE/daemon" "$FIXTURE/routines/meetings" "$FIXTURE/routines/scheduled-jobs"
cp "$REPO_ROOT/system/scripts/ensure-routine-workspaces.sh" "$FIXTURE/system/scripts/"
touch "$FIXTURE/routines/meetings/CONTEXT.md" "$FIXTURE/routines/scheduled-jobs/CONTEXT.md"
cat > "$FIXTURE/system/cmux-helpers.sh" <<'EOF'
log_warn() { echo "[WARN] $*"; }
log_info() { :; }
find_workspace_ref_for_source_key() { return 1; }
if [[ "${1:-}" == launch ]]; then
    echo "$2" >> "$REPO_ROOT/routine-calls"
    [[ "$2" != routine:meetings ]]
fi
EOF
if REPO_ROOT="$FIXTURE" bash "$FIXTURE/system/scripts/ensure-routine-workspaces.sh" meetings scheduled-jobs > "$FIXTURE/routines.log" 2>&1 \
    && grep -q 'routine:scheduled-jobs' "$FIXTURE/routine-calls" \
    && grep -Fq '[WARN] Launcher for meetings exited 1 (non-fatal)' "$FIXTURE/routines.log"; then
    pass "failed meetings launch logs and continues to scheduled-jobs"
else
    fail "failed routine aborted later launch"
fi

for launcher in ensure-routine-workspaces ensure-p0-workspaces; do
    printf 'echo "%s" >> "%s/phase-calls"\nexit 7\n' "$launcher" "$FIXTURE" > "$FIXTURE/system/scripts/$launcher.sh"
done
# Use a new Bash process, not a conditional function call: the latter disables
# errexit throughout the function and would hide precisely this regression.
if START_DAY_REPO_ROOT="$FIXTURE" bash -c '
    source "$1"
    publish_switchboard_status() { :; }
    phase_4b_launch_routines
    FLAG_ALL=true
    WORKSTREAMS_FOUND=(first second)
    launch_single_workstream() { echo "$1" >> "$REPO_ROOT/phase-calls"; return 6; }
    phase_5_launch_workstreams
    echo complete >> "$REPO_ROOT/phase-calls"
' _ "$START_DAY" > "$FIXTURE/phases.log" 2>&1 \
    && [[ "$(cat "$FIXTURE/phase-calls")" == $'ensure-routine-workspaces\nensure-p0-workspaces\nfirst\nsecond\ncomplete' ]] \
    && grep -Fq '[WARN] Routine workspace launcher failed (non-fatal)' "$FIXTURE/phases.log"; then
    pass "all failed optional launchers warn and execution reaches later phases"
else
    cat "$FIXTURE/phases.log"
    fail "optional launch failure stopped startup or hid the warning"
fi

echo "== 13. Local verification can skip the Git snapshot without changing the default =="
printf 'echo snapshot >> "%s/snapshot-calls"\n' "$FIXTURE" > "$FIXTURE/system/scripts/commit-todo-state.sh"
snapshot_block=$(sed -n '/    # Snapshot todo state/,/    # Self-heal tracked skill/p' "$START_DAY")
for skip in 1 0; do
    START_DAY_SKIP_TODO_SNAPSHOT="$skip" SYSTEM_DIR="$FIXTURE/system" FLAG_DRY_RUN=false \
        bash -c 'log_info() { :; }; log_warn() { :; }; eval "$1"' _ "$snapshot_block"
    if [[ "$skip" == 1 && ! -e "$FIXTURE/snapshot-calls" ]]; then
        pass "local-only verification invokes no snapshot or push"
    elif [[ "$skip" == 0 && "$(cat "$FIXTURE/snapshot-calls")" == snapshot ]]; then
        pass "normal startup retains its snapshot"
    else
        fail "snapshot skip boundary failed for skip=$skip"
    fi
done

echo "== 14. CoS cold-launch failures leave background startup reachable =="
for stage in create reset send ready; do
    if START_DAY_REPO_ROOT="$FIXTURE" FAIL_STAGE="$stage" bash -c '
        source "$1"
        refresh_comms_review_state() { :; }
        set_workspace_status() { :; }
        find_or_create_workspace() { [[ "$FAIL_STAGE" != create ]] || return 1; echo workspace:77; }
        reset_workspace_surface() { [[ "$FAIL_STAGE" != reset ]]; }
        resolve_agent_mode() { echo ultra; }
        send_to_workspace() { [[ "$FAIL_STAGE" != send ]]; }
        wait_for_amp_ready() { [[ "$FAIL_STAGE" != ready ]]; }
        phase_3_chief_of_staff
        echo later-phases-reachable
    ' _ "$START_DAY" > "$FIXTURE/cos-$stage.log" 2>&1 \
        && grep -q 'later-phases-reachable' "$FIXTURE/cos-$stage.log" \
        && grep -Fq '[WARN]' "$FIXTURE/cos-$stage.log"; then
        pass "CoS $stage failure warns and returns to later phases"
    else
        cat "$FIXTURE/cos-$stage.log"
        fail "CoS $stage failure aborts startup or hides warning"
    fi
done

echo "== 15. Dry-run logging and discovery are mutation-free =="
DRY_FIXTURE="$FIXTURE/dry-run-repo"
mkdir -p "$DRY_FIXTURE/system" "$DRY_FIXTURE/workstreams"
printf 'fixture prompt\n' > "$DRY_FIXTURE/system/chief-of-staff-prompt.md"
FAKE_BIN="$FIXTURE/fake-bin"
mkdir -p "$FAKE_BIN"
for command_name in amp jq cmux; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "$FAKE_BIN/$command_name"
    chmod +x "$FAKE_BIN/$command_name"
done
before_dry=$(
    find "$DRY_FIXTURE" -print | LC_ALL=C sort
    find "$DRY_FIXTURE" -type f -exec shasum {} + | LC_ALL=C sort
)
if PATH="$FAKE_BIN:$PATH" START_DAY_REPO_ROOT="$DRY_FIXTURE" bash -c '
    source "$1"
    FLAG_DRY_RUN=true
    CMUX_BIN="$2/cmux"
    setup_logging
    phase_1_discover
' _ "$START_DAY" "$FAKE_BIN" > "$FIXTURE/dry-run.log" 2>&1; then
    after_dry=$(
        find "$DRY_FIXTURE" -print | LC_ALL=C sort
        find "$DRY_FIXTURE" -type f -exec shasum {} + | LC_ALL=C sort
    )
    [[ "$before_dry" == "$after_dry" ]] \
        && pass "dry-run leaves fixture paths unchanged" \
        || fail "dry-run created or removed fixture paths"
    [[ ! -e "$DRY_FIXTURE/system/logs" ]] \
        && pass "dry-run creates no log directory" \
        || fail "dry-run created a log directory"
else
    cat "$FIXTURE/dry-run.log"
    fail "fixture-rooted dry-run failed"
fi

echo "== 16. Malformed now state degrades report =="
printf '{malformed\n' > "$FIXTURE/system/state/now.json"
if in_fixture 'phase_6_report; echo report-complete' > "$FIXTURE/report.log" 2>&1 \
    && grep -Fq 'Now:             unavailable (invalid state)' "$FIXTURE/report.log" \
    && grep -q 'report-complete' "$FIXTURE/report.log"; then
    pass "malformed now.json reports unavailable and continues"
else
    cat "$FIXTURE/report.log"
    fail "malformed now.json stopped or escaped the report"
fi

echo "== 17. Report lists workstreams by outcome, and only the outcomes that happened =="
grep -Eq '^\|  (Opened|Skipped|Failed):' "$FIXTURE/report.log" \
    && fail "report with no workstreams printed an empty section" \
    || pass "report with no workstreams prints no empty sections"
if in_fixture '
    WORKSTREAMS_OPENED=(alpha beta)
    WORKSTREAMS_SKIPPED=(gamma)
    WORKSTREAMS_FAILED=(delta)
    phase_6_report
' > "$FIXTURE/report-full.log" 2>&1 \
    && grep -Fq '|  Opened:' "$FIXTURE/report-full.log" \
    && grep -Fq '|    + alpha' "$FIXTURE/report-full.log" \
    && grep -Fq '|    + beta' "$FIXTURE/report-full.log" \
    && grep -Fq '|    - gamma' "$FIXTURE/report-full.log" \
    && grep -Fq '|    x delta' "$FIXTURE/report-full.log"; then
    pass "opened, skipped, and failed workstreams all print"
else
    cat "$FIXTURE/report-full.log"
    fail "report dropped a workstream outcome"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
