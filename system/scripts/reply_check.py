#!/usr/bin/env python3
"""Already-answered pre-check for every comms surface.

Before CoS, the start-day digest, the CEO review, the
next-up digest, the comms-triage draft list, or a todo reminder asks the owner to
reply to, send, or approve something, the live source thread is re-read. An
ask the owner already answered (or that someone else resolved) is rendered as
"already answered <when>" or dropped, never as an ask.

One check per source kind, all read-only:

  slack  -> check-thread-resolution.py (its cache, the collectors' shared
            Slack backoff and the hourly gate budget; nothing here calls the
            Slack CLI directly)
  gmail  -> `pmos mail read` (thread message ids, then the
            headers of the messages newer than the source message)
  gdoc   -> `pmos drive comment` (comment + replies)
  github -> `gh api graphql` for PRs (state, mergeQueueEntry, autoMergeRequest,
            latest reviews, merge-queue timeline) / `gh issue view --json`
            (live state: a MERGED or CLOSED PR/issue, or a PR in the merge
            queue, is "already resolved"; a note that recorded it as
            unmerged is never evidence)

Results are cached per canonical source ref in .state/reply-check-cache.json
for REPLY_CHECK_TTL_MIN minutes (default 45) so the 60 s fastpath sweep, the
30-min comms review, the next-up digest and start-day all read one warm cache
instead of each re-reading the provider. Failures back off per ref
(10 min doubling to 2 h) and rate limits back off per provider in
.state/reply-check-backoff.json (5 min doubling to 30 min). Never wrap the
CLIs in an ad-hoc retry loop instead: repeated retries trip Slack's rate limit
for every collector.

CLI (read-only unless noted):
  reply_check.py --ref URL [--force]        one verdict as JSON
  reply_check.py --draft N [--force]        verdict for a held draft
  reply_check.py --pending [--live]         every held draft plus every open
                                            todo whose source_ref is a GitHub
                                            PR/issue (cache unless --live)
  reply_check.py sweep [--max N] [--budget S] [--every MIN]
      warm the cache for held drafts and mark answered ones superseded in the
      ledger (writes the ledger; runs from run-comms-fastpath.sh)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from repo_paths import main_checkout  # noqa: E402

REPO_ROOT = main_checkout(SCRIPT_DIR.parents[1])
CACHE_PATH = Path(os.environ.get("REPLY_CHECK_CACHE", REPO_ROOT / ".state" / "reply-check-cache.json"))
BACKOFF_PATH = Path(
    os.environ.get("REPLY_CHECK_BACKOFF", REPO_ROOT / ".state" / "reply-check-backoff.json")
)
SWEEP_STAMP = Path(os.environ.get("REPLY_CHECK_SWEEP_STAMP", REPO_ROOT / ".state" / "reply-check-sweep.json"))
LOG_FILE = REPO_ROOT / "system" / "logs" / "reply-check.log"
SLACK_GATE_SCRIPT = SCRIPT_DIR / "check-thread-resolution.py"
LEDGER_SCRIPT = SCRIPT_DIR / "triage_ledger.py"
GMAIL_SEARCH_SCRIPT = SCRIPT_DIR / "gmail_search.py"

TTL_SECONDS = max(1, int(os.environ.get("REPLY_CHECK_TTL_MIN", "45"))) * 60
FAIL_BACKOFF_BASE_SECONDS = 10 * 60
FAIL_BACKOFF_MAX_SECONDS = 2 * 60 * 60
SURFACE_BACKOFF_BASE_SECONDS = 5 * 60
SURFACE_BACKOFF_MAX_SECONDS = 30 * 60
OFFLINE = os.environ.get("REPLY_CHECK_OFFLINE", "") == "1" or os.environ.get("THREAD_GATE_OFFLINE", "") == "1"
CLI_TIMEOUT_SECONDS = float(os.environ.get("REPLY_CHECK_CLI_TIMEOUT", "40"))
GMAIL_CMD = ["pmos", "mail", "read"]
DRIVE_CMD = ["pmos", "drive", "comment"]
GH_CMD = ["gh"]
# PR state is read over GraphQL: `gh pr view --json` has no mergeQueueEntry, so
# the REST field set could not tell a PR sitting in the merge queue from one
# waiting for the owner to press merge.
GH_PR_QUERY = """query($owner:String!,$name:String!,$number:Int!) { repository(owner:$owner,name:$name) { pullRequest(number:$number) {
  state url title isDraft mergedAt closedAt mergedBy { login } author { login } mergeStateStatus reviewDecision
  mergeQueueEntry { position state enqueuedAt }
  autoMergeRequest { enabledAt enabledBy { login } }
  reviewRequests(first:30) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug combinedSlug } } } }
  latestReviews(first:20) { nodes { author { login } state submittedAt } }
  timelineItems(last:10, itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT, MERGED_EVENT]) { nodes {
    __typename ... on AddedToMergeQueueEvent { createdAt enqueuer { login } }
    ... on RemovedFromMergeQueueEvent { createdAt actor { login } } ... on MergedEvent { createdAt actor { login } } } }
} } }"""
GH_ISSUE_FIELDS = "state,closedAt,stateReason,title,url,author"
TODO_PATH = Path(os.environ.get("PM_OS_TODO_JSON") or REPO_ROOT / "system" / "state" / "todo.json")
OPEN_TODO_STATUSES = frozenset({"open", "actionable", "waiting", "scheduled"})
PT = ZoneInfo("America/Los_Angeles")

# Owner identity: match by Slack user id, Slack
# username and email identities only. Display names ("the owner") are never an
# identity: anyone can set one, and matching them produced false positives.
# Same env names as check-thread-resolution.py so one override covers both.
OWNER_SLACK_USER_ID = os.environ.get("OWNER_SLACK_USER_ID", "")
OWNER_SLACK_USERNAMES = {u.strip().lower() for u in os.environ.get("OWNER_SLACK_USERNAMES", "{{OWNER_USER}}").split(",") if u.strip()}
OWNER_EMAILS = {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "{{OWNER_EMAIL}}").split(",") if e.strip()}
# GitHub login (priority-channels.yaml github_pr_reviews.author); a PR
# merged by the owner is resolved_by_owner, anyone else's merge is resolved_by_other.
OWNER_GITHUB_LOGINS = {u.strip().lower() for u in os.environ.get("OWNER_GITHUB_LOGINS", "{{OWNER_USER}}").split(",") if u.strip()}
# Review-ask roles. Only `requested-reviewer` is an
# ask; `team-fanout` (a team the owner is on was requested, but the team is not in
# priority-channels.yaml github_pr_reviews.review_teams), `author`, and `none`
# are status lines. Unverifiable (no live read) is never an ask.
REVIEW_ROLE_ASK = "requested-reviewer"
REVIEW_ROLES = (REVIEW_ROLE_ASK, "team-fanout", "author", "none")
PRIORITY_CHANNELS_PATH = Path(os.environ.get("PM_OS_PRIORITY_CHANNELS") or REPO_ROOT / "system" / "config" / "priority-channels.yaml")

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

def review_teams(path: Path | None = None) -> set[str]:
    """Teams (`org/slug`, lowercased) whose review request counts as an explicit ask to the owner.

    Read from priority-channels.yaml `github_pr_reviews.review_teams`; empty by
    default, so a team request is fan-out unless the owner lists the team.
    """
    target = path or PRIORITY_CHANNELS_PATH
    try:
        import yaml  # local: reply_check is imported by scripts that never need yaml

        data = yaml.safe_load(target.read_text()) or {}
    except (OSError, ValueError, ImportError):
        return set()
    teams = ((data.get("github_pr_reviews") or {}) if isinstance(data, dict) else {}).get("review_teams") or []
    return {str(t).strip().lower() for t in teams if str(t).strip()} if isinstance(teams, list) else set()


def review_role(data: dict[str, Any], *, teams: set[str] | None = None) -> dict[str, Any]:
    """the owner's role on a PR from its GraphQL node: requested reviewer, team fan-out, author, or none."""
    allow = review_teams() if teams is None else teams
    author = str((data.get("author") or {}).get("login") or "")
    users: list[str] = []
    team_slugs: list[str] = []
    for node in ((data.get("reviewRequests") or {}).get("nodes") or []):
        reviewer = (node or {}).get("requestedReviewer") if isinstance(node, dict) else None
        if not isinstance(reviewer, dict):
            continue
        if reviewer.get("__typename") == "Team" or reviewer.get("slug"):
            slug = str(reviewer.get("combinedSlug") or reviewer.get("slug") or "")
            if slug:
                team_slugs.append(slug)
        elif reviewer.get("login"):
            users.append(str(reviewer["login"]))
    direct = any(u.lower() in OWNER_GITHUB_LOGINS for u in users)
    via_team = sorted(t for t in team_slugs if t.lower() in allow)
    if direct or via_team:
        role = REVIEW_ROLE_ASK
    elif author.lower() in OWNER_GITHUB_LOGINS:
        role = "author"  # the owner's own PR: never a review ask, whoever else was requested
    elif team_slugs:
        role = "team-fanout"
    else:
        role = "none"
    return {
        "author": author,
        "review_requests": users + team_slugs,
        "owner_role": role,
        "owner_review_requested": role == REVIEW_ROLE_ASK,
        "review_ask_via": "login" if direct else (f"team {','.join(via_team)}" if via_team else ""),
    }
# Reactions that mean "handled" rather than merely "seen" (mirrors
# triage_ledger.DRAFT_DISMISS_REACTIONS): 👀 and 👍 never retire a reply ask.
HANDLED_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark", "ballot_box_with_check"})
ANSWERED_VERDICTS = frozenset({"resolved_by_owner", "resolved_by_other", "resolved_by_reaction"})
KINDS = ("slack", "gmail", "gdoc", "github")
# GitHub verdict reasons that mean the PR/issue itself is finished, or (pr-queued)
# that the merge is already in GitHub's hands: a merge ask is resolved either way.
GITHUB_RESOLVED_REASONS = frozenset({"pr-merged", "pr-closed", "issue-closed", "pr-queued"})

_RATE_LIMIT_HINTS = (
    "status: 429", "http 429", "code 429", "error 429", "ratelimited", "rate_limit", "rate limit",
    "ratelimitexceeded", "userratelimitexceeded", "quota exceeded", "quotaexceeded", "too many requests",
)
_GMAIL_REF_RE = re.compile(r"mail\.google\.com/.*#(?:inbox|all|sent|starred|imp|label/[^/]+)/([0-9a-f]{10,})")
_GMAIL_IDS_RE = re.compile(r"^Message IDs:\s*(.*)$", re.MULTILINE)
_GDOC_REF_RE = re.compile(r"docs\.google\.com/(document|presentation|spreadsheets)/d/([A-Za-z0-9_-]+)")
_DISCO_RE = re.compile(r"[?&]disco=([A-Za-z0-9_-]+)")
# PR or issue URL, with or without a trailing /files, ?query or #fragment
# (#issuecomment-…, #discussion_r…, #pullrequestreview-…).
_GITHUB_REF_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/(\d+)(?!\d)", re.I)

