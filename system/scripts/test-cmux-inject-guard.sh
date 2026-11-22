#!/usr/bin/env bash
# Fixture tests for the stdin classifiers in lib/cmux-inject-guard.sh.
#
# Regression: a workspace with the live Amp Neo footer "╰ ≈ Streaming ─ …"
# must not make cig_safe return 0 (idle). screen_is_busy once matched only "to steer", "Running Tools",
# and "<Verb> N tok". Current Neo builds print neither "Enter to steer" nor a
# token counter during plain streaming; the prompt-box footer is the signal.
#
# Screens below are trimmed copies of real `cmux read-screen` output.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=lib/cmux-inject-guard.sh
source "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh"

PASS=0
FAIL=0
ok()   { echo "ok   - $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL - $1"; FAIL=$((FAIL + 1)); }

expect_busy() {   # name screen
  if printf '%s\n' "$2" | screen_is_busy; then ok "busy: $1"; else fail "busy: $1 (classified idle)"; fi
}
expect_idle() {   # name screen
  if printf '%s\n' "$2" | screen_is_busy; then fail "idle: $1 (classified busy)"; else ok "idle: $1"; fi
}
expect_safe() {   # name screen  (busy=no AND box positively empty)
  if printf '%s\n' "$2" | screen_is_busy; then fail "safe: $1 (busy)"; return; fi
  if printf '%s\n' "$2" | input_box_is_empty; then ok "safe: $1"; else fail "safe: $1 (box not positively empty)"; fi
}

# --- Live-captured footers ----------------------------------------------------

# workspace:20, streaming.
STREAMING_FOOTER=$' ⡬ Exploring 1 file, 1 search ▸\n╭──────────────────────────────────────── $242.3_ ─ ultra ─╮\n│                                                          │\n│                                                          │\n╰ ≈ Streaming ─────────────────── ◷ ─ ~/Development/pm-os (main) ─╯'

# workspace:20, same session, while a shell_command was in flight.
RUNNING_TOOLS_FOOTER=$' ⢘ Running 1 command, 1 failed ▸\n╭──────────────────────────────────────── $242.3_ ─ ultra ─╮\n│                                                          │\n╰ ≈ Running Tools ─────────────── ◷ ─ ~/Development/pm-os (main) ─╯'

# Footer only — the spinner line scrolled off the sampled window.
FOOTER_ONLY=$'╭──────────────────────────────────────── $242.3_ ─ ultra ─╮\n│                                                          │\n╰ ≈ Thinking ──────────────────── ◷ ─ ~/Development/pm-os (main) ─╯'

# workspace:73 idle, 2026-09-07 — plain footer, empty box.
IDLE_NEO=$' - No business data or permissions changed. Write capability remains unverified.\n╭──────────────────────────────────────── $78.99 ─ high ─╮\n│                                                        │\n│                                                        │\n╰──────────────────────────────── ◷ ─ ~/Development/pm-os (main) ─╯'

# workspace:6 idle — remote-host footer variant.
IDLE_REMOTE=$' Understood — ending this session. No work was performed and no changes were made.\n╭──────────────────────────────────────────── $6.51 ─ ultra ─╮\n│                                                            │\n╰──────────────────────── ◷ ─ example-laptop.local: …/Development/pm-os ─╯'

# --- Legacy TUI fixtures (must keep working) ----------------------------------
LEGACY_BUSY_TOK=$'Thinking 89 tok\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯\n  Enter to steer · esc to interrupt'
LEGACY_BUSY_ELLIPSIS=$'Working…\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯'
LEGACY_STREAMING_TOK=$'≋ Streaming 46 tok\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯'
LEGACY_IDLE=$'agent output above\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯\n  Enter to send'

# --- Scrollback words must NOT read as busy -----------------------------------
IDLE_PROSE=$'I need to see how streaming and thinking are working before rollout.\nThe stream was streaming fine; we kept running the exporter.\n╭──────────────────────────╮\n│                          │\n╰──────────────────────────╯'
IDLE_DRAFT=$'agent output above\n╭──────────────────────────╮\n│ half-typed user prompt   │\n╰──────────────────────────╯'

