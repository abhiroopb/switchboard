#!/usr/bin/env python3
"""Pins the read guards. No agent prints secrets from its environment. Reading Downloads is allowed.

The Downloads read block is retired: agents read and search files in the Downloads folders, text and binary, with the
Read, Grep, and shell tools. Every shape the old block denied is kept below and must now pass, so a
half-removed block shows up here. The environment guard is unchanged.

Runs the real Claude Code hook the way the harness does, with stdin JSON. Every path is invented and no
file is opened, so these tests never touch the real Downloads folder.

Run: python3 system/scripts/test_sensitive_read_guard.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
HOOK = ROOT / ".claude" / "hooks" / "switchboard-guardrails.py"
SETTINGS = ROOT / ".claude" / "settings.json"
DL = "C:/Users/invented/Dropbox/Downloads"


def decide(tool: str, tool_input: dict, cwd: str = "C:/invented") -> tuple[str | None, str]:
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
    out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
    if not out.strip():
        return None, ""
    spec = json.loads(out)["hookSpecificOutput"]
    return spec["permissionDecision"], spec["permissionDecisionReason"]


def shell(command: str, tool: str = "Bash", cwd: str = "C:/invented") -> str | None:
    return decide(tool, {"command": command}, cwd)[0]


ROOT_DIR = "C:/Users/invented/Dropbox"
MAC_DL = "/Users/invented/Downloads"


class DownloadsReadsAllowed(unittest.TestCase):
    """Every shape the retired block denied now passes. All paths are invented and nothing is opened."""

    def test_opening_a_text_file_passes_in_every_shell_form(self) -> None:
        for command in (
            f'cat "{DL}/notes.txt"',
            f'head -5 {DL}/notes.txt',
            f'grep word {DL}/notes.csv',
            f'bash -c "cat {DL}/notes.txt"',
            f'D={DL}; cat "$D/notes.txt"',
            f'cp "{DL}/notes.txt" /tmp/x.txt',
            f'cat "{DL}/no extension at all"',
            f'cat {DL}/*',
            f'grep -r word {DL}',
            f'rg word {DL}/',
            f'for f in {DL}/*; do cat "$f"; done',
            f"python3 -c \"print(open(r'{DL}/notes.txt').read())\"",
            f"python3 - <<'EOF'\nprint(open('{DL}/notes.txt').read())\nEOF",
            "python3 -c \"from pathlib import Path; print((Path.home()/'Dropbox'/'Downloads'/'notes.txt').read_text())\"",
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command))

    def test_powershell_forms_pass(self) -> None:
        for command in (
            f'Get-Content "{DL}/New Invented Document.txt"',
            f'gc -LiteralPath "{DL}/notes.md"',
            f'$x = Get-Content "{DL}/notes.txt"',
            f'[IO.File]::ReadAllText("{DL}/notes.txt")',
            f'Get-ChildItem "{DL}" | Get-Content',
            f'Get-ChildItem "{DL}" -Filter *.txt | Select-String word',
            f'Select-String -Path "{DL}/*" -Pattern word',
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, "PowerShell"))

    def test_read_and_grep_tools_pass(self) -> None:
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/notes.txt"})[0])
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/no extension"})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": DL})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": f"{DL}/notes.txt"})[0])

    def test_a_relative_path_inside_downloads_passes(self) -> None:
        self.assertIsNone(shell("cat notes.txt", cwd=DL))
        self.assertIsNone(shell(f"cd {DL} && cat notes.txt"))
        self.assertIsNone(decide("Read", {"file_path": "notes.txt"}, cwd=DL)[0])

    def test_names_documents_and_mentions_are_allowed(self) -> None:
        for command in (
            f'ls -la {DL}',
            f'ls {DL} | grep -i pdf',
            f'stat "{DL}/notes.txt"',
            f'mv "{DL}/notes.txt" "{DL}/old.txt"',
            f'rm "{DL}/notes.txt"',
            f'python3 switchboard-trash.py "{DL}/notes.txt"',
            f'python3 organize.py --manifest-out {DL}/plan.json',
            f'pdftotext "{DL}/statement.pdf" -',
            f'cat "{DL}/2026-11-08-invented-preview.md"',
            f'git commit -m "stop opening {DL}/notes.txt"',
            f'echo "{DL}/notes.txt is off limits"',
            'grep "Dropbox/Downloads/notes.txt" guide.md',
            'cat /c/Users/invented/Downloads/notes.txt',
            'bash report-to-cos.sh "the file in Dropbox/Downloads/notes.txt was never opened"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command))
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/statement.pdf"})[0])
        self.assertIsNone(decide("Read", {"file_path": "C:/invented/notes.txt"})[0])
        self.assertIsNone(decide("Glob", {"pattern": "*.txt", "path": DL})[0])

    def test_rm_inside_powershell_is_allowed(self) -> None:
        self.assertIsNone(shell(f'Remove-Item -LiteralPath "{DL}/New Invented Document.txt"', "PowerShell"))
        self.assertIsNone(shell(f'Get-ChildItem "{DL}" | Select-Object Name, Length', "PowerShell"))

    def test_more_downloads_shapes_pass(self) -> None:
        for command, cwd in (
            (f'cd {ROOT_DIR} && cat Downloads/x.txt', "C:/invented"),
            ('cat ../Downloads/x.txt', f"{ROOT_DIR}/Money"),
            (f'grep -ri seed {ROOT_DIR}', "C:/invented"),
            (f'rg seed {ROOT_DIR}', "C:/invented"),
            ('rg seed', ROOT_DIR),
            (f'find {DL} -name "*.txt" -exec cat {{}} +', "C:/invented"),
            (f'find {DL} -name "*.txt" | xargs cat', "C:/invented"),
            (f'find {DL} -type f | while read f; do cat "$f"; done', "C:/invented"),
            (f'echo "$(cat {DL}/x.txt)"', "C:/invented"),
            (f'bash -l -c "cat {DL}/x.txt"', "C:/invented"),
            (f'mv {DL}/x.txt /tmp/ && cat /tmp/x.txt', "C:/invented"),
            (f'mv {DL}/x.txt /tmp/y.log; cat /tmp/y.log', "C:/invented"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, cwd=cwd))

    def test_a_grep_tool_above_downloads_passes(self) -> None:
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": ROOT_DIR})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": ROOT_DIR, "glob": "Downloads/**"})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x"}, cwd=DL)[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": ROOT_DIR, "glob": "*.pdf"})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": ROOT_DIR, "glob": "Development/**"})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": f"{ROOT_DIR}/Development"})[0])

    def test_ordinary_work_near_downloads_is_allowed(self) -> None:
        for command, cwd in (
            ("pip install pypdf", DL),
            ("code .", DL),
            ("pdftotext a.pdf out", DL),
            (f"tar -tf {DL}/a.tar.xz", "C:/invented"),
            (f'mv {DL}/x.txt {DL}/y.txt && ls {DL}', "C:/invented"),
            (f'rg seed {ROOT_DIR}/Development', "C:/invented"),
            (f'find {DL} -name "*.pdf"', "C:/invented"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, cwd=cwd))
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/../Money/x.txt"})[0])

    def test_a_file_named_for_a_credential_is_readable(self) -> None:
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/2026-11-08-seed-phrase-backup.md"})[0])
        self.assertIsNone(decide("Read", {"file_path": f"{DL}/2026-11-08-invented-preview.md"})[0])

    def test_names_sizes_and_reads_outside_downloads_are_allowed(self) -> None:
        """Shapes the guard once wrongly blocked, rebuilt with invented paths."""
        state = "C:/invented/repo/state/history.json"
        for command, cwd in (
            # ls -l on PDFs, with a "(1)" copy, next to a Python read of a JSON file elsewhere
            (f'ls -l "{DL}/Invented Statement.pdf" "{DL}/0000000001 (1).pdf"; '
             f"python3 -c \"import json; print(json.load(open('{state}'))['last'])\"", "C:/invented"),
            # find -newermt over Downloads, then a JSON read elsewhere
            (f"find {ROOT_DIR}/Money {DL} -maxdepth 4 -newermt '2026-11-06' -type f -iname '*stub*' | head -12; "
             f"python3 -c \"import json; d=json.load(open('{state}')); print(d)\"", "C:/invented"),
            (f'ls -lt --time-style="+%m-%d %H:%M" {DL} | sed -n 2,14p | cut -c30-110', "C:/invented"),
            # a sed script after cd into Downloads is not a file, and the later Python runs elsewhere
            (f"cd {DL} && ls -la --time-style=+%m-%d_%H:%M | sed -E 's/[0-9]{{3}}/<masked>/g'; "
             "cd C:/invented/repo && python3 -c \"import json; print(json.load(open('local/manifest.json')))\"", "C:/invented"),
            # a Python script reading a JSON log outside Downloads whose rows name Downloads paths
            (f"python3 - <<'EOF'\nimport json\nfor row in json.load(open('{state}')):\n"
             f"    if row['src'].startswith('{DL}/'):\n        print(row['src'].replace('{DL}/', ''))\nEOF", "C:/invented"),
            (f"python3 -c \"import json; rows=json.load(open('{state}')); print([r for r in rows if 'Dropbox/Downloads' in r['src']])\"",
             "C:/invented"),
            (f'H={state}; python3 - "$H" <<\'EOF\'\nimport json,sys\nprint(json.load(open(sys.argv[1])))\nEOF\n'
             f'ls -l "{DL}/Invented Paycheck.pdf"', "C:/invented"),
            (f"python3 C:/invented/check.py --log {state} --prefix {DL}", "C:/invented"),
            (f'echo "== history for stubs"; ls {DL} | head -5', "C:/invented"),
            (f"cat > C:/invented/probe.py <<'EOF'\nDL = r'{DL}'\nprint(open('{state}').read())\nEOF", "C:/invented"),
            (f"python3 -c \"print(open(r'{DL}/Invented Statement.pdf', 'rb').read(4))\"", "C:/invented"),
            (f"python3 -c \"import sys; print(open(sys.argv[1]).read())\" {state}; ls {DL}", "C:/invented"),
            (f"python3 -c \"with open('{state}') as f: print(f.read())\"; ls {DL}", "C:/invented"),
            (f'powershell -Command "Get-Content {state} -TotalCount 3"; ls {DL}', "C:/invented"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, cwd=cwd))
        self.assertIsNone(shell(f'Get-ChildItem "{DL}" | Select-Object Name, Length, LastWriteTime', "PowerShell"))

    def test_inline_code_reading_downloads_passes(self) -> None:
        for command, cwd in (
            (f"cd {DL} && python3 -c \"print(open('notes.txt').read())\"", "C:/invented"),
            ("python3 -c \"print(open('notes.txt').read())\"", DL),
            (f"python3 -c \"import os; os.chdir(r'{DL}'); print(open('notes.txt').read())\"", "C:/invented"),
            (f"python3 - <<'EOF'\nimport os\nD = r'{DL}'\nfor n in os.listdir(D):\n    print(open(os.path.join(D, n)).read())\nEOF",
             "C:/invented"),
            (f"python3 - <<'EOF'\nfrom pathlib import Path\nprint([p.read_text() for p in Path(r'{DL}').iterdir()])\nEOF", "C:/invented"),
            (f"python3 - <<'EOF'\nfrom pathlib import Path\nprint((Path.home() / 'Dropbox' / 'Downloads' / 'notes.txt').read_text())\nEOF",
             "C:/invented"),
            (f"node -e \"console.log(require('fs').readFileSync('{DL}/notes.txt', 'utf8'))\"", "C:/invented"),
            (f"python3 -c \"print(open(r'{DL}/notes (1).txt').read())\"", "C:/invented"),
            (f"sed -n 1,5p {DL}/notes.txt", "C:/invented"),
            (f"awk '{{print}}' {DL}/notes.csv", "C:/invented"),
            ("sed -n 1,5p notes.txt", DL),
            (f"ls {DL} | xargs cat", "C:/invented"),
            (f'find {DL} -newermt 2026-11-08 -exec head {{}} +', "C:/invented"),
            (f'for f in {DL}/*; do head "$f"; done', "C:/invented"),
            # shapes the code review found: anything the guard cannot trace fails closed
            (f"python3 - <<'EOF'\nimport os\ndef peek(path):\n    print(open(path).read())\nD = r'{DL}'\n"
             "for n in os.listdir(D):\n    peek(os.path.join(D, n))\nEOF", "C:/invented"),
            ("python3 - <<'EOF'\nimport os\nD = os.path.join(\n    os.path.expanduser('~'), 'Dropbox', 'Downloads')\n"
             "print(open(os.path.join(D, 'notes.txt')).read())\nEOF", "C:/invented"),
            (f"node -e \"const fs=require('fs'); const show=f=>console.log(fs.readFileSync(f,'utf8')); "
             f"fs.readdirSync('{DL}').forEach(n=>show('{DL}/'+n))\"", "C:/invented"),
            ("python3 -c \"import json; from pathlib import Path; print(json.load((Path.home()/'Dropbox'/'Downloads'/'wallet.json').open()))\"",
             "C:/invented"),
            ("ruby -e 'puts File.read(File.expand_path(\"~/Dropbox/Downloads/notes.txt\"))'", "C:/invented"),
            (f"python3 -c \"f=open(r'{DL}/notes.txt'); print(f.readline())\"", "C:/invented"),
            (f"python3 -c \"d=r'{DL}'; print(open(f'{{d}}/notes.txt').read())\"", "C:/invented"),
            ("powershell -NoProfile -Command \"$dl = Join-Path $env:USERPROFILE 'Dropbox\\Downloads'; "
             "Get-ChildItem $dl -Filter *.txt | ForEach-Object { Get-Content $_.FullName -TotalCount 5 }\"", "C:/invented"),
            (f"cd {DL} && python3 -u <<'EOF'\nprint(open('notes.txt').read())\nEOF", "C:/invented"),
            (f"sed --expression=1,20p -n {DL}/notes.txt", "C:/invented"),
            (f"sed -n -e1,20p {DL}/notes.txt", "C:/invented"),
            (f'for f in {DL}/*.txt; do\n  head "$f"\ndone', "C:/invented"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, cwd=cwd))
        for command in (f'[IO.File]::ReadAllText("{DL}/notes.txt")',
                        f'Get-ChildItem {DL}/*.txt | ForEach-Object {{ "== $($_.Name)"; Get-Content $_ -TotalCount 5 }}',
                        f'$d = "{DL}"; foreach ($f in Get-ChildItem $d) {{ [IO.File]::ReadAllText($f.FullName) }}'):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, "PowerShell"))
        self.assertIsNone(shell(f"for %f in ({DL.replace('/', chr(92))}\\*.txt) do type %f", "PowerShell"))

    def test_mac_downloads_and_override_free_reads_pass(self) -> None:
        for command in (f'cat "{MAC_DL}/notes.txt"', f'grep -r word {MAC_DL}', 'cat ~/Downloads/notes.txt',
                        'head -5 ~/Dropbox/Downloads/notes.txt', f'cat "{DL}/no extension at all"'):
            with self.subTest(command=command):
                self.assertIsNone(shell(command))
        self.assertIsNone(decide("Read", {"file_path": f"{MAC_DL}/notes.txt"})[0])
        self.assertIsNone(decide("Grep", {"pattern": "x", "path": MAC_DL})[0])

    def test_no_downloads_refusal_text_is_left(self) -> None:
        hook = HOOK.read_text(encoding="utf-8")
        mirror = (ROOT / "system" / "amp-plugins" / "switchboard" / "sensitive-reads.ts").read_text(encoding="utf-8")
        plugin = (ROOT / "system" / "amp-plugins" / "switchboard-guardrails.ts").read_text(encoding="utf-8")
        for text in (hook, mirror, plugin):
            self.assertNotIn("SWITCHBOARD_DOWNLOADS_TEXT_OK", text)
            self.assertNotIn("DOWNLOADS_MESSAGE", text)
        self.assertNotIn("downloadsTextViolation", plugin)
        self.assertNotIn("downloadsToolViolation", plugin)

    def test_the_hook_is_wired_to_the_shell_tools_only(self) -> None:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        matchers = "|".join(h["matcher"] for h in settings["hooks"]["PreToolUse"]).split("|")
        for tool in ("Bash", "PowerShell"):
            self.assertIn(tool, matchers)
        for tool in ("Read", "Grep"):
            self.assertNotIn(tool, matchers)


class EnvDump(unittest.TestCase):
    def test_a_dump_with_values_is_denied(self) -> None:
        for command in ("env", "printenv", "export -p", "declare -x", "set", "env | sort", "env | grep -i wmux", "true && env",
                        "printenv INVENTED_TOKEN", "echo $INVENTED_TOKEN", 'echo "got ${INVENTED_API_KEY}"',
                        "wmux token", "cat ~/AppData/Roaming/wmux/pipe-token",
                        "python3 -c 'import os; print(os.environ)'", "node -e 'console.log(process.env)'"):
            with self.subTest(command=command):
                self.assertEqual(shell(command), "deny")
        for command in ("Get-ChildItem Env:", "gci env:", "ls env:", "dir env:*", "Get-ChildItem Env: | Sort-Object Name",
                        "[Environment]::GetEnvironmentVariables()", "$env:INVENTED_TOKEN", 'Write-Output "$env:INVENTED_SECRET"',
                        "cmd /c set", "Get-Content $env:APPDATA\\wmux\\pipe-token"):
            with self.subTest(command=command):
                self.assertEqual(shell(command, "PowerShell"), "deny")

    def test_names_and_ordinary_uses_are_allowed(self) -> None:
        for command in ("env | cut -d= -f1", "env | cut -d= -f1 | grep -i wmux", "env | sed 's/=.*//'", "env FOO=1 python3 x.py",
                        "printenv HOME", "echo $HOME", "echo ${#INVENTED_TOKEN}", 'test -n "$INVENTED_TOKEN"', "set -e", "set -euo pipefail",
                        "export FOO=1", "wmux ping", "cat invented-token-notes.md", 'git commit -m "stop running env in tabs"'):
            with self.subTest(command=command):
                self.assertIsNone(shell(command))
        for command in ("(Get-ChildItem Env:).Name", "Get-ChildItem Env: | Select-Object Name", "gci env: | % Name", "$env:HOME"):
            with self.subTest(command=command):
                self.assertIsNone(shell(command, "PowerShell"))

    def test_the_refusal_says_how_to_list_names(self) -> None:
        self.assertIn("cut -d= -f1", decide("Bash", {"command": "env"})[1])

    def test_ordinary_commands_pass_the_environment_guard(self) -> None:
        for command in (
            'for token in a b; do echo "$token"; done',
            'echo "$MAX_TOKENS"',
            "echo $secret_dir",
            'git commit -m "guard: wmux token read refused"',
            'git commit -m "note; env; note"',
            'grep -n "wmux token" docs/x.md',
            'gh pr comment 1 --body "use echo $API_KEY in docs"',
            "env | grep -i proxy",
            "cat > t.py <<'EOF'\nprint(os.environ)\nEOF",
        ):
            with self.subTest(command=command):
                self.assertIsNone(shell(command))

    def test_more_environment_shapes_are_denied(self) -> None:
        for command in ("env > /tmp/e; cat /tmp/e", "env 2>/dev/null", "bash -c env", "env | grep .", "env | grep -i token",
                        """python3 -c "import os; print(os.environ['WMUX_PIPE_TOKEN'])" """,
                        """python3 -c "import os; print(os.environ.items())" """,
                        """python3 -c "import os; print(os.getenv('INVENTED_TOKEN'))" """,
                        """node -e "console.log(process.env.INVENTED_TOKEN)" """,
                        "python3 - <<'EOF'\nimport os\nprint(os.environ)\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(shell(command), "deny")
        for command in ('powershell -NoProfile -Command "Get-ChildItem Env:"', "Get-Item Env:INVENTED_TOKEN", "gc env:INVENTED_TOKEN",
                        '[Environment]::GetEnvironmentVariable("INVENTED_TOKEN")'):
            with self.subTest(command=command):
                self.assertEqual(shell(command, "PowerShell"), "deny")
        self.assertIsNone(shell("Get-Item Env:PATH", "PowerShell"))
        self.assertIsNone(shell('[Environment]::GetEnvironmentVariable("PATH")', "PowerShell"))



if __name__ == "__main__":
    unittest.main()
