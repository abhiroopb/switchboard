#!/usr/bin/env bash
set -euo pipefail

# start-day.sh -- Daily operating environment bootstrap
#
# One command to start your day:
#   1. Validates prerequisites (cmux, amp, jq)
#   2. Bootstraps cmux without changing focus (optional explicit focus)
#   3. Opens the Megadesk workspace, launches Amp, pins it to the top,
#      and keeps the tab title stamped as 🧭 CoS
#   4. Creates today's daily note deterministically (idempotent skeleton;
#      CoS populates placeholders asynchronously), rebuilds queue/now state
#      deterministically, then derives today-plan.json through
#      system/scripts/sod-workspace-plan.py. NOTHING blocks on a CoS agent
#      turn — the script owns fresh state; CoS enrichment routes back later.
#   5. Opens only the workstream tabs sod-workspace-plan.py selected (a to-do
#      due today, a meeting today, an unanswered ask, or a P0), each seeded
#      with its short note; everything else stays closed unless --all
#   6. Prints status report and archives the plan
#
# Usage: start-day.sh [--all] [--with-resolvers] [--dry-run] [--focus] [--reuse-only]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# START_DAY_REPO_ROOT: test-only override so the regression harness can point
# file paths at a fixture tree while still sourcing the real helpers.
# The default is the main checkout, not this script's own tree: start-day run
# from a task worktree would relink ~/.config/amp plugins and ~/bin onto
# .amp/worktrees/<task>/..., which dispatch.py deletes at reap. Helpers below still come from SCRIPT_DIR so a worktree can test them.
PM_OS_MAIN_CHECKOUT="${PM_OS_ROOT:-$HOME/Development/pm-os}"
if [[ -n "${START_DAY_REPO_ROOT:-}" ]]; then
    REPO_ROOT="$START_DAY_REPO_ROOT"
elif [[ -d "$PM_OS_MAIN_CHECKOUT/system" ]]; then
    REPO_ROOT="$PM_OS_MAIN_CHECKOUT"
    if [[ "$(cd "$SCRIPT_DIR/.." && pwd)" != "$REPO_ROOT" ]]; then
        echo "start-day: running from $SCRIPT_DIR; REPO_ROOT stays the main checkout $REPO_ROOT" >&2
    fi
else
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

# Export REPO_ROOT before sourcing helpers (helpers use ${REPO_ROOT:-default})
export REPO_ROOT

# shellcheck source=cmux-helpers.sh
source "$SCRIPT_DIR/cmux-helpers.sh"
# shellcheck source=scripts/lib/cmux-inject-guard.sh
source "$SCRIPT_DIR/scripts/lib/cmux-inject-guard.sh"

# --- Constants ---
SYSTEM_DIR="$REPO_ROOT/system"
NOTES_DIR="$REPO_ROOT/notes"
DAILY_DIR="$NOTES_DIR/daily"
WORKSTREAMS_DIR="$REPO_ROOT/workstreams"
PLAN_FILE="$SYSTEM_DIR/today-plan.json"
NOW_FILE="$SYSTEM_DIR/state/now.json"
QUEUE_FILE="$SYSTEM_DIR/state/queue.json"
PLANS_ARCHIVE="$SYSTEM_DIR/plans"
LOGS_DIR="$SYSTEM_DIR/logs"
COS_PROMPT="$SYSTEM_DIR/chief-of-staff-prompt.md"
COS_WORKSPACE_NAME="megadesk"
COS_WORKSPACE_DISPLAY_NAME="Megadesk"
PEOPLE_MAINTENANCE_SCRIPT="$SYSTEM_DIR/scripts/people_maintenance.py"
PEOPLE_SUMMARY_FILE="$SYSTEM_DIR/state/sources/people.json"
PEOPLE_MAINTENANCE_STATE_FILE="$SYSTEM_DIR/state/people-maintenance.json"
AMP_PLUGIN_INSTALLER="$SYSTEM_DIR/scripts/install-amp-plugins.sh"
CMUX_SIDEBAR_INSTALLER="$SYSTEM_DIR/scripts/install-cmux-sidebar.sh"

TODAY=$(date +%Y-%m-%d)
START_EPOCH=$(date +%s)  # freshness floor for deterministic state verification

# Marker line stamped into the deterministic daily-note skeleton. The CoS
# prompt (Step 2) populates the placeholder sections in place and removes this
# line; a note WITHOUT the marker is treated as hand-touched and never modified.
DAILY_NOTE_SKELETON_MARKER="<!-- start-day-skeleton: created by start-day.sh — populate placeholders in place, then remove this line -->"

# --- Flags ---
FLAG_UPGRADE=false
FLAG_ALL=false
FLAG_WITH_RESOLVERS=false
FLAG_DRY_RUN=false
FLAG_FOCUS=false
FLAG_REUSE_ONLY=false

# --- State ---
WORKSTREAMS_FOUND=()
WORKSTREAMS_OPENED=()
WORKSTREAMS_SKIPPED=()
WORKSTREAMS_FAILED=()
COS_REF=""
NOTE_PATH=""
IS_FIRST_RUN=false
NOTE_PREEXISTED=false
COS_ALREADY_RUNNING=false
ROUTER_ORIGIN="${PM_OS_ROUTER_ORIGIN:-0}"

# ============================================================================
# CLI
# ============================================================================

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --upgrade)       log_info "--upgrade is now default (ignored)" ;;
            --all)           FLAG_ALL=true ;;
            --with-resolvers) FLAG_WITH_RESOLVERS=true ;;
            --with-browser-stack) log_warn "--with-browser-stack is retired (Megadesk app archived); ignoring" ;;
            --dry-run)       FLAG_DRY_RUN=true ;;
            --focus)         FLAG_FOCUS=true ;;
            --no-fullscreen) FLAG_FOCUS=false ;; # compatibility: now the default
            --reuse-only)    FLAG_REUSE_ONLY=true ;;
            -h|--help)       print_usage; exit 0 ;;
            *)               log_error "Unknown flag: $1"; print_usage; exit 1 ;;
        esac
        shift
    done
}

print_usage() {
    cat <<'USAGE'
Usage: start-day.sh [OPTIONS]

Bootstrap your daily operating environment in cmux.

Options:
  --upgrade        (no-op, upgrade always runs)
  --all            Open all valid workstreams (ignore today's plan)
  --with-resolvers Open every workstream listed in today-plan.json
  --dry-run        Show what would happen without mutating anything
  --focus          Focus and fullscreen cmux (explicit user action only)
  --no-fullscreen  Deprecated compatibility flag (default behavior)
  --reuse-only     Only reuse existing workspaces, do not create new ones
  -h, --help       Show this help
USAGE
}

# ============================================================================
# Logging setup
# ============================================================================

setup_logging() {
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] File logging disabled"
        return 0
    fi

    mkdir -p "$LOGS_DIR"
    local log_file="$LOGS_DIR/$TODAY.log"
    # Tee all output to log file and stdout
    exec > >(tee -a "$log_file") 2>&1
    log_info "=========================================="
    log_info "Start of day: $TODAY"
    log_info "Log file: $log_file"
    log_info "=========================================="
}

# ============================================================================
# First-run detection
# ============================================================================

detect_first_run() {
    if [[ ! -d "$DAILY_DIR" ]] || [[ ! -d "$PLANS_ARCHIVE" ]] || \
       [[ -z "$(ls -A "$PLANS_ARCHIVE" 2>/dev/null)" ]]; then
        IS_FIRST_RUN=true
    fi
}

