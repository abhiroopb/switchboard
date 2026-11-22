#!/usr/bin/env python3
"""Build the one JSON feed the CoS Review app reads.

The app (its own cos-review repo, /Applications/CoS Review.app)
never parses raw PM OS state. This script turns the last month of worker
reports, live ledger rows and held Comms Triage drafts into five fixed sections
(feed v3):

- ``pinned``: anything the owner pinned (``pins`` in the actions file), shown first.
- ``needs_you``: decisions, approvals, drafts to send, vetoes. Each item has a
  heading, a 2-3 line summary, the options as the worker reported them, the
  reference links, a paste-ready reply string, the draft as markdown
  (``draft_markdown``, Slack mrkdwn converted; a saved edit wins), and the
  ``thread_id`` / ``workspace_ref`` the reply box delivers to.
- ``running``: live ledger rows (routing, queued, active) with the cmux agent
  state (running, idle, error) and elapsed time; read only.
- ``for_review``: finished work from the last ``FRESH_HOURS`` (48 h) the owner has not
  yet acted on, plus Needs-you items the owner marked as read (``read`` in the actions
  file; they return to Needs you when the row changes after the read).
- ``past``: everything older, plus items the owner completed or skipped (``acted``),
  grouped per PT day in ``past_days`` ("Today (23 Oct)", "Yesterday (22 Oct)",
  "Thursday (21 Oct)", ...), back ``DEFAULT_WINDOW_HOURS`` (30 days).

Sources (read only): ``system/state/running-tasks.json`` (worker reports and
live rows), ``running-task-state.py live_agents`` (cmux agent stamps),
``triage_ledger.pending_drafts`` (held drafts), the ``reply_check`` verdict
cache (already-answered asks are dropped), and
``system/state/cos-review-actions.json`` (complete / skip / snooze / pins /
draft edits / reply log, written only by ``cos_review_actions.py`` and
``cos_review_reply.py``). Link titles (PR title and state, hosted page title, Drive file
name) come from a bounded on-disk cache (``LinkTitles``): at most ``COS_REVIEW_MAX_LOOKUPS``
live lookups per build, none with ``COS_REVIEW_OFFLINE=1``. Nothing else touches the network.

Output: ``system/state/cos-review-feed.json`` in the main checkout. Run from
``command-center-loop.sh`` every tick (``COS_REVIEW_FEED_DISABLED=1`` skips) or
by hand: ``python3 system/scripts/cos_review_feed.py [--print] [--window-hours N]``.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from repo_paths import main_checkout, shared_state_file  # noqa: E402
import private_workstreams  # noqa: E402
import cos_review_asks as asks_mod  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
FEED_VERSION = 4  # v4: link chips labelled by target (kind, short, tooltip) and per-workstream artifact strips
DEFAULT_WINDOW_HOURS = 24 * 30  # Past scrolls back one month
FRESH_HOURS = 48  # finished work younger than this is "For review"; older finished work is "Past"
SECTIONS = ("pinned", "needs_you", "running", "for_review", "past")

# Ledger rows that are automation, never the owner's work.
NOISE_ID_PREFIXES = ("auto-push-", "capacity-", "feedback-pass-", "wr-", "github-")
# One rolling health row per scheduled job (``<job>-health``, written by that job's health pull). It is
# a tracked row like an ``rt-`` task: Needs you while blocked, Past once recovered, so everything listed in
# cmux is also in CoS Review.
HEALTH_ROW_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-health$")
# Landing-admin continuations: nothing for the owner in them.
ADMIN_SUMMARY = re.compile(
    r"^\s*(Nothing to land|No conflict left|No action needed|superseded by|started\b|failed: cmux)",
    re.I,
)
TERMINAL = {"done", "failed", "blocked", "cancelled", "deferred"}
LIVE = {"routing", "queued", "active"}

LINE_MARK = re.compile(r"\s*⏎\s*")
FENCED = re.compile(r"```.*?```", re.S)
MD_LINK = re.compile(r"\[(?P<label>[^\]\n]+)\]\((?P<url>(?:https?|(?:pmos-)?obsidian)://[^\s)]+)\)")
SLACK_LINK = re.compile(r"<(?P<url>https?://[^|>\s]+)\|(?P<label>[^>]+)>")
BARE_URL = re.compile(r"(?P<url>(?:https?|(?:pmos-)?obsidian)://[^\s)>\]\"'`|]+)")
NOTE_PATH = re.compile(r"(?<![\w/])(workstreams/[\w./-]+?\.(?:md|pdf|png|jpg|json|yaml|html))\b")
SLACK_THREAD_REF = re.compile(r"\b(?:thread|channel)\s+(C[A-Z0-9]{8,})\s+ts\s+(\d{10})\.(\d{6})")
LABEL_RESIDUE = re.compile(
    r"\b(?:Review note|Review|Thread|Note|Notes|PR|Doc|Site|Hosted code|Console|Repo|Posted text)\s*:\s*"
    r"(?=(?:Review note|Review|Thread|Note|Notes|PR|Doc|Site|Console|Repo|Posted text)\s*:|[.;,)]|\s*$|\s*[A-Z(\[])",
    re.I,
)
BARE_LABELS_BEFORE_LIST = re.compile(
    r"\b(?:Thread|Review note|Review|Note)(?:\s+(?:Thread|Review note|Review|Note))*\s+"
    r"(?=(?:Vetoes?|To veto|Decisions? (?:to veto|for the owner)|Draft for the owner)\b)",
)
EMPTY_PARENS = re.compile(r"\(\s*\)|\(\s*(?:twenty-\w+|\w+) pass\s*\)")
ROBOT = re.compile(r":robot_face:")

# Signals, strongest first. Kind names drive the section, options and reply text.
# A draft needs explicit phrasing; a fenced block alone is often a quoted post or log.
SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("console", re.compile(r"approve (?:or edit )?in the console|console-approved|in the console\b", re.I)),
    ("ruling", re.compile(r"NEEDS RULING|Decisions for the owner\s*:|needs? (?:your|a product) (?:call|ruling)|Deviation needing the owner|need(?:s|ing) the owner\b|the owner's (?:call|ruling)\b", re.I)),
    ("go", re.compile(r"waiting on the owner(?:'s)? go|the owner's go\b|on your word|until you approve", re.I)),
    ("draft", re.compile(r"Draft for the owner to send|Filing promised|draft(?:s)? created, not sent|\bnot sent\b", re.I)),
    ("post", re.compile(r"for the owner to (?:edit and )?post|post(?:s)? (?:himself|themselves)|Post text as it reads", re.I)),
    # "9 veto items in note §13" counts as vetoes too, not only "vetoes" / "decisions to veto".
    ("vetoes", re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?:decisions? to veto|veto(?:es)?(?:\s+items?)?\b)|Decisions to veto\s*:|Choice to veto|\bVetoes?(?:\s+first)?\s*:|\bTo veto\s*:|veto-able", re.I)),
]
NEEDS_YOU_KINDS = {"blocked", "failed", "ruling", "go", "draft", "post", "console", "triage", "stalled", "ask", "followup"}
# Kinds that show on their own even when a newer row in the same workstream already shows.
STANDALONE_KINDS = {"blocked", "ruling", "draft", "console", "go", "post", "failed"}
# Kinds that never fold, even when the workstream already shows a row of the same kind: every done report
# with vetoes is the owner's to read, so its vetoes never fold under "+N earlier passes".
ALWAYS_STANDALONE_KINDS = {"vetoes"}

DEFAULT_OPTIONS = {
    "draft": ["Send as is", "Edit, then send", "Do not send"],
    "post": ["Open the Doc and post", "Ask for edits"],
    "go": ["Go", "Hold"],
    "ruling": ["Decide (reply in CoS)"],
    "blocked": ["Unblock (say how)", "Drop it"],
    "failed": ["Retry", "Drop it"],
    "console": ["Approve in the console", "Skip"],
    "vetoes": ["No vetoes", "Veto (name which)"],
    # A done report's "Follow-up: <what>" line (work the worker left open on purpose): its own Needs You
    # item beside the row's for-review item, so the open work reaches the owner's list.
    "followup": ["Dispatch the follow-up", "Drop it"],
    "review": [],
}



# --------------------------------------------------------------------------- paths

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def git_toplevel(path: Path) -> Path | None:
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if probe.returncode or not probe.stdout.strip():
        return None
    return Path(probe.stdout.strip())


def repo_root() -> Path:
    """Main checkout: from the cwd's git toplevel when it is a pm-os checkout, else this file's."""
    for candidate in (git_toplevel(Path.cwd()), SCRIPT_DIR.parents[1]):
        if candidate and (candidate / "system" / "scripts").is_dir():
            return main_checkout(candidate)
    return main_checkout(SCRIPT_DIR.parents[1])


def ledger_path(root: Path) -> Path:
    return Path(os.environ.get("RUNNING_TASKS_FILE", root / "system" / "state" / "running-tasks.json"))


def state_dir(root: Path) -> Path:
    """``PM_OS_STATE_DIR`` (hermetic tests, dispatch.py's own override) or ``<root>/system/state``."""
    return Path(os.environ.get("PM_OS_STATE_DIR") or root / "system" / "state")


def feed_path(root: Path) -> Path:
    return Path(os.environ.get("COS_REVIEW_FEED_FILE", state_dir(root) / "cos-review-feed.json"))


def actions_path(root: Path) -> Path:
    return Path(os.environ.get("COS_REVIEW_ACTIONS_FILE", state_dir(root) / "cos-review-actions.json"))


def router_config(root: Path) -> dict[str, Any]:
    path = root / "system" / "config" / "router.yaml"
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- time

def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pt_label(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(PT)
    today = now.astimezone(PT).date()
    if local.date() == today:
        return local.strftime("%H:%M PT")
    if local.date() == today - timedelta(days=1):
        return local.strftime("Yesterday %H:%M PT")
    return local.strftime("%a %H:%M PT")


def reply_at_label(value: Any, now: datetime) -> str:
    """A stored reply's ``written_at`` is UTC ISO; the pane shows Pacific Time (timezone rule)."""
    moment = parse_iso(value)
    if not moment:
        return str(value or "")
    local = moment.astimezone(PT)
    if local.date() == now.astimezone(PT).date():
        return local.strftime("%H:%M PT")
    return local.strftime("%a %d %b %H:%M PT")


def day_label(moment: datetime, now: datetime) -> str:
    """Past group header: "Today (23 Oct)", "Yesterday (22 Oct)", then "Thursday (21 Oct)"."""
    local = moment.astimezone(PT).date()
    today = now.astimezone(PT).date()
    date_part = f"{local.day} {local.strftime('%b')}"
    if local == today:
        return f"Today ({date_part})"
    if local == today - timedelta(days=1):
        return f"Yesterday ({date_part})"
    return f"{local.strftime('%A')} ({date_part})"


def elapsed_label(start: datetime | None, now: datetime) -> str:
    if not start:
        return ""
    minutes = max(0, int((now - start).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("finished_at", "updated_at", "created_at"):
        moment = parse_iso(row.get(key))
        if moment:
            return moment
    return None


# --------------------------------------------------------------------------- text

def normalize(text: str) -> str:
    return LINE_MARK.sub("\n", text or "")


def drafts_in(text: str) -> list[str]:
    out = []
    for block in FENCED.findall(text):
        body = block.strip("`").strip()
        if body:
            out.append(body)
    return out


def prose_of(text: str) -> str:
    """Report prose without drafts, links, paths and the empty label residue they leave."""
    prose = FENCED.sub(" ", text)
    prose = MD_LINK.sub(" ", prose)
    prose = SLACK_LINK.sub(lambda m: m.group("label"), prose)
    prose = BARE_URL.sub(" ", prose)
    prose = NOTE_PATH.sub(" ", prose)
    prose = re.sub(r"Open in Obsidian\s*:?", " ", prose)
    # report-to-cos.sh writes "; thread <link> Posted text: …"; with the link gone only the labels remain.
    prose = re.sub(r"[;,]?\s*\bthread\s+(?=Posted text\s*:)", " ", prose)
    prose = ROBOT.sub("🤖", prose)
    prose = EMPTY_PARENS.sub(" ", prose)
    for _ in range(3):
        prose = LABEL_RESIDUE.sub(" ", prose)
    # Labels left bare (no colon) ahead of a vetoes list once their links are gone: "Thread Review note Vetoes first:".
    prose = BARE_LABELS_BEFORE_LIST.sub("", prose)
    prose = re.sub(r"\s*\n\s*", " ", prose)
    prose = re.sub(r"\s{2,}", " ", prose)
    # Tighten punctuation, but never glue a dotfile path (".amp/in/…") onto the word before it.
    prose = re.sub(r"\s+([.,;:)])(?![\w/])", r"\1", prose)
    prose = re.sub(r",\s*([;.])", r"\1", prose)
    prose = re.sub(r"\.\s*;", ".", prose)
    prose = re.sub(r"(?:^|\s)[.;:]+(?=\s|$)", " ", prose)
    return prose.strip()


def sentences(prose: str) -> list[str]:
    # A numbered or lettered list stays one sentence: never split before "(2)", "(b)" or "2. " so a summary
    # never starts mid-list ("(2) the Sample drip …") or shows item 1 without the rest.
    parts = asks_mod.SENTENCE_SPLIT.split(prose)
    return [p.strip() for p in parts if p.strip()]


# Noise a summary never carries: the filing warning (the pane shows it as its own badge) and the label
# residue a report-to-cos wrapper leaves ("Thread Review note Vetoes first:", "Review: —").
SUMMARY_NOISE = re.compile(r"^(?:⚠️|Filing promised\b)", re.I)
SUMMARY_LABEL_RESIDUE = re.compile(r"^(?:Thread Review note|Review note|Thread|Review)\s*:?\s*(?:[—–-]+\s*)*(?=[A-Z0-9(])")


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:(") + "…"


def strip_status_prefix(text: str) -> str:
    return re.sub(r"^(?:done|failed|blocked|cancelled)\s*:\s*", "", text, flags=re.I).strip()


def title_of(prose: str) -> str:
    first = sentences(prose)[:1]
    head = strip_status_prefix(first[0] if first else prose)
    head = head.rstrip(".;")
    return clip(head, 100) or "Report"


SUMMARY_LIMIT = 280
# A quoted mention ('in the note under "Draft for the owner to send".') is prose, not a marker: the span
# never crosses a quote or a full stop and never starts inside one.
DRAFT_MARKER_PREFIX = re.compile(r'(?<!")(?:Draft for the owner to send|Body as sent|Post text as it reads)\b[^:".]{0,90}(?::\s*|$)', re.I)
STUB_TAIL = re.compile(r"\b(?:in|the|of|to|at|for|a|an|on|with|from|under|via|is|are)\s*[:;,]?\s*$", re.I)


# A part that begins inside a numbered list ("Never folded. 2. Why 0 fixed: …") or is a list fragment
# ("39 opt-out group (…)→Alder+Perry S", "18,22→Yael Example;") reads as nonsense on its own.
MID_LIST = re.compile(r"^.{0,60}?[.;:]\s+\(?(?:[2-9]|1\d)[).]\s")
LIST_FRAGMENT = re.compile(r"^[\d,()+ ]*\d[\d,()+ ]*(?:→|->)|^\(?[a-h]\)\s|;$")


def summary_of(prose: str, title: str, kind: str = "review") -> str:
    """Two or three sentences, the ones that carry the needs-you signal first."""
    parts = sentences(prose)
    opening = strip_status_prefix(parts[0]) if parts else ""
    if parts and opening.rstrip(".;").startswith(title.rstrip("…")):
        parts = parts[1:]
        if kind in {"draft", "go", "post"} and len(opening) > len(title) + 24:
            # The opening sentence carries more than the title did ("Relabel done, … fixed 0 of 45"):
            # keep it rather than start the summary mid-report.
            parts = [opening] + parts
    parts = [p for p in parts if not MID_LIST.match(p) and not LIST_FRAGMENT.search(p)]
    # The draft block is shown on its own; its marker ("Draft for the owner to send (#chan):") is noise here.
    parts = [DRAFT_MARKER_PREFIX.sub("", p).strip() for p in parts]
    parts = [SUMMARY_LABEL_RESIDUE.sub("", p) for p in parts if not SUMMARY_NOISE.match(p)]
    # Drop stubs left where a link or draft block was removed ("Post text as it reads in the").
    parts = [p for p in parts if len(p) >= 20 and not STUB_TAIL.search(p)]
    # A sentence split inside "(… v0.25. CI green)" leaves "CI green), …"; drop the orphan tail.
    parts = [re.sub(r"^[^()]*\)\s*,?\s*", "", p) if p.count(")") > p.count("(") else p for p in parts]
    parts = [asks_mod.capitalize_first(re.sub(r"[;:,]$", ".", p)) for p in parts if len(p) >= 20]
    signal = dict(SIGNALS).get(kind)
    if signal and kind not in {"go", "draft", "post"}:
        # A go/draft/post report reads in order; reordering would pull a later sentence to the front.
        # Rulings and vetoes still lead with the sentence that names the decision.
        parts = [p for p in parts if signal.search(p)] + [p for p in parts if not signal.search(p)]
    out: list[str] = []
    for part in parts:
        # Whole sentences only: the card shows the full summary, never a mid-sentence "…".
        if out and len(" ".join(out + [part])) > SUMMARY_LIMIT:
            break
        out.append(part)
        if len(out) == 3:
            break
    return " ".join(out)


# --------------------------------------------------------------------------- links

# (needle, kind): a bare host needle matches the host; a needle with "/" matches host+path.
LINK_KINDS = [
    ("github.com", "GitHub"),
    ("slack.com", "Slack"),
    ("docs.google.com/document", "Doc"),
    ("docs.google.com/spreadsheets", "Sheet"),
    ("docs.google.com/presentation", "Slides"),
    ("docs.google.com/forms", "Form"),
    ("drive.google.com", "Drive"),
    ("mail.google.com", "Gmail"),
    ("pages.example.com", "Page"),
    ("sessions.example.com", "Session"),
    ("jenkins", "Jenkins"),
    ("linear.app", "Linear"),
    ("jira", "Jira"),
    ("atlassian.net", "Jira"),
    ("ampcode.com/threads", "Thread"),
]
GOOGLE_KINDS = {"Doc": "Google Doc", "Sheet": "Google Sheet", "Slides": "Google Slides", "Form": "Google Form",
                "Drive": "Google Drive"}
# Chip order on an item; kinds not listed take the "Link" slot.
LINK_ORDER = ["PR", "Issue", "Commit", "GitHub", "Slack", "Doc", "Sheet", "Slides", "Form", "Drive", "Gmail", "Linear",
              "Jira", "Page", "Session", "Jenkins",
              "File", "Link", "Note", "Thread"]
MAX_LINKS = 6
MAX_PER_KIND = {"Session": 2, "Link": 2, "Jenkins": 1}
# Kinds that outlive the report that named them: the per-workstream artifacts strip.
ARTIFACT_KINDS = ["PR", "Issue", "Page", "Doc", "Sheet", "Slides", "Form", "Drive", "File", "Note"]
MAX_ARTIFACTS = 6
MAX_ARTIFACT_NOTES = 2
GOOGLE_ID = re.compile(r"/(?:d|folders)/(?P<id>[\w-]{10,})")
GITHUB_PATH = re.compile(r"^/(?P<repo>[^/]+/[^/]+)(?:/(?P<what>pull|issues|commit)/(?P<ref>[0-9a-f]+))?")
HTML_TITLE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.I | re.S)
FRONTMATTER_TITLE = re.compile(r"^title:\s*[\"']?(?P<title>.+?)[\"']?\s*$", re.M)
MD_H1 = re.compile(r"^#\s+(?P<title>[^\n#].*?)\s*$", re.M)
LINK_CACHE_VERSION = 1
LOOKUP_TIMEOUT = 8
GENERIC_HINTS = {"open link", "link", "here", "this", "url", "the thread", "thread", "pr", "note", "site", "doc"}


def link_kind(url: str) -> str:
    """What a URL points at, by host and path, never by words inside an unrelated path segment."""
    if url.startswith("obsidian://"):
        return "Note"
    if url.startswith("file://"):
        return "File"
    parsed = urlsplit(url)
    host_path = f"{parsed.netloc}{parsed.path}"
    for needle, kind in LINK_KINDS:
        matched = needle in parsed.netloc if "/" not in needle else host_path.startswith(needle) or needle in host_path
        if not matched:
            continue
        if kind == "GitHub":
            path = GITHUB_PATH.match(parsed.path)
            what = path.group("what") if path else None
            return {"pull": "PR", "issues": "Issue", "commit": "Commit"}.get(what or "", "GitHub")
        return kind
    return "Link"


def canonical_url(url: str) -> str:
    url = url.rstrip(".,;:")
    if url.startswith("obsidian://"):
        url = unquote(url)
        # "file=path/note.md" and "file=path/note" open the same note.
        url = re.sub(r"(file=[^&]+?)\.md(?=&|$)", r"\1", url)
    return url


def dedupe_key(url: str, kind: str) -> str:
    """Two URLs that open the same thing share a key: a PR and its commits/comments/discussions, a Drive
    file with any ``?usp=`` or ``#heading=`` tail, a hosted page with or without its trailing slash."""
    parsed = urlsplit(url)
    if kind in {"Note", "File"}:
        return url
    if kind in {"PR", "Issue"}:
        path = GITHUB_PATH.match(parsed.path)
        if path:
            return f"https://{parsed.netloc}/{path.group('repo')}/{path.group('what')}/{path.group('ref')}"
    if kind in GOOGLE_KINDS:
        match = GOOGLE_ID.search(parsed.path) or re.search(r"[?&]id=(?P<id>[\w-]{10,})", url)
        if match:
            return f"google:{kind}:{match.group('id')}"
    path = parsed.path.rstrip("/")
    keep_query = kind in {"Link", "Gmail"}
    query = f"?{parsed.query}" if keep_query and parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{query}"


def resolve_note_url(url: str, root: Path) -> str:
    """The obsidian URL for a note that exists in the vault, or "" when none does. A note a report linked
    while its workstream was live may since have moved to ``workstreams/_archive/<slug>/``, and a report may
    name the slug in the wrong number (``team-offsite`` for ``team-offsites``); both resolve."""
    query = parse_qs(urlsplit(url).query)
    file_param = (query.get("file") or [""])[0]
    if not file_param:
        return ""
    rel = file_param[:-3] if file_param.endswith(".md") else file_param
    candidates = [rel]
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "workstreams" and parts[1] != "_archive":
        slug = parts[1]
        variants = {slug, slug + "s", slug[:-1] if slug.endswith("s") else slug}
        for variant in sorted(variants - {slug}) :
            candidates.append("/".join([parts[0], variant, *parts[2:]]))
        for variant in sorted(variants):
            candidates.append("/".join([parts[0], "_archive", variant, *parts[2:]]))
    for candidate in candidates:
        if (root / f"{candidate}.md").is_file():
            return f"obsidian://open?vault=pm-os&file={candidate}"
    return ""


def note_path(url: str, root: Path) -> Path | None:
    file_param = (parse_qs(urlsplit(url).query).get("file") or [""])[0]
    return (root / f"{file_param}.md") if file_param else None


_NOTE_TITLES: dict[str, str] = {}


def note_title(path: Path) -> str:
    """``title:`` from the note's frontmatter, else its H1, else the stem without its date, humanised."""
    key = str(path)
    if key not in _NOTE_TITLES:
        title = ""
        try:
            with path.open(errors="replace") as handle:
                head = "".join(handle.readline() for _ in range(40))
        except OSError:
            head = ""
        match = FRONTMATTER_TITLE.search(head) if head.startswith("---") else None
        if not match:
            match = MD_H1.search(head)
        if match:
            title = match.group("title").strip().strip("\"'")
        if not title:
            stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
            title = stem.replace("-", " ").replace("_", " ")
            title = title[:1].upper() + title[1:]
        _NOTE_TITLES[key] = title
    return _NOTE_TITLES[key]


class LinkTitles:
    """Titles and states for the links the feed shows (PR title and draft/open/merged state, hosted page
    title, Drive file or folder name), looked up once and cached on disk so a feed build stays cheap and
    works offline. Bounded: ``COS_REVIEW_MAX_LOOKUPS`` (default 12) fetches per build, 8 s each, a 1 h
    backoff after a failure; ``COS_REVIEW_OFFLINE=1`` never fetches. Open and draft PRs refresh every 6 h,
    everything else weekly."""

    LIVE_TTL = timedelta(hours=6)
    SETTLED_TTL = timedelta(days=7)
    BACKOFF = timedelta(hours=1)

    def __init__(self, root: Path, now: datetime) -> None:
        self.root = root
        self.now = now
        self.path = state_dir(root) / "cos-review-link-cache.json"
        data = load_json(self.path, {})
        entries = data.get("entries") if isinstance(data, dict) and data.get("version") == LINK_CACHE_VERSION else {}
        self.entries: dict[str, dict[str, Any]] = entries if isinstance(entries, dict) else {}
        self.offline = bool(os.environ.get("COS_REVIEW_OFFLINE"))
        self.budget = int(os.environ.get("COS_REVIEW_MAX_LOOKUPS") or 12)
        self.lookups = 0
        self.dirty = False

    def get(self, key: str, kind: str, url: str) -> dict[str, Any]:
        entry = self.entries.get(key) or {}
        if self.offline or kind not in {"PR", "Issue", "Page", *GOOGLE_KINDS}:
            return entry
        fetched = parse_iso(entry.get("fetched_at"))
        failed = parse_iso(entry.get("failed_at"))
        ttl = self.LIVE_TTL if entry.get("live") else self.SETTLED_TTL
        if fetched and self.now - fetched < ttl:
            return entry
        if failed and self.now - failed < self.BACKOFF:
            return entry
        if self.lookups >= self.budget:
            return entry
        self.lookups += 1
        fresh = fetch_title(kind, url)
        if fresh:
            entry = {**entry, **fresh, "fetched_at": iso(self.now)}
            entry.pop("failed_at", None)
        else:
            entry = {**entry, "failed_at": iso(self.now)}
        self.entries[key] = entry
        self.dirty = True
        return entry

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".cos-review-link-cache.", dir=str(self.path.parent))
            with os.fdopen(fd, "w") as handle:
                json.dump({"version": LINK_CACHE_VERSION, "updated_at": iso(self.now), "entries": self.entries},
                          handle, indent=1, ensure_ascii=False)
            _os_replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


_LINK_TITLES: dict[str, LinkTitles] = {}


def link_titles(root: Path, now: datetime | None = None) -> LinkTitles:
    key = str(root)
    if key not in _LINK_TITLES:
        _LINK_TITLES[key] = LinkTitles(root, now or datetime.now(timezone.utc))
    elif now:
        _LINK_TITLES[key].now = now
    return _LINK_TITLES[key]


def run_lookup(cmd: list[str]) -> str:
    try:
        # errors="replace": a title lookup can land on a binary body (PDF, image); undecodable bytes must
        # not kill the whole rebuild.
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=LOOKUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def fetch_title(kind: str, url: str) -> dict[str, Any]:
    """One live lookup for a PR, issue, hosted page, or Drive file. ``{}`` when it fails."""
    if kind == "PR":
        out = run_lookup(["gh", "pr", "view", url, "--json", "title,state,isDraft"])
        data = load_json_text(out)
        if not data.get("title"):
            return {}
        state = "draft" if data.get("isDraft") else str(data.get("state") or "").lower()
        return {"title": str(data["title"]).strip(), "state": state, "live": state in {"draft", "open"}}
    if kind == "Issue":
        out = run_lookup(["gh", "issue", "view", url, "--json", "title,state"])
        data = load_json_text(out)
        if not data.get("title"):
            return {}
        state = str(data.get("state") or "").lower()
        return {"title": str(data["title"]).strip(), "state": state, "live": state == "open"}
    if kind == "Page":
        out = run_lookup(["curl", "-sfL", "--max-time", str(LOOKUP_TIMEOUT), url])
        match = HTML_TITLE.search(out or "")
        title = html.unescape(re.sub(r"\s+", " ", match.group("title"))).strip() if match else ""
        return {"title": title, "live": False} if title else {}
    if kind in GOOGLE_KINDS:
        match = GOOGLE_ID.search(urlsplit(url).path) or re.search(r"[?&]id=(?P<id>[\w-]{10,})", url)
        if not match:
            return {}
        out = run_lookup(["pmos", "drive", "info", "--file-id-or-url", match.group("id")])
        data = load_json_text(out)
        file = data.get("file") if isinstance(data.get("file"), dict) else data
        name = str(file.get("name") or "").strip() if isinstance(file, dict) else ""
        if not name:
            return {}
        return {"title": name, "mime": str(file.get("mime_type") or ""), "live": False}
    return {}


def load_json_text(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        data = json.loads(text[start:])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def extract_links(text: str, root: Path) -> list[dict[str, Any]]:
    """Every link in ``text`` as a slot ``{key, kind, url, hint, urls}``, deduped by target, in order of first
    mention. ``url`` is the URL a chip opens (a PR's own page, not the comment anchor); ``urls`` every
    distinct form that was linked. Cheap: no lookups here."""
    slots: dict[str, dict[str, Any]] = {}

    def add(url: str, hint: str = "") -> None:
        if url.startswith("pmos-obsidian://"):
            # The Claude click link for a note is the same pointer as its obsidian:// URI.
            url = url[len("pmos-"):]
        url = canonical_url(url)
        if "<" in url or ">" in url:
            return   # a placeholder ("<token>") a note never filled in
        if url.startswith("obsidian://"):
            resolved = resolve_note_url(url, root)
            if not resolved:
                return   # a note that does not exist in the vault is a dead chip, never shown
            url = resolved
        kind = link_kind(url)
        key = dedupe_key(url, kind)
        slot = slots.get(key)
        if slot is None:
            open_url = key if kind in {"PR", "Issue"} else url
            slot = slots[key] = {"key": key, "kind": kind, "url": open_url, "hint": "", "urls": []}
        if url not in slot["urls"]:
            slot["urls"].append(url)
        if hint and not slot["hint"] and hint.strip().lower() not in GENERIC_HINTS:
            slot["hint"] = hint.strip()

    for match in MD_LINK.finditer(text):
        add(match.group("url"), match.group("label"))
    for match in SLACK_LINK.finditer(text):
        add(match.group("url"), match.group("label"))
    for match in BARE_URL.finditer(text):
        add(match.group("url"))
    for match in SLACK_THREAD_REF.finditer(text):
        add(f"https://example.slack.com/archives/{match.group(1)}/p{match.group(2)}{match.group(3)}")
    for match in NOTE_PATH.finditer(text):
        rel = match.group(1)
        if rel.endswith(".md"):
            # ``add`` resolves the note through ``resolve_note_url``, so a bare path to a note whose
            # workstream has since moved to ``_archive/`` resolves the same way an obsidian URL does.
            add(f"obsidian://open?vault=pm-os&file={rel[:-3]}")
        elif (root / rel).exists():
            add((root / rel).resolve().as_uri())
    return list(slots.values())


def link_entry(slot: dict[str, Any], root: Path | None, titles: LinkTitles | None,
               channels: dict[str, str] | None = None) -> dict[str, str]:
    """The chip for one slot: ``label`` names the target ("PR #10001: checkout copy (draft)",
    "Page: Docs search skill · before and after", "Note: Pass 8 review"), ``short`` fits a strip
    ("PR #10001 (draft)"), ``tooltip`` carries the title and every URL that was linked."""
    kind, url, hint = slot["kind"], slot["url"], slot.get("hint") or ""
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else parsed.netloc
    info = titles.get(slot["key"], kind, url) if titles else {}
    title = str(info.get("title") or "")
    state = str(info.get("state") or "")
    label = short = kind
    if kind in {"PR", "Issue"}:
        match = GITHUB_PATH.match(parsed.path)
        number = match.group("ref") if match else tail
        head = f"{kind} #{number}"
        suffix = f" ({state})" if state else ""
        label = f"{head}: {clip(title, 60)}{suffix}" if title else head + suffix
        short = head + suffix
    elif kind == "Commit":
        label = short = f"Commit {tail[:7]}"
    elif kind == "GitHub":
        match = GITHUB_PATH.match(parsed.path)
        label = short = f"GitHub: {match.group('repo')}" if match else "GitHub"
    elif kind == "Slack":
        channel = SLACK_REF_CHANNEL.search(parsed.path)
        cid = channel.group("id") if channel else ""
        name = (channels or {}).get(cid, "")
        if name:
            label = short = f"Slack: #{name}"
        elif cid.startswith("D"):
            label = short = "Slack DM"
        elif hint:
            label = short = f"Slack: {clip(hint, 40)}"
    elif kind in GOOGLE_KINDS:
        name = title or hint
        if name:
            label = f"{kind}: {clip(name, 60)}"
            short = f"{kind}: {clip(name, 40)}"
        else:
            label = short = GOOGLE_KINDS[kind]
    elif kind == "Page":
        site = re.search(r"/sites/([^/]+)", path)
        name = title or (site.group(1) if site else tail)
        label = f"Page: {clip(name, 60)}"
        short = f"Page: {clip(name, 40)}"
    elif kind == "Session":
        label = short = f"Session {clip(tail.removeprefix('mbc-task-'), 14)}"
    elif kind == "Jenkins":
        label = short = f"Jenkins build {tail}" if tail.isdigit() else "Jenkins"
    elif kind == "Linear":
        match = re.search(r"/issue/([A-Z]+-\d+)", path)
        label = short = f"Linear {match.group(1)}" if match else "Linear"
    elif kind == "Jira":
        match = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", path)
        label = short = f"Jira {match.group(1)}" if match else "Jira"
    elif kind == "Note":
        note = note_path(url, root) if root else None
        hidden = note and private_workstreams.is_private_path(_note_rel(note, root), private_workstreams.slugs(root))
        name = note_title(note) if note and not hidden else ""  # a private note's title stays in the note
        if not name:
            file_param = (parse_qs(parsed.query).get("file") or [""])[0]
            name = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", file_param.rsplit("/", 1)[-1])
        title = title or name
        label = f"Note: {clip(name, 60)}"
        short = f"Note: {clip(name, 32)}"
    elif kind == "File":
        name = unquote(tail)
        label = short = f"File: {clip(name, 40)}"
    elif kind == "Link":
        if hint:
            label = short = clip(hint, 30)
        else:
            host = parsed.netloc.removeprefix("www.")
            label = short = clip(f"{host}/{tail}" if tail and path else host, 30)
    tooltip_lines = ([title] if title and title not in label else []) + list(slot.get("urls") or [url])
    return {"kind": kind, "label": label, "short": short, "url": url, "tooltip": "\n".join(tooltip_lines)}


def links_of(text: str, root: Path, titles: LinkTitles | None = None) -> list[dict[str, str]]:
    """The chips for one item: deduped by target, ordered by kind, capped at ``MAX_LINKS``; titles
    resolved only for the chips kept."""
    slots = extract_links(text, root)
    per_kind: dict[str, int] = {}
    ordered: list[dict[str, Any]] = []
    for kind_name in LINK_ORDER:
        for slot in slots:
            kind = slot["kind"] if slot["kind"] in LINK_ORDER else "Link"
            if kind != kind_name:
                continue
            cap = MAX_PER_KIND.get(kind, 3)
            if per_kind.get(kind, 0) >= cap:
                continue
            per_kind[kind] = per_kind.get(kind, 0) + 1
            ordered.append(slot)
    channels = channel_names(root)
    return distinguish_labels([link_entry(slot, root, titles, channels) for slot in ordered[:MAX_LINKS]])


def single_link(url: str, channels: dict[str, str] | None = None) -> dict[str, str]:
    """One chip for a URL that is not inside report text (a draft's source, a worker thread)."""
    kind = link_kind(url)
    slot = {"key": dedupe_key(url, kind), "kind": kind, "url": url, "hint": "", "urls": [url]}
    return link_entry(slot, None, None, channels)


def label_for(url: str, hint: str = "") -> str:
    """Offline label for one URL (no title lookup)."""
    kind = link_kind(url)
    slot = {"key": dedupe_key(url, kind), "kind": kind, "url": url, "hint": hint, "urls": [url]}
    return link_entry(slot, None, None)["label"]


def distinguish_labels(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Chips never repeat a label: two different targets that resolve to the same words get an ordinal."""
    counts: dict[str, int] = {}
    for link in links:
        counts[link["label"]] = counts.get(link["label"], 0) + 1
    seen: dict[str, int] = {}
    out: list[dict[str, str]] = []
    for link in links:
        label = link["label"]
        if counts[label] > 1:
            seen[label] = seen.get(label, 0) + 1
            link = {**link, "label": f"{label} · {seen[label]}", "short": f"{link.get('short') or label} · {seen[label]}"}
        out.append(link)
    return out


# --------------------------------------------------------------------------- workstream artifacts

def context_text(root: Path, slug: str) -> str:
    for folder in (root / "workstreams" / slug, root / "workstreams" / "_archive" / slug):
        path = folder / "CONTEXT.md"
        if path.is_file():
            try:
                return path.read_text(errors="replace")
            except OSError:
                return ""
    return ""


def workstream_artifacts(root: Path, slug: str, rows: list[dict[str, Any]], titles: LinkTitles | None) -> dict[str, Any]:
    """The durable artifacts of one workstream (PRs with state, hosted pages, Drive files and folders,
    exported files, notes), in CONTEXT.md order first (the curated record: the workstream's own PR leads,
    a sibling workstream's PR it merely mentions comes after) then its ledger rows newest first, so a
    draft or report item shows "PR #10001 (draft) · Page: Docs search skill · Note" even when its
    own report did not repeat them. Grouped by kind, capped at ``MAX_ARTIFACTS``
    distinct targets with the rest counted in ``overflow``; notes take at most ``MAX_ARTIFACT_NOTES`` of
    the slots and the first note always gets one, so a strip never drops the workstream's note for a
    fifth Drive file."""
    own = [r for r in rows if str(r.get("workstream") or "") == slug and tracked_id(str(r.get("id") or ""))
           and r.get("status") != "cancelled"]
    own.sort(key=lambda r: row_time(r) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    seen: set[str] = set()
    slots: list[dict[str, Any]] = []
    for text in [context_text(root, slug)] + [normalize(str(r.get("summary") or "")) for r in own]:
        for slot in extract_links(text, root):
            if slot["kind"] not in ARTIFACT_KINDS or slot["key"] in seen:
                continue
            seen.add(slot["key"])
            slots.append(slot)
    notes = [s for s in slots if s["kind"] == "Note"][:MAX_ARTIFACT_NOTES]
    others = [s for s in slots if s["kind"] != "Note"]
    ordered = sorted(others + notes, key=lambda s: ARTIFACT_KINDS.index(s["kind"]))
    kept, rest = ordered[:MAX_ARTIFACTS], ordered[MAX_ARTIFACTS:]
    if notes and notes[0] not in kept:
        kept, rest = kept[:-1] + [notes[0]], [kept[-1]] + [s for s in rest if s is not notes[0]]
    return {
        "name": workstream_name(root, slug),
        "artifacts": distinguish_labels([link_entry(slot, root, titles) for slot in kept]),
        "overflow": len(rest) + max(0, len([s for s in slots if s["kind"] == "Note"]) - len(notes)),
    }


def workstream_index(root: Path, rows: list[dict[str, Any]], titles: LinkTitles | None) -> dict[str, dict[str, Any]]:
    """``slug -> {name, artifacts, overflow}`` for every workstream with ledger rows or an active folder."""
    slugs = {str(r.get("workstream") or "") for r in rows if tracked_id(str(r.get("id") or ""))}
    folder = root / "workstreams"
    if folder.is_dir():
        slugs |= {p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "CONTEXT.md").is_file()}
    slugs.discard("")
    return {slug: workstream_artifacts(root, slug, rows, titles) for slug in sorted(slugs)}


# --------------------------------------------------------------------------- classification

def classify(row: dict[str, Any], prose: str, has_draft: bool) -> str:
    if row.get("status") == "blocked":
        return "blocked"
    if row.get("status") == "failed":
        return "failed"
    if row.get("needs_ruling"):
        return "ruling"
    for kind, pattern in SIGNALS:
        if pattern.search(prose):
            if kind == "draft" and not has_draft and not re.search(r"Filing promised|Draft for the owner|drafts? created, not sent", prose, re.I):
                continue
            return kind
    return "review"


def section_for(kind: str) -> str:
    return "needs_you" if kind in NEEDS_YOU_KINDS else "for_review"


def option_label(option: Any) -> str:
    return str(option.get("label") or "") if isinstance(option, dict) else str(option)


def reply_for(task_id: str, kind: str, options: list[Any], recipient: str = "") -> str:
    if kind == "draft":
        who = f" to {recipient}" if recipient else ""
        return f"{task_id}: send the draft{who} as is"
    if kind == "post":
        return f"{task_id}: posted, close it out"
    if kind == "go":
        return f"{task_id}: go"
    if kind in {"ruling", "blocked"}:
        if options and option_label(options[0]).startswith("(a)"):
            return f"{task_id}: go with (a)"
        return f"{task_id}: ruling: <your call>"
    if kind == "failed":
        return f"{task_id}: retry"
    if kind == "vetoes":
        return f"{task_id}: no vetoes"
    if kind == "followup":
        return f"{task_id}: dispatch the follow-up"
    if kind == "console":
        return ""
    return f"{task_id}: reviewed, no changes"


def recipient_of(prose: str) -> str:
    match = re.search(r"Draft for the owner to send(?: to)?\s*\(?\s*(#[\w-]+|[A-Z][a-z]+(?: [A-Z][a-z]+)?(?:'s)? thread|[A-Z][a-z]+ and [A-Z][a-z]+|[A-Z][a-z]+)", prose)
    if not match:
        return ""
    who = match.group(1).strip()
    if who.lower() in {"thread", "the", "in"}:
        return ""
    return who


def heading_for(kind: str, title: str, draft_count: int) -> str:
    if kind == "draft" and draft_count > 1:
        return f"{draft_count} drafts to send: {title}"
    prefix = {"draft": "Draft to send", "post": "To post", "go": "Your go", "ruling": "Ruling", "blocked": "Blocked",
              "failed": "Failed", "console": "Console approvals", "vetoes": "Vetoes", "followup": "Follow-up"}.get(kind)
    if prefix and not title.lower().startswith(prefix.lower()):
        return f"{prefix}: {title}"
    return title


# --------------------------------------------------------------------------- items

_NAMES_CACHE: dict[str, dict[str, Any]] = {}
_WS_NAME_CACHE: dict[str, str] = {}


PEOPLE_SLACK_ID = re.compile(r"\bSlack\b[^\n]{0,40}?\b(U[A-Z0-9]{8,})\b|\((U[A-Z0-9]{8,})\)")
PEOPLE_H1 = re.compile(r"^#\s+(?P<name>[^\n#]+?)\s*$", re.M)


def people_slack_names(root: Path) -> dict[str, dict[str, str]]:
    """Slack user id -> {name} from the ``people/`` profile headers (first lines of each
    ``<first>-<last>.md``; the H1 is the display name). Read only, never a Slack call."""
    found: dict[str, dict[str, str]] = {}
    for folder in (root / "people", main_checkout(root) / "people"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.name.startswith("_") or path.name == "index.md":
                continue
            try:
                with path.open(errors="replace") as handle:
                    head = "".join(handle.readline() for _ in range(15))
            except OSError:
                continue
            title = PEOPLE_H1.search(head)
            if not title:
                continue
            name = re.sub(r"\s*\(.*?\)\s*$", "", title.group("name")).strip()
            for match in PEOPLE_SLACK_ID.finditer(head):
                uid = match.group(1) or match.group(2)
                found.setdefault(uid, {"name": name, "display_name": name})
        if found:
            break
    return found


def slack_names(root: Path) -> dict[str, Any]:
    """Slack user id -> {name, display_name}: ``people/`` profiles first, then the
    shared on-disk cache for ids no profile carries. Read only; never a live Slack lookup."""
    key = str(root)
    if key not in _NAMES_CACHE:
        data = load_json(main_checkout(root) / ".state" / "slack-user-names.json", {})
        merged = dict(data) if isinstance(data, dict) else {}
        merged.update(people_slack_names(root))
        _NAMES_CACHE[key] = merged
    return _NAMES_CACHE[key]


def workstream_name(root: Path, slug: str) -> str:
    """``name:`` from the workstream's config.yaml (active or archived), else the slug in title case."""
    if not slug:
        return "Workstream"
    key = f"{root}:{slug}"
    if key not in _WS_NAME_CACHE:
        name = ""
        for folder in (root / "workstreams" / slug, root / "workstreams" / "_archive" / slug):
            config = folder / "config.yaml"
            if config.is_file():
                match = re.search(r"^name:\s*[\"']?(.+?)[\"']?\s*$", config.read_text(errors="replace"), re.M)
                if match:
                    name = match.group(1).strip()
                    break
        _WS_NAME_CACHE[key] = name or slug.replace("-", " ").title()
    return _WS_NAME_CACHE[key]


def ask_item_id(task_id: str, index: int, title: str, taken: set[str]) -> str:
    """Item id for one ask of a worker report.

    The first ask keeps ``task:<rt>`` so a row with a single ask has a stable, readable id.
    Further asks are keyed by a short hash of their title, not their position, so a re-report
    or parser change that adds, drops or reorders asks does not move the owner's complete / snooze /
    draft records onto a different ask. ``taken`` disambiguates two asks with the same title."""
    if index == 0:
        return f"task:{task_id}"
    import hashlib
    key = re.sub(r"\s+", " ", title or "").strip().lower()
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    candidate = f"task:{task_id}:{digest}"
    bump = 2
    while candidate in taken:
        candidate = f"task:{task_id}:{digest}-{bump}"
        bump += 1
    return candidate


def folded_entry(row: dict[str, Any], root: Path, now: datetime) -> dict[str, Any]:
    """One folded "+N earlier passes" row as the app lists it when the owner opens the fold: title, time, and the
    report's links (thread, note), so Past covers the month's work per workstream, not only the newest row."""
    text = normalize(row.get("summary") or "")
    prose = prose_of(text)
    slug = str(row.get("workstream") or "")
    moment = row_time(row) or now
    thread_id = str(row.get("thread_id") or "")
    thread_url = f"https://ampcode.com/threads/{thread_id}" if thread_id.startswith("T-") else ""
    kind = classify(row, prose, bool(drafts_in(text)))
    return {
        "task_id": str(row.get("id") or ""),
        "title": asks_mod.review_title(prose, str(row.get("prompt") or ""), workstream_name(root, slug)),
        "kind": kind,
        "status": str(row.get("status") or ""),
        "updated_at": iso(moment),
        "updated_label": pt_label(moment, now),
        "thread_url": thread_url,
        "links": links_of(text + ("\n" + thread_url if thread_url else ""), root, link_titles(root, now)),
        "report_markdown": report_markdown(text),
    }


def task_items(row: dict[str, Any], root: Path, now: datetime, extra_rows: int = 0,
               folded: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The feed rows for one ledger row: one needs-you row per ask the owner can decide from the
    list alone, or one for-later row that names the finished artifact. A done report's
    ``Follow-up: <what>`` line adds one needs-you row beside them. ``folded`` are the older
    rows this one hides as "+N earlier passes"; the first item lists them under ``folded``."""
    text = normalize(row.get("summary") or "")
    if row.get("status") == "blocked" and STUCK_REASON.match(text):
        return [stuck_item(row, root, now)]
    drafts = drafts_in(text)
    prose = prose_of(text)
    kind = classify(row, prose, bool(drafts))
    task_id = str(row.get("id"))
    slug = str(row.get("workstream") or "")
    moment = row_time(row) or now
    thread_id = str(row.get("thread_id") or "")
    thread_url = f"https://ampcode.com/threads/{thread_id}" if thread_id.startswith("T-") else ""
    links = links_of(text + ("\n" + thread_url if thread_url else ""), root, link_titles(root, now))
    earlier = f" (+{extra_rows} earlier pass{'es' if extra_rows > 1 else ''} in this workstream)" if extra_rows else ""
    name = workstream_name(root, slug)
    folded_list = [folded_entry(r, root, now) for r in (folded or [])]
    base = {
        "links": links,
        "workstream": slug,
        "task_id": task_id,
        "thread_id": thread_id,
        "thread_url": thread_url,
        "workspace_ref": str(row.get("workspace_ref") or ""),
        "status": str(row.get("status") or ""),
        "updated_at": iso(moment),
        "updated_label": pt_label(moment, now),
        "day_label": day_label(moment, now),
        "source": "task",
        "decisions": [d for d in (row.get("decisions") or []) if isinstance(d, dict)],
        # Context the owner needs to judge the ask without opening the thread: the ask that launched the task and the whole
        # report that produced the item, never the one-sentence ask alone.
        "prompt_excerpt": prompt_excerpt(str(row.get("prompt") or "")),
        "report_markdown": report_markdown(text),
    }
    asks = asks_mod.asks_for(kind, text, prose, drafts, task_id, name, bool(row.get("needs_ruling")), slack_names(root))
    # Work the report leaves open on purpose ("Follow-up: reload plugins in the Scheduled Jobs tab") is its
    # own needs-you item, whatever else the report carries: a done-with-vetoes row keeps its for-review item
    # and veto pair, and the open work still reaches the owner's list. Blocked and failed rows already ask; no second ask there.
    follow = asks_mod.followup_ask(prose, task_id, name) if kind not in {"blocked", "failed", "console"} else None
    seen_ids: set[str] = set()

    def followup_item(index: int) -> dict[str, Any]:
        item = dict(base, **follow.as_dict())
        item["id"] = ask_item_id(task_id, index, follow.title, seen_ids)
        seen_ids.add(item["id"])
        item["recipient"] = ""
        item["gmail_id"] = ""
        item["meaning"] = meaning_of("followup", prose, name, str(row.get("status") or ""), 0, follow)
        item["report_open"] = True
        item["note_section"] = note_section(root, slug, follow.summary, text)
        return item

    if not asks:
        # A for-review row names its subject and outcome, carries the whole report open by default, and offers only the standard veto pair.
        title = asks_mod.review_title(prose, str(row.get("prompt") or ""), name)
        # Any report that lists vetoes is a vetoes row, whatever word it opened with, even "1. … 7." under a
        # review label.
        options = asks_mod.veto_options(prose, task_id)
        # No ask came out of the report, so a needs-you kind the signals guessed (a "draft" with no draft
        # text, a "go" with nothing to approve) is finished work here, never a needs-you meaning line.
        kind = "vetoes" if options else "review"
        reply = f"{task_id}: no vetoes" if options else f"{task_id}: reviewed, no changes"
        veto_count = asks_mod.numbered_veto_count(prose) or (len(asks_mod.ruling_items(prose)) if options else 0)
        item = dict(base, id=f"task:{task_id}", section="for_review", kind=kind, title=title,
                    summary=(summary_of(prose, asks_mod.later_title(prose), kind) + earlier).strip(), options=options,
                    reply_text=reply, reply_surface="CoS", draft_text="\n\n".join(drafts))
        item["meaning"] = meaning_of(kind, prose, name, str(row.get("status") or ""), veto_count)
        item["report_open"] = True
        item["note_section"] = ""
        item["folded"] = folded_list
        seen_ids.add(item["id"])
        return [item, followup_item(1)] if follow else [item]
    row_summary = summary_of(prose, asks_mod.later_title(prose), kind)
    items: list[dict[str, Any]] = []
    for index, ask in enumerate(asks):
        summary = ask.summary or row_summary
        if index == 0:
            summary = (summary + earlier).strip()
        if ask.options_source == "default" and (ask.kind == "blocked" or (ask.kind == "ruling" and ask.options
                                                                            and ask.options[0]["label"].startswith("Decide"))):
            # No Options segment in a blocked or needs-ruling report: the review note's alternatives table
            # and Recommendation lean stand in for the status defaults, labelled "options from note" in the
            # meaning line. A failed row keeps Retry / Drop it: its note's alternatives are not about the failure.
            asks_mod.with_segment(ask, note_options(root, slug, text), task_id, source="note")
        item = dict(base, **ask.as_dict())
        item["id"] = ask_item_id(task_id, index, ask.title, seen_ids)
        seen_ids.add(item["id"])
        item["summary"] = summary
        item["recipient"] = ask.recipient
        item["gmail_id"] = ask.gmail_id
        item["meaning"] = meaning_of(ask.kind, prose, name, str(row.get("status") or ""), 0, ask)
        item["report_open"] = ask.kind in {"ruling", "blocked", "failed"}
        item["note_section"] = note_section(root, slug, ask.summary or ask.title, text)
        if index == 0:
            item["folded"] = folded_list
        items.append(item)
    if follow:
        items.append(followup_item(len(items)))
    return items


# Hard safety ceiling only. The Ask shows the owner's whole ask; the app collapses a long one behind "Show full ask" instead of the feed
# cutting it. Real dispatch prompts run 1,000 to 3,000 characters, so this
# limit never touches one; it only guards the feed against a runaway blob.
PROMPT_EXCERPT_CHARS = 20_000


def prompt_excerpt(prompt: str, limit: int = PROMPT_EXCERPT_CHARS) -> str:
    """The whole ledger prompt after the ``Workstream:`` line: the ask that launched the task, in the owner's or
    CoS's words, fenced drafts kept whole. Cut at a word boundary only past the safety ceiling ``limit``."""
    text = normalize(prompt or "").strip()
    text = PROMPT_PREFIX.sub("", text).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if cut.count("```") % 2 == 1:
        # Never end inside a fenced draft: stop before the fence opens so the pane shows no bare ``` marker.
        cut = cut[: cut.rfind("```")].rstrip()
        if len(cut) < limit // 3:
            cut = text[:limit]
    boundary = max(cut.rfind("\n\n"), cut.rfind(". "), cut.rfind(" "))
    if boundary < limit // 2:
        boundary = len(cut)
    return cut[:boundary].rstrip(" ,;:") + "…"


# `Review: <vault path>.` with the pointer hoisted left `Review:.` in rows stored before review_surface kept the
# basename inline. Those rows never re-render, so the residue goes here.
STORED_EMPTY_POINTER = re.compile(r"(?:(?<=\s)|^)Review(?: note)?:\s*\.(?:[ \t]+|(?=\s|$))")


def report_markdown(text: str) -> str:
    """The worker report as the app renders it: fenced drafts kept whole, Slack ``<url|label>`` links in the
    prose turned into Markdown links so Session and thread links are clickable, ``⏎`` already unfolded."""
    out: list[str] = []
    for index, piece in enumerate(re.split(r"(```.*?```)", normalize(text), flags=re.S)):
        if index % 2 == 1:
            out.append(piece)
            continue
        piece = SLACK_LINK.sub(lambda m: f"[{m.group('label')}]({m.group('url')})", piece)
        piece = SLACK_BARE.sub(lambda m: m.group(0)[1:-1], piece)
        piece = GENERIC_MD_LINK.sub(lambda m: f"[{named_link_label(m.group('url'))}]({m.group('url')})", piece)
        piece = NAKED_URL.sub(lambda m: f"[{named_link_label(m.group('url'))}]({m.group('url')})", piece)
        piece = STORED_EMPTY_POINTER.sub("", piece)
        out.append(piece)
    return "".join(out).strip()


def named_link_label(url: str) -> str:
    """``report-to-cos.sh`` writes every link as ``[Open link](url)``; four of those in a row tell the owner nothing.
    Label by host (``Slack``, ``Session``, ``Thread``) plus the shortest path that still tells them apart."""
    entry = single_link(url)
    label = entry["label"]
    if label != entry["kind"]:
        return label   # already names the target ("PR #10001", "Session abc12345", "Linear EX-12")
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else parsed.netloc
    if label == "Slack":
        parts = [p for p in path.split("/") if p]
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else tail
    tail = clip(tail, 48)
    return f"{label} · {tail}" if tail and tail != label else label


SLACK_BARE = re.compile(r"<https?://[^>\s]+>")
# A URL pasted bare into the report prose (not already the target of a Markdown link).
NAKED_URL = re.compile(r"(?<![(<\w/])(?P<url>https?://[^\s)>\]\"'`|]+)(?<![.,;:])")
GENERIC_MD_LINK = re.compile(r"\[(?:Open link|link|here|Link)\]\((?P<url>https?://[^)\s]+)\)", re.I)
NOTE_SECTION_CHARS = 3000
_NOTE_SECTION_CACHE: dict[tuple[str, str], str] = {}


def ways_forward(ask: Any) -> str:
    """The sentence in a meaning line that names where a card's lettered options came from and which one is
    recommended: "The worker lists 3 ways forward (options from note); lean (a): <reason>." Empty for the
    status defaults, so the owner can tell a real choice from the fallback pair."""
    source = str(getattr(ask, "options_source", "") or "")
    if source not in {"report", "note"}:
        return ""
    lettered = [o for o in (getattr(ask, "options", None) or []) if str(o.get("label", "")).startswith("(")]
    if not lettered:
        return ""
    where = "options from note" if source == "note" else "options from the report"
    line = f"The worker lists {len(lettered)} way{'s' if len(lettered) != 1 else ''} forward ({where})"
    lean = getattr(ask, "lean", None)
    if lean:
        reason = clip(lean.reason, 160).rstrip(".")
        line += f"; lean ({lean.letter}): {reason}" if reason else f"; lean ({lean.letter})"
    else:
        line += "; no lean given"
    return line + "."


def meaning_of(kind: str, prose: str, name: str, status: str, veto_count: int, ask: Any = None) -> str:
    """One plain-language line, derived from the report rather than the title: what the owner has to do and what
    happens if they do nothing. Heuristic, no model call."""
    first = asks_mod.STATUS_PREFIX.sub("", asks_mod.first_sentence(prose)).rstrip(".;")
    if asks_mod.RULING_MARK.match(first) or re.match(r"(?:vetoes?|to veto)\b", first, re.I):
        # "Vetoes: (a) …" is the list, not the outcome; the next non-veto sentence says what happened.
        rest = [p for p in asks_mod.sentences_of(prose)[1:]
                if len(p) >= 25 and not asks_mod.RULING_MARK.match(p) and not asks_mod.INLINE_VETO_LIST.match(p)]
        if rest:
            first = asks_mod.STATUS_PREFIX.sub("", rest[0]).rstrip(".;")
    inline = asks_mod.INLINE_VETO_LIST.search(first)
    if inline and inline.start() >= 8:
        # "Phase 2 done; vetoes first: (1) …" keeps only the outcome before the list.
        first = first[: inline.start()].rstrip(" ,;:")
    if len(first) > 160:
        # A long first sentence is cut at its first clause boundary, not mid-parenthesis.
        for boundary in (r"\s+\(", r"[;]\s", r"[:]\s"):
            clause = re.split(boundary, first, maxsplit=1)[0].strip()
            if 40 <= len(clause) < len(first):
                first = clause
                break
    first = clip(first, 160)
    if first.count("(") > first.count(")"):
        # The clip landed inside a parenthesis (often where a note path was removed): drop the open clause.
        first = first[: first.rfind("(")].rstrip(" ,;:…")
        first = STUB_TAIL.sub("", first).rstrip(" ,;:") or first
    if len(first) < 25:
        # "Close-out marked." says nothing; the next sentence usually does.
        rest = [p for p in asks_mod.sentences_of(prose)[1:] if len(p) >= 25]
        if rest:
            first = clip(asks_mod.STATUS_PREFIX.sub("", rest[0]).rstrip(".;"), 160)
    first = first.rstrip(" .…;,")
    if kind == "stalled":
        return STALLED_MEANING
    if kind == "blocked":
        prose = asks_mod.without_options_segment(prose, asks_mod.options_segment(prose))
        blocker = asks_mod.blocker_of(prose)
        if blocker:
            what = clip(blocker, 120)
            what = "the worker needs " + (what if what.endswith("…") else what + ".")
        else:
            # No "blocked on …" clause: the report's opening sentence names the blocker itself
            # ("Native refresh needs Sample Shop staging sign-in: …").
            what = clip(first, 140)
            what = what if what.endswith("…") else what + "."
        ways = ways_forward(ask)
        if ways:
            return f"{name} stopped: {what} {ways} Pick one, or drop the task; nothing moves until then."
        return f"{name} stopped: {what} Say how to get it, or drop the task; nothing moves until then."
    if kind == "failed":
        return f"{name} failed: {first}. Retry sends the same task again; Drop closes it."
    if kind == "go":
        vetoes = asks_mod.numbered_veto_count(prose)
        if vetoes:
            return (f"The work landed; the worker made {vetoes} call{'s' if vetoes != 1 else ''} on your behalf and lists them as vetoes. "
                    "Go accepts them as they stand; Hold names the one to undo.")
        return f"The work is ready and waits on your go: {first}. Hold keeps it unposted."
    if kind == "draft":
        who = getattr(ask, "recipient", "") or "the thread"
        labels = [str(getattr(o, "label", "") or (o.get("label") if isinstance(o, dict) else "")) for o in (getattr(ask, "options", None) or [])]
        if any(l.lower().startswith("file") for l in labels) or who.lower() in {"linear", "jira", "tracker", "github"}:
            # A filing ask: the record is written, not created.
            count = asks_mod.numbered_veto_count(prose) or len(re.findall(r"\bTitle\s*:", prose, re.I))
            noun = f"{count} {who} issue drafts are" if count and count > 1 else f"A {who} issue draft is"
            return f"{noun} written but not filed. File it creates the record; Do not file drops it."
        return f"A reply to {who} is written but not sent. Send as is posts it in your name; Do not send drops it."
    if kind == "post":
        return f"The artifact is ready for you to post yourself: {first}."
    if kind == "console":
        return "Replies wait in the console for your approval; nothing posts until you approve them there."
    if kind == "ruling":
        question = clip(getattr(ask, "summary", "") or getattr(ask, "title", "") or first, 140).rstrip(".")
        ways = ways_forward(ask)
        if ways:
            return f"The worker finished but could not decide this without you: {question}. {ways} Your answer goes back to the worker's thread."
        return f"The worker finished but could not decide this without you: {question}. Your answer goes back to the worker's thread."
    if kind == "followup":
        # The follow-up names the open work in full: a 140-character clip cut it mid-parenthesis.
        what = clip(getattr(ask, "summary", "") or asks_mod.followup_text(prose), 400).rstrip(".")
        return (f"Finished, but the worker left work open: {what}. Dispatch sends it as a continuation of the task; "
                "Drop closes it with nothing further.")
    if kind == "vetoes":
        numbered = asks_mod.numbered_veto_count(prose)
        if numbered:
            return f"Finished: {first}. The worker lists {numbered} choice{'s' if numbered != 1 else ''} you can veto; No vetoes closes it."
        # An unnumbered "Vetoes: a, b, c" list has no reliable count.
        return f"Finished: {first}. The worker lists choices you can veto; No vetoes closes it."
    return f"Finished, nothing to decide: {first}." if status == "done" else first


def note_section(root: Path, slug: str, ask_text: str, report: str) -> str:
    """When the ask matches a heading in the task's review note, that section's body (≤3000 chars), so the
    pane carries the evidence the worker wrote up. The note is the review link the report names; failing
    that, the newest Markdown note in the workstream folder. Read-only, cached per (note, ask)."""
    ask_key = re.sub(r"[^a-z0-9 ]", "", (ask_text or "").lower())
    words = [_stem(w) for w in ask_key.split()
             if len(w) > 3 and w not in {"which", "whether", "should", "with", "that", "this", "from", "have", "keep", "veto"}]
    if len(words) < 2:
        return ""
    for note in report_notes(root, slug, report):
        key = (str(note), ask_key)
        if key not in _NOTE_SECTION_CACHE:
            _NOTE_SECTION_CACHE[key] = _section_matching(note, words)
        if _NOTE_SECTION_CACHE[key]:
            return _NOTE_SECTION_CACHE[key]
    return ""


def _note_rel(note: Path, root: Path) -> str:
    """The note's path relative to root, or "" when it lies elsewhere (a worktree, say)."""
    try:
        return note.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def report_notes(root: Path, slug: str, report: str) -> list[Path]:
    """The review notes a report names (obsidian ``file=`` links and ``workstreams/…/x.md`` paths, in order,
    no duplicates); failing that, the newest three Markdown notes in the workstream folder. A private
    workstream's notes are never read here, so their text stays out of the feed; its card keeps the link."""
    private = private_workstreams.slugs(root)
    if slug in private:
        return []
    notes: list[Path] = []
    for match in re.finditer(r"obsidian://open\?[^\s)>\]]+", report or ""):
        candidate = note_path(match.group(0), root)
        if candidate and candidate.is_file() and candidate not in notes and not private_workstreams.is_private_path(_note_rel(candidate, root), private):
            notes.append(candidate)
    for match in re.finditer(r"workstreams/([\w-]+/[\w./-]+?\.md)", report or ""):
        candidate = root / "workstreams" / match.group(1)
        if candidate.is_file() and candidate not in notes and not private_workstreams.is_private_path(f"workstreams/{match.group(1)}", private):
            notes.append(candidate)
    folder = root / "workstreams" / slug
    if not notes and folder.is_dir():
        notes = sorted((p for p in folder.glob("*.md") if p.name != "CONTEXT.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    return notes


# The note sections the fallback reads when a blocked or needs-ruling report carries no Options segment:
# the ways forward live in the review note's alternatives table and
# the lean in the bold sentence of its Recommendation section.
RECOMMENDATION_HEADING = re.compile(r"^#{2,4}\s+Recommendation\b", re.I)
ALTERNATIVES_HEADING = re.compile(r"^#{2,4}\s+.*\b(?:Alternative|Option)", re.I)
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")
_NOTE_OPTIONS_CACHE: dict[str, Any] = {}


def _section_lines(lines: list[str], heading: re.Pattern[str]) -> list[str]:
    """The body lines of the first section whose heading matches, up to the next heading of the same or a
    higher level; [] when no heading matches."""
    for index, line in enumerate(lines):
        if not heading.match(line):
            continue
        level = len(line) - len(line.lstrip("#"))
        end = len(lines)
        for later, later_line in enumerate(lines[index + 1:], start=index + 1):
            if re.match(r"^#{1,6}\s+\S", later_line) and len(later_line) - len(later_line.lstrip("#")) <= level:
                end = later
                break
        return lines[index + 1:end]
    return []


def _plain_cell(cell: str) -> str:
    cell = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell)       # [text](url) → text
    cell = re.sub(r"[*_`]+", "", cell)
    return re.sub(r"\s+", " ", cell).strip(" .")


def _first_table(section: list[str]) -> list[list[str]]:
    """Rows (cells) of the first pipe table in the section, header excluded; [] when there is none."""
    rows: list[list[str]] = []
    seen_table = False
    for line in section:
        match = TABLE_ROW.match(line)
        if not match:
            if seen_table:
                break
            continue
        seen_table = True
        if TABLE_RULE.match(line):
            continue
        rows.append([cell.strip() for cell in match.group(1).split("|")])
    return rows[1:] if len(rows) > 1 else []


def _note_options_uncached(note: Path) -> Any:
    try:
        lines = note.read_text(errors="replace").split("\n")
    except OSError:
        return None
    table = _first_table(_section_lines(lines, ALTERNATIVES_HEADING))
    labels = [_plain_cell(row[0]) for row in table if row and _plain_cell(row[0])]
    if len(labels) < 2:
        return None
    rows = [[_plain_cell(c) for c in row] for row in table if row and _plain_cell(row[0])][:asks_mod.MAX_OPTIONS]
    labels = labels[:asks_mod.MAX_OPTIONS]
    letters = "abcd"
    options = [(letters[i], label) for i, label in enumerate(labels)]
    raw_rows = [row for row in table if row and _plain_cell(row[0])][:asks_mod.MAX_OPTIONS]
    return asks_mod.OptionsSegment(options, _note_lean(lines, rows, raw_rows, letters))


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.sub(r"[^a-z0-9 ]", "", text.lower()).split() if len(w) > 3}


def _note_lean(lines: list[str], rows: list[list[str]], raw_rows: list[list[str]], letters: str) -> Any:
    """Which table row the note recommends. The bold sentence of ``## Recommendation`` is the reason when it
    names one row's label (two or more shared words; one shared domain noun such as "Button" is not naming
    it, since a lean that shares only "Button" can match the row the note itself rejects). Otherwise the one row whose cells carry bold (the author's emphasis, "**Least
    setup now.**") is the lean with that bold text as the reason. None when neither says which row."""
    recommendation = "\n".join(_section_lines(lines, RECOMMENDATION_HEADING))
    bold = re.search(r"\*\*(.+?)\*\*", recommendation, re.S)
    if bold:
        sentence = re.sub(r"\s+", " ", bold.group(1)).strip()
        words = _words(sentence)
        scores = [len(words & _words(row[0])) for row in rows]
        top = max(scores, default=0)
        if top >= 2 and scores.count(top) == 1:
            return asks_mod.Lean(letters[scores.index(top)], sentence.rstrip(". "))
    emphasised = [(index, re.search(r"\*\*(.+?)\*\*", " ".join(row[1:]), re.S))
                  for index, row in enumerate(raw_rows)]
    emphasised = [(index, match) for index, match in emphasised if match]
    if len(emphasised) == 1:
        index, match = emphasised[0]
        return asks_mod.Lean(letters[index], re.sub(r"\s+", " ", match.group(1)).strip().rstrip(". "))
    return None


def note_options(root: Path, slug: str, report: str) -> Any:
    """An OptionsSegment read from the task's review note: the first column of the first table under a heading
    matching /Alternative|Option/ (≤3 rows → (a)(b)(c)) and the bold sentence of ``## Recommendation`` as the
    lean, matched to the row it names. None when no note has both a table and two or more rows; an
    unreadable note is the same as no note (fail soft, the status default stays)."""
    for note in report_notes(root, slug, report):
        key = str(note)
        if key not in _NOTE_OPTIONS_CACHE:
            _NOTE_OPTIONS_CACHE[key] = _note_options_uncached(note)
        if _NOTE_OPTIONS_CACHE[key]:
            return _NOTE_OPTIONS_CACHE[key]
    return None


def _stem(word: str) -> str:
    return re.sub(r"(?:able|ing|ed|es|s)$", "", word) if len(word) > 4 else word


def _section_matching(note: Path, words: list[str]) -> str:
    try:
        lines = note.read_text(errors="replace").split("\n")
    except OSError:
        return ""
    # H1 is the note title, so a match there would paste the whole note; sections start at H2.
    headings = [(i, line) for i, line in enumerate(lines) if re.match(r"^#{2,4}\s+\S", line)]
    best: tuple[float, int] | None = None
    for position, (index, line) in enumerate(headings):
        heading = {_stem(w) for w in re.sub(r"[^a-z0-9 ]", "", line.lstrip("# ").lower()).split()}
        hits = sum(1 for w in words if w in heading)
        score = hits / len(words)
        if hits >= 2 and score >= 0.6 and (best is None or score > best[0]):
            best = (score, position)
    if best is None:
        return ""
    index, line = headings[best[1]]
    level = len(line) - len(line.lstrip("#"))
    end = len(lines)
    for later_index, later_line in headings[best[1] + 1:]:
        if len(later_line) - len(later_line.lstrip("#")) <= level:
            end = later_index
            break
    body = "\n".join(lines[index:end]).strip()
    if len(body) > NOTE_SECTION_CHARS:
        body = body[:NOTE_SECTION_CHARS].rsplit("\n", 1)[0].rstrip() + "\n…"
    return body


def stalled_title(ask: str, name: str) -> str:
    """A stalled row names its workstream before the ask it stopped on ("Bake Sale Flyer: loop and
    put together a web page…"); the bare prompt tail alone did not say what the task was."""
    ask = (ask or "").strip()
    if not ask:
        return f"{name} worker stalled"
    if name and ask.lower().startswith(name.lower()):
        return ask
    return f"{name}: {ask}" if name else ask


STALLED_MEANING = ("The worker stopped reporting and its tab is gone or idle; the task did not finish. "
                   "Re-dispatch starts it again from the ledger prompt; Drop it closes the row.")


def stuck_item(row: dict[str, Any], root: Path, now: datetime) -> dict[str, Any]:
    """A row the dispatch sweep marked blocked because it sat in routing/queued past
    ``dispatch.stuck_routing_minutes`` with no live thread (``stuck: ...`` reason). It reads
    like the live Stuck rows running_items builds: same label, same Re-dispatch / Drop
    options, and the sweep's own reason (age, tab, re-dispatch command) as the summary."""
    task_id = str(row.get("id") or "")
    slug = str(row.get("workstream") or "")
    name = workstream_name(root, slug)
    reason = normalize(row.get("summary") or "")
    moment = row_time(row) or now
    ask = running_title(str(row.get("prompt") or ""), name)
    return {
        "id": f"task:{task_id}",
        "section": "needs_you",
        "kind": "stalled",
        "stall": stuck_label(reason),
        "agent_state": "stalled",
        "title": stalled_title(ask, name),
        "summary": f"{stuck_label(reason)} ({name}): {clip(reason, 300)}",
        "meaning": STALLED_MEANING,
        "prompt_excerpt": prompt_excerpt(str(row.get("prompt") or "")),
        "options": [asks_mod.opt("Re-dispatch", f"{task_id}: re-dispatch"), asks_mod.opt("Drop it", f"{task_id}: drop it")],
        "links": [],
        "reply_text": f"{task_id}: re-dispatch",
        "reply_surface": "CoS",
        "draft_text": "",
        "workstream": slug,
        "task_id": task_id,
        "thread_id": "",
        "thread_url": "",
        "workspace_ref": str(row.get("workspace_ref") or ""),
        "status": "blocked",
        "updated_at": iso(moment),
        "updated_label": pt_label(moment, now),
        "day_label": day_label(moment, now),
        "source": "task",
        "decisions": [],
    }


def task_item(row: dict[str, Any], root: Path, now: datetime, extra_rows: int = 0) -> dict[str, Any]:
    """The primary row only (tests and callers that want one item per ledger row)."""
    return task_items(row, root, now, extra_rows)[0]


# Per-source wording for a triage draft, so a Doc comment draft never says "Open Slack" or
# "posts the draft to Slack". ``send-triage-draft.py`` posts only to Slack permalinks, so
# Do now is off for the other sources and the caption says to use Send to Triage instead.
SOURCE_WORDING: dict[str, dict[str, Any]] = {
    "slack": {"open_label": "Open Slack", "do_now_enabled": True,
              "do_now_caption": "Do now posts the draft to Slack as you."},
    "google-docs": {"open_label": "Open Doc", "do_now_enabled": False,
                    "do_now_caption": "Do now is off for Doc comments: send-triage-draft.py posts only to Slack. "
                                      "Use Send to Triage (send N) and Comms Triage posts the comment on the Doc."},
    "gmail": {"open_label": "Open Gmail", "do_now_enabled": False,
              "do_now_caption": "Do now is off for email: send-triage-draft.py posts only to Slack. "
                                "Use Send to Triage (send N) and Comms Triage sends the reply."},
    "github": {"open_label": "Open GitHub", "do_now_enabled": False,
               "do_now_caption": "Do now is off for GitHub: send-triage-draft.py posts only to Slack. "
                                 "Use Send to Triage (send N) and Comms Triage posts the comment."},
}
ME_NAMES = {"owner example", "owner", "{{OWNER_USER}}", "{{OWNER_USER}} example", "{{OWNER_USER}}"}
CHAIN_LINE = re.compile(r"^(?P<author>[A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,3}):\s(?P<text>.+)$")


def source_kind_of(draft: dict[str, Any]) -> str:
    """slack | google-docs | gmail | github from the ledger ``source`` field, else the source link."""
    raw = str(draft.get("source") or (draft.get("inbound") or {}).get("source_type")
              or (draft.get("inbound_context") or {}).get("surface") or "").strip().lower()
    url = str(draft.get("draft_source_link") or draft.get("ref") or "")
    if raw in {"google-docs", "google docs", "gdoc", "gdocs", "google-doc"} or "docs.google.com" in url:
        return "google-docs"
    if raw in {"gmail", "email", "mail"} or "mail.google.com" in url:
        return "gmail"
    if raw == "github" or "github.com" in url:
        return "github"
    return "slack"


def is_me(author: str) -> bool:
    return author.strip().lower() in ME_NAMES


def chain_message(author: str, text: str, at: Any, now: datetime, me: bool | None = None) -> dict[str, Any]:
    moment = parse_iso(at)
    return {
        "author": author.strip() or "someone",
        "me": is_me(author) if me is None else bool(me),
        "at": iso(moment) if moment else "",
        "at_label": pt_label(moment, now) if moment else "",
        "text": text.strip(),
    }


def parse_chain_lines(body: str, now: datetime) -> list[dict[str, Any]]:
    """``Author: text`` lines (the shape the comms collector stores in ``inbound.body`` for Doc comment
    threads) into messages; a line with no author prefix continues the previous message."""
    out: list[dict[str, Any]] = []
    for line in normalize(body).split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        match = CHAIN_LINE.match(line.strip())
        if match:
            out.append(chain_message(match.group("author"), match.group("text"), None, now))
        elif out:
            out[-1]["text"] = (out[-1]["text"] + "\n" + line.strip()).strip()
    return out


def chain_messages(draft: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """The whole source conversation, oldest first: ``inbound_context.messages`` when the collector or
    ``triage_chain.py`` stored it, else the ``Author: text`` lines of ``inbound.body``, else the one inbound
    message the ledger holds. Never elided; the list row keeps the summary."""
    context = draft.get("inbound_context") or {}
    inbound = draft.get("inbound") or {}
    stored = context.get("messages")
    if isinstance(stored, list) and stored:
        out = []
        for msg in stored:
            if not isinstance(msg, dict) or not str(msg.get("text") or "").strip():
                continue
            out.append(chain_message(str(msg.get("author") or ""), str(msg.get("text") or ""),
                                     msg.get("at"), now, msg.get("me")))
        if out:
            return out
    for body in (inbound.get("body"), context.get("text")):
        parsed = parse_chain_lines(str(body or ""), now)
        if len(parsed) >= 2 or (parsed and parsed[0]["text"] != str(body or "").strip()):
            received = inbound.get("received_at")
            if received and parsed and not parsed[-1]["at"]:
                # The ledger stamps the latest message only; earlier messages stay undated.
                parsed[-1].update(at=iso(parse_iso(received)) if parse_iso(received) else "",
                                  at_label=pt_label(parse_iso(received), now) if parse_iso(received) else "")
            return parsed
    text = str(context.get("text") or inbound.get("body") or inbound.get("summary") or "").strip()
    if not text:
        return []
    author = str(context.get("author") or inbound.get("from") or "someone")
    return [chain_message(author, text, inbound.get("received_at") or context.get("captured_at"), now)]


TRIAGE_WHERE = {"slack": "in Slack", "google-docs": "on the Doc", "gmail": "by email", "github": "on GitHub"}


def triage_meaning(author: str, source_kind: str, wording: dict[str, Any]) -> str:
    where = TRIAGE_WHERE.get(source_kind, "in Slack")
    if wording.get("do_now_enabled"):
        return f"A reply to {author} is drafted. Send as is posts it {where} as you; Dismiss drops it."
    return f"A reply to {author} is drafted. Send to Triage (send N) posts it {where} as you; Dismiss drops it."


_CHANNEL_NAMES: dict[str, dict[str, str]] = {}


def channel_names(root: Path) -> dict[str, str]:
    """Slack channel id -> name from ``system/config/priority-channels.yaml`` (read once per root)."""
    key = str(root)
    if key not in _CHANNEL_NAMES:
        found: dict[str, str] = {}
        path = root / "system" / "config" / "priority-channels.yaml"
        try:
            text = path.read_text(errors="replace")
        except OSError:
            text = ""
        for match in re.finditer(r"^\s*-\s*id:\s*(?P<id>[CG][A-Z0-9]{6,})\s*\n(?P<body>(?:(?!^\s*-\s*id:)[^\n]*\n?)*)", text, re.M):
            name = re.search(r"^\s*name:\s*[\"']?(?P<name>[\w-]+)", match.group("body"), re.M)
            if name:
                found[match.group("id")] = name.group("name")
        _CHANNEL_NAMES[key] = found
    return _CHANNEL_NAMES[key]


SLACK_REF_CHANNEL = re.compile(r"/archives/(?P<id>[CDG][A-Z0-9]{6,})(?:/|$)")
_NAME = r"[A-Z][\w'.-]+(?: [A-Z][\w'.-]+)*"
MPDM_TITLE = re.compile(rf"\bin MPDM\s+(?P<names>{_NAME}(?:,\s*{_NAME})*)")
TITLE_AUTHOR = re.compile(rf"^(?P<name>[A-Z][\w'.-]+(?: [A-Z][\w'.-]+){{1,3}})(?=\s+(?:via|＠|@|\(|in\b))")
DOC_TITLE_TAIL = re.compile(r",\s*(?:email\s+)?\d{1,2}:\d{2}(?:\s*[AP]M)?(?:\s*P[DS]T)?\s*$")


def triage_destination(draft: dict[str, Any], author: str, source_kind: str, source_title: str,
                        channels: dict[str, str] | None = None) -> str:
    """Where ``send N`` posts the draft, as the "To:" line reads. ``send-triage-draft.py``
    always replies in the source thread, so a Slack draft is "#channel, thread reply" (channel from the
    source title, else from the ref's channel id), a DM is "<author> (DM)", a group DM names its members;
    a Doc comment, Gmail or GitHub draft names the author and the surface."""
    title = str(source_title or "")
    meta = draft.get("draft_meta") if isinstance(draft.get("draft_meta"), dict) else {}
    if str(draft.get("ref") or "").startswith("slack://dm/") or meta.get("parked_by_task"):
        # A worker-parked DM (parked_draft_tracker.py): the recipient is in the meta, the ref is slack://dm/<UID>.
        name = str(meta.get("recipient_name") or meta.get("draft_to") or author).strip()
        return name if name.endswith("(DM)") or name.startswith("#") else f"{name} (DM)"
    if source_kind == "google-docs":
        doc = re.search(r"Google Docs:\s*(?P<title>.+)$", title)
        name = DOC_TITLE_TAIL.sub("", doc.group("title")).strip(" ,") if doc else ""
        return f"{author}, Doc comment reply" + (f" in {name}" if name else "")
    if source_kind == "gmail":
        return f"{author}, email reply"
    if source_kind == "github":
        return f"{author}, PR comment"
    channel = re.search(r"(?<![\w])#(?P<name>[\w-]+)", title)
    if channel:
        return f"#{channel.group('name')}, thread reply"
    if re.search(r"\bin (?:a )?DM\b", title):
        return f"{author} (DM)"
    mpdm = MPDM_TITLE.search(title)
    if mpdm:
        return f"Group DM ({mpdm.group('names').strip().rstrip(',')}), thread reply"
    ref = str(draft.get("draft_source_link") or draft.get("ref") or "")
    ref_channel = SLACK_REF_CHANNEL.search(ref)
    if ref_channel:
        cid = ref_channel.group("id")
        if cid.startswith("D"):
            return f"{author} (DM)"
        name = (channels or {}).get(cid)
        if name:
            return f"#{name}, thread reply"
        if cid.startswith("G"):
            return f"Group DM with {author}, thread reply"
    return f"{author}'s Slack thread, thread reply"


def triage_item(draft: dict[str, Any], now: datetime, channels: dict[str, str] | None = None) -> dict[str, Any]:
    number = int(draft.get("draft_number") or 0)
    held = parse_iso(draft.get("draft_held_at")) or parse_iso(draft.get("draft_updated_at")) or now
    inbound = draft.get("inbound_context") or {}
    ask = re.sub(r"\s+", " ", str(inbound.get("review_text") or inbound.get("text") or "")).strip()
    source_title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(draft.get("draft_source_title") or ""))
    title_author = TITLE_AUTHOR.match(source_title)
    author = (inbound.get("author") or (draft.get("inbound") or {}).get("from")
              or (title_author.group("name") if title_author else "") or "someone")
    where = re.search(r"in (#[\w-]+|a DM|DM)", source_title)
    text = str(draft.get("draft_text") or "")
    url = str(draft.get("draft_source_link") or draft.get("ref") or "")
    links = [single_link(url, channels)] if url else []
    source_kind = source_kind_of(draft)
    wording = SOURCE_WORDING.get(source_kind, SOURCE_WORDING["slack"])
    meta = draft.get("draft_meta") if isinstance(draft.get("draft_meta"), dict) else {}
    parked_by = str(meta.get("parked_by_task") or "")
    if parked_by and author == "someone":
        author = str(meta.get("recipient_name") or meta.get("draft_to") or "").replace(" (DM)", "").strip() or author
    parked_thread = str(meta.get("thread_id") or "") if parked_by else ""
    item = {
        "id": f"draft:{number}",
        "section": "needs_you",
        "kind": "triage",
        "title": (f"Send the parked DM to {author}" if parked_by else f"Reply to {author}") + (f" {where.group(0)}" if where else ""),
        "summary": clip(ask, 240) if ask else clip(source_title, 240),
        "options": [asks_mod.opt("Send as is", f"send {number}"), asks_mod.opt("Edit, then send", f"edit {number} <your changes>"),
                    asks_mod.opt("Dismiss", f"dismiss {number}")],
        "links": links,
        "reply_text": f"send {number}",
        "reply_surface": "Comms Triage",
        "draft_text": text,
        "workstream": "comms-triage",
        "draft_id": f"draft-{number}",
        # A parked DM keeps its worker's task, thread and tab so "Show in cmux" and a steer still work.
        "task_id": parked_by,
        "thread_id": parked_thread,
        "thread_url": (str(meta.get("thread_url") or "") or (f"https://ampcode.com/threads/{parked_thread}" if parked_thread.startswith("T-") else "")) if parked_by else "",
        "workspace_ref": str(meta.get("workspace_ref") or "") if parked_by else "",
        "parked_by": parked_by,
        "note": str(meta.get("note") or "") if parked_by else "",
        "no_robot": bool(draft.get("draft_no_prefix")),
        "snoozed_until": str(meta.get("snooze_until") or ""),
        "status": str(draft.get("draft_status") or "held"),
        "ref": url,
        "updated_at": iso(held),
        "updated_label": pt_label(held, now),
        "day_label": day_label(held, now),
        "source": "triage",
        "source_kind": source_kind,
        "source_title": source_title,
        "draft_to": triage_destination(draft, author, source_kind, source_title, channels),
        "draft_to_inferred": False,
        "messages": chain_messages(draft, now),
        "meaning": triage_meaning(author, source_kind, wording),
        **wording,
    }
    status = str(draft.get("draft_status") or "")
    if status == "dismissed" and str(meta.get("reason") or "").startswith("cos-review"):
        # Completed in CoS Review (Complete drops the draft); the ledger row is the record.
        dismissed = parse_iso(meta.get("at")) or parse_iso(draft.get("draft_updated_at")) or held
        item.update(section="past", acted="complete", acted_at=iso(dismissed), acted_by=str(draft.get("draft_text_by") or "owner"),
                    updated_at=iso(dismissed), updated_label=pt_label(dismissed, now), day_label=day_label(dismissed, now))
    elif status in {"sent", "dismissed"}:
        # Sent from Comms Triage (``send N``), marked sent by a worker, or dismissed there: the ledger's
        # own decision settles the card (settle_from_ledger moves it to Past with this caption).
        when = parse_iso(meta.get("at")) or parse_iso(meta.get("sent_at")) or parse_iso(draft.get("draft_updated_at")) or held
        reason = str(meta.get("sent_permalink") or meta.get("reason") or "")
        if meta.get("classification") and meta.get("classification_reason"):
            # Auto-dismissed by the already-answered pre-check: say why, not just the label.
            reason = f"{str(meta['classification']).replace('_', ' ')}: {meta['classification_reason']}"
        item.update(acted_at=iso(when), acted_by=str(meta.get("by") or draft.get("draft_text_by") or "owner"), acted_reason=reason,
                    updated_at=iso(when), updated_label=pt_label(when, now), day_label=day_label(when, now))
    return item


# --------------------------------------------------------------------------- running

PROMPT_PREFIX = re.compile(r"^(?:Workstream:\s*\S+\s*|Steer on rt-[0-9a-f]+\s*\([^)]*\)[^\n:]*:\s*)+", re.I)
QUOTED_ASK = re.compile(r"[\"“]([^\"”]{12,}?)[\"”]")
STEER_LEAD = re.compile(r"^(?:the owner|Steer(?: from the owner)?)\b[^:\"“]{0,60}[:,]\s*", re.I)


def running_title(prompt: str, workstream: str) -> str:
    """A short line naming what the worker is doing: the owner's quoted ask when the prompt
    carries one, else the first sentence after the routing preamble."""
    text = normalize(prompt or "").strip()
    text = PROMPT_PREFIX.sub("", text).strip()
    quoted = QUOTED_ASK.search(text)
    if quoted:
        text = quoted.group(1)
    else:
        text = STEER_LEAD.sub("", text)
        first = sentences(prose_of(text))
        text = first[0] if first else text.split("\n", 1)[0]
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"^\(?\d{1,2}[).]?\s+(?=[a-z])", "", text)  # a quoted ask that opens with an option number ("1 loop and …")
    return clip(text, 140) if text else ""


def agent_state_of(raw: str) -> tuple[str, str]:
    """(state, status_line) from the cmux lifecycle stamp: running | idle | error."""
    raw = (raw or "").strip()
    low = raw.lower()
    if not raw:
        return "unknown", ""
    if low.startswith("idle"):
        return "idle", ""
    if low.startswith("error"):
        return "error", raw
    if low.startswith("running"):
        return "running", ""
    return "running", raw  # a tool line such as "shell_command: python3 …"


def live_agent_map() -> dict[str, dict[str, Any]]:
    """thread_id -> cmux lifecycle stamp, via running-task-state.live_agents (empty when unavailable)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("running_task_state", SCRIPT_DIR / "running-task-state.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module.live_agents()
    except Exception:
        return {}


LAUNCH_GRACE_MIN = 10   # a routing/queued row with no thread yet is "launching" this long (dispatch.live_workers)
HEARTBEAT_STALE_MIN = 15  # an active row whose thread is not in cmux and whose heartbeat is older than this is stalled
STAMP_STALE_MIN = 120   # a cmux "Running" stamp older than this, with no heartbeat since, is a leftover, not a live worker


TERMINAL_STATUSES = {"done", "failed", "blocked", "deferred", "cancelled"}


def stall_of(row: dict[str, Any], stamp: dict[str, Any], now: datetime,
             agents: dict[str, dict[str, Any]] | None = None,
             rows_by_id: dict[str, dict[str, Any]] | None = None) -> tuple[str, str]:
    """("", "") for a live worker, else (short state, one-line fix) for a row that only
    looks like it is running.
    Live means: its thread is live in cmux, or it heartbeat within HEARTBEAT_STALE_MIN, or it
    is still inside the launch grace window with no thread yet (``launching_at`` counts as the
    dispatcher's claim). Judged only when the cmux lifecycle store is readable: an empty agent
    map is a store outage, not evidence (dispatch.py fail_abandoned_rows applies the same guard).
    A queued row waiting on a non-terminal ``depends_on`` row is waiting, not stalled.
    A row the sweep already judged wedged (dispatch.py flag_wedged_workers: heartbeat, cmux
    stamp and Amp thread all silent past ``dispatch.wedged_hours``) is shown as Wedged with the
    sweep's own line, whatever the thresholds here say; a heartbeat clears that mark."""
    wedged = row.get("wedged") if isinstance(row.get("wedged"), dict) else None
    if wedged and str(row.get("status") or "") == "active":
        fix = str(row.get("recovery_action") or wedged.get("reason") or "").strip()
        return "Wedged", fix or "worker shows no progress on any signal; check the tab, re-dispatch it, or drop it"
    thread_id = str(row.get("thread_id") or "")
    if not stamp and thread_id and agents:
        stamp = agents.get(thread_id) or {}
    beat = parse_iso(row.get("heartbeat_at")) or parse_iso(row.get("started_at")) or parse_iso(row.get("updated_at"))
    beat_min = (now - beat).total_seconds() / 60 if beat else None
    if stamp and not stamp_is_stale(stamp, beat_min, now):
        return "", ""
    if not stamp and agents is not None and not agents:
        return "", ""
    status = str(row.get("status") or "")
    created = parse_iso(row.get("created_at")) or parse_iso(row.get("started_at")) or row_time(row) or now
    claim = parse_iso(row.get("launching_at"))
    if claim and claim > created:
        created = claim
    beat = beat or created
    age_min = (now - created).total_seconds() / 60
    beat_min = (now - beat).total_seconds() / 60
    if not thread_id and age_min <= LAUNCH_GRACE_MIN:
        return "", ""
    if thread_id and beat_min <= HEARTBEAT_STALE_MIN:
        return "", ""
    if status == "queued":
        deps = row.get("depends_on") or []
        deps = deps if isinstance(deps, list) else [deps]
        for dep in deps:
            dep_row = (rows_by_id or {}).get(str(dep)) or {}
            if dep_row and str(dep_row.get("status") or "") not in TERMINAL_STATUSES:
                return "", ""
        return "Stuck: queued with no worker", stuck_fix(str(row.get("id") or ""))
    if not thread_id:
        return "Stuck: never launched", stuck_fix(str(row.get("id") or ""))
    recovery = str(row.get("recovery_action") or "").strip()
    if recovery:
        return "Stalled", recovery
    return "Stalled", f"no heartbeat for {elapsed_label(beat, now)}; re-dispatch it, or drop it"


def stamp_is_stale(stamp: dict[str, Any], beat_min: float | None, now: datetime) -> bool:
    """A cmux lifecycle stamp proves a live worker only while it is recent: an old "Running" stamp with
    no heartbeat since must not keep a row in Running after the sweep wrote ``stale_since``. A stamp with no ``state_ts``
    (older store shape, test fixtures) is trusted as before."""
    raw_ts = stamp.get("state_ts")
    if not raw_ts:
        return False
    try:
        stamped = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False
    stamp_min = (now - stamped).total_seconds() / 60
    if stamp_min <= STAMP_STALE_MIN:
        return False
    return beat_min is None or beat_min > HEARTBEAT_STALE_MIN


STUCK_REASON = re.compile(r"^\s*stuck:\s*(routing|queued)\b", re.I)


def stuck_fix(task_id: str) -> str:
    """The same re-dispatch command dispatch.py flag_stuck_routes puts in its origin line,
    so the feed and the CoS line read the same thing."""
    return f"re-dispatch with router-dispatch.sh --task-id {task_id or '<rt-id>'} --force-redispatch, or drop it"


def stuck_label(reason: str) -> str:
    """Feed label for a row the sweep marked blocked with a ``stuck: <status> ...`` reason."""
    match = STUCK_REASON.match(reason)
    return "Stuck: queued with no worker" if match and match.group(1).lower() == "queued" else "Stuck: never launched"


def running_items(live: list[dict[str, Any]], root: Path, now: datetime, agents: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Rows for tasks the ledger says are routing, queued or active. A row with a live
    worker is a read-only Running row titled by its ask text. A row that only looks live
    (never launched, queued with no worker, or no heartbeat) is a Needs-you "stalled" row
    with Re-dispatch / Drop as its options, so the Running count matches live workers."""
    agents = live_agent_map() if agents is None else agents
    rows_by_id = {str(r.get("id") or ""): r for r in live if isinstance(r, dict)}
    items: list[dict[str, Any]] = []
    for row in live:
        task_id = str(row.get("id") or "")
        slug = str(row.get("workstream") or "")
        name = workstream_name(root, slug)
        thread_id = str(row.get("thread_id") or "")
        stamp = agents.get(thread_id) or {}
        state, status_line = agent_state_of(str(stamp.get("state") or ""))
        if row.get("status") != "active":
            state = str(row.get("status"))
        started = parse_iso(row.get("started_at")) or parse_iso(row.get("created_at")) or row_time(row)
        beat = parse_iso(row.get("heartbeat_at")) or started or now
        thread_url = f"https://ampcode.com/threads/{thread_id}" if thread_id.startswith("T-") else ""
        ask = running_title(str(row.get("prompt") or ""), name)
        stall, fix = stall_of(row, stamp, now, agents, rows_by_id)
        item = {
            "id": f"task:{task_id}",
            "section": "running",
            "kind": "running",
            "title": ask or f"{name} worker",
            "summary": (f"{name}. " if ask else "") + (status_line or f"{row.get('status')}, {elapsed_label(started, now)}"),
            "status_line": status_line,
            "options": [],
            "links": [single_link(thread_url)] if thread_url else [],
            "reply_text": "",
            "reply_surface": "worker",
            "draft_text": "",
            "workstream": slug,
            "task_id": task_id,
            "thread_id": thread_id,
            "thread_url": thread_url,
            "workspace_ref": str(row.get("workspace_ref") or ""),
            "status": str(row.get("status") or ""),
            "agent_state": state,
            "thread_live": bool(stamp),
            "started_at": iso(started) if started else "",
            "elapsed_label": elapsed_label(started, now),
            "updated_at": iso(beat),
            "updated_label": pt_label(beat, now),
            "day_label": day_label(beat, now),
            "source": "ledger",
            "decisions": [d for d in (row.get("decisions") or []) if isinstance(d, dict)],
            # The ask that launched the task, so a Running or Stalled pane has context too.
            "prompt_excerpt": prompt_excerpt(str(row.get("prompt") or "")),
        }
        if stall:
            item.update(
                section="needs_you", kind="stalled", stall=stall, agent_state="stalled",
                title=stalled_title(ask, name),
                summary=f"{stall} ({name}, {row.get('status')} for {elapsed_label(started, now)}): {fix}.",
                meaning=STALLED_MEANING,
                options=[asks_mod.opt("Re-dispatch", f"{task_id}: re-dispatch"), asks_mod.opt("Drop it", f"{task_id}: drop it")],
                reply_text=f"{task_id}: re-dispatch", reply_surface="CoS",
            )
        items.append(item)
    items.sort(key=lambda i: i["started_at"] or i["updated_at"], reverse=True)
    return items


# --------------------------------------------------------------------------- chat-only asks (cos-asks.jsonl)

def ask_items(root: Path, now: datetime, window_hours: int) -> list[dict[str, Any]]:
    """Rows from the CoS ask ledger (``cos_review_ask_ledger.py``): open asks under Needs you
    with their options as preset replies, resolved asks inside the window under Past."""
    try:
        import cos_review_ask_ledger as asks_ledger  # type: ignore
        asks = asks_ledger.load_asks(asks_ledger.asks_path(root))
    except Exception:
        return []
    cutoff = now - timedelta(hours=window_hours)
    items: list[dict[str, Any]] = []
    for ask in asks.values():
        asked = parse_iso(ask.get("at")) or now
        resolved = ask.get("resolved")
        moment = (parse_iso(resolved.get("at")) or asked) if resolved else asked
        if moment < cutoff:
            continue
        options = [asks_mod.opt(str(o.get("label")), str(o.get("reply") or o.get("label"))) for o in ask.get("options") or []]
        text = str(ask.get("text") or "")
        links = links_of(str(ask.get("ref") or ""), root, link_titles(root, now)) if ask.get("ref") else []
        thread = str(ask.get("thread") or "")
        if thread.startswith("T-"):
            links.append(single_link(f"https://ampcode.com/threads/{thread}"))
        item = {
            "id": f"ask:{ask['id']}",
            "section": "needs_you",
            "kind": "ask",
            "title": str(ask.get("title") or "") or clip(text, 90),
            "summary": text,
            "options": options,
            "links": links,
            "reply_text": options[0]["reply"] if options else "",
            "reply_surface": "CoS",
            "draft_text": "",
            "workstream": str(ask.get("source") or "cos"),
            "task_id": "",
            "thread_id": thread,
            "thread_url": f"https://ampcode.com/threads/{thread}" if thread.startswith("T-") else "",
            "workspace_ref": "",
            "status": "resolved" if resolved else "open",
            "ask_id": str(ask["id"]),
            "updated_at": iso(moment),
            "updated_label": pt_label(moment, now),
            "day_label": day_label(moment, now),
            "source": "cos-ask",
        }
        if resolved:
            item.update(section="past", acted="complete", acted_at=iso(moment),
                        acted_reply=str(resolved.get("reply") or ""), acted_by=str(resolved.get("by") or "owner"))
        items.append(item)
    return items


# --------------------------------------------------------------------------- drafts as markdown

MRKDWN_BOLD = re.compile(r"(?<![\w*])\*(?P<body>[^*\n]+?)\*(?![\w*])")
MRKDWN_STRIKE = re.compile(r"(?<![\w~])~(?P<body>[^~\n]+?)~(?![\w~])")
MRKDWN_BULLET = re.compile(r"^(\s*)[•◦▪]\s+", re.M)
MRKDWN_CHANNEL = re.compile(r"<#(?P<id>[A-Z0-9]+)\|(?P<name>[^>]+)>")
MRKDWN_USER = re.compile(r"<@(?P<id>[A-Z0-9]+)(?:\|(?P<name>[^>]+))?>")


def mention_label(match: "re.Match[str]", names: dict[str, Any] | None) -> str:
    """``@Real Name`` for a ``<@U…>`` mention: the inline ``|name`` first, then the shared
    user-name cache, and the raw id only when neither knows the user (read-only; no lookup)."""
    uid = match.group("id")
    if match.group("name"):
        return f"@{match.group('name')}"
    entry = (names or {}).get(uid)
    name = entry.get("name") if isinstance(entry, dict) else None
    return f"@{name}" if name else f"@{uid}"


def mrkdwn_to_markdown(text: str, names: dict[str, Any] | None = None) -> str:
    """Slack mrkdwn → plain Markdown so the app can render and edit one syntax.
    Already-Markdown text (``**bold**``, ``[label](url)``) passes through unchanged.
    ``names`` is accepted for callers that still pass it; mentions are no longer substituted."""
    if not text:
        return ""
    out = normalize(text)
    out = SLACK_LINK.sub(lambda m: f"[{m.group('label')}]({m.group('url')})", out)
    # ``<@U…>`` and ``<#C…|name>`` stay raw: the Edit tab and the posted text keep
    # the ids Slack resolves; the Preview resolves them through ``draft_mentions``.
    out = MRKDWN_BOLD.sub(lambda m: f"**{m.group('body')}**", out)
    out = MRKDWN_STRIKE.sub(lambda m: f"~~{m.group('body')}~~", out)
    out = MRKDWN_BULLET.sub(r"\1- ", out)
    return out.strip()


BARE_USER_ID = re.compile(r"(?<![\w<])@(?P<id>U[A-Z0-9]{8,})\b")


def draft_mentions(text: str, names: dict[str, Any] | None) -> dict[str, str]:
    """Raw mention token -> display label for the Preview tab: ``<@U08N…>`` -> ``@Peyton Example``,
    ``<#C…|design-team>`` -> ``#design-team``, and a bare ``@U08N…`` left by an older edit.
    Tokens no source names are left out, so the Preview shows the id rather than a guess."""
    out: dict[str, str] = {}
    for match in MRKDWN_USER.finditer(text or ""):
        label = mention_label(match, names)
        if label != f"@{match.group('id')}":
            out[match.group(0)] = label
    for match in BARE_USER_ID.finditer(text or ""):
        entry = (names or {}).get(match.group("id"))
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            out[match.group(0)] = f"@{name}"
    for match in MRKDWN_CHANNEL.finditer(text or ""):
        out[match.group(0)] = f"#{match.group('name')}"
    return out


HTTP_MD_LINK = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)")
MD_BOLD = re.compile(r"\*\*(?P<body>[^*\n]+?)\*\*")
MD_STRIKE = re.compile(r"~~(?P<body>[^~\n]+?)~~")
MD_BULLET = re.compile(r"^(\s*)[-*]\s+", re.M)
SLACK_SYNTAX = re.compile(r"<@[A-Z0-9]+|<#[A-Z0-9]+\||<https?://[^>]+\|[^>]+>|:robot_face:")


def is_slack_mrkdwn(text: str) -> bool:
    """True when the stored draft uses Slack syntax, so an edit must go back to mrkdwn to post."""
    return bool(SLACK_SYNTAX.search(text or ""))


def markdown_to_mrkdwn(text: str, original: str, names: dict[str, Any] | None = None) -> str:
    """Reverse of ``mrkdwn_to_markdown`` for an edited draft: Markdown links, bold, strike and
    bullets become Slack mrkdwn, and ``@name`` / ``#channel`` mentions that the original draft
    carried as ``<@U…>`` / ``<#C…|name>`` are restored from it so Slack still resolves them.
    ``names`` must be the same map the Markdown was rendered with, so ``@Real Name`` maps back."""
    if not text:
        return ""
    out = text
    out = HTTP_MD_LINK.sub(lambda m: f"<{m.group('url')}|{m.group('label')}>", out)
    out = MD_BOLD.sub(lambda m: f"*{m.group('body')}*", out)
    out = MD_STRIKE.sub(lambda m: f"~{m.group('body')}~", out)
    out = MD_BULLET.sub(r"\1• ", out)
    # Longest labels first so "@Perry Stone" is restored before a shorter "@Perry" could match.
    mentions = sorted({(mention_label(m, names), m.group(0)) for m in MRKDWN_USER.finditer(original or "")},
                      key=lambda pair: -len(pair[0]))
    for label, raw in mentions:
        out = out.replace(label, raw)
    for m in MRKDWN_CHANNEL.finditer(original or ""):
        out = out.replace(f"#{m.group('name')}", m.group(0))
    return out.strip()


def post_ready_draft(item: dict[str, Any], names: dict[str, Any] | None = None) -> str:
    """The text the owner pastes: the stored draft as-is, or their saved edit converted back to the
    original surface's syntax (Slack mrkdwn when the original used it, Markdown otherwise)."""
    original = str(item.get("draft_text") or "")
    edited = str(item.get("draft_markdown") or "")
    if not item.get("draft_edited_at") or not edited:
        return original or edited
    return markdown_to_mrkdwn(edited, original, names) if is_slack_mrkdwn(original) else edited


# --------------------------------------------------------------------------- selection

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def ledger_rows(root: Path) -> list[dict[str, Any]]:
    data = load_json(ledger_path(root), {})
    rows = data.get("tasks") if isinstance(data, dict) else data
    return [row for row in rows or [] if isinstance(row, dict)]


def is_health_row(task_id: str) -> bool:
    return bool(HEALTH_ROW_ID.match(task_id))


def tracked_id(task_id: str) -> bool:
    """A ledger id the feed represents: a dispatched task (``rt-``) or a job's rolling health row."""
    if task_id.startswith(NOISE_ID_PREFIXES):
        return False
    return task_id.startswith("rt-") or is_health_row(task_id)


def is_noise(row: dict[str, Any]) -> bool:
    task_id = str(row.get("id") or "")
    if not tracked_id(task_id):
        return True
    if row.get("status") == "cancelled":
        return True
    return bool(ADMIN_SUMMARY.match(normalize(row.get("summary") or "")))


def select_rows(rows: list[dict[str, Any]], now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows inside the window, split into (terminal candidates, live rows). Rows a later steer
    or continuation took over are not candidates; ``handled_rows`` lists them."""
    candidates, live, _handled = partition_rows(rows, now, window_hours)
    return candidates, live


def partition_rows(rows: list[dict[str, Any]], now: datetime, window_hours: int,
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """(terminal candidates, live rows, handled) inside the window. ``handled`` pairs a finished
    row with the later row that continued it (``continuation_of`` set by ``dispatch.py steer``
    or a ``--continuation-of`` dispatch after the row finished): its ask was consumed by that
    steer, so it shows in Past as handled instead of waiting in Needs you. A row nothing
    references stays a candidate."""
    cutoff = now - timedelta(hours=window_hours)
    live: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row.get("id") or "")
        if not tracked_id(task_id):
            continue
        moment = row_time(row)
        if not moment or moment < cutoff:
            continue
        if row.get("status") in LIVE:
            live.append(row)
            continue
        if row.get("status") not in TERMINAL or is_noise(row):
            continue
        candidates.append(row)
    continued_by: dict[str, dict[str, Any]] = {}
    for child in sorted(candidates + live, key=lambda r: parse_iso(r.get("created_at")) or row_time(r) or now):
        parent = str(child.get("continuation_of") or "")
        if parent and parent not in continued_by:
            continued_by[parent] = child
    handled: list[tuple[dict[str, Any], dict[str, Any]]] = []
    kept: list[dict[str, Any]] = []
    for row in candidates:
        child = continued_by.get(str(row.get("id")))
        if child is None:
            kept.append(row)
            continue
        child_at = parse_iso(child.get("created_at")) or row_time(child) or now
        if child_at >= (row_time(row) or now):
            handled.append((row, child))
        # A continuation created before the row finished (restart of live work) leaves nothing
        # for the owner either; the newer row carries the ask, so the parent still drops.
    kept.sort(key=lambda r: row_time(r) or now, reverse=True)
    return kept, live, handled


def handled_items(handled: list[tuple[dict[str, Any], dict[str, Any]]], root: Path, now: datetime) -> list[dict[str, Any]]:
    """Past rows for asks a later steer or continuation consumed: ``acted`` is ``handled`` and
    ``acted_by`` is ``cos`` (the surface that steered), dated by the steer. A finished row
    with no ask (for-review only) was never waiting on the owner, so it drops as before."""
    items: list[dict[str, Any]] = []
    for row, child in handled:
        child_id = str(child.get("id") or "")
        acted_at = parse_iso(child.get("created_at")) or row_time(child) or now
        for item in task_items(row, root, now):
            if item.get("section") != "needs_you":
                continue
            item.update(section="past", acted="handled", acted_by="cos", acted_at=iso(acted_at),
                        acted_reason=f"handled in chat: continued as {child_id}", continued_by=child_id,
                        day_label=day_label(acted_at, now), updated_at=iso(acted_at), updated_label=pt_label(acted_at, now))
            items.append(item)
    return items


def build_task_items(rows: list[dict[str, Any]], root: Path, now: datetime, quiet: set[str]) -> list[dict[str, Any]]:
    """One item per workstream by default; older rows show on their own only when they
    carry a standalone ask (draft, ruling, go, ...) of a kind the workstream is not already
    showing, or a kind that never folds (vetoes; a ``Follow-up:`` line). Everything else
    folds into the newest row as "+N earlier passes", listed under that item's ``folded`` so the
    app can open the fold. Quiet workstreams (router.yaml ``quiet_done_workstreams``) show only
    their newest row. ``rows`` arrive newest first."""
    shown: dict[str, dict[str, Any]] = {}  # workstream -> {"row", "extra", "kinds", "folded"}
    standalone: list[dict[str, Any]] = []
    for row in rows:
        workstream = str(row.get("workstream") or "")
        task_id = str(row.get("id") or "")
        if is_health_row(task_id):
            # A job's rolling health row is its own line: it never folds a worker's report for the
            # same workstream and is never folded under one, so the health of the job stays visible.
            workstream = f"{workstream}#{task_id}"
        text = normalize(row.get("summary") or "")
        prose = prose_of(text)
        kind = classify(row, prose, bool(drafts_in(text)))
        # A report that leaves work open ("Follow-up: …") is an ask the owner has not answered; a vetoes row is
        # finished work the owner still reads. Both show on their own, however many the workstream already shows;
        # a quiet workstream still folds its vetoes rows (finished work) but never an open follow-up.
        has_followup = kind not in {"blocked", "failed", "console"} and bool(asks_mod.followup_text(prose))
        never_folds = has_followup or kind in ALWAYS_STANDALONE_KINDS
        entry = shown.get(workstream)
        if entry is None:
            shown[workstream] = {"row": row, "extra": 0, "kinds": {kind}, "folded": []}
            continue
        if kind == "failed" or (workstream in quiet and kind not in STANDALONE_KINDS and not has_followup):
            entry["extra"] += 1  # a later pass replaced it; quiet workstreams fold finished rows
            entry["folded"].append(row)
            continue
        if never_folds or (kind in STANDALONE_KINDS and kind not in entry["kinds"]):
            entry["kinds"].add(kind)
            standalone.append(row)
            continue
        entry["extra"] += 1
        entry["folded"].append(row)
    items: list[dict[str, Any]] = []
    for entry in shown.values():
        items += task_items(entry["row"], root, now, entry["extra"], entry["folded"])
    for row in standalone:
        items += task_items(row, root, now)
    items.sort(key=lambda i: i["updated_at"], reverse=True)
    return items


def sent_evidence(rows: list[dict[str, Any]], now: datetime) -> list[tuple[datetime, set[str]]]:
    """(time, names-or-gmail-ids) a later report says were sent, posted, declined or superseded."""
    out: list[tuple[datetime, set[str]]] = []
    for row in rows:
        found = asks_mod.sent_evidence(prose_of(normalize(row.get("summary") or "")))
        if found:
            out.append((row_time(row) or now, found))
    return out


def drop_superseded_drafts(items: list[dict[str, Any]], evidence: list[tuple[datetime, set[str]]]) -> tuple[list[dict[str, Any]], int]:
    """A draft ask whose recipient or Gmail draft id a report at or after it says was sent is answered."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in items:
        if item.get("kind") != "draft" or item.get("source") != "task":
            kept.append(item)
            continue
        moment = parse_iso(item.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
        keys = {str(item.get("gmail_id") or ""), str(item.get("recipient") or "").split()[0] if item.get("recipient") else ""}
        keys.discard("")
        keys.discard("the")
        if any(when >= moment and keys & found for when, found in evidence):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


ANSWERABLE_LINK = re.compile(r"slack\.com/archives/|github\.com/[^/]+/[^/]+/(?:pull|issues)/\d+")


def answerable_refs(item: dict[str, Any]) -> list[str]:
    """Slack permalinks and PR/issue urls on a draft or post ask that reply_check can settle."""
    if item.get("kind") not in {"draft", "post"}:
        return []
    return [link["url"] for link in item.get("links") or [] if ANSWERABLE_LINK.search(link.get("url") or "")]


SLACK_REF = re.compile(r"slack\.com/archives/|^slack://", re.I)


def is_reply_item(item: dict[str, Any]) -> bool:
    """The app's Replies tab: a Slack message the owner owes. True for every triage-ledger
    draft whose source is Slack (``source_kind`` slack; Doc comments, Gmail and GitHub drafts are not
    Slack replies), a worker-parked Slack draft (``draft_to`` a DM, a #channel or a Slack thread; a Linear
    issue body or a Gmail draft is not), a post ask that carries a Slack permalink, and a CoS ask whose
    ref is a Slack thread or DM. Everything else (tasks, running, stalled, reviews) is a Project."""
    if item.get("source") == "triage":
        return str(item.get("source_kind") or "slack") == "slack"
    urls = [str(link.get("url") or "") for link in item.get("links") or []]
    has_slack_link = any(SLACK_REF.search(url) for url in urls)
    if item.get("kind") == "draft":
        if item.get("gmail_id") or item.get("recipient") == "Linear":
            return False
        to = str(item.get("draft_to") or "")
        if to == "Linear" or to.endswith(", email"):
            return False
        return bool(to) or has_slack_link
    if item.get("kind") == "post" or item.get("source") == "cos-ask":
        return has_slack_link
    return False


def pending_triage_drafts(now: datetime, window_hours: int) -> list[dict[str, Any]]:
    if os.environ.get("COS_REVIEW_SKIP_TRIAGE"):  # tests: never read the real triage ledger
        return []
    try:
        import triage_ledger  # type: ignore
    except Exception:
        return []
    try:
        drafts = triage_ledger.pending_drafts() + cos_review_dismissed_drafts(triage_ledger) + settled_drafts(triage_ledger)
    except Exception:
        return []
    cutoff = now - timedelta(hours=window_hours)
    out = []
    for draft in drafts:
        meta = draft.get("draft_meta") if isinstance(draft.get("draft_meta"), dict) else {}
        status = str(draft.get("draft_status") or "")
        if status in triage_ledger.PENDING_DRAFT_STATUSES and meta.get("parked_by_task"):
            # A parked DM stays a Needs-you card until sent or dropped, however old.
            out.append(draft)
            continue
        held = (parse_iso(meta.get("at")) if status in {"dismissed", "sent"} else None) \
            or parse_iso(draft.get("draft_held_at")) or parse_iso(draft.get("draft_updated_at"))
        if held and held >= cutoff:
            out.append(draft)
    return out


def settled_drafts(triage_ledger: Any) -> list[dict[str, Any]]:
    """Drafts the ledger itself settled (``sent`` from Comms Triage or a worker's sent mark, or
    ``dismissed`` outside CoS Review), so Past shows where the decision landed."""
    out: list[dict[str, Any]] = []
    try:
        with triage_ledger.state_transaction(triage_ledger.STATE_FILE) as state:
            for item in state["items"].values():
                meta = item.get("draft_meta") if isinstance(item.get("draft_meta"), dict) else {}
                status = str(item.get("draft_status") or "")
                if status == "sent" or (status == "dismissed" and not str(meta.get("reason") or "").startswith("cos-review")):
                    out.append(dict(item))
    except Exception:
        return []
    return out


def cos_review_dismissed_drafts(triage_ledger: Any) -> list[dict[str, Any]]:
    """Drafts the owner completed in CoS Review (ledger ``dismissed`` with ``draft_meta.reason`` starting
    ``cos-review``), so Past keeps showing them after Comms Triage stops listing them as held."""
    out: list[dict[str, Any]] = []
    try:
        with triage_ledger.state_transaction(triage_ledger.STATE_FILE) as state:
            for item in state["items"].values():
                meta = item.get("draft_meta") if isinstance(item.get("draft_meta"), dict) else {}
                if item.get("draft_status") == "dismissed" and str(meta.get("reason") or "").startswith("cos-review"):
                    out.append(dict(item))
    except Exception:
        return []
    return out


def answered_refs(refs: list[str]) -> set[str]:
    """Refs whose cached reply_check verdict says the draft is not an open ask: answered by the owner or
    someone else, or classified no_ask / settled / stale by the already-answered pre-check. Cache only,
    no network; the reconcile tick supersedes the ledger row itself."""
    try:
        import reply_check  # type: ignore
    except Exception:
        return set()
    non_open = getattr(reply_check, "NON_OPEN_CLASSIFICATIONS", frozenset({"answered"}))
    answered: set[str] = set()
    for ref in refs:
        try:
            verdict = reply_check.cached_verdict(ref, allow_stale=True)
            if not verdict:
                continue
            if reply_check.is_answered(verdict) or str(verdict.get("classification") or "") in non_open:
                answered.add(ref)
        except Exception:
            continue
    return answered


def moved_on_notes(refs: list[str]) -> dict[str, str]:
    """One short caption per ref whose thread moved on without settling the draft: a teammate
    replied after the ask, or the owner reacted with something other than a handled-reaction. The
    draft stays in Needs you (Comms Triage's rule: only ✅-style evidence retires it) but the row
    says so, so the owner can skip it from the list. Cache only, no network."""
    try:
        import reply_check  # type: ignore
    except Exception:
        return {}
    notes: dict[str, str] = {}
    for ref in refs:
        try:
            verdict = reply_check.cached_verdict(ref, allow_stale=True)
        except Exception:
            continue
        if not verdict:
            continue
        kind = str(verdict.get("verdict") or "")
        evidence = verdict.get("evidence") or {}
        if kind == "teammate_replied":
            closer = str(evidence.get("closer") or evidence.get("closer_user") or "a teammate")
            notes[ref] = f"Thread moved on: {closer} replied"
        elif kind == "resolved_by_reaction":
            reaction = str(evidence.get("reaction") or "").split("::")[0]
            notes[ref] = f"Thread moved on: you reacted :{reaction}:" if reaction else "Thread moved on: you reacted"
    return notes


LIVE_CHECK_BUDGET_SECONDS = float(os.environ.get("COS_REVIEW_REPLY_BUDGET", "20") or 0)
LIVE_CHECK_MAX_REFS = 4


def answered_task_refs(refs: list[str], *, budget_seconds: float | None = None) -> set[str]:
    """Refs on worker-report asks that are settled: the owner replied in that Slack thread, or the
    PR/issue is merged or closed. Someone else replying in Slack does not retire a draft the owner was
    going to send. Cached verdicts first; refs with no cached verdict get one bounded live read each
    (at most ``LIVE_CHECK_MAX_REFS`` per build, inside ``budget_seconds``) through
    ``reply_check.bounded_checker``, which honours the provider backoff and never raises."""
    try:
        import reply_check  # type: ignore
    except Exception:
        return set()
    budget = LIVE_CHECK_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    checker = reply_check.bounded_checker(budget, live=budget > 0) if budget > 0 else None
    live_reads = 0
    answered: set[str] = set()
    for ref in refs:
        try:
            verdict = reply_check.cached_verdict(ref, allow_stale=True)
        except Exception:
            continue
        if not verdict and checker is not None and live_reads < LIVE_CHECK_MAX_REFS:
            live_reads += 1
            try:
                verdict = checker({"ref": ref})
            except Exception:
                verdict = None
        if not verdict:
            continue
        if str(verdict.get("verdict")) == "resolved_by_owner":
            answered.add(ref)
        elif "github.com" in ref and reply_check.is_answered(verdict, for_draft=False):
            answered.add(ref)
    return answered


def action_record(records: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    """The action record for an item: its own id first, else a task-scoped record
    (``complete --task-id rt-x``, key ``task:rt-x`` with ``scope: task``) that closes every
    ask the task row raised. An app click on the first ask (``task:rt-x`` with no scope)
    still closes only that row."""
    own = records.get(str(item.get("id") or ""))
    if isinstance(own, dict):
        return own
    task_id = str(item.get("task_id") or "")
    if task_id and str(item.get("id") or "").startswith("task:"):
        shared = records.get(f"task:{task_id}")
        if isinstance(shared, dict) and shared.get("scope") == "task":
            return shared
    return None


DELIVERED_REPLY_STATES = {"sent", "typed", "continuation", "decided", "dispatched", "redispatched"}


def latest_reply_state(log: list[dict[str, Any]]) -> str:
    """State of the newest reply record (by ``at``, then order), ``""`` when none. A failed or
    deferred attempt followed by a sent one reads as sent; the row marker shows this value."""
    if not log:
        return ""
    newest = max(enumerate(log), key=lambda pair: (parse_iso(pair[1].get("at")) or datetime.min.replace(tzinfo=timezone.utc), pair[0]))
    return str(newest[1].get("state") or "")


def reply_view(record: dict[str, Any], now: datetime, task_refs: dict[str, str]) -> dict[str, Any]:
    """A stored reply as the pane shows it: PT stamp, and the continuation's tab when the record lacks it."""
    view = dict(record, at_label=reply_at_label(record.get("at"), now))
    child = str(record.get("continuation_task_id") or "")
    if child and not view.get("workspace_ref") and task_refs.get(child):
        view["workspace_ref"] = task_refs[child]
    return view


def apply_actions(items: list[dict[str, Any]], actions: dict[str, Any], now: datetime,
                  names: dict[str, Any] | None = None,
                  task_refs: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Fold ``cos-review-actions.json`` into the items.

    - complete / skip: the item moves to Past (its day is the day the owner acted) with
      ``acted`` set, so the list reads like a logbook instead of silently losing rows.
    - snooze: hidden while ``until`` is after today (PT); the day it returns it shows
      again with ``snoozed_until`` set.
    - read: a Needs-you item parks under For review (``read``, ``read_at``) with its draft
      and options intact, until the row changes after the read (newer ``updated_at``).
    - pins: ``pinned`` is set and the item moves to the Pinned section; a completed or
      skipped row drops its pin and goes to Past.
    - drafts: a saved edit replaces ``draft_markdown``.
    - replies: the delivery log is attached as ``replies``; a continuation reply whose record
      predates the child's tab gets ``workspace_ref`` from the child's ledger row (``task_refs``),
      so "Show in cmux" works for it.
    Running rows never complete, skip or snooze; they can be pinned."""
    actions = actions if isinstance(actions, dict) else {}
    records = actions.get("items") if isinstance(actions.get("items"), dict) else {}
    pins = {str(p) for p in (actions.get("pins") or []) if isinstance(actions.get("pins"), list)}
    drafts = actions.get("drafts") if isinstance(actions.get("drafts"), dict) else {}
    replies = actions.get("replies") if isinstance(actions.get("replies"), dict) else {}
    today = now.astimezone(PT).date()
    kept: list[dict[str, Any]] = []
    snoozed = 0
    for item in items:
        item = dict(item)
        item.setdefault("draft_markdown", mrkdwn_to_markdown(item.get("draft_text") or "", names))
        saved = drafts.get(item["id"])
        if isinstance(saved, dict) and str(saved.get("text") or "").strip():
            item["draft_markdown"] = str(saved["text"])
            item["draft_edited_at"] = str(saved.get("at") or "")
        item["draft_post"] = post_ready_draft(item, names)
        item["draft_mentions"] = draft_mentions(f"{item.get('draft_text') or ''}\n{item['draft_markdown']}", names)
        log = replies.get(item["id"])
        item["replies"] = [reply_view(r, now, task_refs or {}) for r in log if isinstance(r, dict)] \
            if isinstance(log, list) else []
        item["reply_state"] = latest_reply_state(item["replies"])
        item["pinned"] = item["id"] in pins
        record = action_record(records, item) if item["section"] != "running" else None
        if not isinstance(record, dict) and item["section"] in {"needs_you", "for_review"} \
                and item["reply_state"] in DELIVERED_REPLY_STATES:
            # A reply that reached its thread, or became a continuation task, answers the ask. The item
            # leaves Needs you on the next rebuild even before the ledger shows the continuation;
            # a continuation shows under Running through its own live row.
            latest = item["replies"][-1]
            acted_at = parse_iso(latest.get("at")) or now
            child = str(latest.get("continuation_task_id") or "")
            reason = f"replied: continued as {child}" if child else f"replied ({item['reply_state']})"
            item.update(section="past", acted="replied", acted_at=iso(acted_at), acted_by="owner",
                        acted_reason=reason, day_label=day_label(acted_at, now), updated_at=iso(acted_at),
                        updated_label=pt_label(acted_at, now))
            if child:
                item["continued_by"] = child
        if isinstance(record, dict):
            action = record.get("action")
            if action in {"complete", "skip"}:
                acted_at = parse_iso(record.get("at")) or now
                item.update(section="past", acted=action, acted_at=iso(acted_at),
                            day_label=day_label(acted_at, now), updated_at=iso(acted_at),
                            updated_label=pt_label(acted_at, now),
                            acted_by=str(record.get("by") or ""), acted_reason=str(record.get("reason") or ""))
            elif action == "snooze":
                until = record.get("until")
                try:
                    until_date = datetime.strptime(str(until), "%Y-%m-%d").date()
                except ValueError:
                    until_date = today
                if until_date > today:
                    snoozed += 1
                    continue
                item["snoozed_until"] = str(until)
            elif action == "read" and item["section"] == "needs_you":
                # Mark as read: the ask parks under For review with its
                # draft and options intact; nothing is sent, dismissed or snoozed. The record only
                # holds while the item is unchanged: a report or steer that updates the row after
                # the read (``updated_at`` newer than the record) needs a new decision, so the item
                # returns to Needs you and the stale record is ignored.
                read_at = parse_iso(record.get("at")) or now
                changed = parse_iso(item.get("updated_at")) or now
                if changed <= read_at:
                    item.update(section="for_review", read=True, read_at=iso(read_at),
                                read_by=str(record.get("by") or ""))
        if item.get("acted"):
            # A completed or skipped row is history: it leaves Pinned and the pin no longer applies.
            item["pinned"] = False
        if item["pinned"]:
            item["origin_section"] = item["section"]
            item["section"] = "pinned"
        kept.append(item)
    return kept, snoozed


# --------------------------------------------------------------------------- settle (other consumers)
#
# Many Needs you items are already sent, dropped, snoozed or ruled somewhere else. The feed reads the
# three places a decision lands outside the app: todo.json (done / dropped / scheduled / snoozed),
# the triage ledger (sent / dismissed / snoozed / no-robot) and CoS chat rulings recorded in notes
# and CONTEXT ("sent", "dropped", "approved", "ruled"). An item settled there leaves Needs you on
# the next rebuild, captioned with where the decision was recorded.

SLACK_ARCHIVE_KEY = re.compile(r"archives/([A-Z0-9]+)/p(\d+)")
TODO_CLOSED = {"done", "dropped", "cancelled"}
RULING_VERB = re.compile(r"\b(sent|posted|dropped|approved|ruled|dismissed|declined|superseded|complete|completed)\b", re.I)
# "the owner-owned sites" and "the owner's" are not an actor; a ruling names the owner or CoS as the one who decided.
RULING_ACTOR = re.compile(r"\bOwner\b(?![-'’])|\bCoS\b|Complete in CoS Review|cos_review_actions\.py", re.I)
# "Nothing deployed, deleted, or sent" is a status line, never a send: the negation may sit a few words
# before the verb.
RULING_NOT_SENT = re.compile(
    r"\bnot (?:yet )?sent\b|\bunsent\b|\bnever sent\b|\bnothing\b[^.;]{0,60}?\b(?:sent|posted)\b|\bnot posted\b|\bparked\b", re.I)
RULING_DROP = re.compile(r"\b(dropped|complete|completed|dismissed|declined|superseded)\b", re.I)
RULING_ITEM_ID = re.compile(r"\b(task:rt-[0-9a-f]{12}:[0-9a-f]{6})\b")
RULING_TASK_ID = re.compile(r"\b(rt-[0-9a-f]{12})\b")
RULING_DRAFT_ID = re.compile(r"\bdraft[- ](\d{1,5})\b", re.I)
RULING_LINE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
CLOSEOUT_HEADING = re.compile(r"^##\s+Worker close-outs", re.I)
DECISION_HEADING = re.compile(r"^##\s+the owner decisions", re.I)
# `dispatch.py record_decision` shape: "- <date> · `<task>` · <by>: <text>", where <by> is owner, cos,
# "the owner via CoS 00:25 PT", "report:rt-…" or "dispatch:cos". The backticked task is the target.
DECISION_LINE = re.compile(
    r"^\s*-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+·\s+`(?P<task>rt-[0-9a-f]{12})`\s+·\s+"
    r"(?P<by>report:rt-[0-9a-f]{12}|dispatch:\w+|(?:the owner|owner|CoS|cos)\b[^·:]{0,60}?(?:\d{1,2}:\d{2}\s*PT)?):\s+(?P<text>.*)$")
# Worker close-out shape: "- <date> · `<task>` · <emoji> <Title> <n> · done: …" (also written outside the
# close-outs heading, for example under "## the owner decisions"). A report, never a ruling.
CLOSEOUT_LINE = re.compile(
    r"^\s*-\s+\d{4}-\d{2}-\d{2}\s+·\s+`?rt-[0-9a-f]{12}`?\s+·\s+[^·]{1,80}·\s+(done|failed|blocked|cancelled|deferred):")
RULING_HOLD = re.compile(r"\b(held|hold|do not (?:dm|send|post)|don't (?:dm|send|post))\b", re.I)
RULING_MAX_LINE = 400
RULING_DIRS = ("workstreams", "outputs/reviews")
RULING_SKIP_FILES = {"workstreams/README.md"}


def settle_key(url: str) -> str:
    """Host-independent key for a source ref: Slack permalinks collapse to ``archives/<channel>/p<id>``
    (``team-a.slack.com`` and ``example.slack.com`` name the same message), everything else
    to the ledger's canonical form without a trailing slash."""
    text = str(url or "").strip()
    if not text:
        return ""
    match = SLACK_ARCHIVE_KEY.search(text)
    if match:
        return f"archives/{match.group(1)}/p{match.group(2)}"
    try:
        import triage_ledger  # type: ignore
        text = triage_ledger.canonicalize_ref(text)
    except Exception:
        pass
    return text.rstrip("/").lower()


def source_refs(item: dict[str, Any]) -> list[str]:
    """The refs that identify what an item asks about, for todo matching: a triage draft's inbound ref,
    an ask's source, and the Slack or PR links on a worker draft or post. Follow-up lines, rulings and
    blocked items carry links to deliverables, not to a source, so they never match a todo by url."""
    kind = str(item.get("kind") or "")
    refs: list[str] = []
    if kind == "triage":
        refs.append(str(item.get("ref") or ""))
        refs += [link.get("url") or "" for link in item.get("links") or []]
    elif kind == "ask":
        refs += [link.get("url") or "" for link in item.get("links") or [] if not str(link.get("url") or "").startswith("https://ampcode.com/")]
    elif kind in {"draft", "post"}:
        refs += answerable_refs(item)
    return [settle_key(r) for r in refs if r]


def todo_entries(root: Path) -> list[dict[str, Any]]:
    data = load_json(state_dir(root) / "todo.json", {})
    items = data.get("items") if isinstance(data, dict) else data
    return [t for t in (items or []) if isinstance(t, dict)]


def todo_settles(todo: dict[str, Any], today: Any) -> tuple[str, str] | None:
    """(acted, reason) for a closed todo, ("snoozed", until) for a scheduled or snoozed one, else None."""
    status = str(todo.get("status") or "")
    if status in TODO_CLOSED:
        acted = "done" if status == "done" else "dropped"
        return acted, str(todo.get("resolution") or todo.get("completed_at") or "")
    until = str(todo.get("snooze_until") or "")[:10]
    if status == "scheduled" or (until and until > today.isoformat()):
        return "snoozed", until or str(todo.get("due") or "")[:10]
    return None


def todo_text_match(todo: dict[str, Any], item: dict[str, Any]) -> bool:
    """A todo that names this item by id: the parked-draft mirror todo (``Parked by <rt>`` in its
    context) or any todo whose text names the draft id (``draft-N`` / ``draft N``). A bare rt id in a
    resolution never counts: meeting-ingest duplicates say "folded into rt-…" about a different ask."""
    text = " ".join(str(todo.get(k) or "") for k in ("title", "context", "resolution"))
    draft_id = str(item.get("draft_id") or "")
    if draft_id:
        number = draft_id.split("-")[-1]
        if re.search(rf"\bdraft[- ]{re.escape(number)}\b", text, re.I):
            return True
    task_id = str(item.get("task_id") or "")
    return bool(task_id and todo.get("source") == "parked-draft-slack" and task_id in str(todo.get("context") or ""))


def todo_time(todo: dict[str, Any]) -> datetime | None:
    return parse_iso(todo.get("completed_at")) or parse_iso(todo.get("updated_at")) or parse_iso(todo.get("snoozed_at"))


def settle_from_todos(items: list[dict[str, Any]], root: Path, now: datetime) -> tuple[list[dict[str, Any]], int, int]:
    """Fold todo.json into the items: an exact source_ref match settles regardless of time (the todo
    and the item are the same ask), a text match only when the todo changed after the item did."""
    todos = todo_entries(root)
    if not todos:
        return items, 0, 0
    today = now.astimezone(PT).date()
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for todo in todos:
        key = settle_key(str(todo.get("source_ref") or ""))
        if key:
            by_ref.setdefault(key, []).append(todo)
    named = [t for t in todos if t.get("source") == "parked-draft-slack" or re.search(r"\bdraft[- ]\d{1,5}\b", " ".join(str(t.get(k) or "") for k in ("title", "context", "resolution")), re.I)]
    kept: list[dict[str, Any]] = []
    settled = snoozed = 0
    for item in items:
        if item.get("section") not in {"needs_you", "for_review"}:
            kept.append(item)
            continue
        verdict: tuple[str, str] | None = None
        match: dict[str, Any] | None = None
        for key in source_refs(item):
            for todo in by_ref.get(key, []):
                verdict = todo_settles(todo, today)
                if verdict:
                    match = todo
                    break
            if verdict:
                break
        if not verdict:
            changed = parse_iso(item.get("updated_at")) or now
            for todo in named:
                if not todo_text_match(todo, item):
                    continue
                candidate = todo_settles(todo, today)
                when = todo_time(todo)
                if candidate and (candidate[0] == "snoozed" or (when and when >= changed)):
                    verdict, match = candidate, todo
                    break
        if not verdict or not match:
            kept.append(item)
            continue
        acted, reason = verdict
        if acted == "snoozed":
            snoozed += 1
            continue
        when = todo_time(match) or now
        item = dict(item)
        item.update(section="past", acted=acted, acted_at=iso(when), acted_by="todo",
                    acted_reason=f"todo {match.get('id') or ''}: {reason}".strip(), updated_at=iso(when),
                    updated_label=pt_label(when, now), day_label=day_label(when, now))
        settled += 1
        kept.append(item)
    return kept, settled, snoozed


def settle_from_ledger(items: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], int, int]:
    """Fold the triage ledger's own decisions into the items: a sent or dismissed draft goes to Past,
    a snoozed one hides until its date, and a worker "draft" card whose parked copy lives in the ledger
    defers to that copy (one card per draft, and ``send N`` works on it)."""
    today = now.astimezone(PT).date().isoformat()
    parked: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("kind") == "triage" and item.get("parked_by"):
            parked[str(item["parked_by"])] = item
    kept: list[dict[str, Any]] = []
    settled = snoozed = 0
    for item in items:
        if item.get("kind") == "draft" and item.get("section") in {"needs_you", "for_review"} and str(item.get("task_id") or "") in parked:
            twin = parked[str(item["task_id"])]
            if twin.get("section") == "past":
                item = dict(item)
                item.update(section="past", acted=twin.get("acted"), acted_at=twin.get("acted_at"), acted_by=twin.get("acted_by"),
                            acted_reason=f"ledger {twin.get('draft_id')}: {twin.get('acted_reason') or ''}".strip(),
                            updated_at=twin.get("updated_at"), updated_label=twin.get("updated_label"), day_label=twin.get("day_label"))
                settled += 1
                kept.append(item)
            # Pending twin: the ledger card is the one card; the worker's own draft card is folded into it.
            continue
        if item.get("kind") != "triage" or item.get("section") not in {"needs_you", "for_review"}:
            kept.append(item)
            continue
        status = str(item.get("status") or "held")
        until = str(item.get("snoozed_until") or "")[:10]
        if status in {"sent", "dismissed"}:
            when = parse_iso(item.get("acted_at")) or parse_iso(item.get("updated_at")) or now
            item = dict(item)
            item.update(section="past", acted=status, acted_at=iso(when), acted_by=str(item.get("acted_by") or "owner"),
                        acted_reason=str(item.get("acted_reason") or ""), updated_at=iso(when),
                        updated_label=pt_label(when, now), day_label=day_label(when, now))
            settled += 1
            kept.append(item)
            continue
        if until and until > today:
            snoozed += 1
            continue
        kept.append(item)
    return kept, settled, snoozed


def ruling_lines(root: Path, since: datetime) -> list[tuple[str, int, str, datetime, bool]]:
    """(path, line number, line, file mtime, under ``## the owner decisions``) for every candidate ruling line
    in a recently changed note or CONTEXT. Skipped: lines under ``## Worker close-outs``, close-out shaped
    lines anywhere (worker reports, not rulings), table rows, the generated workstreams README, and lines
    longer than RULING_MAX_LINE (narrative that names many tasks is never a ruling on one of them)."""
    out: list[tuple[str, int, str, datetime, bool]] = []
    private = private_workstreams.slugs(root)
    for rel in RULING_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        paths = list(base.glob("*.md")) + list(base.glob("*/*.md")) if rel == "workstreams" else list(base.glob("*.md"))
        for path in paths:
            rel_path = str(path.relative_to(root))
            if rel_path in RULING_SKIP_FILES or private_workstreams.is_private_path(path.relative_to(root).as_posix(), private):
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < since:
                continue
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            in_closeouts = in_decisions = False
            for number, line in enumerate(lines, 1):
                if line.startswith("#"):
                    in_closeouts = bool(CLOSEOUT_HEADING.match(line))
                    in_decisions = bool(DECISION_HEADING.match(line))
                    continue
                stripped = line.lstrip()
                if in_closeouts or stripped.startswith("|") or len(line) > RULING_MAX_LINE:
                    continue
                if CLOSEOUT_LINE.match(line):
                    continue
                if not (DECISION_LINE.match(line) or RULING_VERB.search(line)):
                    continue
                out.append((rel_path, number, line, mtime, in_decisions))
    return out


def ruling_targets(line: str) -> tuple[set[str], set[str], set[str]]:
    """(full item ids, bare task ids, draft ids) a ruling line names."""
    full = set(RULING_ITEM_ID.findall(line))
    tasks = set(RULING_TASK_ID.findall(line)) - {f.split(":")[1] for f in full}
    drafts = {f"draft-{n}" for n in RULING_DRAFT_ID.findall(line)}
    return full, tasks, drafts


def ruling_verdict(line: str) -> str:
    """"sent", "dropped", "approved" or "ruled" for a line that records an the owner/CoS decision; "" otherwise."""
    if not RULING_ACTOR.search(line):
        return ""
    if RULING_NOT_SENT.search(line) and not RULING_DROP.search(line):
        return ""
    lowered = line.lower()
    if RULING_DROP.search(line):
        return "dropped"
    if re.search(r"\b(sent|posted)\b", lowered):
        return "sent"
    if "approved" in lowered:
        return "approved"
    return "ruled"


def decision_verdict(text: str) -> str:
    """Verdict for the text of a ``record_decision`` line. Every such line is an the owner or CoS ruling by
    construction, so it always settles: dropped, sent, approved, held (a "do not DM / hold" ruling) or ruled."""
    if RULING_DROP.search(text):
        return "dropped"
    if re.search(r"\b(sent|posted)\b", text, re.I) and not RULING_NOT_SENT.search(text):
        return "sent"
    if RULING_HOLD.search(text):
        return "held"
    if re.search(r"\b(approved|accepted|option \d|\bgo\b)\b", text, re.I):
        return "approved"
    return "ruled"


def parse_ruling(line: str, in_decisions: bool) -> tuple[str, set[str], Any] | None:
    """(verdict, targets, date) for a ruling line, or None. A ``record_decision`` line targets its backticked
    task. Any other line is a ruling only when it names exactly one target (one task id, one full item id,
    or one draft id), carries an the owner or CoS actor and a ruling verb."""
    decision = DECISION_LINE.match(line)
    if decision:
        verdict = decision_verdict(decision.group("text"))
        when = datetime.strptime(decision.group("date"), "%Y-%m-%d").date()
        return verdict, {f"task:{decision.group('task')}"}, when
    if in_decisions:
        # Under "## the owner decisions" only record_decision lines count; anything else there is pasted context.
        return None
    verdict = ruling_verdict(line)
    if not verdict:
        return None
    full, tasks, drafts = ruling_targets(line)
    targets = full | {f"task:{t}" for t in tasks} | drafts
    if len(targets) != 1:
        return None
    dated = RULING_LINE_DATE.search(line)
    when = None
    if dated:
        try:
            when = datetime.strptime(dated.group(1), "%Y-%m-%d").date()
        except ValueError:
            when = None
    return verdict, targets, when


def settle_from_rulings(items: list[dict[str, Any]], root: Path, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], int]:
    """Fold chat rulings recorded in notes into the items. A line settles an item only when it names the
    item (its full ``task:<rt>:<hash>`` id, its task id for the task's primary card, or its ``draft-N``),
    carries a ruling verb and an the owner or CoS actor, and is dated on or after the item's last change.
    A bare task ruling settles the task's primary card and its parked ledger twin, never its follow-ups."""
    if os.environ.get("COS_REVIEW_SKIP_RULINGS"):
        return items, 0
    since = now - timedelta(hours=window_hours + 24)
    lines = ruling_lines(root, since)
    if not lines:
        return items, 0
    rulings: dict[str, tuple[str, str, Any]] = {}  # target -> (verdict, "path:line", date or None)
    for path, number, line, mtime, in_decisions in lines:
        parsed = parse_ruling(line, in_decisions)
        if not parsed:
            continue
        verdict, targets, when = parsed
        for target in targets:
            rulings.setdefault(target, (verdict, f"{path}:{number}", when or mtime))
    result: list[dict[str, Any]] = []
    settled = 0
    for item in items:
        if item.get("section") not in {"needs_you", "for_review"}:
            result.append(item)
            continue
        hit = rulings.get(str(item.get("id") or ""))
        if not hit and item.get("draft_id"):
            hit = rulings.get(str(item["draft_id"]))
        if not hit and item.get("kind") == "triage" and item.get("parked_by"):
            hit = rulings.get(f"task:{item['parked_by']}")
        if not hit:
            result.append(item)
            continue
        verdict, where, when = hit
        changed = (parse_iso(item.get("updated_at")) or now).astimezone(PT).date()
        when_date = when if not isinstance(when, datetime) else when.astimezone(PT).date()
        if when_date < changed:
            result.append(item)
            continue
        acted_at = when if isinstance(when, datetime) else datetime.combine(when, datetime.min.time(), tzinfo=PT).astimezone(timezone.utc)
        acted_at = min(acted_at, now)
        item = dict(item)
        item.update(section="past", acted=verdict, acted_at=iso(acted_at), acted_by="owner",
                    acted_reason=f"ruled in chat, recorded at {where}", updated_at=iso(acted_at),
                    updated_label=pt_label(acted_at, now), day_label=day_label(acted_at, now))
        settled += 1
        result.append(item)
    return result, settled


def settle_items(items: list[dict[str, Any]], root: Path, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply every consumer outside the app, ledger first (it holds the draft itself), then todo.json,
    then chat rulings. Returns the items and the counts the feed reports."""
    items, by_ledger, hidden_ledger = settle_from_ledger(items, now)
    items, by_todo, hidden_todo = settle_from_todos(items, root, now)
    items, by_ruling = settle_from_rulings(items, root, now, window_hours)
    return items, {"settled_by_ledger": by_ledger, "settled_by_todo": by_todo, "settled_by_ruling": by_ruling,
                   "snoozed_elsewhere": hidden_ledger + hidden_todo}


def by_day(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Past rows grouped newest day first: [{"label", "date", "items"}]."""
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        moment = parse_iso(item.get("updated_at")) or now
        key = moment.astimezone(PT).date().isoformat()
        group = groups.setdefault(key, {"label": item.get("day_label") or day_label(moment, now), "date": key, "items": []})
        group["items"].append(item)
    for group in groups.values():
        group["items"].sort(key=lambda i: i["updated_at"], reverse=True)
    return [groups[k] for k in sorted(groups, reverse=True)]


def build_feed(root: Path, now: datetime | None = None, window_hours: int = DEFAULT_WINDOW_HOURS,
               fresh_hours: int = FRESH_HOURS) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    config = router_config(root)
    quiet = set((config.get("dispatch") or {}).get("quiet_done_workstreams") or [])
    profile = root / "system" / "config" / "router.profile.yaml"
    if profile.exists():  # organisation profile additions (apply-profile.py)
        try:
            import yaml
            extra = (yaml.safe_load(profile.read_text(encoding="utf-8")) or {}).get("dispatch") or {}
            quiet |= set(extra.get("quiet_done_workstreams") or [])
        except Exception:
            pass
    candidates, live, handled = partition_rows(ledger_rows(root), now, window_hours)
    fresh_cutoff = now - timedelta(hours=fresh_hours)
    fresh = [r for r in candidates if (row_time(r) or now) >= fresh_cutoff]
    older = [r for r in candidates if (row_time(r) or now) < fresh_cutoff]
    items = build_task_items(fresh, root, now, quiet)
    for item in items:
        # A recovered job needs no review: its health row goes to Past as soon as it is green,
        # captioned recovered, and comes back to Needs you only on the next incident.
        if is_health_row(str(item.get("task_id") or "")) and item["section"] == "for_review":
            item.update(section="past", acted="recovered", acted_at=item["updated_at"], acted_by="health-pull")
    # Older finished work folds per workstream within each PT day, so Past reads as a logbook.
    days: dict[str, list[dict[str, Any]]] = {}
    for row in older:
        days.setdefault((row_time(row) or now).astimezone(PT).date().isoformat(), []).append(row)
    for day_rows in days.values():
        for item in build_task_items(day_rows, root, now, quiet):
            if item["section"] == "for_review":
                item["section"] = "past"
            items.append(item)
    items, superseded = drop_superseded_drafts(items, sent_evidence(candidates + live, now))
    task_refs = sorted({ref for item in items for ref in answerable_refs(item)})
    settled = answered_task_refs(task_refs) if not os.environ.get("COS_REVIEW_SKIP_TRIAGE") else set()
    before = len(items)
    items = [item for item in items if not (settled and set(answerable_refs(item)) & settled)]
    superseded += before - len(items)
    # Asks a later steer or continuation consumed: Past, captioned as handled, never Needs you.
    items += handled_items(handled, root, now)

    drafts = pending_triage_drafts(now, window_hours)
    refs = [str(d.get("ref") or d.get("draft_source_link") or "") for d in drafts]
    answered = answered_refs([r for r in refs if r])
    moved_on = moved_on_notes([r for r in refs if r])
    empty_drafts = 0
    for draft in drafts:
        ref = str(draft.get("ref") or draft.get("draft_source_link") or "")
        if ref and ref in answered:
            continue
        if not str(draft.get("draft_text") or "").strip():
            # No reply text yet: "Send as is" would post nothing. Comms Triage still holds the card.
            empty_drafts += 1
            continue
        item = triage_item(draft, now, channel_names(root))
        if ref in moved_on:
            # A thread that moved on is not an ask any more; it goes to Past with the caption.
            item.update(thread_note=moved_on[ref], section="past", acted="moved_on")
        items.append(item)

    items += ask_items(root, now, window_hours)
    agents = live_agent_map()
    items += running_items(live, root, now, agents)
    for item in items:
        # Reply routing in the app: a live thread takes a native message, a reaped one needs a continuation.
        item.setdefault("thread_live", bool(item.get("thread_id") and agents.get(item["thread_id"])))
    actions = load_json(actions_path(root), {})
    task_refs = {str(r.get("id") or ""): str(r.get("workspace_ref") or "")
                 for r in ledger_rows(root) if isinstance(r, dict) and r.get("workspace_ref")}
    items, settled_counts = settle_items(items, root, now, window_hours)
    items, snoozed = apply_actions(items, actions, now, slack_names(root), task_refs)
    for item in items:
        # Every card carries its workstream display name as a tag and a Replies/Projects verdict.
        # Additive fields: FEED_VERSION 4 consumers ignore them.
        item.setdefault("workstream_name", workstream_name(root, str(item.get("workstream") or "")))
        item["reply_item"] = is_reply_item(item)
    items.sort(key=lambda i: i["updated_at"], reverse=True)
    sections = {name: [i for i in items if i["section"] == name] for name in SECTIONS}
    sections["running"].sort(key=lambda i: i.get("started_at") or i["updated_at"], reverse=True)
    titles = link_titles(root, now)
    workstreams = workstream_index(root, ledger_rows(root), titles)
    titles.save()
    return {
        "version": FEED_VERSION,
        "generated_at": iso(now),
        "generated_label": now.astimezone(PT).strftime("%a %H:%M PT"),
        # The actions file this build applied. The app keeps a row it acted on hidden until a feed built
        # from an actions file written at or after that action arrives, so a loop build that started
        # before the action cannot bring the row back.
        "actions_updated_at": str((actions or {}).get("updated_at") or "") if isinstance(actions, dict) else "",
        "window_hours": window_hours,
        "fresh_hours": fresh_hours,
        "sections": [{"id": name, "label": SECTION_LABELS[name], "count": len(sections[name])} for name in SECTIONS],
        **sections,
        "past_days": by_day(sections["past"], now),
        "in_progress": sorted({str(i.get("workstream") or i.get("task_id")) for i in sections["running"]}),
        "stalled_count": sum(1 for i in items if i.get("kind") == "stalled"),
        "snoozed_count": snoozed + settled_counts["snoozed_elsewhere"],
        # Decisions recorded outside the app that settled a card this build (todo.json, triage ledger, chat rulings).
        **{k: v for k, v in settled_counts.items() if k != "snoozed_elsewhere"},
        "answered_dropped": len(answered) + superseded,
        "empty_drafts_dropped": empty_drafts,
        # Per-workstream durable artifacts for the item header strip and the search header card.
        "workstreams": workstreams,
    }


SECTION_LABELS = {"pinned": "Pinned", "needs_you": "Needs you", "running": "Running",
                  "for_review": "For review", "past": "Past"}


def write_feed(feed: dict[str, Any], path: Path) -> Path:
    target = shared_state_file(path) if path.exists() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cos-review-feed.", dir=str(target.parent))
    with os.fdopen(fd, "w") as handle:
        json.dump(feed, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    _os_replace(tmp, target)
    return target


def render_item(item: dict[str, Any]) -> list[str]:
    flags = "".join(f for f, on in (("📌", item.get("pinned")), ("✎", item.get("draft_edited_at"))) if on)
    head = f"- [{item['updated_label']}] {flags + ' ' if flags else ''}{item['title']}  ({item['workstream']}, {item['id']})"
    if item.get("acted"):
        head += f"  [{item['acted']}]"
    lines = [head]
    if item["section"] == "running":
        lines.append(f"    {item.get('agent_state', '?')} {item.get('elapsed_label', '')}  {item.get('status_line', '')}".rstrip())
        return lines
    if item["summary"]:
        lines.append(f"    {item['summary']}")
    for option in item["options"]:
        reply = option.get("reply") if isinstance(option, dict) else ""
        lines.append(f"    - {option_label(option)}" + (f"  → {reply}" if reply else ""))
    if item["links"]:
        lines.append("    links: " + ", ".join(f"{l['label']} {l['url']}" for l in item["links"]))
    if item["reply_text"]:
        lines.append(f"    reply ({item['reply_surface']}): {item['reply_text']}")
    for entry in item.get("replies") or []:
        lines.append(f"    sent {entry.get('at', '')} [{entry.get('state', '')}]: {entry.get('text', '')}")
    return lines


def render(feed: dict[str, Any]) -> str:
    lines = [f"CoS Review feed, {feed['generated_label']}, last {feed['window_hours']} h"]
    for section in feed["sections"]:
        name = section["id"]
        lines.append(f"\n== {section['label']} ({section['count']}) ==")
        if name == "past":
            for day in feed["past_days"]:
                lines.append(f"-- {day['label']} --")
                for item in day["items"]:
                    lines.extend(render_item(item))
            continue
        for item in feed[name]:
            lines.extend(render_item(item))
    lines.append(f"\nSnoozed: {feed['snoozed_count']}; answered drafts dropped: {feed['answered_dropped']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--print", action="store_true", help="print a text rendering after writing")
    parser.add_argument("--dry-run", action="store_true", help="build and print, write nothing")
    args = parser.parse_args(argv)
    root = repo_root()
    feed = build_feed(root, window_hours=args.window_hours)
    if not args.dry_run:
        target = write_feed(feed, feed_path(root))
        print(f"wrote {target}: " + " ".join(f"{s['id']}={s['count']}" for s in feed["sections"]))
    if args.print or args.dry_run:
        print(render(feed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
