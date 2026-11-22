#!/usr/bin/env python3
"""Spin up a new workstream on demand from a free-text ask.

Used by the Chief of Staff skill: whenever the owner brings a new topic/task to a
CoS session, scaffold a full workstream (folder + inbox/ + sources/ +
CONTEXT.md + config.yaml with a topic-relevant emoji) so the work is captured
and continuable instead of living only in chat.

This is the on-demand sibling of scaffold_workstream() in process-new-inbox.py
(which is shaped for inbound Slack/email/Linear records). Here the input is a
human ask, not an inbox record.

Usage:
  python3 system/scripts/new-workstream.py "<the ask text>" \
      [--name kebab-slug] [--priority high|medium|low] [--json] [--no-render]

Behavior:
  - Derives a 1-3 word Title Case name (and its kebab slug) from the subject of
    the work: verbs, filler words, leading descriptors ("Pre-review of",
    "Help me with"), and URLs never reach the name. When the ask links an
    artifact, its title names the work and is read first (link_titles.py:
    Drive, Docs, Sheets, Slides; Slack parent message or a Doc it links;
    Linear; Jira; GitHub), so "Pre-review of a Google Doc: <link>" is
    named from "Maple_Proposal_Working Draft.docx" -> "Maple Proposal".
    If the title lookup fails, the ask's own
    words are used only when at least two specific topic words survive;
    otherwise the name is the workstream-<8hex> placeholder. A name is 1-3
    words and reads as the title of the workstream (for example "Acme Bake
    Sale"). An explicit
    --name is validated against the same rule. --source-title passes a title
    the caller already resolved (dispatch.py), so nothing is fetched twice.
  - Picks a unique, topic-relevant emoji via pick-workstream-emoji.py.
  - Creates workstreams/<slug>/ with inbox/, sources/, CONTEXT.md, config.yaml.
  - Tracked files carry only the name and neutral text, never the ask, so a
    private name in an ask never reaches a published file. The verbatim ask goes to private/<date>-asks.md, which .gitignore
    keeps out (workstreams/**/private/); if that path is not ignored here the
    ask is not saved at all. The name, slug and every tracked text pass the
    private identifier list and the secret shapes first: a name that hits
    falls back to the workstream-<8hex> placeholder, and a tracked text that
    still hits is refused (exit 3) with nothing written.
  - Idempotent: if the slug already points at an existing workstream, appends a
    dated note to its private/ asks file and returns it instead of clobbering it.
  - Re-renders workstreams/README.md (unless --no-render).
  - Prints a JSON line (slug, emoji, path, created) so the caller can parse it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# Keep slugs well under the common 255-byte filesystem name limit.
MAX_SLUG_LEN = 80
# A workstream name is a title: 1-3 words.
MAX_NAME_WORDS = 3

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not installed; run: python3 -m pip install --user pyyaml\n")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "system" / "scripts"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import link_titles  # noqa: E402 (sibling module)
WORKSTREAMS_DIR = REPO_ROOT / "workstreams"
EMOJI_PICKER = SCRIPTS_DIR / "pick-workstream-emoji.py"
README_RENDERER = SCRIPTS_DIR / "render-workstreams-readme.py"

# The verbatim ask lives here, gitignored by workstreams/**/private/.
PRIVATE_DIR = "private"
# Secret shapes from owner_action_capture.SENSITIVE that scaffold text must not carry.
# The topic-word kinds (financial, health, identity words) stay out: a name such as
# "Financial Plan Review" is a fine title, and tracked text no longer holds the ask.
SCAFFOLD_SECRET_KINDS = ("masked number", "labelled number", "amount", "id shape", "long number", "token")

# Directory names under workstreams/ that are not real workstreams.
RESERVED_SLUGS = {"shared", "archive", "inbox"}

# Acronyms that should not be naively title-cased in the display name.
ACRONYMS = {"ai": "AI", "ios": "iOS", "pr": "PR",
            "sms": "SMS", "api": "API", "ui": "UI", "ux": "UX",
            "sfs": "SFS", "mb": "MB", "kpi": "KPI",
            "ama": "AMA", "pm": "PM", "os": "OS", "cta": "CTA", "gtm": "GTM",
            "mpdm": "MPDM", "prd": "PRD", "uxr": "UXR",
            "au": "AU", "us": "US", "uk": "UK", "ca": "CA", "ld": "LD",
            "faq": "FAQ", "okr": "OKR", "roi": "ROI", "sla": "SLA",
            "ssh": "SSH", "cli": "CLI", "mcp": "MCP", "llm": "LLM",
            "gsuite": "GSuite", "qa": "QA", "eod": "EOD"}

# Function words that never carry the subject of a workstream.
STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "can", "you", "please", "help", "me", "i", "we", "my", "our", "this",
    "that", "is", "are", "do", "does", "make", "set", "up", "get", "let",
    "want", "need", "would", "could", "should", "about", "into", "from",
    "it", "so", "whenever", "something", "new", "out", "at", "by", "as",
    "be", "if", "then", "also", "just", "some", "any", "all", "here",
    "there", "these", "those", "its", "his", "her", "their", "your",
    "when", "what", "which", "who", "how", "why", "will", "have", "has",
    "had", "was", "were", "been", "being", "not", "no", "yes", "ok",
    "today", "tomorrow", "tonight", "now", "later", "asap", "again",
    # the owner's own name arrives in attributions (""), never as the topic.
    "owner", "{{OWNER_USER}}",
    # Filler of a review ask ("let me know if there's anything I need to ...").
    "know", "anything", "everything", "nothing", "theres", "pre", "prereview",
    "whether", "think", "thoughts", "feedback", "quick",
    # Question openers ("What's going on with ...", "go ahead and fix this").
    "whats", "hows", "wheres", "going", "ahead",
    # Filler and narration that would name stray workstreams such as
    # "only-diagnosis-changes", "links-arent-working",
    # "might" (a Slack title), "did-happen-thought".
    "only", "arent", "isnt", "dont", "doesnt", "didnt", "wont", "cant", "couldnt", "shouldnt",
    "wasnt", "werent", "havent", "hasnt", "ive", "im", "id", "ill", "youre", "theyre", "lets",
    "thats", "verbatim", "require", "requires", "required", "personal", "might", "maybe",
    "perhaps", "probably", "likely", "did",
    "happen", "happened", "happening", "thought", "chose", "sees", "saw", "said", "says",
    "asked", "asks", "working", "works", "really", "actually", "still", "even", "already",
    "anyway", "seems", "seem", "fine", "okay", "sure", "thing", "why", "because",
    # Pronouns, auxiliaries, and narration from an ask's first words
    # ("I am going to run the <name> picnic ...").
    "am", "he", "she", "they", "him", "them", "mine", "myself", "yours", "ours", "may", "must",
    "shall", "gonna", "wanna", "available", "option", "options", "criteria", "etc", "overall",
    "other", "others", "sense", "couple", "few", "several", "lot", "lots", "much", "many", "more",
    "most", "very", "good", "best", "great", "nice", "near", "around", "while", "since", "where",
    "able", "next", "last", "first", "own", "friend", "spouse", "partner", "family", "friend", "friends",
    # Calendar words date the work; they never name it.
    "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "weekend", "week", "weeks", "month", "months", "year", "years", "night", "nights", "days",
    "hours", "minutes", "dollars", "dollar", "bucks",
    "before", "after", "under", "over", "between", "during", "within", "without", "through",
    "across", "against", "until", "per", "via", "than", "thinking", "buying", "bought",
}

# Verbs and request words: they say what to do, never what the work is about.
# A slug whose first token is one of these is prompt-derived and rejected.
VERBS = {
    "create", "fill", "write", "draft", "update", "fix", "check", "review",
    "look", "send", "add", "build", "run", "find", "schedule", "organize",
    "organise", "prepare", "prep", "plan", "summarize", "summarise", "read",
    "research", "investigate", "analyze", "analyse", "compare", "compile",
    "collect", "gather", "track", "monitor", "watch", "follow", "finish",
    "complete", "start", "kick", "kickoff", "setup", "install", "deploy",
    "ship", "post", "reply", "respond", "answer", "ask", "email", "message",
    "ping", "nudge", "remind", "book", "cancel", "move", "rename", "delete",
    "remove", "clean", "cleanup", "migrate", "convert", "export", "import",
    "share", "open", "close", "edit", "rewrite", "revise", "polish",
    "tighten", "shorten", "expand", "explain", "describe", "list", "show",
    "tell", "give", "take", "put", "use", "try", "test", "verify",
    "validate", "confirm", "decide", "pick", "choose", "think", "figure",
    "work", "handle", "process", "triage", "audit", "sync", "merge",
    "rebase", "push", "pull", "commit", "copy", "paste", "download",
    "upload", "generate", "produce", "render", "publish", "launch",
    "refresh", "rerun", "retry", "reconcile", "digest", "capture", "save",
    "file", "log", "note", "record",
    # Past tense narration ("I've built", "I thought we fixed this").
    "built", "fixed", "made", "created", "wrote", "added", "changed", "broke",
    # Common verbs of a personal ask ("going to stay", "using the program", "takes
    # credits").
    "stay", "staying", "stayed", "go", "goes", "went", "gone", "come", "coming", "came", "using",
    "used", "trying", "tried", "looking", "getting", "got", "doing", "done", "keep", "keeping",
    "see", "seeing", "seen", "like", "love", "feel", "include", "includes", "including",
    "deciding", "helps", "helping", "wants", "needs", "wanted", "needed", "renew", "buy", "sell",
    "pay", "apply", "bring", "takes", "taking", "explore", "exploring", "recommend", "suggest",
}

# Nouns that name an artifact or container, never the topic. They stay only
# when nothing more specific survives (a "doc" workstream is still better than
# a hash, but "Acme Bake Sale" is better than "Doc").
GENERIC = {
    "doc", "docs", "document", "documents", "thread", "threads", "ticket",
    "tickets", "link", "links", "url", "file", "files", "sheet", "sheets",
    "slide", "slides", "deck", "message", "messages", "note", "notes",
    "issue", "issues", "pr", "prs", "email", "emails", "page", "pages",
    "form", "forms", "request", "requests", "task", "tasks", "item",
    "items", "thing", "things", "stuff", "work", "info", "information",
    "details", "context", "version", "draft", "drafts", "copy", "text",
    "summary", "update", "updates", "response", "responses", "reply",
    "replies", "comment", "comments", "channel", "dm", "post", "posts",
    "question", "questions", "answer", "answers", "workstream", "project",
    "google", "gdoc", "spreadsheet", "presentation", "change", "changes",
}

# Leading descriptors say what kind of help the ask wants, never what the work
# is about: "Pre-review of a Google Doc", "Review", "Help me with".
# Stripped from the start of the ask (and of each line) before naming.
REVIEW_KINDS = r"(?:legal|quick|final|first|second|peer|design|copy|code|eng|engineering|security|privacy|" \
               r"compliance|brand|content|product|pm|exec|leadership|sanity)"
DESCRIPTOR_RES = (
    re.compile(r"^\s*(?:" + REVIEW_KINDS + r"[\s-]+){0,2}(?:pre[\s-]?)?review(?:s|ing)?(?:\s+(?:of|on|for))?\b",
               re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?help(?:\s+(?:me|us))?(?:\s+(?:with|on))?\b",
               re.IGNORECASE | re.MULTILINE),
)
# Phrases whose words are not stopwords alone ("call" can be a sales call).
FILLER_RES = (
    re.compile(r"\blet\s+(?:me|us)\s+know\b", re.IGNORECASE),
    re.compile(r"\bcall(?:ing|ed)?\s+out\b", re.IGNORECASE),
    re.compile(r"\bif\s+there(?:'s|\s+is|\s+are)\b", re.IGNORECASE),
    re.compile(r"\bgo\s+ahead\b", re.IGNORECASE),
    # Scope notes, never the subject: "Read-only diagnosis, no changes (the owner chose this)".
    re.compile(r"\bread[\s-]only(?:\s+(?:diagnosis|review|investigation|audit|pass|look))?\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:code\s+)?changes\b", re.IGNORECASE),
    re.compile(r"\((?:owner|{{OWNER_USER}})\s+[^()\n]{0,40}\)", re.IGNORECASE),
)
# The dispatch preamble CoS and Launchpad put before the owner's words names the
# speaker, date, time, and surface, never the work: 'the owner (<date>, CoS):
# "Looks like it failed: <Slack DM>"' must not name a placeholder like `pm-pt-cos`.
DATED_ATTRIBUTION_RE = re.compile(r"\((?=[^()\n]*\b\d{4}-\d{2}-\d{2}\b)[^()\n]*\)")
CLOCK_TIME_RE = re.compile(r"~?\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b(?:\s*(?:pt|pst|pdt|et|est|edt|utc))?", re.IGNORECASE)
# 'the owner (<date…>): "<their words>"', optionally after a "Workstream: <slug>" line.
# Single quotes too ("the owner: 'The open in obsidian links aren't working'"): the closing
# quote is one followed by a space, punctuation, or the end, so "aren't" stays inside.
# A surface label may come first: "Personal research (the owner, <date> PT): '<words>'";
# the label is the surface's framing, the owner's words name the work.
QUOTED_ASK_RE = re.compile(r"\A\s*(?:Workstream:[^\n]*\n+)?\s*(?:[A-Za-z][A-Za-z ,-]{0,60}?\(\s*)?"
                           r"(?:the owner|{{OWNER_USER}})(?:'s)?\b[^\n:\"\u201c']{0,80}:\s*"
                           r"(?:[\"\u201c](?P<quote>[^\"\u201d]+)[\"\u201d]|'(?P<squote>.+?)'(?=[\s.,;:)!?]|$))",
                           re.IGNORECASE | re.DOTALL)
# "Looks like it failed", "seems broken": a report of a failure, not its subject.
FAILURE_FILLER_RES = (
    re.compile(r"\b(?:looks|looked|seems|seemed)\s+like\b", re.IGNORECASE),
    re.compile(r"\b(?:it|this|that|something)(?:'s|\s+is|\s+has|\s+was)?\s+"
               r"(?:failed|failing|broke|broken|errored|erroring|down|crashed|stuck)\b", re.IGNORECASE),
)
FAILURE_RE = re.compile(r"\b(?:fail(?:ed|ing|ure|s)?|broke|broken|error(?:ed|s)?|crash(?:ed)?|alert|down|stuck|outage)\b",
                        re.IGNORECASE)
# A link-only failure report with no readable subject gets this neutral name
# until the launcher or the worker resolves the owner.
PLACEHOLDER_SLUG_RE = re.compile(r"(?:workstream|triage)-[0-9a-f]{8}")
# With a link in the ask and no readable title, the ask's own words name the
# work only when at least this many specific topic words survive.
MIN_WORDS_WITHOUT_TITLE = 2

# Tokens a URL leaves behind; a slug that contains one was built from a link.
URL_FRAGMENTS = {
    "https", "http", "www", "com", "org", "net", "io", "co", "html", "htm",
    "slack", "archives", "pmos", "google", "docs", "drive", "sheets",
    "document", "spreadsheets", "presentation", "edit", "view", "d", "u",
    "linear", "app", "internal-example", "github", "pull", "blob", "tree", "issues",
    "atlassian", "browse", "jira", "confluence", "wiki", "pages", "figma",
    "design", "file", "notion", "so", "airtable", "tbl", "rec", "viw",
    "tab", "gid", "usp", "sharing", "t", "p", "s",
}
URL_RE = link_titles.URL_RE
# Words a source title can end with that describe the artifact, not the topic.
TITLE_TAIL = {"guide", "doc", "document", "draft", "notes", "template", "v2",
              "v3", "copy", "final", "wip", "overview", "plan", "checklist",
              "tracker", "sheet", "deck", "brief", "memo", "faq", "faqs"}

# Source fetches (link_titles.py) are skipped offline: in tests, in dispatch
# dry runs, or when the caller sets this variable.
OFFLINE_ENV = "PM_OS_NEW_WORKSTREAM_OFFLINE"

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class NameError_(ValueError):
    """An explicit --name or a derived slug breaks the naming rule."""


def strip_urls(text: str) -> tuple[str, list[str]]:
    """Remove every URL from the ask; return (text without URLs, bare URLs)."""
    return URL_RE.sub(" ", text), link_titles.urls_in(text)


def strip_descriptors(text: str) -> str:
    """Drop leading descriptors and review and failure filler (see DESCRIPTOR_RES)."""
    for pattern in (*DESCRIPTOR_RES, *FILLER_RES, *FAILURE_FILLER_RES):
        text = pattern.sub(" ", text)
    return text


def strip_preamble(text: str) -> str:
    """Drop the dispatch boilerplate: dated attributions ("(2000-01-01
    PT, CoS)") and clock times ("~1:00 PM PT")."""
    return CLOCK_TIME_RE.sub(" ", DATED_ATTRIBUTION_RE.sub(" ", text))


