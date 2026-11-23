#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / "system" / "state"
SOURCES_DIR = STATE_DIR / "sources"
OUTPUT_PATH = SOURCES_DIR / "context-sync.json"

SOURCE_CHECKS = {
    "comms": SOURCES_DIR / "comms.json",
    "meetings": SOURCES_DIR / "meetings.json",
    "scheduled_jobs": SOURCES_DIR / "scheduled-jobs.json",
    "todo": SOURCES_DIR / "todo.json",
    "workspace_updates": SOURCES_DIR / "workspace-updates.json",
    "workstreams": SOURCES_DIR / "workstreams.json",
    "queue": STATE_DIR / "queue.json",
    "now": STATE_DIR / "now.json",
}

TOP_LEVEL_INDEXES = [
    REPO_ROOT / "advanced" / "README.md",
    REPO_ROOT / "config" / "README.md",
    REPO_ROOT / "daemon" / "README.md",
    REPO_ROOT / "docs" / "README.md",
    REPO_ROOT / "notes" / "README.md",
    REPO_ROOT / "outputs" / "README.md",
    REPO_ROOT / "projects" / "README.md",
    REPO_ROOT / "references" / "README.md",
    REPO_ROOT / "sub-agents" / "README.md",
    REPO_ROOT / "system" / "README.md",
    REPO_ROOT / "templates" / "README.md",
]


def run_step(label: str, command: list[str], *, write: bool) -> dict[str, object]:
    if not write:
        return {
            "label": label,
            "command": command,
            "ok": True,
            "skipped": True,
            "stdout": "",
            "stderr": "preview: mutation disabled (use --write)",
        }
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "label": label,
        "command": command,
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return round((datetime.now(timezone.utc) - modified).total_seconds() / 3600, 2)


def build_source_status() -> dict[str, object]:
    freshness: dict[str, object] = {}
    stale: list[dict[str, object]] = []
    for label, path in SOURCE_CHECKS.items():
        hours = age_hours(path)
        is_stale = hours is None or hours > 24
        payload = {
            "path": str(path.relative_to(REPO_ROOT)),
            "exists": path.exists(),
            "age_hours": hours,
            "stale": is_stale,
        }
        freshness[label] = payload
        if is_stale:
            stale.append({"label": label, **payload})
    return {"freshness": freshness, "stale": stale}


def build_index_status() -> dict[str, object]:
    missing = [str(path.relative_to(REPO_ROOT)) for path in TOP_LEVEL_INDEXES if not path.exists()]
    return {
        "total_expected": len(TOP_LEVEL_INDEXES),
        "missing": missing,
        "ok": not missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["start", "end", "manual"], default="manual")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    commands = [
        (
            "refresh_workstreams_readme",
            [sys.executable, "system/scripts/render-workstreams-readme.py", "--write"],
        ),
        (
            "refresh_people_summary",
            [
                sys.executable,
                "system/scripts/people_maintenance.py",
                "--summary-json",
                "system/state/sources/people.json",
            ],
        ),
        (
            "refresh_switchboard_state",
            [
                sys.executable,
                "system/scripts/rebuild-switchboard-state.py",
                "--write",
                "--event-source",
                f"context-sync-{args.phase}",
            ],
        ),
    ]
    # This read-only check runs in previews too. Match the skill's preflight:
    # do not refresh indexes until instruction drift is resolved.
    steps = [run_step(
        "instruction_drift",
        [sys.executable, "system/scripts/check-instruction-drift.py", "--strict"],
        write=True,
    )]
    can_refresh = args.write and bool(steps[0]["ok"])
    steps.extend(run_step(label, command, write=can_refresh) for label, command in commands)
    if not steps[0]["ok"]:
        for step in steps[1:]:
            step["stderr"] = "skipped: strict instruction check failed"

    source_status = build_source_status()
    index_status = build_index_status()
    summary = {
        "updated_at": iso_now(),
        "phase": args.phase,
        "steps": steps,
        "sources": source_status,
        "indexes": index_status,
        "ok": all(step["ok"] for step in steps) and index_status["ok"],
    }

    if args.write:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(summary, indent=2) + "\n")

    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
