#!/usr/bin/env python3
"""Stop a finished switchboard worker tab from taking on new work.

A worker that reads a cmux message after it has reported done must not act on
it, or it duplicates the continuation CoS has already dispatched for the same
ask. cmux delivers a message to an
idle Claude tab through its Stop hook, so the worker wakes and runs tools as if
the task were still open.

Events:
  pre-tool-use         in a worker tab (SWITCHBOARD_WORKER_TASK_ID set) whose ledger row
                       is terminal, deny every tool except read-only ones and a
                       report-to-cos.sh retry, with a
                       reason that tells the model to reply in one line and stop.
                       Allowed through: a newer non-terminal row bound to this tab
                       (an adopted finished tab: same session or workspace ref),
                       and a turn the owner typed in this tab.
  user-prompt-submit   a prompt the owner typed in a finished worker tab: record it, so the
                       owner can still work there. cmux delivers `agent message` without
                       this event, but a guarded send and every wmux delivery are typed, so
                       they fire it too. A typed delivery is not the owner: a prompt whose
                       hash the mux recorded for this pane (cmux-shim.py note_injected,
                       ~/.switchboard/injected-prompts.jsonl, the log gmail_send_guard.py reads) or
                       one shaped like a callback (`[ROUTER REPORT]`, `[AUTOMATION …]`, a
                       spilled message's pointer line) never lifts the block.

Wiring: hooks.json registers this file for both events where each hook is cheap. Where
hooks/worker-closed-gate.ts exists, the Bun bridge (amp-bridge.ts) runs it instead, and only
when the worker's own row is terminal, so a live worker starts no extra program per tool call
(on Windows every hook costs several program starts).

The report retry allow list: the script must be `<root>/system/scripts/report-to-cos.sh` (or
`running-task-state.py show`) under a trusted root (SWITCHBOARD_REPO, SWITCHBOARD_ROOT, SWITCHBOARD_WORKTREE), with
exactly one `--task-id` naming this task. A double-quoted Windows path
(`bash "C:\\…\\system\\scripts\\report-to-cos.sh" …`) is read literally by bash and passes;
a backslash elsewhere, `$`, backticks, `;`, `||`, redirection, braces, parentheses and globs
outside single quotes are refused, as are unlisted `VAR=value` prefixes.

The ledger is read only, never written. Kill switch:
~/.claude/switchboard-plugin-config.json {"worker-closed.disabled": true}.
Never fails the hook (exit 0 always); prints only the PreToolUse decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

KILL_SWITCH = "worker-closed.disabled"
HOOK_DIR = Path(__file__).resolve().parent
TERMINAL = {"blocked", "deferred", "done", "failed", "cancelled"}
# TaskOutput and BashOutput read a background command's output (a report retry run in the background).
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "ToolSearch", "TaskOutput", "BashOutput"}
# Bash commands a finished worker may still run: a report retry (report-to-cos.sh
# replays a delivered=false or timed-out report) and a read of its own ledger row.
# Matched as the command itself, not as a substring, so `rm -rf x; report-to-cos.sh`
# or `echo > f && report-to-cos.sh` does not pass.
ALLOWED_BASH = ("report-to-cos.sh", "running-task-state.py show")
# Read-only filters a report retry may pipe into, e.g. `... 2>&1 | tail -5`.
PIPE_FILTERS = {"tail", "head", "cat", "jq", "grep"}
WRAPPERS = {"bash", "sh", "zsh", "python3", "python", "env", "command"}
# The only leading VAR=value words a retry may carry. Anything else (PATH, BASH_ENV, PYTHONPATH,
# GIT_CONFIG_*) could make the allowed script run other code.
ALLOWED_ASSIGNMENTS = {"REPORT_TO_COS_NO_HOOKS=1"}
# Inside double quotes bash keeps a backslash literal unless one of these follows it.
DQ_ESCAPED = '$`"\\\n'
# A typed delivery the mux recorded for this pane within this window is not the owner's prompt.
INJECTED_WINDOW_SECONDS = 24 * 3600


def _split_unquoted(command: str) -> list[str] | None:
    """Split on unquoted `&&` and `|`. None when the command has any other shell control
    (`;`, `||`, `&`, `<`, `>` other than `2>&1`, newline, backticks) or any `$` or backslash outside
    single quotes: `$'...'` reads a backslash-escaped quote as a plain character, so a hand-rolled splitter would see a
    separator as quoted while bash runs it, and `$VAR` or `$(...)` expands. The one backslash allowed outside
    single quotes is a literal one inside double quotes (a Windows path), where bash and shlex agree.
    Unquoted `( ) { } * ? [ ]` are refused too: shlex reads `cd ()({bash,-c,'…'})` as `cd` and one word, while bash
    defines a function named cd and brace expansion builds its command; braces and globs also turn one word into
    several (`{--task-id,rt-other}`)."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = command.replace("2>&1", " ")
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"' and ch == "\\" and i + 1 < len(text) and text[i + 1] not in DQ_ESCAPED:
            pass   # `"C:\Users\..."`: literal in bash and in shlex
        elif not quote and ch == "\\" and i + 1 < len(text) and text[i + 1] in "'\"":
            buf.append(ch); buf.append(text[i + 1]); i += 2   # `'the owner'\''s doc'`: a literal quote in both
            continue
        elif ch in "\\`$":
            return None
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif text.startswith("&&", i):
            parts.append("".join(buf)); buf = []; i += 2
            continue
        elif text.startswith("||", i) or ch in ";&<>\n(){}*?[]":
            return None
        elif ch == "|":
            parts.append("".join(buf)); parts.append("|"); buf = []; i += 1
            continue
        buf.append(ch)
        i += 1
    if quote:
        return None
    parts.append("".join(buf))
    return parts


