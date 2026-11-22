#!/usr/bin/env python3
"""The stale next-up alarm names a closed CoS as the cause and carries the Options shape.

When CoS is closed, the next-up digest defers "no-target". The alarm must not read like a
delivery fault ("check the waiter log"), and it carries its own Options line.

Run: python3 system/scripts/test_digest_stale_alarm.py
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("check_digest_staleness", HERE / "check-digest-staleness.py")
stale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stale)  # type: ignore[union-attr]

ZONE = ZoneInfo("America/Los_Angeles")
POSTED = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)


class AlarmLine(unittest.TestCase):
    def line(self, reason: str) -> str:
        state = {"last_status": "deferred", "deferred_reason": reason, "deferred_streak": 19}
        return stale.alarm_line(state, ["rt-000000000045"], POSTED, 2.4, ZONE)

    def test_no_target_names_the_closed_cos(self) -> None:
        text = self.line("no-target")
        self.assertIn("no CoS tab was open", text)
        self.assertIn("open CoS", text)
        self.assertNotIn("next-up-waiter.log", text)

    def test_other_reasons_keep_the_waiter_advice(self) -> None:
        text = self.line("busy")
        self.assertIn("next-up-waiter.log", text)
        self.assertNotIn("no CoS tab was open", text)

    def test_every_alarm_carries_options_and_lean(self) -> None:
        for reason in ("no-target", "busy", "draft", "unreadable"):
            with self.subTest(reason=reason):
                text = self.line(reason)
                self.assertRegex(text, r"Options: \(a\) .+; \(b\) .+ Lean: [ab] because ")


if __name__ == "__main__":
    unittest.main()
