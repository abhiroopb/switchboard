#!/usr/bin/env python3
"""Names, never bare numbers.

Anything that speaks to the owner names a PR, issue, task or workstream by its
name. A bare number such as "example-repo#10001" or "pause 10001" never stands in
for the name.

This module is the one place that turns an identifier into the name the owner reads:

- ``pr_name(pr)``            a GitHub PR/issue dict (title, url, number, repo)
                             -> ``[title](url) (repo#N)``
- ``pr_title(ref)``          title for a PR/issue URL or ``owner/repo#N`` label,
                             from the cache or one ``gh`` call, "" when unknown
- ``linear_title(key)``      title for a Linear key (``EXBOT-4219``), cached,
                             one ``switchboard linear issue`` call
- ``task_name(task_id)``     ``<emoji> <Workstream Name> (rt-…)`` from the
                             running-tasks ledger and the workstream config
- ``name_refs(text)``        rewrites every bare reference in a line so the
                             title leads and the id follows in parentheses or
                             sits in the link; refs already inside parentheses
                             or markdown link text are left alone

Titles are cached in ``.state/named-refs-cache.json`` on the main checkout
(``repo_paths.main_checkout``), never in a worktree. Lookups are bounded:
``NAMED_REFS_MAX_FETCH`` (default 4) network calls per process, each with an
8 s timeout; ``NAMED_REFS_OFFLINE=1`` disables fetching (tests, digests that
must never block); ``NAMED_REFS_CACHE`` overrides the cache path. Nothing
here raises: an unknown title falls back to the repo name plus the id in
parentheses ("the example-repo PR (example-repo#10001)"), which still names the
thing before the number.

CLI::

    python3 system/scripts/named_refs.py name "<line>"     # rewrite one line
    python3 system/scripts/named_refs.py task rt-xxxxxxxxxxxx
    python3 system/scripts/named_refs.py pr <url|owner/repo#N>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from repo_paths import main_checkout  # noqa: E402

REPO_ROOT = main_checkout(SCRIPT_DIR.parents[1])
DEFAULT_OWNER = "acme"
LINEAR_ORG = "acme"
CACHE_TTL_SECONDS = 14 * 24 * 3600
FETCH_TIMEOUT = 8
_MAX_TITLE = 80

_fetches_done = 0

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

def _cache_path() -> Path:
    override = os.environ.get("NAMED_REFS_CACHE")
    return Path(override) if override else REPO_ROOT / ".state" / "named-refs-cache.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
        _os_replace(tmp, path)
    except OSError:
        pass


def _cached(cache: dict, key: str) -> str:
    row = cache.get(key)
    if not isinstance(row, dict):
        return ""
    if time.time() - float(row.get("at", 0)) > CACHE_TTL_SECONDS and not row.get("title"):
        return ""
    return str(row.get("title") or "")


def _remember(key: str, title: str) -> None:
    cache = _load_cache()
    cache[key] = {"title": title, "at": time.time()}
    _save_cache(cache)


def _may_fetch() -> bool:
    global _fetches_done
    if os.environ.get("NAMED_REFS_OFFLINE") == "1":
        return False
    limit = int(os.environ.get("NAMED_REFS_MAX_FETCH", "4") or 0)
    if _fetches_done >= limit:
        return False
    _fetches_done += 1
    return True


def _run_json(cmd: list[str]) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clip(title: str) -> str:
    title = " ".join(str(title or "").split())
    return title if len(title) <= _MAX_TITLE else title[: _MAX_TITLE - 1].rstrip() + "…"


# --- GitHub ---------------------------------------------------------------------------

_PR_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?P<kind>pull|issues)/(?P<num>\d+)(?P<tail>[/#?][^\s)\]>]*)?"
)
_PR_LABEL = re.compile(r"(?<![\w/#-])(?:(?P<owner>[\w.-]+)/)?(?P<repo>[A-Za-z][\w.-]*)#(?P<num>\d+)\b")


def parse_pr_ref(ref: str) -> tuple[str, str, str, int] | None:
    """(owner, repo, kind, number) for a GitHub PR/issue URL or ``[owner/]repo#N``."""
    ref = (ref or "").strip()
    m = _PR_URL.match(ref)
    if m:
        return m["owner"], m["repo"], ("pull" if m["kind"] == "pull" else "issues"), int(m["num"])
    m = _PR_LABEL.fullmatch(ref)
    if m:
        return (m["owner"] or DEFAULT_OWNER), m["repo"], "pull", int(m["num"])
    return None


def pr_url(owner: str, repo: str, kind: str, num: int) -> str:
    return f"https://github.com/{owner}/{repo}/{kind}/{num}"


