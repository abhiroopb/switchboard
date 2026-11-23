#!/usr/bin/env bash
# Switch the agent every switchboard launch starts (routine tabs, workers, start-day,
# daemon jobs) between Claude Code and Amp. Both are installed by install-all.sh,
# so switching is only this setting; tabs already open keep their agent.
#
#   bash system/scripts/set-agent.sh claude
#   bash system/scripts/set-agent.sh amp
#   bash system/scripts/set-agent.sh          print the current agent and where it came from
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="$ROOT/system/config/router.yaml"
PY="${SWITCHBOARD_PYTHON:-$(command -v python3 || command -v python)}"
want="${1:-}"
if [[ -z "$want" ]]; then
  "$PY" "$ROOT/system/scripts/switchboard_agent.py" --explain
  exit 0
fi
case "$want" in claude|amp) ;; *) echo "usage: set-agent.sh claude|amp" >&2; exit 2 ;; esac
if grep -q '^agent:' "$CONFIG"; then
  sed -i.bak "s/^agent:.*/agent: $want/" "$CONFIG" && rm -f "$CONFIG.bak"
else
  printf 'agent: %s\n' "$want" | cat - "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
fi
if [[ -n "${SWITCHBOARD_AGENT:-}" && "$SWITCHBOARD_AGENT" != "$want" ]]; then
  echo "note: SWITCHBOARD_AGENT=$SWITCHBOARD_AGENT is set in this shell and wins over router.yaml; unset it" >&2
fi
"$PY" "$ROOT/system/scripts/switchboard_agent.py" --explain
