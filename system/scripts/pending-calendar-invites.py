#!/usr/bin/env python3
"""Render pending calendar invites against the owner's remaining work week.

On-demand only. No review surface runs this: the start-day digest, the next-up
digest, and the CoS review never list pending invites or RSVP handles, because
the owner accepts and declines meetings directly. The library
functions (``fetch_live_events``, ``normalize_events``) still feed
``sod-workspace-plan.py`` with today's meetings for workspace seeds.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
CALENDAR_API = REPO_ROOT / "daemon" / "calendar-api.sh"
CALENDAR_LOG = REPO_ROOT / ".state" / "pending-calendar-invites.log"
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "{{OWNER_USER}}@gmail.com").lower()
DEFAULT_TIMEZONE = "America/Los_Angeles"
PENDING_STATUSES = {"needsaction", "tentative"}


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    response_status: str
    status: str
    transparency: str
    link: str


def parse_now(value: str | None, zone: ZoneInfo) -> datetime:
    if not value:
        return datetime.now(zone)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def workweek_window(now: datetime) -> tuple[datetime, datetime] | None:
    if now.weekday() > 4:
        return None
    start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    friday = now.date() + timedelta(days=4 - now.weekday())
    end = datetime.combine(friday + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    return start, end


def extract_events(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("items")
    if rows is None:
        rows = payload.get("events")
    if rows is None:
        rows = payload.get("result")
    if isinstance(rows, dict):
        rows = rows.get("events") or rows.get("items")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def my_response_status(raw: dict[str, Any]) -> str:
    for attendee in raw.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        email = str(attendee.get("email") or "").lower()
        if attendee.get("self") is True or email == OWNER_EMAIL:
            value = attendee.get("responseStatus") or attendee.get("response_status") or ""
            return str(value).replace("_", "").lower()
    return ""


def parse_event_time(
    raw: dict[str, Any],
    key: str,
    zone: ZoneInfo,
) -> tuple[datetime | None, bool]:
    nested = raw.get(key)
    if isinstance(nested, dict):
        date_time = nested.get("dateTime") or nested.get("date_time")
        date_value = nested.get("date")
    else:
        date_time = raw.get(f"{key}_time")
        date_value = raw.get(f"{key}_date")

    if date_time:
        parsed = datetime.fromisoformat(str(date_time).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(zone), False
    if date_value:
        parsed_date = date.fromisoformat(str(date_value))
        return datetime.combine(parsed_date, time.min, tzinfo=zone), True
    return None, False


def normalize_event(raw: dict[str, Any], zone: ZoneInfo) -> CalendarEvent | None:
    start, start_all_day = parse_event_time(raw, "start", zone)
    end, end_all_day = parse_event_time(raw, "end", zone)
    if start is None:
        return None
    all_day = start_all_day or end_all_day
    if end is None or end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    return CalendarEvent(
        event_id=str(raw.get("id") or raw.get("event_id") or ""),
        summary=" ".join(str(raw.get("summary") or "Untitled event").split()),
        start=start,
        end=end,
        all_day=all_day,
        response_status=my_response_status(raw),
        status=str(raw.get("status") or "confirmed").lower(),
        transparency=str(raw.get("transparency") or "opaque").lower(),
        link=str(raw.get("htmlLink") or raw.get("html_link") or ""),
    )


def normalize_events(payload: Any, zone: ZoneInfo) -> list[CalendarEvent]:
    events = []
    for raw in extract_events(payload):
        event = normalize_event(raw, zone)
        if event is not None:
            events.append(event)
    return events


def run_json(command: list[str], timeout: int) -> Any | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def fetch_live_events(start: datetime, end: datetime, timezone: str) -> tuple[Any | None, str]:
    CALENDAR_LOG.parent.mkdir(parents=True, exist_ok=True)
    if CALENDAR_API.exists():
        payload = run_json(
            [
                "bash",
                "-c",
                'source "$1" && fetch_calendar_events_json "$2" "$3" "$4"',
                "_",
                str(CALENDAR_API),
                start.isoformat(),
                end.isoformat(),
                str(CALENDAR_LOG),
            ],
            timeout=25,
        )
        if payload is not None:
            return payload, "direct Calendar API"

    time_period = json.dumps(
        {
            "time_min": start.replace(tzinfo=None).isoformat(),
            "time_max": end.replace(tzinfo=None).isoformat(),
        }
    )
    payload = run_json(
        [
            "switchboard",
            "calendar",
            "events",
            "--time-period",
            time_period,
            "--timezone",
            timezone,
            "--include-attendees-info",
            "--output",
            "json",
        ],
        timeout=30,
    )
    if payload is not None:
        return payload, "gcal fallback"
    return None, "unavailable"


def overlaps(left: CalendarEvent, right: CalendarEvent) -> bool:
    return left.start < right.end and right.start < left.end


def intersects_window(event: CalendarEvent, start: datetime, end: datetime) -> bool:
    return event.start < end and start < event.end


def blocks_time(event: CalendarEvent) -> bool:
    if event.status == "cancelled" or event.transparency == "transparent":
        return False
    return event.response_status not in {"declined", "needsaction", "tentative"}


def format_day(value: date) -> str:
    return value.strftime("%a %b %d").replace(" 0", " ")


def format_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def format_timed_range(event: CalendarEvent) -> str:
    return f"{format_time(event.start)}–{format_time(event.end)}"


def days_for_event(event: CalendarEvent, start: datetime, end: datetime) -> list[date]:
    first = max(event.start.date(), start.date())
    last = min((event.end - timedelta(microseconds=1)).date(), (end - timedelta(days=1)).date())
    days = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days


def busy_events(events: list[CalendarEvent], target: CalendarEvent) -> list[CalendarEvent]:
    return [event for event in events if event is not target and blocks_time(event)]


def conflict_events(events: list[CalendarEvent], target: CalendarEvent) -> list[CalendarEvent]:
    if target.all_day and target.transparency == "transparent":
        return []
    return [event for event in busy_events(events, target) if overlaps(event, target)]


def event_label(event: CalendarEvent) -> str:
    response = "Accepted" if event.response_status == "accepted" else "Scheduled"
    return f"{response} · {event.summary}"


def render_all_day_timeline(
    target: CalendarEvent,
    events: list[CalendarEvent],
    start: datetime,
    end: datetime,
) -> list[str]:
    lines = ["```text"]
    conflicts = conflict_events(events, target)
    for day in days_for_event(target, start, end):
        day_start = datetime.combine(day, time.min, tzinfo=start.tzinfo)
        day_end = day_start + timedelta(days=1)
        scheduled = [
            event
            for event in busy_events(events, target)
            if intersects_window(event, day_start, day_end)
        ]
        availability = "free" if target.transparency == "transparent" else "busy"
        suffix = f" · {len(scheduled)} scheduled item{'s' if len(scheduled) != 1 else ''}"
        if availability == "busy" and scheduled:
            suffix += f" · ‼ {len(scheduled)} conflict{'s' if len(scheduled) != 1 else ''}"
        lines.append(f"{format_day(day):<12} [?] pending all day ({availability}){suffix}")
    if conflicts:
        names = "; ".join(event.summary for event in conflicts[:5])
        if len(conflicts) > 5:
            names += f"; +{len(conflicts) - 5} more"
        lines.append(f"Conflicts: ‼ {names}")
    elif target.transparency == "transparent":
        lines.append("Conflicts: none; this invite is marked free.")
    else:
        lines.append("Conflicts: none.")
    lines.append("```")
    return lines


def render_timed_timeline(target: CalendarEvent, events: list[CalendarEvent]) -> list[str]:
    day_start = datetime.combine(target.start.date(), time.min, tzinfo=target.start.tzinfo)
    day_end = day_start + timedelta(days=1)
    view_start = max(day_start, target.start - timedelta(hours=3))
    view_end = min(day_end, target.end + timedelta(hours=3))
    conflicts = conflict_events(events, target)
    neighbors = [
        event
        for event in busy_events(events, target)
        if intersects_window(event, view_start, view_end)
    ]
    rows = sorted([target, *neighbors], key=lambda event: (event.start, event is not target))
    lines = ["```text", format_day(target.start.date())]
    for event in rows:
        if event is target:
            marker = "[?] PENDING" if target.response_status == "needsaction" else "[~] TENTATIVE"
            conflict = "  ‼ CONFLICT" if conflicts else ""
            lines.append(f"> {format_timed_range(event):<21} {marker} · {event.summary}{conflict}")
            continue
        conflict = "  ‼ overlaps invite" if event in conflicts else ""
        lines.append(f"  {format_timed_range(event):<21} ● {event_label(event)}{conflict}")
    if conflicts:
        detail = "; ".join(
            f"{event.summary} ({format_timed_range(event)})" for event in conflicts
        )
        lines.append(f"Conflicts: ‼ {detail}")
    else:
        lines.append("Conflicts: none.")
    lines.append("```")
    return lines


def format_window(start: datetime, end: datetime) -> str:
    last_day = (end - timedelta(days=1)).date()
    if start.date() == last_day:
        return format_day(start.date())
    return f"{format_day(start.date())}–{format_day(last_day)}"


def format_event_span(event: CalendarEvent) -> str:
    if not event.all_day:
        return f"{format_day(event.start.date())}, {format_timed_range(event)} PT"
    last_day = (event.end - timedelta(days=1)).date()
    if event.start.date() == last_day:
        return f"All day {format_day(event.start.date())}"
    return f"All day {format_day(event.start.date())}–{format_day(last_day)}"


def render_report(
    events: list[CalendarEvent],
    start: datetime,
    end: datetime,
    source: str,
) -> dict[str, Any]:
    in_window = [
        event
        for event in events
        if event.status != "cancelled" and intersects_window(event, start, end)
    ]
    pending = sorted(
        [event for event in in_window if event.response_status in PENDING_STATUSES],
        key=lambda event: (event.start, event.summary.lower()),
    )
    heading = f"=== 📆 Pending calendar invites · {format_window(start, end)} ==="
    if not pending:
        return {
            "available": True,
            "source": source,
            "pending_count": 0,
            "message": f"{heading}\n- No pending invites.",
        }

    lines = [heading]
    lines.append(
        f"- {len(pending)} pending invite{'s' if len(pending) != 1 else ''}. "
        "`‼ CONFLICT` marks overlap with accepted or scheduled time."
    )
    for index, target in enumerate(pending, start=1):
        status = "NEEDS RSVP" if target.response_status == "needsaction" else "TENTATIVE"
        title = f"[{target.summary}]({target.link})" if target.link else target.summary
        lines.extend(["", f"{index}. {title} · **{status}** · {format_event_span(target)}"])
        if target.all_day:
            lines.extend(render_all_day_timeline(target, in_window, start, end))
        else:
            lines.extend(render_timed_timeline(target, in_window))
    return {
        "available": True,
        "source": source,
        "pending_count": len(pending),
        "message": "\n".join(lines),
    }


def unavailable_report() -> dict[str, Any]:
    return {
        "available": False,
        "source": "unavailable",
        "pending_count": 0,
        "message": (
            "=== 📆 Pending calendar invites ===\n"
            "- ⚠️ Calendar is unavailable."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=os.environ.get("PENDING_CALENDAR_EVENTS_FILE"))
    parser.add_argument("--now", default=os.environ.get("PENDING_CALENDAR_NOW"))
    parser.add_argument(
        "--timezone",
        default=os.environ.get("PENDING_CALENDAR_TIMEZONE", DEFAULT_TIMEZONE),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    zone = ZoneInfo(args.timezone)
    now = parse_now(args.now, zone)
    window = workweek_window(now)
    if window is None:
        report = {
            "available": True,
            "source": "not-needed",
            "pending_count": 0,
            "message": (
                "=== 📆 Pending calendar invites ===\n"
                "- No work-week days remain before Saturday."
            ),
        }
    else:
        start, end = window
        if args.input:
            try:
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            source = "fixture"
        else:
            payload, source = fetch_live_events(start, end, args.timezone)
        report = unavailable_report() if payload is None else render_report(
            normalize_events(payload, zone), start, end, source
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(report["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
