#!/usr/bin/env python3
"""Deterministic review-surface gate.

Local Markdown is the default review surface. The gate accepts a Markdown
note inside the pm-os Obsidian vault as the review pointer and normalises it
to an ``obsidian://`` URI so the rendered link opens in Obsidian and never
exposes a filesystem path. Google Docs / hosted page URLs stay accepted for
the cases where the owner explicitly asks for that surface.

Accepted review pointers
  * Markdown note inside the vault: repo-relative path, absolute path,
    ``~/``-prefixed path, or ``obsidian://open?vault=<name>&file=<path>``.
    The note must exist. ``review_url`` becomes the obsidian:// URI.
  * Approved live URL (Google Docs/Sheets/Slides, Drive, a hosted page).
    ``review_url`` is the URL unchanged.

Rejected review pointers
  * A ``.md`` that does not exist, or lives outside the vault root (for
    example ``/tmp/x.md``) — the owner cannot open it from Obsidian.
  * A workstream/output directory path that is not a note.
  * Any other URL.

Scope rules (what the gate does NOT block): code diffs, logs, tests, raw source
files, internal state, and Markdown that is not being presented for the owner's
review. Intent is inferred only from explicit signals — a review status, the
``--review-artifact`` flag, ``--needs-ruling``, or a phrase that ties the owner to a
review/approval/sign-off action. A "done" summary that merely mentions a
``.md`` backing note passes untouched. Review intent with no pointer at all
still passes (legacy chat-only review).

This module is the single enforcement point shared by the completion contract
(`emit-workspace-update.py --review-artifact [POINTER]`) and the report
boundary (`report-to-cos.sh`). Both call `evaluate()` and fail closed (exit 2,
no state write) on rejection.

Vault root: ``$PM_OS_VAULT_ROOT`` when set, else the main checkout of the repo
that contains this file (``system/scripts/review_surface.py`` -> two parents
up, then ``git rev-parse --git-common-dir`` so a copy running inside a task
worktree still names main). Vault name: ``$PM_OS_VAULT_NAME`` (default
``pm-os``).

Task worktrees: a worker writes its review note
in its own worktree ``<vault>/.amp/worktrees/<task-id>`` and reports before
``dispatch.py finish`` lands the branch, so right then the note exists only in
the worktree. The gate therefore looks for the note in the vault root first and
then in the calling task's worktree, found from (in order) the ``--task-id``
passed by the boundary, ``$PM_OS_WORKER_TASK_ID`` / ``$PM_OS_WORKTREE`` set by
``dispatch.py launch``, this file's own checkout when it is a worktree, and
``git rev-parse --show-toplevel`` of the cwd when that is a worktree of the
vault. The returned path is relative to whichever root matched, so the
``obsidian://`` URI stays vault-relative and is valid once the branch lands. A
note that exists in neither place, or only in another task's worktree, is still
rejected.

CLI:
    review_surface.py check --status STATUS [--review-artifact [POINTER]]
                             [--needs-ruling] [--task-id ID]
                             --text TEXT [--text TEXT ...]
    exit 0  -> ok (stdout: verdict JSON; `review_url` set when found)
    exit 2  -> rejected (stdout: verdict JSON; stderr: actionable guidance)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_review_asks  # noqa: E402
import named_refs  # noqa: E402
import pm_os_agent  # noqa: E402
import repo_paths  # noqa: E402

REVIEW_STATUSES = {"to review", "needs review"}

DEFAULT_VAULT_NAME = "pm-os"

# Approved live review surfaces (explicit-request path). Order is cosmetic.
APPROVED_URL_PATTERNS = [
    re.compile(r"https://docs\.google\.com/(?:document|spreadsheets|presentation)/d/[A-Za-z0-9_-]+[^\s)>\]\"']*"),
    re.compile(r"https://drive\.google\.com/(?:file/d/|drive/(?:u/\d+/)?folders/)[A-Za-z0-9_-]+[^\s)>\]\"']*"),
    re.compile(r"https://pages\.example\.com/[A-Za-z0-9_.-]+[^\s)>\]\"']*"),
]

_ANY_URL = re.compile(r"(?:https?|(?:pmos-)?obsidian)://[^\s)>\]\"']+")
_OBSIDIAN_URI = re.compile(r"obsidian://open\?[^\s)>\]\"']+")

# Local review-pointer shapes: Markdown files anywhere, or paths rooted in the
# home dir / PM OS content directories. Code, logs, state and other extensions
# are deliberately not matched.
_LOCAL_MD = re.compile(r"(?<![A-Za-z0-9_./-])(?:~/|/Users/[^\s]*?/|\./)?[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.(?:md|markdown)\b")
_LOCAL_DIR = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:~/|/Users/[A-Za-z0-9_.-]+/(?:Development/pm-os/)?)?"
    r"(?:workstreams|outputs|notes|projects|knowledge|docs|triage-briefs)/[A-Za-z0-9_./-]+"
)

# Explicit review intent tying the owner to a review/approval action. Kept narrow on
# purpose: plain "reviewed" in a done summary must not trigger the gate.
_INTENT_PATTERNS = [
    re.compile(r"\b(?:owner|{{OWNER_FIRST}})(?:'s)?\b[^.\n]{0,80}\b(?:review|approv\w*|sign-?off|feedback|read-?through)\b", re.I),
    re.compile(r"\b(?:review|approv\w*|sign-?off|feedback|read-?through)\b[^.\n]{0,80}\b(?:owner|{{OWNER_FIRST}})\b", re.I),
    re.compile(r"\b(?:awaiting|pending|needs|ready for|for)\s+(?:(?:the\s+)?(?:owner|{{OWNER_FIRST}})'?s?\s+)?(?:review|approval|sign-?off)\b", re.I),
    re.compile(r"\b(?:review|approve)\s+(?:the\s+)?(?:draft|doc|brief|plan|deliverable|artifact|note)\b", re.I),
]


@dataclass
class Verdict:
    ok: bool
    review_url: str | None = None
    review_intent: bool = False
    offending: list[str] = field(default_factory=list)
    reason: str = ""
    guidance: str = ""
    # Filing promises with no record URL in the same text (external-replies
    # Rule 9). A warning the boundaries surface, never a reason to reject.
    filing_unbacked: list[str] = field(default_factory=list)
    # Options/Lean segment missing from a blocked or needs-ruling report.
    # Names what is missing ("Options", "Lean");
    # a warning the boundaries surface, never a reason to reject.
    options_missing: list[str] = field(default_factory=list)
    # A done report that leaves work open in prose ("reload plugins after
    # landing", "checkpoint remains") with no "Follow-up:" line. The phrases
    # found; a warning the boundaries surface, never a reason to reject.
    followup_missing: list[str] = field(default_factory=list)
    # A report that talks about images (a screenshot, contact sheet, cover
    # option, crop preview) with no link to any image file (external-replies
    # Rule 14). The phrases found; a warning the
    # boundaries surface, never a reason to reject.
    image_unlinked: list[str] = field(default_factory=list)
    # A report that names two or more images with no link to one comparison
    # page. The image names; a
    # warning the boundaries surface (report-to-cos.sh builds the page first
    # with image_compare.py), never a reason to reject.
    compare_missing: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# --- file before you promise -------------------
#
# A report (prose or the fenced draft it
# carries) that says PM OS is filing, tracking, or logging a record must carry
# that record's URL. This is the deterministic backstop: it names the promise
# phrases found when no record URL is present. Bare "Linear" or "ticket" is not
# a promise (pm-os-maintenance reports mention Linear tooling all the time);
# the phrases below are the shapes that commit to a filing.

_DET = r"(?:a|an|the|one|two|three|four|five|\d+|some|these|those|both|another|new|each|all|this|it|them|two more)"
_RECORD_NOUN = r"(?:ticket|issue|bug|record|story|task)s?"
_FILED_NOUN = r"(?:ticket|issue|bug|record|story|task|thing|item|request|report)s?"
FILING_PROMISE_PATTERNS = [
    # "filing two things", "filing with the platform team", "filing a ticket", "we're filing".
    # Only "filing" used as a verb: "the line-by-line filing check" and "patent filing" name a thing
    # and commit nobody to anything.
    re.compile(
        rf"(?:\b(?:am|is|are|be|been|was|were|keep|start|started|now)\s+|\b(?:i|we|they)['’](?:m|re)\s+)filing\b"
        # a lookahead: the matched (and reported) phrase stays the bare word "filing"
        rf"|\bfiling(?=\s+(?:{_DET}\b|with\b|in\b|into\b|to\b|on\b|against\b|under\b|for\b|now\b|today\b|tomorrow\b))",
        re.I,
    ),
    # "file a ticket", "filed two issues", "file two things with the team"
    # ("the file a worker writes" has no record noun within reach: not a promise)
    re.compile(rf"\bfiled?\s+{_DET}\s+(?:\w+\s+){{0,2}}?{_FILED_NOUN}\b", re.I),
    # "Linear issue", "Jira ticket", "Tracker record", "GitHub issue"
    re.compile(rf"\b(?:linear|jira|tracker|github)\s+{_RECORD_NOUN}\b", re.I),
    # "created two issues", "logging a bug", "opened Jira tickets": a past or -ing form is a verb.
    # ("track" is left to the last pattern: "the PR tracks the issue" is prose)
    re.compile(
        rf"\b(?:open(?:ed|ing)|creat(?:ed|ing)|log(?:ged|ging)|rais(?:ed|ing))"
        rf"\s+(?:{_DET}\s+)?(?:\w+\s+){{0,2}}?{_RECORD_NOUN}\b",
        re.I,
    ),
    # "open a ticket", "raise an issue", "log a bug": the bare and -s forms double as nouns and
    # adjectives ("the run log records the source", "three open issues"), so they promise a record only with a determiner before the noun.
    re.compile(
        rf"\b(?:opens?|creates?|logs?|raises?)\s+{_DET}\s+(?:\w+\s+){{0,2}}?{_RECORD_NOUN}\b",
        re.I,
    ),
    # "tracked in Linear", "logged with Jira", "filed to Tracker"
    re.compile(
        r"\b(?:tracked|tracking|logged|logging|filed|filing|opened|captured|recorded)\s+(?:\w+\s+){0,4}?"
        r"(?:in|with|on|to|into)\s+(?:linear|jira|tracker)\b",
        re.I,
    ),
]
# A created record shows as its URL. Ids alone (EX-123) are not enough: Rule 9
# asks for id and URL verbatim, and an id with no link is what a promise
# without a record looks like.
RECORD_URL_PATTERNS = [
    re.compile(r"linear\.app/", re.I),
    re.compile(r"/browse/[A-Z][A-Z0-9]{1,9}-\d+"),
    re.compile(r"atlassian\.net/", re.I),
    re.compile(r"tracker\.example\.com/", re.I),
    re.compile(r"airtable\.com/", re.I),
    re.compile(r"github\.com/[^\s/]+/[^\s/]+/issues/\d+", re.I),
]

FILING_WARNING = "⚠️ Filing promised, no record URL"

# "File" and "filing" also mean paperwork: "we're filing the invoice", "filing the
# receipts into the documents folder". A sentence with one of these words and no tracker word promises
# no record. A tracker word wins, so "filing a Linear ticket about the receipts folder" stays flagged.
_FILE_WORD = re.compile(r"\bfil(?:e|ed|es|ing)\b", re.I)
_DOCUMENT_FILING_CONTEXT = re.compile(
    r"\b(?:tax|taxes|irs|cpa|accountant|preparer|1040|1099|w-?2|return|returns|amended"
    r"|extension|10-[kq]|sec|court|patent|trademark"
    r"|folder|folders|dropbox|downloads|drive|obsidian|vault|pdf|pdfs|document|documents|paperwork"
    r"|statement|statements|receipt|receipts|invoice|invoices|pay\s?stub|pay\s?stubs|stub|stubs"
    # "filing the stub" and "filing the file". The matched verb's
    # own "file" is blanked first, so "we'll file two things" still reads as a promise.
    r"|file|files|form|forms|scan|scans|letter|letters)\b",
    re.I,
)
_TRACKER_CONTEXT = re.compile(r"\b(?:linear|jira|tracker|tracker|github|airtable|ticket|tickets|issue|issues|bug|bugs|team)\b", re.I)

# Logging an item in a local Markdown note is not filing a record:
# "Both are logged as issues 21 and 22 in the Windows issue log" and "The issue log gains issue 14"
# name numbered entries in a PM OS note. A sentence that names such a note and no tracker by name
# promises nothing. A named tracker wins, so "logged a Linear issue and noted it in the issue log"
# stays flagged.
# Only the matched verb's own object counts: right after the match, optional entry numbers,
# then "in/to/into <a note or log>" or a .md file. A note elsewhere in the sentence ("file a
# ticket ... and note it in CONTEXT.md") does not excuse the promise.
# Between the numbers and the note, a companion entry in the same note may sit: "Logged as issue 30 with Mac kit item K18 in .../windows-cos-issues.md" and
# "(kit item K18)". It must read "item(s) <id>", so "Filed an issue with the team in the run log"
# still promises a record.
_ENTRY_IDS = r"[A-Z]{0,2}\d+(?:\s*(?:,|and|to|-)\s*[A-Z]{0,2}\d+)*"
_COMPANION_ITEM = (
    rf"(?:,?\s*(?:with|and|plus)\s+(?:(?:a|an|the)\s+)?(?:[\w-]+\s+){{0,2}}?items?\s+{_ENTRY_IDS}\s*,?\s*"
    rf"|\(\s*(?:[\w-]+\s+){{0,2}}?items?\s+{_ENTRY_IDS}\s*\)\s*)?"
)
_LOCAL_LOG_OBJECT = re.compile(
    r"\s*(?:\d+(?:\s*(?:,|and|to|-)\s*\d+)*\s*)?"
    + _COMPANION_ITEM
    + r"(?:in|to|into|on)\s+(?:(?:the|this|that|its|my|our|your)\s+(?:[\w-]+\s+){0,3}?(?:note|notes|log|to-?do\s+list)\b"
    r"|\S+\.md\b)",
    re.I,
)
_TRACKER_ID_NOT = {"UTF", "SHA", "ISO", "RFC", "MD", "AES", "RSA", "TLS", "SSL", "HTTP", "COVID", "MP", "W", "K"}
_NAMED_TRACKER = re.compile(r"(?i:\b(?:linear|jira|tracker|github|airtable|gh\s+issue)\b)|\b([A-Z][A-Z0-9]{1,9})-\d+\b")
# "a file this task did not touch": "file" after an article or possessive is the noun.
_NOUN_BEFORE = re.compile(
    r"\b(?:a|an|the|this|that|each|every|one|any|no|which|whose|its|his|her|our|your|their|my|same|other|new|old)\s*$",
    re.I,
)
# The check's own warning, appended to a stored report, is not a promise ("create the record via
# pmos"): a report read a second time (result cards, reconcile) re-flagged itself.
_OWN_WARNING = re.compile(re.escape("⚠️ Filing promised, no record URL") + r".*?\(external-replies Rule 9\)\.?", re.S)


# "Logged as issue 32" with a bare number is a numbered entry in a note, never a tracker record
# (those read EX-32 or carry a URL), so the note may sit later in the sentence: "Logged as issue 32 for a numbered entry in the local file .../windows-cos-issues.md".
# Only "logged as issue/entry/item N" qualifies: opening "issue 412" or creating "ticket 4821" is a
# tracker act. The note must be the object of in/into/to/on, never a bare mention, and a named
# tracker or a recipient (a repo, team, board, queue, IT) keeps the promise.
_LOGGED_AS_ENTRY = re.compile(r"\blog(?:ged|ging|s)?\s+as\s+(?:an?\s+)?(?:issue|entry|item)s?$", re.I)
_ENTRY_NUMBER_AFTER = re.compile(r"\s+\d+\b")
_LOCAL_NOTE_IN_SENTENCE = re.compile(
    r"\b(?:in|into|to|on)\s+\[?(?:(?:the|this|that|its|our|my)\s+)?(?:[\w-]+\s+){0,3}?"
    r"(?:\S+\.md\b|(?:note|notes|log)\b)",
    re.I,
)
_RECIPIENT = re.compile(r"\b(?:[Rr]epos?|[Rr]epository|[Tt]eams?|[Bb]oard|[Qq]ueue|IT)\b")


def _is_local_log(text: str, match: re.Match[str]) -> bool:
    if _LOGGED_AS_ENTRY.search(match.group(0)) and _ENTRY_NUMBER_AFTER.match(text, match.end()):
        sentence = _sentence_around(text, match.start(), match.end())
        if (_LOCAL_NOTE_IN_SENTENCE.search(sentence) and not _names_tracker(sentence)
                and not _RECIPIENT.search(sentence)):
            return True
    if not _LOCAL_LOG_OBJECT.match(text, match.end()):
        return False
    return not _names_tracker(_sentence_around(text, match.start(), match.end()))


def _names_tracker(sentence: str) -> bool:
    return any(m.group(1) is None or m.group(1).upper() not in _TRACKER_ID_NOT
               for m in _NAMED_TRACKER.finditer(sentence))


def _is_noun_file(text: str, match: re.Match[str]) -> bool:
    return bool(re.match(r"file\b", match.group(0), re.I)) and bool(_NOUN_BEFORE.search(text[max(0, match.start() - 12):match.start()]))


def has_record_url(text: str) -> bool:
    return any(p.search(text) for p in RECORD_URL_PATTERNS)


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence holding a match. A period inside a word or number does not end a sentence."""
    before = [m.end() for m in re.finditer(r"[.!?]\s|\n", text[:start])]
    after = re.search(r"[.!?]\s|\n", text[end:])
    return text[before[-1] if before else 0: end + after.start() if after else len(text)]