print_welcome() {
    cat <<'WELCOME'

+======================================================+
|          Welcome to your Start-of-Day System          |
+======================================================+
|                                                       |
|  This is your first run. Here's what will happen:     |
|                                                       |
|  1. Create notes/daily/ for your daily notes          |
|  2. Create system/plans/ for plan archives            |
|  3. Open Megadesk workspace in cmux                   |
|  4. Amp generates config.yaml for each workstream     |
|     (based on existing CONTEXT.md files)              |
|  5. Script creates today's daily-note skeleton        |
|     (CoS populates it asynchronously)                 |
|  6. Script ranks the queue and writes now.json        |
|  7. Resolver workspaces stay optional                 |
|                                                       |
|  Tip: Run with --dry-run first to preview.            |
|                                                       |
+======================================================+

WELCOME
}

# ============================================================================
# Phase 1: Discovery
# ============================================================================

phase_1_discover() {
    log_info "Phase 1: Discovery"

    # Hard prerequisites
    if [[ ! -d "$REPO_ROOT" ]]; then
        log_error "Repo root not found: $REPO_ROOT"
        exit 1
    fi
    if [[ ! -x "$CMUX_BIN" ]]; then
        log_error "cmux binary not found or not executable: $CMUX_BIN"
        exit 1
    fi
    local agent_cli="$AMP_CMD"
    [[ "$(pm_os_agent)" == "claude" ]] && agent_cli="$CLAUDE_CMD"
    if ! command -v "$agent_cli" >/dev/null 2>&1; then
        log_error "$agent_cli CLI not found in PATH (router.yaml agent: $(pm_os_agent))"
        exit 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        log_error "jq not found (install with: brew install jq)"
        exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        log_error "python3 not found"
        exit 1
    fi
    if ! python3 -c "import yaml" 2>/dev/null; then
        log_error "PyYAML not found (install with: pip3 install pyyaml)"
        exit 1
    fi
    if [[ ! -f "$COS_PROMPT" ]]; then
        log_error "Chief of Staff prompt not found: $COS_PROMPT"
        exit 1
    fi

    log_ok "Prerequisites validated"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would install and verify PM OS Amp plugins"
    elif ! bash "$AMP_PLUGIN_INSTALLER"; then
        log_error "Failed to install or verify PM OS Amp plugins"
        exit 1
    fi

    # Self-heal tracked PM OS CLIs (system/bin/ -> ~/bin symlinks, e.g. amp-mem).
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would install and verify PM OS bin scripts"
    elif ! bash "$SYSTEM_DIR/scripts/install-pm-os-bin.sh"; then
        # A differing runtime copy is skipped, never clobbered; that is a
        # warning, not a reason to cancel the morning.
        log_warn "PM OS bin scripts not fully installed (see warnings above; run: bash system/scripts/install-pm-os-bin.sh --force to back up and replace)"
    fi

    # Snapshot todo state into git + push (morning backstop for days when the
    # end-of-day close didn't run). Best-effort: warn, never abort.
    # A local repair verification must not publish unrelated local commits.
    if [[ "${START_DAY_SKIP_TODO_SNAPSHOT:-0}" == 1 ]]; then
        log_info "Todo state snapshot skipped (START_DAY_SKIP_TODO_SNAPSHOT=1)"
    elif [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would commit + push todo state snapshot"
    elif ! bash "$SYSTEM_DIR/scripts/commit-todo-state.sh"; then
        log_warn "Todo state snapshot failed (run: bash system/scripts/commit-todo-state.sh)"
    fi

    # Self-heal tracked skill overrides onto the runtime skills (todo CLI
    # atomicity/locking, CoS read-only todo policy, etc). Skill reinstalls
    # overwrite runtime copies; this restores them from tracked source every
    # morning so the fixes survive without relying on ~/bin/install-amp-skills.sh.
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would apply tracked skill overrides"
    elif ! bash "$SYSTEM_DIR/scripts/sync-skill-overrides.sh" >/dev/null 2>&1; then
        log_warn "Skill override sync failed — runtime skills may be missing pm-os fixes (run: bash system/scripts/sync-skill-overrides.sh)"
    else
        log_ok "Skill overrides applied"
    fi

    # Self-heal the pm-os cmux sidebar symlink (cosmetic — warn, never abort).
    # The sidebar is a macOS Swift build; wmux lanes come from the shim's sidecar,
    # so off macOS there is nothing to install.
    if [[ "$(uname -s)" != "Darwin" ]]; then
        :
    elif [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would install pm-os cmux sidebar symlink"
    else
        bash "$CMUX_SIDEBAR_INSTALLER" >/dev/null 2>&1 || \
            log_warn "Failed to install pm-os cmux sidebar symlink (non-fatal)"
    fi

    # Discover workstreams (directories with CONTEXT.md, excluding shared/)
    local ws_dir ws_name
    for ws_dir in "$WORKSTREAMS_DIR"/*/; do
        [[ ! -d "$ws_dir" ]] && continue
        ws_name=$(basename "$ws_dir")
        [[ "$ws_name" == "shared" ]] && continue
        [[ -f "$ws_dir/CONTEXT.md" ]] || continue
        WORKSTREAMS_FOUND+=("$ws_name")
    done

    log_info "Found ${#WORKSTREAMS_FOUND[@]} workstreams: ${WORKSTREAMS_FOUND[*]+"${WORKSTREAMS_FOUND[*]}"}"

    # Ensure directories exist unless this is a mutation-free preview.
    if [[ "$FLAG_DRY_RUN" != "true" ]]; then
        mkdir -p "$DAILY_DIR" "$PLANS_ARCHIVE" "$LOGS_DIR" "$NOTES_DIR/sensitive"
    fi

    NOTE_PATH="$DAILY_DIR/$TODAY.md"
    if [[ -f "$NOTE_PATH" ]]; then
        NOTE_PREEXISTED=true
    fi

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Daily note path: $NOTE_PATH"
        [[ -f "$NOTE_PATH" ]] && log_info "[DRY RUN] Note already exists (will skip creation)"

        log_info "[DRY RUN] Workstream configs:"
        for ws_name in ${WORKSTREAMS_FOUND[@]+"${WORKSTREAMS_FOUND[@]}"}; do
            local cfg="$WORKSTREAMS_DIR/$ws_name/config.yaml"
            if [[ -f "$cfg" ]]; then
                local priority
                priority=$(python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)
print(d.get('priority', 'unknown'))
" "$cfg" 2>/dev/null || echo "unknown")
                log_info "  $ws_name: config.yaml exists (priority=$priority)"
            else
                log_info "  $ws_name: no config.yaml (will be generated on first run)"
            fi
        done
    fi
}

# ============================================================================
# Phase 2: cmux Bootstrap
# ============================================================================

phase_2_cmux_bootstrap() {
    log_info "Phase 2: cmux bootstrap"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        if cmux_cmd ping >/dev/null 2>&1; then
            log_info "[DRY RUN] cmux is running"
            local ws_list
            ws_list=$(cmux_cmd list-workspaces 2>/dev/null || true)
            local ws_count
            ws_count=$(echo "$ws_list" | grep -c 'workspace:' || true)
            log_info "[DRY RUN] Found $ws_count existing workspace(s) (managed ones reused in Phase 2b)"
        else
            log_info "[DRY RUN] cmux not running, would launch"
        fi
        return 0
    fi

    # Check if cmux is responding
    if ! cmux_cmd ping >/dev/null 2>&1; then
        log_info "cmux not responding, launching in the background..."
        open -g -a cmux
        local elapsed=0
        while ! cmux_cmd ping >/dev/null 2>&1; do
            sleep 1
            (( elapsed += 1 ))
            if (( elapsed > 60 )); then
                log_error "cmux failed to start after 60s"
                log_error ""
                log_error "This usually means cmux's Socket Control Mode is set to 'cmux only',"
                log_error "which blocks connections from external terminals."
                log_error ""
                log_error "Fix: cmux Settings > Automation > Socket Control Mode > 'Full open access'"
                exit 1
            fi
        done
        log_ok "cmux started (${elapsed}s)"
    else
        log_ok "cmux already running"
    fi

    # Focusing and fullscreen transitions are allowed only when the user asks
    # for them explicitly. Scheduled and background starts preserve focus.
    if [[ "$FLAG_FOCUS" == "true" ]]; then
        osascript -e 'tell application "cmux" to activate' 2>/dev/null || true
        osascript -e '
            tell application "System Events"
                tell process "cmux"
                    try
                        set value of attribute "AXFullScreen" of window 1 to true
                    end try
                end tell
            end tell
        ' 2>/dev/null || log_warn "Fullscreen failed (non-critical)"
    fi
}

# ============================================================================
# Phase 2b: Inventory existing managed workspaces (non-destructive)
# ============================================================================

phase_2b_cleanup_workspaces() {
    log_info "Phase 2b: Inventorying existing managed workspaces"

    if COS_REF=$(find_workspace_ref_for_key "megadesk" 2>/dev/null); then
        COS_ALREADY_RUNNING=true
        log_info "Detected existing $COS_WORKSPACE_DISPLAY_NAME workspace: $COS_REF"
    else
        COS_REF=""
    fi

    if [[ "$FLAG_REUSE_ONLY" == "true" ]]; then
        log_info "Skipping workspace cleanup (--reuse-only)"
        return 0
    fi

    local ws_list
    ws_list=$(cmux_cmd list-workspaces 2>/dev/null || true)

    if [[ -z "$ws_list" ]]; then
        log_info "No existing workspaces to clean"
        return 0
    fi

    local -a managed_workspace_names=()
    local candidate routine_dir routine_name

    append_managed_workspace_name() {
        local item="$1"
        local existing
        [[ -z "$item" ]] && return 0
        for existing in "${managed_workspace_names[@]+"${managed_workspace_names[@]}"}"; do
            [[ "$existing" == "$item" ]] && return 0
        done
        managed_workspace_names+=("$item")
    }

    if [[ "$COS_ALREADY_RUNNING" != "true" ]]; then
        append_managed_workspace_name "🧭 CoS"
        append_managed_workspace_name "CoS"
        append_managed_workspace_name "$COS_WORKSPACE_DISPLAY_NAME"
        append_managed_workspace_name "Chief of Staff"
        append_managed_workspace_name "megadesk"
    fi

    for ws_name in ${WORKSTREAMS_FOUND[@]+"${WORKSTREAMS_FOUND[@]}"}; do
        append_managed_workspace_name "$(format_workspace_name_for_source workstream "$ws_name")"
        append_managed_workspace_name "$(format_legacy_workspace_name "$ws_name")"
        append_managed_workspace_name "$ws_name"
    done

    for routine_dir in "$REPO_ROOT/routines"/*/; do
        [[ ! -d "$routine_dir" ]] && continue
        routine_name=$(basename "$routine_dir")
        [[ -f "$routine_dir/CONTEXT.md" ]] || continue
        if [[ "$COS_ALREADY_RUNNING" == "true" && "$routine_name" == "$COS_WORKSPACE_NAME" ]]; then
            continue
        fi
        append_managed_workspace_name "$(format_workspace_name_for_source routine "$routine_name")"
        append_managed_workspace_name "$(format_legacy_workspace_name "$routine_name")"
        append_managed_workspace_name "$routine_name"
    done

    # NON-DESTRUCTIVE: existing managed workspaces are inventoried and reused,
    # never closed here. Later phases go through prepare_workspace /
    # find_or_create_workspace, which reuse an existing workspace by name and
    # preserve its live Amp session and thread. Lifecycle decisions
    # ("work here is done, close it" vs "continue today") belong to the CoS
    # open-workspace audit, which runs with full context after launch — not to
    # this pre-CoS phase, which would destroy the very threads the audit needs
    # to evaluate.
    local count=0
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local ref
        ref=$(echo "$line" | grep -oE 'workspace:[0-9]+' || true)
        [[ -z "$ref" ]] && continue

        local ws_name
        ws_name=$(echo "$line" | sed -E 's/^[* ]*workspace:[0-9]+  //' | sed 's/ *\[selected\]$//')

        local is_managed=false
        for candidate in "${managed_workspace_names[@]+"${managed_workspace_names[@]}"}"; do
            if [[ "$ws_name" == "$candidate" ]]; then
                is_managed=true
                break
            fi
        done
        if [[ "$is_managed" != "true" ]]; then
            continue
        fi

        log_info "Existing managed workspace will be reused: $ws_name ($ref)"
        (( count += 1 ))
    done <<< "$ws_list"

    if (( count > 0 )); then
        log_ok "Found $count existing managed workspace(s); reusing (CoS audit owns close decisions)"
    else
        log_info "No existing managed workspaces found"
    fi

    unset -f append_managed_workspace_name
}

# ============================================================================
# Optional: Update Amp CLI
# ============================================================================

# `amp upgrade` was removed from the Amp CLI; the supported command is
# `amp update` (alias `up`). Failures are non-critical for the morning launch
# but must stay VISIBLE: output passes through untouched and the warn names
# the real command so a genuine update failure is never hidden.
update_amp() {
    # Only Amp is updated here. Claude Code updates itself in the background,
    # so a claude knob (router.yaml `agent:`) skips this step.
    if [[ "$(pm_os_agent)" != "amp" ]]; then
        log_info "Agent is $(agent_display_name); skipping amp update"
        return 0
    fi
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would run: amp update"
        return 0
    fi

    log_info "Updating Amp CLI (amp update)..."
    if "$AMP_CMD" update 2>&1; then
        log_ok "amp update completed"
    else
        log_warn "amp update failed (non-critical, continuing) — see output above"
    fi
}

# ============================================================================
# Phase 1b: Deterministic daily note
# ============================================================================

# Creates notes/daily/$TODAY.md idempotently, independent of any CoS agent
# turn, so a busy CoS tab never blocks it.
# - Missing note  -> write the canonical skeleton (atomic tmp+mv) + marker.
# - Existing note -> never touched, whatever its content.
# Returns non-zero only when creation was attempted and verification failed.
ensure_daily_note() {
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        if [[ -f "$NOTE_PATH" ]]; then
            log_info "[DRY RUN] Daily note exists, would leave untouched: $NOTE_PATH"
        else
            log_info "[DRY RUN] Would create daily-note skeleton: $NOTE_PATH"
        fi
        return 0
    fi

    if [[ -f "$NOTE_PATH" ]]; then
        log_info "Daily note already exists: $NOTE_PATH"
        return 0
    fi

    mkdir -p "$DAILY_DIR"
    local tmp_note
    tmp_note=$(mktemp "$DAILY_DIR/.$TODAY.md.XXXXXX") || {
        log_error "Daily note creation failed: mktemp error in $DAILY_DIR"
        return 1
    }

    cat > "$tmp_note" <<EOF
# $TODAY
$DAILY_NOTE_SKELETON_MARKER

## Priorities
- [ ] (derive from carry-forwards, workstream state, calendar, to-do items)

## Carry Forward
- [ ] (incomplete items from yesterday's note)
- [ ] (unresolved follow-ups)

## To-Do (top 5 actionable)

### ⚡ Quick hits (30-45 min)
| # | To do | Priority | Description | Action I need to take | Workspace | Link |
|---|---|---|---|---|---|---|

### ✍️ Medium lifts (1-2 hours)
| # | To do | Priority | Description | Action I need to take | Workspace | Link |
|---|---|---|---|---|---|---|

### 🧠 Deep work (2-4 hours)
| # | To do | Priority | Description | Action I need to take | Workspace | Link |
|---|---|---|---|---|---|---|

## Calendar
- (today's meetings, times, and prep needed)

## Notes

## Decisions

## Follow-ups
EOF

    # A hard link publishes within the same directory only when NOTE_PATH is
    # still absent. Unlike mv, it cannot replace a note created after our
    # initial check.
    if ln "$tmp_note" "$NOTE_PATH" 2>/dev/null; then
        rm -f "$tmp_note"
    else
        rm -f "$tmp_note"
        if [[ -f "$NOTE_PATH" ]]; then
            log_info "Daily note was created concurrently; leaving it untouched: $NOTE_PATH"
            return 0
        fi
        log_error "Daily note creation failed: could not publish skeleton at $NOTE_PATH"
        return 1
    fi

    # Verify: exists, non-empty, carries today's heading.
    if [[ -s "$NOTE_PATH" ]] && grep -q "^# $TODAY" "$NOTE_PATH"; then
        log_ok "Daily note created (skeleton): $NOTE_PATH"
        return 0
    fi

    log_error "Daily note verification failed: $NOTE_PATH missing or malformed after write"
    return 1
}

# ============================================================================
# Phase 3: Megadesk Bootstrap
# ============================================================================

refresh_comms_review_state() {
    if declare -F pm_os_comms_triage_enabled >/dev/null && ! pm_os_comms_triage_enabled; then
        log_info "Comms Triage is off on this machine; skipping the comms collector"
    else
        log_info "Refreshing comms candidates for CoS planning"
        python3 "$SYSTEM_DIR/scripts/collect-comms-candidates.py" >/dev/null 2>&1 || \
            log_warn "Failed to refresh comms candidates; CoS will use the existing queue and comms state"
    fi
    python3 "$SYSTEM_DIR/scripts/rebuild-megadesk-state.py" --write --event-source start-day-pre-cos >/dev/null 2>&1 || \
        log_warn "Failed to refresh the Megadesk queue with the latest comms candidates"
    publish_megadesk_status
}

post_start_day_review() {
    local result status
    if ! result=$(COS_WORKSPACE_REF="$COS_REF" bash "$SYSTEM_DIR/scripts/post-next-up-digest.sh" --surface start-day 2>&1); then
        log_warn "Start-day comms review did not post: ${result:-unknown error}"
        return 0
    fi
    status=$(printf '%s\n' "$result" | jq -r '.status // "unknown"' 2>/dev/null || printf 'unknown')
    case "$status" in
        posted)
            log_ok "Posted the start-day review to CoS"
            ;;
        deferred)
            log_info "CoS is busy or drafting; the start-day review remains pending for the next NEXT-UP post"
            ;;
        start-day-skip)
            log_info "The start-day review already posted today"
            ;;
        *)
            log_info "Start-day review status: $status"
            ;;
    esac
}

