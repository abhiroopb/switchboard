#!/usr/bin/env python3
"""PreToolUse hook for mcp__claude-in-chrome__*: runs system/scripts/claude_in_chrome_guard.py.

Claude Code runs this plugin from its installed copy (~/.claude/plugins/cache/...), so the
repo is found the way surface-context.py finds it: SWITCHBOARD_REPO, then SWITCHBOARD_ROOT, then five
folders up from this file (the in-repo copy). A missing guard refuses the call (fails closed).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(os.environ.get("SWITCHBOARD_REPO") or os.environ.get("SWITCHBOARD_ROOT") or HOOK_DIR.parents[4])
GUARD = REPO_ROOT / "system" / "scripts" / "claude_in_chrome_guard.py"


def deny(reason: str) -> int:
    """Exit 2 with the reason on stderr: Claude Code blocks the call even if stdout is lost."""
    text = f"claude-in-chrome guard: {reason}"
    try:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": text}}))
    finally:
        print(text, file=sys.stderr)
    return 2


def main() -> int:
    data = sys.stdin.buffer.read()  # bytes in, bytes out: no console code page can break the hand-off
    try:
        tool = str(json.loads(data.decode("utf-8", "replace") or "{}").get("tool_name") or "")
    except (ValueError, AttributeError):
        return deny("unreadable hook input, so the call is refused.")
    if not tool.startswith("mcp__claude-in-chrome__"):
        return 0
    if not GUARD.is_file():
        return deny(f"guard script not found at {GUARD}, so the call is refused.")
    try:
        result = subprocess.run([sys.executable, str(GUARD), "pre-tool-use"], input=data, capture_output=True,
                                timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return deny(f"guard did not finish ({type(exc).__name__}), so the call is refused.")
    if result.returncode != 0:
        return deny("guard exited with an error, so the call is refused.")
    sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # fail closed on anything else
        sys.exit(deny(f"wrapper failed ({type(exc).__name__}), so the call is refused."))
