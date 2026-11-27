#!/usr/bin/env python3
"""Tests never touch a real to-do list or a real calendar.

A combined unittest run once imported a module pinned to a real checkout before a
later test pointed the capture helper at its temp folder, so that test's captures
wrote invented to-dos into the real list. The guard has three layers, each tested here:
  1. Under SWITCHBOARD_TEST_SANDBOX=1 todo_cli.py writes only inside the temp directory
     (list, archive, lock, calendar state) and makes no calendar or helper call.
  2. owner_action_capture.run_cli refuses a non-temp list under the sandbox.
  3. test_owner_action_capture stays in its temp list whatever was imported first.
Plus a static check: every test module that drives todo_cli.py sets the sandbox.

Run from system/scripts:  python3 -m unittest test_todo_sandbox_guard
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TODO_CLI = REPO / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"


def load_cli():
    """Load this checkout's CLI with this checkout as its root, then put sys.path back."""
    saved_path = list(sys.path)
    spec = importlib.util.spec_from_file_location("todo_cli_sandbox_guard_test", TODO_CLI)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with mock.patch.dict(os.environ, {"SWITCHBOARD_ROOT": str(REPO)}):
        spec.loader.exec_module(module)
    sys.path[:] = saved_path
    return module


cli = load_cli()
REAL_TODO = REPO / "system" / "state" / "todo.json"


