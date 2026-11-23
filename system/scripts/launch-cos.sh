#!/usr/bin/env bash
# launch-cos.sh: open CoS in a new tab, Claude Code with Remote Control on, so the owner's
# phone reaches it.
#
#   bash system/scripts/launch-cos.sh                # open a CoS tab now
#   bash system/scripts/launch-cos.sh --if-missing   # only when no live CoS tab is open
#   bash system/scripts/launch-cos.sh --at-logon     # sign-in task: start wmux, let it restore, then --if-missing
#   bash system/scripts/launch-cos.sh --dry-run      # print the launch line, open nothing
#
# The tab runs what a hand-started CoS runs, plus the CoS-only Remote Control flag, in the
# main checkout (never a task worktree, which is removed when its task lands):
#   cd <main> && SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH=1 claude --dangerously-skip-permissions
#     --model <m> --effort <e> --settings <tab settings> --remote-control CoS CoS
# The seed prompt "CoS" is cos_autobind.py's trigger, so the tab registers itself as CoS
# (titled "🧭 CoS", pinned, session bound) and replies "Ready.", as when the owner types
# `claude` then `CoS`. An older CoS tab is renamed "🧭 CoS (old …)", never closed.
#
# --at-logon (Windows Task Scheduler, com.switchboard.cos-autostart in
# system/windows/register-scheduled-tasks.ps1): starts wmux when it is not running, then
# polls inside the restore window. wmux resumes the CoS it had with `claude --resume`, and a
# resumed session that had Remote Control turns it back on, so a restored live CoS is kept and
# nothing opens. An empty or unreadable tab list means wmux is still restoring, never "no
# CoS": if it stays that way the run opens nothing and says so. A CoS tab with no live Claude
# after the window (a clean exit is not resumed) gets a fresh CoS beside it.
#
# Live CoS: owner_tabs.py cos-target names a tab, and wmux's agent-state for its surface
# names a Claude session (state "unknown" once Claude exits). Without wmux, the tab's last
# screen lines must show Claude Code's footer and no "Resume this session with" exit note.
# One run at a time: a lock folder (stale after LAUNCH_COS_LOCK_SECONDS, default 300) also
# covers the minute a new tab is still booting under its "CoS starting" title.
# Amp: Remote Control is a Claude Code feature, so with router.yaml agent: amp this opens CoS
# through ensure-routine-workspaces.sh switchboard instead.
#
# Switches (environment, else local/machines/<MACHINE>.env): SWITCHBOARD_COS_AUTOSTART=off skips
# --at-logon and --if-missing runs; SWITCHBOARD_COS_REMOTE_CONTROL=off drops the flag
# (cmux-helpers.sh). Waits: COS_RESTORE_WAIT_SECONDS (default 90), COS_MUX_WAIT_SECONDS
# (default 180). Tests: system/scripts/test_launch_cos.py.
set -uo pipefail

say() { printf '[launch-cos %s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

# Scripts come from the checkout this runs in; CoS itself opens in the main checkout.
CODE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { say "failed: run inside the switchboard repo"; exit 2; }
common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || common="$CODE_ROOT/.git"
COS_ROOT="${LAUNCH_COS_ROOT:-$(dirname "$common")}"
[[ -d "$COS_ROOT/system" ]] || COS_ROOT="$CODE_ROOT"
cd "$CODE_ROOT" || exit 2

if_missing=0 at_logon=0 dry_run=0
for arg in "$@"; do
    case "$arg" in
        --if-missing) if_missing=1 ;;
        --at-logon) at_logon=1; if_missing=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,41p' "$0"; exit 0 ;;
        *) say "unknown argument: $arg"; exit 2 ;;
    esac
done

for wait_var in COS_RESTORE_WAIT_SECONDS COS_MUX_WAIT_SECONDS LAUNCH_COS_LOCK_SECONDS; do
    [[ "${!wait_var:-0}" =~ ^[0-9]+$ ]] || { say "failed: $wait_var must be a whole number of seconds"; exit 2; }
done

