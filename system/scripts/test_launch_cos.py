#!/usr/bin/env python3
"""CoS always runs with Remote Control on; nothing else does.

Every scripted CoS launch, including the auto-start at sign-in, runs with
--remote-control, and the global Claude Code setting stays unchanged.

Covers cmux-helpers.sh (start-day's CoS line, the routine launch and resume lines),
dispatch.py worker lines, system/scripts/launch-cos.sh against a stub multiplexer, and the
com.pm-os.cos-autostart sign-in task. Wrong impls this catches: the flag on a worker,
routine, or Amp tab; a flag with no name that swallows the seed prompt; a launcher that
opens a second CoS beside a live one; a sign-in task that is missing or kept alive.
PM_OS_TEST_SANDBOX=1 and a stub CMUX_BIN: no live tab is ever reached.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HELPERS = ROOT / "system" / "cmux-helpers.sh"
LAUNCH = ROOT / "system" / "scripts" / "launch-cos.sh"
TASKS = ROOT / "system" / "windows" / "register-scheduled-tasks.ps1"
sys.path.insert(0, str(HERE))

BASH = shutil.which("bash") or "bash"
SESSION = "12345678-aaaa-4bbb-8ccc-1234567890ab"  # invented

# The multiplexer stand-in: tabs in STUB_STATE, every call logged to STUB_LOG.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = json.load(open(os.environ["STUB_STATE"], encoding="utf-8"))
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
def flag(name):
    return args[args.index(name) + 1] if name in args else None
cmd = args[0] if args else ""
if cmd in ("ping",):
    print("pong")
elif cmd == "rpc":
    print(json.dumps({"workspaces": state["tabs"]}))
elif cmd == "list-workspaces":
    print("\n".join(t["ref"] + " " + t["title"] for t in state["tabs"]))
elif cmd == "read-screen":
    print(state.get("screens", {}).get(flag("--workspace"), ""))
elif cmd == "new-workspace":
    print("OK workspace:999")
else:
    sys.exit(2)
'''

READY = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)"


def helper(expr: str, **env: str) -> str:
    """Run `expr` in a bash that sourced cmux-helpers.sh; returns stdout."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("PM_OS_COS", "CMUX", "WMUX"))}
    base.update({"PM_OS_TEST_SANDBOX": "1", "PM_OS_AGENT": "claude", "REPO_ROOT": str(ROOT).replace("\\", "/"),
                 "PM_OS_LOCAL_DIR": env.pop("PM_OS_LOCAL_DIR", str(ROOT / "no-such-local")), "PM_OS_MACHINE": "TESTBOX"})
    base.update(env)
    proc = subprocess.run([BASH, "-c", f'source "{HELPERS.as_posix()}" >/dev/null 2>&1; {expr}'],
                          capture_output=True, text=True, encoding="utf-8", env=base, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class HelperLines(unittest.TestCase):
    def test_start_day_cos_line_has_named_remote_control(self) -> None:
        line = helper("agent_interactive_command ultra megadesk")
        self.assertTrue(line.startswith("PM_OS_COS_REMOTE_CONTROL_LAUNCH=1 claude "), line)
        self.assertTrue(line.rstrip().endswith("--remote-control CoS"), line)
        self.assertIn("--dangerously-skip-permissions --model opus --effort high", line)

    def test_other_surfaces_get_nothing(self) -> None:
        for expr in ("agent_interactive_command ultra", "agent_interactive_command ultra todo",
                     "agent_interactive_command ultra launchpad",
                     'local_amp_launch_command "$REPO_ROOT/routines/todo" ultra todo',
                     'local_amp_launch_command "$REPO_ROOT/workstreams/pm-os-maintenance" ultra pm-os-maintenance',
                     f'local_agent_resume_command "$REPO_ROOT/routines/todo" ultra {SESSION} todo'):
            line = helper(expr)
            self.assertNotIn("--remote-control", line, expr)
            self.assertNotIn("PM_OS_COS_REMOTE_CONTROL_LAUNCH", line, expr)

    def test_routine_launch_keeps_the_seed_prompt_after_the_name(self) -> None:
        line = helper('PM_OS_LAUNCH_SEED_FILE="" local_amp_launch_command "$REPO_ROOT/routines/megadesk" ultra megadesk')
        match = re.search(r"--remote-control CoS (\S.*)$", line.strip())
        self.assertIsNotNone(match, line)
        self.assertNotRegex(match.group(1), r"^--", "the seed prompt must follow the explicit name")
        self.assertIn("PM_OS_COS_REMOTE_CONTROL_LAUNCH=1 PM_OS_WORKSTREAM_SLUG=", line)

    def test_routine_resume_carries_the_flag(self) -> None:
        line = helper(f'local_agent_resume_command "$REPO_ROOT/routines/megadesk" ultra {SESSION} megadesk')
        self.assertIn(f"--resume {SESSION}", line)
        self.assertIn("--remote-control CoS", line)

    def test_kill_switch_and_amp(self) -> None:
        for value in ("off", "OFF", "0", "false", "no"):
            self.assertNotIn("--remote-control", helper("agent_interactive_command ultra megadesk",
                                                        PM_OS_COS_REMOTE_CONTROL=value))
        self.assertEqual(helper("agent_interactive_command ultra megadesk", PM_OS_AGENT="amp").strip(), "amp -m ultra")

    def test_kill_switch_in_the_machine_file(self) -> None:
        local = Path(tempfile.mkdtemp(prefix="launch-cos-local-"))
        self.addCleanup(shutil.rmtree, local, True)
        (local / "machines").mkdir()
        (local / "machines" / "TESTBOX.env").write_text("PM_OS_COS_REMOTE_CONTROL=off\n", encoding="utf-8")
        self.assertNotIn("--remote-control", helper("agent_interactive_command ultra megadesk", PM_OS_LOCAL_DIR=str(local)))
        # The environment still wins over the file.
        self.assertIn("--remote-control CoS", helper("agent_interactive_command ultra megadesk",
                                                     PM_OS_LOCAL_DIR=str(local), PM_OS_COS_REMOTE_CONTROL="on"))

    def test_worker_lines_never_get_remote_control(self) -> None:
        import dispatch
        row = {"id": "rt-x", "attempt_id": "rt-x.a1.000000", "workstream": "pm-os-maintenance", "worker_id": "w-1",
               "worktree": str(ROOT), "mode": "ultra", "agent": "claude", "session_id": SESSION}
        self.assertNotIn("--remote-control", dispatch.launch_command_for(row))
        self.assertNotIn("--remote-control", dispatch.launch_command_for({**row, "workstream": "megadesk"}))


class Launcher(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="launch-cos-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "cmux"
        self.stub.write_text(STUB, encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "calls.log"
        self.registration = self.tmp / "cos-surface.json"
        self.local = self.tmp / "local"
        (self.local / "machines").mkdir(parents=True)

    def opened(self) -> list[list[str]]:
        return [c for c in self.calls() if c and c[0] == "new-workspace"]

    def tabs(self, tabs: list[dict], screens: dict | None = None) -> None:
        self.state.write_text(json.dumps({"tabs": tabs, "screens": screens or {}}), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def run_launch(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PM_OS_COS", "CMUX", "WMUX", "CLAUDE_"))}
        env.update({"PM_OS_TEST_SANDBOX": "1", "PM_OS_AGENT": "claude", "CMUX_BIN": str(self.stub).replace("\\", "/"),
                    "STUB_STATE": str(self.state), "STUB_LOG": str(self.log), "COS_SURFACE_FILE": str(self.registration),
                    "CC_CMUX_SESSION_JSON": str(self.tmp / "no-store.json"), "PM_OS_PYTHON": sys.executable.replace("\\", "/"),
                    "PM_OS_STATE_DIR": str(self.tmp / "st"), "PYTHONIOENCODING": "utf-8",
                    "COS_RESTORE_WAIT_SECONDS": "0", "COS_MUX_WAIT_SECONDS": "0",
                    "LAUNCH_COS_LOCK_DIR": str(self.tmp / "lock"), "PM_OS_LOCAL_DIR": str(self.local),
                    "PM_OS_MACHINE": "TESTBOX"})
        env.update(extra)
        return subprocess.run([BASH, LAUNCH.as_posix(), *args], capture_output=True, text=True, encoding="utf-8",
                              env=env, cwd=str(ROOT), timeout=120)

    def test_dry_run_prints_the_cos_line(self) -> None:
        proc = self.run_launch("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip()
        self.assertIn("PM_OS_COS_REMOTE_CONTROL_LAUNCH=1 claude --dangerously-skip-permissions", line)
        self.assertTrue(line.endswith("--remote-control CoS CoS"), line)  # explicit name, then the CoS seed
        self.assertEqual(self.calls(), [])

    def test_cos_opens_in_the_main_checkout_from_a_worktree(self) -> None:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(ROOT),
                                capture_output=True, text=True, encoding="utf-8").stdout.strip()
        main = str(Path(common).parent).replace("\\", "/")
        line = self.run_launch("--dry-run").stdout.strip()
        self.assertTrue(line.startswith(f"cd {main} && "), line)
        self.assertIn(f"--settings {main}/system/config/claude-tab-settings.json", line)

    def test_live_cos_is_kept(self) -> None:
        self.tabs([{"ref": "workspace:285", "id": "ws-cos", "title": "\U0001f9ed CoS", "description": ""}],
                  {"workspace:285": READY})
        self.registration.write_text(json.dumps({"workspace_id": "ws-cos", "ref": "workspace:285"}), encoding="utf-8")
        proc = self.run_launch("--if-missing")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("kept: CoS is already live in workspace:285", proc.stdout)
        self.assertFalse(any(c and c[0] == "new-workspace" for c in self.calls()), self.calls())

    def test_missing_cos_opens_one_with_remote_control(self) -> None:
        self.tabs([{"ref": "workspace:12", "id": "ws-todo", "title": "✅ To Do", "description": ""}])
        proc = self.run_launch("--if-missing")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        new = [c for c in self.calls() if c and c[0] == "new-workspace"]
        self.assertEqual(len(new), 1, self.calls())
        command = new[0][new[0].index("--command") + 1]
        self.assertIn("--remote-control CoS CoS", command)
        self.assertEqual(new[0][new[0].index("--focus") + 1], "false")
        self.assertNotIn("CoS", new[0][new[0].index("--name") + 1].replace("CoS starting", ""))

    def test_dead_cos_tab_gets_a_fresh_cos(self) -> None:
        self.tabs([{"ref": "workspace:285", "id": "ws-cos", "title": "\U0001f9ed CoS", "description": ""}],
                  {"workspace:285": "user@laptop MINGW64 ~\n$ "})
        self.registration.write_text(json.dumps({"workspace_id": "ws-cos", "ref": "workspace:285"}), encoding="utf-8")
        proc = self.run_launch("--if-missing")
        self.assertIn("shows no live Claude session", proc.stdout)
        self.assertEqual(len([c for c in self.calls() if c and c[0] == "new-workspace"]), 1)

    def test_autostart_kill_switch(self) -> None:
        proc = self.run_launch("--at-logon", PM_OS_COS_AUTOSTART="off")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("skipped: PM_OS_COS_AUTOSTART=off", proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_autostart_kill_switch_in_the_machine_file(self) -> None:
        (self.local / "machines" / "TESTBOX.env").write_text("PM_OS_COS_AUTOSTART=off\n", encoding="utf-8")
        proc = self.run_launch("--at-logon")
        self.assertIn("skipped: PM_OS_COS_AUTOSTART=off", proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_restoring_wmux_with_no_tabs_opens_nothing(self) -> None:
        self.tabs([])  # wmux answers but has not recreated its tabs yet
        proc = self.run_launch("--at-logon")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("opening nothing so a restoring CoS is never doubled", proc.stdout)
        self.assertEqual(self.opened(), [])

    def test_exited_claude_frame_is_not_live(self) -> None:
        dead = READY + "\n\nResume this session with:\nclaude --resume x\n\n$ "
        self.tabs([{"ref": "workspace:285", "id": "ws-cos", "title": "\U0001f9ed CoS", "description": ""}],
                  {"workspace:285": dead})
        self.registration.write_text(json.dumps({"workspace_id": "ws-cos", "ref": "workspace:285"}), encoding="utf-8")
        self.assertIn("shows no live Claude session", self.run_launch("--if-missing").stdout)
        self.assertEqual(len(self.opened()), 1)

    def test_second_run_inside_the_lock_window_opens_nothing(self) -> None:
        self.tabs([{"ref": "workspace:12", "id": "ws-todo", "title": "✅ To Do", "description": ""}])
        self.run_launch("--if-missing")
        proc = self.run_launch("--if-missing")
        self.assertIn("skipped: another CoS launch started", proc.stdout)
        self.assertEqual(len(self.opened()), 1)
        expired = self.run_launch("--if-missing", LAUNCH_COS_LOCK_SECONDS="0")
        self.assertIn("opened CoS with Remote Control", expired.stdout)

    def test_bad_wait_value_fails_fast(self) -> None:
        proc = self.run_launch("--at-logon", COS_RESTORE_WAIT_SECONDS="ninety")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.calls(), [])


class SignInTask(unittest.TestCase):
    def test_cos_autostart_is_a_one_shot_logon_task(self) -> None:
        text = TASKS.read_text(encoding="utf-8")
        job = re.search(r'@\{ Label = "com\.pm-os\.cos-autostart";[^\n]*\}', text)
        self.assertIsNotNone(job, "com.pm-os.cos-autostart is not in the task list")
        self.assertIn('Cmd = "bash system/scripts/launch-cos.sh --at-logon"', job.group(0))
        self.assertIn('Kind = "logon"', job.group(0))
        self.assertRegex(text, r'"logon"\s*\{\s*\n\s*\$t = New-ScheduledTaskTrigger -AtLogOn -User')
        # One run per sign-in: only daemon jobs restart and start at registration.
        self.assertRegex(text, r'if \(\$job\.Kind -eq "daemon"\) \{\s*\n\s*# KeepAlive')
        self.assertIn('if ($job.Kind -eq "daemon") { Start-ScheduledTask', text)


if __name__ == "__main__":
    unittest.main()