def pr_label(owner: str, repo: str, num: int) -> str:
    return f"{repo}#{num}" if owner == DEFAULT_OWNER else f"{owner}/{repo}#{num}"


def remember_pr(pr: dict) -> None:
    """Cache the title of a PR dict a collector already holds, so later
    renderers never need a network call for it."""
    title = _clip(pr.get("title") or "")
    url = str(pr.get("url") or "")
    if title and url:
        _remember(url.split("#", 1)[0].rstrip("/"), title)


def pr_title(ref: str) -> str:
    parsed = parse_pr_ref(ref)
    if not parsed:
        return ""
    owner, repo, kind, num = parsed
    key = pr_url(owner, repo, kind, num)
    cache = _load_cache()
    hit = _cached(cache, key)
    if hit:
        return hit
    alt = pr_url(owner, repo, "issues" if kind == "pull" else "pull", num)
    hit = _cached(cache, alt)
    if hit:
        return hit
    if key in cache and not hit:
        return ""  # a recent miss: do not re-fetch every render
    if not _may_fetch():
        return ""
    data = _run_json(["gh", "pr" if kind == "pull" else "issue", "view", key, "--json", "title"])
    if not data and kind == "pull":
        data = _run_json(["gh", "issue", "view", alt, "--json", "title"])
    title = _clip(data.get("title") or "")
    _remember(key, title)
    return title


def pr_name(pr: dict) -> str:
    """``[title](url) (repo#N)`` for a PR/issue dict as the GitHub collectors
    store it (title, url, number, repository.nameWithOwner). Falls back to the
    repo name when the title is missing, so the number never leads."""
    repo_full = (pr.get("repository") or {}).get("nameWithOwner") or ""
    owner, _, repo = repo_full.rpartition("/")
    owner = owner or DEFAULT_OWNER
    num = pr.get("number")
    url = str(pr.get("url") or "")
    if not url and repo and num is not None:
        url = pr_url(owner, repo, "pull", int(num))
    title = _clip(pr.get("title") or "") or (pr_title(url) if url else "")
    label = pr_label(owner, repo or "?", num if num is not None else "?")
    if not title:
        title = f"the {repo or 'GitHub'} PR"
    lead = f"[{title}]({url})" if url else title
    return f"{lead} ({label})"


# --- Linear -----------------------------------------------------------------------------

