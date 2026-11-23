#!/usr/bin/env bash
# refresh-todo-digest.sh — Render the current todo digest and surface it locally.

set -euo pipefail

# Keep the caller's PATH: on Windows, Task Scheduler gets Python only through
# switchboard-env.sh (SWITCHBOARD_TOOL_PATH), and a rendered C:/... entry splits on ":".
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PY="${SWITCHBOARD_PYTHON:-python3}"

REPO_ROOT="{{SWITCHBOARD_ROOT}}"
TODO_CLI="$HOME/.agents/skills/todo/scripts/todo_cli.py"
OUT_DIR="$REPO_ROOT/outputs/todo-refresh"
LATEST="$OUT_DIR/latest.md"
STAMPED="$OUT_DIR/$(date '+%Y-%m-%d-%H%M').md"
TS_LOCAL="$(date '+%Y-%m-%d %H:%M %Z')"
REMINDER_STATE="$REPO_ROOT/.state/todo-reminder-state.json"

mkdir -p "$OUT_DIR"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  echo "# Todo Refresh — $TS_LOCAL"
  echo
  "$PY" "$TODO_CLI" render-markdown --limit 5
  # Likely-resolved flags (doc updates / workstream channel activity after todo
  # creation) — present these as one-tap confirms, not open work. Flags are
  # produced by the flag-likely-resolved-todos background task.
  "$PY" "$REPO_ROOT/system/scripts/flag-likely-resolved-todos.py" --render-markdown 2>/dev/null || true
} > "$tmp"

cp "$tmp" "$STAMPED"
mv "$tmp" "$LATEST"
trap - EXIT

"$PY" - "$TODO_CLI" "$REMINDER_STATE" "$TS_LOCAL" <<'PY'
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

todo_cli = Path(sys.argv[1])
state_path = Path(sys.argv[2])
timestamp = sys.argv[3]
now = datetime.now(ZoneInfo("America/Los_Angeles"))
today = now.date().isoformat()

try:
    state = json.loads(state_path.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

slots = []
if 7 <= now.hour < 11:
    slots.append(("morning", "Today’s P1 focus", 3))
    if now.weekday() == 0:
        slots.append(("weekly", "Monday P2 review", 5))
if 16 <= now.hour < 20:
    slots.append(("eod", "P1s due within 48 hours", 3))

for mode, title, limit in slots:
    if state.get(mode) == today:
        continue
    completed = subprocess.run(
        [sys.executable, str(todo_cli), "reminders", "--mode", mode, "--limit", str(limit)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    items = payload.get("items") or []
    if items:
        body = "\n".join(f"{index}. {item['title']}" for index, item in enumerate(items, 1))
        subprocess.run(
            ["osascript", "-", title, timestamp, body],
            input="""
on run argv
  display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run
""",
            text=True,
            check=False,
        )
    state[mode] = today

state_path.parent.mkdir(parents=True, exist_ok=True)
temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(state, indent=2) + "\n")
os.replace(temporary, state_path)
PY

echo "todo-refresh: saved to $LATEST and $STAMPED"
