import { expect, test } from 'bun:test'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { spawnSync } from 'node:child_process'

test('router settings preserve safety configuration and source, block command and URL MCPs', () => {
  const root = resolve(import.meta.dir, '../../..')
  const home = mkdtempSync(join(tmpdir(), 'router-settings-'))
  const settings = join(home, '.config/amp/settings.json')
  mkdirSync(join(home, '.config/amp'), { recursive: true })
  const original = JSON.stringify({ 'amp.mcpServers': { fixture: { command: 'do-not-run' } },
    'amp.permissions': [{ tool: 'shell_command', action: 'ask' }],
    'amp.mcpPermissions': [{ matches: { command: '*' }, action: 'allow' }], 'pm-os-guardrails.disabled': false })
  writeFileSync(settings, original)
  try {
    const run = () => spawnSync('bash', ['-c', 'source "$REPO_ROOT/system/cmux-helpers.sh"; amp_router_settings_file'],
      { env: { ...process.env, HOME: home, REPO_ROOT: root, AMP_SETTINGS_FILE: settings }, encoding: 'utf8' })
    const result = run()
    expect(result.status).toBe(0)
    const path = result.stdout.trim()
    const profile = JSON.parse(readFileSync(path, 'utf8'))
    expect(profile['amp.permissions']).toEqual(JSON.parse(original)['amp.permissions'])
    expect(profile['pm-os-guardrails.disabled']).toBe(false)
    expect(profile['amp.mcpServers']).toEqual({ fixture: { command: 'do-not-run' } })
    expect(profile['amp.mcpPermissions'].slice(0, 2)).toEqual([
      { matches: { command: '*' }, action: 'reject' }, { matches: { url: '*' }, action: 'reject' },
    ])
    // Windows has no POSIX mode bits (stat reports 0o666 for any writable file); the profile's
    // privacy there comes from the per-user ACL on HOME, which this test cannot read through stat.
    if (process.platform !== 'win32') expect(statSync(path).mode & 0o777).toBe(0o600)
    expect(readFileSync(settings, 'utf8')).toBe(original)
    expect(run().status).toBe(0)
    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual(profile)
  } finally { rmSync(home, { recursive: true, force: true }) }
})
