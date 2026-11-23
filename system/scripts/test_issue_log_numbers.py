#!/usr/bin/env python3
"""issue_log_numbers.py takes the next entry number from every copy of a log and never hands one out twice.

Run: python3 system/scripts/test_issue_log_numbers.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import issue_log_numbers as iln

REL = Path("workstreams/demo/2026-11-12-demo-issues.md")
MAIN_LOG = "# Log\n\n## Fix here\n\n### 1. First\n\n### 2. Second\n\n## Kit\n\n### K1. One\n"
# A worker's unlanded copy: it already added issue 3 and kit item K2.
WORKER_LOG = MAIN_LOG.replace("## Kit", "### 3. Third\n\n## Kit") + "\n### K2. Two\n"


class IssueLogNumbers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.main = self.tmp / "main"
        self.trees = self.tmp / "worktrees"
        for root, text in ((self.main, MAIN_LOG), (self.trees / "rt-aaa", WORKER_LOG), (self.trees / "rt-bbb", None)):
            (root / REL.parent).mkdir(parents=True)
            if text:
                (root / REL).write_text(text, encoding="utf-8")
        env = {"SWITCHBOARD_VAULT_ROOT": str(self.main), "SWITCHBOARD_WORKTREES_DIR": str(self.trees),
               "SWITCHBOARD_ISSUE_LOG_CLAIMS_DIR": str(self.tmp / "claims")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        top = mock.patch.object(iln, "_toplevel", return_value=self.trees / "rt-bbb")
        top.start()
        self.addCleanup(top.stop)

    def test_an_unlanded_entry_in_another_worktree_counts(self) -> None:
        # Main may say 31 is last while a live worker has already written 32.
        self.assertEqual(iln.next_number(REL, "issue"), 4)
        self.assertEqual(iln.next_number(REL, "kit"), 3)

    def test_a_claim_is_never_handed_out_twice(self) -> None:
        first = iln.next_number(REL, "issue", claim=True, task_id="rt-one")
        second = iln.next_number(REL, "issue", claim=True, task_id="rt-two")
        self.assertEqual((first, second), (4, 5))
        self.assertEqual(iln.next_number(REL, "issue"), 6)
        self.assertEqual(iln.next_number(REL, "kit"), 3)
        self.assertEqual((iln.claims_dir(REL) / "issue-4" / "task").read_text(encoding="utf-8").strip(), "rt-one")

    def test_a_claim_lost_to_a_race_takes_the_next_number(self) -> None:
        (iln.claims_dir(REL) / "kit-3").mkdir(parents=True)
        with mock.patch.object(iln, "highest", return_value=2):
            self.assertEqual(iln.next_number(REL, "kit", claim=True), 4)

    def test_check_names_duplicate_headings(self) -> None:
        self.assertEqual(iln.duplicates(MAIN_LOG), {})
        self.assertEqual(iln.duplicates(MAIN_LOG + "\n### 2. Again\n\n### K1. Again\n"), {"issue": ["2"], "kit": ["K1"]})
        # Summary table rows and prose that cite a number are not headings.
        self.assertEqual(iln.duplicates(MAIN_LOG + "\n| **2. Second** | Fixed. |\nSee issue 2 and K1.\n"), {})

    def test_check_catches_the_same_number_in_two_copies(self) -> None:
        # Each worktree wrote "### 3." with its own title: neither file has a duplicate alone.
        other = MAIN_LOG.replace("## Kit", "### 3. Something else\n\n## Kit")
        self.assertEqual(iln.clashes(WORKER_LOG, [MAIN_LOG]), [])
        self.assertEqual(iln.clashes(WORKER_LOG, [other]), ["3"])

    def test_a_missing_log_fails_loudly(self) -> None:
        for argv in (["next", "--log", "workstreams/demo/typo-issues.md", "--kind", "issue", "--claim"],
                     ["check", "--log", "workstreams/demo/typo-issues.md"]):
            with self.subTest(argv=argv[0]), self.assertRaises(SystemExit) as caught:
                iln.main(argv)
            self.assertIn("no copy of", str(caught.exception.code))
        self.assertFalse((self.tmp / "claims").exists())

    def test_claims_are_keyed_by_the_full_log_path(self) -> None:
        iln.next_number(REL, "issue", claim=True)
        self.assertTrue((self.tmp / "claims" / "workstreams__demo__2026-11-12-demo-issues.md" / "issue-4").is_dir())

    def test_the_real_windows_log_has_no_duplicates(self) -> None:
        log = Path(__file__).resolve().parents[2] / "workstreams/switchboard-maintenance/2026-11-12-windows-cos-issues.md"
        if not log.is_file():
            self.skipTest("log not in this checkout")
        self.assertEqual(iln.duplicates(log.read_text(encoding="utf-8")), {})


if __name__ == "__main__":
    unittest.main()
