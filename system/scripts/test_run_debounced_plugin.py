#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import subprocess
import sys
import tempfile
import time
import unittest


RUNNER = Path(__file__).with_name("run-debounced-plugin.py")


class DebouncedPluginRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.count = self.root / "count"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invocation(self, child: list[str], cooldown: float = 3600) -> list[str]:
        return [
            sys.executable, str(RUNNER), "--plugin", "test-plugin", "--key", "same-key",
            "--cooldown-seconds", str(cooldown), "--state-dir", str(self.root / "state"),
            "--", *child,
        ]

    def child(self, exit_code: int = 0, delay: float = 0) -> list[str]:
        script = (
            "from pathlib import Path; import sys,time; "
            f"p=Path({str(self.count)!r}); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            f"time.sleep({delay}); sys.exit({exit_code})"
        )
        return [sys.executable, "-c", script]

    def test_two_concurrent_processes_invoke_one_child(self) -> None:
        command = self.invocation(self.child(delay=0.2))
        processes = [subprocess.Popen(command) for _ in range(2)]
        outcomes = [process.wait(timeout=5) for process in processes]
        self.assertIn(0, outcomes)
        self.assertTrue(all(code in (0, 75) for code in outcomes))
        self.assertEqual(self.count.read_text(), "1")

    def test_failed_child_does_not_burn_cooldown(self) -> None:
        failed = subprocess.run(self.invocation(self.child(exit_code=7)), check=False)
        succeeded = subprocess.run(self.invocation(self.child()), check=False)
        self.assertEqual(failed.returncode, 7)
        self.assertEqual(succeeded.returncode, 0)
        self.assertEqual(self.count.read_text(), "2")

    def test_success_skips_next_child_but_zero_cooldown_runs_again(self) -> None:
        for _ in range(2):
            result = subprocess.run(self.invocation(self.child()), timeout=5, check=False)
            self.assertEqual(result.returncode, 0)
        self.assertEqual(self.count.read_text(), "1")
        result = subprocess.run(self.invocation(self.child(), cooldown=0), timeout=5, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.count.read_text(), "2")

    @unittest.skipIf(sys.platform == "win32", "Windows: an msvcrt lock cannot pass to a child process")
    def test_child_retains_lock_after_supervisor_exits(self) -> None:
        ready, release = self.root / "ready", self.root / "release"
        script = (
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).touch(); deadline=time.monotonic()+5;"
            f"\nwhile not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)"
        )
        supervisor = subprocess.Popen(self.invocation([sys.executable, "-c", script]))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            supervisor.terminate()  # Synthetic runner only, never a live plugin.
            supervisor.wait(timeout=5)
            blocked = subprocess.run(self.invocation(self.child()), timeout=5, check=False)
            self.assertEqual(blocked.returncode, 75)
            self.assertFalse(self.count.exists())
            self.assertFalse((self.root / "state/test-plugin/same-key.json").exists())
        finally:
            release.touch()
            supervisor.wait(timeout=7)
        lock = self.root / "state/test-plugin/same-key.lock"
        with lock.open("a+") as handle:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("synthetic child did not release lock")
                    time.sleep(.01)
        recovered = subprocess.run(self.invocation(self.child()), timeout=5, check=False)
        self.assertEqual(recovered.returncode, 0)
        self.assertEqual(self.count.read_text(), "1")

    def test_kernel_lock_releases_when_holder_exits(self) -> None:
        lock_path = self.root / "state" / "test-plugin" / "same-key.lock"
        lock_path.parent.mkdir(parents=True)
        holder = subprocess.Popen([
            sys.executable, "-c",
            "import sys; sys.path.insert(0, sys.argv[2])\n"
            "try:\n    import fcntl\nexcept ImportError:\n    import flock_compat as fcntl\n"
            "f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX)",
            str(lock_path), str(Path(__file__).resolve().parent),
        ])
        holder.wait(timeout=5)
        result = subprocess.run(self.invocation(self.child()), timeout=5, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.count.read_text(), "1")


if __name__ == "__main__":
    unittest.main()
