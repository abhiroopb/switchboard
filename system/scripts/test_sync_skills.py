#!/usr/bin/env python3
"""sync-skills.py: the automatic skill sync.

Run: python3 system/scripts/test_sync_skills.py
Every test uses a temporary home, state file and locks dir, and stubs the CoS send and
the trash, so nothing reaches ~/.claude, ~/.agents, the Recycle Bin or a live tab.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sync_skills", HERE / "sync-skills.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
REPO = HERE.parents[1]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state" / "skill-sync.json"
        self.edits = self.tmp / "skill-edits"
        env = mock.patch.dict(os.environ, {"SWITCHBOARD_LOCKS_DIR": str(self.tmp / "locks"), "SWITCHBOARD_PERSONAL_SKILLS": "off",
                                           "SWITCHBOARD_SKILL_EDITS_DIR": str(self.edits)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("SKILL_SYNC_DISABLED", "SWITCHBOARD_TEST_SANDBOX"):
            os.environ.pop(key, None)
        self.sent: list[str] = []
        cos = mock.patch.object(sync, "cos_line", side_effect=lambda main, text, status="done": self.sent.append(text) or True)
        cos.start()
        self.addCleanup(cos.stop)
        self.trashed: list[str] = []
        trash = mock.patch.object(sync, "move_to_trash", side_effect=lambda p, main: (self.trashed.append(p.name), (True, "trash"))[1])
        trash.start()
        self.addCleanup(trash.stop)
        main = mock.patch.object(sync, "main_checkout", return_value=REPO)
        main.start()
        self.addCleanup(main.stop)

    def run_sync(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sync.main_cli(["--home", str(self.home), "--state", str(self.state), *args])
        return code, out.getvalue()


class Skips(Base):
    def test_disabled_does_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"SKILL_SYNC_DISABLED": "1"}):
            code, out = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("disabled", out)
        self.assertFalse((self.home / ".claude").exists())

    def test_the_sandbox_never_touches_the_real_home(self) -> None:
        with mock.patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": "1"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sync.main_cli(["--state", str(self.state)]), 0)
        self.assertIn("test sandbox", out.getvalue())

    def test_a_held_landing_lock_skips_the_run(self) -> None:
        with sync.try_lock(self.tmp / "locks" / "landing.lock") as held:
            self.assertTrue(held)
            code, out = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("landing is in flight", out)
        self.assertFalse((self.home / ".claude" / "skills").exists())

    def test_a_second_sync_skips_while_one_runs(self) -> None:
        with sync.try_lock(self.tmp / "locks" / "skill-sync.lock"):
            code, out = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("another sync is running", out)


class FullRun(Base):
    def test_a_fresh_home_gets_every_skill_and_the_todo_override(self) -> None:
        with mock.patch.object(sync, "company_cli", return_value=False):
            code, out = self.run_sync("--notify")
        self.assertEqual(code, 0, out)
        dest = self.home / ".claude" / "skills"
        self.assertTrue((dest / "chief-of-staff" / ".switchboard-source").exists())
        self.assertTrue((dest / "humanizer" / "SKILL.md").exists())
        override = REPO / "system" / "skill-overrides" / "todo" / "SKILL.md"
        self.assertEqual((dest / "todo" / "SKILL.md").read_bytes(), override.read_bytes())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["last_result"], "ok")
        self.assertEqual(self.sent, [])
        code, out = self.run_sync("--notify")
        self.assertEqual(code, 0, out)
        self.assertNotIn("✓ chief-of-staff", out, "a second run copies nothing")
        self.assertIn("settings.json left alone", out)

    def test_an_edited_copy_is_saved_and_cos_hears_once(self) -> None:
        with mock.patch.object(sync, "company_cli", return_value=False):
            self.assertEqual(self.run_sync()[0], 0)
            skill = self.home / ".claude" / "skills" / "recall" / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal hand edit\n", encoding="utf-8")
            code, out = self.run_sync("--notify")
            self.assertEqual(code, 0, out)
            self.assertNotIn("local hand edit", skill.read_text(encoding="utf-8"))
            saved = list(self.edits.glob("recall-*"))
            self.assertEqual(len(saved), 1)
            self.assertIn("local hand edit", (saved[0] / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(len(self.sent), 1)
            self.assertIn("recall had edits", self.sent[0])
            self.assertEqual(self.run_sync("--notify")[0], 0)
            self.assertEqual(len(self.sent), 1, "nothing new, nothing sent")


class Notices(Base):
    def test_an_unmanaged_folder_is_news_once_and_again_after_it_comes_back(self) -> None:
        state: dict = {}
        item = {"kind": "unmanaged", "name": "dropped", "text": "dropped is an unmanaged skill folder"}
        self.assertEqual(len(sync.notify(REPO, state, [item], False, "", send=True)), 1)
        self.assertIn("Follow-up: the owner says whether dropped moves", self.sent[0])
        self.assertEqual(sync.notify(REPO, state, [item], False, "", send=True), [])
        sync.notify(REPO, state, [], False, "", send=True)
        self.assertEqual(len(sync.notify(REPO, state, [item], False, "", send=True)), 1)
        self.assertEqual(len(self.sent), 2)

    def test_three_failures_in_a_row_send_one_line(self) -> None:
        state: dict = {"last_ok_at": "2026-11-09T00:00:00Z"}
        for _ in range(2):
            self.assertEqual(sync.notify(REPO, state, [], True, "boom", send=True), [])
        self.assertEqual(len(sync.notify(REPO, state, [], True, "boom", send=True)), 1)
        self.assertEqual(sync.notify(REPO, state, [], True, "boom", send=True), [])
        self.assertIn("failed 3 runs in a row", self.sent[0])
        sync.notify(REPO, state, [], False, "", send=True)
        self.assertEqual(state["failure_streak"], 0)

    def test_without_notify_nothing_is_sent_and_nothing_is_marked_sent(self) -> None:
        state: dict = {}
        item = {"kind": "unmanaged", "name": "dropped", "text": "x"}
        sync.notify(REPO, state, [item], False, "", send=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(state["sent"], {})


class AmpCopies(Base):
    def make(self, root: Path, name: str, marker: bool = False) -> Path:
        path = root / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        if marker:
            (path / ".switchboard-source").write_text("{}", encoding="utf-8")
        return path

    def test_only_old_repo_copies_with_a_managed_twin_go_to_the_trash(self) -> None:
        agents, claude = self.home / ".agents" / "skills", self.home / ".claude" / "skills"
        self.make(agents, "chief-of-staff")
        self.make(claude, "chief-of-staff", marker=True)
        self.make(agents, "todo")
        self.make(claude, "todo", marker=True)
        self.make(agents, "drafting-docs")                  # no managed twin yet: stays
        self.make(agents, "recall", marker=True)            # made for Codex: stays
        self.make(claude, "recall", marker=True)
        self.make(agents, "slack")                          # catalog skill, not a repo skill: stays
        self.make(claude, "slack", marker=True)
        lines = sync.tidy_agents_copies(REPO, self.home, dry_run=False)
        self.assertEqual(self.trashed, ["chief-of-staff"], lines)

    def test_a_dry_run_moves_nothing(self) -> None:
        agents, claude = self.home / ".agents" / "skills", self.home / ".claude" / "skills"
        self.make(agents, "recall")
        self.make(claude, "recall", marker=True)
        lines = sync.tidy_agents_copies(REPO, self.home, dry_run=True)
        self.assertEqual(self.trashed, [])
        self.assertIn("would move", lines[0])


class Codex(Base):
    def test_codex_gets_marked_copies_and_the_library_and_todo_stay(self) -> None:
        agents = self.home / ".agents" / "skills"
        old = agents / "recall"                       # an old unmarked copy from install-amp-skills.sh
        old.mkdir(parents=True)
        (old / "SKILL.md").write_text("---\nname: recall\n---\nold\n", encoding="utf-8")
        library = agents / "slack"                    # a catalog skill with no repo source
        library.mkdir()
        (library / "SKILL.md").write_text("---\nname: slack\n---\nlibrary\n", encoding="utf-8")
        real_which = sync.shutil.which
        with mock.patch.object(sync, "company_cli", return_value=False), \
                mock.patch.object(sync.shutil, "which", side_effect=lambda n: "codex" if n == "codex" else real_which(n)):
            # The trash stub records the move; remove the folder as the real helper would.
            with mock.patch.object(sync, "move_to_trash",
                                   side_effect=lambda p, main: (self.trashed.append(p.name), sync.shutil.rmtree(p), (True, "trash"))[2]):
                code, out = self.run_sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.trashed, ["recall"])
        self.assertTrue((agents / "recall" / ".switchboard-source").exists(), "Codex gets a managed copy back")
        self.assertNotIn("STALE-COPY-7Q", (agents / "recall" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual((library / "SKILL.md").read_text(encoding="utf-8"), "---\nname: slack\n---\nlibrary\n")
        self.assertFalse((library / ".switchboard-source").exists())
        self.assertTrue((agents / "todo" / "SKILL.md").exists())
        self.assertFalse((agents / "todo" / ".switchboard-source").exists(), "todo is the library's, not a managed copy")
        code, out = self.run_sync()
        self.assertEqual(code, 0, out)


class Junctions(Base):
    @unittest.skipUnless(os.name == "nt" and hasattr(Path, "is_junction"), "Windows junctions")
    def test_a_junction_in_the_library_folder_is_never_a_tidy_candidate(self) -> None:
        import _winapi
        agents, claude = self.home / ".agents" / "skills", self.home / ".claude" / "skills"
        agents.mkdir(parents=True)
        target = claude / "recall"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: recall\n---\n", encoding="utf-8")
        (target / ".switchboard-source").write_text("{}", encoding="utf-8")
        _winapi.CreateJunction(str(REPO / ".agents" / "skills" / "recall"), str(agents / "recall"))
        try:
            self.assertEqual(sync.tidy_agents_copies(REPO, self.home, dry_run=False), [])
            self.assertEqual(self.trashed, [])
        finally:
            os.rmdir(agents / "recall")
        self.assertTrue((REPO / ".agents" / "skills" / "recall" / "SKILL.md").exists())


class Husks(Base):
    """A deleted skill's leftover __pycache__."""

    def setUp(self) -> None:
        super().setUp()
        import subprocess
        self.main = self.tmp / "main"
        self.skills = self.main / ".agents" / "skills"
        self.skills.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        subprocess.run(["git", "-C", str(self.main), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.main), "config", "user.name", "t"], check=True)
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.main), *a], check=True, capture_output=True)
        # The stubbed trash records the name; make it also take the folder away, like the real one.
        import shutil
        self.real_trash = mock.patch.object(
            sync, "move_to_trash",
            side_effect=lambda p, main: (self.trashed.append(p.name), shutil.rmtree(p), (True, "the trash"))[2])
        self.real_trash.start()
        self.addCleanup(self.real_trash.stop)

    def write(self, rel: str, text: str = "x", root: Path | None = None) -> Path:
        path = (root or self.main) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def age(self, folder: Path, seconds: int = 2 * 3600) -> None:
        """Backdate a folder and everything in it, as a husk left days ago would be."""
        import time
        stamp = time.time() - seconds
        for p in [folder, *folder.rglob("*")]:
            os.utime(p, (stamp, stamp))

    def test_a_deleted_skill_leaves_a_husk_that_the_sync_moves_to_the_trash(self) -> None:
        self.write(".agents/skills/gone/SKILL.md", "---\nname: gone\n---\n")
        self.write(".agents/skills/gone/scripts/tool.py")
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "add gone")
        self.write(".agents/skills/gone/scripts/__pycache__/tool.cpython-312.pyc")
        self.write(".agents/skills/gone/tests/__pycache__/test_tool.cpython-312.pyc")
        self.git("rm", "-rq", ".agents/skills/gone")
        self.git("commit", "-qm", "remove gone")
        self.assertTrue((self.skills / "gone").is_dir(), "git leaves the ignored cache behind")
        self.age(self.skills / "gone")

        planned = sync.prune_skill_husks(self.main, self.home, dry_run=True)
        self.assertEqual(len(planned), 1)
        self.assertIn("would move", planned[0])
        self.assertTrue((self.skills / "gone").is_dir(), "a dry run changes nothing")
        self.assertEqual(self.trashed, [])

        lines = sync.prune_skill_husks(self.main, self.home, dry_run=False)
        self.assertEqual(lines, ["♻ gone: moved the empty skill folder .agents/skills/gone (2 cache files) to the trash"])
        self.assertEqual(self.trashed, ["gone"])
        self.assertFalse((self.skills / "gone").exists())

    def test_a_folder_touched_in_the_last_hour_is_left_while_files_may_still_arrive(self) -> None:
        self.write(".agents/skills/arriving/__pycache__/a.pyc")
        self.assertEqual(sync.prune_skill_husks(self.main, self.home, dry_run=False), [])
        self.assertTrue((self.skills / "arriving").is_dir())
        self.age(self.skills / "arriving", seconds=600)
        self.assertEqual(sync.prune_skill_husks(self.main, self.home, dry_run=False), [])
        self.assertEqual(self.trashed, [])

    def test_live_skills_folders_with_real_files_and_tracked_files_are_kept(self) -> None:
        self.write(".agents/skills/live/SKILL.md")
        self.write(".agents/skills/live/__pycache__/x.pyc")
        self.write(".agents/skills/notes-only/README.md")
        self.write(".agents/skills/notes-only/__pycache__/x.pyc")
        self.write(".agents/skills/tracked/__pycache__/keep.pyc")
        self.git("add", "-f", ".agents/skills/tracked/__pycache__/keep.pyc")
        self.git("commit", "-qm", "tracked cache")
        for name in ("live", "notes-only", "tracked"):
            self.age(self.skills / name)
        self.assertEqual(sync.prune_skill_husks(self.main, self.home, dry_run=False), [])
        for name in ("live", "notes-only", "tracked"):
            self.assertTrue((self.skills / name).is_dir(), name)

    def test_empty_folders_and_the_private_skills_folder_are_never_touched(self) -> None:
        empty = self.skills / "new-skill" / "scripts"
        empty.mkdir(parents=True)
        self.age(self.skills / "new-skill")
        private = self.write("local/skills/private-skill/__pycache__/a.pyc").parents[1]
        self.age(private)
        self.assertEqual(sync.prune_skill_husks(self.main, self.home, dry_run=False), [])
        self.assertTrue(empty.is_dir(), "an empty folder may be a skill still arriving")
        self.assertTrue(private.is_dir(), "the private skills folder is gitignored and synced")

    def test_installed_husks_go_and_claude_own_folders_stay(self) -> None:
        claude = self.home / ".claude" / "skills"
        self.write("synced/.DS_Store", root=claude)
        (claude / "empty-now").mkdir()
        self.write("old-skill/scripts/__pycache__/a.pyc", root=claude)
        for name in ("synced", "empty-now", "old-skill"):
            self.age(claude / name)
        lines = sync.prune_skill_husks(self.main, self.home, dry_run=False)
        self.assertEqual(lines, ["♻ old-skill: moved the empty skill folder ~/.claude/skills/old-skill (1 cache files) to the trash"])
        self.assertTrue((claude / "synced").is_dir())
        self.assertTrue((claude / "empty-now").is_dir())

    def test_a_refused_trash_is_an_error_line_and_the_folder_stays(self) -> None:
        self.write(".agents/skills/gone/__pycache__/a.pyc")
        self.age(self.skills / "gone")
        with mock.patch.object(sync, "move_to_trash", return_value=(False, "bin refused")):
            lines = sync.prune_skill_husks(self.main, self.home, dry_run=False)
        self.assertEqual(lines, ["✗ gone: empty skill folder .agents/skills/gone not moved: bin refused"])
        self.assertTrue((self.skills / "gone").is_dir())

    def test_cache_path_rule(self) -> None:
        for rel in ("scripts/__pycache__/a.cpython-312.pyc", "a.pyc", ".DS_Store", "x/.pytest_cache/v/cache/nodeids"):
            self.assertTrue(sync.is_cache_path(Path(rel)), rel)
        for rel in ("SKILL.md", "scripts/tool.py", "__pycache__", "reference/notes.md"):
            self.assertFalse(sync.is_cache_path(Path(rel)), rel)


