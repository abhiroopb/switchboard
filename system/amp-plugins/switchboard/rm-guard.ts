// rm-guard — the shell-aware half of switchboard-guardrails Rule 3 (rm -rf).
//
// Destructive-delete prompts in worker tabs stall work the owner does not see,
// so Rule 3 has two parts and a false-positive fix:
//
//  1. Trash instead of delete. An `rm -rf` whose target is outside switchboard, the
//     temp dirs, and build output no longer asks: its command word is
//     rewritten to system/scripts/switchboard-trash.py, which moves the targets to
//     the macOS Trash, logs each move, and lists them in the worker's report.
//     Repo-root, `.git`, and top-level system or home folders stay hard blocks.
//  2. (permission_relay.py) Any prompt that is left reaches CoS.
//  3. Shell-aware reading. An `rm -rf $T` inside a heredoc
//     written to a file (`cat > $S/job.sh <<'EOF'`), with `T` assigned to a temp
//     path on the line before and the file sent to a remote box to run, must
//     not prompt. This module reads the command as a shell does: only words in command position are
//     commands, variables assigned earlier in the same command resolve to
//     their literal value, heredoc bodies and quoted text fed to anything but
//     a local shell are data, and payloads for remote execution (ssh, a
//     remote-box `exec`, docker or kubectl exec) are remote, never local.
//
// Pure functions only (no I/O): switchboard-guardrails.ts owns logging and decisions;
// rm-guard.test.ts carries a command of the same shape.

import { posix } from 'node:path'

export interface RmGuardOptions {
  home: string
  repo: string
  /** The tool call's working directory; relative targets resolve against it. */
  workdir?: string
  /** Resolves symlinks in a path (the caller does the I/O). `followLast` is
   * false for a plain target, because rm deletes a link, not what it points to. */
  realpath?: (path: string, followLast: boolean) => string
  /** Extra temp roots (Windows: the user's temp folder, which Git Bash calls /tmp). */
  temp?: string[]
}

/** True for `/x` and, on any host, a drive path `C:/x` or `C:\x`. */
export function isAbsPath(p: string): boolean {
  return p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p)
}

/** One spelling for a Windows path, so the repo, home, and temp roots compare:
 * forward slashes, an upper-case drive letter, and Git Bash `/c/x` as `C:/x`.
 * Other hosts get the path unchanged. Without it a `C:/` repo path reads as
 * relative, so in-repo deletes would go to the Recycle Bin and `~/Documents`
 * would not be a hard block on Windows. */
export function slashPath(p: string): string {
  if (!isWindowsHost()) return p
  const s = p.replace(/\\/g, '/')
  const bash = /^\/([A-Za-z])(\/.*)?$/.exec(s)
  if (bash) return `${bash[1].toUpperCase()}:${bash[2] ?? '/'}`
  return /^[a-z]:\//.test(s) ? s[0].toUpperCase() + s.slice(1) : s
}

export interface RmInvocation {
  /** Raw offsets of the command word (`rm`, `/bin/rm`, `\rm`) in the analyzed command. */
  wordStart: number
  wordEnd: number
  word: string
  args: string[]
  targets: string[]
  recursive: boolean
  force: boolean
  /** `find … -exec rm`: the targets are the find roots. */
  via: 'rm' | 'find-exec'
  /** False when the invocation sits in text whose raw offsets cannot be mapped (multi-word eval). */
  rewritable: boolean
}

export interface RmAnalysis {
  invocations: RmInvocation[]
  /** Command words of remote-execution commands whose payload was skipped. */
  remote: string[]
  /** Heredoc bodies treated as data (not run by a local shell). */
  dataHeredocs: number
  /** Redirect and `tee` targets (unquoted, unexpanded) with the folder a
   * same-command `cd` left the shell in; filled only when the caller asks
   * (redirectCwds). */
  redirects?: Array<{ value: string; cwd: string | undefined }>
}

export type TargetClass = 'temp' | 'switchboard' | 'build' | 'blocked' | 'trash'

export interface TargetVerdict {
  target: string
  resolved: string
  cls: TargetClass
  reason: string
}

// ── Tokenizer ──────────────────────────────────────────────────────────

interface Word {
  kind: 'word'
  start: number
  end: number
  raw: string
  /** Unquoted value (quotes removed, backslash escapes applied, no expansion). */
  value: string
  /** value index → raw offset in the analyzed string. */
  map: number[]
  /** Raw ranges of `$(…)` and backtick command substitutions outside single quotes. */
  subs: Array<{ start: number; end: number }>
}

interface Op { kind: 'op'; value: string; start: number; end: number }

interface Heredoc { delim: string; stripTabs: boolean; bodyStart: number; bodyEnd: number }

interface SimpleCommand {
  words: Word[]
  heredocs: Heredoc[]
  /** Redirection targets (`> file`): never commands, but they may hold substitutions. */
  redirects: Word[]
}

const CONTROL_OPS = new Set([';', '&&', '||', '|', '|&', '&', '\n', '(', ')', ';;'])

/** Read a `$(…)` starting at `i` (pointing at `$`); returns the index after `)`. */
function skipDollarParen(s: string, i: number): number {
  let depth = 0
  let j = i + 1
  while (j < s.length) {
    const c = s[j]
    if (c === '\\') { j += 2; continue }
    if (c === "'") { const k = s.indexOf("'", j + 1); j = k < 0 ? s.length : k + 1; continue }
    if (c === '"') { j = skipDoubleQuoted(s, j); continue }
    if (c === '`') { const k = s.indexOf('`', j + 1); j = k < 0 ? s.length : k + 1; continue }
    if (c === '(') depth++
    else if (c === ')') { depth--; if (depth === 0) return j + 1 }
    j++
  }
  return s.length
}

