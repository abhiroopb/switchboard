#!/usr/bin/env python3
"""Claude Code PreToolUse hook: the shell-expressible subset of switchboard-guardrails.

Mirrors rules 1-4 of system/amp-plugins/switchboard-guardrails.ts for the Bash tool:
  1. deny `git add -A` / `--all` / `add .`
  2. deny `git push --force` / `-f` / `--no-verify`
  3. ask before `rm -rf` outside /tmp, $TMPDIR, node_modules, dist/build and
     outside this switchboard checkout (deletes inside the repo are pre-approved)
  4. deny `switchboard slack <write>` whose text does not open with the
     bot prefix `:robot_face:`
  5. deny `organize.py --execute` without an approved plan, or on any copy of
     the script that predates the plan fix (rule 9c in the plugin, on the
     PowerShell tool too). Tests: system/scripts/test_organize_execute_guard.py
  6. deny `switchboard mail send…` from a worker session or worktree, on the
     PowerShell tool too, and a raw Gmail API send from any session. The connector
     runs the full check (system/scripts/gmail_send_guard.py); this layer reads the
     worker markers from the harness's environment, so a command cannot strip them.
     Tests: system/scripts/test_gmail_send_guard.py
  7. (retired) the Downloads read block: agents may read and search files in
     Downloads, text and binary.
  8. deny printing the environment with values, a secret-named variable, or the wmux
     pipe token. Listing names is fine. Tests: system/scripts/test_sensitive_read_guard.py
  9. deny `amp login`/`logout`, and any amp call (in the command or in a script file it runs)
     next to an override of HOME, USERPROFILE, APPDATA, LOCALAPPDATA or an XDG folder, and on
     Windows any amp call while Amp's key file is missing: each starts Amp's device sign-in,
     which opens a tab in the owner's browser. Bash and PowerShell.
     Tests: system/scripts/test_amp_auth_guard.py

Everything else in the plugin (Gmail send confirm modal, calendar, browser lease,
CoS delivery gate, Slack MCP prefix injection) needs the Amp plugin API and is
Amp-only; see adapters/README.md.

Wire-up: .claude/settings.json -> hooks.PreToolUse (matcher "Bash|PowerShell").
Claude passes the tool call as JSON on stdin; this prints a permissionDecision
on stdout. Any other tool, malformed input, or an unmatched command allows.

Self-test:  python3 .claude/hooks/switchboard-guardrails.py --self-test
"""
from __future__ import annotations

import json
import os
import re
import sys

SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|[;|&]|\n)\s*")
GIT_ADD_ALL_RE = re.compile(r"^git\s+add\s+(?:-A\b|--all\b|\.(?:\s|$))")
GIT_PUSH_FORCE_RE = re.compile(r"^git\s+push\b[^\n]*(?:--force\b|--no-verify\b|\s-f\b)")
SLACK_WRITE_RE = re.compile(
    r"^switchboard\s+slack\s+((?:post|send|reply|update|edit)[a-z-]*|write-canvas)\b", re.I
)
GMAIL_SEND_START_RE = re.compile(
    r"^(?:&\s*)?(?:(?:env\b(?:\s+-i|\s+-u\s+\S+|\s+\w+=\S*)*|time|nohup|command|exec)\s+)*(?:\w+=\S*\s+)*"
    r"""(?:(?:bash|sh|zsh|dash|pwsh|powershell|cmd)(?:\.exe)?\s+(?:[-/]\w+\s+)*["']?)?"""
    r"""(?:(?:python[\d.]*|py)(?:\.exe)?\s+(?:-\S+\s+(?:utf8\s+)?)*)?["']?(?:[^\s"']*[\\/])?(?:switchboard(?:\.cmd|\.exe)?|switchboard-shim\.py)["']?\s+"""
    r"mail\s+(?:--timeout(?:=|\s+)\S+\s+)*send(?:-[a-z]+)*(?![\w-])",
    re.I,
)
GMAIL_FLAG_SET_RE = re.compile(r"""^(?:\w+=\S*\s+)*(?:python[\d.]*|py)(?:\.exe)?\s+(?:-\S+\s+(?:utf8\s+)?)*["']?\S*gmail_send_guard\.py["']?\s+--set\b""", re.I)
GMAIL_RAW_SEND_RE = re.compile(r"googleapis\.com/\S*/(?:messages|drafts)/send\b", re.I)
SLACK_BOT_PREFIX = ":robot_face:"
SLACK_PREFIX_INLINE_RE = re.compile(r"""(?:^|["'])[ \t]*""" + re.escape(SLACK_BOT_PREFIX) + r"[ \t]+\S")
RM_RE = re.compile(r"^rm\s+(.+)$")
ARG_RE = re.compile(r""""(?:\\.|[^"])*"|'[^']*'|\S+""")
# The file organizer only runs from an approved plan. A flag check is not
# enough, because an old restored copy ignores --manifest and runs its plain execute anyway, so the
# script itself is checked too. Mirrors system/amp-plugins/switchboard/organize-execute.ts.
ORGANIZE_SAFE_MARKER = "PLAIN_EXECUTE_REFUSAL"
# An old copy runs `execute_flag = "--execute" in sys.argv` as code at the start of a line. A guard or
# test that quotes it in a string (this hook, organize-execute.ts, their tests) is not a copy, so a plain
# substring match is too wide: it blocked every run that named this hook file.
ORGANIZE_OLD_LINE_RE = re.compile(r"""^[ \t]*execute_flag\s*=\s*\(?\s*["']--execute["']\s+in\s+sys\.argv\b""", re.M)
ORGANIZE_NAME_RE = re.compile(r"(?:^|[\\/])organize(?:[_.-][\w.-]*)?\.py(?:\.\w+)?$", re.I)
ORGANIZE_OLD_CALL_RE = re.compile(r"scan_and_organize\(\s*(?:execute\s*=\s*)?True")
ORGANIZE_ASSIGN_RE = re.compile(r"""(?:^|[\s;&])\$?(\w+)\s*=\s*["']?([^"'\s;&|]*organize[^"'\s;&|]*\.py)""", re.I)
ORGANIZE_HEREDOC_RE = re.compile(r"""<<-?\s*["']?(\w+)["']?[^\n]*\n.*?\n\s*\1[ \t]*(?=\n|$)""", re.S)
# Commands that only read or quote their arguments, so naming the script isn't running it.
ORGANIZE_READ_ONLY = {
    "cat", "grep", "egrep", "rg", "echo", "printf", "less", "more", "head", "tail", "sed", "awk", "git", "gh", "ls", "dir", "wc", "diff",
    "type", "get-content", "gc", "select-string", "sls", "write-output", "write-host", "code", "vim", "nano", "stat", "file", "cp", "mv",
}
ORGANIZE_SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd"}
ORGANIZE_EVALUATORS = {"invoke-expression", "iex", "start-process", "saps", "start"}
ORGANIZE_FIX = (
    "Preview with no flags, have the owner review the plan, run `--approve <plan> --approved-by <name>`, "
    "then `--execute --manifest <plan>`. The organizing-downloads skill has the steps."
)

# Inline code passed to an interpreter (-c, -e, a heredoc). Mirrored in system/amp-plugins/switchboard/sensitive-reads.ts.
INTERPRETER_RE = re.compile(r"(?:^|[\s&;|(\"'\\/])(?:python[\dw.]*|py|node|bun|deno|ruby|perl|pwsh|powershell)(?:\.exe)?[\"']?(?=\s)[^\n]*(?:\s-c\b|\s-e\b|\s-command\b|<<)|\[(?:system\.)?io\.file\]::read", re.I)

