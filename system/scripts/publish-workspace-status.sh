#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UPDATES_FILE="$REPO_ROOT/system/state/sources/workspace-updates.json"
QUEUE_FILE="$REPO_ROOT/system/state/queue.json"
# Cache the last rendered per-workspace payload so steady-state refreshes only
# touch cmux for rows whose visible sidebar state actually changed.
CACHE_FILE="$REPO_ROOT/system/state/sources/workspace-status-published.json"
STAGED_CACHE_FILE="$CACHE_FILE.staged.$$"
SUCCESS_KEYS_FILE="$CACHE_FILE.success.$$"
CMUX="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"

trap 'rm -f "$STAGED_CACHE_FILE" "$SUCCESS_KEYS_FILE"' EXIT

[ -x "$CMUX" ] || exit 0
[ -f "$UPDATES_FILE" ] || exit 0

# Keep a kernel lock across publication. Never steal an old owner's lock.
LOCK_DIR="$REPO_ROOT/system/state/.publish-workspace-status.lock"
if [[ -e "$LOCK_DIR" ]]; then
    echo "Legacy publisher lock requires owner verification" >&2
    exit 75
fi
# Several daemons (amp-notify, context-snapshot, meeting jobs) and every
# emit-workspace-update call publish concurrently. Failing fast on a held lock
# would leave the tile stale, so wait briefly for the in-flight publisher; the
# state files are already current, so this run publishes them. Exit 75 only
# when the lock is still held after the bounded wait.
# shellcheck source=lib/dir-lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/dir-lock.sh"
if pm_os_is_windows_shell; then
    if ! pm_os_dir_lock_acquire "$LOCK_DIR.flock.win" "${PUBLISH_WORKSPACE_STATUS_LOCK_WAIT_SECONDS:-15}"; then
        echo "publish-workspace-status: another publisher held the lock" >&2
        exit 75
    fi
    trap 'rm -f "$STAGED_CACHE_FILE" "$SUCCESS_KEYS_FILE"; pm_os_dir_lock_release' EXIT
else
exec 9>>"$LOCK_DIR.flock"
if ! python3 - "${PUBLISH_WORKSPACE_STATUS_LOCK_WAIT_SECONDS:-15}" <<'PY'
# pm-os: posix-only (the pm_os_is_windows_shell branch above takes lib/dir-lock.sh)
import fcntl
import sys
import time

deadline = time.monotonic() + float(sys.argv[1])
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.25)
PY
then
    echo "publish-workspace-status: another publisher held the lock for ${PUBLISH_WORKSPACE_STATUS_LOCK_WAIT_SECONDS:-15}s; skipped" >&2
    exit 75
fi
fi

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"
cmux_dynamic_ui_updates_allowed || exit 0

# Workspaces review-pinned by the review-lane amp plugin hold a purple
# "to review" color until the user explicitly marks them read in the sidebar.
# While that purple is live we must not overwrite the workspace's color, or
# the sidebar row would fall out of the "To review" lane before it was read.
#
# A pin can land mid-run (the plugin fires on agent.end, any time), so live
# colours are re-read right before the backfill loop, closing the race to the
# length of that loop.
# The publisher is NOT a review-state writer. The review purple (#BB9AF7) on a
# workspace's `color` is the sidebar's "To review / unread" fact; only the
# review-lane plugin (agent completion) and the user's explicit Mark unread set
# it, and only the user's explicit Mark read clears it.
# `publish_workspace_color` never writes purple, never mirrors a purple
# "🟪 To review" signal colour onto the workspace, and never overwrites a live
# purple. The review-pin marker file is only consulted by the plugin itself.
REVIEW_HOLD_COLOR="#BB9AF7"
# Accent used in place of a purple signal colour so every row keeps a line.
REVIEW_FALLBACK_COLOR="#BDF5C2"
LIVE_COLOR_LISTING=""

