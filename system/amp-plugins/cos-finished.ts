// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// cos-finished — when the Chief of Staff workspace finishes a successful turn,
// re-run schedule-meeting-jobs.sh so calendar updates the owner made post-startup
// get pre-brief/post-ingest jobs scheduled.
//
// The scheduler also runs every 15 min via daemon/pm-sync.sh; this plugin is
// the low-latency path so meetings added mid-conversation get jobs immediately
// instead of waiting for the next pm-sync tick.
//
// Skip conditions:
//  - Not a CoS workspace
//  - Headless mode
//  - Status is error/cancelled
//  - Re-ran in the last 90 minutes
//
// Kill switch: `cos-finished.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import {
  SWITCHBOARD_REPO,
  activeWorkspaceName,
  isHeadless,
  isPluginEnabled,
  logEvent,
  runDebouncedDetached,
} from './switchboard/lib'

const PLUGIN = 'cos-finished'
const DEBOUNCE_SECONDS = 90 * 60

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.on('agent.end', async (event, _ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return
    if (isHeadless()) return
    if (event.status !== 'done') return

    const ws = await activeWorkspaceName()
    if (!ws || !/cos|chief.*staff/i.test(ws)) {
      await logEvent(PLUGIN, { action: 'skip-workspace', workspace: ws })
      return
    }

    const spawned = await runDebouncedDetached(
      PLUGIN, 'global', DEBOUNCE_SECONDS,
      'bash', [`${SWITCHBOARD_REPO}/daemon/schedule-meeting-jobs.sh`],
      { label: PLUGIN },
    )
    if (!spawned) return
    await logEvent(PLUGIN, { action: 'queued', workspace: ws })
  })
}
