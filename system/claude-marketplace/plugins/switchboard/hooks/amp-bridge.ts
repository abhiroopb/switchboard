// Claude Code hook → Amp plugin bridge.
//
// Claude Code runs this script once per hook event with the event JSON on
// stdin. The script loads the switchboard Amp plugin modules from
// system/amp-plugins/, replays the Claude event through a minimal Amp
// PluginAPI shim (same shapes as system/amp-plugins/switchboard/test-runner.ts),
// and prints the Claude hook JSON decision on stdout.
//
// Usage (from hooks/hooks.json):
//   bun "${CLAUDE_PLUGIN_ROOT}/hooks/amp-bridge.ts" <pre-tool-use|post-tool-use|user-prompt-submit|stop|session-start>
//
// Rules are never re-implemented here: switchboard-guardrails.ts and friends stay the
// single source of truth. This file only maps event shapes and decisions.
//
// Known limits (documented in README.md): no in-memory state across events,
// `ctx.ui.confirm` becomes a Claude permission "ask", `ctx.ai.ask` is
// unavailable, Amp-only tools (send_thread_message, create_thread, wait_for_threads)
// never arrive so the dispatch and wait guards are inert.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { homedir } from 'node:os'
import { exec } from 'node:child_process'

type EventKind = 'pre-tool-use' | 'post-tool-use' | 'user-prompt-submit' | 'stop' | 'session-start'

const HOME = process.env.HOME ?? process.env.USERPROFILE ?? homedir()
// The plugin lives at <repo>/system/claude-marketplace/plugins/switchboard/hooks, so the
// repo root is five directories up from this file. Claude loads the plugin in place,
// so this holds wherever the repo was installed. SWITCHBOARD_REPO or SWITCHBOARD_ROOT override it.
const REPO_FROM_FILE = resolve(import.meta.dir, '..', '..', '..', '..', '..')
const SWITCHBOARD_REPO = process.env.SWITCHBOARD_REPO ?? process.env.SWITCHBOARD_ROOT ?? REPO_FROM_FILE
const AMP_PLUGINS_DIR = process.env.SWITCHBOARD_AMP_PLUGINS_DIR ?? `${SWITCHBOARD_REPO}/system/amp-plugins`
const CONFIG_PATH = process.env.SWITCHBOARD_PLUGIN_CONFIG ?? `${HOME}/.claude/switchboard-plugin-config.json`
const BRIDGE = 'switchboard-bridge'

/** Which Amp plugins run for each Amp event. Amp usage telemetry plugins stay out. amp-mem is replaced by claude-mem. goal-loop is native /goal in Claude. */
const PLUGINS_BY_AMP_EVENT: Record<string, string[]> = {
  'tool.call': ['switchboard-guardrails', 'task-liveness'],
  'tool.result': ['switchboard-guardrails', 'task-liveness', 'daily-rollup-trigger'],
  'agent.start': ['auto-pilot-router', 'switchboard-guardrails', 'task-liveness', 'review-lane', 'task-caffeinate', 'workspace-report'],
  'agent.end': ['chrome-mcp-lease', 'comms-triage-trigger', 'cos-finished', 'review-lane', 'task-caffeinate', 'task-liveness', 'workspace-report', 'workstream-lifecycle'],
  'session.start': ['watched-docs-refresh', 'watched-slack-refresh', 'workspace-report', 'workstream-signals'],
}

const AMP_EVENT_FOR_KIND: Record<EventKind, string> = {
  'pre-tool-use': 'tool.call',
  'post-tool-use': 'tool.result',
  'user-prompt-submit': 'agent.start',
  'stop': 'agent.end',
  'session-start': 'session.start',
}

interface ClaudeHookInput {
  session_id?: string
  transcript_path?: string
  cwd?: string
  hook_event_name?: string
  tool_name?: string
  tool_input?: Record<string, unknown>
  tool_response?: unknown
  prompt?: string
  source?: string
  stop_hook_active?: boolean
  permission_mode?: string
  last_assistant_message?: string
  /** Set on a subagent's tool call (Claude Code 2.1+). */
  agent_id?: string
  agent_type?: string
}

