#!/usr/bin/env python3
"""Behaviour tests for the slim dispatch path (dispatch.py, report-to-cos.sh,
running-task-state.py, command-center-loop.sh). Hermetic: a stub `cmux` whose
CoS screen a test flips between busy and idle, a temp ledger, a temp feed.

Each test names the wrong implementation it catches.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path

# Never open a browser from a test (report-to-cos.sh opens linked compare pages).
os.environ.setdefault("IMAGE_COMPARE_OPEN_DISABLED", "1")
# Never reach a live cmux or wmux tab: lib/mux, the inject guard and the shim fail closed.
os.environ["PM_OS_TEST_SANDBOX"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "system" / "scripts"
REPORT = SCRIPTS / "report-to-cos.sh"
LEDGER = SCRIPTS / "running-task-state.py"
LOOP = SCRIPTS / "command-center-loop.sh"

IDLE_SCREEN = "scrollback\n╭──────────╮\n│          │\n╰──────────╯\n"
BUSY_SCREEN = "scrollback\n╭──────────╮\n│          │\n╰ ≈ Streaming ─╯\n"
DRAFT_SCREEN = "scrollback\n╭──────────╮\n│ a draft  │\n╰ ≈ Streaming ─╯\n"   # busy AND text in the box: nothing may paste

# `cmux` stub: CoS is workspace:252; its screen comes from $STUB_SCREEN_FILE;
# every send is logged (that is the "paste" we must never do twice).
STUB_CMUX = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_CMUX_CALLS"
[[ -n "${STUB_SANDBOX_LOG:-}" ]] && printf '%s\n' "${PM_OS_TEST_SANDBOX:-unset}" >> "$STUB_SANDBOX_LOG"
case "$1" in
  list-workspaces)
    if [[ -n "${STUB_LIST_FILE:-}" && -f "$STUB_LIST_FILE" ]]; then cat "$STUB_LIST_FILE"
    else printf '* workspace:252  🧭 CoS  [selected]\n  workspace:254  🚀 Launchpad\n'; fi ;;
  tree) echo "surface:1" ;;
  read-screen)
    # A per-tab screen ($STUB_SCREEN_FILE.workspace_N) wins over the shared one.
    screen="$STUB_SCREEN_FILE"
    for a in "$@"; do [[ -f "$STUB_SCREEN_FILE.${a//:/_}" ]] && screen="$STUB_SCREEN_FILE.${a//:/_}"; done
    cat "$screen" ;;
  send) printf '%s\n' "$*" >> "$STUB_SENDS"; [[ -f "$STUB_SEND_FAIL" ]] && exit 1 ;;
esac
exit 0
"""


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.cmux = bin_dir / "cmux"
        self.cmux.write_text(STUB_CMUX)
        self.cmux.chmod(self.cmux.stat().st_mode | stat.S_IEXEC)
        self.state = self.tmp / "state"
        (self.state / "logs").mkdir(parents=True)
        (self.state / "next-up-post.json").write_text("{}")
        self.ledger = self.state / "running-tasks.json"
        self.screen = self.tmp / "screen.txt"
        self.sends = self.tmp / "sends.log"
        self.idle()
        # The worker pane's WMUX/CMUX ids would point the scripts at the live tabs.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PM_OS_WORKER", "WMUX", "CMUX_WORKSPACE", "CMUX_SURFACE"))}
        self.env["PM_OS_TEST_SANDBOX"] = "1"
        self.env.update({
            "REPO_ROOT": str(REPO_ROOT), "CMUX_BIN": str(self.cmux), "CMUX": str(self.cmux),
            "STUB_CMUX_CALLS": str(self.tmp / "cmux-calls.log"), "STUB_SCREEN_FILE": str(self.screen),
            "STUB_SENDS": str(self.sends), "STUB_SEND_FAIL": str(self.tmp / "send-fail"),
            "STUB_LIST_FILE": str(self.tmp / "list-workspaces.txt"),
            "PM_OS_STATE_DIR": str(self.state), "RUNNING_TASKS_FILE": str(self.ledger),
            "THREAD_STATUS_FEED_FILE": str(self.state / "thread-status-feed.jsonl"),
            "REPORT_TO_COS_LOCK_DIR": str(self.state / "next-up-post.json.lockdir"),
            "CIG_SEND_LOG": str(self.tmp / "cmux-sends.log"),
            # After hours. A plain done report batches for the
            # digest at any hour; tests of the direct paste path pass
            # --delivery direct (blocked/failed/urgent go direct on their own).
            "REPORT_TO_COS_NOW": "2026-10-04T21:30:00-07:00",
            "REPORT_TO_COS_WAIT_SECONDS": "0", "REPORT_TO_COS_NO_HOOKS": "1", "PM_OS_WORKSTREAM_SLUG": "",
            "REPORT_TO_COS_NO_WAITER": "1",   # waiter tests enable it explicitly
            "REPORT_TO_COS_WAITER_POLL": "1",
            # Windows runs each poll through bash and Python much more slowly.
            "REPORT_TO_COS_WAITER_MAX_SECONDS": "120" if os.name == "nt" else "20",
        })

    def tearDown(self) -> None:
        # Detached waiters outlive the test otherwise (one replayed a fixture into
        # the live CoS after its stub was cleaned up).
        for pidfile in (self.state / "worker-dispatch").glob("*waiter*.pid"):
            pid = pidfile.read_text().strip()
            if pid.isdigit():
                subprocess.run(["bash", "-c", f"kill {pid}"], capture_output=True)
        try:
            self._tmp.cleanup()
        except PermissionError:   # Windows: a waiter can still hold a file in the temp tree
            if os.name != "nt":
                raise

    def idle(self) -> None:
        self.screen.write_text(IDLE_SCREEN)

    def busy(self) -> None:
        self.screen.write_text(BUSY_SCREEN)

    def draft(self) -> None:
        self.screen.write_text(DRAFT_SCREEN)

    def sent(self) -> list[str]:
        return [l for l in self.sends.read_text().splitlines() if l] if self.sends.exists() else []

    def ledger_cmd(self, *args: str) -> dict:
        out = subprocess.run([sys.executable, str(LEDGER), *args], env=self.env, capture_output=True, text=True, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def report(self, *args: str, summary: str = "finished: x") -> dict:
        out = subprocess.run(["bash", str(REPORT), *args, summary], env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def row(self, task_id: str) -> dict:
        return self.ledger_cmd("show", "--task-id", task_id)

    def seed(self, task_id: str, origin: str = "cos") -> None:
        self.ledger_cmd("start", "--task-id", task_id, "--origin", origin, "--workstream", "pm-os-maintenance",
                        "--prompt", "test task")


class SandboxedTestLedger(Harness):
    """A test fixture must never reach the real CoS. A run whose
    ledger is not main's sandboxes itself even when the caller forgot to, so
    every mux call it makes carries PM_OS_TEST_SANDBOX=1 and fails closed
    unless it hits an explicit stub. Wrong impls: trust the caller to set the
    flag; sandbox a default (main) run."""

    def test_a_non_main_ledger_sandboxes_the_run(self) -> None:
        self.env.pop("PM_OS_TEST_SANDBOX", None)
        self.env["STUB_SANDBOX_LOG"] = str(self.tmp / "sandbox.log")
        self.seed("rt-sbox00000001")
        self.report("--task-id", "rt-sbox00000001", "--origin", "cos", "--status", "done", "--delivery", "direct",
                    summary="finished: sandboxed")
        seen = (self.tmp / "sandbox.log").read_text().split()
        self.assertTrue(seen, "the stub cmux was never called")
        self.assertEqual(set(seen), {"1"})

    def test_the_real_shim_refuses_inside_the_sandbox(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "PM_OS_TEST_WMUX_BIN"}
        out = subprocess.run([sys.executable, str(SCRIPTS / "mux" / "cmux-shim.py"), "list-workspaces"],
                             env=env, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("pm-os-test-sandbox", out.stderr)


class OpensSharedComparePage(Harness):
    """A delivered report that links a compare page opens it on the owner's machine."""

    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "vault").mkdir()
        self.vault = (self.tmp / "vault").resolve()
        self.live = self.vault / ".amp/worktrees/rt-open00000001/workstreams/demo/assets/compare/p.html"
        self.live.parent.mkdir(parents=True)
        self.live.write_text("<html></html>")
        self.opened = self.tmp / "opened.log"
        rec = self.tmp / "bin" / "rec-open"
        rec.write_text(f'#!/bin/sh\necho "$1" >> "{self.opened}"\n', newline="\n")
        rec.chmod(0o755)
        self.env.pop("IMAGE_COMPARE_OPEN_DISABLED", None)
        self.env.update({"PM_OS_OPEN_CMD": str(rec), "PM_OS_VAULT_ROOT": str(self.vault)})

    @unittest.skipIf(os.name == "nt", "expects POSIX paths and URLs, and an extensionless fake opener Windows cannot run")
    def test_parked_ask_opens_the_live_worktree_page_once(self) -> None:
        # Wrong impls: the link names main, where the page is not yet (an
        # event-page miss); nothing opens; a repeat of the same report opens it again.
        self.seed("rt-open00000001")
        summary = f"waiting on the owner: pick a cover. [Compare 3 images](file://{self.vault}/workstreams/demo/assets/compare/p.html)"
        out = self.report("--task-id", "rt-open00000001", "--origin", "cos", "--status", "active", "--needs-ruling",
                          "--delivery", "direct", summary=summary)
        self.assertTrue(out["delivered"], out)
        self.assertEqual(out["opened_pages"], [str(self.live)])
        self.assertEqual(self.opened.read_text().splitlines(), [str(self.live)])
        self.assertIn(f"file://{self.live}", self.sent()[-1], "the delivered link names the copy that exists")
        self.report("--task-id", "rt-open00000001", "--origin", "cos", "--status", "active", "--needs-ruling",
                    "--delivery", "direct", summary=summary + " (again)")
        self.assertEqual(len(self.opened.read_text().splitlines()), 1, "one open per page version")

    def test_kill_switch_and_undelivered_reports_open_nothing(self) -> None:
        self.seed("rt-open00000002")
        summary = f"waiting on the owner: [Compare](file://{self.live})"
        self.busy()
        out = self.report("--task-id", "rt-open00000002", "--origin", "cos", "--status", "active", "--needs-ruling",
                          "--delivery", "direct", summary=summary)
        self.assertFalse(out["delivered"])
        self.idle()
        self.env["IMAGE_COMPARE_OPEN_DISABLED"] = "1"
        out = self.report("--task-id", "rt-open00000002", "--origin", "cos", "--status", "active", "--needs-ruling",
                          "--delivery", "direct", summary=summary + " retry")
        self.assertEqual(out["opened_pages"], [])
        self.assertFalse(self.opened.exists())


class DeliveryOncePerTask(Harness):
    """One message per task: never paste the same terminal report twice."""

    def test_busy_first_attempt_pastes_nothing_and_stays_pending(self) -> None:
        # Wrong impl: queue paste into the live composer on the first attempt
        # (lands mid-turn, read as a replay, outcome unknown so never retried).
        self.seed("rt-000000000054")
        self.busy()
        first = self.report("--task-id", "rt-000000000054", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertFalse(first["delivered"])
        self.assertEqual(first["delivery_outcome"], "deferred")
        self.assertEqual(self.sent(), [], "a busy CoS never gets a composer paste")
        row = self.row("rt-000000000054")
        self.assertFalse(row.get("report_delivery_unknown", False))
        self.assertIs(row["report_delivered"], False, "still pending: the waiter / loop deliver it when idle")
        self.idle()
        self.report("--task-id", "rt-000000000054", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertEqual(len(self.sent()), 1)
        self.assertTrue(self.row("rt-000000000054")["report_delivered"])

    def test_deferred_report_retries_only_through_idle_path(self) -> None:
        # Wrong impl: a retry of a pending row falls back to the queue paste.
        self.seed("rt-000000000055")
        self.draft()
        first = self.report("--task-id", "rt-000000000055", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertFalse(first["delivered"])
        self.assertEqual(self.sent(), [])
        self.busy()   # empty box but streaming: queue would paste here; a retry must not
        for _ in range(2):
            again = self.report("--task-id", "rt-000000000055", "--origin", "cos", "--status", "done", "--delivery", "direct")
            self.assertFalse(again["delivered"])
            self.assertEqual(self.sent(), [])
        self.assertNotIn("report_attempted_at", self.row("rt-000000000055"), "clean deferral leaves the row retryable")
        self.idle()
        final = self.report("--task-id", "rt-000000000055", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertTrue(final["delivered"])
        self.assertEqual(len(self.sent()), 1)
        self.assertTrue(self.row("rt-000000000055")["report_delivered"])
        self.report("--task-id", "rt-000000000055", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertEqual(len(self.sent()), 1)

    # cmux `send` returned nonzero (rc 3). Its exit status is not the outcome; the
    # destination screen is (most "unknown" rows have in fact landed).

    def test_transport_error_with_line_on_screen_is_delivered(self) -> None:
        # Wrong impl: rc 3 -> unknown even though the line is visible in CoS, so
        # the row never settles and the tab is never reaped.
        self.seed("rt-000000000056")
        (self.tmp / "send-fail").write_text("")
        # The lead names the workstream, the task id follows it (Rule 13).
        self.screen.write_text("scrollback\n│ [🛠️ PM OS Maintenance · rt-000000000056] done: finished: x\n" + IDLE_SCREEN)
        first = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertTrue(first["delivered"])
        self.assertEqual(first["delivery_outcome"], "delivered")
        self.assertEqual(len(self.sent()), 1)
        row = self.row("rt-000000000056")
        self.assertTrue(row["report_delivered"])
        self.assertNotIn("report_delivery_unknown", row)
        self.assertIn("verified", row.get("note", ""))
        (self.tmp / "send-fail").unlink()
        self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertEqual(len(self.sent()), 1, "a delivered row is never pasted again")

    def test_transport_error_with_nothing_pasted_gets_exactly_one_retry(self) -> None:
        # Wrong impls: (a) rc 3 on an idle, empty screen is final, so a transient
        # cmux error loses the report; (b) the retry is unbounded, so a dead
        # transport pastes on every pass.
        self.seed("rt-000000000056")
        (self.tmp / "send-fail").write_text("")
        first = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertFalse(first["delivered"])
        self.assertEqual(first["delivery_outcome"], "deferred")
        self.assertEqual(len(self.sent()), 1)
        row = self.row("rt-000000000056")
        self.assertFalse(row.get("report_delivery_unknown", False))
        self.assertNotIn("report_attempted_at", row, "the screen proved nothing was pasted: retryable")
        self.assertEqual(row["report_transport_errors"], 1)
        # Transport still broken: the second failure is final.
        second = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertEqual(second["delivery_outcome"], "unknown")
        self.assertEqual(len(self.sent()), 2)
        self.assertTrue(self.row("rt-000000000056")["report_delivery_unknown"])
        (self.tmp / "send-fail").unlink()
        third = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertEqual(len(self.sent()), 2, "a row with a final unknown outcome is never pasted again")
        self.assertTrue(third.get("ignored"))

    def test_transport_error_recovers_when_the_retry_succeeds(self) -> None:
        self.seed("rt-000000000056")
        (self.tmp / "send-fail").write_text("")
        self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        (self.tmp / "send-fail").unlink()
        again = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertTrue(again["delivered"])
        self.assertEqual(len(self.sent()), 2)
        self.assertTrue(self.row("rt-000000000056")["report_delivered"])

    def test_transport_error_with_a_draft_or_busy_screen_stays_unknown(self) -> None:
        # Wrong impl: retry while text sits in the composer (it may be our
        # half-typed line) or while CoS is mid-turn (our line may have landed
        # and scrolled away): that is the double paste the one-message rule forbids.
        idle_draft = "scrollback\n╭──────────╮\n│ [rt-aaaa0 │\n╰──────────╯\n"   # our half-typed line, agent idle
        for screen in (DRAFT_SCREEN, BUSY_SCREEN, idle_draft):
            self.seed("rt-000000000056")
            (self.tmp / "send-fail").write_text("")
            self.idle()
            # cig_safe_send needs an idle screen to attempt; the stub flips to the
            # post-send screen when the send is recorded.
            self.cmux.write_text(STUB_CMUX.replace('[[ -f "$STUB_SEND_FAIL" ]] && exit 1',
                                                   'cat "$STUB_AFTER_SEND" > "$STUB_SCREEN_FILE"; exit 1'))
            (self.tmp / "after-send").write_text(screen)
            self.env["STUB_AFTER_SEND"] = str(self.tmp / "after-send")
            result = self.report("--task-id", "rt-000000000056", "--origin", "cos", "--status", "done", "--delivery", "direct")
            self.assertEqual(result["delivery_outcome"], "unknown", screen)
            row = self.row("rt-000000000056")
            self.assertTrue(row.get("report_delivery_unknown"))
            self.assertIn("report_attempted_at", row)
            self.assertNotIn("report_transport_errors", row)
            self.sends.unlink()
            self.ledger.unlink()

    def test_terminal_report_clears_admission_blocked_and_loop_uses_row_status(self) -> None:
        # Wrong impl: loop sends "--status blocked" for a done row with a stale flag.
        self.seed("rt-000000000057")
        self.ledger_cmd("update", "--task-id", "rt-000000000057", "--status", "active", "--summary", "working")
        payload = json.loads(self.ledger.read_text())
        next(t for t in payload["tasks"] if t["id"] == "rt-000000000057")["admission_blocked"] = True
        self.ledger.write_text(json.dumps(payload))
        self.draft()
        self.report("--task-id", "rt-000000000057", "--origin", "cos", "--status", "done", "--delivery", "direct", summary="finished: three")
        row = self.row("rt-000000000057")
        self.assertNotIn("admission_blocked", row)
        self.assertEqual(row["status"], "done")
        # The loop's retry pass must report the row as done (its own status), and
        # while CoS is busy it must paste nothing.
        scan = subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                              capture_output=True, text=True)
        self.assertIn('"status": "done"', scan.stdout + scan.stderr)
        self.assertNotIn('"status": "blocked"', scan.stdout + scan.stderr)
        self.assertEqual(self.sent(), [])
        self.idle()
        subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                       capture_output=True, text=True)
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("done: finished: three", self.sent()[0])
        subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                       capture_output=True, text=True)
        self.assertEqual(len(self.sent()), 1, "a delivered row is never re-sent by a later pass")

    def test_loop_retry_keeps_no_carriage_return_in_the_thread_id(self) -> None:
        # Windows python prints CRLF and read keeps the CR on the last field;
        # a retry must not append it to the row's thread_id.
        self.seed("rt-000000000058")
        payload = json.loads(self.ledger.read_text())
        row = next(t for t in payload["tasks"] if t["id"] == "rt-000000000058")
        row.update(status="done", summary="finished: five", report_delivered=False, thread_id="T-abc\r\r")
        self.ledger.write_text(json.dumps(payload))
        self.busy()
        subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                       capture_output=True, text=True)
        self.assertEqual(self.row("rt-000000000058")["thread_id"], "T-abc")

    def test_loop_settles_a_cancelled_row_without_sending(self) -> None:
        # A cancelled row with report_delivered false must not be retried on
        # every tick or spawn a waiter. A cancelled row has no report (reap and
        # the waiter agree).
        self.seed("rt-000000000055")
        payload = json.loads(self.ledger.read_text())
        row = next(t for t in payload["tasks"] if t["id"] == "rt-000000000055")
        row.update(status="cancelled", summary="false alarm", report_delivered=False)
        self.ledger.write_text(json.dumps(payload))
        self.idle()
        subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                       capture_output=True, text=True)
        self.assertEqual(self.sent(), [], "a cancelled row sends nothing")
        self.assertIs(self.row("rt-000000000055")["report_delivered"], True, "settled, so no later tick retries it")


class BoundedDelivery(Harness):
    """A done report never waits silently for the next loop pass: a waiter
    delivers it the moment CoS's turn ends, still exactly once, never elsewhere."""

    def setUp(self) -> None:
        super().setUp()
        self.env.pop("REPORT_TO_COS_NO_WAITER")
        self.pidfile = self.state / "worker-dispatch" / "report-waiter-rt-000000000059.pid"

    def wait_for(self, predicate, seconds: float = 60 if os.name == "nt" else 12) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail("condition not met within %.0fs" % seconds)

    def waiter_alive(self) -> bool:
        try:
            pid = int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return False
        if os.name == "nt":   # a Git Bash PID, and os.kill(pid, 0) would terminate a Windows process
            return subprocess.run(["bash", "-c", f"kill -0 {pid}"], capture_output=True).returncode == 0
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_busy_cos_spawns_one_waiter_that_delivers_once_when_idle(self) -> None:
        # Wrong impls: (a) row sits pending until a loop pass hours later; (b) the
        # line is routed to Launchpad (workspace:254) instead; (c) a second
        # deferred attempt spawns a second waiter and the line lands twice.
        self.seed("rt-000000000059")
        self.busy()
        first = self.report("--task-id", "rt-000000000059", "--origin", "cos", "--status", "done", "--delivery", "direct", summary="finished: waited")
        self.assertEqual(first["delivery_outcome"], "deferred")
        self.assertTrue(first["waiter"])
        self.wait_for(self.waiter_alive, 5)
        again = self.report("--task-id", "rt-000000000059", "--origin", "cos", "--status", "done", "--delivery", "direct", summary="finished: waited")
        self.assertTrue(again["waiter"], "second deferral reuses the live waiter")
        time.sleep(2.5)   # several polls while busy
        self.assertEqual(self.sent(), [], "busy CoS: the waiter pastes nothing")
        self.assertTrue(self.waiter_alive())
        self.idle()
        self.wait_for(lambda: len(self.sent()) >= 1)
        self.wait_for(lambda: self.row("rt-000000000059")["report_delivered"] is True)
        self.wait_for(lambda: not self.waiter_alive())
        time.sleep(1.5)
        sent = self.sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("--workspace workspace:252", sent[0], "CoS report goes to CoS, never Launchpad")
        self.assertIn("done: finished: waited", sent[0])
        self.assertFalse(self.pidfile.exists())

    def test_one_queue_per_tab_sends_owner_reports_first_and_notifies_while_busy(self) -> None:
        # One waiter per tab, owner results first, and the owner is told while CoS is busy.
        # Wrong impls: (a) a second waiter polls
        # the same tab; (b) a system notice deferred first is sent before an owner result;
        # (c) the owner is never told reports are waiting; (d) the notice is typed into the turn.
        notice, owner = "auto-push-20261004-2000", "rt-000000000059"
        self.seed(notice)
        self.seed(owner)
        self.busy()
        self.report("--task-id", notice, "--origin", "cos", "--status", "blocked", summary="auto-push: refusing to push")
        self.wait_for(lambda: (self.state / "worker-dispatch" / "report-queue-workspace_252.pid").exists(), 10)
        second = self.report("--task-id", owner, "--origin", "cos", "--status", "done", "--delivery", "direct",
                             summary="finished: owner result")
        self.assertTrue(second["waiter"])
        log = self.state / "worker-dispatch" / "report-waiter.log"
        try:
            self.wait_for(lambda: log.exists() and "queued: the workspace:252 queue" in log.read_text(), 30)
        except AssertionError:
            self.fail("no queued line; waiter log:\n" + (log.read_text() if log.exists() else "(none)"))
        calls = self.tmp / "cmux-calls.log"
        self.wait_for(lambda: calls.exists() and "2 PM OS reports waiting for CoS" in calls.read_text(), 30)
        self.assertEqual(self.sent(), [], "busy CoS: nothing is typed, the notification is not a send")
        self.idle()
        self.wait_for(lambda: len(self.sent()) >= 2)
        self.wait_for(lambda: self.row(notice)["report_delivered"] is True and self.row(owner)["report_delivered"] is True)
        sent = self.sent()
        self.assertEqual(len(sent), 2, sent)
        self.assertIn("finished: owner result", sent[0], "the owner result goes first")
        self.assertIn("auto-push: refusing to push", sent[1])

    def test_waiter_follows_a_new_cos_tab(self) -> None:
        # The owner starts a new CoS (cos-autobind moves CoS to another workspace ref).
        # Wrong impls: (a) the waiter keeps polling the ref it was
        # spawned with, which never goes idle, so the report waits out its 6 h; (b) it sends to the
        # old tab. The replay must land in the tab that is CoS now, once.
        self.env["REPORT_TO_COS_WAITER_RESOLVE_SECONDS"] = "0"
        self.seed("rt-000000000059")
        self.busy()
        first = self.report("--task-id", "rt-000000000059", "--origin", "cos", "--status", "done",
                            "--delivery", "direct", summary="finished: follows CoS")
        self.assertTrue(first["waiter"])
        self.wait_for(self.waiter_alive, 5)
        # The owner opens a new CoS: the old tab is retitled and stays busy, the new one is idle.
        (self.tmp / "list-workspaces.txt").write_text(
            "  workspace:252  🧭 CoS (old claude)\n* workspace:260  🧭 CoS  [selected]\n  workspace:254  🚀 Launchpad\n",
            encoding="utf-8", newline="\n")   # a CRLF listing hides every title from the bash matcher
        Path(f"{self.screen}.workspace_260").write_text(IDLE_SCREEN, encoding="utf-8")
        log_path = self.state / "worker-dispatch" / "report-waiter.log"
        try:
            self.wait_for(lambda: len(self.sent()) >= 1)
        except AssertionError:
            self.fail("nothing sent; waiter log:\n" + (log_path.read_text(encoding="utf-8") if log_path.exists() else "(none)"))
        self.wait_for(lambda: self.row("rt-000000000059")["report_delivered"] is True)
        self.wait_for(lambda: not self.waiter_alive())
        sent = self.sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("--workspace workspace:260", sent[0], "the report goes to the CoS tab of now")
        log = (self.state / "worker-dispatch" / "report-waiter.log").read_text(encoding="utf-8")
        self.assertIn("moved from workspace:252 to workspace:260; following it", log)
        self.assertFalse((self.state / "worker-dispatch" / "report-queue-workspace_252.pid").exists())

    def test_waiter_and_loop_pass_together_send_once(self) -> None:
        # Wrong impl: waiter and command-center-loop both read "pending" before
        # the lock and both send.
        self.seed("rt-000000000059")
        self.busy()
        self.report("--task-id", "rt-000000000059", "--origin", "cos", "--status", "done", "--delivery", "direct", summary="finished: raced")
        self.wait_for(self.waiter_alive, 5)
        self.idle()
        loop_env = {**self.env, "STATE_DIR": str(self.state), "REPORT_TO_COS_NO_WAITER": "1"}
        procs = [subprocess.Popen(["bash", str(LOOP), "--retry-reports-only"], env=loop_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        for p in procs:
            p.wait(timeout=30)
        self.wait_for(lambda: not self.waiter_alive(), 8)
        time.sleep(1.5)
        self.assertEqual(len(self.sent()), 1, self.sent())
        self.assertTrue(self.row("rt-000000000059")["report_delivered"])

    def test_waiter_exits_when_someone_else_settles_the_row(self) -> None:
        # Wrong impl: waiter keeps polling (and later sends) after the loop
        # already delivered or the row became outcome-unknown.
        self.seed("rt-000000000059")
        self.busy()
        self.report("--task-id", "rt-000000000059", "--origin", "cos", "--status", "done", "--delivery", "direct", summary="finished: settled")
        self.wait_for(self.waiter_alive, 5)
        self.ledger_cmd("report-delivered", "--task-id", "rt-000000000059")
        self.wait_for(lambda: not self.waiter_alive(), 5)
        self.idle()
        time.sleep(2)
        self.assertEqual(self.sent(), [])


POSTER = SCRIPTS / "post-next-up-digest.sh"
STALE_CHECK = SCRIPTS / "check-digest-staleness.py"


class NextUpDigestDelivery(Harness):
    """The next-up digest is the only path for non-urgent CoS completions. It
    must not stall when the poster samples a short idle window against a CoS
    that streams most of the day, when a draft resets the queue-escalation
    streak, or when one rc-3 queue attempt latches queueing off. Each test
    names the wrong impl."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.tmp / "router.yaml"
        self.cfg.write_text(
            "next-up:\n  cadence_minutes: 30\n  working_hours_start: \"08:00\"\n  working_hours_end: \"18:00\"\n"
            "  timezone: America/Los_Angeles\n  send_wait_seconds: 1\n  send_poll_seconds: 1\n"
            "  queue_after_busy_deferrals: 3\n  unknown_hold_minutes: 120\n")
        self.digest_state = self.state / "next-up-post.json"
        self.pidfile = self.state / "worker-dispatch" / "next-up-waiter.pid"
        self.env.update({
            "NEXT_UP_CONFIG_FILE": str(self.cfg), "NEXT_UP_STATE_FILE": str(self.digest_state),
            "NEXT_UP_LOCK_DIR": str(self.state / "next-up-post.json.lockdir"), "COS_WORKSPACE_REF": "workspace:252",
            "NEXT_UP_NOW": "2026-10-06T10:00:00-07:00",   # Tuesday, in hours
            "NEXT_UP_NO_WAITER": "1", "NEXT_UP_WAITER_POLL": "1",
            "NEXT_UP_WAITER_MAX_SECONDS": "120" if os.name == "nt" else "20",
            "NEXT_UP_DIGEST_FILE": str(self.tmp / "no-command-center.md"),
            # The poster stamps meeting ingests as shown after a post; keep the
            # stamp and the daily notes it reads inside the temp root so a test
            # run never touches .state/meeting-ingests-digest.json.
            "MEETING_INGESTS_STAMP_FILE": str(self.state / "meeting-ingests-digest.json"),
            "MEETING_INGESTS_DAILY_DIR": str(self.tmp / "daily"),
        })

    def post(self, **env: str) -> dict:
        out = subprocess.run(["bash", str(POSTER)], env={**self.env, **env}, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def seed_digest_row(self, task_id: str, summary: str = "pushed 1 commit to origin/main") -> None:
        # A done row routed to the digest (what dispatch.py auto-push writes).
        self.seed(task_id)
        self.report("--task-id", task_id, "--origin", "cos", "--status", "done", "--delivery", "digest", summary=summary)
        self.assertIs(self.row(task_id)["report_delivered"], False)

    def digest_state_json(self) -> dict:
        return json.loads(self.digest_state.read_text())

    def wait_for(self, predicate, seconds: float = 60 if os.name == "nt" else 12) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail("condition not met within %.0fs" % seconds)

    def waiter_alive(self) -> bool:
        try:
            pid = int(self.pidfile.read_text().strip())
            if os.name == "nt":   # a Git Bash PID, and os.kill(pid, 0) would terminate a Windows process
                return subprocess.run(["bash", "-c", f"kill -0 {pid}"], capture_output=True).returncode == 0
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def test_busy_cos_spawns_one_digest_waiter_that_posts_once_when_idle(self) -> None:
        # Wrong impls: (a) the deferred digest waits for the next tick's 45 s
        # window and never catches an always-streaming CoS; (b) a second deferral
        # spawns a second waiter and the digest lands twice.
        self.env.pop("NEXT_UP_NO_WAITER")
        self.seed_digest_row("auto-push-20261006-0900")
        self.busy()
        first = self.post()
        self.assertEqual(first["status"], "deferred", first)
        self.assertEqual(first["reason"], "busy")
        self.assertTrue(first["waiter"], first)
        self.wait_for(self.waiter_alive, 5)
        again = self.post()
        self.assertEqual(again["status"], "deferred")
        self.assertTrue(again["waiter"], "second deferral reuses the live waiter")
        time.sleep(2.5)
        self.assertEqual(self.sent(), [], "busy CoS: the waiter pastes nothing")
        self.idle()
        self.wait_for(lambda: len(self.sent()) >= 1)
        self.wait_for(lambda: self.row("auto-push-20261006-0900")["report_delivered"] is True)
        self.wait_for(lambda: not self.waiter_alive(), 60 if os.name == "nt" else 5)
        time.sleep(1.5)
        sent = self.sent()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("--workspace workspace:252", sent[0])
        self.assertIn("[NEXT-UP AUTOPOST]", sent[0])
        self.assertIn("pushed 1 commit to origin/main", sent[0])
        state = self.digest_state_json()
        self.assertEqual(state["last_status"], "posted")
        self.assertEqual(state["deferred_streak"], 0)
        self.assertFalse(self.pidfile.exists())
        # The slot is now processed: the loop's next tick in the same slot skips.
        self.assertEqual(self.post()["status"], "cadence-skip")

    def test_draft_deferral_keeps_the_busy_streak(self) -> None:
        # Wrong impl: a draft resets busy_deferred_streak to 0, so three busy
        # ticks in a row never accumulate while the owner types between turns.
        self.seed_digest_row("auto-push-20261006-0901")
        self.busy()
        self.post(NEXT_UP_NOW="2026-10-06T10:00:00-07:00")
        self.post(NEXT_UP_NOW="2026-10-06T10:31:00-07:00")
        self.assertEqual(self.digest_state_json()["busy_deferred_streak"], 2)
        self.draft()
        out = self.post(NEXT_UP_NOW="2026-10-06T11:02:00-07:00")
        self.assertEqual(out["reason"], "draft")
        self.assertEqual(self.digest_state_json()["busy_deferred_streak"], 2, "a draft does not reset the streak")
        self.busy()
        self.post(NEXT_UP_NOW="2026-10-06T11:33:00-07:00")
        self.assertEqual(self.digest_state_json()["busy_deferred_streak"], 3)
        self.assertEqual(self.sent(), [])

    def test_empty_lock_folder_is_reclaimed_once_stale(self) -> None:
        # Wrong impl: a lock folder with no pid file counted as held
        # forever, so one empty folder Windows kept answered "locked" to every tick.
        lock = self.state / "next-up-post.json.lockdir"
        lock.mkdir(parents=True)
        self.assertEqual(self.post()["status"], "locked", "a fresh empty folder may be a holder mid-mkdir")
        old = time.time() - 600
        os.utime(lock, (old, old))
        out = self.post()
        self.assertNotEqual(out["status"], "locked", out)
        self.assertFalse(lock.exists(), "the poster removes its lock on exit")

    def test_unknown_hold_is_bounded_so_queue_escalation_resumes(self) -> None:
        # Wrong impl: any unknown-held row keeps queue_ready off until a human
        # reconciles it (one rc-3 attempt switched the queue off for a day).
        self.seed_digest_row("auto-push-20261006-0902")
        self.seed_digest_row("auto-push-20261006-0903")
        self.ledger_cmd("report-unknown", "--task-id", "auto-push-20261006-0903")
        self.digest_state.write_text(json.dumps({"version": 1, "busy_deferred_streak": 3, "deferred_reason": "busy"}))
        self.busy()
        # Fresh hold (attempted just now): no queue attempt, idle path defers.
        out = self.post(NEXT_UP_NOW="2026-10-06T10:00:00-07:00")
        self.assertEqual(out["status"], "deferred")
        self.assertEqual(self.sent(), [], "a fresh unknown hold still blocks the queue paste")
        # Age the hold past unknown_hold_minutes: the queue path is tried again.
        payload = json.loads(self.ledger.read_text())
        for task in payload["tasks"]:
            if task["id"] == "auto-push-20261006-0903":
                task["report_attempted_at"] = "2026-10-06T10:00:00Z"
        self.ledger.write_text(json.dumps(payload))
        self.post(NEXT_UP_NOW="2026-10-06T10:31:00-07:00")
        queue_sends = [l for l in self.sent() if "[NEXT-UP AUTOPOST]" in l and not l.rstrip().endswith("\\r")]
        self.assertEqual(len(queue_sends), 1, self.sent())
        # The static stub screen never shows the typed draft, so the queue stage
        # is unverifiable: rows are held, never acked, nothing is retyped.
        self.assertIs(self.row("auto-push-20261006-0902")["report_delivered"], False)
        self.assertTrue(self.row("auto-push-20261006-0902").get("report_delivery_unknown"))

    def test_transport_error_is_judged_by_the_screen_not_the_exit_code(self) -> None:
        # Wrong impl: cmux nonzero -> delivery-unknown and rows held, even when
        # the screen proves nothing was pasted (deferred) or that it landed (posted).
        self.seed_digest_row("auto-push-20261006-0904")
        (self.tmp / "send-fail").write_text("")
        self.idle()
        out = self.post(NEXT_UP_NOW="2026-10-06T10:00:00-07:00")
        self.assertEqual(out["status"], "deferred", out)
        self.assertEqual(out["reason"], "transport-retry")
        row = self.row("auto-push-20261006-0904")
        self.assertIs(row["report_delivered"], False)
        self.assertNotIn("report_delivery_unknown", row)
        self.screen.write_text("scrollback\n│ [NEXT-UP AUTOPOST] Present the digest below as its === sections ===\n" + IDLE_SCREEN)
        out = self.post(NEXT_UP_NOW="2026-10-06T10:31:00-07:00")
        self.assertEqual(out["status"], "posted", out)
        self.assertTrue(self.row("auto-push-20261006-0904")["report_delivered"])

    def test_after_hours_pending_count_matches_what_the_digest_carries(self) -> None:
        # Wrong impl: the after-hours refresh counts every undelivered cos row
        # (19) while the render carries only digest-eligible rows (13).
        self.seed_digest_row("auto-push-20261006-0905")
        self.seed_digest_row("auto-push-20261006-0906")
        self.ledger_cmd("report-unknown", "--task-id", "auto-push-20261006-0906")
        self.seed("rt-00000000005a")
        self.busy()
        self.report("--task-id", "rt-00000000005a", "--origin", "cos", "--status", "blocked", summary="blocked: needs a ruling")
        out = self.post(NEXT_UP_NOW="2026-10-06T21:00:00-07:00")
        self.assertEqual(out["status"], "outside-working-hours")
        self.assertEqual(out["pending_reports"], 1, out)
        self.assertEqual(self.digest_state_json()["pending_report_count"], 1)


class StaleDigestAlarm(Harness):
    """Pending reports and no digest for more than one working day: one direct
    CoS line per day through report-to-cos.sh (the inventory check's path)."""

    def setUp(self) -> None:
        super().setUp()
        self.alarm = self.state / "next-up-stale-alarm.json"
        self.digest_state = self.state / "next-up-post.json"
        self.calls = self.tmp / "stale-report-calls.log"
        stub = self.tmp / "stub-report-to-cos.sh"
        stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$STALE_CALLS"\n'
                        'env | grep -E "^(PM_OS_WORKER|REPORT_TO_COS_NO_HOOKS)" >> "$STALE_CALLS.env"\n'
                        'echo \'{"delivered": true}\'\n', newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.env.update({
            "STALE_CALLS": str(self.calls), "DIGEST_STALE_REPORT_SCRIPT": str(stub),
            "NEXT_UP_STATE_FILE": str(self.digest_state), "DIGEST_STALE_ALARM_FILE": str(self.alarm),
            "NEXT_UP_CONFIG_FILE": str(REPO_ROOT / "system" / "config" / "router.yaml"),
            "PM_OS_WORKER_ID": "worker-test", "PM_OS_WORKER_TASK_ID": "rt-test",
        })
        self.digest_state.write_text(json.dumps({
            "version": 1, "last_posted_at": "2026-10-02T22:00:00Z",   # Friday 15:00 PT
            "last_status": "deferred", "deferred_reason": "busy", "deferred_streak": 40}))

    def check(self, now: str, pending: str = '["auto-push-1","auto-push-2"]') -> dict:
        # A python script, not printf: MSYS printf mangles the JSON's embedded quotes on
        # Windows, so the check saw nothing pending and never went stale.
        pending_script = self.tmp / "pending-ids.py"
        pending_script.write_text(f"print({pending!r})\n")
        command = f"'{Path(sys.executable).as_posix()}' '{pending_script.as_posix()}'"
        env = {**self.env, "DIGEST_STALE_NOW": now, "DIGEST_STALE_PENDING_CMD": command}
        out = subprocess.run([sys.executable, str(STALE_CHECK)], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def calls_made(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_stale_is_measured_in_working_time_not_wall_clock(self) -> None:
        # Wrong impl: 24 wall-clock hours -> alarms Saturday afternoon for a
        # digest that last posted Friday 15:00 with nothing it could have done.
        self.assertFalse(self.check("2026-10-03T15:00:00-07:00")["stale"], "Saturday is not a working day")
        self.assertFalse(self.check("2026-10-05T09:00:00-07:00")["stale"], "Mon 09:00: 3 h Fri + 1 h Mon < one working day")
        self.assertEqual(self.calls_made(), [])
        out = self.check("2026-10-05T16:00:00-07:00")   # 3 h Fri + 8 h Mon = 11 h > 10 h working day
        self.assertTrue(out["stale"])
        self.assertTrue(out["sent"])
        calls = self.calls_made()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--task-id digest-stale-20261005", calls[0])
        self.assertIn("--status blocked --urgent --create-if-missing", calls[0])
        self.assertIn("2 reports waiting (auto-push-1, auto-push-2)", calls[0])
        self.assertIn("last posted 2026-10-02 15:00 PDT", calls[0])
        env_seen = (self.tmp / "stale-report-calls.log.env").read_text()
        self.assertIn("REPORT_TO_COS_NO_HOOKS=1", env_seen)
        self.assertNotIn("PM_OS_WORKER", env_seen, "worker env is stripped so the line is not fenced to a worker attempt")

    def test_one_line_per_local_day(self) -> None:
        # Wrong impl: every 10-minute tick re-sends while the digest stays stale.
        self.check("2026-10-05T16:00:00-07:00")
        self.check("2026-10-05T16:10:00-07:00")
        self.check("2026-10-05T23:50:00-07:00")
        self.assertEqual(len(self.calls_made()), 1)
        out = self.check("2026-10-06T08:05:00-07:00")
        self.assertTrue(out["sent"])
        self.assertEqual(len(self.calls_made()), 2)
        self.assertIn("digest-stale-20261006", self.calls_made()[1])

    def test_nothing_pending_or_recent_post_means_no_alarm(self) -> None:
        # Wrong impl: alarms on staleness alone, or counts raw undelivered rows
        # (held/urgent) instead of what the digest would carry.
        self.assertFalse(self.check("2026-10-05T16:00:00-07:00", pending="[]")["stale"])
        self.digest_state.write_text(json.dumps({"version": 1, "last_posted_at": "2026-10-05T15:00:00Z"}))
        self.assertFalse(self.check("2026-10-05T16:00:00-07:00")["stale"])
        self.assertEqual(self.calls_made(), [])


class QuietWorkstreams(Harness):
    """router.yaml dispatch.quiet_done_workstreams: a
    done report from a listed workstream is logged and
    settled but never reaches CoS, directly or through the digest Updates
    section; blocked and failed from the same workstream still go out."""

    QUIET = "launch-notes-replies"

    def setUp(self) -> None:
        super().setUp()
        cfg = self.tmp / "router.yaml"
        cfg.write_text(
            "dispatch:\n  quiet_done_workstreams:\n    - %s\n"
            "next-up:\n  working_hours_start: \"08:00\"\n  working_hours_end: \"18:00\"\n"
            "  timezone: America/Los_Angeles\n" % self.QUIET)
        self.env["REPORT_TO_COS_ROUTER_CONFIG"] = str(cfg)
        self.feed = self.state / "thread-status-feed.jsonl"

    def seed_quiet(self, task_id: str) -> None:
        self.ledger_cmd("start", "--task-id", task_id, "--origin", "cos", "--workstream", self.QUIET,
                        "--prompt", "feedback pass")

    def feed_rows(self) -> list[dict]:
        return [json.loads(l) for l in self.feed.read_text().splitlines() if l.strip()]

    def test_done_from_quiet_workstream_is_logged_settled_and_never_sent(self) -> None:
        # Wrong impls: (a) still pastes the line into idle CoS; (b) leaves the
        # row report_delivered=false so the next-up digest batches it anyway.
        self.seed_quiet("rt-quiet0000001")
        self.idle()   # after hours and idle: a normal done report would paste right here
        out = self.report("--task-id", "rt-quiet0000001", "--origin", "cos", "--status", "done",
                          summary="done: 5 drafts published to the console")
        self.assertTrue(out["quiet"])
        self.assertEqual(out["routed_to"], "log")
        self.assertEqual(out["delivery_outcome"], "quiet")
        self.assertFalse(out["delivered"])
        self.assertFalse(out["batched"], "a quiet report must not wait for the digest")
        self.assertEqual(self.sent(), [], "nothing is pasted into CoS")
        row = self.row("rt-quiet0000001")
        self.assertTrue(row["report_delivered"], "settled so the Updates section and reap skip it")
        self.assertEqual(row["status"], "done")
        logged = [r for r in self.feed_rows() if r["task_id"] == "rt-quiet0000001"]
        self.assertEqual(len(logged), 1)
        self.assertIn("5 drafts published", logged[0]["summary"], "the full text still lands in the report log")
        self.assertEqual(logged[0]["routed_to"], "log")

    def test_done_during_hours_is_not_batched_into_the_digest_either(self) -> None:
        # Wrong impl: quiet only short-circuits the direct branch, so an
        # in-hours done report still becomes a pending digest row.
        self.seed_quiet("rt-quiet0000002")
        out = self.report("--task-id", "rt-quiet0000002", "--origin", "cos", "--status", "done",
                          "--delivery", "digest", summary="done: drafts", )
        self.assertTrue(out["quiet"])
        self.assertFalse(out["batched"])
        self.assertTrue(self.row("rt-quiet0000002")["report_delivered"])
        self.assertEqual(self.sent(), [])

    def test_blocked_and_failed_from_quiet_workstream_still_reach_cos(self) -> None:
        # Wrong impl: silences the whole workstream, so a broken pass goes unseen.
        for task_id, status in (("rt-quiet0000003", "blocked"), ("rt-quiet0000004", "failed")):
            self.seed_quiet(task_id)
            self.idle()
            out = self.report("--task-id", task_id, "--origin", "cos", "--status", status,
                              summary="%s: Slack read rate-limited" % status)
            self.assertFalse(out["quiet"], status)
            self.assertTrue(out["delivered"], status)
        self.assertEqual(len(self.sent()), 2)

    def test_other_workstreams_are_unaffected(self) -> None:
        # Wrong impl: the list is ignored and everything goes quiet, or the
        # workstream flag on the command line is not honoured.
        self.seed("rt-quiet0000005")   # pm-os-maintenance
        self.idle()
        out = self.report("--task-id", "rt-quiet0000005", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertFalse(out["quiet"])
        self.assertTrue(out["delivered"])
        self.assertEqual(len(self.sent()), 1)
        # --workstream on the command line wins over the row's workstream.
        self.seed("rt-quiet0000006")
        out = self.report("--task-id", "rt-quiet0000006", "--origin", "cos", "--status", "done",
                          "--workstream", self.QUIET)
        self.assertTrue(out["quiet"])
        self.assertEqual(len(self.sent()), 1)


ACK_LINK = " ⏎ [Open link](https://ampcode.com/threads/T-00000000-0000-4000-8000-000000000000)"


class LocalityAcks(Harness):
    """A workspace report whose text is only the owner tab's locality
    acknowledgment to the start-of-day seed is logged and settled, never sent and never batched into the digest."""

    ACK = "• 🌐 Garden Revamp — Understood. Staying local on this machine, waiting for the workstream instructions." + ACK_LINK

    def feed_rows(self) -> list[dict]:
        feed = self.state / "thread-status-feed.jsonl"
        return [json.loads(l) for l in feed.read_text().splitlines() if l.strip()]

    def test_locality_ack_is_logged_settled_and_never_sent(self) -> None:
        # Wrong impls: (a) idle CoS after hours gets the ack as a standalone
        # line; (b) the row stays report_delivered=false so the digest Updates
        # section lists it; (c) the waiter is spawned to replay it later.
        self.idle()
        out = self.report("--task-id", "wr-ack0000000001", "--origin", "cos", "--status", "done",
                          "--workstream", "garden-revamp", "--create-if-missing", summary=self.ACK)
        self.assertTrue(out["quiet"], out)
        self.assertEqual(out["quiet_reason"], "locality-ack")
        self.assertEqual(out["routed_to"], "log")
        self.assertEqual(out["delivery_outcome"], "quiet")
        self.assertFalse(out["delivered"])
        self.assertFalse(out["batched"], "an ack must not wait for the digest either")
        self.assertFalse(out["waiter"])
        self.assertEqual(self.sent(), [], "nothing is pasted into CoS")
        row = self.row("wr-ack0000000001")
        self.assertTrue(row["report_delivered"], "settled so Updates and reap skip it")
        self.assertIn("locality acknowledgment", row.get("note") or "")
        logged = [r for r in self.feed_rows() if r["task_id"] == "wr-ack0000000001"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["routed_to"], "log")
        self.assertIn("Staying local on this machine", logged[0]["summary"], "the report log keeps the text")

    def test_ack_wording_variants_are_all_quiet(self) -> None:
        # Wrong impl: matches only one exact sentence, so the
        # next seed reply phrased differently drains into CoS again.
        variants = [
            "• 🪧 Recipe Search — Acknowledged. Staying local on this machine, no remote box, SSH, cloud execution, or remote subagents. Waiting for the workstream instructions.",
            "• 🏪 Launch Notes — Ready. Staying local on this machine; waiting for the workstream instructions.",
            "• 🎓 Garden Revamp — Acknowledged. Running locally on this machine in C:/Users/tester/Development/pm-os. Waiting for the workstream instructions.",
            "• 🛠️ PM OS Maintenance — Read the live pm-os registry entry and shared contract. I’ll stay on this machine, follow its approval and write-back rules, and wait for your workstream instructions.",
            "• 📜 recipe-search — Standing by on this machine. Waiting for the workstream instructions.",
        ]
        self.idle()
        for n, text in enumerate(variants):
            out = self.report("--task-id", "wr-ack00000001%02d" % n, "--origin", "cos", "--status", "done",
                              "--create-if-missing", summary=text + ACK_LINK)
            self.assertTrue(out["quiet"], text)
        self.assertEqual(self.sent(), [])

    def test_real_deliverable_from_the_same_tab_is_still_delivered(self) -> None:
        # Wrong impl: the predicate keys on the workstream or on "on this
        # machine" alone, so a real result mentioning locality goes quiet.
        self.idle()
        real = ("• 🌐 Garden Revamp — Ran the report refresh on this machine: 12 rows updated, "
                "note at workstreams/garden-revamp/2026-10-22-refresh.md" + ACK_LINK)
        out = self.report("--task-id", "wr-ack0000000002", "--origin", "cos", "--status", "done", "--delivery", "direct",
                          "--workstream", "garden-revamp", "--create-if-missing", summary=real)
        self.assertFalse(out["quiet"], out)
        self.assertTrue(out["delivered"])
        self.assertEqual(len(self.sent()), 1)
        # Anything carrying a link other than the thread is a deliverable too.
        linked = "• 🌐 Garden Revamp — Understood. Staying local on this machine, waiting for the workstream instructions. See https://docs.google.com/document/d/abc" + ACK_LINK
        out = self.report("--task-id", "wr-ack0000000003", "--origin", "cos", "--status", "done", "--delivery", "direct",
                          "--create-if-missing", summary=linked)
        self.assertFalse(out["quiet"])
        self.assertEqual(len(self.sent()), 2)

    def test_blocked_ack_still_reaches_cos(self) -> None:
        # Wrong impl: silences by text alone, so a tab that is blocked while
        # staying local goes unseen.
        self.idle()
        out = self.report("--task-id", "wr-ack0000000004", "--origin", "cos", "--status", "blocked",
                          "--create-if-missing", summary=self.ACK)
        self.assertFalse(out["quiet"])
        self.assertTrue(out["delivered"])
        self.assertEqual(len(self.sent()), 1)

    def test_ack_during_hours_is_not_batched_into_the_digest(self) -> None:
        # Wrong impl: quiet only short-circuits the direct branch, so an
        # in-hours ack still becomes a pending digest row.
        self.env["REPORT_TO_COS_NOW"] = "2026-10-05T10:30:00-07:00"   # Monday, in hours
        out = self.report("--task-id", "wr-ack0000000005", "--origin", "cos", "--status", "done",
                          "--create-if-missing", summary=self.ACK)
        self.assertTrue(out["quiet"])
        self.assertFalse(out["batched"])
        self.assertTrue(self.row("wr-ack0000000005")["report_delivered"])
        self.assertEqual(self.sent(), [])


class StaleBatched(Harness):
    """A non-priority CoS done report is batched for the next digest's
    Updates section at any hour: a fresh after-hours completion, an
    in-hours row just after close, and a stale in-hours row are never
    pasted as standalone lines.
    Blocked, failed, urgent, needs-ruling and --delivery direct still go
    direct.

    router.yaml sets
    terminal_report_delivery: direct, so this batching runs only under the
    `auto` setting that restores it. These tests pin `auto`; the direct default
    has its own test in this class."""

    IN_HOURS = "2026-10-05T10:00:00-07:00"      # Monday 10:00 PT
    AFTER_HOURS = "2026-10-05T19:30:00-07:00"   # same Monday, 19:30 PT

    def setUp(self) -> None:
        super().setUp()
        router = (REPO_ROOT / "system" / "config" / "router.yaml").read_text(encoding="utf-8")
        self.assertIn("terminal_report_delivery: direct", router)
        cfg = self.tmp / "router-auto.yaml"
        cfg.write_text(router.replace("terminal_report_delivery: direct", "terminal_report_delivery: auto"), encoding="utf-8")
        self.env["REPORT_TO_COS_ROUTER_CONFIG"] = str(cfg)

    def test_default_policy_sends_a_done_report_direct_after_hours(self) -> None:
        # Every terminal report goes direct, after hours included.
        self.env.pop("REPORT_TO_COS_ROUTER_CONFIG")
        self.seed("rt-stale00000000")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.idle()
        out = self.report("--task-id", "rt-stale00000000", "--origin", "cos", "--status", "done", summary="finished: direct")
        self.assertTrue(out["delivered"], out)
        self.assertFalse(out["batched"])
        self.assertEqual(len(self.sent()), 1)

    def set_finished_at(self, task_id: str, iso_utc: str) -> None:
        payload = json.loads(self.ledger.read_text())
        for task in payload["tasks"]:
            if task["id"] == task_id:
                task["finished_at"] = iso_utc
        self.ledger.write_text(json.dumps(payload))

    def batch_in_hours(self, task_id: str, finished_utc: str = "2026-10-05T17:00:00Z") -> None:
        """A done report during working hours while CoS is busy: batched for the digest."""
        self.seed(task_id)
        self.env["REPORT_TO_COS_NOW"] = self.IN_HOURS
        self.busy()
        first = self.report("--task-id", task_id, "--origin", "cos", "--status", "done", summary="finished: in hours")
        self.assertTrue(first["batched"], first)
        self.assertEqual(self.sent(), [])
        self.set_finished_at(task_id, finished_utc)   # 10:00 PT on that Monday

    def test_stale_in_hours_row_is_not_replayed_after_hours(self) -> None:
        # Wrong impl: the after-hours branch sends every pending CoS row
        # directly, so a batch the digest missed drains as standalone lines.
        self.batch_in_hours("rt-stale00000001")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.idle()   # idle CoS: a direct send would land right here
        out = self.report("--task-id", "rt-stale00000001", "--origin", "cos", "--status", "done", summary="finished: in hours")
        self.assertTrue(out["stale_batched"], out)
        self.assertTrue(out["batched"])
        self.assertFalse(out["delivered"])
        self.assertFalse(out["waiter"], "no waiter: the digest carries it")
        self.assertEqual(self.sent(), [], "stale rows wait for the digest Updates section")
        self.assertIs(self.row("rt-stale00000001")["report_delivered"], False, "still pending for the digest")

    def test_fresh_after_hours_completion_is_batched_for_the_digest(self) -> None:
        # Wrong impl: an after-hours branch pastes a fresh
        # evening result straight into idle CoS.
        self.seed("rt-stale00000002")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.set_finished_at("rt-stale00000002", "2026-10-06T02:20:00Z")   # 19:20 PT, ten minutes ago
        self.idle()   # idle CoS: a direct send would land right here
        out = self.report("--task-id", "rt-stale00000002", "--origin", "cos", "--status", "done", summary="finished: evening")
        self.assertFalse(out["stale_batched"])
        self.assertTrue(out["batched"], out)
        self.assertFalse(out["delivered"])
        self.assertFalse(out["waiter"], "no waiter: the digest carries it")
        self.assertEqual(self.sent(), [])
        self.assertIs(self.row("rt-stale00000002")["report_delivered"], False, "still pending for the digest")

    def test_in_hours_row_within_one_cadence_after_close_is_batched(self) -> None:
        # Wrong impl: a 17:50 PT result reported at 18:10 PT is pasted because
        # it is not yet stale.
        self.seed("rt-stale00000003")
        self.env["REPORT_TO_COS_NOW"] = "2026-10-05T18:10:00-07:00"
        self.set_finished_at("rt-stale00000003", "2026-10-06T00:50:00Z")   # 17:50 PT, twenty minutes ago
        self.idle()
        out = self.report("--task-id", "rt-stale00000003", "--origin", "cos", "--status", "done", summary="finished: late afternoon")
        self.assertFalse(out["stale_batched"])
        self.assertTrue(out["batched"])
        self.assertFalse(out["delivered"])
        self.assertEqual(self.sent(), [])

    def test_after_hours_direct_policy_is_kept_on_loop_retry(self) -> None:
        # Wrong impl: --delivery direct is not stored, so the loop's retry
        # pass (which calls back with --delivery auto) batches a row the
        # worker asked to have pasted.
        self.seed("rt-stale00000007")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.busy()
        first = self.report("--task-id", "rt-stale00000007", "--origin", "cos", "--status", "done",
                            "--delivery", "direct", summary="finished: paste me")
        self.assertEqual(first["delivery_outcome"], "deferred")
        self.assertEqual(self.row("rt-stale00000007")["delivery"], "direct")
        self.idle()
        again = self.report("--task-id", "rt-stale00000007", "--origin", "cos", "--status", "done", summary="finished: paste me")
        self.assertTrue(again["delivered"], again)
        self.assertEqual(len(self.sent()), 1)

    def test_blocked_stale_row_is_still_sent(self) -> None:
        # Wrong impl: staleness outranks priority, so a blocked worker waits overnight.
        self.seed("rt-stale00000004")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.busy()
        self.report("--task-id", "rt-stale00000004", "--origin", "cos", "--status", "blocked", summary="blocked: needs a ruling")
        self.set_finished_at("rt-stale00000004", "2026-10-05T17:00:00Z")
        self.idle()
        out = self.report("--task-id", "rt-stale00000004", "--origin", "cos", "--status", "blocked", summary="blocked: needs a ruling")
        self.assertTrue(out["delivered"])
        self.assertEqual(len(self.sent()), 1)

    def test_loop_retry_pass_keeps_stale_rows_batched_after_hours(self) -> None:
        # Wrong impl: command-center-loop's retry pass forces --delivery direct
        # for CoS rows, bypassing the staleness rule.
        self.batch_in_hours("rt-stale00000005")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.idle()
        loop_env = {**self.env, "STATE_DIR": str(self.state)}
        out = subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env=loop_env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("still batched for the next digest", out.stdout + out.stderr)
        self.assertEqual(self.sent(), [])
        self.assertIs(self.row("rt-stale00000005")["report_delivered"], False)

    def test_batched_after_hours_done_spawns_no_waiter(self) -> None:
        # Wrong impl: a busy CoS after hours still spawns a waiter for a plain
        # done report, which pastes the line as a standalone message the
        # moment CoS idles instead of leaving it to the digest.
        self.env.pop("REPORT_TO_COS_NO_WAITER")
        pidfile = self.state / "worker-dispatch" / "report-waiter-rt-stale00000006.pid"
        self.seed("rt-stale00000006")
        self.env["REPORT_TO_COS_NOW"] = self.AFTER_HOURS
        self.busy()
        first = self.report("--task-id", "rt-stale00000006", "--origin", "cos", "--status", "done", summary="finished: deferred")
        self.assertTrue(first["batched"], first)
        self.assertFalse(first["waiter"])
        self.assertFalse(pidfile.exists(), "no waiter pidfile for a batched row")
        self.idle()
        time.sleep(1.5)
        self.assertEqual(self.sent(), [], "nothing pastes once CoS idles")
        self.assertIs(self.row("rt-stale00000006")["report_delivered"], False)


# Same stub, but the tab list comes from a file: the owner reset wmux and CoS is not open.
STUB_CMUX_TABS = STUB_CMUX.replace(
    "printf '* workspace:252  🧭 CoS  [selected]\\n  workspace:254  🚀 Launchpad\\n'",
    'cat "$STUB_TABS_FILE"')


class ClosedCos(Harness):
    """After a wmux reset CoS may not be open, so a report has nowhere to land yet.
    Wrong impls: mark the report delivered with nowhere
    to send it, tell the owner to clear a draft in a CoS that is not running, lose the report, or send
    it twice once CoS returns."""

    def setUp(self) -> None:
        super().setUp()
        self.assertIn("STUB_TABS_FILE", STUB_CMUX_TABS)
        self.cmux.write_text(STUB_CMUX_TABS)
        self.tabs = self.tmp / "tabs.txt"
        self.close_cos()
        self.env["STUB_TABS_FILE"] = str(self.tabs)
        self.days_later(2)   # past the alert window and the 24-hour stale settle

    def days_later(self, days: int) -> None:
        self.env["REPORT_TO_COS_NOW"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    def close_cos(self) -> None:
        self.tabs.write_bytes("* workspace:300  Workspace 1  [selected]\n".encode())   # bytes: no CRLF on Windows

    def open_cos(self) -> None:
        self.tabs.write_bytes("* workspace:252  🧭 CoS  [selected]\n  workspace:300  Workspace 1\n".encode())

    def retry_tick(self) -> None:
        subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env={**self.env, "STATE_DIR": str(self.state)},
                       capture_output=True, text=True)

    def test_report_to_a_closed_cos_waits_and_lands_once_when_it_reopens(self) -> None:
        self.seed("rt-00000000005b")
        first = self.report("--task-id", "rt-00000000005b", "--origin", "cos", "--status", "done", "--delivery", "direct",
                            summary="finished: while closed")
        self.assertFalse(first["delivered"])
        self.assertIsNone(first["destination_workspace_ref"])
        self.assertEqual(self.sent(), [], "nothing typed into whatever tab is open")
        row = self.row("rt-00000000005b")
        self.assertIs(row["report_delivered"], False)
        self.assertFalse(row.get("report_delivery_unknown", False), "still retryable")

        self.retry_tick()
        self.assertEqual(self.sent(), [], "a tick with CoS still closed sends nothing")
        self.assertIs(self.row("rt-00000000005b")["report_delivered"], False)

        self.open_cos()
        self.retry_tick()
        self.assertEqual(len(self.sent()), 1, "the next tick after CoS reopens delivers it")
        self.assertIn("finished: while closed", self.sent()[0])
        self.assertIs(self.row("rt-00000000005b")["report_delivered"], True)
        self.retry_tick()
        self.assertEqual(len(self.sent()), 1, "never twice")

    def test_a_report_that_waited_too_long_settles_without_a_send(self) -> None:
        self.seed("rt-00000000005c")
        self.report("--task-id", "rt-00000000005c", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.days_later(8)
        self.open_cos()
        self.retry_tick()
        self.assertEqual(self.sent(), [], "a week-old line is not replayed")
        self.assertIs(self.row("rt-00000000005c")["report_delivered"], True)

    def test_a_cos_tab_at_a_bare_shell_gets_nothing_typed_into_it(self) -> None:
        # A wmux restart restores tabs with no agent in them.
        self.open_cos()
        self.screen.write_text("PS C:\\Users\\tester> ")
        self.seed("rt-00000000005d")
        first = self.report("--task-id", "rt-00000000005d", "--origin", "cos", "--status", "done", "--delivery", "direct")
        self.assertFalse(first["delivered"])
        self.assertEqual(self.sent(), [])
        self.assertIs(self.row("rt-00000000005d")["report_delivered"], False)

    def test_the_alert_never_asks_the_owner_to_clear_a_box_in_a_closed_cos(self) -> None:
        self.seed("rt-00000000005e")
        self.report("--task-id", "rt-00000000005e", "--origin", "cos", "--status", "blocked", summary="blocked: x")
        note = self.row("rt-00000000005e")["report_alert_note"]
        self.assertIn("CoS is not open", note)
        self.assertIn("lands when CoS reopens", note)
        for wrong in ("box", "draft", "busy", "empty"):
            self.assertNotIn(wrong, note.lower())

    def test_an_open_busy_cos_is_described_as_busy_with_no_box_to_clear(self) -> None:
        self.open_cos()
        self.busy()
        self.seed("rt-00000000005f")
        self.report("--task-id", "rt-00000000005f", "--origin", "cos", "--status", "blocked", summary="blocked: y")
        note = self.row("rt-00000000005f")["report_alert_note"]
        self.assertIn("waiting on CoS", note)
        self.assertNotIn("box", note)


if __name__ == "__main__":
    unittest.main()