def _words(segment: str) -> list[str]:
    import shlex  # noqa: PLC0415
    try:
        words = shlex.split(segment)
    except ValueError:
        return []
    while words and "=" in words[0] and not words[0].startswith("="):
        if words[0] not in ALLOWED_ASSIGNMENTS:
            return []   # an unlisted assignment refuses the whole segment
        words.pop(0)
    while words and words[0] in WRAPPERS:
        words.pop(0)
    return words


def _norm(path: str) -> str:
    """One spelling for a directory: slashes, no trailing slash, `~` expanded, Git Bash `/c/` as `c:/`, lower case
    (Windows and default macOS volumes ignore case)."""
    path = path.strip().replace("\\", "/")
    if path == "~" or path.startswith("~/"):
        path = Path.home().as_posix() + path[1:]
    if re.match(r"^/[A-Za-z](/|$)", path):
        path = f"{path[1]}:{path[2:] or '/'}"
    while len(path) > 1 and path.endswith("/") and not path.endswith(":/"):
        path = path[:-1]
    return path.lower()


def trusted_roots() -> set[str]:
    """The checkouts whose system/scripts a finished worker may run: the repo the hook was started for and the
    worker's own worktree, never a folder a message names."""
    return {_norm(v) for v in (os.environ.get(k) for k in ("SWITCHBOARD_REPO", "SWITCHBOARD_ROOT", "SWITCHBOARD_WORKTREE")) if v}


def allowed_bash(command: str, task_id: str = "", cwd: str = "") -> bool:
    """True only for `[cd ROOT &&] [VAR=x] [bash] <ROOT>/system/scripts/report-to-cos.sh --task-id <this task> ...` or
    `.../running-task-state.py show ...`, optionally piped into a read-only filter. ROOT is a trusted checkout
    (trusted_roots); a relative `system/scripts/...` counts only when the directory it runs in is one."""
    parts = _split_unquoted(command)
    if not parts:
        return False
    # Pipes: parts look like [seg, "|", seg, "|", seg]; only the last && group may pipe.
    segments: list[str] = []
    piped: list[str] = []
    expect_filter = False
    for part in parts:
        if part == "|":
            piped.append("")
            expect_filter = True
        elif expect_filter:
            piped[-1] = part
            expect_filter = False
        elif piped:
            return False   # `... | tail && more`: nothing may follow a pipe
        else:
            segments.append(part)
    roots = trusted_roots()
    here = _norm(cwd) if cwd else ""
    *setup, main = segments
    for seg in setup:
        words = _words(seg)
        if len(words) != 2 or words[0] != "cd" or _norm(words[1]) not in roots:
            return False
        here = _norm(words[1])
    for seg in piped:
        words = _words(seg)
        if not words or not _trusted_filter(words[0]):
            return False
    words = _words(main)
    if not words:
        return False
    for allowed in ALLOWED_BASH:
        script, *sub = allowed.split()
        # The switchboard script itself (`<trusted root>/system/scripts/<name>`), never any file that shares its name
        # (round 2 cross-model check), wherever a message says it is.
        if _is_switchboard_script(words[0], script, roots, here) and words[1:1 + len(sub)] == sub:
            return script != "report-to-cos.sh" or _own_report(words[1:], task_id)
    return False


