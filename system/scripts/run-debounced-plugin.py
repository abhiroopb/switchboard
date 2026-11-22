#!/usr/bin/env python3
"""Serialize and completion-debounce one detached plugin invocation."""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_last_success(path: Path) -> float | None:
    try:
        return parse_timestamp(json.loads(path.read_text()).get("lastFiredAt"))
    except (FileNotFoundError, OSError, ValueError, AttributeError):
        return None


def publish_success(path: Path) -> None:
    payload = {"lastFiredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _os_replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--cooldown-seconds", required=True, type=float)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if not SAFE_KEY.fullmatch(args.plugin) or not SAFE_KEY.fullmatch(args.key):
        parser.error("plugin and key must be filesystem-safe")
    if args.cooldown_seconds < 0:
        parser.error("cooldown must be non-negative")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a child command is required after --")

    plugin_dir = args.state_dir / args.plugin
    plugin_dir.mkdir(parents=True, exist_ok=True)
    state_path = plugin_dir / f"{args.key}.json"
    lock_path = plugin_dir / f"{args.key}.lock"

    with lock_path.open("a+") as lock_file:
        # Do not leave one waiting Python process per tool event during a
        # slow refresh. Contention is not a successful completion.
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{args.plugin}/{args.key}: busy; no success recorded", file=sys.stderr)
            return 75
        last_success = read_last_success(state_path)
        now = datetime.now(timezone.utc).timestamp()
        if last_success is not None and now - last_success < args.cooldown_seconds:
            return 0

        # If this supervisor exits while the child is active, the inherited
        # descriptor keeps a second invocation from entering concurrently.
        # Windows has no pass_fds: an msvcrt lock belongs to this process and ends with it.
        inherit = {} if os.name == "nt" else {"pass_fds": (lock_file.fileno(),)}
        result = subprocess.run(command, check=False, **inherit)
        if result.returncode == 0:
            publish_success(state_path)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
