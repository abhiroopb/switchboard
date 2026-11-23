import { expect, test } from 'bun:test'
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { analyzeRm, assignedVars, classifyTarget, planRmRf, helperWord, redirectCwds, rewriteToTrash } from './rm-guard'

// Trash instead of delete, and no false positives on temp or remote deletes.
// Run: bun test system/amp-plugins/switchboard/rm-guard.test.ts

const HOME = '/Users/tester'
const REPO = `${HOME}/Development/switchboard`
const opts = { home: HOME, repo: REPO, workdir: `${REPO}/workstreams/example` }
const HELPER = `${REPO}/system/scripts/switchboard-trash.py`

// Regression: a delete inside a heredoc that runs on a remote box must not prompt locally.
const REMOTE_HEREDOC = `S=/tmp/agent-tmp/-Users-tester-Development-acme-reports/11111111-2222-4333-8444-555555555555/scratchpad
cat > $S/job.sh <<'EOF'
set -euo pipefail
ROOT=$HOME/acme-reports-state
T=/tmp/acme-job; rm -rf $T; mkdir -p $T/in $T/work
cp $ROOT/data/a.json $ROOT/data/b.html $T/in/
source ./env.sh
cd ~/acme-reports
export TZ=UTC LANG=C.UTF-8 JOB_IN=$T/in JOB_WORK=$T/work
$ROOT/venv/bin/python - <<'PY'
import json
d = json.load(open("a.json"))
print("n", len(d))
PY
EOF
B64=$(base64 < $S/job.sh)
timeout 60 ssh buildvm -- bash -lc "echo $B64 | base64 -d > /tmp/acme-job.sh && (nohup bash /tmp/acme-job.sh > /tmp/acme-job.out 2>&1 &); echo started" 2>&1 | grep -v warning | tail -2`

test('the remote heredoc fixture matches the inline command', () => {
  const fixture = readFileSync(`${import.meta.dir}/test-fixtures/rm-heredoc-remote.sh`, 'utf8')
  expect(fixture.replace(/\n$/, '')).toBe(REMOTE_HEREDOC)
})

test('a heredoc delete that runs on a remote box has no local delete', () => {
  const plan = planRmRf(REMOTE_HEREDOC, opts)
  expect(plan.analysis.invocations).toEqual([])
  expect(plan.trash).toEqual([])
  expect(plan.blocked).toEqual([])
  expect(plan.analysis.remote).toContain('ssh')
  expect(plan.analysis.dataHeredocs).toBeGreaterThanOrEqual(1)
})

test('(a) a variable assigned earlier in the same command resolves to its temp path', () => {
  const plan = planRmRf('T=/tmp/acme-job; rm -rf $T; mkdir -p $T/sources', opts)
  expect(plan.analysis.invocations).toHaveLength(1)
  expect(plan.analysis.invocations[0].targets).toEqual(['/tmp/acme-job'])
  expect(plan.trash).toEqual([])
  expect(planRmRf('D=$(mktemp -d); rm -rf "$D"', opts).trash).toEqual([])
  expect(planRmRf('export W=${HOME}/scratch-x; rm -rf "${W}"', opts).trash[0].targets[0].resolved).toBe(`${HOME}/scratch-x`)
})

test('(b) rm text in heredoc bodies and quoted payloads written to files is data', () => {
  for (const cmd of [
    "cat > f.sh <<'EOF'\nrm -rf ~/zzz\nEOF\necho done",
    'cat > f.sh <<-EOF\n\trm -rf ~/zzz\n\tEOF',
    'echo "rm -rf ~/x" > notes.txt',
    "printf '%s\\n' 'rm -rf ~/x' >> f",
    'git commit -m "drop the rm -rf ~/old step"',
    "python3 - <<'PY'\nprint('rm -rf ~/x')\nPY",
  ]) expect(planRmRf(cmd, opts).analysis.invocations).toEqual([])
})

