import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { agentRegistryPath, autoPilotSkillInstalled, autoPilotSkillPaths, routeForMessage } from '../auto-pilot-router'

// system/config/switchboard-agent-registry.json is optional and not in the repo, so
// the tests carry their own registry.
const registry = {
  format: 'switchboard-agent-registry',
  version: 1,
  agents: [
    { id: 'switchboard', display_name: 'switchboard', aliases: ['switchboard architecture'] },
    { id: 'garden-revamp', display_name: 'Garden Revamp', aliases: ['Planter Revamp'] },
  ],
}

// Opening of the start-day seed preamble (system/cmux-helpers.sh,
// workstream_launch_preamble) as sent to seeded tabs.
// "every switchboard thread" and "the switchboard Obsidian vault" normalize to "switchboard",
// which matched the `switchboard` registry entry regardless of the tab's workstream.
const SEED_PROMPT = [
  'IMPORTANT: Skip all AGENTS.md session-start steps (todo, start-of-day, daemon, skill loading).',
  'You are a focused workspace launched by start-day.sh. Go straight to the task below.',
  'Agent-mode routing: ultra is the default for every switchboard thread.',
  'Review-surface contract: any deliverable the owner must review is a Markdown note inside the switchboard Obsidian vault (the repo root).',
  'Start-of-day seed for Recipe Search on 2026-10-15. This turn is the seed note only.',
].join(' ')

const WORKER_PROMPT = [
  'You are switchboard worker worker-442f7a0a7926 for task rt-000000000005 (attempt rt-000000000005.a1.5186ef), workstream launch-notes, origin cos.',
  'Your checkout is the git worktree C:/Users/tester/Development/switchboard/.amp/worktrees/rt-000000000005.',
  'Router return contract: task rt-000000000005.',
].join(' ')

test('start-day seed and dispatch worker prompts are not routed through the switchboard registry entry', () => {
  expect(routeForMessage(SEED_PROMPT, registry, true)).toEqual({ kind: 'skip', reason: 'machine-prompt' })
  expect(routeForMessage(WORKER_PROMPT, registry, true)).toEqual({ kind: 'skip', reason: 'machine-prompt' })
  expect(routeForMessage('IMPORTANT: Skip all AGENTS.md session-start steps. You are the quiet Launchpad dispatch surface.', registry, true))
    .toEqual({ kind: 'skip', reason: 'machine-prompt' })
})

test('a human first turn that names a registry agent still routes to it', () => {
  const route = routeForMessage('Walk me through the switchboard architecture for the daemon.', registry, true)
  expect(route.kind).toBe('registry')
  if (route.kind === 'registry') expect(route.agent.id).toBe('switchboard')

  const billing = routeForMessage('What is the Planter Revamp plan for new users?', registry, true)
  expect(billing.kind).toBe('registry')
  if (billing.kind === 'registry') expect(billing.agent.id).toBe('garden-revamp')
})

test('with the auto-pilot skill installed, an unmatched human turn gets the reminder; a named core skill skips it', () => {
  expect(routeForMessage('Summarize this PR for me.', registry, true)).toEqual({ kind: 'inject', withRegistry: true })
  expect(routeForMessage('Summarize this PR for me.', undefined, true)).toEqual({ kind: 'inject', withRegistry: false })
  expect(routeForMessage('Check my gcal for conflicts tomorrow.', registry, true)).toEqual({ kind: 'skip', reason: 'skill-named' })
})

test('a personal install with neither the skill nor the registry adds nothing', () => {
  expect(routeForMessage('Summarize this PR for me.', undefined, false)).toEqual({ kind: 'skip', reason: 'targets-missing' })
  expect(routeForMessage('What is on my plate this afternoon?', undefined, false)).toEqual({ kind: 'skip', reason: 'targets-missing' })
  expect(routeForMessage('start of day', undefined, false)).toEqual({ kind: 'skip', reason: 'targets-missing' })
})

test('a registry without the skill still routes a match but never names the missing skill', () => {
  const route = routeForMessage('Walk me through the switchboard architecture.', registry, false)
  expect(route.kind).toBe('registry')
  expect(routeForMessage('Summarize this PR for me.', registry, false)).toEqual({ kind: 'skip', reason: 'targets-missing' })
})

test('core instruction files never tell an agent to load auto-pilot (it exists only with an optional profile)', () => {
  const repo = join(import.meta.dir, '..', '..', '..')
  const surfaces = ['AGENTS.md', '.agents/skills/chief-of-staff/SKILL.md', 'system/scheduled-jobs.yaml']
  for (const rel of surfaces) {
    const text = readFileSync(join(repo, rel), 'utf8')
    expect({ rel, hit: /\b(load|use)\s+auto-pilot\b|auto-pilot[^\n]*routing defaults/i.test(text) }).toEqual({ rel, hit: false })
  }
})

test('the skill counts as installed in any folder Amp or Claude Code loads it from', () => {
  const base = mkdtempSync(join(tmpdir(), 'auto-pilot-router-'))
  try {
    const home = join(base, 'home')
    const root = join(base, 'repo')
    expect(autoPilotSkillInstalled(home, root)).toBe(false)
    expect(agentRegistryPath(root)).toBe(join(root, 'system', 'config', 'switchboard-agent-registry.json'))
    for (const path of autoPilotSkillPaths(home, root)) {
      mkdirSync(join(path, '..'), { recursive: true })
      writeFileSync(path, '---\nname: auto-pilot\n---\n')
      expect(autoPilotSkillInstalled(home, root)).toBe(true)
      rmSync(path)
      expect(autoPilotSkillInstalled(home, root)).toBe(false)
    }
    // A folder with no SKILL.md (a deleted skill's leftovers) is not the skill.
    mkdirSync(join(home, '.claude', 'skills', 'auto-pilot', '__pycache__'), { recursive: true })
    expect(autoPilotSkillInstalled(home, root)).toBe(false)
  } finally {
    rmSync(base, { recursive: true, force: true })
  }
})
