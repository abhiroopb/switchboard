#!/usr/bin/env bash
# cos-phase0.sh: Phase 0 of the chief-of-staff skill on macOS (cmux) and Windows (wmux).
#
#   bash system/scripts/cos-phase0.sh            # chrome --claim + audit + rearm (what Phase 0 runs)
#   bash system/scripts/cos-phase0.sh chrome     # register the caller's tab as CoS (owner_tabs.py claim-cos:
#                                                #   title "🧭 CoS", pinned, old CoS tabs demoted), move it to the top;
#                                                #   refuses while another tab is the live CoS
#   bash system/scripts/cos-phase0.sh chrome --claim   # take CoS over from another tab (a fresh CoS session)
#   bash system/scripts/cos-phase0.sh audit      # list tabs still titled with the generic 📁
#   bash system/scripts/cos-phase0.sh rearm      # make sure the command-center job is loaded, then tick once
#
# Why: a skill that runs /Applications/cmux.app/... by hand renames nothing on
# Windows, so the CoS tab keeps a generic name and the Gmail send guard, which
# accepts only a tab named CoS or Launchpad, refuses a "send it". lib/mux.sh picks the backend: on wmux, CMUX_BIN is the
# cmux shim (system/scripts/mux/cmux), which renames through wmux and keeps pin and
# order in its sidecar (wmux has no pin or reorder RPC).
#
# A worker never takes the title: a "switchboard worker <task>" description, a
# SWITCHBOARD_WORKER* variable, or a task worktree refuses `chrome` (exit 3), and an
# unreadable tab list fails closed (exit 1). The Gmail send guard trusts the CoS
# title, so this script is careful about who gets it.
# Exit 0 ok, 1 failed, 2 usage, 3 refused.
set -uo pipefail

