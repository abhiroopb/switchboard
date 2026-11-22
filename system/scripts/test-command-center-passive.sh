#!/usr/bin/env bash
# test-command-center-passive.sh — focused tests for the PASSIVE-ONLY
# command-center loop (2026-08-05 automation-routing policy, replacing
# test-command-center-inject.sh):
#   - every tick writes the digest snapshot + refreshes the status badge/log
#   - the loop itself never sends a chat turn into the CoS Amp session; the
#     guarded next-up and callback helpers own the approved delivery paths
#   - result delivery still runs when command-center rendering fails
#   - the retired inject machinery (trigger phrase, inject marker, cadence
#     floor) is actually gone from the script
#   - the shared inject guard still works for INTERACTIVE paths that use it
#     (legacy allow-busy defers; idle sends atomically; drafts still block)
#
# Hermetic: temp fixture repo (REPO_ROOT override) with a fake `cmux` binary
# and a fake digest script — no real cmux calls, no writes outside the fixture.
# Runs the REAL system/scripts/command-center-loop.sh with --once.
#
# Run: bash system/scripts/test-command-center-passive.sh

set -uo pipefail

REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOOP_SCRIPT="$REAL_REPO/system/scripts/command-center-loop.sh"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/cmd-center-passive.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT

# --- Fixture repo -----------------------------------------------------------
mkdir -p "$FIXTURE/system/scripts/lib" "$FIXTURE/system/state" "$FIXTURE/bin"

# Fake digest generator: always succeeds, emits one P0 and one P1 marker line.
cat > "$FIXTURE/bin/fake-digest.py" <<'EOF'
import sys
sys.stdout.write("# Command Center\n\n- 🔴 P0 fixture item\n- 🟠 P1 fixture item\n")
EOF
cat > "$FIXTURE/bin/fail-digest.py" <<'EOF'
import sys
sys.exit(3)
EOF

# Delivery helpers are fakes so this suite proves orchestration without
# entering a real Amp session or mutating the real task ledger.
cat > "$FIXTURE/system/scripts/post-next-up-digest.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "post" >> "$FIXTURE/next-up-calls.log"
printf '{"status":"deferred"}\n'
EOF
cat > "$FIXTURE/system/scripts/report-to-cos.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$FIXTURE/report-calls.log"
printf '{"delivered":true,"batched":false}\n'
EOF
chmod +x "$FIXTURE/system/scripts/post-next-up-digest.sh" \
  "$FIXTURE/system/scripts/report-to-cos.sh"

# The loop runs `dispatch.py sweep` (expire leases, release, reap, nudge idle)
# once per tick; the fake proves it is invoked without touching cmux or the ledger.
cat > "$FIXTURE/system/scripts/dispatch.py" <<'EOF'
import os
from pathlib import Path
import sys
assert sys.argv[1:] == ["sweep"]
Path(os.environ["RUNNING_TASKS_FILE"]).with_name("launch-expiry-called").touch()
print("{}")
EOF
mkdir -p "$FIXTURE/system/state/worker-dispatch"

# Fake cmux: logs every invocation; screen driven by $FIXTURE/screen.txt.
cat > "$FIXTURE/bin/cmux" <<EOF
#!/usr/bin/env bash
FIXTURE="$FIXTURE"
EOF
cat >> "$FIXTURE/bin/cmux" <<'EOF'
printf '%s\n' "$*" >> "$FIXTURE/cmux-calls.log"
case "$1" in
  list-workspaces)
    printf '* workspace:9  🧭 CoS\n  workspace:3  other tab\n' ;;
  tree)
    echo "surface:1" ;;
  read-screen)
    cat "$FIXTURE/screen.txt" 2>/dev/null ;;
  send)
    last="${!#}"
    case "$last" in
      *'\r'|*'\n') touch "$FIXTURE/submitted" ;;
    esac
    exit 0 ;;
  send-key|set-status|log)
    exit 0 ;;
esac
exit 0
EOF
chmod +x "$FIXTURE/bin/cmux"

# --- Screen fixtures ---------------------------------------------------------
IDLE_SCREEN=$'agent output above\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯\n  Enter to send'
BUSY_SCREEN=$'Thinking 89 tok\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯\n  Enter to steer · esc to interrupt'
DRAFT_SCREEN=$'agent output above\n╭──────────────────────────╮\n│ half-typed user prompt   │\n╰──────────────────────────╯\n  Enter to send'
IDLE_PROSE_SCREEN=$'I need to see how that is working before rollout.\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯\n  Enter to send'

# --- Harness ------------------------------------------------------------------
PASS=0
FAIL=0
ok()   { echo "ok   - $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL - $1"; FAIL=$((FAIL + 1)); }

CALLS="$FIXTURE/cmux-calls.log"
LATEST="$FIXTURE/system/state/command-center-latest.md"

run_tick() {
  : > "$CALLS"
  rm -f "$FIXTURE/submitted"
  REPO_ROOT="$FIXTURE" \
  CMUX="$FIXTURE/bin/cmux" \
  DIGEST_SCRIPT="${DIGEST_SCRIPT_OVERRIDE:-$FIXTURE/bin/fake-digest.py}" \
    bash "$LOOP_SCRIPT" --once 2>&1
}

set_screen() { printf '%s\n' "$1" > "$FIXTURE/screen.txt"; }
any_send()   { grep -qE '^send(-key)? ' "$CALLS"; }

