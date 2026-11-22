import { afterAll, expect, mock, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

test('local execution hooks publish child identity without a borrowed terminal and revoke on end', async () => {
  const savedInterval = globalThis.setInterval
  // The ps proof path under test is POSIX; win32 skips it on purpose, so pin the platform.
  const savedPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!
  Object.defineProperty(process, 'platform', { value: 'linux' })
  const handlers: Record<string, Function> = {}
  const proofs = new Map<string, any>()
  let tick!: () => void
  mock.module('./lib', () => ({
    PM_OS_REPO: '/fixture', isPluginEnabled: () => true, runDetached: async () => true, logEvent: async () => {},
    run: async () => ({ code: 0, stdout: `${process.ppid} start-stamp` }),
    writeState: async (name: string, key: string, value: unknown) => {
      expect(name).toBe('local-executors')
      proofs.set(key, value)
    },
  }))
  try {
    globalThis.setInterval = ((fn: () => void) => { tick = fn; return { unref() {} } }) as any
    const { watchLocalExecutors } = await import('../task-liveness')
    watchLocalExecutors({ on: (name: string, fn: Function) => { handlers[name] = fn },
      logger: { log() {} }, activeThread: { current: { id: 'T-parent' } } } as any)
    const id = 'T-00000000-0000-4000-8000-0000000000f3'
    const event = { thread: { id } }
    await handlers['agent.start'](event)
    const key = `${id}-${process.pid}`
    expect(proofs.get(key)).toMatchObject({ thread_id: id, repo_root: '/fixture',
      amp_pid: process.ppid, publisher_pid: process.pid, publisher_start: 'start-stamp' })
    expect(proofs.get(key)).not.toHaveProperty('workspace_ref')
    expect(proofs.get(key)).not.toHaveProperty('workspace_id')
    expect(proofs.get(key).runtime_revision).toBe(createHash('sha256')
      .update(readFileSync(new URL('../task-liveness.ts', import.meta.url))).digest('hex'))
    tick()
    await handlers['agent.end'](event)
    tick()
    await Bun.sleep(0)
    expect(proofs.get(key)).toEqual({})
    await handlers['tool.call'](event) // reload during a running turn
    expect(proofs.get(key).thread_id).toBe(id)
  } finally {
    globalThis.setInterval = savedInterval
    Object.defineProperty(process, 'platform', savedPlatform)
    mock.restore()
  }
})
