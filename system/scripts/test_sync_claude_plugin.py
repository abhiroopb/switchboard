#!/usr/bin/env python3
"""sync-claude-plugin.py keeps the installed switchboard plugin in step with the repo.

An installed copy that never updates keeps hook fixes from reaching live sessions. Wrong impls: copy nothing because the version did not change, undo the
Windows hook profile by restoring the heartbeat, delete files only the install has, or
overwrite a hand fix made after the repo copy.

Run: python3 system/scripts/test_sync_claude_plugin.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("sync_claude_plugin", HERE / "sync-claude-plugin.py")
sync_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_mod)  # type: ignore[union-attr]

HOOKS = {"description": "x", "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/cmux-heartbeat.py\" pre-tool-use"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 surface-context.py", "timeout": 30}]}],
}}


class SyncClaudePlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        src = self.repo / sync_mod.PLUGIN_REL
        (src / "hooks").mkdir(parents=True)
        (src / ".claude-plugin").mkdir()
        (src / ".claude-plugin" / "plugin.json").write_text('{"name": "switchboard", "version": "0.1.0"}\n')
        (src / "hooks" / "hooks.json").write_text(json.dumps(HOOKS, indent=2))
        (src / "hooks" / "surface-context.py").write_text("print('new')\n")
        (src / "hooks" / "__pycache__").mkdir()
        (src / "hooks" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        (self.repo / "system" / "windows").mkdir(parents=True)
        shutil.copy(REPO / "system" / "windows" / "apply-hook-profile.py", self.repo / "system" / "windows")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.base = self.tmp / "claude"
        self.installed = self.base / "plugins" / "cache" / "switchboard-local" / "switchboard" / "0.1.0"
        (self.installed / "hooks").mkdir(parents=True)
        (self.installed / "hooks" / "surface-context.py").write_text("print('old')\n")
        (self.installed / "hooks" / "only-installed.py").write_text("x\n")
        (self.base / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {sync_mod.PLUGIN: [{"installPath": str(self.installed)}]}}))
        (self.base / "settings.json").write_text(json.dumps({"hooks": {}}))
        # Installed copies predate the repo files, as after a real install.
        old = time.time() - 3600
        for p in self.installed.rglob("*"):
            os.utime(p, (old, old))

    def run_sync(self, check: bool = False) -> dict:
        return sync_mod.sync(self.repo, self.base, check=check)

    def test_check_reports_behind_and_writes_nothing(self) -> None:
        out = self.run_sync(check=True)
        self.assertEqual(out["status"], "behind")
        self.assertIn("hooks/surface-context.py", out["changed"])
        self.assertEqual((self.installed / "hooks" / "surface-context.py").read_text(), "print('old')\n")

    def test_sync_copies_changes_skips_pycache_and_keeps_extras(self) -> None:
        out = self.run_sync()
        self.assertEqual((self.installed / "hooks" / "surface-context.py").read_text(), "print('new')\n")
        self.assertTrue((self.installed / ".claude-plugin" / "plugin.json").is_file())
        self.assertFalse((self.installed / "hooks" / "__pycache__").exists())
        self.assertTrue((self.installed / "hooks" / "only-installed.py").is_file())
        self.assertIn("hooks/only-installed.py", out["extra"])
        self.assertEqual(self.run_sync(check=True)["status"], "current")

    def test_hook_profile_keeps_the_heartbeat_out(self) -> None:
        (self.base / "settings.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"command": "node wmux-hook.js --event Stop"}]}]}}))
        os.environ["SWITCHBOARD_HOOK_PROFILE_ANY_OS"] = "1"
        self.addCleanup(os.environ.pop, "SWITCHBOARD_HOOK_PROFILE_ANY_OS", None)
        self.assertTrue(self.run_sync()["heartbeat_stripped"])
        installed = json.loads((self.installed / "hooks" / "hooks.json").read_text())
        self.assertNotIn("PreToolUse", installed["hooks"])
        self.assertEqual(installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"], 30)
        self.assertEqual(self.run_sync(check=True)["status"], "current")

    def test_without_the_profile_the_heartbeat_stays(self) -> None:
        self.run_sync()
        installed = json.loads((self.installed / "hooks" / "hooks.json").read_text())
        self.assertIn("PreToolUse", installed["hooks"])

    def test_a_hand_fix_newer_than_the_repo_is_held(self) -> None:
        fix = self.installed / "hooks" / "surface-context.py"
        fix.write_text("print('hand fix')\n")
        future = time.time() + 60
        os.utime(fix, (future, future))
        out = self.run_sync()
        self.assertIn("hooks/surface-context.py", out["held"])
        self.assertEqual(fix.read_text(), "print('hand fix')\n")

    def test_not_installed_is_quiet(self) -> None:
        (self.base / "plugins" / "installed_plugins.json").write_text('{"plugins": {}}')
        self.assertEqual(self.run_sync()["status"], "not-installed")


if __name__ == "__main__":
    unittest.main()
