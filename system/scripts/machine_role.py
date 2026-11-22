#!/usr/bin/env python3
"""Per-machine role: which fixed PM OS surfaces open by default.

A personal machine skips the work extras (Meetings, Launchpad, and Scheduled Jobs) that a
work machine opens. The rule follows the machine's role, never its OS or host name.

  PM_OS_MACHINE_ROLE=personal   CoS and To Do open by default. Launchpad, Meetings
                                (the Meetings Registry), and Scheduled Jobs open only
                                when the owner says "load <surface>" in CoS
                                (system/scripts/load-surface.sh).
  PM_OS_MACHINE_ROLE=work       every surface opens, as before.
  (no key)                      work, so a machine with no file does not change.

"Not opened" is not "off": the background work behind those tabs keeps running (the
command-center tick still rebuilds meetings.json and scheduled-jobs.json, and the
Task Scheduler or launchd jobs never needed a tab). Work that does need the tab waits:
automation-dispatch.sh leaves an envelope queued while Scheduled Jobs is closed, and the
tab runs it on load. Comms Triage keeps its own switch (comms_triage_setting.py).

Two more keys in the same file, both written by this script:
  PM_OS_KEEP_SURFACES=launchpad,meetings   on-demand surfaces the owner chose to keep in
                                           the default set on a personal machine
  PM_OS_LOADED_SURFACES=2026-01-15:launchpad   surfaces loaded today (Pacific date); they
                                           count as default for the rest of that day

The file is local/machines/<MACHINE>.env on main's checkout (comms_triage_setting.py owns
the path rule). A value in the process environment wins over the file.

  machine_role.py                     print the role
  machine_role.py --set personal|work write the role (work removes the key)
  machine_role.py surfaces [--json]   the surfaces that open by default today
  machine_role.py check <surface>     exit 0 when it opens by default today, 1 when on demand
  machine_role.py resolve "<words>"   the surface key for the owner's words, or exit 2
  machine_role.py mark-loaded <surface>   record a load for today
  machine_role.py keep <surface> | unkeep <surface>

system/scripts/lib/machine-role.sh is the bash twin and seed-session.ps1 reads the role;
keep the key names and values in step.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_triage_setting as _comms  # noqa: E402  (path rule and machine name)

ROLE_KEY = "PM_OS_MACHINE_ROLE"
KEEP_KEY = "PM_OS_KEEP_SURFACES"
LOADED_KEY = "PM_OS_LOADED_SURFACES"
ROLES = ("work", "personal")
DEFAULT_ROLE = "work"

# Routine keys in sidebar order. megadesk is the CoS tab.
ALWAYS = ("megadesk", "todo")
ON_DEMAND = ("launchpad", "scheduled-jobs", "meetings")
ALL = ("megadesk", "todo", "launchpad", "comms-triage", "scheduled-jobs", "meetings")

LABELS = {
    "megadesk": "CoS",
    "todo": "To Do",
    "launchpad": "Launchpad",
    "comms-triage": "Comms Triage",
    "scheduled-jobs": "Scheduled Jobs",
    "meetings": "Meetings",
}

# The owner's words for each surface, lower case, spaces and dashes folded.
ALIASES = {
    "megadesk": ("cos", "chief of staff", "megadesk"),
    "todo": ("todo", "to do", "todos", "to dos", "to do list", "todo list"),
    "launchpad": ("launchpad", "launch pad"),
    "comms-triage": ("comms triage", "comms", "triage"),
    "scheduled-jobs": ("scheduled jobs", "scheduled job", "scheduled tasks", "scheduled task",
                       "scheduled", "jobs"),
    "meetings": ("meetings", "meeting", "meetings registry", "meeting registry"),
}


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def env_file(env: Mapping[str, str] | None = None) -> Path:
    return _comms.env_file(env)


def file_value(key: str, env: Mapping[str, str] | None = None) -> str:
    try:
        lines = env_file(env).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    value = ""
    for line in lines:
        k, sep, val = line.strip().partition("=")
        if sep and k.strip() == key:
            value = val.strip().strip("\"'")
    return value


def value(key: str, env: Mapping[str, str] | None = None) -> str:
    env = _env(env)
    return env[key] if env.get(key) else file_value(key, env)


def write_key(key: str, new: str | None, env: Mapping[str, str] | None = None) -> Path:
    """Set or clear one key, keeping every other line; a file left empty is removed."""
    path = env_file(env)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    except UnicodeDecodeError:
        lines = []
    keep = [ln for ln in lines if ln.partition("=")[0].strip() != key]
    if new:
        keep.append(f"{key}={new}")
    if keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
    return path


def role(env: Mapping[str, str] | None = None) -> str:
    raw = value(ROLE_KEY, env).strip().lower()
    return raw if raw in ROLES else DEFAULT_ROLE


def _split(raw: str) -> list[str]:
    return [p for p in (canonical(x) for x in re.split(r"[,\s]+", raw or "")) if p]


def canonical(name: str) -> str | None:
    """Routine key for a key or an alias, else None."""
    text = re.sub(r"[\s_\-]+", " ", (name or "").strip().lower()).strip(" .,!?'\"")
    text = re.sub(r"^(the|my)\s+", "", text)
    text = re.sub(r"\s+(tab|surface|workspace)$", "", text)
    for key, words in ALIASES.items():
        if text == key.replace("-", " ") or text in words:
            return key
    return None


def resolve(words: str) -> str | None:
    """Surface key from the owner's phrase: "load Meetings", "open the scheduled tasks tab"."""
    text = re.sub(r"^\s*(please\s+)?(load|open|bring up|start)\s+", "", words or "", flags=re.I)
    text = re.sub(r"\s+(and\s+)?keep(\s+it)?\s*$", "", text, flags=re.I)
    return canonical(text)


