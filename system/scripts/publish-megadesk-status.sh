#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="$REPO_ROOT/system/state"
NOW_FILE="$STATE_DIR/now.json"
# Cache the last Megadesk tile payload so background refreshes can skip
# redundant cmux writes when the visible state is unchanged.
CACHE_FILE="$STATE_DIR/megadesk-status-published.json"
# Overridable so hermetic tests can point at a stub (or a missing path).
CMUX="${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
PENDING_FILE="$REPO_ROOT/.state/pending-amp-notifications.jsonl"

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

# Benign skips exit 0 but say why on stderr, so log-capturing callers
# (post-ingest, run-background-tasks) can tell "nothing to do" from a real
# failure. Actual cmux write failures exit 2 (see bottom).
skip() {
  echo "[publish-megadesk-status] skip: $1" >&2
  exit 0
}

[ -x "$CMUX" ] || skip "cmux binary not available at $CMUX"
[ -f "$NOW_FILE" ] || skip "now.json missing (run rebuild-megadesk-state.py --write first)"
cmux_dynamic_ui_updates_allowed || skip "dynamic cmux UI updates disabled in this context"

# Sidebar contract for CoS mirrors workstreams:
# - description = current focus / next action
# - owner row = driver when useful
# - signal row = state
# - no separate action row

WS_REF=$(find_workspace_ref_for_source_key "routine" "megadesk" 2>/dev/null || true)
[ -n "$WS_REF" ] || skip "no megadesk routine workspace open in cmux"

arrange_workspace_sidebar >/dev/null 2>&1 || true

STATUS_PAYLOAD=$(python3 - "$NOW_FILE" "$PENDING_FILE" "$CACHE_FILE" <<'PY'
from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

now_path = Path(sys.argv[1])
pending_path = Path(sys.argv[2])
cache_path = Path(sys.argv[3])
payload = json.loads(now_path.read_text())
current = payload.get("now") or {}
pending = 0
if pending_path.exists():
    pending = len([line for line in pending_path.read_text().splitlines() if line.strip()])
attention = payload.get("attention", "low")
if pending:
    attention = "high"
requires_user = bool(current.get("requires_user"))
colors = {"high": "#FF5F56", "medium": "#FFBD2E", "low": "#27C93F"}
fills = {"megadesk": "#363B43"}
URL_RE = re.compile(r"https?://\S+")
MARKUP_RE = re.compile(r"[*_~#>]+")


def shorten(text: str | None, limit: int = 48) -> str:
    text = text or ""
    text = URL_RE.sub("", text)
    text = MARKUP_RE.sub("", text)
    text = text.replace("workstreams/", "")
    text = text.replace("routines/", "")
    text = text.replace("projects/", "")
    text = " ".join(text.split()).strip(" :-")
    if not text:
        return "No queued action"
    for splitter in (". ", "; ", ", then ", " then "):
        if splitter in text:
            text = text.split(splitter, 1)[0]
            break
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def wrap_text(text: str | None, width: int = 30, max_lines: int = 4, prefix: str = "") -> str:
    text = text or ""
    text = URL_RE.sub("", text)
    text = MARKUP_RE.sub("", text)
    text = text.replace("workstreams/", "")
    text = text.replace("routines/", "")
    text = text.replace("projects/", "")
    text = " ".join(text.split()).strip(" :-")
    if not text:
        return prefix.strip()
    wrapped = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = shorten(wrapped[-1], limit=max(12, width - 1))
    if prefix:
        wrapped[0] = f"{prefix}{wrapped[0]}"
    return "\n".join(wrapped)


def action_treatment(attention: str, requires_user: bool, has_title: bool):
    if attention == "high" and not has_title:
        return "🟥 Blocked", "#FFB3AC"
    if requires_user and attention != "low":
        return "👤 You", "#FFE0A6"
    if attention in {"high", "medium"}:
        return "🤖 Agent", "#C2EAFF"
    return "📚 Reference", "#BDF5C2"


def state_label(attention: str, requires_user: bool, has_title: bool):
    if attention == "high" and not has_title:
        return "🟥 Blocked", "#FFB3AC"
    if attention in {"high", "medium"} and requires_user:
        return "🟦 Waiting", "#C2EAFF"
    if attention in {"high", "medium"}:
        return "🟨 Active", "#FFF0A8"
    return "🟩 Reference", "#BDF5C2"

if attention == "high" and not current.get("title"):
    signal = "🟥 Blocked"
    signal_color = "#FFB3AC"
    owner = "🤝 Shared"
    owner_color = "#FFB3AC"
elif requires_user and attention != "low":
    signal = "🟧 Needs you"
    signal_color = "#FFE0A6"
    owner = "👤 You"
    owner_color = "#FFE0A6"
elif attention in {"high", "medium"}:
    signal = "🟨 In motion"
    signal_color = "#FFF0A8"
    owner = "🤖 Agent"
    owner_color = "#FFF0A8"
else:
    signal = "🟩 Stable"
    signal_color = "#BDF5C2"
    owner = "⚙️ System"
    owner_color = "#BDF5C2"

focus_label = current.get("display_name") or current.get("workspace") or "Idle"
estimate = current.get("estimate_min")
if requires_user and estimate:
    eta_label = f"{estimate}m"
else:
    eta_label = ""

status_summary = shorten(current.get("title")) if current.get("title") else (f"Next focus: {focus_label}" if focus_label and focus_label != "Idle" else "Queue review")
action_label, action_color = action_treatment(attention, requires_user, bool(current.get("title")))
signal, signal_color = state_label(attention, requires_user, bool(current.get("title")))
owner = action_label
owner_color = action_color

queue_label = f"{payload.get('queue_size', 0)} queued"
status_payload = {
    "signal": signal,
    "owner": owner,
    "eta_label": eta_label,
    "status_summary": wrap_text(status_summary, width=30, max_lines=4),
    "queue_label": queue_label,
    "signal_color": signal_color,
    "owner_color": owner_color,
    "workspace_color": fills["megadesk"] if attention == "high" and not current.get("title") else "#363B43",
}

try:
    previous_payload = json.loads(cache_path.read_text()) if cache_path.exists() else None
except json.JSONDecodeError:
    previous_payload = None

if previous_payload == status_payload:
    print("")
else:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(status_payload, indent=2) + "\n")
    tmp_path.replace(cache_path)
    print(json.dumps(status_payload))
