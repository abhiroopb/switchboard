#!/usr/bin/env python3
"""A plain install-claude-skills.py run passes when an entry may be absent on this
platform (`absent_ok_on`), and still fails for a skill that should exist.

Run: python3 system/scripts/test_install_claude_skills.py
Every run uses a temporary home and manifest, so it never touches ~/.claude.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("install_claude_skills", HERE / "install-claude-skills.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

MANIFEST = """\
destination: ~/.claude/skills
sources:
  agents: ~/.agents/skills
skills:
  - {name: present, source: agents}
%s
"""
OPTIONAL_NAME = "  - {name: optional-skill, source: agents, absent_ok_on: [win32]}"
OPTIONAL_PATTERN = '  - {pattern: "optional-*", source: agents, absent_ok_on: [win32]}'
REQUIRED_NAME = "  - {name: required-skill, source: agents}"
REQUIRED_PATTERN = '  - {pattern: "required-*", source: agents}'


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        skill = self.home / ".agents" / "skills" / "present"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: present\n---\n", encoding="utf-8")
        self.config = Path(tmp.name) / "config"
        self.config.mkdir()

    def run_installer(self, platform: str, *args: str, manifest: Path | None = None) -> tuple[int, str]:
        """Return (exit code, stdout). A load_manifest refusal is exit 1 with its message."""
        out = io.StringIO()
        argv = ["--manifest", str(manifest), "--home", str(self.home), *args]
        with mock.patch.object(installer, "PLATFORM", platform), contextlib.redirect_stdout(out):
            try:
                return installer.main(argv), out.getvalue()
            except SystemExit as exc:
                return 1, f"{out.getvalue()}{exc.code}"

    def manifest(self, *entries: str) -> Path:
        path = self.config / "claude-skills-manifest.yaml"
        path.write_text(MANIFEST % "\n".join(entries), encoding="utf-8")
        return path


class AbsentOkOn(Base):
    def test_declared_entries_are_dormant_and_a_plain_run_passes(self) -> None:
        manifest = self.manifest(OPTIONAL_NAME, OPTIONAL_PATTERN)
        code, out = self.run_installer("win32", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertIn("· optional-skill: dormant", out)
        self.assertIn("· pattern optional-*: dormant", out)
        self.assertIn("✓ present: installed", out)
        self.assertTrue((self.home / ".claude" / "skills" / "present" / "SKILL.md").exists())
        self.assertFalse((self.home / ".claude" / "skills" / "optional-skill").exists())

    def test_declared_entries_pass_the_check_too(self) -> None:
        manifest = self.manifest(OPTIONAL_NAME, OPTIONAL_PATTERN)
        self.assertEqual(self.run_installer("win32", manifest=manifest)[0], 0)
        code, out = self.run_installer("win32", "--check", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertIn("· optional-skill: dormant", out)
        self.assertIn("= present: in sync", out)

    def test_missing_skill_without_the_field_still_fails(self) -> None:
        manifest = self.manifest(OPTIONAL_NAME, REQUIRED_NAME)
        code, out = self.run_installer("win32", manifest=manifest)
        self.assertEqual(code, 1, out)
        self.assertIn("✗ required-skill: source has no SKILL.md", out)
        self.assertIn("· optional-skill: dormant", out)
        code, out = self.run_installer("win32", "--check", manifest=manifest)
        self.assertEqual(code, 1, out)
        self.assertIn("✗ required-skill: source missing", out)

    def test_missing_pattern_without_the_field_still_fails(self) -> None:
        code, out = self.run_installer("win32", manifest=self.manifest(REQUIRED_PATTERN))
        self.assertEqual(code, 1, out)
        self.assertIn("pattern required-*: no skill matches", out)

    def test_mac_stays_strict_for_entries_declared_on_windows_only(self) -> None:
        code, out = self.run_installer("darwin", manifest=self.manifest(OPTIONAL_NAME))
        self.assertEqual(code, 1, out)
        self.assertIn("✗ optional-skill: source has no SKILL.md", out)
        code, out = self.run_installer("darwin", manifest=self.manifest(OPTIONAL_PATTERN))
        self.assertEqual(code, 1, out)
        self.assertIn("pattern optional-*: no skill matches", out)

    def test_a_present_declared_skill_installs_normally(self) -> None:
        skill = self.home / ".agents" / "skills" / "optional-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: optional-skill\n---\n", encoding="utf-8")
        code, out = self.run_installer("win32", manifest=self.manifest(OPTIONAL_NAME))
        self.assertEqual(code, 0, out)
        self.assertIn("✓ optional-skill: installed", out)

    def test_skip_missing_still_covers_every_entry(self) -> None:
        manifest = self.manifest(REQUIRED_NAME, REQUIRED_PATTERN)
        code, out = self.run_installer("darwin", "--skip-missing", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertIn("· required-skill: dormant", out)
        self.assertIn("· pattern required-*: dormant", out)


LOCAL_MANIFEST = """\
destination: ~/.claude/skills
sources:
  agents: ~/.agents/skills
  local: local/skills
