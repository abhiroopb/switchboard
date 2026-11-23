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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write back to workstreams/README.md")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    readme = build_readme(repo_root)

    if args.write:
        target = repo_root / "workstreams" / "README.md"
        target.write_text(readme + "\n")
    else:
        print(readme)


if __name__ == "__main__":
    main()
