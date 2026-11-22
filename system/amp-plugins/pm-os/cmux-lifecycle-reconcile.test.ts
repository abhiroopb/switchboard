import { expect, mock, test } from 'bun:test'
import { BADGE, CompletionRepair, RETRY_DELAYS_MS, storedState } from '../cmux-lifecycle-reconcile'

const end = { thread: 'T-owner', turn: 'turn-1', outcome: 'done' }
const immediate = { delaysMs: [0, 0, 0, 0, 0] }

test('late idle from a fresh handle repairs a lost completion and retries failed delivery', async () => {
  let handle = { state: 'running' }
  const read = mock(async () => handle.state)
  let acknowledged = false
  const send = mock(async () => acknowledged)
  const repair = new CompletionRepair(() => end.thread, read, send, immediate)
  repair.end(end)
  await repair.poll()
  expect(send).not.toHaveBeenCalled()
  handle = { state: 'idle' } // Replaced Amp handle; no subscription callback.
  await repair.poll()
  expect(send).toHaveBeenCalledTimes(1)
  await repair.poll() // RPC failed or no cmux read-back yet.
  expect(send).toHaveBeenCalledTimes(2)
  acknowledged = true
  await repair.poll()
  await repair.poll()
  expect(send).toHaveBeenCalledTimes(3)
  expect(send.mock.calls[0].slice(0, 2)).toEqual([end, 'idle'])
  expect(send.mock.calls.map(call => call[3])).toEqual([0, 1, 2]) // the sender sees the attempt number
})

test.each(['running', 'awaiting-approval', 'unknown'])('%s is never treated as completion', async state => {
  const send = mock(async () => true)
  const repair = new CompletionRepair(() => end.thread, async () => state, send, immediate)
  repair.end(end)
  await repair.poll()
  expect(send).not.toHaveBeenCalled()
})

test('new turn or changed active thread fences a delayed idle read', async () => {
  for (const switchThread of [false, true]) {
    let resolve!: (value: string) => void
    let current = end.thread
    const send = mock(async () => true)
    const repair = new CompletionRepair(() => current, () => new Promise(r => { resolve = r }), send, immediate)
    repair.end(end)
    const poll = repair.poll()
    if (switchThread) current = 'T-other'
    else repair.start()
    resolve('idle')
    await poll
    expect(send).not.toHaveBeenCalled()
  }
})

test('delivery can recheck the generation after I/O and cannot clear a newer completion', async () => {
  let release!: () => void
  let valid!: () => boolean
  const send = mock(async (_end, _state, current) => {
    valid = current
    await new Promise<void>(resolve => { release = resolve })
    return true
  })
  const repair = new CompletionRepair(() => end.thread, async () => 'idle', send, immediate)
  repair.end(end)
  const poll = repair.poll()
  await Bun.sleep(0)
  repair.start()
  repair.end({ ...end, turn: 'turn-2' })
  expect(valid()).toBe(false)
  release()
  await poll
  const next = repair.poll()
  await Bun.sleep(0)
  expect(send.mock.calls[1][0].turn).toBe('turn-2')
  release()
  await next
})

test('state errors retry without dispatching guessed completion', async () => {
  const send = mock(async () => true)
  let fail = true
  const repair = new CompletionRepair(() => end.thread, async () => {
    if (fail) throw new Error('unavailable')
    return 'error'
  }, send, immediate)
  repair.end({ ...end, outcome: 'error' })
  await repair.poll()
  expect(send).not.toHaveBeenCalled()
  fail = false
  await repair.poll()
  expect(send.mock.calls[0][1]).toBe('error')
})

