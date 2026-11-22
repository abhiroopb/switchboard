#!/usr/bin/env python3
"""How often dispatched workers actually query the knowledge graph.

Measures whether workers run `graphify query` before their first rg. Read-only over what already
exists: the Amp usage telemetry logs under ~/.agents/.amp-plugin-logs/<thread>.jsonl
(one `tool_call` row per tool call with its input) and the task ledger
system/state/running-tasks.json (one row per dispatched task with its thread_id).
No new hook, no new state.

A graphify query is a `tool_call` whose input runs `graphify query` or reads
`graphify-out/graph.json` (cat, sed, python open, read_file, ...). The dispatch
prompt itself names both in `agent_start` text, which is why only `tool_call`
rows count.

    python3 system/scripts/graphify-usage-report.py --since 2026-01-15
    python3 system/scripts/graphify-usage-report.py --since 2026-01-15 --json

Prints "tasks since <date>, tasks with at least one graphify query, median
queries per task" (median over the tasks that queried at all, and over all
tasks) plus the per-task counts. Exit 0 always; a missing log for a task counts
as zero queries and is listed under "no telemetry".
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path.home() / ".agents" / ".amp-plugin-logs"
QUERY_RE = re.compile(r"graphify\s+query\b|graphify-out/graph\.json")


def repo_root() -> Path:
    probe = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
    if probe.returncode or not probe.stdout.strip():
        return Path.cwd()
    common = Path(probe.stdout.strip())
    return common.parent if common.name == ".git" else Path.cwd()


def parse_since(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def row_time(row: dict) -> datetime | None:
    for key in ("started_at", "launched_at", "created_at"):
        value = row.get(key)
        if value:
            try:
                return parse_since(value)
            except ValueError:
                continue
    return None


def load_tasks(ledger: Path, since: datetime, include_workspace_reports: bool = False) -> list[dict]:
    """Dispatched tasks (`rt-…`) started at or after `since` with a bound thread. `wr-…` rows are
    owner-tab workspace reports that never saw the dispatch prompt, so they are out by default."""
    data = json.loads(ledger.read_text())
    rows = data.get("tasks", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = list(rows.values())
    out = []
    for row in rows:
        when = row_time(row)
        if when is None or when < since or not row.get("thread_id"):
            continue
        if not include_workspace_reports and str(row.get("id", "")).startswith("wr-"):
            continue
        out.append(row)
    return sorted(out, key=lambda r: row_time(r) or since)


def count_queries(log: Path) -> int | None:
    """Graphify tool calls in one telemetry log; None when the log is missing."""
    if not log.exists():
        return None
    n = 0
    with log.open() as fh:
        for line in fh:
            if "graphify" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "tool_call":
                continue
            if QUERY_RE.search(json.dumps(event.get("input", {}), ensure_ascii=False)):
                n += 1
    return n


def report(tasks: list[dict], log_dir: Path, since: datetime) -> dict:
    per_task = []
    for row in tasks:
        n = count_queries(log_dir / f"{row['thread_id']}.jsonl")
        per_task.append({"task_id": row.get("id"), "workstream": row.get("workstream"),
                         "thread_id": row["thread_id"], "started_at": row.get("started_at") or row.get("created_at"),
                         "queries": n})
    counted = [t["queries"] or 0 for t in per_task]
    with_query = [n for n in counted if n > 0]
    return {
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tasks": len(per_task),
        "tasks_with_query": len(with_query),
        "tasks_without_telemetry": sum(1 for t in per_task if t["queries"] is None),
        "median_queries_per_querying_task": statistics.median(with_query) if with_query else 0,
        "median_queries_per_task": statistics.median(counted) if counted else 0,
        "per_task": per_task,
    }


def render(result: dict) -> str:
    lines = [
        f"tasks since {result['since'][:10]}: {result['tasks']}",
        f"tasks with at least one graphify query: {result['tasks_with_query']}",
        f"median queries per task (tasks that queried): {result['median_queries_per_querying_task']}",
        f"median queries per task (all tasks): {result['median_queries_per_task']}",
    ]
    if result["tasks_without_telemetry"]:
        lines.append(f"no telemetry log (counted as 0): {result['tasks_without_telemetry']}")
    lines.append("")
    for t in result["per_task"]:
        n = "no telemetry" if t["queries"] is None else str(t["queries"])
        lines.append(f"  {t['task_id']}  {t['workstream']}  {str(t['started_at'])[:16]}  {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True, help="ISO date or timestamp (UTC); tasks started at or after it")
    ap.add_argument("--ledger", type=Path, default=None, help="running-tasks.json (default: main checkout's)")
    ap.add_argument("--logs", type=Path, default=LOG_DIR, help="Amp usage telemetry log directory")
    ap.add_argument("--include-workspace-reports", action="store_true",
                    help="also count wr-… owner-tab report rows (not dispatched workers)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    ledger = args.ledger or repo_root() / "system" / "state" / "running-tasks.json"
    since = parse_since(args.since)
    result = report(load_tasks(ledger, since, args.include_workspace_reports), args.logs, since)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
