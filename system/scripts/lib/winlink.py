"""winlink.py: shared-state links that work on Windows without Developer Mode.

dispatch.py links main's ignored state (ledgers, logs, people/) into every task
worktree. On macOS that is a symlink. On Windows an ordinary user cannot create
one (WinError 1314, "a required privilege is not held by the client") unless
Developer Mode is on, so this module falls back, per entry:

  * a directory gets a junction (`_winapi.CreateJunction`, no privilege needed);
  * a file gets a hard link when both paths sit on one volume;
  * anything else raises LinkUnavailable. Callers log it and skip. Shared state
    is never copied, because a copy diverges from main's file.

A real symlink is still tried first everywhere, so a Windows user with
Developer Mode on, and every Mac, behaves exactly as before.

Junctions are not symlinks to Python: `Path.is_symlink()` and `os.path.islink()`
are False for them, and on older Pythons `shutil.rmtree` could walk through one
into the folder it points at. Use `is_link()` wherever the code asks "is this a
shared link?", `remove_link()` to delete a link without touching its target,
and `rmtree()` to remove a tree that may contain links.

`pid_alive()` lives here too: `os.kill(pid, 0)` is a liveness probe on POSIX but
calls TerminateProcess on Windows, so it must never be used there.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

ERROR_PRIVILEGE_NOT_HELD = 1314
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


class LinkUnavailable(OSError):
    """No link of any kind could be made; the caller skips the entry."""


def is_windows() -> bool:
    return os.name == "nt"


def is_junction(path: str | os.PathLike[str]) -> bool:
    """True for an NTFS junction (mount-point reparse point). Always False off Windows."""
    if not is_windows():
        return False
    checker = getattr(os.path, "isjunction", None)   # Python 3.12+
    if checker is not None:
        return bool(checker(path))
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return getattr(info, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


def is_link(path: str | os.PathLike[str]) -> bool:
    """A symlink or a junction, working or broken. A hard link is not detectable from one side."""
    return os.path.islink(path) or is_junction(path)


def points_into(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when `path` is a link whose real target lies inside `root`."""
    return is_link(path) and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """True when both names reach one file (a hard link, or the same path)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_shared(path: str | os.PathLike[str], source: str | os.PathLike[str]) -> bool:
    """True when `path` is a link to `source`, or a hard link of the same file."""
    if is_link(path):
        return True
    return os.path.isfile(path) and not os.path.isdir(path) and same_file(path, source)


def _privilege_error(error: OSError) -> bool:
    return getattr(error, "winerror", None) == ERROR_PRIVILEGE_NOT_HELD


def create_junction(source: str | os.PathLike[str], link: str | os.PathLike[str]) -> None:
    """Make `link` a junction to the directory `source` (absolute path, same or other volume)."""
    import _winapi  # Windows only
    _winapi.CreateJunction(str(source), str(link))


def link_path(source: str | os.PathLike[str], link: str | os.PathLike[str]) -> str:
    """Make `link` reach `source`. Returns "symlink", "junction", or "hardlink".
    Raises LinkUnavailable when Windows allows none of them. Errors that are not a
    missing privilege on Windows (an existing `link`, a missing parent) pass through."""
    source, link = Path(source), Path(link)
    try:
        os.symlink(source, link, target_is_directory=source.is_dir())
        return "symlink"
    except OSError as error:
        if not (is_windows() and _privilege_error(error)):
            raise
        cause: Exception = error
    except NotImplementedError as error:   # a Python build with no symlink support
        if not is_windows():
            raise
        cause = error
    if source.is_dir():
        try:
            create_junction(source, link)
            return "junction"
        except (OSError, AttributeError, ImportError) as error:
            raise LinkUnavailable(f"junction {link} -> {source} failed ({error}); symlink failed ({cause})") from error
    try:
        os.link(source, link)
        return "hardlink"
    except OSError as error:
        raise LinkUnavailable(f"hard link {link} -> {source} failed ({error}); symlink failed ({cause})") from error


# --- hard-link freshness ------------------------------------------------------------
# A hard link names one inode. main rewrites shared state atomically (tmp + replace),
# which gives main's path a new inode and leaves every worktree's hard link on the
# old one: a worker read a stale running-tasks.json / now.json. Developer Mode
# (symlinks) needs an admin prompt, so instead every writer that replaces a shared
# file calls relink_shared() after the replace, and the dispatch sweep calls
# heal_shared_links() as the backstop for writers that do not. Only paths that
# dispatch.py link_shared_state recorded in .git/info/exclude are touched, so a
# worktree's own (tracked or untracked) files are never replaced.

def _main_root(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").is_dir():
            return parent
    return None


def _shared_rels(root: Path) -> set[str]:
    try:
        lines = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip().lstrip("/") for line in lines
            if line.strip().startswith("/") and not is_transient(line.strip())}


def _worktrees(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        entries = list((root / ".git" / "worktrees").iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            gitfile = Path((entry / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue
        found.append(gitfile.parent)
    return found


def _refresh_link(target: Path, link: Path) -> bool:
    staged = link.with_name(f"{link.name}.relink-{os.getpid()}")
    for attempt in range(20):
        try:
            if os.path.lexists(staged):
                os.unlink(staged)
            os.link(target, staged)
            os.replace(staged, link)
            return True
        except PermissionError:   # a reader has the old link open; it frees within ms
            __import__("time").sleep(0.05)
        except OSError:
            break
    try:
        os.unlink(staged)
    except OSError:
        pass
    return False


def relink_shared(target: str | os.PathLike[str], root: str | os.PathLike[str] | None = None,
                  shared: set[str] | None = None) -> int:
    """Windows only: point each task worktree's hard link of `target` (a shared-state
    file in the main checkout) at its current inode. Returns how many were refreshed.
    Never raises; a no-op elsewhere, for a path no worktree shares, or for a link
    that is already current."""
    if not is_windows():
        return 0
    try:
        real = Path(os.path.realpath(target))
        base = Path(root) if root else _main_root(real.parent)
        if base is None or not real.is_file():
            return 0
        rel = real.relative_to(Path(os.path.realpath(base))).as_posix()
        if rel not in (shared if shared is not None else _shared_rels(base)):
            return 0
        count = 0
        for worktree in _worktrees(base):
            link = worktree / rel
            if not link.is_file() or is_link(link) or same_file(link, real):
                continue
            count += _refresh_link(real, link)
        return count
    except Exception:  # noqa: BLE001  (bookkeeping after a write must never fail the write)
        return 0


def heal_shared_links(root: str | os.PathLike[str]) -> int:
    """relink_shared() for every shared file the main checkout records, across every
    worktree. The dispatch sweep runs it each tick."""
    if not is_windows():
        return 0
    base = Path(root)
    shared = _shared_rels(base)
    return sum(relink_shared(base / rel, base, shared) for rel in sorted(shared) if (base / rel).is_file())


# --- what never gets shared ---------------------------------------------------------
# main's system/state holds writers' temp files while they write (running-tasks.json.<pid>.tmp)
# and, after a failed replace, leftovers. link_shared_state hard-linked them into every new
# worktree and recorded them in info/exclude. They are never shared state, so neither the
# launch nor the heal touches them. Lock files stay linked on purpose: events.jsonl and the
# bindings file are written in place from main and worktrees alike, and one inode is what
# makes their flock one lock. A lock file is opened, never replaced, so its link never goes stale.
_TRANSIENT = re.compile(r"(\.tmp|\.temp|\.link-tmp|\.tmp-\d+|\.relink-\d+)$", re.IGNORECASE)


def is_transient(name: str | os.PathLike[str]) -> bool:
    """True for a writer's temp file (`*.tmp`, `*.<pid>.tmp`, `*.tmp-<pid>`, `*.temp`,
    `*.link-tmp`, `*.relink-<pid>`), judged by its base name."""
    return bool(_TRANSIENT.search(os.path.basename(os.fspath(name))))


DEVELOPER_MODE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
DEVELOPER_MODE_ON = (f'reg add "HKLM\\{DEVELOPER_MODE_KEY}" '
                     '/t REG_DWORD /f /v AllowDevelopmentWithoutDevLicense /d 1')


def developer_mode() -> bool | None:
    """Windows: True when Developer Mode is on (unprivileged symlinks), False when off,
    None when the registry cannot be read. Always None off Windows."""
    if not is_windows():
        return None
    try:
        import winreg  # Windows only
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DEVELOPER_MODE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
        return bool(int(value))
    except FileNotFoundError:
        return False   # the value only exists once Developer Mode was turned on at least once
    except (OSError, ImportError, ValueError, TypeError):
        return None


def developer_mode_hint() -> str:
    """One line for install-all: empty unless this is Windows with Developer Mode off."""
    if developer_mode() is not False:
        return ""
    return ("Windows Developer Mode is off (optional): shared-state files are hard links that PM OS "
            "relinks after main rewrites them. For symlinks instead, run once in an admin PowerShell: "
            + DEVELOPER_MODE_ON)


def remove_link(path: str | os.PathLike[str]) -> None:
    """Delete the link itself, never what it points at. A junction goes through rmdir
    (unlink raises on it); a symlink or hard link through unlink."""
    target = os.fspath(path)
    if is_junction(target) and not os.path.islink(target):
        try:
            os.rmdir(target)
            return
        except OSError:
            pass
    try:
        os.unlink(target)
    except (IsADirectoryError, PermissionError):
        os.rmdir(target)


def _make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def rmtree(path: str | os.PathLike[str], ignore_errors: bool = False) -> None:
    """Remove `path` and everything under it, never descending into a symlink or
    junction: each link is deleted on its own and its target is left alone. A path
    that is itself a link is unlinked, not emptied. A read-only file is made
    writable and retried (Windows refuses to delete it otherwise)."""
    root = os.fspath(path)
    try:
        if is_link(root):
            remove_link(root)
            return
        if not os.path.lexists(root):
            return
        _remove_dir(root, ignore_errors)
    except OSError:
        if not ignore_errors:
            raise


def _remove_dir(directory: str, ignore_errors: bool) -> None:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        if ignore_errors:
            return
        raise
    for entry in entries:
        try:
            if is_link(entry.path):
                remove_link(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                _remove_dir(entry.path, ignore_errors)
            else:
                try:
                    os.unlink(entry.path)
                except PermissionError:
                    _make_writable(entry.path)
                    os.unlink(entry.path)
        except OSError:
            if not ignore_errors:
                raise
    try:
        os.rmdir(directory)
    except PermissionError:
        _make_writable(directory)
        try:
            os.rmdir(directory)
        except OSError:
            if not ignore_errors:
                raise
    except OSError:
        if not ignore_errors:
            raise


def pid_alive(pid: int) -> bool:
    """True when process `pid` exists. Never signals it: on Windows `os.kill(pid, 0)`
    would terminate the process, so that platform asks the OS for its exit code."""
    if pid <= 0:
        return False
    if not is_windows():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    # use_last_error: plain windll lets ctypes clobber GetLastError between calls.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5   # access denied: the process exists
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


__all__ = ["LinkUnavailable", "create_junction", "developer_mode", "developer_mode_hint", "is_junction", "is_link",
           "is_shared", "is_transient", "is_windows", "link_path", "pid_alive", "points_into", "remove_link", "rmtree",
           "same_file"]


if __name__ == "__main__":   # install-all.sh: python3 system/scripts/lib/winlink.py developer-mode-hint
    import sys
    if sys.argv[1:] == ["developer-mode-hint"]:
        hint = developer_mode_hint()
        if hint:
            print(hint)
        sys.exit(0)
    sys.exit("usage: winlink.py developer-mode-hint")
