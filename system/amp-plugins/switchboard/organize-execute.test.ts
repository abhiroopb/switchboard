import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { organizeExecuteViolation } from './organize-execute'

// Pins the rule that an agent can't run the file organizer outside an approved plan.
// Run: bun test system/amp-plugins/switchboard/organize-execute.test.ts

const safe = () => 'def main(): print(PLAIN_EXECUTE_REFUSAL)'
const old = () => 'execute_flag = "--execute" in sys.argv'
const check = (cmd: string, read = safe) => organizeExecuteViolation(cmd, '/invented', read)

test('plain execute is rejected in every shell form', () => {
  for (const cmd of [
    'python organize.py --execute',
    'python3 /invented/skills/organizing-downloads/scripts/organize.py --execute',
    'python "$env:USERPROFILE\\.claude\\skills\\organizing-downloads\\scripts\\organize.py" --execute',
    'cd /invented && python3 organize.py --verbose --execute',
    'FOO=1 python3 organize.py --execute',
    'bash -c "python3 organize.py --execute"',
    'S=organize.py; python3 "$S" --execute',
    'python3 organize.py \\\n  --execute',
    '"C:/Program Files/Invented Python/python.exe" organize.py --execute',
    'pythonw organize.py --execute',
    'timeout 60 python organize.py --execute',
    'nohup python organize.py --execute',
    '( python organize.py --execute )',
    'if true; then python organize.py --execute; fi',
    '$P organize.py --execute',
    'python organize_old.py --execute',
    'python other.py --execute --manifest x.json; python organize.py --execute',
    '& { python organize.py --execute }',
    '$out = python organize.py --execute',
    "Start-Process python -ArgumentList 'organize.py --execute'",
    "Invoke-Expression 'python organize.py --execute'",
    "pwsh -Command 'python organize.py --execute'",
    'cmd /c python organize.py --execute',
    'python3 organize.py --execute --manifest-out /invented/plan.json',
    'python3 organize.py --archive-past-years --execute',
  ]) expect(check(cmd)).toContain('needs `--manifest')
})

test('flags hidden in a variable are rejected', () => {
  expect(check('python organize.py $FLAGS')).toContain('in a variable')
})

test('execute with a plan passes on a current copy', () => {
  expect(check('python3 organize.py --execute --manifest /invented/plan.json')).toBeNull()
  expect(check('python3 organize.py --manifest=/invented/plan.json --execute')).toBeNull()
})

test('an old restored copy is rejected even with --manifest, because it ignores the flag', () => {
  expect(check('python3 organize.py --execute --manifest /invented/plan.json', old)).toContain('predates')
  expect(check('python3 renamed_tool.py --execute --manifest /invented/plan.json', old)).toContain('predates')
})

test('a guard file that quotes the old line in a string is not a copy', () => {
  // The Claude hook quotes the old line, and a run that names it must not be refused.
  const hook = '.claude/hooks/switchboard-guardrails.py'
  const real = () => readFileSync(join(import.meta.dir, '..', '..', '..', hook), 'utf8')
  for (const cmd of [`python3 ${hook} --self-test`, `python3 edit_hook.py ${hook}`, `python3 other.py --execute ${hook}`]) {
    expect(check(cmd, real)).toBeNull()
  }
  expect(check('python3 guard.py --execute --manifest /invented/plan.json', () => `const OLD = '${old()}'`)).toBeNull()
  expect(check('python3 indented.py', () => `def main():\n    ${old()}\n`)).toContain('predates')
})

test('a copy that cannot be read is rejected', () => {
  const missing = () => { throw new Error('missing') }
  expect(check('python3 organize.py --execute --manifest /invented/plan.json', missing)).toContain('could not be read')
})

test('calling the old function directly is rejected', () => {
  expect(check('python3 -c "import organize; organize.scan_and_organize(execute=True)"')).toContain('bypasses')
  expect(check('python3 -c "import organize; organize.scan_and_organize(True)"')).toContain('bypasses')
  expect(check('python3 <<EOF\nimport organize\norganize.scan_and_organize(execute=True)\nEOF')).toContain('bypasses')
})

test('previews and other commands pass, and mentions in text are not runs', () => {
  for (const cmd of [
    'python3 organize.py',
    'python3 organize.py --manifest-out /invented/plan.json',
    'python3 organize.py --check-manifest /invented/plan.json',
    'python3 organize.py --archive-past-years --manifest-out /invented/plan.json',
    'python3 organize.py --undo',
    'grep -n "organize.py --execute" notes.md',
    "bash report.sh 'plain organize.py --execute now refuses'",
    'python3 other_tool.py --execute',
    'git commit -m "guard; python organize.py --execute is refused"',
    'git commit -m "first line\n\npython organize.py --execute is refused"',
    'grep "a && python organize.py --execute" notes.md',
    'python other.py "plain organize.py --execute now refuses"',
    'python other.py --execute; cat organize.py',
    'cat <<EOF\npython organize.py --execute\nEOF',
    'cat organize.py',
  ]) expect(check(cmd, safe)).toBeNull()
  expect(check('cat organize.py', old)).toBeNull()
})

test('no run of an old copy is allowed, and the module form, escaped flag, and pushd are seen', () => {
  for (const cmd of ['python organize.py', 'python organize.py --undo', 'python organize.py --manifest-out plan.json']) {
    expect(check(cmd, old)).not.toBeNull()
  }
  expect(check('python3 -m organize --execute')).not.toBeNull()
  expect(check('python3 organize.py --ex\\ecute')).not.toBeNull()
  expect(check('python3 -m organize_helper --list')).toBeNull()
  const seen: string[] = []
  const read = (path: string) => { seen.push(path); return safe() }
  organizeExecuteViolation('pushd /invented/other && python3 organize.py --execute --manifest plan.json', '/invented', read)
  expect(seen.some(path => path.replaceAll('\\', '/').includes('invented/other'))).toBe(true)
})
