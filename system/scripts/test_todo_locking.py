try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TODO_CLI = REPO_ROOT / "system" / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"


def load_todo_cli():
    spec = importlib.util.spec_from_file_location("todo_cli_locking_test", TODO_CLI)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {TODO_CLI}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


todo_cli = load_todo_cli()


class TodoLockingTests(unittest.TestCase):
    def test_auto_reconcile_does_network_work_outside_write_lock_and_preserves_concurrent_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = pathlib.Path(temp_dir) / "todo.json"
            todo_path.write_text(json.dumps({
                "version": 2,
                "items": [{
                    "id": "todo-a",
                    "title": "Reply to teammate",
                    "status": "actionable",
                    "priority": "P2",
                }],
                "recurring_templates": [],
            }))
            reconcile_started = threading.Event()
            release_reconcile = threading.Event()

            def slow_reconcile(payload, lookback_days, apply, diagnose=False):
                self.assertFalse(apply)
                reconcile_started.set()
                self.assertTrue(release_reconcile.wait(timeout=5))
                return {
                    "lookback_days": lookback_days,
                    "reconciled": [{
                        "id": "todo-a",
                        "title": "Reply to teammate",
                        "reason": "matched cached reply",
                    }],
                    "count": 1,
                }

            error: list[BaseException] = []

            def run_reconcile():
                try:
                    todo_cli.classify_payload(todo_path, auto_reconcile=True)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    error.append(exc)

            with (
                mock.patch.object(todo_cli, "reconcile_items", side_effect=slow_reconcile),
                mock.patch.object(todo_cli, "sync_calendar_payload", return_value={"updated": 0, "unsynced": 0}),
            ):
                worker = threading.Thread(target=run_reconcile)
                worker.start()
                self.assertTrue(reconcile_started.wait(timeout=5))

                lock_path = todo_path.with_suffix(".json.lock")
                with lock_path.open("w") as lock_handle:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    payload = json.loads(todo_path.read_text())
                    payload["items"].append({
                        "id": "todo-b",
                        "title": "Concurrent todo",
                        "status": "actionable",
                        "priority": "P1",
                    })
                    todo_path.write_text(json.dumps(payload))
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

                release_reconcile.set()
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(error, [])
            saved = json.loads(todo_path.read_text())
            by_id = {item["id"]: item for item in saved["items"]}
            self.assertEqual(by_id["todo-a"]["status"], "done")
            self.assertEqual(by_id["todo-b"]["status"], "actionable")


if __name__ == "__main__":
    unittest.main()