class ConfirmRequested extends Error {
  constructor(public title: string, public detail?: string) { super(title) }
}

function readConfig(): Record<string, unknown> {
  try {
    if (existsSync(CONFIG_PATH)) return JSON.parse(readFileSync(CONFIG_PATH, 'utf8'))
  } catch (e) {
    process.stderr.write(`[${BRIDGE}] config unreadable: ${(e as Error).message}\n`)
  }
  return {}
}

function readStdin(): Promise<string> {
  return new Promise(resolvePromise => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', chunk => { data += chunk })
    process.stdin.on('end', () => resolvePromise(data))
    process.stdin.on('error', () => resolvePromise(data))
  })
}

// ── Tool-name mapping ──────────────────────────────────────────────────

/** Claude tool call → Amp tool call. Returns null for tools with no Amp meaning. */
const BRIDGE_WORKDIR = '__switchboard_bridge_workdir'

function toAmpToolCall(name: string, input: Record<string, unknown>, cwd = ''): { tool: string; input: Record<string, unknown> } {
  switch (name) {
    case 'Bash':
      // Amp's Bash input carries `workdir`; Claude's hook input has the session cwd.
      // Rule 3 (rm -rf) resolves relative targets against it.
      return { tool: 'Bash', input: { ...input, cmd: String(input.command ?? ''), ...(cwd && !('workdir' in input) ? { workdir: cwd, [BRIDGE_WORKDIR]: true } : {}) } }
    case 'Edit':
    case 'MultiEdit':
    case 'NotebookEdit':
      return { tool: 'edit_file', input: { ...input, path: String(input.file_path ?? input.notebook_path ?? '') } }
    case 'Write':
      return { tool: 'create_file', input: { ...input, path: String(input.file_path ?? '') } }
    default:
      // mcp__<server>__<tool> and native tools pass through unchanged.
      return { tool: name, input }
  }
}

/** Amp `allow-with-input` → Claude `updatedInput` (undo the field renames). */
function toClaudeInput(name: string, ampInput: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...ampInput }
  if (name === 'Bash' && 'cmd' in out) { out.command = out.cmd; delete out.cmd }
  if (out[BRIDGE_WORKDIR]) { delete out.workdir; delete out[BRIDGE_WORKDIR] }
  if ((name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'NotebookEdit') && 'path' in out) {
    if (name === 'NotebookEdit') out.notebook_path = out.path
    else out.file_path = out.path
    delete out.path
  }
  return out
}

// ── Amp PluginAPI shim ────────────────────────────────────────────────

type Handler = { event: string; fn: (event: unknown, ctx: unknown) => unknown }

class BridgeAmp {
  handlers: Handler[] = []
  commands: Array<{ id: string }> = []
  tools: Array<{ name: string }> = []
  notices: string[] = []
  confirms: Array<{ title: string; detail?: string }> = []
  disposers: Array<() => void> = []

  // Class field initializers run before parameter properties are assigned, so a
  // field that reads this.sessionId would see undefined.
  activeThread: { current: { id: string; title: string }; subscribe: (cb: () => void) => { unsubscribe(): void } }

  constructor(public config: Record<string, unknown>, public sessionId: string, public cwd: string) {
    this.activeThread = {
      current: { id: sessionId, title: process.env.SWITCHBOARD_GUARDRAILS_WS_TITLE_OVERRIDE ?? '' },
      subscribe: (_cb: () => void) => ({ unsubscribe() {} }),
    }
  }

  logger = {
    log: (...a: unknown[]) => process.stderr.write(`[plugin.log] ${a.map(String).join(' ')}\n`),
    warn: (...a: unknown[]) => process.stderr.write(`[plugin.warn] ${a.map(String).join(' ')}\n`),
  }
  system = { env: process.env, workspaceRoot: { toString: () => `file://${this.cwd}` } }
  $ = (strings: TemplateStringsArray, ...values: unknown[]) => {
    const cmd = strings.reduce((acc, s, i) => acc + s + (i < values.length ? String(values[i]) : ''), '')
    return new Promise<{ stdout: string; stderr: string }>(resolvePromise => {
      exec(cmd, { cwd: this.cwd, timeout: 20_000 }, (_err, stdout, stderr) => resolvePromise({ stdout: String(stdout), stderr: String(stderr) }))
    })
  }

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

