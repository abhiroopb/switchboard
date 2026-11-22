#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_paths import main_checkout  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark one or more scheduled jobs as run today.")
    parser.add_argument("job_names", nargs="+", help="Scheduled job names to mark as run")
    parser.add_argument(
        "--job-state-path",
        default="system/job-state.json",
        help="Path to job-state.json relative to the main checkout, or absolute",
    )
    args = parser.parse_args()

    # job-state.json is gitignored shared state read by main's start-day.sh, so a
    # stamp from a task worktree goes to the main checkout. A stamp in a worktree
    # copy would leave start-day still reporting the job as due.
    repo_root = main_checkout(Path(__file__).resolve().parents[2])
    job_state_path = Path(args.job_state_path)
    if not job_state_path.is_absolute():
        job_state_path = repo_root / job_state_path

    if job_state_path.exists():
        try:
            payload = json.loads(job_state_path.read_text())
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    for job_name in args.job_names:
        payload[job_name] = today

    job_state_path.parent.mkdir(parents=True, exist_ok=True)
    job_state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
