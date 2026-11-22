#!/usr/bin/env python3
"""Where shared PM OS state lives when a script runs from a task worktree.

Every task gets its own checkout under `.amp/worktrees/<task-id>` (dispatch.py).
The scripts in that checkout are copies of main's, so anything
derived from `Path(__file__)` points at the worktree, not at main. Shared state
(`system/state/*.json`, the ledgers, receipts) has exactly one home: the main
checkout. Two failure modes motivate this module:

- A worker runs its worktree's `dispatch.py finish`: REPO_ROOT is the worktree
  and `main` never moves.
- A worker saves a draft through its worktree's `triage_ledger.py`.
  `system/state/triage-ledger.json` there is a symlink to main's file, but the
  atomic `os.replace(tmp, path)` replaces the *link* with a real file, so the
  draft stays in the worktree.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

WORKTREES_SEGMENTS = (".amp", "worktrees")


def main_checkout(path: Path) -> Path:
    """The main checkout for `path`. In a linked worktree the common git dir is
    <main>/.git; in the main checkout it is ./.git. Non-git paths come back as-is."""
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if probe.returncode or not probe.stdout.strip():
        return path
    common = Path(probe.stdout.strip())
    return common.parent if common.name == ".git" else path


def inside_worktree(path: Path) -> bool:
    parts = path.parts
    return any(parts[i:i + 2] == WORKTREES_SEGMENTS for i in range(len(parts) - 1))


class WorktreeStateError(RuntimeError):
    """Raised instead of writing shared state into a task worktree."""


def shared_state_file(path: Path) -> Path:
    """The real file behind a shared-state path, so an atomic rename lands on
    main's file and the lock file sits next to it. Refuses a path that still
    resolves inside `.amp/worktrees/<id>` and names the main path instead."""
    real = Path(os.path.realpath(path))
    if inside_worktree(real):
        worktree = real
        while worktree.parent.parts[-2:] != WORKTREES_SEGMENTS:
            worktree = worktree.parent
        main = main_checkout(worktree)
        if main == worktree:  # not a registered worktree: the checkout that owns .amp/
            main = worktree.parents[2]
        rel = real.relative_to(worktree)
        raise WorktreeStateError(
            f"refusing to write shared state inside a task worktree: {real}. "
            f"Shared state lives only in the main checkout: {main / rel}. "
            f"Run the main checkout's script or export the main path explicitly."
        )
    return real
