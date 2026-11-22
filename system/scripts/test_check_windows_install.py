#!/usr/bin/env python3
"""The doctor's real MCP connection test and its wake-aware staleness check."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-windows-install.py")
SPEC = importlib.util.spec_from_file_location("check_windows_install_under_test", SCRIPT)
assert SPEC and SPEC.loader
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)

ANSWERS = """
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}}), flush=True)
        elif msg.get("method") == "tools/list":
            print("not json noise", flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": %s}}), flush=True)
"""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        doctor.results.clear()

    def server(self, body: str) -> dict:
        path = self.tmp / "server.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return {"command": sys.executable, "args": [str(path)]}


class HandshakeTests(Base):
    def test_a_server_that_lists_tools_is_connected(self) -> None:
        ok, detail = doctor.mcp_handshake(self.server(ANSWERS % '[{"name": "click"}]'), 30)
        self.assertTrue(ok, detail)
        self.assertIn("1 tools", detail)

    def test_a_silent_server_fails_at_the_limit(self) -> None:
        started = time.monotonic()
        ok, detail = doctor.mcp_handshake(self.server("import time\ntime.sleep(60)\n"), 1)
        self.assertFalse(ok)
        self.assertIn("no answer within 1 s", detail)
        self.assertLess(time.monotonic() - started, 30, "the server must be ended, not waited for")

    def test_a_server_that_exits_fails(self) -> None:
        ok, detail = doctor.mcp_handshake(self.server("raise SystemExit(1)\n"), 30)
        self.assertFalse(ok)
        self.assertIn("after", detail)

    def test_a_server_with_no_tools_fails(self) -> None:
        ok, detail = doctor.mcp_handshake(self.server(ANSWERS % "[]"), 30)
        self.assertFalse(ok)
        self.assertIn("no tools", detail)

    def test_a_missing_command_fails_without_raising(self) -> None:
        ok, detail = doctor.mcp_handshake({"command": str(self.tmp / "missing.exe")}, 5)
        self.assertFalse(ok)
        self.assertIn("could not start", detail)


class TimeoutSettingTests(Base):
    def limit(self, settings: dict | None, env: str | None = None) -> int:
        (self.tmp / ".claude").mkdir(exist_ok=True)
        if settings is not None:
            (self.tmp / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        clean = {k: v for k, v in os.environ.items() if k != "MCP_TIMEOUT"}
        if env:
            clean["MCP_TIMEOUT"] = env
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(doctor.Path, "home", return_value=self.tmp):
            return doctor.mcp_timeout_ms()

    def test_defaults_to_claudes_30_seconds(self) -> None:
        self.assertEqual(self.limit(None), 30000)

    def test_reads_the_user_settings(self) -> None:
        self.assertEqual(self.limit({"env": {"MCP_TIMEOUT": "60000"}}), 60000)

    def test_the_process_env_wins(self) -> None:
        self.assertEqual(self.limit({"env": {"MCP_TIMEOUT": "60000"}}, env="90000"), 90000)

    def test_a_bad_value_falls_back(self) -> None:
        self.assertEqual(self.limit({"env": {"MCP_TIMEOUT": "soon"}}), 30000)


class StalenessTests(Base):
    def level(self, age_hours: float, woke: float | None) -> tuple[str, str]:
        relative = Path("system/state/knowledge/snapshot.json")
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        with mock.patch.object(doctor, "ROOT", self.tmp), mock.patch.object(doctor, "last_wake_epoch", return_value=woke) as wake:
            doctor.check_knowledge()
        row = next(r for r in doctor.results if r["check"] == "knowledge layer")
        self.wake_calls = wake.call_count
        return row["level"], row["detail"]

    def test_fresh_needs_no_wake_lookup(self) -> None:
        level, _ = self.level(0.5, None)
        self.assertEqual(level, "ok")
        self.assertEqual(self.wake_calls, 0, "a fresh file spawns no powershell")

    def test_old_right_after_a_wake_is_not_a_warning(self) -> None:
        level, detail = self.level(3.5, time.time() - 60)
        self.assertEqual(level, "ok")
        self.assertIn("woke", detail)

    def test_old_with_the_machine_awake_warns(self) -> None:
        level, detail = self.level(3.5, None)
        self.assertEqual(level, "warn")
        self.assertIn("2 h limit", detail)


if __name__ == "__main__":
    unittest.main()
