#!/usr/bin/env python3
"""register-obsidian-vault.py adds one switchboard vault and keeps every other vault."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "register-obsidian-vault.py"


def main_checkout() -> Path:
    common = subprocess.check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                     text=True, cwd=SCRIPT.parent).strip()
    return Path(common).parent.resolve()


class RegisterVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "obsidian" / "obsidian.json"
        self.cfg.parent.mkdir(parents=True)
        self.other = {"abc123": {"path": str(Path(self.tmp.name) / "Other"), "ts": 1, "open": True}}
        self.cfg.write_text(json.dumps({"vaults": dict(self.other), "updateDisabled": True}), encoding="utf-8")
        self.env = dict(os.environ, SWITCHBOARD_OBSIDIAN_CONFIG=str(self.cfg), SWITCHBOARD_OBSIDIAN_ASSUME_CLOSED="1")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=self.env, cwd=SCRIPT.parent)
        return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])

    def test_check_reports_missing(self):
        code, out = self.run_script("--check")
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "missing")

    def test_dry_run_changes_nothing(self):
        before = self.cfg.read_text(encoding="utf-8")
        code, out = self.run_script("--dry-run")
        self.assertEqual((code, out["status"]), (0, "would-register"))
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), before)

    def test_register_keeps_other_vaults_and_is_idempotent(self):
        code, out = self.run_script()
        self.assertEqual((code, out["status"]), (0, "registered"))
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["updateDisabled"], True)
        self.assertEqual(data["vaults"]["abc123"], self.other["abc123"])
        added = [v for k, v in data["vaults"].items() if k != "abc123"]
        self.assertEqual(len(added), 1)
        self.assertEqual(Path(added[0]["path"]).resolve(), main_checkout())
        self.assertTrue(Path(out["backup"]).exists())
        code, out = self.run_script()
        self.assertEqual((code, out["status"]), (0, "present"))
        self.assertEqual(len(json.loads(self.cfg.read_text(encoding="utf-8"))["vaults"]), 2)
        code, out = self.run_script("--check")
        self.assertEqual(code, 0)

    def test_missing_config_is_created(self):
        self.cfg.unlink()
        code, out = self.run_script()
        self.assertEqual((code, out["status"]), (0, "registered"))
        self.assertEqual(len(json.loads(self.cfg.read_text(encoding="utf-8"))["vaults"]), 1)


if __name__ == "__main__":
    unittest.main()
