#!/usr/bin/env python3
"""owner_action_capture.py: an action a worker hands the owner becomes a deduped to-do.

Every action item for the owner goes on the to-do list. Worker reports and questions already name what the owner must do; this makes
each one a to-do at delivery time, through todo_cli.py, so the list never depends on
someone remembering to add it.

What counts (deliberately narrow, so worker chores stay off the owner's list):

  followup  A `Follow-up:` line whose subject is the owner, the one gate
            `cos_review_asks.followup_owner` that Needs-you also reads.
  must      A sentence where the owner or "you" must, or needs to, do something.
  ruling    A terminal `--needs-ruling` report, or a blocked report that lists Options.
  waiting   A live worker's `--needs-ruling` report: its turn is parked on the owner.
  question  An open `ask-cos.sh` question (no due date: most close within the hour).
  draft     A parked "Draft for ..." block, only where parked_draft_tracker.py is
            absent; a machine that has it keeps its own mirror todo.

What makes none, so the list never fills with "read its report (rt-…)" items:

  - a follow-up the report itself says is already on the to-do list;
  - an action an open to-do already names, from any source ("Send the skills.rest
    removal request" covers "the owner needs to say send for the skills.rest removal
    request after CoS shows it");
  - a follow-up tagged "(q1)" whose question the owner has already answered;
  - a done report flagged for a ruling whose ask is already a Follow-up line, because
    the ruling would repeat it under the report's first sentence.

What closes one:

  question  Answered (`ask_cos.py answered`, or a delivered steer), or the task ended,
            because its report then names the default it used.
  followup  Tagged "(q1)": that question answered. Otherwise only the owner closes it.
  waiting   A steer on the task, a continuation of it, or the task ended done, failed or
            cancelled.
  ruling    A steer on the task, or a continuation CoS dispatched for it. The report that
            raised it is the task's last word, so the row's status says nothing about
            whether the owner has ruled.
  any kind  A task-wide CoS Review complete or skip recorded after the to-do was raised.
            An app click on one card closes only that card, so it closes nothing here.
  followup, must and draft otherwise stay open until the owner closes them. A wrong
  close loses an action, which is worse than a stale line on the list.

Titles say what to do in plain words and name the topic, never a task id (AGENTS.md, names
never bare numbers). The task id stays in the context, after the name, as the pointer.

Scrubbing: a to-do title reaches a calendar hold and every digest. A private workstream
gets one title, "Review the <Workstream> report and act on it", with a pointer, and its
report text is never read into the to-do. Text that matches local/private-identifiers.txt,
or that holds a figure, an account tail, an id shape, a token, a secret word or an identity
word, gets "<Verb> a private item from the <Workstream> report" with a pointer. A money or
tax word with no figure ("pay stub", "balance", "Form 1040") is replaced by "private item"
and the rest of the action stays readable. An identifier list known to exist that cannot
be read counts as a match, the same fail-closed rule the landing guard uses.

Never blocks a report or a question: every entry point swallows its own failure. A failed
add waits in worker-dispatch/owner-action-pending/, already scrubbed, and each tick retries.

Kill switch: OWNER_ACTION_CAPTURE_DISABLED=1. Test seam: OWNER_ACTION_TODO_CLI names the
CLI. Under SWITCHBOARD_TEST_SANDBOX=1 nothing runs unless that seam names one, the same rule
that keeps a test from reaching a live tab.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import cos_review_asks  # noqa: E402
import no_window  # noqa: E402
import owner_tabs  # noqa: E402
import private_identifiers  # noqa: E402
import private_workstreams  # noqa: E402

# The owner's first name, as workers write it in reports. Default {{OWNER_FIRST}}, so the
# source machine reads reports as before; a kit install sets SWITCHBOARD_OWNER_FIRST, and empty means workers say only
# "the owner". It also keeps the kit scrub from collapsing the name into "the owner|the owner".
OWNER_FIRST = os.environ.get("SWITCHBOARD_OWNER_FIRST", "{{OWNER_FIRST}}").strip()
OWNER_FIRST_RE = re.escape(OWNER_FIRST) if OWNER_FIRST else r"(?!)"

REF_PREFIX = "switchboard://owner-action/"
TAG = "owner-action"
PT = ZoneInfo("America/Los_Angeles")
TITLE_LIMIT = 110
CONTEXT_LIMIT = 600
CLI_TIMEOUT = 120
TERMINAL = {"done", "failed", "blocked", "deferred", "cancelled"}
WAITING_SETTLED = {"done", "failed", "cancelled"}   # blocked and deferred still wait on the owner
REPORT_STATUSES = {"done", "failed", "blocked", "deferred"}
TRACKER = SCRIPTS / "parked_draft_tracker.py"

MUST = re.compile(
    # (?<!\w), not \b: the same for any name, and it also matches a raw kit's {{OWNER_FIRST}} placeholder.
    r"(?<![\"'`“‘])(?<!\w)(?P<subject>you|(?:the\s+)?owner|" + OWNER_FIRST_RE + r")(?:'ll|\s+will)?\s+(?:still\s+)?"
    r"(?:must|should(?!\s+(?:be|see|now|get|have|find|receive|notice|expect|work|pass)\b)|needs?\s+to|ha(?:s|ve)\s+to)\b"
    r"(?!\s+(?:not|never|know|be aware|be sure)\b)", re.I)
# The subject opens a clause, so "Fixed the guard so the owner must approve" and "To rerun it you
# have to pass a flag" stay off, while "After landing, you must reboot" counts. A condition or a
# quoted rule asks nothing today.
CONDITIONAL = re.compile(r"^(?:if|unless|whether|in case)\b|\brules?\b", re.I)
CLAUSE_START = re.compile(r"(?:^|[:;,]|\*\*|^[-*•])\s*(?:(?:and|but|then|so|now|next|also|first|finally)\s*)?$", re.I)
WAITING_HEAD = re.compile(r"^\s*waiting on (?:" + OWNER_FIRST_RE + r"|the owner)(?!\w)", re.I)
MAX_PENDING_ATTEMPTS = 200   # about two days of command-center ticks
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'0-9*-])|\n+")
NOT_AN_ACTION = re.compile(r"\bnothing waits\b|\bno action (?:is )?needed\b", re.I)
PLACEHOLDER = re.compile(r"^<|\.\.\.|…\s*$")
DRAFT_HEADING = re.compile(r"\bDraft for\b", re.I)
FENCE = re.compile(r"```.*?```", re.S)
QUOTES = "\"'`“‘"
# The second verb of "the owner reboots and makes the dump". A closed list, because "and tests" or
# "or files" are usually nouns.
JOINED_VERB = re.compile(
    r"\b(and|or|then)\s+(?:also\s+)?(adds|answers|approves|asks|changes|checks|clicks|confirms|decides|deletes|downloads"
    r"|makes|opens|picks|purges|reads|removes|renames|replies|reviews|rotates|runs|saves|says|sends|signs|tells|uploads|verifies)\b",
    re.I)
# "(q1)": the report ties this action to an open ask-cos question, so the answer to that question closes it.
QUESTION_TAG = re.compile(r"\s*\((q\d+)\)", re.I)
# A worker that already put the action on the list says so; capturing it again doubles the list.
ON_THE_LIST = re.compile(r"\bon (?:your|the|his|their|its) to-?do list\b", re.I)
# Money and tax words with no figure: the word is replaced, and the rest of the action stays readable.
# A figure, an account tail, an id shape, a token, a secret word or an identity word still withholds the title.
SOFT_KINDS = {"financial word"}
SOFT_PLACEHOLDER = "private item"
# Words that carry no topic, for matching one action against a to-do already on the list.
FILLER = {"a", "an", "the", "and", "or", "to", "for", "of", "from", "in", "on", "with", "at", "by", "it", "its", "after",
          "before", "then", "that", "this", "is", "are", "be", "when", "once", "owner", "cos", OWNER_FIRST.lower(), "you", "your", "his",
          "say", "tell", "whether", "about", "into", "up", "so"}
SAME_ACTION = 0.8      # share of the shorter title's topic words that must match
SAME_ACTION_FLOOR = 3  # fewer topic words than this is too little to call two actions the same
# The CoS Review card's own replies: "<task>: dispatch the follow-up", "<task>: drop the follow-up".
FOLLOWUP_HANDLED = re.compile(r"^\s*(?:rt-\w+\s*:\s*)?(?:dispatch|drop)\s+(?:the\s+)?follow-?ups?\b", re.I)
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b")
SENSITIVE: dict[str, re.Pattern[str]] = {
    "masked number": re.compile(r"(?:[…•*]+|\.{3}|\b[xX]{2,})\s?\d{2,}\b|\bending\s+(?:in\s+|with\s+)?\d{2,}", re.I),
    "labelled number": re.compile(r"\b(?:account|acct|card|pin|routing)\s*(?:number|no\.?|#)?\s*\d{2,}|\btokens?\s+\w*\d", re.I),
    "amount": re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:dollars|usd)\b|\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?k\b", re.I),
    "id shape": re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{2}-\d{7}\b"),
    "long number": re.compile(r"(?<![\d-])\d(?:[ -]?\d){6,}\b"),
    "token": re.compile(
        r"\b(?:sk[-_]|pk_live|rk_live|ghp_|gho_|xox[abprs]-|AKIA|AIza)[\w-]{6,}|\b[0-9a-f]{32,}\b"
        r"|\b(?=[A-Za-z0-9+_-]*[A-Z])(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{40,}"),
    "secret word": re.compile(
        r"\b(?:passwords?|pass ?phrases?|recovery phrases?|seed phrases?|private keys?|api keys?|secrets?|credentials?"
        r"|(?:auth|access|bearer|refresh|client|session)\s+(?:tokens?|keys?))\b", re.I),
    "identity word": re.compile(
        r"\b(?:ssns?|social security|passports?|national ids?|driver'?s licen[cs]es?|vin|ein|itin|dob|date of birth"
        r"|account numbers?|routing numbers?)\b", re.I),
    "financial word": re.compile(
        r"\b(?:balances?|net worth|checking (?:accounts?|acct)|savings|brokerage|401\(?k\)?|ira|hsa|ach|banks?|salary|mortgages?"
        r"|credit cards?|tax(?:es)?|irs|w-?[29]s?|1099s?|1040s?|k-?1s?|form\s+\d{3,5}|agi|withholding|capital gains?|roth|dividends?"
        r"|pay ?stubs?|paychecks?|refunds?|estate|beneficiar(?:y|ies))\b", re.I),
}


@dataclass
class Action:
    kind: str
    what: str
    qid: str = ""        # the ask-cos question this action answers, from a "(q1)" tag
    context: str = ""    # extra lines for the to-do context (a ruling's options and lean)

    @property
    def key(self) -> str:
        normal = " ".join(re.findall(r"[a-z0-9]+", self.what.lower()))
        digest = hashlib.sha256(normal.encode("utf-8")).hexdigest()[:10]
        return f"{self.qid}.{digest}" if self.qid else digest


def key_qid(key: str) -> str:
    """The question id a captured key carries, or ''."""
    match = re.match(r"^(q\d+)\.", key or "")
    return match.group(1) if match else ""


def now_pt() -> datetime:
    return datetime.now(PT)


def next_business_day(today: datetime | None = None) -> str:
    day = (today or now_pt()).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


def disabled() -> bool:
    return os.environ.get("OWNER_ACTION_CAPTURE_DISABLED") == "1"


def repo_root() -> Path:
    return Path(owner_tabs.REPO_ROOT)


def todo_path() -> Path:
    """Main's list, never a worktree's copy: on Windows a worktree's todo.json is a
    hard link that keeps the old contents once main's file is replaced."""
    return Path(owner_tabs.STATE_DIR) / "todo.json"


