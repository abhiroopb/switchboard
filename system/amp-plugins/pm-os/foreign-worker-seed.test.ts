import { afterAll, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// A worker process (PM_OS_WORKER_TASK_ID set by the launch command) that sees a
// worker-shaped seed for ANOTHER task must not ack it, for
// example when a compaction summary quotes another tab's launch command
// (`PM_OS_WORKER_TASK_ID=<task> ...`). Its own task still acks normally.
test('a worker ignores a worker seed for a task other than its own', async () => {
  const savedInterval = globalThis.setInterval
  const savedTask = process.env.PM_OS_WORKER_TASK_ID
  const handlers = new Map<string, Function[]>()
  const acked: string[] = []
  const events: string[] = []
  const id = 'T-00000000-0000-4000-8000-0000000000f1'
  mock.module('./lib', () => ({
    PM_OS_REPO: '/fixture', isPluginEnabled: () => true, runDetached: async () => true,
    logEvent: async (_plugin: string, payload: any) => { events.push(`${payload.event}:${payload.task_id ?? ''}`) },
    run: async (bin: string, args: string[]) => {
      if (bin === 'ps') return { code: 0, stdout: `${process.ppid} start-stamp` }
      if (args.includes('--thread')) return { code: 0, stdout: '{}' }
      if (args[1] === 'ack') acked.push(args[args.indexOf('--task-id') + 1] ?? args.join(' '))
      return { code: 0, stdout: JSON.stringify({ attempt_id: 'rt-00000000000a.a1.9928ca', phase: 'running' }) }
    },
    writeState: async () => {},
  }))
  try {
    process.env.PM_OS_WORKER_TASK_ID = 'rt-00000000000a'
    globalThis.setInterval = (() => ({ unref() {} })) as any
    const { default: initialize } = await import('../task-liveness')
    const logger = { log() {} }
    initialize({
      activeThread: { current: { id: 'T-host' }, subscribe() {} },
      configuration: { subscribe(fn: Function) { fn({}) } }, logger, registerCommand() {},
      on(name: string, fn: Function) { handlers.set(name, [...(handlers.get(name) ?? []), fn]) },
    } as any)
    const start = handlers.get('agent.start')!
    // Quoted launch command of a different task (what the compaction summary carried).
    const foreign = { id: 'm1', thread: { id },
      message: 'Earlier: `cat /x.md | PM_OS_WORKER_TASK_ID=rt-00000000000b PM_OS_WORKER_ATTEMPT_ID=rt-00000000000b.a1.671ef2 amp -m ultra` never showed the TUI.' }
    await Promise.all(start.map(fn => fn(foreign, { thread: { id }, logger })))
    expect(acked).toEqual([])
    expect(events).toContain('foreign-worker-seed-ignored:rt-00000000000b')
    expect(events.filter(e => e.startsWith('seed-detected'))).toEqual([])

    // Its own seed still binds.
    const own = { id: 'm2', thread: { id },
      message: 'PM_OS_WORKER_TASK_ID=rt-00000000000a PM_OS_WORKER_ATTEMPT_ID=rt-00000000000a.a1.9928ca' }
    await Promise.all(start.map(fn => fn(own, { thread: { id }, logger })))
    expect(events).toContain('seed-detected:rt-00000000000a')
    expect(acked.length).toBe(1)
    expect(acked[0]).toContain('rt-00000000000a')
  } finally {
    globalThis.setInterval = savedInterval
    if (savedTask === undefined) delete process.env.PM_OS_WORKER_TASK_ID
    else process.env.PM_OS_WORKER_TASK_ID = savedTask
    mock.restore()
  }
})
