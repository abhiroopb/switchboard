// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// amp-mem plugin — Passive memory capture for Amp
// Inspired by claude-mem: automatically captures observations from sessions,
// classifies them by type, and injects context into future sessions.
// Wraps ~/bin/amp-mem CLI (SQLite + FTS5 backed).
//
import type {
  PluginAPI,
  PluginEventContext,
  AgentEndEvent,
  ThreadMessage,
} from '@ampcode/plugin'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { basename } from 'node:path'
import { HOME, isHeadless, isMachinePrompt, isPrivateTurn, run } from './switchboard/lib'

const execFileAsync = promisify(execFile)

// ── Configuration ────────────────────────────────────────────────────────
//
// Capture model: ALLOWLIST, not denylist.
// The plugin captures ONLY:
//   1. Explicit amp_mem_save tool calls
//   2. AI-gated observation extraction at agent.end
//   3. Batched file edits (edit_file/create_file)
//   4. Structured extraction of `switchboard` connector write commands
// Everything else in tool.result is ignored. The previous generic capture
// path produced ~35k pure-noise rows (every shell_command saved verbatim).

// File edit tools — batched per turn, saved at agent.end
const FILE_TOOLS = new Set(['edit_file', 'create_file'])

// Paths that indicate noisy/insignificant file edits
const NOISY_PATHS = [
  'node_modules', '.cache', '.tmp', '.DS_Store',
  'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
]

// AI gating threshold — observations below this probability are discarded
// (live-tunable via amp config: amp-mem.ai_gate_threshold)
let AI_GATE_THRESHOLD = 0.75

// Context injection budget in characters.
// > 0: scoped, index-first block from `amp-mem context --budget-chars N`
//      (one line per row with its #id; agent fetches detail on demand).
// 0:   legacy unscoped `--lines 60` dump (≈9.9k chars) — the rollback path.
// Live-tunable via amp config: amp-mem.context_budget_chars
let CONTEXT_BUDGET_CHARS = 3000

/**
 * Build the `amp-mem context` argument list for session-start injection.
 * Pure so it can be tested: budget 0 selects the legacy output; otherwise
 * the budgeted block scoped to `project`, logged against `session` when
 * `log` is true. Headless runs (scheduled jobs, dispatch sweeps) still get
 * the block but are not logged, so `injection_log` measures interactive
 * sessions only (plan note §9).
 */
export function contextArgs(
  budgetChars: number,
  project: string | null,
  session: string | null,
  log = true,
): string[] {
  if (!(budgetChars > 0)) return ['context', '--lines', '60']
  const args = ['context', '--budget-chars', String(Math.floor(budgetChars))]
  if (project) args.push('--project', project)
  if (session) args.push('--session', session)
  if (log) args.push('--log')
  return args
}

