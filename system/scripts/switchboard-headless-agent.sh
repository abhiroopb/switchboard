#!/usr/bin/env bash
# switchboard-headless-agent.sh — run one prompt headlessly on the configured agent.
#
# The daemon jobs (pre-brief, post-ingest, uxr-debrief, the
# post-session job) used to call `amp --settings-file settings-headless.json -x`
# directly. They call this instead, so the one agent knob (SWITCHBOARD_AGENT env, else router.yaml `agent:`, else claude; resolver
# system/scripts/switchboard_agent.py) decides which CLI answers:
#
#   amp:    $SWITCHBOARD_AMP_BIN --settings-file $SWITCHBOARD_AMP_HEADLESS_SETTINGS -x <prompt>
#   claude: $SWITCHBOARD_CLAUDE_BIN -p --dangerously-skip-permissions --model <m> --effort <e> <prompt>
#
# Usage: switchboard-headless-agent.sh "<prompt>"
# The agent's stdout and stderr pass through untouched and the agent's exit code
# is this script's (exec), so callers keep their `timeout N ...`, output capture
# and marker checks unchanged.
#
# Env: SWITCHBOARD_AMP_BIN (default: amp on PATH, else ~/.local/bin/amp),
#      SWITCHBOARD_AMP_HEADLESS_SETTINGS (default ~/.config/amp/settings-headless.json),
#      SWITCHBOARD_CLAUDE_BIN (default: claude on PATH, else /usr/local/bin/claude),
#      SWITCHBOARD_HEADLESS_CLAUDE_MODEL (opus), SWITCHBOARD_HEADLESS_CLAUDE_EFFORT (high),
#      ROUTER_CONFIG (another router.yaml, for tests).
set -uo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: switchboard-headless-agent.sh \"<prompt>\"" >&2
    exit 2
fi
prompt="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agent="$(python3 "$script_dir/switchboard_agent.py" 2>/dev/null || true)"
[[ "$agent" == "amp" || "$agent" == "claude" ]] || agent=claude

if [[ "$agent" == "amp" ]]; then
    amp_bin="${SWITCHBOARD_AMP_BIN:-$(command -v amp || echo "$HOME/.local/bin/amp")}"
    exec "$amp_bin" --settings-file "${SWITCHBOARD_AMP_HEADLESS_SETTINGS:-$HOME/.config/amp/settings-headless.json}" -x "$prompt"
fi
claude_bin="${SWITCHBOARD_CLAUDE_BIN:-$(command -v claude || echo /usr/local/bin/claude)}"
exec "$claude_bin" -p --dangerously-skip-permissions \
    --model "${SWITCHBOARD_HEADLESS_CLAUDE_MODEL:-opus}" --effort "${SWITCHBOARD_HEADLESS_CLAUDE_EFFORT:-high}" "$prompt"
