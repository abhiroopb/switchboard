#!/usr/bin/env python3
"""Turn one worker report into the asks the CoS Review app lists.

A report is prose written for CoS, not for a list. This module derives, with
no model call, one row per decision the owner can make from the list alone:

- a parked draft ("Draft for the owner to send (#chan):" + fenced block) becomes
  ``Send to #chan: <first clause of the draft>``;
- a veto or decision list ("Decisions to veto: a; b; c") becomes one row per
  item, ``Pick (a) or (b): <lead>``, ``Decide: <question>`` or
  ``<Item>: keep or veto?``, each with its own options and paste-ready reply;
- a go, a post, console approvals, a blocked or failed task become one short
  verb-first row.

Finished work gets one title that names the artifact (``later_title``), never
the first 100 characters of the report. Nothing here reads state or the
network; ``cos_review_feed.py`` supplies the text and applies the result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FENCED = re.compile(r"```(?P<body>.*?)```", re.S)
DRAFT_MARK = re.compile(r"Draft for the owner to send\b(?P<tail>[^\n`:]{0,90})(?::|(?=\n)|(?=```))")
GMAIL_DRAFT = re.compile(
    r"(?:Gmail )?draft created, not sent[^\n]{0,80}?draft (?P<draft_id>r-?\d+)[^\n]{0,80}?To (?P<name>[A-Z][a-z]+(?: [A-Z][a-z]+)?)"
    r"[^\n]{0,120}?subject \"(?P<subject>[^\"]+)\"",
    re.I,
)
UNFENCED_DRAFT = re.compile(r"\bdraft to (?P<who>[A-Z][a-z]+(?: and [A-Z][a-z]+)?)\b[^.;]{0,40}?\bnot sent\b")
SLACK_MENTION = re.compile(r"<@(U[A-Z0-9]+)>")
SLACK_LINK = re.compile(r"<(?P<url>https?://[^|>\s]+)\|(?P<label>[^>]+)>")
SLACK_BARE = re.compile(r"<https?://[^>\s]+>")
PARENS = re.compile(r"\s*\([^()]*\)")
RULING_MARK = re.compile(
    r"(?P<mark>Decisions? to veto|Decisions? for the owner|To veto|Choices? to veto|Vetoes?(?:\s+first)?|NEEDS RULING)\s*:\s*"
    r"(?P<body>.+?)(?:(?<=[a-z0-9)\"])\.\s+(?=[A-Z])|$)",
    re.S | re.I,
)
LETTER = re.compile(r"\(([a-d])\)\s*")
GO_FOR = re.compile(r"\bgo for ([^.;,:()]+)", re.I)
# "Follow-up: reload plugins in the Scheduled Jobs tab." in a done report: work the worker left open on
# purpose, which would otherwise never reach the owner. The line runs to the end of its sentence or to the
# next labelled segment (Vetoes, Options, Thread).
FOLLOWUP_LINE = re.compile(
    r"\bFollow-ups?\s*:\s*(?P<what>.+?)(?=\s*(?:\n|$)|(?<=[.;!?])\s+(?=[A-Z(\"'0-9])|\s+(?:Vetoes?|To veto|Options|Lean|Thread)\s*:)",
    re.I | re.S,
)
POST_DOC = re.compile(r"(#[\w-]+) Doc", re.I)
FEEDBACK_NUM = re.compile(r"Feedback (\d+)\s*:")
FILING_FLAG = re.compile(r"Filing promised", re.I)
QUESTION_WORDS = re.compile(r"^(?:which|whether|who|what|how|where|when)\b|\bor not\b|\?$", re.I)
NAME_STOPLIST = {"the owner", "Example", "Acme", "Page", "Slack", "Linear", "Google", "Gmail",
                 "Day", "Oct", "Phase", "Cycle"}
STATUS_PREFIX = re.compile(r"^(?:done|failed|blocked|cancelled)\s*:\s*", re.I)
STUB_WORDS = re.compile(r"\b(?:in|the|of|to|at|for|a|an|on|with|from|under|via|is|are|and|by)\s*[:;,]?\s*$", re.I)
TRAILING_VERB = re.compile(r"\s+(?:built|created|landed|done|complete|completed|finished|shipped|written)\s*$", re.I)

TITLE_SOFT = 64     # fits one line in the popover
TITLE_HARD = 110    # two lines; never cut below this with an ellipsis
ISSUE_DRAFT = re.compile(r"^\s*(?:\*\*)?Title(?:\*\*)?\s*:\s*(?P<title>[^\n]+)", re.I)
MAX_RULING_ASKS = 4
MIN_RULING_ITEM = 28
MIN_QUESTION_ITEM = 16   # "which owners get the fixes" is a decision too


@dataclass
class Lean:
    """The option a worker recommends: ``Lean: (a) because <reason>`` in a report, or the bold sentence of a
    review note's Recommendation section matched to a row of its alternatives table."""
    letter: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"letter": self.letter, "reason": self.reason}


@dataclass
class Ask:
    kind: str
    title: str
    summary: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    # Where ``options`` came from: "report" (the worker's own Options segment or decision list), "note"
    # (the review note's Recommendation and alternatives table, filled in by the feed) or "default" (the
    # status pair, "Unblock (say how) / Drop it"). The lean is the recommended option when one was named.
    options_source: str = "default"
    lean: Lean | None = None
    draft_text: str = ""
    recipient: str = ""
    reply_text: str = ""
    reply_surface: str = "CoS"
    section: str = "needs_you"
    refs: list[str] = field(default_factory=list)   # Slack permalinks / PR urls this ask is answered by
    gmail_id: str = ""
    filing_flag: bool = False
    # Where the draft goes, as the "To:" line reads ("Peyton Example (DM)", "#vm"); ``draft_to_inferred``
    # is True when it was read off the draft text rather than the worker's marker (shown as "To (inferred)").
    draft_to: str = ""
    draft_to_inferred: bool = False
    # For a follow-up: who it waits on ("owner", "other", "chore") and the label the card shows for a wait
    # that is not on the owner. Empty for every other kind.
    waits_on: str = ""
    waits_on_label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "section": self.section,
            "waits_on": self.waits_on,
            "waits_on_label": self.waits_on_label,
            "title": self.title,
            "summary": self.summary,
            "options": list(self.options),
            "draft_text": self.draft_text,
            "recipient": self.recipient,
            "reply_text": self.reply_text,
            "reply_surface": self.reply_surface,
            "draft_to": self.draft_to,
            "draft_to_inferred": self.draft_to_inferred,
            "options_source": self.options_source,
            "lean": self.lean.as_dict() if self.lean else None,
        }


