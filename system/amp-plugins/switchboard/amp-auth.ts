// Rule 9e: no agent runs Amp in a way that starts its device sign-in, which opens a tab in the
// owner's browser. A tool run with HOME and USERPROFILE set to a temp folder finds no stored key
// there and starts that sign-in, so each run opens a sign-in tab in the owner's browser. Mirror of rule 9 in .claude/hooks/switchboard-guardrails.py.
// Tests: amp-auth.test.ts (bun) and system/scripts/test_amp_auth_guard.py (the Claude hook).
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, isAbsolute, join } from 'node:path'

// An amp call: the binary by path, a quoted "amp" argv entry, or lowercase `amp <subcommand>`
// (prose such as "Amp session" never counts).
const AMP_SUBCOMMANDS =
  '(?:-x\\b|--\\w|-\\s|skills?\\b|threads?\\b|login\\b|logout\\b|usage\\b|tools\\b|mcp\\b|permissions\\b|plugins?\\b|config\\b|update\\b|review\\b)'
const AMP_CALL_RE = new RegExp(
  `\\.amp[\\\\/]+bin[\\\\/]+amp\\b|\\bamp\\.exe\\b|\\[\\s*["']amp["']\\s*,|which\\(\\s*["']amp["']\\s*\\)|(?:^|[\\s;&|(\`"'])amp\\s+${AMP_SUBCOMMANDS}`,
  'm',
)
const AMP_SIGN_IN_TEXT_RE = /(?:^|[\s;&|(`"'])amp(?:\.exe)?["']?\s+(?:login|logout)\b/m
const HOME_VARS = '(?:HOME|USERPROFILE|HOMEPATH|HOMEDRIVE|XDG_DATA_HOME|XDG_CONFIG_HOME|APPDATA|LOCALAPPDATA)'
// Variable names are case-sensitive, so a key map's "home" never counts, and `HOME="${HOME:-x}"`
// (keep the real value) is not an override.
const HOME_OVERRIDE_RE = new RegExp(
  [
    `(?<![\\w$.:])(?<v>${HOME_VARS})=(?!=)(?!["']?\\$\\{?\\k<v>\\b)`,
    `\\$[eE][nN][vV]:${HOME_VARS}\\s*=(?!=)`,
    `["']${HOME_VARS}["']\\s*(?:\\]\\s*=(?!=)|:)`,
    `\\benv\\b[^\\n;|&]*\\s-u\\s*${HOME_VARS}\\b`,
    `\\b(?:putenv|setdefault|update)\\(\\s*["']?${HOME_VARS}\\b`,
  ].join('|'),
)
const NEAR_LINES = 15 // an override counts only this close to the amp call (the incident script had them 2 lines apart)
const SCRIPT_RUNNER_RE = /^(?:python[\d.]*|py|bash|sh|node|bun|pwsh|powershell|deno)(?:\.exe)?$/i
const SCRIPT_EXT_RE = /\.(?:py|sh|js|mjs|cjs|ts|ps1)$/i
const SCAN_EXEMPT = new Set(['test_amp_auth_guard.py', 'switchboard-guardrails.py', 'amp-auth.test.ts', 'amp-auth.ts'])

export const AMP_FIX =
  'Run amp with the real home and point the test at a temp project folder (cwd) instead, or ask the owner through ask-cos.sh. Never sign in to anything.'

function textProblem(text: string, signIn: boolean): string | null {
  if (signIn && AMP_SIGN_IN_TEXT_RE.test(text)) {
    return "agents never run `amp login` or `amp logout`: Amp's sign-in opens a browser tab in the owner's browser."
  }
  const lines = text.split(/\r?\n/)
  const calls = lines.flatMap((l, i) => (AMP_CALL_RE.test(l) ? [i] : []))
  const overrides = calls.length ? lines.flatMap((l, i) => (HOME_OVERRIDE_RE.test(l) ? [i] : [])) : []
  if (calls.some(c => overrides.some(o => Math.abs(c - o) <= NEAR_LINES))) {
    return "amp with an overridden HOME, USERPROFILE, APPDATA, or XDG folder finds no stored key, starts its device sign-in, and opens a browser tab in the owner's browser."
  }
  return null
}

/** Commands as token lists; quoted text stays one token, so a sentence that mentions `amp login` is one argument. */
function parseShell(command: string): string[][] {
  const segments: string[][] = [[]]
  let token = ''
  let has = false
  let quote = ''
  const flush = () => {
    if (has) segments[segments.length - 1].push(token)
    token = ''
    has = false
  }
  for (const ch of command) {
    if (quote) {
      if (ch === quote) quote = ''
      else token += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
      has = true
    } else if (';|&\n(){}'.includes(ch)) {
      flush()
      segments.push([])
    } else if (/\s/.test(ch)) flush()
    else {
      token += ch
      has = true
    }
  }
  flush()
  return segments.filter(s => s.length)
}

const word = (t: string) => t.split(/[\\/]/).pop()!.toLowerCase().replace(/\.exe$/, '')

function resolvePath(raw: string, cwd: string): string {
  const home = homedir()
  let p = raw
    .replace(/\$\{?(\w+)\}?|%(\w+)%/g, (m, a, b) => process.env[a ?? b] ?? m)
    .replace(/^~(?=[\\/]|$)/, home)
  const msys = /^\/([a-zA-Z])\/(.*)$/.exec(p)
  if (msys && process.platform === 'win32') p = `${msys[1]}:/${msys[2]}`
  return isAbsolute(p) ? p : join(cwd || process.cwd(), p)
}

function scriptsRun(command: string, cwd: string): string[] {
  const found: string[] = []
  let here = cwd
  for (const seg of parseShell(command)) {
    const w = word(seg[0])
    if (w === 'cd' && seg.length > 1) {
      here = resolvePath(seg[1], here)
      continue
    }
    if (!SCRIPT_RUNNER_RE.test(w)) continue
    const arg = seg.slice(1).find(t => !t.startsWith('-'))
    if (arg && SCRIPT_EXT_RE.test(arg)) found.push(resolvePath(arg, here))
  }
  return found
}

function secretsPresent(): boolean {
  // Amp keeps its key in a file on Windows; macOS may use the keychain, so only Windows is checked.
  if (process.platform !== 'win32') return true
  const data = process.env.XDG_DATA_HOME || join(homedir(), '.local', 'share')
  return existsSync(join(data, 'amp', 'secrets.json'))
}

export function ampAuthViolation(command: string, cwd = ''): string | null {
  for (const seg of parseShell(command)) {
    const words = seg.filter(t => !/^\w+=/.test(t))
    while (words.length && (['env', 'timeout', 'nohup', 'command', 'exec'].includes(word(words[0])) || /^-|^\d+[smh]?$/.test(words[0]))) words.shift()
    if (words.length > 1 && word(words[0]) === 'amp' && /^(?:login|logout)$/i.test(words[1])) {
      return "agents never run `amp login` or `amp logout`: Amp's sign-in opens a browser tab in the owner's browser."
    }
  }
  const direct = textProblem(command, false)
  if (direct) return direct
  for (const path of scriptsRun(command, cwd)) {
    if (SCAN_EXEMPT.has(basename(path))) continue
    let text: string
    try {
      text = readFileSync(path, 'utf8').slice(0, 400_000)
    } catch {
      continue
    }
    const problem = textProblem(text, true)
    if (problem) return `${basename(path)} runs ${problem}`
  }
  if (AMP_CALL_RE.test(command) && !secretsPresent()) {
    return 'Amp is signed out on this machine, so any amp call starts a sign-in that opens a browser tab.'
  }
  return null
}