class SandboxPaths(unittest.TestCase):
    """Wrong impl: the sandbox flag only closed the network, so a test handed a real path wrote it."""

    def test_temp_paths_pass_and_real_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(cli.sandbox_allows_path(Path(tmp) / "system" / "state" / "todo.json"))
        self.assertFalse(cli.sandbox_allows_path(REAL_TODO))
        self.assertFalse(cli.sandbox_allows_path(Path.home() / "Development" / "switchboard" / "system" / "state" / "todo.json"))

    def test_real_state_is_refused_even_when_temp_is_its_ancestor(self) -> None:
        # Wrong impl: trusting the temp directory alone, so TMPDIR=<home> allowed the real list.
        with mock.patch.object(cli.tempfile, "gettempdir", lambda: str(Path(REAL_TODO.anchor))):
            self.assertFalse(cli.sandbox_allows_path(REAL_TODO))
        sys.path.insert(0, str(HERE))
        import owner_action_capture as capture
        with mock.patch.object(capture.tempfile, "gettempdir", lambda: str(Path(REAL_TODO.anchor))):
            self.assertFalse(capture.sandbox_path_ok(REAL_TODO))

    def test_outside_the_sandbox_nothing_changes(self) -> None:
        with mock.patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": ""}):
            self.assertTrue(cli.sandbox_allows_path(REAL_TODO))

    def test_saving_the_real_list_is_refused_before_any_file_is_opened(self) -> None:
        # Both file calls fail the test if reached, so a broken guard still writes nothing.
        boom = mock.Mock(side_effect=AssertionError("reached a real write"))
        with mock.patch.object(cli.tempfile, "mkstemp", boom), mock.patch.object(cli, "_replace_file", boom):
            with self.assertRaises(cli.SandboxRefused):
                cli.save_data(REAL_TODO, {"items": []})
            with self.assertRaises(cli.SandboxRefused):
                cli.atomic_write_text(REAL_TODO.parent / "todo-archive" / "2026-11.json", "{}")
            with self.assertRaises(cli.SandboxRefused):
                with cli.exclusive_lock(REAL_TODO):
                    self.fail("took the real lock")
        boom.assert_not_called()

    def test_a_refusal_is_a_handled_error(self) -> None:
        self.assertTrue(issubclass(cli.SandboxRefused, cli.TodoError), "the CLI prints it and exits 1")

    def test_the_cli_exits_nonzero_and_creates_nothing_outside_temp(self) -> None:
        # The child's temp dir is <tmp>/inner, so <tmp>/outer stands in for real state, harmlessly.
        with tempfile.TemporaryDirectory() as tmp:
            inner, outer = Path(tmp) / "inner", Path(tmp) / "outer" / "system" / "state"
            inner.mkdir()
            env = {**os.environ, "SWITCHBOARD_TEST_SANDBOX": "1", "TMPDIR": str(inner), "TEMP": str(inner), "TMP": str(inner),
                   "SWITCHBOARD_ROOT": str(Path(tmp) / "outer"), "PYTHONUTF8": "1",
                   "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
            done = subprocess.run([sys.executable, str(TODO_CLI), "--path", str(outer / "todo.json"),
                                   "add", "--title", "Invented probe item", "--priority", "P1", "--allow-unrouted"],
                                  env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertNotEqual(done.returncode, 0, done.stdout)
            self.assertIn("SWITCHBOARD_TEST_SANDBOX=1", done.stderr)
            self.assertFalse((outer / "todo.json").exists())
            self.assertFalse((outer / "todo.json.lock").exists())


class SandboxCalendar(unittest.TestCase):
    """Wrong impl: a P1 with a due date reached the calendar, or a helper reached Slack, from a test run."""

    def setUp(self) -> None:
        self.net = mock.Mock(side_effect=AssertionError("reached the network"))
        self.proc = mock.Mock(side_effect=AssertionError("ran an external program"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import urllib.request
        for patcher in (mock.patch.object(urllib.request, "urlopen", self.net),
                        mock.patch.object(cli.subprocess, "run", self.proc),
                        mock.patch.object(cli, "TODO_CALENDAR_STATE_PATH", Path(tmp.name) / "todo-calendar.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_every_calendar_path_fails_closed(self) -> None:
        self.assertIsNone(cli.calendar_api_request("GET", "users/me/calendarList"))
        self.assertIsNone(cli.calendar_api_request("POST", "calendars/x/events", payload={"summary": "x"}))
        self.assertIsNone(cli.todo_calendar_id())
        self.net.assert_not_called()
        self.proc.assert_not_called()

    def test_the_thread_helper_is_never_run(self) -> None:
        self.assertEqual(cli.slack_surface_results([{"id": "x", "source_ref": "https://invented.slack.com/archives/C1/p1"}]), {})
        self.proc.assert_not_called()

    def test_a_p1_hold_sync_makes_no_call_and_stores_no_event(self) -> None:
        item = {"id": "invented-hold", "title": "Invented hold probe", "priority": "P1", "status": "open",
                "due": "2030-01-08", "estimate_min": 15, "calendar_event_id": None}
        cli.sync_calendar_hold_if_needed(item)
        self.assertIsNone(item.get("calendar_event_id"))
        self.net.assert_not_called()
        self.proc.assert_not_called()


class CaptureRefusesRealList(unittest.TestCase):
    def test_run_cli_refuses_a_non_temp_list(self) -> None:
        sys.path.insert(0, str(HERE))
        import owner_action_capture as capture
        with mock.patch.object(capture, "todo_path", lambda: REAL_TODO), \
             mock.patch.object(capture, "main_checkout", lambda path: REPO), \
             mock.patch.object(capture, "cli_path", lambda: TODO_CLI), \
             mock.patch.object(capture.subprocess, "run", mock.Mock(side_effect=AssertionError("ran the CLI"))):
            self.assertEqual(capture.run_cli("list"), {"failed": "test sandbox: refusing the real to-do list"})


class ImportOrder(unittest.TestCase):
    """Wrong impl: owner_tabs imported first, pinned elsewhere, then the capture test wrote that list."""

    def test_capture_tests_stay_in_their_temp_list_whatever_ran_first(self) -> None:
        with tempfile.TemporaryDirectory() as standin:
            state = Path(standin) / "system" / "state"
            state.mkdir(parents=True)
            code = (
                "import sys, unittest\n"
                f"sys.path.insert(0, {str(HERE)!r})\n"
                "import owner_tabs  # an earlier test module's import, pinned to the stand-in checkout\n"
                "suite = unittest.defaultTestLoader.loadTestsFromName("
                "'test_owner_action_capture.Capture.test_a_followup_becomes_a_routed_p1_due_next_business_day')\n"
                "result = unittest.TextTestRunner(verbosity=0).run(suite)\n"
                "sys.exit(0 if result.wasSuccessful() else 1)\n"
            )
            env = {**os.environ, "OWNER_TABS_ROOT": standin, "SWITCHBOARD_STATE_DIR": str(state), "SWITCHBOARD_TEST_SANDBOX": "1",
                   "PYTHONUTF8": "1"}
            done = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), env=env, capture_output=True,
                                  text=True, timeout=300, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertFalse((state / "todo.json").exists(), "the capture wrote the stand-in checkout's list")


class EveryTodoTestIsSandboxed(unittest.TestCase):
    """Wrong impl: a new test drives todo_cli.py with no sandbox, and one bad path writes the real list."""

    def test_modules_that_drive_todo_cli_set_the_sandbox(self) -> None:
        drives = re.compile(r"todo_cli(\.py)?\b")
        sets = re.compile(r"""SWITCHBOARD_TEST_SANDBOX["']?\]?\s*(=|:)\s*["']1["']""")
        missing = []
        for path in sorted(HERE.glob("test_*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.name != Path(__file__).name and drives.search(text) and not sets.search(text):
                missing.append(path.name)
        self.assertEqual(missing, [], "set os.environ['SWITCHBOARD_TEST_SANDBOX'] = '1' at the top of these modules")


if __name__ == "__main__":
    unittest.main()
