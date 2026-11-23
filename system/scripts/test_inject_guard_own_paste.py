#!/usr/bin/env python3
"""Tests for the own-paste ledger in lib/cmux-inject-guard.sh and the payload
spill in lib/automation-dispatch.sh. Each test sources the library in bash
with a scratch ledger folder and never reaches cmux.

Wrong impls these tests catch: a human draft treated as the guard's own paste,
a non-automation paste recovered (CoS and workstream tabs must never be
touched), text typed after the paste still matching, a stale ledger entry
matching, a folded "[Pasted text #N]" draft missed, the ledger not cleared,
and a payload spilled when state-pruned.txt has no row for it.

Run: python3 system/scripts/test_inject_guard_own_paste.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

HERE = Path(__file__).resolve().parent
GUARD = HERE / "lib" / "cmux-inject-guard.sh"
DISPATCH = HERE / "lib" / "automation-dispatch.sh"
BASH = shutil.which("bash")

ENVELOPE = "[AUTOMATION job=comms-example-0001 dest=cos] Background automation job, run it here. | payload body ends here"


def flat(text: str) -> str:
    return "".join(text.split())


@unittest.skipUnless(BASH, "bash not on PATH")
class OwnPaste(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cig-paste-test-")

    def bash(self, script: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "CIG_PASTE_DIR": self.dir, "CIG_CMUX": "/nonexistent/cmux", **(extra_env or {})}
        return subprocess.run([BASH, "-c", f'source "$1"; {script}', "_", GUARD.as_posix()],
                              capture_output=True, text=True, env=env, timeout=60)

    def own(self, text: str, draft: str, extra_env: dict | None = None) -> int:
        script = 'cig_note_paste workspace:3 "$TEXT"; cig_own_paste workspace:3 "$DRAFT"'
        return self.bash(script, {"TEXT": text, "DRAFT": draft, **(extra_env or {})}).returncode

    def test_full_draft_matches(self):
        self.assertEqual(self.own(ENVELOPE, flat(ENVELOPE)), 0)

    def test_folded_paste_matches(self):
        draft = flat(ENVELOPE)[:50] + "[Pastedtext#1+12lines]"
        self.assertEqual(self.own(ENVELOPE, draft), 0)

    def test_text_typed_after_paste_fails(self):
        self.assertNotEqual(self.own(ENVELOPE, flat(ENVELOPE) + "andmore"), 0)

    def test_text_typed_before_paste_fails(self):
        self.assertNotEqual(self.own(ENVELOPE, "ownerwroteaprivatenotehere" + flat(ENVELOPE)), 0)

    def test_second_paste_after_fold_fails(self):
        draft = flat(ENVELOPE)[:50] + "[Pastedtext#1+12lines]ownertyped[Pastedtext#2+40lines]"
        self.assertNotEqual(self.own(ENVELOPE, draft), 0)

    def test_human_draft_fails(self):
        self.assertNotEqual(self.own(ENVELOPE, "pleasereviewthedraftwhenyougetachance"), 0)

    def test_non_automation_paste_fails(self):
        text = "[ROUTER REPORT] a worker finished and this is its report text"
        self.assertNotEqual(self.own(text, flat(text)), 0)

    def test_stale_entry_fails(self):
        self.assertNotEqual(self.own(ENVELOPE, flat(ENVELOPE), {"CIG_OWN_PASTE_MAX_AGE": "-1"}), 0)

    def test_clear_removes_entry(self):
        proc = self.bash('cig_note_paste workspace:3 "$TEXT"; cig_clear_paste workspace:3; '
                         'cig_own_paste workspace:3 "$DRAFT"', {"TEXT": ENVELOPE, "DRAFT": flat(ENVELOPE)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(Path(self.dir).glob("*.paste")), [])

    def test_ledger_file_per_target(self):
        self.bash('cig_note_paste workspace:3 "$TEXT"; cig_note_paste "work/space:4" "$TEXT"', {"TEXT": ENVELOPE})
        names = sorted(p.name for p in Path(self.dir).glob("*.paste"))
        self.assertEqual(names, ["work_space_4.paste", "workspace_3.paste"])


@unittest.skipUnless(BASH, "bash not on PATH")
class PayloadSpill(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ad-payload-test-"))
        self.pruned = self.tmp / "state-pruned.txt"
        self.out = self.tmp / "payloads"

    def spill(self, pruned_text: str | None, override_dir: bool) -> subprocess.CompletedProcess:
        if pruned_text is not None:
            self.pruned.write_text(pruned_text, encoding="utf-8")
        env = {**os.environ, "AD_STATE_PRUNED_FILE": str(self.pruned), "CIG_PASTE_DIR": str(self.tmp / "p")}
        env.pop("AD_PAYLOAD_DIR", None)
        if override_dir:
            env["AD_PAYLOAD_DIR"] = str(self.out)
        script = ('log() { :; }; source "$1"; source "$2"; ad_declared_payload_dir; echo "rc=$?"; '
                  'ad_write_payload_file "job/one" "hello payload"')
        return subprocess.run([BASH, "-c", script, "_", GUARD.as_posix(), DISPATCH.as_posix()],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_declared_row(self):
        proc = self.spill("automation-dispatch.sh    automation-payloads/\n", override_dir=True)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], "automation-payloads")
        written = Path(lines[-1])
        self.assertEqual(written.name, "job_one.md")
        self.assertEqual(written.read_text(encoding="utf-8").strip(), "hello payload")

    def test_no_row_is_reported(self):
        proc = self.spill("rotate-logs.sh    context-snapshots/\n", override_dir=True)
        self.assertIn("rc=1", proc.stdout)

    def test_two_rows_are_not_a_declaration(self):
        proc = self.spill("automation-dispatch.sh a/\nautomation-dispatch.sh b/\n", override_dir=True)
        self.assertIn("rc=1", proc.stdout)

    def test_shipped_state_pruned_declares_the_dir(self):
        shipped = HERE.parent / "config" / "state-pruned.txt"
        rows = [line.split() for line in shipped.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertEqual([r[1] for r in rows if r[0] == "automation-dispatch.sh"], ["automation-payloads/"])


if __name__ == "__main__":
    unittest.main()
