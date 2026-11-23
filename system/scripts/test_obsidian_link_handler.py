#!/usr/bin/env python3
"""switchboard Obsidian Link handler (system/native/obsidian-link/main.swift).

A background open can load the note behind cmux on a one-screen Mac, so
nothing shows. The handler opens in the background
only when Obsidian's window is at least half visible, and in front otherwise.
Wrong impls: always `open -g` (the bug); always front (steals focus when
Obsidian is already in view); a mode default other than auto; a switchboard-obsidian
URL that passes anything but vault and file through to obsidian://.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
SRC = REPO / "system" / "native" / "obsidian-link" / "main.swift"


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "macOS with swiftc only")
class HandlerChoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bin = Path(cls.tmp.name) / "switchboard-obsidian-link"
        subprocess.run(["swiftc", "-o", str(cls.bin), str(SRC)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def choice(self, visible: float, mode: str) -> str:
        out = subprocess.run([str(self.bin), "--decide-for", str(visible), mode],
                             capture_output=True, text=True, check=True, timeout=20)
        return out.stdout.strip()

    def test_auto_covered_window_comes_forward(self):
        self.assertEqual(self.choice(0.0, "auto"), "front")
        self.assertEqual(self.choice(0.49, "auto"), "front")

    def test_auto_visible_window_stays_background(self):
        self.assertEqual(self.choice(0.5, "auto"), "background")
        self.assertEqual(self.choice(1.0, "auto"), "background")

    def test_modes_override(self):
        self.assertEqual(self.choice(1.0, "front"), "front")
        self.assertEqual(self.choice(0.0, "background"), "background")

    def test_unknown_mode_acts_as_auto(self):
        self.assertEqual(self.choice(0.0, "bogus"), "front")

    def test_decide_measures_without_opening(self):
        out = subprocess.run([str(self.bin), "--decide"], capture_output=True, text=True,
                             check=True, timeout=20).stdout
        self.assertRegex(out, r"^visible=\d\.\d\d mode=\w+ open=(front|background) front_app=")


class HandlerSource(unittest.TestCase):
    def test_only_vault_and_file_pass_through(self):
        text = SRC.read_text()
        self.assertIn('URLQueryItem(name: "vault", value: vault), URLQueryItem(name: "file", value: file)', text)

    def test_background_branch_keeps_open_g_and_guard(self):
        text = SRC.read_text()
        self.assertIn('(front ? [] : ["-g"])', text)
        self.assertIn("guardFocus(before: before)", text)


if __name__ == "__main__":
    unittest.main()
