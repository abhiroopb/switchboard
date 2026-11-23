// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// task-liveness — deterministic receipt + heartbeat for router tasks.
//
// Problem: "work started" needs proof that a worker actually *received* a
// task, and an "agent running" age inferred from cmux status is not evidence.
//
// This plugin turns the seeded prompt into ledger evidence:
//   agent.start  → parse the seeded handoff for task ids and call
//                  `running-task-state.py ack` with this thread id, the cmux
//                  workspace ref and the workstream slug (executor provenance).
//                  Role is `router` when the prompt carries ROUTER_TASK_ID=
//                  (a Router seeded by router-dispatch.sh) and `worker` when it
//                  carries a "Router return contract: task rt-…" / `--task-id
//                  rt-…` return contract (a workstream seeded by a Router).
//   tool.result  → `heartbeat` for every tracked task, throttled to one call
//                  per SWITCHBOARD_TASK_HEARTBEAT_MS (default 60s) so the lease in
//                  the ledger only expires when the agent really stops.
//   agent.end    → final heartbeat; tasks the ledger reports as terminal or
//                  fenced out (_stale_attempt) are dropped from tracking.
//
// Late ack: a handoff that arrives as a steer during boot does not fire
// another agent.start. Guarded TUI handoffs now defer
// while busy, including legacy allow-busy sends. A pending
// handoff is not task receipt: ack occurs when Amp processes the user turn.
// Retain the late scan for deliberate native same-task steering and older
// senders. It scans user messages it has not seen yet via ctx.thread.messages() on
// tool.result (throttled by SWITCHBOARD_TASK_SCAN_MS, default 15s) and via
// event.messages on agent.end — and acks any seeded task it finds. Only user
// text blocks are parsed; tool results, tool inputs and outputs never are.
//
// Own task from the environment: dispatch.py launches every worker with
// SWITCHBOARD_WORKER_TASK_ID / SWITCHBOARD_WORKER_ATTEMPT_ID in its environment, so the
// worker acks that task on agent.start, tool.result and agent.end without
// finding the seed prompt in the transcript. The Claude hook bridge (one fresh
// process per hook event, no in-memory tracking) cannot rely on the seed: after
// enough tool calls it falls out of the recent messages. Only the launched session binds this way: a Claude row
// names it in SWITCHBOARD_WORKER_SESSION_ID, so a child `claude -p` that inherits the
// env never takes the worker's row; Amp falls back to the UI thread, so a
// native child running in the same process never does either.
//
// Native threads: a `create_thread` tool
// result registers the child as a ledger task (`kind: native-thread`,
// status routing, thread_id = child) so it is visible in the command center
// with the same task/attempt/thread identity as routed work. When the child
// runs on this runner its agent.start acks the task as worker and its
// agent.end finishes it (turn finished ≠ business completion; the summary
// says so). An orb child cannot fire hooks here, so it stays awaiting-ack
// until the accept deadline expires — that is the honest state.
//
// Attempt fencing: ROUTER_ATTEMPT_ID=… (router) or `--attempt-id …` (worker)
// is forwarded when present so a stale attempt can never refresh the lease of
// a newer one. Absent attempt id = legacy/unfenced and accepted by the ledger.
//
// The ledger is the single source of truth (system/state/running-tasks.json).
// A separate expiring foreground-thread proof binds the live plugin process
// to its actual terminal; native event hooks cannot supply this identity. Ledger calls are
// best-effort and never block or fail the turn. Kill switch:
// `task-liveness.disabled = true` in amp config.
//
// Test seams (env): TASK_LIVENESS_LEDGER_SCRIPT, TASK_LIVENESS_CMUX_BIN,
// CMUX_WORKSPACE_ID, SWITCHBOARD_WORKSTREAM_SLUG, SWITCHBOARD_TASK_HEARTBEAT_MS,
// RUNNING_TASKS_FILE (read by the ledger script itself).

import type { AgentEndEvent, AgentStartEvent, PluginAPI, ToolResultEvent } from '@ampcode/plugin'
import { readFile, realpath } from 'node:fs/promises'
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { createHash } from 'node:crypto'
import { dirname, join } from 'node:path'
import { isPluginEnabled, logEvent, SWITCHBOARD_REPO, run, runDetached, writeState } from './switchboard/lib'

const PLUGIN = 'task-liveness'
// Capture once at module load. An old host must not advertise new disk bytes.
export const runtimeRevision = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex')
const TASK_ID_RE = /\brt-[0-9a-f]{12}\b/g
const ATTEMPT_ID_RE = /\brt-[0-9a-f]{12}\.a\d+\.[0-9a-f]+\b/

export type TaskRole = 'router' | 'worker'