refresh_live_workspace_colors() {
    # "ref<TAB>color" per line, from the live workspace list (custom_color).
    LIVE_COLOR_LISTING=$(CMUX_QUIET=1 cmux_cmd rpc workspace.list 2>/dev/null \
        | "${PM_OS_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/lib/json_get.py" --tsv "workspaces|." ref custom_color 2>/dev/null || true)
}

live_workspace_color() {
    printf '%s\n' "$LIVE_COLOR_LISTING" | awk -F '\t' -v ref="$1" '$1 == ref { print $2; exit }'
}

is_review_held() {
    local color
    color=$(live_workspace_color "$1")
    [[ "$(printf '%s' "$color" | tr '[:lower:]' '[:upper:]')" == "$REVIEW_HOLD_COLOR" ]]
}

# Usage: publish_workspace_color <ref> <color>. Returns non-zero only when the
# cmux write itself failed (a skipped write is a success).
publish_workspace_color() {
    local ws_ref="$1" color="$2"
    if [[ "$(printf '%s' "$color" | tr '[:lower:]' '[:upper:]')" == "$REVIEW_HOLD_COLOR" ]]; then
        # Never publish the review hold from status data. A "To review"
        # signal still gets a neutral accent so the row keeps its line.
        color="$REVIEW_FALLBACK_COLOR"
    fi
    # Re-read live colours right before the write: the review-lane plugin
    # pins at agent.end, any time, and a purple that landed since the last
    # listing must not be overwritten (race shrinks to one rpc round-trip).
    refresh_live_workspace_colors
    if is_review_held "$ws_ref"; then
        # Live purple = unread/To review. Only Mark read may clear it.
        return 0
    fi
    cmux_cmd workspace-action --workspace "$ws_ref" --action set-color --color "$color" >/dev/null 2>&1
}

# Sidebar contract:
# - description = next concrete action
# - owner row = driver (you/agent/others/reference)
# - signal row = state (needs review/active/waiting/reference)
# - no separate "up next" row
# Keep the sidebar order stable as queue/status updates stream in.
arrange_workspace_sidebar >/dev/null 2>&1 || true

