#!/usr/bin/env python3
"""`dispatch.py lock` must answer with JSON even while Dropbox holds its queue file (WinError 32)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

import dispatch  # noqa: E402


class UnlinkRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_lock(self) -> dict:
        args = argparse.Namespace(task_id="rt-test", path=str(self.dir / "target"), wait=0, poll=0)
        out = io.StringIO()
        with patch.object(dispatch, "LOCKS", self.dir), \
             patch.object(dispatch, "current_leases", lambda: []), \
             patch.object(dispatch.ledger, "load_payload", lambda: {"tasks": []}), \
             patch.object(dispatch.time, "sleep", lambda _s: None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(dispatch.command_lock(args), 0)
        return json.loads(out.getvalue())

    def test_retry_succeeds_once_the_holder_lets_go(self) -> None:
        target = self.dir / "held"
        target.write_text("x")
        real, calls = Path.unlink, []

        def flaky(path, missing_ok=False):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(32, "in use")
            return real(path, missing_ok=missing_ok)

        with patch.object(dispatch.os, "name", "nt"), patch.object(dispatch.time, "sleep", lambda _s: None), \
             patch.object(Path, "unlink", flaky):
            self.assertTrue(dispatch._unlink_retry(target))
        self.assertEqual(len(calls), 3)
        self.assertFalse(target.exists())

    def test_retry_gives_up_quietly_on_windows(self) -> None:
        def held(_path, missing_ok=False):
            raise PermissionError(32, "in use")

        with patch.object(dispatch.os, "name", "nt"), patch.object(dispatch.time, "sleep", lambda _s: None), \
             patch.object(Path, "unlink", held):
            self.assertFalse(dispatch._unlink_retry(self.dir / "held"))

    def test_lock_still_answers_when_the_queue_file_stays_held(self) -> None:
        def held(_path, missing_ok=False):
            raise PermissionError(32, "in use")

        with patch.object(dispatch.os, "name", "nt"), patch.object(Path, "unlink", held):
            answer = self.run_lock()
        self.assertTrue(answer["allowed"])
        queues = list(self.dir.glob("*.queue.jsonl"))
        self.assertEqual([q.read_text() for q in queues], [""])

    def test_lock_still_answers_when_the_queue_file_refuses_writes_too(self) -> None:
        def held(_path, missing_ok=False):
            raise PermissionError(32, "in use")

        real_write = Path.write_text

        def refuse_queue(path, *args, **kwargs):
            if path.name.endswith(".queue.jsonl"):
                raise PermissionError(32, "in use")
            return real_write(path, *args, **kwargs)

        with patch.object(dispatch.os, "name", "nt"), patch.object(Path, "unlink", held), \
             patch.object(Path, "write_text", refuse_queue):
            answer = self.run_lock()
        self.assertTrue(answer["allowed"])
        self.assertEqual(len(list(self.dir.glob("*.lease.json"))), 1)

    def test_release_does_not_report_a_lease_it_could_not_remove(self) -> None:
        lease = self.dir / "abc.lease.json"
        lease.write_text(json.dumps({"task_id": "rt-test", "path": "p"}))

        def held(_path, missing_ok=False):
            raise PermissionError(32, "in use")

        with patch.object(dispatch, "LOCKS", self.dir), patch.object(dispatch.os, "name", "nt"), \
             patch.object(dispatch.time, "sleep", lambda _s: None), patch.object(Path, "unlink", held):
            self.assertEqual(dispatch.release_locks("rt-test"), [])
        with patch.object(dispatch, "LOCKS", self.dir):
            self.assertEqual(dispatch.release_locks("rt-test"), ["p"])

    def test_lock_removes_the_queue_file_normally(self) -> None:
        self.assertTrue(self.run_lock()["allowed"])
        self.assertEqual(list(self.dir.glob("*.queue.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
