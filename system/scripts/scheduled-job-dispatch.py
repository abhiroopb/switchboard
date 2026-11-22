#!/usr/bin/env python3
"""Dispatch ``dispatch: router`` jobs from ``system/scheduled-jobs.yaml`` as workers.

Most scheduled jobs run inside the Scheduled Jobs tab. A job that sets
``dispatch: router`` instead runs as one worker in its ``workstream:`` through
``router-dispatch.sh`` (worktree, branch, report to CoS). The command-center
loop calls ``tick`` every 10 minutes, so the job fires at its ``not_before``
slot with no launchd job or cron of its own. The Scheduled Jobs routine and
CoS call ``run <job>`` when they see the job due; both paths are idempotent.

Usage:
  scheduled-job-dispatch.py due [--now ISO] [--job NAME] [--json]
  scheduled-job-dispatch.py run NAME [--now ISO] [--dry-run] [--force]
  scheduled-job-dispatch.py tick [--now ISO] [--dry-run]

``--now`` takes a Pacific Time timestamp (``2026-01-15T15:05``) for dry runs.
``due`` and every ``--dry-run`` never dispatch and never write state.

Idempotence: each dispatch carries ``--request-id <job>:<due date>``, so
``dispatch.py`` returns the live task instead of a second worker. A routed,
queued, duplicate, or already-live result stamps ``system/job-state.json``
with the PT date. A failed dispatch is retried on later ticks, at most
``MAX_ATTEMPTS`` times per due date; attempts log to
``system/state/logs/scheduled-job-dispatch.jsonl``.

Kill switch: ``SCHEDULED_JOB_DISPATCH_DISABLED=1`` makes ``tick`` a no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduled_jobs  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

MAX_ATTEMPTS = 3
OK_STATUSES = {"routed", "queued", "duplicate", "already-live", "delivered"}


def repo_root() -> Path:
    env = os.environ.get("SCHEDULED_JOB_DISPATCH_REPO_ROOT")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def load_jobs(root: Path) -> list[dict[str, Any]]:
    if yaml is None:
        raise SystemExit("PyYAML is not installed for this python3")
    data = yaml.safe_load((root / "system" / "scheduled-jobs.yaml").read_text()) or {}
    return [j for j in data.get("jobs", []) if isinstance(j, dict) and j.get("name")]


def job_state_path(root: Path) -> Path:
    return root / "system" / "job-state.json"


def load_state(root: Path) -> dict[str, Any]:
    try:
        return json.loads(job_state_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def stamp(root: Path, name: str, day: str) -> None:
    path = job_state_path(root)
    state = load_state(root)
    state[name] = day
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def log_path(root: Path) -> Path:
    return root / "system" / "state" / "logs" / "scheduled-job-dispatch.jsonl"


def log_event(root: Path, event: dict[str, Any]) -> None:
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def failed_attempts(root: Path, name: str, due_date: str) -> int:
    try:
        lines = log_path(root).read_text().splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("job") == name and row.get("due_date") == due_date and row.get("result") == "failed":
            count += 1
    return count


def parse_now(value: str | None) -> datetime:
    if not value:
        return scheduled_jobs.now_pt()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None and scheduled_jobs.PT is not None:
        parsed = parsed.replace(tzinfo=scheduled_jobs.PT)
    return parsed.astimezone(scheduled_jobs.PT) if scheduled_jobs.PT is not None else parsed


def render_prompt(job: dict[str, Any], due_date: str, last_run: str | None) -> str:
    body = str(job.get("command") or "").strip()
    body = body.replace("{due_date}", due_date).replace("{last_run}", last_run or "never")
    header = f"Workstream: {job['workstream']}"
    return body if body.startswith("Workstream:") else f"{header}\n{body}"


def dispatch_command(root: Path, job: dict[str, Any], due_date: str, last_run: str | None) -> list[str]:
    return [
        "bash", str(root / "system" / "scripts" / "router-dispatch.sh"),
        "--origin", str(job.get("origin") or "cos"),
        "--workstream", str(job["workstream"]),
        "--request-id", f"{job['name']}:{due_date}",
        "--delivery", str(job.get("delivery") or "digest"),
        "--parallel-worker",
        render_prompt(job, due_date, last_run),
    ]


def router_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [j for j in jobs if j.get("dispatch") == "router" and j.get("workstream")]


def describe(job: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    last = state.get(job["name"])
    verdict = scheduled_jobs.evaluate(job, last, now)
    nxt = scheduled_jobs.next_fire(job, last, now)
    return {
        "name": job["name"],
        "schedule": job.get("schedule"),
        "not_before": job.get("not_before"),
        "dispatch": job.get("dispatch", "routine"),
        "workstream": job.get("workstream"),
        "last_run": last,
        "now_pt": now.strftime("%Y-%m-%d %H:%M %Z"),
        **verdict,
        "next_fire_pt": nxt.strftime("%a %Y-%m-%d %H:%M %Z") if nxt else None,
    }


def run_job(root: Path, job: dict[str, Any], now: datetime, dry_run: bool, force: bool) -> dict[str, Any]:
    state = load_state(root)
    last = state.get(job["name"])
    verdict = scheduled_jobs.evaluate(job, last, now)
    result: dict[str, Any] = {"job": job["name"], **verdict}
    if not verdict["due"] and not force:
        result["result"] = "not-due"
        return result
    due_date = verdict["due_date"] or now.date().isoformat()
    if job.get("dispatch") != "router" or not job.get("workstream"):
        result["result"] = "not-a-router-job"
        return result
    if not force and failed_attempts(root, job["name"], due_date) >= MAX_ATTEMPTS:
        result["result"] = "gave-up"
        result["reason"] = f"{MAX_ATTEMPTS} failed dispatches for {due_date}; see {log_path(root)}"
        return result
    cmd = dispatch_command(root, job, due_date, last)
    if dry_run:
        result["result"] = "dry-run"
        result["command"] = cmd[:-1] + ["<prompt>"]
        result["prompt"] = cmd[-1]
        return result
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=300)
    payload: dict[str, Any] = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    status = str(payload.get("status") or "")
    ok = proc.returncode == 0 and (status in OK_STATUSES or payload.get("deduplicated") is True)
    today = now.date().isoformat()
    if ok:
        stamp(root, job["name"], today)
    result.update({
        "result": "dispatched" if ok else "failed",
        "status": status or None,
        "task_id": payload.get("task_id"),
        "message": payload.get("message") or (proc.stderr.strip()[-300:] if not ok else None),
    })
    log_event(root, {"at": now.isoformat(), "job": job["name"], "due_date": due_date, "result": result["result"],
                     "status": result["status"], "task_id": result["task_id"], "rc": proc.returncode})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch dispatch: router scheduled jobs as workers.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("due", help="dry-run due check; never dispatches")
    d.add_argument("--now")
    d.add_argument("--job")
    d.add_argument("--json", action="store_true")
    r = sub.add_parser("run", help="dispatch one job if it is due")
    r.add_argument("name")
    r.add_argument("--now")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--force", action="store_true", help="dispatch even when not due (explicit ask only)")
    t = sub.add_parser("tick", help="dispatch every due router job (command-center loop)")
    t.add_argument("--now")
    t.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = repo_root()
    jobs = load_jobs(root)
    now = parse_now(getattr(args, "now", None))

    if args.cmd == "due":
        state = load_state(root)
        selected = [j for j in jobs if not args.job or j["name"] == args.job]
        if args.job and not selected:
            print(f"no job named {args.job}", file=sys.stderr)
            return 2
        rows = [describe(j, state, now) for j in selected]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                mark = "DUE" if row["due"] else "not due"
                print(f"{row['name']}: {mark} ({row['reason']}); last run {row['last_run'] or 'never'}; "
                      f"next fire {row['next_fire_pt'] or 'none in 21 days'}")
        return 0

    if args.cmd == "run":
        job = next((j for j in jobs if j["name"] == args.name), None)
        if job is None:
            print(f"no job named {args.name}", file=sys.stderr)
            return 2
        print(json.dumps(run_job(root, job, now, args.dry_run, args.force), indent=2))
        return 0

    if os.environ.get("SCHEDULED_JOB_DISPATCH_DISABLED") == "1":
        return 0
    results = [run_job(root, job, now, args.dry_run, False) for job in router_jobs(jobs)]
    acted = [r for r in results if r["result"] != "not-due"]
    if acted or args.dry_run:
        print(json.dumps(acted if not args.dry_run else results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
