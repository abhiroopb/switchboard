#!/usr/bin/env python3
"""ask_cos.py: a worker's mid-task question reaches CoS as one [WORKER QUESTION] line,
is recorded on the ledger row, never blocks the worker, and is answered by a steer.
The owner never has to look at a worker tab. Wrong impls:
wait for the answer; send a multi-line message (the wmux transport submits early);
drop the question when CoS is busy; ask without naming the default or the steer."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
LEDGER = Path(_TMP.name) / "running-tasks.json"
# setdefault: never clobber the env another suite set before it imported ask_cos (setUp repoints the ledger).
os.environ.setdefault("RUNNING_TASKS_FILE", str(LEDGER))
os.environ.setdefault("OWNER_TABS_ROOT", _TMP.name)

import ask_cos  # noqa: E402


def write_row(**extra) -> None:
    LEDGER.write_text(json.dumps({"version": 2, "tasks": [
        {"id": "rt-q1", "status": "active", "workstream": "pm-os-maintenance", "origin": "cos", **extra}]}), encoding="utf-8")


def row() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))["tasks"][0]


def ask(*argv: str) -> dict:
    out = io.StringIO()
    with redirect_stdout(out):
        code = ask_cos.main(list(argv))
    assert code == 0
    return json.loads(out.getvalue().strip().splitlines()[-1])


class AskCosTests(unittest.TestCase):
    def setUp(self) -> None:
        # Hermetic when run in one process with other suites that import ask_cos first: ask_cos.ledger() reloads
        # the ledger module on every call and reads RUNNING_TASKS_FILE then, so point it at this file's temp row;
        # keep the question's to-do capture (a detached process) off the real list.
        patcher = patch.dict(os.environ, {"RUNNING_TASKS_FILE": str(LEDGER), "OWNER_ACTION_CAPTURE_DISABLED": "1",
                                          "PM_OS_TEST_SANDBOX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        dry = patch.dict(os.environ)   # another suite sets ASK_COS_DRY_RUN=1 at import; these tests patch send_once instead
        dry.start()
        self.addCleanup(dry.stop)
        os.environ.pop("ASK_COS_DRY_RUN", None)
        write_row()

    def test_question_is_recorded_and_sent_as_one_line_with_default_and_steer(self) -> None:
        sent: list[str] = []
        with patch.object(ask_cos, "send_once", lambda line: sent.append(line) or {"ok": True, "ref": "workspace:1"}):
            result = ask("--task-id", "rt-q1", "--option", "Keep the name", "--option", "Rename it",
                         "--default", "a", "Rename the\nworkstream?")
        self.assertTrue(result["asked"] and result["delivered"])
        self.assertEqual(len(sent), 1)
        line = sent[0]
        self.assertTrue(line.startswith("[WORKER QUESTION] "))
        self.assertNotIn("\n", line)
        self.assertIn("(a) Keep the name; (b) Rename it", line)
        self.assertIn("Default: (a) Keep the name; the worker is continuing on it.", line)
        self.assertIn("router-dispatch.sh --origin cos --steer rt-q1", line)
        q = row()["worker_questions"][0]
        self.assertEqual((q["id"], q["delivered"], q["answered"], q["cos_ref"]), ("q1", True, False, "workspace:1"))

    def test_a_busy_cos_queues_a_background_retry_and_the_worker_is_not_held(self) -> None:
        spawned: list[tuple[str, str]] = []
        with patch.object(ask_cos, "send_once", lambda line: {"ok": False, "reason": "the recipient's composer holds a draft"}), \
                patch.object(ask_cos, "spawn_retry", lambda tid, qid: spawned.append((tid, qid))):
            result = ask("--task-id", "rt-q1", "Which option?")
        self.assertEqual((result["asked"], result["delivered"]), (True, False))
        self.assertEqual(spawned, [("rt-q1", "q1")])
        self.assertIn("draft", row()["worker_questions"][0]["last_refusal"])

    def test_retry_delivers_once_cos_frees_up_and_stops_after_a_report(self) -> None:
        with patch.object(ask_cos, "send_once", lambda line: {"ok": False, "reason": "busy"}), \
                patch.object(ask_cos, "spawn_retry", lambda tid, qid: None):
            ask("--task-id", "rt-q1", "Which option?")
        replies = iter([{"ok": False, "reason": "busy"}, {"ok": True, "ref": "workspace:1"}])
        with patch.object(ask_cos, "send_once", lambda line: next(replies)), patch.object(ask_cos.time, "sleep", lambda s: None), \
                redirect_stdout(io.StringIO()):
            ask_cos.main(["retry", "--task-id", "rt-q1", "--qid", "q1"])
        self.assertTrue(row()["worker_questions"][0]["delivered"])

    def test_answered_marks_the_newest_open_question(self) -> None:
        with patch.object(ask_cos, "send_once", lambda line: {"ok": True, "ref": "workspace:1"}):
            ask("--task-id", "rt-q1", "First?")
            ask("--task-id", "rt-q1", "Second?")
        with redirect_stdout(io.StringIO()):
            ask_cos.main(["answered", "--task-id", "rt-q1", "--answer", "go with b"])
        qs = row()["worker_questions"]
        self.assertEqual([q["answered"] for q in qs], [False, True])
        self.assertEqual(qs[1]["answer"], "go with b")

    def test_unknown_task_is_reported_not_raised(self) -> None:
        self.assertEqual(ask("--task-id", "rt-nope", "Anything?")["asked"], False)


if __name__ == "__main__":
    unittest.main()