# Rotate the CoS Amp thread once per day at start of day: at SoD only, via
# split-thread.sh with a seeded recall pack, with no compaction-count trigger. Runs only on the reuse paths
# (a fresh CoS launch is already a fresh thread). Never fatal: a skip or a
# failure leaves COS_REF pointing at the surface that will receive the review.
rotate_cos_thread_at_start_of_day() {
    local result status new_ref new_thread
    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would rotate the CoS thread via rotate-cos-thread.sh"
        return 0
    fi
    if ! result=$(bash "$SYSTEM_DIR/scripts/rotate-cos-thread.sh" 2>/dev/null); then
        log_warn "CoS thread rotation failed: ${result:-unknown error}; keeping $COS_REF"
        return 0
    fi
    status=$(printf '%s\n' "$result" | tail -1 | jq -r '.status // "unknown"' 2>/dev/null || printf 'unknown')
    case "$status" in
        rotated)
            new_ref=$(printf '%s\n' "$result" | tail -1 | jq -r '.new_ref // ""' 2>/dev/null || true)
            new_thread=$(printf '%s\n' "$result" | tail -1 | jq -r '.new_thread // ""' 2>/dev/null || true)
            if [[ "$new_ref" == workspace:* ]]; then
                COS_REF="$new_ref"
                log_ok "Rotated CoS to thread ${new_thread:-?} in $COS_REF"
            else
                log_warn "CoS rotation reported no workspace ref; keeping $COS_REF"
            fi
            ;;
        skipped)
            log_info "CoS thread rotation skipped: $(printf '%s\n' "$result" | tail -1 | jq -r '.reason // "no reason"' 2>/dev/null || true)"
            ;;
        *)
            log_warn "CoS thread rotation status: $status; keeping $COS_REF"
            ;;
    esac
    return 0
}

