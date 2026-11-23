#!/usr/bin/env python3
"""Build the canonical daily knowledge layer that query-knowledge.py reads.

One atomic generation under system/state/knowledge/ on the main checkout:

    snapshot.json            {"version": 1, "coverage": {...}, "records": [...]}
    records.jsonl            the same records, one per line (legacy readers)
    coverage/YYYY-MM-DD.json the coverage manifest for the day

Rules come from system/config/temporal-contract.md: event time is not written
time, a fact is current only inside its source-class freshness window
(system/config/knowledge-freshness.yaml when present, else the defaults
below), anything older than hard_historical_days is historical, and done or
cancelled things stay resolved.

Sources this kit can read on any machine:

    todo          system/state/todo.json
    workstream    workstreams/*/CONTEXT.md + config.yaml (status, next step)
    switchboard_output  dated durable artifacts (YYYY-MM-DD-*.md) in workstreams/,
                  outputs/, projects/, knowledge/decisions/, notes/daily/
    calendar      this week and next through `switchboard calendar`
                  (the switchboard connector shim or another switchboard CLI); skipped with
                  --no-calendar or when switchboard is not on PATH

A source whose read fails is listed in missing_source_classes, so a gap never
looks like a quiet day. Comms collectors (Slack, Gmail, GitHub, Linear, Jira)
do not ship in the kit; they are listed under not_collected, not as missing.

query-knowledge.py imports this file as a module for LOCAL_TZ, parse_when,
load_freshness and resolve_status, so keep those names stable.

Usage:
    python3 system/scripts/build-daily-knowledge.py [--no-calendar] [--repo-root PATH]
                                                    [--knowledge-dir PATH] [--print]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_FRESHNESS: dict[str, Any] = {
    "default_days": 7,
    "hard_historical_days": 14,
    "classes": {
        "slack": 3,
        "meetings": 3,
        "calendar": 3,
        "gmail": 7,
        "docs": 7,
        "issues": 7,
        "todo": 7,
        "workstream": 7,
        "switchboard_output": 7,
        "product_kb": 7,
    },
}
NOT_COLLECTED = ["slack", "gmail", "github", "linear", "jira"]
OUTPUT_ROOTS = ("workstreams", "outputs", "projects", "knowledge/decisions", "notes/daily")
OUTPUT_SKIP_PARTS = {"_archive", ".signals", "inbox", "assets", "sources", "todo-refresh", "compare", "reviews-archive"}
DATED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --------------------------------------------------------------- time helpers
def parse_when(value: Any) -> datetime | None:
    """ISO date or datetime (Z, offset, or naive local) -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time(0, 0))
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.combine(date.fromisoformat(text), time(0, 0))
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def load_freshness(repo_root: Path) -> tuple[dict[str, Any], str]:
    """(freshness config, origin). Reads system/config/knowledge-freshness.yaml when present."""
    path = Path(repo_root) / "system" / "config" / "knowledge-freshness.yaml"
    config = json.loads(json.dumps(DEFAULT_FRESHNESS))
    if not path.exists():
        return config, "defaults"
    try:
        import yaml  # PyYAML ships with every switchboard install

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return config, "defaults (unreadable knowledge-freshness.yaml)"
    if isinstance(loaded, dict):
        for key in ("default_days", "hard_historical_days"):
            if isinstance(loaded.get(key), (int, float)):
                config[key] = loaded[key]
        classes = loaded.get("classes") or loaded.get("source_classes") or {}
        if isinstance(classes, dict):
            for name, value in classes.items():
                days = value.get("days") if isinstance(value, dict) else value
                if isinstance(days, (int, float)):
                    config["classes"][str(name)] = days
    return config, str(path)


def resolve_status(rec: dict[str, Any], as_of: datetime, freshness: dict[str, Any]) -> str:
    """current | upcoming | historical | completed | cancelled, judged by event time."""
    lifecycle = rec.get("lifecycle")
    if lifecycle in ("completed", "cancelled"):
        return str(lifecycle)
    event = parse_when(rec.get("event_time")) or parse_when(rec.get("source_time"))
    if event is None:
        return "historical"
    if event > as_of:
        return "upcoming"
    age = as_of - event
    hard = timedelta(days=float(freshness.get("hard_historical_days", 14)))
    if age > hard:
        return "historical"
    window = freshness.get("classes", {}).get(rec.get("source_class"), freshness.get("default_days", 7))
    return "current" if age <= timedelta(days=float(window)) else "historical"


# --------------------------------------------------------------- repo helpers
def main_checkout(start: Path) -> Path:
    """The main checkout (where shared state lives), also from a task worktree."""
    sys.path.insert(0, str(SCRIPT_DIR))
    import repo_paths

    return repo_paths.main_checkout(start.resolve())


