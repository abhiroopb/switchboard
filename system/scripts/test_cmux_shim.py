#!/usr/bin/env python3
"""Tests for system/scripts/mux/cmux-shim.py against a fake wmux CLI.

The fake wmux (written into a temp dir) records every call it receives and
answers with the JSON shapes wmux 2.49 prints (list-workspaces, new-workspace,
list-surfaces, read-screen, identify). Each test asserts both the cmux-shaped
output switchboard scripts parse and the exact wmux verbs the shim sent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SHIM = HERE / "mux" / "cmux-shim.py"
WRAPPER = HERE / "mux" / "cmux"
# Windows: CreateProcess cannot start the extensionless wrapper, and a bare "bash" can resolve to
# System32's WSL launcher, so name Git Bash from PATH. Elsewhere the wrapper runs directly.
BASH = [shutil.which("bash") or "bash"] if os.name == "nt" else []

FAKE_WMUX = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_WMUX_STATE"]
log_path = os.environ["FAKE_WMUX_LOG"]
state = json.load(open(state_path))
args = sys.argv[1:]
verb = args[0] if args else ""
with open(log_path, "a") as fh:
    entry = list(args)
    if verb == "notify" and os.environ.get("WMUX_SURFACE_ID"):
        entry.append("env:WMUX_SURFACE_ID=" + os.environ["WMUX_SURFACE_ID"])
    fh.write(json.dumps(entry) + "\n")
def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def save():
    json.dump(state, open(state_path, "w"))
if verb == "list-workspaces":
    print(json.dumps({"workspaces": state["workspaces"]}))
elif verb == "new-workspace":
    wid = "ws-%d" % (len(state["workspaces"]) + 1)
    state["workspaces"].append({"id": wid, "title": flag("--title", "Untitled"), "isActive": False,
                                "cwd": flag("--cwd", "C:\\Users\\kit"), "shell": "pwsh"})
    state["surfaces"][wid] = [{"id": "sf-" + wid, "type": "terminal", "paneId": "pane-" + wid, "isActive": True}]
    save(); print(json.dumps({"workspaceId": wid}))
elif verb == "close-workspace":
    state["workspaces"] = [w for w in state["workspaces"] if w["id"] != args[1]]
    state["surfaces"].pop(args[1], None); save(); print(json.dumps({"ok": True}))
elif verb == "select-workspace":
    for w in state["workspaces"]:
        w["isActive"] = w["id"] == args[1]
    save(); print(json.dumps({"ok": True}))
elif verb == "rename-workspace":
    for w in state["workspaces"]:
        if w["id"] == args[1]:
            w["title"] = args[2]
    save(); print(json.dumps({"ok": True}))
elif verb == "list-surfaces":
    wid = flag("--workspace")
    surfaces = state["surfaces"].get(wid, []) if wid else [s for v in state["surfaces"].values() for s in v]
    print(json.dumps({"surfaces": surfaces}))
elif verb == "read-screen":
    if "composer" in state:
        rule = "─" * 40
        composer = state["composer"]
        draft = composer.get("draft", "")
        hint = composer.get("hint", "")
        typed = composer.get("typed", "")
        parked = composer.get("parked", "")     # a real draft whose cursor sits at its start
        shown = typed + parked or draft or composer.get("suggestion", "") or hint
        lines = composer.get("scrollback", []) + [rule, "❯ " + shown, rule, "  ? for shortcuts"]
        reply = {"text": "\n".join(lines), "lines": 1}
        if composer.get("cursor", True):
            # wmux reports the cell cursor: after typed text, right after "❯ " otherwise.
            at_end = draft or typed
            reply.update(rows=len(lines), cursorY=len(lines) - 3, cursorX=2 + len(at_end if at_end else ""))
        print(json.dumps(reply))
    else:
        print(json.dumps({"text": state.get("screen", "amp> \n"), "lines": 1}))
elif verb == "agent-state":
    print(json.dumps({"state": state.get("agent", {"surfaceId": flag("--surface"), "state": "unknown"})}))
elif verb == "send" and "composer" in state:
    text = [a for a in args[1:] if a != "--surface" and a != flag("--surface")]
    typed = " ".join(text)
    composer = state["composer"]
    composer.pop("suggestion", None)    # the first key dismisses it
    if composer.get("parked"):
        composer["typed"] = composer.get("typed", "") + typed
    else:
        composer["draft"] = composer.get("draft", "") + typed
    save(); print(json.dumps({"ok": True}))
elif verb == "send-key" and "composer" in state and "backspace" in args:
    state.setdefault("backspaces", 0)
    state["backspaces"] += 1
    if state["composer"].get("typed"):
        state["composer"]["typed"] = state["composer"]["typed"][:-1]
    save(); print(json.dumps({"ok": True}))
elif verb == "send-key" and "composer" in state and "enter" in args:
    composer = state["composer"]
    if composer.get("typed") or composer.get("parked"):
        # Enter takes the whole box: this is the clobber the shim must never cause.
        state.setdefault("submitted", []).append(composer.pop("typed", "") + composer.pop("parked", ""))
    elif composer.get("suggestion") and not composer.get("draft"):
        # Enter on an empty box accepts the suggestion, as if the owner had typed it.
        state.setdefault("submitted", []).append(composer.pop("suggestion"))
    elif composer.get("draft") and not composer.get("stuck"):
        state.setdefault("submitted", []).append(composer["draft"])
        composer["draft"] = ""
    save(); print(json.dumps({"ok": True}))
elif verb == "prompts" and "prompts" in state:
    print(json.dumps({"enabled": True, "truncated": False, "prompts": state["prompts"]}))
elif verb == "send-key" and "prompts" in state and "enter" in args:
    # The prompt log gains an entry only on the Enter that is not lost.
    state["enters"] = state.get("enters", 0) + 1
    if state["enters"] >= state.get("accept_on_enter", 1):
        state["prompts"].append({"seq": len(state["prompts"]) + 1, "source": "shell", "text": "line"})
    save(); print(json.dumps({"ok": True}))
elif verb == "identify":
    print(json.dumps({"name": "wmux", "version": "2.49.0", "platform": "win32"}))
elif verb == "ping":
    print("pong")
elif verb == "current-workspace":
    if os.environ.get("FAKE_CURRENT_NEEDS_SURFACE") and "--surface" not in sys.argv:
        print("{}")
    else:
        print(json.dumps({"workspaceId": os.environ.get("FAKE_CURRENT", "ws-1")}))
elif verb == "new-surface":
    kind = flag("--type", "terminal")
    wid = flag("--workspace") or next((w["id"] for w in state["workspaces"] if w.get("isActive")), "ws-1")
    n = sum(len(v) for v in state["surfaces"].values()) + 1
    sid = "sf-%s-%d" % ("md" if kind == "markdown" else kind, n) if kind != "markdown" else "sf-md-1"
    state["surfaces"].setdefault(wid, []).append({"id": sid, "type": kind, "paneId": "pane-" + sid, "isActive": False})
    save(); print(json.dumps({"surfaceId": sid, "paneId": "pane-" + sid}))
elif verb in ("send", "send-key", "set-status", "report-agent", "log", "notify", "markdown", "rename-surface", "close-surface", "browser", "open"):
    print(json.dumps({"ok": True}))
else:
    print("unknown verb " + verb, file=sys.stderr); sys.exit(2)
'''


class ShimHarness:
    def __init__(self, tmp: Path, workspaces=None):
        self.tmp = tmp
        # wmux.py, not wmux: Windows cannot start a shebang script, so the shim runs a .py WMUX_BIN with Python.
        self.fake = tmp / "wmux.py"
        self.fake.write_text(FAKE_WMUX)
        self.fake.chmod(0o755)
        self.state = tmp / "fake-state.json"
        self.log = tmp / "fake-log.jsonl"
        self.state.write_text(json.dumps({
            "workspaces": workspaces if workspaces is not None else [
                {"id": "ws-1", "title": "🧭 CoS", "isActive": True, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-2", "title": "🚀 Launchpad", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
            ],
            "surfaces": {"ws-1": [{"id": "sf-1", "type": "terminal", "paneId": "pane-1", "isActive": True}],
                         "ws-2": [{"id": "sf-2", "type": "terminal", "paneId": "pane-2", "isActive": True}]},
            "screen": "amp> \n",
        }))
        self.log.write_text("")
        self.sidecar = tmp / "sidecar.json"
        self.session = tmp / "session-store.json"

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CMUX_", "WMUX", "SWITCHBOARD_"))}
        env.update({"WMUX_BIN": str(self.fake), "FAKE_WMUX_STATE": str(self.state), "FAKE_WMUX_LOG": str(self.log),
                    "SWITCHBOARD_WMUX_SIDECAR": str(self.sidecar), "SWITCHBOARD_CMUX_SESSION_JSON": str(self.session),
                    "HOME": str(self.tmp), "SWITCHBOARD_PYTHON": sys.executable,
                    "SWITCHBOARD_TEST_SANDBOX": "1", "SWITCHBOARD_TEST_WMUX_BIN": str(self.fake)})   # never the live wmux
        env.update(extra)
        return env

    def run(self, *args, stdin=None, use_wrapper=False, **extra):
        cmd = [*BASH, str(WRAPPER), *args] if use_wrapper else [sys.executable, str(SHIM), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env(**extra), input=stdin)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def reset_log(self):
        self.log.write_text("")


class CmuxShimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = ShimHarness(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # -- listing -----------------------------------------------------------
    def test_list_workspaces_plain_matches_cmux_shape(self):
        out = self.h.run("list-workspaces")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0], "* workspace:1  🧭 CoS  [selected]")
        self.assertEqual(lines[1], "  workspace:2  🚀 Launchpad")

    def test_list_workspaces_id_format_both_and_json(self):
        both = self.h.run("list-workspaces", "--id-format", "both").stdout.splitlines()
        self.assertEqual(both[1], "  workspace:2 ws-2  🚀 Launchpad")
        payload = json.loads(self.h.run("list-workspaces", "--json").stdout)
        self.assertEqual(payload["window_ref"], "window:1")
        ws = payload["workspaces"]
        self.assertEqual([w["ref"] for w in ws], ["workspace:1", "workspace:2"])
        self.assertEqual(ws[0]["title"], "🧭 CoS")
        self.assertTrue(ws[0]["selected"])
        self.assertIn("description", ws[0])
        self.assertIn("pinned", ws[0])
        self.assertEqual(ws[1]["index"], 1)

    def test_refs_are_stable_across_calls_and_reused_for_new_ids(self):
        self.h.run("list-workspaces")
        first = json.loads(self.h.run("rpc", "workspace.list", "{}").stdout)["workspaces"]
        self.assertEqual(first[1]["ref"], "workspace:2")
        self.h.run("new-workspace", "--name", "🍻 Switchboard Portable Kit 1", "--description", "switchboard worker rt-1")
        third = json.loads(self.h.run("rpc", "workspace.list", "{}").stdout)["workspaces"]
        self.assertEqual(third[2]["ref"], "workspace:3")
        self.assertEqual(third[2]["description"], "switchboard worker rt-1")
        self.assertEqual(third[0]["ref"], "workspace:1")

    def test_workspace_list_json_alias(self):
        payload = json.loads(self.h.run("workspace", "list", "--json").stdout)
        self.assertEqual(payload["workspaces"][0]["ref"], "workspace:1")

    # -- create ------------------------------------------------------------
    def test_new_workspace_sends_command_and_prints_ref(self):
        out = self.h.run("new-workspace", "--name", "🍻 Switchboard Portable Kit 1", "--description", "switchboard worker rt-abc",
                         "--cwd", "/mnt/c/switchboard", "--focus", "false", "--command", "amp threads continue T-1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"workspace:3")
        calls = self.h.calls()
        create = next(c for c in calls if c[0] == "new-workspace")
        self.assertIn("--panes", create)
        self.assertEqual(create[create.index("--panes") + 1], "1")
        self.assertEqual(create[create.index("--title") + 1], "🍻 Switchboard Portable Kit 1")
        self.assertEqual(create[create.index("--cwd") + 1], "/mnt/c/switchboard")
        send = next(c for c in calls if c[0] == "send")
        self.assertEqual(send, ["send", "--surface", "sf-ws-3", "amp threads continue T-1"])
        self.assertIn(["send-key", "enter", "--surface", "sf-ws-3"], calls)
        self.assertFalse(any(c[0] == "select-workspace" for c in calls), "--focus false must not select")

    def submit_env(self):
        return {"SWITCHBOARD_SHIM_SUBMIT_SETTLE": "0", "SWITCHBOARD_SHIM_READY_SETTLE": "0", "SWITCHBOARD_SHIM_SUBMIT_WAIT": "0.2"}

    def set_state(self, **values):
        data = json.loads(self.h.state.read_text())
        data.update(values)
        self.h.state.write_text(json.dumps(data))

    def test_new_workspace_presses_enter_again_until_the_prompt_log_has_the_line(self):
        # A lost launch Enter on wmux leaves the worker row at "routing", so the
        # shim checks wmux's prompt log.
        self.set_state(prompts=[], accept_on_enter=3)
        out = self.h.run("new-workspace", "--name", "W", "--command", "claude 'go'", **self.submit_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        enters = [c for c in self.h.calls() if c[:2] == ["send-key", "enter"]]
        self.assertEqual(len(enters), 3, "two lost Enters, then the one the shell accepted")
        self.assertIn("pressing Enter again", out.stderr)

    def test_enter_retries_are_bounded(self):
        self.set_state(prompts=[], accept_on_enter=99)
        out = self.h.run("new-workspace", "--name", "W", "--command", "claude 'go'", **self.submit_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        enters = [c for c in self.h.calls() if c[:2] == ["send-key", "enter"]]
        self.assertEqual(len(enters), 4, "the first Enter plus SWITCHBOARD_SHIM_SUBMIT_RETRIES (3)")
        self.assertIn("never acknowledged", out.stderr)

    def test_no_prompt_log_means_one_enter_as_before(self):
        out = self.h.run("new-workspace", "--name", "W", "--command", "claude 'go'", **self.submit_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len([c for c in self.h.calls() if c[:2] == ["send-key", "enter"]]), 1)

    def test_launch_state_reports_shell_and_agent_prompts(self):
        self.set_state(prompts=[{"seq": 1, "source": "shell", "text": "SWITCHBOARD_WORKER_TASK_ID=rt-1 claude"},
                                {"seq": 2, "source": "agent", "text": "Read and follow"}],
                       agent={"state": "working", "sessionProvider": "claude", "sessionId": "s-1"})
        out = self.h.run("launch-state", "--workspace", "workspace:2")
        self.assertEqual(out.returncode, 0, out.stderr)
        state = json.loads(out.stdout)
        self.assertEqual((state["prompt_log"], state["agent_prompts"], state["provider"], state["session_id"]),
                         (True, 1, "claude", "s-1"))
        self.assertEqual(state["shell_prompts"], ["SWITCHBOARD_WORKER_TASK_ID=rt-1 claude"])

    def test_new_workspace_defaults_to_git_bash_shell(self):
        # wmux's own default on Windows is pwsh; switchboard scripts and the seeded
        # .bashrc assume Git Bash, so every shim-created workspace asks for it.
        self.h.run("new-workspace", "--name", "X")
        create = next(c for c in self.h.calls() if c[0] == "new-workspace")
        self.assertEqual(create[create.index("--shell") + 1], "bash.exe")

    def test_new_workspace_shell_override_and_wmux_default(self):
        self.h.run("new-workspace", "--name", "X", SWITCHBOARD_WMUX_SHELL="pwsh")
        create = next(c for c in self.h.calls() if c[0] == "new-workspace")
        self.assertEqual(create[create.index("--shell") + 1], "pwsh")
        self.h.reset_log()
        self.h.run("new-workspace", "--name", "Y", SWITCHBOARD_WMUX_SHELL="default")
        create = next(c for c in self.h.calls() if c[0] == "new-workspace")
        self.assertNotIn("--shell", create)

    def test_new_workspace_focus_true_selects(self):
        self.h.run("new-workspace", "--name", "X", "--focus", "true")
        self.assertTrue(any(c[0] == "select-workspace" for c in self.h.calls()))

    def test_new_workspace_layout_json_is_rejected_like_old_cmux(self):
        out = self.h.run("new-workspace", "--name", "X", "--layout", "{}")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--layout", out.stderr)

    # -- close / rename / select ------------------------------------------
    def test_close_by_ref_and_force_flag(self):
        self.h.run("list-workspaces")
        out = self.h.run("close-workspace", "--workspace", "workspace:2", "--force")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(["close-workspace", "ws-2"], self.h.calls())
        remaining = self.h.run("list-workspaces").stdout
        self.assertNotIn("Launchpad", remaining)
        closed = json.loads(self.h.run("closed-workspaces").stdout)["closed"]
        self.assertEqual(closed[-1]["ref"], "workspace:2")

    def test_rpc_workspace_close_takes_workspace_id(self):
        self.h.run("list-workspaces")
        out = self.h.run("rpc", "workspace.close", json.dumps({"workspace_id": "workspace:2"}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(["close-workspace", "ws-2"], self.h.calls())

    def test_rename_workspace_positional_title(self):
        out = self.h.run("rename-workspace", "--workspace", "workspace:2", "🚀 Launchpad ✎")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(["rename-workspace", "ws-2", "🚀 Launchpad ✎"], self.h.calls())
        payload = json.loads(self.h.run("list-workspaces", "--json").stdout)
        self.assertTrue(payload["workspaces"][1]["has_custom_title"])

    def test_select_by_raw_id_and_by_index(self):
        self.h.run("select-workspace", "--workspace", "ws-2")
        self.assertIn(["select-workspace", "ws-2"], self.h.calls())
        self.h.reset_log()
        self.h.run("select-workspace", "--workspace", "0")
        self.assertIn(["select-workspace", "ws-1"], self.h.calls())

    def test_unknown_workspace_is_an_error(self):
        out = self.h.run("close-workspace", "--workspace", "workspace:99")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not found", out.stderr)

    def test_workspace_defaults_to_env(self):
        out = self.h.run("select-workspace", CMUX_WORKSPACE_ID="ws-2")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(["select-workspace", "ws-2"], self.h.calls())

    # -- workspace-action / sidecar state ----------------------------------
    def test_workspace_action_pin_description_color_persist(self):
        pin = self.h.run("workspace-action", "--action", "pin", "--workspace", "workspace:1")
        self.assertIn("seed-session.ps1 -Restart", pin.stderr)
        self.h.run("workspace-action", "--action", "set-description", "--description", "switchboard worker rt-9", "--workspace", "workspace:2")
        self.h.run("workspace-action", "--action", "set-color", "--color", "#8e44ad", "--workspace", "workspace:2")
        payload = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertTrue(payload[0]["pinned"])
        self.assertEqual(payload[1]["description"], "switchboard worker rt-9")
        self.assertEqual(payload[1]["custom_color"], "#8e44ad")
        self.h.run("workspace-action", "--action", "unpin", "--workspace", "workspace:1")
        payload = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertFalse(payload[0]["pinned"])
        # wmux has no pin/color RPC: the only thing that reaches it is the lane
        # label on the row's idle status text, and only when the label changes.
        calls = self.h.calls()
        self.assertEqual({c[0] for c in calls}, {"list-workspaces", "set-status"})
        statuses = [c for c in calls if c[0] == "set-status"]
        self.assertEqual(statuses, [
            ["set-status", "--workspace", "ws-1", "--state", "idle", "--text", "Pinned"],
            ["set-status", "--workspace", "ws-2", "--state", "idle", "--text", "Workers"],
            ["set-status", "--workspace", "ws-1", "--state", "idle", "--text", "Idle"],
        ])

    def test_workspace_status_lane_follows_description(self):
        self.h.run("workspace-action", "--action", "set-description", "--description", "switchboard worker rt-9", "--workspace", "workspace:2")
        out = self.h.run("workspace", "status", "--workspace", "workspace:2").stdout.splitlines()
        # line 1 is cmux vocabulary (command-center-digest.py reads it); `lane:` is the switchboard lane
        self.assertEqual(out[0], "working")
        self.assertIn("lane: workers", out)
        self.assertEqual(self.h.run("workspace", "status", "--workspace", "workspace:1").stdout.splitlines()[0], "todo")
        self.h.run("workspace", "status", "set", "review", "--workspace", "workspace:1")
        out = self.h.run("workspace", "status", "--workspace", "workspace:1").stdout.splitlines()
        self.assertEqual(out[0], "review")
        self.assertIn("override: review", out)
        self.assertIn("lane: toreview", out)
        self.h.run("workspace", "status", "set", "auto", "--workspace", "workspace:1")
        self.assertEqual(self.h.run("workspace", "status", "--workspace", "workspace:1").stdout.splitlines()[0], "todo")

    # -- lanes (mirror of system/cmux-sidebars/switchboard.swift) ---------------
    def test_sidebar_lanes_follow_switchboard_bucket_rules(self):
        self.h.state.write_text(json.dumps({
            "workspaces": [
                {"id": "ws-1", "title": "🧭 CoS", "isActive": True, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-2", "title": "⏰ Scheduled Jobs", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-3", "title": "🔥 P0 · Fix the thing", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-4", "title": "◆ Needs me", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-5", "title": "⠋ Running one", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-6", "title": "📝 Review me", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
                {"id": "ws-7", "title": "🧱 Worker 1", "isActive": False, "cwd": "C:\\switchboard", "shell": "pwsh"},
            ],
            "surfaces": {f"ws-{n}": [{"id": f"sf-{n}", "type": "terminal", "paneId": f"pane-{n}", "isActive": True}] for n in range(1, 8)},
        }))
        self.h.run("workspace-action", "--action", "pin", "--workspace", "ws-1")
        self.h.run("workspace-action", "--action", "set-color", "--color", "#bb9af7", "--workspace", "ws-6")
        self.h.run("workspace-action", "--action", "set-description", "--description", "switchboard worker rt-1", "--workspace", "ws-7")
        view = json.loads(self.h.run("sidebar", "lanes", "--json").stdout)
        by_lane = {lane["key"]: [w["id"] for w in lane["workspaces"]] for lane in view["lanes"]}
        self.assertEqual([lane["key"] for lane in view["lanes"]],
                         ["pinned", "attention", "active", "workers", "toreview", "p0", "waiting", "paused", "idle", "background"])
        self.assertEqual(by_lane["pinned"], ["ws-1"])
        self.assertEqual(by_lane["background"], ["ws-2"])
        self.assertEqual(by_lane["p0"], ["ws-3"])
        self.assertEqual(by_lane["attention"], ["ws-4"])
        self.assertEqual(by_lane["active"], ["ws-5"])
        self.assertEqual(by_lane["toreview"], ["ws-6"])
        self.assertEqual(by_lane["workers"], ["ws-7"])
        self.assertEqual(view["workspace_count"], 7)
        self.assertEqual(view["selected"], "ws-1")
        tints = {lane["key"]: lane["tint"] for lane in view["lanes"]}
        self.assertEqual(tints["attention"], "#F5A623")
        self.assertEqual(tints["toreview"], "#BB9AF7")
        row = next(w for w in view["lanes"][5]["workspaces"] if w["id"] == "ws-3")
        self.assertEqual((row["icon"], row["label"]), ("🔥", "Fix the thing"))
        # a running agent state outranks the review color
        self.h.run("set-status", "amp", "__cmux_amp_status_thinking", "--workspace", "ws-6")
        view = json.loads(self.h.run("sidebar", "lanes", "--json").stdout)
        by_lane = {lane["key"]: [w["id"] for w in lane["workspaces"]] for lane in view["lanes"]}
        self.assertEqual(by_lane["active"], ["ws-5", "ws-6"])
        plain = self.h.run("sidebar", "lanes").stdout
        self.assertTrue(plain.startswith("7 ws · 0 unread"))
        self.assertIn("ACTIVE (2)", plain)

    def test_unread_counts_turns_that_end_off_screen(self):
        # ws-2 is not selected: a turn that ends there is unread until it is selected or marked read
        self.h.run("set-status", "amp", "__cmux_amp_status_thinking", "--workspace", "workspace:2")
        self.h.run("set-status", "amp", "__cmux_amp_status_done", "--workspace", "workspace:2")
        view = json.loads(self.h.run("sidebar", "lanes", "--json").stdout)
        self.assertEqual(view["unread_total"], 1)
        self.assertIn(["set-status", "--workspace", "ws-2", "--state", "idle", "--text", "Idle · 1 unread"], self.h.calls())
        # the selected workspace never accrues unread
        self.h.run("set-status", "amp", "__cmux_amp_status_thinking", "--workspace", "workspace:1")
        self.h.run("set-status", "amp", "__cmux_amp_status_done", "--workspace", "workspace:1")
        self.assertEqual(json.loads(self.h.run("sidebar", "lanes", "--json").stdout)["unread_total"], 1)
        self.h.run("workspace-action", "--action", "mark-unread", "--workspace", "workspace:1")
        self.assertEqual(json.loads(self.h.run("sidebar", "lanes", "--json").stdout)["unread_total"], 1)  # selected row is spent on read
        self.h.run("workspace-action", "--action", "mark-read", "--workspace", "workspace:2")
        self.assertEqual(json.loads(self.h.run("sidebar", "lanes", "--json").stdout)["unread_total"], 0)
        self.h.run("set-status", "amp", "__cmux_amp_status_thinking", "--workspace", "workspace:2")
        self.h.run("set-status", "amp", "__cmux_amp_status_done", "--workspace", "workspace:2")
        self.h.run("select-workspace", "--workspace", "workspace:2")
        self.assertEqual(json.loads(self.h.run("sidebar", "lanes", "--json").stdout)["unread_total"], 0)

    def test_move_top_and_close_others_and_reload(self):
        self.h.run("new-workspace", "--name", "🧪 Third")
        for wid in ("ws-1", "ws-2", "ws-3"):
            self.h.run("workspace-action", "--action", "pin", "--workspace", wid)
        self.h.run("workspace-action", "--action", "move-top", "--workspace", "ws-3")
        pinned = next(l for l in json.loads(self.h.run("sidebar", "lanes", "--json").stdout)["lanes"] if l["key"] == "pinned")
        self.assertEqual([w["id"] for w in pinned["workspaces"]], ["ws-3", "ws-1", "ws-2"])
        self.h.reset_log()
        out = self.h.run("sidebar", "reload")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len([c for c in self.h.calls() if c[0] == "set-status"]), 3)
        self.assertEqual(self.h.run("sidebar", "validate").returncode, 0)
        out = self.h.run("workspace-action", "--action", "close-others", "--workspace", "ws-1")
        self.assertEqual(out.returncode, 0, out.stderr)
        closed = [c[1] for c in self.h.calls() if c[0] == "close-workspace"]
        self.assertEqual(sorted(closed), ["ws-2", "ws-3"])
        remaining = json.loads(self.h.run("list-workspaces", "--json").stdout)["workspaces"]
        self.assertEqual([w["id"] for w in remaining], ["ws-1"])
        closed_rows = json.loads(self.h.run("closed-workspaces").stdout)["closed"]
        self.assertEqual({row["id"] for row in closed_rows}, {"ws-2", "ws-3"})
        self.assertTrue(all(row["pinned"] for row in closed_rows))

    def test_new_workspace_never_reorders_wmux(self):
        # No tab ordering on wmux. A new tab keeps its
        # sidecar order empty and no reorder or pin call reaches wmux.
        out = self.h.run("new-workspace", "--name", "✅ Todo")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse([c for c in self.h.calls() if c[0] in ("reorder-workspace", "move-workspace", "pin-workspace")])
        side = json.loads(self.h.sidecar.read_text(encoding="utf-8"))["workspaces"]
        self.assertTrue(all(row.get("order_index") is None for row in side.values()))
        arrange = self.h.run("sidebar", "arrange")   # the cmux verb is a plain no-op here
        self.assertIn("no-op on wmux", arrange.stdout)
        side = json.loads(self.h.sidecar.read_text(encoding="utf-8"))["workspaces"]
        self.assertTrue(all(row.get("order_index") is None for row in side.values()))

    def test_markdown_open_returns_surface_and_set_updates_it(self):
        out = self.h.run("markdown", "open", "C:\\switchboard\\board.md")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["ok", "surface:1", "sf-md-1"])
        self.assertIn(["markdown", "set", "sf-md-1", "--file", "C:\\switchboard\\board.md"], self.h.calls())
        out = self.h.run("markdown", "set", "surface:1", "--content", "# hello", "--title", "Board")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(["markdown", "set", "sf-md-1", "--content", "# hello", "--title", "Board"], self.h.calls())

    # -- typing ------------------------------------------------------------
    def test_send_unescapes_and_presses_enter_for_trailing_newline(self):
        out = self.h.run("send", "--workspace", "workspace:1", "hello world\\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.h.calls()
        self.assertIn(["send", "--surface", "sf-1", "hello world"], calls)
        self.assertIn(["send-key", "enter", "--surface", "sf-1"], calls)

    def test_send_to_another_workspace_ignores_the_callers_surface(self):
        # Windows v6: mux.sh exports CMUX_SURFACE_ID, and a send meant for a new tab
        # was typed into the pane that ran the launcher.
        self.h.run("send", "--workspace", "workspace:2", "for ws2", CMUX_SURFACE_ID="sf-1")
        self.assertIn(["send", "--surface", "sf-2", "for ws2"], self.h.calls())
        self.assertNotIn(["send", "--surface", "sf-1", "for ws2"], self.h.calls())

    def test_send_to_own_workspace_keeps_the_callers_surface(self):
        self.h.run("send", "--workspace", "workspace:1", "mine", CMUX_SURFACE_ID="sf-1")
        self.assertIn(["send", "--surface", "sf-1", "mine"], self.h.calls())

    def test_send_to_own_workspace_keeps_a_ref_form_caller_surface(self):
        # CMUX_SURFACE_ID may hold the ref form (surface:N);
        # matching only the id dropped it and typed into the workspace's first surface.
        tree = self.h.run("tree", "--workspace", "workspace:1").stdout
        ref = next(tok for tok in tree.split() if tok.startswith("surface:"))
        self.h.reset_log()
        self.h.run("send", "--workspace", "workspace:1", "mine", CMUX_SURFACE_ID=ref)
        self.assertIn(["send", "--surface", "sf-1", "mine"], self.h.calls())
        self.h.reset_log()
        self.h.run("send", "--workspace", "workspace:2", "for ws2", CMUX_SURFACE_ID=ref)
        self.assertIn(["send", "--surface", "sf-2", "for ws2"], self.h.calls())

    def test_send_without_newline_does_not_press_enter(self):
        self.h.run("send", "--workspace", "workspace:1", "draft text")
        self.assertFalse(any(c[0] == "send-key" for c in self.h.calls()))

    def test_send_key_translates_enter_and_ctrl_c(self):
        self.h.run("send-key", "--workspace", "workspace:1", "Enter")
        self.h.run("send-key", "--workspace", "workspace:1", "ctrl+c")
        calls = [c for c in self.h.calls() if c[0] == "send-key"]
        self.assertEqual(calls[0], ["send-key", "enter", "--surface", "sf-1"])
        self.assertEqual(calls[1], ["send-key", "c", "--ctrl", "--surface", "sf-1"])

    def test_send_uses_surface_ref_from_tree(self):
        tree = self.h.run("tree", "--workspace", "workspace:2").stdout
        self.assertIn('└── workspace workspace:2 "🚀 Launchpad"', tree)
        self.assertRegex(tree, r"surface surface:\d+ \[terminal\]")
        ref = next(tok for tok in tree.split() if tok.startswith("surface:"))
        self.h.reset_log()
        self.h.run("read-screen", "--workspace", "workspace:2", "--surface", ref, "--lines", "40")
        self.assertIn(["read-screen", "--surface", "sf-2", "--lines", "40"], self.h.calls())

    def test_read_screen_prints_text(self):
        out = self.h.run("read-screen", "--workspace", "workspace:1", "--lines", "20")
        self.assertEqual(out.stdout.rstrip("\n"), "amp> ")

    # -- status pills, agent state, hooks ---------------------------------
    def test_set_status_amp_mirrors_wmux_state_and_blocked(self):
        self.h.run("set-status", "amp", "__cmux_amp_status_thinking", "--icon", "brain", "--color", "#f39c12", "--workspace", "workspace:1")
        calls = self.h.calls()
        self.assertIn(["set-status", "--workspace", "ws-1", "--state", "running", "--text", "Thinking"], calls)
        self.assertFalse(any(c[0] == "report-agent" for c in calls))
        self.h.reset_log()
        self.h.run("set-status", "amp", "__cmux_amp_status_needs_input", "--workspace", "workspace:1")
        calls = self.h.calls()
        self.assertIn(["set-status", "--workspace", "ws-1", "--state", "interrupted", "--text", "Needs input"], calls)
        self.assertIn(["report-agent", "--blocked", "Amp needs input", "--surface", "sf-1"], calls)
        self.h.reset_log()
        self.h.run("set-status", "amp", "__cmux_amp_status_idle", "--workspace", "workspace:1")
        self.assertIn(["report-agent", "--unblocked", "--surface", "sf-1"], self.h.calls())
        listed = self.h.run("list-status", "--workspace", "workspace:1").stdout
        self.assertEqual(listed.strip(), "amp=__cmux_amp_status_idle")

    def test_non_amp_status_stays_in_sidecar_only(self):
        self.h.run("set-status", "running-tasks", "2 running", "--icon", "bolt", "--priority", "3", "--workspace", "workspace:1")
        self.assertFalse(any(c[0] in ("set-status", "report-agent") for c in self.h.calls()))
        listed = self.h.run("list-status", "--workspace", "workspace:1").stdout.strip()
        self.assertEqual(listed, "running-tasks=2 running icon=bolt priority=3")
        self.h.run("clear-status", "running-tasks", "--workspace", "workspace:1")
        self.assertEqual(self.h.run("list-status", "--workspace", "workspace:1").stdout.strip(), "")

    def test_hooks_session_start_binds_thread_into_session_store(self):
        payload = json.dumps({"session_id": "T-00000000-0000-4000-8000-0000000000e1", "cwd": "/mnt/c/switchboard"})
        out = self.h.run("hooks", "enqueue", "amp", "session-start", stdin=payload, CMUX_WORKSPACE_ID="ws-2", CMUX_SURFACE_ID="sf-2")
        self.assertEqual(out.returncode, 0, out.stderr)
        store = json.loads(self.h.session.read_text())
        ws = store["windows"][0]["tabManager"]["workspaces"]
        bound = next(w for w in ws if w["workspaceId"] == "ws-2")
        self.assertEqual(bound["panels"][0]["terminal"]["agent"], {"kind": "amp", "sessionId": "T-00000000-0000-4000-8000-0000000000e1"})
        self.assertEqual(bound["title"], "🚀 Launchpad")
        self.assertEqual(self.h.run("hooks", "install", "amp").returncode, 0)

    def test_session_store_carries_status_entries(self):
        self.h.run("set-status", "owner", "rt-1", "--workspace", "workspace:1")
        store = json.loads(self.h.session.read_text())
        entries = store["windows"][0]["tabManager"]["workspaces"][0]["statusEntries"]
        self.assertEqual(entries[0]["key"], "owner")
        self.assertEqual(entries[0]["value"], "rt-1")

    # -- misc verbs --------------------------------------------------------
    def test_identify_and_ping(self):
        ident = json.loads(self.h.run("identify", CMUX_WORKSPACE_ID="ws-1", CMUX_SURFACE_ID="sf-1").stdout)
        self.assertEqual(ident["app"], "cmux")
        self.assertEqual(ident["backend"]["app"], "wmux")
        self.assertEqual(ident["backend"]["version"], "2.49.0")
        self.assertEqual(ident["caller"]["workspace_ref"], "workspace:1")
        self.assertEqual(self.h.run("ping").stdout.strip(), "PONG")

    def test_log_notify_top_sidebar(self):
        self.h.run("log", "--level", "info", "--source", "amp", "--workspace", "workspace:1", "--", "hello", "there")
        self.assertIn(["log", "info", "[amp] hello there"], self.h.calls())
        # untargeted: no WMUX_SURFACE_ID is added, wmux keeps its own default
        self.h.run("notify", "--title", "switchboard", "--body", "done")
        self.assertIn(["notify", "switchboard: done"], self.h.calls())
        # --workspace targets the row: wmux gets that workspace's surface id and the
        # sidecar unread count rises for a non-selected workspace only
        self.h.run("notify", "--workspace", "workspace:2", "--title", "switchboard", "--body", "ready")
        self.assertIn(["notify", "switchboard: ready", "env:WMUX_SURFACE_ID=sf-2"], self.h.calls())
        side = json.loads(self.h.sidecar.read_text())
        self.assertEqual(side["workspaces"]["ws-2"]["unread"], 1)
        self.h.run("notify", "--workspace", "workspace:1", "hi")
        self.assertIn(["notify", "hi", "env:WMUX_SURFACE_ID=sf-1"], self.h.calls())
        self.assertEqual(json.loads(self.h.sidecar.read_text())["workspaces"]["ws-1"]["unread"], 0)
        top = self.h.run("top", "--workspace", "workspace:1", "--processes", "--flat", "--format", "tsv")
        self.assertEqual(top.returncode, 0)
        self.assertEqual(top.stdout.strip(), "")
        self.assertEqual(self.h.run("sidebar", "reload").returncode, 0)

    def test_workspace_id_of_surface(self):
        out = self.h.run("workspace-id-of-surface", FAKE_CURRENT="ws-2")
        self.assertEqual(out.stdout.strip(), "ws-2")

    def test_unknown_verb_exits_2_like_cmux(self):
        out = self.h.run("frobnicate")
        self.assertEqual(out.returncode, 2)
        self.assertIn("Unknown command", out.stderr)

    def test_missing_wmux_binary_is_a_clear_error(self):
        out = self.h.run("list-workspaces", WMUX_BIN=str(self.h.tmp / "no-such-wmux"))
        self.assertEqual(out.returncode, 127)
        self.assertIn("WMUX_BIN", out.stderr)

    def test_bash_wrapper_runs_the_shim(self):
        out = self.h.run("ping", use_wrapper=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "PONG")


class MuxDetectionTest(unittest.TestCase):
    def run_sh(self, snippet: str, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("CMUX_", "WMUX", "SWITCHBOARD_"))}
        base.update(env)
        return subprocess.run([shutil.which("bash") or "bash", "-c", f"source '{(HERE / 'lib' / 'mux.sh').as_posix()}'; {snippet}"],
                              capture_output=True, text=True, env=base)

    def test_forced_backend_wins(self):
        self.assertEqual(self.run_sh("mux_detect", SWITCHBOARD_MUX="wmux").stdout.strip(), "wmux")
        self.assertEqual(self.run_sh("mux_detect", SWITCHBOARD_MUX="cmux").stdout.strip(), "cmux")

    def test_wmux_pane_env_selects_wmux_and_points_cmux_bin_at_shim(self):
        out = self.run_sh("mux_init; echo \"$SWITCHBOARD_MUX|$CMUX_BIN|$CMUX_SURFACE_ID\"", WMUX="1", WMUX_SURFACE_ID="sf-9",
                          WMUX_BIN="/bin/false")
        backend, cmux_bin, surface = out.stdout.strip().split("|")
        self.assertEqual(backend, "wmux")
        self.assertTrue(cmux_bin.endswith("/mux/cmux"), cmux_bin)
        self.assertEqual(surface, "sf-9")

    def test_existing_cmux_bin_is_preserved_on_wmux(self):
        out = self.run_sh("mux_init; echo \"$CMUX_BIN\"", WMUX="1", WMUX_BIN="/bin/false", CMUX_BIN="/bin/echo")
        self.assertEqual(out.stdout.strip(), "/bin/echo")

    def test_macos_auto_detection_does_not_touch_cmux_bin(self):
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        out = self.run_sh("mux_init; echo \"$SWITCHBOARD_MUX|${CMUX_BIN:-unset}\"")
        backend, cmux_bin = out.stdout.strip().split("|")
        self.assertIn(backend, ("cmux", "none"))
        self.assertEqual(cmux_bin, "unset")

    def test_python_mirror(self):
        sys.path.insert(0, str(HERE))
        try:
            from lib import mux  # type: ignore
        finally:
            sys.path.pop(0)
        saved = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith(("CMUX_", "WMUX", "SWITCHBOARD_")):
                    del os.environ[key]
            os.environ["SWITCHBOARD_MUX"] = "wmux"
            self.assertEqual(mux.backend(), "wmux")
            self.assertTrue(Path(mux.cmux_bin()).as_posix().endswith("/mux/cmux"))
            os.environ["CMUX_BIN"] = "/opt/cmux"
            self.assertEqual(mux.cmux_bin(), "/opt/cmux")
            del os.environ["CMUX_BIN"]
            os.environ["SWITCHBOARD_MUX"] = "cmux"
            self.assertEqual(mux.backend(), "cmux")
            self.assertNotIn("/mux/cmux", mux.cmux_bin())
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_runnable_starts_the_wrapper_through_python_on_windows(self):
        sys.path.insert(0, str(HERE))
        try:
            from lib import mux  # type: ignore
        finally:
            sys.path.pop(0)
        from unittest import mock

        with mock.patch.object(mux, "_is_windows", return_value=False):   # pinned: on real Windows this call is rewritten
            self.assertEqual(mux.runnable([str(WRAPPER), "ping"]), [str(WRAPPER), "ping"])  # never rewritten off Windows
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tmp = Path(scratch.name)
        (tmp / "tool.sh").write_text("#!/usr/bin/env bash\necho hi\n")
        (tmp / "tool.py").write_text("#!/usr/bin/env python3\nprint('hi')\n")
        (tmp / "tool.exe").write_text("MZ")
        with mock.patch.object(mux, "_is_windows", return_value=True):
            self.assertEqual(mux.runnable([str(WRAPPER), "send", 'a "quoted"\nline']),
                             [sys.executable, str(SHIM), "send", 'a "quoted"\nline'])
            self.assertEqual(mux.runnable([str(tmp / "tool.py"), "x"]), [sys.executable, str(tmp / "tool.py"), "x"])
            self.assertTrue(mux.runnable([str(tmp / "tool.sh")])[0].lower().endswith(("bash", "bash.exe")))
            self.assertEqual(mux.runnable([str(tmp / "tool.exe")]), [str(tmp / "tool.exe")])
            self.assertEqual(mux.runnable(["cmux", "ping"]), ["cmux", "ping"])  # not a file here: left to PATH
            self.assertEqual(mux.runnable([]), [])

    def test_python_wmux_bin_runs_with_the_current_interpreter(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        harness = ShimHarness(Path(scratch.name))
        out = harness.run("list-workspaces")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(harness.fake.name.endswith(".py"))


def load_shim():
    spec = importlib.util.spec_from_file_location("cmux_shim_under_test", SHIM)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SidecarConcurrencyTest(unittest.TestCase):
    """Every shim call is its own process and each wrote the whole file back, so the last writer
    erased the others' changes. That dropped "switchboard worker <task-id>" from live tabs and the
    reaper closed them."""

    @classmethod
    def setUpClass(cls):
        cls.shim = load_shim()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sidecar.json"

    def fresh(self):
        return self.shim.Sidecar(self.path)

    def test_a_stale_writer_keeps_a_description_written_after_it_loaded(self):
        first = self.fresh()
        first.workspace("ws-1")
        first.save()
        stale, launcher = self.fresh(), self.fresh()
        launcher.workspace("ws-1")["description"] = "switchboard worker rt-new"
        launcher.dirty = True
        launcher.save()
        stale.workspace("ws-1")["unread"] = 3
        stale.dirty = True
        stale.save()
        final = self.fresh().workspace("ws-1")
        self.assertEqual(final["description"], "switchboard worker rt-new")
        self.assertEqual(final["unread"], 3)

    def test_two_processes_number_two_new_tabs_apart_and_one_new_tab_alike(self):
        a, b = self.fresh(), self.fresh()
        self.assertNotEqual(a.workspace("ws-a")["ref"], b.workspace("ws-b")["ref"])
        self.assertEqual(b.workspace("ws-a")["ref"], a.workspace("ws-a")["ref"])
        a.save(); b.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(sorted(data["workspaces"]), ["ws-a", "ws-b"])
        self.assertEqual(data["next_ref"], 3)

    def test_a_close_seen_by_one_process_is_not_undone_by_a_stale_writer(self):
        first = self.fresh()
        first.workspace("ws-1"); first.workspace("ws-2")
        first.save()
        stale, closer = self.fresh(), self.fresh()
        closer.sync([{"id": "ws-2", "title": "kept"}])
        closer.save()
        stale.workspace("ws-1")["unread"] = 1
        stale.dirty = True
        stale.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(sorted(data["workspaces"]), ["ws-2"])
        self.assertEqual([c["id"] for c in data["closed"]], ["ws-1"])

    def test_an_empty_list_never_counts_as_every_tab_closed(self):
        first = self.fresh()
        first.workspace("ws-1")
        first.save()
        again = self.fresh()
        again.sync([])
        again.dirty = True
        again.save()
        self.assertIn("ws-1", json.loads(self.path.read_text())["workspaces"])

    def test_an_unreadable_file_is_never_merged_as_empty(self):
        first = self.fresh()
        first.workspace("ws-1")["description"] = "switchboard worker rt-1"
        first.dirty = True
        first.save()
        saved = self.path.read_text()
        again = self.fresh()
        again.workspace("ws-1")["unread"] = 2
        again.dirty = True
        with patch.object(self.shim.time, "sleep", lambda s: None), patch.object(type(self.path), "read_text", side_effect=PermissionError("busy")):
            again.save()
        self.assertTrue(again.dirty, "not saved, so a later save still carries the change")
        self.assertEqual(self.path.read_text(), saved)
        again.save()
        final = json.loads(self.path.read_text())["workspaces"]["ws-1"]
        self.assertEqual((final["description"], final["unread"]), ("switchboard worker rt-1", 2))

    def test_concurrent_processes_lose_no_description(self):
        script = textwrap.dedent(f"""
            import importlib.util, sys, pathlib
            spec = importlib.util.spec_from_file_location('s', {str(SHIM)!r})
            m = importlib.util.module_from_spec(spec); sys.modules['s'] = m; spec.loader.exec_module(m)
            side = m.Sidecar(pathlib.Path({str(self.path)!r}))
            wid = sys.argv[1]
            side.workspace(wid)['description'] = 'switchboard worker ' + wid
            side.dirty = True
            side.save()
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script, f"rt-{n}"]) for n in range(6)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 6)
        data = json.loads(self.path.read_text())
        self.assertEqual({w: e["description"] for w, e in data["workspaces"].items()},
                         {f"rt-{n}": f"switchboard worker rt-{n}" for n in range(6)})
        self.assertEqual(len({e["ref"] for e in data["workspaces"].values()}), 6, "no two tabs share a ref")


