from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("router-context-pack.py")
SPEC = importlib.util.spec_from_file_location("router_context_pack", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RouterContextPackTest(unittest.TestCase):
    def test_ranking_prefers_specific_match_terms(self) -> None:
        rows = [
            {
                "slug": "recipe-search",
                "display_name": "Recipe Search",
                "status": "active",
                "todo_match_terms": ["firecrawl approval"],
                "signal_match_terms": [],
                "description": "Owns search quality and Firecrawl reviews.",
                "startup_instruction": "",
                "next_step": "",
            },
            {
                "slug": "garden-revamp",
                "display_name": "Garden Revamp",
                "status": "active",
                "todo_match_terms": [],
                "signal_match_terms": [],
                "description": "Owns broad billing planning.",
                "startup_instruction": "",
                "next_step": "",
            },
        ]

        ranked = MODULE.rank_workstreams("Check the Firecrawl approval", rows, [])

        self.assertEqual(ranked[0]["slug"], "recipe-search")
        self.assertIn('match term "firecrawl approval"', ranked[0]["evidence"])

    def test_priority_channel_is_strong_ownership_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "priority-channels.yaml"
            path.write_text(
                "tier_1:\n"
                "  - id: C123\n"
                "    name: proj-example\n"
                "    default_workstream: example-owner\n",
                encoding="utf-8",
            )

            routes = MODULE.channel_routes(
                "Follow up in https://slack.example/archives/C123/p1",
                path,
            )

        self.assertEqual(routes[0]["workstream"], "example-owner")

    def test_active_related_task_surfaces_duplicate_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "running-tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "rt-existing",
                                "status": "active",
                                "workstream": "recipe-search",
                                "prompt": "Complete the Firecrawl approval review",
                                "summary": "Review is already running.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            related = MODULE.active_related_tasks(
                "Check progress on the Firecrawl approval",
                path,
            )

        self.assertEqual(related[0]["task_id"], "rt-existing")

    def test_current_digest_is_one_shot_but_recurring_digest_is_durable(self) -> None:
        one_shot = MODULE.request_shape("Summarize everything I did in the past 7 days")
        durable = MODULE.request_shape("Build a recurring weekly digest of my work")

        self.assertEqual(one_shot["classification"], "likely-one-shot")
        self.assertEqual(durable["classification"], "likely-durable")

    def test_single_generic_overlap_does_not_create_a_candidate(self) -> None:
        rows = [
            {
                "slug": "historic-planning",
                "display_name": "Historic Planning",
                "status": "active",
                "todo_match_terms": [],
                "signal_match_terms": [],
                "description": "Owns work from the past planning cycle.",
                "startup_instruction": "",
                "next_step": "",
            }
        ]

        ranked = MODULE.rank_workstreams("Summarize the past seven days", rows, [])

        self.assertEqual(ranked, [])

    def test_explicit_workstream_directive_outranks_lexical_overlap(self) -> None:
        # A "Workstream: <slug>" directive wins even when another owner shares more topic terms.
        rows = [
            {
                "slug": "launch-notes",
                "display_name": "Launch Notes",
                "status": "active",
                "todo_match_terms": ["stall classes", "regression tests"],
                "signal_match_terms": [],
                "description": "Candidate workers never resume; dispatched reliability fixes must add regression tests.",
                "startup_instruction": "",
                "next_step": "",
            },
            {
                "slug": "pm-os-maintenance",
                "display_name": "PM OS Maintenance",
                "status": "active",
                "todo_match_terms": [],
                "signal_match_terms": [],
                "description": "Owns the operating system itself.",
                "startup_instruction": "",
                "next_step": "",
            },
        ]
        prompt = ("Original request: Workstream: pm-os-maintenance (fold into the reliability review). Workers never resume; "
                  "two stall classes were verified; add them to the fix and its regression tests.")

        ranked = MODULE.rank_workstreams(prompt, rows, [])

        self.assertEqual(MODULE.explicit_workstream(prompt), "pm-os-maintenance")
        self.assertEqual(ranked[0]["slug"], "pm-os-maintenance")
        self.assertIn('explicit directive "Workstream: pm-os-maintenance"', ranked[0]["evidence"])
        self.assertGreater(ranked[1]["score"], 220, "the decoy must still score higher than the directive bonus alone would need")

    def test_explicit_workstream_parses_common_wrappers(self) -> None:
        self.assertEqual(MODULE.explicit_workstream("Workstream: launch-notes. the owner said ..."), "launch-notes")
        self.assertEqual(MODULE.explicit_workstream("**Workstream:** pm-os-maintenance\nmore"), "pm-os-maintenance")
        self.assertEqual(MODULE.explicit_workstream("workstream : Foo-Bar (see note)"), "foo-bar")
        self.assertEqual(MODULE.explicit_workstream("Create a new workstream for this."), "")
        self.assertEqual(MODULE.explicit_workstream("No directive here"), "")