STATUS_ROWS=$(python3 - "$UPDATES_FILE" "$QUEUE_FILE" "$CACHE_FILE" "$STAGED_CACHE_FILE" <<'PY'
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
import re
import textwrap
from pathlib import Path

updates_path = sys.argv[1]
queue_path = sys.argv[2]
cache_path = Path(sys.argv[3])
staged_cache_path = Path(sys.argv[4])

# After this many minutes with no fresh update, an "active" (in-motion) signal
# decays to 🟩 Stable so stale yellow lines don't linger on idle tabs.
DECAY_MIN = 30
INTENTIONAL_QUEUE_OVERRIDE_MIN = 48 * 60
INTENTIONAL_DECAY_MIN = 7 * 24 * 60


def _update_age_min(update: dict | None) -> float | None:
    if not update:
        return None
    ts = update.get("timestamp")
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 60.0

with open(updates_path) as handle:
    updates_payload = json.load(handle)

try:
    with open(queue_path) as handle:
        queue_payload = json.load(handle)
except FileNotFoundError:
    queue_payload = {"queue": []}

ROUTINE_SLUGS = {"todo", "comms-triage", "meetings", "scheduled-jobs"}
COLOR = {
    "red": "#FFB3AC",
    "orange": "#FFE0A6",
    "yellow": "#FFF0A8",
    "blue": "#C2EAFF",
    "green": "#BDF5C2",
    # "To review" accent — matches the pm-os.swift To review lane color so
    # calm review-and-approve items land there instead of Needs attention.
    "purple": "#BB9AF7",
    # Paused gets its own accent so the custom sidebar (pm-os.swift) can
    # bucket paused separately from waiting — the workspace color is the
    # only signal channel custom sidebars can read.
    "gray": "#D7DEE7",
}
FILL = {
    "neutral": "#363B43",
    "urgent": "#6A2F3A",
}
URL_RE = re.compile(r"https?://\S+")
MARKUP_RE = re.compile(r"[*_~#>]+")

queue_by_workspace = {item.get("workspace"): item for item in queue_payload.get("queue", []) if item.get("workspace")}
queue_generated_at = queue_payload.get("generated_at")
updates_by_slug = defaultdict(dict)

for source, payload in (updates_payload.get("updates") or {}).items():
    if ":" not in source:
        continue
    source_type, slug = source.split(":", 1)
    updates_by_slug[slug][source_type] = payload


def pick_update(slug: str):
    typed = updates_by_slug.get(slug, {})
    if slug in ROUTINE_SLUGS and "routine" in typed:
        return typed["routine"], "routine"
    if slug not in ROUTINE_SLUGS and "workstream" in typed:
        return typed["workstream"], "workstream"
    latest = None
    latest_type = None
    for payload in typed.values():
        if latest is None or (payload.get("timestamp") or "") > (latest.get("timestamp") or ""):
            latest = payload
            latest_type = payload.get("source", "").split(":", 1)[0] or None
    return latest, latest_type


def normalize_text(text: str | None) -> str:
    text = text or ""
    text = URL_RE.sub("", text)
    text = MARKUP_RE.sub("", text)
    text = text.replace("workstreams/", "")
    text = text.replace("routines/", "")
    text = text.replace("projects/", "")
    text = text.replace("notes/", "")
    return " ".join(text.split()).strip(" :-")


def shorten(text: str | None, limit: int = 46) -> str:
    text = normalize_text(text)
    if not text:
        return "No next action captured"
    for splitter in (". ", "; ", ", then ", " then "):
        if splitter in text:
            text = text.split(splitter, 1)[0]
            break
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def wrap_text(text: str | None, width: int = 28, max_lines: int = 4, prefix: str = "") -> str:
    text = normalize_text(text)
    if not text:
        return prefix.strip()
    wrapped = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    if not wrapped:
        return prefix.strip()
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = shorten(wrapped[-1], limit=max(12, width - 1))
    if prefix:
        wrapped[0] = f"{prefix}{wrapped[0]}"
    return "\n".join(wrapped)


def choose_context_text(update: dict | None, queue_item: dict | None, raw_status: str) -> str:
    update = update or {}
    queue_item = queue_item or {}
    if raw_status in {"needs action", "needs review", "to review"}:
        text = normalize_text(queue_item.get("title") or update.get("recommended_next_action") or update.get("summary"))
    elif raw_status in {"waiting", "paused", "scheduled", "needs follow-up"}:
        text = normalize_text(update.get("summary") or update.get("recommended_next_action") or queue_item.get("title"))
    else:
        text = normalize_text(update.get("summary") or update.get("recommended_next_action") or queue_item.get("title"))
    if not text:
        return ""

    for marker in (
        "Remaining external dependencies are ",
        "remaining external dependencies are ",
        "Current picture:",
        "current picture:",
        "Current status:",
        "current status:",
    ):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break

    clauses = [clause.strip(" ,") for clause in re.split(r";\s+", text) if clause.strip()]
    if clauses:
        text = clauses[-1] if raw_status in {"waiting", "paused", "scheduled"} and len(clauses) > 1 else clauses[0]

    if raw_status in {"waiting", "paused", "scheduled"} and " with " in text:
        tail = text.rsplit(" with ", 1)[1].strip()
        if len(tail.split()) >= 3:
            text = tail

    if ": " in text and len(text.split(": ", 1)[0].split()) <= 4:
        text = text.split(": ", 1)[1].strip()

    text = re.sub(
        r"^(confirmed|updated|finished|adjusted|paused after|condensed|captured|moved|saved|reconciled|resume by|resume when|resume in|resume)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def build_status_summary(update: dict | None, queue_item: dict | None, raw_status: str) -> str:
    queue_item = queue_item or {}
    update = update or {}
    action_text = normalize_text(queue_item.get("title") or update.get("recommended_next_action") or update.get("summary"))
    if not action_text:
        action_text = {
            "blocked": "Review the blocker and decide the next move",
            "needs action": "Review this item and decide the next move",
            "needs review": "Review this item and decide the next move",
            "to review": "Review this item and decide the next move",
            "active": "Continue the current work",
            "new": "Start the current work",
            "waiting": "Check back on the pending dependency",
            "paused": "Resume when the dependency is unblocked",
            "scheduled": "Pick this back up at the scheduled time",
            "needs follow-up": "Follow up on the pending dependency",
            "reference": "Reference context only",
            "done": "Reference context only",
            "resolved": "Reference context only",
        }.get(raw_status, "Review the current context")
    return wrap_text(action_text, width=28, max_lines=4)


def build_driver_label(raw_status: str, requires_user: bool) -> tuple[str, str]:
    if requires_user or raw_status in {"needs action", "needs review", "to review"}:
        return "👤 You", COLOR["orange"]
    if raw_status in {"active", "new"}:
        return "🤖 Agent", COLOR["blue"]
    if raw_status in {"waiting", "paused", "scheduled", "needs follow-up", "blocked"}:
        return "🤝 Others", "#D7DEE7"
    return "📚 Reference", COLOR["green"]


def build_state_label(raw_status: str) -> tuple[str, str]:
    if raw_status == "blocked":
        return "🟥 Blocked", COLOR["red"]
    if raw_status in {"needs action", "needs review"}:
        return "🟥 Needs review", COLOR["red"]
    if raw_status == "to review":
        return "🟪 To review", COLOR["purple"]
    if raw_status in {"active", "new"}:
        return "🟨 Active", COLOR["yellow"]
    if raw_status == "paused":
        return "⏸ Paused", COLOR["gray"]
    if raw_status in {"waiting", "scheduled", "needs follow-up"}:
        return "🟦 Waiting", COLOR["blue"]
    if raw_status in {"reference", "done", "resolved"}:
        return "🟩 Reference", COLOR["green"]
    return "🟩 Stable", COLOR["green"]


def workspace_fill_color(slug: str, queue_item: dict | None, raw_status: str) -> str:
    if raw_status in {"blocked", "needs action", "needs review"}:
        return FILL["urgent"]
    return FILL["neutral"]


def derive_status(slug: str, update: dict | None, queue_item: dict | None):
    update = update or {}
    queue_item = queue_item or {}
    update_status = str(update.get("status") or "").lower()
    queue_status = str(queue_item.get("status") or "").lower()
    intentional_statuses = {
        "blocked", "needs action", "needs review", "to review",
        "waiting", "paused", "scheduled", "needs follow-up",
    }
    raw_status = queue_status or update_status or "idle"
    stale_intentional = False
    if update_status in intentional_statuses:
        raw_status = update_status
        age = _update_age_min(update)
        update_timestamp = str(update.get("timestamp") or "")
        if (
            age is not None
            and age > INTENTIONAL_QUEUE_OVERRIDE_MIN
            and queue_status
            and queue_status != update_status
            and queue_generated_at
            and str(queue_generated_at) > update_timestamp
        ):
            raw_status = queue_status
        elif age is not None and age > INTENTIONAL_DECAY_MIN:
            raw_status = "idle"
            stale_intentional = True
    requires_user = bool(queue_item.get("requires_user"))
    estimate = queue_item.get("estimate_min")

    # Decay stale in-motion signals: if the only freshness signal is an
    # update (no live queue entry) and that update is older than DECAY_MIN,
    # relax "active"/"new" to idle so the yellow line doesn't linger forever.
    if not queue_item and raw_status in {"active", "new"}:
        age = _update_age_min(update)
        if age is not None and age > DECAY_MIN:
            raw_status = "idle"

    # Waiting-style statuses whose next action is explicitly addressed to
    # the owner are not blocked on others — the ball is with the user. Surface
    # them as to review (purple / 👤 You) so the sidebar puts them under
    # To review instead of Waiting. Needs attention (red) stays reserved
    # for blocked / needs action / needs review states.
    # Exception: updates emitted during a machine-seeded startup
    # turn (provenance=startup_hydration, stamped by emit-workspace-update.py)
    # routinely phrase next actions as "the owner should …" without any genuine
    # escalation — never let phrasing alone move a startup update to purple.
    hydration_seeded = str(update.get("provenance") or "") == "startup_hydration"
    if raw_status in {"waiting", "scheduled", "needs follow-up"} and not hydration_seeded:
        action_text = str(
            queue_item.get("title") or update.get("recommended_next_action") or ""
        ).lower()
        if (
            "owner" in action_text
            or action_text.startswith("you")
        ):
            raw_status = "to review"
            requires_user = True

    if raw_status in {"waiting", "paused", "scheduled", "needs follow-up", "reference", "done", "resolved"}:
        eta = ""
    elif isinstance(estimate, int):
        eta = f"{estimate}m"
    else:
        # No estimate: clear the eta pill. A "--" placeholder never
        # publishes: `cmux set-status eta -- --workspace ...` reads the bare
        # "--" as end-of-options and rejects the call.
        eta = ""

    status_summary = build_status_summary(update, queue_item, raw_status)
    if stale_intentional:
        status_summary = f"{status_summary} (stale)"
    owner, owner_color = build_driver_label(raw_status, requires_user)
    signal, signal_color = build_state_label(raw_status)
    # The sidebar's left accent line is the workspace `color` field, so set it
    # to the signal color — this makes the line visible and state-accurate on
    # every tab (the old dark FILL color rendered as effectively no line).
    fill_color = signal_color
    return {
        "signal": signal,
        "signal_color": signal_color,
        "owner": owner,
        "owner_color": owner_color,
        "eta": eta,
        "status_summary": status_summary,
        "workspace_color": fill_color,
    }


all_slugs = set(queue_by_workspace) | set(updates_by_slug)
rows = []
for slug in sorted(all_slugs):
    update, source_type = pick_update(slug)
    queue_item = queue_by_workspace.get(slug)
    if not update and not queue_item:
        continue
    if source_type is None:
        source_type = "routine" if slug in ROUTINE_SLUGS else "workstream"
    derived = derive_status(slug, update, queue_item)
    rows.append({"slug": slug, "source_type": source_type, **derived})

try:
    previous_rows = json.loads(cache_path.read_text()) if cache_path.exists() else {}
except json.JSONDecodeError:
    previous_rows = {}

current_rows = {f"{row['source_type']}:{row['slug']}": row for row in rows}
changed_rows = [row for row in rows if previous_rows.get(f"{row['source_type']}:{row['slug']}") != row]

cache_path.parent.mkdir(parents=True, exist_ok=True)
staged_cache_path.parent.mkdir(parents=True, exist_ok=True)
staged_cache_path.write_text(json.dumps(current_rows, indent=2) + "\n")

print(json.dumps(changed_rows))
PY
)

# Steady state (no changed rows) still needs the coverage backfill below to
# run, so don't exit early — just skip the per-row publish loop.
if [[ "$STATUS_ROWS" != "[]" ]]; then
# Fetch the workspace listing once for the whole loop and resolve every row
# against it; a per-row RPC lookup adds seconds to every pass. Rows for
# workspaces opened mid-loop are simply picked up on the next pass. Rows whose
# workspace is closed are counted into one summary line instead of one stderr
# line each. Closed rows never reach SUCCESS_KEYS_FILE, so they stay "changed"
# and are re-checked on every pass; the pure-bash lookup in cmux-helpers.sh
# keeps that cheap enough to stay inside the 300s background-tasks timeout.
PUBLISH_LISTING=$(cmux_cmd list-workspaces 2>/dev/null || true)
PUBLISH_CLOSED_ROWS=0
refresh_live_workspace_colors
while IFS=$'\t' read -r SLUG SOURCE_TYPE SIGNAL SIGNAL_COLOR OWNER OWNER_COLOR ETA STATUS_SUMMARY_ESCAPED WS_COLOR; do
    [[ -z "$SLUG" ]] && continue
    # Bash collapses empty tab-delimited fields, which used to shift waiting
    # rows (empty ETA) and make status-summary/color land in the wrong columns.
    [[ "$ETA" == "__EMPTY__" ]] && ETA=""
    WS_REF=$(find_workspace_ref_for_source_key_in_listing "$PUBLISH_LISTING" "$SOURCE_TYPE" "$SLUG" 2>/dev/null || true)
    if [[ -z "$WS_REF" ]]; then
        PUBLISH_CLOSED_ROWS=$((PUBLISH_CLOSED_ROWS + 1))
        continue
    fi

    STATUS_SUMMARY=$(printf '%b' "$STATUS_SUMMARY_ESCAPED")

    ROW_OK=true
    cmux_cmd set-status signal "$SIGNAL" --workspace "$WS_REF" --color "$SIGNAL_COLOR" >/dev/null 2>&1 || ROW_OK=false
    cmux_cmd clear-status action --workspace "$WS_REF" >/dev/null 2>&1 || ROW_OK=false
    if [[ -n "$OWNER" ]]; then
        cmux_cmd set-status owner "$OWNER" --workspace "$WS_REF" --color "$OWNER_COLOR" >/dev/null 2>&1 || ROW_OK=false
    else
        cmux_cmd clear-status owner --workspace "$WS_REF" >/dev/null 2>&1 || ROW_OK=false
    fi
    if [[ -n "$ETA" ]]; then
        cmux_cmd set-status eta "$ETA" --workspace "$WS_REF" --color "#D7DEE7" >/dev/null 2>&1 || ROW_OK=false
    else
        cmux_cmd clear-status eta --workspace "$WS_REF" >/dev/null 2>&1 || ROW_OK=false
    fi
    cmux_cmd workspace-action --workspace "$WS_REF" --action set-description --description "$STATUS_SUMMARY" >/dev/null 2>&1 || ROW_OK=false
    publish_workspace_color "$WS_REF" "$WS_COLOR" || ROW_OK=false
    if [[ "$ROW_OK" == true ]]; then
        printf '%s:%s\n' "$SOURCE_TYPE" "$SLUG" >> "$SUCCESS_KEYS_FILE"
    else
        echo "workspace status publish failed: $SOURCE_TYPE:$SLUG (will retry)" >&2
    fi
done < <("${PM_OS_PYTHON:-python3}" -c '
import json, sys
for r in json.loads(sys.stdin.read() or "[]"):
    eta = r.get("eta")
    cells = [r.get("slug"), r.get("source_type"), r.get("signal"), r.get("signal_color"), r.get("owner"),
             r.get("owner_color"), "__EMPTY__" if eta == "" else eta, r.get("status_summary"), r.get("workspace_color")]
    print("\t".join("" if c is None else str(c).replace("\t", " ").replace("\n", " ") for c in cells))
' <<< "$STATUS_ROWS")
if (( PUBLISH_CLOSED_ROWS > 0 )); then
    echo "workspace status publish: skipped $PUBLISH_CLOSED_ROWS row(s) whose workspace is not open" >&2
fi
fi

python3 - "$CACHE_FILE" "$STAGED_CACHE_FILE" "$SUCCESS_KEYS_FILE" <<'PY'
import json
import sys
from pathlib import Path

cache_path, staged_path, success_path = map(Path, sys.argv[1:])
try:
    previous = json.loads(cache_path.read_text()) if cache_path.exists() else {}
except json.JSONDecodeError:
    previous = {}
current = json.loads(staged_path.read_text())
successes = set(success_path.read_text().splitlines()) if success_path.exists() else set()
final = {key: value for key, value in previous.items() if key in current}
for key in successes:
    if key in current:
        final[key] = current[key]
cache_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(final, indent=2) + "\n")
tmp_path.replace(cache_path)
PY

# Coverage backfill: guarantee every open workspace shows a left accent line.
# The sidebar's left line is the workspace `color` field, so sync each tab's
# color to its current `signal` color (idempotent, runs every pass). Tabs that
# never emitted a signal get a neutral 🟩 Stable + matching green color. This
# keeps the line present and state-accurate on ALL tabs, not just the ones the
# main loop touched, and works on steady-state passes too.
STABLE_SIGNAL="🟩 Stable"
STABLE_COLOR="#BDF5C2"
WS_LISTING=$(cmux_cmd list-workspaces 2>/dev/null || true)
# Re-read live colours right before the backfill: a review pin landing during
# the main loop (plugin fires on agent.end, any time) must not be overwritten.
refresh_live_workspace_colors
while IFS= read -r WS_REF; do
    [[ -z "$WS_REF" ]] && continue
    STATUS_DUMP=$(cmux_cmd list-status --workspace "$WS_REF" 2>/dev/null || true)
    SIGNAL_LINE=$(printf '%s\n' "$STATUS_DUMP" | grep '^signal=' | head -1 || true)
    OWNER_LINE=$(printf '%s\n' "$STATUS_DUMP" | grep '^owner=' | head -1 || true)
    if [[ -z "$SIGNAL_LINE" ]]; then
        # No signal at all → set a neutral stable line.
        cmux_cmd set-status signal "$STABLE_SIGNAL" --workspace "$WS_REF" --color "$STABLE_COLOR" >/dev/null 2>&1 || true
        publish_workspace_color "$WS_REF" "$STABLE_COLOR" || true
        SIGNAL_LINE="signal=$STABLE_SIGNAL color=$STABLE_COLOR"
    else
        # Mirror the signal's color onto the workspace accent line. A purple
        # signal is mapped to the neutral accent and a live purple is never
        # overwritten (see publish_workspace_color).
        SIG_COLOR=$(printf '%s' "$SIGNAL_LINE" | grep -oE 'color=#[0-9A-Fa-f]{6}' | head -1 | cut -d= -f2)
        [[ -z "$SIG_COLOR" ]] && SIG_COLOR="$STABLE_COLOR"
        publish_workspace_color "$WS_REF" "$SIG_COLOR" || true
    fi
    # Guarantee an owner row too, so every tab shows BOTH rows. If one is
    # already present (set by the main loop) leave it; otherwise derive the
    # owner from the current signal state.
    if [[ -z "$OWNER_LINE" ]]; then
        case "$SIGNAL_LINE" in
            *"Needs review"*|*"To review"*|*"Blocked"*) BF_OWNER="👤 You";       BF_OWNER_COLOR="#FFE0A6" ;;
            *"Active"*)                    BF_OWNER="🤖 Agent";     BF_OWNER_COLOR="#C2EAFF" ;;
            *"Waiting"*|*"Paused"*)        BF_OWNER="🤝 Others";    BF_OWNER_COLOR="#D7DEE7" ;;
            *)                             BF_OWNER="📚 Reference"; BF_OWNER_COLOR="#BDF5C2" ;;
        esac
        cmux_cmd set-status owner "$BF_OWNER" --workspace "$WS_REF" --color "$BF_OWNER_COLOR" >/dev/null 2>&1 || true
    fi
done < <(printf '%s\n' "$WS_LISTING" | grep -oE 'workspace:[0-9]+' | sort -u)