def _own_report(args: list[str], task_id: str) -> bool:
    """A report retry for this task only: exactly one --task-id, and it is this tab's (with none, report-to-cos.sh
    falls back to --create-if-missing), and no --create-if-missing."""
    if not task_id or "--create-if-missing" in args:
        return False
    ids: list[str] = []
    for i, arg in enumerate(args):
        if arg == "--task-id":
            ids.append(args[i + 1] if i + 1 < len(args) else "")
        elif arg.startswith("--task-id="):
            ids.append(arg.split("=", 1)[1])
    return ids == [task_id]


def _is_switchboard_script(word: str, script: str, roots: set[str], here: str) -> bool:
    word = word.replace("\\", "/")   # a Windows path, already known to be literal
    if word in (f"system/scripts/{script}", f"./system/scripts/{script}"):
        return here in roots
    return any(_norm(word) == f"{root}/system/scripts/{script}" for root in roots)


def _trusted_filter(word: str) -> bool:
    """A filter by bare name or from the system bin folders, never a same-named file elsewhere."""
    name = os.path.basename(word)
    return name in PIPE_FILTERS and word in (name, f"/usr/bin/{name}", f"/bin/{name}", f"/opt/homebrew/bin/{name}")


def config_path() -> Path:
    override = os.environ.get("SWITCHBOARD_PLUGIN_CONFIG")
    return Path(override) if override else Path.home() / ".claude" / "switchboard-plugin-config.json"


def disabled() -> bool:
    try:
        return json.loads(config_path().read_text()).get(KILL_SWITCH) is True
    except (OSError, ValueError, AttributeError):
        return False


def ledger_path() -> Path:
    override = os.environ.get("RUNNING_TASKS_FILE")
    if override:
        return Path(override)
    root = Path(os.environ.get("SWITCHBOARD_REPO") or os.environ.get("SWITCHBOARD_ROOT") or HOOK_DIR.parents[4])
    return root / "system" / "state" / "running-tasks.json"


