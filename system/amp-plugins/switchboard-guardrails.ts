// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// switchboard-guardrails — enforces deterministic policies from AGENTS.md.
//
// Rules enforced (all on tool.call unless noted):
//  1. Block `git add -A` / `git add --all`            (~/AGENTS.md Non-Negotiables)
//  2. Block `git push --force` / `--no-verify`        (~/AGENTS.md Side Effects)
//  3. `rm -rf` outside switchboard, temp, and build paths moves to the Trash
//     (the command word is rewritten to
//     system/scripts/switchboard-trash.py, no prompt); repo root, `.git`, and
//     top-level system or home folders are hard blocks. Shell-aware:
//     heredoc bodies and quoted text are data,
//     same-command assignments resolve, and remote payloads (ssh,
//     `vm … exec`) never count. See switchboard/rm-guard.ts.
//                                                      (~/AGENTS.md Side Effects)
//  4. Inject the bot attribution prefix on ANY bot-authored Slack message (DM,
//     channel, thread reply) in its same-line form, `:robot_face: <first
//     word…>`: emoji, one space, then the first sentence on the same line,
//     never alone on its own line. A standalone prefix line is folded onto the
//     first sentence. Bash-invoked Slack writes whose message text (including
//     any `$(cat /path)` substitutions) doesn't open that way are rejected: a
//     standalone prefix line and a missing prefix each get their own reason.
//     Comms Triage drafts carry the prefix too. Approved replies sent via
//     `python3 system/scripts/send-triage-draft.py` post from the owner's
//     account WITH the same-line prefix. The script guarantees it itself
//     (`with_bot_prefix`, test_send_triage_draft.py); this rule recognises the
//     script only so the guarded path is logged, not to waive the prefix. The
//     one opt-out there is a per-draft `no robot` (`triage_ledger.py no-robot`).
//     Per-send opt-out: when the owner says "no robot" / "remove the robot"
//     for a specific message, the agent writes the marker `SWITCHBOARD_NO_ROBOT=1`
//     on that one send and nothing else changes. Bash: the env assignment at
//     the start of the `switchboard slack …` command itself
//     (`SWITCHBOARD_NO_ROBOT=1 switchboard slack post …`). Tool level
//     (`mcp__slack__*`): the first line of `text` is exactly
//     `SWITCHBOARD_NO_ROBOT=1`, which this rule strips before the send. The marker
//     skips the prefix requirement and the injection for that send only, logs
//     `no-robot-optout` with the subcommand or tool and the channel, and leaves
//     rules 13, 14, 15 and 17 running on the same send. Never written without
//     the owner's opt-out for that message in the current conversation.
//     GitHub: GitHub has no robot_face shortcode, so `gh` writes that carry a
//     body (pr/issue comment, create, review, edit; `gh api` with a body field)
//     must open their first paragraph with the same emoji as a 20px inline
//     image of the user-attachment PNG (or the unicode 🤖 fallback), one space,
//     then the first word. The bare shortcode is rejected there because it
//     renders as literal text. Placement: the first paragraph is the first
//     line, or the paragraph directly under a leading markdown heading (PR
//     bodies open with "## Why"). An image alone on its own line is rejected
//     (a retired shape), and an image above a heading is rejected
//     because GitHub renders the robot inline on the heading line.
//                                   (switchboard AGENTS.md "Bot Attribution Prefix")
//  5. Require Gmail send preview in recent thread     (~/AGENTS.md Non-Negotiables)
//  6. Calendar: block sub-P1 holds, default 1pm in the configured zone
//     (SWITCHBOARD_TZ, default America/Los_Angeles)       (~/AGENTS.md Canonical Calendar Rule)
//  7. Workstream auto-archive on status flip          (switchboard/AGENTS.md Workstream Hygiene) — fires on tool.result
//  8. CoS delivery confirm-gate: in the CoS session, file writes outside
//     orchestration-state paths (edit_file/create_file, bash `>`/`>>`/`tee`/
//     heredocs, gdrive write subcommands) require an explicit confirm. A
//     heredoc body fed to a non-shell command (`python3 - <<'PY'`) is data,
//     never scanned for redirects or Drive verbs: prose such as `>160` or
//     `>=90%` inside a prompt must not open the confirm modal. Planning-only
//     enforcement lives in structure, not prose.
//  9. Browser automation: require the sanctioned persistent profile, block
//     focus-stealing launches, and block AppleScript control of daily Chrome.
// 10. Dispatch guard: in CoS / Launchpad / Router sessions, block
//     `send_thread_message` when the target thread is not live in the cmux
//     session store or is bound to a Router workspace. A message to a finished
//     thread is "accepted" with no ledger record, so CoS would report work as
//     started that never was. `create_thread` is NOT blocked: the native path
//     stays supported and task-liveness registers the child in the ledger from
//     the tool result, so the ledger still owns the lifecycle.
//     Gated by `switchboard-guardrails.dispatch-guard.disabled`. Test seams:
//     SWITCHBOARD_GUARDRAILS_SESSION_STORE (cmux store path) and the title override.
// 11. Orchestrator wait guard: in CoS / Launchpad / Router sessions,
//     `wait_for_threads` requires an explicit confirm that the owner asked CoS
//     to wait (declined/headless → reject), repeated `get_thread_status` /
//     `read_thread` on the same thread beyond 2 per 15 minutes is rejected as
//     supervisory polling, and a bash command that starts with `sleep N`
//     (N ≥ 30 s) is rejected as a blocking wait. A CoS that waits on its
//     workers in a loop locks the owner out of CoS. Dispatch-and-return is the
//     policy: results arrive via report-to-cos.sh / the digest /
//     running-tasks.json.
//     Gated by `switchboard-guardrails.wait-guard.disabled`; window and poll limit
//     via `switchboard-guardrails.wait-guard.poll-window-ms` / `.poll-limit`.
// 12. (retired) Slack DM approval hold: a confirm modal on every DM
//     target. Retired outright. The approval for a DM is the owner saying "send it" (or
//     equivalent) for that text in the current conversation; the policy lives
//     in system/prompts/external-replies.md Rule 1. Workers still park drafts
//     under "Draft for the owner to send" (prompt rule, dispatch.py launch_prompt),
//     and rules 4 and 13 still run on every Slack send, DMs included. No
//     config key remains.
// 13. Slack link and channel-mention format: a
//     Slack message links inline with anchor text that names the thing
//     (`<https://url|anchor>`, or `[anchor](url)` in a markdown block), never
//     a bare URL, and mentions channels as `<#C…|name>`, never plain `#name`,
//     which Slack renders as text in an API-posted message. A
//     bare URL always rejects, naming the URL: anchor text cannot be invented.
//     A plain `#name` is rewritten to `<#ID|name>` on the MCP tools when the
//     id is known (priority-channels.yaml, then the comms source files) and
//     rejected with the exact replacement on bash `switchboard slack`
//     writes, which cannot be safely rewritten; an unknown name rejects.
//     `send-triage-draft.py` applies the same check itself through
//     system/scripts/slack_format.py, after adding the bot prefix (rule 4).
//     Gated by `switchboard-guardrails.slack-link-format.disabled`.
// 14. Slack native bullets: every text bullet becomes a native Slack
//     rich_text list with the words unchanged. Any list in a Slack message switchboard posts is a `rich_text_list` block
//     (style bullet, or ordered when the source is numbered), never a `•`,
//     `◦`, `▪`, `-`, `*`, `1.` or `1)` text bullet inside mrkdwn text. The
//     guardrail REJECTS (never rewrites): a quoted shell argument cannot be
//     restructured into blocks safely, and the tool-level `text` would need a
//     `blocks` payload the caller must own. switchboard send scripts
//     (send-triage-draft.py, daemon/slack-send.sh, send-scheduled-slack-dm.sh)
//     convert at send time through
//     system/scripts/slack_format.py `to_blocks`, so existing drafts keep
//     posting. Judged: the tool `text` when no `blocks` is given, every
//     non-`rich_text` block text, and on bash the `--blocks` mrkdwn texts (else
//     `--text`) plus `$(cat)`, ANSI-C and heredoc payloads. Fenced code is
//     skipped; `---`, `*bold*`, `-3`, `1.5x` never match. Canonical text:
//     system/prompts/external-replies.md Rule 4 item 5.
//     Gated by `switchboard-guardrails.slack-native-bullets.disabled`.
// 15. Slack people mentions and PR links, WARN ONLY. Every message switchboard drafts links
//     every thread, PR, issue, doc, or session it refers to and @mentions every
//     person it names (system/prompts/external-replies.md Rule 4 item 6; CoS
//     checks drafts before presenting). This rule is the backstop at send time:
//     prose in a Slack send (tool-level post/update, or bash `switchboard
//     slack post`/`edit`) that names a person who has a
//     `people/<firstname-lastname>.md` profile with no `<@U…>` mention next to
//     the name, that carries a PR number (`#1234`, `PR 1234`,
//     `org/repo#1234`) outside a link, or that names a term in
//     SLACK_GO_LINK_TERMS with no link to its path anywhere in the message, is
//     logged as `slack-people-links warn`
//     and surfaced through `ui.notify` when the host allows it; the send is
//     never rejected or rewritten (the fix needs a lookup the caller owns).
//     Code spans, `<…>` tokens, and `[text](url)` links are not judged; single
//     word profiles and the owner's own profile are ignored. The people directory
//     can be overridden with SWITCHBOARD_GUARDRAILS_PEOPLE_DIR (tests).
//     Gated by `switchboard-guardrails.slack-people-links.disabled`.
// 16. Code review wording, WARN ONLY. A switchboard code review covers the code
//     only: no bot comments, approvals, CI, or points the body already states
//     (system/prompts/external-replies.md Rule 10). This rule is the backstop
//     at write time: a bash `gh pr review` or `gh pr comment` body (inline,
//     --body-file, $(cat), ANSI-C, heredoc) that contains `as the body says`, `needs approval`, or
//     `owner teams` (case-insensitive, whole word) is logged as
//     `github-review-wording warn-bash` with the terms and surfaced through
//     `ui.notify` when the host allows it; the write is never rejected or
//     rewritten. `gh issue comment`, `gh pr create|edit`, and `gh api` are not
//     judged: PR bodies and issues legitimately name CI and owners.
//     Gated by `switchboard-guardrails.github-review-wording.disabled`.
//
// Kill switch: set `switchboard-guardrails.disabled = true` in Amp config.
// Per-rule disable: set `switchboard-guardrails.<rule>.disabled = true`.
// Rule 4 is gated by `switchboard-guardrails.slack-bot-prefix.disabled`.
// Rule 8 is gated by `switchboard-guardrails.cos-delivery-gate.disabled`.
// Rule 9 is gated by `switchboard-guardrails.browser-automation.disabled`.
// CoS sticky approval TTL: `switchboard-guardrails.cos_sticky_allow_ttl_ms`
// (default 30 minutes; set to 0 to require confirmation for every write).
// Confirm deadline: `switchboard-guardrails.confirm_timeout_ms` (default 90 seconds).
// Timeout/UI errors reject the tool call and let the agent continue.

import type { PluginAPI } from '@ampcode/plugin'
import { existsSync, readFileSync, readdirSync, realpathSync } from 'node:fs'
import { dirname, posix, resolve } from 'node:path'
import {
  HOME,
  SWITCHBOARD_REPO,
  paneWorkspace,
  bashStartsWith,
  isWorkerWorkspace,
  isPluginEnabled,
  logEvent,
  runDetached,
} from './switchboard/lib'
import { assignedVars, expand, planRmRf, redirectCwds, helperWord, rewriteToTrash, rewriteNulRedirects, isWindowsHost, isAbsPath, slashPath } from './switchboard/rm-guard'
import { tmpdir } from 'node:os'
import { organizeExecuteViolation, ORGANIZE_EXECUTE_FIX } from './switchboard/organize-execute'
import { envDumpViolation, ENV_FIX } from './switchboard/sensitive-reads'
import { ampAuthViolation, AMP_FIX } from './switchboard/amp-auth'

const PLUGIN = 'switchboard-guardrails'

/** The owner's IANA time zone for calendar defaults: SWITCHBOARD_TZ, else America/Los_Angeles. */
export const DEFAULT_OWNER_TZ = 'America/Los_Angeles'
export function ownerTimeZone(env: Record<string, string | undefined> = process.env): string {
  const zone = (env.SWITCHBOARD_TZ ?? '').trim()
  if (!zone) return DEFAULT_OWNER_TZ
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: zone })
    return zone
  } catch {
    return DEFAULT_OWNER_TZ
  }
}

/**
 * A delete target with symlinks resolved in the folders above it (rm removes a
 * link itself, not what it points to), walking up to the nearest folder that
 * exists. `followLast` resolves the target too (a glob or a trailing slash).
 */
export function realParentPath(path: string, followLast: boolean): string {
  let head = followLast ? path : posix.dirname(path)
  let tail = followLast ? '' : posix.basename(path)
  for (let guard = 0; guard < 64; guard++) {
    // Windows only: nothing above a root to resolve. realpath('/') is `C:\` there, which
    // turned Git Bash `/tmp/x` into `C:\/tmp/x` and sent temp deletes to the Bin. Other
    // hosts keep the original order: try the real path first, then stop at a root, so
    // macOS `/tmp` resolves to `/private/tmp`. This is about the real file system, so it
    // reads process.platform, never the SWITCHBOARD_RM_GUARD_PLATFORM policy override a test sets.
    if (process.platform === 'win32' && (head === '/' || head === '.' || /^[A-Za-z]:\/?$/.test(head))) return path
    try {
      const real = slashPath(realpathSync(head))
      return tail ? posix.join(real, tail) : real
    } catch { /* not there yet */ }
    if (head === '/' || head === '.') return path
    tail = tail ? posix.join(posix.basename(head), tail) : posix.basename(head)
    head = posix.dirname(head)
  }
  return path
}

/** The Windows temp folder Git Bash calls /tmp, only when it is a temp folder by
 * name and not home (the same test switchboard-trash.py uses). Empty on other hosts. */
export function windowsTempRoots(): string[] {
  if (!isWindowsHost()) return []
  const temp = slashPath(tmpdir()).replace(/\/+$/, '')
  const named = /\/(temp|tmp)$/i.test(temp)
  return named && temp.toLowerCase() !== slashPath(HOME).replace(/\/+$/, '').toLowerCase() ? [temp] : []
}

/** The drop-in `rm -rf` that moves outside targets to the Trash (Rule 3). */
export function trashHelperPath(): string {
  return process.env.SWITCHBOARD_TRASH_HELPER ?? `${SWITCHBOARD_REPO}/system/scripts/switchboard-trash.py`
}

type ConfirmContext = { ui: { confirm: (options: { title: string; message?: string; confirmButtonText?: string }) => Promise<boolean> } }
type ConfirmationResult = boolean | { action: 'reject-and-continue'; message: string }

/** Bound the handler's wait; late UI responses cannot authorize this call.
 * Amp's current UI API has no cancellation method for the underlying dialog.
 */
