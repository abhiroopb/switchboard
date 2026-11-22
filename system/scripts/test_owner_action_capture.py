#!/usr/bin/env python3
"""An action a worker hands the owner lands on the to-do list, once, scrubbed.

Every action item the owner must take goes on the to-do list. All text and
values here are invented. Each test names the wrong implementation it catches. The list is the
real todo_cli.py against a temp file with its calendar calls stubbed, so dedupe and completion
are the real ones and nothing reaches a calendar or the real list. Run from system/scripts:

    python3 -m unittest test_owner_action_capture
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REAL_CLI = REPO / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"
_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
ROOT = Path(_TMP.name)
STATE = ROOT / "system" / "state"
STATE.mkdir(parents=True)
os.environ["OWNER_TABS_ROOT"] = str(ROOT)
os.environ["PM_OS_STATE_DIR"] = str(STATE)
os.environ["RUNNING_TASKS_FILE"] = str(STATE / "running-tasks.json")
os.environ["PM_OS_TEST_SANDBOX"] = "1"
os.environ["ASK_COS_DRY_RUN"] = "1"
os.environ.pop("PM_OS_PRIVATE_IDENTIFIERS", None)
os.environ.pop("OWNER_ACTION_CAPTURE_DISABLED", None)
sys.path.insert(0, str(HERE))

WRAPPER = f"""
import importlib.util, os, sys
sys.path.insert(0, {str(HERE)!r})
spec = importlib.util.spec_from_file_location("todo_cli", {str(REAL_CLI)!r})
cli = importlib.util.module_from_spec(spec)
sys.modules["todo_cli"] = cli
spec.loader.exec_module(cli)
cli.sync_calendar_hold_if_needed = lambda item, used_event_ids=None: None
cli.delete_calendar_hold = lambda item: True
cli.main()
"""
STUB = ROOT / "todo_stub.py"
STUB.write_text(WRAPPER, encoding="utf-8")

import ask_cos  # noqa: E402
import cos_review_asks as asks  # noqa: E402
import owner_action_capture as capture  # noqa: E402
import owner_tabs  # noqa: E402
import private_identifiers  # noqa: E402

TASK = "rt-invented01"
SLUG = "widget-rename"
PRIVATE_SLUG = "invented-private-review"
TODO = STATE / "todo.json"
LEDGER = STATE / "running-tasks.json"


def write_row(status: str = "active", task: str = TASK, slug: str = SLUG, **extra) -> None:
    LEDGER.write_text(json.dumps({"version": 2, "tasks": [
        {"id": task, "status": status, "workstream": slug, "origin": "cos", **extra}]}), encoding="utf-8")


def items() -> list[dict]:
    return json.loads(TODO.read_text(encoding="utf-8"))["items"]


def open_items() -> list[dict]:
    return [i for i in items() if i.get("status") != "done"]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        TODO.write_text(json.dumps({"items": []}), encoding="utf-8")
        write_row()
        (ROOT / "system" / "config").mkdir(parents=True, exist_ok=True)
        (ROOT / "system" / "config" / "private-workstreams.txt").write_text(PRIVATE_SLUG + "\n", encoding="utf-8")
        for path in (ROOT / "local" / "private-identifiers.txt", ROOT / ".state" / "private-identifiers.seen"):
            path.unlink(missing_ok=True)
        patcher = mock.patch.dict(os.environ, {"OWNER_ACTION_TODO_CLI": str(STUB)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def report(self, summary: str, status: str = "done", task: str = TASK, slug: str = SLUG, needs_ruling: bool = False) -> dict:
        return capture.capture_report(task, slug, status, summary, needs_ruling)


class OwnerGate(unittest.TestCase):
    """Wrong impl: the gate only knows the name, so "the owner decides" reads as a wait on someone
    else, which is how 33 of 39 real Follow-up lines left Needs-you and would have left this list."""

    def test_owner_subject_is_an_owner_action_whatever_the_verb(self) -> None:
        for what in ("the owner decides whether to rename the widget", "the owner reboots the invented test box",
                     "{{OWNER_FIRST}} to run one invented dry run", "you add the invented guard to the removal list",
                     "(1) the owner reads the note", "the owner reboots once CoS says no workers are running"):
            self.assertEqual(asks.followup_owner(what), "owner", what)

    def test_chores_and_waits_on_others_stay_off(self) -> None:
        for what, want in (("worker chore: rerun the plain installer from main", "chore"),
                           ("run install-amp-skills.sh after landing", "chore"),
                           ("waits on the widget worker to fold the steer into its tests", "other"),
                           ("Re-run the eval when Drew replies", "other"),
                           ("the 6 leftover tmp files are harmless and were left in place", "other"),
                           ("the owner can rule on the small optional moves", "other")):
            self.assertEqual(asks.followup_owner(what), want, what)

    def test_the_owner_first_name_comes_from_config(self) -> None:
        # PM_OS_OWNER_FIRST names the owner; unset, the code's built-in default applies.
        import importlib.util
        def load(env: dict) -> object:
            with mock.patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location("oac_under_test", HERE / "owner_action_capture.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules["oac_under_test"] = module   # its dataclasses look the module up
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop("oac_under_test", None)
                return module
        default = load({})
        self.assertEqual(default.OWNER_FIRST, "{{OWNER_FIRST}}")
        # A kit tree holds the {{OWNER_FIRST}} placeholder until the installer renders it; both forms match.
        self.assertTrue(default.MUST.search(f"{default.OWNER_FIRST} must reboot the invented box"))
        self.assertTrue(default.WAITING_HEAD.match(f"waiting on {default.OWNER_FIRST}: pick a vendor"))
        sam = load({"PM_OS_OWNER_FIRST": "Sam"})
        self.assertTrue(sam.MUST.search("Sam must reboot the invented box"))
        self.assertFalse(sam.MUST.search("{{OWNER_FIRST}} must reboot the invented box"))
        nobody = load({"PM_OS_OWNER_FIRST": ""})
        self.assertFalse(nobody.MUST.search("{{OWNER_FIRST}} must reboot the invented box"))
        self.assertTrue(nobody.MUST.search("the owner must reboot the invented box"))

    def test_an_owner_line_gated_on_someone_elses_reply_is_other(self) -> None:
        # The owner acts only after another person replies, so it
        # waits on that person; a gate that names the owner stays the owner's.
        for what in ("the owner reviews the run when Reese pings", "the owner merges it once Drew confirms"):
            self.assertEqual(asks.followup_owner(what), "other", what)
        self.assertEqual(asks.followup_owner("the owner reviews the run once the owner approves"), "owner")
        self.assertEqual(asks.followup_owner("the owner reviews the run when the invented box finishes"), "owner")

    def test_a_chore_waiting_on_the_owners_go_is_still_theirs(self) -> None:
        self.assertEqual(asks.followup_owner("re-run the eval on the owner's go"), "owner")

    def test_a_wait_on_the_owner_is_theirs(self) -> None:
        for what in ("re-run the eval once the owner approves", "waiting on the owner to pick a vendor",
                     "you'll need to reboot the invented box", "Owner must reboot the invented box"):
            self.assertEqual(asks.followup_owner(what), "owner", what)

    def test_shapes_that_only_look_like_the_owner(self) -> None:
        """Wrong impl: the subject match alone, which reads a noun or a description as an action."""
        for what in ("the owner tabs need a reload", "the owner is waiting on Drew", "you don't need to do anything",
                     "Rerun the eval you checked yesterday", "the owner does nothing here", "the owner checked it yesterday",
                     "reload once the owner tab is open", "you should see a green check", "the owner list rebuilds",
                     "redeploy until you hear from Drew"):
            self.assertNotEqual(asks.followup_owner(what), "owner", what)


class Extraction(unittest.TestCase):
    """Wrong impl: anything near the words becomes a to-do, so chores and rule text flood the list."""

    def kinds(self, text: str, **kw) -> list[str]:
        return [a.kind for a in capture.extract_actions(text, **kw)]

    def test_the_two_written_forms(self) -> None:
        got = capture.extract_actions("Done. Follow-up: the owner picks a vendor. The tab map is stale, and you must confirm a new one.")
        self.assertEqual([(a.kind, a.what) for a in got],
                         [("followup", "the owner picks a vendor"), ("must", "you must confirm a new one")])

    def test_a_must_sentence_starts_at_the_subject(self) -> None:
        got = capture.extract_actions("Six tabs are gone now. The owner must confirm a new tab map. Nothing else changed.")
        self.assertEqual([a.what for a in got], ["The owner must confirm a new tab map"])

    def test_a_list_bullet_ends_a_must_sentence(self) -> None:
        """A real report ran on into its next bullet: "Confirm a new tab map. - sms: pass"."""
        got = capture.extract_actions("The owner must confirm a new tab map. - widget: pass. Nothing else changed.")
        self.assertEqual([a.what for a in got], ["The owner must confirm a new tab map"])

    def test_routine_reports_raise_nothing(self) -> None:
        for text in ("Fixed the widget and tests pass.",
                     "Follow-up: worker chore: rerun the installer.",
                     "Follow-up: waits on Drew to reply.",
                     "Follow-up: the owner can rule on the optional moves, and nothing waits on that.",
                     "Follow-up: the owner reads it if they like, and nothing waits on that.",
                     "You must not run this twice.",
                     "If you must rerun it, use the flag.",
                     "Here is what you need to know about the widget.",
                     "To rerun the eval you have to pass --force.",
                     "Fixed the guard so the owner must approve every send.",
                     "If it fails, you must rerun it.",
                     "The rule is: you must say send it first.",
                     "You should be all set.",
                     "You should see a green check."):
            self.assertEqual(self.kinds(text), [], text)

    def test_a_form_named_in_quotes_is_not_an_action(self) -> None:
        for text in ('Capture reads the "Follow-up: the owner picks" form.', 'It also reads the "you must rotate it" form.',
                     "It reads the `you must` form."):
            self.assertEqual(self.kinds(text), [], text)

    def test_a_fenced_draft_is_not_read_as_prose(self) -> None:
        text = "Done. ⏎ ```⏎ Follow-up: the owner picks a vendor. You must reply today. ⏎ ```"
        self.assertEqual(self.kinds(text.replace("```⏎", "``` ⏎")), [])

    def test_a_marked_line_break_ends_a_followup(self) -> None:
        got = capture.extract_actions("Follow-up: the owner signs the invented form ⏎ AGENTS.md ⏎ [Open in Obsidian](x)")
        self.assertEqual(got[0].what, "the owner signs the invented form")

    def test_every_followup_line_counts(self) -> None:
        self.assertEqual(self.kinds("Follow-up: the owner picks a vendor. Follow-up: the owner signs the invented form."), ["followup", "followup"])

    def test_rulings(self) -> None:
        options = "Options: (a) keep it; (b) rename it. Lean: a because it is shorter."
        self.assertEqual(self.kinds("Rename the widget? " + options, status="blocked"), ["ruling"])
        self.assertEqual(self.kinds("worker thread is gone from the tab list.", status="blocked"), [],
                         "a machine-blocked row asks the owner nothing")
        self.assertEqual(self.kinds("waiting on the owner: which name?", status="active", needs_ruling=True), ["waiting"])
        self.assertEqual(self.kinds("Which name?", status="done", needs_ruling=True), ["ruling"])
        self.assertEqual(self.kinds("waiting on the owner: which name?", status="blocked"), ["ruling"])

    def test_softer_wording_of_a_must(self) -> None:
        for text in ("You'll need to reboot the invented box.", "The owner should reboot the invented box.",
                     "- you must reboot the invented box.", "After landing, you must reboot the invented box.",
                     "To finish, you must sign the invented form.", "Now you must reboot the invented box.",
                     "Owner must reboot the invented box."):
            self.assertEqual(self.kinds(text), ["must"], text)

    def test_the_owners_first_name_is_a_subject(self) -> None:
        """The owner's first name is a subject too. The kit carries the name as its install placeholder."""
        self.assertEqual(self.kinds("After landing, {{OWNER_FIRST}} must reboot the invented box."), ["must"])
        self.assertEqual(self.kinds("Waiting on {{OWNER_FIRST}}: which name?", status="blocked"), ["ruling"])

    def test_a_parked_draft_counts_only_with_no_tracker(self) -> None:
        text = "Draft for the owner to send: ⏎ ```Hello Drew``` ⏎ done"
        with mock.patch.object(capture, "TRACKER", ROOT / "absent.py"):
            self.assertEqual(self.kinds(text), ["draft"])
        with mock.patch.object(capture, "TRACKER", STUB):
            self.assertEqual(self.kinds(text), [])


