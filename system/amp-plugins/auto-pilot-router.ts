// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// auto-pilot-router — ensures auto-pilot is loaded for the first user turn
// of every interactive session, per ~/AGENTS.md "Routing Defaults":
//   "Start with auto-pilot before loading a new skill unless the relevant
//    core skill is already active in the current session."
//
// Hooks: agent.start (injects routing reminder into the first user turn)
//
// Skip conditions:
//  - headless mode (amp -x): no need, the headless prompt is explicit
//  - machine-written first turn (start-day seed, dispatch worker launch,
//    automation, CoS handoff): it carries its own instructions, and its
//    boilerplate ("every switchboard thread", "the switchboard Obsidian vault") would
//    match the `switchboard` registry entry in every seeded tab
//  - user message already references a core skill bypass (takes precedence
//    over the agent registry, because the named skill is already the route)
//  - already injected this thread (deduped via per-thread state)
//
//  - its targets are missing: the auto-pilot skill and
//    system/config/switchboard-agent-registry.json are optional and not in the kit
//    (an organization profile may install them). The reminder needs the skill,
//    a registry route needs the registry, and with neither the hook adds
//    nothing, so no session gets an instruction it cannot follow.
//
// Kill switch: `auto-pilot-router.disabled = true`

import type { PluginAPI } from '@ampcode/plugin'
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { isHeadless, isMachinePrompt, isPluginEnabled, logEvent, readState, writeState } from './switchboard/lib'

const PLUGIN = 'auto-pilot-router'

function switchboardRoot(): string {
  return process.env.SWITCHBOARD_ROOT || process.env.SWITCHBOARD_REPO || join(homedir(), 'Development', 'switchboard')
}

function homeDir(): string {
  return process.env.HOME || process.env.USERPROFILE || homedir()
}

export function agentRegistryPath(root: string = switchboardRoot()): string {
  return join(root, 'system', 'config', 'switchboard-agent-registry.json')
}

/** Every place Amp or Claude Code loads the auto-pilot skill from. */
export function autoPilotSkillPaths(home: string = homeDir(), root: string = switchboardRoot()): string[] {
  return [
    join(home, '.agents', 'skills', 'auto-pilot', 'SKILL.md'),
    join(home, '.claude', 'skills', 'auto-pilot', 'SKILL.md'),
    join(home, '.config', 'amp', 'skills', 'auto-pilot', 'SKILL.md'),
    join(root, '.agents', 'skills', 'auto-pilot', 'SKILL.md'),
  ]
}

export function autoPilotSkillInstalled(home?: string, root?: string): boolean {
  return autoPilotSkillPaths(home, root).some(p => existsSync(p))
}

export const REMINDER_WITH_REGISTRY = '\n\n[auto-pilot-router] Load the auto-pilot skill before loading any other skill. For switchboard domain context, read `system/config/switchboard-agent-registry.json` fresh, use the closest matching entry, and apply its live shared contract and write-back policy.'
export const REMINDER = '\n\n[auto-pilot-router] Load the auto-pilot skill before loading any other skill.'

const CORE_SKILL_BYPASS = [
  'todo', 'chief-of-staff', 'cos', 'slack', 'gmail', 'gcal', 'gdrive',
  'memory', 'amp-mem', 'kb-distill', 'skill-management', 'auto-pilot',
]

const SKILL_HINT_PATTERNS = CORE_SKILL_BYPASS.map(s => new RegExp(`\\b${s}\\b`, 'i'))

interface RegistryAgent {
  id: string
  display_name: string
  aliases: string[]
}

interface AgentRegistry {
  format: string
  version: number
  agents: RegistryAgent[]
}

let registryErrorLogged = false

function normalizedWords(value: string): string {
  return value.toLocaleLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
}

function includesPhrase(message: string, phrase: string): boolean {
  const normalizedPhrase = normalizedWords(phrase)
  if (!normalizedPhrase) return false
  return ` ${normalizedWords(message)} `.includes(` ${normalizedPhrase} `)
}