// ── Skill correction detection ──────────────────────────────────────────
// Patterns that indicate a user is correcting/overriding skill behavior mid-flow
const CORRECTION_PATTERNS = [
  /\b(?:no|nope|don'?t|stop|wait)\b.*\b(?:instead|rather|actually)\b/i,
  /\b(?:actually|instead)\b.*\b(?:do|use|try|make|change|switch|set)\b/i,
  /\b(?:always|never)\b.*\b(?:do|use|skip|include|add|remove)\b/i,
  /\bskip\s+(?:that|this|the)\b/i,
  /\bchange\s+(?:that|this|it)\s+to\b/i,
  /\bnot\s+like\s+that\b/i,
  /\bdon'?t\s+(?:do|add|include|use|send|post)\b/i,
  /\buse\s+\S+\s+instead\b/i,
  /\bfrom\s+now\s+on\b/i,
  /\bnext\s+time\b/i,
]

/** Detect if a user message contains a skill correction signal */
export function isSkillCorrection(message: string): boolean {
  return CORRECTION_PATTERNS.some(p => p.test(message))
}

/**
 * A user turn written by switchboard machinery rather than by the owner: automation
 * envelopes, next-up autoposts, router/workspace reports, worker launch
 * prompts, CoS→worker steers and handoffs, subagent completions. These carry
 * instruction text ("never push", "always use …") that CORRECTION_PATTERNS
 * would read as a skill correction and save bogus preference rows. Shared
 * machine-prompt shapes come from lib.
 */
const ENVELOPE_PATTERNS = [
  /^\[AUTOMATION\s+job=/i,
  /^\[NEXT-UP AUTOPOST\]/i,
  /^\[(?:ROUTER|WORKSPACE) REPORT\]/i,
  // Worker report leads: legacy `[rt-… slug]` and the named shape
  // `[🛠️ Switchboard Maintenance · rt-…]` (names-not-numbers rule).
  /^\[[^\]]*\brt-[0-9a-f]{12}[^\]]*\]/i,
  /^Steer from the owner via (?:CoS|Launchpad)\b/i,
  /^CoS for rt-[0-9a-f]{12}\b/i,
  /^Handoff from (?:task|CoS|Launchpad)\b/i,
  /^A background subagent you started has ended\b/i,
  /^Task rt-[0-9a-f]{12}\b.*\b(?:done|failed|blocked)\b/i,
  /^rt-[0-9a-f]{12}\b/i,
  /^(?:Same-task follow-up|CoS same-task resume) for rt-/i,
  /^This is a (?:fresh )?parallel Amp tab\b/i,
  /^\[START-DAY REVIEW\]/i,
  /^Input received on stdin:/i,
  /^FIRST: call shell_command directly\b/i,
  /^Load the kb-distill skill and execute its full distillation workflow\b/i,
  /^SWITCHBOARD_WORKER_TASK_ID=/,
  /^CoS native delivery for task\b/i,
  /^(?:CoS|Same-task) ruling\b/i,
  /^\[COMMS TRIAGE REVIEW\]/i,
  /^\d+ new (?:comms|Slack|GitHub)\b.*\bjust landed\b/i,
  /^Continuing work from thread T-/i,
  /^You are (?:a local switchboard|the quiet Launchpad)\b/i,
  /\bThread message metadata: this message was sent by Amp thread\b/i,
]

export function isAutomationEnvelope(message: string): boolean {
  const text = message.trim()
  if (!text) return true
  if (ENVELOPE_PATTERNS.some(p => p.test(text))) return true
  return isMachinePrompt(text)
}

/**
 * Project label for every save. Owner tabs and workers run with a cwd under
 * workstreams/<slug>, so the slug wins; otherwise the repository basename,
 * with a task worktree (.amp/worktrees/<id>) mapped back to its main repo.
 * Returns null for HOME, "/", or an empty path so a save never carries a
 * meaningless project such as "{{OWNER_USER}}".
 */
export function inferProject(opts: { cwd?: string | null; workspaceRoot?: string | null; env?: Record<string, string | undefined> }): string | null {
  const env = opts.env ?? process.env
  const envSlug = env.SWITCHBOARD_WORKSTREAM_SLUG?.trim()
  if (envSlug && /^[a-z0-9-]+$/i.test(envSlug)) return envSlug
  const cwd = (opts.cwd ?? '').trim()
  const slug = cwd.match(/workstreams\/([a-z0-9][a-z0-9-]*)(?:\/|$)/i)?.[1]
  if (slug && slug !== '_archive') return slug
  let root = (opts.workspaceRoot ?? '').trim().replace(/\/+$/, '')
  if (!root) return null
  const worktree = root.match(/^(.*)\/\.amp\/worktrees\/[^/]+$/)
  if (worktree) root = worktree[1]
  const home = (env.HOME ?? HOME).replace(/\/+$/, '')
  if (root === '/' || root === home) return null
  return basename(root) || null
}

// Confidence for rows that are evidence rather than insight. `context`
// ranks by confidence (times decay), so these sit below decisions and
// preferences and drop out of injection first.
const LOW_CONFIDENCE = '0.3'

// Track which skill is currently active (set when `skill` tool is called).
// It disarms at the end of the turn after the one that loaded it.
let activeSkillName: string | null = null
let skillLoadedThisTurn = false

// Project label resolved once per session (see inferProject)
let currentProject: string | null = null

// Batch file edits — accumulate within a turn, save once at agent.end
let pendingFileEdits: { tool: string; path: string }[] = []

// Track the current session ID
let currentSessionId: string | null = null

// Track observation count for auto-consolidation
let sessionObservationCount = 0

// Track whether context has been injected this session (prevent duplicates)
let contextInjected = false

// Privacy is classified once at agent.start and applies to every event in
// that turn. Fail closed until the first interactive turn is classified.
let currentTurnPrivate = true

// Deduplication: recent topics saved this session to avoid duplicates
let recentTopics: Set<string> = new Set()

// ── Helpers ──────────────────────────────────────────────────────────────

