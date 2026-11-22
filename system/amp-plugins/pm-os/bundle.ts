import type { PluginAPI } from '@ampcode/plugin'

type Initializer = (amp: PluginAPI) => void | Promise<void>

// These modules have at most one response-producing handler per event. Keep
// guardrails, memory, telemetry, and goal-loop in their own hosts: they have
// independent request/continuation semantics and are not merged here.
export async function bundle(amp: PluginAPI, modules: Array<[string, Initializer]>) {
  const hooks = new Map<string, Array<{ name: string; handler: Function }>>()
  for (const [name, initialize] of modules) {
    const scoped = Object.create(amp)
    scoped.on = (event: string, handler: Function) => {
      const entries = hooks.get(event) ?? []
      const entry = { name, handler }
      entries.push(entry)
      hooks.set(event, entries)
      return { unsubscribe: () => { const index = entries.indexOf(entry); if (index >= 0) entries.splice(index, 1) } }
    }
    try { await initialize(scoped) }
    catch (error) { amp.logger.log(`[${name}] initialization failed: ${String(error)}`) }
  }
  for (const [event, entries] of hooks) {
    amp.on(event as any, async (payload: any, ctx: any) => {
      const current = [...entries]
      const outcomes = await Promise.allSettled(current.map(({ handler }) => Promise.resolve().then(() => handler(payload, ctx))))
      const results: any[] = []
      for (const [index, outcome] of outcomes.entries()) {
        if (outcome.status === 'rejected') {
          ctx.logger.log(`[${current[index]?.name}] hook failed: ${String(outcome.reason)}`)
        } else if (outcome.value !== undefined) results.push(outcome.value)
      }
      if (results.length > 1) throw new Error(`PM OS bundle has multiple ${event} responses; keep these modules in separate hosts`)
      return results[0]
    })
  }
}
