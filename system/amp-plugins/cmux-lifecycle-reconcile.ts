// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
// Backstop for the cmux-managed plugin. Never patch its generated runtime file.
import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import type { PluginAPI } from '@ampcode/plugin'
import { HOME, isPluginEnabled, logEvent } from './switchboard/lib'

export const description = 'Re-stamps the cmux amp badge and re-sends the completion hook after fresh Amp idle evidence, bounded and fenced by the current thread and turn.'

type Completion = { thread: string; turn: string; outcome: string }

// The managed plugin's presentation strings (cmux renders the labels as
// "Idle" / "Running" in the sidebar and in the session store).
export const BADGE = {
  idle: { label: '__cmux_amp_status_idle', icon: 'circle', color: '#adb5bd' },
  error: { label: '__cmux_amp_status_error', icon: 'xmark.circle', color: '#ff5555' },
} as const

// Attempt schedule after agent.end. The first wait lets the managed plugin's
// own Idle write persist (the store is flushed lazily, about 20 s) so a
// healthy turn costs no cmux call at all. The schedule is bounded: an
// unbounded re-send of `cmux hooks amp lifecycle` per stuck thread never
// flips the badge and starves cmux's socket.
export const RETRY_DELAYS_MS = [30_000, 60_000, 120_000, 240_000, 480_000]

export function storedState(data: any, workspace: string, surface: string, thread: string): string | null {
  for (const window of data.windows ?? []) {
    for (const ws of window.tabManager?.workspaces ?? []) {
      if (ws.workspaceId !== workspace) continue
      const panels = (ws.panels ?? []).filter((p: any) => p.terminal?.agent?.kind === 'amp')
      // Workspace-wide status cannot disambiguate multiple Amp surfaces.
      if (panels.length !== 1 || panels[0].id !== surface || panels[0].terminal.agent.sessionId !== thread) return null
      return ws.statusEntries?.find((entry: any) => entry.key === 'amp')?.value?.toLowerCase() ?? null
    }
  }
  return null
}

export type Sender = (completion: Completion, state: string, current: () => boolean, attempt: number) => Promise<boolean>

// Owns only completion delivery, not task status or worker admission. Read a
// fresh handle each time: the managed plugin can retain an old subscription
// when an event replaces the same thread's observable object.
export class CompletionRepair {
  private pending: Completion | null = null
  private generation = 0
  private polling = false
  private attempt = 0
  private nextAt = 0

  constructor(
    private current: () => string | undefined,
    private state: (id: string) => Promise<string>,
    private send: Sender,
    private options: { delaysMs?: number[]; now?: () => number; onAbandon?: (completion: Completion, attempts: number) => void } = {},
  ) {}

  private get delays(): number[] { return this.options.delaysMs ?? RETRY_DELAYS_MS }
  private get now(): number { return (this.options.now ?? Date.now)() }

  start(): void {
    this.generation++
    this.pending = null
  }

  end(completion: Completion): void {
    if (completion.thread !== this.current()) return
    this.generation++
    this.pending = completion
    this.attempt = 0
    this.nextAt = this.now + this.delays[0]
  }

  async poll(): Promise<void> {
    if (this.polling || !this.pending || this.now < this.nextAt) return
    this.polling = true
    const completion = this.pending
    const generation = this.generation
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const state = await Promise.race([
        this.state(completion.thread),
        new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('state timeout')), 2000) }),
      ])
      if (generation !== this.generation || completion.thread !== this.current()) return
      // agent.end is not proof of idle: a continuation or approval can follow.
      if (state !== 'idle' && state !== 'error') return
      const delivered = await this.send(completion, state,
        () => generation === this.generation && completion.thread === this.current(), this.attempt)
      if (generation !== this.generation) return
      if (delivered) { this.pending = null; return }
      this.schedule(completion)
    } catch {
      // Keep pending; the next bounded poll retries, never synthesizes idle.
      if (generation === this.generation) this.schedule(completion)
    } finally {
      clearTimeout(timer)
      this.polling = false
    }
  }

  private schedule(completion: Completion): void {
    this.attempt++
    if (this.attempt >= this.delays.length) {
      this.pending = null
      this.options.onAbandon?.(completion, this.attempt)
      return
    }
    this.nextAt = this.now + this.delays[this.attempt]
  }
}