/** Attest the UI thread, never the thread that happens to emit a worker hook. */
export function watchActiveSurface(amp: PluginAPI) {
  const workspace = process.env.CMUX_WORKSPACE_ID
  const surface = process.env.CMUX_SURFACE_ID
  if (!workspace || !surface || !amp.activeThread) return
  let writing = false
  const publish = async () => {
    if (writing || process.platform === 'win32') return // MSYS ps has no -o, so this could never write there
    writing = true
    try {
      const stamp = await run('ps', ['-p', String(process.pid), '-o', 'ppid=,lstart='],
        { timeoutMs: 2000, env: { LC_ALL: 'C' } })
      const match = stamp.stdout.trim().match(/^(\d+)\s+(.+)$/)
      if (stamp.code !== 0 || !match || Number(match[1]) !== process.ppid) return
      await writeState('active-surfaces', String(process.ppid), {
        thread_id: amp.activeThread.current?.id ?? null,
        workspace_id: workspace, surface_id: surface,
        amp_pid: process.ppid, publisher_pid: process.pid, publisher_start: match[2],
        runtime_revision: runtimeRevision,
        observed_at: Date.now() / 1000,
      })
    } finally { writing = false }
  }
  amp.activeThread.subscribe(() => { void publish() })
  void publish()
  const timer = setInterval(() => { void publish() }, 5000)
  timer.unref()
}

export interface TrackedTask {
  taskId: string
  attemptId: string
  role: TaskRole
}

/**
 * Windows only. The Claude bridge is one process per hook, so
 * the in-memory throttles here reset on every tool call and each result started the ledger
 * script and git, about 10 processes that a kernel leak keeps until reboot. A stamp file
 * carries the throttle across them: at most one pass per SWITCHBOARD_TASK_RESULT_THROTTLE_MS (2 min).
 */
function windowsResultThrottled(threadID: string): boolean {
  if (process.platform !== 'win32') return false
  const raw = Number(process.env.SWITCHBOARD_TASK_RESULT_THROTTLE_MS ?? 120_000)
  const windowMs = Number.isFinite(raw) && raw >= 0 ? raw : 120_000
  const stamp = join(tmpdir(), `switchboard-liveness-${threadID.replace(/[^A-Za-z0-9-]/g, '_')}.stamp`)
  try {
    if (Date.now() - statSync(stamp).mtimeMs < windowMs) return true
  } catch { /* no stamp yet */ }
  try { writeFileSync(stamp, '') } catch { /* an unwritable stamp only loses the throttle */ }
  return false
}

/** Local hook execution proves a background executor, not terminal ownership. */
export function watchLocalExecutors(amp: PluginAPI) {
  const active = new Set<string>()
  let pending = Promise.resolve()
  const publish = () => {
    if (process.platform === 'win32') return pending // MSYS ps has no -o, so this could never write there
    pending = pending.then(async () => {
      const stamp = await run('ps', ['-p', String(process.pid), '-o', 'ppid=,lstart='],
        { timeoutMs: 2000, env: { LC_ALL: 'C' } })
      const match = stamp.stdout.trim().match(/^(\d+)\s+(.+)$/)
      if (stamp.code !== 0 || !match || Number(match[1]) !== process.ppid) return
      for (const threadID of active) {
        await writeState('local-executors', `${threadID}-${process.pid}`, {
          thread_id: threadID, repo_root: SWITCHBOARD_REPO,
          amp_pid: process.ppid, publisher_pid: process.pid, publisher_start: match[2],
          runtime_revision: runtimeRevision,
          observed_at: Date.now() / 1000,
        })
      }
    }).catch(error => { amp.logger.log(`[task-liveness] executor proof: ${String(error)}`) })
    return pending
  }
  const observe = async (event: { thread: { id: string } }) => {
    if (!/^T-[0-9a-f-]{36}$/.test(event.thread.id)) return
    active.add(event.thread.id)
    await publish()
  }
  amp.on('agent.start', observe)
  // Also establishes proof after plugin reload, before the first local claim.
  amp.on('tool.call', observe)
  amp.on('agent.end', async event => {
    if (!active.delete(event.thread.id)) return
    await pending
    // Tombstone after queued publishers finish; never renew ended turns.
    await writeState('local-executors', `${event.thread.id}-${process.pid}`, {})
  })
  const timer = setInterval(() => { void publish() }, 5000)
  timer.unref()
  return observe
}