def typed_dir() -> Path:
    override = os.environ.get("SWITCHBOARD_WORKER_CLOSED_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "switchboard-worker-closed"


def typed_marker(session: str) -> Path:
    safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_")
    return typed_dir() / f"{safe}.typed"


def injected_key(text: str) -> str:
    """The hash cmux-shim.py records for a line it typed (gmail_send_guard.injected_key): ` ⏎ ` and
    whitespace do not count. Kept here because the hook runs from Claude's plugin copy, not the repo."""
    return hashlib.sha256(re.sub(r"[\s⏎]+", "", text).encode("utf-8")).hexdigest()


def injected_log() -> Path:
    return Path(os.environ.get("SWITCHBOARD_STATE_HOME") or Path.home() / ".switchboard") / "injected-prompts.jsonl"


def was_injected(prompt: str, now: float | None = None) -> bool:
    """True when the mux typed this exact text into this pane in the last day. An entry with no
    pane counts for every pane. An unreadable log reads as no entries."""
    now = time.time() if now is None else now
    own = os.environ.get("WMUX_SURFACE_ID") or os.environ.get("CMUX_SURFACE_ID") or ""
    key = injected_key(prompt)
    try:
        lines = injected_log().read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return False
    for line in reversed(lines):
        try:
            row = json.loads(line)
            if row.get("sha") != key:
                continue
            if own and row.get("surface") and row["surface"] != own:
                continue
            if -60 <= now - float(row["at"]) <= INJECTED_WINDOW_SECONDS:
                return True
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return False


def callback_shaped(prompt: str) -> bool:
    """Every callback and envelope the mux types opens with a bracket (`[ROUTER REPORT]`,
    `[WORKER QUESTION]`, `[AUTOMATION job=…]`, `[<emoji> <Name> · rt-…]`), harness text opens with a
    tag, and a spilled message's pointer line says so in its tail (gmail_send_guard._is_callback)."""
    return prompt.lstrip()[:1] in ("[", "<") or "[full message, " in prompt


def owner_typed(prompt: str) -> bool:
    return bool(prompt.strip()) and not callback_shaped(prompt) and not was_injected(prompt)


def closed_row(task_id: str, session: str) -> dict | None:
    """This tab's own row when it is terminal and no live row has taken the tab over."""
    try:
        rows = json.loads(ledger_path().read_text(encoding="utf-8")).get("tasks") or []
    except (OSError, ValueError, AttributeError):
        return None
    own = next((r for r in rows if isinstance(r, dict) and r.get("id") == task_id), None)
    if not own or str(own.get("status") or "") not in TERMINAL:
        return None
    ref = str(own.get("workspace_ref") or "")
    latest = own
    for row in rows:
        if not isinstance(row, dict) or row.get("id") == task_id:
            continue
        bound = (session and session in {str(row.get("session_id") or ""), str(row.get("thread_id") or "")}) \
            or (ref and ref in {str(row.get("workspace_ref") or ""), str(row.get("adopting_ref") or "")})
        if not bound:
            continue
        if str(row.get("status") or "") not in TERMINAL:
            return None
        if finished_epoch(row) > finished_epoch(latest):
            latest = row   # an adopted tab names the last task it ran, not its first
    return latest


def finished_epoch(row: dict) -> float:
    stamp = str(row.get("finished_at") or row.get("updated_at") or "")
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def reason_text(row: dict) -> str:
    tid = str(row.get("id") or "")
    status = str(row.get("status") or "done")
    return (f"switchboard: task {tid} is already reported {status}; this worker tab takes no new work, and anything done "
            f"here now is lost or duplicates a continuation. Do not retry this tool or any other. The only command "
            f"still allowed is a retry of this task's own report: <repo>/system/scripts/report-to-cos.sh --task-id {tid} "
            f"... with the text in single quotes and no $, ;, || or redirection. Otherwise reply in one line: "
            f"\"{tid} is closed ({status}); a new ask goes through router-dispatch.sh --steer {tid}, which starts a "
            f"continuation.\" Then stop.")


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    try:
        # Bytes, decoded as UTF-8: Claude sends UTF-8, and a cp1252 read on Windows would hash a typed
        # delivery with a dash or a curly quote differently from the mux's log entry.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    task_id = os.environ.get("SWITCHBOARD_WORKER_TASK_ID") or ""
    if not task_id or disabled():
        return 0
    session = str(payload.get("session_id") or os.environ.get("SWITCHBOARD_WORKER_SESSION_ID") or "")
    row = closed_row(task_id, session)
    if row is None:
        return 0
    marker = typed_marker(session) if session else None   # no session: no marker, never a shared one
    if kind == "user-prompt-submit":
        if marker is None or not owner_typed(str(payload.get("prompt") or "")):
            return 0
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        return 0
    if kind != "pre-tool-use":
        return 0
    tool = str(payload.get("tool_name") or "")
    if tool in READ_ONLY_TOOLS:
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "") if tool == "Bash" else ""
    if command and allowed_bash(command, task_id, str(payload.get("cwd") or "")):
        return 0
    try:
        if marker is not None and marker.stat().st_mtime > finished_epoch(row):
            return 0   # the owner typed in this tab after the task closed
    except OSError:
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason_text(row)}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # never fail the hook
        sys.exit(0)