def record_id(*parts: Any) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def first_heading(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            in_frontmatter = False
            for index, line in enumerate(handle):
                if index > 80:
                    break
                stripped = line.strip()
                if index == 0 and stripped == "---":
                    in_frontmatter = True
                    continue
                if in_frontmatter:
                    if stripped == "---":
                        in_frontmatter = False
                    elif stripped.startswith("title:"):
                        return stripped.split(":", 1)[1].strip().strip("'\"") or None
                    continue
                if stripped.startswith("#"):
                    return stripped.lstrip("#").strip() or None
    except OSError:
        return None
    return None


def mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


# --------------------------------------------------------------- collectors
def collect_todo(root: Path, now: datetime) -> list[dict[str, Any]]:
    path = root / "system" / "state" / "todo.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    records = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("text") or item.get("summary")
        if not title:
            continue
        created = parse_when(item.get("created_at") or item.get("captured_at")) or now
        updated = parse_when(item.get("updated_at") or item.get("completed_at")) or created
        due = parse_when(item.get("due") or item.get("due_date"))
        status = str(item.get("status") or "open").lower()
        priority = str(item.get("priority") or "")
        lifecycle = None
        if status in ("done", "completed", "complete"):
            lifecycle = "completed"
        elif status in ("dropped", "cancelled", "canceled"):
            lifecycle = "cancelled"
        records.append({
            "id": record_id("todo", item.get("id") or title),
            "source_class": "todo",
            "topic": f"{priority} {title}".strip(),
            "source_time": iso(min(updated, now)),
            "ingested_at": iso(created),
            "event_time": iso(due or created),
            "lifecycle": lifecycle,
            "actionability": "action" if priority in ("P0", "P1") and not lifecycle else "review",
            "sensitivity": "normal",
            "people": [str(p) for p in item.get("people") or []],
            "workstream": item.get("workstream"),
            "url": item.get("source_url") or item.get("link"),
        })
    return records


def collect_workstreams(root: Path, now: datetime) -> list[dict[str, Any]]:
    records = []
    base = root / "workstreams"
    if not base.is_dir():
        return records
    try:
        import yaml
    except ImportError:  # pragma: no cover
        yaml = None
    for context in sorted(base.glob("*/CONTEXT.md")):
        slug = context.parent.name
        if slug.startswith(("_", ".")):
            continue
        config: dict[str, Any] = {}
        cfg_path = context.parent / "config.yaml"
        if yaml and cfg_path.exists():
            try:
                config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                config = {}
        status = str(config.get("status") or "active")
        worked = parse_when(config.get("last_worked_on")) or mtime(context)
        next_step = config.get("next_step") or ""
        name = config.get("name") or slug
        records.append({
            "id": record_id("workstream", slug),
            "source_class": "workstream",
            "topic": f"{name}: {status}" + (f"; next: {next_step}" if next_step else ""),
            "source_time": iso(min(mtime(context), now)),
            "ingested_at": iso(now),
            "event_time": iso(min(worked, now)),
            "lifecycle": "completed" if status in ("done", "resolved") else ("cancelled" if status == "cancelled" else None),
            "actionability": "review",
            "sensitivity": "normal",
            "people": [],
            "workstream": slug,
            "path": context.relative_to(root).as_posix(),
        })
    return records


def collect_outputs(root: Path, now: datetime) -> list[dict[str, Any]]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import private_workstreams

    private = private_workstreams.slugs(root)
    records = []
    for rel in OUTPUT_ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            parts = set(path.relative_to(root).parts[:-1])
            if parts & OUTPUT_SKIP_PARTS or path.name == "latest.md":
                continue
            if private_workstreams.is_private_path(path.relative_to(root).as_posix(), private):
                continue
            match = DATED_NAME.match(path.name)
            if not match:
                continue
            try:
                event = parse_when(match.group(1))
            except ValueError:
                continue
            rel_path = path.relative_to(root).as_posix()
            workstream = path.relative_to(root).parts[1] if rel_path.startswith("workstreams/") else None
            records.append({
                "id": record_id("switchboard_output", rel_path),
                "source_class": "switchboard_output",
                "topic": first_heading(path) or path.stem,
                "source_time": iso(min(mtime(path), now)),
                "ingested_at": iso(now),
                "event_time": iso(event),
                "lifecycle": None,
                "actionability": "fyi",
                "sensitivity": "private" if "sensitive" in rel_path else "normal",
                "people": [],
                "workstream": workstream,
                "path": rel_path,
            })
    return records


def collect_calendar(root: Path, now: datetime) -> list[dict[str, Any]]:
    switchboard = shutil.which("switchboard")
    if not switchboard:
        raise RuntimeError("switchboard not on PATH")
    records = []
    for period in ("this_week", "next_week"):
        proc = subprocess.run(
            [switchboard, "calendar", "events",
             "--time-period", json.dumps(period), "--output", "json"],
            capture_output=True, text=True, timeout=90, cwd=root,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"google-calendar {period}: exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        payload = json.loads(proc.stdout or "{}")
        events = payload.get("result") if isinstance(payload, dict) else payload
        for ev in events or []:
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            start = parse_when(ev.get("start_time") or ev.get("start_date"))
            if start is None:
                continue
            state = str(ev.get("meeting_state") or "")
            lifecycle = "cancelled" if ev.get("status") == "cancelled" else ("completed" if state == "completed" else None)
            records.append({
                "id": record_id("calendar", ev["id"]),
                "source_class": "calendar",
                "topic": ev.get("summary") or "(no title)",
                "source_time": iso(now),
                "ingested_at": iso(now),
                "event_time": iso(start),
                "lifecycle": lifecycle,
                "actionability": "review",
                "sensitivity": "private" if ev.get("visibility") == "private" else "normal",
                "people": [],
                "workstream": None,
                "url": ev.get("html_link"),
            })
    return records


COLLECTORS = {
    "todo": collect_todo,
    "workstream": collect_workstreams,
    "switchboard_output": collect_outputs,
    "calendar": collect_calendar,
}


# --------------------------------------------------------------- build
def build(root: Path, knowledge_dir: Path, *, calendar: bool = True, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    freshness, origin = load_freshness(root)
    records: dict[str, dict[str, Any]] = {}
    classes: dict[str, Any] = {}
    missing: list[str] = []
    for name, collect in COLLECTORS.items():
        if name == "calendar" and not calendar:
            classes[name] = {"count": 0, "skipped": True, "stale": False}
            continue
        try:
            rows = collect(root, now)
        except Exception as exc:  # one failed source never hides the others
            missing.append(name)
            classes[name] = {"count": 0, "error": str(exc)[:300], "stale": False}
            continue
        for row in rows:
            previous = records.get(row["id"])
            if previous is None or (row.get("source_time") or "") >= (previous.get("source_time") or ""):
                records[row["id"]] = row
        classes[name] = {"count": len(rows), "last_success": iso(now), "stale": False}
    ordered = sorted(records.values(), key=lambda r: (r.get("event_time") or "", r["id"]), reverse=True)
    for row in ordered:
        row["status"] = resolve_status(row, now, freshness)
    day = now.astimezone(LOCAL_TZ).date().isoformat()
    coverage = {
        "day": day,
        "generated_at": iso(now),
        "records_total": len(ordered),
        "classes": classes,
        "missing_source_classes": missing,
        "not_collected": NOT_COLLECTED,
        "not_collected_note": "comms collectors do not ship in the switchboard kit",
        "freshness": origin,
    }
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "coverage").mkdir(exist_ok=True)
    snapshot = {"version": 1, "coverage": coverage, "records": ordered}
    write_atomic(knowledge_dir / "records.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in ordered))
    write_atomic(knowledge_dir / "coverage" / f"{day}.json", json.dumps(coverage, indent=2, sort_keys=True) + "\n")
    write_atomic(knowledge_dir / "snapshot.json", json.dumps(snapshot, sort_keys=True) + "\n")
    return coverage


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):  # Windows: a reader holding the file makes replace fail briefly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            import time as _time

            _time.sleep(0.3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-root", help="default: the main checkout of the repo in the current directory")
    parser.add_argument("--knowledge-dir", help="default: <repo-root>/system/state/knowledge")
    parser.add_argument("--no-calendar", action="store_true", help="skip the calendar source (no network)")
    parser.add_argument("--print", action="store_true", help="print the coverage manifest")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve() if args.repo_root else main_checkout(Path.cwd())
    knowledge_dir = Path(args.knowledge_dir) if args.knowledge_dir else root / "system" / "state" / "knowledge"
    coverage = build(root, knowledge_dir, calendar=not args.no_calendar)
    if args.print:
        print(json.dumps(coverage, indent=2, sort_keys=True))
    else:
        counts = ", ".join(f"{k} {v.get('count', 0)}" for k, v in coverage["classes"].items())
        gaps = f"; missing: {', '.join(coverage['missing_source_classes'])}" if coverage["missing_source_classes"] else ""
        print(f"knowledge: {coverage['records_total']} records ({counts}){gaps} -> {knowledge_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
