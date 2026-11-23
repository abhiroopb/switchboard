#!/usr/bin/env bash
# Per-machine role: the bash twin of system/scripts/machine_role.py.
#
# SWITCHBOARD_MACHINE_ROLE=personal in local/machines/<MACHINE>.env (host name up to its first
# dot, upper case) opens only CoS and To Do by default; Launchpad, Meetings, and Scheduled
# Jobs wait for "load <surface>" (system/scripts/load-surface.sh). No key, or any other
# value, is a work machine and every surface opens, so a machine with no file is unchanged.
# Never exits a sourcing script.

# switchboard_machine_value <KEY>: the environment's value, else the last `KEY=value` line in
# local/machines/<MACHINE>.env, quotes and blanks stripped; empty when neither has it.
# The bash twin of machine_role.value(). The CoS Remote Control and auto-start
# kill switches live in that file.
switchboard_machine_value() {
    local key="${1:-}" value
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 0
    value="${!key:-}"
    if [[ -z "$value" ]]; then
        local root dir name file
        root="${SWITCHBOARD_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
        dir="${SWITCHBOARD_LOCAL_DIR:-$root/local}"
        name="${SWITCHBOARD_MACHINE:-${HOSTNAME:-$(hostname)}}"
        name="$(printf '%s' "${name%%.*}" | tr '[:lower:]' '[:upper:]')"
        file="$dir/machines/$name.env"
        [[ -f "$file" ]] && value="$(sed -n "s/^[[:space:]]*$key[[:space:]]*=\\(.*\\)\$/\\1/p" "$file" | tail -n 1)"
    fi
    printf '%s' "$value" | tr -d "\"' \t\r"
}

# Prints work or personal.
switchboard_machine_role() {
    local value="${SWITCHBOARD_MACHINE_ROLE:-}"
    if [[ -z "$value" ]]; then
        local root dir name file
        root="${SWITCHBOARD_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
        dir="${SWITCHBOARD_LOCAL_DIR:-$root/local}"
        if [[ -d "$dir/machines" ]]; then
            name="${SWITCHBOARD_MACHINE:-${HOSTNAME:-$(hostname)}}"
            name="$(printf '%s' "${name%%.*}" | tr '[:lower:]' '[:upper:]')"
            file="$dir/machines/$name.env"
            if [[ -f "$file" ]]; then
                value="$(sed -n 's/^[^A-Za-z#]*SWITCHBOARD_MACHINE_ROLE[[:space:]]*=\(.*\)$/\1/p' "$file" | tail -n 1)"
            fi
        fi
    fi
    value="$(printf '%s' "$value" | tr -d "\"' \t\r" | tr '[:upper:]' '[:lower:]')"
    if [[ "$value" == "personal" ]]; then echo personal; else echo work; fi
}

# Returns 0 when <routine> opens by default on this machine today, 1 when it waits for
# "load <surface>". A work machine answers 0 with no python call. On a personal machine
# machine_role.py decides, because the kept and loaded-today lists live in the same file.
switchboard_surface_opens_by_default() {
    local routine="${1:-}"
    case "$routine" in
        launchpad|meetings|scheduled-jobs) ;;
        *) return 0 ;;
    esac
    [[ "$(switchboard_machine_role)" == "personal" ]] || return 0
    local script
    script="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/machine_role.py"
    [[ -f "$script" ]] || return 1
    python3 "$script" check "$routine" >/dev/null 2>&1
}