def opt(label: str, reply: str = "") -> dict[str, str]:
    return {"label": label, "reply": reply}


# --------------------------------------------------------------------------- Options / Lean segment
#
# A blocked or needs-ruling report carries its ways forward in one fixed shape, so the CoS Review card shows
# the same options and lean CoS presents: ``Options: (a) …; (b) …; (c) …`` (at most three, one line each) then
# ``Lean: <letter> because <reason>``. Nothing here clips an option: each is already one line.

OPTIONS_MARK = re.compile(r"\bOptions\s*:\s*(?=\(a\))", re.I)
LEAN_MARK = re.compile(r"\bLean\s*:\s*\(?(?P<letter>[a-d])\)?\s*(?:[,:;]\s*)?(?:because\s+)?(?P<reason>[^\n]*)", re.I)
MAX_OPTIONS = 3
LEAN_LABEL = "lean"


@dataclass
class OptionsSegment:
    options: list[tuple[str, str]]   # (letter, text)
    lean: Lean | None = None
    span: tuple[int, int] = (0, 0)   # where the segment sits in the prose it was parsed from

    def as_options(self, task_id: str) -> list[dict[str, str]]:
        return [opt(option_label(letter, text, self.lean), f"{task_id}: go with ({letter})") for letter, text in self.options]

    def lean_reply(self, task_id: str, fallback: str) -> str:
        return f"{task_id}: go with ({self.lean.letter})" if self.lean else fallback


def option_label(letter: str, text: str, lean: Lean | None) -> str:
    """``(a) text``, with `` (lean)`` on the option the worker or note recommends."""
    label = f"({letter}) {text}"
    return f"{label} ({LEAN_LABEL})" if lean and lean.letter == letter else label


def lean_of(text: str, letters: set[str]) -> tuple[Lean | None, int]:
    """The ``Lean:`` line in ``text`` when it names one of ``letters``, and where it starts (-1 when absent)."""
    for match in LEAN_MARK.finditer(text):
        letter = match.group("letter").lower()
        if letter not in letters:
            continue
        reason = match.group("reason").strip()
        # The reason is the rest of its sentence: stop before the next capitalised sentence or a label.
        reason = re.split(r"(?<=[a-z0-9)\"])\.\s+(?=[A-Z])|\s+(?=(?:Review|Thread|Note|Open link|Open in Obsidian)\s*:)", reason, maxsplit=1)[0]
        return Lean(letter, reason.strip(" .;:,")), match.start()
    return None, -1


def options_segment(prose: str) -> OptionsSegment | None:
    """The ``Options: (a) …; (b) …`` segment of a report with its ``Lean:`` line, or None when the report has
    none. Two lettered items are the floor (one is a sentence, not a choice); past three, the extra items are
    dropped, never merged into the third. An option is never clipped."""
    start = OPTIONS_MARK.search(prose or "")
    if not start:
        return None
    body = prose[start.end():]
    lean, lean_at = lean_of(body, {"a", "b", "c", "d"})
    end_of_options = lean_at if lean_at >= 0 else len(body)
    listing = body[:end_of_options]
    matches = list(LETTER.finditer(listing))
    if len(matches) < 2 or [m.group(1) for m in matches[:2]] != ["a", "b"]:
        return None
    items: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        tail = listing[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(listing)]
        if index + 1 == len(matches) and lean_at < 0:
            # No Lean line: the last option ends with its sentence, not with the rest of the report.
            tail = re.split(r"(?<=[a-z0-9)\"])\.\s+(?=[A-Z])", tail, maxsplit=1)[0]
        text = squash(tail).strip(" ,;.")
        text = re.sub(r"\s+or$", "", text)
        if re.search(r"\w", text):
            # A placeholder item ("…", as in the gate's own warning text) is not an option.
            items.append((match.group(1), text))
    items = items[:MAX_OPTIONS]
    if len(items) < 2:
        return None
    if lean and lean.letter not in {letter for letter, _ in items}:
        lean = None
    span_end = start.end() + (lean_at if lean_at >= 0 else 0)
    if lean_at >= 0:
        # Up to the end of the Lean sentence.
        reason_end = body.find(lean.reason, lean_at) + len(lean.reason) if lean and lean.reason else lean_at
        span_end = start.end() + max(reason_end, lean_at)
    else:
        last = matches[-1]
        span_end = start.end() + last.end() + len(items[-1][1]) if len(items) == len(matches) else start.end() + len(listing)
    return OptionsSegment(items, lean, (start.start(), min(span_end, len(prose))))


def without_options_segment(prose: str, segment: OptionsSegment | None) -> str:
    """``prose`` with the Options/Lean segment cut out, so a decision list parser never splits it."""
    if not segment:
        return prose
    begin, end = segment.span
    return squash(prose[:begin] + " " + prose[end:])


# --------------------------------------------------------------------------- text helpers

def squash(text: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"\s+([.,;:)?!])(?![\w/])", r"\1", text).strip()


def strip_parens(text: str) -> str:
    for _ in range(2):
        text = PARENS.sub("", text)
    return squash(text)


def capitalize_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


# A report sentence never worth a title or summary: the filing warning (the pane shows it as a badge) and the
# label residue a report-to-cos wrapper leaves in front of the prose ("Thread Review note Vetoes first:").
NOISE_SENTENCE = re.compile(r"^(?:⚠️|Filing promised\b)", re.I)
LABEL_LEAD = re.compile(r"^(?:Thread Review note|Review note|Thread|Review)\s*[:—–-]*\s+(?=[A-Z0-9(])")
# Never split after a list number ("1. Keep … 2. Drop") or before the next numbered item.
SENTENCE_SPLIT = re.compile(r"(?<!\s\d\.)(?<!\s\d\d\.)(?<=[.;!?])\s+(?![(]?(?:[2-9]|1\d|[b-h])[).]\s)(?=[A-Z(\"'0-9])")


# "⚠️ Filing promised, no record URL. done: …" and a bare "done." opener carry no subject.
NOISE_LEAD = re.compile(r"^(?:⚠️\s*(?:Filing promised[^.]*\.)?\s*|(?:done|failed|blocked|cancelled)\.\s+)+", re.I)


def sentences_of(prose: str) -> list[str]:
    prose = NOISE_LEAD.sub("", LABEL_LEAD.sub("", prose.strip()))
    parts = SENTENCE_SPLIT.split(prose)
    return [p.strip() for p in parts if p.strip() and not NOISE_SENTENCE.match(p.strip())]