export async function confirmWithTimeout(
  ctx: ConfirmContext,
  options: Parameters<ConfirmContext['ui']['confirm']>[0],
  configuredTimeout: unknown,
  rule: string,
): Promise<ConfirmationResult> {
  const timeoutMs = typeof configuredTimeout === 'number' && Number.isFinite(configuredTimeout)
    && configuredTimeout > 0 && configuredTimeout <= 2_147_483_647 ? configuredTimeout : 90_000
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const result = await Promise.race([
      Promise.resolve().then(() => ctx.ui.confirm(options)).catch(() => 'ui-error' as const),
      new Promise<'timeout'>(resolve => { timer = setTimeout(() => resolve('timeout'), timeoutMs) }),
    ])
    if (typeof result === 'boolean') return result
    // Logging must not extend the confirmation deadline.
    void logEvent(PLUGIN, { rule, action: 'confirm-timeout', reason: result, timeoutMs })
    return {
      action: 'reject-and-continue',
      message: `Blocked by switchboard-guardrails (${rule}): confirmation ${result === 'timeout' ? 'timed out' : 'UI failed'}; no approval was granted. Do not retry this modal. For CoS delivery, use system/scripts/router-dispatch.sh (Routers use route-to-workspace.sh). Otherwise ask the owner in chat for approval and continue independent work. Keep any required email preview and other safety checks.`,
    }
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

// Bot attribution prefix (switchboard AGENTS.md "Bot Attribution Prefix"): the shortcode opens the first
// sentence — emoji, one space, then the first word — never alone on its own
// line. The constant name is load-bearing: the portable kit slices this file on it.
const SLACK_BOT_PREFIX = ':robot_face:'
const SLACK_BOT_PREFIX_ESCAPED = SLACK_BOT_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// The correct shape: prefix, at least one space, then a word on the same line.
// A literal `\n`/`\r` escape right after the prefix is how inline shell payloads
// spell a line break, so it does not count as the first word.
const SLACK_BOT_PREFIX_RE = new RegExp(`^[ \\t]*${SLACK_BOT_PREFIX_ESCAPED}[ \\t]+(?!\\\\[nr])\\S`)
// The retired shape: the prefix alone on the first line (real or
// literal line break after it). The tool-level injector folds it onto the first
// sentence; bash sends are rejected with their own reason.
const SLACK_BOT_PREFIX_STANDALONE_RE = new RegExp(`^[ \\t]*${SLACK_BOT_PREFIX_ESCAPED}[ \\t]*(?:\\\\n|\\r?\\n)`)
// What the injector strips before re-prefixing: leading whitespace, and a
// leading prefix in any shape together with the whitespace and line breaks
// (real or literal) that follow it.
const SLACK_BOT_PREFIX_LEAD_RE = new RegExp(`^[ \\t]*(?:${SLACK_BOT_PREFIX_ESCAPED}(?:\\s|\\\\[nr])*)?`)

// GitHub form of the same emoji: GitHub has no robot_face
// shortcode, so the first paragraph opens with the Slack PNG hosted as a GitHub
// user attachment, 20px high, or the unicode robot when no image can embed;
// the first word follows on the same line. Any user-attachment id passes so a
// per-repository copy also works; AGENTS.md records the canonical URL and the
// reject message quotes it from there.
const GITHUB_BOT_PREFIX_IMG_SRC_RE = /\bsrc="https:\/\/github\.com\/user-attachments\/assets\/[A-Za-z0-9-]+"/
const GITHUB_BOT_PREFIX_IMG_HEIGHT_RE = /\bheight="20"/
// An `<img …>` tag opening a line, with whatever follows it on that line captured.
const GITHUB_BOT_PREFIX_IMG_LEAD_RE = /^(<img\b[^<>]*>)[ \t]*(.*)$/
const GITHUB_BOT_PREFIX_FALLBACK = '\u{1F916}'
const GITHUB_BOT_PREFIX_FALLBACK_LEAD_RE = /^\u{1F916}\uFE0F?[ \t]*(.*)$/u
// A markdown ATX heading line ("## Why"). PR bodies open with one, and the
// attribution rule puts the prefix at the start of the paragraph directly under
// the first heading; a prefix above a heading renders the robot inline on the
// heading line, so that shape is rejected.
const MARKDOWN_HEADING_LINE_RE = /^ {0,3}#{1,6}(?:[ \t]+|$)/
// Line breaks as the body carries them: a real newline, or the literal `\n`
// that inline shell payloads spell out.
const BODY_LINE_BREAK_RE = /\\n|\r?\n/
// The sentence in AGENTS.md that carries the hosted URL; the plugin reads it
// rather than duplicating the id in code.
const GITHUB_BOT_PREFIX_CANONICAL_RE = /<img src="https:\/\/github\.com\/user-attachments\/assets\/[A-Za-z0-9-]+" height="20" alt="robot_face">/
const GITHUB_BOT_PREFIX_IMG_TEMPLATE = '<img src="https://github.com/user-attachments/assets/<id from AGENTS.md Bot Attribution Prefix>" height="20" alt="robot_face">'

// `gh` writes that carry comment or body text. `gh pr merge --body` is a commit
// message and `gh pr create --fill` has no inspectable body, so neither is gated.
const GITHUB_BASH_WRITE_AT_COMMAND_START = /^gh\s+(?:(pr|issue)\s+(comment|create|review|edit)|(api))\b/i
// pr/issue subcommands: -b/--body inline, -F/--body-file from a file.
const GITHUB_PR_BODY_FLAG_RE = /(?:^|\s)(?:--body|--body-file|-b|-F)(?=[=\s]|$)/
const GITHUB_PR_BODY_FILE_RE = /(?:^|\s)(?:--body-file|-F)(?:=|\s+)['"]?([^'"\s]+)['"]?/g
// gh api: a `body` field (inline or @file) or a JSON --input file.
const GITHUB_API_BODY_FLAG_RE = /(?:^|\s)(?:(?:-f|-F|--field|--raw-field)(?:=|\s+)['"]?body=|--input(?=[=\s]))/
const GITHUB_API_BODY_FILE_RE = /\bbody=@['"]?([^'"\s]+)['"]?/g
const GITHUB_API_INPUT_FILE_RE = /(?:^|\s)--input(?:=|\s+)['"]?([^'"\s]+)['"]?/g
const BASH_HEREDOC_RE = /<<-?\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?[^\n]*\n([\s\S]*?)\n[ \t]*\1[ \t]*(?:\n|$)/g

// All Slack tools that send or update a message — DM, channel, thread reply.
// The bot-prefix rule applies to all of them; AGENTS.md says any bot-authored
// Slack message must lead with the prefix.
const SLACK_POST_TOOLS = new Set([
  'mcp__slack__chat_postMessage',
  'mcp__slack__chat_update',
  'mcp__slack__send_dm',
])

const SLACK_BASH_WRITE_AT_COMMAND_START =
  /^switchboard\s+slack\s+((?:post|send|reply|update|edit)[a-z-]*|write-canvas)\b/i
// Per-send prefix opt-out. The marker is the
// literal env assignment `SWITCHBOARD_NO_ROBOT=1`; an agent writes it only after the owner
// said "no robot" / "remove the robot" for that specific message. Bash: among
// the leading `NAME=value` assignments of the `switchboard slack …` command
// itself (never on a wrapper such as `bash -c`, whose payload is judged on its
// own). Tool level: the first line of `text`, alone, stripped before the send.
export const SLACK_NO_ROBOT_MARKER = 'SWITCHBOARD_NO_ROBOT=1'
const SHELL_ASSIGNMENT_WORD = String.raw`[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"[^"]*"|[^\s'";&|])*`
// Leading `NAME=value …` assignments before the command word. `bashCommandStartMatch`
// skips them when the raw command start did not match, so `FOO=1 git add -A`
// is judged as `git add -A` and `SWITCHBOARD_NO_ROBOT=1 switchboard slack …` still
// runs the Slack link, bullet, people-links, and naming rules.
const LEADING_SHELL_ASSIGNMENTS_RE = new RegExp(String.raw`^(?:${SHELL_ASSIGNMENT_WORD}\s+)+`)
// The marker among the leading assignments, case-sensitive and exactly `=1`.
const SLACK_NO_ROBOT_LEADING_RE = new RegExp(
  String.raw`^(?:${SHELL_ASSIGNMENT_WORD}\s+)*${SLACK_NO_ROBOT_MARKER}\s+(?:${SHELL_ASSIGNMENT_WORD}\s+)*`,
)
// Comms Triage approved replies go out through this script, which posts the
// stored draft from the owner's account with the :robot_face: prefix it guarantees
// itself (`triage_ledger.with_bot_prefix`) and records `sent` in
// the triage ledger. Recognised explicitly so the script-guarded path is logged
// and auditable rather than an accident of the write regex not matching.
const COMMS_TRIAGE_SEND_AT_COMMAND_START =
  /^(?:python3?\s+)?(?:\S*\/)?system\/scripts\/send-triage-draft\.py\b/
const GIT_ADD_ALL_AT_COMMAND_START =
  /^git\s+add\s+(?:-A\b|--all\b|\.(?=[ \t]*(?:$|[;&|]|\n)))/
const GIT_PUSH_FORCE_AT_COMMAND_START =
  /^git\s+push\b[^\n;&|]*(?:--force\b|--no-verify\b|-f\b)/

/** Find a match only when its first token starts a shell command.
 *
 * Shell separators inside quotes are argument text, not command boundaries.
 * Command substitution remains executable inside double quotes, but not inside
 * single quotes. This intentionally is not a complete shell parser; it covers
 * the command boundaries accepted by the Bash guardrails.
 */
export function bashCommandStartMatch(cmd: string, pattern: RegExp, depth = 0): RegExpExecArray | null {
  const starts = [0]
  let singleQuoted = false
  let doubleQuoted = false

  for (let i = 0; i < cmd.length; i++) {
    const char = cmd[i]

    if (char === '\\' && !singleQuoted) {
      i++
      continue
    }
    if (char === "'" && !doubleQuoted) {
      singleQuoted = !singleQuoted
      continue
    }
    if (char === '"' && !singleQuoted) {
      doubleQuoted = !doubleQuoted
      continue
    }
    if (!singleQuoted && char === '$' && cmd[i + 1] === '(') {
      starts.push(i + 2)
      i++
      continue
    }
    if (singleQuoted || doubleQuoted) continue

    if (char === '\n' || char === ';') {
      starts.push(i + 1)
      continue
    }
    if (char === '&' || char === '|') {
      const next = cmd[i + 1] === char ? i + 2 : i + 1
      starts.push(next)
      i = next - 1
    }
  }

  for (const start of starts) {
    const command = cmd.slice(start).trimStart()
    const match = pattern.exec(command) ?? execAfterLeadingAssignments(command, pattern)
    if (match) return match
    // A wrapper behind leading assignments (`FOO=1 bash -c '…'`) is still a
    // wrapper; its payload is judged on its own, without those assignments.
    const unassigned = command.replace(LEADING_SHELL_ASSIGNMENTS_RE, '')
    const wrapper = /^(?:\S*\/)?(?:bash|sh|zsh)\s+(?:(?:--noprofile|--norc|--posix)\s+)*-[a-z]*c[a-z]*\s+/i.exec(unassigned)
    if (wrapper && depth < 16) {
      const payload = literalShellWord(unassigned.slice(wrapper[0].length))
      if (payload !== null) {
        const nested = bashCommandStartMatch(payload, pattern, depth + 1)
        if (nested) return nested
      }
    }
  }
  return null
}

/** Retry a `^`-anchored command-start pattern past leading `NAME=value` assignments.
 *
 * The match is taken on the raw command so `match.input` and `match[0]` keep the
 * assignments: `slackNoRobotOptOutBash` reads the marker from there.
 */
function execAfterLeadingAssignments(command: string, pattern: RegExp): RegExpExecArray | null {
  if (!pattern.source.startsWith('^') || !LEADING_SHELL_ASSIGNMENTS_RE.test(command)) return null
  const withAssignments = new RegExp(LEADING_SHELL_ASSIGNMENTS_RE.source + pattern.source.slice(1), pattern.flags)
  return withAssignments.exec(command)
}

/** Decode one literal shell word. Dynamic expansions are not evaluated. */
function literalShellWord(input: string): string | null {
  let quote = ''
  let result = ''
  for (let i = 0; i < input.length; i++) {
    const char = input[i]
    if (!quote && /[\s;&|]/.test(char)) break
    if (char === quote) { quote = ''; continue }
    if (!quote && (char === "'" || char === '"')) { quote = char; continue }
    if (quote !== "'" && char === '\\') {
      if (i + 1 === input.length) return null
      const next = input[++i]
      if (quote === '"' && !/[\\"$`\n]/.test(next)) result += '\\'
      if (next !== '\n') result += next
      continue
    }
    if (quote !== "'" && (char === '$' || char === '`')) return null
    result += char
  }
  return quote ? null : result
}

/**
 * Decode the body of one ANSI-C quoted word (`$'…'`, bash QUOTING). Agents
 * legitimately write `--text $':robot_face:\nIt\'s approved'` to get a real
 * line break after the prefix; the raw command text then carries `\n` and
 * `\'` as escapes and never matches the literal prefix line.
 */
export function decodeAnsiCQuoted(body: string): string {
  const simple: Record<string, string> = {
    a: '\x07', b: '\b', e: '\x1b', E: '\x1b', f: '\f', n: '\n', r: '\r', t: '\t', v: '\v',
    '\\': '\\', "'": "'", '"': '"', '?': '?',
  }
  let out = ''
  for (let i = 0; i < body.length; i++) {
    const char = body[i]
    if (char !== '\\' || i + 1 === body.length) { out += char; continue }
    const next = body[++i]
    if (next in simple) { out += simple[next]; continue }
    let hex: RegExpExecArray | null
    if (next === 'x' && (hex = /^[0-9a-fA-F]{1,2}/.exec(body.slice(i + 1)))) {
      out += String.fromCharCode(parseInt(hex[0], 16)); i += hex[0].length; continue
    }
    if ((next === 'u' || next === 'U') && (hex = new RegExp(`^[0-9a-fA-F]{1,${next === 'u' ? 4 : 8}}`).exec(body.slice(i + 1)))) {
      out += String.fromCodePoint(parseInt(hex[0], 16)); i += hex[0].length; continue
    }
    const octal = /^[0-7]{1,3}/.exec(body.slice(i))
    if (octal) { out += String.fromCharCode(parseInt(octal[0], 8)); i += octal[0].length - 1; continue }
    out += '\\' + next
  }
  return out
}

/** Every ANSI-C quoted argument in a command, decoded; `$'` inside quotes is literal. */
export function ansiCQuotedWords(cmd: string): string[] {
  const words: string[] = []
  let singleQuoted = false
  let doubleQuoted = false
  for (let i = 0; i < cmd.length; i++) {
    const char = cmd[i]
    if (char === '\\' && !singleQuoted) { i++; continue }
    if (char === "'" && !doubleQuoted) { singleQuoted = !singleQuoted; continue }
    if (char === '"' && !singleQuoted) { doubleQuoted = !doubleQuoted; continue }
    if (singleQuoted || doubleQuoted || char !== '$' || cmd[i + 1] !== "'") continue
    let end = i + 2
    while (end < cmd.length && cmd[end] !== "'") end += cmd[end] === '\\' ? 2 : 1
    if (end >= cmd.length) break
    words.push(decodeAnsiCQuoted(cmd.slice(i + 2, end)))
    i = end
  }
  return words
}

/** Find a Slack write only when `switchboard` starts a shell command. */
export function slackBashWriteSubcommand(cmd: string): string | null {
  return bashCommandStartMatch(cmd, SLACK_BASH_WRITE_AT_COMMAND_START)?.[1] ?? null
}

/** Subcommand of a Slack write whose own command carries `SWITCHBOARD_NO_ROBOT=1` among its leading assignments; null otherwise. */
export function slackNoRobotOptOutBash(cmd: string): string | null {
  const match = bashCommandStartMatch(cmd, SLACK_BASH_WRITE_AT_COMMAND_START)
  if (!match || !SLACK_NO_ROBOT_LEADING_RE.test(match.input)) return null
  return match[1]
}

/** Tool-level marker: `text` whose first non-blank line is exactly `SWITCHBOARD_NO_ROBOT=1`. Returns the text without that line, or null. */
export function slackNoRobotOptOutText(text: string): string | null {
  const m = /^\s*SWITCHBOARD_NO_ROBOT=1[ \t]*(?:\n|$)/.exec(text)
  if (!m) return null
  return text.slice(m[0].length).replace(/^\s+/, '')
}

export function isCommsTriageSend(cmd: string): boolean {
  return bashCommandStartMatch(cmd, COMMS_TRIAGE_SEND_AT_COMMAND_START) !== null
}

// Flag values as `switchboard slack post` takes them: `--flag value`
// or `--flag=value`, with or without one quote pair. Used by rules 4 and 13.
function bashFlagValue(cmd: string, flag: string): string | null {
  const re = new RegExp(`(?:^|\\s)${flag}(?:=|\\s+)(?:"([^"]*)"|'([^']*)'|(\\S+))`)
  const m = re.exec(cmd)
  if (!m) return null
  return m[1] ?? m[2] ?? m[3] ?? null
}

// The file behind `--flag "$(cat /path)"` (quoted or not, `=` or space), or null
// when the flag's value is anything else. `bashFlagValue` returns the literal
// `$(cat …)` for these, which no JSON parser reads.
function bashFlagCatPath(cmd: string, flag: string): string | null {
  const re = new RegExp(`(?:^|\\s)${flag}(?:=|\\s+)["']?\\$\\(\\s*cat\\s+['"]?([^'")\\s]+)['"]?\\s*\\)["']?(?=\\s|$)`)
  return re.exec(cmd)?.[1] ?? null
}

// ── Rule 13 helpers: Slack link and channel-mention format ───────────
// Links are inline with anchor text that names the
// thing (`<https://url|anchor>`; `[anchor](url)` in markdown blocks), never a
// bare URL; channel mentions are `<#C…|name>`, never plain `#name`, which Slack
// leaves as text in an API-posted message.
// Detection is shared with system/scripts/slack_format.py; keep the two in
// step (test-fixtures/slack-link-format-cases.json runs against both).

const SLACK_CHANNEL_ID_RE = /^[CG][A-Z0-9]{8,}$/
// A typed channel mention: `#name` where name is a Slack channel slug (lowercase
// letters, digits, `_`, `-`, at least one letter, no trailing `_`/`-`). Not preceded by a word
// character, `<`, `#`, `/`, `&`, `@`, `.`, or `-` so issue refs (`example-repo#10002`),
// anchors in URLs, and mrkdwn tokens never match. Pure digits (`#1`) are not channels.
const SLACK_PLAIN_CHANNEL_RE = /(?<![\w<#/&@.\-])#([a-z0-9](?:[a-z0-9_\-]*[a-z0-9])?)(?![\w\-])/g
const SLACK_FENCED_CODE_RE = /```[\s\S]*?```/g
const SLACK_INLINE_CODE_RE = /`[^`\n]*`/g
const SLACK_ANGLE_TOKEN_RE = /<([^<>\n]*)>/g
const SLACK_MARKDOWN_LINK_RE = /\[([^\]\n]*)\]\((<?)(https?:\/\/[^)\s]+)>?\)/g
const BARE_URL_RE = /https?:\/\/[^\s<>]+/g

function trimUrlPunctuation(url: string): string {
  return url.replace(/[.,;:!?)\]'"]+$/, '')
}

/** Message text with code spans, mrkdwn tokens, and labelled links removed, leaving only prose the rule judges. */
function slackProseForLinkCheck(text: string): { prose: string; bareFromTokens: string[] } {
  const bareFromTokens: string[] = []
  let prose = text.replace(SLACK_FENCED_CODE_RE, ' ').replace(SLACK_INLINE_CODE_RE, ' ')
  prose = prose.replace(SLACK_ANGLE_TOKEN_RE, (_m, inner: string) => {
    // `<https://url>` with no label renders the URL itself: still bare.
    const [target, label] = inner.split('|', 2)
    if (/^https?:\/\//i.test(target) && !(label ?? '').trim()) bareFromTokens.push(trimUrlPunctuation(target))
    return ' '
  })
  prose = prose.replace(SLACK_MARKDOWN_LINK_RE, (_m, label: string, _lt: string, url: string) => {
    // `[https://x](https://x)` names nothing; a label that is itself a URL is bare.
    if (!label.trim() || /^https?:\/\/\S*$/i.test(label.trim())) bareFromTokens.push(trimUrlPunctuation(url))
    return ' '
  })
  return { prose, bareFromTokens }
}

/** URLs in the message that are not wrapped as `<url|anchor>` or `[anchor](url)`, in order, deduplicated. */
export function slackBareUrls(text: string): string[] {
  const { prose, bareFromTokens } = slackProseForLinkCheck(text)
  const found = [...bareFromTokens, ...[...prose.matchAll(BARE_URL_RE)].map((m) => trimUrlPunctuation(m[0]))]
  return [...new Set(found)]
}

/** Channel names typed as plain `#name` (not `<#C…>`), in order, deduplicated. */
export function slackPlainChannelMentions(text: string): string[] {
  const { prose } = slackProseForLinkCheck(text)
  const names = [...prose.matchAll(SLACK_PLAIN_CHANNEL_RE)].map((m) => m[1]).filter((n) => /[a-z]/.test(n))
  return [...new Set(names)]
}

export type SlackChannelResolver = (name: string) => string | null

/** `name → C… id` pairs from priority-channels.yaml text: an `id:` line and the `name:` at the same key column that follows it. */
export function channelIdsFromPriorityYaml(yaml: string): Map<string, string> {
  const out = new Map<string, string>()
  let pending: { id: string; column: number } | null = null
  for (const line of yaml.split('\n')) {
    const idMatch = /^(\s*(?:-\s+)?)id:\s*([A-Z0-9]+)\s*$/.exec(line)
    if (idMatch) {
      pending = SLACK_CHANNEL_ID_RE.test(idMatch[2]) ? { id: idMatch[2], column: idMatch[1].length } : null
      continue
    }
    const nameMatch = /^(\s*)name:\s*"?([^"\s#]+)"?\s*(?:#.*)?$/.exec(line)
    if (nameMatch && pending && nameMatch[1].length === pending.column) {
      if (!out.has(nameMatch[2])) out.set(nameMatch[2], pending.id)
      pending = null
    }
  }
  return out
}

/** `name → C… id` pairs from comms source jsonl lines carrying `channel` and `channel_id`. */
export function channelIdsFromSourceJsonl(jsonl: string): Map<string, string> {
  const out = new Map<string, string>()
  for (const m of jsonl.matchAll(/"channel":\s*"([^"\\]+)",\s*"channel_id":\s*"([CG][A-Z0-9]{8,})"/g)) {
    if (!out.has(m[1])) out.set(m[1], m[2])
  }
  return out
}

const SLACK_CHANNEL_SOURCE_FILES = [
  'system/state/sources/owner-mentions.jsonl',
  'system/state/sources/owner-slack-sent.jsonl',
  'system/state/sources/owner-channel-signals.jsonl',
  'system/state/sources/owner-mentions.archive.jsonl',
]
let channelIdCache: { repo: string; map: Map<string, string>; at: number } | null = null

/** Channel name → id from priority-channels.yaml first, then the comms source files; cached for 10 minutes. */
export function resolveSlackChannelId(name: string, repo: string = SWITCHBOARD_REPO): string | null {
  if (!channelIdCache || channelIdCache.repo !== repo || Date.now() - channelIdCache.at > 600_000) {
    const map = new Map<string, string>()
    try {
      for (const [n, id] of channelIdsFromPriorityYaml(readFileSync(`${repo}/system/config/priority-channels.yaml`, 'utf8'))) {
        if (!map.has(n)) map.set(n, id)
      }
    } catch { /* no priority list: fall through to the sources */ }
    for (const rel of SLACK_CHANNEL_SOURCE_FILES) {
      try {
        for (const [n, id] of channelIdsFromSourceJsonl(readFileSync(`${repo}/${rel}`, 'utf8'))) {
          if (!map.has(n)) map.set(n, id)
        }
      } catch { /* source absent */ }
    }
    channelIdCache = { repo, map, at: Date.now() }
  }
  return channelIdCache.map.get(name) ?? null
}

export type SlackLinkFormatFinding = {
  bareUrls: string[]
  /** `#name` tokens rewritten to `<#ID|name>`, with the id used. */
  rewritten: { name: string; id: string }[]
  /** `#name` tokens with no known id. */
  unresolved: string[]
  /** The text with every resolvable `#name` rewritten; equal to the input when nothing was rewritten. */
  text: string
}

/** Judge one Slack message text against rule 13 and rewrite the channel mentions it can. */
export function checkSlackLinkFormat(text: string, resolve: SlackChannelResolver = resolveSlackChannelId): SlackLinkFormatFinding {
  const bareUrls = slackBareUrls(text)
  const rewritten: { name: string; id: string }[] = []
  const unresolved: string[] = []
  let out = text
  for (const name of slackPlainChannelMentions(text)) {
    const id = resolve(name)
    if (!id) { unresolved.push(name); continue }
    rewritten.push({ name, id })
    // Rewrite only the prose occurrences: code spans and existing tokens keep their text.
    out = rewriteOutsideTokens(out, name, `<#${id}|${name}>`)
  }
  return { bareUrls, rewritten, unresolved, text: out }
}

function rewriteOutsideTokens(text: string, name: string, replacement: string): string {
  const protectedRe = new RegExp(`${SLACK_FENCED_CODE_RE.source}|${SLACK_INLINE_CODE_RE.source}|${SLACK_ANGLE_TOKEN_RE.source}`, 'g')
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const tokenRe = new RegExp(`(?<![\\w<#/&@.\\-])#${escaped}(?![\\w\\-])`, 'g')
  let result = ''
  let last = 0
  for (const m of text.matchAll(protectedRe)) {
    result += text.slice(last, m.index).replace(tokenRe, replacement) + m[0]
    last = (m.index ?? 0) + m[0].length
  }
  return result + text.slice(last).replace(tokenRe, replacement)
}

/**
 * Every string under a `text` key in a Block Kit payload (blocks, elements, fields), or the raw string when it is not JSON.
 * A rich_text `link` element is a labelled link when its `text` is non-empty, so its `url` is never judged as prose;
 * a `link` with no `text` renders the URL itself, so its `url` is returned and the link rule reads it as bare
 * (mirrors slack_format.py `blocks_texts`; the `blocks` cases in test-fixtures/slack-link-format-cases.json run against both).
 */
export function slackBlocksTexts(blocks: unknown): string[] {
  let value: unknown = blocks
  if (typeof blocks === 'string') {
    try { value = JSON.parse(blocks) } catch { return [blocks] }
  }
  const out: string[] = []
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (!node || typeof node !== 'object') return
    const rec = node as Record<string, unknown>
    if (rec.type === 'link' && typeof rec.url === 'string' && !(typeof rec.text === 'string' && rec.text.trim())) out.push(rec.url)
    for (const [key, v] of Object.entries(rec)) {
      if (key === 'text' && typeof v === 'string') out.push(v)
      else walk(v)
    }
  }
  walk(value)
  return out
}

/**
 * The Block Kit payload behind `--blocks "$(cat blocks.json)"`, parsed, with its path; null when `--blocks` is not a
 * `$(cat)` or the file is unreadable or not JSON (then the file is read raw like any other `$(cat)`, so nothing is skipped).
 * Judged as raw prose, a slack_format.py blocks file would read `"url": "https://…"` on a labelled rich_text link
 * as a bare URL.
 */
async function slackBashBlocksFile(cmd: string): Promise<{ path: string; blocks: unknown } | null> {
  const path = bashFlagCatPath(cmd, '--blocks')
  if (!path) return null
  const { readFile } = await import('node:fs/promises')
  try { return { path, blocks: JSON.parse(await readFile(path, 'utf8')) } } catch { return null }
}

/** The message texts a bash `switchboard slack` write carries: `--blocks` strings (then `--text` is only the notification fallback; a `$(cat)` blocks file is parsed as Block Kit), else `--text`, plus other `$(cat)` files, ANSI-C words, and heredocs. */
export async function slackBashMessageTexts(cmd: string): Promise<string[]> {
  const texts: string[] = []
  const blocksFile = await slackBashBlocksFile(cmd)
  const blocks = bashFlagValue(cmd, '--blocks')
  if (blocksFile) texts.push(...slackBlocksTexts(blocksFile.blocks))
  else if (blocks !== null) texts.push(...slackBlocksTexts(blocks))
  else {
    const text = bashFlagValue(cmd, '--text')
    if (text !== null) texts.push(text)
  }
  const { readFile } = await import('node:fs/promises')
  for (const path of catSubstitutionPaths(cmd)) {
    if (path === blocksFile?.path) continue
    try { texts.push(await readFile(path, 'utf8')) } catch { /* unreadable: rule 4 rejects */ }
  }
  texts.push(...ansiCQuotedWords(cmd), ...heredocBodies(cmd))
  return texts
}

// ── Rule 14 helpers: Slack native bullets ────────────────────────────
// Every text bullet becomes a native Slack rich_text list (true bullets,
// proper wrap), with the words unchanged. A list line is
// optional indent, then `•`, `◦`, `▪`, `-`, `*`, `N.` or `N)`, then at least
// one space and a non-space. Fenced code is not judged. Detection is shared
// with system/scripts/slack_format.py (`bullet_lines`); keep the two in step
// (test-fixtures/slack-native-bullets-cases.json runs against both).

const SLACK_TEXT_BULLET_RE = /^[ \t]*(?:[•◦▪]|-|\*|\d{1,3}[.)])[ \t]+\S/

/** The lines of `text` (outside fenced code) that are text bullets, verbatim. */
export function slackBulletLines(text: string): string[] {
  const out: string[] = []
  for (const line of text.replace(SLACK_FENCED_CODE_RE, '').split('\n')) {
    if (SLACK_TEXT_BULLET_RE.test(line)) out.push(line)
  }
  return out
}

/** Every string under a `text` key in a Block Kit payload, skipping `rich_text` blocks (native lists are the goal, not a fault) and `markdown` blocks (Slack converts their `- ` items to rich_text lists server-side). */
export function slackMrkdwnTexts(blocks: unknown): string[] {
  let value: unknown = blocks
  if (typeof blocks === 'string') {
    try { value = JSON.parse(blocks) } catch { return [blocks] }
  }
  const out: string[] = []
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (!node || typeof node !== 'object') return
    const rec = node as Record<string, unknown>
    if (rec.type === 'rich_text' || rec.type === 'markdown') return
    for (const [key, v] of Object.entries(rec)) {
      if (key === 'text' && typeof v === 'string') out.push(v)
      else walk(v)
    }
  }
  walk(value)
  return out
}