# Helpers read REPO_ROOT as a plain shell variable; it is never exported, so wmux and the
# tabs it opens never inherit this checkout's root.
REPO_ROOT="$CODE_ROOT"
# shellcheck source=/dev/null
source "$CODE_ROOT/system/scripts/lib/machine-role.sh" || { say "failed: machine-role.sh did not load"; exit 1; }
if (( if_missing )); then
    case "$(switchboard_machine_value SWITCHBOARD_COS_AUTOSTART | tr '[:upper:]' '[:lower:]')" in
        off|0|false|no) say "skipped: SWITCHBOARD_COS_AUTOSTART=off"; exit 0 ;;
    esac
fi
# shellcheck source=/dev/null
source "$CODE_ROOT/system/cmux-helpers.sh" >/dev/null || { say "failed: cmux-helpers.sh did not load"; exit 1; }
REPO_ROOT="$CODE_ROOT"
PY="${SWITCHBOARD_PYTHON:-python3}"
CMUX="${CMUX_BIN:-cmux}"

cos_line() {
    local mode
    mode="$(resolve_agent_mode switchboard 2>/dev/null)" || mode="ultra"
    [[ -n "$mode" ]] || mode="ultra"
    # The settings file path is the main checkout's, like the rest of the line.
    local cmd
    cmd="$(REPO_ROOT="$COS_ROOT" agent_interactive_command "$mode" switchboard)"
    printf 'cd %q && %s CoS' "$COS_ROOT" "$cmd"
}

if (( dry_run )); then
    if [[ "$(switchboard_agent)" != "claude" ]]; then say "dry run: agent is $(switchboard_agent); would run ensure-routine-workspaces.sh switchboard"; exit 0; fi
    printf '%s\n' "$(cos_line)"
    exit 0
fi

is_windows() { [[ "${OS:-}" == "Windows_NT" ]] || [[ "$(uname -s 2>/dev/null)" == MINGW* || "$(uname -s 2>/dev/null)" == MSYS* ]]; }

mux_up() { CMUX_QUIET=1 "$CMUX" ping >/dev/null 2>&1 || CMUX_QUIET=1 "$CMUX" list-workspaces >/dev/null 2>&1; }

start_wmux() {
    local exe="${WMUX_EXE:-${LOCALAPPDATA:-$HOME/AppData/Local}/Programs/wmux/wmux.exe}"
    [[ -f "$exe" ]] || { say "wmux not found at $exe"; return 1; }
    say "starting wmux ($exe)"
    # cmd's start detaches it from this task; "" is start's window title. REPO_ROOT stays
    # out of wmux's environment, so its tabs resolve their own checkout.
    env -u REPO_ROOT cmd.exe //c start "" "$(cygpath -w "$exe" 2>/dev/null || printf '%s' "$exe")" >/dev/null 2>&1
}

wait_for() {  # <seconds> <command...>
    local end=$((SECONDS + $1)); shift
    while (( SECONDS < end )); do "$@" && return 0; sleep 3; done
    "$@"
}

# The tab list is readable and names at least one tab (wmux always has one once restored).
tabs_ready() { [[ -n "$(CMUX_QUIET=1 "$CMUX" list-workspaces 2>/dev/null | grep -E 'workspace:[0-9]+' | head -1)" ]]; }

COS_JSON=""
read_cos() { COS_JSON="$("$PY" "$CODE_ROOT/system/scripts/owner_tabs.py" cos-target 2>/dev/null)"; }
cos_field() { printf '%s' "$COS_JSON" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get(sys.argv[1]) or "")
except Exception: print("")' "$1"; }

# wmux: the surface's agent-state names a Claude session while Claude runs, and turns
# "unknown" with no session once it exits. Prints live, dead, or "".
wmux_agent_state() {  # <workspace id>
    local wmux="${WMUX_BIN:-}" sid state
    [[ -n "$wmux" && -n "${1:-}" ]] || return 0
    sid="$("$wmux" list-surfaces --workspace "$1" 2>/dev/null | grep -oE 'surf-[0-9a-fA-F-]+' | head -1)"
    [[ -n "$sid" ]] || return 0
    state="$("$wmux" agent-state --surface "$sid" 2>/dev/null)" || return 0
    printf '%s' "$state" | "$PY" -c 'import json,sys
try: s=(json.load(sys.stdin).get("state") or {})
except Exception: sys.exit(0)
live = s.get("sessionId") and s.get("state") not in (None, "", "unknown")
print("live" if live else "dead")'
}

