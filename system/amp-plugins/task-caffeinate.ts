// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// Keep macOS awake only while Amp is actively processing a turn. The
// caffeinate process is stopped on agent.end and also watches this plugin
// runtime's PID so a crashed or terminated runtime cannot leave it orphaned.
//
// Kill switch: `task-caffeinate.disabled = true`

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import type { PluginAPI } from '@ampcode/plugin'

const PLUGIN = 'task-caffeinate'
// macOS only: when the binary is absent (Linux, WSL, Windows) the plugin stays idle.
const CAFFEINATE = process.env.TASK_CAFFEINATE_BIN ?? '/usr/bin/caffeinate'

function isEnabled(config: Record<string, unknown>): boolean {
  return config[`${PLUGIN}.disabled`] !== true
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  let inhibitor: ChildProcess | null = null

  amp.configuration.subscribe(value => {
    config = value
  })

  const stop = (ctx?: { logger?: { log(message: string): void } }) => {
    const child = inhibitor
    inhibitor = null
    if (!child || child.exitCode !== null || child.killed) return

    try {
      child.kill('SIGTERM')
      ctx?.logger?.log(`[${PLUGIN}] stopped sleep inhibitor`)
    } catch (error) {
      ctx?.logger?.log(`[${PLUGIN}] failed to stop sleep inhibitor: ${String(error)}`)
    }
  }

  amp.on('agent.start', async (_event, ctx) => {
    if (!isEnabled(config) || inhibitor) return
    if (!existsSync(CAFFEINATE)) return

    try {
      const child = spawn(CAFFEINATE, ['-i', '-w', String(process.pid)], {
        stdio: 'ignore',
      })
      inhibitor = child
      child.unref()
      child.once('error', error => {
        if (inhibitor === child) inhibitor = null
        ctx.logger.log(`[${PLUGIN}] sleep inhibitor failed: ${String(error)}`)
      })
      child.once('exit', () => {
        if (inhibitor === child) inhibitor = null
      })
      ctx.logger.log(`[${PLUGIN}] started sleep inhibitor for active turn`)
    } catch (error) {
      inhibitor = null
      ctx.logger.log(`[${PLUGIN}] failed to start sleep inhibitor: ${String(error)}`)
    }
  })

  amp.on('agent.end', async (_event, ctx) => {
    stop(ctx)
  })

  process.once('exit', () => stop())
}
