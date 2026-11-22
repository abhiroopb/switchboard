import { expect, test } from 'bun:test'
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { readTranscript, stopEvent } from '../../claude-marketplace/plugins/pm-os/hooks/amp-bridge'
import { finalAssistantText } from '../task-liveness'

// Claude workers stopped reporting back while Amp workers still did.
// The bridge replayed Claude's Stop as agent.end with no status and no messages,
// so every agent.end plugin gated on status 'done' (workspace-report,
// review-lane, cos-finished, workstream-lifecycle) never ran on Claude, and a
// worker that stopped for the owner's approval sent nothing.

const BRIDGE = resolve(import.meta.dir, '../../claude-marketplace/plugins/pm-os/hooks/amp-bridge.ts')
const PLUGINS = resolve(import.meta.dir, '..')
const SESSION = '0fd3afb0-1234-4abc-8def-0123456789ab'
const ASK = 'Draft is ready.\n\n> 1. **Go (recommended):** I publish it.\n> 2. **Hold:** I wait for the reviewer.'

function transcriptFile(dir: string, finalText: string | null): string {
  const rows: unknown[] = [
    { type: 'user', uuid: 'u0', message: { role: 'user', content: 'first prompt' } },
    { type: 'assistant', uuid: 'a0', message: { role: 'assistant', content: [{ type: 'text', text: 'first answer' }] } },
    { type: 'user', uuid: 'u1', message: { role: 'user', content: 'update the event page' } },
    { type: 'user', uuid: 'm1', isMeta: true, message: { role: 'user', content: 'skill body, not a prompt' } },
    { type: 'assistant', uuid: 'a1', message: { role: 'assistant', content: [{ type: 'tool_use', id: 't1', name: 'Bash', input: {} }] } },
    { type: 'user', uuid: 'r1', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } },
  ]
  if (finalText) rows.push({ type: 'assistant', uuid: 'a2', message: { role: 'assistant', content: [{ type: 'text', text: finalText }] } })
  const path = join(dir, 'transcript.jsonl')
  writeFileSync(path, rows.map(r => JSON.stringify(r)).join('\n') + '\n')
  return path
}

test('stopEvent is Amp-shaped: status done, the prompt, and this turn only', () => {
  // Wrong impl: {id, thread} only, so `event.status === "done"` gates never pass.
  const dir = mkdtempSync(join(tmpdir(), 'claude-parity-'))
  const event = stopEvent(readTranscript(transcriptFile(dir, ASK)), {}, SESSION, { id: SESSION, title: '' })
  expect(event.status).toBe('done')
  expect(event.message).toBe('update the event page')   // the meta row and tool results are not the prompt
  const messages = event.messages as Array<{ role: string }>
  expect(messages[0].role).toBe('user')
  expect(messages.length).toBe(3)                        // prompt, tool call, final text; not the earlier turn
  expect(finalAssistantText(messages)).toBe(ASK)
})

test('stopEvent adds last_assistant_message when the transcript has not flushed it yet', () => {
  // Wrong impl: the Stop hook fires before the final row is written, the turn
  // reads as ending on a tool call, and the ask is never seen.
  const dir = mkdtempSync(join(tmpdir(), 'claude-parity-'))
  const event = stopEvent(readTranscript(transcriptFile(dir, null)), { last_assistant_message: ASK }, SESSION, { id: SESSION, title: '' })
  expect(finalAssistantText(event.messages as unknown[])).toBe(ASK)
  const again = stopEvent(readTranscript(transcriptFile(dir, ASK)), { last_assistant_message: ASK }, SESSION, { id: SESSION, title: '' })
  expect((again.messages as unknown[]).length).toBe(3)  // never added twice
})

