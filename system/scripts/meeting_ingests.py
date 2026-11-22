#!/usr/bin/env python3
"""Meeting ingests for the CoS next-up digest.

``daemon/post-ingest.sh`` writes each Gemini meeting ingest to the daily note
(``notes/daily/YYYY-MM-DD.md``, ``## Activity Log``) as a headline plus an
``INGEST_OK`` detail line, creates ``meeting-ingest`` to-dos, and enqueues a
notification that only ever reaches ``outputs/automation-inbox/meetings.md``
and a passive cmux log line. Nothing surfaced the ingest to CoS the same day,
so an action item from a 9 AM meeting sat silent until its due date.

This module turns those daily-note lines into digest lines:

    <title> · <HH:MM> PT · <N> new to-do(s)[: titles when ≤3] · <note path>

under a ``=== 📅 Meetings ingested ===`` section.

Source of truth for "since the last digest" is the daily note (deterministic,
written by the same script that marks the ingest processed); the daemon log is
never parsed. ``post-next-up-digest.sh`` calls ``--stamp-shown`` after a posted
digest so each ingest appears once on next-up; the stamp lives in
``.state/meeting-ingests-digest.json`` next to the other digest stamps.

Failed ingests (INGEST_FAIL) are not handled here: they stay urgent and go
direct through post-ingest.sh's own warning path.

Env overrides (tests): ``MEETING_INGESTS_DAILY_DIR``, ``MEETING_INGESTS_TODO_FILE``,
``MEETING_INGESTS_PROJECTS_DIR``, ``MEETING_INGESTS_NOTES_DIR``,
``MEETING_INGESTS_STAMP_FILE``, ``MEETING_INGESTS_TODAY`` (YYYY-MM-DD, local).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
DAILY_DIR = Path(os.environ.get("MEETING_INGESTS_DAILY_DIR") or REPO_ROOT / "notes" / "daily")
TODO_FILE = Path(os.environ.get("MEETING_INGESTS_TODO_FILE") or REPO_ROOT / "system" / "state" / "todo.json")
PROJECTS_DIR = Path(os.environ.get("MEETING_INGESTS_PROJECTS_DIR") or REPO_ROOT / "projects")
NOTES_DIR = Path(os.environ.get("MEETING_INGESTS_NOTES_DIR") or REPO_ROOT / "outputs" / "meeting-notes")
STAMP_FILE = Path(os.environ.get("MEETING_INGESTS_STAMP_FILE") or REPO_ROOT / ".state" / "meeting-ingests-digest.json")

# Daily notes to read: today and yesterday (local). An ingest older than that
# was already in a digest or predates this feature; the stamp prunes to match.
LOOKBACK_DAYS = 1
MAX_TITLES = 3
TITLE_CLIP = 100
SECTION_TITLE = "📅 Meetings ingested"

HEADLINE_RE = re.compile(r"^- (?P<time>\d{2}:\d{2}) \[\[Calendar\]\] Gemini notes ingested for (?P<title>.+?)\s*$")
INGEST_OK_RE = re.compile(
    r"^\s+- INGEST_OK:\s*(?P<title>.+?)\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|.*?actions:\s*(?P<actions>\d+)",
)
DOC_ID_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")
MEETING_NOTE_GLOBS = ("*/meetings/*.md",)


@dataclass
class Ingest:
    date: str          # YYYY-MM-DD of the daily note
    time: str          # HH:MM local, meeting end time as post-ingest.sh wrote it
    title: str         # calendar event summary
    actions: int       # "actions: N for the owner" from the INGEST_OK line
    ok_title: str = ""  # title on the INGEST_OK line (the ingest's own name for the meeting)
    note_path: str = ""  # repo-relative meeting note, "" when not found
    doc_ids: list[str] = field(default_factory=list)
    todo_titles: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        # post-ingest.sh dedupes on the headline, but a retitled calendar event
        # (Team Standup vs [🕰️⚔️] Team Standup) writes two headlines
        # for one ingest; the INGEST_OK title is the same for both.
        return f"{self.date}|{self.time}|{self.ok_title or self.title}"


def local_today() -> date:
    override = os.environ.get("MEETING_INGESTS_TODAY")
    if override:
        return date.fromisoformat(override)
    return datetime.now().astimezone().date()


def parse_daily_note(path: Path, day: str) -> list[Ingest]:
    """Headline + INGEST_OK pairs under ``## Activity Log``; the headline alone is not an ingest."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    ingests: list[Ingest] = []
    in_activity = False
    pending: re.Match[str] | None = None
    for line in lines:
        if line.startswith("## "):
            in_activity = line.strip() == "## Activity Log"
            pending = None
            continue
        if not in_activity:
            continue
        headline = HEADLINE_RE.match(line)
        if headline:
            pending = headline
            continue
        detail = INGEST_OK_RE.match(line)
        if detail and pending:
            ingests.append(Ingest(
                date=day,
                time=pending.group("time"),
                title=pending.group("title").strip(),
                actions=int(detail.group("actions")),
                ok_title=detail.group("title").strip(),
            ))
            pending = None
            continue
        if line.strip() and not line.startswith("  "):
            pending = None
    return ingests


