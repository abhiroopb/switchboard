#!/usr/bin/env python3
"""Build compact, advisory ownership evidence for one Router request."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(REPO_ROOT / "system" / "scripts"))

from workstream_metadata import load_workstream_metadata  # noqa: E402


GENERIC_TOKENS = {
    "about", "after", "again", "also", "and", "are", "been", "before",
    "build", "can", "check", "complete", "context", "could", "days", "deliver",
    "did", "digest", "draft", "everything", "first", "fix", "follow", "for",
    "from", "have", "into", "its", "last", "make",
    "need", "please", "progress", "request", "review", "route",
    "run", "should", "example", "summarize", "summary", "test", "tests", "that",
    "the", "their", "this", "through", "update", "want", "was", "were", "with",
    "work", "workflow", "workspace", "workstream", "would", "write", "you", "your",
}
TERMINAL_STATUSES = {"archived", "cancelled", "done", "resolved"}
EXPLICIT_WORKSTREAM = re.compile(r"(?i)\bworkstream\s*:\s*(?:\*\*)?\s*([a-z0-9][a-z0-9-]*)")
EXPLICIT_SCORE = 1000.0
URL_RE = re.compile(r"""<?(?:https?://|www\.)[^\s<>|'"\)\]]+(?:\|[^>]*>|>)?""")
# A linked artifact's title (--source-title, resolved by link_titles.py) names
# the work. A title word that appears in only a few workstreams' name, slug,
# match terms, or description is distinctive; the one workstream it names
# owns the ask ("Maple Proposal" -> a maple-book-club workstream).
SOURCE_TITLE_SCORE = 240.0
TITLE_RARE_MAX = 2
TITLE_NOISE = {"draft", "working", "copy", "final", "notes", "doc", "docs", "document", "sheet", "deck",
               "slides", "plan", "brief", "memo", "guide", "overview", "template", "tracker", "agreement",
               "proposal", "review", "partnership", "partner", "contract", "terms", "untitled"}

# --- named references ------------------------------------------------------------------
# An ask can name its owner without the exact name: a workstream path
# ("workstreams/photo-book-kit/2026-01-15-...md"), a note that lives in a workstream
# ("2026-01-15-example-note.md"), a slug written with its hyphens
# ("pm-os-maintenance"), a shortened name ("Photo Kit" for "Photo Book Kit"), the name's
# words in another form ("flyer and email skills" for "Flyer Skill"), or a PM OS
# script ("dispatch.py"). Each is named evidence (dispatch.py STRONG_EVIDENCE_PREFIXES).
WORKSTREAM_PATH_RE = re.compile(r"(?<![a-z0-9_-])workstreams/([a-z0-9][a-z0-9-]*)(?=/|\b)", re.IGNORECASE)
FILENAME_RE = re.compile(r"(?<![\w/.-])((?:[\w-]+/)*)([\w][\w.-]*\.(?:md|html|png|jpg|jpeg|gif|pdf|csv|json|yaml|txt))\b",
                         re.IGNORECASE)
SCRIPT_RE = re.compile(r"(?<![\w-])([a-z][a-z0-9_-]*\.(?:py|sh|ts))\b", re.IGNORECASE)
MODULE_REF_RE = re.compile(r"(?<![\w.])([a-z][a-z0-9]*_[a-z0-9_]+)\.[a-z_]+", re.IGNORECASE)
PATH_SCORE = 400.0
NOTE_SCORE = 300.0
SYSTEM_SCORE = 250.0
NAME_FORM_SCORE = 200.0
# Words that never tell one workstream from another: the product brands sit in many names.
BRAND_TOKENS = {"acme", "example"}
# Name words count only when they sit this close together in the ask, so a long prompt
# that mentions "garden" in one paragraph and "swap" in another names nobody.
NAME_WINDOW = 6
# PM OS system and tooling work belongs to this owner (AGENTS.md, Conversation Routing).
SYSTEM_OWNER = "pm-os-maintenance"
SYSTEM_DIRS = ("system/scripts", "system/scripts/lib", "system", "system/prompts", "system/config",
               "system/amp-plugins", "system/claude-marketplace/plugins/pm-os/hooks")
SYSTEM_FILES = {"agents.md", "claude.md"}
# Files every workstream has: naming one names nobody.
COMMON_FILES = {"context.md", "config.yaml", "readme.md", "skill.md", "index.html"}
# A note found in more workstreams than this is a common name, not a pointer.
NOTE_MAX_OWNERS = 2
SUBJECT_SCORE = 300.0
# The ask's own words: the owner's quote when the surface quotes them ("the owner: '<words>'",
# 'the owner (<date>, CoS): "<words>"', "the owner's personal ask (verbatim): '<words>'"), else
# the first sentence. What they name is the subject; the rest is context, and context
# names sources from other workstreams (the Photo Kit ask cited a note from another workstream).
QUOTED_SUBJECT_RE = re.compile(
    r"\A\s*(?:Workstream:[^\n]*\n+)?\s*(?:the owner|{{OWNER_USER}})(?:'s)?\b[^\n:\"\u201c']{0,80}:\s*"
    r"(?:[\"\u201c](?P<dq>[^\"\u201d]+)[\"\u201d]|'(?P<single>.+?)'(?=[\s.,;:)!?]|$))", re.IGNORECASE | re.DOTALL)
# "do NOT deploy it to SiteBuilder" names no SiteBuilder owner.
NEGATION_RE = re.compile(r"\b(?:not|never|no|dont|don't|without|avoid|except)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE)
PM_OS_RE = re.compile(r"\bpm[ -]?os\b", re.IGNORECASE)
SYSTEM_PHRASES = re.compile(r"\b(?:router|dispatch(?:er|ed)?\.py|owner tabs?|worker tabs?|cos tab|drift check|"
                            r"pm-os-guardrails|report-to-cos|cmux workspace|launchd job)\b", re.IGNORECASE)


def explicit_workstream(prompt: str) -> str:
    """Slug named by the first ``Workstream: <slug>`` directive, or ``""``.

    CoS and Router prefix routed asks with this line (sometimes after an
    "Original request:" wrapper). It is an instruction, not a hint, so the
    ranking must not let lexical overlap with another owner outvote it."""
    match = EXPLICIT_WORKSTREAM.search(prompt or "")
    return match.group(1).lower().rstrip("-") if match else ""


def tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 3 and token not in GENERIC_TOKENS
    }


def name_tokens(row: dict[str, Any]) -> set[str]:
    text = " ".join([str(row.get("slug") or "").replace("-", " "), str(row.get("display_name") or ""),
                     *[str(t) for t in row.get("todo_match_terms") or []],
                     *[str(t) for t in row.get("signal_match_terms") or []]])
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def word_hits(word: str, pool: set[str]) -> bool:
    """`northwind` hits `northwindbot`: equal, or one is a 4+ letter prefix of the other."""
    return any(word == tok or (len(word) >= 4 and tok.startswith(word)) or (len(tok) >= 5 and word.startswith(tok))
               for tok in pool)


def ask_subject(prompt: str) -> str:
    match = QUOTED_SUBJECT_RE.search(prompt or "")
    if match:
        return match.group("dq") or match.group("single") or ""
    text = (prompt or "").strip()
    text = re.sub(r"\A\s*Workstream:[^\n]*\n+", "", text)
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]
    return first[:240]


