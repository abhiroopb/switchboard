#!/usr/bin/env python3
"""Regression tests for the Windows process-rate cuts.

A kernel leak on Windows keeps every exited process until reboot, so a process start has a real cost
there. The budget tests fail when a fork returns to a path that runs constantly. Other tests pin the guards that
keep macOS path unchanged. They read files and start no live tab.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import windows_leak_trace as trace  # noqa: E402
import windows_process_rate as rate  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def git_bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


WINDOWS = os.name == "nt"


class CensusTests(unittest.TestCase):
    ROWS = {
        10: {"image": "wmux.exe", "ppid": 1, "alive": True, "created": 100.0},
        20: {"image": "bash.exe", "ppid": 10, "alive": False, "created": 200.0},
        30: {"image": "git.exe", "ppid": 20, "alive": False, "created": 210.0},
        # Parent 40 started after its child, so that PID was reused and is not an ancestor.
        40: {"image": "node.exe", "ppid": 10, "alive": True, "created": 500.0},
        50: {"image": "git.exe", "ppid": 40, "alive": False, "created": 300.0},
    }

    def test_first_live_ancestor_names_the_tree(self):
        self.assertEqual(rate.live_root(30, self.ROWS), "wmux.exe")

    def test_a_reused_parent_pid_is_not_an_ancestor(self):
        self.assertEqual(rate.live_root(50, self.ROWS), "(ancestor unreadable)")

    def test_census_counts_dead_processes_in_the_window(self):
        out = rate.census(self.ROWS, since_minutes=2, now=300.0)
        self.assertEqual(out["dead"], 3)
        self.assertEqual(out["live"], 2)
        self.assertEqual(dict(map(tuple, out["images"]))["git.exe"], 2)


@unittest.skipUnless(WINDOWS, "job objects and pool tags are Windows only")
class CounterTests(unittest.TestCase):
    def test_job_counts_a_whole_tree_exactly(self):
        out = rate.count_tree([os.environ.get("ComSpec", "cmd.exe"), "/c", "cmd.exe /c exit"], timeout=30)
        self.assertIn(out["processes"], (2, 3))  # conhost joins the job on some consoles
        self.assertEqual(out["exit"], 0)

    def test_proc_counters_read_without_admin(self):
        counters = rate.proc_counters()
        self.assertIsNotNone(counters)
        self.assertGreater(counters[0], 0)


@unittest.skipUnless(WINDOWS and git_bash(), "Git Bash only")
class ForkBudgetTests(unittest.TestCase):
    def posix(self, path: Path) -> str:
        text = str(path).replace("\\", "/")
        return f"/{text[0].lower()}{text[2:]}"

    def count(self, script: str) -> int:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WMUX", "CMUX"))}
        env.update(MSYSTEM="UCRT64", SWITCHBOARD_ROOT=str(REPO))
        return rate.count_tree([git_bash(), "-c", "PATH=/ucrt64/bin:/usr/bin:$PATH; " + script], env=env)["processes"]

    def test_sourcing_the_env_stays_near_zero_forks(self):
        count = self.count(f"source '{self.posix(REPO / 'system/windows/switchboard-env.sh')}' 2>/dev/null; true")
        # 52 before this work. bash itself takes 2.
        self.assertLessEqual(count, 6)

    def test_env_prefers_the_real_git_over_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp) / "Git"
            (git_root / "cmd").mkdir(parents=True)
            (git_root / "ucrt64" / "bin").mkdir(parents=True)
            (git_root / "ucrt64" / "bin" / "git.exe").write_bytes(b"MZ")
            env_file = Path(tmp) / "env"
            env_file.write_text(f"SWITCHBOARD_TOOL_PATH={str(git_root / 'cmd').replace(chr(92), '/')}\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("WMUX", "CMUX"))}
            env.update(MSYSTEM="UCRT64", SWITCHBOARD_ENV_FILE=self.posix(env_file), SWITCHBOARD_ROOT=str(REPO))
            env.pop("SWITCHBOARD_TOOL_PATH", None)
            script = (f"PATH=/usr/bin:$PATH; source '{self.posix(REPO / 'system/windows/switchboard-env.sh')}' 2>/dev/null; "
                      'printf "%s" "$PATH"')
            out = subprocess.run([git_bash(), "-c", script], env=env, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, check=False).stdout
            parts = out.split(":")
            self.assertTrue(any(part.endswith("/Git/ucrt64/bin") for part in parts), out)
            self.assertFalse(any(part.endswith("/Git/cmd") for part in parts), out)


class EnvScriptTests(unittest.TestCase):
    def test_no_command_substitution_on_the_tool_path(self):
        text = (REPO / "system/windows/switchboard-env.sh").read_text(encoding="utf-8")
        self.assertNotIn('$(_switchboard_to_posix', text)
        self.assertNotIn('"$SWITCHBOARD_PYTHON" --version', text)


class ShimDirectTests(unittest.TestCase):
    def setUp(self):
        self.shim = load("cmux_shim_for_test", REPO / "system/scripts/mux/cmux-shim.py")
        self.saved = dict(os.environ)
        for key in ("SWITCHBOARD_TEST_SANDBOX", "SWITCHBOARD_TEST_WMUX_BIN", "WMUX_BIN", "WMUX_REMOTE", "WMUX_REMOTE_TOKEN",
                    "SWITCHBOARD_WMUX_DIRECT"):
            os.environ.pop(key, None)
        self.wmux = self.shim.Wmux.__new__(self.shim.Wmux)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    @unittest.skipUnless(WINDOWS, "the direct path is Windows only")
    def test_mapped_verbs(self):
        request = self.wmux._direct_request
        self.assertEqual(request(["list-workspaces"]), ("workspace.list", {}))
        self.assertEqual(request(["agent-state", "--surface", "s"]), ("pane.agent_state", {"surfaceId": "s"}))
        self.assertEqual(request(["prompts", "--surface", "s", "--json"]), ("surface.list_prompts", {"surfaceId": "s"}))
        self.assertEqual(request(["set-status", "--workspace", "w", "--state", "idle", "--text", "t"]),
                         ("workspace.set_status", {"workspaceId": "w", "state": "idle", "text": "t"}))
        self.assertEqual(request(["log", "info", "a", "b"]), ("sidebar.log", {"level": "info", "message": "a b"}))

    @unittest.skipUnless(WINDOWS, "the direct path is Windows only")
    def test_anything_that_types_or_creates_uses_the_cli(self):
        request = self.wmux._direct_request
        for args in (["send", "--surface", "s", "x"], ["send-key", "enter", "--surface", "s"], ["new-workspace"],
                     ["close-workspace", "w"], ["prompts", "--surface", "s"], ["set-status", "key", "value"],
                     ["set-status", "--workspace", "w", "--state", "bogus"]):
            self.assertIsNone(request(args), args)

    @unittest.skipUnless(WINDOWS, "the direct path is Windows only")
    def test_tests_and_stand_ins_never_reach_the_live_pipe(self):
        request = self.wmux._direct_request
        for key, value in (("SWITCHBOARD_TEST_SANDBOX", "1"), ("SWITCHBOARD_TEST_WMUX_BIN", "fake.py"), ("WMUX_BIN", "fake-wmux.py"),
                           ("WMUX_REMOTE", "host:1"), ("SWITCHBOARD_WMUX_DIRECT", "0")):
            os.environ[key] = value
            self.assertIsNone(request(["list-workspaces"]), key)
            del os.environ[key]
        os.environ["WMUX_BIN"] = "/c/Users/x/AppData/Local/Programs/wmux/resources/cli-bin/wmux.cmd"
        self.assertIsNotNone(request(["list-workspaces"]))

    def test_other_platforms_always_use_the_cli(self):
        real = self.shim.os.name
        self.shim.os.name = "posix"
        try:
            self.assertIsNone(self.wmux._direct_request(["list-workspaces"]))
        finally:
            self.shim.os.name = real


class IgnoredPathsTests(unittest.TestCase):
    def test_batch_matches_one_at_a_time(self):
        saved = sys.argv
        sys.argv = ["dispatch"]
        try:
            dispatch = load("dispatch_for_test", SCRIPTS / "dispatch.py")
        finally:
            sys.argv = saved
        rels = [entry.name for entry in sorted(dispatch.REPO_ROOT.iterdir())]
        rels += ["system/scripts/dispatch.py", "system/state/running-tasks.json", ".amp/in", "people", "name with space"]
        self.assertEqual(dispatch.ignored_paths(rels), {rel for rel in rels if dispatch.is_ignored(rel)})
        self.assertEqual(dispatch.ignored_paths([]), set())


class BadgeTests(unittest.TestCase):
    def setUp(self):
        self.badge = load("running_tasks_badge_for_test", SCRIPTS / "running_tasks_badge.py")
        self.tmp = tempfile.mkdtemp()
        self.file = Path(self.tmp) / "badge.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unchanged_only_with_same_counts_inside_the_window(self):
        counts = {"cos": [1, 2]}
        self.assertFalse(self.badge.unchanged(self.file, counts, 1000.0))
        self.file.write_text(json.dumps({"counts": counts, "at": 1000.0}), encoding="utf-8")
        self.assertTrue(self.badge.unchanged(self.file, counts, 1100.0))
        self.assertFalse(self.badge.unchanged(self.file, {"cos": [2, 2]}, 1100.0))
        self.assertFalse(self.badge.unchanged(self.file, counts, 1000.0 + self.badge.REFRESH_SECONDS + 1))


class DigestRetryTests(unittest.TestCase):
    def test_only_plain_digest_rows_wait_and_they_still_retry_hourly(self):
        text = (REPO / "system/scripts/command-center-loop.sh").read_text(encoding="utf-8")
        block = text[text.index('if (os.name == "nt" and origin == "cos"'):]
        block = block[:block.index("continue")]
        for needed in ('task.get("summary")', 'not task.get("urgent")', 'not task.get("needs_ruling")', "tm_min >= 15"):
            self.assertIn(needed, block)


class ShimPipeFailureTests(unittest.TestCase):
    @unittest.skipUnless(WINDOWS, "the direct path is Windows only")
    def test_a_missing_pipe_is_a_quiet_fallback(self):
        shim = load("cmux_shim_for_pipe_test", REPO / "system/scripts/mux/cmux-shim.py")
        saved = dict(os.environ)
        try:
            for key in ("SWITCHBOARD_TEST_SANDBOX", "SWITCHBOARD_TEST_WMUX_BIN", "WMUX_BIN", "WMUX_REMOTE", "WMUX_REMOTE_TOKEN"):
                os.environ.pop(key, None)
            os.environ["WMUX_PIPE"] = r"\\\\.\\pipe\\switchboard-test-no-such-pipe"
            wmux = shim.Wmux.__new__(shim.Wmux)
            wmux.trace = False
            self.assertIsNone(wmux._direct(["list-workspaces"], 2))
        finally:
            os.environ.clear()
            os.environ.update(saved)


class HookProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = load("apply_hook_profile_for_test", REPO / "system/windows/apply-hook-profile.py")
        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / "claude"
        self.plugin = self.tmp / "plugin"
        (self.base / "plugins").mkdir(parents=True)
        (self.plugin / "hooks").mkdir(parents=True)
        self.source = json.loads((REPO / "system/claude-marketplace/plugins/switchboard/hooks/hooks.json").read_text(encoding="utf-8"))
        (self.plugin / "hooks/hooks.json").write_text(json.dumps(self.source), encoding="utf-8")
        (self.base / "plugins/installed_plugins.json").write_text(json.dumps(
            {"plugins": {self.profile.SWITCHBOARD_PLUGIN: [{"installPath": str(self.plugin)}]}}), encoding="utf-8")
        self.write_settings(True)
        self.repo = self.tmp / "repo"
        (self.repo / ".claude").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_settings(self, wmux: bool) -> None:
        hooks = {"PreToolUse": [{"hooks": [{"command": "node wmux-hook.js"}]}]} if wmux else {}
        (self.base / "settings.json").write_text(json.dumps(
            {"hooks": hooks, "enabledPlugins": {self.profile.HOOKIFY: True}}), encoding="utf-8")

    def settings(self) -> dict:
        return json.loads((self.base / "settings.json").read_text(encoding="utf-8"))

    def installed_hooks(self) -> dict:
        return json.loads((self.plugin / "hooks/hooks.json").read_text(encoding="utf-8"))

    def test_the_repo_hooks_keep_the_heartbeat_for_the_mac(self):
        self.assertTrue(self.profile.has_heartbeat(self.source))

    def test_apply_removes_only_the_no_op_hooks(self):
        self.assertEqual(len(self.profile.status(self.base, self.repo)), 2)
        self.profile.apply(self.base, self.repo)
        hooks = self.installed_hooks()
        self.assertFalse(self.profile.has_heartbeat(hooks))
        self.assertIn("amp-bridge", json.dumps(hooks["hooks"]["PreToolUse"]))
        self.assertIn("surface-context", json.dumps(hooks["hooks"]["SessionStart"]))
        self.assertFalse(self.settings()["enabledPlugins"][self.profile.HOOKIFY])
        self.assertEqual(self.profile.status(self.base, self.repo), [])

    def test_heartbeat_stays_without_wmux_hooks(self):
        self.write_settings(False)
        self.profile.apply(self.base, self.repo)
        self.assertTrue(self.profile.has_heartbeat(self.installed_hooks()))

    def test_hookify_stays_when_a_rule_exists(self):
        (self.repo / ".claude" / "hookify.mine.local.md").write_text("rule", encoding="utf-8")
        self.profile.apply(self.base, self.repo)
        self.assertTrue(self.settings()["enabledPlugins"][self.profile.HOOKIFY])

    def test_undo_restores_both(self):
        self.profile.apply(self.base, self.repo)
        self.profile.undo(self.base, REPO)
        self.assertTrue(self.settings()["enabledPlugins"][self.profile.HOOKIFY])
        self.assertTrue(self.profile.has_heartbeat(self.installed_hooks()))


class MacUnchangedTests(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (REPO / rel).read_text(encoding="utf-8")

    def test_launchd_tick_still_runs_every_ten_minutes(self):
        self.assertRegex(self.read("system/launchd/com.switchboard.command-center.plist"), r"<integer>600</integer>")

    def test_every_windows_branch_in_shared_shell_is_gated(self):
        loop = self.read("system/scripts/command-center-loop.sh")
        self.assertIn('(os.name == "nt" and origin == "cos"', loop)
        self.assertIn('if [[ -n "${MSYSTEM:-}" ]]; then\n    TZ=UTC printf', loop)
        self.assertIn('"$(date -u +%Y-%m-%dT%H:%M:%SZ)"', loop)
        publish = self.read("system/scripts/publish-running-tasks-status.sh")
        self.assertIn('[[ -n "${MSYSTEM:-}" ]] && python3', publish)
        mux = self.read("system/scripts/lib/mux.sh")
        self.assertIn('SWITCHBOARD_MUX="$(mux_detect)"', mux)
        self.assertIn('[[ "${SWITCHBOARD_MUX:-}" != "wmux" || -z "${MSYSTEM:-}" ]]', mux)

    def test_liveness_throttle_is_windows_only(self):
        text = self.read("system/amp-plugins/task-liveness.ts")
        self.assertIn("if (process.platform !== 'win32') return false", text)
        self.assertEqual(len(re.findall(r"process\.platform === 'win32'", text)), 2)

    def test_scheduled_tasks_use_no_login_shell(self):
        text = self.read("system/windows/register-scheduled-tasks.ps1")
        native = text[text.index("function Native-Action"):text.index("function Wsl-Action")]
        self.assertNotIn("-lc", native)
        # The command-center tick runs every 10 minutes.
        self.assertIn('command-center.out.log";  Kind = "every";   Minutes = 10', text)


class LeakTraceTests(unittest.TestCase):
    OUTPUT = """