/** Run amp-mem CLI and return stdout. */
const AMP_MEM = process.env.AMP_MEM_BIN ?? '{{HOME}}/bin/amp-mem'

// Most recent hook logger — lets ampMem() report CLI failures instead of
// swallowing them silently (each hook sets this on entry).
let memLogger: { log(msg: string): void } | null = null

// Commands where a non-zero exit is expected/informational (e.g.
// compact-status exits 1 when stale) — don't log those as errors.
const EXPECTED_NONZERO = new Set(['compact-status', 'doctor'])

async function ampMem(_$: unknown, args: string[]): Promise<string> {
  try {
    const { stdout } = await execFileAsync(AMP_MEM, args, {
      env: { ...process.env, HOME: '{{HOME}}' },
      timeout: 10_000,
    })
    return stdout.trim()
  } catch (e) {
    if (!EXPECTED_NONZERO.has(args[0] ?? '')) {
      const detail = e instanceof Error ? e.message.split('\n')[0] : String(e)
      memLogger?.log(`[amp-mem] CLI error: amp-mem ${args.slice(0, 3).join(' ')} — ${detail}`)
    }
    return ''
  }
}

/** `--session` and `--project` flags every passive save carries. */
function baseSaveArgs(): string[] {
  const args: string[] = []
  if (currentSessionId) args.push('--session', currentSessionId)
  if (currentProject) args.push('--project', currentProject)
  return args
}

/** Resolve the project once per session: workspace root, else git toplevel of the cwd. */
async function resolveProject(workspaceRoot: string | null): Promise<string | null> {
  const cwd = process.env.CMUX_AGENT_LAUNCH_CWD || process.env.PWD || process.cwd()
  const direct = inferProject({ cwd, workspaceRoot })
  if (direct) return direct
  const top = await run('git', ['rev-parse', '--show-toplevel'], { cwd, timeoutMs: 5_000 })
  return top.code === 0 ? inferProject({ cwd, workspaceRoot: top.stdout.trim() }) : null
}

/** Heuristic markers that a text block is a Neo auto-compaction summary
 *  (defensive — capturing a compaction summary would create memory loops).
 */
const COMPACTION_SUMMARY_MARKERS = [
  /^# Compacted thread summary/i,
  /^# Conversation summary/i,
  /^Auto-compacted at .* tokens/i,
  /^Session summary \(auto-generated by Amp\)/i,
]

function looksLikeCompactionSummary(text: string): boolean {
  return COMPACTION_SUMMARY_MARKERS.some(p => p.test(text))
}

export function memoryPrivacyPolicy(message: unknown, alreadyPrivate = false) {
  const isPrivate = alreadyPrivate || isPrivateTurn(message)
  return {
    isPrivate,
    saveArgs: isPrivate ? ['--private'] : [],
    sessionSummary: isPrivate ? 'Private turn completed' : String(message ?? '').slice(0, 200),
  }
}

/** Extract text blocks from assistant messages */
function extractAssistantText(messages: ThreadMessage[]): string[] {
  const blocks: string[] = []
  for (const msg of messages) {
    if (msg.role === 'assistant') {
      for (const block of msg.content) {
        if (block.type === 'text' && block.text.length > 20) {
          if (looksLikeCompactionSummary(block.text)) continue
          blocks.push(block.text.slice(0, 500))
        }
      }
    }
  }
  return blocks
}

// ── Structured extraction for `switchboard` connector write commands ───────────
// Connector tools (Slack, Gmail, Linear, Jira, gcal, gdrive, Airtable)
// all route through `switchboard <tool> <subcommand>` shell commands now
// (the old mcp__gmail__* / mcp__linear__* tools no longer exist). Detect
// write subcommands and capture them as structured observations.

interface Extraction { type: string; topic: string; summary: string }

// Write-verb subcommands worth capturing. Deliberately excludes:
// draft (floods DB, AI gate catches important ones), search/list/get/read
// (read-only), describe (introspection).
const SWITCHBOARD_WRITE_VERB = /^(send|post|edit|put|react|modify|comment-add|comment-reply|create|update|reply|add|remove|delete|archive|transition|assign|upload|move|set)\b/

/** Pull a human-useful flag value out of a CLI command string. */
function flagValue(command: string, names: string[]): string {
  for (const name of names) {
    const m = command.match(new RegExp(`--${name}[= ]+(?:"([^"]+)"|'([^']+)'|(\\S+))`))
    const v = m?.[1] || m?.[2] || m?.[3]
    if (v) return v.slice(0, 80)
  }
  return ''
}

