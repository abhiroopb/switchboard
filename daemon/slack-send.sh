#!/usr/bin/env bash
# Slack bot-token send helper (verifiable delivery).
#
# Posts via `pmos slack post`, which returns the created
# message `ts` — real proof the message exists, unlike the incoming webhook
# where HTTP 200 only means "payload accepted" (a blank message can still
# return HTTP 200). Falls back to the webhook
# when the bot path is unavailable (offline, pmos auth expired) so delivery
# degrades instead of failing.
#
# Caller must define log() and source daemon/slack-webhook.sh first (for the
# fallback and its empty-text guards).
#
# Usage:
#   ts=$(slack_send_message "message text")   # ts empty on webhook fallback
#
# Test hooks:
#   PM_OS_SLACK_SEND_CAPTURE_FILE  capture text locally instead of posting;
#                                  emits a fake ts
#   PM_OS_SLACK_SEND_FORCE_FAIL=1  simulate bot-path failure (exercises the
#                                  webhook fallback)
#   PM_OS_SLACK_SEND_REQUIRE_TS=1   fail without using the webhook fallback
#
# Native bullets (external-replies.md Rule 4 item 5): when
# the text holds `•`/`-`/`1.` text bullets, the bot path also passes
# `--blocks` built by `system/scripts/slack_format.py blocks`, so Slack shows
# true rich_text_list bullets. `--text` stays the verbatim fallback. The
# webhook fallback is text-only. In capture mode the blocks JSON is written to
# "${PM_OS_SLACK_SEND_CAPTURE_FILE}.blocks" (one line per send).

PM_OS_SLACK_BOT_CHANNEL="${PM_OS_SLACK_BOT_CHANNEL:-}"
PM_OS_SLACK_FORMAT_PY="${PM_OS_SLACK_FORMAT_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/system/scripts/slack_format.py}"

# Prints rich_text_list blocks JSON for text with bullet lines; prints nothing
# (and returns 0) when the text has no list lines or the converter is missing.
slack_native_blocks() {
  local text="$1" blocks
  [ -f "$PM_OS_SLACK_FORMAT_PY" ] || return 0
  blocks=$(python3 "$PM_OS_SLACK_FORMAT_PY" blocks "$text" 2>/dev/null) || blocks=""
  [ -n "$blocks" ] && printf '%s' "$blocks"
  return 0
}

slack_send_message() {
  local text="$1"
  # Same guard as the webhook: never send an empty/whitespace-only body.
  if [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ]; then
    log "Slack send: refusing to post empty/whitespace-only text"
    return 1
  fi

  if [ -n "${PM_OS_SLACK_SEND_CAPTURE_FILE:-}" ]; then
    if [ "${PM_OS_SLACK_SEND_FORCE_FAIL:-0}" = "1" ]; then
      if [ "${PM_OS_SLACK_SEND_REQUIRE_TS:-0}" = "1" ]; then
        log "Slack send capture: simulated bot-path failure; verified ts required"
        return 1
      fi
      log "Slack send capture: simulated bot-path failure; falling back to webhook"
      slack_webhook_post_text "$text" && return 0
      return 1
    fi
    printf '%s\n' "$text" >> "$PM_OS_SLACK_SEND_CAPTURE_FILE"
    local cap_blocks
    cap_blocks=$(slack_native_blocks "$text")
    [ -n "$cap_blocks" ] && printf '%s\n' "$cap_blocks" >> "${PM_OS_SLACK_SEND_CAPTURE_FILE}.blocks"
    printf '%s\n' "0000000000.000000"
    return 0
  fi

  # Hermetic guard: a test that stubs only the webhook (older tests) must
  # never reach the real bot path — route straight to the captured webhook.
  if [ -n "${PM_OS_SLACK_WEBHOOK_CAPTURE_FILE:-}" ]; then
    if [ "${PM_OS_SLACK_SEND_REQUIRE_TS:-0}" = "1" ]; then
      log "Slack send: verified ts required; webhook-only capture is unavailable"
      return 1
    fi
    slack_webhook_post_text "$text" && return 0
    return 1
  fi

  local out ts blocks
  local -a block_args=()
  blocks=$(slack_native_blocks "$text")
  [ -n "$blocks" ] && block_args=(--blocks "$blocks")
  out=$(timeout 60 pmos slack post \
    --channel "$PM_OS_SLACK_BOT_CHANNEL" \
    --text "$text" ${block_args[@]+"${block_args[@]}"} 2>>"$LOG_FILE") || out=""
  ts=$(printf '%s' "$out" | jq -r '.ts // .message.ts // empty' 2>/dev/null) || ts=""
  if [ -n "$ts" ]; then
    printf '%s\n' "$ts"
    return 0
  fi

  if [ "${PM_OS_SLACK_SEND_REQUIRE_TS:-0}" = "1" ]; then
    log "bot post failed; verified ts required"
    return 1
  fi
  log "bot post failed; falling back to webhook"
  slack_webhook_post_text "$text" && return 0
  return 1
}

# Direct command mode for callers that need a verified channel + root ts.
# Sourced callers keep the existing function-only behavior above.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
  LOG_FILE="${PM_OS_SLACK_SEND_LOG_FILE:-$HOME/.agents/.amp-plugin-logs/slack-send.log}"
  mkdir -p "$(dirname "$LOG_FILE")"
  log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }
  # shellcheck source=daemon/slack-webhook.sh
  source "$DAEMON_DIR/slack-webhook.sh"

  channel=""
  text=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --channel)
        channel="${2:-}"
        shift 2
        ;;
      --text)
        text="${2:-}"
        shift 2
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 2
        ;;
    esac
  done
  if [ -z "$channel" ] || [ -z "$text" ]; then
    echo "Usage: slack-send.sh --channel <channel-id> --text <message>" >&2
    exit 2
  fi

  PM_OS_SLACK_BOT_CHANNEL="$channel"
  PM_OS_SLACK_SEND_REQUIRE_TS=1
  ts="$(slack_send_message "$text")"
  jq -nc --arg channel "$channel" --arg ts "$ts" '{channel:$channel,ts:$ts}'
fi
