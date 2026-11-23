#!/usr/bin/env python3
"""Canonical switchboard sidebar order: three sections, top to bottom.

The order: CoS, then To Do, then Launchpad pinned at the top, workers in a
middle section called 'workers', and Scheduled Jobs and Meetings at the bottom.

  Pinned      CoS, To Do, Launchpad, then Comms Triage where it is on
  Workers     every other tab (worker tabs, owner workstream tabs, P0 cards),
              in their current relative order
  Background  Scheduled Jobs, then Meetings (the Meetings Registry), then any
              other background row (Router N)

This module is the one place the order is defined. It applies on cmux only:
system/cmux-helpers.sh arrange_workspace_sidebar (start of day and every
status publish) pins and move-tops the same pinned list, and
test_sidebar_order.py fails when that list drifts from this one.

wmux cannot order tabs from a script, so the order stays instructions for cmux. switchboard
never reorders, plans drags for, or restarts wmux to order its sidebar.

  python3 system/scripts/sidebar_order.py order     print the sections
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Iterable

SECTIONS: list[dict[str, str]] = [
    {"key": "pinned", "label": "Pinned"},
    {"key": "workers", "label": "Workers"},
    {"key": "background", "label": "Background"},
]

# (key, pattern on the title text with the emoji and agent glyph removed)
PINNED: list[tuple[str, re.Pattern[str]]] = [
    ("cos", re.compile(r"^(CoS|Chief of Staff)$", re.I)),
    ("todo", re.compile(r"^(To ?-?Do|To-?dos?|Todo)$", re.I)),
    ("launchpad", re.compile(r"^Launch ?pad$", re.I)),
    ("comms-triage", re.compile(r"^Comms Triage$", re.I)),
]
BOTTOM: list[tuple[str, re.Pattern[str]]] = [
    ("scheduled-jobs", re.compile(r"^Scheduled Jobs$", re.I)),
    ("meetings", re.compile(r"^Meetings( Registry)?$", re.I)),
]
OTHER_BACKGROUND = re.compile(r"^Router \d+$")

GLYPHS = {"✳", "✻", "✽", "✢", "✶", "✷", "✸", "✹", "✺", "·", "•", "∗", "*", "◆"}


def title_text(title: str) -> str:
    """Title without the agent glyph, the leading emoji, and Amp's suffix."""
    parts = (title or "").strip().split(" ")
    while parts and (parts[0] in GLYPHS or (len(parts[0]) == 1 and 0x2800 <= ord(parts[0]) <= 0x28FF)):
        parts = parts[1:]
    text = " ".join(parts)
    if " - amp - " in text:
        text = text.split(" - amp - ")[0]
    first = text.split(" ")[0] if text else ""
    if first and not first.isascii() and not any(ch.isalnum() and ch.isascii() for ch in first):
        text = " ".join(text.split(" ")[1:])
    return text.strip()


def surface_key(title: str) -> str | None:
    """'cos', 'todo', ... for a fixed surface, else None."""
    text = title_text(title)
    for key, pattern in PINNED + BOTTOM:
        if pattern.match(text):
            return key
    return None


def pinned_keys(comms_on: bool = True) -> list[str]:
    return [key for key, _ in PINNED if comms_on or key != "comms-triage"]


def section_of(title: str, comms_on: bool = True) -> str:
    key = surface_key(title)
    if key in pinned_keys(comms_on):
        return "pinned"
    if key in {k for k, _ in BOTTOM} or OTHER_BACKGROUND.match(title_text(title)):
        return "background"
    return "workers"


def plan(rows: Iterable[dict[str, Any]], comms_on: bool = True) -> dict[str, Any]:
    """Target order for live rows given in their current display order.

    Each row needs `id` and `title`. Returns the sections with their rows, the
    target id order, the live id order, and whether they differ.
    """
    live = [dict(r) for r in rows]
    pins = pinned_keys(comms_on)
    bottom_rank = {key: n for n, (key, _) in enumerate(BOTTOM)}
    buckets: dict[str, list[tuple[tuple[int, int], dict[str, Any]]]] = {s["key"]: [] for s in SECTIONS}
    for index, row in enumerate(live):
        title = str(row.get("title") or "")
        key = surface_key(title)
        section = section_of(title, comms_on)
        if section == "pinned":
            rank = pins.index(key)  # type: ignore[arg-type]
        elif section == "background":
            rank = bottom_rank.get(key or "", len(BOTTOM))
        else:
            rank = 0
        row["section"] = section
        row["surface"] = key
        buckets[section].append(((rank, index), row))
    sections = []
    target: list[dict[str, Any]] = []
    for s in SECTIONS:
        ordered = [row for _, row in sorted(buckets[s["key"]], key=lambda item: item[0])]
        sections.append({**s, "count": len(ordered), "rows": ordered})
        target.extend(ordered)
    target_ids = [str(r.get("id")) for r in target]
    live_ids = [str(r.get("id")) for r in live]
    present = {r["surface"] for r in target if r["section"] == "pinned"}
    return {
        "sections": sections,
        "target": target_ids,
        "live": live_ids,
        "in_order": target_ids == live_ids,
        "missing_pinned": [k for k in pins if k not in present],
    }


def comms_triage_on() -> bool:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import comms_triage_setting  # type: ignore

        for name in ("enabled", "is_enabled", "comms_triage_enabled"):
            fn = getattr(comms_triage_setting, name, None)
            if callable(fn):
                return bool(fn())
    except Exception:  # noqa: BLE001 - the setting reader is optional here
        pass
    value = os.environ.get("SWITCHBOARD_COMMS_TRIAGE", "").strip().lower()
    return value not in {"off", "0", "false", "no", "disabled"}


def render(comms_on: bool = True) -> str:
    lines = []
    for section in SECTIONS:
        if section["key"] == "pinned":
            names = pinned_keys(comms_on)
        elif section["key"] == "background":
            names = [key for key, _ in BOTTOM] + ["Router N"]
        else:
            names = ["worker and workstream tabs, in their current order"]
        lines.append(f"{section['label']}: {', '.join(names)}")
    lines.append("cmux only: wmux cannot order tabs, so switchboard leaves its order alone.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_order = sub.add_parser("order", help="print the three sections")
    p_order.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    comms_on = comms_triage_on()
    if args.json:
        print(json.dumps({"sections": [s["key"] for s in SECTIONS], "pinned": pinned_keys(comms_on),
                          "bottom": [key for key, _ in BOTTOM]}))
    else:
        print(render(comms_on))
    return 0


if __name__ == "__main__":
    sys.exit(main())