/** Pull every task reference out of a seeded prompt. Pure; exported for tests. */
export function parseTaskReferences(message: string): TrackedTask[] {
  const text = message ?? ''
  const found = new Map<string, TrackedTask>()

  // Supplemental-worker seed (busy-owner-dispatch.py): an authoritative marker
  // names exactly one task. Everything else in the prompt (quoted history,
  // the owner's busy task id, other --task-id mentions) is ignored so a
  // parallel worker can never ack or steal the owner's task.
  const workerTask = text.match(/\bSWITCHBOARD_WORKER_TASK_ID=(rt-[0-9a-f]{12})\b/)
  if (workerTask) {
    const attempt = text.match(/\bSWITCHBOARD_WORKER_ATTEMPT_ID=(rt-[0-9a-f]{12}\.a\d+\.[0-9a-f]+)\b/)
    return [{
      taskId: workerTask[1],
      attemptId: attempt && attempt[1].startsWith(workerTask[1]) ? attempt[1] : '',
      role: 'worker',
    }]
  }

  // Router seed: router-dispatch.sh writes `ROUTER_TASK_ID=rt-… ROUTER_ATTEMPT_ID=rt-….a1.abcd`.
  const routerTask = text.match(/\bROUTER_TASK_ID=(rt-[0-9a-f]{12})\b/)
  if (routerTask) {
    const attempt = text.match(/\bROUTER_ATTEMPT_ID=(rt-[0-9a-f]{12}\.a\d+\.[0-9a-f]+)\b/)
    found.set(routerTask[1], {
      taskId: routerTask[1],
      attemptId: attempt && attempt[1].startsWith(routerTask[1]) ? attempt[1] : '',
      role: 'router',
    })
  }

  // Worker seed: the return contract names the task (`Router return contract: task rt-…`)
  // and the report-to-cos command repeats it (`--task-id rt-…`, optionally `--attempt-id …`).
  const contractRe = /\b(?:Router return contract:\s*task|--task-id)\s+(rt-[0-9a-f]{12})\b/g
  for (const match of text.matchAll(contractRe)) {
    const taskId = match[1]
    if (found.has(taskId)) continue
    found.set(taskId, { taskId, attemptId: '', role: 'worker' })
  }
  // Attach explicit attempt ids to their task (prefix match), never to another task.
  const attemptRe = /--attempt-id\s+(rt-[0-9a-f]{12}\.a\d+\.[0-9a-f]+)\b/g
  for (const match of text.matchAll(attemptRe)) {
    const attemptId = match[1]
    const taskId = attemptId.slice(0, 'rt-'.length + 12)
    const tracked = found.get(taskId)
    if (tracked && tracked.role === 'worker' && !tracked.attemptId) tracked.attemptId = attemptId
  }

  return [...found.values()]
}

/** The worker task named by the launch environment, or null. Pure; exported for tests. */
export function ownWorkerTask(env: Record<string, string | undefined>): TrackedTask | null {
  const taskId = (env.SWITCHBOARD_WORKER_TASK_ID ?? '').trim()
  if (!/^rt-[0-9a-f]{12}$/.test(taskId)) return null
  const attempt = (env.SWITCHBOARD_WORKER_ATTEMPT_ID ?? '').trim()
  return { taskId, attemptId: isAttemptId(attempt) && attempt.startsWith(`${taskId}.`) ? attempt : '', role: 'worker' }
}

/** Loose extra check so a bare id inside a machine prompt is still noticed. */
export function looksLikeSeededHandoff(message: string): boolean {
  const text = message ?? ''
  return /\bSWITCHBOARD_WORKER_TASK_ID=/.test(text) || /\bROUTER_TASK_ID=/.test(text) || /Router return contract:/i.test(text) || (/--task-id\s+rt-/.test(text) && TASK_ID_RE.test(text))
}

/** Text blocks of a user message, ignoring tool results. Pure; exported for tests. */
export function userMessageText(message: { role?: string; content?: unknown }): string {
  if (!message || message.role !== 'user' || !Array.isArray(message.content)) return ''
  return (message.content as Array<{ type?: string; text?: string }>)
    .filter(block => block && block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text as string)
    .join('\n')
}

/** Last non-empty assistant text of a turn's messages, or ''. Pure; exported for tests. */
export function finalAssistantText(messages: unknown[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i] as { role?: string; content?: unknown }
    if (message?.role !== 'assistant' || !Array.isArray(message.content)) continue
    const text = (message.content as Array<{ type?: string; text?: string }>)
      .filter(block => block && block.type === 'text' && typeof block.text === 'string')
      .map(block => block.text as string)
      .join('\n')
      .trim()
    if (text) return text
  }
  return ''
}

