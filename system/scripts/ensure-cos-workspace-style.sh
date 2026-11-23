#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

title="${1:-🧭 CoS}"
ws_ref=$(normalize_switchboard_workspace "" "$title") || {
    echo "No CoS workspace found" >&2
    exit 1
}

echo "$ws_ref"
