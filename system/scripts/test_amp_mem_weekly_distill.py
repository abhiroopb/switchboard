#!/usr/bin/env python3
"""The weekly amp-mem distill: backend gate, loud failures, and the legacy distillations table.

rt-000000000004 (2026-11-12): on Windows the Sunday tick logged only its header
and exited 1, because `amp-mem distill-status` failed on a Gemini-era distillations table
(run_at, not created_at) and set -e ended the script before it logged anything.

Every case copies the daemon into a throwaway tree with a stub amp-mem, so the real
.state/ log, local/ folder and memory database never decide a result. No case calls amp.

  python3 system/scripts/test_amp_mem_weekly_distill.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
DAEMON = REPO / "daemon" / "amp-mem-weekly-distill.sh"
AMP_MEM = REPO / "system" / "bin" / "amp-mem"


def find_bash() -> str | None:
    """Git Bash or a real Unix bash; WSL's System32 launcher cannot read these Windows paths."""
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
SQLITE = shutil.which("sqlite3")


def posix(path: Path) -> str:
    """A path Git Bash accepts (forward slashes work for drive paths too)."""
    return str(path).replace("\\", "/")


@unittest.skipUnless(BASH, "bash not found")
class DaemonGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amp-mem-distill-"))
        (self.tmp / "daemon").mkdir()
        shutil.copy(DAEMON, self.tmp / "daemon" / DAEMON.name)
        (self.tmp / "local" / "machines").mkdir(parents=True)
        self.stub = self.tmp / "amp-mem-stub"
        self.log = self.tmp / ".state" / "amp-mem-weekly-distill.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_amp_mem(self, body: str) -> None:
        self.stub.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)

    def run_daemon(self, *args: str, memory: str | None = None, machine_file: str | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("PM_OS_MEMORY", "PM_OS_LOCAL_DIR")}
        env["AMP_MEM_BIN"] = posix(self.stub)
        env["PM_OS_MACHINE"] = "TESTBOX"
        if memory is not None:
            env["PM_OS_MEMORY"] = memory
        if machine_file is not None:
            (self.tmp / "local" / "machines" / "TESTBOX.env").write_text(machine_file, encoding="utf-8")
        return subprocess.run(
            [BASH, posix(self.tmp / "daemon" / DAEMON.name), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_builtin_backend_in_machine_file_skips(self) -> None:
        self.stub_amp_mem('echo "should not run" >&2; exit 9')
        proc = self.run_daemon("--dry-run", machine_file="PM_OS_COMMS_TRIAGE=off\nPM_OS_MEMORY=builtin\r\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipped, memory backend is builtin", proc.stdout)
        self.assertIn("Skipped: memory backend on this machine is 'builtin'", self.log_text())

    def test_environment_wins_over_machine_file(self) -> None:
        self.stub_amp_mem('echo "  Pending observations: 3"')
        proc = self.run_daemon(memory="amp-mem", machine_file="PM_OS_MEMORY=builtin\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Pending observations: 3 (threshold: 30)", self.log_text())

    def test_distill_status_failure_is_logged_and_exits_1(self) -> None:
        self.stub_amp_mem('echo "Error: in prepare, no such column: created_at" >&2; exit 1')
        proc = self.run_daemon("--dry-run")
        self.assertEqual(proc.returncode, 1)
        log = self.log_text()
        self.assertIn("ERROR: amp-mem distill-status failed (exit 1)", log)
        self.assertIn("no such column: created_at", log)
        self.assertIn("distill-status failed", proc.stderr)

    def test_unreadable_status_output_exits_1(self) -> None:
        self.stub_amp_mem('echo "something else"')
        proc = self.run_daemon("--dry-run")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not read 'Pending observations'", self.log_text())

    def test_below_threshold_exits_clean(self) -> None:
        self.stub_amp_mem('echo "  Pending observations: 5"')
        proc = self.run_daemon("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Below threshold; nothing to distill. Done.", self.log_text())


LEGACY_SCHEMA = """
CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
    topic TEXT NOT NULL, summary TEXT NOT NULL, private INTEGER NOT NULL DEFAULT 0, superseded_by INTEGER);
CREATE TABLE distillations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    observation_count INTEGER NOT NULL,
    distilled_notes TEXT,
    proposed_rules TEXT
);
INSERT INTO observations (ts, type, topic, summary) VALUES
    ('2026-03-02T00:00:00Z', 'observation', 'a', 'old'),
    ('2026-06-21T00:00:00Z', 'observation', 'b', 'new');
INSERT INTO distillations (run_at, observation_count, distilled_notes, proposed_rules)
    VALUES ('2026-04-01T00:00:00Z', 1, 'note text', 'rule text');
"""


@unittest.skipUnless(BASH and SQLITE, "bash or sqlite3 not found")
class LegacyDistillationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amp-mem-legacy-"))
        self.db = self.tmp / "amp-mem.db"
        con = sqlite3.connect(self.db)
        con.executescript(LEGACY_SCHEMA)
        con.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def amp_mem(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, AMP_MEM_DB=posix(self.db), AMP_MEM_SCHEMA=posix(self.tmp / "schema.sql"))
        return subprocess.run([BASH, posix(AMP_MEM), *args], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def test_status_migrates_legacy_table_and_keeps_rows(self) -> None:
        proc = self.amp_mem("distill-status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Last run:            2026-04-01T00:00:00Z", proc.stdout)
        self.assertIn("Pending observations: 1", proc.stdout)
        self.assertIn("Total distillations:  1", proc.stdout)
        con = sqlite3.connect(self.db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(distillations)")]
        self.assertIn("created_at", cols)
        self.assertNotIn("run_at", cols)
        summary = con.execute("SELECT summary FROM distillations").fetchone()[0]
        self.assertEqual(summary, "note text\nRules: rule text")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM distillations_legacy").fetchone()[0], 1)
        con.close()

    def test_backup_writes_a_snapshot_and_exits_0(self) -> None:
        # Git Bash: sqlite3.exe needs a Windows path inside .backup, and GNU stat -f
        # succeeded with filesystem text, which broke the size line.
        proc = self.amp_mem("backup", "--tag", "test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snaps = list(self.tmp.glob("amp-mem.db.backup-*-test"))
        self.assertEqual(len(snaps), 1, proc.stdout)
        con = sqlite3.connect(snaps[0])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        con.close()

    def test_second_run_is_a_no_op(self) -> None:
        self.assertEqual(self.amp_mem("distill-status").returncode, 0)
        proc = self.amp_mem("distill-status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Migrated", proc.stderr)
        self.assertIn("Total distillations:  1", proc.stdout)


if __name__ == "__main__":
    unittest.main()