def cli_path() -> Path | None:
    forced = os.environ.get("OWNER_ACTION_TODO_CLI")
    if forced:
        return Path(forced)
    if os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
        return None
    for candidate in (Path.home() / ".agents" / "skills" / "todo" / "scripts" / "todo_cli.py",
                      repo_root() / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"):
        if candidate.exists():
            return candidate
    return None


def run_cli(*args: str) -> dict[str, Any]:
    cli = cli_path()
    if cli is None:
        return {"skipped": "no to-do CLI in this environment"}
    env = {**os.environ, "SWITCHBOARD_ROOT": str(repo_root()), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        done = subprocess.run([sys.executable, str(cli), "--path", str(todo_path()), *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"failed": type(exc).__name__}
    if done.returncode:
        return {"failed": f"exit {done.returncode}"}
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"failed": "unparsed output"}


def todo_items() -> list[dict[str, Any]]:
    try:
        data = json.loads(todo_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [item for item in items or [] if isinstance(item, dict)]


def is_open(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "").lower() not in {"done", "dropped", "cancelled", "canceled", "completed"}


def captured_items(task_id: str = "") -> list[dict[str, Any]]:
    prefix = f"{REF_PREFIX}{task_id}/" if task_id else REF_PREFIX
    return [item for item in todo_items() if str(item.get("source_ref") or "").startswith(prefix)]


def ref_parts(ref: str) -> tuple[str, str, str]:
    """(task id, kind, key) of a captured source_ref."""
    parts = ref[len(REF_PREFIX):].split("/") if ref.startswith(REF_PREFIX) else []
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else ("", "", "")


# --- what counts ----------------------------------------------------------------------

def prose_of(summary: str) -> str:
    """The report without its fenced drafts, with the one-line transport's marks as newlines."""
    return FENCE.sub(" ", (summary or "").replace(" ⏎ ", "\n"))


def quoted_at(text: str, start: int) -> bool:
    """A form named in quotes, as a report about reports does, is not an action."""
    return start > 0 and text[start - 1] in QUOTES


def is_owner_followup(what: str) -> bool:
    return bool(what) and not PLACEHOLDER.search(what) and not NOT_AN_ACTION.search(what) \
        and cos_review_asks.followup_owner(what) == "owner"


def already_tracked(prose: str, match: re.Match[str], following: int) -> bool:
    """The follow-up itself, or the next two sentences before another Follow-up line, say it is on the list."""
    tail = prose[match.end():following][:400]
    near = " ".join([match.group("what"), *[s for s in SENTENCE.split(tail) if s.strip()][:2]])
    return bool(ON_THE_LIST.search(near))


def split_question_tag(what: str) -> tuple[str, str]:
    """("the owner names the trip", "q2") from "the owner names the trip (q2)"."""
    match = QUESTION_TAG.search(what)
    if not match:
        return what, ""
    return cos_review_asks.squash(QUESTION_TAG.sub("", what)).strip().rstrip(".;,"), match.group(1).lower()


def extract_actions(summary: str, status: str = "done", needs_ruling: bool = False) -> list[Action]:
    prose = prose_of(summary)
    actions: list[Action] = []
    spans: list[tuple[int, int]] = []
    matches = list(cos_review_asks.FOLLOWUP_LINE.finditer(prose))
    for index, match in enumerate(matches):
        spans.append(match.span())
        if quoted_at(prose, match.start()):
            continue
        following = matches[index + 1].start() if index + 1 < len(matches) else len(prose)
        what = cos_review_asks.squash(match.group("what")).strip().rstrip(".;,")
        if is_owner_followup(what) and not already_tracked(prose, match, following):
            text, qid = split_question_tag(what)
            actions.append(Action("followup", text, qid=qid))
    rest = prose
    for start, end in reversed(spans):
        rest = rest[:start] + "\n" + rest[end:]
    for sentence in SENTENCE.split(rest):
        match = MUST.search(sentence)
        if not match or sentence[:match.start()].count('"') % 2:
            continue
        lead = sentence[:match.start()].strip()
        if CONDITIONAL.search(lead) or not CLAUSE_START.search(lead):
            continue
        what = cos_review_asks.squash(sentence[match.start():]).strip().rstrip(".;,")
        if what and not PLACEHOLDER.search(what) and not NOT_AN_ACTION.search(what) and not ON_THE_LIST.search(sentence):
            text, qid = split_question_tag(what)
            actions.append(Action("must", text, qid=qid))
    options = cos_review_asks.options_segment(prose)
    if needs_ruling or (status == "blocked" and (options or WAITING_HEAD.match(prose))):
        # A done report flagged for a ruling whose ask is already a Follow-up line raises that line only:
        # the ruling would repeat it under the report's first sentence, which is a status, not an action.
        if options or not actions or status not in REPORT_STATUSES:
            head = re.split(r"\bOptions\s*:", prose, maxsplit=1)[0]
            head = re.sub(r"^\s*waiting on (?:" + OWNER_FIRST_RE + r"|the owner)\s*:\s*", "", head, flags=re.I)
            sentences = [s for s in SENTENCE.split(head) if s.strip()]
            kind = "ruling" if status in REPORT_STATUSES else "waiting"
            actions.append(Action(kind, cos_review_asks.squash(sentences[0]).strip().rstrip(".;,") if sentences else "",
                                  context=ruling_context(options)))
    if DRAFT_HEADING.search(summary or "") and "```" in (summary or "") and not TRACKER.exists():
        actions.append(Action("draft", "parked draft"))
    return actions


# --- scrubbing ------------------------------------------------------------------------

def ruling_context(options: Any) -> str:
    """ "Options: (a) …; (b) …. Lean: a." for a ruling's to-do, or '' when the report lists none."""
    if not options:
        return ""
    listing = "; ".join(f"({letter}) {text}" for letter, text in options.options)
    lean = f" Lean: {options.lean.letter}." if getattr(options, "lean", None) else ""
    return f"Options: {listing}.{lean}"


def hard_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if reason not in SOFT_KINDS]


def redact_soft(text: str) -> str:
    """Money and tax words become "private item"; "a private item" keeps the article right."""
    out = SENSITIVE["financial word"].sub(SOFT_PLACEHOLDER, text or "")
    out = re.sub(rf"\ban\s+(?={SOFT_PLACEHOLDER})", "a ", out, flags=re.I)
    return re.sub(rf"(?:{SOFT_PLACEHOLDER}\s+)+{SOFT_PLACEHOLDER}", SOFT_PLACEHOLDER, out)


def topic_words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if word in FILLER or (len(word) < 3 and not word.isdigit()):
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word)
    return words


def same_action(left: str, right: str) -> bool:
    """Two wordings of one action: most topic words of the shorter title appear in the longer one."""
    a, b = topic_words(left), topic_words(right)
    shared = a & b
    return len(shared) >= SAME_ACTION_FLOOR and len(shared) / max(1, min(len(a), len(b))) >= SAME_ACTION


def listed_match(text: str) -> dict[str, Any] | None:
    """An open to-do, from any source, that already names this action."""
    return next((item for item in todo_items() if is_open(item) and same_action(text, str(item.get("title") or ""))), None)


def sensitive_reasons(text: str, root: Path | None = None) -> list[str]:
    """Why this text may not reach a to-do title or context; [] when it is clean. Names the
    kind only, so a log line never carries the value."""
    probe = ISO_DATE.sub(" ", text or "")
    reasons = [kind for kind, pattern in SENSITIVE.items() if pattern.search(probe)]
    try:
        listed = private_identifiers.load_list(root or repo_root())
        if listed is not None and listed.kinds_in(text or ""):
            reasons.append("listed identifier")
    except Exception:  # noqa: BLE001  (ListUnreadable, or anything else: fail closed)
        reasons.append("identifier list unreadable")
    return reasons


def base_verb(word: str) -> str:
    lowered = word.lower()
    if lowered == "has":
        return "have"
    if lowered == "does":
        return "do"
    if lowered.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if re.search(r"(?:ch|sh|x|ss)es$", lowered) or lowered == "goes":
        return word[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def imperative(what: str) -> str:
    """ "the owner decides whether X" reads as "Decide whether X" on a to-do list."""
    led = cos_review_asks.OWNER_LED.match(what)
    body = what[led.end():] if led else what
    if led:
        body = re.sub(r"^(?:still\s+)?(?:must|needs?\s+to|ha(?:s|ve)\s+to|should|to)\s+", "", body, flags=re.I)
        first, sep, tail = body.partition(" ")
        tail = JOINED_VERB.sub(lambda m: f"{m.group(1)} {base_verb(m.group(2))}", tail)
        body = base_verb(first) + sep + tail
    return body[:1].upper() + body[1:]


def title_of(action: Action) -> str:
    return cos_review_asks.soft_cut(imperative(action.what), TITLE_LIMIT, floor=30)


def worker_name(workstream: str) -> str:
    return owner_tabs.owner_name(workstream) if workstream else "switchboard"


def is_private(workstream: str) -> bool:
    return bool(workstream) and private_workstreams.is_private_slug(workstream, repo_root())


def lead_verb(action: Action) -> str:
    """The action's verb ("Download"), so a withheld title still says what kind of thing to do."""
    first = imperative(action.what).split(" ", 1)[0] if action.what else ""
    return first if re.fullmatch(r"[A-Z][a-z]{2,}", first) and first.lower() not in FILLER else ""


def withheld_title(name: str, verb: str = "") -> str:
    """A title for an action whose text may not be shown. Names the topic by its workstream, never a task id."""
    return f"{verb} a private item from the {name} report" if verb else f"Act on a private item from the {name} report"


def private_title(name: str) -> str:
    return f"Review the {name} report and act on it"


# --- writing --------------------------------------------------------------------------

def pending_dir() -> Path:
    return Path(owner_tabs.STATE_DIR) / "worker-dispatch" / "owner-action-pending"


def pending_path(ref: str) -> Path:
    return pending_dir() / f"{hashlib.sha256(ref.encode('utf-8')).hexdigest()[:16]}.json"


def add_result(out: dict[str, Any]) -> str:
    return "added" if out.get("added") else "duplicate" if out.get("duplicate_skipped") else out.get("skipped") or out.get("failed") or "unknown"


def add_todo(ref: str, title: str, context: str, source: str, workstream: str, due: str, allow_duplicate: bool) -> dict[str, Any]:
    """`allow_duplicate` skips the CLI's title dedupe for a to-do whose title is generic, or whose close
    depends on its own task. The source_ref check here is then the dedupe."""
    if any(str(item.get("source_ref") or "") == ref for item in todo_items()):
        return {"ref": ref, "result": "already captured"}
    args = ["add", "--title", title, "--source", source, "--source-ref", ref, "--priority", "P1", "--tag", TAG,
            "--requires-user", "true", "--context", context[:CONTEXT_LIMIT]]
    if workstream:
        args += ["--workstream", workstream]
    if due:
        args += ["--due", due]
    if not workstream and not due:
        args += ["--allow-unrouted"]
    if allow_duplicate:
        args.append("--allow-duplicate")
    result = add_result(run_cli(*args))
    if result not in {"added", "duplicate"} and cli_path() is not None:
        # A busy list or a dead CLI must not lose the action. Everything here is already scrubbed.
        path = pending_path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ref": ref, "args": args}), encoding="utf-8")
    return {"ref": ref, "result": result}


def retry_pending() -> list[dict[str, Any]]:
    directory = pending_dir()
    done: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        attempts = int(entry.get("attempts") or 0)
        if attempts >= MAX_PENDING_ATTEMPTS:
            done.append({"ref": entry.get("ref"), "result": "gave up; the file stays for a look"})
            continue
        result = "already captured" if any(str(i.get("source_ref") or "") == entry.get("ref") for i in todo_items()) \
            else add_result(run_cli(*entry.get("args") or []))
        if result in {"added", "duplicate", "already captured"}:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({**entry, "attempts": attempts + 1}), encoding="utf-8")
        done.append({"ref": entry.get("ref"), "result": result})
    return done


def question_answered(task_id: str, qid: str) -> bool:
    if not qid:
        return False
    try:
        row = row_for(task_id) or {}
    except Exception:  # noqa: BLE001
        return False
    return any(isinstance(q, dict) and q.get("id") == qid and q.get("answered") for q in row.get("worker_questions") or [])


def capture_action(action: Action, task_id: str, workstream: str) -> dict[str, Any]:
    name = worker_name(workstream)
    ref = f"{REF_PREFIX}{task_id}/{action.kind}/{action.key}"
    due = next_business_day()
    if action.kind == "draft":
        return add_todo(ref, f"Send the parked draft from the {name} worker",
                        f"The worker's report to CoS carries the draft text verbatim (report {task_id}).",
                        "worker-report", workstream, due, allow_duplicate=True)
    if question_answered(task_id, action.qid):
        return {"ref": ref, "result": "already answered in CoS"}
    ruling = action.kind in {"ruling", "waiting"}
    if ruling:
        due = ""   # usually closes the same hour; no calendar hold for it
    if not ruling and action.what:
        # Compared in memory against the unredacted wording, so nothing sensitive is stored by the check.
        existing = listed_match(imperative(action.what))
        if existing:
            return {"ref": ref, "result": "already on the list", "existing": existing.get("id")}
    reasons = sensitive_reasons(action.what) if action.what else ["no text"]
    hard = hard_reasons(reasons)
    if hard:
        return {**add_todo(ref, withheld_title(name, lead_verb(action)),
                           f"Title withheld, because the text looked sensitive. Read the {name} report in CoS (report {task_id}).",
                           "worker-report", workstream, due, allow_duplicate=True), "withheld": hard}
    what = redact_soft(action.what) if reasons else action.what
    if ruling:
        title = cos_review_asks.soft_cut(f"Decide the next step: {what[:1].lower() + what[1:]}", TITLE_LIMIT, floor=30)
    else:
        title = title_of(Action(action.kind, what))
    context = " ".join(part for part in (f"{what}.", redact_soft(action.context) if action.context else "",
                                          f"From the {name} report (report {task_id}).") if part)
    out = add_todo(ref, title, context, "worker-report", workstream, due, allow_duplicate=ruling or bool(reasons))
    return {**out, "redacted": reasons} if reasons else out


def capture_private(task_id: str, workstream: str) -> dict[str, Any]:
    name = worker_name(workstream)
    ref = f"{REF_PREFIX}{task_id}/private/report"
    pointer = (f"Report withheld, because {name} is a private workstream. Read workstreams/{workstream}/ on the main checkout (report {task_id})."
               if workstream else f"Report withheld, because its workstream could not be confirmed. Read the report in CoS (report {task_id}).")
    title = private_title(name) if workstream else "Review a worker report with private details and act on it"
    return {**add_todo(ref, title, pointer,
                       "worker-report", workstream, next_business_day(), allow_duplicate=True), "withheld": ["private workstream"]}


def resolve_workstream(task_id: str, given: str) -> tuple[str, bool]:
    """(slug, private). The ledger row outranks the caller's flag, and a private name on either side
    counts. A ledger that cannot be read counts as private, so report text never slips through."""
    try:
        row_slug = str((row_for(task_id) or {}).get("workstream") or "")
    except Exception:  # noqa: BLE001
        return given, True
    for slug in (row_slug, given):
        if is_private(slug):
            return slug, True
    slug = row_slug or given
    return slug, not slug   # a workstream nobody can name cannot be shown to be public


def capture_report(task_id: str, workstream: str, status: str, summary: str, needs_ruling: bool = False) -> dict[str, Any]:
    if disabled():
        return {"skipped": "disabled"}
    if not task_id.startswith("rt-"):
        return {"skipped": "not a worker task"}   # owner-tab mirrors and scheduled-job rows
    if status not in REPORT_STATUSES and not needs_ruling:
        return {"skipped": f"status {status}", "closed": sync_task(task_id)}
    workstream, private = resolve_workstream(task_id, workstream)
    actions = extract_actions(summary, status, needs_ruling)
    if private:
        captured = [capture_private(task_id, workstream)] if actions else []
    else:
        captured = [capture_action(action, task_id, workstream) for action in actions]
    return {"captured": captured, "closed": sync_task(task_id, keep={str(c.get("ref")) for c in captured})}


def capture_question_soon(task: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Detached, so a busy to-do lock never holds up a worker asking a question. A named stub runs in line."""
    if disabled():
        return {"skipped": "disabled"}
    if os.environ.get("OWNER_ACTION_TODO_CLI"):
        return capture_question(task, entry)
    if cli_path() is None:
        return {"skipped": "no to-do CLI in this environment"}
    spawn_self(["question", "--task-id", str(task.get("id")), "--qid", str(entry.get("id"))])
    return {"spawned": True}


def capture_question(task: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    if disabled():
        return {"skipped": "disabled"}
    task_id, slug = str(task.get("id") or ""), str(task.get("workstream") or "")
    qid = str(entry.get("id") or "")
    ref = f"{REF_PREFIX}{task_id}/question/{qid}"
    name = worker_name(slug)
    private_question = f"Answer the {name} worker's private question in CoS"
    if is_private(slug):
        return add_todo(ref, private_question,
                        f"Question withheld, because {name} is a private workstream. Read it in CoS (task {task_id}, {qid}).",
                        "worker-question", slug, "", allow_duplicate=True)
    blob = " ".join([str(entry.get("question") or ""), *map(str, entry.get("options") or []), str(entry.get("default") or "")])
    reasons = sensitive_reasons(blob)
    if hard_reasons(reasons):
        return {**add_todo(ref, private_question,
                           f"Question withheld, because the text looked sensitive. Read it in CoS (task {task_id}, {qid}).",
                           "worker-question", slug, "", allow_duplicate=True), "withheld": hard_reasons(reasons)}
    clean = redact_soft if reasons else (lambda text: text)
    question = clean(" ".join(str(entry.get("question") or "").split()))
    options = [clean(str(option)) for option in entry.get("options") or []]
    default = clean(str(entry.get("default") or ""))
    title = cos_review_asks.soft_cut(f"Answer the {name} worker: {question}", TITLE_LIMIT, floor=30)
    parts = [question]
    if options:
        parts.append("Options: " + "; ".join(options) + ".")
    if default:
        parts.append(f"The worker continues on: {default}.")
    parts.append(f"Answer in CoS (task {task_id}, {qid}).")
    return add_todo(ref, title, " ".join(parts), "worker-question", slug, "", allow_duplicate=True)


# --- closing --------------------------------------------------------------------------

def close(item: dict[str, Any], reason: str) -> dict[str, Any]:
    out = run_cli("complete", "--id", str(item.get("id")), "--reason", reason)
    return {"ref": item.get("source_ref"), "reason": reason, "closed": bool(out.get("completed"))}


def row_for(task_id: str) -> dict[str, Any] | None:
    ledger = owner_tabs._ledger()
    return ledger.find_task(ledger.load_payload(), task_id)


def continued(task_id: str) -> bool:
    """CoS dispatched a continuation of this task, so the owner's answer reached a worker. A supplemental
    row runs beside the task and answers nothing; a cancelled or failed one never carried the answer."""
    try:
        payload = owner_tabs._ledger().load_payload()
    except Exception:  # noqa: BLE001
        return False
    rows = payload.get("tasks") if isinstance(payload, dict) else payload
    return any(isinstance(r, dict) and r.get("continuation_of") == task_id and not r.get("supplemental")
               and str(r.get("status") or "") not in {"cancelled", "failed"} for r in rows or [])


def row_workstream(task_id: str) -> str:
    try:
        return str((row_for(task_id) or {}).get("workstream") or "")
    except Exception:  # noqa: BLE001
        return ""


def review_actions() -> dict[str, Any]:
    path = Path(os.environ.get("COS_REVIEW_ACTIONS_FILE") or Path(owner_tabs.STATE_DIR) / "cos-review-actions.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else {}
    return items if isinstance(items, dict) else {}


def parse_when(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def close_reason(item: dict[str, Any], kind: str, key: str, row: dict[str, Any] | None, review: dict[str, Any], task_id: str) -> str:
    action = review.get(f"task:{task_id}") or {}
    if isinstance(action, dict) and action.get("action") in {"complete", "skip"} and action.get("scope") == "task":
        acted, raised = parse_when(action.get("at")), parse_when(item.get("created_at"))
        if acted and raised and acted >= raised:
            return f"CoS Review {action.get('action')}"
    if row is None:
        return ""
    status = str(row.get("status") or "")
    asked = key if kind == "question" else key_qid(key)
    if kind in {"followup", "must"} and asked:
        question = next((q for q in row.get("worker_questions") or [] if isinstance(q, dict) and q.get("id") == asked), None)
        if question and question.get("answered"):
            return "answered in CoS"
    if kind in {"ruling", "waiting"} and row.get("_continued"):
        return "CoS dispatched a continuation, which carries the answer"
    if kind == "question":
        question = next((q for q in row.get("worker_questions") or [] if isinstance(q, dict) and q.get("id") == key), None)
        if question and question.get("answered"):
            return "answered in CoS"
        if status in TERMINAL:
            return "worker finished; its report names the default it used"
    if kind == "waiting" and status in WAITING_SETTLED:
        return f"task ended {status}"
    return ""


def sync_task(task_id: str, keep: set[str] | None = None, steered: bool = False, followup_handled: bool = False) -> list[dict[str, Any]]:
    """Close what this task's state has settled. `keep` holds refs raised by the report in hand."""
    if disabled():
        return []
    items = [item for item in captured_items(task_id) if is_open(item) and str(item.get("source_ref")) not in (keep or set())]
    if not items:
        return []
    row, review = row_for(task_id), review_actions()
    if row is not None:
        row = {**row, "_continued": continued(task_id)}
    closed = []
    for item in items:
        _, kind, key = ref_parts(str(item.get("source_ref")))
        reason = close_reason(item, kind, key, row, review, task_id)
        if not reason and steered and kind in {"ruling", "waiting"}:
            reason = "answered by a steer"
        if not reason and followup_handled and kind in {"followup", "must"}:
            reason = "follow-up dispatched or dropped in CoS"
        if reason:
            closed.append(close(item, reason))
    return closed


def reconcile(limit: int = 25) -> dict[str, Any]:
    """Each command-center tick, so a task that ended with no report still closes its to-dos."""
    if disabled():
        return {"skipped": "disabled"}
    retried = retry_pending()
    task_ids = sorted({ref_parts(str(item.get("source_ref")))[0] for item in captured_items() if is_open(item)} - {""})
    closed: list[dict[str, Any]] = []
    for task_id in task_ids:
        if len(closed) >= limit:
            break
        closed += sync_task(task_id)
    return {"open_tasks": len(task_ids), "closed": closed, **({"retried": retried} if retried else {})}


def spawn_self(args: list[str]) -> None:
    """The same detach the report waiter uses, because nohup alone dies with the tool call on macOS.
    On Windows a hidden console, never DETACHED_PROCESS: a console-less child opens a terminal
    window for every git call it makes."""
    log_path = Path(owner_tabs.STATE_DIR) / "worker-dispatch" / "owner-actions.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True,
                                  **no_window.detached_kwargs()}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args], **kwargs)


