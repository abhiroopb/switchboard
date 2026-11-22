"""Stable, bounded advisory locks for source JSONL files.

The lock lives beside the source and is never renamed or removed.  Lock the
sidecar, rather than the JSONL inode, because writers atomically replace the
JSONL file.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import os
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator, TypeVar

DEFAULT_TIMEOUT_SECONDS = 10.0
_F = TypeVar("_F", bound=Callable)


def lock_path(source_path: Path) -> Path:
    source = Path(source_path).expanduser().resolve(strict=False)
    target = source.with_name(f".{source.name}.lock")
    if target.is_symlink():
        # Worktrees link shared-state lock files per file (link_shared_state);
        # O_NOFOLLOW below would refuse the link, so lock the real file on main.
        # Only a link to a same-named lock file is followed; anything else is
        # left as the symlink so os.open fails closed.
        real = target.resolve(strict=False)
        if real.name == target.name:
            target = real
    return target


@contextmanager
def source_jsonl_lock(
    source_path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Exclusively lock one source, or raise TimeoutError after ``timeout``."""
    target = lock_path(source_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)  # no O_NOFOLLOW on Windows
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out locking source JSONL: {source_path}")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def source_jsonl_locked(source_path: Callable[..., Path], timeout: float = DEFAULT_TIMEOUT_SECONDS):
    """Resolve the current source path and lock one complete invocation."""
    def decorate(func: _F) -> _F:
        @wraps(func)
        def wrapped(*args, **kwargs):
            with source_jsonl_lock(source_path(*args, **kwargs), timeout=timeout):
                return func(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorate
