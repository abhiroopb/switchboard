#!/usr/bin/env python3
"""Tests for downloads_handoff.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import downloads_handoff as dh  # noqa: E402


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.dl = base / "Downloads"
        self.repo.mkdir()
        self.dl.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "notes").mkdir()
        self.note = self.repo / "notes" / "plan.md"
        self.note.write_text("# Plan\n\nfull text\n", encoding="utf-8")
        self.cwd = os.getcwd()
        os.chdir(self.repo)
        self.env = mock.patch.dict(os.environ, {"PM_OS_DOWNLOADS_DIR": str(self.dl)})
        self.env.start()
        self.top = dh.repo_root()

    def tearDown(self):
        self.env.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_put_copies_and_records(self):
        row = dh.put("notes/plan.md", None, False, self.top)
        self.assertEqual(row["repo_path"], "notes/plan.md")
        self.assertEqual((self.dl / "plan.md").read_text(encoding="utf-8"), "# Plan\n\nfull text\n")
        self.assertEqual(dh.check(False, self.top)[0]["status"], "ok")

    def test_put_refuses_path_outside_repo(self):
        outside = Path(self.tmp.name) / "loose.md"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            dh.put(str(outside), None, False, self.top)

    def test_put_refuses_foreign_downloads_file(self):
        (self.dl / "plan.md").write_text("owner's own file", encoding="utf-8")
        with self.assertRaises(SystemExit):
            dh.put("notes/plan.md", None, False, self.top)
        self.assertEqual((self.dl / "plan.md").read_text(encoding="utf-8"), "owner's own file")
        dh.put("notes/plan.md", None, True, self.top)
        self.assertIn("full text", (self.dl / "plan.md").read_text(encoding="utf-8"))

    def test_put_updates_its_own_copy(self):
        dh.put("notes/plan.md", None, False, self.top)
        self.note.write_text("# Plan\n\nnew section\n", encoding="utf-8")
        dh.put("notes/plan.md", None, False, self.top)
        self.assertIn("new section", (self.dl / "plan.md").read_text(encoding="utf-8"))

    def test_check_reports_missing_and_restores_without_touching_others(self):
        dh.put("notes/plan.md", None, False, self.top)
        other = self.dl / "keep-me.pdf"
        other.write_bytes(b"pdf")
        (self.dl / "plan.md").unlink()  # another device removed it
        self.assertEqual(dh.check(False, self.top)[0]["status"], "missing")
        self.assertEqual(dh.check(True, self.top)[0]["status"], "restored")
        self.assertTrue((self.dl / "plan.md").is_file())
        self.assertEqual(other.read_bytes(), b"pdf")
        self.assertEqual(dh.check(False, self.top)[0]["status"], "ok")

    def test_check_reports_changed_and_never_overwrites(self):
        dh.put("notes/plan.md", None, False, self.top)
        (self.dl / "plan.md").write_text("owner edited", encoding="utf-8")
        self.assertEqual(dh.check(True, self.top)[0]["status"], "changed")
        self.assertEqual((self.dl / "plan.md").read_text(encoding="utf-8"), "owner edited")

    def test_name_cannot_escape_downloads(self):
        for bad in ("../escaped.md", str(Path(self.tmp.name) / "abs.md"), "..", "sub/x.md"):
            with self.assertRaises(SystemExit):
                dh.put("notes/plan.md", bad, False, self.top)
        self.assertFalse((Path(self.tmp.name) / "escaped.md").exists())

    def test_repo_edit_after_put_is_stale(self):
        dh.put("notes/plan.md", None, False, self.top)
        self.note.write_text("# Plan\n\nnewer\n", encoding="utf-8")
        self.assertEqual(dh.check(False, self.top)[0]["status"], "stale")

    def test_never_restores_text_other_than_the_last_put(self):
        dh.put("notes/plan.md", None, False, self.top)
        self.note.write_text("# Plan\n\nedited after put\n", encoding="utf-8")
        (self.dl / "plan.md").unlink()
        self.assertEqual(dh.check(True, self.top)[0]["status"], "source-changed")
        self.assertFalse((self.dl / "plan.md").exists())

    def test_worktree_put_then_main_check_restores_worktree_text(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "notes/plan.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "old"], check=True)
        wt = Path(self.tmp.name) / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "task", str(wt)], check=True)
        (wt / "notes" / "plan.md").write_text("# Plan\n\nNEW from worktree\n", encoding="utf-8")
        os.chdir(wt)
        wtop = dh.repo_root()
        dh.put("notes/plan.md", None, False, wtop)
        self.assertTrue((self.repo / ".state" / "downloads-handoffs.json").is_file())  # on main
        self.assertFalse((wt / ".state").exists())
        (self.dl / "plan.md").unlink()
        os.chdir(self.repo)
        self.assertEqual(dh.check(True, self.top)[0]["status"], "restored")
        self.assertIn("NEW from worktree", (self.dl / "plan.md").read_text(encoding="utf-8"))

    def test_retire_drops_the_record_and_leaves_the_file(self):
        dh.put("notes/plan.md", None, False, self.top)
        with self.assertRaises(SystemExit):   # the replacement must be on record first
            dh.retire("plan.md", "plan-v2.md", self.top)
        (self.repo / "notes" / "plan-v2.md").write_text("v2", encoding="utf-8")
        dh.put("notes/plan-v2.md", None, False, self.top)
        out = dh.retire("plan.md", "plan-v2.md", self.top)
        self.assertEqual(out["retired"], "plan.md")
        self.assertTrue((self.dl / "plan.md").exists())
        (self.dl / "plan.md").unlink()
        rows = dh.check(True, self.top)
        self.assertEqual([r["name"] for r in rows], ["plan-v2.md"])
        self.assertFalse((self.dl / "plan.md").exists())
        with self.assertRaises(SystemExit):
            dh.retire("plan.md", None, self.top)

    def test_main_exit_code(self):
        dh.put("notes/plan.md", None, False, self.top)
        (self.dl / "plan.md").unlink()
        with mock.patch("sys.stdout"):
            self.assertEqual(dh.main(["check"]), 1)
            self.assertEqual(dh.main(["check", "--restore"]), 0)


if __name__ == "__main__":
    unittest.main()
