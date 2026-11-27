#!/usr/bin/env bash
# Hourly workstream context snapshot
# Creates a consolidated snapshot of all workstream state, to-dos, and memory
# so that after a crash, a new session can quickly reload context.
set -euo pipefail
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

# Work hours guard, local time: SWITCHBOARD_SNAPSHOT_START_HOUR (default 6) to
# SWITCHBOARD_SNAPSHOT_END_HOUR (default 20).
CURRENT_HOUR=$((10#$(date +%H)))
START_HOUR="${SWITCHBOARD_SNAPSHOT_START_HOUR:-6}"
END_HOUR="${SWITCHBOARD_SNAPSHOT_END_HOUR:-20}"
if [ "$CURRENT_HOUR" -lt "$START_HOUR" ] || [ "$CURRENT_HOUR" -ge "$END_HOUR" ]; then
  exit 0
fi

DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
SWITCHBOARD_DIR="$(dirname "$DAEMON_DIR")"
STATE_DIR="$SWITCHBOARD_DIR/.state"
LOG_FILE="$STATE_DIR/context-snapshot.log"
# The snapshot dir this script prunes is declared in system/config/state-pruned.txt
# with every other scheduled deleter under .state (check-state-inventory.py skips
# those paths). No row, no snapshot: better a missing snapshot than an undeclared
# delete.
STATE_PRUNED_FILE="$SWITCHBOARD_DIR/system/config/state-pruned.txt"
snapshot_rel=$(awk -v me="context-snapshot.sh" '$1 == me && $2 ~ /\/$/ {print $2}' "$STATE_PRUNED_FILE" 2>/dev/null || true)
if [ "$(printf '%s' "$snapshot_rel" | grep -c .)" -ne 1 ]; then
  echo "context-snapshot: $STATE_PRUNED_FILE must list exactly one dir row for context-snapshot.sh; no snapshot written" >&2
  exit 1
fi
SNAPSHOT_DIR="$STATE_DIR/${snapshot_rel%/}"
TODO_FILE="${SWITCHBOARD_ROOT:-$HOME/Development/switchboard}/system/state/todo.json"
TODO_CLI="$HOME/.agents/skills/todo/scripts/todo_cli.py"
WORKSTREAMS_DIR="$SWITCHBOARD_DIR/workstreams"

mkdir -p "$SNAPSHOT_DIR"
touch "$LOG_FILE"

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"
}

log "=== Context snapshot started ==="

TIMESTAMP=$(date '+%Y-%m-%dT%H:%M:%S')
SNAPSHOT_FILE="$SNAPSHOT_DIR/snapshot-latest.md"
SNAPSHOT_DATED="$SNAPSHOT_DIR/snapshot-$(date '+%Y%m%d-%H%M').md"

{
  echo "# Workstream Context Snapshot"
  echo "**Generated:** $TIMESTAMP"
  echo ""

  # ── Workstreams README (status table) ──
  echo "---"
  echo "## Workstream Status"
  echo ""
  if [ -f "$WORKSTREAMS_DIR/README.md" ]; then
    cat "$WORKSTREAMS_DIR/README.md"
  else
    echo "_No README.md found_"
  fi
  echo ""

  # ── Each active workstream's CONTEXT.md ──
  echo "---"
  echo "## Active Workstream Details"
  echo ""
  for ctx in "$WORKSTREAMS_DIR"/*/CONTEXT.md; do
    [ -f "$ctx" ] || continue
    ws_name=$(basename "$(dirname "$ctx")")
    # Skip archive
    [[ "$ws_name" == _* ]] && continue
    echo "### $ws_name"
    echo ""
    cat "$ctx"
    echo ""
    echo "---"
    echo ""
  done

  # ── To-dos ──
  echo "## Open To-Do Items"
  echo ""
  if [ -f "$TODO_FILE" ]; then
    # Verify completion evidence before rendering open items.
    python3 "$TODO_CLI" --path "$TODO_FILE" snapshot | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = [i for i in data.get('items', []) if i.get('bucket') != 'done']
items.sort(key=lambda x: (x.get('priority', 'P9'), x.get('due') or '9999'))
for i in items:
    p = i.get('priority', 'P3')
    due = i.get('due', 'no due')
    if i.get('bucket') == 'confirmation':
        evidence = (i.get('completion_evidence') or [{}])[0].get('detail', 'completion evidence found')
        print(f'- **{p} · confirm close** [{due}] {i[\"title\"]} — {evidence}')
    else:
        print(f'- **{p}** [{due}] {i[\"title\"]}')
if not items:
    print('_No open items_')
" 2>/dev/null || echo "_Failed to parse todo.json_"
  else
    echo "_No todo.json found_"
  fi
  echo ""

  # ── Recent memory observations (last 24h) ──
  echo "---"
  echo "## Recent Memory (last 24h)"
  echo ""
  MEMDB="$HOME/.amp/memory/amp-mem.db"
  if [ -f "$MEMDB" ]; then
    sqlite3 "$MEMDB" "
      SELECT topic || ': ' || summary
      FROM observations
      WHERE ts > datetime('now', '-24 hours')
      ORDER BY ts DESC
      LIMIT 20;
    " 2>/dev/null | while IFS= read -r line; do
      echo "- $line"
    done
  else
    echo "_Memory DB not found_"
  fi
  echo ""

} > "$SNAPSHOT_FILE"

# Keep a dated copy
cp "$SNAPSHOT_FILE" "$SNAPSHOT_DATED"

# Prune old snapshots (keep last 48 = ~2 days of hourly snapshots)
ls -1t "$SNAPSHOT_DIR"/snapshot-2*.md 2>/dev/null | tail -n +49 | xargs rm -f 2>/dev/null || true

log "Snapshot written to $SNAPSHOT_FILE ($(wc -l < "$SNAPSHOT_FILE") lines)"

# --commit: a render left uncommitted on main made the next task that also
# re-rendered the README fail to land as a stray write. The renderer writes only when the text changed and commits
# README.md alone, path-limited.
if python3 "$SWITCHBOARD_DIR/system/scripts/render-workstreams-readme.py" --root "$SWITCHBOARD_DIR" --write --commit >> "$LOG_FILE" 2>&1; then
  log "Refreshed generated workstreams README"
else
  log "WARN: Failed to refresh generated workstreams README"
fi

if python3 "$SWITCHBOARD_DIR/system/scripts/rebuild-switchboard-state.py" --write --event-source collector:context-snapshot >> "$LOG_FILE" 2>&1; then
  log "Refreshed switchboard queue from context snapshot"
else
  log "WARN: Failed to refresh switchboard queue"
fi

bash "$SWITCHBOARD_DIR/system/scripts/publish-switchboard-status.sh" >> "$LOG_FILE" 2>&1 || true

log "=== Context snapshot complete ==="
