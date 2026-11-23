#!/usr/bin/env python3
"""slack_format.py against the shared Slack link cases (Mac kit v8 port, 2026-11-13).

The same fixture runs against switchboard-guardrails.ts in
system/amp-plugins/switchboard/slack-link-format.test.ts, so the Python and TypeScript
detectors stay aligned.

Run: python3 system/scripts/test_slack_format.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import slack_format  # noqa: E402

FIXTURE = HERE.parent / "amp-plugins" / "switchboard" / "test-fixtures" / "slack-link-format-cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


class SharedLinkCases(unittest.TestCase):
    def test_text_cases(self) -> None:
        for case in CASES["text"]:
            self.assertEqual(slack_format.bare_urls(case["text"]), case["bare_urls"], case["name"])

    def test_blocks_cases(self) -> None:
        for case in CASES["blocks"]:
            self.assertEqual(slack_format.blocks_bare_urls(case["blocks"]), case["bare_urls"], case["name"])
            # The same payload as a JSON string, the way --blocks carries it.
            self.assertEqual(slack_format.blocks_bare_urls(json.dumps(case["blocks"])), case["bare_urls"], case["name"])

    def test_a_string_that_is_not_json_is_read_whole(self) -> None:
        self.assertEqual(slack_format.blocks_texts("plain https://example.com/x"), ["plain https://example.com/x"])


if __name__ == "__main__":
    unittest.main()
