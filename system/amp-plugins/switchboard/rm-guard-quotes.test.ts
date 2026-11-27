import { expect, test } from 'bun:test'
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { rewriteNulRedirects, rewriteToTrash, shellTextComplete } from './rm-guard'

// The command rewriters never change text they cannot read to its end.
// On 2026-11-27 two long CoS dispatches failed with "unexpected EOF while looking for
// matching `''". These fixtures keep the shapes of both commands: a long double-quoted
// brief full of apostrophes and a Windows path, and a quoted heredoc with apostrophes and
// double quotes, each ending in `2>&1 | grep -o '"message": "[^"]*"'`.
// Run: bun test system/amp-plugins/switchboard/rm-guard-quotes.test.ts

import { BS, DOUBLE_QUOTED, GREP, HEREDOC } from './test-fixtures/cos-dispatch-shapes'

test('the two CoS dispatch shapes read to the end and are never rewritten', () => {
  for (const cmd of [DOUBLE_QUOTED, HEREDOC]) {
    expect(shellTextComplete(cmd)).toBe(true)
    expect(rewriteNulRedirects(cmd)).toBeNull()
  }
})

test('a nul redirect after either shape changes only that redirect', () => {
  for (const cmd of [DOUBLE_QUOTED, HEREDOC]) {
    const withNul = `${cmd}\nwhere py 2>nul`
    expect(rewriteNulRedirects(withNul)).toBe(`${cmd}\nwhere py 2>/dev/null`)
  }
})

test('nul inside the brief text is data, not a redirect', () => {
  const cmd = `bash system/scripts/router-dispatch.sh --origin cos "Fix the >nul guard: it's writing 2>nul files" ${GREP}`
  expect(rewriteNulRedirects(cmd)).toBeNull()
  const heredoc = `cat > /tmp/b.txt <<'EOF'\nthe owner's 2>nul note\nEOF\nls 2>nul`
  expect(rewriteNulRedirects(heredoc)).toBe(`cat > /tmp/b.txt <<'EOF'\nthe owner's 2>nul note\nEOF\nls 2>/dev/null`)
})

test('text that does not close is left exactly as written', () => {
  for (const cmd of [
    "echo 'it never closes 2>nul",                         // open single quote
    'echo "still open 2>nul',                              // open double quote
    'echo `date 2>nul',                                    // open backtick
    'x=$(ls 2>nul',                                        // open $(
    "cat <<'EOF' >nul\nbody\nno terminator",              // heredoc with no delimiter line
    `echo "the owner's brief" ${GREP} 'tail`,              // trailing open quote after a grep
  ]) {
    expect(shellTextComplete(cmd)).toBe(false)
    expect(rewriteNulRedirects(cmd)).toBeNull()
  }
})

test('the Trash rewrite refuses text that does not close', () => {
  const open = "rm -rf /c/Users/Pat/old && echo 'never closed"
  const inv = { wordStart: 0, wordEnd: 2, word: 'rm', args: ['-rf', '/c/Users/Pat/old'], targets: ['/c/Users/Pat/old'], recursive: true, force: true, via: 'rm' as const, rewritable: true }
  expect(rewriteToTrash(open, [inv], '/c/Users/Pat/switchboard/system/scripts/trash-helper.sh')).toBeNull()
  const closed = "rm -rf /c/Users/Pat/old && echo 'closed'"
  expect(rewriteToTrash(closed, [inv], '/c/h.sh')).toBe("/c/h.sh -rf /c/Users/Pat/old && echo 'closed'")
})