_slack_gate: Any | None = None
_ledger: Any | None = None
_gmail_search: Any | None = None
_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None


# --- small shared helpers -------------------------------------------------


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")
    except OSError:
        pass


def _load_script(path: Path, name: str) -> Any | None:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def slack_gate() -> Any | None:
    global _slack_gate
    if _slack_gate is None:
        _slack_gate = _load_script(SLACK_GATE_SCRIPT, "pm_os_reply_check_slack_gate")
    return _slack_gate


def ledger() -> Any:
    global _ledger
    if _ledger is None:
        _ledger = _load_script(LEDGER_SCRIPT, "pm_os_reply_check_ledger")
    return _ledger


def gmail_search() -> Any:
    global _gmail_search
    if _gmail_search is None:
        _gmail_search = _load_script(GMAIL_SEARCH_SCRIPT, "pm_os_reply_check_gmail_search")
    return _gmail_search


def canonical_key(ref: str) -> str:
    target = github_target({"ref": ref})
    if target is not None:
        # One cache entry per PR/issue, whatever comment or tab the ref pointed at.
        return target["url"]
    return str(ledger().canonicalize_ref(ref or ""))


def _now_epoch(now: float | None = None) -> float:
    if now is not None:
        return now
    # Same frozen clock as the triage ledger (tests and replays set it).
    override = os.environ.get("TRIAGE_LEDGER_NOW")
    if override:
        parsed = parse_iso(override)
        if parsed:
            return parsed.timestamp()
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_iso(value: Any) -> str:
    parsed = parse_iso(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else str(value or "")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _os_replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def is_owner_sender(value: Any) -> bool:
    """True when a From header / sender string carries one of the owner's email identities.

    A bare display name ("the owner") is not an identity and never matches.
    """
    low = str(value or "").strip().lower()
    if not low:
        return False
    return any(email in OWNER_EMAILS for email in _EMAIL_RE.findall(low))


def is_owner_slack_user(user_id: Any = None, username: Any = None) -> bool:
    uid = str(user_id or "").strip()
    name = str(username or "").strip().lower()
    return (bool(uid) and uid == OWNER_SLACK_USER_ID) or (bool(name) and name in OWNER_SLACK_USERNAMES)


def own_message_author(item: dict[str, Any]) -> str | None:
    """Which identity proves the item's source message is the owner's own, or None.

    Reads the author fields comms candidates and ledger items carry
    (``from_user_id`` / ``from_handle`` for Slack, ``from`` / ``from_email``
    for Gmail). ``from`` display names are ignored on purpose.
    """
    inbound = item.get("inbound") or {}
    user_id = item.get("from_user_id") or inbound.get("from_user_id")
    handle = item.get("from_handle") or inbound.get("from_handle")
    if is_owner_slack_user(user_id, None):
        return f"slack user id {str(user_id).strip()}"
    if is_owner_slack_user(None, handle):
        return f"slack username {str(handle).strip().lower()}"
    for value in (item.get("from_email"), inbound.get("from_email"), item.get("from"), inbound.get("from")):
        if is_owner_sender(value):
            emails = [e for e in _EMAIL_RE.findall(str(value).lower()) if e in OWNER_EMAILS]
            return f"email {emails[0]}"
    return None


# --- verdicts ---------------------------------------------------------------


def _verdict(
    ref: str,
    kind: str,
    verdict: str,
    *,
    source: str = "live",
    evidence: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    epoch = _now_epoch(now)
    evidence = dict(evidence or {})
    if not evidence.get("reason"):
        # Every verdict explains itself; provider-specific code sets precise
        # reasons, this is the floor for failure/unknown paths.
        if verdict == "unknown":
            evidence["reason"] = "check-failed"
            evidence.setdefault("why", f"could not check the source: {evidence.get('note') or 'unknown error'}")
        elif verdict == "open":
            evidence["reason"] = "no-reply-after"
            evidence.setdefault("why", "no message from the owner after the ask")
    return {
        "ref": ref,
        "kind": kind,
        "verdict": verdict,
        "source": source,
        "evidence": {k: v for k, v in evidence.items() if v not in (None, "")},
        "checked_at": _iso(epoch),
        "checked_at_epoch": epoch,
    }


def is_answered(verdict: dict[str, Any] | None, *, for_draft: bool = True) -> bool:
    """True when the ask behind ``verdict`` no longer needs the owner.

    ``for_draft`` keeps the draft rule: only a ✅-style reaction retires a
    reply draft, because 👀/👍 mean "seen", not "answered". Todo and plan
    callers pass ``for_draft=False`` and accept any resolving reaction, the
    same way the thread gate does.
    """
    if not verdict:
        return False
    kind = str(verdict.get("verdict") or "")
    if kind in {"resolved_by_owner", "resolved_by_other"}:
        return True
    if kind == "resolved_by_reaction":
        if not for_draft:
            return True
        reaction = str((verdict.get("evidence") or {}).get("reaction") or "").split("::")[0]
        return reaction in HANDLED_REACTIONS
    return False


def dismiss_reason(verdict: dict[str, Any] | None) -> str | None:
    """Ledger dismissal reason for an answered verdict, or None to keep the draft."""
    if not is_answered(verdict, for_draft=True):
        return None
    kind = str((verdict or {}).get("verdict") or "")
    if kind == "resolved_by_owner":
        return "owner-already-replied"
    if kind == "resolved_by_other":
        return "resolved-by-other"
    return "owner-marked-resolved"


def answered_label(verdict: dict[str, Any] | None) -> str:
    """Human line for a digest: 'already answered Oct 5 6:17 PM PT' etc."""
    if not verdict:
        return ""
    evidence = verdict.get("evidence") or {}
    when = parse_iso(evidence.get("closer_at"))
    if when is None and evidence.get("closer_ts"):
        try:
            when = datetime.fromtimestamp(float(str(evidence["closer_ts"]).split("::")[0]), timezone.utc)
        except (TypeError, ValueError):
            when = None
    local = when.astimezone(PT) if when else None
    stamp = f"{local:%b} {local.day} {local.hour % 12 or 12}:{local:%M %p} PT" if local else "earlier"
    kind = str(verdict.get("verdict") or "")
    reason = str(evidence.get("reason") or "")
    if reason == "pr-merged":
        return f"already merged by {evidence.get('closer') or 'someone'} {stamp}"
    if reason == "pr-queued":
        position = evidence.get("queue_position")
        where = f"position {position}, " if position not in (None, "") else ""
        return f"queued for merge ({where}since {stamp})"
    if reason in ("pr-closed", "issue-closed"):
        return f"already closed {stamp}" + (" (not merged)" if reason == "pr-closed" else "")
    if kind == "resolved_by_owner":
        if evidence.get("reason") == "own-message":
            return f"the owner's own message {stamp}, not an ask"
        return f"already answered {stamp}"
    if kind == "resolved_by_other":
        closer = str(evidence.get("closer") or "someone else")
        return f"resolved by {closer} {stamp}"
    if kind == "resolved_by_reaction":
        reaction = str(evidence.get("reaction") or "reaction").split("::")[0]
        return f"already marked :{reaction}: {stamp}"
    return ""


# --- source identification -------------------------------------------------


def item_ref(item: dict[str, Any]) -> str:
    return str(item.get("draft_source_link") or item.get("source_ref") or item.get("ref") or "").strip()


def source_kind(item: dict[str, Any]) -> str | None:
    """Which provider owns the source message behind a ledger item or candidate."""
    ref = item_ref(item)
    declared = str(item.get("source") or item.get("source_type") or "").lower()
    meta = item.get("draft_meta") or {}
    if "slack.com/archives/" in ref or ref.startswith("slack://dm/") or declared == "slack":
        return "slack"
    if _GITHUB_REF_RE.search(ref):
        return "github"
    if "docs.google.com/" in ref and _DISCO_RE.search(ref):
        return "gdoc"
    if (
        "mail.google.com/" in ref
        or declared == "gmail"
        or meta.get("gmail_thread_id")
        or meta.get("reply_to_message_id")
    ):
        return "gmail"
    return None


def gmail_target(item: dict[str, Any]) -> tuple[str, str]:
    """(thread_id, source_message_id) for a Gmail-backed item; '' when unknown."""
    meta = item.get("draft_meta") or {}
    inbound = item.get("inbound") or {}
    thread_id = str(
        meta.get("gmail_thread_id") or meta.get("thread_id") or item.get("thread_id")
        or inbound.get("thread_id") or ""
    )
    message_id = str(meta.get("reply_to_message_id") or item.get("message_id") or inbound.get("message_id") or "")
    if not message_id:
        match = _GMAIL_REF_RE.search(item_ref(item))
        message_id = match.group(1) if match else ""
    return thread_id, message_id


def gdoc_target(item: dict[str, Any]) -> tuple[str, str]:
    """(file_id, comment_id) for a Doc/Slides/Sheets comment item; '' when unknown."""
    ref = item_ref(item)
    file_match = _GDOC_REF_RE.search(ref)
    disco = _DISCO_RE.search(ref)
    file_id = str(item.get("doc_id") or (file_match.group(2) if file_match else ""))
    comment_id = str(item.get("comment_id") or (disco.group(1) if disco else ""))
    return file_id, comment_id


def github_target(item: dict[str, Any]) -> dict[str, str] | None:
    """{repo, kind (pull|issues), number, url} for a GitHub PR/issue ref, else None."""
    match = _GITHUB_REF_RE.search(item_ref(item))
    if not match:
        return None
    owner, repo, kind, number = match.groups()
    kind = kind.lower()
    return {
        "repo": f"{owner}/{repo}",
        "kind": kind,
        "number": number,
        "url": f"https://github.com/{owner}/{repo}/{kind}/{number}",
    }


def source_seen_at(item: dict[str, Any]) -> datetime | None:
    """When PM OS first saw the source message (the floor for 'after')."""
    inbound = item.get("inbound") or {}
    for value in (inbound.get("received_at"), item.get("received_at"), item.get("first_seen"), item.get("draft_held_at")):
        parsed = parse_iso(value)
        if parsed:
            return parsed
    return None


# --- provider calls, backoff --------------------------------------------------


def _default_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout or "", result.stderr or ""


def set_runner(runner: Callable[[list[str], float], tuple[int, str, str]] | None) -> None:
    """Tests inject a fake CLI; production uses subprocess."""
    global _runner
    _runner = runner


def _looks_rate_limited(blob: str) -> bool:
    low = (blob or "").lower()
    return any(hint in low for hint in _RATE_LIMIT_HINTS)


def surface_backoff_remaining(kind: str, now: float | None = None) -> int:
    entry = _read_json(BACKOFF_PATH).get(kind) or {}
    try:
        next_allowed = float(entry.get("next_allowed_at") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(next_allowed - _now_epoch(now) + 0.999))


def record_surface_backoff(kind: str, blob: str = "", now: float | None = None) -> int:
    """Persist a provider rate limit so every reply_check caller waits."""
    state = _read_json(BACKOFF_PATH)
    entry = state.get(kind) if isinstance(state.get(kind), dict) else {}
    epoch = _now_epoch(now)
    try:
        previous_until = float(entry.get("next_allowed_at") or 0)
    except (TypeError, ValueError):
        previous_until = 0.0
    # A limit hit soon after the last one escalates; an old strike resets.
    recent = previous_until > epoch - 2 * SURFACE_BACKOFF_MAX_SECONDS
    strikes = int(entry.get("strikes") or 0) + 1 if recent else 1
    wait = min(SURFACE_BACKOFF_MAX_SECONDS, SURFACE_BACKOFF_BASE_SECONDS * (2 ** (strikes - 1)))
    state[kind] = {
        "next_allowed_at": epoch + wait,
        "strikes": strikes,
        "recorded_at": _iso(epoch),
        "last_error": (blob or "")[:200],
    }
    _write_json(BACKOFF_PATH, state)
    log(f"{kind} rate-limited; backoff {wait}s (strike {strikes})")
    return wait


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


def provider_call(kind: str, cmd: list[str], *, timeout: float | None = None, now: float | None = None) -> str:
    """Run one read-only CLI call inside the per-provider backoff. Returns stdout."""
    if OFFLINE:
        raise ProviderError("offline")
    remaining = surface_backoff_remaining(kind, now=now)
    if remaining > 0:
        raise ProviderError(f"{kind} backoff active ({remaining}s)", rate_limited=True)
    runner = _runner or _default_runner
    rc, out, err = runner(cmd, timeout or CLI_TIMEOUT_SECONDS)
    blob = f"{out} {err}"
    if rc != 0:
        if _looks_rate_limited(blob):
            wait = record_surface_backoff(kind, blob, now=now)
            raise ProviderError(f"{kind} rate-limited; backoff {wait}s", rate_limited=True)
        raise ProviderError(f"{kind} rc={rc}: {(err or out)[:200]}")
    return out


# --- per-kind live checks -----------------------------------------------------


def _check_slack(ref: str, *, use_cache: bool, force: bool, now: float | None) -> dict[str, Any]:
    gate = slack_gate()
    if gate is None:
        return _verdict(ref, "slack", "unknown", source="error", evidence={"note": "thread gate unavailable"}, now=now)
    result = gate.check_ref(ref, use_cache=use_cache, force=force)
    evidence = dict(result.get("evidence") or {})
    if evidence.get("closer_ts") and not evidence.get("closer_at"):
        try:
            evidence["closer_at"] = _iso(float(str(evidence["closer_ts"]).split("::")[0]))
        except (TypeError, ValueError):
            pass
    verdict = _verdict(
        ref, "slack", str(result.get("verdict") or "unknown"),
        source=str(result.get("source") or "live"), evidence=evidence, now=now,
    )
    # The gate's thread excerpt feeds the classifier (no_ask / settled / stale).
    if isinstance(result.get("thread"), list):
        verdict["thread"] = result["thread"]
    return verdict


def _gmail_thread_message_ids(thread_id: str, *, now: float | None = None) -> list[str]:
    out = provider_call("gmail", GMAIL_CMD + ["--thread-ids", thread_id, "--max-body-chars", "0"], now=now)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProviderError("gmail returned non-JSON") from exc
    text = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise ProviderError("gmail returned no result text")
    if payload.get("isError") or payload.get("error"):
        raise ProviderError(f"gmail error: {text[:200]}", rate_limited=_looks_rate_limited(text))
    match = _GMAIL_IDS_RE.search(text)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _gmail_headers(message_ids: list[str], *, now: float | None = None) -> list[dict[str, Any]]:
    if not message_ids:
        return []
    out = provider_call("gmail", GMAIL_CMD + ["--message-ids", *message_ids, "--max-body-chars", "0"], now=now)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProviderError("gmail returned non-JSON") from exc
    text = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise ProviderError("gmail returned no result text")
    rows, _token = gmail_search().parse_search_text(text)
    return rows


def _gmail_permalink(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def _check_gmail(item: dict[str, Any], ref: str, *, now: float | None) -> dict[str, Any]:
    thread_id, source_id = gmail_target(item)
    if not source_id:
        return _verdict(ref, "gmail", "unknown", source="error", evidence={"note": "no source message id"}, now=now)
    if not thread_id:
        rows = _gmail_headers([source_id], now=now)
        thread_id = str(rows[0].get("threadId") or "") if rows else ""
        if not thread_id:
            return _verdict(ref, "gmail", "unknown", source="error", evidence={"note": "no thread id"}, now=now)
    ids = _gmail_thread_message_ids(thread_id, now=now)  # newest first
    if source_id not in ids:
        return _verdict(
            ref, "gmail", "unknown", source="live",
            evidence={"note": "source message not in thread", "thread_id": thread_id}, now=now,
        )
    newer = ids[: ids.index(source_id)]
    if not newer:
        # One call only when nothing is newer (budget rule from the original
        # pre-check); an the owner-sent source with no replies is caught earlier by
        # own_message_author when the item carries its author.
        return _verdict(
            ref, "gmail", "open",
            evidence={"thread_id": thread_id, "reason": "no-reply-after", "why": "no newer message in the thread after the ask"},
            now=now,
        )
    # Fetch the source message with the newer ones (same call): when the owner sent
    # it, it is not an ask.
    headers = _gmail_headers([*newer[-15:], source_id], now=now)
    by_id = {str(row.get("id")): row for row in headers}

    def _is_owner_row(row: dict[str, Any]) -> bool:
        labels = {str(label).upper() for label in (row.get("labels") or [])}
        return is_owner_sender(row.get("from")) or "SENT" in labels

    source_row = by_id.get(source_id)
    if source_row and _is_owner_row(source_row):
        return _verdict(
            ref, "gmail", "resolved_by_owner",
            evidence={
                "closer": "the owner",
                "closer_at": _to_iso(source_row.get("date")),
                "closer_permalink": _gmail_permalink(source_id),
                "thread_id": thread_id,
                "reason": "own-message",
                "why": f"the source message is from the owner ({str(source_row.get('from') or 'SENT')}); the owner's messages are never asks",
            },
            now=now,
        )
    latest_other: dict[str, Any] | None = None
    for message_id in reversed(newer):  # oldest newer message first
        row = by_id.get(message_id)
        if not row:
            continue
        labels = {str(label).upper() for label in (row.get("labels") or [])}
        if "DRAFT" in labels:
            continue  # an unsent draft (possibly ours) is not a reply
        if _is_owner_row(row):
            closer_at = _to_iso(row.get("date"))
            return _verdict(
                ref, "gmail", "resolved_by_owner",
                evidence={
                    "closer": "the owner",
                    "closer_at": closer_at,
                    "closer_permalink": _gmail_permalink(message_id),
                    "thread_id": thread_id,
                    "reason": "owner-replied-after",
                    "why": f"the owner sent a reply in this thread ({closer_at or 'later'}) after the ask",
                },
                now=now,
            )
        latest_other = row
    if latest_other:
        closer = re.sub(r"\s*<.*?>", "", str(latest_other.get("from") or "")).strip()
        return _verdict(
            ref, "gmail", "teammate_replied",
            evidence={
                "closer": closer,
                "closer_at": _to_iso(latest_other.get("date")),
                "closer_permalink": _gmail_permalink(str(latest_other.get("id") or "")),
                "thread_id": thread_id,
                "reason": "teammate-replied-after",
                "why": f"{closer or 'someone else'} replied after the ask but the owner has not sent anything since; still pending",
            },
            now=now,
        )
    return _verdict(
        ref, "gmail", "open",
        evidence={"thread_id": thread_id, "reason": "no-reply-after", "why": "nothing but unsent drafts after the ask; the owner has not replied"},
        now=now,
    )


def _gdoc_entries(comment: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    root_author = comment.get("author") or {}
    entries.append(
        {
            "id": str(comment.get("id") or ""),
            "me": bool(root_author.get("me")),
            "author": str(root_author.get("displayName") or ""),
            "at": parse_iso(comment.get("created_time") or comment.get("createdTime")),
            "deleted": bool(comment.get("deleted")),
        }
    )
    for reply in comment.get("replies") or []:
        if not isinstance(reply, dict):
            continue
        author = reply.get("author") or {}
        entries.append(
            {
                "id": str(reply.get("id") or ""),
                "me": bool(author.get("me")),
                "author": str(author.get("displayName") or ""),
                "at": parse_iso(reply.get("createdTime") or reply.get("created_time")),
                "deleted": bool(reply.get("deleted")),
            }
        )
    return [entry for entry in entries if entry["at"] is not None and not entry["deleted"]]


def _check_gdoc(item: dict[str, Any], ref: str, *, now: float | None) -> dict[str, Any]:
    file_id, comment_id = gdoc_target(item)
    if not file_id or not comment_id:
        return _verdict(ref, "gdoc", "unknown", source="error", evidence={"note": "no file/comment id"}, now=now)
    out = provider_call(
        "gdoc",
        DRIVE_CMD + ["--file-id-or-url", file_id, "--comment-id", comment_id, "--no-include-quoted-content"],
        now=now,
    )
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProviderError("drive returned non-JSON") from exc
    comment = payload.get("comment") if isinstance(payload, dict) else None
    if not isinstance(comment, dict):
        blob = json.dumps(payload)[:200] if isinstance(payload, dict) else str(payload)[:200]
        raise ProviderError(f"drive returned no comment: {blob}", rate_limited=_looks_rate_limited(blob))
    entries = _gdoc_entries(comment)
    if not entries:
        return _verdict(ref, "gdoc", "unknown", source="live", evidence={"note": "comment has no entries"}, now=now)
    seen = source_seen_at(item)
    others = [entry for entry in entries if not entry["me"]]
    permalink = ref
    if not others:
        # Every entry is the owner's (author.me): the comment is the owner's own, not an ask.
        last = entries[-1]
        return _verdict(
            ref, "gdoc", "resolved_by_owner",
            evidence={
                "closer": "the owner",
                "closer_at": _iso(last["at"].timestamp()),
                "closer_permalink": permalink,
                "reason": "own-message",
                "why": "every entry on this comment is the owner's own; the owner's messages are never asks",
            },
            now=now,
        )
    # The source message is the newest non-the owner entry PM OS had seen when the
    # item was captured; everything after it is "after the source message".
    floor_candidates = [e for e in others if seen is None or e["at"] <= seen] or others
    floor = max((e["at"] for e in floor_candidates), default=entries[0]["at"])
    owner_after = [e for e in entries if e["me"] and e["at"] > floor]
    others_after = [e for e in others if e["at"] > floor]
    if owner_after:
        last = owner_after[-1]
        closer_at = _iso(last["at"].timestamp())
        return _verdict(
            ref, "gdoc", "resolved_by_owner",
            evidence={
                "closer": "the owner",
                "closer_at": closer_at,
                "closer_permalink": permalink,
                "reason": "owner-replied-after",
                "why": f"the owner replied on this comment ({closer_at}) after the ask",
            },
            now=now,
        )
    if comment.get("resolved") is True:
        closer = others_after[-1]["author"] if others_after else "someone"
        return _verdict(
            ref, "gdoc", "resolved_by_other",
            evidence={
                "closer": closer,
                "closer_at": _to_iso(comment.get("modified_time") or comment.get("modifiedTime")),
                "closer_permalink": permalink,
                "note": "comment resolved",
                "reason": "comment-resolved",
                "why": f"the comment thread was marked resolved ({closer}); nothing left for the owner",
            },
            now=now,
        )
    if others_after:
        last = others_after[-1]
        return _verdict(
            ref, "gdoc", "teammate_replied",
            evidence={
                "closer": last["author"],
                "closer_at": _iso(last["at"].timestamp()),
                "closer_permalink": permalink,
                "reason": "teammate-replied-after",
                "why": f"{last['author']} replied after the ask but the owner has not; still pending",
            },
            now=now,
        )
    return _verdict(
        ref, "gdoc", "open",
        evidence={"reason": "no-reply-after", "why": "no reply from the owner on this comment after the ask and it is not resolved"},
        now=now,
    )


def _gh_json(args: list[str], *, now: float | None) -> dict[str, Any]:
    out = provider_call("github", GH_CMD + args, now=now)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProviderError("gh returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("gh returned no object")
    return payload


def _check_github(item: dict[str, Any], ref: str, *, now: float | None) -> dict[str, Any]:
    """Live PR/issue state. MERGED/CLOSED is resolved; OPEN stays an ask.

    The ask behind a PR ref ("ping X to merge", "review this", "rebase") ends
    when the PR itself does, whoever closed it, so a merge by anyone is
    ``resolved_by_other`` (``resolved_by_owner`` when the owner merged it) with
    reason ``pr-merged``; a close without merge is ``pr-closed``. An OPEN PR
    sitting in the merge queue is ``pr-queued`` (resolved: GitHub will merge
    it, so "merge now?" is never asked; the enqueuer decides by-the owner/other).
    Auto-merge enabled on an unqueued PR is evidence only; the ask stands
    until the PR is actually queued or merged. Comments on an OPEN PR are
    not read here: the PR review loop owns those.
    """
    target = github_target(item)
    if target is None:
        return _verdict(ref, "github", "unknown", source="error", evidence={"note": "no PR/issue number"}, now=now)
    label = f"{target['repo']}#{target['number']}"
    if target["kind"] == "pull":
        owner, name = target["repo"].split("/", 1)
        payload = _gh_json(["api", "graphql", "-f", f"query={GH_PR_QUERY}", "-F", f"owner={owner}",
                            "-F", f"name={name}", "-F", f"number={target['number']}"], now=now)
        if payload.get("errors"):
            raise ProviderError(f"gh graphql errors for {label}: {str(payload['errors'])[:200]}")
        data = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        if not isinstance(data, dict) or not data:
            raise ProviderError(f"gh returned no pull request for {label}")
    else:
        data = _gh_json(["issue", "view", target["url"], "--json", GH_ISSUE_FIELDS], now=now)
    state = str(data.get("state") or "").upper()
    if not state:
        raise ProviderError(f"gh returned no state for {label}")
    base = {"repo": target["repo"], "number": target["number"], "state": state, "closer_permalink": target["url"]}
    if target["kind"] == "pull":
        approvers = sorted({
            str((r.get("author") or {}).get("login") or "")
            for r in ((data.get("latestReviews") or {}).get("nodes") or [])
            if isinstance(r, dict) and str(r.get("state") or "").upper() == "APPROVED" and (r.get("author") or {}).get("login")
        })
        if approvers:
            base["approvers"] = approvers
        # the owner's role on the PR rides along with every verdict so a surface
        # can tell a review ask (requested-reviewer) from the owner's own PR or team
        # fan-out without a second gh read.
        base.update(review_role(data))
    if state == "MERGED":
        merged_by = data.get("mergedBy") or {}
        login = str(merged_by.get("login") or "")
        merged_at = _to_iso(data.get("mergedAt"))
        by_owner = login.lower() in OWNER_GITHUB_LOGINS
        return _verdict(
            ref, "github", "resolved_by_owner" if by_owner else "resolved_by_other",
            evidence={
                **base,
                "closer": "the owner" if by_owner else (login or "someone"),
                "closer_login": login,
                "closer_at": merged_at,
                "reason": "pr-merged",
                "why": f"{label} is MERGED (by {login or 'unknown'} at {merged_at or 'unknown time'}); nothing left to ask",
            },
            now=now,
        )
    if state == "CLOSED":
        closed_at = _to_iso(data.get("closedAt"))
        is_pr = target["kind"] == "pull"
        detail = str(data.get("stateReason") or "").lower().replace("_", " ")
        return _verdict(
            ref, "github", "resolved_by_other",
            evidence={
                **base,
                "closer": "",
                "closer_at": closed_at,
                "reason": "pr-closed" if is_pr else "issue-closed",
                "why": (
                    f"{label} is CLOSED without merge ({closed_at or 'unknown time'}); the ask is moot"
                    if is_pr else
                    f"{label} is CLOSED ({detail or 'no reason'}, {closed_at or 'unknown time'}); the ask is moot"
                ),
            },
            now=now,
        )
    if state != "OPEN":
        raise ProviderError(f"gh returned unexpected state {state!r} for {label}")
    merge_state = str(data.get("mergeStateStatus") or "")
    review = str(data.get("reviewDecision") or "")
    draft = bool(data.get("isDraft"))
    queue = data.get("mergeQueueEntry")
    queue_failed = isinstance(queue, dict) and str(queue.get("state") or "").upper() == "UNMERGEABLE"
    if isinstance(queue, dict) and not queue_failed:
        # Queued: GitHub merges it when the queue reaches it. The enqueuer is
        # the last AddedToMergeQueueEvent actor (the entry itself has none).
        # An UNMERGEABLE entry is on its way out of the queue (queue checks
        # failed), so that one stays an open ask below.
        enqueuer = ""
        for item_ in reversed((data.get("timelineItems") or {}).get("nodes") or []):
            if isinstance(item_, dict) and item_.get("__typename") == "AddedToMergeQueueEvent":
                enqueuer = str((item_.get("enqueuer") or {}).get("login") or "")
                break
        queued_at = _to_iso(queue.get("enqueuedAt"))
        position = queue.get("position")
        by_owner = enqueuer.lower() in OWNER_GITHUB_LOGINS
        return _verdict(
            ref, "github", "resolved_by_owner" if by_owner else "resolved_by_other",
            evidence={
                **base,
                "closer": "the owner" if by_owner else (enqueuer or "someone"),
                "closer_login": enqueuer,
                "closer_at": queued_at,
                "queue_position": position,
                "queue_state": str(queue.get("state") or ""),
                "merge_state": merge_state,
                "review_decision": review,
                "reason": "pr-queued",
                "why": f"{label} is OPEN but in the merge queue (position {position if position is not None else '?'}, "
                       f"queued by {enqueuer or 'unknown'} at {queued_at or 'unknown time'}); GitHub merges it, nothing to ask",
            },
            now=now,
        )
    auto = data.get("autoMergeRequest") if isinstance(data.get("autoMergeRequest"), dict) else None
    bits = [b for b in (
        "draft" if draft else "",
        f"review {review}" if review else "",
        f"merge state {merge_state}" if merge_state else "",
        "merge queue entry UNMERGEABLE (queue checks failed; it will be dequeued)" if queue_failed else "",
        f"auto-merge enabled by {(auto.get('enabledBy') or {}).get('login') or 'someone'}" if auto else "",
    ) if b]
    evidence = {
        **base,
        "merge_state": merge_state,
        "review_decision": review,
        "is_draft": draft,
        "reason": "pr-open" if target["kind"] == "pull" else "issue-open",
        "why": f"{label} is still OPEN" + (f" ({', '.join(bits)})" if bits else "") + "; the ask stands",
    }
    if auto:
        evidence["auto_merge"] = {"enabled_at": _to_iso(auto.get("enabledAt")),
                                  "enabled_by": str((auto.get("enabledBy") or {}).get("login") or "")}
    return _verdict(ref, "github", "open", evidence=evidence, now=now)


# --- classification -----------------------------------------------------------
#
# The trailing-reply check above answers "was this replied to?". The
# classification answers "does this need a reply at all?":
#
#   answered  a later reply, reaction, merge, or resolution exists (is_answered)
#   no_ask    the source message is an FYI, acknowledgement, status update, or
#             emoji-level reply with no question or request for the owner or their team
#   settled   the owner or anyone resolved the matter in the thread (or, for a
#             top-level @mention, in a later top-level post of the same channel)
#   stale     older than STALE_BUSINESS_DAYS business days with no later nudge
#   open      a real unanswered ask (the only outcome a surface may present)
#
# Heuristics run first on the item's own text and the gate's thread excerpt;
# an optional AI pass (REPLY_CHECK_AI=1, headless amp) may only move an
# "open" item to no_ask or settled, never the other way.

CLASSIFICATIONS = ("answered", "no_ask", "settled", "stale", "open")
NON_OPEN_CLASSIFICATIONS = frozenset({"answered", "no_ask", "settled", "stale"})
STALE_BUSINESS_DAYS = int(os.environ.get("REPLY_CHECK_STALE_BUSINESS_DAYS", "5"))
AI_CLASSIFY_ENABLED = os.environ.get("REPLY_CHECK_AI", "") == "1"
AI_CLASSIFY_TIMEOUT_SECONDS = float(os.environ.get("REPLY_CHECK_AI_TIMEOUT", "90"))
AI_CLASSIFY_MAX_PER_RUN = int(os.environ.get("REPLY_CHECK_AI_MAX", "2"))
AI_SETTINGS_FILE = Path(os.environ.get("REPLY_CHECK_AI_SETTINGS", Path.home() / ".config" / "amp" / "settings-headless.json"))
_ai_calls_this_run = {"n": 0}

# Three mention shapes: Slack raw `<@>`, the collector's bare `@`,
# and a display handle `@{{OWNER_USER}}` / `@Milan Vale` (one optional capitalised surname).
_MENTION_RE = re.compile(
    r"<@([A-Z0-9]+)(?:\|[^>]*)?>|@([UW][A-Z0-9]{6,})\b|@([A-Za-z][\w.'-]*(?: [A-Z][\w.'-]+)?)"
)
_REQUEST_RE = re.compile(
    r"\b(?:can|could|would|will|did|do|does|should|have|has|are|is)\s+(?:you|u|we|ya'?ll|y'all)\b"
    r"|\b(?:please|pls|plz|ptal|lmk|wdyt|thoughts\??|any update|take a look|let me know|need(?:s)? (?:you|your)"
    r"|sign[- ]?off|stamp|approve|review this|confirm|heads up needed|can someone|anyone know|does anyone)\b",
    re.I,
)
_SETTLE_RE = re.compile(
    r"\b(?:sounds good|will do|done|thanks|thank you|got it|resolved|fixed|merged|shipped|updated|handled|all set|"
    r"nvm|never ?mind|plan is|let'?s go with|we'?ll go with|agreed|closing this|taken care of|no longer needed)\b",
    re.I,
)
_ACK_ONLY_RE = re.compile(
    r"^\W*(?:ok(?:ay)?|k|kk|got it|thanks?|thank you|ty|tysm|sounds good|will do|done|cool|great|nice|perfect|"
    r"noted|ack|\+1|👍|🙏|✅|lgtm|sg|yep|yup|yes|no|nope|fyi)[\W\d]*$",
    re.I,
)
_BOT_AUTHORS = {"buildbot", "github", "linear", "jira", "launchdarkly", "datadog", "jenkins"}


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole PT business days from ``start``'s date to ``end``'s date."""
    s = start.astimezone(PT).date()
    e = end.astimezone(PT).date()
    if e <= s:
        return 0
    from datetime import timedelta

    days = 0
    cursor = s
    while cursor < e:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _strip_quoted_context(text: str) -> str:
    """The comms collectors append quoted context (a PR body, the reply that
    followed) after a ``---`` line; only the part before it is the source message."""
    return str(text or "").split("\n---\n", 1)[0].strip()


def source_text_is_verbatim(item: dict[str, Any]) -> bool:
    """True when ``source_message_text`` returns the message's own words, not a
    collector summary or title. Only verbatim text can be judged an FYI."""
    context = item.get("inbound_context") or {}
    messages = [m for m in (context.get("messages") or []) if isinstance(m, dict)]
    if any(str(m.get("text") or "").strip() for m in messages if not m.get("me")):
        return True
    inbound = item.get("inbound") or {}
    return bool(context.get("text") or inbound.get("body") or inbound.get("text"))


# A collector summary or paraphrase describes the ask in the third person ("Juniper asked for…").
_SUMMARY_ASK_RE = re.compile(r"\b(?:ask(?:s|ed|ing)?|request(?:s|ed)?|wants?|needs?|question|ping(?:s|ed)?|flag(?:s|ged)?|waiting on)\b", re.I)


def source_message_text(item: dict[str, Any]) -> str:
    """The text of the message the draft would answer, without appended context."""
    context = item.get("inbound_context") or {}
    messages = [m for m in (context.get("messages") or []) if isinstance(m, dict)]
    text = ""
    # The source is the first message the owner did not write: a thread the owner
    # started (Doc comment root, `me: true`) is judged by the reply to it.
    for msg in messages:
        if msg.get("me"):
            continue
        text = str(msg.get("text") or "")
        break
    # A collector row whose text is only a shared link (Slack unfurl) carries
    # the real words in ``inbound_context.text``.
    if text and not re.search(r"[A-Za-z]{2,}", re.sub(r"<[^>]*>|https?://\S+|\([^)]*\)", " ", _strip_quoted_context(text))):
        text = ""
    if not text:
        inbound = item.get("inbound") or {}
        for candidate in (context.get("text"), inbound.get("body"), inbound.get("text"), item.get("summary"), item.get("title")):
            if candidate:
                text = str(candidate)
                break
    return _strip_quoted_context(text)


def source_author(item: dict[str, Any]) -> str:
    context = item.get("inbound_context") or {}
    inbound = item.get("inbound") or {}
    return str(context.get("author") or inbound.get("from") or item.get("from") or "").strip()


def mentions_in(text: str) -> set[str]:
    """Every @mention in ``text``: user ids and display handles, lower-cased."""
    found: set[str] = set()
    for uid, bare_uid, handle in _MENTION_RE.findall(str(text or "")):
        if uid or bare_uid:
            found.add(uid or bare_uid)
        elif handle:
            found.add(handle.strip().lower())
    return found


def mentions_owner(text: str) -> bool:
    found = mentions_in(text)
    if OWNER_SLACK_USER_ID in found:
        return True
    return any(h in OWNER_SLACK_USERNAMES or h in {"owner", "owner example"} for h in found if not h.startswith("U"))


def _all_mentions_text(item: dict[str, Any]) -> str:
    context = item.get("inbound_context") or {}
    messages = context.get("messages") or []
    parts = [source_message_text(item), _strip_quoted_context(str(context.get("text") or ""))]
    if messages and isinstance(messages[0], dict):
        parts.append(_strip_quoted_context(str(messages[0].get("text") or "")))
    return "\n".join(parts)


def asks_something(text: str) -> bool:
    """True when ``text`` carries a question or a request verb."""
    body = str(text or "")
    if "?" in body:
        return True
    return bool(_REQUEST_RE.search(body))


def _is_bot_author(item: dict[str, Any], thread: list[dict[str, Any]]) -> bool:
    author = source_author(item).lower()
    if author and (author in _BOT_AUTHORS or author.endswith(" bot") or author.endswith("bot")):
        return True
    if thread and thread[0].get("is_bot"):
        return True
    return False


def _source_epoch_from_ref(ref: str) -> float | None:
    gate = slack_gate()
    if gate is None:
        return None
    parsed = gate.parse_permalink(ref)
    if not parsed:
        return None
    try:
        return float(parsed[1])
    except (TypeError, ValueError):
        return None


def source_message_at(item: dict[str, Any], ref: str) -> datetime | None:
    """When the source message was posted (permalink ts, then the collector's stamps)."""
    context = item.get("inbound_context") or {}
    messages = context.get("messages") or []
    if messages and isinstance(messages[0], dict) and not messages[0].get("me"):
        parsed = parse_iso(messages[0].get("at"))
        if parsed:
            return parsed
    epoch = _source_epoch_from_ref(ref)
    if epoch:
        return datetime.fromtimestamp(epoch, timezone.utc)
    return source_seen_at(item)


def _excerpt_ts(msg: dict[str, Any]) -> float:
    """Epoch seconds of a thread-excerpt entry (``ts`` like ``1700000000.000100``)."""
    try:
        return float(str(msg.get("ts") or "0"))
    except (TypeError, ValueError):
        return 0.0


def _thread_after_source(verdict: dict[str, Any], source_at: datetime | None) -> list[dict[str, Any]]:
    thread = verdict.get("thread") or []
    if not isinstance(thread, list):
        return []
    if source_at is None:
        return [m for m in thread if isinstance(m, dict)]
    floor = source_at.timestamp()
    return [m for m in thread if isinstance(m, dict) and _excerpt_ts(m) > floor + 0.5]


def thread_source_message(verdict: dict[str, Any] | None, ref: str) -> dict[str, Any] | None:
    """The source message's own entry in the verdict's thread excerpt.

    The ``--ref`` path (and any caller that hands over a bare ``{"ref": ...}``)
    carries no ``inbound_context``, so the classifier used to judge the thread
    with an empty source text: ``directed_at_owner`` was False and a teammate
    answering their own side question made the whole ask "settled". The gate
    already read the thread; the entry whose ``ts`` is the permalink's is the
    source message. No match (another provider, an excerpt cut at 30 entries)
    returns None rather than guessing at a neighbour.
    """
    if not verdict or not isinstance(verdict.get("thread"), list):
        return None
    epoch = _source_epoch_from_ref(ref)
    if not epoch:
        return None
    for msg in verdict["thread"]:
        if isinstance(msg, dict) and abs(_excerpt_ts(msg) - epoch) < 0.0005:
            return msg
    return None


def effective_source_text(
    item: dict[str, Any], verdict: dict[str, Any] | None, ref: str
) -> tuple[str, str, bool, dict[str, Any] | None]:
    """(source text, text to scan for @mentions, verbatim?, thread entry used or None).

    The item's own ``inbound_context`` wins; a bare item falls back to the
    verdict's thread excerpt, whose text is the message's own words.
    """
    text = source_message_text(item)
    if text:
        return text, _all_mentions_text(item), source_text_is_verbatim(item), None
    msg = thread_source_message(verdict, ref)
    if msg and str(msg.get("text") or "").strip():
        from_thread = _strip_quoted_context(str(msg.get("text") or ""))
        return from_thread, from_thread, True, msg
    return "", _all_mentions_text(item), False, None


def source_directed_at_owner(item: dict[str, Any], verdict: dict[str, Any] | None, ref: str | None = None) -> bool:
    """True when the source message addresses the owner (``directed_at_me``, an
    @mention of the owner, or @channel/@here), reading the thread excerpt when the
    item carries no text of its own."""
    if item.get("directed_at_me"):
        return True
    _text, mention_text, _verbatim, _msg = effective_source_text(item, verdict, ref or item_ref(item))
    if mentions_owner(mention_text):
        return True
    return bool({"channel", "here", "everyone"} & mentions_in(mention_text))


def nudged_at(item: dict[str, Any]) -> datetime | None:
    """When the asker nudged outside the thread (``nudged_at`` on the item: a
    Jira transition, a DM, a calendar hold). ``reply_check.py --ref … --nudged-at``
    sets it by hand; collectors may set it from their own signals."""
    return parse_iso(item.get("nudged_at")) if item.get("nudged_at") else None


def needs_human_look(item: dict[str, Any], verdict: dict[str, Any] | None, label: str) -> str:
    """Why a non-open classification still deserves a human look, or "".

    A ``stale`` ask that @mentions the owner and that the owner never answered is dropped
    by age alone; the surfaces keep the label but say so, because the asker may
    still be waiting.
    """
    if label != "stale" or is_answered(verdict or {}):
        return ""
    if source_directed_at_owner(item, verdict, item_ref(item)):
        return "@mentions the owner with no reply from the owner; needs a human look"
    return ""


def classify_item(
    item: dict[str, Any],
    verdict: dict[str, Any] | None,
    *,
    now: float | None = None,
) -> tuple[str, str]:
    """(classification, reason) for a ledger item or comms candidate.

    Pure: reads the item, the verdict (with its ``thread`` excerpt and optional
    ``channel_after`` posts) and the clock. Never calls a provider.
    """
    verdict = verdict or {}
    ref = item_ref(item)
    kind = str(verdict.get("verdict") or "")
    evidence = verdict.get("evidence") or {}
    now_dt = datetime.fromtimestamp(_now_epoch(now), timezone.utc)

    # 1. answered: the trailing-reply check already proved it.
    if is_answered(verdict, for_draft=True):
        return "answered", answered_label(verdict) or str(evidence.get("why") or "answered")

    # 2. a worker-parked DM has no source message to judge; it is the owner's own ask to send.
    if ref.startswith("slack://dm/") or str(evidence.get("reason") or "") == "parked-dm":
        return "open", "parked DM waits for send N or a drop"

    # A bare item (the --ref path) has no text of its own: the source message
    # is read from the thread excerpt the gate already fetched.
    text, mention_text, verbatim, thread_source = effective_source_text(item, verdict, ref)
    thread = verdict.get("thread") if isinstance(verdict.get("thread"), list) else []
    source_at = source_message_at(item, ref)
    later = _thread_after_source(verdict, source_at)
    directed_at_owner = bool(item.get("directed_at_me")) or mentions_owner(mention_text)
    channel = ""
    gate = slack_gate()
    if gate is not None and "slack.com/archives/" in ref:
        parsed = gate.parse_permalink(ref)
        channel = str(parsed[0]) if isinstance(parsed, (list, tuple)) and parsed else ""
    is_dm = channel.startswith("D")
    # Third-person ask words count too: a stored context may be the collector's
    # paraphrase ("Juniper asked for…"), and keeping a real ask open costs less
    # than dismissing one.
    asks = asks_something(text) or bool(_SUMMARY_ASK_RE.search(text))
    # @channel / @here address the owner too, so they never count as "someone else".
    other_mentions = {
        m for m in mentions_in(mention_text)
        if m != OWNER_SLACK_USER_ID and m not in OWNER_SLACK_USERNAMES
        and m not in {"owner", "owner example", "channel", "here", "everyone"}
    }
    if {"channel", "here", "everyone"} & mentions_in(mention_text):
        directed_at_owner = True

    # 3. no_ask: nothing is asked of the owner or their team.
    if text and _ACK_ONLY_RE.match(text):
        return "no_ask", "acknowledgement only, no question or request"
    if (_is_bot_author(item, thread) or bool(thread_source and thread_source.get("is_bot"))) and not asks:
        return "no_ask", f"automated post by {source_author(item) or 'a bot'} with no question"
    # A statement that @mentions the owner, or arrives in a DM, can still be an ask
    # ("we could do it on nov 19 @{{OWNER_USER}}"): it falls through to settled,
    # stale, and open instead of being dismissed as an FYI.
    if text and verbatim and not asks and not directed_at_owner and not is_dm:
        return "no_ask", "FYI or status update: no question and no request directed at the owner"
    if asks and other_mentions and not directed_at_owner and not is_dm:
        who = ", ".join(sorted(other_mentions))[:60]
        return "no_ask", f"question is directed at {who}, not at the owner"

    # 4. settled: the thread (or the channel) closed the matter.
    if kind == "teammate_replied" and not directed_at_owner:
        closer = str(evidence.get("closer") or evidence.get("closer_user") or "a teammate")
        return "settled", f"{closer} replied in the thread and the ask was not directed at the owner"
    later_owner_nudge = any(mentions_owner(str(m.get("text") or "")) for m in later if not m.get("is_owner"))
    for msg in later:
        if msg.get("is_owner"):
            continue
        if _SETTLE_RE.search(str(msg.get("text") or "")) and not later_owner_nudge:
            return "settled", f"{msg.get('user') or 'a teammate'} closed the thread later: {str(msg.get('text') or '')[:80]!r}"
    channel_after = verdict.get("channel_after")
    if isinstance(channel_after, list):
        for post in channel_after:
            if isinstance(post, dict) and post.get("is_owner"):
                return "settled", f"the owner posted in the same channel after the mention ({post.get('ts') or 'later'})"

    # 5. stale: an old ask nobody nudged again. A nudge outside the thread
    # (``nudged_at``: a Jira transition, a DM, a calendar hold) restarts the
    # clock from the nudge.
    nudge = nudged_at(item)
    age_from = source_at
    if nudge is not None and (age_from is None or nudge > age_from):
        age_from = nudge
    if age_from is not None and business_days_between(age_from, now_dt) > STALE_BUSINESS_DAYS and not later_owner_nudge:
        days = business_days_between(age_from, now_dt)
        since = "since the last nudge" if age_from is nudge else "old"
        # An unanswered ask that @mentions the owner stays stale;
        # needs_human_look() owns the flag and its wording.
        return "stale", f"{days} business days {since} with no later nudge or mention"

    # 6. open.
    if age_from is nudge and nudge is not None:
        source = str(item.get("nudge_source") or "").strip()
        when = nudge.astimezone(PT).strftime("%Y-%m-%d %H:%M PT")
        return "open", f"asker nudged outside the thread on {when}{f' ({source})' if source else ''} with no reply from the owner"
    if directed_at_owner:
        return "open", "question or request directed at the owner with no reply"
    return "open", "unanswered question or request in a thread the owner is part of"


def ai_classify(
    item: dict[str, Any],
    verdict: dict[str, Any],
    *,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> tuple[str, str] | None:
    """Optional AI pass over the full thread (headless amp). Returns None when
    disabled, over budget, or the output could not be parsed. May only return
    no_ask or settled; anything else keeps the heuristic verdict."""
    if not AI_CLASSIFY_ENABLED:
        return None
    if _ai_calls_this_run["n"] >= AI_CLASSIFY_MAX_PER_RUN:
        return None
    text, _mention_text, _verbatim, _msg = effective_source_text(item, verdict, item_ref(item))
    thread = verdict.get("thread") if isinstance(verdict.get("thread"), list) else []
    if not text and not thread:
        return None
    _ai_calls_this_run["n"] += 1
    lines = [f"- {m.get('user') or '?'}{' (the owner)' if m.get('is_owner') else ''}: {str(m.get('text') or '')[:400]}" for m in thread[:30]]
    prompt = (
        "You classify one Slack/email/comment message for the owner (Slack id "
        f"{OWNER_SLACK_USER_ID}, username {{OWNER_USER}}). Decide if the message still needs a reply from the owner.\n"
        "Answer with one JSON object only: {\"classification\": \"open\"|\"no_ask\"|\"settled\", \"reason\": \"<one sentence>\"}.\n"
        "no_ask: the message is an FYI, acknowledgement, status update, or emoji-level reply with no question or request for the owner or their team.\n"
        "settled: the thread already resolved the matter, or the message confirms a plan that was already agreed.\n"
        "open: a real question or request to the owner (or their team) that nobody answered.\n\n"
        f"Source message from {source_author(item) or 'unknown'}:\n{text[:1500]}\n\n"
        "Thread (oldest first):\n" + ("\n".join(lines) if lines else "(no other messages)")
    )
    cmd = ["amp", "--settings-file", str(AI_SETTINGS_FILE), "-x", prompt]
    run = runner or _runner or _default_runner
    rc, out, err = run(cmd, AI_CLASSIFY_TIMEOUT_SECONDS)
    if rc != 0:
        log(f"ai classify failed rc={rc}: {(err or out)[:160]}")
        return None
    match = re.search(r"\{.*?\}", out, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    label = str(payload.get("classification") or "").strip()
    if label not in {"no_ask", "settled"}:
        return None
    return label, f"AI: {str(payload.get('reason') or '')[:200]}"


def classify(item: dict[str, Any], verdict: dict[str, Any], *, now: float | None = None, ai: bool = False) -> dict[str, Any]:
    """Attach ``classification`` and ``classification_reason`` to ``verdict`` (returned copy)."""
    label, reason = classify_item(item, verdict, now=now)
    if label == "open" and ai:
        ai_result = ai_classify(item, verdict)
        if ai_result is not None:
            label, reason = ai_result
    result = dict(verdict)
    result["classification"] = label
    result["classification_reason"] = reason
    # A non-open label that still deserves eyes (a stale @the owner ask the owner never
    # answered) is flagged, so the digest and the CoS Review feed can say so.
    note = needs_human_look(item, result, label)
    result["needs_human_look"] = bool(note)
    if note:
        result["needs_human_look_reason"] = note
    return result


def needs_reply(verdict: dict[str, Any] | None) -> bool:
    """True only for an ``open`` classification (or an unclassified open verdict)."""
    if not verdict:
        return False
    label = str(verdict.get("classification") or "")
    if label:
        return label == "open"
    return str(verdict.get("verdict") or "") in {"open", "teammate_replied", "unknown", "unchecked"} and not is_answered(verdict)


def classification_label(verdict: dict[str, Any] | None) -> str:
    """Digest line: 'already answered …', 'no ask: …', 'settled: …', 'stale: …'."""
    if not verdict:
        return ""
    label = str(verdict.get("classification") or "")
    reason = str(verdict.get("classification_reason") or "")
    if label == "answered":
        return answered_label(verdict) or reason
    if label in NON_OPEN_CLASSIFICATIONS:
        return f"{label.replace('_', ' ')}: {reason}" if reason else label.replace("_", " ")
    return ""


# --- cache ------------------------------------------------------------------


def load_cache() -> dict[str, Any]:
    return _read_json(CACHE_PATH)


def cached_verdict(ref: str, *, now: float | None = None, allow_stale: bool = False) -> dict[str, Any] | None:
    """The cached verdict for ``ref`` if it is still inside the TTL (or any, when allow_stale)."""
    entry = load_cache().get(canonical_key(ref))
    if not isinstance(entry, dict):
        return None
    age = _now_epoch(now) - float(entry.get("checked_at_epoch") or 0)
    if allow_stale or age < TTL_SECONDS:
        result = dict(entry)
        result["source"] = "cache"
        return result
    return None


def cached_verdicts(*, now: float | None = None) -> dict[str, dict[str, Any]]:
    """Every in-TTL cached verdict keyed by canonical ref (for read-only surfaces)."""
    epoch = _now_epoch(now)
    result = {}
    for key, entry in load_cache().items():
        if isinstance(entry, dict) and epoch - float(entry.get("checked_at_epoch") or 0) < TTL_SECONDS:
            result[key] = entry
    return result


def _in_failure_backoff(entry: dict[str, Any] | None, now: float) -> bool:
    if not entry or str(entry.get("verdict")) != "unknown":
        return False
    strikes = int(entry.get("fail_count") or 0)
    if strikes <= 0:
        return False
    wait = min(FAIL_BACKOFF_MAX_SECONDS, FAIL_BACKOFF_BASE_SECONDS * (2 ** (strikes - 1)))
    return now - float(entry.get("checked_at_epoch") or 0) < wait


def _remember(key: str, result: dict[str, Any], previous: dict[str, Any] | None) -> None:
    cache = load_cache()
    entry = dict(result)
    entry["key"] = key
    if str(result.get("verdict")) == "unknown":
        entry["fail_count"] = int((previous or {}).get("fail_count") or 0) + 1
        # Keep the last definite verdict so read-only surfaces degrade, not flip.
        if previous and str(previous.get("verdict")) not in ("unknown", ""):
            entry["last_known"] = {k: previous[k] for k in ("verdict", "evidence", "checked_at") if k in previous}
        elif previous and previous.get("last_known"):
            entry["last_known"] = previous["last_known"]
    cache[key] = entry
    if len(cache) > 2000:
        for stale_key in sorted(cache, key=lambda k: float(cache[k].get("checked_at_epoch") or 0))[: len(cache) - 2000]:
            cache.pop(stale_key, None)
    _write_json(CACHE_PATH, cache)


# --- public API ---------------------------------------------------------------


def check_item(
    item: dict[str, Any],
    *,
    use_cache: bool = True,
    force: bool = False,
    now: float | None = None,
    ai: bool = True,
) -> dict[str, Any]:
    """Live-or-cached verdict for a ledger item or comms candidate.

    Returns a dict with ``verdict`` in {resolved_by_owner, resolved_by_other,
    resolved_by_reaction, teammate_replied, open, unknown}, ``kind``,
    ``source`` (live|cache|error|offline), ``evidence`` and ``checked_at``.
    Never raises on provider failure: that is an ``unknown`` verdict.
    """
    ref = item_ref(item)
    kind = source_kind(item)
    epoch = _now_epoch(now)
    if not ref or kind is None:
        return _verdict(ref, kind or "", "unknown", source="error", evidence={"note": "unsupported source"}, now=epoch)
    key = canonical_key(ref)
    own_by = own_message_author(item)
    if own_by:
        # the owner's own message is never an ask: decide from the item itself,
        # without a provider call, and without caching (the item's author
        # fields are the proof and the cache key is shared with other items).
        when = source_seen_at(item)
        return classify(item, _verdict(
            ref, kind, "resolved_by_owner", source="item",
            evidence={
                "closer": "the owner",
                "closer_at": _iso(when.timestamp()) if when else None,
                "closer_permalink": ref,
                "reason": "own-message",
                "why": f"the source message is the owner's own ({own_by}); the owner's messages are never asks",
            },
            now=epoch,
        ), now=epoch)
    if ref.startswith("slack://dm/"):
        # A worker-parked DM (parked_draft_tracker.py) has no thread to read:
        # it settles only through `send N`, a drop, or its mirrored todo.
        return classify(item, _verdict(
            ref, "slack", "open", source="item",
            evidence={
                "reason": "parked-dm",
                "why": "parked DM has no thread to read; settles via send N, a drop, or the mirrored todo",
            },
            now=epoch,
        ), now=epoch)
    previous = load_cache().get(key) if use_cache or not force else None
    if not isinstance(previous, dict):
        previous = None
    if use_cache and not force and previous is not None:
        age = epoch - float(previous.get("checked_at_epoch") or 0)
        if str(previous.get("verdict")) != "unknown" and age < TTL_SECONDS:
            result = dict(previous)
            result["source"] = "cache"
            return classify(item, result, now=epoch)
        if _in_failure_backoff(previous, epoch):
            result = dict(previous)
            result["source"] = "cache"
            return classify(item, result, now=epoch)
    if OFFLINE and kind != "slack":
        return classify(item, _verdict(ref, kind, "unknown", source="offline", evidence={"note": "offline"}, now=epoch), now=epoch)
    try:
        if kind == "slack":
            result = _check_slack(ref, use_cache=use_cache, force=force, now=epoch)
            _attach_channel_after(item, ref, result)
        elif kind == "gmail":
            result = _check_gmail(item, ref, now=epoch)
        elif kind == "github":
            result = _check_github(item, ref, now=epoch)
        else:
            result = _check_gdoc(item, ref, now=epoch)
    except ProviderError as exc:
        result = _verdict(ref, kind, "unknown", source="error", evidence={"note": str(exc)[:200]}, now=epoch)
        log(f"{kind} check failed for {ref}: {exc}")
    except Exception as exc:  # a provider parse bug must never break a digest
        result = _verdict(ref, kind, "unknown", source="error", evidence={"note": f"{type(exc).__name__}: {exc}"[:200]}, now=epoch)
        log(f"{kind} check crashed for {ref}: {type(exc).__name__}: {exc}")
    _remember(key, result, previous)
    # The AI pass runs on live reads only (sweep, reconcile, --ref, --draft);
    # digest reads answer from the cache and the heuristics.
    return classify(item, result, now=epoch, ai=AI_CLASSIFY_ENABLED and ai)


def _attach_channel_after(item: dict[str, Any], ref: str, result: dict[str, Any]) -> None:
    """For a top-level @mention of the owner that is still open after the thread
    read, fetch the channel's later top-level posts once: the owner answering in a
    new top-level post (not in the thread) settles the ask."""
    gate = slack_gate()
    if gate is None or not hasattr(gate, "channel_posts_after"):
        return
    if str(result.get("verdict") or "") not in {"open", "teammate_replied"}:
        return
    parsed = gate.parse_permalink(ref)
    if not isinstance(parsed, (list, tuple)) or len(parsed) < 3 or parsed[2]:
        return  # a thread reply: the thread read already covered it
    channel, ts = str(parsed[0]), str(parsed[1])
    if channel.startswith("D"):
        return
    # A bare --ref item reads its text from the thread excerpt the gate attached.
    source_text, mention_text, _verbatim, _msg = effective_source_text(item, result, ref)
    if not (mentions_owner(mention_text) and asks_something(source_text)):
        return
    try:
        posts = gate.channel_posts_after(channel, ts)
    except Exception as exc:  # a failed channel read never changes the thread verdict
        log(f"channel_posts_after failed for {ref}: {type(exc).__name__}: {exc}")
        return
    if not isinstance(posts, list):
        return
    result["channel_after"] = posts


def check_ref(
    ref: str,
    *,
    use_cache: bool = True,
    force: bool = False,
    now: float | None = None,
    nudged_at: str | None = None,
    nudge_source: str | None = None,
) -> dict[str, Any]:
    """Check one permalink. ``nudged_at`` (ISO) records a nudge the asker made
    outside the thread (a Jira transition, a DM) so an old ask is not stale."""
    item: dict[str, Any] = {"ref": ref}
    if nudged_at:
        item["nudged_at"] = nudged_at
        if nudge_source:
            item["nudge_source"] = nudge_source
    return check_item(item, use_cache=use_cache, force=force, now=now)


def bounded_checker(budget_seconds: float | None, *, live: bool = True) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A check_item wrapper that stops going live once ``budget_seconds`` elapse.

    After the deadline (or when ``live`` is False) it answers from the cache
    only, so a digest with a short time budget still benefits from the sweep's
    warm cache and never blocks on a provider.
    """
    deadline = time.monotonic() + budget_seconds if budget_seconds and budget_seconds > 0 else None

    def checker(item: dict[str, Any]) -> dict[str, Any]:
        if live and (deadline is None or time.monotonic() < deadline):
            return check_item(item)
        cached = cached_verdict(item_ref(item))
        verdict = cached or _verdict(item_ref(item), source_kind(item) or "", "unknown", source="cache", evidence={"note": "no cached verdict"})
        return classify(item, verdict)

    return checker


# --- sweep --------------------------------------------------------------------


def _sweep_due(every_minutes: float, now: float) -> bool:
    stamp = _read_json(SWEEP_STAMP)
    try:
        last = float(stamp.get("last_run_epoch") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return now - last >= every_minutes * 60


def sweep(*, max_checks: int = 6, budget_seconds: float = 50.0, every_minutes: float = 5.0, ledger_path: Path | None = None) -> dict[str, Any]:
    """Warm the cache for held drafts and mark answered ones superseded.

    Runs from run-comms-fastpath.sh every tick but self-throttles to one pass
    per ``every_minutes``; each pass issues at most ``max_checks`` live reads
    within ``budget_seconds``. Cached drafts cost nothing, so coverage rotates
    across passes and the digests read a warm cache.
    """
    now = time.time()
    if not _sweep_due(every_minutes, now):
        return {"ran": False, "reason": "not due"}
    _write_json(SWEEP_STAMP, {"last_run_epoch": now, "last_run": _iso(now)})
    live_calls = {"n": 0}
    deadline = time.monotonic() + budget_seconds

    def checker(item: dict[str, Any]) -> dict[str, Any]:
        cached = cached_verdict(item_ref(item), now=time.time())
        if cached is not None:
            return classify(item, cached)
        if live_calls["n"] >= max_checks or time.monotonic() >= deadline:
            skipped = _verdict(item_ref(item), source_kind(item) or "", "unknown", source="skipped", evidence={"note": "sweep budget"}, now=time.time())
            return classify(item, skipped)
        live_calls["n"] += 1
        return check_item(item)

    kwargs: dict[str, Any] = {"checker": checker, "time_budget_seconds": budget_seconds + 5}
    if ledger_path is not None:
        kwargs["path"] = ledger_path
    superseded = ledger().refresh_pending_drafts(**kwargs)
    summary = {"ran": True, "live_checks": live_calls["n"], "superseded": superseded, "at": _iso(now)}
    log(f"sweep: live={live_calls['n']} superseded={[s.get('draft_id') for s in superseded]}")
    return summary


# --- reconcile ------------------------------------------------------------------


def _mirror_todo_ids(item: dict[str, Any]) -> list[str]:
    """The to-do ids a held draft mirrors (comms-triage `meta.todo_id`, a parked
    draft's `draft_meta.tracked_todo`, or an older `draft_meta.todo_id`)."""
    ids: list[str] = []
    for holder, key in ((item.get("meta") or {}, "todo_id"), (item.get("draft_meta") or {}, "tracked_todo"), (item.get("draft_meta") or {}, "todo_id")):
        value = str((holder or {}).get(key) or "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def _todos_by_source_ref(path: Path | None = None) -> dict[str, dict[str, Any]]:
    items = _read_json(path or TODO_PATH).get("items") or []
    out: dict[str, dict[str, Any]] = {}
    for todo in items:
        if isinstance(todo, dict) and todo.get("source_ref"):
            out[str(todo.get("source_ref"))] = todo
    return out


def reconcile(
    *,
    budget_seconds: float = 20.0,
    max_checks: int = 4,
    ledger_path: Path | None = None,
    todo_path: Path | None = None,
    todo_cli: Path | None = None,
    dry_run: bool = False,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """Dismiss held drafts that no longer need a reply and close their mirror to-dos.

    Runs from each command-center tick beside ``parked_draft_tracker.py reconcile``.
    Every pending draft is classified (cache first, then at most ``max_checks``
    live reads within ``budget_seconds``); a draft classified answered, no_ask,
    settled, or stale is superseded in the ledger with the classification as
    the reason, and every to-do it mirrors is completed with that reason. The
    closed draft and to-do ids are logged so the tick is auditable.
    """
    live_calls = {"n": 0}
    deadline = time.monotonic() + budget_seconds

    def checker(item: dict[str, Any]) -> dict[str, Any]:
        cached = cached_verdict(item_ref(item), now=time.time())
        if cached is not None:
            return classify(item, cached, ai=False)
        if live_calls["n"] >= max_checks or time.monotonic() >= deadline:
            skipped = _verdict(item_ref(item), source_kind(item) or "", "unknown", source="skipped", evidence={"note": "reconcile budget"}, now=time.time())
            return classify(item, skipped, ai=False)
        live_calls["n"] += 1
        return check_item(item)

    pending = list(ledger().pending_drafts(**({"path": ledger_path} if ledger_path is not None else {})))
    todos = _todos_by_source_ref(todo_path)
    planned: list[dict[str, Any]] = []
    for item in pending:
        try:
            verdict = checker(item)
        except Exception as exc:  # a provider error never stops the tick
            log(f"reconcile: {item.get('draft_id')} check failed: {exc}")
            continue
        label = str(verdict.get("classification") or "")
        if label not in NON_OPEN_CLASSIFICATIONS:
            continue
        todo_ids = _mirror_todo_ids(item)
        by_ref = todos.get(str(item_ref(item)))
        if by_ref and str(by_ref.get("id") or "") not in todo_ids:
            todo_ids.append(str(by_ref.get("id")))
        planned.append({
            "draft_id": str(item.get("draft_id") or ""),
            "ref": item_ref(item),
            "classification": label,
            "reason": str(verdict.get("classification_reason") or ""),
            "verdict": verdict,
            "todo_ids": todo_ids,
        })

    # Drafts another path already superseded (show, a digest render, a
    # start-day seed all run refresh_pending_drafts) leave their mirror to-dos
    # open; close those too so the to-do list and the ledger agree.
    open_todo_statuses = OPEN_TODO_STATUSES | {"waiting", "scheduled", "snoozed"}
    orphans: list[dict[str, Any]] = []
    try:
        state_items = ledger().load_state(ledger_path).get("items", {}) if ledger_path is not None else ledger().load_state().get("items", {})
    except Exception as exc:  # pragma: no cover - a corrupt ledger is logged, never fatal here
        log(f"reconcile: ledger read failed: {exc}")
        state_items = {}
    for item in state_items.values():
        if not isinstance(item, dict) or item.get("draft_status") != "dismissed" or not ledger().is_superseded(item):
            continue
        todo_ids = _mirror_todo_ids(item)
        by_ref = todos.get(str(item_ref(item)))
        if by_ref and str(by_ref.get("id") or "") not in todo_ids:
            todo_ids.append(str(by_ref.get("id")))
        still_open = [
            tid for tid in todo_ids
            if any(str(t.get("id") or "") == tid and str(t.get("status") or "") in open_todo_statuses for t in todos.values())
        ]
        if not still_open:
            continue
        superseded = item.get("superseded") if isinstance(item.get("superseded"), dict) else {}
        label = str(superseded.get("classification") or (item.get("draft_meta") or {}).get("classification") or "answered")
        orphans.append({
            "draft_id": str(item.get("draft_id") or ""),
            "ref": item_ref(item),
            "classification": label,
            "reason": str(superseded.get("classification_reason") or superseded.get("reason") or (item.get("draft_meta") or {}).get("reason") or ""),
            "todo_ids": still_open,
        })

    closed_drafts: list[str] = []
    closed_todos: list[str] = []
    if dry_run:
        return {
            "dry_run": True,
            "live_checks": live_calls["n"],
            "planned": [{k: v for k, v in p.items() if k != "verdict"} for p in planned],
            "orphan_todos": orphans,
        }

    kwargs: dict[str, Any] = {"by": "already-answered-reconcile"}
    if ledger_path is not None:
        kwargs["path"] = ledger_path
    todo_cli = todo_cli or Path(os.environ.get("PM_OS_TODO_CLI") or REPO_ROOT / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py")
    run = runner or _runner or _default_runner

    def complete_todos(plan: dict[str, Any]) -> None:
        for todo_id in plan["todo_ids"]:
            todo = next((t for t in todos.values() if str(t.get("id") or "") == todo_id), None)
            if todo is not None and str(todo.get("status") or "") not in open_todo_statuses:
                continue
            reason = f"{plan['classification']}: {plan['reason']}"[:200]
            cmd = [sys.executable, str(todo_cli)]
            if todo_path is not None:
                cmd += ["--path", str(todo_path)]
            cmd += ["complete", "--id", todo_id, "--reason", reason]
            rc, out, err = run(cmd, 30.0)
            if rc == 0:
                closed_todos.append(todo_id)
            else:
                log(f"reconcile: todo {todo_id} complete failed rc={rc}: {(err or out)[:160]}")

    for plan in planned:
        verdicts = {plan["draft_id"]: plan["verdict"]}
        superseded = ledger().refresh_pending_drafts(draft_id=plan["draft_id"], checker=lambda item, v=verdicts: v.get(str(item.get("draft_id") or ""), {}), time_budget_seconds=None, **kwargs)
        if not superseded:
            continue
        closed_drafts.append(plan["draft_id"])
        complete_todos(plan)
    for plan in orphans:
        complete_todos(plan)
    summary = {
        "live_checks": live_calls["n"],
        "closed_drafts": closed_drafts,
        "closed_todos": closed_todos,
        "classified": [{"draft_id": p["draft_id"], "classification": p["classification"], "reason": p["reason"]} for p in planned],
        "orphan_todos": [{"draft_id": p["draft_id"], "classification": p["classification"], "todo_ids": p["todo_ids"]} for p in orphans],
        "at": _iso(time.time()),
    }
    if closed_drafts or closed_todos:
        log(f"reconcile: closed drafts={closed_drafts} todos={closed_todos} " + " ".join(f"{p['draft_id']}={p['classification']}" for p in planned + orphans))
    return summary


# --- CLI ----------------------------------------------------------------------


def open_github_todos(path: Path | None = None) -> list[dict[str, Any]]:
    """Open to-dos whose source_ref is a GitHub PR/issue (the asks a merge retires)."""
    items = _read_json(path or TODO_PATH).get("items") or []
    rows = []
    for todo in items:
        if not isinstance(todo, dict) or str(todo.get("status") or "") not in OPEN_TODO_STATUSES:
            continue
        if source_kind({"ref": str(todo.get("source_ref") or "")}) != "github":
            continue
        rows.append({
            "todo_id": str(todo.get("id") or ""),
            "title": str(todo.get("title") or ""),
            "priority": str(todo.get("priority") or ""),
            "status": str(todo.get("status") or ""),
            "ref": str(todo.get("source_ref") or ""),
        })
    return rows


_REVIEW_FRAMED_RE = re.compile(r"^\s*(?:review|please review|re-?review)\b.*\b(?:pr|prs|pull request|github|#\d+)\b", re.I)


def review_framed(title: str) -> bool:
    """True when a to-do or ask label asks the owner to *review* a PR (auto-capture's
    "Review GitHub PR: …" and hand-written "Review acme/x#12 …"). "Get review
    and merge example-repo#10002" is the owner's own work, not review-framed."""
    return bool(_REVIEW_FRAMED_RE.search(str(title or "")))


def review_ask_status(label: str, verdict: dict[str, Any]) -> str:
    """Why a review-framed GitHub item is or is not an ask, from its verdict evidence.

    ``ask``: the owner is a requested reviewer on the live PR. ``not-asked``: the PR
    is OPEN and the owner is not (author, team fan-out, nobody); the item renders as a
    status line, never a review ask. ``unverified``: no live role (offline,
    rate-limited, issue, not a PR); fail closed, never an ask. ``""`` when the
    label is not review-framed.
    """
    if not review_framed(label):
        return ""
    evidence = verdict.get("evidence") or {}
    role = str(evidence.get("owner_role") or "")
    if not role:
        return "unverified"
    return "ask" if role == REVIEW_ROLE_ASK else "not-asked"


def _pending_rows(live: bool) -> list[dict[str, Any]]:
    """Held drafts (every kind) plus open GitHub-backed to-dos, each with its verdict."""
    rows = []
    items = list(ledger().pending_drafts()) + open_github_todos()
    for item in items:
        if live:
            verdict = check_item(item)
        else:
            verdict = cached_verdict(item_ref(item), allow_stale=True) or {"verdict": "unchecked", "evidence": {}}
            if item.get("draft_id") and not verdict.get("classification"):
                verdict = classify(item, verdict)
        evidence = verdict.get("evidence") or {}
        title = item.get("title") if item.get("todo_id") else None
        rows.append(
            {
                "draft_id": item.get("draft_id"),
                "draft_number": item.get("draft_number"),
                "todo_id": item.get("todo_id"),
                "title": title,
                "kind": source_kind(item),
                "ref": item_ref(item),
                "verdict": verdict.get("verdict"),
                "answered": is_answered(verdict),
                "label": answered_label(verdict),
                "classification": verdict.get("classification", ""),
                "classification_reason": verdict.get("classification_reason", ""),
                "needs_human_look": bool(verdict.get("needs_human_look")),
                "needs_reply": needs_reply(verdict),
                "reason": evidence.get("reason", ""),
                "why": evidence.get("why", ""),
                "owner_role": evidence.get("owner_role", ""),
                "review_ask": review_ask_status(title or "", verdict) if title else "",
                "checked_at": verdict.get("checked_at", ""),
                "evidence": evidence,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        nargs="?",
        choices=["sweep", "reconcile"],
        help="sweep: warm the cache + supersede answered drafts; reconcile: classify every held draft and auto-dismiss no_ask/settled/stale ones with their mirror todos",
    )
    parser.add_argument("--dry-run", action="store_true", help="reconcile: print the plan, change nothing")
    parser.add_argument("--ref", help="check one source permalink")
    parser.add_argument("--nudged-at", help="with --ref: ISO time the asker nudged outside the thread (Jira transition, DM); restarts the stale clock for this verdict only (not stored; --pending and reconcile do not see it)")
    parser.add_argument("--nudge-source", help="with --ref --nudged-at: what the nudge was, for the reason line")
    parser.add_argument("--draft", help="check one held draft by id or number")
    parser.add_argument("--pending", action="store_true", help="list every held draft with its verdict")
    parser.add_argument("--live", action="store_true", help="with --pending: go live instead of reading the cache")
    parser.add_argument("--force", action="store_true", help="ignore the cache for --ref/--draft")
    parser.add_argument("--json", action="store_true", help="JSON output (default for --ref/--draft)")
    parser.add_argument("--max", type=int, default=6, help="sweep: live checks per pass")
    parser.add_argument("--budget", type=float, default=50.0, help="sweep: seconds per pass")
    parser.add_argument("--every", type=float, default=5.0, help="sweep: minutes between passes")
    args = parser.parse_args(argv)

    if args.command == "sweep":
        print(json.dumps(sweep(max_checks=args.max, budget_seconds=args.budget, every_minutes=args.every), indent=2))
        return 0
    if args.command == "reconcile":
        print(json.dumps(reconcile(budget_seconds=args.budget, max_checks=args.max, dry_run=args.dry_run), indent=2))
        return 0
    if args.ref:
        print(json.dumps(check_ref(args.ref, force=args.force, nudged_at=args.nudged_at, nudge_source=args.nudge_source), indent=2))
        return 0
    if args.draft:
        draft_id = args.draft if args.draft.startswith("draft-") else f"draft-{args.draft}"
        item = ledger().get_draft(draft_id)
        print(json.dumps(check_item(item, force=args.force), indent=2))
        return 0
    if args.pending:
        rows = _pending_rows(args.live)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                cls = row.get("classification") or ""
                if row["answered"]:
                    mark = "✅"
                elif cls in NON_OPEN_CLASSIFICATIONS:
                    mark = "🚫"
                elif row["verdict"] in ("unknown", "unchecked") and cls != "open":
                    mark = "❔"
                else:
                    mark = "⏳"
                handle = row["draft_id"] or f"todo {str(row['todo_id'])[:8]}"
                label = row["label"] or (f"{cls.replace('_', ' ')}: {row['classification_reason']}" if cls in NON_OPEN_CLASSIFICATIONS else "")
                print(f"{mark} {handle:<13} {row['kind'] or '?':<6} {row['verdict']:<20} {label}  {row['ref']}")
                if row.get("title"):
                    print(f"     todo: {row['title']}")
                if row["why"]:
                    print(f"     why: {row['why']}")
                if row.get("review_ask") == "not-asked":
                    print(f"     🚫 not a review ask: the owner's role on the live PR is {row['owner_role']} (status line only; relabel or drop the to-do)")
                elif row.get("review_ask") == "unverified":
                    print("     ❔ review ask unverified: no live reviewer read; never surface as an ask until it is")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