def ask_core(ask: str) -> str:
    """The part of a dispatched prompt that names the work. When the prompt
    quotes the owner ('the owner (<date>, CoS): "<words>"') and those words carry a link or
    a topic word, the name comes from them alone: what follows is the surface's
    routing guesses ("Most likely candidates from tonight: ..."), never the
    subject. Otherwise the whole prompt, preamble stripped."""
    match = QUOTED_ASK_RE.search(ask)
    if match:
        quote = match.group("quote") or match.group("squote") or ""
        bare, urls = strip_urls(quote)
        if urls or topic_words(strip_descriptors(strip_preamble(bare))):
            return strip_preamble(quote)
    return strip_preamble(ask)


def _display_word(original: str, lower: str) -> str:
    """Title-case one word, honouring acronyms and mixed-case brand names
    (Netlify, iOS, AMA) the ask already spelled."""
    if lower in ACRONYMS:
        return ACRONYMS[lower]
    if original.isupper() and 2 <= len(original) <= 5:
        return original
    if original[:1].isupper() and any(c.isupper() for c in original[1:]):
        return original
    return original[:1].upper() + original[1:].lower()


def topic_words(text: str) -> list[tuple[str, str]]:
    """Return (original, lower) word pairs that can carry the subject of the
    ask: stopwords, verbs, and digits are dropped (URLs were stripped before);
    derive_name decides whether generic artifact nouns stay."""
    originals = [re.sub(r"['\u2019]s$", "", o) for o in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\u2019&]*", text)]
    pairs = [(re.sub(r"['\u2019]", "", o), re.sub(r"['\u2019]", "", o).lower()) for o in originals]
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for o, w in pairs:
        if not w or w in STOPWORDS or w in VERBS or w.isdigit() or w in seen:
            continue
        seen.add(w)
        kept.append((o, w))
    return kept


