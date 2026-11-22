import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

let headless = false
let workspace = 'Chief of Staff'
let slug: string | null = 'fixture-workstream'
const queued: any[][] = []
mock.module('./lib', () => ({
  PM_OS_REPO: '/synthetic/pm-os',
  isHeadless: () => headless,
  isPluginEnabled: (config: any, key: string) => !config[`${key}.disabled`],
  activeWorkspaceName: async () => workspace,
  workstreamSlugFromContext: () => slug,
  logEvent: async () => {},
  runDebouncedDetached: async (...args: any[]) => { queued.push(args); return true },
}))

const definitions = [
  ['cos-finished', 'agent.end', 5400, (await import('../cos-finished')).default],
  ['daily-rollup-trigger', 'tool.result', 300, (await import('../daily-rollup-trigger')).default],
  ['watched-docs-refresh', 'session.start', 900, (await import('../watched-docs-refresh')).default],
  ['watched-slack-refresh', 'session.start', 900, (await import('../watched-slack-refresh')).default],
  ['workstream-signals', 'session.start', 900, (await import('../workstream-signals')).default],
] as const

const originalWorker = process.env.PM_OS_WORKER_ID
beforeEach(() => { headless = false; workspace = 'Chief of Staff'; slug = 'fixture-workstream'; queued.length = 0; delete process.env.PM_OS_WORKER_ID })
afterEach(() => {
  if (originalWorker === undefined) delete process.env.PM_OS_WORKER_ID
  else process.env.PM_OS_WORKER_ID = originalWorker
})

function fixture(plugin: any, config = {}) {
  const handlers: Record<string, any> = {}
  plugin({ configuration: { subscribe: (cb: any) => cb(config) },
    on: (name: string, callback: any) => { handlers[name] = callback } })
  return handlers
}

const eligible = { status: 'done', tool: 'shell_command', input: { command: 'pmos slack post --channel fixture' } }

for (const [name, event, cooldown, plugin] of definitions) {
  test(`${name} delegates exact key and cooldown without recording success`, async () => {
    await fixture(plugin)[event](eligible, {})
    expect(queued).toHaveLength(1)
    expect(queued[0].slice(0, 3)).toEqual([name, event === 'session.start' ? slug : 'global', cooldown])
    expect(queued[0][4][0]).toStartWith('/synthetic/pm-os/')
  })
  test(`${name} respects disabled and headless guards`, async () => {
    await fixture(plugin, { [`${name}.disabled`]: true })[event](eligible, {})
    headless = true
    await fixture(plugin)[event](eligible, {})
    expect(queued).toHaveLength(0)
  })
  if (event === 'session.start') {
    test(`${name} excludes supplemental workers and unknown workstreams`, async () => {
      process.env.PM_OS_WORKER_ID = 'synthetic-worker'
      expect(fixture(plugin)[event]).toBeUndefined()
      delete process.env.PM_OS_WORKER_ID
      slug = null
      await fixture(plugin)[event](eligible, {})
      expect(queued).toHaveLength(0)
    })
  }
}

test('CoS trigger excludes failed turns and other workspaces', async () => {
  const handlers = fixture(definitions[0][3])
  await handlers['agent.end']({ status: 'error' }, {})
  workspace = 'Fixture workstream'
  await handlers['agent.end'](eligible, {})
  expect(queued).toHaveLength(0)
})

test('rollup ignores read-only commands', async () => {
  await fixture(definitions[1][3])['tool.result']({ tool: 'shell_command', input: { command: 'pmos slack search query' } }, {})
  expect(queued).toHaveLength(0)
})
