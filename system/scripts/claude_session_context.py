#!/usr/bin/env python3
"""Context size of the Claude Code session running in a cmux workspace.

Used by lib/automation-dispatch.sh to rotate ⏰ Scheduled Jobs with /clear
before its context grows large. A session near Claude's auto-compact point
can strand an automation paste and stall the tab, and pastes right after an
auto-compact may never reach the session.

    claude_session_context.py tokens --workspace-ref workspace:N
        prints {"workspace_ref", "workspace_id", "session_id", "tokens", "at"}
        exit 0 with tokens, 1 when the session or a usage record is unknown.

Resolution: cmux `workspace list --json` maps the ref to its UUID; cmux's
Claude hook store (~/.cmuxterm/claude-hook-sessions.json,
activeSessionsByWorkspace) maps the UUID to the live session id; the session
transcript (~/.claude/projects/*/<session>.jsonl) gives the last main-thread
assistant usage, read from the file tail only. Tokens are input +
cache_read + cache_creation, the prompt size Claude reports per turn.

Test overrides: CLAUDE_CONTEXT_WORKSPACES_JSON (a file holding cmux's list
JSON), CLAUDE_HOOK_SESSIONS_FILE, CLAUDE_PROJECTS_DIR.
Read-only. Never raises; any failure prints nothing useful and exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SESSION_ID = re.compile(r"^[0-9a-fA-F-]{16,64}$")
TAIL_BYTES = 4 * 1024 * 1024


def _cmux_bin() -> str:
    for candidate in (os.environ.get("CMUX"), os.environ.get("CMUX_BIN")):
        if candidate:
            return candidate
    app = "/Applications/cmux.app/Contents/Resources/bin/cmux"
    return app if Path(app).exists() else "cmux"


def workspace_id(ref: str) -> str | None:
    override = os.environ.get("CLAUDE_CONTEXT_WORKSPACES_JSON")
    try:
        if override:
            data = json.loads(Path(override).read_text(encoding="utf-8"))
        else:
            out = subprocess.run([_cmux_bin(), "workspace", "list", "--json"], capture_output=True,
                                 text=True, timeout=10, env={**os.environ, "CMUX_QUIET": "1"})
            data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for ws in (data or {}).get("workspaces") or []:
        if isinstance(ws, dict) and ws.get("ref") == ref and ws.get("id"):
            return str(ws["id"])
    return None


def session_for_workspace(ws_id: str) -> str | None:
    path = Path(os.environ.get("CLAUDE_HOOK_SESSIONS_FILE")
                or Path.home() / ".cmuxterm" / "claude-hook-sessions.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("activeSessionsByWorkspace") or {}).get(ws_id) or {}
    sid = str(entry.get("sessionId") or "") if isinstance(entry, dict) else ""
    return sid if SESSION_ID.match(sid) else None


def transcript(session_id: str) -> Path | None:
    root = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return None
    for folder in root.iterdir():
        candidate = folder / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def last_usage(path: Path) -> tuple[int, str] | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()  # drop the partial first line
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        usage = (record.get("message") or {}).get("usage") if isinstance(record.get("message"), dict) else None
        if not isinstance(usage, dict):
            continue
        tokens = sum(int(usage.get(key) or 0) for key in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return tokens, str(record.get("timestamp") or "")
    return None


def context_for_ref(ref: str) -> dict[str, Any] | None:
    ws_id = workspace_id(ref)
    sid = session_for_workspace(ws_id) if ws_id else None
    path = transcript(sid) if sid else None
    usage = last_usage(path) if path else None
    if not usage:
        return None
    return {"workspace_ref": ref, "workspace_id": ws_id, "session_id": sid,
            "tokens": usage[0], "at": usage[1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    tokens = sub.add_parser("tokens", help="print the session's current context size")
    tokens.add_argument("--workspace-ref", required=True)
    args = parser.parse_args(argv)
    result = context_for_ref(args.workspace_ref)
    if not result:
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
