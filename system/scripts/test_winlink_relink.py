#!/usr/bin/env python3
"""winlink.relink_shared / heal_shared_links (2026-11-07, rt-000000000002): on Windows
without Developer Mode a worktree's shared-state file is a hard link, and an atomic
rewrite on main left it on the old inode (a worker read a stale running-tasks.json and
now.json). Wrong impls: relink a path dispatch never shared (a worktree's own file);
copy instead of link; leave a link stale after the writer's replace."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import winlink  # noqa: E402


@unittest.skipUnless(os.name == "nt", "hard-link fallback exists only on Windows")
class RelinkSharedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.main = root / "main"
        (self.main / ".git" / "info").mkdir(parents=True)
        (self.main / ".git" / "info" / "exclude").write_text("# shared\n/system/state/now.json\n", encoding="utf-8")
        self.wt = root / "wt"
        (self.main / ".git" / "worktrees" / "rt-1").mkdir(parents=True)
        (self.main / ".git" / "worktrees" / "rt-1" / "gitdir").write_text(str(self.wt / ".git") + "\n", encoding="utf-8")
        for base in (self.main, self.wt):
            (base / "system" / "state").mkdir(parents=True)
        self.now = self.main / "system" / "state" / "now.json"
        self.now.write_text('{"v": 1}\n', encoding="utf-8")
        os.link(self.now, self.wt / "system" / "state" / "now.json")
        own = self.main / "system" / "state" / "own.json"
        own.write_text("main\n", encoding="utf-8")
        (self.wt / "system" / "state" / "own.json").write_text("worktree\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rewrite_atomically(self, path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def test_replace_leaves_the_link_stale_and_relink_fixes_it(self) -> None:
        link = self.wt / "system" / "state" / "now.json"
        self.rewrite_atomically(self.now, '{"v": 2}\n')
        self.assertIn('"v": 1', link.read_text(encoding="utf-8"), "the bug: the hard link kept the old inode")
        self.assertEqual(winlink.relink_shared(self.now), 1)
        self.assertTrue(os.path.samefile(link, self.now))
        self.assertIn('"v": 2', link.read_text(encoding="utf-8"))
        self.assertEqual(winlink.relink_shared(self.now), 0, "a current link is left alone")

    def test_unshared_paths_are_never_touched(self) -> None:
        self.assertEqual(winlink.relink_shared(self.main / "system" / "state" / "own.json"), 0)
        self.assertEqual((self.wt / "system" / "state" / "own.json").read_text(encoding="utf-8"), "worktree\n")

    def test_heal_refreshes_every_shared_file(self) -> None:
        self.rewrite_atomically(self.now, '{"v": 3}\n')
        self.assertEqual(winlink.heal_shared_links(self.main), 1)
        self.assertEqual(winlink.heal_shared_links(self.main), 0)


if __name__ == "__main__":
    unittest.main()
