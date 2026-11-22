import { afterAll, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ampAuthViolation } from './amp-auth'

// Pins the Amp mirror of rule 9: no amp call that starts Amp's device sign-in. Never runs amp.
// Run: bun test system/amp-plugins/pm-os/amp-auth.test.ts

const dir = mkdtempSync(join(tmpdir(), 'amp-auth-'))
mkdirSync(join(dir, 'data', 'amp'), { recursive: true })
writeFileSync(join(dir, 'data', 'amp', 'secrets.json'), '{}')
const savedData = process.env.XDG_DATA_HOME
process.env.XDG_DATA_HOME = join(dir, 'data')
afterAll(() => {
  if (savedData === undefined) delete process.env.XDG_DATA_HOME
  else process.env.XDG_DATA_HOME = savedData
  rmSync(dir, { recursive: true, force: true })
})

test('home overrides next to amp are rejected', () => {
  for (const cmd of [
    'HOME=/tmp/fake USERPROFILE=C:/tmp/fake amp skill list',
    'env USERPROFILE="$(cygpath -w "$H")" HOME="$H" HOMEPATH="" timeout 60 ~/.amp/bin/amp skill list',
    'XDG_DATA_HOME=/tmp/x ~/.amp/bin/amp.exe threads list',
    'env -u HOME amp skill list',
    '$env:USERPROFILE = "C:\\tmp\\fake"; amp skill list',
    "python3 -c \"import os,subprocess; subprocess.run(['amp','skill','list'], env=dict(os.environ, HOME='x'))\"",
  ]) expect(ampAuthViolation(cmd, dir)).not.toBeNull()
})

test('a script file that overrides home is rejected', () => {
  const sub = join(dir, 'skilltest')
  mkdirSync(sub, { recursive: true })
  writeFileSync(
    join(sub, 'ampl.py'),
    "import os, subprocess\nfrom pathlib import Path\nAMP = str(Path.home() / '.amp/bin/amp.exe')\n" +
      "env = dict(os.environ, USERPROFILE='C:/tmp/home1', HOME='C:/tmp/home1')\nsubprocess.run([AMP, 'skill', 'list'], env=env)\n",
  )
  expect(ampAuthViolation(`cd "${sub}" && python3 ampl.py 2>&1 | cut -c1-170`, dir)).toContain('ampl.py runs')
})

test('sign-in commands are rejected, mentions pass', () => {
  expect(ampAuthViolation('amp login', dir)).not.toBeNull()
  expect(ampAuthViolation('~/.amp/bin/amp.exe logout', dir)).not.toBeNull()
  expect(ampAuthViolation('git commit -m "docs: amp login opens a browser"', dir)).toBeNull()
})

test('ordinary amp and home use pass', () => {
  for (const cmd of [
    'amp skill list',
    'cd "$LOCALAPPDATA/Temp/proj" && timeout 60 ~/.amp/bin/amp skill list',
    'REAL=$HOME; echo $REAL',
    'PM_OS_STATE_HOME=/tmp/s bash system/scripts/report.sh',
    'ls .amp/in && grep -n USERPROFILE= notes.md',
  ]) expect(ampAuthViolation(cmd, dir)).toBeNull()
})

test('scripts that only mention both pass', () => {
  const far = join(dir, 'far.py')
  writeFileSync(far, "env = {**os.environ, 'HOME': tmp}\n" + 'pass\n'.repeat(40) + "run(['amp', 'threads', 'continue', 'T-1'])\n")
  const notify = join(dir, 'notify.sh')
  writeFileSync(notify, 'export HOME="${HOME:-C:/Users/invented}"\n# chrome instead of a turn in its Amp session\namp threads list\n')
  const keys = join(dir, 'keys.py')
  writeFileSync(keys, 'KEYS = {"home": "home", "end": "end"}\nCMD = "amp threads continue T-1"\n')
  expect(ampAuthViolation(`python3 "${far}"`, dir)).toBeNull()
  expect(ampAuthViolation(`bash "${notify}"`, dir)).toBeNull()
  expect(ampAuthViolation(`python3 "${keys}"`, dir)).toBeNull()
})

test.if(process.platform === 'win32')('any amp call is rejected while the key file is missing', () => {
  process.env.XDG_DATA_HOME = join(dir, 'empty')
  try {
    expect(ampAuthViolation('amp skill list', dir)).toContain('signed out')
    expect(ampAuthViolation('git status', dir)).toBeNull()
  } finally {
    process.env.XDG_DATA_HOME = join(dir, 'data')
  }
})
