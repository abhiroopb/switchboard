#!/usr/bin/env python3
"""Run a command with wall-clock and process-group RSS caps.

macOS does not reliably enforce shell `ulimit` memory limits for these daemon
jobs, so PM OS meeting jobs use this wrapper to keep one runaway headless Amp
or Google Drive call from freezing the laptop.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def log(message: str) -> None:
    root = Path(os.environ.get("PM_OS_ROOT") or Path.home() / "Development/pm-os")
    log_file = root / ".state" / "daemon-memory-watchdog.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with log_file.open("a") as handle:
        handle.write(f"[{timestamp}] run-capped: {message}\n")


def rss_for_pgid_kb(pgid: int) -> int:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-g", str(pgid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return 0

    total = 0
    for line in result.stdout.splitlines():
        try:
            total += int(line.strip())
        except ValueError:
            continue
    return total


def terminate_group(pgid: int) -> None:
    if os.name == "nt":
        # Windows has no killpg; the child was started without a new session, so
        # pgid is its pid. taskkill /T ends the whole tree. The RSS cap reads
        # `ps -g`, which Windows lacks, so only the wall-clock cap holds there.
        result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pgid)],
                                check=False, capture_output=True, text=True)
        if result.returncode not in (0, 128):  # 128: already gone
            log(f"failed taskkill pid={pgid}: {result.stderr.strip()}")
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        log(f"failed SIGTERM pgid={pgid}: {exc}")
        return

    time.sleep(3)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        log(f"failed SIGKILL pgid={pgid}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rss-kb", type=int, required=True)
    parser.add_argument("--timeout-sec", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command")

    started = time.monotonic()
    proc = subprocess.Popen(command, **({} if os.name == "nt" else {"start_new_session": True}))
    pgid = proc.pid if os.name == "nt" else os.getpgid(proc.pid)

    log(
        "started "
        f"pid={proc.pid} pgid={pgid} rss_cap={args.rss_kb}KB "
        f"timeout={args.timeout_sec}s cmd={' '.join(command)}"
    )

    while True:
        rc = proc.poll()
        if rc is not None:
            log(f"finished pid={proc.pid} rc={rc}")
            return rc

        elapsed = time.monotonic() - started
        if elapsed > args.timeout_sec:
            log(f"timeout pid={proc.pid} pgid={pgid} elapsed={elapsed:.1f}s")
            terminate_group(pgid)
            return 124

        rss_kb = rss_for_pgid_kb(pgid)
        if rss_kb > args.rss_kb:
            log(
                f"rss cap exceeded pid={proc.pid} pgid={pgid} "
                f"rss={rss_kb}KB cap={args.rss_kb}KB"
            )
            terminate_group(pgid)
            return 137

        time.sleep(2)


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system", "scripts"))
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
