#!/usr/bin/env python3
"""No PM OS background process opens a terminal window on Windows.

A process started with DETACHED_PROCESS has no console, so each console program it runs
(git, python, bash) gets a new console that Windows hands to Windows Terminal: one visible
window per call. no_window.py holds the fix. These tests fail when a spawn goes back to
DETACHED_PROCESS, when the two spawns that caused the incident lose the hidden console, or
(on Windows) when a console-less helper's children stop getting one. Run from system/scripts:

    python3 -m unittest test_no_window
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import no_window  # noqa: E402

DETACHED = no_window.DETACHED_PROCESS
SOURCE_SUFFIXES = {".py", ".sh", ".ts", ".js", ".mjs", ".ps1"}
SKIP_PARTS = {"node_modules", ".git", "_archive", ".amp", "graphify-out"}
SELF = {"system/scripts/no_window.py", "system/scripts/test_no_window.py"}
SPAWN_CONTEXT = re.compile(r"creationflags|\bflags\s*=|CreateProcess|process_flags", re.I)
HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")


def repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True,
                             check=True, **no_window.hidden_kwargs()).stdout.strip()
        return Path(out)
    except (OSError, subprocess.CalledProcessError):
        return HERE.parents[1]


def tracked_sources(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
                             **no_window.hidden_kwargs()).stdout.decode("utf-8", "replace")
        names = [name for name in out.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()]
    paths = []
    for name in names:
        rel = Path(name)
        if rel.suffix in SOURCE_SUFFIXES and not SKIP_PARTS & set(rel.parts) and name not in SELF:
            paths.append(root / rel)
    return paths


def code_part(line: str, suffix: str) -> str:
    """The line without a trailing comment, near enough for a flag scan. A `//` inside a URL
    or a `#` inside a word is not a comment."""
    pattern = r"(^|\s)//" if suffix in {".ts", ".js", ".mjs"} else r"(^|\s)#"
    match = re.search(pattern, line)
    return line if match is None else line[:match.start()]


def _flag_bits(node: ast.AST, names: dict[str, int]) -> int:
    """The flag bits an expression can set, as far as the source shows them."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return DETACHED if node.id == "DETACHED_PROCESS" else names.get(node.id, 0)
    if isinstance(node, ast.Attribute):
        return DETACHED if node.attr == "DETACHED_PROCESS" else names.get(node.attr, 0)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        name = node.args[1]
        if isinstance(name, ast.Constant) and name.value == "DETACHED_PROCESS":
            return DETACHED
        return _flag_bits(node.args[2], names) if len(node.args) > 2 else 0
    if isinstance(node, ast.BinOp):
        left, right = _flag_bits(node.left, names), _flag_bits(node.right, names)
        if isinstance(node.op, (ast.BitOr, ast.Add)):
            return left | right
        if isinstance(node.op, ast.BitAnd):
            return left & right
        return 0
    if isinstance(node, ast.IfExp):
        return _flag_bits(node.body, names) | _flag_bits(node.orelse, names)
    return 0


