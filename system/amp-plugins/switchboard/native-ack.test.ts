import { afterAll, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Separate process: tests the child's hooks, with no parent create_thread event.
test('a background seed publishes proof before native registration and never borrows the host', async () => {
  const savedInterval = globalThis.setInterval
  // The ps proof path under test is POSIX; win32 skips it on purpose, so pin the platform.
  const savedPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!
  Object.defineProperty(process, 'platform', { value: 'linux' })
  // The seed below names an arbitrary task; a switchboard worker running this suite
  // inherits SWITCHBOARD_WORKER_TASK_ID from its launch command and would ignore it as foreign.
  const savedTask = process.env.SWITCHBOARD_WORKER_TASK_ID
  delete process.env.SWITCHBOARD_WORKER_TASK_ID
  const handlers = new Map<string, Function[]>()
  const calls: string[][] = []
  const id = 'T-00000000-0000-4000-8000-0000000000f2'
  const attempt = 'rt-00000000000e.a1.51e243'
  let published = false
  mock.module('./lib', () => ({
    SWITCHBOARD_REPO: '/fixture', isPluginEnabled: () => true, runDetached: async () => true, logEvent: async () => {},
    run: async (bin: string, args: string[]) => {
      if (bin === 'ps') return { code: 0, stdout: `${process.ppid} start-stamp` }
      if (args.includes('--thread')) return { code: 0, stdout: '{}' }
      calls.push(args)
      if (args[1] === 'register-native') expect(published).toBe(true)
      return { code: 0, stdout: JSON.stringify({ attempt_id: attempt, phase: 'running' }) }
    },
    writeState: async (name: string, key: string, proof: any) => {
      if (name === 'local-executors' && proof.thread_id === id) published = true
    },
  }))
  try {
    globalThis.setInterval = (() => ({ unref() {} })) as any
    const { default: initialize } = await import('../task-liveness')
    const logger = { log() {} }
    initialize({
      activeThread: { current: { id: 'T-host' }, subscribe() {} },
      configuration: { subscribe(fn: Function) { fn({}) } }, logger, registerCommand() {},
      on(name: string, fn: Function) { handlers.set(name, [...(handlers.get(name) ?? []), fn]) },
    } as any)
    const event = { id: 'seed', thread: { id },
      message: `SWITCHBOARD_WORKER_TASK_ID=rt-00000000000e SWITCHBOARD_WORKER_ATTEMPT_ID=${attempt}` }
    await Promise.all(handlers.get('agent.start')!.map(fn => fn(event, { thread: { id }, logger })))
    expect(calls.map(args => args[1])).toEqual(['register-native', 'ack'])
    expect(calls[0]).toContain('--local-claim')
    expect(calls[0]).toContain(id)
    expect(calls[1]).toContain(attempt)
    expect(calls.flat()).not.toContain('--workspace-ref')
  } finally {
    globalThis.setInterval = savedInterval
    Object.defineProperty(process, 'platform', savedPlatform)
    if (savedTask !== undefined) process.env.SWITCHBOARD_WORKER_TASK_ID = savedTask
    mock.restore()
  }
})
