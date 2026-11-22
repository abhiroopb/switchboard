#!/usr/bin/env python3
"""Which undelivered terminal reports have nothing left to say.

A blocked or failed report asks the owner to land or discard a checkout. Once a continuation
owns that work, or the row's ask is marked handled in cos-review-actions.json, the report is
moot: retrying it writes one options_missing event per row per tick, and a CoS that
reopened would take every stale line at once.

The command-center retry scan and report-to-cos.sh both ask this module, so neither a loop pass
nor a detached waiter types a moot report. A done report always goes through: it carries the
result, and a continuation of a done row is a follow-up, not a replacement.

  report_settlement.py check --task-id rt-x [--state-dir DIR]
      Prints the settle note and exits 0 when the report is moot; exits 1 when it is not.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

SETTLED_STATUSES = {"blocked", "failed", "deferred"}
HANDLED_ACTIONS = {"complete", "skip"}
ENDED_WITHOUT_THE_WORK = {"failed", "cancelled"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_actions(state_dir: Path) -> dict[str, Any]:
    return load_json(Path(os.environ.get("COS_REVIEW_ACTIONS_FILE") or state_dir / "cos-review-actions.json"))


def owning_continuation(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str | None:
    """The id of a continuation that owns this row's work, walking the chain, because a worker
    that dies is continued again. A failed or cancelled continuation owns nothing, and neither
    does a supplemental worker. One older than the row's current attempt predates a deliberate
    re-dispatch of this row, so it does not count either."""
    children: dict[str, list[dict[str, Any]]] = {}
    for row in tasks:
        if isinstance(row, dict) and row.get("continuation_of") and not row.get("supplemental"):
            children.setdefault(str(row["continuation_of"]), []).append(row)
    began = str(task.get("attempt_began_at") or "")
    stack = [c for c in children.get(str(task.get("id")), []) if str(c.get("created_at") or "") >= began]
    seen: set[str] = set()
    owners: list[dict[str, Any]] = []
    while stack:
        row = stack.pop()
        row_id = str(row.get("id"))
        if row_id in seen:
            continue
        seen.add(row_id)
        stack.extend(children.get(row_id, []))
        if row.get("status") not in ENDED_WITHOUT_THE_WORK:
            owners.append(row)
    if not owners:
        return None
    return str(max(owners, key=lambda row: str(row.get("created_at") or "")).get("id"))


def handled_mark(task: dict[str, Any], actions: dict[str, Any]) -> dict[str, Any] | None:
    """The complete or skip record on the row's ask, made during its current attempt."""
    records = actions.get("items")
    record = records.get(f"task:{task.get('id')}") if isinstance(records, dict) else None
    if not isinstance(record, dict) or record.get("action") not in HANDLED_ACTIONS:
        return None
    if str(record.get("at") or "") < str(task.get("attempt_began_at") or ""):
        return None
    return record


def moot_note(task: dict[str, Any], tasks: list[dict[str, Any]], actions: dict[str, Any]) -> str | None:
    """The settle note when this row's undelivered report should never be sent, else None."""
    if task.get("status") not in SETTLED_STATUSES or task.get("report_delivered") is not False:
        return None
    owner = owning_continuation(task, tasks)
    if owner:
        return f"settled without send: the continuation {owner} owns this work"
    record = handled_mark(task, actions)
    if record:
        return f"settled without send: marked handled in CoS Review by {record.get('by') or 'the owner'}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--state-dir", default=os.environ.get("PM_OS_STATE_DIR") or str(Path(__file__).resolve().parents[1] / "state"))
    args = parser.parse_args()
    state_dir = Path(args.state_dir)
    ledger = Path(os.environ.get("RUNNING_TASKS_FILE") or state_dir / "running-tasks.json")
    tasks = [t for t in load_json(ledger).get("tasks", []) if isinstance(t, dict)]
    task = next((t for t in tasks if str(t.get("id")) == args.task_id), None)
    note = moot_note(task, tasks, load_actions(state_dir)) if task else None
    if not note:
        return 1
    print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
