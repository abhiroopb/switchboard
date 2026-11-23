#!/usr/bin/env python3
"""Stop a finished switchboard worker tab from taking on new work.

A worker that reads a cmux message after it has reported done must not act on
it, or it duplicates the continuation CoS has already dispatched for the same
ask. cmux delivers a message to an
idle Claude tab through its Stop hook, so the worker wakes and runs tools as if
the task were still open.

Events (hooks.json):
  pre-tool-use         in a worker tab (SWITCHBOARD_WORKER_TASK_ID set) whose ledger row
                       is terminal, deny every tool except read-only ones and a
                       report-to-cos.sh retry, with a
                       reason that tells the model to reply in one line and stop.
                       Allowed through: a newer non-terminal row bound to this tab
                       (an adopted finished tab: same session or workspace ref),
                       and a turn the owner typed in this tab.
  user-prompt-submit   a typed prompt (cmux messages never fire this event) in a
                       finished worker tab: record it, so the owner can still work there.

The ledger is read only, never written. Kill switch:
~/.claude/switchboard-plugin-config.json {"worker-closed.disabled": true}.
Never fails the hook (exit 0 always); prints only the PreToolUse decision.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

KILL_SWITCH = "worker-closed.disabled"
HOOK_DIR = Path(__file__).resolve().parent
TERMINAL = {"blocked", "deferred", "done", "failed", "cancelled"}
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "ToolSearch"}
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


def _split_unquoted(command: str) -> list[str] | None:
    """Split on unquoted `&&` and `|`. None when the command has any other shell control
    (`;`, `||`, `&`, `<`, `>` other than `2>&1`, newline, backticks) or any `$` or backslash outside
    single quotes: `$'...'` reads a backslash-escaped quote as a plain character, so a hand-rolled splitter would see a
    separator as quoted while bash runs it, and `$VAR` or `$(...)` expands."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    text = command.replace("2>&1", " ")
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch in "\\`$":
            return None
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif text.startswith("&&", i):
            parts.append("".join(buf)); buf = []; i += 2
            continue
        elif text.startswith("||", i) or ch in ";&<>\n":
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


def allowed_bash(command: str, task_id: str = "") -> bool:
    """True only for `[cd DIR &&] [VAR=x] [bash] .../report-to-cos.sh ...` or
    `.../running-task-state.py show ...`, optionally piped into a read-only filter."""
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
    *setup, main = segments
    for seg in setup:
        words = _words(seg)
        if not words or words[0] != "cd" or len(words) > 2:
            return False
    for seg in piped:
        words = _words(seg)
        if not words or not _trusted_filter(words[0]):
            return False
    words = _words(main)
    if not words:
        return False
    for allowed in ALLOWED_BASH:
        script, *sub = allowed.split()
        # The switchboard script itself (`system/scripts/<name>`, relative or absolute), never any file that shares
        # its name (round 2 cross-model check).
        if _is_switchboard_script(words[0], script) and words[1:1 + len(sub)] == sub:
            return script != "report-to-cos.sh" or _own_report(words[1:], task_id)
    return False


def _own_report(args: list[str], task_id: str) -> bool:
    """A report retry for this task only: no --create-if-missing, and any --task-id is this tab's."""
    if "--create-if-missing" in args:
        return False
    for i, arg in enumerate(args):
        value = args[i + 1] if arg == "--task-id" and i + 1 < len(args) else (
            arg.split("=", 1)[1] if arg.startswith("--task-id=") else None)
        if value is not None and value != task_id:
            return False
    return True


def _is_switchboard_script(word: str, script: str) -> bool:
    return word == f"system/scripts/{script}" or word.endswith(f"/system/scripts/{script}")


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
            f"here now is lost or duplicates a continuation. Do not retry this tool or any other. Reply in one line: "
            f"\"{tid} is closed ({status}); a new ask goes through router-dispatch.sh --steer {tid}, which starts a "
            f"continuation.\" Then stop.")


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
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
        if marker is None:
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
    if command and allowed_bash(command, task_id):
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
