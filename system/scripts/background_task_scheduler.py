#!/usr/bin/env python3
"""Select due background tasks without starving later config entries."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Ticks start on the clock (:00 and :30), but a success is stamped when the task
# finishes, up to a few minutes into its tick. Without this slack a 30-minute task
# stamped at 15:30:12 is 29.95 minutes old at the 16:00:09 tick, counts as not due,
# and really runs hourly.
TICK_SLACK_MINUTES = 5


def is_due(elapsed_minutes: float, interval_minutes: int) -> bool:
    slack = min(TICK_SLACK_MINUTES, interval_minutes / 6)
    return elapsed_minutes >= interval_minutes - slack


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def select_due_tasks(
    config: dict[str, Any],
    successes: dict[str, Any],
    attempts: dict[str, Any],
    *,
    now: datetime,
    local_now: datetime,
    default_timeout: int,
) -> list[dict[str, Any]]:
    """Return due tasks ordered by oldest attempt, then config order.

    Success timestamps decide whether work is due. Attempt timestamps decide
    fairness. A failing early task therefore rotates behind every other due
    task instead of consuming one fixed-order slot on every scheduler tick.
    """
    due: list[tuple[tuple[int, float, int], dict[str, Any]]] = []
    now = now.astimezone(timezone.utc)
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("background task execution config must be a mapping")

    for index, task in enumerate(config.get("tasks", [])):
        if not isinstance(task, dict) or not task.get("enabled", False):
            continue
        name = str(task.get("name", "")).strip()
        if not name:
            continue
        executor = execution.get(name)
        if not isinstance(executor, dict):
            raise ValueError(f"background task {name!r} has no execution config")
        executor_type = str(executor.get("type", "")).strip()
        if executor_type not in {"command", "amp"}:
            raise ValueError(f"background task {name!r} has invalid executor {executor_type!r}")
        payload_key = "prompt" if executor_type == "amp" else "command"
        payload = str(executor.get(payload_key) or task.get(payload_key, "")).strip()
        if not payload:
            raise ValueError(f"background task {name!r} has no {payload_key}")
        if task.get("weekdays_only") and local_now.weekday() >= 5:
            continue
        start_hour = task.get("start_hour")
        end_hour = task.get("end_hour")
        if start_hour is not None and local_now.hour < int(start_hour):
            continue
        if end_hour is not None and local_now.hour >= int(end_hour):
            continue

        interval_minutes = int(task.get("interval_minutes", 60))
        last_success = parse_timestamp(successes.get(name))
        if last_success is not None:
            elapsed_minutes = (now - last_success).total_seconds() / 60
            if not is_due(elapsed_minutes, interval_minutes):
                continue

        last_attempt = parse_timestamp(attempts.get(name))
        # Never-attempted tasks go first. Among attempted tasks, the oldest
        # attempt wins. Config order is only the final stable tiebreaker.
        order = (
            0 if last_attempt is None else 1,
            last_attempt.timestamp() if last_attempt is not None else 0.0,
            index,
        )
        entry = {
            "name": name,
            "executor": executor_type,
            payload_key: payload,
            "timeout_seconds": int(task.get("timeout_seconds", default_timeout)),
        }
        due.append((order, entry))

    due.sort(key=lambda item: item[0])
    return [task for _, task in due]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--success-state", type=Path, required=True)
    parser.add_argument("--attempt-state", type=Path, required=True)
    parser.add_argument("--default-timeout", type=int, required=True)
    args = parser.parse_args()

    config = json.loads(os.environ["TASK_CONFIG_JSON"])
    now = datetime.now(timezone.utc)
    tasks = select_due_tasks(
        config,
        read_json(args.success_state),
        read_json(args.attempt_state),
        now=now,
        local_now=now.astimezone(),
        default_timeout=args.default_timeout,
    )
    for task in tasks:
        print(json.dumps(task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
