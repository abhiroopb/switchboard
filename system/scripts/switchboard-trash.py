#!/usr/bin/env python3
"""switchboard-trash.py — a drop-in `rm -rf` that moves outside targets to the Trash.

Destructive deletes in workers raised prompts the owner often missed. Option 1, approved: trash instead of delete. switchboard-guardrails Rule 3
(system/amp-plugins/switchboard/rm-guard.ts) rewrites the command word of an
`rm -rf` whose targets are outside switchboard, the temp dirs, and build output to
this script, so the worker never waits on a prompt. The arguments stay as the
agent wrote them; the shell expands them, and this script re-checks each real
path:

  temp path, build output, and inside the switchboard
  repo only what git tracks and holds unchanged     deleted, as rm would
  switchboard repo root, a .git directory, a top-level
  system or home folder                             refused (exit 1), never touched
  anything else, including in-repo paths git does
  not track (gitignored local/, system/state/,
  .amp/in scratch, untracked files)                 moved to the Trash:
                                                    macOS /usr/bin/trash (Finder's
                                                    Put Back works; fallback: a
                                                    timestamped move into ~/.Trash);
                                                    Linux `gio trash`, else the
                                                    freedesktop Trash folder;
                                                    Windows the Recycle Bin
                                                    (PowerShell SendToRecycleBin)

Inside the repo, git decides, because gitignored paths have no git copy to
restore from. The fast hard delete is kept
only for a path whose every file git tracks and holds unchanged: `git ls-files`
names it and `git status --ignored --untracked-files=all` lists nothing under it,
so git can restore it. A gitignored or untracked path, a folder holding any
ignored, untracked, or changed file, an empty folder, and any path git cannot
answer for go to the Trash (the Recycle Bin on Windows). Two in-repo exceptions
still delete: a node_modules tree (build output) and a link or junction, where
only the link goes and its target stays. switchboard-guardrails Rule 3 sends an
in-repo `rm -rf` here too, so a plain `rm -rf` of a gitignored folder gets the same check.

Every move and refusal is one JSON line in system/state/logs/trash-moves.jsonl
on the main checkout, with the worker task id, cmux workspace, and session from
the environment. report-to-cos.sh lists a task's moves in its report:

  switchboard-trash.py --switchboard-list [--task-id ID] [--since ISO] [--format line|json]

Paths compare in POSIX form on every OS (Windows: case-insensitive, `C:/...`,
and Git Bash `/c/...` arguments), so the refusals hold on Windows and Linux too.

Windows never hard-deletes an outside target. The target goes
to the Recycle Bin through powershell.exe (Microsoft.VisualBasic FileSystem,
SendToRecycleBin; the path travels in an environment variable, never in the
script text). With OnlyErrorDialogs, Windows deletes an item for good, with no
question, when it is too big for the bin, when the volume's bin is set to
"Don't move files to the Recycle Bin", when a policy turns the bin off, when
the path is longer than MAX_PATH, and on network or removable drives. So the
helper checks each of those first. Any check that fails, any PowerShell error,
or a source that still exists afterward means: nothing is deleted, the helper
prints why, logs action "failed", and exits 1 so the caller asks the owner.
Each "trashed" log row has a trashed_via field: recycle-bin, folder, macos-trash.

Env: SWITCHBOARD_TRASH_LOG (log path), SWITCHBOARD_TRASH_DIR (fallback Trash folder; when
set off macOS, the only Trash, Windows included), SWITCHBOARD_TRASH_NO_CLI=1 (skip
/usr/bin/trash and gio), SWITCHBOARD_TRASH_REPO (repo root),
SWITCHBOARD_TRASH_TEMP_PREFIXES (colon or semicolon list; tests only), HOME. Tests: system/scripts/test_switchboard_trash.py.
"""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from lib import winlink  # noqa: E402

DEFAULT_TEMP_PREFIXES = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
SYSTEM_ROOTS = {"/", "/Users", "/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/var",
                "/private", "/private/var", "/private/etc", "/opt", "/opt/homebrew", "/Volumes", "/cores", "/dev",
                *DEFAULT_TEMP_PREFIXES[2:],
                # Linux
                "/home", "/root", "/mnt", "/media", "/srv", "/boot", "/lib", "/lib64", "/proc", "/sys", "/run"}
