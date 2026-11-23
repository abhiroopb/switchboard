#!/usr/bin/env python3
"""Decide which workstream tabs start-of-day opens and what each tab is seeded with.

Rule: treat workspaces like a to-do list. Open only the
workstreams with something for today and seed each one with a short note:
to-dos for today, anything urgent or important missed yesterday, people to
reply to, and links to relevant docs. Everything else stays closed; the
next-up digest and comms triage still surface it.

A workstream opens today when at least one of these holds:

  due today      a linked to-do (open, actionable, waiting, scheduled, not
                 snoozed) has `due` or `review_on` equal to the plan date
  P0             a linked P0 to-do is surfaced (system/scripts/lib/p0-todos.py)
  meeting today  a calendar event on the plan date matches the workstream
                 (signal_match_terms, todo_match_terms, display name, or slug)
  reply pending  a comms candidate directed at the owner that reads like an ask
                 (Slack: a question mark or an ask phrase; Gmail and Doc
                 comments: the collector's response_expected), received within
                 the reply window, linked to this workstream through an open
                 to-do (source_ref), an explicit `workspace`, or a single
                 workstream hint, and nothing shows the owner answered it (todo
                 done, ledger draft sent, ledger stage done/dropped, mention
                 marked replied, resolution cache). Asks with no workstream
                 are listed under replies_unattributed and open nothing.

Overdue P0/P1 to-dos from yesterday (Friday on a Monday) fill "Missed
yesterday" but do not by themselves open a tab. Calendar holds the todo
system creates ("[Todo] ...") are not meetings.

Seed note: headings in this order, empty ones omitted, about 12 lines total.

  To dos today
  Missed yesterday (urgent or important only)
  Reply to
  Docs

Outputs
  default        plan JSON on stdout
  --dry-run      human-readable plan, opens and writes nothing
  --write        writes system/today-plan.json in the launch schema
                 start-day.sh phase 5 already consumes (every entry open_now)

Inputs are read-only: system/state/todo.json, the live calendar through
daemon/calendar-api.sh (or --calendar-json / --no-calendar),
system/state/sources/comms.json, system/state/triage-ledger.json,
.state/thread-resolution-cache.json, system/state/sources/owner-mentions.jsonl
and every workstreams/<slug>/config.yaml.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from workstream_metadata import TERMINAL_STATUSES, load_workstream_metadata  # noqa: E402
import named_refs  # noqa: E402

DEFAULT_REPO_ROOT = SCRIPT_DIR.parents[1]
PT = ZoneInfo("America/Los_Angeles")
# Owner identity strings as they appear in Slack/Gmail exports (rendered at install).
OWNER_NAMES = {s.lower() for s in ("{{OWNER_NAME_LOWER}}", "{{OWNER_USER}}", "{{OWNER_EMAIL}}")}
BOT_SENDERS = {"buildbot", "slackbot", "github", "linear", "jira"}
OPEN_TODO_STATUSES = {"open", "actionable", "waiting", "scheduled"}
CLOSED_TODO_STATUSES = {"done", "dropped"}
IMPORTANT = {"P0", "P1"}
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
REPLY_SOURCE_TYPES = {"slack", "gmail", "google docs"}
ANSWERED_VERDICTS = {"resolved_by_owner", "resolved_by_reaction", "resolved_by_other"}
# Ledger dismissal reasons that mean the ask was answered (triage_ledger.ANSWERED_DISMISS_REASONS)
# or was never an ask (triage_ledger.AUTO_DISMISS_REASONS: the already-answered pre-check
# classified it no_ask, settled, or stale).
ANSWERED_DISMISS_REASONS = {"owner-already-replied", "owner-marked-resolved", "resolved-by-other", "no-ask", "settled", "stale"}
# reply_check classifications that mean "never present this as an ask".
NON_OPEN_CLASSIFICATIONS = {"answered", "no_ask", "settled", "stale"}
REPLY_CHECK_SCRIPT = SCRIPT_DIR / "reply_check.py"
MAX_SEED_LINES = 12
SECTION_CAPS = {"todos": 4, "missed": 2, "reply": 3, "docs": 3}
TRIM_ORDER = ("docs", "missed", "todos", "reply")
ASK_MAX_CHARS = 90
# A Slack mention counts as "needs a reply" only when it reads like an ask.
# Gmail and Doc comments carry response_expected from the collector instead.
ASK_PHRASES = (
    "can you", "could you", "would you", "will you", "do you", "did you", "are you",
    "please", "thoughts", "wdyt", "lmk", "let me know", "kindly", "if you wanna",
    "if you want to", "to confirm", "should i", "is that", "any update",
)
# Calendar holds the todo system places for itself are not meetings.
CALENDAR_HOLD_PREFIXES = ("[todo]", "(todo)")

HEADINGS = {
    "todos": "To dos today",
    "missed": "Missed yesterday (urgent or important only)",
    "reply": "Reply to",
    "docs": "Docs",
}
SECTION_ORDER = ("todos", "missed", "reply", "docs")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered before exec
    try:
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - optional dependency
        sys.modules.pop(name, None)
        return None
    return module


def date_only(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def normalize_ref(ref: Any) -> str:
    """Slack permalinks carry ?thread_ts=... on candidates but not on todos."""
    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith("http"):
        parts = urlsplit(text)
        host = parts.netloc.lower()
        path = parts.path.rstrip("/")
        if "slack.com" in host:
            return f"slack:{path}"
        if "docs.google.com" in host:
            disco = re.search(r"disco=([A-Za-z0-9_-]+)", text)
            return f"{host}{path}" + (f"#{disco.group(1)}" if disco else "")
        return f"{host}{path}"
    return text


def clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace(" — ", ": ").replace("—", ":").replace("–", "-").replace("!", ".")
    return text.replace("[", "(").replace("]", ")")


def sender_label(raw: Any) -> str:
    text = clean_text(raw)
    if "@" in text and " " not in text:
        text = text.split("@", 1)[0]
    return text


def is_owner(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    return not text or text in OWNER_NAMES or text in BOT_SENDERS


def inline_link(text: str, url: str) -> str:
    text = clean_text(text) or "link"
    # A bare PR, issue, or Linear key in the label leads with its name and
    # keeps the number in parentheses. The named text already links the thing itself; the source link
    # is kept beside it when it points somewhere else.
    named = named_refs.name_refs(text, tasks=False)
    if named != text:
        if url.startswith("http") and url not in named:
            return f"{named} ([source]({url}))"
        return named
    return f"[{text}]({url})" if url.startswith("http") else text


def strip_slack_markup(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<@[A-Z0-9]+>", "", text)
    text = re.sub(r"<#[A-Z0-9]+\|?([^>]*)>", lambda m: "#" + m.group(1) if m.group(1) else "", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r":[a-z0-9_+-]+:", "", text)
    return text


def shorten(text: str, limit: int = ASK_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return (cut or text[:limit]) + "..."


def looks_like_ask(candidate: dict[str, Any]) -> bool:
    """Slack mentions need a question or ask phrase; other sources trust the collector."""
    if str(candidate.get("source_type") or "").lower() != "slack":
        return True
    body = strip_slack_markup(str(candidate.get("body") or candidate.get("summary") or "")).lower()
    if "?" in body:
        return True
    return any(phrase in body for phrase in ASK_PHRASES)


def message_ask(candidate: dict[str, Any]) -> str:
    """One short line of what the sender asked, from the message itself."""
    body = strip_slack_markup(str(candidate.get("body") or candidate.get("summary") or ""))
    body = re.sub(r"^\s*(hi|hey|hello)\b[^,.:!-]*[,.:!-]?\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"https?://\S+", "", body)
    body = re.sub(r"\b[\w.-]+\.(?:com|co|net|org|io|dev)(?:/\S*)?", "", body)
    body = re.sub(r"(?<!\w)" + re.escape("{{OWNER_NAME_LOWER}}") + r"(?!\w)\s*[,.:-]?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s+([?.,;:)])", r"\1", body)
    return shorten(clean_text(body))


def plan_dates(plan_date: date) -> tuple[str, str]:
    """Return (missed_floor, reply_floor) ISO dates. Monday reaches back to Friday."""
    back = 3 if plan_date.weekday() == 0 else 1
    yesterday = plan_date - timedelta(days=back)
    return yesterday.isoformat(), yesterday.isoformat()


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def workstream_display(meta: dict[str, Any]) -> str:
    return str(meta.get("display_name") or meta.get("slug"))


def tab_title(meta: dict[str, Any], p0: bool) -> str:
    emoji = str(meta.get("emoji") or "").strip()
    name = workstream_display(meta)
    if p0:
        return f"{emoji} P0 · {name}" if emoji else f"🔥 P0 · {name}"
    return f"{emoji} {name}".strip()


def load_workstreams(repo_root: Path) -> list[dict[str, Any]]:
    items = []
    for meta in load_workstream_metadata(repo_root):
        if meta.get("status") in TERMINAL_STATUSES or meta.get("status") == "reference":
            continue
        meta = dict(meta)
        meta["emoji"] = read_config_emoji(repo_root / "workstreams" / meta["slug"] / "config.yaml")
        items.append(meta)
    return items


def read_config_emoji(config_path: Path) -> str:
    try:
        for line in config_path.read_text().splitlines():
            if line.startswith("emoji:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def surfaced_p0_ids(todos: list[dict[str, Any]], plan_date: date, now_iso: str) -> set[str]:
    module = load_script(SCRIPT_DIR / "lib" / "p0-todos.py", "switchboard_p0_todos")
    if module is None:
        return set()
    return {t["id"] for t in module.surfaced_p0s(todos, now_iso=now_iso, today=plan_date.isoformat())}


def load_calendar(repo_root: Path, plan_date: date, calendar_json: Path | None, no_calendar: bool) -> tuple[list[dict[str, Any]], str]:
    if no_calendar:
        return [], "skipped"
    module = load_script(repo_root / "system" / "scripts" / "pending-calendar-invites.py", "switchboard_pending_calendar")
    if module is None:
        return [], "unavailable"
    start = datetime.combine(plan_date, time.min, tzinfo=PT)
    end = start + timedelta(days=1)
    if calendar_json is not None:
        payload, source = load_json(calendar_json, None), f"file {calendar_json}"
    else:
        payload, source = module.fetch_live_events(start, end, "America/Los_Angeles")
    if payload is None:
        return [], source
    events = []
    for event in module.normalize_events(payload, PT):
        if event.status == "cancelled" or event.response_status == "declined":
            continue
        if event.end <= start or event.start >= end:
            continue
        events.append({"summary": event.summary, "start": event.start.isoformat(), "all_day": event.all_day, "link": event.link})
    events.sort(key=lambda e: e["start"])
    return events, source


def load_channel_names(repo_root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    mentions = repo_root / "system" / "state" / "sources" / "owner-mentions.jsonl"
    try:
        for line in mentions.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("channel_id") and row.get("channel"):
                names[str(row["channel_id"])] = str(row["channel"])
    except OSError:
        pass
    config = load_yaml_safe(repo_root / "system" / "config" / "priority-channels.yaml")
    for tier in ("tier_1", "tier_2"):
        for entry in config.get(tier) or []:
            if isinstance(entry, dict) and entry.get("id") and entry.get("name"):
                names.setdefault(str(entry["id"]), str(entry["name"]))
    return names


def load_yaml_safe(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_replied_refs(repo_root: Path) -> set[str]:
    """Refs the mention collector already saw the owner answer."""
    replied: set[str] = set()
    mentions = repo_root / "system" / "state" / "sources" / "owner-mentions.jsonl"
    try:
        for line in mentions.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("owner_already_replied") is True and row.get("source_ref"):
                replied.add(normalize_ref(row["source_ref"]))
    except OSError:
        pass
    return replied


def load_resolution_cache(repo_root: Path) -> dict[str, str]:
    """Cached verdicts: the Slack thread gate plus the cross-source reply check."""
    cache = load_json(repo_root / ".state" / "thread-resolution-cache.json", {})
    verdicts: dict[str, str] = {}
    if isinstance(cache, dict):
        for row in cache.values():
            if isinstance(row, dict) and row.get("permalink"):
                verdicts[normalize_ref(row["permalink"])] = str(row.get("verdict") or "")
    reply_cache = load_json(repo_root / ".state" / "reply-check-cache.json", {})
    if isinstance(reply_cache, dict):
        for row in reply_cache.values():
            if not isinstance(row, dict) or not row.get("ref"):
                continue
            if str(row.get("classification") or "") in NON_OPEN_CLASSIFICATIONS - {"answered"}:
                # A no_ask / settled / stale classification is recorded as its own verdict so
                # the seed drops the item like an answered one.
                verdicts[normalize_ref(row["ref"])] = str(row["classification"])
            elif str(row.get("verdict") or "") not in ("", "unknown"):
                verdicts[normalize_ref(row["ref"])] = str(row.get("verdict") or "")
    return verdicts


def ledger_item_answered(ledger: dict[str, Any]) -> bool:
    """True when the ledger already proved this ask answered (superseded draft) or the
    already-answered pre-check auto-dismissed it before a draft existed."""
    if isinstance(ledger.get("superseded"), dict):
        return True
    if str((ledger.get("meta") or {}).get("already_answered_classification") or "") in NON_OPEN_CLASSIFICATIONS:
        return True
    return (
        ledger.get("draft_status") == "dismissed"
        and str((ledger.get("draft_meta") or {}).get("reason") or "") in ANSWERED_DISMISS_REASONS
    )


_reply_check_module: Any = None


def reply_check_module():
    """reply_check.py loaded once (shared owner-identity rules), or None when unavailable."""
    global _reply_check_module
    if _reply_check_module is None:
        _reply_check_module = load_script(REPLY_CHECK_SCRIPT, "sod_reply_check") or False
    return _reply_check_module or None


def load_reply_checker(budget_seconds: float):
    """Live already-answered checker (reply_check.bounded_checker) or None when unavailable."""
    module = reply_check_module()
    if module is None:
        return None
    return module.bounded_checker(budget_seconds)


def github_todo_live(
    todo: dict[str, Any],
    reply_checker: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """The live verdict for a todo whose source_ref is a GitHub URL, else None."""
    if reply_checker is None:
        return None
    module = reply_check_module()
    if module is None:
        return None
    try:
        if module.source_kind(todo) != "github":
            return None
        live = reply_checker(todo)
    except Exception:  # pragma: no cover - a provider failure must never break the plan
        return None
    return live if isinstance(live, dict) else None


def github_todo_resolved(
    todo: dict[str, Any],
    reply_checker: Callable[[dict[str, Any]], dict[str, Any]] | None,
    live: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Live PR/issue re-check for a todo whose source_ref is a GitHub URL.

    A stale note can call a merged PR unmerged. A todo backed by a PR or issue is re-read via ``reply_checker`` (the
    reply_check github provider, cached and backoff-aware) before it is
    seeded; MERGED/CLOSED returns {id, title, url, label} so the plan can
    list it as resolved instead of asking. None keeps the todo. Pass ``live``
    to reuse a verdict already fetched for this todo.
    """
    if live is None:
        live = github_todo_live(todo, reply_checker)
    module = reply_check_module()
    if module is None or live is None:
        return None
    if str(live.get("verdict") or "") not in ANSWERED_VERDICTS:
        return None
    return {
        "id": str(todo.get("id") or ""),
        "title": str(todo.get("title") or "Untitled"),
        "url": str(todo.get("source_ref") or ""),
        "label": str(module.answered_label(live) or "already resolved"),
    }