PY
)

# Track individual tile-write failures instead of discarding them: a stale
# Megadesk tile with a green exit code is exactly the silent-drop failure mode
# this surface exists to prevent.
WRITE_FAILURES=0
tile_write() {
  local label="$1"
  shift
  if ! "$@" >/dev/null 2>&1; then
    echo "[publish-megadesk-status] WARNING: $label failed for $WS_REF" >&2
    ((WRITE_FAILURES += 1)) || true
  fi
}

if [[ -n "$STATUS_PAYLOAD" ]]; then
  # Python, not jq: jq is not on a fresh Windows install.
  {
    IFS= read -r SIGNAL; IFS= read -r OWNER; IFS= read -r ETA_LABEL; IFS= read -r STATUS_SUMMARY
    IFS= read -r QUEUE_LABEL; IFS= read -r SIGNAL_COLOR; IFS= read -r OWNER_COLOR; IFS= read -r WORKSPACE_COLOR
  } < <("${PM_OS_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/lib/json_get.py" --lines \
          "signal=🟩 Stable" "owner=⚙️ System" "eta_label=" "status_summary=▶ No queued action" \
          "queue_label=0 queued" "signal_color=#27C93F" "owner_color=#27C93F" "workspace_color=#363B43" \
          <<< "$STATUS_PAYLOAD")

  tile_write "set-status signal" cmux_cmd set-status signal "$SIGNAL" --workspace "$WS_REF" --color "$SIGNAL_COLOR"
  tile_write "clear-status action" cmux_cmd clear-status action --workspace "$WS_REF"
  tile_write "clear-status focus" cmux_cmd clear-status focus --workspace "$WS_REF"
  if [[ -n "$OWNER" ]]; then
    tile_write "set-status owner" cmux_cmd set-status owner "$OWNER" --workspace "$WS_REF" --color "$OWNER_COLOR"
  else
    tile_write "clear-status owner" cmux_cmd clear-status owner --workspace "$WS_REF"
  fi
  if [[ -n "$ETA_LABEL" ]]; then
    tile_write "set-status eta" cmux_cmd set-status eta "$ETA_LABEL" --workspace "$WS_REF" --color "#D7DEE7"
  else
    tile_write "clear-status eta" cmux_cmd clear-status eta --workspace "$WS_REF"
  fi
  tile_write "set-status queue" cmux_cmd set-status queue "$QUEUE_LABEL" --workspace "$WS_REF" --color "#D7DEE7"
  tile_write "set-description" cmux_cmd workspace-action --workspace "$WS_REF" --action set-description --description "$STATUS_SUMMARY"
  # Do NOT paint the workspace accent color here. The workspace
  # publisher's coverage backfill mirrors the signal-tile color onto the
  # accent for every workspace, so painting $WORKSPACE_COLOR (#363B43,
  # unmapped in the sidebar's bucketOf → idle) here just fought that backfill
  # and made the CoS row's color flip every pass. The signal tile above is
  # the single source of truth for this row's color.
fi

# The workspace publisher is not a tile write: it exits 75 when another
# publisher (emit-workspace-update, a daemon) already holds the flock and
# will publish the same current state files. Counting that as a failed
# write would report a stale tile that had in fact been written. Only a real publisher error counts.
WORKSPACE_PUBLISH_RC=0
WORKSPACE_PUBLISH_ERR=$(bash "$REPO_ROOT/system/scripts/publish-workspace-status.sh" 2>&1 >/dev/null) || WORKSPACE_PUBLISH_RC=$?
if (( WORKSPACE_PUBLISH_RC == 75 )); then
  echo "[publish-megadesk-status] skip: workspace publisher busy (another publisher holds the lock and publishes the current state files)" >&2
elif (( WORKSPACE_PUBLISH_RC != 0 )); then
  echo "[publish-megadesk-status] WARNING: publish-workspace-status.sh exited $WORKSPACE_PUBLISH_RC for $WS_REF: $(printf '%s' "$WORKSPACE_PUBLISH_ERR" | tail -n 3 | tr '\n' ' ')" >&2
  ((WRITE_FAILURES += 1)) || true
fi

if (( WRITE_FAILURES > 0 )); then
  echo "[publish-megadesk-status] WARNING: $WRITE_FAILURES status write(s) failed; the Megadesk tile may be stale" >&2
  exit 2
fi
