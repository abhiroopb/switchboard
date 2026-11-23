// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// workstream-lifecycle — prevents a completed Amp turn from leaving a stale
// "active" workstream status behind.
//
// On agent.end, compare-and-set any current active update to "to review".
// Active is reserved for a live agent turn; once the turn settles, its result
// is ready for the user to inspect. Explicit-launch and P0 keep-open rules
// remain owned by emit-workspace-update.py.
//
// Kill switch: `workstream-lifecycle.disabled = true`

import { readFile } from 'node:fs/promises'
import type { PluginAPI } from '@ampcode/plugin'
import {
  SWITCHBOARD_REPO,
  isHeadless,
  isPluginEnabled,
  logEvent,
  runDetached,
  workstreamSlugFromContext,
} from './switchboard/lib'

const PLUGIN = 'workstream-lifecycle'
const UPDATES_PATH = `${SWITCHBOARD_REPO}/system/state/sources/workspace-updates.json`

interface WorkspaceUpdate {
  update_id?: string
  timestamp?: string
  status?: string
  summary?: string
  recommended_next_action?: string
}

interface WorkspaceUpdatesPayload {
  updates?: Record<string, WorkspaceUpdate>
}

async function readUpdate(source: string): Promise<WorkspaceUpdate | null> {
  try {
    const payload = JSON.parse(await readFile(UPDATES_PATH, 'utf8')) as WorkspaceUpdatesPayload
    return payload.updates?.[source] ?? null
  } catch {
    return null
  }
}

export default function (amp: PluginAPI) {
  // Supplemental workers own task callbacks, not the owner's aggregate status.
  if (process.env.SWITCHBOARD_WORKER_ID) return

  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.on('agent.end', async (event, ctx) => {
    if (!isPluginEnabled(config, PLUGIN) || isHeadless()) return

    const slug = workstreamSlugFromContext(process.cwd())
    if (!slug) return
    const source = `workstream:${slug}`
    const current = await readUpdate(source)
    if (!current?.timestamp || String(current.status ?? '').toLowerCase() !== 'active') return

    const guardArgs = current.update_id
      ? ['--only-if-current-update-id', current.update_id]
      : ['--only-if-current-timestamp', current.timestamp]

    const spawned = await runDetached('python3', [
      `${SWITCHBOARD_REPO}/system/scripts/emit-workspace-update.py`,
      '--source', source,
      '--status', 'to review',
      '--summary', current.summary ?? 'Agent turn completed; the result is ready for review.',
      '--recommended-next-action', current.recommended_next_action ?? 'Review the completed work and decide the next step.',
      '--only-if-current-status', 'active',
      '--skip-auto-close',
      ...guardArgs,
    ], { cwd: SWITCHBOARD_REPO, label: `${PLUGIN}:${source}` })

    await logEvent(PLUGIN, {
      action: spawned ? 'reconcile-review' : 'spawn-failed',
      source,
      expectedUpdateId: current.update_id,
      expectedTimestamp: current.timestamp,
      turnStatus: event.status,
    })
    if (spawned) ctx.logger.log(`[${PLUGIN}] queued active-to-review reconciliation for ${source}`)
  })
}