def _row(slug: str, name: str, description: str = "", terms: list[str] | None = None, status: str = "active") -> dict:
    return {"slug": slug, "display_name": name, "status": status, "todo_match_terms": terms or [],
            "signal_match_terms": [], "description": description, "startup_instruction": "", "next_step": ""}


class SourceTitleOwnerTest(unittest.TestCase):
    """A linked doc's title ("Maple_Proposal_Working Draft.docx") names
    maple-book-club even when the prompt words alone match nothing strong."""

    PROMPT = ("Quick read of a Google Doc: Can you look at this and tell me if "
              "there's anything I should flag for the host: "
              "https://docs.google.com/document/d/file0000000001/edit")
    ROWS = [
        _row("maple-book-club", "Maplewood Book Club",
             "Owns the book club venue search for Maplewood: room booking for the spring meetings.", ["maplewood"]),
        _row("launch-notes", "Launch Notes", "Owns host checklists for launch notes."),
        _row("garden-swap", "Garden Swap", "Owns garden swap proposal work."),
        _row("venue-photos", "Venue Photos", "Owns venue photos proposal work."),
    ]

    def test_title_word_names_the_owner(self) -> None:
        ranked = MODULE.rank_workstreams(self.PROMPT, self.ROWS, [], ["Maple Proposal"])
        self.assertEqual(ranked[0]["slug"], "maple-book-club")
        self.assertTrue(ranked[0]["evidence"][0].startswith('source title "Maple Proposal"'), ranked[0]["evidence"])

    def test_without_the_title_nothing_names_the_book_club(self) -> None:
        ranked = MODULE.rank_workstreams(self.PROMPT, self.ROWS, [])
        self.assertFalse(any(e.startswith("source title") for r in ranked for e in r["evidence"]))

    def test_urls_never_feed_lexical_overlap(self) -> None:
        ranked = MODULE.rank_workstreams(self.PROMPT, self.ROWS, [])
        for row in ranked:
            for item in row["evidence"]:
                self.assertNotIn("file0000000001", item)
                self.assertNotIn("docs", item.split(":", 1)[-1].replace(" ", "").split(","))

    def test_common_or_tied_title_words_name_nobody(self) -> None:
        # Title-noise words alone name nobody; a description-only hit scores 1, below the bar.
        self.assertIsNone(MODULE.title_owner(["Proposal Working Draft"], self.ROWS))
        self.assertIsNone(MODULE.title_owner(["Venue Booking"], self.ROWS))
        tied = [*self.ROWS, _row("maple-proposal", "Maple Proposal")]
        self.assertIsNone(MODULE.title_owner(["Maple Proposal"], tied))
        many = [*self.ROWS, *[_row(f"maple-{n}", f"Maple {n}") for n in ("a", "b")]]
        self.assertIsNone(MODULE.title_owner(["Maple Proposal"], many))

    def test_terminal_workstreams_are_not_title_owners(self) -> None:
        rows = [_row("maple-book-club", "Maplewood Book Club", status="done"), *self.ROWS[1:]]
        self.assertIsNone(MODULE.title_owner(["Maple Proposal"], rows))

    def test_title_matches_a_match_term_phrase(self) -> None:
        rows = [_row("acme-bake-sale", "Acme Bake Sale", terms=["acme bake sale", "bake sale"]),
                *self.ROWS]
        ranked = MODULE.rank_workstreams("Can you create a doc: https://example.slack.com/archives/C1/p1700000000000100",
                                         rows, [], ["Acme Bake Sale Flyer"])
        self.assertEqual(ranked[0]["slug"], "acme-bake-sale")
        self.assertIn('match term "acme bake sale"', ranked[0]["evidence"])


