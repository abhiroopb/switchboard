#!/usr/bin/env python3
"""Shared-state links without Developer Mode (2026-11-07, rt-00000000002f).

Every Windows worker launch logged one "made as a hard link; it goes stale ... turn on
Windows Developer Mode" line per state file, and hard-linked writers' temp files
(running-tasks.json.<pid>.tmp) into the new worktree and info/exclude. The staleness
itself is handled by winlink.relink_shared / heal_shared_links (test_winlink_relink.py).

Wrong impls this catches: link a *.tmp into the worktree or record it in info/exclude;
keep an earlier launch's tmp line in info/exclude; stop linking lock files (their one
inode is what makes main's and a worktree's flock one lock); log one line per file; let
the ledger writer leave its tmp behind when the replace fails; print the Developer Mode
hint when it is on or off Windows.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch as td  # noqa: E402  (HermeticRepo: a real git repo + worktree)
from lib import winlink  # noqa: E402

dispatch = td.dispatch
ledger = td.ledger


class TransientNameTests(unittest.TestCase):
    def test_writer_temp_files_are_transient_and_state_and_locks_are_not(self) -> None:
        for name in ("running-tasks.json.1110116.tmp", "now.json.tmp", ".events.jsonl.trim.tmp",
                     ".router.yaml.tmp-4242", "todo.json.link-tmp", "queue.json.relink-77", "x.TEMP"):
            self.assertTrue(winlink.is_transient(name), name)
        for name in ("running-tasks.json", "now.json", "queue.json", "running-tasks.json.lock", ".events.lock",
                     ".rebuild.lock", "todo.json.lock", "tmp", "tmpfiles", "events.jsonl", "logs"):
            self.assertFalse(winlink.is_transient(name), name)

    def test_heal_ignores_tmp_lines_in_info_exclude(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git" / "info").mkdir(parents=True)
            (root / ".git" / "info" / "exclude").write_text(
                "/system/state/now.json\n/system/state/running-tasks.json.1110116.tmp\n", encoding="utf-8")
            self.assertEqual(winlink._shared_rels(root), {"system/state/now.json"})


class DeveloperModeHintTests(unittest.TestCase):
    def test_hint_only_when_windows_reports_developer_mode_off(self) -> None:
        with patch.object(winlink, "developer_mode", return_value=False):
            hint = winlink.developer_mode_hint()
        self.assertIn("Developer Mode is off", hint)
        self.assertIn(winlink.DEVELOPER_MODE_ON, hint)
        self.assertEqual(hint.count("\n"), 0, "one line")
        for state in (True, None):
            with patch.object(winlink, "developer_mode", return_value=state):
                self.assertEqual(winlink.developer_mode_hint(), "")

    def test_developer_mode_is_none_off_windows(self) -> None:
        with patch.object(winlink, "is_windows", return_value=False):
            self.assertIsNone(winlink.developer_mode())

    def test_install_all_prints_the_hint_on_windows(self) -> None:
        script = (Path(__file__).resolve().parent / "install-all.sh").read_text(encoding="utf-8")
        self.assertIn("system/scripts/lib/winlink.py developer-mode-hint", script)


class LaunchLinkHygieneTests(td.HermeticRepo):
    """Windows without symlink privilege: files become hard links (no junction needed here)."""

    def setUp(self) -> None:
        super().setUp()
        state = self.repo / "system" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (self.repo / ".gitignore").write_text(".amp/*\n/system/state/*\n")
        for name, text in (("running-tasks.json", "{}\n"), ("now.json", "{}\n"), ("queue.json", "{}\n"),
                           ("running-tasks.json.lock", ""), (".events.lock", ""),
                           ("running-tasks.json.1110116.tmp", "{}\n"), ("now.json.tmp", "{}\n")):
            (state / name).write_text(text)
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"{dispatch.EXCLUDE_HEADER}\n/system/state/running-tasks.json.999.tmp\n")
        privilege = OSError(1314, "A required privilege is not held by the client")
        privilege.winerror = 1314   # type: ignore[attr-defined]
        for p in (patch.object(dispatch.winlink, "is_windows", return_value=True),
                  patch.object(dispatch.winlink.os, "symlink", side_effect=privilege)):
            p.start()
            self.addCleanup(p.stop)
        self.logged: list[str] = []
        log_patch = patch.object(dispatch, "log", side_effect=lambda text, *a, **k: self.logged.append(str(text)))
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_tmp_files_are_never_linked_and_state_and_locks_are(self) -> None:
        path, _ = dispatch.create_worktree("rt-hyg")
        state, main_state = path / "system" / "state", self.repo / "system" / "state"
        for name in ("running-tasks.json", "now.json", "queue.json", "running-tasks.json.lock", ".events.lock"):
            self.assertTrue(winlink.same_file(state / name, main_state / name), f"{name} is shared")
        for name in ("running-tasks.json.1110116.tmp", "now.json.tmp"):
            self.assertFalse(os.path.lexists(state / name), f"{name} must not be linked")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertIn("/system/state/now.json", exclude)
        self.assertIn("/system/state/running-tasks.json.lock", exclude)
        self.assertFalse([line for line in exclude if line.endswith(".tmp")], exclude)

    def test_one_log_line_per_launch_and_no_developer_mode_nag(self) -> None:
        dispatch.create_worktree("rt-log")
        linked = [line for line in self.logged if "shared-state link" in line]
        self.assertEqual(len(linked), 1, self.logged)
        self.assertIn("hardlink", linked[0])
        self.assertNotIn("goes stale", " ".join(self.logged))
        self.assertNotIn("turn on Windows Developer Mode", " ".join(self.logged))


class LedgerTempCleanupTests(td.HermeticRepo):
    def test_failed_replace_leaves_no_tmp_behind(self) -> None:
        state = ledger.shared_state_file(ledger.STATE_FILE).parent
        with patch.object(ledger, "_os_replace", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(PermissionError):
                ledger.write_payload({"tasks": []})
        self.assertEqual(sorted(p.name for p in state.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
