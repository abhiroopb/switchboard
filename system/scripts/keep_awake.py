#!/usr/bin/env python3
"""Keep a Windows machine awake while PM OS work is live, and let it sleep when none is.

Keep the machine awake while work runs. On a Modern Standby laptop the display
turning off starts standby, so workers can stall overnight. `system/windows/keep-awake.ps1` holds SYSTEM_REQUIRED and
DISPLAY_REQUIRED from one hidden process and changes no power setting. This module
decides when that hold runs:

* `dispatch.py sweep` (every command-center tick) calls `sync_rows(rows)` and
  `dispatch.py launch` calls `sync(live)`. Live work means a worker that is actually
  working, or an unexpired long-job lease. A row holding a worker slot
  (`live_workers`) counts only while its session shows life within QUIET_MINUTES:
  a Claude transcript or sub-agent write, a background task's output or a job still
  holding its output file open, a heartbeat, or a fresh start. A worker parked on a question to the owner, or sitting idle in
  its tab, stops holding. Idle CoS, idle wmux, and other apps never count here. While work is live, `keep-awake.ps1 -Ensure` starts the
  hold or moves its end to now + HOLD_MINUTES, in place, so there is one holder and
  no gap. With nothing live, `-Release -Owner dispatch` ends it. A hold the owner
  started by hand (`keep-awake.ps1 -Hours 10`) is never ended here.
* The hold is bounded: if the tick stops running, the hold ends HOLD_MINUTES after
  the last sync on its own.
* The screen stays on while work runs. That is the cost of holding DISPLAY_REQUIRED
  on this laptop, and it is expected.
* macOS and Linux do nothing here: `sync` returns `skipped`. The Mac's own long
  jobs use `caffeinate` (lib/mux.sh mux_keep_awake).

Long jobs that are not workers (a suite run, an overnight script) take a lease:

    python3 system/scripts/keep_awake.py lease --name suite-rerun --minutes 180
    python3 system/scripts/keep_awake.py unlease --name suite-rerun

Other commands: `sync` (the same pass the tick runs), `status`.
Kill switch: PM_OS_KEEP_AWAKE=off (the next sync releases a dispatch-owned hold).
Log: .state/keep-awake.log (written by keep-awake.ps1).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402



@lru_cache(maxsize=1)
def repo_root() -> Path:
    # Lazy: dispatch.py imports this module on every run and every platform, and
    # main_checkout runs git; only a Windows sync needs the path.
    return main_checkout(Path(os.environ.get("REPO_ROOT") or SCRIPTS.parents[1]))


def state_dir() -> Path:
    return Path(os.environ.get("PM_OS_KEEP_AWAKE_STATE_DIR") or repo_root() / ".state")


def lease_dir_default() -> Path:
    return state_dir() / "keep-awake-leases"


LOCK_PATH: Path | None = None   # tests point this at a temp file; None means state_dir()/keep-awake.lock
PS1 = SCRIPTS.parent / "windows" / "keep-awake.ps1"
OWNER = "dispatch"
# Longer than the 15-minute Windows tick, so one late tick never drops the hold,
# and short enough that a dead tick lets the laptop sleep within the hour.
HOLD_MINUTES = int(os.environ.get("PM_OS_KEEP_AWAKE_MINUTES") or 60)
# A worker quiet this long is parked or idle, not working. Longer than the 10-minute
# foreground tool timeout, so one long command never reads as idle.
QUIET_MINUTES = int(os.environ.get("PM_OS_KEEP_AWAKE_QUIET_MINUTES") or 20)
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

Runner = Callable[[list[str]], dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return (os.environ.get("PM_OS_KEEP_AWAKE") or "on").strip().lower() not in {"off", "0", "false", "no"}


def on_windows() -> bool:
    """Live Windows only. The test sandbox reads as not Windows, like dispatch._on_windows,
    so no suite ever starts a real hold on this laptop."""
    return os.name == "nt" and os.environ.get("PM_OS_TEST_SANDBOX") != "1"


# --- long-job leases -------------------------------------------------------------------

def lease(name: str, minutes: int, lease_dir: Path | None = None) -> dict[str, Any]:
    if not _NAME.match(name):
        raise ValueError("lease name: letters, digits, dot, dash, underscore; at most 64")
    if not 1 <= minutes <= 24 * 60:
        raise ValueError("lease minutes: 1 to 1440")
    d = lease_dir or lease_dir_default()
    d.mkdir(parents=True, exist_ok=True)
    until = _now() + timedelta(minutes=minutes)
    row = {"name": name, "until": until.isoformat(), "pid": os.getpid(), "created_at": _now().isoformat()}
    (d / f"{name}.json").write_text(json.dumps(row), encoding="utf-8")
    return row


def unlease(name: str, lease_dir: Path | None = None) -> bool:
    if not _NAME.match(name):
        return False
    path = (lease_dir or lease_dir_default()) / f"{name}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def active_leases(lease_dir: Path | None = None, now: datetime | None = None) -> list[str]:
    """Names of unexpired leases. Expired lease files are removed."""
    d = lease_dir or lease_dir_default()
    now = now or _now()
    out = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            until = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["until"])
        except (OSError, ValueError, KeyError, TypeError):
            until = None
        if until is not None and until > now:
            out.append(path.stem)
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return out


# --- the hold --------------------------------------------------------------------------

def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("pwsh.exe")


def run_ps1(args: list[str]) -> dict[str, Any]:
    """Run keep-awake.ps1 with -Json and return its last JSON line."""
    exe = _pwsh()
    if not exe:
        return {"error": "pwsh not found"}
    try:
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-File", str(PS1), *args, "-Json"],
                              capture_output=True, text=True, timeout=60, cwd=str(repo_root()),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": f"keep-awake.ps1: {error}"[:300]}
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip().startswith("{")]
    if proc.returncode or not lines:
        return {"error": ((proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}")[:300]}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"error": lines[-1][:300]}


@contextmanager
def _sync_lock(path: Path, wait_seconds: float = 30.0) -> Iterator[bool]:
    """One sync at a time (the tick and a launch can overlap). Windows byte-range
    lock through msvcrt; elsewhere sync never runs, so no lock is needed."""
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    got = False
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
        yield got
    finally:
        if got:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fh.close()


def _state_file_exists() -> bool:
    return (state_dir() / "keep-awake.json").exists()


def sync(live: list[str], *, runner: Runner | None = None, windows: bool | None = None,
         lease_dir: Path | None = None, state_exists: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Hold while anything is live, release when nothing is. Never raises.

    `live` names what holds a worker slot (task ids); leases are added here.
    Returns {"action": started|extended|kept|released|idle|skipped|left: …|error, "live": [...]}.
    """
    try:
        if not (on_windows() if windows is None else windows):
            return {"action": "skipped", "reason": "not Windows"}
        run = runner or run_ps1
        reasons = list(live) + [f"lease:{n}" for n in active_leases(lease_dir)]
        if not enabled():
            reasons = []
        with _sync_lock(LOCK_PATH or state_dir() / "keep-awake.lock") as locked:
            if not locked:
                return {"action": "skipped", "reason": "another sync holds the lock", "live": reasons}
            if reasons:
                minutes = max(HOLD_MINUTES, 5)
                result = run(["-Ensure", "-Hours", f"{minutes / 60:.4f}", "-Owner", OWNER])
            elif (state_exists or _state_file_exists)():
                # Spawn pwsh only when a hold may exist: an idle tick on an idle laptop costs nothing.
                result = run(["-Release", "-Owner", OWNER])
            else:
                result = {"action": "idle"}
        out = {**result, "live": reasons[:10], "live_count": len(reasons)}
        if not enabled():
            out["disabled"] = True
        return out
    except Exception as error:  # noqa: BLE001  (a keep-awake fault never blocks a launch or a sweep)
        return {"action": "error", "error": str(error)[:300]}


