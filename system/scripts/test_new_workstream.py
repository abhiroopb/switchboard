#!/usr/bin/env python3
"""Regression tests for the workstream naming rule in new-workstream.py.

Rule: a new workstream gets a 1-3 word Title Case name
that reads as the title of the work, with a matching kebab slug, derived from
the subject of the work and never from the prompt's verbs, filler, or URL
fragments. The incident slug was `create-doc-fill-out-https-example`, built from
"Can you create a doc and help me fill this out: <slack permalink>".

Run: python3 -m pytest system/scripts/test_new_workstream.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().with_name("new-workstream.py")
spec = importlib.util.spec_from_file_location("new_workstream", SCRIPT)
nw = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(nw)

INCIDENT_PROMPT = ("Can you create a doc and help me fill this out: "
                   "https://example.slack.com/archives/C0AAAAAAAA/p1700000000000100")
INCIDENT_SLUG = "create-doc-fill-out-https-example"
SLACK_TEXT = ("Morning <@U123>! Following up on the Orchard Gazette on Jan 15. "
              "Here is the guide: <https://docs.google.com/document/d/file0000000002/edit|doc>")


def _slug_words(slug: str) -> list[str]:
    return slug.split("-")


LONG_PERSONAL_PROMPT = "Club research (the owner, 2000-01-01 PT): 'I am going to run the Brightwater chess club night using the Orchard Library room booking program. These are the rooms I have available to me. Using the following criteria (and any other that makes sense) please help me decide which room I should book: 1. Seating for twenty players 2. Opening hours (the booking has to cover setup and cleanup) 3. The quietest room and the longest booking 4. Any x-factor, for example a room with its own projector or a booking that also covers coffee. Any other interesting things you can find that is relevant.' The options (copied from the library booking page): (1) Alder Room (2) Birch Room (3) Cedar Room (4) Dogwood Room (5) Elm Room."


class IncidentPromptTest(unittest.TestCase):
    """The exact prompt that produced the literal slug."""

    def test_offline_slug_has_no_verbs_or_url_fragments(self) -> None:
        name, slug, quality = nw.derive_name(INCIDENT_PROMPT, fetch=False)
        self.assertNotEqual(slug, INCIDENT_SLUG)
        self.assertLessEqual(len(_slug_words(slug)), nw.MAX_NAME_WORDS)
        for bad in ("https", "example", "archives", "create", "fill", "help", "can"):
            self.assertNotIn(bad, _slug_words(slug), slug)
        self.assertTrue(nw.validate_slug(slug)[0], slug)
        # The ask links a thread and no title resolved offline; only an
        # artifact noun ("doc") is left, so the placeholder is the hash and
        # dispatch logs a rename warning.
        self.assertEqual(quality, "hash")
        self.assertRegex(slug, r"^workstream-[0-9a-f]{8}$")

    def test_source_title_names_the_work(self) -> None:
        with patch.object(nw.link_titles, "slack_thread_text", return_value=SLACK_TEXT), \
             patch.object(nw.link_titles, "drive_title", return_value="Acme Bake Sale Guide [2026]"), \
             patch.dict(os.environ, {e: "" for e in nw.link_titles.OFFLINE_ENVS}):
            name, slug, quality = nw.derive_name(INCIDENT_PROMPT, fetch=True)
        self.assertEqual((name, slug, quality), ("Acme Bake Sale", "acme-bake-sale", "source"))

    def test_slack_text_alone_gives_capitalized_run(self) -> None:
        with patch.object(nw.link_titles, "slack_thread_text", return_value=SLACK_TEXT), \
             patch.object(nw.link_titles, "drive_title", return_value=""), \
             patch.dict(os.environ, {e: "" for e in nw.link_titles.OFFLINE_ENVS}):
            name, slug, quality = nw.derive_name(INCIDENT_PROMPT, fetch=True)
        self.assertEqual((name, slug, quality), ("Orchard Gazette", "orchard-gazette", "source"))

    def test_old_slug_is_rejected_as_explicit_name(self) -> None:
        ok, reason = nw.validate_slug(INCIDENT_SLUG)
        self.assertFalse(ok)
        self.assertIn("words", reason)
        with self.assertRaises(nw.NameError_):
            nw.kebab_name(INCIDENT_SLUG)

    def test_offline_env_skips_fetch(self) -> None:
        with patch.object(nw.link_titles, "slack_thread_text", side_effect=AssertionError("fetched")):
            self.assertEqual(nw.source_title(["https://example.slack.com/archives/C1/p1234567890123456"],
                                             fetch=False), "")


class ValidateSlugTest(unittest.TestCase):
    def test_rejects_prompt_and_url_shapes(self) -> None:
        for slug, fragment in (
            ("create-doc", "verb"),
            ("help-with-flyer", "verb"),
            ("can-you-review", "verb"),
            ("docs-google-thread", "URL fragments"),
            ("slack-archives-thread", "URL fragments"),
            ("flyer-p1700000000000100", "URL fragments"),
            ("usp-sharing", "URL fragments"),
            ("a-b-c-d", "words"),
            ("the-and-of", "no topic word"),
            ("Acme Bake", "kebab"),
            ("", "kebab"),
        ):
            ok, reason = nw.validate_slug(slug)
            self.assertFalse(ok, slug)
            self.assertIn(fragment, reason, slug)

    def test_accepts_topic_titles_and_hash_placeholder(self) -> None:
        for slug in ("acme-bake-sale", "garden-swap", "pm-os-maintenance",
                     "maple-street-pta", "workstream-1a2b3c4d", "design-review", "photo-sharing"):
            self.assertTrue(nw.validate_slug(slug)[0], slug)


class DeriveNameTest(unittest.TestCase):
    def test_specific_words_in_ask_win_without_fetch(self) -> None:
        cases = {
            "Garden swap rollout plan": ("Garden Swap Rollout", "garden-swap-rollout"),
            "Help me prep for the Maple Street PTA fair registration": ("Maple Street PTA", "maple-street-pta"),
            "Can you fix the Netlify banner for Orchard apps": ("Netlify Banner Orchard", "netlify-banner-orchard"),
        }
        for ask, expected in cases.items():
            with patch.object(nw, "source_title", side_effect=AssertionError("fetched")):
                name, slug, quality = nw.derive_name(ask, fetch=True)
            self.assertEqual((name, slug), expected, ask)
            self.assertEqual(quality, "ask")

    def test_link_only_ask_offline_is_hash_placeholder(self) -> None:
        name, slug, quality = nw.derive_name("https://linear.app/acme/issue/ENG-1/x", fetch=False)
        self.assertEqual(quality, "hash")
        self.assertRegex(slug, r"^workstream-[0-9a-f]{8}$")
        self.assertTrue(nw.validate_slug(slug)[0])

    def test_slugify_is_offline_and_bounded(self) -> None:
        with patch.object(nw.link_titles, "slack_thread_text", side_effect=AssertionError("fetched")):
            slug = nw.slugify(INCIDENT_PROMPT)
        self.assertLessEqual(len(_slug_words(slug)), nw.MAX_NAME_WORDS)

    def test_attribution_names_are_not_topics(self) -> None:
        name, slug, _ = nw.derive_name("Fix workstream naming", fetch=False)
        self.assertNotIn("owner", _slug_words(slug))
        self.assertEqual(slug, "workstream-naming")


class SourceTitleHelpersTest(unittest.TestCase):
    def test_clean_source_title_drops_year_and_artifact_tail(self) -> None:
        self.assertEqual(nw.clean_source_title("Acme Bake Sale Guide [2026]"), "Acme Bake Sale")
        self.assertEqual(nw.clean_source_title("Garden Swap: Rollout Plan (draft)"), "Garden Swap Rollout")
        self.assertEqual(nw.clean_source_title(""), "")

    def test_capitalized_run_skips_greetings_and_dates(self) -> None:
        self.assertEqual(nw.capitalized_run(SLACK_TEXT), "Orchard Gazette")
        self.assertEqual(nw.capitalized_run("hi there, nothing capitalised here"), "")

    def test_source_title_rejects_titles_that_fail_the_rule(self) -> None:
        with patch.object(nw.link_titles, "drive_title", return_value="Create a doc for the slack archives thread"), \
             patch.dict(os.environ, {e: "" for e in nw.link_titles.OFFLINE_ENVS}):
            self.assertEqual(nw.source_title(["https://docs.google.com/document/d/abc/edit"]), "")


CLUB_DOC_PROMPT = ("Quick pre-review of a Google Doc: Can you review this and let me know if "
                   "there's anything I need to call out to the hosts: "
                   "https://docs.google.com/document/d/file0000000001/edit")
CLUB_DOC_NAME = "Maple_Proposal_Working Draft.docx"
ONLINE = {e: "" for e in nw.link_titles.OFFLINE_ENVS}


class BookClubDocTest(unittest.TestCase):
    """The prompt above carries no topic of its own; the linked doc's title names the work."""

    def test_doc_title_names_the_work(self) -> None:
        with patch.object(nw.link_titles, "drive_title", return_value=CLUB_DOC_NAME) as drive, \
             patch.dict(os.environ, ONLINE):
            result = nw.derive_name(CLUB_DOC_PROMPT, fetch=True)
        self.assertEqual(result, ("Maple Proposal", "maple-proposal", "source"))
        drive.assert_called_once()

    def test_resolved_title_is_used_without_fetching(self) -> None:
        with patch.object(nw.link_titles, "fetch_titles", side_effect=AssertionError("fetched")):
            result = nw.derive_name(CLUB_DOC_PROMPT, fetch=True, titles=["Maple Proposal"])
        self.assertEqual(result, ("Maple Proposal", "maple-proposal", "source"))

    def test_failed_lookup_is_hash_not_prompt_words(self) -> None:
        for fetch, patcher in ((False, None), (True, patch.object(nw.link_titles, "drive_title", return_value=""))):
            if patcher:
                with patcher, patch.dict(os.environ, ONLINE):
                    name, slug, quality = nw.derive_name(CLUB_DOC_PROMPT, fetch=fetch)
            else:
                name, slug, quality = nw.derive_name(CLUB_DOC_PROMPT, fetch=fetch)
            self.assertEqual(quality, "hash", (fetch, slug))
            self.assertRegex(slug, r"^workstream-[0-9a-f]{8}$")
            self.assertNotIn("quick-pre", slug)

    def test_ask_words_survive_a_failed_lookup_when_specific(self) -> None:
        ask = "Garden swap FAQ: https://docs.google.com/document/d/abc/edit"
        with patch.object(nw.link_titles, "drive_title", return_value=""), patch.dict(os.environ, ONLINE):
            self.assertEqual(nw.derive_name(ask, fetch=True), ("Garden Swap FAQ", "garden-swap-faq", "ask"))


