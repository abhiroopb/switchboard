import { afterAll, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Run in its own Bun process: these mocks replace subprocess and timer adapters.
test('close adapter waits for idle and cancels on new turns or plugin disposal', async () => {
  const savedEnv = { ...process.env }
  const savedTimeout = globalThis.setTimeout
  const savedClear = globalThis.clearTimeout
  let tick!: () => Promise<void>
  let dispose!: () => void
  let state = 'idle'
  const handlers: Record<string, Function> = {}
  const launch = mock(async () => true)
  mock.module('node:fs/promises', () => ({ readFile: async () => '{"tasks":[]}' }))
  mock.module('./lib', () => ({ SWITCHBOARD_REPO: '/fixture', isPluginEnabled: () => true, runDetached: launch,
    run: async () => ({ code: 0, stdout: '{"caller":{"workspace_ref":"workspace:9"}}' }) }))
  try {
    Object.assign(process.env, { CMUX_WORKSPACE_ID: 'uuid-9', CMUX_SURFACE_ID: 'surface-9', SWITCHBOARD_WORKER_ID: 'worker-9' })
    globalThis.setTimeout = ((callback, delay) => { expect(delay).toBe(10000); tick = callback; return { unref() {} } }) as any
    globalThis.clearTimeout = (() => {}) as any
    const { default: plugin } = await import('./ephemeral-close')
    plugin({ system: { executor: { kind: 'local' } }, activeThread: { current: { id: 'T-self' } },
      configuration: { subscribe: callback => callback({}) }, onDispose: callback => { dispose = callback },
      threads: { get: () => ({ state: { get: async () => state } }) },
      logger: { log: (message: string) => { throw new Error(message) } },
      on: (event, handler) => { handlers[event] = handler },
    } as any)
    const finish = () => handlers['agent.end']({ status: 'done', id: 'turn-2', thread: { id: 'T-self' } })
    finish()
    state = 'awaiting-approval'
    await tick()
    expect(launch).not.toHaveBeenCalled()
    state = 'idle'
    finish()
    handlers['agent.start']()
    await tick()
    expect(launch).not.toHaveBeenCalled()
    finish()
    await tick()
    expect(launch).toHaveBeenCalledTimes(1)
    expect(launch.mock.calls[0][1]).toContain('uuid-9')
    expect(launch.mock.calls[0][1]).toContain('turn-2')
    finish()
    dispose()
    await tick()
    expect(launch).toHaveBeenCalledTimes(1)
  } finally {
    globalThis.setTimeout = savedTimeout
    globalThis.clearTimeout = savedClear
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
    mock.restore()
  }
})