def name_from_words(words: list[tuple[str, str]], max_words: int = MAX_NAME_WORDS) -> str:
    return " ".join(_display_word(o, w) for o, w in words[:max_words])


# Ranking when an ask has more topic words than a name holds, so its first
# three words never name it by default. A proper noun (capitalized mid-sentence) outranks a
# common noun; in a long ask, a word repeated through the prompt outranks a
# word said once; ties keep the ask's order. Candidates come from the lead
# sentence, so a long list of options later in the prompt never names it.
PROPER_BONUS = 2
LONG_ASK_WORDS = 25
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’&]*")


def _norm(word: str) -> str:
    return re.sub(r"['’]", "", re.sub(r"['’]s$", "", word)).lower()


def lead_sentence(text: str) -> str:
    """The first sentence that carries at least two specific topic words, else ""."""
    for sentence in SENTENCE_RE.split(text):
        if len([w for _, w in topic_words(sentence) if w not in GENERIC]) >= 2:
            return sentence
    return ""


def topic_units(sentence: str, specific: set[str]) -> list[dict[str, Any]]:
    """Specific words of `sentence` in order, with adjacent proper nouns joined
    into one unit ("Northwind Energy", "Northwind Cloud API") so a name never keeps
    half of a brand. A word is proper when it is capitalized and not the first
    word of a sentence or clause."""
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_end, joinable = 0, False
    for match in WORD_RE.finditer(sentence):
        original = match.group(0)
        word = _norm(original)
        gap = sentence[previous_end:match.start()]
        previous_end = match.end()
        if word not in specific or word in seen:
            joinable = False
            continue
        seen.add(word)
        before = sentence[:match.start()].rstrip(" \t'\"“‘(")
        proper = original[:1].isupper() and bool(before) and not before.endswith((".", "!", "?", ":"))
        pair = (re.sub(r"['’]", "", re.sub(r"['’]s$", "", original)), word)
        if proper and joinable and units and units[-1]["proper"] and not gap.strip():
            units[-1]["words"].append(pair)
        else:
            units.append({"words": [pair], "proper": proper, "position": match.start()})
        joinable = proper
    return units


