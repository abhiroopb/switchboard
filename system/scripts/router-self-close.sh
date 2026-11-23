#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CMUX_BIN="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
SKIP_FILE="${CMUX_RELAUNCH_SKIP_FILE:-$REPO_ROOT/.state/cmux-skip-record}"
workspace_ref=""

if [[ "${1:-}" == "--workspace" ]]; then
    workspace_ref="${2:-}"
elif (( $# > 0 )); then
    echo "error: usage: router-self-close.sh [--workspace workspace:N]" >&2
    exit 2
fi

if [[ -z "$workspace_ref" ]]; then
    workspace_ref="$($CMUX_BIN identify 2>/dev/null | "${SWITCHBOARD_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/lib/json_get.py" caller.workspace_ref)"
fi
[[ "$workspace_ref" =~ ^workspace:[0-9]+$ ]] || {
    echo "error: could not resolve Router workspace" >&2
    exit 1
}

workspace_id="$($CMUX_BIN list-workspaces --id-format both 2>/dev/null | awk -v ref="$workspace_ref" '
    $1 == ref { print $2; exit }
    $1 == "*" && $2 == ref { print $3; exit }
')"

if [[ -n "$workspace_id" ]]; then
    python3 - "$SKIP_FILE" "$workspace_id" <<'PY'
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("SWITCHBOARD_SCRIPTS_DIR") or _os.path.join(_os.environ.get("SWITCHBOARD_ROOT") or _os.path.expanduser("~/Development/switchboard"), "system", "scripts"))
    import flock_compat as fcntl
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    handle.write(sys.argv[2] + "\n")
PY
fi

$CMUX_BIN workspace-action --action unpin --workspace "$workspace_ref" >/dev/null 2>&1 || true
# --force: cmux 0.65 refuses a workspace with a running process (the Amp CLI)
# without it; the flag-less forms stay as the fallback for an older cmux.
$CMUX_BIN close-workspace --workspace "$workspace_ref" --force >/dev/null 2>&1 || \
    $CMUX_BIN close-workspace --workspace "$workspace_ref" >/dev/null 2>&1 || \
    $CMUX_BIN workspace close "$workspace_ref" >/dev/null 2>&1