# Windows, compared lower-case: a drive root and its system folders
WINDOWS_SYSTEM_RE = re.compile(r"[a-z]:(?:/(?:windows|users|programdata|program files|program files \(x86\)))?/?")
HOME_ROOTS = ("", "Development", "Documents", "Desktop", "Downloads", "Library", "Pictures", "Movies", "Music",
              "Applications", "Public", ".ssh", ".config", ".claude", ".agents", ".Trash", ".local", ".gnupg",
              # Windows
              "AppData", "OneDrive", ".switchboard-trash")


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def repo_root() -> Path:
    if os.environ.get("SWITCHBOARD_TRASH_REPO"):
        return Path(os.environ["SWITCHBOARD_TRASH_REPO"])
    try:
        from repo_paths import main_checkout
        return main_checkout(SCRIPTS.parents[1])
    except Exception:  # noqa: BLE001  (a missing helper must not break a delete)
        return SCRIPTS.parents[1]


def log_path() -> Path:
    if os.environ.get("SWITCHBOARD_TRASH_LOG"):
        return Path(os.environ["SWITCHBOARD_TRASH_LOG"])
    return repo_root() / "system" / "state" / "logs" / "trash-moves.jsonl"


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _is_windows() -> bool:
    return os.name == "nt"


def _key(path: str, windows: bool | None = None) -> str:
    """One path in comparable POSIX form. Windows: `C:\\X` and Git Bash `/c/X` both
    become `c:/x` (NTFS is case-insensitive); elsewhere only normpath."""
    windows = _is_windows() if windows is None else windows
    if not windows:
        return posixpath.normpath(path) if path else path
    raw = path.replace("\\", "/")
    raw = re.sub(r"^//[?.]/(?=[A-Za-z]:)", "", raw)  # \\?\C:\X long-path form names C:\X
    raw = re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", raw)
    # Win32 drops trailing dots and spaces from each name: `.git.` is `.git`, `C:\Windows.` is `C:\Windows`.
    raw = "/".join(part if part in (".", "..") else part.rstrip(". ") for part in raw.split("/"))
    key = ntpath.normpath(raw).replace("\\", "/").lower()
    return key + "/" if re.fullmatch(r"[a-z]:", key) else key


def _temp_prefixes(windows: bool | None = None) -> tuple[str, ...]:
    # Test seam: the hermetic tests live under /var/folders, so they name their own.
    raw = os.environ.get("SWITCHBOARD_TRASH_TEMP_PREFIXES")
    if raw is not None:
        return tuple(p for p in raw.split(";" if ";" in raw else ":") if p)  # ";" for Windows drive paths
    if not (_is_windows() if windows is None else windows):
        return DEFAULT_TEMP_PREFIXES
    # Windows has no /tmp: the user's temp folder, only when it is a temp folder by name
    # (AppData\Local\Temp, C:\Temp); a TEMP pointed at Documents or home hard-deletes nothing.
    temp = _key(tempfile.gettempdir(), True)
    named = temp.rsplit("/", 1)[-1] in ("temp", "tmp")
    return (temp,) if named and temp.count("/") >= 1 and temp != _key(str(_home()), True) else ()


def classify(path: str, windows: bool | None = None) -> tuple[str, str]:
    """(action, reason) for one real path: delete | refuse | trash."""
    windows = _is_windows() if windows is None else windows
    real = _key(path, windows)
    temp = tuple(_key(p, windows) for p in _temp_prefixes(windows))
    for prefix in ("/tmp", "/private/tmp"):
        if real == prefix and prefix in temp:
            return "delete", "temp path"
    if any(real.startswith(p.rstrip("/") + "/") for p in temp):
        return "delete", "temp path"
    repo = _key(str(repo_root()), windows)
    if real == repo:
        return "refuse", "the switchboard repo root"
    if ".git" in real.lower().split("/"):   # APFS is case-insensitive: .GIT is the same folder
        return "refuse", "a .git directory"
    if real in SYSTEM_ROOTS or (windows and WINDOWS_SYSTEM_RE.fullmatch(real)):
        return "refuse", "a top-level system folder"
    home = _key(str(_home()), windows)
    if any(real == (_key(f"{home}/{r}", windows) if r else home) for r in HOME_ROOTS):
        return "refuse", "a top-level home folder"
    if _under(real, repo):
        if "node_modules" in real.split("/"):
            return "delete", "build output"
        if os.path.lexists(path) and winlink.is_link(path):
            return "delete", "a link inside the switchboard repo (its target stays)"
        why = _git_unrestorable(path)
        if why is None:
            return "delete", "inside the switchboard repo, tracked and unchanged in git"
        return "trash", f"inside the switchboard repo, {why}"
    parts = real.split("/")
    if "node_modules" in parts or parts[-1] in {"dist", "build"}:
        return "delete", "build output"
    return "trash", "outside switchboard and temp paths"


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotepath=off", "--literal-pathspecs", "-C", cwd, *args],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=60)


