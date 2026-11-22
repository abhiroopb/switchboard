#!/usr/bin/env python3
"""Render a compact list-only CoS next-up digest.

Surfaces: ``next-up`` and ``start-day`` are the CoS digests (running work,
pending reports, meeting ingests). Neither lists pending calendar invites or
RSVP handles: the owner accepts and declines meetings directly. ``comms-triage`` is the separate
review posted only into the 📨 Comms Triage workspace: every held draft in
full with Obsidian edit links. CoS digests never include inbound items or
drafts.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_triage_setting
import named_refs
import review_surface
from meeting_ingests import SECTION_TITLE as MEETINGS_TITLE, digest_items as meeting_ingest_items

MEETINGS_TITLE_LINE = f"=== {MEETINGS_TITLE} ==="

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
COMMAND_CENTER_FILE = Path(
    os.environ.get("NEXT_UP_DIGEST_FILE")
    or REPO_ROOT / "system" / "state" / "command-center-latest.md"
)
RUNNING_TASKS_FILE = Path(
    os.environ.get("RUNNING_TASKS_FILE")
    or REPO_ROOT / "system" / "state" / "running-tasks.json"
)
COMMS_REVIEW_SCRIPT = Path(__file__).resolve().parent / "comms-review-digest.py"


PLAN_FRESHNESS_FILE = REPO_ROOT / "system" / "state" / "plan-freshness.json"
PLAN_FRESHNESS_MAX_AGE_DAYS = 15


def plan_freshness_line(now: datetime | None = None) -> str:
    """One CoS line from plan-freshness.py's state, or a line if that check went quiet."""
    try:
        state = json.loads(PLAN_FRESHNESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    checked = parse_utc(state.get("checked_at"))
    now = now or datetime.now(timezone.utc)
    if checked is None or (now - checked).days > PLAN_FRESHNESS_MAX_AGE_DAYS:
        return f"{state.get('label') or 'Plan'}: the weekly freshness check has not run in over {PLAN_FRESHNESS_MAX_AGE_DAYS} days."
    return str(state.get("line") or "")


def task_origin(task: dict[str, object]) -> str:
    return "launchpad" if task.get("origin") == "launchpad" else "cos"


def clean_cell(value: str, limit: int = 190) -> str:
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " — ", value, flags=re.IGNORECASE)
    value = re.sub(r"<!--.*?-->", "", value)
    rendered = review_surface.report_text(value, limit)
    prose, separator, links = rendered.partition("\n")
    # Markdown emphasis is stripped from the prose line only. A pointer path and
    # a ``` draft block (report_text keeps it whole after the prose) stay as is.
    if not links.startswith("[Open in Obsidian](") and not prose.startswith("```"):
        prose = re.sub(r"[*_`]", "", prose)
    return prose + separator + links


def command_center_items() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"act": [], "important": []}
    try:
        lines = COMMAND_CENTER_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return sections

    current = ""
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        joined = " ".join(cells)
        if "🔴 Act now" in joined:
            current = "act"
            continue
        if "🟠 Important" in joined:
            current = "important"
            continue
        if "⚪ Background" in joined:
            current = ""
            continue
        if current and cells[0] not in {"#", "---", "⏤", "—"}:
            workspace = clean_cell(cells[1], 90)
            next_step = named_refs.name_refs(clean_cell(cells[2], 180), tasks=False)
            if workspace and next_step:
                sections[current].append(f"{workspace}: {next_step}")
    return sections