  threads = {
    get: (_id: string) => ({ state: { get: async () => 'idle' } }),
  }

  on(event: string, fn: (e: unknown, ctx: unknown) => unknown) {
    this.handlers.push({ event, fn })
    return { unsubscribe() {} }
  }
  registerCommand(id: string, _options: unknown) { this.commands.push({ id }); return { unsubscribe() {} } }
  registerTool(def: { name: string }) { this.tools.push({ name: def.name }); return { unsubscribe() {} } }
  onDispose(fn: () => void) { this.disposers.push(fn) }
}

export interface TranscriptMessage { role: 'user' | 'assistant' | 'info'; id: string | number; content: Array<Record<string, unknown>> }

/** True when a Claude transcript row is a tool result carried as a "user" message. */
function isToolResultOnly(content: Array<Record<string, unknown>>): boolean {
  return content.length > 0 && content.every(c => c?.type === 'tool_result')
}

/**
 * Best-effort read of the Claude session transcript (JSONL) in Amp message shape.
 * Claude stores every tool result as a `type: "user"` row; Amp never counts those
 * as user messages, so they are skipped. Kept, they would make a
 * `messages({ roles: ['user'], limit: 20 })` read lose the seed prompt after
 * about 20 tool calls.
 */
export function readTranscript(path: string | undefined): TranscriptMessage[] {
  if (!path || !existsSync(path)) return []
  const out: TranscriptMessage[] = []
  try {
    for (const line of readFileSync(path, 'utf8').split('\n')) {
      if (!line.trim()) continue
      let row: Record<string, unknown>
      try { row = JSON.parse(line) } catch { continue }
      const role = row.type === 'user' ? 'user' : row.type === 'assistant' ? 'assistant' : null
      if (!role) continue
      const msg = row.message as { content?: unknown } | undefined
      const content = Array.isArray(msg?.content)
        ? (msg!.content as Array<Record<string, unknown>>)
        : typeof msg?.content === 'string' ? [{ type: 'text', text: msg.content }] : []
      if (role === 'user' && isToolResultOnly(content)) continue
      // Meta rows (slash-command caveats, skill bodies) are not prompts anyone typed.
      if (role === 'user' && row.isMeta === true) continue
      out.push({ role, id: String(row.uuid ?? out.length), content })
    }
  } catch { /* unreadable transcript: empty history */ }
  return out
}

function buildCtx(amp: BridgeAmp, transcript: TranscriptMessage[]) {
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
      notify: async (msg: string) => { amp.notices.push(msg) },
      // A hook cannot show a dialog. Escalate to Claude's permission prompt.
      // Plugins such as guardrails wrap confirm in a timeout race that swallows
      // the throw, so the request is also recorded for the reject→ask upgrade.
      confirm: async (opts: { title: string; message?: string }) => {
        amp.confirms.push({ title: opts.title, detail: opts.message })
        throw new ConfirmRequested(opts.title, opts.message)
      },
      select: async (opts: { title: string; options: string[] }) => opts.options[0],
      input: async (_opts: { title?: string }) => '',
    },
    ai: {
      ask: async (_q: string) => ({ result: 'no', probability: 0, reason: 'ai.ask unavailable in Claude hook bridge' }),
    },
    thread: { id: amp.sessionId, title: amp.activeThread.current.title, messages },
  }
}

// ── Event replay ──────────────────────────────────────────────────────

async function loadPlugins(amp: BridgeAmp, names: string[]): Promise<string[]> {
  const loaded: string[] = []
  const only = process.env.SWITCHBOARD_BRIDGE_ONLY?.split(',').map(s => s.trim()).filter(Boolean)
  for (const name of names) {
    if (only && only.length > 0 && !only.includes(name)) continue
    if (amp.config[`${name}.disabled`] === true) continue
    const path = resolve(AMP_PLUGINS_DIR, `${name}.ts`)
    if (!existsSync(path)) { process.stderr.write(`[${BRIDGE}] missing plugin ${path}\n`); continue }
    try {
      const mod = await import(path)
      if (typeof mod.default !== 'function') continue
      await mod.default(amp)
      loaded.push(name)
    } catch (e) {
      process.stderr.write(`[${BRIDGE}] ${name} failed to load: ${(e as Error).stack ?? e}\n`)
    }
  }
  return loaded
}

