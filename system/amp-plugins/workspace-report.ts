// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// Mirror completed human-initiated workstream turns into the reporting hub.
// Machine-seeded turns are excluded, and report-to-cos.sh keeps callbacks durable.

import type { AgentEndEvent, AgentStartEvent, PluginAPI, ThreadMessage } from '@ampcode/plugin'
import { createHash } from 'node:crypto'
import { appendFileSync, mkdirSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { isHeadless, isMachinePrompt, isPluginEnabled, SWITCHBOARD_REPO, run, runDetached } from './switchboard/lib'

const PLUGIN = 'workspace-report'
const GUARDED_SEND_TTL_MS = 5 * 60 * 1000

interface WorkspaceInfo {
  id: string
  ref: string
  title: string
  slug: string
}

interface PendingHandoff {
  id: string
  message: string
  delivery_kind?: string | null
  thread_id?: string | null
  workspace_uuid?: string | null
}

interface WorkspaceResolution {
  workspace: WorkspaceInfo | null
  reason?: string
  detail?: string
}

interface PendingMirror {
  workspace: WorkspaceInfo
  threadID: string
  prompt: string
}

function normalized(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function cleanTitle(title: string): string {
  return title.replace(/^[^\p{L}\p{N}]+/u, '').trim()
}

function excludedSurfaceTitles(): Set<string> {
  try {
    const path = process.env.SURFACES_CONFIG_FILE ?? join(SWITCHBOARD_REPO, 'system/config/surfaces.json')
    const payload = JSON.parse(readFileSync(path, 'utf8')) as {
      surfaces?: Record<string, { title?: string; workspace_reports?: boolean }>
    }
    return new Set(Object.values(payload.surfaces ?? {})
      .filter(surface => surface.workspace_reports === false)
      .map(surface => cleanTitle(surface.title ?? ''))
      .filter(Boolean))
  } catch {
    return new Set(['CoS', 'Launchpad'])
  }
}

const EXCLUDED_SURFACE_TITLES = excludedSurfaceTitles()

function isExcludedWorkspace(title: string): boolean {
  const clean = cleanTitle(title)
  return EXCLUDED_SURFACE_TITLES.has(clean) || /^Router \d+\b/.test(clean)
}

function yamlScalar(config: string, key: string): string {
  const match = config.match(new RegExp(`^${key}:\\s*(.+?)\\s*$`, 'm'))
  if (!match) return ''
  return match[1].replace(/^(["'])(.*)\1$/, '$2').trim()
}

function comparableTitle(title: string): string {
  return normalized(cleanTitle(title).replace(/^P0\s*·\s*/i, '')).toLowerCase()
}

function workstreamTitleMap(): Map<string, string> {
  const aliases = new Map<string, string>()
  const workstreamsRoot = join(SWITCHBOARD_REPO, 'workstreams')
  const roots = [join(workstreamsRoot, '_archive'), workstreamsRoot]
  for (const root of roots) {
    let entries: Array<{ name: string; isDirectory(): boolean }> = []
    try { entries = readdirSync(root, { withFileTypes: true }) } catch { continue }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      let config = ''
      try { config = readFileSync(join(root, entry.name, 'config.yaml'), 'utf8') } catch { continue }
      for (const alias of [entry.name, yamlScalar(config, 'name'), yamlScalar(config, 'title')]) {
        const key = comparableTitle(alias)
        if (key) aliases.set(key, entry.name)
      }
    }
  }
  return aliases
}

// Shared with auto-pilot-router; re-exported for callers of this module.
export { isMachinePrompt }

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
}

function cleanMarkdown(text: string): string {
  return normalized(
    text
      .replace(/\[([^\]]+)\]\([^\)]+\)/g, '$1')
      .replace(/[*_`~]/g, '')
      .replace(/^#{1,6}\s+/, '')
      .replace(/^[-+•]\s+/, ''),
  )
}

export function compactOutcome(text: string, maxChars = 220): string {
  // Extract targets from the whole final, BEFORE choosing or clipping prose.
  // report-to-cos normalizes these protected links and adds the vault path.
  const links = new Map<string, string>()
  text = text.replace(/\[([^\]\n]+)\]\(((?:https?|(?:switchboard-)?obsidian):\/\/[^\s)]+)\)/g, (_, label, url) => {
    links.set(url, label)
    return ''
  }).replace(/(?:https?|(?:switchboard-)?obsidian):\/\/[^\s)>\]"']+/g, raw => {
    const url = raw.replace(/[.,;:]+$/, '')
    if (!links.has(url)) links.set(url, 'Open link')
    return ''
  })
  const finish = (outcome: string): string => {
    const prose = outcome.length > maxChars ? `${outcome.slice(0, maxChars - 1).trimEnd()}…` : outcome
    return [prose, ...Array.from(links, ([url, label]) =>
      `[${/^(?:switchboard-)?obsidian:\/\//.test(url) ? 'Open in Obsidian' : label}](${url})`,
    )].join(' ⏎ ')
  }
  const lines = text.split('\n').map(line => line.trim())
  const headerIndex = lines.findIndex(line => /^\|/.test(line) && /What'?s going on/i.test(line))
  if (headerIndex >= 0) {
    for (let index = headerIndex + 1; index < lines.length; index++) {
      if (!/^\|/.test(lines[index]) || /^\|?\s*:?-{3}/.test(lines[index])) continue
      const cells = tableCells(lines[index])
      const outcome = cleanMarkdown(cells[1] ?? '')
      if (outcome) return finish(outcome)
    }
  }

  const candidate = lines.find(line =>
    line
    && !/^```/.test(line)
    && !/^\|/.test(line)
    && !/^(?:Signal|Next action|Owner|ETA)\b/i.test(line),
  ) ?? ''
  const outcome = cleanMarkdown(candidate) || 'The workstream turn finished.'
  return finish(outcome)
}

function finalAssistantText(messages: ThreadMessage[]): string {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex--) {
    const message = messages[messageIndex]
    if (message.role !== 'assistant') continue
    for (let blockIndex = message.content.length - 1; blockIndex >= 0; blockIndex--) {
      const block = message.content[blockIndex]
      if (block.type === 'text' && block.text.trim()) return block.text.trim()
    }
  }
  return ''
}

function userMessageText(message: ThreadMessage): string {
  if (message.role !== 'user') return ''
  return message.content
    .filter(block => block.type === 'text')
    .map(block => block.text)
    .join('\n')
}

function assistantTextForPrompt(messages: ThreadMessage[], prompt: string): string {
  const expected = normalized(prompt)
  for (let index = messages.length - 1; index >= 0; index--) {
    if (normalized(userMessageText(messages[index])) !== expected) continue
    const turnMessages: ThreadMessage[] = []
    for (let next = index + 1; next < messages.length; next++) {
      if (messages[next].role === 'user') break
      turnMessages.push(messages[next])
    }
    return finalAssistantText(turnMessages)
  }
  return ''
}

// A human-typed owner-tab turn that ends waiting on the owner becomes a needs-ruling
// callback, unless a live dispatch row is bound to the thread: task-liveness
// park sends that ask already (turn_wait.py "bound"), so one parked turn is one
// callback. Any classifier failure reads as not waiting (the compact mirror).
export async function waitsOnOwner(text: string, threadID: string,
                                  classify: (args: string[]) => Promise<{ code: number; stdout: string }>): Promise<boolean> {
  if (process.env.TURN_WAIT_DISABLED === '1') return false
  const result = await classify(['classify', '--text', text, '--thread-id', threadID])
  try {
    const verdict = JSON.parse(result.stdout)
    return result.code === 0 && verdict.waiting === true && verdict.bound !== true
  } catch { return false }
}

export default function (amp: PluginAPI) {
  // Supplemental (busy-owner) worker sessions report through the task ledger
  // callbacks (report-to-cos + task-liveness) only. Registering here would let
  // the timer drain the owner's pendingHandoffThreadIDs and mirror this
  // worker's turn to the default CoS surface, breaking callback origin.
  if (process.env.SWITCHBOARD_WORKER_ID) return

  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(value => { config = value })

  const workspaceId = process.env.CMUX_WORKSPACE_ID?.trim() ?? ''
  const configuredWorkstream = process.env.SWITCHBOARD_WORKSTREAM_SLUG?.trim() ?? ''
  const titleToWorkstream = workstreamTitleMap()
  const reportScript = process.env.WORKSPACE_REPORT_SCRIPT ?? `${SWITCHBOARD_REPO}/system/scripts/report-to-cos.sh`
  const pendingStateScript = process.env.PENDING_HANDOFF_STATE_SCRIPT ?? `${SWITCHBOARD_REPO}/system/scripts/pending-handoff-state.py`
  const pendingDir = process.env.PENDING_HANDOFFS_DIR ?? `${SWITCHBOARD_REPO}/system/state/pending-handoffs`
  const sendLog = process.env.WORKSPACE_REPORT_SEND_LOG ?? `${SWITCHBOARD_REPO}/system/logs/cmux-sends.log`
  const logFile = process.env.WORKSPACE_REPORT_LOG_FILE ?? `${SWITCHBOARD_REPO}/system/state/logs/workspace-report.log`
  const mirrorableTurns = new Map<string, PendingMirror>()
  const reportedTurns = new Set<string>()
  const mirroringTurns = new Set<string>()
  const deliveringHandoffs = new Set<string>()
  const knownThreadIDs = new Set<string>()
  const loggedDecisions = new Set<string>()

  const logDecision = (
    reason: string,
    details: Record<string, unknown> = {},
    logger?: { log: (...args: unknown[]) => unknown },
  ) => {
    const key = `${String(details.turn_id ?? 'session')}:${String(details.phase ?? '')}:${reason}`
    if (loggedDecisions.has(key)) return
    loggedDecisions.add(key)
    const record = {
      timestamp: new Date().toISOString(),
      plugin: PLUGIN,
      reason,
      workspace_id: workspaceId || null,
      ...details,
    }
    try {
      mkdirSync(dirname(logFile), { recursive: true })
      appendFileSync(logFile, `${JSON.stringify(record)}\n`)
    } catch {
      // The Amp logger is the fallback when the durable log is unavailable.
    }
    logger?.log(`[${PLUGIN}] ${reason} ${JSON.stringify(details)}`)
  }

  const workspaceInfo = async (): Promise<WorkspaceResolution> => {
    if (!workspaceId) return { workspace: null, reason: 'workspace-id-missing' }
    const cmux = process.env.WORKSPACE_REPORT_CMUX_BIN
      ?? process.env.CMUX_BIN
      ?? '/Applications/cmux.app/Contents/Resources/bin/cmux'
    const result = await run(cmux, ['rpc', 'workspace.list', '{}'], { timeoutMs: 5000 })
    if (result.code !== 0) {
      return {
        workspace: null,
        reason: 'workspace-list-command-failed',
        detail: `exit=${result.code} ${normalized(result.stderr).slice(0, 300)}`.trim(),
      }
    }
    try {
      const payload = JSON.parse(result.stdout) as {
        workspaces?: Array<{ id?: string; ref?: string; title?: string; custom_title?: string }>
      }
      const workspace = (payload.workspaces ?? []).find(item => item.id === workspaceId)
      if (!workspace) return { workspace: null, reason: 'workspace-not-found' }
      if (!workspace.ref) return { workspace: null, reason: 'workspace-ref-missing' }
      const title = workspace.custom_title ?? workspace.title ?? configuredWorkstream
      const slug = configuredWorkstream || titleToWorkstream.get(comparableTitle(title)) || ''
      if (!slug) return { workspace: null, reason: 'workstream-slug-unresolved', detail: title }
      return {
        workspace: {
          id: workspaceId,
          ref: workspace.ref,
          title,
          slug,
        },
      }
    } catch (error) {
      return {
        workspace: null,
        reason: 'workspace-list-invalid-json',
        detail: error instanceof Error ? error.message : String(error),
      }
    }
  }

  const matchesRecentGuardedSend = (message: string, workspaceRef: string): boolean => {
    let content = ''
    try { content = readFileSync(sendLog, 'utf8').slice(-100_000) } catch { return false }
    const prompt = normalized(message)
    for (const line of content.trim().split('\n').reverse()) {
      const fields = line.split(' | ')
      if (fields.length < 5 || fields[3] !== workspaceRef) continue
      const sentAt = Date.parse(fields[0])
      if (!Number.isFinite(sentAt) || Date.now() - sentAt < 0 || Date.now() - sentAt > GUARDED_SEND_TTL_MS) continue
      const prefix = normalized(fields.slice(4).join(' | '))
      if (prefix.length >= 20 && prompt.startsWith(prefix)) return true
    }
    return false
  }

  const isMirrorable = async (message: string): Promise<WorkspaceResolution> => {
    if (!isPluginEnabled(config, PLUGIN)) return { workspace: null, reason: 'plugin-disabled' }
    if (isHeadless()) return { workspace: null, reason: 'headless-session' }
    if (isMachinePrompt(message)) return { workspace: null, reason: 'machine-prompt' }
    const resolution = await workspaceInfo()
    const workspace = resolution.workspace
    if (!workspace) return resolution
    if (isExcludedWorkspace(workspace.title)) return { workspace: null, reason: 'excluded-workspace', detail: workspace.title }
    if (matchesRecentGuardedSend(message, workspace.ref)) return { workspace: null, reason: 'guarded-send' }
    return { workspace }
  }

  const markHandoffDelivered = async (handoffID: string, detail: string) => {
    const result = await run('python3', [
      pendingStateScript,
      'mark',
      '--handoff-id', handoffID,
      '--status', 'delivered',
      '--detail', detail,
    ], { cwd: SWITCHBOARD_REPO, timeoutMs: 10_000 })
    return result.code === 0
  }

  const pendingHandoffs = (workspace: WorkspaceInfo | null, threadID: string): PendingHandoff[] => {
    let files: string[] = []
    try { files = readdirSync(pendingDir).filter(file => /^handoff-.*\.json$/.test(file)).sort() } catch { return [] }
    const records: Array<PendingHandoff & { created_at?: string }> = []
    for (const file of files) {
      try {
        const record = JSON.parse(readFileSync(join(pendingDir, file), 'utf8')) as Record<string, unknown>
        const expectedThread = String(record.thread_id ?? '')
        const expectedWorkspace = String(record.workspace_uuid ?? '')
        if (record.status !== 'pending') continue
        const exactUserResponse = record.delivery_kind === 'user-response'
        if (exactUserResponse) {
          if (!expectedThread || expectedThread !== threadID) continue
        } else {
          if (!workspace || record.workstream !== workspace.slug) continue
          if (expectedThread ? expectedThread !== threadID : expectedWorkspace !== workspace.id) continue
        }
        const id = String(record.id ?? '')
        const message = String(record.message ?? '').trim()
        if (!id || !message) continue
        records.push({
          id,
          message,
          delivery_kind: String(record.delivery_kind ?? '') || null,
          thread_id: expectedThread || null,
          workspace_uuid: expectedWorkspace || null,
          created_at: String(record.created_at ?? ''),
        })
      } catch {
        // A malformed record must not block other queued handoffs.
      }
    }
    return records
      .sort((left, right) => String(left.created_at).localeCompare(String(right.created_at)))
      .map(({ created_at: _createdAt, ...record }) => record)
  }

  const pendingHandoffThreadIDs = (workspace: WorkspaceInfo): string[] => {
    let files: string[] = []
    try { files = readdirSync(pendingDir).filter(file => /^handoff-.*\.json$/.test(file)) } catch { return [] }
    const threadIDs = new Set<string>()
    for (const file of files) {
      try {
        const record = JSON.parse(readFileSync(join(pendingDir, file), 'utf8')) as Record<string, unknown>
        const threadID = String(record.thread_id ?? '')
        if (
          record.status === 'pending'
          && record.workstream === workspace.slug
          && /^T-[A-Za-z0-9-]+$/.test(threadID)
        ) {
          threadIDs.add(threadID)
        }
      } catch {
        // A malformed record must not block other queued handoffs.
      }
    }
    return [...threadIDs]
  }

  const deliverPendingHandoffs = async (workspace: WorkspaceInfo | null, threadID: string) => {
    if (process.env.WORKSPACE_REPORT_PENDING_RETRY === '0') return
    if (deliveringHandoffs.has(threadID)) return
    deliveringHandoffs.add(threadID)
    try {
      const pending = pendingHandoffs(workspace, threadID)
      if (!pending.length) return
      const directResponses = pending.filter(item => item.delivery_kind === 'user-response')
      const deliverable = directResponses.length ? directResponses.slice(0, 1) : pending

      const thread = (() => {
        try { return amp.threads.get(threadID as `T-${string}`) } catch { return null }
      })()
      if (!thread) return
      if (directResponses.length) {
        try {
          if ((await thread.state.get()) !== 'idle') return
        } catch {
          return
        }
      }
      let recent: ThreadMessage[] = []
      try { recent = await thread.messages({ full: true, from: 'end', limit: 20, roles: ['user'] }) } catch { /* append can still proceed */ }
      const recentText = recent.map(userMessageText)
      const undelivered: PendingHandoff[] = []
      for (const handoff of deliverable) {
        if (recentText.some(text => text.includes(handoff.message))) {
          await markHandoffDelivered(handoff.id, 'already present in destination thread')
        } else {
          undelivered.push(handoff)
        }
      }
      if (!undelivered.length) return

      const body = undelivered.length === 1
        ? undelivered[0].message
        : [
            `[QUEUED WORKSPACE HANDOFFS — ${undelivered.length} items]`,
            'Process each complete handoff below in order. Preserve every task ID and return contract.',
            ...undelivered.flatMap(handoff => ['', `### ${handoff.id}`, handoff.message]),
          ].join('\n')
      try {
        await thread.appendUserMessage({ type: 'user-message', content: body })
      } catch {
        return
      }
      for (const handoff of undelivered) {
        await markHandoffDelivered(handoff.id, 'queued through Amp thread append')
      }
    } finally {
      deliveringHandoffs.delete(threadID)
    }
  }

  const turnWaitScript = process.env.TURN_WAIT_SCRIPT ?? `${SWITCHBOARD_REPO}/system/scripts/turn_wait.py`
  const turnWaitsOnOwner = (text: string, threadID: string) =>
    waitsOnOwner(text, threadID, (args) => run('python3', [turnWaitScript, ...args], { cwd: SWITCHBOARD_REPO, timeoutMs: 10_000 }))

  const mirrorTurn = async (
    turnKey: string,
    pending: PendingMirror,
    assistantText: string,
    source: 'agent-end' | 'idle-recovery',
    logger?: { log: (...args: unknown[]) => unknown },
  ): Promise<boolean> => {
    if (reportedTurns.has(turnKey) || mirroringTurns.has(turnKey)) return true
    mirroringTurns.add(turnKey)
    const { workspace, threadID } = pending
    // A turn that ends waiting on the owner is a needs-ruling callback, sent direct
    // with the final text verbatim; every other turn stays
    // the compact digest mirror. turn_wait.py is the one classifier.
    const waiting = await turnWaitsOnOwner(assistantText, threadID)
    const outcome = waiting ? `waiting on the owner: ${assistantText.trim()}` : compactOutcome(assistantText)
    const taskId = `wr-${createHash('sha256').update(`${threadID}:${turnKey}`).digest('hex').slice(0, 16)}`
    // Amp threads have a web link; a Claude session id has none.
    const pointer = /^T-/.test(threadID) ? `https://ampcode.com/threads/${threadID}` : `Claude session ${threadID}`
    const summary = `• ${workspace.title} — ${outcome} · ${pointer}`
    const args = [
      reportScript,
      '--task-id', taskId,
      '--workstream', workspace.slug,
      '--workspace-ref', workspace.ref,
      '--report-kind', 'workspace',
      '--source-workspace', workspace.title,
      '--thread-id', threadID,
      '--create-if-missing',
      ...(waiting ? ['--needs-ruling'] : []),
      summary,
    ]
    try {
      let started = false
      if (process.env.WORKSPACE_REPORT_RUN_SYNC === '1') {
        const result = await run('bash', args, { cwd: SWITCHBOARD_REPO, timeoutMs: 30_000 })
        started = result.code === 0
        if (!started) {
          logDecision('report-command-failed', {
            turn_id: turnKey,
            thread_id: threadID,
            phase: source,
            exit_code: result.code,
            detail: normalized(result.stderr).slice(0, 300),
          }, logger)
        }
      } else {
        started = await runDetached('bash', args, {
          cwd: SWITCHBOARD_REPO,
          logFile,
          label: `${PLUGIN} ${taskId}`,
        })
        if (!started) {
          logDecision('detached-report-launch-failed', {
            turn_id: turnKey,
            thread_id: threadID,
            phase: source,
          }, logger)
        }
      }
      if (!started) return false
      reportedTurns.add(turnKey)
      mirrorableTurns.delete(turnKey)
      logDecision('mirrored', {
        turn_id: turnKey,
        thread_id: threadID,
        phase: source,
        workstream: workspace.slug,
        task_id: taskId,
      }, logger)
      return true
    } finally {
      mirroringTurns.delete(turnKey)
    }
  }

  // A route or Slack response can arrive just after agent.end. Retry while
  // this session is open so it reaches the verified Amp thread without another turn.
  const pendingRetryMs = Number.parseInt(
    process.env.WORKSPACE_REPORT_PENDING_RETRY_MS ?? '15000',
    10,
  )
  const pendingRetryTimer = setInterval(async () => {
    if (process.env.WORKSPACE_REPORT_PENDING_RETRY === '0') return
    const workspace = (await workspaceInfo()).workspace
    const threadIDs = new Set(knownThreadIDs)
    if (workspace) {
      for (const threadID of pendingHandoffThreadIDs(workspace)) threadIDs.add(threadID)
    }
    for (const threadID of threadIDs) {
      try {
        const thread = amp.threads.get(threadID as `T-${string}`)
        if ((await thread.state.get()) !== 'idle') continue
        await deliverPendingHandoffs(workspace, threadID)
      } catch {
        // Keep the durable queue record pending for the next retry.
      }
    }
  }, Number.isFinite(pendingRetryMs) && pendingRetryMs > 0 ? pendingRetryMs : 15_000)
  pendingRetryTimer.unref?.()
  amp.onDispose(() => clearInterval(pendingRetryTimer))

  amp.on('session.start', async (_event, ctx) => {
    const threadID = String(ctx.thread.id)
    if (!threadID) return
    knownThreadIDs.add(threadID)
    try {
      const thread = amp.threads.get(threadID as `T-${string}`)
      if ((await thread.state.get()) === 'idle') await deliverPendingHandoffs(null, threadID)
    } catch {
      // Keep the durable record pending until the existing idle retry succeeds.
    }
  })

  // Amp can occasionally persist a completed assistant turn without emitting
  // agent.end to plugins. Keep the human turn pending from agent.start and
  // reconcile it once the authenticated thread returns to idle.
  const mirrorRetryMs = Number.parseInt(
    process.env.WORKSPACE_REPORT_MIRROR_RETRY_MS ?? '5000',
    10,
  )
  const mirrorRetryTimer = setInterval(async () => {
    if (process.env.WORKSPACE_REPORT_MIRROR_RETRY === '0') return
    for (const [turnKey, pending] of mirrorableTurns) {
      if (reportedTurns.has(turnKey) || mirroringTurns.has(turnKey)) continue
      let state = ''
      let messages: ThreadMessage[] = []
      try {
        const thread = amp.threads.get(pending.threadID as `T-${string}`)
        state = String(await thread.state.get())
        if (state !== 'idle') continue
        messages = await thread.messages({ full: true, from: 'end', limit: 20, roles: ['user', 'assistant'] })
      } catch (error) {
        logDecision('idle-recovery-read-failed', {
          turn_id: turnKey,
          thread_id: pending.threadID,
          phase: 'idle-recovery',
          detail: error instanceof Error ? error.message : String(error),
        })
        continue
      }
      const assistantText = assistantTextForPrompt(messages, pending.prompt)
      if (!assistantText) {
        logDecision('idle-recovery-turn-missing', {
          turn_id: turnKey,
          thread_id: pending.threadID,
          phase: 'idle-recovery',
          state,
        })
        continue
      }
      await mirrorTurn(turnKey, pending, assistantText, 'idle-recovery')
    }
  }, Number.isFinite(mirrorRetryMs) && mirrorRetryMs > 0 ? mirrorRetryMs : 5_000)
  mirrorRetryTimer.unref?.()
  amp.onDispose(() => clearInterval(mirrorRetryTimer))

  amp.on('agent.start', async (event: AgentStartEvent, ctx) => {
    const turnKey = String(event.id)
    const threadID = String(event.thread?.id ?? ctx.thread.id)
    knownThreadIDs.add(threadID)
    const resolution = await isMirrorable(String(event.message ?? ''))
    if (resolution.workspace) {
      mirrorableTurns.set(turnKey, {
        workspace: resolution.workspace,
        threadID,
        prompt: String(event.message ?? ''),
      })
    } else {
      logDecision(resolution.reason ?? 'mirror-ineligible', {
        turn_id: turnKey,
        thread_id: threadID,
        phase: 'agent-start',
        ...(resolution.detail ? { detail: resolution.detail } : {}),
      }, ctx.logger)
    }
  })

  amp.on('agent.end', async (event: AgentEndEvent, ctx) => {
    const turnKey = String(event.id)
    const threadID = String(event.thread?.id ?? ctx.thread.id)
    knownThreadIDs.add(threadID)
    let pending = mirrorableTurns.get(turnKey)
    let resolution: WorkspaceResolution = { workspace: pending?.workspace ?? null }
    if (!pending) {
      resolution = await isMirrorable(String(event.message ?? ''))
      if (resolution.workspace) {
        pending = { workspace: resolution.workspace, threadID, prompt: String(event.message ?? '') }
      }
    }
    const pendingWorkspace = pending?.workspace ?? (await workspaceInfo()).workspace
    await deliverPendingHandoffs(pendingWorkspace, threadID)
    if (event.status !== 'done') {
      mirrorableTurns.delete(turnKey)
      logDecision('agent-status-not-done', {
        turn_id: turnKey,
        thread_id: threadID,
        phase: 'agent-end',
        status: event.status,
      }, ctx.logger)
      return
    }
    if (!pending) {
      logDecision(resolution.reason ?? 'mirror-ineligible', {
        turn_id: turnKey,
        thread_id: threadID,
        phase: 'agent-end',
        ...(resolution.detail ? { detail: resolution.detail } : {}),
      }, ctx.logger)
      return
    }
    if (reportedTurns.has(turnKey)) {
      mirrorableTurns.delete(turnKey)
      logDecision('already-mirrored', { turn_id: turnKey, thread_id: threadID, phase: 'agent-end' }, ctx.logger)
      return
    }
    const assistantText = finalAssistantText(event.messages)
    if (!assistantText) {
      mirrorableTurns.delete(turnKey)
      logDecision('assistant-output-missing', {
        turn_id: turnKey,
        thread_id: threadID,
        phase: 'agent-end',
      }, ctx.logger)
      return
    }
    await mirrorTurn(turnKey, pending, assistantText, 'agent-end', ctx.logger)
  })
}