def first_sentence(prose: str) -> str:
    parts = sentences_of(prose)
    return parts[0] if parts else prose.strip()


def first_clause(text: str, minimum: int = 24) -> str:
    """Cut at the first sentence or clause boundary after ``minimum`` characters; keep a '?'."""
    text = squash(text)
    best: int | None = None
    keep_mark = False
    for mark in (". ", ": ", "? ", "! ", "; ", ", "):
        pos = text.find(mark, minimum)
        if pos != -1 and (best is None or pos < best):
            best = pos
            keep_mark = mark in ("? ", "! ")
    if best is None:
        return text.rstrip(".;:,")
    return text[: best + 1] if keep_mark else text[:best].rstrip(".;:,")


def balance_parens(text: str) -> str:
    """Drop an unclosed trailing '(…' left by a cut; close nothing, cut nothing else."""
    if text.count("(") > text.count(")"):
        text = text[: text.rfind("(")]
    return text.rstrip(" ,;:(")


def soft_cut(text: str, limit: int = TITLE_SOFT, floor: int = 18) -> str:
    """Longest prefix that ends at a natural cut point and fits ``limit``; whole text if it fits."""
    text = squash(text).rstrip(".;")
    if len(text) <= limit:
        return balance_parens(text)
    best = ""
    for mark in (": ", "; ", ", ", " (", " after ", " with ", " is ", " are ", " now ", " via ", " as ", " and ", " so ",
                 " but ", " while ", " because ", " until ", " when ", " where ", " which "):
        start = 0
        while True:
            pos = text.find(mark, start)
            if pos == -1 or pos > limit:
                break
            if pos >= floor and pos > len(best):
                best = text[:pos]
            start = pos + 1
    if not best:
        # Weaker cut points (prepositions) only when no clause boundary fits.
        for mark in (" from ", " for ", " on ", " to ", " in ", " of ", " by "):
            pos = text.rfind(mark, floor, limit)
            if pos > len(best):
                best = text[:pos]
    if best:
        return balance_parens(best)
    if len(text) <= TITLE_HARD:
        return balance_parens(text)
    cut = text[: TITLE_HARD - 1].rsplit(" ", 1)[0]
    return balance_parens(cut) + "…"


def later_title(prose: str) -> str:
    head = STATUS_PREFIX.sub("", first_sentence(prose)).rstrip(".;")
    head = soft_cut(head)
    for _ in range(3):
        before = head
        head = STUB_WORDS.sub("", head).rstrip(" ,;:(")
        head = TRAILING_VERB.sub("", head)
        if head == before:
            break
    return capitalize_first(head) or "Report"


def draft_subject(draft: str) -> str:
    """First clause of a Slack draft, stripped of prefix, greeting, mention, links and markup."""
    text = draft.strip()
    text = re.sub(r"^\s*:robot_face:\s*", "", text)
    text = re.sub(r"^\s*(?:Hey|Hi|Hello)\b[^,\n]{0,40},\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*<@U[A-Z0-9]+>\s*,?\s*", "", text)
    text = re.sub(r"^\s*(?:Hey|Hi|Hello)\b[^,\n]{0,40},\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*@[\w.-]+\s*,?\s*", "", text)
    text = SLACK_LINK.sub(lambda m: m.group("label"), text)
    text = SLACK_BARE.sub("", text)
    text = SLACK_MENTION.sub("", text)
    text = re.sub(r"[*_`]+", "", text)
    text = text.split("\n", 1)[0] if "\n" in text and len(text.split("\n", 1)[0].strip()) >= 24 else text
    text = strip_parens(text)
    clause = first_clause(text)
    if len(clause) > TITLE_HARD - 10:
        clause = soft_cut(clause, TITLE_SOFT + 16)
    return capitalize_first(clause.strip(" ,;:"))


# --------------------------------------------------------------------------- recipients

def _name_from_cache(user_id: str, names: dict[str, Any]) -> str:
    """The display name for a Slack user id from the shared map (people/ first, then the on-disk
    cache): the full name, never the surname or first name alone, so a draft item says who the message
    is going to."""
    rec = names.get(user_id) if isinstance(names, dict) else None
    if not isinstance(rec, dict):
        return ""
    full = str(rec.get("name") or "").strip()
    return full or str(rec.get("display_name") or "").strip()


# "(Peyton Example DM, also his talking points for Monday)": the worker named the person and the surface.
TAIL_DM = re.compile(r"^([A-Z][\w'.-]+(?: [A-Z][\w'.-]+){0,3}) DM\b")
TAIL_DM_TO = re.compile(r"^(?:Slack )?DM to ([A-Z][\w'.-]+(?: [A-Z][\w'.-]+){0,3})\b")
LEADING_MENTION = re.compile(r"^\s*(?::robot_face:\s*|🤖\s*)?(?:(?:Hey|Hi|Hello)\s+)?<@(U[A-Z0-9]+)>", re.I)
CHANNEL_MENTION = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
GREETING = re.compile(r"^\s*(?::robot_face:\s*|🤖\s*)?(?:Hey|Hi|Hello)\s+([A-Z][\w'-]+(?: [A-Z][\w'-]+)?)\s*[,!:.]")


def recipient_from_tail(tail: str) -> str:
    tail = tail.strip(" ():")
    chan = re.search(r"#[\w-]+", tail)
    if chan:
        return chan.group(0)
    dm_to = TAIL_DM_TO.match(tail)
    if dm_to:
        return dm_to.group(1)
    dm = TAIL_DM.match(tail)
    if dm and dm.group(1) != "Slack":
        return dm.group(1)
    named = re.search(r"\b([A-Z][a-z]+)(?:'s)? thread\b", tail)
    if named:
        return named.group(1)
    to = re.search(r"\bto ([A-Z][a-z]+(?: and [A-Z][a-z]+)?)\b", tail)
    if to:
        return to.group(1)
    lone = re.fullmatch(r"([A-Z][a-z]+(?: and [A-Z][a-z]+)?)", tail)
    if lone and lone.group(1) not in {"Thread", "Note"}:
        return lone.group(1)
    return ""


