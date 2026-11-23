#!/usr/bin/env python3
"""Shared access layer for system/state/events.jsonl.

events.jsonl is an append-only fact stream (workspace updates, queue
refreshes, auto-close records, notifications) with multiple concurrent
writers — rebuild-switchboard-state.py, emit-workspace-update.py, and
daemon/amp-notify.sh — plus a periodic trimmer invoked from
system/scripts/rotate-logs.sh.

Coordination protocol (all parties MUST use this module):
  - Appends take a SHARED flock on .events.lock, so concurrent appenders
    never block each other (O_APPEND keeps single-line writes atomic).
  - Trim takes an EXCLUSIVE flock, so no append can land between reading
    the tail and replacing the file. A copytruncate-style trim without the
    lock silently loses any event written in that window.
  - The lock lives in a separate file so replacing events.jsonl never
    invalidates a held lock, and writers must open events.jsonl only AFTER
    acquiring the lock (never reuse a pre-lock handle).

Usage as a library (sibling scripts):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from events_stream import append_event_line

Usage from shell:
    echo '{"type": "notification", ...}' | \
        python3 events_stream.py append --state-dir system/state
    python3 events_stream.py trim --state-dir system/state
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import gzip
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

EVENTS_NAME = "events.jsonl"
LOCK_NAME = ".events.lock"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_KEEP_LINES = 2000

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

@contextmanager
def events_lock(state_dir: Path, exclusive: bool):
    lock_path = state_dir / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:  # "a", not "w": never truncate a lock file another process holds
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_event_line(state_dir: Path, event: dict) -> None:
    """Append one JSON event, coordinated with trim_events()."""
    line = json.dumps(event)
    with events_lock(state_dir, exclusive=False):
        with (state_dir / EVENTS_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def trim_events(
    state_dir: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep_lines: int = DEFAULT_KEEP_LINES,
) -> bool:
    """Archive the full stream to <events>.1.gz and keep the newest lines.

    Returns True when a trim happened. Holds the exclusive lock for the whole
    archive+replace sequence, so no concurrently appended event can be lost:
    appenders block until the new (trimmed) file is in place, then append to
    it.
    """
    events_path = state_dir / EVENTS_NAME
    if not events_path.exists():
        return False

    with events_lock(state_dir, exclusive=True):
        size = events_path.stat().st_size
        if size <= max_bytes:
            return False

        mode = events_path.stat().st_mode & 0o7777
        archive_path = events_path.with_name(f"{events_path.name}.1.gz")
        with events_path.open("rb") as src, gzip.open(archive_path, "wb") as dst:
            shutil.copyfileobj(src, dst)

        lines = events_path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = lines[-keep_lines:] if keep_lines > 0 else []
        tmp_path = events_path.with_name(f".{events_path.name}.trim.tmp")
        tmp_path.write_text("".join(kept), encoding="utf-8")
        os.chmod(tmp_path, mode)
        _os_replace(tmp_path, events_path)

        print(
            f"Trimmed {events_path} ({size} bytes -> last {len(kept)} lines; "
            f"archive: {archive_path})"
        )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    append_parser = subparsers.add_parser("append", help="Append one JSON event (from --json or stdin)")
    append_parser.add_argument("--state-dir", type=Path, required=True)
    append_parser.add_argument("--json", help="JSON object; defaults to reading stdin")

    trim_parser = subparsers.add_parser("trim", help="Archive + trim the stream when it exceeds --max-bytes")
    trim_parser.add_argument("--state-dir", type=Path, required=True)
    trim_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    trim_parser.add_argument("--keep-lines", type=int, default=DEFAULT_KEEP_LINES)

    args = parser.parse_args()

    if args.command == "append":
        raw = args.json if args.json is not None else sys.stdin.read()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SystemExit(f"events_stream append: invalid JSON: {error}")
        if not isinstance(event, dict):
            raise SystemExit("events_stream append: event must be a JSON object")
        append_event_line(args.state_dir, event)
    else:
        trim_events(args.state_dir, max_bytes=args.max_bytes, keep_lines=args.keep_lines)


if __name__ == "__main__":
    main()