/** The texts a bash Slack write carries for the bullet rule: with `--blocks`, only its mrkdwn texts (the `--text` fallback may keep the glyphs); otherwise `--text` plus file, ANSI-C and heredoc payloads. */
export async function slackBashMrkdwnTexts(cmd: string): Promise<string[]> {
  const blocksFile = await slackBashBlocksFile(cmd)
  if (blocksFile) return slackMrkdwnTexts(blocksFile.blocks)
  const blocks = bashFlagValue(cmd, '--blocks')
  if (blocks !== null) return slackMrkdwnTexts(blocks)
  const texts: string[] = []
  const text = bashFlagValue(cmd, '--text')
  if (text !== null) texts.push(text)
  const { readFile } = await import('node:fs/promises')
  for (const path of catSubstitutionPaths(cmd)) {
    try { texts.push(await readFile(path, 'utf8')) } catch { /* unreadable: rule 4 rejects */ }
  }
  texts.push(...ansiCQuotedWords(cmd), ...heredocBodies(cmd))
  return texts
}

// ── Rule 15 helpers: people mentions and PR links in Slack (warn only) ─
// Drafts link the threads they refer to and @mention the people they name. A person
// is known when `people/<firstname-lastname>.md` exists on main; the name is
// the slug title-cased. First names alone are not matched (too ambiguous for
// a backstop), nor are single-word profiles or the owner's own profile.

