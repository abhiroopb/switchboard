#!/usr/bin/env bash
#
# cmux-inject-guard.sh — shared guard for programmatically sending a prompt
# (a "user turn") into a live Amp or Claude Code TUI running inside a cmux
# workspace. The classifiers recognise both composers (Amp ╭…╮ box, Claude
# `❯` line fenced by rules). Amp submits with one atomic `cmux send
# "<text>\r"`. Claude Code does not: a long typed burst reads as a paste and
# its trailing \r becomes a newline in the draft, which then sits unsubmitted.
# Claude sends
# type the text, then submit with a separate CR (cig_claude_submit).
#
# WHY: any automation that types into a workspace CoS routes to — the digest
# loop, CoS handoff seeding, wrap/close prompts, heartbeats, comms/notify
# flushes — can land ON TOP of whatever the user is actively typing there, or
# steer running work. That mangles the user's prompt. This library
# is the single place that decides "is it safe to inject right now?" and does
# the send safely, so every caller inherits the same protection.
#
# Rules enforced:
#   1. If the user is typing a prompt        -> don't inject (box has text).
#   2. If there's text sitting in the box    -> don't inject (same check).
#   3. If something is already running        -> don't inject (agent mid-turn).
# Plus an anti-race double-sample + a final same-instant re-check right before
# the send, so a keystroke landing mid-check can't get our text appended.
#
# Amp 0.0.1789000905 submits plain Enter as steering when busy.
# Live verification disproved the old /queue text command: '/' opened the
# palette and 'queue <text>' became a steer. `amp config keymap` exposes the
# unbound prompt.queue palette action, not a safe atomic text prefix. Ordinary
# sends, including legacy allow_busy/force, defer while busy. Only explicitly
# queueable CoS reports use cig_safe_queue's verified palette sequence.
# Report/handoff callers keep their durable pending queues. Native
# same-task follow-ups in route-to-workspace.sh remain deliberate steering.
# A successful send means transport accepted, not task acknowledgment.
#
# Source this file; do not execute it. Functions are prefixed `cig_` plus the
# three stdin classifiers `screen_is_busy` / `input_box_has_text` /
# `input_box_is_empty`.
#
# Usage:
#   source "$REPO_ROOT/system/scripts/lib/cmux-inject-guard.sh"
#   cig_safe_send "workspace:9" "some prompt" [max_wait_s] [poll_s]
#     returns 0 = sent, 2 = deferred (never became safe within max_wait_s)
#   cig_safe_send_allow_busy "workspace:9" "some prompt" [max_wait_s] [poll_s]
#     compatibility alias for cig_safe_send; busy sessions now defer
#   cig_safe "workspace:9"        # 0 = safe to inject, 1 = unsafe/unknown
#   cig_force_send "workspace:9" "text"   # guarded attempt; no safety bypass
#   cig_steer_send "workspace:9" "text" [max_wait_s] [poll_s]
#     human-initiated steer into a task's own thread (CoS Review Send): sends
#     while busy, defers only for a draft or an unreadable box; 0/2/3

# Guard against double-sourcing.
[[ -n "${_CIG_LOADED:-}" ]] && return 0
_CIG_LOADED=1

# cmux CLI: $CMUX/$CMUX_BIN if set, else whatever PATH offers, else the copy inside cmux.app. A
# Finder-launched caller (CoS Review.app) runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin, where a bare
# `cmux` is not found (that reads as a false "CoS tab is not open").
# On wmux (Windows/WSL) mux_init points CMUX_BIN at the cmux-compatible shim first; on macOS it is a no-op.
# shellcheck source=mux.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mux.sh" && mux_init
CIG_CMUX="${CMUX:-${CMUX_BIN:-$(command -v cmux 2>/dev/null || echo /Applications/cmux.app/Contents/Resources/bin/cmux)}}"
# Test sandbox: only an explicit stub, never the real cmux found on PATH.
[[ "${SWITCHBOARD_TEST_SANDBOX:-0}" == "1" && -z "${CMUX:-}${CMUX_BIN:-}" ]] && CIG_CMUX="/nonexistent/switchboard-test-sandbox/cmux"
CIG_READ_LINES="${CIG_READ_LINES:-40}"