def _is_document_filing(text: str, match: re.Match[str]) -> bool:
    if not _FILE_WORD.search(match.group(0)):
        return False
    # Blank the matched verb, so the "file" in "file two things" is not read as a document noun.
    verb_blanked = _FILE_WORD.sub(lambda m: " " * len(m.group(0)), match.group(0))
    blanked = text[:match.start()] + verb_blanked + text[match.end():]
    sentence = _sentence_around(blanked, match.start(), match.end())
    return bool(_DOCUMENT_FILING_CONTEXT.search(sentence)) and not _TRACKER_CONTEXT.search(sentence)


def filing_promises(text: str) -> list[str]:
    """The phrases in ``text`` that commit PM OS to filing a record, deduped, in order."""
    text = _OWN_WARNING.sub(" ", _LINE_MARK.sub("\n", text or ""))
    found: list[str] = []
    for pat in FILING_PROMISE_PATTERNS:
        for m in pat.finditer(text):
            if _is_document_filing(text, m) or _is_local_log(text, m) or _is_noun_file(text, m):
                continue
            phrase = " ".join(m.group(0).split())
            if phrase.lower() not in {f.lower() for f in found}:
                found.append(phrase)
    # "Opened a Jira ticket" already names "Jira ticket": keep the longer phrase.
    return [p for p in found if not any(p.lower() != q.lower() and p.lower() in q.lower() for q in found)]


