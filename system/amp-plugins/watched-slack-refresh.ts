// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// watched-slack-refresh — when a workstream workspace opens, refresh that
// workstream's watched_slack[] snapshots so the prompt reads current Slack
// conversations. Parallel to watched-docs-refresh, but for Slack channels and
// threads instead of Google Docs.
//
// Cron stays as a backstop at lower cadence (see system/background-tasks.yaml).
//
// Kill switch: `watched-slack-refresh.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import {
  SWITCHBOARD_REPO,
  activeWorkspaceName,
  isHeadless,
  isPluginEnabled,
  logEvent,
  runDebouncedDetached,
  workstreamSlugFromContext,
} from './switchboard/lib'

const PLUGIN = 'watched-slack-refresh'
const PER_WORKSTREAM_DEBOUNCE = 15 * 60

export default function (amp: PluginAPI) {
  // The canonical owner owns snapshot writes; supplemental startup is read-only.
  if (process.env.SWITCHBOARD_WORKER_ID) return

  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.on('session.start', async (_event, _ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return
    if (isHeadless()) return

    const ws = await activeWorkspaceName()
    const slug = workstreamSlugFromContext(ws) ?? workstreamSlugFromContext(process.cwd())
    if (!slug) return

    const spawned = await runDebouncedDetached(
      PLUGIN, slug, PER_WORKSTREAM_DEBOUNCE,
      'python3', [`${SWITCHBOARD_REPO}/system/scripts/refresh-watched-slack.py`, '--workstream', slug],
      { label: `${PLUGIN}:${slug}` },
    )
    if (!spawned) return
    await logEvent(PLUGIN, { action: 'queued-refresh', slug })
  })
}