class NamedOwnerEvidenceTest(unittest.TestCase):
    """Asks that name their owner by path, note, hyphenated slug, or a shortened
    name rank that owner first."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        kit = self.root / "workstreams" / "photo-book-kit"
        kit.mkdir(parents=True)
        (kit / "2026-01-15-photo-kit-print-fixes.md").write_text("# v5\n")
        (kit / "CONTEXT.md").write_text("# kit\n")
        (self.root / "system" / "scripts").mkdir(parents=True)
        (self.root / "system" / "scripts" / "dispatch.py").write_text("")
        self.rows = [
            {"slug": "pm-os-maintenance", "display_name": "PM OS Maintenance", "status": "active"},
            {"slug": "photo-book-kit", "display_name": "Photo Book Kit", "status": "active"},
            {"slug": "launch-notes", "display_name": "Launch Notes", "status": "active",
             "todo_match_terms": ["netlify"]},
        ]

    def evidence(self, prompt: str, slug: str) -> list[str]:
        ranked = MODULE.rank_workstreams(prompt, self.rows, [], root=self.root)
        return next((row["evidence"] for row in ranked if row["slug"] == slug), [])

    def test_workstream_path_and_note_are_named(self) -> None:
        prompt = "See workstreams/photo-book-kit/2026-01-15-photo-kit-print-fixes.md first."
        evidence = self.evidence(prompt, "photo-book-kit")
        self.assertIn("workstream path named: workstreams/photo-book-kit/", evidence)
        self.assertIn("note named: 2026-01-15-photo-kit-print-fixes.md", evidence)
        self.assertEqual(MODULE.path_owner(prompt, self.rows), "photo-book-kit")

    def test_bare_note_name_is_named(self) -> None:
        evidence = self.evidence("Start from 2026-01-15-photo-kit-print-fixes.md.", "photo-book-kit")
        self.assertIn("note named: 2026-01-15-photo-kit-print-fixes.md", evidence)

    def test_common_file_names_name_nobody(self) -> None:
        self.assertFalse([e for e in self.evidence("Update CONTEXT.md", "photo-book-kit") if e.startswith("note named")])

    def test_path_owner_is_empty_when_another_slug_is_named(self) -> None:
        prompt = "Move workstreams/photo-book-kit/ notes into pm-os-maintenance."
        self.assertEqual(MODULE.path_owner(prompt, self.rows), "")

    def test_shortened_name_and_subject(self) -> None:
        evidence = self.evidence("the owner: 'Can you pick up the Photo Kit work?'", "photo-book-kit")
        self.assertIn('subject names this workstream: "photo kit"', evidence)
        self.assertIn('abbreviated workstream name "photo kit"', evidence)

    def test_hyphenated_slug_is_an_exact_slug_phrase(self) -> None:
        self.assertIn("exact slug phrase", self.evidence("Fold it into pm-os-maintenance.", "pm-os-maintenance"))
        # A repo path that merely contains a slug is not the slug.
        self.assertNotIn("exact slug phrase", self.evidence("Publish to example/photo-book-kit", "photo-book-kit"))

    def test_pm_os_script_names_the_system_owner(self) -> None:
        evidence = self.evidence("Why does dispatch.py open a new tab?", "pm-os-maintenance")
        self.assertIn("PM OS system named: dispatch.py", evidence)

    def test_pm_os_in_the_owners_words_names_the_system_owner(self) -> None:
        evidence = self.evidence("the owner: 'why did this happen again? PM OS should prevent this'", "pm-os-maintenance")
        self.assertIn('subject names this workstream: "PM OS"', evidence)

    def test_negated_match_term_names_nobody(self) -> None:
        self.assertEqual(self.evidence("Build a local app; do NOT deploy it to Netlify.", "launch-notes"), [])
        self.assertIn('match term "netlify"', self.evidence("Publish the report on Netlify.", "launch-notes"))

    def test_quoted_subject_forms(self) -> None:
        self.assertEqual(MODULE.ask_subject("the owner's personal ask (verbatim): 'Review my plan.' More context."),
                         "Review my plan.")
        self.assertEqual(MODULE.ask_subject('the owner (11-05, CoS): "Fix the links" and more'), "Fix the links")
        self.assertEqual(MODULE.ask_subject("Read-only diagnosis. More."), "Read-only diagnosis.")


if __name__ == "__main__":
    unittest.main()
