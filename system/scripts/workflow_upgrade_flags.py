#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "system" / "config" / "workflow-upgrade-flags.yaml"
DEFAULT_LOCAL_PATH = Path.home() / ".config" / "amp" / "workflow-upgrade-flags.local.yaml"
FLAG_KEYS = [
    "recall_enabled",
    "recurrence_enabled",
    "rule_proposals_enabled",
    "import_enabled",
    "explorer_enabled",
]


def default_flags() -> dict[str, bool]:
    return {key: False for key in FLAG_KEYS}


def parse_simple_yaml(path: Path) -> dict[str, bool]:
    parsed: dict[str, bool] = {}
    if not path.exists():
        return parsed
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in FLAG_KEYS:
            continue
        parsed[key] = value.strip().lower() == "true"
    return parsed


def load_workflow_upgrade_flags() -> dict[str, Any]:
    flags = default_flags()
    sources: list[str] = []

    baseline = parse_simple_yaml(BASELINE_PATH)
    if baseline:
        flags.update(baseline)
        sources.append(str(BASELINE_PATH))

    local_path = Path(os.environ.get("PM_OS_WORKFLOW_UPGRADE_FLAGS_PATH", str(DEFAULT_LOCAL_PATH))).expanduser()
    local_flags = parse_simple_yaml(local_path)
    if local_flags:
        flags.update(local_flags)
        sources.append(str(local_path))

    env_overrides = os.environ.get("PM_OS_WORKFLOW_UPGRADE_FLAG_OVERRIDES")
    if env_overrides:
        payload = json.loads(env_overrides)
        for key, value in payload.items():
            if key in FLAG_KEYS:
                flags[key] = bool(value)
        sources.append("env:PM_OS_WORKFLOW_UPGRADE_FLAG_OVERRIDES")

    return {"flags": flags, "sources": sources}


def is_flag_enabled(flag_name: str) -> bool:
    return bool(load_workflow_upgrade_flags()["flags"].get(flag_name, False))
