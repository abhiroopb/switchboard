#!/usr/bin/env python3
"""Stale-external-claim guard for "waiting on the owner" lists.

A note is never evidence that an ask is still open: the PR it names may have
merged since it was written. This guard takes any pending-ask
list (Markdown, JSON, or stdin), finds every GitHub PR/issue URL, Slack
permalink, Gmail thread link and Doc comment link in it, runs the matching
reply_check provider (cached, backoff-aware, read-only) and prints each item
with its live verdict, so a surface pastes only the unresolved ones.

Usage (one line, from any cwd):

  python3 system/scripts/verify-asks.py pending.md              # every item + verdict
  python3 system/scripts/verify-asks.py pending.md --unresolved-only
  cat asks.json | python3 system/scripts/verify-asks.py - --json --live

Input shapes:
  Markdown/text  each line (or bullet) that contains a supported URL is one item;
                 the line text is the item's label.
  JSON           a list of strings, or a list of dicts with ref/source_ref/url
                 and optional title/ask/label; or {"items": [...]}.

Exit code 0 always when the input parsed; the verdicts are the output. Items
with no supported reference are listed as "unverifiable" (kept, never dropped,
because a claim without a live source still needs a human eye).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import reply_check  # noqa: E402

# Every reference shape a reply_check provider can answer for. Order matters
# only for which URL a line with several references is checked against first.
REF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github", re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pull|issues)/\d+(?:[/?#][^\s)>\]]*)?", re.I)),
    ("slack", re.compile(r"https?://[A-Za-z0-9_.-]+\.slack\.com/archives/[A-Z0-9]+/p\d+(?:\?[^\s)>\]]*)?")),
    ("gmail", re.compile(r"https?://mail\.google\.com/[^\s)>\]]*#(?:inbox|all|sent|starred|imp|label/[^/\s]+)/[0-9a-f]{10,}")),
    ("gdoc", re.compile(r"https?://docs\.google\.com/(?:document|presentation|spreadsheets)/d/[A-Za-z0-9_-]+[^\s)>\]]*[?&]disco=[A-Za-z0-9_-]+[^\s)>\]]*")),
)

Checker = Callable[[dict[str, Any]], dict[str, Any]]


def find_refs(text: str) -> list[tuple[str, str]]:
    """[(kind, url)] for every supported reference in ``text``, in order, deduplicated."""
    found: list[tuple[int, str, str]] = []
    for kind, pattern in REF_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), kind, match.group(0).rstrip(".,;:")))
    seen: set[str] = set()
    refs: list[tuple[str, str]] = []
    for _, kind, url in sorted(found):
        key = reply_check.canonical_key(url)
        if key in seen:
            continue
        seen.add(key)
        refs.append((kind, url))
    return refs


def _label_from_line(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", stripped)
    stripped = re.sub(r"^\[[ xX]\]\s+", "", stripped)
    return stripped


_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


def items_from_markdown(text: str) -> list[dict[str, Any]]:
    """One item per line that carries a supported URL, plus every bullet line
    without one (kept as unverifiable so a link-less ask is never silently
    dropped). Headings and prose without links are not items."""
    items = []
    for number, line in enumerate(text.splitlines(), start=1):
        refs = find_refs(line)
        if not refs and not _BULLET_RE.match(line):
            continue
        items.append({"line": number, "label": _label_from_line(line), "refs": refs})
    return items


def items_from_json(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("asks") or payload.get("pending") or []
    if not isinstance(payload, list):
        raise ValueError("JSON input must be a list or an object with an items/asks/pending list")
    items = []
    for index, entry in enumerate(payload, start=1):
        if isinstance(entry, str):
            label, blob = entry, entry
        elif isinstance(entry, dict):
            ref = str(entry.get("ref") or entry.get("source_ref") or entry.get("url") or entry.get("link") or "")
            label = str(entry.get("label") or entry.get("title") or entry.get("ask") or entry.get("text") or ref)
            blob = " ".join(str(v) for v in (ref, label, entry.get("text") or "") if v)
        else:
            continue
        items.append({"line": index, "label": label, "refs": find_refs(blob)})
    return items


def load_items(source: str, *, as_json: bool | None = None) -> list[dict[str, Any]]:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    if as_json is None:
        as_json = source.endswith(".json") or text.lstrip().startswith(("[", "{"))
    if as_json:
        return items_from_json(json.loads(text))
    return items_from_markdown(text)


def verify(items: Iterable[dict[str, Any]], checker: Checker) -> list[dict[str, Any]]:
    """Attach {status, verdicts} to each item.

    status: resolved   every reference is answered live (merged/closed/replied)
            not_asked  a review-framed item whose open PR does not request the owner
                       (author, team fan-out, nobody): a status line, never a
                       review ask
            open       at least one reference is still live-open
            unknown    the provider could not be read (cache miss, backoff, gh down)
            unverifiable no supported reference; kept for a human
    """
    out = []
    for item in items:
        verdicts = []
        label = str(item.get("label") or "")
        for kind, url in item.get("refs") or []:
            verdict = checker({"ref": url})
            evidence = verdict.get("evidence") or {}
            review_ask = reply_check.review_ask_status(label, verdict) if kind == "github" else ""
            why = str(evidence.get("why") or evidence.get("note") or "")
            if review_ask == "not-asked":
                why = f"the owner is not a requested reviewer ({evidence.get('owner_role') or 'none'}); {why}".strip("; ")
            verdicts.append(
                {
                    "kind": kind,
                    "ref": url,
                    "verdict": str(verdict.get("verdict") or "unknown"),
                    "source": str(verdict.get("source") or ""),
                    "label": reply_check.answered_label(verdict),
                    "why": why,
                    "review_ask": review_ask,
                    "checked_at": str(verdict.get("checked_at") or ""),
                }
            )
        if not verdicts:
            status = "unverifiable"
        elif all(v["verdict"] in reply_check.ANSWERED_VERDICTS for v in verdicts):
            status = "resolved"
        elif any(v["verdict"] in ("open", "teammate_replied") for v in verdicts):
            open_github = [v for v in verdicts if v["kind"] == "github" and v["verdict"] in ("open", "teammate_replied")]
            if open_github and all(v["review_ask"] == "not-asked" for v in open_github):
                status = "not_asked"
            else:
                status = "open"
        else:
            status = "unknown"
        out.append({**item, "status": status, "verdicts": verdicts})
    return out


STATUS_MARK = {"resolved": "✅", "not_asked": "🚫", "open": "⏳", "unknown": "❓", "unverifiable": "➖"}
# Statuses `--unresolved-only` hides: nothing for the owner to do on either.
SETTLED = {"resolved", "not_asked"}


def render_text(results: list[dict[str, Any]], *, unresolved_only: bool) -> str:
    lines = []
    counts = {k: 0 for k in STATUS_MARK}
    for item in results:
        counts[item["status"]] += 1
        if unresolved_only and item["status"] in SETTLED:
            continue
        lines.append(f"{STATUS_MARK[item['status']]} {item['status']}: {item['label']}")
        for v in item["verdicts"]:
            detail = v["label"] or v["why"] or v["verdict"]
            lines.append(f"    {v['kind']} {v['verdict']} — {detail} ({v['ref']})")
    summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
    lines.append(f"-- {len(results)} item(s): {summary or 'none'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("source", help="markdown/text/JSON file with the pending asks, or - for stdin")
    parser.add_argument("--json-input", action="store_true", help="force JSON parsing of the input")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--live", action="store_true", help="ignore the reply-check cache (one live read per reference)")
    parser.add_argument("--unresolved-only", action="store_true", help="omit items already resolved live, and review-framed PRs that do not request the owner (not_asked)")
    args = parser.parse_args(argv)

    try:
        items = load_items(args.source, as_json=True if args.json_input else None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"verify-asks: cannot read {args.source}: {exc}", file=sys.stderr)
        return 2

    def checker(item: dict[str, Any]) -> dict[str, Any]:
        return reply_check.check_item(item, force=args.live)

    results = verify(items, checker)
    if args.json:
        shown = [r for r in results if not (args.unresolved_only and r["status"] in SETTLED)]
        print(json.dumps({"items": shown, "counts": {s: sum(1 for r in results if r["status"] == s) for s in STATUS_MARK}}, indent=2))
    else:
        print(render_text(results, unresolved_only=args.unresolved_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
