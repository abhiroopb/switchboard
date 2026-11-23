#!/usr/bin/env python3
"""Word counts for every switchboard instruction surface, with deltas against a baseline.

The weekly switchboard improvement pass (scheduled job ``weekly-switchboard-improvement``,
spec in ``workstreams/switchboard-maintenance/weekly-improvement-routine.md``) runs
this first and last, so the review note carries measured before and after
counts instead of estimates.

Usage:
  instruction-word-counts.py                      # table to stdout
  instruction-word-counts.py --json               # JSON to stdout
  instruction-word-counts.py --baseline FILE      # add deltas against a saved JSON run
  instruction-word-counts.py --write FILE         # also save this run as JSON (next baseline)

The repo root comes from ``git rev-parse --show-toplevel`` in the current
directory, so a worker measures its own worktree. Home surfaces are read from
``$HOME``. A surface that is missing or cannot be rendered is reported with
``words: null`` and a reason; the script never fails on one surface.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# (key, path relative to repo root or "~/..." for home). Order is the report order.
FILE_SURFACES: list[tuple[str, str]] = [
    ("repo AGENTS.md", "AGENTS.md"),
    ("~/AGENTS.md", "~/AGENTS.md"),
    ("repo CLAUDE.md", "CLAUDE.md"),
    ("~/.claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
    ("behavior-map.md", "system/behavior-map.md"),
    ("dispatch-core.md", "system/prompts/dispatch-core.md"),
    ("CoS card (dispatch-core-card.md)", "system/prompts/dispatch-core-card.md"),
    ("chief-of-staff-prompt.md", "system/chief-of-staff-prompt.md"),
    ("external-replies.md", "system/prompts/external-replies.md"),
]

SURFACE_HOOK = "system/claude-marketplace/plugins/switchboard/hooks/surface-context.py"


def repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def words(text: str) -> int:
    return len(text.split())


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def file_surface(root: Path, key: str, rel: str) -> dict[str, Any]:
    path = Path(os.path.expanduser(rel)) if rel.startswith("~") else root / rel
    if not path.is_file():
        return {"surface": key, "path": rel, "words": None, "note": "missing"}
    return {"surface": key, "path": rel, "words": words(path.read_text(errors="replace"))}


def launch_prompt_surface(root: Path) -> dict[str, Any]:
    rel = "system/scripts/dispatch.py launch_prompt()"
    try:
        dispatch = load_module(root / "system/scripts/dispatch.py", "switchboard_dispatch_wordcount")
        row = {
            "id": "rt-wordcount000",
            "attempt_id": "rt-wordcount000.a1.000000",
            "workstream": "switchboard-maintenance",
            "origin": "cos",
            "prompt": "",
        }
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            text = dispatch.launch_prompt(row, Path("/tmp/wordcount-worktree"), "worker-wordcount")
        return {"surface": "worker launch prompt", "path": rel, "words": words(text),
                "note": "rendered with an empty request"}
    except Exception as exc:  # noqa: BLE001 - one surface must never fail the run
        return {"surface": "worker launch prompt", "path": rel, "words": None, "note": f"not rendered: {exc}"[:200]}


def hook_reminder_surface(root: Path) -> dict[str, Any]:
    rel = f"{SURFACE_HOOK} reminder()"
    try:
        hook = load_module(root / SURFACE_HOOK, "switchboard_surface_context_wordcount")
        return {"surface": "per-prompt hook reminder (cos)", "path": rel, "words": words(hook.reminder("cos"))}
    except Exception as exc:  # noqa: BLE001
        return {"surface": "per-prompt hook reminder (cos)", "path": rel, "words": None,
                "note": f"not rendered: {exc}"[:200]}


def measure(root: Path) -> dict[str, Any]:
    rows = [file_surface(root, key, rel) for key, rel in FILE_SURFACES]
    rows.append(launch_prompt_surface(root))
    rows.append(hook_reminder_surface(root))
    total = sum(r["words"] for r in rows if isinstance(r.get("words"), int))
    return {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_root": str(root),
        "surfaces": rows,
        "total_words": total,
    }


def add_deltas(result: dict[str, Any], baseline: dict[str, Any]) -> None:
    before = {r.get("surface"): r.get("words") for r in baseline.get("surfaces", [])}
    for row in result["surfaces"]:
        old = before.get(row["surface"])
        row["baseline_words"] = old
        row["delta"] = (row["words"] - old) if isinstance(old, int) and isinstance(row["words"], int) else None
    old_total = baseline.get("total_words")
    result["baseline_total_words"] = old_total
    result["total_delta"] = (result["total_words"] - old_total) if isinstance(old_total, int) else None
    result["baseline_measured_at"] = baseline.get("measured_at")


def render_table(result: dict[str, Any]) -> str:
    has_delta = "baseline_total_words" in result
    head = "| Surface | Words |" + (" Baseline | Delta |" if has_delta else "")
    sep = "|---|---:|" + ("---:|---:|" if has_delta else "")
    lines = [head, sep]

    def fmt(value: Any, signed: bool = False) -> str:
        if value is None:
            return "n/a"
        return f"{value:+,}" if signed else f"{value:,}"

    for row in result["surfaces"]:
        label = row["surface"] + (f" ({row['note']})" if row.get("note") else "")
        line = f"| {label} | {fmt(row['words'])} |"
        if has_delta:
            line += f" {fmt(row.get('baseline_words'))} | {fmt(row.get('delta'), True)} |"
        lines.append(line)
    total = f"| **Total** | **{fmt(result['total_words'])}** |"
    if has_delta:
        total += f" {fmt(result.get('baseline_total_words'))} | {fmt(result.get('total_delta'), True)} |"
    lines.append(total)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of a Markdown table")
    parser.add_argument("--baseline", help="JSON file from an earlier --write run; adds deltas")
    parser.add_argument("--write", help="save this run as JSON (the next run's baseline)")
    args = parser.parse_args(argv)

    root = repo_root()
    result = measure(root)
    if args.baseline:
        base_path = Path(args.baseline)
        try:
            add_deltas(result, json.loads(base_path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            result["baseline_error"] = f"{base_path}: {exc}"
    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(result, indent=2) + "\n")
        tmp.replace(out)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_table(result))
        if result.get("baseline_error"):
            print(f"\nBaseline not read: {result['baseline_error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
