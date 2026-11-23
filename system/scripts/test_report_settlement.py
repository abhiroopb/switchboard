#!/usr/bin/env python3
"""A report that was continued or marked handled stops retrying (2026-11-08, rt-00000000003c).

Five blocked rows kept report_delivered false. Each tick retried them, each retry appended an
options_missing event (86 in events.jsonl), and when CoS reopened it took the
four stale lines. Each test names the wrong implementation it catches.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from test_report_delivery import LOOP, Harness


class ReportSettlement(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.env["STATE_DIR"] = str(self.state)

    def rows(self, *rows: dict) -> None:
        self.ledger.write_text(json.dumps({"tasks": list(rows)}))

    def blocked(self, task_id: str, **extra: object) -> dict:
        return {"id": task_id, "status": "blocked", "origin": "cos", "workstream": "switchboard-maintenance",
                "summary": f"blocked: worker of {task_id} is gone", "report_delivered": False,
                "attempt_id": f"{task_id}.a1.abc", "created_at": "2026-11-08T22:00:00Z",
                "finished_at": "2026-11-08T23:00:00Z", **extra}

    def tick(self) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(LOOP), "--retry-reports-only"], env=self.env, capture_output=True, text=True)

    def options_events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in events if e.get("type") == "options_missing"]

    def mark(self, task_id: str, action: str = "complete", at: str = "2026-11-09T01:00:00Z") -> None:
        (self.state / "cos-review-actions.json").write_text(json.dumps({"items": {
            f"task:{task_id}": {"action": action, "at": at, "by": "dispatch:cos", "reason": "handled in chat"}}}))

    def assert_settled_quietly(self, task_id: str, why: str) -> None:
        self.assertEqual(self.sent(), [], "a moot report is never typed into CoS")
        row = self.row(task_id)
        self.assertIs(row["report_delivered"], True, "settled, so no later tick retries it")
        self.assertIn(why, row.get("note", ""))
        self.assertEqual(self.options_events(), [], "a moot report never re-runs the options check")

    def test_tick_settles_a_row_a_continuation_took_over(self) -> None:
        # Wrong impl: keep retrying until the CoS tab is idle, then type the stale line.
        self.rows(self.blocked("rt-moot00000001"),
                  {"id": "rt-heir00000001", "status": "active", "origin": "cos", "continuation_of": "rt-moot00000001",
                   "created_at": "2026-11-09T00:00:00Z"})
        self.tick()
        self.assert_settled_quietly("rt-moot00000001", "rt-heir00000001")

    def test_tick_follows_the_continuation_chain(self) -> None:
        # A worker that dies is continued again: rt-000000000062, rt-000000000063, rt-000000000064.
        self.rows(self.blocked("rt-moot00000002"),
                  self.blocked("rt-midl00000002", continuation_of="rt-moot00000002", report_delivered=True),
                  {"id": "rt-heir00000002", "status": "active", "origin": "cos", "continuation_of": "rt-midl00000002",
                   "created_at": "2026-11-09T00:00:00Z"})
        self.tick()
        self.assert_settled_quietly("rt-moot00000002", "rt-heir00000002")

    def test_tick_settles_a_row_marked_handled(self) -> None:
        # Wrong impl: the CoS Review card moves to Past while the loop still retries the report.
        self.rows(self.blocked("rt-moot00000003"))
        self.mark("rt-moot00000003")
        self.tick()
        self.assert_settled_quietly("rt-moot00000003", "marked handled")

    def test_a_replay_from_a_waiter_settles_too(self) -> None:
        # Wrong impl: only the loop checks, so a detached waiter types the line when CoS reopens.
        self.rows(self.blocked("rt-moot00000004"))
        self.mark("rt-moot00000004", action="skip")
        out = self.report("--task-id", "rt-moot00000004", "--origin", "cos", "--status", "blocked",
                          "--attempt-id", "rt-moot00000004.a1.abc", summary="blocked: worker of rt-moot00000004 is gone")
        self.assertTrue(out["delivered"] and out["ignored"], "the waiter exits on this answer")
        self.assert_settled_quietly("rt-moot00000004", "marked handled")

    def test_a_blocked_row_with_no_owner_still_delivers(self) -> None:
        # Wrong impl: settle every blocked row, so a real blocker never reaches the owner. A
        # failed continuation owns nothing, and a supplemental worker only adds to its parent.
        self.rows(self.blocked("rt-real00000001"),
                  {"id": "rt-000000000065", "status": "failed", "origin": "cos", "continuation_of": "rt-real00000001",
                   "report_delivered": True, "created_at": "2026-11-09T00:00:00Z"},
                  {"id": "rt-supp00000001", "status": "active", "origin": "cos", "continuation_of": "rt-real00000001",
                   "supplemental": True, "created_at": "2026-11-09T00:00:00Z"})
        self.tick()
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("blocked: worker of rt-real00000001", self.sent()[0])

    def test_a_done_report_always_goes_through(self) -> None:
        # Wrong impl: drop a result because a follow-up continued it or its card was closed.
        self.rows(self.blocked("rt-done00000001", status="done", summary="finished: the result", delivery="direct"),
                  {"id": "rt-next00000001", "status": "active", "origin": "cos", "continuation_of": "rt-done00000001",
                   "created_at": "2026-11-09T00:00:00Z"})
        self.mark("rt-done00000001")
        self.tick()
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("finished: the result", self.sent()[0])

    def test_a_mark_or_continuation_older_than_the_attempt_does_not_count(self) -> None:
        # Wrong impl: a row re-dispatched on purpose loses its new report to an old mark.
        self.rows(self.blocked("rt-agin00000001", attempt_began_at="2026-11-09T02:00:00Z"),
                  {"id": "rt-oldh00000001", "status": "done", "origin": "cos", "continuation_of": "rt-agin00000001",
                   "created_at": "2026-11-09T00:00:00Z"})
        self.mark("rt-agin00000001", at="2026-11-09T01:00:00Z")
        self.tick()
        self.assertEqual(len(self.sent()), 1)

    def test_a_retry_never_reruns_the_options_check(self) -> None:
        # Wrong impl: every pass over a pending blocked row appends another event (86 of them).
        self.seed("rt-pend00000001")
        self.busy()
        first = self.report("--task-id", "rt-pend00000001", "--origin", "cos", "--status", "blocked",
                            summary="blocked: no way forward listed")
        self.assertFalse(first["delivered"])
        self.assertEqual(len(self.options_events()), 1, "the first report is checked once")
        summary = self.row("rt-pend00000001")["summary"]
        self.assertIn("Options missing", summary, "the warning stays on the row")
        self.tick()
        self.tick()
        self.assertEqual(len(self.options_events()), 1, "retries add nothing")
        self.idle()
        self.tick()
        self.assertEqual(len(self.sent()), 1, "and the pending report still lands")
        self.assertIn("Options missing", self.sent()[0], "with the warning it was stored with")


if __name__ == "__main__":
    unittest.main()