# wmux puts its pipe token in every shell it starts, so a plain `env` writes it into the transcript.
ENV_SECRET_NAME = r"\w*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)\w*"
ENV_HINT_RE = re.compile(r"\benv\b|printenv|\bexport\b|\bdeclare\b|\btypeset\b|\bset\b|environ|environment|process\.env|getenv|pipe-token|wmux|token|secret|passw|api_?key|credential", re.I)
ENV_REDIRECT_RE = re.compile(r"\s*\d?>>?\s*\S+")
ENV_DUMP_RE = re.compile(
    r"^(?:(?:/usr/bin/)?env(?:\.exe)?(?:\s+(?:-0|--null))?|printenv(?:\.exe)?|export(?:\s+-p)?|declare\s+-[xp]+|typeset\s+-x|set"
    r"|(?:get-childitem|gci|ls|dir|get-item|gi)\s+(?:-path\s+|-literalpath\s+)?[\"']?env:[\\/]?\*?[\"']?"
    r"|cmd(?:\.exe)?\s+/c\s+set)$",
    re.I,
)
# `env | grep -i proxy` shows only what it names. A pattern like wmux, token, or `.` isn't narrowing.
ENV_NARROWING_GREP_RE = re.compile(r"^(?:grep|egrep|rg|select-string|sls|findstr)(?:\s+-\w+)*\s+[\"']?[\w-]{3,}[\"']?\s*$", re.I)
ENV_SECRETISH_RE = re.compile(r"wmux|token|secret|pass|key|auth|cred", re.I)
ENV_INLINE_DUMP_RE = re.compile(r"\bprint\(\s*(?:dict\()?os\.environ\)?\s*\)|json\.dumps\(\s*dict\(os\.environ\)|os\.environ\.items\(\)|console\.log\(\s*process\.env\s*\)|JSON\.stringify\(\s*process\.env\b", re.I)
ENV_INLINE_PRINT_RE = re.compile(r"\bprint\(|console\.log\(|\.write\(", re.I)
ENV_INLINE_SECRET_RE = re.compile(rf"""os\.environ(?:\.get)?[\[(]\s*["']{ENV_SECRET_NAME}|os\.getenv\(\s*["']{ENV_SECRET_NAME}|process\.env\.{ENV_SECRET_NAME}""", re.I)
ENV_PS_INLINE_RE = re.compile(rf"""\[(?:System\.)?Environment\]::GetEnvironmentVariables\(\s*\)(?!\s*\.Keys)|\[(?:System\.)?Environment\]::GetEnvironmentVariable\(\s*["']{ENV_SECRET_NAME}""", re.I)
ENV_NAMES_ONLY_RE = re.compile(
    r"\|\s*cut\s+-d\s*['\"]?=['\"]?\s+-f\s*1\b|\|\s*sed\s+['\"]?s/=\.\*//|\|\s*awk\s+-F\s*['\"]?=['\"]?\s+['\"]\{\s*print\s+\$1\s*\}"
    r"|\|\s*(?:select-object|select)\s+(?:-expandproperty\s+|-property\s+)?(?:name|key)\s*(?:\||$)"
    r"|\|\s*(?:foreach-object|foreach|%)\s+(?:name|key|\{\s*\$_\.(?:name|key)\s*\})|\)\.(?:name|key)\b",
    re.I,
)
# Bash names are upper case by convention, so `$token` in a loop isn't one. $MAX_TOKENS and $TOKENS are counts.
ENV_SECRET_VAR_RE = re.compile(
    r"\$\{?(?!MAX_)[A-Z0-9_]*(?:TOKEN(?!S)|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Z0-9_]*\}?"
    r"|(?i:\$env:(?!MAX_)\w*(?:TOKEN(?!S)|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)\w*)"
)
ENV_PRINTERS = frozenset(("echo", "printf", "write-output", "write-host"))
ENV_READERS = frozenset(("cat", "type", "more", "less", "head", "tail", "get-content", "gc", "strings", "xxd", "get-item", "gi"))
WMUX_TOKEN_FILE_RE = re.compile(r"wmux(?:-[\w-]+)?[\\/]+pipe-token$", re.I)
ENV_FIX = "Read one named variable that isn't a secret, or list names only: `env | cut -d= -f1` or `(Get-ChildItem Env:).Name`."


def deny(reason: str) -> dict:
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


def ask(reason: str) -> dict:
    return {"permissionDecision": "ask", "permissionDecisionReason": reason}


def command_segments(cmd: str) -> list[str]:
    """Each shell command start in a compound command (`a && b; c | d`)."""
    return [s.strip() for s in SEGMENT_SPLIT_RE.split(cmd) if s.strip()]


def _is_temp_or_build(target: str, cmd: str) -> bool:
    if target in ("/tmp", "/private/tmp") or target.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", "$TMPDIR", "${TMPDIR}")):
        return True
    var = re.match(r"^\$\{?(\w+)\}?(?:/|$)", target)
    if var and re.search(r"\b" + re.escape(var.group(1)) + r"=[\"']?\$\(\s*mktemp\b", cmd):
        return True
    return target.startswith(("./node_modules", "node_modules")) or target.endswith(("/dist", "/build"))


def _inside_project(target: str, project_dir: str, cwd: str) -> bool:
    if not project_dir:
        return False
    if target == project_dir or target.startswith(project_dir + "/"):
        return True
    home = os.path.expanduser("~")
    if home and project_dir.startswith(home) and target.startswith("~"):
        return os.path.expanduser(target) == project_dir or os.path.expanduser(target).startswith(project_dir + "/")
    relative = not target.startswith(("/", "~", "$")) and ".." not in target
    return relative and (cwd == project_dir or cwd.startswith(project_dir + "/"))


