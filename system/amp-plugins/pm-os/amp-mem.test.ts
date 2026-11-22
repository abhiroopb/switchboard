import { afterAll, beforeAll, expect, test } from 'bun:test'
import { execFileSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { contextArgs, inferProject, isAutomationEnvelope, isSkillCorrection } from '../amp-mem'

const root = resolve(import.meta.dir, '../../..')

// ── Part A fix 1: machine-written turns are never skill corrections ──────────

const AUTOMATION = '[AUTOMATION job=comms-triage dest=megadesk] Run the comms fastpath. Never post to Slack from this job; always use the ledger.'
const AUTOPOST = '[NEXT-UP AUTOPOST] 🔴 Act now\n- open 3 · always run check-ci before merging'
const ROUTER_REPORT = '[ROUTER REPORT] rt-000000000005 done: never use the old path again'
const WORKSPACE_REPORT = '[WORKSPACE REPORT] launch-notes: always use the new deck'
const WORKER_PROMPT = 'You are PM OS worker worker-442f7a0a7926 for task rt-000000000005 (attempt rt-000000000005.a1.5186ef), workstream launch-notes, origin cos. Never push. Do not commit; the finish step commits for you.'
const SEED_PROMPT = 'IMPORTANT: Skip all AGENTS.md session-start steps (todo, kb-distill). You are a focused workspace launched by start-day.sh. Always use the seed note.'
const STEER = 'Steer from the owner via CoS: "Rewriting amp-mem is fine." Always keep Part A small.'
const SUBAGENT_DONE = 'A background subagent you started has ended. Never rely on its summary alone.'
const THREAD_META = 'Please land this. Always use the ledger. Thread message metadata: this message was sent by Amp thread `T-00000000-0000-4000-8000-0000000000a1`, not directly by the user.'

test('every PM OS envelope shape is an automation envelope', () => {
  for (const msg of [AUTOMATION, AUTOPOST, ROUTER_REPORT, WORKSPACE_REPORT, WORKER_PROMPT, SEED_PROMPT, STEER, SUBAGENT_DONE, THREAD_META]) {
    expect(isAutomationEnvelope(msg)).toBe(true)
  }
  // The automation job and worker prompt also trip the correction heuristic
  // (that is how the 1,500 bogus rows were filed), so the envelope guard has
  // to run first.
  expect(isSkillCorrection(AUTOMATION)).toBe(true)
  expect(isSkillCorrection(WORKER_PROMPT)).toBe(true)
  expect(isAutomationEnvelope('')).toBe(true)
  expect(isAutomationEnvelope('   \n')).toBe(true)
})

test('a human correction is not an envelope and still counts as a correction', () => {
  const human = "no, always use pmos slack for that instead of curl. Don't use the raw API."
  expect(isAutomationEnvelope(human)).toBe(false)
  expect(isSkillCorrection(human)).toBe(true)
  // Mentioning an envelope mid-sentence is fine; only a leading tag counts.
  expect(isAutomationEnvelope('why did the [NEXT-UP AUTOPOST] digest skip my item? always include P0s')).toBe(false)
})

// ── Part A fix 2: project inference ────────────────────────────────────────

const noEnv = { HOME: 'C:/Users/tester' }

test('inferProject prefers the workstream slug from the cwd', () => {
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os/workstreams/pm-os-maintenance', workspaceRoot: 'C:/Users/tester/Development/pm-os', env: noEnv })).toBe('pm-os-maintenance')
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os/.amp/worktrees/rt-000000000006/workstreams/launch-notes/sources', workspaceRoot: 'C:/Users/tester/Development/pm-os/.amp/worktrees/rt-000000000006', env: noEnv })).toBe('launch-notes')
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os/workstreams/_archive/old-thing', workspaceRoot: 'C:/Users/tester/Development/pm-os', env: noEnv })).toBe('pm-os')
})

test('inferProject maps a task worktree back to its repository and rejects HOME or /', () => {
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os/.amp/worktrees/rt-000000000006', workspaceRoot: 'C:/Users/tester/Development/pm-os/.amp/worktrees/rt-000000000006/', env: noEnv })).toBe('pm-os')
  expect(inferProject({ cwd: 'C:/Users/tester/Development/web-clone/src', workspaceRoot: 'C:/Users/tester/Development/web-clone', env: noEnv })).toBe('web-clone')
  expect(inferProject({ cwd: 'C:/Users/tester', workspaceRoot: 'C:/Users/tester', env: noEnv })).toBeNull()
  expect(inferProject({ cwd: '/', workspaceRoot: '/', env: noEnv })).toBeNull()
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os', workspaceRoot: null, env: noEnv })).toBeNull()
})

test('inferProject lets PM_OS_WORKSTREAM_SLUG win and ignores a malformed one', () => {
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os', workspaceRoot: 'C:/Users/tester/Development/pm-os', env: { ...noEnv, PM_OS_WORKSTREAM_SLUG: 'weekly-recap' } })).toBe('weekly-recap')
  expect(inferProject({ cwd: 'C:/Users/tester/Development/pm-os', workspaceRoot: 'C:/Users/tester/Development/pm-os', env: { ...noEnv, PM_OS_WORKSTREAM_SLUG: '../etc' } })).toBe('pm-os')
})

// ── Part A fixes 3 and 4: CLI --confidence and 24 h repeat dedupe ─────────────
// Runs the real bash CLI against a scratch DB under a temp HOME.

const BIN = resolve(root, 'system/bin/amp-mem')
let home = ''

function mem(args: string[], opts: { expectFail?: boolean } = {}): { code: number; out: string } {
  try {
    const out = execFileSync('bash', [BIN, ...args], { env: { ...process.env, HOME: home }, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
    return { code: 0, out }
  } catch (err) {
    const e = err as { status?: number; stdout?: string; stderr?: string }
    if (!opts.expectFail) throw err
    return { code: e.status ?? 1, out: `${e.stdout ?? ''}${e.stderr ?? ''}` }
  }
}

function sql(query: string): string {
  return execFileSync('sqlite3', [resolve(home, '.amp/memory/amp-mem.db'), query], { encoding: 'utf8' }).trim()
}

beforeAll(() => {
  home = mkdtempSync(resolve(tmpdir(), 'amp-mem-test-'))
  mkdirSync(resolve(home, '.amp/memory'), { recursive: true })
  copyFileSync(resolve(root, 'system/amp-mem/schema.sql'), resolve(home, '.amp/memory/schema.sql'))
  mem(['init'])
})

afterAll(() => {
  if (home) rmSync(home, { recursive: true, force: true })
})

test('save stores --confidence and rejects a value outside 0..1', () => {
  mem(['save', 'observation', 'Edited instruction surfaces: amp-mem.ts', '- system/amp-plugins/amp-mem.ts', '--confidence', '0.3', '--project', 'pm-os-maintenance'])
  expect(sql("SELECT confidence || '|' || project FROM observations WHERE topic LIKE 'Edited instruction surfaces%'")).toBe('0.3|pm-os-maintenance')
  mem(['save', 'decision', 'Keep amp-mem', 'Patch, do not rewrite'])
  expect(sql("SELECT confidence FROM observations WHERE topic = 'Keep amp-mem'")).toBe('1.0')
  const bad = mem(['save', 'decision', 'Bad', 'x', '--confidence', '1.5'], { expectFail: true })
  expect(bad.code).toBe(1)
  expect(bad.out).toContain('--confidence must be')
  const inj = mem(['save', 'decision', 'Bad', 'x', '--confidence', '0.5); DROP TABLE observations;--'], { expectFail: true })
  expect(inj.code).toBe(1)
  expect(sql("SELECT COUNT(*) FROM observations WHERE topic = 'Bad'")).toBe('0')
})

test('an identical save within 24 h bumps access_count instead of inserting a row', () => {
  const summary = 'the owner prefers the CRM API first for customer emails, Chrome DevTools only as fallback, never report email not returned before that.'
  mem(['save', 'preference', 'Customer emails via CRM', summary])
  const before = sql("SELECT COUNT(*) FROM observations WHERE topic = 'Customer emails via CRM'")
  expect(before).toBe('1')
  const again = mem(['save', 'preference', 'Customer emails via CRM', summary])
  expect(again.out).toContain('Repeat of observation #')
  expect(sql("SELECT COUNT(*) || '|' || MAX(access_count) FROM observations WHERE topic = 'Customer emails via CRM'")).toBe('1|1')
  // A repeated save is not a retrieval: last_accessed stays NULL so the
  // hit-rate metric in the plan note only counts search and detail.
  expect(sql("SELECT last_accessed IS NULL FROM observations WHERE topic = 'Customer emails via CRM'")).toBe('1')
  // The key includes the project: the same text under another project is a new row.
  mem(['save', 'preference', 'Customer emails via CRM', summary, '--project', 'other-ws'])
  expect(sql("SELECT COUNT(*) FROM observations WHERE topic = 'Customer emails via CRM'")).toBe('2')
})

test('a private save never matches a public row with the same text', () => {
  const summary = 'Compensation conversation notes for the 2026 cycle, keep out of context.'
  mem(['save', 'observation', 'Comp notes', summary])
  const priv = mem(['save', 'observation', 'Comp notes', summary, '--private'])
  expect(priv.out).not.toContain('Repeat of observation #')
  expect(sql("SELECT COUNT(*) FROM observations WHERE topic = 'Comp notes' AND private = 1")).toBe('1')
  expect(sql("SELECT COUNT(*) FROM observations WHERE topic = 'Comp notes'")).toBe('2')
})

test('a same-topic save with a different summary is not a 24 h repeat', () => {
  mem(['save', 'decision', 'Deck owner', 'Noel owns the overview deck through Friday.'])
  const changed = mem(['save', 'decision', 'Deck owner', 'the owner owns the overview deck from Monday; Noel hands off Friday.'])
  expect(changed.out).not.toContain('Repeat of observation #')
})

// ── Option B: scoped, index-first, budgeted injection ────────────────────────

test('contextArgs: budget 0 is the legacy dump; a budget scopes, logs and omits null flags', () => {
  expect(contextArgs(0, 'pm-os', '12')).toEqual(['context', '--lines', '60'])
  expect(contextArgs(3000, 'pm-os', '12')).toEqual(['context', '--budget-chars', '3000', '--project', 'pm-os', '--session', '12', '--log'])
  expect(contextArgs(2500.7, null, null)).toEqual(['context', '--budget-chars', '2500', '--log'])
  // Headless sessions still inject but are not logged (§9 counts interactive only)
  expect(contextArgs(3000, 'pm-os', '12', false)).toEqual(['context', '--budget-chars', '3000', '--project', 'pm-os', '--session', '12'])
})

test('context --budget-chars stays under the cap, boosts the current project, logs ids and never bumps access_count', () => {
  mem(['save', 'decision', 'Budget mode other project', 'A decision recorded under a different project, long enough to matter for ranking purposes.', '--project', 'elsewhere'])
  mem(['save', 'decision', 'Budget mode this project', 'A decision recorded under the current project, long enough to matter for ranking purposes.', '--project', 'scoped-proj'])
  const before = sql("SELECT access_count FROM observations WHERE topic = 'Budget mode this project'")

  const { out } = mem(['context', '--budget-chars', '600', '--project', 'scoped-proj', '--session', '77', '--log'])
  expect(out.length).toBeLessThanOrEqual(600)
  expect(out).toContain('# Amp Memory — scoped-proj')
  expect(out).toContain('amp-mem detail <id>')
  const thisIdx = out.indexOf('Budget mode this project')
  const otherIdx = out.indexOf('Budget mode other project')
  expect(thisIdx).toBeGreaterThan(-1)
  if (otherIdx > -1) expect(thisIdx).toBeLessThan(otherIdx)
  expect(out).not.toContain('[scoped-proj]')

  const id = sql("SELECT id FROM observations WHERE topic = 'Budget mode this project'")
  expect(out).toContain(`#${id} [`)
  // Same join the plan note's §9 hit-rate query uses: row_ids is a JSON array of ints.
  expect(sql("SELECT l.project || '|' || l.session_id FROM injection_log l WHERE EXISTS (SELECT 1 FROM json_each(l.row_ids) WHERE value = " + id + ") ORDER BY l.id DESC LIMIT 1")).toBe('scoped-proj|77')
  expect(sql("SELECT access_count FROM observations WHERE topic = 'Budget mode this project'")).toBe(before)
})

test('context --budget-chars drops junk rows and refuses a budget below the footer minimum', () => {
  mem(['save', 'preference', 'undefined', 'undefined'])
  mem(['save', 'decision', 'I do', 'I do'])
  const { out } = mem(['context', '--budget-chars', '3000', '--project', 'scoped-proj'])
  expect(out).not.toContain('preference: undefined')
  expect(out).not.toContain('decision: I do')
  expect(out).toContain('Budget mode this project')
  const tiny = mem(['context', '--budget-chars', '50'], { expectFail: true })
  expect(tiny.code).toBe(2)
  expect(tiny.out).toContain('below the minimum')
})

test('detail bumps access_count like a search hit', () => {
  const id = sql("SELECT id FROM observations WHERE topic = 'Budget mode this project'")
  const before = Number(sql(`SELECT access_count FROM observations WHERE id = ${id}`))
  const { out } = mem(['detail', id])
  expect(out).toContain('Budget mode this project')
  expect(sql(`SELECT access_count || '|' || (last_accessed IS NOT NULL) FROM observations WHERE id = ${id}`)).toBe(`${before + 1}|1`)
})

test('search --project admits rows with no project and excludes rows of another project', () => {
  mem(['save', 'discovery', 'Legacy unscoped finding', 'The ledger lives in system/state/running-tasks.json and is symlinked into worktrees.'])
  mem(['save', 'discovery', 'Elsewhere finding', 'The ledger symlink is also used by elsewhere workers.', '--project', 'elsewhere'])
  const { out } = mem(['search', 'ledger', '--project', 'scoped-proj'])
  expect(out).toContain('Legacy unscoped finding')
  expect(out).not.toContain('Elsewhere finding')
})

test('context without --budget-chars is the legacy sectioned output and writes no injection_log row', () => {
  const rows = sql("SELECT COUNT(*) FROM injection_log")
  const { out } = mem(['context', '--lines', '30'])
  expect(out).toContain('# Amp Memory — Recent Context')
  expect(sql("SELECT COUNT(*) FROM injection_log")).toBe(rows)
})
