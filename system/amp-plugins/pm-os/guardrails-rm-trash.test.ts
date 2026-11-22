import { afterAll, afterEach, expect, mock, test } from 'bun:test'
import { mkdtempSync, readFileSync, realpathSync, rmdirSync, symlinkSync, unlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import * as lib from './lib'
import { helperWord } from './rm-guard'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Rule 3: an outside `rm -rf` goes to the Trash with no
// prompt, and quoted delete text in a steer is data. Run:
// bun test system/amp-plugins/pm-os/guardrails-rm-trash.test.ts

const logs: Record<string, unknown>[] = []
mock.module('./lib', () => ({
  ...lib,
  logEvent: async (_plugin: string, event: Record<string, unknown>) => { logs.push(event) },
}))
const { default: plugin, gatedBashWriteTargets, realParentPath } = await import('../pm-os-guardrails')

const HELPER = resolve(import.meta.dir, '../../scripts/pm-os-trash.py')
// The shell word the rewrite uses: quoted when the install folder holds a space.
const HW = helperWord(HELPER)!
const saved = { helper: process.env.PM_OS_TRASH_HELPER, title: process.env.PM_OS_GUARDRAILS_WS_TITLE_OVERRIDE }
process.env.PM_OS_TRASH_HELPER = HELPER
delete process.env.PM_OS_GUARDRAILS_WS_TITLE_OVERRIDE
afterAll(() => {
  if (saved.helper === undefined) delete process.env.PM_OS_TRASH_HELPER
  else process.env.PM_OS_TRASH_HELPER = saved.helper
  if (saved.title !== undefined) process.env.PM_OS_GUARDRAILS_WS_TITLE_OVERRIDE = saved.title
})
afterEach(() => { logs.length = 0 })

const pending = () => new Promise<boolean>(() => {})
const bash = (cmd: string, workdir?: string) => ({ tool: 'Bash', input: workdir ? { cmd, workdir } : { cmd } })

function harness(config: Record<string, unknown> = {}) {
  let handler: (event: any, ctx: any) => Promise<any>
  const prompt = mock(pending)
  plugin({
    configuration: { subscribe: (cb: any) => cb({
      'pm-os-guardrails.cos-delivery-gate.assume-cos': true,
      'pm-os-guardrails.dispatch-guard.assume-origin': true,
      'pm-os-guardrails.confirm_timeout_ms': 20,
      'pm-os-guardrails.permission-relay.disabled': true,
      ...config,
    }) },
    on: (name: string, fn: any) => { if (name === 'tool.call') handler = fn },
    helpers: { shellCommandFromToolCall: (event: any) => event.tool === 'Bash' ? { command: event.input.cmd } : null },
  } as any)
  return { prompt, call: (event: any) => handler(event, { ui: { confirm: prompt } }) }
}

test('an outside delete is rewritten to the Trash helper with no prompt', async () => {
  const h = harness()
  expect(await h.call(bash('rm -rf /outside/example'))).toEqual({
    action: 'allow-with-input', input: { cmd: `${HW} -rf /outside/example` },
  })
  // A relative target resolves against the tool's workdir.
  const rel = await h.call(bash('rm -rf build-cache', '/Users/someone/proj'))
  expect(rel.action).toBe('allow-with-input')
  expect(rel.input).toEqual({ cmd: `${HW} -rf build-cache`, workdir: '/Users/someone/proj' })
  expect(h.prompt).not.toHaveBeenCalled()
  expect(logs).toContainEqual(expect.objectContaining({ rule: 'rm-rf', action: 'rewritten-to-trash', targets: ['/outside/example'] }))
})

test('temp and data-only deletes pass unchanged', async () => {
  const h = harness()
  for (const cmd of [
    'T=/tmp/acme-job; rm -rf $T; mkdir -p $T/sources',
    readFileSync(`${import.meta.dir}/test-fixtures/rm-heredoc-remote.sh`, 'utf8'),
  ]) expect(await h.call(bash(cmd))).toEqual({ action: 'allow' })
  expect(h.prompt).not.toHaveBeenCalled()
})

test('an in-repo delete goes to the helper, which keeps gitignored paths recoverable', async () => {
  // The helper deletes only what git tracks unchanged; local/ and scratch go to the Trash.
  const h = harness()
  for (const target of [`${lib.PM_OS_REPO}/.amp/in/rt-1`, `${lib.PM_OS_REPO}/local/skills-archive/not-in-use/old-skill`]) {
    expect(await h.call(bash(`rm -rf ${target}`))).toEqual({ action: 'allow-with-input', input: { cmd: `${HW} -rf ${target}` } })
  }
  // Mixed with an outside target, both invocations are rewritten.
  expect(await h.call(bash(`rm -rf /outside/x && rm -rf ${lib.PM_OS_REPO}/local/y`))).toEqual({
    action: 'allow-with-input', input: { cmd: `${HW} -rf /outside/x && ${HW} -rf ${lib.PM_OS_REPO}/local/y` },
  })
  // With the rewrite switched off, an in-repo delete still runs as written (pre-approved).
  expect(await harness({ 'pm-os-guardrails.rm-rf.trash.disabled': true }).call(bash(`rm -rf ${lib.PM_OS_REPO}/local/y`)))
    .toEqual({ action: 'allow' })
  expect(h.prompt).not.toHaveBeenCalled()
  expect(logs).toContainEqual(expect.objectContaining({ rule: 'rm-rf', action: 'repo-delete-as-written' }))
})

test('CoS steer 4: the Portable Kit worker command (cd into its worktree, relative delete) passes with no prompt', async () => {
  const h = harness({ 'pm-os-guardrails.cos-delivery-gate.assume-cos': false })
  const cmd = `cd ${lib.PM_OS_REPO}/.amp/worktrees/rt-00000000000c && rm -rf .amp/in/rt-00000000000c/kit-render`
  const helped = { action: 'allow-with-input', input: { cmd: cmd.replace('&& rm -rf', `&& ${HW} -rf`) } }
  expect(await h.call(bash(cmd))).toEqual(helped)
  expect(await h.call(bash(cmd, '/Users/someone/elsewhere'))).toEqual({ ...helped, input: { ...helped.input, workdir: '/Users/someone/elsewhere' } })
  expect(h.prompt).not.toHaveBeenCalled()
  expect(logs.filter(l => l.rule === 'rm-rf').map(l => l.action)).toEqual(['rewritten-repo-to-helper', 'rewritten-repo-to-helper'])
})

test('CoS steer 1: a heredoc of delete test strings, then bun run, never prompts', async () => {
  // A worker tab: the repo-relative write is its own worktree scratch.
  const h = harness({ 'pm-os-guardrails.cos-delivery-gate.assume-cos': false })
  const cmd = [
    "cat > .amp/in/rt-00000000000d/smoke.ts <<'EOF'",
    "console.log(['rm -rf /outside/example', 'T=/tmp/x; rm -rf $T', \"bash -c 'rm -rf ~/foo'\"])",
    'EOF',
    'bun run .amp/in/rt-00000000000d/smoke.ts',
  ].join('\n')
  expect(await h.call(bash(cmd))).toEqual({ action: 'allow' })
  expect(h.prompt).not.toHaveBeenCalled()
})

test('CoS steer 2: a router-dispatch steer that quotes a delete command never prompts', async () => {
  const h = harness()
  const prose = "the owner just hit a confirm prompt from your own smoke test: 'Allow destructive delete of /outside/example'. " +
    'Your Bash heredoc wrote test strings containing rm -rf, and the live guardrail is still the old one, so it fired.'
  for (const cmd of [
    `bash system/scripts/router-dispatch.sh --origin cos --steer rt-00000000000d "${prose}"`,
    `bash system/scripts/router-dispatch.sh --origin cos --steer rt-00000000000d '${prose.replace(/'/g, '')}'`,
    `git commit -m "Trash outside targets instead of rm -rf ~/scratch"`,
    `echo "rm -rf /outside/example and the live guardrail"`,
  ]) {
    const result = await h.call(bash(cmd))
    expect(result.action).toBe('allow')
  }
  expect(h.prompt).not.toHaveBeenCalled()
  expect(logs.filter(l => l.rule === 'rm-rf')).toEqual([])
})

test('code review: a temp path that is a symlink to an outside folder goes to the Trash', async () => {
  // Windows: a junction, the link type a user can make without admin rights.
  const slash = (p: string) => p.replace(/\\/g, '/')
  const root = slash(realpathSync(mkdtempSync(join(tmpdir(), 'rm-guard-link-'))))
  const outside = `${slash(lib.HOME)}/Documents`
  symlinkSync(outside, `${root}/link`, process.platform === 'win32' ? 'junction' : undefined)
  try {
    expect(realParentPath(`${root}/link/proj`, false)).toBe(`${slash(realpathSync(outside))}/proj`)
    expect(realParentPath(`${root}/link`, false)).toBe(`${root}/link`)
    const h = harness()
    const result = await h.call(bash(`rm -rf ${root}/link/proj`))
    expect(result).toEqual({ action: 'allow-with-input', input: { cmd: `${HW} -rf ${root}/link/proj` } })
    // Deleting the link itself stays a plain temp delete.
    expect(await h.call(bash(`rm -rf ${root}/link`))).toEqual({ action: 'allow' })
    if (process.platform === 'win32') {
      // Git Bash spells the temp folder /tmp; the junction under it still goes to the Bin.
      const viaTmp = `/tmp/${root.split('/').pop()}/link/proj`
      expect(await h.call(bash(`rm -rf ${viaTmp}`))).toEqual({ action: 'allow-with-input', input: { cmd: `${HW} -rf ${viaTmp}` } })
      expect(await h.call(bash(`rm -rf /tmp/${root.split('/').pop()}/link`))).toEqual({ action: 'allow' })
    }
  } finally {
    unlinkSync(`${root}/link`)
    rmdirSync(root)
  }
})

test('repo root, .git, and top-level home folders are hard blocks', async () => {
  const h = harness()
  // The repo root spelled with ~ and a trailing slash (~/Development/pm-os/ on macOS).
  const home = lib.HOME.replace(/\\/g, '/')
  const tildeRepo = lib.PM_OS_REPO.startsWith(`${home}/`) ? `~${lib.PM_OS_REPO.slice(home.length)}/` : `${lib.PM_OS_REPO}/`
  const windowsOnly = process.platform === 'win32' ? ['rm -rf ~/Dropbox', 'rm -rf ~/Dropbox/Documents/', 'rm -rf /c/Users', 'rm -rf D:/'] : []
  for (const cmd of [`rm -rf ${lib.PM_OS_REPO}`, `rm -rf ${tildeRepo}`, 'rm -rf ~/other-repo/.git', 'rm -rf ~/Documents', ...windowsOnly]) {
    const result = await h.call(bash(cmd))
    expect(result.action).toBe('reject-and-continue')
    expect(result.message).toContain('never delete')
  }
  expect(h.prompt).not.toHaveBeenCalled()
})

test('a delete that cannot be rewritten, or the trash kill switch, keeps the prompt', async () => {
  // The macOS behavior; the Windows test below covers the host that never prompts.
  const saved = process.env.PM_OS_RM_GUARD_PLATFORM
  process.env.PM_OS_RM_GUARD_PLATFORM = 'darwin'
  try {
    const sudo = harness()
    expect((await sudo.call(bash('sudo rm -rf /outside/example'))).action).toBe('reject-and-continue')
    expect(sudo.prompt).toHaveBeenCalledTimes(1)
    const off = harness({ 'pm-os-guardrails.rm-rf.trash.disabled': true })
    expect((await off.call(bash('rm -rf /outside/example'))).action).toBe('reject-and-continue')
    expect(off.prompt).toHaveBeenCalledTimes(1)
  } finally {
    if (saved === undefined) delete process.env.PM_OS_RM_GUARD_PLATFORM
    else process.env.PM_OS_RM_GUARD_PLATFORM = saved
  }
})

test('Windows: a delete that cannot be rewritten is rejected, never confirmed into a hard delete', async () => {
  const saved = process.env.PM_OS_RM_GUARD_PLATFORM
  process.env.PM_OS_RM_GUARD_PLATFORM = 'win32'
  try {
    const win = harness()
    const result = await win.call(bash('sudo rm -rf /c/Users/Pat/old-work'))
    expect(result.action).toBe('reject-and-continue')
    expect(result.message).toContain('Recycle Bin')
    expect(result.message).toContain('Nothing was deleted')
    expect(win.prompt).not.toHaveBeenCalled()
    expect(logs).toContainEqual(expect.objectContaining({ rule: 'rm-rf', action: 'reject-no-recycle-rewrite' }))
    expect(logs.filter(l => l.action === 'allowed-after-confirm')).toEqual([])
    // A plain delete still rewrites to the helper (which sends it to the Recycle Bin).
    expect((await win.call(bash('rm -rf /c/Users/Pat/old-work'))).action).toBe('allow-with-input')
    // The explicit kill switch keeps the old prompt (the owner opted out).
    const off = harness({ 'pm-os-guardrails.rm-rf.trash.disabled': true })
    expect((await off.call(bash('rm -rf /c/Users/Pat/old-work'))).action).toBe('reject-and-continue')
    expect(off.prompt).toHaveBeenCalledTimes(1)
  } finally {
    if (saved === undefined) delete process.env.PM_OS_RM_GUARD_PLATFORM
    else process.env.PM_OS_RM_GUARD_PLATFORM = saved
  }
})

// `sd=<session scratchpad>; … > $sd/q.json` passes the CoS delivery gate only
// when the gate resolves the variable.
const SCRATCH = '/private/tmp/claude-501/-Users-tester-Development-pm-os/0f1e2d3c/scratchpad'

test('CoS steer 3: the delivery gate resolves variables the command assigns', async () => {
  expect(gatedBashWriteTargets(`sd=${SCRATCH}; pmos slack search --query x > $sd/q.json`)).toEqual([])
  expect(gatedBashWriteTargets(`sd="${SCRATCH}"\npython3 -c 'print(1)' > "$sd/q.json"`)).toEqual([])
  expect(gatedBashWriteTargets(`export OUT=/tmp/x; echo hi | tee $OUT/a.txt`)).toEqual([])
  // Unknown, reassigned, or non-temp values stay gated.
  expect(gatedBashWriteTargets('echo hi > $nope/q.json')).toEqual(['$nope/q.json'])
  expect(gatedBashWriteTargets(`sd=${SCRATCH}; sd=/Users/someone/Documents; echo hi > $sd/q.json`)).toEqual(['$sd/q.json'])
  expect(gatedBashWriteTargets('sd=/Users/someone/Documents; echo hi > $sd/q.json')).toEqual(['$sd/q.json'])
  expect(gatedBashWriteTargets(`sd=${SCRATCH}; echo hi > '$sd/q.json'`)).toEqual(["'$sd/q.json'"])
  // Code review: a `..` after the variable cannot climb out of the temp dir and pass.
  // The repo as a shell path: `C:/x` is `/c/x` in Git Bash (`/tmp/..C:/x` would be a real temp subfolder).
  const shellRepo = lib.PM_OS_REPO.replace(/^([A-Za-z]):\//, (_m, d: string) => `/${d.toLowerCase()}/`)
  expect(gatedBashWriteTargets(`sd=/tmp; echo hi > $sd/..${shellRepo}/knowledge/x.md`)).toEqual([`$sd/..${shellRepo}/knowledge/x.md`])

  const h = harness()
  expect(await h.call(bash(`sd=${SCRATCH}; pmos slack search --query x > $sd/q.json`))).toEqual({ action: 'allow' })
  expect(h.prompt).not.toHaveBeenCalled()
})