class NewWorkspaceIdentityTest(unittest.TestCase):
    """The tab's identity is on disk before the slow typing starts, a markdown or browser pane
    is never the target, and a pane that answers "has no PTY" gets another try."""

    @classmethod
    def setUpClass(cls):
        cls.shim = load_shim()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sidecar.json"
        patcher = patch.dict(os.environ, {"SWITCHBOARD_WMUX_SIDECAR": str(self.path), "SWITCHBOARD_TEST_SANDBOX": "1",
                                          "SWITCHBOARD_CMUX_SESSION_JSON": str(Path(self._tmp.name) / "store.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.surfaces = [{"id": "sf-md", "type": "markdown", "paneId": "p-1", "isActive": True},
                         {"id": "sf-term", "type": "terminal", "paneId": "p-2", "isActive": False}]

    def make(self):
        shim = self.shim.Shim()
        live = [{"id": "ws-1", "title": "🧭 CoS", "isActive": True}]

        def answer(args, timeout=20):
            if args[0] == "list-workspaces":
                return {"workspaces": list(live)}
            if args[0] == "new-workspace":
                live.append({"id": "ws-new", "title": "W", "isActive": False})
                return {"workspaceId": "ws-new"}
            if args[0] == "list-surfaces":
                return {"surfaces": self.surfaces}
            return {}

        shim.wmux = types.SimpleNamespace(json=answer, run=lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
        shim.out = lambda text="": None
        return shim

    def test_the_description_is_on_disk_before_the_command_is_typed(self):
        shim = self.make()
        seen = []
        shim.wait_shell_ready = lambda sid: seen.append(json.loads(self.path.read_text())["workspaces"]["ws-new"]["description"])
        shim.type_into = lambda sid, text, press_enter=False: None
        shim.cmd_new_workspace([], {"--name": "W", "--description": "switchboard worker rt-new", "--command": "go"})
        self.assertEqual(seen, ["switchboard worker rt-new"])

    def test_typing_targets_the_terminal_when_another_pane_has_focus(self):
        shim = self.make()
        shim.wait_shell_ready = lambda sid: None
        typed = []
        shim.type_into = lambda sid, text, press_enter=False: typed.append(sid)
        shim.cmd_new_workspace([], {"--name": "W", "--description": "d", "--command": "go"})
        self.assertEqual(typed, ["sf-term"])

    def test_no_pty_retries_and_another_error_does_not(self):
        shim = self.make()
        shim.wait_shell_ready = lambda sid: None
        tries = []

        def flaky(sid, text, press_enter=False):
            tries.append(sid)
            if len(tries) == 1:
                raise self.shim.ShimError("Error: surface sf has no PTY (pane is markdown/browser, or surface was closed).")

        shim.type_into = flaky
        with patch.object(self.shim.time, "sleep", lambda s: None):
            shim.cmd_new_workspace([], {"--name": "W", "--description": "d", "--command": "go"})
        self.assertEqual(len(tries), 2)
        shim = self.make()
        shim.wait_shell_ready = lambda sid: None

        def broken(sid, text, press_enter=False):
            raise self.shim.ShimError("wmux send failed")

        shim.type_into = broken
        with self.assertRaises(self.shim.ShimError):
            shim.cmd_new_workspace([], {"--name": "W", "--description": "d", "--command": "go"})


if __name__ == "__main__":
    unittest.main()