function skipDoubleQuoted(s: string, i: number): number {
  let j = i + 1
  while (j < s.length) {
    const c = s[j]
    if (c === '\\') { j += 2; continue }
    if (c === '"') return j + 1
    if (c === '$' && s[j + 1] === '(') { j = skipDollarParen(s, j); continue }
    if (c === '`') { const k = s.indexOf('`', j + 1); j = k < 0 ? s.length : k + 1; continue }
    j++
  }
  return s.length
}

function isBlank(c: string): boolean { return c === ' ' || c === '\t' }
function isMeta(c: string): boolean { return c === ';' || c === '&' || c === '|' || c === '(' || c === ')' || c === '<' || c === '>' || c === '\n' }

/** One word from `i`; returns the word and the index after it. */
function readWord(s: string, i: number): Word {
  const start = i
  let value = ''
  const map: number[] = []
  const subs: Word['subs'] = []
  const push = (ch: string, at: number) => { value += ch; map.push(at) }
  while (i < s.length) {
    const c = s[i]
    if (isBlank(c) || isMeta(c)) break
    if (c === '\\') {
      if (s[i + 1] === '\n') { i += 2; continue }
      if (i + 1 < s.length) push(s[i + 1], i + 1)
      i += 2
      continue
    }
    if (c === "'") {
      const k = s.indexOf("'", i + 1)
      const end = k < 0 ? s.length : k
      for (let j = i + 1; j < end; j++) push(s[j], j)
      i = end + 1
      continue
    }
    if (c === '$' && s[i + 1] === "'") {
      // ANSI-C quoting: keep the text verbatim (escapes are rare in paths).
      const k = s.indexOf("'", i + 2)
      const end = k < 0 ? s.length : k
      for (let j = i + 2; j < end; j++) push(s[j], j)
      i = end + 1
      continue
    }
    if (c === '"') {
      const end = skipDoubleQuoted(s, i) - 1
      let j = i + 1
      while (j < end) {
        const d = s[j]
        if (d === '\\' && j + 1 < end && '"\\$`\n'.includes(s[j + 1])) {
          if (s[j + 1] !== '\n') push(s[j + 1], j + 1)
          j += 2
          continue
        }
        if (d === '$' && s[j + 1] === '(') {
          const k = skipDollarParen(s, j)
          subs.push({ start: j + 2, end: k - 1 })
          for (let x = j; x < k; x++) push(s[x], x)
          j = k
          continue
        }
        if (d === '`') {
          const k = s.indexOf('`', j + 1)
          const stop = k < 0 || k > end ? end : k
          subs.push({ start: j + 1, end: stop })
          for (let x = j; x <= stop && x < end; x++) push(s[x], x)
          j = stop + 1
          continue
        }
        push(d, j)
        j++
      }
      i = end + 1
      continue
    }
    if (c === '$' && s[i + 1] === '(') {
      const k = skipDollarParen(s, i)
      subs.push({ start: i + 2, end: k - 1 })
      for (let x = i; x < k; x++) push(s[x], x)
      i = k
      continue
    }
    if (c === '`') {
      const k = s.indexOf('`', i + 1)
      const end = k < 0 ? s.length : k
      subs.push({ start: i + 1, end })
      for (let x = i; x <= end && x < s.length; x++) push(s[x], x)
      i = end + 1
      continue
    }
    if (c === '$' && s[i + 1] === '{') {
      const k = s.indexOf('}', i + 2)
      const end = k < 0 ? s.length : k + 1
      for (let x = i; x < end; x++) push(s[x], x)
      i = end
      continue
    }
    push(c, i)
    i++
  }
  return { kind: 'word', start, end: i, raw: s.slice(start, i), value, map, subs }
}