_LINEAR_KEY = re.compile(r"(?<![\w/-])(?P<key>[A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
_LINEAR_URL = re.compile(r"https://linear\.app/[\w-]+/issue/(?P<key>[A-Z][A-Z0-9]{1,9}-\d{1,6})(?:/[^\s)\]>]*)?")
_LINEAR_TEAMS_SKIP = {"UTC", "PT", "ISO", "RT", "T", "SHA", "PR", "CI", "API", "QA", "ID", "UI", "UX", "AI", "MCP", "SSO", "EOD", "SOD", "DM", "CEO", "COS", "P", "LD", "GPT", "COVID", "HTTP", "RFC"}


def linear_url(key: str) -> str:
    return f"https://linear.app/{LINEAR_ORG}/issue/{key}"


def linear_title(key: str) -> str:
    key = (key or "").strip().upper()
    if not _LINEAR_KEY.fullmatch(key):
        return ""
    url = linear_url(key)
    cache = _load_cache()
    hit = _cached(cache, url)
    if hit:
        return hit
    if url in cache:
        return ""
    if not _may_fetch():
        return ""
    data = _run_json(["switchboard", "linear", "issue", "--id", key])
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else data
    title = _clip((issue or {}).get("title") or "")
    _remember(url, title)
    return title


# --- tasks ------------------------------------------------------------------------------

_TASK_ID = re.compile(r"\brt-[0-9a-f]{12}\b")


def _config_scalar(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            value = stripped.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value
    return ""


def titleize(slug: str) -> str:
    special = {"ai": "AI", "ios": "iOS", "pr": "PR", "os": "OS", "pm": "PM"}
    return " ".join(special.get(p, p.capitalize()) for p in slug.replace("_", "-").split("-") if p)


def workstream_title(slug: str) -> str:
    """``<emoji> <Name>`` for a workstream slug, exactly as the owner tab shows it."""
    slug = (slug or "").strip()
    if not slug:
        return ""
    cfg = REPO_ROOT / "workstreams" / slug / "config.yaml"
    if not cfg.exists():
        cfg = REPO_ROOT / "workstreams" / "_archive" / slug / "config.yaml"
    return f"{_config_scalar(cfg, 'emoji') or '📁'} {_config_scalar(cfg, 'name') or titleize(slug)}"


def _ledger_row(task_id: str) -> dict:
    path = REPO_ROOT / "system" / "state" / "running-tasks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("tasks") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        row = rows.get(task_id)
        return row if isinstance(row, dict) else {}
    for row in rows or []:
        if isinstance(row, dict) and (row.get("id") or row.get("task_id")) == task_id:
            return row
    return {}


def task_name(task_id: str, workstream: str = "") -> str:
    """``<emoji> <Workstream Name> (rt-…)``; the slug comes from the ledger row
    unless the caller already knows it. An unknown task is still named by its
    kind: ``a switchboard task (rt-…)``."""
    task_id = (task_id or "").strip()
    slug = workstream or str(_ledger_row(task_id).get("workstream") or "")
    if slug:
        return f"{workstream_title(slug)} ({task_id})" if task_id else workstream_title(slug)
    return f"a switchboard task ({task_id})" if task_id else ""


def set_fetch_budget(n: int) -> None:
    """Cap network lookups for this process (digests with a time budget pass a
    small number; 0 means cache only)."""
    os.environ["NAMED_REFS_MAX_FETCH"] = str(max(0, int(n)))


_TASK_LEAD = re.compile(r"^\s*\[(?P<id>rt-[0-9a-f]{12})(?:\s+(?P<slug>[A-Za-z0-9_-]+))?\]\s*")


def task_lead(task_id: str, workstream: str = "") -> str:
    """``[<emoji> <Workstream Name> · rt-…]``: the lead every worker report,
    dispatch message, and Updates line opens with (Rule 13)."""
    title = workstream_title(workstream or str(_ledger_row(task_id).get("workstream") or ""))
    if not title:
        return f"[{task_id}]"
    return f"[{title} · {task_id}]" if task_id else f"[{title}]"


def relabel_task_lead(text: str, workstream: str = "") -> str:
    """Rewrite a legacy ``[rt-… slug] rest`` lead (stored summaries, old
    reports) to the named shape; text without that lead is returned as is."""
    if not isinstance(text, str):
        return text
    m = _TASK_LEAD.match(text)
    if not m:
        return text
    slug = workstream or m["slug"] or ""
    return f"{task_lead(m['id'], slug)} {text[m.end():]}"


# --- line rewriting ---------------------------------------------------------------------

_MD_LINK = re.compile(r"\[[^\]\n]*\]\([^)\s]*\)")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_SLACK_LINK = re.compile(r"<(?:https?://|@|#)[^>\n]*>")


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Spans the rewriter never touches: markdown links and parenthesised
    groups (the ref already follows a name), fenced ``` blocks (a draft for
    the owner stays verbatim), and Slack ``<url|label>`` / ``<@U…>`` forms."""
    spans = [(m.start(), m.end()) for m in _MD_LINK.finditer(text)]
    spans += [(m.start(), m.end()) for m in _FENCE.finditer(text)]
    spans += [(m.start(), m.end()) for m in _SLACK_LINK.finditer(text)]
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
                start = -1
    return spans


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def name_refs(text: str, *, tasks: bool = True) -> str:
    """Rewrite bare references so the name leads and the id follows.

    - ``https://github.com/o/r/pull/N`` -> ``[title](url) (r#N)``
    - ``o/r#N`` or ``r#N``               -> ``[title](url) (r#N)``
    - ``EXBOT-4219``                   -> ``[title](linear url) (EXBOT-4219)``
    - ``rt-xxxxxxxxxxxx`` (tasks=True)  -> ``<emoji> <Workstream> (rt-…)``

    A ref already inside parentheses or inside a markdown link is left as it
    is, so a line this function already rewrote is stable. A title that is not
    known and cannot be fetched falls back to ``the <repo> PR (r#N)`` /
    ``the Linear issue (KEY)``, which still puts a name before the number.
    Never raises.
    """
    if not text or not isinstance(text, str):
        return text
    try:
        return _name_refs(text, tasks=tasks)
    except Exception:  # noqa: BLE001 - a renderer must never fail on a label
        return text


def name_refs_plain(text: str, *, tasks: bool = True) -> str:
    """``name_refs`` for a link label: the rewritten refs become plain
    ``title (r#N)`` text (a label cannot hold a nested link). Pre-existing
    markdown links are flattened to their label too, so the result is safe
    inside ``[...]``."""
    named = name_refs(text, tasks=tasks)
    if not isinstance(named, str):
        return named
    return re.sub(r"\[([^\]\n]*)\]\([^)\s]*\)", r"\1", named)


def _name_refs(text: str, *, tasks: bool) -> str:
    out = text
    # 1. URLs first (they would otherwise match the label pattern on "pull/N").
    spans = _protected_spans(out)
    pieces: list[tuple[int, int, str]] = []
    for m in _PR_URL.finditer(out):
        if _inside(spans, m.start(), m.end()):
            continue
        owner, repo, kind, num = m["owner"], m["repo"], ("pull" if m["kind"] == "pull" else "issues"), int(m["num"])
        url = m.group(0)
        title = pr_title(url) or f"the {repo} {'PR' if kind == 'pull' else 'issue'}"
        label = pr_label(owner, repo, num)
        suffix = "" if _label_follows(out, m.end(), label, num) else f" ({label})"
        pieces.append((m.start(), m.end(), f"[{title}]({url}){suffix}"))
    out = _splice(out, pieces)

    spans = _protected_spans(out)
    pieces = []
    for m in _LINEAR_URL.finditer(out):
        if _inside(spans, m.start(), m.end()):
            continue
        key = m["key"]
        title = linear_title(key) or "the Linear issue"
        pieces.append((m.start(), m.end(), f"[{title}]({m.group(0)}) ({key})"))
    out = _splice(out, pieces)

    # 2. repo#N labels.
    spans = _protected_spans(out)
    pieces = []
    for m in _PR_LABEL.finditer(out):
        if _inside(spans, m.start(), m.end()):
            continue
        owner = m["owner"] or DEFAULT_OWNER
        repo, num = m["repo"], int(m["num"])
        url = pr_url(owner, repo, "pull", num)
        title = pr_title(url) or f"the {repo} PR"
        pieces.append((m.start(), m.end(), f"[{title}]({url}) ({pr_label(owner, repo, num)})"))
    out = _splice(out, pieces)

    # 3. Linear keys.
    spans = _protected_spans(out)
    pieces = []
    for m in _LINEAR_KEY.finditer(out):
        if _inside(spans, m.start(), m.end()):
            continue
        key = m["key"]
        if key.split("-", 1)[0] in _LINEAR_TEAMS_SKIP:
            continue
        title = linear_title(key)
        if not title:
            continue  # an unknown key may be a flag or a code, not an issue: leave it
        pieces.append((m.start(), m.end(), f"[{title}]({linear_url(key)}) ({key})"))
    out = _splice(out, pieces)

    # 4. task ids.
    if tasks:
        spans = _protected_spans(out)
        pieces = []
        for m in _TASK_ID.finditer(out):
            if _inside(spans, m.start(), m.end()):
                continue
            # "[rt-… slug]" leads and "rt-….a1.xxx" attempt ids are left alone.
            if out[m.end():m.end() + 1] == ".":
                continue
            row = _ledger_row(m.group(0))
            slug = str(row.get("workstream") or "")
            if not slug:
                continue
            pieces.append((m.start(), m.end(), f"{workstream_title(slug)} ({m.group(0)})"))
        out = _splice(out, pieces)
    return out


def _label_follows(text: str, pos: int, label: str, num: int) -> bool:
    """True when the text right after `pos` already carries the id in
    parentheses, e.g. ``url (example-repo#10001)`` or ``url (#10001)``."""
    tail = text[pos:pos + len(label) + 12]
    m = re.match(r"\s*\((?P<inner>[^)]*)\)", tail)
    return bool(m) and (label in m["inner"] or f"#{num}" in m["inner"])


def _splice(text: str, pieces: list[tuple[int, int, str]]) -> str:
    if not pieces:
        return text
    out = []
    last = 0
    for start, end, repl in sorted(pieces):
        out.append(text[last:start])
        out.append(repl)
        last = end
    out.append(text[last:])
    return "".join(out)


# --- CLI --------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in {"name", "task", "lead", "pr", "linear"}:
        print("usage: named_refs.py name '<line>' | task <rt-id> [slug] | lead <rt-id> [slug] | pr <url|repo#N> | linear <KEY>",
              file=sys.stderr)
        return 2
    cmd, arg = argv[0], argv[1]
    if cmd == "name":
        print(name_refs(arg))
    elif cmd == "task":
        print(task_name(arg, argv[2] if len(argv) > 2 else ""))
    elif cmd == "lead":
        print(task_lead(arg, argv[2] if len(argv) > 2 else ""))
    elif cmd == "pr":
        parsed = parse_pr_ref(arg)
        if not parsed:
            print("", end="")
            return 1
        owner, repo, kind, num = parsed
        print(pr_name({"title": pr_title(arg), "url": pr_url(owner, repo, kind, num), "number": num,
                       "repository": {"nameWithOwner": f"{owner}/{repo}"}}))
    else:
        print(linear_title(arg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
