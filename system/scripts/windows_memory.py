#!/usr/bin/env python3
"""Windows memory health: commit charge, free RAM, and leaked (zombie) processes.

    python3 system/scripts/windows_memory.py            # one line
    python3 system/scripts/windows_memory.py --json     # the snapshot
    python3 system/scripts/windows_memory.py --check [--commit-pct 70] [--zombies 50000]
                                                        # exit 1 when unhealthy

Why: a laptop can hit its commit limit with only about a third of it in process
private bytes. The rest can be hundreds of thousands of
zombie processes: every process that exited kept its kernel process object
(pool tags Proc, MiP2, PsIn, Toke), about 80 KB of commit each, because a
kernel driver keeps a reference. No user process holds them, so killing
processes frees nothing; only a reboot does. PM OS
creates processes fast on Windows (Git Bash forks, python per call), so the
leak can grow by hundreds per minute and starve RAM until a 1-line `cmd /c exit`
takes most of a second.

Reads everything in-process with ctypes (GetPerformanceInfo and
NtQuerySystemInformation class 22, no admin), so a probe adds no process of its
own. Zombies = outstanding Proc pool allocations minus live processes. On any
other OS, or when a call fails, fields are None and --check passes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_COMMIT_PCT = 85.0
DEFAULT_ZOMBIES = 50_000
_SYSTEM_POOLTAG_INFORMATION = 22
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


def _perf_info() -> dict[str, float] | None:
    import ctypes
    from ctypes import wintypes

    class PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                    ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                    ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                    ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                    ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                    ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]

    info = PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(info)
    if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(info), info.cb):
        return None
    page, gb = info.PageSize, 1024 ** 3
    return {"commit_used_gb": info.CommitTotal * page / gb, "commit_limit_gb": info.CommitLimit * page / gb,
            "ram_total_gb": info.PhysicalTotal * page / gb, "free_ram_gb": info.PhysicalAvailable * page / gb,
            "nonpaged_pool_gb": info.KernelNonpaged * page / gb, "live_processes": float(info.ProcessCount)}


def pool_tags() -> dict[str, dict[str, int]] | None:
    """Every kernel pool tag: outstanding allocations and bytes, paged and nonpaged
    (NtQuerySystemInformation class 22, no admin). None when unreadable."""
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None  # SYSTEM_POOLTAG layout below is the x64 one
    ntdll = ctypes.WinDLL("ntdll")
    size = 1 << 20
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(_SYSTEM_POOLTAG_INFORMATION, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, ret.value + 65536)
            continue
        if status != 0:
            return None
        raw, out = buf.raw, {}
        for i in range(int.from_bytes(raw[0:4], "little")):
            off = 8 + i * 40
            u32 = lambda o: int.from_bytes(raw[off + o:off + o + 4], "little")  # noqa: E731
            u64 = lambda o: int.from_bytes(raw[off + o:off + o + 8], "little")  # noqa: E731
            out[raw[off:off + 4].decode("latin-1")] = {
                "paged": max(0, u32(4) - u32(8)), "paged_bytes": u64(16),
                "nonpaged": max(0, u32(24) - u32(28)), "nonpaged_bytes": u64(32)}
        return out
    return None


# Kernel pool tags that can leak with no user handle behind them, growing over
# days of uptime. `D2d ` is a driver tag; the rest are nt object tags a
# driver keeps referenced. Only a reboot frees them.
LEAK_TAGS = ("Toke", "SeAt", "SeTl", "SeTd", "File", "Etwr", "EtwD", "D2d ")


def pools_summary(tags: dict[str, dict[str, int]] | None) -> str:
    if not tags:
        return "kernel pools: not readable"
    total = sum(v["paged_bytes"] + v["nonpaged_bytes"] for v in tags.values()) / _GB
    leak = {t: tags[t] for t in LEAK_TAGS if t in tags}
    leak_gb = sum(v["paged_bytes"] + v["nonpaged_bytes"] for v in leak.values()) / _GB
    parts = ", ".join(f"{t.strip()} {v['paged'] + v['nonpaged']:,}" for t, v in leak.items())
    return f"kernel pools {total:.2f} GB; leak-prone tags {leak_gb:.2f} GB ({parts})"


def _proc_objects() -> int | None:
    """Outstanding nonpaged allocations under pool tag 'Proc' (one per process object, live or zombie)."""
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None  # SYSTEM_POOLTAG layout below is the x64 one
    ntdll = ctypes.WinDLL("ntdll")
    size = 1 << 20
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(_SYSTEM_POOLTAG_INFORMATION, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, ret.value + 4096)
            continue
        if status != 0:
            return None
        raw = buf.raw
        count = int.from_bytes(raw[0:4], "little")
        # x64 SYSTEM_POOLTAG, 40 bytes from offset 8: Tag, PagedAllocs, PagedFrees, pad,
        # PagedUsed(8), NonPagedAllocs, NonPagedFrees, NonPagedUsed(8).
        for i in range(count):
            off = 8 + i * 40
            if raw[off:off + 4] == b"Proc":
                np_off = off + 24
                allocs = int.from_bytes(raw[np_off:np_off + 4], "little")
                frees = int.from_bytes(raw[np_off + 4:np_off + 8], "little")
                return max(0, allocs - frees)
        return None
    return None


_GB = 1024 ** 3
# Free disk a growing pagefile never takes, so the ceiling stays honest on a nearly full drive.
_DISK_RESERVE_GB = 10.0


def parse_paging_files(entries: list[str]) -> list[tuple[str, float | None]]:
    """The PagingFiles registry value as (path, max_gb) pairs; max_gb None means
    system-managed (grows on demand). '?:\\pagefile.sys' is system-managed on the
    system drive; 'C:\\pagefile.sys 0 0' is system-managed on C; 'C:\\pagefile.sys
    4096 8192' is a fixed range in MB, so its max is 8 GB."""
    out: list[tuple[str, float | None]] = []
    for entry in entries or []:
        parts = str(entry).strip().split()
        if not parts:
            continue
        sizes: list[float] = []
        while len(parts) > 1 and len(sizes) < 2 and parts[-1].isdigit():
            sizes.insert(0, float(parts.pop()))   # trailing sizes in MB; the path may hold spaces
        path = " ".join(parts)
        if path.startswith("?:"):
            path = (os.environ.get("SystemDrive") or "C:") + path[2:]
        # No sizes or "0 0" is system-managed; "min max" is fixed at max; a lone
        # initial size is a fixed pagefile Windows does not grow past.
        hi = sizes[-1] if sizes else 0.0
        out.append((path, None if hi <= 0 else hi / 1024.0))
    return out


