#!/usr/bin/env python3
"""Tests for scheduled_jobs.py (the due check) and scheduled-job-dispatch.py.

Wrong impls these tests catch: a weekly job that is not caught up on a later
day, a not_before slot that fires early on the due day, a catch-up that fires
before catchup_not_before, a new job firing as a catch-up before first_due,
the wrong biweekly week, next_fire skipping the slot, and a dispatch dry run
that writes state or runs the router.

Run: python3 system/scripts/test_scheduled_jobs.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scheduled_jobs as sj  # noqa: E402

DISPATCH = HERE / "scheduled-job-dispatch.py"


def pt(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=sj.PT) if sj.PT is not None else value


class Evaluate(unittest.TestCase):
    def test_daily(self):
        job = {"schedule": "daily"}
        self.assertTrue(sj.evaluate(job, None, pt("2026-03-04T09:00"))["due"])
        self.assertFalse(sj.evaluate(job, "2026-03-04", pt("2026-03-04T09:00"))["due"])

    def test_weekly_due_day_and_catch_up(self):
        job = {"schedule": "weekly-monday"}  # 2026-03-02 is a Monday
        self.assertEqual(sj.evaluate(job, "2026-02-23", pt("2026-03-02T09:00"))["reason"], "due today")
        later = sj.evaluate(job, "2026-02-23", pt("2026-03-04T09:00"))
        self.assertTrue(later["due"])
        self.assertEqual(later["due_date"], "2026-03-02")
        self.assertIn("catch-up", later["reason"])
        self.assertFalse(sj.evaluate(job, "2026-03-02", pt("2026-03-04T09:00"))["due"])

    def test_not_before_on_due_day(self):
        job = {"schedule": "weekly-monday", "not_before": "15:00"}
        self.assertFalse(sj.evaluate(job, "2026-02-23", pt("2026-03-02T14:59"))["due"])
        self.assertTrue(sj.evaluate(job, "2026-02-23", pt("2026-03-02T15:00"))["due"])

    def test_not_before_still_catches_up_a_missed_week(self):
        job = {"schedule": "weekly-monday", "not_before": "15:00"}
        early = sj.evaluate(job, "2026-02-16", pt("2026-03-02T10:00"))
        self.assertTrue(early["due"])
        self.assertEqual(early["due_date"], "2026-02-23")

    def test_catch_up_waits_for_morning(self):
        job = {"schedule": "weekly-friday"}  # 2026-03-06 is a Friday
        self.assertFalse(sj.evaluate(job, "2026-02-27", pt("2026-03-07T03:00"))["due"])
        self.assertTrue(sj.evaluate(job, "2026-02-27", pt("2026-03-07T08:00"))["due"])
        custom = {"schedule": "weekly-friday", "catchup_not_before": "10:30"}
        self.assertFalse(sj.evaluate(custom, "2026-02-27", pt("2026-03-07T10:00"))["due"])

    def test_first_due(self):
        job = {"schedule": "weekly-monday", "first_due": "2026-03-09"}
        result = sj.evaluate(job, None, pt("2026-03-04T09:00"))
        self.assertFalse(result["due"])
        self.assertEqual(result["due_date"], "2026-03-09")
        self.assertTrue(sj.evaluate(job, None, pt("2026-03-09T09:00"))["due"])

    def test_biweekly(self):
        job = {"schedule": "biweekly-monday"}
        self.assertEqual(sj.evaluate(job, None, pt("2026-01-05T09:00"))["due_date"], "2026-01-05")
        self.assertEqual(sj.evaluate(job, None, pt("2026-01-12T09:00"))["due_date"], "2026-01-05")
        self.assertEqual(sj.evaluate(job, None, pt("2026-01-19T09:00"))["due_date"], "2026-01-19")

    def test_unknown_schedule(self):
        self.assertFalse(sj.evaluate({"schedule": "hourly"}, None, pt("2026-03-04T09:00"))["due"])

    def test_next_fire(self):
        job = {"schedule": "weekly-monday", "not_before": "15:00"}
        nxt = sj.next_fire(job, "2026-03-02", pt("2026-03-04T09:07"))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.strftime("%Y-%m-%d %H:%M"), "2026-03-09 15:00")


class DispatchCli(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sj-dispatch-test-"))
        (self.root / "system").mkdir()
        jobs = ("jobs:\n"
                "  - name: weekly-review\n    schedule: weekly-monday\n    not_before: \"15:00\"\n"
                "    dispatch: router\n    workstream: example-stream\n"
                "    command: \"Run the weekly review for {due_date} (last run {last_run}).\"\n"
                "  - name: in-tab\n    schedule: daily\n")
        (self.root / "system" / "scheduled-jobs.yaml").write_text(jobs, encoding="utf-8")

    def run_cli(self, *args):
        env = {**os.environ, "SCHEDULED_JOB_DISPATCH_REPO_ROOT": str(self.root)}
        proc = subprocess.run([sys.executable, str(DISPATCH), *args], capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_due_json(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        rows = json.loads(self.run_cli("due", "--now", "2026-03-02T15:05", "--json"))
        by_name = {r["name"]: r for r in rows}
        self.assertTrue(by_name["weekly-review"]["due"])
        self.assertEqual(by_name["weekly-review"]["dispatch"], "router")

    def test_dry_run_writes_nothing(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        out = json.loads(self.run_cli("run", "weekly-review", "--now", "2026-03-02T15:05", "--dry-run"))
        self.assertEqual(out["result"], "dry-run")
        self.assertTrue(out["prompt"].startswith("Workstream: example-stream\n"))
        self.assertIn("2026-03-02", out["prompt"])
        self.assertIn("--request-id", out["command"])
        self.assertIn("weekly-review:2026-03-02", out["command"])
        self.assertFalse((self.root / "system" / "job-state.json").exists())
        self.assertFalse((self.root / "system" / "state").exists())

    def test_tick_kill_switch(self):
        env = {**os.environ, "SCHEDULED_JOB_DISPATCH_REPO_ROOT": str(self.root),
               "SCHEDULED_JOB_DISPATCH_DISABLED": "1"}
        proc = subprocess.run([sys.executable, str(DISPATCH), "tick", "--now", "2026-03-02T15:05"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
