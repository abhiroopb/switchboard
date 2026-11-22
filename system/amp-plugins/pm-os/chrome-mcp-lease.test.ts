import { afterAll, expect, mock, test } from 'bun:test'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

async function load(repo: string, run: unknown) {
  mock.module('./lib', () => ({ PM_OS_REPO: repo, run }))
  const handlers: Record<string, Function> = {}
  const { default: plugin } = await import('../chrome-mcp-lease')
  plugin({ on: (name: string, fn: Function) => { handlers[name] = fn } } as any)
  return handlers
}

function fixture() {
  const repo = mkdtempSync(join(tmpdir(), 'lease-'))
  mkdirSync(join(repo, 'system/scripts'), { recursive: true })
  mkdirSync(join(repo, '.state'))
  return repo
}

test('turn completion requests only a local-owner release; helper errors are reported', async () => {
  const repo = fixture()
  writeFileSync(join(repo, 'system/scripts/chrome-mcp-session.py'), '')
  let code = 0
  const run = mock(async () => ({ code, stdout: '', stderr: code ? 'Chrome MCP release not confirmed: supervisor 57459 still holds pending call(s): tools/call id=9 91.0s\n' : '' }))
  try {
    const handlers = await load(repo, run)
    const log = mock(() => {})
    expect(Object.keys(handlers)).toEqual(['agent.end'])
    await handlers['agent.end']({ status: 'done' }, { logger: { log } })
    expect(run).toHaveBeenCalledWith('python3', [
      `${repo}/system/scripts/chrome-mcp-session.py`, '--release', '--wait', '12',
    ], { timeoutMs: 15000 })
    expect(log).not.toHaveBeenCalled()
    code = 1
    await handlers['agent.end']({ status: 'error' }, { logger: { log } })
    expect(log).toHaveBeenCalledTimes(1)
    // The holder's reason reaches the log so a blocked worker can be told who holds the profile.
    expect((log.mock.calls[0] as unknown as string[])[0]).toContain('supervisor 57459 still holds pending call(s)')
  } finally {
    mock.restore()
    rmSync(repo, { recursive: true, force: true })
  }
})

test('a machine with no lease and no supervisor spawns nothing at turn end', async () => {
  const repo = fixture()
  const run = mock(async () => ({ code: 0, stdout: '', stderr: '' }))
  try {
    const handlers = await load(repo, run)
    await handlers['agent.end']({ thread: { id: 'invented-session' } }, { logger: { log() {} } })
    expect(run).not.toHaveBeenCalled()
  } finally {
    mock.restore()
    rmSync(repo, { recursive: true, force: true })
  }
})

test('an owner browser lease is released for this session at turn end', async () => {
  const repo = fixture()
  const saved = process.env.PM_OS_MACHINE
  process.env.PM_OS_MACHINE = 'INVENTED.local'
  writeFileSync(join(repo, '.state/owner-browser-lease-INVENTED.json'), '{}')
  const run = mock(async () => ({ code: 0, stdout: '', stderr: '' }))
  try {
    const handlers = await load(repo, run)
    await handlers['agent.end']({ thread: { id: 'invented-session' } }, { logger: { log() {} } })
    expect(run).toHaveBeenCalledWith('python3', [
      `${repo}/system/scripts/owner_browser.py`, 'release', '--session', 'invented-session',
    ], { timeoutMs: 10000 })
  } finally {
    if (saved === undefined) delete process.env.PM_OS_MACHINE
    else process.env.PM_OS_MACHINE = saved
    mock.restore()
    rmSync(repo, { recursive: true, force: true })
  }
})