def rank_topic_words(text: str, specific: list[tuple[str, str]],
                     max_words: int = MAX_NAME_WORDS) -> list[tuple[str, str]]:
    """Pick up to `max_words` words for the name (see PROPER_BONUS above)."""
    lead = lead_sentence(text)
    pool = {w for _, w in specific}
    units = topic_units(lead, pool) if lead else []
    if not units:
        return specific[:max_words]
    counts = Counter(_norm(o) for o in WORD_RE.findall(text))
    long_ask = sum(counts[w] for w in pool) >= LONG_ASK_WORDS
    for unit in units:
        repeated = max(counts[w] for _, w in unit["words"]) if long_ask else 0
        unit["score"] = (PROPER_BONUS if unit["proper"] else 0) + repeated
    chosen: list[dict[str, Any]] = []
    room = max_words
    for unit in sorted(units, key=lambda u: (-u["score"], u["position"])):
        if room == max_words and len(unit["words"]) > max_words:
            unit = {**unit, "words": unit["words"][:max_words]}
        if len(unit["words"]) <= room:
            chosen.append(unit)
            room -= len(unit["words"])
        if not room:
            break
    return [pair for unit in sorted(chosen, key=lambda u: u["position"]) for pair in unit["words"]]


def slug_from_name(name: str, max_len: int = MAX_SLUG_LEN) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " and ")).strip("-")
    return _truncate_slug(slug, max_len).strip("-")


