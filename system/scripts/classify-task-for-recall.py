#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys


TRIVIAL_PREFIXES = (
    "date",
    "time",
    "pwd",
    "ls",
    "cat",
    "echo",
    "status",
)

TASK_CLASSES = {
    "meeting-prep": ("meeting", "prep", "1:1", "sync", "standup"),
    "status-update": ("status", "weekly update", "summary", "rollup"),
    "stakeholder-comms": ("slack", "email", "draft", "reply", "stakeholder"),
    "prd": ("prd", "spec", "requirements", "proposal"),
    "project-work": ("workstream", "project", "implementation", "cutover", "migration"),
    "historical-question": ("what happened", "last time", "previous", "history", "thread"),
}


def classify(task: str, context: str | None = None) -> dict:
    text = f"{task} {context or ''}".strip().lower()
    compact = re.sub(r"\s+", " ", text)

    if len(compact) < 18 or compact.startswith(TRIVIAL_PREFIXES):
        return {
            "task_class": "trivial-utility",
            "should_recall": False,
            "rationale": "Short utility-like task. Skip recall.",
        }

    for task_class, keywords in TASK_CLASSES.items():
        if any(keyword in compact for keyword in keywords):
            return {
                "task_class": task_class,
                "should_recall": True,
                "rationale": f"Matched {task_class} keywords.",
            }

    return {
        "task_class": "general-substantive",
        "should_recall": True,
        "rationale": "Default to bounded recall for substantive work.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify whether a task should trigger bounded recall.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--context")
    args = parser.parse_args()
    print(json.dumps(classify(args.task, args.context), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
