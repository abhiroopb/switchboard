#!/usr/bin/env python3
"""Measure how fast this machine creates processes, and who creates them (Windows).

    python3 system/scripts/windows_process_rate.py rate --seconds 300
    python3 system/scripts/windows_process_rate.py count -- bash -lc 'exit 0'
    python3 system/scripts/windows_process_rate.py census [--since-minutes 60] [--json out.json]

Why: a kernel leak keeps every exited process object until reboot, about 80 KB of
commit each, so a process start is the unit of cost on this machine. All three commands read the kernel in-process
with no admin rights and start no helper process of their own:

- rate reads the cumulative allocation counter of pool tag Proc, one count per process object created.
- count runs a command inside a job object and reads TotalProcesses, an exact count of its whole tree with no
  background noise. It includes the command itself.
- census opens every PID. While the leak is active the dead processes stay openable, so it can rebuild the process
  tree of the boot with image, parent and start time. A dead process keeps no command line, so a tree is named by
  its first live ancestor and by its image names. It reads zero once the leak is fixed, which is a useful result too.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time

_FILETIME_UNIX = 116444736000000000
_QUERY_LIMITED = 0x1000
_STILL_ACTIVE = 259


def proc_counters() -> tuple[int, int] | None:
    """Cumulative (allocations, frees) of nonpaged pool tag Proc; None when not readable."""
    if os.name != "nt":
        return None
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    size = 1 << 20
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(22, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if status == 0xC0000004:
            size = max(size * 2, ret.value + 4096)
            continue
        if status:
            return None
        raw = buf.raw
        for i in range(int.from_bytes(raw[:4], "little")):
            off = 8 + i * 40
            if raw[off:off + 4] == b"Proc":
                return (int.from_bytes(raw[off + 24:off + 28], "little"),
                        int.from_bytes(raw[off + 28:off + 32], "little"))
        return None
    return None


def rate(seconds: float) -> dict[str, float] | None:
    first = proc_counters()
    if first is None:
        return None
    t0 = time.monotonic()
    time.sleep(seconds)
    second = proc_counters()
    if second is None:
        return None
    elapsed = time.monotonic() - t0
    created = second[0] - first[0]
    return {"seconds": round(elapsed, 1), "created": created, "freed": second[1] - first[1],
            "per_minute": round(created * 60 / elapsed, 1)}


def count_tree(argv: list[str], timeout: float = 600, stdin: bytes | None = None,
               env: dict[str, str] | None = None, cwd: str | None = None) -> dict[str, object]:
    """Run argv in a job object; return its exact process count, wall time and exit code."""
    if os.name != "nt":
        raise OSError("count needs Windows job objects")
    import ctypes
    from ctypes import wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.AssignProcessToJobObject.restype = w.BOOL
    k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = w.BOOL
    k.OpenThread.restype = w.HANDLE
    k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]

    class Accounting(ctypes.Structure):
        _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong), ("this_user", ctypes.c_longlong),
                    ("this_kernel", ctypes.c_longlong), ("page_faults", w.DWORD), ("total", w.DWORD),
                    ("active", w.DWORD), ("terminated", w.DWORD)]

    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    start = time.perf_counter()
    # CREATE_SUSPENDED so the first child cannot escape before it joins the job.
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=cwd,
                            creationflags=0x4 | 0x08000000)
    try:
        if not k.AssignProcessToJobObject(job, w.HANDLE(int(proc._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        _resume(k, proc.pid)
        if stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()
        code = proc.wait(timeout=timeout)
        info = Accounting()
        # Tree members can outlive the root; wait briefly so the total is final.
        for _ in range(100):
            k.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None)
            if info.active == 0:
                break
            time.sleep(0.1)
        return {"processes": info.total, "still_active": info.active, "seconds": round(time.perf_counter() - start, 2),
                "exit": code, "pid": proc.pid}
    finally:
        k.CloseHandle(job)


def _resume(k, pid: int) -> None:
    import ctypes
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD), ("owner", w.DWORD),
                    ("base", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]

    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.Thread32First.argtypes = [w.HANDLE, ctypes.c_void_p]
    k.Thread32Next.argtypes = [w.HANDLE, ctypes.c_void_p]
    k.ResumeThread.argtypes = [w.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x4, 0)
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    more = k.Thread32First(snap, ctypes.byref(entry))
    while more:
        if entry.owner == pid:
            thread = k.OpenThread(0x2, False, entry.tid)
            if thread:
                k.ResumeThread(thread)
                k.CloseHandle(thread)
        more = k.Thread32Next(snap, ctypes.byref(entry))
    k.CloseHandle(snap)


def read_processes(max_pid: int = 4_000_000, stop_after_misses: int = 250_000) -> dict[int, dict[str, object]]:
    """Open every PID. Stops after a long empty run of PIDs, because Windows hands PIDs out low first."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k.OpenProcess.restype = w.HANDLE
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_ulong)]

    class Basic(ctypes.Structure):
        _fields_ = [("exit", ctypes.c_void_p), ("peb", ctypes.c_void_p), ("affinity", ctypes.c_void_p),
                    ("base", ctypes.c_void_p), ("pid", ctypes.c_void_p), ("ppid", ctypes.c_void_p)]

    rows: dict[int, dict[str, object]] = {}
    buf = ctypes.create_unicode_buffer(1024)
    name_buf = ctypes.create_string_buffer(4096)
    misses = 0
    for pid in range(4, max_pid, 4):
        handle = k.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            misses += 4
            if misses >= stop_after_misses:
                break
            continue
        misses = 0
        code = w.DWORD(0)
        k.GetExitCodeProcess(handle, ctypes.byref(code))
        size = w.DWORD(1024)
        image = buf.value if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        if not image:
            # A dead process fails the call above, but still answers ProcessImageFileName with its NT path.
            got = ctypes.c_ulong(0)
            if nt.NtQueryInformationProcess(handle, 27, name_buf, ctypes.sizeof(name_buf), ctypes.byref(got)) == 0:
                length = int.from_bytes(name_buf.raw[:2], "little")
                image = name_buf.raw[16:16 + length].decode("utf-16-le", "replace").replace("\\", "/")
        basic = Basic()
        got = ctypes.c_ulong(0)
        status = nt.NtQueryInformationProcess(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), ctypes.byref(got))
        created, exited, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
        k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
        rows[pid] = {"image": os.path.basename(image.replace("\\", "/")).lower(),
                     "ppid": int(basic.ppid or 0) if status == 0 else 0,
                     "alive": code.value == _STILL_ACTIVE,
                     "created": ((created.dwHighDateTime << 32 | created.dwLowDateTime) - _FILETIME_UNIX) / 1e7}
        k.CloseHandle(handle)
    return rows


