#!/usr/bin/env python3
"""Trace the Windows zombie-process leak with run-time object reference tracing.

Status: dormant. If the leak returns, check first for a recent driver change, and only then trace.

    python system/scripts/windows_leak_trace.py status
    python system/scripts/windows_leak_trace.py enable     # admin terminal; ends by itself at the next reboot
    python system/scripts/windows_leak_trace.py disable    # admin terminal
    python system/scripts/windows_leak_trace.py spawn 50   # traced test processes
    python system/scripts/windows_leak_trace.py analyze <full live kernel dump.dmp>   # names and counts on stdout
    python system/scripts/windows_leak_trace.py summary                               # re-read the logs

Why: every exited process keeps exactly one kernel reference, and no user process holds
it. Object reference tracing records a stack per reference on objects under pool tag Proc, with nothing disabled. It
sets the same kernel state as `gflags /ko` through NtSetSystemInformation(SystemRefTraceInformation), which gflags.exe
wraps. Nothing is written to the registry. Only processes started after `enable` are traced.

Limits seen on the real dump: the stuck reference came from an untraced PsLookupProcessByProcessId, so no stack named its
owner, and reading the stacks by hand with offline cdb gave the result. `analyze` has never finished on a real dump: its
one run stopped partway through `!process 0 0`, so the module, callback, and !obtrace parsers have only seen test fixtures.
Live kd runs have preceded system crashes, though later cdb runs did not crash, so that link is unproven. Run any dump
analysis offline at low priority.

Windows only. `status` and `spawn` need no admin rights. `analyze` writes raw kd logs under LOCALAPPDATA/switchboard/leak-dump/out
and refuses Dropbox, git, and the backed-up folders, because those logs hold kernel memory. Its stdout has no addresses.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from ctypes import wintypes as w
from pathlib import Path

SYSTEM_REF_TRACE_INFORMATION = 86
POOL_TAG = "Proc"
TRACE_IMAGE = "hostname.exe"


class UnicodeString(ctypes.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]


class RefTraceInformation(ctypes.Structure):
    _fields_ = [("TraceEnable", w.BOOLEAN), ("TracePermanent", w.BOOLEAN),
                ("TraceProcessName", UnicodeString), ("TracePoolTags", UnicodeString)]


def _ustr(text: str) -> UnicodeString:
    size = len(text) * 2
    return UnicodeString(size, size + 2, text)


def _enable_debug_privilege() -> bool:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE

    class Luid(ctypes.Structure):
        _fields_ = [("Low", w.DWORD), ("High", w.LONG)]

    class Privileges(ctypes.Structure):
        _fields_ = [("Count", w.DWORD), ("Luid", Luid), ("Attributes", w.DWORD)]

    token = w.HANDLE()
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)):
        return False
    privileges = Privileges(1, Luid(), 2)
    if not advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(privileges.Luid)):
        return False
    advapi.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
    return ctypes.get_last_error() == 0


def set_trace(enable: bool) -> int:
    """Return the NTSTATUS of the kernel call; 0 means it took effect."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
    _enable_debug_privilege()
    # Pool tag only. The image filter means "objects created by that process", and a process object is
    # created by its parent, so filtering on the test image would trace none of the objects that matter.
    info = RefTraceInformation(enable, enable, _ustr(""), _ustr(POOL_TAG if enable else ""))
    return ntdll.NtSetSystemInformation(SYSTEM_REF_TRACE_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)) & 0xFFFFFFFF


def query_trace() -> tuple[int, str]:
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    # The kernel writes the strings inline after the header, so a header-sized buffer gets a length
    # mismatch as soon as a tag is set. That made `status` fail exactly while tracing was on.
    buffer = ctypes.create_string_buffer(1024)
    ret = ctypes.c_ulong()
    status = ntdll.NtQuerySystemInformation(SYSTEM_REF_TRACE_INFORMATION, buffer, len(buffer), ctypes.byref(ret)) & 0xFFFFFFFF
    if status:
        return status, ""
    return 0, format_trace_state(buffer.raw[:ret.value])


def format_trace_state(raw: bytes) -> str:
    header = ctypes.sizeof(RefTraceInformation)
    name_length = int.from_bytes(raw[8:10], "little")
    name_end = header + name_length
    tag_length = int.from_bytes(raw[24:26], "little")
    image = raw[header:name_end].decode("utf-16-le", "replace")
    # A set name is stored with its terminator before the tags.
    tag_start = name_end + (int.from_bytes(raw[10:12], "little") - name_length)
    tags = raw[tag_start:tag_start + tag_length].decode("utf-16-le", "replace")
    return f"enabled={bool(raw[0])} permanent={bool(raw[1])} image={image!r} tags={tags!r}"