type AmpToolDecision =
  | { action: 'allow' }
  | { action: 'reject' | 'reject-and-continue'; message?: string }
  | { action: 'allow-with-input'; input: Record<string, unknown> }
  | { action: 'warn'; message?: string }
  | undefined
  | null

/**
 * The Amp `agent.end` event for a Claude Stop hook. Amp sends
 * `{ status, message: <the user prompt>, messages: <this turn> }`. Plugins
 * gated on `status === 'done'` (workspace-report, review-lane, cos-finished,
 * workstream-lifecycle) need the status. Claude only runs Stop for a turn that finished, so the
 * status is always done. `last_assistant_message` (newer Claude Code) covers a
 * final message the transcript has not flushed yet.
 */
export function stopEvent(transcript: TranscriptMessage[], input: { last_assistant_message?: unknown }, sessionId: string,
                          thread: { id: string; title: string }): Record<string, unknown> {
  let start = transcript.length
  for (let i = transcript.length - 1; i >= 0; i--) {
    if (transcript[i].role === 'user') { start = i; break }
  }
  const prompt = start < transcript.length
    ? transcript[start].content.filter(c => c.type === 'text').map(c => String(c.text ?? '')).join('\n').trim()
    : ''
  const messages = transcript.slice(start < transcript.length ? start : 0)
  const final = typeof input.last_assistant_message === 'string' ? input.last_assistant_message.trim() : ''
  if (final && lastAssistantText(messages) !== final) {
    messages.push({ role: 'assistant', id: `final-${messages.length}`, content: [{ type: 'text', text: final }] })
  }
  return { id: `${sessionId}:${Date.now()}`, thread, status: 'done', message: prompt, messages }
}

function lastAssistantText(transcript: TranscriptMessage[]): string {
  for (let i = transcript.length - 1; i >= 0; i--) {
    const m = transcript[i]
    if (m.role !== 'assistant') continue
    const text = m.content.filter(c => c.type === 'text').map(c => String(c.text ?? '')).join('\n').trim()
    if (text) return text
  }
  return ''
}

/**
 * Record a permission ask for system/scripts/permission_relay.py, which sends any
 * remaining permission prompt to CoS. Claude's
 * Notification hook carries only a message string, so the relay reads the exact
 * command and the guardrail reason from here. One file per session, newest ask
 * wins. The relay uses a record while its command is still in the dialog on
 * screen (up to an hour), or for 120 s when the screen cannot be read.
 */
export function recordPermissionAsk(sessionId: string, toolName: string, toolInput: Record<string, unknown>, reason: string,
                                    cwd: string, agentType = '',
                                    stateDir = process.env.SWITCHBOARD_STATE_DIR ?? `${SWITCHBOARD_REPO}/system/state`) {
  try {
    const dir = `${stateDir}/worker-dispatch/permission-asks`
    mkdirSync(dir, { recursive: true })
    const command = toolName === 'Bash' ? String(toolInput.command ?? '') : JSON.stringify(toolInput).slice(0, 4000)
    writeFileSync(`${dir}/${sessionId.replace(/[^A-Za-z0-9_.-]/g, '_')}.json`, JSON.stringify({
      session_id: sessionId, tool: toolName, command, reason, cwd, agent_type: agentType, at: new Date().toISOString(),
      workspace_id: process.env.CMUX_WORKSPACE_ID ?? '', task_id: process.env.SWITCHBOARD_WORKER_TASK_ID ?? '',
    }, null, 1) + '\n')
  } catch (e) {
    process.stderr.write(`[${BRIDGE}] could not record the permission ask: ${(e as Error).message}\n`)
  }
}