class LeadingDescriptorTest(unittest.TestCase):
    """'Pre-review of', 'Review', 'Help me with' never become the name."""

    def test_descriptors_are_stripped(self) -> None:
        cases = {
            "Final pre-review of the garden swap FAQ": "garden-swap-faq",
            "Review the venue photos launch plan": "venue-photos",
            "Help me with Maple Street PTA fair registration": "maple-street-pta",
            "Privacy review of the Acme Club signup form": "acme-club-signup",
            "Quick review: library fee deprecation": "library-fee-deprecation",
        }
        for ask, slug in cases.items():
            got = nw.derive_name(ask, fetch=False)[1]
            self.assertEqual(got, slug, ask)
            for bad in ("final", "privacy", "pre", "review", "help", "quick"):
                self.assertNotIn(bad, got.split("-"), ask)

    def test_review_inside_a_topic_is_kept_as_topic_words(self) -> None:
        self.assertEqual(nw.derive_name("Garden swap review", fetch=False)[1], "garden-swap")


class TitleNoiseTest(unittest.TestCase):
    def test_extensions_and_copy_words_are_stripped(self) -> None:
        cases = {
            CLUB_DOC_NAME: "Maple Proposal",
            "Copy of Garden Swap FAQ v2": "Garden Swap FAQ",
            "[DRAFT] Venue Photos - final.pdf": "Venue Photos",
            "Untitled document": "",
        }
        for raw, want in cases.items():
            self.assertEqual(nw.link_titles.strip_title_noise(raw), want, raw)
        self.assertEqual(nw.clean_source_title(CLUB_DOC_NAME), "Maple Proposal")

    def test_link_kinds(self) -> None:
        lt = nw.link_titles
        text = ("see https://docs.google.com/spreadsheets/d/x/edit, <https://example.slack.com/archives/C1/p1700000000000100|t>, "
                "https://linear.app/acme/issue/ENG-1/x https://example.atlassian.net/browse/ENG-7121 "
                "https://github.com/example/app/pull/1 https://example.com/a")
        self.assertEqual([lt.link_kind(u) for u in lt.resolvable_urls(text, limit=9)],
                         ["drive", "slack", "linear", "jira", "github"])

    def test_offline_fetch_is_empty(self) -> None:
        with patch.dict(os.environ, {"PM_OS_LINK_TITLES_OFFLINE": "1"}), \
             patch.object(nw.link_titles, "drive_title", side_effect=AssertionError("fetched")):
            self.assertEqual(nw.link_titles.fetch_titles(CLUB_DOC_PROMPT), [])

    def test_jira_and_github_parsers(self) -> None:
        lt = nw.link_titles
        jira = {"data": {"issues": [{"fields": {"summary": "Legacy Search Deprecation"}, "key": "ENG-7121"}]}}
        with patch.object(lt, "_run_json", return_value=jira) as run:
            self.assertEqual(lt.jira_title("https://example.atlassian.net/browse/ENG-7121"), "Legacy Search Deprecation")
        self.assertIn("key = ENG-7121", run.call_args[0][0])
        with patch.object(lt, "_run_json", return_value={"title": "Recipe card skill"}) as run:
            self.assertEqual(lt.github_title("https://github.com/example/app/pull/1234"), "Recipe card skill")
        self.assertEqual(run.call_args[0][0][:3], ["gh", "pr", "view"])