test('attempts follow the backoff schedule and stop after the last delay', async () => {
  // An unbounded loop re-sends the hook every 5 s per stuck tab. Wrong impls: retry on every poll; never give up; give up
  // before the schedule is spent; keep a dead completion pending.
  let clock = 1_000_000
  const send = mock(async () => false)
  const abandoned = mock((_c: unknown, _n: number) => {})
  const repair = new CompletionRepair(() => end.thread, async () => 'idle', send,
    { delaysMs: [30, 60, 120], now: () => clock, onAbandon: abandoned })
  repair.end(end)
  await repair.poll()
  expect(send).not.toHaveBeenCalled() // the managed plugin's own write gets the first wait
  clock += 29; await repair.poll()
  expect(send).not.toHaveBeenCalled()
  clock += 1; await repair.poll()
  expect(send).toHaveBeenCalledTimes(1)
  clock += 59; await repair.poll(); await repair.poll()
  expect(send).toHaveBeenCalledTimes(1)
  clock += 1; await repair.poll()
  expect(send).toHaveBeenCalledTimes(2)
  clock += 120; await repair.poll()
  expect(send).toHaveBeenCalledTimes(3)
  expect(abandoned).toHaveBeenCalledWith(end, 3)
  clock += 10_000; await repair.poll()
  expect(send).toHaveBeenCalledTimes(3) // nothing pending any more
})

test('a failing sender counts as an attempt instead of looping on the next poll', async () => {
  let clock = 0
  const send = mock(async () => { throw new Error('socket refused') })
  const repair = new CompletionRepair(() => end.thread, async () => 'idle', send, { delaysMs: [0, 50, 50], now: () => clock })
  repair.end(end)
  await repair.poll()
  await repair.poll()
  expect(send).toHaveBeenCalledTimes(1)
  clock = 50
  await repair.poll()
  expect(send).toHaveBeenCalledTimes(2)
})

test('a new turn end restarts the schedule', async () => {
  let clock = 0
  const send = mock(async () => false)
  const repair = new CompletionRepair(() => end.thread, async () => 'idle', send, { delaysMs: [10, 10], now: () => clock })
  repair.end(end)
  clock = 10; await repair.poll()
  expect(send.mock.calls[0][3]).toBe(0)
  repair.start()
  repair.end({ ...end, turn: 'turn-2' })
  await repair.poll()
  expect(send).toHaveBeenCalledTimes(1) // the new completion waits its first delay again
  clock = 20; await repair.poll()
  expect(send.mock.calls[1][0].turn).toBe('turn-2')
  expect(send.mock.calls[1][3]).toBe(0)
})

test('default schedule is minutes, not seconds, and the badge strings match the managed plugin', () => {
  expect(RETRY_DELAYS_MS[0]).toBeGreaterThanOrEqual(20_000)
  expect(RETRY_DELAYS_MS.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(30 * 60_000)
  expect(BADGE.idle).toEqual({ label: '__cmux_amp_status_idle', icon: 'circle', color: '#adb5bd' })
})

test('cmux read-back requires the exact workspace, surface, thread, and one Amp binding', () => {
  const panel = { id: 'surface-uuid', terminal: { agent: { kind: 'amp', sessionId: end.thread } } }
  const ws = { workspaceId: 'ws-uuid', panels: [panel], statusEntries: [{ key: 'amp', value: 'Running' }] }
  const data = { windows: [{ tabManager: { workspaces: [ws] } }] }
  expect(storedState(data, 'ws-uuid', 'surface-uuid', end.thread)).toBe('running')
  expect(storedState(data, 'reused', 'surface-uuid', end.thread)).toBeNull()
  expect(storedState(data, 'ws-uuid', 'reused', end.thread)).toBeNull()
  expect(storedState(data, 'ws-uuid', 'surface-uuid', 'T-other')).toBeNull()
  ws.statusEntries[0].value = 'Idle'
  expect(storedState(data, 'ws-uuid', 'surface-uuid', end.thread)).toBe('idle')
  ws.panels.push({ ...panel, id: 'second' })
  expect(storedState(data, 'ws-uuid', 'surface-uuid', end.thread)).toBeNull()
})
