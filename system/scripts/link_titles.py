#!/usr/bin/env python3
"""Read the title of every artifact an ask links to.

The subject of an ask like "Can you review this: <Google Doc>" lives in the
linked doc, not in the prompt words. Without the title, "Pre-review of a
Google Doc ... <docs.google.com link>" would scaffold a new workstream named
after the prompt words even when the doc title names an existing owner.
dispatch.py feeds these titles into owner matching
(router-context-pack.py --source-title) before new-workstream.py derives a
new name from them.

Supported links: Google Docs, Sheets, Slides, and Drive files
(`switchboard drive info`), Slack permalinks (the
thread's parent message, gated by slack_backoff), Linear issues, Jira issues,
and GitHub PRs and issues (`gh ... view --json title`).

Every lookup has a short timeout and runs in parallel, so a dead link costs
seconds, never a stalled dispatch. SWITCHBOARD_LINK_TITLES_OFFLINE=1 (or
SWITCHBOARD_NEW_WORKSTREAM_OFFLINE=1) turns every lookup off.

CLI: python3 system/scripts/link_titles.py "<ask text>"   -> JSON list
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


OFFLINE_ENVS = ("SWITCHBOARD_LINK_TITLES_OFFLINE", "SWITCHBOARD_NEW_WORKSTREAM_OFFLINE")
LOOKUP_TIMEOUT_S = 6
MAX_LINKS = 3
SLACK_BUCKET_MAX_WAIT_S = 5.0

URL_RE = re.compile(r"""(?:<)?(?:https?://|www\.)[^\s<>|'"\)\]]+(?:\|[^>]*>|>)?""")
DRIVE_URL_RE = re.compile(r"https?://(?:docs|drive)\.google\.com/[^\s<>|'\")\]]+")
SLACK_URL_RE = re.compile(r"https?://[^/\s]+/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
LINEAR_URL_RE = re.compile(r"https?://linear\.app/[^/\s]+/issue/([A-Z][A-Z0-9]+-\d+)")
JIRA_URL_RE = re.compile(r"https?://[a-z0-9-]+\.atlassian\.net/browse/(([A-Z][A-Z0-9]+)-\d+)")
GITHUB_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/(pull|issues)/\d+")
SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")

# File extensions a Drive name carries ("Northwind_Partnership_Working Draft.docx").
EXTENSION_RE = re.compile(r"\.(?:docx?|xlsx?|xlsm|pptx?|pdf|csv|tsv|txt|md|rtf|key|pages|numbers|odt|ods|odp)\s*$",
                          re.IGNORECASE)
# Words and phrases that describe the copy or its state, never the topic.
NOISE_RES = (
    re.compile(r"^\s*(?:\[?(?:external|internal|confidential|draft|wip)\]?\s*[:\-]?\s*)+", re.IGNORECASE),
    re.compile(r"^\s*(?:copy\s+of\s+)+", re.IGNORECASE),
    re.compile(r"\b(?:working|rough|first|final|latest|internal)\s+(?:draft|copy|version)\b", re.IGNORECASE),
    re.compile(r"\b(?:draft|wip|final|redline|redlined|untitled)\b", re.IGNORECASE),
    re.compile(r"\bv\d+(?:\.\d+)*\b", re.IGNORECASE),
    re.compile(r"\b(?:rev|version)\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b(?:document|spreadsheet|presentation)\b\s*$", re.IGNORECASE),
)
# Capitalized words that open a sentence or address someone, never a topic.
CAP_RUN_SKIP = {"Morning", "Hi", "Hey", "Hello", "Thanks", "Please", "Here",
                "The", "This", "That", "These", "Those", "Our", "Your", "We",
                "I", "A", "An", "Following", "Due", "Quick", "Title", "Who",
                "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday", "January", "February", "March", "April",
                "May", "June", "July", "August", "September", "October",
                "November", "December", "Nov", "Oct", "Dec", "Jan", "Feb",
                "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept"}


def offline() -> bool:
    return any(os.environ.get(name) for name in OFFLINE_ENVS)


def link_kind(url: str) -> str:
    if DRIVE_URL_RE.match(url):
        return "drive"
    if SLACK_URL_RE.search(url):
        return "slack"
    if LINEAR_URL_RE.search(url):
        return "linear"
    if JIRA_URL_RE.search(url):
        return "jira"
    if GITHUB_URL_RE.search(url):
        return "github"
    return ""


def urls_in(text: str) -> list[str]:
    """Every URL in the text, Slack `<url|label>` wrappers removed, in order."""
    urls: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.lstrip("<").split("|", 1)[0].rstrip(">").rstrip(".,;:")
        if url not in urls:
            urls.append(url)
    return urls


def resolvable_urls(text: str, limit: int = MAX_LINKS) -> list[str]:
    """The first `limit` URLs this module can read a title for."""
    return [url for url in urls_in(text) if link_kind(url)][:limit]


def strip_title_noise(raw: str) -> str:
    """"Northwind_Partnership_Working Draft.docx" -> "Northwind Partnership": drop the
    extension, underscores, bracketed asides, and copy or state words
    ("Copy of", "Working Draft", "v2", "Final")."""
    text = EXTENSION_RE.sub("", (raw or "").strip())
    text = text.replace("_", " ")
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", text)
    for pattern in NOISE_RES:
        text = pattern.sub(" ", text)
    text = re.sub(r"\s+[-|:–—]\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -|:–—")


def capitalized_run(text: str) -> str:
    """The longest run of capitalized words in free text (a product or event
    name such as "Spring Fair Q&A"). Sentence openers and greetings are skipped."""
    plain = SLACK_LINK_RE.sub(lambda m: m.group(2) or "", text or "")
    plain = re.sub(r"<@[A-Z0-9]+>", " ", plain)
    best: list[str] = []
    run: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'&]*|[^A-Za-z0-9\s]", plain):
        word = token.replace("'", "")
        if word[:1].isupper() and word not in CAP_RUN_SKIP and not word.isdigit():
            run.append(word)
            continue
        if len(run) > len(best):
            best = run
        run = []
    if len(run) > len(best):
        best = run
    return " ".join(best)


def _run_json(cmd: list[str], timeout: float, parse: Any = json.loads) -> dict | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        data = parse(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def drive_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    data = _run_json(["switchboard", "drive", "info", "--file-id-or-url", url], timeout)
    file = (data or {}).get("file") or data or {}
    return str(file.get("name") or file.get("title") or "").strip()


def linear_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    match = LINEAR_URL_RE.search(url)
    if not match:
        return ""
    data = _run_json(["switchboard", "linear", "issue", "--id", match.group(1)], timeout)
    issue = (data or {}).get("issue") or data or {}
    return str(issue.get("title") or "").strip()


def jira_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    match = JIRA_URL_RE.search(url)
    if not match:
        return ""
    key, project = match.groups()
    data = _run_json(["switchboard", "jira", "issues", "--projectKey", project, "--jql", f"key = {key}",
                      "--fields", "summary", "--maxResults", "1", "--response-format", "minimal"], timeout)
    issues = (((data or {}).get("data") or {}).get("issues")) or []
    first = issues[0] if issues and isinstance(issues[0], dict) else {}
    return str((first.get("fields") or {}).get("summary") or "").strip()


def github_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    match = GITHUB_URL_RE.search(url)
    if not match:
        return ""
    kind = "pr" if match.group(1) == "pull" else "issue"
    data = _run_json(["gh", kind, "view", match.group(0), "--json", "title"], timeout)
    return str((data or {}).get("title") or "").strip()


def slack_thread_text(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    """The parent message of a Slack permalink's thread, read through the shared
    per-method token buckets in slack_backoff (never an ungated reader)."""
    match = SLACK_URL_RE.search(url)
    if not match:
        return ""
    channel, secs, micros = match.groups()
    argv = ["thread", "--channel", channel, "--thread-ts", f"{secs}.{micros}"]
    try:
        scripts = Path(__file__).resolve().parent
        sys.path.insert(0, str(scripts))
        import slack_backoff  # noqa: WPS433 (sibling module)
        root = Path(os.environ.get("REPO_ROOT") or scripts.parents[1])
        backoff_file = Path(os.environ.get("SLACK_MENTIONS_BACKOFF_FILE", root / ".state" / "slack-mentions-backoff.json"))
        wait = slack_backoff.acquire(slack_backoff.call_cost(argv), backoff_file=backoff_file)
        if wait > SLACK_BUCKET_MAX_WAIT_S:
            return ""
    except Exception:  # noqa: BLE001 (a broken gate must not break dispatch)
        return ""
    data = _run_json(["switchboard", "slack", *slack_backoff.json_argv(argv)], timeout, slack_backoff.loads)
    messages = (data or {}).get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], dict) else {}
    text = str(first.get("text") or "")
    SLACK_TEXTS[url] = text
    return text


def slack_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> str:
    """A Slack thread names its work through a Google Doc it links to (the doc
    title), else through the longest capitalized run of the parent message."""
    text = slack_thread_text(url, timeout)
    if not text:
        return ""
    for linked, _label in SLACK_LINK_RE.findall(text):
        if DRIVE_URL_RE.match(linked):
            title = strip_title_noise(drive_title(linked, timeout))
            if title:
                return title
    return capitalized_run(text)


# Parent-message text per Slack permalink read in this process, so fetch_title
# can hand dispatch the content as well as the title: system_owners.py resolves
# the owner of the box, site, or job a failure message names.
SLACK_TEXTS: dict[str, str] = {}

FETCHERS: dict[str, str] = {  # looked up by name so tests can patch one fetcher
    "drive": "drive_title", "slack": "slack_title", "linear": "linear_title",
    "jira": "jira_title", "github": "github_title",
}


def fetch_title(url: str, timeout: float = LOOKUP_TIMEOUT_S) -> dict:
    kind = link_kind(url)
    raw = ""
    if kind:
        try:
            raw = globals()[FETCHERS[kind]](url, timeout)
        except Exception:  # noqa: BLE001 (one bad link never breaks the others)
            raw = ""
    return {"url": url, "kind": kind, "raw": raw, "title": strip_title_noise(raw),
            "text": SLACK_TEXTS.get(url, "") if kind == "slack" else ""}


def fetch_titles(text_or_urls: str | list[str], timeout: float = LOOKUP_TIMEOUT_S,
                 limit: int = MAX_LINKS) -> list[dict]:
    """[{url, kind, raw, title}] for each resolvable link, in link order, with
    the lookups run in parallel. Empty when offline or nothing resolves."""
    urls = (resolvable_urls(text_or_urls, limit) if isinstance(text_or_urls, str)
            else [u for u in text_or_urls if link_kind(u)][:limit])
    if not urls or offline():
        return []
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(lambda u: fetch_title(u, timeout), urls))
    return [r for r in results if r["title"]]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(fetch_titles(sys.argv[1]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
