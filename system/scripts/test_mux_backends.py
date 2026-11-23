#!/usr/bin/env python3
"""Per-backend tests for the calls the permission relay, report-to-cos, and the
heartbeat make: send a message to a session, read the pending prompt, find the
caller's workspace, and detect which multiplexer runs.

wmux runs against the fake wmux CLI from test_cmux_shim.py (a Claude composer
on screen, agent-state, typing). cmux runs against a fake `cmux` that records
`agent message` and answers in cmux's JSON shape. Both go through the same
owner_tabs.agent_message() and permission_relay entry points switchboard uses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_cmux_shim import SHIM, ShimHarness  # noqa: E402

from lib import mux  # noqa: E402
import owner_tabs  # noqa: E402

CLAUDE_IDLE = {"surfaceId": "sf-1", "state": "idle", "agent": "claude"}


class WmuxHarness(ShimHarness):
    def set_state(self, **changes):
        state = json.loads(self.state.read_text())
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def get_state(self):
        return json.loads(self.state.read_text())

    def env(self, **extra):
        return super().env(CMUX_SHIM_SETTLE="0", **extra)

    def message(self, target, text, *, stdin=True, **extra):
        args = ["agent", "message", target, "--from", "permission-relay", "--json", "--"]
        args += ["-"] if stdin else [text]
        out = self.run(*args, stdin=text if stdin else None, **extra)
        return out, (json.loads(out.stdout) if out.stdout.strip().startswith("{") else {})


class WmuxAgentMessageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = WmuxHarness(Path(self._tmp.name))
        self.h.set_state(composer={"draft": "", "hint": "", "scrollback": ["● Done."]}, agent=CLAUDE_IDLE)

    def tearDown(self):
        self._tmp.cleanup()

    def sends(self):
        return [c for c in self.h.calls() if c and c[0] in ("send", "send-key")]

    def test_idle_claude_gets_the_message_typed_and_submitted(self):
        out, reply = self.h.message("workspace:1", "Permission ask from worker\nAllow rm -rf build?")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(reply["state"], "delivered")
        self.assertTrue(reply["recipient_has_agent"])
        self.assertIsNone(reply["failure_reason"])
        self.assertEqual(reply["recipient_workspace_ref"], "workspace:1")
        self.assertEqual(reply["recipient_workspace_id"], "ws-1")
        self.assertEqual(self.h.get_state()["submitted"], ["Permission ask from worker ⏎ Allow rm -rf build?"])
        self.assertEqual(self.sends()[-1], ["send-key", "enter", "--surface", "sf-1"])

    def test_target_by_title_and_positional_text(self):
        out, reply = self.h.message("CoS", "hello", stdin=False)
        self.assertEqual(reply["state"], "delivered", out.stderr)
        self.assertEqual(reply["recipient_workspace_id"], "ws-1")

    def test_bracketed_paste_is_opt_in(self):
        self.h.message("workspace:1", "a\nb", SWITCHBOARD_WMUX_PASTE="bracketed")
        sent = [c for c in self.sends() if c[0] == "send"][0]
        self.assertEqual(sent[-1], "\x1b[200~a\nb\x1b[201~")

    def test_blocked_pane_is_refused_and_nothing_is_typed(self):
        self.h.set_state(agent={"surfaceId": "sf-1", "state": "blocked", "agent": "claude",
                                "blockedReason": "permission: Bash"})
        out, reply = self.h.message("workspace:1", "hi")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(reply["state"], "refused")
        self.assertIn("dialog", reply["failure_reason"])
        self.assertEqual(self.sends(), [])

    def test_draft_in_composer_is_never_overwritten(self):
        self.h.set_state(composer={"draft": "half-typed note from the owner"}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "hi")
        self.assertIn("draft", reply["failure_reason"])
        self.assertEqual(self.sends(), [])

    def test_prompt_suggestion_is_not_a_draft(self):
        # A greyed prompt suggestion must not read as "composer holds a draft" and hold reports.
        self.h.set_state(composer={"suggestion": "leave out"}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "report line")
        self.assertIsNone(reply["failure_reason"])
        self.assertEqual(self.h.get_state()["submitted"], ["report line"])

    def test_draft_parked_at_home_is_kept_when_it_looks_like_a_suggestion(self):
        # The cursor alone cannot tell these apart. The composer check before Enter can.
        self.h.set_state(composer={"parked": "half-typed note"}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "report", SWITCHBOARD_SHIM_VERIFY_SECONDS="0")
        self.assertIn("draft", reply["failure_reason"])
        state = self.h.get_state()
        self.assertNotIn("submitted", state)
        self.assertEqual(state["composer"]["parked"], "half-typed note")
        self.assertEqual(state["composer"]["typed"], "")
        self.assertEqual(state["backspaces"], len("report"))

    def test_second_enter_never_submits_a_suggestion_that_came_back(self):
        # Enter on a suggestion sends it as the owner's own words.
        self.h.set_state(composer={"suggestion": "ship it"}, agent=CLAUDE_IDLE)
        self.h.message("workspace:1", "report line")
        self.assertEqual(self.h.get_state()["submitted"], ["report line"])
        enters = [c for c in self.h.calls() if c and c[0] == "send-key" and "enter" in c]
        self.assertEqual(len(enters), 1)

    def test_text_with_the_cursor_after_it_stays_a_draft(self):
        self.h.set_state(composer={"draft": "typed by the owner"}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "hi")
        self.assertIn("draft", reply["failure_reason"])
        self.assertEqual(self.sends(), [])

    def test_text_with_no_cursor_reported_stays_a_draft(self):
        self.h.set_state(composer={"suggestion": "leave out", "cursor": False}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "hi")
        self.assertIn("draft", reply["failure_reason"])
        self.assertEqual(self.sends(), [])

    def test_guards_and_the_waiter_read_a_suggestion_as_empty(self):
        # The next-up digest, the inject guard, and end-of-day close read this text; the
        # report waiter reads agent state. All of them deferred on the suggestion.
        self.h.set_state(composer={"suggestion": "leave out", "scrollback": ["● Done."]}, agent=CLAUDE_IDLE)
        screen = self.h.run("read-screen", "--workspace", "workspace:1", "--lines", "20").stdout
        self.assertNotIn("leave out", screen)
        self.assertIn("● Done.", screen)
        view = json.loads(self.h.run("agent", "state", "workspace:1", "--json").stdout)
        self.assertEqual(view["composer"], "claude-empty")
        self.h.set_state(composer={"draft": "real text"}, agent=CLAUDE_IDLE)
        self.assertIn("real text", self.h.run("read-screen", "--workspace", "workspace:1", "--lines", "20").stdout)
        self.assertEqual(json.loads(self.h.run("agent", "state", "workspace:1", "--json").stdout)["composer"], "claude-text")

    def test_placeholder_hint_reads_as_empty(self):
        self.h.set_state(composer={"draft": "", "hint": 'Try "write a test for x.py"'}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "hi")
        self.assertEqual(reply["state"], "delivered")

    def test_non_claude_pane_is_refused(self):
        self.h.set_state(agent={"surfaceId": "sf-1", "state": "idle", "agent": "codex"})
        _, reply = self.h.message("workspace:1", "hi")
        self.assertFalse(reply["recipient_has_agent"])
        self.assertIn("codex", reply["failure_reason"])
        self.assertEqual(self.sends(), [])

    def test_unknown_screen_is_refused(self):
        state = self.h.get_state()
        state.pop("composer")
        state["screen"] = "PS C:\\> \n"
        state["agent"] = {"surfaceId": "sf-1", "state": "unknown"}
        self.h.state.write_text(json.dumps(state))
        _, reply = self.h.message("workspace:1", "hi")
        self.assertEqual(reply["state"], "refused")
        self.assertFalse(reply["recipient_has_agent"])
        self.assertEqual(self.sends(), [])

    def test_stuck_composer_retries_enter_once_and_reports_typed(self):
        self.h.set_state(composer={"draft": "", "stuck": True}, agent=CLAUDE_IDLE)
        _, reply = self.h.message("workspace:1", "hi")
        self.assertEqual(reply["state"], "typed")
        self.assertIsNone(reply["failure_reason"])  # typed: callers must not retry
        self.assertEqual([c for c in self.sends() if c[0] == "send-key"], [["send-key", "enter", "--surface", "sf-1"]] * 2)

    def test_owner_tabs_agent_message_reads_the_shim_reply(self):
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(owner_tabs, "_cmux_argv", return_value=[sys.executable, str(SHIM)]):
            ok = owner_tabs.agent_message("workspace:1", "relay text", "permission-relay")
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(ok["workspace_id"], "ws-1")
            self.h.set_state(agent={"surfaceId": "sf-1", "state": "blocked", "agent": "claude"})
            refused = owner_tabs.agent_message("workspace:1", "relay text", "permission-relay")
            self.assertFalse(refused["ok"])
            self.assertIn("dialog", refused["reason"])

    def test_agent_state_verb(self):
        out = self.h.run("agent", "state", "workspace:1", "--json")
        view = json.loads(out.stdout)
        self.assertEqual(view["agent"], "claude")
        self.assertEqual(view["composer"], "claude-empty")

    def test_control_characters_are_dropped_and_a_leading_command_char_is_quoted(self):
        _, reply = self.h.message("workspace:1", "/clear now\x03 then\x1b[201~ go\r\tok")
        self.assertEqual(reply["state"], "delivered")
        self.assertEqual(self.h.get_state()["submitted"], ["[message] /clear now then[201~ go ok"])

    def test_bang_bash_mode_is_never_triggered(self):
        self.h.message("workspace:1", "!rm -rf ~")
        self.assertEqual(self.h.get_state()["submitted"], ["[message] !rm -rf ~"])

    @unittest.skipIf(os.name == "nt", "the fake wmux cannot serve read-screen through a real .cmd wrapper")
    def test_wmux_cmd_refuses_text_cmd_exe_would_rewrite(self):
        batch = self.h.tmp / "wmux.cmd"
        batch.write_text(f'@"{sys.executable}" "{self.h.fake}" %*\r\n' if os.name == "nt"
                         else f"#!/bin/sh\nexec {sys.executable} {self.h.fake} \"$@\"\n")
        batch.chmod(0o755)
        _, reply = self.h.message("workspace:1", 'Allow rm "x" & calc.exe', WMUX_BIN=str(batch))
        self.assertEqual(reply["state"], "refused")
        self.assertIn("cmd.exe", reply["failure_reason"])
        self.assertEqual(self.sends(), [])
        _, reply = self.h.message("workspace:1", "plain words only", WMUX_BIN=str(batch))
        self.assertEqual(reply["state"], "delivered")

    def test_without_a_prompt_log_an_empty_composer_still_reads_delivered(self):
        _, reply = self.h.message("workspace:1", "short line")
        self.assertEqual((reply["state"], reply["verified"]), ("delivered", "composer"))


# A wmux with the prompt log (`wmux prompts --json`): every line the pane accepted.
# state["prompt_log"] lists earlier entries; each submitted draft is appended,
# cut to its last state["prompt_truncate"] characters when set.
PROMPTS_HANDLER = r'''if verb == "prompts" and "prompt_log" in state:
    texts = list(state["prompt_log"]) + [t[-state["prompt_truncate"]:] if state.get("prompt_truncate") else t
                                         for t in state.get("submitted", []) if not state.get("prompt_lag")]
    print(json.dumps({"surfaceId": flag("--surface"), "enabled": True, "truncated": False,
                      "prompts": [{"seq": i + 1, "source": "agent", "text": t} for i, t in enumerate(texts)]}))
el'''


class ProvenDeliveryHarness(WmuxHarness):
    def __init__(self, tmp):
        super().__init__(tmp)
        from test_cmux_shim import FAKE_WMUX
        self.fake.write_text(FAKE_WMUX.replace('if verb == "list-workspaces":', PROMPTS_HANDLER + 'if verb == "list-workspaces":', 1))
        self.spill = tmp / "spill"

    def env(self, **extra):
        return super().env(SWITCHBOARD_WMUX_SPILL_DIR=str(self.spill), SWITCHBOARD_SHIM_VERIFY_SECONDS="0.3", **extra)


class WmuxProvenDeliveryTest(unittest.TestCase):
    """A long report typed into a Claude CoS on wmux that arrives truncated is not delivered."""

    REPORT = "[🤖 Example Research · rt-00000000004d] done: " + "Research done, all findings in the note. " * 70

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = ProvenDeliveryHarness(Path(self._tmp.name))
        self.h.set_state(composer={"draft": "", "hint": "", "scrollback": ["● Done."]}, agent=CLAUDE_IDLE,
                         prompt_log=["earlier owner prompt"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_long_text_is_spilled_to_a_file_and_a_short_pointer_is_typed(self):
        out, reply = self.h.message("workspace:1", self.REPORT)
        self.assertEqual(reply["state"], "delivered", out.stderr)
        self.assertEqual(reply["verified"], "prompt-log")
        typed = self.h.get_state()["submitted"][-1]
        self.assertLessEqual(len(typed), 300)
        self.assertTrue(typed.startswith("[🤖 Example Research · rt-00000000004d] done:"), typed)
        spill = Path(reply["spilled_to"])
        self.assertIn(str(spill), typed)
        self.assertEqual(spill.read_text(encoding="utf-8").strip(), self.REPORT.strip())
        self.assertEqual(reply["spilled_chars"], len(self.REPORT))

    def test_short_text_is_typed_whole(self):
        _, reply = self.h.message("workspace:1", "[🧭 switchboard · rt-1] done: short")
        self.assertEqual((reply["state"], reply["verified"]), ("delivered", "prompt-log"))
        self.assertNotIn("spilled_to", reply)

    def test_a_truncated_prompt_entry_is_garbled_not_delivered(self):
        self.h.set_state(prompt_truncate=40)
        _, reply = self.h.message("workspace:1", "[🧭 switchboard · rt-1] done: " + "word " * 40)
        self.assertEqual(reply["state"], "garbled")
        self.assertIn("did not arrive whole", reply["failure_reason"])

    def test_no_new_prompt_entry_reads_queued_not_delivered(self):
        self.h.set_state(prompt_lag=True)
        _, reply = self.h.message("workspace:1", "[🧭 switchboard · rt-1] done: short")
        self.assertEqual((reply["state"], reply["verified"]), ("queued", "none"))
        self.assertIsNone(reply["failure_reason"])

    def test_owner_tabs_marks_only_a_proven_line_verified(self):
        env = self.h.env()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(owner_tabs, "_cmux_argv", return_value=[sys.executable, str(SHIM)]):
            ok = owner_tabs.agent_message("workspace:1", "[🧭 switchboard · rt-1] done: short", "switchboard report")
            self.assertTrue(ok["ok"] and ok["verified"], ok)
            self.assertEqual(ok["proof"], "prompt-log")
            self.h.set_state(prompt_lag=True)
            queued = owner_tabs.agent_message("workspace:1", "[🧭 switchboard · rt-2] done: short", "switchboard report")
            self.assertTrue(queued["ok"])
            self.assertFalse(queued["verified"])
            self.h.set_state(prompt_lag=False, prompt_truncate=10)
            garbled = owner_tabs.agent_message("workspace:1", "[🧭 switchboard · rt-3] done: " + "x " * 60, "switchboard report")
            self.assertFalse(garbled["ok"] or garbled["verified"])

    def test_agent_seen_finds_a_marker_on_the_prompt_log_then_the_screen(self):
        self.h.message("workspace:1", self.REPORT)
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "[🤖 Example Research · rt-00000000004d] done:", "--json")
        self.assertEqual(json.loads(out.stdout)["where"], "prompt-log", out.stderr)
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "rt-nothere] done:", "--json")
        self.assertFalse(json.loads(out.stdout)["seen"])
        self.h.set_state(prompt_log=[], submitted=[], composer={"draft": "", "scrollback": ["> [🧭 switchboard · rt-9]", "  done: queued"]})
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "[🧭 switchboard · rt-9] done:", "--json")
        self.assertEqual(json.loads(out.stdout)["where"], "screen")

    def test_agent_seen_ignores_a_line_still_in_the_composer(self):
        # An Enter that never landed leaves the line in the box: not taken.
        self.h.set_state(prompt_log=[], submitted=[], composer={"draft": "[🧭 switchboard · rt-9] done: unsent", "scrollback": []})
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "[🧭 switchboard · rt-9] done:", "--json")
        self.assertFalse(json.loads(out.stdout)["seen"], out.stdout)

    def test_agent_seen_rejects_a_pointer_line_cut_short(self):
        # The opening arrived, the file path did not.
        self.h.set_state(prompt_log=["[🧭 switchboard · rt-9] done: start … [full message, 2900 characters: C:\\Us"])
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "[🧭 switchboard · rt-9] done:", "--json")
        self.assertFalse(json.loads(out.stdout)["seen"], out.stdout)
        self.h.set_state(prompt_log=["[🧭 switchboard · rt-9] done: start … [full message, 2900 characters: C:\\m.md: "
                                     "read that file and show it in full]"])
        out = self.h.run("agent", "seen", "workspace:1", "--marker", "[🧭 switchboard · rt-9] done:", "--json")
        self.assertTrue(json.loads(out.stdout)["seen"], out.stdout)

    def test_owner_tabs_seen_on_macos_cmux_is_unsupported(self):
        with mock.patch.object(owner_tabs, "_cmux_argv", return_value=[sys.executable, "-c", "import sys; sys.exit(2)"]):
            self.assertEqual(owner_tabs.agent_seen("workspace:1", "x")["supported"], False)


class WmuxMessageMiscTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = WmuxHarness(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_workspace_id_of_surface_tries_the_bare_form_first(self):
        out = self.h.run("workspace-id-of-surface", WMUX_SURFACE_ID="sf-2", FAKE_CURRENT="ws-2")
        self.assertEqual(out.stdout.strip(), "ws-2")
        self.assertIn(["current-workspace"], self.h.calls())
        self.assertNotIn(["current-workspace", "--surface", "sf-2"], self.h.calls())

    def test_workspace_id_of_surface_falls_back_to_the_caller_surface(self):
        out = self.h.run("workspace-id-of-surface", WMUX_SURFACE_ID="sf-2", FAKE_CURRENT="ws-2",
                         FAKE_CURRENT_NEEDS_SURFACE="1")
        self.assertEqual(out.stdout.strip(), "ws-2")
        self.assertIn(["current-workspace", "--surface", "sf-2"], self.h.calls())


class ComposerParserTest(unittest.TestCase):
    """The Python composer parser must agree with cig_composer_state in cmux-inject-guard.sh."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cmux_shim", SHIM)
        cls.shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.shim)

    def state(self, screen):
        return self.shim.Shim.composer(screen)[0]

    def test_cases_match_the_bash_guard(self):
        rule = "─" * 30
        cases = {
            f"{rule}\n❯ \n{rule}\n": "claude-empty",
            f"{rule}\n❯ draft text\n{rule}\n": "claude-text",
            f"{rule}\n❯ first\n  second line\n{rule}\n": "claude-text",
            f"{rule}\n❯ Press up to edit queued messages\n{rule}\n": "claude-empty",
            f"❯ old prompt\n{rule}\n❯ \n": "none",                         # no bottom rule
            "user@host ❯ ls\n": "none",                                    # a shell prompt
            f"{rule}\n❯ echoed\n{rule}\nout\n{rule}\n❯ \n{rule}\n": "claude-empty",  # last composer wins
            # A Windows composer draws its prompt as ">"; it must not read as "none".
            f"{rule}\n> \n{rule}\n  ⏵⏵ bypass permissions on\n": "claude-empty",
            f"{rule}\n>\n{rule}\n": "claude-empty",
            f"{rule}\n> a draft\n{rule}\n": "claude-text",
            f"{rule}\n>> \n{rule}\n": "none",                                # never ">>"
            "PS C:\\> \n> \n": "none",                                      # a shell, no rules
        }
        guard = HERE / "lib" / "cmux-inject-guard.sh"
        for screen, want in cases.items():
            self.assertEqual(self.state(screen), want, screen)
            bash = subprocess.run(["bash", "-c", f'source "{guard}" >/dev/null 2>&1; cig_composer_state'],
                                  input=screen, capture_output=True, text=True)
            if bash.returncode == 0 and bash.stdout.strip():
                self.assertEqual(bash.stdout.strip(), want, f"bash guard disagrees on {screen!r}")