// A smoke test (a heredoc writing a .ts file of delete strings, then `bun run`
// of that file) must not count as a delete.
test('steer: a heredoc that writes a .ts file of delete strings, then bun run, is data', () => {
  const cmd = [
    "cat > .amp/in/rt-00000000000d/smoke.ts <<'EOF'",
    "import { planRmRf } from '../../../system/amp-plugins/switchboard/rm-guard'",
    "for (const c of ['rm -rf /outside/example', 'T=/tmp/x; rm -rf $T', \"bash -c 'rm -rf ~/foo'\"]) console.log(c, planRmRf(c, opts))",
    'EOF',
    'bun run .amp/in/rt-00000000000d/smoke.ts',
  ].join('\n')
  const plan = planRmRf(cmd, opts)
  expect(plan.analysis.invocations).toEqual([])
  expect(plan.trash).toEqual([])
  expect(plan.blocked).toEqual([])
})

// A steer that quotes a delete command must not split the prose into targets
// ('and', 'the', 'live', 'guardrail'…). Quoted prose arguments to any command
// are data.
test('steer: quoted prose arguments that quote a delete command are data', () => {
  const prose = "the owner just hit a confirm prompt from your own smoke test: 'Allow destructive delete of /outside/example'. " +
    'Your Bash heredoc wrote test strings containing rm -rf, and the live guardrail is still the old one.'
  for (const cmd of [
    `bash system/scripts/router-dispatch.sh --origin cos --steer rt-00000000000d "${prose}"`,
    `bash C:/Users/tester/Development/switchboard/system/scripts/router-dispatch.sh --origin cos --steer rt-00000000000d -c "rm -rf ~/x and the live guardrail"`,
    `cmux agent message --reply-to da5a0b9a "${prose}"`,
    `git commit -m "Trash outside targets instead of rm -rf ~/scratch"`,
    `gh pr comment 12 --body "Run rm -rf node_modules; then rm -rf ~/cache to reset"`,
    `printf '%s\\n' "rm -rf /outside/example and; the live guardrail"`,
    `python3 system/scripts/report-to-cos.sh --status done "rm -rf ~/old moved to Trash"`,
  ]) {
    const plan = planRmRf(cmd, opts)
    expect(plan.analysis.invocations).toEqual([])
  }
})

test('a heredoc or -c script fed to a local shell still runs here', () => {
  expect(planRmRf('bash <<EOF\nrm -rf ~/yyy\nEOF', opts).trash[0].targets[0].resolved).toBe(`${HOME}/yyy`)
  expect(planRmRf("bash -c 'rm -rf ~/foo'", opts).trash[0].targets[0].resolved).toBe(`${HOME}/foo`)
  expect(planRmRf("bash -e -o pipefail -c 'rm -rf ~/foo'", opts).trash[0].targets[0].resolved).toBe(`${HOME}/foo`)
  expect(planRmRf("zsh -lc 'rm -rf ~/foo'", opts).trash).toHaveLength(1)
  expect(planRmRf('x=$(rm -rf ~/sub); echo $x', opts).trash[0].targets[0].resolved).toBe(`${HOME}/sub`)
})

test('(c) payloads for remote execution are remote, never local deletes', () => {
  for (const cmd of [
    'ssh buildvm "rm -rf /data/cache"',
    "vmctl exec box -- bash -lc 'rm -rf ~/work'",
    'timeout 60 vmctl exec box -- bash -lc "rm -rf $HOME/x"',
    'docker exec web rm -rf /srv/tmp',
    'kubectl exec pod -- rm -rf /var/cache',
    "ssh buildvm bash <<'EOF'\nrm -rf ~/remote-only\nEOF",
  ]) {
    const plan = planRmRf(cmd, opts)
    expect(plan.analysis.invocations).toEqual([])
    expect(plan.analysis.remote.length).toBe(1)
  }
})

