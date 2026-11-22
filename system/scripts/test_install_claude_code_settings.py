#!/usr/bin/env python3
"""install-claude-code.sh step 5: the bypass-permissions merge (Mac kit v8 port, 2026-11-13).

Bypass stays the default here; macOS's opt-in flag is not ported.
The port is the guard: a settings file that is not a JSON object, or a non-object
`permissions`, gets a skip note instead of a crash.

Run: python3 system/scripts/test_install_claude_code_settings.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "install-claude-code.sh"


def step5_python() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    section = text[text.index('step "settings'):]
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", section, re.S)
    assert match, "step 5 heredoc not found"
    return match.group(1)


class Step5Settings(unittest.TestCase):
    def run_step(self, content: str | None) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            if content is not None:
                settings.write_text(content, encoding="utf-8")
            out = subprocess.run([sys.executable, "-", str(settings)], input=step5_python(), text=True,
                                 capture_output=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout, settings.read_text(encoding="utf-8") if settings.exists() else ""

    def test_bypass_is_still_the_default(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--bypass-permissions", text)
        _, written = self.run_step(None)
        data = json.loads(written)
        self.assertEqual(data["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIs(data["skipDangerousModePermissionPrompt"], True)

    def test_a_file_that_is_not_an_object_is_skipped_and_left_as_is(self) -> None:
        for content in ("[1, 2]", "not json", '"text"'):
            said, written = self.run_step(content)
            self.assertIn("is not a JSON object", said, content)
            self.assertEqual(written, content)

    def test_a_non_object_permissions_value_is_skipped_and_left_as_is(self) -> None:
        said, written = self.run_step('{"permissions": "x", "keep": 1}')
        self.assertIn("permissions value that is not an object", said)
        self.assertEqual(written, '{"permissions": "x", "keep": 1}')


if __name__ == "__main__":
    unittest.main()
