#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CMUX_BIN="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"

# Windows: one python process decides nothing changed, in place of about 80 processes to
# reach the same badge. Exit 0 means the badge is current.
if [[ -n "${MSYSTEM:-}" ]] && python3 "$REPO_ROOT/system/scripts/running_tasks_badge.py" unchanged 2>/dev/null; then
    exit 0
fi

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

all_published=1
while IFS= read -r surface; do
    [[ -n "$surface" ]] || continue
    source_type="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get "$surface" source_type)"
    source_key="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get "$surface" source_key)"
    surface_ref="$(find_workspace_ref_for_source_key "$source_type" "$source_key" 2>/dev/null || true)"
    [[ -n "$surface_ref" ]] || { all_published=0; continue; }

    summary="$(python3 "$REPO_ROOT/system/scripts/running-task-state.py" summary --origin "$surface")"
    read -r active_count pending_count <<< "$(printf '%s' "$summary" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
print(payload["active_count"], payload.get("pending_report_count", 0))
')"

    if (( active_count > 0 && pending_count > 0 )); then
        cmux_cmd set-status running-tasks "$active_count running · $pending_count updates" \
            --workspace "$surface_ref" --icon bolt --color "#FFF0A8" >/dev/null 2>&1 || all_published=0
    elif (( active_count > 0 )); then
        cmux_cmd set-status running-tasks "$active_count running" \
            --workspace "$surface_ref" --icon bolt --color "#FFF0A8" >/dev/null 2>&1 || all_published=0
    elif (( pending_count > 0 )); then
        cmux_cmd set-status running-tasks "$pending_count updates" \
            --workspace "$surface_ref" --icon bell --color "#FFF0A8" >/dev/null 2>&1 || all_published=0
    else
        cmux_cmd clear-status running-tasks --workspace "$surface_ref" >/dev/null 2>&1 || all_published=0
    fi
done < <(python3 "$REPO_ROOT/system/scripts/surface-registry.py" list --where command_center_badge)
if [[ -n "${MSYSTEM:-}" && "$all_published" == "1" ]]; then
    python3 "$REPO_ROOT/system/scripts/running_tasks_badge.py" record 2>/dev/null || true
fi
