import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'

// Rule 9c: the organizing-downloads script only ever runs from an
// approved plan. A flag check is not enough, because an old restored copy of the script
// ignores --manifest and runs its plain execute anyway, so the script itself is checked too.
// Mirrored for Claude Code in .claude/hooks/pm-os-guardrails.py; keep them in step.

export const ORGANIZE_SAFE_MARKER = 'PLAIN_EXECUTE_REFUSAL'
// An old copy runs `execute_flag = "--execute" in sys.argv` as code at the start of a line. A guard or
// test that quotes it in a string (this file, the Claude hook, their tests) is not a copy.
const OLD_LINE = /^[ \t]*execute_flag\s*=\s*\(?\s*["']--execute["']\s+in\s+sys\.argv\b/m
const NAME = /(?:^|[\\/])organize(?:[_.-][\w.-]*)?\.py(?:\.\w+)?$/i
const OLD_CALL = /scan_and_organize\(\s*(?:execute\s*=\s*)?True/
const ASSIGN = /(?:^|[\s;&])\$?(\w+)\s*=\s*["']?([^"'\s;&|]*organize[^"'\s;&|]*\.py)/gi
const HEREDOC = /<<-?\s*["']?(\w+)["']?[^\n]*\n[\s\S]*?\n\s*\1[ \t]*(?=\n|$)/g
// Commands that only read or quote their arguments, so naming the script isn't running it.
const READ_ONLY = new Set([
  'cat', 'grep', 'egrep', 'rg', 'echo', 'printf', 'less', 'more', 'head', 'tail', 'sed', 'awk', 'git', 'gh', 'ls', 'dir', 'wc', 'diff',
  'type', 'get-content', 'gc', 'select-string', 'sls', 'write-output', 'write-host', 'code', 'vim', 'nano', 'stat', 'file', 'cp', 'mv',
])
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'pwsh', 'powershell', 'cmd'])
const EVALUATORS = new Set(['invoke-expression', 'iex', 'start-process', 'saps', 'start'])
const START_PROCESS_FLAGS = new Set(['-argumentlist', '-filepath', '-command', '-nonewwindow', '-wait'])

type Read = (path: string) => string

function resolveScript(raw: string, cwd: string): string {
  const home = homedir()
  let path = raw.replace(/\$env:USERPROFILE|%USERPROFILE%|\$\{?HOME\}?/gi, home).replace(/^~(?=[\\/])/, home)
  const msys = path.match(/^\/([a-zA-Z])\/(.*)$/)
  if (msys && process.platform === 'win32') path = `${msys[1]}:/${msys[2]}`
  return isAbsolute(path) || /^[a-zA-Z]:[\\/]/.test(path) ? path : join(cwd, path)
}

// Commands as token lists. Quoted text stays one token, so a sentence that mentions the script is one argument.
function parseShell(command: string): string[][] {
  const segments: string[][] = [[]]
  let token = ''
  let hasToken = false
  let quote = ''
  const flush = () => {
    if (hasToken) segments[segments.length - 1].push(token)
    token = ''
    hasToken = false
  }
  for (const ch of command) {
    if (quote) {
      if (ch === quote) quote = ''
      else token += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
      hasToken = true
    } else if (';|&\n(){}'.includes(ch)) {
      flush()
      segments.push([])
    } else if (/\s/.test(ch)) {
      flush()
    } else {
      token += ch
      hasToken = true
    }
  }
  flush()
  return segments.filter(s => s.length > 0)
}

const commandWord = (token: string) => (token.split(/[\\/]/).pop() ?? '').toLowerCase().replace(/\.exe$/, '')

function scriptProblem(path: string, cwd: string, read: Read): string | null {
  try {
    return read(resolveScript(path, cwd)).includes(ORGANIZE_SAFE_MARKER)
      ? null
      : 'this copy of organize.py predates the approved-plan fix and would ignore --manifest.'
  } catch {
    return 'this copy of organize.py could not be read, so it could not be checked.'
  }
}

function oldCopyUnderOtherName(token: string, cwd: string, read: Read): boolean {
  if (!token.toLowerCase().endsWith('.py')) return false
  try {
    return read(resolveScript(token, cwd)).match(OLD_LINE) !== null
  } catch {
    return false
  }
}

function checkSegments(command: string, startCwd: string, variables: Map<string, string>, read: Read, depth = 0): string | null {
  let cwd = startCwd
  for (const tokens of parseShell(command)) {
    // Skip environment prefixes and PowerShell's assignment form to find the command itself.
    let start = 0
    while (start < tokens.length && (/^\w+=/.test(tokens[start]) || tokens[start] === '=' || tokens[start + 1] === '=')) start += 1
    if (start >= tokens.length) continue
    const word = commandWord(tokens[start])
    const rest = tokens.slice(start + 1)
    if (['cd', 'set-location', 'sl', 'chdir', 'pushd', 'push-location'].includes(word) && rest.length) {
      cwd = resolveScript(rest[0], cwd)
      continue
    }
    if (READ_ONLY.has(word)) continue
    if (depth < 3) {
      let payload: string | null = null
      if (SHELLS.has(word) && rest.length && /^(?:-\w*c|-c(?:ommand)?|\/c)$/i.test(rest[0])) payload = rest.slice(1).join(' ')
      else if (EVALUATORS.has(word)) payload = rest.filter(t => !START_PROCESS_FLAGS.has(t.toLowerCase())).join(' ')
      if (payload !== null) {
        const problem = checkSegments(payload, cwd, variables, read, depth + 1)
        if (problem) return problem
        continue
      }
    }
    if (/^python[\dw.]*$|^py$/.test(word) && rest.includes('-c')) {
      const code = rest.slice(rest.indexOf('-c') + 1).join(' ')
      if (code.includes('organize') && code.includes('--execute')) return 'running organize.py from `python -c` bypasses the approved plan.'
    }
    if (/^python[\dw.]*$|^py$/.test(word)) {
      if (rest.includes('-m')) {
        const module = rest[rest.indexOf('-m') + 1] ?? ''
        if (module.toLowerCase().startsWith('organize')) {
          if (rest.some(t => t.replaceAll('\\', '') === '--execute')) return 'running organize as a module bypasses the approved plan.'
          if (oldCopyUnderOtherName(`${module}.py`, cwd, read)) return 'this copy of organize.py predates the approved-plan fix, and no run of it is safe.'
        }
      }
      if (rest.some(t => oldCopyUnderOtherName(t, cwd, read))) return 'this copy of organize.py predates the approved-plan fix, and no run of it is safe.'
    }
    const resolved = tokens.map(t => variables.get(t.replace(/^\$\{?(\w+)\}?$/, '$1')) ?? t)
    const scripts = resolved.filter(t => NAME.test(t))
    const executes = resolved.some(t => t.replaceAll('\\', '') === '--execute')
    if (executes && !scripts.length && resolved.some(t => oldCopyUnderOtherName(t, cwd, read))) {
      return 'this script is an old copy of organize.py under another name, and it would ignore --manifest.'
    }
    if (!scripts.length) continue
    // An unfixed copy may unpack archives even on a preview, so no run of it is safe. An unreadable file isn't proof.
    if (oldCopyUnderOtherName(scripts[0], cwd, read)) return 'this copy of organize.py predates the approved-plan fix, and no run of it is safe.'
    const hasManifest = resolved.some(t => t === '--manifest' || t.toLowerCase().startsWith('--manifest='))
    if (!hasManifest) {
      if (executes) return '`organize.py --execute` needs `--manifest <approved plan>`.'
      if (resolved.slice(resolved.indexOf(scripts[0]) + 1).some(t => /^\$\w+$|^@\w+$/.test(t))) return "organize.py's flags are in a variable, so they could not be checked."
    } else if (executes) {
      const problem = scriptProblem(scripts[0], cwd, read)
      if (problem) return problem
    }
  }
  return null
}

export function organizeExecuteViolation(
  cmd: string,
  cwd: string,
  read: Read = path => readFileSync(path, 'utf8'),
): string | null {
  const flat = cmd.replace(/[\\`]\r?\n\s*/g, ' ')
  if (OLD_CALL.test(flat) && /(?:^|[\s&;|"'\\/])(?:python[\dw.]*|py)(?:\.exe)?["']?\s/i.test(flat)) {
    return 'calling scan_and_organize(execute=True) bypasses the approved plan.'
  }
  const body = flat.replace(HEREDOC, '')
  const variables = new Map<string, string>()
  for (const m of body.matchAll(ASSIGN)) variables.set(m[1], m[2])
  return checkSegments(body, cwd, variables, read)
}

export const ORGANIZE_EXECUTE_FIX = 'Preview with no flags, have the owner review the plan, run `--approve <plan> --approved-by <name>`, then `--execute --manifest <plan>`. The organizing-downloads skill has the steps.'
