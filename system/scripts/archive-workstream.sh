#!/usr/bin/env bash
# archive-workstream.sh — Move a workstream to workstreams/_archive/ and
# refresh the README index.
#
# Called by pm-os-guardrails plugin when a workstream's config.yaml status
# flips to Done/Cancelled/Resolved.
#
# Usage: archive-workstream.sh <slug>
#
# Idempotent: if the slug is already archived or doesn't exist, exits 0.

set -euo pipefail

PM_OS_DIR="${PM_OS_DIR:-${PM_OS_ROOT:-$HOME/Development/pm-os}}"
LOG_FILE="$PM_OS_DIR/.state/archive-workstream.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"
}

slug="${1:-}"
if [ -z "$slug" ]; then
  echo "Usage: archive-workstream.sh <slug>" >&2
  exit 1
fi

src="$PM_OS_DIR/workstreams/$slug"
dst_root="$PM_OS_DIR/workstreams/_archive"

if [ ! -d "$src" ]; then
  log "skip: $slug not found at $src"
  exit 0
fi

if [ -d "$dst_root/$slug" ]; then
  log "skip: $slug already in _archive"
  exit 0
fi

mkdir -p "$dst_root"
mv "$src" "$dst_root/$slug"
log "archived: $slug → _archive/"

# Keep open todos visible after their owning workstream leaves the active tree.
TODO_FILE="${TODO_FILE:-${PM_OS_ROOT:-$HOME/Development/pm-os}/system/state/todo.json}"
if [ -f "$TODO_FILE" ]; then
  python3 - "$TODO_FILE" "$slug" <<'PY' >> "$LOG_FILE" 2>&1
import json
import sys
from datetime import date
from pathlib import Path

path = Path(sys.argv[1])
slug = sys.argv[2]
payload = json.loads(path.read_text())
terminal = {"done", "completed", "complete", "closed", "resolved", "cancelled", "canceled", "dropped"}
note = f"workstream archived {date.today().isoformat()}"
matched = 0
for item in payload.get("items", []):
    if not isinstance(item, dict) or str(item.get("status") or "open").strip().lower() in terminal:
        continue
    if str(item.get("workstream") or "").strip() != slug:
        continue
    context = str(item.get("context") or "").strip()
    if note not in context:
        item["context"] = f"{context} | {note}" if context else note
    item["workstream"] = ""
    matched += 1
if matched:
    path.write_text(json.dumps(payload, indent=2) + "\n")
print(f"todo reconcile: cleared {matched} open link(s) for {slug}")
PY
fi

# Drop the workstream's entries from sidebar-publisher state so
# publish-workspace-status.sh stops retrying a closed workspace forever.
for STATE_FILE in \
  "$PM_OS_DIR/system/state/sources/workspace-updates.json" \
  "$PM_OS_DIR/system/state/sources/workspace-status-published.json"; do
  if [ -f "$STATE_FILE" ]; then
    python3 - "$STATE_FILE" "$slug" <<'PY' >> "$LOG_FILE" 2>&1
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("PM_OS_SCRIPTS_DIR") or _os.path.join(_os.environ.get("PM_OS_ROOT") or _os.path.expanduser("~/Development/pm-os"), "system", "scripts"))
    import flock_compat as fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
slug = sys.argv[2]
key = f"workstream:{slug}"
lock_path = path.with_name(f".{path.name}.lock")
lock_path.parent.mkdir(parents=True, exist_ok=True)
removed = 0
with lock_path.open("w") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    payload = json.loads(path.read_text())
    for section in ("updates", "published"):
        entries = payload.get(section)
        if isinstance(entries, dict) and key in entries:
            del entries[key]
            removed += 1
    if isinstance(payload, dict) and key in payload:
        del payload[key]
        removed += 1
    if removed:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
print(f"state prune: removed {removed} entr{'y' if removed == 1 else 'ies'} for {key} from {path.name}")
PY
  fi
done

# Refresh the workstreams README table
if [ -x "$PM_OS_DIR/system/scripts/render-workstreams-readme.py" ] || \
   [ -f "$PM_OS_DIR/system/scripts/render-workstreams-readme.py" ]; then
  python3 "$PM_OS_DIR/system/scripts/render-workstreams-readme.py" >> "$LOG_FILE" 2>&1 || \
    log "warn: render-workstreams-readme.py failed (non-fatal)"
fi

# Drop a one-line note in today's daily log if the directory exists
TODAY="$(date +%Y-%m-%d)"
DAILY="$PM_OS_DIR/notes/daily/$TODAY.md"
if [ -d "$PM_OS_DIR/notes/daily" ] && [ -f "$DAILY" ]; then
  printf '\n- Archived workstream: %s (auto, via pm-os-guardrails plugin)\n' "$slug" >> "$DAILY"
fi

echo "Archived $slug"
