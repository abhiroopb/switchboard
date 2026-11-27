#!/usr/bin/env bash
#
# command-center-action.sh
#
# Translate short the owner commands into cmux + workstream lifecycle actions.
#
# Usage:
#   command-center-action.sh open  <ws-num>
#   command-center-action.sh close <ws-num>           # wrap + close cmux tab
#   command-center-action.sh pause <slug>             # close cmux tab + mark Paused
#   command-center-action.sh archive <slug>           # close cmux tab + move to _archive/
#   command-center-action.sh wrap  <ws-num>           # send wrap-up prompt only, don't close
#   command-center-action.sh resolve <ws-num>         # print the tab a handle names; changes nothing
#
# Exit non-zero on error so the CoS agent can surface it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSTREAMS="$REPO_ROOT/workstreams"
# Render-time provenance for numeric handles, written atomically by
# command-center-digest.py on every render. CC_STATE_DIR is a test override.
ACTIONS_MAP="${CC_STATE_DIR:-$REPO_ROOT/system/state}/command-center-actions.json"
DIGEST_SCRIPT="${CC_DIGEST_SCRIPT:-$REPO_ROOT/system/scripts/command-center-digest.py}"

# Shared inject guard so wrap/close prompts don't clobber a draft the user is
# typing in the target workspace. Exposes cig_safe_send / cig_force_send.
# Sourced before the CMUX default because its mux_init points CMUX at the wmux
# shim on Windows. With the cmux.app default set first, a caller with no CMUX in
# its environment would reach nothing under wmux.
# shellcheck source=lib/cmux-inject-guard.sh
source "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh"
CMUX="${CMUX:-${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
ARCHIVE="$WORKSTREAMS/_archive"

action="${1:?usage: $0 <open|close|pause|archive|wrap|close-all> <ws-num-or-slug|rt-task-id> [--execute]}"
[[ "$action" == close && $# == 1 ]] && action=close-all
# close-all/end-of-day take no target (or an optional flag); everything else requires one.
case "$action" in
  close-all|end-of-day|close-of-day|eod) target="${2:-}" ;;
  *) target="${2:?missing target}" ;;
esac

ts() { date +"%H:%M:%S"; }

ws_ref_for_num() {
  local n="$1"
  echo "workspace:$n"
}

# Resolve and VALIDATE a numeric digest handle. cmux reuses workspace numbers,
# so a handle from a stale digest can point at a completely different
# workspace (`close 7` from a stale digest could close Scheduled Jobs after
# workspace:7 is reused). Cross-checks the workspace UUID
# recorded at render time against the live one; fails closed on any mismatch.
# Prints the ref on success.
# wmux numbers tabs from a counter that only goes up (cmux-shim.py
# allocate_workspace), so a wmux ref never names a second tab. There a handle
# the last render missed, such as a tab opened since the 15-minute tick, is safe
# to take from the live list: refresh the map and look again. cmux reuses
# numbers, so on cmux a missing handle still fails closed.
# The wmux guarantee holds while ~/.switchboard/wmux-sidecar.json keeps its counter:
# deleting that file restarts numbering at 1.
refs_never_reused() {
  [[ "${SWITCHBOARD_MUX:-}" == "wmux" ]]
}

refresh_actions_map() {
  CMUX_BIN="$CMUX" python3 "$DIGEST_SCRIPT" --actions-map-only --actions-file "$ACTIONS_MAP" >/dev/null
}

map_entry() {
  [[ -f "$ACTIONS_MAP" ]] || return 0
  python3 - "$ACTIONS_MAP" "$1" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
h = (data.get("handles") or {}).get(sys.argv[2])
if h:
    print((h.get("id") or "") + "\t" + (h.get("title") or ""))
PY
}

