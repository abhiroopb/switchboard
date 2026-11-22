#!/usr/bin/env python3
"""Private workstreams: a folder whose real content stays off git.

A workstream listed in system/config/private-workstreams.txt keeps its data in its
folder on the main checkout, where Dropbox syncs it and Obsidian reads it. Only
CONTEXT.md and config.yaml are tracked, and those hold status and pointers. A matching
.gitignore rule keeps the rest out of `git add`. Everything here is the second layer,
for what ignore rules cannot stop:

  * dispatch.py auto-push and finish refuse a commit that carries a private path, for
    example one added with `git add -f`.
  * The recall index, the knowledge graph, the daily knowledge build and the review
    feed skip the private files, so their text stays out of generated context.
  * Close-out lines and result cards, which land in the tracked CONTEXT.md, say the
    report is withheld instead of repeating it. The ledger row keeps the full text.

The rule follows the folder into workstreams/_archive/. A checkout with no list (a kit
install) has no private workstreams. A list that exists and cannot be read raises, so
the push gate fails closed.
"""
from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

CONFIG = "system/config/private-workstreams.txt"
TRACKED_NAMES = frozenset({"CONTEXT.md", "config.yaml"})
WITHHELD_REPORT = ("report withheld from this tracked file because this workstream keeps private data; "
                   "the full text is on the ledger row.")
WITHHELD_DECISION = ("decision withheld from this tracked file because this workstream keeps private data; "
                     "the full text is on the ledger row.")


def slugs(root: Path | str) -> frozenset[str]:
    """The private workstream slugs named by root's list; empty when there is no list."""
    try:
        return parse_slugs((Path(root) / CONFIG).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()


def parse_slugs(text: str) -> frozenset[str]:
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return frozenset(line for line in lines if line)


def is_private_slug(slug: str, root: Path | str) -> bool:
    """Whether a report for this workstream must stay out of tracked files. An unreadable list
    answers True for any slug: withholding a report costs little, and the ledger row keeps it."""
    try:
        return bool(slug) and slug.casefold() in {s.casefold() for s in slugs(root)}
    except OSError:
        return bool(slug)


def _split(rel: str) -> tuple[str, tuple[str, ...]] | None:
    """(slug, path parts below the workstream folder) for a path in a workstream folder."""
    parts = PurePosixPath(posixpath.normpath(str(rel).replace("\\", "/"))).parts
    if len(parts) < 2 or parts[0].casefold() != "workstreams":
        return None
    start = 2 if parts[1].casefold() == "_archive" else 1
    if len(parts) <= start:
        return None
    return parts[start], parts[start + 1:]


def is_private_path(rel: str, private: frozenset[str] | set[str]) -> bool:
    """True for a repo-relative path inside a private workstream, other than its
    top-level CONTEXT.md and config.yaml. `private` comes from slugs(). Case-insensitive, because
    git can track a path in any case on a filesystem that folds case."""
    found = _split(rel)
    if not found or found[0].casefold() not in {s.casefold() for s in private} or not found[1]:
        return False
    below = found[1]
    return not (len(below) == 1 and below[0] in TRACKED_NAMES)


def gate_pathspecs(private: frozenset[str] | set[str]) -> list[str]:
    """Git pathspecs that cover these private workstreams, live or archived, in any case."""
    out: list[str] = []
    for slug in sorted(private):
        out += [f":(icase)workstreams/{slug}", f":(icase)workstreams/_archive/{slug}"]
    return out
