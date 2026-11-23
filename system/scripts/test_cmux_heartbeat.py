#!/usr/bin/env python3
"""Tests for the switchboard Claude plugin hook hooks/cmux-heartbeat.py (rt-000000000014, 2026-11-02).

A worker tab was mid-turn in a
10-minute sleep loop, its cmux pill read Running, and its sidebar row had no ⚡.
cmux's own PreToolUse hook had lost every tool call after one point to
"Broken pipe (errno 32)" on the cmux socket, so the feed went silent for 20+
minutes and the sidebar's freshness guard turned ⚡ off.
Wrong impls these tests catch: no retry on a failed enqueue (the original bug),
a heartbeat on every tool call (doubles the cmux feed), a throttle that is not
cleared on Stop (a Stop-hook continuation would wait out the throttle with ⚡
off), a heartbeat for AskUserQuestion or ExitPlanMode (a second feed card),
sending outside cmux, a hook that writes stdout or exits non-zero, a retry loop
with no time bound, and hooks.json not registering the hook for every tool.
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

# Never reach a live cmux or wmux tab (2026-11-07, rt-00000000002e).
os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"

REPO = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent).stdout.strip())
HOOK = REPO / "system" / "claude-marketplace" / "plugins" / "switchboard" / "hooks" / "cmux-heartbeat.py"
HOOKS_JSON = HOOK.parent / "hooks.json"

# A fake cmux CLI: appends its argv and stdin to calls.jsonl, and fails with the
# real cmux 0.65.2 socket error until it has failed FAILS times.
FAKE_CMUX = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
d = Path(os.environ["FAKE_CMUX_DIR"])
calls = d / "calls.jsonl"
n = sum(1 for _ in calls.open()) if calls.exists() else 0
with calls.open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                        "pid": os.environ.get("CMUX_CLAUDE_PID"),
                        "timeout": os.environ.get("CMUXTERM_CLI_RESPONSE_TIMEOUT_SEC")}) + "\\n")
if os.environ.get("FAKE_CMUX_HANG"):
    import time
    time.sleep(30)
if n < int(os.environ.get("FAKE_CMUX_FAILS", "0")):
    sys.stderr.write("Error: Failed to write to socket (Broken pipe, errno 32)\\n")
    sys.exit(1)
print("{}")
"""

SESSION = "00000000-0000-4000-8000-0000000000a7"


class CmuxHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = self.tmp / "cmux"
        self.fake.write_text(FAKE_CMUX)
        self.fake.chmod(0o755)
        if os.name == "nt":  # an extensionless script cannot start on Windows
            script, self.fake = self.fake, self.tmp / "cmux.cmd"
            self.fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        self.config = self.tmp / "switchboard-plugin-config.json"
        # WMUX* too: run inside a wmux pane, the real surface and wmux CLI would leak into
        # the cmux cases and send the hook down its wmux branch.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CMUX_", "SWITCHBOARD_", "WMUX"))}
        self.env.update({
            "CMUX_SURFACE_ID": "1924BDC5-DF1C-4DFF-86C9-3387A1BE9E62",
            "CMUX_CLAUDE_HOOK_CMUX_BIN": str(self.fake),
            "CMUX_CLAUDE_PID": "90620",
            "FAKE_CMUX_DIR": str(self.tmp),
            "SWITCHBOARD_CMUX_HEARTBEAT_DIR": str(self.tmp / "stamps"),
            "SWITCHBOARD_PLUGIN_CONFIG": str(self.config),
        })

    def run_hook(self, kind: str, tool: str = "Bash", **env: str) -> subprocess.CompletedProcess:
        payload = {"session_id": SESSION, "cwd": "/tmp", "hook_event_name": "x",
                   "tool_name": tool, "tool_input": {"command": "sleep 598"}}
        result = subprocess.run(["python3", str(HOOK), kind], input=json.dumps(payload), text=True,
                                capture_output=True, env={**self.env, **env}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "", "the hook must never write stdout")
        return result

    def calls(self) -> list[dict]:
        path = self.tmp / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_retries_broken_pipe_until_cmux_accepts(self) -> None:
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="2")
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        last = calls[-1]
        self.assertEqual(last["argv"][-4:], ["hooks", "enqueue", "claude", "pre-tool-use"])
        body = json.loads(last["stdin"])
        self.assertEqual(body["hook_event_name"], "PreToolUse")
        self.assertEqual(body["session_id"], SESSION)
        self.assertEqual(last["pid"], "90620")
        self.assertEqual(last["timeout"], "2")

    def test_throttles_within_window(self) -> None:
        self.run_hook("pre-tool-use")
        self.run_hook("pre-tool-use")
        self.run_hook("pre-tool-use", tool="Read")
        self.assertEqual(len(self.calls()), 1)

    def test_stop_hook_continuation_sends_again(self) -> None:
        # Turn ends (cmux marks the session idle), a Stop hook blocks, the turn
        # continues with no UserPromptSubmit: the first tool call must reach cmux.
        self.run_hook("pre-tool-use")
        self.run_hook("stop")
        self.run_hook("pre-tool-use")
        self.assertEqual(len(self.calls()), 2)

    def test_user_prompt_clears_throttle(self) -> None:
        self.run_hook("pre-tool-use")
        self.run_hook("user-prompt-submit")
        self.run_hook("pre-tool-use")
        self.assertEqual(len(self.calls()), 2)

    def test_stale_stamp_sends(self) -> None:
        self.run_hook("pre-tool-use")
        stamp = self.tmp / "stamps" / SESSION
        old = time.time() - 600
        os.utime(stamp, (old, old))
        self.run_hook("pre-tool-use")
        self.assertEqual(len(self.calls()), 2)

    def test_failure_is_bounded_and_unstamped(self) -> None:
        started = time.monotonic()
        result = self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="99")
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(len(self.calls()), 4)
        self.assertIn("did not accept", result.stderr)
        self.assertFalse((self.tmp / "stamps" / SESSION).exists(), "a failed send must not stamp success")
        # Inside the backoff window the next tool call skips cmux entirely.
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="99")
        self.assertEqual(len(self.calls()), 4, "a failed send must back off, not cost every tool call 3 s")
        # After the backoff window it retries.
        fail = self.tmp / "stamps" / (SESSION + ".fail")
        old = time.time() - 600
        os.utime(fail, (old, old))
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="99")
        self.assertEqual(len(self.calls()), 8)

    @unittest.skipIf(os.name == "nt", "cmux itself does not run on Windows; WmuxHeartbeatTests covers this there")
    def test_hung_cmux_is_bounded(self) -> None:
        started = time.monotonic()
        self.run_hook("pre-tool-use", FAKE_CMUX_HANG="1")
        self.assertLess(time.monotonic() - started, 4.5, "the 3 s budget must cut a hung cmux call")
        started = time.monotonic()
        self.run_hook("pre-tool-use", FAKE_CMUX_HANG="1")
        self.assertLess(time.monotonic() - started, 1.0, "the backoff must skip a hung cmux")

    def test_stop_clears_backoff(self) -> None:
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="99")
        self.run_hook("stop")
        self.run_hook("pre-tool-use")
        self.assertEqual(len(self.calls()), 5)

    def test_socket_path_forwarded(self) -> None:
        self.run_hook("pre-tool-use", CMUX_SOCKET_PATH="/tmp/cmux-test.sock")
        self.assertEqual(self.calls()[0]["argv"][:2], ["--socket", "/tmp/cmux-test.sock"])

    def test_empty_pid_falls_back_to_parent(self) -> None:
        self.run_hook("pre-tool-use", CMUX_CLAUDE_PID="")
        self.assertTrue(self.calls()[0]["pid"].isdigit())

    def test_malformed_stdin_is_silent(self) -> None:
        result = subprocess.run(["python3", str(HOOK), "pre-tool-use"], input="not json", text=True,
                                capture_output=True, env=self.env, timeout=20)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.calls(), [])

    def test_old_stamps_pruned(self) -> None:
        stamps = self.tmp / "stamps"
        stamps.mkdir()
        dead = stamps / "dead-session"
        dead.touch()
        old = time.time() - 3 * 86400
        os.utime(dead, (old, old))
        self.run_hook("pre-tool-use")
        self.assertFalse(dead.exists())
        self.assertTrue((stamps / SESSION).exists())

    def test_outside_cmux_is_silent(self) -> None:
        env = dict(self.env)
        env.pop("CMUX_SURFACE_ID")
        self.env = env
        self.run_hook("pre-tool-use")
        self.assertEqual(self.calls(), [])

    def test_hooks_disabled_env(self) -> None:
        self.run_hook("pre-tool-use", CMUX_CLAUDE_HOOKS_DISABLED="1")
        self.assertEqual(self.calls(), [])

    def test_interactive_tools_skipped(self) -> None:
        for tool in ("AskUserQuestion", "ExitPlanMode", "EnterPlanMode"):
            self.run_hook("pre-tool-use", tool=tool)
        self.assertEqual(self.calls(), [])

    def test_kill_switch(self) -> None:
        self.config.write_text(json.dumps({"cmux-heartbeat.disabled": True}))
        self.run_hook("pre-tool-use")
        self.assertEqual(self.calls(), [])

    def test_hooks_json_registers_every_tool_and_turn_edges(self) -> None:
        hooks = json.loads(HOOKS_JSON.read_text())["hooks"]

        def entries(event: str) -> list[dict]:
            return [m for m in hooks[event] if any("cmux-heartbeat.py" in h["command"] for h in m["hooks"])]

        pre = entries("PreToolUse")
        self.assertEqual(len(pre), 1)
        self.assertIn(pre[0].get("matcher", ""), ("", "*"), "the heartbeat must run for every tool")
        self.assertTrue(any("pre-tool-use" in h["command"] for h in pre[0]["hooks"]))
        self.assertEqual(len(entries("Stop")), 1)
        self.assertEqual(len(entries("UserPromptSubmit")), 1)
        for event in ("PreToolUse", "Stop", "UserPromptSubmit"):
            for m in entries(event):
                for h in m["hooks"]:
                    if "cmux-heartbeat.py" in h["command"]:
                        self.assertLessEqual(h.get("timeout", 60), 10)


