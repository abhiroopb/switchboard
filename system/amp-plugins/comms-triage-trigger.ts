// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// comms-triage-trigger — fires the comms triage script when the CoS thread's
// agent.end completes successfully. Replaces (or supplements) the hourly cron
// in system/background-tasks.yaml.
//
// Passes --background: the script verifies that the persistent Comms Triage
// workspace exists, but never changes workspace selection or app focus.
//
// The script runs synchronously so its exit code can gate the debounce:
// 0 = workspace available; anything else = error and retry on the next turn.
//
// Skip conditions:
//  - Not a CoS workspace (best-effort detection via cmux identify)
//  - Headless mode
//  - Triage availability checked in the last 30 minutes
//  - Status is error/cancelled
//
// Kill switch: `comms-triage-trigger.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import {
  PM_OS_REPO,
  activeWorkspaceName,
  isHeadless,
  isPluginEnabled,
  logEvent,
  readState,
  run,
  withinSeconds,
  writeState,
} from './pm-os/lib'

const PLUGIN = 'comms-triage-trigger'
const DEBOUNCE_SECONDS = 30 * 60

interface TriageState {
  lastFiredAt: string
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.on('agent.end', async (event, _ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return
    if (isHeadless()) return
    if (event.status !== 'done') return

    const ws = await activeWorkspaceName()
    if (!ws || !/\bcos\b|chief.*staff/i.test(ws)) {
      await logEvent(PLUGIN, { action: 'skip-workspace', workspace: ws })
      return
    }

    const state = await readState<TriageState>(PLUGIN, 'global')
    if (state && withinSeconds(state.lastFiredAt, DEBOUNCE_SECONDS)) {
      await logEvent(PLUGIN, { action: 'skip-debounce', workspace: ws })
      return
    }

    // Synchronous on purpose: only a successful availability check burns the
    // debounce. This callback never focuses or selects a workspace.
    const result = await run('bash', [`${PM_OS_REPO}/system/scripts/open-comms-triage.sh`, '--background'], { timeoutMs: 15_000 })
    if (result.code === 0) {
      await writeState<TriageState>(PLUGIN, 'global', { lastFiredAt: new Date().toISOString() })
      await logEvent(PLUGIN, { action: 'fire', workspace: ws })
      return
    }
    await logEvent(PLUGIN, { action: 'error', workspace: ws, code: result.code, stderr: result.stderr.slice(0, 300) })
  })
}