TITLE="${SWITCHBOARD_COS_TITLE:-🧭 CoS}"
PY="${SWITCHBOARD_PYTHON:-python3}"
here="${BASH_SOURCE[0]//\\//}"
if [[ "$here" == */* ]]; then here="${here%/*}"; else here="."; fi
# shellcheck source=lib/mux.sh
source "$here/lib/mux.sh"
mux_init
OWNER_TABS="${SWITCHBOARD_OWNER_TABS:-$here/owner_tabs.py}"

cmux_bin() {
  if [[ -n "${CMUX_BIN:-}" ]]; then echo "$CMUX_BIN"; return; fi
  if command -v cmux >/dev/null 2>&1; then command -v cmux; return; fi
  echo /Applications/cmux.app/Contents/Resources/bin/cmux
}

json_query() {  # json_query caller|has <ref>|description <ref>|ref|claimed|folders: JSON on stdin, no eval
  "$PY" -c '
import json, sys
mode = sys.argv[1]
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
tabs = d.get("workspaces", []) if isinstance(d, dict) else []
if mode == "caller":
    print((d.get("caller") or {}).get("workspace_ref", ""))
elif mode == "has":
    print("yes" if any(w.get("ref") == sys.argv[2] for w in tabs) else "no")
elif mode == "ref":
    print(d.get("ref", "") if isinstance(d, dict) else "")
elif mode == "claimed":
    print("yes" if isinstance(d, dict) and d.get("claimed") else "no")
elif mode == "description":
    print(next((w.get("description") or "" for w in tabs if w.get("ref") == sys.argv[2]), ""))
elif mode == "folders":
    for w in tabs:
        if "📁" in (w.get("title") or ""):
            print(w.get("ref", ""), w.get("title", ""))
' "$@"
}

same_dir() {  # same_dir <a> <b>: both resolve to one directory
  [[ -n "$1" && -n "$2" ]] && [[ "$(cd "$1" 2>/dev/null && pwd -P)" == "$(cd "$2" 2>/dev/null && pwd -P)" ]]
}

do_chrome() {
  local claim=0
  [[ "${1:-}" == "--claim" ]] && claim=1
  if [[ "${SWITCHBOARD_MUX:-none}" == "none" ]]; then
    echo "chrome: no multiplexer here (SWITCHBOARD_MUX=none); nothing to rename"
    return 0
  fi
  if [[ -n "${SWITCHBOARD_WORKER:-}${SWITCHBOARD_WORKER_ID:-}${SWITCHBOARD_WORKER_TASK_ID:-}${SWITCHBOARD_WORKTREE:-}" ]]; then
    echo "chrome: refused, this is a worker session; only the CoS tab takes the CoS title" >&2
    return 3
  fi
  local gd gcd
  gd="$(git rev-parse --git-dir 2>/dev/null || true)"
  gcd="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  if [[ -n "$gd" && -n "$gcd" ]] && ! same_dir "$gd" "$gcd"; then
    echo "chrome: refused, this runs inside a task worktree; CoS runs from the main checkout" >&2
    return 3
  fi
  local cmux ws list desc current
  cmux="$(cmux_bin)"
  ws="$(CMUX_QUIET=1 "$cmux" identify 2>/dev/null | json_query caller)"
  if [[ -z "$ws" ]]; then
    echo "chrome: failed, $cmux identify named no caller tab (CMUX_WORKSPACE_ID unset?)" >&2
    return 1
  fi
  # Fail closed: an unreadable tab list cannot prove this is not a worker tab.
  if ! list="$(CMUX_QUIET=1 "$cmux" workspace list --json 2>/dev/null)" || \
     [[ "$(printf '%s' "$list" | json_query has "$ws")" != "yes" ]]; then
    echo "chrome: failed, $ws is not in the tab list (or the list is unreadable); nothing renamed" >&2
    return 1
  fi
  desc="$(printf '%s' "$list" | json_query description "$ws")"
  if [[ "$desc" == "switchboard worker "* ]]; then
    echo "chrome: refused, $ws is a worker tab ($desc)" >&2
    return 3
  fi
  # One CoS at a time. claim-cos registers this tab (system/state/cos-surface.json),
  # retitles it, pins it, and demotes any other CoS-titled tab to "🧭 CoS (old …)".
  # Without --claim, a different live CoS tab refuses, so a refused send in some
  # other owner tab cannot quietly take the CoS title.
  current="$("$PY" "$OWNER_TABS" cos-target 2>/dev/null | json_query ref)"
  if [[ -n "$current" && "$current" != "$ws" && $claim -eq 0 ]]; then
    echo "chrome: refused, the live CoS tab is $current. Only when the owner started CoS in $ws: cos-phase0.sh chrome --claim" >&2
    return 3
  fi
  if [[ "$("$PY" "$OWNER_TABS" claim-cos --workspace-ref "$ws" 2>/dev/null | json_query claimed)" != "yes" ]]; then
    echo "chrome: failed, owner_tabs.py claim-cos did not register $ws" >&2
    return 1
  fi
  if ! CMUX_QUIET=1 "$cmux" workspace-action --action move-top --workspace "$ws" >/dev/null 2>&1; then
    echo "chrome: $ws registered as CoS and renamed \"$TITLE\", pinned; move-top failed ($SWITCHBOARD_MUX)"
    return 0
  fi
  echo "chrome: $ws registered as CoS, renamed \"$TITLE\", pinned, moved to top ($SWITCHBOARD_MUX)"
}

do_audit() {
  [[ "${SWITCHBOARD_MUX:-none}" == "none" ]] && { echo "audit: no multiplexer here"; return 0; }
  local cmux hits
  cmux="$(cmux_bin)"
  hits="$(CMUX_QUIET=1 "$cmux" workspace list --json 2>/dev/null | json_query folders)"
  if [[ -z "$hits" ]]; then
    echo "audit: no tab carries the generic 📁"
  else
    echo "audit: these tabs need their workstream emoji (config.yaml emoji:, then rename):"
    echo "$hits"
  fi
}

do_rearm() {
  local root rc=0
  root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      local plist="$HOME/Library/LaunchAgents/com.switchboard.command-center.plist"
      if [[ -f "$plist" ]]; then
        launchctl load -w "$plist" 2>/dev/null || true
        echo "rearm: launchd com.switchboard.command-center loaded"
      else
        echo "rearm: $plist is missing; install the launchd jobs first" >&2
        rc=1
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      # Task Scheduler is the Windows launchd (system/windows/register-scheduled-tasks.ps1).
      if MSYS2_ARG_CONV_EXCL='*' schtasks /Change /TN '\switchboard\com.switchboard.command-center' /ENABLE >/dev/null 2>&1; then
        echo "rearm: scheduled task \\switchboard\\com.switchboard.command-center enabled"
      else
        echo "rearm: scheduled task \\switchboard\\com.switchboard.command-center not found; run system/windows/register-scheduled-tasks.ps1" >&2
        rc=1
      fi
      ;;
    *)
      echo "rearm: no scheduler step for $(uname -s 2>/dev/null)"
      ;;
  esac
  if [[ -n "$root" && -f "$root/system/scripts/command-center-loop.sh" ]]; then
    bash "$root/system/scripts/command-center-loop.sh" --once >/dev/null 2>&1 || { echo "rearm: command-center-loop.sh --once failed" >&2; rc=1; }
    [[ $rc -eq 0 ]] && echo "rearm: command-center ticked once"
  fi
  return $rc
}

case "${1:-all}" in
  chrome) do_chrome "${2:-}" ;;
  audit) do_audit ;;
  rearm) do_rearm ;;
  all) rc=0; do_chrome --claim || rc=$?; do_audit; do_rearm || { [[ $rc -eq 0 ]] && rc=1; }; exit $rc ;;
  -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *) echo "usage: cos-phase0.sh [chrome|audit|rearm]" >&2; exit 2 ;;
esac