def validate_slug(slug: str) -> tuple[bool, str]:
    """Is this slug a 1-3 word topic title? Returns (ok, reason)."""
    if not slug or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        return False, "slug must be lowercase kebab-case"
    parts = slug.split("-")
    if PLACEHOLDER_SLUG_RE.fullmatch(slug):
        return True, "ok"
    if len(parts) > MAX_NAME_WORDS:
        return False, f"{len(parts)} words; a workstream name is 1-{MAX_NAME_WORDS} words"
    # Real topic words that a URL can also leave behind ("sharing" in file-sharing).
    hits = [p for p in parts if p in URL_FRAGMENTS and p not in {"design", "file", "tab", "sharing"} or re.fullmatch(r"p\d{6,}", p)]
    if hits:
        return False, f"contains URL fragments ({', '.join(hits)})"
    if parts[0] in VERBS or parts[0] in {"can", "please", "help"}:
        return False, f"starts with a verb or request word ({parts[0]})"
    if all(p in STOPWORDS or p in VERBS for p in parts):
        return False, "no topic word"
    return True, "ok"


def clean_source_title(title: str) -> str:
    """Turn a document, issue, or thread title into a 1-3 word topic name:
    drop bracketed years and parentheticals, drop a trailing artifact word
    ("Guide", "Draft"), then keep the first three words."""
    text = link_titles.strip_title_noise(title or "")
    text = re.sub(r"[|:/\u2013\u2014-]+", " ", text)
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'&]*", text) if not w.isdigit()]
    while len(words) > 1 and words[-1].lower() in TITLE_TAIL:
        words.pop()
    pairs = [(w, w.lower()) for w in words if w.lower() not in STOPWORDS]
    if not pairs:
        return ""
    return name_from_words(pairs)


def capitalized_run(text: str) -> str:
    """The longest capitalized run in free text, as a 1-3 word name."""
    run = link_titles.capitalized_run(text)
    pairs = [(w, w.lower()) for w in run.split() if w.lower() not in STOPWORDS]
    return name_from_words(pairs)


