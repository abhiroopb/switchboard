#!/usr/bin/env python3
"""The CoS queue ranks real work, never scaffold text (2026-11-08, rt-00000000003c).

now.json's top card was switchboard-maintenance "Complete and verify the initial request", left from an
E2E smoke test, and the meetings card asked the owner to verify automation that never ran on this
install. Each test names the wrong implementation it catches.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("rebuild_switchboard_next_step_test", SCRIPTS / "rebuild-switchboard-state.py")
switchboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = switchboard
SPEC.loader.exec_module(switchboard)

from workstream_metadata import NO_OPEN_STEP, load_workstream_metadata, parse_context  # noqa: E402

SCAFFOLD_CONFIG = (
    "name: {name}\nstatus: active\npriority: medium\nqueue_score_bonus: 5\nlast_worked_on: '2026-11-07'\n"
    "next_step: Complete and verify the initial request.\nestimate_min: 30\n"
    "description: 'Owns durable follow-through for this originating request: Workstream: x E2E smoke test: "
    "write the single line e2e ok, then report done.'\n"
)
SCAFFOLD_STEPS = "1. Complete and verify the initial request.\n2. Record decisions, dependencies, and follow-through here.\n"
REAL_STEP = "Dry-run the export once. Time the three steps."
REAL_STEPS = "1. Complete and verify the initial request.\n2. **Dry-run the export once.** Time the three steps.\n3. Ask the reviewer.\n"


class Tree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def workstream(self, slug: str, steps: str, config: str | None = None) -> None:
        folder = self.root / "workstreams" / slug
        folder.mkdir(parents=True)
        (folder / "CONTEXT.md").write_text(f"# {slug}\n\n## Status: Active\n\n## Open / Next Steps\n{steps}\n## Sources\n- none\n", encoding="utf-8")
        (folder / "config.yaml").write_text(config if config is not None else SCAFFOLD_CONFIG.format(name=slug), encoding="utf-8")

    def item(self, slug: str) -> dict:
        return next(i for i in load_workstream_metadata(self.root) if i["slug"] == slug)


class NextStepComesFromRealOpenSteps(Tree):
    def test_scaffold_config_gives_way_to_the_real_step_in_context(self) -> None:
        # Wrong impl: config next_step always wins, so a real workstream kept the placeholder.
        self.workstream("demo-prep", REAL_STEPS)
        item = self.item("demo-prep")
        self.assertEqual(item["next_step"], REAL_STEP, "a card title carries no markdown")
        self.assertTrue(item["has_open_step"])

    def test_only_scaffold_text_means_no_open_step(self) -> None:
        # Wrong impl: fall back to the description, which quotes the smoke test ask again.
        self.workstream("maintenance", SCAFFOLD_STEPS)
        item = self.item("maintenance")
        self.assertEqual(item["next_step"], NO_OPEN_STEP)
        self.assertFalse(item["has_open_step"])

    def test_a_written_config_step_still_wins(self) -> None:
        # Mac and hand-kept workstreams: an explicit next_step keeps its place ahead of CONTEXT.md.
        self.workstream("handkept", REAL_STEPS, "name: handkept\nstatus: active\nnext_step: Ship the pricing note.\n")
        self.assertEqual(self.item("handkept")["next_step"], "Ship the pricing note.")

    def test_a_real_description_is_still_a_fallback(self) -> None:
        self.workstream("described", "", "name: described\nstatus: active\ndescription: Draft the launch memo. Then send it.\n")
        item = self.item("described")
        self.assertEqual(item["next_step"], "Draft the launch memo.")
        self.assertTrue(item["has_open_step"])

    def test_parse_context_skips_done_and_scaffold_lines(self) -> None:
        self.workstream("mixed", "- [x] finished\n1. Complete and verify the initial request.\n- [ ] Call the vendor.\n")
        self.assertEqual(parse_context(self.root / "workstreams" / "mixed" / "CONTEXT.md")["next_step"], "Call the vendor.")


class QueueRanksRealWork(Tree):
    def build(self, todos: list[dict] | None = None, comms: list[dict] | None = None, updates: dict | None = None) -> tuple[dict, dict]:
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        derived = {
            "workstreams": load_workstream_metadata(self.root), "todos": todos or [], "launch_todos": [],
            "updates": updates or {}, "comms": {"candidates": comms or []},
            "meetings": {"updated_at": None, "items": []}, "knowledge": {},
        }
        with (
            mock.patch.object(switchboard, "derive_sources", return_value=derived),
            mock.patch.object(switchboard, "build_calendar_context", return_value={"available_focus_minutes": None, "minutes_until_next_event": None, "upcoming": []}),
            mock.patch.object(switchboard.comms_triage_setting, "enabled", return_value=False),
        ):
            return switchboard.build_queue(self.root, state, self.root / "todo.json", persist_sources=False)

    def test_a_scaffold_workstream_never_becomes_the_now_card(self) -> None:
        # Wrong impl: the 2026-11-08 queue, with the smoke test placeholder on top at score 95.
        self.workstream("maintenance", SCAFFOLD_STEPS)
        self.workstream("demo-prep", REAL_STEPS)
        queue, now = self.build()
        self.assertEqual(now["now"]["workspace"], "demo-prep")
        self.assertIn("Dry-run the export once", now["now"]["title"])
        self.assertEqual([f["workspace"] for f in now["fallbacks"]], [], "the meetings card and the empty workstream rank nothing")
        self.assertEqual(now["queue_size"], 1)
        titles = " ".join(card["title"] for card in queue["queue"])
        self.assertNotIn("Complete and verify the initial request", titles)
        card = next(c for c in queue["queue"] if c["workspace"] == "maintenance")
        self.assertEqual(card["score"], 8, "it stays visible at the bottom")
        self.assertFalse(card["requires_user"])

    def test_no_real_step_at_all_leaves_now_empty(self) -> None:
        self.workstream("maintenance", SCAFFOLD_STEPS)
        _, now = self.build()
        self.assertIsNone(now["now"])
        self.assertEqual(now["attention"], "low")

    def test_an_open_todo_becomes_the_step(self) -> None:
        # Wrong impl: hide a workstream that has a real open to-do because CONTEXT.md is bare.
        self.workstream("maintenance", SCAFFOLD_STEPS)
        todo = {"id": "t1", "title": "Renew the maintenance contract", "priority": "P1", "status": "open", "workstream": "maintenance",
                "requires_user": True}
        with mock.patch.object(switchboard, "todo_matches_workstream", return_value=True):
            _, now = self.build(todos=[todo])
        self.assertEqual(now["now"]["title"], "Renew the maintenance contract")
        self.assertTrue(now["now"]["requires_user"])

    def test_a_real_update_action_counts_and_a_scaffold_one_does_not(self) -> None:
        self.workstream("maintenance", SCAFFOLD_STEPS)
        scaffold = {"workstream:maintenance": {"status": "to review", "recommended_next_action": "Complete and verify the initial request."}}
        self.assertIsNone(self.build(updates=scaffold)[1]["now"])
        real = {"workstream:maintenance": {"status": "to review", "recommended_next_action": "Read the kit note."}}
        self.assertEqual(self.build(updates=real)[1]["now"]["title"], "Read the kit note.")


class MeetingsCardNeverAsksToVerifyAutomation(Tree):
    def card(self, source: dict) -> dict:
        return switchboard.build_meetings_routine_card(self.root, source)

    def test_an_install_with_no_collector_is_quiet(self) -> None:
        # Wrong impl: updated_at null reads as stale, so Windows asks the owner to verify a
        # scheduler it never had.
        card = self.card({"updated_at": None, "items": []})
        self.assertEqual(card["status"], "reference")
        self.assertFalse(card["requires_user"])
        self.assertNotIn("verify", card["title"].lower())

    def test_a_stale_collector_keeps_its_card_without_asking_the_owner(self) -> None:
        # Mac: a collector that stopped still shows and ranks where it did.
        old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        card = self.card({"updated_at": old, "items": [{"collector": "meeting-scheduler", "scheduled_event_ids": [], "recent_log": []}]})
        self.assertEqual((card["status"], card["score"]), ("needs review", 40))
        self.assertIn("Refresh meeting state", card["title"])
        self.assertFalse(card["requires_user"])

    def test_real_meeting_work_still_asks_the_owner(self) -> None:
        # Wrong impl: drop requires_user everywhere, so prep and follow-ups go quiet on macOS.
        fresh = datetime.now(timezone.utc).isoformat()
        prep = self.card({"updated_at": fresh, "items": [{"collector": "meeting-scheduler", "scheduled_event_ids": ["a", "b"], "recent_log": []}]})
        self.assertEqual(prep["status"], "needs action")
        self.assertTrue(prep["requires_user"])
        ingest = self.card({"updated_at": fresh, "items": [{"collector": "post-ingest", "summary": "notes"}]})
        self.assertTrue(ingest["requires_user"])


if __name__ == "__main__":
    unittest.main()
