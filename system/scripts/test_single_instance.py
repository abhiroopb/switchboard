#!/usr/bin/env python3
"""Synthetic process-level tests for switchboard invocation locking."""
from __future__ import annotations

import os
import importlib.util
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent


def run_python(code: str, lock_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWITCHBOARD_LOCK_DIR"] = str(lock_dir)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    return subprocess.run(
        [sys.executable, "-c", code, *args], env=env, capture_output=True, text=True, check=False
    )


class SingleInstanceTests(unittest.TestCase):

    def test_entrypoints_hold_lock_before_work_and_help_does_not_write(self) -> None:
        scripts = {
            "build-workspace-signals": ["synthetic"],
        }
        for name, args in scripts.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                with mock.patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
                    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                def invoke(arguments):
                    with mock.patch.object(sys, "argv", [name, *arguments]):
                        return module.main() if name.startswith("refresh") else module.main(arguments)

                def work(_args):
                    attempt = run_python(
                        "from single_instance import single_instance, LockBusyError; import sys;"
                        "\ntry:\n with single_instance(sys.argv[1], timeout=0): pass"
                        "\nexcept LockBusyError: raise SystemExit(75)", root / "locks", name,
                    )
                    self.assertEqual(attempt.returncode, 75, attempt.stderr)
                    return 17

                with mock.patch.dict(os.environ, {"SWITCHBOARD_LOCK_DIR": str(root / "locks")}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_result:
                        invoke(["--help"])
                    self.assertEqual(exit_result.exception.code, 0)
                    self.assertEqual(list(root.iterdir()), [])
                    with mock.patch.object(module, "_run", side_effect=work):
                        self.assertEqual(invoke(args), 17)

    def test_atomic_publish_preserves_old_bytes_and_cleans_failed_temps(self) -> None:
        for name in ("build-workspace-signals",):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                target = root / "snapshot"
                target.write_text("old bytes")
                with mock.patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
                    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                with mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace failure")):
                    with self.assertRaises(OSError):
                        module.atomic_write_text(target, "new bytes")
                self.assertEqual(target.read_text(), "old bytes")
                self.assertEqual(list(root.iterdir()), [target])

                real_tempfile = tempfile.NamedTemporaryFile

                @contextlib.contextmanager
                def failing_writer(*args, **kwargs):
                    with real_tempfile(*args, **kwargs) as stream:
                        with mock.patch.object(stream, "write", side_effect=OSError("synthetic full disk")):
                            yield stream

                with mock.patch.object(module.tempfile, "NamedTemporaryFile", failing_writer):
                    with self.assertRaises(OSError):
                        module.atomic_write_text(target, "new bytes")
                self.assertEqual(target.read_text(), "old bytes")
                self.assertEqual(list(root.iterdir()), [target])
                module.atomic_write_text(target, "new bytes")
                self.assertEqual(target.read_text(), "new bytes")


    def test_exclusion_reports_busy_and_keeps_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_dir = Path(raw) / "locks"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from single_instance import single_instance; import time; "
                    "\nwith single_instance('job'): print('held', flush=True); time.sleep(2)",
                ],
                env={**os.environ, "SWITCHBOARD_LOCK_DIR": str(lock_dir), "PYTHONPATH": str(SCRIPTS_DIR)},
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(holder.stdout.readline().strip(), "held")
            blocked = run_python(
                "from single_instance import LockBusyError, single_instance; "
                "\ntry:\n with single_instance('job', timeout=.1): pass"
                "\nexcept LockBusyError as exc: print(exc); raise SystemExit(75)",
                lock_dir,
            )
            self.assertEqual(blocked.returncode, 75)
            self.assertIn("busy: job is already running", blocked.stdout)
            holder.wait(timeout=5)
            holder.stdout.close()
            self.assertTrue((lock_dir / "job.lock").exists())

    def test_process_exit_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            lock_dir = Path(raw) / "locks"
            crashed = run_python(
                "from single_instance import single_instance; import os; "
                "\nwith single_instance('job'): os._exit(23)",
                lock_dir,
            )
            self.assertEqual(crashed.returncode, 23)
            acquired = run_python(
                "from single_instance import single_instance; "
                "\nwith single_instance('job', timeout=.2): print('acquired')",
                lock_dir,
            )
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
            self.assertEqual(acquired.stdout.strip(), "acquired")

    def test_lock_precedes_read_compute_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_dir = root / "locks"
            counter = root / "counter"
            counter.write_text("0")
            code = (
                "from pathlib import Path; from single_instance import single_instance; import sys,time; "
                "\np=Path(sys.argv[1]); "
                "\nwith single_instance('increment', timeout=5):"
                "\n n=int(p.read_text()); time.sleep(.02); p.write_text(str(n+1))"
            )
            env = {**os.environ, "SWITCHBOARD_LOCK_DIR": str(lock_dir), "PYTHONPATH": str(SCRIPTS_DIR)}
            procs = [
                subprocess.Popen([sys.executable, "-c", code, str(counter)], env=env)
                for _ in range(12)
            ]
            for proc in procs:
                self.assertEqual(proc.wait(timeout=10), 0)
            self.assertEqual(counter.read_text(), "12")


if __name__ == "__main__":
    unittest.main()