def source_title(urls: list[str], fetch: bool = True, titles: list[str] | None = None) -> str:
    """A 1-3 word topic name from the first linked artifact whose title passes
    the naming rule. `titles` are titles the caller already resolved (dispatch
    passes them with --source-title); otherwise link_titles reads them."""
    if titles is None:
        if not fetch or not urls:
            return ""
        titles = [item["title"] for item in link_titles.fetch_titles(urls)]
    for raw in titles:
        title = clean_source_title(raw)
        if title and validate_slug(slug_from_name(title))[0]:
            return title
    return ""


def derive_name(ask: str, fetch: bool = True, titles: list[str] | None = None) -> tuple[str, str, str]:
    """Return (name, slug, quality) for an ask. quality is one of
    "source" (title of a linked artifact), "ask" (specific words in the ask),
    "generic" (only artifact nouns were left, no link), "hash" (nothing usable).

    A linked artifact's title is read first: the link is what the ask is
    about. If no title resolves, the ask's words
    are used only when at least MIN_WORDS_WITHOUT_TITLE specific words
    survive; a link-bearing ask with fewer falls to the hash placeholder and
    dispatch logs a rename warning.

    Names come from ask_core(): the owner's quoted words when the prompt quotes
    them, with the dated preamble and clock times dropped.
    A link-only failure report ("Looks like it failed: <link>") with no
    readable title gets the neutral placeholder "Triage <8hex>"."""
    core = ask_core(ask)
    text, urls = strip_urls(core)
    text = strip_descriptors(text)
    if urls or titles:
        title = source_title(urls, fetch=fetch, titles=titles)
        if title:
            return title, slug_from_name(title), "source"
    words = topic_words(text)
    specific = [(o, w) for o, w in words if w not in GENERIC]
    enough = len(specific) >= (MIN_WORDS_WITHOUT_TITLE if urls else 1)
    if specific and enough:
        # A short ask keeps its artifact noun when it fits ("Workstream
        # Naming"); a long one drops them to make room for the topic.
        chosen = words if len(words) <= MAX_NAME_WORDS else rank_topic_words(text, specific)
        name = name_from_words(chosen)
        slug = slug_from_name(name)
        if validate_slug(slug)[0]:
            return name, slug, "ask"
    if words and not urls:
        name = name_from_words(words)
        slug = slug_from_name(name)
        if validate_slug(slug)[0]:
            return name, slug, "generic"
    digest = hashlib.sha1(ask.strip().encode("utf-8")).hexdigest()[:8]
    if urls and FAILURE_RE.search(core):
        return f"Triage {digest}", f"triage-{digest}", "hash"
    return f"Workstream {digest}", f"workstream-{digest}", "hash"


def normalize_ask(text: str) -> str:
    """Collapse line endings and strip control chars so the ask is safe to
    embed in Markdown provenance (YAML is handled separately via safe_dump)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_RE.sub("\ufffd", text)
    return text.strip()


def _truncate_slug(slug: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Bound slug length, preferring to cut at a hyphen boundary so a single
    huge token can't produce a File-name-too-long OSError."""
    if len(slug) <= max_len:
        return slug
    cut = slug[:max_len]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]  # avoid a truncated trailing word
    return cut.strip("-") or slug[:max_len].strip("-")


def slugify(text: str, fetch: bool = False) -> str:
    """Derive the kebab slug of an ask's 1-3 word title (see derive_name).
    Kept for callers that only need the slug; offline by default."""
    return derive_name(text, fetch=fetch)[1]