def rm_rf_unsafe_targets(cmd: str, project_dir: str, cwd: str) -> list[str]:
    unsafe: list[str] = []
    for seg in command_segments(cmd):
        m = RM_RE.match(seg)
        if not m:
            continue
        args = ARG_RE.findall(m.group(1))
        flags = [a for a in args if a.startswith("-")]
        recursive = any(f == "--recursive" or (re.match(r"^-[^-]", f) and "r" in f.lower()) for f in flags)
        force = any(f == "--force" or (re.match(r"^-[^-]", f) and "f" in f.lower()) for f in flags)
        if not (recursive and force):
            continue
        for raw in (a for a in args if not a.startswith("-")):
            target = raw.strip("\"'")
            if not (_is_temp_or_build(target, cmd) or _inside_project(target, project_dir, cwd)):
                unsafe.append(target)
    return unsafe


def _resolve_organize_script(raw: str, cwd: str) -> str:
    home = os.path.expanduser("~")
    path = re.sub(r"\$env:USERPROFILE|%USERPROFILE%|\$\{?HOME\}?", lambda _: home, raw, flags=re.I)
    if path.startswith("~"):
        path = os.path.expanduser(path)
    msys = re.match(r"^/([a-zA-Z])/(.*)$", path)
    if msys and os.name == "nt":
        path = f"{msys.group(1)}:/{msys.group(2)}"
    return path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)


def _parse_shell(command: str) -> list[list[str]]:
    """Commands as token lists. Quoted text stays one token, so a sentence that mentions the script is one argument."""
    segments: list[list[str]] = [[]]
    token: list[str] = []
    has_token = False
    quote = ""

    def flush() -> None:
        nonlocal token, has_token
        if has_token:
            segments[-1].append("".join(token))
        token, has_token = [], False

    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            else:
                token.append(ch)
        elif ch in "\"'":
            quote, has_token = ch, True
        elif ch in ";|&\n(){}":
            flush()
            segments.append([])
        elif ch.isspace():
            flush()
        else:
            token.append(ch)
            has_token = True
    flush()
    return [seg for seg in segments if seg]


def _command_word(token: str) -> str:
    return re.sub(r"\.exe$", "", re.split(r"[\\/]", token)[-1].lower())


def _script_problem(path: str, cwd: str) -> str | None:
    try:
        with open(_resolve_organize_script(path, cwd), encoding="utf-8", errors="ignore") as f:
            if ORGANIZE_SAFE_MARKER not in f.read():
                return "this copy of organize.py predates the approved-plan fix and would ignore --manifest."
    except OSError:
        return "this copy of organize.py could not be read, so it could not be checked."
    return None


def _old_copy_under_other_name(token: str, cwd: str) -> bool:
    if not token.lower().endswith(".py"):
        return False
    try:
        with open(_resolve_organize_script(token, cwd), encoding="utf-8", errors="ignore") as f:
            return bool(ORGANIZE_OLD_LINE_RE.search(f.read()))
    except OSError:
        return False


def _is_old_copy(path: str, cwd: str) -> bool:
    """An unfixed copy may unpack archives even on a preview, so no run of it is safe. An unreadable file isn't proof."""
    return _old_copy_under_other_name(path, cwd)


