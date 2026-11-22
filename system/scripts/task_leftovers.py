#!/usr/bin/env python3
"""task_leftovers.py: stop the processes a finished PM OS task left behind.

Why: two kinds of orphans can outlive their tasks. A prototype's dev server
(cmd.exe, node.exe and conhost.exe, its parent gone) can run in a shared folder
outside any worktree, so nothing ties it to a checkout. A `tail -f | grep` monitor
can follow a log in a worktree after that worktree is removed. dispatch.py's
kill_task_processes() only matches bash/python/node/claude roots whose command line
names the task id with a report or launch marker, so it sees neither.

What counts as a task's leftover (each is a "root"; a root's descendants go with it):
  * a process whose working directory is inside the task's worktree or its scratch
    (`<worktree>/.amp/in`, which lives inside the worktree, and main's
    `.amp/in/<task-id>`, where the scratch moves when the worktree goes);
  * a process with an open file there (macOS and Linux, from lsof; Windows has no
    cheap open-file list, so the command line stands in for it);
  * a process whose command line names a path there (the tail monitor);
  * a dev server (vite, next dev, npm run dev, http.server, ...) whose environment
    carries PM_OS_WORKER_TASK_ID=<task-id>: every worker exports it and every child
    inherits it, wherever the server runs.

Never stopped, whatever matches:
  * this process and its ancestors;
  * a program that is not a shell, interpreter, dev tool, or console helper
    (KILLABLE): the owner's Brave, wmux, cmux, Obsidian, editors, Dropbox, and every
    claude session (the worker's own claude is kill_task_processes()' business);
  * PM OS helpers that are parentless by design (PROTECTED_MARKERS): the owner Brave
    connection broker (owner_browser.py), the Obsidian link server (note-links.py),
    keep-awake (keep_awake.py), the scheduled `dispatch.py release`, the board, the
    command-center loop, report waiters and report-to-cos replays;
  * anything a live task claims: its environment names a live task, or its working
    directory, open files, or command line sit in a live task's worktree or scratch;
  * the children of a protected helper (a report waiter's queue reader);
  * anything still attached to a live program that is not a shell or dev tool (a
    wmux or cmux pane, mintty, Terminal, an editor, a live claude session, Explorer):
    an owner's shell that happens to sit in an old worktree, or a worker that is not
    as dead as it looked. A true orphan's parent is gone (Windows keeps the dead PID)
    or is launchd/init (macOS and Linux reparent to PID 1);
  * a process whose command line and working directory could not be read.

A child is followed only when it was created at or after its parent, so a reused PID
is never walked. Each stop is logged to stderr and appended to a JSONL log, with
token-looking values in the command line redacted. Before a kill the PID is checked
again (creation time on Windows, command line elsewhere).

Kill switch: PM_OS_LEFTOVER_SWEEP=off. The test sandbox (PM_OS_TEST_SANDBOX=1) reads
an empty process list and refuses to kill, so only fake process lists reach the
planner in tests.

CLI (dry run unless --apply):
  python3 system/scripts/task_leftovers.py scan               every finished, reaped task
  python3 system/scripts/task_leftovers.py scan --task-id rt-x --worktree <path>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

TASK_ID = r"rt-[0-9a-f]{8,}"
TASK_ENV = "PM_OS_WORKER_TASK_ID"
TERMINAL_STATUSES = {"blocked", "deferred", "done", "failed", "cancelled"}   # running-task-state.py

# Program names (lowercase, no .exe, python3.12 -> python3) that may be stopped.
KILLABLE = {
    "bash", "sh", "zsh", "dash", "fish", "cmd", "conhost",
    "python", "python3", "pythonw", "py", "uv", "uvicorn", "flask", "gunicorn",
    "node", "bun", "deno", "npm", "npx", "pnpm", "yarn", "esbuild", "tsx", "ts-node",
    "tail", "grep", "egrep", "rg", "sleep", "cat", "tee", "awk", "gawk", "sed", "watch",
    "timeout", "xargs", "less", "head", "inotifywait", "fswatch", "entr",
}
# Command-line markers (normalized: lowercase, forward slashes) of processes that
# outlive their launcher on purpose.
PROTECTED_MARKERS = (
    "dispatch.py", "router-dispatch.sh", "owner_browser.py", "note-links.py", "keep_awake.py",
    "pm-os-board.py", "cmux-shim.py", "command-center-loop.sh", "report-waiter.sh",
    "report-to-cos.sh", "end-of-day-close.sh", "/programs/wmux/", "/wmux/resources/", "cmux.app/",
)
INIT_PIDS = {0, 1}   # launchd / init adopt orphans on macOS and Linux; never an attachment
DEV_SERVER = re.compile(
    r"\bvite(\.js|\.cmd)?\b"
    r"|\bnext(\.js)?\s+(dev|start)\b|\bnuxt\s+dev\b|\bastro\s+dev\b|\bremix\s+dev\b"
    r"|\bwebpack(-dev-server|\s+serve)\b|\bparcel\b|\blive-server\b|\bhttp-server\b"
    r"|\b(npm|pnpm|yarn|bun)(\.cmd)?\s+(run\s+)?(dev|start|serve|preview)\b"
    r"|\bhttp\.server\b|\buvicorn\b|\bflask\s+run\b|\bgunicorn\b|\bstorybook\b"
)
SECRETISH = re.compile(r"((?:token|secret|password|passwd|api[_-]?key|auth)[\w-]*[=:\s]+)\S+", re.I)


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str                      # program name as KILLABLE spells it
    cmdline: str = ""
    cwd: str = ""
    created: float | None = None   # epoch seconds
    env_task: str = ""             # PM_OS_WORKER_TASK_ID from the process environment
    open_paths: tuple[str, ...] = field(default_factory=tuple)


# --- normalization ---------------------------------------------------------------------

_MSYS_DRIVE = re.compile(r"(^|[\s\"'=(:,])/(?:cygdrive/|mnt/)?([a-z])/")


def norm_name(name: str) -> str:
    base = re.split(r"[\\/]", (name or "").strip())[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    m = re.fullmatch(r"(python[w3]?)[\d.]*", base)
    return m.group(1) if m else base


def norm_text(text: str) -> str:
    """Lowercase, forward slashes, and Git Bash / Cygwin / WSL drive paths (/c/Users,
    /cygdrive/c/, /mnt/c/) spelled as c:/Users, so one root string matches every form."""
    out = (text or "").replace("\\", "/").lower()
    if os.name == "nt" or re.search(r"\b[a-z]:/", out):
        out = _MSYS_DRIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}:/", out)
    out = re.sub(r"(?<!:)/{2,}", "/", out)
    return out


def norm_path(path: str | os.PathLike[str]) -> str:
    out = norm_text(str(path)).strip().strip('"')
    if out.startswith("//?/") or out.startswith("/??/"):
        out = out[4:]
    return out.rstrip("/")


def roots_for(task_id: str, worktree: str | os.PathLike[str] | None, repo_root: str | os.PathLike[str],
              worktrees_root: str | os.PathLike[str] | None = None) -> list[str]:
    """The task's worktree and its scratch, normalized, each also in its resolved form."""
    paths: list[str] = []
    wt = worktree or (Path(worktrees_root) / task_id if worktrees_root else None)
    for raw in ([wt] if wt else []) + [Path(repo_root) / ".amp" / "in" / task_id]:
        for form in {str(raw), os.path.realpath(str(raw))}:
            normed = norm_path(form)
            if normed and normed not in paths:
                paths.append(normed)
    return paths