/** Split a command string into simple commands, with heredoc bodies attached to their owner. */
function parse(s: string): SimpleCommand[] {
  const commands: SimpleCommand[] = []
  let cur: SimpleCommand = { words: [], heredocs: [], redirects: [] }
  const pending: Array<{ cmd: SimpleCommand; delim: string; stripTabs: boolean }> = []
  const flush = () => {
    if (cur.words.length || cur.heredocs.length || cur.redirects.length) commands.push(cur)
    cur = { words: [], heredocs: [], redirects: [] }
  }
  let i = 0
  while (i < s.length) {
    const c = s[i]
    if (isBlank(c)) { i++; continue }
    if (c === '#' && (i === 0 || isBlank(s[i - 1]) || s[i - 1] === '\n' || s[i - 1] === ';')) {
      const k = s.indexOf('\n', i)
      i = k < 0 ? s.length : k
      continue
    }
    if (c === '\n') {
      flush()
      i++
      // Heredoc bodies start on the line after their `<<WORD`.
      while (pending.length) {
        const h = pending.shift()!
        const bodyStart = i
        let j = i
        let bodyEnd = s.length
        let next = s.length
        while (j <= s.length) {
          const nl = s.indexOf('\n', j)
          const lineEnd = nl < 0 ? s.length : nl
          let line = s.slice(j, lineEnd)
          if (h.stripTabs) line = line.replace(/^\t+/, '')
          if (line === h.delim) { bodyEnd = j; next = nl < 0 ? s.length : nl + 1; break }
          if (nl < 0) { bodyEnd = s.length; next = s.length; break }
          j = nl + 1
        }
        h.cmd.heredocs.push({ delim: h.delim, stripTabs: h.stripTabs, bodyStart, bodyEnd })
        i = next
      }
      continue
    }
    // Redirections, with an optional fd number in front.
    const redir = /^(\d*)(<<<|<<-|<<|&>>|&>|>>|>&|<&|>\||<>|>|<)/.exec(s.slice(i, i + 6))
    if (redir && (redir[1] === '' || i === 0 || isBlank(s[i - 1]) || isMeta(s[i - 1]))) {
      const op = redir[2]
      i += redir[0].length
      while (i < s.length && isBlank(s[i])) i++
      if (op === '>&' || op === '<&') {
        // `2>&1`: the fd is part of the operator.
        const m = /^[\d-]+/.exec(s.slice(i))
        if (m) { i += m[0].length; continue }
      }
      if (i >= s.length || s[i] === '\n') continue
      const target = readWord(s, i)
      i = target.end
      if (op === '<<' || op === '<<-') {
        pending.push({ cmd: cur, delim: target.value, stripTabs: op === '<<-' })
      } else {
        cur.redirects.push(target)
      }
      continue
    }
    const two = s.slice(i, i + 2)
    if (two === '&&' || two === '||' || two === '|&' || two === ';;') { flush(); i += 2; continue }
    if (c === ';' || c === '&' || c === '|' || c === '(' || c === ')') { flush(); i++; continue }
    const w = readWord(s, i)
    if (w.end === i) { i++; continue }
    cur.words.push(w)
    i = w.end
  }
  flush()
  return commands
}

// ── Analysis ───────────────────────────────────────────────────────────

const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh'])
const RM_WORDS = new Set(['rm', 'grm'])
const DECLARE = new Set(['export', 'local', 'declare', 'readonly', 'typeset'])
const SKIP_WORDS = new Set(['then', 'do', 'else', 'elif', 'if', 'while', 'until', '!', '{', '}', 'time', 'nohup', 'command', 'builtin', 'exec', 'caffeinate'])
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
/** The value of a name the command rebinds at run time (a loop, `read`, `unset`). */
const REBOUND = '$REBOUND'
/** The target of an rm fed by xargs: whatever stdin holds. */
const XARGS_INPUT = '$XARGS_INPUT'

/** Names a command binds to a run-time value. */
function reboundNames(values: string[]): string[] {
  const head = base(values[0] ?? '')
  const args = values.slice(1)
  if (head === 'for' || head === 'select') return NAME_RE.test(args[0] ?? '') ? [args[0]] : []
  if (head === 'read' || head === 'unset' || head === 'mapfile' || head === 'readarray') return args.filter(a => NAME_RE.test(a))
  if (head === 'getopts') return NAME_RE.test(args[1] ?? '') ? [args[1]] : []
  if (head === 'printf') { const i = args.indexOf('-v'); return i >= 0 && NAME_RE.test(args[i + 1] ?? '') ? [args[i + 1]] : [] }
  return []
}
const ASSIGN_RE = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/s

function base(word: string): string {
  return word.replace(/^\\/, '').split('/').pop() ?? word
}

/** True when a command word sequence runs its payload on another machine. */
function remoteCommand(values: string[]): string | null {
  const head = base(values[0] ?? '')
  const rest = values.slice(1)
  if (head === 'ssh' || head === 'mosh' || head === 'autossh') return head
  if (head === 'vmctl' && rest.some(v => v === 'exec' || v === 'ssh' || v === 'run')) return 'vmctl exec'
  if ((head === 'docker' || head === 'podman') && rest.some(v => v === 'exec' || v === 'run')) return `${head} exec`
  if ((head === 'kubectl' || head === 'oc') && rest.includes('exec')) return `${head} exec`
  if (head === 'gcloud' && rest.includes('ssh')) return 'gcloud ssh'
  if (head === 'fly' && rest.includes('ssh')) return 'fly ssh'
  return null
}

/** Strip wrappers (`sudo`, `env A=b`, `timeout 60`, `xargs -0`, `nohup`…) from the front. */
function stripWrappers(words: Word[], vars: Map<string, string>, opts: RmGuardOptions, seen?: { sudo: boolean; xargs?: boolean }): Word[] {
  let w = words
  for (let guard = 0; guard < 20 && w.length; guard++) {
    const head = base(w[0].value)
    const m = ASSIGN_RE.exec(w[0].value)
    if (m) {
      // `A=b cmd` sets A for cmd's environment only, after the shell has
      // expanded cmd's own words. Only a bare assignment changes later words.
      if (w.slice(1).every(x => ASSIGN_RE.test(x.value))) vars.set(m[1], expand(m[2], vars, opts))
      w = w.slice(1)
      continue
    }
    if (SKIP_WORDS.has(head)) { w = w.slice(1); continue }
    if (head === 'sudo' || head === 'doas') {
      if (seen) seen.sudo = true
      w = w.slice(1)
      while (w.length && w[0].value.startsWith('-')) { const f = w[0].value; w = w.slice(1); if (/^-[ugCDhpRrTt]$/.test(f)) w = w.slice(1) }
      continue
    }
    if (head === 'env') {
      w = w.slice(1)
      while (w.length && (w[0].value.startsWith('-') || ASSIGN_RE.test(w[0].value))) { const f = w[0].value; w = w.slice(1); if (f === '-u' || f === '-C' || f === '-S') w = w.slice(1) }
      continue
    }
    if (head === 'timeout' || head === 'gtimeout') {
      w = w.slice(1)
      while (w.length && w[0].value.startsWith('-')) { const f = w[0].value; w = w.slice(1); if (f === '-s' || f === '-k' || f === '--signal' || f === '--kill-after') w = w.slice(1) }
      w = w.slice(1)  // the duration
      continue
    }
    if (head === 'nice') {
      w = w.slice(1)
      if (w.length && w[0].value === '-n') w = w.slice(2)
      else if (w.length && /^-\d+$/.test(w[0].value)) w = w.slice(1)
      continue
    }
    if (head === 'xargs') {
      if (seen) seen.xargs = true
      w = w.slice(1)
      while (w.length && w[0].value.startsWith('-')) { const f = w[0].value; w = w.slice(1); if (/^-[IiLlnPdsEa]$/.test(f)) w = w.slice(1) }
      continue
    }
    if (head === 'stdbuf') { w = w.slice(1); while (w.length && w[0].value.startsWith('-')) w = w.slice(1); continue }
    break
  }
  return w
}

