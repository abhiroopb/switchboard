#!/usr/bin/env python3

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

import comms_triage_setting
from events_stream import append_event_line
from knowledge_snapshot import load_snapshot
from workstream_metadata import GENERIC_MATCH_TERMS, TERMINAL_STATUSES, format_status_label, is_scaffold_next_step, load_workstream_metadata


PRIORITY_SCORE = {"high": 90, "medium": 60, "low": 30}
STATUS_SCORE = {
    "active": 25,
    "needs action": 18,
    "needs review": 16,
    # Live emit-workspace-update vocabulary: a card missing here scores 0.
    "to review": 16,
    "new": 12,
    "waiting": -8,
    "blocked": -18,
    "needs follow-up": 6,
    "paused": -35,
    "reference": -65,
    "done": -120,
    "resolved": -120,
    "cancelled": -120,
    "archived": -120,
}

LOG_TIMESTAMP_RE = re.compile(r"^\[(?P<timestamp>[^\]]+)\]\s*(?:\[[^\]]+\]\s*)?(?P<message>.*)$")
COMMS_SUMMARY_RE = re.compile(r"(?P<critical>\d+)\s+critical,\s+(?P<important>\d+)\s+important", re.IGNORECASE)
COMMS_ADDED_RE = re.compile(r"\((?P<added>\d+)\s+added(?:,\s*(?P<deduped>\d+)\s+deduped)?\)", re.IGNORECASE)
MEETING_ACTIONS_RE = re.compile(r"actions:\s*(?P<count>\d+)\s+for\s+the owner", re.IGNORECASE)
TODO_DONE_STATUSES = {"done", "completed", "complete", "closed", "cancelled", "canceled", "resolved"}
TODO_WAITING_STATUSES = {"waiting", "blocked", "needs-response", "needs-follow-up"}
TODO_SCHEDULED_STATUSES = {"scheduled", "snoozed", "deferred"}
TODO_SOMEDAY_STATUSES = {"someday", "backlog", "maybe", "parking-lot"}
TODO_STOPWORDS = {
    "a",
    "an",
    "and",
    "check",
    "follow",
    "for",
    "from",
    "in",
    "of",
    "on",
    "reply",
    "respond",
    "review",
    "re",
    "send",
    "the",
    "to",
    "up",
    "with",
}
TODO_DUE_STATE_RANK = {"overdue": 0, "today": 1, "upcoming": 2, "unscheduled": 3}
TODO_ROUTING_RANK = {"workstream": 0, "project": 1, "due": 2, "review_on": 2, "unrouted": 3}
TODO_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 3}
TODO_BACKLOG_BUCKET_RANK = {"actionable": 0, "waiting": 1, "scheduled": 2, "someday": 3, "done": 4}
TODO_DEFAULT_MAX_ACTIONABLE_DAILY = 5
TODO_STALE_ACTIONABLE_DAYS = 7
TODO_STALE_ACTIONABLE_PRIORITIES = {"P2", "P3"}
REQUIRES_USER_TERMS = {
    "acknowledge",
    "ask",
    "bring up",
    "call",
    "chat with",
    "confirm",
    "coordinate with",
    "decide with",
    "discuss",
    "follow up with",
    "meet with",
    "ping",
    "present",
    "raise",
    "reply to",
    "respond to",
    "send to",
    "share with",
    "sync with",
    "talk to",
}
REPLY_LIKE_PREFIXES = ("reply to", "respond to", "follow up", "follow-up", "confirm", "send")
from _local_tz import LOCAL_TZ as LOCAL_TIMEZONE
WORKDAY_END_HOUR = 18
TODO_CLI_PATH = Path.home() / ".agents" / "skills" / "todo" / "scripts" / "todo_cli.py"
GCAL_CLI_CANDIDATES = [
    Path.home() / ".agents" / "skills" / "gcal" / "gcal-cli.py",
    Path.home() / "agents" / "skills" / "gcal" / "gcal-cli.py",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
        if os.name == "nt":   # now.json / queue.json: refresh worktree hard links (lib/winlink.py)
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from lib import winlink
                winlink.relink_shared(path)
            except Exception:  # noqa: BLE001
                pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_json_file(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        # Fail loud but not fatal: a corrupt source must not silently become
        # "no work" (pm-os-reliability: no silent drop points).
        print(
            f"[rebuild-megadesk-state] WARNING: malformed JSON in {path}: {exc}; using default",
            file=sys.stderr,
        )
        return default


def load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text()
    if yaml is not None:
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as error:
            # One malformed config must not abort the whole rebuild
            # (for example a ScannerError from an unquoted ": " in free text).
            mark = getattr(error, "problem_mark", None)
            where = f" line {mark.line + 1} column {mark.column + 1}" if mark else ""
            print(f"[rebuild-megadesk-state] WARNING: invalid YAML in {path}{where}: "
                  f"{getattr(error, 'problem', None) or error}; using default", file=sys.stderr)
            return default
        return payload if payload is not None else default

    result = subprocess.run(
        [
            "ruby",
            "-ryaml",
            "-rjson",
            "-e",
            "print JSON.generate(YAML.safe_load(STDIN.read, aliases: true))",
        ],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return default

    payload = json.loads(result.stdout)
    return payload if payload is not None else default


def ensure_source_file(path: Path, default: Any) -> None:
    if not path.exists():
        write_json_file(path, default)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def path_mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def latest_timestamp(*values: str | None) -> str | None:
    candidates = [parse_iso(value) for value in values if value]
    candidates = [value for value in candidates if value is not None]
    if not candidates:
        return None
    return max(candidates).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cli_json(
    command: list[str],
    *,
    required: bool = False,
    timeout: int = 45,
    env: dict[str, str] | None = None,
) -> Any | None:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        if required:
            if isinstance(error, subprocess.TimeoutExpired):
                detail = f"timed out after {error.timeout} seconds"
            elif isinstance(error, subprocess.CalledProcessError):
                detail = f"exit {error.returncode}: {(error.stderr or '').strip()[:500]}"
            else:
                detail = str(error)
            raise RuntimeError(f"{' '.join(command[:2])}: {detail}") from error
        return None
    raw = completed.stdout.strip()
    if not raw:
        if required:
            raise RuntimeError(f"{' '.join(command[:2])}: empty output")
        return None
    for index, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            return json.loads(raw[index:])
        except json.JSONDecodeError:
            continue
    if required:
        raise RuntimeError(f"{' '.join(command[:2])}: invalid JSON output")
    return None


def find_gcal_cli() -> Path | None:
    for candidate in GCAL_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def fetch_calendar_events_via_daemon_api(time_min: str, time_max: str) -> Any | None:
    repo_root = Path(__file__).resolve().parents[2]
    calendar_api = repo_root / "daemon" / "calendar-api.sh"
    if not calendar_api.exists():
        return None

    log_path = repo_root / ".state" / "rebuild-megadesk-state-calendar.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return cli_json(
        [
            "bash",
            "-lc",
            'source "$1" && fetch_calendar_events_json "$2" "$3" "$4"',
            "_",
            str(calendar_api),
            time_min,
            time_max,
            str(log_path),
        ]
    )


def normalize_calendar_events(payload: Any | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_events = payload.get("events", payload.get("items", [])) or []
    if not isinstance(raw_events, list):
        return []
    return [event for event in raw_events if isinstance(event, dict)]


def token_set(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if len(token) < 3 or token in GENERIC_MATCH_TERMS or token in TODO_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def infer_requires_user(title: str, matched_todos: list[dict[str, Any]] | None = None, kind: str | None = None, workspace: str | None = None) -> bool:
    if matched_todos and any(bool(todo.get("requires_user")) for todo in matched_todos):
        return True
    if kind == "routine" and workspace in {"comms-triage", "meetings"}:
        return True
    text = str(title or "").lower()
    if text.startswith(REPLY_LIKE_PREFIXES):
        return True
    return any(term in text for term in REQUIRES_USER_TERMS)


def build_calendar_context() -> dict[str, Any]:
    now_local = datetime.now(LOCAL_TIMEZONE)
    day_start = datetime.combine(now_local.date(), time(0, 0), tzinfo=LOCAL_TIMEZONE)
    day_end = datetime.combine(now_local.date(), time(WORKDAY_END_HOUR, 0), tzinfo=LOCAL_TIMEZONE)

    # Prefer the same direct Calendar API path used by the scheduler and ingest
    # daemons. That keeps the megadesk queue trustworthy even when the
    # separate gcal / pmos auth path is flaky.
    payload = fetch_calendar_events_via_daemon_api(day_start.isoformat(), day_end.isoformat())

    if payload is None:
        gcal_cli = find_gcal_cli()
        if gcal_cli is None:
            return {"available_focus_minutes": None, "minutes_until_next_event": None, "upcoming": []}
        payload = cli_json(
            [
                "uv",
                "run",
                str(gcal_cli),
                "events",
                "list",
                "--time-min",
                day_start.isoformat(),
                "--time-max",
                day_end.isoformat(),
                "--limit",
                "100",
            ]
        )

    events = []
    for raw_event in normalize_calendar_events(payload):
        start = parse_iso((raw_event.get("start") or {}).get("dateTime") or (raw_event.get("start") or {}).get("date"))
        end = parse_iso((raw_event.get("end") or {}).get("dateTime") or (raw_event.get("end") or {}).get("date"))
        if start is None or end is None:
            continue
        local_start = start.astimezone(LOCAL_TIMEZONE)
        local_end = end.astimezone(LOCAL_TIMEZONE)
        if local_end <= now_local:
            continue
        events.append(
            {
                "summary": raw_event.get("summary") or "",
                "start": local_start,
                "end": local_end,
                "text": f"{raw_event.get('summary') or ''} {raw_event.get('description') or ''}",
                "tokens": token_set(raw_event.get("summary"), raw_event.get("description")),
            }
        )
    events.sort(key=lambda event: event["start"])

    next_event = events[0] if events else None
    next_boundary = next_event["start"] if next_event else day_end
    available_focus_minutes = max(0, int((next_boundary - now_local).total_seconds() // 60)) if next_boundary > now_local else 0
    minutes_until_next_event = max(0, int((next_event["start"] - now_local).total_seconds() // 60)) if next_event else None

    return {
        "available_focus_minutes": available_focus_minutes,
        "minutes_until_next_event": minutes_until_next_event,
        "upcoming": events,
    }


def best_matching_calendar_event(item: dict[str, Any], todo_matches: list[dict[str, Any]], calendar_context: dict[str, Any]) -> dict[str, Any] | None:
    signal_terms = [
        str(term).strip().lower()
        for term in item.get("signal_match_terms", [])
        if str(term).strip()
    ]
    card_tokens = token_set(item.get("display_name"), item.get("next_step"))
    for todo in todo_matches[:5]:
        card_tokens |= token_set(todo.get("title"), todo.get("context"), todo.get("source_ref"))
    best_match = None
    best_overlap = 0
    for event in calendar_context.get("upcoming", []):
        event_text = str(event.get("text") or event.get("summary") or "").lower()
        exact_matches = sum(1 for term in signal_terms if term in event_text)
        overlap = 100 + exact_matches if exact_matches else len(card_tokens & event["tokens"])
        if overlap < 2:
            continue
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = event
    return best_match


def extract_recent_log_messages(path: Path, matchers: tuple[str, ...], limit: int = 5) -> list[dict[str, str]]:
    if not path.exists():
        return []

    matches: list[dict[str, str]] = []
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed = LOG_TIMESTAMP_RE.match(line)
        if not parsed:
            continue
        message = parsed.group("message").strip()
        if not any(matcher in message for matcher in matchers):
            continue
        matches.append(
            {
                "timestamp": parsed.group("timestamp"),
                "message": message,
            }
        )
    return matches[-limit:]


def parse_recent_ingests(path: Path, limit: int = 5) -> list[dict[str, str]]:
    if not path.exists():
        return []

    matches: list[dict[str, str]] = []
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        parsed = LOG_TIMESTAMP_RE.match(line)
        if not parsed:
            continue
        message = parsed.group("message").strip()
        if "INGEST_OK:" not in message:
            continue
        ingest_summary = message.split("INGEST_OK:", 1)[1].strip()
        meeting = ingest_summary.split("|", 1)[0].strip()
        matches.append(
            {
                "timestamp": parsed.group("timestamp"),
                "meeting": meeting,
                "summary": ingest_summary,
            }
        )
    return matches[-limit:]


def check_collector_health(repo_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Inspect the collector log files for recent errors.

    Returns (overall_status, per_collector). overall_status is one of:
      - "ok"        — all known collectors logged a recent successful run
      - "degraded"  — at least one collector lacks a successful run
      - "unknown"   — no log lines found in the inspection window
    """
    logs_dir = repo_root / "system" / "logs"
    # (collector_id, log_filename, success_marker_substring)
    collectors = [
        ("slack-mentions", "slack-mentions.log", "done:"),
        ("slack-channels", "slack-channels.log", "done:"),
        ("gmail-inbox", "gmail-inbox.log", "done"),
        ("github-inbox", "github-inbox.log", "done"),
        ("linear-inbox", "linear-inbox.log", "done"),
        ("jira-issues", "jira-issues.log", "done"),
    ]
    error_markers = ("rc=2", "rc=1", "validation error", "error: unrecognized", "error rc=", "ERROR")
    per_collector: list[dict[str, Any]] = []
    saw_any = False
    for cid, filename, _success in collectors:
        path = logs_dir / filename
        if not path.exists():
            per_collector.append({"collector": cid, "status": "missing", "log": filename})
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()[-40:]
        except Exception:
            per_collector.append({"collector": cid, "status": "unreadable", "log": filename})
            continue
        if not lines:
            per_collector.append({"collector": cid, "status": "empty", "log": filename})
            continue
        saw_any = True
        # Find the last "start" → "done" window. If errors appeared between them, mark degraded.
        last_start_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if "start " in lines[i] or "start collect" in lines[i]:
                last_start_idx = i
                break
        if last_start_idx is None:
            per_collector.append({"collector": cid, "status": "no-recent-start", "log": filename})
            continue
        window = lines[last_start_idx:]
        had_error = any(any(marker in line for marker in error_markers) for line in window)
        last_line = window[-1] if window else ""
        completed = any(_success in line for line in window[1:])
        status = "ok" if completed and not had_error else "degraded"
        per_collector.append({
            "collector": cid,
            "status": status,
            "log": filename,
            "last_line": last_line[:200],
            "errors_in_last_run": had_error,
        })
    overall = "unknown"
    if saw_any:
        overall = "ok" if all(row["status"] == "ok" for row in per_collector) else "degraded"
    return overall, per_collector


def derive_comms_source(repo_root: Path) -> dict[str, Any]:
    background_state_path = repo_root / "system" / "background-task-state.json"
    background_log_path = repo_root / "system" / "logs" / "background-tasks.log"
    pending_path = repo_root / ".state" / "pending-amp-notifications.jsonl"
    comms_candidates_path = repo_root / "system" / "state" / "sources" / "comms-candidates.json"

    state = load_json(background_state_path, {})
    recent_summaries = []
    if background_log_path.exists():
        for line in background_log_path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("inbox-monitor:"):
                recent_summaries.append(stripped)
        recent_summaries = recent_summaries[-3:]

    pending_items = []
    if pending_path.exists():
        for raw_line in pending_path.read_text(errors="ignore").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                pending_items.append(json.loads(raw_line))
            except json.JSONDecodeError:
                continue

    comms_candidates = load_json(comms_candidates_path, {"updated_at": None, "items": []})

    items: list[dict[str, Any]] = []
    inbox_last_run = state.get("inbox-monitor")
    if inbox_last_run or recent_summaries:
        items.append(
            {
                "collector": "inbox-monitor",
                "last_run_at": inbox_last_run,
                "recent_summaries": recent_summaries,
            }
        )

    items.append(
        {
            "collector": "pending-notifications",
            "count": len(pending_items),
            "recent": pending_items[-3:],
        }
    )

    updated_at = latest_timestamp(
        inbox_last_run,
        pending_items[-1].get("created_at") if pending_items else None,
        path_mtime_iso(background_log_path),
        path_mtime_iso(background_state_path),
    )

    overall_status, collector_health = check_collector_health(repo_root)

    return {
        "updated_at": updated_at,
        "items": items,
        "candidates": comms_candidates.get("items", []),
        "candidate_summary": comms_candidates.get("summary", {}),
        "collector_status": overall_status,
        "collector_health": collector_health,
    }


def derive_meetings_source(repo_root: Path) -> dict[str, Any]:
    scheduler_log_path = repo_root / ".state" / "scheduled-jobs.log"
    daemon_log_path = repo_root / ".state" / "daemon.log"
    scheduled_ids_path = repo_root / ".state" / "scheduled-meeting-ids.json"
    meetings_path = repo_root / "system" / "state" / "sources" / "meetings.json"

    scheduled_ids = load_json(scheduled_ids_path, {"date": None, "event_ids": []})
    scheduled_event_ids = scheduled_ids.get("event_ids") or sorted(
        {
            job_key.split(":", 1)[1]
            for job_key in scheduled_ids.get("jobs", [])
            if isinstance(job_key, str) and ":" in job_key
        }
    )
    existing_meetings = load_json(meetings_path, {"items": []})
    scheduler_entries = extract_recent_log_messages(
        scheduler_log_path,
        (
            "Scheduled pre-brief",
            "Scheduled post-ingest",
            "Scheduled post-ingest retry",
            "Queued pre-brief",
            "Queued post-ingest",
            "Queued post-ingest retry",
            "Skipped pre-brief",
            "No new meetings to schedule jobs for.",
            "ERROR: Failed to fetch calendar events",
            "WARN: pmos calendar failed",
        ),
        limit=8,
    )
    ingests = parse_recent_ingests(daemon_log_path, limit=5)

    items: list[dict[str, Any]] = []
    if scheduled_ids.get("date") or scheduler_entries:
        items.append(
            {
                "collector": "meeting-scheduler",
                "date": scheduled_ids.get("date"),
                "scheduled_event_ids": scheduled_event_ids,
                "recent_log": scheduler_entries,
            }
        )
    items.extend(
        {
            "collector": "post-ingest",
            "meeting": ingest["meeting"],
            "summary": ingest["summary"],
            "processed_at": ingest["timestamp"],
        }
        for ingest in ingests
    )
    prebrief_cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    for item in existing_meetings.get("items", []) or []:
        if not isinstance(item, dict) or item.get("collector") != "pre-brief":
            continue
        generated_at = parse_iso(item.get("generated_at"))
        if generated_at is not None and generated_at < prebrief_cutoff:
            continue
        items.append(item)

    updated_at = latest_timestamp(
        scheduler_entries[-1]["timestamp"] if scheduler_entries else None,
        ingests[-1]["timestamp"] if ingests else None,
        path_mtime_iso(scheduler_log_path),
        path_mtime_iso(daemon_log_path),
        *(item.get("generated_at") for item in items if item.get("collector") == "pre-brief"),
    )

    return {
        "updated_at": updated_at,
        "items": items,
    }


def is_scheduled_job_due(schedule: str, last_run: str | None, today: datetime) -> bool:
    today_date = today.date()
    last_run_date = None
    if last_run:
        try:
            last_run_date = datetime.strptime(last_run, "%Y-%m-%d").date()
        except ValueError:
            last_run_date = None

    if schedule == "daily":
        return last_run_date != today_date

    if schedule.startswith("weekly-"):
        weekday_name = schedule.split("weekly-", 1)[1]
        weekday_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        target_weekday = weekday_map.get(weekday_name)
        if target_weekday is None:
            return False
        days_since_target = (today.weekday() - target_weekday) % 7
        due_date = today_date.replace() if days_since_target == 0 else today_date.fromordinal(today_date.toordinal() - days_since_target)
        return last_run_date is None or last_run_date < due_date

    if schedule == "biweekly-monday":
        if today.weekday() < 0:
            return False
        anchor = datetime.strptime("2026-01-05", "%Y-%m-%d").date()
        weeks_since_anchor = (today_date - anchor).days // 7
        if weeks_since_anchor < 0 or weeks_since_anchor % 2 != 0:
            return False
        due_monday = today_date.fromordinal(today_date.toordinal() - today.weekday())
        return last_run_date is None or last_run_date < due_monday

    return False


def derive_scheduled_jobs_source(repo_root: Path) -> dict[str, Any]:
    scheduled_jobs_path = repo_root / "system" / "scheduled-jobs.yaml"
    job_state_path = repo_root / "system" / "job-state.json"

    config = load_yaml(scheduled_jobs_path, {"jobs": []})
    state = load_json(job_state_path, {})
    today = datetime.now()

    items = []
    for job in config.get("jobs", []):
        name = job.get("name")
        if not name:
            continue
        last_run = state.get(name)
        items.append(
            {
                "name": name,
                "schedule": job.get("schedule"),
                "confirm": bool(job.get("confirm", False)),
                "last_run": last_run,
                "due": is_scheduled_job_due(str(job.get("schedule", "")), last_run, today),
            }
        )

    updated_at = latest_timestamp(
        path_mtime_iso(scheduled_jobs_path),
        path_mtime_iso(job_state_path),
    )

    return {
        "updated_at": updated_at,
        "items": items,
    }


def normalize_todo_priority(value: Any) -> str:
    text = f"P{value}" if isinstance(value, int) else str(value or "P3").upper().strip()
    if text in {"0", "1", "2", "3", "4"}:
        return {"0": "P0", "1": "P0", "2": "P1", "3": "P2", "4": "P3"}[text]
    if not text.startswith("P"):
        text = f"P{text}"
    if text == "P4":
        return "P3"
    return text


def normalize_todo_source_type(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    if not text:
        return "unknown"
    if "command center" in text or "command-center" in text or "megadesk" in text:
        return "megadesk"
    if "gmail" in text or text == "mail":
        return "gmail"
    if "slack" in text:
        return "slack"
    if "linear" in text:
        return "linear"
    if "calendar" in text or "meeting" in text:
        return "meeting"
    if "notion" in text:
        return "notion"
    if "figma" in text:
        return "figma"
    if "doc" in text:
        return "document"
    if text == "manual":
        return "manual"
    return text


def normalize_todo_source(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if normalize_todo_source_type(text) == "megadesk":
        return "Megadesk"
    return text


def title_stem(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    filtered = [word for word in words if word not in TODO_STOPWORDS]
    return " ".join(filtered[:6])


def todo_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def todo_rules(payload: dict[str, Any]) -> dict[str, Any]:
    raw_rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
    return {
        "max_actionable_daily": todo_positive_int(raw_rules.get("max_actionable_daily")) or TODO_DEFAULT_MAX_ACTIONABLE_DAILY,
    }


def apply_todo_actionable_cap(
    items: list[dict[str, Any]], actionable: list[dict[str, Any]], max_actionable_daily: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if max_actionable_daily <= 0 or len(actionable) <= max_actionable_daily:
        return actionable, []

    kept = actionable[:max_actionable_daily]
    overflow = actionable[max_actionable_daily:]
    overflow_ids = {item.get("id") for item in overflow}
    for item in items:
        if item.get("id") not in overflow_ids:
            continue
        item["bucket"] = "scheduled"
        item["daily_cap_overflow"] = True
    return kept, overflow


def todo_bucket(raw_status: str, snooze_until: str | None, review_on: str | None) -> str:
    status = raw_status.strip().lower() or "open"
    if status in TODO_DONE_STATUSES:
        return "done"
    if status in TODO_WAITING_STATUSES:
        return "waiting"
    if status in TODO_SOMEDAY_STATUSES:
        return "someday"
    if status in TODO_SCHEDULED_STATUSES:
        return "scheduled"

    now = datetime.now(timezone.utc)
    snooze_at = parse_iso(snooze_until)
    if snooze_at and snooze_at > now:
        return "scheduled"

    review_at = parse_iso(review_on)
    if review_at and review_at.date() > now.date():
        return "scheduled"

    return "actionable"


def todo_due_state(due: str | None, review_on: str | None, now: datetime) -> str:
    due_at = parse_iso(due) or parse_iso(review_on)
    if due_at is None:
        return "unscheduled"
    if due_at.date() < now.date():
        return "overdue"
    if due_at.date() == now.date():
        return "today"
    return "upcoming"


def todo_routing_kind(item: dict[str, Any]) -> str:
    if item.get("workstream"):
        return "workstream"
    if item.get("project"):
        return "project"
    if item.get("due"):
        return "due"
    if item.get("review_on"):
        return "review_on"
    return "unrouted"


def duplicate_key(item: dict[str, Any]) -> str | None:
    stem = title_stem(item.get("title", ""))
    if not stem:
        return None

    source_ref = str(item.get("source_ref") or "").strip()
    source_type = str(item.get("source_type") or "unknown").strip()
    due = str(item.get("due") or "").strip()

    if source_ref:
        return f"{source_type}|{source_ref}|{stem}"
    if due:
        return f"{source_type}|{due}|{stem}"
    return None


def todo_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    due_value = item.get("due") or item.get("review_on") or "9999-12-31"
    created_value = item.get("created_at") or "9999-12-31T00:00:00Z"
    return (
        TODO_PRIORITY_RANK.get(item.get("priority", "P3"), 9),
        TODO_DUE_STATE_RANK.get(item.get("due_state", "unscheduled"), 9),
        due_value,
        TODO_ROUTING_RANK.get(item.get("routing_kind", "unrouted"), 9),
        created_value,
        item.get("title", "").lower(),
    )


def todo_backlog_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (TODO_BACKLOG_BUCKET_RANK.get(item.get("bucket", "done"), 9), *todo_sort_key(item))


def build_todo_backlog(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    backlog = [item for item in items if item.get("bucket") != "done" and not item.get("is_duplicate")]
    backlog.sort(key=todo_backlog_sort_key)
    return backlog


def normalize_todo_item(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    source = normalize_todo_source(item.get("source"))
    normalized = {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "source": source,
        "source_type": normalize_todo_source_type(source or item.get("source")),
        "source_ref": item.get("source_ref"),
        "priority": normalize_todo_priority(item.get("priority")),
        "raw_status": str(item.get("status", "open") or "open"),
        "due": item.get("due"),
        "review_on": item.get("review_on"),
        "snooze_until": item.get("snooze_until"),
        "created_at": item.get("created_at"),
        "completed_at": item.get("completed_at") or item.get("completed"),
        "context": item.get("context"),
        "tags": item.get("tags") or [],
        "workstream": item.get("workstream"),
        "project": item.get("project"),
    }
    normalized["bucket"] = todo_bucket(normalized["raw_status"], normalized.get("snooze_until"), normalized.get("review_on"))
    normalized["routing_kind"] = todo_routing_kind(normalized)
    normalized["due_state"] = todo_due_state(normalized.get("due"), normalized.get("review_on"), now)
    created_at = parse_iso(normalized.get("created_at"))
    normalized["age_days"] = (now.date() - created_at.date()).days if created_at else None
    normalized["needs_routing"] = normalized["bucket"] == "actionable" and normalized["priority"] in {"P0", "P1"} and normalized["routing_kind"] == "unrouted"
    normalized["is_duplicate"] = False
    normalized["duplicate_of"] = None
    normalized["fingerprint"] = duplicate_key(normalized)
    return normalized


def stale_todo_recommendation(item: dict[str, Any]) -> str:
    if item.get("needs_routing"):
        return "route"
    if item.get("due_state") == "overdue":
        return "reschedule-or-close"
    if item.get("source_type") in {"gmail", "slack", "meeting"}:
        return "waiting-or-someday"
    return "review"


def todo_snapshot_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the todo snapshot run inside a Megadesk rebuild.

    The snapshot's Slack verification (`check-thread-resolution.py`) must not
    call Slack synchronously from a rebuild: rebuilds fire from several
    schedulers, and re-fetching the same unreadable threads many times an
    hour keeps every collector in rate-limit backoff. In
    offline mode the gate serves local outbound state and cached verdicts
    only; live refreshes belong to the Todo routine and comms callers. An
    explicit THREAD_GATE_OFFLINE in the caller's environment wins.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault("THREAD_GATE_OFFLINE", "1")
    return env


def load_open_todos(todo_path: Path, *, read_only: bool = False) -> dict[str, Any]:
    if not TODO_CLI_PATH.exists():
        raise RuntimeError(f"todo verification CLI is missing: {TODO_CLI_PATH}")
    # Snapshot retains bounded source verification and the live LaunchDarkly gate.
    # The Todo routine owns full reconciliation, outside the refresh timeout.
    # The snapshot's own external-tool timeout is 45s per source, so this
    # budget must exceed it or the two timeouts race.
    command = [sys.executable, str(TODO_CLI_PATH), "--path", str(todo_path), "snapshot"]
    if read_only:
        command.append("--dry-run")
    try:
        helper_payload = cli_json(command, required=True, timeout=150, env=todo_snapshot_env())
    except RuntimeError as error:
        raise RuntimeError(f"todo surface verification failed; Megadesk state was not rebuilt: {error}") from error
    if not (
        isinstance(helper_payload, dict)
        and "items" in helper_payload
        and "actionable" in helper_payload
    ):
        raise RuntimeError("todo surface verification failed; Megadesk state was not rebuilt")
    helper_items = helper_payload.get("items", [])
    return {
        "all": helper_items,
        "actionable": helper_payload.get("actionable", []),
        "backlog_items": build_todo_backlog(helper_items) if isinstance(helper_items, list) else [],
        "summary": helper_payload.get("summary", {}),
        "triage_candidates": helper_payload.get("triage_candidates", []),
        "stale_actionable": helper_payload.get("stale_actionable", []),
        "duplicates": helper_payload.get("duplicates", []),
        "auto_reconcile": helper_payload.get("auto_reconcile"),
        "rules": helper_payload.get("rules", {}),
    }


def due_today_launch_items(items: list[dict[str, Any]], target_date: str | None = None) -> list[dict[str, Any]]:
    """Keep explicit due/review-today overflow items available as launch signals."""
    today = target_date or datetime.now(LOCAL_TIMEZONE).date().isoformat()
    return [
        item
        for item in items
        if item.get("bucket") != "done"
        and not item.get("is_duplicate")
        and item.get("daily_cap_overflow") is True
        and (
            str(item.get("due") or "")[:10] == today
            or str(item.get("review_on") or "")[:10] == today
        )
    ]


def todo_matches_workstream(todo: dict[str, Any], item: dict[str, Any]) -> bool:
    explicit_workstream = str(todo.get("workstream") or "").strip().lower()
    if explicit_workstream:
        return explicit_workstream == str(item.get("slug") or "").strip().lower()

    title = str(todo.get("title") or "").lower()
    normalized_bits = set()

    for part in item["slug"].replace("_", "-").split("-"):
        part = part.strip().lower()
        if len(part) <= 2 or part in GENERIC_MATCH_TERMS:
            continue
        normalized_bits.add(part)

    display_source = item["display_name"].replace(":", " ").replace("/", " ").replace("-", " ")
    for part in display_source.split():
        part = part.strip().lower()
        if len(part) <= 2 or part in GENERIC_MATCH_TERMS:
            continue
        normalized_bits.add(part)

    matched_bits = [bit for bit in normalized_bits if bit in title]
    if len(matched_bits) >= 2:
        return True
    if any(len(bit) >= 8 for bit in matched_bits):
        return True

    for phrase in item.get("todo_match_terms", []):
        if phrase and phrase in title:
            return True

    return False


def comms_candidate_matches_workstream(candidate: dict[str, Any], item: dict[str, Any]) -> bool:
    if candidate.get("source_type") == "slack" and candidate.get("planning_relevant") is False:
        return False

    hints = [str(hint).strip().lower() for hint in candidate.get("workstream_hints") or [] if str(hint).strip()]
    if str(item.get("slug") or "").strip().lower() in hints:
        return True

    text_parts = [
        str(candidate.get("title") or ""),
        str(candidate.get("summary") or ""),
        str(candidate.get("from") or ""),
        str(candidate.get("source_ref") or ""),
    ]
    text_blob = " ".join(part.lower() for part in text_parts if part)
    if not text_blob:
        return False

    for phrase in item.get("todo_match_terms", []):
        if phrase and phrase in text_blob:
            return True

    normalized_bits = set()
    for part in item["slug"].replace("_", "-").split("-"):
        part = part.strip().lower()
        if len(part) <= 2 or part in GENERIC_MATCH_TERMS:
            continue
        normalized_bits.add(part)

    display_source = item["display_name"].replace(":", " ").replace("/", " ").replace("-", " ")
    for part in display_source.split():
        part = part.strip().lower()
        if len(part) <= 2 or part in GENERIC_MATCH_TERMS:
            continue
        normalized_bits.add(part)

    matched_bits = [bit for bit in normalized_bits if bit in text_blob]
    if len(matched_bits) >= 2:
        return True
    if any(len(bit) >= 8 for bit in matched_bits):
        return True
    return False


def derive_knowledge_source(repo_root: Path) -> dict[str, Any]:
    """Canonical knowledge layer health for queue/now consumers.

    Reads the latest coverage manifest written by build-daily-knowledge.py
    (system/state/knowledge/coverage/) so surfaces built on megadesk state can
    show when the reconciled layer is missing or stale source classes —
    a silent collector gap must never look like a quiet day
    (system/config/temporal-contract.md). Fail-soft: no manifest → status
    "unavailable". Deterministic ranking inputs (todo.json, comms-candidates,
    workstream CONTEXT status) are themselves canonical-layer upstreams;
    this source adds coverage visibility, not a new ranking signal.
    """
    cov_dir = repo_root / "system" / "state" / "knowledge" / "coverage"
    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "status": "unavailable",
        "day": None,
        "records_total": 0,
        "missing_source_classes": [],
        "stale_source_classes": [],
        "kb_feed_pending": 0,
        "kb_feed_oldest_day": None,
        "kb_feed_backlog": False,
    }

    # Product KB candidate-feed backlog: pending canonical→BRAIN inbox
    # files must be visible here, or they pile up silently when the CoS
    # drain does not run. Pending files older than 2 days flag the
    # source degraded so queue/now consumers surface the backlog.
    kb_status_path = repo_root / "system" / "state" / "knowledge" / "kb-feed-status.json"
    kb_backlog = False
    try:
        kb_status = json.loads(kb_status_path.read_text())
        pending_files = kb_status.get("pending_files") or []
        payload["kb_feed_pending"] = len(pending_files)
        days = sorted(
            m.group(0)
            for f in pending_files
            if (m := re.match(r"\d{4}-\d{2}-\d{2}", str(f)))
        )
        if days:
            payload["kb_feed_oldest_day"] = days[0]
            oldest = datetime.strptime(days[0], "%Y-%m-%d").date()
            kb_backlog = (datetime.now().date() - oldest).days >= 2
        payload["kb_feed_backlog"] = kb_backlog
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    files = sorted(cov_dir.glob("2*.json")) if cov_dir.is_dir() else []
    try:
        snapshot = load_snapshot(cov_dir.parent)
        if snapshot is not None:
            cov = snapshot['coverage']
        elif files:
            cov = json.loads(files[-1].read_text())
        else:
            return payload
    except (ValueError, OSError):
        return payload
    missing = cov.get("missing_source_classes") or []
    stale = sorted(
        cls for cls, meta in (cov.get("classes") or {}).items()
        if isinstance(meta, dict) and meta.get("stale")
    )
    payload.update(
        {
            "status": "degraded" if (missing or stale or kb_backlog) else "ok",
            "day": cov.get("day"),
            "generated_at": cov.get("generated_at"),
            "records_total": cov.get("records_total", 0),
            "missing_source_classes": missing,
            "stale_source_classes": stale,
        }
    )
    return payload


def derive_sources(repo_root: Path, state_dir: Path, todo_path: Path, *, persist: bool = True) -> dict[str, Any]:
    source_dir = state_dir / "sources"
    if persist:
        source_dir.mkdir(parents=True, exist_ok=True)
        ensure_source_file(source_dir / "workspace-updates.json", {"updated_at": None, "updates": {}})

    knowledge_source = derive_knowledge_source(repo_root)
    if persist:
        write_json_file(source_dir / "knowledge.json", knowledge_source)

    comms_source = derive_comms_source(repo_root)
    if persist:
        write_json_file(source_dir / "comms.json", comms_source)

    meetings_source = derive_meetings_source(repo_root)
    if persist:
        write_json_file(source_dir / "meetings.json", meetings_source)

    scheduled_jobs_source = derive_scheduled_jobs_source(repo_root)
    if persist:
        write_json_file(source_dir / "scheduled-jobs.json", scheduled_jobs_source)

    workstreams = load_workstream_metadata(repo_root)
    workstream_source = {
        "updated_at": now_iso(),
        "items": [
            {
                "slug": item["slug"],
                "display_name": item["display_name"],
                "status": item["status"],
                "status_label": item["status_label"],
                "priority": item["priority"],
                "next_step": item["next_step"],
                "estimate_min": item["estimate_min"],
                "last_worked_on": item["last_worked_on"],
                "last_worked_display": item["last_worked_display"],
                "paused_until": item["paused_until"],
                "description": item["description"],
            }
            for item in workstreams
        ],
    }
    if persist:
        write_json_file(source_dir / "workstreams.json", workstream_source)

    todo_payload = load_open_todos(todo_path, read_only=not persist)
    launch_items = due_today_launch_items(todo_payload["all"])
    todo_source = {
        "updated_at": now_iso(),
        "rules": todo_payload.get("rules", {}),
        "summary": todo_payload["summary"],
        "items": todo_payload["actionable"],
        # Due-today commitments remain launch signals even when the todo
        # system's daily cap moves them out of the visible actionable list.
        "launch_items": launch_items,
        "backlog_items": todo_payload["backlog_items"],
        "triage_candidates": todo_payload["triage_candidates"],
        "stale_actionable": todo_payload["stale_actionable"],
        "duplicates": todo_payload["duplicates"],
    }
    if todo_payload.get("auto_reconcile") is not None:
        todo_source["auto_reconcile"] = todo_payload["auto_reconcile"]
    if persist:
        write_json_file(source_dir / "todo.json", todo_source)

    today_plan = load_json(repo_root / "system" / "today-plan.json", {})
    plan_source = {
        "updated_at": now_iso(),
        "date": today_plan.get("date"),
        "workstreams_to_open": [ws.get("name") for ws in today_plan.get("workstreams_to_open", []) if ws.get("name")],
        "calendar_summary": today_plan.get("calendar_summary"),
        "todo_summary": today_plan.get("todo_summary"),
    }
    if persist:
        write_json_file(source_dir / "plan.json", plan_source)

    updates = load_json(source_dir / "workspace-updates.json", {"updated_at": None, "updates": {}})

    return {
        "workstreams": workstreams,
        "todos": todo_payload["actionable"],
        "launch_todos": launch_items,
        "todo_summary": todo_payload["summary"],
        "plan": plan_source,
        "updates": updates.get("updates", {}),
        "comms": comms_source,
        "meetings": meetings_source,
        "scheduled_jobs": scheduled_jobs_source,
        "knowledge": knowledge_source,
        "source_dir": source_dir,
    }


def routine_display_name(repo_root: Path, slug: str, fallback: str) -> str:
    config = load_yaml(repo_root / "routines" / slug / "config.yaml", {})
    return str(config.get("name") or fallback)


def parse_comms_summary(summary: str | None) -> tuple[int, int, int]:
    if not summary:
        return 0, 0, 0
    match = COMMS_SUMMARY_RE.search(summary)
    if not match:
        return 0, 0, 0

    critical_count = int(match.group("critical"))
    important_count = int(match.group("important"))

    actionable_important_count = important_count
    lowered_summary = summary.lower()
    if "already tracked" in lowered_summary:
        actionable_important_count = 0
    else:
        added_match = COMMS_ADDED_RE.search(summary)
        if added_match:
            actionable_important_count = int(added_match.group("added"))

    return critical_count, important_count, actionable_important_count


def active_scheduler_warning(recent_log: list[dict[str, str]]) -> str | None:
    latest_warning: tuple[datetime, str] | None = None
    latest_success: datetime | None = None

    for entry in recent_log:
        message = entry.get("message", "")
        timestamp = parse_iso(entry.get("timestamp"))
        if timestamp is None:
            continue

        is_warning = "WARN" in message or "ERROR" in message or "failed" in message.lower()
        if is_warning:
            if latest_warning is None or timestamp >= latest_warning[0]:
                latest_warning = (timestamp, message)
            continue

        if latest_success is None or timestamp >= latest_success:
            latest_success = timestamp

    if latest_warning is None:
        return None
    if latest_success is not None and latest_success >= latest_warning[0]:
        return None
    return latest_warning[1]


def parse_meeting_action_count(summary: str | None) -> int:
    if not summary:
        return 0
    match = MEETING_ACTIONS_RE.search(summary)
    if not match:
        return 0
    return int(match.group("count"))


def build_comms_routine_card(repo_root: Path, comms_source: dict[str, Any], pending_notifications: int) -> dict[str, Any]:
    updated_at = parse_iso(comms_source.get("updated_at"))
    age_minutes = None
    if updated_at is not None:
        age_minutes = int((datetime.now(timezone.utc) - updated_at).total_seconds() // 60)

    inbox_monitor = next((item for item in comms_source.get("items", []) if item.get("collector") == "inbox-monitor"), None)
    latest_summary = ""
    if inbox_monitor:
        summaries = inbox_monitor.get("recent_summaries") or []
        latest_summary = summaries[-1] if summaries else ""

    critical_count, important_count, actionable_important_count = parse_comms_summary(latest_summary)
    stale = age_minutes is None or age_minutes > 180

    collector_status = comms_source.get("collector_status", "unknown")
    degraded_collectors = [
        c.get("collector") for c in comms_source.get("collector_health", []) or []
        if c.get("status") == "degraded"
    ]

    status = "reference"
    priority = "medium"
    score = 8
    title = "No important comms are queued right now"
    why_now = ["background inbox monitor already filtered low-signal noise"]

    if collector_status == "degraded":
        # Treat collector failure as a hard signal: do NOT trust a quiet snapshot.
        status = "needs review"
        priority = "high"
        score = 75 + (5 * len(degraded_collectors))
        title = f"Collector{'s' if len(degraded_collectors) != 1 else ''} degraded: {', '.join(degraded_collectors) or 'unknown'}"
        why_now = [
            "comms collectors logged errors on their last run — the candidate queue may be silently empty",
            "fix the collector before triaging or you'll miss real inbound items",
        ]
    elif critical_count or pending_notifications:
        status = "needs action"
        priority = "high"
        score = 86 + (critical_count * 12) + (actionable_important_count * 4) + (pending_notifications * 10)

        parts = []
        if critical_count:
            parts.append(f"{critical_count} critical")
        if actionable_important_count:
            parts.append(f"{actionable_important_count} important")
        elif important_count:
            parts.append(f"{important_count} important")
        if pending_notifications:
            parts.append(f"{pending_notifications} pending notification{'s' if pending_notifications != 1 else ''}")
        title = f"Triage {' + '.join(parts)} and clear the highest-signal inbox items"
        why_now = [f"collector summary: {latest_summary or 'important comms waiting'}"]
        if pending_notifications:
            why_now.append("pending notifications are waiting for human review")
        why_now.append("collector-first triage should happen before a full inbox sweep")
    elif actionable_important_count:
        status = "needs action"
        priority = "medium"
        score = 58 + (actionable_important_count * 6)
        title = f"Triage {actionable_important_count} important inbox item{'s' if actionable_important_count != 1 else ''}"
        why_now = [f"collector summary: {latest_summary or 'important comms waiting'}", "important inbox items are waiting, but nothing looks same-hour critical"]
    elif stale:
        status = "needs review"
        score = 42
        title = "Refresh comms state and verify that no important inbox items are waiting"
        why_now = ["comms snapshot is stale or missing", "routine should verify collector health before trusting a quiet inbox"]

    return {
        "workspace": "comms-triage",
        "display_name": routine_display_name(repo_root, "comms-triage", "Comms Triage"),
        "title": title,
        "priority": priority,
        "status": status,
        "status_label": format_status_label(status),
        "estimate_min": 15 if status == "needs action" else 10,
        "last_worked_on": updated_at.date().isoformat() if updated_at else None,
        "score": score,
        "why_now": why_now,
        "matched_todos": [],
        "requires_user": True,
        "config_path": "routines/comms-triage/config.yaml",
        "context_path": "routines/comms-triage/CONTEXT.md",
        "kind": "routine",
    }


def routine_cards_for(repo_root: Path, derived: dict[str, Any], pending_notifications: int) -> list[tuple[dict[str, Any], Any]]:
    """Queue cards for the ops-loop routines; no Comms Triage card where it is off on this machine."""
    cards = []
    if comms_triage_setting.enabled():
        cards.append((build_comms_routine_card(repo_root, derived["comms"], pending_notifications), derived["comms"].get("updated_at")))
    cards.append((build_meetings_routine_card(repo_root, derived["meetings"]), derived["meetings"].get("updated_at")))
    return cards


def build_meetings_routine_card(repo_root: Path, meetings_source: dict[str, Any]) -> dict[str, Any]:
    updated_at = parse_iso(meetings_source.get("updated_at"))
    age_minutes = None
    if updated_at is not None:
        age_minutes = int((datetime.now(timezone.utc) - updated_at).total_seconds() // 60)

    scheduler = next((item for item in meetings_source.get("items", []) if item.get("collector") == "meeting-scheduler"), None)
    scheduled_count = len((scheduler or {}).get("scheduled_event_ids") or [])
    recent_log = (scheduler or {}).get("recent_log") or []
    active_warning_message = active_scheduler_warning(recent_log)

    ingests = [item for item in meetings_source.get("items", []) if item.get("collector") == "post-ingest"]
    ingest_count = len(ingests)
    action_count = sum(parse_meeting_action_count(item.get("summary")) for item in ingests)
    # A source with no timestamp and no items never had a collector: Windows registers no
    # meeting scheduler or post-ingest task, so the card must not ask the owner to verify
    # automation that does not exist. A collector that went stale still shows below. Keeping
    # automation healthy is PM OS's job, so neither case sets requires_user.
    never_collected = updated_at is None and not meetings_source.get("items")
    stale = not never_collected and (age_minutes is None or age_minutes > 240)

    status = "reference"
    priority = "medium"
    score = 8
    title = "No urgent meeting prep or follow-up is queued right now"
    why_now = ["no meeting collector runs on this install"] if never_collected else ["meeting automation is quiet"]
    meeting_work = bool(active_warning_message or scheduled_count or ingest_count)

    if active_warning_message:
        status = "needs action"
        priority = "high"
        score = 88 + min(action_count, 6) * 3
        title = "Recover calendar fetch and rehydrate the meeting prep queue"
        why_now = [active_warning_message, "meeting automation cannot be trusted until calendar intake is healthy"]
        if action_count:
            title += f", then close follow-ups from {action_count} captured action item{'s' if action_count != 1 else ''}"
            why_now.append(f"recent post-ingests already captured {action_count} action item{'s' if action_count != 1 else ''} for the owner")
    elif scheduled_count:
        status = "needs action"
        score = 76 + min(scheduled_count, 5) * 4
        title = f"Prep {scheduled_count} scheduled meeting{'s' if scheduled_count != 1 else ''} and review attendee context"
        why_now = [f"scheduler has {scheduled_count} meeting{'s' if scheduled_count != 1 else ''} queued", "pre-brief work is highest leverage before the next meeting block"]
    elif action_count:
        status = "needs action"
        priority = "medium"
        score = 52 + min(action_count, 6) * 3
        title = f"Review follow-ups from {ingest_count} recent meeting ingest{'s' if ingest_count != 1 else ''} ({action_count} actions for the owner)"
        why_now = [f"recent post-ingests captured {action_count} action item{'s' if action_count != 1 else ''}", "meeting follow-through should be routed before it decays into inbox noise"]
    elif ingest_count:
        status = "needs review"
        score = 44
        title = f"Route decisions from {ingest_count} recent meeting ingest{'s' if ingest_count != 1 else ''} into the right workstreams"
        why_now = ["meeting notes landed recently", "durable decisions should move into project context instead of staying in the notes queue"]
    elif stale:
        status = "needs review"
        score = 40
        title = "Refresh meeting state and verify scheduler plus post-ingest health"
        why_now = ["meetings snapshot is stale or missing", "routine should verify automation health before assuming the meeting queue is empty"]

    return {
        "workspace": "meetings",
        "display_name": routine_display_name(repo_root, "meetings", "Meetings"),
        "title": title,
        "priority": priority,
        "status": status,
        "status_label": format_status_label(status),
        "estimate_min": 15 if status == "needs action" else 10,
        "last_worked_on": updated_at.date().isoformat() if updated_at else None,
        "score": score,
        "why_now": why_now,
        "matched_todos": [],
        "requires_user": meeting_work,
        "config_path": "routines/meetings/config.yaml",
        "context_path": "routines/meetings/CONTEXT.md",
        "kind": "routine",
    }


def routine_update_is_fresh(update: dict[str, Any] | None, source_updated_at: str | None) -> bool:
    if not update:
        return False

    update_timestamp = parse_iso(update.get("timestamp"))
    if update_timestamp is None:
        return False

    source_timestamp = parse_iso(source_updated_at)
    if source_timestamp is None:
        return True

    return update_timestamp >= source_timestamp


def apply_workspace_update(card: dict[str, Any], update: dict[str, Any], paused_until: str | None = None) -> dict[str, Any]:
    updated_card = dict(card)

    if update.get("status"):
        updated_card["status"] = update["status"]
        updated_card["status_label"] = format_status_label(update["status"], paused_until)

    if update.get("recommended_next_action"):
        updated_card["title"] = update["recommended_next_action"]

    if update.get("review_url"):
        # Live review surface (Google Doc / hosted page). The only review pointer
        # rendered to the owner — local paths never reach the queue card.
        updated_card["review_url"] = update["review_url"]

    return updated_card


def append_update_actions(queue_cards: list[dict[str, Any]], card: dict[str, Any], update: dict[str, Any]) -> None:
    for index, new_action in enumerate(update.get("new_actions") or []):
        action_priority = str(new_action.get("priority", card["priority"]))
        action_priority = action_priority.lower()
        action_bonus = {"high": 18, "medium": 10, "low": 4}.get(action_priority, 0)
        queue_cards.append(
            {
                "workspace": card["workspace"],
                "display_name": card["display_name"],
                "title": new_action.get("title") or card["title"],
                "priority": action_priority,
                "status": card["status"],
                "status_label": card["status_label"],
                "estimate_min": int(new_action.get("estimate_min") or card["estimate_min"]),
                "last_worked_on": card.get("last_worked_on"),
                "score": card["score"] + action_bonus,
                "why_now": card["why_now"] + ["fresh workspace follow-up action"],
                "matched_todos": card.get("matched_todos", []),
                "requires_user": infer_requires_user(
                    str(new_action.get("title") or card["title"]),
                    card.get("matched_todos", []),
                    kind="new_action",
                    workspace=card["workspace"],
                )
                if new_action.get("requires_user") is None
                else bool(new_action.get("requires_user")),
                "config_path": card.get("config_path"),
                "context_path": card.get("context_path"),
                "update_source": update.get("source"),
                "kind": "new_action",
                "action_index": index,
            }
        )


def score_workstream(
    item: dict[str, Any],
    todo_matches: list[dict[str, Any]],
    comms_matches: list[dict[str, Any]],
    update: dict[str, Any] | None,
    calendar_context: dict[str, Any],
    current_workspace: str | None,
) -> tuple[int, list[str]]:
    score = PRIORITY_SCORE.get(item["priority"], 30) + STATUS_SCORE.get(item["status"], 0) + int(item.get("queue_score_bonus", 0))
    reasons: list[str] = []

    if item["priority"] == "high":
        reasons.append("high-priority workstream")

    if item.get("queue_score_bonus"):
        reasons.append("strategic queue boost")

    if item["status"] in {"needs action", "needs review", "to review", "needs follow-up"}:
        reasons.append(item["status_label"].lower())

    if todo_matches:
        top_todo = todo_matches[0]
        todo_boost = {"P0": 32, "P1": 24, "P2": 16, "P3": 8}.get(top_todo["priority"], 4)
        score += todo_boost
        reasons.append(f"matches open {top_todo['priority']} todo")

    if comms_matches:
        top_signal = comms_matches[0]
        signal_type = str(top_signal.get("source_type") or "comms").lower()
        score += 10
        reasons.append(f"related unread {signal_type} signal")
        signal_date = parse_iso(top_signal.get("received_at"))
        if signal_date is not None and signal_date.astimezone(LOCAL_TIMEZONE).date() == datetime.now(LOCAL_TIMEZONE).date():
            score += 6
            reasons.append(f"same-day {signal_type} activity")

    last_worked = item.get("last_worked_on")
    if last_worked:
        try:
            delta_days = (datetime.now().date() - datetime.strptime(last_worked, "%Y-%m-%d").date()).days
            if item["status"] not in TERMINAL_STATUSES and delta_days >= item["stale_after_days"]:
                score += 10
                reasons.append("stale enough to revisit")
            elif delta_days <= 1:
                score += 6
                reasons.append("recently active")
        except ValueError:
            pass

    if item.get("paused_until"):
        try:
            paused_until = datetime.fromisoformat(item["paused_until"]).date()
            if paused_until > datetime.now().date():
                score -= 25
                reasons.append(f"paused until {paused_until.isoformat()}")
            elif item["status"] == "paused":
                score += 18
                reasons.append(f"resume date reached ({paused_until.isoformat()})")
        except ValueError:
            pass

    if update:
        update_status = update.get("status")
        recommended = update.get("recommended_next_action")
        if update_status and update_status != item["status"]:
            score += STATUS_SCORE.get(update_status, 0) - STATUS_SCORE.get(item["status"], 0)
        if recommended:
            # Only call an update "fresh" when it actually is: a June update
            # must not keep earning a freshness bonus in August. Missing or
            # unparseable timestamps get no bonus (fail closed).
            update_ts = parse_iso(update.get("timestamp"))
            if update_ts is not None and datetime.now(timezone.utc) - update_ts <= timedelta(days=7):
                score += 8
                reasons.append("fresh workspace update available")

    if current_workspace and item["slug"] == current_workspace:
        score += 8
        reasons.append("avoid context switch")
    elif current_workspace and current_workspace not in {"comms-triage", "meetings"}:
        score -= 6
        reasons.append("context switch penalty")

    available_focus_minutes = calendar_context.get("available_focus_minutes")
    next_event_minutes = calendar_context.get("minutes_until_next_event")
    if available_focus_minutes is not None:
        if available_focus_minutes > 0 and item["estimate_min"] <= available_focus_minutes:
            score += 7
            reasons.append(f"fits {available_focus_minutes}m calendar window")
        elif next_event_minutes is not None and next_event_minutes <= 90 and item["estimate_min"] > available_focus_minutes:
            score -= 10
            reasons.append(f"won't fit before next meeting in {next_event_minutes}m")

    matching_event = best_matching_calendar_event(item, todo_matches, calendar_context)
    if matching_event is not None:
        minutes_until = max(0, int((matching_event["start"] - datetime.now(LOCAL_TIMEZONE)).total_seconds() // 60))
        if minutes_until <= 180:
            score += 12 if minutes_until <= 60 else 8
            reasons.append(f"related meeting '{matching_event['summary']}' starts in {minutes_until}m")

    if not reasons:
        reasons.append("best available actionable workstream")

    return score, reasons


def build_queue(repo_root: Path, state_dir: Path, todo_path: Path, *, persist_sources: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    derived = derive_sources(repo_root, state_dir, todo_path, persist=persist_sources)
    calendar_context = build_calendar_context()
    previous_now = load_json(state_dir / "now.json", {})
    current_workspace = ((previous_now.get("now") or {}).get("workspace") or None)

    pending_notifications = 0
    pending_notifications_file = repo_root / ".state" / "pending-amp-notifications.jsonl"
    if pending_notifications_file.exists():
        pending_notifications = len([line for line in pending_notifications_file.read_text().splitlines() if line.strip()])

    queue_cards = []
    todo_candidates_by_id = {
        str(todo.get("id") or f"anonymous:{index}"): todo
        for index, todo in enumerate([*derived["todos"], *derived["launch_todos"]])
    }
    todo_candidates = list(todo_candidates_by_id.values())
    for item in derived["workstreams"]:
        if item["slug"] == "meetings":
            continue

        update = derived["updates"].get(f"workstream:{item['slug']}") or derived["updates"].get(f"routine:{item['slug']}")
        if update:
            item = dict(item)
            if update.get("status"):
                item["status"] = update["status"]
            if update.get("recommended_next_action") and not is_scaffold_next_step(update["recommended_next_action"]):
                item["next_step"] = update["recommended_next_action"]
                item["has_open_step"] = True
            item["status_label"] = format_status_label(item["status"], item.get("paused_until"))

        todo_matches = [todo for todo in todo_candidates if todo_matches_workstream(todo, item)]
        # Sort so the boost (and matched_todos[0]) reflects the highest-priority
        # match, not source order — a P3 match must not mask a P0.
        todo_matches.sort(key=todo_sort_key)
        comms_matches = [candidate for candidate in derived["comms"].get("candidates", []) if comms_candidate_matches_workstream(candidate, item)]
        comms_matches.sort(key=lambda candidate: candidate.get("received_at") or "", reverse=True)
        score, reasons = score_workstream(item, todo_matches, comms_matches, update, calendar_context, current_workspace)

        # A workstream with no written step and no open to-do has nothing to rank: it stays in
        # the queue, at the bottom, and never becomes the now card. Its open to-do is its step
        # when it has one, and a matching message still ranks it.
        if not item.get("has_open_step", True) and todo_matches and todo_matches[0].get("title"):
            item = dict(item, next_step=todo_matches[0]["title"], has_open_step=True)
        no_open_step = not item.get("has_open_step", True) and not comms_matches
        if no_open_step:
            score = min(score, 8)
            reasons = ["no open step in CONTEXT.md"]

        card = {
            "workspace": item["slug"],
            "display_name": item["display_name"],
            "title": item["next_step"],
            "priority": item["priority"],
            "status": item["status"],
            "status_label": item["status_label"],
            "estimate_min": item["estimate_min"],
            "last_worked_on": item["last_worked_on"],
            "score": score,
            "why_now": reasons,
            "matched_todos": todo_matches[:5],
            "matched_comms": comms_matches[:5],
            "requires_user": infer_requires_user(item["next_step"], todo_matches, workspace=item["slug"]),
            "config_path": item.get("config_path"),
            "context_path": item.get("context_path"),
        }
        if no_open_step:
            card["no_open_step"] = True
            card["requires_user"] = False
        if update and update.get("review_url"):
            card["review_url"] = update["review_url"]
        queue_cards.append(card)

        if update:
            append_update_actions(queue_cards, card, update)

    for card, source_updated_at in routine_cards_for(repo_root, derived, pending_notifications):
        update = derived["updates"].get(f"routine:{card['workspace']}")
        if routine_update_is_fresh(update, source_updated_at):
            card = apply_workspace_update(card, update)
        queue_cards.append(card)

        if routine_update_is_fresh(update, source_updated_at):
            append_update_actions(queue_cards, card, update)

    queue_cards.sort(key=lambda card: (-card["score"], card["display_name"].lower()))

    actionable = [
        card
        for card in queue_cards
        if card["status"] not in TERMINAL_STATUSES and card["status"] not in {"reference"} and not card.get("no_open_step")
    ]
    now_card = actionable[0] if actionable else None
    fallback_cards = actionable[1:4]

    attention = "low"
    if pending_notifications > 0 or (now_card and now_card["priority"] == "high"):
        attention = "high"
    elif now_card:
        attention = "medium"

    knowledge_health = {
        k: derived.get("knowledge", {}).get(k)
        for k in ("status", "day", "records_total",
                  "missing_source_classes", "stale_source_classes")
    }
    queue_payload = {
        "generated_at": now_iso(),
        "knowledge": knowledge_health,
        "queue": queue_cards,
    }
    now_payload = {
        "generated_at": now_iso(),
        "attention": attention,
        "queue_size": len(actionable),
        "pending_notifications": pending_notifications,
        "knowledge": knowledge_health,
        "now": None,
        "fallbacks": [],
    }

    if now_card:
        now_payload["now"] = {
            "title": now_card["title"],
            "workspace": now_card["workspace"],
            "display_name": now_card["display_name"],
            "estimate_min": now_card["estimate_min"],
            "why_now": "; ".join(now_card["why_now"][:3]),
            "status": now_card["status_label"],
            "requires_user": now_card.get("requires_user", False),
        }
        now_payload["fallbacks"] = [
            {
                "title": card["title"],
                "workspace": card["workspace"],
                "estimate_min": card["estimate_min"],
                "status": card["status_label"],
                "requires_user": card.get("requires_user", False),
            }
            for card in fallback_cards
        ]

    return queue_payload, now_payload


def append_event(state_dir: Path, source: str, queue_payload: dict[str, Any], now_payload: dict[str, Any]) -> None:
    event = {
        "timestamp": now_iso(),
        "source": source,
        "type": "queue_refreshed",
        "queue_size": now_payload.get("queue_size", 0),
        "attention": now_payload.get("attention"),
        "now_workspace": (now_payload.get("now") or {}).get("workspace"),
        "top_queue": [card["workspace"] for card in queue_payload.get("queue", [])[:3]],
    }
    # Locked append coordinates with the rotate-logs trim (events_stream.py).
    append_event_line(state_dir, event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write state files")
    parser.add_argument("--event-source", default="manual")
    parser.add_argument("--todo-path", type=Path, default=Path(os.environ.get("PM_OS_ROOT") or Path.home() / "Development" / "pm-os") / "system" / "state" / "todo.json")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    state_dir = repo_root / "system" / "state"

    if args.write:
        state_dir.mkdir(parents=True, exist_ok=True)
        # Serialize concurrent rebuilds (emit-workspace-update, collectors,
        # start-day can overlap). Without this, an older build finishing last
        # can overwrite a newer queue.json/now.json. The lock covers build +
        # write so the published view always reflects the newest inputs.
        lock_path = state_dir / ".rebuild.lock"
        with lock_path.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            queue_payload, now_payload = build_queue(repo_root, state_dir, args.todo_path)
            write_json_file(state_dir / "queue.json", queue_payload)
            write_json_file(state_dir / "now.json", now_payload)
            append_event(state_dir, args.event_source, queue_payload, now_payload)
    else:
        queue_payload, now_payload = build_queue(repo_root, state_dir, args.todo_path, persist_sources=False)
        print(json.dumps({"queue": queue_payload, "now": now_payload}, indent=2))


if __name__ == "__main__":
    main()