phase_3_chief_of_staff() {
    log_info "Phase 3: $COS_WORKSPACE_DISPLAY_NAME bootstrap"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        if [[ "$COS_ALREADY_RUNNING" == "true" && -n "$COS_REF" ]]; then
            log_info "[DRY RUN] Would reuse existing $COS_WORKSPACE_DISPLAY_NAME workspace: $COS_REF"
            log_info "[DRY RUN] Would rotate the CoS thread via rotate-cos-thread.sh (once per PT date)"
            log_info "[DRY RUN] Would post only the bounded start-day review to CoS"
        else
            log_info "[DRY RUN] Would prepare workspace: $COS_WORKSPACE_NAME"
            log_info "[DRY RUN] Workspace missing, would create"
            log_info "[DRY RUN] Would initialize CoS with only the bounded start-day review"
        fi
        return 0
    fi

    refresh_comms_review_state

    if [[ "$COS_ALREADY_RUNNING" == "true" && -n "$COS_REF" ]]; then
        log_info "Reusing existing $COS_WORKSPACE_DISPLAY_NAME workspace without reset: $COS_REF"
        rotate_cos_thread_at_start_of_day
        style_megadesk_workspace "$COS_REF"
        arrange_workspace_sidebar
        set_workspace_status "$COS_REF" "role" "$COS_WORKSPACE_DISPLAY_NAME" "" "#4A90D9"
        post_start_day_review
        log_ok "Reused CoS and posted only the bounded start-day review"
        return 0
    fi

    # Remove stale plan file from a prior run
    rm -f "$PLAN_FILE"

    # Prepare the workspace (manual steps so we can upgrade claude inside cmux first)
    COS_REF=$(find_or_create_workspace "$COS_WORKSPACE_NAME" "$REPO_ROOT") || {
        log_warn "Failed to initialize $COS_WORKSPACE_DISPLAY_NAME; continuing with background routines"
        return 0
    }

    set_workspace_status "$COS_REF" "status" "booting" "" "#FFA500"
    local cos_reset_rc=0
    reset_workspace_surface "$COS_REF" || cos_reset_rc=$?
    if (( cos_reset_rc == 3 )); then
        # A live (or unverifiable) Amp session is already in the Megadesk tab
        # even though phase 2b didn't flag it as running. NOTHING was typed
        # into it. Preserve it and fall back to the
        # guarded-refresh reuse path instead of typing a launch command.
        log_warn "Live Amp session preserved in Megadesk tab $COS_REF; reusing it without reset"
        rotate_cos_thread_at_start_of_day
        style_megadesk_workspace "$COS_REF"
        arrange_workspace_sidebar
        set_workspace_status "$COS_REF" "role" "$COS_WORKSPACE_DISPLAY_NAME" "" "#4A90D9"
        post_start_day_review
        log_ok "Preserved CoS and posted only the bounded start-day review"
        return 0
    elif (( cos_reset_rc != 0 )); then
        set_workspace_status "$COS_REF" "status" "failed" "" "#FF0000"
        log_warn "Could not clear the Megadesk surface ($COS_REF); skipping CoS launch (non-fatal)"
        return 0
    fi

    # Now launch the configured agent (router.yaml `agent:`)
    log_info "Launching $(agent_display_name) in: $COS_WORKSPACE_NAME"
    # CoS launches in ultra. The mode comes from
    # routines/megadesk/config.yaml via resolve_agent_mode, which also floors
    # core surfaces to ultra — never hardcode -m here. A dated
    # `cos_mode_trial:` window in router.yaml may lower CoS alone.
    local cos_agent_mode
    cos_agent_mode="$(resolve_agent_mode "$COS_WORKSPACE_NAME")"
    # CoS starts with Remote Control on (cos_remote_control_args).
    send_to_workspace "$COS_REF" "cd $REPO_ROOT && $(agent_interactive_command "$cos_agent_mode" "$COS_WORKSPACE_NAME")" || {
        log_warn "CoS launch send failed; preserving its surface and continuing (non-fatal)"
        return 0
    }

    if wait_for_amp_ready "$COS_REF"; then
        set_workspace_status "$COS_REF" "status" "ready" "" "#00CC00"
        log_ok "$(agent_display_name) ready in: $COS_WORKSPACE_NAME"
    else
        set_workspace_status "$COS_REF" "status" "failed" "" "#FF0000"
        log_warn "$(agent_display_name) failed to start in: $COS_WORKSPACE_NAME (non-fatal)"
        return 0
    fi

    # Keep Megadesk visually stable in cmux even when Amp tries to
    # replace the active surface title with its own session text.
    style_megadesk_workspace "$COS_REF"

    # Keep the Megadesk and core routine tabs in the expected sidebar order.
    arrange_workspace_sidebar

    # Set sidebar badges
    set_workspace_status "$COS_REF" "role" "$COS_WORKSPACE_DISPLAY_NAME" "" "#4A90D9"
    set_workspace_status "$COS_REF" "status" "orienting" "" "#FFA500"

    log_info "Initializing CoS with only the bounded start-day review"
    post_start_day_review
    style_megadesk_workspace "$COS_REF"
    set_workspace_status "$COS_REF" "status" "ready" "" "#00CC00"
}

