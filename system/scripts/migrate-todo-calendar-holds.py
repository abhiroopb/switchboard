#!/usr/bin/env python3
"""Move the todo system's "[Todo] …" calendar holds off the primary calendar
onto the dedicated, unshared "To-dos (PM OS)" calendar.

One-time migration: holds on primary are visible to anyone who shares that
calendar. Uses the Calendar API `events.move`, which keeps the event id, so
the `calendar_event_id` stored on each todo item stays valid; only
`calendar_event_url` is refreshed from the moved event. Nothing is deleted:
an event that fails to move stays on primary and is listed as a leftover.

Usage:
  migrate-todo-calendar-holds.py            # move and report
  migrate-todo-calendar-holds.py --dry-run  # count only, no writes
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True)
    return Path(out.stdout.strip())


def load_todo_cli() -> Any:
    path = Path.home() / ".agents" / "skills" / "todo" / "scripts" / "todo_cli.py"
    spec = importlib.util.spec_from_file_location("todo_cli", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "todo_calendar_id"):
        raise SystemExit(f"{path} has no todo_calendar_id(); run install-amp-skills.sh (todo private-calendar patch) first")
    return module


def list_events(todo_cli: Any, calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        payload = todo_cli.calendar_api_request(
            "GET",
            f"calendars/{quote(calendar_id, safe='')}/events",
            query={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "maxResults": "2500",
                "pageToken": page_token,
            },
        )
        if not isinstance(payload, dict):
            raise SystemExit(f"could not list events on {calendar_id}")
        events.extend(item for item in payload.get("items", []) if isinstance(item, dict))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return events


def todo_holds(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("status") != "cancelled" and str(event.get("summary", "")).startswith("[Todo]")
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="count holds on both calendars without moving anything")
    parser.add_argument("--path", type=Path, default=None, help="todo.json path (default: todo_cli.TODO_PATH)")
    args = parser.parse_args()

    root = repo_root()
    todo_cli = load_todo_cli()
    todo_path = args.path or todo_cli.TODO_PATH

    calendar_id = todo_cli.todo_calendar_id()
    if not calendar_id:
        raise SystemExit("no dedicated to-do calendar reachable (Calendar API unavailable); nothing moved")
    if calendar_id == "primary":
        raise SystemExit("refusing: to-do calendar id resolved to primary")

    now = dt.datetime.now(dt.timezone.utc)
    time_min = (now - dt.timedelta(days=365)).isoformat()
    time_max = (now + dt.timedelta(days=730)).isoformat()

    before_primary = todo_holds(list_events(todo_cli, "primary", time_min, time_max))
    before_dedicated = todo_holds(list_events(todo_cli, calendar_id, time_min, time_max))
    report: dict[str, Any] = {
        "repo_root": str(root),
        "calendar_id": calendar_id,
        "before": {"primary": len(before_primary), "dedicated": len(before_dedicated)},
    }

    if args.dry_run:
        report["dry_run"] = True
        print(json.dumps(report, indent=2))
        return 0

    moved: list[str] = []
    leftovers: list[dict[str, str]] = []
    relinked = 0
    with todo_cli.exclusive_lock(todo_path):
        data = todo_cli.load_data(todo_path)
        by_event_id = {
            str(item.get("calendar_event_id")): item
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("calendar_event_id")
        }
        for event in before_primary:
            event_id = str(event.get("id"))
            response = todo_cli.calendar_api_request(
                "POST",
                f"calendars/primary/events/{quote(event_id, safe='')}/move",
                query={"destination": calendar_id},
            )
            if not isinstance(response, dict) or response.get("id") != event_id:
                leftovers.append({"id": event_id, "summary": str(event.get("summary", ""))})
                continue
            moved.append(event_id)
            item = by_event_id.get(event_id)
            if item is not None and response.get("htmlLink"):
                item["calendar_event_url"] = response["htmlLink"]
                relinked += 1
        if relinked:
            todo_cli.save_data(todo_path, data)

    after_primary = todo_holds(list_events(todo_cli, "primary", time_min, time_max))
    after_dedicated = todo_holds(list_events(todo_cli, calendar_id, time_min, time_max))
    linked_items = len(by_event_id)
    dedicated_ids = {str(event.get("id")) for event in after_dedicated}
    orphan_items = sorted(event_id for event_id in by_event_id if event_id not in dedicated_ids)

    report.update(
        {
            "moved": len(moved),
            "relinked_items": relinked,
            "leftovers_on_primary": leftovers,
            "after": {"primary": len(after_primary), "dedicated": len(after_dedicated)},
            "todo_items_with_event_id": linked_items,
            "items_whose_event_is_not_on_dedicated": orphan_items,
        }
    )
    print(json.dumps(report, indent=2))
    return 1 if leftovers else 0


if __name__ == "__main__":
    sys.exit(main())