def today_pt(now: dt.datetime | None = None) -> str:
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("America/Los_Angeles")
    except Exception:  # no tz database: UTC minus 8 is close enough for a day stamp
        zone = dt.timezone(dt.timedelta(hours=-8))
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(zone).date().isoformat()


def kept(env: Mapping[str, str] | None = None) -> list[str]:
    return [k for k in _split(value(KEEP_KEY, env)) if k in ON_DEMAND]


def loaded_today(env: Mapping[str, str] | None = None, today: str | None = None) -> list[str]:
    raw = value(LOADED_KEY, env)
    day, sep, names = raw.partition(":")
    if not sep or day.strip() != (today or today_pt()):
        return []
    return [k for k in _split(names) if k in ALL]


def on_demand(env: Mapping[str, str] | None = None, today: str | None = None) -> list[str]:
    """Surfaces that wait for "load <surface>" on this machine today."""
    if role(env) != "personal":
        return []
    skip = set(kept(env)) | set(loaded_today(env, today))
    return [k for k in ON_DEMAND if k not in skip]


def opens_by_default(surface: str, env: Mapping[str, str] | None = None, today: str | None = None) -> bool:
    key = canonical(surface) or surface
    return key not in on_demand(env, today)


def default_surfaces(env: Mapping[str, str] | None = None, today: str | None = None) -> list[str]:
    comms_on = _comms.enabled(env)
    waiting = set(on_demand(env, today))
    return [k for k in ALL if k not in waiting and (comms_on or k != "comms-triage")]


def _add(key: str, surface: str, env: Mapping[str, str] | None, current: Iterable[str], prefix: str = "") -> None:
    names = list(dict.fromkeys([*current, surface]))
    write_key(key, prefix + ",".join(names), env)


def mark_loaded(surface: str, env: Mapping[str, str] | None = None, today: str | None = None) -> list[str]:
    day = today or today_pt()
    names = list(dict.fromkeys([*loaded_today(env, day), surface]))
    write_key(LOADED_KEY, f"{day}:{','.join(names)}", env)
    return names


def set_keep(surface: str, keep: bool, env: Mapping[str, str] | None = None) -> list[str]:
    names = [k for k in kept(env) if k != surface]
    if keep:
        names.append(surface)
    write_key(KEEP_KEY, ",".join(names) or None, env)
    return names


def summary(env: Mapping[str, str] | None = None) -> dict:
    return {
        "role": role(env),
        "machine": _comms.machine_name(env),
        "file": str(env_file(env)),
        "default": default_surfaces(env),
        "on_demand": on_demand(env),
        "kept": kept(env),
        "loaded_today": loaded_today(env),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=ROLES, dest="state")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("surfaces")
    s.add_argument("--json", action="store_true")
    for name in ("check", "mark-loaded", "keep", "unkeep"):
        sub.add_parser(name).add_argument("surface")
    sub.add_parser("resolve").add_argument("words", nargs="+")
    args = ap.parse_args(argv)

    if args.state:
        write_key(ROLE_KEY, None if args.state == DEFAULT_ROLE else args.state)
        if os.environ.get(ROLE_KEY):
            print(f"this shell still sets {ROLE_KEY}={os.environ[ROLE_KEY]}; new shells read the file", file=sys.stderr)
        print(role())
        return 0
    if args.cmd in (None,):
        print(role())
        return 0
    if args.cmd == "surfaces":
        info = summary()
        print(json.dumps(info) if args.json else " ".join(info["default"]))
        return 0
    if args.cmd == "resolve":
        key = resolve(" ".join(args.words))
        if not key:
            print("unknown surface: say CoS, To Do, Launchpad, Meetings, or Scheduled Jobs", file=sys.stderr)
            return 2
        print(key)
        return 0
    key = canonical(args.surface)
    if not key:
        print(f"unknown surface: {args.surface}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return 0 if opens_by_default(key) else 1
    if args.cmd == "mark-loaded":
        print(" ".join(mark_loaded(key)))
        return 0
    if key not in ON_DEMAND:
        print(f"{LABELS[key]} always opens; nothing to keep", file=sys.stderr)
        return 0
    print(" ".join(set_keep(key, args.cmd == "keep")) or "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