# ============================================================================
# Phase 4: Verify Deterministic State
# ============================================================================
#
# Background automation must not block on an interactive agent turn:
#   - the daily note is created deterministically by ensure_daily_note()
#   - queue.json/now.json are rebuilt deterministically in Phase 4b
#   - this phase only VERIFIES freshness, repairs loudly if needed, derives
#     the compatibility plan, and ALWAYS returns so fan-out still runs.
# CoS enrichment of the same state files continues asynchronously.

# Both state files parse and contain the expected top-level keys.
megadesk_state_valid() {
    [[ -f "$NOW_FILE" && -f "$QUEUE_FILE" ]] &&
        jq -e '.now' "$NOW_FILE" >/dev/null 2>&1 &&
        jq -e '.queue' "$QUEUE_FILE" >/dev/null 2>&1
}

# Publish the Megadesk sidebar tile without discarding failures. Benign skips
# (no cmux, no megadesk workspace, dynamic UI disabled) exit 0 inside the
# publisher and stay quiet here; a nonzero exit means status writes actually
# failed and the tile may be stale, which deserves a visible warning.
publish_megadesk_status() {
    local publish_err
    if ! publish_err=$(bash "$SYSTEM_DIR/scripts/publish-megadesk-status.sh" 2>&1 >/dev/null); then
        log_warn "Megadesk status publish failed: ${publish_err:-unknown error}"
    fi
}

# Both state files were written during THIS run (mtime >= script start).
megadesk_state_fresh() {
    local now_m queue_m
    now_m=$(mux_mtime "$NOW_FILE" || echo 0)
    queue_m=$(mux_mtime "$QUEUE_FILE" || echo 0)
    (( now_m >= START_EPOCH && queue_m >= START_EPOCH ))
}

# Always derive a FRESH plan for today. The start-of-day rule: open only the workstreams with a to-do due today, a meeting
# today, an unanswered ask, or a P0, and seed each with a short note.
# system/scripts/sod-workspace-plan.py owns selection and seed content; this
# script only launches what it lists. Rebuild when the plan is missing,
# invalid, dated, produced by another source, or older than the queue or
# todo state it projects, including a same-day refresh.
derive_compat_plan() {
    local plan_date="" plan_source="" todo_file="${TODO_FILE:-$SYSTEM_DIR/state/todo.json}"
    if [[ -f "$PLAN_FILE" ]]; then
        plan_date=$(jq -r '.date // ""' "$PLAN_FILE" 2>/dev/null || true)
        plan_source=$(jq -r '.plan_source // ""' "$PLAN_FILE" 2>/dev/null || true)
    fi
    if [[ ! -f "$PLAN_FILE" ]] || ! jq -e '.workstreams_to_open' "$PLAN_FILE" >/dev/null 2>&1 \
        || [[ "$plan_date" != "$TODAY" || "$plan_source" != "sod-workspace-plan" ]] \
        || [[ "$QUEUE_FILE" -nt "$PLAN_FILE" || "$todo_file" -nt "$PLAN_FILE" ]]; then
        log_info "Deriving today's workspace plan for $TODAY (was: ${plan_date:-none}, source: ${plan_source:-none})"
        python3 "$SYSTEM_DIR/scripts/sod-workspace-plan.py" \
            --write \
            --date "$TODAY" \
            --today-note "$NOTE_PATH" >/dev/null 2>&1 || {
            log_warn "Failed to derive today's workspace plan (sod-workspace-plan.py); no workstream tabs will auto-open"
        }
    fi
}

phase_4_verify_state() {
    log_info "Phase 4: Verifying deterministic queue state ($NOW_FILE, $QUEUE_FILE)"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would verify queue/now freshness (rebuilt in Phase 4b; no polling, no CoS dependency)"
        log_info "[DRY RUN] Would derive $PLAN_FILE with sod-workspace-plan.py if missing or stale"
        return 0
    fi

    # Phase 4b rebuilds state synchronously just before this runs. If that
    # rebuild failed or was skipped, repair here — loudly, with output visible.
    if ! megadesk_state_valid || ! megadesk_state_fresh; then
        log_warn "Queue state missing or stale after Phase 4b rebuild; rebuilding deterministically"
        if ! python3 "$SYSTEM_DIR/scripts/rebuild-megadesk-state.py" --write --event-source start-day-verify; then
            log_warn "Deterministic queue rebuild FAILED (rebuild-megadesk-state.py exited non-zero)"
        fi
    fi

    if megadesk_state_valid; then
        if ! megadesk_state_fresh; then
            local stale_ts queue_epoch
            queue_epoch=$(mux_mtime "$QUEUE_FILE" || echo 0)
            # BSD date reads -r as epoch seconds, GNU date as a file name, so try both.
            stale_ts=$(date -r "$queue_epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
                || date -d "@$queue_epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
                || echo "unknown")
            log_warn "Using STALE but valid queue state (last write: $stale_ts) — rebuild did not refresh it"
        fi
        derive_compat_plan
        log_ok "Queue state ready (deterministic; CoS enrichment continues asynchronously)"
        set_workspace_status "$COS_REF" "status" "plan ready" "" "#00CC00"
        publish_megadesk_status
        return 0
    fi

    # Genuine failure: even the deterministic rebuild could not produce valid
    # state. Stay loud, mark the workspace, and CONTINUE — phase 5 degrades to
    # an empty plan instead of being skipped entirely.
    log_warn "============================================"
    log_warn "No usable queue state: now.json/queue.json invalid even after rebuild"
    log_warn "Resolver fan-out will degrade to an empty plan."
    log_warn "Repair manually: python3 system/scripts/rebuild-megadesk-state.py --write"
    log_warn "============================================"
    set_workspace_status "$COS_REF" "status" "state failed" "" "#FF0000"
    publish_megadesk_status
    return 0
}

# Fallback when plan file is missing: launch workstreams with priority=high in config.yaml
phase_5_fallback_launch() {
    log_info "Fallback: scanning config.yaml for high-priority workstreams"

    local ws_name priority
    for ws_name in ${WORKSTREAMS_FOUND[@]+"${WORKSTREAMS_FOUND[@]}"}; do
        local cfg="$WORKSTREAMS_DIR/$ws_name/config.yaml"
        [[ -f "$cfg" ]] || continue

        priority=$(python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)
print(d.get('priority', 'medium'))
" "$cfg" 2>/dev/null || echo "medium")

        if [[ "$priority" == "high" ]]; then
            log_info "Fallback launching: $ws_name (priority=high)"
            launch_single_workstream "$ws_name" "" "high" || log_warn "Workstream launcher failed for $ws_name (non-fatal)"
        fi
    done
}

