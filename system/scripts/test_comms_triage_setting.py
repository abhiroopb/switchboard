#!/usr/bin/env python3
"""The per-machine Comms Triage switch, and the rule that owner context stays out of the kit.

Every case runs against a throwaway local folder, so the real local/ never decides a
result. No case reaches a live tab: launches go to a stub, or return before any mux call.

  python3 system/scripts/test_comms_triage_setting.py
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
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import comms_triage_setting as setting  # noqa: E402


def find_bash() -> str | None:
    """Git Bash or a real Unix bash; WSL's System32 launcher cannot read these Windows paths."""
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
POWERSHELL = (shutil.which("powershell") or shutil.which("pwsh")) if os.name == "nt" else None
MACHINE = "TESTBOX"


def clean_env(state_home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != setting.KEY}
    return {**env, "PM_OS_LOCAL_DIR": str(state_home), "PM_OS_MACHINE": MACHINE, "PM_OS_STATE_HOME": str(state_home),
            "PM_OS_TEST_SANDBOX": "1", **extra}


def setting_env(home: Path, **extra: str) -> dict[str, str]:
    return {"PM_OS_LOCAL_DIR": str(home), "PM_OS_MACHINE": MACHINE, **extra}


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env_file = self.home / "machines" / f"{MACHINE}.env"

    def write_env(self, text: str) -> None:
        self.env_file.parent.mkdir(exist_ok=True)
        self.env_file.write_bytes(text.encode("utf-8"))


class SettingTests(Base):
    def env(self, **extra: str) -> dict[str, str]:
        return setting_env(self.home, **extra)

    def test_default_is_on_with_no_file_or_key(self) -> None:
        self.assertTrue(setting.enabled(self.env()))
        self.write_env("OTHER_KEY=x\n")
        self.assertTrue(setting.enabled(self.env()))

    def test_off_values_read_from_the_env_file(self) -> None:
        for value in ("off", "OFF", "0", "false", "no", "disabled", '"off"', "off\r"):
            with self.subTest(value=value):
                self.write_env(f"OTHER_KEY=x\n{setting.KEY}={value}\n")
                self.assertFalse(setting.enabled(self.env()))

    def test_another_machines_file_changes_nothing(self) -> None:
        # Dropbox syncs local/, so a second device sees this machine's file and must stay on.
        self.write_env(f"{setting.KEY}=off\n")
        self.assertFalse(setting.enabled(self.env()))
        self.assertTrue(setting.enabled(self.env(PM_OS_MACHINE="example-MacBook.local")))

    def test_machine_name_ignores_case_and_domain(self) -> None:
        self.write_env(f"{setting.KEY}=off\n")
        for name in ("testbox", "TestBox.local", "TESTBOX.example.com"):
            with self.subTest(name=name):
                self.assertFalse(setting.enabled(self.env(PM_OS_MACHINE=name)))

    def test_local_folder_follows_the_main_checkout(self) -> None:
        self.assertEqual(setting.local_dir({"PM_OS_ROOT": str(self.home)}), self.home / "local")
        worktree = self.home / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {(self.home / 'main' / '.git' / 'worktrees' / 'x').as_posix()}\n", encoding="utf-8")
        with mock.patch.object(setting, "REPO", worktree):
            self.assertEqual(setting.local_dir({}), self.home / "main" / "local", "a worktree writes to main's folder")

    def test_byte_order_mark_and_non_utf8_files(self) -> None:
        self.env_file.parent.mkdir(exist_ok=True)
        self.env_file.write_bytes(f"﻿{setting.KEY}=off\n".encode("utf-8"))
        self.assertFalse(setting.enabled(self.env()), "Set-Content -Encoding utf8 adds a mark")
        self.env_file.write_bytes(f"{setting.KEY}=off\n".encode("utf-16"))
        setting.write_setting("off", self.env())
        self.assertEqual(self.env_file.read_bytes().replace(b"\r\n", b"\n"), f"{setting.KEY}=off\n".encode("utf-8"), "--set repairs it")

    def test_process_environment_wins_over_the_file(self) -> None:
        self.write_env(f"{setting.KEY}=off\n")
        self.assertTrue(setting.enabled(self.env(**{setting.KEY: "on"})))
        self.env_file.unlink()
        self.assertFalse(setting.enabled(self.env(**{setting.KEY: "off"})))

    def test_set_keeps_other_keys_and_round_trips(self) -> None:
        self.write_env("OTHER_KEY=x\n")
        env = self.env()
        setting.write_setting("off", env)
        self.assertFalse(setting.enabled(env))
        setting.write_setting("off", env)
        self.assertEqual(self.env_file.read_text(encoding="utf-8").count(setting.KEY), 1)
        setting.write_setting("on", env)
        self.assertTrue(setting.enabled(env))
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "OTHER_KEY=x\n")
        self.env_file.unlink()
        setting.write_setting("off", env)
        setting.write_setting("on", env)
        self.assertFalse(self.env_file.exists(), "an empty file is removed")


