#!/usr/bin/env python3
"""Enforce local retention for scratch state in pm-os and the home directory.

Rules:

pm-os
  workstreams/*/.skill-data/pre-*.sqlite*         > 14 days  delete
  workstreams/*/.skill-data/reload-bridge/<id>/   > 14 days  delete
  system/state/logs/emit-*.out                    > 30 days  delete
  .state/<dir rows in system/config/state-pruned.txt, today *-demo/>  > 30 days  delete
  untracked files (git ls-files --others)         > 200      warn (exit 2)

home
  ~/.cache/amp/logs/threads/<thread>/             > 14 days  delete
  ~/.agents/.amp-plugin-logs/T-*.jsonl            > 14 days  delete
  ~/.cmuxterm/agent-turn-diff-baseline-snapshots*/<id>/ > 14 days  delete
  ~/.cmuxterm/events.jsonl.<n> (rotated)          > 14 days  delete
  ~/.codex/sessions/**/*.jsonl                    > 60 days  delete

package caches (monthly, tracked in system/state/local-retention.json)
  npm cache clean --force; pnpm store prune; uv cache prune; bun pm cache rm
  Each command gets PACKAGE_CACHE_COMMAND_TIMEOUT seconds and the run is
  recorded even when one times out, so the weekly background task (300 s
  budget) never loops on it. `uv cache prune` waits on ~/.cache/uv/.lock,
  which every long-lived `uvx` MCP server holds while Amp runs, so it
  reports "timeout" on a normal workday.
  end-of-day-close.sh runs `uv cache prune` once cmux has quit (no uvx server
  holds the lock then); this script only measures.

uv cache size guard (every run, tracked in the same state file)
  ~/.cache/uv is measured with `du -sk` and recorded as uv_cache_bytes. Above
  UV_CACHE_ALERT_BYTES (40 GB) one CoS line goes out, at most once every
  UV_CACHE_ALERT_REPEAT_DAYS, through report-to-cos.sh exactly as
  check-state-inventory.py sends its shrink alert. Growth past the limit is
  disk pressure only: nothing breaks until the disk fills.

amp-mem backups are already rotated by `amp-mem backup` (7 days) and
`rotate-logs.sh` (14-day backstop); they are deliberately not touched here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path


HOME = Path(os.environ.get("HOME", "{{HOME}}"))


def _default_root() -> Path:
    """PM_OS_ROOT, else the git checkout around cwd, else ~/Development/pm-os.

    Scheduled runs start in the repo root without --root, and the install
    folder is the owner's choice (a synced folder on Windows), so the home
    default alone would prune the wrong tree or nothing.
    """
    env = os.environ.get("PM_OS_ROOT")
    if env:
        return Path(env).expanduser()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return HOME / "Development/pm-os"


DEFAULT_ROOT = _default_root()
# What we delete under .state is declared, with every other scheduled pruner's
# paths, in system/config/state-pruned.txt; check-state-inventory.py skips those
# paths in its .state count. Our row(s) there are the only place the pattern is named.
STATE_PRUNED_FILE = Path(__file__).resolve().parents[1] / "config" / "state-pruned.txt"
DAY = 86_400
UNTRACKED_LIMIT = 200
PACKAGE_CACHE_INTERVAL_DAYS = 30
# Four commands must fit inside the background task's 300 s budget with room
# for the rest of the chain.
PACKAGE_CACHE_COMMAND_TIMEOUT = 45
PACKAGE_CACHE_COMMANDS = (
    ("npm", ["npm", "cache", "clean", "--force"]),
    ("pnpm", ["pnpm", "store", "prune"]),
    ("uv", ["uv", "cache", "prune"]),
    ("bun", ["bun", "pm", "cache", "rm"]),
)
UV_CACHE_DIR = Path(os.environ.get("UV_CACHE_DIR") or HOME / ".cache/uv")
UV_CACHE_ALERT_BYTES = 40 * 1024**3
UV_CACHE_ALERT_REPEAT_DAYS = 7
# `du -sk` over a large cache can take many seconds; the weekly task
# has a 300 s budget shared with four other scripts.
UV_CACHE_MEASURE_TIMEOUT = 120
REPORT_TO_COS = Path(__file__).resolve().parent / "report-to-cos.sh"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--home", type=Path, default=HOME)
    parser.add_argument("--now", type=float, default=time.time())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--skip-package-caches",
        action="store_true",
        help="Do not run the monthly npm/pnpm/uv/bun cache prune.",
    )
    parser.add_argument(
        "--skip-untracked-check",
        action="store_true",
        help="Do not count untracked files in --root.",
    )
    return parser.parse_args()


def newest_mtime(path: Path) -> float:
    """Latest mtime under a directory tree (so an active dir is never expired)."""
    latest = path.stat().st_mtime
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except FileNotFoundError:
                continue
    return latest


def expired(paths: list[Path], cutoff: float) -> list[Path]:
    return sorted(p for p in paths if p.exists() and newest_mtime(p) < cutoff)


def declared_state_dirs(declaration: Path = STATE_PRUNED_FILE) -> list[str]:
    """Dir globs (relative to .state, trailing slash dropped) this script may prune.

    Read from the shared declaration so the path is named once. No row means no
    deletion under .state: the caller stops with an error instead of guessing.
    """
    try:
        lines = declaration.read_text().splitlines()
    except OSError as exc:
        raise SystemExit(f"prune-local-retention: cannot read {declaration}: {exc}") from exc
    rows = [line.split() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    dirs = [cols[1].rstrip("/") for cols in rows
            if len(cols) >= 2 and cols[0] == "prune-local-retention.py" and cols[1].endswith("/")]
    if not dirs:
        raise SystemExit(f"prune-local-retention: {declaration} lists no dir row for prune-local-retention.py; "
                         "declare the .state path there before pruning it")
    return dirs


def pm_os_candidates(root: Path, now: float) -> dict[str, list[Path]]:
    skill_data = root.glob("workstreams/*/.skill-data")
    snapshots: list[Path] = []
    for directory in skill_data:
        snapshots.extend(p for p in directory.glob("pre-*.sqlite*") if p.is_file())
        bridge = directory / "reload-bridge"
        if bridge.is_dir():
            snapshots.extend(p for p in bridge.iterdir() if p.is_dir())
    emit_logs = [p for p in (root / "system/state/logs").glob("emit-*.out") if p.is_file()]
    demos = [p for pattern in declared_state_dirs()
             for p in (root / ".state").glob(pattern) if p.is_dir()]
    return {
        "skill_data_snapshots": expired(snapshots, now - 14 * DAY),
        "emit_logs": expired(emit_logs, now - 30 * DAY),
        "state_demos": expired(demos, now - 30 * DAY),
    }


def home_candidates(home: Path, now: float) -> dict[str, list[Path]]:
    amp_threads = [p for p in (home / ".cache/amp/logs/threads").glob("*") if p.is_dir()]
    cmux = []
    for base in home.glob(".cmuxterm/agent-turn-diff-baseline-snapshots*"):
        cmux.extend(p for p in base.iterdir() if p.is_dir())
    codex = [p for p in (home / ".codex/sessions").rglob("*.jsonl") if p.is_file()]
    plugin_logs = [
        p for p in (home / ".agents/.amp-plugin-logs").glob("T-*.jsonl") if p.is_file()
    ]
    cmux_rotated = [
        p for p in (home / ".cmuxterm").glob("events.jsonl.[0-9]*") if p.is_file()
    ]
    return {
        "amp_thread_logs": expired(amp_threads, now - 14 * DAY),
        "amp_plugin_thread_logs": expired(plugin_logs, now - 14 * DAY),
        "cmux_diff_snapshots": expired(cmux, now - 14 * DAY),
        "cmux_rotated_events": expired(cmux_rotated, now - 14 * DAY),
        "codex_sessions": expired(codex, now - 60 * DAY),
    }


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def untracked_count(root: Path) -> int | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def prune_package_caches(
    state_file: Path,
    now: float,
    apply: bool,
    commands: tuple[tuple[str, list[str]], ...] = PACKAGE_CACHE_COMMANDS,
    command_timeout: float = PACKAGE_CACHE_COMMAND_TIMEOUT,
) -> dict[str, object]:
    state: dict[str, object] = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            state = {}
    last_run = float(state.get("package_caches_last_run", 0) or 0)
    due = now - last_run >= PACKAGE_CACHE_INTERVAL_DAYS * DAY
    results: dict[str, str] = {}
    if due and apply:
        for name, command in commands:
            if shutil.which(command[0]) is None:
                results[name] = "missing"
                continue
            try:
                with tempfile.TemporaryDirectory() as scratch:
                    # `bun pm cache rm` refuses to run outside a project
                    # ("No package.json was found"); every command gets an
                    # empty throwaway project as its cwd.
                    (Path(scratch) / "package.json").write_text('{"name": "pm-os-cache-prune"}\n')
                    proc = subprocess.run(
                        command, capture_output=True, text=True, timeout=command_timeout, cwd=scratch
                    )
            except subprocess.TimeoutExpired:
                # A cache another process holds (uv's lock under a running
                # uvx server) is tried again next month, never this tick.
                results[name] = "timeout"
                continue
            results[name] = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        state["package_caches_last_run"] = now
        state["package_caches_last_results"] = results
        state["package_caches_last_run_date"] = date.fromtimestamp(now).isoformat()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return {"due": due, "ran": bool(due and apply), "results": results}


def measure_uv_cache(cache_dir: Path = UV_CACHE_DIR, timeout: float = UV_CACHE_MEASURE_TIMEOUT) -> int | None:
    """Bytes under the uv cache via `du -sk`; None when absent or unmeasurable."""
    if not cache_dir.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["du", "-sk", str(cache_dir)], capture_output=True, text=True, timeout=timeout
        )
        return int(proc.stdout.split()[0]) * 1024
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        return None


def send_cos_line(text: str, now: float) -> bool:
    """One line to CoS through report-to-cos.sh, the path check-state-inventory.py uses."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(now))
    cmd = ["bash", str(REPORT_TO_COS), "--task-id", f"uv-cache-size-{stamp}", "--origin", "cos",
           "--workstream", "pm-os-maintenance", "--status", "blocked", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        last = (result.stdout.strip().splitlines() or [""])[-1]
        return bool(json.loads(last).get("delivered"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def guard_uv_cache(
    state_file: Path,
    now: float,
    apply: bool,
    size_bytes: int | None,
    limit_bytes: int = UV_CACHE_ALERT_BYTES,
    send=send_cos_line,
) -> dict[str, object]:
    """Record the uv cache size; above the limit send one CoS line, at most weekly.
    The state file is only written under --apply; the report is always built."""
    state: dict[str, object] = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            state = {}
    over = size_bytes is not None and size_bytes > limit_bytes
    last_alert = float(state.get("uv_cache_alert_last_sent", 0) or 0)
    due = over and now - last_alert >= UV_CACHE_ALERT_REPEAT_DAYS * DAY
    report: dict[str, object] = {
        "bytes": size_bytes,
        "gb": round(size_bytes / 1024**3, 1) if size_bytes is not None else None,
        "limit_gb": round(limit_bytes / 1024**3, 1),
        "over_limit": over,
        "alert_sent": False,
    }
    if size_bytes is not None:
        state["uv_cache_bytes"] = size_bytes
        state["uv_cache_measured_at"] = date.fromtimestamp(now).isoformat()
    if due and apply:
        text = (f"uv cache at {report['gb']} GB (limit {report['limit_gb']} GB, {UV_CACHE_DIR}). "
                "Disk pressure only; nothing breaks until the disk fills. "
                "Run `uv cache prune` with cmux quit (end-of-day-close.sh does this), "
                "or `uv cache clean` to drop it all.")
        report["alert_sent"] = bool(send(text, now))
        state["uv_cache_alert_last_sent"] = now
        state["uv_cache_alert_last_delivered"] = report["alert_sent"]
    if apply and size_bytes is not None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    home = args.home.resolve()
    groups = pm_os_candidates(root, args.now)
    groups.update(home_candidates(home, args.now))

    removed: dict[str, list[str]] = {}
    for name, paths in groups.items():
        removed[name] = []
        for path in paths:
            if args.apply:
                remove(path)
            removed[name].append(str(path))

    report: dict[str, object] = {
        "applied": args.apply,
        "expired": removed,
        "expired_count": sum(len(v) for v in removed.values()),
    }

    status = 0
    if not args.skip_untracked_check:
        count = untracked_count(root)
        report["untracked_count"] = count
        report["untracked_limit"] = UNTRACKED_LIMIT
        if count is not None and count > UNTRACKED_LIMIT:
            report["warning"] = (
                f"{count} untracked files in {root} exceed {UNTRACKED_LIMIT}; "
                "triage with git add or .gitignore"
            )
            status = 2

    state_file = root / "system/state/local-retention.json"
    if not args.skip_package_caches:
        report["package_caches"] = prune_package_caches(state_file, args.now, args.apply)

    cache_dir = UV_CACHE_DIR if os.environ.get("UV_CACHE_DIR") else home / ".cache/uv"
    report["uv_cache"] = guard_uv_cache(state_file, args.now, args.apply, measure_uv_cache(cache_dir))

    print(json.dumps(report, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