def unbacked_filing_promise(text: str) -> list[str]:
    """Filing promises in ``text`` when it carries no record URL; empty otherwise.

    Prose and fenced draft blocks are both searched: the promise usually sits
    in the draft, and the ids and URLs may sit in either.
    """
    if has_record_url(text or ""):
        return []
    return filing_promises(text)


def filing_warning_text(phrases: list[str]) -> str:
    quoted = ", ".join(f"\"{p}\"" for p in phrases[:3])
    more = f" (+{len(phrases) - 3} more)" if len(phrases) > 3 else ""
    return (
        f"{FILING_WARNING} ({quoted}{more}): create the record via pmos (linear, jira, airtable) "
        f"or gh issue create and resend with its id and URL verbatim; CoS does not send this draft (external-replies Rule 9)."
    )


# --- options and lean on decision reports

# A worker that puts its options only in its note leaves the CoS Review card
# with the status defaults ("Unblock (say how) / Drop it"). A blocked or
# needs-ruling report carries the choice itself, in one parsable shape that
# `cos_review_asks.options_segment` reads for the feed and CoS alike:
#
#   Options: (a) …; (b) …; (c) … Lean: <letter> because <reason>
#
# At most three options, each one line. Missing segment or missing Lean is a
# warning in the report output, never a reason to reject.

OPTIONS_WARNING = "⚠️ Options missing"
OPTIONS_SHAPE = "Options: (a) …; (b) …; (c) … Lean: <letter> because <reason>"
_LINE_MARK_OPTIONS = re.compile(r" ?⏎ ?")


def decision_report(status: str | None, needs_ruling: bool) -> bool:
    """True when the report asks the owner to choose: status blocked, or --needs-ruling."""
    return (status or "").strip().lower() == "blocked" or bool(needs_ruling)


# System notices (capacity-, auto-push-, skill-sync-, digest-stale-, ...) carry a
# `<kind>-<YYYYmmdd>[-HHMM]` id, the same shape report_queue.NOTICE_KIND reads. They
# report machine state; no owner choice sits behind them, so the Options check never
# fires on one.
SYSTEM_NOTICE_ID = re.compile(r"^[a-z][a-z-]*?-\d{8}(?:-\d{2,6})?$")


def system_notice(task_id: str | None) -> bool:
    """True for a synthetic system-notice row id, never for an owner task (rt-...)."""
    tid = (task_id or "").strip()
    return bool(tid) and not tid.startswith("rt-") and bool(SYSTEM_NOTICE_ID.match(tid))


def missing_options(text: str, *, status: str | None = None, needs_ruling: bool = False,
                    task_id: str | None = None) -> list[str]:
    """What a decision report lacks: ``["Options", "Lean"]`` with no segment at all, ``["Lean"]`` when the
    lettered options are there without a lean, ``[]`` when complete, when the report is not a decision,
    or when it is a system notice (see `system_notice`)."""
    if not decision_report(status, needs_ruling) or system_notice(task_id):
        return []
    segment = cos_review_asks.options_segment(_LINE_MARK_OPTIONS.sub("\n", text or ""))
    if segment is None:
        return ["Options", "Lean"]
    if segment.lean is None:
        return ["Lean"]
    return []


def options_warning_text(missing: list[str]) -> str:
    what = " and ".join(missing)
    return (
        f"{OPTIONS_WARNING} ({what}): a blocked or needs-ruling report lists the ways forward as "
        f"\"{OPTIONS_SHAPE}\" (at most 3, one line each) so the CoS Review card and CoS show the same choice; "
        f"the card shows the status defaults until it does."
    )


# --- follow-up declared on done reports --------
#
# Nothing reads prose for open work, so open work left only in prose
# ("reload plugins after landing") never reaches the owner. A done report that leaves work open names it on one line:
#
#   Follow-up: <what stays open>
#
# `cos_review_feed` turns that line into a Needs You item (Dispatch the
# follow-up / Drop it). A done report whose prose reads like open work with
# no such line gets a warning in the report output, never a rejection.

FOLLOWUP_WARNING = "⚠️ Follow-up not declared"
FOLLOWUP_SHAPE = "Follow-up: <what stays open>"
OPEN_WORK = re.compile(
    r"\bafter landing\b"
    r"|\bremains?\b(?:\s+\w+){0,4}\s+(?:open|to do|to run|pending)\b|\bcheckpoint remains\b"
    r"|\bstill\s+(?:open|pending|to do|to run|needs?)\b"
    r"|\bnot yet\s+(?:done|run|reloaded|landed|closed|wired)\b"
    r"|\bleft for later\b|\bneeds? (?:a )?(?:restart|reload|closing)\b",
    re.I,
)


def missing_followup(text: str, *, status: str | None = None) -> list[str]:
    """The open-work phrases a done report carries with no ``Follow-up:`` line; ``[]`` for other statuses,
    for a report that declares its follow-up, or for one whose prose leaves nothing open."""
    if (status or "").strip().lower() != "done":
        return []
    blob = _LINE_MARK_OPTIONS.sub("\n", text or "")
    if cos_review_asks.followup_text(blob):
        return []
    found: list[str] = []
    for match in OPEN_WORK.finditer(blob):
        phrase = " ".join(match.group(0).split())
        if phrase.lower() not in {f.lower() for f in found}:
            found.append(phrase)
    return found


def followup_warning_text(found: list[str]) -> str:
    quoted = ", ".join(f"\"{f}\"" for f in found[:3])
    return (
        f"{FOLLOWUP_WARNING} ({quoted}): a done report that leaves work open names it as "
        f"\"{FOLLOWUP_SHAPE}\" so CoS Review shows it as a Needs You item; prose alone reaches nobody."
    )


# --- images are links, never descriptions ----------------
#
# Images are links the owner can view, never descriptions (external-replies Rule
# 14). Two deterministic pieces back the rule:
#   * `link_image_paths` turns every bare image path a report names (absolute,
#     `~/`, a task-worktree path, or a repo-relative `.amp/in/`, `workstreams/`,
#     `outputs/`, `projects/`, `notes/` path) into `[basename](file:///abs)`, so
#     `report_text` hoists a clickable link to each image into the suffix. A
#     worktree path is rewritten to the same path on main, where the file lands.
#   * `missing_image_links` names the image phrases in a report that carries no
#     image link at all, so the boundary warns ("⚠️ Image not linked").