def spawn_sync(task_id: str, steered: bool = False, followup_handled: bool = False) -> None:
    """Detached, so a steer returns at once. Nothing runs where no CLI may."""
    if disabled() or cli_path() is None:
        return
    args = ["sync", "--task-id", task_id]
    if steered:
        args.append("--steered")
    if followup_handled:
        args.append("--followup-handled")
    spawn_self(args)


def note_steer(task_id: str, text: str) -> None:
    spawn_sync(task_id, steered=True, followup_handled=bool(FOLLOWUP_HANDLED.match(text or "")))


# --- command line ---------------------------------------------------------------------

def command_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.detach and not os.environ.get("OWNER_ACTION_TODO_CLI"):
        child = ["report", "--task-id", args.task_id, "--workstream", args.workstream or "", "--status", args.status,
                 "--summary-file", args.summary_file, "--delete-summary"]
        if args.needs_ruling:
            child.append("--needs-ruling")
        spawn_self(child)
        return {"task_id": args.task_id, "spawned": True}
    summary_path = Path(args.summary_file) if args.summary_file else None
    try:
        summary = summary_path.read_text(encoding="utf-8") if summary_path else (args.summary or "")
        return {"task_id": args.task_id, **capture_report(args.task_id, args.workstream or "", args.status, summary, args.needs_ruling)}
    finally:
        if summary_path and args.delete_summary:
            summary_path.unlink(missing_ok=True)