def _organize_segments_violation(command: str, cwd: str, variables: dict[str, str], depth: int = 0) -> str | None:
    for tokens in _parse_shell(command):
        # Skip environment prefixes and PowerShell's assignment form to find the command itself.
        start = 0
        while start < len(tokens) and (re.match(r"^\w+=", tokens[start]) or tokens[start] == "=" or (start + 1 < len(tokens) and tokens[start + 1] == "=")):
            start += 1
        if start >= len(tokens):
            continue
        word = _command_word(tokens[start])
        rest = tokens[start + 1:]
        if word in ("cd", "set-location", "sl", "chdir", "pushd", "push-location") and rest:
            cwd = _resolve_organize_script(rest[0], cwd)
            continue
        if word in ORGANIZE_READ_ONLY:
            continue
        if depth < 3:
            payload = None
            if word in ORGANIZE_SHELLS and rest and re.match(r"^(?:-\w*c|-c(?:ommand)?|/c)$", rest[0], re.I):
                payload = " ".join(rest[1:])
            elif word in ORGANIZE_EVALUATORS:
                payload = " ".join(t for t in rest if t.lower() not in ("-argumentlist", "-filepath", "-command", "-nonewwindow", "-wait"))
            if payload is not None:
                problem = _organize_segments_violation(payload, cwd, variables, depth + 1)
                if problem:
                    return problem
                continue
        if re.match(r"^python[\dw.]*$|^py$", word) and "-c" in rest:
            code = " ".join(rest[rest.index("-c") + 1:])
            if "organize" in code and "--execute" in code:
                return "running organize.py from `python -c` bypasses the approved plan."
        if re.match(r"^python[\dw.]*$|^py$", word):
            if "-m" in rest:
                module = rest[rest.index("-m") + 1:rest.index("-m") + 2]
                if module and module[0].lower().startswith("organize"):
                    if any(t.replace("\\", "") == "--execute" for t in rest):
                        return "running organize as a module bypasses the approved plan."
                    if _old_copy_under_other_name(module[0] + ".py", cwd):
                        return "this copy of organize.py predates the approved-plan fix, and no run of it is safe."
            if any(_old_copy_under_other_name(t, cwd) for t in rest):
                return "this copy of organize.py predates the approved-plan fix, and no run of it is safe."
        resolved = [variables.get(re.sub(r"^\$\{?(\w+)\}?$", r"\1", t), t) for t in tokens]
        scripts = [t for t in resolved if ORGANIZE_NAME_RE.search(t)]
        executes = any(t.replace("\\", "") == "--execute" for t in resolved)
        if executes and not scripts and any(_old_copy_under_other_name(t, cwd) for t in resolved):
            return "this script is an old copy of organize.py under another name, and it would ignore --manifest."
        if not scripts:
            continue
        if _is_old_copy(scripts[0], cwd):
            return "this copy of organize.py predates the approved-plan fix, and no run of it is safe."
        has_manifest = any(t == "--manifest" or t.lower().startswith("--manifest=") for t in resolved)
        if not has_manifest:
            if executes:
                return "`organize.py --execute` needs `--manifest <approved plan>`."
            if any(re.match(r"^\$\w+$|^@\w+$", t) for t in resolved[resolved.index(scripts[0]) + 1:]):
                return "organize.py's flags are in a variable, so they could not be checked."
        elif executes:
            problem = _script_problem(scripts[0], cwd)
            if problem:
                return problem
    return None


def organize_execute_violation(command: str, cwd: str = "") -> str | None:
    """Why this command may not run the file organizer, or None. Only real runs count, never mentions in text."""
    flat = re.sub(r"[\\`]\r?\n\s*", " ", command)
    if ORGANIZE_OLD_CALL_RE.search(flat) and re.search(r"(?:^|[\s&;|\"'\\/])(?:python[\dw.]*|py)(?:\.exe)?[\"']?\s", flat, re.I):
        return "calling scan_and_organize(execute=True) bypasses the approved plan."
    body = ORGANIZE_HEREDOC_RE.sub("", flat)
    variables = {m.group(1): m.group(2) for m in ORGANIZE_ASSIGN_RE.finditer(body)}
    return _organize_segments_violation(body, cwd, variables)


def _worker_session(cwd: str) -> bool:
    if any(value and (key.startswith("SWITCHBOARD_WORKER") or key == "SWITCHBOARD_WORKTREE") for key, value in os.environ.items()):
        return True
    folder = os.path.abspath(cwd or os.getcwd())
    while True:
        dot_git = os.path.join(folder, ".git")
        if os.path.exists(dot_git):
            return os.path.isfile(dot_git)
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def gmail_send_violation(command: str, cwd: str = "") -> str | None:
    """Only the subcommand at a command start counts, so a report or note that quotes the send
    command stays allowed. The connector still decides for every session this lets through."""
    if GMAIL_RAW_SEND_RE.search(command):
        return "a raw Gmail API send skips the send guard. Use `switchboard mail send` from CoS or Launchpad."
    segments = command_segments(command)
    if any(GMAIL_FLAG_SET_RE.match(s) for s in segments) and _worker_session(cwd):
        return "a worker never changes the machine's email send flag. The owner decides that."
    if any(GMAIL_SEND_START_RE.match(s) for s in segments) and _worker_session(cwd):
        return ("workers never send email. Park the text as a Gmail draft with `switchboard mail draft` "
                "and show it verbatim in the report; CoS sends it on the owner's word.")
    return None


