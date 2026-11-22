// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// goal-loop — Amp equivalent of Claude Code's /goal loop pattern
// (https://claude.com/blog/getting-started-with-loops).
//
// The agent (or the owner, via the palette command) sets a done-condition for the
// current thread. On every agent.end, the plugin asks the thread-scoped AI
// whether the goal is met based on the turn's messages:
//   - met            → clear the goal, notify
//   - not met        → return { action: 'continue' } to start another turn
//   - max turns hit  → stop the loop, notify
//   - error/cancel   → pause the loop (never auto-continue a failed turn)
//
// Tools (agent-callable, thread-scoped):
//   goal_set(goal, max_turns?)   goal_status()   goal_clear()
//
// Config keys (live-tunable via amp config):
//   goal-loop.disabled        kill switch (default false)
//   goal-loop.max_turns       default max loop turns (default 10)
//   goal-loop.met_threshold   min P(yes) to count the goal as met (default 0.8)

import type { PluginAPI, ThreadMessage } from '@ampcode/plugin'
import { isHeadless, isPluginEnabled, logEvent, readState, writeState } from './pm-os/lib'

const PLUGIN = 'goal-loop'
const DEFAULT_MAX_TURNS = 10
const DEFAULT_MET_THRESHOLD = 0.8
const HARD_MAX_TURNS = 50

export const description =
  'Goal-based agent loops: set a done-condition for a thread; after each turn an AI check either continues the loop or stops when the goal is met, cleared, or max turns are reached.'

export interface GoalState {
  goal: string
  maxTurns: number
  turnsUsed: number
  active: boolean
  setAt: string
  revision?: number
  stoppedReason?: string
  lastEvaluation?: string
}

type StateReader = (key: string) => Promise<GoalState | null>
type StateWriter = (key: string, state: GoalState) => Promise<void>

/**
 * Serializes this plugin instance's short read-modify-write transitions.
 * Evaluator I/O stays outside the queue, so goal_clear remains responsive.
 * The persisted revision fences evaluator results that became stale while awaiting AI.
 * This is intentionally not a cross-process lock; writeState remains last-rename-wins
 * if two Amp processes own the same thread state file.
 */
export class GoalStateCoordinator {
  private readonly tails = new Map<string, Promise<void>>()

  constructor(private readonly read: StateReader, private readonly write: StateWriter) {}

  private async serial<T>(key: string, operation: () => Promise<T>): Promise<T> {
    const previous = this.tails.get(key) ?? Promise.resolve()
    let release!: () => void
    const current = new Promise<void>(resolve => { release = resolve })
    const tail = previous.then(() => current)
    this.tails.set(key, tail)
    await previous
    try {
      return await operation()
    } finally {
      release()
      if (this.tails.get(key) === tail) this.tails.delete(key)
    }
  }

  snapshot(key: string): Promise<GoalState | null> {
    return this.serial(key, () => this.read(key))
  }

  clear(key: string): Promise<GoalState | null> {
    return this.serial(key, async () => {
      const current = await this.read(key)
      if (!current?.active) return null
      const state = { ...current, active: false, stoppedReason: 'cleared', revision: (current.revision ?? 0) + 1 }
      await this.write(key, state)
      return state
    })
  }

  replace(key: string, makeState: (current: GoalState | null) => GoalState): Promise<GoalState> {
    return this.serial(key, async () => {
      const current = await this.read(key)
      const state = { ...makeState(current), revision: (current?.revision ?? 0) + 1 }
      await this.write(key, state)
      return state
    })
  }

  commit(key: string, expectedRevision: number, update: (current: GoalState) => GoalState): Promise<GoalState | null> {
    return this.serial(key, async () => {
      const current = await this.read(key)
      if (!current || !current.active || (current.revision ?? 0) !== expectedRevision) return null
      const state = { ...update(current), revision: expectedRevision + 1 }
      await this.write(key, state)
      return state
    })
  }
}