# --- which slot-holding workers are actually working --------------------------------

def claude_temp_dir() -> Path:
    """Claude Code keeps each session's tool and background-task output under
    %TEMP%/claude/<project hash>/<session id>/tasks/."""
    return Path(os.environ.get("PM_OS_CLAUDE_TEMP_DIR") or Path(tempfile.gettempdir()) / "claude")


def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _file_in_use(path: Path) -> bool:
    """True when another process holds the file open (Windows: an exclusive open fails
    with a sharing violation). A running background job keeps its output file open even
    while it prints nothing. Elsewhere, or on any other error, False."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        # GENERIC_READ, share mode 0 (exclusive), OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            return ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return False


def _task_output_touched(session: str, temp_dir: Path, now: datetime | None = None,
                         in_use: Callable[[Path], bool] | None = None) -> datetime | None:
    """Newest sign of life in this session's tool or background-task output files: the
    newest write, or now when a recent output file is still held open by its job (a
    quiet build or a wait loop prints nothing for long stretches)."""
    now = now or _now()
    check = in_use or _file_in_use
    latest = None
    try:
        for tasks in temp_dir.glob(f"*/{session}/tasks"):
            for f in tasks.iterdir():
                stamp = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if now - stamp < timedelta(hours=24) and check(f):
                    return now
                latest = stamp if latest is None or stamp > latest else latest
    except (OSError, ValueError):
        pass
    return latest


def working(rows: list[dict[str, Any]], *, tail: Callable[[dict[str, Any]], dict[str, Any] | None],
            now: datetime | None = None, temp_dir: Path | None = None,
            quiet_minutes: int | None = None, in_use: Callable[[Path], bool] | None = None) -> dict[str, list[str]]:
    """Split slot-holding rows into {"working": ids, "idle": ids}.

    `tail` is running-task-state's claude_tail: the worker's transcript and sub-agent
    files. A row with no session yet, or no readable transcript (an Amp worker), counts
    as working, as before. Any other row counts only with life inside the quiet window."""
    now = now or _now()
    quiet = timedelta(minutes=quiet_minutes if quiet_minutes is not None else QUIET_MINUTES)
    temp = temp_dir or claude_temp_dir()
    out: dict[str, list[str]] = {"working": [], "idle": []}
    for row in rows:
        session = str(row.get("session_id") or row.get("thread_id") or "")
        try:
            info = tail(row) if session else None
        except Exception:  # noqa: BLE001  (an unreadable transcript, say mid-write, counts as working)
            info = None
        if not session or info is None:
            out["working"].append(row["id"])
            continue
        stamps = [info.get("last_at"), _parse(row.get("heartbeat_at")), _parse(row.get("started_at")),
                  _parse(row.get("launched_at")), _task_output_touched(session, temp, now, in_use)]
        latest = max((s for s in stamps if isinstance(s, datetime)), default=None)
        out["working" if latest is not None and now - latest < quiet else "idle"].append(row["id"])
    return out


def sync_rows(rows: list[dict[str, Any]], *, tail: Callable[[dict[str, Any]], dict[str, Any] | None],
              **kw: Any) -> dict[str, Any]:
    """The sweep's pass: hold only for rows that are working; report the idle ones."""
    try:
        if not (on_windows() if kw.get("windows") is None else kw["windows"]):
            return {"action": "skipped", "reason": "not Windows"}
        split = working(rows, tail=tail)
    except Exception as error:  # noqa: BLE001  (never blocks a sweep)
        return {"action": "error", "error": str(error)[:300]}
    result = sync(split["working"], **kw)
    if split["idle"]:
        result["idle_workers"] = split["idle"][:10]
    return result