def _git_unrestorable(path: str) -> str | None:
    """None when git can restore every file under `path` (all tracked, none changed,
    no ignored or untracked file inside); otherwise the reason, in a few words.
    Any doubt (no git, no checkout, an error, a timeout) is a reason: the path
    goes to the Trash, never a hard delete."""
    try:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        top = _git(parent, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return "not in a git checkout"
        root = os.fsdecode(top.stdout).strip()
        rel = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
        if rel == "." or rel.startswith("../") or rel == "..":
            return "outside its git checkout"
        tracked = _git(root, "ls-files", "-z", "--", rel)
        if tracked.returncode != 0:
            return "git could not list it"
        if not tracked.stdout.strip(b"\0"):
            return "not tracked by git (gitignored or untracked)"
        status = _git(root, "status", "--porcelain", "-z", "--ignored=matching", "--untracked-files=all", "--", rel)
        if status.returncode != 0:
            return "git could not read its status"
        if status.stdout.strip(b"\0"):
            return "holds ignored, untracked, or changed files git cannot restore"
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return f"git could not check it ({error})"


def _absolute(arg: str) -> str:
    # Keep the last component as given: trashing a symlink moves the link, as rm does.
    if _is_windows():  # a Git Bash path that skipped MSYS conversion: /c/work/x -> C:/work/x
        arg = re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", arg)
    p = Path(arg)
    if not p.is_absolute():
        p = Path.cwd() / p
    parent = p.parent.resolve() if p.parent.exists() else p.parent
    result = str(parent / p.name) if p.name not in {"", ".", ".."} else str(p.resolve())
    return _long_name(result) if _is_windows() else result


def _long_name(path: str) -> str:
    """Windows: expand 8.3 short names (`C:\\PROGRA~1`, `PATRIC~1`) so the refusals see
    the real folder. `resolve()` expands the parent but not the last component.
    Best effort: an unreadable path stays as given."""
    if "~" not in path:
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetLongPathNameW(path, buf, len(buf))  # type: ignore[attr-defined]
        return buf.value if 0 < size < len(buf) else path
    except Exception:  # noqa: BLE001
        return path


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(entry: dict) -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        base = {"at": _stamp(), "task_id": os.environ.get("SWITCHBOARD_WORKER_TASK_ID", ""),
                "workspace_id": os.environ.get("CMUX_WORKSPACE_ID", ""),
                "session_id": os.environ.get("SWITCHBOARD_WORKER_SESSION_ID", ""),
                "workstream": os.environ.get("SWITCHBOARD_WORKSTREAM_SLUG", ""), "cwd": os.getcwd()}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**base, **entry}, sort_keys=True) + "\n")
    except OSError as error:
        print(f"switchboard-trash: could not write the log {path}: {error}", file=sys.stderr)


def _fallback_trash(path: str) -> str:
    default = ".Trash" if sys.platform == "darwin" else ".switchboard-trash"
    trash = Path(os.environ.get("SWITCHBOARD_TRASH_DIR") or _home() / default)
    trash.mkdir(parents=True, exist_ok=True)
    name = Path(path).name or "item"
    when = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    dest = trash / f"{name} (switchboard {when})"
    n = 2
    while dest.exists() or dest.is_symlink():
        dest = trash / f"{name} (switchboard {when} {n})"
        n += 1
    shutil.move(path, str(dest))
    return str(dest)