resolve_ws_handle() {
  local n="$1" ref="workspace:$1"
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then
    echo "ERROR: '$n' is not a tab number. Use the number from the digest, as in close 12." >&2
    return 1
  fi
  local expected expected_id expected_title
  expected="$(map_entry "$n")"
  if [[ -z "$expected" ]] && refs_never_reused; then
    if ! refresh_actions_map; then
      echo "ERROR: could not list open tabs to refresh $ACTIONS_MAP; nothing done for '$n'." >&2
      return 1
    fi
    expected="$(map_entry "$n")"
    if [[ -z "$expected" ]]; then
      echo "ERROR: no open tab is $ref. Refresh the digest and retry." >&2
      return 1
    fi
  fi
  if [[ ! -f "$ACTIONS_MAP" ]]; then
    echo "ERROR: no action map at $ACTIONS_MAP — cannot verify handle $n. Refresh the digest and retry." >&2
    return 1
  fi
  if [[ -z "$expected" ]]; then
    echo "ERROR: stale handle — the rendered digest does not offer '$n'. Refresh the digest and retry." >&2
    return 1
  fi
  expected_id="${expected%%$'\t'*}"
  expected_title="${expected#*$'\t'}"
  local live live_id live_title live_json
  live_json="$("$CMUX" rpc workspace.list '{}' 2>/dev/null || true)"
  live="$(CC_LIVE_JSON="$live_json" python3 - "$ref" <<'PY'
import json, os, sys
try:
    data = json.loads(os.environ.get("CC_LIVE_JSON") or "")
except Exception:
    sys.exit(0)
for w in data.get("workspaces", []):
    if (w.get("ref") or "") == sys.argv[1]:
        wid = (w.get("id") or "").strip()
        title = (w.get("custom_title") or w.get("title") or "").strip()
        print(wid + "\t" + title)
        break
PY
)"
  if [[ -z "$live" ]]; then
    echo "ERROR: $ref no longer exists (closed since the digest was rendered; it was '$expected_title'). Refresh the digest." >&2
    return 1
  fi
  live_id="${live%%$'\t'*}"
  live_title="${live#*$'\t'}"
  if [[ -z "$expected_id" || -z "$live_id" ]]; then
    echo "ERROR: missing workspace identity for $ref. Refresh the digest and retry." >&2
    return 1
  fi
  if [[ "$expected_id" != "$live_id" ]]; then
    echo "ERROR: stale handle — $ref was '$expected_title' when the digest was rendered but is now '$live_title' (number reused). Refusing to act; refresh the digest." >&2
    return 1
  fi
  echo "$ref"
}

slug_for_ref() {
  # Map cmux workspace title to workstream slug by looking up config.yaml name fields.
  local ref="$1"
  local title
  title="$($CMUX list-workspaces | awk -v r="$ref" '
    {
      gsub(/^[ \t*]+/, "", $0)
      if ($1 == r) {
        sub(r"[ \t]+", "", $0)
        sub(/[ \t]+\[selected\]$/, "", $0)
        print $0
        exit
      }
    }
  ')"
  # Strip leading non-word chars (emoji)
  local visible
  visible="$(echo "$title" | sed -E 's/^[[:space:]]*[^[:alnum:]]+//; s/[[:space:]]+$//')"
  if [[ -z "$visible" ]]; then
    return 1
  fi
  # Find matching workstream by name field (case-insensitive)
  for cfg in "$WORKSTREAMS"/*/config.yaml "$REPO_ROOT/routines"/*/config.yaml; do
    [[ -f "$cfg" ]] || continue
    local cfg_name
    cfg_name="$(grep -E '^name:' "$cfg" | head -1 | sed -E 's/^name:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
    if [[ "${cfg_name,,}" == "${visible,,}" ]]; then
      basename "$(dirname "$cfg")"
      return 0
    fi
  done
  return 1
}

ws_dir_for_slug() {
  local slug="$1"
  for parent in "$WORKSTREAMS" "$REPO_ROOT/routines"; do
    if [[ -d "$parent/$slug" ]]; then
      echo "$parent/$slug"
      return 0
    fi
  done
  return 1
}

# Slugify a cmux workspace title using the same recipe as
# command-center-digest.py:name_to_slug fallback (lower, strip non-word, hyphenate).
slugify_title() {
  python3 -c "
import re, sys
s = sys.argv[1]
s = re.sub(r'^[\\s\\W_]+', '', s).strip().lower()
s = re.sub(r'[^\\w\\s-]', '', s)
s = re.sub(r'\\s+', '-', s)
print(s)
" "$1"
}