def _py_flag_exprs(tree: ast.AST) -> list[ast.AST]:
    """Every expression that becomes creationflags: a keyword, a dict or subscript key, or a *flags name."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found += [kw.value for kw in node.keywords if kw.arg == "creationflags"]
        elif isinstance(node, ast.Dict):
            found += [v for k, v in zip(node.keys, node.values)
                      if isinstance(k, ast.Constant) and k.value == "creationflags"]
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                key = target.slice if isinstance(target, ast.Subscript) else None
                if (isinstance(key, ast.Constant) and key.value == "creationflags") or \
                        (isinstance(target, ast.Name) and target.id.lower().endswith("flags")):
                    found.append(node.value)
    return found


def _py_detached(text: str) -> list[str] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names: dict[str, int] = {}
    for _ in range(2):  # twice, so a constant built from another resolves in any order
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                bits = _flag_bits(node.value, names)
                if bits:
                    names[node.targets[0].id] = bits
    lines = text.splitlines()
    hits = set()
    for expr in _py_flag_exprs(tree):
        if _flag_bits(expr, names) & DETACHED:
            line = lines[expr.lineno - 1].strip() if 0 < expr.lineno <= len(lines) else ""
            hits.add((expr.lineno, line))
    return [f"{number}: {line}" for number, line in sorted(hits)]


def detached_spawns(path: Path, text: str) -> list[str]:
    """Places that start a Windows process with DETACHED_PROCESS, by name or by flag value.

    Python goes through the AST, so a multi-line flag expression, a named constant and
    getattr(subprocess, "DETACHED_PROCESS", ...) all count, and a mask such as
    `value & 0xff` does not. Shell heredocs and other files are read line by line."""
    if path.suffix == ".py":
        hits = _py_detached(text)
        if hits is not None:
            return hits
    if path.suffix in {".ts", ".js", ".mjs"}:
        return []  # Node and Bun ask for DETACHED_PROCESS with `detached: true`: bare_node_detached
    hits = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = code_part(raw, path.suffix)
        named = bool(re.search(r"\bDETACHED_PROCESS\b", line))
        literal = 0
        if SPAWN_CONTEXT.search(line):
            for value in HEX.findall(line):
                literal |= int(value, 16)
        if named or literal & DETACHED:
            hits.append(f"{number}: {raw.strip()}")
    return hits


def bare_node_detached(path: Path, text: str) -> list[str]:
    """Node or Bun detached spawns whose command does not come from detachedSpawnPlan
    (DETACHED_PROCESS on Windows). Judged per spawn: the plan must feed that call."""
    if path.suffix not in {".ts", ".js", ".mjs"} or ".test." in path.name:
        return []
    lines = text.splitlines()
    hits = []
    for index, raw in enumerate(lines):
        if not re.search(r"\bdetached\s*:\s*true\b", code_part(raw, path.suffix)):
            continue
        window = "\n".join(code_part(line, path.suffix) for line in lines[max(0, index - 6):index + 1])
        if not re.search(r"spawn\(\s*plan\.command\s*,\s*plan\.args", window):
            hits.append(f"{index + 1}: {raw.strip()}")
    return hits


class SourceScanTests(unittest.TestCase):
    def test_scanner_catches_the_incident_shapes(self) -> None:
        bad = [
            'kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)',
            "flags = 0x00000008 | 0x00000200 | NO_WINDOW",  # CREATE_NO_WINDOW is ignored beside DETACHED_PROCESS
            "subprocess.Popen(cmd, creationflags=0x00000208)",
            "subprocess.Popen(cmd, creationflags=(\n    subprocess.DETACHED_PROCESS\n    | 0x200))",  # split across lines
            "DETACHED = 0x00000008\nsubprocess.Popen(cmd, creationflags=DETACHED)",  # a named constant
            "subprocess.Popen(cmd, **({'creationflags': 0x208} if os.name == 'nt' else {}))",
            'subprocess.Popen(["a#b"], creationflags=0x8)',  # a # inside a string is not a comment
        ]
        for source in bad:
            self.assertTrue(detached_spawns(Path("x.py"), source), source)
        heredoc = "python3 - <<" + "'PY'\nsubprocess.Popen(c, creationflags=0x00000208)\nPY"
        self.assertTrue(detached_spawns(Path("x.sh"), heredoc))
        self.assertTrue(detached_spawns(Path("x.sh"), "kw = subprocess.DETACHED_PROCESS"))
        good = [
            "subprocess.Popen(cmd, creationflags=0x08000200)",
            'kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP',
            "subprocess.run(cmd, creationflags=0x4 | 0x08000000)",
            "# never DETACHED_PROCESS: flags = 0x8",
            "flags = value & 0xff",  # a mask, not a spawn flag
            '"""Never DETACHED_PROCESS: a console-less child opens a window."""',
        ]
        for source in good:
            self.assertFalse(detached_spawns(Path("x.py"), source), source)
        self.assertTrue(bare_node_detached(Path("lib.ts"), "spawn(bin, args, { detached: true })"))
        self.assertTrue(bare_node_detached(Path("lib.ts"), "// see detachedSpawnPlan(\nspawn(bin, args, { detached: true })"))
        self.assertTrue(bare_node_detached(Path("lib.ts"), "spawn(bin, args, { cwd: 'https://x', detached: true })"))
        self.assertFalse(bare_node_detached(Path("lib.ts"), "const plan = detachedSpawnPlan(bin, args)\n"
                                            "const child = spawn(plan.command, plan.args, {\n  detached: true,"))

    def test_no_windows_spawn_uses_detached_process(self) -> None:
        root = repo_root()
        problems = []
        for path in tracked_sources(root):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            problems += [f"{rel}:{hit}" for hit in detached_spawns(path, text)]
            problems += [f"{rel}:{hit} (use detachedSpawnPlan)" for hit in bare_node_detached(path, text)]
        self.assertEqual(problems, [], "A Windows spawn with DETACHED_PROCESS leaves the child with no console, so every "
                         "git or python call under it opens a terminal window. Use no_window.detached_kwargs() "
                         "(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP), or no_window.py spawn from bash and TypeScript.")


class HelperTests(unittest.TestCase):
    def test_detached_kwargs_is_a_hidden_console_on_windows(self) -> None:
        with mock.patch.object(no_window, "is_windows", return_value=True):
            flags = no_window.detached_kwargs()["creationflags"]
        self.assertEqual(flags, 0x08000200)
        self.assertFalse(flags & DETACHED)
        with mock.patch.object(no_window, "is_windows", return_value=False):
            self.assertEqual(no_window.detached_kwargs(), {"start_new_session": True})

    def test_with_hidden_console_keeps_an_explicit_choice(self) -> None:
        self.assertEqual(no_window.with_hidden_console(0), no_window.CREATE_NO_WINDOW)
        self.assertEqual(no_window.with_hidden_console(0x200), 0x200 | no_window.CREATE_NO_WINDOW)
        self.assertEqual(no_window.with_hidden_console(no_window.CREATE_NEW_CONSOLE), no_window.CREATE_NEW_CONSOLE)

    def test_install_is_a_no_op_off_windows_and_when_switched_off(self) -> None:
        original = subprocess.Popen.__init__
        try:
            with mock.patch.object(no_window, "is_windows", return_value=False):
                self.assertFalse(no_window.install(force=True))
            with mock.patch.object(no_window, "is_windows", return_value=True), \
                    mock.patch.dict(os.environ, {"PM_OS_NO_WINDOW": "off"}):
                self.assertFalse(no_window.install(force=True))
            self.assertIs(subprocess.Popen.__init__, original)
        finally:
            subprocess.Popen.__init__ = original  # type: ignore[method-assign]
            no_window._installed = False

    def test_install_adds_the_flag_to_later_calls(self) -> None:
        original = subprocess.Popen.__init__
        seen: list[int] = []

        def fake_init(self, *args, **kwargs):  # noqa: ANN001
            seen.append(kwargs.get("creationflags", -1))
            raise OSError("stop before any process starts")

        try:
            subprocess.Popen.__init__ = fake_init  # type: ignore[method-assign]
            no_window._installed = False
            with mock.patch.object(no_window, "is_windows", return_value=True), \
                    mock.patch.dict(os.environ, {"PM_OS_NO_WINDOW": ""}):
                self.assertTrue(no_window.install(force=True))
            for call in (lambda: subprocess.run(["git", "status"]),
                         lambda: subprocess.Popen(["git"], creationflags=0x200),
                         lambda: subprocess.Popen(["x"], creationflags=no_window.CREATE_NEW_CONSOLE)):
                with self.assertRaises(OSError):
                    call()
        finally:
            subprocess.Popen.__init__ = original  # type: ignore[method-assign]
            no_window._installed = False
        self.assertEqual(seen, [no_window.CREATE_NO_WINDOW, 0x200 | no_window.CREATE_NO_WINDOW,
                                no_window.CREATE_NEW_CONSOLE])


class IncidentSpawnTests(unittest.TestCase):
    """Two spawns that once opened windows: the to-do capture after a report,
    and the ask-cos retry. Both now pass CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP."""

    def test_owner_action_capture_spawn_self(self) -> None:
        import owner_action_capture

        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(owner_action_capture.owner_tabs, "STATE_DIR", tmp), \
                mock.patch.object(no_window, "is_windows", return_value=True), \
                mock.patch.object(owner_action_capture.subprocess, "Popen",
                                  side_effect=lambda cmd, **kw: calls.append(kw)):
            owner_action_capture.spawn_self(["sync", "--task-id", "rt-x"])
        self.assertEqual(calls[0]["creationflags"], 0x08000200)
        self.assertNotIn("start_new_session", calls[0])

    def test_ask_cos_spawn_retry(self) -> None:
        import ask_cos

        calls = []
        with mock.patch.object(no_window, "is_windows", return_value=True), \
                mock.patch.object(ask_cos.subprocess, "Popen", side_effect=lambda cmd, **kw: calls.append(kw)):
            ask_cos.spawn_retry("rt-x", "q1")
        self.assertEqual(calls[0]["creationflags"], 0x08000200)


PROBE = """
import ctypes, sys
buffer = (ctypes.c_uint32 * 8)()
print(ctypes.windll.kernel32.GetConsoleProcessList(buffer, 8))
"""


@unittest.skipUnless(os.name == "nt", "a live console check needs Windows")
class LiveWindowsTests(unittest.TestCase):
    """Real processes. Nothing here can open a window: every console-less process below
    either runs no console child, or runs one only after the fix gives it a hidden console."""

    def console_count(self, flags: int) -> int:
        out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True,
                             creationflags=flags).stdout
        return int(out.strip())

    def test_detached_kwargs_child_has_its_own_hidden_console(self) -> None:
        self.assertEqual(self.console_count(no_window.DETACH_FLAGS), 1)
        self.assertEqual(self.console_count(DETACHED), 0, "DETACHED_PROCESS leaves no console")

    def test_console_less_helper_gives_its_children_a_hidden_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            # A console-less parent, as a DETACHED_PROCESS spawn once made, that calls install()
            # the way every helper entry point does, then runs a console child.
            parent = (
                "import subprocess, sys\n"
                f"sys.path.insert(0, {str(HERE)!r})\n"
                "import no_window\n"
                "own = no_window.has_console()\n"
                "patched = no_window.install()\n"
                # Never run the child unpatched: a console-less parent's console child opens a window.
                f"child = subprocess.run([sys.executable, '-c', {PROBE!r}], capture_output=True, text=True).stdout.strip() if patched else 'skipped'\n"
                f"open({str(out)!r}, 'w').write(f'{{own}} {{patched}} {{child}}')\n"
            )
            subprocess.run([sys.executable, "-c", parent], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=DETACHED, timeout=60, check=False)
            own, patched, child = out.read_text().split()
        self.assertEqual(own, "False")
        self.assertEqual(patched, "True")
        self.assertEqual(child, "1", "the child has its own hidden console, so no window opens")

    def test_spawn_cli_from_a_console_less_parent(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp) / "out.txt"
            script = Path(tmp) / "probe.py"
            script.write_text(PROBE + f"open({str(out)!r}, 'w').write('done')\n")
            log = Path(tmp) / "spawn.log"
            subprocess.run([sys.executable, str(HERE / "no_window.py"), "spawn", "--log", str(log), "--",
                            sys.executable, str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=DETACHED, timeout=60, check=True)
            for _ in range(100):
                if out.exists():
                    break
                time.sleep(0.1)
            self.assertEqual(out.read_text(), "done")
            self.assertEqual(log.read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()