IMAGE_WARNING = "⚠️ Image not linked"
IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "svg", "heic", "tif", "tiff", "bmp", "avif")
_IMAGE_EXT = r"\.(?:" + "|".join(IMAGE_EXTS) + r")"
_REL_ROOTS = r"(?:\.amp/in|workstreams|outputs|projects|notes)/"
# A path token: no whitespace, quotes, brackets or angle brackets. The
# lookbehind keeps paths that are already the target of a URL or a link out.
_IMAGE_PATH = re.compile(
    r"(?<![\w/:.\-(\[<])(?P<path>(?:~/|/|[A-Za-z]:[\\/]|" + _REL_ROOTS + r")[^\s\"'<>()\[\]`]*?" + _IMAGE_EXT + r")(?![\w/])",
    re.I,
)
# `[label](/abs/x.png)`: a markdown link whose target is a plain path.
_MD_PATH_LINK = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<path>(?:~/|/|[A-Za-z]:[\\/]|" + _REL_ROOTS + r")[^\s)]*?" + _IMAGE_EXT + r")\)",
    re.I,
)
_IMAGE_URL = re.compile(r"(?:https?|file)://[^\s<>)\]\"']+?" + _IMAGE_EXT + r"(?![\w])", re.I)
# Hosted image surfaces count as a link to the image too.
_IMAGE_HOSTS = re.compile(
    r"https?://(?:[\w.-]*\.)?(?:figma\.com|drive\.google\.com|docs\.google\.com/presentation|pages\.example\.com|"
    r"user-attachments|githubusercontent\.com)[^\s<>)\]\"']*",
    re.I,
)
IMAGE_MENTION = re.compile(
    r"\bscreenshots?\b|\bcontact sheets?\b|\bcover (?:options?|images?|art)\b|\bcrop (?:previews?|options?)\b"
    r"|\bthumbnails?\b|\bposters?\b|\bmock-?ups?\b|\bimage (?:options?|previews?|candidates?)\b"
    r"|\b(?:generated|exported|rendered) (?:images?|gifs?|posters?|assets?|frames?)\b|\bgifs?\b|\bheadshots?\b",
    re.I,
)
# A mention is an explicit image reference only when it points at particular
# images: a determiner, number, or possessive before it ("the contact sheet",
# "two screenshots", "our cover options"), or a label or locator after it
# ("cover option 1", "screenshot at", "cover options described below"). A bare
# category word inside rule text ("options, variants, before and after,
# screenshots, even one") is not a reference.
_MENTION_BEFORE = re.compile(
    r"\b(?:the|this|these|that|those|a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+|both|each|"
    r"new|attached|final|our|my|your|its|his|her|their)\s+(?:[a-z][\w-]*\s+)?$",
    re.I,
)
_MENTION_AFTER = re.compile(
    r"^\s*(?:#?\d+\b|[A-Z]\b|(?:of|at|in|on|attached|below|above|described|shows?|showing)\b)"
)
# The first item of a category list ("a screenshot, contact sheet, or cover
# option") is rule text too: another image word follows after a separator.
_MENTION_LIST = re.compile(r"^\s*(?:,|/|\bor\b|\band\b)\s*(?:(?:or|and)\s+)?(?:an?\s+)?(?:" + IMAGE_MENTION.pattern + r")", re.I)
# The placeholder target the rule text uses as an example (`file:///abs/x.png`,
# bare `file:///abs/` or `file:///abs`, or a plain `/abs/x.png`). It is never a
# real link, so `report_text` never hoists it and it never counts as an image
# link or a compare-page image.
_EXAMPLE_FILE_URL = re.compile(r"^file:///abs(?:[/).,;:\]]|$)", re.I)
_EXAMPLE_FILE_ANY = re.compile(r"file:///abs(?:/[^\s)\]]*)?(?![\w-])", re.I)
# Quoted text is an example or a quotation, never this report's own image
# reference: straight or curly double quotes and backtick spans.
_QUOTED_SPAN = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|`[^`\n]*`")


def _is_example_path(path: str) -> bool:
    return path.startswith("/abs/")


# An image word used as the name of a program, not a picture: "The poster and report-to-cos.sh
# now clear a lock folder" means the next-up digest poster script. The word is the
# software sense when a tool noun follows it ("the screenshot script", "the thumbnail job"), when
# it is paired with a script file ("the poster and report-to-cos.sh"), or, for "poster", which
# also names whatever posts, when a program word comes before it ("the digest poster", "the
# original poster") or a program verb follows it ("the poster ticks", "the poster now clears").
# Picture verbs ("the poster shows", "two posters attached") still read as images.
_CODE_FILE = r"`?[\w./\\-]+\.(?:sh|py|ps1|ts|js|mjs|cjs|bash|zsh)\b`?"
# A tool noun with a label after it ("the screenshot step 3") is still a picture.
_SOFTWARE_AFTER = re.compile(
    r"^\s+(?:script|scripts|job|jobs|loop|loops|daemon|daemons|process|processes|lock|locks|tick|ticks|cron|hook|hooks"
    r"|bot|bots|service|services|cli|tool|tools|step|steps|module|modules|routine|routines|worker|workers)\b(?!\s+#?\d)",
    re.I,
)
_CODE_FILE_PAIR = re.compile(rf"^\s*(?:,|/|\band\b|\bor\b|\bplus\b)\s*(?:the\s+)?{_CODE_FILE}", re.I)
# A picture modifier right before "poster" keeps it an image ("the final poster now ...").
_PICTURE_BEFORE = re.compile(
    r"\b(?:attached|new|final|draft|printed|printable|launch|event|movie|film|campaign|\d+|two|three|four|five|six)\s+$",
    re.I,
)
_AGENT_WORD = re.compile(r"\bposters?\b", re.I)
_AGENT_BEFORE = re.compile(
    r"\b(?:digest|next-?up|autopost|auto-post|slack|message|comment|reply|original|forum|thread|cron|job|bot|launchd"
    r"|scheduled|background|command-center|loop|script)\s+$",
    re.I,
)
_AGENT_AFTER = re.compile(
    r"^\s+(?:(?:now|also|then|still|only|already|never|no longer)\s+)?"
    r"(?:posts?|posted|posting|ticks?|ticked|clears?|cleared|stops?|stopped|retries|retried|sends?|sent"
    r"|fires?|fired|locks?|locked|skips?|skipped|exits?|exited|crash(?:es|ed)?|hangs?|hung|reclaims?|reclaimed"
    r"|removes?|removed|waits?|waited|delivers?|delivered)\b",
    re.I,
)


def _software_sense(blob: str, match: re.Match) -> bool:
    before, after = blob[max(0, match.start() - 40):match.start()], blob[match.end():match.end() + 80]
    if _SOFTWARE_AFTER.match(after):
        return True
    if not _AGENT_WORD.fullmatch(match.group(0)) or _PICTURE_BEFORE.search(before):
        return False
    return bool(_AGENT_BEFORE.search(before) or _AGENT_AFTER.match(after) or _CODE_FILE_PAIR.match(after))


def _explicit_image_mention(blob: str, match: re.Match) -> bool:
    before, after = blob[max(0, match.start() - 40):match.start()], blob[match.end():match.end() + 60]
    if _MENTION_LIST.match(after) or _software_sense(blob, match):
        return False
    return bool(_MENTION_BEFORE.search(before) or _MENTION_AFTER.match(after))


# Links that resolve when the owner clicks them: open the HTML file itself, never a folder
# link. A page may exist only in an unlanded worktree, not yet on main. Two modes:
#   * "landing": the report lands the branch before delivery (report-to-cos.sh
#     --status done), so a worktree path names the same path on main.
#   * "resolve" (default): the copy that exists now. Main when the file is on
#     main; else the path as given when it exists (a live worktree); else the
#     same relative path in a live task worktree (this task's first, then the
#     newest). A parked turn, a digest re-render, and a CoS reply all use it.
LINK_MODES = ("resolve", "landing")


def _main_equivalent(expanded: Path, root: Path, trees: Path) -> Path:
    if _under(expanded, trees):
        parts = expanded.relative_to(trees).parts
        if len(parts) > 1:
            return root.joinpath(*parts[1:])
    return expanded


def resolve_local_path(path: str | Path, mode: str = "resolve", task_id: str | None = None) -> Path:
    """The local file a link should name. ``~`` expands; a repo-relative path
    resolves against main; see ``LINK_MODES`` for the worktree rules."""
    expanded = Path(path).expanduser()
    root = vault_root()
    if not expanded.is_absolute():
        expanded = root / expanded
    expanded = Path(os.path.abspath(expanded))
    trees = worktrees_dir(root)
    on_main = _main_equivalent(expanded, root, trees)
    if mode == "landing" or on_main.exists():
        return on_main
    if expanded.exists():
        return expanded
    if not _under(on_main, root) or _under(on_main, trees):
        return on_main
    rel = on_main.relative_to(root)
    try:
        live = sorted((d for d in trees.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        live = []
    for tree in [*worktree_roots(task_id), *live]:
        if (tree / rel).exists():
            return tree / rel
    return on_main


def _file_url(path: Path) -> str:
    """file:// URL; a Windows drive path is file:///C:/..., colon kept."""
    posix = path.as_posix()
    if re.match(r"^[A-Za-z]:/", posix):
        return "file:///" + quote(posix, safe="/:")
    return "file://" + quote(posix, safe="/")


def _url_path(raw: str) -> str:
    """Local path from a URL path: /C:/x is C:/x on Windows."""
    return raw[1:] if re.match(r"^/[A-Za-z]:/", raw) else raw


def local_file_url(path: str | Path, mode: str = "resolve", task_id: str | None = None) -> str:
    return _file_url(resolve_local_path(path, mode, task_id))


def image_file_url(path: str, mode: str = "resolve", task_id: str | None = None) -> str:
    """``file://`` URL for an image or page path (``resolve_local_path``)."""
    return local_file_url(path, mode, task_id)


_FILE_URL = re.compile(r"file://(?P<path>/[^\s<>)\]\"'`]+)")


def localize_file_urls(text: str, mode: str = "resolve", task_id: str | None = None) -> str:
    """Point every ``file://`` URL inside the vault or its worktrees at the copy
    ``resolve_local_path`` picks, so a shared link opens.
    Paths outside the vault and the rule's example target stay as written."""
    root = vault_root()

    def fix(match: re.Match) -> str:
        raw = match.group("path")
        trail = raw[len(raw.rstrip(".,;:")):]
        path = _url_path(unquote(raw[:len(raw) - len(trail)]))
        if _is_example_path(path) or path == "/abs" or not _under(Path(path), root):
            return match.group(0)
        return local_file_url(path, mode, task_id) + trail

    return _FILE_URL.sub(fix, text or "")


