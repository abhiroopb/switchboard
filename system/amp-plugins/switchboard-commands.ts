// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// switchboard-commands — registers switchboard workflows in Amp's command palette so they
// can be invoked via Ctrl-o instead of typed shell wrappers.
//
// Commands registered:
//   switchboard: Start day                  → system/start-day.sh
//   switchboard: Triage comms now           → system/scripts/open-comms-triage.sh
//   switchboard: Distill memory             → ~/bin/amp-mem distill
//   switchboard: Generate recall pack       → prompt for task, run generate-recall-pack.py
//   switchboard: Open workstream…           → ui.select over workstreams/*/config.yaml
//   switchboard: Refresh watched docs       → refresh-watched-docs.py --force
//   switchboard: Search memory              → prompt, run amp-mem search
//
// Kill switch: `switchboard-commands.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import { readdirSync, statSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { SWITCHBOARD_REPO, run, runDetached, isPluginEnabled, logEvent, HOME } from './switchboard/lib'

const PLUGIN = 'switchboard-commands'
const SCRIPTS = `${SWITCHBOARD_REPO}/system/scripts`
const AMP_MEM = `${HOME}/bin/amp-mem`

function listWorkstreams(): string[] {
  const root = `${SWITCHBOARD_REPO}/workstreams`
  if (!existsSync(root)) return []
  return readdirSync(root)
    .filter(n => !n.startsWith('_') && !n.startsWith('.'))
    .filter(n => {
      try {
        return statSync(join(root, n)).isDirectory() && existsSync(join(root, n, 'config.yaml'))
      } catch {
        return false
      }
    })
    .sort()
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.registerCommand(
    'switchboard-start-day',
    { title: 'Start day', category: 'switchboard', description: 'Run start-of-day chief-of-staff workflow' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      await logEvent(PLUGIN, { command: 'start-day' })
      await runDetached('bash', [`${SWITCHBOARD_REPO}/system/start-day.sh`])
      await ctx.ui.notify('Start-day workflow launched')
    },
  )

  amp.registerCommand(
    'switchboard-triage-comms',
    { title: 'Triage comms now', category: 'switchboard', description: 'Open the comms triage workspace' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      await logEvent(PLUGIN, { command: 'triage-comms' })
      await runDetached('bash', [`${SCRIPTS}/open-comms-triage.sh`])
      await ctx.ui.notify('Comms triage launched')
    },
  )

  amp.registerCommand(
    'switchboard-distill-memory',
    { title: 'Distill memory', category: 'switchboard', description: 'Compact and distill amp-mem observations' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      await logEvent(PLUGIN, { command: 'distill-memory' })
      const r = await run(AMP_MEM, ['distill'], { timeoutMs: 120_000 })
      await ctx.ui.notify(r.code === 0 ? 'Memory distilled' : `Distill failed: ${r.stderr.slice(0, 200)}`)
    },
  )

  amp.registerCommand(
    'switchboard-recall-pack',
    { title: 'Generate recall pack', category: 'switchboard', description: 'Generate a bounded recall pack for a task' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      const task = await ctx.ui.input({
        title: 'Recall pack task',
        helpText: 'What is the recall pack for? (e.g. "profiles bookings status update")',
      })
      if (!task) return
      await logEvent(PLUGIN, { command: 'recall-pack', task })
      const r = await run('python3', [`${SCRIPTS}/generate-recall-pack.py`, '--task', task], { timeoutMs: 120_000 })
      const head = r.stdout.split('\n').slice(0, 20).join('\n')
      await ctx.ui.notify(r.code === 0 ? `Recall pack ready:\n${head}` : `Failed: ${r.stderr.slice(0, 200)}`)
    },
  )

  amp.registerCommand(
    'switchboard-open-workstream',
    { title: 'Open workstream…', category: 'switchboard', description: 'Pick a workstream and launch its workspace' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      const opts = listWorkstreams()
      if (opts.length === 0) {
        await ctx.ui.notify('No workstreams found')
        return
      }
      const slug = await ctx.ui.select({ title: 'Open workstream', options: opts })
      if (!slug) return
      await logEvent(PLUGIN, { command: 'open-workstream', slug })
      const quotedRepo = JSON.stringify(SWITCHBOARD_REPO)
      const quotedSlug = JSON.stringify(slug)
      const command = [
        `cd ${quotedRepo}`,
        `bash system/cmux-helpers.sh launch workstream:${quotedSlug}`,
        'source system/cmux-helpers.sh',
        `ws_ref=$(workspace_ref_if_exists ${quotedSlug})`,
        'SWITCHBOARD_EXPLICIT_USER_FOCUS=1 cmux_cmd select-workspace --workspace "$ws_ref"',
      ].join(' && ')
      await runDetached('bash', ['-c', command])
      await ctx.ui.notify(`Launching workstream: ${slug}`)
    },
  )

  amp.registerCommand(
    'switchboard-refresh-docs',
    { title: 'Refresh watched docs', category: 'switchboard', description: 'Force-refresh all watched Google Doc snapshots' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      await logEvent(PLUGIN, { command: 'refresh-docs' })
      await runDetached('python3', [`${SCRIPTS}/refresh-watched-docs.py`, '--force'])
      await ctx.ui.notify('Watched-docs refresh started in background')
    },
  )


  amp.registerCommand(
    'switchboard-end-day',
    { title: 'End day (close all + quit cmux)', category: 'switchboard', description: 'Wrap up + close every workstream tab and CoS, then quit cmux' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      await logEvent(PLUGIN, { command: 'end-day' })
      const result = await run('bash', [`${SCRIPTS}/command-center-action.sh`, 'close-all', '--request'])
      await ctx.ui.notify(result.code === 0
        ? 'Closeout requested in CoS. CoS saves context before shutdown.'
        : `Nothing closed: ${result.stderr.slice(0, 200)}`)
    },
  )

  amp.registerCommand(
    'switchboard-memory-search',
    { title: 'Search memory', category: 'switchboard', description: 'Search amp-mem observations' },
    async (ctx) => {
      if (!isPluginEnabled(config, PLUGIN)) return
      const query = await ctx.ui.input({ title: 'Memory search', helpText: 'FTS5 query, e.g. "mobile onboarding"' })
      if (!query) return
      await logEvent(PLUGIN, { command: 'memory-search', query })
      const r = await run(AMP_MEM, ['search', query], { timeoutMs: 30_000 })
      await ctx.ui.notify(r.stdout.slice(0, 800) || '(no results)')
    },
  )
}
