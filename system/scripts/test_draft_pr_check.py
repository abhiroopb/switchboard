#!/usr/bin/env python3
"""Tests for draft_pr_check.py.

Wrong impls these tests catch: a PR named twice looked up twice, a short
owner/repo#N form missed, a merged or ready PR flagged, a report that says
why the PR stays a draft still flagged, a failed lookup raising, and the kill
switch being ignored. No test calls gh: every lookup is a fake.

Run: python3 system/scripts/test_draft_pr_check.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import draft_pr_check as dpc  # noqa: E402


def fake(table):
    calls = []

    def lookup(repo, number):
        calls.append((repo, number))
        return table.get((repo, number))
    return lookup, calls


class NamedPrs(unittest.TestCase):
    def test_url_and_short_forms_once_each(self):
        text = ("See https://github.com/acme-org/gizmos/pull/12 and acme-org/gizmos#12, "
                "then acme-org/sprockets#7.")
        self.assertEqual(dpc.named_prs(text), [("acme-org/gizmos", "12"), ("acme-org/sprockets", "7")])

    def test_no_prs(self):
        self.assertEqual(dpc.named_prs("Nothing to see, issue #4 only."), [])


class DraftPrs(unittest.TestCase):
    def test_open_draft_flagged(self):
        lookup, _ = fake({("acme-org/gizmos", "12"): {"isDraft": True, "state": "OPEN", "title": "Add widget",
                                                         "url": "https://github.com/acme-org/gizmos/pull/12"}})
        drafts = dpc.draft_prs("Opened https://github.com/acme-org/gizmos/pull/12", lookup)
        self.assertEqual([d["title"] for d in drafts], ["Add widget"])
        self.assertIn("gh pr ready", dpc.warning_text(drafts))

    def test_ready_merged_or_failed_lookup_not_flagged(self):
        lookup, calls = fake({("o/a", "1"): {"isDraft": False, "state": "OPEN"},
                              ("o/b", "2"): {"isDraft": True, "state": "MERGED"}})
        self.assertEqual(dpc.draft_prs("o/a#1 o/b#2 o/c#3", lookup), [])
        self.assertEqual(len(calls), 3)

    def test_stated_reason_skips_lookups(self):
        lookup, calls = fake({("o/a", "1"): {"isDraft": True, "state": "OPEN"}})
        self.assertEqual(dpc.draft_prs("o/a#1 stays a draft because CI is red.", lookup), [])
        self.assertEqual(calls, [])

    def test_lookup_cap(self):
        lookup, calls = fake({})
        dpc.draft_prs(" ".join(f"o/r#{n}" for n in range(1, 20)), lookup)
        self.assertEqual(len(calls), dpc.MAX_LOOKUPS)


class Cli(unittest.TestCase):
    def test_kill_switch_prints_nothing(self):
        env = {**os.environ, "DRAFT_PR_CHECK_DISABLED": "1"}
        proc = subprocess.run([sys.executable, str(HERE / "draft_pr_check.py"), "check", "--text", "o/a#1"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