test('a Claude Stop runs task-liveness park with the final text', async () => {
  // Wrong impl: the park hook exists but the bridge never feeds it a done turn.
  const dir = mkdtempSync(join(tmpdir(), 'claude-parity-'))
  mkdirSync(join(dir, 'Development', 'pm-os'), { recursive: true })
  // On Windows runDetached starts the park through <repo>/system/scripts/no_window.py, as a real install has it.
  mkdirSync(join(dir, 'Development', 'pm-os', 'system', 'scripts'), { recursive: true })
  copyFileSync(resolve(import.meta.dir, '../../scripts/no_window.py'), join(dir, 'Development', 'pm-os', 'system', 'scripts', 'no_window.py'))
  const calls = join(dir, 'park-calls.txt')
  const script = join(dir, 'fake-turn-wait.py')
  writeFileSync(script, `#!/usr/bin/env python3
import sys
args = sys.argv[1:]
text = open(args[args.index("--text-file") + 1]).read()
with open(${JSON.stringify(calls)}, "a") as fh:
    fh.write(" ".join(args[:3]) + "\\n" + text + "\\n")
`)
  chmodSync(script, 0o755)
  const ledger = join(dir, 'fake-ledger.py')
  writeFileSync(ledger, 'print("{}")\n')
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.startsWith('PM_OS_') || k.startsWith('CMUX_')) continue
    base[k] = v
  }
  const proc = Bun.spawn(['bun', BRIDGE, 'stop'], {
    stdin: new TextEncoder().encode(JSON.stringify({
      session_id: SESSION, transcript_path: transcriptFile(dir, ASK), cwd: dir, hook_event_name: 'Stop',
    })),
    stdout: 'pipe', stderr: 'pipe',
    env: {
      ...base, HOME: dir, PM_OS_AMP_PLUGINS_DIR: PLUGINS, PM_OS_BRIDGE_ONLY: 'task-liveness',
      TASK_LIVENESS_LEDGER_SCRIPT: ledger, TASK_LIVENESS_PROBE_SCRIPT: ledger, TASK_LIVENESS_CMUX_BIN: '/usr/bin/false',
      TURN_WAIT_SCRIPT: script, TURN_WAIT_TEXT_DIR: join(dir, 'turn-wait'),
    },
  })
  await proc.exited
  for (let i = 0; i < 50 && !existsSync(calls); i++) await Bun.sleep(100)
  const out = readFileSync(calls, 'utf8')
  expect(out).toContain(`park --thread-id ${SESSION}`)
  expect(out).toContain('**Hold:** I wait for the reviewer.')
}, 30_000)

test('an owner-tab turn waiting on the owner is a needs-ruling callback once, never twice', async () => {
  // Wrong impls: (a) workspace-report mirrors a parked owner-tab turn as a
  // compact digest line, so CoS never sees the ask; (b) an owner tab that a
  // dispatch was sent to gets one callback from task-liveness park and a
  // second from workspace-report for the same turn.
  const { waitsOnOwner } = await import('../workspace-report')
  const dir = mkdtempSync(join(tmpdir(), 'claude-parity-'))
  const ledger = join(dir, 'running-tasks.json')
  writeFileSync(ledger, JSON.stringify({ tasks: [{ id: 'rt-000000000007', status: 'active', thread_id: 'sess-bound' }] }))
  const script = resolve(import.meta.dir, '../../scripts/turn_wait.py')
  const classify = async (args: string[]) => {
    const proc = Bun.spawn(['python3', script, ...args], { stdout: 'pipe', env: { ...process.env, RUNNING_TASKS_FILE: ledger, PM_OS_STATE_DIR: dir } })
    const stdout = await new Response(proc.stdout).text()
    return { code: await proc.exited, stdout }
  }
  expect(await waitsOnOwner(ASK, 'sess-owner', classify)).toBe(true)
  expect(await waitsOnOwner('Done. Landed on main.', 'sess-owner', classify)).toBe(false)
  expect(await waitsOnOwner(ASK, 'sess-bound', classify)).toBe(false)
  expect(await waitsOnOwner(ASK, 'sess-owner', async () => ({ code: 1, stdout: '' }))).toBe(false)
}, 30_000)
