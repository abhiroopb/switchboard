#!/usr/bin/env python3
"""Bounded, process-scoped advisory locks for PM OS scripts."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


class LockBusyError(RuntimeError):
    """Raised when another invocation holds the lock past the deadline."""


def _lock_dir() -> Path:
    override = os.environ.get("PM_OS_LOCK_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "state" / "locks"


@contextmanager
def single_instance(name: str, timeout: float | None = None) -> Iterator[Path]:
    """Hold a stable sidecar lock for the caller's complete critical section."""
    if not name or Path(name).name != name:
        raise ValueError("lock name must be one path component")
    if timeout is None:
        timeout = float(os.environ.get("PM_OS_LOCK_TIMEOUT_SECONDS", "30"))
    if timeout < 0:
        raise ValueError("lock timeout must be non-negative")

    lock_dir = _lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    handle: TextIO = lock_path.open("a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusyError(
                        f"busy: {name} is already running (waited {timeout:g}s)"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield lock_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
