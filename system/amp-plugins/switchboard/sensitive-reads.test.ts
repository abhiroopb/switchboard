import { expect, test } from 'bun:test'
import * as reads from './sensitive-reads'
import { envDumpViolation } from './sensitive-reads'

// Pins the Amp mirror of the environment read guard. Every path is invented and nothing is opened.
// The Downloads read block is retired, so agents read and search Downloads, text and binary.
// Run: bun test system/amp-plugins/switchboard/sensitive-reads.test.ts

const DL = 'C:/Users/invented/Downloads'

test('the Downloads read block is gone from the module', () => {
  const names = Object.keys(reads)
  expect(names).not.toContain('downloadsTextViolation')
  expect(names).not.toContain('downloadsToolViolation')
  expect(names).not.toContain('DOWNLOADS_MESSAGE')
})

test('reading Downloads never trips the environment guard', () => {
  for (const cmd of [
    `cat "${DL}/notes.txt"`,
    `grep -r word ${DL}`,
    'cat ~/Downloads/notes.txt',
    `python3 -c "print(open(r'${DL}/notes.txt').read())"`,
    `Get-Content "${DL}/New Invented Document.txt"`,
    `for f in ${DL}/*; do head "$f"; done`,
  ]) expect(envDumpViolation(cmd)).toBeNull()
})

test('environment dumps are rejected and names pass', () => {
  for (const cmd of ['env', 'printenv', 'export -p', 'set', 'env | sort', 'env | grep -i wmux', 'echo $INVENTED_TOKEN', 'printenv INVENTED_TOKEN',
    'wmux token', 'cat ~/AppData/Roaming/wmux/pipe-token', "python3 -c 'import os; print(os.environ)'", 'Get-ChildItem Env:', 'gci env:',
    '[Environment]::GetEnvironmentVariables()', '$env:INVENTED_TOKEN', 'cmd /c set']) {
    expect(envDumpViolation(cmd)).not.toBeNull()
  }
  for (const cmd of ['env | cut -d= -f1', "env | sed 's/=.*//'", 'env FOO=1 python3 x.py', 'printenv HOME', 'echo $HOME', 'echo ${#INVENTED_TOKEN}',
    'set -euo pipefail', 'export FOO=1', 'wmux ping', '(Get-ChildItem Env:).Name', 'gci env: | % Name', 'git commit -m "stop running env in tabs"']) {
    expect(envDumpViolation(cmd)).toBeNull()
  }
})
