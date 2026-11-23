#!/usr/bin/env python3
"""Regressions for two dispatch stall classes, plus an owner misclassification.

Hermetic: temp ledger and state dir, a stub cmux that lists no workspaces, and
patched live_agents / git. Each test names the wrong implementation it catches.

1. Idle-route receipts could not conclude "not started": the verifier treated
   the owner tab as a permanent candidate and reported UNKNOWN OUTCOME forever,
   and `dispatch` answered already-live for any routing/active row. A row with
   no live thread and no fresh stamp must resolve to not-started and allow a
   new attempt; a row whose thread is still an Amp agent must stay already-live.
2. A receipt left "blocked-idle-outcome-unknown" while the ledger shows the
   same attempt acked must be reconciled by the sweep.
3. dispatch.py ignored an explicit "Workstream: <slug>" prefix and chose the
   lexically closer owner. The directive wins.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))
import scoped_test_env  # noqa: E402  (env stubs and script imports stay in this module)

_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
(TMP / "state" / "worker-dispatch").mkdir(parents=True)
STUB = TMP / "cmux"
# Bytes, not write_text: on Windows text mode writes CRLF, and bash then fails on every line of the stub.
STUB.write_bytes(b'#!/usr/bin/env bash\nif [ "$1" = rpc ]; then echo \'{"workspaces":[]}\'; fi\nexit 0\n')
STUB.chmod(STUB.stat().st_mode | stat.S_IEXEC)
# Applied only while this module loads and runs (scoped_test_env), never left in
# os.environ for another suite in the same process.
ENV = {
    "SWITCHBOARD_TEST_SANDBOX": "1",   # never a live cmux or wmux tab
    "REPO_ROOT": str(REPO_ROOT),
    "SWITCHBOARD_STATE_DIR": str(TMP / "state"),
    "RUNNING_TASKS_FILE": str(TMP / "state" / "running-tasks.json"),
    "CMUX_BIN": str(STUB),
    "CC_CMUX_SESSION_JSON": str(TMP / "no-session-store.json"),
    "SWITCHBOARD_WORKTREES_DIR": str(TMP / "worktrees"),
    "SWITCHBOARD_LOCKS_DIR": str(TMP / "locks"),
    # Every test below that patches dispatch.launch expects the inline launch;
    # AsyncLaunchTests clears this to exercise the detached path.
    "SWITCHBOARD_DISPATCH_SYNC": "1",
    # No test reads a real Drive, Slack, Jira, Linear, or GitHub title; the
    # linked-title tests patch dispatch.link_titles.fetch_titles.
    "SWITCHBOARD_LINK_TITLES_OFFLINE": "1",
    # A queued steer never spawns a real deliver-steers process from a test; the
    # queued-steer tests call deliver_queued_steers directly.
    "SWITCHBOARD_NO_STEER_SPAWN": "1",
}
setUpModule, tearDownModule = scoped_test_env.module_hooks(ENV)

with scoped_test_env.isolated_load(ENV):
    _spec = importlib.util.spec_from_file_location("dispatch", SCRIPTS / "dispatch.py")
    dispatch = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(dispatch)  # type: ignore[union-attr]
ledger = dispatch.ledger
REAL_LIVE_AGENTS = ledger.live_agents   # captured before HermeticRepo stubs it; store-fixture tests run the real reader
REAL_CMUX_WORKSPACES = dispatch.cmux_workspaces


def _symlinks_work() -> bool:
    """Windows without Developer Mode refuses os.symlink (WinError 1314)."""
    probe = TMP / "symlink-probe"
    try:
        os.symlink(TMP, probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


HAS_SYMLINKS = _symlinks_work()
needs_symlinks = unittest.skipUnless(HAS_SYMLINKS, "real symlinks need Developer Mode or admin on Windows")


def needs_workstream(slug: str):
    """Skip a test that ranks against the real workstreams tree when `slug` is absent.
    The portable kit ships no workstreams (private content), so on a kit install the
    real ranking has no owner to find and returns an empty slug."""
    return unittest.skipUnless((REPO_ROOT / "workstreams" / slug / "CONTEXT.md").is_file(),
                               f"workstreams/{slug} is not in this checkout (the kit ships no workstreams)")


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_ledger(*tasks: dict) -> None:
    Path(os.environ["RUNNING_TASKS_FILE"]).write_text(json.dumps({"tasks": list(tasks)}))


def run_dispatch(**overrides) -> tuple[int, dict]:
    args = argparse.Namespace(prompt=["do the thing"], origin="cos", workstream="switchboard-maintenance", task_id=None,
                              depends_on=[], continuation_of=None, request_id=None, delivery=None, force_redispatch=False)
    for key, value in overrides.items():
        setattr(args, key, value)
    out, err = io.StringIO(), io.StringIO()
    # A pinned slug exists for these tests. Without this, an unknown slug is scaffolded by the
    # real new-workstream.py, whose root is the checkout running the tests.
    real_exists = dispatch.workstream_exists
    with patch.object(dispatch, "workstream_exists", side_effect=lambda slug: slug == args.workstream or real_exists(slug)), \
            redirect_stdout(out), redirect_stderr(err):
        code = dispatch.command_dispatch(args)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


class NotStartedResolutionTests(unittest.TestCase):
    """Stall class 1: a routing row with nobody behind it."""

    def setUp(self) -> None:
        self.old = datetime.now(timezone.utc) - timedelta(hours=1)
        self.row = {"id": "rt-000000000046", "status": "routing", "attempt": 1, "attempt_id": "rt-000000000046.a1.96bd22",
                    "workstream": "book-club", "origin": "cos", "prompt": "set up the appointment schedule",
                    "workspace_ref": "workspace:541", "created_at": iso(self.old), "updated_at": iso(self.old),
                    "stale_since": iso(self.old + timedelta(minutes=30))}
        write_ledger(self.row)
        # The owner tab exists and is idle: the old verifier looped on it forever.
        owner_tab = {"ref": "workspace:541", "id": "4E901FE2", "title": "book-club · owner"}
        for p in [patch.object(dispatch, "cmux_workspaces", return_value=[owner_tab]),
                  patch.object(ledger, "live_agents", return_value={}),
                  patch.object(dispatch, "launch", side_effect=self.fake_launch),
                  patch.object(dispatch, "reap_all", return_value={"closed": [], "skipped": []})]:
            p.start()
            self.addCleanup(p.stop)

    def fake_launch(self, task: dict, cfg: dict) -> dict:
        def bind(payload):
            row = ledger.find_task(payload, task["id"])
            row.update(workspace_ref="workspace:999", worktree=str(TMP / "wt"), branch="task/x", worker_id="worker-aaaaaaaaaaaa",
                       status="routing", summary="started", updated_at=ledger.now_iso())
            return row
        return ledger.locked_mutation(bind)

    def test_idle_owner_tab_is_not_a_live_worker(self) -> None:
        # Wrong implementation: "workspace_ref present in cmux" => already-live (the old permanent-candidate loop).
        evidence = dispatch.not_started_evidence(self.row)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["verdict"], "not-started")
        self.assertTrue(evidence["workspace_present"])
        self.assertFalse(evidence["worker_tab"])
        self.assertGreaterEqual(evidence["idle_seconds"], 3500)

    def test_dispatch_replaces_never_started_attempt(self) -> None:
        code, result = run_dispatch(task_id="rt-000000000046", workstream="book-club")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["replaced_attempt"]["attempt_id"], "rt-000000000046.a1.96bd22")
        self.assertEqual(result["replaced_attempt"]["verdict"], "not-started")
        row = ledger.find_task(ledger.load_payload(), "rt-000000000046")
        self.assertEqual(row["attempt"], 2)
        self.assertTrue(row["attempt_id"].startswith("rt-000000000046.a2."))
        self.assertNotIn("stale_since", row)

    def test_live_thread_keeps_already_live(self) -> None:
        # Wrong implementation: treating every stale_since row as not-started would duplicate a slow but live worker.
        self.row["thread_id"] = "T-live"
        write_ledger(self.row)
        with patch.object(ledger, "live_agents", return_value={"T-live": {"state": "Idle", "workspace_id": "x"}}):
            self.assertIsNone(dispatch.not_started_evidence(self.row))
            code, result = run_dispatch(task_id="rt-000000000046", workstream="book-club")
        self.assertEqual((code, result["status"]), (0, "already-live"))
        self.assertEqual(ledger.find_task(ledger.load_payload(), "rt-000000000046")["attempt"], 1)

    def test_fresh_row_is_not_judged_yet(self) -> None:
        # Wrong implementation: "no thread bound" alone => not-started would re-dispatch a worker that is still booting.
        fresh = dict(self.row, created_at=ledger.now_iso(), updated_at=ledger.now_iso(), worker_id="worker-bbbbbbbbbbbb")
        fresh.pop("stale_since")
        self.assertIsNone(dispatch.not_started_evidence(fresh))

    def test_abandoned_worktree_with_work_is_not_silently_discarded(self) -> None:
        self.row["worktree"] = str(TMP / "dirty-wt")
        (TMP / "dirty-wt").mkdir(exist_ok=True)
        write_ledger(self.row)
        fake = lambda *a, cwd=None, check=True: argparse.Namespace(stdout=" M file.md\n" if a[0] == "status" else "", returncode=0)
        with patch.object(dispatch, "git", side_effect=fake):
            code, result = run_dispatch(task_id="rt-000000000046", workstream="book-club")
        self.assertEqual((code, result["status"]), (3, "stalled"))
        self.assertEqual(result["evidence"]["verdict"], "abandoned-with-work")
        self.assertEqual(ledger.find_task(ledger.load_payload(), "rt-000000000046")["attempt"], 1)


class ReceiptReconciliationTests(unittest.TestCase):
    """Stall class 1, aftermath: the receipt kept saying UNKNOWN OUTCOME after the ack landed."""

    def setUp(self) -> None:
        self.receipt = TMP / "state" / "worker-dispatch" / "f91a2195bcf03164c8be64da.json"
        self.receipt.write_text(json.dumps({"task_id": "rt-000000000046", "attempt_id": "rt-000000000046.a1.96bd22",
                                            "seed_status": "blocked-idle-outcome-unknown", "workspace_mode": "idle-route"}))
        self.addCleanup(lambda: self.receipt.unlink(missing_ok=True))

    def test_acked_attempt_reconciles_blocked_receipt(self) -> None:
        payload = {"tasks": [{"id": "rt-000000000046", "status": "blocked", "attempt_id": "rt-000000000046.a1.96bd22",
                              "thread_id": "T-example"}]}
        changed = dispatch.reconcile_receipts(payload)
        self.assertEqual([c["was"] for c in changed], ["blocked-idle-outcome-unknown"])
        after = json.loads(self.receipt.read_text())
        self.assertEqual(after["seed_status"], "reconciled")
        self.assertIn("T-example", after["reconciled_by"])

    def test_unacked_or_different_attempt_stays_blocked(self) -> None:
        # Wrong implementation: reconciling on task id alone would hide a genuinely unstarted route.
        for row in ({"id": "rt-000000000046", "status": "routing", "attempt_id": "rt-000000000046.a1.96bd22"},
                    {"id": "rt-000000000046", "status": "done", "attempt_id": "rt-000000000046.a2.06379e", "thread_id": "T-x"}):
            self.assertEqual(dispatch.reconcile_receipts({"tasks": [row]}), [])
            self.assertEqual(json.loads(self.receipt.read_text())["seed_status"], "blocked-idle-outcome-unknown")


class ExplicitWorkstreamTests(unittest.TestCase):
    """The directive in the ask outranks lexical overlap (hermetic: temp workstreams tree)."""

    PROMPT = ("Workstream: switchboard-maintenance (fold into the open reliability review). Workers are not "
              "resuming; add the two new stall classes to the fix and its regression tests.")
    CONTINUATION_PROMPT = ("Workstream: example-integrations (google-workspace substream). "
                           "Continuation of rt-000000000047. Finish the meeting packet.")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for slug in ("switchboard-maintenance", "example-integrations", "meet-example"):
            (root / "workstreams" / slug).mkdir(parents=True)
            (root / "workstreams" / slug / "CONTEXT.md").write_text("# fixture\n")
        patcher = patch.object(dispatch, "REPO_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pick_workstream_honors_directive(self) -> None:
        pack = {"explicit_workstream": "switchboard-maintenance",
                "candidate_owners": [{"slug": "meet-example", "score": 408.5},
                                     {"slug": "switchboard-maintenance", "score": 264.9}]}
        with patch.object(dispatch, "run", return_value=argparse.Namespace(stdout=json.dumps(pack), returncode=0)):
            self.assertEqual(dispatch.pick_workstream(self.PROMPT, dispatch.DEFAULTS), "switchboard-maintenance")

    def test_unknown_directive_falls_back_to_ranking(self) -> None:
        pack = {"explicit_workstream": "no-such-workstream",
                "candidate_owners": [{"slug": "switchboard-maintenance", "score": 100}]}
        with patch.object(dispatch, "run", return_value=argparse.Namespace(stdout=json.dumps(pack), returncode=0)):
            self.assertEqual(dispatch.pick_workstream("x", dispatch.DEFAULTS), "switchboard-maintenance")

    def test_explicit_directive_beats_ranking_without_consulting_context_pack(self) -> None:
        # Wrong impl: fuzzy ranking runs first and its top candidate wins.
        with patch.object(dispatch, "run", side_effect=AssertionError("ranking must not run")):
            self.assertEqual(dispatch.pick_workstream(self.CONTINUATION_PROMPT, dispatch.DEFAULTS),
                             "example-integrations")

    def test_continuation_inherits_parent_workstream_before_ranking(self) -> None:
        # Wrong impl: --continuation-of is recorded but the picker still ranks the prompt.
        payload = {"tasks": [{"id": "rt-000000000047", "workstream": "example-integrations", "status": "done"}]}
        with patch.object(dispatch.ledger, "load_payload", return_value=payload), \
                patch.object(dispatch, "run", side_effect=AssertionError("ranking must not run")):
            self.assertEqual(dispatch.pick_workstream("Continuation: finish the meeting packet.", dispatch.DEFAULTS,
                                                      continuation_of="rt-000000000047"), "example-integrations")
        # A parent with no (or a deleted) workstream falls through to ranking (a named signal, so the
        # existing owner wins).
        pack = {"candidate_owners": [{"slug": "meet-example", "score": 999, "evidence": ["exact workstream name"]}]}
        with patch.object(dispatch.ledger, "load_payload", return_value={"tasks": [{"id": "rt-000000000047"}]}), \
                patch.object(dispatch, "run", return_value=argparse.Namespace(stdout=json.dumps(pack), returncode=0)):
            self.assertEqual(dispatch.pick_workstream("finish the meeting packet", dispatch.DEFAULTS,
                                                      continuation_of="rt-000000000047"), "meet-example")


class MaintenanceMisfileWarningTests(unittest.TestCase):
    """A brief sent to switchboard-maintenance that reads as an owner topic gets a one-line warning and
    still routes. Invented text only."""

    def test_owner_topic_brief_warns(self) -> None:
        # Wrong impl: no check, so a Downloads or DNS brief runs as Switchboard Maintenance in silence.
        for brief in ("Workstream: switchboard-maintenance\nSort the Downloads folder and file the pay stubs.",
                      "Workstream: switchboard-maintenance\nAdd SPF and DMARC records for the example.org domain."):
            warning = dispatch.maintenance_misfile_warning(brief, "switchboard-maintenance")
            self.assertTrue(warning.startswith("warning: this brief reads as an owner topic ("), brief)
            self.assertNotIn("\n", warning)
            self.assertIn("switchboard-maintenance is only for switchboard", warning)

    def test_switchboard_fix_that_mentions_an_owner_topic_stays_quiet(self) -> None:
        # Wrong impl: any owner-topic word warns, so a guardrail fix about Downloads would too.
        brief = ("Workstream: switchboard-maintenance\nThe Downloads folder guardrail blocks a PDF read of an insurance form; "
                 "fix the hook and add a regression test.")
        self.assertEqual(dispatch.maintenance_misfile_warning(brief, "switchboard-maintenance"), "")

    def test_other_owner_or_plain_system_brief_stays_quiet(self) -> None:
        self.assertEqual(dispatch.maintenance_misfile_warning("Sort the Downloads folder.", "organizing-downloads"), "")
        self.assertEqual(dispatch.maintenance_misfile_warning(
            "Workstream: switchboard-maintenance\nWorker tabs vanish after a sweep.", "switchboard-maintenance"), "")
        # One passing owner word is not a topic, and `claude --resume` is not an owner term.
        self.assertEqual(dispatch.maintenance_misfile_warning(
            "Workstream: switchboard-maintenance\nThe board should reopen rows with claude --resume; the old budget link test is moot.",
            "switchboard-maintenance"), "")

    def test_warning_rides_on_the_dispatch_message(self) -> None:
        choice = {"slug": "switchboard-maintenance", "reason": "directive", "created": False, "weak_match": None,
                  "warning": "warning: this brief reads as an owner topic (dns), not switchboard itself"}
        self.assertEqual(dispatch.owner_note(choice), "; warning: this brief reads as an owner topic (dns), not switchboard itself")


class WorkstreamCreationRuleTests(unittest.TestCase):
    """Rule: novel or merely tangential work gets its own
    workstream, a one-shot included; "Workstream: <new-slug>" in the ask creates that slug.
    Before this, min_match_score=20 let any two shared topic terms bind an ask to an existing
    owner (a signup-conversion note scored 73.5 on recipe-search from
    'signup, note, one' and was forced there). Hermetic workstreams tree: three real
    workstreams plus an inbox-only shell, as the archive sweep leaves behind."""

    WEAK = {"slug": "recipe-search", "score": 156.6,
            "evidence": ["shared topic terms: owner, data, matches, open, prompt, search"]}
    STRONG = {"slug": "garden-revamp", "score": 615.4,
              "evidence": ['match term "plant detail"', "shared topic terms: owner, change, dashboard"]}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for slug in ("switchboard-maintenance", "garden-revamp", "recipe-search"):
            (self.repo / "workstreams" / slug).mkdir(parents=True)
            (self.repo / "workstreams" / slug / "CONTEXT.md").write_text(f"# {slug}\n")
        (self.repo / "workstreams" / "old-catalog" / "inbox").mkdir(parents=True)
        self.calls: list[list[str]] = []
        self.patches = [patch.object(dispatch, "REPO_ROOT", self.repo)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def fake_run(self, pack: dict, scaffold_slug: str | None = "zz-derived-slug", scaffold_rc: int = 0):
        def run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[1].endswith("new-workstream.py"):
                if scaffold_rc:
                    return argparse.Namespace(stdout="", stderr="PyYAML not installed", returncode=scaffold_rc)
                slug = cmd[cmd.index("--name") + 1] if "--name" in cmd else scaffold_slug
                (self.repo / "workstreams" / slug).mkdir(parents=True, exist_ok=True)
                (self.repo / "workstreams" / slug / "CONTEXT.md").write_text("# created\n")
                return argparse.Namespace(stdout=json.dumps({"slug": slug, "created": True}), stderr="", returncode=0)
            if cmd[1].endswith("router-context-pack.py"):
                return argparse.Namespace(stdout=json.dumps(pack), stderr="", returncode=0)
            if cmd[0] == "git":   # this fixture is a plain directory; commit_scaffold must bow out, not fail
                return argparse.Namespace(stdout="", stderr="fatal: not a git repository", returncode=128)
            raise AssertionError(f"unexpected command {cmd[:2]}")
        return run

    def scaffold_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1].endswith("new-workstream.py")]

    def test_unknown_directive_creates_that_workstream(self) -> None:
        # Wrong impl (the old one): the directive is dropped and ranking binds the ask to the
        # closest existing owner (here switchboard-maintenance on a real match term).
        pack = {"explicit_workstream": "signup-notes",
                "candidate_owners": [{"slug": "switchboard-maintenance", "score": 100, "evidence": ['match term "switchboard script"']}]}
        with patch.object(dispatch, "run", side_effect=self.fake_run(pack)):
            choice = dispatch.choose_workstream("Workstream: signup-notes. Write a note on signup conversion.",
                                                dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("signup-notes", "directive-created", True))
        (scaffold,) = self.scaffold_calls()
        self.assertEqual(scaffold[scaffold.index("--name") + 1], "signup-notes")
        self.assertTrue(scaffold[2].startswith("Workstream: signup-notes."), "the ask is the scaffold's provenance")
        self.assertFalse(any(c[1].endswith("router-context-pack.py") for c in self.calls), "ranking must not run for a proposed slug")
        self.assertIn("in new workstream signup-notes (proposed in the ask); the owner: keep it, or name the owner",
                      dispatch.owner_note(choice))

    def test_existing_directive_is_not_recreated(self) -> None:
        with patch.object(dispatch, "run", side_effect=AssertionError("nothing to run")):
            choice = dispatch.choose_workstream("Workstream: garden-revamp. Next mockup.", dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("garden-revamp", "directive", False))
        self.assertEqual(dispatch.owner_note(choice), "")

    def test_continuation_outranks_a_new_slug_mentioned_mid_prompt(self) -> None:
        # A continuation stays with its parent when a new slug only appears further down
        # (for example inside the parent's quoted prompt).
        payload = {"tasks": [{"id": "rt-000000000047", "workstream": "recipe-search", "status": "done"}]}
        with patch.object(dispatch.ledger, "load_payload", return_value=payload), \
                patch.object(dispatch, "run", side_effect=AssertionError("nothing to run")):
            choice = dispatch.choose_workstream("Continue the search work.\nOriginal: Workstream: web-search-v2. Go.",
                                                dispatch.DEFAULTS, continuation_of="rt-000000000047")
        self.assertEqual((choice["slug"], choice["reason"]), ("recipe-search", "continuation"))

    def test_continuation_goes_to_a_new_topic_named_on_the_first_line(self) -> None:
        # A continuation of a task misfiled under
        # switchboard-maintenance goes where CoS names it. Wrong impl (the old one): the parent's
        # workstream wins and the new topic is never created.
        payload = {"tasks": [{"id": "rt-000000000047", "workstream": "switchboard-maintenance", "status": "done"}]}
        with patch.object(dispatch.ledger, "load_payload", return_value=payload), \
                patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": []})):
            choice = dispatch.choose_workstream("Workstream: bike-repair-notes\nContinue the repair notes.",
                                                dispatch.DEFAULTS, continuation_of="rt-000000000047")
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]),
                         ("bike-repair-notes", "directive-created", True))
        (scaffold,) = self.scaffold_calls()
        self.assertEqual(scaffold[scaffold.index("--name") + 1], "bike-repair-notes")
        # A reserved slug on the first line still inherits the parent.
        self.calls.clear()
        with patch.object(dispatch.ledger, "load_payload", return_value=payload), \
                patch.object(dispatch, "run", side_effect=AssertionError("nothing to run")):
            choice = dispatch.choose_workstream("Workstream: shared\nContinue.", dispatch.DEFAULTS,
                                                continuation_of="rt-000000000047")
        self.assertEqual((choice["slug"], choice["reason"]), ("switchboard-maintenance", "continuation"))

    def test_reserved_or_private_directive_is_not_created(self) -> None:
        # Wrong impl: "Workstream: _archive" or "Workstream: shared" scaffolds a folder in a reserved place.
        for bad in ("_archive", "shared", "inbox", "readme"):
            self.calls.clear()
            with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": []}, scaffold_rc=2)):
                choice = dispatch.choose_workstream(f"Workstream: {bad}. do a thing", dispatch.DEFAULTS)
            self.assertEqual(choice["slug"], "switchboard-maintenance", bad)
            self.assertFalse(any("--name" in c for c in self.scaffold_calls()), bad)

    def test_lexical_only_match_above_floor_creates_instead_of_binding(self) -> None:
        # Wrong impl (the old one): score 156.6 >= 20 -> recipe-search.
        # The new name "Prompt Box Panel" shares "prompt" with the weak owner, so it is offered.
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.WEAK]},
                                                                     scaffold_slug="prompt-box-panel")):
            choice = dispatch.choose_workstream("Mock a prompt box in a panel", dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("prompt-box-panel", "created", True))
        self.assertEqual(choice["weak_match"], {"slug": "recipe-search", "score": 156.6})
        note = dispatch.owner_note(choice)
        self.assertIn("in new workstream prompt-box-panel (derived from the prompt; closest existing owner "
                      "recipe-search (157, weak match)); the owner: keep it, or name the owner to fold it into", note)
        (scaffold,) = self.scaffold_calls()
        self.assertNotIn("--name", scaffold)

    def test_unrelated_weak_owner_is_not_offered(self) -> None:
        # An unrelated owner was reported as "closest existing owner (1225, weak match)" on
        # filler overlap. No word of the new name is shared and nothing is named, so no
        # fold target is offered.
        unrelated = {"slug": "recipe-search", "score": 1224.9,
                     "evidence": ["shared topic terms: 2026, owner, any, available, category, city"],
                     "shared_terms": ["2026", "owner", "any", "available", "category", "city", "program"]}
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [unrelated]},
                                                                     scaffold_slug="bike-lock-review")):
            choice = dispatch.choose_workstream("I am going to compare bike locks using the review program",
                                                dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("bike-lock-review", "created", True))
        self.assertIsNone(choice["weak_match"])
        note = dispatch.owner_note(choice)
        self.assertNotIn("closest existing owner", note)
        self.assertIn("in new workstream bike-lock-review (derived from the prompt)", note)

    def test_weak_owner_rule(self) -> None:
        lexical = {"slug": "a", "score": 9, "evidence": ["shared topic terms: owner, city"]}
        named = {"slug": "b", "score": 5, "evidence": ["name words in the ask (2 of 3): lock, review"]}
        full = {"slug": "c", "score": 4, "evidence": ["shared topic terms: owner"], "shared_terms": ["owner", "lock"]}
        self.assertIsNone(dispatch.weak_owner([lexical], "bike-lock-review"))
        self.assertEqual(dispatch.weak_owner([lexical, named], "bike-lock-review")["slug"], "b")
        self.assertEqual(dispatch.weak_owner([lexical, full], "bike-lock-review")["slug"], "c")
        self.assertEqual(dispatch.weak_owner([lexical], "city-guide")["slug"], "a")
        self.assertIsNone(dispatch.weak_owner([lexical], "workstream-1a2b3c4d"))

    def test_named_signal_binds_to_the_existing_owner(self) -> None:
        # Wrong impl: every match now creates a workstream, duplicating garden-revamp.
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.STRONG, self.WEAK]})):
            choice = dispatch.choose_workstream("Mock a prompt box in the plant detail panel", dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("garden-revamp", "strong-match", False))
        self.assertEqual(self.scaffold_calls(), [])
        self.assertEqual(dispatch.owner_note(choice), "")

    def test_strong_owner_needs_a_named_signal_and_the_floor(self) -> None:
        # Both sides of the floor: a match term on a terminal owner (220 - 60 = 160) fails a 200 floor and passes 20.
        cfg = dict(dispatch.DEFAULTS, min_match_score=200)
        self.assertFalse(dispatch.strong_owner(dict(self.STRONG, score=160), cfg))
        self.assertTrue(dispatch.strong_owner(dict(self.STRONG, score=160), dispatch.DEFAULTS))
        for evidence in (["shared topic terms: a, b"], ["paused owner"], ["terminal status: done"], []):
            self.assertFalse(dispatch.strong_owner({"slug": "x", "score": 999, "evidence": evidence}, dispatch.DEFAULTS), evidence)
        for evidence in ('match term "plant detail"', "exact slug phrase", "exact workstream name",
                         "exact priority-channel owner", 'explicit directive "Workstream: x"'):
            self.assertTrue(dispatch.strong_owner({"slug": "x", "score": 20, "evidence": [evidence]}, dispatch.DEFAULTS), evidence)
        self.assertFalse(dispatch.strong_owner({"slug": "x", "score": 19.9, "evidence": ["exact slug phrase"]}, dispatch.DEFAULTS))

    def test_top_candidate_without_context_md_is_skipped(self) -> None:
        # old-catalog is an inbox-only shell: ranking may still list it, but it cannot own work.
        shell = {"slug": "old-catalog", "score": 400, "evidence": ["exact slug phrase"]}
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [shell, self.STRONG]})):
            choice = dispatch.choose_workstream("old catalog plant detail", dispatch.DEFAULTS)
        self.assertEqual(choice["slug"], "garden-revamp")
        self.assertFalse(dispatch.workstream_exists("old-catalog"))

    def test_scaffold_failure_falls_back_to_default_and_keeps_the_weak_candidate(self) -> None:
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.WEAK]}, scaffold_rc=1)):
            choice = dispatch.choose_workstream("Mock a prompt box", dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("switchboard-maintenance", "default", False))
        self.assertEqual(choice["weak_match"]["slug"], "recipe-search")
        self.assertEqual(dispatch.owner_note(choice), "")


    START_OF_DAY = ("start of day. Run SWITCHBOARD_ROUTER_ORIGIN=1 bash system/start-day.sh and report back. "
                    "Context from last night's CoS session: the ABC-101 ticket is waiting on review; "
                    "the vendor thread needs a nudge.")
    TICKET = {"slug": "vendor-ticket-followup", "score": 420.0, "evidence": ['match term "abc-101"']}

    def test_system_routine_routes_to_the_default_owner_without_ranking(self) -> None:
        # Wrong impl: with no Workstream line and context naming ABC-101, ranking
        # strong-matches vendor-ticket-followup and the whole start-day runs there.
        # No daily-routines folder in this fixture, so the default owner takes it.
        (self.repo / "workstreams" / "vendor-ticket-followup").mkdir(parents=True)
        (self.repo / "workstreams" / "vendor-ticket-followup" / "CONTEXT.md").write_text("# vendor\n")
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.TICKET]})):
            choice = dispatch.choose_workstream(self.START_OF_DAY, dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("switchboard-maintenance", "system-routine", False))
        self.assertEqual(self.calls, [], "neither ranking nor scaffolding runs for a system routine")
        miss_log = self.repo / "system" / "logs" / dispatch.ROUTER_MISS_LOG
        (row,) = [json.loads(line) for line in miss_log.read_text().splitlines()]
        self.assertEqual((row["kind"], row["chosen"]), ("system-routine", "switchboard-maintenance"))
        self.assertTrue(row["prompt"].startswith("start of day."))

    def test_system_routine_routes_to_its_own_owner_when_it_exists(self) -> None:
        # switchboard-maintenance is for switchboard itself, so system routines get their own owner and the tab reads "📅 Daily Routines <n>".
        for slug in ("daily-routines", "vendor-ticket-followup"):
            (self.repo / "workstreams" / slug).mkdir(parents=True)
            (self.repo / "workstreams" / slug / "CONTEXT.md").write_text(f"# {slug}\n")
        cfg = {**dispatch.DEFAULTS}
        with patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.TICKET]})):
            for prompt in (self.START_OF_DAY, "chief of staff: run start of day (CoS morning workflow) for 2026-11-12 PT",
                           "closing day: write the EOD note"):
                choice = dispatch.choose_workstream(prompt, cfg)
                self.assertEqual((choice["slug"], choice["reason"], choice["created"]),
                                 ("daily-routines", "system-routine", False), prompt)
        self.assertEqual(self.calls, [], "neither ranking nor scaffolding runs for a system routine")
        self.assertEqual(dispatch.DEFAULTS["system_routine_workstream"], "daily-routines")
        self.assertNotEqual(dispatch.DEFAULTS["system_routine_workstream"], dispatch.DEFAULTS["default_workstream"])

    def test_system_routine_phrasings_and_near_misses(self) -> None:
        for prompt in ("Start of day", "start my day please", "good morning, run start-day", "SOD",
                       "closing day", "Close the day and write the EOD note", "end of day", "eod",
                       "Run SWITCHBOARD_ROUTER_ORIGIN=1 bash system/start-day.sh", "chief of staff: morning triage"):
            self.assertTrue(dispatch.system_routine(prompt), prompt)
        for prompt in ("Write the ABC-101 vendor nudge; close the day on it by 5pm",
                       "Sodium levels in the demo data look off",
                       "Mock a prompt box in the plant detail panel",
                       "Draft the start-of-week note for the team"):
            self.assertFalse(dispatch.system_routine(prompt), prompt)

    def test_explicit_directive_still_beats_the_system_routine_guard(self) -> None:
        with patch.object(dispatch, "run", side_effect=self.fake_run({})):
            choice = dispatch.choose_workstream("Workstream: garden-revamp\nstart of day for the billing demo", dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"]), ("garden-revamp", "directive"))



class LinkedTitleOwnerTests(WorkstreamCreationRuleTests):
    """A CoS ask with no Workstream: line, "Pre-review of a Google Doc ... <docs link>",
    must not scaffold pre-review-google ("derived from the prompt") when the doc
    title, "Acme_Roadmap_Working Draft.docx", names the existing owner acme-plans.
    Linked titles are resolved and ranked before any new workstream is named."""

    PARTNER_PROMPT = ("Pre-review of a Google Doc: Can you review this and let me know if "
                      "there's anything I need to call out: "
                      "https://docs.google.com/document/d/file0000000001/edit")
    AMA_PROMPT = ("Can you create a doc and help me fill this out: "
                  "https://example.slack.com/archives/C0AAAAAAAA/p1700000000000001")
    PARTNER_TITLE = [{"url": "https://docs.google.com/document/d/file0000000001/edit", "kind": "drive",
                   "raw": "Acme_Roadmap_Working Draft.docx", "title": "Acme Roadmap"}]
    PROMPT_ONLY = {"candidate_owners": [{"slug": "recipe-search", "score": 268.1,
                                         "evidence": ["shared topic terms: 2026, owner, anything, call, doc, google"]}]}
    TITLED = {"candidate_owners": [{"slug": "acme-plans", "score": 335.6,
                                    "evidence": ['source title "Acme Roadmap" names this workstream (acme)']}]}

    def setUp(self) -> None:
        super().setUp()
        for slug in ("acme-plans",):
            (self.repo / "workstreams" / slug).mkdir(parents=True)
            (self.repo / "workstreams" / slug / "CONTEXT.md").write_text(f"# {slug}\n")

    def titled_run(self, titled_pack: dict, scaffold_slug: str = "acme-roadmap"):
        plain = self.fake_run(self.PROMPT_ONLY, scaffold_slug=scaffold_slug)

        def run(cmd, **kw):
            if cmd[1].endswith("router-context-pack.py") and any(str(a).startswith("--source-title=") for a in cmd):
                self.calls.append(cmd)
                return argparse.Namespace(stdout=json.dumps(titled_pack), stderr="", returncode=0)
            return plain(cmd, **kw)
        return run

    def test_partner_doc_title_routes_to_existing_owner(self) -> None:
        # Wrong impl: the prompt words scaffold pre-review-google.
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.PARTNER_TITLE), \
                patch.object(dispatch, "run", side_effect=self.titled_run(self.TITLED)):
            choice = dispatch.choose_workstream(self.PARTNER_PROMPT, dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]),
                         ("acme-plans", "source-title", False))
        self.assertEqual(self.scaffold_calls(), [], "a title that names an owner never scaffolds")
        ranked = [c for c in self.calls if c[1].endswith("router-context-pack.py")]
        self.assertIn("--source-title=Acme Roadmap", ranked[-1])
        self.assertFalse(dispatch.workstream_exists("pre-review-google"))

    def test_unmatched_title_names_the_new_workstream(self) -> None:
        weak = {"candidate_owners": [{"slug": "acme-plans", "score": 60, "evidence": ["shared topic terms: acme"]}]}
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.PARTNER_TITLE), \
                patch.object(dispatch, "run", side_effect=self.titled_run(weak)):
            choice = dispatch.choose_workstream(self.PARTNER_PROMPT, dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"], choice["created"]), ("acme-roadmap", "created", True))
        (scaffold,) = self.scaffold_calls()
        self.assertIn("--source-title=Acme Roadmap", scaffold, "the resolved title is passed, never fetched twice")
        self.assertEqual(choice["weak_match"]["slug"], "acme-plans")

    def test_failed_lookup_scaffolds_without_refetch(self) -> None:
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=[]), \
                patch.object(dispatch, "run", side_effect=self.fake_run(self.PROMPT_ONLY, scaffold_slug="workstream-454c06a7")):
            choice = dispatch.choose_workstream(self.PARTNER_PROMPT, dispatch.DEFAULTS)
        (scaffold,) = self.scaffold_calls()
        self.assertIn("--no-fetch", scaffold)
        self.assertFalse(any(str(a).startswith("--source-title") for a in scaffold))
        self.assertEqual(choice["slug"], "workstream-454c06a7")

    def test_community_qa_slack_title_names_new_workstream(self) -> None:
        ama = [{"url": "https://example.slack.com/archives/C0AAAAAAAA/p1700000000000001", "kind": "slack",
                "raw": "Community QA Guide", "title": "Community QA Guide"}]
        none = {"candidate_owners": []}
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=ama), \
                patch.object(dispatch, "run", side_effect=self.titled_run(none, scaffold_slug="community-qa")):
            choice = dispatch.choose_workstream(self.AMA_PROMPT, dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["created"]), ("community-qa", True))
        (scaffold,) = self.scaffold_calls()
        self.assertIn("--source-title=Community QA Guide", scaffold)

    def test_prompt_without_links_never_looks_up_titles(self) -> None:
        with patch.object(dispatch.link_titles, "fetch_titles", side_effect=AssertionError("fetched")), \
                patch.object(dispatch, "run", side_effect=self.fake_run(self.PROMPT_ONLY)):
            choice = dispatch.choose_workstream("Write a note on signup conversion", dispatch.DEFAULTS)
        self.assertEqual(choice["reason"], "created")

    def test_strong_prompt_match_skips_the_lookup(self) -> None:
        with patch.object(dispatch.link_titles, "fetch_titles", side_effect=AssertionError("fetched")), \
                patch.object(dispatch, "run", side_effect=self.fake_run({"candidate_owners": [self.STRONG]})):
            choice = dispatch.choose_workstream("Plant detail mock: " + self.PARTNER_PROMPT, dispatch.DEFAULTS)
        self.assertEqual((choice["slug"], choice["reason"]), ("garden-revamp", "strong-match"))

    def test_source_title_evidence_is_strong(self) -> None:
        self.assertTrue(dispatch.strong_owner(self.TITLED["candidate_owners"][0], dispatch.DEFAULTS))

    def test_real_context_pack_matches_partner_title(self) -> None:
        # The real router-context-pack.py against this hermetic workstreams tree.
        (self.repo / "workstreams" / "acme-plans" / "config.yaml").write_text(
            json.dumps({"name": "Acme Plans", "status": "active", "description": "Roadmap planning docs for Acme."}))

        def real_pack(cmd, **kw):
            return subprocess.run([*cmd, "--workstreams-root", str(self.repo),
                                   "--priority-channels", str(self.repo / "none.yaml"),
                                   "--running-tasks", str(self.repo / "none.json")],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        with patch.object(dispatch, "run", side_effect=real_pack), \
                patch.object(dispatch.link_titles, "fetch_titles", return_value=self.PARTNER_TITLE), \
                patch.object(dispatch, "create_workstream", side_effect=AssertionError("must not scaffold")):
            linked = dispatch.owner_from_links(self.PARTNER_PROMPT, dispatch.DEFAULTS)
        self.assertEqual(linked["slug"], "acme-plans")


class IncidentOwnerRoutingTests(unittest.TestCase):
    """A dispatch with no --workstream must not scaffold a stray workstream when an owner
    exists. Reading only the top-ranked candidate lets switchboard-maintenance's big config win on
    shared words as a weak match, so every named owner below it is ignored. The real router-context-pack.py ranks each shortened prompt against a hermetic workstreams tree
    whose switchboard-maintenance and session-review configs share many words with every ask."""

    BIG = ("Owns switchboard scripts, router, dispatch, worker tabs, owner tabs, review notes, review links, "
           "Claude Code settings, hooks, skills, image generation, email drafting, flyer and export packaging, "
           "Windows install tests, drift check, reports, root cause analysis, options with a lean, "
           "seed catalog sheets, labeling research, spreadsheet review, pick up finished work.")
    TREE = {
        "switchboard-maintenance": ("Switchboard Maintenance", BIG, []),
        "session-review": ("Session Review", BIG.replace("switchboard", "Session"), []),
        "garden-planner-export": ("Garden Planner Export", "CSV and PDF export for the Garden Planner app, Windows support.", []),
        "flyer-skill": ("Flyer Skill", "Flyer generation skill.", []),
        "site-template-library": ("Site Template Library", "Template library for Netlify sites.", ["netlify"]),
    }

    EXPORT = ("the owner: 'Can you pick up the Garden export work? Is it finished?' State: export v2 was built last week "
              "(note workstreams/garden-planner-export/2026-01-15-export-csv-fixes.md). Nothing is published yet. "
              "Port the photo wall grid fix (workstreams/photo-wall/2026-01-20-photo-wall-grid-fix.md) where it "
              "applies. Report what is done and options with a lean.")
    LINKS = ("Review links in the CoS tab open the wrong note. Context: the links look like "
             "[Open note](notes-app://open?vault=demo&file=<path>). Check whether the focus fix, the open-focus "
             "guardrail, or the link handler broke them. If needed switch review_surface.review_link and propose "
             "AGENTS.md text. Run the drift check.")
    ONLY_DIAG = ("Read-only diagnosis, no changes. A confirmation dialog shows on every new worker tab that "
                 "dispatch.py launches. Find out why the choice is not remembered. Do NOT edit any settings file, "
                 "hooks, or dispatch.py.")
    PERSONAL = ("the owner's personal ask: 'Sort my seed catalog spreadsheet "
                "https://docs.google.com/spreadsheets/d/1-TEST/edit and suggest labeling guardrails.' Build a private "
                "local app; do NOT deploy it to Netlify or any shared host.")
    FLYER = ("the owner: 'Can you update the flyer and email skills to use image generation for better pictures?' "
             "Context Slack thread: https://example.slack.com/archives/CHANNEL/p1700000000000000. "
             "Read the full thread first, then update those skills. Do not post in the Slack thread.")
    DUPLICATE = ("Why did two workers start for one ask when switchboard should stop that? CoS dispatched 'Can you pick "
                 "up the Garden export work' with no --workstream; the router created garden-export although "
                 "workstreams/garden-planner-export exists and its note "
                 "workstreams/garden-planner-export/2026-01-15-export-csv-fixes.md was named. Fold the strays "
                 "into switchboard-maintenance. Run tests and the drift check.")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        for slug, (name, description, terms) in self.TREE.items():
            self.add_workstream(slug, name, description, terms)
        (self.repo / "workstreams" / "garden-planner-export" / "2026-01-15-export-csv-fixes.md").write_text("# v2\n")
        for script in ("dispatch.py", "review_surface.py"):
            (self.repo / "system" / "scripts").mkdir(parents=True, exist_ok=True)
            (self.repo / "system" / "scripts" / script).write_text("")
        self.calls: list[list[str]] = []
        patcher = patch.object(dispatch, "REPO_ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_workstream(self, slug: str, name: str, description: str = "", terms: list[str] | None = None) -> None:
        folder = self.repo / "workstreams" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "CONTEXT.md").write_text(f"# {name}\n\n**Status:** active\n")
        (folder / "config.yaml").write_text(json.dumps({"name": name, "status": "active", "description": description,
                                                        "todo_match_terms": terms or []}))

    def run_real_pack(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[1].endswith("router-context-pack.py"):
            return subprocess.run([*cmd, "--workstreams-root", str(self.repo),
                                   "--priority-channels", str(self.repo / "none.yaml"),
                                   "--running-tasks", str(self.repo / "none.json")],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        if cmd[1].endswith("new-workstream.py"):
            return argparse.Namespace(stdout=json.dumps({"slug": "zz-new", "created": False, "exists": False}),
                                      stderr="", returncode=0)
        raise AssertionError(f"unexpected command {cmd[:2]}")

    def choose(self, prompt: str) -> dict:
        with patch.object(dispatch, "run", side_effect=self.run_real_pack):
            return dispatch.choose_workstream(prompt, dispatch.DEFAULTS, defer_scaffold=True)

    def test_export_ask_goes_to_the_export_owner_not_a_new_stray(self) -> None:
        # The export ask also names the photo-wall stray's note path; the owner's quoted words name the export.
        self.add_workstream("photo-wall", "Photo Wall")
        choice = self.choose(self.EXPORT)
        self.assertEqual((choice["slug"], choice["created"]), ("garden-planner-export", False))
        self.assertEqual(choice["reason"], "subject")

    def test_export_note_path_alone_is_a_hard_match(self) -> None:
        choice = self.choose(self.EXPORT)
        self.assertEqual((choice["slug"], choice["reason"]), ("garden-planner-export", "path"))

    def test_review_links_ask_goes_to_switchboard_maintenance(self) -> None:
        choice = self.choose(self.LINKS)
        self.assertEqual((choice["slug"], choice["created"]), ("switchboard-maintenance", False))

    def test_dispatch_diagnosis_goes_to_switchboard_maintenance(self) -> None:
        choice = self.choose(self.ONLY_DIAG)
        self.assertEqual((choice["slug"], choice["created"]), ("switchboard-maintenance", False))

    def test_duplicate_worker_ask_goes_to_switchboard_maintenance(self) -> None:
        choice = self.choose(self.DUPLICATE)
        self.assertEqual((choice["slug"], choice["created"]), ("switchboard-maintenance", False))

    def test_flyer_ask_goes_to_the_flyer_skill_owner(self) -> None:
        choice = self.choose(self.FLYER)
        self.assertEqual((choice["slug"], choice["created"]), ("flyer-skill", False))

    def test_novel_personal_ask_gets_its_own_workstream(self) -> None:
        # Novel work: no owner named. "Netlify" (a match term deep in the context) and
        # "guardrails" must not bind it to an existing workstream.
        choice = self.choose(self.PERSONAL)
        self.assertTrue(choice["created"], choice)
        self.assertNotEqual(choice["slug"], "site-template-library")

    def test_closest_owner_beats_a_new_workstream_when_nothing_is_named(self) -> None:
        # "dark mode" spells two of the three distinctive name words; no link to read.
        self.add_workstream("dark-mode-rollout", "Dark Mode Rollout", "Dark mode for the dashboard.")
        choice = self.choose("What is still blocking dark mode for customers in the next release?")
        self.assertEqual((choice["slug"], choice["reason"]), ("dark-mode-rollout", "closest-owner"))
        self.assertFalse(any(c[1].endswith("new-workstream.py") for c in self.calls))

    def test_pinned_new_slug_is_created_from_the_router_flag(self) -> None:
        # router-dispatch.sh --workstream <slug> adds the directive; an unknown slug is created as named.
        choice = self.choose("Workstream: seed-catalog-notes\nReview the plan.")
        self.assertEqual((choice["slug"], choice["reason"]), ("zz-new", "directive-created"))
        self.assertFalse(any(c[1].endswith("router-context-pack.py") for c in self.calls))


class PickOwnerTierTests(unittest.TestCase):
    """pick_owner reads past rank 1 only for named evidence."""

    BIG = {"slug": "switchboard-maintenance", "score": 1900, "evidence": ["shared topic terms: owner, kit, note, open"]}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        repo = Path(self.tmp.name)
        for slug in ("switchboard-maintenance", "photo-book-kit", "signup-flow"):
            (repo / "workstreams" / slug).mkdir(parents=True)
            (repo / "workstreams" / slug / "CONTEXT.md").write_text("# x\n")
        patcher = patch.object(dispatch, "REPO_ROOT", repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_weak_top_candidate_with_nothing_named_picks_nobody(self) -> None:
        self.assertEqual(dispatch.pick_owner({"candidate_owners": [self.BIG]}, dispatch.DEFAULTS), (None, ""))

    def test_note_pointer_below_a_weak_top_candidate_wins(self) -> None:
        kit = {"slug": "photo-book-kit", "score": 700, "evidence": ["note named: 2026-01-15-kit.md"]}
        picked, tier = dispatch.pick_owner({"candidate_owners": [self.BIG, kit]}, dispatch.DEFAULTS)
        self.assertEqual((picked["slug"], tier), ("photo-book-kit", "named-in-context"))

    def test_match_term_below_rank_one_is_not_enough(self) -> None:
        # A match term deep in an ask's context names nobody.
        launch = {"slug": "signup-flow", "score": 400, "evidence": ['match term "sitebuilder"']}
        self.assertEqual(dispatch.pick_owner({"candidate_owners": [self.BIG, launch]}, dispatch.DEFAULTS), (None, ""))

    def test_a_finished_workstream_is_not_named_back_to_life(self) -> None:
        done = {"slug": "photo-book-kit", "score": 340, "evidence": ["note named: 2026-01-15-kit.md", "terminal status: done"]}
        self.assertEqual(dispatch.pick_owner({"candidate_owners": [self.BIG, done]}, dispatch.DEFAULTS), (None, ""))

    def test_path_owner_wins_over_every_score(self) -> None:
        pack = {"path_owner": "photo-book-kit", "candidate_owners": [self.BIG]}
        self.assertEqual(dispatch.pick_owner(pack, dispatch.DEFAULTS)[1], "path")

    def test_subject_beats_a_strong_top_candidate(self) -> None:
        top = {**self.BIG, "evidence": ["switchboard system named: router", *self.BIG["evidence"]]}
        kit = {"slug": "photo-book-kit", "score": 900, "evidence": ['subject names this workstream: "photo kit"']}
        picked, tier = dispatch.pick_owner({"candidate_owners": [top, kit]}, dispatch.DEFAULTS)
        self.assertEqual((picked["slug"], tier), ("photo-book-kit", "subject"))


class FinishOnDirtyMainTests(unittest.TestCase):
    """Landing on a main whose working tree still carries direct-to-main edits
    (an uncommitted CONTEXT.md on main must not stop a land). Hermetic git repo per test; nothing may be discarded."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "a.md").write_text("".join(f"line {n}\n" for n in range(1, 11)))
        (self.repo / "other.md").write_text("other\n")
        (self.repo / ".gitignore").write_text(".amp/\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        self.patches = [patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "WORKTREES", self.repo / ".amp" / "worktrees"),
                        patch.object(ledger, "REPO_ROOT", self.repo)]
        for p in self.patches:
            p.start()
        self.path, self.branch = dispatch.create_worktree("rt-test")
        self.task = {"id": "rt-test", "branch": self.branch, "worktree": str(self.path), "prompt": "edit a.md"}

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return dispatch.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]).stdout

    def test_dirty_file_the_branch_does_not_touch_never_blocks(self) -> None:
        (self.path / "a.md").write_text((self.path / "a.md").read_text() + "line 11 (branch)\n")
        (self.repo / "other.md").write_text("other, edited on main\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertNotIn("preserved_main_commit", outcome)
        # main's unrelated edit is left alone, uncommitted.
        self.assertIn(" M other.md", self._git("status", "--porcelain"))
        self.assertIn("line 11 (branch)", (self.repo / "a.md").read_text())

    def test_overlapping_dirty_file_is_committed_with_attribution_then_landed(self) -> None:
        # Wrong impl: fail the landing and label the worker's result "failed" (the
        # old behaviour), or stash/discard main's edit.
        (self.path / "a.md").write_text((self.path / "a.md").read_text() + "line 11 (branch)\n")
        (self.repo / "a.md").write_text("line 1 (edited on main)\n" + "".join(f"line {n}\n" for n in range(2, 11)))
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual(outcome["preserved_paths"], ["a.md"])
        log = self._git("log", "--format=%s", "main")
        self.assertIn("left by direct-to-main workers; committed by dispatch.py finish before landing rt-test", log)
        self.assertIn("rt-test: edit a.md", log)
        landed = (self.repo / "a.md").read_text()
        self.assertIn("line 1 (edited on main)", landed)
        self.assertIn("line 11 (branch)", landed)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        self.assertEqual(self._git("rev-parse", "--short", "HEAD~1").strip(), outcome["preserved_main_commit"])

    def test_dirty_edit_on_the_old_path_of_a_branch_rename_is_overlap(self) -> None:
        # The branch `git mv`-es a workstream folder to _archive/ while a watched-doc
        # refresh edits files at the old path on main. `git diff --name-only` lists only
        # the new path of a rename, so the old path must still count as overlap or the
        # fast-forward fails with "would be overwritten".
        self._git("mv", "a.md", "archived.md", cwd=self.path)
        (self.repo / "a.md").write_text("line 1 (edited on main)\n" + "".join(f"line {n}\n" for n in range(2, 11)))
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual(outcome["preserved_paths"], ["a.md"])
        self.assertFalse((self.repo / "a.md").exists())
        self.assertIn("line 1 (edited on main)", (self.repo / "archived.md").read_text())
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_main_moving_between_rebase_and_ff_is_retried_not_failed(self) -> None:
        # Reconciliation commits can land on main between the worker's rebase and ff.
        # Wrong impl: report a conflict on the first ff failure.
        (self.path / "a.md").write_text((self.path / "a.md").read_text() + "line 11 (branch)\n")
        real_git = dispatch.git
        moved = []

        def racing_git(*args, **kwargs):
            if args[:2] == ("merge", "--ff-only") and not moved:
                moved.append(True)
                (self.repo / "other.md").write_text("landed on main meanwhile\n")
                self._git("commit", "-q", "-am", "another task landed")
            return real_git(*args, **kwargs)

        with patch.object(dispatch, "git", racing_git):
            outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        log = self._git("log", "--format=%s", "main").splitlines()
        self.assertEqual(log[:2], ["rt-test: edit a.md", "another task landed"])
        self.assertIn("line 11 (branch)", (self.repo / "a.md").read_text())
        self.assertEqual((self.repo / "other.md").read_text(), "landed on main meanwhile\n")

    def test_untracked_overlap_that_truly_conflicts_is_preserved_not_discarded(self) -> None:
        (self.path / "new.md").write_text("branch version\n")
        (self.repo / "new.md").write_text("main version\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertFalse(outcome["landed"])
        self.assertTrue(outcome["conflict"])
        self.assertIn("rebase conflict", outcome["reason"])
        # main's version is committed (not lost), the branch is intact for a human.
        self.assertEqual(self._git("show", "main:new.md"), "main version\n")
        self.assertEqual(self._git("show", f"{self.branch}:new.md"), "branch version\n")
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    @needs_symlinks
    def test_tracked_symlink_replaced_by_a_directory_on_main_still_lands(self) -> None:
        # Main HEAD tracks `.state` as a symlink while the working tree holds a real
        # directory with ignored contents and a nested .git; the branch deletes the
        # symlink. `git add -A -- .state` fails ("does not have a commit checked out").
        # Wrong impl: bulk `git add -A` and surface the error as a landing failure.
        (self.repo / ".gitignore").write_text(".amp/\n/.state/\n")
        os.symlink("nowhere", self.repo / ".state")
        self._git("add", "--", ".gitignore", ".state")
        self._git("commit", "-q", "-m", "track .state as a symlink")
        self._git("reset", "-q", "--hard", "main", cwd=self.path)  # branch starts from this main
        # The branch untracks the symlink; main's working tree replaced it with a real dir.
        (self.path / ".state").unlink()
        self._git("rm", "-q", "--cached", "--", ".state", cwd=self.path)
        (self.repo / ".state").unlink()
        (self.repo / ".state").mkdir()
        (self.repo / ".state" / "ledger.json").write_text("{}\n")
        (self.repo / ".state" / ".git").mkdir()
        self.assertIn(".state", dispatch.dirty_main_paths())  # ` D .state` (nested repo) or ` T .state`
        outcome = dispatch.finish_branch({**self.task, "prompt": "untrack .state"})
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual(outcome["preserved_paths"], [".state"])
        self.assertEqual(self._git("ls-files", "--", ".state").strip(), "")  # gone from the index on main
        self.assertTrue((self.repo / ".state" / "ledger.json").exists())  # the directory itself is untouched
        self.assertEqual(self._git("status", "--porcelain").strip(), "")

    def test_finish_corrects_a_landing_failure_row_to_done(self) -> None:
        # Wrong impl: the row keeps status=failed after the branch lands.
        (self.path / "a.md").write_text("rewritten on branch\n")
        write_ledger({**self.task, "status": "failed", "origin": "launchpad", "report_delivered": True,
                      "report_attempted_at": "2026-10-05T04:50:00Z",
                      "summary": "failed: could not land on main (main has uncommitted edits to a.md; branch task/rt-test "
                                 "is rebased and ready). Worker result stands: Doc updated and shared with Noel."})
        out = io.StringIO()
        with redirect_stdout(out):
            code = dispatch.command_finish(argparse.Namespace(task_id="rt-test"))
        result = json.loads(out.getvalue().strip())
        self.assertEqual((code, result["landed"], result.get("status_corrected")), (0, True, "done"))
        row = ledger.find_task(ledger.load_payload(), "rt-test")
        self.assertEqual((row["status"], row["summary"]), ("done", "Doc updated and shared with Noel."))
        self.assertIs(row["report_delivered"], False)
        self.assertNotIn("report_attempted_at", row)
        self.assertEqual(row["landed_commit"], result["commit"])

    def test_main_checkout_resolves_a_linked_worktree_to_main(self) -> None:
        # A worker that runs its worktree's copy of dispatch.py must not make
        # REPO_ROOT the worktree and report landed=true with main untouched. Wrong impl: REPO_ROOT = the script's
        # own repo root, whichever checkout that is.
        self.assertEqual(dispatch.main_checkout(self.path).resolve(), self.repo.resolve())
        self.assertEqual(dispatch.main_checkout(self.repo).resolve(), self.repo.resolve())
        self.assertEqual(dispatch.main_checkout(Path(self.tmp.name)), Path(self.tmp.name))  # not a repo: unchanged

    def test_finish_refuses_when_repo_root_does_not_have_main_checked_out(self) -> None:
        # Second line of defence: with REPO_ROOT pointing at the worktree,
        # `merge --ff-only` merges the task branch into itself (rc 0).
        # Wrong impl: landed=true, commit = main's unchanged head.
        (self.path / "a.md").write_text((self.path / "a.md").read_text() + "line 11 (branch)\n")
        before = self._git("rev-parse", "main").strip()
        with patch.object(dispatch, "REPO_ROOT", self.path):
            outcome = dispatch.finish_branch(self.task)
        self.assertFalse(outcome["landed"], outcome)
        self.assertEqual(outcome["wrong_checkout"], self.branch)
        self.assertIn("not main", outcome["reason"])
        self.assertEqual(self._git("rev-parse", "main").strip(), before)
        # Nothing was committed either: the worktree still holds the edit for a real finish.
        self.assertIn("line 11 (branch)", (self.path / "a.md").read_text())
        self.assertTrue(dispatch.finish_branch(self.task)["landed"])
        self.assertIn("line 11 (branch)", (self.repo / "a.md").read_text())

    def test_finish_leaves_an_ordinary_failed_row_alone(self) -> None:
        # A row the worker itself reported failed (not a landing failure) is not rewritten.
        write_ledger({**self.task, "status": "failed", "summary": "failed: staging token expired", "report_delivered": True})
        out = io.StringIO()
        with redirect_stdout(out):
            dispatch.command_finish(argparse.Namespace(task_id="rt-test"))
        row = ledger.find_task(ledger.load_payload(), "rt-test")
        self.assertEqual((row["status"], row["summary"]), ("failed", "failed: staging token expired"))


@needs_symlinks   # git records a symlink as mode 120000; a junction or hard link is not that case
class SymlinkLoopGuardTests(unittest.TestCase):
    """A worktree's shared-state symlinks (people/ -> <main>/people) committed after
    a .gitignore change un-ignores them would make fast-forwarding main replace the
    real directories with links to themselves. finish must never commit such a link,
    must refuse a branch whose own commits add one, and must keep allowing the
    portable relative links the repo already uses (CLAUDE.md -> AGENTS.md)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "AGENTS.md").write_text("guidance\n")
        # people/ is ignored with the directory-only pattern main had before
        # a8deb077: it does not match a symlink, so the worktree's link leaks
        # into `git add -A` exactly as it did in 8b3230df.
        (self.repo / ".gitignore").write_text(".amp\npeople/\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        (self.repo / "people").mkdir()
        (self.repo / "people" / "alice.md").write_text("# Alice\n")
        self.patches = [patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "WORKTREES", self.repo / ".amp" / "worktrees"),
                        patch.object(ledger, "REPO_ROOT", self.repo)]
        for p in self.patches:
            p.start()
        self.path, self.branch = dispatch.create_worktree("rt-test")
        self.task = {"id": "rt-test", "branch": self.branch, "worktree": str(self.path), "prompt": "edit"}
        # link_shared_state symlinked the ignored people/ at main's copy and (fix E)
        # excluded it root-anchored in the shared info/exclude, which closes the leak.
        self.assertTrue(dispatch.winlink.is_link(self.path / "people"))
        self.exclude = self.repo / ".git" / "info" / "exclude"
        self.assertIn("/people", self.exclude.read_text().splitlines())
        self.assertEqual(self._git("status", "--porcelain", cwd=self.path).strip(), "")

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return dispatch.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]).stdout

    def reopen_leak(self) -> None:
        """Drop the info/exclude entries so finish's own guard is exercised as the
        last line of defence (the state before fix E)."""
        self.exclude.write_text("")

    def test_exclude_entries_are_written_once_and_keep_existing_lines(self) -> None:
        # Wrong impls: rewrite the file (losing the owner's own entries), or append a
        # duplicate line on every launch.
        self.exclude.write_text("my-own-rule\n" + self.exclude.read_text())
        before = self.exclude.read_text()
        dispatch.create_worktree("rt-again")
        self.assertEqual(self.exclude.read_text(), before)
        self.assertEqual(before.splitlines().count("/people"), 1)
        self.assertEqual(before.splitlines()[0], "my-own-rule")
        self.assertEqual(dispatch.exclude_shared_links(["people", "people"]), [])

    def test_finish_never_commits_a_shared_state_loop_symlink(self) -> None:
        self.reopen_leak()
        # Wrong impl: `git add -A` sweeps the people -> main/people link into the commit.
        self.assertIn("?? people", self._git("status", "--porcelain", cwd=self.path))  # the leak precondition
        (self.path / "AGENTS.md").write_text("guidance, edited on branch\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual([e["path"] for e in outcome["rejected_symlinks"]], ["people"])
        self.assertEqual(self._git("ls-tree", "main", "people").strip(), "")
        # main's real people/ directory and its only profile survive the fast-forward.
        self.assertFalse((self.repo / "people").is_symlink())
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice\n")
        self.assertEqual(self._git("show", "main:AGENTS.md"), "guidance, edited on branch\n")

    def test_finish_refuses_a_branch_that_committed_the_loop_itself(self) -> None:
        self.reopen_leak()
        # Wrong impl: trust the worker's own commit and fast-forward main onto the loop.
        self._git("add", "-A", cwd=self.path)
        self._git("commit", "-q", "-m", "worker commit with loop", cwd=self.path)
        self.assertEqual(self._git("ls-tree", self.branch, "people").split()[0], "120000")
        outcome = dispatch.finish_branch(self.task)
        self.assertFalse(outcome["landed"], outcome)
        self.assertTrue(outcome["conflict"])
        self.assertEqual(outcome["rejected_symlinks"][0]["path"], "people")
        self.assertIn("point inside the repo", outcome["reason"])
        self.assertEqual(self._git("ls-tree", "main", "people").strip(), "")
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice\n")

    def test_relative_symlink_to_a_sibling_file_is_allowed(self) -> None:
        self.reopen_leak()
        # Wrong impl: reject every symlink whose target is inside the repo, which
        # would block the repo's existing CLAUDE.md -> AGENTS.md style links.
        (self.path / "CLAUDE.md").symlink_to("AGENTS.md")
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual([e["path"] for e in outcome["rejected_symlinks"]], ["people"])  # only the loop, not CLAUDE.md
        self.assertEqual(self._git("ls-tree", "main", "CLAUDE.md").split()[0], "120000")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), "guidance\n")

    def test_relative_self_loop_and_absolute_link_into_worktree_are_rejected(self) -> None:
        self.reopen_leak()
        # Both spell the same loop without naming main's path.
        (self.path / "docs").mkdir()
        (self.path / "docs" / "loop").symlink_to("../docs/loop")
        (self.path / "AGENTS-copy.md").symlink_to(self.path / "AGENTS.md")
        self._git("add", "-A", cwd=self.path)
        found = dispatch.loop_symlinks(self.path, dispatch.staged_paths(self.path))
        self.assertEqual(sorted(e["path"] for e in found), ["AGENTS-copy.md", "docs/loop", "people"])

    def test_check_symlinks_subcommand_fails_only_on_loops(self) -> None:
        self.reopen_leak()
        self._git("add", "-A", cwd=self.path)
        err = io.StringIO()
        with patch.dict(os.environ, {"SWITCHBOARD_CHECKOUT": str(self.path)}), redirect_stderr(err):
            code = dispatch.command_check_symlinks(argparse.Namespace())
        self.assertEqual(code, 1)
        self.assertIn("people -> ", err.getvalue())
        self._git("reset", "-q", "--", "people", cwd=self.path)
        with patch.dict(os.environ, {"SWITCHBOARD_CHECKOUT": str(self.path)}), redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.command_check_symlinks(argparse.Namespace()), 0)


class IdleResumeTests(unittest.TestCase):
    """Done is observable: a worker that ends
    its turn with branch task/<id> unmerged gets exactly one resume message after
    IDLE_RESUME_MINUTES, then exactly one line to the origin, then nothing.
    Wrong impls: resume on every pass; escalate without a resume; treat a landed
    branch or a streaming worker as idle."""

    def setUp(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.notified: list[tuple[str, str]] = []
        idle_since = (datetime.now(timezone.utc) - timedelta(minutes=2 * dispatch.IDLE_RESUME_MINUTES)).timestamp()
        self.agents = {"T-idle": {"state": "Idle", "state_ts": idle_since, "workspace_id": "w1", "title": "worker"}}
        self.git_state = "open"
        self.patches = [
            patch.object(ledger, "live_agents", lambda: self.agents),
            patch.object(ledger, "git_state", lambda task: self.git_state),
            patch.object(dispatch, "tab_send", lambda ref, text: self.sent.append((ref, text)) or True),
            patch.object(dispatch, "tab_idle", lambda ref: False),   # a streaming worker's screen is busy
            patch.object(dispatch, "notify", lambda tid, text, **kw: self.notified.append((tid, text)) or {}),
        ]
        for p in self.patches:
            p.start()
        write_ledger({"id": "rt-idle", "status": "active", "branch": "task/rt-idle", "thread_id": "T-idle",
                      "workspace_ref": "workspace:9", "origin": "launchpad", "attempt_id": "rt-idle.a1"})

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _row(self) -> dict:
        return ledger.find_task(ledger.load_payload(), "rt-idle") or {}

    def test_one_resume_then_one_escalation_then_silence(self) -> None:
        first = dispatch.nudge_idle(dispatch.config())
        self.assertEqual(first, {"resumed": ["rt-idle"], "escalated": []})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("task/rt-idle has not landed on main", self.sent[0][1])
        self.assertTrue(self._row().get("resume_sent_at"))
        self.assertEqual(self._row()["status"], "active")  # the report cannot flip it; only the branch landing can
        # Still idle, but not yet IDLE_RESUME_MINUTES since the resume: nothing.
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual(len(self.sent), 1)
        # Backdate the resume: now the origin hears exactly one line.
        old = iso(datetime.now(timezone.utc) - timedelta(minutes=dispatch.IDLE_RESUME_MINUTES + 1))
        ledger.locked_mutation(lambda p: (ledger.find_task(p, "rt-idle") or {}).update(resume_sent_at=old))
        second = dispatch.nudge_idle(dispatch.config())
        self.assertEqual(second, {"resumed": [], "escalated": ["rt-idle"]})
        self.assertEqual(len(self.notified), 1)
        self.assertIn("after resume", self.notified[0][1])
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual((len(self.sent), len(self.notified)), (1, 1))

    def test_landed_branch_or_busy_worker_is_left_alone(self) -> None:
        self.git_state = "landed"
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.git_state = "open"
        self.agents["T-idle"]["state"] = "Running"
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual(self.sent, [])


class StuckLifecycleTests(unittest.TestCase):
    """The cmux lifecycle stamp can stick at "Running" after the Amp thread goes
    idle, so a "Running" tab can show an idle, empty screen. The resume rule and the
    reaper must not trust a stale stamp. Wrong impls: (a) only state == "Idle"
    counts, so a stuck worker never gets its resume and a finished tab is never
    reaped; (b) any old stamp counts as idle, so a worker mid-tool-call (fresh
    heartbeat) or mid-stream (busy screen) gets steered."""

    def setUp(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.screen_idle = True
        old = (datetime.now(timezone.utc) - timedelta(minutes=2 * dispatch.IDLE_RESUME_MINUTES)).timestamp()
        self.agents = {"T-stuck": {"state": "Running", "state_ts": old, "workspace_id": "w1", "title": "worker"}}
        self.patches = [
            patch.object(ledger, "live_agents", lambda: self.agents),
            patch.object(ledger, "git_state", lambda task: "open"),
            patch.object(dispatch, "tab_send", lambda ref, text: self.sent.append((ref, text)) or True),
            patch.object(dispatch, "tab_idle", lambda ref: self.screen_idle),
            patch.object(dispatch, "notify", lambda tid, text, **kw: {}),
        ]
        for p in self.patches:
            p.start()
        self.row = {"id": "rt-stuck", "status": "active", "branch": "task/rt-stuck", "thread_id": "T-stuck",
                    "workspace_ref": "workspace:9", "origin": "cos", "attempt_id": "rt-stuck.a1",
                    "heartbeat_at": iso(datetime.now(timezone.utc) - timedelta(minutes=2 * dispatch.IDLE_RESUME_MINUTES))}
        write_ledger(self.row)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_stale_running_stamp_with_idle_screen_and_old_heartbeat_resumes(self) -> None:
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": ["rt-stuck"], "escalated": []})
        self.assertEqual(len(self.sent), 1)

    def test_tool_name_state_is_treated_like_running(self) -> None:
        self.agents["T-stuck"]["state"] = "shell_command_status"   # cmux writes the live tool name as the state
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": ["rt-stuck"], "escalated": []})

    def test_needs_input_counts_as_idle_without_a_screen_read_and_escalates_at_once(self) -> None:
        # A dialog may hide the composer, so the stamp alone decides idleness; and a
        # resume line would queue behind that dialog unread, so the origin hears it
        # now (one urgent "waiting on a question" line) instead of after a resume.
        self.agents["T-stuck"]["state"] = "Needs input"
        self.screen_idle = False
        notified: list[tuple[str, str]] = []
        with patch.object(dispatch, "notify", lambda tid, text, **kw: notified.append((tid, text)) or {"delivered": True}):
            self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": ["rt-stuck"]})
            self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual(self.sent, [])
        self.assertEqual(len(notified), 1)
        self.assertIn("waiting on a question", notified[0][1])

    def test_fresh_heartbeat_or_busy_screen_or_fresh_stamp_means_working(self) -> None:
        self.screen_idle = False
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.screen_idle = True
        write_ledger({**self.row, "heartbeat_at": iso(datetime.now(timezone.utc) - timedelta(minutes=1))})
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        write_ledger(self.row)
        self.agents["T-stuck"]["state_ts"] = datetime.now(timezone.utc).timestamp()
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual(self.sent, [])

    def test_idle_since_is_the_later_of_stamp_and_heartbeat(self) -> None:
        # The worker kept working (heartbeats) after the stamp stuck: idleness starts
        # at the last heartbeat, not at the stamp, so the window is not cut short.
        recent = datetime.now(timezone.utc) - timedelta(minutes=dispatch.IDLE_RESUME_MINUTES // 2)
        write_ledger({**self.row, "heartbeat_at": iso(recent)})
        since = dispatch.worker_idle_since(ledger.load_payload()["tasks"][0], self.agents["T-stuck"],
                                           timedelta(minutes=dispatch.IDLE_RESUME_MINUTES // 4))
        self.assertIsNotNone(since)
        self.assertEqual(since.replace(microsecond=0), recent.replace(microsecond=0))
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})

    def test_reaper_closes_a_delivered_tab_whose_stamp_is_stuck_running(self) -> None:
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=10))
        write_ledger({**self.row, "status": "done", "report_delivered": True, "finished_at": finished, "worktree": None})
        workspaces = [{"ref": "workspace:9", "id": "w1", "title": "switchboard-maintenance · worker-0123456789ab"}]
        closed_refs: list[str] = []
        with patch.object(dispatch, "cmux_workspaces", lambda *a, **k: workspaces), \
                patch.object(dispatch, "cmux_close_result", lambda ref, wid, *why: (closed_refs.append(ref) or True, "")), \
                patch.object(dispatch, "drop_worktree", lambda task: None), \
                patch.object(dispatch, "release_locks", lambda tid, path=None: []):
            self.screen_idle = False
            busy = dispatch.reap_all(dispatch.config())
            self.assertEqual([s["reason"] for s in busy["skipped"]], ["agent still running"])
            self.screen_idle = True
            done = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in done["closed"]], ["rt-stuck"])
        self.assertEqual(closed_refs, ["workspace:9"])

    def test_stuck_stamp_tie_breaker_reads_the_resolved_tab_for_a_legacy_row(self) -> None:
        # A legacy row (title stored as workspace_ref) on an hours-old "Running"
        # stamp: the screen check needs a "workspace:N" ref,
        # so it must read the tab the reaper resolved, not the row's title.
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=10))
        title = "switchboard-maintenance · worker-0123456789ab"
        write_ledger({**self.row, "status": "done", "report_delivered": True, "finished_at": finished, "worktree": None,
                      "workspace_ref": title, "worker_id": "worker-0123456789ab", "workstream": "switchboard-maintenance",
                      "reaped_at": iso(datetime.now(timezone.utc) - timedelta(hours=3))})
        workspaces = [{"ref": "workspace:9", "id": "w1", "title": title}]
        closed_refs: list[str] = []
        read_refs: list[str] = []
        with patch.object(dispatch, "cmux_workspaces", lambda *a, **k: workspaces), \
                patch.object(dispatch, "cmux_close_result", lambda ref, wid, *why: (closed_refs.append(ref) or True, "")), \
                patch.object(dispatch, "tab_idle", lambda ref: read_refs.append(ref) or self.screen_idle), \
                patch.object(dispatch, "record_on_main", lambda task, **kw: {"recorded": True}), \
                patch.object(dispatch, "drop_worktree", lambda task: None), \
                patch.object(dispatch, "release_locks", lambda tid, path=None: []):
            self.screen_idle = False
            busy = dispatch.reap_all(dispatch.config())
            self.assertEqual([s["reason"] for s in busy["skipped"]], ["agent still running"])
            self.screen_idle = True
            done = dispatch.reap_all(dispatch.config())
        self.assertEqual(read_refs, ["workspace:9", "workspace:9"])
        self.assertEqual([c["task_id"] for c in done["closed"]], ["rt-stuck"])
        self.assertEqual(closed_refs, ["workspace:9"])


class UnknownReportSettlementTests(unittest.TestCase):
    """A terminal row whose single report ended outcome-unknown was never re-sent
    and never settled, so the reaper skipped its tab and worktree forever. The sweep settles
    such a row after UNKNOWN_REPORT_SETTLES_AFTER with an audit note. Wrong impls:
    settle a fresh unknown (a waiter or a human may still verify it); settle an
    active row; re-send."""

    def _row(self, **extra: object) -> dict:
        return {"id": "rt-unk", "status": "done", "report_delivered": False, "report_delivery_unknown": True,
                "attempt_id": "rt-unk.a1", **extra}

    def test_old_unknown_row_settles_with_a_note_and_fresh_or_active_rows_do_not(self) -> None:
        old = iso(datetime.now(timezone.utc) - dispatch.UNKNOWN_REPORT_SETTLES_AFTER - timedelta(hours=1))
        fresh = iso(datetime.now(timezone.utc) - timedelta(hours=1))
        write_ledger(self._row(report_attempted_at=old),
                     {**self._row(report_attempted_at=fresh), "id": "rt-fresh"},
                     {**self._row(report_attempted_at=old), "id": "rt-active", "status": "active"},
                     {**self._row(finished_at=old), "id": "rt-no-stamp"})   # legacy row: unknown flag, no attempt stamp
        settled = dispatch.settle_unknown_reports()
        self.assertEqual(sorted(s["task_id"] for s in settled), ["rt-no-stamp", "rt-unk"])
        rows = {t["id"]: t for t in ledger.load_payload()["tasks"]}
        self.assertTrue(rows["rt-unk"]["report_delivered"])
        self.assertNotIn("report_delivery_unknown", rows["rt-unk"])
        self.assertIn("settled by sweep", rows["rt-unk"]["note"])
        self.assertFalse(rows["rt-fresh"]["report_delivered"])
        self.assertTrue(rows["rt-fresh"]["report_delivery_unknown"])
        self.assertFalse(rows["rt-active"]["report_delivered"])
        self.assertEqual(dispatch.settle_unknown_reports(), [])


class OrphanedRouteRequeueTests(unittest.TestCase):
    """A "routing" row whose dispatch never reached launch/queue (crashed launcher,
    or a pre-worktree row) has no worker and is invisible to `release`, which only
    launches "queued" rows, so it can sit in routing for hours. The sweep must re-queue such a
    row once, after NOT_STARTED_AFTER, and leave alone: a row still inside the
    not-started window, a row with a live thread, a row a launcher claimed in the
    last 5 min, and a row that already has a worker or worktree."""

    def setUp(self) -> None:
        self.agents: dict[str, dict] = {}
        self.patches = [patch.object(ledger, "live_agents", lambda: self.agents)]
        for p in self.patches:
            p.start()
        self.old = iso(datetime.now(timezone.utc) - dispatch.NOT_STARTED_AFTER - timedelta(minutes=1))
        self.fresh = iso(datetime.now(timezone.utc) - timedelta(minutes=1))

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _rows(self) -> dict[str, dict]:
        return {t["id"]: t for t in ledger.load_payload()["tasks"]}

    def test_stale_workerless_routing_row_is_requeued_once_with_a_trace(self) -> None:
        write_ledger({"id": "rt-orphan", "status": "routing", "attempt_id": "rt-orphan.a1", "workstream": "switchboard-maintenance",
                      "origin": "cos", "prompt": "held route", "depends_on": ["rt-dep"], "created_at": self.old,
                      "updated_at": self.old, "launching_at": None, "summary": "Prerequisites finished; starting the held route."},
                     {"id": "rt-dep", "status": "done"})
        requeued = dispatch.requeue_orphaned_routes()
        self.assertEqual([r["task_id"] for r in requeued], ["rt-orphan"])
        row = self._rows()["rt-orphan"]
        self.assertEqual(row["status"], "queued")
        self.assertIn("re-queued by sweep", row["summary"])
        self.assertIn("rt-orphan.a1", row["summary"])
        self.assertTrue(row.get("requeued_at"))
        self.assertEqual(row["depends_on"], ["rt-dep"])  # release still applies the dependency rule
        # Idempotent: a queued row is release's business now, not the sweep's.
        self.assertEqual(dispatch.requeue_orphaned_routes(), [])

    def test_rows_that_may_still_launch_or_already_run_are_left_alone(self) -> None:
        self.agents = {"T-live": {"state": "Running", "state_ts": 0, "workspace_id": "w1", "title": "owner"}}
        claim = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        write_ledger({"id": "rt-young", "status": "routing", "attempt_id": "a", "created_at": self.fresh, "updated_at": self.fresh},
                     {"id": "rt-live", "status": "routing", "attempt_id": "a", "thread_id": "T-live",
                      "created_at": self.old, "updated_at": self.old},
                     {"id": "rt-claimed", "status": "routing", "attempt_id": "a", "launching_at": claim,
                      "created_at": self.old, "updated_at": self.old},
                     {"id": "rt-worker", "status": "routing", "attempt_id": "a", "worker_id": "worker-x",
                      "worktree": str(TMP / "wt"), "created_at": self.old, "updated_at": self.old},
                     {"id": "rt-active", "status": "active", "attempt_id": "a", "created_at": self.old, "updated_at": self.old})
        self.assertEqual(dispatch.requeue_orphaned_routes(), [])
        self.assertEqual({tid: t["status"] for tid, t in self._rows().items()},
                         {"rt-young": "routing", "rt-live": "routing", "rt-claimed": "routing", "rt-worker": "routing",
                          "rt-active": "active"})

    def test_expired_launch_claim_no_longer_protects_the_row(self) -> None:
        stale_claim = iso(datetime.now(timezone.utc) - timedelta(minutes=6))
        write_ledger({"id": "rt-crashed", "status": "routing", "attempt_id": "rt-crashed.a1", "launching_at": stale_claim,
                      "created_at": self.old, "updated_at": self.old})
        self.assertEqual([r["task_id"] for r in dispatch.requeue_orphaned_routes()], ["rt-crashed"])
        row = self._rows()["rt-crashed"]
        self.assertEqual(row["status"], "queued")
        self.assertNotIn("launching_at", row)  # claim_launch must be able to take it fresh


class NotifyArgvTests(unittest.TestCase):
    """Every sweep-originated line to the origin ("started", "failed: …",
    "blocked: …", idle escalations, lock waits) goes through notify() →
    report-to-cos.sh. Optional flags spliced in between --task-id and its value
    make the ledger CLI die with `--task-id: expected one argument` on every row
    that has an attempt id. Parse the argv the way the
    shell script does and demand each flag keeps its own value."""

    def test_flags_keep_their_values_and_the_message_is_the_only_positional(self) -> None:
        captured: list[list[str]] = []
        row = {"id": "rt-n", "status": "active", "origin": "launchpad", "attempt_id": "rt-n.a1.abc", "workstream": "demo"}
        fake = Mock(stdout='{"delivered": true}\n', stderr="")
        with patch.object(ledger, "load_payload", lambda: {"tasks": [row]}), \
                patch.object(dispatch, "run", lambda cmd, **kw: captured.append(cmd) or fake):
            result = dispatch.notify("rt-n", "blocked: worker gone", status="blocked", urgent=True)
        self.assertEqual(result, {"delivered": True})
        argv = captured[0][2:]  # after "bash <script>"
        flags: dict[str, str | bool] = {}
        positional: list[str] = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--urgent":
                flags[tok] = True
                i += 1
            elif tok.startswith("--"):
                self.assertLess(i + 1, len(argv), f"{tok} has no value")
                self.assertFalse(argv[i + 1].startswith("--"), f"{tok} swallowed {argv[i + 1]}")
                flags[tok] = argv[i + 1]
                i += 2
            else:
                positional.append(tok)
                i += 1
        self.assertEqual(flags, {"--task-id": "rt-n", "--origin": "launchpad", "--status": "blocked",
                                 "--attempt-id": "rt-n.a1.abc", "--workstream": "demo", "--urgent": True})
        self.assertEqual(positional, ["blocked: worker gone"])


class LaunchPromptClauseTests(unittest.TestCase):
    """Standing clauses every worker launch prompt must carry. An evidence search
    starts with every workstream folder, so the launch prompt states that order
    itself and a brief that names one owner cannot narrow it."""

    def test_a_claude_worker_prompt_names_its_session_and_no_amp_link(self) -> None:
        session = "00000000-0000-4000-8000-000000000001"
        task = {"id": "rt-cl", "attempt_id": "rt-cl.a1.abc", "workstream": "w", "origin": "cos", "prompt": "x",
                "agent": "claude", "session_id": session}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-cl"), "worker-cl")
        self.assertIn(f"--thread-id {session} ", prompt)
        self.assertIn(f"including claude --resume {session}>", prompt)
        self.assertIn(f"Include the result and claude --resume {session} in your final response", prompt)
        self.assertNotIn("https://ampcode.com/threads/T-", prompt)
        self.assertNotIn("your Amp thread id", prompt)

    def test_an_amp_worker_prompt_keeps_its_link(self) -> None:
        task = {"id": "rt-am", "attempt_id": "rt-am.a1.abc", "workstream": "w", "origin": "cos", "prompt": "x", "agent": "amp"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-am"), "worker-am")
        self.assertIn("--thread-id <your Amp thread id>", prompt)
        self.assertIn("including your https://ampcode.com/threads/T-... link>", prompt)
        self.assertNotIn("claude --resume", prompt)

    def test_evidence_search_starts_in_all_workstreams_then_projects(self) -> None:
        task = {"id": "rt-ev", "attempt_id": "rt-ev.a1.abc", "workstream": "pricing-tool", "origin": "cos",
                "prompt": "Workstream: pricing-tool\nFind the bulk-repricing screenshot"}
        worktree = Path("/tmp/wt-rt-ev")
        prompt = dispatch.launch_prompt(task, worktree, "worker-ev")
        clause = next((line for line in prompt.splitlines() if line.startswith("Evidence search")), "")
        self.assertTrue(clause, "launch prompt carries an Evidence search clause")
        self.assertIn(f"{worktree}/workstreams/", clause)
        self.assertIn("every workstream, not just pricing-tool", clause)
        self.assertIn("assets/", clause)
        self.assertLess(clause.index(f"{worktree}/workstreams/"), clause.index(f"{worktree}/projects/"))
        self.assertLess(clause.index(f"{worktree}/projects/"), clause.index("external source"))
        self.assertIn("List the workstreams you searched in your review note", clause)

    def test_no_real_customer_brands_in_images_clause(self) -> None:
        """The launch prompt bans any real customer's brand and real
        customer data, allows customer photos with the brand cropped out, and requires
        a check of every crop."""
        task = {"id": "rt-rs", "attempt_id": "rt-rs.a1.abc", "workstream": "spring-fair-page", "origin": "cos",
                "prompt": "Workstream: spring-fair-page\nMake three event covers options"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-rs"), "worker-rs")
        clause = next((line for line in prompt.splitlines() if line.startswith("No real customer brands in images")), "")
        self.assertTrue(clause, "launch prompt carries a No real customer brands in images clause")
        for needle in ("no company name, logo, or", "customer-identifying text",
                       "Customer screenshots never show real data", "Photos of customers, staff, or products in use are allowed",
                       "cropped or retouched out", "check every crop", "Product shots, abstract brand textures",
                       "illustrations", "never offer it as an option"):
            self.assertIn(needle, clause)
        self.assertNotIn("No real customers in images", prompt)

    def test_plan_items_open_with_bold_lead_then_full_sentences(self) -> None:
        """Each plan item opens with a bold 1 to 3 word summary, then a sentence or two.
        "Short plan" means fewer items, never a word-count cut, so the launch prompt
        states the item shape itself."""
        task = {"id": "rt-pl", "attempt_id": "rt-pl.a1.abc", "workstream": "signup-flow", "origin": "cos",
                "prompt": "Workstream: signup-flow\nWrite a short GTM plan"}
        worktree = Path("/tmp/wt-rt-pl")
        prompt = dispatch.launch_prompt(task, worktree, "worker-pl")
        clause = next((line for line in prompt.splitlines() if line.startswith("Plan and list item shape")), "")
        self.assertTrue(clause, "launch prompt carries a Plan and list item shape clause")
        self.assertIn("bold 1 to 3 word summary", clause)
        self.assertIn("one or two complete sentences", clause)
        self.assertIn("never the lead", clause)
        self.assertIn("never fragments", clause)
        self.assertIn(f"{worktree}/.agents/skills/drafting-docs/SKILL.md", clause)

    def test_parked_drafts_are_tracked_items_clause(self) -> None:
        """A worker-parked draft must never fall through the cracks. The launch prompt tells the worker a fenced draft in its report
        becomes a triage-ledger draft plus a P1 todo, and that a stamp ask keeps its
        PR set through any supersede."""
        task = {"id": "rt-pd", "attempt_id": "rt-pd.a1.abc", "workstream": "report-tooling", "origin": "cos",
                "prompt": "Workstream: report-tooling\nReview Reese's message and draft a DM"}
        worktree = Path("/tmp/wt-rt-pd")
        prompt = dispatch.launch_prompt(task, worktree, "worker-pd")
        clause = next((line for line in prompt.splitlines() if line.startswith("Parked drafts are tracked items")), "")
        self.assertTrue(clause, "launch prompt carries a Parked drafts are tracked items clause")
        self.assertIn("must never fall through the cracks", clause)
        self.assertIn("parked_draft_tracker.py track", clause)
        self.assertIn("triage-ledger draft", clause)
        self.assertIn("P1 todo due the next business day", clause)
        self.assertIn("CoS Review Needs-you pane", clause)
        self.assertIn("never closed by a todo for PR C", clause)
        followup = next((line for line in prompt.splitlines() if line.startswith("Follow-up on done reports")), "")
        self.assertIn("Needs-you stays honest", followup)
        self.assertIn("waits on someone else", followup)
        self.assertIn("worker chore", followup)

    def test_code_review_comments_clause_carries_four_rules(self) -> None:
        """Review drafts never count bot comments, list owner-team approvals or CI, or
        restate the body. The launch prompt carries the four Rule 10 items and the author-already-knows check."""
        task = {"id": "rt-cr", "attempt_id": "rt-cr.a1.abc", "workstream": "recipe-app", "origin": "cos",
                "prompt": "Workstream: recipe-app\nReview example-web#101"}
        worktree = Path("/tmp/wt-rt-cr")
        prompt = dispatch.launch_prompt(task, worktree, "worker-cr")
        clause = next((line for line in prompt.splitlines() if line.startswith("Code review comments")), "")
        self.assertTrue(clause, "launch prompt carries a Code review comments clause")
        self.assertNotIn("the owner, 20", clause)
        self.assertIn("Never cite, count, or refer to a bot comment", clause)
        self.assertIn("Code only", clause)
        self.assertIn("Never restate anything already in the PR title, body", clause)
        self.assertIn("Does the author already know this?", clause)
        self.assertIn("file:line", clause)
        self.assertIn("reuses the key of an existing feature flag", clause)
        self.assertIn("repo#101 needs four owner teams", clause)
        self.assertIn("PM scoping goes in a separate Slack line from the owner", clause)
        self.assertIn("Rule 10", clause)
        self.assertIn("github-review-wording guardrail warns, never rejects", clause)

    def test_shared_google_file_feedback_clause_offers_comments(self) -> None:
        """Feedback on a shared sheet is offered as comments in the file, not a note and a
        long DM. The launch prompt carries Rule 15: comments in the file are option 1."""
        task = {"id": "rt-gc", "attempt_id": "rt-gc.a1.abc", "workstream": "recipe-app", "origin": "cos",
                "prompt": "Workstream: recipe-app\nFeedback on Kerry's copy sheet"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-gc"), "worker-gc")
        clause = next((line for line in prompt.splitlines() if line.startswith("Feedback on shared Google files")), "")
        self.assertTrue(clause, "launch prompt carries a Feedback on shared Google files clause")
        for needle in ("Rule 15", "option 1", "anchored to the exact cells", "opens with 🤖",
                       "sheet-cell-comment.py post", "Never edit the author's text", "never resolve",
                       "pointer to the comments"):
            self.assertIn(needle, clause)

    def test_names_never_bare_numbers_clause(self) -> None:
        """A bare number such as "example-repo#10001" or "pause 10001" never stands in
        for a name. The launch prompt carries Rule 13: name first, id in
        parentheses or the link, never in the ask, fetch titles via named_refs."""
        task = {"id": "rt-nn", "attempt_id": "rt-nn.a1.abc", "workstream": "switchboard-maintenance", "origin": "cos",
                "prompt": "Workstream: switchboard-maintenance\nCodify the names rule"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-nn"), "worker-nn")
        clause = next((line for line in prompt.splitlines() if line.startswith("Names, never bare numbers")), "")
        self.assertTrue(clause, "launch prompt carries a Names, never bare numbers clause")
        self.assertIn("always state the name", clause)
        self.assertIn('never "#10001" or "repo#10001"', clause)
        self.assertIn('never "PROJ-4219"', clause)
        self.assertIn('never "rt-000000000028" alone', clause)
        self.assertIn("never appears in a question or ask", clause)
        self.assertIn("named_refs.py name", clause)
        self.assertIn("gh pr view <url> --json title", clause)
        self.assertIn("Rule 13", clause)
        self.assertIn("[<emoji> <Workstream Name> · rt-id]", clause)

    def test_people_profiles_are_written_directly_never_committed(self) -> None:
        """Rule: workers write people/ directly, with no approval step. The
        no-main-write sentence must not read as covering people/, so the prompt names
        people/ as the direct-write exception and keeps the never-commit rule."""
        task = {"id": "rt-pp", "attempt_id": "rt-pp.a1.abc", "workstream": "data-imports", "origin": "cos",
                "prompt": "Workstream: data-imports\nIntake the Beefree zip"}
        worktree = Path("/tmp/wt-rt-pp")
        prompt = dispatch.launch_prompt(task, worktree, "worker-pp")
        clause = next((line for line in prompt.splitlines() if line.startswith("People profiles")), "")
        self.assertTrue(clause, "launch prompt carries a People profiles clause")
        self.assertIn("write people/ directly, no approval and no staging", clause)
        self.assertIn(f"{worktree}/people/<firstname-lastname>.md", clause)
        self.assertIn("people/_template.md", clause)
        self.assertIn("never `git add` or commit it", clause)
        self.assertIn("Only if that link is missing or unwritable", clause)
        self.assertIn("this exception is people/ only", clause)
        main_rule = next(line for line in prompt.splitlines() if "Do not edit the main checkout" in line)
        self.assertLess(prompt.index(main_rule), prompt.index(clause), "the exception follows the rule it carves out")

    def test_email_drafts_come_from_the_owners_account_through_the_connector(self) -> None:
        """Drafts go through the connector shim's draft verb from the owner's
        own account and are never sent."""
        task = {"id": "rt-gm", "attempt_id": "rt-gm.a1.abc", "workstream": "replies", "origin": "cos",
                "prompt": "Workstream: replies\nDraft the reply email"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-gm"), "worker-gm")
        clause = next((line for line in prompt.splitlines() if line.startswith("Email drafts:")), "")
        self.assertTrue(clause, "launch prompt carries an email draft clause")
        self.assertIn("`switchboard mail draft`", clause)
        self.assertIn("never sent", clause)

    def test_stakeholder_reply_clause_requires_the_draft_in_the_report(self) -> None:
        """Every draft is shown in the report, never only "draft parked". The prompt
        tells the worker to carry the full text as a fenced block."""
        task = {"id": "rt-dr", "attempt_id": "rt-dr.a1.abc", "workstream": "quiz-scoring", "origin": "cos",
                "prompt": "Workstream: quiz-scoring\nReply to Perry"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-dr"), "worker-dr")
        clause = next((line for line in prompt.splitlines() if line.startswith("Stakeholder replies:")), "")
        self.assertTrue(clause, "launch prompt carries a Stakeholder replies clause")
        self.assertIn("Draft for the owner to send", clause)
        self.assertIn("The draft is always shown", clause)
        self.assertIn("full draft text verbatim", clause)
        self.assertIn("``` fenced code block", clause)
        self.assertIn("⏎", clause)
        self.assertIn("\"draft parked\" is never enough", clause)
        self.assertLess(clause.index("Draft for the owner to send"), clause.index("The draft is always shown"))

    def test_stakeholder_reply_clause_links_references_and_mentions_people(self) -> None:
        """A draft links every thread or PR it refers to and @mentions every person.
        The prompt carries the rule and names the warn-only guardrail."""
        task = {"id": "rt-lm", "attempt_id": "rt-lm.a1.abc", "workstream": "survey-results", "origin": "cos",
                "prompt": "Workstream: survey-results\nReply to Oakley about the PR"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-lm"), "worker-lm")
        clause = next((line for line in prompt.splitlines() if line.startswith("Stakeholder replies:")), "")
        self.assertTrue(clause, "launch prompt carries a Stakeholder replies clause")
        self.assertIn("Link every reference and @mention every person", clause)
        self.assertNotIn("2026-10-28", clause)
        self.assertIn("Rule 4 item 6", clause)
        self.assertIn("slack-people-links", clause)
        self.assertIn("switchboard slack people", clause)
        self.assertIn("Email uses full names", clause)
        # Steer additions: shorthand links to the artifact.
        self.assertIn("ref 05", clause)
        self.assertIn("spells out the full file or section name on first mention", clause)
        self.assertEqual(clause.count("Link every reference and @mention every person"), 1)

    def test_stakeholder_reply_clause_requires_the_record_before_the_promise(self) -> None:
        """Rule 9: file the record before promising it, and paste its id and URL in the
        response. A reply must never promise "filing two things" with no issue behind
        it, so the prompt says file first and
        carry the ids and URLs verbatim (external-replies Rule 9)."""
        task = {"id": "rt-fl", "attempt_id": "rt-fl.a1.abc", "workstream": "survey-results", "origin": "cos",
                "prompt": "Workstream: survey-results\nReply to the stakeholder"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-fl"), "worker-fl")
        clause = next((line for line in prompt.splitlines() if line.startswith("Stakeholder replies:")), "")
        self.assertTrue(clause, "launch prompt carries a Stakeholder replies clause")
        self.assertIn("File before you promise", clause)
        self.assertIn("Rule 9", clause)
        self.assertIn("filing, tracking, or logging", clause)
        self.assertIn("before or with the draft", clause)
        self.assertIn("your tracker's CLI or API", clause)
        self.assertIn("record ids and URLs verbatim", clause)
        self.assertIn("https://linear.app/", clause)
        self.assertIn("CoS does not send it", clause)
        self.assertIn("⚠️ Filing promised, no record URL", clause)
        self.assertLess(clause.index("The draft is always shown"), clause.index("File before you promise"))

    def test_decision_reports_carry_an_options_segment_and_a_lean(self) -> None:
        """The options in the CoS Review app and in CoS must match, so a card never shows
        status defaults while CoS reads real options from the review note. The prompt
        tells a worker finishing blocked or needs-ruling to put the options and the lean in the
        report text in the one shape both surfaces parse, and names the warning it earns otherwise."""
        task = {"id": "rt-op", "attempt_id": "rt-op.a1.abc", "workstream": "switchboard-maintenance", "origin": "cos",
                "prompt": "Workstream: switchboard-maintenance\nFix the feed"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-op"), "worker-op")
        lines = prompt.splitlines()
        clause = next((line for line in lines if line.startswith("Options on decision reports")), "")
        self.assertTrue(clause, "launch prompt carries an Options on decision reports clause")
        self.assertIn("--status blocked", clause)
        self.assertIn("--needs-ruling", clause)
        self.assertIn("Options: (a)", clause)
        self.assertIn("Lean: <letter> because <reason>", clause)
        self.assertIn("At most three", clause)
        self.assertIn("⚠️ Options missing", clause)
        self.assertIn("options from note", clause)
        stakeholder = next(i for i, line in enumerate(lines) if line.startswith("Stakeholder replies:"))
        agent_mode = next(i for i, line in enumerate(lines) if line.startswith("Agent-mode routing:"))
        self.assertLess(stakeholder, lines.index(clause))
        self.assertLess(lines.index(clause), agent_mode)

    def test_done_reports_declare_open_work_as_a_followup_line(self) -> None:
        """Open work named only in a done report's prose ("reload plugins after landing")
        reaches no surface. The prompt tells a worker finishing done with open work to carry one
        "Follow-up: <what stays open>" line, to count vetoes as "N veto items", and names the warning."""
        task = {"id": "rt-fu", "attempt_id": "rt-fu.a1.abc", "workstream": "switchboard-maintenance", "origin": "cos",
                "prompt": "Workstream: switchboard-maintenance\nFix the feed"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-fu"), "worker-fu")
        lines = prompt.splitlines()
        clause = next((line for line in lines if line.startswith("Follow-up on done reports")), "")
        self.assertTrue(clause, "launch prompt carries a Follow-up on done reports clause")
        self.assertIn("--status done", clause)
        self.assertIn('"Follow-up: <what stays open>"', clause)
        self.assertIn("Needs You", clause)
        self.assertIn("N veto items", clause)
        self.assertIn("⚠️ Follow-up not declared", clause)
        self.assertIn("reaches nobody", clause)
        options = next(i for i, line in enumerate(lines) if line.startswith("Options on decision reports"))
        agent_mode = next(i for i, line in enumerate(lines) if line.startswith("Agent-mode routing:"))
        self.assertLess(options, lines.index(clause))
        self.assertLess(lines.index(clause), agent_mode)

    def test_parked_turns_reach_cos_without_a_hand_sent_heads_up(self) -> None:
        """A hand-sent heads-up with `cmux agent message "<text>" --to "🧭 CoS"` fails: cmux has no
        --to flag, so it reads the text as the target. The prompt says the Stop hook (turn_wait.py) sends the needs-ruling callback, forbids the
        hand-sent heads-up, and names the owner_tabs.py path for a genuine mid-task message."""
        task = {"id": "rt-pk", "attempt_id": "rt-pk.a1.abc", "workstream": "signup-flow", "origin": "cos",
                "prompt": "Workstream: signup-flow\nPrep the event page"}
        prompt = dispatch.launch_prompt(task, Path("/tmp/wt-rt-pk"), "worker-pk")
        lines = prompt.splitlines()
        clause = next((line for line in lines if line.startswith("Parked turns reach CoS on their own")), "")
        self.assertTrue(clause, "launch prompt carries a Parked turns reach CoS clause")
        self.assertIn("turn_wait.py", clause)
        self.assertIn("Do not hand-send a heads-up to CoS", clause)
        self.assertIn("owner_tabs.py cos-target", clause)
        self.assertIn("owner_tabs.py message --ref", clause)
        self.assertIn("no --to flag", clause)
        followup = next(i for i, line in enumerate(lines) if line.startswith("Follow-up on done reports"))
        self.assertLess(followup, lines.index(clause))


class HermeticRepo(unittest.TestCase):
    """A real git repo with a worker worktree for `rt-wt`, so clean/dirty/unlanded
    judgements run against git rather than stubs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "core.fsmonitor", "false")   # ~/.gitconfig turns it on; a fixture repo must not leave a daemon behind
        (self.repo / "a.md").write_text("base\n")
        (self.repo / ".gitignore").write_text(".amp/*\n")  # as in the real repo: the dir is tracked-visible, its contents ignored
        self._git("add", "--", "a.md", ".gitignore")
        self._git("commit", "-q", "-m", "base")
        self.agents: dict[str, dict] = {"T-owner": {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-owner", "title": "CoS"}}
        self.workspaces: list[dict] = []
        self.closed: list[str] = []
        self.notified: list[tuple[str, str, str]] = []
        self.patches = [patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "WORKTREES", self.repo / ".amp" / "worktrees"),
                        patch.object(ledger, "REPO_ROOT", self.repo),
                        patch.object(ledger, "live_agents", lambda: self.agents),
                        patch.object(dispatch, "cmux_workspaces", lambda *a, **k: self.workspaces),
                        patch.object(dispatch, "cmux_close_result", lambda ref, wid, *why: (self.closed.append(ref) or True, "")),
                        patch.object(dispatch, "tab_idle", lambda ref: False),   # never read a real cmux screen from here
                        patch.object(dispatch, "tab_is_bare_shell", lambda ref, agent: False),
                        # The stub cmux shows no screen, so a launch would poll for 30 s; the
                        # launch verification tests set their own budget.
                        patch.object(dispatch, "AMP_VERIFY_SECONDS", 0),
                        patch.object(dispatch, "notify", lambda tid, text, status="active", **kw:
                                     self.notified.append((tid, status, text)) or {"delivered": True})]
        for p in self.patches:
            p.start()
        self.path, self.branch = dispatch.create_worktree("rt-wt")
        self.stale = iso(datetime.now(timezone.utc) - ledger.STALE_AFTER - timedelta(minutes=1))

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return dispatch.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]).stdout

    def row(self, tid: str = "rt-wt") -> dict:
        return ledger.find_task(ledger.load_payload(), tid) or {}

    def worker_row(self, **extra) -> dict:
        return {"id": "rt-wt", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "audit it",
                "attempt": 1, "attempt_id": "rt-wt.a1.aaa", "worker_id": "worker-aaaaaaaaaaaa", "worktree": str(self.path),
                "branch": self.branch, "thread_id": "T-dead", "workspace_ref": "workspace:40",
                "started_at": self.stale, "heartbeat_at": self.stale, "updated_at": self.stale, **extra}


class ReapStopsLeftoverProcessesTests(HermeticRepo):
    """A dev server or a tail monitor must not outlive its task. Reap stops what runs inside a removed worktree and the
    env-tagged dev servers of finished, reaped tasks, and never a protected helper.
    Wrong impls: remove the worktree before stopping what runs in it; stop a dev
    server only by worktree path (it runs in the shared react-starter folder); stop
    the browser broker because it inherited a worker's environment."""

    OLD = "rt-000000000007"

    def setUp(self) -> None:
        super().setUp()
        task_leftovers = dispatch.task_leftovers   # the copy dispatch imported (isolated_load)
        self.killed: list[int] = []
        self.order: list[str] = []
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        write_ledger(self.worker_row(status="done", report_delivered=True, finished_at=finished, summary="done"),
                     {"id": self.OLD, "status": "done", "reaped_at": finished, "worktree": str(self.repo / ".amp" / "worktrees" / self.OLD)})
        procs = [
            task_leftovers.Proc(500, 1, "bash.exe", "bash -c 'tail -f build.log'", cwd=str(self.path / "system"), created=5.0),
            task_leftovers.Proc(501, 500, "tail.exe", "tail -f build.log", cwd=str(self.path / "system"), created=6.0),
            task_leftovers.Proc(600, 1, "node.exe", "node node_modules/vite/bin/vite.js --port 5173", cwd=str(self.repo / "react-starter"),
                                created=5.0, env_task=self.OLD),
            task_leftovers.Proc(700, 1, "python3.exe", "python3 owner_browser.py broker", cwd=str(self.path), created=5.0, env_task=self.OLD),
        ]
        real_drop = dispatch.git
        def fake_kill(victim):
            self.killed.append(victim["pid"])
            self.order.append(f"kill {victim['pid']}")
            return ""
        def watch_git(*args, **kw):
            if args[:2] == ("worktree", "remove"):
                self.order.append("worktree remove")
            return real_drop(*args, **kw)
        for extra in (patch.object(task_leftovers, "snapshot", lambda: procs), patch.object(task_leftovers, "kill", fake_kill),
                      patch.object(dispatch, "git", watch_git),
                      patch.object(dispatch, "LEFTOVER_LOG", self.repo / "leftover-stops.jsonl"),
                      patch.object(dispatch, "LEFTOVER_SWEEP_STAMP", self.repo / "leftover-sweep.stamp")):
            extra.start()
            self.patches.append(extra)

    def test_reap_stops_worktree_processes_before_removal_and_finished_dev_servers(self) -> None:
        with redirect_stderr(io.StringIO()):
            result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.order[:3], ["kill 501", "kill 500", "worktree remove"])
        self.assertIn(600, self.killed)
        self.assertNotIn(700, self.killed)
        self.assertEqual(result["leftover_sweep"]["tasks"], [self.OLD])
        logged = [json.loads(line) for line in (self.repo / "leftover-stops.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(r["pid"] for r in logged), [500, 501, 600])

    def test_kill_switch_leaves_everything_running(self) -> None:
        with patch.dict(os.environ, {"SWITCHBOARD_LEFTOVER_SWEEP": "off"}), redirect_stderr(io.StringIO()):
            result = dispatch.reap_all(dispatch.config())
        self.assertEqual(self.killed, [])
        self.assertNotIn("leftover_sweep", result)


class ReapSettlesCancelledAndUnlandedTests(HermeticRepo):
    """A row cancelled by its origin while its worker runs on: the reaper must not
    wait for a report that is ignored by design, and must not delete a worktree
    that holds uncommitted fixes. Wrong impls: require report_delivered for cancelled
    rows; drop a worktree that holds work main lacks; treat a tool-label
    lifecycle state as idle; let a protected ref that no longer resolves pin a
    row open forever."""

    def setUp(self) -> None:
        super().setUp()
        self.ws = {"ref": "workspace:40", "id": "w-40", "title": "switchboard-maintenance · worker-aaaaaaaaaaaa"}
        self.workspaces = [self.ws]
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        write_ledger(self.worker_row(status="cancelled", report_delivered=False, finished_at=finished,
                                     summary="cancelled as superseded"))

    def test_cancelled_row_is_reaped_without_a_report_and_its_dirty_worktree_survives(self) -> None:
        (self.path / "a.md").write_text("300 lines of fixes\n")
        self.agents["T-dead"] = {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-40", "title": "worker"}
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(self.closed, ["workspace:40"])
        self.assertEqual(result["closed"][0]["worktree_holds"], "uncommitted edits")
        self.assertTrue(self.path.is_dir(), "worktree with work must survive the reap")
        self.assertEqual((self.path / "a.md").read_text(), "300 lines of fixes\n")
        self.assertEqual(self.row()["worktree_preserved"], str(self.path))
        self.assertTrue(self.row()["reaped_at"])

    def test_cancelled_row_with_a_clean_worktree_is_dropped(self) -> None:
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertNotIn("worktree_preserved", self.row())
        self.assertFalse(self.path.exists())
        self.assertEqual(self._git("branch", "--list", self.branch).strip(), "")

    def test_worker_mid_tool_is_still_running(self) -> None:
        self.agents["T-dead"] = {"state": "shell_command: pytest …", "state_ts": datetime.now(timezone.utc).timestamp(),
                                 "workspace_id": "w-40", "title": "worker"}
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "agent still running"}])
        self.assertEqual(self.closed, [])

    def test_done_row_whose_branch_never_landed_keeps_its_tab_and_branch(self) -> None:
        # Close-out order: land, record, reap. finish_branch leaves a conflicting
        # branch "for a human". Wrong impls: close the tab while the branch
        # holds commits main lacks; drop the branch.
        (self.path / "a.md").write_text("branch commit\n")
        self._git("commit", "-q", "-am", "work", cwd=self.path)
        write_ledger(self.worker_row(status="done", report_delivered=True,
                                     finished_at=iso(datetime.now(timezone.utc) - timedelta(minutes=30))))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": f"branch {self.branch} has not landed on main"}])
        self.assertEqual(self.closed, [])
        self.assertTrue(self._git("branch", "--list", self.branch).strip())
        self.assertTrue(self.path.is_dir())
        # Once the tab is gone (closed by hand), the row still settles and keeps the branch.
        self.workspaces = []
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"][0]["worktree_holds"], "commits")
        self.assertFalse(result["closed"][0]["was_open"])
        self.assertTrue(self._git("branch", "--list", self.branch).strip())

    def test_other_terminal_rows_still_wait_for_their_report(self) -> None:
        # No summary on the row: the report still lives only in the tab, so neither
        # a waiter nor the loop could replay it. The tab stays.
        write_ledger(self.worker_row(status="done", report_delivered=False,
                                     finished_at=iso(datetime.now(timezone.utc) - timedelta(minutes=30))))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])
        self.assertEqual(self.closed, [])

    # Swap pressure: done worker tabs must not sit behind "report not delivered
    # yet" while a detached report-waiter.sh already holds each report and the
    # command-center loop retries every terminal row with a summary on each tick.
    # A done row whose report is carried that way closes; the row's summary and
    # report_delivered flag are untouched so the carrier still delivers it.
    # Wrong impls: close a row with no summary (report lost); close a row whose
    # delivery is outcome-unknown (no carrier retries it); flip report_delivered
    # on close; close a blocked row or an unlanded branch because a waiter exists.

    def _done_pending(self, **extra) -> dict:
        fields = {"status": "done", "report_delivered": False, "summary": "done: audited it https://ampcode.com/threads/T-dead",
                  "finished_at": iso(datetime.now(timezone.utc) - timedelta(minutes=30))}
        fields.update(extra)
        return self.worker_row(**fields)

    def _waiter_pidfile(self) -> Path:
        pidfile = dispatch.STATE_DIR / "worker-dispatch" / "report-waiter-rt-wt.pid"
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: pidfile.unlink(missing_ok=True))
        return pidfile

    # Report must land first: a tab reaped while a waiter only "carries" its report
    # can leave CoS with the tail of it. By default a tab stays until report_delivered is True, waiter or
    # not. Wrong impls: close on a live waiter; close on the loop carrier; close
    # on report_unproven_at; flip report_delivered on close.

    def test_report_not_delivered_keeps_the_tab_even_with_a_live_waiter(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending())
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])
        self.assertEqual(self.closed, [])
        row = self.row()
        self.assertIs(row["report_delivered"], False)
        self.assertNotIn("reaped_at", row)

    def test_report_not_delivered_keeps_the_tab_when_only_the_loop_carries_it(self) -> None:
        self._waiter_pidfile().write_text("999999999\n")
        write_ledger(self._done_pending())
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])
        self.assertEqual(self.closed, [])

    def test_unproven_report_keeps_the_tab(self) -> None:
        write_ledger(self._done_pending(report_unproven_at=ledger.now_iso()))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])
        self.assertEqual(self.closed, [])

    def test_the_tab_closes_once_the_report_is_proven(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending())
        self.assertEqual(dispatch.reap_all(dispatch.config())["closed"], [])
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-wt").update(
            report_delivered=True, report_delivered_at=ledger.now_iso()))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(self.closed, ["workspace:40"])

    def carrier_cfg(self) -> dict:
        return {**dispatch.config(), "reap_requires_delivered_report": False}

    def test_done_tab_closes_when_a_live_waiter_holds_its_report(self) -> None:
        # Opt-out (router.yaml reap_requires_delivered_report: false) keeps the
        # older carrier rule.
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending())
        result = dispatch.reap_all(self.carrier_cfg())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(result["closed"][0]["report_carried_by"], "waiter")
        self.assertEqual(self.closed, ["workspace:40"])
        row = self.row()
        self.assertIs(row["report_delivered"], False, "reap must not mark the report delivered")
        self.assertEqual(row["summary"], "done: audited it https://ampcode.com/threads/T-dead")
        self.assertTrue(row["reaped_at"])

    def test_done_tab_closes_when_the_loop_carries_its_report(self) -> None:
        # No waiter (or a dead one): command-center-loop.sh replays every terminal
        # row with report_delivered False and a summary, without --workspace-ref.
        self._waiter_pidfile().write_text("999999999\n")
        write_ledger(self._done_pending())
        result = dispatch.reap_all(self.carrier_cfg())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(result["closed"][0]["report_carried_by"], "loop")
        self.assertEqual(self.closed, ["workspace:40"])
        self.assertIs(self.row()["report_delivered"], False)

    def test_done_tab_with_an_unknown_delivery_outcome_still_waits(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending(report_delivery_unknown=True, report_attempted_at=ledger.now_iso()))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])
        self.assertEqual(self.closed, [])

    def test_done_tab_with_a_blank_summary_still_waits(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending(summary="   "))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "report not delivered yet"}])

    def test_blocked_tab_with_a_carried_report_stays_open_for_owner(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        write_ledger(self._done_pending(status="blocked"))
        result = dispatch.reap_all(self.carrier_cfg())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "blocked result stays open for the owner"}])
        self.assertEqual(self.closed, [])

    def test_unlanded_branch_with_a_carried_report_keeps_its_tab(self) -> None:
        self._waiter_pidfile().write_text(f"{os.getpid()}\n")
        (self.path / "a.md").write_text("branch commit\n")
        self._git("commit", "-q", "-am", "work", cwd=self.path)
        write_ledger(self._done_pending())
        result = dispatch.reap_all(self.carrier_cfg())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": f"branch {self.branch} has not landed on main"}])
        self.assertEqual(self.closed, [])

    def test_delivered_row_reports_no_carrier(self) -> None:
        write_ledger(self.worker_row(status="done", report_delivered=True,
                                     finished_at=iso(datetime.now(timezone.utc) - timedelta(minutes=30))))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertNotIn("report_carried_by", result["closed"][0])

    def test_reap_protection_is_by_title_not_by_ref(self) -> None:
        # The protected_workspaces ref list is gone; the
        # WORKER_TITLE rule alone decides. Wrong impls: pin a row whose ref no longer
        # resolves; close a non-worker tab; keep a worker tab open because its ref was
        # on the old list (cmux renumbers refs on restart, so 252 may now hold anything).
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        self.workspaces = []
        write_ledger(self.worker_row(status="done", report_delivered=True, workspace_ref="workspace:252", finished_at=finished))
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertFalse(result["closed"][0]["was_open"])
        # A resolving tab whose title is not a worker tab is never closed, whatever its ref.
        write_ledger(self.worker_row(status="done", report_delivered=True, workspace_ref="workspace:252", finished_at=finished))
        self.workspaces = [{"ref": "workspace:252", "id": "w-252", "title": "🧭 CoS"}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": "title is not a worker tab of rt-wt (🧭 CoS)"}])
        self.assertEqual(self.closed, [])
        # A worker-titled tab is closed even on a ref the old list named, and a stray
        # protected_workspaces key in router.yaml no longer changes the verdict.
        write_ledger(self.worker_row(status="done", report_delivered=True, workspace_ref="workspace:252", finished_at=finished))
        self.workspaces = [{"ref": "workspace:252", "id": "w-252", "title": "switchboard-maintenance · worker-aaaaaaaaaaaa"}]
        result = dispatch.reap_all({**dispatch.config(), "protected_workspaces": ["workspace:252"]})
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(self.closed, ["workspace:252"])

    def test_row_without_worktree_field_judges_its_own_checkout_not_main(self) -> None:
        # A row with no `worktree` field must not resolve Path("") = main, whose dirty
        # tree would read as "uncommitted edits" and keep worktrees forever. Wrong
        # impl: read main's status; right impl: WORKTREES/<id>.
        (self.repo / "a.md").write_text("main is dirty\n")
        row = self.worker_row(status="done", report_delivered=True)
        del row["worktree"]
        self.assertEqual(dispatch.unlanded_work(row), "")
        (self.path / "a.md").write_text("worker edit\n")
        self.assertEqual(dispatch.unlanded_work(row), "uncommitted edits")

    def test_default_config_has_no_protected_workspaces(self) -> None:
        self.assertNotIn("protected_workspaces", dispatch.DEFAULTS)
        self.assertEqual(dispatch.protected({"ref": "workspace:5", "id": "w-5", "title": "🧭 CoS"}),
                         "title is not a worker tab (🧭 CoS)")
        self.assertEqual(dispatch.protected({"ref": "workspace:8", "id": "w-8", "custom_title": "🚀 Launchpad"}),
                         "title is not a worker tab (🚀 Launchpad)")
        self.assertEqual(dispatch.protected({"ref": "workspace:9", "id": "w-9", "title": "comms-triage · worker-7d516b5d0f14"}), "")
        self.assertEqual(dispatch.protected(None), "")


class DecideTests(HermeticRepo):
    """CoS Review pass 4: a go / veto / keep / "option N" reply
    on a task records the decision on the ledger row and in the owning
    workstream's CONTEXT.md on main, and dispatches nothing. Wrong impls: open a
    continuation task for a decision; rewrite CONTEXT.md on main when it holds
    someone's uncommitted edits (the line must land without a commit); record the
    same decision twice; change the row's status."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.repo / "workstreams" / "switchboard-maintenance" / "CONTEXT.md"
        self.ctx.parent.mkdir(parents=True)
        self.ctx.write_text("# Switchboard Maintenance\n\nStatus: active\n")
        self._git("add", "--", "workstreams")
        self._git("commit", "-q", "-m", "ctx")
        write_ledger(self.worker_row(status="done"))

    def decide(self, *words: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dispatch.command_decide(argparse.Namespace(task_id="rt-wt", by="owner", text=list(words)))
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_decision_lands_on_row_and_context_and_commits_when_clean(self) -> None:
        rc, result = self.decide("go", "with", "option 2")
        self.assertEqual((rc, result["status"]), (0, "decided"), result)
        row = self.row()
        self.assertEqual(row["status"], "done", "a decision never changes the row's status")
        self.assertEqual([d["text"] for d in row["decisions"]], ["go with option 2"])
        self.assertIn("decision: go with option 2", row["evidence"])
        body = self.ctx.read_text()
        self.assertIn("## the owner decisions", body)
        self.assertIn("`rt-wt` · owner: go with option 2", body)
        self.assertTrue(result["context"]["committed"], result)
        self.assertEqual(self._git("status", "--porcelain", "--", "workstreams").strip(), "")
        self.assertNotIn("task/rt-wt-", self._git("branch", "--list"), "a decision opens no continuation")

    def test_repeated_decision_is_recorded_once(self) -> None:
        self.decide("veto")
        rc, result = self.decide("veto")
        self.assertEqual(rc, 0)
        self.assertTrue(result["ledger"].get("_duplicate_decision"), result)
        self.assertEqual(result["context"]["reason"], "already recorded")
        self.assertEqual(self.ctx.read_text().count("owner: veto"), 1)
        self.assertEqual(len(self.row()["decisions"]), 1)

    def test_dirty_context_on_main_gets_the_line_but_no_commit(self) -> None:
        self.ctx.write_text(self.ctx.read_text() + "\nsomeone's unsaved paragraph\n")
        rc, result = self.decide("keep")
        self.assertEqual(rc, 0)
        self.assertFalse(result["context"]["committed"], result)
        body = self.ctx.read_text()
        self.assertIn("someone's unsaved paragraph", body, "the other edit survives")
        self.assertIn("owner: keep", body)
        self.assertIn(" M workstreams/switchboard-maintenance/CONTEXT.md", self._git("status", "--porcelain"))

    def test_unknown_task_fails_without_touching_context(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dispatch.command_decide(argparse.Namespace(task_id="rt-nope", by="owner", text=["go"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("the owner decisions", self.ctx.read_text())


class WorkerTabIdentityTests(HermeticRepo):
    """Worker tabs are titled like the owner tab
    plus a slot number ("⚖️ Meal Planner 2") and carry the description
    "switchboard worker <task-id>". Wrong impls: a renamed owner tab passes as a worker
    because its title ends in a digit; one worker reaps another's tab; two
    concurrent workers of one workstream take the same slot; a tab whose row has
    not yet opened loses its slot to the next launch."""

    def setUp(self) -> None:
        super().setUp()
        ws = self.repo / "workstreams"
        (ws / "meal-planner").mkdir(parents=True)
        (ws / "meal-planner" / "config.yaml").write_text('name: "Meal Planner"\nemoji: "⚖️"\nstatus: active\n')
        (ws / "switchboard-maintenance").mkdir()
        (ws / "switchboard-maintenance" / "config.yaml").write_text("name: Switchboard Maintenance\nemoji: 🛠️\n")
        (ws / "bare-slug").mkdir()
        (ws / "bare-slug" / "config.yaml").write_text("status: active\n")

    def test_titles_come_from_config_yaml_like_the_owner_tab(self) -> None:
        self.assertEqual(dispatch.owner_title("meal-planner"), "⚖️ Meal Planner")
        self.assertEqual(dispatch.owner_title("switchboard-maintenance"), "🛠️ Switchboard Maintenance")
        self.assertEqual(dispatch.owner_title("bare-slug"), "📁 Bare Slug")        # cmux-helpers fallback
        self.assertEqual(dispatch.owner_title("no-such-workstream"), "📁 No Such Workstream")
        self.assertEqual(dispatch.worker_title("meal-planner", 2), "⚖️ Meal Planner 2")
        self.assertEqual(dispatch.worker_description("rt-abc"), "switchboard worker rt-abc")
        self.assertEqual(dispatch.owner_titles()["⚖️ Meal Planner"], "meal-planner")

    def test_next_slot_is_the_lowest_free_integer_across_tabs_and_opening_rows(self) -> None:
        tabs = [{"ref": "workspace:1", "title": "⚖️ Meal Planner"},            # the owner tab holds no slot
                {"ref": "workspace:2", "title": "⚖️ Meal Planner 1"},
                {"ref": "workspace:3", "title": "⚖️ Meal Planner 3"},
                {"ref": "workspace:4", "custom_title": "🛠️ Switchboard Maintenance 2"}]  # another workstream's slot
        payload = {"tasks": [{"workstream": "meal-planner", "status": "active", "tab_title": "⚖️ Meal Planner 2"},
                             {"workstream": "meal-planner", "status": "done", "tab_title": "⚖️ Meal Planner 4"}]}
        self.assertEqual(dispatch.next_worker_slot("meal-planner", payload, tabs), 4)   # 1,3 open; 2 opening; 4's row is terminal
        self.assertEqual(dispatch.next_worker_slot("switchboard-maintenance", payload, tabs), 1)
        self.assertEqual(dispatch.next_worker_slot("meal-planner", {"tasks": []}, []), 1)

    def test_worker_tab_reason_prefers_the_description_and_rejects_renamed_owner_tabs(self) -> None:
        mine = {"id": "rt-wt", "tab_title": "⚖️ Meal Planner 1"}
        other = {"ref": "workspace:7", "title": "⚖️ Meal Planner 1", "description": "switchboard worker rt-other"}
        self.assertEqual(dispatch.worker_tab_reason(other, mine), "tab belongs to another worker (rt-other)")
        self.assertEqual(dispatch.worker_tab_reason(other), "")                      # still a worker tab, just not mine
        self.assertEqual(dispatch.worker_tab_reason({"title": "anything", "description": "switchboard worker rt-wt"}, mine), "")
        # A reused slot title alone never proves the tab is this row's; its own ref plus the title does.
        self.assertEqual(dispatch.worker_tab_reason({"title": "⚖️ Meal Planner 1"}, mine),
                         "title is not a worker tab of rt-wt (⚖️ Meal Planner 1)")
        self.assertEqual(dispatch.worker_tab_reason({"ref": "workspace:7", "title": "⚖️ Meal Planner 1"},
                                                    {**mine, "workspace_ref": "workspace:7"}), "")
        # With no row to compare: "<owner> <n>" for a known workstream, the legacy form.
        self.assertEqual(dispatch.worker_tab_reason({"title": "🛠️ Switchboard Maintenance 12"}), "")
        self.assertEqual(dispatch.worker_tab_reason({"title": "switchboard-maintenance · worker-0123456789ab"}), "")
        # Not workers: owner tabs, an owner tab the owner renamed to end in a digit for an unknown
        # workstream, a P0 tab, fixed surfaces, a slot of 0.
        self.assertEqual(dispatch.worker_tab_reason({"title": "⚖️ Meal Planner"}), "title is not a worker tab (⚖️ Meal Planner)")
        self.assertEqual(dispatch.worker_tab_reason({"title": "🧪 Pilot Wave 2"}), "title is not a worker tab (🧪 Pilot Wave 2)")
        self.assertEqual(dispatch.worker_tab_reason({"title": "⚖️ P0 · Meal Planner 1"}), "title is not a worker tab (⚖️ P0 · Meal Planner 1)")
        self.assertEqual(dispatch.worker_tab_reason({"title": "⚖️ Meal Planner 0"}), "title is not a worker tab (⚖️ Meal Planner 0)")
        self.assertEqual(dispatch.worker_tab_reason({"title": "🧭 CoS", "description": "CoS · idle"}), "title is not a worker tab (🧭 CoS)")

    def test_reaper_closes_only_the_tab_whose_description_names_the_task(self) -> None:
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        write_ledger(self.worker_row(status="done", report_delivered=True, finished_at=finished,
                                     workstream="meal-planner", tab_title="⚖️ Meal Planner 1"))
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "⚖️ Meal Planner 1", "description": "switchboard worker rt-other"}]
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [])   # another worker's tab is not this row's tab at all, ref or title
        write_ledger(self.worker_row(status="done", report_delivered=True, finished_at=finished,
                                     workstream="meal-planner", tab_title="⚖️ Meal Planner 1"))
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "⚖️ Meal Planner 1", "description": "switchboard worker rt-wt"}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(self.closed, ["workspace:40"])

    def test_reaper_finds_the_tab_by_description_when_the_row_holds_its_uuid(self) -> None:
        # A worker that passes its tab UUID as --workspace-ref leaves a row that no
        # longer says "workspace:N"; the sweep must still find the tab. Wrong impls: settle without looking for the tab;
        # close a tab whose description names another task.
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        uuid = "603CBCB6-FE9F-492C-B399-1FD5C9C89B99"
        write_ledger(self.worker_row(status="done", report_delivered=True, finished_at=finished, workspace_ref=uuid,
                                     workstream="meal-planner", tab_title="⚖️ Meal Planner 1"))
        self.workspaces = [{"ref": "workspace:83", "id": uuid, "title": "⚖️ Meal Planner 1", "description": "switchboard worker rt-wt"},
                           {"ref": "workspace:84", "id": "w-84", "title": "⚖️ Meal Planner 2", "description": "switchboard worker rt-other"}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([(c["task_id"], c["workspace_ref"], c["was_open"]) for c in result["closed"]], [("rt-wt", "workspace:83", True)])
        self.assertEqual(self.closed, ["workspace:83"])
        # No tab anywhere for a non-"workspace:" ref: the checkout settles, nothing closes.
        write_ledger(self.worker_row(status="done", report_delivered=True, finished_at=finished, workspace_ref="nonsense"))
        self.workspaces = [self.workspaces[1]]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual((result["closed"], result["skipped"]), ([], []))
        self.assertTrue(self.row()["reaped_at"])
        self.assertEqual(self.closed, ["workspace:83"])


class CloseoutRecordTests(HermeticRepo):
    """No worker tab closes without one durable line
    in the owning workstream's CONTEXT.md. Wrong impls: record twice (report-to-cos
    and the reaper both call it); record a cancelled or still-active row; commit a
    CONTEXT.md someone had mid-edit on main along with their edits; lose the line
    when the heading already exists mid-file."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.repo / "workstreams" / "switchboard-maintenance" / "CONTEXT.md"
        self.ctx.parent.mkdir(parents=True)
        (self.ctx.parent / "config.yaml").write_text("name: Switchboard Maintenance\nemoji: 🛠️\n")
        self.ctx.write_text("# 🛠️ Switchboard Maintenance\n\n## Log\n\n### 2026-10-05 — something\n")
        self._git("add", "-A", "--", "workstreams")
        self._git("commit", "-q", "-m", "workstream")
        self.task = self.worker_row(status="done", tab_title="🛠️ Switchboard Maintenance 1", thread_id="T-abc",
                                    summary="done: sidebar lane shipped")

    def test_line_format_and_idempotence(self) -> None:
        line = dispatch.closeout_line(self.task, "done", "done: sidebar lane shipped. https://ampcode.com/threads/T-abc", "T-abc")
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(line, f"- {today} · `rt-wt` · 🛠️ Switchboard Maintenance 1 · done: sidebar lane shipped. https://ampcode.com/threads/T-abc")
        # No link in the result -> the thread link is appended; whitespace collapsed; status prefix not doubled.
        line = dispatch.closeout_line(self.task, "blocked", "blocked:  needs\n  the owner", "T-abc")
        self.assertEqual(line, f"- {today} · `rt-wt` · 🛠️ Switchboard Maintenance 1 · blocked: needs the owner · https://ampcode.com/threads/T-abc")
        # A Claude row names its session: no ampcode.com link, in either direction.
        session = "00000000-0000-4000-8000-000000000001"
        claude = {**self.task, "agent": "claude", "session_id": session}
        line = dispatch.closeout_line(claude, "done", "shipped", session)
        self.assertTrue(line.endswith(f"done: shipped · claude --resume {session}"), line)
        self.assertNotIn("ampcode.com", line)
        # Already named by the worker: not doubled. An old prompt's dead link: rewritten.
        line = dispatch.closeout_line(claude, "done", f"shipped. claude --resume {session}", session)
        self.assertEqual(line.count("claude --resume"), 1)
        line = dispatch.closeout_line(claude, "done", f"shipped. https://ampcode.com/threads/{session}", session)
        self.assertNotIn("ampcode.com", line)
        self.assertEqual(line.count("claude --resume"), 1)
        # A legacy row with no agent and a session id gets no Amp link either.
        self.assertNotIn("ampcode.com", dispatch.closeout_line(self.task, "done", "shipped", session))
        # An Amp row keeps its link.
        self.assertIn("https://ampcode.com/threads/T-abc", dispatch.closeout_line({**self.task, "agent": "amp"}, "done", "shipped", "T-abc"))
        first = dispatch.record_closeout(self.task, "done", "shipped", "T-abc", self.repo)
        self.assertTrue(first["recorded"])
        second = dispatch.record_closeout(self.task, "done", "shipped again", "T-abc", self.repo)
        self.assertEqual(second["reason"], "already recorded")
        text = self.ctx.read_text()
        self.assertEqual(text.count("`rt-wt`"), 1)
        self.assertTrue(text.startswith("# 🛠️ Switchboard Maintenance\n\n## Log\n"))
        self.assertTrue(text.endswith(f"\n\n## Worker close-outs\n{first['line']}\n"), text)
        # A second task appends under the existing heading, keeping the first line.
        other = {**self.task, "id": "rt-two", "tab_title": "🛠️ Switchboard Maintenance 2"}
        dispatch.record_closeout(other, "failed", "merge conflict", "T-def", self.repo)
        text = self.ctx.read_text()
        self.assertEqual(text.count("## Worker close-outs"), 1)
        self.assertLess(text.index("`rt-wt`"), text.index("`rt-two`"))

    def test_missing_workstream_directory_is_reported_not_raised(self) -> None:
        outcome = dispatch.record_closeout({**self.task, "workstream": "nope"}, "done", "x", "", self.repo)
        self.assertEqual(outcome, {"recorded": False, "reason": "no workstream directory for nope"})
        self.assertFalse((self.repo / "workstreams" / "nope").exists())

    def test_record_on_main_commits_a_clean_file_and_leaves_a_dirty_one(self) -> None:
        before = self._git("rev-parse", "HEAD").strip()
        outcome = dispatch.record_on_main(self.task)
        self.assertTrue(outcome["recorded"] and outcome["committed"], outcome)
        self.assertNotEqual(self._git("rev-parse", "HEAD").strip(), before)
        self.assertEqual(self._git("status", "--porcelain").strip(), "")
        self.assertIn("sidebar lane shipped", self.ctx.read_text())
        # Dirty file: the line is appended but nothing is committed, and the other edit stays uncommitted.
        self.ctx.write_text(self.ctx.read_text() + "\n### 2026-10-06 — the owner mid-edit\n")
        (self.repo / "a.md").write_text("unrelated edit\n")
        outcome = dispatch.record_on_main({**self.task, "id": "rt-two"}, result="second")
        self.assertTrue(outcome["recorded"])
        self.assertFalse(outcome["committed"])
        self.assertIn("uncommitted edits on main", outcome["reason"])
        self.assertEqual(sorted(dispatch.dirty_main_paths()), ["a.md", "workstreams/switchboard-maintenance/CONTEXT.md"])
        self.assertIn("the owner mid-edit", self.ctx.read_text())
        self.assertIn("`rt-two`", self.ctx.read_text())

    def test_record_on_main_commits_a_context_md_it_creates_for_a_new_workstream(self) -> None:
        # A rename mid-task pointed the row at a folder with no tracked CONTEXT.md. Wrong impl
        # (the old one): `git commit -- <path>` on the file record_closeout just created fails
        # with "pathspec did not match", leaving it untracked on main. Also wrong: `git add -A`
        # sweeping the untracked config.yaml or a human's staged a.md into the close-out commit.
        folder = self.repo / "workstreams" / "reading-list"
        folder.mkdir()
        (folder / "config.yaml").write_text("name: Reading List\nemoji: 📚\n", encoding="utf-8")
        (self.repo / "a.md").write_text("human staged this\n")
        self._git("add", "--", "a.md")
        ctx = "workstreams/reading-list/CONTEXT.md"
        outcome = dispatch.record_on_main({**self.task, "workstream": "reading-list"})
        self.assertTrue(outcome["recorded"] and outcome["committed"], outcome)
        self.assertEqual(outcome["added"], [ctx])
        self.assertEqual(self._git("show", "--name-only", "--format=%an|%s", "HEAD").split(),
                         ["switchboard", "worker|rt-wt:", "worker", "close-out", "recorded", "in", ctx, ctx])
        self.assertIn("`rt-wt`", self._git("show", f"HEAD:{ctx}"))
        status = self._git("status", "--porcelain", "--untracked-files=all").splitlines()
        self.assertEqual(sorted(status), ["?? workstreams/reading-list/config.yaml", "M  a.md"],
                         "only the file record_on_main wrote is committed; the rest of main stays as it was")
        # A second close-out for the same workstream now finds a tracked, clean file.
        again = dispatch.record_on_main({**self.task, "id": "rt-two", "workstream": "reading-list"}, result="second")
        self.assertTrue(again["committed"], again)
        self.assertNotIn("added", again)

    def test_failed_commit_of_a_new_context_md_unstages_it(self) -> None:
        # Wrong impl: the explicit add succeeds, the commit fails, and the new file is left
        # staged on main, where the next unrelated commit would sweep it in.
        (self.repo / "workstreams" / "brand-new").mkdir()
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(b"#!/bin/sh\necho refused by hook >&2\nexit 1\n")
        hook.chmod(0o755)
        self._git("config", "core.hooksPath", str(hook.parent))   # ~/.gitconfig may point hooks elsewhere
        outcome = dispatch.record_on_main({**self.task, "workstream": "brand-new"})
        self.assertTrue(outcome["recorded"])
        self.assertFalse(outcome["committed"])
        self.assertIn("refused by hook", outcome["reason"])
        self.assertEqual(self._git("diff", "--cached", "--name-only").strip(), "")
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=all").strip(),
                         "?? workstreams/brand-new/CONTEXT.md")

    def test_cancelled_and_non_terminal_rows_are_not_recorded(self) -> None:
        self.assertEqual(dispatch.record_on_main({**self.task, "status": "cancelled"})["reason"], "status cancelled is not recorded")
        self.assertEqual(dispatch.record_on_main({**self.task, "status": "active"})["reason"], "status active is not recorded")
        self.assertNotIn("Worker close-outs", self.ctx.read_text())

    def test_reaper_records_before_it_closes(self) -> None:
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        write_ledger({**self.task, "report_delivered": True, "finished_at": finished})
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "🛠️ Switchboard Maintenance 1", "description": "switchboard worker rt-wt"}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertTrue(result["closed"][0]["recorded"])
        self.assertIn("`rt-wt` · 🛠️ Switchboard Maintenance 1 · done: sidebar lane shipped · https://ampcode.com/threads/T-abc", self.ctx.read_text())
        self.assertEqual(self.closed, ["workspace:40"])


class LegacyTabResolutionTests(HermeticRepo):
    """Legacy rows store the tab's *title* as workspace_ref (report-to-cos.sh takes
    whatever the worker passes) and legacy tabs carry no description, so without
    title resolution worker_tab_for finds nothing, the row is silently settled and
    the tab stays open forever. Wrong impls: match
    an owner tab whose title a row happened to record; re-record or re-close a
    row whose tab is really gone; close a legacy tab whose branch never landed."""

    LEGACY = "switchboard-maintenance · worker-aaaaaaaaaaaa"

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.repo / "workstreams" / "switchboard-maintenance" / "CONTEXT.md"
        self.ctx.parent.mkdir(parents=True)
        (self.ctx.parent / "config.yaml").write_text("name: Switchboard Maintenance\nemoji: 🛠️\n")
        self.ctx.write_text("# 🛠️ Switchboard Maintenance\n\n## Log\n")
        self._git("add", "-A", "--", "workstreams")
        self._git("commit", "-q", "-m", "workstream")

    def legacy_row(self, **extra) -> dict:
        finished = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        settled = iso(datetime.now(timezone.utc) - timedelta(hours=2))
        return self.worker_row(**{"status": "done", "report_delivered": True, "finished_at": finished, "thread_id": "T-abc",
                                  "summary": "done: legacy work", "workspace_ref": self.LEGACY, "reaped_at": settled, **extra})

    def test_worker_tab_for_falls_back_to_title_but_never_to_an_owner_tab(self) -> None:
        legacy = {"ref": "workspace:46", "id": "w-46", "title": self.LEGACY}
        owner = {"ref": "workspace:14", "id": "w-14", "title": "product demos: v8 thumbnails with the video title"}
        tabs = [owner, legacy]
        self.assertIs(dispatch.worker_tab_for(self.legacy_row(), tabs), legacy)                 # ref is the title
        self.assertIs(dispatch.worker_tab_for(self.legacy_row(workspace_ref=owner["title"]), tabs), legacy)   # by "<slug> · <worker-id>"
        self.assertIsNone(dispatch.worker_tab_for(self.legacy_row(workspace_ref=owner["title"], worker_id=""), tabs))
        self.assertIsNone(dispatch.worker_tab_for(self.legacy_row(worker_id="worker-bbbbbbbbbbbb"), [owner]))
        recorded = {"ref": "workspace:90", "id": "w-90", "title": "🛠️ Switchboard Maintenance 1"}
        # A recorded slot title with no ref behind it proves nothing: slots are reused.
        self.assertIsNone(dispatch.worker_tab_for(self.legacy_row(workspace_ref="not-a-ref", tab_title=recorded["title"]), [owner, recorded]))
        # A row that stored a slot title as its ref does not match a live tab holding that slot, either.
        self.assertIsNone(dispatch.worker_tab_for(self.legacy_row(workspace_ref=recorded["title"], worker_id=""), [owner, recorded]))

    def test_settled_legacy_row_with_an_open_tab_is_recorded_and_closed_once(self) -> None:
        self._git("worktree", "remove", "--force", str(self.path))   # the silent settle already dropped the checkout
        self._git("branch", "-D", self.branch)
        write_ledger(self.legacy_row())
        self.workspaces = [{"ref": "workspace:46", "id": "w-46", "title": self.LEGACY},
                           {"ref": "workspace:14", "id": "w-14", "title": "🛠️ Switchboard Maintenance"}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual([c["task_id"] for c in result["closed"]], ["rt-wt"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(self.closed, ["workspace:46"])
        self.assertTrue(result["closed"][0]["recorded"])
        self.assertIn("`rt-wt` · worker-aaaaaaaaaaaa · done: legacy work · https://ampcode.com/threads/T-abc", self.ctx.read_text())
        # Tab gone now: the row is finished and the next sweep neither records nor closes anything.
        self.workspaces = self.workspaces[1:]
        self.closed.clear()
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual((result["closed"], result["skipped"], self.closed), ([], [], []))
        self.assertEqual(self.ctx.read_text().count("`rt-wt`"), 1)

    def test_settled_legacy_row_whose_branch_never_landed_keeps_its_tab(self) -> None:
        (self.path / "a.md").write_text("branch commit\n")
        self._git("commit", "-q", "-am", "work", cwd=self.path)
        write_ledger(self.legacy_row(worktree_preserved=str(self.path), worktree_holds="commits"))
        self.workspaces = [{"ref": "workspace:46", "id": "w-46", "title": self.LEGACY}]
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["skipped"], [{"task_id": "rt-wt", "reason": f"branch {self.branch} has not landed on main"}])
        self.assertEqual(self.closed, [])
        self.assertTrue(self.path.is_dir())
        self.assertNotIn("Worker close-outs", self.ctx.read_text())


class IdleOwnerCloseTests(HermeticRepo):
    """An owner tab idle for idle_owner_close_hours
    with no live worker gets the guarded wrap-up-and-close. Wrong impls: close a
    fixed surface or a pinned tab; close while a worker of that workstream is
    live; treat a tab with no stamps as idle forever; miss the boundary by an
    hour because only one of the two stamps is read."""

    def setUp(self) -> None:
        super().setUp()
        for slug, name, emoji in (("meal-planner", "Meal Planner", "⚖️"), ("switchboard-maintenance", "Switchboard Maintenance", "🛠️")):
            (self.repo / "workstreams" / slug).mkdir(parents=True)
            (self.repo / "workstreams" / slug / "config.yaml").write_text(f"name: {name}\nemoji: {emoji}\n")
        self.at = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        self.old = iso(self.at - timedelta(hours=4, minutes=1))
        self.fresh = iso(self.at - timedelta(hours=3, minutes=59))

    def owner(self, title: str, ref: str, **extra) -> dict:
        return {"ref": ref, "id": f"w-{ref.split(':')[1]}", "title": title, "latest_submitted_at": self.old, "pinned": False, **extra}

    def test_boundary_and_exclusions(self) -> None:
        tabs = [self.owner("⚖️ Meal Planner", "workspace:1"),
                self.owner("🛠️ Switchboard Maintenance", "workspace:2", latest_submitted_at=self.fresh),
                self.owner("🧭 CoS", "workspace:3"), self.owner("📨 Comms Triage", "workspace:4"), self.owner("🚀 Launchpad", "workspace:5"),
                self.owner("⚖️ Meal Planner", "workspace:6", pinned=True),
                self.owner("⚖️ Meal Planner", "workspace:7", color="#BB9AF7"),
                self.owner("⚖️ Meal Planner 1", "workspace:8", description="switchboard worker rt-x"),   # a worker tab
                self.owner("⚖️ P0 · Meal Planner", "workspace:9"),
                {"ref": "workspace:10", "id": "w-10", "title": "⚖️ Meal Planner"}]                  # no stamps at all
        got = dispatch.idle_owner_candidates(tabs, {"tasks": []}, {}, 4, self.at)
        self.assertEqual([(c["ref"], c["workstream"], c["idle_hours"]) for c in got], [("workspace:1", "meal-planner", 4.0)])

    def test_busy_workstream_keeps_its_owner_tab_open(self) -> None:
        tabs = [self.owner("⚖️ Meal Planner", "workspace:1"), self.owner("🛠️ Switchboard Maintenance", "workspace:2")]
        payload = {"tasks": [{"id": "rt-live", "workstream": "meal-planner", "status": "active"},
                             {"id": "rt-old", "workstream": "switchboard-maintenance", "status": "done"}]}
        got = dispatch.idle_owner_candidates(tabs, payload, {}, 4, self.at)
        self.assertEqual([c["ref"] for c in got], ["workspace:2"])
        agents = {"T-m": {"state": "Running", "state_ts": (self.at - timedelta(hours=5)).timestamp(), "workspace_id": "w-2"}}
        self.assertEqual(dispatch.idle_owner_candidates(tabs, payload, agents, 4, self.at), [])
        agents = {"T-m": {"state": "Idle", "state_ts": (self.at - timedelta(hours=1)).timestamp(), "workspace_id": "w-2"}}
        self.assertEqual(dispatch.idle_owner_candidates(tabs, payload, agents, 4, self.at), [])
        # An agent stamp alone (tab restored without latest_submitted_at) is enough once it is old.
        tabs[1].pop("latest_submitted_at")
        agents = {"T-m": {"state": "Idle", "state_ts": (self.at - timedelta(hours=4, minutes=1)).timestamp(), "workspace_id": "w-2"}}
        self.assertEqual([c["ref"] for c in dispatch.idle_owner_candidates(tabs, payload, agents, 4, self.at)], ["workspace:2"])

    def test_sweep_paces_one_attempt_per_hour_and_honours_zero(self) -> None:
        self.workspaces = [self.owner("⚖️ Meal Planner", "workspace:1", latest_submitted_at=iso(datetime.now(timezone.utc) - timedelta(hours=5)))]
        spawned: list[list[str]] = []
        with patch.object(dispatch.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or Mock()), \
                patch.object(dispatch, "IDLE_OWNER_STAMPS", self.repo / "stamps.json"):
            self.assertEqual(dispatch.close_idle_owners({**dispatch.config(), "idle_owner_close_hours": 0}), [])
            started = dispatch.close_idle_owners(dispatch.config())
            self.assertEqual([c["ref"] for c in started], ["workspace:1"])
            self.assertEqual(spawned[0][-4:], ["--ref", "workspace:1", "--title", "⚖️ Meal Planner"])
            self.assertEqual(dispatch.close_idle_owners(dispatch.config()), [])   # within the retry hour
        self.assertEqual(len(spawned), 1)
        self.assertEqual(dispatch.DEFAULTS["idle_owner_close_hours"], 4)


class StaleRunningStampTests(HermeticRepo):
    """An owner tab can carry a stale cmux "Running" badge while `amp` reports its
    thread idle (the Idle write at turn end is fire-and-forget and can be lost),
    so idle_owner_candidates would skip it and close-idle-owner would give up
    with "wrap-up turn still running after the wait". A busy stamp older than
    IDLE_STAMP_TRUST is unknown, not busy: the live Amp thread decides. Wrong
    impls: trust the badge forever; ignore the badge and close a tab whose
    thread is mid-turn; call out to Amp for a fresh Running stamp; count the
    idle span from the stale stamp instead of the thread's last turn; treat a
    user-last or streaming transcript as idle."""

    # A cmux session store, reduced to the fields live_agents() reads.
    STORE = {"windows": [{"tabManager": {"workspaces": [
        {"workspaceId": "DC6C6117-1", "customTitle": "🌍 AI Pm Second Brain Results",
         "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-01a13b42"}}}],
         "statusEntries": [{"key": "amp", "value": "Running", "timestamp": 1791995801.4, "icon": "brain"}]},   # 16:36:41Z
        {"workspaceId": "41080358-1", "customTitle": "🤖 Signup Flow",
         "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-01a13b4c"}}}],
         "statusEntries": [{"key": "amp", "value": "Running", "timestamp": 1792026420.0}]},                    # 01:07:00Z
        {"workspaceId": "A1BECBED-1", "customTitle": "✅ Todo",
         "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-todo"}}}],
         "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1791997440.0}]},
    ]}}]}

    def setUp(self) -> None:
        super().setUp()
        for slug, name, emoji in (("ai-pm-second-brain-results", "AI Pm Second Brain Results", "🌍"),
                                  ("signup-flow", "Signup Flow", "🤖")):
            (self.repo / "workstreams" / slug).mkdir(parents=True)
            (self.repo / "workstreams" / slug / "config.yaml").write_text(f"name: {name}\nemoji: {emoji}\n")
        store = self.repo / "session.json"
        store.write_text(json.dumps(self.STORE))
        self.patches.append(patch.object(ledger, "CMUX_SESSION_STORE", store)); self.patches[-1].start()
        self.patches.append(patch.object(ledger, "live_agents", REAL_LIVE_AGENTS)); self.patches[-1].start()
        self.at = datetime(2026, 10, 15, 5, 51, tzinfo=timezone.utc)
        self.tabs = [{"ref": "workspace:7", "id": "DC6C6117-1", "title": "🌍 AI Pm Second Brain Results",
                      "latest_submitted_at": "2026-10-14T16:34:07Z", "pinned": False},
                     {"ref": "workspace:17", "id": "41080358-1", "title": "🤖 Signup Flow",
                      "latest_submitted_at": "2026-10-15T01:06:57Z", "pinned": False}]
        self.asked: list[str] = []

    def live(self, answers: dict[str, datetime | None]):
        def live_idle_since(thread_id: str) -> datetime | None:
            self.asked.append(thread_id)
            return answers[thread_id]
        return live_idle_since

    def test_store_fixture_binds_thread_ids_and_stale_running_stamps(self) -> None:
        agents = ledger.live_agents()
        self.assertEqual(agents["T-01a13b42"]["thread_id"], "T-01a13b42")
        self.assertEqual((agents["T-01a13b42"]["state"], agents["T-01a13b42"]["workspace_id"]), ("Running", "DC6C6117-1"))
        self.assertEqual(agents["T-todo"]["state"], "Idle")
        # Before the fix: every candidate skipped because the badge says Running.
        self.assertEqual(dispatch.idle_owner_candidates(self.tabs, {"tasks": []}, agents, 4, self.at), [])

    def test_stale_running_badge_defers_to_the_live_thread(self) -> None:
        agents = ledger.live_agents()
        idle_16_36 = datetime(2026, 10, 14, 16, 36, 47, tzinfo=timezone.utc)
        got = dispatch.idle_owner_candidates(self.tabs, {"tasks": []}, agents, 4, self.at,
                                             live_idle_since=self.live({"T-01a13b42": idle_16_36, "T-01a13b4c": None}))
        self.assertEqual([(c["ref"], c["idle_hours"]) for c in got], [("workspace:7", 13.2)])   # from the thread's last turn, not now
        self.assertEqual(sorted(self.asked), ["T-01a13b42", "T-01a13b4c"])
        # The thread that really is mid-turn keeps its tab whatever its age.
        self.assertNotIn("workspace:17", [c["ref"] for c in got])

    def test_fresh_running_badge_is_trusted_without_asking_amp(self) -> None:
        agents = ledger.live_agents()
        agents["T-01a13b4c"]["state_ts"] = (self.at - timedelta(hours=1, minutes=59)).timestamp()
        got = dispatch.idle_owner_candidates(self.tabs, {"tasks": []}, agents, 4, self.at,
                                             live_idle_since=self.live({"T-01a13b42": None, "T-01a13b4c": self.at - timedelta(hours=5)}))
        self.assertEqual(got, [])
        self.assertEqual(self.asked, ["T-01a13b42"], "a badge inside IDLE_STAMP_TRUST is believed; no Amp call")
        # A prompt newer than the stale stamp renews the trust window the same way.
        self.asked.clear()
        agents["T-01a13b4c"]["state_ts"] = (self.at - timedelta(hours=5)).timestamp()
        self.tabs[1]["latest_submitted_at"] = iso(self.at - timedelta(minutes=30))
        dispatch.idle_owner_candidates(self.tabs, {"tasks": []}, agents, 4, self.at,
                                       live_idle_since=self.live({"T-01a13b42": None, "T-01a13b4c": self.at - timedelta(hours=5)}))
        self.assertEqual(self.asked, ["T-01a13b42"])

    def test_thread_idle_less_than_the_close_window_waits(self) -> None:
        agents = ledger.live_agents()
        got = dispatch.idle_owner_candidates(self.tabs, {"tasks": []}, agents, 4, self.at,
                                             live_idle_since=self.live({"T-01a13b42": self.at - timedelta(hours=3, minutes=59),
                                                                        "T-01a13b4c": None}))
        self.assertEqual(got, [])

    def test_export_shape_decides_idle_only_on_a_finished_assistant_turn(self) -> None:
        ended = {"role": "assistant", "state": {"type": "complete", "stopReason": "end_turn"}, "createdAt": "2026-10-14T16:36:47.606Z"}
        self.assertEqual(dispatch.thread_idle_since_export({"messages": [ended]}),
                         datetime(2026, 10, 14, 16, 36, 47, 606000, tzinfo=timezone.utc))
        self.assertIsNone(dispatch.thread_idle_since_export({"messages": [ended, {"role": "user", "createdAt": "2026-10-15T05:57:25Z"}]}),
                          "a user message last is a prompt or tool result the agent has not answered")
        self.assertIsNone(dispatch.thread_idle_since_export({"messages": [{"role": "assistant", "state": {"type": "streaming"}}]}))
        # The assistant "completed" by calling shell_command; the tool ran for 60 s before its result message existed.
        tool_pending = {"role": "assistant", "state": {"type": "complete", "stopReason": "tool_use"}, "createdAt": "2026-10-15T06:18:54.045Z",
                        "content": [{"type": "tool_use", "name": "shell_command"}]}
        self.assertIsNone(dispatch.thread_idle_since_export({"messages": [ended, tool_pending]}),
                          "a turn that stopped for tool use is mid-turn until the tool result is answered")
        self.assertIsNone(dispatch.thread_idle_since_export({"messages": []}))
        self.assertIsNone(dispatch.thread_idle_since_export({}))
        usage_only = {"role": "assistant", "state": {"type": "cancelled"}, "usage": {"timestamp": "2026-10-15T01:07:00Z"}}
        self.assertEqual(dispatch.thread_idle_since_export({"messages": [usage_only]}), datetime(2026, 10, 15, 1, 7, tzinfo=timezone.utc))

    def test_close_idle_owner_trusts_a_thread_that_finished_the_wrap_up_over_the_badge(self) -> None:
        # The wrap-up turn ends, the badge stays Running, and the worker must not say "still running after the wait".
        self.workspaces = [self.tabs[0]]
        clock = {"now": self.at}
        answers = {"T-01a13b42": self.at - timedelta(hours=13)}   # idle before the wrap-up: the wrap-up turn has not ended
        with patch.object(dispatch, "tab_idle", lambda ref: True), patch.object(dispatch, "tab_send", lambda ref, text: True), \
                patch.object(dispatch, "now", lambda: clock["now"]), patch.object(dispatch, "thread_idle_since", lambda tid: answers[tid]), \
                patch.object(dispatch.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s))):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = dispatch.command_close_idle_owner(argparse.Namespace(ref="workspace:7", title="🌍 AI Pm Second Brain Results"))
            self.assertEqual((rc, self.closed), (0, []))
            self.assertIn("wrap-up turn still running after the wait; left open", out.getvalue())
            # Now the thread's last turn ends after the wrap-up was sent: close.
            clock["now"] = self.at
            answers["T-01a13b42"] = self.at + timedelta(seconds=40)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = dispatch.command_close_idle_owner(argparse.Namespace(ref="workspace:7", title="🌍 AI Pm Second Brain Results"))
            self.assertEqual((rc, self.closed), (0, ["workspace:7"]))
            self.assertIn("trusting the thread", out.getvalue())
            self.assertIn("closed after wrap-up", out.getvalue())

    def test_close_idle_owner_will_not_prompt_a_thread_that_is_mid_turn(self) -> None:
        self.workspaces = [self.tabs[0]]
        sent: list[str] = []
        with patch.object(dispatch, "tab_idle", lambda ref: True), patch.object(dispatch, "tab_send", lambda ref, text: sent.append(text) or True), \
                patch.object(dispatch, "thread_idle_since", lambda tid: None), redirect_stdout(io.StringIO()) as out:
            rc = dispatch.command_close_idle_owner(argparse.Namespace(ref="workspace:7", title="🌍 AI Pm Second Brain Results"))
        self.assertEqual((rc, sent, self.closed), (0, [], []))
        self.assertIn("mid-turn or unreadable; left open", out.getvalue())


REAL_CMUX_CLOSE_RESULT = dispatch.cmux_close_result   # captured before any fixture patches it


class CmuxCloseTests(HermeticRepo):
    """cmux 0.65 refuses `close-workspace` on a workspace with a running process
    unless --force is passed, and every switchboard tab runs an agent CLI. These drive the real cmux_close_result against a fake cmux that emulates each
    version. Wrong impls: omit --force (0.65 refuses); send --force to an older
    cmux with no fallback (unknown option); swallow cmux's stderr in the reap
    skip reason; skip the unpin or the skip-record sentinel on the way."""

    FAKE = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_CMUX_LOG"
case "$1" in
  rpc) echo '{"workspaces":[]}'; exit 0 ;;
  workspace-action) echo "OK action=unpin"; exit 0 ;;
  close-workspace)
    force=0; for a in "$@"; do [ "$a" = --force ] && force=1; done
    case "$FAKE_CMUX_MODE" in
      v065)   [ $force = 1 ] || { echo "Error: confirmation_required: Workspace has a running process; retry with --force" >&2; exit 1; } ;;
      legacy) [ $force = 0 ] || { echo "Error: close-workspace: unknown option --force" >&2; exit 1; } ;;
      gone)   echo "Error: Workspace ref not found: $3" >&2; exit 1 ;;
    esac
    echo "OK $3"; exit 0 ;;
esac
exit 0
'''

    def setUp(self) -> None:
        super().setUp()
        self.fake = self.repo / "fake-cmux"
        self.fake.write_bytes(self.FAKE.encode())   # LF endings on Windows too
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.repo / "fake-cmux.log"
        self.sentinel = self.repo / "cmux-skip-record"
        os.environ["FAKE_CMUX_LOG"] = str(self.log)
        for p in (patch.object(dispatch, "CMUX", str(self.fake)),
                  patch.object(dispatch, "CMUX_SKIP_RECORD", self.sentinel),
                  patch.object(dispatch, "cmux_close_result", REAL_CMUX_CLOSE_RESULT)):   # undo the fixture stub: run the real argv
            p.start(); self.patches.append(p)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_cmux_065_needs_force_and_gets_it(self) -> None:
        os.environ["FAKE_CMUX_MODE"] = "v065"
        self.assertEqual(dispatch.cmux_close_result("workspace:42", "W-42"), (True, ""))
        self.assertEqual(self.calls(), ["workspace-action --action unpin --workspace workspace:42",
                                        "close-workspace --workspace workspace:42 --force"])
        self.assertEqual(self.sentinel.read_text(), "W-42\n")   # the closed-workspaces daemon must not record this as the owner's close
        self.assertTrue(dispatch.cmux_close("workspace:42", "W-42"))

    def test_older_cmux_rejecting_force_gets_one_flagless_retry(self) -> None:
        os.environ["FAKE_CMUX_MODE"] = "legacy"
        self.assertEqual(dispatch.cmux_close_result("workspace:7", ""), (True, ""))
        self.assertEqual([c for c in self.calls() if c.startswith("close-workspace")],
                         ["close-workspace --workspace workspace:7 --force", "close-workspace --workspace workspace:7"])
        self.assertFalse(self.sentinel.exists(), "no workspace id, no sentinel line")

    def test_real_failure_surfaces_cmux_stderr(self) -> None:
        os.environ["FAKE_CMUX_MODE"] = "gone"
        ok, error = dispatch.cmux_close_result("workspace:9", "W-9")
        self.assertFalse(ok)
        self.assertEqual(error, "Error: Workspace ref not found: workspace:9")
        self.assertEqual(len([c for c in self.calls() if c.startswith("close-workspace")]), 1, "a not-found is not retried")

    def test_reap_closes_a_finished_worker_tab_on_cmux_065_and_names_the_error_otherwise(self) -> None:
        # The sweep path end to end: a done, delivered, landed row whose tab is
        # a worker tab. Wrong impl: the row sits in skipped as "cmux close
        # failed" on every tick.
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "🛠️ Switchboard Maintenance 1", "description": "switchboard worker rt-wt"}]
        self.agents["T-dead"] = {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-40", "title": "worker"}
        write_ledger(self.worker_row(status="done", report_delivered=True,
                                     finished_at=iso(datetime.now(timezone.utc) - timedelta(minutes=30)), summary="done"))
        os.environ["FAKE_CMUX_MODE"] = "gone"
        with patch.object(dispatch, "record_on_main", lambda task, **kw: {"recorded": True}):
            result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"], [])
        self.assertEqual([s["reason"] for s in result["skipped"]],
                         ["cmux close failed: Error: Workspace ref not found: workspace:40"])
        os.environ["FAKE_CMUX_MODE"] = "v065"
        with patch.object(dispatch, "record_on_main", lambda task, **kw: {"recorded": True}):
            result = dispatch.reap_all(dispatch.config())
        self.assertEqual([(c["task_id"], c["workspace_ref"], c["was_open"]) for c in result["closed"]], [("rt-wt", "workspace:40", True)])
        self.assertIn("close-workspace --workspace workspace:40 --force", self.calls())
        self.assertEqual(self.sentinel.read_text().splitlines()[-1], "w-40")

    def test_idle_owner_close_names_the_error_in_its_log(self) -> None:
        os.environ["FAKE_CMUX_MODE"] = "gone"
        ws = {"ref": "workspace:3", "id": "w-3", "title": "⚖️ Meal Planner", "latest_submitted_at": "2026-10-15T00:00:00Z"}
        self.workspaces = [ws]
        out = io.StringIO()
        with patch.object(dispatch, "tab_idle", lambda ref: True), patch.object(dispatch, "tab_send", lambda ref, text: True), \
                patch.object(dispatch.time, "sleep", lambda s: None), redirect_stdout(out):
            # no agent on w-3 and the tab reads idle, so the wait loop breaks on its first pass and the close runs
            rc = dispatch.command_close_idle_owner(argparse.Namespace(ref="workspace:3", title="⚖️ Meal Planner"))
        self.assertEqual(rc, 1)
        self.assertIn("cmux close failed; left open: Error: Workspace ref not found: workspace:3", out.getvalue())


class AbandonedRowTests(HermeticRepo):
    """An active row whose worker thread is gone from cmux and whose stamps are
    stale must end in the ledger without a CoS nudge. Wrong impls:
    relaunch a checkout that holds work; relaunch without bound; judge liveness
    when the session store is empty; touch rows whose thread is live or fresh."""

    def test_clean_abandoned_checkout_is_relaunched_once_as_a_new_attempt(self) -> None:
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "switchboard-maintenance · worker-aaaaaaaaaaaa"}]
        write_ledger(self.worker_row(resume_sent_at=self.stale, landed_commit="deadbeef"))
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result, {"relaunched": [{"task_id": "rt-wt", "replaced_attempt": "rt-wt.a1.aaa"}], "blocked": []})
        row = self.row()
        self.assertEqual((row["status"], row["attempt"]), ("queued", 2))
        self.assertTrue(row["attempt_id"].startswith("rt-wt.a2."))
        self.assertEqual(row["sweep_relaunches"], 1)
        self.assertIn("re-queued by sweep", row["summary"])
        for key in ("worker_id", "worktree", "branch", "thread_id", "workspace_ref", "heartbeat_at", "resume_sent_at", "landed_commit"):
            self.assertNotIn(key, row, key)  # nothing of the dead attempt leaks into the next (claim_launch needs no worker_id)
        self.assertEqual(row["prompt"], "audit it")
        self.assertEqual(self.closed, ["workspace:40"])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.notified, [])
        # The second death is not relaunched: the origin hears a blocker once.
        write_ledger(self.worker_row(sweep_relaunches=1, attempt=2, attempt_id="rt-wt.a2.bbb", worktree=str(self.repo / "gone")))
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [])
        self.assertEqual(result["blocked"][0]["task_id"], "rt-wt")
        self.assertIn("already relaunched 1x", result["blocked"][0]["reason"])
        self.assertEqual([(t, s) for t, s, _ in self.notified], [("rt-wt", "blocked")])

    def test_relaunch_parks_a_checkout_that_turns_dirty_between_judgement_and_drop(self) -> None:
        # Fix B3-lite: the sweep judged the checkout clean, then a late write landed
        # before drop_worktree ran. Wrong impl: `worktree remove --force` deletes it.
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "switchboard-maintenance · worker-aaaaaaaaaaaa"}]
        write_ledger(self.worker_row())
        real_unlanded = dispatch.unlanded_work

        def judge_then_write(task):
            held = real_unlanded(task)
            (self.path / "late.md").write_text("written after the judgement\n")
            return held

        with patch.object(dispatch, "unlanded_work", judge_then_write):
            result = dispatch.fail_abandoned_rows(dispatch.config())
        entry = result["relaunched"][0]
        parked = Path(entry["worktree_parked"])
        self.assertEqual(parked.parent.name, "_abandoned")
        self.assertEqual((parked / "late.md").read_text(), "written after the judgement\n")
        self.assertFalse(self.path.exists())
        self.assertIn(f"parked at {parked}", self.row()["summary"])
        self.assertEqual(self.row()["status"], "queued")

    def test_abandoned_checkout_with_work_is_blocked_with_the_recovery_and_kept(self) -> None:
        # No proof of death (the tab list is unreadable here): never a second worker on this checkout.
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.worker_row())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [])
        reason = result["blocked"][0]["reason"]
        self.assertIn("holds uncommitted edits", reason)
        self.assertIn(f"dispatch.py finish --task-id rt-wt", reason)
        self.assertEqual(self.notified[0][:2], ("rt-wt", "blocked"))
        self.assertTrue(self.path.is_dir())
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")

    def test_pre_worktree_ghost_is_blocked_only_after_the_long_window(self) -> None:
        recent = iso(datetime.now(timezone.utc) - dispatch.ABANDONED_AFTER + timedelta(minutes=5))
        ghost = {"id": "rt-ghost", "status": "active", "origin": "cos", "attempt_id": "rt-ghost.a1.x", "thread_id": "T-dead",
                 "workspace_ref": "workspace:293", "heartbeat_at": recent, "updated_at": recent, "created_at": recent}
        write_ledger(ghost)
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config())["blocked"], [])
        old = iso(datetime.now(timezone.utc) - dispatch.ABANDONED_AFTER - timedelta(minutes=5))
        write_ledger({**ghost, "heartbeat_at": old, "updated_at": old})
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [])
        self.assertIn("ran without a worktree", result["blocked"][0]["reason"])
        self.assertIn("router-dispatch.sh --task-id rt-ghost", result["blocked"][0]["reason"])
        self.assertEqual([(t, s) for t, s, _ in self.notified], [("rt-ghost", "blocked")])

    def test_live_fresh_claimed_or_workerless_rows_are_left_alone(self) -> None:
        self.agents["T-live"] = {"state": "Running", "state_ts": 1.0, "workspace_id": "w-41", "title": "worker"}
        fresh = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        write_ledger(self.worker_row(id="rt-live", thread_id="T-live"),
                     self.worker_row(id="rt-fresh", heartbeat_at=fresh),
                     self.worker_row(id="rt-claimed", launching_at=fresh),
                     {"id": "rt-orphan", "status": "routing", "attempt_id": "x", "created_at": self.stale, "updated_at": self.stale})
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})
        self.assertEqual({t["id"]: t["status"] for t in ledger.load_payload()["tasks"]},
                         {"rt-live": "active", "rt-fresh": "active", "rt-claimed": "active", "rt-orphan": "routing"})

    def test_empty_session_store_is_an_outage_not_a_mass_death(self) -> None:
        self.agents = {}
        write_ledger(self.worker_row())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertIn("skipped", result)
        self.assertIn("store outage", result["skipped"])
        self.assertEqual(self.row()["status"], "active")
        self.assertTrue(self.path.is_dir())

    def test_empty_store_with_no_live_row_says_nothing_to_judge(self) -> None:
        # On wmux, a sweep with no worker running must not log "cmux lists no agents",
        # which reads as "the checks never run on this machine".
        self.agents = {}
        write_ledger({**self.worker_row(), "status": "done"})
        self.assertIn("nothing to judge", dispatch.agentless_skip("liveness"))
        self.assertIn("read outage", dispatch.agentless_skip("limits", listing_empty=True))
        write_ledger(self.worker_row())
        self.assertIn("1 row(s) are live (store outage)", dispatch.agentless_skip("liveness"))


class OrphanWorktreePruneTests(HermeticRepo):
    def test_old_rowless_checkouts_are_removed_when_clean_and_parked_when_dirty(self) -> None:
        # Fix B3-lite. Wrong impls: delete the dirty one (the old `worktree remove
        # --force`), or leave it in place forever without saying so.
        write_ledger(self.worker_row())  # rt-wt has a row: never pruned, whatever its age
        clean, _ = dispatch.create_worktree("rt-clean")
        dirty, _ = dispatch.create_worktree("rt-dirty")
        (dirty / "a.md").write_text("work\n")
        (dirty / "notes.md").write_text("untracked too\n")
        committed, _ = dispatch.create_worktree("rt-committed")
        (committed / "a.md").write_text("committed work\n")
        self._git("commit", "-q", "-am", "unlanded", cwd=committed)
        young, _ = dispatch.create_worktree("rt-young")
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        for path in (self.path, clean, dirty, committed):
            os.utime(path, (old, old))
        result = dispatch.prune_orphan_worktrees()
        self.assertEqual(result["removed"], ["rt-clean"])
        self.assertFalse(clean.exists())
        self.assertEqual([p["task_id"] for p in result["parked"]], ["rt-dirty"])
        parked = Path(result["parked"][0]["parked_to"])
        self.assertEqual(parked.parent, self.repo / ".amp" / "worktrees" / "_abandoned")
        self.assertRegex(parked.name, r"^rt-dirty-\d{8}T\d{6}Z$")
        self.assertFalse(dirty.exists())
        self.assertEqual((parked / "a.md").read_text(), "work\n")
        self.assertEqual((parked / "notes.md").read_text(), "untracked too\n")
        listed = [os.path.realpath(l.split()[0]) for l in self._git("worktree", "list").splitlines()]
        self.assertIn(os.path.realpath(parked), listed)                        # still a linked worktree
        self.assertEqual(self._git("symbolic-ref", "--short", "HEAD", cwd=parked).strip(), "task/rt-dirty")  # branch kept
        self.assertTrue(committed.is_dir() and young.is_dir() and self.path.is_dir())
        # A second pass does not touch the parked checkout (its name is not rt-*).
        self.assertEqual(dispatch.prune_orphan_worktrees(), {"removed": [], "parked": []})
        self.assertTrue(parked.is_dir())


class WorktreeGuardTests(HermeticRepo):
    """Fix B1-lite: a launcher never opens a tab on a checkout that is not a
    linked worktree on the task branch, and a worker row without a branch can
    never read done."""

    def test_launch_fails_the_row_and_opens_no_tab_when_the_worktree_does_not_verify(self) -> None:
        write_ledger({"id": "rt-bad", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                      "attempt": 1, "attempt_id": "rt-bad.a1.aaa", "created_at": iso(datetime.now(timezone.utc))})
        calls: list[list[str]] = []
        real_run, real_create = dispatch.run, dispatch.create_worktree

        def spying_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX:
                calls.append(cmd)
            return real_run(cmd, **kw)

        def wrong_branch(task_id: str):
            path, branch = real_create(task_id)
            dispatch.run(["git", "-C", str(path), "checkout", "-q", "--detach"])
            return path, branch

        with patch.object(dispatch, "create_worktree", wrong_branch), patch.object(dispatch, "run", spying_run), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        row = self.row("rt-bad")
        self.assertEqual(row["status"], "failed")
        self.assertIn("worktree_unverified", row["summary"])
        self.assertIn("detached HEAD", row["summary"])
        self.assertEqual([c for c in calls if "new-workspace" in c], [])
        self.assertEqual(self.notified[-1][:2], ("rt-bad", "failed"))

    CMUX_TIMEOUT = ("cmux: 'new-workspace' is now an alias for 'cmux workspace create'.\n"
                    "Error: timeout: cmux did not respond within 10 seconds after the command started, so its result is unknown.")

    def launch_with_cmux(self, create, task_id: str = "rt-slow") -> dict:
        """Run release_queued for one queued row with `create(cmd, **kw)` standing
        in for the cmux new-workspace call (every other `run` is real)."""
        write_ledger({"id": task_id, "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                      "attempt": 1, "attempt_id": f"{task_id}.a1.aaa", "created_at": iso(datetime.now(timezone.utc))})
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                return create(cmd, **kw)
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch.time, "sleep", lambda s: None), redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        return self.row(task_id)

    def test_launch_adopts_the_tab_cmux_opened_before_timing_out(self) -> None:
        # new-workspace returns cmux's 10 s timeout error although the tab opened.
        # Wrong impl: fail the row and leave workspace:4073 open with this task's description.
        def create(cmd, **kw):
            self.workspaces.append({"ref": "workspace:4073", "id": "w-73", "title": cmd[cmd.index("--name") + 1],
                                    "description": cmd[cmd.index("--description") + 1]})
            return subprocess.CompletedProcess(cmd, 1, "", self.CMUX_TIMEOUT)
        row = self.launch_with_cmux(create)
        self.assertEqual((row["status"], row["workspace_ref"], row["summary"]), ("routing", "workspace:4073", "started"))
        self.assertTrue(Path(row["worktree"]).is_dir())
        self.assertEqual(self.workspaces[-1]["description"], "switchboard worker rt-slow")

    def test_launch_adopts_the_tab_after_a_hung_cmux_cli(self) -> None:
        def create(cmd, **kw):
            self.workspaces.append({"ref": "workspace:4074", "id": "w-74", "title": "x", "description": "switchboard worker rt-slow"})
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))
        row = self.launch_with_cmux(create)
        self.assertEqual((row["status"], row["workspace_ref"]), ("routing", "workspace:4074"))

    def test_launch_without_a_tab_after_two_timeouts_fails_the_row_and_drops_the_worktree(self) -> None:
        self.workspaces.append({"ref": "workspace:4075", "id": "w-75", "title": "x", "description": "switchboard worker rt-other"})
        calls: list[list[str]] = []
        row = self.launch_with_cmux(lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", self.CMUX_TIMEOUT))
        self.assertEqual(len(calls), 2, "a timeout with no tab anywhere is retried exactly once")
        self.assertEqual(row["status"], "failed")
        self.assertIn("cmux new-workspace failed: cmux: 'new-workspace' is now an alias", row["summary"])
        self.assertFalse(dispatch.worktree_path("rt-slow").exists(), "no tab, so the checkout is dropped as before")
        self.assertEqual(self.notified[-1][:2], ("rt-slow", "failed"))
        self.assertEqual(self.closed, [], "another task's tab is never touched")

    def test_launch_retries_once_after_a_timeout_with_no_tab_and_the_retry_succeeds(self) -> None:
        # The RPC never reached cmux. Wrong impl: fail the row on the first timeout.
        calls: list[list[str]] = []

        def create(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))
            self.workspaces.append({"ref": "workspace:4081", "id": "w-81", "title": "x", "description": "switchboard worker rt-slow"})
            return subprocess.CompletedProcess(cmd, 0, "workspace:4081\n", "")
        with patch.object(dispatch, "CMUX_TIMEOUT_LOG", self.repo / "cmux-timeouts.jsonl"):
            row = self.launch_with_cmux(create)
            noted = dispatch.cmux_timeouts_since(None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1], "the retry is the same tab: same title, description, cwd and command")
        self.assertEqual((row["status"], row["workspace_ref"], row["summary"]), ("routing", "workspace:4081", "started"))
        self.assertEqual(self.closed, [])
        self.assertEqual([(n["task_id"], n["call"]) for n in noted], [("rt-slow", "new-workspace")],
                         "the timeout is noted for the sweep's capacity line")

    def test_launch_closes_the_half_made_duplicate_when_the_first_tab_appears_after_the_retry(self) -> None:
        calls: list[list[str]] = []

        def create(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, 60)
            # The retry's tab answers; the first call's tab lands late, right after.
            self.workspaces.append({"ref": "workspace:4082", "id": "w-82", "title": "x", "description": "switchboard worker rt-slow"})
            self.workspaces.append({"ref": "workspace:4083", "id": "w-83", "title": "x", "description": "switchboard worker rt-slow"})
            return subprocess.CompletedProcess(cmd, 0, "workspace:4082\n", "")
        row = self.launch_with_cmux(create)
        self.assertEqual((row["status"], row["workspace_ref"]), ("routing", "workspace:4082"))
        self.assertEqual(self.closed, ["workspace:4083"], "only the duplicate closes; the bound tab stays")

    def test_launch_hard_failure_is_not_retried(self) -> None:
        # Wrong implementation: retrying every failure would open a second tab on
        # a real error (bad --cwd, unknown option) and double the damage.
        calls: list[list[str]] = []
        row = self.launch_with_cmux(lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 2, "", "Error: unknown option --focus"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(row["status"], "failed")
        self.assertIn("unknown option", row["summary"])

    def test_workspace_list_timeout_is_retried_with_backoff_then_empty(self) -> None:
        calls: list[int] = []
        slept: list[int] = []

        def slow(cmd, **kw):
            calls.append(kw["timeout"])
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return subprocess.CompletedProcess(cmd, 0, '{"workspaces":[{"ref":"workspace:1"}]}', "")
        real = REAL_CMUX_WORKSPACES
        with patch.object(dispatch, "run", slow), patch.object(dispatch.time, "sleep", slept.append), \
                patch.object(dispatch, "CMUX_TIMEOUT_LOG", self.repo / "cmux-timeouts.jsonl"):
            self.assertEqual(real("rt-list"), [{"ref": "workspace:1"}])
            self.assertEqual([(n["task_id"], n["call"]) for n in dispatch.cmux_timeouts_since(None)], [("rt-list", "workspace.list")],
                             "the timeout is attributed to the launch it stalled, never task_id \"\"")
        self.assertEqual(calls, [10, 25], "the second try gets a longer budget and stops once cmux answers")
        self.assertEqual(slept, [5], "one pause before the retry, none before the first try")
        calls.clear()
        slept.clear()
        never = lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, kw["timeout"]))
        with patch.object(dispatch, "run", never), patch.object(dispatch.time, "sleep", slept.append), \
                patch.object(dispatch, "CMUX_TIMEOUT_LOG", self.repo / "cmux-timeouts.jsonl"):
            self.assertEqual(real(), [], "a non-strict caller still gets an empty list after the bound")
            with self.assertRaises(dispatch.CmuxUnreachable) as raised:
                real("rt-list", strict=True)
        self.assertEqual(slept, [5, 15, 5, 15], "the bound is three tries per call; it never loops")
        self.assertIn("workspace.list timed out 3x", str(raised.exception))

    def test_launch_holds_the_row_with_the_reason_when_cmux_never_lists_tabs(self) -> None:
        # Repeated workspace.list timeouts while a launch is in flight.
        # Wrong impl: carry on with an empty tab list (slot 1, dup-close blind) or
        # leave the row where it was with nothing to say why.
        write_ledger({"id": "rt-deaf", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                      "attempt": 1, "attempt_id": "rt-deaf.a1.aaa", "created_at": iso(datetime.now(timezone.utc))})
        calls: list[list[str]] = []
        real_run = dispatch.run

        def deaf_cmux(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX:
                calls.append(cmd)
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 10))
            return real_run(cmd, **kw)

        with patch.object(dispatch, "cmux_workspaces", REAL_CMUX_WORKSPACES), patch.object(dispatch, "run", deaf_cmux), \
                patch.object(dispatch.time, "sleep", lambda s: None), \
                patch.object(dispatch, "CMUX_TIMEOUT_LOG", self.repo / "cmux-timeouts.jsonl"), redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        row = self.row("rt-deaf")
        self.assertEqual(row["status"], "queued", "held and retried, never failed")
        self.assertIn("cmux_unreachable: workspace.list timed out 3x", row["launch_hold_reason"])
        self.assertIn("wmux is not reachable", row["summary"])
        self.assertEqual([c[1:3] for c in calls], [["rpc", "workspace.list"]] * 3, "three list tries, no new-workspace on a deaf cmux")
        self.assertFalse(dispatch.worktree_path("rt-deaf").exists(), "the list comes first, so no checkout was made")
        self.assertEqual(self.notified, [])

    def test_verify_worktree_names_each_miss(self) -> None:
        self.assertEqual(dispatch.verify_worktree(self.path, self.branch), "")
        self.assertIn("does not exist", dispatch.verify_worktree(self.path / "nope", self.branch))
        self.assertIn("not a worktree link file", dispatch.verify_worktree(self.repo, "main"))   # main itself: .git is a dir
        self.assertIn("not task/other", dispatch.verify_worktree(self.path, "task/other"))
        loose = self.repo / ".amp" / "worktrees" / "rt-loose"
        loose.mkdir(parents=True)
        (loose / ".git").write_text("gitdir: /nowhere\n")
        self.assertTrue(dispatch.verify_worktree(loose, "task/rt-loose"))

    def test_finish_on_a_worker_row_without_a_branch_is_a_conflict_not_a_skip(self) -> None:
        # Worker launched, no branch recorded. Wrong impl:
        # `skipped` (owner-tab semantics) lets report-to-cos.sh flip the row to done.
        outcome = dispatch.finish_branch(self.worker_row(branch=None, worktree=None))
        self.assertEqual((outcome["landed"], outcome["conflict"]), (False, True))
        self.assertNotIn("skipped", outcome)
        self.assertEqual(outcome["reason"], "worker row without a branch; nothing to land")
        owner = dispatch.finish_branch({"id": "rt-owner", "status": "active", "prompt": "owner tab"})
        self.assertTrue(owner["skipped"])


class StuckRouteTests(HermeticRepo):
    """Sweep rule: a row in routing/queued past
    `dispatch.stuck_routing_minutes` with no live thread is flagged once and its
    origin told, never re-dispatched. Wrong impls: judge
    from updated_at (every retype refreshes it); flag on every tick; flag a row
    release is holding on purpose; re-dispatch; judge liveness from an empty
    session store."""

    CFG = {**dispatch.DEFAULTS, "stuck_routing_minutes": 10}

    def routing_row(self, minutes_ago: int, **extra) -> dict:
        when = iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        return {"id": "rt-stuck", "status": "routing", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                "attempt": 1, "attempt_id": "rt-stuck.a1.aaa", "worker_id": "worker-aaaaaaaaaaaa", "worktree": str(self.path),
                "branch": self.branch, "workspace_ref": "workspace:540", "created_at": when, "launched_at": when,
                "updated_at": iso(datetime.now(timezone.utc)), **extra}   # updated_at fresh, as a retype leaves it

    def test_routing_row_past_the_threshold_with_no_thread_is_flagged_once_and_the_origin_told(self) -> None:
        self.workspaces = [{"ref": "workspace:540", "id": "w-540", "title": "🛠️ Switchboard Maintenance 1"}]
        write_ledger(self.routing_row(11))
        result = dispatch.flag_stuck_routes(self.CFG)
        self.assertEqual([(s["task_id"], s["status_was"], s["age_minutes"]) for s in result["stuck"]], [("rt-stuck", "routing", 11)])
        row = self.row("rt-stuck")
        self.assertTrue(row.get("stuck_at"))
        self.assertEqual(row["attempt"], 1, "never re-dispatched by the sweep")
        self.assertEqual([(t, s) for t, s, _ in self.notified], [("rt-stuck", "blocked")])
        text = self.notified[0][2]
        self.assertIn("stuck: routing for 11m with no live thread (worker tab workspace:540", text)
        self.assertIn("router-dispatch.sh --task-id rt-stuck --force-redispatch", text)
        self.assertEqual(self.closed, [], "the tab is left for the owner to inspect")
        self.assertTrue(self.path.exists(), "the checkout is left alone")
        # Second tick: nothing more, whatever the age.
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [])
        self.assertEqual(len(self.notified), 1)

    def test_row_under_the_threshold_is_left_alone(self) -> None:
        write_ledger(self.routing_row(9))
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [])
        self.assertNotIn("stuck_at", self.row("rt-stuck"))
        self.assertEqual(self.notified, [])

    def test_age_is_the_latest_dispatch_stamp_not_updated_at(self) -> None:
        # created 3 h ago, re-queued and relaunched 4 min ago: the current attempt is young.
        write_ledger(self.routing_row(180, requeued_at=iso(datetime.now(timezone.utc) - timedelta(minutes=4)),
                                      launched_at=iso(datetime.now(timezone.utc) - timedelta(minutes=4))))
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [])

    def test_live_thread_or_fresh_launch_claim_or_amp_tenant_is_not_stuck(self) -> None:
        self.agents["T-live"] = {"state": "Busy", "state_ts": 1.0, "workspace_id": "w-540", "title": "worker"}
        write_ledger(self.routing_row(30, thread_id="T-live"))
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [], "thread live in cmux")
        write_ledger(self.routing_row(30, launching_at=iso(datetime.now(timezone.utc) - timedelta(minutes=2))))
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [], "a launcher holds a fresh claim")
        self.workspaces = [{"ref": "workspace:540", "id": "w-540", "title": "x"}]
        write_ledger(self.routing_row(30))
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [], "an Amp session runs in the tab; binding, not launch")
        self.assertEqual(self.notified, [])

    def test_queued_row_is_stuck_only_when_nothing_holds_it(self) -> None:
        def queued(**extra):
            return {"id": "rt-q", "status": "queued", "origin": "launchpad", "workstream": "switchboard-maintenance", "prompt": "later",
                    "attempt": 1, "attempt_id": "rt-q.a1.aaa",
                    "created_at": iso(datetime.now(timezone.utc) - timedelta(minutes=45)), **extra}
        write_ledger(queued(depends_on=["rt-dep"]), {"id": "rt-dep", "status": "active", "attempt_id": "rt-dep.a1.x"})
        self.assertEqual(dispatch.flag_stuck_routes(self.CFG)["stuck"], [], "held by a prerequisite")
        active = self.worker_row(id="rt-busy", attempt_id="rt-busy.a1.x", thread_id="T-owner")
        write_ledger(queued(), active)
        self.assertEqual(dispatch.flag_stuck_routes({**self.CFG, "max_workers": 1})["stuck"], [], "held by the worker cap")
        # A dated launch hold: a row queued for hours behind it is waiting on purpose.
        dated = {**self.CFG, "launch_holds": [{"workstreams": ["switchboard-maintenance"], "reason": "dated event",
                                               "until": iso(datetime.now(timezone.utc) + timedelta(hours=3))}]}
        write_ledger(queued())
        self.assertEqual(dispatch.flag_stuck_routes(dated)["stuck"], [], "held by a dated launch hold")
        write_ledger(queued())
        result = dispatch.flag_stuck_routes(self.CFG)
        self.assertEqual([(s["task_id"], s["status_was"]) for s in result["stuck"]], [("rt-q", "queued")])
        self.assertIn("stuck: queued for 45m with no live thread (no worker tab", self.notified[-1][2])

    def test_rule_is_off_at_zero_and_skips_an_empty_session_store(self) -> None:
        write_ledger(self.routing_row(60))
        self.assertEqual(dispatch.flag_stuck_routes({**self.CFG, "stuck_routing_minutes": 0})["stuck"], [])
        self.agents.clear()
        result = dispatch.flag_stuck_routes(self.CFG)
        self.assertEqual(result["stuck"], [])
        self.assertIn("no agents", result["skipped"])
        self.assertEqual(self.notified, [])


class WedgedWorkerTests(HermeticRepo):
    """Sweep rule: an `active` row whose worker shows no
    progress on any signal for `dispatch.wedged_hours` is stamped `wedged`, given
    a recovery_action, and its origin told once; never re-dispatched. A worker hung
    on a tool call inside code_exec stops its heartbeat, keeps the cmux stamp at
    Running and shows a spinner, so nudge_idle (tab_idle False) never fires.
    Wrong impls: judge on
    the heartbeat alone (plugin hooks never see code_exec, so a code_exec-heavy
    worker has a stale heartbeat while it works); flag an Idle worker (that is the
    nudge's case); flag on every tick; flag with the thread unreadable at the
    plain threshold; re-dispatch; judge from an empty session store."""

    CFG = {**dispatch.DEFAULTS, "wedged_hours": 2}

    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now(timezone.utc)
        self.agents["T-dead"] = {"state": "Running", "state_ts": (self.now - timedelta(hours=3)).timestamp(),
                                 "workspace_id": "w-40", "title": "worker"}
        self.thread_at: dict[str, datetime | None] = {"T-dead": self.now - timedelta(hours=3)}
        self.screen: str | None = None   # unreadable by default: the line keeps its old tail
        reader = patch.object(dispatch, "read_tab_screen", lambda ref, lines=50: self.screen)
        reader.start()
        self.addCleanup(reader.stop)

    def test_the_notice_names_what_the_screen_shows(self) -> None:
        """A worker whose tab shows a DNS error at an empty prompt is not "likely stuck
        on a tool call"."""
        self.screen = NETWORK_SCREEN
        write_ledger(self.silent_row())
        self.flag()
        text = self.notified[0][2]
        self.assertIn('the tab shows "API Error: Can\'t reach the API server — check your internet or DNS (ENOTFOUND)"', text)
        self.assertNotIn("likely stuck on a tool call", text)
        self.screen = TOOL_SCREEN
        write_ledger(self.silent_row())
        self.flag()
        self.assertIn('the tab shows "⎿  Running…"', self.notified[1][2], "the last output line above the footer")

    def test_a_row_the_network_step_owns_is_not_flagged_until_its_resumes_run_out(self) -> None:
        stop = {"error_at": "2026-11-10T00:51:34+00:00", "text": "API Error", "code": "ENOTFOUND"}
        write_ledger(self.silent_row(network_stop=stop))
        self.assertEqual(self.flag()["wedged"], [])
        spent = {"attempt_id": "rt-wt.a1.aaa", "count": dispatch.MAX_NETWORK_RESUMES}
        write_ledger(self.silent_row(network_stop=stop, network_resumes=spent))
        self.assertEqual(self.flag()["wedged"], [], "still owned until CoS hears the cap")
        write_ledger(self.silent_row(network_stop=stop, network_resumes={**spent, "capped_told": stop["error_at"]}))
        self.assertEqual([w["task_id"] for w in self.flag()["wedged"]], ["rt-wt"])

    def hours_ago(self, hours: float) -> str:
        return iso(self.now - timedelta(hours=hours))

    def silent_row(self, hours: float = 3, **extra) -> dict:
        base = dict(started_at=self.hours_ago(hours + 1), heartbeat_at=self.hours_ago(hours), updated_at=self.hours_ago(hours))
        return self.worker_row(**{**base, **extra})

    def flag(self, cfg: dict | None = None) -> dict:
        return dispatch.flag_wedged_workers(cfg or self.CFG, last_activity=lambda tid: self.thread_at.get(tid))

    def test_silent_on_every_signal_is_flagged_once_and_the_origin_told(self) -> None:
        write_ledger(self.silent_row())
        result = self.flag()
        self.assertEqual([(w["task_id"], w["thread_id"], w["origin_told"]) for w in result["wedged"]], [("rt-wt", "T-dead", True)])
        row = self.row()
        self.assertTrue(row.get("wedged_at"))
        self.assertEqual(row["wedged"]["reason"], row["recovery_action"], "the review feed shows the sweep's own line")
        self.assertEqual((row["status"], row["attempt"], row["attempt_id"]), ("active", 1, "rt-wt.a1.aaa"),
                         "still active and never re-dispatched by the sweep")
        self.assertEqual([(t, s) for t, s, _ in self.notified], [("rt-wt", "active")])
        text = self.notified[0][2]
        self.assertTrue(text.startswith("wedged: no tool result for 3h, last thread message 3h ago, cmux says Running since 3h ago"), text)
        for needle in ("T-dead", "workspace:40", "attempt rt-wt.a1.aaa", "Not re-dispatched",
                       "router-dispatch.sh --task-id rt-wt --force-redispatch"):
            self.assertIn(needle, text)
        self.assertEqual(self.closed, [], "the tab is left for the owner to inspect")
        self.assertTrue(self.path.exists(), "the checkout is left alone")
        # Second tick: nothing more, however long it stays wedged.
        self.assertEqual(self.flag()["wedged"], [])
        self.assertEqual(len(self.notified), 1)

    def test_fresh_thread_message_keeps_a_heartbeat_silent_worker_live(self) -> None:
        # code_exec blind spot: heartbeat 3 h old, cmux stamp 3 h old, but the thread moved 10 min ago.
        self.thread_at["T-dead"] = self.now - timedelta(minutes=10)
        write_ledger(self.silent_row())
        self.assertEqual(self.flag()["wedged"], [])
        self.assertNotIn("wedged_at", self.row())
        self.assertEqual(self.notified, [])

    def test_idle_stamp_or_any_young_local_signal_is_not_a_wedge(self) -> None:
        self.agents["T-dead"].update(state="Idle")
        write_ledger(self.silent_row())
        self.assertEqual(self.flag()["wedged"], [], "an Idle worker is nudge_idle's case")
        self.agents["T-dead"].update(state="Running", state_ts=(self.now - timedelta(minutes=30)).timestamp())
        write_ledger(self.silent_row())
        self.assertEqual(self.flag()["wedged"], [], "a cmux stamp inside the window is progress")
        self.agents["T-dead"].update(state_ts=(self.now - timedelta(hours=3)).timestamp())
        write_ledger(self.silent_row(heartbeat_at=self.hours_ago(1)))
        self.assertEqual(self.flag()["wedged"], [], "a heartbeat inside the window is progress")
        write_ledger(self.silent_row(heartbeat_at=None, started_at=self.hours_ago(1)))
        self.assertEqual(self.flag()["wedged"], [], "no heartbeat yet: started_at stands in")
        self.assertEqual(self.notified, [])

    def test_unreadable_thread_is_judged_on_local_signals_only_at_twice_the_threshold(self) -> None:
        self.thread_at["T-dead"] = None
        write_ledger(self.silent_row(hours=3))
        self.assertEqual(self.flag()["wedged"], [], "3 h silent, thread unreadable: under 2 × 2 h")
        self.agents["T-dead"]["state_ts"] = (self.now - timedelta(hours=5)).timestamp()
        write_ledger(self.silent_row(hours=5))
        result = self.flag()
        self.assertEqual([w["task_id"] for w in result["wedged"]], ["rt-wt"])
        self.assertIn("thread unreadable", self.notified[0][2])

    def test_rule_is_off_at_zero_and_skips_an_empty_store_and_a_conflict_window(self) -> None:
        write_ledger(self.silent_row())
        self.assertEqual(self.flag({**self.CFG, "wedged_hours": 0})["wedged"], [])
        write_ledger(self.silent_row(landing={"state": "conflict", "since": self.hours_ago(1)}))
        self.assertEqual(self.flag()["wedged"], [], "a landing-conflict window has its own clock")
        self.agents.clear()
        write_ledger(self.silent_row())
        result = self.flag()
        self.assertEqual(result["wedged"], [])
        self.assertIn("no agents", result["skipped"])
        self.assertEqual(self.notified, [])

    def test_heartbeat_clears_the_mark_so_a_woken_worker_is_live_again(self) -> None:
        write_ledger(self.silent_row())
        self.flag()
        self.assertTrue(self.row().get("wedged_at"))
        out = dispatch.run([sys.executable, str(SCRIPTS / "running-task-state.py"), "heartbeat",
                            "--task-id", "rt-wt", "--thread-id", "T-dead", "--attempt-id", "rt-wt.a1.aaa"])
        self.assertEqual(out.returncode, 0, out.stderr)
        row = self.row()
        for key in ("wedged", "wedged_at", "recovery_action", "stale_since"):
            self.assertNotIn(key, row)

    def test_thread_last_activity_reads_the_newest_message_or_updated_at(self) -> None:
        export = {"updatedAt": "2026-10-23T18:49:05.458Z",
                  "messages": [{"role": "user", "createdAt": "2026-10-23T17:46:50.000Z"},
                               {"role": "assistant", "createdAt": "2026-10-23T18:48:51.819Z",
                                "state": {"type": "complete", "stopReason": "tool_use"}}]}
        self.assertEqual(dispatch.thread_last_activity_export(export), datetime(2026, 10, 23, 18, 49, 5, 458000, tzinfo=timezone.utc))
        self.assertEqual(dispatch.thread_last_activity_export({"messages": [{"usage": {"timestamp": "2026-10-23T18:00:00Z"}}]}),
                         datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc))
        self.assertIsNone(dispatch.thread_last_activity_export({}))


class LaunchVerificationTests(HermeticRepo):
    """A launch command typed into a fresh tab can be consumed before the agent
    starts, so the launcher reads the screen. Wrong impls: retype the command
    into a tab whose screen is unreadable or still rendering (Amp would receive
    it as a message); retype more than once per launcher pause; treat the command
    echo or a mid-render screen as a shell prompt; answer a prompt on screen, which
    is for a person; skip the check on the sweep's relaunch path (it goes through
    release_queued → launch, the same as here)."""

    ACK_PROMPT = ("================================================================\n"
                  "  NOTICE: example security notice\n"
                  "================================================================\n"
                  "Type 'yes' to acknowledge this warning: ")
    AMP = "╭──────────────────────────────────────────╮\n│ >                                        │\n╰──────────────────────────────────────────╯\n Ctrl+O for help"
    ZSH = "Last login: Sun Oct 19 19:44:01 on ttys013\nowner@mac rt-x % "
    STARSHIP = "\n~/Development/switchboard/.amp/worktrees/rt-000000000048 on task/rt-000000000048\n❯ "

    def setUp(self) -> None:
        super().setUp()
        # Amp-shaped fixtures (screens, `amp -m` commands): pin the agent knob,
        # which defaults to claude (switchboard_agent.py).
        env_pin = patch.dict(os.environ, {"SWITCHBOARD_AGENT": "amp"})
        env_pin.start()
        self.addCleanup(env_pin.stop)
        self.sent: list[str] = []
        self.screens: list[str | None] = []
        self.budget = 0
        self.clock = 0.0

    def launch(self, screens: list[str | None], task_id: str = "rt-cf") -> dict:
        """release_queued for one queued row against a scripted cmux: new-workspace
        opens the tab, `tree` names a surface, each read-screen pops the next
        scripted screen (the last one repeats), send/send-key are recorded. The
        fake clock advances AMP_VERIFY_POLL_SECONDS per sleep so a 30 s budget
        is a bounded number of polls."""
        self.screens = list(screens)
        write_ledger({"id": task_id, "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                      "attempt": 1, "attempt_id": f"{task_id}.a1.aaa", "created_at": iso(datetime.now(timezone.utc))})
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if not cmd or str(cmd[0]) != dispatch.CMUX:
                return real_run(cmd, **kw)
            if "new-workspace" in cmd:
                self.command = cmd[cmd.index("--command") + 1]
                self.workspaces.append({"ref": "workspace:4090", "id": "w-90", "title": cmd[cmd.index("--name") + 1],
                                        "description": cmd[cmd.index("--description") + 1]})
                return subprocess.CompletedProcess(cmd, 0, "workspace:4090\n", "")
            if cmd[1] == "tree":
                return subprocess.CompletedProcess(cmd, 0, "workspace:4090\n  surface:7\n", "")
            if cmd[1] == "read-screen":
                screen = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
                return subprocess.CompletedProcess(cmd, 1 if screen is None else 0, screen or "", "")
            if cmd[1] == "send":
                self.sent.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1] == "send-key":
                self.sent.append(f"<{cmd[-1]}>")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kw)

        def sleep(seconds: float) -> None:
            # Rounded: 0.1 s fsmonitor-stop sleeps accumulate float error before the tab opens,
            # which put the verify deadline a hair past 15 polls and gave one extra read (order-dependent flake).
            self.clock = round(self.clock + seconds, 6)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch.time, "sleep", sleep), \
                patch.object(dispatch.time, "monotonic", lambda: self.clock), \
                patch.object(dispatch, "AMP_VERIFY_SECONDS", self.budget), redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        return self.row(task_id)

    # --- the prompt names the session the tab is launched with ------------------

    def test_claude_launch_prompt_names_the_session_it_launches_with(self) -> None:
        with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "claude"}):
            self.launch([self.ZSH])
        session = self.command.split("--session-id ", 1)[1].split()[0]
        prompt = next(dispatch.LAUNCH_PROMPTS.glob("switchboard-maintenance-rt-cf.a1*")).read_text(encoding="utf-8")
        self.assertIn(f"--thread-id {session} ", prompt)
        self.assertIn(f"claude --resume {session}", prompt)
        self.assertNotIn("ampcode.com/threads", prompt)

    # --- the tab is verified to run Amp; a bare shell gets the command once -------

    def test_bare_shell_after_the_budget_gets_the_launch_command_typed_once(self) -> None:
        self.budget = 30
        row = self.launch([self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH,
                           self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.AMP])
        self.assertEqual(self.sent, [self.command, "<Enter>"], "the identical --command string, exactly once")
        self.assertIn("amp -m ultra", self.command)
        self.assertEqual((row["launch_retyped"], row["amp_verified"]), (True, True))

    def test_starship_prompt_counts_as_a_bare_shell(self) -> None:
        # A screenshot: a two-line prompt ending in ❯ at the worktree path.
        self.budget = 30
        row = self.launch([self.STARSHIP] * 16 + [self.AMP])
        self.assertEqual(self.sent, [self.command, "<Enter>"])
        self.assertTrue(row["launch_retyped"])

    def test_shell_that_stays_bare_is_retyped_once_per_launcher_pause_then_left_to_the_sweep(self) -> None:
        # `amp` exits with ECONNRESET at startup, so the first retype can die the
        # same way. The launcher retries once per pause (0 s, then 20 s)
        # and hands over to the sweep; it never loops on its own.
        self.budget = 30
        row = self.launch([self.ZSH])
        self.assertEqual(self.sent.count(self.command), len(dispatch.LAUNCH_RETYPE_PAUSES_SECONDS))
        self.assertEqual((row["launch_retyped"], row["launch_retypes"], row["amp_verified"]), (True, 2, False))
        self.assertIn("screen read as shell", row["amp_verify_note"])
        self.assertIn("the sweep keeps retyping", row["amp_verify_note"])
        self.assertGreaterEqual(self.clock, 20, "the second retype waits out the pause (network blips clear in seconds)")
        self.assertEqual(row["status"], "routing", "the sweep, not the launcher, judges liveness from here")

    def test_amp_connect_error_at_startup_is_a_bare_shell_and_named_in_the_verdict(self) -> None:
        # Same shape: a zsh PROMPT that is the bare
        # path plus branch with no trailing glyph, and amp's error sits above it.
        self.budget = 30
        echo = "cat /tmp/launch-prompt.md | amp -m ultra"  # the echoed launch command; self.command exists only after launch()
        first = (f"{echo}\n"
                 "ECONNRESET: The socket connection was closed unexpectedly. For more information, pass `verbose: true` in the second argument to fetch()\n"
                 "~/Development/switchboard/.amp/worktrees/rt-cf/workstreams/switchboard-maintenance task/rt-cf ")
        row = self.launch([first] * 16 + [self.AMP])
        self.assertEqual(self.sent, [self.command, "<Enter>"])
        self.assertEqual((row["launch_retyped"], row["amp_verified"]), (True, True))
        self.assertEqual(dispatch.amp_start_error(first).split(":")[0], "ECONNRESET")

        self.sent.clear()
        second = ("Couldn't connect to the Amp server. Check your network connection and try again.\n"
                  "~/Development/switchboard/.amp/worktrees/rt-cf2/workstreams/example-cli-consent-copy task/rt-cf2 ")
        row = self.launch([second], task_id="rt-cf2")
        self.assertEqual(self.sent.count(self.command), len(dispatch.LAUNCH_RETYPE_PAUSES_SECONDS))
        self.assertIn("amp exited: Couldn't connect to the Amp server", row["amp_verify_note"])
        self.assertEqual(row["amp_verified"], False)

    def test_this_macs_glyphless_prompt_is_a_shell_but_a_command_echo_is_not(self) -> None:
        prompt = "~/Development/switchboard/.amp/worktrees/rt-x/workstreams/switchboard-maintenance task/rt-x "
        self.assertEqual(dispatch.classify_screen(prompt), "shell")
        self.assertEqual(dispatch.classify_screen("~/Development/switchboard main "), "shell")
        self.assertEqual(dispatch.classify_screen("~ "), "shell", "outside a git checkout __git_ps1 prints nothing")
        self.assertEqual(dispatch.classify_screen(f"{prompt}cat /tmp/p.md | amp -m ultra"), "unknown")
        self.assertEqual(dispatch.classify_screen("Loading 100%"), "unknown")
        self.assertEqual(dispatch.classify_screen("reading ~/Development/switchboard/notes"), "unknown", "a path mid-sentence is not a prompt")

    def test_sweep_retypes_a_stalled_launch_once_per_tick_until_the_cap(self) -> None:
        # The launcher gave up (two retypes); the sweep keeps typing the exact
        # launch command once per tick while the tab reads as a bare shell, stops at
        # MAX_LAUNCH_RETYPES, and never types into amp or an unknown screen.
        self.budget = 30
        self.launch([self.ZSH])
        row = self.row("rt-cf")
        self.assertEqual(row["launch_retypes"], 2)
        self.sent.clear()
        screen = {"value": self.ZSH}
        typed: list[tuple[str, str]] = []
        with patch.object(dispatch, "read_tab_screen", lambda ref: screen["value"]), \
                patch.object(dispatch, "type_into_tab", lambda ref, text: typed.append((ref, text)) or True), \
                patch.object(dispatch, "MAX_LAUNCH_RETYPES", 4), redirect_stdout(io.StringIO()):
            fresh = dispatch.retype_stalled_launches()
            self.assertEqual(fresh["retyped"], [], "a row the launcher touched seconds ago is not raced")
            aged = iso(datetime.now(timezone.utc) - dispatch.SWEEP_RETYPE_MIN_AGE - timedelta(seconds=1))
            ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
            first = dispatch.retype_stalled_launches()
            self.assertEqual([(r["task_id"], r["retypes"], r["sent"]) for r in first["retyped"]], [("rt-cf", 3, True)])
            self.assertEqual(typed, [("workspace:4090", dispatch.launch_command_for(self.row("rt-cf")))])
            self.assertEqual(typed[0][1], self.command, "the identical --command string the tab was opened with")
            self.assertEqual(self.row("rt-cf")["launch_retypes"], 3)
            self.assertEqual(dispatch.retype_stalled_launches()["retyped"], [], "updated_at moved: one retype per tick")
            ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
            self.assertEqual(dispatch.retype_stalled_launches()["retyped"][0]["retypes"], 4)
            ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
            capped = dispatch.retype_stalled_launches()
            self.assertEqual((capped["retyped"], [e["retypes"] for e in capped["exhausted"]]), ([], [4]))
            self.assertIn("launch_retypes_exhausted_at", self.row("rt-cf"))
            self.assertEqual(len(typed), 2)
            forced = dispatch.retype_stalled_launches(only="rt-cf", force=True)
            self.assertEqual(forced["retyped"][0]["retypes"], 5, "--force (an operator) ignores the cap and the age")
            screen["value"] = self.AMP
            ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged, launch_retypes=0) or ledger.find_task(p, "rt-cf"))
            self.assertEqual(dispatch.retype_stalled_launches()["skipped"][0]["screen"], "amp")
            screen["value"] = None
            self.assertEqual(dispatch.retype_stalled_launches()["skipped"][0]["screen"], "unknown")
            self.assertEqual(len(typed), 3, "amp and unknown screens are never typed into")

    def test_sweep_reopens_a_routing_row_whose_tab_vanished(self) -> None:
        # The worker tab is gone from `cmux workspace list` while the row sits routing with its worktree and launch prompt intact,
        # so no retype could reach it. The sweep opens a fresh tab on the same
        # worktree with the identical command, rebinds the ref, counts one retype,
        # and never reads or types into the dead ref. Wrong impls: treat an empty
        # cmux read (RPC timeout) as "every tab is gone"; reopen a tab the task
        # already has under a new ref (same description); reopen when the prompt
        # file is missing (the command would cat nothing into amp).
        self.budget = 30
        self.launch([self.ZSH])
        row = self.row("rt-cf")
        self.assertEqual((row["workspace_ref"], row["launch_retypes"]), ("workspace:4090", 2))
        aged = iso(datetime.now(timezone.utc) - dispatch.SWEEP_RETYPE_MIN_AGE - timedelta(seconds=1))
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
        created: list[list[str]] = []
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                created.append([str(c) for c in cmd])
                return subprocess.CompletedProcess(cmd, 0, "workspace:4519\n", "")
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), \
                patch.object(dispatch, "read_tab_screen", lambda ref: self.fail(f"read a dead ref {ref}")), \
                patch.object(dispatch, "type_into_tab", lambda ref, text: self.fail(f"typed into a dead ref {ref}")), \
                redirect_stdout(io.StringIO()):
            self.workspaces = []   # cmux read failed or returned nothing: not evidence the tab is gone
            unreadable = dispatch.retype_stalled_launches()
            self.assertEqual(unreadable["retyped"], [])
            self.assertEqual(unreadable["skipped"][0]["screen"], "cmux unreadable")
            self.assertEqual(created, [])
            self.workspaces = [{"ref": "workspace:4511", "id": "w-511", "title": "🛠️ Switchboard Maintenance 2",
                                "description": "switchboard worker rt-other"}]
            result = dispatch.retype_stalled_launches()
        self.assertEqual([(r["task_id"], r["workspace_ref"], r["retypes"], r["reopened_from"]) for r in result["retyped"]],
                         [("rt-cf", "workspace:4519", 3, "workspace:4090")])
        self.assertEqual(len(created), 1)
        create = created[0]
        self.assertEqual(create[create.index("--command") + 1], self.command, "the identical command the first tab ran")
        self.assertEqual(create[create.index("--description") + 1], "switchboard worker rt-cf")
        self.assertEqual(create[create.index("--name") + 1], row["tab_title"])
        # Same cwd rule as launch(): the workstream folder when the worktree has one, else the worktree root.
        ws_dir = Path(row["worktree"]) / "workstreams" / "switchboard-maintenance"
        self.assertEqual(create[create.index("--cwd") + 1], str(ws_dir if ws_dir.is_dir() else Path(row["worktree"])))
        row = self.row("rt-cf")
        self.assertEqual((row["status"], row["workspace_ref"], row["tab_reopened_from"], row["launch_retypes"]),
                         ("routing", "workspace:4519", "workspace:4090", 3))
        self.assertIn("reopened as workspace:4519", row["amp_verify_note"])

        # The same task already has a tab under another ref: rebind nothing, open nothing.
        self.workspaces = [{"ref": "workspace:4520", "id": "w-520", "title": row["tab_title"], "description": "switchboard worker rt-cf"}]
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
        with patch.object(dispatch, "run", fake_run), patch.object(dispatch, "read_tab_screen", lambda ref: self.AMP), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.retype_stalled_launches()["skipped"][0]["screen"], "amp")
        self.assertEqual(len(created), 1)

        # No launch prompt on disk: the reopen is refused and reported, not typed blind.
        self.workspaces = [{"ref": "workspace:1", "id": "w-1", "title": "🧭 CoS"}]
        (dispatch.LAUNCH_PROMPTS / f"switchboard-maintenance-{row['attempt_id']}.md").unlink()
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-cf").update(updated_at=aged) or ledger.find_task(p, "rt-cf"))
        with patch.object(dispatch, "run", fake_run), redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.retype_stalled_launches()["skipped"], [{"task_id": "rt-cf", "workspace_ref": "workspace:4519", "screen": "tab gone; reopen failed"}])
        self.assertEqual(len(created), 1)

    def test_an_acknowledgement_prompt_is_left_for_a_person(self) -> None:
        # The launcher never answers a prompt on someone's behalf: not `yes`, and
        # not the launch command, which that prompt would read as its answer.
        self.budget = 30
        row = self.launch([self.ACK_PROMPT])
        self.assertEqual(self.sent, [])
        self.assertEqual((row["amp_verified"], row["launch_retyped"]), ("unknown", False))

    def test_the_sweep_leaves_an_acknowledgement_prompt_alone(self) -> None:
        self.launch([self.ACK_PROMPT])
        typed: list[tuple[str, str]] = []
        with patch.object(dispatch, "read_tab_screen", lambda ref: self.ACK_PROMPT), \
                patch.object(dispatch, "type_into_tab", lambda ref, text: typed.append((ref, text)) or True), \
                redirect_stdout(io.StringIO()):
            result = dispatch.retype_stalled_launches(only="rt-cf", force=True)
        self.assertEqual(typed, [])
        self.assertEqual(result["skipped"][0]["screen"], "unknown")

    def test_amp_on_screen_means_nothing_is_typed(self) -> None:
        self.budget = 30
        row = self.launch([self.ZSH, self.AMP])
        self.assertEqual(self.sent, [])
        self.assertEqual((row["amp_verified"], row["launch_retyped"]), (True, False))

    def test_slow_amp_inside_the_budget_is_waited_for_not_retyped(self) -> None:
        # A shell prompt is what the tab shows for an instant before cmux's
        # keystrokes land; judging it before the budget ends would double-launch.
        self.budget = 30
        row = self.launch([self.ZSH, self.ZSH, self.ZSH, None, "", self.AMP])
        self.assertEqual(self.sent, [])
        self.assertEqual(row["amp_verified"], True)

    def test_unreadable_or_unrecognised_screens_are_never_typed_into(self) -> None:
        self.budget = 30
        row = self.launch([None])
        self.assertEqual(self.sent, [])
        self.assertEqual(row["amp_verified"], "unknown")
        self.sent.clear()
        echo = f"{{OWNER_USER}}@mac rt-x % {self.command}\nLoading…"
        row = self.launch([echo], task_id="rt-cf2")
        self.assertEqual(self.sent, [], "the command echo is not a bare prompt; Amp may be about to render")
        self.assertEqual(row["amp_verified"], "unknown")

    def test_classify_screen_cases(self) -> None:
        self.assertEqual(dispatch.classify_screen(self.AMP), "amp")
        self.assertEqual(dispatch.classify_screen("Welcome to Amp\n"), "amp")
        self.assertEqual(dispatch.classify_screen(self.ACK_PROMPT), "unknown", "a prompt on screen is for a person")
        self.assertEqual(dispatch.classify_screen(self.ZSH), "shell")
        self.assertEqual(dispatch.classify_screen(self.STARSHIP), "shell")
        self.assertEqual(dispatch.classify_screen("➜  rt-x git:(task/rt-x) "), "shell")
        self.assertEqual(dispatch.classify_screen("bash-5.2$ "), "shell")
        self.assertEqual(dispatch.classify_screen(None), "unknown")
        self.assertEqual(dispatch.classify_screen(""), "unknown")
        self.assertEqual(dispatch.classify_screen("% cat prompt.md | amp -m ultra"), "unknown")
        self.assertEqual(dispatch.classify_screen("Loading 100%"), "unknown", "a % inside a word is not a prompt")


class MassChangeGuardTests(HermeticRepo):
    """Fix E: finish refuses a branch that deletes more than MASS_DELETE_LIMIT
    tracked files or tracks a path under a directory main ignores, unless
    --allow-mass-delete says so."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "docs").mkdir()
        for n in range(60):
            (self.repo / "docs" / f"d{n:02}.md").write_text(f"doc {n}\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "sixty docs")
        self._git("reset", "-q", "--hard", "main", cwd=self.path)
        self.task = self.worker_row()

    def test_more_than_the_limit_of_deletions_is_refused_until_allowed(self) -> None:
        for n in range(51):
            (self.path / "docs" / f"d{n:02}.md").unlink()
        outcome = dispatch.finish_branch(self.task)
        self.assertEqual((outcome["landed"], outcome["conflict"], outcome["mass_change"]), (False, True, True))
        self.assertIn("deletes 51 tracked files", outcome["reason"])
        self.assertEqual(len(self._git("ls-files", "docs").split()), 60)   # main untouched
        self.assertTrue(dispatch.finish_branch(self.task, allow_mass_delete=True)["landed"])
        self.assertEqual(len(self._git("ls-files", "docs").split()), 9)

    def test_exactly_the_limit_lands_without_the_flag(self) -> None:
        for n in range(50):
            (self.path / "docs" / f"d{n:02}.md").unlink()
        self.assertTrue(dispatch.finish_branch(self.task)["landed"])
        self.assertEqual(len(self._git("ls-files", "docs").split()), 10)

    def test_tracked_path_under_a_directory_main_ignores_is_refused(self) -> None:
        # 8b3230df shape without the symlink: the branch tracks people/<file> while
        # main keeps people/ as real, ignored data.
        (self.repo / ".gitignore").write_text(".amp/*\n/people/\n")
        self._git("commit", "-q", "-am", "ignore people")
        (self.repo / "people").mkdir()
        (self.repo / "people" / "alice.md").write_text("# Alice (real, ignored on main)\n")
        self._git("reset", "-q", "--hard", "main", cwd=self.path)
        (self.path / "people").mkdir()
        (self.path / "people" / "alice.md").write_text("# Alice, rewritten on the branch\n")
        (self.path / "a.md").write_text("ordinary edit\n")
        self._git("add", "-f", "--", "people/alice.md", cwd=self.path)
        self._git("commit", "-q", "-am", "track a profile", cwd=self.path)
        outcome = dispatch.finish_branch(self.task)
        self.assertEqual((outcome["landed"], outcome["conflict"]), (False, True), outcome)
        self.assertIn("people/alice.md (under people)", outcome["reason"])
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice (real, ignored on main)\n")
        self.assertEqual(self._git("show", "main:a.md"), "base\n")
        # A new file that merely matches an ignore pattern elsewhere is not this case.
        self.assertEqual(dispatch.tracked_into_ignored_dirs(["docs/new.md"]), [])
        self.assertTrue(dispatch.finish_branch(self.task, allow_mass_delete=True)["landed"])


class ScaffoldCommitTests(HermeticRepo):
    """A new "Workstream: <slug>" is scaffolded on main's working tree through
    new-workstream.py, which never commits; `git worktree add ... main` branches from
    main's HEAD, so without a commit the worker's checkout has no folder for it and the
    rebase later hits an add/add conflict. Scaffold edits are committed on main before
    the branch is cut."""

    SLUG = "zz-new-diagnosis"

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "workstreams").mkdir()
        (self.repo / "workstreams" / "README.md").write_text("| none |\n")
        with (self.repo / ".gitignore").open("a") as handle:
            handle.write("workstreams/**/private/\n")   # as in the real repo
        self._git("add", "--", "workstreams/README.md", ".gitignore")
        self._git("commit", "-q", "-m", "readme")
        self.real_run = dispatch.run

    def fake_scaffold(self, cmd, **kw):
        """Stand-in for new-workstream.py: writes exactly what the real script writes on main
        (folder with CONTEXT.md, config.yaml, and the verbatim ask in the gitignored
        private/<date>-asks.md; README re-rendered)."""
        if len(cmd) > 1 and str(cmd[1]).endswith("new-workstream.py"):
            slug = cmd[cmd.index("--name") + 1]
            folder = self.repo / "workstreams" / slug
            (folder / "private").mkdir(parents=True)
            (folder / "CONTEXT.md").write_text(f"# {slug}\n")
            (folder / "config.yaml").write_text(f"name: {slug}\nstatus: active\n")
            (folder / "private" / "2026-10-19-asks.md").write_text("> the ask\n")
            (self.repo / "workstreams" / "README.md").write_text(f"| {slug} | Active |\n")
            return argparse.Namespace(stdout=json.dumps({"slug": slug, "created": True}), stderr="", returncode=0)
        return self.real_run(cmd, **kw)

    def dirty_under_workstreams(self) -> list[str]:
        return [l for l in self._git("status", "--porcelain", "--untracked-files=all").splitlines() if "workstreams/" in l]

    def test_directive_scaffold_is_committed_before_the_branch_is_cut(self) -> None:
        # Wrong impl (the old one): create_workstream returns with main dirty on the four
        # scaffold paths and a branch cut afterwards lacks the folder entirely.
        with patch.object(dispatch, "run", side_effect=self.fake_scaffold), redirect_stderr(io.StringIO()):
            created = dispatch.create_workstream(f"Workstream: {self.SLUG} fix the LG audio", self.SLUG)
        self.assertEqual(created, self.SLUG)
        self.assertEqual(self.dirty_under_workstreams(), [], "scaffold paths must be committed on main, not left dirty")
        head = self._git("log", "-1", "--format=%an %s")
        self.assertIn("switchboard worker", head)
        self.assertIn(f"{self.SLUG}: scaffold workstream", head)
        tracked = set(self._git("ls-tree", "-r", "--name-only", "HEAD", "--", "workstreams").splitlines())
        self.assertLessEqual({f"workstreams/{self.SLUG}/CONTEXT.md", f"workstreams/{self.SLUG}/config.yaml",
                              "workstreams/README.md"}, tracked)
        self.assertNotIn(f"workstreams/{self.SLUG}/private/2026-10-19-asks.md", tracked,
                         "the verbatim ask never lands in a commit")
        self.assertIn(self.SLUG, self._git("show", "HEAD:workstreams/README.md"), "the re-rendered README lands in the same commit")
        path, _branch = dispatch.create_worktree("rt-new")
        self.assertTrue((path / "workstreams" / self.SLUG / "CONTEXT.md").is_file(),
                        "a worker branch cut after the scaffold must carry the workstream folder")
        self.assertFalse((path / "workstreams" / self.SLUG / "private").exists())

    def test_untracked_scaffold_from_another_writer_is_committed_at_launch(self) -> None:
        # process-new-inbox.py or a hand-made folder: nothing under workstreams/<slug> at HEAD.
        folder = self.repo / "workstreams" / self.SLUG
        folder.mkdir()
        (folder / "CONTEXT.md").write_text("# by hand\n")
        (folder / "config.yaml").write_text("status: active\n")
        self.assertTrue(dispatch.workstream_untracked(self.SLUG))
        with redirect_stderr(io.StringIO()):
            outcome = dispatch.commit_scaffold(self.SLUG, "untracked on main when rt-x launched")
        self.assertTrue(outcome["committed"], outcome)
        self.assertFalse(dispatch.workstream_untracked(self.SLUG))
        self.assertEqual(self.dirty_under_workstreams(), [])
        self.assertIn("untracked on main when rt-x launched", self._git("log", "-1", "--format=%s"))

    def test_existing_workstream_with_edits_in_flight_is_not_swept(self) -> None:
        # Wrong impl: treating any dirty workstreams/<slug> as a scaffold would commit a
        # human's half-written CONTEXT.md at every launch. Only a folder HEAD does not track counts.
        folder = self.repo / "workstreams" / "switchboard-maintenance"
        folder.mkdir()
        (folder / "CONTEXT.md").write_text("# tracked\n")
        self._git("add", "--", "workstreams/switchboard-maintenance")
        self._git("commit", "-q", "-m", "existing workstream")
        (folder / "CONTEXT.md").write_text("# tracked\nhalf-written edit\n")
        self.assertFalse(dispatch.workstream_untracked("switchboard-maintenance"))
        self.assertFalse(dispatch.workstream_untracked("never-existed"))
        self.assertEqual(self.dirty_under_workstreams(), [" M workstreams/switchboard-maintenance/CONTEXT.md"])

    def test_foreign_staged_changes_block_the_scaffold_commit(self) -> None:
        # A human's staged edit elsewhere on main must not be swept into the scaffold commit.
        (self.repo / "a.md").write_text("human staged this\n")
        self._git("add", "--", "a.md")
        folder = self.repo / "workstreams" / self.SLUG
        folder.mkdir()
        (folder / "CONTEXT.md").write_text("# scaffold\n")
        with redirect_stderr(io.StringIO()):
            outcome = dispatch.commit_scaffold(self.SLUG, "test")
        self.assertFalse(outcome["committed"])
        self.assertIn("a.md", outcome["reason"])
        self.assertEqual(self._git("diff", "--cached", "--name-only").split(), ["a.md"], "the human's index is left exactly as it was")
        self.assertEqual(self._git("log", "-1", "--format=%s").strip(), "readme")


class ScratchIsolationTests(HermeticRepo):
    """A worktree's .amp/in that is a symlink to main's lets a scratch script that
    derives the repo root from its own resolved path write into MAIN.
    .amp/in is worktree-local and migrates into main's on landing or drop;
    finish refuses probable stray writes instead of attribute-committing them;
    the sweep tells the worker in its tab once per path."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".amp" / "in").mkdir(parents=True)
        (self.repo / ".amp" / "in" / "shared.txt").write_text("main's copy\n")
        (self.repo / ".amp" / "locks").mkdir()
        self.lines = [f"line {n}\n" for n in range(1, 11)]
        (self.repo / "a.md").write_text("".join(self.lines))   # long enough that a top and a bottom edit merge cleanly
        self._git("commit", "-q", "-am", "ten lines")
        self.path, self.branch = dispatch.create_worktree("rt-scratch")
        self.task = self.worker_row(id="rt-scratch", worktree=str(self.path), branch=self.branch)

    def test_new_worktree_gets_its_own_scratch_dir_while_locks_stay_shared(self) -> None:
        # Wrong impl: link every ignored entry under .amp (the old behaviour), or
        # stop linking .amp/locks too (the flock FIFO would fork per worktree).
        scratch = self.path / ".amp" / "in"
        self.assertTrue(scratch.is_dir() and not dispatch.winlink.is_link(scratch), scratch)
        self.assertFalse((scratch / "shared.txt").exists())
        self.assertTrue(dispatch.winlink.is_link(self.path / ".amp" / "locks"))
        # The failure shape: a script under .amp/in resolving its own path lands in the worktree.
        probe = scratch / "probe.py"
        probe.write_text("from pathlib import Path\nprint(Path(__file__).resolve().parents[2])\n")
        out = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(os.path.realpath(out), os.path.realpath(self.path))
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=all", cwd=self.path).strip(), "")   # ignored, never staged

    def test_landing_moves_scratch_into_main_without_overwriting(self) -> None:
        scratch = self.path / ".amp" / "in"
        (scratch / "shared.txt").write_text("worktree's copy\n")              # collides with main's
        (scratch / "artifacts" / "rt-scratch").mkdir(parents=True)
        (scratch / "artifacts" / "rt-scratch" / "shot.png").write_bytes(b"png")
        (self.repo / ".amp" / "in" / "artifacts").mkdir()
        (self.repo / ".amp" / "in" / "artifacts" / "older.png").write_bytes(b"old")  # a sibling that must survive the merge
        (self.path / "a.md").write_text("edited on the branch\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual(outcome["scratch"]["moved"], 2)
        self.assertEqual(outcome["scratch"]["renamed"], [".amp/in/shared.rt-scratch.txt"])
        main_in = self.repo / ".amp" / "in"
        self.assertEqual((main_in / "shared.txt").read_text(), "main's copy\n")
        self.assertEqual((main_in / "shared.rt-scratch.txt").read_text(), "worktree's copy\n")
        self.assertEqual((main_in / "artifacts" / "rt-scratch" / "shot.png").read_bytes(), b"png")
        self.assertTrue((main_in / "artifacts" / "older.png").exists())
        self.assertFalse((scratch / "shared.txt").exists())
        self.assertTrue(scratch.is_dir())   # the worker may still write here until the worktree is dropped

    def test_dropping_a_clean_worktree_moves_scratch_but_a_symlinked_one_is_left_alone(self) -> None:
        (self.path / ".amp" / "in" / "note.txt").write_text("late scratch\n")
        dispatch.drop_worktree(self.task)
        self.assertEqual((self.repo / ".amp" / "in" / "note.txt").read_text(), "late scratch\n")
        # A legacy worktree still links to main's .amp/in.
        legacy = self.repo / ".amp" / "worktrees" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / ".amp").mkdir()
        dispatch.winlink.link_path(self.repo / ".amp" / "in", legacy / ".amp" / "in")
        self.assertEqual(dispatch.migrate_scratch(legacy, "rt-legacy"), {"skipped": "symlink to main"})
        self.assertEqual((self.repo / ".amp" / "in" / "shared.txt").read_text(), "main's copy\n")
        self.assertTrue(dispatch.winlink.is_link(legacy / ".amp" / "in"))

    def _branch_edit(self) -> None:
        (self.path / "a.md").write_text("".join(self.lines) + "branch line\n")

    def _main_edit(self, text: str) -> None:
        (self.repo / "a.md").write_text(text + "".join(self.lines[1:]))

    def test_finish_refuses_a_main_edit_to_a_held_path_written_after_the_task_started(self) -> None:
        # Wrong impl: the old attribute-commit ("left by direct-to-main workers"),
        # which buries the worker's own leak in main's history.
        self._branch_edit()
        (self.repo / "a.md").write_text("".join(self.lines) + "branch line\n")   # the stray write: same bytes landed in MAIN
        (self.repo / "new.md").write_text("also stray\n")        # untracked, but not a path the branch holds
        outcome = dispatch.finish_branch(self.task)
        self.assertEqual((outcome["landed"], outcome["conflict"]), (False, True), outcome)
        self.assertEqual([s["path"] for s in outcome["stray_writes"]], ["a.md"])
        self.assertTrue(outcome["stray_writes"][0]["identical_to_worktree"])
        self.assertIn("git -C", outcome["reason"])
        self.assertIn("--accept-main-edits", outcome["reason"])
        self.assertIn(" M a.md", self._git("status", "--porcelain"))            # main untouched, nothing committed
        self.assertNotIn("direct-to-main", self._git("log", "--format=%s", "main"))
        # Restoring main is the remediation the reason names; the next finish lands.
        self._git("checkout", "--", "a.md")
        landed = dispatch.finish_branch(self.task)
        self.assertTrue(landed["landed"], landed)
        self.assertIn("branch line", (self.repo / "a.md").read_text())

    def test_accept_main_edits_lands_with_attribution(self) -> None:
        self._branch_edit()
        self._main_edit("line 1 (edited on main meanwhile)\n")
        outcome = dispatch.finish_branch(self.task, accept_main_edits=True)
        self.assertTrue(outcome["landed"], outcome)
        self.assertEqual(outcome["accepted_main_edits"], ["a.md"])
        self.assertEqual(outcome["preserved_paths"], ["a.md"])
        self.assertIn("left by direct-to-main workers", self._git("log", "--format=%s", "main"))

    def test_a_main_edit_older_than_the_task_is_the_ordinary_overlap(self) -> None:
        # A scheduled job's edit from before the task started is not a stray write.
        self._branch_edit()
        self._main_edit("line 1 (edited on main before the task)\n")
        old = (datetime.now(timezone.utc) - ledger.STALE_AFTER - timedelta(hours=1)).timestamp()
        os.utime(self.repo / "a.md", (old, old))
        outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertNotIn("accepted_main_edits", outcome)
        self.assertEqual(outcome["preserved_paths"], ["a.md"])

    def test_sweep_tells_the_worker_once_per_path(self) -> None:
        write_ledger(self.task)
        self._branch_edit()
        (self.repo / "a.md").write_text("stray\n")
        sent: list[tuple[str, str]] = []
        with patch.object(dispatch, "tab_send", lambda ref, text: sent.append((ref, text)) or True):
            first = dispatch.flag_stray_writes()
            second = dispatch.flag_stray_writes()
            (self.path / "b.md").write_text("another branch file\n")
            (self.repo / "b.md").write_text("another stray\n")
            third = dispatch.flag_stray_writes()
        self.assertEqual(first["flagged"], [{"task_id": "rt-scratch", "paths": ["a.md"]}])
        self.assertEqual((first["told"], second["told"], third["told"]), (["rt-scratch"], [], ["rt-scratch"]))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], "workspace:40")
        self.assertIn("a.md", sent[0][1])
        self.assertIn("git rev-parse --show-toplevel", sent[0][1])
        self.assertIn("b.md", sent[1][1])
        self.assertNotIn("a.md", sent[1][1])
        self.assertEqual(json.loads(dispatch.STRAY_WRITE_STAMPS.read_text()), {"rt-scratch": ["a.md", "b.md"]})
        # A tab that cannot take the message is retried next sweep, not stamped.
        (self.path / "c.md").write_text("c\n")
        (self.repo / "c.md").write_text("c stray\n")
        with patch.object(dispatch, "tab_send", lambda ref, text: False):
            self.assertEqual(dispatch.flag_stray_writes()["told"], [])
        self.assertEqual(json.loads(dispatch.STRAY_WRITE_STAMPS.read_text()), {"rt-scratch": ["a.md", "b.md"]})


class FsmonitorTests(unittest.TestCase):
    """When ~/.gitconfig turns core.fsmonitor on, a new worktree spawns its own
    fsmonitor daemon and `worktree add` can hang on that daemon's socket; a daemon the socket cannot reach also outlives `worktree
    remove`. Wrong impls: run the add under the inherited config (a daemon appears
    for the worktree); stop the daemon only through the socket (a wedged one stays);
    match daemons by ancestor path (the repo's own daemon is killed with the
    worktree's). The fixture plays ~/.gitconfig through GIT_CONFIG_GLOBAL."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        gitconfig = Path(self.tmp.name) / "gitconfig"
        gitconfig.write_text("[core]\n\tfsmonitor = true\n[user]\n\tname = t\n\temail = t@t\n")
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.patches = [patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gitconfig)}),
                        patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "WORKTREES", self.repo / ".amp" / "worktrees"),
                        patch.object(dispatch, "FSMONITOR_STOP_TIMEOUT", 1.0)]
        for p in self.patches:
            p.start()
        self._git("init", "-q", "-b", "main")
        (self.repo / "a.md").write_text("base\n")
        (self.repo / ".gitignore").write_text(".amp/*\n")
        self._git("add", "--", "a.md", ".gitignore")
        self._git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        mine = os.path.realpath(self.tmp.name)
        for pid, held in dispatch.fsmonitor_daemons().items():   # never leave a fixture daemon behind
            if any(os.path.realpath(v).startswith(mine + "/") for v in held.values() if v):
                os.kill(pid, 9)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return dispatch.run(["git", "-C", str(cwd or self.repo), "-c", "core.fsmonitor=false", *args], check=True).stdout

    def wait_dead(self, pid: int) -> bool:
        for _ in range(30):
            if not dispatch.pid_alive(pid):
                return True
            dispatch.time.sleep(0.1)
        return False

    def test_create_worktree_runs_with_fsmonitor_off_and_pins_it_off_for_the_repo(self) -> None:
        effective = lambda path: dispatch.run(["git", "-C", str(path), "config", "core.fsmonitor"]).stdout.strip()  # noqa: E731
        self.assertEqual(effective(self.repo), "true")   # inherited from the global config
        adds: list[list[str]] = []
        real_run = dispatch.run

        def spying_run(cmd, **kw):
            if "worktree" in cmd and "add" in cmd:
                adds.append(list(cmd))
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", spying_run):
            path, _ = dispatch.create_worktree("rt-fs")
        self.assertEqual(len(adds), 1)
        argv = adds[0]
        self.assertLess(argv.index("core.fsmonitor=false"), argv.index("worktree"))
        self.assertEqual(argv[argv.index("core.fsmonitor=false") - 1], "-c")
        self.assertEqual(self._git("config", "--local", "core.fsmonitor").strip(), "false")
        self.assertEqual((effective(self.repo), effective(path)), ("false", "false"))
        self.assertFalse((self.repo / ".git" / "worktrees" / "rt-fs" / "fsmonitor--daemon.ipc").exists())
        dispatch.time.sleep(0.3)   # a daemon spawned by the add creates its socket asynchronously
        self.assertIsNone(dispatch.fsmonitor_daemon_pid(path))
        self.assertIsNone(dispatch.fsmonitor_daemon_pid(self.repo))

    @unittest.skipIf(os.name == "nt", "fsmonitor daemon discovery needs pgrep and lsof, which Windows lacks")
    def test_drop_worktree_kills_a_wedged_daemon_of_that_checkout_only(self) -> None:
        path, branch = dispatch.create_worktree("rt-fs")
        for checkout in (path, self.repo):
            dispatch.run(["git", "-C", str(checkout), "-c", "core.fsmonitor=true", "fsmonitor--daemon", "start"], check=True)
        wt_pid, repo_pid = dispatch.fsmonitor_daemon_pid(path), dispatch.fsmonitor_daemon_pid(self.repo)
        self.assertIsNotNone(wt_pid)
        self.assertIsNotNone(repo_pid)
        self.assertNotEqual(wt_pid, repo_pid)
        # The wedged shape: a daemon that answers nothing on its socket. A stopped
        # process also sits on a plain SIGTERM until it is continued, so only a kill ends it.
        os.kill(wt_pid, dispatch.signal.SIGSTOP)
        self.addCleanup(lambda: dispatch.pid_alive(wt_pid) and os.kill(wt_pid, dispatch.signal.SIGKILL))
        parked = dispatch.drop_worktree({"id": "rt-fs", "worktree": str(path), "branch": branch})
        self.assertIsNone(parked)
        self.assertTrue(self.wait_dead(wt_pid), f"worktree daemon {wt_pid} survived drop_worktree")
        self.assertTrue(dispatch.pid_alive(repo_pid), "the repo's own daemon is not the worktree's")
        self.assertFalse(path.exists())
        self.assertNotIn("task/rt-fs", self._git("branch", "--list", "task/rt-fs"))
        self.assertIsNone(dispatch.fsmonitor_daemon_pid(path))
        self.assertEqual(dispatch.fsmonitor_daemon_pid(self.repo), repo_pid)

    @unittest.skipIf(os.name == "nt", "fsmonitor daemon discovery needs pgrep and lsof, which Windows lacks")
    def test_daemon_table_is_read_from_lsof_shapes_seen_on_this_mac(self) -> None:
        # Verbatim shapes from `lsof -Ftpn`: a worktree daemon holds its
        # checkout root and its gitdir (each with ancestors, the repo root among them);
        # the repo's own daemon holds only the root plus its socket; a /tmp checkout's
        # socket is named by the unresolved path while its root is /private/tmp; a
        # daemon whose socket was unlinked reports a relative socket name.
        lsof = "\n".join([
            "p57990", "fDIR", "tDIR", "n/home/a/switchboard/.amp/worktrees/rt-7e04", "n/home/a/switchboard/.amp/worktrees",
            "n/home/a/switchboard/.amp", "n/home/a/switchboard", "n/home/a", "n/Users",
            "n/home/a/switchboard/.git/worktrees/rt-7e04", "n/home/a/switchboard/.git/worktrees", "n/home/a/switchboard/.git",
            "n/home/a/switchboard", "n/home/a", "n/Users",
            "tunix", "n->0xfbe2824c5e948f26", "n/home/a/switchboard/.git/worktrees/rt-7e04/fsmonitor--daemon.ipc",
            "p60038", "tDIR", "n/home/a/switchboard", "n/home/a", "n/Users", "tunix", "n/home/a/switchboard/.git/fsmonitor--daemon.ipc",
            "p82561", "tDIR", "n/private/tmp/kit-home/switchboard", "n/private/tmp/kit-home", "n/private/tmp", "n/private",
            "tunix", "n/tmp/kit-home/switchboard/.git/fsmonitor--daemon.ipc",
            "p21328", "tDIR", "n/private/var/T/kit-real-9v3/home/switchboard", "n/private/var/T/kit-real-9v3/home",
            "n/private/var/T", "tunix", "nfsmonitor--daemon.ipc", ""])

        def canned(cmd, **kw):
            if cmd[0] == "pgrep":
                return Mock(stdout="57990\n60038\n82561\n21328\n", returncode=0)
            self.assertEqual(cmd[:3], ["lsof", "-a", "-p"])
            self.assertEqual(sorted(cmd[3].split(",")), ["21328", "57990", "60038", "82561"])
            return Mock(stdout=lsof, returncode=0)

        with patch.object(dispatch, "run", canned):
            table = dispatch.fsmonitor_daemons()
        self.assertEqual(table, {
            57990: {"root": "/home/a/switchboard/.amp/worktrees/rt-7e04", "gitdir": "/home/a/switchboard/.git/worktrees/rt-7e04"},
            60038: {"root": "/home/a/switchboard", "gitdir": "/home/a/switchboard/.git"},
            82561: {"root": "/tmp/kit-home/switchboard", "gitdir": "/tmp/kit-home/switchboard/.git"},
            21328: {"root": "/private/var/T/kit-real-9v3/home/switchboard", "gitdir": ""},
        })

    def test_daemon_lookup_matches_the_checkout_not_its_ancestors(self) -> None:
        table = {57990: {"root": str(self.repo / ".amp" / "worktrees" / "rt-7e04"),
                         "gitdir": str(self.repo / ".git" / "worktrees" / "rt-7e04")},
                 60038: {"root": str(self.repo), "gitdir": str(self.repo / ".git")},
                 11111: {"root": str(self.repo / ".amp" / "worktrees" / "gone"), "gitdir": ""}}
        wt = self.repo / ".amp" / "worktrees" / "rt-7e04"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {self.repo / '.git' / 'worktrees' / 'rt-7e04'}\n")
        with patch.object(dispatch, "fsmonitor_daemons", lambda: table):
            self.assertEqual(dispatch.fsmonitor_daemon_pid(wt), 57990)
            self.assertEqual(dispatch.fsmonitor_daemon_pid(self.repo), 60038)
            self.assertEqual(dispatch.fsmonitor_daemon_pid(self.repo / ".amp" / "worktrees" / "gone"), 11111)   # removed checkout, daemon left
            self.assertIsNone(dispatch.fsmonitor_daemon_pid(self.repo / ".amp" / "worktrees" / "never"))
            self.assertIsNone(dispatch.fsmonitor_daemon_pid(self.repo / ".amp"))   # an ancestor every daemon holds open

    def _add_nested_repo(self, rel: str = "knowledge/shared") -> Path:
        """The knowledge/shared shape: a populated nested repo recorded as a gitlink
        (mode 160000) with no .gitmodules, inheriting core.fsmonitor=true."""
        nested = self.repo / rel
        nested.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", cwd=nested)
        (nested / "block.md").write_text("nested\n")
        self._git("add", "--", "block.md", cwd=nested)
        self._git("commit", "-q", "-m", "nested", cwd=nested)
        self._git("add", "--", rel)   # records a gitlink, not the files
        self._git("commit", "-q", "-m", "gitlink")
        self.assertIn("160000 ", self._git("ls-files", "--stage", "--", rel))
        return nested

    @unittest.skipIf(os.name == "nt", "fsmonitor daemon discovery needs pgrep and lsof, which Windows lacks")
    def test_nested_repo_is_pinned_off_and_status_never_recurses_into_it(self) -> None:
        # `git status` in main runs `git status --porcelain=2` inside a nested repo, and
        # that child can hang on the nested repo's wedged daemon while holding main's
        # .git/index.lock. Wrong impls: pin only the
        # switchboard config (the nested repo still inherits true); keep recursing into
        # the nested repo (a wedged daemon there still blocks dirty_main_paths).
        nested = self._add_nested_repo()
        effective = lambda path: dispatch.run(["git", "-C", str(path), "config", "core.fsmonitor"]).stdout.strip()  # noqa: E731
        self.assertEqual(effective(nested), "true")
        self.assertEqual(dispatch.nested_repos(), [nested])

        # A nested daemon that answers nothing, while the nested repo still inherits
        # fsmonitor=true: a status in main that recursed
        # would block on it. Run the probes on a thread so a regression fails the
        # test instead of hanging the suite (tearDown kills the daemon, which frees
        # the stuck child).
        dispatch.run(["git", "-C", str(nested), "-c", "core.fsmonitor=true", "fsmonitor--daemon", "start"], check=True)
        pid = dispatch.fsmonitor_daemon_pid(nested)
        self.assertIsNotNone(pid)
        os.kill(pid, dispatch.signal.SIGSTOP)
        self.addCleanup(lambda: dispatch.pid_alive(pid) and os.kill(pid, dispatch.signal.SIGKILL))
        (nested / "block.md").write_text("dirty nested\n")
        (self.repo / "b.md").write_text("new\n")
        probe: dict[str, Any] = {}

        def probes() -> None:
            probe["dirty"] = dispatch.dirty_main_paths()
            probe["worktree"] = dispatch.worktree_dirty(self.repo)

        worker = threading.Thread(target=probes, daemon=True)
        worker.start()
        worker.join(timeout=10.0)
        self.assertFalse(worker.is_alive(), "git status in main waited on the nested repo's daemon")
        self.assertIn("b.md", probe["dirty"])
        self.assertNotIn("knowledge/shared", probe["dirty"])
        self.assertEqual([l[3:] for l in probe["worktree"]], ["b.md"])
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

        dispatch.ensure_fsmonitor_off()
        self.assertEqual(effective(nested), "false")
        self.assertEqual(self._git("config", "--local", "diff.ignoreSubmodules").strip(), "all")

    def test_nested_repos_skips_unpopulated_gitlinks(self) -> None:
        # In a linked worktree the gitlink directory exists but is empty (no .git):
        # nothing to pin there, and the walk must not invent a repo.
        self._add_nested_repo()
        path, _ = dispatch.create_worktree("rt-fs")
        self.assertTrue((path / "knowledge" / "shared").is_dir())
        self.assertEqual(dispatch.nested_repos(path), [])


class AutoPushGateTests(unittest.TestCase):
    """Hourly push of main. A bare `origin`, a clone
    as the main checkout, and a second clone standing in for someone else's
    push. CoS lines and digest rows are captured, never sent. Each test names
    the wrong implementation it catches."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        dispatch.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)])
        self.repo = root / "repo"
        dispatch.run(["git", "clone", "-q", str(self.origin), str(self.repo)])
        self._git("checkout", "-q", "-b", "main")
        (self.repo / "a.md").write_text("base\n")
        (self.repo / ".gitignore").write_text(".amp/*\n/people\n/system/state/**\n!/system/state/README.md\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        self._git("push", "-q", "-u", "origin", "main")
        self.other = root / "other"
        dispatch.run(["git", "clone", "-q", str(self.origin), str(self.other)])
        self.alerts: list[tuple[str, str]] = []
        self.digest: list[str] = []
        self.pushes: list[tuple[str, ...]] = []
        real_git = dispatch.git

        def recording_git(*args, cwd=None, check=True, timeout=None):
            if args and args[0] == "push":
                self.pushes.append(args)
            return real_git(*args, cwd=cwd, check=check, timeout=timeout)

        self.patches = [patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "LOCKS", self.repo / ".amp" / "locks"),
                        patch.object(dispatch, "AUTO_PUSH_STAMP", root / "state" / "auto-push.json"),
                        patch.object(dispatch, "AUTO_PUSH_LOG", root / "state" / "logs" / "auto-push.log"),
                        patch.object(dispatch, "git", recording_git),
                        patch.object(dispatch, "auto_push_alert", lambda text, status: self.alerts.append((status, text)) or True),
                        patch.object(dispatch, "auto_push_digest_line", lambda text: self.digest.append(text) or True)]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return dispatch.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]).stdout

    def commit(self, name: str, cwd: Path | None = None) -> None:
        with ((cwd or self.repo) / "a.md").open("a") as fh:
            fh.write(f"{name}\n")
        self._git("commit", "-q", "-am", name, cwd=cwd)

    def origin_main(self) -> str:
        return dispatch.run(["git", "-C", str(self.origin), "rev-parse", "main"]).stdout.strip()

    def log_text(self) -> str:
        return dispatch.AUTO_PUSH_LOG.read_text() if dispatch.AUTO_PUSH_LOG.exists() else ""

    def test_ahead_pushes_main_only_without_force_and_queues_one_digest_line(self) -> None:
        self.commit("one")
        self.commit("two")
        self._git("branch", "task/rt-x")   # a task branch must never ride along
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "pushed", outcome)
        self.assertEqual(outcome["ahead"], 2)
        self.assertEqual(self.origin_main(), self._git("rev-parse", "main").strip())
        self.assertEqual(self.pushes, [("push", "origin", "main:main")])   # wrong impl: `push --force`, `push --all`, or bare `push`
        self.assertEqual(dispatch.run(["git", "-C", str(self.origin), "branch", "--list"]).stdout.split(), ["*", "main"])
        self.assertIn("pushed 2 commit(s)", self.log_text())
        self.assertEqual(self.digest, [f"pushed 2 commits to origin/main ({outcome['origin_main_before']}..{outcome['main']})"])
        self.assertEqual(self.alerts, [])   # success is silent on the CoS composer
        stamp = dispatch.auto_push_stamp()
        self.assertTrue(stamp["last_attempt_at"] and stamp["last_push_at"] == stamp["last_attempt_at"])
        self.assertEqual(stamp["last_pushed_commit"], outcome["main"])

    def test_no_origin_remote_skips_quietly(self) -> None:
        # No `origin` remote: a tick must not log fetch-failed. Wrong impls: fetch anyway, log a line, alert, or add a remote.
        self._git("remote", "remove", "origin")
        self.commit("local only")
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "no-remote", outcome)
        self.assertEqual((self.pushes, self.alerts, self.digest), ([], [], []))
        self.assertEqual(self.log_text(), "", "no auto-push.log line for a missing remote")
        self.assertEqual(self._git("remote").strip(), "", "the gate never adds a remote")
        self.assertEqual(dispatch.auto_push_tick()["action"], "too-soon", "checked at most once an hour")

    def test_not_ahead_pushes_nothing_and_does_not_count_as_an_attempt(self) -> None:
        # Wrong impl: stamp last_attempt_at on every tick, so a commit landing a
        # minute later waits an hour.
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "up-to-date", outcome)
        self.assertEqual(self.pushes, [])
        self.assertNotIn("last_attempt_at", dispatch.auto_push_stamp())
        self.assertIn("last_checked_at", dispatch.auto_push_stamp())
        self.assertEqual((self.alerts, self.digest), ([], []))
        self.commit("later")
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")

    def test_diverged_is_logged_and_reported_never_pushed(self) -> None:
        # Wrong impl: push anyway (rejected non-fast-forward) or push --force.
        self.commit("theirs", cwd=self.other)
        self._git("push", "-q", "origin", "main", cwd=self.other)
        remote_before = self.origin_main()
        self.commit("ours")
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "diverged", outcome)
        self.assertEqual((outcome["ahead"], outcome["behind"]), (1, 1))
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.origin_main(), remote_before)
        self.assertEqual(len(self.alerts), 1)
        status, text = self.alerts[0]
        self.assertEqual(status, "blocked")
        self.assertIn("diverged origin/main (ahead 1, behind 1", text)
        self.assertIn("diverged ahead=1 behind=1", self.log_text())
        self.assertIn("last_attempt_at", dispatch.auto_push_stamp())   # the line repeats at most once per interval
        self.assertEqual(dispatch.auto_push_tick()["action"], "too-soon")

    def test_behind_only_is_reported_not_pushed(self) -> None:
        self.commit("theirs", cwd=self.other)
        self._git("push", "-q", "origin", "main", cwd=self.other)
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "behind", outcome)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.alerts[0][0], "blocked")
        self.assertIn("behind origin/main (ahead 0, behind 1", self.alerts[0][1])

    def test_landing_lock_held_skips_the_tick_without_touching_git_or_the_stamp(self) -> None:
        # Wrong impl: push while dispatch.py finish is mid rebase/fast-forward.
        self.commit("one")
        with dispatch.landing_lock():
            outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome, {"action": "landing"})
        self.assertEqual(self.pushes, [])
        self.assertFalse(dispatch.AUTO_PUSH_STAMP.exists())
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")   # free again: pushed on the next tick

    def test_rebase_in_progress_in_main_or_a_worktree_skips_the_tick(self) -> None:
        self.commit("one")
        marker = self.repo / ".git" / "rebase-merge"
        marker.mkdir()
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "rebase", outcome)
        self.assertEqual(outcome["paths"], [str(marker)])
        marker.rmdir()
        wt_marker = self.repo / ".git" / "worktrees" / "rt-x" / "rebase-apply"
        wt_marker.mkdir(parents=True)
        self.assertEqual(dispatch.auto_push_tick()["paths"], [str(wt_marker)])
        wt_marker.rmdir()
        self.assertEqual(self.pushes, [])
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")

    def test_under_the_interval_since_the_last_attempt_waits(self) -> None:
        self.commit("one")
        recent = (datetime.now(timezone.utc) - timedelta(minutes=59)).isoformat(timespec="seconds").replace("+00:00", "Z")
        dispatch.write_auto_push_stamp({"last_attempt_at": recent})
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "too-soon", outcome)
        self.assertEqual(self.pushes, [])
        old = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(timespec="seconds").replace("+00:00", "Z")
        dispatch.write_auto_push_stamp({"last_attempt_at": old})
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")

    def test_tracked_sensitive_path_refuses_the_push_and_tells_cos(self) -> None:
        # Wrong impl: push a commit that tracks people/<profile> (the 8b3230df shape).
        (self.repo / "people").mkdir()
        (self.repo / "people" / "alice.md").write_text("# Alice\n")
        (self.repo / "system" / "state").mkdir(parents=True)
        (self.repo / "system" / "state" / "README.md").write_text("# State\n")   # the .gitignore exception: allowed
        self._git("add", "-f", "--", "people/alice.md", "system/state/README.md")
        self._git("commit", "-q", "-m", "oops")
        remote_before = self.origin_main()
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "sensitive", outcome)
        self.assertEqual(outcome["paths"], ["people/alice.md"])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.origin_main(), remote_before)
        self.assertEqual(self.alerts[0][0], "blocked")
        self.assertIn("people/alice.md", self.alerts[0][1])
        self.assertIn("sensitive-tracked count=1", self.log_text())

    def test_tracked_todo_files_refuse_the_push(self) -> None:
        # Wrong impl: an allow-list that lets the to-do list, its archive or its backups go up.
        # They hold real personal tasks and .gitignore says they live only on this machine.
        for name in ("todo.json", "todo-archive/2026-11.json", "todo-backups/a/todo.json"):
            path = self.repo / "system" / "state" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        self._git("add", "-f", "--", "system/state")
        self._git("commit", "-q", "-m", "oops")
        remote_before = self.origin_main()
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "sensitive", outcome)
        self.assertEqual(len(outcome["paths"]), 3, outcome)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.origin_main(), remote_before)

    def test_stalled_fetch_fails_and_releases_the_landing_lock(self) -> None:
        # Wrong impl: fetch with no timeout, holding the lock every landing waits on.
        self.commit("next")
        real_run = dispatch.run

        def stalling_run(cmd, **kw):
            if "fetch" in cmd:
                self.assertEqual(kw.get("timeout"), dispatch.AUTO_PUSH_GIT_TIMEOUT)
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", stalling_run):
            outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "fetch-failed", outcome)
        self.assertIn("timed out", outcome["reason"])
        self.assertEqual(self.pushes, [])
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")   # not an attempt: the next tick pushes

    def test_stalled_push_is_reported_as_failed(self) -> None:
        self.commit("next")
        real_run = dispatch.run

        def stalling_run(cmd, **kw):
            if "push" in cmd:
                self.assertEqual(kw.get("timeout"), dispatch.AUTO_PUSH_GIT_TIMEOUT)
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", stalling_run):
            outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "failed", outcome)
        self.assertEqual(outcome["rc"], 124)
        self.assertEqual(self.alerts[0][0], "failed")

    def test_the_command_never_lets_git_prompt_for_credentials(self) -> None:
        # Wrong impl: unattended tick opens Git Credential Manager's window and waits forever.
        seen: dict[str, str | None] = {}
        with patch.dict(os.environ, {"SWITCHBOARD_AUTO_PUSH": "on"}), patch.object(
                dispatch, "auto_push_tick",
                lambda: seen.update(prompt=os.environ.get("GIT_TERMINAL_PROMPT"), gcm=os.environ.get("GCM_INTERACTIVE")) or {"action": "up-to-date"}), \
                patch("builtins.print"):
            self.assertEqual(dispatch.command_auto_push(argparse.Namespace()), 0)
        self.assertEqual(seen, {"prompt": "0", "gcm": "never"})

    def test_the_command_is_off_unless_turned_on(self) -> None:
        # Wrong impl: switchboard stays local, yet a tick still fetches and pushes main.
        called: list[bool] = []
        env = {k: v for k, v in os.environ.items() if k != "SWITCHBOARD_AUTO_PUSH"}
        for value in (None, "off", "1", "yes"):
            if value is not None:
                env["SWITCHBOARD_AUTO_PUSH"] = value
            with patch.dict(os.environ, env, clear=True), patch.object(
                    dispatch, "auto_push_tick", lambda: called.append(True) or {"action": "pushed"}),                     patch("builtins.print") as printed:
                self.assertEqual(dispatch.command_auto_push(argparse.Namespace()), 0)
            self.assertIn('"disabled"', printed.call_args[0][0])
        self.assertEqual(called, [], "auto-push ran its gate while SWITCHBOARD_AUTO_PUSH was not on")

    def test_the_real_repo_tracks_no_sensitive_path(self) -> None:
        # The key assertion: people/, notes/sensitive, notes/chrome-tabs,
        # .state and system/state (beyond .gitignore's exceptions) are not tracked in
        # this checkout, so the hourly push cannot carry them to GitHub.
        with patch.object(dispatch, "REPO_ROOT", REPO_ROOT):
            self.assertEqual(dispatch.tracked_sensitive_paths(), [])
            allowed = dispatch.run(["git", "-C", str(REPO_ROOT), "ls-files", "--", "system/state"]).stdout.split()
        self.assertTrue(allowed, "system/state keeps its README tracked on purpose; an empty list means the fixture is wrong")
        self.assertTrue(all(dispatch.SENSITIVE_TRACKED_ALLOWED.match(p) for p in allowed), allowed)

    def test_digest_only_row_keeps_its_delivery_policy_on_the_ledger(self) -> None:
        # The success line goes to the next-up Updates section only. The loop's retry
        # pass calls back with --delivery auto; without the stored policy an
        # after-hours retry would paste the line into the CoS composer.
        ledger_file = Path(self.tmp.name) / "ledger.json"
        env = {**os.environ, "RUNNING_TASKS_FILE": str(ledger_file)}
        dispatch.run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", "auto-push-test",
                      "--origin", "cos", "--status", "done", "--delivery", "digest", "--create-if-missing",
                      "--summary", "pushed 2 commits to origin/main (a..b)"], env=env)
        row = json.loads(ledger_file.read_text())["tasks"][0]
        self.assertEqual((row["delivery"], row["status"], row["report_delivered"]), ("digest", "done", False))


class WorkerCapTests(unittest.TestCase):
    """No worker cap: max_workers 0
    or absent is unlimited at both call sites; a positive cap still queues."""

    def setUp(self) -> None:
        self.live = [{"id": f"rt-live{i}", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance",
                      "prompt": "busy", "attempt": 1, "attempt_id": f"rt-live{i}.a1.aaa", "worker_id": "worker-aaaaaaaaaaaa",
                      "thread_id": f"T-live{i}", "created_at": iso(datetime.now(timezone.utc) - timedelta(minutes=i + 1)),
                      "heartbeat_at": ledger.now_iso(), "updated_at": ledger.now_iso()} for i in range(12)]
        self.launched: list[str] = []
        for p in [patch.object(ledger, "live_agents", return_value={t["thread_id"]: {"state": "Idle"} for t in self.live}),
                  patch.object(dispatch, "cmux_workspaces", return_value=[]),
                  patch.object(dispatch, "launch", side_effect=self.fake_launch),
                  patch.object(dispatch, "reap_all", return_value={"closed": [], "skipped": []}),
                  patch.object(dispatch, "notify", lambda *a, **k: {"delivered": True})]:
            p.start()
            self.addCleanup(p.stop)

    def fake_launch(self, task: dict, cfg: dict) -> dict:
        self.launched.append(task["id"])

        def bind(payload):
            row = ledger.find_task(payload, task["id"])
            row.update(workspace_ref="workspace:999", worktree=str(TMP / "wt"), branch="task/x", worker_id="worker-bbbbbbbbbbbb",
                       status="routing", summary="started", updated_at=ledger.now_iso())
            return row
        return ledger.locked_mutation(bind)

    def test_worker_cap_reads_zero_absent_and_junk_as_unlimited(self) -> None:
        for cfg in ({"max_workers": 0}, {}, {"max_workers": None}, {"max_workers": "nope"}, {"max_workers": -2}):
            self.assertIsNone(dispatch.worker_cap(cfg), cfg)
            self.assertFalse(dispatch.at_capacity(500, cfg), cfg)
        self.assertEqual(dispatch.worker_cap({"max_workers": "6"}), 6)   # router.yaml may quote it
        self.assertTrue(dispatch.at_capacity(6, {"max_workers": 6}))
        self.assertFalse(dispatch.at_capacity(5, {"max_workers": 6}))
        self.assertIsNone(dispatch.worker_cap(dispatch.DEFAULTS), "the shipped default is no cap")

    def test_dispatch_with_no_cap_launches_past_twelve_live_workers(self) -> None:
        # Wrong implementation: int(cfg["max_workers"]) with 0 queues every task
        # behind the oldest worker forever.
        write_ledger(*self.live)
        with patch.object(dispatch, "config", return_value={**dispatch.DEFAULTS, "max_workers": 0}):
            code, result = run_dispatch(task_id="rt-new")
        self.assertEqual((code, result["status"]), (0, "routed"), result)
        self.assertEqual(self.launched, ["rt-new"])

    def test_dispatch_with_a_positive_cap_still_queues_behind_the_oldest_worker(self) -> None:
        write_ledger(*self.live)
        with patch.object(dispatch, "config", return_value={**dispatch.DEFAULTS, "max_workers": 6}):
            code, result = run_dispatch(task_id="rt-new")
        self.assertEqual((code, result["status"]), (0, "queued"), result)
        self.assertIn("12 workers are already running (dispatch.max_workers 6)", result["reason"])
        # The oldest by created_at, not the first in the file; named by its workstream
        # with the id after it (names, never bare numbers).
        self.assertIn(f"waiting on {dispatch.owner_title('switchboard-maintenance')} (rt-live11)", result["reason"])
        self.assertNotIn("waiting on rt-live11", result["reason"])
        self.assertTrue(result["message"].startswith(f"[{dispatch.owner_title('switchboard-maintenance')} · rt-new]"), result["message"])
        self.assertEqual(self.launched, [])
        self.assertEqual(ledger.find_task(ledger.load_payload(), "rt-new")["status"], "queued")

    def test_release_holds_on_capacity_only_with_a_positive_cap(self) -> None:
        queued = {"id": "rt-q", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "later",
                  "attempt": 1, "attempt_id": "rt-q.a1.aaa", "created_at": ledger.now_iso(), "updated_at": ledger.now_iso()}
        write_ledger(*self.live, queued)
        out = io.StringIO()
        with redirect_stdout(out):
            dispatch.release_queued({**dispatch.DEFAULTS, "max_workers": 6})
        self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1]),
                         {"started": [], "held": [{"task_id": "rt-q", "waiting_on": "capacity"}]})
        out = io.StringIO()
        with redirect_stdout(out):
            dispatch.release_queued({**dispatch.DEFAULTS, "max_workers": 0})
        self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1])["started"], ["rt-q"])
        self.assertEqual(self.launched, ["rt-q"])


class QueueOrderTests(unittest.TestCase):
    """Queue fairness: a brand-new dispatch must not take a freed slot while rows
    queued on capacity for hours keep waiting. A free slot goes to the eligible queued row that ranks first; owner asks rank before switchboard-maintenance rows until a maintenance row has waited
    queue_maintenance_aging_minutes."""

    CAP = {**dispatch.DEFAULTS, "max_workers": 3}

    def setUp(self) -> None:
        self.at = datetime.now(timezone.utc)
        # Two live workers under a cap of 3: one slot is free.
        self.live = [{"id": f"rt-live{i}", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance",
                      "prompt": "busy", "attempt": 1, "attempt_id": f"rt-live{i}.a1.aaa", "worker_id": "worker-aaaaaaaaaaaa",
                      "thread_id": f"T-live{i}", "created_at": iso(self.at - timedelta(hours=4)),
                      "heartbeat_at": ledger.now_iso(), "updated_at": ledger.now_iso()} for i in range(2)]
        self.launched: list[str] = []
        for p in [patch.object(ledger, "live_agents", return_value={t["thread_id"]: {"state": "Idle"} for t in self.live}),
                  patch.object(dispatch, "cmux_workspaces", return_value=[]),
                  patch.object(dispatch, "owner_tab_eligible", return_value=False),
                  patch.object(dispatch, "usage_limit_until", return_value=None),
                  patch.object(dispatch, "usage_limit_stamp", return_value={}),
                  patch.object(dispatch, "commit_hold_reason", return_value=""),
                  patch.object(dispatch, "launch", side_effect=self.fake_launch),
                  patch.object(dispatch, "reap_all", return_value={"closed": [], "skipped": []}),
                  patch.object(dispatch, "notify", lambda *a, **k: {"delivered": True})]:
            p.start()
            self.addCleanup(p.stop)

    def fake_launch(self, task: dict, cfg: dict) -> dict:
        self.launched.append(task["id"])

        def bind(payload):
            row = ledger.find_task(payload, task["id"])
            row.update(workspace_ref="workspace:999", worktree=str(TMP / "wt"), branch="task/x", worker_id="worker-bbbbbbbbbbbb",
                       status="routing", summary="started", updated_at=ledger.now_iso())
            return row
        return ledger.locked_mutation(bind)

    def queued(self, task_id: str, minutes_ago: int, workstream: str = "switchboard-maintenance", **extra) -> dict:
        return {"id": task_id, "status": "queued", "origin": "cos", "workstream": workstream, "prompt": "later",
                "attempt": 1, "attempt_id": f"{task_id}.a1.aaa", "summary": "waiting on capacity",
                "created_at": iso(self.at - timedelta(minutes=minutes_ago)), "updated_at": ledger.now_iso(), **extra}

    def dispatch_fresh(self, cfg: dict | None = None, **overrides) -> tuple[int, dict]:
        with patch.object(dispatch, "config", return_value=cfg or self.CAP):
            return run_dispatch(task_id="rt-fresh", **overrides)

    def release(self, cfg: dict | None = None) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            dispatch.release_queued(cfg or self.CAP)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def status(self, task_id: str) -> str:
        return ledger.find_task(ledger.load_payload(), task_id)["status"]

    def test_fresh_dispatch_does_not_jump_an_older_eligible_queued_row(self) -> None:
        # Wrong impl: launches rt-fresh into the free slot.
        write_ledger(*self.live, self.queued("rt-terminal", 173))
        code, result = self.dispatch_fresh()
        self.assertEqual((code, result["status"]), (0, "queued"), result)
        self.assertEqual(result["queued_behind"], ["rt-terminal"])
        self.assertIn("because it was queued first and takes the free slot", result["reason"])
        self.assertIn("(rt-terminal)", result["reason"])
        # The release ran at once: the older row took the slot, the fresh one waits for the next.
        self.assertEqual(result["release"], "inline")
        self.assertEqual(self.launched, ["rt-terminal"])
        self.assertEqual((self.status("rt-terminal"), self.status("rt-fresh")), ("routing", "queued"))

    def test_dependency_held_queued_row_does_not_block_a_fresh_dispatch(self) -> None:
        write_ledger(*self.live, self.queued("rt-dependent", 173, depends_on=["rt-live0"]))
        code, result = self.dispatch_fresh()
        self.assertEqual((code, result["status"]), (0, "routed"), result)
        self.assertEqual(self.launched, ["rt-fresh"])
        self.assertEqual(self.status("rt-dependent"), "queued")

    def test_continuation_owned_and_quiet_window_rows_do_not_count_as_ahead(self) -> None:
        heir = {**self.live[1], "continuation_of": "rt-owned", "created_at": iso(self.at - timedelta(minutes=100))}
        write_ledger(self.live[0], heir, self.queued("rt-owned", 173), self.queued("rt-quiet-held", 150))
        fresh = {"id": "rt-fresh", "workstream": "switchboard-maintenance", "created_at": iso(self.at)}
        with patch.object(dispatch, "usage_limit_stamp", return_value={"quiet_waiting": ["rt-history-rewrite"]}):
            self.assertEqual(dispatch.queued_ahead(fresh, ledger.load_payload(), self.CAP), [])
        # Without the quiet window the plain queued row is ahead; the continuation-owned one never is.
        self.assertEqual([t["id"] for t in dispatch.queued_ahead(fresh, ledger.load_payload(), self.CAP)], ["rt-quiet-held"])

    def test_owner_ask_ranks_before_a_young_maintenance_row(self) -> None:
        write_ledger(*self.live, self.queued("rt-trash-fix", 10))
        code, result = self.dispatch_fresh(workstream="reading-list")
        self.assertEqual((code, result["status"]), (0, "routed"), result)
        self.assertEqual(self.launched, ["rt-fresh"])

    def test_maintenance_row_past_the_aging_window_ranks_with_owner_asks(self) -> None:
        write_ledger(*self.live, self.queued("rt-terminal", 31))
        code, result = self.dispatch_fresh(workstream="reading-list")
        self.assertEqual((code, result["status"]), (0, "queued"), result)
        self.assertEqual(self.launched, ["rt-terminal"])

    def test_fresh_maintenance_dispatch_waits_behind_a_queued_owner_ask(self) -> None:
        write_ledger(*self.live, self.queued("rt-sheet", 2, workstream="reading-list"))
        code, result = self.dispatch_fresh()
        self.assertEqual((code, result["status"]), (0, "queued"), result)
        self.assertEqual(self.launched, ["rt-sheet"])

    def test_release_fills_the_slot_in_rank_order_not_file_order(self) -> None:
        # File order puts the newest first; requeued rows land anywhere in the file.
        write_ledger(*self.live, self.queued("rt-young-fix", 5), self.queued("rt-old-fix", 20),
                     self.queued("rt-sheet", 3, workstream="reading-list"))
        result = self.release()
        self.assertEqual(result["started"], ["rt-sheet"])
        self.assertEqual([h["task_id"] for h in result["held"]], ["rt-old-fix", "rt-young-fix"])
        self.assertTrue(all(h["waiting_on"] == "capacity" for h in result["held"]))

    def test_strict_fifo_when_owner_first_is_off(self) -> None:
        write_ledger(*self.live, self.queued("rt-young-fix", 5), self.queued("rt-sheet", 3, workstream="reading-list"))
        result = self.release({**self.CAP, "queue_owner_first": False})
        self.assertEqual(result["started"], ["rt-young-fix"])

    def test_no_cap_means_no_line_to_respect(self) -> None:
        write_ledger(*self.live, self.queued("rt-memory-held", 60))
        code, result = self.dispatch_fresh(cfg={**dispatch.DEFAULTS, "max_workers": 0})
        self.assertEqual((code, result["status"]), (0, "routed"), result)
        self.assertEqual(self.launched, ["rt-fresh"])

    def test_queue_rank_classes(self) -> None:
        cfg = dispatch.DEFAULTS
        owner = {"workstream": "reading-list", "created_at": iso(self.at)}
        young_fix = {"workstream": "switchboard-maintenance", "created_at": iso(self.at - timedelta(minutes=29))}
        aged_fix = {"workstream": "switchboard-maintenance", "created_at": iso(self.at - timedelta(minutes=30))}
        self.assertEqual(dispatch.queue_rank(owner, cfg, self.at)[0], 0)
        self.assertEqual(dispatch.queue_rank(young_fix, cfg, self.at)[0], 1)
        self.assertEqual(dispatch.queue_rank(aged_fix, cfg, self.at)[0], 0)
        self.assertLess(dispatch.queue_rank(aged_fix, cfg, self.at), dispatch.queue_rank(owner, cfg, self.at))
        never_ages = {**cfg, "queue_maintenance_aging_minutes": 0}
        self.assertEqual(dispatch.queue_rank(aged_fix, never_ages, self.at)[0], 1)
        self.assertEqual(dispatch.queue_rank({"workstream": "switchboard-maintenance"}, cfg, self.at), (1, float("inf")))


class DatedLaunchHoldTests(unittest.TestCase):
    """Dated launch holds (for example: hold new switchboard-maintenance launches until a
    set time so memory stays free for a reviewer demo). A hold names workstreams or
    task ids and an absolute `until`; it lifts by the clock alone, so it can never get stuck."""

    # The queue fixture, borrowed without re-running QueueOrderTests' own tests.
    CAP = QueueOrderTests.CAP
    setUp, fake_launch, queued = QueueOrderTests.setUp, QueueOrderTests.fake_launch, QueueOrderTests.queued
    dispatch_fresh, release, status = QueueOrderTests.dispatch_fresh, QueueOrderTests.release, QueueOrderTests.status

    def held_cfg(self, minutes_ahead: int = 60, **hold) -> dict:
        entry = {"workstreams": ["switchboard-maintenance"], "until": iso(self.at + timedelta(minutes=minutes_ahead)),
                 "reason": "memory stays free for the reviewer demo", **hold}
        return {**self.CAP, "launch_holds": [entry]}

    def test_fresh_maintenance_dispatch_queues_with_the_hold_time_and_owner_asks_launch(self) -> None:
        write_ledger(*self.live)
        cfg = self.held_cfg()
        code, result = self.dispatch_fresh(cfg)
        self.assertEqual((code, result["status"]), (0, "queued"), result)
        self.assertIn("held until", result["reason"])
        self.assertIn("because memory stays free for the reviewer demo; it starts on its own", result["reason"])
        self.assertEqual(self.launched, [])
        with patch.object(dispatch, "config", return_value=cfg):
            code, result = run_dispatch(task_id="rt-owner", workstream="reading-list")
        self.assertEqual((code, result["status"]), (0, "routed"), result)
        self.assertEqual(self.launched, ["rt-owner"])

    def test_held_row_never_blocks_a_fresh_dispatch_and_release_skips_it(self) -> None:
        write_ledger(*self.live, self.queued("rt-held-fix", 173))
        cfg = self.held_cfg()
        result = self.release(cfg)
        self.assertEqual(result["started"], [])
        self.assertEqual(result["held"][0]["task_id"], "rt-held-fix")
        self.assertTrue(result["held"][0]["waiting_on"].startswith("held until"), result)
        fresh = {"id": "rt-fresh", "workstream": "reading-list", "created_at": iso(self.at)}
        self.assertEqual(dispatch.queued_ahead(fresh, ledger.load_payload(), cfg), [])

    def test_the_hold_lifts_by_itself_when_until_passes(self) -> None:
        write_ledger(*self.live, self.queued("rt-held-fix", 173))
        expired = self.held_cfg(minutes_ahead=-1)
        self.assertIsNone(dispatch.launch_hold(self.queued("rt-held-fix", 173), expired))
        self.assertEqual(self.release(expired)["started"], ["rt-held-fix"])
        # The same config read an hour later: `at` past `until` matches nothing.
        cfg = self.held_cfg(minutes_ahead=60)
        later = self.at + timedelta(minutes=61)
        self.assertIsNotNone(dispatch.launch_hold(self.queued("rt-x", 1), cfg, self.at))
        self.assertIsNone(dispatch.launch_hold(self.queued("rt-x", 1), cfg, later))

    def test_a_relaunched_worker_finishes_and_task_ids_match(self) -> None:
        cfg = self.held_cfg()
        self.assertIsNone(dispatch.launch_hold(self.queued("rt-relaunch", 30, attempt=2), cfg))
        by_id = self.held_cfg(workstreams=[], tasks=["rt-cleanup"])
        self.assertIsNotNone(dispatch.launch_hold(self.queued("rt-cleanup", 5, workstream="todo-cleanup"), by_id))
        self.assertIsNone(dispatch.launch_hold(self.queued("rt-other", 5, workstream="todo-cleanup"), by_id))

    def test_typos_fail_open(self) -> None:
        row = self.queued("rt-held-fix", 5)
        far = self.held_cfg(minutes_ahead=60 * 25)     # past launch_hold_max_hours (24)
        naive = {**self.CAP, "launch_holds": [{"workstreams": ["switchboard-maintenance"], "until": "2026-11-13T14:00:00"}]}
        junk = {**self.CAP, "launch_holds": [{"workstreams": ["switchboard-maintenance"], "until": "two pm"}, "not a dict"]}
        for cfg in (far, naive, junk, {**self.CAP, "launch_holds": None}, dispatch.DEFAULTS):
            self.assertIsNone(dispatch.launch_hold(row, cfg), cfg.get("launch_holds"))

    def test_shipped_router_yaml_holds_are_dated_and_zoned(self) -> None:
        import yaml  # type: ignore
        data = yaml.safe_load((Path(dispatch.__file__).resolve().parents[1] / "config" / "router.yaml").read_text(encoding="utf-8"))
        for hold in (data.get("dispatch") or {}).get("launch_holds") or []:
            until = hold["until"] if isinstance(hold["until"], datetime) else datetime.fromisoformat(str(hold["until"]))
            self.assertIsNotNone(until.tzinfo, hold)
            self.assertTrue(hold.get("workstreams") or hold.get("tasks"), hold)


class CapacityReportTests(unittest.TestCase):
    """Capacity is reported, never enforced: one digest line
    plus one urgent CoS line, at most hourly, while swap used is over the
    threshold or a launch-path cmux RPC timed out; the snapshot is written on
    every tick regardless. CoS lines are captured, never sent."""

    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                                    33026.\n"
               "Pages active:                                1034007.\n"
               "Pages inactive:                              1026613.\n"
               "Pages speculative:                              5841.\n"
               "Pages throttled:                                   0.\n"
               "Pages wired down:                             439335.\n"
               "Pages occupied by compressor:                 995056.\n")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alerts: list[tuple[str, str, str]] = []
        self.digest: list[tuple[str, str]] = []
        self.memory = {"free_ram_gb": 20.0, "compressor_gb": 15.2, "swap_used_gb": 1.0, "swap_total_gb": 17.0}
        self.real_memory_snapshot = dispatch.memory_snapshot
        write_ledger({"id": "rt-w", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "busy",
                      "attempt": 1, "attempt_id": "rt-w.a1.aaa", "worker_id": "worker-aaaaaaaaaaaa", "thread_id": "T-w",
                      "created_at": ledger.now_iso(), "heartbeat_at": ledger.now_iso(), "updated_at": ledger.now_iso()})
        for p in [patch.object(dispatch, "CAPACITY_STAMP", root / "capacity.json"),
                  patch.object(dispatch, "CMUX_TIMEOUT_LOG", root / "cmux-timeouts.jsonl"),
                  patch.object(dispatch, "memory_snapshot", lambda *a, **k: dict(self.memory)),
                  patch.object(ledger, "live_agents", return_value={"T-w": {"state": "Idle"}}),
                  patch.object(dispatch, "cos_alert", lambda prefix, text, status: self.alerts.append((prefix, status, text)) or True),
                  patch.object(dispatch, "cos_digest_line", lambda prefix, text: self.digest.append((prefix, text)) or True)]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_memory_snapshot_parses_vm_stat_and_swapusage_with_their_units(self) -> None:
        # Inactive pages are not counted as free (they would read as plenty of free
        # RAM while swap is high); the compressor line is parsed instead. Wrong
        # impls: keep inactive in free_ram_gb; miss the compressor line because its
        # label has spaces; report 0 instead of None when vm_stat is unreadable.
        parse = self.real_memory_snapshot   # the class patches the probe for the tick tests
        snap = parse(self.VM_STAT, "vm.swapusage: total = 17408.00M  used = 16455.25M  free = 952.75M  (encrypted)\n")
        self.assertAlmostEqual(snap["free_ram_gb"], (33026 + 5841) * 16384 / 1024 ** 3, places=3)
        self.assertAlmostEqual(snap["compressor_gb"], 995056 * 16384 / 1024 ** 3, places=3)
        self.assertAlmostEqual(snap["swap_used_gb"], 16455.25 / 1024, places=3)
        self.assertAlmostEqual(snap["swap_total_gb"], 17.0, places=3)
        mixed = parse(self.VM_STAT, "vm.swapusage: total = 2.00G  used = 512.00K  free = 2.00G\n")
        self.assertAlmostEqual(mixed["swap_total_gb"], 2.0, places=6)
        self.assertAlmostEqual(mixed["swap_used_gb"], 512 / 1024 ** 2, places=9)
        unreadable = parse("", "")
        self.assertEqual(unreadable, {"free_ram_gb": None, "compressor_gb": None, "swap_used_gb": None, "swap_total_gb": None})
        # An older vm_stat without the compressor line still yields free RAM.
        no_compressor = parse(self.VM_STAT.replace("Pages occupied by compressor:                 995056.\n", ""), "")
        self.assertIsNone(no_compressor["compressor_gb"])
        self.assertAlmostEqual(no_compressor["free_ram_gb"], (33026 + 5841) * 16384 / 1024 ** 3, places=3)

    def test_swap_probe_uses_an_absolute_sysctl_path(self) -> None:
        # The command-center launchd PATH has no /usr/sbin, so a bare "sysctl" raises
        # FileNotFoundError and leaves swap_used_gb null. Wrong impl: run(["sysctl", ...]).
        calls: list[list[str]] = []

        def fake_run(argv, timeout=None, **_):
            calls.append(list(argv))
            text = self.VM_STAT if argv[0] == "vm_stat" else "vm.swapusage: total = 15360.00M  used = 14213.69M  free = 1146.31M  (encrypted)\n"
            return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

        with patch.object(dispatch, "run", fake_run):
            snap = self.real_memory_snapshot()
        self.assertEqual(dispatch.SYSCTL, "/usr/sbin/sysctl")
        self.assertIn([dispatch.SYSCTL, "vm.swapusage"], calls)
        self.assertAlmostEqual(snap["swap_used_gb"], 14213.69 / 1024, places=3)
        self.assertEqual(dispatch.DEFAULTS["swap_alert_gb"], 16)

    def test_quiet_tick_writes_the_snapshot_and_sends_nothing(self) -> None:
        outcome = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((outcome["workers"], outcome["reasons"], outcome["reported"]), (1, [], False))
        self.assertEqual((self.alerts, self.digest), ([], []))
        stamp = json.loads(dispatch.CAPACITY_STAMP.read_text())
        self.assertEqual(stamp["swap_used_gb"], 1.0)
        self.assertIsNone(stamp["last_alert_at"])

    def test_swap_over_threshold_sends_one_digest_line_and_one_cos_line_then_waits_an_hour(self) -> None:
        self.memory["swap_used_gb"] = 16.1
        first = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertTrue(first["reported"])
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(len(self.digest), 1)
        prefix, status, text = self.alerts[0]
        # Swap holds no launch, so the line is informational (capacity-20261115-0716).
        self.assertEqual((prefix, status), ("capacity", "done"))
        self.assertIn("1 worker(s) live, 15.2 GB compressed, 16.1 GB swap used; swap used 16.1 GB > 8 GB", text)
        self.assertNotIn("RAM free", text)
        self.assertIn("Launches are not blocked", text)
        self.assertEqual(self.digest[0], ("capacity", text))
        # Ten minutes later, still swapping: rate-limited, snapshot still refreshed.
        self.memory["swap_used_gb"] = 16.4
        second = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((second["reported"], len(self.alerts), len(self.digest)), (False, 1, 1))
        self.assertEqual(json.loads(dispatch.CAPACITY_STAMP.read_text())["swap_used_gb"], 16.4)
        # An hour on, still the same swap trigger: one more digest line, but no
        # second direct CoS turn. A trigger that holds all day would otherwise cost
        # CoS an identical model turn every hour; the digest and capacity.json carry it.
        stamp = json.loads(dispatch.CAPACITY_STAMP.read_text())
        stamp["last_alert_at"] = iso(datetime.now(timezone.utc) - dispatch.CAPACITY_ALERT_INTERVAL - timedelta(seconds=1))
        dispatch.CAPACITY_STAMP.write_text(json.dumps(stamp))
        third = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((third["reported"], third["direct"], len(self.alerts), len(self.digest)), (True, False, 1, 2))

    def test_swap_that_clears_and_crosses_again_is_a_new_direct_alert(self) -> None:
        # Wrong impls: never alert again once swap was reported (a cleared-then-
        # returned trigger is news); or alert on every hourly digest line.
        self.memory["swap_used_gb"] = 16.1
        dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual(len(self.alerts), 1)
        self.memory["swap_used_gb"] = 2.0            # swap drained: quiet tick, stamp has no swap reason
        quiet = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((quiet["reported"], quiet["direct"]), (False, False))
        self.memory["swap_used_gb"] = 17.0           # crosses again inside the hour: digest waits, no alert yet
        self.assertEqual(dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})["reported"], False)
        self.assertEqual(len(self.alerts), 1)
        # An hour on: the clearing between alerts makes this re-crossing news, so
        # the direct line goes out again (the stamp remembers the clear tick even
        # though the latest tick already saw swap over the threshold).
        stamp = json.loads(dispatch.CAPACITY_STAMP.read_text())
        self.assertTrue(stamp["swap_cleared_since_alert"])
        stamp["last_alert_at"] = iso(datetime.now(timezone.utc) - dispatch.CAPACITY_ALERT_INTERVAL - timedelta(seconds=1))
        dispatch.CAPACITY_STAMP.write_text(json.dumps(stamp))
        again = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((again["reported"], again["direct"], len(self.alerts)), (True, True, 2))
        self.assertEqual(self.alerts[-1][1], "done")   # swap holds no launch (capacity-20261115-0716)
        self.assertFalse(json.loads(dispatch.CAPACITY_STAMP.read_text())["swap_cleared_since_alert"])

    def test_cmux_timeout_while_swap_persists_still_goes_direct(self) -> None:
        # A timeout is an event, not a standing condition: it earns the direct
        # line even when the swap trigger has been reported already.
        self.memory["swap_used_gb"] = 16.1
        dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        stamp = json.loads(dispatch.CAPACITY_STAMP.read_text())
        stamp["last_alert_at"] = iso(datetime.now(timezone.utc) - dispatch.CAPACITY_ALERT_INTERVAL - timedelta(seconds=1))
        dispatch.CAPACITY_STAMP.write_text(json.dumps(stamp))
        dispatch.note_cmux_timeout("rt-slow", "new-workspace", 60)
        outcome = dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})
        self.assertEqual((outcome["reported"], outcome["direct"], len(self.alerts)), (True, True, 2))
        self.assertEqual(self.alerts[-1][1], "done")   # swap holds no launch (capacity-20261115-0716)
        self.assertIn("new-workspace", self.alerts[-1][2])

    def test_exactly_at_threshold_or_threshold_zero_is_quiet(self) -> None:
        self.memory["swap_used_gb"] = 8.0
        self.assertFalse(dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 8})["reported"])
        self.memory["swap_used_gb"] = 30.0
        self.assertFalse(dispatch.capacity_tick({**dispatch.DEFAULTS, "swap_alert_gb": 0})["reported"])
        self.assertEqual(self.alerts, [])

    def test_launch_cmux_timeout_is_reported_once_as_settled_and_carried_through_a_quiet_window(self) -> None:
        dispatch.capacity_tick(dispatch.DEFAULTS)   # a quiet tick first, so "since" is set
        dispatch.note_cmux_timeout("rt-slow", "new-workspace", 60)
        outcome = dispatch.capacity_tick(dispatch.DEFAULTS)
        self.assertEqual((outcome["cmux_timeouts"], outcome["reported"]), (1, True))
        # A capacity line is a one-off observation, so its synthetic ledger row is
        # terminal at once. Status "active" would leave the row open and the digest
        # would repeat "0 worker(s) live … 1 cmux RPC timeout(s)" all day.
        self.assertEqual(self.alerts[-1][:2], ("capacity", "done"))
        self.assertIn("1 cmux RPC timeout(s) in launch (new-workspace)", self.alerts[-1][2])
        # Already reported: the next tick does not count it again.
        self.assertEqual(dispatch.capacity_tick(dispatch.DEFAULTS)["cmux_timeouts"], 0)
        # A timeout inside the hourly quiet window is held, not lost: it shows up in
        # the next line that does go out.
        dispatch.note_cmux_timeout("rt-slow2", "workspace.list", 10)
        held = dispatch.capacity_tick(dispatch.DEFAULTS)
        self.assertEqual((held["cmux_timeouts"], held["reported"]), (1, False))
        stamp = json.loads(dispatch.CAPACITY_STAMP.read_text())
        stamp["last_alert_at"] = iso(datetime.now(timezone.utc) - dispatch.CAPACITY_ALERT_INTERVAL - timedelta(seconds=1))
        dispatch.CAPACITY_STAMP.write_text(json.dumps(stamp))
        released = dispatch.capacity_tick(dispatch.DEFAULTS)
        self.assertEqual((released["cmux_timeouts"], released["reported"]), (1, True))
        self.assertIn("workspace.list", self.alerts[-1][2])

    def test_capacity_never_enters_the_launch_decision(self) -> None:
        # The point of the rule: release_queued and command_dispatch consult
        # at_capacity only, which ignores memory; a tick that is screaming about swap
        # changes nothing about whether a queued task starts.
        self.memory["swap_used_gb"] = 40.0
        dispatch.capacity_tick(dispatch.DEFAULTS)
        self.assertFalse(dispatch.at_capacity(50, dispatch.DEFAULTS))


class SteerTargetTests(unittest.TestCase):
    """A steer sent by native message can arrive after `reap` closed the worker's tab
    (row done, report delivered, grace elapsed) and never be acted on. `dispatch.py steer` is the one entry point: a terminal row becomes a
    --continuation-of dispatch, a non-terminal row keeps the native message."""

    PARENT = "rt-000000000049"

    def setUp(self) -> None:
        self.live_agents = patch.object(ledger, "live_agents", return_value={})
        self.live_agents.start()
        self.addCleanup(self.live_agents.stop)
        self.workspaces: list[dict] = []
        self.messages: list[tuple[str, str, str]] = []
        self.message_reply: dict = {"ok": True, "id": "msg-1", "state": "queued"}
        for p in [patch.object(dispatch, "launch", side_effect=self.fake_launch),
                  patch.object(dispatch, "workstream_exists", side_effect=lambda s: s == "example-training"),
                  patch.object(dispatch, "reap_all", return_value={"closed": [], "skipped": []}),
                  patch.object(dispatch, "cmux_workspaces", side_effect=lambda *a, **k: list(self.workspaces)),
                  patch.object(dispatch, "cmux_agent_message", side_effect=self.fake_message)]:
            p.start()
            self.addCleanup(p.stop)

    def fake_message(self, target: str, text: str, sender: str, timeout: float = 15.0) -> dict:
        self.messages.append((target, text, sender))
        return dict(self.message_reply)

    def fake_launch(self, task: dict, cfg: dict) -> dict:
        def bind(payload):
            row = ledger.find_task(payload, task["id"])
            row.update(workspace_ref="workspace:777", worktree=str(TMP / "wt-steer"), branch=f"task/{task['id']}",
                       worker_id="worker-bbbbbbbbbbbb", status="routing", summary="started", updated_at=ledger.now_iso())
            return row
        return ledger.locked_mutation(bind)

    def row(self, status: str, **extra: Any) -> dict:
        done = datetime.now(timezone.utc) - timedelta(minutes=20)
        base = {"id": self.PARENT, "status": status, "attempt": 1, "attempt_id": f"{self.PARENT}.a1.0a1b2c",
                "workstream": "example-training", "origin": "cos", "prompt": "build the overview deck",
                "thread_id": "T-example", "workspace_ref": "workspace:612",
                "created_at": iso(done - timedelta(hours=1)), "updated_at": iso(done), "finished_at": iso(done),
                "report_delivered": True, "reaped_at": iso(done + timedelta(minutes=6))}
        base.update(extra)
        return base

    def run_steer(self, text: str = "Also move the Help button to the top right.",
                  no_fallback_dispatch: bool = False) -> tuple[int, dict]:
        args = argparse.Namespace(prompt=[text], origin="cos", task_id=self.PARENT,
                                  no_fallback_dispatch=no_fallback_dispatch)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch.command_steer(args)
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_every_terminal_status_takes_a_continuation(self) -> None:
        # Wrong impl: only "done" is treated as reaped; a blocked/failed row's tab is reaped the same way.
        for status in sorted(ledger.TERMINAL_STATUSES):
            with self.subTest(status=status):
                self.assertEqual(dispatch.steer_decision(self.row(status), {})["action"], "continuation")
        for status in ("routing", "queued", "active"):
            with self.subTest(status=status):
                self.assertEqual(dispatch.steer_decision(self.row(status), {})["action"], "message")
            # Wrong impl: a non-terminal row with no thread bound is "message" with an empty
            # target (lost), "continuation" (retires the parent's asks), or a supplemental
            # worker (several workers on one deliverable).
            with self.subTest(status=status, thread=None):
                self.assertEqual(dispatch.steer_decision(self.row(status, thread_id=None), {})["action"], "queue")
        self.assertEqual(dispatch.steer_decision(None, {})["action"], "unknown-task")

    def test_terminal_row_dispatches_continuation_in_parent_workstream(self) -> None:
        # Wrong impl: the steer is printed as "message the thread" (lost) or the new row ranks the text
        # and lands in another workstream, or forgets continuation_of so the worker never reads the parent row.
        write_ledger(self.row("done"))
        code, result = self.run_steer()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["steer"]["action"], "continuation")
        self.assertEqual(result["continuation_of"], self.PARENT)
        self.assertEqual(result["workstream"], "example-training")
        self.assertEqual(result["owner"]["reason"], "continuation")   # inherited, not ranked from the steer text
        # The one line the surface replies with says plainly that this started a continuation.
        self.assertIn("had already ended (done), so this steer started a continuation", result["message"])
        self.assertIn(self.PARENT, result["message"])
        self.assertIn(result["task_id"], result["message"])
        self.assertNotEqual(result["task_id"], self.PARENT)
        payload = ledger.load_payload()
        parent = ledger.find_task(payload, self.PARENT)
        self.assertEqual(parent["status"], "done")             # the terminal row is never reopened
        child = ledger.find_task(payload, result["task_id"])
        self.assertEqual(child["continuation_of"], self.PARENT)
        self.assertEqual(child["workstream"], "example-training")
        self.assertIn("Also move the Help button to the top right.", child["prompt"])
        self.assertIn(self.PARENT, child["prompt"])

    def test_steer_continuation_inherits_parent_delivery(self) -> None:
        # Wrong impl: the steer path passes delivery=None, so the child row has no policy,
        # its done report batches for a digest that defers on a draft in CoS, and it reaches nobody.
        for parent_delivery, expected in ((None, "direct"), ("direct", "direct"), ("digest", "digest")):
            with self.subTest(parent=parent_delivery):
                write_ledger(self.row("done", **({"delivery": parent_delivery} if parent_delivery else {})))
                code, result = self.run_steer()
                self.assertEqual(code, 0, result)
                child = ledger.find_task(ledger.load_payload(), result["task_id"])
                self.assertEqual(child.get("delivery"), expected)

    def test_live_row_is_messaged_and_no_row_is_written(self) -> None:
        # Wrong impl: every steer dispatches a new task, duplicating work the live worker is still doing.
        self.live_agents.stop()
        self.live_agents = patch.object(ledger, "live_agents", return_value={
            "T-example": {"state": "idle", "workspace_id": "ABC"}})
        self.live_agents.start()
        write_ledger(self.row("active", finished_at=None, reaped_at=None, report_delivered=None))
        code, result = self.run_steer()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "message")
        self.assertEqual(result["thread_id"], "T-example")
        self.assertEqual(result["workspace_ref"], "workspace:612")
        self.assertTrue(result["thread_live"])
        self.assertIn("native message", result["message"])
        self.assertEqual([t["id"] for t in ledger.load_payload()["tasks"]], [self.PARENT])

    def test_unknown_task_fails_without_dispatching(self) -> None:
        write_ledger()
        code, result = self.run_steer()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(ledger.load_payload()["tasks"], [])

    def not_ready_row(self) -> dict:
        # Async ack: a routing row in the 16 to 30 s before the detached
        # launcher binds a thread has no target yet.
        return self.row("routing", thread_id=None, workspace_ref=None, finished_at=None,
                        reaped_at=None, report_delivered=None)

    def test_routing_row_without_a_thread_queues_the_steer(self) -> None:
        # A steer on a row that is still routing. Wrong impl: dispatches a supplemental worker (two workers
        # on one deliverable), parks the steer with "retry in about 30 s", or treats the row as terminal.
        write_ledger(self.not_ready_row())
        code, result = self.run_steer()
        self.assertEqual(code, 0, result)
        self.assertEqual((result["status"], result["delivered"], result["queued"]), ("steer-queued", False, True))
        self.assertEqual(result["steer"]["action"], "queue")
        self.assertEqual(result["message"],
                         f"steer queued for {dispatch.steer_worker_name('example-training')} worker ({self.PARENT}); "
                         f"delivers when it starts")
        payload = ledger.load_payload()
        self.assertEqual([t["id"] for t in payload["tasks"]], [self.PARENT], "no new worker row")
        parent = ledger.find_task(payload, self.PARENT)
        self.assertEqual(parent["status"], "routing")
        self.assertEqual([e["text"] for e in dispatch.pending_steers(parent)],
                         ["Also move the Help button to the top right."])
        self.assertEqual(self.messages, [], "nothing is sent to a worker that has no thread")

    def test_two_steers_on_a_starting_row_queue_in_order_with_no_new_worker(self) -> None:
        # A second steer meets cmux's refusal ("recipient runs no agent that reads messages").
        # Wrong impl: either steer spawns a worker, or the second replaces the first.
        self.message_reply = {"ok": False, "reason": "recipient runs no agent that reads messages"}
        write_ledger(self.not_ready_row())
        self.run_steer("Recolor the thumbnails.")
        write_ledger({**ledger.find_task(ledger.load_payload(), self.PARENT), "agent": "claude",
                      "session_id": self.SID, "workspace_ref": "workspace:612"})
        code, result = self.run_steer("Use slide 2 as the deck thumbnail.")
        self.assertEqual((code, result["status"], result["queue_length"]), (0, "steer-queued", 2))
        self.assertEqual(len(self.messages), 1, "the second steer tried the session once, then queued")
        payload = ledger.load_payload()
        self.assertEqual([t["id"] for t in payload["tasks"]], [self.PARENT])
        self.assertEqual([e["text"] for e in dispatch.pending_steers(ledger.find_task(payload, self.PARENT))],
                         ["Recolor the thumbnails.", "Use slide 2 as the deck thumbnail."])

    def test_no_fallback_dispatch_keeps_the_still_starting_message(self) -> None:
        # Opt-out for a caller that will message the thread itself once it is bound. Wrong impl:
        # ignores the flag and dispatches anyway, or reports "<unbound>" as a live target.
        write_ledger(self.not_ready_row())
        code, result = self.run_steer(no_fallback_dispatch=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "message")
        self.assertEqual(result["steer"]["action"], "queue")
        self.assertEqual(dispatch.pending_steers(ledger.find_task(ledger.load_payload(), self.PARENT)), [])
        self.assertEqual(result["thread_id"], "")
        self.assertIn("still starting", result["message"])
        self.assertIn("retry the steer", result["message"])
        self.assertNotIn("live in", result["message"])
        self.assertEqual([t["id"] for t in ledger.load_payload()["tasks"]], [self.PARENT])

    # --- Claude workers ---
    SID = "0fd3afb0-1234-4abc-8def-0123456789ab"

    def claude_row(self, status: str = "active", **extra: Any) -> dict:
        fields = {"agent": "claude", "session_id": self.SID, "thread_id": None, "report_delivered": None,
                  "finished_at": None, "reaped_at": None, **extra}
        return self.row(status, **fields)

    def test_claude_session_id_is_the_thread_before_the_first_heartbeat(self) -> None:
        # Wrong impl: a Claude row whose launcher set session_id and workspace_ref but whose
        # thread_id is not bound yet reads as "no thread" and is queued without trying the session.
        decision = dispatch.steer_decision(self.claude_row("routing"), {})
        self.assertEqual((decision["action"], decision["thread_id"], decision["agent"]), ("message", self.SID, "claude"))
        # No tab yet (queued, launcher not run): the steer is queued.
        queued = self.claude_row("queued", workspace_ref=None)
        self.assertEqual(dispatch.steer_decision(queued, {})["action"], "queue")
        # An Amp row without thread_id is unchanged: session_id means nothing there.
        amp = self.row("routing", thread_id=None, session_id=self.SID)
        self.assertEqual(dispatch.steer_decision(amp, {})["action"], "queue")

    def test_open_tab_counts_as_live_when_the_session_store_lags(self) -> None:
        # Wrong impl: thread_live comes from live_agents() alone, which misses a worker the
        # lazily written cmux session store has not saved yet (thread_live=false).
        row = self.claude_row(thread_id=self.SID)
        self.assertFalse(dispatch.steer_decision(row, {}, [])["thread_live"])
        tabs = [{"ref": "workspace:612", "id": "ABC-1"}]
        self.assertTrue(dispatch.steer_decision(row, {}, tabs)["thread_live"])
        other = {"X": {"thread_id": "X", "agent": "claude", "workspace_id": "abc-1", "state": "Running"}}
        self.assertTrue(dispatch.steer_decision(self.row("active"), other, tabs)["thread_live"],
                        "any live agent in the row's tab counts, matched case-insensitively")

    def test_live_claude_worker_gets_the_steer_by_cmux_agent_message(self) -> None:
        # Wrong impl: the surface is told to "send by native message", which a Claude CoS cannot do.
        write_ledger(self.claude_row(thread_id=self.SID))
        before = len(ledger.load_payload()["tasks"])
        code, result = self.run_steer("Use the new owner_tabs helper.")
        self.assertEqual((code, result["status"], result["delivered"]), (0, "delivered", True))
        self.assertEqual(len(self.messages), 1)
        target, text, sender = self.messages[0]
        self.assertEqual((target, sender), ("workspace:612", "switchboard steer (cos)"))
        self.assertIn("Use the new owner_tabs helper.", text)
        self.assertIn(self.PARENT, text)
        self.assertEqual(len(ledger.load_payload()["tasks"]), before, "a delivered steer writes no new row")

    def test_timed_out_message_is_never_sent_twice(self) -> None:
        # Wrong impl (code review): a cmux timeout after the message was queued reads as a
        # refusal, so a starting row also gets a supplemental worker and an active row gets
        # the text typed again: the worker receives the steer twice.
        self.message_reply = {"ok": False, "reason": "cmux agent message failed: Command timed out after 15 seconds"}
        for status in ("routing", "active"):
            with self.subTest(status=status):
                write_ledger(self.claude_row(status))
                before = len(ledger.load_payload()["tasks"])
                code, result = self.run_steer()
                self.assertEqual((code, result["status"], result["delivered"], result["ambiguous"]),
                                 (0, "message", False, True))
                self.assertEqual(len(ledger.load_payload()["tasks"]), before, "no supplemental worker")
                self.assertIn("check the tab before resending", result["message"])

    def test_owner_tab_row_gets_the_steer_in_the_owner_tab(self) -> None:
        # Wrong impl: a row deliver_to_owner_tab bound to an open owner tab (owner_tab true, no
        # worktree, never reaped) is steered like a worker that never started (supplemental),
        # opening a second tab while the owner tab already works the ask.
        write_ledger(self.row("active", agent="claude", thread_id=self.SID, owner_tab=True,
                              workspace_ref="workspace:5050", report_delivered=None,
                              finished_at=None, reaped_at=None))
        code, result = self.run_steer("Cite the review thread too.")
        self.assertEqual((code, result["status"]), (0, "delivered"))
        self.assertEqual(self.messages[0][0], "workspace:5050")

    def test_refused_message_on_a_starting_claude_row_queues_the_steer(self) -> None:
        # Wrong impl: a session still booting (cmux has no agent hooks for it yet) loses the steer,
        # or the refusal starts a supplemental worker on the same deliverable.
        self.message_reply = {"ok": False, "reason": "recipient runs no agent that reads messages"}
        for status, extra in (("routing", {}), ("active", {"started_at": None, "heartbeat_at": None})):
            with self.subTest(status=status):
                write_ledger(self.claude_row(status, **extra))
                code, result = self.run_steer()
                self.assertEqual((code, result["status"], result["steer"]["action"]), (0, "steer-queued", "queue"))
                self.assertIn("delivers when it starts", result["message"])
                self.assertEqual([t["id"] for t in ledger.load_payload()["tasks"]], [self.PARENT])

    def test_refused_message_on_an_active_claude_row_tells_the_surface_how_to_send(self) -> None:
        # Boundary: a worker that already acked (started_at set) is not "starting"; a refusal there
        # is reported to the surface, never queued behind a session that may be gone.
        self.message_reply = {"ok": False, "reason": "unreachable"}
        write_ledger(self.claude_row("active", thread_id=self.SID, started_at=iso(datetime.now(timezone.utc))))
        code, result = self.run_steer()
        self.assertEqual((result["status"], result["delivered"], result["delivery_error"]), ("message", False, "unreachable"))
        self.assertIn("cmux agent message workspace:612", result["message"])

    def test_amp_row_is_never_sent_a_cmux_agent_message(self) -> None:
        write_ledger(self.row("active", report_delivered=None, finished_at=None, reaped_at=None))
        code, result = self.run_steer()
        self.assertEqual((result["status"], self.messages), ("message", []))
        self.assertIn("native message", result["message"])

    def test_bound_but_not_live_thread_still_takes_the_native_message(self) -> None:
        # Boundary: the fallback fires on "no thread", not on "thread not live". A bound thread whose
        # cmux agent is momentarily absent reads the message when it resumes; dispatching a second
        # worker here would duplicate work the parent is doing.
        write_ledger(self.row("active", finished_at=None, reaped_at=None, report_delivered=None))
        code, result = self.run_steer()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "message")
        self.assertEqual(result["steer"]["action"], "message")
        self.assertEqual(result["thread_id"], "T-example")
        self.assertFalse(result["thread_live"])
        self.assertEqual([t["id"] for t in ledger.load_payload()["tasks"]], [self.PARENT])


class QueuedSteerDeliveryTests(unittest.TestCase):
    """Queued steers. A steer on a worker that has not started
    waits in the row's `pending_steers`; `deliver_queued_steers` (spawned at the worker's
    ack and heartbeat, and run by the sweep) sends them in order once the worker started,
    reports one still queued after steer_queue_timeout_minutes to the origin, and reports
    and drops the queue of a row that ended first. Nothing on this path spawns a worker."""

    TID = "rt-00000000004a"
    SID = "1a2b3c4d-1234-4abc-8def-0123456789ab"

    def setUp(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.replies: list[dict] = []
        self.notified: list[tuple[str, str, str, bool]] = []
        self.cos_lines: list[str] = []
        for p in [patch.object(dispatch, "cmux_agent_message", side_effect=self.fake_message),
                  patch.object(dispatch, "notify", side_effect=self.fake_notify),
                  patch.object(dispatch, "tell_cos_direct", side_effect=self.fake_cos),
                  patch.object(dispatch, "command_dispatch", side_effect=AssertionError("no worker is dispatched"))]:
            p.start()
            self.addCleanup(p.stop)

    def fake_message(self, target: str, text: str, sender: str, timeout: float = 15.0) -> dict:
        self.messages.append((target, text, sender))
        return dict(self.replies.pop(0)) if self.replies else {"ok": True, "id": f"msg-{len(self.messages)}"}

    def fake_notify(self, task_id: str, text: str, status: str = "active", urgent: bool = False) -> dict:
        self.notified.append((task_id, text, status, urgent))
        return {"delivered": True}

    def fake_cos(self, text: str) -> dict:
        self.cos_lines.append(text)
        return {"ok": True}

    def row(self, status: str = "active", minutes_ago: int = 1, *texts: str, **extra: Any) -> dict:
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        steers = [{"id": f"st-{i:08x}", "origin": "cos", "text": t, "queued_at": iso(stamp)}
                  for i, t in enumerate(texts or ("Recolor the thumbnails.", "Use slide 2 as the deck thumbnail."))]
        base = {"id": self.TID, "status": status, "attempt_id": f"{self.TID}.a1.000001", "workstream": "example-training",
                "origin": "cos", "prompt": "refresh the Help Center", "agent": "claude", "session_id": self.SID,
                "workspace_ref": "workspace:612", "created_at": iso(stamp), "updated_at": iso(stamp),
                "launched_at": iso(stamp), "pending_steers": steers}
        if status == "active":
            base.update(thread_id=self.SID, started_at=iso(stamp), heartbeat_at=iso(stamp))
        base.update(extra)
        return base

    def queue(self) -> list[dict]:
        return list(ledger.find_task(ledger.load_payload(), self.TID).get("pending_steers") or [])

    def test_started_worker_gets_queued_steers_in_order_once(self) -> None:
        # Wrong impl: steers go out of order, go out again on the next heartbeat, or need CoS to resend.
        write_ledger(self.row("active"))
        result = dispatch.deliver_queued_steers(self.TID)
        self.assertEqual(len(result["delivered"]), 2)
        self.assertEqual([m[0] for m in self.messages], ["workspace:612", "workspace:612"])
        self.assertIn("Recolor the thumbnails.", self.messages[0][1])
        self.assertIn("Use slide 2 as the deck thumbnail.", self.messages[1][1])
        self.assertIn(self.TID, self.messages[0][1])
        self.assertTrue(all(e.get("delivered_at") for e in self.queue()))
        dispatch.deliver_queued_steers(self.TID)
        self.assertEqual(len(self.messages), 2, "a delivered steer is never sent twice")

    def test_not_started_worker_keeps_the_queue(self) -> None:
        # Wrong impl: sends to a session that has not registered (lost) or to a routing row's tab.
        for status, extra in (("routing", {}), ("queued", {"workspace_ref": None}),
                              ("active", {"started_at": None, "heartbeat_at": None, "thread_id": None})):
            with self.subTest(status=status):
                self.messages.clear()
                write_ledger(self.row(status, 1, **extra) if status != "active" else
                             {**self.row("active", 1), **extra})
                result = dispatch.deliver_queued_steers(self.TID)
                self.assertEqual((self.messages, result["delivered"], self.notified), ([], [], []))
                self.assertEqual(len(dispatch.pending_steers(ledger.find_task(ledger.load_payload(), self.TID))), 2)

    def test_refusal_at_delivery_keeps_order(self) -> None:
        # Wrong impl: the second steer is delivered while the first waits, so the worker reads them reversed.
        self.replies = [{"ok": False, "reason": "recipient runs no agent that reads messages"}]
        write_ledger(self.row("active"))
        result = dispatch.deliver_queued_steers(self.TID)
        self.assertEqual((len(self.messages), result["delivered"]), (1, []))
        first, second = self.queue()
        self.assertEqual((first.get("attempts"), first.get("delivered_at"), second.get("attempts")), (1, None, None))
        dispatch.deliver_queued_steers(self.TID)
        self.assertEqual([("Recolor" in m[1]) for m in self.messages], [True, True, False])

    def test_timed_out_message_counts_as_delivered(self) -> None:
        # Wrong impl: a cmux timeout (message may already be queued) is retried, so the worker reads it twice.
        self.replies = [{"ok": False, "reason": "cmux agent message failed: Command timed out after 15 seconds"}]
        write_ledger(self.row("active", 1, "Recolor the thumbnails."))
        dispatch.deliver_queued_steers(self.TID)
        dispatch.deliver_queued_steers(self.TID)
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.queue()[0].get("ambiguous"))

    def test_worker_that_never_starts_sends_one_callback_and_spawns_nothing(self) -> None:
        # Wrong impl: the steer waits forever unseen, a worker is spawned silently, or CoS hears every tick.
        write_ledger(self.row("routing", 11, "Recolor the thumbnails."))
        result = dispatch.deliver_queued_steers(self.TID, {**dispatch.DEFAULTS, "steer_queue_timeout_minutes": 10})
        self.assertEqual(len(result["timed_out"]), 1)
        self.assertEqual(len(self.notified), 1)
        task_id, text, status, urgent = self.notified[0]
        self.assertEqual((task_id, status, urgent), (self.TID, "routing", True))
        self.assertIn("still undelivered", text)
        self.assertIn("Nothing was spawned", text)
        self.assertIn("--force-redispatch", text)
        dispatch.deliver_queued_steers(self.TID, {**dispatch.DEFAULTS, "steer_queue_timeout_minutes": 10})
        self.assertEqual(len(self.notified), 1, "one callback per queued steer")
        self.assertEqual(self.messages, [])

    def test_timeout_boundaries(self) -> None:
        # Wrong impl: fires inside the window, fires for a queued row held by a prerequisite, or
        # ignores a relaunch (the clock restarts at the latest launch claim).
        cfg = {**dispatch.DEFAULTS, "steer_queue_timeout_minutes": 10}
        recent = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        for name, row in (("inside window", self.row("routing", 5, "x")),
                          ("queued row", self.row("queued", 60, "x")),
                          ("relaunched", self.row("routing", 60, "x", launching_at=recent)),
                          ("disabled", self.row("routing", 60, "x"))):
            with self.subTest(name):
                self.notified.clear()
                write_ledger(row)
                dispatch.deliver_queued_steers(self.TID, {**cfg, "steer_queue_timeout_minutes": 0} if name == "disabled" else cfg)
                self.assertEqual(self.notified, [])

    def test_row_that_ends_before_starting_reports_and_drops_its_queue(self) -> None:
        # Wrong impl: messages a reaped tab, dispatches a continuation on its own, or drops the steers silently.
        write_ledger(self.row("failed", 3, finished_at=iso(datetime.now(timezone.utc))))
        result = dispatch.deliver_queued_steers(self.TID)
        self.assertEqual((len(result["dropped"]), self.messages), (2, []))
        self.assertEqual(len(self.cos_lines), 1)
        self.assertIn(f"never reached the {dispatch.steer_worker_name('example-training')} worker", self.cos_lines[0])
        self.assertIn("Recolor the thumbnails.", self.cos_lines[0])
        self.assertTrue(all(e.get("dropped_at") for e in self.queue()))
        self.assertEqual(dispatch.pending_steers(ledger.find_task(ledger.load_payload(), self.TID)), [])

    def test_amp_worker_queue_goes_to_the_origin_with_the_thread(self) -> None:
        # An Amp thread reads native messages only, which a script cannot send.
        write_ledger(self.row("active", 1, "Recolor the thumbnails.", agent="amp", session_id=None,
                              thread_id="T-example"))
        dispatch.deliver_queued_steers(self.TID)
        self.assertEqual(self.messages, [])
        self.assertIn("T-example", self.notified[0][1])
        self.assertIn("Recolor the thumbnails.", self.notified[0][1])
        self.assertTrue(self.queue()[0].get("delivered_at"))

    def test_second_deliverer_skips_a_locked_row(self) -> None:
        # Wrong impl: the ack spawn and the sweep both send the same steer.
        write_ledger(self.row("active"))
        with dispatch.steer_queue_lock(self.TID) as held:
            self.assertTrue(held)
            result = dispatch.deliver_queued_steers(self.TID)
        self.assertEqual((result["busy"], self.messages), ([{"task_id": self.TID}], []))

    def test_ack_and_heartbeat_spawn_delivery_only_with_a_queue(self) -> None:
        # Wrong impl: the worker's start never triggers delivery (steers wait for the 10-minute sweep),
        # or every heartbeat of every worker spawns a process.
        spawn = ledger.spawn_steer_delivery
        with patch.dict(os.environ, {"SWITCHBOARD_NO_STEER_SPAWN": ""}), patch.object(ledger.subprocess, "Popen") as popen:
            self.assertTrue(spawn(self.row("active"), {}))
            self.assertIn("deliver-steers", popen.call_args.args[0])
            self.assertIn(self.TID, popen.call_args.args[0])
            popen.reset_mock()
            self.assertFalse(spawn(self.row("active", 1, pending_steers=[]), {}))
            self.assertFalse(spawn(self.row("active"), {"_terminal": True}))
            self.assertFalse(spawn({**self.row("active"), "pending_steers": [{"id": "st-1", "delivered_at": "x"}]}, {}))
            popen.assert_not_called()
        write_ledger(self.row("routing", 1, thread_id=None))
        with patch.object(ledger, "spawn_steer_delivery") as hook:
            out = io.StringIO()
            with redirect_stdout(out):
                with patch.object(sys, "argv", ["running-task-state.py", "ack", "--task-id", self.TID, "--role", "worker",
                                                "--thread-id", self.SID]):
                    ledger.main()
            hook.assert_called_once()
            self.assertEqual(hook.call_args.args[0]["id"], self.TID)
            hook.reset_mock()
            with redirect_stdout(out):
                with patch.object(sys, "argv", ["running-task-state.py", "heartbeat", "--task-id", self.TID, "--role",
                                                "worker", "--thread-id", self.SID]):
                    ledger.main()
            hook.assert_called_once()


class AsyncLaunchTests(unittest.TestCase):
    """One-call dispatch ack: the ledger
    row is written and `dispatch` returns at once; the worktree, tab and Amp
    verification (16 to 30 s) run in a detached `dispatch.py launch`. Each test
    names the wrong implementation it catches."""

    def setUp(self) -> None:
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWITCHBOARD_DISPATCH_SYNC", None)
        write_ledger()
        self.launch = patch.object(dispatch, "launch", side_effect=self.fake_launch)
        self.launch_mock = self.launch.start()
        self.addCleanup(self.launch.stop)
        for p in [patch.object(ledger, "live_agents", return_value={}),
                  patch.object(dispatch, "reap_all", return_value={"closed": [], "skipped": []}),
                  patch.object(dispatch, "config", return_value=dict(dispatch.DEFAULTS))]:
            p.start()
            self.addCleanup(p.stop)

    def popen_stub(self, result: Any = None, error: Exception | None = None):
        """Patch Popen only for the detached launcher argv; run() and every other
        subprocess (running-task-state.py start/update) keep the real Popen."""
        real = subprocess.Popen
        launched: list[tuple[list[str], dict]] = []

        def fake(argv, *a, **kw):
            if isinstance(argv, list) and "launch" in argv and str(SCRIPTS / "dispatch.py") in argv:
                launched.append((list(argv), dict(kw)))
                if error is not None:
                    raise error
                return result if result is not None else Mock(pid=1)
            return real(argv, *a, **kw)

        patcher = patch.object(dispatch.subprocess, "Popen", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return launched

    def fake_launch(self, task: dict, cfg: dict) -> dict:
        def bind(payload):
            row = ledger.find_task(payload, task["id"])
            row.update(workspace_ref="workspace:4242", worktree=str(TMP / "wt-async"), branch=f"task/{task['id']}",
                       worker_id="worker-cccccccccccc", status="routing", summary="started", updated_at=ledger.now_iso())
            return row
        return ledger.locked_mutation(bind)

    def run_launch(self, task_id: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch.command_launch(argparse.Namespace(task_id=task_id))
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_dispatch_acks_before_the_launch_and_records_the_launcher(self) -> None:
        # Wrong impl: dispatch still calls launch() inline (24 s on the CoS hot path), or acks
        # without spawning anything, or forgets the launcher pid so nobody can tell a crashed
        # launcher from a slow one.
        launched = self.popen_stub(result=Mock(pid=43210))
        code, result = run_dispatch()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["launch"], "background")
        self.assertEqual(result["launcher_pid"], 43210)
        self.assertIn("accepted; worker starting", result["message"])
        # Names, never bare numbers: the ack leads with the workstream name.
        self.assertFalse(result["message"].startswith(f"[{result['task_id']} "), result["message"])
        self.assertIn(f"· {result['task_id']}]", result["message"])
        self.assertIsNone(result["workspace_ref"])
        self.launch_mock.assert_not_called()
        self.assertEqual(len(launched), 1)
        argv, kwargs = launched[0]
        self.assertEqual(argv[1:], [str(SCRIPTS / "dispatch.py"), "launch", "--task-id", result["task_id"]])
        # Detached: a new session off Windows, CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP on it (a hidden console, so its children open no windows).
        self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags") == 0x08000200)
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertEqual(row["status"], "routing")
        self.assertEqual(row["launcher_pid"], 43210)
        self.assertTrue(row["launch_log"].endswith(f"{result['task_id']}.log"))
        self.assertNotIn("worker_id", row)
        self.assertTrue(Path(row["launch_log"]).read_text().strip().endswith("launcher spawned by dispatch"))

    def test_sync_flag_and_env_keep_the_inline_launch(self) -> None:
        # Wrong impl: the opt-outs are ignored, so a test or a debugging run that needs the ref
        # back gets an ack with workspace_ref null.
        launched = self.popen_stub()
        code, result = run_dispatch(sync=True)
        self.assertEqual((code, result["status"], result["workspace_ref"]), (0, "routed", "workspace:4242"), result)
        self.assertNotIn("launch", result)
        os.environ["SWITCHBOARD_DISPATCH_SYNC"] = "1"
        code, result = run_dispatch()
        self.assertEqual((code, result["workspace_ref"]), (0, "workspace:4242"), result)
        self.assertEqual(launched, [])
        self.assertEqual(self.launch_mock.call_count, 2)

    def test_config_false_keeps_the_inline_launch(self) -> None:
        launched = self.popen_stub()
        with patch.object(dispatch, "config", return_value={**dispatch.DEFAULTS, "async_launch": False}):
            code, result = run_dispatch()
        self.assertEqual((code, result["workspace_ref"]), (0, "workspace:4242"), result)
        self.assertEqual(launched, [])

    def test_spawn_failure_falls_back_to_the_inline_launch(self) -> None:
        # Wrong impl: a Popen error (no python, fork limit) leaves the row routing with nobody
        # behind it until requeue_orphaned_routes notices 10 min later.
        launched = self.popen_stub(error=OSError("fork failed"))
        code, result = run_dispatch()
        self.assertEqual(len(launched), 1)
        self.assertEqual((code, result["status"], result["workspace_ref"]), (0, "routed", "workspace:4242"), result)
        self.assertNotIn("launch", result)
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertNotIn("launcher_pid", row)

    def test_launch_command_binds_the_routing_row(self) -> None:
        self.popen_stub()
        _, acked = run_dispatch()
        with patch.object(dispatch, "run") as run_cmd:
            code, result = self.run_launch(acked["task_id"])
        self.assertEqual((code, result["status"]), (0, "launched"), result)
        self.assertEqual(result["workspace_ref"], "workspace:4242")
        row = ledger.find_task(ledger.load_payload(), acked["task_id"])
        self.assertEqual((row["status"], row["worker_id"], row["workspace_ref"]), ("routing", "worker-cccccccccccc", "workspace:4242"))
        # The status publish left the dispatch hot path and runs here instead.
        published = [c for c in run_cmd.call_args_list if "publish-running-tasks-status.sh" in str(c.args[0])]
        self.assertEqual(len(published), 1)

    def test_launch_failure_fails_the_row_and_tells_the_origin_once(self) -> None:
        # Wrong impl: the detached launcher dies quietly; the ack said "accepted" and the origin
        # never hears that no worker exists.
        self.popen_stub()
        _, acked = run_dispatch()
        task_id = acked["task_id"]
        self.launch.stop()
        self.launch = patch.object(dispatch, "launch", side_effect=RuntimeError("cmux new-workspace failed: no answer"))
        self.launch.start()
        self.addCleanup(self.launch.stop)

        def fake_run(cmd, **kw):
            if "running-task-state.py" in str(cmd[1]) and "update" in cmd:
                subprocess.run(cmd, check=False, capture_output=True, text=True, env=os.environ.copy())
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(dispatch, "run", side_effect=fake_run), \
                patch.object(dispatch, "notify", return_value={"delivered": True}) as notify:
            code, result = self.run_launch(task_id)
        self.assertEqual((code, result["status"]), (1, "failed"), result)
        self.assertTrue(result["origin_notified"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], task_id)
        self.assertEqual(notify.call_args.kwargs["status"], "failed")
        self.assertIn("launch did not start a worker", notify.call_args.args[1])
        self.assertIn("cmux new-workspace failed", notify.call_args.args[1])
        row = ledger.find_task(ledger.load_payload(), task_id)
        self.assertEqual(row["status"], "failed")

    def test_launch_command_skips_rows_it_does_not_own(self) -> None:
        # Wrong impl: a second launcher (manual rerun, sweep) launches a row that already has a
        # worker, or a queued/terminal row, creating a duplicate worktree and tab.
        write_ledger({"id": "rt-00000000004b", "status": "routing", "attempt": 1, "attempt_id": "rt-00000000004b.a1.a168e4",
                      "workstream": "switchboard-maintenance", "origin": "cos", "prompt": "x", "worker_id": "worker-111111111111",
                      "created_at": ledger.now_iso(), "updated_at": ledger.now_iso()},
                     {"id": "rt-00000000004c", "status": "queued", "attempt": 1, "attempt_id": "rt-00000000004c.a1.606bee",
                      "workstream": "switchboard-maintenance", "origin": "cos", "prompt": "y",
                      "created_at": ledger.now_iso(), "updated_at": ledger.now_iso()})
        for task_id in ("rt-00000000004b", "rt-00000000004c"):
            with self.subTest(task_id=task_id):
                code, result = self.run_launch(task_id)
                self.assertEqual((code, result["status"]), (0, "skipped"), result)
        self.launch_mock.assert_not_called()
        code, result = self.run_launch("rt-00000000000f")
        self.assertEqual((code, result["status"]), (2, "failed"))


class OwnerTabVerifyCommandTests(HermeticRepo):
    """cmux-helpers.sh owner-tab launches reuse the worker launch check through
    `verify-amp`, which runs after the launch command is typed and maps the
    verdict to an exit code the shell can branch on: 0 Amp up, 2 inconclusive (the
    helper keeps its own readiness waits and types nothing), 1 known not Amp after
    the retypes. Wrong impls: `verify-amp` exiting 1 on an unreadable screen (the
    helper would give up on a tab whose Amp is still rendering); --seconds ignored
    so the harness waits the production 30 s."""

    ACK_PROMPT = LaunchVerificationTests.ACK_PROMPT
    AMP = LaunchVerificationTests.AMP
    ZSH = LaunchVerificationTests.ZSH

    def setUp(self) -> None:
        super().setUp()
        # Amp-shaped fixtures (screens, `amp -m` commands): pin the agent knob,
        # which defaults to claude (switchboard_agent.py).
        env_pin = patch.dict(os.environ, {"SWITCHBOARD_AGENT": "amp"})
        env_pin.start()
        self.addCleanup(env_pin.stop)
        self.sent: list[str] = []
        self.clock = 0.0

    def run_main(self, argv: list[str], screens: list[str | None] | None = None) -> tuple[int, dict]:
        screens = list(screens or [])
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if not cmd or str(cmd[0]) != dispatch.CMUX:
                return real_run(cmd, **kw)
            if cmd[1] == "tree":
                return subprocess.CompletedProcess(cmd, 0, "workspace:77\n  surface:7\n", "")
            if cmd[1] == "read-screen":
                screen = screens.pop(0) if len(screens) > 1 else screens[0]
                return subprocess.CompletedProcess(cmd, 1 if screen is None else 0, screen or "", "")
            if cmd[1] == "send":
                self.sent.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1] == "send-key":
                self.sent.append(f"<{cmd[-1]}>")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kw)

        def sleep(seconds: float) -> None:
            # Rounded: 0.1 s fsmonitor-stop sleeps accumulate float error before the tab opens,
            # which put the verify deadline a hair past 15 polls and gave one extra read (order-dependent flake).
            self.clock = round(self.clock + seconds, 6)

        out = io.StringIO()
        # verify-amp assigns the module budget globals; patching them to their own
        # value restores the production defaults for the tests that follow.
        with patch.object(dispatch, "run", fake_run), patch.object(dispatch.time, "sleep", sleep), \
                patch.object(dispatch.time, "monotonic", lambda: self.clock), \
                patch.object(dispatch, "AMP_VERIFY_SECONDS", dispatch.AMP_VERIFY_SECONDS), \
                patch.object(dispatch, "AMP_VERIFY_POLL_SECONDS", dispatch.AMP_VERIFY_POLL_SECONDS), \
                patch.object(sys, "argv", ["dispatch.py", *argv]), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = dispatch.main()
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_verify_amp_exit_codes_follow_the_verdict(self) -> None:
        argv = ["verify-amp", "--workspace-ref", "workspace:77", "--command", "amp --mode ultra", "--seconds", "3", "--poll", "1"]
        code, result = self.run_main(argv, [self.AMP])
        self.assertEqual(code, 0)
        self.assertTrue(result["amp_verified"])
        self.assertEqual(self.sent, [])

        self.sent.clear()
        self.clock = 0.0
        code, result = self.run_main(argv, [None])
        self.assertEqual(code, 2, result)
        self.assertEqual(result["amp_verified"], "unknown")
        self.assertEqual(self.sent, [], "an unreadable screen is never typed into")

        self.sent.clear()
        self.clock = 0.0
        code, result = self.run_main(argv, [self.ACK_PROMPT])
        self.assertEqual(code, 2, result)
        self.assertEqual(self.sent, [], "a prompt on screen is never answered")

        self.sent.clear()
        self.clock = 0.0
        code, result = self.run_main(argv, [self.ZSH, self.ZSH, self.ZSH, self.ZSH, self.AMP])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["launch_retyped"])
        self.assertEqual([s for s in self.sent if not s.startswith("<")], ["amp --mode ultra"])

        self.sent.clear()
        self.clock = 0.0
        code, result = self.run_main(argv, [self.ZSH])
        self.assertEqual(code, 1, result)
        self.assertTrue(result["launch_retyped"])
        self.assertEqual([s for s in self.sent if not s.startswith("<")], ["amp --mode ultra"] * len(dispatch.LAUNCH_RETYPE_PAUSES_SECONDS),
                         "retyped once per launcher pause, then left to the sweep")
        self.assertEqual(result["launch_retypes"], len(dispatch.LAUNCH_RETYPE_PAUSES_SECONDS))

    def test_verify_amp_honours_the_seconds_budget(self) -> None:
        argv = ["verify-amp", "--workspace-ref", "workspace:77", "--command", "amp", "--seconds", "2", "--poll", "1"]
        code, _ = self.run_main(argv, [None])
        self.assertEqual(code, 2)
        self.assertLess(self.clock, 10, "the harness budget replaced the production 30 s")


class ConflictGraceTests(HermeticRepo):
    """Rebase-conflict grace window. A worker that hits a rebase conflict at finish
    often resolves it and lands within minutes, so an instant "failed: could not land
    on main" sends CoS after land-this continuations with nothing to do. Wrong impls: deliver the failed report at once; deliver
    both a failed and a done line; reap the tab while the row is held; route a
    steer during the window to a continuation; let a rerun of finish extend the
    window."""

    REPORT = "Deck updated and shared with Noel."

    def setUp(self) -> None:
        super().setUp()
        self.sent: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str]] = []
        self.clock: datetime | None = None
        for p in [patch.object(dispatch, "tab_send", lambda ref, text: self.sent.append((ref, text)) or True),
                  patch.object(dispatch, "record_on_main",
                               lambda task, status="", result="", thread_id="": self.recorded.append((task["id"], status)) or {}),
                  patch.object(dispatch, "config", lambda: {**dispatch.DEFAULTS, "conflict_grace_seconds": 600}),
                  patch.object(dispatch, "now", lambda: self.clock or datetime.now(timezone.utc))]:
            p.start()
            self.addCleanup(p.stop)
        self.workspaces = [{"ref": "workspace:40", "id": "w-40", "title": "switchboard-maintenance · worker-aaaaaaaaaaaa"}]
        self.agents["T-dead"] = {"state": "Idle", "state_ts": datetime.now(timezone.utc).timestamp(),
                                 "workspace_id": "w-40", "title": "worker"}
        fresh = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        write_ledger(self.worker_row(started_at=fresh, heartbeat_at=fresh, updated_at=fresh))
        # The same line edited on both sides: a real rebase conflict, not an overlap.
        (self.path / "a.md").write_text("branch version\n")
        self._git("add", "a.md", cwd=self.path)
        self._git("commit", "-q", "-m", "rt-wt: edit a.md", cwd=self.path)
        (self.repo / "a.md").write_text("main version\n")
        self._git("add", "a.md")
        self._git("commit", "-q", "-m", "landed on main meanwhile")

    def finish(self) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = dispatch.command_finish(argparse.Namespace(task_id="rt-wt"))
        return code, json.loads(out.getvalue().strip())

    def hold_report(self) -> None:
        """What the ledger's _not_landed path stores when report-to-cos.sh runs during the window."""
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-wt").update(report=self.REPORT))

    def resolve_conflict(self) -> None:
        """The worker resolves in its worktree: rebase onto main, keep its version, commit."""
        rebase = dispatch.run(["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "main"])
        self.assertNotEqual(rebase.returncode, 0, "fixture must conflict")
        (self.path / "a.md").write_text("branch version, resolved\n")
        self._git("add", "a.md", cwd=self.path)
        dispatch.run(["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.editor=true",
                      "rebase", "--continue"], check=True)

    def test_conflict_opens_the_window_instead_of_failing(self) -> None:
        code, outcome = self.finish()
        self.assertEqual((code, outcome["landed"], outcome.get("grace"), outcome.get("landing")), (1, False, True, "conflict"))
        row = self.row()
        self.assertEqual((row["status"], row["landing"]["state"], row["landing"]["files"]), ("active", "conflict", ["a.md"]))
        self.assertTrue(row["landing"]["told"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "workspace:40")
        self.assertIn("rt-wt landing conflict", self.sent[0][1])
        self.assertIn("a.md", self.sent[0][1])
        self.assertIn(str(self.path), self.sent[0][1])
        self.assertEqual(self.notified, [], "nothing reaches the origin while the window is open")
        self.assertEqual(self.recorded, [])
        # A rerun does not extend the window and does not repeat the tab message.
        first_until = row["landing"]["until"]
        self.clock = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.finish()
        self.assertEqual(self.row()["landing"]["until"], first_until)
        self.assertEqual(len(self.sent), 1)

    def test_landing_inside_the_window_delivers_one_done_report(self) -> None:
        self.finish()
        self.hold_report()
        self.resolve_conflict()
        code, outcome = self.finish()
        self.assertEqual((code, outcome["landed"], outcome.get("landed_in_grace")), (0, True, True))
        self.assertEqual(self.notified, [("rt-wt", "done", self.REPORT)])
        self.assertEqual(self.recorded, [("rt-wt", "done")])
        row = self.row()
        self.assertEqual((row["status"], row["summary"]), ("done", self.REPORT))
        self.assertNotIn("landing", row)
        self.assertEqual(row["landed_commit"], outcome["commit"])
        self.assertIn("branch version, resolved", (self.repo / "a.md").read_text())
        # The sweep afterwards has nothing to settle and sends nothing more.
        self.assertEqual(dispatch.expire_conflict_windows(dispatch.config()), {"landed": [], "failed": [], "open": []})
        self.assertEqual(len(self.notified), 1)

    def test_sweep_notices_a_hand_landing_inside_the_window(self) -> None:
        # The worker rebased and fast-forwarded main itself without rerunning finish.
        self.finish()
        self.hold_report()
        self.resolve_conflict()
        self._git("merge", "--ff-only", self.branch)
        result = dispatch.expire_conflict_windows(dispatch.config())
        self.assertEqual(result, {"landed": ["rt-wt"], "failed": [], "open": []})
        self.assertEqual(self.notified, [("rt-wt", "done", self.REPORT)])
        row = self.row()
        self.assertEqual((row["status"], row["summary"]), ("done", self.REPORT))
        self.assertTrue(row["landed_commit"])
        self.assertNotIn("landing", row)

    def test_lapse_delivers_one_failed_report_in_the_landing_failure_shape(self) -> None:
        self.finish()
        self.hold_report()
        before = dispatch.expire_conflict_windows(dispatch.config())
        self.assertEqual(before, {"landed": [], "failed": [], "open": ["rt-wt"]})
        self.assertEqual(self.notified, [])
        self.assertEqual(self.row()["status"], "active")
        self.clock = datetime.now(timezone.utc) + timedelta(seconds=601)
        after = dispatch.expire_conflict_windows(dispatch.config())
        self.assertEqual(after, {"landed": [], "failed": ["rt-wt"], "open": []})
        self.assertEqual(len(self.notified), 1)
        tid, status, text = self.notified[0]
        self.assertEqual((tid, status), ("rt-wt", "failed"))
        self.assertTrue(text.startswith("failed: could not land on main (rebase conflict on branch task/rt-wt (a.md)"), text)
        self.assertIn(f"resolve in {self.path} then run dispatch.py finish --task-id rt-wt", text)
        self.assertTrue(text.endswith(f"Worker result stands: {self.REPORT}"), text)
        self.assertEqual(self.recorded, [("rt-wt", "failed")])
        row = self.row()
        self.assertEqual((row["status"], row["summary"]), ("failed", text))
        self.assertNotIn("landing", row)
        # finish later corrects it to done through the existing landing-failure path.
        self.assertEqual(dispatch.landing_failure_result(row), self.REPORT)
        # A second sweep is idempotent.
        self.assertEqual(dispatch.expire_conflict_windows(dispatch.config()), {"landed": [], "failed": [], "open": []})
        self.assertEqual(len(self.notified), 1)

    def test_reap_leaves_the_tab_open_during_the_window(self) -> None:
        self.finish()
        self.hold_report()
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(result["closed"], [])
        self.assertEqual(self.closed, [])
        self.assertTrue(self.path.is_dir())
        self.assertIn(self.branch, self._git("branch", "--list", self.branch), "branch survives the window")

    def test_sweep_does_not_nudge_a_row_in_landing_state(self) -> None:
        self.finish()
        self.hold_report()
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        stale = iso(two_hours_ago)
        ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-wt").update(heartbeat_at=stale, updated_at=stale))
        self.agents["T-dead"]["state_ts"] = two_hours_ago.timestamp()
        self.assertEqual(dispatch.nudge_idle(dispatch.config()), {"resumed": [], "escalated": []})
        self.assertEqual(len(self.sent), 1, "the one conflict message is the only tab text")

    def test_steer_during_the_window_targets_the_live_thread(self) -> None:
        self.finish()
        self.hold_report()
        decision = dispatch.steer_decision(self.row(), self.agents)
        self.assertEqual((decision["action"], decision["thread_id"], decision["thread_live"]), ("message", "T-dead", True))

    def test_grace_zero_fails_at_once_as_before(self) -> None:
        with patch.object(dispatch, "config", lambda: {**dispatch.DEFAULTS, "conflict_grace_seconds": 0}):
            code, outcome = self.finish()
        self.assertEqual((code, outcome["landed"], outcome.get("conflict")), (1, False, True))
        self.assertNotIn("grace", outcome)
        self.assertNotIn("landing", self.row())
        self.assertEqual(self.sent, [])


class ClaudeAgentLaunchTests(unittest.TestCase):
    """SWITCHBOARD_AGENT=claude: the worker launchers start Claude Code
    instead of Amp. Wrong impls: the Amp command string changes shape when the
    variable is unset or `amp`; a Claude row misses --dangerously-skip-permissions
    (the worker would stall on its first tool prompt), the mapped model/effort, or
    the session id the ledger recorded; a Claude startup dialog counts as the TUI
    being ready; the transcript readers crash on a session uuid or return
    something other than the Amp readers' None when the file does not exist."""

    ROW = {"workstream": "switchboard-maintenance", "attempt_id": "a1", "worker_id": "w1", "id": "rt-claude", "worktree": "/tmp/wt",
           "mode": "ultra"}
    SID = "0fd3afb0-1234-4abc-8def-0123456789ab"
    CLAUDE_READY = ("────────────────────────────────────────────\n"
                    "❯ Try \"write a test for cmux-helpers.sh\"\n"
                    "────────────────────────────────────────────\n"
                    "  ⏵⏵ auto mode on (shift+tab to cycle)")
    CLAUDE_DIALOG = ("WARNING: Claude Code running in Bypass Permissions mode\n"
                     " ❯ No, exit\n   Yes, I accept\n Enter to confirm · Esc to cancel")

    def amp_command(self, row: dict[str, Any]) -> str:
        prompt = dispatch.LAUNCH_PROMPTS / f"{row['workstream']}-{row['attempt_id']}.md"
        # Windows: typed paths use forward slashes (the shim reads \r \n \t as keys).
        prompt_text = prompt.as_posix() if os.name == "nt" else str(prompt)
        worktree = Path(str(row['worktree'])).as_posix() if os.name == "nt" else row['worktree']
        worktree = shlex.quote(str(worktree))
        env = (f"SWITCHBOARD_WORKSTREAM_SLUG={row['workstream']} SWITCHBOARD_WORKER_ID={row['worker_id']} SWITCHBOARD_WORKER_TASK_ID={row['id']} "
               f"SWITCHBOARD_WORKER_ATTEMPT_ID={row['attempt_id']} SWITCHBOARD_WORKTREE={worktree} SWITCHBOARD_AGENT_MODE={row['mode']}")
        return f"cat {shlex.quote(prompt_text)} | {env} amp -m {row['mode']}"

    def test_agent_kind_precedence(self) -> None:
        """SWITCHBOARD_AGENT env, then router.yaml
        `agent:`, then claude. Wrong impls: the old amp default; config ignored;
        env not winning over config; an unrecognised env value forcing amp."""
        with tempfile.TemporaryDirectory() as tmp:
            amp_cfg, claude_cfg, none_cfg = (Path(tmp) / n for n in ("amp.yaml", "claude.yaml", "none.yaml"))
            amp_cfg.write_text("agent: amp\nrouter:\n  mode: ultra\n")
            claude_cfg.write_text("agent: claude\n")
            none_cfg.write_text("router:\n  agent: amp  # nested keys are not the knob\n")
            cases = [(None, amp_cfg, "amp"), (None, claude_cfg, "claude"), (None, none_cfg, "claude"),
                     (None, Path(tmp) / "missing.yaml", "claude"), ("claude", amp_cfg, "claude"),
                     (" AMP ", claude_cfg, "amp"), ("", amp_cfg, "amp"), ("orb", amp_cfg, "amp"), ("orb", none_cfg, "claude")]
            for value, cfg, expected in cases:
                env = {"ROUTER_CONFIG": str(cfg)} | ({} if value is None else {"SWITCHBOARD_AGENT": value})
                with patch.dict(os.environ, env, clear=False):
                    if value is None:
                        os.environ.pop("SWITCHBOARD_AGENT", None)
                    self.assertEqual(dispatch.agent_kind(), expected, f"SWITCHBOARD_AGENT={value!r} config={cfg.name}")

    def test_sweep_classifies_by_the_row_agent(self) -> None:
        """A row keeps the agent it launched with: flipping the knob mid-launch
        never reads an Amp tab as a stalled Claude tab or the reverse."""
        with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "claude"}):
            self.assertEqual(dispatch.classify_screen(LaunchVerificationTests.AMP, "amp"), "amp")
            self.assertEqual(dispatch.classify_screen(self.CLAUDE_READY, "amp"), "unknown")
        with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "amp"}):
            self.assertEqual(dispatch.classify_screen(self.CLAUDE_READY, "claude"), "amp")

    def test_mode_table(self) -> None:
        self.assertEqual(dispatch.claude_mode_flags("ultra"), ("opus", "high"))
        self.assertEqual(dispatch.claude_mode_flags("high"), ("opus", "high"))
        self.assertEqual(dispatch.claude_mode_flags("medium"), ("sonnet", "medium"))
        self.assertEqual(dispatch.claude_mode_flags("low"), ("sonnet", "low"))
        self.assertEqual(dispatch.claude_mode_flags("nonsense"), ("opus", "high"), "unknown modes take the ultra row")
        self.assertEqual(dispatch.claude_mode_flags(""), ("opus", "high"))
        self.assertEqual(set(dispatch.CLAUDE_MODE_FLAGS), {"ultra", "high", "medium", "low"})

    def test_amp_launch_command_is_unchanged(self) -> None:
        for row in (dict(self.ROW), {**self.ROW, "agent": "amp"}):
            with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "claude"}):
                # launch() stamps the agent on the row; the builder never consults the
                # environment, so a retype of an Amp row stays Amp whatever the env says.
                self.assertEqual(dispatch.launch_command_for(row), self.amp_command(row))
            self.assertEqual(dispatch.launch_command_for(row), self.amp_command(row))
        self.assertEqual(dispatch.launch_command_for({**self.ROW, "mode": "medium"}), self.amp_command({**self.ROW, "mode": "medium"}))

    def test_claude_launch_command(self) -> None:
        row = {**self.ROW, "agent": "claude", "session_id": self.SID}
        command = dispatch.launch_command_for(row)
        prompt = dispatch.LAUNCH_PROMPTS / "switchboard-maintenance-a1.md"
        self.assertTrue(command.startswith("SWITCHBOARD_WORKSTREAM_SLUG=switchboard-maintenance SWITCHBOARD_WORKER_ID=w1 "), command)
        self.assertIn(" SWITCHBOARD_AGENT_MODE=ultra claude --dangerously-skip-permissions --no-chrome --model opus --effort high ", command)
        self.assertIn(f" --session-id {self.SID} ", command)
        self.assertIn(f" SWITCHBOARD_WORKER_SESSION_ID={self.SID} ", command, "task-liveness fences the env ack to this session")
        if os.name == "nt":   # the inline prompt would exceed the Windows command-line limit
            self.assertTrue(command.endswith(shlex.quote(f"Read and follow all instructions in {prompt.as_posix()}")), command)
        else:
            self.assertTrue(command.endswith(f'"$(cat {shlex.quote(str(prompt))})"'), "the seed prompt is a positional argument, not stdin")
        self.assertNotIn(" amp ", command)
        self.assertNotIn("| ", command, "stdin would put Claude into -p print mode")
        for mode, flags in [("high", "--model opus --effort high"), ("medium", "--model sonnet --effort medium"),
                            ("low", "--model sonnet --effort low")]:
            self.assertIn(f" SWITCHBOARD_AGENT_MODE={mode} claude --dangerously-skip-permissions --no-chrome {flags} ",
                          dispatch.launch_command_for({**row, "mode": mode}))
        self.assertNotIn("--session-id", dispatch.launch_command_for({**row, "session_id": ""}))
        self.assertNotIn("SWITCHBOARD_WORKER_SESSION_ID", dispatch.launch_command_for({**row, "session_id": ""}))

    def test_classify_screen_by_agent(self) -> None:
        amp_screen = LaunchVerificationTests.AMP
        with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "claude"}):
            self.assertEqual(dispatch.classify_screen(self.CLAUDE_READY), "amp", "the shared label means 'agent TUI up'")
            self.assertEqual(dispatch.classify_screen("  ? for shortcuts"), "amp")
            self.assertEqual(dispatch.classify_screen(self.CLAUDE_DIALOG), "unknown", "a startup dialog is never typed into")
            self.assertEqual(dispatch.classify_screen(LaunchVerificationTests.STARSHIP), "shell", "a bare ❯ is a shell prompt")
            self.assertEqual(dispatch.classify_screen(amp_screen), "unknown", "an Amp box is not a ready Claude")
        with patch.dict(os.environ, {"SWITCHBOARD_AGENT": "amp"}):
            self.assertEqual(dispatch.classify_screen(amp_screen), "amp")
            self.assertEqual(dispatch.classify_screen(self.CLAUDE_READY), "unknown")

    def test_claude_transcript_readers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            (projects / "-Users-x-proj").mkdir(parents=True)
            with patch.object(dispatch, "CLAUDE_PROJECTS", projects):
                self.assertIsNone(dispatch.claude_transcript_tail(self.SID), "no file yet: same None as the Amp readers")
                self.assertIsNone(dispatch.thread_idle_since(self.SID))
                self.assertIsNone(dispatch.thread_last_activity(self.SID))
                self.assertIsNone(dispatch.claude_transcript_path("not-a-uuid"))
                transcript = projects / "-Users-x-proj" / f"{self.SID}.jsonl"
                records = [
                    {"type": "user", "timestamp": "2026-11-01T10:00:00.000Z", "message": {"role": "user", "content": "hi"}},
                    {"type": "assistant", "timestamp": "2026-11-01T10:00:05.000Z",
                     "message": {"role": "assistant", "stop_reason": "tool_use",
                                 "content": [{"type": "text", "text": "looking"}, {"type": "tool_use", "name": "Bash"}]}},
                    {"type": "assistant", "timestamp": "2026-11-01T10:00:09.000Z", "isSidechain": True,
                     "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "subagent"}]}},
                    {"type": "assistant", "timestamp": "2026-11-01T10:00:10.000Z",
                     "message": {"role": "assistant", "stop_reason": "end_turn",
                                 "content": [{"type": "text", "text": "done: "}, {"type": "text", "text": "all good"}]}},
                ]
                transcript.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n")
                tail = dispatch.claude_transcript_tail(self.SID)
                self.assertEqual((tail["text"], tail["stop_reason"], tail["last_role"]), ("done: \nall good", "end_turn", "assistant"))
                idle = dispatch.thread_idle_since(self.SID)
                self.assertEqual(idle, datetime(2026, 11, 1, 10, 0, 10, tzinfo=timezone.utc))
                self.assertEqual(dispatch.thread_last_activity(self.SID), idle)
                # A trailing user record (prompt or tool result not yet answered) is mid-turn.
                with transcript.open("a") as handle:
                    handle.write(json.dumps({"type": "user", "timestamp": "2026-11-01T10:00:20.000Z",
                                             "message": {"role": "user", "content": [{"type": "tool_result"}]}}) + "\n")
                self.assertIsNone(dispatch.thread_idle_since(self.SID))
                self.assertEqual(dispatch.thread_last_activity(self.SID), datetime(2026, 11, 1, 10, 0, 20, tzinfo=timezone.utc))
                # An assistant message that stopped for a tool call is still in flight.
                transcript.write_text(json.dumps(records[1]) + "\n")
                self.assertIsNone(dispatch.thread_idle_since(self.SID))

    def test_claude_worker_binding_recorded_at_launch(self) -> None:
        """A Claude worker has no in-tab observer to bind its session id, so the
        launcher records the binding itself (that is what lets a resume verify).
        Wrong impls: the row misses the uuid or the `claude` agent tag, or a
        missing cmux tab id raises and fails the launch instead of logging."""
        with tempfile.TemporaryDirectory() as tmp:
            bindings = Path(tmp) / "bindings.json"
            with patch.dict(os.environ, {"WORKSTREAM_THREAD_BINDINGS_FILE": str(bindings)}), \
                    patch.object(dispatch, "worker_tab", return_value={"id": "42", "ref": "workspace:42"}), \
                    patch.object(dispatch, "log") as log:
                row = dispatch.record_claude_worker_binding("rt-claude", "switchboard-maintenance", self.SID, "workspace:42")
                self.assertEqual((row["thread_id"], row["agent"], row["role"], row["workspace_id"]),
                                 (self.SID, "claude", "worker", "42"))
                self.assertEqual(row["evidence"]["task_id"], "rt-claude")
                from workstream_thread_bindings import binding_provenance
                self.assertEqual(binding_provenance(dispatch.REPO_ROOT, self.SID, "switchboard-maintenance"), "verified")
                self.assertEqual(binding_provenance(dispatch.REPO_ROOT, self.SID, "other-slug"), "unverified")
                log.assert_not_called()
            with patch.dict(os.environ, {"WORKSTREAM_THREAD_BINDINGS_FILE": str(bindings)}), \
                    patch.object(dispatch, "worker_tab", return_value=None), \
                    patch.object(dispatch, "cmux_workspaces", return_value=[]), \
                    patch.object(dispatch, "BINDING_LOOKUP_PAUSES", (0, 0)), \
                    patch.object(dispatch, "log") as log:
                self.assertIsNone(dispatch.record_claude_worker_binding("rt-claude", "switchboard-maintenance", self.SID, "workspace:42"))
                self.assertIn("not recorded", log.call_args[0][0])
                self.assertIn("cmux listed 0 tabs", log.call_args[0][0])

    def test_claude_worker_binding_retries_and_matches_the_ref(self) -> None:
        """Wrong impl (most Claude launches logged "no cmux tab id"): one list right after
        new-workspace, matched only by description, so a list that does not
        show the new tab yet (or shows it before its description) loses the
        binding for the life of the worker."""
        lists = [[], [{"id": "TAB-42", "ref": "workspace:42", "description": ""}]]
        with tempfile.TemporaryDirectory() as tmp:
            bindings = Path(tmp) / "bindings.json"
            with patch.dict(os.environ, {"WORKSTREAM_THREAD_BINDINGS_FILE": str(bindings)}), \
                    patch.object(dispatch, "cmux_workspaces", side_effect=lambda *a, **k: lists.pop(0) if lists else []), \
                    patch.object(dispatch, "BINDING_LOOKUP_PAUSES", (0, 0, 0)), \
                    patch.object(dispatch, "log") as log:
                row = dispatch.record_claude_worker_binding("rt-claude", "switchboard-maintenance", self.SID, "workspace:42")
                self.assertEqual(row["workspace_id"], "TAB-42")
                log.assert_not_called()


class DeferredScaffoldTests(unittest.TestCase):
    """The async ack writes only the ledger, so the CoS window stays uncluttered. A new
    workstream is planned offline in the ack (new-workstream.py --dry-run) and scaffolded by the detached launcher, so the CoS
    tool call shows no workstreams/ diffs. Each test names the wrong implementation it catches."""

    PROMPT = "Can you build a Community QA prep doc for Thursday"

    def setUp(self) -> None:
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWITCHBOARD_DISPATCH_SYNC", None)
        write_ledger()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "workstreams" / "switchboard-maintenance").mkdir(parents=True)
        (self.repo / "workstreams" / "switchboard-maintenance" / "CONTEXT.md").write_text("# Switchboard Maintenance\n")
        (self.repo / "workstreams" / "README.md").write_text("# Workstreams\n")
        self.calls: list[list[str]] = []
        real_run = dispatch.run
        for p in [patch.object(dispatch, "REPO_ROOT", self.repo),
                  patch.object(dispatch, "run", side_effect=lambda cmd, **kw: self.fake_run(real_run, cmd, **kw)),
                  patch.object(dispatch, "spawn_launcher", return_value={"pid": 4321, "log": str(TMP / "launch.log")}),
                  patch.object(dispatch, "config", return_value=dict(dispatch.DEFAULTS)),
                  patch.object(ledger, "live_agents", return_value={})]:
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, real_run, cmd, **kw):
        cmd = [str(c) for c in cmd]
        if len(cmd) > 1 and cmd[1].endswith("new-workstream.py"):
            self.calls.append(cmd)
            slug = cmd[cmd.index("--name") + 1] if "--name" in cmd else getattr(self, "derived", "community-qa")
            if "--dry-run" in cmd:
                quality = getattr(self, "quality", "ask")
                return argparse.Namespace(stdout=json.dumps({"slug": slug, "name": dispatch.titleize(slug).replace("Qa", "QA"),
                                                             "name_quality": quality, "exists": False, "dry_run": True}),
                                          stderr="", returncode=0)
            folder = self.repo / "workstreams" / slug
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "CONTEXT.md").write_text("# created\n")
            (folder / "config.yaml").write_text(f"name: {dispatch.titleize(slug)}\nemoji: 🎤\n")
            (self.repo / "workstreams" / "README.md").write_text("# Workstreams (re-rendered)\n")
            return argparse.Namespace(stdout=json.dumps({"slug": slug, "created": True, "name_quality": "name"}),
                                      stderr="", returncode=0)
        if len(cmd) > 1 and cmd[1].endswith("router-context-pack.py"):
            pack = {"candidate_owners": [{"slug": "switchboard-maintenance", "score": 5, "evidence": ["token overlap"]}]}
            return argparse.Namespace(stdout=json.dumps(pack), stderr="", returncode=0)
        if cmd[0] == "bash":   # agent_mode / review_rule source cmux-helpers.sh
            return argparse.Namespace(stdout="ultra\n", stderr="", returncode=0)
        if cmd[0] == "git":    # the fixture is a plain directory; commit_scaffold bows out
            return argparse.Namespace(stdout="", stderr="fatal: not a git repository", returncode=128)
        return real_run(cmd, **kw)

    def snapshot(self) -> list[tuple[str, int, int]]:
        root = self.repo / "workstreams"
        return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in root.rglob("*") if p.is_file())

    def test_ack_path_writes_no_files_under_workstreams(self) -> None:
        # Wrong impl: the ack runs new-workstream.py for real, so CONTEXT.md, config.yaml,
        # the inbox, and the README re-render land on main under the CoS tool call and
        # Claude Code prints every diff.
        before = self.snapshot()
        code, result = run_dispatch(prompt=[self.PROMPT], workstream=None)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["launch"], "background")
        self.assertEqual(self.snapshot(), before, "the ack must not write under workstreams/")
        self.assertFalse((self.repo / "workstreams" / "community-qa").exists())
        self.assertTrue(all("--dry-run" in c for c in self.calls), self.calls)
        # The ack still names the workstream it will create, from the derived name.
        self.assertEqual(result["workstream"], "community-qa")
        self.assertTrue(result["message"].startswith(f"[📁 Community QA · {result['task_id']}]"), result["message"])
        self.assertIn("in new workstream community-qa (derived from the prompt", result["message"])
        self.assertEqual(result["owner"]["scaffold"], "launcher")
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertEqual(row["workstream"], "community-qa")
        self.assertEqual(row["pending_workstream"]["slug"], "community-qa")

    def test_launcher_scaffolds_the_planned_workstream_before_the_worktree(self) -> None:
        # Wrong impl: the scaffold is deferred but nobody creates it, so the worker's
        # checkout has no workstreams/<slug>/ and its tab is titled with a bare slug.
        code, result = run_dispatch(prompt=[self.PROMPT], workstream=None)
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], "community-qa")
        self.assertTrue((self.repo / "workstreams" / "community-qa" / "CONTEXT.md").is_file())
        scaffold = [c for c in self.calls if "--dry-run" not in c]
        self.assertEqual(len(scaffold), 1)
        self.assertEqual(scaffold[0][scaffold[0].index("--name") + 1], "community-qa",
                         "an ask-derived name is created exactly as the ack reported it")
        stored = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertNotIn("pending_workstream", stored)
        self.assertEqual(stored["workstream"], "community-qa")
        # A second launch (a retry) finds nothing pending and scaffolds nothing.
        self.assertEqual(dispatch.materialize_pending_workstream(stored)["workstream"], "community-qa")
        self.assertEqual(len([c for c in self.calls if "--dry-run" not in c]), 1)

    def test_placeholder_name_is_re_derived_with_the_source_in_the_launcher(self) -> None:
        # Wrong impl: the offline placeholder (workstream-<hex>) the ack derived for a
        # link-only ask is scaffolded as is, though the launcher can read the linked title.
        self.derived, self.quality = "workstream-1a2b3c4d", "hash"
        code, result = run_dispatch(prompt=["https://docs.google.com/document/d/abc/edit"], workstream=None)
        self.assertEqual(result["workstream"], "workstream-1a2b3c4d")
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.derived = "acme-roadmap"
        settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], "acme-roadmap")
        self.assertNotIn("--name", [c for c in self.calls if "--dry-run" not in c][0])
        self.assertEqual(ledger.find_task(ledger.load_payload(), result["task_id"])["workstream"], "acme-roadmap")

    def test_sync_dispatch_still_scaffolds_inline(self) -> None:
        # Wrong impl: deferring in sync mode too, where launch() may be mocked and the
        # caller expects the folder when dispatch returns.
        choice = dispatch.choose_workstream(self.PROMPT, dispatch.DEFAULTS)
        self.assertEqual(choice["slug"], "community-qa")
        self.assertIsNone(choice.get("pending"))
        self.assertTrue((self.repo / "workstreams" / "community-qa" / "CONTEXT.md").is_file())

    def test_real_dry_run_writes_nothing(self) -> None:
        # Wrong impl: --dry-run is parsed but the create path still runs.
        slug = "zebra-quokka-migration"
        target = REPO_ROOT / "workstreams" / slug
        self.assertFalse(target.exists())
        out = subprocess.run([sys.executable, str(SCRIPTS / "new-workstream.py"), "Plan the zebra quokka migration",
                              "--json", "--dry-run", "--no-fetch"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((payload["slug"], payload["exists"], payload["dry_run"]), (slug, False, True))
        self.assertFalse(target.exists(), "dry run created the folder")



class DeferredLinkedTitleTests(DeferredScaffoldTests):
    """The async ack stays offline, so the launcher reads linked titles before it
    scaffolds."""

    PARTNER_PROMPT = LinkedTitleOwnerTests.PARTNER_PROMPT
    PARTNER_TITLE = LinkedTitleOwnerTests.PARTNER_TITLE

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "workstreams" / "acme-plans").mkdir(parents=True)
        (self.repo / "workstreams" / "acme-plans" / "CONTEXT.md").write_text("# Acme Plans\n")
        self.titled_owner = "acme-plans"

    def fake_run(self, real_run, cmd, **kw):
        cmd = [str(c) for c in cmd]
        if len(cmd) > 1 and cmd[1].endswith("router-context-pack.py") and any(a.startswith("--source-title=") for a in cmd):
            self.calls.append(cmd)
            owners = ([{"slug": self.titled_owner, "score": 335.6,
                        "evidence": ['source title "Acme Roadmap" names this workstream (acme)']}]
                      if self.titled_owner else [{"slug": "switchboard-maintenance", "score": 5, "evidence": ["token overlap"]}])
            return argparse.Namespace(stdout=json.dumps({"candidate_owners": owners}), stderr="", returncode=0)
        return super().fake_run(real_run, cmd, **kw)

    def ack(self):
        self.derived, self.quality = "workstream-1a2b3c4d", "hash"
        with patch.object(dispatch.link_titles, "fetch_titles") as fetch:
            code, result = run_dispatch(prompt=[self.PARTNER_PROMPT], workstream=None)
        self.assertEqual(code, 0, result)
        fetch.assert_not_called()
        return result

    def test_ack_reads_no_title_and_says_the_worker_will(self) -> None:
        # Wrong impl: the ack fetches the Drive title (1.6 s live) or reports the
        # placeholder as the final owner.
        before = self.snapshot()
        result = self.ack()
        self.assertEqual(self.snapshot(), before)
        self.assertIn("reads the linked source first", result["message"])
        self.assertFalse(any("--source-title=" in a for c in self.calls for a in c), self.calls)

    def test_launcher_routes_the_partner_doc_to_its_existing_owner(self) -> None:
        # Wrong impl (async path): the launcher scaffolds the
        # placeholder or a prompt-word name instead of reading the doc title.
        result = self.ack()
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.PARTNER_TITLE):
            settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], "acme-plans")
        self.assertEqual([c for c in self.calls if c[1].endswith("new-workstream.py") and "--dry-run" not in c], [],
                         "a title that names an owner never scaffolds")
        self.assertFalse((self.repo / "workstreams" / "workstream-1a2b3c4d").exists())
        stored = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertEqual(stored["workstream"], "acme-plans")
        self.assertNotIn("pending_workstream", stored)

    def test_launcher_names_the_new_workstream_from_an_unmatched_title(self) -> None:
        # Wrong impl: the title is read but dropped, so the scaffold re-derives
        # from the prompt words.
        self.titled_owner = ""
        result = self.ack()
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.derived = "acme-roadmap"
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.PARTNER_TITLE):
            settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], "acme-roadmap")
        scaffold = [c for c in self.calls if c[1].endswith("new-workstream.py") and "--dry-run" not in c]
        self.assertEqual(len(scaffold), 1)
        self.assertIn("--source-title=Acme Roadmap", scaffold[0])
        self.assertNotIn("--name", scaffold[0])



class PlaceholderOwnerRoutingMissTests(DeferredScaffoldTests):
    """A routing miss with an invented prompt and Slack DM
    (fixtures/routing_miss_placeholder_owner.json). Three failures, one test each:
    1. the ack must not name the placeholder `pm-pt-cos` from CoS's preamble;
    2. the launcher must not name the DM after the generic infra workstream and
       scaffold it, since report-tooling owns the nightly-report-box box;
    3. the owner already has a live primary worker, so the task joins it
       as "<Name> 2" with a coordination clause, never queues or goes elsewhere."""

    FIXTURE = json.loads((SCRIPTS / "fixtures" / "routing_miss_placeholder_owner.json").read_text(encoding="utf-8"))
    OWNER, BOX, INFRA = FIXTURE["owner"], "nightly-report-box", FIXTURE["wrong_owner"]
    PRIMARY, TITLE = FIXTURE["primary"], FIXTURE["slack_title"]

    def setUp(self) -> None:
        super().setUp()
        ws = self.repo / "workstreams"
        for slug, name, emoji, extra, context in (
                (self.OWNER, "Report Tooling", "🌐",
                 f"systems:\n  {self.INFRA}:\n    - {self.BOX}\n", "Report tracker.\n"),
                (self.INFRA, self.TITLE, "🪄", f"infra_only: true\ntodo_match_terms:\n- {self.INFRA}\n",
                 f"{self.TITLE} infrastructure.\n"),
                ("photo-book-kit", "Photo Book Kit", "🧳", "", "Windows kit.\n")):
            (ws / slug).mkdir(parents=True, exist_ok=True)
            (ws / slug / "CONTEXT.md").write_text(f"# {name}\n{context}")
            (ws / slug / "config.yaml").write_text(f"name: {name}\nemoji: {emoji}\n{extra}")
        write_ledger(dict(self.PRIMARY, attempt_id=f"{self.PRIMARY['id']}.a1.x", origin="cos"))
        self.slack = [{"url": self.FIXTURE["slack_url"], "kind": "slack", "raw": self.FIXTURE["slack_title"],
                       "title": self.FIXTURE["slack_title"], "text": self.FIXTURE["slack_text"]}]

    def fake_run(self, real_run, cmd, **kw):
        cmd = [str(c) for c in cmd]
        if len(cmd) > 1 and cmd[1].endswith("new-workstream.py") and "--dry-run" in cmd:
            self.calls.append(cmd)
            return real_run(cmd, **kw)      # the real offline naming rule
        if len(cmd) > 1 and cmd[1].endswith("router-context-pack.py") and any(a.startswith("--source-title=") for a in cmd):
            self.calls.append(cmd)          # the DM title would strong-match the generic workstream
            owners = [{"slug": self.INFRA, "score": 400, "evidence": [f'source title "{self.TITLE}" names this workstream ({self.INFRA})']}]
            return argparse.Namespace(stdout=json.dumps({"candidate_owners": owners}), stderr="", returncode=0)
        return super().fake_run(real_run, cmd, **kw)

    def ack(self) -> dict:
        with patch.object(dispatch.link_titles, "fetch_titles") as fetch:
            code, result = run_dispatch(prompt=[self.FIXTURE["prompt"]], workstream=None)
        self.assertEqual(code, 0, result)
        fetch.assert_not_called()
        return result

    def test_1_placeholder_name_never_comes_from_the_preamble(self) -> None:
        result = self.ack()
        self.assertNotEqual(result["workstream"], self.FIXTURE["wrong_placeholder"])
        self.assertRegex(result["workstream"], r"^triage-[0-9a-f]{8}$")
        self.assertTrue(result["message"].startswith("[📁 Triage "), result["message"])
        self.assertNotIn("PM PT CoS", result["message"])

    def test_2_launcher_resolves_the_box_owner_not_the_generic_infra_workstream(self) -> None:
        result = self.ack()
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.slack):
            settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], self.FIXTURE["owner"])
        self.assertNotEqual(settled["workstream"], self.FIXTURE["wrong_owner"])
        self.assertEqual([c for c in self.calls if c[1].endswith("new-workstream.py") and "--dry-run" not in c], [],
                         "a resolved owner never scaffolds")
        self.assertFalse(any(a.startswith("--source-title=") for c in self.calls for a in c),
                         "systems in the content are resolved before the title is ranked")
        stored = ledger.find_task(ledger.load_payload(), result["task_id"])
        self.assertEqual(stored["workstream"], self.OWNER)
        self.assertIn(self.BOX, stored["system_owner"]["identifiers"])
        self.assertNotIn("provisional_workstream", stored)
        self.assertFalse((self.repo / "workstreams" / result["workstream"]).exists())

    def test_3_task_joins_the_live_owner_worker_as_its_numbered_secondary(self) -> None:
        result = self.ack()
        self.assertEqual(result["status"], "routed", "never queued behind the primary")
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=self.slack):
            settled = dispatch.materialize_pending_workstream(row)
        slug = settled["workstream"]

        def bind_live_thread(payload):
            ledger.find_task(payload, self.PRIMARY["id"])["thread_id"] = "T-primary"

        ledger.locked_mutation(bind_live_thread)
        with patch.object(ledger, "live_agents", return_value={"T-primary": {"state": "Running", "workspace_id": "w-1"}}):
            primary = dispatch.secondary_primary(slug, settled["id"])
        self.assertEqual(primary["id"], self.PRIMARY["id"])
        slot = dispatch.next_worker_slot(slug, ledger.load_payload(), [{"title": self.PRIMARY["tab_title"]}])
        self.assertEqual(dispatch.worker_title(slug, slot), "🌐 Report Tooling 2")
        prompt = dispatch.launch_prompt({**settled, "secondary_to": primary, "branch": "task/x"},
                                        Path("/tmp/wt-secondary"), "worker-2")
        clause = next(p for p in prompt.split("\n\n") if p.startswith("Secondary worker:"))
        self.assertIn(f"{self.PRIMARY['tab_title']} ({self.PRIMARY['id']}) is already live in this workstream", clause)
        self.assertIn("neither queued behind it nor moved to another workstream", clause)
        self.assertIn(f"running-task-state.py show --task-id {self.PRIMARY['id']}", clause)
        self.assertIn("Do not disturb its running work", clause)
        self.assertIn(f"never edit files in its worktree {self.PRIMARY['worktree']}", clause)
        self.assertIn(f"owner_tabs.py message --ref {self.PRIMARY['workspace_ref']}", clause)
        self.assertIn(self.BOX, clause, "the clause says which system made it the owner")

    def test_a_primary_free_workstream_gets_no_secondary_clause(self) -> None:
        write_ledger()
        self.assertIsNone(dispatch.secondary_primary(self.OWNER, "rt-new"))
        task = {"id": "rt-new", "attempt_id": "rt-new.a1.x", "workstream": self.OWNER,
                "origin": "cos", "prompt": f"Workstream: {self.OWNER}\nRebuild"}
        self.assertNotIn("Secondary worker:", dispatch.launch_prompt(task, Path("/tmp/wt"), "worker-x"))

    def test_unresolved_link_keeps_the_provisional_workstream_and_the_rehome_clause(self) -> None:
        # A DM that names no owned system falls through to the old title path and the
        # worker is told how to re-home with ownership resolution.
        result = self.ack()
        row = ledger.find_task(ledger.load_payload(), result["task_id"])
        other = [dict(self.slack[0], raw="Nightly Gizmo", title="Nightly Gizmo", text="Nightly gizmo export failed")]
        self.derived = "nightly-gizmo"
        with patch.object(dispatch.link_titles, "fetch_titles", return_value=other), \
                patch.object(self, "fake_run", side_effect=lambda real_run, cmd, **kw: (
                    argparse.Namespace(stdout=json.dumps({"candidate_owners": []}), stderr="", returncode=0)
                    if str(cmd[1]).endswith("router-context-pack.py")
                    else DeferredScaffoldTests.fake_run(self, real_run, cmd, **kw))):
            settled = dispatch.materialize_pending_workstream(row)
        self.assertEqual(settled["workstream"], "nightly-gizmo")
        self.assertTrue(settled.get("provisional_workstream"))
        prompt = dispatch.launch_prompt({**settled, "branch": "task/x"}, Path("/tmp/wt"), "worker-x")
        clause = next(p for p in prompt.split("\n\n") if p.startswith("Provisional workstream:"))
        self.assertIn("system_owners.py", clause)
        self.assertIn("vm-fleet workstream owns VM infrastructure only", clause)
        self.assertIn(f"running-task-state.py update --task-id {settled['id']} --workstream <owner>", clause)



def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, os.path.join(tmp, "l"), target_is_directory=True)
            return True
        except OSError:
            return False


_CAN_SYMLINK = _can_symlink()


class WindowsSharedLinkTests(HermeticRepo):
    """On Windows without Developer Mode every `symlink_to` in link_shared_state raises
    WinError 1314, and pgrep/lsof are absent. These run the
    Windows branches on a Mac: `is_windows` is patched on, `os.symlink` raises the
    privilege error, and a "junction" is a symlink that Python's own link checks cannot
    see, which is how a junction looks to Python before 3.12 and to `shutil.rmtree`."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".gitignore").write_text(".amp/*\n/people/\n/local.json\n")
        (self.repo / "people").mkdir()
        (self.repo / "people" / "alice.md").write_text("# Alice\n")
        (self.repo / "local.json").write_text("{}\n")
        (self.repo / ".amp" / "locks").mkdir(parents=True, exist_ok=True)
        (self.repo / ".amp" / "locks" / "x.lock").write_text("lock\n")
        self.junctions: set[str] = set()
        self.real_symlink, self.real_islink = os.symlink, os.path.islink

    def windows(self, *, hardlinks: bool = True):
        """Patches for a Windows user with no symlink privilege."""
        from contextlib import ExitStack
        privilege = OSError(1314, "A required privilege is not held by the client")
        privilege.winerror = 1314   # type: ignore[attr-defined]  (only Windows sets it)

        def fake_junction(source, link):
            self.real_symlink(str(source), str(link), target_is_directory=True)
            self.junctions.add(str(link))

        stack = ExitStack()
        for p in (patch.object(dispatch.winlink, "is_windows", return_value=True),
                  patch.object(dispatch.winlink.os, "symlink", side_effect=privilege),
                  patch.object(dispatch.winlink, "create_junction", side_effect=fake_junction),
                  patch.object(dispatch.winlink, "is_junction", side_effect=lambda path: str(path) in self.junctions),
                  # Python before 3.12 and shutil.rmtree see a junction as a plain directory.
                  patch.object(dispatch.winlink.os.path, "islink", side_effect=lambda path: False if str(path) in self.junctions else self.real_islink(path))):
            stack.enter_context(p)
        if not hardlinks:
            stack.enter_context(patch.object(dispatch.winlink.os, "link", side_effect=OSError(18, "Invalid cross-device link")))
        self.addCleanup(stack.close)
        return stack

    @unittest.skipUnless(_CAN_SYMLINK, "the fake junction is a real symlink, which needs Developer Mode or admin")
    def test_symlink_privilege_error_picks_a_junction_for_dirs_and_a_hard_link_for_files(self) -> None:
        # Wrong impl: let WinError 1314 escape, so create_worktree fails on every Windows launch.
        with self.windows():
            path, _ = dispatch.create_worktree("rt-win")
        self.assertIn(str(path / "people"), self.junctions)
        self.assertIn(str(path / ".amp" / "locks"), self.junctions)
        self.assertEqual((path / "people" / "alice.md").read_text(), "# Alice\n")
        self.assertEqual(os.stat(path / "local.json").st_ino, os.stat(self.repo / "local.json").st_ino)   # one file, two names
        self.assertGreaterEqual(os.stat(path / "local.json").st_nlink, 2)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        for rel in ("/people", "/local.json", "/.amp/locks"):
            self.assertIn(rel, exclude, "the exclude bookkeeping lists junctions and hard links like symlinks")
        self.assertEqual(self._git("status", "--porcelain", cwd=path).strip(), "")
        self.assertEqual(dispatch.worktree_dirty(path), [])

    @unittest.skipUnless(_CAN_SYMLINK, "the fake junction is a real symlink, which needs Developer Mode or admin")
    def test_junctions_and_hard_links_are_not_the_workers_untracked_work(self) -> None:
        # Wrong impl: only a symlink counts as a shared-state link, so a junction or a
        # hard link shows as the worker's work and parks every worktree instead of dropping it.
        with self.windows():
            path, _ = dispatch.create_worktree("rt-work")
            (path / "mine.txt").write_text("real work\n")
            self.assertTrue(dispatch._shared_state_link(path, "?? people/"))
            self.assertTrue(dispatch._shared_state_link(path, "?? local.json"))
            self.assertFalse(dispatch._shared_state_link(path, "?? mine.txt"))
            self.assertFalse(dispatch._shared_state_link(self.repo, "?? local.json"), "main's own files are not links")

    @unittest.skipUnless(_CAN_SYMLINK, "the fake junction is a real symlink, which needs Developer Mode or admin")
    def test_a_file_that_cannot_be_linked_is_skipped_never_copied(self) -> None:
        # Wrong impl: fall back to copying, which forks the shared file from main's.
        with self.windows(hardlinks=False):
            path, _ = dispatch.create_worktree("rt-vol")
        self.assertFalse(os.path.lexists(path / "local.json"))
        self.assertIn(str(path / "people"), self.junctions, "directories still link")
        self.assertNotIn("/local.json", (self.repo / ".git" / "info" / "exclude").read_text().splitlines())

    def test_todo_json_is_marked_skip_worktree_only_once_its_link_is_in_place(self) -> None:
        # Wrong impl: set skip-worktree before the swap, so a failed link hides real todo
        # edits; or trip over a .link-tmp an interrupted run left behind.
        (self.repo / "system" / "state").mkdir(parents=True, exist_ok=True)
        (self.repo / "system" / "state" / "todo.json").write_text('{"items": []}\n')
        self._git("add", "-f", "--", "system/state/todo.json")
        self._git("commit", "-q", "-m", "todo")
        todo = Path("system/state/todo.json")
        with self.windows(hardlinks=False):
            path, _ = dispatch.create_worktree("rt-todo")
        self.assertTrue(self._git("ls-files", "-v", "--", str(todo), cwd=path).startswith("H "))
        self.assertFalse(os.path.lexists(path / "system/state/todo.json.link-tmp"))
        (path / "system/state/todo.json.link-tmp").write_text("stale\n")
        with self.windows():
            dispatch.link_shared_state(path)
        self.assertEqual(os.stat(path / todo).st_ino, os.stat(self.repo / todo).st_ino)
        self.assertTrue(self._git("ls-files", "-v", "--", str(todo), cwd=path).startswith("S "))
        self.assertFalse(os.path.lexists(path / "system/state/todo.json.link-tmp"))

    def test_privilege_error_is_only_swallowed_on_windows(self) -> None:
        privilege = OSError(1314, "x")
        privilege.winerror = 1314   # type: ignore[attr-defined]
        with patch.object(dispatch.winlink, "is_windows", return_value=False), \
                patch.object(dispatch.winlink.os, "symlink", side_effect=privilege):
            with self.assertRaises(OSError):
                dispatch.winlink.link_path(self.repo / "people", self.repo / "elsewhere")

    def test_drop_unlinks_every_junction_before_anything_is_deleted_and_main_survives(self) -> None:
        # Wrong impl: `git worktree remove --force` / rmtree run with the junctions in place and
        # walk through them into main's people/ and .amp/locks (the data loss this guards).
        with self.windows():
            path, branch = dispatch.create_worktree("rt-drop")
            seen: list[list[str]] = []
            real_git = dispatch.git

            def spy(*args, **kw):
                if args[:2] == ("worktree", "remove"):
                    seen.append([rel for rel in ("people", ".amp/locks") if os.path.lexists(path / rel)])
                return real_git(*args, **kw)

            with patch.object(dispatch, "git", spy):
                parked = dispatch.drop_worktree({"id": "rt-drop", "worktree": str(path), "branch": branch})
        self.assertIsNone(parked)
        self.assertEqual(seen, [[]], "no junction was left in the checkout when git removed it")
        self.assertFalse(path.exists())
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice\n")
        self.assertEqual((self.repo / ".amp" / "locks" / "x.lock").read_text(), "lock\n")
        self.assertEqual((self.repo / "local.json").read_text(), "{}\n")

    def test_recreating_a_worktree_in_place_does_not_follow_junctions_either(self) -> None:
        with self.windows():
            path, _ = dispatch.create_worktree("rt-again")
            dispatch.create_worktree("rt-again")   # path exists: removed and rebuilt
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice\n")
        self.assertEqual((self.repo / ".amp" / "locks" / "x.lock").read_text(), "lock\n")

    @unittest.skipUnless(_CAN_SYMLINK, "the fake junction is a real symlink, which needs Developer Mode or admin")
    def test_rmtree_unlinks_a_junction_and_leaves_its_target(self) -> None:
        root = self.repo / "scratch-tree"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "own.txt").write_text("mine\n")
        link = root / "sub" / "shared"
        with self.windows():
            dispatch.winlink.link_path(self.repo / "people", link)
            self.assertIn(str(link), self.junctions)
            dispatch.winlink.rmtree(root)
        self.assertFalse(root.exists())
        self.assertEqual((self.repo / "people" / "alice.md").read_text(), "# Alice\n")

    def test_remove_link_uses_rmdir_for_a_real_junction(self) -> None:
        empty = self.repo / "pretend-junction"
        empty.mkdir()
        self.junctions.add(str(empty))
        with patch.object(dispatch.winlink, "is_windows", return_value=True), \
                patch.object(dispatch.winlink, "is_junction", side_effect=lambda p: str(p) in self.junctions), \
                patch.object(dispatch.winlink.os.path, "islink", return_value=False):
            dispatch.winlink.remove_link(empty)
        self.assertFalse(empty.exists())

    def test_pid_alive_probes_without_signalling(self) -> None:
        self.assertTrue(dispatch.winlink.pid_alive(os.getpid()))
        self.assertFalse(dispatch.winlink.pid_alive(0))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(dispatch.winlink.pid_alive(dead.pid))


class FsmonitorWithoutPosixToolsTests(HermeticRepo):
    """pgrep and lsof do not exist for Windows Python (WinError 2 from `run`), which broke
    drop_worktree, settle_checkout and the four launch-failure paths that drop a checkout."""

    def boom(self, cmd, **kw):
        raise AssertionError(f"no process listing may run here: {cmd}")

    def test_no_pgrep_means_no_daemon_table(self) -> None:
        which = lambda name: None if name == "pgrep" else f"/usr/bin/{name}"
        with patch.object(dispatch.shutil, "which", which), patch.object(dispatch, "run", self.boom):
            self.assertEqual(dispatch.fsmonitor_daemons(), {})
            self.assertIsNone(dispatch.fsmonitor_daemon_pid(self.path))

    def test_no_lsof_means_no_daemon_table(self) -> None:
        which = lambda name: None if name == "lsof" else f"/usr/bin/{name}"
        with patch.object(dispatch.shutil, "which", which), patch.object(dispatch, "run", self.boom):
            self.assertEqual(dispatch.fsmonitor_daemons(), {})

    def test_windows_has_no_daemon_table(self) -> None:
        with patch.object(dispatch.winlink, "is_windows", return_value=True), patch.object(dispatch, "run", self.boom):
            self.assertEqual(dispatch.fsmonitor_daemons(), {})

    def test_a_tool_that_vanishes_between_the_check_and_the_call_is_not_fatal(self) -> None:
        with patch.object(dispatch, "run", side_effect=FileNotFoundError(2, "The system cannot find the file specified")):
            self.assertEqual(dispatch.fsmonitor_daemons(), {})

    def test_stop_still_asks_git_to_stop_the_daemon_when_nothing_can_be_listed(self) -> None:
        calls: list[list[str]] = []
        fake = lambda cmd, **kw: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 1, "", "not running")
        with patch.object(dispatch.winlink, "is_windows", return_value=True), patch.object(dispatch, "run", fake):
            self.assertIsNone(dispatch.stop_fsmonitor_daemon(self.path))
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][:3], ["git", "-C", str(self.path)])
        self.assertEqual(calls[0][-2:], ["fsmonitor--daemon", "stop"])

    def test_drop_worktree_completes_without_pgrep_or_lsof(self) -> None:
        # The report's 22 errors: drop_worktree raised FileNotFoundError out of pgrep.
        shutil_which = dispatch.shutil.which
        which = lambda name: None if name in ("pgrep", "lsof") else shutil_which(name)
        path, branch = dispatch.create_worktree("rt-nopgrep")
        with patch.object(dispatch.shutil, "which", which):
            self.assertIsNone(dispatch.drop_worktree({"id": "rt-nopgrep", "worktree": str(path), "branch": branch}))
        self.assertFalse(path.exists())


class RunStdinTests(unittest.TestCase):
    """Windows install report v3, bug 2: a headless task has no stdin handle, so a child that
    inherits it fails with WinError 6. `run` gives every child a closed stdin unless it feeds input."""

    def capture(self, **kw) -> dict:
        seen: dict = {}
        with patch.object(dispatch.subprocess, "run", side_effect=lambda cmd, **k: seen.update(k) or subprocess.CompletedProcess(cmd, 0, "", "")):
            dispatch.run(["true"], **kw)
        return seen

    def test_children_get_devnull_stdin_by_default(self) -> None:
        self.assertIs(self.capture()["stdin"], subprocess.DEVNULL)

    def test_input_is_not_combined_with_stdin(self) -> None:
        seen = self.capture(input="x")
        self.assertNotIn("stdin", seen)   # subprocess.run raises ValueError when both are given
        self.assertEqual(seen["input"], "x")

    def test_an_explicit_stdin_is_kept(self) -> None:
        self.assertIs(self.capture(stdin=subprocess.PIPE)["stdin"], subprocess.PIPE)


# PlaceholderOwnerRoutingMissTests reuses DeferredScaffoldTests' fixture, not its tests
# (its fake_run runs the real offline naming rule, which those tests stub).
for _name in [n for n in vars(DeferredScaffoldTests) if n.startswith("test_")]:
    setattr(PlaceholderOwnerRoutingMissTests, _name, None)


class WmuxLaunchSubmitTests(HermeticRepo):
    """On Windows/wmux the launch line can be typed into the new tab with its Enter
    lost, and wmux reads a never-shown tab as blank text, so classify_screen says
    "unknown" and nothing presses Enter. Wrong impls: trust the blank screen; press Enter into a tab whose shell
    already accepted the line; retype instead of pressing Enter first; look for the
    Amp TUI on a Claude row; leave a typed-not-submitted tab to the owner."""

    def setUp(self) -> None:
        super().setUp()
        env_pin = patch.dict(os.environ, {"SWITCHBOARD_AGENT": "claude"})
        env_pin.start()
        self.addCleanup(env_pin.stop)
        self.sent: list[str] = []
        self.states: list[dict] = []
        self.clock = 0.0

    def state(self, *, shell: list[str] | None = None, provider: str | None = None, agent_prompts: int = 0,
              prompt_log: bool = True) -> dict:
        return {"surface_id": "surf-1", "prompt_log": prompt_log, "shell_prompts": shell or [],
                "agent_prompts": agent_prompts, "provider": provider, "session_id": None, "state": "unknown", "screen": ""}

    def fake_run(self, real_run):
        def run(cmd, **kw):
            if not cmd or str(cmd[0]) != dispatch.CMUX:
                return real_run(cmd, **kw)
            if "new-workspace" in cmd:
                self.command = cmd[cmd.index("--command") + 1]
                self.workspaces.append({"ref": "workspace:4091", "id": "w-91", "title": cmd[cmd.index("--name") + 1],
                                        "description": cmd[cmd.index("--description") + 1]})
                return subprocess.CompletedProcess(cmd, 0, "workspace:4091\n", "")
            if cmd[1] == "launch-state":
                source = getattr(self, "state_source", None)
                state = source() if source else (self.states.pop(0) if len(self.states) > 1 else self.states[0])
                return subprocess.CompletedProcess(cmd, 0, json.dumps(state), "")
            if cmd[1] == "send":
                self.sent.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1] == "send-key":
                self.sent.append(f"<{cmd[-1]}>")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kw)
        return run

    def sleep(self, seconds: float) -> None:
        self.clock = round(self.clock + seconds, 6)

    def launch(self, states: list[dict], task_id: str = "rt-wm") -> dict:
        self.states = list(states)
        write_ledger({"id": task_id, "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                      "attempt": 1, "attempt_id": f"{task_id}.a1.aaa", "created_at": iso(datetime.now(timezone.utc))})
        with patch.object(dispatch, "run", self.fake_run(dispatch.run)), patch.object(dispatch.time, "sleep", self.sleep), \
                patch.object(dispatch.time, "monotonic", lambda: self.clock), patch.object(dispatch, "on_wmux", lambda: True), \
                patch.object(dispatch, "AMP_VERIFY_SECONDS", 30), redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        return self.row(task_id)

    def test_classify_reads_the_prompt_log_and_the_claude_session(self) -> None:
        self.assertEqual(dispatch.classify_wmux_launch(self.state(), "rt-a", "claude"), "typed")
        self.assertEqual(dispatch.classify_wmux_launch(self.state(shell=["X=1 SWITCHBOARD_WORKER_TASK_ID=rt-a claude"]), "rt-a", "claude"),
                         "submitted")
        self.assertEqual(dispatch.classify_wmux_launch(self.state(provider="claude"), "rt-a", "claude"), "amp")
        self.assertEqual(dispatch.classify_wmux_launch(self.state(agent_prompts=1), "rt-a", "claude"), "amp")
        self.assertEqual(dispatch.classify_wmux_launch(self.state(provider="claude"), "rt-a", "amp"), "typed",
                         "a Claude session does not prove an Amp row is up")
        self.assertEqual(dispatch.classify_wmux_launch(self.state(prompt_log=False), "rt-a", "claude"), "unknown")
        self.assertEqual(dispatch.classify_wmux_launch(None, "rt-a", "claude"), "unknown")
        claude_screen = dict(self.state(), screen="❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)")
        self.assertEqual(dispatch.classify_wmux_launch(claude_screen, "rt-a", "claude"), "amp", "the Claude TUI, not the Amp one")

    def test_lost_enter_is_pressed_again_until_the_agent_is_up(self) -> None:
        # Typed until an Enter lands, then the shell has the line, then Claude is up.
        typed, accepted, up = self.state(), self.state(shell=["SWITCHBOARD_WORKER_TASK_ID=rt-wm"]), self.state(provider="claude")
        reads_after_enter: list[int] = []

        def source() -> dict:
            if "<Enter>" not in self.sent:
                return typed
            reads_after_enter.append(1)
            return accepted if len(reads_after_enter) == 1 else up

        self.state_source = source
        row = self.launch([typed])
        self.assertEqual(self.sent, ["<Enter>"], "one Enter after the grace, then the shell took the line")
        self.assertEqual((row["amp_verified"], row.get("launch_enters"), row.get("launch_retyped")), (True, 1, False))
        self.assertEqual(row["status"], "routing")

    def test_enter_presses_are_bounded_then_the_line_is_cleared_and_retyped_once(self) -> None:
        row = self.launch([self.state()])
        enters = self.sent.count("<Enter>")
        self.assertEqual(enters, dispatch.WMUX_MAX_LAUNCH_ENTERS + 1, "three Enter presses, plus the retype's own Enter")
        self.assertEqual(self.sent.count("<ctrl+u>"), 1)
        self.assertEqual(self.sent.count(self.command), 1, "retyped exactly once by the launcher")
        self.assertEqual((row["amp_verified"], row["launch_retypes"]), (False, 1))
        self.assertIn("never accepted", row["amp_verify_note"])

    def test_an_accepted_line_is_never_pressed_or_retyped(self) -> None:
        row = self.launch([self.state(shell=["SWITCHBOARD_WORKER_TASK_ID=rt-wm claude"])])
        self.assertEqual(self.sent, [])
        self.assertEqual(row["amp_verified"], "unknown")
        self.assertIn("accepted by the shell", row["amp_verify_note"])

    def test_sweep_presses_enter_on_a_typed_not_submitted_tab_then_retypes(self) -> None:
        self.launch([self.state(shell=["SWITCHBOARD_WORKER_TASK_ID=rt-wm"])])
        self.sent.clear()
        aged = iso(datetime.now(timezone.utc) - dispatch.SWEEP_RETYPE_MIN_AGE - timedelta(seconds=1))
        self.states = [self.state()]

        def age(**extra):
            ledger.locked_mutation(lambda p: ledger.find_task(p, "rt-wm").update(updated_at=aged, **extra) or ledger.find_task(p, "rt-wm"))

        with patch.object(dispatch, "run", self.fake_run(dispatch.run)), patch.object(dispatch, "on_wmux", lambda: True), \
                redirect_stdout(io.StringIO()):
            age()
            first = dispatch.retype_stalled_launches()
            self.assertEqual([(r["task_id"], r["how"]) for r in first["retyped"]], [("rt-wm", "enter")])
            self.assertEqual(self.sent, ["<Enter>"])
            age(launch_enters=dispatch.WMUX_MAX_LAUNCH_ENTERS)
            self.sent.clear()
            second = dispatch.retype_stalled_launches()
            self.assertEqual(second["retyped"][0]["how"], "retype")
            self.assertEqual(self.sent[:2], ["<ctrl+u>", dispatch.launch_command_for(self.row("rt-wm"))])
            self.states = [self.state(provider="claude")]
            age()
            self.sent.clear()
            self.assertEqual(dispatch.retype_stalled_launches()["skipped"][0]["screen"], "amp")
            self.assertEqual(self.sent, [], "a running agent is never typed into")

class VanishedTabTests(HermeticRepo):
    """A launcher's post-launch reap_all must not match a finished row to a live tab by its
    reused slot title once the wmux sidecar has lost the tab's description, or a long-cancelled
    row closes another worker's live tab. Wrong impls: trust a title; trust a ref alone; reuse a tab list read before a slow commit."""

    def finished_row(self, **extra) -> dict:
        return {"id": "rt-old", "status": "cancelled", "origin": "cos", "workstream": "switchboard-maintenance", "attempt": 1,
                "attempt_id": "rt-old.a1.aaa", "worker_id": "worker-bbbbbbbbbbbb", "tab_title": "📊 Switchboard Maintenance 2",
                "workspace_ref": "workspace:11", "finished_at": self.stale, "report_delivered": True,
                "reaped_at": self.stale, **extra}

    def live_tab(self, **extra) -> dict:
        return {"ref": "workspace:74", "id": "w-74", "title": "📊 Switchboard Maintenance 2", "description": "", **extra}

    def test_a_finished_row_never_closes_a_live_tab_that_reuses_its_slot_title(self) -> None:
        self.workspaces = [self.live_tab()]
        for status in ("cancelled", "done"):
            write_ledger(self.finished_row(status=status))
            result = dispatch.reap_all(dispatch.config())
            self.assertEqual(self.closed, [], status)
            self.assertEqual(result["closed"], [], status)
        write_ledger(self.finished_row(reaped_at=None))
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [], "a row not yet reaped settles without touching the tab")
        self.assertTrue(self.row("rt-old").get("reaped_at"))

    def test_a_finished_row_never_closes_a_tab_described_for_another_worker(self) -> None:
        self.workspaces = [self.live_tab(description="switchboard worker rt-new")]
        write_ledger(self.finished_row(workspace_ref="workspace:74", reaped_at=None))
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [], "its stale ref now names another worker's tab")

    def test_a_directory_alone_does_not_prove_an_owner_shell_or_a_pinned_tab(self) -> None:
        task_id = "rt-000000000007"
        cwd = rf"C:\x\worktrees\{task_id}\workstreams\y"
        for tab in (self.live_tab(title="🧭 CoS", current_directory=cwd), self.live_tab(pinned=True, current_directory=cwd)):
            self.workspaces = [tab]
            write_ledger(self.finished_row(id=task_id, attempt_id=f"{task_id}.a1.aaa", status="done"))
            dispatch.reap_all(dispatch.config())
            self.assertEqual(self.closed, [], tab)
        row = self.finished_row(id=task_id, status="active")
        other_agent = {"T-other": {"workspace_id": "w-74"}}
        self.assertIsNone(dispatch.resolve_live_tab(row, [self.live_tab(current_directory=cwd)], other_agent))

    def test_a_finished_rows_restored_tab_closes_by_its_directory(self) -> None:
        task_id = "rt-000000000007"
        self.workspaces = [self.live_tab(current_directory=rf"C:\x\worktrees\{task_id}\workstreams\y")]
        write_ledger(self.finished_row(id=task_id, attempt_id=f"{task_id}.a1.aaa", status="done"))
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, ["workspace:74"])

    def test_another_workers_directory_proves_nothing(self) -> None:
        self.workspaces = [self.live_tab(current_directory=r"C:\x\worktrees\rt-000000000007\workstreams\y")]
        write_ledger(self.finished_row())
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [])

    def test_the_row_still_closes_its_own_tab(self) -> None:
        self.workspaces = [self.live_tab(description="switchboard worker rt-old")]
        write_ledger(self.finished_row(status="done", reaped_at=None))
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, ["workspace:74"])

    def test_a_tab_that_lost_its_description_closes_only_with_its_own_ref_and_title(self) -> None:
        self.workspaces = [self.live_tab()]
        write_ledger(self.finished_row(status="done", reaped_at=None, workspace_ref="workspace:74"))
        dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, ["workspace:74"])

    def test_identity_is_read_again_right_before_the_close(self) -> None:
        # The launcher's description lands while record_on_main is committing.
        self.workspaces = [self.live_tab()]
        write_ledger(self.finished_row(status="done", reaped_at=None, workspace_ref="workspace:74"))

        def record(task):
            self.workspaces = [self.live_tab(description="switchboard worker rt-new")]
            return {"recorded": False}

        with patch.object(dispatch, "record_on_main", record):
            result = dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [])
        self.assertIn("another worker", result["skipped"][0]["reason"])

    def test_a_tab_a_live_row_points_at_is_never_reaped(self) -> None:
        self.workspaces = [self.live_tab()]
        write_ledger(self.finished_row(status="done", reaped_at=None, workspace_ref="workspace:74"),
                     {"id": "rt-new", "status": "active", "workspace_ref": "workspace:74", "workstream": "switchboard-maintenance",
                      "attempt_id": "rt-new.a1.aaa", "thread_id": "T-new"})
        self.agents["T-new"] = {"state": "Idle", "workspace_id": "w-other"}
        result = dispatch.reap_all(dispatch.config())
        self.assertEqual(self.closed, [])
        self.assertIn("held by live row rt-new", result["skipped"][0]["reason"])

    def test_every_close_is_recorded_with_who_asked(self) -> None:
        record = self.repo / "skip-record"
        fake = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(dispatch, "cmux_close_result", REAL_CMUX_CLOSE_RESULT), patch.object(dispatch, "CMUX_SKIP_RECORD", record), \
                patch.object(dispatch, "run", lambda cmd, **kw: fake):
            dispatch.cmux_close_result("workspace:9", "w-9", "reap rt-old")
        line = json.loads(record.with_name("tab-closes.jsonl").read_text())
        self.assertEqual((line["ref"], line["workspace_id"], line["why"]), ("workspace:9", "w-9", "reap rt-old"))


class LaunchHoldTests(HermeticRepo):
    """A launch that cannot open a tab for a passing reason waits: "could not connect to
    pipe" during a wmux restart, or "has no PTY" when reap closes its new tab mid-launch."""

    PIPE = "wmux is not running (could not connect to pipe)"
    NO_PTY = ("Error: surface surf-e637 has no PTY (pane is markdown/browser, or surface was closed). "
              "Pass an explicit surfaceId pointing at a terminal surface.")

    def queued(self, tid: str = "rt-hold") -> dict:
        return {"id": tid, "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "do it",
                "attempt": 1, "attempt_id": f"{tid}.a1.aaa", "created_at": iso(datetime.now(timezone.utc))}

    def release(self, create=None, *rows: dict) -> list[list[str]]:
        write_ledger(*(rows or (self.queued(),)))
        calls: list[list[str]] = []
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                calls.append(cmd)
                return create(cmd, **kw)
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch.time, "sleep", lambda s: None), redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        return calls

    def test_a_mux_that_is_down_at_the_tab_list_holds_with_no_worktree(self) -> None:
        def down(*a, **k):
            if k.get("strict"):
                raise dispatch.CmuxUnreachable(f"cmux_unreachable: {self.PIPE}")
            return []

        with patch.object(dispatch, "cmux_workspaces", down), \
                patch.object(dispatch, "create_worktree", side_effect=AssertionError("no checkout while the mux is down")):
            self.release(None, self.queued("rt-a"), self.queued("rt-b"), self.queued("rt-c"))
        first = self.row("rt-a")
        self.assertEqual(first["status"], "queued")
        self.assertIn("wmux is not reachable", first["summary"])
        self.assertTrue(first["launch_hold_since"])
        self.assertNotIn("launching_at", first)
        self.assertEqual(self.notified, [], "nothing failed, so nothing reports failed")
        self.assertNotIn("launch_holds", self.row("rt-b"), "one try per pass while the mux is down")
        self.assertEqual(self.row("rt-c")["status"], "queued")

    def test_strict_list_turns_a_pipe_error_into_unreachable(self) -> None:
        fail = subprocess.CompletedProcess([], 1, "", self.PIPE)
        with patch.object(dispatch, "cmux_workspaces", REAL_CMUX_WORKSPACES), patch.object(dispatch, "run", lambda cmd, **kw: fail):
            with self.assertRaises(dispatch.CmuxUnreachable):
                dispatch.cmux_workspaces("rt-x", strict=True)
            self.assertEqual(dispatch.cmux_workspaces("rt-x"), [], "a non-strict caller still gets an empty list")

    def test_a_pipe_error_at_new_workspace_holds_and_drops_the_worktree(self) -> None:
        calls = self.release(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", self.PIPE))
        self.assertEqual(len(calls), 1)
        row = self.row("rt-hold")
        self.assertEqual(row["status"], "queued")
        self.assertFalse(dispatch.worktree_path("rt-hold").exists())
        self.assertEqual(self.notified, [])

    def test_no_pty_retries_once_on_a_new_tab_then_holds(self) -> None:
        calls = self.release(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", self.NO_PTY))
        self.assertEqual(len(calls), 2)
        row = self.row("rt-hold")
        self.assertEqual(row["status"], "queued")
        self.assertIn("had no terminal", row["summary"])
        self.assertEqual(self.notified, [])

    def test_no_pty_recovers_when_the_retry_opens_a_live_terminal(self) -> None:
        answers = [subprocess.CompletedProcess([], 1, "", self.NO_PTY),
                   subprocess.CompletedProcess([], 0, "Created workspace workspace:80 (w-80)", "")]
        self.release(lambda cmd, **kw: answers.pop(0))
        row = self.row("rt-hold")
        self.assertEqual((row["status"], row["workspace_ref"]), ("routing", "workspace:80"))

    def test_a_tab_that_keeps_losing_its_terminal_fails_in_the_end(self) -> None:
        over = {**self.queued(), "launch_holds": dispatch.LAUNCH_HOLD_MAX_NO_TERMINAL, "launch_hold_since": self.stale}
        self.release(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", self.NO_PTY), over)
        self.assertEqual(self.row("rt-hold")["status"], "failed")
        self.assertEqual(self.notified[-1][:2], ("rt-hold", "failed"))

    def test_another_new_workspace_error_still_fails(self) -> None:
        self.release(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "Error: unknown option --frob"))
        self.assertEqual(self.row("rt-hold")["status"], "failed")

    def test_a_long_hold_tells_the_origin_once_and_is_never_marked_stuck(self) -> None:
        old = iso(datetime.now(timezone.utc) - timedelta(minutes=40))
        write_ledger({**self.queued(), "created_at": old, "launch_hold_since": old, "launch_hold_reason": self.PIPE})
        told: list[str] = []
        with patch.object(dispatch, "tell_cos_direct", lambda text: told.append(text) or {"ok": True}):
            self.assertEqual(len(dispatch.report_launch_holds(dispatch.config())), 1)
            self.assertEqual(dispatch.report_launch_holds(dispatch.config()), [])
        self.assertEqual(len(told), 1)
        self.assertIn("waiting on the multiplexer", told[0])
        self.assertEqual(dispatch.flag_stuck_routes(dispatch.config())["stuck"], [])
        self.assertEqual(self.row("rt-hold")["status"], "queued")

    def test_a_successful_launch_clears_the_hold(self) -> None:
        row = {**self.queued(), "launch_holds": 2, "launch_hold_since": self.stale, "launch_hold_reason": "x"}
        self.release(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Created workspace workspace:81 (w-81)", ""), row)
        fresh = self.row("rt-hold")
        self.assertEqual(fresh["status"], "routing")
        for key in ("launch_holds", "launch_hold_since", "launch_hold_reason"):
            self.assertNotIn(key, fresh, key)


class StaleRefHealTests(HermeticRepo):
    """After a wmux restart, or when a worker reports another pane's ref, a live row kept the
    old ref and --steer answered "workspace not found" or reached the wrong worker."""

    def steer(self, sent: list) -> dict:
        args = argparse.Namespace(prompt=["look here"], task_id="rt-wt", origin="cos", no_fallback_dispatch=False, no_deliver=False)
        out = io.StringIO()
        with patch.object(dispatch, "cmux_agent_message", lambda target, text, sender, **kw: sent.append(target) or {"ok": True, "id": "m1"}), \
                patch.object(dispatch, "spawn_steer_delivery", lambda tid: False), redirect_stdout(out):
            dispatch.command_steer(args)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def live_row(self, **extra) -> dict:
        return self.worker_row(agent="claude", thread_id="T-live", session_id="T-live", **extra)

    def test_steer_heals_a_stale_ref_from_the_tab_description(self) -> None:
        self.workspaces = [{"ref": "workspace:90", "id": "w-90", "title": "x", "description": "switchboard worker rt-wt"}]
        write_ledger(self.live_row(workspace_ref="workspace:11"))
        sent: list[str] = []
        result = self.steer(sent)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(sent, ["workspace:90"])
        row = self.row()
        self.assertEqual((row["workspace_ref"], row["workspace_ref_healed_from"]), ("workspace:90", "workspace:11"))

    def test_a_ref_that_names_another_workers_tab_is_corrected_before_the_send(self) -> None:
        self.workspaces = [{"ref": "workspace:76", "id": "w-76", "title": "y", "description": "switchboard worker rt-sibling"},
                           {"ref": "workspace:73", "id": "w-73", "title": "x", "description": "switchboard worker rt-wt"}]
        write_ledger(self.live_row(workspace_ref="workspace:76"))
        sent: list[str] = []
        self.steer(sent)
        self.assertEqual(sent, ["workspace:73"], "the steer goes to this row's tab, never another worker's")

    def test_a_steer_to_a_worker_with_no_tab_waits_for_its_next_attempt(self) -> None:
        self.workspaces = [{"ref": "workspace:1", "id": "w-1", "title": "🧭 CoS", "description": ""}]
        write_ledger(self.live_row(workspace_ref="workspace:11"))
        sent: list[str] = []
        result = self.steer(sent)
        self.assertEqual(result["status"], "steer-queued")
        self.assertEqual(sent, [], "never a message to a tab that does not exist")
        self.assertEqual(len(self.row()["pending_steers"]), 1)

    def test_heal_finds_the_tab_by_live_agent_and_by_worktree_directory_and_restores_the_description(self) -> None:
        calls: list[list[str]] = []
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX:
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kw)

        by_dir = [{"ref": "workspace:90", "id": "w-90", "title": "x", "description": "",
                   "current_directory": r"C:\Users\x\AppData\Local\switchboard\worktrees\rt-wt\workstreams\switchboard-maintenance"}]
        write_ledger(self.live_row(id="rt-wt", workspace_ref="workspace:11"))
        task = self.row()
        with patch.object(dispatch, "run", fake_run):
            tab = dispatch.resolve_live_tab({**task, "tab_title": "x"}, by_dir, {})
            self.assertEqual(tab["ref"], "workspace:90")
            self.agents["T-live"] = {"state": "Running", "workspace_id": "w-90"}
            healed = dispatch.heal_row_ref(task, [{"ref": "workspace:90", "id": "W-90", "title": "x", "description": ""}], self.agents)
        self.assertEqual(healed["workspace_ref"], "workspace:90")
        restore = next(c for c in calls if "set-description" in c)
        self.assertEqual(restore[restore.index("--description") + 1], "switchboard worker rt-wt")

    def test_heal_never_takes_a_tab_by_its_slot_title(self) -> None:
        tab = {"ref": "workspace:74", "id": "w-74", "title": "📊 Switchboard Maintenance 2", "description": ""}
        task = self.live_row(workspace_ref="workspace:11", tab_title="📊 Switchboard Maintenance 2")
        self.assertIsNone(dispatch.resolve_live_tab(task, [tab], {}))

    def test_an_unreadable_tab_list_changes_nothing(self) -> None:
        task = self.live_row(workspace_ref="workspace:11")
        self.assertEqual(dispatch.heal_row_ref(task, [], self.agents), task)

    def test_the_sweep_step_reports_each_healed_row(self) -> None:
        self.workspaces = [{"ref": "workspace:90", "id": "w-90", "title": "x", "description": "switchboard worker rt-wt"}]
        write_ledger(self.live_row(workspace_ref="workspace:11"))
        self.assertEqual(dispatch.heal_live_refs(self.workspaces, self.agents),
                         [{"task_id": "rt-wt", "from": "workspace:11", "to": "workspace:90"}])


class DeadWorkerRedispatchTests(HermeticRepo):
    """After a reboot, rows stayed active with no tab and no process, held the 3-worker cap,
    and sat 30 minutes before the sweep judged them. A worker's work stays usable: the next
    attempt carries on in its checkout. Wrong impls: drop the worktree, wait out the stale
    window with a tab plainly gone, judge on an unreadable list, or stay silent."""

    def setUp(self) -> None:
        super().setUp()
        self.recent = iso(datetime.now(timezone.utc) - dispatch.DEAD_WORKER_AFTER - timedelta(seconds=30))
        self.told: list[str] = []
        told = patch.object(dispatch, "tell_cos_direct", lambda text: self.told.append(text) or {"ok": True})
        told.start()
        self.addCleanup(told.stop)
        self.workspaces = [{"ref": "workspace:1", "id": "w-1", "title": "🧭 CoS", "description": ""}]

    def dead_row(self, **extra) -> dict:
        return self.worker_row(**{"started_at": self.recent, "heartbeat_at": self.recent, "updated_at": self.recent, **extra})

    def test_tab_gone_with_uncommitted_work_is_requeued_on_the_same_checkout_and_cos_hears(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"][0]["resumed_with"], "uncommitted edits")
        row = self.row()
        self.assertEqual((row["status"], row["attempt"]), ("queued", 2))
        self.assertEqual((row["resume_worktree"], row["resume_branch"]), (str(self.path), self.branch))
        self.assertEqual(row["resume_from_attempt"], "rt-wt.a1.aaa")
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n", "the work stays")
        self.assertEqual(len(self.told), 1)
        self.assertIn("worker died", self.told[0])
        self.assertIn("uncommitted edits", self.told[0])
        self.assertEqual(self.notified, [], "not terminal, so no terminal report")
        self.assertEqual(dispatch.live_workers(ledger.load_payload()), [], "the slot is free for this very pass")

    def test_tab_gone_with_commits_is_requeued_too(self) -> None:
        (self.path / "b.md").write_text("done\n")
        self._git("add", "b.md", cwd=self.path)
        self._git("commit", "-q", "-m", "work", cwd=self.path)
        write_ledger(self.dead_row())
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"][0]["resumed_with"], "commits")
        self.assertTrue(self.path.is_dir())

    def test_the_next_launch_reuses_the_checkout_and_says_so(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row(thread_id="T-dead", session_id="T-dead"))
        dispatch.fail_abandoned_rows(dispatch.config())
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "Created workspace workspace:91 (w-91)", "")
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch.time, "sleep", lambda s: None), \
                patch.object(dispatch, "LAUNCH_PROMPTS", self.repo / "prompts"), \
                patch.object(dispatch, "create_worktree", side_effect=AssertionError("must not recreate the checkout")), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        row = self.row()
        self.assertEqual((row["status"], row["workspace_ref"], row["worktree"]), ("routing", "workspace:91", str(self.path)))
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")
        prompt = next((self.repo / "prompts").glob("*.md")).read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("RESUME: attempt rt-wt.a1.aaa"), prompt[:80])
        self.assertIn("T-dead", prompt.split("\n")[0])

    def test_a_launch_held_on_a_resumed_checkout_keeps_the_work(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row())
        dispatch.fail_abandoned_rows(dispatch.config())
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "wmux is not running (could not connect to pipe)")
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch, "LAUNCH_PROMPTS", self.repo / "prompts"), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        row = self.row()
        self.assertEqual(row["status"], "queued")
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")
        self.assertEqual(row["resume_worktree"], str(self.path), "the retry still resumes")

    def test_tab_gone_and_clean_relaunches_from_main_and_cos_hears(self) -> None:
        write_ledger(self.dead_row())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"][0]["origin_told"], True)
        self.assertNotIn("resume_worktree", self.row())
        self.assertFalse(self.path.exists())
        self.assertIn("held no work", self.told[0])

    def test_a_worker_that_keeps_dying_is_blocked_with_its_work_kept(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row(sweep_relaunches=dispatch.MAX_DEAD_WORKER_RELAUNCHES))
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [])
        self.assertIn("already relaunched it 2x", result["blocked"][0]["reason"])
        self.assertTrue(self.path.is_dir())

    def test_a_restored_tab_at_a_bare_shell_counts_as_dead_and_is_closed(self) -> None:
        # After a reboot wmux restores the workers' tabs with new ids, no
        # description, the old directory, and nothing running in them.
        (self.path / "a.md").write_text("half-done\n")
        task_id = "rt-000000000007"
        restored = {"ref": "workspace:68", "id": "w-68", "title": "📊 Switchboard Maintenance 2", "description": "",
                    "current_directory": rf"C:\x\worktrees\{task_id}\workstreams\switchboard-maintenance"}
        self.workspaces.append(restored)
        row = self.dead_row(id=task_id, attempt_id=f"{task_id}.a1.aaa", workspace_ref="workspace:66", tab_title="📊 Switchboard Maintenance 2")
        write_ledger(row)
        with patch.object(dispatch, "tab_is_bare_shell", lambda ref, agent: ref == "workspace:68"):
            result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"][0]["resumed_with"], "uncommitted edits")
        self.assertEqual(self.closed, ["workspace:68"])
        self.assertIn("bare shell", self.told[0])

    def test_a_quiet_open_tab_holding_work_is_blocked_never_closed_or_relaunched(self) -> None:
        # It may be one long tool call under a mismatched thread id.
        (self.path / "a.md").write_text("half-done\n")
        self.workspaces.append({"ref": "workspace:40", "id": "w-40", "title": "x", "description": "switchboard worker rt-wt"})
        write_ledger(self.worker_row())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [])
        self.assertIn("its tab is open", result["blocked"][0]["reason"])
        self.assertEqual(self.closed, [])

    def test_a_close_that_fails_relaunches_nothing(self) -> None:
        # Two workers on one checkout is worse than a late relaunch.
        (self.path / "a.md").write_text("half-done\n")
        self.workspaces.append({"ref": "workspace:40", "id": "w-40", "title": "x", "description": "switchboard worker rt-wt"})
        write_ledger(self.dead_row())
        with patch.object(dispatch, "tab_is_bare_shell", lambda ref, agent: True), \
                patch.object(dispatch, "cmux_close_result", lambda ref, wid, *why: (False, "timed out")):
            self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})
        self.assertEqual(self.row()["status"], "active")

    def test_an_unusable_resume_checkout_is_held_and_never_deleted(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row())
        dispatch.fail_abandoned_rows(dispatch.config())
        real = dispatch.verify_worktree
        with patch.object(dispatch, "verify_worktree", lambda p, b: "git worktree list failed"), \
                patch.object(dispatch, "create_worktree", side_effect=AssertionError("would delete the work")), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        self.assertEqual(self.row()["status"], "queued")
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")
        self.assertEqual(real(self.path, self.branch), "")

    def test_a_resumed_launch_that_fails_keeps_its_pointer_to_the_work(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row())
        dispatch.fail_abandoned_rows(dispatch.config())
        real_run = dispatch.run

        def fake_run(cmd, **kw):
            if cmd and str(cmd[0]) == dispatch.CMUX and "new-workspace" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "Error: unknown option --frob")
            return real_run(cmd, **kw)

        with patch.object(dispatch, "run", fake_run), patch.object(dispatch, "LAUNCH_PROMPTS", self.repo / "prompts"), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        row = self.row()
        self.assertEqual((row["status"], row["worktree"], row["branch"]), ("failed", str(self.path), self.branch))
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")

    def test_a_failed_git_read_is_never_a_clean_checkout(self) -> None:
        real_git = dispatch.git
        failed = subprocess.CompletedProcess([], 128, "", "fatal")
        with patch.object(dispatch, "git", lambda *a, **k: failed if a and a[0] == "status" else real_git(*a, **k)):
            self.assertIn("uncommitted edits", dispatch.unlanded_work(self.worker_row()))
            self.assertTrue(dispatch.worktree_dirty(self.path))

    def test_a_tab_running_or_unreadable_waits_for_the_stale_window(self) -> None:
        self.workspaces.append({"ref": "workspace:40", "id": "w-40", "title": "x", "description": "switchboard worker rt-wt"})
        write_ledger(self.dead_row())
        with patch.object(dispatch, "tab_is_bare_shell", lambda ref, agent: False):
            self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})
        self.assertEqual(self.closed, [])

    def test_a_tab_seen_seconds_ago_is_not_judged_yet(self) -> None:
        fresh = iso(datetime.now(timezone.utc) - timedelta(seconds=20))
        write_ledger(self.worker_row(started_at=fresh, heartbeat_at=fresh, updated_at=fresh))
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})

    def test_a_worker_whose_tab_is_open_is_left_alone(self) -> None:
        self.workspaces.append({"ref": "workspace:40", "id": "w-40", "title": "x", "description": "switchboard worker rt-wt"})
        write_ledger(self.dead_row())
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})

    def test_an_unreadable_tab_list_judges_nothing_new(self) -> None:
        self.workspaces = []
        write_ledger(self.dead_row())
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})

    def test_a_launch_that_never_acked_stays_with_the_retype_step(self) -> None:
        write_ledger(self.dead_row(status="routing", started_at=None, thread_id=""))
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})

    def test_an_empty_session_store_still_finds_a_missing_tab_but_judges_nothing_else(self) -> None:
        self.agents = {}
        write_ledger(self.dead_row())
        self.assertEqual(len(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"]), 1)
        self.workspaces.append({"ref": "workspace:41", "id": "w-41", "title": "x", "description": "switchboard worker rt-wt"})
        write_ledger(self.worker_row(workspace_ref="workspace:41"))
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config()), {"relaunched": [], "blocked": []})


class ContinuationFixture(HermeticRepo):
    """Rows with a real checkout, a stubbed launch, and CoS lines captured."""

    def setUp(self) -> None:
        super().setUp()
        self.recent = iso(datetime.now(timezone.utc) - dispatch.DEAD_WORKER_AFTER - timedelta(seconds=30))
        self.told: list[str] = []
        told = patch.object(dispatch, "tell_cos_direct", lambda text: self.told.append(text) or {"ok": True})
        told.start()
        self.addCleanup(told.stop)
        self.workspaces = [{"ref": "workspace:1", "id": "w-1", "title": "🧭 CoS", "description": ""}]
        self.launched: list[str] = []
        launch = patch.object(dispatch, "launch", side_effect=lambda task, cfg: self.launched.append(task["id"]) or task)
        launch.start()
        self.addCleanup(launch.stop)

    def dead_row(self, **extra) -> dict:
        return self.worker_row(**{"started_at": self.recent, "heartbeat_at": self.recent, "updated_at": self.recent, **extra})

    def heir(self, tid: str = "rt-heir", status: str = "active", parent: str = "rt-wt", **extra) -> dict:
        return {"id": tid, "status": status, "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "carry on",
                "attempt": 1, "attempt_id": f"{tid}.a1.bbb", "continuation_of": parent,
                "created_at": ledger.now_iso(), "updated_at": ledger.now_iso(), **extra}

    def release(self) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            dispatch.release_queued(dispatch.DEFAULTS)
        return json.loads(out.getvalue().strip().splitlines()[-1])


class ContinuationOwnsWorkTests(ContinuationFixture):
    """A continuation takes a dead row's work, so the sweep never re-queues a parent whose
    continuation is live (the relaunch would act on GitHub a second time). Wrong impls: relaunch the parent, block it and ask the owner to land what
    the continuation carries, retire a parent whose worker is still running, drop its checkout, or
    count a failed, cancelled, supplemental, or older continuation as the owner."""

    def test_a_dead_row_with_a_live_continuation_is_never_relaunched(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        write_ledger(self.dead_row(), self.heir())
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(result["relaunched"], [], "two workers on one job")
        self.assertEqual(result["blocked"], [], "no blocked line asking the owner to land what the continuation carries")
        self.assertEqual(result["superseded"], [{"task_id": "rt-wt", "superseded_by": "rt-heir"}])
        row = self.row()
        self.assertEqual((row["status"], row["attempt"], row["superseded_by"]), ("cancelled", 1, "rt-heir"))
        self.assertIs(row["report_delivered"], True, "a retired row has no report to retry")
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n", "its checkout stays")
        self.assertEqual((self.told, self.notified), ([], []), "CoS dispatched the continuation; it needs no line")
        self.release()
        self.assertEqual(self.launched, [])

    def test_a_clean_dead_row_with_a_continuation_is_retired_too(self) -> None:
        write_ledger(self.dead_row(), self.heir())
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"], [])
        self.assertEqual(self.row()["status"], "cancelled")

    def test_a_row_already_queued_when_its_continuation_appears_never_launches(self) -> None:
        # The row is already back in the queue.
        write_ledger(self.worker_row(status="queued"), self.heir())
        result = self.release()
        self.assertEqual(self.launched, [])
        self.assertEqual(result["superseded"], [{"task_id": "rt-wt", "superseded_by": "rt-heir"}])
        self.assertEqual(self.row()["status"], "cancelled")

    def test_a_routing_row_with_no_worker_is_retired_not_requeued(self) -> None:
        stale = iso(datetime.now(timezone.utc) - dispatch.NOT_STARTED_AFTER - timedelta(minutes=1))
        write_ledger({"id": "rt-wt", "status": "routing", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "x",
                      "attempt": 1, "attempt_id": "rt-wt.a1.aaa", "created_at": stale, "updated_at": stale}, self.heir())
        self.assertEqual(dispatch.requeue_orphaned_routes(), [])
        self.assertEqual(self.row()["status"], "cancelled")

    def test_a_stalled_launch_is_not_retyped(self) -> None:
        stale = iso(datetime.now(timezone.utc) - dispatch.SWEEP_RETYPE_MIN_AGE - timedelta(minutes=1))
        write_ledger(self.worker_row(status="routing", thread_id="", started_at="", heartbeat_at="", updated_at=stale,
                                     created_at=stale), self.heir())
        with patch.object(dispatch, "read_tab_screen", side_effect=AssertionError("must not touch the tab")):
            result = dispatch.retype_stalled_launches()
        self.assertEqual(result["retyped"], [])

    def test_the_whole_chain_counts(self) -> None:
        # A chain of continuations: each one died and was continued.
        write_ledger(self.dead_row(), self.heir("rt-one", "blocked"), self.heir("rt-two", "active", parent="rt-one"))
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config())["superseded"][0]["superseded_by"], "rt-two")

    def test_a_continuation_that_owns_nothing_leaves_the_relaunch_alone(self) -> None:
        for name, heir in (("failed", self.heir(status="failed")), ("cancelled", self.heir(status="cancelled")),
                           ("supplemental", self.heir(supplemental=True))):
            with self.subTest(name):
                write_ledger(self.dead_row(), heir)
                self.assertEqual(len(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"]), 1)
                self.path, self.branch = dispatch.create_worktree("rt-wt")   # the clean relaunch dropped it

    def test_a_row_redispatched_after_its_continuation_owns_itself_again(self) -> None:
        old = iso(datetime.now(timezone.utc) - timedelta(hours=2))
        write_ledger(self.dead_row(attempt_began_at=ledger.now_iso()), self.heir(created_at=old))
        self.assertEqual(len(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"]), 1)

    def test_begin_attempt_stamps_when_the_attempt_began(self) -> None:
        row = self.worker_row(attempt_began_at="2020-01-01T00:00:00Z")
        ledger.begin_attempt(row, status="queued", summary="x")
        self.assertGreater(row["attempt_began_at"], "2026")

    def test_a_parent_whose_worker_is_still_running_is_left_alone(self) -> None:
        self.agents["T-dead"] = {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-40", "title": "x"}
        write_ledger(self.dead_row(), self.heir())
        self.assertNotIn("superseded", dispatch.fail_abandoned_rows(dispatch.config()))
        self.assertEqual(self.row()["status"], "active")
        self.assertIsNone(dispatch.retire_superseded(self.row(), self.row("rt-heir")))

    def test_a_running_parent_gets_no_resume_nudge(self) -> None:
        idle_since = (datetime.now(timezone.utc) - timedelta(minutes=2 * dispatch.IDLE_RESUME_MINUTES)).timestamp()
        self.agents["T-dead"] = {"state": "Idle", "state_ts": idle_since, "workspace_id": "w-40", "title": "x"}
        write_ledger(self.dead_row(), self.heir())
        with patch.object(dispatch, "tab_send", side_effect=AssertionError("must not wake it")):
            self.assertEqual(dispatch.nudge_idle(dispatch.config())["resumed"], [])

    def test_blocked_reports_of_replaced_rows_settle_and_done_reports_do_not(self) -> None:
        def row(tid, status, **extra):
            return {"id": tid, "status": status, "report_delivered": False, "attempt_id": f"{tid}.a1", **extra}
        write_ledger(row("rt-replaced", "blocked"), row("rt-failed", "failed"), row("rt-alone", "blocked"),
                     row("rt-done", "done"), self.heir("rt-a", parent="rt-replaced"), self.heir("rt-b", parent="rt-failed"),
                     self.heir("rt-c", parent="rt-done"))
        settled = {s["task_id"] for s in dispatch.settle_superseded_reports()}
        self.assertEqual(settled, {"rt-replaced", "rt-failed"})
        self.assertIs(self.row("rt-replaced")["report_delivered"], True)
        self.assertIs(self.row("rt-alone")["report_delivered"], False, "no continuation: the owner still needs to hear it")
        self.assertIs(self.row("rt-done")["report_delivered"], False, "a done report always goes through")


class LaunchTabLostTests(ContinuationFixture):
    """A finished row can reap a brand-new tab by its reused slot title, leaving the new row
    active with a dead tab, no heartbeat and no transcript, or failing with "has no PTY". Wrong impls: leave an unacked active row for the 30-minute stale window,
    or let a finished row close a new tab that shares its slot title."""

    def test_an_active_row_that_never_acked_and_lost_its_tab_is_relaunched_and_cos_hears(self) -> None:
        write_ledger(self.dead_row(thread_id="", started_at="", heartbeat_at="", session_id="never-bound"))
        result = dispatch.fail_abandoned_rows(dispatch.config())
        self.assertEqual(len(result["relaunched"]), 1)
        self.assertEqual((self.row()["status"], self.row()["attempt"]), ("queued", 2))
        self.assertIn("worker died", self.told[0])

    def test_a_routing_row_still_belongs_to_the_launcher(self) -> None:
        write_ledger(self.dead_row(status="routing", thread_id="", started_at="", heartbeat_at=""))
        self.assertEqual(dispatch.fail_abandoned_rows(dispatch.config())["relaunched"], [])
        self.assertEqual(self.row()["status"], "routing")

    def test_a_finished_row_never_closes_a_new_tab_with_its_slot_title(self) -> None:
        # Same title, a different ref, with the description written (attempt 1) and not yet written (attempt 2).
        finished = iso(datetime.now(timezone.utc) - timedelta(hours=3))
        for description in ("switchboard worker rt-new", ""):
            with self.subTest(description=description):
                self.closed.clear()
                self.workspaces = [{"ref": "workspace:127", "id": "w-127", "title": "📊 Switchboard Maintenance 7",
                                    "description": description}]
                write_ledger(self.worker_row(status="done", finished_at=finished, report_delivered=True, thread_id="",
                                             tab_title="📊 Switchboard Maintenance 7", workspace_ref="workspace:113"))
                dispatch.reap_all(dispatch.DEFAULTS)
                self.assertEqual(self.closed, [], "a slot title alone never proves a tab")


class PrerequisiteFollowsReplacementTests(ContinuationFixture):
    """A dependent row never goes blocked on a dead prerequisite whose continuation has that
    work. Wrong impls: read only the named prerequisite, follow a continuation of a done row, leave a
    blocked dependent blocked once a replacement appears, or revive it with nothing to wait on."""

    def waiting(self, **extra) -> dict:
        return {"id": "rt-dep", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "after it",
                "attempt": 1, "attempt_id": "rt-dep.a1.ccc", "depends_on": ["rt-pre"], **extra}

    def pre(self, status: str = "blocked") -> dict:
        return {"id": "rt-pre", "status": status, "attempt_id": "rt-pre.a1", "report_delivered": True}

    def test_a_task_waits_on_the_replacement_of_a_blocked_prerequisite(self) -> None:
        write_ledger(self.waiting(), self.pre(), self.heir(parent="rt-pre"))
        result = self.release()
        self.assertEqual(result["held"], [{"task_id": "rt-dep", "waiting_on": "rt-heir"}])
        self.assertEqual((self.row("rt-dep")["status"], self.launched), ("queued", []))

    def test_a_task_starts_once_the_replacement_is_done(self) -> None:
        write_ledger(self.waiting(), self.pre(), self.heir(status="done", parent="rt-pre"))
        self.release()
        self.assertEqual(self.launched, ["rt-dep"])

    def test_a_blocked_replacement_blocks_the_task_and_names_it(self) -> None:
        write_ledger(self.waiting(), self.pre(), self.heir(status="blocked", parent="rt-pre"))
        self.release()
        row = self.row("rt-dep")
        self.assertEqual((row["status"], row["blocked_on_prerequisite"]), ("blocked", "rt-pre"))
        self.assertIn("through its continuation rt-heir", row["summary"])

    def test_a_blocked_prerequisite_with_no_replacement_still_blocks(self) -> None:
        write_ledger(self.waiting(), self.pre())
        self.release()
        self.assertEqual(self.row("rt-dep")["status"], "blocked")

    def test_a_done_prerequisite_is_not_reopened_by_a_follow_up(self) -> None:
        write_ledger(self.waiting(), self.pre("done"), self.heir(status="active", parent="rt-pre"))
        self.release()
        self.assertEqual(self.launched, ["rt-dep"], "a continuation of a done row is a follow-up")

    def test_a_blocked_dependent_returns_to_the_queue_when_the_replacement_appears(self) -> None:
        blocked = self.waiting(status="blocked", blocked_on_prerequisite="rt-pre", report_delivered=False)
        write_ledger(blocked, self.pre())
        self.assertEqual(dispatch.follow_replaced_prerequisites(), [], "still nothing to wait on")
        self.assertEqual(self.row("rt-dep")["status"], "blocked")
        write_ledger(blocked, self.pre(), self.heir(parent="rt-pre"))
        followed = dispatch.follow_replaced_prerequisites()
        self.assertEqual(followed[0]["now_waiting_on"], "rt-heir")
        row = self.row("rt-dep")
        self.assertEqual((row["status"], row["attempt"]), ("queued", 2))
        self.assertNotIn("blocked_on_prerequisite", row)
        self.assertNotIn("report_delivered", row, "the old blocked report is gone with its attempt")
        self.assertEqual(len(self.told), 1)
        self.assertEqual(dispatch.follow_replaced_prerequisites(), [], "once")

    def test_a_blocked_dependent_with_no_stamp_is_left_alone(self) -> None:
        write_ledger(self.waiting(status="blocked"), self.pre(), self.heir(parent="rt-pre"))
        self.assertEqual(dispatch.follow_replaced_prerequisites(), [])


class SecondaryPrimaryLivenessTests(HermeticRepo):
    """A task sent to take over a dead worker must not be told that worker is "already live"
    or be forbidden its worktree. Wrong impls: trust the row status, name the row this task
    replaces, or say "already live" with no agent to prove it."""

    def other(self, **extra) -> dict:
        return {"id": "rt-other", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "other work",
                "attempt": 1, "attempt_id": "rt-other.a1", "worker_id": "worker-b", "worktree": "/tmp/other", "thread_id": "T-other",
                "tab_title": "📊 Switchboard Maintenance 2", "created_at": self.stale, "updated_at": self.stale, **extra}

    def me(self, **extra) -> dict:
        return {"id": "rt-me", "status": "routing", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "mine",
                "attempt": 1, "attempt_id": "rt-me.a1", "created_at": ledger.now_iso(), **extra}

    def clause(self, primary: dict) -> str:
        task = {**self.me(), "secondary_to": primary, "branch": "task/rt-me"}
        return next(p for p in dispatch.launch_prompt(task, Path("/tmp/wt"), "worker-1").split("\n\n") if p.startswith("Secondary worker:"))

    def test_a_dead_worker_is_not_a_primary(self) -> None:
        write_ledger(self.other(), self.me())
        self.assertIsNone(dispatch.secondary_primary("switchboard-maintenance", "rt-me"))

    def test_the_row_this_task_continues_is_never_its_primary(self) -> None:
        self.agents["T-other"] = {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-2", "title": "x"}   # even with an agent
        write_ledger(self.other(), self.me(continuation_of="rt-other"))
        self.assertIsNone(dispatch.secondary_primary("switchboard-maintenance", "rt-me"))
        write_ledger(self.other(), self.other(id="rt-mid", status="blocked", continuation_of="rt-other"),
                     self.me(continuation_of="rt-mid"))
        self.assertIsNone(dispatch.secondary_primary("switchboard-maintenance", "rt-me"), "through a chain too")

    def test_another_live_worker_is_still_the_primary_and_really_is_live(self) -> None:
        self.agents["T-live"] = {"state": "Running", "state_ts": 1.0, "workspace_id": "w-3", "title": "x"}
        write_ledger(self.other(), self.other(id="rt-live", thread_id="T-live", created_at=ledger.now_iso()),
                     self.me(continuation_of="rt-other"))
        primary = dispatch.secondary_primary("switchboard-maintenance", "rt-me")
        self.assertEqual((primary["id"], primary["proven_live"]), ("rt-live", True))
        self.assertIn("(rt-live) is already live in this workstream", self.clause(primary))

    def test_a_worker_with_no_agent_yet_is_not_called_live(self) -> None:
        write_ledger(self.other(id="rt-new", thread_id="", created_at=ledger.now_iso(), updated_at=ledger.now_iso()), self.me())
        primary = dispatch.secondary_primary("switchboard-maintenance", "rt-me")
        self.assertIs(primary["proven_live"], False)
        clause = self.clause(primary)
        self.assertNotIn("already live", clause)
        self.assertIn("may still be coming up", clause)


class LimitResumeTests(HermeticRepo):
    """Rule: a worker that hits a context or usage limit restarts on its own. Once, three workers
    and CoS stopped on the 5-hour usage limit, two more launched into it, and all sat until the
    owner asked hours later. Transcripts here are invented, in the shape Claude Code 2.1.319
    wrote. Wrong impls: leave a limited worker for the idle nudge and the wedge flag, launch
    into the limit, resume before the reset or twice, resume a cancelled row, resume beside a quiet-window task, type over a CoS draft, or end a context-limited worker."""

    S1 = "11111111-1111-4111-8111-111111111111"
    S2 = "22222222-2222-4222-8222-222222222222"
    COS = "33333333-3333-4333-8333-333333333333"

    def setUp(self) -> None:
        super().setUp()
        self.projects = Path(self.tmp.name) / "projects"
        self.told: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.send_ok = True
        for p in (patch.object(dispatch, "CLAUDE_PROJECTS", self.projects),
                  patch.object(dispatch, "USAGE_LIMIT_STAMP", Path(self.tmp.name) / "usage-limit.json"),
                  patch.object(dispatch, "tell_cos_direct", lambda text: self.told.append(text) or {"ok": True}),
                  patch.object(dispatch, "tab_send", lambda ref, text: (self.sent.append((ref, text)) or self.send_ok)),
                  patch.object(dispatch.owner_tabs, "read_registration", lambda: {})):
            p.start()
            self.addCleanup(p.stop)
        self.now = datetime.now(timezone.utc)
        self.workspaces = [{"ref": "workspace:1", "id": "w-cos", "title": "🧭 CoS", "description": ""}]

    def transcript(self, session: str, *records: dict, folder: str = "C--worktree") -> None:
        path = self.projects / folder / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        launch = {"type": "user", "timestamp": iso(self.now - timedelta(hours=3)),
                  "message": {"role": "user", "content": "Read and follow all instructions in the launch prompt"}}
        path.write_text("\n".join(json.dumps(r) for r in (launch, *records)) + "\n", encoding="utf-8")

    def usage_error(self, at: datetime, resets: datetime) -> dict:
        return {"type": "assistant", "timestamp": iso(at), "isApiErrorMessage": True, "apiError": "usage_limit_reached",
                "error": "rate_limit", "apiErrorStatus": 429,
                "apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": int(resets.timestamp()),
                                                       "rateLimitType": "five_hour"}},
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "You've hit your 5-hour limit · resets 2:30am (America/Los_Angeles)"}]}}

    def context_error(self, at: datetime) -> dict:
        return {"type": "assistant", "timestamp": iso(at), "isApiErrorMessage": True,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Prompt is too long"}]}}

    def limited_row(self, tid: str, session: str, created: datetime, ref: str, **extra) -> dict:
        row = self.worker_row(id=tid, session_id=session, thread_id=session, agent="claude", workspace_ref=ref,
                              attempt_id=f"{tid}.a1.aaa", created_at=iso(created), heartbeat_at=iso(self.now - timedelta(hours=2)),
                              **extra)
        self.agents[session] = {"state": "Idle", "state_ts": (self.now - timedelta(hours=2)).timestamp(),
                                "workspace_id": f"w-{tid}", "title": "worker"}
        self.workspaces.append({"ref": ref, "id": f"w-{tid}", "title": "📊 Switchboard Maintenance 1",
                                "description": f"switchboard worker {tid}"})
        return row

    def test_a_usage_limited_worker_is_paused_once_and_new_claude_launches_wait(self) -> None:
        resets = self.now + timedelta(hours=1)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(minutes=5), resets))
        queued = {"id": "rt-next", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance",
                  "prompt": "next", "agent": "claude", "created_at": iso(self.now), "attempt": 1, "attempt_id": "rt-next.a1.q"}
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"), queued)
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual([p["task_id"] for p in result["paused"]], ["rt-wt"])
        row = self.row()
        self.assertTrue(row["paused"].startswith("paused: usage limit, resumes at "), row.get("paused"))
        self.assertEqual(row["usage_limit"]["limit_type"], "five_hour")
        self.assertEqual(len(self.told), 1)
        self.assertIn("paused until", self.told[0])
        self.assertEqual(self.sent, [], "nothing is typed into a limited session before the reset")
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(len(self.told), 1, "one CoS line per pause, not one per tick")
        self.assertIsNotNone(dispatch.usage_limit_until())
        with patch.object(dispatch, "launch", side_effect=AssertionError("must not launch into the usage limit")), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        self.assertEqual(self.row("rt-next")["status"], "queued")
        with patch.object(dispatch, "tab_idle", lambda ref: True), patch.object(ledger, "git_state", lambda t: "open"):
            self.assertEqual(dispatch.nudge_idle(dispatch.DEFAULTS)["resumed"], [], "no idle nudge into a limited worker")

    def test_after_the_reset_paused_workers_resume_in_place_in_creation_order_and_cos_hears_once(self) -> None:
        resets = self.now - timedelta(minutes=5)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--a")
        self.transcript(self.S2, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--b")
        write_ledger(self.limited_row("rt-late", self.S2, self.now - timedelta(hours=3), "workspace:42"),
                     self.limited_row("rt-early", self.S1, self.now - timedelta(hours=4), "workspace:41"))
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(result["resumed"], ["rt-early", "rt-late"])
        self.assertEqual([ref for ref, _ in self.sent], ["workspace:41", "workspace:42"])
        self.assertIn("Continue task rt-early from where it stopped", self.sent[0][1])
        for tid in ("rt-early", "rt-late"):
            self.assertNotIn("usage_limit", self.row(tid))
            self.assertEqual(self.row(tid)["usage_resumes"], 1)
        self.assertEqual(len(self.told), 1)
        self.assertIn("resumed in place", self.told[0])
        self.assertNotIn("paused until", self.told[0], "a reset already past needs no pause line")
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(len(self.sent), 2, "the same limit stop is resumed once")
        self.assertEqual(len(self.told), 1)

    def test_a_worker_whose_tab_is_gone_at_the_reset_is_relaunched_on_its_checkout(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), self.now - timedelta(minutes=5)))
        row = self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40")
        self.agents.pop(self.S1)
        self.workspaces = [w for w in self.workspaces if w["ref"] != "workspace:40"]
        write_ledger(row)
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(result["relaunched"], ["rt-wt"])
        row = self.row()
        self.assertEqual((row["status"], row["attempt"], row["resume_reason"]), ("queued", 2, "usage"))
        self.assertEqual(row["resume_worktree"], str(self.path))
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n", "the work stays")
        self.assertIn("paused on the Claude usage limit", dispatch.resume_note(row, self.path, self.branch))

    def test_a_quiet_window_task_resumes_alone_and_others_wait_for_it(self) -> None:
        resets = self.now - timedelta(minutes=5)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--a")
        self.transcript(self.S2, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--b")
        quiet = self.limited_row("rt-rewrite", self.S1, self.now - timedelta(hours=4), "workspace:41",
                                 prompt="Rewrite history.\n\nPRECONDITIONS: no active workers (verify on the ledger).")
        other = self.limited_row("rt-other", self.S2, self.now - timedelta(hours=3), "workspace:42")
        busy = self.worker_row(id="rt-busy", thread_id="T-busy", attempt_id="rt-busy.a1.b",
                               heartbeat_at=iso(self.now), created_at=iso(self.now - timedelta(hours=5)))
        self.agents["T-busy"] = {"state": "Running", "state_ts": self.now.timestamp(), "workspace_id": "w-busy", "title": "w"}
        write_ledger(quiet, other, busy)
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(self.sent, [], "the rewrite waits for rt-busy, and rt-other waits for the rewrite")
        self.assertEqual({w["task_id"] for w in result["waiting"]}, {"rt-rewrite", "rt-other"})
        write_ledger(quiet, other, {**busy, "status": "done"})
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(result["resumed"], ["rt-rewrite"])
        self.assertEqual([w["task_id"] for w in result["waiting"]], ["rt-other"])

    def test_a_cancelled_row_is_never_resumed(self) -> None:
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), self.now - timedelta(minutes=5)))
        row = self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40", status="cancelled",
                               usage_limit={"error_at": "x", "resets_at": iso(self.now - timedelta(minutes=5))})
        write_ledger(row)
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.row()["status"], "cancelled")

    def test_cos_on_the_limit_is_resumed_through_the_inject_guard_and_retried_over_a_draft(self) -> None:
        resets = self.now - timedelta(minutes=5)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--a")
        self.transcript(self.COS, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--switchboard")
        self.agents[self.COS] = {"agent": "claude", "thread_id": self.COS, "state": "Idle", "state_ts": 1.0,
                                 "workspace_id": "w-cos", "title": "🧭 CoS"}
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"))
        self.send_ok = False   # a draft in the CoS box: the guard refuses
        with patch.object(dispatch.owner_tabs, "agents_by_workspace",
                          lambda agents=None: {"W-COS": {"agent": "claude", "thread_id": self.COS, "state": "Idle"}}):
            first = dispatch.handle_limits(dispatch.DEFAULTS)
            self.assertTrue(first.get("cos_retry"))
            self.assertEqual(self.told, [], "the summary waits for CoS's own resume line")
            self.send_ok = True
            self.sent.clear()
            second = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertTrue(second.get("cos_resumed"))
        cos_lines = [text for ref, text in self.sent if ref == "workspace:1"]
        self.assertEqual(len(cos_lines), 1)
        self.assertTrue(cos_lines[0].startswith("[ROUTER REPORT] Claude usage limit reset at"))
        self.assertIn("CoS resumed", cos_lines[0])
        self.assertIn("resumed in place", cos_lines[0])
        self.assertEqual(self.told, [], "CoS hears it once, through the guard")

    def test_a_context_limited_worker_restarts_once_on_its_checkout_then_cos_hears(self) -> None:
        (self.path / "a.md").write_text("half-done\n")
        self.transcript(self.S1, self.context_error(self.now - timedelta(minutes=5)))
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"))
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual([r["task_id"] for r in result["context_restarted"]], ["rt-wt"])
        row = self.row()
        self.assertEqual((row["status"], row["resume_reason"], row["context_restarts"]), ("queued", "context", 1))
        self.assertEqual(self.closed, ["workspace:40"], "the stuck session's tab is closed before the relaunch")
        self.assertIn("stopped at the context limit", dispatch.resume_note(row, self.path, self.branch))
        self.assertEqual((self.path / "a.md").read_text(), "half-done\n")
        self.assertEqual(len(self.told), 1)
        # The second attempt stops at the context limit too: CoS hears, nothing relaunches.
        self.transcript(self.S2, self.context_error(self.now - timedelta(minutes=4)), folder="C--b")
        self.agents[self.S2] = {"state": "Idle", "state_ts": 1.0, "workspace_id": "w-43", "title": "worker"}
        second = {**row, "status": "active", "session_id": self.S2, "thread_id": self.S2, "worktree": str(self.path),
                  "branch": self.branch, "workspace_ref": "workspace:43"}
        write_ledger(second)
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual([r["task_id"] for r in result["context_told"]], ["rt-wt"])
        self.assertEqual(self.row()["status"], "active")
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(len(self.told), 2, "told once per stop")

    def test_other_api_errors_and_a_session_that_moved_on_are_not_limits(self) -> None:
        dns = {"type": "assistant", "timestamp": iso(self.now), "isApiErrorMessage": True, "apiError": "server_error",
               "message": {"role": "assistant", "content": [{"type": "text", "text": "API Error: Can't reach the API server (ENOTFOUND)"}]}}
        self.transcript(self.S1, dns)
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40",
                                      usage_limit={"error_at": "old", "resets_at": iso(self.now + timedelta(hours=1))}))
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(result.get("cleared"), ["rt-wt"], "someone typed into it and it answered")
        self.assertNotIn("usage_limit", self.row())

    def test_a_fresh_context_error_or_a_busy_session_is_left_to_settle(self) -> None:
        self.transcript(self.S1, self.context_error(self.now - timedelta(seconds=30)))
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"))
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(self.row()["status"], "active", "it may still be compacting")
        self.transcript(self.S1, self.context_error(self.now - timedelta(minutes=5)))
        self.agents[self.S1]["state"] = "Running"
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual((self.row()["status"], self.closed), ("active", []), "a busy session is never closed")

    def test_an_empty_cmux_read_judges_nothing(self) -> None:
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), self.now - timedelta(minutes=5)))
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"))
        self.agents = {}
        self.assertIn("skipped", dispatch.handle_limits(dispatch.DEFAULTS))
        self.assertEqual((self.row()["status"], self.closed, self.sent), ("active", [], []))

    def test_staggered_resumes_keep_one_window_and_one_cos_line(self) -> None:
        """Review finding: a row still held after the reset restarted the window each tick,
        so CoS got a resume line per tick and the summary lost earlier workers."""
        resets = self.now - timedelta(minutes=5)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--a")
        self.transcript(self.S2, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--b")
        write_ledger(self.limited_row("rt-a", self.S1, self.now - timedelta(hours=4), "workspace:41"),
                     self.limited_row("rt-b", self.S2, self.now - timedelta(hours=3), "workspace:42"))
        sends = iter([True, False, True])
        with patch.object(dispatch, "tab_send", lambda ref, text: self.sent.append((ref, text)) or next(sends)):
            first = dispatch.handle_limits(dispatch.DEFAULTS)
            self.assertEqual((first["resumed"], first["retry"]), (["rt-a"], ["rt-b"]))
            second = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(second["resumed"], ["rt-b"])
        self.assertEqual(len(self.told), 1, "one resume line for the window")
        self.assertEqual(json.loads(dispatch.USAGE_LIMIT_STAMP.read_text())["resumed"], ["rt-a", "rt-b"])

    def test_resumes_stop_after_two_in_a_row_and_cos_hears(self) -> None:
        self.transcript(self.S1, self.usage_error(self.now - timedelta(minutes=10), self.now - timedelta(minutes=5)))
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40", usage_streak=2))
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual([c["task_id"] for c in result["capped"]], ["rt-wt"])
        self.assertIn("left for the owner", self.row()["paused"])
        self.assertEqual(self.sent, [])
        self.assertIn("again right after 2 automatic resumes", self.told[0])

    def test_two_quiet_rows_never_resume_together_and_launches_wait_for_them(self) -> None:
        resets = self.now - timedelta(minutes=5)
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--a")
        self.transcript(self.S2, self.usage_error(self.now - timedelta(hours=2), resets), folder="C--b")
        brief = "PRECONDITIONS: no active workers."
        queued = {"id": "rt-next", "status": "queued", "origin": "cos", "workstream": "switchboard-maintenance",
                  "prompt": "next", "agent": "claude", "created_at": iso(self.now), "attempt": 1, "attempt_id": "rt-next.a1.q"}
        write_ledger(self.limited_row("rt-q1", self.S1, self.now - timedelta(hours=4), "workspace:41", prompt=brief),
                     self.limited_row("rt-q2", self.S2, self.now - timedelta(hours=3), "workspace:42", prompt=brief), queued)
        result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(result["resumed"], ["rt-q1"])
        self.assertEqual([w["task_id"] for w in result["waiting"]], ["rt-q2"])
        with patch.object(dispatch, "launch", side_effect=AssertionError("held for the quiet window")), \
                redirect_stdout(io.StringIO()):
            dispatch.release_queued(dispatch.DEFAULTS)
        self.assertEqual(self.row("rt-next")["status"], "queued")

    def test_a_paused_row_with_a_continuation_is_left_alone(self) -> None:
        self.transcript(self.S1, self.usage_error(self.now - timedelta(hours=2), self.now - timedelta(minutes=5)))
        heir = {"id": "rt-heir", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "go on",
                "continuation_of": "rt-wt", "created_at": iso(self.now), "attempt": 1, "attempt_id": "rt-heir.a1.h"}
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"), heir)
        dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertEqual(self.sent, [])

    def test_the_kill_switch_turns_it_off(self) -> None:
        self.transcript(self.S1, self.usage_error(self.now, self.now + timedelta(hours=1)))
        write_ledger(self.limited_row("rt-wt", self.S1, self.now - timedelta(hours=3), "workspace:40"))
        with patch.dict(os.environ, {"SWITCHBOARD_LIMIT_RESUME": "off"}):
            self.assertIn("skipped", dispatch.handle_limits(dispatch.DEFAULTS))
        self.assertNotIn("usage_limit", self.row())


NETWORK_ERROR_FORMS = {
    # The shape Claude Code 2.1.321 wrote for three workers on a network drop.
    "ENOTFOUND": {"apiError": "connection_failed", "error": "server_error",
                  "text": "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"},
    "ECONNRESET": {"apiError": "connection_failed", "error": "server_error", "text": "API Error: read ECONNRESET"},
    "ETIMEDOUT": {"text": "API Error: connect ETIMEDOUT 160.79.104.10:443"},
    "timeout": {"text": "API Error: Request timed out."},
    "connection": {"text": "API Error (Connection error.)"},
    "529": {"apiErrorStatus": 529, "text": 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'},
    "500": {"apiErrorStatus": 500, "text": 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'},
    "503": {"text": "API Error: 503 Service Unavailable"},
}
NOT_TRANSIENT_FORMS = {
    "401": {"apiErrorStatus": 401, "text": "API Error: 401 authentication_error · Please run /login"},
    "400": {"apiErrorStatus": 400, "text": 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error"}}'},
    "billing": {"text": "Credit balance is too low"},
}
# What the tab showed: output, the error under it, then the prompt box and the footer.
NETWORK_SCREEN = """● Reading the dispatch ledger next.
  ⎿  API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)

╭──────────────────────────────────────────────────────────────╮
│ >                                                            │
╰──────────────────────────────────────────────────────────────╯
  ⏵⏵ bypass permissions on (shift+tab to cycle)
  Opus 5.5 · switchboard · 41% context
"""
OVERLOADED_SCREEN = """● Running the test suite.
  ⎿  API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}
     Retrying may help; see status.anthropic.com
────────────────────────────────────────────────
>
────────────────────────────────────────────────
"""
MOVED_ON_SCREEN = NETWORK_SCREEN.replace("\n\n╭", "\n> continue\n● Picking up where I stopped.\n\n╭", 1)
TOOL_SCREEN = """● Bash(python3 system/scripts/dispatch.py sweep)
  ⎿  Running…
✻ Thinking… (esc to interrupt)
"""


class NetworkStopTests(HermeticRepo):
    """A DNS drop can end every live Claude worker's turn with "API Error: Can't reach the
    API server (ENOTFOUND)", leaving them at an empty prompt holding every worker slot while
    the sweep says only "likely stuck on a tool call". Wrong impls: resume while the API is still down,
    resume a fresh error Claude Code may still be retrying, send twice in one backoff window, keep
    resuming past the cap, read a network error as a context limit (closing the tab), nudge or
    wedge-flag a row this step owns, judge an old error under newer output on screen, treat an
    auth or 400 error as transient, type into CoS over a draft, or reach a live tab or the network."""

    S1 = "44444444-4444-4444-8444-444444444444"
    COS = "55555555-5555-4555-8555-555555555555"

    def setUp(self) -> None:
        super().setUp()
        self.projects = Path(self.tmp.name) / "projects"
        self.told: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.screens: dict[str, str | None] = {}
        self.api = {"up": False, "probes": 0}
        self.message_reply: dict = {"ok": True, "id": "m-1"}
        self.send_ok = True
        for p in (patch.object(dispatch, "CLAUDE_PROJECTS", self.projects),
                  patch.object(dispatch, "USAGE_LIMIT_STAMP", Path(self.tmp.name) / "usage-limit.json"),
                  patch.object(dispatch, "NETWORK_STAMP", Path(self.tmp.name) / "network-resume.json"),
                  patch.object(dispatch, "tell_cos_direct", lambda text: self.told.append(text) or {"ok": True}),
                  patch.object(dispatch, "cmux_agent_message",
                               lambda ref, text, sender, timeout=45.0: self.messages.append((ref, text)) or self.message_reply),
                  patch.object(dispatch, "tab_send", lambda ref, text: (self.sent.append((ref, text)) or self.send_ok)),
                  patch.object(dispatch, "read_tab_screen", lambda ref, lines=50: self.screens.get(ref)),
                  patch.object(dispatch, "api_reachable", self.probe),
                  patch.object(dispatch.owner_tabs, "read_registration", lambda: {})):
            p.start()
            self.addCleanup(p.stop)
        self.now = datetime.now(timezone.utc)
        self.workspaces = [{"ref": "workspace:1", "id": "w-cos", "title": "🧭 CoS", "description": ""}]

    def probe(self) -> bool:
        self.api["probes"] += 1
        return self.api["up"]

    def error_record(self, at: datetime, form: str = "ENOTFOUND") -> dict:
        spec = {**NETWORK_ERROR_FORMS.get(form, NOT_TRANSIENT_FORMS.get(form, {}))}
        text = spec.pop("text")
        return {"type": "assistant", "timestamp": iso(at), "isApiErrorMessage": True, **spec,
                "message": {"role": "assistant", "model": "<synthetic>", "stop_reason": "stop_sequence",
                            "content": [{"type": "text", "text": text}]}}

    def transcript(self, session: str, *records: dict, folder: str = "C--worktree") -> None:
        path = self.projects / folder / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        launch = {"type": "user", "timestamp": iso(self.now - timedelta(hours=3)),
                  "message": {"role": "user", "content": "Read and follow all instructions in the launch prompt"}}
        path.write_text("\n".join(json.dumps(r) for r in (launch, *records)) + "\n", encoding="utf-8")

    def stopped_row(self, **extra) -> dict:
        row = self.worker_row(session_id=self.S1, thread_id=self.S1, agent="claude", workspace_ref="workspace:40",
                              created_at=iso(self.now - timedelta(hours=3)),
                              heartbeat_at=iso(self.now - timedelta(hours=3)), **extra)
        # The stamp stuck on "Running" that night: no Stop hook fires on an API error.
        self.agents[self.S1] = {"state": "Running", "state_ts": (self.now - timedelta(hours=3)).timestamp(),
                                "workspace_id": "w-40", "title": "worker"}
        self.workspaces.append({"ref": "workspace:40", "id": "w-40", "title": "📊 Switchboard Maintenance 1",
                                "description": "switchboard worker rt-wt"})
        return row

    def run_once(self) -> dict:
        return dispatch.handle_network_stops(dispatch.DEFAULTS)

    def test_each_transient_error_form_is_a_network_stop_and_auth_errors_are_not(self) -> None:
        import usage_limits
        for name in NETWORK_ERROR_FORMS:
            found = usage_limits.classify_record(self.error_record(self.now, name))
            self.assertEqual((found or {}).get("kind"), "network", name)
        self.assertEqual(usage_limits.classify_record(self.error_record(self.now, "ENOTFOUND"))["code"], "ENOTFOUND")
        self.assertEqual(usage_limits.classify_record(self.error_record(self.now, "529"))["code"], "529")
        for name in NOT_TRANSIENT_FORMS:
            self.assertIsNone(usage_limits.classify_record(self.error_record(self.now, name)), name)

    def test_each_screen_form_reads_the_last_turn_only(self) -> None:
        import usage_limits
        self.assertEqual(usage_limits.classify_screen(NETWORK_SCREEN)["code"], "ENOTFOUND")
        self.assertEqual(usage_limits.classify_screen(OVERLOADED_SCREEN)["kind"], "network", "a retry hint is not new output")
        self.assertIsNone(usage_limits.classify_screen(MOVED_ON_SCREEN), "real output after the error: it moved on")
        self.assertIsNone(usage_limits.classify_screen(TOOL_SCREEN))
        self.assertIsNone(usage_limits.classify_screen(None))
        self.assertTrue(usage_limits.screen_evidence(NETWORK_SCREEN).startswith('"API Error: Can\'t reach the API server'))
        self.assertEqual(usage_limits.screen_evidence("╭────╮\n│ > │\n╰────╯\n"), "an empty prompt")
        self.assertEqual(usage_limits.screen_evidence(None), "")

    def test_a_network_stop_waits_for_the_api_then_resumes_once_through_the_steer_path(self) -> None:
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        write_ledger(self.stopped_row())
        first = self.run_once()
        self.assertEqual((first["detected"], first["waiting_api"]), (["rt-wt"], ["rt-wt"]))
        self.assertEqual((self.messages, self.told), ([], []), "nothing typed while the API is down")
        self.assertEqual(self.row()["network_stop"]["code"], "ENOTFOUND")
        # The idle nudge and the wedge flag leave the row to this step.
        with patch.object(dispatch, "tab_idle", lambda ref: True), patch.object(ledger, "git_state", lambda t: "open"):
            self.assertEqual(dispatch.nudge_idle(dispatch.DEFAULTS)["resumed"], [])
        self.assertEqual(dispatch.flag_wedged_workers({**dispatch.DEFAULTS, "wedged_hours": 2},
                                                      last_activity=lambda tid: self.now - timedelta(hours=3))["wedged"], [])
        self.api["up"] = True
        second = self.run_once()
        self.assertEqual(second["resumed"], ["rt-wt"])
        self.assertEqual([ref for ref, _ in self.messages], ["workspace:40"])
        self.assertIn("Continue task rt-wt from where it stopped", self.messages[0][1])
        self.assertIn("(ENOTFOUND)", self.messages[0][1])
        self.assertEqual(self.row()["network_resumes"]["count"], 1)
        self.assertEqual(len(self.told), 1)
        self.assertIn("🌐 The Claude API answers again after a network error (ENOTFOUND)", self.told[0])
        third = self.run_once()   # the stub delivers nothing, so the error is still the last record
        self.assertEqual((third.get("backoff"), len(self.messages), len(self.told)), (["rt-wt"], 1, 1),
                         "one continue line per backoff window")

    def test_backoff_then_a_cap_that_tells_cos_once(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        recent = {"attempt_id": "rt-wt.a1.aaa", "count": 1, "last_at": iso(self.now - timedelta(minutes=2))}
        write_ledger(self.stopped_row(network_resumes=recent))
        self.assertEqual(self.run_once().get("backoff"), ["rt-wt"])
        write_ledger(self.stopped_row(network_resumes={**recent, "last_at": iso(self.now - timedelta(minutes=6))}))
        self.assertEqual(self.run_once()["resumed"], ["rt-wt"])
        self.assertEqual(self.row()["network_resumes"]["count"], 2)
        self.messages.clear()
        self.told.clear()
        capped = {"attempt_id": "rt-wt.a1.aaa", "count": dispatch.MAX_NETWORK_RESUMES, "last_at": iso(self.now - timedelta(hours=2))}
        write_ledger(self.stopped_row(network_resumes=capped))
        self.assertEqual([c["task_id"] for c in self.run_once()["capped"]], ["rt-wt"])
        self.run_once()
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.told), 1, "told once per stop")
        self.assertIn(f"after {dispatch.MAX_NETWORK_RESUMES} automatic resumes", self.told[0])
        self.assertFalse(dispatch.network_owned(self.row()), "past the cap the wedge flag may speak again")

    def test_a_new_attempt_starts_its_own_count(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        old = {"attempt_id": "rt-wt.a0.old", "count": dispatch.MAX_NETWORK_RESUMES, "last_at": iso(self.now)}
        write_ledger(self.stopped_row(network_resumes=old))
        self.assertEqual(self.run_once()["resumed"], ["rt-wt"])
        self.assertEqual(self.row()["network_resumes"]["attempt_id"], "rt-wt.a1.aaa")

    def test_a_fresh_error_is_left_for_claude_codes_own_retries(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(seconds=20)))
        write_ledger(self.stopped_row())
        self.assertEqual(self.run_once(), {})
        self.assertEqual((self.messages, self.api["probes"]), ([], 0), "no probe while nothing waits on it")

    def test_a_session_that_moved_on_clears_the_marker(self) -> None:
        answer = {"type": "assistant", "timestamp": iso(self.now), "message": {"role": "assistant", "stop_reason": "end_turn",
                                                                             "content": [{"type": "text", "text": "Done."}]}}
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=30)),
                        {"type": "user", "timestamp": iso(self.now - timedelta(minutes=5)),
                         "message": {"role": "user", "content": "continue"}}, answer)
        write_ledger(self.stopped_row(network_stop={"error_at": "x", "text": "API Error", "code": "ENOTFOUND"}))
        self.assertEqual(self.run_once()["cleared"], ["rt-wt"])
        self.assertNotIn("network_stop", self.row())

    def test_a_session_with_no_transcript_is_read_from_its_screen(self) -> None:
        self.api["up"] = True
        self.screens["workspace:40"] = NETWORK_SCREEN
        write_ledger(self.stopped_row())
        self.assertEqual(self.run_once()["resumed"], ["rt-wt"])
        self.assertTrue(self.row()["network_resumes"]["last_error_at"].startswith("screen:"))
        self.screens["workspace:40"] = MOVED_ON_SCREEN
        self.assertEqual(self.run_once()["cleared"], ["rt-wt"])

    def test_a_refused_send_retries_and_a_timeout_counts_as_sent(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        write_ledger(self.stopped_row())
        self.message_reply = {"ok": False, "reason": "recipient runs no agent that reads messages"}
        self.assertEqual(self.run_once()["retry"], ["rt-wt"])
        self.assertNotIn("network_resumes", self.row())
        self.assertEqual(self.told, [])
        self.message_reply = {"ok": False, "reason": "cmux agent message failed: timed out after 45 s"}
        self.assertEqual(self.run_once()["resumed"], ["rt-wt"], "a timeout may have queued it: never type it twice")

    def test_handle_limits_never_reads_a_network_error_as_a_context_limit(self) -> None:
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=30)))
        write_ledger(self.stopped_row())
        self.agents[self.S1]["state"] = "Idle"
        with patch.object(dispatch, "tab_send", lambda ref, text: self.fail("handle_limits typed into a network stop")):
            result = dispatch.handle_limits(dispatch.DEFAULTS)
        self.assertNotIn("context_restarted", result)
        self.assertEqual((self.row()["status"], self.closed), ("active", []), "the tab is never closed")

    def test_cos_on_a_network_error_is_resumed_through_the_inject_guard(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)), folder="C--a")
        self.transcript(self.COS, self.error_record(self.now - timedelta(minutes=10)), folder="C--switchboard")
        write_ledger(self.stopped_row())
        self.send_ok = False   # a draft in the CoS box: the guard refuses
        with patch.object(dispatch.owner_tabs, "agents_by_workspace",
                          lambda agents=None: {"W-COS": {"agent": "claude", "thread_id": self.COS, "state": "Idle"}}):
            first = self.run_once()
            self.assertTrue(first.get("cos_retry"))
            self.assertEqual(len(self.told), 1, "the worker resume still reaches CoS by agent message")
            self.send_ok = True
            self.sent.clear()
            self.told.clear()
            second = self.run_once()
            self.assertTrue(second.get("cos_resumed"))
            cos_lines = [text for ref, text in self.sent if ref == "workspace:1"]
            self.assertEqual(len(cos_lines), 1)
            self.assertTrue(cos_lines[0].startswith("[ROUTER REPORT] The Claude API answers again"))
            self.assertEqual(self.told, [], "CoS hears it once, through the guard")
            self.sent.clear()
            self.run_once()
            self.assertEqual(self.sent, [], "the same CoS stop is resumed once")

    def test_the_kill_switches_turn_it_off(self) -> None:
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        write_ledger(self.stopped_row())
        for env in ({"SWITCHBOARD_NETWORK_RESUME": "off"}, {"SWITCHBOARD_LIMIT_RESUME": "off"}):
            with patch.dict(os.environ, env):
                self.assertIn("skipped", self.run_once())
        self.assertEqual(self.messages, [])
        self.assertNotIn("network_stop", self.row())

    def test_an_empty_cmux_read_judges_nothing(self) -> None:
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        write_ledger(self.stopped_row())
        self.agents = {}
        self.assertIn("skipped", self.run_once())

    def test_a_row_whose_continuation_owns_the_work_is_left_alone(self) -> None:
        """Review finding: CoS re-dispatched with --continuation-of while the parent's tab
        was still up; a continue line to the parent would put two workers on one job."""
        self.api["up"] = True
        self.transcript(self.S1, self.error_record(self.now - timedelta(minutes=10)))
        heir = {"id": "rt-heir", "status": "active", "origin": "cos", "workstream": "switchboard-maintenance", "prompt": "go on",
                "continuation_of": "rt-wt", "created_at": iso(self.now), "attempt": 1, "attempt_id": "rt-heir.a1.h"}
        write_ledger(self.stopped_row(), heir)
        self.run_once()
        self.assertEqual(self.messages, [])

    def test_with_the_kill_switch_on_a_marked_row_goes_back_to_the_nudge_and_the_wedge_flag(self) -> None:
        """Review finding: a row marked before the switch was set was skipped by everyone."""
        stop = {"error_at": "2026-11-10T00:51:34+00:00", "text": "API Error", "code": "ENOTFOUND"}
        row = self.stopped_row(network_stop=stop)
        self.assertTrue(dispatch.network_owned(row))
        with patch.dict(os.environ, {"SWITCHBOARD_NETWORK_RESUME": "off"}):
            self.assertFalse(dispatch.network_owned(row))
        self.assertIn("network_stop", ledger.ATTEMPT_SCOPED, "a new attempt never inherits the mark")

    def test_the_fourth_resume_keeps_the_row_until_cos_hears_the_cap(self) -> None:
        stop = {"error_at": "e1", "text": "API Error", "code": "ENOTFOUND"}
        spent = {"attempt_id": "rt-wt.a1.aaa", "count": dispatch.MAX_NETWORK_RESUMES}
        self.assertTrue(dispatch.network_owned(self.stopped_row(network_stop=stop, network_resumes=spent)),
                        "no idle nudge right behind the last resume")
        self.assertFalse(dispatch.network_owned(self.stopped_row(network_stop=stop,
                                                                 network_resumes={**spent, "capped_told": "e1"})))

    def test_a_live_retry_or_a_spinner_on_screen_is_not_a_stop(self) -> None:
        import usage_limits
        retrying = ("  ⎿  API Error (Connection error.) · Retrying in 5 seconds… (attempt 3/10)\n"
                    "✻ Thinking… (esc to interrupt)\n╭────╮\n│ > │\n╰────╯\n")
        self.assertIsNone(usage_limits.classify_screen(retrying))
        spinner = NETWORK_SCREEN.replace("\n\n╭", "\n✻ Pondering… (esc to interrupt)\n\n╭", 1)
        self.assertIsNone(usage_limits.classify_screen(spinner))
        newer = NETWORK_SCREEN.replace("\n\n╭", "\n● Retrying the failed upload now.\n● Bash(./retry-upload.sh)\n"
                                                "  ⎿  Upload OK; no retry needed.\n\n╭", 1)
        self.assertIsNone(usage_limits.classify_screen(newer), "new output that says retry is still new output")
        self.api["up"] = True
        self.screens["workspace:40"] = retrying
        write_ledger(self.stopped_row())
        self.assertEqual(self.run_once(), {})
        self.assertEqual(self.messages, [])

    def test_a_bad_settle_value_does_not_break_the_import(self) -> None:
        with patch.dict(os.environ, {"SWITCHBOARD_NETWORK_SETTLE_SECONDS": "ninety"}):
            self.assertEqual(dispatch._env_seconds("SWITCHBOARD_NETWORK_SETTLE_SECONDS", 90), 90)

    def test_the_probe_reads_any_answer_below_500_as_up_and_never_leaves_the_sandbox(self) -> None:
        import urllib.error
        import urllib.request
        # setUp patches dispatch.api_reachable; REAL_API_REACHABLE is the function itself.
        with patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": "1"}):
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("network reached")):
                self.assertFalse(REAL_API_REACHABLE())
                with patch.dict(os.environ, {"SWITCHBOARD_API_PROBE": "up"}):
                    self.assertTrue(REAL_API_REACHABLE())

        def http(code):
            return urllib.error.HTTPError("https://api.anthropic.com/v1/models", code, "x", {}, None)

        with patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": "0"}):
            for side, up in ((http(401), True), (http(404), True), (http(503), False), (http(529), False),
                             (urllib.error.URLError("getaddrinfo ENOTFOUND"), False), (TimeoutError("timed out"), False)):
                with patch.object(urllib.request, "urlopen", side_effect=side):
                    self.assertEqual(REAL_API_REACHABLE(), up, side)


REAL_API_REACHABLE = dispatch.api_reachable


class UsageLimitParseTests(unittest.TestCase):
    """usage_limits.py reads the reset from the epoch, the legacy `|<epoch>` text, or the
    "resets 2:30am (America/Los_Angeles)" text, and only the last record counts."""

    def record(self, text: str, **extra) -> dict:
        return {"type": "assistant", "timestamp": "2026-11-09T09:18:03Z", "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": text}]}, **extra}

    def test_reset_from_the_text_is_the_next_such_time_in_that_zone(self) -> None:
        import usage_limits
        found = usage_limits.classify_record(self.record("You've hit your 5-hour limit · resets 2:30am (America/Los_Angeles)"))
        self.assertEqual(found["resets_at"], datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc))

    def test_legacy_epoch_and_context_text(self) -> None:
        import usage_limits
        self.assertEqual(usage_limits.classify_record(self.record("Claude AI usage limit reached|1794220200"))["resets_at"],
                         datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(usage_limits.classify_record(self.record("Prompt is too long"))["kind"], "context")
        self.assertIsNone(usage_limits.classify_record({**self.record("You've hit your limit"), "isApiErrorMessage": False}))

    def test_a_weekly_reset_with_a_date_and_a_millisecond_epoch(self) -> None:
        import usage_limits
        weekly = usage_limits.classify_record(self.record("You've hit your weekly limit · resets Nov 12, 2pm (America/Los_Angeles)"))
        self.assertEqual(weekly["resets_at"], datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(usage_limits.classify_record(self.record("Claude AI usage limit reached|1794220200000"))["resets_at"],
                         datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc))

    def test_a_record_with_no_timestamp_reads_the_same_every_time(self) -> None:
        import usage_limits
        record = {k: v for k, v in self.record("You've hit your 5-hour limit").items() if k != "timestamp"}
        at = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(usage_limits.classify_record(record, at)["resets_at"], usage_limits.classify_record(record, at)["resets_at"])

    def test_a_reset_before_the_error_falls_back_to_a_later_retry(self) -> None:
        import usage_limits
        found = usage_limits.classify_record(self.record("Claude AI usage limit reached|1794200000"))
        self.assertEqual(found["resets_at"], datetime(2026, 11, 9, 9, 48, 3, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
