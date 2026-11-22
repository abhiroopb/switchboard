#!/usr/bin/env python3
"""Local rewrites of main's unpushed commits, and the lookups that keep them invisible.

`dispatch.py strip-unpushed` takes a private or confidential file out of the commits on main
that origin has not seen yet, for example a file a commit added before its workstream
went private, which would otherwise make auto-push refuse. Only unpushed commits change, so no force push is ever
needed, and every tip tree stays the same, so nothing on disk moves.

A rewrite gives those commits new ids. Anything that still holds an old id has to be told:
- a task branch created from the old main: `fork_point` finds where it left the old main,
  and finish rebases only the task's own commits onto the new main;
- a ledger landed_commit or a landed branch tip: `on_main` answers "on main" through the map.

Record: system/state/main-rewrites.json, a list of
{"at", "upstream", "old_tip", "new_tip", "paths", "map": {old full sha: new full sha}}
holding only the commits whose id changed. Never pruned by this module; one record is a few
hundred lines.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

FILE_NAME = "main-rewrites.json"

Git = Callable[..., subprocess.CompletedProcess]


def record_path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE_NAME


def load(state_dir: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(record_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("map"), dict)] if isinstance(data, list) else []


def append(state_dir: Path, record: dict[str, Any]) -> None:
    path = record_path(state_dir)
    records = load(state_dir) + [record]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(records, indent=1) + "\n", encoding="utf-8")
    # Windows: Dropbox or a reader holding the file makes os.replace fail with PermissionError for a
    # moment. Retry for about 5 s, then raise, so the caller never moves main without the record.
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


def prune_unapplied(git: Git, state_dir: Path, main: str = "main") -> int:
    """Drop records whose rewrite never reached main: the old tip is still on main and the new
    tip is not (the compare-and-swap lost to a commit that landed meanwhile). Returns how many."""
    records = load(state_dir)
    kept = [r for r in records
            if not (git("merge-base", "--is-ancestor", str(r.get("old_tip") or ""), main).returncode == 0
                    and git("merge-base", "--is-ancestor", str(r.get("new_tip") or ""), main).returncode != 0)]
    if len(kept) != len(records):
        path = record_path(state_dir)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(kept, indent=1) + "\n", encoding="utf-8")
        for attempt in range(50):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 49:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.1)
    return len(records) - len(kept)


def translate(commit: str, records: list[dict[str, Any]]) -> str | None:
    """The newest id of an old main commit (full sha or a unique prefix), following chained
    rewrites; None when no rewrite touched it."""
    commit = (commit or "").strip().lower()
    if len(commit) < 7:
        return None
    found = None
    for record in records:
        current = found or commit
        hits = [new for old, new in record["map"].items() if old.startswith(current)]
        if len(hits) == 1:
            found = hits[0]
    return found


def on_main(git: Git, ref: str, records: list[dict[str, Any]], main: str = "main") -> bool:
    """True when `ref` is on main, directly or as a commit a rewrite replaced."""
    if not ref:
        return False
    if git("merge-base", "--is-ancestor", ref, main).returncode == 0:
        return True
    if not records:
        return False
    full = git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").stdout.strip() or ref
    new = translate(full, records)
    return bool(new) and git("merge-base", "--is-ancestor", new, main).returncode == 0


def fork_point(git: Git, branch: str, records: list[dict[str, Any]], main: str = "main") -> str | None:
    """Where `branch` left an old (rewritten) main, or None when it forks from the current one.

    Oldest record first: a branch made before two rewrites shares a rewritten commit with the
    first one's old tip, and one made between them shares nothing rewritten with it. The answer
    is that shared commit: the branch's own commits are exactly <fork>..<branch>."""
    for record in records:
        old_tip = str(record.get("old_tip") or "")
        if not old_tip or git("cat-file", "-e", f"{old_tip}^{{commit}}").returncode:
            continue
        base = git("merge-base", branch, old_tip).stdout.strip()
        if base and base in record["map"] and git("merge-base", "--is-ancestor", base, main).returncode:
            return base
    return None