class MacKitV8Port(Base):
    """The retried swap and the override skip."""

    def test_a_held_state_file_is_retried_on_windows_then_replaced(self) -> None:
        calls: list[int] = []
        real = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("held by another process")
            return real(src, dst)

        with mock.patch.object(sync.os, "name", "nt"), mock.patch.object(sync.os, "replace", side_effect=flaky), \
             mock.patch("time.sleep"):
            sync.save_state(self.state, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8")), {"ok": True})

    def test_off_windows_a_permission_error_is_not_retried(self) -> None:
        with mock.patch.object(sync.os, "name", "posix"), \
             mock.patch.object(sync.os, "replace", side_effect=PermissionError("denied")) as replaced:
            with self.assertRaises(PermissionError):
                sync._os_replace("a", "b")
        self.assertEqual(replaced.call_count, 1)

    def overrides(self, code: int, out: str) -> tuple[bool, str]:
        done = mock.Mock(returncode=code, stdout=out, stderr="")
        (self.home / ".agents" / "skills" / "todo").mkdir(parents=True, exist_ok=True)   # take the script path
        with mock.patch.object(sync, "run", return_value=done):
            return sync.apply_overrides(REPO, self.home)

    def test_only_skill_not_installed_failures_are_a_skip(self) -> None:
        ok, detail = self.overrides(3, "\u2717 firecrawl: skill not installed\n\u2717 gdrive: skill not installed\n")
        self.assertTrue(ok)
        self.assertEqual(detail, "2 override file(s) skipped: skill not installed here")

    def test_a_skip_beside_an_unprinted_failure_still_fails(self) -> None:
        # Under set -e a locked mv aborts with only its own error, so exit 1
        # with skip lines is a failure; only the script's exit 3 means skips alone.
        ok, detail = self.overrides(1, "\u2717 firecrawl: skill not installed\nmv: cannot move: Permission denied\n")
        self.assertFalse(ok)
        self.assertIn("Permission denied", detail)

    def test_a_real_override_failure_still_fails(self) -> None:
        ok, detail = self.overrides(1, "\u2717 firecrawl: skill not installed\n\u2717 todo: patch did not apply\n")
        self.assertFalse(ok)
        self.assertIn("patch did not apply", detail)
        self.assertNotIn("firecrawl", detail)


if __name__ == "__main__":
    unittest.main()
