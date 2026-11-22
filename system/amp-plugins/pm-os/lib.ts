// Shared utilities for PM OS plugins.
// NOT a plugin itself — imported by the real plugins above this folder.
//
// Conventions:
//  - All paths PM-OS-relative use PM_OS_REPO.
//  - Telemetry events go through `logEvent` so a usage telemetry plugin can pick them up.
//  - Side effects (fs writes, exec) live here, not in plugin event handlers,
//    so plugin handlers stay readable and testable.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { appendFile, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const HOME = process.env.HOME ?? '{{HOME}}'
export const PM_OS_REPO = process.env.PM_OS_ROOT?.replaceAll('\\', '/') ?? `${HOME}/Development/pm-os`
export const PLUGIN_LOG_DIR = `${HOME}/.agents/.amp-plugin-logs`
export const PLUGIN_STATE_DIR = `${HOME}/.config/amp/plugin-state`

const execFileAsync = promisify(execFile)

export interface RunResult {
  stdout: string
  stderr: string
  code: number
}

/** Run a binary with args, capturing output. Never throws. */
export async function run(bin: string, args: string[], opts: { cwd?: string; timeoutMs?: number; env?: Record<string, string> } = {}): Promise<RunResult> {
  try {
    const { stdout, stderr } = await execFileAsync(bin, args, {
      cwd: opts.cwd,
      timeout: opts.timeoutMs ?? 60_000,
      env: { ...process.env, ...(opts.env ?? {}) },
    })
    return { stdout: stdout.toString(), stderr: stderr.toString(), code: 0 }
  } catch (e: unknown) {
    const err = e as { code?: number; stdout?: string | Buffer; stderr?: string | Buffer; message?: string }
    return {
      stdout: err.stdout?.toString() ?? '',
      stderr: err.stderr?.toString() ?? err.message ?? '',
      code: typeof err.code === 'number' ? err.code : 1,
    }
  }
}

/**
 * What runDetached starts. On Windows a detached spawn (Node and Bun both) is DETACHED_PROCESS,
 * a process with no console, and every git or python call under it opens a new terminal window.
 * A non-detached spawn dies with Bun. So Windows goes through no_window.py spawn,
 * which starts the command with a hidden console its children inherit, then returns.
 */
export function detachedSpawnPlan(bin: string, args: string[], platform: string = process.platform): { command: string; args: string[] } {
  if (platform !== 'win32') return { command: bin, args }
  return { command: 'python3', args: [`${PM_OS_REPO}/system/scripts/no_window.py`, 'spawn', '--', bin, ...args] }
}

/** Fire-and-forget background spawn. Detaches so a long-running script doesn't block the plugin. */
export async function runDetached(bin: string, args: string[], opts: { cwd?: string; logFile?: string; label?: string } = {}): Promise<boolean> {
  const { spawn } = await import('node:child_process')
  const log = opts.logFile ?? `${PLUGIN_LOG_DIR}/detached.log`
  await mkdir(dirname(log), { recursive: true })
  const { openSync, closeSync } = await import('node:fs')
  const fd = openSync(log, 'a')
  const label = opts.label ?? `${bin} ${args[0] ?? ''}`.trim()
  try {
    const plan = detachedSpawnPlan(bin, args)
    const child = spawn(plan.command, plan.args, {
      cwd: opts.cwd,
      detached: true,
      windowsHide: true,
      stdio: ['ignore', fd, fd],
      env: process.env,
    })
    child.on('error', (error) => {
      void appendFile(log, `[${new Date().toISOString()}] detached spawn error (${label}): ${error.message}\n`)
    })
    child.unref()
    return child.pid !== undefined
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    await appendFile(log, `[${new Date().toISOString()}] detached spawn error (${label}): ${message}\n`)
    return false
  } finally {
    // The child inherits its own copy of the descriptor; close the parent's
    // to avoid leaking one fd per detached invocation.
    try { closeSync(fd) } catch { /* already closed */ }
  }
}

/**
 * Start a detached runner that serializes one plugin/key, waits for the child,
 * and publishes the cooldown timestamp only after a successful exit.
 */