export function registryAgentForMessage(message: string, registry: AgentRegistry): RegistryAgent | undefined {
  if (registry.format !== 'switchboard-agent-registry' || registry.version !== 1 || !Array.isArray(registry.agents)) {
    throw new Error('unsupported registry shape')
  }
  return registry.agents.find(agent =>
    [agent.id, agent.display_name, ...(agent.aliases ?? [])].some(term => includesPhrase(message, term)),
  )
}

async function loadRegistry(): Promise<AgentRegistry | undefined> {
  const path = agentRegistryPath()
  // A missing registry is the normal state on a personal install, not an error.
  if (!existsSync(path)) return undefined
  try {
    return JSON.parse(await readFile(path, 'utf8')) as AgentRegistry
  } catch (error) {
    await logRegistryError(error)
    return undefined
  }
}

async function logRegistryError(error: unknown): Promise<void> {
  if (registryErrorLogged) return
  registryErrorLogged = true
  await logEvent(PLUGIN, {
    action: 'registry_error',
    registry: agentRegistryPath(),
    error: error instanceof Error ? error.message : String(error),
  })
}

export type Route =
  | { kind: 'skip'; reason: 'machine-prompt' | 'skill-named' | 'targets-missing' }
  | { kind: 'registry'; agent: RegistryAgent }
  | { kind: 'inject'; withRegistry: boolean }

/**
 * Pure routing decision for a thread's first user turn. `autoPilotSkill` says
 * whether the auto-pilot skill is installed; without it there is nothing to
 * remind about, so only a registry match (which needs the registry) adds text.
 */
export function routeForMessage(message: string, registry: AgentRegistry | undefined, autoPilotSkill: boolean): Route {
  if (isMachinePrompt(message)) return { kind: 'skip', reason: 'machine-prompt' }
  if (SKILL_HINT_PATTERNS.some(p => p.test(message))) return { kind: 'skip', reason: 'skill-named' }
  if (registry) {
    const agent = registryAgentForMessage(message, registry)
    if (agent) return { kind: 'registry', agent }
  }
  if (!autoPilotSkill) return { kind: 'skip', reason: 'targets-missing' }
  return { kind: 'inject', withRegistry: Boolean(registry) }
}

interface ThreadState {
  injected: boolean
  injectedAt?: string
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  amp.on('agent.start', async (event, ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return
    if (isHeadless()) return

    const threadId = event.thread?.id ?? ctx.thread?.id
    if (!threadId) return

    // Already injected this thread? Skip.
    const prior = await readState<ThreadState>(PLUGIN, String(threadId))
    if (prior?.injected) return

    const msg = String(event.message ?? '')
    const skill = autoPilotSkillInstalled()
    let registry: AgentRegistry | undefined
    let route: Route
    try {
      registry = await loadRegistry()
      route = routeForMessage(msg, registry, skill)
    } catch (error) {
      // Unsupported registry shape: fall back to the plain reminder.
      await logRegistryError(error)
      route = routeForMessage(msg, undefined, skill)
    }

    await writeState<ThreadState>(PLUGIN, String(threadId), { injected: true, injectedAt: new Date().toISOString() })

    if (route.kind === 'skip') {
      if (route.reason === 'machine-prompt') await logEvent(PLUGIN, { action: 'skip_machine_prompt', thread_id: threadId })
      if (route.reason === 'targets-missing') await logEvent(PLUGIN, { action: 'skip_targets_missing', thread_id: threadId })
      return
    }

    if (route.kind === 'registry') {
      await logEvent(PLUGIN, { action: 'registry_route', thread_id: threadId, agent_id: route.agent.id })
      return {
        message: {
          content: `\n\n[auto-pilot-router] Route this turn through switchboard agent registry entry \`${route.agent.id}\` in \`system/config/switchboard-agent-registry.json\`. Read it fresh and apply the live shared contract, mission, sources, workflows, rules, and write-back policy before acting. Load the referenced skill instructions before using them.`,
          display: true as const,
        },
      }
    }

    await logEvent(PLUGIN, { action: 'inject', thread_id: threadId })

    return {
      message: {
        content: route.withRegistry ? REMINDER_WITH_REGISTRY : REMINDER,
        display: true as const,
      },
    }
  })
}