class WmuxHeartbeatTests(CmuxHeartbeatTests):
    """wmux pane: report-agent --run-depth through WMUX_BIN, or nothing when wmux's own hooks run."""

    def setUp(self) -> None:
        super().setUp()
        self.env.pop("CMUX_SURFACE_ID")
        self.env.pop("CMUX_CLAUDE_HOOK_CMUX_BIN")
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env.update({"WMUX": "1", "WMUX_SURFACE_ID": "sf-7", "WMUX_BIN": str(self.fake),
                         "CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        for key in ("WMUX_NODE", "WMUX_CLI"):
            self.env.pop(key, None)

    def test_pre_tool_use_sets_run_depth_one_then_throttles(self) -> None:
        self.run_hook("pre-tool-use")
        self.run_hook("pre-tool-use")
        argvs = [c["argv"] for c in self.calls()]
        self.assertEqual(argvs, [["report-agent", "--run-depth", "1", "--surface", "sf-7"]])

    def test_stop_sets_run_depth_zero(self) -> None:
        self.run_hook("pre-tool-use")
        self.run_hook("stop")
        self.assertEqual(self.calls()[-1]["argv"], ["report-agent", "--run-depth", "0", "--surface", "sf-7"])

    def test_wmux_own_hooks_win(self) -> None:
        (self.home / ".claude" / "settings.json").write_text('{"hooks": {"Stop": [{"command": "node wmux-hook.js stop"}]}}')
        for kind in ("user-prompt-submit", "pre-tool-use", "stop"):
            self.run_hook(kind)
        self.assertEqual(self.calls(), [])

    # The cmux-only cases below do not apply to a wmux pane.
    def test_retries_broken_pipe_until_cmux_accepts(self) -> None:
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="1")
        self.assertEqual(len(self.calls()), 1)  # one try per event; the throttle backs off

    def test_socket_path_forwarded(self) -> None:
        pass

    def test_empty_pid_falls_back_to_parent(self) -> None:
        pass

    def test_hung_cmux_is_bounded(self) -> None:
        start = time.monotonic()
        self.run_hook("pre-tool-use", FAKE_CMUX_HANG="1")
        self.assertLess(time.monotonic() - start, 10)

    def test_failure_is_bounded_and_unstamped(self) -> None:
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="9")
        self.run_hook("pre-tool-use", FAKE_CMUX_FAILS="9")
        self.assertEqual(len(self.calls()), 1)  # backoff after a failure

    def test_stop_hook_continuation_sends_again(self) -> None:
        self.run_hook("pre-tool-use")
        self.run_hook("stop")
        self.run_hook("pre-tool-use")
        self.assertEqual([c["argv"][2] for c in self.calls()], ["1", "0", "1"])

    def test_stop_clears_backoff(self) -> None:
        pass

    def test_old_stamps_pruned(self) -> None:
        pass

    def test_outside_cmux_is_silent(self) -> None:
        self.env.pop("WMUX_SURFACE_ID")
        self.run_hook("pre-tool-use")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
