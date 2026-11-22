#!/usr/bin/env python3
"""Pins rule 9 of the Claude guardrail hook: no agent runs Amp in a way that starts its device sign-in.

Incident: a skills test ran `amp skill list` five times with HOME and
USERPROFILE set to a temp folder. Amp found no stored key there, started its sign-in, and opened tabs in
the owner's browser. The hook now refuses amp next to a home override (in the command or in a script file
the command runs), `amp login` and `amp logout`, and, on Windows, any amp call while Amp's key file is
missing.

Runs the real hook the way the harness does, with stdin JSON. Never runs amp. Script files live in a
temp folder and every path is invented.

Run: python3 system/scripts/test_amp_auth_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
HOOK = ROOT / ".claude" / "hooks" / "pm-os-guardrails.py"


def decide(command: str, tool: str = "Bash", cwd: str = "C:/invented", env: dict | None = None) -> tuple[str | None, str]:
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd}
    out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                         text=True, check=True, env=env).stdout
    if not out.strip():
        return None, ""
    spec = json.loads(out)["hookSpecificOutput"]
    return spec["permissionDecision"], spec["permissionDecisionReason"]


def signed_in_env(folder: Path) -> dict:
    """A hook environment whose Amp key file exists, so only the override rule is under test."""
    data = folder / "data"
    (data / "amp").mkdir(parents=True, exist_ok=True)
    (data / "amp" / "secrets.json").write_text("{}", encoding="utf-8")
    return dict(os.environ, XDG_DATA_HOME=str(data))


class AmpHomeOverride(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = signed_in_env(self.dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def kind(self, command: str, tool: str = "Bash", cwd: str | None = None) -> str | None:
        return decide(command, tool, cwd or str(self.dir), self.env)[0]

    def test_overrides_in_the_command_are_denied(self) -> None:
        for command in (
            'HOME=/tmp/fake USERPROFILE=C:/tmp/fake amp skill list',
            'env USERPROFILE="$(cygpath -w "$H")" HOME="$H" HOMEPATH="" timeout 60 ~/.amp/bin/amp skill list',
            'XDG_DATA_HOME=/tmp/x ~/.amp/bin/amp.exe threads list',
            'APPDATA=C:/tmp/a amp --version',
            'env -u HOME amp skill list',
            "python3 -c \"import os,subprocess; subprocess.run(['amp','skill','list'], env=dict(os.environ, HOME='x'))\"",
            "cat > t.sh <<'EOF'\nH=\"$1\"; env USERPROFILE=\"$H\" HOME=\"$H\" amp skill list\nEOF\nbash t.sh /tmp/h",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kind(command), "deny")

    def test_powershell_overrides_are_denied(self) -> None:
        for command in (
            '$env:USERPROFILE = "C:\\tmp\\fake"; amp skill list',
            '$env:HOME="C:\\tmp\\fake"; & "$HOME\\.amp\\bin\\amp.exe" threads list',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kind(command, tool="PowerShell"), "deny")

    def test_a_script_file_that_overrides_home_is_denied(self) -> None:
        sub = self.dir / "skilltest"
        sub.mkdir()
        (sub / "ampl.py").write_text(
            "import os, subprocess\nfrom pathlib import Path\n"
            "AMP = str(Path.home() / '.amp/bin/amp.exe')\n"
            "env = dict(os.environ, USERPROFILE='C:/tmp/home1', HOME='C:/tmp/home1')\n"
            "subprocess.run([AMP, 'skill', 'list'], env=env)\n",
            encoding="utf-8",
        )
        (sub / "env_dict.py").write_text(
            "import os, subprocess\nenv = os.environ.copy()\nenv['XDG_DATA_HOME'] = '/tmp/x'\n"
            "subprocess.run(['amp', 'threads', 'list'], env=env)\n",
            encoding="utf-8",
        )
        self.assertEqual(self.kind(f'cd "{sub.as_posix()}" && python3 ampl.py 2>&1 | cut -c1-170'), "deny")
        self.assertEqual(self.kind(f'python3 "{(sub / "env_dict.py").as_posix()}"'), "deny")
        reason = decide(f'python3 "{(sub / "ampl.py").as_posix()}"', "Bash", str(self.dir), self.env)[1]
        self.assertIn("ampl.py runs", reason)
        self.assertIn("browser", reason)

    def test_sign_in_commands_are_denied(self) -> None:
        for command in ("amp login", "~/.amp/bin/amp.exe logout", "amp.exe login --help"):
            with self.subTest(command=command):
                self.assertEqual(self.kind(command), "deny")

    def test_ordinary_amp_and_home_use_is_allowed(self) -> None:
        clean = self.dir / "clean.py"
        clean.write_text("import subprocess\nsubprocess.run(['amp', 'skill', 'list'])\n", encoding="utf-8")
        for command in (
            "amp skill list",
            "cd \"$LOCALAPPDATA/Temp/proj\" && timeout 60 ~/.amp/bin/amp skill list",
            "REAL=$HOME; echo $REAL",
            "HOME=/tmp/x python3 other.py",
            "PM_OS_STATE_HOME=/tmp/s bash system/scripts/report.sh",
            "ls .amp/in && grep -n USERPROFILE= notes.md",
            f'python3 "{clean.as_posix()}"',
            'git commit -m "docs: amp login opens a browser"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.kind(command))

    def test_scripts_that_only_mention_both_are_allowed(self) -> None:
        # Shapes from tracked scripts that a whole-file match refused: a HOME default, prose about
        # "Amp", a key map's "home", and a test env set far from any amp call.
        far = self.dir / "far.py"
        far.write_text("env = {**os.environ, 'HOME': tmp}\n" + "pass\n" * 40
                       + "run(['amp', 'threads', 'continue', 'T-1'])\n", encoding="utf-8")
        default = self.dir / "notify.sh"
        default.write_text('export HOME="${HOME:-C:/Users/invented}"\n# chrome instead of a turn in its Amp session\n'
                           'amp threads list\n', encoding="utf-8")
        keys = self.dir / "keys.py"
        keys.write_text('KEYS = {"home": "home", "end": "end"}\nCMD = "amp threads continue T-1"\n', encoding="utf-8")
        for script in (far, default, keys):
            with self.subTest(script=script.name):
                self.assertIsNone(self.kind(f'python3 "{script.as_posix()}"' if script.suffix == ".py"
                                            else f'bash "{script.as_posix()}"'))

    def test_the_refusal_says_what_to_do_instead(self) -> None:
        reason = decide("HOME=/tmp/x amp skill list", "Bash", str(self.dir), self.env)[1]
        self.assertIn("temp project folder", reason)
        self.assertIn("ask-cos.sh", reason)


@unittest.skipUnless(os.name == "nt", "Amp keeps its key in a file only on Windows; elsewhere the check is off")
class AmpSignedOut(unittest.TestCase):
    def test_any_amp_call_is_denied_while_the_key_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_DATA_HOME=str(Path(tmp) / "empty"))
            kind, reason = decide("amp skill list", "Bash", tmp, env)
            self.assertEqual(kind, "deny")
            self.assertIn("signed out", reason)
            self.assertIsNone(decide("git status", "Bash", tmp, env)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
