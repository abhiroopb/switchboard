#!/usr/bin/env python3
"""Register the switchboard checkout as its own Obsidian vault named after its folder (switchboard).

Every review pointer switchboard writes is obsidian://open?vault=switchboard&file=<path>, so
the vault must exist under that name. Obsidian keeps its vault list in
obsidian.json and rewrites that file while it runs, so an edit made while it is
open is lost. This script:

  1. reads obsidian.json (Windows %APPDATA%/obsidian, macOS ~/Library/Application
     Support/obsidian, Linux ~/.config/obsidian);
  2. exits 0 when a vault already points at the repo root;
  3. otherwise closes Obsidian gracefully when it is running (never a force kill;
     it gives up with exit 3 when the app does not quit within --timeout), adds
     one vault entry, keeps every other vault exactly as it was (a timestamped
     backup sits next to the file), and starts Obsidian again on HOME.md when it
     was running before (or with --open).

The vault name is the folder name, so the checkout folder must be called switchboard.
Shared vault settings (.obsidian/app.json and friends) are tracked in git.

Usage:
  python3 system/scripts/register-obsidian-vault.py            # register if needed
  python3 system/scripts/register-obsidian-vault.py --check    # report only; exit 1 when missing
  python3 system/scripts/register-obsidian-vault.py --dry-run  # say what would change
  python3 system/scripts/register-obsidian-vault.py --open     # also open HOME.md in the vault

Prints one JSON line: {"status": "registered"|"present"|"missing"|..., ...}.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    """The main checkout (the vault), also when run from a task worktree: the
    parent of the shared git dir that `git rev-parse` reports for the cwd."""
    common = subprocess.check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip()
    if Path(common).name == ".git":
        return Path(common).parent.resolve()
    out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
    return Path(out.strip()).resolve()


def config_path() -> Path:
    if os.environ.get("SWITCHBOARD_OBSIDIAN_CONFIG"):
        return Path(os.environ["SWITCHBOARD_OBSIDIAN_CONFIG"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "obsidian" / "obsidian.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "obsidian" / "obsidian.json"


def norm(p: str | Path) -> str:
    s = str(Path(p).resolve()) if Path(p).exists() else str(p)
    s = s.replace("\\", "/").rstrip("/")
    return s.lower() if os.name == "nt" else s


def find_vault(data: dict, root: Path) -> tuple[str, dict] | None:
    for vid, v in (data.get("vaults") or {}).items():
        if norm(v.get("path", "")) == norm(root):
            return vid, v
    return None


def obsidian_running() -> bool:
    if os.environ.get("SWITCHBOARD_OBSIDIAN_ASSUME_CLOSED") == "1":
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe", "/NH"], capture_output=True, text=True, timeout=15).stdout
            return "obsidian.exe" in out.lower()
        if sys.platform == "darwin":
            return subprocess.run(["pgrep", "-x", "Obsidian"], capture_output=True).returncode == 0
        # Match the process name only: -f would match this script's own command line.
        return subprocess.run(["pgrep", "-ix", "obsidian"], capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def close_obsidian(timeout: float) -> bool:
    """Ask Obsidian to quit (window close, never /F or SIGKILL); True once it is gone."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", "Obsidian.exe"], capture_output=True, timeout=15)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'quit app "Obsidian"'], capture_output=True, timeout=15)
        else:
            subprocess.run(["pkill", "-TERM", "-ix", "obsidian"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not obsidian_running():
            time.sleep(1.0)  # let it finish its last obsidian.json write
            return True
        time.sleep(0.5)
    return False


def open_uri(uri: str) -> None:
    if os.name == "nt":
        os.startfile(uri)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])  # focus-ok: one-time vault registration at install, Obsidian must open
    else:
        subprocess.Popen(["xdg-open", uri])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report only; exit 1 when the vault is missing")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    ap.add_argument("--open", action="store_true", help="open HOME.md in the vault afterwards")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for Obsidian to quit")
    args = ap.parse_args()

    root = repo_root()
    cfg = config_path()
    name = root.name
    result: dict = {"vault": name, "path": str(root), "config": str(cfg)}
    if name != "switchboard":
        result["warning"] = f"checkout folder is {name!r}; review links use vault=switchboard, so rename the folder to switchboard"

    data: dict = {"vaults": {}}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            result.update(status="unreadable", error=f"{cfg}: {exc}")
            print(json.dumps(result))
            return 2
    data.setdefault("vaults", {})

    found = find_vault(data, root)
    if found:
        result.update(status="present", vault_id=found[0])
        if args.open and not args.check and not args.dry_run:
            open_uri(f"obsidian://open?vault={name}&file=HOME")
        print(json.dumps(result))
        return 0
    if args.check:
        result.update(status="missing", fix="python3 system/scripts/register-obsidian-vault.py")
        print(json.dumps(result))
        return 1
    if not cfg.parent.exists() and not args.dry_run:
        # Obsidian was never started: create the config dir; Obsidian reads it on first launch.
        cfg.parent.mkdir(parents=True, exist_ok=True)

    was_running = obsidian_running()
    if args.dry_run:
        result.update(status="would-register", obsidian_running=was_running,
                      would_close_and_reopen_obsidian=was_running, other_vaults=len(data["vaults"]))
        print(json.dumps(result))
        return 0
    if was_running and not close_obsidian(args.timeout):
        result.update(status="obsidian-busy", error="Obsidian did not quit; close it and rerun this script")
        print(json.dumps(result))
        return 3

    # Re-read after Obsidian quit: it may have written the file on exit.
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
        data.setdefault("vaults", {})
        backup = cfg.with_name(f"obsidian.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(cfg, backup)
        result["backup"] = str(backup)
    if not find_vault(data, root):
        vid = secrets.token_hex(8)
        while vid in data["vaults"]:
            vid = secrets.token_hex(8)
        data["vaults"][vid] = {"path": str(root), "ts": int(time.time() * 1000)}
        tmp = cfg.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, cfg)
        result["vault_id"] = vid
    result.update(status="registered", other_vaults_kept=len(data["vaults"]) - 1, reopened=False)
    if was_running or args.open:
        open_uri(f"obsidian://open?vault={name}&file=HOME")
        result["reopened"] = True
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
