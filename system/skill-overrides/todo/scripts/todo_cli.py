#!/usr/bin/env python3
"""Persistent to-do list for one person on one machine.

The file works two ways. As a command-line tool it stores and changes to-dos,
shows verified views of them, reconciles them against completion evidence, and
keeps private calendar holds for P0 and P1 work. As an importable module it
exposes the same building blocks to other scripts and tests.

Importing has no side effects beyond reading the environment and resolving
default paths. The command-line entry point runs only under the __main__ guard.

Every name that tests patch is looked up as a module global at call time, so
internal code never captures those names in default arguments or closures.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

try:
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - only when the tz database is missing
    PACIFIC = timezone(timedelta(hours=-8), "PST")

PACIFIC_NAME = "America/Los_Angeles"


# ---------------------------------------------------------------------------
# Paths and module-level defaults
# ---------------------------------------------------------------------------


def _path_from_env(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else fallback


REPO_ROOT = _path_from_env("SWITCHBOARD_ROOT", Path.home() / "Development" / "switchboard")
STATE_DIR = REPO_ROOT / "system" / "state"
SOURCES_DIR = STATE_DIR / "sources"
SCRIPTS_DIR = REPO_ROOT / "system" / "scripts"
WORKSTREAMS_DIR = REPO_ROOT / "workstreams"

TODO_PATH = STATE_DIR / "todo.json"
TODO_CALENDAR_STATE_PATH = _path_from_env("SWITCHBOARD_TODO_CALENDAR_STATE", STATE_DIR / "todo-calendar.json")
TODO_RESOLUTION_FLAGS_PATH = _path_from_env("SWITCHBOARD_TODO_RESOLUTION_FLAGS", STATE_DIR / "todo-resolution-flags.json")
CONNECTORS_DIR = _path_from_env("SWITCHBOARD_CONNECTORS_DIR", Path.home() / ".switchboard" / "connectors")

MENTIONS_CACHE_PATH = SOURCES_DIR / "owner-mentions.jsonl"
CHANNEL_SIGNALS_CACHE_PATH = SOURCES_DIR / "owner-channel-signals.jsonl"
LINEAR_INBOX_CACHE_PATH = SOURCES_DIR / "owner-linear-inbox.jsonl"
GITHUB_INBOX_CACHE_PATH = SOURCES_DIR / "owner-github-inbox.jsonl"
GMAIL_SENT_CACHE_PATH = SOURCES_DIR / "owner-gmail-sent.jsonl"
DRIVE_ACTIVITY_CACHE_PATH = SOURCES_DIR / "owner-drive-activity.jsonl"
NOTION_ACTIVITY_CACHE_PATH = SOURCES_DIR / "owner-notion-activity.jsonl"
BLUEPRINT_UPDATES_CACHE_PATH = SOURCES_DIR / "owner-blueprint-updates.jsonl"
JIRA_CACHE_PATH = SOURCES_DIR / "owner-jira-issues.jsonl"
WORKSPACE_UPDATES_PATH = SOURCES_DIR / "workspace-updates.json"
DOC_COMMENTS_CACHE_PATH = SOURCES_DIR / "owner-doc-comments.jsonl"
DOC_COMMENT_TRIGGERS_PATH = SOURCES_DIR / "doc-comment-gmail-triggers.json"
QUEUE_PATH = STATE_DIR / "queue.json"
SIDEBAR_SNAPSHOT_PATH = STATE_DIR / "sidebar-arrangement.snapshot"

# Process-wide surface-gate state. Tests clear these between runs.
SURFACE_CONFIRMATIONS_BY_ID: dict[str, list[dict[str, Any]]] = {}
SURFACE_WOULD_CLOSE_BY_ID: dict[str, str] = {}
SURFACE_GATE_RESULT: dict[str, Any] = {}

# Name-helper cache: None means not tried yet, False means tried and unavailable.
_NAMED_REFS_MODULE: Any = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
DEFAULT_DAILY_CAP = 5
DEFAULT_ESTIMATE_MIN = 15
DEFAULT_LOOKBACK_DAYS = 14
EXTERNAL_TIMEOUT_SECONDS = 45
HELPER_TIMEOUT_SECONDS = 180

TODO_CALENDAR_SUMMARY = "To-dos (switchboard)"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
CALENDAR_FULL_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
HOLD_PREFIX = "[Todo] "
HOLD_DAY_START = (13, 0)
HOLD_DAY_END = (17, 0)
HOLD_STEP = timedelta(minutes=15)
HOLD_SEARCH_DAYS = 10
HOLD_COLORS = {"P0": "11", "P1": "5"}
HOLD_FALLBACK_COLOR = "7"

CONFIRM_COMMAND_TEMPLATE = "python3 ~/.agents/skills/todo/scripts/todo_cli.py complete --id {id}"

SURFACE_COMMANDS = frozenset(
    {"summary", "list", "snapshot", "render-markdown", "reminders", "suggest-routes", "checkpoint"}
)

CALENDAR_EVENT_FIELDS = ("calendar_event_id", "calendar_event_url", "calendar_event_start", "calendar_event_end")
CALENDAR_FIELDS = CALENDAR_EVENT_FIELDS + ("calendar_synced_at",)

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
DUE_STATE_RANK = {"overdue": 0, "today": 1, "upcoming": 2, "unscheduled": 3}
ROUTING_RANK = {"workstream": 0, "project": 1, "due": 2, "review_on": 2, "unrouted": 3}

STATUS_SYNONYMS = {
    "done": "done", "completed": "done", "complete": "done", "closed": "done", "cancelled": "done",
    "canceled": "done", "resolved": "done", "dropped": "done",
    "waiting": "waiting", "blocked": "waiting", "needs-response": "waiting", "needs-follow-up": "waiting",
    "scheduled": "scheduled", "snoozed": "scheduled", "deferred": "scheduled",
    "someday": "someday", "backlog": "someday", "maybe": "someday", "parking-lot": "someday",
    "open": "open", "actionable": "open", "in-progress": "open", "active": "open", "new": "open",
}

STEM_STOPWORDS = frozenset(
    "a an and check follow for from in of on reply respond review re send the to up with".split()
)
GENERIC_TERMS = frozenset(
    "amp and doc meeting notes plan pm project review strategy task todo work workstream".split()
)
EXTRA_NOISE_TERMS = frozenset("comment comments docs issue items linear next note prd steps ticket".split())

USER_ACTION_PHRASES = (
    "acknowledge", "align with", "ask", "bring up", "call", "chat with", "confirm", "coordinate with",
    "decide with", "discuss", "follow up with", "meet with", "ping", "present", "raise", "reply to",
    "respond to", "send to", "share with", "sync with", "talk to",
)
MESSAGE_REPLY_PREFIXES = ("reply to", "respond to", "follow up", "follow-up", "confirm", "send")
MEETING_PREFIXES = (
    "ask ", "bring ", "bring up", "chat with", "discuss", "mention", "present", "raise", "share",
    "standup", "sync with", "talk to",
)
TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "user", "human", "requires-user", "requires_user"})
FALSE_TOKENS = frozenset({"0", "false", "no", "n", "agent", "agent-executable", "agent_executable"})

ARTIFACT_WORD_RE = re.compile(
    r"\b(brief|deck|draft|memo|outline|plan|prd|proposal|rfc|spec|strategy|writeup)\b", re.IGNORECASE
)
REVIEW_PREFIX_RE = re.compile(r"^\s*(ack|audit|check|look at|look over|read|review|revisit|scan|skim|validate)\b", re.I)
SEND_WORD_RE = re.compile(r"\b(send|email|post|share|announce|publish|ship|broadcast|notify|dm)\b", re.I)
CLOSE_OUT_PREFIX_RE = re.compile(r"^\s*(continue|wrap up|close out|finish)\s+", re.I)
DONE_WORDS = frozenset({"done", "completed", "complete", "cancelled", "canceled", "closed", "resolved"})

SLACK_REF_RE = re.compile(r"https?://[^/\s]*slack\.com/archives/([A-Z0-9]+)/p(\d{7,})", re.I)
MPIM_REF_RE = re.compile(r"^\s*MPIM\s+(\S+)\s+\S+\s+(\d+(?:\.\d+)?)\s*$", re.I)
MAIL_WEB_RE = re.compile(r"https?://mail\.google\.com/[^\s#]*#(?:[^/\s]+/)+([0-9a-fA-F]{10,})\b")
GDOC_RE = re.compile(
    r"https?://docs\.google\.com/(document|spreadsheets|presentation|forms)/(?:u/\d+/)?d/(?:e/)?([A-Za-z0-9_-]+)",
    re.I,
)
DISCO_RE = re.compile(r"[?&]disco=([^&#?\s]+)")
LINEAR_URL_RE = re.compile(r"https?://linear\.app/[^/\s]+/issue/([A-Z][A-Z0-9]{0,9}-\d+)", re.I)
LINEAR_BARE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")
LINEAR_KEY_RE = re.compile(r"(?<![\w/-])([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
LINEAR_KEY_SKIP = frozenset({"UTC", "GMT", "ISO", "RFC", "SHA", "HTTP", "PT", "UTF", "CVE", "COVID", "P"})
GITHUB_REF_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)", re.I)
NOTION_RE = re.compile(r"https?://(?:www\.)?notion\.(?:so|site)/\S*?([0-9a-f]{32})", re.I)
JIRA_RE = re.compile(r"https?://[^/\s]*atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)", re.I)

PR_TRIGGER_RE = re.compile(
    r"\b(superseded by|duplicate of|clean todo|merged into|folded into|dup of|covered by)\b", re.I
)
PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/([^/\s]+)/pull/(\d+)", re.I)
PR_LABEL_RE = re.compile(r"(?<![\w.-])([A-Za-z][\w.-]*)#(\d{3,7})\b")
PR_BARE_RE = re.compile(r"(?<![\w/])#(\d{4,7})\b")
ID_TOKEN_RE = re.compile(
    r"comms-[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{8}\b"
)


class TodoError(Exception):
    """A handled failure: the CLI prints the message and exits 1."""


class SandboxRefused(TodoError):
    """A test-sandboxed run tried to write outside the temp directory."""


class PrSetRefusal(Exception):
    """The PR-set guard refused a close: the CLI prints the payload and exits 3."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("reason", "PR-set mismatch"))
        self.payload = payload


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPACT_OFFSET_RE = re.compile(r"(T.*[+-])(\d{2})(\d{2})$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_z() -> str:
    return iso_z(utc_now())


def today_pacific() -> date:
    return utc_now().astimezone(PACIFIC).date()


def parse_moment(value: Any) -> datetime | None:
    """Read a timestamp. A date alone is midnight UTC, and a naive time is UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if _DATE_ONLY_RE.match(text):
            try:
                day = date.fromisoformat(text)
            except ValueError:
                return None
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        candidate = text
        if re.match(r"^\d{4}-\d{2}-\d{2} \d", candidate):
            candidate = candidate.replace(" ", "T", 1)
        if candidate[-1:] in ("Z", "z"):
            candidate = candidate[:-1] + "+00:00"
        candidate = _COMPACT_OFFSET_RE.sub(r"\1\2:\3", candidate)
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError:
            moment = _parse_mail_date(text)
            if moment is None:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _parse_mail_date(text: str) -> datetime | None:
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def pacific_date(value: Any) -> date | None:
    """A plain date stays as it is. A timestamp becomes its Pacific calendar date."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(PACIFIC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if _DATE_ONLY_RE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    moment = parse_moment(text)
    return moment.astimezone(PACIFIC).date() if moment else None


def _moment_or(value: Any, fallback: datetime) -> datetime:
    moment = parse_moment(value)
    return moment if moment is not None else fallback


def created_moment(item: dict[str, Any], default_days: int = 30) -> datetime:
    """When the item was created, or a stand-in some days ago when unknown."""
    return _moment_or(item.get("created_at"), utc_now() - timedelta(days=default_days))


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_priority(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "P3"
    if isinstance(value, int):
        label = f"P{value}"
    else:
        text = str(value).strip().upper()
        if not text:
            return "P3"
        legacy = {"0": "P0", "1": "P0", "2": "P1", "3": "P2", "4": "P3"}
        if text in legacy:
            return legacy[text]
        label = text if text.startswith("P") else f"P{text}"
    return "P3" if label == "P4" else label


def normalize_status(value: Any) -> str:
    if value is None:
        return "open"
    text = str(value).strip().lower()
    if not text:
        return "open"
    return STATUS_SYNONYMS.get(text, text)


def source_type(value: Any) -> str:
    if value is None:
        return "unknown"
    label = str(value).strip().lower()
    if not label:
        return "unknown"
    if "gmail" in label or label == "mail":
        return "gmail"
    for needle, family in (("slack", "slack"), ("linear", "linear")):
        if needle in label:
            return family
    if "calendar" in label or "meeting" in label:
        return "meeting"
    for needle, family in (("notion", "notion"), ("figma", "figma"), ("doc", "document")):
        if needle in label:
            return family
    return label


def parse_requires_user(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    return None


def infer_requires_user(item: dict[str, Any]) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("title", "context")).strip().lower()
    if any(phrase in text for phrase in USER_ACTION_PHRASES):
        return True
    family = source_type(item.get("source"))
    if family in ("gmail", "slack") and text.startswith(MESSAGE_REPLY_PREFIXES):
        return True
    if family == "meeting" and text.startswith(MEETING_PREFIXES):
        return True
    return False


def requires_user_value(item: dict[str, Any]) -> bool:
    explicit = parse_requires_user(item.get("requires_user"))
    return explicit if explicit is not None else infer_requires_user(item)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = str(value).strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tokens(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def title_stem(title: Any) -> str:
    words = [word for word in _tokens(title) if word not in STEM_STOPWORDS]
    return " ".join(words)[:120]


def distinctive_terms(*texts: Any) -> list[str]:
    seen: list[str] = []
    for token in _tokens(" ".join(str(text or "") for text in texts)):
        if len(token) < 3 or token in seen:
            continue
        if token in STEM_STOPWORDS or token in GENERIC_TERMS or token in EXTRA_NOISE_TERMS:
            continue
        seen.append(token)
    return seen


# ---------------------------------------------------------------------------
# Source references
# ---------------------------------------------------------------------------


def canonicalize_source_ref(ref: Any) -> str:
    text = str(ref or "").strip()
    if not text:
        return ""
    if text.lower().startswith("msg:"):
        return text.lower()
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    mail = MAIL_WEB_RE.search(text)
    if mail:
        return f"msg:{mail.group(1).lower()}"
    doc = GDOC_RE.match(text)
    if doc:
        base = f"https://docs.google.com/{doc.group(1).lower()}/d/{doc.group(2)}"
        disco = DISCO_RE.search(text)
        return f"{base}/edit?disco={disco.group(1)}" if disco else base
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def classify_source_ref(ref: Any) -> str | None:
    text = str(ref or "").strip()
    if not text:
        return None
    if text.lower().startswith("msg:"):
        return "gmail"
    if SLACK_REF_RE.search(text):
        return "slack"
    if MAIL_WEB_RE.search(text):
        return "gmail"
    if GDOC_RE.search(text):
        return "gdoc"
    if LINEAR_URL_RE.search(text) or LINEAR_BARE_RE.match(text):
        return "linear"
    if GITHUB_REF_RE.search(text):
        return "github"
    if NOTION_RE.search(text):
        return "notion"
    if JIRA_RE.search(text):
        return "jira"
    return None


def extract_gmail_message_id(ref: Any) -> str | None:
    text = str(ref or "").strip()
    if not text:
        return None
    if text.lower().startswith("msg:"):
        value = text[4:].strip()
        return value or None
    match = MAIL_WEB_RE.search(text)
    return match.group(1) if match else None


def parse_slack_ref(ref: Any) -> dict[str, str] | None:
    """Channel, message ts, and thread ts from a Slack permalink or the legacy MPIM text."""
    text = str(ref or "").strip()
    if not text:
        return None
    match = SLACK_REF_RE.search(text)
    if match:
        digits = match.group(2)
        message_ts = f"{digits[:-6]}.{digits[-6:]}"
        query = parse_qs(urlsplit(text).query)
        thread_ts = (query.get("thread_ts") or [message_ts])[0]
        return {"channel": match.group(1), "ts": message_ts, "thread_ts": thread_ts}
    legacy = MPIM_REF_RE.match(text)
    if legacy:
        return {"channel": legacy.group(1), "ts": legacy.group(2), "thread_ts": legacy.group(2)}
    return None


def google_doc_source_ref(item: dict[str, Any], triggers: dict[str, Any] | None = None) -> str:
    ref = str(item.get("source_ref") or "").strip()
    if GDOC_RE.search(ref):
        return ref
    message_id = extract_gmail_message_id(ref)
    if message_id and triggers:
        entry = triggers.get(message_id) or triggers.get(message_id.lower())
        if isinstance(entry, dict) and entry.get("source_ref"):
            return str(entry["source_ref"])
    return ""


def _doc_ids(doc_ref: str) -> tuple[str | None, str | None]:
    match = GDOC_RE.search(doc_ref or "")
    if not match:
        return None, None
    disco = DISCO_RE.search(doc_ref)
    return match.group(2), (disco.group(1) if disco else None)


def linear_keys(*texts: Any) -> list[str]:
    keys: list[str] = []
    for text in texts:
        raw = str(text or "")
        for match in LINEAR_URL_RE.finditer(raw):
            key = match.group(1).upper()
            if key not in keys:
                keys.append(key)
        for match in LINEAR_KEY_RE.finditer(raw):
            key = match.group(1)
            if key.split("-", 1)[0] in LINEAR_KEY_SKIP or key in keys:
                continue
            keys.append(key)
    return keys


def jira_keys(ref: Any) -> list[str]:
    return [match.group(1).upper() for match in JIRA_RE.finditer(str(ref or ""))]


# ---------------------------------------------------------------------------
# Owner identity (from configuration, never hard-coded)
# ---------------------------------------------------------------------------


def _env_list(name: str) -> set[str]:
    return {part.strip().lower() for part in os.environ.get(name, "").split(",") if part.strip()}


def owner_slack_ids() -> set[str]:
    return _env_list("SWITCHBOARD_OWNER_SLACK_IDS")


def owner_handles() -> set[str]:
    return _env_list("SWITCHBOARD_OWNER_HANDLES")


def _flag_says_me(record: dict[str, Any]) -> bool:
    return any(record.get(key) is True for key in ("is_me", "me", "self", "by_me", "authored_by_me"))


def is_owner_slack_message(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    if _flag_says_me(message):
        return True
    user = message.get("user")
    user_id = message.get("from_user_id") or (user.get("id") if isinstance(user, dict) else user)
    if user_id and str(user_id).lower() in owner_slack_ids():
        return True
    handles = owner_handles()
    names = [message.get("from_handle"), message.get("from")]
    if isinstance(user, dict):
        names.append(user.get("username"))
    return any(name and str(name).strip().lstrip("@").lower() in handles for name in names)


def is_owner_author(author: Any) -> bool:
    if not isinstance(author, dict):
        return False
    if _flag_says_me(author):
        return True
    handles = owner_handles()
    for key in ("displayName", "emailAddress", "email", "name"):
        value = author.get(key)
        if value and str(value).strip().lower() in handles:
            return True
    return False


# ---------------------------------------------------------------------------
# Storage: load, save, lock
# ---------------------------------------------------------------------------


def default_payload() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "rules": {"max_actionable_daily": DEFAULT_DAILY_CAP},
        "recurring_templates": [],
        "items": [],
    }


def load_data(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default_payload()
    except OSError as exc:
        raise TodoError(f"cannot read to-do file {target}: {exc}") from exc
    if not text.strip():
        return default_payload()
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise TodoError(f"to-do file is not valid JSON: {target}: {exc}") from exc
    if isinstance(raw, list):
        raw = {"items": raw}
    if not isinstance(raw, dict):
        raise TodoError(f"to-do file must hold a JSON object: {target}")
    if "items" not in raw and "todos" in raw:
        raw["items"] = raw.pop("todos")
    if not isinstance(raw.get("items"), list):
        raw["items"] = []
    if not isinstance(raw.get("recurring_templates"), list):
        raw["recurring_templates"] = []
    if not isinstance(raw.get("rules"), dict):
        raw["rules"] = {"max_actionable_daily": DEFAULT_DAILY_CAP}
    raw.setdefault("version", SCHEMA_VERSION)
    return raw


# ---------------------------------------------------------------------------
# Test sandbox: under SWITCHBOARD_TEST_SANDBOX=1 every write stays inside the temp
# directory and never lands in a real checkout's system/state, whatever path or
# root a test hands the CLI. An earlier import pinned to the real root once let a
# combined test run write invented to-dos into a real list. Network and helper
# calls are already closed by _network_allowed().
# ---------------------------------------------------------------------------


def test_sandbox_active() -> bool:
    return os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1"


def _under(target: str, directory: str) -> bool:
    return target == directory or target.startswith(directory.rstrip("\\/") + os.sep)


def real_state_dirs() -> list[str]:
    """system/state of the default root, the checkout this file lives in, and its main
    checkout when that is a worktree. Refused under the sandbox even when the temp
    directory is one of their ancestors. The configured root is not on the list: tests
    point it at a temp folder."""
    roots = [Path.home() / "Development" / "switchboard"]
    here = Path(__file__).resolve().parent
    if len(here.parents) > 3:
        roots.append(here.parents[3])  # <root>/system/skill-overrides/todo/scripts, with or without .git
    for parent in (here, *here.parents):
        marker = parent / ".git"
        if marker.is_dir():
            roots.append(parent)
            break
        if marker.is_file():
            roots.append(parent)
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                gitdir = gitdir if gitdir.is_absolute() else parent / gitdir
                if gitdir.parent.name == "worktrees":  # <main>/.git/worktrees/<name>
                    roots.append(gitdir.parent.parent.parent)
            break
    return [os.path.normcase(os.path.realpath(str(root / "system" / "state"))) for root in roots]


def sandbox_allows_path(path: Path | str) -> bool:
    """Outside a test sandbox every path is allowed; inside one, only the temp
    directory, and never a real checkout's system/state."""
    if not test_sandbox_active():
        return True
    try:
        target = os.path.normcase(os.path.realpath(str(path)))
        temp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        if any(_under(target, state) for state in real_state_dirs()):
            return False
    except (OSError, ValueError):
        return False
    return _under(target, temp)


def guard_sandbox_write(path: Path | str) -> None:
    if not sandbox_allows_path(path):
        raise SandboxRefused(
            f"SWITCHBOARD_TEST_SANDBOX=1: refusing to write {path}, which is outside the temp directory. "
            "Point the test's --path, SWITCHBOARD_ROOT and state directory at a temp folder."
        )


def _replace_file(source: str, target: Path) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def atomic_write_text(path: Path | str, text: str, temp_tag: str = "") -> None:
    guard_sandbox_write(path)
    target = Path(os.path.realpath(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.{temp_tag}", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_file(temp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def save_data(path: Path | str, payload: dict[str, Any]) -> None:
    payload["version"] = SCHEMA_VERSION
    if not isinstance(payload.get("rules"), dict):
        payload["rules"] = {"max_actionable_daily": DEFAULT_DAILY_CAP}
    if not isinstance(payload.get("recurring_templates"), list):
        payload["recurring_templates"] = []
    if not isinstance(payload.get("items"), list):
        payload["items"] = []
    atomic_write_text(path, _dump(payload))


class _MsvcrtLock:
    """Last-resort Windows lock on the same far byte the repo helper uses."""

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    _OFFSET = 0x7FFFFFFE

    def flock(self, fd: Any, operation: int) -> None:  # pragma: no cover - Windows without the helper
        import errno
        import msvcrt

        fileno = fd if isinstance(fd, int) else fd.fileno()
        position = os.lseek(fileno, 0, os.SEEK_CUR)
        try:
            os.lseek(fileno, self._OFFSET, os.SEEK_SET)
            with contextlib.suppress(OSError):
                msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(fileno, position, os.SEEK_SET)
        if operation & self.LOCK_UN:
            return
        while True:
            position = os.lseek(fileno, 0, os.SEEK_CUR)
            try:
                os.lseek(fileno, self._OFFSET, os.SEEK_SET)
                msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if operation & self.LOCK_NB:
                    raise BlockingIOError(errno.EAGAIN, "lock is held") from exc
            finally:
                os.lseek(fileno, position, os.SEEK_SET)
            time.sleep(0.05)


_FLOCK_IMPL: Any = None
_LOCKS_HELD = threading.local()


def _flock_impl() -> Any:
    global _FLOCK_IMPL
    if _FLOCK_IMPL is not None:
        return _FLOCK_IMPL
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        try:
            import flock_compat as fcntl  # type: ignore[import-not-found]
        except ImportError:
            fcntl = None
    if fcntl is not None:
        _FLOCK_IMPL = fcntl
        return _FLOCK_IMPL
    folders = [Path(REPO_ROOT) / "system" / "scripts"]
    with contextlib.suppress(IndexError, OSError):
        folders.append(Path(__file__).resolve().parents[3] / "scripts")
    for folder in folders:
        candidate = folder / "flock_compat.py"
        if candidate.is_file():
            module = _load_module_from(candidate, "flock_compat")
            if module is not None:
                sys.modules.setdefault("flock_compat", module)
                _FLOCK_IMPL = module
                return _FLOCK_IMPL
    _FLOCK_IMPL = _MsvcrtLock()
    return _FLOCK_IMPL


@contextlib.contextmanager
def exclusive_lock(path: Path | str):
    """Hold the sidecar lock `<path>.lock` for the duration of the block.

    The lock is re-entrant within one thread, so nested helpers never deadlock
    on a lock their caller already holds.
    """
    lock_path = Path(f"{Path(path)}.lock")
    guard_sandbox_write(lock_path)
    key = os.path.normcase(os.path.abspath(str(lock_path)))
    held = getattr(_LOCKS_HELD, "paths", None)
    if held is None:
        held = _LOCKS_HELD.paths = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    impl = _flock_impl()
    with open(lock_path, "a+") as handle:
        impl.flock(handle.fileno(), impl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            impl.flock(handle.fileno(), impl.LOCK_UN)


# ---------------------------------------------------------------------------
# Small file readers and helper loading
# ---------------------------------------------------------------------------


def _read_json_file(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_jsonl_cache(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


_MODULE_CACHE: dict[str, Any] = {}


def _load_module_from(path: Path, name: str) -> Any:
    key = f"{name}:{os.path.normcase(str(path))}"
    if key in _MODULE_CACHE:
        return _MODULE_CACHE[key]
    module = None
    try:
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except Exception:
        module = None
    _MODULE_CACHE[key] = module
    return module


def default_repo_root() -> Path | None:
    root = Path(REPO_ROOT)
    return root if root.is_dir() else None


def _scripts_dir() -> Path:
    return Path(REPO_ROOT) / "system" / "scripts"


def _network_allowed() -> bool:
    """Test sandboxes and the offline switch keep every external call closed."""
    return os.environ.get("SWITCHBOARD_TEST_SANDBOX") != "1" and os.environ.get("SWITCHBOARD_TODO_OFFLINE") != "1"


def _run_command(command: list[str], timeout: float = EXTERNAL_TIMEOUT_SECONDS) -> str | None:
    try:
        finished = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return finished.stdout if finished.returncode == 0 else None


def run_workspace_tool(args: list[str], timeout: float = EXTERNAL_TIMEOUT_SECONDS) -> Any:
    """Call `switchboard agent-tools ...` and parse its JSON. Any failure means no evidence."""
    if not _network_allowed():
        return None
    executable = shutil.which("switchboard")
    if not executable:
        return None
    output = _run_command([executable, "agent-tools", *args], timeout)
    if output is None:
        return None
    try:
        return json.loads(output)
    except ValueError:
        return {"text": output}


# ---------------------------------------------------------------------------
# Workstream config and estimates
# ---------------------------------------------------------------------------

_WORKSTREAM_CONFIG_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _safe_slug(value: Any) -> str | None:
    slug = str(value or "").strip()
    if not slug or "/" in slug or "\\" in slug or slug.startswith("."):
        return None
    return slug


def load_workstream_config(slug: Any, root: Path | None = None) -> dict[str, Any]:
    name = _safe_slug(slug)
    if name is None:
        return {}
    base = Path(root) if root is not None else Path(WORKSTREAMS_DIR)
    key = (str(base), name)
    if key in _WORKSTREAM_CONFIG_CACHE:
        return _WORKSTREAM_CONFIG_CACHE[key]
    config: dict[str, Any] = {}
    path = base / name / "config.yaml"
    if path.is_file():
        try:
            import yaml  # type: ignore[import-untyped]

            loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                config = loaded
        except Exception:
            config = {}
    _WORKSTREAM_CONFIG_CACHE[key] = config
    return config


def hydrate_estimate(item: dict[str, Any]) -> int:
    explicit = _positive_int(item.get("estimate_min"))
    if explicit:
        return explicit
    configured = _positive_int(load_workstream_config(item.get("workstream")).get("estimate_min"))
    return configured or DEFAULT_ESTIMATE_MIN


# ---------------------------------------------------------------------------
# Classification: buckets, routing, sorting, dedupe
# ---------------------------------------------------------------------------


def base_bucket(item: dict[str, Any], now: datetime, today: date) -> str:
    status = normalize_status(item.get("status"))
    if status == "done":
        return "done"
    if str(item.get("id") or "") in SURFACE_CONFIRMATIONS_BY_ID:
        return "confirmation"
    snooze = parse_moment(item.get("snooze_until"))
    review = pacific_date(item.get("review_on"))
    if status in ("waiting", "scheduled"):
        if snooze is not None:
            return "actionable" if snooze <= now else status
        if review is not None and review <= today:
            return "actionable"
        return status
    if status == "someday":
        return "someday"
    if snooze is not None and snooze > now:
        return "scheduled"
    if review is not None and review > today:
        return "scheduled"
    return "actionable"


def routing_kind(item: dict[str, Any]) -> str:
    for key in ("workstream", "project", "due", "review_on"):
        if _clean_text(item.get(key)):
            return key
    return "unrouted"


def attention_date(item: dict[str, Any]) -> date | None:
    return pacific_date(item.get("due")) or pacific_date(item.get("review_on"))


def due_state(item: dict[str, Any], today: date) -> str:
    when = attention_date(item)
    if when is None:
        return "unscheduled"
    if when < today:
        return "overdue"
    if when == today:
        return "today"
    return "upcoming"


def age_days(item: dict[str, Any], now: datetime) -> int | None:
    created = parse_moment(item.get("created_at"))
    if created is None:
        return None
    return (now.astimezone(timezone.utc).date() - created.astimezone(timezone.utc).date()).days


def fingerprint(item: dict[str, Any]) -> str | None:
    stem = title_stem(item.get("title"))
    if not stem:
        return None
    ref = canonicalize_source_ref(item.get("source_ref"))
    if ref:
        return f"source-ref|{ref}"
    due = _clean_text(item.get("due"))
    if due:
        return f"{source_type(item.get('source'))}|{due}|{stem}"
    return None


def sort_key(item: dict[str, Any], now: datetime | None = None, today: date | None = None) -> tuple:
    now = now or utc_now()
    today = today or today_pacific()
    priority = normalize_priority(item.get("priority"))
    rank = PRIORITY_RANK.get(priority, 9)
    age = age_days(item, now)
    if priority == "P2" and age is not None and age >= 21:
        rank = 1
    attention = 0
    if priority == "P1":
        when = attention_date(item)
        created = parse_moment(item.get("created_at"))
        recent = created is not None and now - created <= timedelta(days=3)
        if (when is not None and when >= today) or recent:
            attention = 0
        elif when is not None and (today - when).days > 5:
            attention = 2
        else:
            attention = 1
    raw_due = str(_clean_text(item.get("due")) or _clean_text(item.get("review_on")) or "9999-12-31")
    created_raw = _clean_text(item.get("created_at"))
    return (
        rank,
        attention,
        DUE_STATE_RANK[due_state(item, today)],
        raw_due,
        ROUTING_RANK[routing_kind(item)],
        (0, created_raw) if created_raw else (1, ""),
        str(item.get("title") or "").lower(),
    )


def _daily_cap(rules: Any) -> int:
    value = rules.get("max_actionable_daily") if isinstance(rules, dict) else None
    return _positive_int(value) or DEFAULT_DAILY_CAP


def confirmation_action(item_id: Any) -> str:
    return CONFIRM_COMMAND_TEMPLATE.format(id=item_id)


def normalized_copy(item: dict[str, Any], now: datetime | None = None, today: date | None = None) -> dict[str, Any]:
    """A view copy of one stored item with every derived key. Storage is never touched."""
    now = now or utc_now()
    today = today or today_pacific()
    view = copy.deepcopy(item)
    item_id = str(item.get("id") or "")
    view["priority"] = normalize_priority(item.get("priority"))
    view["status"] = normalize_status(item.get("status"))
    view["source_type"] = source_type(item.get("source"))
    view["estimate_min"] = hydrate_estimate(item)
    view["requires_user"] = requires_user_value(item)
    for field in CALENDAR_FIELDS:
        view.setdefault(field, None)
    view["bucket"] = base_bucket(item, now, today)
    view["routing_kind"] = routing_kind(item)
    view["due_state"] = due_state(item, today)
    view["age_days"] = age_days(item, now)
    view["needs_routing"] = False
    view["fingerprint"] = fingerprint(item)
    view["is_duplicate"] = False
    view["duplicate_of"] = None
    if item_id and item_id in SURFACE_WOULD_CLOSE_BY_ID and view["bucket"] != "done":
        view["status"] = "done"
        view["bucket"] = "done"
        view["surface_gate_reason"] = SURFACE_WOULD_CLOSE_BY_ID[item_id]
    elif view["bucket"] == "confirmation":
        view["completion_evidence"] = list(SURFACE_CONFIRMATIONS_BY_ID.get(item_id) or [])
        view["confirmation_action"] = confirmation_action(item_id)
        view["requires_user"] = True
    return view


def _stale_entry(view: dict[str, Any]) -> dict[str, Any]:
    unrouted_high = view["routing_kind"] == "unrouted" and view["priority"] in ("P0", "P1")
    if view.get("needs_routing") or (unrouted_high and view["bucket"] == "actionable"):
        advice = "route"
    elif view["due_state"] == "overdue":
        advice = "reschedule-or-close"
    elif view["source_type"] in ("gmail", "slack", "meeting"):
        advice = "waiting-or-someday"
    else:
        advice = "review"
    return {
        "id": view.get("id"),
        "title": view.get("title"),
        "priority": view["priority"],
        "age_days": view["age_days"],
        "due_state": view["due_state"],
        "routing_kind": view["routing_kind"],
        "workstream": view.get("workstream"),
        "recommendation": advice,
    }


def stale_entries(views: list[dict[str, Any]], days: int = 7, priorities: Iterable[str] | None = ("P2", "P3"),
                  now: datetime | None = None, today: date | None = None) -> list[dict[str, Any]]:
    wanted = {normalize_priority(p) for p in priorities} if priorities else set()
    pool = [
        view for view in views
        if view["bucket"] not in ("done", "confirmation")
        and not view["is_duplicate"]
        and view["age_days"] is not None
        and view["age_days"] >= days
        and (not wanted or view["priority"] in wanted)
    ]
    pool.sort(key=lambda view: sort_key(view, now, today))
    return [_stale_entry(view) for view in pool]


def classify_items(items: list[dict[str, Any]], rules: Any = None) -> dict[str, Any]:
    now = utc_now()
    today = today_pacific()
    cap = _daily_cap(rules)
    views = [normalized_copy(item, now, today) for item in items or [] if isinstance(item, dict)]
    keys = {id(view): sort_key(view, now, today) for view in views}
    order = {id(view): index for index, view in enumerate(views)}

    groups: dict[str, list[dict[str, Any]]] = {}
    for view in views:
        if view["bucket"] in ("done", "confirmation") or not view["fingerprint"]:
            continue
        groups.setdefault(view["fingerprint"], []).append(view)
    duplicate_views: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda view: keys[id(view)])
        primary = members[0]
        for extra in members[1:]:
            extra["is_duplicate"] = True
            extra["duplicate_of"] = primary.get("id")
            duplicate_views.append(extra)
    duplicate_views.sort(key=lambda view: order[id(view)])
    duplicates = [
        {"id": view.get("id"), "title": view.get("title"), "duplicate_of": view["duplicate_of"],
         "source_ref": view.get("source_ref")}
        for view in duplicate_views
    ]

    pool = sorted(
        (view for view in views if view["bucket"] == "actionable" and not view["is_duplicate"]),
        key=lambda view: keys[id(view)],
    )
    actionable = pool[:cap]
    for view in pool[cap:]:
        view["bucket"] = "scheduled"
        view["daily_cap_overflow"] = True
    for view in actionable:
        view["needs_routing"] = view["priority"] in ("P0", "P1") and view["routing_kind"] == "unrouted"

    confirmations = sorted(
        (view for view in views if view["bucket"] == "confirmation" and not view["is_duplicate"]),
        key=lambda view: keys[id(view)],
    )
    triage = [view for view in actionable if view["needs_routing"]]
    stale = stale_entries(views, 7, ("P2", "P3"), now, today)

    counted = [view for view in views if not view["is_duplicate"]]

    def count(bucket: str) -> int:
        return sum(1 for view in counted if view["bucket"] == bucket)

    summary = {
        "total": len(views),
        "actionable": len(actionable),
        "waiting": count("waiting"),
        "scheduled": count("scheduled"),
        "someday": count("someday"),
        "confirmation": count("confirmation"),
        "done": count("done"),
        "overdue_actionable": sum(1 for view in actionable if view["due_state"] == "overdue"),
        "today_actionable": sum(1 for view in actionable if view["due_state"] == "today"),
        "unrouted_actionable": sum(1 for view in actionable if view["routing_kind"] == "unrouted"),
        "unrouted_high_priority": len(triage),
        "stale_actionable_count": len(stale),
        "duplicates_ignored": len(duplicates),
        "max_actionable_daily": cap,
        "overflow_scheduled": len(pool) - len(actionable),
    }
    return {
        "items": views,
        "actionable": actionable,
        "completion_confirmations": confirmations,
        "triage_candidates": triage,
        "stale_actionable": stale,
        "duplicates": duplicates,
        "summary": summary,
        "rules": {"max_actionable_daily": cap},
    }


def overflow_views(classified: dict[str, Any]) -> list[dict[str, Any]]:
    pool = [view for view in classified["items"] if view.get("daily_cap_overflow") and not view["is_duplicate"]]
    return sorted(pool, key=lambda view: sort_key(view))


def reminder_views(classified: dict[str, Any], mode: str, today: date | None = None) -> list[dict[str, Any]]:
    today = today or today_pacific()
    candidates = [
        view for view in classified["items"]
        if view.get("id") and view["bucket"] not in ("done", "confirmation") and not view["is_duplicate"]
    ]
    horizon = today + timedelta(days=7)
    picked: list[dict[str, Any]] = []
    if mode == "morning":
        for view in candidates:
            if view["priority"] != "P1":
                continue
            when = attention_date(view)
            if view["bucket"] == "actionable":
                picked.append(view)
            elif view.get("daily_cap_overflow") and (when is None or when <= horizon):
                picked.append(view)
            elif view["status"] == "open" and when is not None and when <= horizon:
                picked.append(view)

        def review_only(view: dict[str, Any]) -> int:
            review = pacific_date(view.get("review_on"))
            return 1 if review is not None and not _clean_text(view.get("due")) and review < today else 0

        picked.sort(key=lambda view: (review_only(view), sort_key(view)))
        return picked
    if mode == "eod":
        end = today + timedelta(days=2)
        for view in candidates:
            if view["priority"] != "P1" or view["bucket"] in ("waiting", "someday"):
                continue
            if view["status"] != "open" and view["bucket"] != "actionable":
                continue
            when = attention_date(view)
            if when is not None and today <= when <= end:
                picked.append(view)
    elif mode == "weekly":
        picked = [
            view for view in candidates
            if view["priority"] == "P2" and (view["bucket"] == "actionable" or view.get("daily_cap_overflow"))
        ]
    picked.sort(key=lambda view: sort_key(view))
    return picked


# ---------------------------------------------------------------------------
# Display titles through the repo name helper
# ---------------------------------------------------------------------------


def _named_refs_helper() -> Any:
    global _NAMED_REFS_MODULE
    if _NAMED_REFS_MODULE is None:
        _NAMED_REFS_MODULE = False
        root = default_repo_root()
        if root is not None:
            path = Path(root) / "system" / "scripts" / "named_refs.py"
            if path.is_file():
                try:
                    spec = importlib.util.spec_from_file_location("todo_cli_named_refs", str(path))
                    if spec is not None and spec.loader is not None:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        budget = os.environ.get("TODO_NAMED_REFS_MAX_FETCH", "0").strip() or "0"
                        module.set_fetch_budget(int(budget))
                        _NAMED_REFS_MODULE = module
                except Exception:
                    _NAMED_REFS_MODULE = False
    return _NAMED_REFS_MODULE or None


def display_title(item: dict[str, Any]) -> str:
    title = item.get("title") if isinstance(item, dict) else None
    if not title:
        return ""
    title = str(title)
    helper = _named_refs_helper()
    if helper is None:
        return title
    try:
        named = helper.name_refs_plain(title, tasks=False)
    except Exception:
        return title
    return named if isinstance(named, str) and named else title


# ---------------------------------------------------------------------------
# Recurring templates
# ---------------------------------------------------------------------------

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RULE_HELP = "recurrence_rule must be weekly:<weekday> (monday to sunday) or monthly:<day 1-31>"


def _parse_rule(rule: str) -> tuple[str, int]:
    kind, _, value = rule.partition(":")
    if kind == "weekly" and value in WEEKDAY_NAMES:
        return "weekly", WEEKDAY_NAMES.index(value)
    if kind == "monthly" and value.isdigit() and 1 <= int(value) <= 31:
        return "monthly", int(value)
    raise ValueError(RULE_HELP)


def normalize_template(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("a recurring template must be a JSON object")
    template_id = re.sub(r"[^a-z0-9]+", "-", str(raw.get("template_id") or raw.get("id") or "").lower()).strip("-")
    if not template_id:
        raise ValueError("template_id must contain at least one letter or digit")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("template title is required")
    rule = str(raw.get("recurrence_rule") or "").strip().lower()
    _parse_rule(rule)
    next_due = pacific_date(raw.get("next_due"))
    if next_due is None:
        raise ValueError("next_due must be an ISO date (YYYY-MM-DD)")
    window_raw = raw.get("materialization_window_days")
    window = 3 if window_raw in (None, "") else _positive_int(window_raw)
    if window is None:
        raise ValueError("materialization_window_days must be a positive integer")
    estimate_raw = raw.get("estimate_min")
    estimate = None
    if estimate_raw not in (None, ""):
        estimate = _positive_int(estimate_raw)
        if estimate is None:
            raise ValueError("estimate_min must be a positive integer")
    tags = raw.get("tags")
    normalized = {
        "template_id": template_id,
        "title": title,
        "recurrence_rule": rule,
        "next_due": next_due.isoformat(),
        "priority": normalize_priority(raw.get("priority")),
        "materialization_window_days": window,
        "estimate_min": estimate,
        "source": _clean_text(raw.get("source")) or "recurring-template",
        "status": normalize_status(raw.get("status")),
        "workstream": _clean_text(raw.get("workstream")),
        "project": _clean_text(raw.get("project")),
        "context": _clean_text(raw.get("context")),
        "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
        "last_materialized_at": raw.get("last_materialized_at"),
    }
    for key, value in raw.items():
        if key not in normalized and key != "id":
            normalized[key] = value
    return normalized


def recurrence_enabled() -> bool:
    module = _load_module_from(_scripts_dir() / "workflow_upgrade_flags.py", "todo_cli_workflow_flags")
    if module is None:
        return False
    try:
        return bool(module.is_flag_enabled("recurrence_enabled"))
    except Exception:
        return False


def _template_due_now(template: dict[str, Any], today: date) -> date | None:
    due = date.fromisoformat(template["next_due"])
    if due > today + timedelta(days=template["materialization_window_days"]):
        return None
    return due


def templates_due(payload: dict[str, Any]) -> bool:
    """True when materialization would add at least one item (no writes)."""
    templates = payload.get("recurring_templates") or []
    if not templates or not recurrence_enabled():
        return False
    today = today_pacific()
    existing = {(item.get("template_id"), pacific_date(item.get("due")))
                for item in payload.get("items") or [] if isinstance(item, dict)}
    for raw in templates:
        try:
            template = normalize_template(raw)
        except ValueError:
            continue
        due = _template_due_now(template, today)
        if due is not None and (template["template_id"], due) not in existing:
            return True
    return False


def _item_from_template(template: dict[str, Any], due: date) -> dict[str, Any]:
    item = {
        "id": str(uuid.uuid4()),
        "title": template["title"],
        "source": template["source"],
        "source_ref": f"template:{template['template_id']}",
        "priority": template["priority"],
        "status": template["status"],
        "due": due.isoformat(),
        "review_on": None,
        "snooze_until": None,
        "created_at": now_z(),
        "completed_at": None,
        "context": template["context"],
        "tags": list(template["tags"]),
        "workstream": template["workstream"],
        "project": template["project"],
        "estimate_min": template["estimate_min"],
        "requires_user": False,
        "calendar_event_id": None,
        "calendar_event_url": None,
        "calendar_event_start": None,
        "calendar_event_end": None,
        "calendar_synced_at": None,
        "generated_from_template": True,
        "template_id": template["template_id"],
    }
    item["requires_user"] = infer_requires_user(item)
    item["estimate_min"] = hydrate_estimate(item)
    return item


def materialize_templates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Append one item per due template. Returns the new items; none while the flag is off."""
    templates = payload.get("recurring_templates") or []
    if not templates or not recurrence_enabled():
        return []
    items = payload.setdefault("items", [])
    today = today_pacific()
    existing = {(item.get("template_id"), pacific_date(item.get("due"))) for item in items if isinstance(item, dict)}
    kept: list[Any] = []
    generated: list[dict[str, Any]] = []
    for raw in templates:
        try:
            template = normalize_template(raw)
        except ValueError:
            kept.append(raw)
            continue
        due = _template_due_now(template, today)
        if due is not None and (template["template_id"], due) not in existing:
            item = _item_from_template(template, due)
            items.append(item)
            generated.append(item)
            existing.add((template["template_id"], due))
            template["last_materialized_at"] = now_z()
        kept.append(template)
    payload["recurring_templates"] = kept
    return generated


def _next_occurrence(rule: str, after: date) -> date:
    kind, value = _parse_rule(rule)
    if kind == "weekly":
        step = (value - after.weekday()) % 7 or 7
        return after + timedelta(days=step)
    year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (following - timedelta(days=1)).day
    return date(year, month, min(value, last_day))


def advance_template(payload: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    if not item.get("generated_from_template") or not item.get("template_id"):
        return None
    if not recurrence_enabled():
        return None
    completed_due = pacific_date(item.get("due"))
    if completed_due is None:
        return None
    templates = payload.get("recurring_templates") or []
    for index, raw in enumerate(templates):
        try:
            template = normalize_template(raw)
        except ValueError:
            continue
        if template["template_id"] != item["template_id"]:
            continue
        previous = date.fromisoformat(template["next_due"])
        if previous > completed_due:
            return None
        upcoming = _next_occurrence(template["recurrence_rule"], completed_due)
        template["next_due"] = upcoming.isoformat()
        template["last_materialized_at"] = now_z()
        templates[index] = template
        return {"template_id": template["template_id"], "previous_due": previous.isoformat(),
                "next_due": upcoming.isoformat()}
    return None


# ---------------------------------------------------------------------------
# Calendar: API access
# ---------------------------------------------------------------------------

_CALENDAR_ID_CACHE: dict[str, str] = {}
_RUN_SLOTS: dict[str, tuple[datetime, datetime]] = {}


def _gcloud_token() -> str | None:
    executable = shutil.which("gcloud")
    if not executable:
        return None
    output = _run_command([executable, "auth", "print-access-token"], timeout=30)
    token = (output or "").strip()
    return token or None


def _connector_token() -> str | None:
    path = Path(CONNECTORS_DIR) / "google-token.json"
    stored = _read_json_file(path)
    if not isinstance(stored, dict) or not stored.get("refresh_token"):
        return None
    scopes = stored.get("scopes") or stored.get("scope")
    if isinstance(scopes, str):
        scopes = scopes.split()
    if scopes and CALENDAR_FULL_SCOPE not in list(scopes):
        return None
    access = stored.get("access_token") or stored.get("token")
    try:
        expires_at = float(stored.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if not expires_at and stored.get("expiry"):
        moment = parse_moment(stored.get("expiry"))
        expires_at = moment.timestamp() if moment else 0.0
    if access and expires_at - time.time() > 60:
        return str(access)
    try:
        import urllib.request

        form = urlencode({
            "client_id": stored.get("client_id") or "",
            "client_secret": stored.get("client_secret") or "",
            "refresh_token": stored["refresh_token"],
            "grant_type": "refresh_token",
        }).encode("utf-8")
        request = urllib.request.Request(
            str(stored.get("token_uri") or GOOGLE_TOKEN_URI), data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read().decode("utf-8"))
        fresh = str(answer["access_token"])
    except Exception:
        return None
    stored["access_token"] = fresh
    stored["expires_at"] = time.time() + float(answer.get("expires_in") or 3600)
    with contextlib.suppress(Exception):
        atomic_write_text(path, json.dumps(stored, indent=2) + "\n", temp_tag="todo-cli.")
    return fresh


def _calendar_access_token() -> str | None:
    if not _network_allowed():
        return None
    return _gcloud_token() or _connector_token()


def calendar_api_request(method: str, path: str, payload: Any = None, query: dict[str, Any] | None = None) -> Any:
    """One authenticated Calendar API v3 call. Returns parsed JSON, or None on any failure."""
    token = _calendar_access_token()
    if not token:
        return None
    import urllib.error
    import urllib.request

    url = CALENDAR_API_BASE + (path if path.startswith("/") else f"/{path}")
    if query:
        cleaned = {key: value for key, value in query.items() if value is not None}
        if cleaned:
            url += "?" + urlencode(cleaned, doseq=True)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=EXTERNAL_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if method.upper() == "DELETE" and exc.code in (404, 410):
            return {"status": "ok"}
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not raw.strip():
        return {"status": "ok"}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _q(value: str) -> str:
    return quote(str(value), safe="")


def _read_calendar_state() -> dict[str, Any]:
    state = _read_json_file(TODO_CALENDAR_STATE_PATH)
    return state if isinstance(state, dict) else {}


def _record_calendar(calendar_id: str, origin: str) -> None:
    state = _read_calendar_state()
    first_seen = state.get("created_at")
    state.update({"calendar_id": calendar_id, "summary": TODO_CALENDAR_SUMMARY, "origin": origin})
    state["created_at"] = first_seen or now_z()
    path = Path(TODO_CALENDAR_STATE_PATH)
    if not sandbox_allows_path(path):
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def todo_calendar_id(create: bool = True) -> str | None:
    """The dedicated to-do calendar id. Never the primary calendar."""
    forced = os.environ.get("SWITCHBOARD_TODO_CALENDAR_ID", "").strip()
    if forced and forced.lower() != "primary":
        return forced
    if _CALENDAR_ID_CACHE.get("id"):
        return _CALENDAR_ID_CACHE["id"]
    stored = str(_read_calendar_state().get("calendar_id") or "").strip()
    if stored and stored.lower() != "primary":
        _CALENDAR_ID_CACHE["id"] = stored
        return stored
    listing = calendar_api_request("GET", "/users/me/calendarList", query={"minAccessRole": "owner", "maxResults": 250})
    if not isinstance(listing, dict):
        return None
    for calendar in listing.get("items") or []:
        if not isinstance(calendar, dict) or calendar.get("primary"):
            continue
        if calendar.get("summary") != TODO_CALENDAR_SUMMARY:
            continue
        if calendar.get("accessRole") not in (None, "owner"):
            continue
        found = str(calendar.get("id") or "")
        if found and found.lower() != "primary":
            _record_calendar(found, "adopted")
            _CALENDAR_ID_CACHE["id"] = found
            return found
    if not create:
        return None
    created = calendar_api_request("POST", "/calendars", {
        "summary": TODO_CALENDAR_SUMMARY,
        "description": "Private, free-showing holds for P0 and P1 to-dos. Written by the to-do skill.",
        "timeZone": PACIFIC_NAME,
    })
    made = str(created.get("id") or "") if isinstance(created, dict) else ""
    if made and made.lower() != "primary":
        _record_calendar(made, "created")
        _CALENDAR_ID_CACHE["id"] = made
        return made
    return None


def _moment_text(value: Any) -> str:
    moment = value if isinstance(value, datetime) else parse_moment(value)
    return iso_z(moment) if moment else str(value)


def _list_calendar_events(calendar_id: str, time_min: Any, time_max: Any, limit: int = 250) -> list[dict[str, Any]] | None:
    events: list[dict[str, Any]] = []
    page_token = None
    while True:
        answer = calendar_api_request("GET", f"/calendars/{_q(calendar_id)}/events", query={
            "timeMin": _moment_text(time_min), "timeMax": _moment_text(time_max), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": max(1, min(limit, 250)), "pageToken": page_token,
        })
        if not isinstance(answer, dict):
            return None
        events.extend(event for event in answer.get("items") or [] if isinstance(event, dict))
        page_token = answer.get("nextPageToken")
        if not page_token or len(events) >= limit:
            return events[:limit]


def calendar_api_list_events(time_min: Any, time_max: Any, limit: int = 200) -> list[dict[str, Any]]:
    """Meetings for the meeting reconciler. To-do holds are left out so a hold never proves a meeting."""
    events = _list_calendar_events("primary", time_min, time_max, limit) or []
    return [event for event in events if not str(event.get("summary") or "").startswith(HOLD_PREFIX)]


def _event_time(block: Any) -> datetime | None:
    if not isinstance(block, dict):
        return None
    return parse_moment(block.get("dateTime"))


# ---------------------------------------------------------------------------
# Calendar: hold placement
# ---------------------------------------------------------------------------


def _weekend_shift(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day - timedelta(days=2)
    return day


def _business_days(start: date, count: int = HOLD_SEARCH_DAYS) -> list[date]:
    days = [start]
    cursor = start
    while len(days) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor)
    return days


def _day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, *HOLD_DAY_START, tzinfo=PACIFIC)
    end = datetime(day.year, day.month, day.day, *HOLD_DAY_END, tzinfo=PACIFIC)
    return start, end


def hold_window_date(item: dict[str, Any], today: date | None = None) -> date | None:
    """The day a hold should sit on, or None when the item gets no hold."""
    today = today or today_pacific()
    if normalize_status(item.get("status")) == "done":
        return None
    if normalize_priority(item.get("priority")) not in ("P0", "P1"):
        return None
    due = pacific_date(item.get("due"))
    if due is None:
        return None
    window = _weekend_shift(due)
    if window > today:
        return window
    start = parse_moment(item.get("calendar_event_start"))
    if start is not None:
        start_day = start.astimezone(PACIFIC).date()
        if start_day >= today:
            return start_day
    return None


def _stored_slot(item: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = parse_moment(item.get("calendar_event_start"))
    end = parse_moment(item.get("calendar_event_end"))
    return (start, end) if start and end else None


def _primary_busy(start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
    answer = calendar_api_request("POST", "/freeBusy", {
        "timeMin": iso_z(start), "timeMax": iso_z(end), "timeZone": PACIFIC_NAME, "items": [{"id": "primary"}],
    })
    if not isinstance(answer, dict):
        return None
    primary = (answer.get("calendars") or {}).get("primary") or {}
    if primary.get("errors"):
        return None
    blocks = []
    for block in primary.get("busy") or []:
        block_start = parse_moment((block or {}).get("start"))
        block_end = parse_moment((block or {}).get("end"))
        if block_start and block_end:
            blocks.append((block_start, block_end))
    return blocks


def _find_slot(item: dict[str, Any], window: date, calendar_id: str) -> tuple[datetime, datetime] | None:
    duration = timedelta(minutes=hydrate_estimate(item))
    own = _stored_slot(item)
    own_event = item.get("calendar_event_id")
    for day in _business_days(window):
        low, high = _day_window(day)
        if low + duration > high:
            return None
        busy = _primary_busy(low, high)
        if busy is None:
            return None
        events = _list_calendar_events(calendar_id, low, high)
        if events is None:
            return None
        for event in events:
            if event.get("status") == "cancelled":
                continue
            event_start, event_end = _event_time(event.get("start")), _event_time(event.get("end"))
            if event_start and event_end:
                busy.append((event_start, event_end))
        busy.extend(slot for event_id, slot in _RUN_SLOTS.items() if event_id != own_event)
        if own is not None:
            busy = [block for block in busy if block != own]
        cursor = low
        while cursor + duration <= high:
            finish = cursor + duration
            if not any(cursor < block_end and finish > block_start for block_start, block_end in busy):
                return cursor, finish
            cursor += HOLD_STEP
    return None


def _event_body(item: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    priority = normalize_priority(item.get("priority"))
    lines = [
        f"Todo ID: {item.get('id')}",
        f"Priority: {priority}",
        f"Estimate: {hydrate_estimate(item)} min",
        f"Source: {item.get('source') or 'manual'}",
    ]
    for label, key in (("Workstream", "workstream"), ("Project", "project"), ("Source ref", "source_ref")):
        value = _clean_text(item.get(key))
        if value:
            lines.append(f"{label}: {value}")
    context = _clean_text(item.get("context"))
    if context:
        lines.extend(["", context])
    return {
        "summary": f"{HOLD_PREFIX}{item.get('title') or ''}",
        "description": "\n".join(lines),
        "start": {"dateTime": start.astimezone(PACIFIC).isoformat(), "timeZone": PACIFIC_NAME},
        "end": {"dateTime": end.astimezone(PACIFIC).isoformat(), "timeZone": PACIFIC_NAME},
        "visibility": "private",
        "transparency": "transparent",
        "colorId": HOLD_COLORS.get(priority, HOLD_FALLBACK_COLOR),
    }


def deterministic_event_id(item: dict[str, Any]) -> str:
    seed = f"switchboard:todo:{item.get('id')}:{item.get('calendar_create_generation') or ''}"
    return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex


def _write_hold(item: dict[str, Any], calendar_id: str, start: datetime, end: datetime,
                used: set[str]) -> dict[str, Any] | None:
    body = _event_body(item, start, end)
    base = f"/calendars/{_q(calendar_id)}/events"
    event_id = _clean_text(item.get("calendar_event_id"))
    created = False
    if event_id:
        reply = calendar_api_request("PATCH", f"{base}/{_q(event_id)}", body)
        if not isinstance(reply, dict):
            return None
    else:
        event_id = deterministic_event_id(item)
        reply = calendar_api_request("POST", base, {**body, "id": event_id})
        if not (isinstance(reply, dict) and reply.get("id") == event_id):
            existing = calendar_api_request("GET", f"{base}/{_q(event_id)}")
            if not (isinstance(existing, dict) and existing.get("id") == event_id
                    and existing.get("status") != "cancelled"):
                return None
            reply = calendar_api_request("PATCH", f"{base}/{_q(event_id)}", body)
            if not isinstance(reply, dict):
                return None
        created = True
    item["calendar_event_id"] = event_id
    item["calendar_event_url"] = reply.get("htmlLink") or item.get("calendar_event_url")
    item["calendar_event_start"] = iso_z(start)
    item["calendar_event_end"] = iso_z(end)
    item["calendar_synced_at"] = now_z()
    _RUN_SLOTS[event_id] = (start, end)
    used.add(event_id)
    return {"event_id": event_id, "event_url": item["calendar_event_url"], "start": item["calendar_event_start"],
            "end": item["calendar_event_end"], "estimate_min": hydrate_estimate(item), "created": created}


def _adopt_orphan(item: dict[str, Any], calendar_id: str, window: date, used: set[str]) -> None:
    days = _business_days(window)
    low = _day_window(days[0])[0]
    high = _day_window(days[-1])[1]
    events = _list_calendar_events(calendar_id, low, high) or []
    summary = f"{HOLD_PREFIX}{item.get('title') or ''}"
    matches = []
    for event in events:
        event_id = event.get("id")
        if event.get("summary") != summary or not event_id or event_id in used or event.get("status") == "cancelled":
            continue
        start = _event_time(event.get("start"))
        matches.append((start or datetime.max.replace(tzinfo=timezone.utc), event))
    if not matches:
        return
    matches.sort(key=lambda pair: pair[0])
    event = matches[0][1]
    start, end = _event_time(event.get("start")), _event_time(event.get("end"))
    item["calendar_event_id"] = event["id"]
    item["calendar_event_url"] = event.get("htmlLink")
    item["calendar_event_start"] = iso_z(start) if start else None
    item["calendar_event_end"] = iso_z(end) if end else None
    used.add(event["id"])


def _known_slot(item: dict[str, Any], calendar_id: str) -> tuple[datetime, datetime] | None:
    slot = _stored_slot(item)
    if slot:
        return slot
    event = calendar_api_request("GET", f"/calendars/{_q(calendar_id)}/events/{_q(item['calendar_event_id'])}")
    if isinstance(event, dict):
        start, end = _event_time(event.get("start")), _event_time(event.get("end"))
        if start and end:
            return start, end
    start = parse_moment(item.get("calendar_event_start"))
    if start:
        return start, start + timedelta(minutes=hydrate_estimate(item))
    return None


def delete_calendar_hold(item: dict[str, Any]) -> bool:
    """Remove the item's hold. True only when the calendar confirmed it is gone."""
    event_id = _clean_text(item.get("calendar_event_id"))
    if not event_id:
        for field in CALENDAR_EVENT_FIELDS:
            if field in item:
                item[field] = None
        return False
    calendar_id = todo_calendar_id(create=False)
    if not calendar_id:
        return False
    answer = calendar_api_request("DELETE", f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}")
    if answer is None:
        return False
    item["calendar_create_generation"] = secrets.token_hex(8)
    for field in CALENDAR_EVENT_FIELDS:
        item[field] = None
    item["calendar_synced_at"] = now_z()
    _RUN_SLOTS.pop(event_id, None)
    return True


def sync_calendar_hold_if_needed(item: dict[str, Any], used_event_ids: set[str] | None = None) -> dict[str, Any] | None:
    """Apply the hold rule to one item. Returns the hold result, or None when no hold was written."""
    used = used_event_ids if used_event_ids is not None else set()
    window = hold_window_date(item)
    if window is None:
        if normalize_status(item.get("status")) == "done" or _clean_text(item.get("calendar_event_id")):
            delete_calendar_hold(item)
        return None
    calendar_id = todo_calendar_id()
    if not calendar_id:
        return None
    if not _clean_text(item.get("calendar_event_id")):
        _adopt_orphan(item, calendar_id, window, used)
    slot = _find_slot(item, window, calendar_id)
    if slot is not None:
        return _write_hold(item, calendar_id, slot[0], slot[1], used)
    if _clean_text(item.get("calendar_event_id")):
        known = _known_slot(item, calendar_id)
        if known is not None:
            result = _write_hold(item, calendar_id, known[0], known[1], used)
            if result is not None:
                result["metadata_only"] = True
            return result
    return None


def _hold_snapshot(item: dict[str, Any]) -> tuple:
    return (item.get("calendar_event_id"), item.get("calendar_event_start"), item.get("calendar_event_end"))


def sync_calendar_payload(payload: dict[str, Any], item_id: str | None = None) -> dict[str, Any]:
    items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
    targets = [item for item in items if item_id is None or item.get("id") == item_id]
    used = {str(item["calendar_event_id"]) for item in items if item.get("calendar_event_id")}
    today = today_pacific()
    changes: list[dict[str, Any]] = []
    unsynced: list[dict[str, Any]] = []
    for item in targets:
        before = _hold_snapshot(item)
        eligible = hold_window_date(item, today) is not None
        result = sync_calendar_hold_if_needed(item, used)
        after = _hold_snapshot(item)
        brief = {"id": item.get("id"), "title": item.get("title"), "due": item.get("due"),
                 "estimate_min": hydrate_estimate(item)}
        if before == after:
            reason = None
            if eligible and result is None:
                reason = ("calendar hold update unconfirmed" if item.get("calendar_event_id")
                          else "eligible item still has no calendar hold")
            elif not eligible and item.get("calendar_event_id"):
                reason = "calendar hold removal unconfirmed"
            if reason:
                unsynced.append({**brief, "reason": reason})
            continue
        if before[0] and not after[0]:
            action = "cleared"
        elif before[0]:
            action = "updated"
        else:
            action = "created"
        changes.append({**brief, "action": action, "event_id": item.get("calendar_event_id"),
                        "event_url": item.get("calendar_event_url"), "start": item.get("calendar_event_start"),
                        "end": item.get("calendar_event_end"), "result": result})
    return {"matched": len(targets), "updated": len(changes), "items": changes, "unsynced": unsynced}


# ---------------------------------------------------------------------------
# Evidence sources (local caches)
# ---------------------------------------------------------------------------


def load_linear_inbox_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in load_jsonl_cache(LINEAR_INBOX_CACHE_PATH):
        key = str(row.get("linear_identifier") or "").strip().upper()
        if not key:
            continue
        current = index.get(key)
        if current is None or str(row.get("captured_at") or "") >= str(current.get("captured_at") or ""):
            index[key] = row
    return index


def load_comms_signals_index() -> dict[str, Any]:
    by_thread: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for path in (MENTIONS_CACHE_PATH, CHANNEL_SIGNALS_CACHE_PATH):
        for row in load_jsonl_cache(path):
            messages = [m for m in row.get("thread_messages") or [] if isinstance(m, dict)]
            channel, thread = str(row.get("channel_id") or ""), str(row.get("thread_ts") or "")
            if channel and thread:
                by_thread.setdefault((channel, thread), []).extend(messages)
            ref = canonicalize_source_ref(row.get("source_ref"))
            if ref:
                by_ref.setdefault(ref, []).extend(messages)
    return {"by_thread": by_thread, "by_source_ref": by_ref}


def load_workspace_updates() -> dict[str, Any]:
    data = _read_json_file(WORKSPACE_UPDATES_PATH)
    if not isinstance(data, dict):
        return {"updates": {}}
    if not isinstance(data.get("updates"), dict):
        data["updates"] = {}
    return data


def load_doc_comment_gmail_triggers() -> dict[str, Any]:
    data = _read_json_file(DOC_COMMENT_TRIGGERS_PATH)
    messages = data.get("messages") if isinstance(data, dict) else None
    return messages if isinstance(messages, dict) else {}


def load_local_doc_comment_index() -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in load_jsonl_cache(DOC_COMMENTS_CACHE_PATH):
        key = (str(row.get("file_id") or ""), str(row.get("comment_id") or ""))
        if key[0] and key[1]:
            index.setdefault(key, []).append(row)
    return index


def fetch_drive_comments(file_id: str, cache: dict[str, Any]) -> list[dict[str, Any]]:
    if file_id in cache:
        return cache[file_id]
    answer = run_workspace_tool(["google-drive", "list-comments", "--file-id-or-url", file_id, "--all-pages"])
    comments = answer.get("comments") if isinstance(answer, dict) else None
    cache[file_id] = [c for c in comments or [] if isinstance(c, dict)]
    return cache[file_id]


def slack_surface_results(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Thread verdicts from the repo's thread-resolution helper, keyed by item id."""
    helper = _scripts_dir() / "check-thread-resolution.py"
    if not items or not helper.is_file() or not _network_allowed():
        return {}  # the helper reads Slack: never from a test sandbox
    command = [sys.executable, str(helper), "--json"]
    for item in items:
        command += ["--ref", str(item.get("source_ref") or "")]
    output = _run_command(command, timeout=HELPER_TIMEOUT_SECONDS)
    if output is None:
        return {}
    try:
        results = json.loads(output).get("results")
    except (ValueError, AttributeError):
        return {}
    if not isinstance(results, list):
        return {}
    return {str(item.get("id")): result for item, result in zip(items, results) if isinstance(result, dict)}


# ---------------------------------------------------------------------------
# Reconcilers: one function per kind of completion evidence
# ---------------------------------------------------------------------------


def _slug_pattern(slug: str) -> str:
    return r"[-\s]+".join(re.escape(part) for part in re.split(r"[-\s]+", slug.lower()) if part)


def workspace_state_reconciled(item: dict[str, Any], workspace_updates: dict[str, Any]) -> str | None:
    slug = _clean_text(item.get("workstream"))
    if not slug:
        return None
    updates = (workspace_updates or {}).get("updates") or {}
    entry = updates.get(f"workstream:{slug}")
    if not isinstance(entry, dict):
        return None
    if str(entry.get("status") or "").strip().lower() not in ("done", "completed", "resolved"):
        return None
    stamp = parse_moment(entry.get("timestamp"))
    if stamp is None or stamp < created_moment(item):
        return None
    title = str(item.get("title") or "").lower()
    prefix = CLOSE_OUT_PREFIX_RE.match(title)
    if not prefix or not re.match(_slug_pattern(slug) + r"(?![a-z0-9])", title[prefix.end():]):
        return None
    return f"workstream {slug} reported {entry.get('status')}"


def _slack_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def comms_state_reconciled(item: dict[str, Any], index: dict[str, Any]) -> str | None:
    ref = item.get("source_ref")
    parsed = parse_slack_ref(ref)
    messages: list[dict[str, Any]] = []
    if parsed:
        messages = list((index.get("by_thread") or {}).get((parsed["channel"], parsed["thread_ts"])) or [])
    if not messages:
        messages = list((index.get("by_source_ref") or {}).get(canonicalize_source_ref(ref)) or [])
    created = created_moment(item)
    for message in messages:
        sent = _slack_ts(message.get("ts"))
        if sent is not None and sent >= created and is_owner_slack_message(message):
            return "owner replied in the Slack thread (collector cache)"
    return None


def linear_inbox_reconciled(item: dict[str, Any], index: dict[str, Any]) -> str | None:
    for key in linear_keys(item.get("title"), item.get("context"), item.get("source_ref")):
        entry = index.get(key)
        if isinstance(entry, dict) and str(entry.get("state") or "").strip().lower() in DONE_WORDS:
            return f"Linear {key} is {entry.get('state')}"
    return None


def _reply_like(title: Any) -> bool:
    return str(title or "").strip().lower().startswith(MESSAGE_REPLY_PREFIXES)


def _terms_hit(terms: list[str], *texts: Any) -> int:
    haystack = set(_tokens(" ".join(str(text or "") for text in texts)))
    return sum(1 for term in terms if term in haystack)


def gmail_sent_cache_reconciled(item: dict[str, Any], rows: list[dict[str, Any]], email_sourced: bool) -> str | None:
    created = created_moment(item)
    message_id = (extract_gmail_message_id(item.get("source_ref")) or "").lower()
    recent = []
    for row in rows or []:
        sent = parse_moment(row.get("date"))
        if sent is not None and sent >= created:
            recent.append(row)
    if message_id:
        for row in recent:
            if message_id in (str(row.get("thread_id") or "").lower(), str(row.get("message_id") or "").lower()):
                return f"sent a reply in the same mail thread ({row.get('subject') or 'no subject'})"
    if email_sourced and _reply_like(item.get("title")):
        terms = distinctive_terms(item.get("title"), item.get("context"))[:6]
        if len(terms) >= 2:
            for row in recent:
                if _terms_hit(terms, row.get("subject"), row.get("snippet"), row.get("to")) >= 2:
                    return f"sent mail matches the to-do ({row.get('subject') or 'no subject'})"
    return None


def _artifact_title(title: str) -> bool:
    return bool(ARTIFACT_WORD_RE.search(title)) and not REVIEW_PREFIX_RE.match(title)


def drive_activity_reconciled(item: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    title = str(item.get("title") or "")
    if not _artifact_title(title) or SEND_WORD_RE.search(title):
        return None
    terms = distinctive_terms(title)
    created = created_moment(item)
    for row in rows or []:
        if row.get("owned_by_me") is not True:
            continue
        modified = parse_moment(row.get("modified_time"))
        if modified is None or modified < created:
            continue
        if _terms_hit(terms, row.get("file_name")) >= 2:
            return f"owner updated Drive file {row.get('file_name')}"
    return None


def notion_activity_reconciled(item: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    match = NOTION_RE.search(str(item.get("source_ref") or ""))
    if not match:
        return None
    page = match.group(1).lower()
    created = created_moment(item)
    for row in rows or []:
        if str(row.get("page_id") or "").replace("-", "").lower() != page or row.get("by_me") is not True:
            continue
        stamp = parse_moment(row.get("timestamp"))
        if stamp is not None and stamp >= created:
            return f"owner {row.get('activity_type') or 'edited'} the Notion page {row.get('page_title') or page}"
    return None


def blueprint_updates_reconciled(item: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    title = str(item.get("title") or "")
    if not re.search(r"\bstatus updates?\b", title, re.I):
        return None
    terms = distinctive_terms(title)
    slug_terms = set(_tokens(item.get("workstream")))
    created = created_moment(item)
    for row in rows or []:
        if row.get("by_me") is not True:
            continue
        stamp = parse_moment(row.get("update_date"))
        if stamp is None or stamp < created:
            continue
        for name in row.get("project_names") or []:
            name_terms = set(_tokens(name))
            if (sum(1 for t in terms if t in name_terms) >= 2 or (slug_terms & name_terms)
                    or (str(name).strip() and str(name).lower() in title.lower())):
                return f"owner posted a status update for {name}"
    return None


def jira_state_reconciled(item: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    keys = jira_keys(item.get("source_ref"))
    if not keys:
        return None
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        key = str(row.get("issue_key") or "").upper()
        if key in keys and str(row.get("updated") or "") >= str((latest.get(key) or {}).get("updated") or ""):
            latest[key] = row
    created = created_moment(item)
    for key in keys:
        row = latest.get(key)
        if not row or str(row.get("status_category") or "").strip().lower() != "done":
            continue
        stamp = parse_moment(row.get("updated"))
        if stamp is not None and stamp >= created:
            return f"Jira {key} is {row.get('status') or 'done'}"
    return None


def _gh_state(url: str, kind: str) -> str | None:
    if not _network_allowed():
        return None
    executable = shutil.which("gh")
    if not executable:
        return None
    verb = "pr" if kind == "pull" else "issue"
    output = _run_command([executable, verb, "view", url, "--json", "state"])
    try:
        return str(json.loads(output or "").get("state") or "").upper() or None
    except (ValueError, AttributeError):
        return None


def github_inbox_reconciled(item: dict[str, Any], cache: dict[str, Any]) -> str | None:
    """Only the item's own GitHub ref can close it: closed or merged."""
    match = GITHUB_REF_RE.search(str(item.get("source_ref") or ""))
    if not match:
        return None
    owner, repo, kind, number = match.groups()
    url = f"https://github.com/{owner}/{repo}/{kind}/{number}"
    if cache is None:
        cache = {}
    if url not in cache:
        cache[url] = _gh_state(url, kind.lower())
    state = cache[url]
    if state in ("MERGED", "CLOSED"):
        noun = "PR" if kind.lower() == "pull" else "issue"
        return f"GitHub {noun} {repo}#{number} is {state.lower()}"
    return None


def calendar_reconciled(item: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    title = str(item.get("title") or "").strip().lower()
    if not title.startswith(MEETING_PREFIXES):
        return None
    ref = str(item.get("source_ref") or "").strip()
    wanted = set(distinctive_terms(item.get("source"), item.get("title"), ref))
    now = utc_now()
    created = created_moment(item)
    for event in events or []:
        if not isinstance(event, dict) or event.get("status") == "cancelled":
            continue
        end = _event_time(event.get("end")) or parse_moment((event.get("end") or {}).get("date"))
        if end is None or end > now or end < created:
            continue
        attendees = event.get("attendees")
        if attendees:
            mine = [a for a in attendees if isinstance(a, dict) and a.get("self")]
            if not mine or mine[0].get("responseStatus") not in ("accepted", "tentative"):
                continue
        linked = bool(ref) and (
            ref in str(event.get("htmlLink") or "")
            or ref in str(event.get("description") or "")
            or any(ref == str((a or {}).get("fileUrl") or "") for a in event.get("attachments") or [])
        )
        shared = wanted & set(distinctive_terms(event.get("summary"), event.get("description")))
        if linked or len(shared) >= 2 or any(len(term) >= 8 for term in shared):
            return f"meeting '{event.get('summary') or 'untitled'}' has ended"
    return None


def workstream_artifact_reconciled(item: dict[str, Any]) -> str | None:
    slug = _safe_slug(item.get("workstream"))
    title = str(item.get("title") or "")
    if not slug or not _artifact_title(title):
        return None
    folder = Path(WORKSTREAMS_DIR) / slug
    if not folder.is_dir():
        return None
    terms = distinctive_terms(title)
    if len(terms) < 2:
        return None
    since = created_moment(item).timestamp()
    skip_dirs = {"sources", "inbox", "_archive", ".skill-data", ".git"}
    skip_files = {"CONTEXT.md", "README.md", "config.yaml", ".DS_Store"}
    seen = 0
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            seen += 1
            if seen > 5000:
                return None
            if name in skip_files:
                continue
            path = Path(current) / name
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified >= since and _terms_hit(terms, path.stem.replace("_", " ")) >= 2:
                return f"workstream file {path.relative_to(folder).as_posix()} matches the to-do"
    return None


def _owner_doc_reply(replies: list[dict[str, Any]], threshold: datetime) -> tuple[str, str] | None:
    for reply in replies or []:
        stamp = parse_moment(reply.get("createdTime") or reply.get("created_at"))
        if stamp is None or stamp < threshold:
            continue
        author = reply.get("author") or {}
        if is_owner_author(author):
            return "close", "owner replied in the doc comment thread"
        name = author.get("displayName") if isinstance(author, dict) else None
        return "suggest", str(name or "someone")
    return None


def doc_comment_verdict(item: dict[str, Any], triggers: dict[str, Any], drive_cache: dict[str, Any],
                        local_index: dict[tuple[str, str], list[dict[str, Any]]] | None) -> tuple[str, Any] | None:
    doc_ref = google_doc_source_ref(item, triggers)
    file_id, comment_id = _doc_ids(doc_ref)
    if not file_id or not comment_id:
        return None
    threshold = created_moment(item) - timedelta(minutes=15)
    for comment in fetch_drive_comments(file_id, drive_cache) or []:
        if str(comment.get("id") or "") != comment_id:
            continue
        if comment.get("resolved") is True:
            return "close", "the doc comment thread is resolved"
        decided = _owner_doc_reply(comment.get("replies") or [], threshold)
        if decided and decided[0] == "close":
            return decided
        if decided:
            return "suggest", {"type": "doc-thread-reply", "detail": f"{decided[1]} replied in the doc comment thread",
                               "source_ref": doc_ref, "certainty": "suggestive"}
        return None
    rows = (local_index or {}).get((file_id, comment_id)) or []
    if any(str(row.get("kind")) == "comment" and row.get("resolved") is True for row in rows):
        return "close", "the doc comment thread is resolved (collector cache)"
    replies = sorted((row for row in rows if str(row.get("kind")) == "reply"),
                     key=lambda row: str(row.get("created_at") or ""))
    for row in replies:
        stamp = parse_moment(row.get("created_at"))
        if stamp is None or stamp < threshold:
            continue
        if row.get("authored_by_me") is True:
            return "close", "owner replied in the doc comment thread (collector cache)"
        return "suggest", {"type": "doc-thread-reply", "detail": f"{row.get('author') or 'someone'} replied in the doc comment thread",
                           "source_ref": doc_ref, "certainty": "suggestive"}
    return None


# ---------------------------------------------------------------------------
# Network-backed evidence (every call has a timeout; failure means no evidence)
# ---------------------------------------------------------------------------


def parse_gmail_text_blocks(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    labels = {"Subject": "subject", "From": "from", "To": "to", "Date": "date", "ID": "message_id",
              "Thread ID": "thread_id", "Snippet": "snippet"}
    for line in str(text or "").splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip()
        if label == "Subject":
            current = {}
            messages.append(current)
        if current is not None and label in labels:
            current[labels[label]] = value.strip()
    return messages


class _ScanContext:
    """Lazy, per-scan caches so each source is read at most once."""

    def __init__(self, lookback_days: int):
        self.lookback_days = max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS))
        self._values: dict[str, Any] = {}
        self.gh_cache: dict[str, Any] = {}
        self.drive_cache: dict[str, Any] = {}

    def get(self, name: str, loader: Callable[[], Any]) -> Any:
        if name not in self._values:
            try:
                self._values[name] = loader()
            except Exception:
                self._values[name] = None
        return self._values[name]

    def workspace_updates(self) -> dict[str, Any]:
        return self.get("workspace", lambda: load_workspace_updates()) or {"updates": {}}

    def comms(self) -> dict[str, Any]:
        return self.get("comms", lambda: load_comms_signals_index()) or {}

    def linear_inbox(self) -> dict[str, Any]:
        return self.get("linear_inbox", lambda: load_linear_inbox_index()) or {}

    def cache(self, path_name: str) -> list[dict[str, Any]]:
        return self.get(path_name, lambda: load_jsonl_cache(globals()[path_name])) or []

    def sent_search(self) -> list[dict[str, str]]:
        def search() -> list[dict[str, str]]:
            answer = run_workspace_tool([
                "gmail", "search-messages", "--query", f"in:sent newer_than:{self.lookback_days}d",
                "--label", "all", "--max-results", "100",
            ])
            if not isinstance(answer, dict):
                return []
            return parse_gmail_text_blocks(answer.get("result") or answer.get("text") or "")

        return self.get("sent_search", search) or []

    def slack_thread(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            answer = run_workspace_tool(["slack", "read-thread", "--channel", channel, "--thread-ts", thread_ts,
                                         "--output-format", "json"])
            return [m for m in (answer or {}).get("messages") or [] if isinstance(m, dict)] if isinstance(answer, dict) else []

        return self.get(f"thread:{channel}:{thread_ts}", read) or []

    def slack_search(self, terms: list[str]) -> list[dict[str, Any]]:
        def search() -> list[dict[str, Any]]:
            answer = run_workspace_tool(["slack", "search-messages", "--query", "from:me " + " ".join(terms),
                                         "--newer-than", f"P{self.lookback_days}D", "--limit", "50",
                                         "--output-format", "json"])
            return [m for m in (answer or {}).get("messages") or [] if isinstance(m, dict)] if isinstance(answer, dict) else []

        return self.get("slack:" + " ".join(terms), search) or []

    def linear_state(self, key: str) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            query = "query($id: String!) { issue(id: $id) { identifier state { name type } } }"
            answer = run_workspace_tool(["linear", "execute-graphql", "--query", query,
                                         "--variables", json.dumps({"id": key})])
            issue = (((answer or {}).get("data") or {}).get("issue") or {}) if isinstance(answer, dict) else {}
            return issue.get("state") if isinstance(issue.get("state"), dict) else None

        return self.get(f"linear:{key}", read)

    def meetings(self) -> list[dict[str, Any]]:
        now = utc_now()
        return self.get("meetings", lambda: calendar_api_list_events(
            now - timedelta(days=self.lookback_days), now + timedelta(days=1))) or []


def _addressee(title: str) -> str | None:
    match = re.match(r"^\s*(?:reply to|respond to|follow[ -]up with|follow[ -]up to|send(?: to)?)\s+([A-Z][\w.'-]+)", title)
    return match.group(1) if match else None


def gmail_network_reconciled(item: dict[str, Any], ctx: _ScanContext, email_sourced: bool) -> str | None:
    messages = ctx.sent_search()
    if not messages:
        return None
    created = created_moment(item)
    message_id = (extract_gmail_message_id(item.get("source_ref")) or "").lower()
    name = _addressee(str(item.get("title") or ""))
    recent = [m for m in messages if (parse_moment(m.get("date")) or datetime.min.replace(tzinfo=timezone.utc)) >= created]
    if message_id:
        for message in recent:
            if message_id not in (message.get("thread_id", "").lower(), message.get("message_id", "").lower()):
                continue
            text = f"{message.get('subject', '')} {message.get('snippet', '')}".lower()
            if name is None or name.lower() in text:
                return f"sent a reply in the mail thread ({message.get('subject') or 'no subject'})"
    if email_sourced and _reply_like(item.get("title")):
        terms = distinctive_terms(item.get("title"), item.get("context"))[:6]
        if len(terms) >= 2:
            for message in recent:
                if _terms_hit(terms, message.get("subject"), message.get("snippet"), message.get("to")) >= 2:
                    return f"sent mail matches the to-do ({message.get('subject') or 'no subject'})"
    return None


def gdrive_comment_network_reconciled(item: dict[str, Any], ctx: _ScanContext) -> str | None:
    file_id, comment_id = _doc_ids(str(item.get("source_ref") or ""))
    if not file_id or not comment_id:
        return None
    threshold = created_moment(item) - timedelta(minutes=15)
    for comment in fetch_drive_comments(file_id, ctx.drive_cache) or []:
        if str(comment.get("id") or "") != comment_id:
            continue
        if comment.get("resolved") is True:
            return "the doc comment thread is resolved"
        for reply in comment.get("replies") or []:
            stamp = parse_moment(reply.get("createdTime"))
            if stamp is not None and stamp >= threshold and is_owner_author(reply.get("author")):
                return "owner replied in the doc comment thread"
    return None


def slack_network_reconciled(item: dict[str, Any], ctx: _ScanContext) -> str | None:
    created = _moment_or(item.get("created_at"), utc_now() - timedelta(days=14))
    parsed = parse_slack_ref(item.get("source_ref"))
    if _reply_like(item.get("title")) and parsed:
        thread = ctx.slack_thread(parsed["channel"], parsed["thread_ts"])
        if thread:
            if is_owner_slack_message(thread[-1]):
                return "owner wrote the latest message in the Slack thread"
            for message in thread:
                sent = _slack_ts(message.get("ts"))
                if sent is not None and sent >= created - timedelta(minutes=15) and is_owner_slack_message(message):
                    return "owner posted in the Slack thread"
    terms = distinctive_terms(item.get("title"), item.get("context"))[:5]
    if not terms:
        return None
    for message in ctx.slack_search(terms):
        sent = _slack_ts(message.get("ts"))
        if sent is None or sent < created:
            continue
        hits = _terms_hit(terms, message.get("text"))
        if parsed:
            if str(message.get("thread_ts") or "") == parsed["thread_ts"]:
                return "owner posted in the same Slack thread"
            channel = (message.get("channel") or {}).get("id") if isinstance(message.get("channel"), dict) else None
            if channel == parsed["channel"] and hits >= 1:
                return "owner posted a matching message in the same Slack channel"
        elif hits >= 2:
            return "owner posted a matching Slack message"
    return None


def linear_network_reconciled(item: dict[str, Any], ctx: _ScanContext, keys: list[str]) -> str | None:
    for key in keys:
        state = ctx.linear_state(key)
        if not state:
            continue
        if str(state.get("type") or "").lower() in ("completed", "canceled") or \
                str(state.get("name") or "").strip().lower() in DONE_WORDS:
            return f"Linear {key} is {state.get('name') or state.get('type')}"
    return None


# ---------------------------------------------------------------------------
# Reconcile scan and apply
# ---------------------------------------------------------------------------


def _reconcile_one(item: dict[str, Any], ctx: _ScanContext) -> tuple[str | None, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []

    def attempt(name: str, check: Callable[[], str | None], record: bool = True) -> str | None:
        try:
            reason = check()
        except Exception:
            reason = None
        if record or reason:
            attempts.append({"reconciler": name, "matched": reason})
        return reason

    family = source_type(item.get("source"))
    ref = str(item.get("source_ref") or "")
    ref_class = classify_source_ref(ref)
    slack_like = "slack" in (family, ref_class)
    gmail_like = "gmail" in (family, ref_class)
    email_sourced = family == "gmail"
    reply_like = _reply_like(item.get("title"))
    keys = linear_keys(item.get("title"), item.get("context"), ref)
    workstream = _clean_text(item.get("workstream"))
    title_context = f"{item.get('title') or ''} {item.get('context') or ''}"

    steps: list[tuple[str, bool, Callable[[], str | None], bool]] = [
        ("workspace_state", bool(workstream), lambda: workspace_state_reconciled(item, ctx.workspace_updates()), True),
        ("comms_state", slack_like, lambda: comms_state_reconciled(item, ctx.comms()), True),
        ("linear_inbox", bool(keys), lambda: linear_inbox_reconciled(item, ctx.linear_inbox()), True),
        ("gmail_sent_cache", gmail_like or reply_like,
         lambda: gmail_sent_cache_reconciled(item, ctx.cache("GMAIL_SENT_CACHE_PATH"), email_sourced), True),
    ]
    for name, runs, check, record in steps:
        if runs:
            reason = attempt(name, check, record)
            if reason:
                return reason, attempts

    drive_rows = ctx.cache("DRIVE_ACTIVITY_CACHE_PATH")
    reason = attempt("drive_activity", lambda: drive_activity_reconciled(item, drive_rows), bool(drive_rows))
    if reason:
        return reason, attempts

    notion_rows = ctx.cache("NOTION_ACTIVITY_CACHE_PATH")
    blueprint_rows = ctx.cache("BLUEPRINT_UPDATES_CACHE_PATH")
    jira_rows = ctx.cache("JIRA_CACHE_PATH")
    later: list[tuple[str, bool, Callable[[], str | None], bool]] = [
        ("notion_activity", bool(notion_rows) and ref_class == "notion",
         lambda: notion_activity_reconciled(item, notion_rows), True),
        ("blueprint_updates", bool(blueprint_rows), lambda: blueprint_updates_reconciled(item, blueprint_rows), True),
        ("jira_state", bool(jira_rows) and bool(jira_keys(ref)), lambda: jira_state_reconciled(item, jira_rows), True),
        ("gmail", gmail_like or reply_like, lambda: gmail_network_reconciled(item, ctx, email_sourced), True),
        ("gdrive_comment", ref_class == "gdoc", lambda: gdrive_comment_network_reconciled(item, ctx), True),
        ("github", ref_class == "github" or bool(GITHUB_REF_RE.search(title_context)),
         lambda: github_inbox_reconciled(item, ctx.gh_cache), True),
        ("slack", slack_like, lambda: slack_network_reconciled(item, ctx), True),
        ("linear", True, lambda: linear_network_reconciled(item, ctx, keys) if keys else None, bool(keys)),
        ("calendar", family == "meeting", lambda: calendar_reconciled(item, ctx.meetings()), True),
        ("workstream_artifact", bool(workstream), lambda: workstream_artifact_reconciled(item), True),
    ]
    for name, runs, check, record in later:
        if runs:
            reason = attempt(name, check, record)
            if reason:
                return reason, attempts
    return None, attempts


def reconcile_items(payload: dict[str, Any], lookback_days: int, apply: bool, diagnose: bool = False,
                    item_ids: Iterable[str] | None = None) -> dict[str, Any]:
    wanted = set(item_ids) if item_ids is not None else None
    ctx = _ScanContext(lookback_days)
    reconciled: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in list(payload.get("items") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if wanted is not None and item.get("id") not in wanted:
            continue
        if normalize_status(item.get("status")) == "done":
            continue
        reason, attempts = _reconcile_one(item, ctx)
        if reason:
            reconciled.append({"id": item.get("id"), "title": item.get("title"), "reason": reason})
            if apply:
                complete_in_place(payload, item, reason)
        elif diagnose:
            diagnostics.append({"id": item.get("id"), "title": item.get("title"), "source": item.get("source"),
                                "source_ref": item.get("source_ref"), "workstream": item.get("workstream"),
                                "attempts": attempts})
    result: dict[str, Any] = {"lookback_days": lookback_days, "reconciled": reconciled, "count": len(reconciled)}
    if diagnose:
        result["diagnostics"] = diagnostics
        result["unmatched_count"] = len(diagnostics)
    return result


def complete_in_place(payload: dict[str, Any], item: dict[str, Any], reason: str | None) -> dict[str, Any] | None:
    """Mark one stored item done, drop its hold, and advance its template."""
    item["status"] = "done"
    item["completed_at"] = now_z()
    item["snooze_until"] = None
    if reason:
        item["resolution"] = reason
    delete_calendar_hold(item)
    return advance_template(payload, item)


def _find_by_id(payload: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in payload.get("items") or []:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


def classify_payload(path: Path | str, auto_reconcile: bool = False, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                     apply: bool = True, diagnose: bool = False, *, materialize: bool = True):
    """Load, optionally reconcile and apply under the lock, and classify.

    The scan always runs on an unlocked snapshot. Only the apply step takes the
    lock, and it reloads the file so a concurrent writer's changes survive.
    """
    path = Path(path)
    reconcile_result = None
    if auto_reconcile and apply:
        snapshot = load_data(path)
        scan = reconcile_items(snapshot, lookback_days=lookback_days, apply=False, diagnose=diagnose)
        with exclusive_lock(path):
            payload = load_data(path)
            generated = materialize_templates(payload) if materialize else []
            applied: list[dict[str, Any]] = []
            for match in scan.get("reconciled") or []:
                item = _find_by_id(payload, str(match.get("id")))
                if item is None or normalize_status(item.get("status")) == "done":
                    continue
                reason = match.get("reason") or "matched external completion signal"
                complete_in_place(payload, item, reason)
                applied.append({"id": item.get("id"), "title": item.get("title"), "reason": reason})
            calendar_sync = sync_calendar_payload(payload)
            if generated or applied or (calendar_sync or {}).get("updated") or (calendar_sync or {}).get("unsynced"):
                save_data(path, payload)
        reconcile_result = dict(scan)
        reconcile_result["reconciled"] = applied
        reconcile_result["count"] = len(applied)
        reconcile_result["calendar_sync"] = calendar_sync
        if generated:
            reconcile_result["recurrence"] = {"materialized": generated, "count": len(generated)}
    elif auto_reconcile:
        payload = load_data(path)
        reconcile_result = reconcile_items(payload, lookback_days=lookback_days, apply=False, diagnose=diagnose)
    else:
        payload = load_data(path)
        if apply and materialize and templates_due(payload):
            with exclusive_lock(path):
                payload = load_data(path)
                if materialize_templates(payload):
                    save_data(path, payload)
    classified = classify_items(payload.get("items") or [], payload.get("rules"))
    return payload, classified, reconcile_result


# ---------------------------------------------------------------------------
# Surface gate: verify the exact items a view will show
# ---------------------------------------------------------------------------


def _load_resolution_flags() -> None:
    data = _read_json_file(TODO_RESOLUTION_FLAGS_PATH)
    flags = data.get("flags") if isinstance(data, dict) else None
    for flag in flags or []:
        if not isinstance(flag, dict) or not flag.get("todo_id"):
            continue
        entries = SURFACE_CONFIRMATIONS_BY_ID.setdefault(str(flag["todo_id"]), [])
        for signal in flag.get("signals") or []:
            if not isinstance(signal, dict):
                continue
            _add_evidence(str(flag["todo_id"]), {
                "type": signal.get("type") or "likely-complete",
                "detail": signal.get("detail") or "Possible out-of-band completion; confirm before closing.",
                "source_ref": signal.get("permalink") or flag.get("source_ref"),
                "certainty": "suggestive",
            })
        if not entries:
            SURFACE_CONFIRMATIONS_BY_ID.pop(str(flag["todo_id"]), None)


def _add_evidence(item_id: str, entry: dict[str, Any]) -> None:
    entries = SURFACE_CONFIRMATIONS_BY_ID.setdefault(item_id, [])
    if not any((e.get("type"), e.get("detail")) == (entry.get("type"), entry.get("detail")) for e in entries):
        entries.append(entry)


def _verify_bound(args: Any) -> int:
    try:
        bound = max(1, int(os.environ.get("SWITCHBOARD_TODO_SURFACE_VERIFY_LIMIT", "25")))
    except ValueError:
        bound = 25
    limit = getattr(args, "limit", None)
    if isinstance(limit, int) and not isinstance(limit, bool):
        bound = min(bound, 3 * max(limit, 1))
    return bound


def _priority_set(raw: Any, default: str) -> set[str]:
    text = default if raw is None else str(raw)
    return {normalize_priority(part) for part in text.split(",") if part.strip()}


def _gate_candidates(args: Any, classified: dict[str, Any]) -> list[dict[str, Any]]:
    command = getattr(args, "command", None)
    actionable = list(classified["actionable"])
    overflow = overflow_views(classified)
    confirmations = list(classified["completion_confirmations"])
    views = classified["items"]
    bound = _verify_bound(args)
    if command in ("summary", "render-markdown"):
        pool = actionable + overflow + confirmations
    elif command == "list":
        bucket = getattr(args, "bucket", "actionable") or "actionable"
        if bucket == "actionable":
            pool = actionable + overflow
        elif bucket == "all":
            pool = [view for view in views if view["bucket"] != "done"]
        else:
            pool = [view for view in views if view["bucket"] == bucket]
    elif command == "reminders":
        pool = reminder_views(classified, getattr(args, "mode", "morning") or "morning")[:bound] + confirmations
    elif command == "suggest-routes":
        wanted = _priority_set(getattr(args, "priority", None), "P0,P1")
        pool = [view for view in actionable if not _clean_text(view.get("workstream")) and view["priority"] in wanted]
    elif command == "checkpoint":
        wanted = _priority_set(getattr(args, "priority", None), "P2,P3")
        days = getattr(args, "days", 7)
        days = 7 if days is None else days
        pool = [view for view in views if view["bucket"] != "done" and view["age_days"] is not None
                and view["age_days"] >= days and (not wanted or view["priority"] in wanted)]
    elif command == "snapshot":
        tops: dict[str, dict[str, Any]] = {}
        for view in sorted((v for v in views if v["bucket"] != "done" and _clean_text(v.get("workstream"))),
                           key=lambda v: sort_key(v)):
            tops.setdefault(str(view["workstream"]), view)
        pool = actionable + list(tops.values()) + confirmations
    else:
        pool = []
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for view in pool:
        item_id = str(view.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        picked.append(view)
        if len(picked) >= bound:
            break
    return picked


def verify_before_surface(args: Any) -> None:
    SURFACE_CONFIRMATIONS_BY_ID.clear()
    SURFACE_WOULD_CLOSE_BY_ID.clear()
    SURFACE_GATE_RESULT.clear()
    command = getattr(args, "command", None)
    if command not in SURFACE_COMMANDS or os.environ.get("SWITCHBOARD_TODO_SURFACE_SKIP") == "1":
        return
    _load_resolution_flags()
    path = Path(getattr(args, "path", None) or TODO_PATH)
    dry_run = bool(getattr(args, "dry_run", False))
    payload = load_data(path)
    classified = classify_items(payload.get("items") or [], payload.get("rules"))
    candidates = _gate_candidates(args, classified)
    if not candidates:
        SURFACE_GATE_RESULT.update({"checked": 0, "closed": 0, "confirmations": 0})
        return

    slack_items = [view for view in candidates
                   if classify_source_ref(view.get("source_ref")) == "slack" and parse_slack_ref(view.get("source_ref"))]
    try:
        verdicts = slack_surface_results(slack_items) if slack_items else {}
    except Exception:
        verdicts = {}
    try:
        triggers = load_doc_comment_gmail_triggers()
    except Exception:
        triggers = {}
    drive_cache: dict[str, Any] = {}
    gh_cache: dict[str, Any] = {}
    lazy: dict[str, Any] = {}

    def once(name: str, loader: Callable[[], Any]) -> Any:
        if name not in lazy:
            try:
                lazy[name] = loader()
            except Exception:
                lazy[name] = None
        return lazy[name]

    meeting_needed = any(source_type(view.get("source")) == "meeting" for view in candidates)
    now = utc_now()
    matches: list[dict[str, Any]] = []

    for view in candidates:
        item_id = str(view["id"])
        ref = str(view.get("source_ref") or "")
        ref_class = classify_source_ref(ref)
        reason = None

        verdict = verdicts.get(item_id) or {}
        token = str(verdict.get("verdict") or "")
        if token.startswith("resolved_by_"):
            how = "reacted to" if "reaction" in token else "replied in"
            reason = f"owner {how} the Slack thread"
        elif token == "teammate_replied":
            evidence = verdict.get("evidence") or {}
            _add_evidence(item_id, {
                "type": "slack-teammate-reply",
                "detail": f"{evidence.get('closer_user') or 'a teammate'} replied in the Slack thread",
                "source_ref": evidence.get("closer_permalink") or ref,
                "certainty": "suggestive",
            })

        checks: list[Callable[[], str | None]] = []

        def doc_check(view: dict[str, Any] = view, item_id: str = item_id) -> str | None:
            local = once("doc_index", lambda: load_local_doc_comment_index()) or {}
            decided = doc_comment_verdict(view, triggers or {}, drive_cache, local)
            if not decided:
                return None
            if decided[0] == "close":
                return decided[1]
            _add_evidence(item_id, decided[1])
            return None

        checks.append(doc_check)
        if ref_class == "github":
            checks.append(lambda view=view: github_inbox_reconciled(view, gh_cache))
        if ref_class == "gmail":
            checks.append(lambda view=view: gmail_sent_cache_reconciled(
                view, once("sent", lambda: load_jsonl_cache(GMAIL_SENT_CACHE_PATH)) or [], True))
        if linear_keys(view.get("title"), view.get("context"), ref):
            checks.append(lambda view=view: linear_inbox_reconciled(
                view, once("linear", lambda: load_linear_inbox_index()) or {}))
        if jira_keys(ref):
            checks.append(lambda view=view: jira_state_reconciled(
                view, once("jira", lambda: load_jsonl_cache(JIRA_CACHE_PATH)) or []))
        if meeting_needed and source_type(view.get("source")) == "meeting":
            checks.append(lambda view=view: calendar_reconciled(view, once("events", lambda: calendar_api_list_events(
                now - timedelta(days=30), now + timedelta(days=2))) or []))
        if _clean_text(view.get("workstream")):
            checks.append(lambda view=view: workstream_artifact_reconciled(view))

        for check in checks:
            if reason:
                break
            try:
                reason = check()
            except Exception:
                reason = None
        if reason:
            matches.append({"id": item_id, "title": view.get("title"), "reason": reason})

    applied: list[dict[str, Any]] = []
    if matches and not dry_run:
        with exclusive_lock(path):
            fresh = load_data(path)
            for match in matches:
                item = _find_by_id(fresh, match["id"])
                if item is None or normalize_status(item.get("status")) == "done":
                    continue
                complete_in_place(fresh, item, match["reason"])
                applied.append(match)
                SURFACE_CONFIRMATIONS_BY_ID.pop(match["id"], None)
            if applied:
                save_data(path, fresh)
    elif matches:
        for match in matches:
            SURFACE_WOULD_CLOSE_BY_ID[match["id"]] = match["reason"]

    closed_ids = {match["id"] for match in matches}
    confirmations = sum(1 for view in candidates
                        if str(view["id"]) in SURFACE_CONFIRMATIONS_BY_ID and str(view["id"]) not in closed_ids)
    SURFACE_GATE_RESULT.update({
        "checked": len(candidates),
        "closed": len(applied),
        "would_close": len(matches) if dry_run else 0,
        "matches": matches if dry_run else applied,
        "confirmations": confirmations,
    })


# ---------------------------------------------------------------------------
# Product live gate (optional repo script)
# ---------------------------------------------------------------------------


def run_live_gate(args: Any) -> None:
    if os.environ.get("SWITCHBOARD_LD_RECONCILE_ACTIVE") == "1" or os.environ.get("SWITCHBOARD_LD_RECONCILE_SKIP") == "1":
        return
    script = _scripts_dir() / "ld-reconcile.py"
    if not script.is_file():
        return
    dry_run = bool(getattr(args, "dry_run", False))
    command = [sys.executable, str(script), "--todo-path", str(args.path), "--json", "--quiet",
               "--dry-run" if dry_run else "--apply"]
    env = {**os.environ, "SWITCHBOARD_LD_RECONCILE_ACTIVE": "1"}
    try:
        finished = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=HELPER_TIMEOUT_SECONDS, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise TodoError("product live gate failed: timed out; no to-do list was rendered") from exc
    except OSError as exc:
        raise TodoError(f"product live gate failed: {exc}; no to-do list was rendered") from exc
    output = (finished.stdout or "") + (finished.stderr or "")
    if finished.returncode != 0:
        raise TodoError(f"product live gate failed (exit {finished.returncode}); no to-do list was rendered: "
                        f"{output[:500]}")
    if dry_run:
        try:
            report = json.loads(finished.stdout or "")
        except ValueError as exc:
            raise TodoError(f"product live gate failed: dry run did not return JSON; no to-do list was rendered: "
                            f"{output[:500]}") from exc
        if isinstance(report, dict) and report.get("actioned_count"):
            raise TodoError(f"product live gate would close {report.get('actioned_count')} to-do(s); "
                            "rerun without --dry-run to apply them before viewing the list")


# ---------------------------------------------------------------------------
# PR-set guard
# ---------------------------------------------------------------------------


def pr_refs(*texts: Any) -> set[str]:
    found: set[str] = set()
    for text in texts:
        raw = str(text or "")
        for match in PR_URL_RE.finditer(raw):
            found.add(f"{match.group(1).lower()}#{int(match.group(2))}")
        for match in PR_LABEL_RE.finditer(raw):
            found.add(f"{match.group(1).lower()}#{int(match.group(2))}")
        for match in PR_BARE_RE.finditer(raw):
            found.add(f"#{int(match.group(1))}")
    return found


def item_pr_refs(item: dict[str, Any]) -> set[str]:
    extra = item.get("pr_refs")
    extra_texts = [str(value) for value in extra] if isinstance(extra, list) else []
    return pr_refs(item.get("source_ref"), item.get("title"), item.get("context"), *extra_texts)


def _number(ref: str) -> str:
    return ref.rsplit("#", 1)[-1]


def _pr_sets_compatible(mine: set[str], target: set[str]) -> bool:
    if not target:
        return False
    numbers = {_number(ref) for ref in target}
    bare_target = {_number(ref) for ref in target if ref.startswith("#")}
    for ref in mine:
        if ref in target:
            continue
        if ref.startswith("#") and _number(ref) in numbers:
            continue
        if _number(ref) in bare_target:
            continue
        return False
    return True


def check_pr_set(payload: dict[str, Any], item: dict[str, Any], reason: str | None, allow_mismatch: bool,
                 path: Path) -> None:
    """Refuse a close whose reason re-points the item at a to-do for different PRs."""
    if not reason or allow_mismatch or not PR_TRIGGER_RE.search(reason):
        return
    mine = item_pr_refs(item)
    if not mine:
        return
    others = [other for other in payload.get("items") or [] if isinstance(other, dict) and other is not item]
    target = None
    for token in ID_TOKEN_RE.findall(reason.lower()):
        for other in others:
            other_id = str(other.get("id") or "").lower()
            if other_id and (other_id == token or other_id.startswith(token)):
                target = other
                break
        if target is not None:
            break
    reason_refs = pr_refs(reason)
    if target is None and reason_refs:
        for other in others:
            if item_pr_refs(other) & reason_refs:
                target = other
                break
    target_refs = item_pr_refs(target) if target is not None else reason_refs
    if _pr_sets_compatible(mine, target_refs):
        return
    if target is not None:
        for left, right in ((item, target), (target, item)):
            related = left.get("related") if isinstance(left.get("related"), list) else []
            if right.get("id") and right["id"] not in related:
                related.append(right["id"])
            left["related"] = related
        save_data(path, payload)
    raise PrSetRefusal({
        "refused": True,
        "reason": ("The close reason points at a to-do that covers a different PR set, so the item stays open. "
                   "Pass --allow-pr-mismatch to close it anyway."),
        "item_id": item.get("id"),
        "item_prs": sorted(mine),
        "target_id": target.get("id") if target is not None else None,
        "target_prs": sorted(target_refs),
        "override": "--allow-pr-mismatch",
    })


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def _load_archive(path: Path, month: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "month": month, "updated_at": now_z(), "records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise TodoError(f"archive file is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise TodoError(f"archive file has an unexpected shape: {path}")
    data.setdefault("version", 1)
    data.setdefault("month", month)
    return data


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def archive_items(path: Path, archive_dir: Path, selected: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    month = utc_now().strftime("%Y-%m")
    archive_path = Path(archive_dir) / f"{month}.json"
    archive = _load_archive(archive_path, month)
    by_id = {str(record.get("todo_id")): record for record in archive["records"] if isinstance(record, dict)}
    exported: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in selected:
        item_id = item.get("id")
        if not item_id:
            conflicts.append({"id": None, "reason": "todo has no stable ID"})
            continue
        if _clean_text(item.get("calendar_event_id")):
            conflicts.append({"id": item_id, "reason": "todo still has a calendar hold; clear the hold before archiving"})
            continue
        existing = by_id.get(str(item_id))
        if existing is not None:
            if _same(existing.get("item"), item):
                exported.append(item)
            else:
                conflicts.append({"id": item_id, "reason": "archive already holds different content for this id"})
            continue
        record = {"todo_id": item_id, "archived_at": now_z(), "reason": reason, "item": copy.deepcopy(item)}
        archive["records"].append(record)
        by_id[str(item_id)] = record
        exported.append(item)
    archive["updated_at"] = now_z()
    atomic_write_text(archive_path, _dump(archive))
    reread = _load_archive(archive_path, month)
    stored = {str(record.get("todo_id")): record.get("item") for record in reread["records"] if isinstance(record, dict)}
    for item in exported:
        if not _same(stored.get(str(item["id"])), item):
            raise TodoError(f"archive verification failed for {item['id']} in {archive_path}; nothing was removed")
    exported_ids = {item["id"] for item in exported}
    payload = load_data(path)
    payload["items"] = [item for item in payload["items"] if not (isinstance(item, dict) and item.get("id") in exported_ids)]
    save_data(path, payload)
    return {"archived": len(exported), "archive": str(archive_path), "reason": reason, "items": exported,
            "conflicts": conflicts}


def _last_touched(item: dict[str, Any]) -> datetime | None:
    stamps = [parse_moment(item.get(key)) for key in ("created_at", "updated_at", "due", "review_on", "snooze_until")]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Item lookup and creation
# ---------------------------------------------------------------------------


def find_item(payload: dict[str, Any], item_id: str, missing: str) -> dict[str, Any]:
    exact = _find_by_id(payload, item_id)
    if exact is not None:
        return exact
    wanted = item_id.strip().lower()
    if len(wanted) >= 4:  # a short id is a prefix of at least 4 characters, in any case
        matches = [item for item in payload.get("items") or []
                   if isinstance(item, dict) and str(item.get("id") or "").lower().startswith(wanted)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TodoError(f"Ambiguous id prefix {item_id}: it matches {len(matches)} items; use more characters")
    raise TodoError(missing)


def find_duplicate(items: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    print_key = fingerprint(candidate)
    ref = canonicalize_source_ref(candidate.get("source_ref"))
    family = source_type(candidate.get("source"))
    stem = title_stem(candidate.get("title"))
    for item in items:
        if not isinstance(item, dict):
            continue
        if print_key and fingerprint(item) == print_key:
            return item
        if ref and canonicalize_source_ref(item.get("source_ref")) == ref:
            return item
        if (stem and normalize_status(item.get("status")) != "done" and source_type(item.get("source")) == family
                and title_stem(item.get("title")) == stem):
            return item
    return None


def _used_event_ids(payload: dict[str, Any]) -> set[str]:
    return {str(item["calendar_event_id"]) for item in payload.get("items") or []
            if isinstance(item, dict) and item.get("calendar_event_id")}


def _optional_text(value: Any) -> str | None:
    """Flag value for set-status: None keeps the field, an empty string clears it."""
    return None if value is None else (str(value).strip() or "")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = str(getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


PRIORITY_BADGES = {"P0": "🔴 P0", "P1": "🟡 P1", "P2": "🟠 P2", "P3": "⚪ P3"}
EMPTY_CELL = "—"
SECTIONS = (
    ("⚡ Quick hits (30-45 min)", 0, 45),
    ("✍️ Medium lifts (1-2 hours)", 46, 120),
    ("🧠 Deep work (2-4 hours)", 121, None),
)
TABLE_HEADER = "| # | To do | Priority | Description | Action I need to take | Workspace | Link |"
TABLE_DIVIDER = "|---|---|---|---|---|---|---|"


def _cell(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return EMPTY_CELL
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.replace("|", "\\|")


def _link_target(view: dict[str, Any]) -> str | None:
    ref = str(view.get("source_ref") or "").strip()
    if ref.startswith(("https://", "http://", "file://")):
        return ref
    slug = _safe_slug(view.get("workstream"))
    if slug:
        context = Path(WORKSTREAMS_DIR) / slug / "CONTEXT.md"
        if context.is_file():
            return context.resolve().as_uri()
    return None


def _workspace_labels() -> dict[str, str]:
    names: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("workspace") and node.get("display_name"):
                names.setdefault(str(node["workspace"]), str(node["display_name"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_read_json_file(QUEUE_PATH))
    numbers: dict[str, str] = {}
    try:
        lines = Path(SIDEBAR_SNAPSHOT_PATH).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = re.match(r"^\s*(?:\[[^\]]*\]\s*)?workspace:(\d+)\s+(.+?)\s*$", line)
        if match:
            numbers.setdefault(match.group(2), match.group(1))
    labels = {}
    for slug, name in names.items():
        if name in numbers:
            labels[slug] = f"📁 {name} (workspace:{numbers[name]})"
    return labels


def _action_text(view: dict[str, Any]) -> str:
    bucket = view["bucket"]
    if view.get("daily_cap_overflow"):
        bucket = "actionable"
    if bucket == "waiting":
        review = _clean_text(view.get("review_on"))
        return f"Watch for outside movement, review on {review}" if review else "Watch for outside movement"
    if bucket == "scheduled":
        when = _clean_text(view.get("due")) or _clean_text(view.get("review_on")) or _clean_text(view.get("snooze_until"))
        return f"Pick it back up on {when}" if when else "Pick it back up when it comes due"
    if bucket == "someday":
        return "Keep it parked"
    if bucket == "confirmation":
        evidence = view.get("completion_evidence") or [{}]
        return f"Confirm the close: {evidence[0].get('detail') or 'completion evidence found'}"
    if view.get("requires_user"):
        return "Open the source and handle it"
    return _cell(view.get("title"), 100)


def render_markdown(classified: dict[str, Any], limit: int, backlog_health: bool,
                    reconcile_result: dict[str, Any] | None, gate: dict[str, Any]) -> str:
    pool = [view for view in classified["actionable"] + overflow_views(classified) if not view["is_duplicate"]]
    pool = [view for view in pool if _link_target(view)][: max(0, limit)]
    labels = _workspace_labels() if pool else {}
    lines = ["==="]
    number = 0
    for heading, low, high in SECTIONS:
        lines.append(f"### {heading}")
        lines.append(TABLE_HEADER)
        lines.append(TABLE_DIVIDER)
        rows = [view for view in pool if view["estimate_min"] >= low and (high is None or view["estimate_min"] <= high)]
        if not rows:
            lines.append("| " + " | ".join([EMPTY_CELL] * 7) + " |")
        for view in rows:
            number += 1
            slug = _clean_text(view.get("workstream"))
            workspace = labels.get(slug or "") or (slug or EMPTY_CELL)
            lines.append("| " + " | ".join([
                str(number),
                _cell(display_title(view)),
                PRIORITY_BADGES.get(view["priority"], view["priority"]),
                _cell(view.get("context")),
                _cell(_action_text(view)),
                _cell(workspace),
                f"[Open]({_link_target(view)})",
            ]) + " |")
        lines.append("----")
    confirmations = classified["completion_confirmations"]
    if confirmations:
        lines.append("")
        lines.append("### ✅ Likely done (confirm to close)")
        lines.append("These items show completion evidence that needs a yes before they close.")
        for view in confirmations[: max(0, limit)]:
            details = "; ".join(str(e.get("detail") or "") for e in (view.get("completion_evidence") or [])[:3])
            badge = PRIORITY_BADGES.get(view["priority"], view["priority"])
            lines.append(f"- {badge} {display_title(view)}: {details or 'evidence found'}. "
                         f"Close with `{view.get('confirmation_action')}`")
    summary = classified["summary"]
    if backlog_health:
        parts = [f"{label} {summary[key]}" for label, key in
                 (("watchlist", "waiting"), ("scheduled", "scheduled"), ("someday", "someday")) if summary[key]]
        parts += [f"overdue {summary['overdue_actionable']}", f"unrouted high-priority {summary['unrouted_high_priority']}"]
        lines.append("")
        lines.append("Backlog health: " + ", ".join(parts) + ".")
    if reconcile_result is not None:
        parts = [f"closed {reconcile_result.get('count', 0)}"]
        if "diagnostics" in reconcile_result:
            parts.append(f"open-after-scan {reconcile_result.get('unmatched_count', len(reconcile_result['diagnostics']))}")
        recent = [f"{entry.get('title')} ← {entry.get('reason')}" for entry in (reconcile_result.get("reconciled") or [])[:3]]
        if recent:
            parts.append("recent: " + " | ".join(recent))
        lines.append("Auto-reconcile: " + "; ".join(parts) + ".")
    if gate:
        lines.append(f"Surface verification: checked {gate.get('checked', 0)}; auto-closed {gate.get('closed', 0)}; "
                     f"confirmation needed {gate.get('confirmations', 0)}.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Route suggestions
# ---------------------------------------------------------------------------


def score_workstream(view: dict[str, Any], slug: str, config: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    title = str(view.get("title") or "").lower()
    if f"/workstreams/{slug}/" in str(view.get("source_ref") or ""):
        score += 50
        reasons.append("source_ref points into the workstream folder")
    terms = [str(term) for term in config.get("todo_match_terms") or []
             if str(term).strip() and str(term).strip().lower() in title]
    if terms:
        score += 30 + 5 * len(terms)
        reasons.append("todo_match_terms: " + ", ".join(terms))
    bits: list[str] = []
    for token in _tokens(f"{slug} {config.get('name') or ''} {config.get('description') or ''}"):
        if len(token) > 2 and token not in GENERIC_TERMS and token not in STEM_STOPWORDS and token not in bits:
            bits.append(token)
    title_tokens = set(_tokens(title))
    hits = [bit for bit in bits if bit in title_tokens]
    if len(hits) >= 2:
        score += 20 + 3 * len(hits)
        reasons.append("workstream words: " + ", ".join(hits))
    else:
        long_hits = [bit for bit in hits if len(bit) >= 8]
        if long_hits:
            score += 16
            reasons.append("workstream word: " + long_hits[0])
    if str(config.get("priority") or "").strip().lower() == "high":
        score += 4
        reasons.append("workstream priority: high")
    if str(config.get("status") or "").strip().lower() == "reference":
        score -= 8
        reasons.append("workstream status: reference")
    return score, reasons


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _gate_result() -> dict[str, Any]:
    return dict(SURFACE_GATE_RESULT)


def cmd_summary(args: argparse.Namespace) -> None:
    _, classified, reconcile = classify_payload(args.path, auto_reconcile=args.auto_reconcile,
                                                lookback_days=args.lookback_days, apply=not args.dry_run)
    limit = max(0, args.limit)
    if args.json:
        result = {
            "summary": classified["summary"],
            "top_actionable": classified["actionable"][:limit],
            "completion_confirmations": classified["completion_confirmations"][:limit],
            "triage_candidates": classified["triage_candidates"][:limit],
            "duplicates": classified["duplicates"][:limit],
            "surface_verification": _gate_result(),
        }
        if reconcile is not None:
            result["auto_reconcile"] = reconcile
        emit(result)
        return
    s = classified["summary"]
    lines = [
        f"actionable={s['actionable']} confirmation={s['confirmation']} watchlist={s['waiting']} "
        f"scheduled={s['scheduled']} someday={s['someday']} done={s['done']} overdue={s['overdue_actionable']} "
        f"unrouted_high_priority={s['unrouted_high_priority']} duplicates={s['duplicates_ignored']}"
    ]
    for index, view in enumerate(classified["actionable"][:limit], 1):
        due = f" due={view['due']}" if _clean_text(view.get("due")) else ""
        lines.append(f"{index}. {view['priority']} {display_title(view)}{due} route={view['routing_kind']}")
    for view in classified["completion_confirmations"][:limit]:
        evidence = (view.get("completion_evidence") or [{}])[0].get("detail") or "completion evidence found"
        lines.append(f"confirm-close. {view['priority']} {display_title(view)} evidence: {evidence} "
                     f"close: {view.get('confirmation_action')}")
    sys.stdout.write("\n".join(lines) + "\n")


def cmd_list(args: argparse.Namespace) -> None:
    _, classified, _ = classify_payload(args.path, auto_reconcile=args.auto_reconcile,
                                        lookback_days=args.lookback_days, apply=not args.dry_run)
    if args.bucket == "all":  # open work first, so a limit never hides it behind done rows
        selected = sorted(classified["items"], key=lambda view: view["bucket"] == "done")
    elif args.bucket == "actionable":
        selected = classified["actionable"]
    else:
        selected = [view for view in classified["items"] if view["bucket"] == args.bucket]
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]
    emit(selected if args.json else {"items": selected})


def cmd_snapshot(args: argparse.Namespace) -> None:
    payload, classified, reconcile = classify_payload(args.path, auto_reconcile=args.auto_reconcile,
                                                      lookback_days=args.lookback_days, apply=not args.dry_run)
    result = dict(classified)
    result["recurring_templates"] = payload.get("recurring_templates") or []
    result["surface_verification"] = _gate_result()
    if reconcile is not None:
        result["auto_reconcile"] = reconcile
    emit(result)


def cmd_render_markdown(args: argparse.Namespace) -> None:
    _, classified, reconcile = classify_payload(args.path, auto_reconcile=args.auto_reconcile,
                                                lookback_days=args.lookback_days, apply=not args.dry_run,
                                                diagnose=args.auto_reconcile)
    text = render_markdown(classified, args.limit, not args.no_backlog_health, reconcile, _gate_result())
    sys.stdout.write(text)


def cmd_reminders(args: argparse.Namespace) -> None:
    payload = load_data(args.path)
    classified = classify_items(payload.get("items") or [], payload.get("rules"))
    picked = reminder_views(classified, args.mode)[: max(0, args.limit)]
    emit({
        "mode": args.mode,
        "generated_at": now_z(),
        "count": len(picked),
        "completion_confirmations": classified["completion_confirmations"][: max(0, args.limit)],
        "surface_verification": _gate_result(),
        "items": [{
            "id": view.get("id"), "title": display_title(view), "raw_title": view.get("title"),
            "priority": view["priority"], "due": view.get("due"), "review_on": view.get("review_on"),
            "source_ref": view.get("source_ref"), "workstream": view.get("workstream"),
        } for view in picked],
    })


def cmd_auto_reconcile(args: argparse.Namespace) -> None:
    if args.dry_run:
        payload = load_data(args.path)
        emit(reconcile_items(payload, lookback_days=args.lookback_days, apply=False, diagnose=args.diagnose))
        return
    _, _, result = classify_payload(args.path, auto_reconcile=True, lookback_days=args.lookback_days, apply=True,
                                    diagnose=args.diagnose, materialize=False)
    emit(result)


def cmd_sync_calendar(args: argparse.Namespace) -> None:
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        generated = materialize_templates(payload)
        if args.id is not None:
            target = find_item(payload, args.id, f"todo item not found: {args.id}")["id"]
        else:
            target = None
        result = sync_calendar_payload(payload, target)
        if generated or result["updated"] or result["unsynced"]:
            save_data(args.path, payload)
    emit({key: value for key, value in result.items() if key != "matched"})


def _resolve_id(args: argparse.Namespace) -> str:
    value = args.id if args.id is not None else getattr(args, "id_positional", None)
    if not value:
        raise SystemExit(_usage_exit(args, "the following arguments are required: --id"))
    return str(value).strip()


def _usage_exit(args: argparse.Namespace, message: str) -> int:
    parser = getattr(args, "_parser", None)
    if parser is not None:
        parser.error(message)
    sys.stderr.write(f"error: {message}\n")
    return 2


def cmd_add(args: argparse.Namespace) -> None:
    if args.estimate_min is not None and _positive_int(args.estimate_min) is None:
        raise TodoError("--estimate-min must be a positive integer")
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        materialize_templates(payload)
        raw_ref = (args.source_ref or "").strip()
        canonical = canonicalize_source_ref(raw_ref)
        if raw_ref and classify_source_ref(canonical) is None and classify_source_ref(raw_ref) is None:
            sys.stderr.write(f"warn: source_ref does not match a canonical pattern: {raw_ref}\n")
        item: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "title": args.title.strip(),
            "source": (args.source or "manual").strip() or "manual",
            "source_ref": (canonical or raw_ref) or None,
            "priority": normalize_priority(args.priority),
            "status": normalize_status(args.status),
            "due": _clean_text(args.due),
            "review_on": _clean_text(args.review_on),
            "snooze_until": _clean_text(args.snooze_until),
            "created_at": now_z(),
            "completed_at": None,
            "context": _clean_text(args.context),
            "tags": [tag.strip() for tag in args.tag or [] if tag and tag.strip()],
            "workstream": _clean_text(args.workstream),
            "project": _clean_text(args.project),
            "estimate_min": _positive_int(args.estimate_min),
            "requires_user": False,
            "calendar_event_id": None,
            "calendar_event_url": None,
            "calendar_event_start": None,
            "calendar_event_end": None,
            "calendar_synced_at": None,
        }
        explicit = parse_requires_user(args.requires_user)
        item["requires_user"] = explicit if explicit is not None else infer_requires_user(item)
        item["estimate_min"] = hydrate_estimate(item)
        routed = any(item.get(key) for key in ("due", "review_on", "workstream", "project"))
        if item["priority"] in ("P0", "P1") and not routed and not args.allow_unrouted:
            raise TodoError(f"{item['priority']} to-dos need routing: pass --due, --review-on, --workstream, or "
                            "--project (or --allow-unrouted to capture it anyway)")
        duplicate = find_duplicate(payload["items"], item)
        if duplicate is not None and not args.allow_duplicate:
            emit({"duplicate_skipped": True, "existing": normalized_copy(duplicate)})
            return
        payload["items"].append(item)
        hold = sync_calendar_hold_if_needed(item, _used_event_ids(payload) - {item.get("calendar_event_id")})
        save_data(args.path, payload)
    emit({"added": True, "item": item, "calendar_sync": hold})


def cmd_complete(args: argparse.Namespace) -> None:
    item_id = _resolve_id(args)
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        materialize_templates(payload)
        item = find_item(payload, item_id, f"Item not found: {item_id}")
        check_pr_set(payload, item, args.reason, args.allow_pr_mismatch, Path(args.path))
        advance = complete_in_place(payload, item, args.reason)
        save_data(args.path, payload)
    emit({"completed": True, "item": item, "template_advance": advance})


def cmd_set_status(args: argparse.Namespace) -> None:
    item_id = _resolve_id(args)
    raw_status = args.status if args.status is not None else getattr(args, "status_positional", None)
    if not raw_status:
        raise SystemExit(_usage_exit(args, "the following arguments are required: --status"))
    if args.estimate_min is not None and _positive_int(args.estimate_min) is None:
        raise TodoError("--estimate-min must be a positive integer")
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        materialize_templates(payload)
        item = find_item(payload, item_id, f"Item not found: {item_id}")
        status = normalize_status(raw_status)
        if status == "done":
            check_pr_set(payload, item, args.reason, args.allow_pr_mismatch, Path(args.path))
        item["status"] = status
        if args.reason:
            item["resolution"] = args.reason
            if str(raw_status).strip().lower() == "dropped":
                item["dropped_reason"] = args.reason
        if args.priority is not None:
            item["priority"] = normalize_priority(args.priority)
        for flag, key in (("due", "due"), ("review_on", "review_on"), ("snooze_until", "snooze_until"),
                          ("project", "project")):
            value = _optional_text(getattr(args, flag))
            if value is not None:
                item[key] = value or None
        workstream = _optional_text(args.workstream)
        if workstream is not None:
            changed = (workstream or None) != _clean_text(item.get("workstream"))
            item["workstream"] = workstream or None
            if changed and args.estimate_min is None:
                item["estimate_min"] = None
        if args.estimate_min is not None:
            item["estimate_min"] = _positive_int(args.estimate_min)
        if args.requires_user is not None:
            item["requires_user"] = parse_requires_user(args.requires_user)
        item["estimate_min"] = hydrate_estimate(item)
        advance = None
        if status == "done":
            item["completed_at"] = now_z()
            item["snooze_until"] = None
            delete_calendar_hold(item)
            advance = advance_template(payload, item)
        else:
            item["completed_at"] = None
            sync_calendar_hold_if_needed(item, _used_event_ids(payload) - {item.get("calendar_event_id")})
        save_data(args.path, payload)
    emit({"updated": True, "item": item, "template_advance": advance})


def cmd_snooze(args: argparse.Namespace) -> None:
    item_id = _resolve_id(args)
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        materialize_templates(payload)
        item = find_item(payload, item_id, f"Item not found: {item_id}")
        item["status"] = "scheduled"
        item["snooze_until"] = args.until
        sync_calendar_hold_if_needed(item, _used_event_ids(payload) - {item.get("calendar_event_id")})
        save_data(args.path, payload)
    emit({"snoozed": True, "item": item})


def _archive_dir(args: argparse.Namespace) -> Path:
    return Path(args.archive_dir) if args.archive_dir else Path(args.path).parent / "todo-archive"


def cmd_archive_done(args: argparse.Namespace) -> None:
    if args.days < 0 or args.batch_size < 1:
        raise TodoError("--days must be 0 or more and --batch-size must be 1 or more")
    cutoff = utc_now() - timedelta(days=args.days)
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        selected = []
        for item in payload["items"]:
            if len(selected) >= args.batch_size:
                break
            if not isinstance(item, dict) or normalize_status(item.get("status")) != "done":
                continue
            finished = parse_moment(item.get("completed_at") or item.get("completed"))
            if finished is not None and finished < cutoff:
                selected.append(item)
        result = archive_items(Path(args.path), _archive_dir(args), selected,
                               f"completed more than {args.days} days ago")
    emit(result)


def cmd_archive_stale(args: argparse.Namespace) -> None:
    if args.days < 0 or args.batch_size < 1:
        raise TodoError("--days must be 0 or more and --batch-size must be 1 or more")
    wanted = _priority_set(args.priority, "P2,P3")
    cutoff = utc_now() - timedelta(days=args.days)
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        selected = []
        for item in payload["items"]:
            if len(selected) >= args.batch_size:
                break
            if not isinstance(item, dict) or normalize_status(item.get("status")) == "done":
                continue
            if normalize_priority(item.get("priority")) not in wanted:
                continue
            touched = _last_touched(item)
            if touched is not None and touched < cutoff:
                selected.append(item)
        result = archive_items(Path(args.path), _archive_dir(args), selected,
                               f"{args.priority} todo untouched for more than {args.days} days")
    emit(result)


def cmd_restore_archived(args: argparse.Namespace) -> None:
    folder = _archive_dir(args)
    if args.month:
        files = [folder / f"{args.month}.json"]
    else:
        files = sorted(folder.glob("*.json"), key=lambda path: path.name, reverse=True) if folder.is_dir() else []
    with exclusive_lock(args.path):
        for archive_path in files:
            if not archive_path.exists():
                continue
            archive = _load_archive(archive_path, archive_path.stem)
            for index, record in enumerate(archive["records"]):
                if not isinstance(record, dict) or str(record.get("todo_id")) != args.id:
                    continue
                item = record.get("item")
                if not isinstance(item, dict):
                    raise TodoError(f"archived record for {args.id} has no item in {archive_path}")
                payload = load_data(args.path)
                active = _find_by_id(payload, args.id)
                if active is not None and not _same(active, item):
                    raise TodoError(f"an active todo with id {args.id} has different content; nothing was restored")
                if active is None:
                    payload["items"].append(item)
                    save_data(args.path, payload)
                del archive["records"][index]
                archive["updated_at"] = now_z()
                atomic_write_text(archive_path, _dump(archive))
                emit({"restored": True, "item": item, "archive": str(archive_path)})
                return
    raise TodoError(f"archived todo not found: {args.id}")


def cmd_checkpoint(args: argparse.Namespace) -> None:
    payload = load_data(args.path)
    classified = classify_items(payload.get("items") or [], payload.get("rules"))
    wanted = _priority_set(args.priority, "P2,P3")
    stale = stale_entries(classified["items"], args.days, wanted)
    emit({
        "summary": classified["summary"],
        "stale_actionable": stale[: max(0, args.limit)],
        "completion_confirmations": classified["completion_confirmations"][: max(0, args.limit)],
        "surface_verification": _gate_result(),
    })


def cmd_suggest_routes(args: argparse.Namespace) -> None:
    root = Path(args.workstreams_root) if args.workstreams_root else Path(WORKSTREAMS_DIR)
    if not root.is_dir():
        raise TodoError(f"workstreams folder not found: {root}")
    payload = load_data(args.path)
    classified = classify_items(payload.get("items") or [], payload.get("rules"))
    wanted = _priority_set(args.priority, "P0,P1")
    streams = []
    for folder in sorted(root.iterdir(), key=lambda path: path.name):
        if not folder.is_dir() or folder.name == "shared" or folder.name.startswith((".", "_")):
            continue
        streams.append((folder.name, load_workstream_config(folder.name, root)))
    suggestions = []
    for view in classified["actionable"]:
        if _clean_text(view.get("workstream")) or view["priority"] not in wanted:
            continue
        matches = []
        for slug, config in streams:
            score, reasons = score_workstream(view, slug, config)
            if score >= args.min_score:
                matches.append({"slug": slug, "name": config.get("name") or slug, "score": score, "reasons": reasons,
                                "status": config.get("status"), "priority": config.get("priority")})
        matches.sort(key=lambda match: (-match["score"], match["slug"]))
        if matches:
            suggestions.append({"id": view.get("id"), "title": view.get("title"), "priority": view["priority"],
                                "routing_kind": view["routing_kind"], "top_matches": matches[: args.top_matches]})
    emit(suggestions[: max(0, args.limit)])


def cmd_add_template(args: argparse.Namespace) -> None:
    raw = {
        "template_id": args.template_id, "title": args.title, "recurrence_rule": args.recurrence_rule,
        "next_due": args.next_due, "materialization_window_days": args.materialization_window_days,
        "priority": args.priority, "workstream": args.workstream, "project": args.project,
        "estimate_min": args.estimate_min, "context": args.context, "tags": list(args.tag or []),
        "source": args.source,
    }
    try:
        template = normalize_template(raw)
    except ValueError as exc:
        raise TodoError(str(exc)) from exc
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        templates = payload.setdefault("recurring_templates", [])
        replaced = False
        for index, existing in enumerate(templates):
            try:
                current = normalize_template(existing)
            except ValueError:
                continue
            if current["template_id"] == template["template_id"]:
                template["last_materialized_at"] = current.get("last_materialized_at")
                templates[index] = template
                replaced = True
                break
        if not replaced:
            templates.append(template)
        save_data(args.path, payload)
    emit({"template": template, "updated": replaced})


def cmd_materialize_templates(args: argparse.Namespace) -> None:
    with exclusive_lock(args.path):
        payload = load_data(args.path)
        generated = materialize_templates(payload)
        if generated:
            save_data(args.path, payload)
    emit({"count": len(generated), "items": generated})


def cmd_list_templates(args: argparse.Namespace) -> None:
    payload = load_data(args.path)
    normalized = []
    for raw in payload.get("recurring_templates") or []:
        try:
            normalized.append(normalize_template(raw))
        except ValueError:
            continue
    emit(normalized)


# ---------------------------------------------------------------------------
# Argument parser and entry point
# ---------------------------------------------------------------------------


def _add_view_flags(parser: argparse.ArgumentParser, dry_run_help: str) -> None:
    parser.add_argument("--auto-reconcile", action="store_true",
                        help="Run the full reconcile scan and close matches before showing the view.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help="How far back the reconcile scan looks for evidence (days).")
    parser.add_argument("--dry-run", action="store_true", help=dry_run_help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo_cli.py", description="Persistent to-do list with verified views.")
    parser.add_argument("--path", default=None, help="To-do file (default: system/state/todo.json under the repo).")
    parser.add_argument("--archive-dir", default=None, help="Archive folder (default: todo-archive next to the file).")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    no_write = "Show what would change without writing anything."

    view = commands.add_parser("summary", help="One-line counts plus the top actionable items.")
    view.add_argument("--limit", type=int, default=5, help="How many items to list.")
    view.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    _add_view_flags(view, no_write)
    view.set_defaults(handler=cmd_summary)

    view = commands.add_parser("list", help="Items in one bucket.")
    view.add_argument("--bucket", default="actionable",
                      choices=["actionable", "confirmation", "waiting", "scheduled", "someday", "done", "all"])
    view.add_argument("--limit", type=int, default=10, help="How many items to return (0 means all).")
    view.add_argument("--json", action="store_true", help="Print a bare JSON array.")
    _add_view_flags(view, no_write)
    view.set_defaults(handler=cmd_list)

    view = commands.add_parser("snapshot", help="The full classification as JSON.")
    _add_view_flags(view, no_write)
    view.set_defaults(handler=cmd_snapshot)

    view = commands.add_parser("render-markdown", help="Tables of the top items for a person to read.")
    view.add_argument("--limit", type=int, default=5, help="How many items to show.")
    view.add_argument("--no-backlog-health", action="store_true", help="Leave out the backlog health line.")
    _add_view_flags(view, no_write)
    view.set_defaults(handler=cmd_render_markdown)

    view = commands.add_parser("reminders", help="Morning, end-of-day, or weekly reminder sets.")
    view.add_argument("--mode", required=True, choices=["morning", "eod", "weekly"])
    view.add_argument("--limit", type=int, default=3, help="How many reminders to return.")
    view.set_defaults(handler=cmd_reminders)

    view = commands.add_parser("auto-reconcile", help="Scan every open item for completion evidence.")
    view.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    view.add_argument("--dry-run", action="store_true", help="Scan only, close nothing.")
    view.add_argument("--diagnose", action="store_true", help="List the checks tried on items left open.")
    view.set_defaults(handler=cmd_auto_reconcile)

    view = commands.add_parser("sync-calendar", help="Create, move, or clear calendar holds.")
    view.add_argument("--id", default=None, help="Sync one item instead of the whole list.")
    view.set_defaults(handler=cmd_sync_calendar)

    view = commands.add_parser("add", help="Capture a to-do (deduped).")
    view.add_argument("--title", required=True)
    view.add_argument("--source", default="manual")
    view.add_argument("--source-ref", default=None)
    view.add_argument("--priority", default="P3")
    view.add_argument("--status", default="open")
    view.add_argument("--due", default=None)
    view.add_argument("--review-on", default=None)
    view.add_argument("--snooze-until", default=None)
    view.add_argument("--workstream", default=None)
    view.add_argument("--project", default=None)
    view.add_argument("--estimate-min", type=int, default=None)
    view.add_argument("--context", default=None)
    view.add_argument("--tag", action="append", default=[])
    view.add_argument("--requires-user", default=None, help="true/false style token; inferred when left out.")
    view.add_argument("--allow-duplicate", action="store_true", help="Add even when a matching item exists.")
    view.add_argument("--allow-unrouted", action="store_true", help="Allow a P0 or P1 without any routing.")
    view.set_defaults(handler=cmd_add)

    view = commands.add_parser("complete", help="Mark an item done.")
    view.add_argument("id_positional", nargs="?", default=None, metavar="ID", help=argparse.SUPPRESS)
    view.add_argument("--id", default=None)
    view.add_argument("--reason", default=None)
    view.add_argument("--allow-pr-mismatch", action="store_true", help="Skip the PR-set guard.")
    view.set_defaults(handler=cmd_complete)

    view = commands.add_parser("set-status", help="Change status and optional fields.")
    view.add_argument("id_positional", nargs="?", default=None, metavar="ID", help=argparse.SUPPRESS)
    view.add_argument("status_positional", nargs="?", default=None, metavar="STATUS", help=argparse.SUPPRESS)
    view.add_argument("--id", default=None)
    view.add_argument("--status", default=None)
    view.add_argument("--reason", default=None)
    view.add_argument("--allow-pr-mismatch", action="store_true")
    view.add_argument("--priority", default=None)
    view.add_argument("--due", default=None, help="An empty value clears it.")
    view.add_argument("--review-on", default=None, help="An empty value clears it.")
    view.add_argument("--snooze-until", default=None, help="An empty value clears it.")
    view.add_argument("--workstream", default=None)
    view.add_argument("--project", default=None)
    view.add_argument("--estimate-min", type=int, default=None)
    view.add_argument("--requires-user", default=None)
    view.set_defaults(handler=cmd_set_status)

    view = commands.add_parser("snooze", help="Hide an item until a time.")
    view.add_argument("id_positional", nargs="?", default=None, metavar="ID", help=argparse.SUPPRESS)
    view.add_argument("--id", default=None)
    view.add_argument("--until", required=True, help="Stored exactly as given.")
    view.set_defaults(handler=cmd_snooze)

    view = commands.add_parser("archive-done", help="Move old done items to the monthly archive.")
    view.add_argument("--days", type=int, default=7)
    view.add_argument("--batch-size", type=int, default=100)
    view.set_defaults(handler=cmd_archive_done)

    view = commands.add_parser("archive-stale", help="Move untouched low-priority items to the archive.")
    view.add_argument("--days", type=int, default=14)
    view.add_argument("--priority", default="P2,P3")
    view.add_argument("--batch-size", type=int, default=25)
    view.set_defaults(handler=cmd_archive_stale)

    view = commands.add_parser("restore-archived", help="Bring an archived item back.")
    view.add_argument("--id", required=True)
    view.add_argument("--month", default=None, help="YYYY-MM archive to search (default: all, newest first).")
    view.set_defaults(handler=cmd_restore_archived)

    view = commands.add_parser("checkpoint", help="Stale work and confirmations for a scheduled check.")
    view.add_argument("--days", type=int, default=7)
    view.add_argument("--priority", default="P2,P3")
    view.add_argument("--limit", type=int, default=20)
    view.set_defaults(handler=cmd_checkpoint)

    view = commands.add_parser("suggest-routes", help="Suggest workstreams for unrouted high-priority items.")
    view.add_argument("--priority", default="P0,P1")
    view.add_argument("--limit", type=int, default=10)
    view.add_argument("--top-matches", type=int, default=3)
    view.add_argument("--min-score", type=int, default=18)
    view.add_argument("--workstreams-root", default=None)
    view.set_defaults(handler=cmd_suggest_routes)

    view = commands.add_parser("add-template", help="Add or replace a recurring template.")
    view.add_argument("--template-id", required=True)
    view.add_argument("--title", required=True)
    view.add_argument("--recurrence-rule", required=True, help="weekly:<weekday> or monthly:<day>.")
    view.add_argument("--next-due", required=True)
    view.add_argument("--materialization-window-days", type=int, default=3)
    view.add_argument("--priority", default="P3")
    view.add_argument("--workstream", default=None)
    view.add_argument("--project", default=None)
    view.add_argument("--estimate-min", type=int, default=None)
    view.add_argument("--context", default=None)
    view.add_argument("--tag", action="append", default=[])
    view.add_argument("--source", default="recurring-template")
    view.set_defaults(handler=cmd_add_template)

    view = commands.add_parser("materialize-templates", help="Create items for due templates.")
    view.set_defaults(handler=cmd_materialize_templates)

    view = commands.add_parser("list-templates", help="Print the normalized templates.")
    view.set_defaults(handler=cmd_list_templates)
    return parser


def main() -> None:
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args()
    args._parser = parser
    args.path = Path(args.path).expanduser() if args.path else Path(TODO_PATH)
    if args.archive_dir:
        args.archive_dir = Path(args.archive_dir).expanduser()
    try:
        if args.command in SURFACE_COMMANDS:
            run_live_gate(args)
        verify_before_surface(args)
        args.handler(args)
    except PrSetRefusal as refusal:
        emit(refusal.payload)
        sys.exit(3)
    except TodoError as error:
        sys.stderr.write(f"{error}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
