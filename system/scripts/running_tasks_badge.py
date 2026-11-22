#!/usr/bin/env python3
"""Decide in one process whether the running-tasks badge needs publishing (Windows).

    python3 system/scripts/running_tasks_badge.py unchanged

    python3 system/scripts/running_tasks_badge.py record

Exit 0 from `unchanged` means the counts match the last publish and that publish is under REFRESH_SECONDS old, so
publish-running-tasks-status.sh can stop. Exit 1 means it should publish. The script calls `record` only when the
publish reached every surface, so a missing tab or a failed send retries next tick.

Why: the shell path starts about 80 processes per tick (python for each field, a shim
call for each surface) to set a badge that rarely changes, and a kernel leak on Windows keeps every exited
process until reboot. The scripts run in-process through their own main(), so the counts match the shell path's.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

REFRESH_SECONDS = 1800
REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
STATE_DIR = Path(os.environ.get("STATE_DIR") or REPO_ROOT / "system" / "state")
SCRIPTS = Path(__file__).resolve().parent


def run_script(name: str, argv: list[str]) -> str:
    """A sibling script's stdout, run through its own main() in this process."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    out = io.StringIO()
    saved = sys.argv
    sys.argv = [name, *argv]
    try:
        with contextlib.redirect_stdout(out):
            code = module.main()
    finally:
        sys.argv = saved
    if code:
        raise RuntimeError(f"{name} exited {code}")
    return out.getvalue()


def current_counts() -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {}
    for surface in run_script("surface-registry.py", ["list", "--where", "command_center_badge"]).split():
        summary = json.loads(run_script("running-task-state.py", ["summary", "--origin", surface]))
        counts[surface] = [int(summary["active_count"]), int(summary.get("pending_report_count", 0))]
    return counts


def unchanged(state_file: Path, counts: dict[str, list[int]], now: float) -> bool:
    try:
        previous = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return previous.get("counts") == counts and now - float(previous.get("at", 0)) < REFRESH_SECONDS


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["unchanged"], ["record"]):
        print(__doc__)
        return 2
    state_file = STATE_DIR / "running-tasks-badge.json"
    try:
        counts = current_counts()
    except Exception as error:  # noqa: BLE001 - any failure means the shell path should run
        print(f"running_tasks_badge: {error}", file=sys.stderr)
        return 1
    now = time.time()
    if args == ["unchanged"]:
        return 0 if unchanged(state_file, counts, now) else 1
    tmp = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"counts": counts, "at": now}), encoding="utf-8")
        os.replace(tmp, state_file)
    except OSError:
        tmp.unlink(missing_ok=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