test('quoting forms that close are read as closed', () => {
  for (const cmd of [
    `echo $'it${BS}'s fine'`,                              // ANSI-C quote with an escaped apostrophe
    `echo it${BS}'s fine`,                                 // backslash-escaped apostrophe
    "echo hi # the owner's comment",                      // apostrophe in a comment
    'echo "$(printf \'%s\' "a\'b")"',                      // quotes nested in $( ) in double quotes
    'echo ${x:-"it\'s"}',                                  // quotes inside ${ }
    'echo $((1 << 2))',                                    // shift, not a heredoc
    "cat <<-EOF\n\tit's indented\n\tEOF",                 // <<- strips leading tabs on the delimiter
    `cat <<${BS}EOF\nit's literal\nEOF`,                   // backslash-quoted delimiter
    'grep -o \'"message": "[^"]*"\' file',               // the dispatch grep pattern on its own
  ]) expect(shellTextComplete(cmd)).toBe(true)
})

// Cross-check the scanner against bash itself wherever bash is installed. One deliberate
// difference stays out of this list: bash -n accepts a heredoc that ends at end of file
// (with a warning), and the scanner calls it open, so no rewriter touches it.
const BASH = ['C:/Program Files/Git/usr/bin/bash.exe', '/bin/bash', '/usr/bin/bash'].find(p => existsSync(p))
test.skipIf(!BASH)('shellTextComplete agrees with bash -n on every fixture', () => {
  const fixtures = [
    DOUBLE_QUOTED, HEREDOC, `${HEREDOC}\nls 2>nul`,
    "echo 'open", 'echo "open', 'x=$(ls', `echo $'a${BS}'b'`, "echo hi # it's",
  ]
  for (const cmd of fixtures) {
    const r = spawnSync(BASH!, ['-n'], { input: cmd, encoding: 'utf8' })
    expect({ cmd: cmd.slice(0, 60), complete: shellTextComplete(cmd) }).toEqual({ cmd: cmd.slice(0, 60), complete: r.status === 0 })
  }
})

// Review findings on the first scanner (code-reviewer). Each pair is the
// input and what bash -n says; the scanner must agree.
const NL = '\n'
const REVIEW_OPEN = [
  "echo ${x:-'}",                                          // single quote inside unquoted ${ }
  `echo $(echo $'${BS}'' x')`,                             // $'…' inside $( ), then an open quote
  `cat <<1${NL}'${NL}1${NL}'`,                             // numeric heredoc delimiter is real
  `echo $(cat <<EOF${NL}hi)`,                              // heredoc in $( ) that never closes
]
const REVIEW_CLOSED = [
  'grep foo <<< "$x"',                                     // here-string
  'tr a b <<<$x',
  `git commit -m "$(cat <<'EOF'${NL}Fix: don't break it${NL}EOF${NL})" && rm -rf build`,
  `git commit -m "$(cat <<'EOF'${NL}a lone \` backtick${NL}EOF${NL})"`,
  `git commit -m "$(cat <<'EOF'${NL}an open ( paren${NL}EOF${NL})"`,
  "echo ${x:-'a}b'}",                                      // quoted } inside ${ }
  `cat <<EOF\r${NL}hi\r${NL}EOF\r${NL}`,                   // CRLF heredoc
  '(( x <<= 1 )); rm -rf b',                               // shift-assign
  'echo $((1 << 2)) "it\'s"',
]

test('review cases: open text reads as open, valid text reads as closed', () => {
  for (const cmd of REVIEW_OPEN) expect({ cmd, complete: shellTextComplete(cmd) }).toEqual({ cmd, complete: false })
  for (const cmd of REVIEW_CLOSED) expect({ cmd, complete: shellTextComplete(cmd) }).toEqual({ cmd, complete: true })
})

test('deep nesting reads as open without throwing', () => {
  expect(shellTextComplete('"$('.repeat(20000))).toBe(false)
})

test.skipIf(!BASH)('review cases agree with bash -n', () => {
  for (const cmd of [...REVIEW_OPEN, ...REVIEW_CLOSED]) {
    const r = spawnSync(BASH!, ['-n'], { input: cmd, encoding: 'utf8' })
    expect({ cmd, complete: shellTextComplete(cmd) }).toEqual({ cmd, complete: r.status === 0 })
  }
})