function extractConnectorWrite(command: string): Extraction | null {
  const m = command.match(/\bswitchboard\s+([a-z][\w-]*)\s+([a-z][\w-]*)/)
  if (!m) return null
  const [, tool, sub] = m
  if (!SWITCHBOARD_WRITE_VERB.test(sub)) return null

  const detail = flagValue(command, ['title', 'subject', 'channel', 'summary', 'name', 'message', 'text'])
  const topic = `${tool}: ${sub}${detail ? ` — ${detail}` : ''}`
  // observation, not decision: a command string records that a write
  // happened, and observation rows decay (14 d half-life) while decisions
  // are permanent.
  return {
    type: 'observation',
    topic: topic.slice(0, 100),
    summary: `Command: ${command.slice(0, 300)}`,
  }
}

// ── AI Summary Generation ────────────────────────────────────────────────

const DB_PATH = '{{HOME}}/.amp/memory/amp-mem.db'

async function generateAISummaries(ctx: PluginEventContext) {
  try {
    // -json: raw summaries contain newlines and pipes, which broke the old
    // line/pipe-splitting parser (rows after the first line were dropped).
    const { stdout } = await execFileAsync('sqlite3', ['-json', DB_PATH,
      "SELECT id, period_start, period_end, summary FROM summaries WHERE ai_summary IS NULL OR ai_summary = '' LIMIT 3;"
    ], { timeout: 5000, maxBuffer: 10 * 1024 * 1024 })

    if (!stdout.trim()) return
    const rows = JSON.parse(stdout) as { id: number; period_start: string; period_end: string; summary: string }[]

    for (const row of rows) {
      if (!row.id || !row.summary) continue
      const start = row.period_start?.slice(0, 10)
      const end = row.period_end?.slice(0, 10)

      const truncated = row.summary.slice(0, 4000)
      const result = await ctx.ai.ask(
        `Below is a raw log of one week of work activity (${start} to ${end}). ` +
        `Can it be summarized? Answer yes, and write the summary itself as your reasoning: ` +
        `5-8 concise bullets covering key decisions made, projects worked on, ` +
        `workflows established, and people interacted with. Skip routine tool ` +
        `calls and noise. Do NOT write meta-commentary about the log or this task — ` +
        `your reasoning text is stored verbatim as the archive summary.\n\n${truncated}`
      )

      if (result.result === 'yes' && result.reason && result.reason.length > 50) {
        const escapedSummary = result.reason.replace(/'/g, "''")
        await execFileAsync('sqlite3', [DB_PATH,
          `UPDATE summaries SET ai_summary = '${escapedSummary}' WHERE id = ${row.id};`
        ], { timeout: 5000 })
        ctx.logger.log(`[amp-mem] Generated AI summary for period ${start} to ${end}`)
      }
    }
  } catch (e) {
    ctx.logger.log(`[amp-mem] AI summary generation failed: ${e}`)
  }
}

// ── Plugin ───────────────────────────────────────────────────────────────

export default function (amp: PluginAPI) {
  amp.logger.log('[amp-mem] Plugin loaded')

  // Live-tunable thresholds via amp config namespace `amp-mem`.
  // Keys: amp-mem.ai_gate_threshold, amp-mem.context_budget_chars
  // (compact_threshold is removed: compact runs every session
  //  start as a convergent no-op; min_prompt_length removed with raw-prompt capture)
  amp.configuration.subscribe(v => {
    const config = v as Record<string, unknown>
    const ag = config['amp-mem.ai_gate_threshold']
    if (typeof ag === 'number' && ag >= 0 && ag <= 1) AI_GATE_THRESHOLD = ag
    const cb = config['amp-mem.context_budget_chars']
    if (typeof cb === 'number' && cb >= 0 && cb <= 50_000) CONTEXT_BUDGET_CHARS = cb
  })

  // ── session.start ────────────────────────────────────────────────────
  // Auto-init, backup, start session, check distill status, auto-consolidate
  amp.on('session.start', async (_event, ctx) => {
    memLogger = ctx.logger
    ctx.logger.log('[amp-mem] Session starting')

    // Ensure DB exists
    await ampMem(ctx.$, ['init'])

    // Backup (daily, not per-session). Delegates to `amp-mem backup`, which
    // uses sqlite3 .backup — WAL-safe, unlike the old fs.copyFileSync that
    // could snapshot a torn mid-checkpoint state.
    const backupResult = await ampMem(ctx.$, ['backup'])
    if (backupResult.includes('created')) ctx.logger.log('[amp-mem] Daily backup created')

    // Start a session with the thread URL
    const threadId = ctx.thread?.id || ''
    const threadUrl = threadId ? `https://ampcode.com/threads/${threadId}` : ''
    const sessionArgs = ['session-start']
    if (threadUrl) sessionArgs.push('--thread-url', threadUrl)
    const sessionResult = await ampMem(ctx.$, sessionArgs)
    currentSessionId = sessionResult.trim() || null
    ctx.logger.log(`[amp-mem] Session #${currentSessionId} started (thread: ${threadId || 'none'})`)

    // Project label for every save this session, so passive saves carry a project too.
    try {
      const root = amp.system.workspaceRoot
      let rootPath: string | null = null
      if (root) {
        try { rootPath = amp.helpers.filePathFromURI(root) } catch { rootPath = null }
        if (!rootPath) rootPath = root.startsWith('file://') ? decodeURIComponent(root.slice('file://'.length)) : root
      }
      currentProject = await resolveProject(rootPath)
    } catch {
      currentProject = null
    }
    ctx.logger.log(`[amp-mem] Project: ${currentProject ?? 'none'}`)

    // Check distill status and log
    const status = await ampMem(ctx.$, ['distill-status'])
    ctx.logger.log(`[amp-mem] ${status}`)

    // Auto-consolidation: compact if too many un-compacted observations
    try {
      // Compact is convergent and a cheap no-op when nothing is pending
      // (per-row compacted_at model), so run it every session start.
      // (The old code compared TOTAL observations against the threshold,
      // which fired a full compact on every session once the DB grew.)
      await ampMem(ctx.$, ['compact', '--older-than', '7'])
      // Generate AI summaries for any compacted weeks missing one
      await generateAISummaries(ctx)
    } catch {
      // Non-critical
    }

    // Reset accumulators
    pendingFileEdits = []
    sessionObservationCount = 0
    contextInjected = false
    recentTopics = new Set()
    activeSkillName = null
    skillLoadedThisTurn = false
    currentTurnPrivate = true
  })

  // ── agent.start ──────────────────────────────────────────────────────
  // Inject memory context. Raw user prompts are NOT saved anymore — they
  // were 6.7k rows of noise (25%+ from automation), and the AI-gated
  // agent.end handler already records noteworthy turns.
  amp.on('agent.start', async (event, ctx) => {
    memLogger = ctx.logger
    currentTurnPrivate = isPrivateTurn(event.message)
    // Reset file edit accumulator for this turn
    pendingFileEdits = []

    // Inject recent memory context (only once per session to avoid duplicates)
    if (!contextInjected) {
      const context = await ampMem(
        ctx.$,
        contextArgs(CONTEXT_BUDGET_CHARS, currentProject, currentSessionId, !isHeadless()),
      )

      // Health check: if compact pipeline is stale (>24h since last success
      // OR never run), surface a visible warning so the issue gets fixed
      // rather than silently piling up backlog. compact-status returns
      // non-zero when stale; ampMem catches the failure and returns '',
      // so we re-run with execFile to capture both stdout and exit code.
      let healthWarning = ''
      try {
        const statusBody: string = await new Promise((resolve) => {
          execFile(AMP_MEM, ['compact-status'], {
            env: { ...process.env, HOME: '{{HOME}}' },
            timeout: 5_000,
          }, (err, stdout) => {
            if (err) {
              resolve(stdout || '')
            } else {
              resolve('')
            }
          })
        })
        if (statusBody) {
          healthWarning = `<amp-mem-health-warning>\n⚠️ amp-mem compact pipeline is unhealthy:\n\n${statusBody.trim()}\n\nRun: amp-mem compact --older-than 7\n</amp-mem-health-warning>\n\n`
          ctx.logger.log('[amp-mem] Compact pipeline is stale, injecting warning')
        }
      } catch (e) {
        ctx.logger.log(`[amp-mem] compact-status check failed: ${e}`)
      }

      if (context && context.length > 50) {
        contextInjected = true
        return {
          message: {
            content: `${healthWarning}<amp-mem-context>\n${context}\n</amp-mem-context>`,
            display: false,
          },
        }
      } else if (healthWarning) {
        // Even with no context to inject, still surface the health warning
        contextInjected = true
        return {
          message: {
            content: healthWarning,
            display: false,
          },
        }
      }
    }
  })

  // ── agent.end ────────────────────────────────────────────────────────
  // AI-gated observation extraction from the completed turn.
  // Saves accumulated file edits. Auto-compacts if needed.
  amp.on('agent.end', async (event, ctx) => {
    memLogger = ctx.logger
    if (event.status !== 'done') return

    const toolCalls = amp.helpers.toolCallsInMessages(event.messages)
    const textBlocks = extractAssistantText(event.messages)
    const privacy = memoryPrivacyPolicy(event.message, currentTurnPrivate)
    const turnPrivate = privacy.isPrivate

    // Skip trivial turns
    if (textBlocks.length === 0 && toolCalls.length === 0) return

    const toolSummary = toolCalls
      .slice(0, 15)
      .map(tc => tc.call.tool)
      .join(', ')

    // ── Save accumulated file edits as a low-confidence batch observation ──
    // Only edits to instruction surfaces (plugins, AGENTS.md, skills, config,
    // schema) are kept, as an audit trail for kb-distill; a plain "Edited N
    // file(s)" path list is evidence, not insight, and is almost never retrieved.
    if (pendingFileEdits.length > 0) {
      const uniquePaths = [...new Set(pendingFileEdits.map(e => e.path))]
      const instructionPaths = uniquePaths.filter(p =>
        p.includes('plugin') || p.includes('AGENTS') || p.includes('SKILL') ||
        p.includes('config') || p.includes('schema'))

      if (instructionPaths.length > 0) {
        const filesTopic = `Edited instruction surfaces: ${instructionPaths.map(p => basename(p)).slice(0, 3).join(', ')}`
        const filesSummary = uniquePaths.map(p => `- ${p}`).join('\n')
        await ampMem(ctx.$, ['save', 'observation', filesTopic.slice(0, 100), filesSummary,
          '--confidence', LOW_CONFIDENCE, '--tags', 'file-edits', ...baseSaveArgs(), ...privacy.saveArgs])
        sessionObservationCount++
        ctx.logger.log(`[amp-mem] Saved instruction-surface edits: ${instructionPaths.length} of ${uniquePaths.length} files`)
      }
      pendingFileEdits = []
    }

    // ── AI-gated observation extraction from the turn ──
    const turnText = textBlocks.slice(0, 4).join('\n---\n')
    const turnSummary = `User prompt: ${event.message.slice(0, 200)}\n\nTools: [${toolSummary}]\n\nAssistant output:\n${turnText}`.slice(0, 2000)

    const assessment = turnPrivate ? null : await ctx.ai.ask(
      `Does this agent conversation turn contain a noteworthy insight worth preserving ` +
      `for future sessions? In your reasoning, classify it as one of: ` +
      `DECISION (chose/decided something), DISCOVERY (found/learned something new), ` +
      `PREFERENCE (user expressed a preference), BUGFIX (fixed a bug), ` +
      `CONFIG (changed configuration/settings), WORKFLOW (established a process/pattern), ` +
      `PEOPLE (context about a person/team), or OBSERVATION (general noteworthy fact).\n\n` +
      `NOT noteworthy: trivial reads, routine greetings, simple file lookups, ` +
      `pure informational responses with no new insight.\n\n` +
      `If noteworthy, start your reasoning with a line "TITLE: <concise 5-10 word ` +
      `title describing the insight>" before the classification.\n\n` +
      `Turn summary:\n${turnSummary}`
    ).catch((err: unknown) => {
      // Non-fatal: a transient AI failure must not reject the agent.end handler.
      ctx.logger.log(`[amp-mem] Turn assessment failed (non-fatal): ${err}`)
      return null
    })

    if (assessment) ctx.logger.log(`[amp-mem] Turn assessment: ${assessment.result} (p=${assessment.probability.toFixed(2)}) — ${assessment.reason}`)

    if (assessment && assessment.result === 'yes' && assessment.probability > AI_GATE_THRESHOLD) {
      let type = 'observation'
      const reason = assessment.reason.toUpperCase()
      if (reason.includes('DECISION')) type = 'decision'
      else if (reason.includes('DISCOVERY')) type = 'discovery'
      else if (reason.includes('PREFERENCE')) type = 'preference'
      else if (reason.includes('BUGFIX')) type = 'bugfix'
      else if (reason.includes('CONFIG')) type = 'config'
      else if (reason.includes('WORKFLOW')) type = 'workflow'
      else if (reason.includes('PEOPLE')) type = 'people'

      // Prefer the AI-provided title over the raw prompt prefix — truncated
      // prompts made terrible topics ("Read and follow all instructions in /Us…").
      const titleMatch = assessment.reason.match(/TITLE:\s*(.{5,100}?)(?:\n|$)/i)
      const topic = titleMatch?.[1]?.trim() || 'Agent turn'
      const reasonBody = assessment.reason.replace(/^\s*TITLE:.*$/im, '').trim()

      // Deduplication: skip if we already saved a very similar topic this session
      const topicKey = topic.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
      if (recentTopics.has(topicKey)) {
        ctx.logger.log(`[amp-mem] Skipped duplicate topic: ${topic}`)
      } else {
        recentTopics.add(topicKey)
        const summary = `${reasonBody}\n\nSource: ${ctx.thread.id}; captured: ${new Date().toISOString()}`
        await ampMem(ctx.$, ['save', type, topic, summary, ...baseSaveArgs()])
        sessionObservationCount++
        ctx.logger.log(`[amp-mem] Saved ${type}: ${topic}`)
      }
    }

    // ── Skill correction detection ──
    // If the user is mid-skill and sends a correction signal, capture it
    // as a tagged observation so kb-distill can cluster and propose SKILL.md
    // edits. Machine-written turns (automation envelopes, worker launches,
    // CoS steers) are never corrections.
    if (activeSkillName && event.message && !isAutomationEnvelope(event.message) && isSkillCorrection(event.message)) {
      const correctionTopic = turnPrivate
        ? `Private skill correction [${activeSkillName}]`
        : `Skill correction [${activeSkillName}]: ${event.message.slice(0, 80)}`
      const correctionSummary =
        `User corrected/overrode behavior in skill "${activeSkillName}".\n` +
        `User said: ${event.message.slice(0, 400)}\n` +
        `Assistant response: ${textBlocks.slice(0, 2).join('\n').slice(0, 300)}`

      await ampMem(ctx.$, [
        'save', 'preference', correctionTopic, correctionSummary,
        '--tags', `skill-correction,${activeSkillName}`,
        ...baseSaveArgs(),
        ...privacy.saveArgs,
      ])
      sessionObservationCount++
      ctx.logger.log(`[amp-mem] Captured${turnPrivate ? ' private' : ''} skill correction for "${activeSkillName}"`)
    }
    // A skill stays "active" for the turn that loaded it and the one after
    // (where a correction lands), then disarms. Before this reset the skill
    // stayed active for the rest of the session, so every later instruction
    // turn was filed as a correction of it.
    if (skillLoadedThisTurn) skillLoadedThisTurn = false
    else activeSkillName = null

    // ── Session tracking: update ended_at each turn (last one sticks) ──
    if (currentSessionId) {
      await ampMem(ctx.$, ['session-end', '--session', currentSessionId, '--summary', privacy.sessionSummary])
    }

    // ── Auto-consolidation: compact after many observations in this session ──
    if (sessionObservationCount > 0 && sessionObservationCount % 20 === 0) {
      ctx.logger.log(`[amp-mem] Auto-compacting after ${sessionObservationCount} observations this session`)
      await ampMem(ctx.$, ['compact', '--older-than', '7'])
    }
  })

  // ── tool.result ──────────────────────────────────────────────────────
  // ALLOWLIST capture only:
  //   skill        → track active skill for correction detection
  //   edit/create  → batch file edits (saved at agent.end)
  //   shell_command → structured extraction of `switchboard` connector writes
  // Everything else is ignored — noteworthy turns are captured by the
  // AI-gated agent.end handler instead.
  amp.on('tool.result', async (event, ctx) => {
    memLogger = ctx.logger
    if (event.status !== 'done') return

    // ── Track active skill for correction detection ──
    if (event.tool === 'skill') {
      const input = event.input as Record<string, string>
      activeSkillName = input.name || null
      skillLoadedThisTurn = activeSkillName !== null
      ctx.logger.log(`[amp-mem] Active skill set: ${activeSkillName}`)
      return
    }

    // ── File edits: accumulate, don't save individually ──
    if (FILE_TOOLS.has(event.tool)) {
      const input = event.input as Record<string, string>
      const path = input.path || ''
      if (path && !NOISY_PATHS.some(n => path.includes(n))) {
        pendingFileEdits.push({ tool: event.tool, path })
      }
      return
    }

    // ── switchboard connector writes via shell_command ──
    if (event.tool === 'shell_command') {
      const input = event.input as Record<string, string>
      const command = input.command || ''
      if (!command || command.includes('amp-mem')) return
      const structured = extractConnectorWrite(command)
      if (structured) {
        // A raw command string is an audit record, not a decision: keep it
        // searchable but below insight rows in context ranking.
        const privacyArgs = currentTurnPrivate ? ['--private'] : []
        await ampMem(ctx.$, ['save', structured.type, structured.topic, structured.summary,
          '--confidence', LOW_CONFIDENCE, '--tags', 'switchboard-connector', ...baseSaveArgs(), ...privacyArgs])
        sessionObservationCount++
        ctx.logger.log(`[amp-mem] Captured ${structured.type}: ${structured.topic}`)
      }
      return
    }

    // All other tools: no capture.
  })

  // ── Custom tool: amp_mem_search ────────────────────────────────────────
  amp.registerTool({
    name: 'amp_mem_search',
    description: 'Search persistent cross-session memory. Use before answering questions that might relate to past work, decisions, or context from previous sessions. Results are scoped to the current project (workstream or repo) first and fall back to all projects when the scoped search has no hits; pass project to override.',
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'FTS5 search query (supports AND, OR, NOT, prefix*)' },
        type: { type: 'string', description: 'Filter by type: observation, decision, discovery, preference, bugfix, config, workflow, people, session, thread' },
        limit: { type: 'number', description: 'Max results (default 20)' },
        project: { type: 'string', description: 'Project to search; "*" searches every project without the scoped-first pass' },
      },
      required: ['query'],
    },
    async execute(input) {
      const args = ['search', input.query as string]
      if (input.type) args.push('--type', input.type as string)
      if (input.limit) args.push('--limit', String(input.limit))
      const explicit = typeof input.project === 'string' ? (input.project as string) : null
      const scope = explicit ?? currentProject
      // `--project` in the CLI also admits rows with no project (the 2,506
      // legacy rows saved before passive saves carried --project), so the
      // scoped pass only excludes rows tagged with a different project.
      if (scope && scope !== '*') {
        const scoped = (await ampMem(amp.$, [...args, '--project', scope])).trim()
        if (scoped) return `Scope: project ${scope}\n${scoped}`
        if (explicit) return `No results found in project ${scope}.`
      }
      return (await ampMem(amp.$, args)) || 'No results found.'
    },
  })

  // ── Custom tool: amp_mem_save ──────────────────────────────────────────
  amp.registerTool({
    name: 'amp_mem_save',
    description: 'Save an observation to persistent memory. Use when you encounter a noteworthy decision, discovery, preference, bug fix, or important context that the passive system might miss.',
    inputSchema: {
      type: 'object',
      properties: {
        type: {
          type: 'string',
          description: 'Type: observation, decision, discovery, preference, bugfix, config, workflow, people',
          enum: ['observation', 'decision', 'discovery', 'preference', 'bugfix', 'config', 'workflow', 'people'],
        },
        topic: { type: 'string', description: 'Short topic/title (max 100 chars)' },
        summary: { type: 'string', description: 'Detailed summary of what was learned or decided' },
        private: { type: 'boolean', description: 'Mark as private — excluded from context injection' },
        project: { type: 'string', description: 'Optional project name (defaults to the current workstream slug or repository)' },
        tags: { type: 'string', description: 'Optional comma-separated tags' },
      },
      required: ['type', 'topic', 'summary'],
    },
    async execute(input) {
      const args = ['save', input.type as string, input.topic as string, input.summary as string]
      if (input.private) args.push('--private')
      const project = (input.project as string | undefined) || currentProject
      if (project) args.push('--project', project)
      if (input.tags) args.push('--tags', input.tags as string)
      if (currentSessionId) args.push('--session', currentSessionId)
      return (await ampMem(amp.$, args)) || 'Saved.'
    },
  })

  // ── Custom tool: amp_mem_stats ─────────────────────────────────────────
  amp.registerTool({
    name: 'amp_mem_stats',
    description: 'Show memory statistics: total observations, sessions, compacted summaries, DB size, and breakdown by type/project.',
    inputSchema: { type: 'object', properties: {} },
    async execute() {
      return (await ampMem(amp.$, ['stats'])) || 'No stats available.'
    },
  })
}