def link_image_paths(text: str, mode: str = "resolve", task_id: str | None = None) -> str:
    """Rewrite bare image paths and path-target markdown links as clickable
    ``[basename](file:///abs)`` links. URLs and existing URL links are left alone."""
    url = lambda p: image_file_url(p, mode, task_id)  # noqa: E731
    text = _MD_PATH_LINK.sub(lambda m: m.group(0) if _is_example_path(m.group("path")) else
                             f"[{m.group('label')}]({url(m.group('path'))})", text)
    return _IMAGE_PATH.sub(lambda m: m.group(0) if _is_example_path(m.group("path")) else
                           f"[{Path(m.group('path')).name}]({url(m.group('path'))})", text)


def has_image_link(text: str) -> bool:
    blob = _EXAMPLE_FILE_ANY.sub(" ", text or "")
    for pattern in (_MD_PATH_LINK, _IMAGE_PATH):
        blob = pattern.sub(lambda m: " " if _is_example_path(m.group("path")) else m.group(0), blob)
    return bool(_IMAGE_URL.search(blob) or _IMAGE_PATH.search(blob) or _MD_PATH_LINK.search(blob)
                or _IMAGE_HOSTS.search(blob) or has_compare_link(blob))


def missing_image_links(text: str) -> list[str]:
    """Explicit image references in a report that links no image at all; ``[]``
    when the report names no image or carries at least one image link or path.
    Drafts (fenced blocks) are skipped: a draft's words are the recipient's
    message. A bare category word in rule text is not a reference
    (``_explicit_image_mention``), and neither is a quoted one (``_QUOTED_SPAN``)."""
    blob = _FENCED_BLOCK.sub(" ", _LINE_MARK.sub("\n", text or ""))
    if has_image_link(blob):
        return []
    blob = _QUOTED_SPAN.sub(" ", blob)
    found: list[str] = []
    for match in IMAGE_MENTION.finditer(blob):
        if not _explicit_image_mention(blob, match):
            continue
        phrase = " ".join(match.group(0).split())
        if phrase.lower() not in {f.lower() for f in found}:
            found.append(phrase)
    return found


def image_warning_text(found: list[str]) -> str:
    quoted = ", ".join(f"\"{f}\"" for f in found[:3])
    return (
        f"{IMAGE_WARNING} ({quoted}): give the owner a clickable file:// link to each image file "
        f"instead of describing it (external-replies Rule 14)."
    )


# --- one compare page for every set of images -------------
#
# Every set of shared images goes on one page so the owner can compare easily
# (external-replies Rule 14 item 2). `image_compare.py` writes the page; these
# helpers let `report_text` and `report-to-cos.sh` see it:
#   * a compare page is an `.html` file whose name or folder says "compare"
#     (`workstreams/<slug>/assets/compare/<date>-<slug>.html`); its bare path
#     becomes a `file://` link on main like an image path, and its link leads
#     the suffix ahead of the per-image links;
#   * `report_image_refs` lists the local images a report names, so the
#     boundary can build a page from them, and `missing_compare_page` names
#     them when two or more have no compare link ("⚠️ No compare page").

COMPARE_WARNING = "⚠️ No compare page"
COMPARE_MIN_IMAGES = 2
# A local review page: an `.html` file whose name says "compare", or that sits
# in an `assets/`, `artifacts/`, or `reviews/` folder, such as
# `workstreams/example-event/assets/page/applied-verification.html` named as a
# bare path: such a page is the compare page and becomes a link.
_PSEG = r"[^\s\"'<>()\[\]`]"
_COMPARE_NAME = (r"(?:" + _PSEG + r"*compare" + _PSEG + r"*?|(?:" + _PSEG + r"*?[\\/]|(?<=[\\/]))"
                 r"(?:assets|artifacts|reviews)[\\/]" + _PSEG + r"*?)\.html?")
# "I opened it on your Mac" is the page shared too (Rule 14 item 3).
_OPENED_PAGE = re.compile(
    r"\bopened (?:it|them|the (?:[\w-]+ )?(?:page|html(?: file)?)|\S+\.html?)\b[^.\n]{0,60}?\bon (?:your|(?:the )?owner'?s|the) mac\b",
    re.I)
_COMPARE_PATH = re.compile(
    r"(?<![\w/:.\-(\[<])(?P<path>(?:~/|/|[A-Za-z]:[\\/]|" + _REL_ROOTS + r")" + _COMPARE_NAME + r")(?![\w/])", re.I)
_MD_COMPARE_PATH_LINK = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<path>(?:~/|/|[A-Za-z]:[\\/]|" + _REL_ROOTS + r")" + _COMPARE_NAME + r")\)", re.I)
# Local pages only: a hosted page whose name says "compare" (a pricing
# comparison on hosted page) is not the page for this report's images.
_COMPARE_URL = re.compile(r"file://" + _COMPARE_NAME + r"(?![\w])", re.I)
_LOCAL_IMAGE_URL = re.compile(r"file://(?P<path>[^\s<>)\]\"']+?" + _IMAGE_EXT + r")(?![\w])", re.I)
_MD_ANY_LINK = re.compile(r"\[(?P<label>[^\]\n]+)\]\((?P<target>[^\s)]+)\)")


def is_compare_url(url: str) -> bool:
    return bool(_COMPARE_URL.fullmatch((url or "").rstrip(".,;:")))


def link_compare_paths(text: str, mode: str = "resolve", task_id: str | None = None) -> str:
    """Bare compare-page paths and path-target links become ``file://`` links
    (``resolve_local_path``: main once landed, the live copy before)."""
    url = lambda p: image_file_url(p, mode, task_id)  # noqa: E731
    text = _MD_COMPARE_PATH_LINK.sub(lambda m: f"[{m.group('label')}]({url(m.group('path'))})", text)
    return _COMPARE_PATH.sub(lambda m: f"[{Path(m.group('path')).name}]({url(m.group('path'))})", text)


def has_compare_link(text: str) -> bool:
    blob = _FENCED_BLOCK.sub(" ", _LINE_MARK.sub("\n", text or ""))
    return bool(_COMPARE_URL.search(blob) or _COMPARE_PATH.search(blob) or _MD_COMPARE_PATH_LINK.search(blob)
                or _OPENED_PAGE.search(blob))


def report_image_refs(text: str) -> list[tuple[str, str]]:
    """The local images a report names, in order, as ``(path, label)``: bare
    paths, path-target links, and ``file://`` image links. Drafts (fenced
    blocks) and hosted images are left out. A label that only repeats the
    file name comes back empty."""
    blob = _FENCED_BLOCK.sub(" ", _LINE_MARK.sub("\n", text or ""))
    found: dict[str, tuple[str, str]] = {}

    def add(path: str, label: str = "") -> None:
        path = unquote(path)
        if _is_example_path(path):
            return
        key = image_file_url(path)
        if key not in found or (label and not found[key][1]):
            found[key] = (path, "" if label == Path(path).name else label)

    hits: list[tuple[int, str, str]] = []
    for m in _MD_ANY_LINK.finditer(blob):
        target = m.group("target")
        if target.startswith("file://") and _LOCAL_IMAGE_URL.fullmatch(target):
            hits.append((m.start(), _LOCAL_IMAGE_URL.fullmatch(target).group("path"), m.group("label")))
        elif _MD_PATH_LINK.fullmatch(m.group(0)):
            hits.append((m.start(), target, m.group("label")))
    # Blank out what was matched with same-length runs so positions keep the report's order.
    blank = lambda m: " " * len(m.group(0))  # noqa: E731
    rest = _MD_ANY_LINK.sub(blank, blob)
    hits += [(m.start(), m.group("path"), "") for m in _LOCAL_IMAGE_URL.finditer(rest)]
    hits += [(m.start(), m.group("path"), "") for m in _IMAGE_PATH.finditer(_LOCAL_IMAGE_URL.sub(blank, rest))]
    for _start, path, label in sorted(hits):
        add(path, label)
    return list(found.values())


def missing_compare_page(text: str) -> list[str]:
    """Image names in a report that names COMPARE_MIN_IMAGES or more local
    images and links no compare page; ``[]`` otherwise."""
    refs = report_image_refs(text)
    if len(refs) < COMPARE_MIN_IMAGES or has_compare_link(text):
        return []
    return [label or Path(path).name for path, label in refs]


def compare_warning_text(found: list[str]) -> str:
    shown = ", ".join(f"\"{f}\"" for f in found[:3]) + (f" and {len(found) - 3} more" if len(found) > 3 else "")
    return (
        f"{COMPARE_WARNING} ({shown}): put every image on one page with "
        f"`python3 system/scripts/image_compare.py build` and give the owner its one link (external-replies Rule 14)."
    )


# --- vault resolution --------------------------------------------------------

_HERE = Path(__file__).resolve().parents[2]