const PEOPLE_DIR_ENV = 'SWITCHBOARD_GUARDRAILS_PEOPLE_DIR'
const OWNER_PEOPLE_SLUGS = new Set(['owner-example'])
const SLACK_USER_MENTION_RE = /<@[UW][A-Z0-9]+(?:\|[^>]*)?>/g
/** Chars either side of a name within which a `<@U…>` token counts as that person's mention ("<@U1> (Ada Lovelace, eng lead)"). */
const MENTION_PROXIMITY = 60
/** `#1234`, `repo#1234`, `org/repo#1234`; never a channel (`#name`), a colour, an anchor inside a URL, or a mrkdwn token. */
const SLACK_PR_HASH_RE = /(?<![\w<#/&@.\-])((?:(?:[A-Za-z][\w.-]*\/)?[A-Za-z][\w.-]*)?#\d{2,})(?![\w\-])/g
/** `PR 1234` (a `PR #1234` is already caught by SLACK_PR_HASH_RE as `#1234`). */
const SLACK_PR_WORD_RE = /\bPRs?\s+(\d{2,})\b/g
/**
 * Internal teams, processes, and tools that are always written as a short link on first mention.
 * Key: the display name as it appears in prose (matched case-insensitively as a whole word); value:
 * the short-link path. Empty in the kit; add your organization's terms here.
 */
const SLACK_GO_LINK_TERMS: ReadonlyArray<{ name: string; go: string }> = []

/** `people/` file names → display names: `ada-lovelace.md` → `Ada Lovelace`. Skips `_template.md`, `_archive`, non-Markdown, single-word, and owner profiles. */
export function peopleNamesFromFilenames(files: string[]): string[] {
  const names = new Set<string>()
  for (const file of files) {
    const base = file.split('/').pop() ?? file
    if (!base.endsWith('.md') || base.startsWith('_')) continue
    const slug = base.slice(0, -3)
    if (OWNER_PEOPLE_SLUGS.has(slug)) continue
    const parts = slug.split('-')
    if (parts.length < 2 || !parts.every((p) => /^[a-z]+$/.test(p))) continue
    names.add(parts.map((p) => p[0].toUpperCase() + p.slice(1)).join(' '))
  }
  return [...names].sort()
}

let peopleNamesCache: { dir: string; names: string[]; at: number } | null = null

/** Display names of everyone with a `people/` profile; cached for 10 minutes. Empty when the directory is unreadable. */
export function knownPeopleNames(dir: string = process.env[PEOPLE_DIR_ENV] || `${SWITCHBOARD_REPO}/people`): string[] {
  if (!peopleNamesCache || peopleNamesCache.dir !== dir || Date.now() - peopleNamesCache.at > 600_000) {
    let files: string[] = []
    try { files = readdirSync(dir) } catch { /* no people directory: nothing to match */ }
    peopleNamesCache = { dir, names: peopleNamesFromFilenames(files), at: Date.now() }
  }
  return peopleNamesCache.names
}

/** `text` with code spans, `<…>` tokens, and `[label](url)` links blanked to spaces of equal length, so indices still line up with the original. */
function slackProseMasked(text: string): string {
  const blank = (m: string) => ' '.repeat(m.length)
  return text
    .replace(SLACK_FENCED_CODE_RE, blank)
    .replace(SLACK_INLINE_CODE_RE, blank)
    .replace(SLACK_MARKDOWN_LINK_RE, blank)
    .replace(SLACK_ANGLE_TOKEN_RE, blank)
}

/** Known people named in the prose with no `<@U…>` mention within MENTION_PROXIMITY chars of the name, in `names` order. */
export function slackUnlinkedPeople(text: string, names: string[] = knownPeopleNames()): string[] {
  if (!names.length) return []
  const prose = slackProseMasked(text)
  const mentions = [...text.matchAll(SLACK_USER_MENTION_RE)].map((m) => ({ start: m.index ?? 0, end: (m.index ?? 0) + m[0].length }))
  const out: string[] = []
  for (const name of names) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s+')
    const re = new RegExp(`(?<![\\w@])${escaped}(?!\\w)`, 'gi')
    let unlinked = false
    for (const m of prose.matchAll(re)) {
      const start = (m.index ?? 0) - MENTION_PROXIMITY
      const end = (m.index ?? 0) + m[0].length + MENTION_PROXIMITY
      const near = mentions.some((t) => t.end > start && t.start < end)
      if (!near) { unlinked = true; break }
    }
    if (unlinked) out.push(name)
  }
  return out
}

/** PR references in the prose that are not inside a link: `#1234`, `org/repo#1234`, `PR 1234`, in order, deduplicated. */
export function slackUnlinkedPrRefs(text: string): string[] {
  const { prose } = slackProseForLinkCheck(text)
  const refs = [
    ...[...prose.matchAll(SLACK_PR_HASH_RE)].map((m) => m[1]),
    ...[...prose.matchAll(SLACK_PR_WORD_RE)].map((m) => `PR ${m[1]}`),
  ]
  return [...new Set(refs)]
}

/** Short-linked terms named in the prose while the message carries no link to their path anywhere; `"Name (path)"` entries. */
export function slackUnlinkedGoTerms(text: string): string[] {
  const prose = slackProseMasked(text)
  const lower = text.toLowerCase()
  const out: string[] = []
  for (const { name, go } of SLACK_GO_LINK_TERMS) {
    if (lower.includes(go.toLowerCase())) continue
    const re = new RegExp(`(?<![\\w@/])${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?!\\w)`, 'i')
    if (re.test(prose)) out.push(`${name} (${go})`)
  }
  return out
}

export type SlackPeopleLinksFinding = { people: string[]; prRefs: string[]; goTerms: string[] }

/** Judge Slack message texts against rule 15; merged across texts, deduplicated. */
export function checkSlackPeopleLinks(texts: string[], names: string[] = knownPeopleNames()): SlackPeopleLinksFinding {
  const people: string[] = []
  const prRefs: string[] = []
  const goTerms: string[] = []
  for (const text of texts) {
    for (const n of slackUnlinkedPeople(text, names)) if (!people.includes(n)) people.push(n)
    for (const r of slackUnlinkedPrRefs(text)) if (!prRefs.includes(r)) prRefs.push(r)
    for (const g of slackUnlinkedGoTerms(text)) if (!goTerms.includes(g)) goTerms.push(g)
  }
  return { people, prRefs, goTerms }
}

// ── Rule 4 helpers: Slack form of the bot prefix ─────────────────────

/** How a Slack message text opens: the same-line prefix (`'ok'`), the retired standalone prefix line, or no prefix. */
export type SlackBotPrefixShape = 'ok' | 'standalone' | 'missing'

export function slackBotPrefixShape(text: string): SlackBotPrefixShape {
  if (SLACK_BOT_PREFIX_RE.test(text)) return 'ok'
  if (SLACK_BOT_PREFIX_STANDALONE_RE.test(text)) return 'standalone'
  return 'missing'
}

/** The text with the prefix opening its first sentence, or null when it already does.
 *
 * A standalone prefix line is folded onto the sentence below it and a prefix
 * glued to the first word gets its space, so the result never stacks a second
 * prefix. An empty message becomes the bare prefix.
 */
export function injectSlackBotPrefix(text: string): string | null {
  if (slackBotPrefixShape(text) === 'ok') return null
  const body = text.replace(SLACK_BOT_PREFIX_LEAD_RE, '')
  const prefixed = body ? `${SLACK_BOT_PREFIX} ${body}` : SLACK_BOT_PREFIX
  return prefixed === text ? null : prefixed
}

/** The prefix shape of an inline Slack payload: text right after a quote boundary, so a mention later in the command never counts. */
export function slackInlineBotPrefixShape(cmd: string): SlackBotPrefixShape {
  if (new RegExp(`(?:^|["'])[ \\t]*${SLACK_BOT_PREFIX_ESCAPED}[ \\t]+(?!\\\\[nr])\\S`).test(cmd)) return 'ok'
  if (new RegExp(`(?:^|["'])[ \\t]*${SLACK_BOT_PREFIX_ESCAPED}[ \\t]*(?:\\\\n|\\r?\\n)`).test(cmd)) return 'standalone'
  return 'missing'
}

// ── Rule 4 helpers: GitHub form of the bot prefix ────────────────────

/** How a GitHub body opens: the 20px user-attachment image or the 🤖 fallback with the first word on the same line, a prefix alone on its line, a prefix above a heading, or (null) no prefix. */
export type GithubBotPrefixForm = 'image' | 'fallback' | 'standalone' | 'before-heading'

/** Which GitHub prefix form opens `text`.
 *
 * The prefix opens the first paragraph with the first word on the same line.
 * That paragraph is the first non-blank line, or (PR
 * bodies) the first non-blank line after a leading markdown heading.
 * `'standalone'` means the prefix was found but nothing follows it on its
 * line — the retired shape. `'before-heading'` means the next non-blank line
 * after such a standalone prefix is a heading: GitHub renders the robot inline
 * on that heading, so the caller rejects it with its own reason. A literal
 * `\n` counts as a line break like it does for Slack, because
 * `--body "$(printf '…\n…')"` carries the real newline at run time.
 */
export function githubBotPrefixForm(text: string): GithubBotPrefixForm | null {
  const lines = text.split(BODY_LINE_BREAK_RE)
  const nextNonBlank = (from: number): number => {
    let index = from
    while (index < lines.length && lines[index].trim() === '') index++
    return index
  }
  let index = nextNonBlank(0)
  if (index < lines.length && MARKDOWN_HEADING_LINE_RE.test(lines[index])) index = nextNonBlank(index + 1)
  if (index >= lines.length) return null
  const line = lines[index].trim()
  const image = GITHUB_BOT_PREFIX_IMG_LEAD_RE.exec(line)
  const fallback = GITHUB_BOT_PREFIX_FALLBACK_LEAD_RE.exec(line)
  let form: 'image' | 'fallback'
  let rest: string
  if (image && GITHUB_BOT_PREFIX_IMG_SRC_RE.test(image[1]) && GITHUB_BOT_PREFIX_IMG_HEIGHT_RE.test(image[1])) {
    form = 'image'; rest = image[2]
  } else if (fallback) {
    form = 'fallback'; rest = fallback[1]
  } else {
    return null
  }
  if (rest.trim() !== '') return form
  const following = nextNonBlank(index + 1)
  if (following < lines.length && MARKDOWN_HEADING_LINE_RE.test(lines[following])) return 'before-heading'
  return 'standalone'
}

/** `pr comment`, `issue create`, `api`, … when `gh` starts a shell command and the subcommand writes text. */
export function githubBashWriteSubcommand(cmd: string): string | null {
  const match = bashCommandStartMatch(cmd, GITHUB_BASH_WRITE_AT_COMMAND_START)
  if (!match) return null
  return match[3] ? 'api' : `${match[1]} ${match[2]}`.toLowerCase()
}

/** Does the gated `gh` command carry a body at all? `--approve` alone or `--fill` carry none. */
export function githubBashHasBody(cmd: string, subcmd: string): boolean {
  return (subcmd === 'api' ? GITHUB_API_BODY_FLAG_RE : GITHUB_PR_BODY_FLAG_RE).test(cmd)
}

/** Files the body is read from: `--body-file`, `-F <path>`, `body=@path`, `--input`. `-` (stdin) is skipped. */
export function githubBodyFiles(cmd: string, subcmd: string): Array<{ path: string; json: boolean }> {
  const files: Array<{ path: string; json: boolean }> = []
  if (subcmd === 'api') {
    for (const m of cmd.matchAll(GITHUB_API_BODY_FILE_RE)) files.push({ path: m[1], json: false })
    for (const m of cmd.matchAll(GITHUB_API_INPUT_FILE_RE)) files.push({ path: m[1], json: true })
  } else {
    for (const m of cmd.matchAll(GITHUB_PR_BODY_FILE_RE)) files.push({ path: m[1], json: false })
  }
  return files.filter((file) => file.path !== '-')
}

/** Bodies of every heredoc in the command, for `--body-file -` and `--input -` fed by `<<'EOF'`. */
export function heredocBodies(cmd: string): string[] {
  return [...cmd.matchAll(BASH_HEREDOC_RE)].map((m) => m[2])
}

/** Paths read through `$(cat /path)` substitutions. */
export function catSubstitutionPaths(cmd: string): string[] {
  return [...cmd.matchAll(/\$\(\s*cat\s+['"]?([^'")\s]+)['"]?\s*\)/g)].map((m) => m[1])
}

/** The prefix form of an inline body: text right after a quote boundary or `body=`, so a mention later in the command never counts.
 *
 * The body is read from the boundary to the matching close quote (to the end
 * of the physical line when unquoted). Inside a double-quoted shell word the
 * img attributes arrive as `\"…\"`; those escapes are dropped so the tag is
 * read as the shell passes it. A leading markdown heading (plus blank lines)
 * before the prefix travels along so the PR-body placement rule applies to
 * inline bodies too.
 */
export function githubInlineBodyPrefixForm(cmd: string): GithubBotPrefixForm | null {
  const bodyStartRe = /(?:^|(["'])|\bbody=)[ \t]*((?:(?:\\n|\r?\n)[ \t]*)*(?: {0,3}#{1,6}(?:[ \t]+[^\n]*?|)(?:\\n|\r?\n)(?:[ \t]*(?:\\n|\r?\n))*)?)[ \t]*(?=<img\b|\u{1F916})/gu
  for (const m of cmd.matchAll(bodyStartRe)) {
    const quote = m[1]
    const from = (m.index ?? 0) + m[0].length
    let end = from
    if (quote) {
      while (end < cmd.length && cmd[end] !== quote) end += quote === '"' && cmd[end] === '\\' ? 2 : 1
    } else {
      end = cmd.indexOf('\n', from)
      if (end < 0) end = cmd.length
    }
    const form = githubBotPrefixForm(`${m[2]}${cmd.slice(from, end).replace(/\\(["'])/g, '$1')}`)
    if (form) return form
  }
  return null
}

// ── Rule 16 helpers: code review wording in gh pr review / comment (warn only) ─
// system/prompts/external-replies.md Rule 10: a review never mentions bot comments, approvals, CI, or anything the PR body
// already says. These strings are the deterministic tell for each clause.
export const GITHUB_REVIEW_WORDING_TERMS: ReadonlyArray<{ term: string; re: RegExp; clause: string }> = [
  { term: 'as the body says', re: /\bas the body says\b/i, clause: 'never restate the PR body' },
  { term: 'needs approval', re: /\bneeds approvals?\b/i, clause: 'approvals are not part of a code review' },
  { term: 'owner teams', re: /\bowner teams?\b/i, clause: 'owner teams are not part of a code review' },
]

/** Review-wording terms present in any of the texts, in canonical order, each once. Case-insensitive. */
export function githubReviewWordingHits(texts: string[]): string[] {
  const joined = texts.join('\n')
  return GITHUB_REVIEW_WORDING_TERMS.filter(({ re }) => re.test(joined)).map(({ term }) => term)
}

// ── Rule 9 helpers: browser automation safety ─────────────────────────
// Exported for focused tests and for one canonical profile path in guidance.

export const SANCTIONED_BROWSER_PROFILE = `${HOME}/.cache/chrome-devtools-mcp/chrome-profile`

const APPLESCRIPT_AT_COMMAND_START = /^(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)*)osascript\b/i
const APPLESCRIPT_DAILY_CHROME_RE = /tell\s+application\s+(?:\\?["']Google Chrome\\?["']|id\s+\\?["']com\.google\.Chrome\\?["'])/i
// The Windows names matter on their own: `chrome.exe --version` starts the browser, because
// Windows Chrome has no such flag.
const DIRECT_CHROME_AT_COMMAND_START = /^(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)*)(?:"[^"]*MacOS\/(?:Google Chrome|Chromium)"|'[^']*MacOS\/(?:Google Chrome|Chromium)'|"[^"]*(?:chrome|brave|msedge)\.exe"|'[^']*(?:chrome|brave|msedge)\.exe'|\S*(?:google-chrome|chromium|MacOS\/(?:Google\\ Chrome|Chromium)|(?:chrome|brave|msedge)\.exe)\S*)(?=\s|$)/i
const OPEN_CHROME_AT_COMMAND_START = /^(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)*)open\s+-[^\n;&|]*\b(?:Google Chrome|Chromium)\b/i
const CHROME_DEVTOOLS_AT_COMMAND_START = /^(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)*)(?:(?:npx|bunx)\s+(?:(?:-y|--yes)\s+)?)?chrome-devtools(?:-mcp)?(?:@\S+)?(?:\s+start)?\b/i
const NODE_AT_COMMAND_START = /^(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)*)(?:node|bun|deno)\b/i
const NODE_BROWSER_LAUNCH_RE = /(?:\b(?:puppeteer|chromium)\s*\.\s*launch\s*\(|chrome-devtools(?:\.js)?["']?\s+start\b)/i

function isBrowserLaunchCommand(cmd: string): boolean {
  if (bashCommandStartMatch(cmd, DIRECT_CHROME_AT_COMMAND_START)) return true
  if (bashCommandStartMatch(cmd, OPEN_CHROME_AT_COMMAND_START)) return true
  if (bashCommandStartMatch(cmd, CHROME_DEVTOOLS_AT_COMMAND_START)) return true
  return !!bashCommandStartMatch(cmd, NODE_AT_COMMAND_START) && NODE_BROWSER_LAUNCH_RE.test(cmd)
}

function usesSanctionedBrowserProfile(cmd: string): boolean {
  const expanded = cmd
    .replaceAll('~/.cache/chrome-devtools-mcp/chrome-profile', SANCTIONED_BROWSER_PROFILE)
    .replace(/\$\{?HOME\}?\/\.cache\/chrome-devtools-mcp\/chrome-profile/g, SANCTIONED_BROWSER_PROFILE)
  return expanded.includes(SANCTIONED_BROWSER_PROFILE)
}

function hasExplicitHeadlessMode(cmd: string): boolean {
  return /--headless(?:(?:=|\s+)(?:true|new|shell))?(?=\s|$)/i.test(cmd) ||
    /\bheadless\s*:\s*(?:true|["'](?:new|shell)["'])/i.test(cmd)
}

/** Return the deterministic block reason for a shell command, or null. */
export function browserAutomationBlockReason(cmd: string): string | null {
  if (bashCommandStartMatch(cmd, APPLESCRIPT_AT_COMMAND_START) && APPLESCRIPT_DAILY_CHROME_RE.test(cmd)) {
    return 'AppleScript control of the owner\'s daily Chrome is prohibited.'
  }
  if (!isBrowserLaunchCommand(cmd)) return null
  if (!usesSanctionedBrowserProfile(cmd)) {
    return `Browser launches must use the sanctioned persistent profile: ${SANCTIONED_BROWSER_PROFILE}.`
  }
  if (hasExplicitHeadlessMode(cmd)) return null
  if (/\bSWITCHBOARD_BROWSER_OK=1\b/.test(cmd)) return null
  return 'A visible browser launch requires SWITCHBOARD_BROWSER_OK=1 after CoS coordinates the one-time sign-in with the owner.'
}

// Rule 9b: open pages and images without stealing focus from what the owner is
// doing. Chrome raises itself whenever another app hands it a page, so
// `open -g` is no defence: Chrome comes frontmost within half a second of it. Preview and Quick Look come forward too. An
// agent's `open` of a page or image (an .html/.htm or image file, a file://
// link, or an http(s) URL), with or without -g, and `qlmanage -p`, are
// rejected with the fix: `image_compare.py open`, which puts the page in an
// unselected cmux browser tab. A deliberate foreground open (the owner asked to
// bring it up) carries SWITCHBOARD_FOCUS_OK=1 as a leading assignment. A target held
// in a shell variable or $(…) cannot be judged here and passes
// (image_compare.py is the path that opens computed pages).
const OPEN_AT_COMMAND_START = /^(?:(?:then|do|else|\{|\()\s*)?(?:(?:command|sudo|env|time|nohup|xargs)\s+(?:-\S+\s+)*)*(?:\/usr\/bin\/)?open(?=[\s;&|]|$)((?:[ \t]+(?:"[^"]*"|'[^']*'|[^\s;&|"']+))*)/
const QUICKLOOK_AT_COMMAND_START = /^(?:(?:then|do|else|\{|\()\s*)?(?:(?:command|sudo|env|time|nohup|xargs)\s+(?:-\S+\s+)*)*(?:\/usr\/bin\/)?qlmanage(?:[ \t]+-\S+)*[ \t]+-p\b/
// The Linux and Windows openers: xdg-open, gio open,
// wslview, and the Git Bash and cmd `start` hand a page to the default browser,
// which on X11 and Windows raises its window the same way.
const OTHER_OPENER_AT_COMMAND_START = /^(?:(?:then|do|else|\{|\()\s*)?(?:(?:command|sudo|env|time|nohup|xargs)\s+(?:-\S+\s+)*)*(xdg-open|gio[ \t]+open|wslview|cygstart|start|explorer(?:\.exe)?|cmd(?:\.exe)?[ \t]+\/{1,2}c[ \t]+start)(?=[\s;&|]|$)((?:[ \t]+(?:"[^"]*"|'[^']*'|[^\s;&|"']+))*)/i
const OPEN_PAGE_TARGET_RE = /^(?:file:\/\/|https?:\/\/)|\.(?:html?|png|jpe?g|gif|webp|heic|tiff?|bmp|svg)(?:[?#].*)?$/i

/** Words of an `open` argument string, quotes removed (not a full shell parser). */
function openArgWords(args: string): string[] {
  const words: string[] = []
  for (const m of args.matchAll(/"([^"]*)"|'([^']*)'|([^\s"']+)/g)) words.push(m[1] ?? m[2] ?? m[3] ?? '')
  return words
}

/** Reason to reject a focus-stealing `open` of a page or image, or null when it is fine. */
export function foregroundPageOpenReason(cmd: string): string | null {
  const ql = bashCommandStartMatch(cmd, QUICKLOOK_AT_COMMAND_START)
  if (ql && !/(?:^|\s)SWITCHBOARD_FOCUS_OK=1(?=\s)/.test(ql[0])) {
    return '`qlmanage -p` opens a Quick Look panel in front of the owner\'s app.'
  }
  const other = bashCommandStartMatch(cmd, OTHER_OPENER_AT_COMMAND_START)
  if (other && !/(?:^|\s)SWITCHBOARD_FOCUS_OK=1(?=\s)/.test(other[0])) {
    // Only the opener's target counts: the first word that is not an option (`-x`,
    // `/b`) or `start`'s empty "" title. `start code index.html` opens an editor,
    // not a browser, so a page later in the line is an argument, not the target.
    const target = openArgWords(other[2] ?? '').find((word) => word && !word.startsWith('-') && !/^\/[a-z]+$/i.test(word)) ?? ''
    if (OPEN_PAGE_TARGET_RE.test(target)) return `\`${other[1].replace(/\s+/g, ' ')} ${target}\` hands the file to its default app, which raises its window and steals focus.`
  }
  const match = bashCommandStartMatch(cmd, OPEN_AT_COMMAND_START)
  if (!match) return null
  if (/(?:^|\s)SWITCHBOARD_FOCUS_OK=1(?=\s)/.test(match[0])) return null
  const words = openArgWords(match[1] ?? '')
  let background = false
  let page: string | null = null
  for (let i = 0; i < words.length; i++) {
    const word = words[i]
    if (word === '--args') break
    if (word === '--background' || /^-[a-zA-Z]*g[a-zA-Z]*$/.test(word)) { background = true; continue }
    if (/^-[abu]$/.test(word) || word === '--env' || word === '--stdin' || word === '--stdout' || word === '--stderr') { i++; continue }
    if (word.startsWith('-')) continue
    if (!page && OPEN_PAGE_TARGET_RE.test(word)) page = word
  }
  if (!page) return null
  return background
    ? `\`open -g ${page}\` still steals focus: Chrome (and Preview) raise themselves when handed a page.`
    : `\`open ${page}\` brings the app to the front and steals focus from the owner.`
}

const GMAIL_SEND_TOOLS = new Set([
  'mcp__gmail__send_message',
  'mcp__gmail__send_tool',
])
// `switchboard mail <subcommand>`: only a `send…` subcommand token is a
// send. Global flags (`--timeout 30`) may sit between the tool name and the
// subcommand, as with google-drive. The word "send" anywhere later in the
// command (a search query, a `| rg send`, a filename, a --format value) is
// argument text and never a send. The stock `switchboard mail` ships no send
// subcommand. switchboard-shim has
// `send`, which this catches; system/scripts/gmail_send_guard.py gates it
// in the connector and refuses outside an attended Claude Code CoS or Launchpad.
const GMAIL_BASH_SEND_AT_COMMAND_START =
  /^switchboard\s+mail\s+(?:(?:--timeout(?:=|\s+)\S+|-{1,2}[a-z][\w-]*)\s+)*(send(?:-[a-z]+)*)(?![\w-])/i

/** `send`, `send-message`, … when `switchboard mail` starts a shell command and its subcommand sends; null for reads, drafts, and later "send" text. */
export function gmailBashSendSubcommand(cmd: string): string | null {
  const m = bashCommandStartMatch(cmd, GMAIL_BASH_SEND_AT_COMMAND_START)
  return m ? m[1].toLowerCase() : null
}

const CAL_CREATE_TOOLS = new Set([
  'mcp__gcal__events_create',
  'mcp__gcal__create_event',
])

// ── Rule 8 helpers: CoS delivery confirm-gate ─────────────────────────
// Exported for tests.

const FILE_WRITE_TOOLS = new Set(['edit_file', 'create_file'])

// `switchboard drive [global flags] <subcommand>`: only the token
// directly after `drive` (past `--timeout N` / bare flags) decides
// whether the call writes. Prose later in the command is never consulted.
const GDRIVE_SUBCOMMAND_RE = /\bswitchboard\s+drive\b(?:\s+(?:--timeout(?:=|\s+)\S+|-{1,2}[a-z][\w-]*))*\s+([a-z][a-z0-9-]*)(?![\w-])([^;&|\n]*)/gi
// Hyphen-separated segments of a subcommand that mutate remote files
// (docs-write, docs-v2-update, insert-image, upsert, sharing, delete-file,
// copy-file, comment-reply, sheets-formatting, sheets-charts, …). Read-ish
// verbs (get, list, read, search, export, inspect, describe, activity) are
// intentionally absent. `tool` covers the omnibus sheets-tool / docs-tool /
// comments-tool, which mutate unless their operation is a read.
const GDRIVE_WRITE_SEGMENT_RE = /(?:^|-)(?:create|update|upload|upsert|delete|copy|move|insert|replace|append|batch-update|reply|share|sharing|format|formatting|revise|write|edit|charts|named-ranges|tool|put|add)(?:-|$)/i
// Omnibus subcommands (sheets-tool, docs-tool, comments-tool, sharing,
// sheets-named-ranges) read or write depending on `--operation`;
// `--operation get_values` / `"operation": "list"` makes the call a read.
const GDRIVE_OMNIBUS_RE = /(?:^|-)(?:tool|sharing|named-ranges)$/i
const GDRIVE_READ_OPERATION_RE = /(?:--operation(?:=|\s+)|["']?operation["']?\s*:\s*)["']?(?:get|list|read)(?:[_-]\w+)?\b/i

// The sanctioned CoS/Router handoff. Its prompt argument is data, not a
// command, so the CoS delivery gate does not read it. Recognised only at a
// shell command start, never inside quoted text.
const DISPATCH_SCRIPT_AT_COMMAND_START =
  /^(?:(?:env\s+)?(?:\w+=(?:"(?:\\.|[^"])*"|'[^']*'|\S+)\s+)*)?(?:bash\s+)?(?:"[^"]*\/|'[^']*\/|\S*\/)?(?:router-dispatch|route-to-workspace)\.sh["']?(?=\s|$)/

// ── Rule 10: dispatch guard ─────────────────────────────────────────────
// Dispatch-origin sessions (CoS, Launchpad, Router N) must hand work off via
// router-dispatch.sh / route-to-workspace.sh, never by minting threads or
// messaging threads the cmux session store does not show as live.
const DISPATCH_ORIGIN_TITLE_RE = /🧭|\bcos\b|chief.*staff|🚀|\blaunchpad\b|🔀|\brouter\b/i

/**
 * Origin identity = title pattern minus worker tabs. dispatch.py titles a worker
 * tab after its workstream, so a start-day worker titled "Chief Of Staff 1"
 * matches COS_TITLE_RE and the CoS write gate would block the
 * worker's own file writes. The cmux description "switchboard worker <task-id>" is the
 * dispatcher's mark and wins over the title.
 */
export function isOriginWorkspace(titleRe: RegExp, ws: { title: string; description?: string }): boolean {
  if (isWorkerWorkspace(ws)) return false
  return titleRe.test(ws.title)
}

/** Test seam: SWITCHBOARD_GUARDRAILS_WS_TITLE_OVERRIDE plays the title, SWITCHBOARD_GUARDRAILS_WS_DESCRIPTION_OVERRIDE the description. */
function overrideWorkspace(title: string): { title: string; description: string } {
  return { title, description: process.env.SWITCHBOARD_GUARDRAILS_WS_DESCRIPTION_OVERRIDE ?? '' }
}
const ROUTER_TITLE_RE = /🔀|\brouter\s*\d*\b/i
const DISPATCH_TOOLS = new Set(['create_thread', 'send_thread_message'])
const THREAD_ID_RE = /T-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i
const DEFAULT_CMUX_SESSION_STORE = process.env.SWITCHBOARD_CMUX_SESSION_JSON ?? process.env.CC_CMUX_SESSION_JSON ?? `${HOME}/Library/Application Support/cmux/session-com.cmuxterm.app.json`

export interface LiveThread {
  workspaceId: string
  title: string
  state: string
}

// ── Rule 11: orchestrator wait guard ────────────────────────────────────
// Dispatch-origin sessions dispatch and return. Blocking on a worker
// (wait_for_threads, long `sleep`) or polling the same thread's status in a
// loop is supervisory work that belongs to the ledger/digest path.
const WAIT_TOOL = 'wait_for_threads'
const POLL_TOOLS = new Set(['get_thread_status', 'read_thread'])
const DEFAULT_POLL_WINDOW_MS = 15 * 60_000
const DEFAULT_POLL_LIMIT = 2
const BLOCKING_SLEEP_MIN_S = 30
const SLEEP_CMD_RE = /^(\d+(?:\.\d+)?)([smh]?)\b/

export const WAIT_GUARD_GUIDANCE =
  'CoS/Router sessions dispatch and return; they do not block on or poll delegated work. '
  + 'Worker results arrive through report-to-cos.sh ([ROUTER REPORT]/[WORKSPACE REPORT] callbacks), the digest, '
  + 'and system/state/running-tasks.json. End this turn after the route acknowledgement; '
  + 'if the owner explicitly asked CoS to wait, say so and confirm the prompt.'

export type WaitGuardVerdict =
  | { kind: 'allow' }
  | { kind: 'confirm'; reason: string }
  | { kind: 'reject'; reason: string }

/** Per-thread poll timestamps (ms). Pruned to the window on every check. */
export type PollState = Map<string, number[]>

export function threadIdsFromWaitInput(input: Record<string, unknown>): string[] {
  const raw: unknown[] = []
  for (const key of ['thread', 'threadID', 'threadId', 'threads']) {
    const value = input[key]
    if (Array.isArray(value)) raw.push(...value)
    else if (value !== undefined) raw.push(value)
  }
  const ids = raw.map(v => String(v).match(THREAD_ID_RE)?.[0].toLowerCase() ?? '').filter(Boolean)
  return [...new Set(ids)]
}

/**
 * Pure decision for Rule 11. Only called for dispatch-origin sessions.
 * Mutates `polls` to record poll tool calls (a rejected poll is not recorded,
 * so the window reflects reads that actually happened).
 */
export function waitGuardDecision(
  tool: string,
  input: Record<string, unknown>,
  polls: PollState,
  now: number,
  opts: { windowMs?: number; limit?: number } = {},
): WaitGuardVerdict {
  if (tool === WAIT_TOOL) {
    const threads = threadIdsFromWaitInput(input)
    const timeout = Number(input.timeout_seconds ?? 300)
    return {
      kind: 'confirm',
      reason: `wait_for_threads(${threads.join(', ') || 'no thread id'}, ${Number.isFinite(timeout) ? timeout : 300}s) blocks this orchestrator session on delegated work.`,
    }
  }
  if (!POLL_TOOLS.has(tool)) return { kind: 'allow' }
  const windowMs = opts.windowMs ?? DEFAULT_POLL_WINDOW_MS
  const limit = opts.limit ?? DEFAULT_POLL_LIMIT
  const ids = threadIdsFromWaitInput(input)
  if (ids.length === 0) return { kind: 'allow' }
  const id = ids[0]
  const recent = (polls.get(id) ?? []).filter(ts => now - ts < windowMs)
  if (recent.length >= limit) {
    polls.set(id, recent)
    return {
      kind: 'reject',
      reason: `${tool} on ${id} is status read #${recent.length + 1} within ${Math.round(windowMs / 60_000)} min (limit ${limit}) — supervisory polling of delegated work.`,
    }
  }
  recent.push(now)
  polls.set(id, recent)
  return { kind: 'allow' }
}

/** Seconds a leading `sleep` would block for, or null when the command does not start with sleep. */
export function leadingSleepSeconds(cmd: string): number | null {
  const stripped = cmd.replace(/^\s*(?:sudo\s+|env\s+\w+=\S+\s+)*/, '').trim()
  if (!stripped.startsWith('sleep ')) return null
  const m = stripped.slice('sleep '.length).trim().match(SLEEP_CMD_RE)
  if (!m) return null
  const n = Number(m[1])
  const unit = m[2]
  return unit === 'm' ? n * 60 : unit === 'h' ? n * 3600 : n
}

export function blockingSleepReason(cmd: string): string | null {
  const seconds = leadingSleepSeconds(cmd)
  if (seconds === null || seconds < BLOCKING_SLEEP_MIN_S) return null
  return `bash \`sleep ${seconds}s\` blocks this orchestrator session (limit ${BLOCKING_SLEEP_MIN_S}s).`
}

/**
 * Map of thread id → live cmux workspace, read from the cmux session store
 * (same source as workspace-session-state.py). Returns null when the store is
 * unreadable so callers can fail open — an unreadable store is not evidence
 * that a thread is dead.
 */
export function liveThreadsFromSessionStore(path = process.env.SWITCHBOARD_GUARDRAILS_SESSION_STORE ?? DEFAULT_CMUX_SESSION_STORE): Map<string, LiveThread> | null {
  let raw: unknown
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return null
  }
  const out = new Map<string, LiveThread>()
  const windows = (raw as { windows?: unknown[] })?.windows
  if (!Array.isArray(windows)) return out
  for (const win of windows) {
    const workspaces = (win as { tabManager?: { workspaces?: unknown[] } })?.tabManager?.workspaces
    if (!Array.isArray(workspaces)) continue
    for (const ws of workspaces as Array<Record<string, unknown>>) {
      const title = String(ws.customTitle ?? ws.title ?? '')
      const workspaceId = String(ws.workspaceId ?? '')
      const statusEntries = Array.isArray(ws.statusEntries) ? (ws.statusEntries as Array<Record<string, unknown>>) : []
      const ampStatus = statusEntries.find(entry => entry?.key === 'amp')
      const state = String(ampStatus?.value ?? '')
      const panels = Array.isArray(ws.panels) ? (ws.panels as Array<Record<string, unknown>>) : []
      for (const panel of panels) {
        const agent = (panel?.terminal as { agent?: { kind?: string; sessionId?: string } } | undefined)?.agent
        if (agent?.kind === 'amp' && typeof agent.sessionId === 'string' && agent.sessionId) {
          out.set(agent.sessionId, { workspaceId, title, state })
        }
      }
    }
  }
  return out
}

/** Extract the target thread id from a send_thread_message input (URL or bare id). */
export function threadIdFromInput(input: Record<string, unknown>): string {
  const match = String(input.thread ?? input.threadID ?? input.threadId ?? '').match(THREAD_ID_RE)
  return match ? match[0] : ''
}

/**
 * Decide whether a dispatch-origin session may run a thread tool.
 * Returns a reason string to block, or null to allow. Pure so it can be
 * tested without the plugin runtime.
 */
export function dispatchGuardBlockReason(
  tool: string,
  input: Record<string, unknown>,
  live: Map<string, LiveThread> | null,
): string | null {
  // create_thread is allowed: the native Amp path
  // must stay supported, not blocked to force router-dispatch.sh. task-liveness
  // registers the child in the ledger from the tool result (kind: native-thread,
  // status routing, thread_id = child), so identity and visibility come from
  // transparent registration, not from a block here.
  if (tool !== 'send_thread_message') return null
  const threadId = threadIdFromInput(input)
  if (!threadId) return null
  if (live === null) return null // unreadable store: fail open (not evidence of death)
  const target = live.get(threadId)
  if (!target) {
    return `Target thread ${threadId} is not live in any cmux workspace, so the message would be accepted by the server but no agent would act on it. Verify the workspace with \`cmux rpc workspace.list\`; dispatch new work with \`bash system/scripts/router-dispatch.sh\`; re-dispatch a stale task with \`router-dispatch.sh --task-id <rt-id>\`.`
  }
  if (ROUTER_TITLE_RE.test(target.title)) {
    return `Target thread ${threadId} belongs to Router workspace "${target.title}". Routers are ephemeral and do not accept follow-up messages; dispatch through \`bash system/scripts/router-dispatch.sh\` so a fresh Router and ledger task pick it up.`
  }
  return null
}

/** Normalize a write-target path: strip quotes, expand ~ and $HOME/$TMPDIR. */
export function normalizeWritePath(raw: string): string {
  let p = raw.replace(/^["']|["']$/g, '')
  if (p.startsWith('~/')) p = `${HOME}/${p.slice(2)}`
  else if (p === '~') p = HOME
  p = p.replace(/^\$\{?HOME\}?/, HOME)
  p = p.replace(/^\$\{?TMPDIR\}?/, process.env.TMPDIR ?? '/tmp/')
  // Windows: one spelling (`C:/x`) for `C:\x` and Git Bash `/c/x`, so the repo prefix matches.
  return slashPath(p)
}

/** True when a path sits under the session's own `$TMPDIR` scratch space. A
 * TMPDIR that is a top-level folder, home, the repo, a parent of either, or a
 * folder inside the repo never counts, so a stray setting cannot open real
 * delivery paths. */
function isUnderTmpdir(p: string): boolean {
  const tmp = (process.env.TMPDIR ?? '').replace(/\/+$/, '')
  if (!tmp.startsWith('/') || tmp.split('/').length < 3 || tmp.includes('..')) return false
  if (tmp.startsWith(`${SWITCHBOARD_REPO}/`)) return false
  for (const guarded of [HOME, SWITCHBOARD_REPO]) if (guarded === tmp || guarded.startsWith(`${tmp}/`)) return false
  return p.startsWith(`${tmp}/`)
}

/**
 * True when a path is legitimate CoS orchestration state — the morning flow
 * writes these (today-plan, daily note, workstream config scaffolds, launch
 * state, outputs) and must never hit the gate (alarm fatigue kills confirm
 * gates). Everything else in the CoS session is a delivery signature.
 */
export function isCosAllowlistedPath(raw: string): boolean {
  const p = normalizeWritePath(raw)
  // fd redirects / device sinks
  if (p === '/dev/null' || p === '/dev/stderr' || p === '/dev/stdout' || /^&\d?$/.test(p) || p === '-') return true
  // temp space, including the Claude session scratchpad
  // (/private/tmp/claude-<uid>/<project>/<session>/scratchpad)
  if (/^\/(private\/)?(tmp|var\/folders)\//.test(p) || p === '/tmp') return true
  if (isUnderTmpdir(p)) return true
  const lower = p.toLowerCase()
  if (windowsTempRoots().some(t => lower.startsWith(`${t.toLowerCase()}/`))) return true
  // start-of-day completion state lives outside the switchboard repo
  if (p === `${HOME}/.config/start-of-day` || p.startsWith(`${HOME}/.config/start-of-day/`)) return true
  // unresolved-but-temp-looking shell vars (e.g. "$tmpfile" from mktemp) are
  // NOT auto-allowed — the gate is confirm-style, so a false fire is a click,
  // not a break. Only literal paths below are allowed.
  if (!isAbsPath(p)) {
    // Relative path: resolve against the switchboard repo (CoS runs there). A
    // relative path pointing at allowlisted repo state is allowed. The repo is
    // `C:/…` on Windows; reading it as relative would recurse forever.
    const inRepo = `${SWITCHBOARD_REPO}/${p}`
    return isAbsPath(inRepo) && isCosAllowlistedPath(inRepo)
  }
  if (!p.startsWith(`${SWITCHBOARD_REPO}/`)) return false
  const rel = p.slice(SWITCHBOARD_REPO.length + 1)
  if (rel.startsWith('system/')) return true
  if (rel.startsWith('notes/daily/')) return true
  if (rel.startsWith('outputs/')) return true
  if (rel === 'workstreams/README.md') return true
  if (/^workstreams\/[a-z0-9-]+\/config\.yaml$/i.test(rel)) return true
  if (/^workstreams\/[a-z0-9-]+\/inbox\//i.test(rel)) return true
  return false
}

// A heredoc body is stdin for the command on the `<<TAG` line. When that
// command is a shell (`bash <<'EOF'`, `cat <<EOF | sh`, `ssh host <<EOF`) the
// body is more shell and keeps being scanned; otherwise it is data. CoS hands
// prompts to router-dispatch.sh through `python3 - <<'PY'` + subprocess.run,
// and prose such as `>160 locations` or `>=90%` inside that heredoc is not a
// shell redirect.
const HEREDOC_LINE_SHELL_RE = /(?:^|[\s;|&(])(?:ba|z|da|k|fi)?sh\b|(?:^|[\s;|&(])(?:ssh|eval|source|\.)(?=\s)/

/** The command with every data heredoc body blanked (the `<<TAG` line and
 * the terminator stay), so redirect and subcommand scans see shell text only. */
export function shellTextWithoutDataHeredocs(cmd: string): string {
  return cmd.replace(BASH_HEREDOC_RE, (whole, _tag: string, body: string, offset: number) => {
    const lineStart = cmd.lastIndexOf('\n', offset) + 1
    const heredocLine = cmd.slice(lineStart, cmd.indexOf('\n', offset))
    if (HEREDOC_LINE_SHELL_RE.test(heredocLine)) return whole
    const bodyStart = whole.indexOf('\n') + 1
    return whole.slice(0, bodyStart) + whole.slice(bodyStart + body.length)
  })
}

/**
 * Extract file-write targets from a bash command: `> path`, `>> path`, and
 * `tee [-a] path...`. Heredoc writes (`cat > path <<EOF`) are caught by the
 * redirect form. Skips fd duplication (`>&2`, `2>&1`) and numeric-fd
 * redirects of stderr (`2> path` IS a write and is kept). Heredoc bodies fed
 * to a non-shell command are data and are not scanned.
 */
export function bashWriteTargets(rawCmd: string): string[] {
  const cmd = shellTextWithoutDataHeredocs(rawCmd)
  const targets: string[] = []
  // Redirects: optional fd digit, > or >>, then the target token.
  const redirectRe = /(?:^|[\s;|&(])(?:\d?)>{1,2}\s*("(?:\\.|[^"])*"|'[^']*'|[^\s;|&)<>]+)/g
  for (const m of cmd.matchAll(redirectRe)) {
    const t = m[1]
    if (t.startsWith('&')) continue // fd duplication, not a file
    targets.push(t)
  }
  // tee [-a|--append] target [target...]
  const teeRe = /\btee\s+((?:-a\s+|--append\s+)?)((?:"(?:\\.|[^"])*"|'[^']*'|[^\s;|&)<>]+)(?:\s+(?:"(?:\\.|[^"])*"|'[^']*'|[^\s;|&)<>-][^\s;|&)<>]*))*)/g
  for (const m of cmd.matchAll(teeRe)) {
    for (const tok of m[2].matchAll(/"(?:\\.|[^"])*"|'[^']*'|\S+/g)) {
      if (!tok[0].startsWith('-')) targets.push(tok[0])
    }
  }
  return targets
}

/** Non-allowlisted write targets in a bash command (empty = nothing gated). */
export function gatedBashWriteTargets(cmd: string): string[] {
  // Read `$sd/q.json` the way the shell will when the same command set `sd`
  // (`sd=<session scratchpad>; … > $sd/q.json`). Only names assigned one value resolve; a single-quoted target
  // stays literal.
  const opts = { home: HOME, repo: SWITCHBOARD_REPO }
  const vars = assignedVars(cmd, opts)
  // A relative target opens in the folder a same-command `cd` moved to, so
  // `cd <session scratchpad> && for f in …; do … > ld-$f.json; done` is not read
  // as a repo write. Only a target name that opened
  // in one known folder resolves there; no `cd`, an unreadable `cd`, or two
  // folders keep the old repo-relative reading.
  const cwds = redirectCwds(cmd, opts)
  const resolve = (t: string) => {
    const bare = t.replace(/^"(.*)"$|^'(.*)'$/s, (_m, d?: string, q?: string) => d ?? q ?? '')
    const value = t.startsWith("'") || !t.includes('$') ? t : expand(bare, vars, opts)
    const path = normalizeWritePath(value)
    if (!isAbsPath(path) && !path.startsWith('$') && !/^&\d?$|^-$/.test(path)) {
      const seen = cwds.get(bare)
      const cwd = seen?.size === 1 ? [...seen][0] : ''
      // An unresolved `$f` in the name stays literal; the folder decides.
      if (isAbsPath(cwd)) return posix.join(slashPath(cwd), path)
    }
    // `$sd/../…` must not climb out of the scratchpad and still pass.
    return isAbsPath(path) ? posix.normalize(path) : value
  }
  const targets = bashWriteTargets(cmd).filter(t => !isCosAllowlistedPath(resolve(t)))
  // An actual helper execution can write without a shell redirect. Keep the
  // existing noncanonical-write gate, but do not gate rg/cat/quoted mentions.
  // Bare help/dry-run reads fall through; redirects still use the normal gate.
  const helperWrite = /^(?:(?:env\s+)?(?:\w+=(?:"[^"]*"|'[^']*'|\S+)\s+)*)python3?\s+(?:"[^"]*eod-context-append\.py"|'[^']*eod-context-append\.py'|\S*eod-context-append\.py)(?=\s|$)(?!\s+(?:--help|-h|--dry-run)(?=\s*(?:$|[;&|\n]|2>&1)))/
  if (!isCosEodContextAppend(cmd) && bashCommandStartMatch(cmd, helperWrite)) targets.push(EOD_UNKNOWN_WRITE_TARGET)
  return targets
}

const EOD_UNKNOWN_WRITE_TARGET = '<noncanonical EOD helper write>'

/** Explicit CoS lifecycle exception, not an opaque-script bypass. Only the
 * canonical append-only helper with literal arguments qualifies, repeated on
 * separate lines if needed. Match the entire block, including quoted newlines.
 * No env/path override, other command, substitution, or redirect is allowed.
 * The helper validates the existing destination and dated section itself.
 */
export function isCosEodContextAppend(cmd: string): boolean {
  const helper = `${SWITCHBOARD_REPO}/system/scripts/eod-context-append.py`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const invocation = `python3 (?:system/scripts/eod-context-append\\.py|${helper}) --slug [a-z0-9]+(?:-[a-z0-9]+)* --body '[^']+'(?: --dry-run)?`
  return new RegExp(`^${invocation}(?:[ \\t]*\\n[ \\t]*${invocation})*$`).test(cmd.trim())
}

/** True when the bash command performs a Drive write: some `switchboard drive`
 * invocation whose subcommand token mutates, unless that
 * subcommand is an omnibus tool run with a read operation. */
export function isGdriveWrite(rawCmd: string): boolean {
  const cmd = shellTextWithoutDataHeredocs(rawCmd)
  for (const m of cmd.matchAll(GDRIVE_SUBCOMMAND_RE)) {
    const [, subcommand, rest] = m
    if (!GDRIVE_WRITE_SEGMENT_RE.test(subcommand)) continue
    if (GDRIVE_OMNIBUS_RE.test(subcommand) && GDRIVE_READ_OPERATION_RE.test(rest)) continue
    return true
  }
  return false
}

/** True when the bash command invokes router-dispatch.sh or
 * route-to-workspace.sh at a command start. Matching a mention inside quoted
 * text does not qualify. */
export function isDispatchInvocation(cmd: string): boolean {
  return bashCommandStartMatch(cmd, DISPATCH_SCRIPT_AT_COMMAND_START) !== null
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  function confirm(ctx: ConfirmContext, rule: string, options: Parameters<ConfirmContext['ui']['confirm']>[0]) {
    // A prompt in a worker or owner tab reaches CoS.
    // In Claude the bridge turns this confirm into a permission ask and the
    // Notification hook relays it; in Amp the modal is relayed from here.
    if (process.env.SWITCHBOARD_CLAUDE_BRIDGE !== '1' && config[`${PLUGIN}.permission-relay.disabled`] !== true) {
      const timeout = config[`${PLUGIN}.confirm_timeout_ms`]
      void runDetached('python3', [
        `${SWITCHBOARD_REPO}/system/scripts/permission_relay.py`, 'amp-confirm', '--rule', rule,
        '--title', options.title, '--detail', options.message ?? '',
        '--timeout-ms', String(typeof timeout === 'number' ? timeout : 90_000),
      ], { label: `permission-relay ${rule}` }).catch(() => false)
    }
    return confirmWithTimeout(ctx, options, config[`${PLUGIN}.confirm_timeout_ms`], rule)
  }

  function ruleEnabled(rule: string): boolean {
    if (!isPluginEnabled(config, PLUGIN)) return false
    return config[`${PLUGIN}.${rule}.disabled`] !== true
  }

  // ── Rule 8: CoS delivery confirm-gate ────────────────────────────────
  // CoS identity is structural: cmux identify (process-level RPC), same
  // pattern as cos-finished.ts and comms-triage-trigger.ts. Lazy (only
  // called after a delivery signature matched) and cached 60s so ordinary
  // tool calls cost nothing and a workspace rename is picked up. Fail open:
  // unreadable title = no gate.
  // Test seams: SWITCHBOARD_GUARDRAILS_WS_TITLE_OVERRIDE env var (title override)
  // or `switchboard-guardrails.cos-delivery-gate.assume-cos` boolean config.
  const COS_TITLE_RE = /🧭|\bcos\b|chief.*staff/i
  const DEFAULT_COS_STICKY_ALLOW_TTL_MS = 30 * 60_000
  let cosSessionCache: { value: boolean; at: number } | null = null
  const cosStickyAllows = new Map<string, number>()
  // Reuse covers only the identical operation in the current task turn.
  amp.on('agent.start', () => { cosStickyAllows.clear() })

  function cosStickyAllowTtlMs(): number {
    const configured = config[`${PLUGIN}.cos_sticky_allow_ttl_ms`]
    if (typeof configured === 'number' && Number.isFinite(configured) && configured >= 0) return configured
    return DEFAULT_COS_STICKY_ALLOW_TTL_MS
  }

  // Drive has no local path. Its exact command is part of the approval key,
  // so approval for one document never covers another document or operation.
  const GDRIVE_STICKY_TARGET = 'gdrive:'

  function absoluteWritePath(raw: string): string {
    if (raw === GDRIVE_STICKY_TARGET) return raw
    const normalized = normalizeWritePath(raw)
    return resolve(isAbsPath(normalized) ? normalized : `${SWITCHBOARD_REPO}/${normalized}`)
  }

  function pruneCosStickyAllows(now: number): void {
    for (const [prefix, expiresAt] of cosStickyAllows) {
      if (expiresAt <= now) cosStickyAllows.delete(prefix)
    }
  }

  function hasCosStickyAllow(what: string, detail: string, targets: string[]): boolean {
    if (targets.length === 0) return false
    const ttlMs = cosStickyAllowTtlMs()
    if (ttlMs === 0) {
      cosStickyAllows.clear()
      return false
    }
    const now = Date.now()
    pruneCosStickyAllows(now)
    return targets
      .map(absoluteWritePath)
      .every(path => (cosStickyAllows.get(JSON.stringify([what, detail, path])) ?? 0) > now)
  }

  function rememberCosStickyAllow(what: string, detail: string, targets: string[]): string[] {
    const ttlMs = cosStickyAllowTtlMs()
    if (ttlMs === 0 || targets.length === 0) return []
    const expiresAt = Date.now() + ttlMs
    const paths = [...new Set(targets.map(absoluteWritePath))]
    for (const path of paths) cosStickyAllows.set(JSON.stringify([what, detail, path]), expiresAt)
    return paths
  }

  async function isCosSession(): Promise<boolean> {
    const titleOverride = process.env.SWITCHBOARD_GUARDRAILS_WS_TITLE_OVERRIDE
    if (titleOverride !== undefined) return isOriginWorkspace(COS_TITLE_RE, overrideWorkspace(titleOverride))
    const forced = config[`${PLUGIN}.cos-delivery-gate.assume-cos`]
    if (typeof forced === 'boolean') return forced
    if (cosSessionCache && Date.now() - cosSessionCache.at < 60_000) return cosSessionCache.value
    const ws = await paneWorkspace()
    if (ws === null) return false // transient cmux or wmux failure — fail open, don't cache
    cosSessionCache = { value: isOriginWorkspace(COS_TITLE_RE, ws), at: Date.now() }
    return cosSessionCache.value
  }

  // ── Rule 10: dispatch guard ──────────────────────────────────────────
  // Same identity mechanics as isCosSession (title override seam, forced
  // config, 60s cache, fail open on cmux failure) but a wider title set:
  // CoS, Launchpad and Router N are all dispatch origins.
  let dispatchOriginCache: { value: boolean; at: number } | null = null

  async function isDispatchOriginSession(): Promise<boolean> {
    const titleOverride = process.env.SWITCHBOARD_GUARDRAILS_WS_TITLE_OVERRIDE
    if (titleOverride !== undefined) return isOriginWorkspace(DISPATCH_ORIGIN_TITLE_RE, overrideWorkspace(titleOverride))
    const forced = config[`${PLUGIN}.dispatch-guard.assume-origin`]
    if (typeof forced === 'boolean') return forced
    if (dispatchOriginCache && Date.now() - dispatchOriginCache.at < 60_000) return dispatchOriginCache.value
    const ws = await paneWorkspace()
    if (ws === null) return false
    dispatchOriginCache = { value: isOriginWorkspace(DISPATCH_ORIGIN_TITLE_RE, ws), at: Date.now() }
    return dispatchOriginCache.value
  }

  async function checkDispatch(
    event: { tool: string; input: Record<string, unknown> },
    ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } },
  ): Promise<{ action: 'reject-and-continue'; message: string } | null> {
    if (!ruleEnabled('dispatch-guard')) return null
    if (!DISPATCH_TOOLS.has(event.tool)) return null
    if (!(await isDispatchOriginSession())) return null
    const live = liveThreadsFromSessionStore()
    const reason = dispatchGuardBlockReason(event.tool, event.input, live)
    if (!reason) {
      await logEvent(PLUGIN, { rule: 'dispatch-guard', action: 'allow', tool: event.tool, thread: threadIdFromInput(event.input) })
      return null
    }
    const threadId = threadIdFromInput(event.input)
    const liveRouter = event.tool === 'send_thread_message' && Boolean(threadId && live?.get(threadId))
    if (liveRouter) {
      // Live Router thread: a coordination message (e.g. "hold teardown") can
      // be legitimate, so ask instead of hard-blocking. Declined/headless → reject.
      const ok = await confirm(ctx, 'dispatch-guard', {
        title: 'switchboard-guardrails: dispatch guard',
        message: `${reason}\n\nSend this message to the Router thread anyway? (Not for dispatching new work.)`,
        confirmButtonText: 'Send',
      })
      if (typeof ok !== 'boolean') return ok
      if (ok) {
        await logEvent(PLUGIN, { rule: 'dispatch-guard', action: 'allowed-confirm', tool: event.tool, thread: threadId })
        return null
      }
    }
    await logEvent(PLUGIN, { rule: 'dispatch-guard', action: 'reject', tool: event.tool, thread: threadId, reason })
    return { action: 'reject-and-continue', message: `Blocked by switchboard-guardrails (dispatch guard): ${reason}` }
  }

  // ── Rule 11: orchestrator wait guard ─────────────────────────────────
  const waitGuardPolls: PollState = new Map()

  function waitGuardOpts(): { windowMs?: number; limit?: number } {
    const windowMs = config[`${PLUGIN}.wait-guard.poll-window-ms`]
    const limit = config[`${PLUGIN}.wait-guard.poll-limit`]
    return {
      windowMs: typeof windowMs === 'number' && Number.isFinite(windowMs) && windowMs > 0 ? windowMs : undefined,
      limit: typeof limit === 'number' && Number.isFinite(limit) && limit > 0 ? limit : undefined,
    }
  }

  async function checkWait(
    event: { tool: string; input: Record<string, unknown> },
    ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } },
  ): Promise<{ action: 'reject-and-continue'; message: string } | null> {
    if (!ruleEnabled('wait-guard')) return null
    if (event.tool !== WAIT_TOOL && !POLL_TOOLS.has(event.tool)) return null
    if (!(await isDispatchOriginSession())) return null
    const verdict = waitGuardDecision(event.tool, event.input, waitGuardPolls, Date.now(), waitGuardOpts())
    if (verdict.kind === 'allow') return null
    if (verdict.kind === 'confirm') {
      // Explicit user-requested waiting is the only exception. Headless
      // (callback-triggered) turns cannot confirm, so they reject.
      const ok = await confirm(ctx, 'wait-guard', {
        title: 'switchboard-guardrails: wait guard',
        message: `${verdict.reason}\n\nDid the owner explicitly ask CoS to wait for this thread in the current turn? Dispatch-and-return is the default.`,
        confirmButtonText: 'the owner asked to wait',
      })
      if (typeof ok !== 'boolean') return ok
      if (ok) {
        await logEvent(PLUGIN, { rule: 'wait-guard', action: 'allowed-confirm', tool: event.tool, threads: threadIdsFromWaitInput(event.input) })
        return null
      }
    }
    await logEvent(PLUGIN, { rule: 'wait-guard', action: 'reject', tool: event.tool, threads: threadIdsFromWaitInput(event.input), reason: verdict.reason })
    return { action: 'reject-and-continue', message: `Blocked by switchboard-guardrails (wait guard): ${verdict.reason} ${WAIT_GUARD_GUIDANCE}` }
  }

  async function checkWaitBash(cmd: string): Promise<{ action: 'reject-and-continue'; message: string } | null> {
    if (!ruleEnabled('wait-guard')) return null
    const reason = blockingSleepReason(cmd)
    if (!reason) return null
    if (!(await isDispatchOriginSession())) return null
    await logEvent(PLUGIN, { rule: 'wait-guard', action: 'reject', tool: 'Bash', reason, cmd })
    return { action: 'reject-and-continue', message: `Blocked by switchboard-guardrails (wait guard): ${reason} ${WAIT_GUARD_GUIDANCE}` }
  }

  async function confirmCosDelivery(
    ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } },
    what: string,
    detail: string,
    stickyTargets: string[] = [],
  ): Promise<{ action: 'reject-and-continue'; message: string } | null> {
    if (hasCosStickyAllow(what, detail, stickyTargets)) {
      await logEvent(PLUGIN, {
        rule: 'cos-delivery-gate',
        action: 'allowed-sticky',
        what,
        detail,
        targets: stickyTargets.map(absoluteWritePath),
      })
      return null
    }
    const ok = await confirm(ctx, 'cos-delivery-gate', {
      title: 'switchboard-guardrails: CoS delivery gate',
      message: `CoS is an orchestrator — delivery work belongs in a routed workstream workspace.\n\n${what}:\n${detail}\n\nAllow this write in the CoS session anyway?`,
      confirmButtonText: 'Allow',
    })
    if (typeof ok !== 'boolean') return ok
    if (!ok) {
      await logEvent(PLUGIN, { rule: 'cos-delivery-gate', action: 'reject', what, detail })
      return {
        action: 'reject-and-continue',
        message: 'Blocked by switchboard-guardrails (CoS delivery gate): the CoS session plans and routes; it does not produce deliverables. Route this work to its owning workstream workspace (system/scripts/route-to-workspace.sh) instead, or ask the owner to confirm the write.',
      }
    }
    const stickyPrefixes = rememberCosStickyAllow(what, detail, stickyTargets)
    await logEvent(PLUGIN, { rule: 'cos-delivery-gate', action: 'allowed-after-confirm', what, detail, stickyPrefixes })
    return null
  }

  // edit_file / create_file outside allowlisted orchestration paths.
  async function checkCosFileWrite(
    event: { tool: string; input: Record<string, unknown> },
    ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } },
  ) {
    if (!ruleEnabled('cos-delivery-gate')) return null
    if (!FILE_WRITE_TOOLS.has(event.tool)) return null
    const path = String(event.input.path ?? '')
    if (!path || isCosAllowlistedPath(path)) return null
    if (!(await isCosSession())) return null
    return await confirmCosDelivery(ctx, `${event.tool} to non-orchestration path`, path, [path])
  }

  // Bash writes: `>`/`>>`/`tee` (incl. heredoc targets) outside allowlisted
  // paths, and gdrive write subcommands. Slack/Gmail sends are already
  // gated by rules 4/5.
  async function checkCosBash(
    cmd: string,
    ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } },
  ) {
    if (!ruleEnabled('cos-delivery-gate')) return null
    if (isCosEodContextAppend(cmd) && await isCosSession()) {
      await logEvent(PLUGIN, { rule: 'cos-delivery-gate', action: 'allowed-eod-context-append' })
      return null
    }
    const gated = gatedBashWriteTargets(cmd)
    const gdrive = isGdriveWrite(cmd)
    if (gated.length === 0 && !gdrive) return null
    if (!(await isCosSession())) return null
    // router-dispatch.sh / route-to-workspace.sh is the sanctioned handoff;
    // the prompt it carries is data, so `> path` or a Drive verb inside it is
    // not a CoS write. Logged so the exemption stays auditable.
    if (isDispatchInvocation(cmd)) {
      await logEvent(PLUGIN, { rule: 'cos-delivery-gate', action: 'allowed-dispatch-handoff', gated, gdrive })
      return null
    }
    const what = gdrive ? 'gdrive write from CoS session' : 'bash file write to non-orchestration path'
    const detail = gdrive ? cmd : `${gated.join(', ')}\n\n${cmd}`
    const stickyTargets = gated.includes(EOD_UNKNOWN_WRITE_TARGET) ? [] : gdrive ? [...gated, GDRIVE_STICKY_TARGET] : gated
    return await confirmCosDelivery(ctx, what, detail, stickyTargets)
  }

  // ── Rule 1+2+3+9: Bash command guardrails ────────────────────────────
  amp.on('tool.call', async (event, ctx) => {
    if (!isPluginEnabled(config, PLUGIN)) return { action: 'allow' }
    const sh = amp.helpers.shellCommandFromToolCall(event)
    if (!sh) {
      return await checkWait(event, ctx) ?? await checkDispatch(event, ctx) ?? await checkCosFileWrite(event, ctx) ?? await checkSlackTool(event, ctx) ?? await checkGmail(event, ctx) ?? await checkCalendar(event, ctx) ?? { action: 'allow' }
    }
    // Only after validating the WHOLE EOD block, hide its literal bodies from
    // command-oriented rules (e.g. rm/Gmail text in a saved retrospective).
    // Keep the actual tool input unchanged. Unsafe suffixes never qualify.
    // Rule 3b: on Windows a Git Bash redirect to `nul` writes a real file named nul that
    // Win32 tools cannot read or delete. Point it at /dev/null first,
    // so every rule below sees the command that will run.
    const nulFixed = ruleEnabled('nul-redirect') && isWindowsHost() ? rewriteNulRedirects(sh.command) : null
    const command = nulFixed ?? sh.command
    const cmd = isCosEodContextAppend(command)
      ? command.replace(/'[^']*'/g, "'<EOD data>'")
      : command

    // Rule 1: git add -A / git add --all
    if (ruleEnabled('git-add-all') && bashCommandStartMatch(cmd, GIT_ADD_ALL_AT_COMMAND_START)) {
      await logEvent(PLUGIN, { rule: 'git-add-all', action: 'reject', cmd })
      return {
        action: 'reject-and-continue',
        message: 'Blocked by switchboard-guardrails: never use `git add -A`/`--all`/`add .`. Stage explicit paths instead.',
      }
    }

    // Rule 2: git push --force / --no-verify
    if (ruleEnabled('git-push-force') && bashCommandStartMatch(cmd, GIT_PUSH_FORCE_AT_COMMAND_START)) {
      await logEvent(PLUGIN, { rule: 'git-push-force', action: 'reject', cmd })
      return {
        action: 'reject-and-continue',
        message: 'Blocked by switchboard-guardrails: `git push --force`/`--no-verify` is destructive. Ask user before bypassing safety.',
      }
    }

    // Rule 9: browser automation uses one persistent, headless MCP profile.
    // The only visible exception is a CoS-coordinated sign-in carrying the
    // explicit acknowledgment. AppleScript control of daily Chrome never passes.
    if (ruleEnabled('browser-automation')) {
      const reason = browserAutomationBlockReason(cmd)
      if (reason) {
        await logEvent(PLUGIN, { rule: 'browser-automation', action: 'reject', cmd, reason })
        return {
          action: 'reject-and-continue',
          message: `Blocked by switchboard-guardrails: ${reason} Use the supported API first. If it cannot complete the task, use the chrome-devtools MCP tools, which follow this machine's browser rule (system/scripts/owner_browser.py). Report a blocker to CoS instead of opening another browser.`,
        }
      }
    }

    // Rule 9b: pages open in the background (`open -g`), never stealing focus.
    if (ruleEnabled('open-focus')) {
      const reason = foregroundPageOpenReason(cmd)
      if (reason) {
        await logEvent(PLUGIN, { rule: 'open-focus', action: 'reject', cmd, reason })
        return {
          action: 'reject-and-continue',
          message: `Blocked by switchboard-guardrails: ${reason} Open pages and images with \`python3 system/scripts/image_compare.py open <page-or-url>\`, which puts them in an unselected cmux browser tab so nothing steals focus. Only when the owner asks to bring the page up, prefix SWITCHBOARD_FOCUS_OK=1.`,
        }
      }
    }

    // Rule 9c: the file organizer only runs from an approved plan, on a current copy of the script.
    if (ruleEnabled('organize-execute')) {
      const organizeCwd = [event.input.workdir, event.input.cwd].find(v => typeof v === 'string') as string | undefined
      const reason = organizeExecuteViolation(cmd, organizeCwd ?? process.cwd())
      if (reason) {
        await logEvent(PLUGIN, { rule: 'organize-execute', action: 'reject', cmd, reason })
        return {
          action: 'reject-and-continue',
          message: `Blocked by switchboard-guardrails: ${reason} ${ORGANIZE_EXECUTE_FIX}`,
        }
      }
    }

    // Rule 9d: no agent prints secrets from its environment. Reading files in Downloads is allowed.
    if (ruleEnabled('sensitive-reads')) {
      const envProblem = envDumpViolation(cmd)
      if (envProblem) {
        await logEvent(PLUGIN, { rule: 'env-dump', action: 'reject' })
        return { action: 'reject-and-continue', message: `Blocked by switchboard-guardrails: ${envProblem} ${ENV_FIX}` }
      }
    }

    // Rule 9e: no amp call that starts Amp's device sign-in (a substitute home, its login or logout
    // subcommand, or signed out on Windows), because the sign-in opens a tab in the owner's browser.
    if (ruleEnabled('amp-auth')) {
      const ampCwd = [event.input.workdir, event.input.cwd].find(v => typeof v === 'string') as string | undefined
      const ampProblem = ampAuthViolation(cmd, ampCwd ?? process.cwd())
      if (ampProblem) {
        await logEvent(PLUGIN, { rule: 'amp-auth', action: 'reject' })
        return { action: 'reject-and-continue', message: `Blocked by switchboard-guardrails: ${ampProblem} ${AMP_FIX}` }
      }
    }

    // Rule 13 (bash-side): links carry anchor text and channel mentions are
    // `<#C…|name>`; a quoted shell argument cannot be rewritten, so reject
    // with the exact fix.
    const slackLinkBashDecision = await checkSlackLinkFormatBash(cmd)
    if (slackLinkBashDecision) return slackLinkBashDecision

    // Rule 14 (bash-side): text bullets in a Slack write reject with the
    // slack_format.py conversion command; send-triage-draft.py converts itself.
    const slackBulletsBashDecision = await checkSlackNativeBulletsBash(cmd)
    if (slackBulletsBashDecision) return slackBulletsBashDecision

    // Rule 15 (bash-side): a known person with no <@U…> mention or a PR number
    // with no link is logged and notified, never rejected.
    await warnSlackPeopleLinksBash(cmd, ctx)

    // Rule 4 (bash-side): Slack writes via switchboard must start with bot prefix
    const slackBashDecision = await checkSlackBash(cmd)
    if (slackBashDecision) return slackBashDecision

    // Rule 16 (bash-side): a `gh pr review` / `gh pr comment` body that names
    // approvals, owner teams, or "as the body says"
    // is logged and notified, never rejected.
    await warnGithubReviewWordingBash(cmd, ctx)

    // Rule 4 (bash-side, GitHub): `gh` writes with a body must start with the
    // robot_face image line or the 🤖 fallback, never the Slack shortcode.
    const githubBashDecision = await checkGithubBash(cmd)
    if (githubBashDecision) return githubBashDecision

    // Rule 5 (bash-side): Gmail sends via switchboard require preview confirmation.
    const gmailBashDecision = await checkGmailBash(cmd, ctx)
    if (gmailBashDecision) return gmailBashDecision

    // Rule 3: rm -rf. Destructive actions inside the switchboard repo are pre-approved
    // (~/AGENTS.md Side Effects And Permissions), but go through the
    // helper, which hard-deletes only what git tracks unchanged; temp
    // and build paths run as written. Any other target goes to the Trash
    // with no prompt (the command word is rewritten to switchboard-trash.py, which logs
    // each move for the worker's report). Repo root, `.git`, and top-level system
    // or home folders are hard blocks. The prompt is left only for a delete that
    // cannot be rewritten (sudo, a multi-word eval); permission_relay.py sends
    // that prompt to CoS.
    let trashRewrite: { cmd: string; targets: string[] } | null = null
    if (ruleEnabled('rm-rf')) {
      // The Claude bridge passes `workdir`; Amp's Bash tool may carry `cwd`. With
      // neither, a relative target goes to the helper, which re-checks it at run time.
      const workdir = [event.input.workdir, event.input.cwd].find(v => typeof v === 'string' && isAbsPath(v)) as string ?? ''
      const plan = planRmRf(cmd, { home: HOME, repo: SWITCHBOARD_REPO, workdir, realpath: realParentPath, temp: windowsTempRoots() })
      if (plan.blocked.length > 0) {
        const what = plan.blocked.map(v => `${v.resolved} (${v.reason})`).join(', ')
        await logEvent(PLUGIN, { rule: 'rm-rf', action: 'reject-protected', targets: plan.blocked.map(v => v.resolved), cmd })
        return {
          action: 'reject-and-continue',
          message: `Blocked by switchboard-guardrails: never delete ${what}. Narrow the target to the folder you mean, or ask the owner first.`,
        }
      }
      // In-repo targets go to the helper too: it
      // deletes only what git tracks unchanged and sends gitignored or untracked paths
      // (local/, system/state/, scratch) to the Trash. When the command cannot be
      // rewritten, an in-repo-only delete still runs as written (pre-approved).
      const helper = trashHelperPath()
      const word = helperWord(helper)
      const canRewrite = config[`${PLUGIN}.rm-rf.trash.disabled`] !== true && cmd === command &&
        word !== null && existsSync(helper)
      const repoInvocations = plan.repo.map(t => t.invocation)
      if (plan.trash.length === 0 && repoInvocations.length > 0) {
        const targets = plan.repo.flatMap(t => t.targets.map(v => v.resolved))
        const rewritten = canRewrite ? rewriteToTrash(cmd, repoInvocations, word!) : null
        if (rewritten) {
          trashRewrite = { cmd: rewritten, targets }
          await logEvent(PLUGIN, { rule: 'rm-rf', action: 'rewritten-repo-to-helper', targets, cmd })
        } else {
          await logEvent(PLUGIN, { rule: 'rm-rf', action: 'repo-delete-as-written', targets, cmd })
        }
      }
      if (plan.trash.length > 0) {
        const targets = plan.trash.flatMap(t => t.targets.map(v => v.resolved))
        const trashInvocations = plan.trash.map(t => t.invocation)
        const rewritten = canRewrite
          ? rewriteToTrash(cmd, [...trashInvocations, ...repoInvocations], word!) ??
            rewriteToTrash(cmd, trashInvocations, word!)
          : null
        if (rewritten) {
          trashRewrite = { cmd: rewritten, targets }
          await logEvent(PLUGIN, { rule: 'rm-rf', action: 'rewritten-to-trash', targets, cmd })
        } else if (isWindowsHost() && config[`${PLUGIN}.rm-rf.trash.disabled`] !== true) {
          // Windows never hard-deletes an outside target. A confirm prompt here would end in
          // the original `rm -rf`, so reject and say what to do.
          await logEvent(PLUGIN, { rule: 'rm-rf', action: 'reject-no-recycle-rewrite', targets, cmd })
          return {
            action: 'reject-and-continue',
            message: `Blocked by switchboard-guardrails: on Windows an rm -rf outside switchboard goes to the Recycle Bin through ${helper}, and this command cannot be rewritten to do that (${targets.join(', ')}). Nothing was deleted. Run one plain \`rm -rf <path>\` per target with no sudo, eval, or quoting tricks, or ask the owner to delete it in Explorer.`,
          }
        } else {
          const ok = await confirm(ctx, 'rm-rf', {
            title: 'switchboard-guardrails: confirm rm -rf',
            message: `Allow destructive delete of ${targets.join(', ')}?\n\n${cmd}`,
            confirmButtonText: 'Allow',
          })
          if (typeof ok !== 'boolean') return ok
          if (!ok) {
            await logEvent(PLUGIN, { rule: 'rm-rf', action: 'reject', cmd })
            return { action: 'reject-and-continue', message: 'User rejected destructive rm.' }
          }
          await logEvent(PLUGIN, { rule: 'rm-rf', action: 'allowed-after-confirm', cmd })
        }
      }
    }

    // Rule 11 (bash-side): blocking sleep in an orchestrator session.
    const waitBashDecision = await checkWaitBash(cmd)
    if (waitBashDecision) return waitBashDecision

    // Rule 8 (bash-side): CoS delivery confirm-gate.
    const cosBashDecision = await checkCosBash(cmd, ctx)
    if (cosBashDecision) return cosBashDecision

    const toolDecision = await checkSlack(event, ctx) ?? await checkGmail(event, ctx) ?? await checkCalendar(event, ctx)
    if (toolDecision) return toolDecision
    if (nulFixed) await logEvent(PLUGIN, { rule: 'nul-redirect', action: 'rewritten-to-dev-null', cmd: sh.command })
    // Rule 3: the Trash rewrite goes out only after every other rule passed (it carries Rule 3b's fix too).
    if (trashRewrite) return { action: 'allow-with-input' as const, input: { ...event.input, cmd: trashRewrite.cmd } }
    if (nulFixed) return { action: 'allow-with-input' as const, input: { ...event.input, cmd: nulFixed } }
    return { action: 'allow' }
  })

  // ── Rule 13: Slack link and channel-mention format ───────────────────
  const SLACK_LINK_RULE = 'Slack links are inline with anchor text that names the thing (`<https://url|anchor text>`, or `[anchor text](url)` in a markdown block), never a bare URL; channel mentions are `<#C…|channel-name>`, never plain `#channel-name` (system/prompts/external-replies.md Rule 4)'

  function slackLinkRejectMessage(via: string, finding: SlackLinkFormatFinding, bashCannotRewrite: boolean): string {
    const parts: string[] = []
    if (finding.bareUrls.length) {
      parts.push(`bare URL${finding.bareUrls.length > 1 ? 's' : ''} ${finding.bareUrls.map((u) => `"${u}"`).join(', ')}: wrap each as <url|anchor text> where the anchor names the thing (the PR title, the Linear issue title, the doc name, "the thread with Uma"); the guardrail cannot invent that text`)
    }
    if (finding.unresolved.length) {
      parts.push(`channel mention${finding.unresolved.length > 1 ? 's' : ''} ${finding.unresolved.map((n) => `"#${n}"`).join(', ')} with no known channel id: write <#C…|${finding.unresolved[0]}> with the id from system/config/priority-channels.yaml or a Slack lookup`)
    }
    if (bashCannotRewrite && finding.rewritten.length) {
      parts.push(`plain channel mention${finding.rewritten.length > 1 ? 's' : ''} ${finding.rewritten.map((r) => `"#${r.name}" → "<#${r.id}|${r.name}>"`).join(', ')}: paste the replacement`)
    }
    return `Blocked by switchboard-guardrails (slack-link-format): this Slack ${via} has ${parts.join('; and ')}. ${SLACK_LINK_RULE}. Fix the message text and re-run.`
  }

  /** Tool-level: rewrite resolvable `#name` mentions in place; reject bare URLs and unknown channels. */
  async function checkSlackLinkFormatTool(event: { tool: string; input: Record<string, unknown> }) {
    if (!ruleEnabled('slack-link-format')) return null
    if (!SLACK_POST_TOOLS.has(event.tool)) return null
    const text = String(event.input.text ?? '')
    const finding = checkSlackLinkFormat(text)
    const blockFindings = slackBlocksTexts(event.input.blocks ?? []).map((t) => checkSlackLinkFormat(t))
    const bareUrls = [...new Set([...finding.bareUrls, ...blockFindings.flatMap((f) => f.bareUrls)])]
    const unresolved = [...new Set([...finding.unresolved, ...blockFindings.flatMap((f) => f.unresolved)])]
    // Channel mentions inside `blocks` are not rewritten here (the payload shape
    // varies); they are reported so the caller fixes the block text.
    const blockPlain = blockFindings.flatMap((f) => f.rewritten.map((r) => r.name))
    if (bareUrls.length || unresolved.length || blockPlain.length) {
      const merged: SlackLinkFormatFinding = { bareUrls, unresolved: [...new Set([...unresolved, ...blockPlain])], rewritten: [], text }
      await logEvent(PLUGIN, { rule: 'slack-link-format', action: 'reject', tool: event.tool, bareUrls, unresolved: merged.unresolved })
      return { action: 'reject-and-continue' as const, message: slackLinkRejectMessage(event.tool, merged, false) }
    }
    if (!finding.rewritten.length) return null
    await logEvent(PLUGIN, { rule: 'slack-link-format', action: 'rewrite-channels', tool: event.tool, channels: finding.rewritten })
    return { input: { ...event.input, text: finding.text } }
  }

  // ── Rule 14: Slack native bullets ────────────────────────────────────
  const SLACK_BULLET_RULE = 'Every text bullet is a native Slack rich_text list, words unchanged (system/prompts/external-replies.md Rule 4 item 5)'

  function slackBulletRejectMessage(via: string, lines: string[]): string {
    const quoted = lines.slice(0, 4).map((l) => `"${l.trim()}"`).join(', ')
    const more = lines.length > 4 ? ` and ${lines.length - 4} more` : ''
    return `Blocked by switchboard-guardrails (slack-native-bullets): this Slack ${via} has text bullets (${quoted}${more}). ${SLACK_BULLET_RULE}. Build blocks with \`python3 system/scripts/slack_format.py blocks "<text>"\` and pass them as \`--blocks\` (keep \`--text\` as the fallback).`
  }

  /** Tool-level: `text` is judged only when no `blocks` carry the message; mrkdwn block texts are always judged. */
  async function checkSlackNativeBulletsTool(event: { tool: string; input: Record<string, unknown> }) {
    if (!ruleEnabled('slack-native-bullets')) return null
    if (!SLACK_POST_TOOLS.has(event.tool)) return null
    const blocks = event.input.blocks
    const hasBlocks = Array.isArray(blocks) ? blocks.length > 0 : typeof blocks === 'string' && blocks.trim().length > 0
    const lines: string[] = []
    if (!hasBlocks) lines.push(...slackBulletLines(String(event.input.text ?? '')))
    else for (const t of slackMrkdwnTexts(blocks)) lines.push(...slackBulletLines(t))
    if (!lines.length) return null
    await logEvent(PLUGIN, { rule: 'slack-native-bullets', action: 'reject', tool: event.tool, lines })
    return { action: 'reject-and-continue' as const, message: slackBulletRejectMessage(event.tool, lines) }
  }

  /** Bash-level: a quoted argument cannot be restructured into blocks, so reject with the conversion command. */
  async function checkSlackNativeBulletsBash(cmd: string) {
    if (!ruleEnabled('slack-native-bullets')) return null
    const subcmd = slackBashWriteSubcommand(cmd)
    if (!subcmd || subcmd === 'write-canvas') return null
    const lines: string[] = []
    for (const text of await slackBashMrkdwnTexts(cmd)) lines.push(...slackBulletLines(text))
    if (!lines.length) return null
    await logEvent(PLUGIN, { rule: 'slack-native-bullets', action: 'reject-bash', subcmd, lines })
    return { action: 'reject-and-continue' as const, message: slackBulletRejectMessage(`${subcmd} via switchboard`, lines) }
  }

  /** Rules 13, 14, then 4 on one Slack tool call, so the prefix is injected into the rewritten text. */
  async function checkSlackTool(event: { tool: string; input: Record<string, unknown> }, ctx: unknown) {
    const format = await checkSlackLinkFormatTool(event)
    if (format && 'action' in format) return format
    const bullets = await checkSlackNativeBulletsTool(event)
    if (bullets) return bullets
    await warnSlackPeopleLinksTool(event, ctx)
    const next = format ? { ...event, input: format.input } : event
    const prefix = await checkSlack(next, ctx)
    if (prefix) return prefix
    return format ? { action: 'allow-with-input' as const, input: format.input } : null
  }

  /** Bash-level: a quoted argument cannot be rewritten, so any finding rejects with the exact replacement. */
  async function checkSlackLinkFormatBash(cmd: string) {
    if (!ruleEnabled('slack-link-format')) return null
    const subcmd = slackBashWriteSubcommand(cmd)
    if (!subcmd || subcmd === 'write-canvas') return null
    const merged: SlackLinkFormatFinding = { bareUrls: [], rewritten: [], unresolved: [], text: '' }
    for (const text of await slackBashMessageTexts(cmd)) {
      const f = checkSlackLinkFormat(text)
      merged.bareUrls.push(...f.bareUrls.filter((u) => !merged.bareUrls.includes(u)))
      merged.unresolved.push(...f.unresolved.filter((n) => !merged.unresolved.includes(n)))
      merged.rewritten.push(...f.rewritten.filter((r) => !merged.rewritten.some((m) => m.name === r.name)))
    }
    if (!merged.bareUrls.length && !merged.unresolved.length && !merged.rewritten.length) return null
    await logEvent(PLUGIN, { rule: 'slack-link-format', action: 'reject-bash', subcmd, bareUrls: merged.bareUrls, unresolved: merged.unresolved, channels: merged.rewritten })
    return { action: 'reject-and-continue' as const, message: slackLinkRejectMessage(`${subcmd} via switchboard`, merged, true) }
  }

  // ── Rule 15: Slack people mentions and PR links (warn only) ──────────
  const SLACK_PEOPLE_LINKS_RULE = 'Link every referenced thread, PR, file, or section, write short-linked terms as their link, and @mention every named person (system/prompts/external-replies.md Rule 4 item 6)'

  function slackPeopleLinksWarnMessage(via: string, finding: SlackPeopleLinksFinding): string {
    const parts: string[] = []
    if (finding.people.length) parts.push(`names ${finding.people.map((n) => `"${n}"`).join(', ')} without a <@U…> mention (id via \`switchboard slack people\`)`)
    if (finding.prRefs.length) parts.push(`refers to ${finding.prRefs.map((r) => `"${r}"`).join(', ')} without a <url|anchor> link`)
    if (finding.goTerms.length) parts.push(`mentions ${finding.goTerms.map((g) => `"${g}"`).join(', ')} without its short link`)
    return `Warning from switchboard-guardrails (slack-people-links): this Slack ${via} ${parts.join(' and ')}. ${SLACK_PEOPLE_LINKS_RULE}. The send was not blocked.`
  }

  /** Log the finding and surface it when the host has `ui.notify`; never rejects, never rewrites. */
  async function warnSlackPeopleLinks(via: string, texts: string[], ctx: unknown, extra: Record<string, unknown>) {
    const finding = checkSlackPeopleLinks(texts)
    if (!finding.people.length && !finding.prRefs.length && !finding.goTerms.length) return
    await logEvent(PLUGIN, { rule: 'slack-people-links', ...extra, people: finding.people, prRefs: finding.prRefs, goTerms: finding.goTerms })
    const notify = (ctx as { ui?: { notify?: (msg: string) => unknown } } | undefined)?.ui?.notify
    if (typeof notify !== 'function') return
    try { await Promise.resolve(notify(slackPeopleLinksWarnMessage(via, finding))) } catch { /* notification is best effort */ }
  }

  async function warnSlackPeopleLinksTool(event: { tool: string; input: Record<string, unknown> }, ctx: unknown) {
    if (!ruleEnabled('slack-people-links')) return
    if (!SLACK_POST_TOOLS.has(event.tool)) return
    const texts = [String(event.input.text ?? ''), ...slackBlocksTexts(event.input.blocks ?? [])]
    await warnSlackPeopleLinks(event.tool, texts, ctx, { action: 'warn', tool: event.tool })
  }

  async function warnSlackPeopleLinksBash(cmd: string, ctx: unknown) {
    if (!ruleEnabled('slack-people-links')) return
    const subcmd = slackBashWriteSubcommand(cmd)
    if (!subcmd || subcmd === 'write-canvas') return
    await warnSlackPeopleLinks(`${subcmd} via switchboard`, await slackBashMessageTexts(cmd), ctx, { action: 'warn-bash', subcmd })
  }

  // ── Rule 16: code review wording on gh pr review / comment (warn only) ──
  const GITHUB_REVIEW_WORDING_RULE = 'A switchboard code review covers the code only. Never cite bot comments (Copilot, Dependabot, or any review bot), approvals, owner teams, CI status, merge order, or anything the PR body already says; a comment earns its place only if it tells the author something they do not already know about the code, anchored with file:line (system/prompts/external-replies.md Rule 10)'
  const GITHUB_REVIEW_WORDING_SUBCMDS = new Set(['pr review', 'pr comment'])

  function githubReviewWordingWarnMessage(subcmd: string, hits: string[]): string {
    const clauses = GITHUB_REVIEW_WORDING_TERMS.filter(({ term }) => hits.includes(term)).map(({ term, clause }) => `"${term}" (${clause})`)
    return `Warning from switchboard-guardrails (github-review-wording): this gh ${subcmd} body mentions ${clauses.join(', ')}. ${GITHUB_REVIEW_WORDING_RULE}. The write was not blocked.`
  }

  /** Every text a gh write's body may come from: body files, $(cat) substitutions, ANSI-C words, heredocs, and the command itself. */
  async function githubBashBodyTexts(cmd: string, subcmd: string): Promise<string[]> {
    const { readFile } = await import('node:fs/promises')
    const texts: string[] = [cmd]
    const read = async (path: string, json: boolean): Promise<string | null> => {
      try {
        const content = await readFile(path, 'utf8')
        if (!json) return content
        const body = (JSON.parse(content) as { body?: unknown }).body
        return typeof body === 'string' ? body : null
      } catch {
        return null
      }
    }
    for (const file of githubBodyFiles(cmd, subcmd)) {
      const text = await read(file.path, file.json)
      if (text) texts.push(text)
    }
    for (const path of catSubstitutionPaths(cmd)) {
      const text = await read(path, false)
      if (text) texts.push(text)
    }
    texts.push(...ansiCQuotedWords(cmd), ...heredocBodies(cmd))
    return texts
  }

  /** Log the hits and surface them when the host has `ui.notify`; never rejects, never rewrites. */
  async function warnGithubReviewWordingBash(cmd: string, ctx: unknown) {
    if (!ruleEnabled('github-review-wording')) return
    const subcmd = githubBashWriteSubcommand(cmd)
    if (!subcmd || !GITHUB_REVIEW_WORDING_SUBCMDS.has(subcmd)) return
    if (!githubBashHasBody(cmd, subcmd)) return
    const hits = githubReviewWordingHits(await githubBashBodyTexts(cmd, subcmd))
    if (!hits.length) return
    await logEvent(PLUGIN, { rule: 'github-review-wording', action: 'warn-bash', subcmd, terms: hits })
    const notify = (ctx as { ui?: { notify?: (msg: string) => unknown } } | undefined)?.ui?.notify
    if (typeof notify !== 'function') return
    try { await Promise.resolve(notify(githubReviewWordingWarnMessage(subcmd, hits))) } catch { /* notification is best effort */ }
  }

  // ── Rule 4: Slack bot prefix (DM, channel, thread reply — all bot-authored) ──
  async function checkSlack(event: { tool: string; input: Record<string, unknown> }, _ctx: unknown) {
    if (!ruleEnabled('slack-bot-prefix')) return null
    if (!SLACK_POST_TOOLS.has(event.tool)) return null
    const channel = String(event.input.channel ?? '')
    const text = String(event.input.text ?? '')
    // The owner's explicit "no robot" for one message: a first line of exactly
    // `SWITCHBOARD_NO_ROBOT=1` is that opt-out;
    // it is stripped and no prefix is injected. Rules 13, 14, and 15 already ran.
    const optedOut = slackNoRobotOptOutText(text)
    if (optedOut !== null) {
      await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'no-robot-optout', tool: event.tool, channel })
      return {
        action: 'allow-with-input' as const,
        input: { ...event.input, text: optedOut },
      }
    }
    // Idempotent when the prefix already opens the first sentence; a standalone
    // prefix line (retired shape) is folded onto the sentence below it.
    const newText = injectSlackBotPrefix(text)
    if (newText === null) return null
    await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'inject', tool: event.tool, channel })
    return {
      action: 'allow-with-input' as const,
      input: { ...event.input, text: newText },
    }
  }

  // Helper: how does the bash message (including any `$(cat /path)`
  // substitutions) open? Used by the bash-level slack check since we can't
  // safely rewrite a quoted shell argument. Any source in the correct shape
  // wins; otherwise a standalone prefix line anywhere beats "missing" so the
  // reject names the actual mistake.
  async function slackBashBotPrefixShape(cmd: string): Promise<SlackBotPrefixShape> {
    const { readFile } = await import('node:fs/promises')
    const shapes: SlackBotPrefixShape[] = []
    for (const path of catSubstitutionPaths(cmd)) {
      try {
        shapes.push(slackBotPrefixShape(await readFile(path, 'utf8')))
      } catch {
        // file not readable — fall through and reject
      }
    }
    // ANSI-C quoted payloads ($':robot_face: …') carry escapes; decode the
    // argument and check the real text.
    for (const word of ansiCQuotedWords(cmd)) shapes.push(slackBotPrefixShape(word))
    // For inline command payloads, require the prefix immediately after a quote or
    // JSON string boundary, not merely somewhere later in the body.
    shapes.push(slackInlineBotPrefixShape(cmd))
    if (shapes.includes('ok')) return 'ok'
    return shapes.includes('standalone') ? 'standalone' : 'missing'
  }

  // Bash-level Slack write guard: catches message sends/updates and canvas writes.
  // Returns a tool-call decision or null if not applicable.
  async function checkSlackBash(cmd: string) {
    if (!ruleEnabled('slack-bot-prefix')) return null
    const subcmd = slackBashWriteSubcommand(cmd)
    if (!subcmd) {
      if (isCommsTriageSend(cmd)) {
        // Approved Comms Triage replies post from the owner's account with
        // the :robot_face: prefix the script adds itself (with_bot_prefix; opt-out
        // only by a per-draft `no robot`), so the bash shape check is not repeated
        // here. A direct `switchboard slack …` write chained onto the same
        // command still rejects.
        await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'comms-triage-send-script-prefixes' })
      }
      return null
    }
    // The owner's explicit "no robot" for one message:
    // `SWITCHBOARD_NO_ROBOT=1 switchboard slack …` is that opt-out
    // for this one send; the link, bullet, people-links, and naming rules still ran.
    if (slackNoRobotOptOutBash(cmd)) {
      const channel = bashFlagValue(cmd, '--channel') ?? bashFlagValue(cmd, '--user') ?? ''
      await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'no-robot-optout', subcmd, channel })
      return null
    }
    const shape = await slackBashBotPrefixShape(cmd)
    if (shape === 'ok') return null
    if (shape === 'standalone') {
      await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'reject-bash', subcmd, standalone: true })
      return {
        action: 'reject-and-continue' as const,
        message: `Blocked by switchboard-guardrails: the "${SLACK_BOT_PREFIX}" prefix in this Slack ${subcmd} via bot stands alone on its first line. Put it on the same line as the first sentence — "${SLACK_BOT_PREFIX} <first word…>", emoji, one space, then the first word (switchboard AGENTS.md "Bot Attribution Prefix"). Fix the message text (or the $(cat /path) file) and re-run.`,
      }
    }
    await logEvent(PLUGIN, { rule: 'slack-bot-prefix', action: 'reject-bash', subcmd })
    return {
      action: 'reject-and-continue' as const,
      message: `Blocked by switchboard-guardrails: Slack ${subcmd} via bot MUST start with "${SLACK_BOT_PREFIX} " followed by the first word on the same line (switchboard AGENTS.md "Bot Attribution Prefix"). Prepend it to the message text (or to any file piped in via $(cat /path)) and re-run.`,
    }
  }

  // The hosted image line as AGENTS.md records it, for the reject message. One
  // source of truth: the id lives in the rule, not here.
  async function canonicalGithubPrefixImage(): Promise<string> {
    try {
      const { readFile } = await import('node:fs/promises')
      const agents = await readFile(`${SWITCHBOARD_REPO}/AGENTS.md`, 'utf8')
      return GITHUB_BOT_PREFIX_CANONICAL_RE.exec(agents)?.[0] ?? GITHUB_BOT_PREFIX_IMG_TEMPLATE
    } catch {
      return GITHUB_BOT_PREFIX_IMG_TEMPLATE
    }
  }

  // Which prefix form a `gh` write's body opens with, reading body files
  // (--body-file, -F, body=@, --input JSON), $(cat …) substitutions, ANSI-C
  // words, heredocs, and finally the inline quoted body.
  async function githubBashPrefixForm(cmd: string, subcmd: string): Promise<GithubBotPrefixForm | null> {
    const { readFile } = await import('node:fs/promises')
    const read = async (path: string, json: boolean): Promise<string | null> => {
      try {
        const content = await readFile(path, 'utf8')
        if (!json) return content
        const body = (JSON.parse(content) as { body?: unknown }).body
        return typeof body === 'string' ? `${body}\n` : null
      } catch {
        return null
      }
    }
    for (const file of githubBodyFiles(cmd, subcmd)) {
      const form = githubBotPrefixForm((await read(file.path, file.json)) ?? '')
      if (form) return form
    }
    for (const path of catSubstitutionPaths(cmd)) {
      const form = githubBotPrefixForm((await read(path, false)) ?? '')
      if (form) return form
    }
    for (const text of [...ansiCQuotedWords(cmd), ...heredocBodies(cmd).map((body) => `${body}\n`)]) {
      const form = githubBotPrefixForm(text)
      if (form) return form
    }
    return githubInlineBodyPrefixForm(cmd)
  }

  // Bash-level GitHub write guard: `gh` writes with a body must open
  // their first paragraph with the robot_face image or the 🤖 fallback, one
  // space, then the first word; the Slack shortcode renders as literal text on
  // GitHub and is rejected like any other body. The first paragraph may follow a
  // leading markdown heading (PR bodies). A prefix alone on its line and a
  // prefix above a heading are rejected with their own reasons.
  async function checkGithubBash(cmd: string) {
    if (!ruleEnabled('github-bot-prefix')) return null
    const subcmd = githubBashWriteSubcommand(cmd)
    if (!subcmd) return null
    if (!githubBashHasBody(cmd, subcmd)) {
      await logEvent(PLUGIN, { rule: 'github-bot-prefix', action: 'no-inspectable-body', subcmd })
      return null
    }
    const form = await githubBashPrefixForm(cmd, subcmd)
    const placement = 'Put it at the start of the first body paragraph — image, one space, then the first word on the same line — which is the paragraph directly under the first heading when the body opens with one, or the first line otherwise (switchboard AGENTS.md "Bot Attribution Prefix").'
    if (form === 'before-heading') {
      await logEvent(PLUGIN, { rule: 'github-bot-prefix', action: 'reject-bash', subcmd, beforeHeading: true })
      return {
        action: 'reject-and-continue' as const,
        message: `Blocked by switchboard-guardrails: the robot_face prefix in this GitHub ${subcmd} via bot sits above a markdown heading, so GitHub renders the robot inline on the heading line. ${placement} Fix the body and re-run.`,
      }
    }
    if (form === 'standalone') {
      await logEvent(PLUGIN, { rule: 'github-bot-prefix', action: 'reject-bash', subcmd, standalone: true })
      return {
        action: 'reject-and-continue' as const,
        message: `Blocked by switchboard-guardrails: the robot_face prefix in this GitHub ${subcmd} via bot stands alone on its own line. ${placement} Fix the body and re-run.`,
      }
    }
    if (form) return null
    const shortcode = cmd.includes(SLACK_BOT_PREFIX)
    await logEvent(PLUGIN, { rule: 'github-bot-prefix', action: 'reject-bash', subcmd, shortcode })
    const image = await canonicalGithubPrefixImage()
    return {
      action: 'reject-and-continue' as const,
      message: `Blocked by switchboard-guardrails: GitHub ${subcmd} via bot MUST start its first paragraph with the robot_face emoji as an inline image, one space, then the first word: ${image} First word… (or "${GITHUB_BOT_PREFIX_FALLBACK} First word…" when no image can embed). When the body opens with a markdown heading (PR bodies), that paragraph is the one directly under the first heading.${shortcode ? ` GitHub has no "${SLACK_BOT_PREFIX}" shortcode; it renders as literal text there.` : ''} Add the prefix to the body (or to the --body-file / $(cat /path) file) and re-run (switchboard AGENTS.md "Bot Attribution Prefix").`,
    }
  }

  // ── Rule 5: Gmail send preview ──────────────────────────────────────
  // We can't easily inspect prior messages from inside a tool.call handler in
  // the current API. As a conservative proxy, we require the gmail tool input
  // to include a `confirmed: true` field OR we prompt the user via ui.confirm.
  async function checkGmail(event: { tool: string; input: Record<string, unknown> }, ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } }) {
    if (!ruleEnabled('gmail-preview')) return null
    if (!GMAIL_SEND_TOOLS.has(event.tool)) return null
    if (event.input.confirmed === true || event.input.preview_shown === true) return null
    const ok = await confirm(ctx, 'gmail-preview', {
      title: 'switchboard-guardrails: send email?',
      message: `To: ${event.input.to ?? '(missing)'}\nSubject: ${event.input.subject ?? '(missing)'}\n\nDid you preview the body in chat first?`,
      confirmButtonText: 'Send',
    })
    if (typeof ok !== 'boolean') return ok
    if (!ok) {
      await logEvent(PLUGIN, { rule: 'gmail-preview', action: 'reject', tool: event.tool })
      return { action: 'reject-and-continue' as const, message: 'Email send blocked: preview the draft in chat first per AGENTS.md.' }
    }
    await logEvent(PLUGIN, { rule: 'gmail-preview', action: 'allowed-after-confirm', tool: event.tool })
    return null
  }

  async function checkGmailBash(cmd: string, ctx: { ui: { confirm: (o: unknown) => Promise<boolean> } }) {
    if (!ruleEnabled('gmail-preview')) return null
    // Only the subcommand token decides; text after a pipe, `&&`, `;`, or
    // inside quoted arguments is never judged (bashCommandStartMatch).
    const subcmd = gmailBashSendSubcommand(cmd)
    if (!subcmd) return null
    const tool = `switchboard mail ${subcmd}`
    const ok = await confirm(ctx, 'gmail-preview', {
      title: 'switchboard-guardrails: send email?',
      message: 'Did you preview the body in chat first?',
      confirmButtonText: 'Send',
    })
    if (typeof ok !== 'boolean') return ok
    if (!ok) {
      await logEvent(PLUGIN, { rule: 'gmail-preview', action: 'reject', tool })
      return { action: 'reject-and-continue' as const, message: 'Email send blocked: preview the draft in chat first per AGENTS.md.' }
    }
    await logEvent(PLUGIN, { rule: 'gmail-preview', action: 'allowed-after-confirm', tool })
    return null
  }

  // ── Rule 6: Calendar canonical rule ──────────────────────────────────
  async function checkCalendar(event: { tool: string; input: Record<string, unknown> }, _ctx: unknown) {
    if (!ruleEnabled('calendar-canonical')) return null
    if (!CAL_CREATE_TOOLS.has(event.tool)) return null
    const title = String(event.input.summary ?? event.input.title ?? '')
    // Treat any title that starts with "P<N>" or contains "hold" as a hold candidate
    const isHold = /^P\d\b/i.test(title) || /\bhold\b/i.test(title)
    if (!isHold) return null
    // Sub-P1 block — explicit P2-P9 prefix
    if (/^P[2-9]\b/i.test(title)) {
      await logEvent(PLUGIN, { rule: 'calendar-canonical', action: 'reject', title })
      return { action: 'reject-and-continue' as const, message: 'Calendar holds are P0/P1 only per AGENTS.md Canonical Calendar Rule.' }
    }
    // Default to 1pm in the owner's time zone (SWITCHBOARD_TZ) if no time given
    if (!event.input.start && !event.input.startTime) {
      const timeZone = ownerTimeZone()
      const requestedDate = String(event.input.dueDate ?? event.input.due_date ?? event.input.date ?? '')
      const date = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate)
        ? requestedDate
        : new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
      const probe = new Date(`${date}T20:00:00Z`)
      const zoneName = new Intl.DateTimeFormat('en-US', { timeZone, timeZoneName: 'longOffset' })
        .formatToParts(probe).find(part => part.type === 'timeZoneName')?.value ?? 'GMT-08:00'
      const offset = zoneName.replace('GMT', '') || '+00:00'
      const startISO = `${date}T13:00:00${offset}`
      await logEvent(PLUGIN, { rule: 'calendar-canonical', action: 'inject-default-time', title })
      return {
        action: 'allow-with-input' as const,
        input: { ...event.input, start: { dateTime: startISO, timeZone } },
      }
    }
    return null
  }

  // ── Rule 7: Workstream auto-archive on status flip ───────────────────
  amp.on('tool.result', async (event, _ctx) => {
    if (!ruleEnabled('workstream-archive')) return
    const files = amp.helpers.filesModifiedByToolCall(event)
    if (!files) return
    for (const uri of files) {
      const path = amp.helpers.filePathFromURI(uri)
      const m = path.match(/workstreams\/([a-z0-9-]+)\/config\.yaml$/)
      if (!m) continue
      const slug = m[1]
      try {
        const { readFile } = await import('node:fs/promises')
        const content = await readFile(path, 'utf8')
        const statusMatch = content.match(/^status:\s*(.+)$/m)
        if (!statusMatch) continue
        const status = statusMatch[1].trim().toLowerCase().replace(/['"]/g, '')
        if (['done', 'cancelled', 'canceled', 'resolved'].includes(status)) {
          await logEvent(PLUGIN, { rule: 'workstream-archive', action: 'trigger', slug, status })
          await runDetached(
            'bash',
            ['-c', `cd ${SWITCHBOARD_REPO} && [ -x system/scripts/archive-workstream.sh ] && bash system/scripts/archive-workstream.sh ${slug} || mv workstreams/${slug} workstreams/_archive/ 2>/dev/null; python3 system/scripts/render-workstreams-readme.py`],
          )
        }
      } catch (error) {
        await logEvent(PLUGIN, {
          rule: 'workstream-archive',
          action: 'error',
          slug,
          error: error instanceof Error ? error.message : String(error),
        })
        // Never disrupt the session
      }
    }
  })
}