# TRUE if the agent is mid-turn / actively working. Reads a screen dump on stdin.
#
# Matches the Amp Neo TUI's live-turn indicators. The two most reliable are:
#   - the status line verb + token counter, e.g. "Thinking 89 tok",
#     "Working…", "Generating…", "Compacting…" (previously we only matched the
#     ellipsis forms "Thinking…"/"Thinking...", so "Thinking 89 tok" slipped
#     through and we clobbered a running turn — the exact bug this fixes).
#   - the input-box hint "Enter to steer" / "to steer", which older TUI builds
#     show ONLY while a turn is active (idle shows "Enter to send").
#   - the live prompt-box footer. Current Amp Neo builds render the bottom
#     border of the prompt box as "╰ ≈ <Verb> ────" while a turn is active
#     (e.g. "╰ ≈ Streaming ─", "╰ ≈ Running Tools ─") and as a plain
#     "╰──────" when idle, and they no longer print "Enter to steer". The
#     footer marker is matched directly, so a streaming turn with no tool call
#     in flight reads as busy.
# Match status words only when they have TUI-specific progress text. A plain
# word such as "working" or "streaming" can remain in conversation scrollback
# while Amp is idle and must not block guarded delivery indefinitely.
#
# Claude Code (SWITCHBOARD_AGENT=claude, verified on Claude Code
# 2.1.x in cmux 0.65) has no ╭…╮ box. Its composer is a `❯` line fenced by two
# horizontal rules (`────`), with the status footer below the bottom rule.
# While a turn runs the footer carries "esc to interrupt" and a spinner line
# sits just above the composer: "· Flibbertigibbeting… (running Stop hooks…
# 7/8 · 5s · ↓ 262 tokens)". The verb is random, so the shape "<Verb>… (" is
# matched instead. Idle shows "✻ Cooked for 6s · done 8:32 PM" (no "(" after
# the ellipsis) and a footer without "esc to interrupt".
# The classifiers below match ╭ ❯ ─ with awk and grep, which only see those as
# characters in a UTF-8 locale. A shell started without one (a detached waiter,
# a non-login bash on Windows) would read every screen as busy and never send.
if [[ -z "${LC_ALL:-}${LC_CTYPE:-}${LANG:-}" ]]; then
  export LC_CTYPE=C.UTF-8
fi

screen_is_busy() {
  # Scope: only the region from two lines above
  # the LAST prompt-box top border (╭…╮) downward is live status. Everything
  # above it is conversation scrollback, which can legitimately QUOTE a busy
  # footer (e.g. a report that says '"╰ ≈ Streaming ─" was missed') while the
  # agent is idle. With no Amp box, the Claude composer (a `❯` line
  # directly under a rule) scopes the region from six lines above it, which
  # covers a two-line spinner, the blank, the "● high · /effort" line and the
  # rule. A dump with neither is scanned in full.
  awk '{ l[NR] = $0 } /╭.*╮/ { top = NR }
       /^[[:space:]]*(❯|>([^>]|$))/ { if (NR > 1 && l[NR-1] ~ /^[[:space:]]*─{10,}[[:space:]]*$/) ctop = NR }
       END {
         if (top) s = (top > 2 ? top - 2 : 1)
         else if (ctop) s = (ctop > 6 ? ctop - 6 : 1)
         else s = 1
         for (i = s; i <= NR; i++) print l[i] }' |
  grep -qiE '╰[[:space:]]*[≈≋∼]|[≈≋∼][[:space:]]*(Streaming|Thinking|Working|Generating|Compacting|Reflecting|Exploring|Running)|to steer|esc to interrupt|Running Tools|Running [0-9]+ commands?|\b(Streaming|Thinking|Working|Generating|Compacting|Reflecting)(…|\.\.\.|[[:space:]]+[0-9]+[[:space:]]+tok)|^[[:space:]]*[^[:alnum:][:space:]]?[[:space:]]*[A-Z][a-z-]+(…|\.\.\.)[[:space:]]*\('
}