export async function runDebouncedDetached(
  plugin: string,
  key: string,
  cooldownSeconds: number,
  bin: string,
  args: string[],
  opts: { cwd?: string; logFile?: string; label?: string } = {},
): Promise<boolean> {
  return runDetached('python3', [
    `${PM_OS_REPO}/system/scripts/run-debounced-plugin.py`,
    '--plugin', plugin,
    '--key', key,
    '--cooldown-seconds', String(cooldownSeconds),
    '--state-dir', PLUGIN_STATE_DIR,
    '--', bin,
    ...args,
  ], opts)
}

/** Append a structured event line to the plugin telemetry log. */
export async function logEvent(plugin: string, event: Record<string, unknown>): Promise<void> {
  try {
    await mkdir(PLUGIN_LOG_DIR, { recursive: true })
    const line = JSON.stringify({
      plugin,
      ts: new Date().toISOString(),
      ...event,
    }) + '\n'
    await appendFile(`${PLUGIN_LOG_DIR}/pm-os-events.jsonl`, line)
  } catch {
    // Never disrupt the session
  }
}

/** Per-plugin debounce/state file. Keys must be filesystem-safe. */
export async function readState<T>(plugin: string, key: string): Promise<T | null> {
  try {
    const path = `${PLUGIN_STATE_DIR}/${plugin}/${key}.json`
    if (!existsSync(path)) return null
    const raw = await readFile(path, 'utf8')
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export async function writeState<T>(plugin: string, key: string, value: T): Promise<void> {
  let tmp: string | null = null
  try {
    const dir = `${PLUGIN_STATE_DIR}/${plugin}`
    await mkdir(dir, { recursive: true })
    // Atomic write: tmp file + rename, so concurrent plugin runtimes never
    // observe a half-written JSON file. The unique suffix also prevents two
    // callbacks in this process from sharing and replacing one temp file.
    // Last rename wins; this does not claim cross-process RMW serialization.
    tmp = `${dir}/.${key}.json.tmp-${process.pid}-${randomUUID()}`
    await writeFile(tmp, JSON.stringify(value, null, 2))
    await rename(tmp, `${dir}/${key}.json`)
    tmp = null
  } catch {
    // Never disrupt the session
  } finally {
    if (tmp) {
      try { await rm(tmp, { force: true }) } catch { /* best-effort cleanup */ }
    }
  }
}

/** A private marker applies to the entire turn, including every tool event. */
export function isPrivateTurn(message: unknown): boolean {
  return typeof message === 'string' && /<private(?:\s[^>]*)?>/i.test(message)
}

/** Returns true if the given timestamp was within the last N seconds. */
export function withinSeconds(iso: string | undefined, seconds: number): boolean {
  if (!iso) return false
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return false
  return (Date.now() - ts) < seconds * 1000
}

/**
 * A first turn written by PM OS itself rather than by the owner: start-day seeds,
 * Router/dispatch worker launches, automation jobs, CoS handoffs, routine
 * boots. These prompts already carry their own instructions, so plugins that
 * react to a human first turn (workspace-report mirroring, the auto-pilot
 * routing reminder) skip them. Shared here because the start-day seed
 * preamble and the dispatch prompt both mention "PM OS", which otherwise
 * would match the `pm-os` agent-registry entry in every seeded tab.
 */
export function isMachinePrompt(message: string): boolean {
  const text = message.trim()
  if (!text) return true
  return [
    /^\[AUTOMATION\b/i,
    /^\[CoS handoff\b/i,
    /^\[(?:ROUTER|WORKSPACE) REPORT\]/i,
    /^\[QUEUED WORKSPACE HANDOFFS\b/i,
    /^\[NEXT-UP AUTOPOST\]/i,
    /^COS_QUICK_IO_BOOT\b/i,
    /^CoS outcome check:/i,
    /^Maintenance control for Router\b/i,
    /^🔁 goal-loop\b/i,
    /\bROUTER_TASK_ID=/,
    /\bRouter return contract:/i,
    /^You are PM OS worker\b/i,
    /Handoff from CoS \(LOCAL EXECUTION REQUIRED:/,
    /Background automation job/i,
    /Thread message metadata: this message was sent by Amp thread/i,
    /^IMPORTANT: Skip all AGENTS\.md session-start steps/i,
    /^Read and follow all instructions in .*system\/state\/launch-prompts\//i,
  ].some(pattern => pattern.test(text))
}

/** Detect headless mode by env (set by run-background-tasks.sh and amp -x). */
export function isHeadless(): boolean {
  return Boolean(process.env.AMP_HEADLESS) || process.env.AMP_INTERACTIVE === 'false'
}

/** Detect cmux active workspace name (best-effort). */
export async function activeWorkspaceName(): Promise<string | null> {
  const ws = await activeWorkspace()
  return ws ? ws.title : null
}

/**
 * dispatch.py gives every worker tab the cmux description "PM OS worker <task-id>"
 * (WORKER_DESCRIPTION_PREFIX). Titles are workstream names, so a worker can be
 * titled "Chief Of Staff 1" (the start-day worker) and still not be
 * the CoS session; identity gates must consult the description first.
 */
export const WORKER_DESCRIPTION_RE = /^PM OS worker\b/

export function isWorkerWorkspace(ws: { description?: string } | null): boolean {
  return Boolean(ws && WORKER_DESCRIPTION_RE.test(ws.description ?? ''))
}

/** Title and description of the cmux workspace this process runs in (best-effort). */
export async function activeWorkspace(): Promise<{ title: string; description: string } | null> {
  const cmux = process.env.CMUX_BIN ?? process.env.CMUX_AMP_CMUX_BIN ?? '/Applications/cmux.app/Contents/Resources/bin/cmux'
  if (!existsSync(cmux)) return null
  const r = await run(cmux, ['identify'], { timeoutMs: 2000 })
  if (r.code !== 0) return null
  try {
    const j = JSON.parse(r.stdout) as { caller?: { workspace_ref?: string; title?: string } }
    const workspaceRef = j.caller?.workspace_ref
    if (!workspaceRef) return j.caller?.title ? { title: j.caller.title, description: '' } : null

    // identify exposes only workspace_ref (older builds added title but never the
    // description). Resolve it through workspace.list for the user-visible title
    // and the dispatcher-set description.
    const listed = await run(cmux, ['rpc', 'workspace.list', '{}'], { timeoutMs: 2000 })
    if (listed.code !== 0) return { title: j.caller?.title ?? workspaceRef, description: '' }
    const payload = JSON.parse(listed.stdout) as {
      workspaces?: Array<{ id?: string; ref?: string; title?: string; custom_title?: string; description?: string }>
    }
    const workspace = (payload.workspaces ?? []).find(item =>
      item.id === workspaceRef || item.ref === workspaceRef,
    )
    return {
      title: workspace?.custom_title ?? workspace?.title ?? j.caller?.title ?? workspaceRef,
      description: workspace?.description ?? '',
    }
  } catch {
    return null
  }
}

/**
 * The wmux pane this process runs in, the pipe to ask, and the pipe token; null outside a
 * wmux pane. wmux exports WMUX_SURFACE_ID to every pane (and PM OS copies it to
 * CMUX_SURFACE_ID). PM_OS_TEST_SANDBOX=1 fails closed: only a test's own
 * PM_OS_TEST_WMUX_PIPE is asked, never the live wmux.
 */
export function wmuxPaneTarget(env: Record<string, string | undefined> = process.env): { surface: string; pipe: string; token: string } | null {
  const surface = (env.WMUX_SURFACE_ID ?? '').trim()
  if (!surface) return null
  const suffix = (env.WMUX_INSTANCE ?? '').trim() ? `-${(env.WMUX_INSTANCE ?? '').trim()}` : ''
  const testPipe = (env.PM_OS_TEST_WMUX_PIPE ?? '').trim()
  if (env.PM_OS_TEST_SANDBOX === '1' && !testPipe) return null
  const pipe = testPipe || (env.WMUX_PIPE ?? '').trim() || `\\\\.\\pipe\\wmux${suffix}`
  // Same order as cmux-shim.py: wmux's token file first (it is current after a wmux restart),
  // then the pane's WMUX_PIPE_TOKEN. The sandbox never reads the live token file.
  let token = ''
  const appData = env.APPDATA ?? (env.USERPROFILE ? `${env.USERPROFILE}/AppData/Roaming` : '')
  if (appData && env.PM_OS_TEST_SANDBOX !== '1') {
    try { token = readFileSync(`${appData}/wmux${suffix}/pipe-token`, 'utf8').trim() } catch { /* no file */ }
  }
  if (!token) token = (env.WMUX_PIPE_TOKEN ?? '').trim()
  return { surface, pipe, token }
}

/** One JSON request over wmux's named pipe, in this process. Null on any error or timeout. Never throws. */
export async function wmuxPipeRequest(
  target: { pipe: string; token: string },
  method: string,
  params: Record<string, unknown>,
  timeoutMs = 1_500,
): Promise<Record<string, unknown> | null> {
  const { connect } = await import('node:net')
  return await new Promise(resolve => {
    let done = false
    let buf = ''
    const finish = (value: Record<string, unknown> | null) => {
      if (done) return
      done = true
      clearTimeout(timer)
      try { socket.destroy() } catch { /* already closed */ }
      resolve(value)
    }
    const timer = setTimeout(() => finish(null), timeoutMs)
    let socket: ReturnType<typeof connect>
    try {
      socket = connect(target.pipe)
    } catch {
      clearTimeout(timer)
      resolve(null)
      return
    }
    socket.on('connect', () => {
      socket.write(JSON.stringify({ method, params, id: 1, token: target.token }) + '\n')
    })
    // One decoder for the whole reply: an emoji split across two reads stays whole.
    socket.setEncoding('utf8')
    socket.on('data', chunk => {
      buf += chunk
      const nl = buf.indexOf('\n')
      if (nl < 0) return
      try {
        const reply = JSON.parse(buf.slice(0, nl)) as { result?: unknown; error?: unknown }
        finish(!reply.error && reply.result && typeof reply.result === 'object' ? reply.result as Record<string, unknown> : null)
      } catch {
        finish(null)
      }
    })
    socket.on('error', () => finish(null))
    socket.on('close', () => finish(null))
  })
}

/** cmux-shim.py's sidecar, where the custom title and the "PM OS worker <task-id>" description live. */
export function wmuxSidecarPath(env: Record<string, string | undefined> = process.env, platform: string = process.platform): string {
  if (env.PM_OS_WMUX_SIDECAR) return env.PM_OS_WMUX_SIDECAR
  // The shim uses Python's Path.home(), which is USERPROFILE on Windows even when HOME is set.
  const home = (platform === 'win32' ? env.USERPROFILE ?? env.HOME : env.HOME ?? env.USERPROFILE) ?? HOME
  const stateHome = env.PM_OS_STATE_HOME || `${home.replaceAll('\\', '/')}/.pm-os`
  return `${stateHome}/wmux-sidecar.json`
}

/**
 * The wmux tab this pane belongs to, shaped like activeWorkspace(). One pipe call
 * (workspace.current, about 6 ms) and one file read, no child process: Bun cannot run the
 * bash cmux shim, and every exited process leaks on Windows. The title is the
 * shim's custom title when one is set, else wmux's live title, which is what
 * `cmux rpc workspace.list` reports as custom_title.
 */
export async function wmuxActiveWorkspace(
  env: Record<string, string | undefined> = process.env,
): Promise<{ title: string; description: string } | null> {
  const target = wmuxPaneTarget(env)
  if (!target) return null
  const current = await wmuxPipeRequest(target, 'workspace.current', { caller: target.surface })
  const id = typeof current?.id === 'string' ? current.id : ''
  if (!id) return null
  let entry: { custom_title?: string | null; description?: string | null } | undefined
  const sidecarPath = wmuxSidecarPath(env)
  if (existsSync(sidecarPath)) {
    try {
      const sidecar = JSON.parse(readFileSync(sidecarPath, 'utf8')) as { workspaces?: Record<string, typeof entry> }
      entry = sidecar.workspaces?.[id]
    } catch {
      // Unreadable mid-rewrite: the worker mark may be in there, so say nothing (gates stay open)
      // rather than judge a worker titled "Chief Of Staff 1" by its title alone.
      return null
    }
  }
  const liveTitle = typeof current?.title === 'string' ? current.title : ''
  // dispatch.py sets PM_OS_WORKER_ID in every worker; it marks a worker even with no sidecar entry.
  const workerId = (env.PM_OS_WORKER_ID ?? '').trim()
  const description = entry?.description || (workerId ? `PM OS worker ${env.PM_OS_WORKER_TASK_ID || workerId}` : '')
  return { title: entry?.custom_title ?? (liveTitle || id), description }
}

/**
 * The tab this process runs in, for identity gates (the CoS delivery gate, the dispatch
 * guard, the wait guard). A wmux pane asks wmux in-process, and a failed lookup there is
 * null (gates stay open) with no fallback to the shim, which Bun cannot run and which would
 * cost a leaked process. Anything else uses activeWorkspace() (cmux). Kept apart from
 * activeWorkspaceName(), whose per-turn triggers (comms triage, meeting jobs) stay off on Windows.
 */
export async function paneWorkspace(env: Record<string, string | undefined> = process.env): Promise<{ title: string; description: string } | null> {
  if (wmuxPaneTarget(env)) return await wmuxActiveWorkspace(env)
  return await activeWorkspace()
}

function comparableWorkstreamTitle(title: string): string {
  return title
    .replace(/^[^\p{L}\p{N}]+/u, '')
    .replace(/^P0\s*·\s*/i, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
}

function yamlScalar(config: string, key: string): string {
  const match = config.match(new RegExp(`^${key}:\\s*(.+?)\\s*$`, 'm'))
  if (!match) return ''
  return match[1].replace(/^(["'])(.*)\1$/, '$2').trim()
}

const workstreamTitleMaps = new Map<string, Map<string, string>>()

function workstreamTitleMap(repoRoot: string): Map<string, string> {
  const cached = workstreamTitleMaps.get(repoRoot)
  if (cached) return cached

  const aliases = new Map<string, string>()
  const root = join(repoRoot, 'workstreams')
  let entries: Array<{ name: string; isDirectory(): boolean }> = []
  try { entries = readdirSync(root, { withFileTypes: true }) } catch {
    workstreamTitleMaps.set(repoRoot, aliases)
    return aliases
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    let config = ''
    try { config = readFileSync(join(root, entry.name, 'config.yaml'), 'utf8') } catch { continue }
    for (const alias of [entry.name, yamlScalar(config, 'name'), yamlScalar(config, 'title')]) {
      const key = comparableWorkstreamTitle(alias)
      if (key) aliases.set(key, entry.name)
    }
  }
  workstreamTitleMaps.set(repoRoot, aliases)
  return aliases
}

/** Resolve a workstream slug from the environment, a workspace path, or a workspace title. */
export function workstreamSlugFromContext(
  ctx: string | null | undefined,
  repoRoot = PM_OS_REPO,
): string | null {
  const envSlug = process.env.PM_OS_WORKSTREAM_SLUG?.trim()
  if (envSlug && /^[a-z0-9-]+$/i.test(envSlug)) return envSlug
  if (!ctx) return null
  const m = ctx.match(/workstreams\/([a-z0-9-]+)/i)
  if (m) return m[1]
  return workstreamTitleMap(repoRoot).get(comparableWorkstreamTitle(ctx)) ?? null
}

/** True if a Bash command string starts with one of the given dangerous tokens. */
export function bashStartsWith(cmd: string, prefixes: string[]): boolean {
  const stripped = cmd.replace(/^\s*(?:sudo\s+|env\s+\w+=\S+\s+)*/, '').trim()
  return prefixes.some(p => stripped === p || stripped.startsWith(p + ' '))
}

/** Read the PM OS todo store for surfacing in plugin notifications. */
export async function readTodos(): Promise<Array<{ id: string; title: string; priority: string; status: string }>> {
  try {
    const raw = await readFile(`${PM_OS_REPO}/system/state/todo.json`, 'utf8')
    const items = JSON.parse(raw) as Array<Record<string, unknown>>
    return items
      .filter(t => (t.status ?? 'open') === 'open')
      .map(t => ({
        id: String(t.id ?? ''),
        title: String(t.title ?? t.summary ?? ''),
        priority: String(t.priority ?? 'P3'),
        status: String(t.status ?? 'open'),
      }))
  } catch {
    return []
  }
}

/** Plugin kill-switch check via configuration. Returns true if the plugin should run. */
export function isPluginEnabled(config: Record<string, unknown>, pluginKey: string): boolean {
  const v = config[`${pluginKey}.disabled`]
  return v !== true && v !== 'true'
}

export const paths = {
  HOME,
  PM_OS_REPO,
  PLUGIN_LOG_DIR,
  PLUGIN_STATE_DIR,
  todoJson: () => `${PM_OS_REPO}/system/state/todo.json`,
  workstream: (slug: string) => join(PM_OS_REPO, 'workstreams', slug),
  systemScripts: () => join(PM_OS_REPO, 'system', 'scripts'),
  daemon: () => join(PM_OS_REPO, 'daemon'),
}