skills:
  - {name: present, source: agents}
  - {pattern: "*", source: local, machine_gate: PM_OS_TEST_GATE, absent_ok_on: [win32, darwin, linux]}
"""


class LocalBase(Base):
    """A temporary repo with a gated local/skills pattern; ROOT points at it, so backups stay in it."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.home.parent / "repo"
        self.repo.mkdir()
        patcher = mock.patch.object(installer, "ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(installer.os.environ, {"PM_OS_MACHINE": "thisbox"})
        env.start()
        self.addCleanup(env.stop)
        installer.os.environ.pop("PM_OS_TEST_GATE", None)
        self.local_manifest = self.config / "claude-skills-manifest.yaml"
        self.local_manifest.write_text(LOCAL_MANIFEST, encoding="utf-8")
        self.dest = self.home / ".claude" / "skills"

    def gate_on(self, machine: str = "THISBOX") -> None:
        path = self.repo / "local" / "machines" / f"{machine}.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("OTHER=1\nPM_OS_TEST_GATE=on\n", encoding="utf-8")

    def source(self, name: str, body: str = "body\n") -> Path:
        skill = self.repo / "local" / "skills" / name
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")
        (skill / "scripts" / "tool.py").write_bytes(b"print('x')\r\n")
        return skill

    def managed_copy(self, name: str, kind: str = "local") -> Path:
        copy = self.dest / name
        copy.mkdir(parents=True)
        (copy / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        marker = {"source": "x", "source_kind": kind, "hash": installer.tree_hash(copy)}
        (copy / installer.MARKER).write_text(installer.json.dumps(marker), encoding="utf-8")
        return copy

    def run_local(self, platform: str, *args: str) -> tuple[int, str]:
        return self.run_installer(platform, *args, manifest=self.local_manifest)


class LocalSource(LocalBase):
    """Private skills: gitignored source, a pattern in the manifest, a per-machine gate."""

    def test_missing_root_is_dormant_and_passes_on_every_platform(self) -> None:
        self.gate_on()
        for platform in ("win32", "darwin", "linux"):
            for args in ((), ("--check",)):
                code, out = self.run_local(platform, *args)
                self.assertEqual(code, 0, out)
                self.assertIn("· pattern *: dormant (no source under", out)

    def test_prune_with_a_missing_root_deletes_nothing(self) -> None:
        # Wrong impl: the dormant pattern lists no names, so every copy looks unlisted and goes.
        self.gate_on()
        copy = self.managed_copy("private-one")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertTrue((copy / "SKILL.md").exists(), out)
        self.assertIn("· private-one: kept, its source is dormant", out)
        code, out = self.run_local("win32", "--check")
        self.assertEqual(code, 0, out)

    def test_prune_with_an_empty_root_deletes_nothing(self) -> None:
        self.gate_on()
        (self.repo / "local" / "skills").mkdir(parents=True)
        copy = self.managed_copy("private-one")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertTrue(copy.exists(), out)

    def test_prune_with_a_closed_gate_deletes_nothing(self) -> None:
        self.source("private-one")
        copy = self.managed_copy("private-one")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertTrue(copy.exists(), out)
        self.assertIn("PM_OS_TEST_GATE is not on for this machine", out)

    def test_a_dry_prune_with_a_missing_root_plans_no_delete(self) -> None:
        self.gate_on()
        self.managed_copy("private-one")
        code, out = self.run_local("win32", "--prune", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertNotIn("would remove", out)

    def test_the_hold_covers_only_that_source(self) -> None:
        # Wrong impl: any dormant pattern protects every managed copy, so prune stops working.
        self.gate_on()
        kept = self.managed_copy("private-one")
        other = self.managed_copy("old-library-skill", kind="agents")
        code, out = self.run_local("win32", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("− old-library-skill: managed copy not in manifest", out)
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertTrue(kept.exists(), out)
        self.assertFalse(other.exists(), out)

    def test_a_marker_with_no_origin_is_kept_while_dormant(self) -> None:
        self.gate_on()
        copy = self.managed_copy("private-one")
        (copy / installer.MARKER).write_text("not json", encoding="utf-8")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertTrue(copy.exists(), out)

    def test_a_skill_removed_from_a_present_root_is_still_pruned(self) -> None:
        self.gate_on()
        self.source("private-one")
        gone = self.managed_copy("private-two")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(gone.exists(), out)
        self.assertTrue((self.dest / "private-one" / "SKILL.md").exists())

    def test_closed_gate_installs_nothing_even_with_the_source_on_disk(self) -> None:
        # The synced-folder case: the folder reached a machine that was never switched on.
        self.source("private-one")
        for args in ((), ("--skip-missing",), ("--force",)):
            code, out = self.run_local("darwin", *args)
            self.assertEqual(code, 0, out)
            self.assertFalse((self.dest / "private-one").exists(), out)
            self.assertNotIn("private-one", out, "a closed gate does not read the root")

    def test_another_machines_file_does_not_open_the_gate(self) -> None:
        self.source("private-one")
        self.gate_on("OWNERSLAPTOP")
        code, out = self.run_local("darwin")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.dest / "private-one").exists(), out)

    def test_only_on_opens_the_gate(self) -> None:
        self.source("private-one")
        path = self.repo / "local" / "machines" / "THISBOX.env"
        path.parent.mkdir(parents=True)
        for value in ("off", "0", "no", "false", "", "maybe"):
            path.write_text(f"PM_OS_TEST_GATE={value}\n", encoding="utf-8")
            self.run_local("win32")
            self.assertFalse((self.dest / "private-one").exists(), value)
        path.write_bytes("﻿PM_OS_TEST_GATE=\"on\"\r\n".encode("utf-8"))
        self.assertEqual(self.run_local("win32")[0], 0)
        self.assertTrue((self.dest / "private-one" / "SKILL.md").exists())

    def test_host_name_is_used_up_to_its_first_dot_in_upper_case(self) -> None:
        self.gate_on("THISBOX")
        with mock.patch.dict(installer.os.environ, {"PM_OS_MACHINE": "thisbox.local"}):
            self.assertEqual(installer.machine_setting("PM_OS_TEST_GATE"), "on")

    def test_the_environment_wins_over_the_file(self) -> None:
        self.gate_on()
        with mock.patch.dict(installer.os.environ, {"PM_OS_TEST_GATE": "off"}):
            self.assertEqual(installer.machine_setting("PM_OS_TEST_GATE"), "off")

    def test_open_gate_installs_copies_byte_for_byte_and_never_a_link(self) -> None:
        self.gate_on()
        src = self.source("private-one")
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        copy = self.dest / "private-one"
        self.assertFalse(copy.is_symlink())
        self.assertEqual((copy / "scripts" / "tool.py").read_bytes(), b"print('x')\r\n")
        self.assertEqual(installer.tree_hash(copy), installer.tree_hash(src))
        self.assertEqual(installer.read_marker(copy)["source_kind"], "local")
        code, out = self.run_local("win32", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("= private-one: in sync", out)

    def test_an_identical_unmarked_folder_is_adopted_with_no_file_touched(self) -> None:
        # Wrong impl: replacing the folder, which rewrites every file and loses local caches.
        self.gate_on()
        src = self.source("private-one")
        copy = self.dest / "private-one"
        shutil.copytree(src, copy)
        cache = copy / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "tool.pyc").write_bytes(b"cached")
        stamp = (copy / "SKILL.md").stat().st_mtime_ns
        code, out = self.run_local("win32", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("would adopt", out)
        self.assertIsNone(installer.read_marker(copy))
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        self.assertIn("✓ private-one: adopted", out)
        self.assertEqual((copy / "SKILL.md").stat().st_mtime_ns, stamp)
        self.assertEqual((cache / "tool.pyc").read_bytes(), b"cached")
        self.assertEqual(self.run_local("win32", "--check")[0], 0)

    def test_a_different_unmarked_folder_is_still_refused(self) -> None:
        self.gate_on()
        self.source("private-one")
        copy = self.dest / "private-one"
        copy.mkdir(parents=True)
        (copy / "SKILL.md").write_text("an edit that exists only here\n", encoding="utf-8")
        code, out = self.run_local("win32")
        self.assertEqual(code, 1, out)
        self.assertIn("was not installed by this script", out)
        self.assertEqual((copy / "SKILL.md").read_text(encoding="utf-8"), "an edit that exists only here\n")
        self.assertIsNone(installer.read_marker(copy))

    def test_an_explicit_name_beats_the_private_pattern(self) -> None:
        self.gate_on()
        self.source("present", body="private twin\n")
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        self.assertIn(".agents", installer.read_marker(self.dest / "present")["source"])

    def test_a_dormant_family_in_the_library_keeps_its_copies_too(self) -> None:
        # The same hole, on the existing absent_ok_on pattern.
        copy = self.managed_copy("optional-one", kind="agents")
        manifest = self.manifest(OPTIONAL_PATTERN)
        code, out = self.run_installer("win32", "--prune", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertTrue(copy.exists(), out)


class RealManifest(Base):
    """The shipped manifest, without any profile layer, on an otherwise empty home."""

    def setUp(self) -> None:
        super().setUp()
        self.real = self.config / "claude-skills-manifest.yaml"
        shutil.copy2(HERE.parent / "config" / "claude-skills-manifest.yaml", self.real)
        # Stays away from the real local/ on this machine, whatever its gate says.
        env = mock.patch.dict(installer.os.environ, {"PM_OS_PERSONAL_SKILLS": "off"})
        env.start()
        self.addCleanup(env.stop)

    def provide(self, name: str) -> None:
        skill = self.home / ".agents" / "skills" / name
        skill.mkdir(exist_ok=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    def test_plain_run_on_windows_needs_only_the_required_library_skill(self) -> None:
        self.provide("todo")
        code, out = self.run_installer("win32", "--dry-run", manifest=self.real)
        self.assertEqual(code, 0, out)
        self.assertIn("· pattern firecrawl-*: dormant", out)
        self.assertIn("· firecrawl: dormant", out)

    def test_plain_run_on_windows_fails_when_a_required_skill_is_gone(self) -> None:
        code, out = self.run_installer("win32", "--dry-run", manifest=self.real)
        self.assertEqual(code, 1, out)
        self.assertIn("✗ todo: source has no SKILL.md", out)

    def test_plain_run_on_mac_still_requires_firecrawl(self) -> None:
        self.provide("todo")
        code, out = self.run_installer("darwin", "--dry-run", manifest=self.real)
        self.assertEqual(code, 1, out)
        self.assertIn("pattern firecrawl-*: no skill matches", out)

    def test_only_the_known_entries_are_exempt_and_only_on_windows(self) -> None:
        data = installer.yaml.safe_load(self.real.read_text(encoding="utf-8"))
        exempt = {e.get("name") or e["pattern"]: e["absent_ok_on"] for e in data["skills"] if "absent_ok_on" in e}
        self.assertEqual(set(exempt), {"firecrawl", "firecrawl-*"})
        self.assertTrue(all(platforms == ["win32"] for platforms in exempt.values()), exempt)

    def test_only_on_entry_does_not_exist_on_another_platform(self) -> None:
        # An only_on: [win32] skill stays off macOS. Wrong impl:
        # an entry without only_on fails or installs on macOS because the source is or is not there.
        manifest = self.manifest("  - {name: local-only, source: agents, only_on: [win32]}")
        code, out = self.run_installer("darwin", "--dry-run", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertNotIn("local-only", out)
        self.assertNotIn("local-only", self.run_installer("darwin", "--check", manifest=manifest)[1])
        code, out = self.run_installer("win32", "--dry-run", manifest=manifest)
        self.assertEqual(code, 1, "on its own platform a missing source still fails")
        self.assertIn("local-only", out)

    def test_the_tracked_manifest_names_no_private_skill(self) -> None:
        # The name of a private skill stays out of git and out of the kit.
        data = installer.yaml.safe_load(self.real.read_text(encoding="utf-8"))
        local = [e for e in data["skills"] if e.get("source") == "local"]
        self.assertEqual(local, [])


class Protection(LocalBase):
    """Skills system design steps 1, 2, 6 and 7."""

    def setUp(self) -> None:
        super().setUp()
        self.gate_on()
        installer.NOTICES.clear()
        self.addCleanup(installer.NOTICES.clear)
        self.edits = self.repo / "local" / "skill-edits"

    def installed(self, name: str, body: str = "body\n") -> Path:
        self.source(name, body)
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        return self.dest / name

    def test_an_edited_copy_is_saved_before_the_source_replaces_it(self) -> None:
        # Wrong impl: the old installer rmtree'd the edited copy and the edit was gone.
        copy = self.installed("edited-one")
        (copy / "SKILL.md").write_text("---\nname: edited-one\n---\nhand edit\n", encoding="utf-8")
        (self.repo / "local" / "skills" / "edited-one" / "SKILL.md").write_text(
            "---\nname: edited-one\n---\nsource v2\n", encoding="utf-8")
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        self.assertIn("edited copy saved to", out)
        self.assertIn("source v2", (copy / "SKILL.md").read_text(encoding="utf-8"))
        saved = list(self.edits.glob("edited-one-*"))
        self.assertEqual(len(saved), 1)
        self.assertIn("hand edit", (saved[0] / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual([n["kind"] for n in installer.NOTICES], ["edit-backup"])

    def test_an_edit_with_an_unchanged_source_is_also_restored_and_saved(self) -> None:
        copy = self.installed("edited-two")
        (copy / "extra.md").write_text("added by hand\n", encoding="utf-8")
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        self.assertFalse((copy / "extra.md").exists())
        self.assertTrue(any((p / "extra.md").exists() for p in self.edits.glob("edited-two-*")))

    def test_an_unedited_refresh_saves_nothing(self) -> None:
        self.installed("plain-one")
        (self.repo / "local" / "skills" / "plain-one" / "SKILL.md").write_text("---\nname: plain-one\n---\nv2\n", encoding="utf-8")
        self.assertEqual(self.run_local("win32")[0], 0)
        self.assertFalse(self.edits.exists())

    def test_prune_saves_an_edited_copy_before_removing_it(self) -> None:
        copy = self.installed("going")
        (copy / "SKILL.md").write_text("---\nname: going\n---\nedited\n", encoding="utf-8")
        shutil.rmtree(self.repo / "local" / "skills" / "going")
        self.source("staying")
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(copy.exists())
        self.assertTrue(any("edited" in (p / "SKILL.md").read_text(encoding="utf-8") for p in self.edits.glob("going-*")))

    def test_prune_refuses_more_than_five_at_once_and_deletes_nothing(self) -> None:
        names = [f"many-{i}" for i in range(6)]
        for name in names:
            self.source(name)
        self.source("keeper")
        self.assertEqual(self.run_local("win32")[0], 0)
        for name in names:
            shutil.rmtree(self.repo / "local" / "skills" / name)
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 1, out)
        self.assertIn("prune refused: 6 copies", out)
        self.assertTrue(all((self.dest / n).exists() for n in names))
        code, out = self.run_local("win32", "--prune", "--allow-mass-prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(any((self.dest / n).exists() for n in names))

    def test_five_removals_still_go(self) -> None:
        names = [f"few-{i}" for i in range(5)]
        for name in names:
            self.source(name)
        self.source("keeper")
        self.assertEqual(self.run_local("win32")[0], 0)
        for name in names:
            shutil.rmtree(self.repo / "local" / "skills" / name)
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(any((self.dest / n).exists() for n in names))

    def test_a_locked_rename_fails_that_skill_only_and_keeps_the_old_copy(self) -> None:
        # Wrong impl: rmtree first, then a locked file stops the copy and leaves half a folder.
        copy = self.installed("locked-one")
        self.source("other-one")
        (self.repo / "local" / "skills" / "locked-one" / "SKILL.md").write_text(
            "---\nname: locked-one\n---\nv2\n", encoding="utf-8")
        real_replace = installer.os.replace

        def flaky(src, dst):
            if Path(dst) == copy or Path(src) == copy:
                raise PermissionError("file in use")
            return real_replace(src, dst)

        with mock.patch.object(installer.os, "replace", side_effect=flaky):
            code, out = self.run_local("win32")
        self.assertEqual(code, 1, out)
        self.assertIn("✗ locked-one: could not replace", out)
        self.assertIn("✓ other-one: installed", out)
        self.assertIn("body", (copy / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(list((self.dest.parent / ".skills-staging").glob("locked-one*")), [])

    def test_a_failed_second_rename_puts_the_old_copy_back(self) -> None:
        copy = self.installed("swap-one")
        (self.repo / "local" / "skills" / "swap-one" / "SKILL.md").write_text("---\nname: swap-one\n---\nv2\n", encoding="utf-8")
        real_replace = installer.os.replace

        def second_fails(src, dst):
            if str(src).endswith(".new") and Path(dst) == copy:
                raise PermissionError("antivirus holds it")
            return real_replace(src, dst)

        with mock.patch.object(installer.os, "replace", side_effect=second_fails):
            code, out = self.run_local("win32")
        self.assertEqual(code, 1, out)
        self.assertIn("body", (copy / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(installer.read_marker(copy))

    def test_a_worktree_run_against_the_real_home_is_refused(self) -> None:
        # Wrong impl: a worktree's older skills replace main's and prune newer ones.
        (self.repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.source("wt-one")
        with mock.patch.object(installer.Path, "home", return_value=self.home):
            code, out = self.run_local("win32")
            self.assertEqual(code, 2, out)
            self.assertIn("refusing to install from a task worktree", out)
            self.assertFalse((self.dest / "wt-one").exists())
            self.assertEqual(self.run_local("win32", "--dry-run")[0], 0, "a dry run writes nothing, so it may run")
            self.assertEqual(self.run_local("win32", "--allow-worktree")[0], 2, "--allow-worktree needs --only")
            code, out = self.run_local("win32", "--only", "wt-one", "--allow-worktree")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.dest / "wt-one" / "SKILL.md").exists())

    def test_the_main_checkout_is_not_refused(self) -> None:
        (self.repo / ".git").mkdir()
        self.source("main-one")
        with mock.patch.object(installer.Path, "home", return_value=self.home):
            code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)

    def test_a_destination_inside_a_source_root_is_refused(self) -> None:
        manifest = self.config / "overlap.yaml"
        manifest.write_text(LOCAL_MANIFEST.replace("destination: ~/.claude/skills", "destination: ~/.agents/skills/out"),
                            encoding="utf-8")
        code, out = self.run_installer("win32", manifest=manifest)
        self.assertEqual(code, 2, out)
        self.assertIn("overlaps the agents source root", out)

    @unittest.skipUnless(hasattr(installer.Path, "is_junction") and installer.os.name == "nt", "Windows junctions")
    def test_a_junction_at_the_destination_is_unlinked_and_its_source_survives(self) -> None:
        import _winapi
        src = self.source("linked-one")
        self.dest.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(src), str(self.dest / "linked-one"))
        code, out = self.run_local("win32", "--force")
        self.assertEqual(code, 0, out)
        self.assertTrue((src / "SKILL.md").exists(), "the source folder must survive")
        self.assertTrue((src / "scripts" / "tool.py").exists())
        self.assertFalse((self.dest / "linked-one").is_junction())
        self.assertTrue((self.dest / "linked-one" / installer.MARKER).exists())

    @unittest.skipUnless(hasattr(installer.Path, "is_junction") and installer.os.name == "nt", "Windows junctions")
    def test_prune_unlinks_a_marked_junction_without_touching_its_target(self) -> None:
        import _winapi
        target = self.home / "elsewhere" / "pruned-link"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: pruned-link\n---\n", encoding="utf-8")
        (target / installer.MARKER).write_text(installer.json.dumps({"source_kind": "agents", "hash": "x"}), encoding="utf-8")
        self.source("keeper")
        self.assertEqual(self.run_local("win32")[0], 0)
        _winapi.CreateJunction(str(target), str(self.dest / "pruned-link"))
        code, out = self.run_local("win32", "--prune")
        self.assertEqual(code, 0, out)
        self.assertFalse(installer.os.path.lexists(self.dest / "pruned-link"))
        self.assertTrue((target / "SKILL.md").exists())

    def test_check_fails_when_the_folder_and_frontmatter_names_differ(self) -> None:
        # Claude reads the folder name, Amp reads the frontmatter name.
        skill = self.source("folder-name")
        (skill / "SKILL.md").write_text("---\nname: other-name\ndescription: x\n---\n", encoding="utf-8")
        self.assertEqual(self.run_local("win32")[0], 0)
        code, out = self.run_local("win32", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("✗ folder-name: folder name differs from its frontmatter name 'other-name'", out)

    def test_unmanaged_folders_are_listed_and_noticed_but_synced_and_wmux_are_not(self) -> None:
        self.source("keeper")
        for name in ("dropped-in", "synced", "wmux"):
            (self.dest / name).mkdir(parents=True)
            (self.dest / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        notices = self.config / "notices.json"
        code, out = self.run_local("win32", "--prune", "--notices-json", str(notices))
        self.assertEqual(code, 0, out)
        found = installer.json.loads(notices.read_text(encoding="utf-8"))
        self.assertEqual([(n["kind"], n["name"]) for n in found], [("unmanaged", "dropped-in")])
        code, out = self.run_local("win32", "--check")
        self.assertEqual(code, 0, "an unmanaged folder is news, not drift")
        self.assertIn("? dropped-in: unmanaged folder", out)
        self.assertNotIn("synced", out)
        self.assertNotIn("wmux:", out)
        self.assertTrue((self.dest / "dropped-in").exists() and (self.dest / "synced").exists())

    def test_settings_are_written_only_when_the_listing_changes(self) -> None:
        self.source("keeper")
        state = self.config / "settings.hash"
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"skillOverrides": {"keeper": "off"}}\n', encoding="utf-8")
        code, out = self.run_local("win32", "--settings-state", str(state))
        self.assertEqual(code, 0, out)
        self.assertNotIn("keeper", settings.read_text(encoding="utf-8"), "first run applies the manifest")
        settings.write_text('{"skillOverrides": {"keeper": "name-only"}}\n', encoding="utf-8")
        code, out = self.run_local("win32", "--settings-state", str(state))
        self.assertIn("settings.json left alone", out)
        self.assertIn("name-only", settings.read_text(encoding="utf-8"), "the owner's own change survives a tick")

    def test_a_source_kind_run_touches_only_that_source(self) -> None:
        self.source("private-one")
        code, out = self.run_local("win32", "--source-kind", "agents", "--prune")
        self.assertEqual(code, 0, out)
        self.assertIn("✓ present: installed", out)
        self.assertNotIn("private-one", out)


class PatternsKeepMachineRules(Base):
    def test_a_pattern_cannot_bring_back_a_skill_only_on_leaves_out(self) -> None:
        for name in ("mine-only", "shared-one"):
            skill = self.home / ".agents" / "skills" / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        manifest = self.manifest("  - {name: mine-only, source: agents, only_on: [win32]}",
                                 '  - {pattern: "*", source: agents}')
        code, out = self.run_installer("darwin", "--dry-run", manifest=manifest)
        self.assertEqual(code, 0, out)
        self.assertIn("shared-one", out)
        self.assertNotIn("mine-only", out)
        code, out = self.run_installer("win32", "--dry-run", manifest=manifest)
        self.assertIn("mine-only", out)


class RealSkills(unittest.TestCase):
    def test_every_repo_skill_folder_matches_its_frontmatter_name(self) -> None:
        roots = [HERE.parents[1] / ".agents" / "skills", HERE.parent.parent / "vendor" / "skills"]
        wrong = [f"{p.name} != {installer.frontmatter_name(p)!r}" for root in roots if root.is_dir()
                 for p in sorted(root.iterdir()) if (p / "SKILL.md").exists() and installer.frontmatter_name(p) != p.name]
        self.assertEqual(wrong, [])

    def test_the_manifest_installs_repo_skills_through_one_pattern(self) -> None:
        data = installer.yaml.safe_load((HERE.parent / "config" / "claude-skills-manifest.yaml").read_text(encoding="utf-8"))
        patterns = [e for e in data["skills"] if e.get("source") == "pm-os" and "pattern" in e]
        self.assertEqual(patterns, [{"pattern": "*", "source": "pm-os"}])
        named = [e for e in data["skills"] if e.get("source") == "pm-os" and "name" in e]
        self.assertTrue(all(e.get("only_on") or e.get("absent_ok_on") for e in named),
                        "a named pm-os entry exists only to carry a per-machine rule")


class ReviewFixes(LocalBase):
    """Findings from a code review of the sync."""

    def setUp(self) -> None:
        super().setUp()
        self.gate_on()
        installer.NOTICES.clear()
        self.addCleanup(installer.NOTICES.clear)

    def test_a_git_folder_in_a_source_never_reads_as_a_hand_edit(self) -> None:
        # Wrong impl: .git counted in the hash but skipped by the copy, so every run saved a backup.
        src = self.source("cloned-one")
        (src / ".git").mkdir()
        (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.assertEqual(self.run_local("win32")[0], 0)
        code, out = self.run_local("win32")
        self.assertIn("= cloned-one: up to date", out)
        self.assertFalse((self.repo / "local" / "skill-edits").exists())

    def test_the_marker_records_the_copy_even_when_the_source_moves_mid_copy(self) -> None:
        src = self.source("moving-one")
        real_copytree = installer.shutil.copytree

        def copy_then_change(*args, **kw):
            result = real_copytree(*args, **kw)
            if Path(args[0]) == src:  # the top-level call; copytree recurses through the patched name
                (src / "SKILL.md").write_text("---\nname: moving-one\n---\nchanged after the copy\n", encoding="utf-8")
            return result

        with mock.patch.object(installer.shutil, "copytree", side_effect=copy_then_change):
            self.assertEqual(self.run_local("win32")[0], 0)
        code, out = self.run_local("win32")
        self.assertIn("✓ moving-one: installed", out)
        self.assertNotIn("edited copy saved", out, "a source change is not a hand edit")

    def test_a_pattern_never_takes_over_synced_or_wmux(self) -> None:
        for name in ("wmux", "synced", "keeper"):
            self.source(name)
        code, out = self.run_local("win32")
        self.assertEqual(code, 0, out)
        self.assertNotIn("wmux", out)
        self.assertNotIn("synced", out)
        self.assertFalse((self.dest / "wmux").exists())

    def test_a_named_entry_for_a_foreign_folder_is_refused(self) -> None:
        manifest = self.config / "foreign.yaml"
        manifest.write_text(LOCAL_MANIFEST + "  - {name: wmux, source: agents}\n", encoding="utf-8")
        code, out = self.run_installer("win32", manifest=manifest)
        self.assertEqual(code, 1)
        self.assertIn("another installer owns: wmux", out)

    def test_a_worktree_cannot_reach_the_real_home_through_an_absolute_destination(self) -> None:
        (self.repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.source("wt-two")
        real = self.home.parent / "realhome"
        (real / ".claude" / "skills").mkdir(parents=True)
        with mock.patch.object(installer.Path, "home", return_value=real):
            code, out = self.run_local("win32", "--destination", str(real / ".claude" / "skills"))
        self.assertEqual(code, 2, out)
        self.assertFalse((real / ".claude" / "skills" / "wt-two").exists())

    def test_codex_mode_may_write_into_the_library_root_for_pm_os_only(self) -> None:
        manifest = self.config / "codex.yaml"
        manifest.write_text(LOCAL_MANIFEST.replace("  local: local/skills", "  local: local/skills\n  pm-os: local/skills"),
                            encoding="utf-8")
        dest = str(self.home / ".agents" / "skills")
        code, out = self.run_installer("win32", "--destination", dest, manifest=manifest)
        self.assertEqual(code, 2, "every source kind: the agents root overlaps")
        code, out = self.run_installer("win32", "--destination", dest, "--source-kind", "local", manifest=manifest)
        self.assertNotIn("overlaps", out)


class MacKitV8RetriedSwap(unittest.TestCase):
    """A skill folder another process holds open on Windows."""

    def test_swap_retries_a_held_folder_on_windows(self) -> None:
        calls: list[int] = []
        real = installer.os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("held by another process")
            return real(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "a", Path(tmp) / "b"
            src.write_text("x")
            with mock.patch.object(installer.os, "name", "nt"), \
                 mock.patch.object(installer.os, "replace", side_effect=flaky), mock.patch("time.sleep"):
                installer._os_replace(src, dst)
            self.assertEqual(dst.read_text(), "x")
        self.assertEqual(len(calls), 2)

    def test_swap_gives_up_after_five_seconds(self) -> None:
        with mock.patch.object(installer.os, "name", "nt"), \
             mock.patch.object(installer.os, "replace", side_effect=PermissionError("held")) as replaced, \
             mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                installer._os_replace("a", "b")
        self.assertEqual(replaced.call_count, 50)


if __name__ == "__main__":
    unittest.main()