def spawn(count: int) -> int:
    image = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / TRACE_IMAGE
    for _ in range(count):
        subprocess.run([str(image)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000, check=False)
    return count


def find_kd() -> str | None:
    found = shutil.which("kd.exe") or shutil.which("kd")
    if found:
        return found
    # WindowsApps cannot be listed without admin, so ask for the package's own folder.
    result = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-AppxPackage Microsoft.WinDbg).InstallLocation"],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    location = result.stdout.strip()
    candidate = Path(location) / "amd64" / "kd.exe" if location else None
    return str(candidate) if candidate and candidate.exists() else None


SECTION = "===SECTION "
CALLBACK_TABLES = {"process": "nt!PspCreateProcessNotifyRoutine", "thread": "nt!PspCreateThreadNotifyRoutine",
                   "image": "nt!PspLoadImageNotifyRoutine"}
ADDRESS = r"[0-9a-f]{8}`[0-9a-f]{8}"


def _section(name: str) -> str:
    return f".echo {SECTION}{name}"


def kd_commands() -> list[str]:
    """First pass: dump identity, trace state, all processes, modules, and the registered callback tables."""
    commands = [_section("target"), "vertarget", ".reload /f nt",
                _section("trace_state"), "dd nt!ObpTraceFlags L1", "dc nt!ObpTracePoolTags L4", "du nt!ObpTraceProcessName",
                _section("processes"), "!process 0 0",
                _section("modules"), "lm t n"]
    for name, symbol in CALLBACK_TABLES.items():
        commands += [_section(f"table_{name}"), f"dq {symbol} L40"]
    commands += [_section("pool"), "!poolused 4 Proc"]
    return commands


def sections(text: str) -> dict[str, str]:
    """Split a kd log on its .echo markers. A log with no markers is one section named ''."""
    out: dict[str, str] = {}
    name = ""
    for line in text.splitlines():
        if line.startswith(SECTION):
            name = line[len(SECTION):].strip()
            continue
        out[name] = out.get(name, "") + line + "\n"
    return out


def parse_processes(text: str) -> list[dict]:
    """One row per !process 0 0 entry. object_table is None when the line was absent."""
    rows = []
    for block in re.split(r"(?=^PROCESS\s)", text, flags=re.M):
        head = re.match(r"PROCESS\s+([0-9a-f`]+)", block, re.I)
        if not head:
            continue
        table = re.search(r"ObjectTable:\s*([0-9a-f`]+)", block, re.I)
        image = re.search(r"Image:\s*(\S+)", block)
        rows.append({"address": head.group(1), "image": image.group(1) if image else "?",
                     "object_table": table.group(1).replace("`", "") if table else None})
    return rows


def is_zombie(row: dict) -> bool:
    return row["object_table"] is None or int(row["object_table"], 16) == 0


def zombie_addresses(text: str, limit: int = 120) -> list[str]:
    """EPROCESS addresses of exited processes, the traced test image first."""
    zombies = [row for row in parse_processes(text) if is_zombie(row)]
    zombies.sort(key=lambda row: row["image"].lower() != TRACE_IMAGE)
    return [row["address"] for row in zombies[:limit]]


def callback_addresses(text: str) -> list[int]:
    """EX_CALLBACK values from dq output (two per line): the low four bits are a reference count.
    Each points at an EX_CALLBACK_ROUTINE_BLOCK whose function sits 8 bytes in."""
    out = []
    for line in re.findall(r"^[0-9a-f]{8}`[0-9a-f]{8}[ \t]+((?:[0-9a-f]{8}`[0-9a-f]{8}[ \t]*)+)$", text, re.M | re.I):
        for value in line.split():
            number = int(value.replace("`", ""), 16)
            if number >> 48 == 0xFFFF:
                out.append((number & ~0xF) + 8)
    return out


def second_pass_commands(first_pass: str) -> list[str]:
    """For each zombie: its counts and reference trace. For each callback: the routine it points at."""
    parts = sections(first_pass)
    commands = [_section("objects")]
    for address in zombie_addresses(parts.get("processes", first_pass)):
        commands += [f"!object {address}", f"!obtrace {address}"]
    for name in CALLBACK_TABLES:
        commands.append(_section(f"routines_{name}"))
        commands += [f"dps {value:#x} L1" for value in callback_addresses(parts.get(f"table_{name}", ""))]
    return commands


