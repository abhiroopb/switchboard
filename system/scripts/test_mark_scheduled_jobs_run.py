#!/usr/bin/env python3
"""mark-scheduled-jobs-run.py stamps main's job-state.json, even from a task worktree.

2026-11-12: a worker ran the CEO review and stamped `ceo-review` from its
worktree. The stamp went to the worktree's gitignored copy, so main's
start-day.sh still reported the review as due.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ("mark-scheduled-jobs-run.py", "repo_paths.py")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)


class MarkScheduledJobsRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mark-jobs-"))
        self.main = self.tmp / "main"
        (self.main / "system" / "scripts").mkdir(parents=True)
        for name in SCRIPTS:
            shutil.copy(HERE / name, self.main / "system" / "scripts" / name)
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
        git(self.main, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
        self.worktree = self.tmp / "wt"
        git(self.main, "worktree", "add", "-q", "-b", "task/x", str(self.worktree))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_mark(self, checkout: Path, cwd: Path, *extra: str) -> None:
        subprocess.run([sys.executable, str(checkout / "system" / "scripts" / "mark-scheduled-jobs-run.py"),
                        "ceo-review", *extra], cwd=cwd, check=True, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)

    def test_worktree_stamp_lands_on_main(self):
        self.run_mark(self.worktree, self.worktree)
        state = json.loads((self.main / "system" / "job-state.json").read_text())
        self.assertIn("ceo-review", state)
        self.assertFalse((self.worktree / "system" / "job-state.json").exists())

    def test_main_checkout_stamp_and_existing_keys_kept(self):
        (self.main / "system" / "job-state.json").write_text(json.dumps({"other": "2026-11-01"}))
        self.run_mark(self.main, self.tmp)
        state = json.loads((self.main / "system" / "job-state.json").read_text())
        self.assertEqual(state["other"], "2026-11-01")
        self.assertIn("ceo-review", state)

    def test_absolute_path_is_honored(self):
        target = self.tmp / "elsewhere" / "job-state.json"
        self.run_mark(self.worktree, self.worktree, "--job-state-path", str(target))
        self.assertIn("ceo-review", json.loads(target.read_text()))
        self.assertFalse((self.main / "system" / "job-state.json").exists())


if __name__ == "__main__":
    unittest.main()