# Given a slug, find the matching cmux workspace ref by slugifying every open
# workspace title and comparing. Returns empty if no match.
find_cmux_ref_by_slug() {
  local target="$1"
  "$CMUX" list-workspaces 2>/dev/null | while IFS= read -r line; do
    line_clean="$(echo "$line" | sed -E 's/^[ \t*]+//; s/[ \t]+\[selected\]$//')"
    ref="$(echo "$line_clean" | grep -oE '^workspace:[0-9]+' || true)"
    [[ -z "$ref" ]] && continue
    title="$(echo "$line_clean" | sed -E "s/^${ref}[[:space:]]+//")"
    candidate="$(slugify_title "$title")"
    if [[ "$candidate" == "$target" ]]; then
      echo "$ref"
      return 0
    fi
  done
}

workspace_title_for_ref() {
  local target_ref="$1"
  # $1 == r, never a prefix test: workspace:29 must not read workspace:290's title.
  "$CMUX" list-workspaces 2>/dev/null | awk -v r="$target_ref" '
    {
      gsub(/^[ \t*]+/, "", $0)
      if ($1 == r) {
        sub(r"[ \t]+", "", $0)
        sub(/[ \t]+\[selected\]$/, "", $0)
        print $0
        exit
      }
    }
  '
}

workspace_ref_exists() {
  local target_ref="$1" listing
  if ! listing="$("$CMUX" list-workspaces 2>/dev/null)"; then
    return 2
  fi
  printf '%s\n' "$listing" | awk -v r="$target_ref" '
    {
      gsub(/^[ \t*]+/, "", $0)
      if ($0 == r || index($0, r " ") == 1) { found = 1; exit }
    }
    END { exit(found ? 0 : 1) }
  '
}

close_workspace_verified() {
  local ref="$1" verify_status
  # --force: cmux 0.65 refuses to close a workspace with a running process
  # (every switchboard tab runs the Amp CLI) without it; the guards above already
  # decided this tab may close.
  if ! "$CMUX" close-workspace --workspace "$ref" --force; then
    echo "ERROR: failed to close cmux tab $ref" >&2
    return 1
  fi
  if workspace_ref_exists "$ref"; then
    echo "ERROR: cmux tab $ref is still present after close-workspace" >&2
    return 1
  else
    verify_status=$?
  fi
  if [[ "$verify_status" -eq 2 ]]; then
    echo "ERROR: could not verify cmux tab $ref was removed" >&2
    return 1
  fi
}

snooze_p0_for_workspace() {
  local title="$1" suffix todo_id until
  suffix="${title#*P0 · }"
  todo_id="$(python3 - "${SWITCHBOARD_ROOT:-$REPO_ROOT}/system/state/todo.json" "$suffix" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))
items = data if isinstance(data, list) else data.get("items", data.get("todos", []))
title = sys.argv[2]
for item in items:
    if (item.get("priority") == "P0"
            and item.get("status") in ("open", "actionable")
            and (item.get("title") or "").strip() == title):
        print(item.get("id", ""))
        break
PY
)" || true
  if [[ -z "$todo_id" ]]; then
    echo "WARNING: could not match P0 tab '$title' to an open P0 todo; it may reopen until the todo is completed or snoozed" >&2
    return 1
  fi
  until="$(mux_date_shift +1d '+%Y-%m-%dT06:00:00%z')"
  if python3 "$HOME/.agents/skills/todo/scripts/todo_cli.py" snooze --id "$todo_id" --until "$until"; then
    echo "[$(ts)] snoozed P0 todo $todo_id until $until"
    return 0
  fi
  echo "WARNING: failed to snooze P0 todo $todo_id; the tab may reopen until the todo is completed or snoozed" >&2
  return 1
}

