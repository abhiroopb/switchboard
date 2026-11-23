#!/usr/bin/env python3
"""permission_relay.py and the dispatch.py permission steer: a permission prompt
in a worker tab reaches CoS, and CoS answers it with `--steer <rt-id> allow|deny`.

Hermetic: a temp state dir, a fake cmux binary that serves a scripted screen
and records keys, and patched tab, ledger, and delivery lookups. Nothing
reaches a live CoS.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TMP = tempfile.TemporaryDirectory()
os.environ["SWITCHBOARD_STATE_DIR"] = str(Path(TMP.name) / "state")
os.environ["PERMISSION_RELAY_OWNER_DELAY"] = "0"
# Never reach a live cmux or wmux tab: lib/mux, the inject guard and the shim fail closed.
os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"
os.environ["SWITCHBOARD_PLUGIN_CONFIG"] = str(Path(TMP.name) / "plugin-config.json")
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import permission_relay as relay  # noqa: E402

PROMPT_SCREEN = """\
 Bash command

   sudo rm -rf /opt/example
   Remove a stale folder

 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)
"""
IDLE_SCREEN = "> \n  ? for shortcuts\n"
TAB = {"ref": "workspace:7", "id": "WS-7", "title": "🔧 Switchboard Maintenance 2", "description": "switchboard worker rt-000000000007"}
ROW = {"id": "rt-000000000007", "status": "active", "workstream": "switchboard-maintenance", "workspace_ref": "workspace:7",
       "session_id": "sess-1"}


def fake_cmux(screens: list[str]) -> tuple[str, Path]:
    """A cmux stand-in: read-screen pops the next screen; send-key appends to keys.txt."""
    root = Path(tempfile.mkdtemp(dir=TMP.name))
    (root / "screens.json").write_text(json.dumps(screens))
    keys = root / "keys.txt"
    script = root / "cmux"
    script.write_text(f"""#!{sys.executable}
import json, sys
root = {str(root)!r}
args = sys.argv[1:]
if args[:1] == ["read-screen"]:
    screens = json.load(open(root + "/screens.json"))
    print(screens.pop(0) if len(screens) > 1 else screens[0])
    json.dump(screens, open(root + "/screens.json", "w"))
elif args[:1] == ["send-key"]:
    open(root + "/keys.txt", "a").write(args[-1] + "\\n")
else:
    sys.exit(1)
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), keys


class PermissionRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        state = relay._state_path()
        if state.exists():
            state.unlink()

    def test_prompt_detection(self) -> None:
        self.assertTrue(relay.prompt_visible(PROMPT_SCREEN))
        self.assertTrue(relay.YES_SELECTED_RE.search(PROMPT_SCREEN))
        self.assertFalse(relay.prompt_visible(IDLE_SCREEN))
        self.assertFalse(relay.prompt_visible(None))
        self.assertFalse(relay.prompt_visible("Do you want to proceed? (prose only, no options)"))

    def test_message_for_a_worker_row_names_the_worker_and_the_steer(self) -> None:
        with mock.patch.object(relay.owner_tabs, "owner_name", return_value="Switchboard Maintenance"):
            text = relay.build_message(tab=TAB, row=ROW, tool="Bash", command="sudo rm -rf /opt/example",
                                       reason="switchboard-guardrails: confirm rm -rf", kind="permission_prompt")
        self.assertTrue(text.startswith("[ROUTER REPORT] 🔐 🔧 Switchboard Maintenance 2, the Switchboard Maintenance worker for rt-000000000007 (workspace:7)"))
        self.assertIn("```\nsudo rm -rf /opt/example\n```", text)
        self.assertIn("--steer rt-000000000007 allow", text)
        self.assertIn("never answer it without their word", text)

    def test_message_variants_for_owner_tab_amp_and_question(self) -> None:
        owner = relay.build_message(tab=TAB, row={}, tool="Bash", command="x", reason="", kind="permission_prompt")
        self.assertIn("permission_relay.py answer --workspace-ref workspace:7 allow", owner)
        amp = relay.build_message(tab=TAB, row=ROW, tool="Bash", command="x", reason="", kind="permission_prompt", amp_timeout_s=60)
        self.assertIn("within about 60 s", amp)
        self.assertIn("cannot be pressed from CoS", amp)
        question = relay.build_message(tab=TAB, row=ROW, tool="", command="", reason="Pick one", kind="elicitation_dialog")
        self.assertIn("waiting on a question", question)
        self.assertIn("cannot be answered from CoS", question)

    def test_transcript_tool_call_returns_the_open_call(self) -> None:
        path = Path(TMP.name) / "t.jsonl"
        rows = [
            {"message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}},
            {"message": {"content": [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "sudo rm -rf /opt/x"}}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(relay.transcript_tool_call(str(path)), {"tool": "Bash", "command": "sudo rm -rf /opt/x"})
        self.assertEqual(relay.transcript_tool_call(""), {})

    def test_recorded_ask_is_used_only_when_fresh(self) -> None:
        asks = relay.STATE_DIR / "worker-dispatch" / "permission-asks"
        asks.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        (asks / "sess-1.json").write_text(json.dumps({"session_id": "sess-1", "command": "c", "at": now}))
        self.assertEqual(relay.recorded_ask("sess-1")["command"], "c")
        self.assertEqual(relay.recorded_ask("sess-1", now=time.time() + 600), {})

    def test_send_dry_run_builds_one_callback_and_dedupes(self) -> None:
        sent: list[tuple[str, str]] = []
        with mock.patch.object(relay, "find_tab", return_value=TAB), \
             mock.patch.object(relay, "is_operator_tab", return_value=False), \
             mock.patch.object(relay, "live_row", return_value=ROW), \
             mock.patch.object(relay, "read_screen", return_value=PROMPT_SCREEN), \
             mock.patch.object(relay, "recorded_ask", return_value={"tool": "Bash", "command": "sudo rm -rf /opt/example", "reason": "confirm"}), \
             mock.patch.object(relay, "_cos_ref", return_value="workspace:1"), \
             mock.patch.object(relay.owner_tabs, "owner_name", return_value="Switchboard Maintenance"), \
             mock.patch.object(relay.owner_tabs, "agent_message", side_effect=lambda ref, text, sender: sent.append((ref, text)) or {"ok": True}):
            payload = {"notification_type": "permission_prompt", "session_id": "sess-1", "workspace_id": "WS-7"}
            dry = relay.send(payload, dry_run=True)
            self.assertTrue(dry["dry_run"])
            self.assertEqual(sent, [])
            first = relay.send(payload)
            second = relay.send(payload)
        self.assertTrue(first["sent"])
        self.assertEqual(first["task_id"], "rt-000000000007")
        self.assertFalse(second["sent"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "workspace:1")
        self.assertIn("sudo rm -rf /opt/example", sent[0][1])

    def test_send_skips_operator_tabs_and_gone_prompts(self) -> None:
        with mock.patch.object(relay, "find_tab", return_value={"ref": "workspace:1", "title": "🧭 CoS"}):
            self.assertIn("operator tab", relay.send({"workspace_id": "WS-1"})["reason"])
        with mock.patch.object(relay, "find_tab", return_value=TAB), \
             mock.patch.object(relay, "is_operator_tab", return_value=False), \
             mock.patch.object(relay, "live_row", return_value=ROW), \
             mock.patch.object(relay, "read_screen", return_value=IDLE_SCREEN):
            self.assertEqual(relay.send({"workspace_id": "WS-7"})["reason"], "the prompt is no longer on screen")

    def test_answer_presses_only_a_visible_prompt(self) -> None:
        cmux, keys = fake_cmux([PROMPT_SCREEN, IDLE_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("allow", ref="workspace:7", settle=0)["status"], "answered")
        self.assertEqual(keys.read_text().split(), ["Enter"])

        cmux, keys = fake_cmux([PROMPT_SCREEN, IDLE_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("deny", ref="workspace:7", settle=0)["status"], "answered")
        self.assertEqual(keys.read_text().split(), ["Escape"])

        cmux, keys = fake_cmux([IDLE_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("allow", ref="workspace:7", settle=0)["status"], "no-prompt")
        self.assertFalse(keys.exists())

        moved = PROMPT_SCREEN.replace("❯ 1. Yes", "  1. Yes").replace("   2. No", " ❯ 2. No")
        cmux, keys = fake_cmux([moved])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("allow", ref="workspace:7", settle=0)["status"], "failed")
        self.assertFalse(keys.exists())

        cmux, keys = fake_cmux([PROMPT_SCREEN, PROMPT_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("allow", ref="workspace:7", settle=0)["status"], "unclear")
        self.assertEqual(relay.answer("maybe", ref="workspace:7")["status"], "rejected")

    def test_answer_presses_only_the_dialog_cos_relayed(self) -> None:
        # A late "allow" must not approve the next command
        # after the owner already answered the relayed one in the tab.
        relay._mutate(lambda s: (s["pending"].__setitem__("workspace:7", {"command": "sudo rm -rf /opt/other", "key": "k1"}),
                                 s["sent"].__setitem__("k1", time.time())))
        cmux, keys = fake_cmux([PROMPT_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            result = relay.answer("allow", ref="workspace:7", settle=0)
        self.assertEqual(result["status"], "failed")
        self.assertIn("different command", result["reason"])
        self.assertFalse(keys.exists())

        # The relayed command is on screen: press it, then forget the dedupe so
        # the same command asked again (a retry after deny) is relayed again.
        relay._mutate(lambda s: s["pending"].__setitem__("workspace:7", {"command": "sudo rm -rf /opt/example", "key": "k1"}))
        cmux, keys = fake_cmux([PROMPT_SCREEN, IDLE_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("deny", ref="workspace:7", settle=0)["status"], "answered")
        self.assertEqual(keys.read_text().split(), ["Escape"])
        state = relay._read_state()
        self.assertNotIn("workspace:7", state["pending"])
        self.assertNotIn("k1", state["sent"])

    def test_cursor_on_yes_and_dont_ask_again_is_not_a_plain_yes(self) -> None:
        screen = PROMPT_SCREEN.replace("❯ 1. Yes", "❯ 1. Yes, and don't ask again for rm commands")
        self.assertFalse(relay.YES_SELECTED_RE.search(screen))
        self.assertTrue(relay.YES_SELECTED_RE.search(" │ ❯ 1. Yes │\n"))
        self.assertTrue(relay.command_on_screen("sudo rm -rf /opt/example\nsecond line", PROMPT_SCREEN))
        self.assertFalse(relay.command_on_screen("", PROMPT_SCREEN))

    def test_dialog_text_printed_higher_up_is_not_a_prompt(self) -> None:
        old = PROMPT_SCREEN + "\n".join(f"output line {i}" for i in range(40)) + "\n" + IDLE_SCREEN
        self.assertFalse(relay.prompt_visible(old))

    def sweep_with(self, rows: list[dict], screens: dict[str, str], transcript: str = "") -> tuple[list[dict], list[tuple[str, str]]]:
        sent: list[tuple[str, str]] = []
        with mock.patch.object(relay, "read_screen", side_effect=lambda ref: screens.get(ref)), \
             mock.patch.object(relay, "find_tab", side_effect=lambda workspace_id="", ref="": {**TAB, "ref": ref}), \
             mock.patch.object(relay, "is_operator_tab", return_value=False), \
             mock.patch.object(relay, "live_row", side_effect=lambda session_id="", task_id="", ref="": next((r for r in rows if r["workspace_ref"] == ref), {})), \
             mock.patch.object(relay, "transcript_for_session", return_value=transcript), \
             mock.patch.object(relay, "_cos_ref", return_value="workspace:1"), \
             mock.patch.object(relay.owner_tabs, "owner_name", return_value="Photo Book Kit"), \
             mock.patch.object(relay.owner_tabs, "agent_message", side_effect=lambda ref, text, sender: sent.append((ref, text)) or {"ok": True}):
            return relay.sweep(rows), sent

    def test_sweep_relays_a_worker_tab_opened_before_the_hook(self) -> None:
        # A CoS steer: a worker tab sat on a delete confirm, and CoS heard only
        # "waiting on a question". Its session predates the Notification hook, so the
        # sweep reads the tab and the transcript instead.
        command = "cd /Users/tester/switchboard/.worktrees/rt-000000000050 && rm -rf .amp/in/rt-000000000050/kit-render"
        transcript = Path(TMP.name) / "kit.jsonl"
        transcript.write_text(json.dumps({"message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}}) + "\n")
        kit = {"id": "rt-000000000050", "status": "active", "workstream": "photo-book-kit",
               "workspace_ref": "workspace:41", "session_id": "kit-session"}
        done = {**kit, "id": "rt-000000000045", "status": "done", "workspace_ref": "workspace:9"}
        owner = {**kit, "id": "rt-000000000051", "workspace_ref": "owner-tab"}
        idle = {**kit, "id": "rt-000000000052", "workspace_ref": "workspace:10"}
        screen = PROMPT_SCREEN.replace("sudo rm -rf /opt/example", command)
        screens = {"workspace:41": screen, "workspace:9": screen, "owner-tab": screen, "workspace:10": IDLE_SCREEN}
        results, sent = self.sweep_with([kit, done, owner, idle], screens, str(transcript))
        self.assertEqual([r["task_id"] for r in results], ["rt-000000000050"])
        self.assertTrue(results[0]["sent"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "workspace:1")
        self.assertIn("[ROUTER REPORT] 🔐", sent[0][1])
        self.assertIn(command, sent[0][1])
        self.assertIn("--steer rt-000000000050 allow", sent[0][1])
        self.assertTrue(relay.relayed("workspace:41"))
        self.assertFalse(relay.relayed("workspace:10"))

        # The next tick sees the same dialog: CoS is not told twice.
        results, sent = self.sweep_with([kit], screens, str(transcript))
        self.assertFalse(results[0]["sent"])
        self.assertEqual(sent, [])

    def test_sweep_respects_the_kill_switch(self) -> None:
        with mock.patch.dict(os.environ, {"PERMISSION_RELAY_DISABLED": "1"}), \
             mock.patch.object(relay, "read_screen") as read:
            self.assertEqual(relay.sweep([ROW]), [])
        read.assert_not_called()

    def test_notify_respects_the_kill_switch_and_types(self) -> None:
        with mock.patch.dict(os.environ, {"PERMISSION_RELAY_DISABLED": "1"}):
            self.assertTrue(relay.disabled())
        self.assertNotIn("idle_prompt", relay.RELAY_TYPES)

    def test_notify_outside_cmux_spawns_nothing(self) -> None:
        # switchboard Kit (Windows, Linux, a plain terminal): no CMUX_WORKSPACE_ID means no
        # tab to read and no CoS to tell, so the hook returns before it detaches a sender.
        env = {k: v for k, v in os.environ.items() if k not in ("CMUX_WORKSPACE_ID", "PERMISSION_RELAY_DISABLED")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(relay, "disabled", return_value=False), \
             mock.patch.object(relay, "_detach") as detach, \
             mock.patch.object(sys, "argv", ["permission_relay.py", "notify"]), \
             mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"notification_type": "permission_prompt"}))):
            self.assertEqual(relay.main(), 0)
        detach.assert_not_called()


TALL = json.loads((SCRIPTS / "fixtures" / "permission_relay_tall_command.json").read_text(encoding="utf-8"))
BOLT = {"id": "rt-000000000053", "status": "active", "workstream": "switchboard-maintenance",
        "workspace_ref": "workspace:42", "session_id": "00000000-0000-4000-8000-0000000000b1"}


def write_session(root: Path, main_rows: list[dict], sub_rows: list[dict], meta: dict) -> Path:
    """A Claude session laid out on disk: <id>.jsonl plus <id>/subagents/agent-*.jsonl and .meta.json."""
    main = root / f"{BOLT['session_id']}.jsonl"
    main.write_text("".join(json.dumps(r) + "\n" for r in main_rows))
    sub = root / BOLT["session_id"] / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-a000000000000b001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sub_rows))
    (sub / "agent-a000000000000b001.meta.json").write_text(json.dumps(meta))
    return main


class TallSubagentCommandTest(unittest.TestCase):
    """CoS relay gap: a worker's 🔐 callback said only
    "Prompt: Do you want to proceed?". The Bash call came from its background
    code-reviewer subagent, and the command was taller than the tab, so the screen
    held only its last lines. The relay now reads subagent transcripts and keeps the
    guardrail's ask record while its command is still in the dialog."""

    def setUp(self) -> None:
        for path in (relay._state_path(), relay.STATE_DIR / "worker-dispatch" / "permission-asks" / f"{BOLT['session_id']}.json"):
            if path.exists():
                path.unlink()
        self.root = Path(tempfile.mkdtemp(dir=TMP.name))
        main_rows = [
            {"timestamp": "2026-01-01T00:51:29.745Z", "message": {"content": [
                {"type": "tool_use", "id": "m1", "name": "Bash", "input": {"command": "date"}}]}},
            {"timestamp": "2026-01-01T00:51:30.597Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "m1"}]}},
            # The background Agent launch resolves at once; its subagent keeps running.
            {"timestamp": "2026-01-01T00:51:31.000Z", "message": {"content": [
                {"type": "tool_use", "id": "toolu_01AAAAAAAAAAAAAAAAAAAAAA", "name": "Agent", "input": {"subagent_type": "switchboard:code-reviewer"}}]}},
            {"timestamp": "2026-01-01T00:51:31.200Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_01AAAAAAAAAAAAAAAAAAAAAA"}]}},
        ]
        sub_rows = [
            {"timestamp": "2026-01-01T00:51:37.415Z", "message": {"content": [
                {"type": "tool_use", "id": "s1", "name": "Bash", "input": {"command": "sed -n 2038,2040p wrapper"}}]}},
            {"timestamp": "2026-01-01T00:51:38.052Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "s1"}]}},
            {"timestamp": "2026-01-01T00:52:10.687Z", "message": {"content": [
                {"type": "tool_use", "id": "s2", "name": "Bash", "input": {"command": TALL["command"]}}]}},
        ]
        self.transcript = write_session(self.root, main_rows, sub_rows,
                                        {"agentType": TALL["agent_type"], "description": TALL["agent_description"]})

    def relay_bolt(self, screen: str, ask: dict | None = None) -> list[tuple[str, str]]:
        if ask is not None:
            asks = relay.STATE_DIR / "worker-dispatch" / "permission-asks"
            asks.mkdir(parents=True, exist_ok=True)
            (asks / f"{BOLT['session_id']}.json").write_text(json.dumps(ask))
        sent: list[tuple[str, str]] = []
        with mock.patch.object(relay, "read_screen", return_value=screen), \
             mock.patch.object(relay, "find_tab", return_value={**TAB, "ref": BOLT["workspace_ref"], "title": "🛠️ Switchboard Maintenance 2"}), \
             mock.patch.object(relay, "is_operator_tab", return_value=False), \
             mock.patch.object(relay, "live_row", return_value=BOLT), \
             mock.patch.object(relay, "transcript_for_session", return_value=str(self.transcript)), \
             mock.patch.object(relay, "_cos_ref", return_value="workspace:40"), \
             mock.patch.object(relay.owner_tabs, "owner_name", return_value="Switchboard Maintenance"), \
             mock.patch.object(relay.owner_tabs, "agent_message", side_effect=lambda ref, text, sender: sent.append((ref, text)) or {"ok": True}):
            results = relay.sweep([BOLT])
        self.assertTrue(results and results[0]["sent"], results)
        return sent

    def test_the_screen_holds_only_the_tail(self) -> None:
        self.assertTrue(relay.prompt_visible(TALL["screen"]))
        self.assertNotIn("S=/private/tmp", TALL["screen"])
        self.assertTrue(relay.command_on_screen(TALL["command"], TALL["screen"]))
        self.assertFalse(relay.command_on_screen("rm -rf /opt/other\necho done", TALL["screen"]))

    def test_sweep_relays_the_full_subagent_command_verbatim(self) -> None:
        sent = self.relay_bolt(TALL["screen"])
        self.assertEqual(len(sent), 1)
        text = sent[0][1]
        self.assertIn(f"```\n{TALL['command']}\n```", text)
        self.assertIn("S=/private/tmp/claude-501/", text)
        self.assertIn("Asked by: the switchboard:code-reviewer subagent (Code review cmux heartbeat hook)", text)
        self.assertIn("--steer rt-000000000053 allow", text)
        self.assertNotIn("not found", text)
        pending = relay._read_state()["pending"][BOLT["workspace_ref"]]
        self.assertEqual(pending["command"], TALL["command"])

    def test_a_record_older_than_two_minutes_still_gives_the_guardrail_reason(self) -> None:
        # The sweep runs every 10 minutes; the record is 5 minutes old but its command is on screen.
        at = datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat().replace("+00:00", "Z")
        ask = {"session_id": BOLT["session_id"], "tool": "Bash", "command": TALL["command"], "at": at,
               "agent_type": TALL["agent_type"],
               # The reason the pre-landing bridge gave for this exact command.
               "reason": f"switchboard-guardrails: confirm rm -rf\n\nAllow destructive delete of $S/st, $S/hang?\n\n{TALL['command']}"}
        text = self.relay_bolt(TALL["screen"], ask)[0][1]
        self.assertIn("Guardrail reason: switchboard-guardrails: confirm rm -rf. Allow destructive delete of $S/st, $S/hang?\n", text)
        self.assertNotIn("Prompt: Do you want to proceed?", text)
        self.assertIn(TALL["command"], text)
        self.assertIn("Asked by: the switchboard:code-reviewer subagent", text)

    def test_a_record_for_another_command_is_ignored(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ask = {"session_id": BOLT["session_id"], "tool": "Bash", "command": "sudo rm -rf /opt/elsewhere", "at": now,
               "reason": "Allow destructive delete of /opt/elsewhere?"}
        text = self.relay_bolt(TALL["screen"], ask)[0][1]
        self.assertNotIn("/opt/elsewhere", text)
        self.assertIn(TALL["command"], text)

    def test_no_command_found_says_so(self) -> None:
        self.transcript.write_text("")
        for sub in (self.root / BOLT["session_id"] / "subagents").glob("*.jsonl"):
            sub.write_text("")
        text = self.relay_bolt(TALL["screen"])[0][1]
        self.assertIn("Command: not found in the guardrail record or the session and subagent transcripts", text)

    def test_allow_presses_a_tall_relayed_command(self) -> None:
        # The first line scrolled off; the last line still matches, so the press goes through.
        relay._mutate(lambda s: s["pending"].__setitem__(BOLT["workspace_ref"], {"command": TALL["command"], "key": "k2"}))
        cmux, keys = fake_cmux([TALL["screen"], IDLE_SCREEN])
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}):
            self.assertEqual(relay.answer("allow", ref=BOLT["workspace_ref"], settle=0)["status"], "answered")
        self.assertEqual(keys.read_text().split(), ["Enter"])

    def test_a_tool_use_written_again_after_its_result_stays_closed(self) -> None:
        # Seen live at 04:58:52Z: the subagent transcript repeats the tool_use row
        # (no timestamp) after its tool_result. That call ran; it is not open.
        path = Path(TMP.name) / "rewrite.jsonl"
        use = {"type": "tool_use", "id": "r1", "name": "Bash", "input": {"command": "rm -rf $S/st"}}
        path.write_text("".join(json.dumps(r) + "\n" for r in [
            {"timestamp": "2026-01-01T00:58:43.406Z", "message": {"content": [use]}},
            {"timestamp": "2026-01-01T00:58:52.041Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "r1"}]}},
            {"message": {"content": [use]}}]))
        self.assertEqual(relay.pending_tool_calls(str(path)), [])

    def test_newest_open_call_wins_across_main_and_subagents(self) -> None:
        call = relay.transcript_tool_call(str(self.transcript))
        self.assertEqual(call["command"], TALL["command"])
        self.assertEqual(call["agent"], "the switchboard:code-reviewer subagent (Code review cmux heartbeat hook)")
        self.assertEqual(relay.transcript_tool_call(str(self.transcript), screen=PROMPT_SCREEN)["command"], TALL["command"])


class PermissionCaseTests(unittest.TestCase):
    """Rule: CoS may allow a prompt whose only destructive part deletes or
    overwrites files the same task made; every other prompt stays the owner's."""

    def setUp(self) -> None:
        import subprocess
        self.root = Path(tempfile.mkdtemp(dir=TMP.name))
        self.home = self.root / "home"
        (self.home / "Downloads").mkdir(parents=True)
        (self.home / ".config").mkdir()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.wt = self.root / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        (self.wt / "tracked.md").write_text("x")
        subprocess.run(["git", "-C", str(self.wt), "add", "tracked.md"], check=True)
        self.old = self.home / "Downloads" / "old-plan.pdf"
        self.old.write_text("old")
        self.config = self.home / ".config" / "app.json"
        self.config.write_text("{}")
        time.sleep(1.1)   # creation times have one-second grain on some file systems
        self.started = time.time()
        self.row = {"id": "rt-000000000007", "worktree": str(self.wt),
                    "created_at": datetime.fromtimestamp(self.started - 0.5, timezone.utc).isoformat().replace("+00:00", "Z")}
        time.sleep(1.1)
        self.shots = self.home / "Downloads" / "review" / "shots"
        self.shots.mkdir(parents=True)
        for name in ("a.png", "b.png"):
            (self.shots / name).write_text("png")
            (self.tmp / name).write_text("png")
        (self.wt / ".amp" / "in").mkdir(parents=True)
        (self.wt / ".amp" / "in" / "scratch.txt").write_text("s")

    def case(self, command: str, tool: str = "Bash") -> dict:
        import permission_case
        if tool == "Bash" and os.name == "nt":
            command = command.replace("\\", "/")   # Git Bash commands use forward slashes
        return permission_case.classify(tool, command, row=self.row, cwd=str(self.wt), home=str(self.home),
                                        temp_roots=[str(self.tmp)])

    def test_the_screenshot_command_is_own_task_files(self) -> None:
        # Variables set in the command,
        # a fresh screenshot folder under ~/Downloads, PNGs copied from a temp folder.
        command = (f"D={self.shots}; T={self.tmp}; mkdir -p $D; rm -f $D/*.png; cp $T/*.png $D/ && chmod 600 $D/*.png "
                   f"&& ls $D; ls -la {self.wt}/.amp/in/ 2>/dev/null; cmux --help 2>/dev/null | grep -i lease | head -3")
        verdict = self.case(command)
        self.assertEqual(verdict["case"], "own-task-files", verdict["reason"])
        self.assertIn(str((self.shots / "a.png").resolve()), verdict["targets"])

    def test_own_places_qualify(self) -> None:
        self.assertEqual(self.case(f"rm -f {self.tmp}/a.png")["case"], "own-task-files")
        self.assertEqual(self.case(f"git branch --list; git log -1; rm -f {self.tmp}/a.png")["case"], "own-task-files")
        self.assertEqual(self.case(f"rm -rf {self.tmp}/a.png && cp -fp {self.tmp}/b.png {self.tmp}/c.png "
                                   f"&& chmod 600 {self.tmp}/b.png")["case"], "own-task-files")
        self.assertEqual(self.case(f"D={self.tmp}; rm -f ${{D}}/a.png 2>&1 >&- | head -1")["case"], "own-task-files")
        self.assertEqual(self.case("rm -rf .amp/in/scratch.txt")["case"], "own-task-files")
        self.assertEqual(self.case(f"rm -rf {self.home}/Downloads/review")["case"], "own-task-files")
        self.assertEqual(self.case(f"echo hi > {self.tmp}/b.png")["case"], "own-task-files")
        self.assertEqual(self.case(json.dumps({"file_path": str(self.shots / "a.png"), "content": "x"}), tool="Write")["case"],
                         "own-task-files")

    def test_everything_else_is_the_owners(self) -> None:
        cases = {
            "rm -f tracked.md": "tracked",
            f"rm -f {self.old}": "existed before",
            f"rm -rf {self.home}/Downloads": "outside",
            f"rm -f {self.config}": "outside",
            f"cp {self.tmp}/a.png {self.config}": "outside",
            "rm -f $UNSET/a.png": "not set",
            "rm -f $(ls)": "substitution",
            f"sudo rm -f {self.tmp}/a.png": "sudo",
            f"curl -X POST https://example.com; rm -f {self.tmp}/a.png": "curl",
            f"ls {self.tmp}": "deletes or overwrites no task files",
            f"(rm -f {self.tmp}/a.png)": "subshell",
            f"find {self.tmp} -name '*.png' -exec rm {{}} +": "brace",
            f"find {self.tmp} -name '*.png' -exec rm +": "find",
            f"find {self.tmp} -name '*.png' -exec rm ';'": "quotes ';'",
            # A path-qualified or env-prefixed command, and read-only
            # tools with a write or run flag, must never pass as own task files.
            f"{self.tmp}/ls; rm -f {self.tmp}/a.png": "is a path",
            f"./rm -f {self.tmp}/a.png": "is a path",
            f"PATH={self.tmp} rm -f {self.tmp}/a.png": "changes what a command runs",
            f"LD_PRELOAD={self.tmp}/x.so rm -f {self.tmp}/a.png": "changes what a command runs",
            f"X=1 rm -f {self.tmp}/a.png": "set for the command itself",
            f"rg --pre {self.tmp}/b.png x .; rm -f {self.tmp}/a.png": "`rg` is not",
            f"tree -o {self.config}; rm -f {self.tmp}/a.png": "`tree` is not",
            f"uniq {self.tmp}/b.png {self.config}; rm -f {self.tmp}/a.png": "`uniq` is not",
            f"git log --output={self.config}; rm -f {self.tmp}/a.png": "writes a file or runs a program",
            f"git diff --ext-diff; rm -f {self.tmp}/a.png": "writes a file or runs a program",
            f"git branch -D main; rm -f {self.tmp}/a.png": "does more than list",
            f"git -C {self.wt} status; rm -f {self.tmp}/a.png": "not a plain read",
            f"date -s 2020-01-01; rm -f {self.tmp}/a.png": "writes or changes something",
            # A flag with a value hid the real target from the operand parse.
            f"cp -t {self.home}/.config {self.tmp}/a.png {self.tmp}/b.png": "not a flag the own-task-files case reads",
            f"cp --target-directory={self.home}/.config {self.tmp}/a.png {self.tmp}/b.png": "not a flag",
            f"cp -l {self.config} {self.tmp}/new.json": "not a flag",
            f"mv -T {self.tmp}/a.png {self.tmp}/b.png": "not a flag",
            f"touch -r {self.config} {self.tmp}/a.png": "not a flag",
            f"chmod --reference={self.config} {self.tmp}/a.png": "not a flag",
            f"chmod {self.tmp}/a.png {self.tmp}/b.png": "without a plain mode",
            f"rmdir -p {self.tmp}/x": "not a flag",
            # Third pass: brace and $-forms the parser cannot see, find options, and `>&` writes.
            f"rm -rf {{{self.config},{self.tmp}/a.png}}": "brace",
            f"rm -f $1/a.png {self.tmp}/a.png": "shell variable",
            f"rm -f $'{self.config}' {self.tmp}/a.png": "ANSI-C",
            f"find -L {self.home} -name '*.json' -delete; rm -f {self.tmp}/a.png": "options before",
            f"echo x >& {self.config}; rm -f {self.tmp}/a.png": "outside",
            f"cat <> {self.config}; rm -f {self.tmp}/a.png": "opens a file for writing",
            # Fourth pass: identify deletes an ephemeral: input, and .. after a symlink lands elsewhere.
            f"identify ephemeral:{self.config}; rm -f {self.tmp}/a.png": "`identify` is not",
            f"rm -f {self.tmp}/../tmp/a.png": "goes up a folder",
            f"cd {self.tmp}/..; rm -f tmp/a.png": "goes up a folder",
            # Fifth pass: a # comment swallowed every later line, which bash still runs.
            f"rm -f {self.tmp}/a.png # tidy\nrm -f {self.config}": "holds a #",
            # Operands bash reads differently, and punctuation shlex groups.
            f"rm -rf {self.tmp}/a.png ~+": "~ form",
            f"X='{self.home}/Dropbox x'; rm -rf {self.tmp}/a.png $X": "spaces or glob",
            f"cd {self.tmp}/nope; rm -rf {self.tmp}/a.png": "goes to no folder",
            f"rm -f {self.tmp}/a.png; echo x;>{self.config}": "punctuation",
            f"rm -f {self.tmp}/a.png {self.tmp}/missing.png": "does not exist",
            f"HOME={self.tmp}; rm -rf {self.tmp}/a.png": "changes what a command runs",
            f"chmod 600 {self.tmp}/missing.png; rm -f {self.tmp}/a.png": "does not exist",
            f"touch {self.home}/.config/new.json; rm -f {self.tmp}/a.png": "outside",
        }
        for command, why in cases.items():
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn(why, verdict["reason"], command)
        import permission_case
        self.assertEqual(permission_case.classify("Bash", f"rm -f {self.tmp}/a.png", row={})["case"], "ask")
        fwd = lambda p: str(p).replace("\\", "/")  # noqa: E731  (Git Bash form, so only the continuation is a backslash)
        joined = permission_case.classify("Bash", f"rm -f {fwd(self.tmp)}/a.png \\\n{fwd(self.config)}", row=self.row,
                                          cwd=str(self.wt), home=str(self.home), temp_roots=[str(self.tmp)])
        self.assertEqual((joined["case"], "continues a line" in joined["reason"]), ("ask", True), joined["reason"])
        self.assertEqual(self.case(json.dumps({"file_path": str(self.wt / "new.md"), "content": "x"}), tool="Write")["case"], "ask")
        dotdot = self.case(json.dumps({"file_path": f"{self.shots}/../shots/a.png", "content": "x"}), tool="Write")
        self.assertEqual((dotdot["case"], "goes up a folder" in dotdot["reason"]), ("ask", True))

    def test_a_cd_or_assignment_bash_may_skip_or_run_in_a_subshell_is_the_owners(self) -> None:
        # Bash skips a cd after a failed && and runs one in
        # a subshell in a pipeline or behind &, so `rm -rf *` runs in the worktree, not the temp folder.
        t = self.tmp
        cases = {
            f"false && cd {t}; rm -rf *": "follows `&&`",
            f"cd {t} | true; rm -rf *": "joined by `|`",
            # Variants from the security review of the fix.
            f"cd {t} || true; rm -rf *": "joined by `||`",
            f"true || cd {t}; rm -rf *": "follows `||`",
            f"cd {t} & rm -rf *": "joined by `&`",
            f"sleep 0 & cd {t}; rm -rf *": "follows `&`",
            f"cd {t} |& true; rm -rf *": "joined by `|&`",
            f"true | cd {t}; rm -rf *": "follows `|`",
            f"cd {t} && ls | wc -l & rm -rf *": "goes on to `&`",
            f"cd {t} && ls {t}/a.png || rm -rf *": "goes on to `||`",
            f"false &&\ncd {t}\nrm -rf *": "follows `&&`",
            f"false && D={t}; rm -rf $D/a.png": "`D=` follows `&&`",
            f"D={t} | true; rm -rf $D/a.png": "`D=` is joined by `|`",
            f"D={t} & rm -rf $D/a.png": "`D=` is joined by `&`",
            f"cd {t} extra; rm -rf *": "not a plain cd",
            f"cd -P {t}; rm -rf *": "not a plain cd",
            "cd -; rm -rf *": "not a plain cd",
            "cd help; rm -rf a.png": "CDPATH",
            "cd tmp; rm -rf a.png": "CDPATH",
            "rm help; rm -f .amp/in/scratch.txt": "tracked",
            f"GLOBIGNORE=x; rm -rf {t}/*": "changes what a command runs",
            f"OLDPWD={t}; rm -rf {t}/a.png": "changes what a command runs",
            f"rm -f {t}/a.png\n&& rm -f {t}/b.png": "joins commands",
        }
        (self.wt / "help").write_text("tracked")
        import subprocess
        subprocess.run(["git", "-C", str(self.wt), "add", "help"], check=True)
        for command, why in cases.items():
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn(why, verdict["reason"], command)
        # A cd or an assignment that always runs in this shell still counts.
        for command in (f"cd {t}; rm -f *.png", f"cd {t} && rm -f a.png && ls", f"cd {t}\nrm -f a.png",
                        f"D={t} && rm -f $D/a.png", f"cd {t} && rm -f a.png; ls | wc -l",
                        "cd ./.amp/in && rm -f scratch.txt"):
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "own-task-files", (command, verdict["reason"]))

    def test_what_bash_reads_differently_from_the_check_is_the_owners(self) -> None:
        # Each of these could read as own-task-files while bash deletes
        # or overwrites something the check never looked at.
        import permission_case
        t, wt = self.tmp, self.wt
        (t / "d").mkdir()
        (t / "d" / "x.png").write_text("png")
        (t / "src").mkdir()
        (t / "src" / "spec.md").write_text("new")
        (wt / "docs").mkdir()
        (wt / "docs" / "spec.md").write_text("old")
        import subprocess
        subprocess.run(["git", "-C", str(wt), "add", "docs/spec.md"], check=True)
        cases = {
            f"rm -f {t}/a.png ';' ls {wt}/tracked.md": "quotes ';'",            # 1: bash passes ; ls to rm
            f"rm -f {t}/a.png '<' {wt}/tracked.md": "quotes '<'",               # 2: and < as a file name
            f"cd {t}/d ';'; rm -rf *": "quotes ';'",                            # 3: cd fails, rm runs here
            f'rm -f {t}/a.png "|" {wt}/tracked.md': 'quotes \'|\'',
            f"rm -f '{t}/a.png\nls' {wt}/tracked.md": "quotes '\\n'",
            f"D={t}/a.png; rm -f '$D'": "quotes '$'",                           # bash deletes a file named $D
            f"rm -rf {t}/d; cd {t}/d; rm -rf *": "after a command that changes files",   # 4
            f"mv {t}/d {t}/e; cd {t}/d; rm -rf *": "after a command that changes files",
            f"chmod 000 {t}/d; cd {t}/d; rm -rf *": "after a command that changes files",
            f"echo x > {t}/a.png; cd {t}/d; rm -rf *": "after a command that changes files",
            f"D={t}/a.png; printf -v D tracked.md; rm -f $D": "printf -v",      # 5
            f"D={t}/a.png; printf -vD tracked.md; rm -f $D": "printf -vD",
            "rm -f [^x]* .amp/in/scratch.txt": "[...] pattern",                 # 6
            "rm -f [[:alpha:]]* .amp/in/scratch.txt": "[...] pattern",          # 7
            "rm -f [!x]* .amp/in/scratch.txt": "[...] pattern",
            f"cp -R {t}/src/ {wt}/docs/ && rm -f {t}/a.png": "ends in a slash",  # 8: BSD cp copies contents
            f"cp -R {t}/src/. {wt}/docs && rm -f {t}/a.png": "ends in a slash or a dot",
            f"mv {t}/src/ {t}/d": "ends in a slash",
            f"find {t}/d -follow -delete": "-follow",                           # plausible: follows links out
            f"cd {t}/d\r\nrm -rf *": "carriage return",                         # plausible: macOS keeps \r
        }
        for command, why in cases.items():
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn(why, verdict["reason"], command)
        # Backslash escapes, sent as written (case() turns backslashes into slashes on Windows).
        for command, why in ((f"rm -f {t.as_posix()}/a.png \\; ls tracked.md", "escapes ';'"),
                             (f"D={t.as_posix()}/a.png; rm -f \\$D", "escapes '$'")):
            verdict = permission_case.classify("Bash", command, row=self.row, cwd=str(wt), home=str(self.home),
                                               temp_roots=[str(t)])
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn(why, verdict["reason"], command)
        # `NAME --help` runs NAME: a program the task made gives ask, a name found nowhere does not.
        bindir = t / "bin"
        bindir.mkdir()
        (bindir / "mytool").write_text("#!/bin/sh\nrm -rf ~\n")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bindir) + os.pathsep + old_path
        try:
            verdict = self.case(f"rm -f {t}/a.png; mytool --help")
            self.assertEqual((verdict["case"], "may have made" in verdict["reason"]), ("ask", True), verdict)
            verdict = self.case(f"rm -f {t}/a.png; no-such-tool-rt43927 --help")
            self.assertEqual(verdict["case"], "own-task-files", verdict["reason"])
        finally:
            os.environ["PATH"] = old_path
        if os.name == "nt":
            verdict = self.case(f"rm -f {t}/a.png {wt}/TRACKE~1.MD")
            self.assertEqual((verdict["case"], "short or trimmed name" in verdict["reason"]), ("ask", True), verdict)
        # Plain quoting, a cd before any change, and a cp of a named folder still count.
        for command in (f"rm -f '{t}/a.png' \"{t}/b.png\"", f"cd {t}; rm -f a.png; ls",
                        f"cp -R {t}/src {t}/d && rm -f {t}/a.png", f'D={t}; rm -f "$D/a.png"'):
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "own-task-files", (command, verdict["reason"]))

    def test_the_second_reviews_variants_are_the_owners(self) -> None:
        # More forms that could read as own-task-files.
        import permission_case
        t, wt = self.tmp, self.wt
        for name in ("ab", "ac"):
            (t / name).mkdir()
        (wt / "2").write_text("tracked")
        import subprocess
        subprocess.run(["git", "-C", str(wt), "add", "2"], check=True)
        cases = {
            f'"XQ"={t}; rm -rf $XQ/*': "quoted or escaped NAME=",                 # 1: bash runs rm -rf /*
            f"cd ~; cd {t}/a[bc]; rm -rf Documents": "pattern bash expands",       # 2: two matches, cd fails
            f"cd {t}/a?; rm -f x": "pattern bash expands",
            f"rm -f {t}/a.png; echo x | tee trac?ed.md": "pattern bash expands",   # 3: tee hits tracked.md
            f"rm -f {t}/a.png; echo x | tee trac*.md": "pattern bash expands",
            f"mkdir {t}/n*": "pattern bash expands",
            f"rm -f {t}/a.png 2 >/dev/null": "number next to a redirect",          # 4: bash deletes ./2
            f'rm -f {t}/a.png "2">/dev/null': "number next to a redirect",
            f"rm -f {t}/a.png 2&>/dev/null": "number next to a redirect",
            f"rm -f {t}/a.png; shutdown -h": "not a delete",                       # 5: -h is not help
            f"mv {t}/gone {t}/c.png": "does not exist yet",                        # 6: missing mv source
            f'D="~/x"; rm -f {t}/a.png': "quotes '~'",                             # bash keeps ~ literal
        }
        for command, why in cases.items():
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn(why, verdict["reason"], command)
        verdict = permission_case.classify("Bash", f"XQ\\={t.as_posix()}; rm -rf $XQ/*", row=self.row, cwd=str(wt),
                                           home=str(self.home), temp_roots=[str(t)])   # sent as written
        self.assertEqual((verdict["case"], "NAME=" in verdict["reason"]), ("ask", True), verdict)
        # Descriptor redirects still count.
        self.assertEqual(self.case(f"rm -f {t}/a.png 2>/dev/null >&2")["case"], "own-task-files")
        # 7: a hard link made after the task started, pointing at a file outside the task's places.
        outside = self.root / "state.json"
        outside.write_text("{}")
        try:
            os.link(outside, t / "hl.json")
        except OSError:
            self.skipTest("no hard links here")
        for command in (f"echo pwn > {t}/hl.json", f"cp {t}/a.png {t}/hl.json", f"chmod 600 {t}/hl.json"):
            verdict = self.case(command)
            self.assertEqual((verdict["case"], "hard link" in verdict["reason"]), ("ask", True), (command, verdict))
        verdict = self.case(json.dumps({"file_path": str(t / "hl.json"), "content": "x"}), tool="Write")
        self.assertEqual((verdict["case"], "hard link" in verdict["reason"]), ("ask", True), verdict)
        self.assertEqual(self.case(f"rm -f {t}/hl.json")["case"], "own-task-files")   # removing a name is fine
        # 6: cp -R or cp -a of a folder holding a link copies the link (macOS).
        (t / "src").mkdir()
        try:
            os.symlink(str(self.root), str(t / "src" / "people"), target_is_directory=True)
        except OSError:
            return
        for command in (f"cp -R {t}/src {t}/P", f"cp -a {t}/src {t}/P"):
            verdict = self.case(command)
            self.assertEqual((verdict["case"], "link or junction" in verdict["reason"]), ("ask", True), (command, verdict))

    def test_a_symlink_target_or_an_older_file_inside_a_new_folder_is_the_owners(self) -> None:
        link = self.tmp / "link"
        try:
            link.symlink_to(self.config)
        except OSError:   # Windows without Developer Mode cannot make one
            link = None
        if link is not None:
            self.assertIn("symlink", self.case(f"rm -f {link}")["reason"])
        moved = self.shots / "old-plan.pdf"
        import shutil
        shutil.move(str(self.old), str(moved))   # a move keeps the old creation time
        self.assertIn("existed before", self.case(f"rm -rf {self.shots}")["reason"])

    def test_the_message_states_the_case(self) -> None:
        own = {"case": "own-task-files", "reason": "r", "targets": ["/tmp/x/a.png"]}
        with mock.patch.object(relay.owner_tabs, "owner_name", return_value="Switchboard Maintenance"):
            text = relay.build_message(tab=TAB, row=ROW, tool="Bash", command="rm -f /tmp/x/a.png", reason="",
                                       kind="permission_prompt", case=own)
            other = relay.build_message(tab=TAB, row=ROW, tool="Bash", command="sudo rm -rf /opt/example", reason="",
                                        kind="permission_prompt", case={"case": "ask", "reason": "`sudo` needs the owner"})
            amp = relay.build_message(tab=TAB, row=ROW, tool="Bash", command="rm -f /tmp/x/a.png", reason="",
                                      kind="permission_prompt", amp_timeout_s=60, case=own)
        self.assertIn("Case: own task files.", text)
        self.assertIn("--steer rt-000000000007 auto-allow", text)
        self.assertIn("report the auto-allow in your next reply", text)
        self.assertNotIn("never answer it without their word", text)
        self.assertIn("Case: the owner decides (`sudo` needs the owner).", other)
        self.assertIn("never answer it without their word", other)
        self.assertIn("Case: the owner decides", amp)   # an Amp modal is only ever pressed in the tab

    @unittest.skipUnless(os.name == "nt", "a Windows path shape")
    def test_git_bash_paths_translate_before_they_resolve(self) -> None:
        import permission_case
        self.assertEqual(permission_case.native_path("/c/Users/x"), "C:/Users/x")
        self.assertEqual(permission_case.native_path("/c"), "C:/")
        temp = tempfile.gettempdir().replace("\\", "/")
        self.assertEqual(permission_case.native_path("/tmp/x/a.png"), temp + "/x/a.png")
        self.assertEqual(permission_case.native_path("/usr/bin"), "/usr/bin")   # stays outside every place
        self.assertEqual(permission_case.native_path("relative/a.png"), "relative/a.png")

    @unittest.skipUnless(os.name == "nt", "a Windows path shape")
    def test_a_git_bash_drive_path_to_an_own_file_qualifies(self) -> None:
        drive, rest = os.path.splitdrive(str(self.tmp))
        bash = "/" + drive.rstrip(":").lower() + rest.replace("\\", "/")
        verdict = self.case(f"rm -f {bash}/a.png")
        self.assertEqual(verdict["case"], "own-task-files", verdict["reason"])

    @unittest.skipUnless(os.name == "nt", "a Windows path shape")
    def test_a_git_bash_tmp_path_resolves_to_the_windows_temp_folder(self) -> None:
        import permission_case
        made = Path(tempfile.mkdtemp(prefix="switchboard-case-", dir=tempfile.gettempdir()))
        self.addCleanup(lambda: __import__("shutil").rmtree(made, ignore_errors=True))
        (made / "a.png").write_text("png")
        verdict = permission_case.classify("Bash", f"rm -f /tmp/{made.name}/a.png", row=self.row, cwd=str(self.wt),
                                           home=str(self.home))
        self.assertEqual(verdict["case"], "own-task-files", verdict["reason"])

    @unittest.skipUnless(os.name == "nt", "a Windows path shape")
    def test_dropbox_downloads_is_never_own_task_files(self) -> None:
        downloads = self.home / "Dropbox" / "Downloads"
        (downloads / "fresh").mkdir(parents=True)
        (downloads / "fresh" / "a.png").write_text("png")   # made after the task started, still the owner's
        for command in (f"rm -f {downloads}/fresh/a.png", f"rm -rf {downloads}/fresh",
                        f"cp {self.tmp}/a.png {downloads}/fresh/a.png"):
            verdict = self.case(command)
            self.assertEqual(verdict["case"], "ask", command)
            self.assertIn("outside", verdict["reason"], command)

    def test_review_forms_that_need_the_owner(self) -> None:
        # A relative operand with no known shell folder, and Windows-only forms.
        import permission_case
        nocwd = permission_case.classify("Bash", "rm -f a.png", row=self.row, cwd="", home=str(self.home),
                                         temp_roots=[str(self.tmp)])
        self.assertIn("folder is unknown", nocwd["reason"])
        if os.name != "nt":
            return
        self.assertIn("cannot map", self.case(f"rm -rf {self.tmp}/a.png /usr/bin")["reason"])
        self.assertIn("tracked", self.case(f"rm -f {self.tmp}/a.png {self.wt}/TRACKED.MD")["reason"])
        (self.wt / ".git" / "fresh").write_text("x")
        self.assertIn("outside", self.case(f"rm -f {self.tmp}/a.png {self.wt}/.GIT/fresh")["reason"])
        import _winapi
        junction = self.tmp / "junction"
        _winapi.CreateJunction(str(self.home / ".config"), str(junction))
        self.assertIn("junction", self.case(f"rm -rf {self.tmp}/a.png {junction}")["reason"])
        holder = self.tmp / "holder"
        holder.mkdir()
        _winapi.CreateJunction(str(self.home / ".config"), str(holder / "inner"))
        self.assertIn("junction", self.case(f"rm -rf {self.tmp}/a.png {holder}")["reason"])

    @unittest.skipUnless(os.name == "nt", "a Windows path shape")
    def test_a_backslash_path_is_the_owners(self) -> None:
        import permission_case
        verdict = permission_case.classify("Bash", f"rm -f {self.tmp}\\a.png", row=self.row, cwd=str(self.wt),
                                           home=str(self.home), temp_roots=[str(self.tmp)])
        self.assertEqual(verdict["case"], "ask")
        self.assertIn("backslash", verdict["reason"])


class AutoAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        state = relay._state_path()
        if state.exists():
            state.unlink()
        screen = PROMPT_SCREEN.replace("sudo rm -rf /opt/example", "rm -f /tmp/x/a.png")
        self.screens = [screen, IDLE_SCREEN]
        relay._mutate(lambda s: (s["pending"].__setitem__("workspace:7", {
            "command": "rm -f /tmp/x/a.png", "tool": "Bash", "kind": "permission_prompt", "task_id": ROW["id"],
            "key": "k1", "case": "own-task-files"}), s["sent"].__setitem__("k1", time.time())))

    def press(self, verdict: dict, **kw) -> tuple[dict, Path]:
        cmux, keys = fake_cmux(self.screens)
        with mock.patch.dict(os.environ, {"CMUX_BIN": cmux}), \
             mock.patch.object(relay, "live_row", return_value=ROW), \
             mock.patch.object(relay, "find_tab", return_value=TAB), \
             mock.patch.object(relay.permission_case, "classify", return_value=verdict):
            return relay.answer("allow", ref="workspace:7", settle=0, auto=True, **kw), keys

    def test_auto_allow_presses_yes_and_logs_it_for_the_next_reply(self) -> None:
        result, keys = self.press({"case": "own-task-files", "reason": "r", "targets": ["/tmp/x/a.png"]})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(keys.read_text().split(), ["Enter"])
        self.assertIn("Auto-allowed a permission prompt in 🔧 Switchboard Maintenance 2", result["report_line"])
        rows = relay.auto_answers(unreported=True, mark_reported=True)
        self.assertEqual([r["targets"] for r in rows], [["/tmp/x/a.png"]])
        self.assertEqual(relay.auto_answers(unreported=True), [])
        self.assertEqual(len(relay.auto_answers()), 1)
        log = (relay.STATE_DIR / "logs" / "permission-relay.log").read_text()
        self.assertIn('"event": "auto-answer"', log)

    def test_auto_allow_refuses_a_prompt_that_is_not_own_task_files(self) -> None:
        result, keys = self.press({"case": "ask", "reason": "/x existed before the task started", "targets": []})
        self.assertEqual(result["status"], "rejected")
        self.assertIn("ask the owner", result["reason"])
        self.assertFalse(keys.exists())
        self.assertEqual(relay.auto_answers(), [])
        self.assertEqual(relay.answer("deny", ref="workspace:7", auto=True)["status"], "rejected")

    def test_auto_allow_needs_the_whole_command_and_an_own_record(self) -> None:
        # A cut command, a record not sent as own, or a screen that does not show
        # the whole command never auto-allows, even when the case check itself would say own.
        own = {"case": "own-task-files", "reason": "r", "targets": ["/tmp/x/a.png"]}
        for change, why in (({"command_len": 9000}, "was cut"), ({"case": "ask"}, "not send it as own"),
                            ({"command": "rm -f /tmp/x/a.png /tmp/x/b.png"}, "not on screen")):
            relay._mutate(lambda s, c=change: s["pending"]["workspace:7"].update(c))
            result, keys = self.press(own)
            self.assertIn(result["status"], {"rejected", "failed"}, why)   # failed: the prefix check caught it first
            if result["status"] == "rejected":
                self.assertIn(why, result["reason"])
            self.assertFalse(keys.exists(), why)
            self.setUp()


def load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch_under_test", SCRIPTS / "dispatch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class DispatchPermissionSteerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dispatch = load_dispatch()

    def test_only_a_bare_allow_or_deny_is_a_permission_answer(self) -> None:
        d = self.dispatch
        self.assertEqual(d.permission_steer("allow"), "allow")
        self.assertEqual(d.permission_steer(" Deny. "), "deny")
        self.assertEqual(d.permission_steer("'allow'"), "allow")
        self.assertEqual(d.permission_steer("allow the delete and continue"), "")
        self.assertEqual(d.permission_steer("do not allow"), "")
        self.assertEqual(d.permission_steer("auto-allow"), "auto-allow")

    def run_steer(self, row: dict, decision: dict, answer_result: dict | None = None, word: str = "allow") -> dict:
        d = self.dispatch
        args = argparse.Namespace(task_id="rt-000000000007")
        out = io.StringIO()
        with mock.patch.object(relay, "answer", return_value=answer_result or {}) as pressed, \
             mock.patch.object(d, "task_lead", return_value="Switchboard Maintenance (trash rewrite)"), \
             mock.patch.object(d, "log"), redirect_stdout(out):
            d.command_permission_steer(args, row, decision, "switchboard-maintenance", word)
        result = json.loads(out.getvalue())
        result["_pressed"] = pressed.call_args
        return result

    def test_live_row_presses_the_dialog(self) -> None:
        result = self.run_steer(ROW, {"action": "steer"}, {"status": "answered"})
        self.assertEqual((result["status"], result["delivered"], result["workspace_ref"]), ("answered", True, "workspace:7"))
        self.assertIn("allowed", result["message"])
        self.assertEqual(result["_pressed"].args, ("allow",))

    def test_auto_allow_steer_presses_with_the_own_files_check(self) -> None:
        result = self.run_steer(ROW, {"action": "steer"}, {"status": "answered", "report_line": "Auto-allowed X"}, word="auto-allow")
        self.assertEqual(result["_pressed"].args, ("allow",))
        self.assertTrue(result["_pressed"].kwargs["auto"])
        self.assertIn("auto-allowed", result["message"])
        self.assertEqual(result["report_line"], "Auto-allowed X")

    def test_terminal_row_or_no_tab_presses_nothing(self) -> None:
        result = self.run_steer({**ROW, "status": "done"}, {"action": "continuation"})
        self.assertEqual(result["status"], "no-prompt")
        self.assertIsNone(result["_pressed"])
        result = self.run_steer({**ROW, "workspace_ref": ""}, {"action": "steer"})
        self.assertEqual(result["status"], "no-prompt")

    def test_a_press_that_fails_says_why(self) -> None:
        result = self.run_steer(ROW, {"action": "steer"}, {"status": "no-prompt", "reason": "no permission prompt is showing"})
        self.assertFalse(result["delivered"])
        self.assertIn("no permission prompt is showing", result["message"])

    def test_sweep_relays_from_dispatchs_own_ledger(self) -> None:
        d = self.dispatch
        with mock.patch.object(d.ledger, "load_payload", return_value={"tasks": [ROW]}), \
             mock.patch.object(relay, "sweep", return_value=[{"task_id": ROW["id"], "sent": True}]) as swept:
            self.assertEqual(d.relay_permission_prompts(), [{"task_id": ROW["id"], "sent": True}])
        self.assertEqual(swept.call_args.args, ([ROW],))
        with mock.patch.object(d.ledger, "load_payload", return_value={"tasks": []}), \
             mock.patch.object(relay, "sweep", side_effect=RuntimeError("cmux down")), mock.patch.object(d, "log"):
            self.assertEqual(d.relay_permission_prompts(), [])

    def run_nudge(self, relayed: bool) -> tuple[dict, list]:
        d = self.dispatch
        task = {**ROW, "branch": "task/rt-000000000007", "thread_id": "sess-1"}
        notes: list = []
        with mock.patch.object(d.ledger, "load_payload", return_value={"tasks": [task]}), \
             mock.patch.object(d.ledger, "live_agents", return_value={"sess-1": {"state": "Needs input"}}), \
             mock.patch.object(d.ledger, "git_state", return_value="open"), \
             mock.patch.object(d.ledger, "locked_mutation"), \
             mock.patch.object(d, "worker_idle_since", return_value=d.now() - d.timedelta(hours=1)), \
             mock.patch.object(d, "permission_prompt_relayed", return_value=relayed), \
             mock.patch.object(d, "notify", side_effect=lambda *a, **k: notes.append(a)):
            return d.nudge_idle({}), notes

    def test_a_relayed_prompt_replaces_the_generic_waiting_line(self) -> None:
        # CoS steer 4: CoS got "11m waiting on a question" and no command.
        _, notes = self.run_nudge(relayed=True)
        self.assertEqual(notes, [])
        _, notes = self.run_nudge(relayed=False)
        self.assertEqual(len(notes), 1)
        self.assertIn("waiting on a question", notes[0][1])


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TMP.cleanup()