# Classify the live composer on a screen dump (stdin). Prints one word:
#   amp-text / amp-empty       Amp Neo ╭…╮ box with or without a draft
#   claude-text / claude-empty Claude Code `❯` composer fenced by two rules
#   none                       no composer positively identified (truncated,
#                              a plain shell, or an unknown TUI)
# Claude Code may draw the prompt as a plain `>` instead of `❯` (on Windows),
# so both marks count; a `>` only
# fenced by rules (no shell draws them) and never `>>`.
# The Amp box wins when present. For Claude the LAST `❯` line whose previous
# line is a rule is the live composer; earlier `❯` lines are echoed prompts in
# scrollback, and a starship shell `❯` has no rule above it. Both fences must
# be visible: a long multiline draft can push the bottom rule out of the sample
# and must read as none, never empty.
cig_composer_state() {
  awk '
    { line[NR] = $0 }
    /╭.*╮/ { top = NR; bot = 0 }          # new box-top -> reset bottom
    /╰.*╯/ { if (top && !bot) bot = NR }  # first box-bottom after that top
    /^[[:space:]]*(❯|>([^>]|$))/ { if (NR > 1 && line[NR-1] ~ /^[[:space:]]*─{10,}[[:space:]]*$/) { ctop = NR; cbot = 0 } }
    /^[[:space:]]*─{10,}[[:space:]]*$/ { if (ctop && NR > ctop && !cbot) cbot = NR }
    END {
      if (top) {
        if (!bot || bot <= top) { print "none"; exit }
        for (i = top + 1; i < bot; i++) {
          s = line[i]
          sub(/^[[:space:]]*│/, "", s)
          sub(/│[[:space:]]*$/, "", s)
          gsub(/[[:space:]]/, "", s)
          if (length(s) > 0) { print "amp-text"; exit }
        }
        print "amp-empty"; exit
      }
      if (ctop) {
        if (!cbot) { print "none"; exit }
        for (i = ctop; i < cbot; i++) {
          s = line[i]
          if (i == ctop) {
            sub(/^[[:space:]]*(❯|>)/, "", s)
            # Fresh sessions show a dimmed placeholder hint on the ❯ line,
            # e.g. `❯ Try "write a test for cmux-helpers.sh"`. It is not a
            # draft; the dump cannot carry colour, so match its exact shape.
            if (s ~ /^[[:space:]]*Try "[^"]*"[[:space:]]*$/) s = ""
            # While a turn runs, a queued message leaves this hint in the
            # empty composer.
            if (s ~ /^[[:space:]]*Press up to edit queued messages[[:space:]]*$/) s = ""
            # A dimmed prompt suggestion has no fixed shape. On wmux, read-screen in the shim
            # removes it by cursor position (hide_suggestion in mux/cmux-shim.py). cmux on
            # macOS gives no cursor here, so a suggestion still defers there.
          }
          gsub(/[[:space:]]/, "", s)
          if (length(s) > 0) { print "claude-text"; exit }
        }
        print "claude-empty"; exit
      }
      print "none"
    }
  '
}

# TRUE if the live composer holds user-typed (unsubmitted) text.
# Covers "I'm typing" and "a draft is sitting in the box" — both defer.
input_box_has_text() {
  case "$(cig_composer_state)" in
    amp-text|claude-text) return 0 ;;
    *) return 1 ;;
  esac
}

# TRUE only when the live composer is positively identified and empty.
# Unknown or truncated prompt state is unsafe. In particular, a long multiline
# draft can push the box's top or bottom border outside the read-screen sample;
# treating "could not find text" as "empty" caused automation to append to a
# real draft. Injection callers must use this positive empty-state check.
input_box_is_empty() {
  case "$(cig_composer_state)" in
    amp-empty|claude-empty) return 0 ;;
    *) return 1 ;;
  esac
}

# Resolve the first terminal surface for a workspace (needed for read-screen).
cig_surface() {
  "$CIG_CMUX" tree --workspace "$1" 2>/dev/null | grep -oE 'surface:[0-9]+' | head -1
}

# Print the last CIG_READ_LINES of a workspace's screen (empty on failure).
cig_read() {
  local ref="$1" sf
  sf="$(cig_surface "$ref")" || return 1
  [[ -z "$sf" ]] && return 1
  "$CIG_CMUX" read-screen --workspace "$ref" --surface "$sf" --lines "$CIG_READ_LINES" 2>/dev/null || true
}

# 0 = safe to inject (readable + idle + empty box), 1 = unsafe or unknown.
# An unreadable screen is treated as UNSAFE (fail safe: better to defer than
# to blast a prompt into a surface we can't inspect).
cig_safe() {
  local screen
  screen="$(cig_read "$1")"
  [[ -z "$screen" ]] && return 1
  printf '%s\n' "$screen" | screen_is_busy && return 1
  printf '%s\n' "$screen" | input_box_is_empty || return 1
  return 0
}

# Best-effort send-attribution log. Every programmatic injection into a live
# Amp TUI leaves one line here (timestamp | caller | target | text prefix), so
# a mystery user turn in a workspace can be traced back to its sender.
# Never blocks or fails a send.
# `builtin cd`: login shells (bash -lc) load RVM, whose `cd` function dies under
# a sourcing caller's `set -u` ("rvm_bash_nounset: unbound variable"), and the
# failed substitution then aborts the caller via `set -e`.
CIG_SEND_LOG="${CIG_SEND_LOG:-$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/../../.." 2>/dev/null && pwd)/system/logs/cmux-sends.log}"
cig_log_send() {
  local ref="$1" text="$2" mode="$3"
  {
    mkdir -p "$(dirname "$CIG_SEND_LOG")" 2>/dev/null
    printf '%s | %s | %s | %s | %.100s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mode" "${0##*/}" "$ref" \
      "$(printf '%s' "$text" | tr '\n\r' '  ')" >> "$CIG_SEND_LOG"
  } 2>/dev/null || true
}