/**
 * Expand `$VAR`, `${VAR}`, a leading `~`, `$HOME`, `$TMPDIR`, and `$PWD` from
 * what the command itself assigned. An unknown variable stays as `$NAME`, so
 * the target stays unresolved (the helper re-checks it at run time).
 */
export function expand(value: string, vars: Map<string, string>, opts: RmGuardOptions): string {
  let v = value
  if (v === '~' || v.startsWith('~/')) v = opts.home + v.slice(1)
  // `$(mktemp)` and `$(mktemp -d)` land in the temp dir. A template or `-p`
  // with a path (`mktemp -d /etc/x.XXXX`) does not, so it stays unresolved.
  v = v.replace(/\$\(\s*mktemp\b([^)]*)\)/g, (whole, args: string) => (/[/$`~]/.test(args) ? whole : '/tmp/<mktemp>'))
  return v.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g, (whole, a, b) => {
    const name = a ?? b
    if (vars.has(name)) return vars.get(name) === REBOUND ? whole : vars.get(name)!
    if (name === 'HOME') return opts.home
    if (name === 'TMPDIR') return '/tmp'
    if (name === 'PWD' && opts.workdir) return opts.workdir
    return whole
  })
}

// The directory a preceding `cd` in the same command moved to. A name no shell
// variable can have, so expand() never reads it and assignedVars() drops it.
const CWD_KEY = ' cwd'
const CWD_UNKNOWN = '$CWD_UNKNOWN'

/** A path as the shell sees it after any `cd` earlier in the command. An
 * unknown `cd` (`cd -`, `cd "$x"`) leaves relative targets unresolved, and the
 * helper re-checks them at run time. */
function joinCwd(path: string, vars: Map<string, string>): string {
  if (isAbsPath(path) || path.startsWith('$') || path.startsWith('~')) return path
  const cwd = vars.get(CWD_KEY)
  if (cwd === undefined) return path
  if (cwd.startsWith('$')) return `${CWD_UNKNOWN}/${path}`
  return posix.join(cwd, path)
}

function parseRmArgs(args: string[]): { recursive: boolean; force: boolean; targets: string[] } {
  let recursive = false
  let force = false
  const targets: string[] = []
  let flagsDone = false
  for (const a of args) {
    if (!flagsDone && a === '--') { flagsDone = true; continue }
    if (!flagsDone && a.startsWith('--')) {
      if (a === '--recursive') recursive = true
      if (a === '--force') force = true
      continue
    }
    if (!flagsDone && /^-[A-Za-z]+$/.test(a)) {
      if (/[rR]/.test(a)) recursive = true
      if (/f/.test(a)) force = true
      continue
    }
    targets.push(a)
  }
  return { recursive, force, targets }
}

function analyzeInto(s: string, offset: number, vars: Map<string, string>, opts: RmGuardOptions,
                     out: RmAnalysis, depth: number, mapRaw: ((i: number) => number) | null) {
  if (depth > 6) return
  const toRaw = (i: number) => (mapRaw ? mapRaw(i) : i + offset)
  for (const cmd of parse(s)) {
    // Command substitutions run locally wherever they sit (arguments, assignments, redirect targets).
    for (const w of [...cmd.words, ...cmd.redirects]) {
      for (const sub of w.subs) {
        analyzeInto(s.slice(sub.start, sub.end), 0, vars, opts, out, depth + 1, i => toRaw(sub.start + i))
      }
    }
    // The shell opens a redirect in the folder it is in before the command runs.
    if (out.redirects) for (const r of cmd.redirects) out.redirects.push({ value: r.value, cwd: vars.get(CWD_KEY) })
    // `export A=b`, `local A=b`: assignments only.
    if (cmd.words.length && DECLARE.has(cmd.words[0].value)) {
      for (const w of cmd.words.slice(1)) {
        const m = ASSIGN_RE.exec(w.value)
        if (m) vars.set(m[1], expand(m[2], vars, opts))
        else if (NAME_RE.test(w.value)) vars.set(w.value, REBOUND)  // `local T`: T is now empty
      }
      out.dataHeredocs += cmd.heredocs.length
      continue
    }
    const seen = { sudo: false, xargs: false }
    const words = stripWrappers(cmd.words, vars, opts, seen)
    if (!words.length) { out.dataHeredocs += cmd.heredocs.length; continue }
    const before = out.invocations.length
    const values = words.map(w => w.value)
    // `for T in …`, `read T`, `getopts o T`, `printf -v T`, `unset T` bind T to
    // a value this analysis cannot know, so T stops resolving from here on.
    for (const name of reboundNames(values)) vars.set(name, REBOUND)
    const remote = remoteCommand(values)
    if (remote) {
      // The payload (arguments, `bash -lc "…"`, heredoc bodies) runs on the remote box.
      out.remote.push(remote)
      continue
    }
    const head = base(values[0])
    if (head === 'cd' || head === 'pushd') {
      // `cd <worktree> && rm -rf .amp/in/<task>/x`
      // must resolve against that worktree, not the tool's starting directory.
      const dir = values.slice(1).find(v => !/^-[LPe@]*$/.test(v))
      // A `cd` after a `(` may sit in a subshell that ends before the next
      // delete, so the folder is unknown from here on and the helper checks it.
      const maybeSubshell = s.slice(0, words[0].start).includes('(')
      if (!dir || dir === '-' || dir.startsWith('+') || maybeSubshell) vars.set(CWD_KEY, CWD_UNKNOWN)
      else vars.set(CWD_KEY, joinCwd(expand(dir, vars, opts), vars))
      out.dataHeredocs += cmd.heredocs.length
      continue
    }
    if (SHELLS.has(head)) {
      // `-c` counts only among the shell's own options. After the script
      // operand (`bash router-dispatch.sh --steer rt-1 '…'`) every word is an
      // argument to that script, so quoted prose there is data.
      let cIndex = -1
      for (let k = 1; k < values.length && /^[-+]/.test(values[k]) && values[k] !== '--'; k++) {
        if (/^-[A-Za-z]*c[A-Za-z]*$/.test(values[k])) { cIndex = k; break }
        if (values[k] === '-o' || values[k] === '+o' || values[k] === '-O' || values[k] === '+O') k++
      }
      if (cIndex > 0 && words[cIndex + 1]) {
        const script = words[cIndex + 1]
        analyzeInto(script.value, 0, vars, opts, out, depth + 1, i => script.map[i] !== undefined ? toRaw(script.map[i]) : -1)
      }
      // A heredoc fed to a local shell is a script that runs here.
      for (const h of cmd.heredocs) {
        analyzeInto(s.slice(h.bodyStart, h.bodyEnd), 0, vars, opts, out, depth + 1, i => toRaw(h.bodyStart + i))
      }
      continue
    }
    if (head === 'eval') {
      const parts = words.slice(1)
      if (parts.length === 1) {
        const script = parts[0]
        analyzeInto(script.value, 0, vars, opts, out, depth + 1, i => script.map[i] !== undefined ? toRaw(script.map[i]) : -1)
      } else if (parts.length > 1) {
        const before = out.invocations.length
        analyzeInto(parts.map(p => p.value).join(' '), 0, vars, opts, out, depth + 1, () => -1)
        for (const inv of out.invocations.slice(before)) inv.rewritable = false
      }
      out.dataHeredocs += cmd.heredocs.length
      continue
    }
    // Every other command: heredoc bodies and quoted arguments are data.
    out.dataHeredocs += cmd.heredocs.length
    // `tee [-a] file…` writes its arguments in the current folder, like a redirect.
    if (out.redirects && head === 'tee') {
      for (const v of values.slice(1)) if (!v.startsWith('-')) out.redirects.push({ value: v, cwd: vars.get(CWD_KEY) })
    }
    if (RM_WORDS.has(head)) {
      const args = values.slice(1)
      const { recursive, force, targets } = parseRmArgs(args)
      let resolved = targets.map(t => joinCwd(expand(t, vars, opts), vars))
      if (seen.xargs) {
        // `… | xargs rm -rf` and `xargs -I{} rm -rf {}`: the targets come from
        // stdin, so they stay unresolved and the helper checks them at run time.
        resolved = resolved.filter(t => !t.includes('{}'))
        resolved.push(XARGS_INPUT)
      }
      pushInvocation(out, words[0], toRaw, { args, recursive, force, targets: resolved, via: 'rm' })
      // A Trash move as root lands in root's Trash: a sudo delete keeps the prompt.
      if (seen.sudo) for (const inv of out.invocations.slice(before)) inv.rewritable = false
      continue
    }
    if (head === 'find') {
      const execAt = values.findIndex((v, k) => k > 0 && (v === '-exec' || v === '-execdir' || v === '-ok') && RM_WORDS.has(base(values[k + 1] ?? '')))
      if (execAt > 0) {
        const rmArgs: string[] = []
        for (const v of values.slice(execAt + 2)) { if (v === ';' || v === '+' || v === '\\;') break; rmArgs.push(v) }
        const { recursive, force } = parseRmArgs(rmArgs)
        // Roots follow find's own options (`find -x ~/a`, `find -E -L ~/a`);
        // BSD `-f path` names a root too.
        const roots: string[] = []
        let k = 1
        while (k < values.length && /^-[HLPEXxds]+$/.test(values[k])) k++
        for (; k < values.length; k++) {
          const v = values[k]
          if (v === '-f') { if (k + 1 < values.length) roots.push(values[++k]); continue }
          if (v.startsWith('-') || v === '(' || v === '!') break
          roots.push(v)
        }
        pushInvocation(out, words[execAt + 1], toRaw, {
          args: rmArgs, recursive, force, targets: (roots.length ? roots : ['.']).map(t => joinCwd(expand(t, vars, opts), vars)), via: 'find-exec',
        })
        if (seen.sudo) for (const inv of out.invocations.slice(before)) inv.rewritable = false
      }
    }
  }
}

function pushInvocation(out: RmAnalysis, word: Word, toRaw: (i: number) => number,
                        fields: Pick<RmInvocation, 'args' | 'recursive' | 'force' | 'targets' | 'via'>) {
  // Map the command word back to raw offsets in the top-level command. The word
  // may carry quotes or a backslash (`\rm`), so map its first and last value chars.
  const first = word.map[0]
  const last = word.map[word.map.length - 1]
  const startRaw = first === undefined ? -1 : toRaw(first)
  const endRaw = last === undefined ? -1 : toRaw(last)
  // A nested word (inside `bash -c '…'`) maps value chars, not the outer quotes.
  const wordRaw = word.raw
  const plain = /^\\?[A-Za-z0-9_./-]+$/.test(wordRaw)
  out.invocations.push({
    ...fields,
    word: word.value,
    wordStart: startRaw < 0 ? -1 : startRaw - (wordRaw.startsWith('\\') ? 1 : 0),
    wordEnd: endRaw < 0 ? -1 : endRaw + 1,
    rewritable: plain && startRaw >= 0 && endRaw >= 0,
  })
}

/** A vars map that remembers every value each name took, so a caller can
 * refuse to resolve a name the command reassigned. */
class RecordingVars extends Map<string, string> {
  readonly seen = new Map<string, Set<string>>()
  override set(key: string, value: string): this {
    if (this.seen) {
      if (!this.seen.has(key)) this.seen.set(key, new Set())
      this.seen.get(key)!.add(value)
    }
    return super.set(key, value)
  }
}

/** A vars map that never resolves the given names. */
class StableVars extends Map<string, string> {
  constructor(private readonly unstable: Set<string>) { super() }
  override has(key: string): boolean { return !this.unstable.has(key) && super.has(key) }
  override get(key: string): string | undefined { return this.has(key) ? super.get(key) : undefined }
}

function recordVars(cmd: string, opts: RmGuardOptions): RecordingVars {
  const vars = new RecordingVars()
  analyzeInto(cmd, 0, vars, opts, { invocations: [], remote: [], dataHeredocs: 0 }, 0, null)
  return vars
}

/**
 * Every rm invocation in command position, local only, with targets expanded.
 * A name the command binds to more than one value (reassigned, a loop
 * variable, a function body run after a reassignment) never resolves: its
 * target goes to the helper, which re-checks the real path at run time.
 */
export function analyzeRm(cmd: string, opts: RmGuardOptions): RmAnalysis {
  const unstable = new Set<string>()
  for (const [name, values] of recordVars(cmd, opts).seen) if (values.size > 1 && name !== CWD_KEY) unstable.add(name)
  const out: RmAnalysis = { invocations: [], remote: [], dataHeredocs: 0 }
  analyzeInto(cmd, 0, new StableVars(unstable), opts, out, 0, null)
  return out
}

/**
 * The variables a command assigns to itself (`sd=/private/tmp/…; … > $sd/q.json`),
 * walked the same way as the delete analysis. A name assigned two different
 * values, rebound at run time, or used before its assignment is left out, so
 * it stays unresolved. The CoS delivery gate uses this to read a redirect
 * target the way the shell will.
 */
export function assignedVars(cmd: string, opts: RmGuardOptions): Map<string, string> {
  const stable = new Map<string, string>()
  for (const [name, values] of recordVars(cmd, opts).seen) {
    if (values.size !== 1 || name === CWD_KEY) continue
    const value = [...values][0]
    if (value.includes(REBOUND)) continue
    const assignAt = new RegExp(`(^|[\\s;&|(])${name}=`).exec(cmd)
    const useAt = new RegExp(`\\$\\{?${name}(?![A-Za-z0-9_])`).exec(cmd)
    if (!assignAt || (useAt && useAt.index < assignAt.index)) continue
    stable.set(name, value)
  }
  return stable
}

/**
 * The folder each redirect target opens in, keyed by the target as written
 * (quotes removed, no expansion). `cd <dir> && … > out.json` writes
 * `<dir>/out.json`, not a file in the tool's starting folder. The value set
 * holds every folder the same target name opened in: `''` for no `cd` before
 * it, `$CWD_UNKNOWN` for a `cd` this analysis cannot read (`cd -`, `cd "$x"`,
 * a subshell). The CoS delivery gate uses it.
 */
export function redirectCwds(cmd: string, opts: RmGuardOptions): Map<string, Set<string>> {
  const unstable = new Set<string>()
  for (const [name, values] of recordVars(cmd, opts).seen) if (values.size > 1 && name !== CWD_KEY) unstable.add(name)
  const out: RmAnalysis = { invocations: [], remote: [], dataHeredocs: 0, redirects: [] }
  analyzeInto(cmd, 0, new StableVars(unstable), opts, out, 0, null)
  const byTarget = new Map<string, Set<string>>()
  for (const r of out.redirects!) {
    if (!byTarget.has(r.value)) byTarget.set(r.value, new Set())
    byTarget.get(r.value)!.add(r.cwd ?? '')
  }
  return byTarget
}

// ── Target classification ──────────────────────────────────────────────

const TEMP_PREFIXES = ['/tmp', '/private/tmp', '/var/folders', '/private/var/folders']
const SYSTEM_ROOTS = ['/', '/Users', '/System', '/Library', '/Applications', '/usr', '/bin', '/sbin', '/etc',
  '/var', '/private', '/private/var', '/private/etc', '/opt', '/opt/homebrew', '/Volumes', '/cores', '/dev']
const HOME_ROOTS = ['', 'Development', 'Documents', 'Desktop', 'Downloads', 'Library', 'Pictures', 'Movies',
  'Music', 'Applications', 'Public', '.ssh', '.config', '.claude', '.agents', '.Trash', '.local', '.gnupg']
// Windows: synced roots (a delete there reaches every device) and AppData. A synced
// root often holds the usual home folders (`Dropbox/Documents`, `OneDrive/Desktop`),
// so each home folder name under a synced root is protected the same way.
const WINDOWS_SYNCED_ROOTS = ['Dropbox', 'OneDrive']
const WINDOWS_HOME_ROOTS = [
  ...WINDOWS_SYNCED_ROOTS,
  ...WINDOWS_SYNCED_ROOTS.flatMap(s => HOME_ROOTS.filter(r => r && !r.startsWith('.')).map(r => `${s}/${r}`)),
  'AppData', 'AppData/Local', 'AppData/Roaming',
]
/** Extra home-relative folders to hard-block on every host: SWITCHBOARD_RM_GUARD_EXTRA_ROOTS, comma-separated
 * (`Sync/Projects,Archive`). Empty by default. Leading and trailing slashes are ignored. */
export function extraHomeRoots(env: Record<string, string | undefined> = process.env): string[] {
  return (env.SWITCHBOARD_RM_GUARD_EXTRA_ROOTS ?? '').split(',')
    .map(r => r.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, ''))
    .filter(r => r && !r.split('/').includes('..'))
}

function under(path: string, root: string): boolean {
  return path === root || path.startsWith(root.endsWith('/') ? root : `${root}/`)
}

/** Where a delete target lands: run as is (temp, switchboard, build), hard block, or Trash. */
export function classifyTarget(target: string, opts: RmGuardOptions): TargetVerdict {
  const verdict = (resolved: string, cls: TargetClass, reason: string): TargetVerdict => ({ target, resolved, cls, reason })
  if (!target) return verdict(target, 'trash', 'empty target')
  // A glob is judged by the fixed prefix before its first wildcard. A brace
  // list or a `..` after a wildcard can leave that prefix, so those go to the
  // helper, which sees the shell's expanded paths.
  const globAt = target.search(/[*?[{]/)
  if (globAt >= 0 && target[globAt] === '{' && /\{[^}]*(,|\.\.)[^}]*\}/.test(target.slice(globAt))) {
    return verdict(target, 'trash', 'brace expansion: switchboard-trash.py checks each expanded path at run time')
  }
  if (globAt >= 0 && /(^|\/)\.\.(\/|$)/.test(target.slice(globAt))) {
    return verdict(target, 'trash', 'a .. after a wildcard: switchboard-trash.py checks the real path at run time')
  }
  let fixed = globAt >= 0 ? target.slice(0, globAt) : target
  const globbed = globAt >= 0
  if (fixed.includes('$')) return verdict(target, 'trash', 'unresolved variable: switchboard-trash.py re-checks the real path at run time')
  if (fixed.startsWith('~')) fixed = opts.home + fixed.slice(1)
  fixed = slashPath(fixed)
  // Windows: Git Bash's `/tmp` is the user's temp folder. Spell it that way so
  // the symlink check below can see a junction under it (`/tmp/x/link/proj`
  // with `link` → ~/Documents must not be a plain rm).
  if (isWindowsHost() && opts.temp?.length && /^\/tmp(\/|$)/.test(fixed)) {
    fixed = slashPath(opts.temp[0]).replace(/\/+$/, '') + fixed.slice(4)
  }
  if (!isAbsPath(fixed)) {
    if (!opts.workdir) return verdict(target, 'trash', 'relative path with no known working directory')
    fixed = posix.join(slashPath(opts.workdir), fixed || '.') + (fixed === '' || fixed.endsWith('/') ? '/' : '')
  }
  let resolved = posix.normalize(fixed)
  if (globbed && !fixed.endsWith('/')) resolved = posix.dirname(resolved) + '/'
  if (resolved.length > 1) resolved = resolved.replace(/\/+$/, '')
  if (!resolved) resolved = '/'
  const lexical = classifyResolved(target, resolved, globbed, opts)
  // A temp, repo, or build path reached through a symlink (`/tmp/x` → `~/Documents`)
  // is judged by where it really lands.
  if (opts.realpath && (lexical.cls === 'temp' || lexical.cls === 'switchboard' || lexical.cls === 'build')) {
    const real = slashPath(opts.realpath(resolved, globbed || target.endsWith('/')))
    if (real && real !== resolved) {
      const v = classifyResolved(target, real, globbed, opts)
      if (v.cls === 'blocked' || v.cls === 'trash') return { ...v, reason: `${v.reason} (through a symlink)` }
    }
  }
  return lexical
}

function classifyResolved(target: string, resolved: string, globbed: boolean, opts: RmGuardOptions): TargetVerdict {
  const verdict = (r: string, cls: TargetClass, reason: string): TargetVerdict => ({ target, resolved: r, cls, reason })
  const shown = resolved
  // NTFS is case-insensitive: compare Windows paths in one case.
  const win = isWindowsHost()
  const key = (p: string) => { const s = slashPath(p).replace(/\/+$/, ''); return win ? s.toLowerCase() : s }
  resolved = win ? resolved.toLowerCase() : resolved
  const repo = key(opts.repo)
  // `/tmp` itself stays allowed as before; the `/var/folders` roots are system folders.
  // A Windows temp root (`C:/Users/x/AppData/Local/Temp`) counts like `/tmp`.
  const temp = (opts.temp ?? []).map(key).filter(Boolean)
  if (resolved === '/tmp' || resolved === '/private/tmp' ||
      TEMP_PREFIXES.some(p => resolved.startsWith(`${p}/`)) ||
      temp.some(t => resolved === t || resolved.startsWith(`${t}/`))) {
    return verdict(shown, 'temp', 'temp path')
  }
  if (globbed && resolved === repo) return verdict(shown, 'blocked', 'a glob over the switchboard repo root')
  if (resolved === repo) return verdict(shown, 'blocked', 'the switchboard repo root')
  // APFS is case-insensitive: `.GIT` is the same folder.
  if (/(^|\/)\.git(\/|$)/i.test(resolved)) return verdict(shown, 'blocked', 'a .git directory')
  if (SYSTEM_ROOTS.includes(resolved) || TEMP_PREFIXES.includes(resolved)) return verdict(shown, 'blocked', 'a top-level system folder')
  if (win && /^[a-z]:(\/(windows|users|program files|program files \(x86\)|programdata))?$/.test(resolved)) {
    return verdict(shown, 'blocked', 'a top-level system folder')
  }
  const home = key(opts.home)
  const homeRoots = [...HOME_ROOTS, ...(win ? WINDOWS_HOME_ROOTS : []), ...extraHomeRoots()]
  if (homeRoots.some(r => resolved === (r ? `${home}/${win ? r.toLowerCase() : r}` : home))) return verdict(shown, 'blocked', 'a top-level home folder')
  if (under(resolved, repo)) return verdict(shown, 'switchboard', 'inside the switchboard repo (pre-approved)')
  if (/(^|\/)node_modules(\/|$)/.test(resolved) || /\/(dist|build)$/.test(resolved)) return verdict(shown, 'build', 'build output')
  return verdict(shown, 'trash', 'outside switchboard and temp paths')
}

export interface RmRfPlan {
  /** Recursive-and-force invocations with at least one target bound for the Trash. */
  trash: Array<{ invocation: RmInvocation; targets: TargetVerdict[] }>
  /** Recursive-and-force invocations whose targets sit inside the switchboard repo and none
   * outside it. The helper decides each one: a path git tracks unchanged is deleted,
   * anything git cannot restore (gitignored `local/`, untracked files) goes to the Trash.
   * This needs file I/O, so it runs in the
   * helper, not here. When the command cannot be rewritten it still runs as written. */
  repo: Array<{ invocation: RmInvocation; targets: TargetVerdict[] }>
  blocked: TargetVerdict[]
  analysis: RmAnalysis
}

export function planRmRf(cmd: string, opts: RmGuardOptions): RmRfPlan {
  const analysis = analyzeRm(cmd, opts)
  const plan: RmRfPlan = { trash: [], repo: [], blocked: [], analysis }
  for (const inv of analysis.invocations) {
    if (!(inv.recursive && inv.force)) continue
    const verdicts = inv.targets.map(t => classifyTarget(t, opts))
    plan.blocked.push(...verdicts.filter(v => v.cls === 'blocked'))
    const trash = verdicts.filter(v => v.cls === 'trash')
    const repo = verdicts.filter(v => v.cls === 'switchboard')
    if (trash.length) plan.trash.push({ invocation: inv, targets: trash })
    else if (repo.length) plan.repo.push({ invocation: inv, targets: repo })
  }
  return plan
}

/**
 * Replace each invocation's command word with the trash helper. The arguments
 * stay byte for byte, so quoting and expansions behave as before; the helper
 * takes rm's flags. Returns null when any invocation cannot be rewritten.
 */
/** The helper as one shell word: bare when plain; a POSIX path whose only extra
 *  characters are spaces (an install folder such as "~/My Drive/switchboard") gets each space
 *  backslash-escaped, which reads the same at top level and inside `bash -c '…'`, "…",
 *  or $'…'; null means no rewrite (quotes, `$`, control characters, a UNC path). */
export function helperWord(helper: string): string | null {
  // A Windows helper path (`C:\Users\Pat/Development/switchboard/...`, as Bun builds it from
  // HOME) is the Git Bash path `/c/Users/Pat/...`, which the bash tool can run. A null
  // here means no rewrite, and the confirm prompt would end in the original `rm -rf`:
  // a hard delete on Windows.
  const drive = /^([A-Za-z]):[\\/](.*)$/.exec(helper)
  if (drive) helper = `/${drive[1].toLowerCase()}/${drive[2].replace(/\\/g, '/')}`
  if (/^[A-Za-z0-9_./-]+$/.test(helper)) return helper
  if (/^\/[A-Za-z0-9_./ -]+$/.test(helper)) return helper.replace(/ /g, '\\ ')
  return null
}

export function rewriteToTrash(cmd: string, invocations: RmInvocation[], helper: string): string | null {
  if (invocations.some(inv => !inv.rewritable || inv.wordStart < 0 || inv.wordEnd <= inv.wordStart)) return null
  let out = cmd
  for (const inv of [...invocations].sort((a, b) => b.wordStart - a.wordStart)) {
    out = out.slice(0, inv.wordStart) + helper + out.slice(inv.wordEnd)
  }
  return out
}

/** True on a Windows host. Test seam: SWITCHBOARD_RM_GUARD_PLATFORM names the platform. */
export function isWindowsHost(): boolean {
  return (process.env.SWITCHBOARD_RM_GUARD_PLATFORM ?? process.platform) === 'win32'
}