# ============================================================================
# Phase 4b: Background Collectors
# ============================================================================

phase_4b_launch_routines() {
    log_info "Phase 4b: Background collectors"
    local routine_launcher="$SYSTEM_DIR/scripts/ensure-routine-workspaces.sh"
    local p0_launcher="$SYSTEM_DIR/scripts/ensure-p0-workspaces.sh"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would refresh generated workstream registry, Megadesk queue, and launch routine workspaces: todo, launchpad, comms-triage (unless off on this machine), scheduled-jobs, meetings. A personal machine (PM_OS_MACHINE_ROLE=personal) opens only todo and leaves launchpad, scheduled-jobs, and meetings for \"load <surface>\" unless loaded or kept today."
        log_info "[DRY RUN] Would reconcile P0 workspaces (canonical workstream P0 tabs + unlinked reminder cards, sidebar P0 lane):"
        bash "$p0_launcher" --dry-run || true
        return 0
    fi

    python3 "$SYSTEM_DIR/scripts/render-workstreams-readme.py" --write >/dev/null 2>&1 || log_warn "Failed to refresh generated workstreams README"
    python3 "$SYSTEM_DIR/scripts/rebuild-megadesk-state.py" --write --event-source start-day >/dev/null 2>&1 || log_warn "Failed to refresh megadesk queue"
    publish_megadesk_status

    # ensure-routine-workspaces.sh applies the machine role (system/scripts/machine_role.py):
    # a personal machine skips Launchpad, Scheduled Jobs, and Meetings unless the owner
    # loaded or kept one, and the command-center tick keeps their background state fresh.
    # The sidebar order (sections) applies on cmux only; wmux cannot order tabs
    #, so every machine opens the routines in one pass.
    bash "$routine_launcher" || log_warn "Routine workspace launcher failed (non-fatal)"

    # Product-specific daily launchers (knowledge-base refresh, beta sweeps,
    # research announcements) go here. Each must be non-fatal: `bash x || log_warn`.

    # P0 to-do reminder workspaces: one local workspace per active P0 todo,
    # titled "<emoji> P0 · <title>" so the pm-os sidebar files them in the P0 lane
    # (below Idle, above Background). Reminder cards only — they never execute
    # the task. Idempotent; also reconciles stale reminders whose todo is no
    # longer an active P0. Failure warns but never aborts the morning launch.
    bash "$p0_launcher" || log_warn "P0 reminder workspace launcher failed (non-fatal)"
}

# ============================================================================
# Phase 5: Launch Workstreams
# ============================================================================

phase_5_launch_workstreams() {
    log_info "Phase 5: Opening today's workstream tabs (to-do due today, meeting, unanswered ask, or P0)"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        if [[ "$FLAG_ALL" == "true" ]]; then
            log_info "[DRY RUN] --all: would launch all ${#WORKSTREAMS_FOUND[@]} workstreams"
        elif [[ "$FLAG_WITH_RESOLVERS" == "true" ]]; then
            log_info "[DRY RUN] Would launch every workstream listed in the plan file"
        else
            log_info "[DRY RUN] Plan sod-workspace-plan.py would produce for $TODAY:"
            python3 "$SYSTEM_DIR/scripts/sod-workspace-plan.py" --dry-run --date "$TODAY" 2>/dev/null \
                || log_warn "[DRY RUN] sod-workspace-plan.py failed; nothing would auto-open"
        fi
        return 0
    fi

    if [[ "$FLAG_ALL" == "true" ]]; then
        log_info "Flag --all: launching all discovered workstreams"
        for ws_name in ${WORKSTREAMS_FOUND[@]+"${WORKSTREAMS_FOUND[@]}"}; do
            launch_single_workstream "$ws_name" "" "medium" || log_warn "Workstream launcher failed for $ws_name (non-fatal)"
        done
        return 0
    fi

    derive_compat_plan

    local count
    count=$(jq -r '.workstreams_to_open | length' "$PLAN_FILE" 2>/dev/null || echo 0)

    local auto_open_count
    auto_open_count=$(jq -r '[.workstreams_to_open[]? | select(.open_now == true)] | length' "$PLAN_FILE" 2>/dev/null || echo 0)

    if [[ "$FLAG_WITH_RESOLVERS" != "true" ]]; then
        if (( auto_open_count > 0 )); then
            log_info "Opening $auto_open_count workstream tab(s) with something due today"

            local i
            for (( i = 0; i < count; i++ )); do
                local ws_name ws_prompt ws_priority ws_open_now
                ws_name=$(jq -r ".workstreams_to_open[$i].name" "$PLAN_FILE")
                ws_prompt=$(jq -r ".workstreams_to_open[$i].prompt" "$PLAN_FILE")
                ws_priority=$(jq -r ".workstreams_to_open[$i].priority // \"medium\"" "$PLAN_FILE")
                ws_open_now=$(jq -r ".workstreams_to_open[$i].open_now // false" "$PLAN_FILE")

                if [[ "$ws_open_now" == "true" ]]; then
                    launch_single_workstream "$ws_name" "$ws_prompt" "$ws_priority" || log_warn "Workstream launcher failed for $ws_name (non-fatal)"
                else
                    local recommended_name
                    recommended_name=$(jq -r ".workstreams_to_open[$i].display_name // .workstreams_to_open[$i].name" "$PLAN_FILE")
                    WORKSTREAMS_SKIPPED+=("$recommended_name (kept in queue, not auto-opened today)")
                fi
            done
            return 0
        fi

        log_info "No workstream has a to-do due today, a meeting, an unanswered ask, or a P0; no workstream tabs opened (use --all to open everything)"

        if [[ -f "$PLAN_FILE" ]]; then
            local recommended_count
            recommended_count=$(jq -r '.workstreams_to_open | length' "$PLAN_FILE" 2>/dev/null || echo 0)
            local i
            for (( i = 0; i < recommended_count; i++ )); do
                local recommended_name
                recommended_name=$(jq -r ".workstreams_to_open[$i].display_name // .workstreams_to_open[$i].name" "$PLAN_FILE")
                WORKSTREAMS_SKIPPED+=("$recommended_name (kept in queue, not auto-opened)")
            done
        fi
        return 0
    fi

    if (( count == 0 )); then
        log_info "No workstreams to open today"
    else
        local i
        for (( i = 0; i < count; i++ )); do
            local ws_name ws_prompt ws_priority
            ws_name=$(jq -r ".workstreams_to_open[$i].name" "$PLAN_FILE")
            ws_prompt=$(jq -r ".workstreams_to_open[$i].prompt" "$PLAN_FILE")
            ws_priority=$(jq -r ".workstreams_to_open[$i].priority // \"medium\"" "$PLAN_FILE")
            launch_single_workstream "$ws_name" "$ws_prompt" "$ws_priority" || log_warn "Workstream launcher failed for $ws_name (non-fatal)"
        done
    fi

    # Track skipped workstreams from plan
    local skip_count
    skip_count=$(jq -r '.workstreams_skipped | length' "$PLAN_FILE" 2>/dev/null || echo 0)
    local i
    for (( i = 0; i < skip_count; i++ )); do
        local skip_name skip_reason
        skip_name=$(jq -r ".workstreams_skipped[$i].display_name // .workstreams_skipped[$i].name" "$PLAN_FILE")
        skip_reason=$(jq -r ".workstreams_skipped[$i].reason" "$PLAN_FILE")
        WORKSTREAMS_SKIPPED+=("$skip_name ($skip_reason)")
    done
}

