#!/usr/bin/env python3
"""Automation code opens pages in the background, never stealing focus.

Pages open without taking focus from the owner. macOS `open` activates the app it
hands a page to; `open -g` loads it behind whatever the owner is using. This test
scans the code PM OS runs on its own (scripts, plugins, hooks, daemons, skill
scripts) for a macOS `open` call without `-g` and fails on each one.

A foreground open is allowed only where the owner asked for it (they clicked a
link): mark that line with a `focus-ok: <reason>` comment. The runtime
counterpart for agent shell commands is the pm-os-guardrails `open-focus` rule
(system/amp-plugins/pm-os/open-focus.test.ts).

Run: python3 system/scripts/test_background_open.py
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent, check=True).stdout.strip())

# Code PM OS runs without the owner typing the command.
SCAN_DIRS = [
    "system/scripts",
    "system/bin",
    "system/amp-plugins",
    "system/claude-marketplace",
    "system/connectors",
    "system/windows",
    "system/pm-os-kit",
    "daemon",
    "routines",
    ".agents/skills",
]
SCAN_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".ts", ".mjs", ".js"}
SKIP_PARTS = {"_retired", "node_modules", "vendor", "_archive", "__pycache__"}

LAUNCHER = r"""(?:subprocess\.\w+|Popen|check_call|check_output|os\.exec\w*|os\.spawn\w*|Bun\.spawn(?:Sync)?|spawn(?:Sync)?|execFile(?:Sync)?)"""
OPEN_WORD = r"""(?:/usr/bin/)?open"""
# Python/TS argv lists or tuples that run macOS `open`: a sequence whose first
# element is "open" (or "/usr/bin/open"), handed straight to a process
# launcher (the sequence may start on the next line), returned, or assigned.
# A subscript (plan["open"]), an argparse name, or a subcommand of another tool
# (wmux.run(["open", url]), browser + ["open", uri]) is not.
ARGV_OPEN = re.compile(
    r"""(?:""" + LAUNCHER + r"""\(\s*|\breturn\s+|(?<![=!<>])=\s*)"""
    r"""[\[(]\s*["']""" + OPEN_WORD + r"""["']\s*(?:,|[\])])(?P<rest>[^\])]*)""")
# Node: spawn('open', [path]) / execFile('/usr/bin/open', [...]).
NODE_ARGV_OPEN = re.compile(
    r"""\b(?:spawn|execFile)(?:Sync)?\(\s*["'`]""" + OPEN_WORD + r"""["'`]\s*,\s*\[(?P<rest>[^\]]*)""")
# A command string: os.system('open …'), subprocess.run('open …', shell=True),
# exec/execSync(`open …`), Bun's $`open …`.
STRING_OPEN = re.compile(
    r"""(?:(?:os\.system|""" + LAUNCHER + r"""|\bexec(?:Sync)?)\(\s*f?|\$)["'`]""" + OPEN_WORD + r"""\s+(?P<rest>[^"'`\n]*)""")
# Shell: `open` (or /usr/bin/open) as a command word, at line start (any
# indent), after a separator or `{`/`(`, after `then`/`do`/`else`, or behind a
# wrapper (`exec`, `command`, `sudo`, `env`, `time`, `nohup`, `xargs`).
SHELL_OPEN = re.compile(
    r"""(?:^\s*|[;&|{(]\s*|\b(?:then|do|else|exec|command|sudo|env|time|nohup|xargs)\s+)"""
    + OPEN_WORD + r"""(?=[\s;&|]|$)(?P<rest>(?:[ \t]+[^\s;&|]+)*)""")
FOCUS_OK = re.compile(r"focus-ok:\s*\S")


def _is_test(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or ".test." in name or name.endswith("_test.py")


def _has_background_flag(words: list[str]) -> bool:
    for word in words:
        word = word.strip("\"'")
        if word == "--args":
            return False
        if word == "--background" or re.fullmatch(r"-[a-zA-Z]*g[a-zA-Z]*", word):
            return True
    return False


def _argv_open_is_foreground(m: re.Match[str]) -> bool:
    """True for an argv list that runs `open` with no -g. A list of status words
    (["open", "waiting", "done"]) is not an argv: its next element is a plain
    quoted word, where an argv has a flag, a path, a URL, or an expression."""
    if m.group(0).rstrip().endswith("]"):
        return False
    rest = m.group("rest").lstrip()
    first = re.match(r"""["']([^"']*)["']""", rest)
    if first and not first.group(1).startswith("-") and not re.search(r"[/.:$]", first.group(1)):
        return False
    words = re.findall(r"""["']([^"']*)["']""", rest)
    return not _has_background_flag(words)


def foreground_opens(text: str, suffix: str) -> list[tuple[int, str]]:
    """(line number, line) for every macOS `open` call without -g."""
    lines = text.splitlines()
    hits: dict[int, str] = {}

    def at(pos: int) -> int | None:
        n = text.count("\n", 0, pos) + 1
        line = lines[n - 1] if n <= len(lines) else ""
        if FOCUS_OK.search(line) or line.lstrip().startswith(("#", "//", "*")):
            return None
        return n

    def hit(pos: int) -> None:
        n = at(pos)
        if n is not None:
            hits.setdefault(n, lines[n - 1].strip())

    # Whole-text passes, so a call split across lines is still seen.
    for m in ARGV_OPEN.finditer(text):
        if _argv_open_is_foreground(m):
            hit(m.start())
    for m in NODE_ARGV_OPEN.finditer(text):
        if not _has_background_flag(re.findall(r"""["'`]([^"'`]*)["'`]""", m.group("rest"))):
            hit(m.start())
    for m in STRING_OPEN.finditer(text):
        if not _has_background_flag(m.group("rest").split()):
            hit(m.start())
    if suffix in {".sh", ".bash", ".zsh"}:
        for n, line in enumerate(lines, 1):
            if FOCUS_OK.search(line) or line.lstrip().startswith("#"):
                continue
            # Quoted text (echo "... open ...") is argument text, not a command.
            unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", lambda q: "Q" * len(q.group(0)), line)
            for m in SHELL_OPEN.finditer(unquoted):
                rest = line[m.start("rest"):m.end("rest")]
                # `xargs open` takes its targets from stdin, so no words is still an open.
                has_target = rest.strip() or "xargs" in m.group(0)
                if has_target and not _has_background_flag(rest.split()):
                    hits.setdefault(n, line.strip())
    return sorted(hits.items())


def scan(root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in SCAN_SUFFIXES or not path.is_file() or _is_test(path):
                continue
            if SKIP_PARTS & set(path.relative_to(root).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "open" not in text:
                continue
            for n, line in foreground_opens(text, path.suffix):
                found.append(f"{path.relative_to(root)}:{n}: {line}")
    return found


class BackgroundOpen(unittest.TestCase):
    def test_repo_has_no_foreground_open(self) -> None:
        found = scan()
        self.assertEqual(found, [], "macOS `open` without -g steals focus from the owner. "
                         "Use `open -g` or image_compare.py open, or mark a click path `focus-ok: <reason>`:\n"
                         + "\n".join(found))

    def test_detector(self) -> None:
        bad_py = [
            'subprocess.run(["open", str(path)])',
            "subprocess.run(['/usr/bin/open', '-a', 'Safari', url])",
            'return ["open", str(path)]',
            "subprocess.Popen(('open', p))",
            "subprocess.run(\n    ['open', path],\n    check=True)",
            "os.system('open ' + p)",
            "subprocess.run(f'open {p}', shell=True)",
            "spawn('open', [path])",
            "execFile('/usr/bin/open', [path])",
            "spawnSync('open', ['-a', 'Safari', p])",
            "execSync(`open ${path}`)",
            "await $`open ${path}`",
        ]
        good_py = [
            'subprocess.run(["open", "-g", str(path)])',
            "subprocess.run(['open', '-g', '-a', 'QuickTime Player', p])",
            'subprocess.run(["open", str(path)])  # focus-ok: the owner clicked it',
            'choices=["open", "waiting", "done"]',
            "if status in ('open', 'in_progress'):",
            'STATES = ["open"]',
            "spawn('open', ['-g', path])",
            "execSync(`open -g ${path}`)",
            "os.system('open -g ' + p)",
            "subprocess.run(\n    ['open', '-g', path])",
        ]
        for line in bad_py:
            self.assertTrue(foreground_opens(line, ".py"), line)
        for line in good_py:
            self.assertFalse(foreground_opens(line, ".py"), line)
        bad_sh = [
            'open "$page"',
            '  then open "$target"',
            'exec /usr/bin/open "$img"',
            'cd /tmp && open page.html',
            '    open "$page" &',
            '\topen "$x"',
            '  { open "$x"; }',
            '  sudo open "$x"',
            '  printf x | xargs open',
        ]
        good_sh = [
            'open -g "$page"',
            'then open -g "$target"',
            'echo "open the page"',
            '"$CMUX" markdown open "$path" --focus false',
            'exec /usr/bin/open "$uri"  # focus-ok: the owner clicked this link',
            '# open "$page" in the browser',
            'rg -n open file.sh',
            '    open -g "$page" &',
            '    openssl x509 -in cert.pem',
            '  local open=1',
        ]
        for line in bad_sh:
            self.assertTrue(foreground_opens(line, ".sh"), line)
        for line in good_sh:
            self.assertFalse(foreground_opens(line, ".sh"), line)


if __name__ == "__main__":
    unittest.main()