def live_from_ledger() -> list[str]:
    import dispatch  # heavy; only the CLI needs it
    rows = dispatch.live_workers(dispatch.ledger.load_payload())
    try:
        return working(rows, tail=dispatch.ledger.claude_tail)["working"]
    except Exception:  # noqa: BLE001  (fail toward holding, as before this filter)
        return [t["id"] for t in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="hold while work is live, release when none is")
    sub.add_parser("status", help="print the holder and the active leases")
    lp = sub.add_parser("lease", help="keep awake for a long job that is not a worker")
    lp.add_argument("--name", required=True)
    lp.add_argument("--minutes", type=int, required=True)
    up = sub.add_parser("unlease", help="end a long-job lease")
    up.add_argument("--name", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "lease":
        row = lease(args.name, args.minutes)
        print(json.dumps({**row, "sync": sync(live_from_ledger()) if on_windows() else {"action": "skipped"}}))
    elif args.cmd == "unlease":
        removed = unlease(args.name)
        print(json.dumps({"removed": removed, "sync": sync(live_from_ledger()) if on_windows() else {"action": "skipped"}}))
    elif args.cmd == "sync":
        print(json.dumps(sync(live_from_ledger() if on_windows() else [])))
    else:
        holder = run_ps1(["-Status"]) if on_windows() else {"holding": False, "reason": "not Windows"}
        print(json.dumps({"holder": holder, "leases": active_leases(), "enabled": enabled()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