class MatchTermsTest(unittest.TestCase):
    def test_full_name_then_tail(self) -> None:
        self.assertEqual(nw.match_terms("Acme Bake Sale", "acme-bake-sale"),
                         ["acme bake sale", "bake sale"])
        self.assertEqual(nw.match_terms("Garden Swap", "garden-swap"), ["garden swap"])


class DispatchPreambleTest(unittest.TestCase):
    """A dispatched prompt with a dated preamble ('the owner (<date> <time> PT, CoS):
    "Looks like it failed: <Slack DM>"') and no Workstream line must not name its
    placeholder from the preamble (`pm-pt-cos`). Prompt: fixtures/routing_miss_placeholder_owner.json."""

    FIXTURE = __import__("json").loads((SCRIPT.parent / "fixtures" / "routing_miss_placeholder_owner.json").read_text())

    def test_failure_link_prompt_gets_the_neutral_triage_placeholder(self) -> None:
        # Wrong impl: "PM PT CoS" from the date, time zone, and surface.
        name, slug, quality = nw.derive_name(self.FIXTURE["prompt"], fetch=False)
        self.assertNotEqual(slug, self.FIXTURE["wrong_placeholder"])
        self.assertRegex(slug, r"^triage-[0-9a-f]{8}$")
        self.assertEqual(name, "Triage " + slug.split("-")[1])
        self.assertEqual(quality, "hash")
        self.assertTrue(nw.validate_slug(slug)[0])
        for boilerplate in ("pm", "pt", "cos", "owner", "slack", "first", "kit"):
            self.assertNotIn(boilerplate, _slug_words(slug))

    def test_preamble_never_names_a_workstream(self) -> None:
        cases = {
            'the owner (2000-01-01, CoS): "Plan the Acme Bake Sale" Read the doc first.':
                "acme-bake-sale",
            "the owner (2000-01-01, Launchpad): garden swap rollout plan": "garden-swap-rollout",
            "~1:00 PM PT: Maple Street PTA fair registration": "maple-street-pta",
        }
        for ask, expected in cases.items():
            self.assertEqual(nw.derive_name(ask, fetch=False)[1], expected, ask)

    def test_thin_quote_falls_back_to_the_context(self) -> None:
        # "Can you go ahead and fix this?" carries no topic; the context after it does.
        ask = 'the owner (2000-01-01, CoS): "Can you go ahead and fix this?" Garden swap rollout plan.'
        self.assertEqual(nw.derive_name(ask, fetch=False)[1], "garden-swap-rollout")

    def test_failure_filler_is_not_a_topic(self) -> None:
        name, slug, _ = nw.derive_name("Looks like it failed: https://linear.app/acme/issue/ENG-1/x", fetch=False)
        self.assertRegex(slug, r"^triage-[0-9a-f]{8}$")
        # A non-failure link-only ask keeps the generic placeholder.
        self.assertRegex(nw.derive_name("https://linear.app/acme/issue/ENG-1/x", fetch=False)[1], r"^workstream-[0-9a-f]{8}$")

    def test_a_readable_source_title_still_names_the_work(self) -> None:
        name, slug, quality = nw.derive_name(self.FIXTURE["prompt"], fetch=True, titles=["Acme Bake Sale Guide"])
        self.assertEqual((slug, quality), ("acme-bake-sale", "source"))


