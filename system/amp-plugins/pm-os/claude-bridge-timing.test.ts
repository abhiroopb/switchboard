import { expect, test } from 'bun:test'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { isCosAllowlistedPath } from '../pm-os-guardrails'

// A Write or Edit from a Windows CoS tab must not wait out the 30 s PreToolUse timeout,
// as it does when isCosAllowlistedPath reads a `C:/…` path as relative, prepends the
// repo root, and recurses forever. These tests run the real bridge the way
// hooks.json runs it and fail if a file save or a command gets slow again.

const BRIDGE = resolve(import.meta.dir, '../../claude-marketplace/plugins/pm-os/hooks/amp-bridge.ts')
const HOOKS = resolve(import.meta.dir, '../../claude-marketplace/plugins/pm-os/hooks/hooks.json')
const PLUGINS = resolve(import.meta.dir, '..')
// Normal runs take about 0.2 s on Windows. The bound leaves room for a cold
// Bun start on a loaded machine and still catches a hang long before the hook timeout.
const BUDGET_MS = 5_000

async function timedPreToolUse(tool: string, input: Record<string, unknown>, title: string, cwd = 'C:/Users/someone/Development/pm-os') {
  const dir = mkdtempSync(join(tmpdir(), 'claude-bridge-timing-'))
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.startsWith('PM_OS_') || k.startsWith('CMUX_') || k.startsWith('WMUX')) continue
    base[k] = v
  }
  const started = performance.now()
  const proc = Bun.spawn(['bun', BRIDGE, 'pre-tool-use'], {
    stdin: new TextEncoder().encode(JSON.stringify({
      session_id: 'timing-test', transcript_path: join(dir, 'none.jsonl'), cwd,
      hook_event_name: 'PreToolUse', tool_name: tool, tool_input: input,
    })),
    stdout: 'pipe', stderr: 'pipe',
    env: {
      ...base, HOME: dir, USERPROFILE: dir, PM_OS_AMP_PLUGINS_DIR: PLUGINS, PM_OS_STATE_DIR: join(dir, 'state'),
      PM_OS_REPO: 'C:/Users/someone/Development/pm-os', PM_OS_GUARDRAILS_WS_TITLE_OVERRIDE: title,
    },
  })
  const killer = setTimeout(() => proc.kill(), 20_000)
  const out = await new Response(proc.stdout).text()
  const code = await proc.exited
  clearTimeout(killer)
  const ms = performance.now() - started
  return { ms, code, hook: out.trim() ? JSON.parse(out.trim().split('\n').pop()!).hookSpecificOutput ?? {} : {} }
}

const SCRATCH = 'C:/Users/someone/AppData/Local/Temp/claude/abc/def/scratchpad/brief.txt'
const REPO_FILE = 'C:/Users/someone/Development/pm-os/projects/x/draft.md'

test('isCosAllowlistedPath returns at once for Windows absolute and relative paths', () => {
  for (const p of [SCRATCH, REPO_FILE, 'C:\\Users\\someone\\notes\\a.md', 'projects/x/draft.md', 'D:/other/file.txt']) {
    const t = performance.now()
    isCosAllowlistedPath(p)
    expect(performance.now() - t).toBeLessThan(50)
  }
})

for (const title of ['🧭 CoS', '🔧 PM OS Maintenance 2']) {
  test(`a Write to a Windows scratch path returns fast (${title})`, async () => {
    const r = await timedPreToolUse('Write', { file_path: SCRATCH, content: 'Workstream: x\n' }, title)
    expect(r.code).toBe(0)
    expect(r.ms).toBeLessThan(BUDGET_MS)
  }, 30_000)

  test(`an Edit to a Windows repo path returns fast (${title})`, async () => {
    const r = await timedPreToolUse('Edit', { file_path: REPO_FILE, old_string: 'a', new_string: 'b' }, title)
    expect(r.code).toBe(0)
    expect(r.ms).toBeLessThan(BUDGET_MS)
  }, 30_000)

  test(`a Bash redirect to a relative path returns fast (${title})`, async () => {
    const r = await timedPreToolUse('Bash', { command: 'echo hi > notes/out.txt', description: 'test' }, title)
    expect(r.code).toBe(0)
    expect(r.ms).toBeLessThan(BUDGET_MS)
  }, 30_000)
}

test('the CoS delivery gate still asks on a CoS write outside orchestration paths', async () => {
  const r = await timedPreToolUse('Write', { file_path: REPO_FILE, content: 'draft' }, '🧭 CoS')
  expect(r.hook.permissionDecision).toBe('ask')
  expect(r.ms).toBeLessThan(BUDGET_MS)
}, 30_000)

test('hooks.json caps the blocking pre-tool-use step at 10 s, not 30 s', () => {
  const hooks = JSON.parse(readFileSync(HOOKS, 'utf8')).hooks.PreToolUse as Array<{ hooks: Array<{ command: string; timeout?: number }> }>
  const bridge = hooks.flatMap(h => h.hooks).find(h => h.command.includes('amp-bridge.ts') && h.command.includes('pre-tool-use'))!
  expect(bridge).toBeDefined()
  expect(bridge.timeout).toBeLessThanOrEqual(10)
})