class Titles(unittest.TestCase):
    def test_reads_as_an_instruction(self) -> None:
        for what, want in (("the owner decides whether to rename it", "Decide whether to rename it"),
                           ("the owner must purge the invented file", "Purge the invented file"),
                           ("you need to confirm the map", "Confirm the map"),
                           ("{{OWNER_FIRST}} to reboot the box", "Reboot the box"),
                           ("the owner replies to Dana", "Reply to Dana"),
                           ("the owner still needs to confirm it", "Confirm it"),
                           ("you address the gap", "Address the gap"),
                           ("the owner reboots and makes the dump", "Reboot and make the dump"),
                           ("the owner approves the plan or changes it, and answers the questions",
                            "Approve the plan or change it, and answer the questions"),
                           ("the owner reads the note and replies with answers", "Read the note and reply with answers"),
                           ("the owner reviews docs and tests", "Review docs and tests"),
                           ("you'll need to reboot the box", "Reboot the box"),
                           ("Owner must reboot the box", "Reboot the box")):
            self.assertEqual(capture.imperative(what), want)


class Scrub(Base):
    """Wrong impl: the worker's text goes straight into a title, which a calendar hold and every
    digest then repeat. Every value below is invented."""

    def assert_withheld(self, text: str, marker: str) -> None:
        out = self.report(f"Done. Follow-up: {text}.")
        self.assertTrue(out["captured"][0].get("withheld"), text)
        self.assertEqual(len(open_items()), 1)
        stored = TODO.read_text(encoding="utf-8")
        self.assertNotIn(marker, stored)
        item = open_items()[0]
        self.assertIn(SLUG.split("-")[0].title(), item["title"], "named by topic")
        self.assertNotIn(TASK, item["title"], "names, never bare ids")
        self.assertIn(TASK, item["context"], "the pointer back to the report")
        TODO.write_text(json.dumps({"items": []}), encoding="utf-8")

    def test_shapes_and_words(self) -> None:
        for text, marker in (("the owner calls about checking …47 and savings …12", "…47"),
                             ("the owner moves $4,321 to the invented fund", "4,321"),
                             ("the owner renames the file 123-45-6789", "123-45-6789"),
                             ("the owner rotates ghp_INVENTEDtoken1234", "INVENTEDtoken1234"),
                             ("the owner replaces the password in the invented file", "password"),
                             ("the owner reads the file named 4111111111111111", "4111111111111111"),
                             ("the owner stores client secret hunter2", "hunter2"),
                             ("the owner rotates auth token abc123", "abc123"),
                             ("the owner resets PIN 4821", "4821"),
                             ("the owner closes account 4721", "4721"),
                             ("the owner replaces card ending 4721", "4721"),
                             ("the owner moves 4,321 to the fund", "4,321"),
                             ("the owner rotates sk_live_abc123", "sk_live_abc123"),
                             ("the owner rotates token abc123", "abc123"),
                             ("the owner moves 12k from Examplebank", "12k"),
                             ("the owner renames the invented passport scan", "passport"),
                             ("the owner confirms DOB 1980-04-12", "1980")):
            with self.subTest(text=text):
                self.assert_withheld(text, marker)

    def test_a_withheld_title_keeps_the_verb_and_the_topic(self) -> None:
        """Wrong impl: "Action from the X worker: read its report (rt-…)", which says neither what to do nor about what."""
        self.report("Done. Follow-up: the owner moves $4,321 to the invented fund.")
        self.assertEqual(open_items()[0]["title"], "Move a private item from the Widget Rename report")

    def test_a_money_word_with_no_figure_is_replaced_and_the_action_stays_readable(self) -> None:
        """Wrong impl: one money word hides the whole action, so "download the refund receipt" became "read its report"."""
        for text, marker, want in (("the owner downloads the mid-October refund receipt from the club portal", "refund",
                                    "Download the mid-October private item receipt from the club portal"),
                                   ("the owner checks the gym card balance", "balance", "Check the gym card private item"),
                                   ("the owner decides who can open the club savings jar", "savings",
                                    "Decide who can open the club private item jar"),
                                   ("the owner fixes the dividends dates for the co-op", "dividends",
                                    "Fix the private item dates for the co-op"),
                                   ("the owner signs form 4400 for the gym", "4400", "Sign private item for the gym"),
                                   ("the owner reviews the co-op dividend", "dividend", "Review the co-op private item"),
                                   ("the owner plans the estate sale", "estate", "Plan the private item sale"),
                                   ("the owner files the credit card dispute", "credit card", "File the private item dispute"),
                                   ("the owner signs the ACH form for the gym", "ACH", "Sign the private item form for the gym")):
            with self.subTest(text=text):
                TODO.write_text(json.dumps({"items": []}), encoding="utf-8")
                out = self.report(f"Done. Follow-up: {text}.")
                self.assertEqual(out["captured"][0]["result"], "added", out)
                self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))
                self.assertEqual([i["title"] for i in open_items()], [want])

    def test_clean_text_keeps_its_words(self) -> None:
        for text in ("the owner decides on 2026-11-09 whether to rename the widget",
                     "the owner reviews 2026-11-08-widget-rename-review-note-with-a-long-name.md",
                     "the owner checks the widget PR (12345)", "the owner is checking the deploy"):
            self.assertEqual(capture.sensitive_reasons(text), [], text)

    def test_a_listed_identifier_is_refused_without_ever_being_stored(self) -> None:
        private_identifiers.add_values(ROOT, "street", ["742 Invented Terrace"])
        self.assert_withheld("the owner confirms mail for 742 Invented Terrace", "Invented Terrace")

    def test_an_unreadable_list_fails_closed(self) -> None:
        path = ROOT / "local" / "private-identifiers.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not a valid list\n", encoding="utf-8")
        self.assertEqual(capture.sensitive_reasons("the owner picks a vendor"), ["identifier list unreadable"])

    def test_a_private_workstream_gets_one_generic_title_with_a_pointer(self) -> None:
        marker = "INVENTED-PLAN-MARKER"
        out = self.report(f"Follow-up: the owner picks {marker}. You must read {marker} too.", slug=PRIVATE_SLUG)
        self.assertEqual(len(out["captured"]), 1, "one per task, never one per private sentence")
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))
        item = open_items()[0]
        self.assertIn(f"workstreams/{PRIVATE_SLUG}/", item["context"])
        self.assertEqual(item["title"], "Review the Invented Private Review report and act on it")
        self.assertIn(TASK, item["context"])

    def test_the_ledger_row_outranks_the_workstream_flag(self) -> None:
        """Wrong impl: a worker's wrong --workstream on a private row puts private text in a title."""
        marker = "INVENTED-PLAN-MARKER"
        write_row(slug=PRIVATE_SLUG)
        self.report(f"Follow-up: the owner picks {marker}.", slug=SLUG)
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))
        self.assertEqual(open_items()[0]["workstream"], PRIVATE_SLUG)

    def test_a_private_flag_on_a_public_row_still_withholds(self) -> None:
        marker = "INVENTED-PLAN-MARKER"
        self.report(f"Follow-up: the owner picks {marker}.", slug=PRIVATE_SLUG)
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))

    def test_an_unreadable_ledger_withholds(self) -> None:
        marker = "INVENTED-PLAN-MARKER"
        with mock.patch.object(capture, "row_for", side_effect=OSError("unreadable")):
            self.report(f"Follow-up: the owner picks {marker}.", slug="")
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))

    def test_a_workstream_nobody_can_name_withholds(self) -> None:
        """A corrupt ledger reads as no row, and no flag leaves nothing to check against the private list."""
        marker = "INVENTED-PLAN-MARKER"
        with mock.patch.object(capture, "row_for", return_value=None):
            self.report(f"Follow-up: the owner picks {marker}.", slug="")
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))
        self.assertNotIn("workstreams//", TODO.read_text(encoding="utf-8"))

    def test_a_private_routine_report_raises_nothing(self) -> None:
        self.assertEqual(self.report("Fixed the invented plan.", slug=PRIVATE_SLUG)["captured"], [])
        self.assertEqual(items(), [])

    def test_a_private_question_never_stores_its_text(self) -> None:
        marker = "INVENTED-QUESTION-MARKER"
        write_row(slug=PRIVATE_SLUG)
        ask_cos.main(["ask", "--task-id", TASK, "--option", "keep", "--option", "drop", "--default", "a", f"What about {marker}?"])
        self.assertEqual(len(open_items()), 1)
        self.assertNotIn(marker, TODO.read_text(encoding="utf-8"))