def github_todo_not_asked(
    todo: dict[str, Any],
    live: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """A review-framed GitHub to-do whose live PR does not request the owner's review.

    Nothing asks the owner to review a PR unless they were explicitly asked. ``live`` is the verdict ``github_todo_resolved`` already fetched for
    this to-do; when the PR is still open and ``reply_check.review_ask_status``
    says ``not-asked`` (author, team fan-out, nobody requested them) the to-do is
    dropped from the seed and listed for relabeling. ``unverified`` (no live
    role: offline, rate-limited, an issue) keeps the to-do; the plan never drops
    on a guess.
    """
    if not isinstance(live, dict):
        return None
    module = reply_check_module()
    if module is None:
        return None
    try:
        status = module.review_ask_status(str(todo.get("title") or ""), live)
    except Exception:  # pragma: no cover - defensive, the gate must never break the plan
        return None
    if status != "not-asked":
        return None
    role = str((live.get("evidence") or {}).get("owner_role") or "")
    return {
        "id": str(todo.get("id") or ""),
        "title": str(todo.get("title") or "Untitled"),
        "url": str(todo.get("source_ref") or ""),
        "label": f"the owner's role on the live PR is {role}, not requested reviewer",
    }


def candidate_is_own_message(candidate: dict[str, Any]) -> bool:
    """the owner authored the candidate by identity (Slack user id/username, email), never display name."""
    module = reply_check_module()
    if module is None:
        return False
    try:
        return bool(module.own_message_author(candidate))
    except Exception:  # pragma: no cover - defensive, identity check must never break the plan
        return False


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def todo_is_open(todo: dict[str, Any], now_iso: str) -> bool:
    if todo.get("status") not in OPEN_TODO_STATUSES:
        return False
    snooze = str(todo.get("snooze_until") or "")
    return not (snooze and snooze > now_iso)


def todo_rank(todo: dict[str, Any]) -> tuple[int, str, str]:
    return (PRIORITY_RANK.get(str(todo.get("priority")), 9), date_only(todo.get("due")) or "9999", str(todo.get("title") or ""))


def meeting_matches(meta: dict[str, Any], summary: str) -> bool:
    text = " " + re.sub(r"[^a-z0-9]+", " ", summary.lower()) + " "
    phrases = set(meta.get("signal_match_terms") or []) | set(meta.get("todo_match_terms") or [])
    phrases.add(workstream_display(meta).lower())
    phrases.add(str(meta["slug"]).replace("-", " "))
    for phrase in phrases:
        phrase = " " + re.sub(r"[^a-z0-9]+", " ", str(phrase).lower()).strip() + " "
        if len(phrase.strip()) >= 4 and phrase in text:
            return True
    return False


def candidate_received(candidate: dict[str, Any]) -> str:
    return date_only(candidate.get("received_at")) or date_only(candidate.get("signal_date"))


def candidate_directed_at_owner(candidate: dict[str, Any]) -> bool:
    return any(candidate.get(key) is True for key in ("directed_at_me", "addressed_to_owner", "response_expected"))


def channel_label(candidate: dict[str, Any], channel_names: dict[str, str]) -> str:
    source_type = str(candidate.get("source_type") or "").lower()
    if source_type == "slack":
        match = re.search(r"/archives/([A-Z0-9]+)/", str(candidate.get("source_ref") or ""))
        channel_id = match.group(1) if match else ""
        name = channel_names.get(channel_id, "")
        if candidate.get("is_mpim") or name.startswith("mpdm-"):
            return "Slack group DM"
        if candidate.get("is_im") or channel_id.startswith("D"):
            return "Slack DM"
        if name:
            return f"#{name}"
        return "Slack"
    if source_type == "gmail":
        return "Gmail"
    if source_type == "google docs":
        return "Doc comment"
    return source_type or "message"


def reply_items(
    candidates: list[dict[str, Any]],
    todos_by_ref: dict[str, list[dict[str, Any]]],
    ledger_items: dict[str, dict[str, Any]],
    replied_refs: set[str],
    verdicts: dict[str, str],
    channel_names: dict[str, str],
    valid_slugs: set[str],
    reply_floor: str,
    plan_day: str,
    reply_checker: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    """Map slug -> unanswered inbound messages that need the owner's reply.

    Also returns asks that look unanswered but belong to no workstream; those
    cannot open a tab and stay with comms triage.

    Already-answered pre-check: a candidate is
    dropped when the ledger, the mention collector, or a cached verdict says
    the owner (or someone else) answered it, and — when ``reply_checker`` is given —
    when a live re-read of the source thread says so. The checker takes the
    candidate dict and returns a reply_check verdict; it is bounded by its
    caller and falls back to the cache, so it never blocks the plan.
    """
    ledger_by_norm = {normalize_ref(ref): item for ref, item in ledger_items.items()}
    result: dict[str, list[dict[str, Any]]] = {}
    unattributed: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if str(candidate.get("source_type") or "").lower() not in REPLY_SOURCE_TYPES:
            continue
        if is_owner(candidate.get("from")) or candidate_is_own_message(candidate):
            continue  # the owner's own messages are never asks
        if not candidate_directed_at_owner(candidate):
            continue
        if not looks_like_ask(candidate):
            continue
        received = candidate_received(candidate)
        if not received or received < reply_floor or received > plan_day:
            continue
        ref = normalize_ref(candidate.get("source_ref"))
        linked = todos_by_ref.get(ref, [])
        slug = next((str(t.get("workstream")) for t in linked if t.get("workstream") in valid_slugs), "")
        if not slug and candidate.get("workspace") in valid_slugs:
            slug = str(candidate["workspace"])
        if not slug:
            hints = [h for h in (candidate.get("workstream_hints") or []) if h in valid_slugs]
            slug = hints[0] if len(hints) == 1 else ""
        if linked and all(t.get("status") in CLOSED_TODO_STATUSES for t in linked):
            continue
        ledger = ledger_by_norm.get(ref) or {}
        if ledger.get("stage") in {"done", "dropped"} or ledger.get("draft_status") == "sent":
            continue
        if ledger_item_answered(ledger):
            continue
        if ref in replied_refs or verdicts.get(ref) in ANSWERED_VERDICTS or verdicts.get(ref) in NON_OPEN_CLASSIFICATIONS:
            continue
        if reply_checker is not None:
            try:
                live = reply_checker(candidate)
            except Exception:
                live = None
            if isinstance(live, dict) and (
                str(live.get("verdict") or "") in ANSWERED_VERDICTS
                or str(live.get("classification") or "") in NON_OPEN_CLASSIFICATIONS
            ):
                continue
        sender = sender_label(candidate.get("from"))
        if not slug:
            unattributed.append({"from": sender, "channel": channel_label(candidate, channel_names), "url": str(candidate.get("source_ref") or "")})
            continue
        key = (slug, sender.lower())
        if key in seen:
            continue
        seen.add(key)
        ask = message_ask(candidate)
        if not ask:
            ask = next((str(t.get("title")) for t in linked if t.get("status") not in CLOSED_TODO_STATUSES), "")
            ask = shorten(clean_text(ask or candidate.get("title") or "message"))
        result.setdefault(slug, []).append(
            {
                "from": sender,
                "channel": channel_label(candidate, channel_names),
                "ask": ask,
                "url": str(candidate.get("source_ref") or ""),
                "received": received,
            }
        )
    return result, unattributed


def build_plan(
    *,
    plan_date: date,
    workstreams: list[dict[str, Any]],
    todos: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]],
    comms_candidates: list[dict[str, Any]],
    ledger_items: dict[str, dict[str, Any]] | None = None,
    replied_refs: set[str] | None = None,
    verdicts: dict[str, str] | None = None,
    channel_names: dict[str, str] | None = None,
    p0_ids: set[str] | None = None,
    now_iso: str | None = None,
    calendar_source: str = "",
    reply_checker: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    plan_day = plan_date.isoformat()
    missed_floor, reply_floor = plan_dates(plan_date)
    valid_slugs = {str(m["slug"]) for m in workstreams}
    if p0_ids is None:
        p0_ids = surfaced_p0_ids(todos, plan_date, now_iso)

    todos_by_ws: dict[str, list[dict[str, Any]]] = {}
    todos_by_ref: dict[str, list[dict[str, Any]]] = {}
    for todo in todos:
        if not isinstance(todo, dict) or not todo.get("id"):
            continue
        ref = normalize_ref(todo.get("source_ref"))
        if ref:
            todos_by_ref.setdefault(ref, []).append(todo)
        slug = str(todo.get("workstream") or "")
        if slug in valid_slugs:
            todos_by_ws.setdefault(slug, []).append(todo)

    replies, replies_unattributed = reply_items(
        comms_candidates, todos_by_ref, ledger_items or {}, replied_refs or set(),
        verdicts or {}, channel_names or {}, valid_slugs, reply_floor, plan_day,
        reply_checker=reply_checker,
    )

    calendar_events = [
        e for e in calendar_events
        if not str(e.get("summary") or "").strip().lower().startswith(CALENDAR_HOLD_PREFIXES)
    ]
    opened: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    todos_resolved: list[dict[str, Any]] = []
    todos_not_asked: list[dict[str, Any]] = []
    matched_meetings: set[str] = set()
    for meta in sorted(workstreams, key=lambda m: workstream_display(m).lower()):
        slug = str(meta["slug"])
        linked = []
        for todo in todos_by_ws.get(slug, []):
            if not todo_is_open(todo, now_iso):
                continue
            live = github_todo_live(todo, reply_checker)
            resolved = github_todo_resolved(todo, reply_checker, live=live)
            if resolved:
                todos_resolved.append({"slug": slug, **resolved})
                continue
            not_asked = github_todo_not_asked(todo, live)
            if not_asked:
                todos_not_asked.append({"slug": slug, **not_asked})
                continue
            linked.append(todo)
        due_today = sorted(
            [t for t in linked if date_only(t.get("due")) == plan_day or date_only(t.get("review_on")) == plan_day],
            key=todo_rank,
        )
        p0s = sorted([t for t in linked if t.get("id") in p0_ids], key=todo_rank)
        missed = sorted(
            [
                t for t in linked
                if str(t.get("priority")) in IMPORTANT
                and date_only(t.get("due"))
                and missed_floor <= date_only(t.get("due")) < plan_day
            ],
            key=todo_rank,
        )
        meetings = [e for e in calendar_events if meeting_matches(meta, str(e.get("summary") or ""))]
        for event in meetings:
            matched_meetings.add(str(event.get("summary") or ""))
        pending = replies.get(slug, [])

        reasons = []
        if due_today:
            reasons.append(f"{len(due_today)} to-do{'s' if len(due_today) != 1 else ''} due today")
        if p0s:
            reasons.append("P0")
        if meetings:
            reasons.append("meeting today: " + ", ".join(str(e.get("summary")) for e in meetings[:2]))
        if pending:
            reasons.append("reply pending: " + ", ".join(r["from"] for r in pending[:3]))
        if not reasons:
            closed.append({"slug": slug, "display_name": workstream_display(meta), "reason": "nothing due today"})
            continue

        todo_lines = []
        seen_ids: set[str] = set()
        for todo in p0s + due_today:
            if todo["id"] in seen_ids:
                continue
            seen_ids.add(todo["id"])
            label = inline_link(str(todo.get("title") or "Untitled"), str(todo.get("source_ref") or ""))
            todo_lines.append(f"- {todo.get('priority') or 'P3'}: {label}")
        for event in meetings:
            if event.get("all_day"):
                when = "all day"
            else:
                start = datetime.fromisoformat(event["start"]).astimezone(PT)
                when = f"{start.hour % 12 or 12}:{start:%M %p}"
            todo_lines.append(f"- Meeting {when}: {inline_link(str(event.get('summary')), str(event.get('link') or ''))}")
        missed_lines = [
            f"- {t.get('priority')} due {date_only(t.get('due'))}: {inline_link(str(t.get('title') or 'Untitled'), str(t.get('source_ref') or ''))}"
            for t in missed if t["id"] not in seen_ids
        ]
        reply_lines = [f"- {r['from']} ({r['channel']}): {inline_link(r['ask'], r['url'])}" for r in pending]
        # Every todo in these lists is already linked from a line above, so Docs
        # only adds watched docs and doc links that no earlier line carries.
        already_linked = {str(t.get("source_ref") or "") for t in p0s + due_today + missed}
        doc_lines = doc_links(meta, already_linked)

        sections = {"todos": todo_lines, "missed": missed_lines, "reply": reply_lines, "docs": doc_lines}
        seed_lines = render_seed(sections)
        opened.append(
            {
                "slug": slug,
                "display_name": workstream_display(meta),
                "tab_title": tab_title(meta, bool(p0s)),
                "priority": "high" if p0s else str(meta.get("priority") or "medium"),
                "p0": bool(p0s),
                "reasons": reasons,
                "seed": "\n".join(seed_lines),
                "seed_line_count": len(seed_lines),
            }
        )

    unmatched = [str(e.get("summary") or "") for e in calendar_events if str(e.get("summary") or "") not in matched_meetings]
    return {
        "date": plan_day,
        "generated_at": now_iso,
        "calendar_source": calendar_source,
        "reply_window_from": reply_floor,
        "open": opened,
        "closed": closed,
        "meetings_unmatched": unmatched,
        "replies_unattributed": replies_unattributed,
        "todos_resolved": todos_resolved,
        "todos_not_asked": todos_not_asked,
    }


def watched_doc_label(doc: dict[str, Any], meta: dict[str, Any]) -> str:
    label = str(doc.get("label") or "").strip()
    if label:
        return label
    snapshot = str(doc.get("snapshot") or "")
    stem = Path(snapshot).stem if snapshot else ""
    if stem:
        return stem.replace("-", " ").replace("_", " ").strip().capitalize()
    return f"{workstream_display(meta)} doc"


def doc_links(meta: dict[str, Any], already_linked: set[str]) -> list[str]:
    lines: list[str] = []
    seen = {normalize_ref(u) for u in already_linked if u}
    for doc in meta.get("watched_docs") or []:
        url = str(doc.get("url") or "")
        if url.startswith("http") and normalize_ref(url) not in seen:
            seen.add(normalize_ref(url))
            lines.append(f"- {inline_link(watched_doc_label(doc, meta), url)}")
    return lines


def render_seed(sections: dict[str, list[str]]) -> list[str]:
    trimmed = {key: list(lines[: SECTION_CAPS[key]]) for key, lines in sections.items()}

    def total() -> int:
        return sum(len(lines) + 1 for lines in trimmed.values() if lines)

    while total() > MAX_SEED_LINES:
        key = max(TRIM_ORDER, key=lambda k: (len(trimmed[k]), -TRIM_ORDER.index(k)))
        if not trimmed[key]:
            break
        trimmed[key].pop()
    out: list[str] = []
    for key in SECTION_ORDER:
        if trimmed[key]:
            out.append(HEADINGS[key])
            out.extend(trimmed[key])
    return out


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def seed_prompt(entry: dict[str, Any], plan_day: str) -> str:
    return (
        f"Start-of-day seed for {entry['display_name']} on {plan_day}. This turn is the seed note only: "
        "print the note below exactly as written (same headings, same lines, Markdown links kept), "
        "then stop and wait for the owner. No summary table, no status recap, no history, no CONTEXT.md walkthrough, "
        "no tool calls other than the required emit-workspace-update.py call with --status waiting. "
        "Resume normal workstream behavior from the next prompt.\n\n" + entry["seed"]
    )


def compat_plan(plan: dict[str, Any], repo_root: Path, today_note: str) -> dict[str, Any]:
    note_path = Path(today_note) if today_note else repo_root / "notes" / "daily" / f"{plan['date']}.md"
    try:
        relative_note = note_path.relative_to(repo_root)
    except ValueError:
        relative_note = note_path
    return {
        "date": plan["date"],
        "plan_source": "sod-workspace-plan",
        "today_note": Path(relative_note).as_posix(),
        "note_created": note_path.exists(),
        "git_activity_summary": "",
        "todo_summary": "",
        "calendar_summary": f"calendar: {plan.get('calendar_source') or 'unknown'}",
        "people_updated": [],
        "workstreams_to_open": [
            {
                "name": entry["slug"],
                "display_name": entry["display_name"],
                "tab_title": entry["tab_title"],
                "priority": entry["priority"],
                "prompt": seed_prompt(entry, plan["date"]),
                "reason": "; ".join(entry["reasons"]),
                "open_now": True,
            }
            for entry in plan["open"]
        ],
        "workstreams_skipped": [
            {"name": entry["slug"], "display_name": entry["display_name"], "reason": entry["reason"]}
            for entry in plan["closed"]
        ],
    }


def render_text(plan: dict[str, Any]) -> str:
    lines = [f"Start-of-day plan for {plan['date']} (calendar: {plan.get('calendar_source') or 'unknown'})", ""]
    if not plan["open"]:
        lines.append("Open: nothing. No workstream has a to-do due today, a meeting, a pending reply, or a P0.")
    else:
        lines.append(f"Open ({len(plan['open'])}):")
        for entry in plan["open"]:
            lines.append(f"  {entry['tab_title']}  [{'; '.join(entry['reasons'])}]")
            lines.extend("    " + line for line in entry["seed"].splitlines())
            lines.append("")
    lines.append(f"Stay closed ({len(plan['closed'])}): " + ", ".join(e["slug"] for e in plan["closed"]))
    if plan.get("meetings_unmatched"):
        lines.append("Meetings with no workstream match: " + ", ".join(plan["meetings_unmatched"]))
    if plan.get("replies_unattributed"):
        lines.append(
            "Unanswered asks with no workstream (comms triage keeps them): "
            + ", ".join(f"{r['from']} ({r['channel']})" for r in plan["replies_unattributed"])
        )
    if plan.get("todos_resolved"):
        lines.append(
            "Already resolved on GitHub (dropped from seeds, close the to-dos): "
            + "; ".join(f"{r['slug']}: {r['title']} — {r['label']}" for r in plan["todos_resolved"])
        )
    if plan.get("todos_not_asked"):
        lines.append(
            "Not review asks (the owner is not a requested reviewer; dropped from seeds, relabel the to-dos): "
            + "; ".join(f"{r['slug']}: {r['title']} — {r['label']}" for r in plan["todos_not_asked"])
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="plan date YYYY-MM-DD (default: today in Pacific Time)")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--todo-json", type=Path, help="override system/state/todo.json")
    parser.add_argument("--comms-json", type=Path, help="override system/state/sources/comms.json")
    parser.add_argument("--calendar-json", type=Path, help="read calendar events from this JSON instead of the live API")
    parser.add_argument("--no-calendar", action="store_true", help="skip the calendar lookup")
    parser.add_argument(
        "--no-live-reply-check",
        action="store_true",
        help="skip the live already-answered re-read of Reply-to sources (cache and ledger still apply)",
    )
    parser.add_argument(
        "--reply-check-budget",
        type=float,
        default=float(os.environ.get("SOD_REPLY_CHECK_BUDGET_SECONDS", "25")),
        help="seconds of live source re-reads before falling back to the cache (default 25)",
    )
    parser.add_argument("--today-note", default="", help="daily note path recorded in today-plan.json")
    parser.add_argument("--dry-run", action="store_true", help="print the plan as text; open and write nothing")
    parser.add_argument("--write", action="store_true", help="write system/today-plan.json for start-day.sh")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    plan_date = date.fromisoformat(args.date) if args.date else datetime.now(PT).date()
    todo_path = args.todo_json or Path(os.environ.get("SWITCHBOARD_TODO_JSON") or repo_root / "system" / "state" / "todo.json")
    comms_path = args.comms_json or repo_root / "system" / "state" / "sources" / "comms.json"

    todos = load_json(todo_path, {}).get("items") or []
    comms = load_json(comms_path, {})
    candidates = comms.get("candidates") or []
    ledger_items = load_json(repo_root / "system" / "state" / "triage-ledger.json", {}).get("items") or {}
    calendar_events, calendar_source = load_calendar(repo_root, plan_date, args.calendar_json, args.no_calendar)

    plan = build_plan(
        plan_date=plan_date,
        workstreams=load_workstreams(repo_root),
        todos=todos,
        calendar_events=calendar_events,
        comms_candidates=candidates,
        ledger_items=ledger_items if isinstance(ledger_items, dict) else {},
        replied_refs=load_replied_refs(repo_root),
        verdicts=load_resolution_cache(repo_root),
        channel_names=load_channel_names(repo_root),
        calendar_source=calendar_source,
        reply_checker=None if args.no_live_reply_check else load_reply_checker(args.reply_check_budget),
    )

    if args.dry_run:
        print(render_text(plan))
        return 0
    if args.write:
        target = repo_root / "system" / "today-plan.json"
        target.write_text(json.dumps(compat_plan(plan, repo_root, args.today_note), indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"written": str(target), "open": [e["slug"] for e in plan["open"]], "closed": len(plan["closed"])}))
        return 0
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
