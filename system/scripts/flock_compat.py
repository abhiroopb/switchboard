"""fcntl.flock for Windows: the same names and semantics over msvcrt.locking.

PM OS scripts lock with ``fcntl.flock(fd, fcntl.LOCK_EX)`` and friends. fcntl
is Unix-only, so every caller imports it like this:

    try:
        import fcntl
    except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
        import flock_compat as fcntl

On macOS and Linux the real fcntl always wins and this module is never loaded.

How the Windows lock maps to flock:
  * One byte far past any real content is locked, so the lock never blocks
    another process that reads or writes the file itself (Windows byte-range
    locks are mandatory; flock locks are advisory).
  * Locks belong to the open handle, as flock locks belong to the open file:
    two opens of the same file in one process exclude each other.
  * LOCK_SH is taken as exclusive (msvcrt has no shared lock). Callers only
    use LOCK_SH to read a ledger, so this costs a little concurrency, never
    correctness.
  * A second flock() on a handle that already holds the lock converts it, as
    flock does: release, then lock again. flock(2) documents conversion as
    non-atomic too.
  * The blocking form polls, because msvcrt's own blocking mode gives up after
    about 10 seconds.
  * Closing the file releases the lock, as with flock.
"""
from __future__ import annotations

import errno
import os
import threading
import time

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

# Past the end of every file PM OS locks; below 2**31 because msvcrt.locking
# reads the position as a 32-bit long.
_LOCK_OFFSET = 0x7FFFFFFE
_POLL_SECONDS = 0.05
_SEEK_MUTEX = threading.Lock()
# msvcrt.locking reports another holder as EACCES (and EDEADLOCK from its own blocking mode).
_CONTENDED = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


def _fileno(fd) -> int:
    return fd if isinstance(fd, int) else fd.fileno()


def _msvcrt():
    import msvcrt  # noqa: PLC0415 - Windows only; imported on first lock

    return msvcrt


def _locked_at_offset(fileno: int, action) -> None:
    """Run ``action()`` with the descriptor parked at the lock offset, then put the position back.

    The mutex keeps two threads that share a descriptor from interleaving seeks;
    callers never hold it while they sleep, so a waiter cannot block an unlock.
    """
    with _SEEK_MUTEX:
        position = os.lseek(fileno, 0, os.SEEK_CUR)
        try:
            os.lseek(fileno, _LOCK_OFFSET, os.SEEK_SET)
            action()
        finally:
            os.lseek(fileno, position, os.SEEK_SET)


def flock(fd, operation: int) -> None:
    """Lock or unlock ``fd`` (an int or an object with fileno())."""
    msvcrt = _msvcrt()
    fileno = _fileno(fd)

    def release() -> None:
        try:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # not held: unlocking twice is harmless, as with flock

    # Release first: this is LOCK_UN, and it is also how a held lock converts.
    _locked_at_offset(fileno, release)
    if operation & LOCK_UN:
        return
    if not operation & (LOCK_SH | LOCK_EX):
        raise ValueError(f"flock: operation needs LOCK_SH, LOCK_EX or LOCK_UN (got {operation})")
    while True:
        try:
            _locked_at_offset(fileno, lambda: msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1))
            return
        except OSError as exc:
            if exc.errno not in _CONTENDED:
                raise  # a real fault (bad descriptor, bad argument), not another holder
            if operation & LOCK_NB:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable") from exc
        time.sleep(_POLL_SECONDS)
