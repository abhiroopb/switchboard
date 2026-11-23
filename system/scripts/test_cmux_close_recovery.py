#!/usr/bin/env python3
"""Local fixtures for close capture, subscriber recovery, and task relocation."""
import importlib.util
import io
import json
import os
os.environ.pop("SWITCHBOARD_ROOT", None)  # these tests pin the default ~/Development/switchboard layout
import re
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("recorder", ROOT / "daemon/cmux-closed-workspaces.py")
recorder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recorder)


def path_forms(path: Path) -> list[str]:
    """Every spelling of `path` an installed bash wrapper may carry. Git Bash's `pwd`
    gives the /c/... POSIX form for a C: drive path, and that form is correct inside a bash wrapper."""
    forms = [str(path), path.as_posix()]
    drive, rest = os.path.splitdrive(str(path))
    if drive:
        forms.append("/" + drive[0].lower() + rest.replace("\\", "/"))
    # A path with a space (a Dropbox "switchboard" folder) is written shell-escaped,
    # as bash `printf %q` or `shlex.quote` spell it.
    forms += [re.sub(r"([^A-Za-z0-9_./:@%+=,-])", r"\\\1", f) for f in forms] + [shlex.quote(f) for f in forms]
    return forms


def frame(**kwargs):
    return json.dumps(kwargs) + "\n"


