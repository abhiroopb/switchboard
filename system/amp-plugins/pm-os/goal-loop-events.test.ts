import { afterAll, beforeEach, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

const states = new Map<string, any>()
let onLog: (event: any) => Promise<void> = async () => {}
mock.module('./lib', () => ({
  isHeadless: () => true,
  isPluginEnabled: () => true,
  logEvent: async (_plugin: string, event: any) => onLog(event),
  readState: async (_plugin: string, key: string) => structuredClone(states.get(key) ?? null),
  writeState: async (_plugin: string, key: string, state: any) => { states.set(key, structuredClone(state)) },
}))
const { default: plugin } = await import('../goal-loop')

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fixture() {
  const tools: Record<string, any> = {}
  let end: any
  plugin({
    configuration: { subscribe: (cb: any) => cb({}) },
    registerTool: (tool: any) => { tools[tool.name] = tool.execute },
    on: (_name: string, callback: any) => { end = callback },
  } as any)
  const started = deferred<void>()
  const ai = deferred<any>()
  const context = { thread: { id: 'fixture' }, ai: { ask: () => { started.resolve(); return ai.promise } } }
  const event = { thread: context.thread, status: 'done', messages: [] }
  return { tools, context, ai, started, end: () => end(event, context), state: () => states.get('thread-fixture') }
}

beforeEach(() => { states.clear(); onLog = async () => {} })

test('real end hook does not continue after clear during AI evaluation', async () => {
  const f = fixture()
  await f.tools.goal_set({ goal: 'old' }, f.context)
  const result = f.end()
  await f.started.promise
  await f.tools.goal_clear({}, f.context)
  f.ai.resolve({ result: 'no', probability: 1, reason: 'not done' })
  expect(await result).toBeUndefined()
  expect(f.state().stoppedReason).toBe('cleared')
})

test('real catch path preserves a replacement goal', async () => {
  const f = fixture()
  await f.tools.goal_set({ goal: 'old' }, f.context)
  const result = f.end()
  await f.started.promise
  await f.tools.goal_set({ goal: 'new' }, f.context)
  f.ai.reject(new Error('fixture evaluator failure'))
  expect(await result).toBeUndefined()
  expect(f.state().goal).toBe('new')
  expect(f.state().active).toBe(true)
})

test('clear during continuation logging suppresses the returned continuation', async () => {
  const f = fixture()
  await f.tools.goal_set({ goal: 'old' }, f.context)
  onLog = async event => { if (event.action === 'continue') await f.tools.goal_clear({}, f.context) }
  const result = f.end()
  await f.started.promise
  f.ai.resolve({ result: 'no', probability: 1, reason: 'not done' })
  expect(await result).toBeUndefined()
  expect(f.state().active).toBe(false)
})

test('overlapping end hooks return at most one continuation', async () => {
  const f = fixture()
  await f.tools.goal_set({ goal: 'old' }, f.context)
  const first = f.end()
  const second = f.end()
  await f.started.promise
  // Both hook snapshots queue before either AI result can commit.
  f.ai.resolve({ result: 'no', probability: 1, reason: 'not done' })
  const results = await Promise.all([first, second])
  expect(results.filter(result => result?.action === 'continue')).toHaveLength(1)
  expect(f.state().turnsUsed).toBe(1)
})
