#!/usr/bin/env python3
"""Keep the cmux ⚡ lit on a Claude tab that is still working.

The cmux sidebar (`~/.config/cmux/sidebars/workspaces.swift`, from the
cmux-sidebar repo) lights ⚡ on a Claude row when cmux's agent feed says the
session is `working` AND the feed heard a hook in the last 20 minutes
(`agentWorkingFresh`, the guard against a dropped Stop that would leave ⚡ lit
on an idle tab). cmux 0.65.2's own PreToolUse hook sends each tool call with
`cmux hooks enqueue claude pre-tool-use`, a 0.5 s timeout, and no retry. About
half of those calls fail with "Failed to write to socket (Broken pipe, errno
32)" and the hook swallows the error. cmux forwards no PostToolUse, so a worker
that polls with 10-minute `sleep` calls can lose two in a row and go silent for
20+ minutes: ⚡ goes out while the turn is still running and the pill still
says "Running".

This hook resends that PreToolUse with retries, so the feed hears the session
at least every few minutes while tools run.

Events (hooks.json):
  pre-tool-use         send `cmux hooks enqueue claude pre-tool-use` with the
                       hook payload, up to 4 tries inside a 3 s budget, at most
                       once per THROTTLE_SECONDS per session, and not again
                       for FAIL_BACKOFF_SECONDS after a failed send (a hung
                       cmux socket must not cost every tool call 3 s). A delivered event
                       also turns a session that cmux saw stop back to working,
                       which covers a turn that continues through a Stop hook
                       (no new UserPromptSubmit).
  user-prompt-submit   clear the throttle stamp, so the first tool call of the
  stop                 next turn (or of a Stop-hook continuation) always sends.

wmux (Windows): wmux has no feed to keep fresh. Its own Claude hooks
(wmux-hook.js in ~/.claude/settings.json) report run state and blocked
prompts, so the heartbeat does nothing when they are installed. Without them
it sets the pane's run state with `wmux report-agent --run-depth 1` on
pre-tool-use and user-prompt-submit and `--run-depth 0` on stop, throttled the
same way. An absolute depth, never --run-start/--run-end, so a lost event
cannot leave the count high.

Skipped: outside cmux (no CMUX_SURFACE_ID), CMUX_CLAUDE_HOOKS_DISABLED=1, the
interactive tools cmux renders as feed cards (AskUserQuestion, ExitPlanMode,
EnterPlanMode), and no cmux CLI on PATH.

Kill switch: ~/.claude/switchboard-plugin-config.json {"cmux-heartbeat.disabled": true}.
Never writes stdout; never fails the hook (exit 0 always).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KILL_SWITCH = "cmux-heartbeat.disabled"
THROTTLE_SECONDS = 90
ATTEMPTS = 4
BUDGET_SECONDS = 3.0
RETRY_PAUSE_SECONDS = 0.15
FAIL_BACKOFF_SECONDS = 30
PRUNE_AFTER_SECONDS = 86400
SKIP_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


def config_path() -> Path:
    override = os.environ.get("SWITCHBOARD_PLUGIN_CONFIG")
    return Path(override) if override else Path.home() / ".claude" / "switchboard-plugin-config.json"


def disabled() -> bool:
    try:
        return json.loads(config_path().read_text()).get(KILL_SWITCH) is True
    except (OSError, ValueError, AttributeError):
        return False


def state_dir() -> Path:
    override = os.environ.get("SWITCHBOARD_CMUX_HEARTBEAT_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "switchboard-cmux-heartbeat"


def stamp_path(session_id: str) -> Path:
    return state_dir() / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) or "unknown")


def cmux_bin() -> str | None:
    for key in ("CMUX_CLAUDE_HOOK_CMUX_BIN", "CMUX_BUNDLED_CLI_PATH"):
        path = os.environ.get(key, "")
        if path and os.access(path, os.X_OK):
            return path
    return shutil.which("cmux")


def in_cmux() -> bool:
    return bool(os.environ.get("CMUX_SURFACE_ID")) and os.environ.get("CMUX_CLAUDE_HOOKS_DISABLED") != "1"


def fail_path(session_id: str) -> Path:
    return stamp_path(session_id).with_name(stamp_path(session_id).name + ".fail")


def younger_than(path: Path, seconds: float, now: float) -> bool:
    try:
        return now - path.stat().st_mtime < seconds
    except OSError:
        return False


def touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def prune(now: float) -> None:
    """Drop stamps of sessions that ended more than a day ago."""
    try:
        for path in state_dir().iterdir():
            if not younger_than(path, PRUNE_AFTER_SECONDS, now):
                unlink(path)
    except OSError:
        pass


def clear_stamp(session_id: str) -> None:
    unlink(stamp_path(session_id))
    unlink(fail_path(session_id))


def enqueue(cli: str, payload: dict) -> bool:
    """Send one pre-tool-use event to cmux; retry the intermittent socket failures."""
    env = dict(os.environ)
    # Same pid attribution as cmux's own hook: ${CMUX_CLAUDE_PID:-$PPID}.
    env["CMUX_CLAUDE_PID"] = env.get("CMUX_CLAUDE_PID") or str(os.getppid())
    env["CMUXTERM_CLI_RESPONSE_TIMEOUT_SEC"] = "2"
    cmd = [cli]
    if env.get("CMUX_SOCKET_PATH"):
        cmd += ["--socket", env["CMUX_SOCKET_PATH"]]
    cmd += ["hooks", "enqueue", "claude", "pre-tool-use"]
    data = json.dumps(payload)
    deadline = time.monotonic() + BUDGET_SECONDS
    for attempt in range(ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(cmd, input=data, text=True, capture_output=True,
                                    env=env, timeout=remaining)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if attempt < ATTEMPTS - 1:
            time.sleep(RETRY_PAUSE_SECONDS)
    return False


def in_wmux() -> bool:
    return (os.environ.get("WMUX") == "1" and bool(os.environ.get("WMUX_SURFACE_ID"))
            and os.environ.get("CMUX_CLAUDE_HOOKS_DISABLED") != "1")


def wmux_hooks_installed() -> bool:
    """wmux's own Claude hooks already report run state for this pane."""
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    try:
        return "wmux-hook" in settings.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def wmux_argv() -> tuple[list[str], dict]:
    node, cli = os.environ.get("WMUX_NODE"), os.environ.get("WMUX_CLI")
    if node and cli:
        return [node, cli], ({"ELECTRON_RUN_AS_NODE": "1"} if os.environ.get("WMUX_NODE_ELECTRON") == "1" else {})
    explicit = os.environ.get("WMUX_BIN")
    if explicit:
        return [explicit], {}
    found = shutil.which("wmux") or shutil.which("wmux.cmd")
    return ([found], {}) if found else ([], {})


