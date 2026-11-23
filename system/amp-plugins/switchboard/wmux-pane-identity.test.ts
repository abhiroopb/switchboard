import { afterAll, beforeAll, expect, test } from 'bun:test'
import { createServer, type Server } from 'node:net'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { isWorkerWorkspace, paneWorkspace, wmuxActiveWorkspace, wmuxPaneTarget, wmuxSidecarPath } from './lib'

// On Windows the CoS delivery gate, the dispatch guard, and the wait guard cannot run the
// bash cmux shim through execFile, which Bun cannot run. Identity gates ask wmux over its named pipe in-process (workspace.current)
// and read the shim's sidecar for the custom title and the "switchboard worker" description.
// Every test talks to a fake pipe under SWITCHBOARD_TEST_SANDBOX=1, never the live wmux.

const BRIDGE = resolve(import.meta.dir, '../../claude-marketplace/plugins/switchboard/hooks/amp-bridge.ts')
const PLUGINS = resolve(import.meta.dir, '..')
const TOKEN = 'test-token'
const COS_SURFACE = 'surf-cos'
const WORKER_SURFACE = 'surf-worker'
const TODO_SURFACE = 'surf-todo'
const SLOW_SURFACE = 'surf-slow'

const dir = mkdtempSync(join(tmpdir(), 'wmux-pane-identity-'))
const pipe = process.platform === 'win32' ? `\\\\.\\pipe\\switchboard-test-wmux-${randomUUID()}` : join(dir, 'wmux.sock')
const sidecar = join(dir, 'wmux-sidecar.json')
const requests: Array<{ method: string; params: Record<string, unknown>; token: string }> = []
let server: Server

// The live pane's tab ids and titles, as wmux's workspace.current returns them.
const PANES: Record<string, { id: string; title: string }> = {
  [COS_SURFACE]: { id: 'ws-cos', title: '🧭 CoS' },
  [WORKER_SURFACE]: { id: 'ws-worker', title: '📊 Switchboard Maintenance 1' },
  [TODO_SURFACE]: { id: 'ws-todo', title: '✅ Todo' },
}

beforeAll(async () => {
  writeFileSync(sidecar, JSON.stringify({
    version: 1,
    workspaces: {
      'ws-cos': { ref: 'workspace:1', custom_title: null, description: '' },
      'ws-worker': { ref: 'workspace:2', custom_title: '📊 Switchboard Maintenance 1', description: 'switchboard worker rt-00000000000f' },
      'ws-todo': { ref: 'workspace:3', custom_title: '✅ Todo', description: '' },
      'ws-renamed': { ref: 'workspace:4', custom_title: '🧭 CoS', description: '' },
    },
  }))
  server = createServer(socket => {
    let buf = ''
    socket.on('data', chunk => {
      buf += chunk.toString()
      if (!buf.includes('\n')) return
      const req = JSON.parse(buf.slice(0, buf.indexOf('\n')))
      requests.push(req)
      const caller = String(req.params?.caller ?? '')
      if (caller === SLOW_SURFACE) return // never answers
      if (caller === 'surf-split') {
        // The reply in two writes, cut inside the 4-byte 🧭.
        const bytes = Buffer.from(JSON.stringify({ result: { id: 'ws-split', title: '🧭 CoS' }, id: req.id }) + '\n', 'utf8')
        const cut = bytes.indexOf(Buffer.from('🧭', 'utf8')) + 2
        socket.write(bytes.subarray(0, cut))
        setTimeout(() => socket.write(bytes.subarray(cut)), 30)
        return
      }
      let reply: Record<string, unknown>
      if (req.token !== TOKEN) reply = { error: { code: -32001, message: 'bad token' }, id: req.id }
      else if (req.method !== 'workspace.current') reply = { error: { code: -32601, message: 'no method' }, id: req.id }
      else if (caller === 'surf-renamed') reply = { result: { id: 'ws-renamed', title: 'bash', surfaceId: caller }, id: req.id }
      else if (PANES[caller]) reply = { result: { ...PANES[caller], cwd: 'C:/x', shell: 'bash.exe', surfaceId: caller }, id: req.id }
      else reply = { error: { code: -32000, message: 'surface not found' }, id: req.id }
      socket.write(JSON.stringify(reply) + '\n')
    })
    socket.on('error', () => {})
  })
  await new Promise<void>(res => server.listen(pipe, () => res()))
})

