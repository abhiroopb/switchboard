#!/usr/bin/env python3
"""Only an Amp row gets an ampcode.com link; a Claude row names its session.

A Claude Code session id was being written as https://ampcode.com/threads/<id>, a page
that does not exist (2026-11-08). switchboard_agent.thread_pointer is the one decision, and
the sweep at the bottom fails on any new emitter outside the allowlist.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"
import switchboard_agent  # noqa: E402
import report_quiet  # noqa: E402
import review_surface  # noqa: E402

SESSION = "00000000-0000-4000-8000-0000000000d2"
AMP = "T-019a0000-0000-4000-8000-000000000000"
RESUME = f"claude --resume {SESSION}"
AMP_URL = f"https://ampcode.com/threads/{AMP}"
DEAD_URL = f"https://ampcode.com/threads/{SESSION}"


class ThreadPointer(unittest.TestCase):
    def test_the_row_agent_decides(self) -> None:
        self.assertEqual(switchboard_agent.thread_pointer(AMP, "amp"), AMP_URL)
        self.assertEqual(switchboard_agent.thread_pointer(SESSION, "claude"), RESUME)
        self.assertEqual(switchboard_agent.thread_pointer("sess-1", "claude"), "claude --resume sess-1")

    def test_a_row_with_no_agent_falls_back_to_the_id_shape(self) -> None:
        self.assertEqual(switchboard_agent.thread_pointer(AMP), AMP_URL)
        self.assertEqual(switchboard_agent.thread_pointer(SESSION), RESUME)

    def test_a_mismatch_or_nothing_gets_no_pointer(self) -> None:
        self.assertEqual(switchboard_agent.thread_pointer(SESSION, "amp"), "")
        self.assertEqual(switchboard_agent.thread_pointer(AMP, "claude"), "")
        self.assertEqual(switchboard_agent.thread_pointer("not-an-id"), "")
        self.assertEqual(switchboard_agent.thread_pointer("", "claude"), "")
        self.assertEqual(switchboard_agent.thread_pointer(None), "")

    def test_fix_rewrites_session_links_and_leaves_amp_links(self) -> None:
        self.assertEqual(switchboard_agent.fix_thread_links(f"done {DEAD_URL} ok"), f"done {RESUME} ok")
        self.assertEqual(switchboard_agent.fix_thread_links(f"done ({DEAD_URL})"), f"done ({RESUME})")
        self.assertEqual(switchboard_agent.fix_thread_links(f"[Open link]({DEAD_URL})"), RESUME)
        self.assertEqual(switchboard_agent.fix_thread_links(f"done {AMP_URL}"), f"done {AMP_URL}")

    def test_fix_never_eats_the_prose_before_a_link(self) -> None:
        text = f"Range is [1, 5) checked.\nNext step here. [Open link]({DEAD_URL})"
        self.assertEqual(switchboard_agent.fix_thread_links(text), f"Range is [1, 5) checked.\nNext step here. {RESUME}")
        same_line = f"Range is [1, 5) checked. [Open link]({DEAD_URL})"
        self.assertEqual(switchboard_agent.fix_thread_links(same_line), f"Range is [1, 5) checked. {RESUME}")

    def test_a_fenced_draft_stays_as_written(self) -> None:
        text = review_surface.report_text(f"see [1, 5) done.\n```\nkeep this draft {DEAD_URL}\n```\nand {DEAD_URL}")
        self.assertIn(f"keep this draft {DEAD_URL}", text)
        self.assertIn(f"and {RESUME}", text)
        self.assertIn("see [1, 5) done.", text)

    def test_a_report_cannot_carry_a_dead_link(self) -> None:
        text = review_surface.report_text(f"shipped the fix. {DEAD_URL}")
        self.assertNotIn("ampcode.com", text)
        self.assertIn(RESUME, text)
        self.assertIn(AMP_URL, review_surface.report_text(f"shipped the fix. {AMP_URL}"))

    def test_the_quiet_filter_treats_a_session_pointer_as_frame(self) -> None:
        self.assertEqual(report_quiet.strip_report_frame(f"• Launchpad — ok · {RESUME}"), "Launchpad — ok")
        self.assertEqual(report_quiet.strip_report_frame(f"• Launchpad — ok · Claude session {SESSION}"), "Launchpad — ok")


class ClosedWorkspaceNote(unittest.TestCase):
    def test_a_claude_binding_names_its_session(self) -> None:
        spec = importlib.util.spec_from_file_location("closed_workspaces_under_test", ROOT / "daemon/cmux-closed-workspaces.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            module.CONTEXT_DIR = tmp
            module.write_context_file({"title": "tab", "closed_at": "now", "workspace_id": "abcd"},
                                      {"thread_ids": [AMP, SESSION]})
            note = next(Path(tmp).iterdir()).read_text(encoding="utf-8")
        self.assertIn(f"- Amp thread: {AMP} ({AMP_URL})", note)
        self.assertIn(f"- Claude session: {SESSION} ({RESUME})", note)
        self.assertNotIn(DEAD_URL, note)


class EmitterSweep(unittest.TestCase):
    """A new place that builds an Amp thread link has to justify itself here."""

    ALLOWED = {
        "system/scripts/switchboard_agent.py": "the one helper",
        "system/scripts/dispatch.py": "the Amp branch of the launch prompt",
        "system/scripts/cos_review_feed.py": "every link guarded by startswith('T-')",
        "system/scripts/rotate-cos-thread.sh": "exits unless the agent is amp and the id is T-",
        "system/scripts/cmux-relaunch-workspace.sh": "amp_thread_id requires kind amp and the T- shape",
        "daemon/cmux-closed-workspaces.py": "guarded by the T- shape",
        "system/bin/amp-mem": "guarded by the T- shape",
        "system/amp-plugins/amp-mem.ts": "runs only inside Amp",
        "system/amp-plugins/task-liveness.ts": "runs only inside Amp, on Amp child threads",
        "system/amp-plugins/workspace-report.ts": "guarded by /^T-/",
    }
    NEEDLE = "ampcode.com/threads"
    SUFFIXES = {".py", ".sh", ".ts", ".md", ".yaml", ".yml", ".ps1", ".swift", ""}

    def scan(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for top in ("system", "daemon", "adapters", "routines", ".agents", "profiles/company/overlay/system/prompts"):
            for folder, dirs, files in os.walk(ROOT / top):
                dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", "state", "vendor", "archive")]
                for name in files:
                    path = Path(folder) / name
                    if path.suffix not in self.SUFFIXES or name.startswith("test"):
                        continue
                    try:
                        text = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if self.NEEDLE in text:
                        found[path.relative_to(ROOT).as_posix()] = text.count(self.NEEDLE)
        return found

    def test_no_new_emitter(self) -> None:
        self.assertEqual(sorted(set(self.scan()) - set(self.ALLOWED)), [])

    def test_the_allowlist_has_no_stale_entries(self) -> None:
        self.assertEqual(sorted(set(self.ALLOWED) - set(self.scan())), [])


class BoardCopy(unittest.TestCase):
    """The board's copy action, without a shim: find_row and lane_view are stubbed."""

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location("switchboard_board_under_test", HERE / "mux/switchboard-board.py")
        self.board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.board)
        self.board.lane_view = lambda: {}
        self.board.subprocess = _NoProcess()

    def copied(self, thread: str | None) -> str:
        self.board.find_row = lambda _view, _wid: {"thread": thread}
        return self.board.apply_action({"action": "copy-thread-url", "workspace": "ws-1"})["url"]

    def test_amp_thread_copies_its_link(self) -> None:
        self.assertEqual(self.copied(AMP), AMP_URL)

    def test_claude_session_copies_the_resume_command(self) -> None:
        self.assertEqual(self.copied(SESSION), RESUME)

    def test_an_unknown_id_is_an_error_and_never_a_link(self) -> None:
        with self.assertRaises(self.board.BoardError):
            self.copied("not-an-id")
        with self.assertRaises(self.board.BoardError):
            self.copied(None)


class _NoProcess:
    """No process in this test: the clipboard helper is best effort and is skipped."""

    def run(self, *_args, **_kwargs):
        raise OSError("no process in this test")


if __name__ == "__main__":
    unittest.main()
