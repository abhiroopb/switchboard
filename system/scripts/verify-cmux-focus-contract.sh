#!/usr/bin/env bash
# Live-shaped cmux verification. Creates and removes one background probe
# workspace, while proving that the selected workspace does not change.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT
export CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
export CMUX_LOCK_DIR="${CMUX_LOCK_DIR:-${TMPDIR:-/tmp}/switchboard-focus-verify.lock}"

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

probe_ref=""
before=""
log_file="$(mktemp "${TMPDIR:-/tmp}/switchboard-focus-verify.XXXXXX")"

selected_workspace() {
    CMUX_QUIET=1 cmux_cmd list-workspaces 2>/dev/null \
        | sed -nE 's/^\* (workspace:[0-9]+).*\[selected\]$/\1/p' \
        | head -1
}

mark_probe_close() {
    local uuid
    uuid=$(workspace_uuid_for_ref "$1" 2>/dev/null || true)
    [[ -n "$uuid" ]] || return 0
    python3 - "$REPO_ROOT/.state/cmux-skip-record" "$uuid" <<'PY'
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("SWITCHBOARD_SCRIPTS_DIR") or _os.path.join(_os.environ.get("SWITCHBOARD_ROOT") or _os.path.expanduser("~/Development/switchboard"), "system", "scripts"))
    import flock_compat as fcntl
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    handle.write(sys.argv[2] + "\n")
PY
}

restore_and_cleanup() {
    if [[ -n "$probe_ref" ]]; then
        mark_probe_close "$probe_ref"
        cmux_cmd close-workspace --workspace "$probe_ref" --force >/dev/null 2>&1 || true
    fi
    local current
    current=$(selected_workspace || true)
    if [[ -n "$before" && "$current" != "$before" ]]; then
        SWITCHBOARD_EXPLICIT_USER_FOCUS=1 \
            cmux_cmd select-workspace --workspace "$before" >/dev/null 2>&1 || true
    fi
    rm -f "$log_file"
}
trap restore_and_cleanup EXIT

before=$(selected_workspace)
[[ -n "$before" ]] || { echo "error: cmux has no selected workspace" >&2; exit 1; }

OPEN_COMMS_LOG="$log_file" \
    bash "$REPO_ROOT/system/scripts/open-comms-triage.sh" --background
after_comms=$(selected_workspace)
[[ "$after_comms" == "$before" ]] || {
    echo "error: background Comms check changed selection: $before -> $after_comms" >&2
    exit 1
}

probe_title="🧪 switchboard Focus Probe $$"
output=$(CMUX_QUIET=1 cmux_cmd new-workspace --name "$probe_title" --cwd "$REPO_ROOT")
probe_ref=$(printf '%s\n' "$output" | grep -oE 'workspace:[0-9]+' | head -1 || true)
[[ -n "$probe_ref" ]] || { echo "error: could not resolve probe workspace" >&2; exit 1; }

after_create=$(selected_workspace)
[[ "$after_create" == "$before" ]] || {
    echo "error: background workspace creation changed selection: $before -> $after_create" >&2
    exit 1
}

mark_probe_close "$probe_ref"
cmux_cmd close-workspace --workspace "$probe_ref" --force >/dev/null
probe_ref=""

after_close=$(selected_workspace)
[[ "$after_close" == "$before" ]] || {
    echo "error: background workspace cleanup changed selection: $before -> $after_close" >&2
    exit 1
}

printf 'ok - selected workspace stayed %s across Comms callback and workspace create/close\n' "$before"
