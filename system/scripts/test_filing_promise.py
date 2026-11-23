#!/usr/bin/env python3
"""The "Filing promised" check flags tracker promises and leaves paperwork sentences alone.

Run: python3 system/scripts/test_filing_promise.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_surface as rs


class FilingPromise(unittest.TestCase):
    def test_paperwork_and_document_uses_do_not_trigger(self) -> None:
        for sentence in (
            # The sentence that once raised a false alarm on a review report.
            "The line-by-line filing check and item-by-item tally were not redone.",
            "Your equipment filing is due in April.",
            "The filing deadline moved and the order form is due in April.",
            "Confirm the numbers with the treasurer before filing.",
            "We're filing the order form next week.",
            "We are filing the receipts for the club.",
            "Filing the invoices into the Equipment folder comes next.",
            "File the report in the Documents folder.",
            "The skill would file the installer under Downloads.",
            "Row 26 is filing to the shared drive.",
            "The club's patent filing came out.",
            "the filing cabinet, the filing system",
            # A to-do cleanup report: "filing the stub" names a document.
            "Closed 20: the jersey items (you said ready, and a worker is testing), the sizing test (skip), "
            "the roster update, the schedule additions, filing the stub, the test-rerun ruling "
            "(a continuation is running), the old-list purge, and duplicates.",
            "Filing the order stub comes next.",
            "We're filing the file in the Orders folder.",
            "Filing the signed form and the scan today.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(rs.filing_promises(sentence), [])
                self.assertEqual(rs.unbacked_filing_promise(sentence), [])

    def test_real_tracker_promises_stay_flagged(self) -> None:
        for sentence in (
            # The sentence that started the rule.
            "We're filing two things with the platform team.",
            "Filing with the platform team today.",
            "Filing a ticket for the login bug.",
            "We're filing this tomorrow.",
            "switchboard is filing it.",
            "I'll file a ticket about this.",
            "Filed two issues against the importer.",
            "Opened a Jira ticket.",
            "This is tracked in Linear.",
            "Created three tasks.",
            "Logged a bug on the sync job.",
            # The verb's own "file" is not a document word.
            "We'll file two things about this.",
            "Filing a stub issue in GitHub for the importer.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)
                self.assertTrue(rs.unbacked_filing_promise(sentence), sentence)

    def test_noun_log_and_adjective_open_do_not_trigger(self) -> None:
        # "log records" and "open issues" are a noun and an adjective here.
        for sentence in (
            "(10) The run log records the source, without the email links.",
            "The run log records the source.",
            "The logs record a task id per line.",
            "The script logs records to disk.",
            "There are three open issues on the board.",
            "Open issues stay in the note.",
            "The raise tasks were merged.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(rs.filing_promises(sentence), [])
                self.assertEqual(rs.unbacked_filing_promise(sentence), [])

    def test_local_note_entries_noun_file_and_own_warning_do_not_trigger(self) -> None:
        for sentence in (
            # Entries in a switchboard Markdown note, not a tracker.
            "**Issue log.** Both are logged as issues 21 and 22 in the Windows issue log, with kit item K14 for macOS.",
            # The same note, and "file" as a noun.
            "The issue log gains issue 14 and kit item K9.",
            "test_local_backup.py has 1 failure in switchboard-trash.py, a file this task did not touch.",
            "Logged as issue 23 in 2026-11-12-windows-cos-issues.md.",
            "Each one is logged as an issue in the maintenance note.",
            # The warning a first pass appended, read again by a second pass.
            'Done. ⚠️ Filing promised, no record URL ("logged as issues"): create the record via switchboard '
            "(linear, jira, airtable) or gh issue create and resend with its id and URL verbatim; CoS does not "
            "send this draft (external-replies Rule 9).",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(rs.filing_promises(sentence), [])
                self.assertEqual(rs.unbacked_filing_promise(sentence), [])

    def test_an_issue_log_entry_with_a_kit_item_does_not_trigger(self) -> None:
        # An entry in the Windows issue log that also names a kit item is not a record.
        for sentence in (
            "Logged as issue 30 with Mac kit item K18 in workstreams/switchboard-maintenance/2026-11-12-windows-cos-issues.md.",
            "Logged as issue 30 with Mac kit item K18 in workstreams/switchboard-maintenance/2026-11-12-windows-cos-issues.md",
            "Logged as issue 30, with kit item K18, in the Windows issue log.",
            "Logged as issue 30 (kit item K18) in 2026-11-12-windows-cos-issues.md.",
            "Logged as issues 30 and 31 with kit items K18 and K19 in the Windows issue log.",
            "Logged as issue 34 and kit item K21 in 2026-11-12-windows-cos-issues.md.",
            "Logged as issue 34 plus Mac kit item K21 in the maintenance note.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(rs.filing_promises(sentence), [])
                self.assertEqual(rs.unbacked_filing_promise(sentence), [])

    def test_issue_log_report_texts_do_not_trigger(self) -> None:
        # Full report texts that log numbered entries in a local issue log.
        for text in (
            # The filing-check fix report.
            "**Issue log.** Logged as issue 34 with Mac kit item K21 in 2026-11-12-windows-cos-issues.md: issue 31 "
            "was already the router-dispatch backticks entry. The live kit v9 worker edits other lines of "
            "review_surface.py and was told.",
            # A Windows CoS gate report.
            "Still open: on Windows Bun cannot run the cmux shim, so the CoS delivery, dispatch, and wait guards "
            "never see the tab and never fire. Logged as issue 32 and kit item K19 in "
            "2026-11-12-windows-cos-issues.md. Code-reviewer persona not run (one-number config change plus a test).",
            # A numbered entry with the note later in the sentence, plain or linked.
            "Logged as issue 32 for a numbered entry in the local file "
            "workstreams/switchboard-maintenance/2026-11-12-windows-cos-issues.md.",
            "Logged as issue 35, the digest lock entry, in "
            "[the issue log](workstreams/switchboard-maintenance/2026-11-12-windows-cos-issues.md).",
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(rs.filing_promises(text), [])
                self.assertEqual(rs.unbacked_filing_promise(text), [])

    def test_a_numbered_entry_with_a_named_tracker_or_no_note_stays_flagged(self) -> None:
        for sentence in (
            "Logged as issue 32 in GitHub and noted it in CONTEXT.md.",
            "Logged as issue 32 in Linear, see the issue log.",
            "Logged as issue 32 for the design team.",
            "Opened issue 32 for the team.",
            # A number with a .md file or log nearby is still a record.
            "Opened issue 412 to fix the README.md typo.",
            "Opened issue 412 on the mobile-app repo and updated CONTEXT.md.",
            "Created ticket 4821 with IT and added it to the run log.",
            "Logged as issue 32 for the design team and noted it in CONTEXT.md.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)

    def test_a_companion_clause_that_is_not_a_kit_item_keeps_the_promise(self) -> None:
        for sentence in (
            "Filed an issue with the team in the run log.",
            "Logged an issue with the design team in 2026-11-12-windows-cos-issues.md.",
            "Logged as issue 30 with a Linear issue in the Windows issue log.",
            "Logged as issue 30 with kit item K18 in the Windows issue log and filed it in Jira.",
            "Logged as issue 30 with kit item K18 in the Windows issue log, and I will file a ticket for it.",
            "Opened a GitHub issue with kit item K18 in the issue log.",
            "Logged a bug with item 4 in Linear.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)
                self.assertTrue(rs.unbacked_filing_promise(sentence), sentence)

    def test_a_note_elsewhere_in_the_sentence_does_not_excuse_a_promise(self) -> None:
        # Only the verb's own object may be the local note.
        for sentence in (
            "I will file a ticket for the design team and note it in CONTEXT.md.",
            "Filed an issue with the team; details in the run log.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)
        self.assertEqual(rs.filing_promises("Logged as issues 21 and 22 in the Windows issue log (UTF-8 bug)."), [])

    def test_a_named_tracker_outranks_a_local_note(self) -> None:
        for sentence in (
            "Logged a Linear issue for it and noted it in the issue log.",
            "Opened a GitHub issue; the run log has the trace.",
            "Filed two issues against the importer.",
            "I'll file a ticket about this.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)

    def test_bare_verbs_with_a_determiner_stay_flagged(self) -> None:
        for sentence in (
            "Open a ticket for the importer.",
            "Log a bug on the sync job.",
            "Raise an issue with the design team.",
            "I will create two tasks for this.",
            "It opens a ticket per failure.",
            "Opening Jira tickets for each.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(rs.filing_promises(sentence), sentence)

    def test_a_tracker_word_outranks_a_document_word(self) -> None:
        self.assertTrue(rs.filing_promises("We're filing a Linear ticket about the club folder."))
        self.assertTrue(rs.filing_promises("Filing a bug because the PDF export breaks."))

    def test_context_stays_inside_the_sentence(self) -> None:
        text = "The club folder is done. We're filing a request with the importer owners."
        self.assertTrue(rs.filing_promises(text))
        self.assertEqual(rs.filing_promises("The filing check was not redone. See Linear for the plan."), [])

    def test_a_record_url_clears_a_real_promise(self) -> None:
        self.assertEqual(rs.unbacked_filing_promise("Filing a ticket: https://linear.app/x/issue/ABC-1"), [])

    def test_the_warning_names_the_phrase(self) -> None:
        phrases = rs.unbacked_filing_promise("Filing a ticket for this.")
        self.assertIn(rs.FILING_WARNING, rs.filing_warning_text(phrases))


if __name__ == "__main__":
    unittest.main()