def kebab_name(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Normalize an explicit --name to kebab-case and check it against the
    naming rule. Raises NameError_ with the reason when it fails, so the
    caller (dispatch.py's "Workstream: <slug>" directive) falls through
    instead of scaffolding a prompt-derived or URL-derived folder."""
    slug = slug_from_name(text) if text else ""
    ok, reason = validate_slug(slug)
    if not ok:
        raise NameError_(f"--name {text!r}: {reason}")
    return slug


def pick_emoji(slug: str, body: str) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(EMOJI_PICKER), slug, body[:500]],
            capture_output=True, text=True, timeout=10,
        )
        emoji = (result.stdout or "").strip()
        if emoji:
            return emoji
    except (subprocess.SubprocessError, OSError):
        pass
    return "\U0001f4c1"  # 📁 fallback


def title_from_slug(slug: str) -> str:
    """Display name for an explicit --name slug (the derived path keeps the
    name derive_name built, which preserves brand casing from the source)."""
    parts = slug.split("-")
    return " ".join(ACRONYMS.get(p, p.capitalize()) for p in parts)


def match_terms(name: str, slug: str) -> list[str]:
    """todo_match_terms for config.yaml: the full name plus its last two
    words ("acme bake sale", "bake sale"), lowercased, deduped."""
    words = slug.split("-")
    terms = [" ".join(words)]
    if len(words) > 2:
        terms.append(" ".join(words[-2:]))
    lowered = name.lower()
    if lowered not in terms:
        terms.insert(0, lowered)
    return terms


def markdown_blockquote(text: str) -> str:
    """Blockquote every line so a multi-line/pasted ask can't break out of the
    quote or inject headings into the doc."""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) or ">"


def tracked_findings(text: str) -> list[str]:
    """Kinds of private detail in text bound for a tracked file; [] when clean.
    Names kinds only, never the value. Fails closed: a list that exists and
    cannot be read, or patterns that cannot load, count as a finding."""
    found: list[str] = []
    try:
        import private_identifiers
        listed = private_identifiers.load_list(REPO_ROOT)
        if listed is not None:
            found += sorted(f"listed {kind}" for kind in listed.kinds_in(text))
    except Exception:  # noqa: BLE001  (ListUnreadable or an import failure)
        found.append("identifier list unreadable")
    try:
        import owner_action_capture
        patterns = {k: owner_action_capture.SENSITIVE[k] for k in SCAFFOLD_SECRET_KINDS}
    except Exception:  # noqa: BLE001
        return found + ["secret patterns unavailable"]
    probe = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?\b", " ", text)
    return found + [kind for kind, pattern in patterns.items() if pattern.search(probe)]


def placeholder_name(ask: str) -> tuple[str, str, str]:
    digest = hashlib.sha1(ask.strip().encode("utf-8")).hexdigest()[:8]
    return f"Workstream {digest}", f"workstream-{digest}", "hash"


def private_path_ignored(path: Path) -> bool:
    """True when git ignores `path` in this checkout, so the ask never lands."""
    try:
        done = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", str(path)],
                              capture_output=True, timeout=10, check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    return done.returncode == 0


def save_private_ask(folder: Path, today: str, heading: str, ask_quote: str, title: str) -> str:
    """Append the verbatim ask to private/<date>-asks.md. Returns the
    folder-relative path, or "" when private/ is not gitignored here."""
    note = folder / PRIVATE_DIR / f"{today}-asks.md"
    if not private_path_ignored(note):
        sys.stderr.write(f"{PRIVATE_DIR}/ is not gitignored in {REPO_ROOT}; the verbatim ask was not saved\n")
        return ""
    note.parent.mkdir(parents=True, exist_ok=True)
    with note.open("a", encoding="utf-8") as handle:
        if handle.tell() == 0:
            handle.write(f"# {title} — asks (private, gitignored)\n")
        handle.write(f"\n## {heading}\n{ask_quote}\n")
    return f"{PRIVATE_DIR}/{note.name}"


def render_readme() -> None:
    try:
        subprocess.run([sys.executable, str(README_RENDERER), "--write"],
                       capture_output=True, timeout=30, check=False)
    except (subprocess.SubprocessError, OSError):
        pass


def read_existing_name(folder: Path) -> str:
    cfg = folder / "config.yaml"
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
            n = data.get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
        except yaml.YAMLError:
            pass
    return ""


def read_existing_emoji(folder: Path) -> str:
    cfg = folder / "config.yaml"
    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
            e = data.get("emoji")
            if isinstance(e, str) and e.strip():
                return e.strip()
        except yaml.YAMLError:
            pass
    return "\U0001f4c1"


def emit(result: dict, human: str, json_only: bool) -> None:
    if json_only:
        print(json.dumps(result))
    else:
        print(human)
        print(json.dumps(result))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ask", help="The free-text ask that motivates the workstream")
    ap.add_argument("--name", help="Explicit kebab-case slug (else derived from ask)")
    ap.add_argument("--priority", default="medium",
                    choices=["high", "medium", "low"])
    ap.add_argument("--json", action="store_true",
                    help="Print only the JSON result line")
    ap.add_argument("--no-render", action="store_true",
                    help="Skip re-rendering workstreams/README.md")
    ap.add_argument("--no-inbox", action="store_true",
                    help="Reuse an existing workstream without appending an inbox ask; new workstreams retain their initial ask")
    ap.add_argument("--no-fetch", action="store_true",
                    help=f"Never read a linked artifact's title for the name (also ${OFFLINE_ENV}=1)")
    ap.add_argument("--source-title", action="append", default=[],
                    help="Title of a linked artifact the caller already resolved (repeatable); skips the fetch")
    ap.add_argument("--dry-run", action="store_true",
                    help="Derive and validate the name, print the result, write nothing (dispatch's ack uses this)")
    args = ap.parse_args()

    ask = normalize_ask(args.ask)
    if not ask:
        ap.error("ask must contain non-whitespace text")

    fetch = not args.no_fetch and not os.environ.get(OFFLINE_ENV)
    if args.name:
        try:
            slug = kebab_name(args.name)
        except NameError_ as exc:
            ap.error(str(exc))
        name, quality = title_from_slug(slug), "name"
    else:
        name, slug, quality = derive_name(ask, fetch=fetch, titles=[t for t in args.source_title if t.strip()] or None)
    if slug in RESERVED_SLUGS or slug.startswith("_"):
        ap.error(f"reserved workstream slug: {slug}")
    hits = tracked_findings(f"{name}\n{slug}")
    if hits:
        # The name and slug land in tracked paths; never let them carry the value.
        sys.stderr.write(f"workstream name held private detail ({', '.join(hits)}); using a placeholder name\n")
        name, slug, quality = placeholder_name(ask)

    folder = WORKSTREAMS_DIR / slug
    if args.dry_run:
        # dispatch.py's ack reports the workstream it will use or create
        # without touching workstreams/; the detached launcher scaffolds it.
        exists = (folder / "CONTEXT.md").is_file()
        result = {"slug": slug, "name": (read_existing_name(folder) if exists else "") or name,
                  "name_quality": quality, "emoji": read_existing_emoji(folder) if exists else "",
                  "path": str(folder), "created": False, "exists": exists, "dry_run": True}
        emit(result, f"would {'reuse' if exists else 'create'} workstream '{result['name']}' ({slug})", args.json)
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ask_quote = markdown_blockquote(ask)

    # --- Idempotent path: slug already exists ---
    if folder.exists():
        if not folder.is_dir():
            ap.error(f"path exists but is not a directory: {folder}")
        if not (folder / "CONTEXT.md").exists():
            ap.error(f"path exists but is not a workstream: {folder.relative_to(REPO_ROOT)}")
        action = "left inbox unchanged"
        if not args.no_inbox:
            saved = save_private_ask(folder, today, f"{now} — new ask", ask_quote,
                                     read_existing_name(folder) or name)
            action = (f"appended ask to {(folder / saved).relative_to(REPO_ROOT)}" if saved
                      else f"ask not saved: {PRIVATE_DIR}/ is not gitignored")
        result = {"slug": slug, "name": read_existing_name(folder) or name,
                  "name_quality": quality, "emoji": read_existing_emoji(folder),
                  "path": str(folder), "created": False}
        emit(result,
             f"{result['emoji']}  workstream '{slug}' already exists — {action}",
             args.json)
        return 0

    # --- Create path ---
    emoji = pick_emoji(slug, f"{name}. {ask}")
    title = name
    ask_rel = f"{PRIVATE_DIR}/{today}-asks.md"
    ask_saved = private_path_ignored(folder / ask_rel)
    where = (f"The verbatim ask is in `{ask_rel}` in this folder on the main checkout (gitignored, so a "
             f"worktree has no copy) and in the dispatching task's launch prompt."
             if ask_saved else
             f"The verbatim ask was not saved: `{PRIVATE_DIR}/` is not gitignored in this checkout. "
             f"It is in the dispatching task's launch prompt.")

    context_md = (
        f"# {title} {emoji}\n\n"
        f"## Status: Active (created on demand)\n"
        f"**Created:** {today} (spun up from a CoS ask)\n"
        f"**Last worked:** {today}\n\n"
        f"## Objective\n"
        f"Complete and verify the originating durable request. Keep this workstream as the owner for related follow-through.\n\n"
        f"## Why this exists\n"
        f"Created on demand from a CoS ask on {today}. Tracked files hold no ask text. {where}\n\n"
        f"## Open / Next Steps\n"
        f"1. Complete and verify the initial request.\n"
        f"2. Record decisions, dependencies, and follow-through here.\n\n"
        f"## Sources\n"
        f"- Drop links, docs, and threads into `sources/` or note them here.\n"
    )
    config = {
        "name": title,
        "emoji": emoji,
        # Ultra is the default for every PM OS thread.
        # Set high only when the owner explicitly chooses it for this workstream.
        "agent_mode": "ultra",
        "auto_open": False,
        "priority": args.priority,
        "queue_score_bonus": 5,
        "status": "active",
        "last_worked_on": today,
        "next_step": "Complete and verify the initial request.",
        "estimate_min": 30,
        "stale_after_days": 7,
        "presentation_format": "brief-appendix",
        "todo_match_terms": match_terms(name, slug),
        "description": f"Owns durable follow-through for this originating request ({title}).",
        "created_on_demand": {
            "captured_at": now,
            "ask_path": ask_rel if ask_saved else None,
        },
    }
    config_yaml = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    hits = tracked_findings(f"{context_md}\n{config_yaml}")
    if hits:
        sys.stderr.write(f"refused: scaffold text for tracked files holds private detail ({', '.join(hits)}); "
                         f"nothing written\n")
        return 3

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inbox").mkdir(exist_ok=True)
    (folder / "sources").mkdir(exist_ok=True)
    (folder / "CONTEXT.md").write_text(context_md, encoding="utf-8")
    (folder / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if ask_saved:
        save_private_ask(folder, today, f"{now} — created from", ask_quote, title)

    if not args.no_render:
        render_readme()

    result = {"slug": slug, "name": name, "name_quality": quality, "emoji": emoji,
              "path": str(folder), "created": True}
    emit(result,
         f"{emoji}  created workstream '{name}' ({slug}) at {folder.relative_to(REPO_ROOT)}",
         args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
