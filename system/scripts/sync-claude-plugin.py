#!/usr/bin/env python3
"""Keep the installed pm-os Claude Code plugin in step with the repo.

    python3 system/scripts/sync-claude-plugin.py            # copy what changed, print one JSON line
    python3 system/scripts/sync-claude-plugin.py --check    # exit 1 while the installed copy is behind

Why: Claude Code runs the pm-os plugin from its own copy,
~/.claude/plugins/cache/pm-os-local/pm-os/<version>, made at install time. The version
never changes (0.1.0), so `claude plugin update` has nothing to fetch, and every hook fix
in system/claude-marketplace/plugins/pm-os after the install would stay in the repo,
so a new hook such as the CoS autobind hook would never run.

What it does:
- Copies every file of the repo's plugin folder (hooks, commands, agents, manifest)
  that differs into each installed pm-os copy. __pycache__ and .pyc are skipped.
  Files only in the installed copy are listed, never deleted. An installed file edited
  after the repo copy (a hand fix whose branch has not landed) is held, not overwritten,
  until the repo file changes again.
- hooks/hooks.json is compared as JSON. Where the Windows hook profile applies
  (system/windows/apply-hook-profile.py: wmux's own Claude hooks in settings.json),
  the expected copy is the repo's without the heartbeat hooks, so the refresh never
  undoes the profile. On Windows it then runs that profile, which is idempotent.
- Skips while main has a rebase in flight, so a half-applied tree is never copied.
- New and resumed Claude sessions load the refreshed hooks; a running session keeps
  the hooks it started with.

Repo root: git rev-parse --show-toplevel in the working directory (--repo overrides).
Env: CLAUDE_CONFIG_DIR (default ~/.claude). The command-center loop runs it each tick;
kill switch PLUGIN_SYNC_DISABLED=1. Tests: system/scripts/test_sync_claude_plugin.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN = "pm-os@pm-os-local"
PLUGIN_REL = Path("system") / "claude-marketplace" / "plugins" / "pm-os"
HOOKS_JSON = Path("hooks") / "hooks.json"
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def git_toplevel(cwd: Path) -> Path | None:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=False)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def rebase_in_flight(repo: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        result = subprocess.run(["git", "rev-parse", "--git-path", name], cwd=repo, capture_output=True, text=True, check=False)
        path = Path(result.stdout.strip())
        if result.returncode == 0 and (path if path.is_absolute() else repo / path).exists():
            return True
    return False


def install_paths(base: Path) -> list[Path]:
    try:
        installed = json.loads((base / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    paths = []
    for entry in installed.get("plugins", {}).get(PLUGIN, []) or []:
        path = Path(str(entry.get("installPath") or ""))
        if str(path) and path.is_dir():
            paths.append(path)
    return paths


def load_profile(repo: Path) -> Any | None:
    """system/windows/apply-hook-profile.py as a module, or None."""
    script = repo / "system" / "windows" / "apply-hook-profile.py"
    if not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location("apply_hook_profile", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def profile_applies(profile: Any | None, base: Path) -> bool:
    if profile is None or (os.name != "nt" and not os.environ.get("PM_OS_HOOK_PROFILE_ANY_OS")):
        return False
    try:
        settings = json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(profile.wmux_hooks_installed(settings))


def source_files(source: Path) -> list[Path]:
    return sorted(p.relative_to(source) for p in source.rglob("*")
                  if p.is_file() and not (set(p.relative_to(source).parts) & SKIP_PARTS) and p.suffix != ".pyc")


def expected_bytes(source: Path, rel: Path, strip_heartbeat: bool, profile: Any | None) -> bytes:
    data = (source / rel).read_bytes()
    if rel == HOOKS_JSON and strip_heartbeat and profile is not None:
        hooks = profile.without_heartbeat(json.loads(data.decode("utf-8")))
        return (json.dumps(hooks, indent=2) + "\n").encode("utf-8")
    return data


def same(rel: Path, installed: Path, want: bytes) -> bool:
    try:
        have = installed.read_bytes()
    except OSError:
        return False
    if rel == HOOKS_JSON:
        try:
            return json.loads(have.decode("utf-8")) == json.loads(want.decode("utf-8"))
        except ValueError:
            return False
    # Line endings are not a change: a Windows checkout may hold CRLF where the copy has LF.
    return have.replace(b"\r\n", b"\n") == want.replace(b"\r\n", b"\n")


def newer(installed: Path, source: Path) -> bool:
    try:
        return installed.stat().st_mtime > source.stat().st_mtime
    except OSError:
        return False


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sync(repo: Path, base: Path, check: bool = False) -> dict[str, Any]:
    source = repo / PLUGIN_REL
    if not source.is_dir():
        return {"status": "no-source", "source": str(source)}
    targets = install_paths(base)
    if not targets:
        return {"status": "not-installed", "plugin": PLUGIN}
    if rebase_in_flight(repo):
        return {"status": "skipped", "reason": "rebase in flight on main"}
    profile = load_profile(repo)
    strip = profile_applies(profile, base)
    files = source_files(source)
    out: dict[str, Any] = {"status": "ok", "changed": [], "held": [], "extra": [], "targets": [str(t) for t in targets],
                           "heartbeat_stripped": strip}
    for target in targets:
        for rel in files:
            want = expected_bytes(source, rel, strip, profile)
            if same(rel, target / rel, want):
                continue
            if rel != HOOKS_JSON and newer(target / rel, source / rel):   # the hook profile rewrites hooks.json in place
                # Edited in place after the repo copy (a hand fix whose branch has not landed):
                # held until the repo file changes again.
                out["held"].append(rel.as_posix())
                continue
            out["changed"].append(rel.as_posix())
            if not check:
                write_atomic(target / rel, want)
        known = {r.as_posix() for r in files}
        out["extra"] += sorted(p.relative_to(target).as_posix() for p in target.rglob("*")
                               if p.is_file() and not (set(p.relative_to(target).parts) & SKIP_PARTS)
                               and p.suffix != ".pyc" and p.relative_to(target).as_posix() not in known)
    if check:
        out["status"] = "behind" if out["changed"] else "current"
        return out
    if out["changed"] and strip and profile is not None:
        out["profile"] = profile.apply(base, repo)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="report only; exit 1 while the installed copy is behind")
    ap.add_argument("--repo", help="repo root (default: git toplevel of the working directory)")
    args = ap.parse_args(argv)
    repo = Path(args.repo) if args.repo else git_toplevel(Path.cwd())
    if repo is None:
        print(json.dumps({"status": "no-repo"}))
        return 1
    result = sync(repo, claude_dir(), check=args.check)
    print(json.dumps(result))
    return 1 if args.check and result.get("status") == "behind" else 0


if __name__ == "__main__":
    sys.exit(main())