# Rule 9: Amp with a substitute home, or signed out, starts its device sign-in and opens a browser tab
# (a tool run that sets HOME and USERPROFILE to a temp folder opens sign-in tabs in the owner's
# browser). Tests: system/scripts/test_amp_auth_guard.py
# An amp call: the binary by path, a quoted "amp" argv entry, or lowercase `amp <subcommand>`
# (prose such as "Amp session" never counts).
AMP_SUBCOMMANDS = r"(?:-x\b|--\w|-\s|skills?\b|threads?\b|login\b|logout\b|usage\b|tools\b|mcp\b|permissions\b|plugins?\b|config\b|update\b|review\b)"
AMP_CALL_RE = re.compile(
    r"""\.amp[\\/]+bin[\\/]+amp\b|\bamp\.exe\b|\[\s*["']amp["']\s*,|which\(\s*["']amp["']\s*\)"""
    r"""|(?:^|[\s;&|(`"'])amp\s+""" + AMP_SUBCOMMANDS,
    re.M,
)
AMP_SIGN_IN_RE = re.compile(r"""(?:^|[\s;&|(`"'])amp(?:\.exe)?["']?\s+(?:login|logout)\b""", re.M)
HOME_VARS = r"(?:HOME|USERPROFILE|HOMEPATH|HOMEDRIVE|XDG_DATA_HOME|XDG_CONFIG_HOME|APPDATA|LOCALAPPDATA)"
# Variable names are case-sensitive, so a key map's "home" never counts, and `HOME="${HOME:-x}"`
# (keep the real value) is not an override.
HOME_OVERRIDE_RE = re.compile(
    r"(?<![\w$.:])(?P<v>" + HOME_VARS + r")=(?!=)(?![\"']?\$\{?(?P=v)\b)"   # shell VAR=, Python kwarg HOME=...
    r"|(?i:\$env:)" + HOME_VARS + r"\s*=(?!=)"                               # PowerShell
    r"""|["']""" + HOME_VARS + r"""["']\s*(?:\]\s*=(?!=)|:)"""                # env['HOME'] = ..., {'HOME': ...}
    r"|\benv\b[^\n;|&]*\s-u\s*" + HOME_VARS + r"\b"                          # env -u HOME
    r"|\b(?:putenv|setdefault|update)\(\s*[\"']?" + HOME_VARS + r"\b",
)
AMP_NEAR_LINES = 15  # an override counts only this close to the amp call
SCRIPT_RUNNERS = frozenset(("python", "python3", "py", "bash", "sh", "node", "bun", "pwsh", "powershell", "deno"))
AMP_SCAN_EXEMPT = frozenset(("test_amp_auth_guard.py", "switchboard-guardrails.py"))
SCRIPT_EXT_RE = re.compile(r"\.(?:py|sh|js|mjs|cjs|ts|ps1)$", re.I)
AMP_FIX = ("Run amp with the real home and point the test at a temp project folder (cwd) instead, or ask the "
           "owner through ask-cos.sh. Never sign in to anything.")


def _amp_secrets_present() -> bool:
    """Amp keeps its key in a file on Windows; macOS may use the keychain, so only Windows is checked."""
    if os.name != "nt":
        return True
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.isfile(os.path.join(data, "amp", "secrets.json"))


def _amp_text_problem(text: str, sign_in: bool = True) -> str | None:
    if sign_in and AMP_SIGN_IN_RE.search(text):
        return "agents never run `amp login` or `amp logout`: Amp's sign-in opens a browser tab in the owner's browser."
    lines = text.splitlines()
    calls = [i for i, line in enumerate(lines) if AMP_CALL_RE.search(line)]
    overrides = [i for i, line in enumerate(lines) if HOME_OVERRIDE_RE.search(line)] if calls else []
    if any(abs(c - o) <= AMP_NEAR_LINES for c in calls for o in overrides):
        return ("amp with an overridden HOME, USERPROFILE, APPDATA, or XDG folder finds no stored key, starts its "
                "device sign-in, and opens a browser tab in the owner's browser.")
    return None


def _scripts_run(command: str, cwd: str) -> list[str]:
    """Script files a command runs through an interpreter, resolved against any `cd` earlier in it."""
    found: list[str] = []
    here = cwd
    for seg in _parse_shell(command):
        word = _command_word(seg[0])
        if word == "cd" and len(seg) > 1:
            here = _resolve_organize_script(os.path.expandvars(seg[1]), here)
            continue
        if word not in SCRIPT_RUNNERS and not re.match(r"python[\d.]+$", word):
            continue
        for token in seg[1:]:
            if token.startswith("-"):
                continue
            if SCRIPT_EXT_RE.search(token):
                found.append(_resolve_organize_script(os.path.expandvars(token), here))
            break
    return found


