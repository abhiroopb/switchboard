#!/usr/bin/env python3
"""Regression tests for the slim orchestration ledger (running-task-state.py).

This suite drives the current CLI against a temp ledger and a
fixture cmux session store. Run from system/scripts:

    python3 -m unittest test_running_task_ledger
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "system" / "scripts" / "running-task-state.py"
STALE_SECONDS = 600
TASK = "rt-000000000009"


def iso_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def session_store(*agents: tuple) -> dict:
    """agents: (workspace_id, thread_id, state, state_ts epoch seconds[, kind]).
    kind defaults to "amp"; "claude" builds the tab the way cmux stores a Claude
    Code session (kind "claude", status key "claude_code", session uuid on
    terminal.resumeBinding.checkpointId)."""
    workspaces = []
    for spec in agents:
        wid, tid, state, ts = spec[:4]
        kind = spec[4] if len(spec) > 4 else "amp"
        terminal = {"agent": {"kind": kind, "sessionId": tid}}
        if kind == "claude":
            terminal["resumeBinding"] = {"kind": "claude", "checkpointId": tid}
        workspaces.append({
            "workspaceId": wid, "title": f"ws {wid}",
            "panels": [{"terminal": terminal}],
            "statusEntries": [{"key": "claude_code" if kind == "claude" else "amp", "value": state, "timestamp": ts}],
        })
    return {"windows": [{"tabManager": {"workspaces": workspaces}}]}



def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, os.path.join(tmp, "l"), target_is_directory=True)
            return True
        except OSError:
            return False


_CAN_SYMLINK = _can_symlink()

class LedgerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ledger = root / "running-tasks.json"
        self.store = root / "session.json"
        self.set_agents()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_agents(self, *agents: tuple[str, str, str, float]) -> None:
        self.store.write_text(json.dumps(session_store(*agents)), encoding="utf-8")

    def run_cli(self, *args: str, expect: int = 0) -> dict:
        env = {
            **os.environ,
            "RUNNING_TASKS_FILE": str(self.ledger),
            "CC_CMUX_SESSION_JSON": str(self.store),
            "SWITCHBOARD_TASK_STALE_SECONDS": str(STALE_SECONDS),
        }
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect, f"{' '.join(args)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def start(self, task_id: str = TASK, **extra: str) -> dict:
        args = ["start", "--task-id", task_id, "--prompt", "do the thing", "--origin", "cos"]
        for key, value in extra.items():
            args += [f"--{key.replace('_', '-')}", value]
        return self.run_cli(*args)

    def ack(self, thread: str = "T-w", ref: str = "workspace:20", **extra: str) -> dict:
        args = ["ack", "--task-id", TASK, "--thread-id", thread, "--workspace-ref", ref]
        for key, value in extra.items():
            args += [f"--{key.replace('_', '-')}", value]
        return self.run_cli(*args)

    def show(self, task_id: str = TASK) -> dict:
        return self.run_cli("show", "--task-id", task_id)

    def patch_row(self, task_id: str = TASK, **values) -> None:
        """Edit the fixture ledger directly: the CLI has no frozen clock, so
        'old' timestamps are written in rather than aged."""
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        row = next(t for t in payload["tasks"] if t["id"] == task_id)
        row.update(values)
        self.ledger.write_text(json.dumps(payload), encoding="utf-8")


class TestLifecycle(LedgerHarness):
    def test_start_ack_heartbeat_and_executor_fence(self) -> None:
        started = self.start(workstream="switchboard-maintenance")
        self.assertEqual(started["status"], "routing")
        self.assertEqual(started["attempt"], 1)
        self.assertTrue(started["attempt_id"].startswith(f"{TASK}.a1."))

        acked = self.ack()
        self.assertEqual(acked["status"], "active")
        self.assertEqual(acked["thread_id"], "T-w")
        self.assertEqual(acked["workspace_ref"], "workspace:20")
        self.assertTrue(acked["started_at"])

        # A second thread cannot take the task over; the owner's heartbeat still lands.
        fenced = self.ack(thread="T-other", ref="workspace:21")
        self.assertTrue(fenced["_fenced_executor"])
        self.assertEqual(fenced["thread_id"], "T-w")
        self.assertEqual(self.show()["workspace_ref"], "workspace:20")

        beat = self.run_cli("heartbeat", "--task-id", TASK, "--thread-id", "T-w")
        self.assertNotIn("_fenced_executor", beat)
        self.assertTrue(beat["heartbeat_at"])

    def test_stale_attempt_id_is_flagged_and_does_not_bind(self) -> None:
        self.start()
        stale = self.ack(attempt_id=f"{TASK}.a0.dead00")
        self.assertTrue(stale["_stale_attempt"])
        self.assertNotIn("thread_id", stale)
        self.assertEqual(self.show()["status"], "routing")

    def test_restart_bumps_attempt_and_clears_worker_identity(self) -> None:
        first = self.start()
        self.ack()
        second = self.start()
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        for key in ("thread_id", "started_at", "heartbeat_at", "report_delivered"):
            self.assertNotIn(key, second)
        self.assertEqual(second["status"], "routing")

    def test_terminal_report_is_one_per_task_and_late_active_cannot_resurrect(self) -> None:
        self.start()
        self.ack()
        done = self.run_cli("update", "--task-id", TASK, "--status", "done", "--summary", "shipped")
        self.assertEqual(done["status"], "done")
        self.assertIs(done["report_delivered"], False)

        dup = self.run_cli("update", "--task-id", TASK, "--status", "blocked", "--summary", "late")
        self.assertTrue(dup["_duplicate_terminal"])
        self.assertEqual(self.show()["summary"], "shipped")

        late = self.run_cli("update", "--task-id", TASK, "--status", "active", "--summary", "still going")
        self.assertTrue(late["_late_active_report"])
        self.assertEqual(self.show()["status"], "done")

        beat = self.run_cli("heartbeat", "--task-id", TASK, "--thread-id", "T-w")
        self.assertTrue(beat["_terminal"])

    def test_another_caller_cannot_settle_a_live_worker_row(self) -> None:
        # A sibling caller must not cancel a row whose worker is live.
        self.start(branch="task/rt-000000000009")
        self.ack()
        self.set_agents(("ws-20", "T-w", "Running", time.time()))
        refused = self.run_cli("update", "--task-id", TASK, "--status", "cancelled", "--summary", "superseded",
                               "--workspace-ref", "workspace:99", "--thread-id", "T-cos")
        self.assertTrue(refused["_live_worker"])
        self.assertEqual((refused["live_thread_id"], refused["live_workspace_ref"]), ("T-w", "workspace:20"))
        row = self.show()
        self.assertEqual((row["status"], row["thread_id"], row["workspace_ref"]), ("active", "T-w", "workspace:20"),
                         "a refused update writes nothing, not even the caller's binding")
        self.assertNotIn("finished_at", row)

        # The worker itself, an explicit overrule, or a worker that is no longer live all settle it.
        own = self.run_cli("update", "--task-id", TASK, "--status", "blocked", "--summary", "needs ruling", "--thread-id", "T-w")
        self.assertEqual(own["status"], "blocked")
        self.run_cli("ack", "--task-id", TASK, "--thread-id", "T-w", "--reopen")
        forced = self.run_cli("update", "--task-id", TASK, "--status", "cancelled", "--summary", "overruled", "--replace-report")
        self.assertEqual(forced["status"], "cancelled")
        self.start()
        self.ack()
        self.set_agents()   # the worker's tab is gone
        gone = self.run_cli("update", "--task-id", TASK, "--status", "cancelled", "--summary", "superseded")
        self.assertEqual(gone["status"], "cancelled")

    def test_blocked_task_reopens_only_in_its_own_worker(self) -> None:
        self.start()
        self.ack()
        self.run_cli("update", "--task-id", TASK, "--status", "blocked", "--summary", "needs ruling")
        other = self.run_cli("ack", "--task-id", TASK, "--thread-id", "T-other", "--workspace-ref", "workspace:30", "--reopen")
        self.assertTrue(other.get("_terminal"))
        self.assertEqual(self.show()["status"], "blocked")
        own = self.run_cli("ack", "--task-id", TASK, "--thread-id", "T-w", "--reopen")
        self.assertEqual(own["status"], "active")
        self.assertNotIn("finished_at", own)
        self.assertNotIn("report_delivered", own)


class TestExpireLeases(LedgerHarness):
    """expire-leases flags (never terminates) active rows whose worker is
    neither heartbeating nor freshly Running in cmux."""

    def _active_with_old_heartbeat(self) -> None:
        self.start()
        self.ack()
        self.patch_row(heartbeat_at=iso_ago(STALE_SECONDS * 3), started_at=iso_ago(STALE_SECONDS * 4))

    def test_fresh_running_stamp_keeps_a_silent_worker_live(self) -> None:
        self._active_with_old_heartbeat()
        self.set_agents(("ws-20", "T-w", "Running", time.time() - 30))
        result = self.run_cli("expire-leases")
        self.assertEqual(result["changed"], [])
        self.assertNotIn("stale_since", self.show())

    def test_claude_code_tab_vouches_for_its_worker_like_an_amp_tab(self) -> None:
        # live_agents() must match kind "claude" as well as "amp".
        # A Claude tab (kind "claude", status key "claude_code") with a fresh
        # Running stamp keeps a silent worker live; a stale one does not.
        self._active_with_old_heartbeat()
        self.set_agents(("ws-20", "T-w", "Running", time.time() - 30, "claude"))
        self.assertEqual(self.run_cli("expire-leases")["changed"], [])
        self.set_agents(("ws-20", "T-w", "Running", time.time() - STALE_SECONDS * 2, "claude"))
        self.assertEqual(len(self.run_cli("expire-leases")["changed"]), 1)

    def test_tool_name_state_counts_as_running(self) -> None:
        # cmux writes the active tool name as the lifecycle value.
        self._active_with_old_heartbeat()
        self.set_agents(("ws-20", "T-w", "shell_command_status", time.time() - 30))
        self.assertEqual(self.run_cli("expire-leases")["changed"], [])

    def test_stale_running_stamp_no_longer_vouches_for_the_worker(self) -> None:
        # The lifecycle can stick at Running after the Amp thread goes idle.
        # An aged stamp must not keep the row live forever.
        self._active_with_old_heartbeat()
        self.set_agents(("ws-20", "T-w", "Running", time.time() - STALE_SECONDS * 2))
        result = self.run_cli("expire-leases")
        self.assertEqual(len(result["changed"]), 1)
        row = result["changed"][0]
        self.assertEqual(row["status"], "active", "flagged, never terminated")
        self.assertTrue(row["stale_since"])
        self.assertIn("no heartbeat since", row["recovery_action"])
        self.assertIn("workspace:20", row["recovery_action"])

    def test_idle_and_needs_input_stamps_do_not_vouch_even_when_fresh(self) -> None:
        for state in ("Idle", "Needs input", ""):
            with self.subTest(state=state):
                self.ledger.unlink(missing_ok=True)
                self._active_with_old_heartbeat()
                self.set_agents(("ws-20", "T-w", state, time.time() - 5))
                self.assertEqual(len(self.run_cli("expire-leases")["changed"]), 1)

    def test_missing_store_or_missing_agent_falls_back_to_heartbeat(self) -> None:
        self._active_with_old_heartbeat()
        self.set_agents(("ws-99", "T-someone-else", "Running", time.time()))
        self.assertEqual(len(self.run_cli("expire-leases")["changed"]), 1)
        self.store.unlink()
        self.assertEqual(self.run_cli("expire-leases")["live_agents"], 0)

    def test_fresh_heartbeat_clears_stale_and_terminal_rows_are_untouched(self) -> None:
        self._active_with_old_heartbeat()
        self.set_agents()
        self.assertTrue(self.run_cli("expire-leases")["changed"][0]["stale_since"])
        beat = self.run_cli("heartbeat", "--task-id", TASK, "--thread-id", "T-w")
        self.assertNotIn("stale_since", beat)
        self.assertNotIn("recovery_action", beat)
        self.assertEqual(self.run_cli("expire-leases")["changed"], [])

        self.run_cli("update", "--task-id", TASK, "--status", "done", "--summary", "shipped")
        self.patch_row(heartbeat_at=iso_ago(STALE_SECONDS * 3))
        self.assertEqual(self.run_cli("expire-leases")["changed"], [])


class TestLandedMidTask(LedgerHarness):
    """A worker may land part of its work with a standalone `dispatch.py finish` and keep
    working. Wrong impls: close a
    landed row while its worker is live; close a quiet one with only the commit id."""

    SESSION = "00000000-0000-4000-8000-0000000000d1"

    def setUp(self) -> None:
        super().setUp()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                    ["commit", "-q", "--allow-empty", "-m", "base"], ["branch", "task/rt-000000000009"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "main"],
                                     check=True, capture_output=True, text=True).stdout.strip()
        self.projects = root / "projects"
        (self.projects / "C--some-worktree").mkdir(parents=True)

    def run_cli(self, *args: str, expect: int = 0) -> dict:
        env = {**os.environ, "RUNNING_TASKS_FILE": str(self.ledger), "CC_CMUX_SESSION_JSON": str(self.store),
               "SWITCHBOARD_TASK_STALE_SECONDS": str(STALE_SECONDS), "REPO_ROOT": str(self.repo),
               "CLAUDE_PROJECTS_DIR": str(self.projects)}
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect, f"{' '.join(args)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def transcript(self, said: str, age: float) -> None:
        stamp = iso_ago(age)
        rows = [{"type": "assistant", "timestamp": stamp,
                 "message": {"content": [{"type": "text", "text": said}, {"type": "tool_use", "name": "Bash"}]}},
                {"type": "assistant", "timestamp": stamp, "isSidechain": True, "message": {"content": "subagent noise"}}]
        path = self.projects / "C--some-worktree" / f"{self.SESSION}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        os.utime(path, (time.time() - age, time.time() - age))   # the file time counts as life too

    def landed_row(self, heartbeat_age: float) -> None:
        self.start(branch="task/rt-000000000009")
        self.ack(thread=self.SESSION)
        self.patch_row(landed_commit=self.commit, session_id=self.SESSION,
                       heartbeat_at=iso_ago(heartbeat_age), started_at=iso_ago(STALE_SECONDS * 4))

    def test_live_worker_keeps_its_row_after_a_mid_task_landing(self) -> None:
        self.landed_row(heartbeat_age=30)
        result = self.run_cli("expire-leases")
        row = self.show()
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["landed_midtask_at"])
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(self.run_cli("expire-leases")["changed"], [], "marked once, not every tick")

    def test_fresh_transcript_counts_as_life_without_a_heartbeat(self) -> None:
        # wmux on Windows strips the heartbeat hook; the transcript still moves.
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        self.transcript("Waiting for the scan.", age=20)
        self.run_cli("expire-leases")
        self.assertEqual(self.show()["status"], "active")

    def test_quiet_worker_closes_done_with_its_last_message(self) -> None:
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        self.transcript("Strip ran; main is clean of the private note. Next: the identifier scan.", age=STALE_SECONDS * 2)
        self.run_cli("expire-leases")
        row = self.show()
        self.assertEqual(row["status"], "done")
        self.assertIs(row["report_delivered"], False)
        self.assertIn(f"Landed on main ({self.commit})", row["summary"])
        self.assertIn("without sending its report", row["summary"])
        self.assertIn("Next: the identifier scan.", row["summary"])
        self.assertNotIn("subagent noise", row["summary"])
        self.assertNotIn("landed_midtask_at", row)

    def test_quiet_worker_with_no_transcript_still_says_no_report_came(self) -> None:
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        self.run_cli("expire-leases")
        row = self.show()
        self.assertEqual(row["status"], "done")
        self.assertIn("without sending its report", row["summary"])
        self.assertNotEqual(row["summary"], f"landed on main as {self.commit}")

    def test_commits_after_the_landing_keep_the_row_open(self) -> None:
        # Code review: the first landing is on main, but the branch moved on. Not done.
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "later work"],
                       check=True, capture_output=True, cwd=str(self.repo))
        subprocess.run(["git", "-C", str(self.repo), "branch", "-f", "task/rt-000000000009", "HEAD"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "reset", "-q", "--hard", "HEAD~1"], check=True, capture_output=True)
        self.run_cli("expire-leases")
        row = self.show()
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["stale_since"], "a quiet worker with unlanded commits is flagged, not closed")

    def test_sub_agent_work_counts_as_life(self) -> None:
        # Code review: a long Task call writes only sidechain records or files under <session>/.
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        self.transcript("Starting the scan.", age=STALE_SECONDS * 2)
        sub = self.projects / "C--some-worktree" / self.SESSION / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
        self.run_cli("expire-leases")
        self.assertEqual(self.show()["status"], "active")

    def test_a_bound_session_waiting_on_a_long_job_gets_a_longer_window(self) -> None:
        # A silent wait on a long background job must not close the row after one window.
        self.landed_row(heartbeat_age=STALE_SECONDS * 3)
        self.transcript("Waiting for the 08:15Z auto-push tick.", age=STALE_SECONDS * 2)
        self.set_agents(("ws-20", self.SESSION, "", time.time() - STALE_SECONDS * 2, "claude"))
        self.run_cli("expire-leases")
        self.assertEqual(self.show()["status"], "active")
        self.transcript("Waiting for the 08:15Z auto-push tick.", age=STALE_SECONDS * 4)
        self.run_cli("expire-leases")
        self.assertEqual(self.show()["status"], "done", "a bound session quiet past 3 windows still closes")

    def test_a_stored_report_wins_even_while_live(self) -> None:
        self.landed_row(heartbeat_age=30)
        self.patch_row(report="Full report text.")
        self.run_cli("expire-leases")
        row = self.show()
        self.assertEqual((row["status"], row["summary"]), ("done", "Full report text."))


class TestReportDelivery(LedgerHarness):
    def _terminal(self) -> None:
        self.start()
        self.ack()
        self.run_cli("update", "--task-id", TASK, "--status", "done", "--summary", "shipped")

    def test_transport_error_deferral_is_counted_and_plain_deferral_is_not(self) -> None:
        self._terminal()
        self.run_cli("report-attempted", "--task-id", TASK)
        self.assertTrue(self.show()["report_attempted_at"])

        plain = self.run_cli("report-not-attempted", "--task-id", TASK)
        self.assertNotIn("report_attempted_at", plain)
        self.assertNotIn("report_transport_errors", plain)

        self.run_cli("report-attempted", "--task-id", TASK)
        once = self.run_cli("report-not-attempted", "--task-id", TASK, "--transport-error")
        self.assertEqual(once["report_transport_errors"], 1)
        self.assertNotIn("report_attempted_at", once)
        twice = self.run_cli("report-not-attempted", "--task-id", TASK, "--transport-error")
        self.assertEqual(twice["report_transport_errors"], 2)
        self.assertIs(twice["report_delivered"], False, "a deferral never settles the row")

    def test_no_destination_keeps_an_earlier_paste_stamp(self) -> None:
        # An earlier attempt may have pasted the line, so a
        # no-destination pass keeps report_attempted_at and the next delivery takes the idle path.
        self._terminal()
        self.run_cli("report-attempted", "--task-id", TASK)
        stamp = self.show()["report_attempted_at"]
        first = self.run_cli("report-not-attempted", "--task-id", TASK, "--no-destination")
        self.assertEqual(first["report_attempted_at"], stamp)
        self.assertTrue(first["report_no_destination_at"])
        again = self.run_cli("report-not-attempted", "--task-id", TASK, "--no-destination")
        self.assertEqual(again["report_no_destination_at"], first["report_no_destination_at"])
        self.assertEqual(again["report_attempted_at"], stamp)

    def test_delivered_with_note_clears_unknown_and_unknown_marks_attempted(self) -> None:
        self._terminal()
        unknown = self.run_cli("report-unknown", "--task-id", TASK)
        self.assertTrue(unknown["report_delivery_unknown"])
        self.assertTrue(unknown["report_attempted_at"])
        self.assertIs(unknown["report_delivered"], False)

        delivered = self.run_cli("report-delivered", "--task-id", TASK,
                                 "--note", "cmux send returned an error; the line was verified on workspace:5")
        self.assertIs(delivered["report_delivered"], True)
        self.assertNotIn("report_delivery_unknown", delivered)
        self.assertIn("verified on workspace:5", delivered["note"])
        self.assertTrue(delivered["report_delivered_at"])

    def test_ack_delivered_marks_only_terminal_rows(self) -> None:
        # `update` needs --status and --summary, so the digest policy goes on with the terminal update.
        self.start()
        self.ack()
        self.run_cli("update", "--task-id", TASK, "--status", "done", "--summary", "shipped", "--delivery", "digest")
        self.start("rt-000000000069")
        acked = self.run_cli("ack-delivered", "--task-id", TASK, "--task-id", "rt-000000000069")
        self.assertEqual(acked["acked"], [TASK])
        self.assertIs(self.show()["report_delivered"], True)
        self.assertNotIn("report_delivered", self.show("rt-000000000069"))

    def test_the_digest_never_settles_a_direct_report(self) -> None:
        # The digest summarizes; the direct report must still land before the
        # worker tab closes.
        self._terminal()
        acked = self.run_cli("ack-delivered", "--task-id", TASK)
        self.assertEqual((acked["acked"], acked["digested_only"]), ([], [TASK]))
        row = self.show()
        self.assertIsNot(row.get("report_delivered"), True)
        self.assertTrue(row["report_in_digest_at"])

    def test_summary_counts_pending_reports_not_delivered_ones(self) -> None:
        self._terminal()
        self.assertEqual(self.run_cli("summary")["pending_report_count"], 1)
        self.run_cli("report-delivered", "--task-id", TASK)
        self.assertEqual(self.run_cli("summary")["pending_report_count"], 0)


class TestRetention(LedgerHarness):
    def test_write_keeps_active_and_undelivered_rows_and_the_newest_delivered_ones(self) -> None:
        # Every write trims delivered terminal rows to the newest 600 by
        # finished_at. Fix this test's shape, not the cap: an active row and an undelivered row older than every delivered
        # row must survive, the two oldest delivered rows must go, and age is the
        # finished_at stamp, not the row's position in the file.
        cap = 600
        delivered = [{"id": f"wr-{n:04}", "status": "done", "report_delivered": True,
                      "finished_at": f"2026-09-{1 + n // 100:02}T00:{(n // 60) % 60:02}:{n % 60:02}Z"}
                     for n in range(cap + 2)]
        delivered.reverse()   # newest first in the file, like the live ledger
        rows = [{"id": "rt-pending", "status": "done", "report_delivered": False, "finished_at": "2026-08-01T00:00:00Z"},
                {"id": "rt-active", "status": "active", "created_at": "2026-08-01T00:00:00Z"}, *delivered]
        self.ledger.write_text(json.dumps({"version": 2, "tasks": rows}), encoding="utf-8")

        self.start("rt-00000000006a")

        ids = [t["id"] for t in json.loads(self.ledger.read_text())["tasks"]]
        self.assertEqual(len(ids), cap + 3)
        self.assertEqual({"rt-00000000006a", "rt-active", "rt-pending"} - set(ids), set())
        self.assertNotIn("wr-0000", ids)
        self.assertNotIn("wr-0001", ids)
        self.assertIn("wr-0002", ids)
        self.assertIn(f"wr-{cap + 1:04}", ids)


class TestSharedStateHome(LedgerHarness):
    @unittest.skipUnless(_CAN_SYMLINK, "symlinks need Developer Mode or admin on Windows")
    def test_writes_through_a_symlinked_ledger_land_on_the_real_file(self) -> None:
        # Worktrees hold system/state/running-tasks.json as a symlink to main's
        # file (dispatch.link_shared_state). os.replace(tmp, link) would swap the
        # link for a worktree-local copy. Wrong impl: the link becomes a regular file, main unchanged.
        real_dir = Path(self.tmp.name) / "main-state"
        real_dir.mkdir()
        real = real_dir / "running-tasks.json"
        real.write_text(json.dumps({"version": 2, "tasks": []}), encoding="utf-8")
        self.ledger.symlink_to(real)

        self.start()

        self.assertTrue(self.ledger.is_symlink())
        self.assertEqual([t["id"] for t in json.loads(real.read_text())["tasks"]], [TASK])
        self.assertTrue((real_dir / "running-tasks.json.lock").exists(), "lock belongs beside the real file")
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["main-state", "running-tasks.json", "session.json"])

    def test_ledger_inside_a_task_worktree_is_refused_naming_main(self) -> None:
        inside = Path(self.tmp.name) / "switchboard" / ".amp" / "worktrees" / "rt-x" / "system" / "state" / "running-tasks.json"
        inside.parent.mkdir(parents=True)
        self.ledger = inside
        env = {**os.environ, "RUNNING_TASKS_FILE": str(inside), "CC_CMUX_SESSION_JSON": str(self.store)}
        proc = subprocess.run([sys.executable, str(SCRIPT), "start", "--task-id", TASK, "--prompt", "x", "--origin", "cos"],
                              capture_output=True, text=True, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(str(Path(self.tmp.name) / "switchboard" / "system" / "state" / "running-tasks.json"), proc.stderr)
        self.assertEqual(list(inside.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