def parse_modules(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, module, stamp) from `lm t n`."""
    out = []
    for start, end, name, rest in re.findall(rf"^({ADDRESS})\s+({ADDRESS})\s+(\S+)\s+(.*)$", text, re.M | re.I):
        fields = rest.split(None, 1)
        out.append((int(start.replace("`", ""), 16), int(end.replace("`", ""), 16), name, fields[1].strip() if len(fields) > 1 else ""))
    return out


def module_of(value: int, modules: list[tuple[int, int, str, str]]) -> str | None:
    for start, end, name, _ in modules:
        if start <= value < end:
            return name
    return None


def routine_modules(text: str, modules: list[tuple[int, int, str, str]]) -> Counter:
    """Modules that own the code pointers in dps output. Pool addresses and data outside any module are skipped."""
    found: Counter = Counter()
    for value in re.findall(rf"^{ADDRESS}\s+({ADDRESS})\b", text, re.M | re.I):
        module = module_of(int(value.replace("`", ""), 16), modules)
        if module:
            found[module] += 1
    return found


def _frame(token: str, modules: list[tuple[int, int, str, str]]) -> str:
    """module!symbol with the offset gone, so stacks compare across objects. A bare address becomes its module."""
    token = token.strip()
    if re.fullmatch(r"(0x)?[0-9a-f`]{8,}", token, re.I):
        return module_of(int(token.replace("`", "").removeprefix("0x"), 16), modules) or "unknown"
    return re.sub(r"\+(0x)?[0-9a-f]+$", "", token, flags=re.I)


def parse_obtrace(text: str, modules: list[tuple[int, int, str, str]] | None = None) -> list[list[tuple[int, str, tuple[str, ...]]]]:
    """One list per traced object: (delta, tag, frames) per reference event."""
    modules = modules or []
    objects: list[list[tuple[int, str, tuple[str, ...]]]] = []
    current: list | None = None
    frames: list[str] = []
    delta, tag = 0, ""

    def flush():
        nonlocal frames
        if current is not None and frames:
            current.append((delta, tag, tuple(frames)))
        frames = []

    for line in text.splitlines():
        if re.match(r"\s*Object:\s", line):
            flush()
            current = []
            objects.append(current)
            continue
        if current is None:
            continue
        event = re.match(r"\s*[0-9a-f]+\s+([+-]\s*\d+)\s+(\S{1,4})\s+(\S+)\s*$", line, re.I)
        if event:
            flush()
            delta, tag = int(event.group(1).replace(" ", "")), event.group(2)
            frames = [_frame(event.group(3), modules)]
            continue
        if frames:
            more = re.match(r"\s+(\S+)\s*$", line)
            if more and not line.lstrip().startswith(("-", "Sequence")):
                frames.append(_frame(more.group(1), modules))
            else:
                flush()
    flush()
    return objects


def reference_balance(objects: list) -> tuple[Counter, Counter]:
    """Objects by net references, and net references by tag. Neither names a driver: a reference and its release
    usually sit on different stacks, and the stuck reference on the real dump had no stack at all."""
    over: Counter = Counter()
    by_tag: Counter = Counter()
    for events in objects:
        over[sum(delta for delta, _, _ in events)] += 1
        for delta, tag, _ in events:
            by_tag[tag] += delta
    return over, by_tag


def summarize(first_log: Path, second_log: Path) -> dict:
    """Names and counts only: no addresses, no memory contents. This is what may leave the scratch folder."""
    first = first_log.read_text(encoding="utf-8", errors="replace")
    second = second_log.read_text(encoding="utf-8", errors="replace") if second_log.exists() else ""
    one, two = sections(first), sections(second)
    modules = parse_modules(one.get("modules", ""))
    processes = parse_processes(one.get("processes", ""))
    zombies = [row for row in processes if is_zombie(row)]
    objects = parse_obtrace(two.get("objects", ""), modules)
    over, by_tag = reference_balance(objects)
    pointer_counts = Counter(int(n) for n in re.findall(r"PointerCount:\s*(\d+)", two.get("objects", "")))
    handle_counts = Counter(int(n) for n in re.findall(r"HandleCount:\s*(\d+)", two.get("objects", "")))
    stamps = {name: stamp for _, _, name, stamp in modules}
    routines = {name: routine_modules(two.get(f"routines_{name}", ""), modules) for name in CALLBACK_TABLES}
    return {
        "processes": len(processes), "zombies": len(zombies),
        "zombie_images": Counter(row["image"] for row in zombies).most_common(10),
        "trace_flags": re.findall(rf"^{ADDRESS}\s+([0-9a-f]{{8}})", one.get("trace_state", ""), re.M | re.I)[:1],
        "trace_tag": "Proc" in one.get("trace_state", ""),
        "modules": len(modules),
        "process_callbacks": sorted(routines["process"]), "thread_callbacks": sorted(routines["thread"]),
        "image_callbacks": sorted(routines["image"]),
        "callback_stamps": {m: stamps.get(m, "") for table in routines.values() for m in table},
        "objects_with_trace": sum(1 for events in objects if events),
        "objects_requested": len(zombie_addresses(one.get("processes", ""))),
        "pointer_counts": sorted(pointer_counts.items()), "handle_counts": sorted(handle_counts.items()),
        "over_reference_histogram": sorted(over.items()),
        "net_by_tag": by_tag.most_common(25),
    }


def scratch_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "switchboard" / "leak-dump" / "out"


def unsafe_location(path: Path) -> str | None:
    """Why a dump or extract may not live here, or None. Kernel memory stays out of Dropbox, git, and the backups."""
    resolved = path.resolve()
    if any(part.lower() == "dropbox" for part in resolved.parts):
        return "inside Dropbox"
    if any(part in (".claude", ".switchboard") for part in resolved.parts):
        return "inside a backed-up folder"
    probe = resolved
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_file():
        probe = probe.parent
    inside = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, check=False)
    return "inside a git checkout" if inside.stdout.strip() == "true" else None


def analyze(dump: str, out_dir: Path) -> int:
    for label, path in (("dump", Path(dump)), ("--out", out_dir)):
        reason = unsafe_location(path)
        if reason:
            print(f"refused: {label} is {reason}. Kernel memory stays out of Dropbox, git, and backups. Default --out: {scratch_dir()}")
            return 2
    kd = find_kd()
    if not kd:
        print("kd.exe not found: winget install --id Microsoft.WinDbg")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = out_dir / "symbols"
    env = dict(os.environ, _NT_SYMBOL_PATH=f"srv*{symbols}*https://msdl.microsoft.com/download/symbols")

    def run_kd(log: Path, commands: list[str]) -> int:
        # A command file, because kd limits -c, and a block's function needs a pointer read per callback.
        script = log.with_suffix(".kdcmd")
        script.write_text("\n".join([*commands, "q"]) + "\n", encoding="utf-8")
        return subprocess.run([kd, "-z", dump, "-logo", str(log), "-cf", str(script)], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600, check=False).returncode

    first = out_dir / "leak-dump-first.log"
    code = run_kd(first, kd_commands())
    text = first.read_text(encoding="utf-8", errors="replace")
    second = out_dir / "leak-dump-second.log"
    code = run_kd(second, second_pass_commands(text)) or code
    print(f"kd exit {code}")
    print(json.dumps(summarize(first, second), indent=1))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    s = sub.add_parser("spawn")
    s.add_argument("count", type=int, nargs="?", default=50)
    a = sub.add_parser("analyze")
    a.add_argument("dump")
    a.add_argument("--out", default=None, help="scratch folder outside Dropbox, git, and backups")
    r = sub.add_parser("summary", help="re-read the logs of an earlier analyze: names and counts only")
    r.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("windows_leak_trace: Windows only")
        return 0
    if args.cmd == "status":
        status, text = query_trace()
        print(text if status == 0 else f"query status {status:#x}")
        return 0
    if args.cmd in ("enable", "disable"):
        status = set_trace(args.cmd == "enable")
        if status:
            print(f"{args.cmd}: failed, status {status:#x}" + (" (needs an admin terminal)" if status in (0xC0000061, 0xC0000022) else ""))
            return 1
        print(f"{args.cmd}: ok. " + query_trace()[1])
        return 0
    if args.cmd == "spawn":
        print(f"spawned {spawn(args.count)} {TRACE_IMAGE}")
        return 0
    out = Path(args.out) if args.out else scratch_dir()
    if args.cmd == "summary":
        print(json.dumps(summarize(out / "leak-dump-first.log", out / "leak-dump-second.log"), indent=1))
        return 0
    return analyze(args.dump, out)


if __name__ == "__main__":
    sys.exit(main())
