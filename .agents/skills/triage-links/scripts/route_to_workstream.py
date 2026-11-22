#!/usr/bin/env python3
"""Decide whether a dropped link should land in an existing workstream,
scaffold a new one, or skip routing.

Reuses the matching and CREATE-test logic from
`system/scripts/process-new-inbox.py` so triage-links and the
overnight comms-triage pipeline apply the same rules.

Usage:

    python3 route_to_workstream.py \\
      --channel proj-example-areas \\
      --source-type slack-channel \\
      --source-ref "https://example.slack.com/archives/.../p123" \\
      --body-file /tmp/thread-body.txt

Prints a JSON decision:

    {
      "decision": "match" | "create" | "skip" | "park",
      "workstream": "<slug or null>",
      "reason": "...",
      "create_test": {"passes": bool, "reason": "..."},
      "auto_create_cap_remaining": int,
      "approval_command": "approve route-existing|route-create|skip"
    }

This script never writes anything. It is a pure decision helper.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
INBOX_SCRIPT_DIR = REPO_ROOT / "system" / "scripts"

# Make process-new-inbox importable
if str(INBOX_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(INBOX_SCRIPT_DIR))

# Import via importlib because the filename has a hyphen
import importlib.util

spec = importlib.util.spec_from_file_location(
    "process_new_inbox",
    INBOX_SCRIPT_DIR / "process-new-inbox.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load process-new-inbox.py")
inbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inbox)


def build_record(args: argparse.Namespace) -> dict:
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body

    return {
        "source_type": args.source_type,
        "source_ref": args.source_ref or "",
        "channel": args.channel or "",
        "channel_id": args.channel_id or "",
        "body": body,
        "from": args.from_user or "",
        "subject": args.subject or "",
    }


# System-only workstreams own a system, not a feature. They should only match
# when the body explicitly discusses the system. Keys = workstream slug,
# values = list of substrings (lowercase) that must appear in body/subject for
# the match to stick. If the matched workstream is in this map and the body
# fails the check, drop the match and fall through to no-match handling.
# The kit ships it empty. Example entry for a roadmap-hygiene workstream:
#     "roadmap-hygiene": ["roadmap", "audit", "status update", "field audit", "rollup"],
SYSTEM_ONLY_WORKSTREAMS: dict[str, list[str]] = {}


def filter_system_only_matches(record: dict, matched_ws: list) -> list:
    """Drop matches to system-only workstreams when there is a competing
    feature-workstream match, OR when the body doesn't explicitly discuss
    the system itself.

    Per-feature content/launch threads (e.g. Feature A support content,
    Feature B QA, Feature C design review) belong in their feature
    workstream, not in a roadmap-hygiene workstream, even when the
    feature's roadmap record is part of that workstream's in-scope set.

    Two rules:
    1. If a system workstream AND any feature workstream both match, drop
       the system workstream — the feature wins.
    2. Otherwise, only keep a system workstream if the body has at least
       one of its system-discussion markers.
    """
    if not matched_ws:
        return matched_ws

    feature_matches = [ws for ws in matched_ws if ws not in SYSTEM_ONLY_WORKSTREAMS]
    system_matches = [ws for ws in matched_ws if ws in SYSTEM_ONLY_WORKSTREAMS]

    # Rule 1: feature wins when both present
    if feature_matches and system_matches:
        return feature_matches

    # Rule 2: system workstream needs an explicit marker in the body
    body = ((record.get("body") or "") + " " + (record.get("subject") or "")).lower()
    kept = []
    for ws in matched_ws:
        markers = SYSTEM_ONLY_WORKSTREAMS.get(ws)
        if markers is None:
            kept.append(ws)
            continue
        if any(m in body for m in markers):
            kept.append(ws)
        # else: drop — the match was on a feature term, not the system
    return kept


def decide(record: dict) -> dict:
    keywords = inbox.load_keywords()
    workstream_terms = inbox.load_workstream_terms()
    stakeholders = inbox.load_stakeholders()
    channel_defaults, casual_channels = inbox.load_channels()

    # 1. Channel default mapping (highest-confidence match)
    channel = (record.get("channel") or "").lower()
    channel_id = (record.get("channel_id") or "").lower()
    channel_default = channel_defaults.get(channel) or channel_defaults.get(channel_id)

    # 2. todo_match_terms scoring against existing workstreams
    inbox.score_relevance(record, keywords, stakeholders, workstream_terms, casual_channels)
    matched_ws = record.get("_matched_workstreams") or []

    # System-only workstreams: only match when the body explicitly references
    # the system itself, not a per-feature thread that happens to be tracked
    # there. E.g. a roadmap-hygiene workstream owns field hygiene and audit
    # cadence, not every feature whose roadmap record is in scope.
    matched_ws = filter_system_only_matches(record, matched_ws)

    if channel_default:
        return {
            "decision": "match",
            "workstream": channel_default,
            "reason": f"channel '{record.get('channel')}' has default_workstream mapping",
            "create_test": None,
            "auto_create_cap_remaining": None,
            "approval_command": "approve route-existing",
        }

    if len(matched_ws) == 1:
        return {
            "decision": "match",
            "workstream": matched_ws[0],
            "reason": f"matched workstream '{matched_ws[0]}' via todo_match_terms",
            "create_test": None,
            "auto_create_cap_remaining": None,
            "approval_command": "approve route-existing",
        }

    if len(matched_ws) > 1:
        return {
            "decision": "match",
            "workstream": matched_ws[0],
            "reason": f"multiple matches via todo_match_terms: {matched_ws}; first one suggested, confirm",
            "create_test": None,
            "auto_create_cap_remaining": None,
            "approval_command": "approve route-existing",
            "alternatives": matched_ws,
        }

    # 3. No match — try CREATE test
    passes, reason = inbox.passes_create_test(record, keywords)
    cap_remaining = inbox.MAX_AUTO_CREATES_24H - inbox.recent_auto_creates_count(hours=24)

    if not passes:
        return {
            "decision": "skip",
            "workstream": None,
            "reason": f"no existing workstream matched; CREATE test failed: {reason}",
            "create_test": {"passes": False, "reason": reason},
            "auto_create_cap_remaining": cap_remaining,
            "approval_command": "skip",
        }

    if cap_remaining <= 0:
        slug = inbox.derive_workstream_name(record, keywords) or ""
        return {
            "decision": "park",
            "workstream": None,
            "reason": "CREATE test passed but 24h auto-create cap is hit; park in _inbox/",
            "create_test": {"passes": True, "reason": reason},
            "derived_slug": slug,
            "auto_create_cap_remaining": 0,
            "approval_command": "approve park",
        }

    slug = inbox.derive_workstream_name(record, keywords)
    if not slug:
        return {
            "decision": "park",
            "workstream": None,
            "reason": "CREATE test passed but could not derive a clean project+domain slug",
            "create_test": {"passes": True, "reason": reason},
            "auto_create_cap_remaining": cap_remaining,
            "approval_command": "approve park",
        }

    return {
        "decision": "create",
        "workstream": slug,
        "reason": f"no existing match; CREATE test passed ({reason}); derived slug '{slug}'",
        "create_test": {"passes": True, "reason": reason},
        "auto_create_cap_remaining": cap_remaining,
        "approval_command": "approve route-create",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--channel", default="", help="Slack channel name (without #)")
    p.add_argument("--channel-id", default="", help="Slack channel ID (e.g. CXXXXXXXXXX)")
    p.add_argument("--source-type", default="slack-channel",
                   choices=["slack-channel", "slack-dm", "slack-mention",
                            "gmail", "linear", "github", "gdrive", "web", "other"])
    p.add_argument("--source-ref", default="", help="Canonical URL for the artifact")
    p.add_argument("--body", default="", help="Inline body text")
    p.add_argument("--body-file", default="", help="Path to a file containing the body")
    p.add_argument("--from-user", default="", help="Sender display name")
    p.add_argument("--subject", default="", help="Subject (for emails) or thread title")
    p.add_argument("--format", default="json", choices=["json", "markdown"])
    args = p.parse_args()

    record = build_record(args)
    decision = decide(record)

    if args.format == "json":
        print(json.dumps(decision, indent=2))
    else:
        d = decision["decision"]
        print("## Workstream routing\n")
        print(f"- **Decision:** `{d}`")
        if decision.get("workstream"):
            print(f"- **Workstream:** `{decision['workstream']}`")
        print(f"- **Reason:** {decision['reason']}")
        if decision.get("create_test"):
            ct = decision["create_test"]
            print(f"- **CREATE test:** {'pass' if ct['passes'] else 'fail'} ({ct['reason']})")
        if decision.get("auto_create_cap_remaining") is not None:
            print(f"- **Auto-create cap remaining (24h):** {decision['auto_create_cap_remaining']}")
        print(f"- **Approval command:** `{decision['approval_command']}`")

    return 0


if __name__ == "__main__":
    sys.exit(main())
