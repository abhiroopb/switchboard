#!/usr/bin/env python3
"""Per-machine switch for the Comms Triage surface.

SWITCHBOARD_COMMS_TRIAGE=off in local/machines/<MACHINE>.env turns Comms Triage off on that
machine: no tab opened, seeded, or pinned, no collector run at start of day, no queue
card, and nothing in the CoS digests. The feature stays in the kit. A machine with no
such file behaves as before, so the default is on.

local/ is gitignored and left out of the kit, and Dropbox syncs it. The file is named for
the machine so a checkout shared across machines switches off only the machine it names:
<MACHINE> is the host name up to its first dot, in upper case.

  comms_triage_setting.py              print on or off
  comms_triage_setting.py --set off    write the key to this machine's file
  comms_triage_setting.py --set on     remove the key

A value in the process environment wins over the file, so one command can override it
with SWITCHBOARD_COMMS_TRIAGE=on. system/scripts/lib/comms-triage.sh is the bash twin and
seed-session.ps1 reads the same file; keep the off values and the path in step.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Mapping

KEY = "SWITCHBOARD_COMMS_TRIAGE"
OFF_VALUES = {"off", "0", "false", "no", "disabled"}
REPO = Path(__file__).resolve().parents[2]


def local_dir(env: Mapping[str, str] | None = None) -> Path:
    """Always main's folder: a write into a worktree's own local/ would vanish with the worktree."""
    env = os.environ if env is None else env
    if env.get("SWITCHBOARD_LOCAL_DIR"):
        return Path(env["SWITCHBOARD_LOCAL_DIR"])
    if env.get("SWITCHBOARD_ROOT"):
        return Path(env["SWITCHBOARD_ROOT"]) / "local"
    from local_backup import main_checkout  # reads the .git file, so no git process
    return main_checkout(REPO) / "local"


def machine_name(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("SWITCHBOARD_MACHINE") or socket.gethostname()).split(".")[0].upper()


def env_file(env: Mapping[str, str] | None = None) -> Path:
    return local_dir(env) / "machines" / f"{machine_name(env)}.env"


def raw_value(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if env.get(KEY):
        return env[KEY]
    try:
        lines = env_file(env).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    value = ""
    for line in lines:
        key, sep, val = line.strip().partition("=")
        if sep and key.strip() == KEY:
            value = val.strip().strip("\"'")
    return value


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return raw_value(env).strip().lower() not in OFF_VALUES


def write_setting(state: str, env: Mapping[str, str] | None = None) -> Path:
    """Set or clear the key, keeping every other line; a file left empty is removed.
    A file that is not UTF-8 (PowerShell 5.1's > writes UTF-16) is replaced, not a crash."""
    path = env_file(env)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    except UnicodeDecodeError:
        lines = []
    keep = [ln for ln in lines if ln.partition("=")[0].strip() != KEY]
    if state == "off":
        keep.append(f"{KEY}=off")
    if keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=("on", "off"), dest="state")
    args = ap.parse_args(argv)
    if args.state:
        write_setting(args.state)
        if os.environ.get(KEY):
            print(f"this shell still sets {KEY}={os.environ[KEY]}; new shells read the file", file=sys.stderr)
    print("on" if enabled() else "off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
