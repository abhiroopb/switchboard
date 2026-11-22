import { afterAll, expect, mock, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Separate Bun process: helper mocks must not escape into other plugin suites.
test('foreground proof follows activeThread, never native child event context', async () => {
  const savedEnv = { ...process.env }
  const savedInterval = globalThis.setInterval
  // The ps proof path under test is POSIX; win32 skips it on purpose, so pin the platform.
  const savedPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!
  Object.defineProperty(process, 'platform', { value: 'linux' })
  const writes: any[] = []
  let tick!: () => void
  let changed!: () => void
  let pending!: () => void
  let delay = false
  let fail = false
  const current: { current: { id: string } | null; subscribe: (fn: () => void) => void } = {
    current: { id: 'T-parent' }, subscribe: fn => { changed = fn },
  }
  mock.module('./lib', () => ({
    PM_OS_REPO: '/fixture', isPluginEnabled: () => true, runDetached: async () => true, logEvent: async () => {},
    run: async () => {
      if (delay) await new Promise<void>(resolve => { pending = resolve })
      return { code: fail ? 1 : 0, stdout: `${process.ppid} stable-start` }
    },
    writeState: async (name, key, value) => {
      expect(name).toBe('active-surfaces')
      expect(key).toBe(String(process.ppid))
      writes.push(value)
    },
  }))
  try {
    Object.assign(process.env, { CMUX_WORKSPACE_ID: 'worker-workspace', CMUX_SURFACE_ID: 'worker-surface' })
    globalThis.setInterval = ((fn, delay) => {
      expect(delay).toBe(5000)
      tick = fn
      return { unref() {} }
    }) as any
    const { watchActiveSurface } = await import('../task-liveness')
    watchActiveSurface({ activeThread: current } as any)
    await Bun.sleep(0)
    expect(writes[0]).toMatchObject({ thread_id: 'T-parent', workspace_id: 'worker-workspace',
      surface_id: 'worker-surface', amp_pid: process.ppid, publisher_pid: process.pid, publisher_start: 'stable-start' })
    expect(writes[0].runtime_revision).toBe(createHash('sha256')
      .update(readFileSync(new URL('../task-liveness.ts', import.meta.url))).digest('hex'))
    // Native execution does not change the UI observable. A periodic refresh
    // must keep the parent rather than adopting the child event's thread.
    tick()
    await Bun.sleep(0)
    expect(writes.at(-1).thread_id).toBe('T-parent')
    delay = true
    tick()
    current.current = { id: 'T-selected' }
    changed()
    pending()
    await Bun.sleep(0)
    expect(writes.at(-1).thread_id).toBe('T-selected')
    delay = false
    current.current = null
    changed()
    await Bun.sleep(0)
    expect(writes.at(-1).thread_id).toBeNull()
    const count = writes.length
    fail = true
    tick()
    await Bun.sleep(0)
    expect(writes).toHaveLength(count)
  } finally {
    globalThis.setInterval = savedInterval
    Object.defineProperty(process, 'platform', savedPlatform)
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
    mock.restore()
  }
})
