// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// daily-rollup-trigger — when the owner performs an outbound write via Slack,
// Gmail, Linear, or Tracker, debounce-trigger build-daily-activity-rollup.py
// so outputs/daily-rollup/latest.md stays current within ~5 minutes.
//
// Replaces the hourly cron's primary job; cron stays as a backstop.
//
// Kill switch: `daily-rollup-trigger.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import {
  PM_OS_REPO,
  isHeadless,
  isPluginEnabled,
  logEvent,
  runDebouncedDetached,
} from './pm-os/lib'

const PLUGIN = 'daily-rollup-trigger'
const DEBOUNCE_SECONDS = 5 * 60

const WRITE_TOOL_PATTERNS = [
  /^mcp__slack__chat_postMessage$/,
  /^mcp__slack__send_dm$/,
  /^mcp__gmail__send_message$/,
  /^mcp__linear__linear_(create|update|comment|assign|transition)/,
  /^mcp__atlassian__addCommentToJiraIssue$/,
  /^mcp__atlassian__createJiraIssue$/,
  /^mcp__atlassian__editJiraIssue$/,
  /^mcp__airtable__/,
]

function isOutboundWrite(event: { tool?: string; input?: Record<string, unknown> }): boolean {
  const tool = String(event.tool ?? '')
  if (WRITE_TOOL_PATTERNS.some(p => p.test(tool))) return true
  if (!/(?:^|__)shell_command$/.test(tool)) return false
  const command = String(event.input?.command ?? event.input?.cmd ?? '')
  return (
    /pmos slack\b[\s\S]*\b(send|post|reply)\b/i.test(command)
    || /pmos mail\b[\s\S]*\bsend\b/i.test(command)
    || /pmos (linear|jira)\b[\s\S]*\b(create|update|edit|comment|assign|transition)\b/i.test(command)
    || /pmos airtable\b[\s\S]*\b(create|update)\b/i.test(command)
    || /pmos (drive|calendar)\b[\s\S]*\b(create|update|write|insert|append|put|comment-add|comment-reply)\b/i.test(command)
  )
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  async function fireRollup(action: string): Promise<boolean> {
    const spawned = await runDebouncedDetached(
      PLUGIN, 'global', DEBOUNCE_SECONDS,
      'python3', [`${PM_OS_REPO}/system/scripts/build-daily-activity-rollup.py`],
      { label: PLUGIN },
    )
    if (!spawned) return false
    await logEvent(PLUGIN, { action })
    return true
  }

  amp.on('tool.result', async (event, _ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return
    if (isHeadless()) return
    const toolEvent = event as { tool?: string; input?: Record<string, unknown> }
    if (!isOutboundWrite(toolEvent)) return

    await fireRollup('queued')
  })
}