PROCESS ffffb80c12345080
    SessionId: 1  Cid: 1234    Peb: 00000000  ParentCid: 0abc
    Image: hostname.exe

PROCESS ffffb80c22345080
    Image: hostname.exe

fffff801`11223340  ffffb80c`aaaa000f ffffb80c`bbbb0007
fffff801`11223350  00000000`00000000 00000000`00000000
"""

    def test_zombie_addresses(self):
        self.assertEqual(trace.zombie_addresses(self.OUTPUT), ["ffffb80c12345080", "ffffb80c22345080"])
        self.assertEqual(len(trace.zombie_addresses(self.OUTPUT, limit=1)), 1)

    def test_callback_blocks_drop_the_ref_bits_and_skip_empty_slots(self):
        self.assertEqual(trace.callback_addresses(self.OUTPUT), [0xffffb80caaaa0008, 0xffffb80cbbbb0008])

    def test_second_pass_asks_for_the_reference_trace(self):
        commands = trace.second_pass_commands(self.OUTPUT)
        self.assertIn("!obtrace ffffb80c12345080", commands)
        self.assertIn("!object ffffb80c12345080", commands)

    def test_exited_processes_only_with_the_traced_image_first(self):
        text = """
PROCESS ffffb80c00000001
    SessionId: 1  Cid: 1  Peb: 0  ParentCid: 2
    DirBase: 1  ObjectTable: ffffb80c`11111111  HandleCount: 50.
    Image: live.exe

PROCESS ffffb80c00000002
    DirBase: 1  ObjectTable: 00000000  HandleCount:   0.
    Image: other.exe

PROCESS ffffb80c00000003
    DirBase: 1  ObjectTable: 00000000  HandleCount:   0.
    Image: hostname.exe
"""
        self.assertEqual(trace.zombie_addresses(text), ["ffffb80c00000003", "ffffb80c00000002"])

    def test_obtrace_counts_the_extra_reference_without_naming_a_driver(self):
        modules = [(0xfffff80100000000, 0xfffff80100100000, "baddrv", "")]
        text = """
Object: ffffb80c00000003
 Image: hostname.exe
Sequence  (+/-)  Tag   Stack
--------  -----  ----  ---------------------------
       a   +1    Dflt  nt!ObReferenceObject+1c
                       baddrv!Callback+40
                       nt!PspCallNotify+90
       b   +1    Dflt  nt!ObpCreateHandle+10
                       nt!NtCreateUserProcess+22
       c   -1    Dflt  nt!ObDereferenceObject+10
                       nt!NtClose+22

References: 2, Dereferences 1
"""
        objects = trace.parse_obtrace(text, modules)
        self.assertEqual(len(objects), 1)
        self.assertEqual(len(objects[0]), 3)
        self.assertEqual(objects[0][0][2][1], "baddrv!Callback")
        over, by_tag = trace.reference_balance(objects)
        self.assertEqual(over[1], 1)
        self.assertEqual(by_tag["Dflt"], 1)
        self.assertFalse(hasattr(trace, "unbalanced_stacks"))

    def test_bare_address_frames_become_their_module(self):
        modules = [(0xfffff80100000000, 0xfffff80100100000, "baddrv", "")]
        self.assertEqual(trace._frame("fffff801`00000040", modules), "baddrv")

    def test_routine_modules_skip_pool_addresses(self):
        modules = [(0xfffff80100000000, 0xfffff80100100000, "baddrv", "")]
        text = "ffffb80c`aaaa0008  fffff801`00000040 baddrv+0x40\nffffb80c`bbbb0008  ffffb80c`cccc0000\n"
        self.assertEqual(dict(trace.routine_modules(text, modules)), {"baddrv": 1})

    def test_summary_prints_no_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "a.log", Path(folder) / "b.log"
            first.write_text(f"""{trace.SECTION}processes
PROCESS ffffb80c00000003
    DirBase: 1  ObjectTable: 00000000  HandleCount:   0.
    Image: hostname.exe
{trace.SECTION}modules
fffff801`00000000 fffff801`00100000   baddrv   baddrv.sys   Thu Jan 1 2026
""", encoding="utf-8")
            second.write_text(f"""{trace.SECTION}objects
Object: ffffb80c00000003
       a   +1    Dflt  baddrv!Callback+40
""", encoding="utf-8")
            text = json.dumps(trace.summarize(first, second))
        self.assertNotIn("ffffb80c", text)
        self.assertNotIn("fffff801", text)
        self.assertIn("Dflt", text)

    def test_extracts_refuse_dropbox_and_git(self):
        self.assertEqual(trace.unsafe_location(Path.home() / "Dropbox" / "x"), "inside Dropbox")
        self.assertIsNotNone(trace.unsafe_location(REPO / ".amp" / "in" / "leak-dump"))

    def test_trace_state_reads_the_inline_tag(self):
        raw = bytes([1, 1]) + bytes(6) + bytes(8) + bytes(8) + (8).to_bytes(2, "little") + (10).to_bytes(2, "little") + bytes(12)
        raw += "Proc\0".encode("utf-16-le")
        self.assertEqual(trace.format_trace_state(raw), "enabled=True permanent=True image='' tags='Proc'")

    @unittest.skipUnless(WINDOWS, "kernel query is Windows only")
    def test_status_reads_without_admin(self):
        status, text = trace.query_trace()
        self.assertEqual(status, 0)
        self.assertIn("enabled=", text)


if __name__ == "__main__":
    unittest.main()