function stateKey(threadId: string): string {
  return `thread-${threadId.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

function configNumber(config: Record<string, unknown>, key: string, fallback: number): number {
  const v = config[key]
  const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN
  return Number.isFinite(n) && n > 0 ? n : fallback
}

/** Extract the assistant-visible text of the turn, capped so the ai.ask prompt stays small. */
function turnText(messages: ThreadMessage[], maxChars = 6000): string {
  const parts: string[] = []
  for (const m of messages) {
    if (m.role !== 'assistant') continue
    for (const block of m.content) {
      if ('text' in block && typeof block.text === 'string') parts.push(block.text)
    }
  }
  const joined = parts.join('\n').trim()
  return joined.length > maxChars ? joined.slice(-maxChars) : joined
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })
  const goals = new GoalStateCoordinator(
    key => readState<GoalState>(PLUGIN, key),
    (key, state) => writeState<GoalState>(PLUGIN, key, state),
  )

  // ---- tools -------------------------------------------------------------

  amp.registerTool({
    name: 'goal_set',
    title: 'Set loop goal',
    description:
      'Start a goal loop for this thread. Store a done-condition; after each of your turns the plugin checks whether it is met and automatically continues the conversation until it is met, cleared, or max_turns is reached. Use when the user asks to "loop until", "keep going until", or "don\'t stop until" a condition holds. Write the goal as an objectively checkable condition.',
    inputSchema: {
      type: 'object',
      properties: {
        goal: { type: 'string', description: 'The done-condition, phrased so a reviewer can verify it from the conversation (e.g. "all tests in pkg/foo pass and the fix is committed").' },
        max_turns: { type: 'number', description: `Maximum automatic continuation turns before the loop stops (default ${DEFAULT_MAX_TURNS}, hard cap ${HARD_MAX_TURNS}).` },
      },
      required: ['goal'],
    },
    async execute(input, ctx) {
      const goal = String(input.goal ?? '').trim()
      if (!goal) return { content: [{ type: 'text', text: 'goal_set failed: goal is empty.' }] }
      const defaultMax = configNumber(config, `${PLUGIN}.max_turns`, DEFAULT_MAX_TURNS)
      const requested = typeof input.max_turns === 'number' ? input.max_turns : defaultMax
      const maxTurns = Math.min(Math.max(1, Math.floor(requested)), HARD_MAX_TURNS)
      await goals.replace(stateKey(ctx.thread.id), () => ({
        goal, maxTurns, turnsUsed: 0, active: true, setAt: new Date().toISOString(),
      }))
      await logEvent(PLUGIN, { action: 'set', thread: ctx.thread.id, goal, maxTurns })
      return { content: [{ type: 'text', text: `Goal loop armed (max ${maxTurns} turns): ${goal}` }] }
    },
  })

  amp.registerTool({
    name: 'goal_status',
    title: 'Loop goal status',
    description: 'Show the current goal loop for this thread: the goal, turns used vs max, and whether it is active.',
    inputSchema: { type: 'object', properties: {} },
    async execute(_input, ctx) {
      const state = await goals.snapshot(stateKey(ctx.thread.id))
      if (!state) return { content: [{ type: 'text', text: 'No goal loop has been set for this thread.' }] }
      const lines = [
        `Goal: ${state.goal}`,
        `Active: ${state.active}`,
        `Turns used: ${state.turnsUsed}/${state.maxTurns}`,
        state.stoppedReason ? `Stopped: ${state.stoppedReason}` : null,
        state.lastEvaluation ? `Last evaluation: ${state.lastEvaluation}` : null,
      ].filter(Boolean)
      return { content: [{ type: 'text', text: lines.join('\n') }] }
    },
  })

  amp.registerTool({
    name: 'goal_clear',
    title: 'Clear loop goal',
    description: 'Stop the goal loop for this thread. Use when the user asks to stop looping or the goal is obsolete.',
    inputSchema: { type: 'object', properties: {} },
    async execute(_input, ctx) {
      const key = stateKey(ctx.thread.id)
      const cleared = await goals.clear(key)
      if (!cleared) return { content: [{ type: 'text', text: 'No active goal loop to clear.' }] }
      await logEvent(PLUGIN, { action: 'clear', thread: ctx.thread.id })
      return { content: [{ type: 'text', text: `Goal loop cleared: ${cleared.goal}` }] }
    },
  })

  // ---- the loop ----------------------------------------------------------

  amp.on('agent.end', async (event, ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return

    const key = stateKey(event.thread.id)
    const state = await goals.snapshot(key)
    if (!state || !state.active) return

    // Never auto-continue a failed or cancelled turn — pause instead.
    if (event.status !== 'done') {
      const paused = await goals.commit(key, state.revision ?? 0, current => ({ ...current, active: false, stoppedReason: `paused on ${event.status}` }))
      if (!paused) return
      await logEvent(PLUGIN, { action: 'pause', thread: event.thread.id, status: event.status, turnsUsed: state.turnsUsed })
      if (!isHeadless()) {
        try { await ctx.ui.notify(`Goal loop paused (turn ${event.status}): ${state.goal}`) } catch { /* headless-safe */ }
      }
      return
    }

    try {
      const text = turnText(event.messages)
      const verdict = await ctx.ai.ask(
        `A goal loop is running in this conversation. Goal (done-condition): "${state.goal}".\n` +
        `Here is what the agent reported in its latest turn:\n---\n${text || '(no assistant text this turn)'}\n---\n` +
        `Based only on this, is the goal now fully met?`,
      )
      const threshold = Math.min(Math.max(configNumber(config, `${PLUGIN}.met_threshold`, DEFAULT_MET_THRESHOLD), 0.5), 1)
      const met = verdict.result === 'yes' && verdict.probability >= threshold

      if (met) {
        const committed = await goals.commit(key, state.revision ?? 0, current => ({ ...current, active: false, stoppedReason: 'goal met', lastEvaluation: verdict.reason }))
        if (!committed) return
        await logEvent(PLUGIN, { action: 'met', thread: event.thread.id, turnsUsed: state.turnsUsed, probability: verdict.probability })
        if (!isHeadless()) {
          try { await ctx.ui.notify(`Goal met after ${state.turnsUsed} loop turn(s): ${state.goal}`) } catch { /* headless-safe */ }
        }
        return
      }

      const turnsUsed = state.turnsUsed + 1
      if (turnsUsed >= state.maxTurns) {
        const committed = await goals.commit(key, state.revision ?? 0, current => ({ ...current, turnsUsed, active: false, stoppedReason: 'max turns reached', lastEvaluation: verdict.reason }))
        if (!committed) return
        await logEvent(PLUGIN, { action: 'max-turns', thread: event.thread.id, turnsUsed })
        if (!isHeadless()) {
          try { await ctx.ui.notify(`Goal loop stopped at max turns (${state.maxTurns}): ${state.goal}`) } catch { /* headless-safe */ }
        }
        return
      }

      const committed = await goals.commit(key, state.revision ?? 0, current => ({ ...current, turnsUsed, lastEvaluation: verdict.reason }))
      if (!committed) return
      await logEvent(PLUGIN, { action: 'continue', thread: event.thread.id, turnsUsed, probability: verdict.probability })
      const latest = await goals.snapshot(key)
      if (!latest?.active || latest.revision !== committed.revision) return
      return {
        action: 'continue',
        userMessage:
          `🔁 goal-loop (turn ${turnsUsed}/${state.maxTurns}) — the goal is not yet met.\n` +
          `Goal: ${state.goal}\n` +
          `Evaluator: ${verdict.reason}\n` +
          `Continue working toward the goal. When it is fully met, state so explicitly. ` +
          `If it cannot be met, call goal_clear and explain why.`,
      }
    } catch (error) {
      // Never disrupt the session; pause the loop on evaluator failure.
      const message = error instanceof Error ? error.message : String(error)
      const committed = await goals.commit(key, state.revision ?? 0, current => ({ ...current, active: false, stoppedReason: `evaluator error: ${message}` }))
      if (!committed) return
      await logEvent(PLUGIN, { action: 'error', thread: event.thread.id, error: message })
    }
  })
}
