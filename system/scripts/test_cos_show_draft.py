#!/usr/bin/env python3
"""cos-show-draft.sh routes a draft handle to the right show command (2026-11-12, rt-00000000003b).

"send draft r-…" in a Windows CoS hit `triage_ledger.py: can't open file`, because the
kit install ships no Comms Triage ledger. A Gmail draft id now goes straight to
`switchboard mail show`. A stub switchboard records the call; nothing reaches Google.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "cos-show-draft.sh"
BASH = shutil.which("bash")


@unittest.skipIf(BASH is None, "bash not on PATH")
class CosShowDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cos-show-draft-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.switchboard = self.tmp / "switchboard"
        self.switchboard.write_text('#!/usr/bin/env bash\necho "switchboard $*"\n', encoding="utf-8", newline="\n")
        self.switchboard.chmod(0o755)
        # A bare repo with no triage_ledger.py, like the Windows kit install.
        subprocess.run(["git", "init", "-q", str(self.tmp / "repo")], check=True)

    def run_it(self, handle: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "SWITCHBOARD_CLI": str(self.switchboard).replace("\\", "/")}
        return subprocess.run([BASH, str(SCRIPT).replace("\\", "/"), handle], cwd=cwd or self.tmp / "repo", env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_gmail_draft_id_goes_to_switchboard_show_draft(self) -> None:
        for handle in ("r-1234567890123456789", "r1234567890123456789"):
            result = self.run_it(handle)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"switchboard mail show --draft-id {handle}")

    def test_ledger_handle_without_a_ledger_says_so(self) -> None:
        for handle in ("2", "draft-2"):
            result = self.run_it(handle)
            self.assertEqual(result.returncode, 3)
            self.assertIn("no Comms Triage ledger on this machine", result.stderr)
            self.assertNotIn("can't open file", result.stderr)

    def test_ledger_handle_uses_the_ledger_where_it_exists(self) -> None:
        repo = self.tmp / "repo"
        (repo / "system" / "scripts").mkdir(parents=True)
        (repo / "system" / "scripts" / "triage_ledger.py").write_text(
            "import sys\nprint('ledger', *sys.argv[1:])\n", encoding="utf-8")
        result = self.run_it("draft-7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ledger show --draft-id draft-7")

    def test_unknown_handle_is_refused(self) -> None:
        result = self.run_it("hello")
        self.assertEqual(result.returncode, 3)


if __name__ == "__main__":
    unittest.main()