class CliTest(unittest.TestCase):
    """The CLI refuses a --name that breaks the rule so dispatch.py falls through."""

    def test_bad_explicit_name_exits_2(self) -> None:
        import subprocess
        env = dict(os.environ, **{nw.OFFLINE_ENV: "1"})
        result = subprocess.run([sys.executable, str(SCRIPT), "anything", "--name", INCIDENT_SLUG,
                                 "--json", "--no-inbox", "--no-render"],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("words", result.stderr)


class StrayWorkstreamNameTest(unittest.TestCase):
    """Filler words never name a workstream (only-diagnosis-changes,
    links-arent-working, did-happen-thought, or `might` from a Slack
    message title). Offline, so the ask's own words name the work."""

    FILLER = {"only", "arent", "verbatim", "might", "did", "happen", "thought", "personal", "require"}
    CASES = {
        "the owner: 'Can you pick up the Photo Kit work. Is it completed?' State: v5 was printed last week.": "photo-kit-completed",
        "the owner: 'The open in obsidian links aren't working'. Context: in the Claude Code CoS tab.": "obsidian-links",
        ("Read-only diagnosis, no changes (the owner chose this). the owner sees the 'Allow project hooks?' "
         "dialog on every new worker tab."): "allow-hooks-dialog",
        ("the owner's personal ask (verbatim): 'I've built a comprehensive recipe scoring spreadsheet here: "
         "https://docs.google.com/spreadsheets/d/1-TEST/edit I want a thorough review.'"): "comprehensive-recipe-scoring",
        ("the owner: 'Can you update the flyer and email skills with image gen? So it should use image gen to create "
         "better images' - context Slack thread."): "flyer-skills-image",
        "the owner: 'why did this happen again? PM OS should prevent this' about duplicate workers.": "pm-os-prevent",
    }

    def test_incident_prompts_get_topic_names(self) -> None:
        for ask, expected in self.CASES.items():
            with self.subTest(expected=expected):
                name, slug, quality = nw.derive_name(ask, fetch=False)
                self.assertEqual(slug, expected)
                self.assertFalse(self.FILLER & set(slug.split("-")), slug)

    def test_a_one_word_slack_title_never_names_the_work(self) -> None:
        # The flyer ask's Slack thread title was "Might"; it became the slug `might`.
        ask = next(a for a, s in self.CASES.items() if s == "flyer-skills-image")
        self.assertEqual(nw.derive_name(ask, fetch=False, titles=["Might"])[1], "flyer-skills-image")



class PersonalAskNameTest(unittest.TestCase):
    """A labelled ask ("Picnic research (the owner, <date> PT): 'I am going to run
    the <name> picnic ...'") takes its name from the proper nouns and repeated topic
    nouns of the lead sentence; pronouns, auxiliaries, common verbs, and calendar
    words never name the work."""

    LONG_ASK = LONG_PERSONAL_PROMPT
    FILLER = {"am", "going", "using", "project", "personal", "research", "me", "my", "we", "i",
              "good", "best", "near", "under", "before", "want", "need", "find", "help", "plan"}
    CASES = {
        "Side project (the owner, 2000-01-01 PT): 'I am going to start a Saturday chess club at Brightwater "
        "library and want a simple signup sheet.'": "chess-club-brightwater",
        "the owner's side ask: 'Can you help me find a used road bike under 400 dollars near Zellbrook?'": "road-bike-zellbrook",
        "the owner's side ask: 'I need to plan the Harbor Lane street fair before June. What permits do we "
        "need?'": "harbor-lane-street",
        "Side project (the owner, 2000-01-01 PT): 'We are going to print the Orchard Gazette in October and I "
        "want to find the best printer and a cover artist.'": "print-orchard-gazette",
    }

    def test_long_personal_prompt(self) -> None:
        name, slug, quality = nw.derive_name(self.LONG_ASK, fetch=False)
        self.assertEqual((name, slug, quality), ("Brightwater Room Booking", "brightwater-room-booking", "ask"))
        self.assertTrue(nw.validate_slug(slug)[0])

    def test_side_asks_get_proper_noun_and_topic_names(self) -> None:
        for ask, expected in self.CASES.items():
            with self.subTest(expected=expected):
                slug = nw.derive_name(ask, fetch=False)[1]
                self.assertEqual(slug, expected)
                self.assertFalse(self.FILLER & set(slug.split("-")), slug)

    def test_adjacent_proper_nouns_stay_together(self) -> None:
        # "Northwind Parks" is one unit: it never lends one word to the name.
        units = nw.topic_units("using the Northwind Parks caterer voucher program",
                               {"northwind", "parks", "caterer", "voucher", "program"})
        self.assertEqual([[w for _, w in u["words"]] for u in units],
                         [["northwind", "parks"], ["caterer"], ["voucher"], ["program"]])
        self.assertTrue(units[0]["proper"])

    def test_labelled_quote_names_from_the_owners_words(self) -> None:
        # The surface label ("Picnic research") is framing; the quote is the ask.
        core = nw.ask_core("Picnic research (the owner, 2000-01-01 PT): 'Caterer vouchers for Quillfield.' Context.")
        self.assertEqual(core.strip(), "Caterer vouchers for Quillfield.")

class ScaffoldPrivacyTest(unittest.TestCase):
    """Tracked scaffold files hold no ask text; the verbatim ask goes to the
    gitignored private/ folder, so a scaffold never publishes a network name.
    Every value here is invented."""

    SSID = "Zorblaxnet"
    ASK = ("Workstream: flyer-printer\nI join the Zorblaxnet network at the library, print the flyer, "
           "then switch back to the Quillhaven network. Token sk-live_9QwErTy12345.")

    def setUp(self) -> None:
        import subprocess
        import tempfile
        import private_identifiers as pi
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "-b", "main"], check=True)
        (self.root / ".gitignore").write_text("/local/\n/.state/\nworkstreams/**/private/\n", encoding="utf-8")
        pi.add_values(self.root, "network", [self.SSID, "Quillhaven"])
        for name, value in (("REPO_ROOT", self.root), ("WORKSTREAMS_DIR", self.root / "workstreams")):
            patcher = patch.object(nw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("pick_emoji", lambda slug, body: "\U0001f525"), ("render_readme", lambda: None)):
            patcher = patch.object(nw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {nw.OFFLINE_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args: str) -> int:
        import contextlib
        import io
        with patch.object(sys, "argv", ["new-workstream.py", *args]), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = nw.main()
        self.stderr = err.getvalue()
        return code

    def tracked_after_add(self, slug: str) -> dict[str, str]:
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "add", "-A", "--", f"workstreams/{slug}"], check=True)
        names = subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--name-only"],
                               capture_output=True, text=True, check=True).stdout.split()
        return {n: (self.root / n).read_text(encoding="utf-8") for n in names}

    def test_tracked_files_hold_no_ask_and_private_copy_is_ignored(self) -> None:
        self.assertEqual(self.run_cli(self.ASK, "--name", "flyer-printer", "--json", "--no-render"), 0)
        tracked = self.tracked_after_add("flyer-printer")
        self.assertEqual(sorted(tracked), ["workstreams/flyer-printer/CONTEXT.md", "workstreams/flyer-printer/config.yaml"])
        for path, text in tracked.items():
            for leaked in (self.SSID, "Quillhaven", "sk-live", "switch back"):
                self.assertNotIn(leaked.casefold(), text.casefold(), f"{path} carries {leaked!r}")
        folder = self.root / "workstreams" / "flyer-printer"
        private = list((folder / "private").glob("*-asks.md"))
        self.assertEqual(len(private), 1)
        self.assertIn(self.SSID, private[0].read_text(encoding="utf-8"))
        self.assertFalse(list((folder / "inbox").glob("*")))
        self.assertIn("private/", tracked["workstreams/flyer-printer/CONTEXT.md"])

    def test_name_with_a_listed_value_falls_back_to_placeholder(self) -> None:
        ask = "Zorblaxnet printer setup for the library"
        self.assertEqual(nw.derive_name(ask, fetch=False)[1].split("-")[0], "zorblaxnet")
        self.assertEqual(self.run_cli(ask, "--json", "--no-render"), 0)
        made = [p.name for p in (self.root / "workstreams").iterdir()]
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].startswith("workstream-"), made)
        self.assertIn("listed network", self.stderr)
        self.assertNotIn("zorblaxnet", self.stderr.casefold())
        for text in self.tracked_after_add(made[0]).values():
            self.assertNotIn("zorblaxnet", text.casefold())

    def test_dry_run_plans_the_same_placeholder(self) -> None:
        ask = "Zorblaxnet printer setup for the library"
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(sys, "argv", ["new-workstream.py", ask, "--json", "--dry-run"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(nw.main(), 0)
        self.assertTrue(json.loads(out.getvalue().splitlines()[-1])["slug"].startswith("workstream-"))

    def test_new_ask_on_existing_workstream_goes_to_private(self) -> None:
        self.assertEqual(self.run_cli("Flyer printer", "--name", "flyer-printer", "--json", "--no-render"), 0)
        self.assertEqual(self.run_cli(self.ASK, "--name", "flyer-printer", "--json", "--no-render"), 0)
        folder = self.root / "workstreams" / "flyer-printer"
        self.assertIn(self.SSID, next((folder / "private").glob("*-asks.md")).read_text(encoding="utf-8"))
        self.assertFalse(list((folder / "inbox").glob("*")))

    def test_ask_is_not_saved_when_private_is_not_ignored(self) -> None:
        (self.root / ".gitignore").write_text("/local/\n/.state/\n", encoding="utf-8")
        self.assertEqual(self.run_cli(self.ASK, "--name", "flyer-printer", "--json", "--no-render"), 0)
        tracked = self.tracked_after_add("flyer-printer")
        self.assertEqual(sorted(tracked), ["workstreams/flyer-printer/CONTEXT.md", "workstreams/flyer-printer/config.yaml"])
        self.assertIn("was not saved", tracked["workstreams/flyer-printer/CONTEXT.md"])
        self.assertNotIn(self.SSID.casefold(), "".join(tracked.values()).casefold())

    def test_tracked_findings_names_kinds_only(self) -> None:
        hits = nw.tracked_findings("Zorblaxnet sk-live_9QwErTy12345abcdef")
        self.assertIn("listed network", hits)
        self.assertIn("token", hits)
        self.assertEqual(nw.tracked_findings("Flyer Printer\nflyer-printer\nCreated 2026-11-09 01:18"), [])


if __name__ == "__main__":
    unittest.main()
