#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from repo_paths import main_checkout
from workstream_metadata import TERMINAL_STATUSES, archived_workstream_names, load_workstream_metadata


SECTION_ORDER = [
    ("Active", {"active", "paused"}),
    ("Waiting / Blocked", {"waiting", "blocked", "needs follow-up"}),
    ("Needs Action / Review", {"needs action", "needs review", "new"}),
    ("Reference", {"reference"}),
]


def render_table(items: list[dict[str, str]]) -> list[str]:
    if not items:
        return ["_None_"]

    lines = [
        "| Workstream | Status | Last Worked | Next Step |",
        "|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| [{item['slug']}](./{item['slug']}/) | {item['status_label']} | {item['last_worked_display']} | {item['next_step']} |"
        )
    return lines


def tracked_workstream_names(repo_root: Path) -> set[str]:
    """Workstream slugs tracked at main's HEAD (the commit a task worktree branched
    from). Empty when `repo_root` is not a git checkout."""
    main_root = main_checkout(repo_root)
    probe = subprocess.run(
        ["git", "-C", str(main_root), "ls-tree", "--name-only", "HEAD", "workstreams/"],
        capture_output=True, text=True,
    )
    if probe.returncode:
        return set()
    return {
        Path(line).name for line in probe.stdout.splitlines()
        if line.strip() and Path(line).name not in {"shared", "_archive", "README.md"}
    }


