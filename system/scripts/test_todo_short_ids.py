"""Short to-do ids resolve, failed closes are loud, and `list --bucket all` keeps open items.

A close that finds no item must say so, never "ack: bookkeeping done", and
`list --bucket all --limit 100` must never hide open to-dos behind done rows kept in file order.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TODO_CLI = REPO_ROOT / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"
COS_ACK = REPO_ROOT / "system" / "scripts" / "cos-ack.sh"

ID_A = "5c3e7a19-2b41-4d6e-9f08-3a7c1e5b9d20"
ID_B = "5c3effff-0000-4d6e-9f08-3a7c1e5b9d21"  # shares the 4-char prefix 5c3e with ID_A
ID_C = "8d1b2c47-6e90-4a3f-b5d2-7c4e9a1f0b36"


def item(item_id, title, status="open", priority="P2", due=None):
    return {"id": item_id, "title": title, "status": status, "priority": priority, "due": due,
            "source": "manual", "created_at": "2026-11-15T08:00:00Z", "tags": []}


class TodoShortIdTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.todo_path = self.root / "todo.json"
        self.write_items([item(ID_A, "Decide on kit v9"), item(ID_B, "Other item"), item(ID_C, "Set up the Android app")])

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_items(self, items):
        self.todo_path.write_text(json.dumps({"version": 2, "items": items}) + "\n", encoding="utf-8")

    def items(self):
        return {i["id"]: i for i in json.loads(self.todo_path.read_text(encoding="utf-8"))["items"]}

    def run_cli(self, *args):
        env = {**os.environ, "SWITCHBOARD_TEST_SANDBOX": "1"}
        return subprocess.run([sys.executable, str(TODO_CLI), "--path", str(self.todo_path), *args],
                              text=True, capture_output=True, env=env)

    def test_complete_accepts_unique_8_hex_prefix(self):
        result = self.run_cli("complete", "--id", "5c3e7a19")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["item"]["id"], ID_A)
        self.assertEqual(self.items()[ID_A]["status"], "done")
        self.assertEqual(self.items()[ID_B]["status"], "open")

    def test_prefix_is_case_insensitive_and_full_id_still_works(self):
        self.assertEqual(self.run_cli("complete", "--id", "8D1B2C47").returncode, 0)
        self.assertEqual(self.items()[ID_C]["status"], "done")
        self.assertEqual(self.run_cli("complete", "--id", ID_A).returncode, 0)
        self.assertEqual(self.items()[ID_A]["status"], "done")

    def test_ambiguous_prefix_fails_and_changes_nothing(self):
        result = self.run_cli("complete", "--id", "5c3e")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ambiguous id prefix 5c3e", result.stderr)
        self.assertEqual({i["status"] for i in self.items().values()}, {"open"})

    def test_unknown_and_too_short_ids_fail(self):
        for bad in ("deadbeef", "94a"):
            result = self.run_cli("complete", "--id", bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn(f"Item not found: {bad}", result.stderr)

    def test_set_status_and_snooze_take_prefixes(self):
        self.assertEqual(self.run_cli("set-status", "--id", "8d1b2c47", "--status", "waiting").returncode, 0)
        self.assertEqual(self.items()[ID_C]["status"], "waiting")
        self.assertEqual(self.run_cli("snooze", "--id", "5c3e7a19", "--until", "2026-12-01").returncode, 0)
        self.assertEqual(self.items()[ID_A]["snooze_until"], "2026-12-01")

    def test_list_all_puts_open_items_before_done_ones(self):
        done = [item(f"{n:08x}-0000-4000-8000-000000000000", f"done {n}", status="done") for n in range(150)]
        fresh = [item("46e55e02-65f3-4e50-8e37-50468b8ae8b0", "Install the second laptop"),
                 item("42a0cc92-9a04-498c-8958-2cb6b593c69a", "Send Robin Example your availability", priority="P1")]
        self.write_items(done[:60] + [fresh[0]] + done[60:] + [fresh[1]])
        result = self.run_cli("list", "--bucket", "all", "--limit", "100", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(result.stdout)["items"]
        self.assertEqual(len(listed), 100)
        self.assertEqual({i["id"] for i in listed[:2]}, {f["id"] for f in fresh})
        self.assertTrue(all(i["bucket"] == "done" for i in listed[2:]))


@unittest.skipUnless(shutil.which("bash"), "bash not on PATH")
class CosAckCloseTests(unittest.TestCase):
    """cos-ack.sh runs the real todo_cli.py against a temp to-do file through a small wrapper."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.todo_path = root / "todo.json"
        self.todo_path.write_text(json.dumps({"version": 2, "items": [item(ID_A, "Decide on kit v9")]}) + "\n",
                                  encoding="utf-8")
        self.wrapper = root / "todo_cli_wrapper.py"
        self.wrapper.write_text(textwrap.dedent(f"""\
            import subprocess, sys
            sys.exit(subprocess.run([sys.executable, {str(TODO_CLI)!r}, "--path", {str(self.todo_path)!r},
                                     *sys.argv[1:]]).returncode)
            """), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_ack(self, *args):
        env = {**os.environ, "SWITCHBOARD_TEST_SANDBOX": "1", "TODO_CLI": str(self.wrapper), "PYTHON": sys.executable,
               "REPO_ROOT": str(REPO_ROOT)}
        return subprocess.run(["bash", str(COS_ACK), *args], text=True, capture_output=True, env=env)

    def status(self):
        return json.loads(self.todo_path.read_text(encoding="utf-8"))["items"][0]["status"]

    def test_close_with_short_id_closes_the_item(self):
        result = self.run_ack("--close", "5c3e7a19")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "ack: bookkeeping done")
        self.assertEqual(self.status(), "done")

    def test_failed_close_is_loud_and_exits_nonzero(self):
        result = self.run_ack("--close", "deadbeef")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("ack: FAILED 1 bookkeeping step(s): close deadbeef"), last)
        self.assertIn("Item not found: deadbeef", last)
        self.assertNotIn("bookkeeping done", result.stdout)
        self.assertIn("ack: FAILED", result.stderr)
        payload = json.loads(result.stdout[: result.stdout.rindex("}") + 1])
        self.assertFalse(payload["bookkeeping_ok"])
        self.assertEqual(self.status(), "open")

    def test_one_failure_among_closes_still_closes_the_good_one(self):
        result = self.run_ack("--close", "deadbeef", "--close", "5c3e7a19")
        self.assertEqual(result.returncode, 1)
        self.assertIn("close deadbeef", result.stdout.strip().splitlines()[-1])
        self.assertNotIn("close 5c3e7a19 (", result.stdout.strip().splitlines()[-1])
        self.assertEqual(self.status(), "done")


if __name__ == "__main__":
    unittest.main()