afterAll(() => { server?.close() })

function paneEnv(surface: string, extra: Record<string, string> = {}): Record<string, string> {
  return {
    WMUX_SURFACE_ID: surface, SWITCHBOARD_TEST_SANDBOX: '1', SWITCHBOARD_TEST_WMUX_PIPE: pipe,
    WMUX_PIPE_TOKEN: TOKEN, APPDATA: join(dir, 'no-appdata'), SWITCHBOARD_WMUX_SIDECAR: sidecar, ...extra,
  }
}

test('outside a wmux pane there is no target, and the sandbox never reaches the live pipe', () => {
  expect(wmuxPaneTarget({})).toBeNull()
  expect(wmuxPaneTarget({ WMUX_SURFACE_ID: 's', SWITCHBOARD_TEST_SANDBOX: '1', WMUX_PIPE: '\\\\.\\pipe\\wmux' })).toBeNull()
  expect(wmuxPaneTarget({ WMUX_SURFACE_ID: 's', WMUX_PIPE: 'P', WMUX_PIPE_TOKEN: 't', APPDATA: join(dir, 'none') }))
    .toEqual({ surface: 's', pipe: 'P', token: 't' })
  expect(wmuxPaneTarget({ WMUX_SURFACE_ID: 's', WMUX_INSTANCE: 'dev', APPDATA: join(dir, 'none') })!.pipe).toBe('\\\\.\\pipe\\wmux-dev')
})

test('the token file wins over the pane variable, as in cmux-shim.py', () => {
  const appData = join(dir, 'appdata')
  require('node:fs').mkdirSync(join(appData, 'wmux'), { recursive: true })
  writeFileSync(join(appData, 'wmux', 'pipe-token'), 'from-file\n')
  expect(wmuxPaneTarget({ WMUX_SURFACE_ID: 's', WMUX_PIPE_TOKEN: 'from-env', APPDATA: appData })!.token).toBe('from-file')
})

test('the sandbox never reads the live token file', () => {
  const appData = join(dir, 'appdata-sandbox')
  require('node:fs').mkdirSync(join(appData, 'wmux'), { recursive: true })
  writeFileSync(join(appData, 'wmux', 'pipe-token'), 'live-secret\n')
  const target = wmuxPaneTarget({ WMUX_SURFACE_ID: 's', SWITCHBOARD_TEST_SANDBOX: '1', SWITCHBOARD_TEST_WMUX_PIPE: 'P', APPDATA: appData })
  expect(target!.token).toBe('')
})

test('a reply cut inside an emoji decodes whole', async () => {
  expect((await wmuxActiveWorkspace(paneEnv('surf-split')))!.title).toBe('🧭 CoS')
})

test('a worker with a CoS-like title stays a worker without a sidecar entry, and an unreadable sidecar gives null', async () => {
  // No sidecar file: the live title alone would read as CoS; SWITCHBOARD_WORKER_ID marks the worker.
  const noSidecar = { SWITCHBOARD_WMUX_SIDECAR: join(dir, 'absent.json') }
  expect(await wmuxActiveWorkspace(paneEnv(COS_SURFACE, noSidecar))).toEqual({ title: '🧭 CoS', description: '' })
  const worker = await wmuxActiveWorkspace(paneEnv(COS_SURFACE, { ...noSidecar, SWITCHBOARD_WORKER_ID: 'worker-1', SWITCHBOARD_WORKER_TASK_ID: 'rt-000000000010' }))
  expect(worker).toEqual({ title: '🧭 CoS', description: 'switchboard worker rt-000000000010' })
  expect(isWorkerWorkspace(worker)).toBe(true)
  const broken = join(dir, 'broken-sidecar.json')
  writeFileSync(broken, '{"workspaces": {')
  expect(await wmuxActiveWorkspace(paneEnv(COS_SURFACE, { SWITCHBOARD_WMUX_SIDECAR: broken }))).toBeNull()
})

