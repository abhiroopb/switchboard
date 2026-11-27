import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import os

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"  # todo_cli writes only temp files and makes no calendar call


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TODO_CLI = REPO_ROOT / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"


class TodoArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.todo_path = root / "todo.json"
        self.archive_dir = root / "todo-archive"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_items(self, items):
        self.todo_path.write_text(json.dumps({"version": 2, "items": items}) + "\n")

    def run_cli(self, *args, check=True):
        result = subprocess.run(
            [
                sys.executable,
                str(TODO_CLI),
                "--path",
                str(self.todo_path),
                "--archive-dir",
                str(self.archive_dir),
                *args,
            ],
            text=True,
            capture_output=True,
            check=check,
        )
        return result, json.loads(result.stdout) if result.stdout else None

    def archive_file(self):
        paths = list(self.archive_dir.glob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_archive_done_exports_verbatim_before_removal_and_restores(self):
        old = {
            "id": "old-done",
            "title": "Completed work",
            "priority": "P2",
            "status": "done",
            "completed_at": "2020-01-01T00:00:00Z",
            "context": "Preserve this exact payload.",
        }
        fresh = {
            "id": "active",
            "title": "Active work",
            "priority": "P2",
            "status": "open",
            "created_at": "2020-01-01T00:00:00Z",
        }
        self.write_items([old, fresh])

        _, result = self.run_cli("archive-done", "--days", "7")

        self.assertEqual(result["archived"], 1)
        self.assertEqual(json.loads(self.todo_path.read_text())["items"], [fresh])
        archive = json.loads(self.archive_file().read_text())
        self.assertEqual(archive["records"][0]["item"], old)

        _, restored = self.run_cli("restore-archived", "--id", "old-done")
        self.assertTrue(restored["restored"])
        self.assertEqual(json.loads(self.todo_path.read_text())["items"], [fresh, old])
        self.assertEqual(json.loads(self.archive_file().read_text())["records"], [])

    def test_archive_done_repairs_exported_but_not_removed_partial_state(self):
        item = {
            "id": "partial",
            "title": "Partial archive",
            "priority": "P2",
            "status": "done",
            "completed_at": "2020-01-01T00:00:00Z",
        }
        self.write_items([item])
        self.archive_dir.mkdir()
        from datetime import datetime, timezone

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        archive_path = self.archive_dir / f"{month}.json"
        archive_path.write_text(json.dumps({
            "version": 1,
            "month": month,
            "records": [{"todo_id": "partial", "archived_at": "2000-01-01T00:00:00Z", "reason": "test", "item": item}],
        }))

        current_result, payload = self.run_cli("archive-done", "--days", "7")

        self.assertEqual(current_result.returncode, 0)
        self.assertEqual(payload["archived"], 1)
        self.assertEqual(json.loads(self.todo_path.read_text())["items"], [])
        current_archive = json.loads(pathlib.Path(payload["archive"]).read_text())
        self.assertEqual([row["todo_id"] for row in current_archive["records"]], ["partial"])

    def test_archive_keeps_items_with_unresolved_calendar_holds(self):
        for command, status in (("archive-done", "done"), ("archive-stale", "open")):
            with self.subTest(command=command):
                item = {
                    "id": f"calendar-bearing-{status}", "title": "Keep calendar recovery identity",
                    "priority": "P1", "status": status, "calendar_event_id": "known-event",
                    "created_at": "2020-01-01T00:00:00Z", "completed_at": "2020-01-02T00:00:00Z",
                }
                self.write_items([item])
                extra = ["--priority", "P1"] if command == "archive-stale" else []
                _, result = self.run_cli(command, *extra)
                self.assertEqual(result["archived"], 0)
                self.assertIn("calendar", result["conflicts"][0]["reason"])
                self.assertEqual(json.loads(self.todo_path.read_text())["items"], [item])
                self.assertEqual(json.loads(self.archive_file().read_text())["records"], [])

    def test_archive_stale_respects_priority_recency_and_batch_size(self):
        items = [
            {"id": "stale-p2", "title": "Old P2", "priority": "P2", "status": "open", "created_at": "2020-01-01T00:00:00Z"},
            {"id": "stale-p3", "title": "Old P3", "priority": "P3", "status": "waiting", "created_at": "2020-01-02T00:00:00Z"},
            {"id": "stale-p1", "title": "Old P1", "priority": "P1", "status": "open", "created_at": "2020-01-01T00:00:00Z"},
            {"id": "planned-p2", "title": "Future P2", "priority": "P2", "status": "scheduled", "created_at": "2020-01-01T00:00:00Z", "review_on": "2999-01-01"},
        ]
        self.write_items(items)

        _, first = self.run_cli("archive-stale", "--days", "14", "--batch-size", "1")
        self.assertEqual(first["archived"], 1)
        self.assertEqual(first["items"][0]["id"], "stale-p2")

        _, second = self.run_cli("archive-stale", "--days", "14", "--batch-size", "25")
        self.assertEqual(second["archived"], 1)
        self.assertEqual(second["items"][0]["id"], "stale-p3")
        remaining = {item["id"] for item in json.loads(self.todo_path.read_text())["items"]}
        self.assertEqual(remaining, {"stale-p1", "planned-p2"})

    def test_archive_conflict_keeps_live_todo(self):
        item = {
            "id": "conflict",
            "title": "Current content",
            "priority": "P2",
            "status": "done",
            "completed_at": "2020-01-01T00:00:00Z",
        }
        self.write_items([item])
        self.archive_dir.mkdir()
        # The command always uses the current UTC month.
        from datetime import datetime, timezone

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        (self.archive_dir / f"{month}.json").write_text(json.dumps({
            "version": 1,
            "month": month,
            "records": [{"todo_id": "conflict", "item": {**item, "title": "Older content"}}],
        }))

        _, result = self.run_cli("archive-done", "--days", "7")

        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["conflicts"][0]["id"], "conflict")
        self.assertEqual(json.loads(self.todo_path.read_text())["items"], [item])


if __name__ == "__main__":
    unittest.main()
