// Minimal plugin test harness.
// Loads a plugin module and feeds synthetic events through it without a real
// Amp session. Run via Bun:
//   bun run ~/.config/amp/plugins/switchboard/test-runner.ts <plugin-path> [<fixture-path>]
//
// Fixtures are JSON files of the form:
//   { "events": [ { "type": "tool.call", "event": {...} }, ... ] }
//
// The harness records handler return values and logs to stdout. It does NOT
// implement the full PluginAPI surface — only what's commonly needed:
//  - on(event, handler)
//  - registerCommand (recorded but not invoked)
//  - registerTool (recorded but not invoked)
//  - ctx.ui.notify / confirm / select / input (mocked, configurable)
//  - ctx.$ (mocked echo)
//  - ctx.logger (console)

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

interface SyntheticEvent {
  type: string
  event: Record<string, unknown>
  uiResponse?: { confirm?: boolean; select?: string; input?: string }
  /** Per-event override of the thread transcript served by ctx.thread.messages(). */
  threadMessages?: ThreadMessageLike[]
}

interface ThreadMessageLike {
  role: 'user' | 'assistant' | 'info'
  id: string | number
  content: Array<Record<string, unknown>>
}

interface Fixture {
  config?: Record<string, unknown>
  /** Transcript served by ctx.thread.messages() unless an event overrides it. */
  threadMessages?: ThreadMessageLike[]
  events: SyntheticEvent[]
}

interface Handler {
  event: string
  fn: (event: unknown, ctx: unknown) => unknown
}

class FakeAmp {
  handlers: Handler[] = []
  commands: Array<{ id: string; options: unknown }> = []
  tools: Array<{ name: string; def: unknown }> = []
  config: Record<string, unknown>

  constructor(config: Record<string, unknown> = {}) {
    this.config = config
  }

  logger = { log: (...a: unknown[]) => console.log('[plugin.log]', ...a), warn: (...a: unknown[]) => console.warn('[plugin.warn]', ...a) }
  system = { env: process.env }
  $ = (_strings: TemplateStringsArray, ..._values: unknown[]) => Promise.resolve({ stdout: '', stderr: '' })

  helpers = {
    shellCommandFromToolCall: (e: { tool: string; input: Record<string, unknown> }) =>
      e.tool === 'Bash' ? { command: String(e.input.cmd ?? '') } : null,
    toolCallsInMessages: (_msgs: unknown[]) => [],
    filesModifiedByToolCall: (e: { tool: string; input: Record<string, unknown> }) => {
      if (e.tool === 'edit_file' || e.tool === 'create_file') return [{ toString: () => `file://${String(e.input.path ?? '')}` }]
      return null
    },
    filePathFromURI: (u: { toString: () => string }) => u.toString().replace(/^file:\/\//, ''),
    isPluginUINotAvailableError: (_e: Error) => false,
  }

  configuration = {
    subscribe: (cb: (v: Record<string, unknown>) => void) => { cb(this.config); return { unsubscribe() {} } },
    pipe: <T,>(op: (input: unknown) => T) => op(this.configuration),
    get: async () => this.config,
    update: async (partial: Record<string, unknown>) => { this.config = { ...this.config, ...partial } },
    delete: async (key: string) => { delete this.config[key] },
  }

  on(event: string, fn: (e: unknown, ctx: unknown) => unknown) {
    this.handlers.push({ event, fn })
    return { unsubscribe() {} }
  }

  registerCommand(id: string, options: unknown) {
    this.commands.push({ id, options })
    return { unsubscribe() {} }
  }

  registerTool(def: { name: string }) {
    this.tools.push({ name: def.name, def })
    return { unsubscribe() {} }
  }
}

function buildCtx(amp: FakeAmp, ui: SyntheticEvent['uiResponse'] = {}, transcript: ThreadMessageLike[] = []) {
  const messages = async (options: { roles?: string[]; from?: 'start' | 'end'; offset?: number; limit?: number } = {}) => {
    let items = transcript.filter(m => !options.roles || options.roles.includes(m.role))
    const limit = Math.min(Math.max(options.limit ?? 20, 1), 20)
    const offset = options.offset ?? 0
    if ((options.from ?? 'end') === 'end') {
      const end = Math.max(items.length - offset, 0)
      items = items.slice(Math.max(end - limit, 0), end)
    } else {
      items = items.slice(offset, offset + limit)
    }
    return items
  }
  return {
    logger: amp.logger,
    $: amp.$,
    system: amp.system,
    ui: {
      notify: async (msg: string) => { console.log('[ui.notify]', msg) },
      confirm: async (opts: { title: string; message?: string }) => {
        console.log('[ui.confirm]', opts.title, '→', ui.confirm ?? false)
        return ui.confirm ?? false
      },
      select: async (opts: { title: string; options: string[] }) => {
        console.log('[ui.select]', opts.title, '→', ui.select ?? opts.options[0])
        return ui.select ?? opts.options[0]
      },
      input: async (opts: { title?: string }) => {
        console.log('[ui.input]', opts.title, '→', ui.input ?? '')
        return ui.input ?? ''
      },
    },
    ai: {
      ask: async (q: string) => {
        console.log('[ai.ask]', q)
        return { result: 'no', probability: 0.5, reason: 'mocked' }
      },
    },
    thread: { id: 'T-test-thread', messages },
  }
}

async function main() {
  const [, , pluginPath, fixturePath] = process.argv
  if (!pluginPath) {
    console.error('Usage: bun run test-runner.ts <plugin-path> [<fixture-path>]')
    process.exit(1)
  }
  const absPlugin = resolve(pluginPath)
  const fixture: Fixture = fixturePath
    ? JSON.parse(readFileSync(resolve(fixturePath), 'utf8'))
    : { events: [{ type: 'session.start', event: {} }] }

  const amp = new FakeAmp(fixture.config ?? {})
  const mod = await import(absPlugin)
  const init = mod.default
  if (typeof init !== 'function') {
    console.error('Plugin must export a default function')
    process.exit(1)
  }
  await init(amp)
  console.log(`✓ Plugin loaded: ${absPlugin}`)
  console.log(`  Handlers: ${amp.handlers.map(h => h.event).join(', ') || '(none)'}`)
  console.log(`  Commands: ${amp.commands.map(c => c.id).join(', ') || '(none)'}`)
  console.log(`  Tools:    ${amp.tools.map(t => t.name).join(', ') || '(none)'}`)

  for (const ev of fixture.events) {
    const matched = amp.handlers.filter(h => h.event === ev.type)
    if (matched.length === 0) {
      console.log(`  ↪ ${ev.type}: no handler`)
      continue
    }
    for (const h of matched) {
      const ctx = buildCtx(amp, ev.uiResponse, ev.threadMessages ?? fixture.threadMessages ?? [])
      const result = await h.fn(ev.event, ctx)
      console.log(`  ↪ ${ev.type} → ${JSON.stringify(result) || '(void)'}`)
    }
  }
}

main().catch(e => { console.error(e); process.exit(1) })
