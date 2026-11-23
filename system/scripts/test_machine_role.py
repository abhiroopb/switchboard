#!/usr/bin/env python3
"""The per-machine role: a personal machine opens CoS and To Do by default and loads
Launchpad, Meetings, and Scheduled Jobs on demand; a work machine (or no role) opens all.

Every case runs against a throwaway local folder and a stub multiplexer, so the real
local/ never decides a result and no case reaches a live tab.

  python3 system/scripts/test_machine_role.py
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

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import machine_role as role  # noqa: E402

MACHINE = "TESTBOX"
TODAY = "2026-11-12"
KEYS = (role.ROLE_KEY, role.KEEP_KEY, role.LOADED_KEY, "SWITCHBOARD_COMMS_TRIAGE")


def find_bash() -> str | None:
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
POWERSHELL = (shutil.which("powershell") or shutil.which("pwsh")) if os.name == "nt" else None


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env_file = self.home / "machines" / f"{MACHINE}.env"

    def env(self, **extra: str) -> dict[str, str]:
        return {"SWITCHBOARD_LOCAL_DIR": str(self.home), "SWITCHBOARD_MACHINE": MACHINE, **extra}

    def write_env(self, text: str) -> None:
        self.env_file.parent.mkdir(exist_ok=True)
        self.env_file.write_text(text, encoding="utf-8")

    def proc_env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k not in KEYS and k != "SWITCHBOARD_MUX"}
        return {**base, "SWITCHBOARD_LOCAL_DIR": self.home.as_posix(), "SWITCHBOARD_MACHINE": MACHINE,
                "SWITCHBOARD_TEST_SANDBOX": "1", "PYTHONIOENCODING": "utf-8", **extra}


class RoleTests(Base):
    def test_no_file_or_unknown_value_is_work(self) -> None:
        self.assertEqual(role.role(self.env()), "work")
        self.write_env("SWITCHBOARD_COMMS_TRIAGE=off\n")
        self.assertEqual(role.role(self.env()), "work")
        self.write_env(f"{role.ROLE_KEY}=laptop\n")
        self.assertEqual(role.role(self.env()), "work", "an unknown role never hides a surface")
        self.assertEqual(role.on_demand(self.env(), TODAY), [])

    def test_personal_opens_cos_and_todo_only(self) -> None:
        self.write_env(f'{role.ROLE_KEY}="Personal"\r\n')
        self.assertEqual(role.role(self.env()), "personal")
        self.assertEqual(role.on_demand(self.env(), TODAY), ["launchpad", "scheduled-jobs", "meetings"])
        self.assertEqual(role.default_surfaces(self.env(), TODAY), ["switchboard", "todo", "comms-triage"])
        self.write_env(f"{role.ROLE_KEY}=personal\nSWITCHBOARD_COMMS_TRIAGE=off\n")
        self.assertEqual(role.default_surfaces(self.env(), TODAY), ["switchboard", "todo"],
                         "Comms Triage keeps its own switch")
        for key in ("switchboard", "todo"):
            self.assertTrue(role.opens_by_default(key, self.env(), TODAY))

    def test_role_follows_the_machine_and_the_environment(self) -> None:
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        self.assertEqual(role.role(self.env(SWITCHBOARD_MACHINE="example-MacBook.local")), "work", "another machine's file")
        self.assertEqual(role.role(self.env(**{role.ROLE_KEY: "work"})), "work", "the environment wins")

    def test_loaded_today_counts_until_midnight_pacific(self) -> None:
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        role.mark_loaded("meetings", self.env(), TODAY)
        role.mark_loaded("launchpad", self.env(), TODAY)
        self.assertEqual(role.loaded_today(self.env(), TODAY), ["meetings", "launchpad"])
        self.assertEqual(role.on_demand(self.env(), TODAY), ["scheduled-jobs"])
        self.assertEqual(role.on_demand(self.env(), "2026-11-13"), ["launchpad", "scheduled-jobs", "meetings"])
        role.mark_loaded("scheduled-jobs", self.env(), "2026-11-13")
        self.assertEqual(role.loaded_today(self.env(), "2026-11-13"), ["scheduled-jobs"], "a new day starts a new list")
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"{role.ROLE_KEY}=personal", text, "other lines kept")

    def test_keep_and_unkeep(self) -> None:
        self.write_env(f"{role.ROLE_KEY}=personal\nOTHER=1\n")
        role.set_keep("launchpad", True, self.env())
        self.assertEqual(role.on_demand(self.env(), TODAY), ["scheduled-jobs", "meetings"])
        role.set_keep("launchpad", False, self.env())
        self.assertNotIn(role.KEEP_KEY, self.env_file.read_text(encoding="utf-8"))
        self.assertIn("OTHER=1", self.env_file.read_text(encoding="utf-8"))

    def test_set_work_removes_the_key(self) -> None:
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        role.write_key(role.ROLE_KEY, None, self.env())
        self.assertFalse(self.env_file.exists(), "an emptied file is removed")

    def test_pacific_date(self) -> None:
        import datetime as dt
        late = dt.datetime(2026, 11, 13, 6, 0, tzinfo=dt.timezone.utc)  # 10 PM PT on the 12th
        self.assertEqual(role.today_pt(late), "2026-11-12")

    def test_the_owners_words(self) -> None:
        cases = {
            "load Meetings": "meetings",
            "load Meetings Registry": "meetings",
            "load Scheduled Tasks": "scheduled-jobs",
            "load scheduled jobs": "scheduled-jobs",
            "Load Launchpad": "launchpad",
            "load launch pad and keep it": "launchpad",
            "open the To Do tab": "todo",
            "load CoS": "switchboard",
            "load widgets": None,
        }
        for words, want in cases.items():
            with self.subTest(words=words):
                self.assertEqual(role.resolve(words), want)


@unittest.skipUnless(BASH, "needs bash")
class BashTests(Base):
    def run_bash(self, script: str, cwd: Path | None = None, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], cwd=cwd or REPO, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.proc_env(**extra), timeout=120)

    def test_bash_twin_agrees(self) -> None:
        probe = ('source system/scripts/lib/machine-role.sh; switchboard_machine_role; '
                 'for s in switchboard todo launchpad meetings scheduled-jobs; do '
                 'switchboard_surface_opens_by_default $s && echo "$s=open" || echo "$s=wait"; done')
        done = self.run_bash(probe)
        self.assertIn("work", done.stdout)
        self.assertNotIn("=wait", done.stdout, "no file: the work path is unchanged")
        self.write_env(f"{role.ROLE_KEY}=personal\n{role.KEEP_KEY}=meetings\n")
        done = self.run_bash(probe)
        self.assertIn("personal", done.stdout)
        for want in ("switchboard=open", "todo=open", "launchpad=wait", "meetings=open", "scheduled-jobs=wait"):
            self.assertIn(want, done.stdout)

    def fixture(self) -> Path:
        """ensure-routine-workspaces.sh and load-surface.sh over a stub cmux-helpers.sh."""
        root = self.home / "repo"
        lib = root / "system" / "scripts" / "lib"
        lib.mkdir(parents=True)
        for name in ("ensure-routine-workspaces.sh", "load-surface.sh", "machine_role.py", "comms_triage_setting.py",
                     "local_backup.py"):
            shutil.copy(SCRIPTS / name, root / "system" / "scripts")
        for name in ("comms-triage.sh", "machine-role.sh"):
            shutil.copy(SCRIPTS / "lib" / name, lib)
        for name in ("switchboard", "launchpad", "todo", "comms-triage", "meetings", "scheduled-jobs"):
            (root / "routines" / name).mkdir(parents=True)
            (root / "routines" / name / "CONTEXT.md").write_text("x\n", encoding="utf-8")
        (root / "system" / "cmux-helpers.sh").write_text(
            'source "$REPO_ROOT/system/scripts/lib/comms-triage.sh"\n'
            "log_info() { :; }\nlog_warn() { :; }\n"
            'find_workspace_ref_for_source_key() { grep -q "^routine:$2|" "$REPO_ROOT/launches.txt" 2>/dev/null && echo "workspace:9"; }\n'
            'arrange_workspace_sidebar() { echo arranged >> "$REPO_ROOT/arranged.txt"; }\n'
            'if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then echo "$2|${CMUX_LAUNCH_HANDOFF:-}" >> "$REPO_ROOT/launches.txt"; fi\n',
            encoding="utf-8", newline="\n")
        return root

    def launches(self, root: Path) -> list[str]:
        log = root / "launches.txt"
        return [ln.split("|")[0] for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def ensure(self, root: Path, *routines: str) -> list[str]:
        (root / "launches.txt").unlink(missing_ok=True)
        done = self.run_bash(f"bash system/scripts/ensure-routine-workspaces.sh {' '.join(routines)}",
                             cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.launches(root)

    def test_launcher_follows_the_role(self) -> None:
        root = self.fixture()
        everything = ["routine:todo", "routine:launchpad", "routine:comms-triage", "routine:scheduled-jobs",
                      "routine:meetings"]
        self.assertEqual(self.ensure(root), everything, "no role: work, unchanged")
        self.write_env(f"{role.ROLE_KEY}=personal\nSWITCHBOARD_COMMS_TRIAGE=off\n")
        self.assertEqual(self.ensure(root), ["routine:todo"])
        self.assertEqual(self.ensure(root, "switchboard", "todo", "launchpad"), ["routine:switchboard", "routine:todo"],
                         "named by start of day or open-routines.ps1, still on demand")
        self.assertEqual(self.ensure(root, "scheduled-jobs", "meetings"), [], "named bottom routines still wait for load")
        self.write_env(f"{role.ROLE_KEY}=work\n")
        self.assertEqual(self.ensure(root), everything)

    def test_load_opens_records_and_hands_off(self) -> None:
        root = self.fixture()
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        done = self.run_bash("bash system/scripts/load-surface.sh load Scheduled Tasks", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((out["status"], out["surface"], out["ref"]), ("opened", "scheduled-jobs", "workspace:9"))
        self.assertIn("load Scheduled Jobs and keep it", out["message"])
        line = (root / "launches.txt").read_text(encoding="utf-8")
        self.assertIn("routine:scheduled-jobs|Loaded on demand", line)
        self.assertIn("catch up every job", line)
        self.assertTrue((root / "arranged.txt").exists(), "filed into the sidebar")
        today = role.today_pt()
        self.assertIn(f"{role.LOADED_KEY}={today}:scheduled-jobs", self.env_file.read_text(encoding="utf-8"))
        # A start of day later the same day keeps it; Launchpad and Meetings still wait.
        self.assertEqual(self.ensure(root), ["routine:todo", "routine:comms-triage", "routine:scheduled-jobs"])

        (root / "launches.txt").write_text("routine:scheduled-jobs|\n", encoding="utf-8")
        done = self.run_bash("bash system/scripts/load-surface.sh Scheduled Jobs", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1])["status"], "already-open")

    def test_load_and_keep(self) -> None:
        root = self.fixture()
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        done = self.run_bash('bash system/scripts/load-surface.sh "load Launchpad and keep it"', cwd=root,
                             REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("default set", json.loads(done.stdout.strip().splitlines()[-1])["message"])
        self.assertIn(f"{role.KEEP_KEY}=launchpad", self.env_file.read_text(encoding="utf-8"))
        self.assertTrue(role.opens_by_default("launchpad", self.env(), "2099-01-01"), "kept beyond today")

    def test_load_meetings_hands_off_todays_meetings(self) -> None:
        root = self.fixture()
        self.write_env(f"{role.ROLE_KEY}=personal\n")
        done = self.run_bash("bash system/scripts/load-surface.sh Meetings", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("load today's meetings first", (root / "launches.txt").read_text(encoding="utf-8"))

    def test_work_machine_load_records_nothing(self) -> None:
        root = self.fixture()
        done = self.run_bash("bash system/scripts/load-surface.sh Launchpad", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1])["status"], "opened")
        self.assertFalse(self.env_file.exists(), "a work machine gets no file")

    def test_refusals(self) -> None:
        root = self.fixture()
        self.write_env("SWITCHBOARD_COMMS_TRIAGE=off\n")
        done = self.run_bash("bash system/scripts/load-surface.sh Comms Triage", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 3)
        done = self.run_bash("bash system/scripts/load-surface.sh widgets", cwd=root, REPO_ROOT=root.as_posix())
        self.assertEqual(done.returncode, 2)
        self.assertFalse((root / "launches.txt").exists())

    def test_env_file_never_exports_the_role(self) -> None:
        old = self.home / "env"
        old.write_text(f"{role.ROLE_KEY}=personal\n{role.LOADED_KEY}=x\nSWITCHBOARD_TEST_OTHER=kept\n", encoding="utf-8")
        done = self.run_bash('source system/windows/switchboard-env.sh 2>/dev/null; '
                             'echo "[${SWITCHBOARD_MACHINE_ROLE:-unset}] [${SWITCHBOARD_LOADED_SURFACES:-unset}] [${SWITCHBOARD_TEST_OTHER:-unset}]"',
                             SWITCHBOARD_ENV_FILE=old.as_posix(), SWITCHBOARD_ROOT=REPO.as_posix())
        self.assertIn("[unset] [unset] [kept]", done.stdout)


@unittest.skipUnless(POWERSHELL, "PowerShell only on Windows")
class SeedSessionTests(Base):
    def seed(self) -> str:
        session = self.home / "session.json"
        rows = [{"id": "0", "title": "🧭 CoS"}, {"id": "1", "title": "✅ To Do"}]
        session.write_text(json.dumps({"version": 1, "windows": [{"id": "w", "workspaces": rows}]}), encoding="utf-8")
        done = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                               f"& '{REPO / 'system' / 'windows' / 'seed-session.ps1'}' -DryRun -NoPrefs "
                               f"-SessionPath '{session}' -SettingsPath '{self.home / 'settings.json'}' -Sidecar ''"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.proc_env(USERPROFILE=str(self.home), SWITCHBOARD_LOCAL_DIR=str(self.home)), timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_closed_launchpad_is_expected_on_a_personal_machine(self) -> None:
        self.write_env("SWITCHBOARD_COMMS_TRIAGE=off\n")
        self.assertIn("missing surfaces: Launchpad", self.seed(), "work: a closed Launchpad is reported")
        self.write_env(f"SWITCHBOARD_COMMS_TRIAGE=off\n{role.ROLE_KEY}=personal\n")
        self.assertNotIn("missing surfaces", self.seed())
        self.write_env(f"SWITCHBOARD_COMMS_TRIAGE=off\n{role.ROLE_KEY}=personal\n{role.KEEP_KEY}=launchpad\n")
        self.assertIn("missing surfaces: Launchpad", self.seed(), "kept: reported again")


class WiringTests(unittest.TestCase):
    def test_docs_and_skill_name_the_command(self) -> None:
        agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("load <surface>", agents)
        self.assertIn("load-surface.sh", agents)
        self.assertIn("SWITCHBOARD_MACHINE_ROLE", agents)
        skill = (REPO / ".agents" / "skills" / "chief-of-staff" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("load-surface.sh", skill)
        install = (REPO / "docs" / "install-windows.md").read_text(encoding="utf-8")
        self.assertIn("SWITCHBOARD_MACHINE_ROLE", install)


if __name__ == "__main__":
    unittest.main(verbosity=1)