class Capture(Base):
    def test_a_followup_becomes_a_routed_p1_due_next_business_day(self) -> None:
        self.report("Follow-up: the owner picks a vendor.")
        item = open_items()[0]
        self.assertEqual((item["title"], item["priority"], item["workstream"]), ("Pick a vendor", "P1", SLUG))
        self.assertEqual(item["due"], capture.next_business_day())
        self.assertIn(TASK, item["context"])
        self.assertTrue(item["source_ref"].startswith(capture.REF_PREFIX), item["source_ref"])
        self.assertIn(capture.TAG, item["tags"])
        self.assertTrue(item["requires_user"])

    def test_next_business_day_skips_the_weekend(self) -> None:
        tz = ZoneInfo("America/Los_Angeles")
        self.assertEqual(capture.next_business_day(datetime(2026, 11, 13, 9, tzinfo=tz)), "2026-11-16")   # Friday
        self.assertEqual(capture.next_business_day(datetime(2026, 11, 8, 9, tzinfo=tz)), "2026-11-09")    # Sunday

    def test_a_replayed_report_adds_nothing(self) -> None:
        """Wrong impl: every retry of a held report adds a row."""
        for _ in range(3):
            self.report("Follow-up: the owner picks a vendor.")
        self.assertEqual(len(items()), 1)

    def test_a_done_todo_is_not_raised_again_by_a_replay(self) -> None:
        self.report("Follow-up: the owner picks a vendor.")
        capture.close(open_items()[0], "owner did it")
        self.report("Follow-up: the owner picks a vendor.")
        self.assertEqual((len(items()), open_items()), (1, []))

    def test_the_same_ask_from_a_continuation_dedupes_on_the_title(self) -> None:
        """Wrong impl: a continuation repeats its parent's follow-up and the list doubles."""
        self.report("Follow-up: the owner picks a vendor.")
        self.report("Follow-up: the owner picks a vendor.", task="rt-invented02")
        self.assertEqual(len(items()), 1)

    def test_two_withheld_titles_from_two_tasks_both_stay(self) -> None:
        """Wrong impl: generic titles share a stem, so the CLI's title dedupe would lose the second action."""
        self.report("Follow-up: the owner checks the club balance.")
        self.report("Follow-up: the owner checks the club refunds.", task="rt-invented02")
        self.assertEqual(len(open_items()), 2)

    def test_status_active_without_a_ruling_raises_nothing(self) -> None:
        self.assertIn("skipped", self.report("Follow-up: the owner picks a vendor.", status="active"))
        self.assertEqual(items(), [])

    def test_the_kill_switch(self) -> None:
        with mock.patch.dict(os.environ, {"OWNER_ACTION_CAPTURE_DISABLED": "1"}):
            self.assertEqual(self.report("Follow-up: the owner picks a vendor."), {"skipped": "disabled"})
        self.assertEqual(items(), [])

    def test_the_sandbox_never_reaches_the_list_without_a_named_stub(self) -> None:
        """Wrong impl: a report-to-cos test run adds fixture text to the owner's real list."""
        env = {k: v for k, v in os.environ.items() if k != "OWNER_ACTION_TODO_CLI"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(capture.cli_path())
            self.report("Follow-up: the owner picks a vendor.")
        self.assertEqual(items(), [])

    def test_a_failed_add_waits_and_the_tick_retries_it(self) -> None:
        """Wrong impl: a busy list only logs a failure, and a replay skips the hook, so the action is lost."""
        self.addCleanup(shutil.rmtree, capture.pending_dir(), True)
        with mock.patch.dict(os.environ, {"OWNER_ACTION_TODO_CLI": str(ROOT / "absent_cli.py")}):
            out = self.report("Follow-up: the owner picks a vendor.")
        self.assertEqual(out["captured"][0]["result"], "exit 2")
        self.assertEqual(items(), [])
        self.assertEqual(capture.reconcile()["retried"][0]["result"], "added")
        self.assertEqual([i["title"] for i in open_items()], ["Pick a vendor"])
        self.assertEqual(list(capture.pending_dir().glob("*.json")), [])
        self.assertNotIn("retried", capture.reconcile())

    def test_a_pending_withheld_add_stays_scrubbed(self) -> None:
        self.addCleanup(shutil.rmtree, capture.pending_dir(), True)
        with mock.patch.dict(os.environ, {"OWNER_ACTION_TODO_CLI": str(ROOT / "absent_cli.py")}):
            self.report("Follow-up: the owner moves $4,321 to the fund.")
        stored = next(capture.pending_dir().glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("4,321", stored)

    def test_retries_stop_at_the_cap_and_keep_the_file(self) -> None:
        self.addCleanup(shutil.rmtree, capture.pending_dir(), True)
        with mock.patch.dict(os.environ, {"OWNER_ACTION_TODO_CLI": str(ROOT / "absent_cli.py")}):
            self.report("Follow-up: the owner picks a vendor.")
            path = next(capture.pending_dir().glob("*.json"))
            capture.reconcile()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["attempts"], 1)
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "attempts": capture.MAX_PENDING_ATTEMPTS}), encoding="utf-8")
            self.assertIn("gave up", capture.reconcile()["retried"][0]["result"])
        self.assertTrue(path.exists())

    def test_a_naive_review_time_does_not_raise(self) -> None:
        self.report("Follow-up: the owner picks a vendor.")
        (STATE / "cos-review-actions.json").write_text(json.dumps({"items": {f"task:{TASK}": {
            "action": "skip", "scope": "task", "at": "2099-01-01T00:00:00"}}}), encoding="utf-8")
        self.addCleanup((STATE / "cos-review-actions.json").unlink, True)
        capture.sync_task(TASK)
        self.assertEqual(open_items(), [])

    def test_rows_that_are_not_worker_tasks_raise_nothing(self) -> None:
        self.assertEqual(self.report("Follow-up: the owner picks a vendor.", task="wr-invented"), {"skipped": "not a worker task"})
        self.assertEqual(items(), [])


