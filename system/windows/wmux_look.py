#!/usr/bin/env python3
"""Give wmux the switchboard look: theme, font, layout. Appearance and layout only.

    python system/windows/wmux_look.py apply [--force] [--dry-run]
    python system/windows/wmux_look.py check

The look lives in `system/windows/wmux-look.json`. `apply` merges its keys into
wmux's own settings file, `%APPDATA%\\wmux\\settings.json`, which wmux reads at
startup (its `settings-store.ts`), and sets the sidebar width in
`%APPDATA%\\wmux\\sessions\\session.json` when that file exists. Every other key
in both files is kept. Nothing personal is written: no folders, workspace names,
history, accounts or shell.

Safety rules:

- A value the user already set to something else is a conflict. Without
  `--force` the script lists the conflicts, writes nothing and exits 3. With
  `--force` it backs the file up first.
- Any file it changes is copied to `<file>.switchboard-bak-<UTC time>` first.
- wmux rewrites both files while it runs, so the script refuses (exit 4) while
  that wmux instance is open. Close wmux, apply, then start it.
- `WMUX_INSTANCE=<name>` (or `--instance`) targets wmux's side-by-side instance
  folder `%APPDATA%\\wmux-<name>`, the same way wmux itself does. `--settings-dir`
  points at any folder; the open-wmux check then runs only for `--instance`.
- macOS and Linux: wmux is Windows only. The script says so and exits 0. cmux
  on macOS keeps its own look; switchboard does not change cmux settings.

Exit codes: 0 done or nothing to do, 1 `check` found drift, 2 bad input,
3 conflicts without `--force`, 4 wmux is open, 5 a write failed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOOK_FILE = Path(__file__).with_name("wmux-look.json")
# wmux's own default sidebar width (DEFAULT_SIDEBAR_WIDTH in its App.tsx). A
# session still at the default was never dragged, so changing it is no conflict.
WMUX_DEFAULT_SIDEBAR_WIDTH = 240

EXIT_DRIFT, EXIT_INPUT, EXIT_CONFLICT, EXIT_RUNNING, EXIT_WRITE = 1, 2, 3, 4, 5


def say(msg: str) -> None:
    print(f"[wmux-look] {msg}")


def load_look(path: Path = LOOK_FILE) -> dict[str, Any]:
    look = json.loads(path.read_text(encoding="utf-8"))
    settings = look.get("settings")
    if not isinstance(settings, dict) or not all(isinstance(v, dict) for v in settings.values()):
        raise ValueError(f"{path}: 'settings' must map wmux setting blocks to objects")
    session = look.get("session", {})
    if not isinstance(session, dict) or set(session) - {"sidebarWidth"}:
        raise ValueError(f"{path}: 'session' may only hold sidebarWidth")
    return look


def instance_suffix(instance: str | None) -> str:
    name = (instance or "").strip()
    return f"-{name}" if name else ""


def default_settings_dir(instance: str | None, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / f"wmux{instance_suffix(instance)}"


def wmux_running(instance: str | None) -> bool:
    """wmux holds the named pipe \\\\.\\pipe\\wmux[-<instance>] while it runs."""
    if os.name != "nt":
        return False
    return os.path.exists(rf"\\.\pipe\wmux{instance_suffix(instance)}")


def same(a: Any, b: Any) -> bool:
    # JSON true and 1 are different settings; Python's == says they are equal.
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def plan_settings(current: dict[str, Any] | None, wanted: dict[str, dict[str, Any]]):
    """Return (merged, changes, conflicts). A conflict is a key the user set to another value."""
    merged = json.loads(json.dumps(current or {}))
    changes: list[str] = []
    conflicts: list[str] = []
    for block, values in wanted.items():
        have = merged.get(block)
        if have is not None and not isinstance(have, dict):
            # Not an object: wmux would ignore it. Only --force replaces it.
            conflicts.append(f"{block}: {json.dumps(have)} is not an object")
            have = {}
        have = dict(have or {})
        for key, value in values.items():
            if key in have and same(have[key], value):
                continue
            if key in have:
                conflicts.append(f"{block}.{key}: {json.dumps(have[key])} -> {json.dumps(value)}")
            changes.append(f"{block}.{key}={json.dumps(value)}")
            have[key] = value
        merged[block] = have
    return merged, changes, conflicts


def plan_session(current: dict[str, Any] | None, wanted: dict[str, Any]):
    """Sidebar width per saved window. No session file yet means nothing to do."""
    if current is None or "sidebarWidth" not in wanted:
        return current, [], []
    width = wanted["sidebarWidth"]
    merged = json.loads(json.dumps(current))
    changes: list[str] = []
    conflicts: list[str] = []
    for i, win in enumerate(merged.get("windows") or []):
        if not isinstance(win, dict):
            continue
        have = win.get("sidebarWidth")
        if same(have, width):
            continue
        if have is not None and not same(have, WMUX_DEFAULT_SIDEBAR_WIDTH):
            conflicts.append(f"session window {i} sidebarWidth: {json.dumps(have)} -> {width}")
        changes.append(f"session window {i} sidebarWidth={width}")
        win["sidebarWidth"] = width
    return merged, changes, conflicts


def backup(path: Path, stamp: str) -> Path:
    dest = path.with_name(f"{path.name}.switchboard-bak-{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.switchboard-bak-{stamp}-{n}")
    shutil.copy2(path, dest)
    return dest


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(args: argparse.Namespace) -> int:
    if args.settings_dir is None and sys.platform != "win32":
        say("wmux is Windows only; nothing to do here. On macOS cmux keeps its own look, and switchboard leaves cmux settings alone.")
        return 0
    try:
        look = load_look(Path(args.look))
    except (OSError, ValueError) as exc:
        say(f"cannot read the look file: {exc}")
        return EXIT_INPUT

    instance = args.instance if args.instance is not None else os.environ.get("WMUX_INSTANCE")
    settings_dir = Path(args.settings_dir) if args.settings_dir else default_settings_dir(instance)
    settings_path = settings_dir / "settings.json"
    session_path = settings_dir / "sessions" / "session.json"
    check_running = args.settings_dir is None or args.instance is not None

    try:
        cur_settings = read_json(settings_path)
        cur_session = read_json(session_path)
    except (OSError, ValueError) as exc:
        say(f"cannot read wmux's files, leaving them alone: {exc}")
        return EXIT_INPUT

    new_settings, s_changes, s_conflicts = plan_settings(cur_settings, look["settings"])
    new_session, w_changes, w_conflicts = plan_session(cur_session, look.get("session", {}))
    changes, conflicts = s_changes + w_changes, s_conflicts + w_conflicts

    say(f"wmux folder: {settings_dir}")
    if args.command == "check":
        if cur_session is None:
            say("no session file yet, so the sidebar width is not checked")
        if not changes:
            say("matches the switchboard look")
            return 0
        for c in changes:
            say(f"differs: {c}")
        return EXIT_DRIFT

    if not changes:
        say("already has the switchboard look; nothing to change")
        if cur_session is None:
            say("no session file yet: start wmux once, quit it, and rerun to set the sidebar width")
        return 0
    if conflicts and not args.force:
        say("these settings were set to something else; nothing written:")
        for c in conflicts:
            say(f"  {c}")
        say("rerun with --force to replace them (the old files are backed up first)")
        return EXIT_CONFLICT
    if check_running and wmux_running(instance):
        say("wmux is open and rewrites these files while it runs; quit wmux, then rerun")
        return EXIT_RUNNING
    for c in changes:
        say(f"{'would set' if args.dry_run else 'set'} {c}")
    if args.dry_run:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: list[Path] = []
    try:
        # Back up both files before writing either, so a failure leaves a full restore set.
        if s_changes and settings_path.exists():
            backups.append(backup(settings_path, stamp))
        if w_changes and new_session is not None:
            backups.append(backup(session_path, stamp))
        for b in backups:
            say(f"backup: {b}")
        if s_changes:
            write_json(settings_path, new_settings)
        if w_changes and new_session is not None:
            write_json(session_path, new_session)
    except OSError as exc:
        say(f"write failed: {exc}")
        for b in backups:
            say(f"restore from: {b}")
        return EXIT_WRITE
    if cur_session is None:
        say("no session file yet: start wmux once, quit it, and rerun to set the sidebar width")
    say("done; the look shows the next time wmux starts")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("apply", "check"))
    parser.add_argument("--force", action="store_true", help="replace values the user set, after a backup")
    parser.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    parser.add_argument("--settings-dir", help="wmux's settings folder (default %%APPDATA%%\\wmux[-<instance>])")
    parser.add_argument("--instance", help="wmux instance name (default WMUX_INSTANCE)")
    parser.add_argument("--look", default=str(LOOK_FILE), help=argparse.SUPPRESS)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