def recipient_of_draft(tail: str, draft: str, prose: str, names: dict[str, Any]) -> str:
    who = recipient_from_tail(tail)
    if who:
        return who
    for user_id in SLACK_MENTION.findall(draft):
        name = _name_from_cache(user_id, names)
        if name:
            return name
    for match in re.finditer(r"\b([A-Z][a-z]{2,})(?:'s\b| (?:DM|reply|message|draft|thread)\b)", prose):
        if match.group(1) not in NAME_STOPLIST:
            return match.group(1)
    return "the thread"


def draft_destination(tail: str, draft: str, who: str, names: dict[str, Any]) -> tuple[str, bool]:
    """``(label, inferred)`` for the "To:" line above a task draft. The worker's marker tail is explicit ("(#vm)", "(Una Frost
    Hurtle DM)"); a leading ``<@U…>`` greeting or a ``<#C…|name>`` in the draft text is inferred."""
    plain_tail = tail.strip(" ():")
    tail_channel = re.search(r"#[\w-]+", plain_tail)
    if tail_channel:
        return tail_channel.group(0), False
    dm = TAIL_DM_TO.match(plain_tail) or TAIL_DM.match(plain_tail)
    if dm and dm.group(1) != "Slack":
        return f"{dm.group(1)} (DM)", False
    greeting = LEADING_MENTION.match(draft or "")
    if greeting:
        name = _name_from_cache(greeting.group(1), names)
        return f"{name or '@' + greeting.group(1)} (DM)", True
    hey = GREETING.match(draft or "")
    if hey:
        return f"{hey.group(1)} (DM)", True
    channel = CHANNEL_MENTION.search(draft or "")
    if channel:
        return f"#{channel.group(1)}", True
    if who.startswith("#"):
        return who, True
    if who and who not in ("the thread", "Slack"):
        return f"{who} (DM)", True
    return "Slack thread", True


# --------------------------------------------------------------------------- ask builders

def draft_asks(text: str, prose: str, task_id: str, names: dict[str, Any]) -> list[Ask]:
    """One ask per 'Draft for the owner to send' marker paired with the fenced block that follows it."""
    asks: list[Ask] = []
    fences = [(m.start(), m.group("body").strip()) for m in FENCED.finditer(text)]
    marks = list(DRAFT_MARK.finditer(text))
    used: set[int] = set()
    filing = bool(FILING_FLAG.search(prose))
    for index, mark in enumerate(marks):
        limit = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = ""
        for pos, fence in fences:
            if mark.end() <= pos < limit and pos not in used and fence:
                used.add(pos)
                body = fence
                break
        if not body:
            continue
        issue = ISSUE_DRAFT.match(body)
        if issue:
            # A parked Linear/Jira issue body, not a message: the ask is to file it.
            subject = soft_cut(re.sub(r"[*_`]+", "", issue.group("title")), TITLE_SOFT + 8 - len("File the issue: "), floor=12)
            reply = f"{task_id}: file the issue as drafted"
            asks.append(Ask(
                kind="draft", title=f"File the issue: {subject}", draft_text=body, recipient="Linear", reply_text=reply,
                filing_flag=filing, draft_to="Linear",
                options=[opt("File it", reply), opt("Edit, then file", f"{task_id}: edit the issue draft: <change>"),
                         opt("Do not file", f"{task_id}: do not file the issue")],
            ))
            continue
        who = recipient_of_draft(mark.group("tail"), body, prose, names)
        draft_to, inferred = draft_destination(mark.group("tail"), body, who, names)
        subject = draft_subject(body)
        if subject:
            subject = soft_cut(subject, max(TITLE_SOFT + 8 - len(f"Send to {who}: "), 24), floor=12)
        title = f"Send to {who}: {subject}" if subject else f"Send the draft to {who}"
        reply = f"{task_id}: send the draft to {who} as is"
        asks.append(Ask(
            kind="draft", title=title, draft_text=body, recipient=who, reply_text=reply, filing_flag=filing,
            draft_to=draft_to, draft_to_inferred=inferred,
            options=[opt("Send as is", reply), opt("Edit, then send", f"{task_id}: edit the draft to {who}: <change>"),
                     opt("Do not send", f"{task_id}: do not send the draft to {who}")],
        ))
    if asks:
        return asks
    gmail = GMAIL_DRAFT.search(prose)
    if gmail:
        who = gmail.group("name").split()[0]
        reply = f"{task_id}: send the Gmail draft to {who} as is"
        body = next((fence for _, fence in fences if fence), "")
        return [Ask(kind="draft", title=f"Send Gmail to {who}: {gmail.group('subject')}", draft_text=body,
                    recipient=who, draft_to=f"{gmail.group('name').strip()}, email", reply_text=reply, gmail_id=gmail.group("draft_id").replace("r", "r-", 1) if not gmail.group("draft_id").startswith("r-") else gmail.group("draft_id"),
                    options=[opt("Send as is", reply), opt("Do not send", f"{task_id}: do not send the Gmail draft to {who}")])]
    unfenced = UNFENCED_DRAFT.search(prose)
    if unfenced:
        who = unfenced.group("who")
        reply = f"{task_id}: send the draft to {who} as is"
        return [Ask(kind="draft", title=f"Send the Slack draft to {who}", recipient=who, reply_text=reply, filing_flag=filing,
                    draft_to=f"{who} (DM)" if not who.startswith("#") else who, draft_to_inferred=True,
                    options=[opt("Send as is", reply), opt("Do not send", f"{task_id}: do not send the draft to {who}")])]
    return []


def letter_options(item: str) -> tuple[str, list[tuple[str, str]]]:
    """('lead before (a)', [('a', 'text'), ('b', 'text')]) or ('', []) when the item has no (a)/(b)."""
    matches = list(LETTER.finditer(item))
    if len(matches) < 2 or [m.group(1) for m in matches[:2]] != ["a", "b"]:
        return "", []
    lead = item[: matches[0].start()]
    lead = re.sub(r"\b(?:pick|choose|either)\s*$", "", lead.strip(" ,;:"), flags=re.I).strip(" ,;:")
    out: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(item)
        body = item[match.end():end]
        body = re.sub(r"\s+or\s*$", "", body).strip(" ,;.")
        if body:
            out.append((match.group(1), body))
    return lead, out


NUMBERED_ITEM = re.compile(r"\((\d{1,2})\)\s*")
# "1. … 2. …" dot style; a version "v0.1.8" never matches
# because the dot must be followed by whitespace.
DOT_NUMBERED_ITEM = re.compile(r"(?:(?<=^)|(?<=\s))(\d{1,2})\.\s+")