# awk-scoping regression: a quoted Neo footer in scrollback (e.g. a
# test fixture or a pasted log line) ABOVE an idle box must not read as busy.
# The live region is the box plus the two lines above it (spinner line), so the
# quote sits three lines above the box top — real scrollback.
QUOTED_FOOTER_IDLE=$'expect_busy "neo footer" "╰ ≈ Streaming ─────── ◷ ─ ~/Development/pm-os (main) ─╯"\nThat quoted footer was missed by the old classifier.\nNo work is in flight now.\n╭──────────────────────────────────────── $242.3_ ─ ultra ─╮\n│                                                          │\n╰──────────────────────────────── ◷ ─ ~/Development/pm-os (main) ─╯'
# …and a live streaming footer below a quoted idle footer is still busy.
LIVE_FOOTER_BUSY=$'╰──────────────────────────────── ◷ ─ ~/Development/pm-os (main) ─╯ (quoted earlier)\n╭──────────────────────────────────────── $242.3_ ─ ultra ─╮\n│                                                          │\n╰ ≈ Streaming ─────────────────── ◷ ─ ~/Development/pm-os (main) ─╯'

# --- Assertions ---------------------------------------------------------------
expect_busy "neo footer ≈ Streaming (ws:20 live)"       "$STREAMING_FOOTER"
expect_busy "neo footer ≈ Running Tools (ws:20 live)"   "$RUNNING_TOOLS_FOOTER"
expect_busy "neo footer only, spinner scrolled off"     "$FOOTER_ONLY"
expect_busy "neo footer without surrounding box"        $'╰ ≈ Streaming ─ …'
expect_busy "legacy Thinking N tok + to steer"          "$LEGACY_BUSY_TOK"
expect_busy "legacy Working…"                           "$LEGACY_BUSY_ELLIPSIS"
expect_busy "legacy ≋ Streaming N tok"                  "$LEGACY_STREAMING_TOK"

expect_idle "neo idle footer (ws:73 live)"              "$IDLE_NEO"
expect_idle "neo idle footer, remote host (ws:6 live)"  "$IDLE_REMOTE"
expect_idle "legacy idle Enter to send"                 "$LEGACY_IDLE"
expect_idle "scrollback prose with streaming/thinking"  "$IDLE_PROSE"
expect_idle "quoted ≈ Streaming footer above idle box"  "$QUOTED_FOOTER_IDLE"
expect_busy "live ≈ Streaming footer below quoted idle" "$LIVE_FOOTER_BUSY"

expect_safe "neo idle → safe to inject"                 "$IDLE_NEO"
expect_safe "legacy idle → safe to inject"              "$LEGACY_IDLE"

if printf '%s\n' "$IDLE_DRAFT" | input_box_is_empty; then fail "draft box read as empty"; else ok "draft box is not empty"; fi
if printf '%s\n' "$STREAMING_FOOTER" | input_box_is_empty; then ok "streaming box is positively empty (busy decides, not box)"; else fail "streaming box empty-state"; fi

# Amp 0.0.1789000905 live capture: the animated marker also
# renders U+223C (∼). No token counter or tool-status line may be present.
CURRENT_STREAMING=$'╭────────────────── $3.15 ─ high ─╮\n│                                 │\n╰ ∼ Streaming ─ ◷ ─ ~/Development/pm-os (main) ─╯'
CURRENT_TOOLS="${CURRENT_STREAMING/Streaming/Running Tools}"
expect_busy "current ∼ Streaming without tokens or tools" "$CURRENT_STREAMING"
expect_busy "current ∼ Running Tools footer" "$CURRENT_TOOLS"
expect_busy "current ∼ Thinking footer" "${CURRENT_STREAMING/Streaming/Thinking}"
expect_idle "quoted current footer above idle box" "$CURRENT_STREAMING"$'\nFinished.\nNothing running.\n'"$IDLE_NEO"