def vault_root() -> Path:
    override = os.environ.get("PM_OS_VAULT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # A copy of this file inside a task worktree still names the main checkout.
    return repo_paths.main_checkout(_HERE).resolve()


def vault_name() -> str:
    return os.environ.get("PM_OS_VAULT_NAME", "").strip() or DEFAULT_VAULT_NAME


def worktrees_dir(root: Path | None = None) -> Path:
    override = os.environ.get("PM_OS_WORKTREES_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (root or vault_root()) / ".amp" / "worktrees"


def _git_toplevel(path: Path) -> Path | None:
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    if probe.returncode or not probe.stdout.strip():
        return None
    return Path(probe.stdout.strip()).resolve()


def worktree_roots(task_id: str | None = None) -> list[Path]:
    """Existing task worktrees of the vault that this call may read a review
    note from: the named task's, the calling worker's (``$PM_OS_WORKER_TASK_ID``,
    ``$PM_OS_WORKTREE``), this file's own checkout when it is a worktree, and
    the cwd's checkout when it is a worktree. Another task's worktree is never
    a candidate."""
    root = vault_root()
    wt_dir = worktrees_dir(root)
    candidates: list[Path] = []
    for tid in (task_id, os.environ.get("PM_OS_WORKER_TASK_ID")):
        tid = (tid or "").strip()
        if tid and "/" not in tid:
            candidates.append(wt_dir / tid)
    env_wt = os.environ.get("PM_OS_WORKTREE", "").strip()
    if env_wt:
        candidates.append(Path(env_wt).expanduser())
    candidates.append(_HERE)
    cwd_top = _git_toplevel(Path.cwd())
    if cwd_top:
        candidates.append(cwd_top)

    roots: list[Path] = []
    for cand in candidates:
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if cand == root or cand in roots or not cand.is_dir():
            continue
        # Bound to this vault: sits in its worktrees dir, or git says main is the vault.
        if cand.parent != wt_dir and repo_paths.main_checkout(cand).resolve() != root:
            continue
        roots.append(cand)
    return roots


def note_roots(task_id: str | None = None) -> list[Path]:
    """Vault root first, then the task worktrees a note may legitimately live in."""
    return [vault_root(), *worktree_roots(task_id)]


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def vault_relative_note(pointer: str, task_id: str | None = None) -> str | None:
    """Return the vault-relative path (with extension) of an existing Markdown
    note that `pointer` designates, or None when it is not a vault note.

    Accepts repo-relative paths, absolute paths, ``~/`` paths, ``./`` paths and
    ``obsidian://open?vault=…&file=…`` URIs. The note may exist in the vault
    root or in the calling task's worktree (see `worktree_roots`); the result
    is relative to whichever matched, so it is always a vault path.
    """
    raw = (pointer or "").strip().rstrip(".,;:)")
    if not raw:
        return None
    roots = note_roots(task_id)

    if raw.startswith("obsidian://"):
        parts = urlsplit(raw)
        if parts.netloc != "open":
            return None
        query = parse_qs(parts.query)
        vault = (query.get("vault") or [""])[0]
        file_param = (query.get("file") or [""])[0]
        if not file_param or (vault and vault != vault_name()):
            return None
        rel = unquote(file_param).strip("/")
        candidates = [rel] if rel.endswith((".md", ".markdown")) else [rel + ".md", rel]
        for cand in candidates:
            for root in roots:
                target = root / cand
                if target.is_file() and target.suffix in {".md", ".markdown"}:
                    return _relative_to_root(target, root)
        return None

    if not re.search(r"\.(?:md|markdown)$", raw):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        for root in roots:
            try:
                resolved = (root / path).resolve()
            except OSError:
                continue
            if resolved.is_file():
                return _relative_to_root(resolved, root)
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    # Deepest containing root wins, so a note inside the task's worktree is
    # relative to the worktree, not `.amp/worktrees/<id>/...` under the vault.
    containing = [r for r in roots if _under(resolved, r)]
    if not containing:
        return None
    root = max(containing, key=lambda r: len(r.parts))
    if root == roots[0] and (_under(resolved, worktrees_dir(root)) or repo_paths.inside_worktree(resolved)):
        return None  # another task's worktree: not this task's note, not on main
    return _relative_to_root(resolved, root)


def _relative_to_root(target: Path, root: Path) -> str | None:
    try:
        return target.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def obsidian_uri(rel_note: str) -> str:
    """obsidian://open URI for a vault-relative note path (extension dropped, as
    Obsidian's ``file`` parameter treats ``.md`` as optional)."""
    rel = rel_note[:-3] if rel_note.endswith(".md") else rel_note
    return f"obsidian://open?vault={quote(vault_name(), safe='')}&file={quote(rel, safe='/')}"


def obsidian_note_path(url: str) -> str:
    """Vault-relative ``.md`` path named by an ``obsidian://open`` URI."""
    file = (parse_qs(urlsplit(url).query).get("file") or [""])[0]  # parse_qs unquotes
    return file if file.endswith((".md", ".markdown")) else file + ".md"


def note_file_url(rel_note: str) -> str:
    """``file://`` URL of a vault note on main. The Amp TUI opens only http(s)
    links on click and hands a clicked file://
    link to ``$AMP_EDITOR``; ``amp-obsidian-editor.sh`` turns a vault note into
    ``open obsidian://...``, so this is the link the owner clicks in chat."""
    return _file_url(vault_root() / rel_note)


# The click target for a vault note in Claude Code inside cmux: the "Open in
# Obsidian" link opens the note in Obsidian without stealing focus. cmux sends a Cmd-clicked file://….md
# link to its own Markdown viewer and an obsidian:// link to NSWorkspace.open,
# which brings Obsidian to the front. A pmos-obsidian:// link goes to "PM OS
# Obsidian Link.app" (system/native/obsidian-link, installed by
# install-obsidian-link-handler.sh), a background-only handler that runs
# `open -g obsidian://open?...` when Obsidian's window is at least half visible,
# so the note loads without taking focus. When the window is covered (one
# laptop screen, everything full size), it brings Obsidian forward, because a
# background open there showed nothing.
# The Amp TUI opens only http(s) and file:// links, so an Amp surface keeps the
# file:// link (amp-obsidian-editor.sh routes it).
HANDLER_SCHEME = "pmos-obsidian"
HANDLER_APP = Path.home() / "Applications" / "PM OS Obsidian Link.app"
NOTE_LINK_MODES = ("handler", "file", "http")
# wmux makes only http(s) links clickable and opens them in its browser panel, so
# a file:// or obsidian:// link in a wmux pane is plain text. The ``http`` mode points at system/scripts/mux/note-links.py, a
# 127.0.0.1 server that opens the note in Obsidian and shows it in the panel.
NOTE_LINK_HOST = "127.0.0.1"
NOTE_LINK_DEFAULT_PORT = 7472


def note_link_mode() -> str:
    """``handler`` (pmos-obsidian://), ``http`` (note-links server) or ``file`` (file://).

    PM_OS_NOTE_LINK=handler|file|http decides when set. Otherwise wmux gets
    ``http`` while mux/note-links.py answers on 127.0.0.1, because wmux opens
    only http(s) links; a Claude surface (pm_os_agent.py) whose handler app is
    installed gets ``handler``; Amp, or a Mac without the handler, keeps
    ``file``, so a link is never dead."""
    forced = os.environ.get("PM_OS_NOTE_LINK", "").strip().lower()
    if forced in NOTE_LINK_MODES:
        return forced
    try:
        from lib.mux import backend as _mux_backend
        if _mux_backend() == "wmux" and note_link_server_up():
            return "http"
    except Exception:
        pass
    try:
        import pm_os_agent
        agent = pm_os_agent.agent()
    except Exception:
        agent = "claude"
    app = Path(os.environ.get("PM_OS_OBSIDIAN_LINK_APP") or HANDLER_APP)
    return "handler" if agent == "claude" and (app / "Contents" / "Info.plist").is_file() else "file"


def handler_url(rel_note: str) -> str:
    """``pmos-obsidian://open?...`` for a vault note: the obsidian:// URI with
    the handler scheme, so the handler only swaps the scheme back."""
    return HANDLER_SCHEME + obsidian_uri(rel_note)[len("obsidian"):]


_NOTE_LINK_UP: bool | None = None


def note_link_server_up() -> bool:
    """One short /health probe per process: an http link only when it would open."""
    global _NOTE_LINK_UP
    if _NOTE_LINK_UP is None:
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://{NOTE_LINK_HOST}:{note_link_port()}/health", timeout=0.5) as resp:
                _NOTE_LINK_UP = resp.status == 200
        except OSError:
            _NOTE_LINK_UP = False
    return _NOTE_LINK_UP


def note_link_port() -> int:
    try:
        return int(os.environ.get("PM_OS_NOTE_LINK_PORT") or NOTE_LINK_DEFAULT_PORT)
    except ValueError:
        return NOTE_LINK_DEFAULT_PORT


def http_note_url(rel_note: str) -> str:
    """``http://127.0.0.1:<port>/open?file=<note>`` served by mux/note-links.py."""
    return f"http://{NOTE_LINK_HOST}:{note_link_port()}/open?file={quote(rel_note, safe='/')}"


def http_url_note(url: str) -> str | None:
    """Vault-relative note path of an ``http_note_url`` link, else None."""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != NOTE_LINK_HOST or parts.path != "/open":
        return None
    file = (parse_qs(parts.query).get("file") or [""])[0]
    return file if file.endswith((".md", ".markdown")) else None


def note_click_url(rel_note: str) -> str:
    """The link the owner clicks for a vault note on this surface (``note_link_mode``)."""
    mode = note_link_mode()
    if mode == "handler":
        return handler_url(rel_note)
    if mode == "http":
        return http_note_url(rel_note)
    return note_file_url(rel_note)


def click_url_note(url: str) -> str | None:
    """Vault-relative note path of a clicked-link URL (``file://`` on main or
    ``pmos-obsidian://``), else None."""
    if url.startswith(HANDLER_SCHEME + "://"):
        return obsidian_note_path("obsidian" + url[len(HANDLER_SCHEME):])
    if url.startswith("http://"):
        return http_url_note(url)
    return file_url_note(url)


def file_url_note(url: str) -> str | None:
    """Vault-relative path of a ``file://`` URL to a Markdown note inside the
    vault on main, else None (a worktree copy or a file outside the vault)."""
    if not url.startswith("file://"):
        return None
    path = Path(_url_path(unquote(urlsplit(url).path)))
    if path.suffix not in (".md", ".markdown"):
        return None
    root = vault_root()
    if not _under(path, root) or _under(path, worktrees_dir(root)):
        return None
    return _relative_to_root(path, root) if path.is_absolute() else None


def review_link(url: str) -> str:
    """Render a stored pointer, without revalidating it against the filesystem.

    An Obsidian pointer renders as the note path, then one line with the
    clickable link (``note_click_url``: pmos-obsidian:// in Claude, which opens
    the note in Obsidian, in the background when its window is visible and in
    front when it is covered; file:// in the Amp TUI, which the
    AMP_EDITOR shim routes) and the ``obsidian://`` URI itself, which the CoS
    Review web app, digests and ledgers keep parsing as the canonical pointer.
    """
    if not url.startswith("obsidian://"):
        return f"[Review artifact]({url})"
    path = obsidian_note_path(url)
    return f"{path}\n[Open in Obsidian]({note_click_url(path)}) · [obsidian:// link]({url})"


# A ``` fenced block inside a report is a draft the owner must see verbatim. Fences are matched after `` ⏎ `` expansion, so a block whose lines
# arrived delimited that way is one block too, and a block whose newlines a
# shell quoting path already flattened to spaces is still lifted whole.
_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
# `` ⏎ `` markers may sit next to each other (a blank line inside a draft)
# or lose a flanking space to whitespace collapsing; each one is a newline.
_LINE_MARK = re.compile(r" ?⏎ ?")
# Labels report_text itself writes into the link suffix. A link carrying one
# of these is a hoisted link being rendered again, so it leaves no inline text
# behind (otherwise every re-render would add another "Open link" to the prose).
_SUFFIX_LABELS = frozenset({"Open link", "Open in Obsidian", "obsidian:// link", "Review artifact"})
# A URL body stops before `<`: `.../session/<id>` is a placeholder the worker
# never filled in, not a link (otherwise
# `[Open link](https://sessions.example.com/session/<id)` leaves `>)`
# in the prose). The optional trailing `<...>` is consumed with the URL so the
# whole template is rendered as plain text and never hoisted.
_URL_BODY = r"(?:https?|obsidian|pmos-obsidian|file)://[^\s<>)\]\"']+"
_PLACEHOLDER = r"<[^<>\s]*>"
_MD_LINK = re.compile(r"\[(?P<label>[^\]\n]+)\]\((?P<url>" + _URL_BODY + r"(?:" + _PLACEHOLDER + r")?[^\s)]*)\)")
_BARE_URL = re.compile(r"(?P<url>" + _URL_BODY + r"(?P<placeholder>(?:" + _PLACEHOLDER + r")?))")
_TITLE_CHARS = 60


def link_title(url: str) -> str:
    """Short inline stand-in for a bare URL the suffix now carries: the note
    path for an Obsidian pointer, host plus path (no scheme, query or
    fragment) for anything else, the middle of a long path elided."""
    if url.startswith("obsidian://"):
        return review_link(url).splitlines()[0].rsplit("/", 1)[-1]
    if url.startswith("file://"):
        # A local file (an image, an export) shows its file name, never its path.
        return unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1] or url
    parsed = named_refs.parse_pr_ref(url)
    if parsed:
        # Names, never bare numbers: a GitHub PR or issue
        # shows its title with `repo#N` after it; the bare path only when no
        # title is cached or fetchable.
        owner, repo, _kind, num = parsed
        title = named_refs.pr_title(url)
        if title:
            fragment = urlsplit(url).fragment
            kind = " comment" if re.search(r"comment|discussion|review", fragment) else ""
            return f"{title} ({named_refs.pr_label(owner, repo, num)}{kind})"
    parts = urlsplit(url)
    title = parts.netloc + parts.path.rstrip("/") + (f"#{parts.fragment}" if parts.fragment else "")
    if len(title) <= _TITLE_CHARS:
        return title
    head, tail = title.split("/", 1)[0], title.rsplit("/", 1)[-1]
    room = _TITLE_CHARS - len(head) - len("/…/")
    if len(tail) > room:
        tail = tail[: max(room - 1, 8)] + "…"
    return f"{head}/…/{tail}"


def report_text(text: str, limit: int | None = None, review_url: str = "", *,
                link_mode: str = "resolve", task_id: str | None = None) -> str:
    """Clip prose only. Keep complete link targets in a standalone suffix.

    Stored report transports use `` ⏎ `` in place of newlines. Accept both so
    repeated rendering is stable, including old bare/parenthesized pointers.

    Fenced ``` blocks are drafts for the owner: they are lifted out before link
    extraction and whitespace collapsing, never counted against ``limit``,
    and re-emitted whole (their own line breaks intact) between the clipped
    prose and the link suffix. Links inside a draft stay inside the draft.

    A hoisted link leaves its label (or, for a bare URL, ``link_title``) in
    the prose, so "Review note: ." and "verified live: ." never appear.
    A URL ending in an
    angle-bracket placeholder (``.../session/<id>``) is a template, not a
    link: it stays inline as plain text and is never hoisted.

    Local links (``link_mode``, see ``LINK_MODES``): "landing" names main for
    a report that lands its branch first; "resolve" names the copy that exists
    now, so a parked ask links the live worktree page.
    """
    text = _LINE_MARK.sub("\n", text)
    blocks = [m.group(0) for m in _FENCED_BLOCK.finditer(text)]
    text = pm_os_agent.fix_thread_links(_FENCED_BLOCK.sub(" ", text))
    # Images are links, never descriptions (external-replies Rule 14): every image path becomes a clickable file:// link first.
    text = link_compare_paths(link_image_paths(text, link_mode, task_id), link_mode, task_id)
    text = localize_file_urls(text, link_mode, task_id)
    urls: dict[str, str] = {}
    titles: list[str] = []

    def take(match: re.Match) -> str:
        raw = match.group("url")
        label = match.groupdict().get("label")
        if "<" in raw or ">" in raw or _EXAMPLE_FILE_URL.match(raw):
            # Unfilled placeholder or the rule's example target: show it, never link it.
            return label or raw.split("://", 1)[1]
        url = raw.rstrip(".,;:")
        trail = raw[len(url):]
        if url.startswith(("obsidian://", HANDLER_SCHEME + "://")) and obsidian_note_path(
                "obsidian" + url[url.index("://"):]) in (".md", ".markdown"):
            # A pointer with no note (`file=` empty, or the `file=...` placeholder
            # once its dots are stripped) is prose, never a link: it rendered as a
            # bogus `.md` artifact with an empty file= link.
            return label or raw
        note = click_url_note(url)
        if note:
            # A file:// or pmos-obsidian:// link to a vault note is the same pointer
            # as its obsidian:// URI (review_link renders both from the URI); store one.
            url = obsidian_uri(note)
        urls.setdefault(url, label or (link_title(url) if url.startswith("file://") else "Open link"))
        if label in _SUFFIX_LABELS:
            return trail
        if label:
            # A link alone on its line is a suffix line from an earlier render
            # (the ledger stores the rendered summary and digests render it
            # again); it moves back to the suffix and leaves no label inline.
            src, start, end = match.string, match.start(), match.end()
            line_start = src.rfind("\n", 0, start) + 1
            line_end = src.find("\n", end)
            if not src[line_start:start].strip() and not src[end:len(src) if line_end < 0 else line_end].strip():
                return ""
        if not label:
            titles.append(link_title(url))
        return (label or titles[-1]) + trail

    for url in set(m.group("url") for m in _MD_LINK.finditer(text) if m.group("url").startswith("obsidian://")):
        # An already rendered pointer (`path⏎[Open in Obsidian](file)·[obsidian:// link](url)`,
        # or the older `path⏎[Open in Obsidian](url)`) is the suffix form: drop its
        # path line and file link here and let the obsidian link below move back to the suffix
        # in its original order, so rendering twice is stable and leaves nothing extra inline.
        # The click link may be either form (file:// from an Amp render, pmos-obsidian://
        # from a Claude render), whatever this surface would render now.
        path = review_link(url).splitlines()[0]
        click = "|".join(re.escape(f"[Open in Obsidian]({u})") for u in (note_file_url(path), handler_url(path), http_note_url(path)))
        text = re.sub(re.escape(path) + r"\s*(?:" + click + r")\s*·\s*(?=\[obsidian:// link\]\(" + re.escape(url) + r"\))",
                      "", text)
        text = re.sub(re.escape(path) + r"\s*(?=\[Open in Obsidian\]\(" + re.escape(url) + r"\))", "", text)
    text = _MD_LINK.sub(take, text)
    text = _BARE_URL.sub(take, text)
    if review_url:
        urls.setdefault(review_url, "Review artifact")
    for url in urls:
        if url.startswith("obsidian://"):
            # Move an existing plain path into the protected suffix too, leaving the note's basename
            # inline as a hoisted link would, so "Review: <path>." never becomes "Review:.". When the basename is already inline (a bare
            # pointer beside the path), the path goes without a stand-in.
            path = review_link(url).splitlines()[0]
            title = link_title(url)
            text = text.replace(path, "" if title in text.replace(path, "") else title)
    text = re.sub(r"Open in Obsidian:\s*|\(\s*\)", "", text)
    text = re.sub(r"\s+([.,;])(?=\s|$)", r"\1", text)
    prose = re.sub(r"\s+", " ", text).strip(" ·—;,")
    if prose.strip(" .()") in titles:
        # A pointer standing alone is the suffix; no prose stand-in needed.
        prose = ""
    if limit is not None and len(prose) > limit:
        prose = prose[:max(0, limit - 1)].rstrip() + "…"
    # One compare page per set of images (external-replies Rule 14 item 2): its
    # link leads the per-image links, which stay as the secondary list.
    order = list(urls)
    compare = [u for u in order if is_compare_url(u)]
    if compare:
        images = [u for u in order if _IMAGE_URL.fullmatch(u)]
        rest = [u for u in order if u not in compare]
        at = rest.index(images[0]) if images else len(rest)
        order = rest[:at] + compare + rest[at:]
    suffix = [review_link(url) if url.startswith("obsidian://") else f"[{urls[url]}]({url})" for url in order]
    return "\n".join(part for part in [prose, *blocks, *suffix] if part)


# --- detection helpers -------------------------------------------------------

def find_review_urls(text: str) -> list[str]:
    found: list[str] = []
    for pat in APPROVED_URL_PATTERNS:
        for m in pat.finditer(text or ""):
            url = m.group(0).rstrip(".,;:")
            if url not in found:
                found.append(url)
    return found


def is_approved_url(url: str) -> bool:
    url = (url or "").strip()
    return any(pat.fullmatch(url.rstrip(".,;:")) for pat in APPROVED_URL_PATTERNS)


def find_obsidian_uris(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:") for m in _OBSIDIAN_URI.finditer(text or "")]


def find_local_review_paths(text: str) -> list[str]:
    scrubbed = _ANY_URL.sub(" ", text or "")
    found: list[str] = []
    for pat in (_LOCAL_MD, _LOCAL_DIR):
        for m in pat.finditer(scrubbed):
            tok = m.group(0).rstrip(".,;:)")
            if tok and tok not in found:
                found.append(tok)
    return found


def has_review_intent(text: str) -> bool:
    return any(p.search(text or "") for p in _INTENT_PATTERNS)


def guidance_text(offending: list[str]) -> str:
    paths = ", ".join(f"`{p}`" for p in offending[:3]) or "the pointer"
    return (
        f"Review deliverables for the owner default to a Markdown note inside the pm-os Obsidian vault "
        f"({vault_root()}), not {paths} — that pointer is not an existing note in the vault "
        "or in this task's worktree. "
        "Write the note under outputs/reviews/<date>-<slug>.md or the owning workstream folder "
        "(drafting-docs Markdown workflow), then re-run with its vault path or obsidian:// URI: "
        "emit-workspace-update.py ... --review-artifact <vault-path-or-obsidian-uri>, or include "
        "the path in the report-to-cos.sh summary. Use a Google Doc or hosted page URL only when the owner "
        "explicitly asked for that surface."
    )


# --- gate --------------------------------------------------------------------

def _classify_pointer(pointer: str, task_id: str | None = None) -> Verdict:
    p = pointer.strip()
    if is_approved_url(p):
        return Verdict(ok=True, review_url=p, review_intent=True)
    rel = vault_relative_note(p, task_id)
    if rel:
        return Verdict(ok=True, review_url=obsidian_uri(rel), review_intent=True)
    reason = "review_note_not_in_vault" if (p.startswith("obsidian://") or re.search(r"\.(?:md|markdown)$", p)) \
        else "review_artifact_not_live_surface"
    return Verdict(ok=False, review_intent=True, offending=[p], reason=reason, guidance=guidance_text([p]))


def evaluate(
    texts: list[str],
    *,
    status: str | None = None,
    pointer: str | None = None,
    review_flag: bool = False,
    needs_ruling: bool = False,
    task_id: str | None = None,
) -> Verdict:
    """Decide whether a completion/report carries an acceptable review surface.

    texts        summary / next-action / notes that may reference the artifact
    status       workspace or task status ("to review", "done", ...)
    pointer      explicit --review-artifact value (vault note path, obsidian://
                 URI, or approved live URL) when supplied
    review_flag  bare --review-artifact flag (no value)
    needs_ruling report-to-cos --needs-ruling (the owner decision requested)
    task_id      the reporting task; its worktree may hold the note before the
                 branch lands (see `worktree_roots`)

    The verdict also carries `filing_unbacked` (external-replies Rule 9): the
    filing promises in the texts when no record URL accompanies them. It never
    changes `ok`.
    """
    blob = "\n".join(t for t in texts if t)
    verdict = _evaluate_surface(
        blob,
        status=status,
        pointer=pointer,
        review_flag=review_flag,
        needs_ruling=needs_ruling,
        task_id=task_id,
    )
    verdict.filing_unbacked = unbacked_filing_promise(blob)
    verdict.options_missing = missing_options(blob, status=status, needs_ruling=needs_ruling, task_id=task_id)
    verdict.followup_missing = missing_followup(blob, status=status)
    verdict.image_unlinked = missing_image_links(blob)
    verdict.compare_missing = missing_compare_page(blob)
    return verdict


def _evaluate_surface(
    blob: str,
    *,
    status: str | None,
    pointer: str | None,
    review_flag: bool,
    needs_ruling: bool,
    task_id: str | None,
) -> Verdict:
    status_norm = (status or "").strip().lower()
    intent = (
        status_norm in REVIEW_STATUSES
        or bool(pointer)
        or review_flag
        or needs_ruling
        or has_review_intent(blob)
    )

    if pointer:
        return _classify_pointer(pointer, task_id)

    # Pointers embedded in the text: vault notes first (default surface), then
    # approved live URLs (explicit-request surface).
    for uri in find_obsidian_uris(blob):
        rel = vault_relative_note(uri, task_id)
        if rel:
            return Verdict(ok=True, review_url=obsidian_uri(rel), review_intent=intent)
    local = find_local_review_paths(blob)
    for tok in local:
        rel = vault_relative_note(tok, task_id)
        if rel:
            return Verdict(ok=True, review_url=obsidian_uri(rel), review_intent=intent)
    urls = find_review_urls(blob)
    if urls:
        return Verdict(ok=True, review_url=urls[0], review_intent=intent)

    if not intent:
        return Verdict(ok=True, review_intent=False)

    if local:
        return Verdict(
            ok=False,
            review_intent=True,
            offending=local,
            reason="review_note_not_in_vault",
            guidance=guidance_text(local),
        )

    # Review intent with no artifact pointer at all — legacy-compatible pass.
    return Verdict(ok=True, review_intent=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="evaluate a completion/report for an acceptable review surface")
    chk.add_argument("--status", default="")
    chk.add_argument("--review-artifact", nargs="?", const=True, default=None)
    chk.add_argument("--needs-ruling", action="store_true")
    chk.add_argument("--task-id", default=None, help="reporting task; its worktree may hold the note")
    chk.add_argument("--text", action="append", default=[])
    args = parser.parse_args(argv)

    pointer = args.review_artifact if isinstance(args.review_artifact, str) else None
    review_flag = args.review_artifact is True
    verdict = evaluate(
        args.text,
        status=args.status,
        pointer=pointer,
        review_flag=review_flag,
        needs_ruling=args.needs_ruling,
        task_id=args.task_id,
    )
    print(verdict.to_json())
    if verdict.filing_unbacked:
        print(
            f"[review-surface] WARNING (filing_unbacked): {filing_warning_text(verdict.filing_unbacked)}",
            file=sys.stderr,
        )
    if verdict.options_missing:
        print(
            f"[review-surface] WARNING (options_missing): {options_warning_text(verdict.options_missing)}",
            file=sys.stderr,
        )
    if verdict.followup_missing:
        print(
            f"[review-surface] WARNING (followup_missing): {followup_warning_text(verdict.followup_missing)}",
            file=sys.stderr,
        )
    if verdict.image_unlinked:
        print(
            f"[review-surface] WARNING (image_unlinked): {image_warning_text(verdict.image_unlinked)}",
            file=sys.stderr,
        )
    if verdict.compare_missing:
        print(
            f"[review-surface] WARNING (compare_missing): {compare_warning_text(verdict.compare_missing)}",
            file=sys.stderr,
        )
    if not verdict.ok:
        print(f"[review-surface] BLOCKED ({verdict.reason}): {verdict.guidance}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
