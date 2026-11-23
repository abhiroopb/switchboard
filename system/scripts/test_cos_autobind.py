#!/usr/bin/env python3
"""cos_autobind.py and the surface-context.py hook against a stub multiplexer.

The owner opens a wmux tab, types `claude`, then "start of day", "CoS" or "chief of staff".
The first prompt must leave a registered, titled, pinned CoS tab bound to the Claude session,
with any older CoS renamed and never closed. These tests never reach a live tab:
SWITCHBOARD_TEST_SANDBOX=1 and a stateful stub CMUX_BIN that records every call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HOOK = ROOT / "system" / "claude-marketplace" / "plugins" / "switchboard" / "hooks" / "surface-context.py"
sys.path.insert(0, str(HERE))
import cos_autobind  # noqa: E402

# A cmux/wmux-shim stand-in that keeps its tabs in STUB_STATE and logs each call to STUB_LOG.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state_path = os.environ["STUB_STATE"]
state = json.load(open(state_path, encoding="utf-8"))
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
def flag(name):
    return args[args.index(name) + 1] if name in args else None
def tab(token):
    return next((t for t in state["tabs"] if token in (t["ref"], t["id"])), None)
def save():
    json.dump(state, open(state_path, "w", encoding="utf-8"))
cmd = args[0] if args else ""
if cmd == "rpc":
    print(json.dumps({"workspaces": state["tabs"]}))
elif cmd == "workspace-id-of-surface":
    sid = os.environ.get("WMUX_SURFACE_ID", "")
    wid = state.get("surfaces", {}).get(sid, "")
    if not wid:
        sys.exit(1)
    print(wid)
elif cmd == "session-bind":
    tab(flag("--workspace"))["thread"] = flag("--thread"); save(); print("ok")
elif cmd == "rename-workspace":
    tab(flag("--workspace"))["title"] = args[-1]; save(); print("ok")
elif cmd == "workspace-action":
    t = tab(flag("--workspace")); action = flag("--action")
    if action in ("pin", "unpin"):
        t["pinned"] = action == "pin"
    elif action == "move-top":
        state["tabs"].remove(t); state["tabs"].insert(0, t)
    save(); print("ok " + action)
elif cmd == "sidebar":
    print("ok sidebar arrange")
elif cmd == "close-workspace":
    sys.exit(9)
else:
    sys.exit(2)
'''

OLD_COS = {"ref": "workspace:236", "id": "ws-old", "title": "🧭 CoS", "description": "", "pinned": True}
NEW_TAB = {"ref": "workspace:260", "id": "ws-new", "title": "Workspace 7", "description": "", "pinned": False}
WORKER = {"ref": "workspace:252", "id": "ws-worker", "title": "📊 Switchboard Maintenance 1",
          "description": "switchboard worker rt-abc", "pinned": False}


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cos-autobind-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "cmux"
        self.stub.write_text(STUB, encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)
        self.state = self.tmp / "stub-state.json"
        self.log = self.tmp / "calls.log"
        self.registration = self.tmp / "cos-surface.json"
        self.cwd = self.tmp / "home"  # not a git checkout: a plain `claude` in a new tab
        self.cwd.mkdir()
        self.write_tabs([dict(OLD_COS), dict(NEW_TAB), dict(WORKER)],
                        {"surf-new": "ws-new", "surf-worker": "ws-worker"})

    def write_tabs(self, tabs, surfaces=None) -> None:
        self.state.write_text(json.dumps({"tabs": tabs, "surfaces": surfaces or {}}), encoding="utf-8")

    def tabs(self) -> dict:
        return {t["ref"]: t for t in json.loads(self.state.read_text(encoding="utf-8"))["tabs"]}

    def order(self) -> list:
        return [t["ref"] for t in json.loads(self.state.read_text(encoding="utf-8"))["tabs"]]

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def env(self, wmux: bool = True, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith(("SWITCHBOARD_WORKER", "CMUX", "WMUX", "CLAUDE_")) or k in
                       ("SWITCHBOARD_WORKTREE", "SWITCHBOARD_SURFACE", "CC_CMUX_SESSION_JSON"))}
        env.update({
            "SWITCHBOARD_TEST_SANDBOX": "1", "CMUX_BIN": str(self.stub), "STUB_STATE": str(self.state),
            "STUB_LOG": str(self.log), "COS_SURFACE_FILE": str(self.registration),
            "COS_AUTOBIND_STATE_DIR": str(self.tmp / "autobind"),
            "SURFACE_CONTEXT_STATE_DIR": str(self.tmp / "surface-context"),
            "CC_CMUX_SESSION_JSON": str(self.tmp / "no-session-store.json"),
            "RUNNING_TASKS_FILE": str(self.tmp / "running-tasks.json"),
            "SWITCHBOARD_STATE_DIR": str(self.tmp / "state"),
            "SWITCHBOARD_PLUGIN_CONFIG": str(self.tmp / "plugin-config.json"),
            "SWITCHBOARD_ROOT": str(ROOT), "PYTHONIOENCODING": "utf-8",
        })
        if wmux:
            env.update({"SWITCHBOARD_MUX": "wmux", "WMUX": "1", "WMUX_SURFACE_ID": "surf-new"})
        else:
            env.update({"SWITCHBOARD_MUX": "cmux", "CMUX_WORKSPACE_ID": "ws-new", "CMUX_SURFACE_ID": "surface:1"})
        env.update(extra)
        return env

    def observe(self, prompt: str, session: str = "11111111-2222-3333-4444-555555555555", **env_kw) -> tuple[str, dict]:
        code = ("import json, sys; sys.path.insert(0, sys.argv[3]); import cos_autobind; "
                "ctx, res = cos_autobind.observe(sys.argv[1], sys.argv[2], cwd=sys.argv[4]); "
                "print(json.dumps({'context': ctx, 'result': res}))")
        proc = subprocess.run([sys.executable, "-c", code, prompt, session, str(HERE), str(self.cwd)],
                              capture_output=True, text=True, encoding="utf-8", env=self.env(**env_kw),
                              cwd=str(self.cwd), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        return out["context"], out["result"]


class TriggerTests(unittest.TestCase):
    def test_phrases_match_whole_prompt_only(self) -> None:
        for prompt in ("start of day", "Start of day.", "  start my day ", "SOD", "good morning",
                       "/switchboard:start-day", "ok, start of day please"):
            self.assertEqual(cos_autobind.trigger(prompt), "start-of-day", prompt)
        for prompt in ("CoS", "cos", "Chief of Staff", "chief of staff!", "/chief-of-staff", "hey CoS"):
            self.assertEqual(cos_autobind.trigger(prompt), "cos", prompt)
        for prompt in ("CoS should review the doc", "start of day was slow", "what is chief of staff",
                       "", "cosmos", "send it"):
            self.assertEqual(cos_autobind.trigger(prompt), "", prompt)

    def test_refusals(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cos-autobind-wt-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pane = {"WMUX_SURFACE_ID": "surf"}
        self.assertEqual(cos_autobind.refusal({**pane, "SWITCHBOARD_WORKER_ID": "w"}, str(tmp)), "worker session")
        self.assertEqual(cos_autobind.refusal({**pane, "SWITCHBOARD_WORKTREE": "x"}, str(tmp)), "worker session")
        self.assertEqual(cos_autobind.refusal({**pane, "SWITCHBOARD_SURFACE": "launchpad"}, str(tmp)), "SWITCHBOARD_SURFACE=launchpad")
        self.assertEqual(cos_autobind.refusal({}, str(tmp)), "not in a cmux or wmux pane")
        (tmp / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.assertEqual(cos_autobind.refusal(pane, str(tmp)), "task worktree")
        self.assertEqual(cos_autobind.refusal({**pane, "SWITCHBOARD_SURFACE": "cos"}, str(tmp / "..")), "")


class BindTests(Harness):
    def test_wmux_start_of_day_binds_claims_and_dispatches(self) -> None:
        context, result = self.observe("start of day")
        self.assertTrue(result["bound"], result)
        reg = json.loads(self.registration.read_text(encoding="utf-8"))
        self.assertEqual(reg["workspace_id"], "ws-new")
        self.assertEqual(reg["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(reg["thread_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(reg["agent"], "claude")
        tabs = self.tabs()
        self.assertEqual(tabs["workspace:260"]["title"], "🧭 CoS")
        self.assertTrue(tabs["workspace:260"]["pinned"])
        self.assertEqual(tabs["workspace:260"]["thread"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(self.order()[0], "workspace:236")  # wmux: no tab ordering
        # The newest tab wins; the old CoS is renamed and unpinned, never closed.
        self.assertTrue(tabs["workspace:236"]["title"].startswith("🧭 CoS (old"))
        self.assertFalse(tabs["workspace:236"]["pinned"])
        self.assertNotIn("close-workspace", [c[0] for c in self.calls()])
        self.assertEqual(result["demoted"], ["workspace:236"])
        verbs = [c[0] for c in self.calls()]
        self.assertNotIn("sidebar", verbs)
        self.assertNotIn(["workspace-action", "--action", "move-top", "--workspace", "workspace:260"], self.calls())
        self.assertIn("router-dispatch.sh --origin cos \"start of day\"", context)
        self.assertIn("registered this tab as the CoS", context)

    def test_cos_phrase_boots_to_ready(self) -> None:
        context, result = self.observe("chief of staff")
        self.assertTrue(result["bound"])
        self.assertIn("reply only `Ready.`", context)
        self.assertNotIn("router-dispatch", context)

    def test_only_the_first_prompt_binds(self) -> None:
        _, first = self.observe("hello there")
        self.assertEqual(first["reason"], "no trigger")
        context, second = self.observe("CoS")
        self.assertEqual(context, "")
        self.assertEqual(second["reason"], "not the session's first prompt")
        self.assertFalse(self.registration.exists())
        self.assertEqual(self.tabs()["workspace:260"]["title"], "Workspace 7")

    def test_worker_tab_is_refused(self) -> None:
        context, result = self.observe("CoS", WMUX_SURFACE_ID="surf-worker")
        self.assertFalse(result["bound"])
        self.assertEqual(result["reason"], "worker tab")
        self.assertEqual(context, "")  # no fallback line sends a worker to cos-phase0.sh
        self.assertFalse(self.registration.exists())

    def test_unwritable_marker_never_claims(self) -> None:
        blocker = self.tmp / "autobind-file"
        blocker.write_text("not a folder", encoding="utf-8")
        context, result = self.observe("CoS", COS_AUTOBIND_STATE_DIR=str(blocker))
        self.assertEqual((context, result["reason"]), ("", "not the session's first prompt"))
        self.assertFalse(self.registration.exists())

    def test_bind_error_still_tells_the_model(self) -> None:
        # The registration path is a folder, so claim_cos cannot replace it (as when a sync client holds the file).
        self.registration.mkdir()
        context, result = self.observe("start of day")
        self.assertFalse(result["bound"])
        self.assertIn("could not register this tab", context)
        self.assertIn("router-dispatch.sh --origin cos", context)

    def test_two_tabs_at_once_leave_one_cos(self) -> None:
        second = {"ref": "workspace:261", "id": "ws-two", "title": "Workspace 8", "description": "", "pinned": False}
        self.write_tabs([dict(OLD_COS), dict(NEW_TAB), second], {"surf-new": "ws-new", "surf-two": "ws-two"})
        code = ("import json, sys; sys.path.insert(0, sys.argv[3]); import cos_autobind; "
                "print(json.dumps(cos_autobind.observe(sys.argv[1], sys.argv[2], cwd=sys.argv[4])[1]))")
        procs = []
        for surface, session in (("surf-new", "aaaaaaaa-0000-0000-0000-000000000001"),
                                 ("surf-two", "aaaaaaaa-0000-0000-0000-000000000002")):
            procs.append(subprocess.Popen([sys.executable, "-c", code, "CoS", session, str(HERE), str(self.cwd)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          encoding="utf-8", env=self.env(WMUX_SURFACE_ID=surface), cwd=str(self.cwd)))
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        titled = [ref for ref, tab in self.tabs().items() if tab["title"] == "🧭 CoS"]
        self.assertEqual(len(titled), 1, self.tabs())
        reg = json.loads(self.registration.read_text(encoding="utf-8"))
        self.assertEqual(self.tabs()[titled[0]]["id"], reg["workspace_id"])

    def test_fixed_surface_tab_keeps_its_role(self) -> None:
        launchpad = {"ref": "workspace:261", "id": "ws-lp", "title": "🚀 Launchpad", "description": "", "pinned": True}
        self.write_tabs([dict(OLD_COS), launchpad], {"surf-lp": "ws-lp"})
        context, result = self.observe("CoS", WMUX_SURFACE_ID="surf-lp")
        self.assertEqual((context, result["reason"]), ("", "launchpad tab"))
        self.assertEqual(self.tabs()["workspace:261"]["title"], "🚀 Launchpad")
        self.assertFalse(self.registration.exists())

    def test_worker_session_is_refused(self) -> None:
        context, result = self.observe("start of day", SWITCHBOARD_WORKER_ID="worker-1")
        self.assertEqual((context, result["reason"]), ("", "worker session"))
        self.assertEqual(self.calls(), [])

    def test_unresolved_pane_fails_closed_with_fallback(self) -> None:
        context, result = self.observe("CoS", WMUX_SURFACE_ID="surf-unknown")
        self.assertFalse(result["bound"])
        self.assertFalse(self.registration.exists())
        self.assertIn("cos-phase0.sh chrome --claim", context)

    def test_mac_cmux_path_claims_without_session_bind(self) -> None:
        _, result = self.observe("start of day", wmux=False)
        self.assertTrue(result["bound"], result)
        verbs = [c[0] for c in self.calls()]
        self.assertNotIn("session-bind", verbs)
        self.assertNotIn("workspace-id-of-surface", verbs)
        self.assertNotIn("sidebar", verbs)
        self.assertEqual(self.tabs()["workspace:260"]["title"], "🧭 CoS")
        self.assertEqual(self.order()[0], "workspace:260")

    def test_kill_switch(self) -> None:
        (self.tmp / "plugin-config.json").write_text(json.dumps({"cos-autobind.disabled": True}), encoding="utf-8")
        context, result = self.observe("start of day")
        self.assertEqual((context, result["reason"]), ("", "disabled"))
        self.assertEqual(self.calls(), [])


class HealTests(Harness):
    """A CoS claimed before autobind existed has a cos-surface.json that names no session,
    so the sweep cannot find CoS. A later
    prompt in the registered tab binds it once, silently. Wrong impls: retitle or pin again,
    bind a prompt typed in another tab, or check on every prompt."""

    SESSION = "99999999-8888-7777-6666-555555555555"

    def setUp(self) -> None:
        super().setUp()
        self.write_tabs([dict(OLD_COS), dict(NEW_TAB), dict(WORKER)],
                        {"surf-old": "ws-old", "surf-new": "ws-new", "surf-worker": "ws-worker"})
        self.registration.write_text(json.dumps({"workspace_id": "ws-old", "ref": "workspace:236", "agent": "",
                                                 "thread_id": "", "previous_title": "🧭 CoS"}), encoding="utf-8")

    def test_later_prompt_in_the_registered_tab_binds_its_session_once(self) -> None:
        context, result = self.observe("what's left today?", session=self.SESSION, WMUX_SURFACE_ID="surf-old")
        self.assertEqual(context, "")
        self.assertTrue(result["healed"] and result["bound"], result)
        self.assertEqual(self.tabs()["workspace:236"]["thread"], self.SESSION)
        saved = json.loads(self.registration.read_text(encoding="utf-8"))
        self.assertEqual((saved["thread_id"], saved["ref"]), (self.SESSION, "workspace:236"))
        self.assertFalse(any(c[0] in ("rename-workspace", "workspace-action") for c in self.calls()), self.calls())
        before = len(self.calls())
        _, again = self.observe("and tomorrow?", session=self.SESSION, WMUX_SURFACE_ID="surf-old")
        self.assertNotIn("healed", again)
        self.assertEqual(len(self.calls()), before, "a bound registration costs no multiplexer call")

    def test_a_failed_tab_lookup_is_retried_on_the_next_prompt(self) -> None:
        # Code review: the marker used to be written before the lookup, so one failure blocked the heal all day.
        _, first = self.observe("status?", session=self.SESSION, WMUX_SURFACE_ID="surf-unknown")
        self.assertNotIn("healed", first)
        _, second = self.observe("status?", session=self.SESSION, WMUX_SURFACE_ID="surf-old")
        self.assertTrue(second.get("healed") and second.get("bound"), second)

    def test_prompt_in_another_tab_binds_nothing(self) -> None:
        _, result = self.observe("hello there", WMUX_SURFACE_ID="surf-new")
        self.assertEqual(result["reason"], "no trigger")
        self.assertEqual(json.loads(self.registration.read_text(encoding="utf-8"))["thread_id"], "")
        self.assertFalse(any(c[0] == "session-bind" for c in self.calls()))

    def test_a_registration_with_a_session_is_left_alone(self) -> None:
        self.registration.write_text(json.dumps({"workspace_id": "ws-old", "ref": "workspace:236",
                                                 "thread_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}), encoding="utf-8")
        _, result = self.observe("status?", session=self.SESSION, WMUX_SURFACE_ID="surf-old")
        self.assertNotIn("healed", result)
        self.assertEqual(self.calls(), [])


class HookTests(Harness):
    def run_hook(self, kind: str, event: dict, **env_kw) -> subprocess.CompletedProcess:
        env = self.env(**env_kw)
        env["SWITCHBOARD_REPO"] = str(ROOT)
        proc = subprocess.run([sys.executable, str(HOOK), kind], input=json.dumps(event), capture_output=True,
                              text=True, encoding="utf-8", env=env, cwd=str(self.cwd), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_first_prompt_hook_injects_binding_and_cos_card(self) -> None:
        proc = self.run_hook("user-prompt-submit", {"session_id": "s-1", "prompt": "start of day",
                                                     "cwd": str(self.cwd)})
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        context = out["additionalContext"]
        self.assertIn("registered this tab as the CoS", context)
        self.assertIn("router-dispatch.sh --origin cos", context)
        self.assertIn("CoS", context)  # the dispatch card follows the binding line
        self.assertEqual(json.loads(self.registration.read_text(encoding="utf-8"))["workspace_id"], "ws-new")

    def test_later_prompt_in_registered_wmux_tab_gets_reminder(self) -> None:
        self.run_hook("user-prompt-submit", {"session_id": "s-2", "prompt": "CoS", "cwd": str(self.cwd)})
        proc = self.run_hook("user-prompt-submit", {"session_id": "s-2", "prompt": "what's next?",
                                                     "cwd": str(self.cwd)})
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("switchboard CoS reminder", context)
        self.assertNotIn("registered this tab", context)

    def test_plain_prompt_in_plain_tab_prints_nothing(self) -> None:
        proc = self.run_hook("user-prompt-submit", {"session_id": "s-3", "prompt": "hello", "cwd": str(self.cwd)})
        self.assertEqual(proc.stdout.strip(), "")
        self.assertFalse(self.registration.exists())

    def test_session_start_exports_pane_workspace_on_wmux(self) -> None:
        env_file = self.tmp / "claude-env.sh"
        env_file.write_text("", encoding="utf-8")
        self.run_hook("session-start", {"session_id": "s-4", "source": "startup"}, CLAUDE_ENV_FILE=str(env_file))
        self.assertIn("export CMUX_WORKSPACE_ID=ws-new", env_file.read_text(encoding="utf-8"))

    def test_resumed_session_in_another_tab_finds_its_new_tab(self) -> None:
        other = {"ref": "workspace:262", "id": "ws-other", "title": "Workspace 9", "description": "", "pinned": False}
        self.write_tabs([dict(OLD_COS), dict(NEW_TAB), other], {"surf-new": "ws-new", "surf-other": "ws-other"})
        self.run_hook("user-prompt-submit", {"session_id": "s-6", "prompt": "CoS", "cwd": str(self.cwd)})
        env_file = self.tmp / "claude-env.sh"
        env_file.write_text("", encoding="utf-8")
        self.run_hook("session-start", {"session_id": "s-6", "source": "resume"}, CLAUDE_ENV_FILE=str(env_file),
                      WMUX_SURFACE_ID="surf-other")
        exported = env_file.read_text(encoding="utf-8")
        self.assertIn("export CMUX_WORKSPACE_ID=ws-other", exported)
        self.assertNotIn("DISPATCH_ORIGIN=cos", exported)
        proc = self.run_hook("user-prompt-submit", {"session_id": "s-6", "prompt": "hello", "cwd": str(self.cwd)},
                             WMUX_SURFACE_ID="surf-other")
        self.assertEqual(proc.stdout.strip(), "")

    def test_session_start_skips_workers(self) -> None:
        env_file = self.tmp / "claude-env.sh"
        env_file.write_text("", encoding="utf-8")
        self.run_hook("session-start", {"session_id": "s-5"}, CLAUDE_ENV_FILE=str(env_file), SWITCHBOARD_WORKER_ID="w-1")
        self.assertEqual(env_file.read_text(encoding="utf-8"), "")


class RemoteControlReminderTests(Harness):
    """A hand-typed `claude` that becomes CoS reminds the owner
    once to type /rc; a CoS launched with --remote-control, or one whose transcript shows
    Remote Control on, hears nothing. Wrong impls: remind every prompt, remind a scripted
    launch, remind when the transcript cannot be read, or remind a tab that did not bind."""

    BRIDGE = {"type": "bridge-session", "sessionId": "s-rc", "bridgeSessionId": "cse_fake"}
    run_hook = HookTests.run_hook

    def transcript(self, *records: dict) -> Path:
        path = self.tmp / "transcript.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def context(self, prompt: str, transcript: Path | str, session: str = "s-rc", **env_kw) -> str:
        proc = self.run_hook("user-prompt-submit", {"session_id": session, "prompt": prompt, "cwd": str(self.cwd),
                                                     "transcript_path": str(transcript)}, **env_kw)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout.strip() else ""

    def test_hand_typed_cos_without_remote_control_gets_one_reminder(self) -> None:
        path = self.transcript({"type": "mode"}, {"type": "permission-mode"})
        first = self.context("CoS", path)
        self.assertIn("registered this tab as the CoS", first)
        self.assertIn("type /rc once in this tab", first)
        self.assertNotIn("type /rc", self.context("what's next?", path))

    def test_start_of_day_reminder_follows_the_dispatch_step(self) -> None:
        context = self.context("start of day", self.transcript({"type": "mode"}))
        self.assertLess(context.index("router-dispatch.sh"), context.index("type /rc"))

    def test_scripted_launch_hears_nothing(self) -> None:
        context = self.context("CoS", self.transcript({"type": "mode"}), SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH="1")
        self.assertIn("registered this tab as the CoS", context)
        self.assertNotIn("/rc", context)

    def test_remote_control_on_in_transcript_hears_nothing(self) -> None:
        context = self.context("CoS", self.transcript({"type": "mode"}, self.BRIDGE,
                                                      {"type": "system", "subtype": "bridge_status"}))
        self.assertNotIn("/rc", context)

    def test_unreadable_or_missing_transcript(self) -> None:
        self.assertIsNone(cos_autobind.remote_control_on("", {}))
        self.assertIs(cos_autobind.remote_control_on(str(self.tmp / "not-yet.jsonl"), {}), False)
        self.assertIsNone(cos_autobind.remote_control_on(str(self.tmp), {}))  # a folder: OSError
        self.assertNotIn("/rc", self.context("CoS", ""))

    def test_remote_control_switched_off_hears_nothing(self) -> None:
        context = self.context("CoS", self.transcript({"type": "mode"}), SWITCHBOARD_COS_REMOTE_CONTROL="off")
        self.assertIn("registered this tab as the CoS", context)
        self.assertNotIn("/rc", context)

    def test_unbound_tab_hears_no_reminder(self) -> None:
        context = cos_autobind.context_line(cos_autobind.COS, {"bound": False, "reason": "x"}, "CoS", False)
        self.assertNotIn("/rc", context)


if __name__ == "__main__":
    unittest.main()
