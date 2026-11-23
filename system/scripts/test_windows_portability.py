#!/usr/bin/env python3
"""Windows behavior from the kit v6 Windows install, checked on any OS.

Each test drives the Windows branch with os.name patched to "nt", or feeds a
Windows path (C:/..., C:\\...) to code that runs the same on every OS, so a Mac
run catches a regression before a Windows owner does. The real Windows run is
the verify list in the kit v7 note. Run from system/scripts:

    python3 -m unittest test_windows_portability
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # @dataclass reads its module from sys.modules on Python 3.12
    spec.loader.exec_module(module)
    return module


dispatch = _load("dispatch_win_test", "dispatch.py")
review_surface = _load("review_surface_win_test", "review_surface.py")
image_compare = _load("image_compare_win_test", "image_compare.py")


class LaunchCommandOnWindowsTests(unittest.TestCase):
    """dispatch.launch_command_for: the line wmux types into a new worker pane."""

    def row(self, agent: str = "claude") -> dict:
        return {"id": "rt-win", "attempt_id": "rt-win.a1.abc", "workstream": "demo", "mode": "ultra",
                "agent": agent, "worker_id": "worker-win", "session_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                "worktree": "C:\\Home\\owner\\AppData\\Local\\switchboard\\worktrees\\rt-win"}

    def test_claude_worker_seeds_a_pointer_to_the_prompt_file(self) -> None:
        with mock.patch.object(dispatch.os, "name", "nt"):
            line = dispatch.launch_command_for(self.row())
        self.assertIn("Read and follow all instructions in ", line)
        self.assertNotIn("$(cat", line, "the whole prompt as one argument exceeds the Windows command-line limit")
        self.assertIn("SWITCHBOARD_WORKTREE=C:/Home/owner/AppData/Local/switchboard/worktrees/rt-win", line)
        self.assertNotIn("\\", line, "wmux reads backslash escapes in typed text as keys")

    def test_mac_claude_worker_keeps_the_inline_prompt(self) -> None:
        with mock.patch.object(dispatch.os, "name", "posix"):
            line = dispatch.launch_command_for(self.row())
        self.assertIn('"$(cat ', line)

    def test_claude_worker_turns_prompt_suggestions_off(self) -> None:
        checkout = HERE.parents[1]  # dispatch.REPO_ROOT is the main checkout, which a worktree run predates
        settings = checkout / "system" / "config" / "claude-tab-settings.json"
        self.assertTrue(settings.exists(), "the kit ships claude-tab-settings.json")
        self.assertIn("promptSuggestionEnabled", settings.read_text(encoding="utf-8"))
        with mock.patch.object(dispatch, "REPO_ROOT", checkout):
            self.assertIn("--settings ", dispatch.launch_command_for(self.row()))

    def test_amp_worker_pipes_a_forward_slash_prompt_path(self) -> None:
        with mock.patch.object(dispatch.os, "name", "nt"):
            line = dispatch.launch_command_for(self.row(agent="amp"))
        self.assertTrue(line.startswith("cat "))
        self.assertNotIn("\\", line)


class WorkerTabIdentityTests(unittest.TestCase):
    """Refs renumber when wmux restarts; the reaper matches by identity first."""

    def test_description_beats_a_stale_ref(self) -> None:
        task = {"id": "rt-win", "workspace_ref": "workspace:3"}
        tabs = [{"ref": "workspace:3", "id": "other", "title": "Owner tab", "description": ""},
                {"ref": "workspace:9", "id": "mine", "title": "Demo 1", "description": dispatch.worker_description("rt-win")}]
        self.assertEqual(dispatch.worker_tab_for(task, tabs)["id"], "mine")


class AtomicReplaceRetryTests(unittest.TestCase):
    """_os_replace: WinError 5 while another process holds the target is retried on Windows only."""

    def flaky(self, failures: int):
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(5, "Access is denied")
        return replace, calls

    def test_windows_retries_until_the_target_is_free(self) -> None:
        replace, calls = self.flaky(2)
        with mock.patch.object(dispatch.os, "name", "nt"), mock.patch.object(dispatch.os, "replace", replace), \
                mock.patch("time.sleep"):
            dispatch._os_replace("a", "b")
        self.assertEqual(calls["n"], 3)

    def test_mac_raises_at_once(self) -> None:
        replace, calls = self.flaky(1)
        with mock.patch.object(dispatch.os, "name", "posix"), mock.patch.object(dispatch.os, "replace", replace):
            with self.assertRaises(PermissionError):
                dispatch._os_replace("a", "b")
        self.assertEqual(calls["n"], 1)


class RunCappedTimeoutTests(unittest.TestCase):
    """daemon/run-capped.py: the timeout path on Windows, which has no killpg."""

    def test_windows_timeout_ends_the_tree_with_taskkill(self) -> None:
        run_capped = _load("run_capped_win_test", "../../daemon/run-capped.py")
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(run_capped.os, "name", "nt"), \
                mock.patch.object(run_capped.subprocess, "run", return_value=done) as run, \
                mock.patch.object(run_capped.os, "killpg", create=True) as killpg:
            run_capped.terminate_group(4242)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["taskkill", "/T", "/F", "/PID", "4242"])
        killpg.assert_not_called()


class DrivePathLinkTests(unittest.TestCase):
    """Report links to local pages and images on a Windows drive."""

    def test_drive_path_file_url_keeps_the_colon(self) -> None:
        self.assertEqual(review_surface._file_url(Path("C:/Home/owner/switch board/a.md")),
                         "file:///C:/Home/owner/switch%20board/a.md")
        self.assertEqual(image_compare.file_url(Path("C:/Home/owner/a.png")), "file:///C:/Home/owner/a.png")
        self.assertEqual(review_surface._file_url(Path("/home/owner/a.md")), "file:///home/owner/a.md")

    def test_drive_file_url_parses_back_to_the_drive_path(self) -> None:
        self.assertEqual(review_surface._url_path("/C:/Home/owner/a.md"), "C:/Home/owner/a.md")
        self.assertEqual(review_surface._url_path("/home/owner/a.md"), "/home/owner/a.md")
        self.assertEqual(image_compare._local_target("file:///C:/Home/owner/a%20b.html").as_posix(),
                         "C:/Home/owner/a b.html")

    def test_compare_page_on_a_drive_counts_as_a_compare_link(self) -> None:
        for path in ("C:/Home/owner/switchboard/workstreams/demo/assets/compare/covers.html",
                     "C:\\Home\\owner\\switchboard\\workstreams\\demo\\assets\\compare\\covers.html"):
            self.assertTrue(review_surface.has_compare_link(f"Compare page: {path}"), path)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class DirLockTests(unittest.TestCase):
    """lib/dir-lock.sh: the Git Bash lock that replaces flock(9) on Windows."""

    LIB = HERE / "lib" / "dir-lock.sh"

    def bash(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", f'source "{self.LIB}"\n{script}'], capture_output=True, text=True, timeout=30)

    def test_second_holder_waits_then_gives_up_and_release_frees_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = f"{tmp}/state/x.lock"
            out = self.bash(f'''
                switchboard_dir_lock_acquire "{lock}" 0 || exit 10
                ( source "{self.LIB}"; switchboard_dir_lock_acquire "{lock}" 0 && exit 11; exit 0 ) || exit 12
                switchboard_dir_lock_release
                [ -d "{lock}" ] && exit 13
                switchboard_dir_lock_acquire "{lock}" 0 || exit 14
                switchboard_dir_lock_release''')
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_lock_left_by_a_dead_process_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            lock.mkdir()
            dead = subprocess.Popen(["true"])
            dead.wait()
            (lock / "pid").write_text(str(dead.pid))
            out = self.bash(f'switchboard_dir_lock_acquire "{lock}" 0 && cat "{lock}/pid"')
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotEqual(out.stdout.strip(), str(dead.pid))

    def test_lock_from_a_killed_subshell_is_reclaimed(self) -> None:
        if self.bash('[ -n "${BASHPID:-}" ]').returncode != 0:
            self.skipTest("this bash has no BASHPID (bash 3.2)")
        with tempfile.TemporaryDirectory() as tmp:
            lock = f"{tmp}/x.lock"
            out = self.bash(f'''
                ( switchboard_dir_lock_acquire "{lock}" 0 && kill -9 $BASHPID )
                [ -d "{lock}" ] || exit 10
                switchboard_dir_lock_acquire "{lock}" 0 || exit 11
                [ "$(cat "{lock}/pid")" = "$BASHPID" ] || exit 12
                [ -d "{lock}.reclaim" ] && exit 13
                switchboard_dir_lock_release''')
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_retaken_lock_is_never_reclaimed(self) -> None:
        # Wrong impl: the waiter read the dead owner, then deletes the lock someone just retook.
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            lock.mkdir()
            (lock / "pid").write_text(str(os.getpid()))
            dead = subprocess.Popen(["true"])
            dead.wait()
            out = self.bash(f'switchboard_dir_lock_reclaim "{lock}" {dead.pid}')
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual((lock / "pid").read_text(), str(os.getpid()))
            self.assertFalse(Path(f"{lock}.reclaim").exists())

    def test_a_live_reclaimer_keeps_others_out_and_a_dead_one_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = f"{tmp}/x.lock"
            dead = subprocess.Popen(["true"])
            dead.wait()
            out = self.bash(f'''
                mkdir -p "{lock}" "{lock}.reclaim"
                echo {dead.pid} > "{lock}/pid"
                sleep 300 >/dev/null 2>&1 & live=$!
                echo $live > "{lock}.reclaim/pid"
                switchboard_dir_lock_acquire "{lock}" 0 && exit 10
                [ -d "{lock}" ] || exit 11
                kill $live; wait $live 2>/dev/null
                switchboard_dir_lock_acquire "{lock}" 2 || exit 12
                [ -d "{lock}.reclaim" ] && exit 13
                switchboard_dir_lock_release''')
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_windows_shell_detection(self) -> None:
        self.assertEqual(self.bash("OSTYPE=msys; switchboard_is_windows_shell").returncode, 0)
        self.assertEqual(self.bash("OSTYPE=darwin25; switchboard_is_windows_shell").returncode, 1)


if __name__ == "__main__":
    unittest.main()