def managed_pagefile_max_gb(ram_gb: float, volume_gb: float) -> float:
    """Windows' cap for a system-managed pagefile: 3 x RAM or 4 GB, whichever is
    larger, limited to an eighth of the volume (Microsoft, 'How to determine the
    appropriate page file size')."""
    return min(max(3.0 * ram_gb, 4.0), volume_gb / 8.0)


def commit_ceiling_gb(commit_limit_gb: float, ram_gb: float,
                      pagefiles: list[tuple]) -> float:
    """The most commit can reach without anything being closed: today's limit
    plus the room each pagefile can still grow. `pagefiles` holds
    (current_gb, fixed_max_gb or None for system-managed, volume_gb, free_gb).
    A fixed pagefile at its max adds nothing; a system-managed one grows to
    managed_pagefile_max_gb, never past the free disk minus a reserve. An
    optional fifth field names the drive: pagefiles on one drive share its free
    space instead of each counting all of it."""
    room = 0.0
    budget: dict[str, float] = {}
    for i, row in enumerate(pagefiles):
        current, fixed_max, volume, free = row[:4]
        drive = str(row[4]).upper() if len(row) > 4 else f"#{i}"
        left = budget.setdefault(drive, max(0.0, free - _DISK_RESERVE_GB))
        cap = fixed_max if fixed_max is not None else managed_pagefile_max_gb(ram_gb, volume)
        grow = max(0.0, min(cap - current, left))
        budget[drive] = left - grow
        room += grow
    return commit_limit_gb + room