class QuestionsAndClosing(Base):
    def ask(self, *argv: str) -> None:
        ask_cos.main(["ask", "--task-id", TASK, *argv])

    def question(self) -> dict:
        return next(i for i in items() if "/question/" in i["source_ref"])

    def test_a_question_becomes_an_open_todo_with_no_calendar_date(self) -> None:
        """Wrong impl: a question left only in the CoS scrollback, or a hold made for an hour-long ask."""
        self.ask("--option", "keep", "--option", "rename", "--default", "a", "Rename the widget?")
        item = self.question()
        self.assertIn("Rename the widget?", item["title"])
        self.assertEqual((item["priority"], item.get("due")), ("P1", None))
        self.assertIn("keep", item["context"])

    def test_answered_marks_it_done(self) -> None:
        """Fails without the close path: an answered question stays on the list for good."""
        self.ask("Rename the widget?")
        self.assertEqual(ask_cos.main(["answered", "--task-id", TASK, "--answer", "rename it"]), 0)
        self.assertEqual(self.question()["status"], "done")
        self.assertNotIn("rename it", self.question().get("resolution") or "", "the answer stays on the ledger row")

    def test_a_finished_worker_closes_its_unanswered_question(self) -> None:
        """Wrong impl: the owner accepts the default by not answering, and the to-do never closes."""
        self.ask("Rename the widget?")
        write_row(status="done", worker_questions=[{"id": "q1", "answered": False}])
        self.report("Renamed nothing; used the default.")
        self.assertEqual(self.question()["status"], "done")

    def test_a_ruling_from_a_finished_report_survives_the_tick(self) -> None:
        """Wrong impl: the report that asks also ends the row, so the next tick closes the ask unanswered."""
        write_row(status="done")
        self.report("Rename the widget?", status="done", needs_ruling=True)
        self.assertEqual(capture.reconcile()["closed"], [])
        self.assertEqual(len(open_items()), 1)
        capture.sync_task(TASK, steered=True)
        self.assertEqual(open_items(), [])

    def test_a_parked_turn_closes_when_its_task_ends(self) -> None:
        self.report("waiting on the owner: which name?", status="active", needs_ruling=True)
        self.assertEqual(capture.reconcile()["closed"], [])
        write_row(status="done")
        self.assertEqual(len(capture.reconcile()["closed"]), 1)

    def test_an_identical_ask_from_a_continuation_keeps_its_own_todo(self) -> None:
        """Wrong impl: the title dedupe ties the continuation's ask to its parent, and cancelling the parent closes the only copy."""
        self.report("waiting on the owner: which name?", status="active", needs_ruling=True)
        self.report("waiting on the owner: which name?", status="active", needs_ruling=True, task="rt-invented02")
        self.assertEqual(len(open_items()), 2)

    def test_a_steer_closes_a_ruling_but_not_a_followup(self) -> None:
        """Wrong impl: any later steer wipes an action the owner has not done yet."""
        self.report("Rename it? Options: (a) keep; (b) rename. Lean: a because it is shorter. Follow-up: the owner signs the invented form.",
                    status="blocked")
        self.assertEqual(len(open_items()), 2)
        capture.sync_task(TASK)
        self.assertEqual(len(open_items()), 2, "a blocked row still waits on the owner")
        capture.sync_task(TASK, steered=True)
        self.assertEqual([i["title"] for i in open_items()], ["Sign the invented form"])

    def test_dispatching_the_followup_closes_it(self) -> None:
        self.report("Follow-up: the owner signs the invented form.")
        for text in ("use the longer name", "don't dispatch the follow-up yet"):
            self.assertFalse(capture.FOLLOWUP_HANDLED.match(text), text)
        for text in (f"{TASK}: dispatch the follow-up", f"{TASK}: drop the follow-up", "dispatch the follow-up"):
            self.assertTrue(capture.FOLLOWUP_HANDLED.match(text), text)
        capture.sync_task(TASK, followup_handled=True)
        self.assertEqual(open_items(), [])

    def review_record(self, **record) -> None:
        path = STATE / "cos-review-actions.json"
        path.write_text(json.dumps({"items": {f"task:{TASK}": record}}), encoding="utf-8")
        self.addCleanup(path.unlink, True)

    def test_a_task_wide_cos_review_skip_closes_every_kind(self) -> None:
        self.report("Follow-up: the owner signs the invented form.")
        self.review_record(action="skip", scope="task", at="2099-01-01T00:00:00Z")
        capture.sync_task(TASK)
        self.assertEqual(open_items(), [])

    def test_a_click_on_one_card_closes_nothing(self) -> None:
        """Wrong impl: the feed applies a record with no scope to one ask, and this applied it to all of them."""
        self.report("Follow-up: the owner signs the invented form.")
        self.review_record(action="skip", at="2099-01-01T00:00:00Z")
        capture.sync_task(TASK)
        self.assertEqual(len(open_items()), 1)

    def test_an_older_review_record_does_not_close_a_later_action(self) -> None:
        self.report("Follow-up: the owner signs the invented form.")
        self.review_record(action="skip", scope="task", at="2001-01-01T00:00:00Z")
        capture.sync_task(TASK)
        self.assertEqual(len(open_items()), 1)

    def test_reconcile_closes_a_task_that_ended_with_no_report(self) -> None:
        self.ask("Rename the widget?")
        write_row(status="cancelled")
        self.assertEqual(len(capture.reconcile()["closed"]), 1)
        self.assertEqual(open_items(), [])

    def test_reconcile_leaves_hand_added_todos_alone(self) -> None:
        """Wrong impl: the sweep reads every to-do and closes what it cannot match."""
        capture.run_cli("add", "--title", "Invented hand-added item", "--priority", "P2")
        write_row(status="done")
        self.assertEqual(capture.reconcile(), {"open_tasks": 0, "closed": []})
        self.assertEqual(len(open_items()), 1)

    def test_a_question_keeps_working_when_capture_fails(self) -> None:
        with mock.patch.object(capture, "capture_question", side_effect=RuntimeError("boom")):
            self.assertEqual(ask_cos.main(["ask", "--task-id", TASK, "Rename the widget?"]), 0)


