#!/usr/bin/env python3
"""Windows capacity guards.

A laptop can stall at its memory commit limit when a kernel driver never
frees exited processes (zombies). These tests pin the guards against
it: the Windows worker cap, the commit hold on new launches, the
capacity alert reasons, the in-process memory probe, and the reap leftover-
process kill staying inert under the test sandbox.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
(TMP / "state" / "worker-dispatch").mkdir(parents=True)
os.environ.update({
    "PM_OS_TEST_SANDBOX": "1",
    "PM_OS_STATE_DIR": str(TMP / "state"),
    "RUNNING_TASKS_FILE": str(TMP / "state" / "running-tasks.json"),
    "PM_OS_WORKTREES_DIR": str(TMP / "worktrees"),
    "PM_OS_LOCKS_DIR": str(TMP / "locks"),
    "CC_CMUX_SESSION_JSON": str(TMP / "no-session-store.json"),
})
_spec = importlib.util.spec_from_file_location("dispatch", SCRIPTS / "dispatch.py")
dispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch)  # type: ignore[union-attr]
import windows_memory  # noqa: E402

CFG = {"max_workers": 0, "max_workers_windows": 3, "commit_hold_pct": 92,
       "commit_alert_pct": 85, "zombie_alert_count": 50000, "swap_alert_gb": 16}
HIGH = {"commit_pct": 94.0, "commit_used_gb": 47.0, "commit_limit_gb": 50.0, "free_ram_gb": 0.1,
        "zombie_processes": 350000.0, "compressor_gb": None, "swap_used_gb": None, "swap_total_gb": None}
LOW = {**HIGH, "commit_pct": 40.0, "commit_used_gb": 19.0, "zombie_processes": 1200.0}


class WorkerCapTests(unittest.TestCase):
    def test_sandbox_reads_as_not_windows(self):
        self.assertFalse(dispatch._on_windows())
        self.assertIsNone(dispatch.worker_cap(CFG))

    def test_windows_cap_replaces_unlimited(self):
        with patch.object(dispatch, "_on_windows", return_value=True):
            self.assertEqual(dispatch.worker_cap(CFG), 3)
            self.assertIsNone(dispatch.worker_cap({**CFG, "max_workers_windows": 0}))
            self.assertTrue(dispatch.at_capacity(3, CFG, memory=LOW))
            self.assertFalse(dispatch.at_capacity(2, CFG, memory=LOW))

    def test_mac_ignores_windows_cap(self):
        with patch.object(dispatch, "_on_windows", return_value=False):
            self.assertIsNone(dispatch.worker_cap(CFG))
            self.assertEqual(dispatch.worker_cap({**CFG, "max_workers": 6}), 6)


class CommitHoldTests(unittest.TestCase):
    def test_hold_at_or_above_threshold(self):
        reason = dispatch.commit_hold_reason(CFG, memory=HIGH)
        self.assertIn("94%", reason)
        self.assertIn("reboot", reason)
        self.assertTrue(dispatch.at_capacity(0, CFG, memory=HIGH))

    def test_no_hold_below_threshold_or_unreadable_or_off(self):
        self.assertEqual(dispatch.commit_hold_reason(CFG, memory=LOW), "")
        self.assertEqual(dispatch.commit_hold_reason(CFG, memory={}), "")
        self.assertEqual(dispatch.commit_hold_reason({**CFG, "commit_hold_pct": 0}, memory=HIGH), "")

    def test_no_live_probe_off_windows(self):
        with patch.object(windows_memory, "snapshot", side_effect=AssertionError("probed")):
            self.assertEqual(dispatch.commit_hold_reason(CFG), "")


class CapacityReasonTests(unittest.TestCase):
    def test_high_commit_and_zombies_are_reported(self):
        snap = dispatch.capacity_snapshot(CFG, memory=HIGH)
        text = " ".join(snap["reasons"])
        self.assertIn("memory commit 94%", text)
        self.assertIn("350,000 zombie processes", text)
        line = dispatch.capacity_text(snap)
        self.assertIn("commit 47.0 GB of 50.0 GB (94%)", line)
        self.assertIn("350,000 zombie processes", line)

    def test_healthy_windows_has_no_reason(self):
        self.assertEqual(dispatch.capacity_snapshot(CFG, memory=LOW)["reasons"], [])

    def test_mac_snapshot_text_unchanged(self):
        mac = {"free_ram_gb": 2.0, "compressor_gb": 3.0, "swap_used_gb": 20.0, "swap_total_gb": 24.0}
        snap = dispatch.capacity_snapshot(CFG, memory=mac)
        self.assertIn("swap used 20.0 GB", " ".join(snap["reasons"]))
        self.assertIn("compressed", dispatch.capacity_text(snap))


class MemoryProbeTests(unittest.TestCase):
    def test_problems_thresholds(self):
        self.assertEqual(len(windows_memory.problems(HIGH, 85, 50000)), 2)
        self.assertEqual(windows_memory.problems(LOW, 85, 50000), [])
        self.assertEqual(windows_memory.problems({}, 85, 50000), [])

    @unittest.skipUnless(os.name == "nt", "live probe is Windows only")
    def test_live_probe_reads_this_machine(self):
        snap = windows_memory.snapshot()
        self.assertGreater(snap["commit_limit_gb"], 0)
        self.assertGreaterEqual(snap["zombie_processes"], 0)
        self.assertGreater(snap["live_processes"], 10)

    def test_check_exit_codes(self):
        with patch.object(windows_memory, "snapshot", return_value=HIGH):
            self.assertEqual(windows_memory.main(["--check"]), 1)
        with patch.object(windows_memory, "snapshot", return_value=LOW):
            self.assertEqual(windows_memory.main(["--check"]), 0)


class ReapKillTests(unittest.TestCase):
    def test_sandbox_never_kills(self):
        with patch.object(dispatch, "run", side_effect=AssertionError("ran powershell")):
            out = dispatch.kill_task_processes({"id": "rt-000000000007", "worktree": "C:/x/rt-000000000007"})
        self.assertEqual(out["killed"], 0)

    def test_passes_only_the_task_id(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["task"] = kw["env"]["PMOS_KILL_TASK"]
            return type("R", (), {"stdout": '{"matched": 2, "killed": 2}\n'})()

        with patch.object(dispatch, "_on_windows", return_value=True), patch.object(dispatch, "run", fake_run):
            out = dispatch.kill_task_processes({"id": "RT-000000000007", "worktree": r"C:\Wt\rt-000000000007"})
        self.assertEqual(out, {"matched": 2, "killed": 2})
        self.assertEqual(seen["task"], "rt-000000000007")

    def test_non_worker_ids_are_refused(self):
        with patch.object(dispatch, "_on_windows", return_value=True), \
                patch.object(dispatch, "run", side_effect=AssertionError("ran powershell")):
            for bad in ("", "capacity-20261107", "inventory-shrink-20261108-0110", "rt-1"):
                self.assertEqual(dispatch.kill_task_processes({"id": bad})["killed"], 0)

    def test_script_spares_dispatch_and_other_programs(self):
        ps = dispatch._KILL_TASK_PROCESSES_PS
        self.assertIn("dispatch.py", ps)
        self.assertIn("router-dispatch.sh", ps)
        self.assertNotIn("taskkill", ps)          # no /T walk over reused PIDs
        self.assertIn("CreationDate -gt", ps)      # children must be younger than their parent


class MemoryAlertKindTests(unittest.TestCase):
    def test_memory_reasons_are_their_own_kind(self):
        snap = dispatch.capacity_snapshot(CFG, memory=HIGH)
        self.assertEqual(dispatch.capacity_trigger_kinds(snap["reasons"]), ["memory"])



class CapacityNoticeStatusTests(unittest.TestCase):
    """Commit at 88% is over commit_alert_pct 85 but under commit_hold_pct 92, so nothing
    is held: capacity_tick sends the direct line as done, never blocked, and report-to-cos
    never appends "Options missing (Options and Lean)". 97 zombies are far under the 50,000 alert."""

    # The notice's own numbers and text.
    NOTICE_MEMORY = {"commit_pct": 88.0, "commit_used_gb": 22.0, "commit_limit_gb": 25.0, "free_ram_gb": 2.0,
                     "zombie_processes": 97.0, "compressor_gb": None, "swap_used_gb": None, "swap_total_gb": None}
    NOTICE_TEXT = ("capacity: 2 worker(s) live (cap 3), commit 22.0 GB of 25.0 GB (88%), 2.0 GB RAM free, "
                   "97 zombie processes; memory commit 88% of the limit (22.0 GB of 25.0 GB). "
                   "New launches wait while commit is at or above dispatch.commit_hold_pct.")

    def snap(self, memory):
        with patch.object(dispatch, "live_workers", lambda payload: [{}, {}]):
            return dispatch.capacity_snapshot(CFG, memory=memory)

    def test_alert_below_the_hold_is_informational(self):
        snap = self.snap(self.NOTICE_MEMORY)
        self.assertEqual(snap["reasons"], ["memory commit 88% of the limit (22.0 GB of 25.0 GB)"])
        self.assertFalse(dispatch.capacity_launches_held(snap, CFG))
        self.assertEqual(dispatch.capacity_status(snap, CFG), "done")
        line = dispatch.capacity_text(snap, CFG)
        self.assertIn("97 zombie processes; memory commit 88% of the limit", line)
        self.assertIn("Launches are not held: commit is below the 92% hold", line)
        self.assertNotIn("New launches", line)

    def test_blocked_only_while_launches_are_held(self):
        snap = self.snap(HIGH)
        self.assertTrue(dispatch.capacity_launches_held(snap, CFG))
        self.assertEqual(dispatch.capacity_status(snap, CFG), "blocked")
        self.assertIn("New launches are held: memory commit is at 94% of the limit", dispatch.capacity_text(snap, CFG))

    def test_zombie_alert_alone_holds_nothing(self):
        snap = self.snap({**LOW, "zombie_processes": 60000.0})
        self.assertIn("zombie processes", " ".join(snap["reasons"]))
        self.assertEqual(dispatch.capacity_status(snap, CFG), "done")

    def test_mac_swap_alert_is_informational(self):
        mac = {"free_ram_gb": 2.0, "compressor_gb": 3.0, "swap_used_gb": 20.0, "swap_total_gb": 24.0}
        self.assertEqual(dispatch.capacity_status(self.snap(mac), CFG), "done")

    def test_tick_sends_the_notice_as_done(self):
        alerts = []
        with tempfile.TemporaryDirectory() as tmp,                 patch.object(dispatch, "CAPACITY_STAMP", Path(tmp) / "capacity.json"),                 patch.object(dispatch, "memory_snapshot", lambda: dict(self.NOTICE_MEMORY)),                 patch.object(dispatch, "live_workers", lambda payload: [{}, {}]),                 patch.object(dispatch, "cos_digest_line", lambda prefix, text: True),                 patch.object(dispatch, "cos_alert", lambda prefix, text, status: alerts.append((prefix, status)) or True):
            outcome = dispatch.capacity_tick(CFG)
        self.assertTrue(outcome["direct"])
        self.assertEqual(alerts, [("capacity", "done")])

    def test_options_check_skips_system_notices(self):
        import review_surface
        # Even a held (blocked) capacity line asks the owner nothing.
        for status in ("blocked", "done"):
            verdict = review_surface.evaluate([self.NOTICE_TEXT], status=status, task_id="capacity-20261115-0716")
            self.assertEqual(verdict.options_missing, [], status)
        for tid in ("auto-push-20261115-0700", "skill-sync-20261114-2210", "digest-stale-20261114"):
            self.assertTrue(review_surface.system_notice(tid), tid)
        # An owner task that blocks without options still gets the warning.
        self.assertFalse(review_surface.system_notice("rt-00000000006e"))
        self.assertFalse(review_surface.system_notice("rt-20261115"))
        owner = review_surface.evaluate([self.NOTICE_TEXT], status="blocked", task_id="rt-00000000006e")
        self.assertEqual(owner.options_missing, ["Options", "Lean"])


class CommitCeilingTests(unittest.TestCase):
    """92% of the current limit must not hold every launch while a process still starts
    fast. The system-managed pagefile grows the limit on demand, so
    the hold reads the ceiling, then a measured stall, and one worker always starts."""

    CFG = {**CFG, "memory_hold_floor_workers": 1, "spawn_hold_ms": 400}
    # An invented reading, with the ceiling the probe reads for it.
    TODAY = {**HIGH, "commit_pct": 94.0, "commit_used_gb": 30.0, "commit_limit_gb": 32.0,
             "commit_ceiling_gb": 60.0, "commit_ceiling_pct": 50.0, "free_ram_gb": 0.5, "zombie_processes": 96.0}

    def test_paging_files_parse(self):
        with patch.dict(os.environ, {"SystemDrive": "C:"}):
            self.assertEqual(windows_memory.parse_paging_files([r"?:\pagefile.sys"]), [(r"C:\pagefile.sys", None)])
        self.assertEqual(windows_memory.parse_paging_files([r"C:\pagefile.sys 0 0"]), [(r"C:\pagefile.sys", None)])
        self.assertEqual(windows_memory.parse_paging_files([r"D:\pagefile.sys 4096 8192"]), [(r"D:\pagefile.sys", 8.0)])
        self.assertEqual(windows_memory.parse_paging_files([""]), [])
        self.assertEqual(windows_memory.parse_paging_files([r"D:\page file\pagefile.sys 0 0"]),
                         [(r"D:\page file\pagefile.sys", None)])
        self.assertEqual(windows_memory.parse_paging_files([r"C:\pagefile.sys 16384"]), [(r"C:\pagefile.sys", 16.0)])

    def test_ceiling_math(self):
        self.assertEqual(windows_memory.managed_pagefile_max_gb(12.0, 1000.0), 12.0 * 3)
        self.assertEqual(windows_memory.managed_pagefile_max_gb(1.0, 1000.0), 4.0)
        self.assertEqual(windows_memory.managed_pagefile_max_gb(16.0, 200.0), 25.0)   # an eighth of the volume
        # System-managed, 20.0 GB now, plenty of disk: grows to 3 x RAM.
        self.assertAlmostEqual(windows_memory.commit_ceiling_gb(36.0, 12.0, [(20.0, None, 1000.0, 400.0)]),
                               36.0 + 36.0 - 20.0)
        # Fixed at its max: nothing to grow.
        self.assertEqual(windows_memory.commit_ceiling_gb(36.0, 12.0, [(20.0, 20.0, 1000.0, 400.0)]), 36.0)
        # A nearly full disk: growth stops at free space minus the reserve.
        self.assertAlmostEqual(windows_memory.commit_ceiling_gb(36.0, 12.0, [(20.0, None, 1000.0, 14.0)]), 40.0)
        self.assertEqual(windows_memory.commit_ceiling_gb(36.0, 12.0, [(20.0, None, 1000.0, 5.0)]), 36.0)
        # Two pagefiles on one drive share its free space (30 GB free, 20 usable).
        self.assertEqual(windows_memory.commit_ceiling_gb(30.0, 16.0, [(2.0, None, 1000.0, 30.0, "C:"),
                                                                       (2.0, None, 1000.0, 30.0, "c:")]), 50.0)

    def test_growable_limit_without_a_stall_holds_nothing(self):
        self.assertEqual(dispatch.commit_hold_reason(self.CFG, memory=self.TODAY, live=2), "")
        self.assertEqual(dispatch.commit_hold_reason(self.CFG, memory={**self.TODAY, "spawn_ms": 20.0}, live=2), "")

    def test_a_measured_stall_holds(self):
        reason = dispatch.commit_hold_reason(self.CFG, memory={**self.TODAY, "spawn_ms": 800.0}, live=2)
        self.assertIn("800 ms to start", reason)
        self.assertEqual(dispatch.commit_hold_reason({**self.CFG, "spawn_hold_ms": 0},
                                                     memory={**self.TODAY, "spawn_ms": 800.0}, live=2), "")
        # A spawn that fails (WinError 1455 at real exhaustion) or times out is a stall.
        failed = dispatch.commit_hold_reason(self.CFG, memory={**self.TODAY, "spawn_ms": float("inf")}, live=2)
        self.assertIn("fail to start", failed)

    def test_failed_spawn_reads_as_infinite(self):
        import subprocess
        with patch.object(windows_memory.os, "name", "nt"), \
                patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            self.assertEqual(windows_memory.spawn_ms(), float("inf"))
        with patch.object(windows_memory.os, "name", "nt"), \
                patch.object(subprocess, "run", side_effect=OSError(1455, "paging file is too small")):
            self.assertEqual(windows_memory.spawn_ms(), float("inf"))

    def test_near_the_ceiling_holds(self):
        full = {**self.TODAY, "commit_used_gb": 57.0, "commit_ceiling_pct": 93.6}
        self.assertIn("94% of the most it can grow to", dispatch.commit_hold_reason(self.CFG, memory=full, live=2))

    def test_floor_lets_one_worker_start(self):
        full = {**self.TODAY, "commit_ceiling_pct": 95.0}
        self.assertEqual(dispatch.commit_hold_reason(self.CFG, memory=full, live=0), "")
        self.assertTrue(dispatch.commit_hold_reason(self.CFG, memory=full, live=1))
        self.assertFalse(dispatch.at_capacity(0, self.CFG, memory=full))
        self.assertTrue(dispatch.at_capacity(1, self.CFG, memory=full))
        self.assertEqual(dispatch.commit_hold_reason({**self.CFG, "memory_hold_floor_workers": 0},
                                                     memory=full, live=0)[:13], "memory commit")

    def test_capacity_line_reads_the_ceiling(self):
        with patch.object(dispatch, "live_workers", lambda payload: [{}]):
            snap = dispatch.capacity_snapshot(self.CFG, memory=self.TODAY)
        self.assertEqual(snap["reasons"], [])        # 50% of the ceiling is under the 85% alert
        self.assertEqual(dispatch.capacity_status(snap, self.CFG), "done")
        line = dispatch.capacity_text(snap, self.CFG)
        self.assertIn("can grow to 60.0 GB, 50% of that", line)
        self.assertIn("Launches are not held", line)
        stalled = {**self.TODAY, "spawn_ms": 900.0}
        with patch.object(dispatch, "live_workers", lambda payload: [{}]):
            snap = dispatch.capacity_snapshot(self.CFG, memory=stalled)
        self.assertEqual(dispatch.capacity_trigger_kinds(snap["reasons"]), ["memory"])
        self.assertEqual(dispatch.capacity_status(snap, self.CFG), "blocked")
        with patch.object(dispatch, "live_workers", lambda payload: []):
            snap = dispatch.capacity_snapshot(self.CFG, memory=stalled)
        self.assertEqual(dispatch.capacity_status(snap, self.CFG), "done")
        self.assertIn("no worker is live", dispatch.capacity_text(snap, self.CFG))

    def test_live_probe_only_spawns_when_the_old_gauge_trips(self):
        calls = []
        with patch.object(dispatch, "_on_windows", return_value=True),                 patch.object(windows_memory, "snapshot", return_value=dict(self.TODAY)),                 patch.object(windows_memory, "spawn_ms", lambda samples=3: calls.append(1) or 30.0):
            self.assertEqual(dispatch.commit_hold_reason(self.CFG, live=2), "")
            self.assertEqual(len(calls), 1)
        with patch.object(dispatch, "_on_windows", return_value=True),                 patch.object(windows_memory, "snapshot", return_value={**self.TODAY, "commit_pct": 80.0}),                 patch.object(windows_memory, "spawn_ms", side_effect=AssertionError("spawned")):
            self.assertEqual(dispatch.commit_hold_reason(self.CFG, live=2), "")

    @unittest.skipUnless(os.name == "nt", "live probe is Windows only")
    def test_live_ceiling_and_spawn(self):
        snap = windows_memory.snapshot()
        self.assertGreaterEqual(snap["commit_ceiling_gb"], snap["commit_limit_gb"])
        self.assertIsNotNone(windows_memory.spawn_ms(1))
        self.assertIn("Toke", windows_memory.pool_tags())

if __name__ == "__main__":
    unittest.main()