def _boundary_after(text: str, end: int) -> bool:
    return end >= len(text) or text[end] in "/\"' \t\r\n;:,)|&>"


def path_inside(path: str, root: str) -> bool:
    p = norm_path(path)
    return bool(p) and (p == root or p.startswith(root + "/"))


def mentions(cmdline_norm: str, root: str) -> bool:
    start = cmdline_norm.find(root)
    while start != -1:
        if _boundary_after(cmdline_norm, start + len(root)):
            return True
        start = cmdline_norm.find(root, start + 1)
    return False


def protected(proc: Proc) -> str:
    cl = norm_text(proc.cmdline)
    for marker in PROTECTED_MARKERS:
        if marker in cl:
            return f"protected helper ({marker.strip('/')})"
    return ""


def is_dev_server(proc: Proc) -> bool:
    return bool(DEV_SERVER.search(norm_text(proc.cmdline)))


def redact(cmdline: str, limit: int = 240) -> str:
    text = SECRETISH.sub(lambda m: m.group(1) + "<redacted>", cmdline or "")
    return text if len(text) <= limit else text[:limit] + "…"


# --- planning (pure) -------------------------------------------------------------------

def _path_hit(proc: Proc, roots: Iterable[str]) -> str:
    cl = norm_text(proc.cmdline)
    for root in roots:
        if proc.cwd and path_inside(proc.cwd, root):
            return f"working directory in {root}"
        for opened in proc.open_paths:
            if path_inside(opened, root):
                return f"open file in {root}"
        if mentions(cl, root):
            return f"command line names {root}"
    return ""