# --- Claude Code composer (PM_OS_AGENT=claude), live captures ------------------
# Claude Code 2.1.x in cmux 0.65, workspace:1000040020. No ╭…╮ box: the
# composer is a `❯` line fenced by two ──── rules; the footer sits below.
expect_state() {   # name expected screen
  local got; got="$(printf '%s\n' "$3" | cig_composer_state)"
  if [[ "$got" == "$2" ]]; then ok "state: $1 -> $2"; else fail "state: $1 (expected $2, got $got)"; fi
}
CLAUDE_RULE='───────────────────────────────────────────────────────────────────────────'
CLAUDE_IDLE_EMPTY=$'⏺ PONG\n\n✻ Cooked for 6s · done 8:32 PM\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ \n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
CLAUDE_FRESH_PLACEHOLDER=$'  Welcome back!\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ Try "write a test for cmux-helpers.sh"\n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
CLAUDE_DRAFT=$'⏺ PONG\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ --text draft text not submitted\n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
CLAUDE_MULTILINE_DRAFT=$'● high · /effort\n'"$CLAUDE_RULE"$'\n❯ first line of a draft\n  second line of the same draft\n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
CLAUDE_BUSY_FOOTER=$'· Flibbertigibbeting… (running Stop hooks… 7/8 · 5s · ↓ 262 tokens · thinking with high effort)\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ \n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt'
CLAUDE_BUSY_SPINNER_ONLY=$'· Flibbertigibbeting… (5s · ↓ 262 tokens)\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ \n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interru'
# Scrollback echoes earlier prompts as `❯ text` with no rule fences; the live
# composer is the LAST fenced ❯.
CLAUDE_SCROLLBACK_ECHO=$'❯ Reply with the word PONG only\n\n⏺ PONG\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ \n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
# A long draft pushed the bottom rule out of the sample: unknown, never empty.
CLAUDE_TRUNCATED=$'● high · /effort\n'"$CLAUDE_RULE"$'\n❯ a very long draft\n  that keeps going'
# A starship shell prompt also uses ❯ but has no rule above it.
STARSHIP_SHELL=$'~/Development/pm-os on  main [!?]\n❯ '
# Quoted Claude busy text in scrollback above an idle Claude composer.
CLAUDE_QUOTED_BUSY=$'⏺ The footer said "esc to interrupt" and the spinner read "· Thinking… (3s)".\n\nDone.\nNothing running.\n\n● high · /effort\n'"$CLAUDE_RULE"$'\n❯ \n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'

expect_state "claude idle empty" claude-empty "$CLAUDE_IDLE_EMPTY"
expect_state "claude fresh placeholder" claude-empty "$CLAUDE_FRESH_PLACEHOLDER"
expect_state "claude draft" claude-text "$CLAUDE_DRAFT"
expect_state "claude multiline draft" claude-text "$CLAUDE_MULTILINE_DRAFT"
expect_state "claude scrollback echo" claude-empty "$CLAUDE_SCROLLBACK_ECHO"
expect_state "claude truncated draft" none "$CLAUDE_TRUNCATED"
expect_state "starship shell prompt" none "$STARSHIP_SHELL"
expect_state "amp idle box" amp-empty "$IDLE_NEO"
expect_state "amp draft box" amp-text "$IDLE_DRAFT"
expect_state "unreadable screen" none ''
expect_safe "claude idle empty" "$CLAUDE_IDLE_EMPTY"
expect_safe "claude fresh placeholder" "$CLAUDE_FRESH_PLACEHOLDER"
expect_safe "claude scrollback echo" "$CLAUDE_SCROLLBACK_ECHO"
expect_safe "claude quoted busy text above idle composer" "$CLAUDE_QUOTED_BUSY"
expect_busy "claude footer esc to interrupt" "$CLAUDE_BUSY_FOOTER"
expect_busy "claude spinner with truncated footer" "$CLAUDE_BUSY_SPINNER_ONLY"
expect_idle "claude idle done line" "$CLAUDE_IDLE_EMPTY"
expect_idle "claude draft idle" "$CLAUDE_DRAFT"
if printf '%s\n' "$CLAUDE_DRAFT" | input_box_has_text; then ok "claude draft has text"; else fail "claude draft has text"; fi
if printf '%s\n' "$CLAUDE_DRAFT" | input_box_is_empty; then fail "claude draft not empty"; else ok "claude draft not empty"; fi
if printf '%s\n' "$CLAUDE_TRUNCATED" | input_box_is_empty; then fail "claude truncated not empty"; else ok "claude truncated not empty"; fi
if printf '%s\n' "$STARSHIP_SHELL" | input_box_is_empty; then fail "starship not empty"; else ok "starship not empty"; fi