function emit(obj: Record<string, unknown>) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

async function main() {
  const kind = process.argv[2] as EventKind | undefined
  if (!kind || !(kind in AMP_EVENT_FOR_KIND)) {
    process.stderr.write(`[${BRIDGE}] usage: amp-bridge.ts <${Object.keys(AMP_EVENT_FOR_KIND).join('|')}>\n`)
    process.exit(0)
  }
  const ampEvent = AMP_EVENT_FOR_KIND[kind]
  // Plugins run under the bridge: guardrails leaves the prompt relay to the Notification hook.
  process.env.SWITCHBOARD_CLAUDE_BRIDGE = '1'
  let input: ClaudeHookInput = {}
  try { input = JSON.parse((await readStdin()) || '{}') } catch { input = {} }

  const sessionId = String(input.session_id ?? 'claude-session')
  const cwd = String(input.cwd ?? process.cwd())
  const config = readConfig()
  if (config[`${BRIDGE}.disabled`] === true) process.exit(0)

  const amp = new BridgeAmp(config, sessionId, cwd)
  const loaded = await loadPlugins(amp, PLUGINS_BY_AMP_EVENT[ampEvent] ?? [])
  if (process.env.SWITCHBOARD_BRIDGE_DEBUG) process.stderr.write(`[${BRIDGE}] ${kind} loaded=${loaded.join(',')} handlers=${amp.handlers.filter(h => h.event === ampEvent).length}\n`)

  const transcript = readTranscript(input.transcript_path)
  const ctx = buildCtx(amp, transcript)
  const thread = { id: sessionId, title: amp.activeThread.current.title }
  const handlers = amp.handlers.filter(h => h.event === ampEvent)

  const finish = (output?: Record<string, unknown>) => {
    for (const d of amp.disposers) { try { d() } catch { /* ignore */ } }
    if (output) emit(output)
    // Plugins may leave intervals or detached children behind; a hook must exit.
    process.exit(0)
  }

  if (kind === 'pre-tool-use') {
    const toolName = String(input.tool_name ?? '')
    const toolInput = (input.tool_input ?? {}) as Record<string, unknown>
    const mapped = toAmpToolCall(toolName, toolInput, cwd)
    let event: Record<string, unknown> = { ...mapped, thread, id: `${sessionId}:${Date.now()}` }
    let updatedInput: Record<string, unknown> | null = null
    const warnings: string[] = []
    for (const h of handlers) {
      let decision: AmpToolDecision
      try {
        decision = (await h.fn(event, ctx)) as AmpToolDecision
      } catch (e) {
        if (e instanceof ConfirmRequested) {
          const reason = [e.title, e.detail].filter(Boolean).join('\n\n')
          recordPermissionAsk(sessionId, toolName, toolInput, reason, cwd, String(input.agent_type ?? ''))
          return finish({
            hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'ask', permissionDecisionReason: `[switchboard] ${reason}` },
            ...(amp.notices.length ? { systemMessage: amp.notices.join('\n') } : {}),
          })
        }
        process.stderr.write(`[${BRIDGE}] handler error: ${(e as Error).stack ?? e}\n`)
        continue
      }
      if (!decision || decision.action === 'allow') continue
      if (decision.action === 'reject' || decision.action === 'reject-and-continue') {
        // The handler asked for confirmation and treated the bridge's throw as a
        // UI failure. Hand the decision to Claude's permission prompt instead.
        const pending = amp.confirms.splice(0)
        if (pending.length && /confirmation/i.test(decision.message ?? '')) {
          const reason = pending.map(c => [c.title, c.detail].filter(Boolean).join('\n\n')).join('\n\n')
          recordPermissionAsk(sessionId, toolName, toolInput, reason, cwd, String(input.agent_type ?? ''))
          return finish({
            hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'ask', permissionDecisionReason: `[switchboard] ${reason}` },
            ...(amp.notices.length ? { systemMessage: amp.notices.join('\n') } : {}),
          })
        }
        return finish({
          hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: `[switchboard] ${decision.message ?? 'blocked by switchboard guardrails'}` },
          ...(amp.notices.length ? { systemMessage: amp.notices.join('\n') } : {}),
        })
      }
      if (decision.action === 'allow-with-input') {
        updatedInput = decision.input
        event = { ...event, input: decision.input }
        continue
      }
      if (decision.action === 'warn' && decision.message) warnings.push(decision.message)
    }
    const out: Record<string, unknown> = {}
    const specific: Record<string, unknown> = { hookEventName: 'PreToolUse' }
    if (updatedInput) { specific.permissionDecision = 'allow'; specific.updatedInput = toClaudeInput(toolName, updatedInput) }
    const context = [...warnings, ...amp.notices]
    if (context.length) specific.additionalContext = context.map(c => `[switchboard] ${c}`).join('\n')
    if (Object.keys(specific).length > 1) out.hookSpecificOutput = specific
    if (amp.notices.length) out.systemMessage = amp.notices.join('\n')
    return finish(Object.keys(out).length ? out : undefined)
  }

  if (kind === 'post-tool-use') {
    const toolName = String(input.tool_name ?? '')
    const mapped = toAmpToolCall(toolName, (input.tool_input ?? {}) as Record<string, unknown>)
    const event = { ...mapped, output: input.tool_response, status: 'done', thread, id: `${sessionId}:${Date.now()}` }
    for (const h of handlers) {
      try { await h.fn(event, ctx) } catch (e) { if (!(e instanceof ConfirmRequested)) process.stderr.write(`[${BRIDGE}] handler error: ${(e as Error).stack ?? e}\n`) }
    }
    return finish(amp.notices.length ? { systemMessage: amp.notices.join('\n') } : undefined)
  }

  if (kind === 'user-prompt-submit') {
    const event = { id: `${sessionId}:${Date.now()}`, thread, message: String(input.prompt ?? '') }
    const injected: string[] = []
    for (const h of handlers) {
      try {
        const r = (await h.fn(event, ctx)) as { message?: { content?: string } } | undefined
        const content = r?.message?.content?.trim()
        if (content) injected.push(content)
      } catch (e) { if (!(e instanceof ConfirmRequested)) process.stderr.write(`[${BRIDGE}] handler error: ${(e as Error).stack ?? e}\n`) }
    }
    const out: Record<string, unknown> = {}
    if (injected.length) out.hookSpecificOutput = { hookEventName: 'UserPromptSubmit', additionalContext: injected.join('\n\n') }
    if (amp.notices.length) out.systemMessage = amp.notices.join('\n')
    return finish(Object.keys(out).length ? out : undefined)
  }

  if (kind === 'stop') {
    const event = stopEvent(transcript, input, sessionId, thread)
    for (const h of handlers) {
      try { await h.fn(event, ctx) } catch (e) { if (!(e instanceof ConfirmRequested)) process.stderr.write(`[${BRIDGE}] handler error: ${(e as Error).stack ?? e}\n`) }
    }
    return finish(amp.notices.length ? { systemMessage: amp.notices.join('\n') } : undefined)
  }

  // session-start
  const event = { thread, source: input.source ?? 'startup' }
  const injected: string[] = []
  for (const h of handlers) {
    try {
      const r = (await h.fn(event, ctx)) as { message?: { content?: string } } | undefined
      const content = r?.message?.content?.trim()
      if (content) injected.push(content)
    } catch (e) { if (!(e instanceof ConfirmRequested)) process.stderr.write(`[${BRIDGE}] handler error: ${(e as Error).stack ?? e}\n`) }
  }
  const out: Record<string, unknown> = {}
  if (injected.length) out.hookSpecificOutput = { hookEventName: 'SessionStart', additionalContext: injected.join('\n\n') }
  if (amp.notices.length) out.systemMessage = amp.notices.join('\n')
  return finish(Object.keys(out).length ? out : undefined)
}

// Run only as the hook entry point; tests import readTranscript without running a hook.
if (import.meta.main) {
  main().catch(e => {
    // Fail open: a bridge crash must never block Claude.
    process.stderr.write(`[${BRIDGE}] fatal: ${(e as Error).stack ?? e}\n`)
    process.exit(0)
  })
}