class NoiseStaysOff(Base):
    """The list never fills with titles like "Action from the PM OS Maintenance worker: read its
    report (rt-…)", the same browser step three times, or asks CoS has already answered.
    Every report here is invented."""

    def titles(self) -> list[str]:
        return [i["title"] for i in open_items()]

    def write_rows(self, *rows: dict) -> None:
        LEDGER.write_text(json.dumps({"version": 2, "tasks": list(rows)}), encoding="utf-8")

    def test_no_title_carries_a_task_id(self) -> None:
        """Wrong impl: a title that leans on the task id as its pointer, which the names rule forbids."""
        with mock.patch.object(capture, "TRACKER", ROOT / "absent.py"):
            self.report("Done. Follow-up: the owner moves $4,321 to the fund. Draft for the owner to send: ⏎ ```Hello``` ⏎ ok")
        self.report("Rename it? Options: (a) keep; (b) rename. Lean: a because it is shorter.", status="blocked", task="rt-invented02")
        self.report("Follow-up: the owner picks the invented plan.", task="rt-invented03", slug=PRIVATE_SLUG)
        write_row(task="rt-invented04")
        ask_cos.main(["ask", "--task-id", "rt-invented04", "Move $4,321 to the fund?"])
        self.assertEqual(len(open_items()), 5, self.titles())
        for item in open_items():
            self.assertNotRegex(item["title"], r"\brt-\w+", item["title"])

    def test_a_followup_the_report_says_is_on_the_list_adds_nothing(self) -> None:
        """Wrong impl: the worker added the to-do and said so, and capture added it again."""
        for text in ("Follow-up: the owner purges the invented archive. The owner also renames two files. All three are on the to-do list.",
                     'Follow-up: the owner opens the invented app and tells CoS "go". It is on your to-do list as Open the app.',
                     "Follow-up: the owner signs the invented form (already on the to-do list)."):
            with self.subTest(text=text):
                self.assertEqual(self.report(text)["captured"], [])
        self.assertEqual(items(), [])

    def test_the_next_followup_is_not_hidden_by_an_earlier_ones_tail(self) -> None:
        self.report("Follow-up: the owner signs the invented form. Follow-up: the owner picks a vendor. It is on the to-do list.")
        self.assertEqual(self.titles(), ["Sign the invented form"])

    def test_an_action_already_on_the_list_in_other_words_adds_nothing(self) -> None:
        """Wrong impl: only the exact title dedupes, so a worker's wording doubles a hand-added to-do."""
        capture.run_cli("add", "--title", "Send the invented removal request", "--priority", "P1", "--allow-unrouted")
        capture.run_cli("add", "--title", "Open the invented app and click Allow for the final test", "--priority", "P2", "--allow-unrouted")
        out = self.report("Follow-up: the owner needs to say send for the invented removal request after CoS shows it. "
                          'Follow-up: the owner opens the invented app and tells CoS "run the final test", then clicks Allow.')
        self.assertEqual([c["result"] for c in out["captured"]], ["already on the list", "already on the list"])
        self.assertEqual(len(items()), 2)

    def test_different_actions_on_one_topic_both_land(self) -> None:
        capture.run_cli("add", "--title", "Download the invented October receipt", "--priority", "P2", "--allow-unrouted")
        self.report("Follow-up: the owner renames the invented batch folder.")
        self.assertEqual(len(open_items()), 2)

    def test_a_tagged_followup_closes_when_its_question_is_answered(self) -> None:
        """Wrong impl: the follow-up and its question are two to-dos, and answering one leaves the other open."""
        write_row(worker_questions=[{"id": "q2", "answered": False}])
        self.report("Follow-up: the owner names the batch for the 3 invented files (q2).")
        self.assertEqual(self.titles(), ["Name the batch for the 3 invented files"])
        write_row(status="done", worker_questions=[{"id": "q2", "answered": True}])
        capture.sync_task(TASK)
        self.assertEqual(open_items(), [])

    def test_a_tagged_followup_already_answered_is_not_raised(self) -> None:
        write_row(status="done", worker_questions=[{"id": "q1", "answered": True}])
        out = self.report("Follow-up: the owner approves filing the invented stub (q1).")
        self.assertEqual(out["captured"][0]["result"], "already answered in CoS")
        self.assertEqual(items(), [])

    def test_a_flagged_done_report_raises_its_followup_not_its_first_sentence(self) -> None:
        """Wrong impl: "Rule on: Switch stays off: the test waited two minutes", a status line posing as an ask."""
        self.report("Switch stays off: the invented test waited two minutes. Follow-up: the owner opens the invented app.",
                    status="done", needs_ruling=True)
        self.assertEqual(self.titles(), ["Open the invented app"])

    def test_a_ruling_names_the_decision_and_carries_the_options(self) -> None:
        self.report("Invented rerun stopped after 4 of 10 suites. Options: (a) rerun now; (b) wait for the evening. Lean: a because memory is free.",
                    status="blocked")
        item = open_items()[0]
        self.assertEqual(item["title"], "Decide the next step: invented rerun stopped after 4 of 10 suites")
        self.assertIn("(a) rerun now", item["context"])
        self.assertIn("Lean: a", item["context"])

    def test_a_continuation_closes_the_ruling_it_answers(self) -> None:
        """Wrong impl: CoS relays the answer as a new task, and the ruling sits on the list for good."""
        self.report("Rerun stopped. Options: (a) rerun now; (b) wait. Lean: a because it is free.", status="blocked")
        parent = {"id": TASK, "status": "blocked", "workstream": SLUG, "origin": "cos"}
        for child, closes in (({"id": "rt-invented05", "status": "active", "continuation_of": TASK, "supplemental": True}, False),
                              ({"id": "rt-invented06", "status": "cancelled", "continuation_of": TASK}, False),
                              ({"id": "rt-invented07", "status": "queued", "continuation_of": TASK}, True)):
            self.write_rows(parent, {"workstream": SLUG, "origin": "cos", **child})
            capture.sync_task(TASK)
            self.assertEqual(open_items() == [], closes, child["id"])

    def test_a_continuation_leaves_the_owners_own_followup_open(self) -> None:
        self.report("Follow-up: the owner signs the invented form.")
        self.write_rows({"id": TASK, "status": "done", "workstream": SLUG},
                        {"id": "rt-invented07", "status": "active", "workstream": SLUG, "continuation_of": TASK})
        capture.sync_task(TASK)
        self.assertEqual(len(open_items()), 1)

    def test_names_and_then_read_as_verbs(self) -> None:
        self.assertEqual(asks.followup_owner("the owner names the batch for the invented files"), "owner")
        self.assertNotEqual(asks.followup_owner("the owner names list rebuilds"), "owner")
        self.assertEqual(capture.imperative('the owner opens the app and tells CoS "go", then clicks Allow'),
                         'Open the app and tell CoS "go", then click Allow')