def affirmed(phrase: str, text: str) -> bool:
    """`phrase` occurs in `text` at least once outside a negated clause."""
    start = text.find(phrase)
    while start >= 0:
        clause = re.split(r"[.!?;\n]", text[max(0, start - 40):start])[-1]
        if not NEGATION_RE.search(clause):
            return True
        start = text.find(phrase, start + 1)
    return False


def stem(word: str) -> str:
    """`skills` and `skill`, `venues` and `venue`, read as one word."""
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def word_list(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def distinctive_name_words(row: dict[str, Any]) -> list[str]:
    """The words of a workstream's display name (or slug) that can tell it apart:
    3+ letters, not generic, not a product brand."""
    text = str(row.get("display_name") or "") or str(row.get("slug") or "").replace("-", " ")
    return list(dict.fromkeys(w for w in word_list(text)
                              if len(w) >= 3 and w not in GENERIC_TOKENS and w not in BRAND_TOKENS))


def name_words_near(words: list[str], wanted: list[str]) -> list[str]:
    """The largest set of `wanted` words that all occur within NAME_WINDOW words of
    each other in `words` (the ask)."""
    best: list[str] = []
    positions = [i for i, w in enumerate(words) if w in wanted]
    for start in positions:
        found = list(dict.fromkeys(w for w in words[start:start + NAME_WINDOW] if w in wanted))
        if len(found) > len(best):
            best = found
    return best


def names_workstream(text: str, row: dict[str, Any], root: Path | None, live: set[str]) -> str:
    """How `text` (the ask's subject) names this workstream, or "": its path, a
    note in it, its slug, its name, a shortened name, all its name words, or a
    match term."""
    slug = str(row.get("slug") or "")
    lowered = text.lower()
    words = word_list(lowered)
    if slug in named_paths(text):
        return f"workstreams/{slug}/"
    if named_notes(text, root, live).get(slug):
        return named_notes(text, root, live)[slug][0]
    if slug.replace("-", " ") in lowered or slug_literal(slug, lowered):
        return slug
    name = str(row.get("display_name") or "").lower()
    if name and name in lowered:
        return name
    short = abbreviated_name(words, row)
    if short:
        return short
    wanted = distinctive_name_words(row)
    near = name_words_near(words, wanted) if wanted else []
    if len(wanted) >= 2 and len(near) == len(wanted):
        return " ".join(near)
    for phrase in [*(row.get("todo_match_terms") or []), *(row.get("signal_match_terms") or [])]:
        if str(phrase).strip() and affirmed(str(phrase).strip().lower(), lowered):
            return str(phrase).strip().lower()
    return ""


def abbreviated_name(words: list[str], row: dict[str, Any]) -> str:
    """A run of the ask that is the workstream name with one word left out
    ("photo kit" for "Photo Book Kit"): same first and last word, in order,
    at least two words. Returns the run, or ""."""
    name = word_list(str(row.get("display_name") or ""))
    if len(name) < 3:
        return ""
    want = len(name) - 1
    for i in range(len(words) - want + 1):
        run = words[i:i + want]
        if run[0] != name[0] or run[-1] != name[-1]:
            continue
        it = iter(name)
        if all(any(w == n for n in it) for w in run):
            return " ".join(run)
    return ""


def slug_literal(slug: str, text: str) -> bool:
    """The slug written as itself ("pm-os-maintenance"), never as part of a longer
    slug, a file name, or a repo path ("acme/photo-kit")."""
    if "-" not in slug:
        return False
    return re.search(r"(?<![\w/.-])" + re.escape(slug) + r"(?![\w-])", text) is not None


def named_paths(prompt: str) -> set[str]:
    return {m.group(1).lower().rstrip("-") for m in WORKSTREAM_PATH_RE.finditer(prompt)} - {"_archive"}


def named_notes(prompt: str, root: Path | None, slugs: set[str]) -> dict[str, list[str]]:
    """Files the ask names (bare or with a path) that live in a workstream folder,
    top level or one folder down: {slug: [file name, ...]}."""
    if root is None:
        return {}
    found: dict[str, list[str]] = {}
    for match in FILENAME_RE.finditer(prompt):
        name = match.group(2)
        if name.lower() in SYSTEM_FILES or name.lower() in COMMON_FILES:
            continue
        owners = [slug for slug in sorted(slugs)
                  if ((root / "workstreams" / slug / name).is_file()
                      or any((root / "workstreams" / slug).glob(f"*/{glob_escape(name)}")))]
        if 0 < len(owners) <= NOTE_MAX_OWNERS:
            for slug in owners:
                found.setdefault(slug, []).append(name)
    return found


def glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


def named_system_files(prompt: str, root: Path | None) -> list[str]:
    """PM OS scripts, prompts, and instruction files the ask names (dispatch.py,
    review_surface.review_link, AGENTS.md), plus system phrases (router, owner tab)."""
    hits: list[str] = []
    for match in FILENAME_RE.finditer(prompt):
        if match.group(2).lower() in SYSTEM_FILES:
            hits.append(match.group(2))
    if root is not None:
        for name in [m.group(1) for m in SCRIPT_RE.finditer(prompt)]:
            if any((root / d / name).is_file() for d in SYSTEM_DIRS):
                hits.append(name)
        for module in [m.group(1) for m in MODULE_REF_RE.finditer(prompt)]:
            if (root / "system" / "scripts" / f"{module}.py").is_file():
                hits.append(module)
    hits += [m.group(0).lower() for m in SYSTEM_PHRASES.finditer(prompt)]
    return list(dict.fromkeys(hits))


def title_owner(titles: list[str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one live workstream the source titles name, or None.

    Each distinctive title word (4+ letters, not generic, hit by at most
    TITLE_RARE_MAX live workstreams) scores 2 for a hit on a workstream's
    name, slug, or match terms and 1 for a description-only hit. The top
    workstream owns the ask when it scores 2 or more and no other ties it."""
    words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]+", " ".join(titles).lower()))
             if len(w) >= 4 and not w.isdigit() and w not in GENERIC_TOKENS and w not in TITLE_NOISE]
    live = [r for r in rows if str(r.get("status") or "active") not in TERMINAL_STATUSES]
    pools = {str(r.get("slug")): (name_tokens(r), set(re.findall(r"[a-z0-9]+", str(r.get("description") or "").lower())))
             for r in live}
    scores: Counter[str] = Counter()
    named: dict[str, list[str]] = {}
    for word in words:
        hits = {slug: (2 if word_hits(word, names) else 1) for slug, (names, desc) in pools.items()
                if word_hits(word, names) or word_hits(word, desc)}
        if not hits or len(hits) > TITLE_RARE_MAX:
            continue
        for slug, points in hits.items():
            scores[slug] += points
            named.setdefault(slug, []).append(word)
    ranked = scores.most_common(2)
    if not ranked or ranked[0][1] < 2 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None
    slug = ranked[0][0]
    return {"slug": slug, "words": named[slug], "score": ranked[0][1]}


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def channel_routes(prompt: str, path: Path) -> list[dict[str, str]]:
    lowered = prompt.lower()
    matches: list[dict[str, str]] = []
    config = load_yaml(path)
    for tier in ("tier_1", "tier_2"):
        for row in config.get(tier, []) or []:
            if not isinstance(row, dict) or not row.get("default_workstream"):
                continue
            identifiers = [str(row.get(key) or "").strip() for key in ("id", "name")]
            matched = next((value for value in identifiers if value and value.lower() in lowered), "")
            if matched:
                matches.append(
                    {
                        "identifier": matched,
                        "workstream": str(row["default_workstream"]),
                        "evidence": f"system/config/priority-channels.yaml {tier}",
                    }
                )
    return matches


def active_related_tasks(prompt: str, path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    prompt_tokens = tokens(prompt)
    matches: list[tuple[int, dict[str, str]]] = []
    for row in payload.get("tasks", []):
        if not isinstance(row, dict) or row.get("status") not in {"routing", "active"}:
            continue
        task_prompt = str(row.get("prompt") or "")
        overlap = len(prompt_tokens & tokens(task_prompt))
        same_workstream = bool(row.get("workstream") and str(row["workstream"]) in prompt.lower())
        if overlap < 2 and not same_workstream:
            continue
        matches.append(
            (
                overlap,
                {
                    "task_id": str(row.get("id") or ""),
                    "status": str(row.get("status") or ""),
                    "workstream": str(row.get("workstream") or "unassigned"),
                    "summary": str(row.get("summary") or "")[:240],
                },
            )
        )
    return [row for _, row in sorted(matches, key=lambda item: item[0], reverse=True)[:5]]


def request_shape(prompt: str) -> dict[str, Any]:
    lowered = prompt.lower()
    one_shot_terms = [
        term
        for term in ("summarize", "summary", "digest", "recap", "look up", "lookup")
        if term in lowered
    ]
    durable_terms = [
        term
        for term in ("recurring", "every week", "every day", "automate", "implement", "build", "create a workflow")
        if term in lowered
    ]
    if one_shot_terms and not durable_terms:
        classification = "likely-one-shot"
    elif durable_terms:
        classification = "likely-durable"
    else:
        classification = "needs-router-judgment"
    return {
        "classification": classification,
        "one_shot_signals": one_shot_terms,
        "durable_signals": durable_terms,
    }


def rank_workstreams(prompt: str, rows: list[dict[str, Any]], routes: list[dict[str, str]],
                     source_titles: list[str] | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    source_titles = [t for t in source_titles or [] if t.strip()]
    # URLs carry ids and hostnames, never topic words, so lexical overlap and
    # phrase matches read the prompt without them, plus any linked titles.
    plain = URL_RE.sub(" ", prompt)
    lowered = " \n".join([plain, *source_titles]).lower()
    prompt_tokens = tokens(lowered)
    ask_words = word_list(lowered)
    live_slugs = {str(r.get("slug") or "") for r in rows if str(r.get("status") or "active") not in TERMINAL_STATUSES}
    paths = named_paths(plain)
    notes = named_notes(plain, root, live_slugs)
    system_hits = named_system_files(plain, root)
    subject = URL_RE.sub(" ", ask_subject(prompt))
    titled = title_owner(source_titles, rows) if source_titles else None
    document_frequency: Counter[str] = Counter()
    row_tokens: dict[str, set[str]] = {}
    for row in rows:
        slug = str(row.get("slug") or "")
        text = " ".join(
            str(row.get(key) or "")
            for key in ("slug", "display_name", "description", "startup_instruction", "next_step")
        )
        row_tokens[slug] = tokens(text)
        document_frequency.update(row_tokens[slug])

    channel_destinations = {route["workstream"] for route in routes}
    directed = explicit_workstream(prompt)
    subject_hits = {str(r.get("slug") or ""): names_workstream(subject, r, root, live_slugs)
                    for r in rows if str(r.get("slug") or "") in live_slugs and subject.strip()}
    if not any(subject_hits.values()) and PM_OS_RE.search(subject) and SYSTEM_OWNER in subject_hits:
        # "PM OS should prevent this": the system itself, so its maintenance owner.
        subject_hits[SYSTEM_OWNER] = "PM OS"
    ranked: list[dict[str, Any]] = []
    count = max(len(rows), 1)
    for row in rows:
        slug = str(row.get("slug") or "")
        display_name = str(row.get("display_name") or slug)
        status = str(row.get("status") or "active")
        score = 0.0
        evidence: list[str] = []

        if directed and slug == directed:
            score += EXPLICIT_SCORE
            evidence.append(f'explicit directive "Workstream: {slug}"')

        if slug in channel_destinations:
            score += 500
            evidence.append("exact priority-channel owner")

        subject_hit = subject_hits.get(slug, "")
        if subject_hit:
            score += SUBJECT_SCORE
            evidence.append(f'subject names this workstream: "{subject_hit}"')
        elif slug in live_slugs and subject.strip():
            # Most of the name in the ask's own words ("flyer skills" for a three-word
            # name): not proof, but the closest existing owner when nothing names one.
            wanted = distinctive_name_words(row)
            near = name_words_near(word_list(subject.lower()), wanted) if wanted else []
            if len(near) >= 2 and 3 * len(near) >= 2 * len(wanted):
                score += 60
                evidence.append(f"subject has most name words ({len(near)} of {len(wanted)}): " + ", ".join(near))
        if slug in paths:
            score += PATH_SCORE
            evidence.append(f"workstream path named: workstreams/{slug}/")
        if notes.get(slug):
            score += NOTE_SCORE
            evidence.append("note named: " + ", ".join(notes[slug][:3]))
        if slug == SYSTEM_OWNER and system_hits:
            score += SYSTEM_SCORE
            evidence.append("PM OS system named: " + ", ".join(system_hits[:4]))

        normalized_slug = slug.replace("-", " ")
        if normalized_slug and (normalized_slug in lowered or slug_literal(slug, lowered)):
            score += 180
            evidence.append("exact slug phrase")
        exact_name = display_name.lower() in lowered
        if exact_name:
            score += 180
            evidence.append("exact workstream name")
        else:
            short = abbreviated_name(ask_words, row)
            name_words = distinctive_name_words(row)
            near = name_words_near(ask_words, name_words) if name_words else []
            if short:
                score += NAME_FORM_SCORE
                evidence.append(f'abbreviated workstream name "{short}"')
            elif len(name_words) >= 2 and len(near) == len(name_words):
                score += NAME_FORM_SCORE
                evidence.append("all name words in the ask: " + ", ".join(near))
            elif len(near) >= 2:
                score += 40
                evidence.append(f"name words in the ask ({len(near)} of {len(name_words)}): " + ", ".join(near))

        if titled and titled["slug"] == slug:
            score += SOURCE_TITLE_SCORE
            evidence.append(f'source title "{"; ".join(source_titles)[:80]}" names this workstream '
                            f'({", ".join(titled["words"])})')

        for phrase in [*(row.get("todo_match_terms") or []), *(row.get("signal_match_terms") or [])]:
            normalized_phrase = str(phrase).strip().lower()
            if normalized_phrase and affirmed(normalized_phrase, lowered):
                score += 220
                evidence.append(f'match term "{normalized_phrase}"')

        overlap = prompt_tokens & row_tokens.get(slug, set())
        qualified_overlap = overlap if len(overlap) >= 2 or any(len(token) >= 8 for token in overlap) else set()
        if qualified_overlap:
            lexical_score = sum(
                8 + 6 * math.log((count + 1) / (document_frequency[token] + 1))
                for token in qualified_overlap
            )
            score += lexical_score
            evidence.append("shared topic terms: " + ", ".join(sorted(qualified_overlap)[:6]))

        if status in TERMINAL_STATUSES:
            score -= 60
            evidence.append(f"terminal status: {status}")
        elif status == "paused":
            score -= 15
            evidence.append("paused owner")

        if score <= 0:
            continue
        ranked.append(
            {
                "slug": slug,
                "name": display_name,
                "status": status,
                "score": round(score, 1),
                "evidence": evidence,
                # Every shared word, not just the six the evidence line shows (dispatch.weak_owner).
                "shared_terms": sorted(qualified_overlap),
                "description": str(row.get("description") or "")[:300],
                "config": str(row.get("config_path") or f"workstreams/{slug}/config.yaml"),
                "context": str(row.get("context_path") or f"workstreams/{slug}/CONTEXT.md"),
            }
        )
    ranked.sort(key=lambda row: (-row["score"], row["slug"]))
    # The top eight by score, plus every candidate with named evidence: a big
    # workstream's shared words must never push a named owner off the list.
    return [row for i, row in enumerate(ranked)
            if i < 8 or any(not str(e).startswith(("shared topic terms", "terminal status", "paused owner"))
                            for e in row["evidence"])]


def path_owner(prompt: str, rows: list[dict[str, Any]]) -> str:
    """The one live workstream the ask names by path (workstreams/<slug>/...), or "".
    A hard match: it wins over every ranking signal. Only when no other live
    workstream is named by path or by its hyphenated slug: an ask about routing
    itself can name several and is ranked."""
    plain = URL_RE.sub(" ", prompt)
    live = {str(r.get("slug") or "") for r in rows if str(r.get("status") or "active") not in TERMINAL_STATUSES}
    by_path = named_paths(plain) & live
    if len(by_path) != 1:
        return ""
    (slug,) = by_path
    others = {s for s in live if s != slug and slug_literal(s, plain.lower())}
    return "" if others else slug


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--source-title", action="append", default=[],
                        help="Title of an artifact the prompt links to (link_titles.py); repeatable")
    parser.add_argument("--workstreams-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--priority-channels",
        default=str(REPO_ROOT / "system" / "config" / "priority-channels.yaml"),
    )
    parser.add_argument(
        "--running-tasks",
        default=str(REPO_ROOT / "system" / "state" / "running-tasks.json"),
    )
    args = parser.parse_args()

    root = Path(args.workstreams_root)
    routes = channel_routes(args.prompt, Path(args.priority_channels))
    rows = load_workstream_metadata(root)
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "advisory": ("Verify candidate config and CONTEXT files before routing. Only a named signal (match term, slug or "
                     "workstream name in the ask, priority-channel owner) makes a candidate the owner; shared topic terms "
                     "alone are a weak match, and novel or tangential work gets its own workstream."),
        "request_shape": request_shape(args.prompt),
        "explicit_workstream": explicit_workstream(args.prompt),
        "channel_routes": routes,
        "source_titles": args.source_title,
        "candidate_owners": rank_workstreams(args.prompt, rows, routes, args.source_title, root),
        "path_owner": path_owner(args.prompt, rows),
        "subject": ask_subject(args.prompt)[:240],
        "active_related_tasks": active_related_tasks(args.prompt, Path(args.running_tasks)),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
