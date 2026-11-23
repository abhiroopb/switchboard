#!/usr/bin/env bash
# Detached EOD teardown, after closing-day has saved the durable closeout.
# CoS invokes by default. A legacy Router may invoke and closes after CoS.
# Never stop independent jobs, commit/push, discard drafts, or interrupt turns.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CMUX="${CMUX:-${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
source "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh"
DRY=0
KEEP_APP=0
INVOKING_REF="${SWITCHBOARD_INVOKING_WORKSPACE_REF:-}"
while (( $# > 0 )); do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --keep-app) KEEP_APP=1; shift ;;
    --invoking-workspace|--router-ref) INVOKING_REF="${2:-}"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$INVOKING_REF" ]]; then
  INVOKING_REF="$("$CMUX" identify | "${SWITCHBOARD_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/lib/json_get.py" caller.workspace_ref)"
fi
[[ "$INVOKING_REF" =~ ^workspace:[0-9]+$ ]] || { echo 'invalid invoking workspace' >&2; exit 2; }

ts() { date +"%H:%M:%S"; }
deadline=$((SECONDS + ${EOD_TOTAL_CEILING:-3600}))
reason=""
wrapped=" "
unknown_sends=" "

# Personal sessions: every switchboard launcher names its workspace
# (CoS, Comms Triage, workstream emoji titles, Router N, worker tabs), so a
# workspace with no custom title is one the owner opened by hand, for example an Amp
# session in ~. EOD leaves those alone: no wrap-up prompt, no close, and they
# never count as a failed teardown. When the JSON listing is unavailable (fake
# cmux in tests, older cmux) every workspace is treated as switchboard-managed.
personal_refs_now() {
  local json
  json="$(CMUX_QUIET=1 "$CMUX" workspace list --json 2>/dev/null)" || return 0
  printf '%s\n' "$json" | "${SWITCHBOARD_PYTHON:-python3}" -c '
import json, sys
try:
    rows = json.loads(sys.stdin.read()).get("workspaces") or []
except (ValueError, AttributeError):
    rows = []
for w in rows:
    if isinstance(w, dict) and w.get("has_custom_title") is False and w.get("ref"):
        print(w["ref"])
' 2>/dev/null || true
}
is_personal() { [[ " $personal_refs " == *" $1 "* ]]; }
personal_refs="$(personal_refs_now | tr '\n' ' ')"

# Capture the live board once. A failed read is not an empty board.
listing="$("$CMUX" list-workspaces)" || exit 1
cos_ref=""
other_refs=()
while IFS= read -r line; do
  line="$(printf '%s' "$line" | sed -E 's/^[ \t*]+//; s/[[:space:]]+\[selected\]$//')"
  ref="$(printf '%s' "$line" | grep -oE '^workspace:[0-9]+' || true)"
  [[ -n "$ref" ]] || continue
  title="$(printf '%s' "${line#"$ref"}" | sed -E 's/^[[:space:]]*[^[:alnum:]]*//; s/[[:space:]]+$//' | tr '[:upper:]' '[:lower:]')"
  if [[ "$ref" != "$INVOKING_REF" ]] && is_personal "$ref"; then
    echo "[$(ts)] skipping $ref (personal session, no switchboard title): left open"
    continue
  fi
  if [[ "$title" == cos ]]; then
    [[ -z "$cos_ref" ]] || { echo 'Multiple CoS workspaces; teardown deferred' >&2; exit 1; }
    cos_ref="$ref"
  elif [[ "$ref" != "$INVOKING_REF" ]]; then
    other_refs+=("$ref")
  fi
done <<< "$listing"
[[ -n "$cos_ref" ]] || { echo 'No CoS workspace; teardown deferred' >&2; exit 1; }

# The shared injection guard inspects only the first terminal surface. Do not
# close a split workspace whose other surfaces could hold active work/drafts.
# Read-only `[markdown]` viewer panes (for example a live.md split) cannot hold
# a draft or a running agent, so they never block a close. Any other extra
# surface type still defers the workspace.
eod_safe() {
  local tree screen count markdown first
  reason="cannot read workspace tree"
  tree="$("$CMUX" tree --workspace "$1")" || return 1
  count="$(printf '%s\n' "$tree" | grep -oE 'surface:[0-9]+' | sort -u | wc -l | tr -d ' ')"
  markdown="$(printf '%s\n' "$tree" | grep -E 'surface:[0-9]+ \[markdown\]' | grep -oE 'surface:[0-9]+' | sort -u | wc -l | tr -d ' ')"
  reason="$count surfaces ($markdown markdown); cannot verify all panes"
  [[ $((count - markdown)) == 1 ]] || return 1
  # The guard reads the first listed surface; it must be the terminal one.
  first="$(printf '%s\n' "$tree" | grep -E 'surface:[0-9]+' | head -1)"
  reason="first surface is a markdown pane; cannot read the agent screen"
  [[ "$first" != *"[markdown]"* ]] || return 1
  reason="unreadable screen"
  screen="$(cig_read "$1")" || return 1
  [[ -n "$screen" ]] || return 1
  reason="pending tool approval"
  printf '%s\n' "$screen" | grep -qiE 'approve tool|allow / reject|awaiting approval|allow once|allow this tool' && return 1
  reason="unsent draft in input box"
  printf '%s\n' "$screen" | input_box_has_text && return 1
  reason="agent still working"
  printf '%s\n' "$screen" | screen_is_busy && return 1
  reason="prompt box not positively identified"
  printf '%s\n' "$screen" | input_box_is_empty || return 1
  reason=""
}

# Wait for a positively idle, empty prompt. Recheck twice and at the close
# boundary. A timeout is a blocker, never permission to discard live work.
wait_safe() {
  local ref="$1" limit="${2:-${EOD_IDLE_WAIT_SECONDS:-90}}" phase="${3:-current turn}" start previous=""
  start=$SECONDS
  while true; do
    if (( SECONDS >= deadline )); then
      eod_safe "$ref" || true
      reason="total ceiling reached; ${reason:-idle, close not attempted}"
      return 1
    fi
    if eod_safe "$ref"; then
      sleep 1.3
      eod_safe "$ref" && return 0
    fi
    case "$reason" in
      "unsent draft in input box"|"pending tool approval") return 1 ;;
    esac
    if [[ "$reason" != "$previous" ]]; then
      echo "[$(ts)] waiting for $ref ($phase): $reason"
      previous="$reason"
    fi
    (( SECONDS - start < limit )) || { reason="$reason ($phase wait ceiling reached)"; return 1; }
    sleep 1
  done
}

close_safe() {
  local ref="$1"
  wait_safe "$ref" "${EOD_WRAP_WAIT_SECONDS:-900}" wrap-up && eod_safe "$ref" || {
    echo "[$(ts)] teardown deferred: $ref: $reason" >&2
    return 1
  }
  "$CMUX" workspace-action --action unpin --workspace "$ref" >/dev/null || { reason="unpin failed"; return 1; }
  eod_safe "$ref" || return 1
  reason="close command failed"
  # Legacy Router closes retain the launcher sentinel, but only after guards.
  if [[ "$ref" == "$INVOKING_REF" && "$ref" != "$cos_ref" ]]; then
    CMUX_BIN="$CMUX" bash "$REPO_ROOT/system/scripts/router-self-close.sh" --workspace "$ref" || return 1
  else
    # --force: cmux 0.65 refuses a workspace with a running process (the Amp CLI) without it.
    "$CMUX" close-workspace --workspace "$ref" --force || return 1
  fi
  local remaining
  reason="cannot verify workspace closed"
  remaining="$("$CMUX" list-workspaces)" || return 1
  if printf '%s\n' "$remaining" | grep -qE "^[ *]*${ref}([[:space:]]|$)"; then
    reason="workspace remains open after close"
    echo "teardown deferred: $ref remains open" >&2
    return 1
  fi
}

# Send the wrap-up prompt once the workspace is positively idle. Returns 0 when
# the prompt was sent (or the workspace already closed), 1 with $reason set
# otherwise. Never waits for the agent's wrap-up turn to finish.
wrap_workspace() {
  local ref="$1" live rc
  reason="cannot list workspaces"
  live="$("$CMUX" list-workspaces)" || return 1
  printf '%s\n' "$live" | grep -qE "^[ *]*${ref}([[:space:]]|$)" || return 0
  if (( SECONDS >= deadline )); then
    reason="total ceiling reached; wrap-up not sent"
    return 1
  fi
  if [[ "$unknown_sends" == *" $ref "* ]]; then
    reason="wrap send outcome unknown; verify receipt before retrying"
    return 1
  fi
  [[ "$wrapped" != *" $ref "* ]] || return 0
  wait_safe "$ref" || return 1
  if cig_safe_send "$ref" "$prompt" 0 1; then
    wrapped+="$ref "
    return 0
  else
    rc=$?
  fi
  if [[ "$rc" == 3 ]]; then
    unknown_sends+="$ref "
    reason="wrap send outcome unknown; verify receipt before retrying"
  else
    eod_safe "$ref" || return 1
    reason="wrap send deferred by safety guard"
  fi
  return 1
}

attempt_workspace() {
  local ref="$1" live
  reason="cannot list workspaces"
  live="$("$CMUX" list-workspaces)" || return 1
  # A workspace that closed itself while wrapping needs no further action.
  printf '%s\n' "$live" | grep -qE "^[ *]*${ref}([[:space:]]|$)" || return 0
  if (( SECONDS >= deadline )); then
    reason="total ceiling reached; verification and close deferred"
    return 1
  fi
  if [[ "$wrapped" != *" $ref "* ]]; then
    wrap_workspace "$ref" || return 1
    # Let the agent pick up the prompt before the wrap-up idle check.
    sleep "${EOD_WRAP_SETTLE_SECONDS:-5}"
  fi
  close_safe "$ref"
}

prompt='Please wrap up: save your session summary, drafts, and next action in your existing workstream files. Do not commit, push, send external messages, or start new work. Stop after saving.'
deferred=()
# Phase A: send every wrap-up prompt first so the agents run their wrap-up
# turns concurrently. Phase B then waits for each one and closes it. Total
# teardown time is the slowest wrap-up, not the sum of all of them.
phase_a_sent=0
for ref in ${other_refs[@]+"${other_refs[@]}"}; do
  if [[ "$DRY" == 1 ]]; then
    echo "[$(ts)] wrapping $ref"
    echo "  [dry-run] guard idle + empty input, send wrap-up to $ref (phase A, no wait)"
    continue
  fi
  echo "[$(ts)] wrapping $ref"
  # Unlike targeted `close N`, EOD never falls through after a guard failure.
  if wrap_workspace "$ref"; then
    [[ "$wrapped" == *" $ref "* ]] && phase_a_sent=$((phase_a_sent + 1))
  else
    echo "[$(ts)] wrap deferred: $ref: $reason" >&2
    deferred+=("$ref")
  fi
done
if [[ "$DRY" != 1 && "$phase_a_sent" -gt 0 ]]; then
  echo "[$(ts)] phase A sent $phase_a_sent wrap-up prompt(s); waiting ${EOD_WRAP_SETTLE_SECONDS:-5}s before close checks"
  sleep "${EOD_WRAP_SETTLE_SECONDS:-5}"
fi
for ref in ${other_refs[@]+"${other_refs[@]}"}; do
  echo "[$(ts)] closing $ref"
  if [[ "$DRY" == 1 ]]; then
    echo "  [dry-run] wait for wrap-up completion, guard idle + empty input, then close $ref (phase B)"
    continue
  fi
  # Deferred or self-closed workspaces were handled in phase A.
  [[ "$wrapped" == *" $ref "* ]] || continue
  if ! attempt_workspace "$ref"; then
    echo "[$(ts)] teardown deferred: preserving $ref: $reason" >&2
    deferred+=("$ref")
  fi
done

if [[ "$DRY" == 1 ]]; then
  echo "  [dry-run] two phases: send all wrap-ups first (pre-send wait ${EOD_IDLE_WAIT_SECONDS:-90}s each), then wait for completion (${EOD_WRAP_WAIT_SECONDS:-900}s each, overlapping) and close; retry deferred workspaces up to ${EOD_RETRY_PASSES:-3} passes within ${EOD_TOTAL_CEILING:-3600}s total"
  if [[ "$cos_ref" == "$INVOKING_REF" ]]; then
    echo "  [dry-run] invoking workspace is CoS ($cos_ref): wait for its final turn, close it last, no Router step"
  else
    echo "  [dry-run] close CoS ($cos_ref) after its report turn"
    echo "  [dry-run] close invoking workspace ($INVOKING_REF) after CoS"
  fi
  [[ "$KEEP_APP" == 1 ]] || echo '  [dry-run] quit cmux only after zero workspaces, then clear restore snapshots'
  exit 0
fi
for ((pass=1; pass<=${EOD_RETRY_PASSES:-3} && ${#deferred[@]}>0 && SECONDS<deadline; pass++)); do
  echo "[$(ts)] retry pass $pass: ${deferred[*]}"
  sleep "${EOD_RETRY_INTERVAL_SECONDS:-5}"
  pending=("${deferred[@]}")
  deferred=()
  for ref in "${pending[@]}"; do
    if ! attempt_workspace "$ref"; then
      echo "[$(ts)] survivor $ref: $reason" >&2
      deferred+=("$ref")
    fi
  done
done
[[ ${#deferred[@]} == 0 ]] || { echo 'Teardown incomplete; CoS and cmux remain open. See survivor reasons above.' >&2; exit 1; }

# New workspaces may have opened while other tabs wrapped. Preserve them and
# CoS rather than treating the old snapshot as authority to quit the app.
# Personal sessions (no switchboard title), including ones opened mid-teardown, are
# expected survivors and never defer the CoS close.
personal_refs="$(personal_refs_now | tr '\n' ' ')"
remaining="$("$CMUX" list-workspaces)" || exit 1
unexpected=""
for ref in $(printf '%s\n' "$remaining" | grep -oE 'workspace:[0-9]+' | grep -vxF "$cos_ref" | grep -vxF "$INVOKING_REF" || true); do
  is_personal "$ref" && continue
  unexpected+="$ref "
done
[[ -z "$unexpected" ]] || { echo "Teardown deferred: new workspaces $unexpected" >&2; exit 1; }
if [[ "$INVOKING_REF" != "$cos_ref" ]]; then
  # The legacy launcher reports after spawning us. Give that callback a chance
  # to start before checking CoS's final idle/empty state.
  sleep "${EOD_REPORT_GRACE_SECONDS:-10}"
  wait_safe "$INVOKING_REF" "${EOD_WRAP_WAIT_SECONDS:-900}" final-turn || { echo "survivor $INVOKING_REF: $reason" >&2; exit 1; }
fi
close_safe "$cos_ref" || { echo "survivor $cos_ref: $reason" >&2; exit 1; }
if [[ "$INVOKING_REF" != "$cos_ref" ]]; then
  close_safe "$INVOKING_REF" || { echo "survivor $INVOKING_REF: $reason" >&2; exit 1; }
fi
[[ "$KEEP_APP" == 0 ]] || exit 0

remaining="$("$CMUX" list-workspaces)" || exit 1
if printf '%s\n' "$remaining" | grep -qE 'workspace:[0-9]+'; then
  personal_refs="$(personal_refs_now | tr '\n' ' ')"
  leftover=""
  for ref in $(printf '%s\n' "$remaining" | grep -oE 'workspace:[0-9]+'); do
    is_personal "$ref" || leftover+="$ref "
  done
  if [[ -z "$leftover" ]]; then
    echo "[$(ts)] detached teardown complete; personal sessions left open: ${personal_refs% }. App left running."
    exit 0
  fi
  echo 'Teardown incomplete; cmux still has workspaces. App left running.' >&2
  exit 1
fi
# Normal app quit only: never kill processes to hide a failed close.
osascript -e 'quit app "cmux"'
bash "$REPO_ROOT/system/scripts/clear-cmux-session-state.sh" --wait-seconds 30
# With cmux gone no uvx MCP server holds ~/.cache/uv/.lock, so the prune that
# times out in the weekly retention task can run here. Bounded and best-effort:
# a cache another process still holds is left for the next close.
TIMEOUT_BIN="${TIMEOUT_BIN:-$(command -v timeout || command -v gtimeout || true)}"
if [[ "${EOD_SKIP_UV_PRUNE:-0}" != 1 && -n "$TIMEOUT_BIN" ]] && command -v uv >/dev/null 2>&1; then
  if "$TIMEOUT_BIN" 120 uv cache prune >/dev/null 2>&1; then
    echo "[$(ts)] uv cache prune ok"
  else
    echo "[$(ts)] uv cache prune skipped (lock held or timed out); retried at next close"
  fi
fi
echo "[$(ts)] detached teardown complete"
