import { expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { spawnSync } from 'node:child_process'
import names from '../pm-os-bundle.json'

const root = resolve(import.meta.dir, '../../..')
const source = join(root, 'system/amp-plugins')
function install(target: string, args: string[] = [], env: Record<string, string> = {}) {
  return spawnSync('bash', [join(root, 'system/scripts/install-amp-plugins.sh'), ...args], {
    env: { ...process.env, PM_OS_PLUGIN_TARGET_DIR: target, PM_OS_PLUGIN_SOURCE_DIR: source, ...env }, encoding: 'utf8',
  })
}

test('migration, idempotence, rollback, and foreign plugins', () => {
  const dir = mkdtempSync(join(tmpdir(), 'amp-bundle-install-'))
  try {
    writeFileSync(join(dir, 'cmux-session.ts'), 'managed untouched')
    writeFileSync(join(dir, 'foreign.ts'), 'foreign untouched')
    expect(install(dir, ['--unbundle']).status).toBe(0)
    expect(install(dir).status).toBe(0)
    for (const name of names) expect(readdirSync(dir)).not.toContain(`${name}.ts`)
    expect(readdirSync(dir)).toContain('pm-os-runtime.ts')
    expect(install(dir).stdout).toBe('')
    expect(readFileSync(join(dir, 'cmux-session.ts'), 'utf8')).toBe('managed untouched')
    expect(readFileSync(join(dir, 'foreign.ts'), 'utf8')).toBe('foreign untouched')
    expect(install(dir, ['--unbundle']).status).toBe(0)
    for (const name of names) expect(readdirSync(dir)).toContain(`${name}.ts`)
    expect(readdirSync(dir)).not.toContain('pm-os-runtime.ts')
  } finally { rmSync(dir, { recursive: true, force: true }) }
}, 60_000)

test('noncanonical retired module blocks before installing bundle', () => {
  const dir = mkdtempSync(join(tmpdir(), 'amp-bundle-install-'))
  try {
    writeFileSync(join(dir, `${names[0]}.ts`), 'export default () => {} // foreign copy')
    const before = readdirSync(dir)
    expect(install(dir).status).toBe(1)
    expect(readdirSync(dir)).toEqual(before)
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('shim mode loads the tracked source and replaces a stale copy only with --force', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'amp-bundle-install-'))
  const shim = { PM_OS_PLUGIN_LINK_MODE: 'shim' }
  try {
    writeFileSync(join(dir, 'goal-loop.ts'), '// stale plain copy')
    writeFileSync(join(dir, `${names[0]}.ts`), '// stale plain copy of a bundled module')
    const refused = install(dir, [], shim)
    expect(refused.status).toBe(1)
    expect(readFileSync(join(dir, 'goal-loop.ts'), 'utf8')).toBe('// stale plain copy')
    expect(install(dir, ['--force'], shim).status).toBe(0)
    const backups = readdirSync(join(dir, '_pre-symlink-backup'))
    expect(backups.some(f => f.startsWith('goal-loop.ts.'))).toBe(true)
    expect(backups.some(f => f.startsWith(`${names[0]}.ts.`))).toBe(true)
    const text = readFileSync(join(dir, 'pm-os-guardrails.ts'), 'utf8')
    expect(text).toContain("export { default } from '")
    expect(text).toContain('system/amp-plugins/pm-os-guardrails.ts')
    // Directories and data files stay next to the source; only .ts entries get shims.
    expect(existsSync(join(dir, 'pm-os'))).toBe(false)
    expect(existsSync(join(dir, 'README.md'))).toBe(false)
    for (const name of names) expect(readdirSync(dir)).not.toContain(`${name}.ts`)
    expect(install(dir, [], shim).stdout).toBe('')
    const runtime = await import(join(dir, 'pm-os-runtime.ts'))
    expect(typeof runtime.default).toBe('function')
    expect(typeof runtime.description).toBe('string')
  } finally { rmSync(dir, { recursive: true, force: true }) }
}, 60_000)