def recent_ingests(today: date | None = None, daily_dir: Path = DAILY_DIR) -> list[Ingest]:
    today = today or local_today()
    found: list[Ingest] = []
    seen: set[str] = set()
    for offset in range(LOOKBACK_DAYS, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        for ingest in parse_daily_note(daily_dir / f"{day}.md", day):
            if ingest.key in seen:
                continue
            seen.add(ingest.key)
            found.append(ingest)
    return found


# ------------------------------------------------------------ enrichment ----

def _note_header(path: Path) -> tuple[str, str, list[str]]:
    """(calendar event, date, google doc ids) from a meeting note's header block.
    The Gemini notes doc comes first; attached material (agenda docs) follows."""
    try:
        head = path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return "", "", []
    event = re.search(r"^\*\*Calendar event:\*\*\s*(.+?)\s*$", head, re.MULTILINE)
    day = re.search(r"^\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})", head, re.MULTILINE)
    title = event.group(1) if event else ""
    if not title:
        first = re.search(r"^#\s+(.+?)\s*$", head, re.MULTILINE)
        title = first.group(1) if first else ""
    gemini: list[str] = []
    other: list[str] = []
    for link in re.finditer(r"\[([^\]]*)\]\((https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)[^)]*)\)", head):
        label, _, doc_id = link.groups()
        bucket = gemini if "gemini" in label.casefold() else other
        if doc_id not in bucket:
            bucket.append(doc_id)
    for doc_id in DOC_ID_RE.findall(head):
        if doc_id not in gemini and doc_id not in other:
            other.append(doc_id)
    return title, (day.group(1) if day else ""), gemini + other


def _norm(text: str) -> str:
    """Casefolded title without a trailing ``(…)`` group (the note header appends the
    calendar event id there) or leading decoration such as ``[🕰️⚔️]``."""
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text.strip())
    # Leading tokens with no ASCII letter or digit are decoration: "[🕰️⚔️]",
    # "ᴗ̈" (a Unicode letter, so \W does not catch it).
    text = re.sub(r"^(?:[^\sA-Za-z0-9]+\s+)+", "", text)
    text = re.sub(r"^\W+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _titles_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and short in long_


def find_note(ingest: Ingest, projects_dir: Path = PROJECTS_DIR, notes_dir: Path = NOTES_DIR,
              repo_root: Path = REPO_ROOT) -> tuple[str, list[str]]:
    """(repo-relative note path, google doc ids from its header). Prefers the synced project copy
    (``projects/<x>/meetings/``) over the raw ``outputs/meeting-notes/`` file."""
    candidates: list[Path] = []
    for pattern in MEETING_NOTE_GLOBS:
        candidates.extend(sorted(projects_dir.glob(pattern)))
    candidates.extend(sorted(notes_dir.glob("*.md")))
    exact: tuple[Path, list[str]] | None = None
    loose: tuple[Path, list[str]] | None = None
    for path in candidates:
        if not path.name.startswith(ingest.date):
            continue
        title, day, doc_ids = _note_header(path)
        if day and day != ingest.date:
            continue
        if _norm(title) == _norm(ingest.title):
            exact = exact or (path, doc_ids)
        elif _titles_match(title, ingest.title):
            loose = loose or (path, doc_ids)
    hit = exact or loose
    if hit is None:
        return "", []
    path, doc_ids = hit
    try:
        rel = str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        rel = str(path)
    return rel, doc_ids


def load_todo_items(todo_file: Path = TODO_FILE) -> list[dict[str, object]]:
    try:
        data = json.loads(todo_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [item for item in items or [] if isinstance(item, dict)]


def _local_day(stamp: str) -> str:
    """Local calendar day of a to-do's ``created_at`` (UTC ``Z``): an evening
    meeting ingested at 17:30 PT is created at 00:30Z the next day."""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:10]
    if parsed.tzinfo is None:
        return stamp[:10]
    return parsed.astimezone().date().isoformat()


def matching_todos(ingest: Ingest, items: list[dict[str, object]]) -> list[str]:
    """Titles of the ``meeting-ingest`` to-dos this ingest created on its date: by a
    Google doc id the note header names (the Gemini notes doc; an attached agenda
    doc also counts, which is why the date filter stays), else by the
    ``From <title>`` / ``<title> 10/26:`` context post-ingest writes."""
    same_day = [
        item for item in items
        if item.get("source") == "meeting-ingest" and _local_day(str(item.get("created_at") or "")) == ingest.date
    ]
    by_doc = [
        item for item in same_day
        if any(doc_id in str(item.get("source_ref") or "") for doc_id in ingest.doc_ids)
    ]
    want = _norm(ingest.ok_title or ingest.title)
    by_context = [item for item in same_day if want and want in _norm(str(item.get("context") or ""))]
    chosen = by_doc or by_context
    return [title for title in (str(item.get("title") or "").strip() for item in chosen) if title]


def enrich(ingests: list[Ingest], items: list[dict[str, object]] | None = None) -> list[Ingest]:
    items = load_todo_items() if items is None else items
    for ingest in ingests:
        ingest.note_path, ingest.doc_ids = find_note(ingest)
        ingest.todo_titles = matching_todos(ingest, items)
    return ingests


# ----------------------------------------------------------------- render ----

def _when(ingest: Ingest, today: date) -> str:
    day = date.fromisoformat(ingest.date)
    if day == today:
        return f"{ingest.time} PT"
    return f"{day.strftime('%a')} {day.month}/{day.day} {ingest.time} PT"


def _clip(text: str, limit: int = TITLE_CLIP) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def render_line(ingest: Ingest, today: date | None = None) -> str:
    today = today or local_today()
    count = len(ingest.todo_titles) if ingest.todo_titles else ingest.actions
    if count == 0:
        todos = "no to-dos for the owner"
    else:
        noun = "to-do" if count == 1 else "to-dos"
        todos = f"{count} new {noun}"
        if ingest.todo_titles and count <= MAX_TITLES:
            todos += ": " + "; ".join(_clip(title) for title in ingest.todo_titles)
    parts = [ingest.title, _when(ingest, today), todos]
    parts.append(ingest.note_path or "no meeting note found")
    return " · ".join(parts)


# ------------------------------------------------------------------ stamp ----

def load_stamp(stamp_file: Path = STAMP_FILE) -> dict[str, object]:
    try:
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stamp if isinstance(stamp, dict) else {}


def stamp_shown(keys: list[str], stamp_file: Path = STAMP_FILE, today: date | None = None) -> dict[str, object]:
    """Record the ingests a posted digest just showed; keys older than the
    lookback window are pruned so the file never grows."""
    today = today or local_today()
    floor = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    shown = set(str(k) for k in load_stamp(stamp_file).get("keys") or [])
    shown.update(keys)
    kept = sorted(k for k in shown if k.split("|", 1)[0] >= floor)
    stamp = {"keys": kept, "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    stamp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = stamp_file.with_suffix(stamp_file.suffix + ".tmp")
    tmp.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    tmp.replace(stamp_file)
    return stamp


def digest_items(surface: str = "next-up", stamp_file: Path = STAMP_FILE,
                 today: date | None = None) -> list[tuple[str, str]]:
    """``(key, line)`` for every ingest in the lookback window not yet shown in
    a posted digest. Both CoS surfaces dedupe against the stamp: start-day runs
    once in the morning and yesterday's ingests already went out on next-up."""
    today = today or local_today()
    stamp = load_stamp(stamp_file)
    shown = set(str(k) for k in stamp.get("keys") or [])
    items: list[tuple[str, str]] = []
    for ingest in enrich(recent_ingests(today)):
        if ingest.key in shown:
            continue
        # First run ever (no stamp yet): yesterday's ingests predate this surface
        # and would arrive as a backlog, so only today's count as new.
        if not stamp and ingest.date != today.isoformat():
            continue
        items.append((ingest.key, render_line(ingest, today)))
    return items


def digest_lines(surface: str = "next-up") -> list[str]:
    return [line for _, line in digest_items(surface)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--surface", choices=("next-up", "start-day"), default="next-up")
    parser.add_argument("--json", action="store_true", help="print [{key, line}] instead of lines")
    parser.add_argument("--all", action="store_true", help="ignore the stamp: every ingest in the window")
    parser.add_argument("--stamp-shown", action="store_true",
                        help="read the rendered digest JSON on stdin and record its meeting_ingest_keys as shown")
    args = parser.parse_args()
    if args.stamp_shown:
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        keys = [str(k) for k in (payload.get("meeting_ingest_keys") or []) if k]
        print(json.dumps(stamp_shown(keys)))
        return 0
    if args.all:
        today = local_today()
        items = [(i.key, render_line(i, today)) for i in enrich(recent_ingests(today))]
    else:
        items = digest_items(args.surface)
    if args.json:
        print(json.dumps([{"key": k, "line": line} for k, line in items], ensure_ascii=False))
    else:
        for _, line in items:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