def _ancestors(proc: Proc, by_pid: dict[int, Proc]) -> list[Proc]:
    out, seen, cur = [], {proc.pid}, proc
    while cur.ppid and cur.ppid in by_pid and cur.ppid not in seen:
        parent = by_pid[cur.ppid]
        if parent.created is not None and cur.created is not None and parent.created > cur.created:
            break   # the parent PID was reused by a newer process: not an ancestor
        out.append(parent)
        seen.add(parent.pid)
        cur = parent
    return out


def plan(procs: list[Proc], finished: dict[str, list[str]], *, live: dict[str, list[str]] | None = None,
         dev_tasks: Iterable[str] = (), self_pid: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Which processes to stop. `finished` maps each finished task id to its roots (path
    rule); `dev_tasks` are the finished ids whose env-tagged dev servers go too; `live`
    maps every live task id to its roots. Returns {"stop": [...], "spared": [...]},
    stop ordered leaves first."""
    live = {tid: r for tid, r in (live or {}).items() if tid not in finished}
    dev_tasks = set(dev_tasks)
    by_pid = {p.pid: p for p in procs}
    me = by_pid.get(self_pid if self_pid is not None else os.getpid())
    spare = {self_pid if self_pid is not None else os.getpid()}
    if me is not None:
        spare |= {a.pid for a in _ancestors(me, by_pid)}

    def live_claim(p: Proc) -> str:
        if p.env_task and p.env_task in live:
            return f"environment names live task {p.env_task}"
        for tid, roots in live.items():
            if _path_hit(p, roots):
                return f"inside live task {tid}"
        return ""

    def lineage(p: Proc) -> str:
        """Why an ancestor keeps `p` alive: a protected helper above it, or a live
        program that is not a shell or dev tool (a terminal, editor, claude, Explorer)."""
        for a in _ancestors(p, by_pid):
            if a.pid in INIT_PIDS:
                break
            why = protected(a)
            if why:
                return f"child of a {why}"
            if norm_name(a.name) not in KILLABLE:
                return f"attached to a live {norm_name(a.name) or '?'} (pid {a.pid})"
        return ""

    def veto(p: Proc) -> str:
        if p.pid in spare:
            return "this process or its ancestor"
        if norm_name(p.name) not in KILLABLE:
            return f"not a stoppable program ({norm_name(p.name) or '?'})"
        if not p.cmdline and not p.cwd:
            return "details unreadable"
        return protected(p) or live_claim(p) or lineage(p)

    stop: dict[int, dict[str, Any]] = {}
    spared: list[dict[str, Any]] = []
    roots: list[tuple[Proc, str, str]] = []
    for p in procs:
        hit, owner = "", ""
        for tid, troots in finished.items():
            hit = _path_hit(p, troots)
            if hit:
                owner = tid
                break
        if not hit and p.env_task in dev_tasks and is_dev_server(p):
            hit, owner = f"dev server started by {p.env_task}", p.env_task
        if not hit:
            continue
        why_not = veto(p)
        if why_not:
            spared.append({"pid": p.pid, "name": p.name, "task_id": owner, "match": hit, "reason": why_not})
            continue
        roots.append((p, owner, hit))

    children: dict[int, list[Proc]] = {}
    for p in procs:
        parent = by_pid.get(p.ppid)
        if parent is not None and p.pid != p.ppid and (parent.created is None or p.created is None or p.created >= parent.created):
            children.setdefault(parent.pid, []).append(p)

    depth: dict[int, int] = {}
    for root, owner, hit in roots:
        stack = [(root, 0, hit)]
        while stack:
            p, d, why = stack.pop()
            if p.pid in stop:
                continue
            if d and veto(p):
                spared.append({"pid": p.pid, "name": p.name, "task_id": owner, "match": f"child of {root.pid}", "reason": veto(p)})
                continue
            stop[p.pid] = {"pid": p.pid, "ppid": p.ppid, "name": p.name, "task_id": owner, "reason": why,
                           "created": p.created, "cmdline": p.cmdline}
            depth[p.pid] = d
            for c in children.get(p.pid, []):
                stack.append((c, d + 1, f"child of {p.pid} ({why})"))
    ordered = sorted(stop.values(), key=lambda v: -depth[v["pid"]])
    return {"stop": ordered, "spared": spared}


def candidate_task_ids(procs: list[Proc], worktrees_root: str, repo_root: str,
                       extra_roots: Iterable[str] = ()) -> set[str]:
    """Task ids named by a process's path (under the worktrees root, main's .amp/in, or
    any folder a ledger row recorded its worktree in) or by its environment."""
    folders = [worktrees_root, str(Path(repo_root) / ".amp" / "in"), *extra_roots]
    prefixes = {norm_path(f) for f in folders} | {norm_path(os.path.realpath(f)) for f in folders}
    pattern = re.compile("(?:" + "|".join(re.escape(p) for p in sorted(prefixes) if p) + f")/({TASK_ID})(?=[/\"' \\t;:,)|&>]|$)")
    found: set[str] = set()
    for p in procs:
        if p.env_task and re.fullmatch(TASK_ID, p.env_task):
            found.add(p.env_task)
        for text in (norm_path(p.cwd) if p.cwd else "", norm_text(p.cmdline), *(norm_path(o) for o in p.open_paths)):
            found.update(m.group(1) for m in pattern.finditer(text))
    return found


def sweep_targets(procs: list[Proc], tasks: list[dict[str, Any]], *, worktrees_root: str, repo_root: str
                  ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(finished, live) for the global sweep. Finished means a ledger row that is
    terminal and already reaped, or no ledger row at all and no worktree on disk.
    A terminal row reap left open (blocked, keep-open, report pending) is neither."""
    rows = {str(t.get("id")): t for t in tasks if t.get("id")}
    live = {tid: roots_for(tid, t.get("worktree"), repo_root, worktrees_root)
            for tid, t in rows.items() if t.get("status") not in TERMINAL_STATUSES}
    finished: dict[str, list[str]] = {}
    recorded = {str(Path(t["worktree"]).parent) for t in tasks if t.get("worktree")}
    for tid in candidate_task_ids(procs, worktrees_root, repo_root, recorded):
        row = rows.get(tid)
        if row is None:
            if (Path(worktrees_root) / tid).exists():
                continue
        elif row.get("status") not in TERMINAL_STATUSES or not row.get("reaped_at"):
            continue
        finished[tid] = roots_for(tid, (row or {}).get("worktree"), repo_root, worktrees_root)
    return finished, live