case "$action" in
  resolve)
    # Read-only: the tab a handle names right now, as a preview before close.
    ref="$(resolve_ws_handle "$target")" || exit 1
    printf '%s\t%s\n' "$ref" "$(workspace_title_for_ref "$ref")"
    ;;

  open)
    ref="$(resolve_ws_handle "$target")" || exit 1
    "$CMUX" select-workspace --workspace "$ref"
    echo "[$(ts)] focused $ref"
    ;;

  wrap)
    ref="$(resolve_ws_handle "$target")" || exit 1
    prompt='Please wrap up: 1) one-paragraph summary of what was done in this session, 2) any drafts that should land in workstream files, 3) the single next concrete action. Stop after that — do not start new work.'
    # Guarded: wait up to 20s for the target to be idle + empty before sending,
    # so we never overwrite a draft the user is typing there.
    if cig_safe_send "$ref" "$prompt" 20 3; then
      echo "[$(ts)] wrap-up prompt sent to $ref"
    else
      echo "[$(ts)] wrap-up NOT sent to $ref (busy or draft in box); try again when idle"
    fi
    ;;

  close)
    ref="$(resolve_ws_handle "$target")" || exit 1
    title="$(workspace_title_for_ref "$ref")"
    # Send wrap-up first — but only if the target is safe. close is an explicit
    # intent, so if it never becomes safe we skip the wrap-up (rather than
    # clobber a draft) and still close the tab.
    prompt='Please wrap up: 1) one-paragraph summary of what was done in this session, 2) any drafts that should land in workstream files, 3) the single next concrete action. Stop after that — do not start new work.'
    if cig_safe_send "$ref" "$prompt" 15 3; then
      echo "[$(ts)] wrap-up prompt sent to $ref; closing tab in 5s"
      sleep 5
    else
      echo "[$(ts)] wrap-up skipped for $ref (busy or draft in box); closing tab"
    fi
    # Unpin if pinned, then close
    "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null 2>&1 || true
    close_workspace_verified "$ref" || exit 1
    echo "[$(ts)] closed $ref"
    if [[ "$title" == *"P0 ·"* ]]; then
      echo "[$(ts)] note: this P0 tab will return unless its todo is completed or snoozed"
    fi
    ;;

  pause)
    slug="$target"
    ws_dir="$(ws_dir_for_slug "$slug" 2>/dev/null || true)"
    if [[ -z "${ws_dir:-}" ]]; then
      # No workstream folder — fall back to closing matching cmux tab
      ref=$(find_cmux_ref_by_slug "$slug")
      if [[ -n "$ref" ]]; then
        title="$(workspace_title_for_ref "$ref")"
        if [[ "$title" == *"P0 ·"* ]]; then
          snooze_p0_for_workspace "$title" || true
        fi
        "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null 2>&1 || true
        close_workspace_verified "$ref" || exit 1
        echo "[$(ts)] closed tab only — no workstream folder, no lifecycle change ($ref, '$slug')"
        exit 0
      fi
      echo "ERROR: no workstream or cmux tab found for slug '$slug'" >&2
      exit 1
    fi
    # Find the cmux ref for this workstream (if open)
    title_match="$(grep -E '^name:' "$ws_dir/config.yaml" 2>/dev/null | head -1 | sed -E 's/^name:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
    if [[ -n "$title_match" ]]; then
      ref="$($CMUX list-workspaces | awk -v t="$title_match" '
        {
          gsub(/^[ \t*]+/, "", $0)
          if (match($0, /^workspace:[0-9]+/)) {
            r = substr($0, RSTART, RLENGTH)
            rest = substr($0, RSTART+RLENGTH)
            if (index(rest, t)) { print r; exit }
          }
        }
      ')"
      if [[ -n "${ref:-}" ]]; then
        "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null 2>&1 || true
        close_workspace_verified "$ref" || exit 1
        echo "[$(ts)] closed cmux tab $ref for $slug"
      fi
    fi
    # Set status: paused in config.yaml
    if grep -qE '^status:' "$ws_dir/config.yaml"; then
      sed -i '' -E 's/^status:.*/status: "paused"/' "$ws_dir/config.yaml"
    else
      echo 'status: "paused"' >> "$ws_dir/config.yaml"
    fi
    echo "[$(ts)] marked $slug as paused (workstream files remain in workstreams/$slug/)"
    ;;

  archive)
    slug="$target"
    ws_dir="$(ws_dir_for_slug "$slug" 2>/dev/null || true)"
    if [[ -z "${ws_dir:-}" ]]; then
      # No workstream folder — fall back to closing matching cmux tab
      ref=$(find_cmux_ref_by_slug "$slug")
      if [[ -n "$ref" ]]; then
        "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null 2>&1 || true
        close_workspace_verified "$ref" || exit 1
        echo "[$(ts)] closed tab only — no workstream folder, no lifecycle change ($ref, '$slug'; nothing moved to _archive)"
        exit 0
      fi
      echo "ERROR: no workstream or cmux tab found for slug '$slug'" >&2
      exit 1
    fi
    # Close cmux tab if open
    title_match="$(grep -E '^name:' "$ws_dir/config.yaml" 2>/dev/null | head -1 | sed -E 's/^name:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
    if [[ -n "$title_match" ]]; then
      ref="$($CMUX list-workspaces | awk -v t="$title_match" '
        {
          gsub(/^[ \t*]+/, "", $0)
          if (match($0, /^workspace:[0-9]+/)) {
            r = substr($0, RSTART, RLENGTH)
            rest = substr($0, RSTART+RLENGTH)
            if (index(rest, t)) { print r; exit }
          }
        }
      ')"
      if [[ -n "${ref:-}" ]]; then
        "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null 2>&1 || true
        close_workspace_verified "$ref" || exit 1
        echo "[$(ts)] closed cmux tab $ref for $slug"
      fi
    fi
    # Move to _archive/
    mkdir -p "$ARCHIVE"
    if [[ -d "$ws_dir" && "$ws_dir" != "$ARCHIVE/$slug" ]]; then
      mv "$ws_dir" "$ARCHIVE/$slug"
      echo "[$(ts)] moved workstreams/$slug → workstreams/_archive/$slug"
    fi
    # Re-render workstreams README
    if [[ -x "$REPO_ROOT/system/scripts/render-workstreams-readme.py" ]]; then
      python3 "$REPO_ROOT/system/scripts/render-workstreams-readme.py" --root "$REPO_ROOT" --write >/dev/null 2>&1 || true
    fi
    echo "[$(ts)] $slug archived"
    ;;

  close-all|end-of-day|close-of-day|eod)
    # CoS must save the closeout before shutdown. No Router or task ID.
    case "${target:-}" in
      '')
        printf '%s\n' '{"status":"closeout-required","owner":"cos","skill":"closing-day","instruction":"Run closing-day directly in CoS. Save and re-read the daily note and per-workstream EOD context. Print the summary, then call close-all --closeout-saved. Do not create a Router."}'
        ;;
      --request)
        # Palette invocation from another workspace requests CoS, never teardown.
        title="$(python3 "$REPO_ROOT/system/scripts/surface-registry.py" get cos title)"
        listing="$("$CMUX" list-workspaces)" || exit 1
        ref="$(printf '%s\n' "$listing" | awk -v t="$title" '{
          sub(/^[ *]+/, ""); ref=$1; sub(/^[^ ]+[ ]+/, ""); sub(/[ ]+\[selected\]$/, "")
          if ($0 == t) print ref
        }')"
        [[ "$ref" =~ ^workspace:[0-9]+$ ]] || { echo 'CoS is not uniquely available; run close of day in CoS.' >&2; exit 1; }
        cig_safe_send "$ref" 'End of day. Run closing-day directly in CoS, save the durable closeout, then detach teardown. No Router or handoff.' 0 1 \
          || { echo 'CoS is busy or has a draft. Nothing closed; retry from CoS when ready.' >&2; exit 1; }
        echo 'Closeout requested in CoS; nothing closed yet.'
        ;;
      --dry-run)
        exec bash "$REPO_ROOT/system/scripts/start-end-of-day-teardown.sh" --dry-run
        ;;
      --closeout-saved)
        shift 2
        exec bash "$REPO_ROOT/system/scripts/start-end-of-day-teardown.sh" "$@"
        ;;
      *) echo 'Save closeout first: use closing-day, then close-all --closeout-saved [--keep-app].' >&2; exit 2 ;;
    esac
    ;;

  *)
    echo "ERROR: unknown action '$action' (use open|close|pause|archive|wrap|resolve|close-all)" >&2
    exit 2
    ;;
esac