# A raw newline inside a send payload is executed by the Amp TUI as Enter,
# which splits one intended message into many queued user turns — each line
# lands as its own turn as the agent finishes work. Guarded sends are single-message by contract,
# so embedded CR/LF always collapse to spaces before the send.
cig_flatten() {
  printf '%s' "$1" | tr '\r\n' '  '
}

# Whitespace-free text of the live Claude composer (between its two rules);
# empty when no fenced Claude composer is on the screen (stdin).
cig_claude_draft() {
  awk '
    { line[NR] = $0 }
    /^[[:space:]]*(❯|>([^>]|$))/ { if (NR > 1 && line[NR-1] ~ /^[[:space:]]*─{10,}[[:space:]]*$/) { ctop = NR; cbot = 0 } }
    /^[[:space:]]*─{10,}[[:space:]]*$/ { if (ctop && NR > ctop && !cbot) cbot = NR }
    END {
      if (!ctop || !cbot) exit
      body = ""
      for (i = ctop; i < cbot; i++) { s = line[i]; if (i == ctop) sub(/^[[:space:]]*(❯|>)/, "", s); body = body s }
      gsub(/[[:space:]]/, "", body)
      print body
    }'
}

# Claude Code transport. The caller has already
# proved the composer empty. Type the text, wait until the draft shows its
# tail, then press CR as its own send. If the draft is still there, press CR
# once more (the Enter-plus-CR that worked by hand), but only while the draft
# still ends with our text, so a draft someone typed after our submit is never
# sent. Verified live on Claude Code 2.1.x: a separate CR submits a 1.5 KB
# draft, and while a turn runs it queues the message.
#   0 = submitted (composer back to empty); 2 = cmux refused to type because
#   it reads the prompt as holding text (for example the "Press up to edit
#   queued messages" hint while a queued message waits), so nothing was sent;
#   3 = typed, outcome unknown (the draft may remain; callers never retry).
# Does the Claude draft hold our text? Its tail, or the collapsed
# "[Pasted text #N ...]" placeholder Claude shows for a long paste.
cig_claude_ours() {
  [[ -n "$1" ]] || return 1
  [[ "$2" == *"$1" || "$2" == *"[Pastedtext#"* ]]
}

