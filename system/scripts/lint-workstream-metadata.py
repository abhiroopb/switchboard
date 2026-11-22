#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from workstream_metadata import YAML_ERROR_KEY, load_yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_STATUSES = {
    "active",
    "paused",
    "waiting",
    "blocked",
    "needs follow-up",
    "needs action",
    "needs review",
    "new",
    "reference",
    "done",
    "resolved",
    "cancelled",
    "archived",
}
STATUS_ALIASES = {"complete", "completed", "closed", "planning", "canceled"}
PLACEHOLDER_DESCRIPTION = re.compile(
    r"pending refinement|\btbd\b|refine (?:as|on|the objective)|auto-created from .+ on \d{4}-\d{2}-\d{2}\s*$",
    re.IGNORECASE,
)
PLACEHOLDER_NEXT_STEP = re.compile(
    r"clarify the concrete deliverable|refine objective|identify (?:the )?people / cross-team dependencies|do the first unit of work",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    workstream: str
    field: str
    message: str


def lint_workstreams(repo_root: Path, *, today: date | None = None) -> tuple[int, list[Finding]]:
    workstreams_root = repo_root / "workstreams"
    findings: list[Finding] = []
    checked = 0
    current_date = today or date.today()

    if not workstreams_root.is_dir():
        return 0, [Finding("workstreams", "directory", "workstreams directory is missing")]

    for config_path in sorted(workstreams_root.glob("*/config.yaml")):
        checked += 1
        slug = config_path.parent.name
        config = load_yaml(config_path)
        if not isinstance(config, dict) or not config:
            findings.append(Finding(slug, "config", "config is empty or unreadable"))
            continue
        if config.get(YAML_ERROR_KEY):
            findings.append(Finding(slug, "config", f"config.yaml is not valid YAML ({config[YAML_ERROR_KEY]})"))

        description = str(config.get("description") or "").strip()
        if not description:
            findings.append(Finding(slug, "description", "description is missing"))
        elif PLACEHOLDER_DESCRIPTION.search(description):
            findings.append(Finding(slug, "description", "description still contains placeholder text"))

        raw_status = str(config.get("status") or "").strip().lower().replace("_", " ")
        if not raw_status:
            findings.append(Finding(slug, "status", "status is missing"))
        elif raw_status not in CANONICAL_STATUSES | STATUS_ALIASES:
            findings.append(Finding(slug, "status", f"unsupported status {raw_status!r}"))

        raw_date = str(config.get("last_worked_on") or "").strip()
        if not raw_date:
            findings.append(Finding(slug, "last_worked_on", "last-worked date is missing"))
        else:
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError:
                findings.append(Finding(slug, "last_worked_on", f"invalid ISO date {raw_date!r}"))
            else:
                if parsed_date > current_date:
                    findings.append(Finding(slug, "last_worked_on", f"date {raw_date} is in the future"))

        next_step = str(config.get("next_step") or "").strip()
        if next_step and PLACEHOLDER_NEXT_STEP.search(next_step):
            findings.append(Finding(slug, "next_step", "next step still contains placeholder text"))

    return checked, findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report incomplete workstream metadata without blocking scheduled maintenance."
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when warnings are present.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    checked, findings = lint_workstreams(args.repo_root)
    print(f"workstream-metadata-lint: {checked} workstreams checked, {len(findings)} warnings")
    for finding in findings:
        print(f"WARN {finding.workstream} {finding.field}: {finding.message}")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