def _pagefile_ceiling(commit_limit_gb: float, ram_gb: float) -> tuple[float, bool] | None:
    """(ceiling GB, any pagefile system-managed) from the registry and the disk, or None."""
    import shutil
    import winreg
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management")
    try:
        entries = winreg.QueryValueEx(key, "PagingFiles")[0]
    finally:
        winreg.CloseKey(key)
    files = parse_paging_files(entries if isinstance(entries, list) else [entries])
    rows, managed = [], False
    for path, fixed_max in files:
        drive = os.path.splitdrive(path)[0] + "\\"
        usage = shutil.disk_usage(drive)
        try:
            current = os.stat(path).st_size / _GB
        except OSError:
            current = max(0.0, commit_limit_gb - ram_gb) / max(1, len(files))
        rows.append((current, fixed_max, usage.total / _GB, usage.free / _GB, drive))
        managed = managed or fixed_max is None
    return commit_ceiling_gb(commit_limit_gb, ram_gb, rows), managed


def spawn_ms(samples: int = 3) -> float | None:
    """Median milliseconds to start and finish `cmd /c exit`: the symptom the
    hold exists for (hundreds of ms per spawn when RAM is starved, tens of ms
    on a healthy machine even near the current limit). None off Windows. A spawn that
    fails or times out reads as infinite: at true exhaustion CreateProcess fails
    (WinError 1455), which is exactly when the hold must apply."""
    import subprocess
    import time
    if os.name != "nt":
        return None
    times = []
    try:
        for _ in range(max(1, samples)):
            t0 = time.perf_counter()
            subprocess.run(["cmd.exe", "/c", "exit"], capture_output=True, timeout=30)
            times.append((time.perf_counter() - t0) * 1000)
    except (OSError, subprocess.SubprocessError):
        return float("inf")
    times.sort()
    return times[len(times) // 2]


def snapshot() -> dict[str, float | None]:
    keys = ("commit_used_gb", "commit_limit_gb", "commit_pct", "commit_ceiling_gb", "commit_ceiling_pct",
            "ram_total_gb", "free_ram_gb", "nonpaged_pool_gb", "live_processes", "zombie_processes")
    out: dict[str, float | None] = dict.fromkeys(keys)
    if os.name != "nt":
        return out
    try:
        perf = _perf_info()
    except Exception:  # noqa: BLE001 - keep what the other probe read
        perf = None
    if perf:
        out.update(perf)
        if perf["commit_limit_gb"]:
            out["commit_pct"] = 100.0 * perf["commit_used_gb"] / perf["commit_limit_gb"]
        try:
            ceiling = _pagefile_ceiling(perf["commit_limit_gb"], perf["ram_total_gb"])
        except Exception:  # noqa: BLE001 - no ceiling means dispatch falls back to the current limit
            ceiling = None
        if ceiling and ceiling[0] > 0:
            out["commit_ceiling_gb"] = ceiling[0]
            out["commit_ceiling_pct"] = 100.0 * perf["commit_used_gb"] / ceiling[0]
    try:
        procs = _proc_objects()
    except Exception:  # noqa: BLE001 - keep what the other probe read
        procs = None
    if procs is not None and out.get("live_processes") is not None:
        out["zombie_processes"] = float(max(0, procs - int(out["live_processes"] or 0)))
    return out


def problems(snap: dict[str, float | None], commit_pct: float = DEFAULT_COMMIT_PCT,
             zombies: int = DEFAULT_ZOMBIES) -> list[str]:
    """Plain reasons the machine is unhealthy for new workers; empty when healthy or unreadable."""
    out = []
    pct = snap.get("commit_pct")
    if pct is not None and commit_pct > 0 and pct >= commit_pct:
        out.append(f"memory commit {snap['commit_used_gb']:.1f} of {snap['commit_limit_gb']:.1f} GB ({pct:.0f}%)")
    z = snap.get("zombie_processes")
    if z is not None and zombies > 0 and z >= zombies:
        out.append(f"{int(z):,} zombie processes (a kernel driver leak; only a reboot frees them)")
    return out


def summary(snap: dict[str, float | None]) -> str:
    if snap.get("commit_pct") is None:
        return "windows memory: not available on this OS"
    def gb(key: str) -> str:
        v = snap.get(key)
        return "?" if v is None else f"{v:.2f}"

    z = snap.get("zombie_processes")
    ceiling = snap.get("commit_ceiling_pct")
    grows = "" if ceiling is None else f", can grow to {gb('commit_ceiling_gb')} GB ({ceiling:.0f}% of that)"
    return (f"windows memory: commit {gb('commit_used_gb')}/{gb('commit_limit_gb')} GB "
            f"({snap['commit_pct']:.0f}%{grows}), free RAM {gb('free_ram_gb')} GB, "
            f"nonpaged pool {gb('nonpaged_pool_gb')} GB, live processes {int(snap.get('live_processes') or 0)}, "
            f"zombies {'?' if z is None else f'{int(z):,}'}")


def leak_test(n: int) -> int:
    """Spawn n `cmd /c exit` children and compare Proc objects before and after.
    Background churn adds noise, so run it on a quiet machine; a leak shows as a
    delta close to n. Use it to find the driver: quit one suspect app (a VPN
    client's split tunneling, for example), rerun, and see whether the delta drops to about 0."""
    import subprocess
    import time
    if os.name != "nt":
        print("leak test: Windows only")
        return 0
    before = _proc_objects()
    t0 = time.perf_counter()
    for _ in range(n):
        subprocess.run(["cmd.exe", "/c", "exit"], capture_output=True)
    ms = (time.perf_counter() - t0) * 1000 / n
    time.sleep(2)
    after = _proc_objects()
    if before is None or after is None:
        print("leak test: pool tags not readable")
        return 2
    kept = after - before
    print(f"leak test: spawned {n}, process objects +{kept} ({ms:.0f} ms per spawn). "
          + ("Leak active: exited processes are not freed." if kept >= n * 0.8
             else "No leak from these spawns." if kept <= n * 0.2 else "Unclear; rerun on a quieter machine."))
    return 1 if kept >= n * 0.8 else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pools", action="store_true", help="also print kernel pool use and the leak-prone tags")
    ap.add_argument("--spawn", action="store_true", help="also print the median process start time (3 spawns)")
    ap.add_argument("--check", action="store_true", help="exit 1 when commit or zombies are over the limits")
    ap.add_argument("--commit-pct", type=float, default=DEFAULT_COMMIT_PCT)
    ap.add_argument("--zombies", type=int, default=DEFAULT_ZOMBIES)
    ap.add_argument("--leak-test", type=int, metavar="N", default=0,
                    help="spawn N trivial processes and report how many stayed as zombies "
                         "(about N means the driver leak is active; about 0 means it is not)")
    args = ap.parse_args(argv)
    if args.leak_test > 0:
        return leak_test(args.leak_test)
    snap = snapshot()
    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print(summary(snap))
    if args.pools:
        print(pools_summary(pool_tags() if os.name == "nt" else None))
    if args.spawn:
        ms = spawn_ms()
        print("spawn: not available" if ms is None else f"spawn: {ms:.0f} ms median to start cmd /c exit")
    if args.check:
        bad = problems(snap, args.commit_pct, args.zombies)
        if bad:
            print("unhealthy: " + "; ".join(bad) + ". Reboot before starting workers.")
            return 1
        print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