def collect_metadata(repo_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Workstreams of `repo_root`, plus any that exist only in the main checkout.

    Several workstreams are gitignored or never committed (`workstreams/feedback-*/`,
    `chief-of-staff`, `recipe-app`, ...), so a linked worktree under
    `.amp/worktrees/<task>` does not contain them, and a render from a worktree must
    not drop their rows from the README that lands on main. The worktree's own view wins for every slug it has, so a branch
    that moves or edits a workstream still renders its intent. A slug that is
    tracked in git but absent from the worktree was deleted by the branch (a fold
    or rename), so main's copy is not re-added either."""
    metadata = load_workstream_metadata(repo_root)
    archived = archived_workstream_names(repo_root)
    main_root = main_checkout(repo_root)
    if main_root.resolve() != repo_root.resolve():
        seen = {item["slug"] for item in metadata}
        archived_seen = set(archived)
        deleted_by_branch = tracked_workstream_names(repo_root) - seen - archived_seen
        metadata.extend(
            item for item in load_workstream_metadata(main_root)
            if item["slug"] not in seen and item["slug"] not in archived_seen
            and item["slug"] not in deleted_by_branch
        )
        archived = sorted(
            archived_seen | {name for name in archived_workstream_names(main_root) if name not in seen}
        )
    return metadata, archived


def build_readme(repo_root: Path) -> str:
    metadata, archived = collect_metadata(repo_root)
    shared_path = repo_root / "workstreams" / "shared" / "metric-definitions.md"

    lines: list[str] = [
        "# Workstreams",
        "",
        "Active and resumable work. Each folder has a `CONTEXT.md` with status, next steps, and methodology.",
        "",
        "This file is generated from `config.yaml` metadata when present, with a fallback import from legacy `CONTEXT.md` headers during migration. Update `config.yaml`, then rerun `python3 system/scripts/render-workstreams-readme.py --write`.",
        "",
        "## Conventions",
        "",
        "### Dispatch surfaces",
        "CoS is the reporting hub and a dispatch origin. Launchpad is a quiet, pull-based work console and a dispatch origin. Both apply `system/prompts/dispatch-core.md`, route substantive work through `router-dispatch.sh`, and keep delivery in owning workspaces. Origin-tagged callbacks return to their source surface. All unsolicited output stays in CoS.",
        "",
        "### Document storage",
        "Store the primary markdown copy of workstream-generated docs inside the owning workstream by default. If a doc is promoted into `projects/` or `knowledge/` later, keep a link back from the workstream so the working context stays complete.",
        "",
        "### \"Waiting On\" states",
        "Existing collectors and comms-triage handle monitoring between turns. When a workstream enters a waiting state:",
        "1. Register the relevant source in `system/config/priority-channels.yaml` with the owning `default_workstream`.",
        "2. Record the awaited response in `CONTEXT.md`. Do not create a dedicated polling script, cron job, or launchd watcher.",
        "3. Emit a structured update through `system/scripts/emit-workspace-update.py` so the switchboard queue stays current.",
        "",
    ]

    for section_title, statuses in SECTION_ORDER:
        section_items = [
            item
            for item in metadata
            if item["status"] in statuses and item["status"] not in TERMINAL_STATUSES
        ]
        section_items.sort(
            key=lambda item: (
                {"high": 0, "medium": 1, "low": 2}.get(item["priority"], 3),
                item["display_name"].lower(),
            )
        )
        lines.extend([f"## {section_title}", ""])
        lines.extend(render_table(section_items))
        lines.append("")

    lines.extend(["## Shared", ""])
    if shared_path.exists():
        lines.append("- [metric-definitions.md](./shared/metric-definitions.md) — canonical SQL for actives and churn metrics")
    else:
        lines.append("_None_")
    lines.append("")

    terminal = sorted(item["slug"] for item in metadata if item["status"] in TERMINAL_STATUSES)
    combined_archived = []
    seen = set()
    for name in archived + terminal:
        if name in seen:
            continue
        combined_archived.append(name)
        seen.add(name)

    lines.extend(["## Archived", "", "Completed, cancelled, or superseded workstreams live in [`_archive/`](./_archive/). Includes:"])
    if combined_archived:
        for name in combined_archived:
            lines.append(f"- {name}")
    else:
        lines.append("- _None yet_")
    lines.append("")

    return "\n".join(lines)


README_REL = "workstreams/README.md"


def _toplevel(path: Path) -> Path | None:
    probe = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if probe.returncode or not probe.stdout.strip():
        return None
    return Path(probe.stdout.strip())


def resolve_root(explicit: str = "", cwd: Path | None = None) -> Path:
    """The checkout to render: --root when given, else the switchboard checkout that
    holds the current directory, else the checkout this script sits in.

    The script's own path used to win: a worker in
    a task worktree that ran main's copy by its absolute path re-rendered MAIN's
    README, and finish then refused the landing as a stray write. AGENTS.md:
    scripts derive the repo root from the cwd, never their own path."""
    if explicit:
        return Path(explicit).resolve()
    top = _toplevel(cwd or Path.cwd())
    if top is not None and (top / "workstreams").is_dir() and (top / "system" / "scripts").is_dir():
        return top
    return Path(__file__).resolve().parents[2]


def write_readme(repo_root: Path, readme: str) -> bool:
    """Write README.md only when its text changed, always with LF endings (the
    repo's .gitattributes keeps LF in the working tree; text-mode write_text on
    Windows wrote CRLF). An unchanged README keeps its mtime, so an hourly
    render never makes an untouched file look freshly written to finish's
    stray-write check. Returns True when the file was written."""
    target = repo_root / README_REL
    data = (readme + "\n").encode("utf-8")
    try:
        if target.read_bytes() == data:
            return False
    except OSError:
        pass
    target.write_bytes(data)
    return True


def is_linked_worktree(repo_root: Path) -> bool:
    return main_checkout(repo_root).resolve() != repo_root.resolve()


def commit_readme(repo_root: Path) -> dict[str, object]:
    """Commit workstreams/README.md alone on the main checkout when it differs
    from HEAD, as the switchboard worker. A main-checkout render (the hourly context
    snapshot, CoS) used to leave it uncommitted, and the next task whose branch
    also re-rendered it was refused at finish.
    Never in a task worktree: finish commits a branch. The commit is
    path-limited, so anything else staged on main stays out of it."""
    if is_linked_worktree(repo_root):
        return {"committed": False, "reason": "task worktree; finish commits the branch"}
    def g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    tracked = not g("ls-files", "--error-unmatch", "--", README_REL).returncode
    # Content, not stat: a rewrite that only changes line endings or the mtime is not a change.
    if tracked and not g("diff", "--quiet", "HEAD", "--", README_REL).returncode:
        return {"committed": False, "reason": "README matches HEAD"}
    import owner_tabs  # lazy: only a committing run needs the ledger-side helpers

    return owner_tabs.commit_written_paths(repo_root, [README_REL], "workstreams/README.md re-rendered on main")


def landing_lock_path(repo_root: Path) -> Path:
    """dispatch.py's landing lock (LOCKS / landing.lock)."""
    import os
    return Path(os.environ.get("SWITCHBOARD_LOCKS_DIR") or repo_root / ".amp" / "locks") / "landing.lock"


def commit_outside_landing(repo_root: Path) -> dict[str, object]:
    """commit_readme under dispatch's landing lock, taken without waiting. A
    landing in flight resets main's README just before its fast-forward and
    renders it again after; a commit in between would move main under it. So a
    held lock skips this run (the next one commits) instead of racing it."""
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:  # Windows: flock() over msvcrt
        import flock_compat as fcntl  # type: ignore[no-redef]
    path = landing_lock_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return {"committed": False, "reason": "a landing is in progress; the next render commits"}
        try:
            return commit_readme(repo_root)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def register_uncommitted(repo_root: Path) -> None:
    """A render left uncommitted on main is switchboard's own write (owner_tabs
    registry), so finish treats it as generated output, never as a stray edit."""
    if is_linked_worktree(repo_root):
        return
    def g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    untracked = bool(g("ls-files", "--error-unmatch", "--", README_REL).returncode)
    if untracked or g("diff", "--quiet", "HEAD", "--", README_REL).returncode == 1:
        import owner_tabs

        owner_tabs.note_own_write(repo_root, [README_REL])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write back to workstreams/README.md")
    parser.add_argument("--root", default="", help="checkout to render (default: the switchboard checkout holding the cwd)")
    parser.add_argument("--commit", action="store_true",
                        help="with --write on the main checkout: commit README.md alone when it differs from HEAD")
    args = parser.parse_args()

    repo_root = resolve_root(args.root)
    readme = build_readme(repo_root)

    if args.write:
        write_readme(repo_root, readme)
        outcome: dict[str, object] = {}
        if args.commit:
            outcome = commit_outside_landing(repo_root)
            if outcome.get("committed"):
                print(f"render-workstreams-readme: committed {README_REL} ({outcome.get('commit', '')})")
            elif outcome.get("reason") not in {"README matches HEAD"}:
                print(f"render-workstreams-readme: {README_REL} not committed: {outcome.get('reason')}")
        if not outcome.get("committed"):
            register_uncommitted(repo_root)
    else:
        print(readme)


if __name__ == "__main__":
    main()