cig_claude_submit() {
  local ref="$1" text="$2" key screen draft state i cr err
  # Read deep enough that a tall wrapped draft keeps both composer rules.
  local CIG_READ_LINES=200
  key="$(printf '%s' "$text" | tr -d '[:space:]')"
  (( ${#key} > 24 )) && key="${key: -24}"
  # Record the paste before typing: if this process dies between the text and
  # the CR (a caller's `timeout` fires), the next guarded send
  # knows the draft is ours, not the owner's (cig_recover_own_paste).
  cig_note_paste "$ref" "$text"
  if ! err="$("$CIG_CMUX" send --workspace "$ref" "$text" 2>&1 >/dev/null)"; then
    if [[ "$err" == *"nothing was sent"* ]]; then cig_clear_paste "$ref"; return 2; fi
    return 3
  fi
  for (( i = 0; i < 10; i++ )); do
    sleep 0.2
    screen="$(cig_read "$ref")"
    draft="$(printf '%s\n' "$screen" | cig_claude_draft)"
    cig_claude_ours "$key" "$draft" && break
  done
  sleep "${CIG_CLAUDE_SETTLE:-0.3}"
  for cr in 1 2; do
    # Every CR goes only onto our own unsubmitted text. If it never showed up,
    # the screen may be a dialog, and a CR there would answer it.
    cig_claude_ours "$key" "$draft" || return 3
    "$CIG_CMUX" send --workspace "$ref" '\r' >/dev/null 2>&1 || return 3
    for (( i = 0; i < 10; i++ )); do
      sleep 0.3
      screen="$(cig_read "$ref")"
      state="$(printf '%s\n' "$screen" | cig_composer_state)"
      [[ "$state" == claude-empty ]] && { cig_clear_paste "$ref"; return 0; }
      draft="$(printf '%s\n' "$screen" | cig_claude_draft)"
    done
  done
  return 3
}

# --- Own-paste ledger ---------------------------------------------------------
# A caller's `timeout` can kill a send after the guard types an envelope but
# before the CR. The envelope then sits in the Claude composer, and every later
# send reads it as a human draft and defers.
# cig_claude_submit now writes one ledger file per target before it types:
# epoch, the whitespace-free head (40 chars) and tail (24 chars) of the text.
# A clean submit, or a send cmux refused, removes it.
CIG_PASTE_DIR="${CIG_PASTE_DIR:-}"
if [[ -z "$CIG_PASTE_DIR" ]]; then
  if [[ "${SWITCHBOARD_TEST_SANDBOX:-0}" == "1" ]]; then
    CIG_PASTE_DIR="${TMPDIR:-/tmp}/cig-pastes-sandbox-$$"
  else
    CIG_PASTE_DIR="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/../../.." 2>/dev/null && pwd)/.state/cig-pastes"
  fi
fi
# A ledger entry older than this is ignored (seconds; default one day).
CIG_OWN_PASTE_MAX_AGE="${CIG_OWN_PASTE_MAX_AGE:-86400}"

_cig_paste_file() { printf '%s/%s.paste' "$CIG_PASTE_DIR" "${1//[^A-Za-z0-9_-]/_}"; }

cig_note_paste() {
  local ref="$1" flat head tail
  flat="$(printf '%s' "$2" | tr -d '[:space:]')"
  head="${flat:0:40}"
  tail="$flat"; (( ${#tail} > 24 )) && tail="${tail: -24}"
  {
    mkdir -p "$CIG_PASTE_DIR" &&
      printf '%s\n%s\n%s\n' "$(date +%s)" "$head" "$tail" > "$(_cig_paste_file "$ref")"
  } 2>/dev/null || true
}

cig_clear_paste() { rm -f "$(_cig_paste_file "$1")" 2>/dev/null || true; }

# 0 when the whitespace-free Claude draft $2 is the guard's own unsubmitted
# automation paste on $1: a fresh ledger entry whose text is an automation
# envelope ("[AUTOMATION job=…", the only kind recovered, so CoS and workstream
# tabs are never touched) and a draft that starts with its head (which carries
# the job id) and either ends with its tail or ends with the one
# "[Pasted text #N …]" placeholder Claude folds a long paste into. Text typed
# before our paste fails the head test, text typed after it fails the tail and
# placeholder tests, and a second paste fails the one-placeholder test.
cig_own_paste() {
  local ref="$1" draft="$2" file at head tail now marker='[Pastedtext#' rest
  file="$(_cig_paste_file "$ref")"
  [[ -n "$draft" && -f "$file" ]] || return 1
  { read -r at; read -r head; read -r tail; } < "$file" 2>/dev/null || return 1
  [[ "$at" =~ ^[0-9]+$ && -n "$head" && -n "$tail" ]] || return 1
  now="$(date +%s)"
  (( now - at <= CIG_OWN_PASTE_MAX_AGE )) || return 1
  [[ "$head" == "[AUTOMATIONjob="* && "$draft" == "$head"* ]] || return 1
  [[ "$draft" == *"$tail" ]] && return 0
  # Claude folded the end into a placeholder: exactly one placeholder, and it
  # must be the last thing in the draft.
  local folded='\[Pastedtext#[0-9]+[^]]*\]$'
  rest="${draft//"$marker"/}"
  (( ${#head} >= 30 && ${#draft} - ${#rest} == ${#marker} )) && [[ "$draft" =~ $folded ]] && return 0
  return 1
}

# Submit the guard's own stranded automation paste (retry-safe by the
# Scheduled Jobs contract: a job whose receipt exists replies "duplicate", and
# comms-prompt.sh files a receipt nobody awaits as stale). Submitting, not
# erasing, because a Claude composer has no verified clear key and the job is
# still wanted. Only on an idle screen whose live composer holds that paste.
#   0 = submitted (composer empty again); CIG_RECOVERED_HEAD names it
#   1 = nothing to recover (no own paste, busy, unreadable, or a human draft)
#   3 = CR sent, composer did not clear (outcome unknown; never retried here)
CIG_RECOVERED_HEAD=""
cig_recover_own_paste() {
  local ref="$1" screen draft i
  local CIG_READ_LINES=200
  CIG_RECOVERED_HEAD=""
  screen="$(cig_read "$ref")"
  [[ -n "$screen" ]] || return 1
  printf '%s\n' "$screen" | screen_is_busy && return 1
  [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-text ]] || return 1
  draft="$(printf '%s\n' "$screen" | cig_claude_draft)"
  cig_own_paste "$ref" "$draft" || return 1
  cig_log_send "$ref" "$draft" "recover-own-paste"
  "$CIG_CMUX" send --workspace "$ref" '\r' >/dev/null 2>&1 || return 3
  for (( i = 0; i < 10; i++ )); do
    sleep 0.3
    screen="$(cig_read "$ref")"
    if [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-empty ]]; then
      CIG_RECOVERED_HEAD="$(sed -n 2p "$(_cig_paste_file "$ref")" 2>/dev/null)"
      cig_clear_paste "$ref"
      return 0
    fi
  done
  return 3
}

# One word for a workspace's inject state (alerts and diagnostics):
#   unreadable | busy | own-paste | draft | empty | unknown
cig_diagnose() {
  local ref="$1" screen state
  local CIG_READ_LINES=200
  screen="$(cig_read "$ref")"
  [[ -n "$screen" ]] || { echo unreadable; return 0; }
  printf '%s\n' "$screen" | screen_is_busy && { echo busy; return 0; }
  state="$(printf '%s\n' "$screen" | cig_composer_state)"
  case "$state" in
    claude-text)
      if cig_own_paste "$ref" "$(printf '%s\n' "$screen" | cig_claude_draft)"; then echo own-paste; else echo draft; fi ;;
    amp-text) echo draft ;;
    amp-empty|claude-empty) echo empty ;;
    *) echo unknown ;;
  esac
}

# Atomic text + Enter send with a final same-instant safety re-check. The
# re-check closes the race where a keystroke lands after the caller's last
# cig_safe. Keep text and Enter in ONE cmux operation: a separate send-key can
# race the terminal paste, return success, and still leave the text as a draft.
# That holds for Amp only; a Claude composer goes through cig_claude_submit.
# 0 = sent, 2 = aborted (became unsafe at the last instant).
cig_send_now() {
  local ref="$1" text screen
  text="$(cig_flatten "$2")"
  screen="$(cig_read "$ref")"
  if [[ -n "$screen" ]]; then
    printf '%s\n' "$screen" | screen_is_busy && return 2
    printf '%s\n' "$screen" | input_box_is_empty || return 2
  else
    return 2
  fi
  cig_log_send "$ref" "$text" "safe"
  if [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-empty ]]; then
    cig_claude_submit "$ref" "$text"
    return $?
  fi
  # rc 3 = the transport was ATTEMPTED and cmux returned nonzero. The text may
  # or may not have reached Amp, so callers must treat this as
  # delivery-outcome-unknown: no retry, no spawn, no alternate target.
  "$CIG_CMUX" send --workspace "$ref" "$text\r" >/dev/null 2>&1 || return 3
  return 0
}

# After a send whose transport returned nonzero (rc 3): did the line land?
# cmux's exit status is not the delivery outcome (most "unknown" report rows
# in fact reach CoS). The screen is.
#   0 = the text is on the destination's screen: delivered.
#   2 = positively NOT pasted: screen readable, agent idle, composer empty and
#       the text absent, so the caller may retry through the idle path.
#   1 = unknown: unreadable, mid-turn, a draft in the box (possibly our text,
#       half-typed), or the text scrolled out of view. Never retry on 1.
# Compare on ASCII letters, digits and punctuation only: the TUI wraps a long
# user line, prefixes it with a bar, and the slug/status prefix is ASCII.
cig_verify_sent() {
  local ref="$1" text key screen flat
  local CIG_READ_LINES=200
  text="$(cig_flatten "$2")"
  key="$(printf '%s' "$text" | LC_ALL=C tr -cd '[:alnum:][:punct:]' | cut -c1-48)"
  [[ -z "$key" ]] && return 1
  sleep "${CIG_VERIFY_DELAY:-0.5}"
  screen="$(cig_read "$ref")"
  [[ -z "$screen" ]] && return 1
  # Our text still sitting in a Claude composer is a draft, not a delivery.
  if [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-text ]] &&
    [[ "$(printf '%s\n' "$screen" | cig_claude_draft | LC_ALL=C tr -cd '[:alnum:][:punct:]')" == *"$key"* ]]; then
    return 1
  fi
  flat="$(printf '%s' "$screen" | LC_ALL=C tr -cd '[:alnum:][:punct:]')"
  [[ "$flat" == *"$key"* ]] && return 0
  printf '%s\n' "$screen" | screen_is_busy && return 1
  printf '%s\n' "$screen" | input_box_is_empty || return 1
  return 2
}

