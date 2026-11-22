#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TODO_CLI = Path(__file__).resolve().parents[1] / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"
SPEC = importlib.util.spec_from_file_location("todo_cli_surface_gate_test", TODO_CLI)
todo_cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todo_cli
SPEC.loader.exec_module(todo_cli)

REBUILD_SCRIPT = Path(__file__).with_name("rebuild-megadesk-state.py")
REBUILD_SPEC = importlib.util.spec_from_file_location("rebuild_megadesk_surface_gate_test", REBUILD_SCRIPT)
rebuild_megadesk = importlib.util.module_from_spec(REBUILD_SPEC)
sys.modules[REBUILD_SPEC.name] = rebuild_megadesk
REBUILD_SPEC.loader.exec_module(rebuild_megadesk)


def item(item_id: str, **overrides):
    value = {
        "id": item_id,
        "title": f"Task {item_id}",
        "status": "open",
        "priority": "P1",
        "created_at": "2026-08-28T12:00:00Z",
    }
    value.update(overrides)
    return value


class TodoSurfaceGateTests(unittest.TestCase):
    def setUp(self):
        todo_cli.SURFACE_CONFIRMATIONS_BY_ID.clear()
        todo_cli.SURFACE_WOULD_CLOSE_BY_ID.clear()
        todo_cli.SURFACE_GATE_RESULT.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.todo_path = self.root / "todo.json"
        self.flags_path = self.root / "flags.json"
        self.flags_path.write_text('{"flags": []}\n')
        self.flags_patch = mock.patch.object(todo_cli, "TODO_RESOLUTION_FLAGS_PATH", self.flags_path)
        self.flags_patch.start()
        self.addCleanup(self.flags_patch.stop)

    def write_items(self, items):
        self.todo_path.write_text(json.dumps({"items": items}) + "\n")

    def args(self, command="summary", **overrides):
        values = {
            "command": command,
            "path": self.todo_path,
            "limit": 5,
            "dry_run": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def run_gate(self, args=None, slack_results=None, drive_comments=None, artifact_reason=None):
        with (
            mock.patch.object(todo_cli, "slack_surface_results", return_value=slack_results or {}),
            mock.patch.object(todo_cli, "fetch_drive_comments", return_value=drive_comments or []),
            mock.patch.object(todo_cli, "load_doc_comment_gmail_triggers", return_value={}),
            mock.patch.object(todo_cli, "load_local_doc_comment_index", return_value={}),
            mock.patch.object(todo_cli, "load_jsonl_cache", return_value=[]),
            mock.patch.object(todo_cli, "load_linear_inbox_index", return_value={}),
            mock.patch.object(todo_cli, "workstream_artifact_reconciled", return_value=artifact_reason),
        ):
            todo_cli.verify_before_surface(args or self.args())

    def test_canonical_source_ref_preserves_drive_comment_identity(self):
        normal = "https://docs.google.com/document/d/file12345678901234567890/edit?usp=sharing&disco=comment456"
        malformed = "https://docs.google.com/spreadsheets/d/file12345678901234567890/edit?usp=drivesdk?disco=comment789"
        self.assertEqual(
            todo_cli.canonicalize_source_ref(normal),
            "https://docs.google.com/document/d/file12345678901234567890/edit?disco=comment456",
        )
        self.assertEqual(
            todo_cli.canonicalize_source_ref(malformed),
            "https://docs.google.com/spreadsheets/d/file12345678901234567890/edit?disco=comment789",
        )
        self.assertEqual(todo_cli.extract_gmail_message_id("msg:1a0391d2f9dbfc6f"), "1a0391d2f9dbfc6f")
        mapped = todo_cli.google_doc_source_ref(
            {"source_ref": "msg:1a0391d2f9dbfc6f"},
            {"1a0391d2f9dbfc6f": {"source_ref": normal}},
        )
        self.assertEqual(mapped, normal)

    def test_bounded_reconcile_only_scans_requested_ids(self):
        payload = {"items": [item("a", workstream="one"), item("b", workstream="two")]}
        with (
            mock.patch.object(todo_cli, "workspace_state_reconciled", return_value="done") as reconcile,
            mock.patch.object(todo_cli, "load_workspace_updates", return_value={"updates": {}}),
            mock.patch.object(todo_cli, "load_comms_signals_index", return_value={}),
            mock.patch.object(todo_cli, "load_linear_inbox_index", return_value={}),
            mock.patch.object(todo_cli, "load_jsonl_cache", return_value=[]),
        ):
            result = todo_cli.reconcile_items(payload, 14, apply=False, item_ids={"b"})
        self.assertEqual([entry["id"] for entry in result["reconciled"]], ["b"])
        self.assertEqual(reconcile.call_count, 1)

    def test_megadesk_builder_does_not_fall_back_to_raw_todos(self):
        self.write_items([item("stale")])
        with (
            mock.patch.object(rebuild_megadesk, "TODO_CLI_PATH", self.todo_path),
            mock.patch.object(rebuild_megadesk, "cli_json", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "surface verification failed"):
                rebuild_megadesk.load_open_todos(self.todo_path)

    def test_megadesk_refresh_never_starts_full_reconciliation(self):
        root = self.root.resolve()
        script = root / "system" / "scripts" / "rebuild-megadesk-state.py"
        state_dir = root / "system" / "state"
        for write in (False, True):
            with (
                self.subTest(write=write),
                mock.patch.object(rebuild_megadesk, "__file__", str(script)),
                mock.patch.object(sys, "argv", [str(script), "--todo-path", str(self.todo_path)] + (["--write"] if write else [])),
                mock.patch.object(rebuild_megadesk, "build_queue", return_value=({}, {})) as build,
                mock.patch.object(rebuild_megadesk, "append_event"),
                mock.patch("builtins.print"),
            ):
                rebuild_megadesk.main()
                build.assert_called_once()
                self.assertEqual(build.call_args.args, (root, state_dir, self.todo_path))
                self.assertFalse(build.call_args.kwargs.get("persist_sources", False))
                self.assertFalse((state_dir / ".last-auto-reconcile-attempt").exists())

    def test_megadesk_keeps_verified_snapshot_gate(self):
        result = {"items": [], "actionable": []}
        with (
            mock.patch.object(rebuild_megadesk, "TODO_CLI_PATH", TODO_CLI),
            mock.patch.object(rebuild_megadesk, "cli_json", return_value=result) as cli,
        ):
            rebuild_megadesk.load_open_todos(self.todo_path)
        cli.assert_called_once()
        self.assertEqual(cli.call_args.args, ([sys.executable, str(TODO_CLI), "--path", str(self.todo_path), "snapshot"],))
        self.assertTrue(cli.call_args.kwargs["required"])
        self.assertEqual(cli.call_args.kwargs["timeout"], 150)

    def test_megadesk_snapshot_failures_preserve_diagnostics(self):
        cases = (
            (subprocess.TimeoutExpired(["python3"], 45), "timed out after 45"),
            (subprocess.CalledProcessError(1, ["python3"], stderr="LaunchDarkly gate failed: HTTP 404"), "HTTP 404"),
            (FileNotFoundError("python3 not found"), "python3 not found"),
            (subprocess.CompletedProcess([], 0, "", ""), "empty output"),
            (subprocess.CompletedProcess([], 0, "not JSON", ""), "invalid JSON"),
        )
        for outcome, expected in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(rebuild_megadesk, "TODO_CLI_PATH", TODO_CLI),
                mock.patch.object(rebuild_megadesk.subprocess, "run") as run,
            ):
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                with self.assertRaisesRegex(RuntimeError, expected):
                    rebuild_megadesk.load_open_todos(self.todo_path)
                # Optional calendar sources retain their existing fallback behavior.
                self.assertIsNone(rebuild_megadesk.cli_json(["python3", "optional-calendar.py"]))

    def test_slack_reply_and_reaction_auto_close(self):
        for verdict in ("resolved_by_owner", "resolved_by_reaction"):
            with self.subTest(verdict=verdict):
                self.write_items([
                    item(
                        "slack",
                        source="slack",
                        source_ref="https://example.enterprise.slack.com/archives/C123/p1787900000000000",
                    )
                ])
                self.run_gate(slack_results={"slack": {"verdict": verdict, "evidence": {}}})
                saved = json.loads(self.todo_path.read_text())["items"][0]
                self.assertEqual(saved["status"], "done")

    def test_teammate_reply_requires_confirmation_not_action(self):
        self.write_items([
            item(
                "slack",
                source="slack",
                source_ref="https://example.enterprise.slack.com/archives/C123/p1787900000000000",
            )
        ])
        self.run_gate(
            slack_results={
                "slack": {
                    "verdict": "teammate_replied",
                    "evidence": {"closer_user": "teammate", "closer_permalink": "https://example/reply"},
                }
            }
        )
        classified = todo_cli.classify_items(json.loads(self.todo_path.read_text())["items"])
        self.assertEqual(classified["actionable"], [])
        self.assertEqual(classified["completion_confirmations"][0]["id"], "slack")
        self.assertIn("todo_cli.py complete --id slack", classified["completion_confirmations"][0]["confirmation_action"])

    def test_exact_drive_comment_resolved_or_self_reply_auto_closes(self):
        source_ref = "https://docs.google.com/document/d/file12345678901234567890/edit?disco=comment456"
        fixtures = (
            [{"id": "comment456", "resolved": True, "replies": []}],
            [{
                "id": "comment456",
                "resolved": False,
                "replies": [{
                    "createdTime": "2026-08-28T12:05:00Z",
                    "author": {"me": True, "displayName": "the owner"},
                }],
            }],
        )
        for drive_comments in fixtures:
            with self.subTest(drive_comments=drive_comments):
                self.write_items([item("doc", source="google-docs", source_ref=source_ref)])
                self.run_gate(drive_comments=drive_comments)
                self.assertEqual(json.loads(self.todo_path.read_text())["items"][0]["status"], "done")

    def test_doc_update_is_suggestive_and_excluded_from_actionable(self):
        self.write_items([
            item(
                "doc",
                source="google-docs",
                source_ref="https://docs.google.com/document/d/file12345678901234567890/edit?disco=comment456",
            )
        ])
        self.flags_path.write_text(json.dumps({
            "flags": [{
                "todo_id": "doc",
                "source_ref": "https://docs.google.com/document/d/file12345678901234567890/edit?disco=comment456",
                "signals": [{"type": "doc_updated", "detail": "Source doc changed after todo creation"}],
            }]
        }))
        self.run_gate()
        classified = todo_cli.classify_items(json.loads(self.todo_path.read_text())["items"])
        self.assertEqual(classified["actionable"], [])
        self.assertEqual(classified["summary"]["confirmation"], 1)
        self.assertEqual(classified["completion_confirmations"][0]["bucket"], "confirmation")

    def test_closed_pull_request_auto_closes(self):
        self.write_items([
            item("pr", source="github", source_ref="https://github.com/acme/example/pull/42")
        ])
        with mock.patch.object(todo_cli, "github_inbox_reconciled", return_value="GitHub PR is merged"):
            self.run_gate()
        self.assertEqual(json.loads(self.todo_path.read_text())["items"][0]["status"], "done")

    def test_completed_meeting_auto_closes(self):
        self.write_items([item("meeting", source="meeting", title="Discuss roadmap")])
        with (
            mock.patch.object(todo_cli, "calendar_api_list_events", return_value=[{"id": "event"}]),
            mock.patch.object(todo_cli, "calendar_reconciled", return_value="attended roadmap meeting"),
        ):
            self.run_gate()
        self.assertEqual(json.loads(self.todo_path.read_text())["items"][0]["status"], "done")

    def test_matching_workstream_artifact_auto_closes(self):
        self.write_items([item("artifact", title="Draft beta launch brief", workstream="beta")])
        self.run_gate(artifact_reason="brief exists")
        self.assertEqual(json.loads(self.todo_path.read_text())["items"][0]["status"], "done")


class DisplayTitleTests(unittest.TestCase):
    """To-do surfaces lead with the PR's name; the number follows in parentheses."""

    URL = "https://github.com/acme/example-repo/pull/10001"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache = Path(self.temp_dir.name) / "named-refs-cache.json"
        self.env = mock.patch.dict(
            "os.environ",
            {"NAMED_REFS_OFFLINE": "1", "NAMED_REFS_CACHE": str(self.cache), "TODO_NAMED_REFS_MAX_FETCH": "0"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        # Load named_refs from this checkout, not from ~/Development/pm-os.
        root_patch = mock.patch.object(todo_cli, "default_repo_root", return_value=Path(__file__).resolve().parents[2])
        root_patch.start()
        self.addCleanup(root_patch.stop)
        todo_cli._NAMED_REFS_MODULE = None
        self.addCleanup(setattr, todo_cli, "_NAMED_REFS_MODULE", None)

    def seed(self, titles):
        self.cache.write_text(json.dumps({url: {"title": t, "at": 4102444800} for url, t in titles.items()}))

    def test_cached_pr_title_leads_and_the_number_follows(self):
        self.seed({self.URL: "Docs search skill"})
        self.assertEqual(
            todo_cli.display_title({"title": "Merge example-repo#10001"}),
            "Merge Docs search skill (example-repo#10001)",
        )

    def test_untitled_pr_is_named_by_repo_and_a_plain_title_is_unchanged(self):
        self.seed({})
        self.assertEqual(
            todo_cli.display_title({"title": "Merge example-repo#10001"}),
            "Merge the example-repo PR (example-repo#10001)",
        )
        self.assertEqual(todo_cli.display_title({"title": "Reply to Reese about the demo"}), "Reply to Reese about the demo")
        self.assertEqual(todo_cli.display_title({}), "")


if __name__ == "__main__":
    unittest.main()
