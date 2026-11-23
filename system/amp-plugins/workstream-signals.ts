// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// workstream-signals — when a workstream workspace opens, build that
// workstream's .signals/recent.md so the prompt reads fresh signals.
//
// Replaces the build-workspace-signals-all cron at per-workstream level.
//
// Kill switch: `workstream-signals.disabled = true`

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

const PLUGIN = 'workstream-signals'
const PER_WORKSTREAM_DEBOUNCE = 15 * 60

export default function (amp: PluginAPI) {
  // The canonical owner owns signal writes; supplemental startup is read-only.
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
      'python3', [`${SWITCHBOARD_REPO}/system/scripts/build-workspace-signals.py`, slug],
      { label: `${PLUGIN}:${slug}` },
    )
    if (!spawned) return
    await logEvent(PLUGIN, { action: 'queued-build-signals', slug })
  })
}