class RecorderTests(unittest.TestCase):
    def setUp(self):
        recorder.snapshot.clear()
        recorder.last_seen_at.clear()
        recorder.last_pinned_at.clear()
        self.saved = patch.object(recorder, "SESSION_STORE", "/nonexistent/cmux-session.json")
        self.saved.start()
        self.addCleanup(self.saved.stop)

    def test_session_fallback_joins_exact_surface_and_keeps_live_binding(self):
        tid = "T-00000000-0000-4000-8000-00000000c105"
        other_tid = "T-00000000-0000-4000-8000-00000000c106"
        def panel(sid, **terminal):
            return {"id": sid, "directory": "/tmp/space dir", "terminal": terminal}
        data = {"windows": [{"tabManager": {"workspaces": [
            {"workspaceId": "old", "panels": [
                panel("live", resumeBinding={"command": "stale"}),
                panel("saved", resumeBinding={"command": "saved-command", "checkpointId": other_tid}),
                panel("agent", agent={"kind": "amp", "sessionId": tid}),
                panel("remote", isRemoteTerminal=True, agent={"kind": "amp", "sessionId": tid}),
                panel("invalid", agent={"kind": "amp", "sessionId": "not-a-thread"}),
            ]},
            {"workspaceId": "different", "panels": [panel("unbound", agent={"kind": "amp", "sessionId": tid})]},
        ]}}]}
        surfaces = [{"id": sid, "type": "terminal"} for sid in
                    ("live", "saved", "agent", "unbound", "remote", "invalid")]
        surfaces[0]["resume_binding"] = {"command": "live-command", "checkpoint_id": "T-live"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(json.dumps(data))
            with patch.object(recorder, "SESSION_STORE", str(path)), \
                    patch.object(recorder, "rpc", side_effect=[
                        {"workspaces": [{"id": "old", "ref": "workspace:27"}]}, {"surfaces": surfaces}]), \
                    patch.object(recorder.subprocess, "run", return_value=Mock(returncode=0, stdout="")) as run:
                recorder.refresh()
            entry = recorder.snapshot["old"]
            expected = ["live-command", "saved-command", f"cd -- '/tmp/space dir' && amp threads continue {tid}"]
            self.assertEqual(entry["resume_commands"], expected)
            self.assertEqual([s["command"] for s in entry["surfaces"]], expected + [None, None, None])
            self.assertEqual(entry["thread_ids"], ["T-live", other_tid, tid])
            self.assertEqual(json.loads(run.call_args.kwargs["input"])[-1],
                             {"thread_id": tid, "workspace_ref": "workspace:27"})

    def test_missing_or_partial_session_store_is_optional(self):
        self.assertEqual(recorder.saved_surface_bindings(), {})
        with tempfile.NamedTemporaryFile(mode="w") as f:
            f.write('{"windows":')
            f.flush()
            with patch.object(recorder, "SESSION_STORE", f.name):
                self.assertEqual(recorder.saved_surface_bindings(), {})

    def test_subscription_retires_and_resumes_across_boots(self):
        streams = [
            frame(type="ack", boot_id="old", resume={"latest_seq": 800})
            + frame(name="workspace.closed", seq=805, payload={"workspace_id": "one"}),
            frame(type="ack", boot_id="new", resume={"latest_seq": 4}),
            frame(type="ack", boot_id="new", resume={"latest_seq": 4})
            + frame(name="workspace.closed", seq=3, payload={"workspace_id": "two"}),
        ]
        procs = [Mock(stdout=io.StringIO(s)) for s in streams]
        for proc in procs:
            proc.poll.return_value = None
        with patch.object(recorder.threading, "Thread"), \
                patch.object(recorder.threading, "Timer") as timer, \
                patch.object(recorder.subprocess, "Popen", side_effect=procs) as popen, \
                patch.object(recorder, "record") as record, \
                patch.object(recorder.time, "sleep", side_effect=[None, None, InterruptedError]):
            with self.assertRaises(InterruptedError):
                recorder.main()
        commands = [c.args[0] for c in popen.call_args_list]
        self.assertNotIn("--reconnect", commands[0])
        self.assertNotIn("--after", commands[0])
        self.assertEqual(commands[1][-2:], ["--after", "805"])
        self.assertEqual(commands[2][-2:], ["--after", "0"])
        self.assertEqual([c.args[0] for c in record.call_args_list], ["one", "two"])
        for call, proc in zip(timer.call_args_list, procs):
            self.assertEqual(call.args, (60, proc.kill))
            proc.kill.assert_called_once()
            proc.wait.assert_called_once()

    def test_disappeared_workspace_keeps_recipe_but_not_live_binding(self):
        workspace = {"id": "old", "ref": "workspace:27", "title": "Garden", "pinned": True}
        surface = {"type": "terminal", "resume_binding": {
            "command": "resume-known-thread", "checkpoint_id": "T-known"}}
        with patch.object(recorder, "rpc", side_effect=[
            {"workspaces": [workspace]}, {"surfaces": [surface]},
            {"workspaces": []}, {"workspaces": []},
        ]), patch.object(recorder.subprocess, "run", return_value=Mock(returncode=0)) as run, \
                patch.object(recorder.time, "monotonic", side_effect=[100, 120, 221]):
            recorder.refresh()
            recorder.refresh()
            self.assertEqual(recorder.snapshot["old"]["resume_commands"], ["resume-known-thread"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), [])
            recorder.refresh()
            self.assertNotIn("old", recorder.snapshot)

    def test_record_preserves_recipe_and_consumes_launcher_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.multiple(recorder, STACK=str(root / "stack.json"),
                                STACK_LOCK=str(root / "stack.lock"),
                                SKIP_FILE=str(root / "skip"), CONTEXT_DIR=str(root / "context")), \
                    patch.object(recorder, "save_to_amp_mem"):
                recorder.snapshot["old"] = {"title": "Garden", "resume_commands": ["resume-known-thread"],
                                             "surfaces": [{"type": "terminal", "command": "resume-known-thread"}]}
                recorder.record("old", {})
                stack = json.loads((root / "stack.json").read_text())
                self.assertEqual(stack[-1]["surfaces"][0]["command"], "resume-known-thread")
                (root / "skip").write_text("launcher\n")
                recorder.record("launcher", {})
                self.assertEqual(json.loads((root / "stack.json").read_text()), stack)
                self.assertEqual((root / "skip").read_text(), "")


class SidebarRecoveryTests(unittest.TestCase):
    def test_installed_wrappers_pop_switchboard_stack_not_retired_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / "Development/switchboard/.state"
            state.mkdir(parents=True)
            stack = state / "cmux-recently-closed.json"
            older = {"title": "older", "resume_commands": ["/bin/echo WRONG-OLDER"]}
            stack.write_text(json.dumps([older, {"title": "latest", "resume_commands": ["/bin/echo CORRECT-LATEST"]}]))
            retired = home / ".local/state/cmux-sidebar/recently-closed.json"
            retired.parent.mkdir(parents=True)
            retired.write_text('[{"title":"July"}]')
            env = {**os.environ, "HOME": tmp}
            env.pop("CMUX_WORKSPACE_ID", None)
            installer = ROOT / "system/scripts/install-cmux-sidebar.sh"
            helper_dir = home / ".config/cmux-sidebar/scripts"
            for _ in range(2):
                helper_dir.mkdir(parents=True, exist_ok=True)
                for name in ("cmux-reopen-last-closed.sh", "cmux-relaunch-workspace.sh", "cmux-rename-workspace.sh"):
                    (helper_dir / name).write_text("retired installation")
                result = subprocess.run(["bash", str(installer), "--actions-only"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in ("cmux-reopen-last-closed.sh", "cmux-relaunch-workspace.sh", "cmux-rename-workspace.sh"):
                    wrapper = (helper_dir / name).read_text()
                    self.assertTrue(any(form in wrapper for form in path_forms(ROOT / "system/scripts" / name)), wrapper)
            result = subprocess.run(["bash", str(helper_dir / "cmux-reopen-last-closed.sh"), "--exec"],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CORRECT-LATEST", result.stdout)
            self.assertNotIn("WRONG-OLDER", result.stdout)
            self.assertEqual(json.loads(stack.read_text()), [older])
            self.assertTrue((state / "cmux-recently-closed.lock").exists())
            self.assertEqual(retired.read_text(), '[{"title":"July"}]')
            self.assertEqual(Path(recorder.STACK).relative_to(Path.home()), stack.relative_to(home))
            self.assertEqual(Path(recorder.STACK_LOCK).relative_to(Path.home()),
                             (state / "cmux-recently-closed.lock").relative_to(home))

    def test_reopen_launcher_writes_recorder_skip_path_before_close(self):
        source = (ROOT / "system/scripts/cmux-reopen-last-closed.sh").read_text()
        action = source.split("ACTION=$(python3 <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "Development/switchboard/.state"
            state.mkdir(parents=True)
            skip = state / "cmux-skip-record"
            self.assertEqual(Path(recorder.SKIP_FILE).relative_to(Path.home()), skip.relative_to(Path(tmp)))
            entry = {"surfaces": [{"type": "terminal"}, {"type": "terminal"}]}
            def run(args, **kwargs):
                if "workspace.close" in args:
                    self.assertEqual(skip.read_text(), "launcher\n")
                return Mock(returncode=0, stdout="OK workspace:999", stderr="")
            with patch.dict(os.environ, HOME=tmp, USERPROFILE=tmp, ENTRY=json.dumps(entry), CMUX_WORKSPACE_ID="launcher"), \
                    patch("subprocess.run", side_effect=run), patch("sys.stdout", new_callable=io.StringIO):
                exec(compile(action, "reopen-action", "exec"), {})
            with patch.object(recorder, "SKIP_FILE", str(skip)):
                self.assertFalse(recorder.consume_skip("real-workspace"))
                self.assertTrue(recorder.consume_skip("launcher"))
                self.assertFalse(recorder.consume_skip("launcher"))


    def test_reopen_launcher_imports_fcntl_through_the_windows_fallback(self):
        # Windows has no fcntl; both inline Python blocks must fall back to flock_compat.
        source = (ROOT / "system/scripts/cmux-reopen-last-closed.sh").read_text()
        blocks = [part.split("\nPY\n", 1)[0] for part in source.split("<<'PY'\n")[1:]]
        self.assertEqual(len(blocks), 3)   # peek, pop, action
        locking = [b for b in blocks if "fcntl.flock" in b]
        self.assertEqual(len(locking), 2)
        for block in locking:
            header = block.split("fcntl.flock", 1)[0]
            self.assertIn("try:\n    import fcntl\nexcept ImportError", header)
            self.assertIn("import flock_compat as fcntl", header)
            self.assertNotIn("import fcntl,", header)
        # The action block still runs when the real fcntl cannot be imported.
        action = source.split("ACTION=$(python3 <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"surfaces": [{"type": "terminal"}]}
            with patch.dict(os.environ, HOME=tmp, USERPROFILE=tmp, ENTRY=json.dumps(entry), SWITCHBOARD_SCRIPTS_DIR=str(ROOT / "system/scripts")), \
                    patch.dict(sys.modules, {"fcntl": None}), patch.dict(sys.modules), \
                    patch("subprocess.run", return_value=Mock(returncode=0, stdout="OK workspace:1", stderr="")), \
                    patch("sys.stdout", new_callable=io.StringIO):
                sys.modules.pop("flock_compat", None)
                exec(compile(action, "reopen-action", "exec"), {})
                self.assertIn("flock_compat", sys.modules)


class LedgerLocationTests(unittest.TestCase):
    def test_sync_changes_only_unambiguous_active_thread_locations(self):
        def task(tid, status="active", role="worker"):
            return {"id": tid, "status": status, "thread_id": tid, "workstream": "garden-revamp",
                    "attempt_id": "keep-attempt", "heartbeat_at": "keep-heartbeat", "summary": "keep-summary",
                    "workstream_workspace_ref": "workspace:27", "router_workspace_ref": "workspace:31",
                    "executor": {"thread_id": tid, "workspace_ref": "workspace:27", "role": role}}

        tasks = [task("T-reopened"), task("T-done", "done"), task("T-absent"),
                 task("T-ambiguous"), task("T-router", role="router")]
        bindings = [{"thread_id": t, "workspace_ref": w} for t, w in [
            ("T-reopened", "workspace:32"), ("T-done", "workspace:32"),
            ("T-ambiguous", "workspace:32"), ("T-ambiguous", "workspace:33"),
            ("T-router", "workspace:31"), ("T-unrelated", "workspace:27"),
        ]]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(json.dumps({"tasks": tasks}))
            env = {**os.environ, "RUNNING_TASKS_FILE": str(path)}
            command = [sys.executable, str(ROOT / "system/scripts/running-task-state.py"), "sync-workspaces"]
            result = subprocess.run(command, input=json.dumps(bindings), env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["changed"], [
                {"task_id": "T-reopened", "thread_id": "T-reopened", "workspace_ref": "workspace:32"}])
            actual = {t["id"]: t for t in json.loads(path.read_text())["tasks"]}
            reopened = actual["T-reopened"]
            # Current ledger schema: one top-level workspace_ref; the legacy
            # executor/workstream_workspace_ref fields are read-only fallbacks.
            self.assertEqual(reopened["workspace_ref"], "workspace:32")
            self.assertEqual(reopened["executor"]["workspace_ref"], "workspace:27")
            for field in ("status", "attempt_id", "heartbeat_at", "summary"):
                self.assertEqual(reopened[field], tasks[0][field])
            for t in tasks[1:]:
                self.assertEqual(actual[t["id"]], t)
            before = path.stat().st_mtime_ns
            result = subprocess.run(command, input=json.dumps(bindings), env=env, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), {"changed": []})
            self.assertEqual(path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
