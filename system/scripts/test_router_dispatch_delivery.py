#!/usr/bin/env python3
"""router-dispatch.sh completion policy: every owner ask goes direct.

A start-of-day ask stored with delivery=digest would leave its report waiting for the next
digest instead of reaching CoS on its own. AGENTS.md: every terminal worker report goes to its origin directly; digest is for
automated fan-out, which passes --delivery digest itself.

Hermetic: a fake REPO_ROOT whose dispatch.py records the argv it got and prints one JSON
line. Each test names the wrong implementation it catches.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ["PM_OS_TEST_SANDBOX"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER = REPO_ROOT / "system" / "scripts" / "router-dispatch.sh"

DISPATCH_STUB = """import json, os, sys
open(os.environ["STUB_DISPATCH_ARGS"], "a", encoding="utf-8").write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"status": "dispatched", "task_id": "rt-000000000009", "message": "stub"}))
"""
REGISTRY_STUB = """import sys
print(sys.argv[2] if len(sys.argv) > 2 else "cos")
"""


class RouterDeliveryPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        scripts = root / "system" / "scripts"
        scripts.mkdir(parents=True)
        real_helpers = (REPO_ROOT / "system" / "cmux-helpers.sh").as_posix()
        (root / "system" / "cmux-helpers.sh").write_text(f'source "{real_helpers}"\n', encoding="utf-8", newline="\n")
        (scripts / "dispatch.py").write_text(DISPATCH_STUB, encoding="utf-8", newline="\n")
        (scripts / "surface-registry.py").write_text(REGISTRY_STUB, encoding="utf-8", newline="\n")
        (scripts / "no_window.py").write_text("", encoding="utf-8")
        (scripts / "publish-running-tasks-status.sh").write_text("exit 0\n", encoding="utf-8", newline="\n")
        self.args_log = root / "dispatch-args.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PM_OS_WORKER", "PM_OS_DISPATCH", "WMUX", "CMUX_WORKSPACE", "CMUX_SURFACE"))}
        self.env.update({"REPO_ROOT": str(root), "STUB_DISPATCH_ARGS": str(self.args_log), "PM_OS_DISPATCH_SYNC": "1",
                         "PM_OS_TEST_SANDBOX": "1", "ROUTER_LOCK_DIR": str(root / "lock")})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def dispatched_delivery(self, *args: str) -> str | None:
        out = subprocess.run(["bash", str(ROUTER), "--origin", "cos", *args], env=self.env,
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        argv = json.loads(self.args_log.read_text(encoding="utf-8").splitlines()[-1])
        return argv[argv.index("--delivery") + 1] if "--delivery" in argv else None

    def test_owner_typed_start_of_day_goes_direct(self) -> None:
        # Wrong impl: the old regex stored digest for a bare "start of day" (and its aliases),
        # so the owner's start-of-day result reached CoS only with the next digest.
        for prompt in ("start of day", "Start of day.", "start my day", "good morning", "sod", "start-of-day"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.dispatched_delivery(prompt), "direct")

    def test_other_owner_asks_go_direct(self) -> None:
        # Wrong impl: direct only for start of day, nothing (auto) for the rest.
        self.assertEqual(self.dispatched_delivery("Workstream: pm-os-maintenance\nfix the waiter"), "direct")

    def test_automation_keeps_an_explicit_digest(self) -> None:
        # Wrong impl: the owner-ask default overwrites a caller's explicit digest.
        self.assertEqual(self.dispatched_delivery("--delivery", "digest", "start of day"), "digest")

    def test_retry_keeps_its_stored_policy(self) -> None:
        # Wrong impl: a --task-id retry is forced to direct and loses the row's own policy
        # (dispatch.py inherited_delivery keeps it when no --delivery is passed).
        self.assertIsNone(self.dispatched_delivery("--task-id", "rt-000000000068", "start of day"))


if __name__ == "__main__":
    unittest.main()
