import { expect, test } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { helperWord } from './rm-guard'

// In a Claude tab the bridge turns the Rule 3 Trash rewrite
// into PreToolUse `allow` + updatedInput, so the worker never waits on a
// prompt; a delete that still asks leaves a permission-asks record that
// permission_relay.py reads for its CoS callback. Runs the real bridge the way
// hooks.json runs it (one bun process per hook event).

const BRIDGE = resolve(import.meta.dir, '../../claude-marketplace/plugins/pm-os/hooks/amp-bridge.ts')
const PLUGINS = resolve(import.meta.dir, '..')
const HELPER = resolve(import.meta.dir, '../../scripts/pm-os-trash.py')
const HW = helperWord(HELPER)!
const SESSION = '0fd3afb0-4321-4abc-8def-0123456789ab'

// platform pins the rm guard (PM_OS_RM_GUARD_PLATFORM): an unrewritable delete asks on a Mac and is refused on Windows.
async function preToolUse(command: string, cwd = '/Users/someone/proj', extra: Record<string, unknown> = {}, platform = 'darwin') {
  const dir = mkdtempSync(join(tmpdir(), 'claude-bridge-rm-'))
  mkdirSync(join(dir, 'Development', 'pm-os'), { recursive: true })
  const state = join(dir, 'state')
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.startsWith('PM_OS_') || k.startsWith('CMUX_')) continue
    base[k] = v
  }
  const proc = Bun.spawn(['bun', BRIDGE, 'pre-tool-use'], {
    stdin: new TextEncoder().encode(JSON.stringify({
      session_id: SESSION, transcript_path: join(dir, 'none.jsonl'), cwd,
      hook_event_name: 'PreToolUse', tool_name: 'Bash', tool_input: { command, description: 'test' }, ...extra,
    })),
    stdout: 'pipe', stderr: 'pipe',
    env: {
      ...base, HOME: dir, PM_OS_AMP_PLUGINS_DIR: PLUGINS, PM_OS_BRIDGE_ONLY: 'pm-os-guardrails',
      PM_OS_TRASH_HELPER: HELPER, PM_OS_STATE_DIR: state,
      PM_OS_GUARDRAILS_WS_TITLE_OVERRIDE: '🔧 PM OS Maintenance 2',
      PM_OS_WORKER_TASK_ID: 'rt-000000000007', CMUX_WORKSPACE_ID: 'WS-TEST', PM_OS_RM_GUARD_PLATFORM: platform,
    },
  })
  const out = await new Response(proc.stdout).text()
  await proc.exited
  const askPath = join(state, 'worker-dispatch', 'permission-asks', `${SESSION}.json`)
  return {
    hook: out.trim() ? JSON.parse(out).hookSpecificOutput ?? {} : {},
    ask: existsSync(askPath) ? JSON.parse(readFileSync(askPath, 'utf8')) : null,
  }
}

test('an outside delete comes back as allow with the Trash helper and no workdir leak', async () => {
  // HOME is a tmpdir here, so `~/…` would be a temp path; use an outside one.
  const { hook, ask } = await preToolUse('rm -rf /Users/someone/scratch-folder')
  expect(hook.permissionDecision).toBe('allow')
  expect(hook.updatedInput).toEqual({ command: `${HW} -rf /Users/someone/scratch-folder`, description: 'test' })
  expect(ask).toBeNull()
}, 30_000)

test('a delete that cannot be rewritten asks and records the ask for the CoS relay', async () => {
  const { hook, ask } = await preToolUse('sudo rm -rf /opt/example')
  expect(hook.permissionDecision).toBe('ask')
  expect(ask).toMatchObject({
    session_id: SESSION, tool: 'Bash', command: 'sudo rm -rf /opt/example',
    workspace_id: 'WS-TEST', task_id: 'rt-000000000007', cwd: '/Users/someone/proj',
  })
  expect(String(ask.reason)).toContain('/opt/example')
}, 30_000)

test('a heredoc delete run on a remote box passes with no decision', async () => {
  const cmd = readFileSync(`${import.meta.dir}/test-fixtures/rm-heredoc-remote.sh`, 'utf8')
  const { hook, ask } = await preToolUse(cmd)
  expect(hook.permissionDecision ?? 'allow').toBe('allow')
  expect(hook.updatedInput).toBeUndefined()
  expect(ask).toBeNull()
}, 30_000)

// A tall subagent command whose rm targets sit in its Claude scratchpad under /private/tmp.
const TALL = JSON.parse(readFileSync(resolve(import.meta.dir, '../../scripts/fixtures/permission_relay_tall_command.json'), 'utf8'))

test('a tall scratchpad delete passes with no prompt under the current rule', async () => {
  const { hook, ask } = await preToolUse(TALL.command, '/Users/tester/Development/pm-os/.worktrees/rt-000000000009/workstreams/pm-os-maintenance')
  expect(hook.permissionDecision ?? 'allow').toBe('allow')
  expect(hook.updatedInput).toBeUndefined()
  expect(ask).toBeNull()
}, 30_000)

test('a subagent ask records the subagent type for the CoS relay', async () => {
  const { hook, ask } = await preToolUse('sudo rm -rf /opt/example', '/Users/someone/proj',
    { agent_id: 'acd32a785fb8f2e64', agent_type: 'pm-os:code-reviewer' })
  expect(hook.permissionDecision).toBe('ask')
  expect(ask).toMatchObject({ command: 'sudo rm -rf /opt/example', agent_type: 'pm-os:code-reviewer' })
}, 30_000)

test('on Windows a delete that cannot be rewritten is refused, never asked', async () => {
  // No Recycle Bin route exists for sudo, so the guard refuses rather than prompt.
  const { hook, ask } = await preToolUse('sudo rm -rf /opt/example', '/Users/someone/proj', {}, 'win32')
  expect(hook.permissionDecision).toBe('deny')
  expect(String(hook.permissionDecisionReason)).toContain('Recycle Bin')
  expect(String(hook.permissionDecisionReason)).toContain('/opt/example')
  expect(ask).toBeNull()
}, 30_000)
