#!/usr/bin/env python3
"""keep_awake.sync holds while work is live, releases when none is, and does nothing off Windows.

Run: python3 system/scripts/test_keep_awake.py
No test starts a real hold: every case passes a fake runner, and the sandbox reads as not Windows.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import keep_awake as ka  # noqa: E402


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> dict:
        self.calls.append(args)
        if "-Ensure" in args:
            return {"action": "started" if len(self.calls) == 1 else "extended", "pid": 4242, "owner": "dispatch"}
        if "-Release" in args:
            return {"action": "released"}
        return {"error": "unexpected"}


class KeepAwakeSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.leases = Path(self.tmp.name) / "leases"
        patcher = mock.patch.object(ka, "LOCK_PATH", Path(self.tmp.name) / "keep-awake.lock")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def sync(self, live, runner, state_exists=True, **kw):
        return ka.sync(live, runner=runner, windows=True, lease_dir=self.leases,
                       state_exists=lambda: state_exists, **kw)

    def test_live_work_starts_then_keeps_one_holder(self) -> None:
        run = FakeRunner()
        first = self.sync(["rt-a"], run)
        second = self.sync(["rt-a", "rt-b"], run)
        self.assertEqual(first["action"], "started")
        self.assertEqual(second["action"], "extended")
        self.assertEqual(second["live_count"], 2)
        # Every live pass asks for -Ensure as the dispatch owner, never a plain start that replaces the holder.
        for call in run.calls:
            self.assertEqual(call[0], "-Ensure")
            self.assertIn("dispatch", call)
            self.assertNotIn("-Stop", call)

    def test_nothing_live_releases_only_the_dispatch_hold(self) -> None:
        run = FakeRunner()
        out = self.sync([], run)
        self.assertEqual(out["action"], "released")
        self.assertEqual(run.calls, [["-Release", "-Owner", "dispatch"]])

    def test_idle_with_no_state_file_spawns_nothing(self) -> None:
        run = FakeRunner()
        out = self.sync([], run, state_exists=False)
        self.assertEqual(out["action"], "idle")
        self.assertEqual(run.calls, [])

    def test_a_lease_counts_as_live_work_until_it_expires(self) -> None:
        run = FakeRunner()
        ka.lease("suite-rerun", 30, lease_dir=self.leases)
        self.assertEqual(self.sync([], run)["live"], ["lease:suite-rerun"])
        self.assertEqual(run.calls[-1][0], "-Ensure")
        # Expired: removed, and the next pass releases.
        path = self.leases / "suite-rerun.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(row), encoding="utf-8")
        self.assertEqual(self.sync([], run)["action"], "released")
        self.assertFalse(path.exists())

    def test_unlease_ends_a_lease(self) -> None:
        ka.lease("overnight", 60, lease_dir=self.leases)
        self.assertTrue(ka.unlease("overnight", lease_dir=self.leases))
        self.assertEqual(ka.active_leases(self.leases), [])
        with self.assertRaises(ValueError):
            ka.lease("../escape", 5, lease_dir=self.leases)

    def test_mac_and_sandbox_do_nothing(self) -> None:
        run = FakeRunner()
        self.assertEqual(ka.sync(["rt-a"], runner=run, windows=False)["action"], "skipped")
        # The default reads the sandbox as not Windows, so a dispatch test never holds this laptop.
        self.assertEqual(ka.sync(["rt-a"], runner=run)["action"], "skipped")
        self.assertEqual(run.calls, [])

    def test_kill_switch_releases_even_with_work_live(self) -> None:
        run = FakeRunner()
        with mock.patch.dict(os.environ, {"SWITCHBOARD_KEEP_AWAKE": "off"}):
            out = self.sync(["rt-a"], run)
        self.assertEqual(out["action"], "released")
        self.assertTrue(out["disabled"])

    def test_a_runner_fault_never_raises(self) -> None:
        def boom(args):
            raise RuntimeError("pwsh vanished")
        self.assertEqual(self.sync(["rt-a"], boom)["action"], "error")

    def test_hold_is_bounded_and_longer_than_a_tick(self) -> None:
        run = FakeRunner()
        self.sync(["rt-a"], run)
        hours = float(run.calls[0][run.calls[0].index("-Hours") + 1])
        self.assertGreater(hours * 60, 15)   # the Windows tick is 15 minutes
        self.assertLessEqual(hours, 24)      # keep-awake.ps1's own bound


class WorkingOnly(unittest.TestCase):
    """Only a worker that is doing something holds the laptop."""

    NOW = datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
    S1 = "11111111-1111-1111-1111-111111111111"
    S2 = "22222222-2222-2222-2222-222222222222"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.temp = Path(self.tmp.name) / "claude"
        self.addCleanup(self.tmp.cleanup)

    def split(self, rows, tails, in_use=lambda p: False):
        return ka.working(rows, tail=lambda r: tails.get(r["id"]), now=self.NOW, temp_dir=self.temp,
                          quiet_minutes=20, in_use=in_use)

    def test_a_silent_background_job_still_running_keeps_its_worker(self) -> None:
        tasks = self.temp / "abc123" / self.S1 / "tasks"
        tasks.mkdir(parents=True)
        f = tasks / "quiet.output"
        f.write_text("", encoding="utf-8")
        stamp = self.ago(50).timestamp()
        os.utime(f, (stamp, stamp))
        rows = [{"id": "rt-quiet", "session_id": self.S1}]
        tails = {"rt-quiet": {"last_at": self.ago(50)}}
        self.assertEqual(self.split(rows, tails, in_use=lambda p: p.name == "quiet.output")["working"], ["rt-quiet"])
        self.assertEqual(self.split(rows, tails)["idle"], ["rt-quiet"])

    def test_an_unreadable_transcript_counts_as_working_and_spares_the_others(self) -> None:
        def tail(row):
            if row["id"] == "rt-bad":
                raise UnicodeDecodeError("utf-8", bytes([0xC3]), 0, 1, "unexpected end of data")
            return {"last_at": self.ago(1)}
        rows = [{"id": "rt-bad", "session_id": self.S1}, {"id": "rt-ok", "session_id": self.S2}]
        out = ka.working(rows, tail=tail, now=self.NOW, temp_dir=self.temp, quiet_minutes=20, in_use=lambda p: False)
        self.assertEqual(out["working"], ["rt-bad", "rt-ok"])

    def test_file_in_use_sees_an_open_handle(self) -> None:
        f = Path(self.tmp.name) / "held.output"
        f.write_text("x", encoding="utf-8")
        self.assertFalse(ka._file_in_use(f))
        if os.name != "nt":
            return
        import subprocess
        code = "import sys,time; h=open(sys.argv[1],'a'); print('ok',flush=True); time.sleep(30)"
        holder = subprocess.Popen([sys.executable, "-c", code, str(f)], stdout=subprocess.PIPE, text=True)
        try:
            holder.stdout.readline()
            self.assertTrue(ka._file_in_use(f))
        finally:
            holder.kill()
            holder.wait()

    def ago(self, minutes):
        return self.NOW - timedelta(minutes=minutes)

    def test_recent_transcript_write_holds_and_a_parked_worker_does_not(self) -> None:
        rows = [{"id": "rt-busy", "session_id": self.S1, "started_at": self.ago(120).isoformat()},
                {"id": "rt-parked", "session_id": self.S2, "started_at": self.ago(300).isoformat()}]
        out = self.split(rows, {"rt-busy": {"last_at": self.ago(3)}, "rt-parked": {"last_at": self.ago(45)}})
        self.assertEqual(out, {"working": ["rt-busy"], "idle": ["rt-parked"]})

    def test_fresh_launch_and_unknown_transcript_count_as_working(self) -> None:
        rows = [{"id": "rt-new"}, {"id": "rt-amp", "thread_id": "T-abc"}]
        out = self.split(rows, {})
        self.assertEqual(out["working"], ["rt-new", "rt-amp"])

    def test_background_task_output_keeps_a_quiet_transcript_working(self) -> None:
        tasks = self.temp / "abc123" / self.S1 / "tasks"
        tasks.mkdir(parents=True)
        out_file = tasks / "b1.output"
        out_file.write_text("running", encoding="utf-8")
        stamp = self.ago(2).timestamp()
        os.utime(out_file, (stamp, stamp))
        rows = [{"id": "rt-bg", "session_id": self.S1}]
        out = self.split(rows, {"rt-bg": {"last_at": self.ago(90)}})
        self.assertEqual(out["working"], ["rt-bg"])

    def test_a_recent_heartbeat_counts(self) -> None:
        rows = [{"id": "rt-hb", "session_id": self.S1, "heartbeat_at": self.ago(5).isoformat().replace("+00:00", "Z")}]
        self.assertEqual(self.split(rows, {"rt-hb": {"last_at": self.ago(60)}})["working"], ["rt-hb"])

    def test_sync_rows_releases_when_every_worker_is_idle(self) -> None:
        run = FakeRunner()
        rows = [{"id": "rt-parked", "session_id": self.S2}]
        with mock.patch.object(ka, "LOCK_PATH", Path(self.tmp.name) / "k.lock"),                 mock.patch.object(ka, "working", return_value={"working": [], "idle": ["rt-parked"]}):
            result = ka.sync_rows(rows, tail=lambda r: None, runner=run, windows=True,
                                  lease_dir=Path(self.tmp.name) / "leases", state_exists=lambda: True)
        self.assertEqual(run.calls[0][0], "-Release")
        self.assertEqual(result["idle_workers"], ["rt-parked"])

    def test_sync_rows_does_nothing_off_windows(self) -> None:
        self.assertEqual(ka.sync_rows([{"id": "rt-a"}], tail=lambda r: None)["action"], "skipped")


class DispatchWiring(unittest.TestCase):
    def test_sweep_and_launch_call_sync(self) -> None:
        src = (Path(__file__).resolve().parent / "dispatch.py").read_text(encoding="utf-8")
        self.assertIn('keep_awake.sync([task["id"]])', src)
        self.assertIn("keep_awake.sync_rows(live_workers(ledger.load_payload()), tail=ledger.claude_tail)", src)
        self.assertIn('"keep_awake": awake', src)


if __name__ == "__main__":
    unittest.main()