def _freedesktop_trash(path: str) -> str:
    """Linux without gio: the freedesktop.org Trash folder, with the .trashinfo
    record file managers read to restore the item."""
    from urllib.parse import quote
    base = Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share") / "Trash"
    files, info = base / "files", base / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    stem = Path(path).name or "item"
    name, n = stem, 2
    while os.path.lexists(files / name) or (info / f"{name}.trashinfo").exists():
        name, n = f"{stem}.{n}", n + 1
    record = info / f"{name}.trashinfo"
    record.write_text(
        "[Trash Info]\nPath=" + quote(path) + "\nDeletionDate="
        + datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n", encoding="utf-8")
    try:
        shutil.move(path, str(files / name))
    except OSError:
        record.unlink(missing_ok=True)  # no orphan record for an item that never moved
        raise
    return str(files / name)


class TrashUnavailable(Exception):
    """The target could not be moved to a Trash safely. Nothing was deleted;
    the caller reports it and the owner decides."""


# Windows Recycle Bin ------------------------------------------------------
# Constant script. The target path travels in SWITCHBOARD_TRASH_TARGET, never in the text.
RECYCLE_PS = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName Microsoft.VisualBasic; "
    "$p = $env:SWITCHBOARD_TRASH_TARGET; "
    "if ([string]::IsNullOrEmpty($p)) { throw 'no target' } "
    "if (Test-Path -LiteralPath $p -PathType Container) "
    "{ [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'OnlyErrorDialogs', 'SendToRecycleBin') } "
    "else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin') }"
)
RECYCLE_MAX_PATH = 240          # Win32 MAX_PATH is 260; keep a margin
RECYCLE_CAPACITY_SHARE = 2      # use at most 1/2 of the bin limit for one item
RECYCLE_DEFAULT_PERCENT = 5     # bin limit when the registry names none: 5% of the volume
DRIVE_FIXED = 3
REPARSE_POINT = 0x400


def _powershell_exe() -> str | None:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _windows_plain(path: str) -> str:
    """`C:\\x`, `C:/x`, `/c/x`, and `\\\\?\\C:\\x` as `C:\\x`. Anything else is returned with
    backslashes (a UNC path stays `\\\\server\\share`)."""
    raw = path.replace("\\", "/")
    raw = re.sub(r"^//[?.]/(?=[A-Za-z]:)", "", raw)
    raw = re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", raw)
    return ntpath.normpath(raw)


def _is_network_path(path: str) -> bool:
    raw = path.replace("\\", "/")
    return raw.startswith("//") and not re.match(r"^//[?.]/[A-Za-z]:", raw)


def _windows_volume(path: str) -> dict:
    """Mount root, drive type, volume GUID, and size of the volume holding `path`."""
    import ctypes
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    root = ctypes.create_unicode_buffer(1024)
    if not kernel.GetVolumePathNameW(path, root, len(root)):
        raise OSError("GetVolumePathNameW failed")
    name = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeNameForVolumeMountPointW(root.value, name, len(name)):
        raise OSError("GetVolumeNameForVolumeMountPointW failed")
    match = re.search(r"\{[0-9A-Fa-f-]+\}", name.value)
    return {"root": root.value, "drive_type": int(kernel.GetDriveTypeW(root.value)),
            "guid": match.group(0) if match else "", "total": shutil.disk_usage(root.value).total}


def _windows_bin_config(guid: str) -> dict:
    """Recycle Bin settings that make Windows delete for good: a per-volume or global
    "Don't move files to the Recycle Bin" (NukeOnDelete), the NoRecycleFiles policy,
    and the per-volume size limit in MB (MaxCapacity). A missing value is the default."""
    import winreg  # type: ignore[import-not-found]

    def read(hive, sub: str, name: str):
        try:
            with winreg.OpenKey(hive, sub) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
    volume = base + "\\Volume\\" + guid
    policy = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    user, machine = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    max_mb = read(user, volume, "MaxCapacity")
    if max_mb is None:
        max_mb = read(user, base, "MaxCapacity")
    return {
        "nuke": bool(read(user, base, "NukeOnDelete") or read(user, volume, "NukeOnDelete")),
        "policy_off": bool(read(user, policy, "NoRecycleFiles") or read(machine, policy, "NoRecycleFiles")),
        "max_bytes": int(max_mb) * 1024 * 1024 if isinstance(max_mb, int) and max_mb > 0 else None,
    }