/** Child thread id from a `create_thread` tool result, or '' when absent. Pure; exported for tests. */
export function createdThreadID(output: unknown): string {
  const text = typeof output === 'string' ? output : JSON.stringify(output ?? '')
  const match = text.match(/\b(T-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/)
  return match ? match[1] : ''
}

/** Deterministic ledger id for a native child thread: the UUID node (last 12 hex). Pure; exported for tests. */
export function nativeTaskID(childThreadID: string): string {
  const hex = childThreadID.replace(/^T-/, '').replace(/-/g, '').toLowerCase()
  return /^[0-9a-f]{32}$/.test(hex) ? `rt-${hex.slice(-12)}` : ''
}

export function isAttemptId(value: string): boolean {
  return ATTEMPT_ID_RE.test(value)
}

interface LedgerResult {
  ok: boolean
  terminal: boolean
  stale: boolean
  fenced: boolean
  mismatch: boolean
  phase: string
  attemptId: string
  detail?: string
}

interface NativeChild {
  taskId: string
  attemptId: string
  parentThreadID: string
  executor: string
  routed?: boolean
}

export default function (amp: PluginAPI) {
  watchActiveSurface(amp)
  const publishExecutor = watchLocalExecutors(amp)
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(value => { config = value })

  const ledgerScript = process.env.TASK_LIVENESS_LEDGER_SCRIPT ?? `${SWITCHBOARD_REPO}/system/scripts/running-task-state.py`
  const cmuxBin = process.env.TASK_LIVENESS_CMUX_BIN ?? process.env.CMUX_BIN ?? '/Applications/cmux.app/Contents/Resources/bin/cmux'
  const workspaceId = process.env.CMUX_WORKSPACE_ID?.trim() ?? ''
  const workstream = process.env.SWITCHBOARD_WORKSTREAM_SLUG?.trim() ?? ''
  const heartbeatMsRaw = Number(process.env.SWITCHBOARD_TASK_HEARTBEAT_MS ?? 60_000)
  const heartbeatMs = Number.isFinite(heartbeatMsRaw) && heartbeatMsRaw >= 0 ? heartbeatMsRaw : 60_000
  const scanMsRaw = Number(process.env.SWITCHBOARD_TASK_SCAN_MS ?? 15_000)
  const scanMs = Number.isFinite(scanMsRaw) && scanMsRaw >= 0 ? scanMsRaw : 15_000

  // thread id → tracked tasks. A thread can carry several queued handoffs.
  const tracked = new Map<string, Map<string, TrackedTask>>()
  // thread id → user message ids already parsed for seeded handoffs.
  const seenMessages = new Map<string, Set<string>>()
  const lastScanAt = new Map<string, number>()
  // child thread id → ledger registration made from a `create_thread` result.
  const nativeChildren = new Map<string, NativeChild>()
  let lastHeartbeatAt = 0
  let workspaceRefCache: { value: string; at: number } | null = null

  const enabled = () => isPluginEnabled(config, PLUGIN)

  const workerWorkspaceRef = async (threadID: string): Promise<string> => {
    const probe = process.env.TASK_LIVENESS_PROBE_SCRIPT ?? `${SWITCHBOARD_REPO}/system/scripts/local_owner_probe.py`
    const result = await run('python3', [probe, '--thread', threadID], { cwd: SWITCHBOARD_REPO, timeoutMs: 15_000 })
    try {
      const session = JSON.parse(result.stdout)
      return result.code === 0 && session.local === true && session.thread_id === threadID
        ? String(session.workspace_ref ?? '') : ''
    } catch { return '' }
  }

  const workspaceRef = async (): Promise<string> => {
    if (!workspaceId) return ''
    if (workspaceRefCache && Date.now() - workspaceRefCache.at < 5 * 60_000) return workspaceRefCache.value
    const result = await run(cmuxBin, ['rpc', 'workspace.list', '{}'], { timeoutMs: 5000 })
    let ref = ''
    if (result.code === 0) {
      try {
        const payload = JSON.parse(result.stdout) as { workspaces?: Array<{ id?: string; ref?: string }> }
        ref = (payload.workspaces ?? []).find(item => item.id === workspaceId)?.ref ?? ''
      } catch { ref = '' }
    }
    workspaceRefCache = { value: ref, at: Date.now() }
    return ref
  }

  const callLedger = async (args: string[]): Promise<LedgerResult> => {
    const result = await run('python3', [ledgerScript, ...args], { cwd: SWITCHBOARD_REPO, timeoutMs: 15_000 })
    const failure = (detail: string): LedgerResult => ({ ok: false, terminal: false, stale: false, fenced: false, mismatch: false, phase: 'unknown', attemptId: '', detail })
    if (result.code !== 0) return failure((result.stderr || result.stdout).trim().slice(0, 300))
    try {
      const task = JSON.parse(result.stdout) as Record<string, unknown>
      return {
        ok: true,
        terminal: Boolean(task._terminal),
        stale: Boolean(task._stale_attempt),
        fenced: Boolean(task._fenced_executor),
        mismatch: Boolean(task._workstream_mismatch),
        phase: String(task.phase ?? 'unknown'),
        attemptId: String(task.attempt_id ?? ''),
      }
    } catch {
      return failure('unparseable ledger output')
    }
  }

  const accepted = (result: LedgerResult) => result.ok && !result.terminal && !result.stale && !result.fenced && !result.mismatch
  const rejection = (result: LedgerResult) =>
    result.terminal ? 'terminal' : result.stale ? 'stale attempt' : result.fenced ? 'executor fenced' : result.mismatch ? 'workstream mismatch' : result.detail ?? 'error'

  const track = (threadID: string, task: TrackedTask) => {
    const byTask = tracked.get(threadID) ?? new Map<string, TrackedTask>()
    byTask.set(task.taskId, task)
    tracked.set(threadID, byTask)
  }
  const isTracked = (threadID: string, taskId: string) => Boolean(tracked.get(threadID)?.has(taskId))

  const ackTask = async (threadID: string, task: TrackedTask, logger: { log: (...a: unknown[]) => unknown }, options: { provenance?: boolean } = {}) => {
    // Worker hooks can inherit the spawning surface's environment. Resolve
    // by the executing thread even when this plugin did not observe creation.
    const provenance = options.provenance ?? true
    if (task.role === 'worker' && task.attemptId && amp.activeThread?.current
        && amp.activeThread.current.id !== threadID) {
      // create_thread can be nested inside code_exec, invisible to the parent's
      // tool-name matcher. The child's local hook establishes its own binding.
      // Bundle handlers run concurrently, so explicitly await the proof first.
      await publishExecutor({ thread: { id: threadID } })
      const registered = await callLedger(['register-native', '--task-id', task.taskId,
        '--attempt-id', task.attemptId, '--thread-id', threadID, '--executor', 'runner', '--local-claim'])
      if (!accepted(registered)) return registered
    }
    const ref = task.role === 'worker' ? await workerWorkspaceRef(threadID) : await workspaceRef()
    const args = ['ack', '--task-id', task.taskId, '--role', task.role, '--thread-id', threadID]
    if (ref) args.push('--workspace-ref', ref)
    if (provenance && workstream && task.role === 'worker') args.push('--workstream', workstream)
    if (task.attemptId) args.push('--attempt-id', task.attemptId)
    const result = await callLedger(args)
    await logEvent(PLUGIN, { event: 'ack', task_id: task.taskId, attempt_id: task.attemptId || null, role: task.role, thread_id: threadID, workspace_ref: ref || null, ...result })
    logger.log(`[${PLUGIN}] ack ${task.taskId} (${task.role}) → ${result.ok ? (accepted(result) ? result.phase : rejection(result)) : `error: ${result.detail ?? ''}`}`)
    if (accepted(result)) track(threadID, task)
    return result
  }

  /** Ack every seeded task referenced by a user text that has not been tracked yet. */
  const ackFromText = async (threadID: string, text: string, logger: { log: (...a: unknown[]) => unknown }, source: string) => {
    // The launcher sends a file pointer, not the handoff body. Only dereference
    // that exact user instruction inside the launch directory. Tool output and
    // quoted paths must never become receipts for another task.
    const launchDir = process.env.TASK_LIVENESS_LAUNCH_PROMPT_DIR ?? join(SWITCHBOARD_REPO, 'system/state/launch-prompts')
    const pointer = text.trim().match(/^Read and follow all instructions in ([^\n]+\.md)$/)
    if (pointer && dirname(pointer[1]) === launchDir) {
      try {
        const path = await realpath(pointer[1])
        if (dirname(path) !== await realpath(launchDir)) return
        const seed = await readFile(path, 'utf8')
        const contract = seed.lastIndexOf('Router return contract: task ')
        if (contract < 0) return
        text = seed.slice(contract)
      } catch (error) {
        await logEvent(PLUGIN, { event: 'launch-seed-error', thread_id: threadID, detail: String(error).slice(0, 200) })
        return
      }
    }
    if (!looksLikeSeededHandoff(text)) return
    // A worker process knows its own task from the launch command's environment.
    // A worker-shaped seed for any other task is quoted text (a compaction
    // summary, a pasted launch command from a stuck tab) and must never bind
    // this thread to that row.
    const ownTask = process.env.SWITCHBOARD_WORKER_TASK_ID ?? ''
    for (const task of parseTaskReferences(text)) {
      if (isTracked(threadID, task.taskId)) continue
      if (ownTask && task.role === 'worker' && task.taskId !== ownTask) {
        await logEvent(PLUGIN, { event: 'foreign-worker-seed-ignored', task_id: task.taskId, own_task_id: ownTask, thread_id: threadID, source })
        continue
      }
      await logEvent(PLUGIN, { event: 'seed-detected', task_id: task.taskId, thread_id: threadID, source })
      await ackTask(threadID, task, logger)
    }
  }

  /** Ack this process's own worker task from its launch environment; no seed prompt needed. */
  let ownTaskRejected = ''
  const ackOwnTask = async (threadID: string, logger: { log: (...a: unknown[]) => unknown }) => {
    const own = ownWorkerTask(process.env)
    if (!own || ownTaskRejected === own.taskId || isTracked(threadID, own.taskId)) return
    // A Claude launch names its session; Amp falls back to the UI thread. Any
    // other thread or child process that inherits the env is not the worker.
    const session = (process.env.SWITCHBOARD_WORKER_SESSION_ID ?? '').trim()
    if (session ? threadID !== session : amp.activeThread?.current?.id !== threadID) return
    const result = await ackTask(threadID, own, logger)
    // Terminal, stale or fenced is final for this attempt: stop re-acking.
    if (result.ok && !accepted(result)) ownTaskRejected = own.taskId
  }

  const markSeen = (threadID: string, messageId: unknown): boolean => {
    const id = String(messageId ?? '')
    if (!id) return true
    const seen = seenMessages.get(threadID) ?? new Set<string>()
    seenMessages.set(threadID, seen)
    if (seen.has(id)) return false
    seen.add(id)
    return true
  }

  type ThreadCtx = { thread: { id: string; messages?: (options: Record<string, unknown>) => Promise<unknown> }; logger: { log: (...a: unknown[]) => unknown } }

  /** Late ack: read the thread's recent user messages (steered handoffs never fire agent.start). */
  const scanThread = async (threadID: string, ctx: ThreadCtx, reason: string) => {
    if (typeof ctx.thread?.messages !== 'function') return
    const now = Date.now()
    if (now - (lastScanAt.get(threadID) ?? 0) < scanMs) return
    lastScanAt.set(threadID, now)
    let messages: unknown
    try {
      messages = await ctx.thread.messages({ roles: ['user'], from: 'end', limit: 20 })
    } catch (error) {
      await logEvent(PLUGIN, { event: 'scan-error', thread_id: threadID, reason, detail: String(error).slice(0, 200) })
      return
    }
    if (!Array.isArray(messages)) return
    for (const message of messages as Array<{ role?: string; id?: unknown; content?: unknown }>) {
      if (!markSeen(threadID, message?.id)) continue
      await ackFromText(threadID, userMessageText(message), ctx.logger, `scan:${reason}`)
    }
  }

  const registerNativeChild = async (parentThreadID: string, input: Record<string, unknown>, output: unknown, logger: { log: (...a: unknown[]) => unknown }) => {
    const childID = createdThreadID(output)
    const references = parseTaskReferences(String(input?.prompt ?? ''))
    const routed = references.length === 1 && references[0].role === 'worker' ? references[0] : undefined
    const taskId = routed?.taskId ?? nativeTaskID(childID)
    if (!childID || !taskId) {
      await logEvent(PLUGIN, { event: 'native-thread-unregistered', parent_thread_id: parentThreadID, reason: 'no child thread id in create_thread output' })
      return
    }
    if (nativeChildren.has(childID)) return
    const executor = String(input?.executor ?? 'orb')
    if (routed) {
      // Rung four continues the existing attempt. Never invent a second task,
      // attempt, or receipt, and never borrow the Router's pinned workspace.
      if (!routed.attemptId) {
        logger.log(`[${PLUGIN}] native fallback ${childID} needs an explicit attempt ID; check get_thread_status before retrying`)
        return
      }
      const args = ['register-native', '--task-id', taskId,
        '--attempt-id', routed.attemptId, '--thread-id', childID, '--executor', executor]
      const childRef = executor === 'runner' ? await workerWorkspaceRef(childID) : ''
      if (childRef) args.push('--workspace-ref', childRef)
      const registered = await callLedger(args)
      if (accepted(registered)) {
        nativeChildren.set(childID, { taskId, attemptId: routed.attemptId, parentThreadID, executor, routed: true })
      }
      await logEvent(PLUGIN, { event: 'native-fallback-registered', task_id: taskId, child_thread_id: childID, ...registered })
      logger.log(`[${PLUGIN}] native fallback ${childID}: ${accepted(registered) ? 'registered on existing attempt' : 'UNKNOWN OUTCOME, manual check needed: get_thread_status; ' + rejection(registered)}`)
      return
    }
    const title = String(input?.title ?? '').trim()
    const prompt = String(input?.prompt ?? '').replace(/\s+/g, ' ').trim()
    const head = (title ? `${title}: ${prompt}` : prompt).slice(0, 400) || `native thread ${childID}`
    const ref = await workspaceRef()
    const started = await callLedger([
      'start', '--task-id', taskId, '--router', `native:${parentThreadID}`,
      '--workspace-ref', ref || 'native-thread', '--prompt', head, '--origin', 'cos',
    ])
    if (!started.ok) {
      // The ledger's live-attempt fence refuses a second `start` while attempt 1
      // is still awaiting-ack/live. A repeated create_thread result for the same
      // child (plugin reload, replayed tool result) must adopt that record, not
      // register a second attempt and not drop tracking of the child.
      const existing = await run('python3', [ledgerScript, 'show', '--task-id', taskId], { cwd: SWITCHBOARD_REPO, timeoutMs: 15_000 })
      let adopted: Record<string, unknown> | null = null
      try { adopted = existing.code === 0 ? (JSON.parse(existing.stdout) as Record<string, unknown>) : null } catch { adopted = null }
      if (adopted && String(adopted.thread_id ?? '') === childID && !adopted._terminal && ['routing', 'active'].includes(String(adopted.status ?? ''))) {
        nativeChildren.set(childID, { taskId, attemptId: String(adopted.attempt_id ?? ''), parentThreadID, executor })
        await logEvent(PLUGIN, { event: 'native-thread-adopted', parent_thread_id: parentThreadID, child_thread_id: childID, task_id: taskId, attempt_id: adopted.attempt_id ?? null, executor })
        logger.log(`[${PLUGIN}] native thread ${childID} already registered as ${taskId}; adopted attempt ${String(adopted.attempt ?? '?')}`)
        return
      }
      await logEvent(PLUGIN, { event: 'native-thread-unregistered', parent_thread_id: parentThreadID, child_thread_id: childID, task_id: taskId, reason: started.detail ?? 'ledger start failed' })
      logger.log(`[${PLUGIN}] native thread ${childID} not registered: ${started.detail ?? 'ledger start failed'}`)
      return
    }
    const liveness = executor === 'runner'
      ? 'awaiting agent.start on this runner'
      : `executor ${executor}: liveness not observable from this runner; expires at the accept deadline unless acked`
    const summary = `Native thread ${childID} created by ${parentThreadID} (${executor}); ${liveness}. No cmux workspace.`
    const updateArgs = ['update', '--task-id', taskId, '--status', 'routing', '--kind', 'native-thread', '--thread-id', childID, '--summary', summary, '--evidence', `https://ampcode.com/threads/${childID}`]
    if (ref) updateArgs.push('--source-workspace', ref)
    if (started.attemptId) updateArgs.push('--attempt-id', started.attemptId)
    const updated = await callLedger(updateArgs)
    const bindArgs = ['register-native', '--task-id', taskId, '--attempt-id', started.attemptId,
      '--thread-id', childID, '--executor', executor]
    const childRef = executor === 'runner' ? await workerWorkspaceRef(childID) : ''
    if (childRef) bindArgs.push('--workspace-ref', childRef)
    await callLedger(bindArgs)
    nativeChildren.set(childID, { taskId, attemptId: started.attemptId, parentThreadID, executor })
    await logEvent(PLUGIN, { event: 'native-thread-registered', parent_thread_id: parentThreadID, child_thread_id: childID, task_id: taskId, attempt_id: started.attemptId || null, executor, workspace_ref: ref || null, update_ok: updated.ok })
    logger.log(`[${PLUGIN}] native thread ${childID} registered as ${taskId} (${executor})`)
  }

  const finishNativeChild = async (childID: string, status: string, logger: { log: (...a: unknown[]) => unknown }) => {
    const child = nativeChildren.get(childID)
    if (!child || child.routed) return // Routed work finishes via its report-to-cos contract, not turn end.
    const terminal = status === 'done' ? 'done' : status === 'error' ? 'failed' : status === 'cancelled' ? 'cancelled' : ''
    if (!terminal) return
    const summary = terminal === 'done'
      ? `Native thread ${childID} finished its turn; the result lives in the thread (turn end ≠ verified business completion).`
      : `Native thread ${childID} turn ended with ${status}; inspect the thread.`
    const args = ['update', '--task-id', child.taskId, '--status', terminal, '--thread-id', childID, '--summary', summary, '--evidence', `https://ampcode.com/threads/${childID}`]
    if (child.attemptId) args.push('--attempt-id', child.attemptId)
    const result = await callLedger(args)
    nativeChildren.delete(childID)
    tracked.delete(childID)
    await logEvent(PLUGIN, { event: 'native-thread-finished', child_thread_id: childID, task_id: child.taskId, status: terminal, ...result })
    logger.log(`[${PLUGIN}] native thread ${childID} → ${terminal}`)
  }

  const heartbeatAll = async (threadID: string, reason: string, logger?: { log: (...a: unknown[]) => unknown }) => {
    const byTask = tracked.get(threadID)
    if (!byTask || byTask.size === 0) return
    for (const task of [...byTask.values()]) {
      const args = ['heartbeat', '--task-id', task.taskId, '--role', task.role, '--thread-id', threadID]
      if (task.attemptId) args.push('--attempt-id', task.attemptId)
      const result = await callLedger(args)
      if (!result.ok || result.terminal || result.stale || result.fenced) {
        // fenced: a worker thread now owns this task's executor binding; a
        // router (or any other thread) must stop heartbeating it.
        byTask.delete(task.taskId)
        await logEvent(PLUGIN, { event: 'untrack', task_id: task.taskId, thread_id: threadID, reason, ...result })
        logger?.log(`[${PLUGIN}] untrack ${task.taskId} (${result.terminal ? 'terminal' : result.stale ? 'stale attempt' : result.fenced ? 'executor fenced' : result.detail ?? 'error'})`)
      }
    }
    lastHeartbeatAt = Date.now()
    if (byTask.size === 0) tracked.delete(threadID)
  }

  amp.on('agent.start', async (event: AgentStartEvent, ctx) => {
    if (!enabled()) return
    const threadID = String(event.thread?.id ?? ctx.thread.id)
    markSeen(threadID, event.id)
    // A native child booting on this runner: ack it as the worker of its
    // registered task (no cmux/workstream provenance — it has none).
    const child = nativeChildren.get(threadID)
    if (child && !isTracked(threadID, child.taskId)) {
      await ackTask(threadID, { taskId: child.taskId, attemptId: child.attemptId, role: 'worker' }, ctx.logger, { provenance: false })
    }
    await ackOwnTask(threadID, ctx.logger)
    await ackFromText(threadID, String(event.message ?? ''), ctx.logger, 'agent.start')
  })

  amp.on('tool.result', async (event: ToolResultEvent, ctx) => {
    if (!enabled()) return
    const threadID = String(event.thread?.id ?? ctx.thread.id)
    if (event.tool === 'create_thread' && event.status === 'done') {
      await registerNativeChild(threadID, (event.input ?? {}) as Record<string, unknown>, event.output, ctx.logger)
    }
    if (windowsResultThrottled(threadID)) return
    await ackOwnTask(threadID, ctx.logger)
    // Late ack for handoffs steered into a running boot turn.
    await scanThread(threadID, ctx as unknown as ThreadCtx, 'tool.result')
    if (tracked.size === 0) return
    if (Date.now() - lastHeartbeatAt < heartbeatMs) return
    for (const id of [...tracked.keys()]) {
      await heartbeatAll(id, 'tool.result', ctx.logger)
    }
  })

  amp.on('agent.end', async (event: AgentEndEvent, ctx) => {
    if (!enabled()) return
    const threadID = String(event.thread?.id ?? ctx.thread.id)
    // Steered user messages of this turn arrive here without an agent.start.
    await ackOwnTask(threadID, ctx.logger)
    const turnMessages = Array.isArray(event.messages) ? event.messages : []
    for (const message of turnMessages as Array<{ role?: string; id?: unknown; content?: unknown }>) {
      if (message?.role !== 'user') continue
      if (!markSeen(threadID, message.id)) continue
      await ackFromText(threadID, userMessageText(message), ctx.logger, 'agent.end')
    }
    await heartbeatAll(threadID, `agent.end:${String(event.status ?? '')}`, ctx.logger)
    await finishNativeChild(threadID, String(event.status ?? ''), ctx.logger)
    await parkIfWaiting(threadID, turnMessages, String(event.status ?? ''))
  })

  // A turn that ends waiting on the owner (a question, an approval, a ruling) while
  // its dispatch row is still live reaches the row's origin once, directly.
  // turn_wait.py owns the classifier, the row lookup and the
  // one-per-text dedupe; this only hands it the final text, detached, so the
  // turn never waits on it. Kill switch: task-liveness.park.disabled.
  const parkIfWaiting = async (threadID: string, turnMessages: unknown[], status: string) => {
    if (status !== 'done' || config['task-liveness.park.disabled'] === true || !threadID) return
    const text = finalAssistantText(turnMessages)
    if (!text) return
    const dir = process.env.TURN_WAIT_TEXT_DIR ?? join(SWITCHBOARD_REPO, 'system/state/worker-dispatch/turn-wait')
    const file = join(dir, `${threadID.replace(/[^A-Za-z0-9-]/g, '_')}-${Date.now()}.txt`)
    try {
      mkdirSync(dir, { recursive: true })
      writeFileSync(file, text)
    } catch (error) {
      await logEvent(PLUGIN, { event: 'park-text-write-failed', thread_id: threadID, detail: String(error).slice(0, 200) })
      return
    }
    const script = process.env.TURN_WAIT_SCRIPT ?? join(SWITCHBOARD_REPO, 'system/scripts/turn_wait.py')
    const started = await runDetached('python3', [script, 'park', '--thread-id', threadID, '--text-file', file, '--consume'], {
      cwd: SWITCHBOARD_REPO,
      logFile: join(SWITCHBOARD_REPO, 'system/state/logs/turn-wait.log'),
      label: `${PLUGIN} park ${threadID}`,
    })
    if (!started) await logEvent(PLUGIN, { event: 'park-launch-failed', thread_id: threadID })
  }

  // Exposed for the test harness / debugging only.
  amp.registerCommand(
    'switchboard-task-liveness-status',
    { title: 'Task liveness: tracked tasks', category: 'switchboard', description: 'Show router tasks this session is heartbeating' },
    async (ctx) => {
      const lines: string[] = []
      for (const [threadID, byTask] of tracked) {
        for (const task of byTask.values()) lines.push(`${threadID} ${task.role} ${task.taskId} ${task.attemptId || '(unfenced)'}`)
      }
      await ctx.ui.notify(lines.length ? lines.join('\n') : `${PLUGIN}: no tracked tasks in this session`)
    },
  )
}
