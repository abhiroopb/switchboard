#!/usr/bin/env python3
"""Next entry numbers for a numbered switchboard issue log, so parallel workers never reuse one.

Two workers that each read their own worktree copy of a log cannot see each
other's unlanded entry, so both could write the same number. This script reads every copy of the log (main's and each live task
worktree's), plus the numbers already claimed, and claims the next one atomically.

  python3 system/scripts/issue_log_numbers.py next --log <repo-relative log> --kind issue --claim --task-id <rt-id>
  python3 system/scripts/issue_log_numbers.py next --log <log> --kind kit          # peek, claims nothing
  python3 system/scripts/issue_log_numbers.py check --log <log>                    # exit 1 on a duplicate or a clash

Entries are "### 34. Title" (kind issue) and "### K21. Title" (kind kit). A claim is a folder
under <main>/.state/issue-log-claims/<log path>/ made with mkdir, which fails when another
worker made it first, so two claims never get the same number. Claims are kept: numbers only
grow, and a claimed number that never lands is a harmless gap. `check` fails on a number used twice
in this checkout's copy, or used here and in another live copy under a different title.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_surface  # noqa: E402

KINDS = {"issue": ("", re.compile(r"^###\s+(\d+)\.\s", re.M)),
         "kit": ("K", re.compile(r"^###\s+K(\d+)\.\s", re.M))}
CLAIM_RE = re.compile(r"^(issue|kit)-(\d+)$")


def _toplevel() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else Path.cwd().resolve()


def relative_log(log: str) -> Path:
    path = Path(log).expanduser()
    if not path.is_absolute():
        # A bare filename from inside a workstream folder means the file there, when it exists.
        here = Path.cwd() / path
        if not here.is_file():
            return path
        path = here
    path = path.resolve()
    for root in (_toplevel(), review_surface.vault_root()):
        try:
            return path.relative_to(root)
        except ValueError:
            pass
    trees = review_surface.worktrees_dir()
    try:
        return Path(*path.relative_to(trees).parts[1:])
    except ValueError:
        raise SystemExit(f"log is outside the repo and its worktrees: {path}")


def log_copies(rel: Path) -> list[Path]:
    """Every existing copy of the log: main's, this checkout's, and each live task worktree's."""
    roots = [review_surface.vault_root(), _toplevel()]
    try:
        roots += sorted(d for d in review_surface.worktrees_dir().iterdir() if d.is_dir())
    except OSError:
        pass
    seen: set[str] = set()
    copies: list[Path] = []
    for root in roots:
        copy = root / rel
        key = os.path.normcase(str(copy.resolve()))
        if key not in seen and copy.is_file():
            seen.add(key)
            copies.append(copy)
    return copies


def claims_dir(rel: Path) -> Path:
    override = os.environ.get("SWITCHBOARD_ISSUE_LOG_CLAIMS_DIR", "").strip()
    base = Path(override).expanduser() if override else review_surface.vault_root() / ".state" / "issue-log-claims"
    return base / rel.as_posix().replace("/", "__")


def numbers_in(text: str, kind: str) -> list[int]:
    return [int(n) for n in KINDS[kind][1].findall(text)]


def highest(rel: Path, kind: str) -> int:
    top = 0
    for copy in log_copies(rel):
        top = max([top, *numbers_in(copy.read_text(encoding="utf-8", errors="replace"), kind)])
    folder = claims_dir(rel)
    if folder.is_dir():
        for entry in folder.iterdir():
            m = CLAIM_RE.match(entry.name)
            if m and m.group(1) == kind:
                top = max(top, int(m.group(2)))
    return top


def next_number(rel: Path, kind: str, claim: bool = False, task_id: str = "") -> int:
    number = highest(rel, kind) + 1
    if not claim:
        return number
    folder = claims_dir(rel)
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            (folder / f"{kind}-{number}").mkdir()
        except FileExistsError:
            number += 1
            continue
        (folder / f"{kind}-{number}" / "task").write_text((task_id or "unknown") + "\n", encoding="utf-8")
        return number


def headings(text: str) -> dict[str, list[str]]:
    """Entry label to the heading titles that carry it ("36" -> ["False ..."], "K22" -> [...])."""
    found: dict[str, list[str]] = {}
    for m in re.finditer(r"^###\s+(K?\d+)\.\s+(.+?)\s*$", text, re.M):
        found.setdefault(m.group(1), []).append(m.group(2))
    return found


def clashes(mine: str, others: list[str]) -> list[str]:
    """Labels this copy shares with another copy under a different title, as when
    two worktrees each write "### 32." and neither file holds a duplicate on its own."""
    own = {label: titles[0] for label, titles in headings(mine).items()}
    out: set[str] = set()
    for text in others:
        for label, titles in headings(text).items():
            if label in own and own[label] not in titles:
                out.add(label)
    return sorted(out, key=lambda x: (x.startswith("K"), int(x.lstrip("K"))))


def duplicates(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for kind, (prefix, _) in KINDS.items():
        nums = numbers_in(text, kind)
        dups = sorted({n for n in nums if nums.count(n) > 1})
        if dups:
            found[kind] = [f"{prefix}{n}" for n in dups]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    nxt = sub.add_parser("next", help="print the next free entry number")
    nxt.add_argument("--log", required=True)
    nxt.add_argument("--kind", choices=sorted(KINDS), required=True)
    nxt.add_argument("--claim", action="store_true", help="reserve the number so no other worker takes it")
    nxt.add_argument("--task-id", default=os.environ.get("SWITCHBOARD_WORKER_TASK_ID", ""))
    chk = sub.add_parser("check", help="exit 1 when two entries share a number")
    chk.add_argument("--log", required=True)
    args = parser.parse_args(argv)

    rel = relative_log(args.log)
    copies = log_copies(rel)
    if not copies:
        raise SystemExit(f"no copy of {rel.as_posix()} in main or any task worktree; pass the repo-relative path")
    if args.cmd == "next":
        number = next_number(rel, args.kind, claim=args.claim, task_id=args.task_id)
        label = f"{KINDS[args.kind][0]}{number}"
        print(json.dumps({"kind": args.kind, "number": number, "label": label, "claimed": args.claim,
                          "log": rel.as_posix()}))
        return 0
    texts = {str(c): c.read_text(encoding="utf-8", errors="replace") for c in copies}
    mine_path = _toplevel() / rel
    mine_key = next((k for k in texts if os.path.normcase(str(Path(k).resolve())) ==
                     os.path.normcase(str(mine_path.resolve()))), str(copies[0]))
    dups = {k: d for k, t in texts.items() if (d := duplicates(t))}
    clash = clashes(texts[mine_key], [t for k, t in texts.items() if k != mine_key])
    ok = not dups.get(mine_key) and not clash
    print(json.dumps({"ok": ok, "checkout": mine_key, "duplicates": dups, "clashes_with_other_copies": clash}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
