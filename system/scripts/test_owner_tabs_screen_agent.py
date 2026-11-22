#!/usr/bin/env python3
"""owner_tabs.screen_agent (2026-11-12, rt-00000000003b).

The owner's CoS Claude Code, started by hand in a wmux pane, never bound to its tab,
so owner_tabs named no agent for CoS and every report took the idle-only path. A tab
with no bound agent now counts as Claude when the shim sees a Claude composer on it.
A stub cmux answers `agent state`; nothing reaches a live tab.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("owner_tabs", HERE / "owner_tabs.py")
owner_tabs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner_tabs)  # type: ignore[union-attr]


class ScreenAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="screen-agent-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "cmux.py"
        self.stub.write_text("import os, sys\nprint(os.environ.get('STUB_STATE', '{}'))\n", encoding="utf-8")
        patcher = mock.patch.object(owner_tabs, "_cmux_argv", lambda: [sys.executable, str(self.stub)])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.environ.pop, "STUB_STATE", None)
        self.addCleanup(os.environ.pop, "OWNER_TABS_SKIP_SCREEN", None)

    def state(self, composer: str) -> None:
        os.environ["STUB_STATE"] = '{"workspace_ref": "workspace:3", "agent": null, "composer": "%s"}' % composer

    def test_a_claude_composer_makes_an_unbound_tab_claude(self) -> None:
        for composer in ("claude-empty", "claude-text"):
            self.state(composer)
            self.assertEqual(owner_tabs.screen_agent("workspace:3"), "claude", composer)
            got = owner_tabs.with_screen_agent("workspace:3", {"agent": "", "thread_id": "", "state": ""})
            self.assertEqual((got["agent"], got["agent_source"]), ("claude", "screen"))

    def test_no_composer_stays_unknown(self) -> None:
        self.state("none")
        self.assertEqual(owner_tabs.screen_agent("workspace:3"), "")
        os.environ["STUB_STATE"] = "not json"
        self.assertEqual(owner_tabs.screen_agent("workspace:3"), "")
        self.assertEqual(owner_tabs.screen_agent(""), "")

    def test_a_bound_agent_is_never_second_guessed(self) -> None:
        self.state("none")
        bound = {"agent": "amp", "thread_id": "T-1", "state": "Idle"}
        self.assertIs(owner_tabs.with_screen_agent("workspace:3", bound), bound)

    def test_skip_screen_switch(self) -> None:
        self.state("claude-empty")
        os.environ["OWNER_TABS_SKIP_SCREEN"] = "1"
        self.assertEqual(owner_tabs.with_screen_agent("workspace:3", {"agent": ""})["agent"], "")


if __name__ == "__main__":
    unittest.main()