# --- process snapshot ------------------------------------------------------------------

def sandboxed() -> bool:
    return os.environ.get("PM_OS_TEST_SANDBOX") == "1"


def disabled() -> bool:
    return os.environ.get("PM_OS_LEFTOVER_SWEEP", "").strip().lower() in {"off", "0", "false", "no"}


def snapshot() -> list[Proc]:
    if sandboxed():
        return []
    try:
        return _windows_snapshot() if os.name == "nt" else _posix_snapshot()
    except Exception as error:   # best effort: a failed read stops nothing
        print(f"task_leftovers: process snapshot failed: {error}", file=sys.stderr)
        return []


_FILETIME_UNIX = 116444736000000000


def _windows_snapshot() -> list[Proc]:
    import ctypes
    from ctypes import wintypes as w

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return []   # a 32-bit Python cannot read 64-bit process parameters
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD), ("heap", ctypes.c_size_t),
                    ("module", w.DWORD), ("threads", w.DWORD), ("ppid", w.DWORD), ("pri", ctypes.c_long),
                    ("flags", w.DWORD), ("exe", ctypes.c_wchar * 260)]

    class Basic(ctypes.Structure):
        _fields_ = [("exit", ctypes.c_void_p), ("peb", ctypes.c_void_p), ("affinity", ctypes.c_void_p),
                    ("base", ctypes.c_void_p), ("pid", ctypes.c_void_p), ("ppid", ctypes.c_void_p)]

    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.OpenProcess.restype = w.HANDLE
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    k.IsWow64Process.argtypes = [w.HANDLE, ctypes.POINTER(w.BOOL)]
    k.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_size_t)]
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_ulong)]

    def read(handle: int, addr: int, size: int) -> bytes | None:
        if not addr or size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not k.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def u64(blob: bytes, off: int) -> int:
        return int.from_bytes(blob[off:off + 8], "little")

    def ustring(handle: int, params: bytes, off: int) -> str:
        length = int.from_bytes(params[off:off + 2], "little")
        raw = read(handle, u64(params, off + 8), length) if length else b""
        return (raw or b"").decode("utf-16-le", "replace")

    entries: list[tuple[int, int, str]] = []
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == w.HANDLE(-1).value:
        return []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            entries.append((int(entry.pid), int(entry.ppid), entry.exe))
            more = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)

    procs: list[Proc] = []
    for pid, ppid, exe in entries:
        if pid in (0, 4):
            continue
        proc = Proc(pid=pid, ppid=ppid, name=norm_name(exe))
        handle = k.OpenProcess(0x0410, False, pid) or k.OpenProcess(0x1000, False, pid)   # query + vm read, else limited
        if not handle:
            procs.append(proc)
            continue
        try:
            created, exited, kern, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            if k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kern), ctypes.byref(user)):
                proc.created = ((created.dwHighDateTime << 32 | created.dwLowDateTime) - _FILETIME_UNIX) / 1e7
            wow = w.BOOL(0)
            k.IsWow64Process(handle, ctypes.byref(wow))
            basic, got = Basic(), ctypes.c_ulong(0)
            if not wow.value and nt.NtQueryInformationProcess(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), ctypes.byref(got)) == 0:
                peb = read(handle, int(basic.peb or 0), 0x28)
                params = read(handle, u64(peb, 0x20), 0x400) if peb else None
                if params is None and peb:
                    params = read(handle, u64(peb, 0x20), 0x88)
                if params and len(params) >= 0x88:
                    proc.cwd = ustring(handle, params, 0x38)
                    proc.cmdline = ustring(handle, params, 0x70)
                    env_size = u64(params, 0x3F0) if len(params) >= 0x3F8 else 0
                    if not 0 < env_size <= 4 << 20:
                        env_size = 32768
                    env = read(handle, u64(params, 0x80), env_size) or read(handle, u64(params, 0x80), 4096)
                    if env:
                        for item in env.decode("utf-16-le", "replace").split("\0"):
                            if item[:len(TASK_ENV) + 1].upper() == TASK_ENV + "=":
                                proc.env_task = item[len(TASK_ENV) + 1:].strip().lower()
                                break
        finally:
            k.CloseHandle(handle)
        procs.append(proc)
    return procs