test('targets: temp, switchboard, and build run as written; repo root, .git, top-level folders are blocked', () => {
  expect(classifyTarget('/tmp/x', opts).cls).toBe('temp')
  expect(classifyTarget('/private/var/folders/ab/T/x', opts).cls).toBe('temp')
  expect(classifyTarget(`${REPO}/.amp/worktrees/rt-1`, opts).cls).toBe('switchboard')
  expect(classifyTarget('scratch', opts).cls).toBe('switchboard')
  expect(classifyTarget('./node_modules', { ...opts, workdir: `${HOME}/app` }).cls).toBe('build')
  expect(classifyTarget(REPO, opts).cls).toBe('blocked')
  expect(classifyTarget('~/Development/switchboard/', opts).cls).toBe('blocked')
  expect(classifyTarget(`${REPO}/.git`, opts).cls).toBe('blocked')
  expect(classifyTarget('~/other/.git', opts).cls).toBe('blocked')
  expect(classifyTarget('~', opts).cls).toBe('blocked')
  expect(classifyTarget('~/*', opts).cls).toBe('blocked')
  expect(classifyTarget('~/Documents', opts).cls).toBe('blocked')
  expect(classifyTarget('/', opts).cls).toBe('blocked')
  expect(classifyTarget('/usr', opts).cls).toBe('blocked')
  expect(classifyTarget('~/Documents/Demos/old', opts).cls).toBe('trash')
  expect(classifyTarget('$UNKNOWN/x', opts).cls).toBe('trash')
})

test('an outside delete is rewritten to the trash helper with its arguments unchanged', () => {
  const cmd = 'cd ~/proj && rm -rf ~/scratch-folder "$HOME/other dir" && echo ok'
  const plan = planRmRf(cmd, opts)
  expect(plan.trash).toHaveLength(1)
  expect(rewriteToTrash(cmd, plan.trash.map(t => t.invocation), HELPER))
    .toBe(`cd ~/proj && ${HELPER} -rf ~/scratch-folder "$HOME/other dir" && echo ok`)
  const nested = "bash -c 'rm -rf ~/foo'"
  expect(rewriteToTrash(nested, planRmRf(nested, opts).trash.map(t => t.invocation), HELPER)).toBe(`bash -c '${HELPER} -rf ~/foo'`)
  const dq = 'bash -c "cd /x && \\rm -rf \\"$HOME/q\\""'
  expect(rewriteToTrash(dq, planRmRf(dq, opts).trash.map(t => t.invocation), HELPER)).toBe(`bash -c "cd /x && ${HELPER} -rf \\"$HOME/q\\""`)
  const find = 'find ~/old -name "*.log" -exec rm -rf {} +'
  expect(rewriteToTrash(find, planRmRf(find, opts).trash.map(t => t.invocation), HELPER)).toBe(`find ~/old -name "*.log" -exec ${HELPER} -rf {} +`)
})

test('sudo and multi-word eval deletes cannot be rewritten and keep the prompt', () => {
  const sudo = planRmRf('sudo rm -rf /opt/foo', opts)
  expect(sudo.trash).toHaveLength(1)
  expect(rewriteToTrash('sudo rm -rf /opt/foo', sudo.trash.map(t => t.invocation), HELPER)).toBeNull()
  const ev = planRmRf('eval rm -rf ~/x', opts)
  expect(rewriteToTrash('eval rm -rf ~/x', ev.trash.map(t => t.invocation), HELPER)).toBeNull()
})

// A relative path that follows a `cd` into a worktree must not raise a confirm.
test('steer: a relative target resolves against a preceding cd in the same command', () => {
  const noWorkdir = { home: HOME, repo: REPO }
  const kit = planRmRf(`cd ${REPO}/.amp/worktrees/rt-00000000000c && rm -rf .amp/in/rt-00000000000c/kit-render`, noWorkdir)
  expect(kit.trash).toEqual([])
  expect(kit.blocked).toEqual([])
  expect(kit.analysis.invocations[0].targets).toEqual([`${REPO}/.amp/worktrees/rt-00000000000c/.amp/in/rt-00000000000c/kit-render`])
  // Relative cd, then relative target, against the tool's workdir.
  expect(planRmRf('cd ../.. && rm -rf .amp/in/x', opts).trash).toEqual([])
  // cd somewhere outside: the same relative target goes to the Trash.
  expect(planRmRf('cd ~/proj; rm -rf .amp/in/x', opts).trash[0].targets[0].resolved).toBe(`${HOME}/proj/.amp/in/x`)
  // cd to an unknown place: unresolved, the helper re-checks at run time.
  expect(planRmRf('cd "$X" && rm -rf build-cache', opts).trash[0].targets[0].cls).toBe('trash')
  expect(planRmRf('cd - && rm -rf build-cache', opts).trash[0].targets[0].reason).toContain('unresolved')
  // A cd inside `bash -c` carries into that script.
  expect(planRmRf(`bash -c 'cd ${REPO} && rm -rf .amp/in/x'`, noWorkdir).trash).toEqual([])
})

