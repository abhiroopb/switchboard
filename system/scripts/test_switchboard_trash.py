#!/usr/bin/env python3
"""switchboard-trash.py: the drop-in `rm -rf` that switchboard-guardrails Rule 3 rewrites
an outside delete to: trash instead of delete.

Hermetic: a temp HOME, repo, Trash folder, and log; /usr/bin/trash is skipped
(SWITCHBOARD_TRASH_NO_CLI=1) so nothing reaches the real Trash.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "switchboard-trash.py"
sys.path.insert(0, str(SCRIPT.parent))
from lib import winlink  # noqa: E402

windows_only = unittest.skipUnless(os.name == "nt", "Win32 reserved device names exist only on Windows")


def _reserved(path: Path) -> str:
    """`path` in \\\\?\\ form, so a name like nul is a file, never the device."""
    return winlink.extended_path(str(path))


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, os.path.join(tmp, "link"), target_is_directory=True)
            return True
        except OSError:
            return False


needs_symlink = unittest.skipUnless(_can_symlink(), "symlinks need Developer Mode or admin on Windows")


class SwitchboardTrashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # The temp dir sits under /var/folders, itself a temp path, so the
        # tests name /tmp only as temp (SWITCHBOARD_TRASH_TEMP_PREFIXES). Resolved,
        # because the helper compares real paths with HOME.
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.repo = self.home / "Development" / "switchboard"
        self.trash = self.home / ".Trash"
        self.log = root / "trash-moves.jsonl"
        self.repo.mkdir(parents=True)
        self.env = {
            **os.environ, "HOME": str(self.home), "SWITCHBOARD_TRASH_REPO": str(self.repo),
            "SWITCHBOARD_TRASH_DIR": str(self.trash), "SWITCHBOARD_TRASH_LOG": str(self.log), "SWITCHBOARD_TRASH_NO_CLI": "1",
            "SWITCHBOARD_TRASH_TEMP_PREFIXES": "/tmp:/private/tmp",
            "SWITCHBOARD_WORKER_TASK_ID": "rt-000000000007", "CMUX_WORKSPACE_ID": "WS-TEST",
        }

    def tearDown(self) -> None:
        if os.name == "nt":   # a file named nul defeats shutil.rmtree; winlink.rmtree reaches it by \\?\
            winlink.rmtree(self.tmp.name, ignore_errors=True)
        self.tmp.cleanup()

    def run_helper(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, cwd=str(cwd or self.home),
                              capture_output=True, text=True, timeout=30)

    def classify(self, path: str) -> tuple[str, str]:
        code = ("import importlib.util, json, sys; spec = importlib.util.spec_from_file_location('t', sys.argv[1]); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(json.dumps(m.classify(sys.argv[2])))")
        out = subprocess.run([sys.executable, "-c", code, str(SCRIPT), path], env=self.env, capture_output=True, text=True, check=True)
        return tuple(json.loads(out.stdout))  # type: ignore[return-value]

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_classify(self) -> None:
        home = str(self.home)
        self.assertEqual(self.classify("/tmp/x")[0], "delete")
        # Not a git checkout here, so git cannot restore it: Trash, never a hard delete.
        self.assertEqual(self.classify(f"{self.repo}/.amp/in/rt-1")[0], "trash")
        self.assertEqual(self.classify(f"{self.repo}/tool/node_modules"), ("delete", "build output"))
        self.assertEqual(self.classify(f"{home}/app/node_modules")[0], "delete")
        self.assertEqual(self.classify(str(self.repo)), ("refuse", "the switchboard repo root"))
        self.assertEqual(self.classify(f"{home}/other/.git")[0], "refuse")
        self.assertEqual(self.classify(f"{home}/other/.GIT")[0], "refuse")
        self.assertEqual(self.classify(f"{home}/Documents")[0], "refuse")
        self.assertEqual(self.classify(home)[0], "refuse")
        self.assertEqual(self.classify("/usr")[0], "refuse")
        self.assertEqual(self.classify(f"{home}/Documents/Demos/old"), ("trash", "outside switchboard and temp paths"))

    def test_outside_folder_moves_to_trash_and_logs(self) -> None:
        target = self.home / "scratch-folder"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f.txt").write_text("keep me")
        result = self.run_helper("-rf", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        moved = [p for p in self.trash.iterdir() if p.name.startswith("scratch-folder (switchboard ")]
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0] / "sub" / "f.txt").read_text(), "keep me")
        self.assertIn("moved", result.stderr)
        [row] = self.rows()
        self.assertEqual((row["action"], row["path"], row["task_id"], row["workspace_id"]),
                         ("trashed", str(target), "rt-000000000007", "WS-TEST"))

    def test_same_name_twice_never_overwrites(self) -> None:
        for _ in range(2):
            (self.home / "dup").mkdir()
            self.assertEqual(self.run_helper("-rf", str(self.home / "dup")).returncode, 0)
        self.assertEqual(len([p for p in self.trash.iterdir() if p.name.startswith("dup (switchboard ")]), 2)

    def test_protected_targets_are_refused_and_untouched(self) -> None:
        (self.home / "Documents").mkdir()
        (self.home / "proj" / ".git").mkdir(parents=True)
        for target in (self.home / "Documents", self.home / "proj" / ".git", self.repo):
            result = self.run_helper("-rf", str(target))
            self.assertEqual(result.returncode, 1)
            self.assertTrue(target.exists())
            self.assertIn("refused", result.stderr)
        self.assertEqual([r["action"] for r in self.rows()], ["refused"] * 3)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                               "-c", "core.autocrlf=false", *args],
                              env=self.env, capture_output=True, text=True, check=True).stdout

    def git_repo(self) -> None:
        """The test repo as a git checkout: local/ and .amp/ ignored, tracked/ committed."""
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("local/\n.amp/\n")
        (self.repo / "tracked").mkdir()
        (self.repo / "tracked" / "a.md").write_text("in git\n")
        (self.repo / "tracked" / "b.md").write_text("in git too\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

    def trashed_names(self) -> list[str]:
        return sorted(p.name.split(" (switchboard ")[0] for p in self.trash.iterdir()) if self.trash.exists() else []

    def test_gitignored_local_path_goes_to_the_trash(self) -> None:
        # A gitignored path such as local/skills-archive/... is trashed, never hard-deleted.
        self.git_repo()
        archived = self.repo / "local" / "skills-archive" / "not-in-use" / "old-skill"
        archived.mkdir(parents=True)
        (archived / "SKILL.md").write_text("keep a copy")
        self.assertEqual(self.classify(str(archived)),
                         ("trash", "inside the switchboard repo, not tracked by git (gitignored or untracked)"))
        result = self.run_helper("-rf", str(archived))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(archived.exists())
        self.assertEqual(self.trashed_names(), ["old-skill"])
        moved = next(self.trash.iterdir())
        self.assertEqual((moved / "SKILL.md").read_text(), "keep a copy")
        self.assertEqual([r["action"] for r in self.rows()], ["trashed"])

    def test_untracked_file_goes_to_the_trash(self) -> None:
        self.git_repo()
        draft = self.repo / "tracked" / "draft.md"
        draft.write_text("never committed")
        self.assertEqual(self.run_helper("-rf", str(draft)).returncode, 0)
        self.assertFalse(draft.exists())
        self.assertEqual(self.trashed_names(), ["draft.md"])
        # A tracked folder that holds such a file goes whole to the Trash.
        draft.write_text("never committed")
        self.assertEqual(self.classify(str(self.repo / "tracked"))[0], "trash")

    def test_tracked_clean_file_and_folder_are_deleted(self) -> None:
        self.git_repo()
        self.assertEqual(self.classify(str(self.repo / "tracked" / "a.md")),
                         ("delete", "inside the switchboard repo, tracked and unchanged in git"))
        self.assertEqual(self.run_helper("-rf", str(self.repo / "tracked" / "a.md")).returncode, 0)
        self.assertFalse((self.repo / "tracked" / "a.md").exists())
        self.git("checkout", "--", "tracked/a.md")   # git restores it, so a hard delete loses nothing
        self.assertEqual(self.run_helper("-rf", str(self.repo / "tracked")).returncode, 0)
        self.assertFalse((self.repo / "tracked").exists())
        self.assertFalse(self.trash.exists())
        self.assertEqual(self.rows(), [])

    def test_tracked_but_changed_or_holding_an_ignored_file_goes_to_the_trash(self) -> None:
        self.git_repo()
        (self.repo / "tracked" / "a.md").write_text("edited, not committed\n")
        self.assertEqual(self.classify(str(self.repo / "tracked" / "a.md"))[0], "trash")
        self.git("checkout", "--", "tracked/a.md")
        (self.repo / ".gitignore").write_text("local/\n.amp/\n*.log\n")
        self.git("commit", "-qam", "ignore logs")
        (self.repo / "tracked" / "run.log").write_text("ignored, so no git copy")
        self.assertEqual(self.classify(str(self.repo / "tracked"))[0], "trash")
        self.assertEqual(self.run_helper("-rf", str(self.repo / "tracked")).returncode, 0)
        self.assertEqual(self.trashed_names(), ["tracked"])

    def test_missing_paths_behave_like_rm(self) -> None:
        self.assertEqual(self.run_helper("-rf", str(self.home / "nope")).returncode, 0)
        missing = self.run_helper("-r", str(self.home / "nope"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("No such file", missing.stderr)
        self.assertEqual(self.rows(), [])

    @needs_symlink
    def test_repo_delete_never_walks_into_a_shared_link(self) -> None:
        # A task worktree links main's people/ in. Deleting the worktree removes the
        # link, never main's files (Windows junctions).
        shared = self.repo / "people"
        shared.mkdir()
        (shared / "alice.md").write_text("keep")
        worktree = self.repo / ".amp" / "worktrees" / "rt-x"
        worktree.mkdir(parents=True)
        (worktree / "people").symlink_to(shared)
        (worktree / "notes.txt").write_text("scratch")
        self.load().delete(str(worktree))   # the hard-delete path; an untracked worktree itself now goes to the Trash
        self.assertFalse(os.path.lexists(worktree))
        self.assertEqual((shared / "alice.md").read_text(), "keep")

    @needs_symlink
    def test_delete_skips_junctions_even_where_islink_misses_them(self) -> None:
        # Python 3.11 on Windows: os.path.islink is False for a junction.
        m = self.load()
        shared = self.repo / "state"
        shared.mkdir()
        (shared / "ledger.json").write_text("{}")
        tree = self.repo / ".amp" / "worktrees" / "rt-y"
        tree.mkdir(parents=True)
        junction = tree / "state"
        junction.symlink_to(shared)
        hidden = {str(junction)}
        real_islink = os.path.islink
        with mock.patch.object(m.winlink, "is_junction", side_effect=lambda q: os.fspath(q) in hidden), \
                mock.patch.object(m.winlink.os.path, "islink",
                                  side_effect=lambda q: False if os.fspath(q) in hidden else real_islink(q)):
            m.delete(str(tree))
        self.assertFalse(os.path.lexists(tree))
        self.assertTrue((shared / "ledger.json").exists())

    @needs_symlink
    def test_relative_target_and_symlink(self) -> None:
        real = self.home / "real-dir"
        real.mkdir()
        link = self.home / "work" / "link"
        link.parent.mkdir()
        link.symlink_to(real)
        self.assertEqual(self.run_helper("-rf", "link", cwd=link.parent).returncode, 0)
        self.assertTrue(real.exists(), "the link moves, not the folder it points to")
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(self.rows()[0]["path"], str(link.parent.resolve() / "link"))

    def test_list_line_for_the_report(self) -> None:
        (self.home / "a").mkdir()
        (self.home / "Documents").mkdir()
        self.run_helper("-rf", str(self.home / "a"))
        self.run_helper("-rf", str(self.home / "Documents"))
        line = self.run_helper("--switchboard-list", "--task-id", "rt-000000000007", "--format", "line").stdout.strip()
        self.assertEqual(line, f"🗑️ Moved to Trash instead of deleting (1): {self.home / 'a'}. "
                               f"Refused (1): {self.home / 'Documents'} (a top-level home folder).")
        self.assertEqual(self.run_helper("--switchboard-list", "--task-id", "rt-other", "--format", "line").stdout, "")
        data = json.loads(self.run_helper("--switchboard-list").stdout)
        self.assertEqual(data["count"], 2)

    def load(self, **env: str):
        spec = importlib.util.spec_from_file_location("switchboard_trash_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_classify_windows_paths(self) -> None:
        # switchboard Kit on Windows: native Python sees C:\\ paths (and Git Bash /c/ ones
        # when MSYS conversion is off); the refusals must hold there too.
        m = self.load()
        env = {"HOME": r"C:\Users\Pat", "SWITCHBOARD_TRASH_REPO": r"C:\Users\Pat\Development\switchboard",
               "SWITCHBOARD_TRASH_TEMP_PREFIXES": r"C:\Users\Pat\AppData\Local\Temp;"}
        with mock.patch.dict(os.environ, env):
            def c(path: str) -> str:
                return m.classify(path, windows=True)[0]
            self.assertEqual(c(r"C:\Users\Pat\AppData\Local\Temp\x"), "delete")
            # In the repo, but git cannot restore it (no checkout here): Trash.
            self.assertEqual(c(r"C:\Users\Pat\Development\switchboard\.amp\in\rt-1"), "trash")
            self.assertEqual(c("/c/Users/Pat/Development/switchboard/.amp/in/rt-1"), "trash")
            self.assertEqual(c(r"C:\Users\Pat\Development\switchboard\tool\node_modules"), "delete")
            self.assertEqual(c(r"c:\users\pat\development\SWITCHBOARD"), "refuse")
            self.assertEqual(c(r"C:\Users\Pat\proj\.git"), "refuse")
            self.assertEqual(c(r"C:\Users\Pat\Documents"), "refuse")
            self.assertEqual(c(r"C:\Users\Pat\AppData"), "refuse")
            self.assertEqual(c(r"C:\Users\Pat"), "refuse")
            for system in ("C:\\", "C:", r"C:\Windows", r"C:\Program Files (x86)", r"D:\Users"):
                self.assertEqual(c(system), "refuse", system)
            # Win32 drops trailing dots and spaces, and \\?\ names the same path (code review).
            for alias in (r"C:\Users\Pat\Development\switchboard\.git.", r"C:\Users\Pat\Development\switchboard.",
                          r"C:\Windows.", r"C:\Users\Pat\Desktop ", r"\\?\C:\Windows", "//?/C:/Windows"):
                self.assertEqual(c(alias), "refuse", alias)
            self.assertEqual(c(r"C:\Users\Pat\Documents\Demos\old"), "trash")
            self.assertEqual(c(r"C:\Users\Pat\app\node_modules"), "delete")

    def test_windows_default_temp_never_covers_home(self) -> None:
        m = self.load()
        env = {k: v for k, v in os.environ.items() if k != "SWITCHBOARD_TRASH_TEMP_PREFIXES"}
        with mock.patch.dict(os.environ, {**env, "HOME": r"C:\Users\Pat"}, clear=True), \
             mock.patch.object(m.tempfile, "gettempdir", return_value=r"C:\Users\Pat"):
            self.assertEqual(m._temp_prefixes(windows=True), ())
        with mock.patch.dict(os.environ, {**env, "HOME": r"C:\Users\Pat"}, clear=True), \
             mock.patch.object(m.tempfile, "gettempdir", return_value=r"C:\Users\Pat\AppData\Local\Temp"):
            self.assertEqual(m._temp_prefixes(windows=True), ("c:/users/pat/appdata/local/temp",))
        with mock.patch.dict(os.environ, {**env, "HOME": r"C:\Users\Pat"}, clear=True), \
             mock.patch.object(m.tempfile, "gettempdir", return_value=r"C:\Users\Pat\Documents"):
            self.assertEqual(m._temp_prefixes(windows=True), ())  # TEMP aimed at Documents deletes nothing

    def test_linux_uses_the_freedesktop_trash(self) -> None:
        m = self.load()
        target = self.home / "old-build-notes"
        target.mkdir()
        (target / "a.txt").write_text("keep")
        data = self.home / ".local" / "share"
        env = {k: v for k, v in self.env.items() if k != "SWITCHBOARD_TRASH_DIR"}
        with mock.patch.dict(os.environ, {**env, "XDG_DATA_HOME": str(data)}, clear=True), \
             mock.patch.object(m.sys, "platform", "linux"), mock.patch.object(m, "_is_windows", return_value=False):
            first = m.move_to_trash(str(target))
            target.mkdir()
            second = m.move_to_trash(str(target))
        self.assertEqual(first, str(data / "Trash" / "files" / "old-build-notes"))
        self.assertEqual(second, str(data / "Trash" / "files" / "old-build-notes.2"))
        self.assertEqual((data / "Trash" / "files" / "old-build-notes" / "a.txt").read_text(), "keep")
        info = (data / "Trash" / "info" / "old-build-notes.trashinfo").read_text()
        self.assertTrue(info.startswith("[Trash Info]\nPath="), info)
        self.assertIn("old-build-notes", info)
        self.assertIn("DeletionDate=", info)
        self.assertFalse(target.exists())

    # ── Windows: the Recycle Bin, never a hard delete ──────────
    # Runs on macOS by patching the platform checks and subprocess.run. shutil.rmtree,
    # os.remove, and os.unlink raise on every Windows path below, so any hard delete fails the test.

    def windows(self, *, ps_exit: int = 0, ps_stderr: str = "", removes: bool = True, ps_exe: str | None = "C:/PS/powershell.exe",
                config: dict | None = None, volume: dict | None = None, keep_dir: bool = False):
        """(module, calls, patches) with the Windows branch on. `calls` lists each subprocess.run."""
        m = self.load()
        calls: list[dict] = []
        fake_bin = Path(self.tmp.name) / "fake-recycle-bin"
        fake_bin.mkdir(exist_ok=True)

        def fake_run(argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            if removes and ps_exit == 0:
                source = kwargs["env"]["SWITCHBOARD_TRASH_TARGET"]   # the bin takes the item by a rename, not a delete
                os.rename(source, str(fake_bin / Path(source).name))
            return subprocess.CompletedProcess(argv, ps_exit, stdout="", stderr=ps_stderr)

        def boom(*_a, **_k):
            raise AssertionError("hard delete on the Windows path")

        env = {k: v for k, v in self.env.items() if k != "SWITCHBOARD_TRASH_DIR"} if not keep_dir else dict(self.env)
        stack = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(m.sys, "platform", "win32"), mock.patch.object(m, "_is_windows", return_value=True),
            mock.patch.object(m, "_windows_plain", side_effect=lambda p: p),
            mock.patch.object(m, "_powershell_exe", return_value=ps_exe),
            mock.patch.object(m, "_windows_volume", return_value=volume or {
                "root": "C:\\", "drive_type": 3, "guid": "{b2d7c8c5-0000-4000-8000-000000000001}", "total": 10**12}),
            mock.patch.object(m, "_windows_bin_config", return_value=config or {"nuke": False, "policy_off": False, "max_bytes": None}),
            mock.patch.object(m.subprocess, "run", side_effect=fake_run),
            mock.patch.object(m.shutil, "rmtree", side_effect=boom),
            mock.patch.object(m.os, "remove", side_effect=boom), mock.patch.object(m.os, "unlink", side_effect=boom),
        ]
        return m, calls, stack

    def run_windows(self, stack, m, *args: str) -> tuple[int, str]:
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.ExitStack() as es:
            for patcher in stack:
                es.enter_context(patcher)
            with contextlib.redirect_stderr(err):
                status = m.run(list(args))
        return status, err.getvalue()

    def test_windows_success_goes_to_the_recycle_bin_and_logs_it(self) -> None:
        target = self.home / "scratch"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f.txt").write_text("x")
        m, calls, stack = self.windows()
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assertEqual(status, 0, err)
        self.assertFalse(target.exists())
        [call] = calls
        argv = call["argv"]
        self.assertEqual(argv[0], "C:/PS/powershell.exe")
        for flag in ("-NoProfile", "-NonInteractive", "-Command"):
            self.assertIn(flag, argv)
        script = argv[argv.index("-Command") + 1]
        self.assertIn("SendToRecycleBin", script)
        self.assertIn("OnlyErrorDialogs", script)
        self.assertNotIn(str(target), script, "the path goes in the environment, never in the script text")
        self.assertEqual(call["env"]["SWITCHBOARD_TRASH_TARGET"], str(target))
        self.assertIs(call["stdin"], subprocess.DEVNULL)
        self.assertIn("Recycle Bin", err)
        [row] = self.rows()
        self.assertEqual((row["action"], row["path"], row["trashed_via"], row["task_id"]),
                         ("trashed", str(target), "recycle-bin", "rt-000000000007"))
        self.assertIn("Recycle Bin", row["trashed_to"])
        self.assertFalse((self.home / ".switchboard-trash").exists())

    def assert_windows_fallback(self, status: int, err: str, target: Path, calls: list, *, reason: str, ran_ps: bool) -> None:
        self.assertEqual(status, 1, err)
        self.assertTrue(target.exists(), "nothing is deleted when the Recycle Bin cannot take it")
        self.assertEqual(bool(calls), ran_ps)
        self.assertIn("nothing was deleted", err)
        self.assertIn("Ask the owner", err)
        self.assertIn(reason, err)
        [row] = self.rows()
        self.assertEqual((row["action"], row["path"]), ("failed", str(target)))
        self.assertIn(reason, row["reason"])

    def test_windows_powershell_missing_asks_and_deletes_nothing(self) -> None:
        target = self.home / "scratch"
        target.mkdir()
        m, calls, stack = self.windows(ps_exe=None)
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assert_windows_fallback(status, err, target, calls, reason="powershell.exe was not found", ran_ps=False)

    def test_windows_powershell_error_asks_and_deletes_nothing(self) -> None:
        target = self.home / "scratch"
        target.mkdir()
        m, calls, stack = self.windows(ps_exit=1, ps_stderr="Exception calling DeleteDirectory")
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assert_windows_fallback(status, err, target, calls, reason="exited 1: Exception calling DeleteDirectory", ran_ps=True)

    def test_windows_powershell_launch_failure_asks(self) -> None:
        target = self.home / "scratch"
        target.mkdir()
        m, calls, stack = self.windows()
        stack.append(mock.patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 120)))
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assert_windows_fallback(status, err, target, calls, reason="powershell.exe failed", ran_ps=False)

    def test_windows_source_still_there_after_success_is_a_failure(self) -> None:
        target = self.home / "scratch"
        target.mkdir()
        m, calls, stack = self.windows(removes=False)
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assert_windows_fallback(status, err, target, calls, reason="still exists after the move", ran_ps=True)

    def test_windows_network_path_asks_without_running_powershell(self) -> None:
        m, calls, stack = self.windows()
        import contextlib
        with contextlib.ExitStack() as es:
            for patcher in stack:
                es.enter_context(patcher)
            for unc in (r"\\server\share\old", "//server/share/old", r"\\?\UNC\server\share\old"):
                with self.assertRaises(m.TrashUnavailable) as caught:
                    m._recycle_bin(unc)
                self.assertIn("network", str(caught.exception), unc)
        self.assertEqual(calls, [])

    def test_windows_unsafe_volumes_and_sizes_are_refused_before_any_move(self) -> None:
        target = self.home / "big"
        target.mkdir()
        (target / "blob").write_bytes(b"x" * 100)
        fixed = {"root": "C:\\", "drive_type": 3, "guid": "{g}", "total": 10**12}
        ok = {"nuke": False, "policy_off": False, "max_bytes": None}
        cases = [
            ("not a fixed local drive", dict(volume={**fixed, "drive_type": 2}), None),
            ("not a fixed local drive", dict(volume={**fixed, "drive_type": 4}), None),
            ("a policy turns the Recycle Bin off", dict(config={**ok, "policy_off": True}), None),
            ("deletes files instead of recycling", dict(config={**ok, "nuke": True}), None),
            ("too big for the Recycle Bin", dict(config={**ok, "max_bytes": 100}), None),   # 100 bytes in a 50 byte share
            ("longer than 240", dict(), (0, 300)),
        ]
        import contextlib
        for reason, kwargs, stats in cases:
            m, calls, stack = self.windows(**kwargs)
            with contextlib.ExitStack() as es:
                for patcher in stack:
                    es.enter_context(patcher)
                if stats:
                    es.enter_context(mock.patch.object(m, "_tree_stats", return_value=stats))
                self.assertIn(reason, m._recycle_refusal(str(target)) or "", reason)
                with self.assertRaises(m.TrashUnavailable):
                    m._recycle_bin(str(target))
            self.assertEqual(calls, [])
            self.assertTrue(target.exists())

    @needs_symlink
    def test_windows_unreadable_settings_or_a_link_are_refused(self) -> None:
        target = self.home / "d"
        target.mkdir()
        link = self.home / "lnk"
        link.symlink_to(target)
        import contextlib
        m, calls, stack = self.windows()
        stack.append(mock.patch.object(m, "_windows_bin_config", side_effect=KeyError("no key")))
        with contextlib.ExitStack() as es:
            for patcher in stack:
                es.enter_context(patcher)
            self.assertIn("cannot read the Recycle Bin settings", m._recycle_refusal(str(target)))
        m, calls, stack = self.windows()
        with contextlib.ExitStack() as es:
            for patcher in stack:
                es.enter_context(patcher)
            self.assertIn("link or junction", m._recycle_refusal(str(link)))
            self.assertIsNone(m._recycle_refusal(str(target)))

    def test_windows_never_hard_deletes_a_protected_alias(self) -> None:
        # Drive letters, Git Bash /c/ paths, case, trailing dots, 8.3 expansion: refused, untouched.
        import contextlib
        m, calls, stack = self.windows()
        winenv = {"HOME": r"C:\Users\Pat", "SWITCHBOARD_TRASH_REPO": r"C:\Users\Pat\Development\switchboard",
                  "SWITCHBOARD_TRASH_TEMP_PREFIXES": r"C:\Users\Pat\AppData\Local\Temp;"}
        aliases = [r"C:\Users\Pat", "/c/Users/Pat", "c:/users/pat", r"C:\Users", "/c/Users", r"C:\Windows", "C:/Windows/",
                   "C:\\", "/c", "c:", r"D:\Users", r"C:\Users\Pat\proj\.git", "c:/users/x/.git", "/c/Users/Pat/proj/.GIT",
                   r"C:\Users\Pat\Development\switchboard", "/c/users/pat/development/SWITCHBOARD", r"C:\Users\Pat\Development\switchboard\.git.",
                   r"C:\Users\Pat\Documents", r"C:\Users\Pat\Desktop ", r"C:\Users\Pat\AppData", r"\\?\C:\Windows"]
        with contextlib.ExitStack() as es:
            for patcher in stack:
                es.enter_context(patcher)
            es.enter_context(mock.patch.dict(os.environ, winenv))
            es.enter_context(mock.patch.object(m.os.path, "lexists", return_value=True))
            es.enter_context(mock.patch.object(m, "_absolute", side_effect=lambda a: a))
            for alias in aliases:
                import io
                import contextlib as cl
                err = io.StringIO()
                with cl.redirect_stderr(err):
                    status = m.run(["-rf", alias])
                self.assertEqual(status, 1, alias)
                self.assertIn("refused", err.getvalue(), alias)
        self.assertEqual(calls, [])
        self.assertEqual({r["action"] for r in self.rows()}, {"refused"})
        self.assertEqual(len(self.rows()), len(aliases))

    def test_windows_expands_a_short_name_before_the_refusals(self) -> None:
        m = self.load()
        buf_text = {"value": r"C:\Program Files"}

        class Kernel:
            @staticmethod
            def GetLongPathNameW(path, buf, size):
                buf.value = buf_text["value"]
                return len(buf.value)

        class Windll:
            kernel32 = Kernel

        with mock.patch("ctypes.windll", Windll, create=True):
            self.assertEqual(m._long_name(r"C:\PROGRA~1"), r"C:\Program Files")
            self.assertEqual(m._long_name(r"C:\plain"), r"C:\plain")   # no "~": untouched
        with mock.patch("ctypes.windll", None, create=True):
            self.assertEqual(m._long_name(r"C:\PROGRA~1"), r"C:\PROGRA~1")  # unreadable: as given
        with mock.patch.dict(os.environ, {"HOME": r"C:\Users\Pat", "SWITCHBOARD_TRASH_REPO": r"C:\Users\Pat\Development\switchboard"}):
            self.assertEqual(m.classify(r"C:\Program Files", windows=True)[0], "refuse")

    def test_windows_explicit_trash_folder_still_works_and_is_logged(self) -> None:
        target = self.home / "scratch"
        target.mkdir()
        m, calls, stack = self.windows(keep_dir=True)
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assertEqual(status, 0, err)
        self.assertEqual(calls, [])
        self.assertFalse(target.exists())
        [row] = self.rows()
        self.assertEqual(row["trashed_via"], "folder")
        self.assertTrue(row["trashed_to"].startswith(str(self.trash)))

    def test_summary_line_lists_what_stayed_in_place(self) -> None:
        m = self.load()
        line = m.summary_line([{"action": "failed", "path": r"C:\big", "reason": "too big for the Recycle Bin"}])
        self.assertEqual(line, r"Not trashed, still in place (1): C:\big (too big for the Recycle Bin).")

    def test_macos_log_rows_say_where_they_went(self) -> None:
        target = self.home / "mac"
        target.mkdir()
        self.assertEqual(self.run_helper("-rf", str(target)).returncode, 0)
        [row] = self.rows()
        self.assertEqual(row["trashed_via"], "folder")   # the hermetic run uses SWITCHBOARD_TRASH_DIR

    # ── Windows reserved device names ────────────────────
    # `rm -rf /dev/null` in Git Bash reached the helper as `nul`; Win32 reads <cwd>\nul as
    # the NUL device, so it "existed" and the Recycle Bin checks failed on the device.
    # A real file named nul (Git Bash `cmd > nul` makes one) needs the \\?\ form.

    def test_reserved_names_are_recognised_on_every_os(self) -> None:
        for name in ("nul", "NUL", "nul.txt", "nul ", "nul.", "con", "prn", "aux.tar.gz", "com1", "LPT9", "conin$"):
            self.assertTrue(winlink.is_reserved_name(name), name)
        for name in ("null", "nulx", "console", "com", "lpt", "my-nul", "nul-notes.md", "x.nul"):
            self.assertFalse(winlink.is_reserved_name(name), name)

    @unittest.skipIf(os.name == "nt", "off Windows only")
    def test_reserved_names_are_plain_paths_off_windows(self) -> None:
        self.assertIsNone(winlink.device_name(str(self.home / "nul")))
        self.assertEqual(winlink.real_path(str(self.home / "nul")), str(self.home / "nul"))
        target = self.home / "nul"
        target.write_text("x")
        self.assertEqual(self.run_helper("-rf", str(target)).returncode, 0)
        self.assertFalse(target.exists())

    @windows_only
    def test_windows_dev_null_from_git_bash_is_the_device_not_a_file(self) -> None:
        self.assertEqual(winlink.device_name(str(self.home / "nul")), "nul")
        self.assertIsNone(winlink.device_name(_reserved(self.home / "nul")))
        result = self.run_helper("-rf", "nul")   # what MSYS passes for `rm -rf /dev/null`
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NUL device", result.stderr)
        self.assertEqual(self.rows(), [], "nothing was there: no failed row for the report to call a stray file")
        plain = self.run_helper("nul")
        self.assertEqual(plain.returncode, 1, "without -f, a missing file is an error, as rm says")

    @windows_only
    def test_windows_real_nul_file_goes_to_the_trash_folder(self) -> None:
        folder = self.home / "scratch"
        folder.mkdir()
        with open(_reserved(folder / "nul"), "w") as handle:
            handle.write("stray")
        result = self.run_helper("-rf", "nul", cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(_reserved(folder / "nul")))
        [row] = self.rows()
        self.assertEqual(row["action"], "trashed")
        [moved] = [p for p in os.listdir(_reserved(self.trash)) if p.startswith("nul (switchboard ")]
        with open(_reserved(self.trash / moved)) as handle:
            self.assertEqual(handle.read(), "stray")

    @windows_only
    def test_windows_temp_tree_holding_nul_is_deleted(self) -> None:
        temp = Path(self.tmp.name).resolve() / "tmp"
        tree = temp / "build-scratch"
        (tree / "sub").mkdir(parents=True)
        os.mkdir(_reserved(tree / "nul"))
        for where in (tree / "nul" / "nul", tree / "sub" / "nul", tree / "con"):
            with open(_reserved(where), "w") as handle:
                handle.write("x")
        self.env["SWITCHBOARD_TRASH_TEMP_PREFIXES"] = str(temp) + ";"
        result = self.run_helper("-rf", str(tree))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())

    def windows_reserved(self, **kwargs):
        """windows() on a real Windows host, with the real _windows_plain (the \\\\?\\ form must be stripped)."""
        m, calls, stack = self.windows(**kwargs)
        return m, calls, [p for p in stack if getattr(p, "attribute", None) != "_windows_plain"]

    @windows_only
    def test_windows_nul_file_is_renamed_then_recycled(self) -> None:
        folder = self.home / "scratch"
        folder.mkdir()
        with open(_reserved(folder / "nul"), "w") as handle:
            handle.write("stray")
        m, calls, stack = self.windows_reserved()
        status, err = self.run_windows(stack, m, "-rf", str(folder / "nul"))
        self.assertEqual(status, 0, err)
        [call] = calls
        self.assertEqual(call["env"]["SWITCHBOARD_TRASH_TARGET"], str(folder / "nul (switchboard renamed)"))
        self.assertFalse(os.path.lexists(_reserved(folder / "nul")))
        self.assertTrue((Path(self.tmp.name) / "fake-recycle-bin" / "nul (switchboard renamed)").is_file())
        [row] = self.rows()
        self.assertEqual((row["action"], row["trashed_via"]), ("trashed", "recycle-bin"))
        self.assertIn("reserves the name", row["trashed_to"])

    @windows_only
    def test_windows_folder_holding_reserved_names_is_recycled_whole(self) -> None:
        # SHFileOperation refuses a folder with a nul inside ("This function is not supported on this system").
        target = self.home / "probe"
        (target / "sub").mkdir(parents=True)
        os.mkdir(_reserved(target / "nul"))
        for where in (target / "nul" / "nul", target / "sub" / "nul.txt", target / "keep.txt"):
            with open(_reserved(where), "w") as handle:
                handle.write("x")
        m, calls, stack = self.windows_reserved()
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assertEqual(status, 0, err)
        [call] = calls
        self.assertEqual(call["env"]["SWITCHBOARD_TRASH_TARGET"], str(target))
        binned = Path(self.tmp.name) / "fake-recycle-bin" / "probe"
        self.assertEqual(sorted(os.listdir(binned)), ["keep.txt", "nul (switchboard renamed)", "sub"])
        self.assertEqual(os.listdir(binned / "nul (switchboard renamed)"), ["nul (switchboard renamed)"])
        if winlink.device_name(str(target / "sub" / "nul.txt")):   # Windows 10 reserves nul.txt too; 11 does not
            self.assertEqual(os.listdir(binned / "sub"), ["nul_txt (switchboard renamed)"])
        else:
            self.assertEqual(os.listdir(binned / "sub"), ["nul.txt"])

    @windows_only
    def test_windows_failed_move_renames_reserved_names_back(self) -> None:
        target = self.home / "probe"
        target.mkdir()
        os.mkdir(_reserved(target / "nul"))
        with open(_reserved(target / "nul" / "nul"), "w") as handle:
            handle.write("x")
        m, calls, stack = self.windows_reserved(ps_exit=1, ps_stderr="boom")
        status, err = self.run_windows(stack, m, "-rf", str(target / "nul"))
        self.assertEqual(status, 1)
        self.assertIn("nothing was deleted", err)
        self.assertEqual(calls[0]["env"]["SWITCHBOARD_TRASH_TARGET"], str(target / "nul (switchboard renamed)"))
        self.assertEqual(os.listdir(_reserved(target)), ["nul"])
        self.assertEqual(os.listdir(_reserved(target / "nul")), ["nul"])
        [row] = self.rows()
        self.assertEqual(row["action"], "failed")

    @windows_only
    def test_windows_refused_move_renames_nothing(self) -> None:
        # The Recycle Bin checks run before any rename: a volume that deletes for good leaves names as they are.
        target = self.home / "probe"
        target.mkdir()
        with open(_reserved(target / "nul"), "w") as handle:
            handle.write("x")
        m, calls, stack = self.windows_reserved(config={"nuke": True, "policy_off": False, "max_bytes": None})
        status, err = self.run_windows(stack, m, "-rf", str(target))
        self.assertEqual(status, 1)
        self.assertEqual(calls, [])
        self.assertEqual(os.listdir(target), ["nul"])
        self.assertIn("deletes files instead of recycling", err)

    @windows_only
    def test_windows_failed_undo_says_where_the_item_is(self) -> None:
        target = self.home / "probe"
        target.mkdir()
        with open(_reserved(target / "nul"), "w") as handle:
            handle.write("x")
        m, calls, stack = self.windows_reserved(ps_exit=1, ps_stderr="boom")
        real_rename = os.rename

        def rename(source, dest):
            if str(source).endswith("(switchboard renamed)"):
                raise PermissionError(13, "in use")   # the rename back fails
            return real_rename(source, dest)
        stack.append(mock.patch.object(m.os, "rename", side_effect=rename))
        status, err = self.run_windows(stack, m, "-rf", str(target / "nul"))
        self.assertEqual(status, 1)
        renamed = str(target / "nul (switchboard renamed)")
        self.assertIn(f"still on disk at: {renamed}", err)
        self.assertNotIn("is gone", err)
        [row] = self.rows()
        self.assertEqual((row["action"], row["now_at"]), ("failed", renamed))
        line = m.summary_line(self.rows())
        self.assertIn("Not trashed, still in place (1)", line)

    @windows_only
    def test_windows_extended_path_forms(self) -> None:
        drive = os.path.splitdrive(os.getcwd())[0]
        self.assertEqual(winlink.extended_path(r"C:\x\nul\aux"), r"\\?\C:\x\nul\aux")
        self.assertEqual(winlink.extended_path("/c/x/nul"), r"\\?\c:\x\nul")
        self.assertEqual(winlink.extended_path(r"\tmp\nul"), "\\\\?\\" + drive + r"\tmp\nul")
        self.assertEqual(winlink.extended_path(r"\a\nul"), "\\\\?\\" + drive + r"\a\nul", "a rooted path is not /a/")
        self.assertEqual(winlink.extended_path(r"\\server\share\nul"), r"\\?\UNC\server\share\nul")
        self.assertEqual(winlink.extended_path(r"C:\x\y\..\nul"), r"\\?\C:\x\nul")
        self.assertEqual(winlink.extended_path("nul"), "\\\\?\\" + os.path.join(os.getcwd(), "nul"))
        self.assertEqual(winlink.extended_path("C:\\"), "\\\\?\\C:\\")

    @unittest.skipIf(os.name == "nt", "a POSIX device node")
    def test_dev_null_is_never_trashed(self) -> None:
        result = self.run_helper("-rf", "/dev/null")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is a device", result.stderr)
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.trash.exists() and any(self.trash.iterdir()))

    @windows_only
    def test_winlink_rmtree_removes_reserved_names(self) -> None:
        tree = self.home / "tree"
        tree.mkdir()
        os.mkdir(_reserved(tree / "nul"))
        with open(_reserved(tree / "nul" / "aux"), "w") as handle:
            handle.write("x")
        with open(_reserved(tree / "nul" / "nul"), "w") as handle:
            handle.write("x")
        winlink.rmtree(str(tree))
        self.assertFalse(tree.exists())


if __name__ == "__main__":
    unittest.main()
