import { describe, expect, test } from 'bun:test'
import { SWITCHBOARD_REPO, detachedSpawnPlan } from './lib'

// A detached Node or Bun spawn on Windows is DETACHED_PROCESS: no console, so every git or
// python call under it opens a terminal window. Windows goes through no_window.py.
describe('detachedSpawnPlan', () => {
  test('Windows starts the command through no_window.py spawn', () => {
    const plan = detachedSpawnPlan('python3', ['script.py', '--force'], 'win32')
    expect(plan.command).toBe('python3')
    expect(plan.args).toEqual([`${SWITCHBOARD_REPO}/system/scripts/no_window.py`, 'spawn', '--', 'python3', 'script.py', '--force'])
  })

  test('macOS and Linux start the command as given', () => {
    expect(detachedSpawnPlan('bash', ['start-day.sh'], 'darwin')).toEqual({ command: 'bash', args: ['start-day.sh'] })
    expect(detachedSpawnPlan('bash', ['start-day.sh'], 'linux')).toEqual({ command: 'bash', args: ['start-day.sh'] })
  })
})