class Wiring(unittest.TestCase):
    """The rule lives in the repo-owned surfaces, and the steer path calls the close."""

    def read(self, rel: str) -> str:
        return (REPO / rel).read_text(encoding="utf-8")

    def test_surfaces_carry_the_rule(self) -> None:
        for rel in ("AGENTS.md", "system/prompts/dispatch-core.md", ".agents/skills/chief-of-staff/SKILL.md",
                    "system/skill-overrides/todo/SKILL.md"):
            self.assertIn("owner_action_capture.py", self.read(rel), rel)

    def test_workers_are_told_how_to_word_it(self) -> None:
        self.assertIn("owner_action_capture.py", self.read("system/scripts/dispatch.py").split("def launch_prompt", 1)[1])

    def test_the_hook_does_not_use_nohup(self) -> None:
        """nohup alone dies with the tool call on macOS (report-to-cos.sh, the waiter's own comment)."""
        script = self.read("system/scripts/report-to-cos.sh")
        hook = script.split("owner_action_capture.py\" ", 1)[1].split(") || true", 1)[0]
        self.assertIn("--detach", hook)
        self.assertNotIn("nohup", hook)

    def test_steers_note_the_answer_after_the_permission_path(self) -> None:
        body = self.read("system/scripts/dispatch.py").split("def command_steer", 1)[1]
        self.assertLess(body.index("command_permission_steer(args"), body.index("note_owner_action_steer(args.task_id, text)"),
                        "allow and deny answer a dialog, not the owner's ruling")

    def test_a_steer_that_did_not_arrive_answers_nothing(self) -> None:
        """Wrong impl: the note runs before the send, so a refused message closes the ruling."""
        body = self.read("system/scripts/dispatch.py").split("def command_steer", 1)[1]
        before_send = body.split("sent = cmux_agent_message(", 1)[0]
        self.assertNotIn("note_owner_action_steer(", before_send)
        delivered = body.split("if sent.get(\"ok\"):", 1)[1].split("return 0", 1)[0]
        self.assertIn("note_owner_action_steer(", delivered)

    def test_capture_runs_after_delivery(self) -> None:
        """Wrong impl: calendar calls and a busy to-do lock delay the report itself."""
        script = self.read("system/scripts/report-to-cos.sh")
        self.assertLess(script.index('delivered="true"; outcome="delivered"'), script.index("owner_action_capture.py"))
        asking = self.read("system/scripts/ask_cos.py").split("def command_ask", 1)[1]
        self.assertLess(asking.index("send_once(line)"), asking.index("capture.capture_question"))

    def test_the_loop_reconciles(self) -> None:
        self.assertIn("owner_action_capture.py reconcile", self.read("system/scripts/command-center-loop.sh"))