test('assignedVars: same-command assignments resolve; a reassigned name and data heredoc assignments do not', () => {
  const vars = assignedVars('sd=/private/tmp/claude-501/x/scratchpad; export Q="$sd/q"; A=1; A=2; cat > f <<EOF\nB=/etc\nEOF', opts)
  expect(vars.get('sd')).toBe('/private/tmp/claude-501/x/scratchpad')
  expect(vars.get('Q')).toBe('/private/tmp/claude-501/x/scratchpad/q')
  expect(vars.has('A')).toBe(false)
  expect(vars.has('B')).toBe(false)
  expect(assignedVars("ssh box 'R=/srv/x'", opts).has('R')).toBe(false)
})

test('only recursive-and-force deletes count; rm in argument position never does', () => {
  expect(planRmRf('rm ~/one-file.txt', opts).trash).toEqual([])
  expect(planRmRf('rm -r ~/dir', opts).trash).toEqual([])
  expect(planRmRf('rm -r -f ~/dir', opts).trash).toHaveLength(1)
  expect(planRmRf('rm --recursive --force ~/dir', opts).trash).toHaveLength(1)
  expect(analyzeRm('grep -rn "rm -rf" system/', opts).invocations).toEqual([])
  expect(analyzeRm('echo rm -rf ~/x', opts).invocations).toEqual([])
})

// Forms that must not run bare with no rewrite and no prompt.
const trashTargets = (cmd: string, o = opts) => planRmRf(cmd, o).trash.flatMap(t => t.targets.map(v => v.target))

test('review: xargs targets come from stdin, so the helper checks them', () => {
  expect(trashTargets('ls | xargs rm -rf')).toEqual(['$XARGS_INPUT'])
  expect(trashTargets('cat list | xargs -I{} rm -rf {}')).toEqual(['$XARGS_INPUT'])
  const plan = planRmRf('ls | xargs rm -rf', opts)
  expect(rewriteToTrash('ls | xargs rm -rf', plan.trash.map(t => t.invocation), HELPER)).toBe(`ls | xargs ${HELPER} -rf`)
})

test('review: find roots come after find\'s own options', () => {
  expect(trashTargets(`find -x ${HOME}/Documents/foo -exec rm -rf {} +`)).toEqual([`${HOME}/Documents/foo`])
  expect(trashTargets(`find -E -L ${HOME}/Documents/foo -name x -exec rm -rf {} ;`)).toEqual([`${HOME}/Documents/foo`])
  expect(trashTargets(`find -f ${HOME}/Documents/foo -exec rm -rf {} +`)).toEqual([`${HOME}/Documents/foo`])
  expect(trashTargets('find /tmp/x -exec rm -rf {} +')).toEqual([])
})

test('review: a name bound to more than one value never resolves', () => {
  expect(trashTargets('T=/tmp/x; for T in ~/Documents/a; do rm -rf "$T"; done')).toEqual(['$T'])
  expect(trashTargets('T=/tmp/x; f(){ rm -rf "$T"; }; T=~/Documents/a; f')).toEqual(['$T'])
  expect(trashTargets('ls | while read T; do rm -rf "$T"; done')).toEqual(['$T'])
  // One stable value still resolves, and the temp delete runs as written.
  expect(trashTargets('T=/tmp/x; rm -rf $T; mkdir -p $T')).toEqual([])
  // `A=b cmd` does not change cmd's own words.
  expect(trashTargets('T=/tmp/x rm -rf $T')).toEqual(['$T'])
})