def command_question(args: argparse.Namespace) -> dict[str, Any]:
    task = row_for(args.task_id) or {}
    entry = next((q for q in task.get("worker_questions") or [] if isinstance(q, dict) and q.get("id") == args.qid), None)
    return capture_question(task, entry) if entry else {"skipped": "no such question"}


def command_sync(args: argparse.Namespace) -> dict[str, Any]:
    return {"closed": sync_task(args.task_id, steered=args.steered, followup_handled=args.followup_handled)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--task-id", required=True)
    report.add_argument("--workstream", default="")
    report.add_argument("--status", required=True)
    report.add_argument("--needs-ruling", action="store_true")
    report.add_argument("--summary-file", default="")
    report.add_argument("--summary", default="")
    report.add_argument("--detach", action="store_true", help="return at once; a child does the work (a named stub runs in line)")
    report.add_argument("--delete-summary", action="store_true")
    report.set_defaults(func=command_report)
    question = sub.add_parser("question")
    question.add_argument("--task-id", required=True)
    question.add_argument("--qid", required=True)
    question.set_defaults(func=command_question)
    sync = sub.add_parser("sync")
    sync.add_argument("--task-id", required=True)
    sync.add_argument("--steered", action="store_true")
    sync.add_argument("--followup-handled", action="store_true")
    sync.set_defaults(func=command_sync)
    rec = sub.add_parser("reconcile")
    rec.set_defaults(func=lambda _args: reconcile())
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001  (never blocks a report; names the kind only, never the text)
        result = {"failed": type(exc).__name__}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    no_window.install()
    sys.exit(main(sys.argv[1:]))