def live_root(pid: int, rows: dict[int, dict[str, object]]) -> str:
    """Image of the first live ancestor. A PID reused by a newer process is not an ancestor."""
    seen = set()
    current = pid
    created = rows[pid]["created"]
    while True:
        seen.add(current)
        parent = rows[current]["ppid"]
        row = rows.get(parent)
        if row is None or parent in seen or row["created"] > created:
            return "(ancestor unreadable)"
        if row["alive"]:
            return str(row["image"]) or "(unnamed)"
        current = parent
        created = row["created"]


def census(rows: dict[int, dict[str, object]], since_minutes: float | None = None,
           now: float | None = None) -> dict[str, object]:
    now = time.time() if now is None else now
    cutoff = 0.0 if since_minutes is None else now - since_minutes * 60
    dead = {p: r for p, r in rows.items() if not r["alive"] and r["created"] >= cutoff}
    minutes = max(1 / 60, ((now - min((r["created"] for r in dead.values()), default=now)) / 60))

    def parent_image(row: dict[str, object]) -> str:
        parent = rows.get(row["ppid"])
        return str(parent["image"]) or "(unnamed)" if parent else "(unreadable)"

    return {
        "dead": len(dead), "live": sum(1 for r in rows.values() if r["alive"]), "window_minutes": round(minutes, 1),
        "per_minute": round(len(dead) / minutes, 1),
        "images": collections.Counter(str(r["image"]) or "(unnamed)" for r in dead.values()).most_common(25),
        "live_roots": collections.Counter(live_root(p, rows) for p in dead).most_common(15),
        "parent_child": [[f"{a} > {b}", n] for (a, b), n in collections.Counter(
            (parent_image(r), str(r["image"]) or "(unnamed)") for r in dead.values()).most_common(30)],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rate")
    r.add_argument("--seconds", type=float, default=60)
    c = sub.add_parser("count")
    c.add_argument("--timeout", type=float, default=600)
    c.add_argument("argv", nargs=argparse.REMAINDER)
    z = sub.add_parser("census")
    z.add_argument("--since-minutes", type=float)
    z.add_argument("--json", dest="json_path")
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("windows_process_rate: Windows only")
        return 0
    if args.cmd == "rate":
        out = rate(args.seconds)
        if out is None:
            print("pool tags not readable")
            return 2
        print(json.dumps(out))
    elif args.cmd == "count":
        cmd = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not cmd:
            print("count: no command given")
            return 2
        print(json.dumps(count_tree(cmd, timeout=args.timeout)))
    else:
        out = census(read_processes(), args.since_minutes)
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=1)
        print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