def list_pieces(body: str) -> list[str]:
    """The items of a decision list. An explicit ``(1) … (2) …`` numbering wins: each numbered item is one
    piece however many ``;`` it holds inside. Only an unnumbered list splits on ``; ``, so a ``;`` inside a
    numbered item never makes a fake option."""
    for pattern in (NUMBERED_ITEM, DOT_NUMBERED_ITEM):
        numbered = list(pattern.finditer(body))
        if len(numbered) >= 2 and int(numbered[0].group(1)) == 1:
            pieces = []
            for index, match in enumerate(numbered):
                end = numbered[index + 1].start() if index + 1 < len(numbered) else len(body)
                pieces.append(body[match.end():end])
            return pieces
    return re.split(r";\s+", body)


def ruling_items(prose: str) -> list[tuple[str, str]]:
    """(marker, item) pairs from every decision/veto list in the prose, in order."""
    items: list[tuple[str, str]] = []
    for match in RULING_MARK.finditer(prose):
        mark = match.group("mark")
        for piece in list_pieces(match.group("body")):
            piece = piece.strip(" .;")
            floor = MIN_QUESTION_ITEM if QUESTION_WORDS.search(piece) else MIN_RULING_ITEM
            if len(piece) >= floor:
                items.append((mark, piece))
    return items


def subject(workstream_name: str, rest: str) -> str:
    """``<Workstream name>: <rest>``: a title names the subject, not the ruling shape."""
    name = squash(workstream_name or "").strip()
    rest = rest.strip()
    if not name:
        return capitalize_first(rest)
    return f"{name}: {rest}"


def ruling_asks(prose: str, task_id: str, has_draft: bool, workstream_name: str = "") -> list[Ask]:
    asks: list[Ask] = []
    for _, item in ruling_items(prose):
        if has_draft and re.search(r"\b(?:draft|parked reply|parked)\b", item, re.I):
            continue  # the draft row already carries this decision
        lead, letters = letter_options(item)
        if letters:
            head = soft_cut(strip_parens(lead) or item, 56, floor=12)
            asks.append(Ask(
                kind="ruling", title=f"Pick (a) or (b): {capitalize_first(head)}", summary=capitalize_first(squash(lead)) + ".",
                options=[opt(f"({letter}) {body}", f"{task_id}: go with ({letter})") for letter, body in letters],
                reply_text=f"{task_id}: go with (a)",
            ))
        elif QUESTION_WORDS.search(item):
            question = strip_parens(item).rstrip("?")
            asks.append(Ask(
                kind="ruling", title=subject(workstream_name, f"{soft_cut(question, 70, floor=12)}?"),
                summary=capitalize_first(squash(item)) + ".",
                options=[opt("Decide (reply in CoS)", f"{task_id}: ruling: <your call>")],
                reply_text=f"{task_id}: ruling: <your call>",
            ))
        else:
            head = capitalize_first(soft_cut(strip_parens(item), 52, floor=12))
            asks.append(Ask(
                kind="ruling", title=f"{head}: keep or veto?", summary=capitalize_first(squash(item)) + ".",
                options=[opt("Keep", f"{task_id}: keep: {head}"), opt("Veto", f"{task_id}: veto: {head}")],
                reply_text=f"{task_id}: keep: {head}",
            ))
        if len(asks) == MAX_RULING_ASKS:
            break
    return asks


def veto_options(prose: str, task_id: str) -> list[dict[str, str]]:
    """A for-later row's options: the standard ``No vetoes`` / ``Veto (name which)`` pair, and nothing
    derived from the report prose. The vetoes themselves stay readable in the full report the pane shows;
    numbered clauses inside a report never become options."""
    if not ruling_items(prose) and not numbered_veto_count(prose):
        return []
    return [opt("No vetoes", f"{task_id}: no vetoes"), opt("Veto (name which)", f"{task_id}: veto: <which>")]


SCHEDULED_AT = re.compile(
    r"\b(?:fires|scheduled(?: for)?|sends?(?: at| on)?|armed for)\s+(?P<day>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+"
    r"(?:\d{4}-\d{2}-\d{2}\s+)?(?P<time>\d{1,2}:\d{2})", re.I)
ALREADY_ANSWERED = re.compile(r"(?P<what>[^.;]{0,120}?)\s+(?:is|was|are|were)\s+already\s+answered\b|resolved_by_owner", re.I)
BRAND_WORDS = {"Example", "Acme", "Customer", "Page", "Community"}
PROMPT_SUBJECT = re.compile(
    r"\b(?:on|about|for|re)\s+the\s+(?P<subject>[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,2}\s+(?:DM|reply|post|message|thread|draft|email|note|deck|site))\b")


