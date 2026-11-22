#!/usr/bin/env bash
# Per-machine Comms Triage switch: the bash twin of system/scripts/comms_triage_setting.py.
# Returns 1 when PM_OS_COMMS_TRIAGE is off in the environment or in this machine's file,
# local/machines/<MACHINE>.env (host name up to its first dot, upper case). A machine with
# no such file returns 0, so the default stays on. Never exits a sourcing script.
pm_os_comms_triage_enabled() {
    local value="${PM_OS_COMMS_TRIAGE:-}"
    if [[ -z "$value" ]]; then
        local root dir name file
        root="${PM_OS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
        dir="${PM_OS_LOCAL_DIR:-$root/local}"
        if [[ -d "$dir/machines" ]]; then
            name="${PM_OS_MACHINE:-${HOSTNAME:-$(hostname)}}"
            name="$(printf '%s' "${name%%.*}" | tr '[:lower:]' '[:upper:]')"
            file="$dir/machines/$name.env"
            if [[ -f "$file" ]]; then
                value="$(sed -n 's/^[^A-Za-z#]*PM_OS_COMMS_TRIAGE[[:space:]]*=\(.*\)$/\1/p' "$file" | tail -n 1)"
            fi
        fi
    fi
    value="$(printf '%s' "$value" | tr -d "\"' \t\r" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        off|0|false|no|disabled) return 1 ;;
    esac
    return 0
}