# A ledger row's ``summary`` is the phase word (``dispatch.py`` writes "started" at
# launch) until the worker reports, so a Running bullet never shows it alone.
PHASE_WORDS = {"", "started", "routing", "queued", "active", "launched", "running"}
# The routing preamble ``dispatch.py`` puts ahead of the ask: ``Workstream: <slug>``
# lines and the ``Steer on rt-… (…) …:`` lead of a continuation.
PROMPT_PREAMBLE = re.compile(
    r"^(?:Workstream:\s*\S+\s*|Steer on rt-[0-9a-f]+\s*\([^)]*\)[^\n:]*:\s*)+", re.IGNORECASE
)


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def age_label(task: dict[str, object], now: datetime) -> str:
    """``12m`` / ``1h 05m`` / ``2d 3h`` since the row started (or was launched or created)."""
    started = next(
        (parse_utc(task.get(key)) for key in ("started_at", "launched_at", "created_at") if parse_utc(task.get(key))),
        None,
    )
    if started is None:
        return ""
    minutes = max(0, int((now - started).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def prompt_first_line(prompt: str, limit: int = 120) -> str:
    """The first non-empty line of the ask after the routing preamble, whitespace collapsed
    and clipped at a word boundary. A lead-in line that only names who asked and when
    (``the owner, Mon … in CoS:``) carries the next line with it, so the bullet shows the ask."""
    text = re.sub(r"\s*⏎\s*", "\n", str(prompt or "")).strip()
    text = PROMPT_PREAMBLE.sub("", text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    if first.endswith(":") and len(lines) > 1:
        first = f"{first} {lines[1]}"
    first = html.unescape(re.sub(r"<[^>]+>", "", first))
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) <= limit:
        return first
    cut = first[:limit]
    boundary = cut.rfind(" ")
    if boundary < limit // 2:
        boundary = limit
    return cut[:boundary].rstrip(" ,;:(") + "…"


def running_line(task: dict[str, object], now: datetime) -> str:
    """``<emoji> <Workstream Name>: <first prompt line> · <age> (rt-…)`` for a
    routing or active row.

    The row's summary is the phase word until the worker reports, so the ask leads.
    A summary that says more than the phase word is kept after the ask.

    Names, never bare numbers: the
    workstream name leads and the task id closes the line in parentheses; a PR
    or issue reference inside the ask or summary is named through
    ``named_refs.name_refs``.
    """
    task_id = str(task.get("id") or "").strip()
    workstream = str(task.get("workstream") or "").strip()
    name = named_refs.workstream_title(workstream) if workstream else ""
    ask = named_refs.name_refs(prompt_first_line(str(task.get("prompt") or "")), tasks=False)
    summary = named_refs.relabel_task_lead(clean_cell(str(task.get("summary") or ""), 180), workstream)
    summary = named_refs.name_refs(summary, tasks=False)
    pieces = [f"{name}:" if name else "", ask]
    if summary and summary.strip().lower() not in PHASE_WORDS:
        pieces.append(f"— {summary}" if ask else summary)
    elif not ask:
        pieces.append(summary or str(task.get("status") or ""))
    line = " ".join(piece for piece in pieces if piece).strip()
    age = age_label(task, now)
    if line and age:
        line = f"{line} · {age}"
    if line and task_id:
        line = f"{line} ({task_id})"
    return line


def running_task_sections(now: datetime | None = None) -> tuple[list[str], list[dict[str, str]]]:
    try:
        payload = json.loads(RUNNING_TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    now = now or datetime.now(timezone.utc)
    active = []
    pending = []
    for task in payload.get("tasks", []):
        if not isinstance(task, dict):
            continue
        summary = clean_cell(str(task.get("summary") or ""), 180)
        # Updates lead with the workstream name, never a bare task id (Rule 13).
        summary = named_refs.name_refs(
            named_refs.relabel_task_lead(summary, str(task.get("workstream") or "")), tasks=False)
        if task.get("status") in {"routing", "active"}:
            line = running_line(task, now)
            if line:
                active.append(line)
        elif (
            task_origin(task) == "cos"
            and task.get("report_delivered") is False
            and task.get("report_delivery_unknown") is not True
            # These require the full direct callback, including admission's
            # exception request. A truncated digest must never acknowledge them.
            and task.get("status") not in {"blocked", "failed"}
            and not task.get("urgent") and not task.get("needs_ruling")
            # A direct report already summarized once stays out of later digests
            # while its full line waits for CoS (ack-delivered).
            and not task.get("report_in_digest_at")
            and summary
            and task.get("id")
        ):
            pending.append({"id": str(task["id"]), "summary": summary})
    return active, pending


def emit_section(title: str, items: list[str]) -> list[str]:
    lines = [f"=== {title} ==="]
    lines.extend(f"- {item}" for item in items)
    if not items:
        lines.append("- Nothing queued.")
    return lines


# The refreshing render does at most one live-check budget (see
# TRIAGE_DRAFT_REFRESH_BUDGET_SECONDS in triage_ledger.py) plus whatever one
# in-flight Slack check costs under rate limiting (~12-20s). The loop that
# calls this ticks every 10 minutes, so 45s is a safe upper bound.
COMMS_REVIEW_REFRESH_TIMEOUT = int(os.environ.get("COMMS_REVIEW_REFRESH_TIMEOUT", "45"))
COMMS_REVIEW_READONLY_TIMEOUT = int(os.environ.get("COMMS_REVIEW_READONLY_TIMEOUT", "10"))
# Hard total budget per surface (characters of the multi-line message). The
# comms-review section has its own per-item and section budgets; this cap is
# the last line of defense so no autopost can ever become a wall again.
MAX_CHARS = {
    "next-up": int(os.environ.get("NEXT_UP_MAX_CHARS", "6000")),
    "start-day": int(os.environ.get("START_DAY_MAX_CHARS", "12000")),
    # Every held draft in full; only a runaway backlog hits this. Whole newest
    # drafts survive, the review note keeps the rest.
    "comms-triage": int(os.environ.get("COMMS_TRIAGE_MAX_CHARS", "60000")),
}
# Surfaces that carry the inbound + drafts review. CoS digests are excluded on
# purpose: the owner reviews drafts only in the Comms Triage workspace.
COMMS_REVIEW_SURFACES = frozenset({"comms-triage"})
# The guarded injection path sends one line (a raw newline is Enter in the
# Amp TUI). This visible delimiter replaces each line break so CoS can present
# one topic per line again. comms-review-digest.py uses " ¶ " for paragraph
# marks inside a draft, so the two never collide.
LINE_DELIMITER = " ⏎ "
ENVELOPES = {
    "next-up": (
        "[NEXT-UP AUTOPOST] Present the digest below as its === sections === with one "
        "bullet per item. ' ⏎ ' marks a line break (the injection path is single-line). "
        "A ``` fenced block inside an item is a draft message for the owner: print it verbatim "
        "in a code block under that item, every line, never summarized, shortened, or "
        "replaced by a pointer (the owner always sees the draft). "
        "Do not route this message."
    ),
    "start-day": (
        "[START-DAY REVIEW] Read the quick I/O contract in system/chief-of-staff-prompt.md. "
        "Present the digest below as its === sections === with one bullet per item. "
        "' ⏎ ' marks a line break (the injection path is single-line). "
        "A ``` fenced block inside an item is a draft message for the owner: print it verbatim "
        "in a code block under that item, every line, never summarized, shortened, or "
        "replaced by a pointer (the owner always sees the draft). "
        "Do not route this message."
    ),
    "comms-triage": (
        "[COMMS TRIAGE REVIEW] Show the review below verbatim, one line per ' ⏎ ' "
        "(the injection path is single-line). Put a blank line before the 🆕, "
        "'✍️ Drafts', and 🧭 lines. Keep every '- ✍️ **Draft N**' bullet, its inline "
        "[label](url) links (source title and ✏️ Obsidian), and the quoted reply exactly "
        "as written; the '↳ 📥' line stays indented under its draft. Do not add headings, "
        "code fences, or backticks; do not summarize, shorten, or truncate any draft; do "
        "not rewrite or drop any link. Do not "
        "route this message. Do not send, dismiss, or edit any draft now. When the owner "
        "later replies 'sent N', 'dismiss N', 'reopen N', or 'edit N <changes>', run "
        "python3 system/scripts/triage_ledger.py draft --draft-id draft-N --status "
        "sent|dismissed|held --by owner, or edit-draft --draft-id draft-N --by owner --text "
        "'<full revised reply>'. When the owner replies 'send N', run "
        "python3 system/scripts/send-triage-draft.py --draft-id draft-N --by owner — it "
        "posts the stored reply as shown from the owner's account, with the :robot_face: prefix on "
        "its first line (all AI-drafted messages carry the bot prefix; "
        "'no robot N' opts one draft out via triage_ledger.py no-robot --draft-id draft-N "
        "--by owner), and marks it sent. Never post a reply through pmos slack directly."
    ),
}


def cap_lines(lines: list[str], max_chars: int) -> list[str]:
    """Keep whole lines within ``max_chars``; report how many were dropped."""
    total = sum(len(line) + 1 for line in lines)
    if total <= max_chars:
        return lines
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > max_chars - 80:
            break
        kept.append(line)
        used += cost
    omitted = len(lines) - len(kept)
    kept.append(f"- … {omitted} more line{'' if omitted == 1 else 's'} omitted (over the character budget).")
    return kept


def injection_text(message: str) -> str:
    """Single-line form of ``message`` for the guarded cmux send."""
    return LINE_DELIMITER.join(line for line in message.splitlines() if line.strip())


def _run_comms_review(extra_args: list[str], timeout: int) -> dict[str, object] | None:
    """Run the review renderer once; return its payload or None on any failure."""
    try:
        result = subprocess.run(
            [sys.executable, str(COMMS_REVIEW_SCRIPT), "--json", *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "REPO_ROOT": str(REPO_ROOT)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) and "message" in payload else None


def comms_review_section(surface: str = "next-up") -> dict[str, object]:
    """Render the comms review section, degrading to read-only before giving up.

    The refreshing render may issue live Slack checks. If it exceeds its
    budget, re-render read-only from the ledger so held drafts stay visible.
    Only when both fail do we report review state as unavailable.
    """
    surface_args = ["--surface", surface]
    payload = _run_comms_review(surface_args, COMMS_REVIEW_REFRESH_TIMEOUT)
    if payload is None:
        payload = _run_comms_review(
            [*surface_args, "--no-refresh"], COMMS_REVIEW_READONLY_TIMEOUT
        )
        if payload is not None:
            payload["message"] = (
                f"{payload['message']}\n"
                "- ℹ️ Live Slack reply check skipped this run (time budget); drafts shown as held."
            )
    if payload is None:
        return {
            "message": (
                "📬 Comms Triage Review\n"
                "⚠️ Review state is unavailable; no draft was discarded."
            ),
            "inbound_refs": [],
            "pending_draft_ids": [],
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--act-now-limit", type=int, default=3)
    parser.add_argument("--important-limit", type=int, default=3)
    parser.add_argument("--surface", choices=tuple(ENVELOPES), default="next-up")
    parser.add_argument("--priorities-only", action="store_true", help="Render reminders for the Todo workspace only.")
    parser.add_argument("--pending-report-ids", action="store_true",
                        help="Print only the JSON list of report ids the next-up digest would carry (the single pending predicate).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    # Title lookups for PR/issue refs: at most two network calls per render so
    # the digest stays inside the loop's 45 s budget; the rest come from cache.
    named_refs.set_fetch_budget(int(os.environ.get("NAMED_REFS_MAX_FETCH", "2") or 0))

    # One predicate for "pending": the poster's after-hours bookkeeping and the
    # stale-digest alarm counted raw ledger rows (origin cos, undelivered) while
    # the in-hours render excluded unknown-held, blocked and urgent rows, so the
    # two counts disagreed.
    if args.pending_report_ids:
        _, pending_reports = running_task_sections()
        print(json.dumps([row["id"] for row in pending_reports]))
        return 0

    # Priority reminders belong only in Todo. CoS keeps its comms and reports.
    if args.priorities_only:
        sections = command_center_items()
        lines = emit_section("🔴 Act now", sections["act"][: args.act_now_limit])
        lines.append("")
        lines.extend(emit_section("🟠 Important", sections["important"][: args.important_limit]))
        message = "\n".join(lines)
        print(json.dumps({"message": message}, ensure_ascii=False) if args.json else message)
        return 0

    if args.surface in COMMS_REVIEW_SURFACES and not comms_triage_setting.enabled():
        # Off on this machine: the review has no surface to post into.
        print(json.dumps({"message": ""}) if args.json else "")
        return 0

    envelope = ENVELOPES[args.surface]
    lines = [envelope, ""]
    review: dict[str, object] = {}
    active: list[str] = []
    pending_reports: list[dict[str, str]] = []
    meeting_ingests: list[tuple[str, str]] = []
    if args.surface in COMMS_REVIEW_SURFACES:
        # Comms Triage gets only the review: every draft in full, nothing else.
        review = comms_review_section(args.surface)
        lines.append("")
        lines.extend(str(review["message"]).splitlines())
    else:
        from slack_backoff import digest_line as slack_streak_line
        # The authored-PR scan feeds Comms Triage (priority-channels.yaml
        # github_pr_reviews). Where Comms Triage is off nothing runs it, so the
        # line could only ever read "monitor stale/degraded; 0 events".
        if comms_triage_setting.enabled():
            from github_pr_reviews import digest_line as pr_comments_line
            lines.append("- " + pr_comments_line())
        # Only once this host has re-hit Slack's limit N cooldowns in a row;
        # a lone 429 stays in the logs.
        streak = slack_streak_line()
        if streak:
            lines.append("- " + streak)
        # Only while the weekly plan freshness check has something tripped, or
        # has stopped running. Flag names only; figures stay in the private folder.
        freshness = plan_freshness_line()
        if freshness:
            lines.append("- " + freshness)
        # Only while a volunteer roster grant still awaits its Slack
        # confirmation or the last approvals run did not finish clean.
        # One "Review the feedback console" reminder while console drafts await
        # the owner (next-up dedupes an unchanged waiting set; start-day always
        # shows it), plus a health line only when the last hourly feedback
        # pass was deferred or errored. Done callbacks from that workstream
        # never appear here (router.yaml dispatch.quiet_done_workstreams).
        active, pending_reports = running_task_sections()
        if active:
            lines.append("")
            lines.extend(emit_section("🔄 Running", active))
        if pending_reports:
            lines.append("")
            lines.extend(emit_section("📬 Updates", [row["summary"] for row in pending_reports]))
        # One line per Gemini meeting ingest not yet shown on this surface,
        # the same day, not at the action items' due dates. Source: the daily note's INGEST_OK lines.
        meeting_ingests = meeting_ingest_items(surface=args.surface)
        if meeting_ingests:
            lines.append("")
            lines.extend(emit_section(MEETINGS_TITLE, [line for _, line in meeting_ingests]))
        if not active and not pending_reports and not meeting_ingests:
            lines.append("")
            lines.extend(emit_section("🔄 Running", []))
    capped = cap_lines(lines, MAX_CHARS[args.surface])
    message = "\n".join(capped)
    inbound_refs = list(review.get("inbound_refs", []))
    if len(capped) != len(lines):
        # The poster acknowledges every ref and report id we return; never ack
        # a row whose line the total cap dropped.
        inbound_refs = [ref for ref in inbound_refs if ref and ref in message]
        pending_reports = [row for row in pending_reports if f"- {row['summary']}" in message]
        meeting_ingests = [(key, line) for key, line in meeting_ingests if f"- {line}" in message]
    # The poster's dedupe hash ignores meeting lines: they are stamped as shown
    # and vanish next render, which must not count as a changed digest.
    meeting_line_set = {f"- {line}" for _, line in meeting_ingests}
    hash_text = "\n".join(
        line for line in capped
        if line not in meeting_line_set and line != MEETINGS_TITLE_LINE
    )
    if args.json:
        print(json.dumps({
            "message": message,
            "injection_text": injection_text(message),
            "hash_text": hash_text,
            "meeting_ingest_keys": [key for key, _ in meeting_ingests],
            "pending_report_ids": [row["id"] for row in pending_reports],
            "inbound_refs": inbound_refs,
            "pending_draft_ids": review.get("pending_draft_ids", []),
        }, ensure_ascii=False))
    else:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