# Compatibility alias: the new Enter behavior makes busy delivery unsafe.
# Retain the final busy/draft check and the same 0/2/3 return contract.
cig_send_now_allow_busy() {
  cig_send_now "$@"
}

# Legacy force is now a guarded attempt, never a steer or draft overwrite.
# Returns the same 0/2/3 contract as guarded sends. Never retry rc 3.
cig_force_send() {
  cig_safe_send "$1" "$2" 0
}

# Poll until the workspace is safe, then send. Requires TWO consecutive safe
# reads ~1.3s apart (anti-race for between-keystroke gaps) before sending.
#   $1 ref, $2 text, $3 max_wait_s (default 0 = single attempt), $4 poll_s (3)
# Returns 0 = sent, 2 = deferred cleanly (never safe within the budget; nothing
# was typed), 3 = transport attempted but cmux failed (outcome unknown; the
# loop stops immediately and the caller must not retry or re-route).
cig_safe_send() {
  local ref="$1" text="$2" max_wait="${3:-0}" poll="${4:-3}"
  local start now send_rc recover_rc want_head
  start="$(date +%s)"
  want_head="$(printf '%s' "$(cig_flatten "$text")" | tr -d '[:space:]')"; want_head="${want_head:0:40}"
  while true; do
    # The composer may hold our own earlier automation paste that never got
    # its CR. Submit it instead of deferring behind it
    # forever; when it is this same text, that submit is this send.
    recover_rc=0
    cig_recover_own_paste "$ref" || recover_rc=$?
    if (( recover_rc == 0 )); then
      [[ -n "$want_head" && "$CIG_RECOVERED_HEAD" == "$want_head" ]] && return 0
    elif (( recover_rc == 3 )); then
      return 3
    fi
    if cig_safe "$ref"; then
      sleep 1.3
      if cig_safe "$ref"; then
        send_rc=0
        cig_send_now "$ref" "$text" || send_rc=$?
        (( send_rc == 0 )) && return 0
        (( send_rc == 3 )) && return 3
      fi
    fi
    now="$(date +%s)"
    (( now - start >= max_wait )) && return 2
    sleep "$poll"
  done
}