test('a failed lookup in a wmux pane is null, with no fallback to the cmux shim', async () => {
  const marker = join(dir, 'shim-ran')
  const shim = join(dir, process.platform === 'win32' ? 'fake-cmux.cmd' : 'fake-cmux')
  writeFileSync(shim, process.platform === 'win32' ? `@echo off\r\necho ran> "${marker}"\r\n` : `#!/bin/sh\necho ran > "${marker}"\n`, { mode: 0o755 })
  const saved = process.env.CMUX_BIN
  process.env.CMUX_BIN = shim
  try {
    expect(await paneWorkspace(paneEnv('surf-gone'))).toBeNull()
    expect(require('node:fs').existsSync(marker)).toBe(false)
  } finally {
    if (saved === undefined) delete process.env.CMUX_BIN
    else process.env.CMUX_BIN = saved
  }
})

test('the sidecar path follows the shim: override, state home, then USERPROFILE on Windows', () => {
  expect(wmuxSidecarPath({ SWITCHBOARD_WMUX_SIDECAR: 'X' })).toBe('X')
  expect(wmuxSidecarPath({ SWITCHBOARD_STATE_HOME: 'C:/s' })).toBe('C:/s/wmux-sidecar.json')
  expect(wmuxSidecarPath({ HOME: '/c/Users/a', USERPROFILE: 'C:\\Users\\a' }, 'win32')).toBe('C:/Users/a/.switchboard/wmux-sidecar.json')
  expect(wmuxSidecarPath({ HOME: '/Users/a' }, 'darwin')).toBe('/Users/a/.switchboard/wmux-sidecar.json')
})

test('the CoS pane resolves to its wmux title with one pipe call, fast', async () => {
  const before = requests.length
  const t = performance.now()
  const ws = await wmuxActiveWorkspace(paneEnv(COS_SURFACE))
  expect(performance.now() - t).toBeLessThan(500)
  expect(ws).toEqual({ title: '🧭 CoS', description: '' })
  expect(requests.length - before).toBe(1)
  expect(requests.at(-1)).toMatchObject({ method: 'workspace.current', params: { caller: COS_SURFACE }, token: TOKEN })
})

test('a worker pane carries its dispatcher description, and a custom title wins over the live one', async () => {
  expect(await wmuxActiveWorkspace(paneEnv(WORKER_SURFACE))).toEqual({ title: '📊 Switchboard Maintenance 1', description: 'switchboard worker rt-00000000000f' })
  expect(await wmuxActiveWorkspace(paneEnv('surf-renamed'))).toEqual({ title: '🧭 CoS', description: '' })
})

test('an unknown pane, a bad token, a missing pipe, or a silent pipe gives null within the timeout', async () => {
  expect(await wmuxActiveWorkspace(paneEnv('surf-gone'))).toBeNull()
  expect(await wmuxActiveWorkspace(paneEnv(COS_SURFACE, { WMUX_PIPE_TOKEN: 'wrong' }))).toBeNull()
  const missing = process.platform === 'win32' ? `\\\\.\\pipe\\switchboard-test-missing-${randomUUID()}` : join(dir, 'missing.sock')
  expect(await wmuxActiveWorkspace(paneEnv(COS_SURFACE, { SWITCHBOARD_TEST_WMUX_PIPE: missing }))).toBeNull()
  const t = performance.now()
  expect(await wmuxActiveWorkspace(paneEnv(SLOW_SURFACE))).toBeNull()
  expect(performance.now() - t).toBeLessThan(3_000)
}, 10_000)