# Exercise the actual send functions with a fake transport. Exact payload and
# send count matter: newline fan-out, plain Enter, and retries after an unknown
# transport outcome must fail even when the function returns the expected rc.
FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT
CIG_SEND_LOG="$FIXTURE/attribution.log"
CIG_CMUX=fake_cmux
fake_cmux() {
  printf '%s\n' "$1" >> "$FIXTURE/calls"
  printf '%s' "${!#}" > "$FIXTURE/payload"
  return "${TRANSPORT_RC:-0}"
}
cig_read() {
  local n
  n="$(cat "$FIXTURE/reads")"
  n=$((n + 1)); printf '%s' "$n" > "$FIXTURE/reads"
  if [[ "$n" -ge 3 && -n "${FINAL_SCREEN:-}" ]]; then
    printf '%s\n' "$FINAL_SCREEN"
  else
    printf '%s\n' "$TEST_SCREEN"
  fi
}
sleep() { :; }
expect_send() {
  local name="$1" fn="$2" screen="$3" want_rc="$4" want_payload="$5" rc=0 count
  TEST_SCREEN="$screen"
  : > "$FIXTURE/calls"; : > "$FIXTURE/payload"; echo 0 > "$FIXTURE/reads"
  "$fn" workspace:9 $'first line\nsecond line\rthird line' 0 1 || rc=$?
  count="$(wc -l < "$FIXTURE/calls" | tr -d ' ')"
  if [[ "$rc" == "$want_rc" && "$(cat "$FIXTURE/payload")" == "$want_payload" ]] \
    && { [[ -n "$want_payload" && "$count" == 1 ]] || [[ -z "$want_payload" && "$count" == 0 ]]; }; then
    ok "$name"
  else
    fail "$name (rc=$rc sends=$count payload=$(cat "$FIXTURE/payload"))"
  fi
}
PLAIN='first line second line third line\r'
expect_send "ordinary send defers while streaming" cig_safe_send "$CURRENT_STREAMING" 2 ''
expect_send "ordinary idle send stays one flattened message" cig_safe_send "$IDLE_NEO" 0 "$PLAIN"
expect_send "legacy allow-busy defers instead of steering" cig_safe_send_allow_busy "$CURRENT_STREAMING" 2 ''
expect_send "legacy allow-busy sends once idle (no slash prefix)" cig_safe_send_allow_busy "$IDLE_NEO" 0 "$PLAIN"
expect_send "force busy defers instead of steering" cig_force_send "$CURRENT_TOOLS" 2 ''
expect_send "force idle sends one flattened message" cig_force_send "$IDLE_NEO" 0 "$PLAIN"
expect_send "low-level legacy allow-busy also defers" cig_send_now_allow_busy "$CURRENT_STREAMING" 2 ''
for fn in cig_safe_send cig_safe_send_allow_busy cig_force_send; do
  expect_send "$fn preserves draft" "$fn" "$IDLE_DRAFT" 2 ''
  expect_send "$fn rejects unreadable screen" "$fn" '' 2 ''
  expect_send "$fn rejects truncated box" "$fn" $'╭────╮\n│ draft' 2 ''
  FINAL_SCREEN="$IDLE_DRAFT"
  expect_send "$fn final check catches new draft" "$fn" "$IDLE_NEO" 2 ''
  FINAL_SCREEN=''
  TRANSPORT_RC=1
  expect_send "$fn unknown transport is not retried" "$fn" "$IDLE_NEO" 3 "$PLAIN"
  TRANSPORT_RC=0
done
FINAL_SCREEN="$CURRENT_STREAMING"
expect_send "ordinary final check catches idle-to-busy race" cig_safe_send "$IDLE_NEO" 2 ''
expect_send "allow-busy final check catches idle-to-busy race" cig_safe_send_allow_busy "$IDLE_NEO" 2 ''
expect_send "force final check catches idle-to-busy race" cig_force_send "$IDLE_NEO" 2 ''

FINAL_SCREEN=''
# CoS Review Send: a human steer goes into a busy worker tab (Amp submits Enter as a
# steer) but never over a draft, never blind, and never twice after an unknown transport outcome.
expect_send "steer sends while streaming with an empty box" cig_steer_send "$CURRENT_STREAMING" 0 "$PLAIN"
expect_send "steer sends while running tools" cig_steer_send "$CURRENT_TOOLS" 0 "$PLAIN"
expect_send "steer sends to an idle tab as one message" cig_steer_send "$IDLE_NEO" 0 "$PLAIN"
expect_send "steer preserves draft" cig_steer_send "$IDLE_DRAFT" 2 ''
expect_send "steer rejects unreadable screen" cig_steer_send '' 2 ''
expect_send "steer rejects truncated box" cig_steer_send $'╭────╮\n│ draft' 2 ''
FINAL_SCREEN="$IDLE_DRAFT"
expect_send "steer final check catches new draft" cig_steer_send "$CURRENT_STREAMING" 2 ''
FINAL_SCREEN=''
TRANSPORT_RC=1
expect_send "steer unknown transport is not retried" cig_steer_send "$CURRENT_STREAMING" 3 "$PLAIN"
TRANSPORT_RC=0
if grep -q ' | steer | ' "$CIG_SEND_LOG" && grep -q ' | steer-idle | ' "$CIG_SEND_LOG"; then
  ok "steer sends are attributed as steer / steer-idle"