# Compatibility entry point for callers with durable retries (after-hours
# reports). Wait for idle within the caller's budget, then defer without typing.
cig_safe_send_allow_busy() {
  cig_safe_send "$@"
}

# Human-initiated steer into a worker's own thread (CoS Review "Send"):
# the owner presses Send on that task's item, which is the same act as
# the owner typing into the worker's tab, so a running turn does NOT defer: Amp
# submits plain Enter as a steer while busy and as a new turn while idle, and
# both are what the owner asked for. The draft protection stays: a non-empty or
# unreadable box defers (2) on two consecutive reads ~1.3s apart plus the final
# same-instant re-check. Never use this for automation; ordinary sends and
# reports keep cig_safe_send / cig_safe_queue.
#   $1 ref, $2 text, $3 max_wait_s (default 0), $4 poll_s (3)
# Returns 0 = sent, 2 = deferred (box held a draft or was unreadable for the
# whole budget; nothing typed), 3 = transport attempted, outcome unknown.
cig_steer_send() {
  local ref="$1" text max_wait="${3:-0}" poll="${4:-3}"
  local start now screen submit_rc
  text="$(cig_flatten "$2")"
  start="$(date +%s)"
  while true; do
    if cig_box_clear "$ref"; then
      sleep 1.3
      if cig_box_clear "$ref"; then
        screen="$(cig_read "$ref")"
        if [[ -n "$screen" ]] && printf '%s\n' "$screen" | input_box_is_empty; then
          if printf '%s\n' "$screen" | screen_is_busy; then
            cig_log_send "$ref" "$text" "steer"
          else
            cig_log_send "$ref" "$text" "steer-idle"
          fi
          if [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-empty ]]; then
            # rc 2: cmux typed nothing (a queued message holds the prompt); keep polling.
            submit_rc=0
            cig_claude_submit "$ref" "$text" || submit_rc=$?
            (( submit_rc != 2 )) && return "$submit_rc"
          else
            "$CIG_CMUX" send --workspace "$ref" "$text\r" >/dev/null 2>&1 || return 3
            return 0
          fi
        fi
      fi
    fi
    now="$(date +%s)"
    (( now - start >= max_wait )) && return 2
    sleep "$poll"
  done
}

# 0 = screen readable and the input box positively empty (busy or not).
cig_box_clear() {
  local screen
  screen="$(cig_read "$1")"
  [[ -z "$screen" ]] && return 1
  printf '%s\n' "$screen" | input_box_is_empty
}

