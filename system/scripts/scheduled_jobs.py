"""Shared due check for jobs in ``system/scheduled-jobs.yaml``.

One implementation for every reader: ``rebuild-megadesk-state.py`` (the
Scheduled Jobs source the routine and CoS read) and
``scheduled-job-dispatch.py`` (the command-center tick that dispatches
``dispatch: router`` jobs). Contract: ``routines/scheduled-jobs/CONTEXT.md``.

Schedules: ``daily``, ``weekly-<day>`` (due on that weekday, catch-up on every
later day until the job runs), ``biweekly-monday`` (anchored 2026-01-05).

Optional per-job time gate, Pacific Time:

* ``not_before: "HH:MM"``: on the due day itself the job is not due before
  this time. A run missed on an earlier week is still due (catch-up).
* ``catchup_not_before: "HH:MM"`` (default ``"08:00"``): on a catch-up day
  the job is not due before this time, so a missed Friday run starts the next
  morning, never at 3 AM.
* ``first_due: "YYYY-MM-DD"``: the job is never due for a date before this
  one, so a new job does not fire at once as a catch-up for last week.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

try:
    from zoneinfo import ZoneInfo

    PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    PT = None

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
BIWEEKLY_ANCHOR = date(2026, 1, 5)
DEFAULT_CATCHUP_NOT_BEFORE = "08:00"


def now_pt() -> datetime:
    return datetime.now(PT) if PT is not None else datetime.now()


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_hhmm(value: Any) -> time | None:
    if value in (None, ""):
        return None
    try:
        hour, minute = str(value).strip().split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def latest_due_date(schedule: str, today: date) -> date | None:
    """The most recent date on or before ``today`` when ``schedule`` falls due."""
    if schedule == "daily":
        return today
    if schedule.startswith("weekly-"):
        target = WEEKDAYS.get(schedule.split("weekly-", 1)[1])
        if target is None:
            return None
        return today - timedelta(days=(today.weekday() - target) % 7)
    if schedule == "biweekly-monday":
        monday = today - timedelta(days=today.weekday())
        weeks = (monday - BIWEEKLY_ANCHOR).days // 7
        if weeks < 0:
            return None
        return monday - timedelta(weeks=weeks % 2)
    return None


def previous_due_date(schedule: str, due: date) -> date | None:
    if schedule == "daily":
        return due - timedelta(days=1)
    if schedule.startswith("weekly-"):
        return due - timedelta(days=7)
    if schedule == "biweekly-monday":
        return due - timedelta(days=14)
    return None


def evaluate(job: dict[str, Any], last_run: str | None, now: datetime | None = None) -> dict[str, Any]:
    """Return ``{"due": bool, "due_date": "YYYY-MM-DD" | None, "reason": str}``.

    ``now`` is Pacific Time (naive values are read as PT).
    """
    now = now or now_pt()
    if now.tzinfo is not None and PT is not None:
        now = now.astimezone(PT)
    today = now.date()
    schedule = str(job.get("schedule") or "")
    last = parse_day(last_run)
    due = latest_due_date(schedule, today)
    if due is None:
        return {"due": False, "due_date": None, "reason": f"unknown schedule {schedule!r}"}
    first_due = parse_day(job.get("first_due"))
    if first_due is not None and due < first_due:
        return {"due": False, "due_date": first_due.isoformat(), "reason": f"first due {first_due.isoformat()}"}

    gate_reason = ""
    if due == today:
        gate = parse_hhmm(job.get("not_before"))
        if gate is not None and now.time() < gate:
            gate_reason = f"slot {gate.strftime('%H:%M')} PT today has not arrived"
            due = previous_due_date(schedule, due)
            if due is None or (last is not None and last >= due) or (first_due is not None and due < first_due):
                return {"due": False, "due_date": today.isoformat(), "reason": gate_reason}
    if last is not None and last >= due:
        return {"due": False, "due_date": due.isoformat(), "reason": f"ran {last.isoformat()}"}

    catching_up = due != today
    if catching_up:
        gate = parse_hhmm(job.get("catchup_not_before", DEFAULT_CATCHUP_NOT_BEFORE))
        if gate is not None and now.time() < gate:
            return {"due": False, "due_date": due.isoformat(),
                    "reason": f"catch-up for {due.isoformat()} waits for {gate.strftime('%H:%M')} PT"}
    reason = f"catch-up for {due.isoformat()}" if catching_up else "due today"
    if gate_reason:
        reason = f"{reason} ({gate_reason})"
    return {"due": True, "due_date": due.isoformat(), "reason": reason}


def next_fire(job: dict[str, Any], last_run: str | None, now: datetime | None = None,
              horizon_days: int = 21) -> datetime | None:
    """First PT minute, searched in 15-minute steps, when ``evaluate`` turns due."""
    now = now or now_pt()
    if now.tzinfo is not None and PT is not None:
        now = now.astimezone(PT)
    probe = now.replace(second=0, microsecond=0)
    if evaluate(job, last_run, probe)["due"]:
        return probe
    probe += timedelta(minutes=(15 - probe.minute % 15) % 15 or 15)
    end = probe + timedelta(days=horizon_days)
    while probe <= end:
        if evaluate(job, last_run, probe)["due"]:
            return probe
        probe += timedelta(minutes=15)
    return None