def parse_etime(text: str) -> int | None:
    """ps etime, [[dd-]hh:]mm:ss, in seconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", (text or "").strip())
    if not m:
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def parse_ps(out: str, now: float) -> list[Proc]:
    """`ps -axww -o pid=,ppid=,etime=,command=` rows."""
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        secs = parse_etime(parts[2])
        command = parts[3] if len(parts) > 3 else ""
        argv0 = command.split(" ", 1)[0] if command else ""
        procs.append(Proc(pid=int(parts[0]), ppid=int(parts[1]), name=norm_name(argv0), cmdline=command,
                          created=None if secs is None else float(int(now) - secs)))
    return procs


def parse_comm(out: str) -> dict[int, str]:
    """`ps -axww -o pid=,comm=` rows: {pid: program name}. comm is the executable path
    and may hold spaces, so it is the whole rest of the line."""
    names = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            names[int(parts[0])] = norm_name(parts[1].rstrip())
    return names


def parse_env_tasks(out: str) -> dict[int, str]:
    """`ps -axwwe -o pid=,command=` rows: the PM_OS_WORKER_TASK_ID each process carries."""
    found = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            m = re.search(rf"(?:^|\s){TASK_ENV}=({TASK_ID})(?=\s|$)", parts[1])
            if m:
                found[int(parts[0])] = m.group(1)
    return found


def parse_lsof(out: str) -> tuple[dict[int, str], dict[int, list[str]]]:
    """`lsof -Fpfn` output: ({pid: cwd}, {pid: [open paths]})."""
    cwd: dict[int, str] = {}
    opened: dict[int, list[str]] = {}
    pid, fd = None, ""
    for line in out.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "f":
            fd = value
        elif tag == "n" and pid is not None and value.startswith("/"):
            if fd == "cwd":
                cwd[pid] = value
            elif fd not in {"txt", "mem", "rtd"}:
                opened.setdefault(pid, []).append(value)
    return cwd, opened


def _posix_snapshot() -> list[Proc]:
    def run(cmd: list[str], timeout: int) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace").stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    procs = parse_ps(run(["ps", "-axww", "-o", "pid=,ppid=,etime=,command="], 30), time.time())
    names = parse_comm(run(["ps", "-axww", "-o", "pid=,comm="], 30))
    env = parse_env_tasks(run(["ps", "-axwwe", "-o", "pid=,command="], 30))
    cwd, opened = parse_lsof(run(["lsof", "-nP", "-w", "-u", str(os.getuid()), "-Fpfn"], 60))
    for p in procs:
        p.name = names.get(p.pid) or p.name   # argv0 splits "/Applications/Visual Studio Code.app/..." at the space
        p.env_task = env.get(p.pid, "")
        p.cwd = cwd.get(p.pid, "")
        p.open_paths = tuple(opened.get(p.pid, ()))
    return procs


# --- stopping --------------------------------------------------------------------------

def _windows_kill(victim: dict[str, Any]) -> str:
    import ctypes
    from ctypes import wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = w.HANDLE
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    if victim.get("created") is None:
        return "creation time unknown, so a reused PID cannot be ruled out"
    handle = k.OpenProcess(0x0001 | 0x1000, False, int(victim["pid"]))   # terminate + query limited
    if not handle:
        return "gone or access denied"
    try:
        created, a, b, c = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(a), ctypes.byref(b), ctypes.byref(c)):
            return "unreadable"
        now_created = ((created.dwHighDateTime << 32 | created.dwLowDateTime) - _FILETIME_UNIX) / 1e7
        if abs(now_created - float(victim["created"])) > 0.01:
            return "pid reused"
        return "" if k.TerminateProcess(handle, 1) else f"terminate failed ({ctypes.get_last_error()})"
    finally:
        k.CloseHandle(handle)


def _posix_kill(victim: dict[str, Any]) -> str:
    pid = int(victim["pid"])
    try:
        now_cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unreadable"
    if not now_cmd:
        return "gone"
    if now_cmd != str(victim.get("cmdline") or "").strip():
        return "pid reused"
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return ""
    except PermissionError:
        return "permission denied"
    return ""


def kill(victim: dict[str, Any]) -> str:
    """Stop one planned victim; "" on success, else why not."""
    if sandboxed():
        return "test sandbox"
    return _windows_kill(victim) if os.name == "nt" else _posix_kill(victim)


def execute(planned: dict[str, list[dict[str, Any]]], *, context: str, log_path: str | os.PathLike[str] | None = None,
            killer: Callable[[dict[str, Any]], str] | None = None, dry_run: bool = False,
            log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Stop each planned process, log each stop, and summarize."""
    killer = killer or kill
    log = log or (lambda msg: print(f"task_leftovers: {msg}", file=sys.stderr))
    stopped, failed = [], []
    lines = []
    for victim in planned["stop"]:
        outcome = "dry run" if dry_run else killer(victim)
        record = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "context": context,
                  "pid": victim["pid"], "name": victim["name"], "task_id": victim["task_id"],
                  "reason": victim["reason"], "cmdline": redact(str(victim.get("cmdline") or "")),
                  "result": "stopped" if not outcome else outcome}
        if not outcome:
            stopped.append(record)
            log(f"{victim['task_id']}: stopped {victim['name']} pid {victim['pid']} ({victim['reason']}) during {context}")
        elif dry_run:
            stopped.append(record)
        else:
            failed.append(record)
            log(f"{victim['task_id']}: could not stop {victim['name']} pid {victim['pid']}: {outcome}")
        lines.append(json.dumps(record, sort_keys=True))
    if lines and log_path and not dry_run:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as error:
            log(f"could not write {log_path}: {error}")
    tasks = sorted({r["task_id"] for r in stopped})
    return {"stopped": len(stopped), "failed": len(failed), "tasks": tasks, "spared": len(planned["spared"]),
            "records": stopped + failed, "dry_run": dry_run}


