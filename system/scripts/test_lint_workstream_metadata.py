#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("lint-workstream-metadata.py")
SPEC = importlib.util.spec_from_file_location("lint_workstream_metadata", SCRIPT_PATH)
assert SPEC and SPEC.loader
lint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lint
SPEC.loader.exec_module(lint)


class WorkstreamMetadataLintTests(unittest.TestCase):
    def write_config(self, root: Path, slug: str, body: str) -> None:
        path = root / "workstreams" / slug / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_accepts_complete_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_config(
                root,
                "healthy",
                "description: Track a clear outcome.\nstatus: needs review\nlast_worked_on: '2026-08-20'\nnext_step: Review the result.\n",
            )

            checked, findings = lint.lint_workstreams(root, today=date(2026, 8, 27))

            self.assertEqual(checked, 1)
            self.assertEqual(findings, [])

    def test_reports_placeholder_invalid_status_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_config(
                root,
                "broken",
                "description: Created on demand; pending refinement.\nstatus: mystery\nlast_worked_on: soon\nnext_step: Clarify the concrete deliverable.\n",
            )

            _, findings = lint.lint_workstreams(root, today=date(2026, 8, 27))

            self.assertEqual(
                {(finding.field, finding.message) for finding in findings},
                {
                    ("description", "description still contains placeholder text"),
                    ("status", "unsupported status 'mystery'"),
                    ("last_worked_on", "invalid ISO date 'soon'"),
                    ("next_step", "next step still contains placeholder text"),
                },
            )

    def test_unquoted_colon_space_in_next_step_is_reported_not_fatal(self) -> None:
        # An unquoted "... non-urgent: watch ..." value raises a ScannerError,
        # which must not abort rebuild-megadesk-state.py for every workstream.
        from workstream_metadata import load_workstream_metadata

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            body = (
                "name: Backup Kit\nemoji: 💾\nauto_open: false\npriority: medium\nstatus: active\n"
                "last_worked_on: '2026-09-26'\n"
                "next_step: Weekly LaunchAgent live (Sun 02:30, keep 4); Remaining, all the owner-owned and "
                "non-urgent: watch the first Sunday run (logs/weekly-*.log + notification)\n"
                "estimate_min: 45\ntodo_match_terms:\n  - mac backup\n  - time machine\n"
                "description: Owns macOS backup + reinstall kit.\n"
                "created_on_demand:\n  captured_at: 2026-09-26 18:10\n  ask: Create a separate workstream.\n"
            )
            self.write_config(root, "backup-kit", body)
            (root / "workstreams" / "backup-kit" / "CONTEXT.md").write_text("# Backup Kit\n")
            self.write_config(
                root, "healthy",
                "description: Track a clear outcome.\nstatus: needs review\nlast_worked_on: '2026-09-26'\nnext_step: Review the result.\n",
            )
            (root / "workstreams" / "healthy" / "CONTEXT.md").write_text("# Healthy\n")

            with redirect_stderr(io.StringIO()) as captured:
                _, findings = lint.lint_workstreams(root, today=date(2026, 9, 27))
                items = load_workstream_metadata(root)

            config_findings = [f for f in findings if f.workstream == "backup-kit" and f.field == "config"]
            self.assertEqual(len(config_findings), 1)
            self.assertIn("line 7 column", config_findings[0].message)
            self.assertIn("quote free text", config_findings[0].message)
            self.assertFalse([f for f in findings if f.workstream == "healthy"])
            self.assertIn("invalid YAML", captured.getvalue())

            # The rebuild still sees both workstreams, with the broken file's
            # flat fields intact (the colon text is kept verbatim).
            by_slug = {item["slug"]: item for item in items}
            self.assertEqual(set(by_slug), {"backup-kit", "healthy"})
            broken = by_slug["backup-kit"]
            self.assertEqual(broken["status"], "active")
            self.assertTrue(broken["next_step"].startswith("Weekly LaunchAgent live (Sun 02:30, keep 4); "))
            self.assertIn("non-urgent: watch the first Sunday run", broken["next_step"])
            self.assertEqual(broken["last_worked_on"], "2026-09-26")
            self.assertEqual(broken["display_name"], "Backup Kit")

    def test_flat_recovery_keeps_scalars_blocks_and_lists(self) -> None:
        from workstream_metadata import recover_flat_yaml

        data = recover_flat_yaml(
            "name: X\nflag: true\ncount: 3\nnothing: ~\nquoted: 'a: b'\n"
            "startup_instruction: |\n  Read this file: now.\n  Then act.\n\n"
            "todo_match_terms:\n  - one\n  - \"two: three\"\n"
            "nested:\n  key: value\n"
            "bad: a: b\n# comment: here\ntrailing: value # not: stripped\n"
        )
        self.assertEqual(data["name"], "X")
        self.assertIs(data["flag"], True)
        self.assertEqual(data["count"], 3)
        self.assertIsNone(data["nothing"])
        self.assertEqual(data["quoted"], "a: b")
        self.assertEqual(data["startup_instruction"], "Read this file: now.\nThen act.")
        self.assertEqual(data["todo_match_terms"], ["one", "two: three"])
        self.assertNotIn("nested", data)
        self.assertEqual(data["bad"], "a: b")
        self.assertNotIn("# comment", data)
        self.assertEqual(data["trailing"], "value # not: stripped")

    def test_default_is_non_blocking_and_strict_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_config(root, "incomplete", "description: ''\n")

            with redirect_stdout(io.StringIO()):
                default_result = lint.main(["--repo-root", str(root)])
                strict_result = lint.main(["--repo-root", str(root), "--strict"])

            self.assertEqual(default_result, 0)
            self.assertEqual(strict_result, 1)


if __name__ == "__main__":
    unittest.main()
