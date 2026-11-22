import type { PluginAPI } from '@ampcode/plugin'
import { readFile } from 'node:fs/promises'
import { PM_OS_REPO, isPluginEnabled, run, runDetached } from './lib'

export default function (amp: PluginAPI) {
  const workspace = process.env.CMUX_WORKSPACE_ID
  const surface = process.env.CMUX_SURFACE_ID
  if (!workspace || !surface || amp.system.executor.kind !== 'local') return
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(value => { config = value })
  let generation = 0
  let since = Date.now() / 1000
  let timer: ReturnType<typeof setTimeout> | undefined
  amp.on('agent.start', () => { generation++; since = Date.now() / 1000; clearTimeout(timer) })
  amp.onDispose(() => { generation++; clearTimeout(timer) })
  amp.on('agent.end', event => {
    if (event.status !== 'done' || !isPluginEnabled(config, 'ephemeral-close')) return
    const turn = ++generation
    const started = since
    clearTimeout(timer)
    timer = setTimeout(async () => {
      try {
        if (turn !== generation || amp.activeThread.current?.id !== event.thread.id) return
        if (await amp.threads.get(event.thread.id).state.get() !== 'idle') return
        const tasks = JSON.parse(await readFile(`${PM_OS_REPO}/system/state/running-tasks.json`, 'utf8')).tasks
        const worker = process.env.PM_OS_WORKER_ID ?? ''
        if (!worker && !tasks.some((task: any) => task.router_thread_id === event.thread.id)) return
        const cmuxBin = process.env.CMUX_BIN ?? process.env.CMUX_AMP_CMUX_BIN ?? '/Applications/cmux.app/Contents/Resources/bin/cmux'
        const identified = await run(cmuxBin, ['identify'], { timeoutMs: 3000 })
        if (identified.code !== 0 || turn !== generation) return
        const ref = JSON.parse(identified.stdout).caller?.workspace_ref
        if (!ref) return
        await runDetached('python3', [
          `${PM_OS_REPO}/system/amp-plugins/pm-os/ephemeral-close.py`,
          '--workspace', ref, '--workspace-id', workspace, '--surface-id', surface,
          '--thread', event.thread.id, '--turn', String(event.id), '--since', String(started), '--worker', worker,
        ], { label: 'ephemeral-close' })
      } catch (error) { amp.logger.log(`[ephemeral-close] deferred: ${String(error)}`) }
    }, 10_000)
    timer.unref()
  })
}