test('review: brace lists and a .. after a wildcard go to the helper', () => {
  expect(classifyTarget('{/tmp/a,~/Documents/b}', opts).cls).toBe('trash')
  expect(classifyTarget('/tmp/x*/../../Users/tester/Documents/a', opts).cls).toBe('trash')
  expect(classifyTarget('/tmp/x*', opts).cls).toBe('temp')
  expect(classifyTarget(`${REPO}/.amp/in/rt-1/*`, opts).cls).toBe('switchboard')
  expect(classifyTarget('*', { ...opts, workdir: HOME }).cls).toBe('blocked')
})

test('review: a temp or repo path reached through a symlink is judged by where it lands', () => {
  const links: Record<string, string> = { '/tmp/link': `${HOME}/Documents` }
  const realpath = (p: string, followLast: boolean) => {
    const parent = followLast ? p : p.slice(0, p.lastIndexOf('/'))
    for (const [from, to] of Object.entries(links)) if (parent === from || parent.startsWith(`${from}/`)) return p.replace(from, to)
    return p
  }
  const o = { ...opts, realpath }
  expect(classifyTarget('/tmp/link/proj', o)).toMatchObject({ cls: 'trash', resolved: `${HOME}/Documents/proj` })
  // rm removes a link itself, not what it points to.
  expect(classifyTarget('/tmp/link', o).cls).toBe('temp')
  expect(classifyTarget('/tmp/link/', o).cls).toBe('blocked')
  expect(classifyTarget(`${REPO}/.GIT`, opts).cls).toBe('blocked')
})

test('review: a cd inside a subshell does not move later relative deletes', () => {
  const o = { ...opts, workdir: `${HOME}/Documents/proj` }
  expect(trashTargets('(cd /tmp/a && true); rm -rf build-stuff', o)).toEqual(['$CWD_UNKNOWN/build-stuff'])
  expect(trashTargets(`cd ${REPO}/.amp/worktrees/rt-1 && rm -rf .amp/in/rt-1/x`, o)).toEqual([])
})

test('review: the CoS gate does not resolve rebound, late, or templated names', () => {
  expect(assignedVars('sd=/tmp/x; for sd in /etc; do echo>$sd/q; done', opts).has('sd')).toBe(false)
  expect(assignedVars('echo>$sd/q; sd=/tmp/x', opts).has('sd')).toBe(false)
  expect(assignedVars('sd=$(mktemp -d /etc/foo.XXXX); echo>$sd/q', opts).get('sd')).toBe('$(mktemp -d /etc/foo.XXXX)')
  expect(assignedVars('sd=$(mktemp -d); echo>$sd/q', opts).get('sd')).toBe('/tmp/<mktemp>')
  expect(assignedVars('A=/tmp/x cmd > $A/f', opts).has('A')).toBe(false)
})

// S= is set on the first line, the rm sits on the last, and both targets are
// the worker's Claude scratchpad under /private/tmp. Temp: no prompt and no Trash rewrite.
test('a scratchpad delete with S set lines earlier is temp', () => {
  const tall = JSON.parse(readFileSync(`${import.meta.dir}/../../scripts/fixtures/permission_relay_tall_command.json`, 'utf8'))
  const plan = planRmRf(tall.command, opts)
  expect(plan.analysis.invocations).toHaveLength(1)
  expect(plan.analysis.invocations[0].targets.every((t: string) => t.startsWith('/private/tmp/claude-501/'))).toBe(true)
  expect(plan.trash).toEqual([])
  expect(plan.blocked).toEqual([])
})

test('redirectCwds: each redirect and tee target carries the folder a same-command cd left the shell in', () => {
  const cwds = redirectCwds('echo a > first.txt; cd /private/tmp/x/scratchpad && for f in a b; do echo $f > ld-$f.json; done; echo y | tee -a t.json; (cd /tmp/y && echo z > sub.json); cd "$dest"; echo w > late.json', opts)
  expect(cwds.get('first.txt')).toEqual(new Set(['']))
  expect(cwds.get('ld-$f.json')).toEqual(new Set(['/private/tmp/x/scratchpad']))
  expect(cwds.get('t.json')).toEqual(new Set(['/private/tmp/x/scratchpad']))
  expect([...cwds.get('sub.json')!][0].startsWith('$')).toBe(true)
  expect([...cwds.get('late.json')!][0].startsWith('$')).toBe(true)
  // A data heredoc body is not shell: its `>` is never a redirect.
  expect(redirectCwds("cd /tmp && cat <<'EOF'\necho > body.json\nEOF", opts).has('body.json')).toBe(false)
})

