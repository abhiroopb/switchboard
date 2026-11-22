#!/usr/bin/env python3
"""Query the canonical daily knowledge layer.

The single deterministic read path for every consumer prompt: meeting
pre-briefs, start-of-day / chief-of-staff, comms triage, workstream signals,
command center, and end-of-day summaries. Consumers get pre-resolved temporal
status so they never have to guess whether a fact is current.

Output separates:
    Current   - facts inside their freshness window as of --as-of
    Upcoming  - future events (calendar starts, due dates)
    Historical context - stale/superseded facts, clearly labeled with their
                event date. Consumers may use these ONLY as background, never
                as current updates. Excluded unless --include-historical.

Usage:
    python3 query-knowledge.py [--people slug,slug] [--workstream slug]
                               [--days N] [--as-of ISO]
                               [--include-historical] [--include-private]
                               [--format md|json] [--limit N]
                               [--knowledge-dir PATH]

Examples:
    # Meeting pre-brief: current signals for attendees, last 3 days
    query-knowledge.py --people jordan-example,casey-example --days 3

    # Start-of-day: everything current/upcoming today across sources
    query-knowledge.py --days 1 --include-historical
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from knowledge_snapshot import load_snapshot

SCRIPT_DIR = Path(__file__).resolve().parent  # <repo>/system/scripts
DEFAULT_REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_KNOWLEDGE_DIR = DEFAULT_REPO_ROOT / "system" / "state" / "knowledge"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_daily_knowledge", SCRIPT_DIR / "build-daily-knowledge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


builder = _load_builder()
if not DEFAULT_KNOWLEDGE_DIR.exists():
    # A task worktree has no store of its own; the one store lives on the main checkout.
    DEFAULT_KNOWLEDGE_DIR = builder.main_checkout(SCRIPT_DIR) / "system" / "state" / "knowledge"


def load_records(knowledge_dir: Path) -> list[dict[str, Any]]:
    snapshot = load_snapshot(knowledge_dir)
    if snapshot is not None:
        return snapshot['records']
    path = knowledge_dir / "records.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    rejected: list[tuple[int, str]] = []
    with path.open() as f:
        for line_number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record is not a JSON object")
                out.append(record)
            except (json.JSONDecodeError, ValueError) as exc:
                rejected.append((line_number, str(exc)))
    if rejected:
        details = ", ".join(f"line {number}: {reason}" for number, reason in rejected[:5])
        suffix = "" if len(rejected) <= 5 else f"; plus {len(rejected) - 5} more"
        sys.stderr.write(
            f"warning: knowledge records are incomplete; rejected {len(rejected)} malformed "
            f"row(s) from {path} ({details}{suffix})\n"
        )
    return out


ACTIONABILITY_RANK = {"action": 0, "review": 1, "fyi": 2}


def query(
    records: list[dict[str, Any]],
    *,
    as_of: datetime,
    people: list[str] | None = None,
    workstream: str | None = None,
    workstreams: list[str] | None = None,
    match_any: bool = False,
    days: float | None = None,
    include_historical: bool = False,
    include_private: bool = False,
    rank: str = "recency",
    repo_root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Filter and bucket records. Status is re-resolved against --as-of so a
    store built earlier today still answers correctly for replays.

    `workstream` (single) and `workstreams` (list) are combined into one
    workstream filter. With match_any=False (default) a record must match
    BOTH the people filter and the workstream filter (existing behavior).
    With match_any=True a record passes if it matches EITHER — used by
    meeting pre-briefs, where attendee-linked and workstream-linked signals
    are both relevant (a people-only query shows 'No current signals' when
    zero attendee profiles match)."""
    freshness, _ = builder.load_freshness(repo_root or DEFAULT_REPO_ROOT)
    ws_filter = set(workstreams or [])
    if workstream:
        ws_filter.add(workstream)
    buckets: dict[str, list[dict[str, Any]]] = {
        "current": [],
        "upcoming": [],
        "historical": [],
    }
    for rec in records:
        st = builder.parse_when(rec.get("source_time"))
        if st is None or st > as_of:
            continue
        if rec.get("source_class") == "todo":
            captured = builder.parse_when(rec.get("ingested_at"))
            if captured is None or captured > as_of:
                continue
        if not include_private and rec.get("sensitivity") == "private":
            continue
        people_hit = bool(people) and bool(set(people) & set(rec.get("people") or []))
        ws_hit = bool(ws_filter) and rec.get("workstream") in ws_filter
        if match_any:
            if (people or ws_filter) and not (people_hit or ws_hit):
                continue
        else:
            if people and not people_hit:
                continue
            if ws_filter and not ws_hit:
                continue
        # Re-resolve status as of the query time; supersession survives from build.
        if rec.get("status") == "superseded":
            status = "superseded"
        else:
            status = builder.resolve_status(dict(rec), as_of, freshness)
        if days is not None:
            anchor = builder.parse_when(rec.get("event_time")) or st
            # Recency filter applies to current facts; upcoming events pass,
            # historical ones are bucketed regardless (label carries the date).
            if status == "current" and (as_of - anchor) > timedelta(days=days):
                status = "historical"
        rec = {**rec, "status": status}
        if status in ("current",):
            buckets["current"].append(rec)
        elif status == "upcoming":
            buckets["upcoming"].append(rec)
        elif status in ("historical", "superseded", "completed", "cancelled"):
            if include_historical:
                buckets["historical"].append(rec)
    for key in buckets:
        buckets[key].sort(key=lambda r: r.get("event_time") or "", reverse=(key != "upcoming"))
    if rank == "meeting":
        # Meeting-prep ranking: a same-day doc comment must not rank below
        # generic workstream chatter. The current bucket is already event_time-descending;
        # a stable re-sort by (same-day first, actionability action >
        # review > fyi) preserves recency within each tier.
        as_of_day = as_of.astimezone(builder.LOCAL_TZ).date()

        def _tier(rec: dict[str, Any]) -> tuple[int, int]:
            et = builder.parse_when(rec.get("event_time"))
            same_day = bool(et and et.astimezone(builder.LOCAL_TZ).date() == as_of_day)
            return (
                0 if same_day else 1,
                ACTIONABILITY_RANK.get(str(rec.get("actionability")), 3),
            )

        buckets["current"].sort(key=_tier)
    return buckets


