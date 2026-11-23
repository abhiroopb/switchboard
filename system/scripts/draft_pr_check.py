#!/usr/bin/env python3
"""Flag draft PRs named in a worker report.

Rule: a PR is marked ready for review before the owner hears about it or reviewers are added.
A PR named in a report to the owner is ready for review, or the report says why it stays a draft.
switchboard-guardrails Rule 19 guards the reviewer request itself; this script guards the report.

`report-to-cos.sh` runs `check --text "<summary>"`. It prints one warning line when a named PR is
still a draft, and nothing otherwise. A failed lookup is skipped (fail open). Kill switch:
DRAFT_PR_CHECK_DISABLED=1.

Run tests: python3 system/scripts/test_draft_pr_check.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable, Optional

PR_URL_RE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_SHORT_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d+)\b")
# A report that explains the draft ("stays a draft because …") is not flagged.
DRAFT_REASON_RE = re.compile(r"\b(?:stays?|kept|keep|keeping|left|leave|remains?|staying)\s+(?:as\s+)?(?:a\s+)?draft\b", re.I)
MAX_LOOKUPS = 6
LOOKUP_TIMEOUT_S = 8

Lookup = Callable[[str, str], Optional[dict]]


def named_prs(text: str) -> list[tuple[str, str]]:
    """(owner/repo, number) for each PR the text names, in order, each once."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    hits = [(m.start(), m.group(1), m.group(2)) for m in PR_URL_RE.finditer(text)]
    hits += [(m.start(), m.group(1), m.group(2)) for m in PR_SHORT_RE.finditer(text)]
    for _, repo, number in sorted(hits):
        key = (repo.lower(), number)
        if key in seen:
            continue
        seen.add(key)
        out.append((repo, number))
    return out


def gh_lookup(repo: str, number: str) -> Optional[dict]:
    """{"isDraft", "title", "url", "state"} from gh, or None when the lookup fails."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", number, "--repo", repo, "--json", "isDraft,title,url,state"],
            capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def draft_prs(text: str, lookup: Lookup = gh_lookup) -> list[dict]:
    """Open draft PRs the text names, unless the text says why they stay drafts."""
    if DRAFT_REASON_RE.search(text):
        return []
    drafts = []
    for repo, number in named_prs(text)[:MAX_LOOKUPS]:
        info = lookup(repo, number)
        if info and info.get("isDraft") is True and str(info.get("state", "OPEN")).upper() == "OPEN":
            drafts.append({"repo": repo, "number": number, "title": info.get("title") or "",
                           "url": info.get("url") or f"https://github.com/{repo}/pull/{number}"})
    return drafts


def warning_text(drafts: list[dict]) -> str:
    names = ", ".join(f"{d['title'] or 'PR'} ({d['url']})" for d in drafts)
    return (f"⚠️ Draft PR in report: {names}. Run `gh pr ready <url>` before telling the owner or "
            "requesting reviewers, or say why it stays a draft.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="print a warning line when the text names a draft PR")
    check.add_argument("--text", required=True)
    check.add_argument("--json", action="store_true", help="print the drafts as JSON instead")
    args = parser.parse_args(argv)
    if os.environ.get("DRAFT_PR_CHECK_DISABLED") == "1":
        return 0
    drafts = draft_prs(args.text)
    if args.json:
        print(json.dumps({"drafts": drafts}))
    elif drafts:
        print(warning_text(drafts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