# Explicit queue transport for CoS reports. Amp's documented palette action
# works without changing keybindings or Enter's account-dependent behavior.
# After typing starts, ANY incomplete stage is unknown (3), never retryable
# deferral (2). Only an unavailable queue action on an idle screen may dismiss
# the palette and submit the verified draft. Never erase a draft on failure.
cig_safe_queue() {
  local ref="$1" text screen i stage before after actual expected
  local fallback_idle=false
  local CIG_READ_LINES=200
  text="$(cig_flatten "$2")"
  printf '%s' "$text" | LC_ALL=C grep -q '[[:cntrl:]]' && return 2
  printf '%s' "$text" | grep -qE '\\[nrt]' && return 2
  for i in 1 2 3; do
    screen="$(cig_read "$ref")"
    [[ -n "$screen" ]] || return 2
    [[ "$screen" == *'Command Palette'* ]] && return 2
    printf '%s\n' "$screen" | input_box_is_empty || return 2
    (( i < 3 )) && sleep 1.3
  done
  # Idle sends need no palette; keep the existing final busy/draft recheck.
  if ! printf '%s\n' "$screen" | screen_is_busy; then
    cig_send_now "$ref" "$text"
    return $?
  fi
  # Claude Code queues a message submitted while a turn runs; the palette
  # below is Amp-only and would leave the text as a draft in a Claude tab.
  if [[ "$(printf '%s\n' "$screen" | cig_composer_state)" == claude-empty ]]; then
    cig_log_send "$ref" "$text" "queue"
    cig_claude_submit "$ref" "$text"
    return $?
  fi
  before="$(printf '%s\n' "$screen" | grep -c 'Enter to steer' || true)"
  cig_log_send "$ref" "$text" "queue"
  "$CIG_CMUX" send --workspace "$ref" "$text" >/dev/null 2>&1 || return 3
  sleep 0.3
  screen="$(cig_read "$ref")"
  actual="$(printf '%s\n' "$screen" | awk '
    /╭.*╮/ {body=""; inside=1; next}
    /╰.*╯/ {inside=0}
    inside {sub(/^[[:space:]]*│/, ""); sub(/│[[:space:]]*$/, ""); body=body $0}
    END {gsub(/[[:space:]]/, "", body); print body}')"
  expected="$(printf '%s' "$text" | tr -d '[:space:]')"
  [[ -n "$expected" && "$actual" == "$expected" ]] || return 3
  "$CIG_CMUX" send --workspace "$ref" $'\x0f' >/dev/null 2>&1 || return 3
  sleep 0.3
  screen="$(cig_read "$ref")"
  [[ "$screen" == *'Command Palette'* ]] || return 3
  "$CIG_CMUX" send --workspace "$ref" 'queue' >/dev/null 2>&1 || return 3
  sleep 0.3
  screen="$(cig_read "$ref")"
  [[ "$screen" == *'Command Palette'* ]] || return 3
  printf '%s\n' "$screen" | grep -qE '> queue[[:space:]]+│' || return 3
  for stage in 1 2; do
    if printf '%s\n' "$screen" | grep -qE 'queue message \(unavailable\)|Can only use prompt: queue message while the agent is working'; then
      # The turn can finish between the busy pre-read and opening the palette.
      # It can also finish after the enabled action was read, before Enter.
      # Escape only the known palette, then inspect the real prompt: its busy
      # footer may be stale under the overlay. Never submit a busy or changed draft.
      for i in 1 2; do
        "$CIG_CMUX" send --workspace "$ref" $'\x1b' >/dev/null 2>&1 || return 3
        sleep 0.3
        screen="$(cig_read "$ref")"
        [[ "$screen" == *'Command Palette'* ]] || break
      done
      [[ -n "$screen" && "$screen" != *'Command Palette'* ]] || return 3
      printf '%s\n' "$screen" | screen_is_busy && return 3
      printf '%s\n' "$screen" | input_box_has_text || return 3
      actual="$(printf '%s\n' "$screen" | awk '
        /╭.*╮/ {body=""; inside=1; next}
        /╰.*╯/ {inside=0}
        inside {sub(/^[[:space:]]*│/, ""); sub(/│[[:space:]]*$/, ""); body=body $0}
        END {gsub(/[[:space:]]/, "", body); print body}')"
      [[ "$actual" == "$expected" ]] || return 3
      before="$(printf '%s\n' "$screen" | grep -c 'Enter to steer' || true)"
      fallback_idle=true
      cig_log_send "$ref" "$text" "queue-fallback-idle"
    else
      printf '%s\n' "$screen" | grep -qE 'prompt[[:space:]]+queue message \(alias: queue\)' || return 3
    fi
    "$CIG_CMUX" send --workspace "$ref" '\r' >/dev/null 2>&1 || return 3
    sleep 0.3
    screen="$(cig_read "$ref")"
    if [[ "$screen" == *'Command Palette'* ]]; then
      # An explicit unavailable action proves the palette Enter did not submit
      # the draft. Only this result permits recovery; unreadable/unknown does not.
      if [[ "$stage" == 1 && "$fallback_idle" == false ]] &&
        printf '%s\n' "$screen" | grep -qE 'queue message \(unavailable\)|Can only use prompt: queue message while the agent is working'; then
        continue
      fi
      return 3
    fi
    printf '%s\n' "$screen" | input_box_is_empty || return 3
    # Modern idle submissions start a turn without a legacy "Enter to steer"
    # hint. Busy queue submissions still require the original queued-card proof.
    if [[ "$fallback_idle" == true ]] && printf '%s\n' "$screen" | screen_is_busy; then
      return 0
    fi
    after="$(printf '%s\n' "$screen" | grep -c 'Enter to steer' || true)"
    (( after > before )) || return 3
    return 0
  done
  return 3
}