class SandboxNeverWritesMain(unittest.TestCase):
    """Wrong impl: the sandbox only guards live tabs, so a test with hooks on commits a fixture's
    close-out to the real main, and auto-push publishes it. Nothing here writes: the writers are
    mocked or stopped by the guard under test."""

    def test_the_guard_names_main_and_only_main(self) -> None:
        main = owner_tabs.main_checkout(HERE)
        self.assertTrue(owner_tabs.sandbox_reaching_main(main))
        self.assertFalse(owner_tabs.sandbox_reaching_main(ROOT), "a test's own temp repo still writes")
        with mock.patch.dict(os.environ, {"PM_OS_TEST_SANDBOX": "0"}):
            self.assertFalse(owner_tabs.sandbox_reaching_main(main), "a real worker's close-out still lands")

    def test_record_on_main_refuses_in_the_sandbox(self) -> None:
        import dispatch
        with mock.patch.object(dispatch, "REPO_ROOT", owner_tabs.main_checkout(HERE)), \
                mock.patch.object(dispatch, "record_closeout") as writer:
            out = dispatch.record_on_main({"id": "rt-invented03", "status": "done", "workstream": SLUG, "summary": "invented"})
        writer.assert_not_called()
        self.assertFalse(out["recorded"])

    def test_the_result_card_writer_refuses_in_the_sandbox(self) -> None:
        with mock.patch.object(Path, "write_text", side_effect=AssertionError("wrote the real main")), \
                mock.patch.object(owner_tabs, "commit_written_paths", side_effect=AssertionError("committed the real main")):
            out = owner_tabs.write_latest_results(owner_tabs.main_checkout(HERE), "pm-os-maintenance", "rt-invented03", "invented card")
        self.assertFalse(out["written"])


