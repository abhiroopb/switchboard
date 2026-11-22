#!/usr/bin/env bash
# Regression test: guarded cmux sends must never carry raw CR/LF.
#
# The Amp TUI runs each embedded newline as Enter, so a multi-line payload
# would land as several queued user turns. cig_flatten collapses CR/LF to spaces in all three guarded send
# paths so one payload is always exactly one message.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT

PASS=0; FAIL=0
ok()   { echo "ok   - $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL - $1"; FAIL=$((FAIL+1)); }

# Stub cmux: record every call, one line per invocation (raw newlines in an
# argument would therefore span extra lines in the calls file).
CALLS="$FIXTURE/calls"
mkdir -p "$FIXTURE/bin"
cat > "$FIXTURE/bin/cmux" <<EOF
#!/usr/bin/env bash
printf '%s\0' "\$@" >> "$CALLS"
printf '\n' >> "$CALLS"
exit 0
EOF
chmod +x "$FIXTURE/bin/cmux"

export CIG_SEND_LOG="$FIXTURE/sends.log"
# shellcheck source=lib/cmux-inject-guard.sh
source "$REPO/system/scripts/lib/cmux-inject-guard.sh"
CIG_CMUX="$FIXTURE/bin/cmux"
# Every send now requires positive idle/empty evidence, including --force.
cig_read() { printf '╭────────╮\n│        │\n╰────────╯\n'; }
sleep() { :; }

MULTILINE=$'first line\nworkspace:2  Todo\nworkspace:3  Comms Triage\r\nlast line'

# --- 1. cig_flatten collapses CR/LF ---------------------------------------
flat="$(cig_flatten "$MULTILINE")"
if [[ "$flat" != *$'\n'* && "$flat" != *$'\r'* && "$flat" == *"first line workspace:2"* ]]; then
  ok "cig_flatten strips every CR/LF and preserves content order"
else
  fail "cig_flatten strips every CR/LF and preserves content order"
fi

# --- 2. force path sends a single flattened payload ------------------------
: > "$CALLS"
cig_force_send "workspace:9" "$MULTILINE"
sent="$(tr '\0' '|' < "$CALLS")"
# The stub writes one newline per invocation; a raw newline surviving inside
# the payload argument would add extra lines to the calls file.
if [[ "$sent" == *"first line workspace:2  Todo workspace:3  Comms Triage"*"last line"* ]] \
  && [[ "$(wc -l < "$CALLS" | tr -d ' ')" == "1" ]]; then
  ok "cig_force_send: multi-line payload becomes ONE single-line send"
else
  fail "cig_force_send: multi-line payload becomes ONE single-line send"
fi

# --- 4. attribution log line is single-line too -----------------------------
if [[ "$(wc -l < "$CIG_SEND_LOG" | tr -d ' ')" == "1" ]] && grep -q 'safe | .* | workspace:9 | first line' "$CIG_SEND_LOG"; then
  ok "send log: one line per send, flattened prefix"
else
  fail "send log: one line per send, flattened prefix"
fi

# Verify behavior through compatibility aliases, not the implementation text.
for fn in cig_send_now cig_send_now_allow_busy cig_safe_send_allow_busy; do
  : > "$CALLS"
  "$fn" workspace:9 "$MULTILINE"
  sent="$(tr '\0' '|' < "$CALLS")"
  if [[ "$sent" == *"first line workspace:2  Todo workspace:3  Comms Triage"*"last line"* ]] \
    && [[ "$(wc -l < "$CALLS" | tr -d ' ')" == 1 ]]; then
    ok "$fn: one flattened send"
  else
    fail "$fn: one flattened send"
  fi
done

echo
echo "passed: $PASS, failed: $FAIL"
(( FAIL == 0 )) || exit 1
