#!/usr/bin/env python3
"""Tests for system/scripts/mux/pm-os-board.py against the fake wmux from
test_cmux_shim.ShimHarness. Run: python3 -m unittest test_pm_os_board"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_cmux_shim import ShimHarness

HERE = Path(__file__).resolve().parent
BOARD = HERE / "mux" / "pm-os-board.py"
SESSION = "0b7c9d2e-1111-4222-8333-444455556666"  # a Claude Code session id


def load_board():
    spec = importlib.util.spec_from_file_location("pm_os_board", BOARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EnvPatch:
    """The board shells out to the shim with os.environ, so point it at the fake wmux."""

    def __init__(self, env: dict[str, str]):
        self.env = env

    def __enter__(self):
        self.saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = ShimHarness(Path(self._tmp.name))
        self.patch = EnvPatch(self.h.env())
        self.patch.__enter__()
        self.board = load_board()

    def tearDown(self):
        self.patch.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_state_matches_shim_lane_view(self):
        view = self.board.lane_view()
        self.assertEqual(view["workspace_count"], 2)
        self.assertEqual([l["key"] for l in view["lanes"]][:3], ["pinned", "attention", "active"])

    def test_markdown_render_lists_lanes_and_rows(self):
        self.h.run("workspace-action", "--action", "pin", "--workspace", "workspace:1")
        md = self.board.render_markdown(self.board.lane_view())
        self.assertIn("2 ws · 0 unread", md)
        self.assertIn("## Pinned (1)", md)
        self.assertIn("- **🧭 CoS**", md)
        self.assertIn("## Idle (1)", md)
        self.assertIn("🚀 Launchpad", md)

    def test_mark_read_clears_review_hold_color(self):
        self.h.run("workspace-action", "--action", "set-color", "--color", "#bb9af7", "--workspace", "workspace:2")
        self.h.run("workspace-action", "--action", "mark-unread", "--workspace", "workspace:2")
        self.board.apply_action({"action": "mark-read", "workspace": "ws-2"})
        rows = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertIsNone(rows[1]["custom_color"])
        self.assertEqual(self.board.lane_view()["unread_total"], 0)
        # another color survives mark-read
        self.h.run("workspace-action", "--action", "set-color", "--color", "#8e44ad", "--workspace", "workspace:2")
        self.board.apply_action({"action": "mark-read", "workspace": "ws-2"})
        rows = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertEqual(rows[1]["custom_color"], "#8e44ad")

    def test_relaunch_reopens_on_the_bound_thread_and_closes_the_old_row(self):
        self.h.run("session-bind", "--workspace", "workspace:2", "--thread", "T-abc")
        self.h.run("workspace-action", "--action", "pin", "--workspace", "workspace:2")
        self.h.reset_log()
        result = self.board.apply_action({"action": "relaunch", "workspace": "ws-2"})
        self.assertTrue(result["ok"])
        calls = self.h.calls()
        created = next(c for c in calls if c[0] == "new-workspace")
        self.assertIn("🚀 Launchpad", created)
        self.assertIn("C:\\pm-os", created)
        typed = " ".join(" ".join(c) for c in calls if c[0] == "send")
        self.assertIn("amp threads continue T-abc", typed)
        self.assertIn(["close-workspace", "ws-2"], calls)
        self.assertIn(["select-workspace", "ws-3"], calls)
        rows = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertEqual([r["id"] for r in rows], ["ws-1", "ws-3"])
        self.assertTrue(rows[1]["pinned"])
        view = self.board.lane_view()
        self.assertEqual(self.board.find_row(view, "ws-3")["thread"], "T-abc")

    def test_relaunch_resumes_a_claude_row_with_claude(self):
        self.h.run("session-bind", "--workspace", "workspace:2", "--thread", SESSION)
        self.h.reset_log()
        self.assertTrue(self.board.apply_action({"action": "relaunch", "workspace": "ws-2"})["ok"])
        calls = self.h.calls()
        typed = " ".join(" ".join(c) for c in calls if c[0] == "send")
        self.assertIn(f"claude --resume {SESSION}", typed)
        self.assertNotIn("amp", typed)
        self.assertIn(["close-workspace", "ws-2"], calls)
        self.assertEqual(self.board.find_row(self.board.lane_view(), "ws-3")["thread"], SESSION)

    def test_relaunch_all_gives_each_row_its_own_agent(self):
        self.h.run("session-bind", "--workspace", "workspace:1", "--thread", "T-abc")
        self.h.run("session-bind", "--workspace", "workspace:2", "--thread", SESSION)
        self.h.reset_log()
        self.assertEqual(self.board.apply_action({"action": "relaunch-all"})["count"], 2)
        typed = " ".join(" ".join(c) for c in self.h.calls() if c[0] == "send")
        self.assertIn("amp threads continue T-abc", typed)
        self.assertIn(f"claude --resume {SESSION}", typed)
        self.assertNotIn(f"amp threads continue {SESSION}", typed)

    def test_reopen_last_closed_resumes_a_claude_session_with_claude(self):
        self.h.run("session-bind", "--workspace", "workspace:2", "--thread", SESSION)
        self.board.apply_action({"action": "close", "workspace": "ws-2"})
        self.h.reset_log()
        self.board.apply_action({"action": "reopen-last-closed"})
        typed = " ".join(" ".join(c) for c in self.h.calls() if c[0] == "send")
        self.assertIn(f"claude --resume {SESSION}", typed)
        self.assertNotIn("amp", typed)

    def test_relaunch_command_by_row_kind(self):
        self.assertEqual(self.board.relaunch_command(SESSION), f"claude --resume {SESSION}")
        self.assertEqual(self.board.relaunch_command("T-abc"), "amp threads continue T-abc")
        # Not a session id, so nothing marks it as Claude: it keeps the old command, quoted.
        self.assertEqual(self.board.relaunch_command("odd id"), "amp threads continue 'odd id'")

    def set_default_agent(self, agent: str) -> None:
        """This machine's default, set the way the owner sets it: `agent:` in router.yaml."""
        config = Path(self._tmp.name) / f"router-{agent}.yaml"
        config.write_text(f"agent: {agent}\n", encoding="utf-8")
        os.environ["ROUTER_CONFIG"] = str(config)

    def typed_lines(self) -> list[str]:
        return [c[-1].strip() for c in self.h.calls() if c[0] == "send"]

    def test_unbound_row_relaunches_on_the_configured_default_agent(self):
        for agent in ("claude", "amp"):
            with self.subTest(agent=agent):
                self.set_default_agent(agent)
                self.h.reset_log()
                wid = next(r["id"] for lane in self.board.lane_view()["lanes"] for r in lane["workspaces"]
                           if not r.get("thread"))
                self.assertTrue(self.board.apply_action({"action": "relaunch", "workspace": wid})["ok"])
                self.assertEqual(self.typed_lines(), [agent])
                self.assertIn(["close-workspace", wid], self.h.calls())

    def test_unbound_reopen_last_closed_opens_the_configured_default_agent(self):
        for agent in ("claude", "amp"):
            with self.subTest(agent=agent):
                self.set_default_agent(agent)
                self.board.apply_action({"action": "close", "workspace": "ws-2" if agent == "claude" else "ws-1"})
                self.h.reset_log()
                self.board.apply_action({"action": "reopen-last-closed"})
                self.assertEqual(self.typed_lines(), [agent])

    def test_process_override_beats_the_configured_default_agent(self):
        self.set_default_agent("claude")
        os.environ["PM_OS_AGENT"] = "amp"
        self.assertEqual(self.board.relaunch_command(None), "amp")
        self.set_default_agent("amp")
        os.environ["PM_OS_AGENT"] = "claude"
        self.assertEqual(self.board.relaunch_command(None), "claude")

    def test_bound_row_keeps_its_own_agent_whatever_the_default(self):
        for agent in ("claude", "amp"):
            with self.subTest(agent=agent):
                self.set_default_agent(agent)
                self.assertEqual(self.board.relaunch_command(SESSION), f"claude --resume {SESSION}")
                self.assertEqual(self.board.relaunch_command("T-abc"), "amp threads continue T-abc")

    def test_copy_thread_url_and_unknown_action(self):
        with self.assertRaises(self.board.BoardError):
            self.board.apply_action({"action": "copy-thread-url", "workspace": "ws-1"})
        self.h.run("session-bind", "--workspace", "workspace:1", "--thread", "T-xyz")
        self.assertEqual(self.board.apply_action({"action": "copy-thread-url", "workspace": "ws-1"})["url"],
                         "https://ampcode.com/threads/T-xyz")
        with self.assertRaises(self.board.BoardError):
            self.board.apply_action({"action": "explode", "workspace": "ws-1"})

    def test_http_server_serves_page_state_and_actions(self):
        port = free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), self.board.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("PM OS board", page)
            self.assertIn("/api/state", page)
            state = json.loads(urllib.request.urlopen(base + "/api/state").read())
            self.assertEqual(state["selected"], "ws-1")
            req = urllib.request.Request(base + "/api/action", data=json.dumps({"action": "select", "workspace": "ws-2"}).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            self.assertTrue(json.loads(urllib.request.urlopen(req).read())["ok"])
            self.assertEqual(json.loads(urllib.request.urlopen(base + "/api/state").read())["selected"], "ws-2")
            bad = urllib.request.Request(base + "/api/action", data=json.dumps({"action": "rename", "workspace": "ws-2"}).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad)
            self.assertEqual(ctx.exception.code, 400)
            self.assertIn("title", json.loads(ctx.exception.read())["error"])
            self.assertTrue(self.board.port_live("127.0.0.1", port))
        finally:
            server.shutdown()
            server.server_close()

    def test_ensure_starts_a_detached_server_and_open_uses_a_browser_surface(self):
        port = free_port()
        os.environ["PM_OS_BOARD_LOG"] = str(Path(self._tmp.name) / "board.log")
        url = self.board.ensure("127.0.0.1", port)
        try:
            self.assertEqual(url, f"http://127.0.0.1:{port}/")
            self.assertEqual(json.loads(urllib.request.urlopen(url + "healthz").read())["ok"], True)
            self.h.reset_log()
            out = self.board.open_in_wmux(url, "ws-1", pane=True)
            self.assertIn("browser surface", out)
            calls = self.h.calls()
            self.assertEqual(calls[0][:3], ["new-surface", "--type", "browser"])
            self.assertEqual(calls[1][:3], ["browser", "open", url])
        finally:
            msg = self.board.stop()
        self.assertTrue(msg.startswith("stopped board pid"), msg)
        self.assertFalse(self.board.pid_path().exists())
        for _ in range(50):
            if not self.board.port_live("127.0.0.1", port):
                break
            import time
            time.sleep(0.1)
        self.assertFalse(self.board.port_live("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