# --- 1. Idle CoS screen: passive refresh only, zero sends ---------------------
set_screen "$IDLE_SCREEN"
out="$(run_tick)"
if [[ -f "$LATEST" ]] && grep -q "P0 fixture item" "$LATEST"; then ok "idle: digest snapshot written"; else fail "idle: digest snapshot written"; fi
if grep -q "^set-status command-center" "$CALLS"; then ok "idle: status badge refreshed"; else fail "idle: status badge refreshed"; fi
if any_send; then fail "idle: NO chat send into CoS"; else ok "idle: NO chat send into CoS"; fi
if echo "$out" | grep -q "chrome updated (passive)"; then ok "idle: loop logs passive tick"; else fail "idle: loop logs passive tick"; fi
if [[ -f "$FIXTURE/system/state/launch-expiry-called" ]]; then
  ok "idle: dispatch.py sweep runs with fixture ledger"
else
  fail "idle: dispatch.py sweep runs with fixture ledger"
fi

# --- 2. Busy CoS screen: identical passive behavior ---------------------------
set_screen "$BUSY_SCREEN"
run_tick >/dev/null
if grep -q "P0 fixture item" "$LATEST"; then ok "busy: digest snapshot still written"; else fail "busy: digest snapshot still written"; fi
if any_send; then fail "busy: NO chat send into CoS"; else ok "busy: NO chat send into CoS"; fi

# --- 3. Retired inject machinery is actually gone ------------------------------
# (The trigger phrase may still appear in comments describing the owner's manual
# interactive request — only the functional machinery must be gone.)
if ! grep -qE 'inject_digest_trigger|INJECT_MARKER|MIN_INJECT_GAP|TRIGGER_PHRASE=' "$LOOP_SCRIPT"; then
  ok "source: inject function / marker / cadence floor removed"
else
  fail "source: inject function / marker / cadence floor removed"
fi
if ! grep -q 'cmux-inject-guard' "$LOOP_SCRIPT"; then
  ok "source: loop no longer imports the inject guard"
else
  fail "source: loop no longer imports the inject guard"
fi
if grep -q 'HISTORY_RETENTION_COUNT="${HISTORY_RETENTION_COUNT:-144}"' "$LOOP_SCRIPT"; then
  ok "source: command-center retains 24 hours at its 10-minute cadence"
else
  fail "source: command-center retains 24 hours at its 10-minute cadence"
fi

# --- 4. Terminal delivery is independent from command-center rendering --------
cat > "$FIXTURE/system/state/running-tasks.json" <<'EOF'
{"version":1,"tasks":[{"id":"rt-launchpad-pending","origin":"launchpad","status":"done","workstream":"test","summary":"Finished safely.","report_delivered":false}]}
EOF
: > "$FIXTURE/next-up-calls.log"
: > "$FIXTURE/report-calls.log"
rc=0
out="$(DIGEST_SCRIPT_OVERRIDE="$FIXTURE/bin/fail-digest.py" run_tick)" || rc=$?
if [[ $rc -ne 0 ]] \
    && grep -q '^post$' "$FIXTURE/next-up-calls.log" \
    && grep -q -- '--task-id rt-launchpad-pending.*--delivery direct' "$FIXTURE/report-calls.log"; then
  ok "failed digest: CoS batch and direct-origin retries still run"
else
  fail "failed digest: CoS batch and direct-origin retries still run (rc=$rc, $out)"
fi
if grep -q 'next-up delivery: {"status":"deferred"}' <<<"$out"; then
  ok "delivery sweep: result status is observable"
else
  fail "delivery sweep: result status is observable ($out)"
fi

# --- 5. Guard library still serves interactive paths ---------------------------
CMUX="$FIXTURE/bin/cmux"
# Keep the guard's send log inside the fixture so the suite never appends to
# the live system/logs/cmux-sends.log. Must be set BEFORE sourcing: the lib
# fills in the live default at source time.
CIG_SEND_LOG="$FIXTURE/cmux-sends.log"; export CIG_SEND_LOG
# shellcheck source=lib/cmux-inject-guard.sh
source "$REAL_REPO/system/scripts/lib/cmux-inject-guard.sh"
CIG_CMUX="$FIXTURE/bin/cmux"

set_screen "$IDLE_PROSE_SCREEN"
if cig_safe "workspace:9"; then
  ok "guard (interactive): status words in idle scrollback do not cause a false busy state"
else
  fail "guard (interactive): status words in idle scrollback do not cause a false busy state"
fi

set_screen "$BUSY_SCREEN"; : > "$CALLS"; rm -f "$FIXTURE/submitted"
rc=0
cig_safe_send_allow_busy "workspace:9" "triage active turn" 0 3 || rc=$?
if [[ "$rc" == 2 ]] && ! grep -q '^send --workspace' "$CALLS"; then
  ok "guard (interactive): active Amp turn defers without a steer"
else
  fail "guard (interactive): active Amp turn defers without a steer"
fi

set_screen "$IDLE_SCREEN"; : > "$CALLS"; rm -f "$FIXTURE/submitted"
if cig_safe_send_allow_busy "workspace:9" "triage idle turn" 0 3 \
  && grep -q "^send --workspace workspace:9 triage idle turn" "$CALLS" \
  && [[ -f "$FIXTURE/submitted" ]]; then
  ok "guard (interactive): idle Amp receives an atomic prompt"
else
  fail "guard (interactive): idle Amp receives an atomic prompt"
fi

set_screen "$DRAFT_SCREEN"; : > "$CALLS"; rm -f "$FIXTURE/submitted"
if cig_safe_send_allow_busy "workspace:9" "must not overwrite" 0 3; then
  fail "guard (interactive): draft in input box defers"
elif grep -q "^send --workspace" "$CALLS"; then
  fail "guard (interactive): draft in input box defers"
else
  ok "guard (interactive): draft in input box defers"
fi

echo
echo "passed: $PASS, failed: $FAIL"
(( FAIL == 0 )) || exit 1
