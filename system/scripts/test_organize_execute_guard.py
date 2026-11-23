#!/usr/bin/env python3
"""Pins the rule that an agent can't run the file organizer outside an approved plan.

Runs the real Claude Code hook the way the harness does, with stdin JSON. Fixtures are invented,
and tests stay out of the real skill: every script here is a temp file with made-up contents.

Run: python3 system/scripts/test_organize_execute_guard.py
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
HOOK = ROOT / ".claude" / "hooks" / "switchboard-guardrails.py"
SETTINGS = ROOT / ".claude" / "settings.json"
CURRENT = "PLAIN_EXECUTE_REFUSAL = 'invented'\n"
OLD = 'execute_flag = "--execute" in sys.argv\n'


class OrganizeExecuteGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "organize.py").write_text(CURRENT, encoding="utf-8")

    def decide(self, command: str, tool: str = "Bash") -> str | None:
        payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(self.dir)}
        out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None

    def old_copy(self) -> None:
        (self.dir / "organize.py").write_text(OLD, encoding="utf-8")

    def test_plain_execute_is_denied_in_every_shell_form(self) -> None:
        for command in (
            "python organize.py --execute",
            "python3 ./organize.py --verbose --execute",
            "FOO=1 python3 organize.py --execute",
            'bash -c "python3 organize.py --execute"',
            'S=organize.py; python3 "$S" --execute',
            "python3 organize.py \\\n  --execute",
            "python3 organize.py --execute --manifest-out plan.json",
            'python3 "organize.py" --execute',
            '"C:/Program Files/Invented Python/python.exe" organize.py --execute',
            "pythonw organize.py --execute",
            "timeout 60 python organize.py --execute",
            "nohup python organize.py --execute",
            "( python organize.py --execute )",
            "if true; then python organize.py --execute; fi",
            "$P organize.py --execute",
            "python organize_old.py --execute",
            "python organize.py.bak --execute",
            "python other.py --execute --manifest x.json; python organize.py --execute",
            "python organize.py $FLAGS",
            'python -c "import subprocess; subprocess.run([\'python\',\'organize.py\',\'--execute\'])"',
        ):
            self.assertEqual(self.decide(command), "deny", command)

    def test_powershell_tool_is_covered(self) -> None:
        self.assertEqual(self.decide('& python "organize.py" --execute', "PowerShell"), "deny")
        for command in (
            "& { python organize.py --execute }",
            "$out = python organize.py --execute",
            "Start-Process python -ArgumentList 'organize.py --execute'",
            "Invoke-Expression 'python organize.py --execute'",
            "iex \"python organize.py --execute\"",
            "pwsh -Command 'python organize.py --execute'",
            "cmd /c python organize.py --execute",
        ):
            self.assertEqual(self.decide(command, "PowerShell"), "deny", command)
        self.assertIsNone(self.decide('& python "organize.py" --execute --manifest plan.json', "PowerShell"))

    def test_powershell_tool_only_gets_this_rule(self) -> None:
        self.assertIsNone(self.decide("git add -A", "PowerShell"))

    def test_settings_route_powershell_to_the_hook(self) -> None:
        matcher = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["matcher"]
        self.assertIn("Bash", matcher.split("|"))
        self.assertIn("PowerShell", matcher.split("|"))

    def test_execute_with_a_plan_passes_on_a_current_copy(self) -> None:
        self.assertIsNone(self.decide("python3 organize.py --execute --manifest plan.json"))
        self.assertIsNone(self.decide("python3 organize.py --manifest=plan.json --execute"))

    def test_past_year_filing_gets_no_exemption(self) -> None:
        self.assertEqual(self.decide("python3 organize.py --archive-past-years --execute"), "deny")
        self.assertIsNone(self.decide("python3 organize.py --archive-past-years --manifest-out plan.json"))

    def test_absolute_path_is_the_copy_that_gets_checked(self) -> None:
        self.old_copy()
        current_elsewhere = self.dir / "elsewhere"
        current_elsewhere.mkdir()
        (current_elsewhere / "organize.py").write_text(CURRENT, encoding="utf-8")
        self.assertIsNone(self.decide(f'python3 "{current_elsewhere / "organize.py"}" --execute --manifest plan.json'))
        self.assertEqual(self.decide(f'python3 "{self.dir / "organize.py"}" --execute --manifest plan.json'), "deny")

    def test_old_restored_copy_is_denied_even_with_a_plan(self) -> None:
        self.old_copy()
        self.assertEqual(self.decide("python3 organize.py --execute --manifest plan.json"), "deny")

    def test_old_copy_under_another_name_is_denied(self) -> None:
        (self.dir / "renamed_tool.py").write_text(OLD, encoding="utf-8")
        self.assertEqual(self.decide("python3 renamed_tool.py --execute --manifest plan.json"), "deny")

    def test_a_cd_in_the_same_command_is_followed(self) -> None:
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "organize.py").write_text(OLD, encoding="utf-8")
        self.assertEqual(self.decide("cd sub && python3 organize.py --execute --manifest plan.json"), "deny")

    def test_copy_that_cannot_be_read_is_denied(self) -> None:
        self.assertEqual(self.decide("python3 missing/organize.py --execute --manifest plan.json"), "deny")

    def test_calling_the_old_function_directly_is_denied(self) -> None:
        self.assertEqual(self.decide('python3 -c "import organize; organize.scan_and_organize(execute=True)"'), "deny")
        self.assertEqual(self.decide('python3 -c "import organize; organize.scan_and_organize(True)"'), "deny")
        self.assertEqual(self.decide("python3 <<EOF\nimport organize\norganize.scan_and_organize(execute=True)\nEOF"), "deny")

    def test_no_run_of_an_old_copy_is_allowed(self) -> None:
        self.old_copy()
        for command in ("python3 organize.py", "python3 organize.py --manifest-out plan.json", "python3 organize.py --check-manifest plan.json", "python3 organize.py --undo"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), "deny")

    def test_an_old_copy_under_another_name_or_as_a_module_is_refused(self) -> None:
        (self.dir / "keep-this.py").write_text(OLD, encoding="utf-8")
        self.assertEqual(self.decide("python3 keep-this.py"), "deny")
        self.old_copy()
        self.assertEqual(self.decide("python3 -m organize"), "deny")

    def test_the_module_form_the_escaped_flag_and_pushd_are_seen(self) -> None:
        self.assertEqual(self.decide("python3 -m organize --execute"), "deny")
        self.assertEqual(self.decide("python3 organize.py --ex\\ecute"), "deny")
        elsewhere = self.dir / "other"
        elsewhere.mkdir()
        (elsewhere / "organize.py").write_text(OLD, encoding="utf-8")
        self.assertEqual(self.decide(f'pushd "{elsewhere}" && python3 organize.py'), "deny")
        self.assertEqual(self.decide(f'Push-Location "{elsewhere}"; python organize.py', "PowerShell"), "deny")

    def test_previews_and_mentions_pass(self) -> None:
        for command in (
            "python3 organize.py",
            "python3 organize.py --manifest-out plan.json",
            "python3 organize.py --check-manifest plan.json",
            "python3 organize.py --undo",
            "python3 -m organize_helper --list",
            'grep -n "organize.py --execute" notes.md',
            "bash report.sh 'plain organize.py --execute now refuses'",
            "python3 other_tool.py --execute",
            'git commit -m "guard; python organize.py --execute is refused"',
            'git commit -m "first line\n\npython organize.py --execute is refused"',
            'grep "a && python organize.py --execute" notes.md',
            'python other.py "plain organize.py --execute now refuses"',
            "python other.py --execute; cat organize.py",
            "cat <<EOF\npython organize.py --execute\nEOF",
            "cat organize.py",
        ):
            self.assertIsNone(self.decide(command), command)

    def test_the_guard_files_that_quote_the_old_line_are_not_copies(self) -> None:
        # rt-00000000004f: every run that named the hook file was refused as an old organize.py copy,
        # because the hook quotes the old line in a string. Only that line as code marks a copy.
        hook = str(HOOK)
        for command in (
            f'python3 "{hook}" --self-test',
            f'python3 edit_hook.py "{hook}"',
            f'python3 -c "print(1)" "{hook}"',
            f'python3 "{ROOT / "system" / "scripts" / "test_organize_execute_guard.py"}"',
            f'python3 other.py --execute "{hook}"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command), command)
        (self.dir / "guard.py").write_text(f"MARKER = {OLD.strip()!r}\nOLD = {OLD!r}\n", encoding="utf-8")
        self.assertIsNone(self.decide("python3 guard.py --execute --manifest plan.json"))
        (self.dir / "indented.py").write_text(f"def main():\n    {OLD}", encoding="utf-8")
        self.assertEqual(self.decide("python3 indented.py"), "deny")


if __name__ == "__main__":
    unittest.main()