export default function (amp: PluginAPI) {
  if (!process.env.CMUX_WORKSPACE_ID || !process.env.CMUX_SURFACE_ID || process.env.CMUX_AMP_HOOKS_DISABLED === '1') return
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(value => { config = value })
  const enabled = () => isPluginEnabled(config, 'cmux-lifecycle-reconcile')
  const cmux = process.env.CMUX_BIN ?? process.env.CMUX_AMP_CMUX_BIN ?? '/Applications/cmux.app/Contents/Resources/bin/cmux'
  const store = process.env.SWITCHBOARD_CMUX_SESSION_JSON ?? process.env.CC_CMUX_SESSION_JSON ?? `${HOME}/Library/Application Support/cmux/session-com.cmuxterm.app.json`
  const env = { ...process.env } as NodeJS.ProcessEnv
  delete env.AMP_API_KEY
  env.CMUX_AMP_PID ||= String(process.pid)

  const exec = (args: string[], stdin?: string) => new Promise<string | null>(resolve => {
    const child = execFile(cmux, args, { env, timeout: 5000 }, (error, _stdout, stderr) => {
      resolve(error ? String(stderr || error.message).slice(0, 300) : null)
    })
    child.stdin?.on('error', () => {})
    if (stdin === undefined) child.stdin?.end()
    else child.stdin?.end(stdin)
  })

  const repair = new CompletionRepair(
    () => amp.activeThread.current?.id,
    id => amp.threads.get(id as any).state.get(),
    async (completion, state, current, attempt) => {
      const snapshot = JSON.parse(await readFile(store, 'utf8'))
      const stored = storedState(snapshot, process.env.CMUX_WORKSPACE_ID!, process.env.CMUX_SURFACE_ID!, completion.thread)
      if (stored === 'idle' || (stored === 'error' && state === 'error')) return true
      if (stored !== 'running') return false // Missing/ambiguous binding is not permission to write.
      // Recheck after disk I/O; never send an old completion into a new turn.
      if (!current()) return false
      // The completion hook feeds cmux's notifications; it does not flip the
      // badge, so it goes once. The badge is re-stamped with the managed
      // plugin's own `set-status` every attempt until the store reads it back.
      const hookError = attempt === 0
        ? await exec(['hooks', 'amp', 'lifecycle'], JSON.stringify({
          session_id: completion.thread, cwd: process.env.CMUX_AGENT_LAUNCH_CWD || process.cwd(),
          hook_event_name: 'Lifecycle', event: 'Lifecycle', agent_state: state,
          turn_id: completion.turn, turn_outcome: completion.outcome,
          ...(state === 'error' ? { notification_type: 'error' } : {}),
        }))
        : null
      const badge = state === 'error' ? BADGE.error : BADGE.idle
      const badgeError = await exec(['set-status', 'amp', badge.label, '--icon', badge.icon, '--color', badge.color,
        '--workspace', process.env.CMUX_WORKSPACE_ID!])
      void logEvent('cmux-lifecycle-reconcile', {
        action: badgeError ? 'badge-restamp-failed' : 'badge-restamped',
        thread: completion.thread, turn: completion.turn, attempt, stored,
        ...(attempt === 0 ? { hook: hookError ? 'failed' : 'sent' } : {}),
        ...(hookError ? { hookError } : {}), ...(badgeError ? { error: badgeError } : {}),
      })
      // Exit success is transport evidence. The next attempt must read back
      // Idle from the matching cmux binding before retiring this repair.
      return false
    },
    {
      onAbandon: (completion, attempts) => void logEvent('cmux-lifecycle-reconcile', {
        action: 'repair-abandoned', thread: completion.thread, turn: completion.turn, attempts,
        note: 'store never read back Idle; dispatch.py treats a Running stamp older than IDLE_STAMP_TRUST as unknown',
      }),
    },
  )
  amp.on('agent.start', () => { repair.start() })
  amp.on('agent.end', event => {
    if (enabled()) repair.end({ thread: event.thread.id, turn: event.id, outcome: event.status })
  })
  const timer = setInterval(() => { if (enabled()) void repair.poll() }, 5000)
  timer.unref()
}