class CmuxBackendTest(unittest.TestCase):
    """macOS cmux: agent message goes to cmux itself, unchanged."""

    FAKE_CMUX = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_CMUX_LOG"], "a").write(json.dumps(sys.argv[1:] + [sys.stdin.read() if "-" in sys.argv else ""]) + "\n")
if sys.argv[1:3] == ["agent", "message"]:
    print(json.dumps({"id": "m1", "state": "queued", "recipient_has_agent": True, "failure_reason": None,
                      "recipient_workspace_ref": sys.argv[3], "recipient_workspace_id": "W1",
                      "recipient_workspace_title": "CoS"}))
'''

    def test_agent_message_calls_cmux_with_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "cmux"
            fake.write_text(self.FAKE_CMUX)
            fake.chmod(0o755)
            log = Path(tmp) / "log"
            with mock.patch.dict(os.environ, {"FAKE_CMUX_LOG": str(log)}), \
                    mock.patch.object(owner_tabs, "_cmux_argv", return_value=[sys.executable, str(fake)]):
                out = owner_tabs.agent_message("workspace:3", "line one\nline two", "CoS")
            self.assertTrue(out["ok"])
            call = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(call[:6], ["agent", "message", "workspace:3", "--from", "CoS", "--json"])
            self.assertEqual(call[-1], "line one\nline two")


class DetectionTest(unittest.TestCase):
    def env(self, **values):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("CMUX_", "WMUX", "SWITCHBOARD_MUX"))}
        base.update(values)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_wmux_pane_env_is_wmux(self):
        with self.env(WMUX="1", WMUX_SURFACE_ID="sf-9"):
            self.assertEqual(mux.active(), "wmux")
            self.assertEqual(mux.backend(), "wmux")

    def test_cmux_pane_env_is_cmux_on_macos(self):
        with self.env(CMUX_SURFACE_ID="S1", CMUX_BIN="/bin/echo"), mock.patch("platform.system", return_value="Darwin"):
            self.assertEqual(mux.active(), "cmux")

    def test_windows_without_a_running_wmux_is_none(self):
        with self.env(), mock.patch("platform.system", return_value="Windows"), \
                mock.patch.object(mux, "wmux_running", return_value=False):
            self.assertEqual(mux.backend(), "wmux")
            self.assertEqual(mux.active(), "none")

    def test_current_workspace_id_prefers_cmux_env(self):
        with self.env(CMUX_WORKSPACE_ID="W7", WMUX="1"):
            self.assertEqual(mux.current_workspace_id(), "W7")

    def test_current_workspace_id_asks_the_shim_on_wmux(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = ShimHarness(Path(tmp))
            env = h.env(WMUX="1", WMUX_SURFACE_ID="sf-2", FAKE_CURRENT="ws-2", CMUX_BIN=str(HERE / "mux" / "cmux"))
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(mux.current_workspace_id(), "ws-2")

    def test_current_workspace_id_is_empty_outside_a_pane(self):
        with self.env(), mock.patch("platform.system", return_value="Darwin"):
            self.assertEqual(mux.current_workspace_id(), "")

    def test_wmux_cli_finds_the_install_folder_off_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Programs" / "wmux"
            (root / "resources" / "cli").mkdir(parents=True)
            (root / "resources" / "cli" / "wmux.js").write_text("// cli")
            (root / "wmux.exe").write_text("MZ")
            with self.env(LOCALAPPDATA=tmp, PATH="/nonexistent"):
                argv, extra = mux.wmux_cli()
            self.assertEqual(argv, [str(root / "wmux.exe"), str(root / "resources" / "cli" / "wmux.js")])
            self.assertEqual(extra, {"ELECTRON_RUN_AS_NODE": "1"})

    def test_wmux_cli_prefers_node_over_a_cmd_wmux_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "wmux.js"
            cli.write_text("// cli")
            with self.env(WMUX_BIN=str(Path(tmp) / "wmux.cmd"), WMUX_NODE="/x/node.exe", WMUX_CLI=str(cli)):
                self.assertEqual(mux.wmux_cli()[0], ["/x/node.exe", str(cli)])
            with self.env(WMUX_BIN=str(Path(tmp) / "wmux.cmd"), WMUX_NODE="", WMUX_CLI="", LOCALAPPDATA=tmp,
                          PATH="/nonexistent"):
                self.assertEqual(mux.wmux_cli()[0], [str(Path(tmp) / "wmux.cmd")])

    def test_capability_table_names_every_relay_call(self):
        for call in ("agent message", "read screen", "send key", "pending prompt", "current workspace", "heartbeat"):
            self.assertIn(call, mux.CAPABILITIES)


class RelayUnderWmuxTest(unittest.TestCase):
    """permission_relay notify no longer exits when only WMUX_SURFACE_ID is set."""

    def test_notify_resolves_the_workspace_through_mux(self):
        import permission_relay
        payload = json.dumps({"notification_type": "permission_prompt", "session_id": "abc12345"})
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CMUX_", "WMUX"))}
        env.update({"WMUX": "1", "WMUX_SURFACE_ID": "sf-1"})
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(permission_relay, "current_workspace_id", return_value="ws-1"), \
                mock.patch.object(permission_relay, "disabled", return_value=False), \
                mock.patch.object(permission_relay, "_detach") as detach, \
                mock.patch.object(sys, "argv", ["permission_relay.py", "notify"]), \
                mock.patch.object(sys, "stdin", new=__import__("io").StringIO(payload)):
            self.assertEqual(permission_relay.main(), 0)
        detach.assert_called_once()
        written = json.loads(Path(detach.call_args[0][0][2]).read_text())
        Path(detach.call_args[0][0][2]).unlink()
        self.assertEqual(written["workspace_id"], "ws-1")


if __name__ == "__main__":
    unittest.main()
