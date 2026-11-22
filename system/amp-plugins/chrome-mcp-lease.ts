// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
import { existsSync } from 'node:fs'
import { hostname } from 'node:os'
import type { PluginAPI } from '@ampcode/plugin'
import { PM_OS_REPO, run } from './pm-os/lib'

export const description = 'Release the browser lease this session holds after each turn.'

/** Same name owner_browser.py writes, so a machine with no lease spawns nothing at each Stop. */
function ownerLeaseFile(): string {
  const machine = (process.env.PM_OS_MACHINE || hostname()).split('.')[0].toUpperCase()
  return `${process.env.PM_OS_OWNER_BROWSER_STATE || `${PM_OS_REPO}/.state`}/owner-browser-lease-${machine}.json`
}

export default function (amp: PluginAPI) {
  amp.on('agent.end', async (event, ctx) => {
    // The owner's browser: one driver at a time, freed when the holder's turn ends.
    // The proxy also frees it on exit and after three idle minutes, so a miss here only delays.
    const session = (event as { thread?: { id?: string } }).thread?.id
    if (session && existsSync(ownerLeaseFile())) {
      const owner = await run('python3', [
        `${PM_OS_REPO}/system/scripts/owner_browser.py`, 'release', '--session', session,
      ], { timeoutMs: 10000 })
      if (owner.code !== 0) ctx.logger.log(`[chrome-mcp-lease] owner browser release failed: ${owner.stderr.trim().split('\n').pop() || 'no reason reported'}`)
    }
    // The headless supervisor is an internal extension the kit does not ship.
    const helper = `${PM_OS_REPO}/system/scripts/chrome-mcp-session.py`
    if (!existsSync(helper)) return
    // The helper resolves its real Amp ancestor. Never accept a thread-provided PID.
    // It writes a release nonce, then waits until the owning proxy has closed Chrome
    // and dropped the lock; exit 0 means verified released, not merely requested.
    const result = await run('python3', [helper, '--release', '--wait', '12'], { timeoutMs: 15000 })
    if (result.code !== 0) {
      const reason = result.stderr.trim().split('\n').pop() || 'no reason reported'
      ctx.logger.log(`[chrome-mcp-lease] release not confirmed; lease may remain held: ${reason}`)
    }
  })
}
