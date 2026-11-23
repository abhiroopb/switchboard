#!/usr/bin/env bash
# cmux-helpers.sh — Shared library for cmux workspace management with Amp
#
# Usage:
#   source system/cmux-helpers.sh                          # from start-day.sh
#   bash system/cmux-helpers.sh launch <workstream-name>   # standalone (from Chief of Staff)
#
# Architecture:
#   ┌──────────────┐     ┌────────────────┐     ┌───────────┐
#   │ start-day.sh │────>│ cmux-helpers.sh│────>│  cmux CLI │
#   └──────────────┘     │                │     └───────────┘
#   ┌──────────────┐     │  Functions:    │     ┌───────────┐
#   │ Amp (CoS)    │────>│  prepare_ws()  │────>│    Amp    │
#   └──────────────┘     │  launch()      │     └───────────┘
#                        └────────────────┘

set -euo pipefail

# --- Configuration (all overridable via env) ---
# Backend detection (cmux on macOS, the wmux shim on Windows/WSL). On macOS
# with cmux this sets SWITCHBOARD_MUX=cmux and changes nothing else.
# shellcheck source=scripts/lib/mux.sh
_cmux_helpers_dir="${BASH_SOURCE[0]//\\//}"
if [[ "$_cmux_helpers_dir" == */* ]]; then _cmux_helpers_dir="${_cmux_helpers_dir%/*}"; else _cmux_helpers_dir="."; fi
source "$_cmux_helpers_dir/scripts/lib/mux.sh" && mux_init
# shellcheck source=scripts/lib/comms-triage.sh
source "$_cmux_helpers_dir/scripts/lib/comms-triage.sh"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
CMUX_LOCK_DIR="${CMUX_LOCK_DIR:-${TMPDIR:-/tmp}/switchboard-cmux.lock}"
CMUX_WORKSPACE_LAUNCH_LOCK="${CMUX_WORKSPACE_LAUNCH_LOCK:-${TMPDIR:-/tmp}/switchboard-ws-launch.lock}"
# A cap below the live workspace count makes the status publisher skip every
# pass, so Mark Read and lane state go stale.
CMUX_STATUS_MAX_WORKSPACES="${CMUX_STATUS_MAX_WORKSPACES:-40}"
AMP_CMD="${AMP_CMD:-amp}"
CLAUDE_CMD="${CLAUDE_CMD:-claude}"
REPO_ROOT="${REPO_ROOT:-${SWITCHBOARD_ROOT:-$HOME/Development/switchboard}}"
# The owner's time zone for worker prompts and the CoS trial window: SWITCHBOARD_TZ (an
# IANA name, set in ~/.switchboard/env by the installer), else America/Los_Angeles.
SWITCHBOARD_TZ_NAME="${SWITCHBOARD_TZ:-America/Los_Angeles}"
if [[ "$SWITCHBOARD_TZ_NAME" == "America/Los_Angeles" ]]; then
    SWITCHBOARD_TZ_LABEL="Pacific Time (America/Los_Angeles)"
else
    SWITCHBOARD_TZ_LABEL="the $SWITCHBOARD_TZ_NAME time zone"
fi

# Which agent the launchers start: `claude` (Claude Code) or `amp`. One knob
# SWITCHBOARD_AGENT=claude|amp in the environment,
# else the top-level `agent:` key in system/config/router.yaml, else claude.
# system/scripts/switchboard_agent.py is the one resolver; dispatch.py agent_kind()
# imports it and this function asks it for the config level. Every Amp path
# below is unchanged when the answer is amp. The config answer is read once per
# config path when this library is sourced (callers use $(switchboard_agent), a
# subshell, so a lazy cache would never stick).
_switchboard_agent_config_path() { printf '%s' "${ROUTER_CONFIG:-$REPO_ROOT/system/config/router.yaml}"; }
_switchboard_agent_from_config() {  # <router.yaml path>; prints claude|amp
    local resolver
    resolver="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/switchboard_agent.py"
    if [[ -f "$resolver" ]] && command -v python3 >/dev/null 2>&1; then
        env -u SWITCHBOARD_AGENT python3 "$resolver" --config "$1" 2>/dev/null && return 0
    fi
    echo claude
}
_SWITCHBOARD_AGENT_CFG_PATH="$(_switchboard_agent_config_path)"
_SWITCHBOARD_AGENT_CFG_VALUE="$(_switchboard_agent_from_config "$_SWITCHBOARD_AGENT_CFG_PATH")"
switchboard_agent() {
    local agent
    agent=$(printf '%s' "${SWITCHBOARD_AGENT:-}" | tr '[:upper:]' '[:lower:]' | tr -d "[:space:]\"'")
    case "$agent" in claude|amp) echo "$agent"; return 0 ;; esac
    local cfg
    cfg="$(_switchboard_agent_config_path)"
    if [[ "$cfg" == "$_SWITCHBOARD_AGENT_CFG_PATH" && -n "$_SWITCHBOARD_AGENT_CFG_VALUE" ]]; then
        echo "$_SWITCHBOARD_AGENT_CFG_VALUE"
    else
        _switchboard_agent_from_config "$cfg"
    fi
}

# The bare interactive command for the configured agent at an Amp mode, for
# surfaces that type their own launch line (start-day.sh CoS): `amp -m <mode>`
# or `claude --dangerously-skip-permissions --model <m> --effort <e>`.
# Settings every switchboard Claude tab starts with (system/config/claude-tab-settings.json):
# prompt suggestions off, because a suggested next prompt sits in the composer
# and the inject guard cannot tell it from a typed draft.
claude_tab_settings() {
    printf '%s' "$REPO_ROOT/system/config/claude-tab-settings.json"
}

# Remote Control for CoS only, and the global Claude Code setting stays unchanged.
# Every scripted CoS launch gets `--remote-control CoS`, so the Claude app's
# Code tab lists the session as "CoS". A plain `claude --resume` of a session that had
# Remote Control turns it back on by itself, so a
# wmux restore keeps CoS reachable; the resume builder still passes the flag for a CoS that
# never had it. Workers, routine tabs, and every other surface get nothing.
# SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH=1 rides along so cos_autobind.py skips its /rc reminder.
# Kill switch: SWITCHBOARD_COS_REMOTE_CONTROL=off (local/machines/<MACHINE>.env).
COS_REMOTE_CONTROL_NAME="${COS_REMOTE_CONTROL_NAME:-CoS}"
is_cos_surface() {  # <surface name>
    case "${1:-}" in switchboard|routine:switchboard) return 0 ;; esac
    return 1
}
cos_remote_control_enabled() {  # <surface name>
    is_cos_surface "${1:-}" || return 1
    [[ "$(switchboard_agent)" == "claude" ]] || return 1
    if ! declare -F switchboard_machine_value >/dev/null && [[ -f "$REPO_ROOT/system/scripts/lib/machine-role.sh" ]]; then
        # shellcheck source=scripts/lib/machine-role.sh
        source "$REPO_ROOT/system/scripts/lib/machine-role.sh"
    fi
    local switch="${SWITCHBOARD_COS_REMOTE_CONTROL:-}"
    declare -F switchboard_machine_value >/dev/null && switch="$(switchboard_machine_value SWITCHBOARD_COS_REMOTE_CONTROL)"
    case "$(printf '%s' "${switch:-on}" | tr '[:upper:]' '[:lower:]')" in
        off|0|false|no) return 1 ;;
    esac
    return 0
}
cos_remote_control_args() {  # <surface name>: " --remote-control CoS" or nothing
    cos_remote_control_enabled "${1:-}" || return 0
    printf ' --remote-control %q' "$COS_REMOTE_CONTROL_NAME"
}
cos_remote_control_env() {  # <surface name>: "SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH=1 " or nothing
    cos_remote_control_enabled "${1:-}" || return 0
    printf 'SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH=1 '
}

agent_interactive_command() {  # <agent mode> [surface name]
    local mode="${1:-ultra}"
    local surface="${2:-}"
    if [[ "$(switchboard_agent)" == "claude" ]]; then
        local model effort
        read -r model effort <<<"$(claude_mode_flags "$mode")"
        printf '%s%s --dangerously-skip-permissions --model %s --effort %s --settings %q%s' \
            "$(cos_remote_control_env "$surface")" "$CLAUDE_CMD" "$model" "$effort" "$(claude_tab_settings)" \
            "$(cos_remote_control_args "$surface")"
    else
        printf '%s -m %s' "$AMP_CMD" "$(printf '%q' "$mode")"
    fi
}

# Amp agent mode -> Claude Code `--model <m> --effort <e>`. One table, printed
# as "<model> <effort>"; unknown modes take the ultra row. Keep in step with
# CLAUDE_MODE_FLAGS in system/scripts/dispatch.py.
claude_mode_flags() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        medium) echo "sonnet medium" ;;
        low)    echo "sonnet low" ;;
        *)      echo "opus high" ;;      # ultra, high, and anything unrecognised
    esac
}

# Claude Code's ready screen in a cmux tab: the
# input box footer reads `⏵⏵ auto mode on (shift+tab to cycle)` (or `bypass
# permissions on` under --dangerously-skip-permissions) or `? for shortcuts`.
# Its startup dialogs end in "Enter to confirm · Esc to cancel" and never
# count as ready. Same regex as dispatch.py CLAUDE_SCREEN_MARKER.
CLAUDE_READY_MARKER='(shift\+tab to cycle|\? for shortcuts)'
AMP_READY_MARKER='(╭──|╰──|Welcome to Amp|Ctrl\+O for help)'

# Claude Code session ids are UUIDs (the cmux session store's
# resumeBinding.checkpointId, what `claude --resume` takes). Amp thread ids
# are `T-...`. Both flow through the same `thread_id` fields and bindings.
CLAUDE_SESSION_ID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
AMP_THREAD_ID_RE='^T-[0-9a-fA-F-]+$'

# The id regex a reused tab's prior session must match for the configured agent.
agent_session_id_re() {
    if [[ "$(switchboard_agent)" == "claude" ]]; then printf '%s' "$CLAUDE_SESSION_ID_RE"; else printf '%s' "$AMP_THREAD_ID_RE"; fi
}

# "Amp" or "Claude" for log lines.
agent_display_name() {
    if [[ "$(switchboard_agent)" == "claude" ]]; then echo Claude; else echo Amp; fi
}

# The screen regex that proves the configured agent's TUI is up.
agent_ready_marker() {
    if [[ "$(switchboard_agent)" == "claude" ]]; then printf '%s' "$CLAUDE_READY_MARKER"; else printf '%s' "$AMP_READY_MARKER"; fi
}

# Shared inject guard so any caller (route-to-workspace seeding, wrap/close,
# etc.) can safely send a prompt into a live Amp TUI without clobbering what
# the user is typing there. Exposes cig_safe / cig_safe_send / cig_force_send.
if [[ -f "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh" ]]; then
    CMUX="$CMUX_BIN"  # let the guard reuse our resolved cmux binary
    # shellcheck source=scripts/lib/cmux-inject-guard.sh
    source "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh"
fi

AMP_READY_TIMEOUT="${AMP_READY_TIMEOUT:-60}"
AMP_READY_POLL="${AMP_READY_POLL:-2}"

# Launch verification for owner tabs: dispatch.py already checks
# worker tabs (verify-amp), and owner tabs call the same helper through that
# subcommand, never a second copy. The helper is located next to this library,
# not under REPO_ROOT, so a fixture REPO_ROOT (the test harnesses) still runs
# the real code. SWITCHBOARD_DISPATCH_PY is a test seam.
_CMUX_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWITCHBOARD_DISPATCH_PY="${SWITCHBOARD_DISPATCH_PY:-$_CMUX_HELPERS_DIR/scripts/dispatch.py}"

WORKSTREAM_LINK_SUMMARY_RULE="If the current workspace is a workstream and the user drops a link to a source artifact such as a Google Doc, Slack thread or channel, Markdown file, video, Figma, or web page, treat it as durable workstream input. For normal source artifacts, create or update a dated markdown summary note in the workstream folder instead of leaving the analysis only in chat or /tmp. For meeting artifacts, including Gemini meeting emails and meeting-note docs, save or update the canonical note under projects/*/meetings/ (or projects/_inbox/meetings/ when routing is unclear) and then add a short summary plus link in the workstream CONTEXT.md instead of writing a second meeting note into the workstream folder."
WORKSTREAM_MEETING_NOTES_RULE="Before making recommendations, drafting documents, or replying on behalf of a workstream, check recent meeting context relevant to the workspace. Start with the Recent Meetings section in the workstream CONTEXT.md and any linked projects/*/meetings/ notes. If the linked note cites a Gemini Notes source, or if the owner mentions meeting notes, what was said, what changed, or asks for meeting context, open the linked Gemini note automatically before answering unless they explicitly asks to stay with the summarized note only."
WORKSTREAM_REVIEW_SURFACE_RULE="Review-surface contract: any deliverable the owner must review is a Markdown note inside the switchboard Obsidian vault (the repo root), written with the drafting-docs Markdown workflow: outputs/reviews/<YYYY-MM-DD>-<slug>.md for cross-cutting reviews or the owning workstream folder, with frontmatter and native relative links. Pass the note as --review-artifact <url> on emit-workspace-update.py, giving either the vault-relative path or its obsidian://open?vault=switchboard&file=<path> URI, and include the same pointer in the report-to-cos.sh summary; both boundaries reject a review deliverable whose note does not exist inside the vault and both store the obsidian:// link as the review pointer. Create a Google Doc (drafting-docs template workflow, never a blank Doc, targeted edits only on an existing Doc) or a hosted page only when the owner explicitly asks for that surface; existing collaborative Google Docs stay unchanged. Code diffs, logs, tests, raw sources, and internal-state Markdown are not review deliverables and are not blocked."
WORKSTREAM_MODEL_ROUTING_RULE="Agent-mode routing: ultra is the default for every switchboard thread. high is used only when the owner explicitly chooses it for a thread. If this session is not already ultra and the owner did not explicitly choose high for it, recommend once that the owner press Ctrl+S and select ultra before the next turn. Do not wait for the owner to name the mode. You cannot change the active mode yourself. For one specific unresolved high-impact judgment, use Oracle in addition to the current mode."
WORKSTREAM_LEDGER_FENCE_RULE="Live-worker fence: never settle another task's ledger row while its worker thread is live. running-task-state.py update --status cancelled|done|failed|blocked on an active row whose bound thread is live in cmux returns _live_worker=true and writes nothing; a row that shows routing for hours may have launched seconds ago, so check the live thread first and message that worker to stop or steer it. --replace-report is the only sanctioned override, for a row whose worker already landed its work or when the owner explicitly overrules; never use it on a row that merely looks stuck."

# --- Logging ---
_helpers_log() {
    local level="$1"; shift
    local ts
    ts=$(date '+%H:%M:%S')
    echo "[$ts] [$level] $*" >&2
}

log_info()  { _helpers_log "INFO"  "$@"; }
log_warn()  { _helpers_log "WARN"  "$@"; }
log_error() { _helpers_log "ERROR" "$@"; }
log_ok()    { _helpers_log " OK  " "$@"; }

workstream_launch_preamble() {
    local source_key="$1"
    local presentation_rule

    if [[ "$source_key" == "routine:launchpad" ]]; then
        cat <<'EOF'
IMPORTANT: Skip all AGENTS.md session-start steps. You are the quiet Launchpad dispatch surface. Set DISPATCH_ORIGIN=launchpad. Read system/prompts/dispatch-core.md and routines/launchpad/CONTEXT.md. Pull current state only when a prompt arrives. Never publish unsolicited reports or run delivery inline.
EOF
        return 0
    fi

    if [[ "$source_key" == "routine:comms-triage" ]]; then
        presentation_rule="For substantive comms-triage output, do not prepend the generic workspace summary table. Render the canonical compact per-item view from routines/comms-triage/CONTEXT.md directly: five emoji-labeled rows (From, Message, Real ask, Recommendation, Draft response), followed by one Supporting details box."
    else
        presentation_rule="In every substantive user-facing response, start with a compact markdown summary table with emoji status markers so the update is easy to scan. Use these columns in this order: Signal | What's going on | Next action | Owner | ETA. Keep it tight, usually 1-4 rows max, and make the next action concrete. Use status markers like 🟥 blocked, 🟧 needs you, 🟨 in motion, 🟦 waiting, 🟩 stable. After that summary table, if the workspace CONTEXT.md or config defines a concrete presentation structure, render the workspace-specific format in full beneath the summary table. If no workspace-specific structure exists, the summary table can serve as the main format. For all later substantive user-facing updates, stay table-first and keep emoji visual anchors."
    fi

    cat <<EOF
IMPORTANT: Skip all AGENTS.md session-start steps (todo, kb-distill, start-of-day, daemon, skill loading). You are a focused workspace launched by start-day.sh. Go straight to the task below. $presentation_rule $WORKSTREAM_MODEL_ROUTING_RULE $WORKSTREAM_LEDGER_FENCE_RULE Timezone: all user-facing dates and times are ${SWITCHBOARD_TZ_LABEL}. The session-context "Today's date" is UTC — never treat it as local; run \`date\` before any today/tomorrow/day-of-week claim about meetings, deadlines, or events, and label UTC timestamps explicitly when quoting logs. Before you pause, finish, or enter a waiting state, emit a structured update with python3 system/scripts/emit-workspace-update.py --source $source_key --status <active|to review|waiting|blocked|paused|done> --summary "<what changed>" --recommended-next-action "<next action>". Use active only while the agent is running. Use "to review" ONLY when there is a concrete artifact, decision, or result that genuinely requires the owner's review — never for an ordinary completed startup/verification pass (use done or waiting for those; startup-seeded "to review" is auto-demoted to done unless you add --review-artifact). If this startup-seeded turn genuinely produced an artifact awaiting the owner's review (e.g. drafts pending approval), emit --status "to review" --review-artifact — do NOT escalate to "needs review"/"needs action", those mean urgent and paint the red lane. If you completed or deferred todo items linked to this workstream, include either --todo-update "<todo-id>|done" or --linked-todos <waiting|scheduled|done> with dates when needed. $WORKSTREAM_REVIEW_SURFACE_RULE $WORKSTREAM_MEETING_NOTES_RULE $WORKSTREAM_LINK_SUMMARY_RULE
If the owner explicitly says this workstream or its work is done, complete, finished, resolved, closed, paused, or deferred, treat that as a full lifecycle action, not just a status edit. For done/complete/finished/resolved/closed, mark linked to-dos done and emit the done update with --linked-todos done --close-workspace --clear-manual-open. For pause/defer, emit the paused or waiting update with --close-workspace --clear-manual-open and update linked to-dos accordingly. In every explicit lifecycle case, verify that the owning cmux workspace actually disappears; a state update alone is not completion. Never use --clear-manual-open unless the owner explicitly requested that lifecycle action in this conversation turn. Do not infer it from your own decision to wait, pause, stop, or finish an initial review.
If the owner asks to add or promote work from this workstream to P0, treat that as an immediate workspace-routing action: after updating the to-do, run \`bash system/scripts/ensure-p0-workspaces.sh\` in the same turn. The reconciler must remove the standalone workstream from its normal lane and consolidate it into the canonical \`<emoji> P0 · <workstream>\` workspace in the P0 section. Do not leave duplicate normal and P0 tabs open, and do not wait for the next start-of-day run.
Before reading CONTEXT.md, if this is a workstream session and \`workstreams/<this-workstream>/.signals/recent.md\` exists, read it first. It contains a recent cross-surface activity digest (Slack, Gmail, Linear, GitHub, Jira, Notion, Drive, Tracker, Calendar, Doc comments, Slack reactions) filtered to this workstream by channel and keyword. Use it to surface what changed while the workspace was idle, not as a substitute for CONTEXT.md.
EOF
}

mark_manually_opened_workstream() {
    local slug="$1"

    python3 - "$REPO_ROOT" "$slug" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

repo_root = Path(sys.argv[1])
slug = sys.argv[2]
path = repo_root / "system" / "state" / "sources" / "manual-open-workstreams.json"
path.parent.mkdir(parents=True, exist_ok=True)

try:
    payload = json.loads(path.read_text()) if path.exists() else {}
except json.JSONDecodeError:
    payload = {}

entries = payload.get("workstreams")
if not isinstance(entries, dict):
    entries = {}

now = datetime.now(timezone.utc).replace(microsecond=0)

def parse_iso(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

entries = {
    key: value
    for key, value in entries.items()
    if isinstance(value, dict)
    and (parse_iso(value.get("expires_at")) or now) >= now
}

entries[slug] = {
    "opened_at": now.isoformat().replace("+00:00", "Z"),
    "expires_at": (now + timedelta(hours=16)).isoformat().replace("+00:00", "Z"),
    "reason": "explicit-launch",
}

path.write_text(json.dumps({
    "updated_at": now.isoformat().replace("+00:00", "Z"),
    "workstreams": entries,
}, indent=2) + "\n")
PY
}

# --- Core cmux wrapper ---
cmux_cmd() {
    local -a command_args=("$@")
    local command="${command_args[0]:-}"
    local focus_value=""
    local index

    # switchboard automation is non-focusing by default. New workspaces always start
    # in the background unless a caller passes an explicit focus value. Commands
    # that select a workspace or focus the app require a marker that only
    # user-initiated entry points may set.
    if [[ "$command" == "new-workspace" ]]; then
        for ((index = 1; index < ${#command_args[@]}; index++)); do
            case "${command_args[$index]}" in
                --focus)
                    focus_value="${command_args[$((index + 1))]:-}"
                    ;;
                --focus=*)
                    focus_value="${command_args[$index]#--focus=}"
                    ;;
            esac
        done
        if [[ -z "$focus_value" ]]; then
            command_args+=(--focus false)
        elif [[ "$focus_value" == "true" && "${SWITCHBOARD_EXPLICIT_USER_FOCUS:-0}" != "1" ]]; then
            log_error "Blocked background cmux workspace focus. Use an explicit user focus action."
            return 2
        fi
    fi

    if { [[ "$command" == "select-workspace" || "$command" == "set-app-focus" ]] \
        || [[ "$command" == "workspace" && "${command_args[1]:-}" == "select" ]] \
        || [[ "$command" == "rpc" && "${command_args[1]:-}" =~ ^(workspace\.select|app\.setFocus)$ ]]; } \
        && [[ "${SWITCHBOARD_EXPLICIT_USER_FOCUS:-0}" != "1" ]]; then
        log_error "Blocked background cmux focus change. Use an explicit user focus action."
        return 2
    fi

    local wait_count=0
    local owner_file="$CMUX_LOCK_DIR/pid"
    local lock_owner=""

    # Serialize switchboard automation so overlapping sidebar and workspace writes
    # do not hammer cmux's socket at the same time.
    while ! mkdir "$CMUX_LOCK_DIR" 2>/dev/null; do
        if [[ -f "$owner_file" ]]; then
            lock_owner=$(cat "$owner_file" 2>/dev/null || true)
            if [[ -n "$lock_owner" ]] && ! kill -0 "$lock_owner" 2>/dev/null; then
                rm -f "$owner_file"
                rmdir "$CMUX_LOCK_DIR" 2>/dev/null || true
                continue
            fi
        else
            # Orphaned lock: dir exists but no pid file was ever written
            # (holder died between mkdir and printf, or a crash/reboot left
            # an empty dir). Without this branch the stale-owner cleanup
            # above never fires and every cmux_cmd deadlocks until timeout.
            # A freshly created lock has
            # no pid file for only a few ms, so a 60s age gate is safe.
            local lock_age
            lock_age=$(( $(date +%s) - $(mux_mtime "$CMUX_LOCK_DIR" || date +%s) ))
            if (( lock_age > 60 )); then
                rmdir "$CMUX_LOCK_DIR" 2>/dev/null || true
                continue
            fi
        fi

        sleep 0.05
        (( wait_count += 1 ))
        if (( wait_count >= 600 )); then
            log_error "Timed out waiting for cmux lock: $CMUX_LOCK_DIR"
            return 1
        fi
    done

    printf '%s\n' "${BASHPID:-$$}" > "$owner_file"

    # Capture the REAL exit status of the cmux call. The previous form
    # (`if ! "$CMUX_BIN" "$@"; then status=$?`) captured the status of the `!`
    # negation, which is 0 whenever the command FAILED — so every transient
    # cmux/socket/lock error looked like success to callers. get_workspace_surface
    # then could not tell "tree command errored (contention)" from "no surface
    # exists", and burned its whole retry budget on transient errors. Capture
    # the status directly instead.
    local status=0
    "$CMUX_BIN" "${command_args[@]}"
    status=$?

    rm -f "$owner_file"
    rmdir "$CMUX_LOCK_DIR" 2>/dev/null || true
    return "$status"
}

# --- Workspace launch lock (portable) ---
# Serializes the lookup->create/rename window so concurrent launchers cannot
# create duplicate workspaces. Use Python's flock on a descriptor inherited
# from this shell: the kernel releases it when the last holder exits. No
# PID-publication window or age-based deletion is needed. Never unlink this
# lock file, which would let another writer lock a different inode.
WS_LAUNCH_LOCK_DIR="${CMUX_WORKSPACE_LAUNCH_LOCK}.d"

# shellcheck source=scripts/lib/dir-lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib/dir-lock.sh"

acquire_ws_launch_lock() {
    # Old loaded copies still use mkdir. Do not guess whether an ownerless
    # legacy lock is abandoned; migrate only after old launchers have exited.
    if [[ -e "$WS_LAUNCH_LOCK_DIR" ]]; then
        log_error "Legacy workspace launch lock needs inspected cleanup: $WS_LAUNCH_LOCK_DIR"
        return 1
    fi
    if switchboard_is_windows_shell; then
        switchboard_dir_lock_acquire "$CMUX_WORKSPACE_LAUNCH_LOCK.win" "${WS_LAUNCH_LOCK_TIMEOUT:-60}" && return 0
        log_error "Could not acquire workspace launch lock: $CMUX_WORKSPACE_LAUNCH_LOCK.win"
        return 1
    fi
    exec 9>>"$CMUX_WORKSPACE_LAUNCH_LOCK" || return 1
    if ! python3 - "${WS_LAUNCH_LOCK_TIMEOUT:-60}" <<'PY'
import fcntl, sys, time
deadline = time.monotonic() + float(sys.argv[1])
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.05)
PY
    then
        exec 9>&-
        log_error "Could not acquire workspace launch lock: $CMUX_WORKSPACE_LAUNCH_LOCK"
        return 1
    fi
}

release_ws_launch_lock() {
    if switchboard_is_windows_shell; then
        switchboard_dir_lock_release
        return 0
    fi
    exec 9>&-
}

workspace_count_from_listing() {
    local listing="$1"
    printf '%s\n' "$listing" | grep -c 'workspace:' || true
}

warn_dynamic_ui_updates_disabled() {
    local reason="$1"
    local marker_dir="$REPO_ROOT/.state"
    local marker_file="$marker_dir/cmux-dynamic-updates-warning.timestamp"
    local now last_warn=0

    log_warn "$reason"
    now=$(date +%s)
    if [[ -f "$marker_file" ]]; then
        last_warn=$(cat "$marker_file" 2>/dev/null || echo 0)
    fi
    if (( now - last_warn >= 3600 )); then
        mkdir -p "$marker_dir"
        if cmux_cmd notify --title "switchboard cmux warning" --body "$reason" >/dev/null 2>&1; then
            printf '%s\n' "$now" > "$marker_file"
        fi
    fi
}

cmux_dynamic_ui_updates_allowed() {
    local listing
    local workspace_count

    if ! listing=$(cmux_cmd list-workspaces 2>/dev/null); then
        warn_dynamic_ui_updates_disabled "cmux dynamic updates disabled: workspace count unknown (list-workspaces failed)"
        return 1
    fi
    workspace_count=$(workspace_count_from_listing "$listing")
    if (( workspace_count > CMUX_STATUS_MAX_WORKSPACES )); then
        warn_dynamic_ui_updates_disabled "cmux dynamic updates disabled: $workspace_count workspaces > limit $CMUX_STATUS_MAX_WORKSPACES (set CMUX_STATUS_MAX_WORKSPACES)"
        return 1
    fi
    return 0
}

cmux_dynamic_ui_updates_allowed_from_listing() {
    local listing="$1"
    local workspace_count

    workspace_count=$(workspace_count_from_listing "$listing")
    if (( workspace_count > CMUX_STATUS_MAX_WORKSPACES )); then
        warn_dynamic_ui_updates_disabled "cmux dynamic updates disabled: $workspace_count workspaces > limit $CMUX_STATUS_MAX_WORKSPACES (set CMUX_STATUS_MAX_WORKSPACES)"
        return 1
    fi
    return 0
}

read_workspace_config_emoji() {
    local config_path="$1"

    [[ -f "$config_path" ]] || return 1

    python3 - "$config_path" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
for line in text.splitlines():
    stripped = line.strip()
    if not stripped.startswith("emoji:"):
        continue
    value = stripped.split(":", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if value:
        print(value)
    break
PY
}

# Core surfaces that ALWAYS launch in ultra:
# CoS (routine dir `switchboard`), LaunchPad, Comms Triage, and every Router.
# Each surface's config.yaml / router.yaml stays the documented canonical
# value (ultra). This floor is defense in depth: no env override, config
# drift, or default fallback may recreate one of these surfaces at a lower
# mode. Matching is by routine directory name, case-insensitive.
SWITCHBOARD_ULTRA_SURFACES="switchboard launchpad comms-triage router"

is_ultra_surface() {  # <workstream-or-routine name>
    local needle surface
    needle=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    for surface in $SWITCHBOARD_ULTRA_SURFACES; do
        [[ "$needle" == "$surface" ]] && return 0
    done
    return 1
}

# CoS mode trial: the CoS thread
# alone launches at a lower mode for a dated window; LaunchPad, Comms Triage,
# Routers and every worker stay on the ultra floor. The one knob is
# system/config/router.yaml `cos_mode_trial:` (mode, start, end; calendar
# dates in SWITCHBOARD_TZ, inclusive). Outside the window, or with
# SWITCHBOARD_COS_MODE_TRIAL_DISABLED=1, CoS floors to ultra as before. Prints the
# trial mode when active for the given name, nothing otherwise.
cos_mode_trial_mode() {  # <workstream-or-routine name>
    local needle
    needle=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    [[ "$needle" == "switchboard" ]] || return 1
    [[ "${SWITCHBOARD_COS_MODE_TRIAL_DISABLED:-0}" == "1" ]] && return 1
    local cfg="${ROUTER_CONFIG:-$REPO_ROOT/system/config/router.yaml}"
    [[ -f "$cfg" ]] || return 1
    SWITCHBOARD_TZ="$SWITCHBOARD_TZ_NAME" python3 - "$cfg" <<'PY' 2>/dev/null
import datetime as dt, os, sys, zoneinfo
try:
    import yaml
    trial = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("cos_mode_trial") or {}
except Exception:
    raise SystemExit(1)
mode = str(trial.get("mode") or "").strip().lower()
if mode not in {"high", "ultra"}:
    raise SystemExit(1)
try:
    zone = zoneinfo.ZoneInfo(os.environ.get("SWITCHBOARD_TZ") or "America/Los_Angeles")
except Exception:
    zone = zoneinfo.ZoneInfo("America/Los_Angeles")
today = dt.datetime.now(zone).date()
def as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v).strip())
try:
    start = as_date(trial.get("start"))
    end = as_date(trial.get("end") or trial.get("start"))
except Exception:
    raise SystemExit(1)
if not (start <= today <= end):
    raise SystemExit(1)
print(mode)
PY
}

# Decide which Amp agent mode (-m) a workspace launches with.
# Precedence:
#   0. Ultra floor for core surfaces (see SWITCHBOARD_ULTRA_SURFACES) — wins over
#      everything below. One exception: an active CoS mode trial window in
#      router.yaml `cos_mode_trial:` (see cos_mode_trial_mode) sets the mode
#      for switchboard alone.
#   1. SWITCHBOARD_AGENT_MODE env var — CoS judgment at launch time
#      (e.g. SWITCHBOARD_AGENT_MODE=ultra for explicitly hard or strategic work)
#   2. `agent_mode:` field in the workstream/routine config.yaml
#   3. ultra
# Policy: ultra
# is the switchboard default for every new thread. high is explicit opt-in only —
# it is honored when the owner (or CoS on their instruction) sets SWITCHBOARD_AGENT_MODE=high
# or agent_mode: high in a config. Core surfaces stay floored at ultra.
# medium and low must never reach Amp: legacy "medium"/"low"
# inputs coerce to ultra (compatibility); empty or invalid values default to
# ultra.
# Valid outputs: high | ultra.
resolve_agent_mode() {
    local name="$1"
    local mode="${SWITCHBOARD_AGENT_MODE:-}"
    local cfg

    if [[ -z "$mode" ]]; then
        for cfg in "$REPO_ROOT/workstreams/$name/config.yaml" "$REPO_ROOT/routines/$name/config.yaml"; do
            [[ -f "$cfg" ]] || continue
            mode=$(python3 - "$cfg" <<'PY' 2>/dev/null || true
import sys, yaml
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f) or {}
v = d.get("agent_mode", "")
if v:
    print(v)
PY
)
            [[ -n "$mode" ]] && break
        done
    fi

    mode=$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    case "$mode" in
        high|ultra) ;;
        low|medium)
            # Compatibility: legacy low/medium configs coerce to the ultra
            # default. switchboard launchers never pass medium or low to Amp.
            _helpers_log "WARN" "agent_mode '$mode' is retired for switchboard launches; coercing to 'ultra' (name=$name)"
            mode="ultra"
            ;;
        *) mode="ultra" ;;
    esac
    local trial_mode
    if trial_mode="$(cos_mode_trial_mode "$name")" && [[ -n "$trial_mode" ]]; then
        _helpers_log "INFO" "CoS mode trial active (router.yaml cos_mode_trial): '$name' launches $trial_mode"
        mode="$trial_mode"
    elif [[ "$mode" != "ultra" ]] && is_ultra_surface "$name"; then
        _helpers_log "WARN" "core surface '$name' always launches ultra; overriding resolved mode '$mode'"
        mode="ultra"
    fi
    echo "$mode"
}

workspace_emoji() {
    local name="$1"
    local base_name
    local cfg_emoji

    base_name=$(workspace_base_name "$name")

    # 1. Authoritative source: workstreams/<name>/config.yaml `emoji:` field.
    cfg_emoji=$(read_workspace_config_emoji "$REPO_ROOT/workstreams/$base_name/config.yaml" 2>/dev/null || true)
    if [[ -n "$cfg_emoji" ]]; then
        echo "$cfg_emoji"
        return 0
    fi
    cfg_emoji=$(read_workspace_config_emoji "$REPO_ROOT/routines/$base_name/config.yaml" 2>/dev/null || true)
    if [[ -n "$cfg_emoji" ]]; then
        echo "$cfg_emoji"
        return 0
    fi

    # 2. Fallback to the hardcoded map (kept for routines + back-compat).
    case "$base_name" in
        CoS|"Chief of Staff"|switchboard) echo "🧭" ;;
        launchpad) echo "🚀" ;;
        todo) echo "✅" ;;
        comms-triage|comms-triage-claude) echo "📬" ;;
        meetings) echo "📅" ;;
        scheduled-jobs) echo "⏰" ;;
        meetings-ingest|meeting-ingest) echo "📝" ;;
        *) echo "📁" ;;
    esac
}

workspace_base_name() {
    local name="$1"

    while [[ "$name" == *-cont ]]; do
        name="${name%-cont}"
    done

    echo "$name"
}

workspace_continuation_count() {
    local name="$1"
    local count=0

    while [[ "$name" == *-cont ]]; do
        name="${name%-cont}"
        count=$((count + 1))
    done

    echo "$count"
}

read_workspace_config_name() {
    # First line whose stripped form starts with "name:", value trimmed and
    # unquoted. Pure bash on purpose: this runs several times per
    # workspace-name lookup, and a python3 spawn costs ~0.1s each,
    # which put publish-workspace-status.sh over its 300s background budget
    # when it resolved ~120 status rows per pass.
    local config_path="$1"
    local line value first last

    [[ -f "$config_path" ]] || return 1

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ "$line" == name:* ]] || continue
        value="${line#name:}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        if (( ${#value} >= 2 )); then
            first="${value:0:1}"
            last="${value:${#value}-1:1}"
            if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
                value="${value:1:${#value}-2}"
            fi
        fi
        printf '%s\n' "$value"
        return 0
    done < "$config_path"
    return 0
}

titleize_workspace_name() {
    python3 - "$1" <<'PY'
import sys

parts = sys.argv[1].replace('_', '-').split('-')
special = {
    'ai': 'AI',
    'ios': 'iOS',
    'pr': 'PR',
}

words = []
for part in parts:
    lower = part.lower()
    words.append(special.get(lower, part.capitalize()))

print(' '.join(words))
PY
}

canonical_workspace_name_for_source() {
    local source_type="$1"
    local name="$2"
    local config_name=""

    case "$source_type:$name" in
        switchboard:*|*:CoS|*:"Chief of Staff"|*:switchboard) echo "CoS" ;;
        *:todo) echo "Todo" ;;
        *:comms-triage|*:comms-triage-claude) echo "Comms Triage" ;;
        *:scheduled-jobs) echo "Scheduled Jobs" ;;
        *)
            if [[ "$source_type" == "workstream" || "$source_type" == "generic" ]]; then
                config_name=$(read_workspace_config_name "$REPO_ROOT/workstreams/$name/config.yaml" 2>/dev/null || true)
                if [[ -n "$config_name" ]]; then
                    echo "$config_name"
                    return 0
                fi
            fi

            if [[ "$source_type" == "routine" || "$source_type" == "generic" ]]; then
                config_name=$(read_workspace_config_name "$REPO_ROOT/routines/$name/config.yaml" 2>/dev/null || true)
                if [[ -n "$config_name" ]]; then
                    echo "$config_name"
                    return 0
                fi
            fi

            titleize_workspace_name "$name"
            ;;
    esac
}

canonical_workspace_name() {
    canonical_workspace_name_for_source "generic" "$1"
}

format_workspace_name_for_source() {
    local source_type="$1"
    local name="$2"
    local base_name
    local display_name
    local cont_count
    local emoji
    base_name=$(workspace_base_name "$name")
    display_name=$(canonical_workspace_name_for_source "$source_type" "$base_name")
    cont_count=$(workspace_continuation_count "$name")
    emoji=$(workspace_emoji "$base_name")

    if (( cont_count == 1 )); then
        echo "$emoji $display_name (cont)"
    elif (( cont_count > 1 )); then
        echo "$emoji $display_name (cont $cont_count)"
    else
        echo "$emoji $display_name"
    fi
}

format_workspace_name() {
    format_workspace_name_for_source "generic" "$1"
}

format_legacy_workspace_name() {
    local name="$1"
    local emoji

    emoji=$(workspace_emoji "$name")
    echo "$emoji $name"
}

# --- P0-linked workstream canonicalization ---
#
# Invariant: when an active P0 to-do links a workstream,
# that workstream's canonical local workspace is the P0 tab, titled
# "<workstream emoji> P0 · <display name>" (the "P0 ·" fragment keeps it in
# the sidebar's P0 lane; the leading emoji renders as the row icon, so each
# P0 tab shows a distinct emoji instead of a uniform 🔥),
# seeded with the workstream's CONTEXT/config startup
# instructions. No separate normal workstream tab should be launched while the
# P0 is active. Non-P0-linked workstreams keep the normal name. All launch and
# lookup paths converge on this via workstream_canonical_display_name /
# find_or_create_workspace, so consolidation is a RENAME of the live tab
# (never an interruption of a running session).

SWITCHBOARD_TODO_JSON="${SWITCHBOARD_TODO_JSON:-$REPO_ROOT/system/state/todo.json}"

# Print "id<US>title<US>workstream<US>detail" lines (US = \x1f) for surfaced
# P0 to-dos (active open|actionable, or review-needed waiting whose review_on
# has arrived; never snoozed-into-the-future) whose `workstream` field equals
# the slug. Classification lives in scripts/lib/p0-todos.py — the single
# source of truth shared with ensure-p0-workspaces.sh. rc=1 when none.
p0_active_todos_for_workstream() {
    local slug="$1"
    [[ -n "$slug" && -f "$SWITCHBOARD_TODO_JSON" ]] || return 1
    local out
    out=$(python3 "$REPO_ROOT/system/scripts/lib/p0-todos.py" "$SWITCHBOARD_TODO_JSON" --slug "$slug" 2>/dev/null) || return 1
    [[ -n "$out" ]] || return 1
    printf '%s\n' "$out"
}

# 0 when the slug is a valid workstream linked by at least one active P0.
workstream_has_active_p0() {
    local slug="$1"
    [[ -d "$REPO_ROOT/workstreams/$slug" && -f "$REPO_ROOT/workstreams/$slug/CONTEXT.md" ]] || return 1
    p0_active_todos_for_workstream "$slug" >/dev/null
}

# The P0-canonical workspace title for a workstream:
# "<workstream emoji> P0 · <display name>". The sidebar routes on the
# "P0 · " tag (emoji-independent) and renders the leading emoji as the row
# icon, so each P0 tab carries its workstream's own emoji (no
# uniform 🔥 across the P0 lane).
p0_workspace_name_for_workstream() {
    local formatted emoji rest
    formatted=$(format_workspace_name_for_source workstream "$1")
    emoji="${formatted%% *}"
    rest="${formatted#* }"
    if [[ -z "$emoji" || -z "$rest" || "$emoji" == "$formatted" ]]; then
        echo "🔥 P0 · $formatted"
    else
        echo "$emoji P0 · $rest"
    fi
}

# Legacy uniform-fire variant ("🔥 P0 · <emoji> <display>"): kept so lookups
# still resolve tabs created before the per-workstream emoji format; the
# ensure-p0-workspaces.sh reconcile renames them to the new format.
legacy_p0_workspace_name_for_workstream() {
    echo "🔥 P0 · $(format_workspace_name_for_source workstream "$1")"
}

# Deterministic emoji for an UNLINKED P0 reminder card, hashed on the todo id
# so it is stable across runs and title edits. Varied set — never uniform 🔥.
P0_REMINDER_EMOJIS=(🚨 🎯 ⚡ 🧨 ❗ 💥 📌 🚩 🛎️ ⏳)
p0_reminder_emoji() {
    local sum
    sum=$(printf '%s' "$1" | cksum | awk '{print $1}')
    echo "${P0_REMINDER_EMOJIS[$((sum % ${#P0_REMINDER_EMOJIS[@]}))]}"
}

# Reminder-card title for an unlinked P0 todo: "<emoji> P0 · <title>".
p0_reminder_workspace_name() {
    local todo_id="$1" title="$2"
    echo "$(p0_reminder_emoji "$todo_id") P0 · $title"
}

# True when a workspace title is owned by the P0 mechanism: new format
# "<emoji> P0 · <rest>" (lane tag right after a leading emoji token) or the
# legacy "🔥 P0 · <rest>" prefix. The leading token must not start with an
# ASCII letter/digit so a user workspace like "Discuss P0 · roadmap" is
# never claimed (and never closed) by the reconcile.
is_p0_owned_title() {
    local t="$1"
    [[ "$t" == "🔥 P0 ·"* ]] && return 0
    [[ "$t" == *" "* && "${t#* }" == "P0 · "* ]] || return 1
    [[ "${t:0:1}" =~ [A-Za-z0-9] ]] && return 1
    return 0
}

# Canonical display name: P0 title while an active P0 links the workstream,
# normal formatted title otherwise.
workstream_canonical_display_name() {
    local slug="$1"
    if workstream_has_active_p0 "$slug"; then
        p0_workspace_name_for_workstream "$slug"
    else
        format_workspace_name_for_source workstream "$slug"
    fi
}

# Prompt header describing the linked P0(s); prepended to the workstream
# launch prompt when seeding a P0-canonical workspace.
p0_prompt_header_for_workstream() {
    local slug="$1"
    local todo_cli="$HOME/.agents/skills/todo/scripts/todo_cli.py"
    local lines id title
    lines=$(p0_active_todos_for_workstream "$slug") || return 1
    echo "P0 CONTEXT: This workspace is the canonical execution/review space for workstream '$slug' AND its linked P0 to-do(s):"
    while IFS=$'\x1f' read -r id title _ws _detail; do
        [[ -z "$id" ]] && continue
        echo "- [P0] $title (todo id: $id — when the owner confirms it is done: python3 $todo_cli complete $id)"
    done <<< "$lines"
    echo "Render the P0(s) prominently in your first summary table. Do not start executing a P0 unprompted; surface it, then follow the owner's lead."
}

# Startup prompt for a workstream: config.yaml startup_instruction if set,
# else the standard resume-from-CONTEXT prompt.
workstream_startup_prompt() {
    local slug="$1"
    local ws_dir="$REPO_ROOT/workstreams/$slug"
    local startup=""
    if [[ -f "$ws_dir/config.yaml" ]]; then
        startup=$(python3 - "$ws_dir/config.yaml" <<'PY'
import sys, yaml
try:
    with open(sys.argv[1]) as fh:
        payload = yaml.safe_load(fh) or {}
    print(payload.get("startup_instruction", "") or "")
except Exception:
    pass
PY
) || startup=""
    fi
    if [[ -n "$startup" ]]; then
        printf '%s\n' "$startup"
    else
        echo "Read workstreams/$slug/CONTEXT.md and resume from the last known state. Identify open questions, pending decisions, and next concrete deliverables."
    fi
}

# After the canonical P0 tab exists, close a leftover duplicate normal-named
# tab for the same workstream — but ONLY when it is provably safe (agent idle
# AND empty input box, via cig_safe). A busy tab or one with a draft is left
# alone with a warning; the next reconcile run retries. The Amp thread behind
# a closed tab persists and remains resumable.
consolidate_duplicate_workstream_tab() {
    local slug="$1"
    local canonical_ref="$2"
    local candidate ref
    for candidate in \
        "$(format_workspace_name_for_source workstream "$slug")" \
        "$(format_legacy_workspace_name "$slug")" \
        "$slug"; do
        [[ -z "$candidate" ]] && continue
        ref=$(find_local_workspace_by_name "$candidate" 2>/dev/null) || continue
        [[ "$ref" == "$canonical_ref" ]] && continue
        if ! workspace_thread_gate "$ref" "$slug"; then
            log_warn "Duplicate title has unverified ownership ($ref); leaving it open"
            continue
        fi
        if declare -F cig_safe >/dev/null 2>&1 && cig_safe "$ref"; then
            log_info "Consolidating duplicate workstream tab into P0 space: $candidate ($ref) -> $canonical_ref"
            cmux_cmd close-workspace --workspace "$ref" --force &>/dev/null || \
                log_warn "Failed to close duplicate workstream tab $ref"
        else
            log_warn "Duplicate tab for $slug ($ref) is busy or has a draft; leaving it open (will retry on next reconcile)"
        fi
    done
    return 0
}

# --- Workspace Functions ---

# Get the terminal surface ref for a workspace.
# cmux read-screen requires --surface; this resolves it from the workspace tree.
# Resolve a workspace's terminal surface ref. Tri-state return so callers can
# distinguish a transient cmux failure from a genuinely surface-less workspace:
#   0 -> prints "surface:N" (found)
#   2 -> tree succeeded but no surface is attached yet (real absence)
#   3 -> the cmux command itself failed (contention / socket / lock timeout)
# Callers that only need the ref keep using `$(get_workspace_surface ...)` and
# checking for non-empty output; the retry loops inspect the return code.
get_workspace_surface() {
    local ws_ref="$1"
    local tree_output
    if ! tree_output=$(cmux_cmd tree --workspace "$ws_ref" 2>/dev/null); then
        return 3
    fi
    local surface_ref
    surface_ref=$(printf '%s\n' "$tree_output" | grep -oE 'surface:[0-9]+' | head -1 || true)
    if [[ -z "$surface_ref" ]]; then
        return 2
    fi
    echo "$surface_ref"
    return 0
}

# Parse one `list-workspaces` line into WS_LINE_REF ("workspace:N", the first
# ref on the line) and WS_LINE_NAME (the title with the leading markers and
# ref and the trailing "[selected]" stripped). rc 1 when the line has no ref.
# Pure bash on purpose: a grep + 2×sed per line costs ~30 process spawns per
# 10-workspace lookup, which pushes publish-workspace-status.sh past its 300s
# background budget.
WS_LINE_REF=""
WS_LINE_NAME=""
parse_workspace_listing_line() {
    local line="$1"
    local ref_re='workspace:[0-9]+'
    local head_re='^[* ]*workspace:[0-9]+  (.*)$'
    WS_LINE_REF=""
    WS_LINE_NAME=""
    [[ "$line" =~ $ref_re ]] || return 1
    WS_LINE_REF="${BASH_REMATCH[0]}"
    if [[ "$line" =~ $head_re ]]; then
        WS_LINE_NAME="${BASH_REMATCH[1]}"
    else
        WS_LINE_NAME="$line"
    fi
    if [[ "$WS_LINE_NAME" == *'[selected]' ]]; then
        WS_LINE_NAME="${WS_LINE_NAME%\[selected\]}"
        WS_LINE_NAME="${WS_LINE_NAME%"${WS_LINE_NAME##*[! ]}"}"
    fi
    return 0
}

# Find workspace by exact name. Prints ref (e.g. "workspace:2") on success.
find_workspace_by_name_in_listing() {
    local listing="$1"
    local target_name="$2"
    local line

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        parse_workspace_listing_line "$line" || continue
        if [[ "$WS_LINE_NAME" == "$target_name" ]]; then
            echo "$WS_LINE_REF"
            return 0
        fi
    done <<< "$listing"
    return 1
}

# Select the highest-numbered workspace whose title exactly matches. This is
# deterministic when duplicate titles exist and avoids claiming an unrelated
# workspace created concurrently.
find_newest_workspace_by_name_in_listing() {
    local listing="$1"
    local target_name="$2"
    local line number newest_number=-1 newest_ref=""

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        parse_workspace_listing_line "$line" || continue
        [[ "$WS_LINE_NAME" == "$target_name" ]] || continue
        number="${WS_LINE_REF#workspace:}"
        if (( number > newest_number )); then
            newest_number="$number"
            newest_ref="$WS_LINE_REF"
        fi
    done <<< "$listing"

    [[ -n "$newest_ref" ]] || return 1
    echo "$newest_ref"
}

# Resolve a newly-created workspace from command output, then from an exact
# title match. The last-workspace fallback is retained only for older cmux
# versions that neither print a ref nor honor --name.
workspace_ref_after_create() {
    local output="$1"
    local title="$2"
    local ws_ref listing_output

    ws_ref=$(echo "$output" | grep -oE 'workspace:[0-9]+' | head -1 || true)
    if [[ -n "$ws_ref" ]]; then
        echo "$ws_ref"
        return 0
    fi

    sleep 1
    listing_output=$(cmux_cmd list-workspaces 2>/dev/null || true)
    ws_ref=$(find_newest_workspace_by_name_in_listing "$listing_output" "$title" || true)
    if [[ -z "$ws_ref" ]]; then
        log_warn "No exact title match for newly-created workspace '$title'; falling back to last workspace"
        ws_ref=$(printf '%s\n' "$listing_output" | grep -oE 'workspace:[0-9]+' | tail -1 || true)
    fi
    [[ -n "$ws_ref" ]] || return 1
    echo "$ws_ref"
}

find_workspace_by_substring_in_listing() {
    local listing="$1"
    local target_fragment="$2"
    local line

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        parse_workspace_listing_line "$line" || continue
        if [[ "$WS_LINE_NAME" == *"$target_fragment"* ]]; then
            echo "$WS_LINE_REF"
            return 0
        fi
    done <<< "$listing"
    return 1
}

workspace_name_for_ref_in_listing() {
    local listing="$1"
    local target_ref="$2"
    local line

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        parse_workspace_listing_line "$line" || continue
        [[ "$WS_LINE_REF" != "$target_ref" ]] && continue
        printf '%s\n' "$WS_LINE_NAME"
        return 0
    done <<< "$listing"
    return 1
}

# Create already sets --name. Verify before issuing a redundant rename, and
# verify again after a failed response because the write may have succeeded.
ensure_workspace_title() {
    local ref="$1" title="$2" listing current output="" attempt
    for attempt in 1 2 3; do
        # A new workspace may not be visible yet. A failed read is not proof
        # that creation or rename failed; retry within the same bounded loop.
        if listing=$(cmux_cmd list-workspaces 2>/dev/null) \
            && current=$(workspace_name_for_ref_in_listing "$listing" "$ref"); then
            [[ "$current" == "$title" ]] && return 0
            output=$(cmux_cmd rename-workspace --workspace "$ref" "$title" 2>&1) || true
        fi
        sleep 0.2
    done
    if listing=$(cmux_cmd list-workspaces 2>/dev/null) \
        && current=$(workspace_name_for_ref_in_listing "$listing" "$ref") \
        && [[ "$current" == "$title" ]]; then
        return 0
    fi
    log_warn "Workspace $ref title verification failed: $output"
    return 1
}

find_workspace_by_name() {
    local target_name="$1"
    local output
    output=$(cmux_cmd list-workspaces 2>/dev/null) || return 1
    find_workspace_by_name_in_listing "$output" "$target_name"
}

# A workstream must run in a local cmux terminal, never an SSH/cloud/Orb
# surface. Remote workspaces can share the same display title, so callers that
# route PM work must filter them out instead of blindly reusing the first name
# match.
workspace_is_remote() {
    local ws_ref="$1"
    local processes
    processes=$(cmux_cmd top --workspace "$ws_ref" --processes --flat --format tsv 2>/dev/null || true)
    # Match only a process rooted at this workspace's terminal surface. cmux
    # also attributes Amp hook processes through stale workspace status tags;
    # those tag-owned rows do not mean the terminal is running that process.
    printf '%s\n' "$processes" | grep -Eq $'\tprocess\t[^\t]+\tsurface:[^\t]+\t(ssh|mosh|vmctl|orb)([[:space:]]|$)'
}

find_local_workspace_by_name() {
    local target_name="$1"
    local output line
    output=$(cmux_cmd list-workspaces 2>/dev/null) || return 1

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        parse_workspace_listing_line "$line" || continue
        # The owner probe proved this stale tab has no agent. Leave it intact;
        # the current route needs a fresh workspace, not a reset of that shell.
        [[ "$WS_LINE_REF" == "${CMUX_SKIP_OWNER_REF:-}" ]] && continue
        if [[ "$WS_LINE_NAME" == "$target_name" ]] && ! workspace_is_remote "$WS_LINE_REF"; then
            echo "$WS_LINE_REF"
            return 0
        fi
    done <<< "$output"
    return 1
}

# Find or create workspace by name. Prints ref on success.
# Return 0 (and print the ref) if a workspace already exists for this name
# under any of the naming variants find_or_create_workspace would match.
# Informational lookup only. Creation provenance must come from the locked
# find_or_create_workspace operation, never a separate preflight probe.
workspace_ref_if_exists() {
    local name="$1"
    local display_name legacy_display_name ref
    display_name=$(format_workspace_name "$name")
    legacy_display_name=$(format_legacy_workspace_name "$name")
    # P0-canonical variant: a workstream linked by an active P0 lives in a
    # "<emoji> P0 · <display>" tab (or the legacy "🔥 P0 · <display>" title
    # pending migration); that tab counts as the existing workspace.
    if [[ -d "$REPO_ROOT/workstreams/$name" ]]; then
        local p0_display_name
        for p0_display_name in \
            "$(p0_workspace_name_for_workstream "$name")" \
            "$(legacy_p0_workspace_name_for_workstream "$name")"; do
            if ref=$(find_local_workspace_by_name "$p0_display_name" 2>/dev/null); then
                echo "$ref"; return 0
            fi
        done
    fi
    if ref=$(find_local_workspace_by_name "$display_name" 2>/dev/null); then
        echo "$ref"; return 0
    fi
    if [[ "$legacy_display_name" != "$display_name" ]] && ref=$(find_local_workspace_by_name "$legacy_display_name" 2>/dev/null); then
        echo "$ref"; return 0
    fi
    if ref=$(find_local_workspace_by_name "$name" 2>/dev/null); then
        echo "$ref"; return 0
    fi
    return 1
}

# After the launch command is typed: confirm the tab shows the Amp TUI within
# the dispatch.py budget (30 s). Runs `dispatch.py verify-amp`, which retypes
# the launch command exactly once on a bare shell prompt, and never types into
# an unreadable or unrecognised screen.
# Return codes:
#   0  Amp TUI verified
#   1  the tab is known not to run Amp (shell persists after the one retype,
#      or cmux refused the keystrokes)
#   2  inconclusive: unknown screen (nothing typed), or the helper could not
#      run; the caller keeps its own extended wait
# AMP_VERIFY_SECONDS / AMP_VERIFY_POLL are test seams; production keeps the
# dispatch.py defaults.
verify_amp_launch() {
    local ws_ref="$1"
    local name="$2"
    local launch_command="$3"
    local agent_name
    agent_name=$(agent_display_name)
    if [[ ! -f "$SWITCHBOARD_DISPATCH_PY" ]] || ! command -v python3 >/dev/null 2>&1; then
        log_warn "$agent_name verification skipped for $name: dispatch.py or python3 unavailable"
        return 2
    fi
    local args=(verify-amp --workspace-ref "$ws_ref" --command "$launch_command" --label "$name")
    [[ -n "${AMP_VERIFY_SECONDS:-}" ]] && args+=(--seconds "$AMP_VERIFY_SECONDS")
    [[ -n "${AMP_VERIFY_POLL:-}" ]] && args+=(--poll "$AMP_VERIFY_POLL")
    local verdict rc=0
    # dispatch.py resolves the agent itself (agent_kind) and waits for the
    # matching screen marker; pass this library's answer so both sides agree
    # even when they would read different router.yaml files (fixture REPO_ROOT).
    verdict=$(SWITCHBOARD_AGENT="$(switchboard_agent)" python3 "$SWITCHBOARD_DISPATCH_PY" "${args[@]}") || rc=$?
    case "$verdict" in
        *'"launch_retyped": true'*) log_warn "Launch command retyped once into $ws_ref for $name (consumed by the shell)" ;;
    esac
    case "$rc" in
        0) return 0 ;;
        2) log_warn "$agent_name verification inconclusive for $name in $ws_ref (nothing typed): $verdict"; return 2 ;;
        *) log_error "$agent_name did not start in $ws_ref for $name: $verdict"; return 1 ;;
    esac
}

find_or_create_workspace() (
    local name="$1"
    local cwd="${2:-$REPO_ROOT}"
    local provenance="${3:-false}"
    local display_name
    local legacy_display_name
    local p0_display_name=""
    local legacy_p0_display_name=""
    local target_display
    local ws_ref

    display_name=$(format_workspace_name "$name")
    legacy_display_name=$(format_legacy_workspace_name "$name")
    target_display="$display_name"

    # P0-linked workstreams are canonical under "<emoji> P0 · <display>".
    # Targeting the P0 title here means: an existing normal tab is RENAMED
    # into the P0 space (live session preserved), and when the P0 closes, a
    # stale P0 tab is renamed back to the normal title on the next launch.
    if [[ -d "$REPO_ROOT/workstreams/$name" && -f "$REPO_ROOT/workstreams/$name/CONTEXT.md" ]]; then
        p0_display_name=$(p0_workspace_name_for_workstream "$name")
        legacy_p0_display_name=$(legacy_p0_workspace_name_for_workstream "$name")
        if workstream_has_active_p0 "$name"; then
            target_display="$p0_display_name"
        fi
    fi

    # Serialize only lookup/create/rename. The caller performs readiness
    # polling and prompt seeding after this subshell releases the lock.
    acquire_ws_launch_lock || return 1
    trap 'release_ws_launch_lock' EXIT

    if ws_ref=$(find_local_workspace_by_name "$target_display"); then
        log_info "Reusing workspace: $target_display ($ws_ref)"
        if [[ "$provenance" == true ]]; then printf '%s\tfalse\n' "$ws_ref"; else echo "$ws_ref"; fi
        return 0
    fi

    local variant
    for variant in "$display_name" "$legacy_display_name" "$name" "$p0_display_name" "$legacy_p0_display_name"; do
        [[ -z "$variant" || "$variant" == "$target_display" ]] && continue
        if ws_ref=$(find_local_workspace_by_name "$variant"); then
            log_info "Reusing workspace under prior title: $variant ($ws_ref) -> $target_display"
            ensure_workspace_title "$ws_ref" "$target_display" || {
                log_warn "Failed to rename workspace to: $target_display"
                return 1
            }
            if [[ "$provenance" == true ]]; then printf '%s\tfalse\n' "$ws_ref"; else echo "$ws_ref"; fi
            return 0
        fi
    done

    log_info "Creating workspace: $target_display"
    local output before
    before=$(cmux_cmd rpc workspace.list 2>/dev/null || true)
    output=$(cmux_cmd new-workspace --name "$target_display" --cwd "$cwd" --focus false 2>&1) || {
        # A lost create reply is not proof of failure. Re-probe without another
        # create, accepting only one new UUID with this exact title and cwd.
        if ws_ref=$(python3 "$REPO_ROOT/system/scripts/workstream_thread_bindings.py" \
            --root "$REPO_ROOT" recover-launch "$name" "$target_display" "$cwd" "$before" --cmux "$CMUX_BIN"); then
            log_warn "Recovered workspace after uncertain create: $target_display ($ws_ref)"
            # Treat as reused so the live-session guard prevents typing a
            # shell launch command into an Amp that started before the timeout.
            if [[ "$provenance" == true ]]; then printf '%s\tfalse\n' "$ws_ref"; else echo "$ws_ref"; fi
            return 0
        fi
        log_error "Create outcome remains unverified: $target_display"
        return 1
    }

    if ! ws_ref=$(workspace_ref_after_create "$output" "$target_display"); then
        log_error "Could not determine ref for new workspace: $name"
        return 1
    fi

    ensure_workspace_title "$ws_ref" "$target_display" || {
        log_warn "Failed to rename workspace to: $target_display"
        return 1
    }

    log_ok "Created workspace: $target_display ($ws_ref)"
    if [[ "$provenance" == true ]]; then printf '%s\ttrue\n' "$ws_ref"; else echo "$ws_ref"; fi
)

# Reset a workspace surface to a clean shell prompt so a launch command can be
# typed. PROBE-FIRST, NEVER /exit: against a busy Amp session, typed
# ctrl+c and "/exit" queue unprocessed and later end the thread. Input typed into
# a live session is destructive-by-default (a queued /exit is a time bomb;
# even ctrl+c cancels an active turn or clears a draft), so this function
# types NOTHING until it has positively established there is no live session.
#
# Return codes:
#   0  surface is a plain shell (or a dead post-/exit TUI that quit cleanly);
#      safe to type a launch command
#   1  hard failure (dead TUI would not quit)
#   3  live-or-unverifiable Amp session preserved; NOTHING was typed. The
#      caller must reuse/resume the session or fail closed — never retry
#      with keystrokes.
reset_workspace_surface() {
    local ws_ref="$1"

    # 1. Probe for a live amp process. A SUCCESSFUL `top` listing is the
    #    authority: no amp row means no session (screen markers can be stale
    #    scrollback from a crashed TUI). A FAILED probe means we cannot know
    #    whether a session is live -> fail closed, type nothing.
    local amp_alive="unknown" attempt processes=""
    for attempt in 1 2 3; do
        if processes=$(cmux_cmd top --workspace "$ws_ref" --processes --flat --format tsv 2>/dev/null); then
            if printf '%s\n' "$processes" | grep -Eq $'\tprocess\t[^\t]+\tsurface:[^\t]+\tamp$'; then
                amp_alive="yes"
            else
                amp_alive="no"
            fi
            break
        fi
        sleep 1
    done

    if [[ "$amp_alive" == "unknown" ]]; then
        log_warn "Cannot verify whether an Amp session is live in $ws_ref (process probe failed); preserving it untouched"
        return 3
    fi

    if [[ "$amp_alive" == "yes" ]]; then
        local sf_ref screen=""
        sf_ref=$(get_workspace_surface "$ws_ref" 2>/dev/null || true)
        if [[ -n "$sf_ref" ]]; then
            screen=$(cmux_cmd read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 50 2>/dev/null || true)
        fi
        # A lingering post-/exit TUI ("Session ended") is the ONLY amp process
        # we may quit: its thread is already over, and Amp's documented quit
        # chord (Ctrl+C, Ctrl+C) exits the TUI without typing into any thread.
        if printf '%s\n' "$screen" | grep -q 'Session ended'; then
            log_info "Dead post-/exit Amp TUI in $ws_ref; quitting it (Ctrl+C, Ctrl+C)"
            cmux_cmd send-key --workspace "$ws_ref" "ctrl+c" &>/dev/null || true
            sleep 0.25
            cmux_cmd send-key --workspace "$ws_ref" "ctrl+c" &>/dev/null || true
            local waited=0
            while (( waited < 10 )); do
                if processes=$(cmux_cmd top --workspace "$ws_ref" --processes --flat --format tsv 2>/dev/null) \
                    && ! printf '%s\n' "$processes" | grep -Eq $'\tprocess\t[^\t]+\tsurface:[^\t]+\tamp$'; then
                    return 0
                fi
                sleep 1
                (( waited += 1 ))
            done
            log_error "Dead Amp TUI did not quit in $ws_ref; refusing to type a launch command into it"
            return 1
        fi
        # Live session (or a session whose screen we cannot read): preserve it.
        log_info "Live Amp session in $ws_ref; preserving it untouched (no /exit, no ctrl+c)"
        return 3
    fi

    # No Amp does not mean an idle shell: a bulk relaunch runs Python before
    # execing Amp. Preserve terminal-rooted commands and their descendants;
    # ignore stale tag-owned processes from other sessions.
    if ! printf '%s\n' "$processes" | awk -F '\t' '
        NF >= 5 && $(NF-3) == "process" {
            parent[$(NF-2)] = $(NF-1); command[$(NF-2)] = $NF
            if ($(NF-1) ~ /^surface:/) owned[$(NF-2)] = 1
        }
        END {
            do {
                changed = 0
                for (pid in parent)
                    if (!owned[pid] && owned[parent[pid]]) { owned[pid] = 1; changed = 1 }
            } while (changed)
            for (pid in owned)
                if (owned[pid] && command[pid] !~ /^(login|zsh|bash|sh|fish)$/) exit 1
        }'; then
        log_warn "Non-shell process in $ws_ref; preserving startup or foreground work untouched"
        return 3
    fi
    # A plain shell needs no interrupt. Ctrl+C can kill a relaunch bootstrap
    # that starts between the process probe and the first key.
    return 0
}

# Wait for Amp's prompt to appear. Returns 0 on success, 1 on timeout.
wait_for_amp_ready() {
    local ws_ref="$1"
    local timeout="${2:-$AMP_READY_TIMEOUT}"
    local elapsed=0

    # Resolve the terminal surface (required for read-screen). Use a wall-clock
    # budget and only count a REAL absence (tree ok, no surface -> rc 2) toward
    # giving up. A transient cmux failure (rc 3, e.g. lock/socket contention
    # during start-day fan-out) must NOT consume the budget as if the surface
    # were missing — that false-negative was the recurring "No surface found
    # after 20 attempts" bug. Errors just retry within the deadline.
    local sf_ref=""
    local sf_budget=20
    local sf_elapsed=0
    local sf_rc
    while (( sf_elapsed < sf_budget )); do
        sf_ref=$(get_workspace_surface "$ws_ref" 2>/dev/null)
        sf_rc=$?
        if [[ -n "$sf_ref" ]]; then
            break
        fi
        if (( sf_rc == 3 )); then
            log_warn "cmux busy while resolving surface for $ws_ref (transient); retrying"
        fi
        sleep 1
        (( sf_elapsed += 1 ))
    done
    if [[ -z "$sf_ref" ]]; then
        log_error "No surface found for $ws_ref after ${sf_elapsed}s, cannot poll for Amp prompt"
        return 1
    fi

    while (( elapsed < timeout )); do
        local screen
        # Read more lines so we catch the prompt box AND any Neo welcome
        # markers — the welcome dialog ("Welcome to Amp" / "Ctrl+O for help")
        # sits in the middle of the viewport, well above the last 5 lines.
        screen=$(cmux_cmd read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 50 2>/dev/null || true)
        # Match Amp-specific markers only. Bare "❯" / "> " were removed because
        # they also match a plain zsh/starship shell prompt, which made this
        # falsely report "Amp ready" when the launch command had been dropped
        # and only the shell prompt was on screen. Amp Neo always renders a
        # bordered prompt box (╭──/╰──) or the welcome banner. Under
        # SWITCHBOARD_AGENT=claude the marker is Claude Code's input footer instead.
        if echo "$screen" | grep -qE "$(agent_ready_marker)"; then
            return 0
        fi
        sleep "$AMP_READY_POLL"
        (( elapsed += AMP_READY_POLL ))
    done

    log_error "$(agent_display_name) not ready after ${timeout}s in $ws_ref"
    return 1
}

# The cmux terminal can be local while the active Amp executor is an Orb. The
# TUI renders that state explicitly; detect it before sending any workstream
# prompt or handoff. Return 0 only when the screen positively identifies Orb
# execution.
workspace_amp_is_orb() {
    local ws_ref="$1"
    local sf_ref
    local screen

    # Orbs are an Amp executor; a Claude Code tab is always local.
    [[ "$(switchboard_agent)" == "claude" ]] && return 1
    sf_ref=$(get_workspace_surface "$ws_ref" 2>/dev/null || true)
    [[ -n "$sf_ref" ]] || return 1
    screen=$(cmux_cmd read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 80 2>/dev/null || true)
    printf '%s\n' "$screen" | grep -Eqi '(In the Orb|running inside an orb|running in an Amp Orb)'
}

# TRUE (rc 0) when a workspace holds a LIVE local agent session (Amp or Claude
# Code): the agent process is running AND the TUI is on a real session screen
# (bordered prompt box, welcome banner, or the Claude footer), not the
# post-/exit "Session ended" screen. This is the gate
# for reusing a session in place instead of resetting it. A live session
# must never be reset — it is the recovery target, not an obstacle.
workspace_has_live_amp() {
    local ws_ref="$1"
    local processes
    processes=$(cmux_cmd top --workspace "$ws_ref" --processes --flat --format tsv 2>/dev/null || true)
    # Either agent counts as live: a
    # Claude Code tab must not be reset or typed over any more than an Amp one.
    printf '%s\n' "$processes" | grep -Eq $'\tprocess\t[^\t]+\tsurface:[^\t]+\t(amp|claude)$' || return 1
    local sf_ref screen
    sf_ref=$(get_workspace_surface "$ws_ref" 2>/dev/null || true)
    [[ -n "$sf_ref" ]] || return 1
    screen=$(cmux_cmd read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 50 2>/dev/null || true)
    printf '%s\n' "$screen" | grep -qE "${AMP_READY_MARKER}|${CLAUDE_READY_MARKER}" || return 1
    # A lingering post-/exit TUI is not a live session.
    printf '%s\n' "$screen" | grep -q 'Session ended' && return 1
    return 0
}

# Best-effort Amp thread ID for a workspace from the cmux session store (the
# same join command-center-digest.py uses). Prints the thread ID or nothing.
amp_thread_for_workspace() {
    local ws_ref="$1"
    python3 "$REPO_ROOT/system/scripts/workspace-session-state.py" \
        --workspace "$ws_ref" 2>/dev/null | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('thread_id') or '')
except Exception:
    pass
" 2>/dev/null || true
}

# thread_provenance_for_workstream <thread_id> <slug>
# Classify whether an Amp thread PROVABLY belongs to a workstream before any
# routed prompt is typed into its live session. A tab titled for one
# workstream can host another workstream's thread.
# Evidence source: system/state/workstream-thread-bindings.json, written by
# launchers. Telemetry content is optional and must not control owner reuse.
# Legacy fallback only: ~/.agents/.amp-plugin-logs/<thread>.jsonl (override
# dir via AMP_PLUGIN_LOG_DIR for tests), with these delimited markers:
#   - "launch-prompts/<slug>.md"        (its startup prompt / reading it)
#   - "--source workstream:<slug>"      (emitting its own durable updates)
# Prints one of, and returns 0 ONLY for verified — proof, not absence of
# counter-evidence, is what clears a pre-existing thread for routed sends:
#   verified    marker found                          -> safe (rc 0)
#   unseeded    log exists but no agent_start yet     -> empty thread, but NOT
#                                                        proven ours (rc 1)
#   unverified  log has prompts but NO marker         -> FOREIGN thread (rc 1)
#   unknown     no thread id / no readable log        -> fail closed   (rc 1)
thread_provenance_for_workstream() {
    local thread_id="$1"
    local slug="$2"
    if [[ -z "$thread_id" || -z "$slug" ]]; then
        echo "unknown"
        return 1
    fi
    local binding
    binding=$(python3 "$REPO_ROOT/system/scripts/workstream_thread_bindings.py" \
        --root "$REPO_ROOT" lookup "$thread_id" "$slug" 2>/dev/null) || binding="unverified"
    case "$binding" in
        verified) echo "verified"; return 0 ;;
        absent) ;; # Only an absent binding permits legacy telemetry evidence.
        *) echo "unverified"; return 1 ;;
    esac
    local log_file="${AMP_PLUGIN_LOG_DIR:-$HOME/.agents/.amp-plugin-logs}/${thread_id}.jsonl"
    if [[ ! -r "$log_file" ]]; then
        echo "unknown"
        return 1
    fi
    # Delimited matches so a slug that is a PREFIX of another slug can never
    # cross-verify (e.g. "garden" must not match "garden-planner").
    # Slugs are kebab-case [a-z0-9-], safe as a literal in grep -E.
    if grep -qF "launch-prompts/${slug}.md" "$log_file" 2>/dev/null || \
       grep -Eq -- "--source workstream:${slug}([^a-z0-9-]|$)" "$log_file" 2>/dev/null; then
        echo "verified"
        return 0
    fi
    if ! grep -qF '"type":"agent_start"' "$log_file" 2>/dev/null; then
        echo "unseeded"
        return 1
    fi
    echo "unverified"
    return 1
}

# workspace_thread_gate <ws_ref> <slug>
# Resolve the thread CURRENTLY on a live workspace tab and check its
# provenance for the workstream. Sets WSG_THREAD_ID (the tab's current thread,
# "" if unbound) and WSG_PROVENANCE. Returns 0 only when typing a routed
# prompt into that live session is safe (verified ONLY). A tab title is NOT
# proof of thread ownership — this gate is what prevents seeding a foreign
# thread that happens to be open in the workstream's tab. unseeded (empty
# thread) also fails closed: absence of markers is not proof of ownership,
# and an unprovable pre-existing session is surfaced for the owner instead of
# silently typed into (fail-closed invariant).
workspace_thread_gate() {
    local ws_ref="$1"
    local slug="$2"
    WSG_THREAD_ID="$(amp_thread_for_workspace "$ws_ref")"
    WSG_PROVENANCE="$(thread_provenance_for_workstream "$WSG_THREAD_ID" "$slug")" || true
    [[ "$WSG_PROVENANCE" == "verified" ]]
}

# Block until a brand-new workspace has an attached terminal surface.
# Returns 0 once a surface exists, 1 on timeout. Needed because during
# start-day fan-out several workspaces are created back-to-back and cmux can
# lag allocating the surface for one under contention. If we send the amp
# launch command before the surface exists, the command is lost and the tab
# comes up as an empty shell (the old "No surface found" failure mode).
wait_for_workspace_surface() {
    local ws_ref="$1"
    local timeout="${2:-30}"
    local elapsed=0
    while (( elapsed < timeout )); do
        if [[ -n "$(get_workspace_surface "$ws_ref" 2>/dev/null || true)" ]]; then
            return 0
        fi
        sleep 1
        (( elapsed += 1 ))
    done
    log_warn "No surface for $ws_ref after ${timeout}s"
    return 1
}

# Type text into workspace and press Enter.
send_to_workspace() {
    local ws_ref="$1"
    local text="$2"

    if [[ -z "$text" ]]; then
        log_warn "Empty text for send_to_workspace, skipping"
        return 0
    fi

    cmux_cmd send --workspace "$ws_ref" "$text" &>/dev/null || {
        log_error "Failed to send text to $ws_ref"
        return 1
    }
    cmux_cmd send-key --workspace "$ws_ref" "Enter" &>/dev/null || {
        log_error "Failed to send Enter to $ws_ref"
        return 1
    }
}

# Set a sidebar status badge on a workspace.
set_workspace_status() {
    local ws_ref="$1"
    local key="$2"
    local value="$3"
    local icon="${4:-}"
    local color="${5:-}"

    local args=(set-status "$key" "$value" --workspace "$ws_ref")
    [[ -n "$icon" ]] && args+=(--icon "$icon")
    [[ -n "$color" ]] && args+=(--color "$color")

    cmux_cmd "${args[@]}" &>/dev/null || true
}

# Clear a sidebar status badge.
clear_workspace_status() {
    local ws_ref="$1"
    local key="$2"
    cmux_cmd clear-status "$key" --workspace "$ws_ref" &>/dev/null || true
}

# Reorder workspace to a specific index (0 = top).
reorder_workspace() {
    local ws_ref="$1"
    local index="$2"
    cmux_cmd reorder-workspace --workspace "$ws_ref" --index "$index" &>/dev/null || {
        log_warn "Failed to reorder $ws_ref to index $index"
    }
}

pin_workspace() {
    local ws_ref="$1"
    cmux_cmd workspace-action --workspace "$ws_ref" --action pin &>/dev/null || {
        log_warn "Failed to pin $ws_ref"
    }
}

rename_workspace_tab() {
    local ws_ref="$1"
    local title="$2"
    local sf_ref

    sf_ref=$(get_workspace_surface "$ws_ref" || true)
    if [[ -z "$sf_ref" ]]; then
        log_warn "No surface found for $ws_ref, skipping tab rename"
        return 0
    fi

    cmux_cmd rename-tab --workspace "$ws_ref" --surface "$sf_ref" "$title" &>/dev/null || {
        log_warn "Failed to rename tab for $ws_ref to: $title"
    }
}

style_switchboard_workspace() {
    local ws_ref="$1"
    local title="${2:-🧭 CoS}"
    local sf_ref

    cmux_cmd rename-workspace --workspace "$ws_ref" "$title" &>/dev/null || {
        log_warn "Failed to rename switchboard workspace to: $title"
    }
    # Do NOT rename the tab: a manual tab name freezes the title and masks
    # Amp's live braille-spinner title, which the switchboard sidebar relies on to
    # show the green "running" bolt (the cmux custom-sidebar API exposes no
    # agent status, only titles). The sidebar row label comes from the
    # workspace title anyway. Clear any legacy frozen name so it self-heals.
    sf_ref=$(get_workspace_surface "$ws_ref" || true)
    if [[ -n "$sf_ref" ]]; then
        cmux_cmd tab-action --action clear-name --workspace "$ws_ref" --tab "$sf_ref" &>/dev/null || true
    fi
    cmux_cmd workspace-action --workspace "$ws_ref" --action move-top &>/dev/null || {
        log_warn "Failed to move switchboard workspace $ws_ref to top"
    }
    pin_workspace "$ws_ref"
}

normalize_switchboard_workspace() {
    local listing="${1:-}"
    local title="${2:-🧭 CoS}"
    local ref=""
    local current_name=""

    if [[ -z "$listing" ]]; then
        listing=$(cmux_cmd list-workspaces 2>/dev/null || true)
    fi

    [[ -z "$listing" ]] && return 1

    for current_name in "🧭 CoS" "CoS"; do
        if ref=$(find_workspace_by_name_in_listing "$listing" "$current_name" 2>/dev/null); then
            break
        fi
        ref=""
    done

    if [[ -z "$ref" ]]; then
        for current_name in "Chief of Staff"; do
            if ref=$(find_workspace_by_substring_in_listing "$listing" "$current_name" 2>/dev/null); then
                break
            fi
            ref=""
        done
    fi

    [[ -z "$ref" ]] && return 1

    current_name=$(workspace_name_for_ref_in_listing "$listing" "$ref" 2>/dev/null || true)
    if [[ "$current_name" != "$title" ]]; then
        style_switchboard_workspace "$ref" "$title"
    fi

    echo "$ref"
}

find_workspace_ref_for_key() {
    local key="$1"
    find_workspace_ref_for_source_key "generic" "$key"
}

find_workspace_ref_for_source_key_in_listing() {
    local listing="$1"
    local source_type="$2"
    local key="$3"
    local candidate
    local ref
    local -a candidates=()

    case "$key" in
        switchboard|CoS|"Chief of Staff")
            candidates=("🧭 CoS" "CoS")
            ;;
        *)
            candidates+=("$(format_workspace_name_for_source "$source_type" "$key")")
            candidates+=("$(canonical_workspace_name_for_source "$source_type" "$key")")
            candidates+=("$key")
            if [[ "$key" == "comms-triage" ]]; then
                candidates+=("$(format_workspace_name_for_source "$source_type" "comms-triage-claude")")
            fi
            # P0-canonical variant: a P0-linked workstream's tab is titled
            # "<emoji> P0 · <display>" (legacy: "🔥 P0 · <display>"); resolve
            # updates/closes to it as well.
            if [[ "$source_type" == "workstream" || "$source_type" == "generic" ]] \
                && [[ -d "$REPO_ROOT/workstreams/$key" ]]; then
                candidates+=("$(p0_workspace_name_for_workstream "$key")")
                candidates+=("$(legacy_p0_workspace_name_for_workstream "$key")")
            fi
            ;;
    esac

    for candidate in "${candidates[@]}"; do
        if [[ -n "$candidate" ]] && ref=$(find_workspace_by_name_in_listing "$listing" "$candidate" 2>/dev/null); then
            echo "$ref"
            return 0
        fi
    done

    case "$key" in
        switchboard|CoS|"Chief of Staff")
            for candidate in "Chief of Staff"; do
                if ref=$(find_workspace_by_substring_in_listing "$listing" "$candidate" 2>/dev/null); then
                    echo "$ref"
                    return 0
                fi
            done
            ;;
    esac

    return 1
}

find_workspace_ref_for_source_key() {
    local source_type="$1"
    local key="$2"
    local listing
    listing=$(cmux_cmd list-workspaces 2>/dev/null) || return 1
    find_workspace_ref_for_source_key_in_listing "$listing" "$source_type" "$key"
}

close_workspace_ref_verified() {
    local ref="$1"
    local listing

    # --force: cmux 0.65 refuses to close a workspace with a running process
    # (every switchboard tab runs the Amp CLI) without it.
    if ! cmux_cmd close-workspace --workspace "$ref" --force; then
        log_error "Failed to close cmux workspace $ref"
        return 1
    fi
    if ! listing=$(cmux_cmd list-workspaces 2>/dev/null); then
        log_error "Could not verify cmux workspace $ref was removed"
        return 1
    fi
    if printf '%s\n' "$listing" | awk -v target="$ref" '
        {
            gsub(/^[ \t*]+/, "", $0)
            if ($0 == target || index($0, target " ") == 1) { found = 1; exit }
        }
        END { exit(found ? 0 : 1) }
    '; then
        log_error "Cmux workspace $ref is still present after close-workspace"
        return 1
    fi

    echo "$ref"
}

# Reduce a list-workspaces listing to its stable ordering signature: the
# workspace refs in display order. Raw listings embed live Amp braille
# spinner characters in workspace titles and the [selected] marker, both of
# which mutate constantly while agents run — comparing raw listings defeated
# the snapshot guard in arrange_workspace_sidebar and re-ran the move-top
# cascade on every publisher pass, visibly shuffling sidebar rows.
workspace_order_signature() {
    printf '%s\n' "$1" | grep -oE 'workspace:[0-9]+' || true
}

# Even the normalized
# full-order signature was too fragile a guard. cmux spontaneously reorders
# rows on its own (it bumps the active workspace when an agent runs — a
# workspace.reordered event with no matching workspace.action), so a
# whole-list comparison invalidated on every unrelated bump and the move-top
# cascade re-fired on the next publisher pass anyway. The arrangement only
# manages a handful of rows, so the guard must only care about (a) the
# relative order of those managed rows and (b) workspace open/close
# (membership), never about where cmux shuffles unmanaged rows.
#
# Filter a display-order signature down to the given managed refs,
# preserving display order. $1 = full signature (one ref per line),
# $2 = managed refs (one per line, any order).
filter_signature_to_refs() {
    local sig="$1" refs="$2"
    [[ -z "$refs" ]] && return 0
    printf '%s\n' "$sig" | grep -Fx -f <(printf '%s\n' "$refs") || true
}

arrange_workspace_sidebar() {
    local plan_file="$REPO_ROOT/system/today-plan.json"
    local state_dir="$REPO_ROOT/system/state"
    local snapshot_file="$state_dir/sidebar-arrangement.snapshot"
    local temp_snapshot
    local entry source_type key ref
    local workspace_listing=""
    local plan_snapshot="[]"
    local -a arrange_keys=()
    # Sidebar sections: CoS, To Do, Launchpad pinned on
    # top (then Comms Triage where it is on), Workers in the middle, Scheduled
    # Jobs and Meetings at the bottom. The canonical list is
    # system/scripts/sidebar_order.py; keep this one in step.
    local -a top_pinned=("routine:switchboard" "routine:todo" "routine:launchpad" "routine:comms-triage")
    if ! switchboard_comms_triage_enabled; then
        top_pinned=("routine:switchboard" "routine:todo" "routine:launchpad")
    fi
    local -a bottom_unpinned=("routine:scheduled-jobs" "routine:meetings")

    # The order applies on cmux only. wmux cannot order tabs from a script, and
    # switchboard leaves the wmux order alone.
    [[ "${SWITCHBOARD_MUX:-}" == "wmux" ]] && return 0
    local -a fallback_order=(
    )

    mkdir -p "$state_dir"
    if ! workspace_listing=$(cmux_cmd list-workspaces 2>/dev/null); then
        warn_dynamic_ui_updates_disabled "cmux dynamic updates disabled: workspace count unknown (list-workspaces failed)"
        return 0
    fi
    if ! cmux_dynamic_ui_updates_allowed_from_listing "$workspace_listing"; then
        return 0
    fi

    if command -v jq >/dev/null 2>&1 && [[ -f "$plan_file" ]] && jq -e '.workstreams_to_open' "$plan_file" >/dev/null 2>&1; then
        plan_snapshot=$(jq -c '(.workstreams_to_open // []) | map(.name)' "$plan_file" 2>/dev/null || echo '[]')
    fi

    local full_sig membership
    full_sig=$(workspace_order_signature "$workspace_listing")
    membership=$(printf '%s\n' "$full_sig" | sort)

    # Cheap guard (no ref resolution, no actions): skip when the previously
    # arranged managed rows are still in the same relative order, no
    # workspace opened or closed, and the plan is unchanged. Unmanaged rows
    # moving around (cmux's active-workspace bumps) must NOT invalidate this.
    if [[ -f "$snapshot_file" ]] && grep -q '^---managed---$' "$snapshot_file"; then
        local stored_managed
        stored_managed=$(sed -n '/^---managed---$/,/^---membership---$/p' "$snapshot_file" | sed '1d;$d')
        temp_snapshot=$(mktemp)
        {
            printf '%s\n' '---managed---'
            filter_signature_to_refs "$full_sig" "$stored_managed"
            printf '%s\n' '---membership---'
            printf '%s\n' "$membership"
            printf '%s\n' '---plan---'
            printf '%s\n' "$plan_snapshot"
        } > "$temp_snapshot"
        if cmp -s "$temp_snapshot" "$snapshot_file"; then
            rm -f "$temp_snapshot"
            return 0
        fi
        rm -f "$temp_snapshot"
    fi

    append_arrange_key() {
        local item="$1"
        local existing
        [[ -z "$item" ]] && return 0
        for existing in "${arrange_keys[@]+"${arrange_keys[@]}"}"; do
            [[ "$existing" == "$item" ]] && return 0
        done
        arrange_keys+=("$item")
    }

    for entry in "${top_pinned[@]}"; do
        append_arrange_key "$entry"
    done

    if command -v jq >/dev/null 2>&1 && [[ -f "$plan_file" ]] && jq -e '.workstreams_to_open' "$plan_file" >/dev/null 2>&1; then
        while IFS= read -r key; do
            append_arrange_key "workstream:$key"
        done < <(jq -r '.workstreams_to_open[].name' "$plan_file" 2>/dev/null)
    fi

    for entry in "${fallback_order[@]+"${fallback_order[@]}"}"; do
        append_arrange_key "$entry"
    done

    # Resolve every arrange key to a ref ONCE (this is the slow part — the
    # move-top cascade used to interleave with these lookups and stretch the
    # visible shuffle over ~10s). Matched entries/refs stay in arrange_keys
    # order, which is the desired final top-to-bottom display order.
    local -a matched_entries=()
    local -a matched_refs=()
    local existing_ref dup
    for entry in "${arrange_keys[@]}"; do
        source_type="${entry%%:*}"
        key="${entry#*:}"
        ref=$(find_workspace_ref_for_source_key_in_listing "$workspace_listing" "$source_type" "$key" 2>/dev/null || true)
        [[ -z "$ref" ]] && continue
        dup=0
        for existing_ref in "${matched_refs[@]+"${matched_refs[@]}"}"; do
            [[ "$existing_ref" == "$ref" ]] && { dup=1; break; }
        done
        [[ "$dup" == 1 ]] && continue
        matched_entries+=("$entry")
        matched_refs+=("$ref")
    done

    local desired_order="" current_order=""
    if [[ ${#matched_refs[@]} -gt 0 ]]; then
        desired_order=$(printf '%s\n' "${matched_refs[@]}")
        current_order=$(filter_signature_to_refs "$full_sig" "$desired_order")
    fi

    # Idempotency by construction: only issue move-tops when the managed
    # rows are actually out of relative order. Issuing them unconditionally
    # is what physically shuffled the sidebar on every guard miss.
    if [[ "$current_order" != "$desired_order" ]]; then
        for (( idx=${#matched_refs[@]}-1; idx>=0; idx-- )); do
            ref="${matched_refs[$idx]}"
            cmux_cmd workspace-action --workspace "$ref" --action move-top &>/dev/null || {
                log_warn "Failed to move ${matched_entries[$idx]} to top"
            }
        done

        for entry in "${top_pinned[@]}"; do
            for (( idx=0; idx<${#matched_entries[@]}; idx++ )); do
                if [[ "${matched_entries[$idx]}" == "$entry" ]]; then
                    pin_workspace "${matched_refs[$idx]}"
                    break
                fi
            done
        done

        # Scheduled Jobs and Meetings sit in the bottom section, never pinned;
        # the cmux sidebar's
        # Background lane holds them below the Workers.
        for entry in "${bottom_unpinned[@]}"; do
            ref=$(find_workspace_ref_for_source_key_in_listing "$workspace_listing" "${entry%%:*}" "${entry#*:}" 2>/dev/null || true)
            [[ -n "$ref" ]] && cmux_cmd workspace-action --workspace "$ref" --action unpin &>/dev/null
        done

        workspace_listing=$(cmux_cmd list-workspaces 2>/dev/null || true)
        full_sig=$(workspace_order_signature "$workspace_listing")
        membership=$(printf '%s\n' "$full_sig" | sort)
    fi

    # Record the ACTUAL post-arrangement order of the managed rows (not the
    # theoretical one) so the cheap guard compares like with like next pass.
    temp_snapshot=$(mktemp)
    {
        printf '%s\n' '---managed---'
        filter_signature_to_refs "$full_sig" "$desired_order"
        printf '%s\n' '---membership---'
        printf '%s\n' "$membership"
        printf '%s\n' '---plan---'
        printf '%s\n' "$plan_snapshot"
    } > "$temp_snapshot"
    mv "$temp_snapshot" "$snapshot_file"

    unset -f append_arrange_key
}

# Thread ID for a workspace the launcher just created, waiting (bounded) for
# the cmux session store to learn it. Amp reports its thread at session_start,
# after the prompt renders, so a single read at launch time is empty and the
# post-seed recheck in record_workspace fails. Prints the thread ID or nothing.
amp_thread_for_launched_workspace() {
    local ws_ref="$1" wait="${2:-${LAUNCH_THREAD_WAIT_SECONDS:-30}}"
    python3 "$REPO_ROOT/system/scripts/workstream_thread_bindings.py" \
        --root "$REPO_ROOT" live-thread "$ws_ref" --wait "$wait" 2>/dev/null || true
}

record_workstream_launch_binding() {
    local ws_ref="$1" slug="$2" thread_id="$3" reason=""
    # Failure must not turn a delivered seed into a retryable send failure,
    # but the reason (thread-unknown vs session-changed) must reach the log.
    reason=$(python3 "$REPO_ROOT/system/scripts/workstream_thread_bindings.py" \
        --root "$REPO_ROOT" record-workspace "$ws_ref" "$slug" "$thread_id" 2>&1) || \
        log_warn "Launcher identity for $ws_ref ($slug) could not be recorded: ${reason#Workstream binding not recorded: }"
}

# Launch-argument seeding (Claude Code). Typing a seed into a fresh Claude
# composer races the TUI and can leave the handoff typed but unsubmitted.
# A caller that has already written its launch prompt file sets
# SWITCHBOARD_LAUNCH_SEED_FILE=<file> (plus SWITCHBOARD_LAUNCH_SEED_HYDRATION=<label> for
# a pure startup seed) for prepare_workspace. The Claude launch or resume
# command then carries "Read and follow all instructions in <file>" as its
# positional prompt, so the seed is the session's first turn and nothing is
# typed. prepare_workspace runs in a command substitution, so the boot leaves
# a marker that send_prompt_via_file consumes instead of typing. Amp, a live
# preserved session, and a caller without the variable keep the guarded send.
launch_seed_pointer() {
    [[ "$(switchboard_agent)" == "claude" ]] || return 1
    local f="${SWITCHBOARD_LAUNCH_SEED_FILE:-}"
    [[ -n "$f" && -f "$f" ]] || return 1
    printf 'Read and follow all instructions in %s' "$f"
}

launch_seed_marker() {
    printf '%s/system/state/launch-seeded/%s' "$REPO_ROOT" "${1//[^A-Za-z0-9]/-}"
}

# 0 when <ws_ref> has a launch-seed marker younger than LAUNCH_SEED_MARKER_TTL
# seconds (default 600: the send follows the boot, but cmux status calls can
# stall under start-day fan-out; a live routine launch spent 51 s there).
# A stale marker is debris from a launcher that aborted; it is removed.
launch_seed_marker_fresh() {
    local marker at file
    marker=$(launch_seed_marker "$1")
    [[ -f "$marker" ]] || return 1
    read -r at file < "$marker" || true
    if [[ "$at" =~ ^[0-9]+$ ]] && (( $(date +%s) - at <= ${LAUNCH_SEED_MARKER_TTL:-600} )); then
        return 0
    fi
    rm -f "$marker"
    return 1
}

# 0 when the boot of <ws_ref> carried <prompt_file> as its launch argument
# (fresh marker, same file). Consumes the marker either way.
consume_launch_seed() {
    local ws_ref="$1" prompt_file="$2" marker at file
    launch_seed_marker_fresh "$ws_ref" || return 1
    marker=$(launch_seed_marker "$ws_ref")
    read -r at file < "$marker" || true
    rm -f "$marker"
    [[ "$file" == "$prompt_file" ]]
}

# Tell Claude to read and follow instructions from a file.
#
# Startup-turn race: local_amp_launch_command
# pipes a "wait for instructions" prompt into `exec amp`, and wait_for_amp_ready
# returns at prompt render — while that startup turn is still running. A raw
# send here lands the pointer as a STEER of the running turn, so the
# task-liveness plugin never sees an agent.start for it and the worker's
# ledger ack only arrives from the throttled tool.result scan — after the
# worker's first claim, which then fails worker-identity-unverified for every
# fresh worker. router-dispatch.sh fixed the same race with cig_safe_send.
# Wait for the box to be positively idle (bounded), then send once. Returns
# 0 = sent, 2 = deferred (never idle within the budget; nothing typed),
# 3 = transport attempted, outcome unknown (never retry).
send_prompt_via_file() {
    local ws_ref="$1"
    local prompt_file="$2"
    local max_wait="${3:-${LAUNCH_PROMPT_SEND_WAIT_SECONDS:-90}}"

    if [[ ! -f "$prompt_file" ]]; then
        log_error "Prompt file not found: $prompt_file"
        return 1
    fi

    local pointer="Read and follow all instructions in $prompt_file"
    local seed_slug="" seed_thread="" send_rc=0 verify_rc=0
    if [[ "$prompt_file" == "$REPO_ROOT/system/state/launch-prompts/"*.md ]]; then
        seed_slug="${prompt_file##*/}"
        seed_slug="${seed_slug%.md}"
    fi
    if consume_launch_seed "$ws_ref" "$prompt_file"; then
        # The launch command carried this pointer; Claude submitted it as the
        # first turn. Only a screen that is readable, idle, empty and without
        # the pointer proves it never arrived; then fall back to typing.
        # Claude may not have echoed the prompt yet: rc 2 must hold on three
        # reads about a second apart before the pointer is typed again.
        local attempt
        for attempt in 1 2 3; do
            verify_rc=0
            declare -F cig_verify_sent >/dev/null 2>&1 && { cig_verify_sent "$ws_ref" "$pointer" || verify_rc=$?; }
            (( verify_rc != 2 )) && break
            sleep "${LAUNCH_SEED_VERIFY_GAP:-1}"
        done
        if (( verify_rc != 2 )); then
            declare -F cig_log_send >/dev/null 2>&1 && cig_log_send "$ws_ref" "$pointer" "launch-arg"
            [[ -n "$seed_slug" ]] && record_workstream_launch_binding "$ws_ref" "$seed_slug" \
                "$(amp_thread_for_launched_workspace "$ws_ref")"
            return 0
        fi
        log_warn "Launch-argument seed for $ws_ref is not on screen; sending the pointer through the guard"
    fi
    if [[ -n "$seed_slug" ]]; then
        # Capture the seeded thread BEFORE the send so record_workspace can
        # verify the same session received it; wait for the store to learn it.
        seed_thread=$(amp_thread_for_launched_workspace "$ws_ref")
    fi
    if declare -F cig_safe_send >/dev/null 2>&1; then
        cig_safe_send "$ws_ref" "$pointer" "$max_wait" 3 || send_rc=$?
        if (( send_rc == 2 )); then
            log_warn "Launch prompt for $ws_ref deferred: startup turn still busy after ${max_wait}s (nothing typed)"
        elif (( send_rc == 3 )); then
            log_error "Launch prompt send to $ws_ref attempted but cmux failed; outcome unknown (no retry)"
        fi
    else
        send_to_workspace "$ws_ref" "$pointer" || send_rc=$?
    fi
    if (( send_rc == 0 )) && [[ -n "$seed_slug" ]]; then
        record_workstream_launch_binding "$ws_ref" "$seed_slug" "$seed_thread"
    fi
    return "$send_rc"
}

# Send prompt text directly inline to the workspace.
send_prompt_text() {
    local ws_ref="$1"
    local prompt_text="$2"
    local label="${3:-prompt}"

    send_to_workspace "$ws_ref" "$prompt_text"
}

write_launch_prompt_file() {
    local name="$1"
    local prompt_text="$2"
    local prompts_dir="$REPO_ROOT/system/state/launch-prompts"
    local safe_name
    local prompt_file

    mkdir -p "$prompts_dir"
    safe_name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')
    prompt_file="$prompts_dir/${safe_name:-workspace}.md"
    printf '%s\n' "$prompt_text" > "$prompt_file"
    echo "$prompt_file"
}

# --- One-message seeding + durable launch-result contract ------
# When CoS routes an ask (route-to-workspace.sh), the composed handoff message
# rides in CMUX_LAUNCH_HANDOFF. The launch CLI decides ONE thing and reports it
# durably: was the workspace FRESH (created this invocation) or REUSED (already
# live)? Fresh -> the handoff is folded into the single launch prompt (the
# session starts on the actual ask; no queued second steer). Reused -> the
# redundant startup resend is skipped and the caller delivers ONLY the guarded
# handoff send into the live session. The decision lands in
# system/state/launch-results/<name>.json so the caller (and CoS) never has to
# guess which case happened.

launch_result_file_for() {
    local name="$1"
    local safe_name
    safe_name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')
    echo "$REPO_ROOT/system/state/launch-results/${safe_name:-workspace}.json"
}

# write_launch_result_file <name> <mode:fresh|reused> <ws_ref> \
#     <handoff_delivery:inline|separate|none> <startup_prompt:sent|deferred|skipped> \
#     [thread_id] [recovery] [thread_provenance]
# thread_id: the Amp thread bound to the workspace (from the cmux session
# store) so CoS can inspect/resume the exact session later. recovery: how the
# launch reached its target — "" (normal), "live-session-reused" (live Amp
# session preserved in place), or "recovered-after-failure" (route-recovery
# delivered into an existing thread after a launch failure).
# thread_provenance: how the launcher cleared (or failed to clear) the tab's
# session for a separate handoff send (fail-closed routing invariant):
#   verified         gate PROVED the live thread belongs to this workstream
#   fresh-session    launcher itself booted a brand-new Amp this invocation
#   no-live-session  launcher positively confirmed NO live Amp in the tab
#   unseeded|unverified|unknown  gate ran and could NOT prove ownership
#   "" (None)        never probed (inline delivery, or legacy result)
# route-to-workspace.sh REFUSES the separate handoff send unless provenance
# is verified, fresh-session, or no-live-session, and only surfaces thread_id
# when it is exactly verified.
write_launch_result_file() (
    local name="$1" mode="$2" ws_ref="$3" handoff_delivery="$4" startup_prompt="$5"
    local thread_id="${6:-}" recovery="${7:-}" thread_provenance="${8:-}"
    local result_file result_tmp
    result_file=$(launch_result_file_for "$name")
    mkdir -p "$(dirname "$result_file")"
    result_tmp=$(mktemp "${result_file}.tmp.XXXXXX") || return 1
    trap '[[ -z "${result_tmp:-}" ]] || rm -f "$result_tmp"' EXIT
    if ! python3 - "$name" "$mode" "$ws_ref" "$handoff_delivery" "$startup_prompt" "$thread_id" "$recovery" "$thread_provenance" > "$result_tmp" <<'PY'
import datetime, json, sys
name, mode, ws_ref, handoff_delivery, startup_prompt, thread_id, recovery, thread_provenance = sys.argv[1:9]
print(json.dumps({
    "target": name,
    "workspace_mode": mode,                # fresh | reused
    "workspace_ref": ws_ref or None,
    "handoff_delivery": handoff_delivery,  # inline | separate | none
    "startup_prompt": startup_prompt,      # sent | deferred | skipped | blocked-unverified-thread
    "thread_id": thread_id or None,
    "recovery": recovery or None,          # live-session-reused | recovered-after-failure
    "thread_provenance": thread_provenance or None,  # verified | fresh-session | no-live-session | unseeded | unverified | unknown
    "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
}))
PY
    then
        rm -f "$result_tmp"
        return 1
    fi
    mv -f "$result_tmp" "$result_file" || {
        rm -f "$result_tmp"
        return 1
    }
    result_tmp=""
)

# read_launch_result_field <result_file> <field> -> prints value, rc=1 if absent
read_launch_result_field() {
    local result_file="$1" field="$2"
    [[ -f "$result_file" ]] || return 1
    python3 - "$result_file" "$field" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        value = json.load(fh).get(sys.argv[2])
except Exception:
    sys.exit(1)
if value is None:
    sys.exit(1)
print(value)
PY
}

# --- Startup-hydration provenance (P0 lifecycle) ------------------
# A startup/hydration prompt (workstream context re-seed, P0 header, reminder
# card) is machine-generated: its turn completing is NOT reviewable work, and
# its arrival is NOT the owner responding. The review-lane amp plugin must tell
# these turns apart from routed asks and real user turns, so every hydration
# seed drops a marker file keyed by the cmux workspace UUID (the ID the plugin
# sees in CMUX_WORKSPACE_ID). The plugin consumes the marker: it skips the
# "release review pin" on that turn's agent.start (existing review state
# survives restart) and skips the "pin to review" on its agent.end (a hydrated
# P0 stays in the P0 lane) — unless the turn emitted an explicitly
# review-worthy workspace update. Routed handoffs never mark hydration:
# fresh+inline folds the ask into the launch prompt, and separate delivery
# calls clear_startup_hydration first, so substantive turns pin review.
#
# Contract rules for callers:
#   - mark_startup_hydration BEFORE the send (agent.start fires within
#     milliseconds of the keystrokes landing; writing after the send races).
#   - clear_startup_hydration when the send fails or is deferred, so a stale
#     marker cannot suppress the review pin of a later real turn.
#   - never mark when the message carries a CoS handoff / routed ask.
review_lane_state_dir() {
    echo "${REVIEW_LANE_STATE_DIR:-$HOME/.config/amp/plugin-state/review-lane}"
}

# workspace_uuid_for_ref <workspace:N> -> prints the workspace UUID, rc=1 if
# unresolved. Uses the same `--id-format both` listing that
# publish-workspace-status.sh uses for the reverse (UUID -> ref) mapping.
workspace_uuid_for_ref() {
    local ws_ref="$1"
    local listing uuid
    [[ -z "$ws_ref" ]] && return 1
    listing=$(CMUX_QUIET=1 cmux_cmd list-workspaces --id-format both 2>/dev/null || true)
    uuid=$(printf '%s\n' "$listing" | awk -v ref="$ws_ref" '
        $1 == ref { print $2; exit }
        $1 == "*" && $2 == ref { print $3; exit }
    ')
    if printf '%s' "$uuid" | grep -qiE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
        echo "$uuid"
        return 0
    fi
    return 1
}

# mark_startup_hydration <ws_ref> [source-label]
mark_startup_hydration() {
    local ws_ref="$1" source_label="${2:-startup}"
    local uuid dir
    # A launch-argument seed turn already started; its marker was written at
    # boot. A second marker now would govern the NEXT turn instead.
    launch_seed_marker_fresh "$ws_ref" && return 0
    if ! uuid=$(workspace_uuid_for_ref "$ws_ref"); then
        log_warn "Could not resolve workspace UUID for $ws_ref; hydration marker skipped (its turn may pin To Review)"
        return 1
    fi
    dir=$(review_lane_state_dir)
    mkdir -p "$dir"
    printf '{"at": %s, "source": "%s", "workspace_ref": "%s"}\n' \
        "$(( $(date +%s) * 1000 ))" "$source_label" "$ws_ref" \
        > "$dir/hydration-$uuid.json"
}

# clear_startup_hydration <ws_ref> — safe no-op when no marker exists.
clear_startup_hydration() {
    local ws_ref="$1"
    local uuid
    uuid=$(workspace_uuid_for_ref "$ws_ref" 2>/dev/null) || return 0
    rm -f "$(review_lane_state_dir)/hydration-$uuid.json"
}

# Fold a CoS handoff into a launch prompt as a trailing section so a fresh
# session receives startup instructions + the actual ask in ONE message.
compose_launch_prompt_with_handoff() {
    local prompt_text="$1" handoff_text="$2"
    if [[ -z "$handoff_text" ]]; then
        printf '%s' "$prompt_text"
        return 0
    fi
    printf '%s\n\n%s\n' "$prompt_text" "$handoff_text"
}

# Run a shell command in a workspace and wait for the shell prompt to return.
# Usage: run_shell_in_workspace <ws_ref> <command> [timeout_seconds]
run_shell_in_workspace() {
    local ws_ref="$1"
    local cmd="$2"
    local timeout="${3:-120}"
    local elapsed=0

    log_info "Running in $ws_ref: $cmd"
    send_to_workspace "$ws_ref" "$cmd"

    local sf_ref
    sf_ref=$(get_workspace_surface "$ws_ref" || true)
    if [[ -z "$sf_ref" ]]; then
        log_warn "No surface for $ws_ref, sleeping 10s as fallback"
        sleep 10
        return 0
    fi

    while (( elapsed < timeout )); do
        sleep 2
        (( elapsed += 2 ))
        local screen
        screen=$(cmux_cmd read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 3 2>/dev/null || true)
        # Shell prompt returned (zsh % or bash $)
        if echo "$screen" | grep -qE '[\$%] *$'; then
            log_ok "Command completed (${elapsed}s)"
            return 0
        fi
    done

    log_warn "Command may not have finished (timeout: ${timeout}s)"
    return 0
}

# Routers route with local scripts and do not need MCP connections. Copy the
# current user settings at each launch so safety settings are never replaced
# by a minimal/stale profile. Do not change settings shared by owner workers.
amp_router_settings_file() {
    python3 - <<'PY'
import json, os, tempfile
from pathlib import Path
source = Path(os.environ.get("AMP_SETTINGS_FILE") or Path.home() / ".config/amp/settings.json")
settings = json.loads(source.read_text())  # Fail closed on missing/invalid settings.
settings["amp.mcpPermissions"] = [
    {"matches": {"command": "*"}, "action": "reject"},
    {"matches": {"url": "*"}, "action": "reject"},
] + settings.get("amp.mcpPermissions", [])
target = Path.home() / ".config/amp/switchboard-router-settings.json"
target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".router-settings-", dir=target.parent)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, target)  # mkstemp creates mode 0600; never print secrets.
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(target)
PY
}

# Build an unambiguous local Amp launch command. A bare `amp` can restore the
# TUI's previous thread, while `amp threads new` followed by `continue` can bind
# the empty thread to the repository's Orb executor. Amp's documented
# `prompt | amp` path creates a new interactive thread on the current machine.
# Never use `amp -o`, `amp -ox`, `threads new`, or "thread: new in orb" here.
local_amp_launch_command() {
    local cwd="$1"
    local agent_mode="${2:-ultra}"
    local slug="${3:-}"
    local surface="${3:-}"   # the surface name, kept: slug is rewritten from cwd below
    local quoted_cwd
    local quoted_prompt
    local quoted_slug
    local quoted_mode
    if [[ "$cwd" == "$REPO_ROOT/workstreams/"* ]]; then
        slug="${cwd#"$REPO_ROOT/workstreams/"}"
        slug="${slug%%/*}"
    fi
    [[ -f "$REPO_ROOT/workstreams/$slug/CONTEXT.md" ]] || slug=""
    local requested_mode="$agent_mode"   # Claude maps medium/low to sonnet; Amp coerces them below
    case "$agent_mode" in
        high|ultra) ;;
        low|medium) agent_mode="ultra" ;;  # compatibility: medium/low never reach Amp; default ultra
        *) agent_mode="ultra" ;;           # default: ultra for anything unrecognized
    esac
    quoted_cwd=$(printf '%q' "$cwd")
    quoted_prompt=$(printf '%q' "This is a local-only switchboard workstream session. Stay on this machine and wait for the workstream instructions that follow. Do not use an Amp Orb, a remote VM, SSH, cloud execution, or remote subagent.")
    quoted_slug=$(printf '%q' "$slug")
    quoted_mode=$(printf '%q' "$agent_mode")

    if [[ "$(switchboard_agent)" == "claude" ]]; then
        # Claude Code: the seed prompt is a positional argument (stdin would be
        # print mode), the Amp mode maps to --model/--effort, and permission
        # prompts are skipped because the tab runs unattended (the switchboard
        # guardrails still run as a Claude plugin hook). The contextual Amp
        # tab launcher (thread-binding observer) has no Claude equivalent.
        local model effort seed
        read -r model effort <<<"$(claude_mode_flags "$requested_mode")"
        seed=$(launch_seed_pointer) && quoted_prompt=$(printf '%q' "$seed")
        # CoS only: --remote-control CoS (cos_remote_control_args). The name is always
        # explicit, so the optional flag value never swallows the seed prompt.
        printf 'cd %s && %sSWITCHBOARD_WORKSTREAM_SLUG=%s exec %s --dangerously-skip-permissions --model %s --effort %s --settings %q%s %s' \
            "$quoted_cwd" "$(cos_remote_control_env "$surface")" "$quoted_slug" "$CLAUDE_CMD" "$model" "$effort" \
            "$(claude_tab_settings)" "$(cos_remote_control_args "$surface")" "$quoted_prompt"
        return 0
    fi

    if [[ -n "$slug" ]]; then
        printf 'cd %s && echo %s | SWITCHBOARD_WORKSTREAM_SLUG=%s exec python3 %s --root %s --launch-owner %s -- %s -m %s' \
            "$quoted_cwd" "$quoted_prompt" "$quoted_slug" \
            "$(printf '%q' "$REPO_ROOT/system/scripts/cmux_new_contextual_amp_tab.py")" \
            "$(printf '%q' "$REPO_ROOT")" "$quoted_slug" "$AMP_CMD" "$quoted_mode"
    else
        printf 'cd %s && echo %s | SWITCHBOARD_WORKSTREAM_SLUG=%s exec %s -m %s' \
            "$quoted_cwd" "$quoted_prompt" "$quoted_slug" "$AMP_CMD" "$quoted_mode"
    fi
}

# Resume an EXISTING local Amp thread instead of creating a new one. Used when
# a reused workspace's session is gone (crashed TUI, or queued /exit
# keystrokes that ended the thread) but the cmux session store still binds the tab to a thread
# that PROVABLY belongs to this workstream. `amp threads continue <id>`
# reopens that exact thread on this machine, preserving its history and
# avoiding a duplicate thread. Same local-only and mode-normalization
# guarantees as local_amp_launch_command.
local_amp_resume_command() {
    local cwd="$1"
    local agent_mode="${2:-ultra}"
    local thread_id="$3"
    local slug=""
    if [[ "$cwd" == "$REPO_ROOT/workstreams/"* ]]; then
        slug="${cwd#"$REPO_ROOT/workstreams/"}"
        slug="${slug%%/*}"
    fi
    case "$agent_mode" in
        high|ultra) ;;
        low|medium) agent_mode="ultra" ;;  # compatibility: medium/low never reach Amp; default ultra
        *) agent_mode="ultra" ;;
    esac
    printf 'cd %s && SWITCHBOARD_WORKSTREAM_SLUG=%s exec %s threads continue %s -m %s' \
        "$(printf '%q' "$cwd")" "$(printf '%q' "$slug")" "$AMP_CMD" \
        "$(printf '%q' "$thread_id")" "$(printf '%q' "$agent_mode")"
}

# Claude Code twin of local_amp_resume_command: `claude --resume <session
# uuid>` reopens the exact session the cmux store bound to the tab, with the
# same mode flags and unattended-permission posture as local_amp_launch_command.
# Only call it with a session id that thread_provenance_for_workstream
# verified for this workstream.
local_claude_resume_command() {
    local cwd="$1"
    local agent_mode="${2:-ultra}"
    local session_id="$3"
    local surface="${4:-}"
    local slug=""
    if [[ "$cwd" == "$REPO_ROOT/workstreams/"* ]]; then
        slug="${cwd#"$REPO_ROOT/workstreams/"}"
        slug="${slug%%/*}"
    fi
    local model effort seed
    read -r model effort <<<"$(claude_mode_flags "$agent_mode")"
    printf 'cd %s && %sSWITCHBOARD_WORKSTREAM_SLUG=%s exec %s --resume %s --dangerously-skip-permissions --model %s --effort %s --settings %q%s' \
        "$(printf '%q' "$cwd")" "$(cos_remote_control_env "$surface")" "$(printf '%q' "$slug")" "$CLAUDE_CMD" \
        "$(printf '%q' "$session_id")" "$model" "$effort" "$(claude_tab_settings)" \
        "$(cos_remote_control_args "$surface")"
    # `claude --resume <id> "<prompt>"` submits the prompt as the next turn.
    if seed=$(launch_seed_pointer); then printf ' %q' "$seed"; fi
}

# Resume command for whichever agent switchboard_agent selects.
local_agent_resume_command() {
    if [[ "$(switchboard_agent)" == "claude" ]]; then
        local_claude_resume_command "$@"
    else
        local_amp_resume_command "$@"
    fi
}

# Full lifecycle: find/create workspace, reset surface, launch Amp, wait for ready.
# Prints workspace ref on success.
# Boot Amp inside a workspace surface. Returns 0 and prints the ws_ref on
# success. Levels of defense against the transient cmux surface bug:
#   1. hard-wait for a READABLE terminal (read-screen ok), not just a surface
#      that shows up in `tree` — a cold surface silently drops keystrokes.
#   2. wait a second bounded interval without re-sending uncertain input.
#   3. as a last resort, and ONLY for a workspace this invocation just created,
#      close + recreate it once and retry. Reused workspaces (which may hold
#      real Amp state / scrollback) are never destroyed.
_boot_amp_in_workspace() {
    local ws_ref="$1"
    local name="$2"
    local cwd="$3"
    local is_fresh="${4:-false}"   # "true" when this workspace was just created

    set_workspace_status "$ws_ref" "status" "booting" "" "#FFA500"

    # Hard-wait for the terminal SURFACE to attach (visible in `tree`). That is
    # the correct "can I send input" signal: a fresh surface accepts keystrokes
    # even before it has rendered anything. We deliberately do NOT gate on
    # read-screen succeeding — an empty terminal returns "Failed to read
    # terminal text" until it has output, yet still receives input fine
    # (verified), so a read-screen gate produces false-negative launch failures.
    # We must never send into a surface-LESS workspace, though, so this wait is
    # hard (was previously `|| true`, which masked contention and was a root
    # cause of "No surface found / Amp failed to start").
    # CMUX_SURFACE_WAIT_SECONDS is a test seam only (default 45); production
    # callers never set it.
    local surface_wait="${CMUX_SURFACE_WAIT_SECONDS:-45}"
    if ! wait_for_workspace_surface "$ws_ref" "$surface_wait"; then
        log_error "No terminal surface for $ws_ref after ${surface_wait}s; not sending launch into a surface-less workspace"
        return 1
    fi

    # Brief settle so the shell's initial prompt render finishes before we
    # launch, reducing the chance the first keystrokes race the PTY.
    sleep 1

    # Only reset a REUSED workspace (it may hold a dead TUI or stray shell
    # command). Never reset a freshly-created one: reset sends ctrl+c into the
    # cold shell, and cmux tears the workspace down when its only surface's
    # shell takes a SIGINT before it finishes initializing. That
    # ctrl+c-kills-fresh-workspace behavior was the real "workspace failed on
    # launch" bug seen during start-day fan-out — a brand-new workspace has
    # nothing to reset anyway.
    local resume_thread_id=""
    if [[ "$is_fresh" != "true" ]]; then
        local reset_rc=0
        reset_workspace_surface "$ws_ref" || reset_rc=$?
        if (( reset_rc == 3 )); then
            # Live-or-unverifiable Amp session preserved; NOTHING was typed.
            # Propagate distinctly so the
            # caller can reuse the session in place instead of failing.
            return 3
        elif (( reset_rc != 0 )); then
            return 1
        fi
        # The surface is clean, but the tab may still be bound to a PRIOR
        # thread in the cmux session store (crashed TUI, or a /exit processed
        # after the fact). Resume that exact thread IFF provenance proves it
        # belongs to this workstream — never resume a foreign or unproven
        # thread, and never let resume create a duplicate.
        # Router workspaces intentionally start a new Amp thread for every
        # request. Their cmux workspace stays warm, but prior conversation
        # history must never enter a later ownership decision.
        # The same store binds Claude Code tabs (kind "claude", the session
        # UUID as resumeBinding.checkpointId, written by cmux's Claude hooks),
        # so workspace-session-state.py returns that UUID as thread_id and
        # `claude --resume` reopens it. The id must match the configured
        # agent's shape: an Amp tab is never resumed by Claude or vice versa.
        if [[ "${SWITCHBOARD_FORCE_FRESH_AMP_THREAD:-false}" != "true" ]]; then
            local prior_thread prior_provenance
            prior_thread=$(amp_thread_for_workspace "$ws_ref")
            if [[ "$prior_thread" =~ $(agent_session_id_re) ]]; then
                prior_provenance=$(thread_provenance_for_workstream "$prior_thread" "$name") || true
                if [[ "$prior_provenance" == "verified" ]]; then
                    resume_thread_id="$prior_thread"
                    log_info "Resuming verified prior session $prior_thread for $name (no duplicate session)"
                else
                    log_info "Prior session $prior_thread in $ws_ref has ${prior_provenance:-unknown} provenance for $name; launching a fresh session instead of resuming"
                fi
            elif [[ -n "$prior_thread" ]]; then
                log_info "Prior session $prior_thread in $ws_ref belongs to another agent; launching a fresh $(agent_display_name) session"
            fi
        fi
    fi

    local agent_mode agent_name
    agent_mode=$(resolve_agent_mode "$name")
    agent_name=$(agent_display_name)
    local launch_command
    if [[ -n "$resume_thread_id" ]]; then
        log_info "Launching $agent_name in: $name (agent mode: $agent_mode, resuming $resume_thread_id)"
        launch_command=$(local_agent_resume_command "$cwd" "$agent_mode" "$resume_thread_id" "$name")
    else
        log_info "Launching $agent_name in: $name (agent mode: $agent_mode)"
        launch_command=$(local_amp_launch_command "$cwd" "$agent_mode" "$name")
    fi
    # The router settings file holds Amp settings; Claude Code has no reader for it.
    if [[ "$name" == "router" && "$(switchboard_agent)" != "claude" ]]; then
        local router_settings
        router_settings=$(amp_router_settings_file) || return 1
        launch_command+=" --settings-file $(printf '%q' "$router_settings")"
    fi
    # Launch-argument seed (see launch_seed_pointer): the first turn starts as
    # soon as Claude boots, so its hydration marker must exist before launch.
    local seeded_file=""
    if launch_seed_pointer >/dev/null; then
        seeded_file="$SWITCHBOARD_LAUNCH_SEED_FILE"
        rm -f "$(launch_seed_marker "$ws_ref")"
        [[ -n "${SWITCHBOARD_LAUNCH_SEED_HYDRATION:-}" ]] && \
            { mark_startup_hydration "$ws_ref" "$SWITCHBOARD_LAUNCH_SEED_HYDRATION" || true; }
        log_info "Seeding $name through the launch argument: $seeded_file"
    fi
    send_to_workspace "$ws_ref" "$launch_command" || return 1

    # Launch verification: the same dispatch.py check worker
    # tabs get. Amp on screen within 30 s is success; a bare shell prompt
    # (command consumed) gets the launch retyped exactly once; an unknown screen
    # is never typed into and
    # falls through to the readiness waits below, which also never resend.
    local verify_rc=0
    verify_amp_launch "$ws_ref" "$name" "$launch_command" || verify_rc=$?
    if (( verify_rc == 0 )); then
        sleep 1
        if workspace_amp_is_orb "$ws_ref"; then
            log_error "Amp opened in an Orb for $name; refusing to send any workstream prompt"
            return 2
        fi
        log_ok "$agent_name ready in: $name"
        _record_launch_seed "$ws_ref" "$seeded_file"
        return 0
    elif (( verify_rc == 1 )); then
        return 1
    fi

    if wait_for_amp_ready "$ws_ref"; then
        sleep 1
        if workspace_amp_is_orb "$ws_ref"; then
            log_error "Amp opened in an Orb for $name; refusing to send any workstream prompt"
            return 2
        fi
        log_ok "$agent_name ready in: $name"
        _record_launch_seed "$ws_ref" "$seeded_file"
        return 0
    fi

    # A slow launch can reach Amp after the first wait expires. Re-sending
    # shell text would then insert it into Amp's input box (or submit it).
    log_warn "$agent_name not ready in $name; waiting once more without re-sending launch"
    if wait_for_amp_ready "$ws_ref"; then
        sleep 1
        if workspace_amp_is_orb "$ws_ref"; then
            log_error "Amp opened in an Orb for $name after extended wait; refusing to continue"
            return 2
        fi
        log_ok "$agent_name ready in: $name (after extended wait)"
        _record_launch_seed "$ws_ref" "$seeded_file"
        return 0
    fi

    return 1
}

# Leave the launch-seed marker send_prompt_via_file consumes (no-op unseeded).
_record_launch_seed() {
    local ws_ref="$1" file="$2" marker
    [[ -n "$file" ]] || return 0
    marker=$(launch_seed_marker "$ws_ref")
    mkdir -p "${marker%/*}" 2>/dev/null && printf '%s %s\n' "$(date +%s)" "$file" > "$marker"
    return 0
}

prepare_workspace() {
    local name="$1"
    local cwd="${2:-$REPO_ROOT}"
    local ws_ref
    local was_created result

    result=$(find_or_create_workspace "$name" "$cwd" true) || return 1
    ws_ref="${result%%$'\t'*}"
    was_created="${result#*$'\t'}"
    [[ "$ws_ref" == workspace:* && ( "$was_created" == true || "$was_created" == false ) ]] || return 1

    local boot_rc=0
    _boot_amp_in_workspace "$ws_ref" "$name" "$cwd" "$was_created" || boot_rc=$?
    if (( boot_rc == 0 )); then
        set_workspace_status "$ws_ref" "status" "ready" "" "#00CC00"
        echo "$ws_ref"
        return 0
    fi

    if (( boot_rc == 3 )); then
        # Live-or-unverifiable Amp session in a reused tab; NOTHING was typed
        # into it. Never recreate — the
        # session is real state. Report the ref and rc 3 so the caller can
        # reuse/verify the live session instead of treating this as a failure.
        set_workspace_status "$ws_ref" "status" "live-preserved" "" "#4A90D9"
        log_warn "Live Amp session preserved in $ws_ref for $name; launcher typed nothing (caller decides reuse vs fail-closed)"
        echo "$ws_ref"
        return 3
    fi

    # A seeded boot marked hydration before the launch; a failed boot must not
    # leave that marker to govern a later turn in this tab.
    if [[ -n "${SWITCHBOARD_LAUNCH_SEED_HYDRATION:-}" ]] && launch_seed_pointer >/dev/null; then
        clear_startup_hydration "$ws_ref"
    fi

    if (( boot_rc == 2 )); then
        set_workspace_status "$ws_ref" "status" "blocked-orb" "" "#FF0000"
        log_error "Blocked Orb-backed launch for $name; not recreating or seeding the workspace"
        return 1
    fi

    # Readiness failure does not prove an empty workspace: a slow launch or a
    # concurrent caller may already own it. Preserve it for explicit recovery.
    set_workspace_status "$ws_ref" "status" "failed" "" "#FF0000"
    log_error "Amp failed to start in: $name"
    return 1
}

# --- Standalone CLI ---
# When invoked directly: bash cmux-helpers.sh launch <workstream-name>
# Explicit user requests to "open" or "reopen" a workstream should call this
# launcher so the live cmux workspace exists, instead of only restating the
# workstream context in chat.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "${1:-}" in
        launch)
            target="${2:?Usage: cmux-helpers.sh launch <workstream-name>}"
            ws_name="$target"
            source_type=""

            if [[ "$target" == routine:* ]]; then
                source_type="routine"
                ws_name="${target#routine:}"
            elif [[ "$target" == workstream:* ]]; then
                source_type="workstream"
                ws_name="${target#workstream:}"
            fi

            # Machine-local off switch (system/scripts/comms_triage_setting.py).
            if [[ "$ws_name" == "comms-triage" && "$source_type" != "workstream" ]] && ! switchboard_comms_triage_enabled; then
                log_info "Comms Triage is off on this machine (SWITCHBOARD_COMMS_TRIAGE=off); not opening it"
                exit 0
            fi

            # Check workstreams/ first, then routines/
            if [[ "$source_type" == "workstream" ]]; then
                if [[ -d "$REPO_ROOT/workstreams/$ws_name" ]] && [[ -f "$REPO_ROOT/workstreams/$ws_name/CONTEXT.md" ]]; then
                    ws_dir="$REPO_ROOT/workstreams/$ws_name"
                else
                    log_error "Invalid workstream: $ws_name (missing CONTEXT.md in workstreams/)"
                    exit 1
                fi
            elif [[ "$source_type" == "routine" ]]; then
                if [[ -d "$REPO_ROOT/routines/$ws_name" ]] && [[ -f "$REPO_ROOT/routines/$ws_name/CONTEXT.md" ]]; then
                    ws_dir="$REPO_ROOT/routines/$ws_name"
                else
                    log_error "Invalid routine: $ws_name (missing CONTEXT.md in routines/)"
                    exit 1
                fi
            elif [[ -d "$REPO_ROOT/workstreams/$ws_name" ]] && [[ -f "$REPO_ROOT/workstreams/$ws_name/CONTEXT.md" ]]; then
                ws_dir="$REPO_ROOT/workstreams/$ws_name"
            elif [[ -d "$REPO_ROOT/routines/$ws_name" ]] && [[ -f "$REPO_ROOT/routines/$ws_name/CONTEXT.md" ]]; then
                ws_dir="$REPO_ROOT/routines/$ws_name"
            else
                log_error "Invalid workstream/routine: $ws_name (missing CONTEXT.md in workstreams/ or routines/)"
                exit 1
            fi

            # Default prompt (use relative path from repo root)
            ws_rel="${ws_dir#$REPO_ROOT/}"
            prompt="Read $ws_rel/CONTEXT.md and resume from the last known state. Identify open questions, pending decisions, and next concrete deliverables."

            # Override with config.yaml startup_instruction if available
            if [[ -f "$ws_dir/config.yaml" ]]; then
                startup=$(python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f)
v = d.get('startup_instruction', '')
if v:
    print(v)
" "$ws_dir/config.yaml" 2>/dev/null || true)
                [[ -n "$startup" ]] && prompt="$startup"
            fi

            if [[ "$ws_dir" == "$REPO_ROOT/workstreams/"* ]]; then
                source_key="workstream:$ws_name"
            else
                source_key="routine:$ws_name"
            fi

            # Preamble: skip AGENTS.md session-start, capture source links as durable workstream notes,
            # and emit updates back to the switchboard.
            preamble="$(workstream_launch_preamble "$source_key")"

            # Pre-launch: refresh any watched_docs declared in this workstream
            # so the workspace opens against the latest Google Doc snapshots.
            # Runs in the background; logs to system/logs/refresh-watched-docs.log.
            # Skipped for routines (workstreams/ only).
            if [[ "$source_key" == workstream:* ]]; then
                if grep -q "^watched_docs:" "$ws_dir/config.yaml" 2>/dev/null; then
                    log_info "Refreshing watched_docs for $ws_name (background)"
                    (
                        python3 "$REPO_ROOT/system/scripts/refresh-watched-docs.py" \
                            --workstream "$ws_name" >> "$REPO_ROOT/system/logs/refresh-watched-docs.log" 2>&1 || true
                    ) &
                fi
                # Receipt-bearing routes must reach their first local claim
                # before enrichment. Keep the last digest; normal launches and
                # the collectors refresh it outside the claim deadline.
                if [[ "${CMUX_SKIP_SIGNALS_BUILD:-0}" != "1" ]]; then
                    log_info "Building recent-signals digest for $ws_name"
                    python3 "$REPO_ROOT/system/scripts/build-workspace-signals.py" "$ws_name" \
                        >> "$REPO_ROOT/system/logs/workspace-signals.log" 2>&1 || true
                fi
            fi

            # Fresh-vs-reused is the durable contract with the caller:
            # probe once up front, report the outcome in the
            # launch-result file. CMUX_LAUNCH_HANDOFF (optional, set by
            # route-to-workspace.sh) is the composed CoS handoff message:
            # folded into the launch prompt when FRESH, delivered separately
            # by the caller when REUSED.
            launch_handoff="${CMUX_LAUNCH_HANDOFF:-}"
            if workspace_ref_if_exists "$ws_name" >/dev/null 2>&1; then
                launch_mode="reused"
            else
                launch_mode="fresh"
            fi

            # P0-linked workstream: the canonical space is the "🔥 P0 · <name>"
            # tab. If a tab already exists (P0-named or normal-named), REUSE it
            # without prepare_workspace — its surface reset would kill the live
            # session. Rename a normal tab into the P0 title, seed via guarded
            # send, and consolidate leftovers. Only a missing tab gets the full
            # fresh create + seed.
            if [[ "$source_key" == workstream:* ]] && workstream_has_active_p0 "$ws_name"; then
                canonical_name=$(p0_workspace_name_for_workstream "$ws_name")
                log_info "P0-linked workstream: canonical space is $canonical_name"
                prompt="$(p0_prompt_header_for_workstream "$ws_name")
$prompt"
                if ws_ref=$(workspace_ref_if_exists "$ws_name" 2>/dev/null); then
                    cur_name=$(workspace_name_for_ref_in_listing "$(cmux_cmd list-workspaces 2>/dev/null || true)" "$ws_ref" 2>/dev/null || true)
                    if [[ -n "$cur_name" && "$cur_name" != "$canonical_name" ]]; then
                        log_info "Renaming open tab into P0 space: $cur_name ($ws_ref) -> $canonical_name"
                        cmux_cmd rename-workspace --workspace "$ws_ref" "$canonical_name" &>/dev/null || \
                            log_warn "Failed to rename $ws_ref to: $canonical_name"
                    fi
                    mark_manually_opened_workstream "$ws_name"
                    # Thread-provenance gate: a live
                    # session in the P0 tab may be on a FOREIGN thread — the
                    # tab title proves nothing. Only type into it when the
                    # current thread provably belongs to this workstream.
                    # no-live-session: we positively checked and there is NO
                    # live Amp session in the tab — nothing to hijack, so a
                    # guarded send is permitted (cig_safe_send still defers on
                    # busy screens). Distinct from "" (never probed), which
                    # the caller fails closed on.
                    p0_thread_id=""
                    p0_provenance="no-live-session"
                    p0_gate_ok="true"
                    if workspace_has_live_amp "$ws_ref"; then
                        if workspace_thread_gate "$ws_ref" "$ws_name"; then
                            p0_thread_id="$WSG_THREAD_ID"
                        else
                            p0_gate_ok="false"
                            log_warn "Live session in P0 tab $ws_ref is on thread '${WSG_THREAD_ID:-unbound}' with $WSG_PROVENANCE provenance for $ws_name; refusing to type into it"
                        fi
                        p0_provenance="$WSG_PROVENANCE"
                    fi
                    if [[ -n "$launch_handoff" ]]; then
                        # Reused live tab + pending CoS handoff: skip the
                        # redundant startup resend — the caller's guarded
                        # handoff send is the single message into the session
                        # (the caller re-checks thread_provenance before it).
                        log_info "Reused P0 tab has a pending CoS handoff; skipping startup resend (caller delivers the handoff)"
                        startup_status="skipped"
                    elif [[ "$p0_gate_ok" != "true" ]]; then
                        startup_status="blocked-unverified-thread"
                    else
                        prompt_file=$(write_launch_prompt_file "$ws_name" "$preamble $prompt")
                        startup_status="sent"
                        # Pure startup re-seed (no routed ask): mark hydration so
                        # the review-lane plugin keeps this P0 in the P0 lane.
                        mark_startup_hydration "$ws_ref" "launch-p0-reused" || true
                        if declare -F cig_safe_send >/dev/null 2>&1; then
                            if ! cig_safe_send "$ws_ref" "Read and follow all instructions in $prompt_file" 90 3; then
                                log_warn "P0 workspace for $ws_name is busy; launch prompt deferred (tab already live)"
                                startup_status="deferred"
                            else
                                record_workstream_launch_binding "$ws_ref" "$ws_name" "$p0_thread_id"
                            fi
                        else
                            send_prompt_via_file "$ws_ref" "$prompt_file" || startup_status="deferred"
                        fi
                        [[ "$startup_status" != "sent" ]] && clear_startup_hydration "$ws_ref"
                    fi
                    consolidate_duplicate_workstream_tab "$ws_name" "$ws_ref"
                    write_launch_result_file "$ws_name" "reused" "$ws_ref" \
                        "$([[ -n "$launch_handoff" ]] && echo separate || echo none)" "$startup_status" \
                        "$p0_thread_id" "" "$p0_provenance"
                    log_ok "Reused canonical P0 workspace for: $ws_name ($ws_ref)"
                    arrange_workspace_sidebar
                    exit 0
                fi
            fi

            # Reused workspace with a LIVE local Amp session: reuse it in
            # place. prepare_workspace
            # would reset the surface — /exit-ing a healthy session and
            # failing outright when the TUI survives. That destroyed live
            # state and broke routing. The P0 path above already reuses live
            # tabs without reset; this generalizes the same invariant to every
            # reused workspace. The session store keeps the thread ID so CoS
            # can verify/resume the exact session later.
            if [[ "$launch_mode" == "reused" ]] && \
               ws_ref=$(workspace_ref_if_exists "$ws_name" 2>/dev/null) && \
               ! workspace_is_remote "$ws_ref" && \
               workspace_has_live_amp "$ws_ref"; then
                if workspace_amp_is_orb "$ws_ref"; then
                    log_error "Live Amp session in $ws_ref is Orb-backed; refusing to reuse or reset it"
                    exit 1
                fi
                log_info "Live Amp session detected in $ws_ref for $ws_name; reusing in place (no reset/relaunch)"
                if [[ "$source_key" == workstream:* ]]; then
                    mark_manually_opened_workstream "$ws_name"
                fi
                # Thread-provenance gate: the tab titled for this workstream may be hosting
                # a FOREIGN thread (another workstream's worker, a child
                # verification thread, or anything the owner opened there). Only a
                # thread with proven provenance may receive routed prompts;
                # otherwise report thread_id null and refuse to type into it.
                live_gate_ok="true"
                live_thread_id=""
                if workspace_thread_gate "$ws_ref" "$ws_name"; then
                    live_thread_id="$WSG_THREAD_ID"
                else
                    live_gate_ok="false"
                    log_warn "Live session in $ws_ref is on thread '${WSG_THREAD_ID:-unbound}' with $WSG_PROVENANCE provenance for $ws_name; refusing to type into it (fail-closed)"
                fi
                live_provenance="$WSG_PROVENANCE"
                if [[ -n "$launch_handoff" ]]; then
                    # Pending CoS handoff: the caller's guarded send is the
                    # single message into the live session (the caller
                    # re-checks thread_provenance and blocks on unverified).
                    log_info "Reused live session has a pending CoS handoff; skipping startup resend (caller delivers the handoff)"
                    write_launch_result_file "$ws_name" "reused" "$ws_ref" "separate" "skipped" \
                        "$live_thread_id" "live-session-reused" "$live_provenance"
                elif [[ "$live_gate_ok" != "true" ]]; then
                    write_launch_result_file "$ws_name" "reused" "$ws_ref" "none" "blocked-unverified-thread" \
                        "" "live-session-reused" "$live_provenance"
                else
                    prompt_file=$(write_launch_prompt_file "$ws_name" "$preamble $prompt")
                    startup_status="sent"
                    # Pure startup re-seed into a live session (no routed ask):
                    # mark hydration so the review-lane plugin neither releases
                    # an existing review pin nor pins this turn to review.
                    mark_startup_hydration "$ws_ref" "launch-live-reused" || true
                    if declare -F cig_safe_send >/dev/null 2>&1; then
                        if ! cig_safe_send "$ws_ref" "Read and follow all instructions in $prompt_file" 90 3; then
                            log_warn "Live workspace for $ws_name is busy; launch prompt deferred (session preserved)"
                            startup_status="deferred"
                        fi
                    else
                        send_prompt_via_file "$ws_ref" "$prompt_file" || startup_status="deferred"
                    fi
                    [[ "$startup_status" != "sent" ]] && clear_startup_hydration "$ws_ref"
                    write_launch_result_file "$ws_name" "reused" "$ws_ref" "none" "$startup_status" \
                        "$live_thread_id" "live-session-reused" "$live_provenance"
                fi
                consolidate_duplicate_workstream_tab "$ws_name" "$ws_ref"
                log_ok "Reused live workspace for: $ws_name ($ws_ref)"
                arrange_workspace_sidebar
                exit 0
            fi

            # Write the seed before the boot so a Claude launch carries it as
            # its first turn (launch_seed_pointer); Amp still gets it typed.
            seed_file="" seed_hydration=""
            if [[ -n "$launch_handoff" && "$launch_mode" == "fresh" ]]; then
                # One-message seeding: fold the CoS handoff into the launch
                # prompt so the fresh session starts on the actual ask — no
                # generic resume turn, no queued second steer.
                seed_file=$(write_launch_prompt_file "$ws_name" \
                    "$(compose_launch_prompt_with_handoff "$preamble $prompt" "$launch_handoff")")
            elif [[ -z "$launch_handoff" ]]; then
                seed_file=$(write_launch_prompt_file "$ws_name" "$preamble $prompt")
                seed_hydration="launch-startup-seed"
            fi
            prep_rc=0
            ws_ref=$(SWITCHBOARD_LAUNCH_SEED_FILE="$seed_file" SWITCHBOARD_LAUNCH_SEED_HYDRATION="$seed_hydration" \
                prepare_workspace "$ws_name" "$REPO_ROOT") || prep_rc=$?
            if (( prep_rc == 3 )); then
                # The live-reuse pre-check above said "no live session" but the
                # reset gate found one moments later (transient probe failure
                # or a race). NOTHING was typed into it. Fail closed with a
                # durable result; a re-run takes the live-reuse branch.
                log_error "Live/unverifiable Amp session surfaced mid-launch in ${ws_ref:-the tab} for $ws_name; failing closed (session preserved, nothing typed)"
                write_launch_result_file "$ws_name" "reused" "$ws_ref" \
                    "$([[ -n "$launch_handoff" ]] && echo separate || echo none)" \
                    "blocked-unverified-thread" "" "live-session-reused" "unknown"
                exit 1
            elif (( prep_rc != 0 )); then
                exit 1
            fi
            if [[ "$source_key" == workstream:* ]]; then
                mark_manually_opened_workstream "$ws_name"
            fi
            log_ok "Launched workstream: $ws_name"
            if [[ -n "$launch_handoff" && "$launch_mode" == "fresh" ]]; then
                prompt_file="$seed_file"
                # The pointer must be its own user turn (agent.start) so the
                # task-liveness plugin acks the worker before its first claim.
                # Record the real send outcome; never claim "sent" for a
                # deferred or unknown delivery.
                startup_status="sent"
                send_rc=0
                send_prompt_via_file "$ws_ref" "$prompt_file" || send_rc=$?
                case "$send_rc" in
                    0) ;;
                    2) startup_status="deferred" ;;
                    *) startup_status="send-outcome-unknown" ;;
                esac
                write_launch_result_file "$ws_name" "fresh" "$ws_ref" "inline" "$startup_status"
            elif [[ -n "$launch_handoff" ]]; then
                # Reused (non-P0) tab + pending CoS handoff: skip the redundant
                # startup resend; the caller's guarded handoff send is the
                # single message into the session. fresh-session provenance:
                # this path only runs when the tab had NO live Amp (the
                # live-reuse branch above short-circuits otherwise), so
                # prepare_workspace just booted a brand-new Amp session — the
                # launcher created it this invocation, there is no foreign
                # thread to hijack.
                log_info "Reused workspace has a pending CoS handoff; skipping startup resend (caller delivers the handoff)"
                # prepare_workspace created this local session. The caller owns
                # its first handoff; retain launcher identity even without a
                # separate startup pointer (never applies to live-session reuse).
                record_workstream_launch_binding "$ws_ref" "$ws_name" "$(amp_thread_for_launched_workspace "$ws_ref")"
                write_launch_result_file "$ws_name" "reused" "$ws_ref" "separate" "skipped" \
                    "" "" "fresh-session"
            else
                prompt_file="$seed_file"
                # Pure startup seed (no routed ask): mark hydration so the
                # review-lane plugin keeps this workspace out of To Review
                # when the machine-seeded turn finishes.
                mark_startup_hydration "$ws_ref" "launch-startup-seed" || true
                send_prompt_via_file "$ws_ref" "$prompt_file" || {
                    clear_startup_hydration "$ws_ref"
                    false
                }
                write_launch_result_file "$ws_name" "$launch_mode" "$ws_ref" "none" "sent"
            fi
            arrange_workspace_sidebar
            ;;
        *)
            echo "Usage: cmux-helpers.sh launch <workstream-name>"
            echo ""
            echo "Commands:"
            echo "  launch <name>   Open a workstream workspace and start Amp"
            exit 1
            ;;
    esac
fi
