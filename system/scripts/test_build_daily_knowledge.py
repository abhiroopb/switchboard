"""build-daily-knowledge.py: temporal rules, snapshot shape, and the query path."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("build_daily_knowledge", HERE / "build-daily-knowledge.py")
bdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bdk)

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
FRESH = bdk.DEFAULT_FRESHNESS


class StatusTests(unittest.TestCase):
    def status(self, cls, days_ago, lifecycle=None):
        rec = {"source_class": cls, "event_time": bdk.iso(NOW - timedelta(days=days_ago)), "lifecycle": lifecycle}
        return bdk.resolve_status(rec, NOW, FRESH)

    def test_windows(self):
        self.assertEqual(self.status("calendar", 1), "current")
        self.assertEqual(self.status("calendar", 5), "historical")
        self.assertEqual(self.status("pm_os_output", 5), "current")
        self.assertEqual(self.status("pm_os_output", 20), "historical")
        self.assertEqual(self.status("calendar", -2), "upcoming")
        self.assertEqual(self.status("todo", 1, "completed"), "completed")

    def test_parse_when_naive_is_local(self):
        self.assertEqual(bdk.parse_when("2026-11-07T12:00:00"), datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc))
        self.assertIsNone(bdk.parse_when("not a date"))


class BuildTests(unittest.TestCase):
    def test_build_and_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "system" / "state").mkdir(parents=True)
            (root / "system" / "state" / "todo.json").write_text(json.dumps({"items": [
                {"id": "a1", "title": "Ship it", "priority": "P1", "status": "open",
                 "created_at": "2026-11-06T10:00:00Z", "workstream": "demo"}]}))
            ws = root / "workstreams" / "demo"
            ws.mkdir(parents=True)
            (ws / "CONTEXT.md").write_text("# Demo\n")
            (ws / "config.yaml").write_text("name: Demo\nstatus: active\nlast_worked_on: '2026-11-07'\n")
            (ws / "2026-11-07-plan.md").write_text("# The plan\n")
            (ws / "inbox").mkdir()
            (ws / "inbox" / "2026-11-07-raw.md").write_text("# raw\n")
            kdir = root / "system" / "state" / "knowledge"
            cov = bdk.build(root, kdir, calendar=False, now=NOW)
            self.assertEqual(cov["missing_source_classes"], [])
            snap = json.loads((kdir / "snapshot.json").read_text())
            self.assertEqual(snap["version"], 1)
            self.assertEqual(snap["coverage"]["records_total"], len(snap["records"]))
            topics = {r["topic"] for r in snap["records"]}
            self.assertIn("P1 Ship it", topics)
            self.assertIn("The plan", topics)
            self.assertNotIn("raw", topics)  # inbox captures are not outputs
            out = subprocess.run([sys.executable, str(HERE / "query-knowledge.py"), "--knowledge-dir", str(kdir),
                                  "--repo-root", str(root), "--workstream", "demo", "--format", "json",
                                  "--as-of", "2026-11-07T20:00:00Z"],
                                 capture_output=True, text=True, check=True)
            current = {r["topic"] for r in json.loads(out.stdout)["current"]}
            self.assertIn("The plan", current)

    def test_failed_source_is_missing_not_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # no todo.json: the todo read fails
            cov = bdk.build(root, root / "k", calendar=False, now=NOW)
            self.assertIn("todo", cov["missing_source_classes"])


if __name__ == "__main__":
    unittest.main()