launch_single_workstream() {
    local ws_name="$1"
    local ws_prompt="$2"
    local ws_priority="${3:-medium}"

    log_info "Launching: $ws_name (priority: $ws_priority)"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would launch: $ws_name"
        WORKSTREAMS_OPENED+=("$ws_name")
        return 0
    fi

    local ws_context_dir="workstreams"
    if [[ ! -d "$REPO_ROOT/workstreams/$ws_name" && -d "$REPO_ROOT/routines/$ws_name" ]]; then
        ws_context_dir="routines"
    fi

    # --reuse-only: skip if workspace doesn't already exist
    if [[ "$FLAG_REUSE_ONLY" == "true" ]]; then
        local source_type="workstream"
        if [[ "$ws_context_dir" == "routines" ]]; then
            source_type="routine"
        fi
        if ! find_workspace_ref_for_source_key "$source_type" "$ws_name" >/dev/null 2>&1; then
            log_warn "Skipping $ws_name (--reuse-only, no existing workspace)"
            WORKSTREAMS_SKIPPED+=("$ws_name (no existing workspace)")
            return 0
        fi
    fi

    # P0-linked workstream (invariant): the canonical space is the
    # "<emoji> P0 · <name>" tab. If ANY tab already exists for this workstream
    # (P0-named or normal-named), reuse it WITHOUT prepare_workspace — its
    # surface reset would kill the live session. Rename a normal tab into the
    # P0 title instead. Only a missing tab falls through to the full prepare.
    local ws_ref=""
    local reused_p0="false"
    local p0_linked="false"
    if [[ "$ws_context_dir" == "workstreams" ]] \
        && declare -F workstream_has_active_p0 >/dev/null 2>&1 \
        && workstream_has_active_p0 "$ws_name"; then
        p0_linked="true"
        local p0_canonical_name
        p0_canonical_name=$(p0_workspace_name_for_workstream "$ws_name")
        if ws_ref=$(workspace_ref_if_exists "$ws_name" 2>/dev/null); then
            local cur_name
            cur_name=$(workspace_name_for_ref_in_listing "$(cmux_cmd list-workspaces 2>/dev/null || true)" "$ws_ref" 2>/dev/null || true)
            if [[ -n "$cur_name" && "$cur_name" != "$p0_canonical_name" ]]; then
                log_info "Consolidating open tab into P0 space: $cur_name ($ws_ref) -> $p0_canonical_name"
                cmux_cmd rename-workspace --workspace "$ws_ref" "$p0_canonical_name" &>/dev/null || \
                    log_warn "Failed to rename $ws_ref to: $p0_canonical_name"
            fi
            reused_p0="true"
            log_info "Reusing canonical P0 workspace for $ws_name ($ws_ref); no surface reset"
        fi
    fi

    # Live-session reuse: prepare_workspace's surface reset would type /exit
    # into a busy live session and later end its thread. Mirror the cmux-helpers launch CLI invariant: a live local
    # tab is reused in place — no reset, no relaunch, and nothing typed
    # without the thread-provenance gate below.
    local reused_live="false"
    if [[ "$reused_p0" != "true" ]]; then
        local live_probe_ref=""
        live_probe_ref=$(workspace_ref_if_exists "$ws_name" 2>/dev/null || true)
        if [[ -n "$live_probe_ref" ]] \
            && ! workspace_is_remote "$live_probe_ref" \
            && workspace_has_live_amp "$live_probe_ref"; then
            if workspace_amp_is_orb "$live_probe_ref"; then
                log_warn "Live Amp session in $live_probe_ref for $ws_name is Orb-backed; preserving it and skipping launch"
                WORKSTREAMS_FAILED+=("$ws_name (orb-backed live session preserved)")
                return 0
            fi
            ws_ref="$live_probe_ref"
            reused_live="true"
            log_info "Live Amp session detected in $ws_ref for $ws_name; reusing in place (no reset, no /exit)"
        fi
    fi

    local source_key
    if [[ "$ws_context_dir" == "workstreams" ]]; then
        source_key="workstream:$ws_name"
    else
        source_key="routine:$ws_name"
    fi

    # Preamble: skip AGENTS.md session-start, capture source links as durable workstream notes,
    # and emit updates back to Megadesk.
    local preamble
    preamble="$(workstream_launch_preamble "$source_key")"
    local launch_prompt
    local launch_prompt_file

    if [[ -n "$ws_prompt" && "$ws_prompt" != "null" ]]; then
        launch_prompt="$preamble $ws_prompt"
    else
        local startup_prompt=""
        local config_path="$REPO_ROOT/$ws_context_dir/$ws_name/config.yaml"
        if [[ -f "$config_path" ]]; then
            startup_prompt=$(python3 - "$config_path" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as fh:
    payload = yaml.safe_load(fh) or {}

print(payload.get("startup_instruction", ""))
PY
)
        fi
        local default_prompt="Read $ws_context_dir/$ws_name/CONTEXT.md and resume from the last known state. Identify open questions, pending decisions, and next concrete deliverables."
        if [[ -n "$startup_prompt" ]]; then
            default_prompt="$startup_prompt"
        fi
        launch_prompt="$preamble $default_prompt"
    fi

    # Prepend the linked-P0 header so the seeded session knows this tab is
    # also the P0's execution/review space.
    if [[ "$p0_linked" == "true" ]] && declare -F p0_prompt_header_for_workstream >/dev/null 2>&1; then
        local p0_header
        p0_header=$(p0_prompt_header_for_workstream "$ws_name" || true)
        [[ -n "$p0_header" ]] && launch_prompt="$p0_header
$launch_prompt"
    fi

    # Written before the boot: a fresh Claude launch carries it as the
    # session's first turn (cmux-helpers.sh launch_seed_pointer), so nothing
    # is typed into the new composer. Reused live tabs still get the guarded
    # send below.
    launch_prompt_file=$(write_launch_prompt_file "$ws_name" "$launch_prompt")

    # Before the boot: a launch-argument seed turn starts as Claude boots, and
    # its first workspace update must already see the keep-open mark.
    if [[ "$source_key" == workstream:* ]] && declare -F mark_manually_opened_workstream >/dev/null 2>&1; then
        mark_manually_opened_workstream "$ws_name"
    fi

    # Prepare workspace (find/create, reset, launch amp, wait for ready)
    if [[ "$reused_p0" != "true" && "$reused_live" != "true" ]]; then
        local prep_rc=0
        ws_ref=$(PM_OS_LAUNCH_SEED_FILE="$launch_prompt_file" PM_OS_LAUNCH_SEED_HYDRATION="start-day-launch" \
            prepare_workspace "$ws_name" "$REPO_ROOT") || prep_rc=$?
        if (( prep_rc == 3 )) && [[ -n "$ws_ref" ]]; then
            # Backstop: the pre-probe above missed a live session (transient
            # cmux failure or race); the reset gate caught it WITHOUT typing
            # anything. Reuse it in place, same as the pre-probe path.
            reused_live="true"
            log_info "Reset gate preserved a live Amp session in $ws_ref for $ws_name; reusing in place"
        elif (( prep_rc != 0 )); then
            log_warn "Failed to launch: $ws_name (any existing session was preserved untouched)"
            WORKSTREAMS_FAILED+=("$ws_name")
            return 0  # soft failure, continue with others
        fi
    fi

    if [[ "$reused_p0" == "true" || "$reused_live" == "true" ]]; then
        # Thread-provenance gate (fail-closed invariant): the live
        # thread in this tab may be FOREIGN — the tab title proves nothing.
        # Only a thread that provably belongs to this workstream receives the
        # startup prompt; otherwise preserve the session silently and move on.
        if workspace_has_live_amp "$ws_ref" && ! workspace_thread_gate "$ws_ref" "$ws_name"; then
            log_warn "Live session in $ws_ref is on thread '${WSG_THREAD_ID:-unbound}' with ${WSG_PROVENANCE:-unknown} provenance for $ws_name; preserving it WITHOUT seeding (fail-closed)"
            set_workspace_status "$ws_ref" "status" "live-preserved" "" "#4A90D9"
            WORKSTREAMS_SKIPPED+=("$ws_name (unverified live session preserved, not seeded)")
            arrange_workspace_sidebar
            return 0
        fi
        # Pure startup seed (P0 lifecycle): mark hydration so the
        # review-lane plugin keeps this workspace in its current lane — a
        # start-day-opened P0 stays P0 instead of moving to To Review when the
        # machine-seeded turn finishes. Cleared when the send fails/defers.
        mark_startup_hydration "$ws_ref" "start-day-launch" || true
        # Reused live tab: guarded send only — never clobber a busy agent or a
        # typed draft. A deferred send is fine; the title/badge still carry
        # the signal and the next reconcile retries.
        if declare -F cig_safe_send >/dev/null 2>&1; then
            if ! cig_safe_send "$ws_ref" "Read and follow all instructions in $launch_prompt_file" "${START_DAY_SEED_WAIT_S:-90}" 3; then
                clear_startup_hydration "$ws_ref"
                log_warn "Workspace for $ws_name is busy; launch prompt deferred (session preserved)"
            fi
        else
            send_prompt_via_file "$ws_ref" "$launch_prompt_file" || clear_startup_hydration "$ws_ref"
        fi
        if declare -F consolidate_duplicate_workstream_tab >/dev/null 2>&1; then
            consolidate_duplicate_workstream_tab "$ws_name" "$ws_ref"
        fi
    else
        mark_startup_hydration "$ws_ref" "start-day-launch" || true
        send_prompt_via_file "$ws_ref" "$launch_prompt_file" || {
            # Soft failure, same convention as the prepare_workspace failure
            # above: one workstream's dropped seed must not abort the whole
            # start-day fan-out under set -e.
            clear_startup_hydration "$ws_ref"
            log_warn "Failed to seed launch prompt for $ws_name; continuing with other workstreams"
            WORKSTREAMS_FAILED+=("$ws_name (seed send failed)")
            return 0
        }
    fi

    set_workspace_status "$ws_ref" "status" "active" "" "#00CC00"
    WORKSTREAMS_OPENED+=("$ws_name")
    arrange_workspace_sidebar
    log_ok "Launched: $ws_name"
}

