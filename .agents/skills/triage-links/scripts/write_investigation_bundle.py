#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from link_intake import InvestigationBundle, empty_bundle, parse_markdown_bundle  # noqa: E402


def merge_lists(base: list[str], extra: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in base + extra:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def load_seed_bundle(path: str | None) -> InvestigationBundle:
    if not path:
        return empty_bundle()
    bundle_path = Path(path).expanduser()
    text = bundle_path.read_text()
    if bundle_path.suffix.lower() == ".json":
        payload = json.loads(text)
        return InvestigationBundle(
            discovered_urls=list(payload.get("discovered_urls", []) or []),
            evidence_notes=list(payload.get("evidence_notes", []) or []),
            affected_sections=list(payload.get("affected_sections", []) or []),
            planned_edits=list(payload.get("planned_edits", []) or []),
            reply_bullets=list(payload.get("reply_bullets", []) or []),
            reply_draft=payload.get("reply_draft"),
        )
    return parse_markdown_bundle(text)


def build_bundle(args: argparse.Namespace) -> InvestigationBundle:
    bundle = load_seed_bundle(args.seed_file)
    bundle.discovered_urls = merge_lists(bundle.discovered_urls, args.discovered_link)
    bundle.evidence_notes = merge_lists(bundle.evidence_notes, args.evidence_note)
    bundle.affected_sections = merge_lists(bundle.affected_sections, args.affected_section)
    bundle.planned_edits = merge_lists(bundle.planned_edits, args.planned_edit)
    bundle.reply_bullets = merge_lists(bundle.reply_bullets, args.reply_bullet)
    if args.reply_draft:
        bundle.reply_draft = args.reply_draft
    return bundle


def bundle_to_dict(bundle: InvestigationBundle) -> dict:
    return {
        "discovered_urls": bundle.discovered_urls,
        "evidence_notes": bundle.evidence_notes,
        "affected_sections": bundle.affected_sections,
        "planned_edits": bundle.planned_edits,
        "reply_bullets": bundle.reply_bullets,
        "reply_draft": bundle.reply_draft,
    }


def write_bundle(path: Path, bundle: InvestigationBundle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle_to_dict(bundle), indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a structured dropped-link investigation bundle for the second-pass planner.")
    parser.add_argument("--output", required=True, help="Output JSON bundle path.")
    parser.add_argument("--seed-file", help="Optional existing JSON or markdown bundle to extend.")
    parser.add_argument("--discovered-link", action="append", default=[], help="Linked target discovered during investigation.")
    parser.add_argument("--evidence-note", action="append", default=[], help="Evidence note gathered during investigation.")
    parser.add_argument("--affected-section", action="append", default=[], help="Affected section name.")
    parser.add_argument("--planned-edit", action="append", default=[], help="Exact planned edit.")
    parser.add_argument("--reply-bullet", action="append", default=[], help="Bullet for the draft reply.")
    parser.add_argument("--reply-draft", help="Full draft reply text.")
    args = parser.parse_args(argv)

    bundle = build_bundle(args)
    output_path = Path(args.output).expanduser()
    write_bundle(output_path, bundle)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