def amp_auth_violation(command: str, cwd: str = "") -> str | None:
    for seg in _parse_shell(command):  # tokens, so a commit message that mentions `amp login` stays allowed
        words = [t for t in seg if not re.match(r"^\w+=", t)]
        while words and _command_word(words[0]) in ("env", "timeout", "nohup", "command", "exec", "&") or (words and re.match(r"^-|^\d+[smh]?$", words[0])):
            words = words[1:]
        if len(words) > 1 and _command_word(words[0]) == "amp" and words[1].lower() in ("login", "logout"):
            return "agents never run `amp login` or `amp logout`: Amp's sign-in opens a browser tab in the owner's browser."
    problem = _amp_text_problem(command, sign_in=False)
    if problem:
        return problem
    for path in _scripts_run(command, cwd):
        if os.path.basename(path) in AMP_SCAN_EXEMPT:  # its own tests quote the commands it refuses
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                problem = _amp_text_problem(f.read(400_000))
        except OSError:
            continue
        if problem:
            return f"{os.path.basename(path)} runs " + problem
    if AMP_CALL_RE.search(command) and not _amp_secrets_present():
        return "Amp is signed out on this machine, so any amp call starts a sign-in that opens a browser tab."
    return None


def _split_unquoted(text: str, separators: tuple[str, ...] = ("&&", "||", ";", "\n")) -> list[str]:
    """Split where a separator sits outside quotes, so a commit message holding `; env;` stays one statement."""
    parts: list[str] = []
    buffer: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            quote = "" if ch == quote else quote
            buffer.append(ch)
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buffer.append(ch)
            i += 1
            continue
        sep = next((s for s in separators if text.startswith(s, i)), None)
        if sep:
            parts.append("".join(buffer))
            buffer = []
            i += len(sep)
        else:
            buffer.append(ch)
            i += 1
    parts.append("".join(buffer))
    return [p.strip() for p in parts if p.strip()]


def _shell_payload(rest: list[str]) -> str | None:
    """What a shell was told to run, past any flags in front of -c. None when it runs a script file."""
    for i, token in enumerate(rest):
        if re.match(r"^(?:-\w*c|-c(?:ommand)?|/c)$", token, re.I):
            return " ".join(rest[i + 1:])
        if not token.startswith(("-", "/")):
            return None
    return None


def env_dump_violation(command: str, depth: int = 0) -> str | None:
    """Why this command would print secret values from the environment, or None. A names-only view is fine."""
    if not ENV_HINT_RE.search(command):
        return None
    flat = re.sub(r"[\\`]\r?\n\s*", " ", command)
    if INTERPRETER_RE.search(flat) and (ENV_INLINE_DUMP_RE.search(flat) or (ENV_INLINE_PRINT_RE.search(flat) and ENV_INLINE_SECRET_RE.search(flat))):
        return "this prints a secret from the environment."
    for statement in _split_unquoted(ORGANIZE_HEREDOC_RE.sub("", flat)):
        if ENV_NAMES_ONLY_RE.search(statement):
            continue
        stages = _split_unquoted(statement, ("|",))
        first = ENV_REDIRECT_RE.sub("", stages[0]).strip()
        first = first.lstrip("(").rstrip(")").strip()
        parsed = _parse_shell(first)
        tokens = parsed[0] if parsed else []
        start = 0
        while start < len(tokens) and re.match(r"^\w+=", tokens[start]):
            start += 1
        tokens = tokens[start:]
        if not tokens:
            continue
        word, rest = _command_word(tokens[0]), tokens[1:]
        if word in ORGANIZE_SHELLS and depth < 3:
            payload = _shell_payload(rest)
            if payload is not None:
                problem = env_dump_violation(payload, depth + 1)
                if problem:
                    return problem
                continue
        if ENV_DUMP_RE.match(first):
            narrowed = len(stages) > 1 and ENV_NARROWING_GREP_RE.match(stages[1]) and not ENV_SECRETISH_RE.search(stages[1])
            if not narrowed:
                return "this prints every environment variable with its value."
            continue
        if word == "wmux" and rest[:1] == ["token"] or word in ("wmux.cmd",) and rest[:1] == ["token"]:
            return "this prints the wmux pipe token."
        if word in ENV_READERS and any(WMUX_TOKEN_FILE_RE.search(t) for t in rest):
            return "this prints the wmux pipe token."
        if word in ORGANIZE_READ_ONLY - ENV_PRINTERS - ENV_READERS or word in ("git", "gh"):
            continue
        if word in ENV_PRINTERS and any(ENV_SECRET_VAR_RE.search(t) for t in rest):
            return "this prints a secret from the environment."
        if word == "printenv" and any(re.fullmatch(ENV_SECRET_NAME, t, re.I) and not re.search(r"MAX_|TOKENS", t, re.I) for t in rest):
            return "this prints a secret from the environment."
        if len(tokens) == 1 and ENV_SECRET_VAR_RE.fullmatch(tokens[0]):
            return "this prints a secret from the environment."
        if word in ENV_READERS and any(re.fullmatch(rf"env:{ENV_SECRET_NAME}", t, re.I) for t in rest):
            return "this prints a secret from the environment."
        if ENV_PS_INLINE_RE.search(statement):
            return "this prints a secret from the environment."
    return None