# ============================================================================
# Phase 6: Archive & Report
# ============================================================================

phase_6_archive_and_report() {
    maybe_run_people_maintenance

    # Archive today's plan
    if [[ -f "$PLAN_FILE" && "$FLAG_DRY_RUN" != "true" ]]; then
        cp "$PLAN_FILE" "$PLANS_ARCHIVE/$TODAY.json"
        log_info "Plan archived: $PLANS_ARCHIVE/$TODAY.json"
    fi

    # Update Megadesk status
    if [[ -n "$COS_REF" && "$FLAG_DRY_RUN" != "true" ]]; then
        set_workspace_status "$COS_REF" "status" "ready" "" "#00CC00"
        publish_megadesk_status
    fi

    phase_6_report
}

maybe_run_people_maintenance() {
    log_info "Phase 5b: People maintenance"

    if [[ "$FLAG_DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would run people maintenance once per day after 16:00 local time"
        return 0
    fi

    if [[ ! -f "$PEOPLE_MAINTENANCE_SCRIPT" ]]; then
        log_warn "People maintenance script missing: $PEOPLE_MAINTENANCE_SCRIPT"
        return 0
    fi

    local maintenance_output
    if maintenance_output=$(python3 "$PEOPLE_MAINTENANCE_SCRIPT" \
        --write \
        --summary-json "$PEOPLE_SUMMARY_FILE" \
        --if-due \
        --after-hour 16 \
        --state-file "$PEOPLE_MAINTENANCE_STATE_FILE" 2>&1); then
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            log_info "$line"
        done <<< "$maintenance_output"
    else
        log_warn "People maintenance failed"
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            log_warn "$line"
        done <<< "$maintenance_output"
    fi
}

phase_6_report() {
    echo ""
    echo "+======================================================+"
    echo "|            Start of Day -- Status Report              |"
    echo "+======================================================+"
    echo "|  Date:            $TODAY"
    echo "|  Megadesk:       ${COS_REF:-not started}"

    if [[ -f "$NOTE_PATH" ]]; then
        echo "|  Daily note:      $NOTE_PATH (exists)"
    else
        echo "|  Daily note:      $NOTE_PATH (pending Megadesk creation)"
    fi

    if [[ -f "$NOW_FILE" ]]; then
        local now_summary
        if now_summary=$(python3 - "$NOW_FILE" 2>/dev/null <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
current = payload.get("now") or {}
workspace = current.get("workspace", "idle")
title = current.get("title", "No recommended action")
print(f"{workspace}: {title}")
PY
); then
            echo "|  Now:             $now_summary"
        else
            echo "|  Now:             unavailable (invalid state)"
            log_warn "Could not read current recommendation from $NOW_FILE; continuing with degraded report"
        fi
    fi

    # Daily CEO review: it runs every day.
    # The chief-of-staff skill's Phase 6c runs it and stamps job-state key
    # ceo-review; this line only reports whether today's stamp exists.
    local ceo_review_state
    ceo_review_state=$(python3 - "$SYSTEM_DIR/job-state.json" "$TODAY" 2>/dev/null <<'PY'
import json, sys
path, today = sys.argv[1], sys.argv[2]
try:
    state = json.load(open(path))
except (OSError, ValueError):
    state = {}
last = state.get("ceo-review")
print(f"done {last}" if last == today else f"due (last {last or 'never'}; chief-of-staff Phase 6c writes outputs/reviews/{today}-ceo-review.md)")
PY
) || ceo_review_state="unknown"
    echo "|  CEO review:      $ceo_review_state"

    echo "|"

    local opened_count="${#WORKSTREAMS_OPENED[@]}"
    local skipped_count="${#WORKSTREAMS_SKIPPED[@]}"
    local failed_count="${#WORKSTREAMS_FAILED[@]}"

    if [[ "$opened_count" -gt 0 ]]; then
        echo "|  Opened:"
        for ws in ${WORKSTREAMS_OPENED[@]+"${WORKSTREAMS_OPENED[@]}"}; do
            echo "|    + $ws"
        done
    fi

    if [[ "$skipped_count" -gt 0 ]]; then
        echo "|  Skipped:"
        for ws in ${WORKSTREAMS_SKIPPED[@]+"${WORKSTREAMS_SKIPPED[@]}"}; do
            echo "|    - $ws"
        done
    fi

    if [[ "$failed_count" -gt 0 ]]; then
        echo "|  Failed:"
        for ws in ${WORKSTREAMS_FAILED[@]+"${WORKSTREAMS_FAILED[@]}"}; do
            echo "|    x $ws"
        done
    fi

    echo "|"
    echo "|  Workstreams available: ${#WORKSTREAMS_FOUND[@]}"
    echo "+======================================================+"
    echo ""
}

# ============================================================================
# Main
# ============================================================================

main() {
    parse_args "$@"
    setup_logging
    detect_first_run

    if [[ "$IS_FIRST_RUN" == "true" ]]; then
        print_welcome
    fi

    phase_1_discover

    update_amp

    # Deterministic daily note BEFORE any CoS interaction: phase 3 keys its
    # prompt on NOTE_PREEXISTED (set in phase 1), and CoS only enriches the
    # marker-bearing skeleton asynchronously.
    ensure_daily_note || log_warn "Daily-note skeleton creation failed; CoS Step 2 will create the note as fallback"

    phase_2_cmux_bootstrap
    phase_2b_cleanup_workspaces
    phase_3_chief_of_staff
    phase_4b_launch_routines
    phase_4_verify_state
    phase_5_launch_workstreams
    phase_6_archive_and_report

    log_ok "Start of day complete"
}

# Source guard: test harnesses (test-start-day.sh) source this file to unit-
# test individual functions with START_DAY_REPO_ROOT pointed at a fixture.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
