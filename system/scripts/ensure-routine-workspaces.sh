#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
# shellcheck source=lib/machine-role.sh
[[ -f "$REPO_ROOT/system/scripts/lib/machine-role.sh" ]] && source "$REPO_ROOT/system/scripts/lib/machine-role.sh"

usage() {
    cat <<'EOF'
Usage: ensure-routine-workspaces.sh [routine-name ...]

Launch core routine workspaces if they are missing from cmux.
Defaults to: todo launchpad comms-triage scheduled-jobs meetings
(sidebar order on cmux; wmux cannot order tabs).
comms-triage is skipped, even when named, while PM_OS_COMMS_TRIAGE=off on this machine.
On a personal machine (PM_OS_MACHINE_ROLE=personal, system/scripts/machine_role.py),
launchpad, meetings, and scheduled-jobs are skipped, even when named, until the owner
loads one: system/scripts/load-surface.sh <surface> ("load Launchpad" in CoS).

A routine whose config.yaml declares `launcher: <repo-relative script>` is
started by running that script (always, it must be idempotent) instead of
seeding an Amp session via cmux-helpers.sh launch.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -gt 0 ]]; then
    routines=("$@")
else
    routines=(todo launchpad comms-triage scheduled-jobs meetings)
fi

for routine_name in "${routines[@]}"; do
    # Machine-local off switch (system/scripts/comms_triage_setting.py).
    if [[ "$routine_name" == "comms-triage" ]] && declare -F pm_os_comms_triage_enabled >/dev/null \
        && ! pm_os_comms_triage_enabled; then
        log_info "Skipping comms-triage: PM_OS_COMMS_TRIAGE=off on this machine"
        continue
    fi

    # Machine role (system/scripts/machine_role.py): on a personal machine these open on demand.
    if declare -F pm_os_surface_opens_by_default >/dev/null && ! pm_os_surface_opens_by_default "$routine_name"; then
        case "$routine_name" in
            launchpad) surface_label="Launchpad" ;;
            scheduled-jobs) surface_label="Scheduled Jobs" ;;
            meetings) surface_label="Meetings" ;;
            *) surface_label="$routine_name" ;;
        esac
        log_info "Skipping $surface_label: opens on demand on this personal machine (say \"load $surface_label\" in CoS, or run system/scripts/load-surface.sh $routine_name)"
        continue
    fi

    if [[ ! -f "$REPO_ROOT/routines/$routine_name/CONTEXT.md" ]]; then
        log_warn "Skipping missing routine: $routine_name"
        continue
    fi

    # Deterministic (non-Amp) routines declare their own idempotent launcher.
    launcher=""
    if [[ -f "$REPO_ROOT/routines/$routine_name/config.yaml" ]]; then
        launcher=$(sed -n 's/^launcher:[[:space:]]*"\{0,1\}\([^"#]*[^"# ]\)"\{0,1\}.*$/\1/p' "$REPO_ROOT/routines/$routine_name/config.yaml" | head -1)
    fi
    if [[ -n "$launcher" ]]; then
        if [[ ! -f "$REPO_ROOT/$launcher" ]]; then
            log_warn "Routine $routine_name declares missing launcher: $launcher"
            continue
        fi
        log_info "Ensuring routine via launcher: $routine_name ($launcher)"
        bash "$REPO_ROOT/$launcher" >/dev/null || log_warn "Launcher for $routine_name exited $?"
        continue
    fi

    if ref=$(find_workspace_ref_for_source_key routine "$routine_name" 2>/dev/null || find_workspace_ref_for_source_key generic "$routine_name" 2>/dev/null); then
        log_info "Routine already open: $routine_name ($ref)"
        continue
    fi

    log_info "Launching missing routine: $routine_name"
    bash "$REPO_ROOT/system/cmux-helpers.sh" launch "routine:$routine_name" || \
        log_warn "Launcher for $routine_name exited $? (non-fatal)"
done
