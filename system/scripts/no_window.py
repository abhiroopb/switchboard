#!/usr/bin/env python3
"""Hidden consoles for switchboard background processes on Windows.

Why: a
process started with DETACHED_PROCESS has no console at all. Every console
program it then runs (git, python, bash) gets a brand-new console, Windows
hands that console to Windows Terminal, and a visible terminal window opens,
one per call. CREATE_NO_WINDOW instead gives the process a hidden console that
its children inherit, so nothing it runs ever shows a window. Adding
CREATE_NO_WINDOW to DETACHED_PROCESS does not help: Windows ignores it then.

Three ways in:

    detached_kwargs()   Popen keywords for a background process that outlives
                        its caller: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                        on Windows, a new session elsewhere.
    install()           Called at a helper's entry point. When this process has
                        no console (something started it detached), every later
                        subprocess call gets CREATE_NO_WINDOW unless it chose
                        its own console flags. A no-op with a console, and off
                        Windows.
    no_window.py spawn [--log FILE] -- CMD ...
                        Start CMD detached with detached_kwargs() and return at
                        once. Bash tails and the Amp plugins use it, because
                        nohup, `&` and Bun's detached spawn cannot ask for a
                        hidden console.

test_no_window.py fails on any Windows spawn that uses DETACHED_PROCESS.
Kill switch for install(): SWITCHBOARD_NO_WINDOW=off.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACH_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP  # 0x08000200
CONSOLE_CHOICES = DETACHED_PROCESS | CREATE_NEW_CONSOLE | CREATE_NO_WINDOW

_CREATIONFLAGS_POSITION = 13  # Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn,
#                               close_fds, shell, cwd, env, universal_newlines, startupinfo, creationflags)


def is_windows() -> bool:
    return os.name == "nt"


def detached_kwargs() -> dict[str, Any]:
    """Popen keywords for a process that outlives its caller and never opens a window."""
    return {"creationflags": DETACH_FLAGS} if is_windows() else {"start_new_session": True}


def hidden_kwargs() -> dict[str, Any]:
    """Popen keywords for one short call that must not open a window, wherever its parent runs."""
    return {"creationflags": CREATE_NO_WINDOW} if is_windows() else {}


def has_console() -> bool:
    """True when this process is attached to a console, hidden or not. GetConsoleWindow
    reads 0 for a hidden console too, so count the console's processes instead."""
    if not is_windows():
        return True
    try:
        import ctypes

        buffer = (ctypes.c_uint32 * 1)()
        return bool(ctypes.windll.kernel32.GetConsoleProcessList(buffer, 1))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return True  # unknown: leave subprocess alone


def with_hidden_console(flags: int) -> int:
    """The flags a console-less process should use: its own choice when it made one."""
    return flags if flags & CONSOLE_CHOICES else flags | CREATE_NO_WINDOW


_installed = False


def install(force: bool = False) -> bool:
    """Give every later subprocess call CREATE_NO_WINDOW when this process has no console.

    Returns True when the patch is in place. force skips the console check (tests)."""
    global _installed
    if _installed:
        return True
    if not is_windows() or os.environ.get("SWITCHBOARD_NO_WINDOW", "").lower() == "off":
        return False
    if not force and has_console():
        return False
    original = subprocess.Popen.__init__

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: N807
        if len(args) <= _CREATIONFLAGS_POSITION:
            kwargs["creationflags"] = with_hidden_console(int(kwargs.get("creationflags") or 0))
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    _installed = True
    return True


def spawn(command: list[str], log: str | None = None) -> int:
    """Start command detached with a hidden console. Returns its pid.

    Output goes to log when given, else to this process's own stdout and stderr. A bare
    program name resolves on PATH first: CreateProcess searches System32 before PATH, where
    bash.exe is the WSL launcher, not Git Bash."""
    if command and not os.path.dirname(command[0]):
        command = [shutil.which(command[0]) or command[0], *command[1:]]
    out: Any = None
    if log:
        os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
        out = open(log, "ab")  # noqa: SIM115 - the child keeps its own copy
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True,
                                **detached_kwargs())
    finally:
        if log:
            out.close()
    return proc.pid


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] != "spawn":
        print("usage: no_window.py spawn [--log FILE] -- CMD ...", file=sys.stderr)
        return 2
    rest = argv[1:]
    log = None
    if rest[:1] == ["--log"] and len(rest) >= 2:
        log, rest = rest[1], rest[2:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if not rest:
        print("no_window.py spawn: missing command", file=sys.stderr)
        return 2
    try:
        spawn(rest, log)
    except OSError as exc:
        print(f"no_window.py spawn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    install()
    raise SystemExit(main(sys.argv[1:]))
