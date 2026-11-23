import { afterAll, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Run separately from other plugin tests: these mocks replace process adapters.
test('plugin re-stamps the badge on a bounded schedule until exact cmux read-back, without touching running turns', async () => {
  const savedEnv = { ...process.env }
  const savedInterval = globalThis.setInterval
  const savedNow = Date.now
  let clock = 1_800_000_000_000
  let tick!: () => void
  let ampState = 'running'
  let cmuxState = 'Running'
  let fail = true
  const handlers: Record<string, Function> = {}
  const payloads: any[] = []
  const argvs: string[][] = []
  const snapshot = () => JSON.stringify({ windows: [{ tabManager: { workspaces: [{
    workspaceId: 'ws',
    panels: [{ id: 'surface', terminal: { agent: { kind: 'amp', sessionId: 'T-test' } } }],
    statusEntries: [{ key: 'amp', value: cmuxState }],
  }] } }] })
  const read = mock(async () => snapshot())
  const execute = mock((bin, args, options, callback) => {
    expect(bin).toBe('/Applications/cmux.app/Contents/Resources/bin/cmux')
    expect(options.timeout).toBe(5000)
    expect(options.env.AMP_API_KEY).toBeUndefined()
    expect(options.env.CMUX_AMP_PID).toBe('fixture-pid')
    argvs.push(args)
    return { stdin: {
      on() {},
      end(body?: string) {
        if (body !== undefined) payloads.push(JSON.parse(body))
        callback(fail ? new Error('RPC timeout') : null, '', '')
      },
    } }
  })
  mock.module('node:fs/promises', () => ({ readFile: read }))
  mock.module('node:child_process', () => ({ execFile: execute }))
  mock.module('./lib', () => ({ HOME: '/fixture', isPluginEnabled: () => true, logEvent: async () => {} }))
  try {
    // A run inside a cmux or wmux tab inherits these and would point the plugin at the host's binary and store.
    for (const key of ['CMUX_BIN', 'CMUX_AMP_CMUX_BIN', 'SWITCHBOARD_CMUX_SESSION_JSON', 'CC_CMUX_SESSION_JSON']) delete process.env[key]
    Object.assign(process.env, { CMUX_WORKSPACE_ID: 'ws', CMUX_SURFACE_ID: 'surface',
      CMUX_AMP_PID: 'fixture-pid', AMP_API_KEY: 'fixture-secret', CMUX_AMP_HOOKS_DISABLED: '0' })
    Date.now = () => clock
    globalThis.setInterval = ((callback, delay) => {
      expect(delay).toBe(5000)
      tick = callback
      return { unref() {} }
    }) as any
    const { default: plugin, RETRY_DELAYS_MS } = await import('../cmux-lifecycle-reconcile')
    plugin({
      activeThread: { current: { id: 'T-test' } },
      configuration: { subscribe: callback => callback({}) },
      threads: { get: () => ({ state: { get: async () => ampState } }) },
      on: (name, callback) => { handlers[name] = callback },
    } as any)
    const poll = async (advanceMs = 0) => { clock += advanceMs; tick(); await Bun.sleep(0); await Bun.sleep(0) }
    const setStatusCalls = () => argvs.filter(a => a[0] === 'set-status')
    const hookCalls = () => argvs.filter(a => a[0] === 'hooks')

    handlers['agent.end']({ thread: { id: 'T-test' }, id: 'turn-1', status: 'done' })
    ampState = 'idle'
    await poll()
    expect(execute).not.toHaveBeenCalled() // the managed plugin's own write gets the first wait
    await poll(RETRY_DELAYS_MS[0]) // attempt 0: hook once + badge; both fail at the transport
    expect(hookCalls()).toHaveLength(1)
    expect(setStatusCalls()).toHaveLength(1)
    expect(payloads[0]).toMatchObject({ session_id: 'T-test', hook_event_name: 'Lifecycle',
      event: 'Lifecycle', agent_state: 'idle', turn_id: 'turn-1', turn_outcome: 'done' })
    expect(setStatusCalls()[0]).toEqual(['set-status', 'amp', '__cmux_amp_status_idle', '--icon', 'circle', '--color', '#adb5bd', '--workspace', 'ws'])
    await poll() // 5 s later: nothing, the schedule gates the retry
    expect(execute).toHaveBeenCalledTimes(2)
    fail = false
    await poll(RETRY_DELAYS_MS[1]) // attempt 1: badge only; exit success alone does not retire it
    expect(hookCalls()).toHaveLength(1)
    expect(setStatusCalls()).toHaveLength(2)
    cmuxState = 'Idle'
    await poll(RETRY_DELAYS_MS[2]) // read-back: retired, no write
    expect(execute).toHaveBeenCalledTimes(3)
    cmuxState = 'Running'
    await poll(RETRY_DELAYS_MS[3])
    expect(execute).toHaveBeenCalledTimes(3)
    // A new turn fences the old completion; a stuck one is abandoned after the schedule.
    handlers['agent.end']({ thread: { id: 'T-test' }, id: 'turn-2', status: 'done' })
    handlers['agent.start']()
    await poll(RETRY_DELAYS_MS[0])
    expect(execute).toHaveBeenCalledTimes(3)
    handlers['agent.end']({ thread: { id: 'T-test' }, id: 'turn-3', status: 'done' })
    for (const delay of RETRY_DELAYS_MS) await poll(delay)
    expect(setStatusCalls()).toHaveLength(2 + RETRY_DELAYS_MS.length)
    await poll(RETRY_DELAYS_MS[RETRY_DELAYS_MS.length - 1] * 4)
    expect(setStatusCalls()).toHaveLength(2 + RETRY_DELAYS_MS.length) // abandoned: no more writes
  } finally {
    Date.now = savedNow
    globalThis.setInterval = savedInterval
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
    mock.restore()
  }
})
