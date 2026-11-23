#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name("background_task_scheduler.py")
SPEC = importlib.util.spec_from_file_location("background_task_scheduler", SCRIPT)
assert SPEC and SPEC.loader
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


NOW = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
LOCAL_NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def task(name: str, **overrides):
    payload = {
        "name": name,
        "enabled": True,
        "interval_minutes": 60,
        "prompt": f"run {name}",
    }
    payload.update(overrides)
    return payload


class BackgroundTaskSchedulerTest(unittest.TestCase):
    def select(self, tasks, successes=None, attempts=None, execution=None):
        if execution is None:
            execution = {
                item["name"]: {"type": "amp"}
                for item in tasks
                if isinstance(item, dict) and item.get("name")
            }
        return scheduler.select_due_tasks(
            {"tasks": tasks, "execution": execution},
            successes or {},
            attempts or {},
            now=NOW,
            local_now=LOCAL_NOW,
            default_timeout=900,
        )

    def test_oldest_attempt_runs_first_not_config_order(self):
        selected = self.select(
            [task("always-first"), task("starved-later")],
            attempts={
                "always-first": "2026-08-27T15:55:00Z",
                "starved-later": "2026-08-27T14:00:00Z",
            },
        )
        self.assertEqual([item["name"] for item in selected], ["starved-later", "always-first"])

    def test_failure_attempt_rotates_behind_never_attempted_work(self):
        selected = self.select(
            [task("failing"), task("never-attempted")],
            attempts={"failing": "2026-08-27T15:30:00Z"},
        )
        self.assertEqual([item["name"] for item in selected], ["never-attempted", "failing"])

    def test_success_timestamp_still_controls_due_cadence(self):
        selected = self.select(
            [task("fresh"), task("due")],
            successes={
                "fresh": "2026-08-27T15:30:00Z",
                "due": "2026-08-27T14:30:00Z",
            },
        )
        self.assertEqual([item["name"] for item in selected], ["due"])

    def test_tick_jitter_does_not_skip_a_30_minute_task(self):
        half_hourly = {"interval_minutes": 30}
        selected = self.select(
            [
                task("just-short", **half_hourly),
                task("slow-tick", **half_hourly),
                task("ran-mid-interval", **half_hourly),
            ],
            successes={
                # 15:30:12 stamp, 16:00:09 tick: the case that made the task hourly.
                "just-short": "2026-08-27T15:30:03Z",
                # Stamped late in a slow tick (16:31:41 stamp, 17:00:01 tick).
                "slow-tick": "2026-08-27T15:31:40Z",
                # A catch-up run 20 minutes ago is still fresh.
                "ran-mid-interval": "2026-08-27T15:40:00Z",
            },
        )
        self.assertEqual({item["name"] for item in selected}, {"just-short", "slow-tick"})

    def test_tick_slack_stays_small_for_short_and_daily_intervals(self):
        self.assertFalse(scheduler.is_due(8, 10))
        self.assertTrue(scheduler.is_due(9, 10))
        self.assertFalse(scheduler.is_due(1400, 1440))
        self.assertTrue(scheduler.is_due(1436, 1440))

    def test_disabled_and_out_of_window_tasks_are_skipped(self):
        selected = self.select([
            task("disabled", enabled=False),
            task("too-early", start_hour=10),
            task("too-late", end_hour=9),
            task("eligible", start_hour=7, end_hour=21),
        ])
        self.assertEqual([item["name"] for item in selected], ["eligible"])

    def test_emits_direct_command_without_amp_prompt(self):
        selected = self.select(
            [task("collector")],
            execution={
                "collector": {
                    "type": "command",
                    "command": "python3 system/scripts/collector.py",
                }
            },
        )
        self.assertEqual(selected, [{
            "name": "collector",
            "executor": "command",
            "command": "python3 system/scripts/collector.py",
            "timeout_seconds": 900,
        }])

    def test_rejects_task_without_execution_classification(self):
        with self.assertRaisesRegex(ValueError, "has no execution config"):
            self.select([task("unclassified")], execution={})

    def test_rejects_missing_executor_payload(self):
        with self.assertRaisesRegex(ValueError, "has no command"):
            self.select(
                [task("collector")],
                execution={"collector": {"type": "command"}},
            )

    def test_runner_cap_counts_failed_attempts(self):
        runner = SCRIPT.with_name("run-background-tasks.sh").read_text()
        count_index = runner.index("TASK_COUNT=$((TASK_COUNT + 1))", runner.index('log "Running task:'))
        execute_index = runner.index("if run_with_timeout")
        success_index = runner.index('log "OK: $TASK_NAME complete"')

        self.assertLess(count_index, execute_index)
        self.assertLess(execute_index, success_index)

    def test_runner_dispatches_commands_without_amp(self):
        runner = SCRIPT.with_name("run-background-tasks.sh").read_text()
        command_branch = runner.index("command)")
        amp_branch = runner.index("amp)", command_branch)
        self.assertIn('TASK_RUN=(/bin/bash -c "$TASK_COMMAND")', runner[command_branch:amp_branch])
        self.assertNotIn('fail_prerequisite "amp CLI not found', runner)

    @unittest.skipUnless(shutil.which("ruby"), "ruby not installed: run-background-tasks.sh needs it for the full loop only")
    def test_every_configured_task_has_an_explicit_executor(self):
        config_path = SCRIPT.parent.parent / "background-tasks.yaml"
        completed = subprocess.run(
            [
                shutil.which("ruby"),
                "-ryaml",
                "-rjson",
                "-e",
                "print JSON.generate(YAML.load_file(ARGV[0]))",
                str(config_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        config = json.loads(completed.stdout)
        task_names = {item["name"] for item in config["tasks"]}
        execution = config["execution"]

        self.assertTrue(task_names <= set(execution))
        self.assertTrue(
            {item["type"] for item in execution.values()} <= {"command", "amp"},
        )
        for name, item in execution.items():
            if item["type"] == "command":
                self.assertTrue(item.get("command", "").strip(), name)



BASH = shutil.which("bash") or "bash"


class ProfileTickHookTests(unittest.TestCase):
    """A profile's tick jobs live in the profile (2026-11-08, rt-00000000003e): core
    holds only a generic hook runner, so a machine without that profile has no job."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.scripts = self.root / "system" / "scripts"
        self.hooks = self.scripts / "profile-tick.d"
        self.hooks.mkdir(parents=True)
        self.env = {**os.environ, "REPO_ROOT": str(self.root), "STATE_DIR": str(self.root / "system" / "state"),
                    "SWITCHBOARD_PYTHON": sys.executable}

    def run_runner(self):
        # A copy, so the runner's default root and the hooks glob both sit in the fixture.
        runner = self.scripts / "run-profile-tick-hooks.sh"
        shutil.copy2(SCRIPT.with_name("run-profile-tick-hooks.sh"), runner)
        return subprocess.run([BASH, str(runner)], env=self.env, capture_output=True, text=True, timeout=120)

    def test_core_loop_runs_profile_hooks(self):
        loop = SCRIPT.with_name("command-center-loop.sh").read_text(encoding="utf-8")
        self.assertIn("run-profile-tick-hooks.sh", loop)

    def test_runner_with_no_hooks_does_nothing(self):
        self.hooks.rmdir()
        done = self.run_runner()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_runner_runs_hooks_in_order_and_survives_a_failure(self):
        (self.hooks / "a.sh").write_text('echo "a" >> "$STATE_DIR.out"; exit 7\n', newline="\n")
        (self.hooks / "b.sh").write_text('echo "b" >> "$STATE_DIR.out"\n', newline="\n")
        done = self.run_runner()
        self.assertEqual(done.returncode, 0)
        self.assertIn("profile tick hook failed: a.sh", done.stderr)
        lines = Path(self.env["STATE_DIR"] + ".out").read_text().splitlines()
        self.assertEqual(lines, ["a", "b"])

if __name__ == "__main__":
    unittest.main()