else fail "steer attribution missing in send log"; fi

expect_send "explicit queue also accepts idle-empty" cig_safe_queue "$IDLE_NEO" 0 "$PLAIN"
expect_send "explicit queue protects drafts" cig_safe_queue "$IDLE_DRAFT" 2 ''
expect_send "explicit queue rejects unreadable" cig_safe_queue '' 2 ''
FINAL_SCREEN="$IDLE_DRAFT"
expect_send "explicit queue final check protects a new draft" cig_safe_queue "$IDLE_NEO" 2 ''
FINAL_SCREEN=''

# A stateful palette fixture catches premature Enter, missing draft readback,
# disabled queue commands, and false success when the queued card is absent.
fake_cmux() { printf '%s\n' "${!#}" >> "$FIXTURE/queue-calls"; }
cig_read() {
  local n
  n="$(wc -l < "$FIXTURE/queue-calls" | tr -d ' ')"
  if [[ "$n" == "${QUEUE_BAD_STAGE:-none}" ]]; then printf 'unreadable'; return; fi
  if [[ -n "${QUEUE_RACE:-}" && "$n" -ge 3 ]]; then
    if [[ "$QUEUE_RACE" == post-submit && "$n" == 3 ]]; then
      printf 'Command Palette\n│ > queue     │\n│ prompt  queue message (alias: queue) │\n'
    elif [[ "$n" == 3 || "$QUEUE_RACE" == stuck || ( "$QUEUE_RACE" == twice && "$n" == 4 ) || ( "$QUEUE_RACE" == post-submit && "$n" == 4 ) ]]; then
      printf 'Command Palette\n│ > queue     │\n'
      if [[ "$QUEUE_RACE" == footer ]]; then
        printf 'Can only use prompt: queue message while the agent is working\n'
      elif [[ "$QUEUE_RACE" == bare ]]; then
        printf '│ queue message (unavailable) │\n'
      else
        printf '│ prompt  queue message (unavailable) (alias: queue) │\n'
      fi
      printf '╭────────╮\n│ first line second line │\n'
      if [[ "$QUEUE_RACE" == busy ]]; then
        printf '╰ ≈ Streaming ─╯\n'
      else
        printf '╰────────╯\n'
      fi
    elif [[ "$(tail -1 "$FIXTURE/queue-calls")" == '\r' ]]; then
      case "$QUEUE_RACE" in
        no-proof) printf '%s\n' "$IDLE_NEO" ;;
        legacy) printf '%s\n' "$LEGACY_BUSY_TOK" ;;
        *) printf '%s\n' "$CURRENT_STREAMING" ;;
      esac
    else
      case "$QUEUE_RACE" in
        mismatch) printf '╭────────╮\n│ first line changed draft │\n╰────────╯\n' ;;
        rebound|busy) printf '╭────────╮\n│ first line second line │\n╰ ≈ Streaming ─╯\n' ;;
        truncated) printf '╭────────╮\n│ first line second line │\n' ;;
        *) printf '╭────────╮\n│ first line second line │\n╰────────╯\n' ;;
      esac
    fi
    return 0
  fi
  case "$n" in
    0) printf '%s\n' "$CURRENT_STREAMING" ;;
    1) printf '╭────────╮\n│ first line second line │\n╰ ≈ Streaming ─╯\n' ;;
    2) printf 'Command Palette\n│ >   │\n' ;;
    3) printf 'Command Palette\n│ > queue     │\n│ prompt  queue message (alias: queue) │\n' ;;
    4) printf '│ first line second line   Enter to steer │\n%s\n' "$CURRENT_STREAMING" ;;
  esac
}
for stage in none 1 2 3 4; do
  : > "$FIXTURE/queue-calls"
  QUEUE_BAD_STAGE="$stage"
  rc=0
  cig_safe_queue workspace:9 $'first line\nsecond line' || rc=$?
  if [[ "$stage" == none ]]; then
    printf '%s\n' 'first line second line' $'\x0f' queue '\r' > "$FIXTURE/expected"
    if [[ "$rc" == 0 ]] && cmp -s "$FIXTURE/expected" "$FIXTURE/queue-calls"; then
      ok "queue: exact palette sequence, one submission, queued-card proof"
    else fail "queue: sequence or proof failed (rc=$rc)"; fi
  elif [[ "$rc" == 3 && "$(wc -l < "$FIXTURE/queue-calls" | tr -d ' ')" == "$stage" ]]; then
    ok "queue: stage $stage failure is unknown with no retries"
  else fail "queue: unsafe retry or false success at stage $stage"; fi