def _fmt_line(rec: dict[str, Any]) -> str:
    event_day = (rec.get("event_time") or "")[:10]
    people = ",".join(rec.get("people") or [])
    ws = rec.get("workstream") or ""
    parts = [
        f"- [{rec.get('status')}|{rec.get('actionability')}] {event_day} "
        f"{rec.get('source_class')}: {rec.get('topic')}"
    ]
    if people:
        parts.append(f"(people: {people})")
    if ws:
        parts.append(f"(ws: {ws})")
    if rec.get("url"):
        parts.append(f"<{rec['url']}>")
    return " ".join(parts)


def render_md(buckets: dict[str, list[dict[str, Any]]], limit: int) -> str:
    lines: list[str] = []
    sections = (
        ("current", "## Current (inside freshness window)"),
        ("upcoming", "## Upcoming"),
        ("historical", "## Historical context (background only — NOT current updates)"),
    )
    for key, header in sections:
        rows = buckets.get(key) or []
        if not rows:
            continue
        lines.append(header)
        for rec in rows[:limit]:
            lines.append(_fmt_line(rec))
        lines.append("")
    return "\n".join(lines).strip() or "(no matching knowledge records)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--people", help="comma-separated people/ slugs")
    ap.add_argument("--workstream")
    ap.add_argument("--workstreams", help="comma-separated workstream slugs (OR within)")
    ap.add_argument(
        "--match",
        choices=("all", "any"),
        default="all",
        help="'all' (default): record must match people AND workstream filters; "
        "'any': record passes if it matches either filter",
    )
    ap.add_argument("--days", type=float, help="recency window for current facts")
    ap.add_argument(
        "--rank",
        choices=("recency", "meeting"),
        default="recency",
        help="'recency' (default): current facts newest-first; 'meeting': "
        "same-day facts first, then actionability (action > review > fyi), "
        "recency within tiers — for meeting pre-briefs",
    )
    ap.add_argument("--as-of", help="ISO datetime for replay (default: now)")
    ap.add_argument("--include-historical", action="store_true")
    ap.add_argument("--include-private", action="store_true")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--knowledge-dir", type=Path, default=DEFAULT_KNOWLEDGE_DIR)
    ap.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    args = ap.parse_args()

    as_of = builder.parse_when(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if as_of is None:
        sys.stderr.write(f"could not parse --as-of {args.as_of!r}\n")
        return 2

    records = load_records(args.knowledge_dir)
    buckets = query(
        records,
        as_of=as_of,
        people=[p.strip() for p in args.people.split(",") if p.strip()] if args.people else None,
        workstream=args.workstream,
        workstreams=[w.strip() for w in args.workstreams.split(",") if w.strip()] if args.workstreams else None,
        match_any=(args.match == "any"),
        days=args.days,
        include_historical=args.include_historical,
        include_private=args.include_private,
        rank=args.rank,
        repo_root=args.repo_root,
    )
    if args.format == "json":
        out = {k: v[: args.limit] for k, v in buckets.items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_md(buckets, args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
