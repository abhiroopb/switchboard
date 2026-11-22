"""Selection and seed rules for system/scripts/sod-workspace-plan.py.

Rule: start of day opens only the workstreams with a
to-do due today, a meeting today, an unanswered ask, or a P0, and seeds each
tab with a short note (To dos today / Missed yesterday / Reply to / Docs).
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import date


SCRIPT_PATH = pathlib.Path(__file__).with_name("sod-workspace-plan.py")

# Names never come from the network or the shared cache in tests: PR titles
# are read from a per-run cache file that each test may seed.
_NAMED_REFS_DIR = tempfile.TemporaryDirectory(prefix="sod-named-refs-")
os.environ["NAMED_REFS_OFFLINE"] = "1"
os.environ["NAMED_REFS_CACHE"] = os.path.join(_NAMED_REFS_DIR.name, "named-refs-cache.json")


def seed_named_refs(titles: dict) -> None:
    """Seed the per-run named-refs cache with ``{url: title}``."""
    rows = {url: {"title": title, "at": 4102444800} for url, title in titles.items()}
    pathlib.Path(os.environ["NAMED_REFS_CACHE"]).write_text(json.dumps(rows), encoding="utf-8")


def load_module():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location("sod_workspace_plan", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load sod-workspace-plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sod_workspace_plan"] = module
    spec.loader.exec_module(module)
    return module


sod = load_module()

TODAY = date(2026, 10, 6)  # Tuesday
NOW = "2026-10-06T14:00:00+00:00"
SLACK_REF = "https://example.slack.com/archives/C00000001/p1700000000000100"
SLACK_REF_THREADED = SLACK_REF + "?thread_ts=1700000000.000050&cid=C00000001"


def workstream(slug, name=None, emoji="🧪", **extra):
    meta = {"slug": slug, "display_name": name or slug.replace("-", " ").title(), "emoji": emoji, "priority": "medium"}
    meta.update(extra)
    return meta


def todo(ws, title, due=None, priority="P2", status="open", source_ref="", **extra):
    item = {
        "id": f"{ws}-{title[:12]}-{due}-{priority}",
        "workstream": ws,
        "title": title,
        "priority": priority,
        "status": status,
        "source_ref": source_ref,
    }
    if due:
        item["due"] = due
    item.update(extra)
    return item


def candidate(sender, body, received="2026-10-05T12:00:00-07:00", source_ref=SLACK_REF_THREADED, **extra):
    item = {
        "source_type": "slack",
        "from": sender,
        "body": body,
        "summary": body,
        "received_at": received,
        "source_ref": source_ref,
        "directed_at_me": True,
    }
    item.update(extra)
    return item


def plan(workstreams, todos=(), calendar=(), comms=(), **kwargs):
    kwargs.setdefault("p0_ids", set())
    kwargs.setdefault("now_iso", NOW)
    return sod.build_plan(
        plan_date=TODAY,
        workstreams=list(workstreams),
        todos=list(todos),
        calendar_events=list(calendar),
        comms_candidates=list(comms),
        **kwargs,
    )


def opened_slugs(result):
    return [entry["slug"] for entry in result["open"]]


class SelectionRuleTests(unittest.TestCase):
    def test_todo_due_today_opens_and_due_tomorrow_stays_closed(self):
        result = plan(
            [workstream("alpha"), workstream("beta")],
            todos=[
                todo("alpha", "Ship the thing", due="2026-10-06"),
                todo("beta", "Ship the other thing", due="2026-10-07"),
            ],
        )
        self.assertEqual(opened_slugs(result), ["alpha"])
        self.assertEqual([entry["slug"] for entry in result["closed"]], ["beta"])
        self.assertEqual(result["open"][0]["reasons"], ["1 to-do due today"])

    def test_done_or_snoozed_todo_due_today_does_not_open(self):
        result = plan(
            [workstream("alpha"), workstream("beta")],
            todos=[
                todo("alpha", "Already done", due="2026-10-06", status="done"),
                todo("beta", "Snoozed", due="2026-10-06", snooze_until="2026-10-09T09:00:00-07:00"),
            ],
        )
        self.assertEqual(opened_slugs(result), [])

    def test_reply_pending_opens_but_answered_ask_does_not(self):
        workstreams = [workstream("alpha"), workstream("beta"), workstream("gamma")]
        asks = [
            candidate("Sasha Example", "<@U045|{{OWNER_NAME_LOWER}}> is 10/9 still the date?", workspace="alpha"),
            candidate(
                "Parker Example",
                "<@U045|{{OWNER_NAME_LOWER}}> can you confirm the launch date?",
                workspace="beta",
                source_ref="https://example.slack.com/archives/C00000002/p1700000000000200",
            ),
            candidate(
                "Xan Example",
                "<@U045|{{OWNER_NAME_LOWER}}> which videos did you mean?",
                workspace="gamma",
                source_ref="https://example.slack.com/archives/C00000003/p1700000000000300",
            ),
        ]
        result = plan(
            workstreams,
            comms=asks,
            replied_refs={sod.normalize_ref(asks[1]["source_ref"])},
            verdicts={sod.normalize_ref(asks[2]["source_ref"]): "resolved_by_owner"},
        )
        self.assertEqual(opened_slugs(result), ["alpha"])
        self.assertEqual(result["open"][0]["reasons"], ["reply pending: Sasha Example"])

    def test_reply_pending_cleared_by_done_todo_or_sent_draft(self):
        ask = candidate("Linden Example", "<@U045|{{OWNER_NAME_LOWER}}> wanna give the task a go?")
        done_todo = todo("alpha", "Try the task", source_ref=SLACK_REF, status="done")
        result = plan([workstream("alpha")], todos=[done_todo], comms=[ask])
        self.assertEqual(opened_slugs(result), [], "a done linked todo means the ask was handled")

        result = plan(
            [workstream("alpha")],
            comms=[dict(ask, workspace="alpha")],
            ledger_items={SLACK_REF: {"stage": "todo", "draft_status": "sent"}},
        )
        self.assertEqual(opened_slugs(result), [], "a sent draft means the ask was answered")

    def test_live_reply_check_drops_ask_owner_already_answered(self):
        # Rule: re-read the live thread before asking the owner to reply.
        asks = [
            candidate("Sasha Example", "<@U045|{{OWNER_NAME_LOWER}}> is 10/9 still the date?", workspace="alpha"),
            candidate(
                "Noel Example",
                "<@U045|{{OWNER_NAME_LOWER}}> can you confirm the rollout order?",
                workspace="beta",
                source_ref="https://example.slack.com/archives/C00000002/p1700000000000200",
            ),
        ]
        seen: list[str] = []

        def checker(item):
            seen.append(item["from"])
            if item["from"] == "Noel Example":
                return {"verdict": "resolved_by_owner", "evidence": {"closer_at": "2026-10-06T09:00:00-07:00"}}
            return {"verdict": "open"}

        result = plan([workstream("alpha"), workstream("beta")], comms=asks, reply_checker=checker)
        self.assertEqual(opened_slugs(result), ["alpha"], "Noel's ask was answered live; only Jordan's stays")
        self.assertEqual(sorted(seen), sorted(["Sasha Example", "Noel Example"]), "every surviving ask is re-checked")

    def test_github_backed_todo_is_dropped_when_the_pr_merged_live(self):
        # A stale note can call a merged PR unmerged. The plan re-reads PR state for GitHub-backed to-dos and lists them as resolved, not as asks.
        merged_pr = "https://github.com/acme/example-repo/pull/10003"
        open_pr = "https://github.com/acme/example-repo/pull/10002"
        todos = [
            todo("alpha", "Ping the-pr-author to merge #10002", due="2026-10-06", priority="P1", source_ref=merged_pr),
            todo("alpha", "Review #10002", due="2026-10-06", priority="P2", source_ref=open_pr),
        ]
        checked: list[str] = []

        def checker(item):
            checked.append(item["source_ref"])
            if item["source_ref"] == merged_pr:
                return {"verdict": "resolved_by_other", "kind": "github",
                        "evidence": {"reason": "pr-merged", "closer": "the-pr-author", "closer_at": "2026-10-14T17:09:26Z"}}
            return {"verdict": "open", "evidence": {"reason": "pr-open"}}

        result = plan([workstream("alpha")], todos=todos, reply_checker=checker)
        self.assertEqual(sorted(checked), sorted([merged_pr, open_pr]), "every GitHub-backed to-do is re-read live")
        self.assertEqual(opened_slugs(result), ["alpha"], "the still-open PR keeps the tab")
        seed = result["open"][0]["seed"]
        self.assertIn("Review #10002", seed)
        self.assertNotIn("Ping the-pr-author", seed, "a merged PR is never seeded as an ask")
        self.assertEqual([r["title"] for r in result["todos_resolved"]], ["Ping the-pr-author to merge #10002"])
        self.assertEqual(result["todos_resolved"][0]["label"], "already merged by the-pr-author Oct 14 10:09 AM PT")
        self.assertIn("Already resolved on GitHub", sod.render_text(result))
        # Without a live checker (dry preview) nothing is dropped and the ask stays visible.
        offline = plan([workstream("alpha")], todos=todos, reply_checker=None)
        self.assertEqual(offline["todos_resolved"], [])
        self.assertIn("Ping the-pr-author", offline["open"][0]["seed"])

    def test_review_framed_github_todo_is_dropped_when_owner_is_not_a_requested_reviewer(self):
        # Only an explicit review request goes on the review list. A "Review …" to-do whose live PR requests only a team
        # the owner is on (fan-out) or where the owner is the author is a status line, not a seed.
        # An explicitly requested review stays; an unverified role fails closed but is
        # not dropped on a guess.
        fanout = "https://github.com/acme/example-repo/pull/10004"
        asked = "https://github.com/acme/agents/pull/10001"
        own = "https://github.com/acme/example-repo/pull/10002"
        unverified = "https://github.com/acme/example-web/pull/10005"
        todos = [
            todo("alpha", "Review GitHub PR: sampleproject retries #10004", due="2026-10-06", priority="P1", source_ref=fanout),
            todo("alpha", "Review GitHub PR: eval harness #10001", due="2026-10-06", priority="P1", source_ref=asked),
            todo("alpha", "Get review and merge example-repo#10002", due="2026-10-06", priority="P1", source_ref=own),
            todo("alpha", "Review example-web PR #10005", due="2026-10-06", priority="P1", source_ref=unverified),
        ]
        roles = {fanout: "team-fanout", asked: "requested-reviewer", own: "author", unverified: ""}

        def checker(item):
            evidence = {"reason": "pr-open"}
            if roles[item["source_ref"]]:
                evidence["owner_role"] = roles[item["source_ref"]]
            return {"verdict": "open", "kind": "github", "evidence": evidence}

        result = plan([workstream("alpha")], todos=todos, reply_checker=checker)
        seed = result["open"][0]["seed"]
        self.assertNotIn("#10004", seed, "team fan-out is never seeded as a review ask")
        self.assertIn("#10001", seed, "an explicitly requested review stays")
        self.assertIn("#10002", seed, "the owner's own PR is not review-framed, so it is their work and stays")
        self.assertIn("#10005", seed, "an unverified role is not dropped on a guess")
        self.assertEqual([r["url"] for r in result["todos_not_asked"]], [fanout])
        self.assertIn("team-fanout", result["todos_not_asked"][0]["label"])
        self.assertEqual(result["todos_resolved"], [])
        text = sod.render_text(result)
        self.assertIn("Not review asks", text)
        self.assertIn("#10004", text)

    def test_live_reply_check_failure_keeps_ask_open(self):
        ask = candidate("Sasha Example", "<@U045|{{OWNER_NAME_LOWER}}> is 10/9 still the date?", workspace="alpha")

        def broken(_item):
            raise RuntimeError("slack rate limited")

        result = plan([workstream("alpha")], comms=[ask], reply_checker=broken)
        self.assertEqual(opened_slugs(result), ["alpha"], "an unknown verdict never hides an ask")

    def test_ledger_superseded_draft_means_ask_was_answered(self):
        ask = candidate("Noel Example", "<@U045|{{OWNER_NAME_LOWER}}> can you confirm the rollout order?", workspace="alpha")
        superseded = {
            "stage": "draft",
            "draft_status": "dismissed",
            "draft_meta": {"reason": "owner-already-replied"},
            "superseded": {"at": "2026-10-06T16:00:00+00:00", "verdict": "resolved_by_owner"},
        }
        result = plan([workstream("alpha")], comms=[ask], ledger_items={SLACK_REF: superseded})
        self.assertEqual(opened_slugs(result), [])

        other = {"stage": "draft", "draft_status": "dismissed", "draft_meta": {"reason": "resolved-by-other"}}
        result = plan([workstream("alpha")], comms=[ask], ledger_items={SLACK_REF: other})
        self.assertEqual(opened_slugs(result), [], "a dismissal because someone else resolved it is also answered")

        unrelated = {"stage": "draft", "draft_status": "dismissed", "draft_meta": {"reason": "not-needed"}}
        result = plan([workstream("alpha")], comms=[ask], ledger_items={SLACK_REF: unrelated})
        self.assertEqual(opened_slugs(result), ["alpha"], "other dismissal reasons do not prove an answer")

    def test_slack_mention_without_an_ask_does_not_open(self):
        result = plan(
            [workstream("alpha")],
            comms=[
                candidate("Ivy Example", "cc <@U045|{{OWNER_NAME_LOWER}}>", workspace="alpha"),
                candidate("Indigo Example", "thanks <@U045|{{OWNER_NAME_LOWER}}>!", workspace="alpha"),
            ],
        )
        self.assertEqual(opened_slugs(result), [])

    def test_ask_outside_reply_window_or_from_owner_does_not_open(self):
        result = plan(
            [workstream("alpha")],
            comms=[
                candidate("Old Sender", "can you look?", received="2026-10-02T12:00:00-07:00", workspace="alpha"),
                candidate("{{OWNER_NAME_LOWER}}", "can you look?", workspace="alpha"),
            ],
        )
        self.assertEqual(opened_slugs(result), [])

    def test_own_message_by_identity_does_not_open_even_with_a_renamed_display_name(self):
        # Slack identity (user id / username), not the display name, says this is the owner's own post.
        rc = sod.reply_check_module()
        owner_handle = next(iter(rc.OWNER_SLACK_USERNAMES))
        result = plan(
            [workstream("alpha")],
            comms=[
                candidate("Renamed Owner (they/them)", "can you look?", workspace="alpha", from_handle=owner_handle, from_user_id=rc.OWNER_SLACK_USER_ID),
                candidate("Renamed Owner (they/them)", "can you look?", workspace="alpha", from_handle=owner_handle, from_user_id=None),
            ],
        )
        self.assertEqual(opened_slugs(result), [])
        # Another identity with author fields present is a real ask.
        result = plan(
            [workstream("alpha")],
            comms=[candidate("Drew Example", "can you look?", workspace="alpha", from_handle="dana-example", from_user_id="W-other")],
        )
        self.assertEqual(opened_slugs(result), ["alpha"])

    def test_unattributed_ask_opens_nothing_but_is_reported(self):
        result = plan([workstream("alpha")], comms=[candidate("Drew Example", "any update on this?")])
        self.assertEqual(opened_slugs(result), [])
        self.assertEqual([r["from"] for r in result["replies_unattributed"]], ["Drew Example"])

    def test_p0_opens_without_a_due_date_and_names_the_tab(self):
        p0 = todo("alpha", "Triage for pilot accounts", priority="P0")
        result = plan(
            [workstream("alpha", name="Apps Prototype", emoji="🎨"), workstream("beta")],
            todos=[p0, todo("beta", "Future", due="2026-10-20", priority="P0")],
            p0_ids={p0["id"]},
        )
        self.assertEqual(opened_slugs(result), ["alpha"])
        entry = result["open"][0]
        self.assertTrue(entry["p0"])
        self.assertEqual(entry["reasons"], ["P0"])
        self.assertEqual(entry["tab_title"], "🎨 P0 · Apps Prototype")
        self.assertEqual(entry["priority"], "high")

    def test_meeting_today_opens_but_todo_calendar_hold_does_not(self):
        workstreams = [
            workstream("team-notes", name="Team Notes", signal_match_terms=["design weekly"]),
            workstream("example-cli-beta", name="Example Cli Beta"),
        ]
        calendar = [
            {"summary": "Team Notes Weekly", "start": "2026-10-06T13:35:00-07:00", "all_day": False, "link": "https://cal/1"},
            {"summary": "[Todo] Reply to Jordan about example cli beta", "start": "2026-10-06T13:00:00-07:00", "all_day": False, "link": ""},
            {"summary": "the owner / Alder", "start": "2026-10-06T10:00:00-07:00", "all_day": False, "link": ""},
        ]
        result = plan(workstreams, calendar=calendar)
        self.assertEqual(opened_slugs(result), ["team-notes"])
        self.assertEqual(result["open"][0]["reasons"], ["meeting today: Team Notes Weekly"])
        self.assertIn("- Meeting 1:35 PM: [Team Notes Weekly](https://cal/1)", result["open"][0]["seed"])
        self.assertEqual(result["meetings_unmatched"], ["the owner / Alder"])

    def test_workstream_with_nothing_today_stays_closed(self):
        result = plan(
            [workstream("quiet", watched_docs=[{"url": "https://docs.google.com/document/d/abc/edit", "label": "Plan"}])],
            todos=[
                todo("quiet", "Later", due="2026-10-09"),
                todo("quiet", "Overdue P2 yesterday", due="2026-10-05", priority="P2"),
            ],
        )
        self.assertEqual(opened_slugs(result), [])
        self.assertEqual(result["closed"][0]["reason"], "nothing due today")

    def test_overdue_p1_fills_missed_only_when_tab_opens_for_another_reason(self):
        todos = [
            todo("alpha", "Overdue P1", due="2026-10-05", priority="P1", source_ref="https://example.com/a"),
            todo("alpha", "Today", due="2026-10-06"),
            todo("beta", "Overdue alone", due="2026-10-05", priority="P1"),
        ]
        result = plan([workstream("alpha"), workstream("beta")], todos=todos)
        self.assertEqual(opened_slugs(result), ["alpha"])
        seed = result["open"][0]["seed"].splitlines()
        self.assertIn("Missed yesterday (urgent or important only)", seed)
        self.assertIn("- P1 due 2026-10-05: [Overdue P1](https://example.com/a)", seed)

    def test_monday_missed_window_reaches_back_to_friday(self):
        missed_floor, reply_floor = sod.plan_dates(date(2026, 10, 5))
        self.assertEqual((missed_floor, reply_floor), ("2026-10-02", "2026-10-02"))
        self.assertEqual(sod.plan_dates(TODAY), ("2026-10-05", "2026-10-05"))


class SeedNoteTests(unittest.TestCase):
    def test_headings_in_order_and_empty_sections_omitted(self):
        ws = workstream(
            "alpha",
            watched_docs=[{"url": "https://docs.google.com/document/d/doc1/edit", "label": "Launch plan"}],
        )
        todos = [todo("alpha", "Reply to Jordan", due="2026-10-06", priority="P1", source_ref=SLACK_REF)]
        comms = [candidate("Sasha Example", "<@U045|{{OWNER_NAME_LOWER}}> should I start this in our 1:1?")]
        result = plan([ws], todos=todos, comms=comms, channel_names={"C00000001": "team-notes"})
        seed = result["open"][0]["seed"].splitlines()
        headings = [line for line in seed if not line.startswith("- ")]
        self.assertEqual(headings, ["To dos today", "Reply to", "Docs"])
        self.assertEqual(seed[1], f"- P1: [Reply to Jordan]({SLACK_REF})")
        self.assertEqual(seed[3], f"- Sasha Example (#team-notes): [should I start this in our 1:1?]({SLACK_REF_THREADED})")
        self.assertEqual(seed[5], "- [Launch plan](https://docs.google.com/document/d/doc1/edit)")

    def test_pr_todo_leads_with_its_title_and_keeps_the_number_after_it(self):
        # A PR is named by its title, never by its number alone.
        url = "https://github.com/acme/example-repo/pull/10001"
        seed_named_refs({url: "Docs search skill"})
        try:
            todos = [todo("alpha", "Merge example-repo#10001", due="2026-10-06", priority="P1", source_ref=url)]
            seed = plan([workstream("alpha")], todos=todos)["open"][0]["seed"].splitlines()
            self.assertEqual(seed[1], f"- P1: Merge [Docs search skill]({url}) (example-repo#10001)")
            # Without a cached title the repo names the PR; the number still never leads.
            seed_named_refs({})
            seed = plan([workstream("alpha")], todos=todos)["open"][0]["seed"].splitlines()
            self.assertEqual(seed[1], f"- P1: Merge [the example-repo PR]({url}) (example-repo#10001)")
        finally:
            seed_named_refs({})

    def test_seed_stays_within_twelve_lines(self):
        ws = workstream("alpha", watched_docs=[{"url": f"https://docs.google.com/document/d/d{i}/edit", "label": f"Doc {i}"} for i in range(5)])
        todos = [todo("alpha", f"Task {i}", due="2026-10-06") for i in range(8)]
        todos += [todo("alpha", f"Missed {i}", due="2026-10-05", priority="P1") for i in range(4)]
        comms = [
            candidate(f"Person {i}", "can you look at this?", workspace="alpha",
                      source_ref=f"https://example.slack.com/archives/C00000001/p170000000000050{i}")
            for i in range(5)
        ]
        result = plan([ws], todos=todos, comms=comms)
        entry = result["open"][0]
        self.assertLessEqual(entry["seed_line_count"], 12)
        self.assertEqual(entry["seed_line_count"], len(entry["seed"].splitlines()))
        self.assertTrue(entry["seed"].startswith("To dos today\n"))

    def test_seed_text_has_no_em_dash_exclamation_or_table(self):
        todos = [todo("alpha", "Fix — the thing!", due="2026-10-06", source_ref="https://example.com/x")]
        result = plan([workstream("alpha")], todos=todos)
        seed = result["open"][0]["seed"]
        self.assertNotIn("—", seed)
        self.assertNotIn("!", seed)
        self.assertNotIn("|", seed)

    def test_docs_skip_links_already_in_todo_lines_and_label_unlabelled_watched_docs(self):
        doc = "https://docs.google.com/document/d/doc1/edit?tab=t.0"
        ws = workstream(
            "alpha",
            name="Web Search",
            watched_docs=[{"url": doc, "snapshot": "sources/web-search-prd.md"}, {"url": "https://docs.google.com/document/d/doc2/edit", "label": "Order form"}],
        )
        todos = [todo("alpha", "Comment on the PRD", due="2026-10-06", source_ref=doc)]
        seed = plan([ws], todos=todos)["open"][0]["seed"].splitlines()
        self.assertEqual(seed[-2:], ["Docs", "- [Order form](https://docs.google.com/document/d/doc2/edit)"])

    def test_channel_labels_for_dm_and_group_dm(self):
        names = {"C00000001": "mpdm-foo--bar-1"}
        self.assertEqual(sod.channel_label(candidate("x", "y"), names), "Slack group DM")
        dm = candidate("x", "y", source_ref="https://example.slack.com/archives/D00000001/p1700000000000400")
        self.assertEqual(sod.channel_label(dm, {}), "Slack DM")
        self.assertEqual(sod.channel_label(candidate("x", "y", source_type="gmail"), {}), "Gmail")


class CompatPlanTests(unittest.TestCase):
    def test_write_schema_matches_start_day_phase_five(self):
        p0 = todo("alpha", "Concierge", priority="P0")
        result = plan([workstream("alpha", name="Alpha", emoji="🅰️")], todos=[p0], p0_ids={p0["id"]})
        compat = sod.compat_plan(result, pathlib.Path("/repo"), "/repo/notes/daily/2026-10-06.md")
        self.assertEqual(compat["plan_source"], "sod-workspace-plan")
        self.assertEqual(compat["today_note"], "notes/daily/2026-10-06.md")
        entry = compat["workstreams_to_open"][0]
        self.assertEqual(entry["name"], "alpha")
        self.assertEqual(entry["tab_title"], "🅰️ P0 · Alpha")
        self.assertTrue(entry["open_now"])
        self.assertIn("To dos today", entry["prompt"])
        self.assertIn("No summary table", entry["prompt"])
        self.assertEqual(compat["workstreams_skipped"], [])


class CliTests(unittest.TestCase):
    def test_dry_run_prints_plan_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ws = root / "workstreams" / "alpha"
            ws.mkdir(parents=True)
            (ws / "CONTEXT.md").write_text("# Alpha\n\n**Status:** Active\n")
            (ws / "config.yaml").write_text("name: Alpha\nemoji: '🅰️'\nstatus: active\n")
            (root / "system" / "state" / "sources").mkdir(parents=True)
            (root / "system" / "state" / "todo.json").write_text(json.dumps({"items": [todo("alpha", "Today", due="2026-10-06")]}))
            (root / "system" / "state" / "sources" / "comms.json").write_text(json.dumps({"candidates": []}))
            # The CLI's live reply check never calls a provider from tests.
            env = {**os.environ, "REPLY_CHECK_OFFLINE": "1", "THREAD_GATE_OFFLINE": "1"}
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root), "--date", "2026-10-06", "--no-calendar", "--dry-run"],
                capture_output=True, text=True, check=True, env=env,
            )
            self.assertIn("Open (1):", proc.stdout)
            self.assertIn("🅰️ Alpha", proc.stdout)
            self.assertIn("- P2: Today", proc.stdout)
            self.assertFalse((root / "system" / "today-plan.json").exists())

            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root), "--date", "2026-10-06", "--no-calendar", "--write"],
                capture_output=True, text=True, check=True, env=env,
            )
            written = json.loads((root / "system" / "today-plan.json").read_text())
            self.assertEqual([w["name"] for w in written["workstreams_to_open"]], ["alpha"])
            self.assertEqual(json.loads(proc.stdout)["open"], ["alpha"])


if __name__ == "__main__":
    unittest.main()