test('helperWord escapes spaces, maps a Windows path to its Git Bash form, and refuses a quoted or UNC path', () => {
  expect(helperWord('/opt/switchboard/system/scripts/switchboard-trash.py')).toBe('/opt/switchboard/system/scripts/switchboard-trash.py')
  expect(helperWord('/Users/someone/My Drive/switchboard/switchboard-trash.py')).toBe('/Users/someone/My\\ Drive/switchboard/switchboard-trash.py')
  // Windows: the path Bun builds from HOME becomes the Git Bash path the shell can run.
  expect(helperWord('C:/Users/someone/switchboard/system/scripts/switchboard-trash.py')).toBe('/c/Users/someone/switchboard/system/scripts/switchboard-trash.py')
  expect(helperWord('C:\\Users\\Pat/Development/switchboard/system/scripts/switchboard-trash.py')).toBe('/c/Users/Pat/Development/switchboard/system/scripts/switchboard-trash.py')
  expect(helperWord('D:\\My Drive\\switchboard\\system\\scripts\\switchboard-trash.py')).toBe('/d/My\\ Drive/switchboard/system/scripts/switchboard-trash.py')
  expect(helperWord('\\\\server\\share\\switchboard-trash.py')).toBeNull()
  expect(helperWord("C:\\Users\\it's\\switchboard-trash.py")).toBeNull()
  expect(helperWord("/Users/someone/it's/switchboard-trash.py")).toBeNull()
  expect(helperWord('/Users/someone/$HOME/switchboard-trash.py')).toBeNull()
  expect(helperWord('/Users/someone/a\nb/switchboard-trash.py')).toBeNull()
})

test('an escaped helper runs as one word at top level and inside every quoting form', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rm guard helper '))
  const helper = join(dir, 'fake trash.sh')
  writeFileSync(helper, '#!/bin/sh\nprintf "%s|" "$@"\n')
  chmodSync(helper, 0o755)
  const word = helperWord(helper)!
  const opts = { home: '/Users/someone', repo: '/Users/someone/Development/switchboard', workdir: '' }
  try {
    for (const cmd of ['rm -rf ~/foo', "bash -c 'rm -rf ~/foo'", 'bash -c "rm -rf ~/foo"', "bash -c $'echo \\'a\\'; rm -rf ~/foo'"]) {
      const out = rewriteToTrash(cmd, planRmRf(cmd, opts).trash.map(t => t.invocation), word)
      expect(out).not.toBeNull()
      const run = spawnSync('bash', ['-c', out!], { encoding: 'utf8', env: { ...process.env, HOME: '/Users/someone' } })
      expect(run.status).toBe(0)
      expect(run.stdout.endsWith('-rf|/Users/someone/foo|')).toBe(true)
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('in-repo-only invocations are planned for the helper, never the Trash list', () => {
  const plan = planRmRf(`rm -rf ${REPO}/local/skills-archive/old && rm -rf /outside/x`, opts)
  expect(plan.repo.map(r => r.targets.map(t => t.resolved))).toEqual([[`${REPO}/local/skills-archive/old`]])
  expect(plan.trash.map(r => r.targets.map(t => t.resolved))).toEqual([['/outside/x']])
  // A mixed invocation stays on the Trash list; its in-repo target is judged by the helper too.
  const mixed = planRmRf(`rm -rf ${REPO}/local/a /outside/y`, opts)
  expect(mixed.repo).toEqual([])
  expect(mixed.trash).toHaveLength(1)
  expect(planRmRf('rm -rf /tmp/x', opts).repo).toEqual([])
})