def _tree_stats(path: str, cap: int) -> tuple[int, int]:
    """(bytes, longest path length) of a file or folder tree. A reparse point (link or
    junction) inside is counted as itself, never followed. Stops early once either
    number passes its limit."""
    size, longest = 0, len(path)
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = os.lstat(current)
        except OSError:
            continue
        longest = max(longest, len(current))
        size += info.st_size
        if size > cap or longest > RECYCLE_MAX_PATH:
            break
        if (getattr(info, "st_file_attributes", 0) & REPARSE_POINT) or os.path.islink(current):
            continue
        if os.path.isdir(current):
            try:
                with os.scandir(current) as entries:
                    stack.extend(entry.path for entry in entries)
            except OSError:
                continue
    return size, longest


def _recycle_refusal(path: str) -> str | None:
    """Why Windows might delete `path` for good instead of recycling it; None when safe."""
    if _is_network_path(path):
        return "a network path has no Recycle Bin"
    try:
        info = os.lstat(path)
    except OSError as error:
        return f"cannot read it ({error.strerror or error})"
    if os.path.islink(path) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        return "it is a link or junction"
    try:
        volume = _windows_volume(path)
        config = _windows_bin_config(volume["guid"])
    except Exception as error:  # noqa: BLE001  (cannot prove the bin is safe: do not guess)
        return f"cannot read the Recycle Bin settings ({error})"
    if volume["drive_type"] != DRIVE_FIXED:
        return "the drive is not a fixed local drive"
    if config["policy_off"]:
        return "a policy turns the Recycle Bin off"
    if config["nuke"]:
        return "the volume deletes files instead of recycling them"
    capacity = config["max_bytes"] or volume["total"] * RECYCLE_DEFAULT_PERCENT // 100
    allowed = capacity // RECYCLE_CAPACITY_SHARE
    size, longest = _tree_stats(path, allowed)
    if longest > RECYCLE_MAX_PATH:
        return f"a path longer than {RECYCLE_MAX_PATH} characters cannot be recycled"
    if size > allowed:
        return f"it is too big for the Recycle Bin (over {allowed // (1024 * 1024)} MB, half the bin limit)"
    return None


def _recycle_bin(path: str) -> str:
    """Send one path to the Windows Recycle Bin. Raises TrashUnavailable and leaves
    the path untouched when that cannot be done safely. Never deletes for good."""
    target = _windows_plain(path)
    reason = _recycle_refusal(target)
    if reason:
        raise TrashUnavailable(reason)
    exe = _powershell_exe()
    if not exe:
        raise TrashUnavailable("powershell.exe was not found")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", RECYCLE_PS],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
            env={**os.environ, "SWITCHBOARD_TRASH_TARGET": target}, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as error:
        raise TrashUnavailable(f"powershell.exe failed ({error})") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise TrashUnavailable(f"powershell.exe exited {result.returncode}" + (f": {detail[0]}" if detail else ""))
    if os.path.lexists(target):
        raise TrashUnavailable("the path still exists after the move")
    return "the Windows Recycle Bin"