def review_title(prose: str, prompt: str = "", workstream_name: str = "") -> str:
    """A for-review title that names the subject and the outcome, not the status word the report opened
    with ("SCHEDULED, loaded: com.switchboard.oneshot…" and "Close-out marked" tell the owner nothing).
    Heuristic: the subject comes from the ask (``the Ash Example DM``) or the report's own
    "already answered" clause; the outcome is the scheduled time, the answered state, or the first sentence;
    a numbered veto list adds ``N to veto``. Falls back to ``<Workstream>: <first sentence>``."""
    vetoes = numbered_veto_count(prose)
    veto_tail = f", {vetoes} to veto" if vetoes else ""
    if vetoes and re.search(r"\bvetoe?s?\b", first_sentence(prose), re.I):
        veto_tail = ""   # "landed the fixes, 7 vetoes in the note, 7 to veto" says it twice
    answered = ALREADY_ANSWERED.search(prose)
    if answered:
        what = strip_parens(answered.group("what") or "") if answered.group("what") else ""
        inner = re.search(r"\(([^()]*)\)", answered.group("what") or "")
        if inner:   # "Item 4 (Jordan, #example-for-agents)" → "Jordan #example-for-agents"
            what = inner.group(1).replace(",", "")
        what = re.sub(r"^\s*(?:item|feedback|row|ask)\s*\d+\s*", "", what, flags=re.I).strip(" ,:;")
        if what:
            return capitalize_first(f"{soft_cut(what, 56, floor=8)} ask: already answered by the owner")
    subject_match = PROMPT_SUBJECT.search(squash(prompt or ""))
    subject_text = ""
    if subject_match:
        words = subject_match.group("subject").split()
        # The subject must be something the report talks about: "the Example Customer Community post" in a
        # prompt whose report never mentions it would give "Example post: format correction".
        # The whole phrase (minus the noun) must appear in the report: "Community" alone does not make the
        # report about the "Example Customer Community post".
        phrase = r"\s+".join(re.escape(w) for w in words[:-1])
        # A person's first name alone also counts ("Ash Example DM" when the report says "dm-alex").
        first_name_only = len(words) == 3 and words[0] not in BRAND_WORDS and re.search(rf"\b{re.escape(words[0])}\b", prose, re.I)
        if phrase and (re.search(rf"\b{phrase}\b", prose, re.I) or first_name_only):
            subject_text = " ".join(words[:1] + words[-1:]) if len(words) > 2 else " ".join(words)   # "Marlo DM"
    when = SCHEDULED_AT.search(prose)
    if when:
        head = subject_text or subject(workstream_name, "send")
        return capitalize_first(f"{head} scheduled {when.group('day').title()[:3]} {when.group('time')}{veto_tail}")
    first = LABEL_LEAD.sub("", first_sentence(prose))
    if vetoes and RULING_MARK.match(STATUS_PREFIX.sub("", first)):
        # The report opens with the veto list itself; "Vetoes first: 1" is no subject.
        head = f"{vetoes} to veto"
        return capitalize_first(f"{subject_text}: {head}") if subject_text else subject(workstream_name, head)
    inline = INLINE_VETO_LIST.search(STATUS_PREFIX.sub("", first))
    if vetoes and inline and inline.start() >= 8:
        # "Phase 2 done; vetoes first: (1) slug prefix…": the subject is what came before the list.
        head = later_title(first[: inline.start()].rstrip(" ;:,.") + ".")
        veto_tail = f", {vetoes} to veto"
        if subject_text:
            return capitalize_first(f"{subject_text}: {head[:1].lower() + head[1:]}{veto_tail}")
        return subject(workstream_name, head) + veto_tail
    head = later_title(prose)
    if subject_text:
        return capitalize_first(f"{subject_text}: {head[:1].lower() + head[1:]}{veto_tail}")
    return subject(workstream_name, head) + veto_tail


NUMBERED_VETOES = re.compile(r"\bveto(?:es)?\b[^:]{0,20}:\s*(?:\(1\)|1\.\s|\(a\))", re.I)   # "Vetoes first: (1)", "To veto: 1. …", "Vetoes: (a)"
INLINE_VETO_LIST = re.compile(r"[;:,]?\s*\b(?:vetoes?(?:\s+first)?|to veto|decisions? to veto)\s*:\s*(?:\(1\)|1\.\s|\(a\))", re.I)
LETTER_INDEX = {c: i for i, c in enumerate("abcdefgh", start=1)}
# "9 veto items in note §13" is a stated count too, not only "9 vetoes".
COUNTED_VETOES = re.compile(r"\b(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
                            r"(?:decisions? to veto|veto(?:es)?(?:\s+items?)?)\b", re.I)
