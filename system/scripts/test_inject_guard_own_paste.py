#!/usr/bin/env python3
"""Tests for the own-paste ledger in lib/cmux-inject-guard.sh and the payload
spill in lib/automation-dispatch.sh. Each test sources the library in bash
with a scratch ledger folder and never reaches cmux.

Wrong impls these tests catch: a human draft treated as the guard's own paste,
a non-automation paste recovered (CoS and workstream tabs must never be
touched), text typed after the paste still matching, a stale ledger entry
matching, a folded "[Pasted text #N]" draft missed, the ledger not cleared,
a payload spilled when state-pruned.txt has no row for it, cig_safe_send
deferring forever behind its own stranded paste, the same text typed twice
after a recovery, a human draft submitted by the recovery, the owner's own long
paste after a short guard paste taken for ours, a second run of the same job
with other content taken for this send, an extra screen read on an ordinary
send, and "outcome unknown" for a new text that was never typed.

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

    def test_owner_paste_after_short_own_paste_fails(self):
        draft = flat(ENVELOPE) + "[Pastedtext#1+40lines]"
        self.assertNotEqual(self.own(ENVELOPE, draft), 0)

    def test_old_ledger_without_length_still_reads(self):
        proc = self.bash('mkdir -p "$CIG_PASTE_DIR"; printf "%s\\n%s\\n%s\\n" "$(date +%s)" "${F:0:40}" "${F: -24}" '
                         '> "$CIG_PASTE_DIR/workspace_3.paste"; cig_own_paste workspace:3 "$F"', {"F": flat(ENVELOPE)})
        self.assertEqual(proc.returncode, 0, proc.stderr)

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


FAKE_CMUX = r"""#!/usr/bin/env bash
# A Claude tab: one composer whose draft lives in $FAKE_DIR/draft. Logs every send.
draft="$FAKE_DIR/draft"
case "$1" in
  tree) echo "workspace:3 surface:1" ;;
  read-screen)
    echo read >> "$FAKE_DIR/reads"
    printf 'Claude Code\n\n%s\n\xe2\x9d\xaf %s\n%s\n  ? for shortcuts\n' \
      "$(printf '\xe2\x94\x80%.0s' $(seq 1 40))" "$(cat "$draft" 2>/dev/null)" "$(printf '\xe2\x94\x80%.0s' $(seq 1 40))" ;;
  send)
    text="${@: -1}"
    printf 'send %s\n' "$text" >> "$FAKE_DIR/log"
    if [[ "$text" == '\r' ]]; then
      [[ -f "$FAKE_DIR/stuck" ]] || : > "$draft"
    else
      printf '%s%s' "$(cat "$draft" 2>/dev/null)" "$text" > "$draft"
    fi ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "bash not on PATH")
class Recovery(unittest.TestCase):
    """cig_safe_send with the guard's own automation paste stranded in the composer."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cig-recover-test-"))
        self.cmux = self.dir / "cmux"
        self.cmux.write_text(FAKE_CMUX, encoding="utf-8", newline="\n")
        self.cmux.chmod(0o755)
        (self.dir / "pastes").mkdir()

    def bash(self, script: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "CIG_PASTE_DIR": str(self.dir / "pastes"), "CMUX": self.cmux.as_posix(), "CMUX_BIN": self.cmux.as_posix(),
               "FAKE_DIR": self.dir.as_posix(), "CIG_SEND_LOG": (self.dir / "sends.log").as_posix(),
               "CIG_CLAUDE_SETTLE": "0", **(extra or {})}
        return subprocess.run([BASH, "-c", f'source "$1"; {script}', "_", GUARD.as_posix()],
                              capture_output=True, text=True, env=env, timeout=120)

    def strand(self, text: str) -> None:
        """Our paste typed and noted, CR never sent (the caller's timeout fired)."""
        self.bash('cig_note_paste workspace:3 "$T"', {"T": text})
        (self.dir / "draft").write_text(text, encoding="utf-8")

    def sends(self) -> list[str]:
        log = self.dir / "log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_state_reads_back(self):
        self.assertEqual(self.bash('cig_diagnose workspace:3').stdout.strip(), "empty")
        self.strand(ENVELOPE)
        self.assertEqual(self.bash('cig_diagnose workspace:3').stdout.strip(), "own-paste")
        (self.dir / "draft").write_text("owner is typing a note", encoding="utf-8")
        self.assertEqual(self.bash('cig_diagnose workspace:3').stdout.strip(), "draft")

    def test_same_text_is_submitted_once_not_retyped(self):
        self.strand(ENVELOPE)
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": ENVELOPE})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sends(), [r"send \r"])
        self.assertEqual(list((self.dir / "pastes").glob("*.paste")), [])
        self.assertIn("recover-own-paste", (self.dir / "sends.log").read_text(encoding="utf-8"))

    def test_older_job_is_submitted_then_new_text_goes_in(self):
        self.strand(ENVELOPE)
        newer = "[AUTOMATION job=comms-example-0002 dest=cos] Background automation job. | second payload"
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": newer})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = self.sends()
        self.assertEqual(sent[0], r"send \r")
        self.assertIn(f"send {newer}", sent)
        self.assertEqual(sent[-1], r"send \r")

    def test_no_ledger_entry_reads_no_extra_screen(self):
        proc = self.bash('cig_recover_own_paste workspace:3')
        self.assertEqual(proc.returncode, 1)
        self.assertFalse((self.dir / "reads").exists())

    def test_same_job_other_content_is_not_this_send(self):
        self.strand(ENVELOPE)
        rerun = ENVELOPE.replace("payload body ends here", "a later payload with other content")
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": rerun})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"send {rerun}", self.sends())

    def test_stuck_older_paste_defers_new_text(self):
        self.strand(ENVELOPE)
        (self.dir / "stuck").write_text("", encoding="utf-8")
        newer = "[AUTOMATION job=comms-example-0002 dest=cos] Background automation job. | second payload"
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": newer})
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(f"send {newer}", self.sends())
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": ENVELOPE})
        self.assertEqual(proc.returncode, 3)   # the same text: outcome unknown, never retried

    def test_human_draft_is_never_submitted(self):
        self.bash('cig_note_paste workspace:3 "$T"', {"T": ENVELOPE})   # an old note of ours
        (self.dir / "draft").write_text("owner is typing a private note", encoding="utf-8")
        proc = self.bash('cig_safe_send workspace:3 "$T" 0', {"T": ENVELOPE})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.sends(), [])
        self.assertEqual((self.dir / "draft").read_text(encoding="utf-8"), "owner is typing a private note")


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