done

QUEUE_BAD_STAGE=none
for QUEUE_RACE in idle busy mismatch footer twice stuck rebound truncated no-proof legacy bare post-submit; do
  : > "$FIXTURE/queue-calls"; : > "$CIG_SEND_LOG"
  printf '%s\n' 'first line second line' $'\x0f' queue > "$FIXTURE/expected"
  want_rc=3
  if [[ "$QUEUE_RACE" == post-submit ]]; then
    printf '%s\n' '\r' >> "$FIXTURE/expected"
  fi
  printf '%s\n' $'\x1b' >> "$FIXTURE/expected"
  if [[ "$QUEUE_RACE" == twice || "$QUEUE_RACE" == stuck ]]; then
    printf '%s\n' $'\x1b' >> "$FIXTURE/expected"
  fi
  case "$QUEUE_RACE" in
    idle|footer|twice|no-proof|legacy|bare|post-submit)
      printf '%s\n' '\r' >> "$FIXTURE/expected"
      [[ "$QUEUE_RACE" != no-proof ]] && want_rc=0 ;;
  esac
  rc=0
  cig_safe_queue workspace:9 $'first line\nsecond line' || rc=$?
  if [[ "$rc" != "$want_rc" ]] || ! cmp -s "$FIXTURE/expected" "$FIXTURE/queue-calls"; then
    fail "queue race $QUEUE_RACE: rc=$rc (want $want_rc) or unsafe key sequence"
  elif [[ "$(tail -1 "$FIXTURE/expected")" == '\r' ]]; then
    if [[ "$(grep -c ' | queue-fallback-idle | ' "$CIG_SEND_LOG")" == 1 ]] \
      && cig_read | input_box_is_empty; then
      ok "queue race $QUEUE_RACE: Escape, verified draft, one Enter, proof rc=$rc, fallback attribution"
    else fail "queue race $QUEUE_RACE: missing empty-box proof or fallback attribution"; fi
  elif grep -q ' | queue-fallback-idle | ' "$CIG_SEND_LOG"; then
    fail "queue race $QUEUE_RACE: incorrectly logged an idle submission"
  elif ! cig_read | grep -q '│ first line'; then
    fail "queue race $QUEUE_RACE: draft not preserved"
  else
    ok "queue race $QUEUE_RACE: unknown, no Enter, draft preserved"
  fi
done

