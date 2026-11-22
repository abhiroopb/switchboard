"""Shared, dependency-free primitives for polling collectors.

Collector entry points should return ``main_exit(result)``: 0 means the scan
completed, 1 means it failed before fetching useful data, and 2 means it was
partial. Callers may treat nonzero exits as non-fatal while retaining an
observable signal that the collector did not fully succeed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Iterator, Optional, Sequence

from _lookback import compute_lookback_iso, record_run
from source_jsonl_lock import source_jsonl_lock


REPO_ROOT = Path(__file__).resolve().parents[2]

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

class CollectorError(RuntimeError):
    """Base class for expected collector failures."""


class CollectorTimeout(CollectorError):
    """A collector command exceeded its timeout."""


class CollectorCommandError(CollectorError):
    """A collector command returned a nonzero exit status."""

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"command rc={returncode}: {stderr[:200]}")


class CollectorBadJSON(CollectorError):
    """A collector command returned invalid JSON."""

    def __init__(self, command: Sequence[str], output: str) -> None:
        self.command = command
        self.output = output
        super().__init__(f"command returned non-json: {output[:160]}")


class _CollectorLogger:
    def __init__(self, name: str, path: Path, include_name: bool) -> None:
        self.name = name
        self.path = path
        self.include_name = include_name

    def _write(self, message: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        name = f" [{self.name}]" if self.include_name else ""
        with self.path.open("a") as handle:
            handle.write(f"[{timestamp}]{name} {message}\n")

    def info(self, message: str) -> None:
        self._write(message)

    def warn(self, message: str) -> None:
        self._write(message)

    def error(self, message: str) -> None:
        self._write(message)


def get_logger(
    name: str, path: Optional[Path] = None, *, include_name: bool = True
) -> _CollectorLogger:
    """Return a tiny append logger; explicit paths can preserve legacy format."""
    log_path = path or REPO_ROOT / "system" / "logs" / f"{name}.log"
    return _CollectorLogger(name, Path(log_path), include_name)


@contextmanager
def collector_lock(name: str, stale_after_minutes: int = 10) -> Iterator[bool]:
    """Acquire an atomic mkdir lock, reclaiming an abandoned stale lock."""
    lock_root = REPO_ROOT / ".state"
    lock_path = lock_root / f"{name}.lock"
    lock_root.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        try:
            lock_path.mkdir()
            acquired = True
        except FileExistsError:
            try:
                age = datetime.now().timestamp() - lock_path.stat().st_mtime
                if age > stale_after_minutes * 60:
                    shutil.rmtree(str(lock_path))
                    lock_path.mkdir()
                    acquired = True
            except (FileNotFoundError, FileExistsError, OSError):
                acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.rmdir()
            except FileNotFoundError:
                pass


@dataclass
class RunResult:
    complete: bool = False
    fetched: int = 0
    written: int = 0
    errors: int = 0


@contextmanager
def guarded_lookback(
    name: str,
    default_hours: int,
    result: RunResult,
    *,
    buffer_seconds: int = 120,
    max_seconds: int = 72 * 60 * 60,
    fallback_on_stale: bool = True,
) -> Iterator[str]:
    """Compute lookback and record the attempt without advancing on failure."""
    lookback = compute_lookback_iso(
        name,
        default=f"PT{default_hours}H",
        buffer_seconds=buffer_seconds,
        max_seconds=max_seconds,
        fallback_on_stale=fallback_on_stale,
    )
    try:
        yield lookback
    except Exception:
        result.complete = False
        result.errors += 1
        record_run(name, complete=False)
        raise
    else:
        record_run(name, complete=result.complete)


def run_json(command: Sequence[str], timeout: float, retries: int = 0) -> Any:
    """Run a command and decode stdout, retrying expected command failures."""
    last_error: Optional[CollectorError] = None
    for _attempt in range(retries + 1):
        try:
            completed = subprocess.run(
                list(command), capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            last_error = CollectorTimeout(f"command timed out after {timeout}s: {command}")
            continue
        except OSError as exc:
            last_error = CollectorCommandError(command, 127, str(exc))
            continue
        if completed.returncode != 0:
            last_error = CollectorCommandError(command, completed.returncode, completed.stderr)
            continue
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            last_error = CollectorBadJSON(command, completed.stdout)
    assert last_error is not None
    raise last_error


def load_seen_jsonl(path: Path, key_fn: Callable[[dict[str, Any]], Hashable]) -> set[Hashable]:
    """Load dedupe keys from JSONL, warning once about malformed lines."""
    path = Path(path)
    if not path.exists():
        return set()
    seen: set[Hashable] = set()
    malformed = 0
    with path.open() as handle:
        for line in handle:
            try:
                key = key_fn(json.loads(line))
                if key is not None:
                    seen.add(key)
            except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                malformed += 1
    if malformed:
        warnings.warn(f"{path}: ignored {malformed} malformed JSONL line(s)")
    return seen


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    """Append records as one compact JSON object per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with source_jsonl_lock(path):
        with path.open("a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
                count += 1
    return count


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically replace a JSON file with a fully written temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _os_replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main_exit(result: RunResult) -> int:
    """Map a run result to complete (0), failed (1), or partial (2)."""
    if result.complete:
        return 0
    return 2 if result.fetched or result.written else 1