class ReportToCos(unittest.TestCase):
    """End to end: the real report-to-cos.sh in the sandbox. Fails without the hook. Keeps
    REPORT_TO_COS_NO_HOOKS=1: with the other hooks on, this fixture's close-out line could
    commit to the real main."""

    def setUp(self) -> None:
        from test_report_delivery import Harness

        class Run(Harness):
            def runTest(self) -> None:  # pragma: no cover
                pass

        self.run_ = Run()
        self.run_.setUp()
        self.addCleanup(self.run_.tearDown)
        self.assertEqual(self.run_.env["REPORT_TO_COS_NO_HOOKS"], "1")
        self.todo = self.run_.state / "todo.json"
        self.todo.write_text(json.dumps({"items": []}), encoding="utf-8")
        wrapper = self.run_.tmp / "todo_stub.py"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        self.run_.env.update({"OWNER_ACTION_TODO_CLI": str(wrapper), "OWNER_TABS_ROOT": str(self.run_.tmp),
                              "PM_OS_ROOT": str(self.run_.tmp)})
        self.run_.seed("rt-e2e0001")

    def items(self) -> list[dict]:
        return json.loads(self.todo.read_text(encoding="utf-8"))["items"]

    def send(self, summary: str, *extra: str) -> dict:
        return self.run_.report("--task-id", "rt-e2e0001", "--workstream", "pm-os-maintenance", "--origin", "cos",
                                "--status", "done", "--delivery", "direct", *extra, summary=summary)

    def test_a_done_report_with_an_owner_followup_lands_on_the_list(self) -> None:
        self.send("Finished the invented widget. Follow-up: the owner picks a vendor.")
        self.assertEqual([i["title"] for i in self.items()], ["Pick a vendor"])

    def test_a_chore_report_adds_nothing(self) -> None:
        self.send("Finished the invented widget. Follow-up: worker chore: rerun the installer.")
        self.assertEqual(self.items(), [])

    def test_a_broken_temp_dir_never_stops_the_report(self) -> None:
        """Wrong impl: mktemp fails under set -e and the report is never delivered."""
        self.run_.env["TMPDIR"] = str(self.run_.tmp / "absent" / "dir")
        out = self.send("Finished the invented widget. Follow-up: the owner picks a vendor.")
        self.assertEqual(out["task_id"], "rt-e2e0001")
        self.assertTrue(out["delivered"] or out["batched"] or out["delivery_outcome"], out)

    def test_a_loop_retry_of_a_held_report_still_captures_once(self) -> None:
        """Wrong impl: replays skip the hook, so a pass that died during delivery loses the action for good."""
        self.send("Finished the invented widget. Follow-up: the owner picks a vendor.")
        self.send("Finished the invented widget. Follow-up: the owner picks a vendor.")
        self.assertEqual([i["title"] for i in self.items()], ["Pick a vendor"])

    def test_no_hooks_and_no_stub_means_no_capture(self) -> None:
        del self.run_.env["OWNER_ACTION_TODO_CLI"]
        self.send("Finished the invented widget. Follow-up: the owner picks a vendor.")
        self.assertEqual(self.items(), [])


if __name__ == "__main__":
    unittest.main()
