// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// watched-docs-refresh — when a workstream workspace opens, refresh that
// workstream's watched_docs[] snapshots so the prompt reads current data.
//
// Replaces hourly cron at the per-workstream level. Cron stays as a backstop
// at lower cadence (set in system/background-tasks.yaml).
//
// Kill switch: `watched-docs-refresh.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import {
  PM_OS_REPO,
  activeWorkspaceName,
  isHeadless,
  isPluginEnabled,
  logEvent,
  runDebouncedDetached,
  workstreamSlugFromContext,
} from './pm-os/lib'

const PLUGIN = 'watched-docs-refresh'
const PER_WORKSTREAM_DEBOUNCE = 15 * 60

export default function (amp: PluginAPI) {
  // The canonical owner owns snapshot writes; supplemental startup is read-only.
  if (process.env.PM_OS_WORKER_ID) return

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
      'python3', [`${PM_OS_REPO}/system/scripts/refresh-watched-docs.py`, '--workstream', slug],
      { label: `${PLUGIN}:${slug}` },
    )
    if (!spawned) return
    await logEvent(PLUGIN, { action: 'queued-refresh', slug })
  })
}