cos_live() {  # <ref> <workspace id>
    local verdict screen tail_lines
    verdict="$(wmux_agent_state "${2:-}")"
    [[ "$verdict" == live ]] && return 0
    [[ "$verdict" == dead ]] && return 1
    screen="$(CMUX_QUIET=1 "$CMUX" read-screen --workspace "$1" 2>/dev/null)" || return 1
    tail_lines="$(printf '%s\n' "$screen" | grep -v '^[[:space:]]*$' | tail -n 4)"
    printf '%s\n' "$tail_lines" | grep -q 'Resume this session with' && return 1
    printf '%s\n' "$tail_lines" | grep -qE "$CLAUDE_READY_MARKER|bypass permissions on|esc to interrupt"
}

# Ready to decide: the tab list is readable and either names a live CoS (rc 0, kept) or not.
KEPT_REF=""
cos_kept() {
    tabs_ready || return 1
    read_cos
    local ref; ref="$(cos_field ref)"
    [[ -n "$ref" ]] && cos_live "$ref" "$(cos_field workspace_id)" && { KEPT_REF="$ref"; return 0; }
    return 1
}

if (( at_logon )); then
    if ! mux_up; then
        if is_windows; then start_wmux || { say "failed: wmux is not running and could not be started"; exit 1; }; fi
        wait_for "${COS_MUX_WAIT_SECONDS:-180}" mux_up || { say "failed: the multiplexer did not answer within ${COS_MUX_WAIT_SECONDS:-180}s"; exit 1; }
        say "multiplexer is up"
    fi
fi

if [[ "$(switchboard_agent)" != "claude" ]]; then
    say "agent is $(switchboard_agent) (no Remote Control); opening CoS through ensure-routine-workspaces.sh switchboard"
    exec bash "$CODE_ROOT/system/scripts/ensure-routine-workspaces.sh" switchboard
fi

mux_up || { say "failed: the multiplexer is not running"; exit 1; }

if (( if_missing )); then
    restore_wait=0
    (( at_logon )) && restore_wait="${COS_RESTORE_WAIT_SECONDS:-90}"
    if wait_for "$restore_wait" cos_kept; then
        say "kept: CoS is already live in $KEPT_REF (a resumed CoS keeps the Remote Control it had)"
        exit 0
    fi
    if ! tabs_ready; then
        say "failed: the tab list stayed empty or unreadable for ${restore_wait}s; opening nothing so a restoring CoS is never doubled"
        exit 1
    fi
    ref="$(cos_field ref)"
    if [[ -n "$ref" ]]; then say "CoS tab $ref shows no live Claude session; opening a fresh CoS beside it"
    else say "no CoS tab is open; opening one"; fi
fi

lock="${LAUNCH_COS_LOCK_DIR:-$COS_ROOT/.state/launch-cos.lock}"
mkdir -p "$(dirname "$lock")" 2>/dev/null
if ! mkdir "$lock" 2>/dev/null; then
    age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || date +%s) ))
    if (( age < ${LAUNCH_COS_LOCK_SECONDS:-300} )); then
        say "skipped: another CoS launch started ${age}s ago (lock $lock)"
        exit 0
    fi
    rmdir "$lock" 2>/dev/null; mkdir "$lock" 2>/dev/null || { say "skipped: could not take the launch lock $lock"; exit 0; }
fi
# The lock is left to expire, so a second run within the window sees the booting tab as taken.

line="$(cos_line)"
# Not a CoS title yet: cos_autobind.py titles it "🧭 CoS" when it claims the tab, so
# title-based senders never reach a tab that is still booting.
out="$(CMUX_QUIET=1 "$CMUX" new-workspace --name "CoS starting" --cwd "$COS_ROOT" --focus false --command "$line" 2>&1)" || {
    rmdir "$lock" 2>/dev/null; say "failed: new-workspace: $out"; exit 1; }
say "opened CoS with Remote Control: $out"
