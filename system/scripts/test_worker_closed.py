#!/usr/bin/env python3
"""Tests for the pm-os Claude plugin hook hooks/worker-closed.py.

The hook stops a worker tab whose task is already reported from acting on a
late message. Wrong impls these tests catch: a report-to-cos.sh name anywhere
in a chained command passing the allow list, a same-named script outside
system/scripts passing, a terminal row blocking a tab that a newer live row
took over, a typed prompt after the close not lifting the block, read-only
tools being denied, the kill switch being ignored, a hook that exits non-zero,
and hooks.json not registering the hook for both events.

Run: python3 system/scripts/test_worker_closed.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["PM_OS_TEST_SANDBOX"] = "1"

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "claude-marketplace" / "plugins" / "pm-os" / "hooks" / "worker-closed.py"
HOOKS_JSON = HOOK.parent / "hooks.json"

spec = importlib.util.spec_from_file_location("worker_closed", HOOK)
wc = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wc)


class AllowedBash(unittest.TestCase):
    def test_report_retry_passes(self):
        for cmd in (
            "bash system/scripts/report-to-cos.sh --task-id rt-aaaa --status done 'x'",
            "bash /home/example/pm-os/system/scripts/report-to-cos.sh --status done 'x'",
            "cd /home/example/pm-os && bash system/scripts/report-to-cos.sh --status done 'x' 2>&1 | tail -5",
            "REPORT_TO_COS_NO_HOOKS=1 bash system/scripts/report-to-cos.sh 'x'",
            "python3 system/scripts/running-task-state.py show --task-id rt-aaaa | jq .status",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(wc.allowed_bash(cmd, "rt-aaaa"))

    def test_chained_or_redirected_commands_fail(self):
        for cmd in (
            "rm -rf x; bash system/scripts/report-to-cos.sh 'x'",
            "echo hi > f && bash system/scripts/report-to-cos.sh 'x'",
            "bash system/scripts/report-to-cos.sh 'x' || rm -rf x",
            "bash system/scripts/report-to-cos.sh $(cat payload)",
            "bash system/scripts/report-to-cos.sh `cat payload`",
            "bash system/scripts/report-to-cos.sh 'x' | tail -5 && rm -rf x",
            "bash system/scripts/report-to-cos.sh 'x' &",
            "cd a b && bash system/scripts/report-to-cos.sh 'x'",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(wc.allowed_bash(cmd))

    def test_same_named_script_elsewhere_fails(self):
        self.assertFalse(wc.allowed_bash("bash /tmp/report-to-cos.sh 'x'"))
        self.assertFalse(wc.allowed_bash("bash ./report-to-cos.sh 'x'"))

    def test_untrusted_filter_fails(self):
        self.assertFalse(wc.allowed_bash("bash system/scripts/report-to-cos.sh 'x' | ./tail -5"))
        self.assertFalse(wc.allowed_bash("bash system/scripts/report-to-cos.sh 'x' | python3 -c 'x'"))

    def test_other_running_task_state_verbs_fail(self):
        self.assertFalse(wc.allowed_bash("python3 system/scripts/running-task-state.py set --status done"))

    def test_quoted_separators_do_not_split(self):
        self.assertTrue(wc.allowed_bash("bash system/scripts/report-to-cos.sh 'done; see a > b'"))
        self.assertTrue(wc.allowed_bash("bash system/scripts/report-to-cos.sh 'costs $5, path C:\\x'"))

    def test_ansi_c_quote_and_expansion_fail(self):
        for cmd in (
            "system/scripts/report-to-cos.sh $'\\'' ; touch /tmp/x ; echo \\'",
            "bash system/scripts/report-to-cos.sh \"$HOME\"",
            "bash system/scripts/report-to-cos.sh \"$(id)\"",
            "bash system/scripts/report-to-cos.sh done\\;touch",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(wc.allowed_bash(cmd))

    def test_only_listed_assignments(self):
        self.assertTrue(wc.allowed_bash("REPORT_TO_COS_NO_HOOKS=1 bash system/scripts/report-to-cos.sh x"))
        for cmd in (
            "PATH=/tmp/evil:/usr/bin bash system/scripts/report-to-cos.sh x",
            "BASH_ENV=/tmp/evil bash system/scripts/report-to-cos.sh x",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0='touch /tmp/x' "
            "bash system/scripts/report-to-cos.sh x",
            "PYTHONPATH=/tmp/evil python3 system/scripts/running-task-state.py show",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(wc.allowed_bash(cmd))

    def test_report_only_for_this_task(self):
        ok = "bash system/scripts/report-to-cos.sh --task-id rt-aaaa --status done x"
        self.assertTrue(wc.allowed_bash(ok, "rt-aaaa"))
        self.assertFalse(wc.allowed_bash(ok, "rt-bbbb"))
        self.assertFalse(wc.allowed_bash("bash system/scripts/report-to-cos.sh --task-id=rt-other x", "rt-aaaa"))
        self.assertFalse(wc.allowed_bash(
            "bash system/scripts/report-to-cos.sh --task-id rt-aaaa --create-if-missing x", "rt-aaaa"))


class HookRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="worker-closed-test-"))
        self.ledger = self.tmp / "running-tasks.json"
        self.config = self.tmp / "plugin-config.json"
        self.typed = self.tmp / "typed"

    def write_ledger(self, rows):
        self.ledger.write_text(json.dumps({"tasks": rows}), encoding="utf-8")

    def run_hook(self, kind, payload, task_id="rt-aaaa"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
        env.update({"RUNNING_TASKS_FILE": str(self.ledger), "PM_OS_PLUGIN_CONFIG": str(self.config),
                    "PM_OS_WORKER_CLOSED_DIR": str(self.typed), "PM_OS_WORKER_TASK_ID": task_id})
        proc = subprocess.run([sys.executable, str(HOOK), kind], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def done_row(self, **extra):
        row = {"id": "rt-aaaa", "status": "done", "session_id": "sess-1", "workspace_ref": "workspace:7",
               "finished_at": "2026-01-02T10:00:00Z"}
        row.update(extra)
        return row

    def test_terminal_row_denies_edit(self):
        self.write_ledger([self.done_row()])
        out = self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit", "tool_input": {}})
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("rt-aaaa", decision["permissionDecisionReason"])

    def test_read_only_tool_passes(self):
        self.write_ledger([self.done_row()])
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Read"}), "")

    def test_report_retry_passes(self):
        self.write_ledger([self.done_row()])
        payload = {"session_id": "sess-1", "tool_name": "Bash",
                   "tool_input": {"command": "bash system/scripts/report-to-cos.sh --status done 'x'"}}
        self.assertEqual(self.run_hook("pre-tool-use", payload), "")

    def test_live_row_passes(self):
        self.write_ledger([self.done_row(status="running")])
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")

    def test_adopted_tab_with_newer_live_row_passes(self):
        self.write_ledger([self.done_row(), {"id": "rt-bbbb", "status": "running", "workspace_ref": "workspace:7"}])
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")

    def test_adopted_tab_names_latest_finished_task(self):
        self.write_ledger([self.done_row(), {"id": "rt-cccc", "status": "done", "adopting_ref": "workspace:7",
                                             "finished_at": "2026-01-03T10:00:00Z"}])
        out = self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Write"})
        self.assertIn("rt-cccc", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_typed_prompt_after_close_lifts_block(self):
        self.write_ledger([self.done_row(finished_at="2026-01-02T10:00:00Z")])
        self.assertEqual(self.run_hook("user-prompt-submit", {"session_id": "sess-1", "prompt": "hi"}), "")
        self.assertTrue(any(self.typed.glob("*.typed")))
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")

    def test_typed_marker_older_than_close_keeps_block(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.write_ledger([self.done_row(finished_at=future)])
        self.run_hook("user-prompt-submit", {"session_id": "sess-1", "prompt": "hi"})
        out = self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"})
        self.assertIn("deny", out)

    def test_not_a_worker_passes(self):
        self.write_ledger([self.done_row()])
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}, task_id=""), "")

    def test_kill_switch(self):
        self.write_ledger([self.done_row()])
        self.config.write_text(json.dumps({"worker-closed.disabled": True}), encoding="utf-8")
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")

    def test_missing_or_bad_ledger_passes(self):
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")
        self.ledger.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_hook("pre-tool-use", {"session_id": "sess-1", "tool_name": "Edit"}), "")


class HooksJson(unittest.TestCase):
    def test_registered_for_both_events(self):
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        found = set()
        for event, groups in data["hooks"].items():
            for group in groups:
                for hook in group.get("hooks", []):
                    if "worker-closed.py" in hook.get("command", ""):
                        found.add((event, hook["command"].rsplit(" ", 1)[-1]))
        self.assertIn(("PreToolUse", "pre-tool-use"), found)
        self.assertIn(("UserPromptSubmit", "user-prompt-submit"), found)


if __name__ == "__main__":
    unittest.main()
