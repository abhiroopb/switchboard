#!/usr/bin/env python3
"""Single source of truth for which P0 to-dos are "surfaced" on the cmux board.

A P0 is surfaced when it is:
  * active: status open|actionable and not snoozed into the future, OR
  * review-needed: status waiting whose review_on date has arrived
    (waiting items without a review_on stay hidden, per the todo skill's
    "waiting wake-up" semantics).

Consumers (keep in sync — all rely on the same "surfaced" definition):
  * system/cmux-helpers.sh   p0_active_todos_for_workstream (--slug)
  * system/scripts/ensure-p0-workspaces.sh   collect_p0s (no filter)
  * system/scripts/emit-workspace-update.py   has_surfaced_p0_for_workstream
    (--slug; keeps canonical 🔥 P0 tabs from auto-closing on waiting status)

Output: one line per surfaced P0, unit-separator (\\x1f) delimited:
  id \\x1f title \\x1f workstream \\x1f detail
\\x1f is not an IFS whitespace character, so empty fields (e.g. no
workstream) survive bash `read` without collapsing.

Usage: p0-todos.py <todo.json> [--slug <workstream-slug>]
Exit 0 with no output when nothing matches; exit 1 on unreadable input.
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

US = "\x1f"
TODO_CLI = Path(__file__).resolve().parents[2] / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"


def clean(value):
    return str(value).replace(US, " ").replace("\t", " ").replace("\n", " ")


def surfaced_p0s(items, now_iso=None, today=None):
    """Yield P0 todo dicts that are active or review-needed."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    today = today or date.today().isoformat()
    for t in items:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        if t.get("priority") != "P0":
            continue
        if t.get("bucket") == "confirmation":
            continue
        status = t.get("status")
        if status in ("open", "actionable"):
            pass
        elif status == "waiting":
            review_on = str(t.get("review_on") or "").strip()
            if not review_on or review_on[:10] > today:
                continue
        else:
            continue
        snooze = t.get("snooze_until")
        if snooze and str(snooze) > now_iso:
            continue
        yield t


def load_verified_todos(todo_path):
    # An empty candidate set cannot launch or close an active P0. Avoid a
    # global network gate when this surface has nothing to verify or display.
    try:
        raw = json.loads(Path(todo_path).read_text())
        if not any(surfaced_p0s(raw["items"])):
            return []
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"P0 store unreadable: {error}", file=sys.stderr)
        return None
    completed = subprocess.run(
        [
            sys.executable,
            str(TODO_CLI),
            "--path",
            str(todo_path),
            "snapshot",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        print(f"P0 verification deferred: {completed.stderr.strip() or completed.stdout.strip()}", file=sys.stderr)
        return None
    try:
        return list(json.loads(completed.stdout).get("items") or [])
    except (json.JSONDecodeError, AttributeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("todo_json")
    parser.add_argument(
        "--slug",
        default=None,
        help="only emit P0s whose workstream field equals this slug",
    )
    args = parser.parse_args(argv)

    items = load_verified_todos(args.todo_json)
    if items is None:
        return 1

    for t in surfaced_p0s(items):
        title = clean((t.get("title") or "Untitled P0").strip())
        workstream = clean(str(t.get("workstream") or "").strip())
        if args.slug is not None and workstream != args.slug:
            continue
        parts = [f"title: {title}"]
        for key in ("due", "review_on", "source", "source_ref", "context", "workstream", "estimate_min"):
            v = t.get(key)
            if v:
                parts.append(f"{key}: {v}")
        detail = " | ".join(clean(p) for p in parts)
        print(US.join([clean(t["id"]), title, workstream, detail]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