// ── The real bridge, the way hooks.json runs it, with no title override ─────────────

async function bridge(surface: string, tool: string, input: Record<string, unknown>) {
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.startsWith('SWITCHBOARD_') || k.startsWith('CMUX_') || k.startsWith('WMUX')) continue
    base[k] = v
  }
  const home = mkdtempSync(join(tmpdir(), 'wmux-pane-bridge-'))
  const started = performance.now()
  const proc = Bun.spawn(['bun', BRIDGE, 'pre-tool-use'], {
    stdin: new TextEncoder().encode(JSON.stringify({
      session_id: 'wmux-pane-test', transcript_path: join(home, 'none.jsonl'), cwd: 'C:/Users/someone/Development/switchboard',
      hook_event_name: 'PreToolUse', tool_name: tool, tool_input: input,
    })),
    stdout: 'pipe', stderr: 'pipe',
    env: {
      ...base, ...paneEnv(surface), HOME: home, USERPROFILE: home, SWITCHBOARD_AMP_PLUGINS_DIR: PLUGINS,
      SWITCHBOARD_STATE_DIR: join(home, 'state'), SWITCHBOARD_ROOT: 'C:/Users/someone/Development/switchboard',
      // No cmux on this path: the old lookup returned null here, so every gate stayed open.
      CMUX_BIN: join(home, 'no-cmux'),
    },
  })
  const killer = setTimeout(() => proc.kill(), 20_000)
  const out = await new Response(proc.stdout).text()
  await proc.exited
  clearTimeout(killer)
  const last = out.trim().split('\n').pop() ?? ''
  const decision = last ? JSON.parse(last).hookSpecificOutput?.permissionDecision ?? 'allow' : 'allow'
  return { decision, ms: performance.now() - started }
}

const REPO_FILE = 'C:/Users/someone/Development/switchboard/projects/x/draft.md'
// Under this machine's real temp folder, which the gate allowlists.
const SCRATCH = join(tmpdir(), 'claude', 'abc', 'scratchpad', 'brief.txt').replaceAll('\\', '/')

test('bridge: the CoS delivery gate asks on a CoS pane write outside orchestration paths', async () => {
  const write = await bridge(COS_SURFACE, 'Write', { file_path: REPO_FILE, content: 'draft' })
  expect(write.decision).toBe('ask')
  expect(write.ms).toBeLessThan(5_000)
  expect((await bridge(COS_SURFACE, 'Edit', { file_path: REPO_FILE, old_string: 'a', new_string: 'b' })).decision).toBe('ask')
  expect((await bridge(COS_SURFACE, 'Bash', { command: 'echo hi > projects/x/out.txt', description: 't' })).decision).toBe('ask')
}, 60_000)

test('bridge: the CoS pane still writes scratch freely, and the wait guard blocks a long sleep', async () => {
  expect((await bridge(COS_SURFACE, 'Write', { file_path: SCRATCH, content: 'Workstream: x\n' })).decision).toBe('allow')
  expect((await bridge(COS_SURFACE, 'Bash', { command: 'sleep 600', description: 't' })).decision).toBe('deny')
}, 60_000)

test('bridge: a worker pane and an owner tab keep writing without a prompt', async () => {
  expect((await bridge(WORKER_SURFACE, 'Write', { file_path: REPO_FILE, content: 'draft' })).decision).toBe('allow')
  expect((await bridge(WORKER_SURFACE, 'Bash', { command: 'sleep 600', description: 't' })).decision).toBe('allow')
  expect((await bridge(TODO_SURFACE, 'Write', { file_path: REPO_FILE, content: 'draft' })).decision).toBe('allow')
}, 60_000)

test('bridge: a wmux lookup that fails leaves the gate open, as a cmux failure always did', async () => {
  expect((await bridge('surf-gone', 'Write', { file_path: REPO_FILE, content: 'draft' })).decision).toBe('allow')
}, 30_000)