def sensitive_read_decision(tool: str, tool_input: dict, cwd: str) -> dict | None:
    """The environment read guard for the shell tools. Reading files in Downloads is allowed."""
    if tool in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        if isinstance(command, str):
            problem = env_dump_violation(command)
            if problem:
                return deny(f"Blocked by switchboard-guardrails: {problem} {ENV_FIX}")
    return None


def decide(command: str, project_dir: str = "", cwd: str = "") -> dict | None:
    violation = organize_execute_violation(command, cwd)
    if violation:
        return deny(f"Blocked by switchboard-guardrails: {violation} {ORGANIZE_FIX}")
    violation = gmail_send_violation(command, cwd)
    if violation:
        return deny(f"Blocked by switchboard-guardrails: {violation}")
    violation = amp_auth_violation(command, cwd)
    if violation:
        return deny(f"Blocked by switchboard-guardrails: {violation} {AMP_FIX}")
    segments = command_segments(command)
    if any(GIT_ADD_ALL_RE.match(s) for s in segments):
        return deny("Blocked by switchboard-guardrails: never use `git add -A`/`--all`/`add .`. Stage explicit paths instead.")
    if any(GIT_PUSH_FORCE_RE.match(s) for s in segments):
        return deny("Blocked by switchboard-guardrails: `git push --force`/`--no-verify` is destructive. Ask the owner before bypassing safety.")
    if any(SLACK_WRITE_RE.match(s) for s in segments) and not SLACK_PREFIX_INLINE_RE.search(command):
        return deny(f"Blocked by switchboard-guardrails: bot-authored Slack text must open with `{SLACK_BOT_PREFIX} ` on the same line (AGENTS.md Bot Attribution Prefix).")
    unsafe = rm_rf_unsafe_targets(command, project_dir, cwd)
    if unsafe:
        return ask(f"switchboard-guardrails: confirm destructive delete of {', '.join(unsafe)}")
    return None


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    cwd = str(payload.get("cwd") or "")
    decision = sensitive_read_decision(tool, tool_input, cwd) if isinstance(tool_input, dict) else None
    if decision is None:
        if tool not in ("Bash", "PowerShell"):
            return 0
        command = tool_input.get("command")
        if not isinstance(command, str):
            return 0
        if tool == "PowerShell":  # the other rules are written for POSIX shell syntax
            violation = organize_execute_violation(command, cwd)
            decision = deny(f"Blocked by switchboard-guardrails: {violation} {ORGANIZE_FIX}") if violation else None
            violation = None if decision else gmail_send_violation(command, cwd)
            decision = deny(f"Blocked by switchboard-guardrails: {violation}") if violation else decision
            violation = None if decision else amp_auth_violation(command, cwd)
            decision = deny(f"Blocked by switchboard-guardrails: {violation} {AMP_FIX}") if violation else decision
        else:
            decision = decide(command, project_dir.rstrip("/"), cwd)
    if decision is None:
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", **decision}}
    json.dump(out, sys.stdout)
    return 0


def self_test() -> int:
    cases = [
        ("git status", None),
        ("git add -A", "deny"),
        ("git add --all && git commit -m x", "deny"),
        ("git add . ", "deny"),
        ("git add ./src/file.py", None),
        ("git push origin main", None),
        ("git push --force origin main", "deny"),
        ("git push -f", "deny"),
        ("git push --no-verify", "deny"),
        ("rm -rf /tmp/scratch", None),
        ("rm -rf node_modules", None),
        ("rm -rf ~/Documents/x", "ask"),
        ("rm -r build && rm -rf /opt/thing", "ask"),
        ("switchboard slack send --channel C1 --text 'hello'", "deny"),
        ("switchboard slack send --channel C1 --text ':robot_face: hello'", None),
        ("switchboard slack read --channel C1", None),
        ("python3 organize.py --execute", "deny"),
        ("python3 organize.py", None),
        ("grep 'organize.py --execute' notes.md", None),
        ("git commit -m 'note; python organize.py --execute is refused'", None),
    ]
    failures = 0
    for cmd, expected in cases:
        got = decide(cmd, project_dir="/switchboard", cwd="/switchboard")
        got_kind = got["permissionDecision"] if got else None
        status = "ok  " if got_kind == expected else "FAIL"
        failures += got_kind != expected
        print(f"{status} {cmd!r} -> {got_kind}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