def _move(path: str) -> tuple[str, str]:
    """Move one path to the Trash; returns (where it went, which Trash)."""
    use_cli = os.environ.get("SWITCHBOARD_TRASH_NO_CLI") != "1"
    if sys.platform == "darwin":
        cli = Path("/usr/bin/trash")
        if use_cli and cli.exists():
            result = subprocess.run([str(cli), path], capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and not os.path.lexists(path):
                return "~/.Trash (Finder Put Back)", "macos-trash"
        return _fallback_trash(path), "folder"
    if os.environ.get("SWITCHBOARD_TRASH_DIR"):
        return _fallback_trash(path), "folder"   # an explicit folder is the only Trash
    if _is_windows():
        # Restore from the Recycle Bin: Explorer, right click, Restore. A path in the old
        # ~/.switchboard-trash folder is still a plain folder you can move back by hand.
        return _recycle_bin(path), "recycle-bin"
    gio = shutil.which("gio") if use_cli else None
    if gio:
        try:
            result = subprocess.run([gio, "trash", path], capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and not os.path.lexists(path):
                return "the desktop Trash (gio trash)", "gio-trash"
        except (OSError, subprocess.SubprocessError):
            pass
    return _freedesktop_trash(path), "freedesktop-trash"


def move_to_trash(path: str) -> str:
    """Move one path to the Trash; returns where it went."""
    return _move(path)[0]


def delete(path: str) -> None:
    """Delete a temp path, build output, or an in-repo path git can restore. A symlink or junction is removed on
    its own, and a tree is removed without walking into any link in it: on Windows a
    task worktree holds junctions to main's people/, .amp, and system/state, and
    shutil.rmtree on Python 3.11 can follow a junction into them."""
    if winlink.is_link(path):
        winlink.remove_link(path)
    elif os.path.isdir(path):
        winlink.rmtree(path)
    else:
        os.unlink(path)


def parse_args(argv: list[str]) -> tuple[dict[str, bool], list[str]]:
    flags = {"force": False, "recursive": False, "verbose": False}
    paths: list[str] = []
    done = False
    for arg in argv:
        if not done and arg == "--":
            done = True
            continue
        if not done and arg.startswith("--") and len(arg) > 2:
            flags["force"] |= arg == "--force"
            flags["recursive"] |= arg == "--recursive"
            flags["verbose"] |= arg == "--verbose"
            continue
        if not done and len(arg) > 1 and arg.startswith("-") and arg[1:].isalpha():
            flags["force"] |= "f" in arg
            flags["recursive"] |= "r" in arg or "R" in arg
            flags["verbose"] |= "v" in arg
            continue
        paths.append(arg)
    return flags, paths


def run(argv: list[str]) -> int:
    flags, paths = parse_args(argv)
    status = 0
    for arg in paths:
        # Convert first: a Git Bash /c/... path that skipped MSYS conversion does not
        # exist to Windows Python until it reads C:/....
        path = _absolute(arg)
        if not os.path.lexists(path):
            if not flags["force"]:
                print(f"rm: {arg}: No such file or directory", file=sys.stderr)
                status = 1
            continue
        action, reason = classify(path)
        try:
            if action == "refuse":
                print(f"switchboard-trash: refused {path} ({reason}); nothing was deleted. Ask the owner before removing it.",
                      file=sys.stderr)
                _record({"action": "refused", "path": path, "reason": reason})
                status = 1
            elif action == "delete":
                delete(path)
                if flags["verbose"]:
                    print(path)
            else:
                try:
                    dest, via = _move(path)
                except TrashUnavailable as error:
                    print(f"switchboard-trash: could not move {path} to the Trash ({error}); nothing was deleted. "
                          "Ask the owner before removing it.", file=sys.stderr)
                    _record({"action": "failed", "path": path, "reason": str(error)})
                    status = 1
                    continue
                print(f"switchboard-trash: moved {path} to the Trash ({dest}) instead of deleting it", file=sys.stderr)
                _record({"action": "trashed", "path": path, "trashed_to": dest, "trashed_via": via})
        except OSError as error:
            print(f"rm: {arg}: {error.strerror or error}", file=sys.stderr)
            status = 1
    return status


def read_log(task_id: str = "", since: str = "") -> list[dict]:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if task_id and row.get("task_id") != task_id:
            continue
        if since and str(row.get("at") or "") < since:
            continue
        rows.append(row)
    return rows


def summary_line(rows: list[dict]) -> str:
    """One report line: "🗑️ Moved to Trash instead of deleting (2): /a; /b. Refused (1): /c (reason)."""
    trashed = [r for r in rows if r.get("action") == "trashed"]
    refused = [r for r in rows if r.get("action") == "refused"]
    failed = [r for r in rows if r.get("action") == "failed"]
    parts = []
    if trashed:
        shown = "; ".join(str(r.get("path")) for r in trashed[:10]) + (f"; and {len(trashed) - 10} more" if len(trashed) > 10 else "")
        parts.append(f"🗑️ Moved to Trash instead of deleting ({len(trashed)}): {shown}.")
    if refused:
        shown = "; ".join(f"{r.get('path')} ({r.get('reason')})" for r in refused[:5])
        parts.append(f"Refused ({len(refused)}): {shown}.")
    if failed:
        shown = "; ".join(f"{r.get('path')} ({r.get('reason')})" for r in failed[:5])
        parts.append(f"Not trashed, still in place ({len(failed)}): {shown}.")
    return " ".join(parts)


def list_main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="switchboard-trash.py --switchboard-list")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--format", choices=("line", "json"), default="json")
    args = parser.parse_args(argv)
    rows = read_log(args.task_id, args.since)
    if args.format == "line":
        line = summary_line(rows)
        if line:
            print(line)
    else:
        print(json.dumps({"count": len(rows), "rows": rows}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--switchboard-list":
        raise SystemExit(list_main(sys.argv[2:]))
    raise SystemExit(run(sys.argv[1:]))