def wmux_report(depth: int) -> bool:
    argv, extra = wmux_argv()
    if not argv:
        return False
    try:
        result = subprocess.run([*argv, "report-agent", "--run-depth", str(depth),
                                 "--surface", os.environ["WMUX_SURFACE_ID"]],
                                # No pipes: killing wmux.cmd leaves its child alive on Windows,
                                # and a captured pipe would then outlast the timeout.
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=BUDGET_SECONDS,
                                stdin=subprocess.DEVNULL, env={**os.environ, **extra})
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def handle_wmux(kind: str, session_id: str, payload: dict, now: float) -> str:
    if wmux_hooks_installed():
        return "skip:wmux-hooks"
    if kind == "stop":
        clear_stamp(session_id)
        return "sent" if wmux_report(0) else "failed"
    if kind == "user-prompt-submit":
        clear_stamp(session_id)
    elif str(payload.get("tool_name") or "") in SKIP_TOOLS:
        return "skip:interactive-tool"
    elif younger_than(stamp_path(session_id), THROTTLE_SECONDS, now):
        return "throttled"
    if younger_than(fail_path(session_id), FAIL_BACKOFF_SECONDS, now):
        return "backoff"
    if wmux_report(1):
        touch(stamp_path(session_id))
        unlink(fail_path(session_id))
        return "sent"
    touch(fail_path(session_id))
    return "failed"


def handle(kind: str, payload: dict, now: float | None = None) -> str:
    """Return what happened: sent, failed, throttled, backoff, cleared, or skip:<reason>."""
    if disabled():
        return "skip:disabled"
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return "skip:no-session"
    if in_wmux() and not in_cmux() and kind in ("pre-tool-use", "user-prompt-submit", "stop"):
        return handle_wmux(kind, session_id, payload, time.time() if now is None else now)
    if kind in ("user-prompt-submit", "stop"):
        clear_stamp(session_id)
        return "cleared"
    if kind != "pre-tool-use":
        return "skip:event"
    if not in_cmux():
        return "skip:not-cmux"
    if str(payload.get("tool_name") or "") in SKIP_TOOLS:
        return "skip:interactive-tool"
    now = time.time() if now is None else now
    if younger_than(stamp_path(session_id), THROTTLE_SECONDS, now):
        return "throttled"
    if younger_than(fail_path(session_id), FAIL_BACKOFF_SECONDS, now):
        return "backoff"
    cli = cmux_bin()
    if not cli:
        return "skip:no-cmux-cli"
    body = dict(payload)
    body["hook_event_name"] = "PreToolUse"
    if enqueue(cli, body):
        touch(stamp_path(session_id))
        unlink(fail_path(session_id))
        prune(now)
        return "sent"
    touch(fail_path(session_id))
    return "failed"


def main(argv: list[str]) -> int:
    kind = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    try:
        outcome = handle(kind, payload)
        if outcome == "failed":
            sys.stderr.write("[cmux-heartbeat] cmux did not accept the pre-tool-use heartbeat\n")
        elif os.environ.get("SWITCHBOARD_CMUX_HEARTBEAT_DEBUG"):
            sys.stderr.write(f"[cmux-heartbeat] {kind}: {outcome}\n")
    except Exception as exc:  # a heartbeat must never break a tool call
        sys.stderr.write(f"[cmux-heartbeat] error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
