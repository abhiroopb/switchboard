"""flock_compat: the Windows flock() stand-in behaves like fcntl.flock.

On Windows the real msvcrt is used. Elsewhere a stand-in msvcrt maps
msvcrt.locking onto fcntl.lockf byte ranges and raises where Windows raises
(unlocking an unlocked range, locking a range the handle already holds), so
the same checks run on a Mac.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent

FAKE_MSVCRT = textwrap.dedent(
    """
    import sys, types
    try:
        import msvcrt  # real Windows
    except ImportError:
        import fcntl as _f, os as _os
        _held = set()
        m = types.ModuleType("msvcrt")
        m.LK_UNLCK, m.LK_LOCK, m.LK_NBLCK, m.LK_RLCK, m.LK_NBRLCK = 0, 1, 2, 3, 4
        def locking(fd, mode, nbytes):
            pos = _os.lseek(fd, 0, _os.SEEK_CUR)
            key = (fd, pos)
            if mode == m.LK_UNLCK:
                if key not in _held:
                    raise PermissionError(13, "Permission denied")
                _f.lockf(fd, _f.LOCK_UN, nbytes, pos, 0)
                _held.discard(key)
                return
            if key in _held:
                raise PermissionError(13, "Permission denied")
            try:
                _f.lockf(fd, _f.LOCK_EX | _f.LOCK_NB, nbytes, pos, 0)
            except OSError as exc:
                raise PermissionError(13, "Permission denied") from exc  # what Windows raises on contention
            _held.add(key)
        m.locking = locking
        msvcrt = m
    """
)

# Children install the stand-in as msvcrt; the test process only hands it to flock_compat.
PRELUDE = (FAKE_MSVCRT + "sys.modules['msvcrt'] = msvcrt\n"
           + f"sys.path.insert(0, {str(SCRIPTS)!r})\nimport flock_compat\n")


def run_child(body: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", PRELUDE + textwrap.dedent(body), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class FlockCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "x.lock"
        self.lock.write_text("payload\n")
        namespace: dict = {}
        exec(compile(FAKE_MSVCRT, "<fake-msvcrt>", "exec"), namespace)
        sys.path.insert(0, str(SCRIPTS))
        import flock_compat

        self.flock = flock_compat
        patcher = mock.patch.object(flock_compat, "_msvcrt", return_value=namespace["msvcrt"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def hold(self, seconds: float) -> subprocess.Popen:
        child = run_child(
            """
            import time
            f = open(sys.argv[1], "a+")
            flock_compat.flock(f, flock_compat.LOCK_EX)
            print("held", flush=True)
            time.sleep(float(sys.argv[2]))
            """,
            str(self.lock), str(seconds),
        )
        self.assertEqual(child.stdout.readline().strip(), "held", child.stderr.read() if child.poll() else "")
        return child

    def test_constants_match_fcntl_names(self) -> None:
        for name in ("LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN"):
            self.assertTrue(hasattr(self.flock, name))

    def test_nonblocking_lock_fails_while_another_process_holds_it(self) -> None:
        child = self.hold(3)
        try:
            with open(self.lock, "a+") as f:
                with self.assertRaises(BlockingIOError):
                    self.flock.flock(f, self.flock.LOCK_EX | self.flock.LOCK_NB)
                # The payload stays readable: the lock sits past the end of the file.
                f.seek(0)
                self.assertEqual(f.read(), "payload\n")
        finally:
            child.kill()
            child.wait()

    def test_lock_is_free_after_holder_exits(self) -> None:
        child = self.hold(0)
        child.wait(timeout=5)
        with open(self.lock, "a+") as f:
            self.flock.flock(f, self.flock.LOCK_EX | self.flock.LOCK_NB)
            self.flock.flock(f, self.flock.LOCK_UN)

    def test_blocking_lock_waits_for_release(self) -> None:
        child = self.hold(0.4)
        start = time.monotonic()
        with open(self.lock, "a+") as f:
            self.flock.flock(f.fileno(), self.flock.LOCK_EX)
            self.assertGreater(time.monotonic() - start, 0.1)
            self.flock.flock(f.fileno(), self.flock.LOCK_UN)
        child.wait(timeout=5)

    def test_relock_converts_and_keeps_file_position(self) -> None:
        with open(self.lock, "r+") as f:
            f.seek(3)
            self.flock.flock(f, self.flock.LOCK_SH)
            self.flock.flock(f, self.flock.LOCK_EX)  # conversion must not deadlock
            self.assertEqual(f.tell(), 3)
            self.flock.flock(f, self.flock.LOCK_UN)
            self.flock.flock(f, self.flock.LOCK_UN)  # unlocking twice is harmless, as with flock
            self.assertEqual(f.read(), "load\n")

    def test_real_faults_are_raised_not_retried(self) -> None:
        def broken(fd, mode, nbytes):
            if mode != 0:  # the release call may fail quietly; the lock call must not spin
                raise OSError(9, "Bad file descriptor")

        fake = type("M", (), {"LK_UNLCK": 0, "LK_NBLCK": 2, "locking": staticmethod(broken)})
        with mock.patch.object(self.flock, "_msvcrt", return_value=fake), open(self.lock, "a+") as f:
            with self.assertRaises(OSError) as caught:
                self.flock.flock(f, self.flock.LOCK_EX)
            self.assertEqual(caught.exception.errno, 9)

    def test_operation_without_mode_is_rejected(self) -> None:
        with open(self.lock, "a+") as f, self.assertRaises(ValueError):
            self.flock.flock(f, self.flock.LOCK_NB)

    @unittest.skipUnless((SCRIPTS.parents[1] / ".pm-os-kit").is_dir(),
                         "kit installs only; the publisher gate covers the source repo")
    def test_runtime_scripts_never_import_fcntl_unguarded(self) -> None:
        """Every fcntl import sits in a try whose ImportError handler loads flock_compat (same rule as the publisher)."""
        import ast
        import warnings

        root = SCRIPTS.parents[1]
        offenders = []
        for path in list(root.glob("system/**/*.py")) + list(root.glob("daemon/*.py")):
            if "vendor" in path.parts:
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue  # verify-kit-install.sh compiles every file and reports these
            guarded = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Try) and any(
                        "flock_compat" in ast.unparse(h) and "ImportError" in ast.unparse(h.type or ast.Pass())
                        for h in node.handlers):
                    guarded.update(id(n) for stmt in node.body for n in ast.walk(stmt))
            for node in ast.walk(tree):
                imports_fcntl = (isinstance(node, ast.Import) and any(a.name == "fcntl" for a in node.names)
                                 or isinstance(node, ast.ImportFrom) and node.module == "fcntl")
                if imports_fcntl and id(node) not in guarded:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(offenders, [], "import fcntl needs the flock_compat fallback (see flock_compat.py)")

if __name__ == "__main__":
    unittest.main()
