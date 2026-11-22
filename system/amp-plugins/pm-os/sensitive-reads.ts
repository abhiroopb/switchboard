// The environment read guard. Whatever an agent reads lands in its transcript,
// and wmux puts its pipe token in every shell, so a plain `env` writes it down.
// The Downloads read block that sat beside it is retired: agents may read and search files there, text and binary.
// Mirrored for Claude Code in .claude/hooks/pm-os-guardrails.py; keep them in step.

const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'pwsh', 'powershell', 'cmd'])
const INTERPRETER = /(?:^|[\s&;|("'\\/])(?:python[\dw.]*|py|node|bun|deno|ruby|perl|pwsh|powershell)(?:\.exe)?["']?(?=\s)[^\n]*(?:\s-c\b|\s-e\b|\s-command\b|<<)|\[(?:system\.)?io\.file\]::read/i
const HEREDOC = /<<-?\s*["']?(\w+)["']?[^\n]*\n[\s\S]*?\n\s*\1[ \t]*(?=\n|$)/g

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

// Split where a separator sits outside quotes, so a commit message holding `; env;` stays one statement.
function splitUnquoted(text: string, separators: string[] = ['&&', '||', ';', '\n']): string[] {
  const parts: string[] = []
  let buffer = ''
  let quote = ''
  for (let i = 0; i < text.length;) {
    const ch = text[i]
    if (quote) {
      if (ch === quote) quote = ''
      buffer += ch
      i += 1
    } else if (ch === '"' || ch === "'") {
      quote = ch
      buffer += ch
      i += 1
    } else {
      const sep = separators.find(s => text.startsWith(s, i))
      if (sep) {
        parts.push(buffer)
        buffer = ''
        i += sep.length
      } else {
        buffer += ch
        i += 1
      }
    }
  }
  parts.push(buffer)
  return parts.map(p => p.trim()).filter(Boolean)
}

// What a shell was told to run, past any flags in front of -c. Null when it runs a script file.
function shellPayload(rest: string[]): string | null {
  for (let i = 0; i < rest.length; i += 1) {
    if (/^(?:-\w*c|-c(?:ommand)?|\/c)$/i.test(rest[i])) return rest.slice(i + 1).join(' ')
    if (!rest[i].startsWith('-') && !rest[i].startsWith('/')) return null
  }
  return null
}

const commandWord = (token: string) => (token.split(/[\\/]/).pop() ?? '').toLowerCase().replace(/\.exe$/, '')

const SECRET_NAME = String.raw`\w*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)\w*`
const ENV_HINT = /\benv\b|printenv|\bexport\b|\bdeclare\b|\btypeset\b|\bset\b|environ|environment|process\.env|getenv|pipe-token|wmux|token|secret|passw|api_?key|credential/i
const ENV_REDIRECT = /\s*\d?>>?\s*\S+/g
const ENV_DUMP = /^(?:(?:\/usr\/bin\/)?env(?:\.exe)?(?:\s+(?:-0|--null))?|printenv(?:\.exe)?|export(?:\s+-p)?|declare\s+-[xp]+|typeset\s+-x|set|(?:get-childitem|gci|ls|dir|get-item|gi)\s+(?:-path\s+|-literalpath\s+)?["']?env:[\\/]?\*?["']?|cmd(?:\.exe)?\s+\/c\s+set)$/i
// `env | grep -i proxy` shows only what it names. A pattern like wmux, token, or `.` isn't narrowing.
const ENV_NARROWING_GREP = /^(?:grep|egrep|rg|select-string|sls|findstr)(?:\s+-\w+)*\s+["']?[\w-]{3,}["']?\s*$/i
const ENV_SECRETISH = /wmux|token|secret|pass|key|auth|cred/i
const ENV_INLINE_DUMP = /\bprint\(\s*(?:dict\()?os\.environ\)?\s*\)|json\.dumps\(\s*dict\(os\.environ\)|os\.environ\.items\(\)|console\.log\(\s*process\.env\s*\)|JSON\.stringify\(\s*process\.env\b/i
const ENV_INLINE_PRINT = /\bprint\(|console\.log\(|\.write\(/i
const ENV_INLINE_SECRET = new RegExp(String.raw`os\.environ(?:\.get)?[\[(]\s*["']${SECRET_NAME}|os\.getenv\(\s*["']${SECRET_NAME}|process\.env\.${SECRET_NAME}`, 'i')
const ENV_PS_INLINE = new RegExp(String.raw`\[(?:System\.)?Environment\]::GetEnvironmentVariables\(\s*\)(?!\s*\.Keys)|\[(?:System\.)?Environment\]::GetEnvironmentVariable\(\s*["']${SECRET_NAME}`, 'i')
const ENV_NAMES_ONLY = /\|\s*cut\s+-d\s*['"]?=['"]?\s+-f\s*1\b|\|\s*sed\s+['"]?s\/=\.\*\/\/|\|\s*awk\s+-F\s*['"]?=['"]?\s+['"]\{\s*print\s+\$1\s*\}|\|\s*(?:select-object|select)\s+(?:-expandproperty\s+|-property\s+)?(?:name|key)\s*(?:\||$)|\|\s*(?:foreach-object|foreach|%)\s+(?:name|key|\{\s*\$_\.(?:name|key)\s*\})|\)\.(?:name|key)\b/i
// Bash names are upper case by convention, so `$token` in a loop isn't one. $MAX_TOKENS and $TOKENS are counts.
const BASH_SECRET_VAR = /\$\{?(?!MAX_)[A-Z0-9_]*(?:TOKEN(?!S)|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Z0-9_]*\}?/
const PS_SECRET_VAR = /\$env:(?!MAX_)\w*(?:TOKEN(?!S)|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)\w*/i
const SECRET_VAR = (t: string) => BASH_SECRET_VAR.test(t) || PS_SECRET_VAR.test(t)
const PRINTERS = new Set(['echo', 'printf', 'write-output', 'write-host'])
const ENV_READERS = new Set(['cat', 'type', 'more', 'less', 'head', 'tail', 'get-content', 'gc', 'strings', 'xxd', 'get-item', 'gi'])
const QUOTING = new Set(['grep', 'egrep', 'rg', 'less', 'more', 'sed', 'awk', 'git', 'gh', 'ls', 'dir', 'wc', 'diff', 'select-string', 'sls', 'code', 'vim', 'nano', 'stat', 'file', 'cp', 'mv'])
const WMUX_TOKEN_FILE = /wmux(?:-[\w-]+)?[\\/]+pipe-token$/i

export const ENV_FIX = "Read one named variable that isn't a secret, or list names only: `env | cut -d= -f1` or `(Get-ChildItem Env:).Name`."

/** Why this command would print secret values from the environment, or null. A names-only view is fine. */
export function envDumpViolation(command: string, depth = 0): string | null {
  if (!ENV_HINT.test(command)) return null
  const flat = command.replace(/[\\`]\r?\n\s*/g, ' ')
  if (INTERPRETER.test(flat) && (ENV_INLINE_DUMP.test(flat) || (ENV_INLINE_PRINT.test(flat) && ENV_INLINE_SECRET.test(flat)))) {
    return 'this prints a secret from the environment.'
  }
  const secret = 'this prints a secret from the environment.'
  const wmux = 'this prints the wmux pipe token.'
  for (const statement of splitUnquoted(flat.replace(HEREDOC, ''))) {
    if (ENV_NAMES_ONLY.test(statement)) continue
    const stages = splitUnquoted(statement, ['|'])
    const first = stages[0].replace(ENV_REDIRECT, '').trim().replace(/^\(+/, '').replace(/\)+$/, '').trim()
    let tokens = parseShell(first)[0] ?? []
    while (tokens.length && /^\w+=/.test(tokens[0])) tokens = tokens.slice(1)
    if (!tokens.length) continue
    const word = commandWord(tokens[0])
    const rest = tokens.slice(1)
    if (SHELLS.has(word) && depth < 3) {
      const payload = shellPayload(rest)
      if (payload !== null) {
        const problem = envDumpViolation(payload, depth + 1)
        if (problem) return problem
        continue
      }
    }
    if (ENV_DUMP.test(first)) {
      const narrowed = stages.length > 1 && ENV_NARROWING_GREP.test(stages[1]) && !ENV_SECRETISH.test(stages[1])
      if (!narrowed) return 'this prints every environment variable with its value.'
      continue
    }
    if ((word === 'wmux' || word === 'wmux.cmd') && rest[0] === 'token') return wmux
    if (ENV_READERS.has(word) && rest.some(t => WMUX_TOKEN_FILE.test(t))) return wmux
    if (QUOTING.has(word)) continue
    if (PRINTERS.has(word) && rest.some(SECRET_VAR)) return secret
    const secretName = new RegExp(`^${SECRET_NAME}$`, 'i')
    if (word === 'printenv' && rest.some(t => secretName.test(t) && !/MAX_|TOKENS/i.test(t))) return secret
    if (tokens.length === 1 && SECRET_VAR(tokens[0])) return secret
    if (ENV_READERS.has(word) && rest.some(t => new RegExp(`^env:${SECRET_NAME}$`, 'i').test(t))) return secret
    if (ENV_PS_INLINE.test(statement)) return secret
  }
  return null
}
