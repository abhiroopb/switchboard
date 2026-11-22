#!/usr/bin/env bash
# load-surface.sh: open one fixed PM OS surface on demand, as if it had been open all day.
#
# When the owner says "load Meetings", "load Scheduled Tasks", or "load Launchpad", the
# surface opens and runs as though it had always been meant to run. CoS runs this in one tool call on
# `load <surface>`. On a personal machine (PM_OS_MACHINE_ROLE=personal,
# system/scripts/machine_role.py) Launchpad, Meetings, and Scheduled Jobs open only this
# way; on a work machine they are already open and this reports where.
#
#   bash system/scripts/load-surface.sh Meetings
#   bash system/scripts/load-surface.sh "load Scheduled Tasks"
#   bash system/scripts/load-surface.sh --keep Launchpad     also keep it in the default set
#   bash system/scripts/load-surface.sh --dry-run Launchpad  resolve and print, open nothing
#
# Steps: resolve the owner's words to a routine, record the load for today (so start of
# day, the routine launcher, and the sidebar treat it as part of the day until midnight
# PT), refresh the state the surface starts from, open it through
# ensure-routine-workspaces.sh with a one-line "loaded on demand" handoff folded into its
# startup prompt, file it into the sidebar on cmux (Launchpad under To Do, Scheduled Jobs
# and Meetings at the bottom; wmux cannot order tabs), and print one JSON line. Exit 0 opened or already open,
# 2 unknown surface, 3 switched off on this machine, 1 the launch failed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ROLE_PY="$SCRIPT_DIR/machine_role.py"

keep=0
dry_run=0
words=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep) keep=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) words+=("$1") ;;
    esac
    shift
done
phrase="${words[*]:-}"
if [[ "$phrase" =~ [[:space:]]keep([[:space:]]+it)?[[:space:]]*$ ]]; then
    keep=1
fi

json() { python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }

surface="$(python3 "$ROLE_PY" resolve "$phrase" 2>/dev/null)" || surface=""
if [[ -z "$surface" ]]; then
    json status unknown words "$phrase" message "No surface matches \"$phrase\". Say load CoS, To Do, Launchpad, Meetings, or Scheduled Jobs."
    exit 2
fi
case "$surface" in
    megadesk) label="CoS" ;;
    todo) label="To Do" ;;
    launchpad) label="Launchpad" ;;
    comms-triage) label="Comms Triage" ;;
    scheduled-jobs) label="Scheduled Jobs" ;;
    meetings) label="Meetings" ;;
esac

# shellcheck source=lib/comms-triage.sh
source "$SCRIPT_DIR/lib/comms-triage.sh"
# shellcheck source=lib/machine-role.sh
source "$SCRIPT_DIR/lib/machine-role.sh"
role="$(pm_os_machine_role)"

if [[ "$surface" == "comms-triage" ]] && ! pm_os_comms_triage_enabled; then
    json status off surface "$surface" label "$label" role "$role" \
        message "Comms Triage is off on this machine (PM_OS_COMMS_TRIAGE=off), so it was not loaded."
    exit 3
fi

if [[ "$dry_run" == 1 ]]; then
    json status dry-run surface "$surface" label "$label" role "$role" keep "$keep" \
        message "Would load $label."
    exit 0
fi

# Record the load before opening, so the launcher's role gate lets it through and a start
# of day later today keeps it. A work machine opens everything already: nothing to record.
kept_note=""
if [[ "$role" == "personal" ]]; then
    case "$surface" in
        launchpad|meetings|scheduled-jobs)
            python3 "$ROLE_PY" mark-loaded "$surface" >/dev/null
            if [[ "$keep" == 1 ]]; then
                python3 "$ROLE_PY" keep "$surface" >/dev/null
                kept_note=" It stays in the default set on this machine from now on (unkeep: python3 system/scripts/machine_role.py unkeep $surface)."
            else
                kept_note=" Loaded for today. To open it every day here, say \"load $label and keep it\"."
            fi
            ;;
    esac
fi

# Fresh state for the surface's startup prompt: meetings.json and scheduled-jobs.json come
# from the megadesk rebuild, which the command-center tick also runs.
case "$surface" in
    meetings|scheduled-jobs|launchpad|megadesk)
        python3 "$SCRIPT_DIR/rebuild-megadesk-state.py" --write --event-source load-surface >/dev/null 2>&1 || true
        ;;
esac

today="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import machine_role; print(machine_role.today_pt())' "$SCRIPT_DIR")"
case "$surface" in
    scheduled-jobs)
        handoff="Loaded on demand by the owner today ($today PT). Run as if this tab had been open since start of day: catch up every job in system/scheduled-jobs.yaml that is due or missed its slot today, and any [AUTOMATION] work queued while the tab was closed, under the usual confirm rules." ;;
    meetings)
        handoff="Loaded on demand by the owner today ($today PT). Run as if this tab had been open since start of day: load today's meetings first (pmos calendar for today in Pacific Time, plus system/state/sources/meetings.json), prep the next one, and list follow-ups from meetings that already ended." ;;
    launchpad)
        handoff="Loaded on demand by the owner today ($today PT). This tab is a dispatch origin from now on, exactly as if it had opened at start of day." ;;
    *)
        handoff="Loaded on demand by the owner today ($today PT)." ;;
esac

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh" >/dev/null 2>&1 || true
ref_for() {
    declare -F find_workspace_ref_for_source_key >/dev/null || return 1
    find_workspace_ref_for_source_key routine "$1" 2>/dev/null || find_workspace_ref_for_source_key generic "$1" 2>/dev/null
}

before="$(ref_for "$surface" || true)"
if [[ -n "$before" ]]; then
    status="already-open"
    ref="$before"
else
    CMUX_LAUNCH_HANDOFF="$handoff" bash "$SCRIPT_DIR/ensure-routine-workspaces.sh" "$surface" >&2 || true
    ref="$(ref_for "$surface" || true)"
    status="opened"
    [[ -n "$ref" ]] || status="failed"
fi

if [[ "$status" != "failed" ]] && declare -F arrange_workspace_sidebar >/dev/null; then
    arrange_workspace_sidebar >/dev/null 2>&1 || true   # a no-op on wmux
fi

case "$status" in
    opened) message="Loaded $label${ref:+ ($ref)}.$kept_note" ;;
    already-open) message="$label is already open${ref:+ ($ref)}.$kept_note" ;;
    *) message="$label did not open; see the launcher output above." ;;
esac

json status "$status" surface "$surface" label "$label" ref "${ref:-}" role "$role" \
    keep "$keep" message "$message"
[[ "$status" == "failed" ]] && exit 1
exit 0
