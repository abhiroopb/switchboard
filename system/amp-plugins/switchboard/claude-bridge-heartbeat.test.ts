import { expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { readTranscript } from '../../claude-marketplace/plugins/switchboard/hooks/amp-bridge'
import { ownWorkerTask } from '../task-liveness'

// The hook bridge must not count Claude's tool_result rows (stored as type "user")
// as user messages, or task-liveness's `messages({ roles: ['user'], limit: 20 })`
// loses the seed prompt after about 20 tool calls and its heartbeat freezes. These
// tests run the real bridge as Claude runs it (one bun process per hook event).

const BRIDGE = resolve(import.meta.dir, '../../claude-marketplace/plugins/switchboard/hooks/amp-bridge.ts')
const PLUGINS = resolve(import.meta.dir, '..')
const TASK = 'rt-000000000007'
const ATTEMPT = `${TASK}.a1.abcdef`
const SESSION = '0fd3afb0-1234-4abc-8def-0123456789ab'

function transcript(dir: string, opts: { seed: boolean; toolCalls: number }): string {
  const rows: unknown[] = []
  const seed = `You are switchboard worker for task ${TASK}.\nRouter return contract: task ${TASK}.\nbash report-to-cos.sh --task-id ${TASK} --attempt-id ${ATTEMPT}`
  rows.push({ type: 'user', uuid: 'u0', message: { role: 'user', content: opts.seed ? seed : 'hello' } })
  for (let i = 0; i < opts.toolCalls; i++) {
    rows.push({ type: 'assistant', uuid: `a${i}`, message: { role: 'assistant', content: [{ type: 'tool_use', id: `t${i}`, name: 'Bash', input: { command: 'true' } }] } })
    rows.push({ type: 'user', uuid: `r${i}`, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: `t${i}`, content: 'ok' }] } })
  }
  const path = join(dir, 'transcript.jsonl')
  writeFileSync(path, rows.map(r => JSON.stringify(r)).join('\n') + '\n')
  return path
}

function harness() {
  const dir = mkdtempSync(join(tmpdir(), 'claude-bridge-hb-'))
  // lib.ts derives SWITCHBOARD_REPO from HOME and runs the ledger with that cwd.
  mkdirSync(join(dir, 'Development', 'switchboard'), { recursive: true })
  const calls = join(dir, 'ledger-calls.txt')
  const ledger = join(dir, 'fake-ledger.py')
  writeFileSync(ledger, `#!/usr/bin/env python3
import json, sys
with open(${JSON.stringify(calls)}, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
print(json.dumps({"phase": "running", "attempt_id": ${JSON.stringify(ATTEMPT)}}))
`)
  chmodSync(ledger, 0o755)
  const probe = join(dir, 'fake-probe.py')
  writeFileSync(probe, 'print("{}")\n')
  return { dir, calls, ledger, probe }
}

async function runBridge(h: ReturnType<typeof harness>, transcriptPath: string, env: Record<string, string>) {
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined || k.startsWith('SWITCHBOARD_') || k.startsWith('CMUX_')) continue
    base[k] = v
  }
  const proc = Bun.spawn(['bun', BRIDGE, 'post-tool-use'], {
    stdin: new TextEncoder().encode(JSON.stringify({
      session_id: SESSION, transcript_path: transcriptPath, cwd: h.dir,
      hook_event_name: 'PostToolUse', tool_name: 'Bash', tool_input: { command: 'true' }, tool_response: 'ok',
    })),
    stdout: 'pipe', stderr: 'pipe',
    env: {
      ...base, HOME: h.dir, SWITCHBOARD_AMP_PLUGINS_DIR: PLUGINS, SWITCHBOARD_BRIDGE_ONLY: 'task-liveness',
      TASK_LIVENESS_LEDGER_SCRIPT: h.ledger, TASK_LIVENESS_PROBE_SCRIPT: h.probe, TASK_LIVENESS_CMUX_BIN: '/usr/bin/false',
      // Windows throttles results with a stamp in the temp folder; each call here is its own run.
      SWITCHBOARD_TASK_RESULT_THROTTLE_MS: '0',
      ...env,
    },
  })
  await proc.exited
  return existsSync(h.calls) ? readFileSync(h.calls, 'utf8').trim().split('\n').filter(Boolean) : []
}

test('readTranscript drops tool_result-only rows so the seed stays a recent user message', () => {
  const h = harness()
  const rows = readTranscript(transcript(h.dir, { seed: true, toolCalls: 25 }))
  const users = rows.filter(r => r.role === 'user')
  expect(users.length).toBe(1)
  expect(String(users[0].content[0].text)).toContain(`Router return contract: task ${TASK}`)
  expect(rows.filter(r => r.role === 'assistant').length).toBe(25)
})

test('after 25 tool calls the bridge still acks the seeded task from the transcript', async () => {
  const h = harness()
  const calls = await runBridge(h, transcript(h.dir, { seed: true, toolCalls: 25 }), {})
  expect(calls.some(c => c.startsWith(`ack --task-id ${TASK} --role worker --thread-id ${SESSION}`))).toBe(true)
  expect(calls.some(c => c.startsWith(`heartbeat --task-id ${TASK}`))).toBe(true)
}, 30_000)

test('a worker acks its own task from SWITCHBOARD_WORKER_TASK_ID with no seed in the transcript', async () => {
  const h = harness()
  const calls = await runBridge(h, transcript(h.dir, { seed: false, toolCalls: 3 }), {
    SWITCHBOARD_WORKER_TASK_ID: TASK, SWITCHBOARD_WORKER_ATTEMPT_ID: ATTEMPT,
  })
  const ack = calls.find(c => c.startsWith(`ack --task-id ${TASK}`))
  expect(ack).toBeDefined()
  expect(ack).toContain(`--thread-id ${SESSION}`)
  expect(ack).toContain(`--attempt-id ${ATTEMPT}`)
}, 30_000)

test('the env ack fires only from the session dispatch launched', async () => {
  const env = { SWITCHBOARD_WORKER_TASK_ID: TASK, SWITCHBOARD_WORKER_ATTEMPT_ID: ATTEMPT }
  const w = harness()
  const own = await runBridge(w, transcript(w.dir, { seed: false, toolCalls: 3 }), { ...env, SWITCHBOARD_WORKER_SESSION_ID: SESSION })
  expect(own.some(c => c.startsWith(`ack --task-id ${TASK}`))).toBe(true)
  // A child `claude -p` inherits the env but runs under its own session id.
  const h = harness()
  const child = await runBridge(h, transcript(h.dir, { seed: false, toolCalls: 3 }), { ...env, SWITCHBOARD_WORKER_SESSION_ID: '0fd3afb0-9999-4abc-8def-0123456789ab' })
  expect(child.some(c => c.startsWith('ack '))).toBe(false)
}, 30_000)

test('ownWorkerTask reads only a well-formed task id and its own attempt id', () => {
  expect(ownWorkerTask({})).toBeNull()
  expect(ownWorkerTask({ SWITCHBOARD_WORKER_TASK_ID: 'nope' })).toBeNull()
  expect(ownWorkerTask({ SWITCHBOARD_WORKER_TASK_ID: TASK, SWITCHBOARD_WORKER_ATTEMPT_ID: ATTEMPT }))
    .toEqual({ taskId: TASK, attemptId: ATTEMPT, role: 'worker' })
  expect(ownWorkerTask({ SWITCHBOARD_WORKER_TASK_ID: TASK, SWITCHBOARD_WORKER_ATTEMPT_ID: 'rt-000000000008.a1.8f066b' }))
    .toEqual({ taskId: TASK, attemptId: '', role: 'worker' })
})