# --- Claude Code transport ----------------------------------------------------
# A long `cmux send "<text>\r"` into Claude Code types the text but the \r
# becomes a newline, so the handoff sits unsubmitted. Claude sends type the text, then a separate CR,
# then one more CR only while the draft still ends with our text.
CLAUDE_QUEUED_HINT=$'✢ Nesting… (12s · ↓ 81 tokens)\n\n'"$CLAUDE_RULE"$'\n❯ Press up to edit queued messages\n'"$CLAUDE_RULE"$'\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt'
expect_state "claude queued-message hint" claude-empty "$CLAUDE_QUEUED_HINT"
claude_screen() {  # $1 composer text ("" = empty), $2 busy|idle
  local footer='  ⏵⏵ bypass permissions on (shift+tab to cycle)'
  local spin=$'✻ Cooked for 6s · done 8:32 PM'
  [[ "$2" == busy ]] && { footer="$footer · esc to interrupt"; spin=$'· Nesting… (5s · ↓ 9 tokens)'; }
  printf '%s\n\n● high · /effort\n%s\n❯ %s\n%s\n%s\n' "$spin" "$CLAUDE_RULE" "$1" "$CLAUDE_RULE" "$footer"
}
# Stateful fake: typed text shows as a draft; each CR submits only once the
# CR count reaches CLAUDE_SUBMIT_ON (0 = never); OTHER_DRAFT replaces the draft
# after the first CR; REFUSE makes the text send fail the way cmux refuses;
# SHOWN, when set, is what the composer shows instead of the typed text.
fake_cmux() {
  local payload="${!#}"
  printf '%s\n' "$payload" >> "$FIXTURE/claude-calls"
  if [[ -n "${REFUSE:-}" && "$payload" != '\r' ]]; then
    echo "Error: send: the agent prompt holds text someone is typing, so nothing was sent." >&2; return 1
  fi
  if [[ "$payload" == '\r' ]]; then
    local n; n=$(( $(cat "$FIXTURE/crs") + 1 )); echo "$n" > "$FIXTURE/crs"
    if [[ "$CLAUDE_SUBMIT_ON" != 0 && "$n" -ge "$CLAUDE_SUBMIT_ON" ]]; then : > "$FIXTURE/draft"
    elif [[ -n "${OTHER_DRAFT:-}" ]]; then printf '%s' "$OTHER_DRAFT" > "$FIXTURE/draft"; fi
  else
    printf '%s' "${SHOWN-$payload}" > "$FIXTURE/draft"
  fi
  return 0
}
cig_read() { claude_screen "$(cat "$FIXTURE/draft")" "${CLAUDE_BUSY:-idle}"; }
expect_claude() {  # name fn want_rc want_calls(newline-joined)
  local rc=0
  : > "$FIXTURE/claude-calls"; : > "$FIXTURE/draft"; echo 0 > "$FIXTURE/crs"
  "$2" workspace:9 $'Hand off\nthe Launchpad task now' 0 1 || rc=$?
  if [[ "$rc" == "$3" && "$(cat "$FIXTURE/claude-calls")" == "$4" ]]; then ok "$1"
  else fail "$1 (rc=$rc calls=$(tr '\n' '|' < "$FIXTURE/claude-calls"))"; fi
}
TYPED='Hand off the Launchpad task now'
CLAUDE_SUBMIT_ON=1
expect_claude "claude idle send types text, then one separate CR" cig_safe_send 0 "$TYPED"$'\n\\r'
CLAUDE_SUBMIT_ON=2
expect_claude "claude draft left by the first CR gets one more CR" cig_safe_send 0 "$TYPED"$'\n\\r\n\\r'
CLAUDE_SUBMIT_ON=0
expect_claude "claude draft that never submits is unknown after two CRs" cig_safe_send 3 "$TYPED"$'\n\\r\n\\r'
OTHER_DRAFT='someone else typing'
expect_claude "claude never sends a second CR onto a draft that is not ours" cig_safe_send 3 "$TYPED"$'\n\\r'
OTHER_DRAFT=''
CLAUDE_SUBMIT_ON=1; REFUSE=1
expect_claude "claude cmux refusal (nothing typed) defers with no CR" cig_safe_send 2 "$TYPED"
REFUSE=''
SHOWN=''
expect_claude "claude sends no CR when the typed text never shows (a dialog may hold focus)" cig_safe_send 3 "$TYPED"
SHOWN='[Pasted text #1 +38 lines]'
expect_claude "claude collapsed paste placeholder counts as our text" cig_safe_send 0 "$TYPED"$'\n\\r'
unset SHOWN
CLAUDE_BUSY=busy
expect_claude "claude ordinary send still defers while a turn runs" cig_safe_send 2 ''
expect_claude "claude busy queue submits natively, no Amp palette" cig_safe_queue 0 "$TYPED"$'\n\\r'
expect_claude "claude steer while busy types text, then CR" cig_steer_send 0 "$TYPED"$'\n\\r'
CLAUDE_BUSY=idle
printf '%s' "$TYPED" > "$FIXTURE/draft"
CIG_VERIFY_DELAY=0
rc=0; cig_verify_sent workspace:9 "$TYPED" || rc=$?
if [[ "$rc" == 1 ]]; then ok "claude verify: our text still in the composer is not delivered"
else fail "claude verify: draft read as delivered (rc=$rc)"; fi

echo
echo "cmux-inject-guard: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