@unittest.skipUnless(BASH, "no usable bash")
class BashTests(Base):
    def run_bash(self, script: str, cwd: Path = REPO, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                              cwd=cwd, env=clean_env(self.home, **extra), timeout=120)

    def lib_says(self, **extra: str) -> int:
        return self.run_bash("source system/scripts/lib/comms-triage.sh; pm_os_comms_triage_enabled", **extra).returncode

    def test_bash_twin_matches_python(self) -> None:
        self.assertEqual(self.lib_says(), 0, "no file means on")
        for value, expected in (("off", 1), ("OFF\r", 1), ('"off"', 1), ("disabled", 1), ("on", 0), ("", 0)):
            with self.subTest(value=value):
                self.write_env(f"OTHER_KEY=x\n{setting.KEY}={value}\n")
                self.assertEqual(self.lib_says(), expected)
                self.assertEqual(setting.enabled(setting_env(self.home)), expected == 0)
        self.write_env(f"{setting.KEY}=off\n")
        self.assertEqual(self.lib_says(**{setting.KEY: "on"}), 0, "the environment wins")
        self.assertEqual(self.lib_says(PM_OS_MACHINE="example-MacBook.local"), 0, "another machine stays on")
        self.assertEqual(self.lib_says(PM_OS_MACHINE="testbox.local"), 1, "case and domain do not matter")

    def test_bash_and_python_agree_on_the_real_host_name(self) -> None:
        real = setting.env_file({"PM_OS_LOCAL_DIR": str(self.home)})
        real.parent.mkdir(exist_ok=True)
        real.write_text(f"{setting.KEY}=off\n", encoding="utf-8")
        env = clean_env(self.home)
        env.pop("PM_OS_MACHINE")
        done = subprocess.run([BASH, "-c", "source system/scripts/lib/comms-triage.sh; pm_os_comms_triage_enabled"],
                              capture_output=True, text=True, cwd=REPO, env=env, timeout=120)
        self.assertEqual(done.returncode, 1, f"bash did not read {real}")

    def test_bash_default_root_mark_and_comment(self) -> None:
        root = self.home / "root"
        (root / "local" / "machines").mkdir(parents=True)
        (root / "local" / "machines" / f"{MACHINE}.env").write_text(f"{setting.KEY}=off\n", encoding="utf-8")
        self.assertEqual(self.lib_says(PM_OS_LOCAL_DIR="", PM_OS_ROOT=root.as_posix()), 1, "no override: PM_OS_ROOT/local")
        self.env_file.parent.mkdir(exist_ok=True)
        self.env_file.write_bytes(f"﻿{setting.KEY}=off\n".encode("utf-8"))
        self.assertEqual(self.lib_says(), 1, "a byte order mark still reads off, as in Python and PowerShell")
        self.write_env(f"# {setting.KEY}=off\n")
        self.assertEqual(self.lib_says(), 0, "a comment is not a setting")

    @unittest.skipUnless(os.name == "nt", "pm-os-env.sh runs only on Windows")
    def test_old_home_key_is_not_exported(self) -> None:
        # The environment beats the machine file, so a copy left in ~/.pm-os/env must never reach it.
        old = self.home / "old-env"
        old.write_text(f"{setting.KEY}=off\nPM_OS_TEST_OTHER=kept\n", encoding="utf-8")
        done = self.run_bash('source system/windows/pm-os-env.sh 2>/dev/null; echo "[${PM_OS_COMMS_TRIAGE:-unset}] [${PM_OS_TEST_OTHER:-unset}]"',
                             PM_OS_ENV_FILE=old.as_posix(), PM_OS_ROOT=REPO.as_posix())
        self.assertIn("[unset] [kept]", done.stdout)

    def launcher_fixture(self) -> Path:
        """ensure-routine-workspaces.sh over a stub cmux-helpers.sh that records each launch."""
        root = self.home / "repo"
        (root / "system" / "scripts" / "lib").mkdir(parents=True)
        shutil.copy(SCRIPTS / "ensure-routine-workspaces.sh", root / "system" / "scripts")
        shutil.copy(SCRIPTS / "lib" / "comms-triage.sh", root / "system" / "scripts" / "lib")
        for name in ("launchpad", "todo", "comms-triage", "meetings", "scheduled-jobs"):
            (root / "routines" / name).mkdir(parents=True)
            (root / "routines" / name / "CONTEXT.md").write_text("x\n", encoding="utf-8")
        (root / "system" / "cmux-helpers.sh").write_text(
            'source "$REPO_ROOT/system/scripts/lib/comms-triage.sh"\n'
            "log_info() { :; }\nlog_warn() { :; }\n"
            "find_workspace_ref_for_source_key() { return 1; }\n"
            'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then echo "$2" >> "$REPO_ROOT/launches.txt"; fi\n',
            encoding="utf-8", newline="\n")
        return root

    def launches(self, root: Path, *routines: str) -> list[str]:
        log = root / "launches.txt"
        log.unlink(missing_ok=True)
        done = self.run_bash(f"bash system/scripts/ensure-routine-workspaces.sh {' '.join(routines)}",
                             cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def test_launcher_skips_comms_triage_only_when_off(self) -> None:
        root = self.launcher_fixture()
        everything = ["routine:todo", "routine:launchpad", "routine:comms-triage", "routine:scheduled-jobs", "routine:meetings"]
        self.assertEqual(self.launches(root), everything, "no key: macOS path is unchanged")
        self.write_env(f"{setting.KEY}=off\n")
        self.assertEqual(self.launches(root), [r for r in everything if "comms" not in r])
        self.assertEqual(self.launches(root, "todo", "comms-triage"), ["routine:todo"], "named explicitly, still skipped")

    def test_cmux_helpers_launch_refuses_when_off(self) -> None:
        self.write_env(f"{setting.KEY}=off\n")
        done = self.run_bash("bash system/cmux-helpers.sh launch routine:comms-triage", REPO_ROOT=REPO.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("off on this machine", done.stdout + done.stderr)

    def test_guards_are_wired(self) -> None:
        # The sidebar and start-day paths need a live mux to run, so these check the wiring.
        helpers = (REPO / "system" / "cmux-helpers.sh").read_text(encoding="utf-8")
        pins = re.search(r'if ! pm_os_comms_triage_enabled; then\n\s+top_pinned=\((.*?)\)', helpers)
        self.assertIsNotNone(pins, "arrange_workspace_sidebar has no off branch")
        self.assertNotIn("comms-triage", pins.group(1))
        self.assertIn('"routine:comms-triage"', helpers, "the default pin list keeps it for other machines")
        start_day = (REPO / "system" / "start-day.sh").read_text(encoding="utf-8")
        body = start_day[start_day.index("refresh_comms_review_state() {"):start_day.index("post_start_day_review() {")]
        self.assertLess(body.index("pm_os_comms_triage_enabled"), body.index("collect-comms-candidates.py"))
        loop = (SCRIPTS / "command-center-loop.sh").read_text(encoding="utf-8")
        body = loop[loop.index("post_comms_review() {"):loop.index("run_delivery_sweep() {")]
        self.assertLess(body.index("pm_os_comms_triage_enabled"), body.index('bash "$post_script"'))


class DigestTests(Base):
    def setUp(self) -> None:
        super().setUp()
        for key in (setting.KEY, "PM_OS_LOCAL_DIR", "PM_OS_MACHINE"):
            self.addCleanup(os.environ.__setitem__, key, os.environ[key]) if key in os.environ else self.addCleanup(os.environ.pop, key, None)
            os.environ.pop(key, None)
        os.environ.update(setting_env(self.home))

    def test_queue_has_no_comms_card_when_off(self) -> None:
        megadesk = load_module("rebuild-megadesk-state.py", "rebuild_megadesk_for_test")
        megadesk.build_comms_routine_card = lambda *a: {"workspace": "comms-triage"}
        megadesk.build_meetings_routine_card = lambda *a: {"workspace": "meetings"}
        derived = {"comms": {"updated_at": "c"}, "meetings": {"updated_at": "m"}}
        cards = lambda: [card["workspace"] for card, _ in megadesk.routine_cards_for(REPO, derived, 0)]
        self.assertEqual(cards(), ["comms-triage", "meetings"], "no key: macOS queue is unchanged")
        self.write_env(f"{setting.KEY}=off\n")
        self.assertEqual(cards(), ["meetings"])

    def test_command_center_drops_held_drafts_when_off(self) -> None:
        digest = load_module("command-center-digest.py", "command_center_for_test")
        ledger = self.home / "triage-ledger.json"
        ledger.write_text(json.dumps({"items": {"a": {"draft_status": "held", "ref": "https://example.com/a",
                                                     "draft_held_at": "2026-11-01T00:00:00Z"}}}), encoding="utf-8")
        digest.TRIAGE_LEDGER_FILE = ledger
        self.assertIsNotNone(digest.held_draft_row([], []), "no key: held drafts still surface")
        self.write_env(f"{setting.KEY}=off\n")
        self.assertIsNone(digest.held_draft_row([], []))

    def test_doctor_stops_asking_for_slack_when_off(self) -> None:
        doctor = load_module("check-windows-install.py", "check_windows_install_for_test")
        status = type("Proc", (), {"stdout": "gmail: ready\nslack: not configured (run: pmos login slack)\n"})()
        doctor.shutil.which = lambda name: "pmos"
        doctor.run = lambda *a, **k: status

        def slack_level() -> str:
            doctor.results.clear()
            doctor.check_connectors()
            return next(row["level"] for row in doctor.results if row["check"] == "connector slack")

        self.assertEqual(slack_level(), "owner", "no key: a missing Slack sign-in still reaches the owner")
        self.write_env(f"{setting.KEY}=off\n")
        self.assertEqual(slack_level(), "ok")

    def test_next_up_drops_pr_comments_line_when_off(self) -> None:
        """Nothing scans authored PRs where Comms Triage is off, so the line could
        only read "monitor stale/degraded"."""
        digest = load_module("next-up-digest.py", "next_up_digest_pr_line_for_test")
        import github_pr_reviews
        import slack_backoff
        stubs = [
            mock.patch.object(github_pr_reviews, "digest_line", lambda: "PR comments: STUB"),
            mock.patch.object(slack_backoff, "digest_line", lambda: ""),
            mock.patch.object(digest, "plan_freshness_line", lambda: "", create=True),
            mock.patch.object(digest, "running_task_sections", lambda: ([], [])),
            mock.patch.object(digest, "meeting_ingest_items", lambda surface: []),
        ]
        for stub in stubs:
            stub.start()
            self.addCleanup(stub.stop)

        def render(surface: str) -> str:
            with mock.patch.object(sys, "argv", ["next-up-digest.py", "--surface", surface, "--json"]), \
                    mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out:
                self.assertEqual(digest.main(), 0)
            return json.loads(out.getvalue())["message"]

        self.assertIn("PR comments: STUB", render("next-up"), "no key: macOS digest keeps the line")
        self.write_env(f"{setting.KEY}=off\n")
        for surface in ("next-up", "start-day"):
            self.assertNotIn("PR comments", render(surface), surface)

    def test_next_up_comms_surface_is_empty_when_off(self) -> None:
        self.write_env(f"{setting.KEY}=off\n")
        env = clean_env(self.home, PYTHONIOENCODING="utf-8")
        done = subprocess.run([sys.executable, str(SCRIPTS / "next-up-digest.py"), "--surface", "comms-triage", "--json"],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"message": ""})


@unittest.skipUnless(POWERSHELL, "PowerShell only on Windows")
class SeedSessionTests(Base):
    def pinned(self, **extra: str) -> str:
        session = self.home / "session.json"
        rows = [{"id": str(i), "title": title} for i, title in enumerate(("🧭 CoS", "📨 Comms Triage"))]
        session.write_text(json.dumps({"version": 1, "windows": [{"id": "w", "workspaces": rows}]}), encoding="utf-8")
        env = clean_env(self.home, USERPROFILE=str(self.home), **extra)
        done = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               f"& '{REPO / 'system' / 'windows' / 'seed-session.ps1'}' -DryRun -NoPrefs "
                               f"-SessionPath '{session}' -SettingsPath '{self.home / 'settings.json'}' -Sidecar ''"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        line = next((ln for ln in done.stdout.splitlines() if "pinned ->" in ln), "")
        self.assertTrue(line, done.stdout)
        return line

    def test_comms_triage_pin_follows_the_setting(self) -> None:
        self.assertIn("Comms Triage", self.pinned(), "no file: still pinned")
        self.write_env(f"{setting.KEY}=off\n")
        line = self.pinned()
        self.assertIn("CoS", line)
        self.assertNotIn("Comms Triage", line)
        self.assertIn("Comms Triage", self.pinned(PM_OS_MACHINE="example-MacBook.local"), "another machine keeps the pin")

    def test_powershell_agrees_on_the_real_host_name(self) -> None:
        real = setting.env_file({"PM_OS_LOCAL_DIR": str(self.home)})
        real.parent.mkdir(exist_ok=True)
        real.write_text(f"{setting.KEY}=off\n", encoding="utf-8")
        self.assertNotIn("Comms Triage", self.pinned(PM_OS_MACHINE=""))

    def test_powershell_default_root(self) -> None:
        root = self.home / "root"
        (root / "local" / "machines").mkdir(parents=True)
        (root / "local" / "machines" / f"{MACHINE}.env").write_text(f"{setting.KEY}=off\n", encoding="utf-8")
        self.assertNotIn("Comms Triage", self.pinned(PM_OS_LOCAL_DIR="", PM_OS_ROOT=str(root)))


class OwnerContextTests(Base):
    def test_role_details_stay_out_of_kit_policy(self) -> None:
        # Built in two pieces so this test file does not carry the phrase itself.
        employer = "at " + "Example" + "corp"
        for rel in ("AGENTS.md", "adapters/SYSTEM.md", "CLAUDE.md"):
            text = (REPO / rel).read_text(encoding="utf-8")
            core = text.split("<!-- pm-os:profile-section:start")[0]
            role = core[core.index("## Role"):].split("\n## ")[0] if "## Role" in core else ""
            self.assertNotIn(employer, role, rel)
            self.assertNotIn("Add your employer", role, rel)

    def test_local_folder_never_reaches_git_or_the_kit(self) -> None:
        # .git/info/exclude also lists /local on this machine, so check-ignore alone proves nothing about .gitignore.
        rules = (REPO / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("/local", rules, "a worktree's link to local/ is not a directory to git")
        self.assertIn("/local/", rules)
        # A Mac worktree's local is a symlink, and git refuses to look through one.
        below = () if (REPO / "local").is_symlink() else ("local/agents-local.md", "local/user-lens.yaml", "local/machines/ANY.env")
        for rel in ("local", *below):
            with self.subTest(rel=rel):
                ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO)
                self.assertEqual(ignored.returncode, 0, f"{rel} is not gitignored")
        tracked = subprocess.run(["git", "ls-files", "--", "local", "*agents-local*", "*user-lens.yaml"], cwd=REPO,
                                 capture_output=True, text=True)
        self.assertEqual(tracked.stdout.strip(), "")
        if (REPO / ".pm-os-kit" / "install.json").is_file():  # written by the installer, absent in a kit tree
            manifest = json.loads((REPO / ".pm-os-kit" / "install.json").read_text(encoding="utf-8"))
            self.assertEqual([f for f in manifest["files"] if f.startswith("local/")], [])
        if (REPO / "docs" / "kit-port").is_dir():  # the kit-port tools stay out of the kit
            self.assertIn('"local/"', (REPO / "docs" / "kit-port" / "build-kit-delta.py").read_text(encoding="utf-8"))
        self.assertRegex((SCRIPTS / "dispatch.py").read_text(encoding="utf-8"), r'SENSITIVE_PREFIXES = \(.*"local"')

    def test_lens_skill_ships_with_no_owner_content(self) -> None:
        # AGENTS.md requires this skill, and AGENTS.md ships in the kit.
        self.assertIn("checking-owner-lens", (REPO / "AGENTS.md").read_text(encoding="utf-8"))
        text = (REPO / ".agents" / "skills" / "checking-owner-lens" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("local/user-lens.yaml", text)
        self.assertNotIn("Example" + "corp", text)
        self.assertTrue((REPO / "templates" / "user-lens.yaml.example").exists())
        # The manifest installs every repo skill through one pattern (skills system design, 2026-11-09),
        # so the lens ships unless a named entry limits it to some platforms.
        manifest = (REPO / "system" / "config" / "claude-skills-manifest.yaml").read_text(encoding="utf-8")
        self.assertIn('{pattern: "*", source: pm-os}', manifest)
        self.assertNotRegex(manifest, r"name: checking-owner-lens[^}\n]*only_on")

    @unittest.skipUnless(BASH, "no usable bash")
    def test_adapters_load_the_local_file_only_when_it_exists(self) -> None:
        fake = self.home / "home"
        fake.mkdir()
        root = self.home / "root"
        root.mkdir()
        (root / "AGENTS.md").write_text("policy\n", encoding="utf-8")

        def install() -> tuple[str, str, str]:
            env = {k: v for k, v in os.environ.items()}
            env["PM_OS_PYTHON"] = Path(sys.executable).as_posix()
            done = subprocess.run([BASH, (REPO / "adapters" / "install-adapters.sh").as_posix(), "--apply", "--no-skills",
                                   "--home", fake.as_posix(), "--root", root.as_posix()],
                                  capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("skills: skipped", done.stdout)
            read = lambda rel: (fake / rel).read_text(encoding="utf-8")
            return read(".claude/CLAUDE.md"), read("AGENTS.md"), read(".codex/AGENTS.md")

        claude, agents, codex = install()
        for text in (claude, agents, codex):
            self.assertNotIn("agents-local.md", text, "a machine with no file is unchanged")
        self.assertFalse((fake / ".claude" / "skills").exists(), "--no-skills copies nothing")

        (root / "local").mkdir()
        (root / "local" / "agents-local.md").write_text("role\n", encoding="utf-8")
        claude, agents, codex = install()
        self.assertRegex(claude, r"(?m)^@.*/local/agents-local\.md$")
        for text in (claude, agents, codex):
            self.assertIn("/local/agents-local.md", text)
            self.assertNotIn(".pm-os/agents-local", text, "the old home path is gone")
        self.assertEqual(claude.count("agents-local.md"), 1, "rerun replaces the block")


if __name__ == "__main__":
    unittest.main()