# --- entry points used by dispatch.py --------------------------------------------------

def stop_in_worktree(task_id: str, worktree: str | os.PathLike[str], tasks: list[dict[str, Any]], *,
                     repo_root: str | os.PathLike[str], worktrees_root: str | os.PathLike[str],
                     procs: list[Proc] | None = None, log_path: str | os.PathLike[str] | None = None,
                     killer: Callable[[dict[str, Any]], str] | None = None, dry_run: bool = False,
                     log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Before a worktree is removed or parked: stop what runs inside it or its scratch
    (path rule only). The caller has already settled that no worker runs there."""
    if disabled():
        return {"stopped": 0, "skipped": "PM_OS_LEFTOVER_SWEEP=off"}
    procs = snapshot() if procs is None else procs
    if not procs:
        return {"stopped": 0}
    live = {str(t["id"]): roots_for(str(t["id"]), t.get("worktree"), repo_root, worktrees_root)
            for t in tasks if t.get("id") and t.get("status") not in TERMINAL_STATUSES and t.get("id") != task_id}
    finished = {task_id: roots_for(task_id, worktree, repo_root, worktrees_root)}
    return execute(plan(procs, finished, live=live), context="worktree removal", log_path=log_path,
                   killer=killer, dry_run=dry_run, log=log)


def sweep(tasks: list[dict[str, Any]], *, repo_root: str | os.PathLike[str], worktrees_root: str | os.PathLike[str],
          procs: list[Proc] | None = None, log_path: str | os.PathLike[str] | None = None,
          killer: Callable[[dict[str, Any]], str] | None = None, dry_run: bool = False,
          log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """After reap: stop what every finished, reaped task left behind (path rule and
    env-tagged dev servers), including tasks whose worktree is already gone."""
    if disabled():
        return {"stopped": 0, "skipped": "PM_OS_LEFTOVER_SWEEP=off"}
    procs = snapshot() if procs is None else procs
    if not procs:
        return {"stopped": 0}
    finished, live = sweep_targets(procs, tasks, worktrees_root=str(worktrees_root), repo_root=str(repo_root))
    if not finished:
        return {"stopped": 0}
    return execute(plan(procs, finished, live=live, dev_tasks=finished), context="reap sweep", log_path=log_path,
                   killer=killer, dry_run=dry_run, log=log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="list what would be stopped (dry run unless --apply)")
    scan.add_argument("--task-id", help="only this task's worktree (path rule), as at worktree removal")
    scan.add_argument("--worktree", help="that task's worktree path (default <worktrees>/<task-id>)")
    scan.add_argument("--apply", action="store_true", help="stop them")
    args = parser.parse_args(argv)

    import subprocess as sp
    repo = Path(sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or ".")
    sys.path.insert(0, str(repo / "system" / "scripts"))
    from repo_paths import main_checkout  # noqa: E402
    main_root = main_checkout(repo)
    worktrees = Path(os.environ.get("PM_OS_WORKTREES_DIR") or main_root / ".amp" / "worktrees")
    state = Path(os.environ.get("PM_OS_STATE_DIR") or main_root / "system" / "state")
    try:
        tasks = json.loads((state / "running-tasks.json").read_text(encoding="utf-8")).get("tasks") or []
    except (OSError, ValueError) as error:
        print(json.dumps({"error": f"ledger unreadable: {error}"}))
        return 1
    log_path = state / "worker-dispatch" / "leftover-stops.jsonl"
    common = dict(repo_root=main_root, worktrees_root=worktrees, log_path=log_path, dry_run=not args.apply)
    if args.task_id:
        result = stop_in_worktree(args.task_id, args.worktree or worktrees / args.task_id, tasks, **common)
    else:
        result = sweep(tasks, **common)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
