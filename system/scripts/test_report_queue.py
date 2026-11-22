#!/usr/bin/env python3
"""report_queue.py: one delivery queue per tab (2026-11-12, rt-00000000003b).

24 report waiters held for the owner's busy Windows CoS, in arbitrary order, with
six auto-push notices from Nov 10-12 among fresh results. The queue delivers owner
task reports first, then the newest notice of each kind, and replays each report's
saved arguments. Every case runs against a temp ledger.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "report_queue.py"


def row(task_id: str, finished: str, status: str = "done", **extra) -> dict:
    return {"id": task_id, "status": status, "origin": "cos", "finished_at": finished, "report_delivered": False,
            "summary": f"summary of {task_id}", "workstream": extra.pop("workstream", "pm-os-maintenance"), **extra}


class ReportQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "running-tasks.json"
        self.env = {**os.environ, "RUNNING_TASKS_FILE": str(self.ledger), "PM_OS_STATE_DIR": str(self.dir),
                    "PM_OS_TEST_SANDBOX": "1"}

    def write(self, *rows: dict) -> None:
        self.ledger.write_text(json.dumps({"tasks": list(rows)}), encoding="utf-8")

    def run_queue(self, *args: str) -> str:
        proc = subprocess.run([sys.executable, str(QUEUE), *args], env=self.env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def order(self) -> list[str]:
        seen: list[str] = []
        while True:
            out = json.loads(self.run_queue("next", "--origin", "cos", *sum((["--skip", s] for s in seen), [])))
            if not out:
                return seen
            seen.append(out["task_id"])

    def test_owner_reports_first_then_the_newest_notice_of_each_kind(self) -> None:
        self.write(
            row("auto-push-20261110-2245", "2026-11-10T22:45:21Z", status="blocked"),
            row("auto-push-20261113-0455", "2026-11-13T04:55:00Z", status="blocked"),
            row("skill-sync-20261112-211641", "2026-11-13T05:16:41Z"),
            row("rt-000000000060", "2026-11-13T05:17:00Z", workstream="daily-routines"),
            row("rt-000000000061", "2026-11-13T05:18:25Z", workstream="practice-sessions"),
            row("capacity-20261113-0515", "2026-11-13T05:15:00Z"),
        )
        self.assertEqual(self.order(), ["rt-000000000060", "rt-000000000061", "auto-push-20261113-0455",
                                        "capacity-20261113-0515", "skill-sync-20261112-211641"])

    def test_delivered_cancelled_unknown_and_other_origins_are_not_queued(self) -> None:
        self.write(
            row("rt-a", "2026-11-13T05:00:00Z", report_delivered=True),
            row("rt-b", "2026-11-13T05:00:00Z", status="cancelled"),
            row("rt-c", "2026-11-13T05:00:00Z", report_delivery_unknown=True),
            row("rt-d", "2026-11-13T05:00:00Z", origin="launchpad"),
            row("rt-e", "2026-11-13T05:00:00Z", status="active"),
            row("rt-f", "2026-11-13T05:01:00Z"),
        )
        self.assertEqual(self.order(), ["rt-f"])

    def test_saved_arguments_win_over_the_ledger_summary(self) -> None:
        self.write(row("rt-full", "2026-11-13T05:00:00Z"))
        full = ["--task-id", "rt-full", "--origin", "cos", "--status", "done", "a long report\nwith a second line"]
        self.run_queue("save", "--task-id", "rt-full", "--", *full)
        self.assertEqual(json.loads(self.run_queue("next", "--origin", "cos"))["args"], full)

    def test_rebuilt_arguments_match_the_loop_retry(self) -> None:
        self.write(row("rt-x", "2026-11-13T05:00:00Z", attempt_id="rt-x.a1", thread_id="t-1"))
        args = json.loads(self.run_queue("next", "--origin", "cos"))["args"]
        self.assertEqual(args, ["--task-id", "rt-x", "--origin", "cos", "--status", "done", "--wait-seconds", "0",
                                "--workstream", "pm-os-maintenance", "--attempt-id", "rt-x.a1", "--thread-id", "t-1",
                                "summary of rt-x"])

    def test_fold_settles_older_notices_of_a_kind(self) -> None:
        self.write(
            row("auto-push-20261110-2245", "2026-11-10T22:45:21Z", status="blocked"),
            row("auto-push-20261111-0100", "2026-11-11T01:00:00Z", status="blocked"),
            row("auto-push-20261113-0455", "2026-11-13T04:55:00Z", status="blocked"),
            row("rt-keep", "2026-11-09T00:00:00Z"),
        )
        out = json.loads(self.run_queue("fold", "--origin", "cos"))
        self.assertEqual(out["folded"], 2, out)
        tasks = {t["id"]: t for t in json.loads(self.ledger.read_text(encoding="utf-8"))["tasks"]}
        self.assertTrue(tasks["auto-push-20261110-2245"]["report_delivered"])
        self.assertTrue(tasks["auto-push-20261111-0100"]["report_delivered"])
        self.assertFalse(tasks["auto-push-20261113-0455"]["report_delivered"])
        self.assertFalse(tasks["rt-keep"]["report_delivered"], "an owner report is never folded")

    def test_waiting_line_names_owner_work_and_counts_notices(self) -> None:
        self.write(row("rt-1", "2026-11-13T05:00:00Z", workstream="daily-routines"),
                   row("auto-push-20261113-0455", "2026-11-13T04:55:00Z", status="blocked"))
        self.assertEqual(self.run_queue("waiting", "--origin", "cos"),
                         "2 PM OS reports waiting for CoS: Daily Routines (done), 1 system notice")
        self.write()
        self.assertEqual(self.run_queue("waiting", "--origin", "cos"), "")


if __name__ == "__main__":
    unittest.main()