WORD_NUMBERS = {w: i for i, w in enumerate(["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                                             "eleven", "twelve"], start=1)}


def numbered_veto_count(prose: str) -> int:
    """N when the report lists its vetoes as ``Vetoes first: (1) … (N) …`` or ``1. … N.`` (a go that is really
    N vetoes), or states the count ("7 vetoes in the note") without listing them inline."""
    match = NUMBERED_VETOES.search(prose)
    if match:
        tail = prose[match.start():]
        if match.group(0).endswith("(a)"):
            pattern, index = r"\(([a-h])\)", LETTER_INDEX.get
        else:
            pattern = r"\((\d{1,2})\)" if "(" in match.group(0) else r"(?:^|\s)(\d{1,2})\.\s"
            index = int
        count = 0
        for n in (index(n) for n in re.findall(pattern, tail)):
            if n == count + 1:
                count = n
        if count:
            return count
    counted = COUNTED_VETOES.search(prose)
    if counted:
        raw = counted.group("n").lower()
        return int(raw) if raw.isdigit() else WORD_NUMBERS.get(raw, 0)
    return 0


def go_ask(prose: str, task_id: str, workstream_name: str) -> Ask:
    match = GO_FOR.search(prose)
    vetoes = numbered_veto_count(prose)
    if match:
        title = f"{workstream_name}: go for {squash(match.group(1)).strip()}?"
    else:
        head = STATUS_PREFIX.sub("", first_sentence(prose))
        head = re.split(r",\s+|;\s+|\s+waiting\b|\s+until\b", head, maxsplit=1)[0].rstrip(".")
        head = soft_cut(head, 56, floor=10)
        # "Needs you handled rows fixed: go?" hid that the report was four vetoes.
        title = f"{head}: accept {vetoes} veto{'es' if vetoes != 1 else ''}?" if vetoes else f"{head}: go?"
    return Ask(kind="go", title=title, reply_text=f"{task_id}: go",
               options=[opt("Go", f"{task_id}: go"), opt("Hold", f"{task_id}: hold: <why>")])


def followup_text(prose: str) -> str:
    """The work a done report declares open with ``Follow-up: <what>``, squashed; ``""`` when there is none."""
    match = FOLLOWUP_LINE.search(prose or "")
    if not match:
        return ""
    return squash(match.group("what")).strip().rstrip(".;,")


# Who a Follow-up line waits on: most worker "Follow-up:" lines are chores or waits on someone else.
# A clause opened by one of these words names what the work waits on; when that clause never names the owner, the
# wait is on someone or something else ("when Reese pings", "after #46532 lands", "until v0.3.0 is live").
WAIT_CLAUSE = re.compile(r"\b(?:when|after|once|until|pending|before|waiting\s+(?:on|for))\b(?P<rest>[^,;.()]*)", re.I)
# the owner acting: "the owner rules", "the owner to decide", "the owner's go", "on the owner say-so", "the owner picks (a), (b) or (c)".
OWNER_ACT = re.compile(
    r"\b(?:{{OWNER_FIRST}}|the owner)(?:'s)?\s+(?:to\s+)?(?:rules?|decides?|picks?|sends?|approves?|confirms?|says?|nudges?|chooses?|answers?"
    r"|reviews?|re-?opens?|signs?|posts?|replies|trims?|okays?|say-so|go\b|call\b|ruling|looks?|checks?|verif(?:y|ies))\b",
    re.I)
OWNER_DECIDES = re.compile(r"\byes or no\b|\bgo or hold\b|\b(?:{{OWNER_FIRST}}'s|the owner's|your) (?:go|call|pick|ruling|say-so|approval)\b", re.I)
# A line whose subject is the owner names an action of theirs, whatever the verb ("the owner decides",
# "the owner reboots" and so on), which the name-only patterns above would file as waits on someone else. "Can", "may" and the like offer something optional, "is waiting" and "does nothing" describe rather than
# ask, and "the owner tabs" is a noun, so all of those are left to the patterns above.
OWNER_LED = re.compile(
    r"^\s*(?:\(?[0-9a-z]\)\s*)?(?:{{OWNER_FIRST}}|(?:the\s+)?owner|you)(?:'ll|\s+will)?\s+"
    r"(?!(?:(?!need\b|proceed\b)\w+ed|can|may|could|might|not|tabs?|lists?|rows?|lines?|names?(?!\s+(?:the|a|an|which|what|who|one|it|them)\b)|fields?|columns?|teams?"
    r"|should\s+(?:be|see|now|get|have|find|receive|notice|expect|work|pass)|is|are|was|were|had|did|already|ha(?:s|ve)\s+already|do(?:es)?\s+nothing"
    r"|don'?t|do\s+not|doesn'?t|does\s+not|won'?t|will\s+not|need\s+not|needn'?t)\b)", re.I)
OWNER_NAMED = re.compile(r"\b(?:the owner(?!\s+tabs?\b)|{{OWNER_FIRST}})\b", re.I)
NAMES_OWNER = re.compile(r"\b(?:the owner(?!\s+tabs?\b)|{{OWNER_FIRST}}|you)\b", re.I)
# Worker chores a continuation runs without a ruling: reloads, installs, redeploys, reruns, commits, QA passes.
CHORE = re.compile(
    r"\b(?:run|reload|install|redeploy|re-?run|commit|push|rebuild|rebase|sync|re-?render|re-?sync|compile|retry|re-?upload"
    r"|re-?deploy|screenshots?|visual QA|QA pass|re-?record)\b|install-amp-skills|~/bin/|unpushed|gh pr create", re.I)
# A wait clause on a person answering: "when Reese replies", "once Drew pings", "after Lane gets back".
PERSON_REPLIES = re.compile(r"\b(?:replies|reply|responds?|answers?|pings?|confirms?|gets back|weighs in|signs off|approves?)\b", re.I)


def followup_owner(what: str) -> str:
    """Who the follow-up waits on: ``owner`` (a ruling, approval, pick or send only the owner can make), ``other``
    (a reply from someone else, a deploy, a PR landing) or ``chore`` (a worker task a continuation runs)."""
    text = what or ""
    clauses = [m.group("rest") for m in WAIT_CLAUSE.finditer(text)]
    # "the owner reviews the run when Reese pings" waits on Reese, so an owner-led
    # line only counts as the owner's when no other person's reply gates it.
    other_reply = any(PERSON_REPLIES.search(rest) and not NAMES_OWNER.search(rest) for rest in clauses)
    if (OWNER_LED.match(text) and not other_reply) or any(OWNER_NAMED.search(rest) for rest in clauses):   # "re-run once the owner approves"
        return "owner"
    stripped = WAIT_CLAUSE.sub(" ", text)
    waits = [rest for rest in clauses if not NAMES_OWNER.search(rest)]
    if waits:
        # "Re-run the eval when Reese replies" waits on Reese; "run install-amp-skills.sh after landing" is a
        # chore whose wait is an event, not a person.
        if any(PERSON_REPLIES.search(rest) for rest in waits):
            return "other"
        return "chore" if CHORE.search(stripped) and not (OWNER_ACT.search(stripped) or OWNER_DECIDES.search(stripped)) else "other"
    if OWNER_ACT.search(stripped) or OWNER_DECIDES.search(stripped):
        return "owner"
    if CHORE.search(stripped):
        return "chore"
    return "other"


WAITS_ON_LABEL = {"other": "waits on someone else", "chore": "worker chore, no ruling needed"}


def followup_ask(prose: str, task_id: str, workstream_name: str) -> Ask | None:
    """The follow-up a finished report leaves open. Needs-you only when it waits on the owner (a ruling, approval,
    pick or send); a wait on someone else or a worker chore is For review, so Needs-you stays honest.
    Dispatch sends it as a continuation of the task (``dispatch.py steer`` on a terminal row);
    Drop closes it. ``None`` without a Follow-up line."""
    what = followup_text(prose)
    if not what:
        return None
    head = soft_cut(what, 70, floor=12)
    owner = followup_owner(what)
    return Ask(kind="followup", title=subject(workstream_name, f"follow-up: {head}"),
               summary=capitalize_first(what) + ".", reply_text=f"{task_id}: dispatch the follow-up",
               section="needs_you" if owner == "owner" else "for_review",
               waits_on=owner, waits_on_label=WAITS_ON_LABEL.get(owner, ""),
               options=[opt("Dispatch the follow-up", f"{task_id}: dispatch the follow-up"),
                        opt("Drop it", f"{task_id}: drop the follow-up")])


def post_ask(prose: str, task_id: str, workstream_name: str, drafts: list[str]) -> Ask:
    match = POST_DOC.search(prose)
    title = f"Post the {match.group(1)} Doc" if match else f"Post it: {workstream_name}"
    return Ask(kind="post", title=title, draft_text="\n\n".join(drafts), reply_text=f"{task_id}: posted, close it out",
               options=[opt("Posted", f"{task_id}: posted, close it out"), opt("Ask for edits", f"{task_id}: edits: <what>")])


def console_ask(prose: str, task_id: str, drafts: list[str]) -> Ask:
    tail = prose
    marker = re.search(r"approve (?:or edit )?in the console\s*:?", prose, re.I)
    if marker:
        tail = prose[marker.end():]
    count = len(FEEDBACK_NUM.findall(tail)) or len(drafts)
    noun = "reply" if count == 1 else "replies"
    title = f"Approve {count} {noun} in the console" if count else "Approve the replies in the console"
    return Ask(kind="console", title=title, reply_text="", reply_surface="",
               options=[opt("Open the console"), opt("Skip", "")])


BLOCKER_LINE = re.compile(r"\b(?:(?:blocked|waiting|stuck)\s+(?:on|by|for)|(?:only\s+)?blocker)\s*:?\s+(?P<what>(?:[^.;]|\.(?![\s]|$))+)", re.I)
# An explicit "Only blocker:" names the live blocker; "prior submission blocker rt-… closed done" earlier in the
# same report is history.
ONLY_BLOCKER = re.compile(r"\b(?:only|remaining|current|live)\s+blocker\s*:?\s+(?P<what>(?:[^.;]|\.(?![\s]|$))+)", re.I)


def blocker_of(prose: str) -> str:
    match = ONLY_BLOCKER.search(prose) or BLOCKER_LINE.search(prose)
    return match.group("what").strip() if match else ""
# The sentence that names what the owner must rule on when a report has no explicit decision list.
NEEDS_OWNER = re.compile(r"\b(?:need(?:s|ing)\s+the owner|the owner(?:'s)?\s+(?:call|ruling)|your\s+call|deviation)\b", re.I)


def with_segment(ask: Ask, segment: OptionsSegment | None, task_id: str, source: str = "report") -> Ask:
    """``ask`` carrying the worker's own ways forward: the segment's lettered options (the lean marked)
    in place of the status defaults, plus ``Drop it`` for a blocked or failed task so the owner can still stop it
    in one click. The reply suggests the lean when there is one."""
    if not segment:
        return ask
    ask.options = segment.as_options(task_id)
    if ask.kind in {"blocked", "failed"}:
        ask.options.append(opt("Drop it", f"{task_id}: drop it"))
    ask.options_source = source
    ask.lean = segment.lean
    ask.reply_text = segment.lean_reply(task_id, ask.reply_text)
    return ask


def status_ask(kind: str, prose: str, task_id: str, workstream_name: str = "") -> Ask:
    segment = options_segment(prose)
    prose = without_options_segment(prose, segment)
    head = soft_cut(STATUS_PREFIX.sub("", first_sentence(prose)), 70, floor=12)
    if kind == "blocked":
        blocker = blocker_of(prose)
        what = soft_cut(blocker, 70, floor=12) if blocker else head
        ask = Ask(kind="blocked", title=subject(workstream_name, f"blocked on {what}"), reply_text=f"{task_id}: ruling: <your call>",
                  options=[opt("Unblock (say how)", f"{task_id}: unblock: <how>"), opt("Drop it", f"{task_id}: drop it")])
    else:
        ask = Ask(kind="failed", title=subject(workstream_name, f"failed, {head}"), reply_text=f"{task_id}: retry",
                  options=[opt("Retry", f"{task_id}: retry"), opt("Drop it", f"{task_id}: drop it")])
    return with_segment(ask, segment, task_id)


def asks_for(kind: str, text: str, prose: str, drafts: list[str], task_id: str, workstream_name: str,
             needs_ruling: bool, names: dict[str, Any]) -> list[Ask]:
    """Every needs-you ask in one report, or [] when the report is finished work."""
    if kind in {"blocked", "failed"}:
        return [status_ask(kind, prose, task_id, workstream_name)]
    if kind == "console":
        return [console_ask(prose, task_id, drafts)]
    asks: list[Ask] = []
    drafted = draft_asks(text, prose, task_id, names)
    asks.extend(drafted)
    if kind == "ruling" or needs_ruling:
        segment = options_segment(prose)
        if segment:
            # The worker's own Options/Lean segment is the ruling; the rest of the prose may still carry
            # separate veto items, parsed without the segment so its ";" never splits an option in two.
            prose = without_options_segment(prose, segment)
            focus = next((s for s in sentences_of(prose) if NEEDS_OWNER.search(s)), "") or first_sentence(prose)
            head = soft_cut(re.split(r";\s+", STATUS_PREFIX.sub("", focus), maxsplit=1)[0], 70, floor=12)
            asks.append(with_segment(Ask(kind="ruling", title=subject(workstream_name, f"pick a way forward on {head}"),
                                         summary=capitalize_first(squash(focus)).rstrip(".") + ".",
                                         reply_text=f"{task_id}: ruling: <your call>"), segment, task_id))
        for ask in ruling_asks(prose, task_id, bool(drafted), workstream_name):
            if ask.options and ask.options[0]["label"].startswith("(a)"):
                ask.options_source = "report"
            asks.append(ask)
        if not asks:
            # The ruling sentence ("Deviation needing the owner: the sample production clone…") beats the report's
            # opening line, which usually says what was built.
            focus = next((s for s in sentences_of(prose) if NEEDS_OWNER.search(s)), "") or first_sentence(prose)
            focus = re.sub(r"^[^:]{0,40}\b(?:need(?:s|ing)\s+the owner|the owner(?:'s)?\s+(?:call|ruling))\b[^:]{0,20}:\s*", "", focus, flags=re.I)
            head = soft_cut(re.split(r";\s+", STATUS_PREFIX.sub("", focus), maxsplit=1)[0], 70, floor=12)
            asks.append(Ask(kind="ruling", title=subject(workstream_name, f"decide on {head}"),
                            summary=capitalize_first(squash(focus)).rstrip(".") + ".", reply_text=f"{task_id}: ruling: <your call>",
                            options=[opt("Decide (reply in CoS)", f"{task_id}: ruling: <your call>")]))
    if kind == "go":
        asks.append(go_ask(prose, task_id, workstream_name))
    if kind == "post":
        asks.append(post_ask(prose, task_id, workstream_name, drafts))
    if kind == "draft" and not asks and drafts:
        # A report that only talks about a draft, with no draft text anywhere, is finished work, not an ask.
        who = recipient_of_draft("", "\n".join(drafts), prose, names)
        title = f"Send the draft to {who}" if who != "the thread" else "Send the parked draft"
        draft_to, inferred = draft_destination("", "\n".join(drafts), who, names)
        asks.append(Ask(kind="draft", title=title, reply_text=f"{task_id}: send the draft as is", recipient=who,
                        draft_text="\n\n".join(drafts), draft_to=draft_to, draft_to_inferred=inferred,
                        options=[opt("Send as is", f"{task_id}: send the draft as is"), opt("Do not send", f"{task_id}: do not send")]))
    return asks


# --------------------------------------------------------------------------- supersession

SENT_EVIDENCE = re.compile(
    r"\b([A-Z][a-z]+)(?:'s|-thread)? (?:draft|reply)\b[^.;]{0,80}?(?<!not )(?:superseded|sent|posted|declined|marked done)\b"
    r"|\(([A-Z][a-z]+) reply\)[^.;]{0,80}?marked done"
    r"|\bsent (r-\d+)|(r-\d+)[^.;]{0,40}?\bhimself\b",
)


def sent_evidence(prose: str) -> set[str]:
    """Names and Gmail draft ids this report says were sent, posted, declined or superseded."""
    out: set[str] = set()
    for match in SENT_EVIDENCE.finditer(prose):
        for group in match.groups():
            if group:
                out.add(group)
    return out
