#!/usr/bin/env python3
"""`close <n>` and the action map under wmux (2026-11-13, rt-000000000044).

On a Windows machine the map held only the tabs open at the last 15-minute
render, so `close <n>` refused every tab opened since, and CoS closed five tabs
with `wmux close-workspace` directly, skipping the wrap-up and draft checks.
wmux refs come from a counter that only goes up, so there a missed handle is
refreshed from the live list. cmux reuses numbers and keeps failing closed.

Hermetic: a stub cmux and a temp state folder. PM_OS_TEST_SANDBOX=1 keeps every
mux call off the live tabs.

  python3 system/scripts/test_command_center_action_wmux.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ACTION = SCRIPTS / "command-center-action.sh"
LOOP = SCRIPTS / "command-center-loop.sh"


def find_bash() -> str | None:
    """Git Bash or a real Unix bash; WSL's System32 launcher cannot read these Windows paths."""
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()

# Stub cmux: answers from live.json, logs every call, and a close removes the tab.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
live_path = os.environ["CC_TEST_LIVE"]
with open(os.environ["CC_TEST_CALLS"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
live = json.load(open(live_path, encoding="utf-8"))
args = sys.argv[1:]
verb = args[0] if args else ""
def flag(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""
if verb == "rpc" and args[1:2] == ["workspace.list"]:
    print(json.dumps(live))
elif verb == "list-workspaces":
    for w in live["workspaces"]:
        print(f"  {w['ref']}  {w['title']}")
elif verb == "close-workspace":
    ref = flag("--workspace")
    live["workspaces"] = [w for w in live["workspaces"] if w["ref"] != ref]
    json.dump(live, open(live_path, "w", encoding="utf-8"))
    print(f"Closed workspace {ref}")
'''


@unittest.skipUnless(BASH, "needs bash")
class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.stub = self.root / "bin" / "cmux"
        self.stub.parent.mkdir()
        self.stub.write_text(STUB, encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)
        self.live = self.root / "live.json"
        self.calls = self.root / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        self.map = self.state / "command-center-actions.json"

    def set_live(self, *tabs: tuple[int, str, str]) -> None:
        rows = [{"ref": f"workspace:{n}", "id": wid, "title": title, "custom_title": title,
                 "pinned": False, "selected": False, "description": ""} for n, wid, title in tabs]
        self.live.write_text(json.dumps({"workspaces": rows}), encoding="utf-8")

    def set_map(self, *tabs: tuple[int, str, str]) -> None:
        handles = {str(n): {"ref": f"workspace:{n}", "id": wid, "title": title} for n, wid, title in tabs}
        self.map.write_text(json.dumps({"generated_at": "2026-11-13T20:30:00+00:00", "handles": handles}),
                            encoding="utf-8")

    def env(self, mux: str, **extra: str) -> dict[str, str]:
        drop = {"CMUX", "CMUX_BIN", "CMUX_AMP_CMUX_BIN", "CMUX_WORKSPACE_ID", "CMUX_SURFACE_ID",
                "WMUX_SURFACE_ID", "PM_OS_MUX", "CC_STATE_DIR", "CC_DIGEST_SCRIPT", "STATE_DIR", "REPO_ROOT"}
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update({"PM_OS_TEST_SANDBOX": "1", "PM_OS_MUX": mux, "CMUX_BIN": str(self.stub),
                    "CMUX": str(self.stub), "CMUX_WORKSPACE_ID": "ws-test-caller",
                    "CC_STATE_DIR": str(self.state), "CC_TEST_LIVE": str(self.live),
                    "CC_TEST_CALLS": str(self.calls), "PM_OS_STATE_HOME": str(self.root),
                    "PYTHONIOENCODING": "utf-8"})
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def action(self, mux: str, *args: str, unset: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        env = self.env(mux)
        for key in unset:
            env.pop(key, None)
        return subprocess.run([BASH, str(ACTION), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)

    def calls_made(self) -> str:
        return self.calls.read_text(encoding="utf-8")

    def handles(self) -> dict:
        return json.loads(self.map.read_text(encoding="utf-8"))["handles"]


class WmuxCloseTests(Base):
    def test_close_takes_a_tab_opened_after_the_render(self) -> None:
        self.set_map((285, "ws-cos", "🧭 CoS"), (287, "ws-todo", "✅ Todo"))
        self.set_live((285, "ws-cos", "🧭 CoS"), (287, "ws-todo", "✅ Todo"), (290, "ws-pmos", "📊 PM OS Maintenance"))
        done = self.action("wmux", "close", "290")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("close-workspace --workspace workspace:290", self.calls_made())
        self.assertIn("closed workspace:290", done.stdout)
        # The refresh saw every open tab, CoS included.
        self.assertEqual(self.handles()["290"]["id"], "ws-pmos")
        self.assertEqual(set(self.handles()), {"285", "287", "290"})

    def test_works_with_no_map_and_only_cmux_bin_set(self) -> None:
        """No CMUX in the caller's environment: the cmux.app default must not win over the shim."""
        self.set_live((285, "ws-cos", "🧭 CoS"), (291, "ws-notes", "🤖 Meeting Notes"))
        done = self.action("wmux", "close", "291", unset=("CMUX",))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("close-workspace --workspace workspace:291", self.calls_made())
        self.assertTrue(self.map.exists())

    def test_resolve_names_the_tab_and_changes_nothing(self) -> None:
        self.set_map((285, "ws-cos", "🧭 CoS"))
        self.set_live((285, "ws-cos", "🧭 CoS"), (289, "ws-printer", "📲 Printer Setup"))
        done = self.action("wmux", "resolve", "289")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.strip(), "workspace:289\t📲 Printer Setup")
        for verb in ("close-workspace", "select-workspace", "send", "workspace-action"):
            self.assertNotIn(verb + " ", self.calls_made())

    def test_resolve_matches_the_whole_ref(self) -> None:
        """workspace:29 must not read the title of workspace:290 listed before it."""
        self.set_live((290, "ws-pmos", "📊 PM OS Maintenance"), (29, "ws-old", "📲 Printer Setup"))
        done = self.action("wmux", "resolve", "29")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.strip(), "workspace:29\t📲 Printer Setup")

    def test_a_closed_tab_is_refused(self) -> None:
        self.set_map((285, "ws-cos", "🧭 CoS"))
        self.set_live((285, "ws-cos", "🧭 CoS"))
        done = self.action("wmux", "close", "292")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no open tab is workspace:292", done.stderr)
        self.assertNotIn("close-workspace", self.calls_made())

    def test_a_mismatched_identity_still_fails_closed(self) -> None:
        self.set_map((290, "ws-old", "📊 PM OS Maintenance"))
        self.set_live((290, "ws-new", "🗂️ Example Assignment"))
        done = self.action("wmux", "close", "290")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("number reused", done.stderr)
        self.assertNotIn("close-workspace", self.calls_made())

    def test_a_name_is_not_a_handle(self) -> None:
        self.set_live((285, "ws-cos", "🧭 CoS"))
        done = self.action("wmux", "close", "workspace:285")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("not a tab number", done.stderr)
        self.assertNotIn("close-workspace", self.calls_made())


class CmuxUnchangedTests(Base):
    def test_cmux_still_refuses_a_handle_the_render_missed(self) -> None:
        self.set_map((7, "uuid-a", "📉 Multi-entity"))
        self.set_live((7, "uuid-a", "📉 Multi-entity"), (9, "uuid-b", "🤖 Scheduled Jobs"))
        before = self.map.read_text(encoding="utf-8")
        done = self.action("cmux", "close", "9")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("does not offer '9'", done.stderr)
        self.assertNotIn("close-workspace", self.calls_made())
        self.assertEqual(self.map.read_text(encoding="utf-8"), before, "cmux never rewrites the map on a miss")


class SkippedTickTests(Base):
    def test_a_skipped_tick_still_refreshes_the_map(self) -> None:
        self.set_map((285, "ws-cos", "🧭 CoS"))
        self.set_live((285, "ws-cos", "🧭 CoS"), (288, "ws-ai", "🧩 AI Prototyping"))
        # A fresh lock with no owner file reads as "another tick is starting".
        (self.state / "command-center-tick.lock").mkdir()
        env = self.env("wmux", STATE_DIR=str(self.state))
        done = subprocess.run([BASH, str(LOOP), "--once"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("skipping", done.stdout)
        self.assertIn("action map refreshed", done.stdout)
        self.assertEqual(set(self.handles()), {"285", "288"})

    def test_cmux_skipped_tick_leaves_the_map(self) -> None:
        """cmux reuses numbers: a rewrite between renders would let close 7 from the
        digest on screen pass the identity check for the newer tab now numbered 7."""
        self.set_map((7, "uuid-a", "📉 Multi-entity"))
        self.set_live((7, "uuid-b", "🤖 Scheduled Jobs"))
        (self.state / "command-center-tick.lock").mkdir()
        env = self.env("cmux", STATE_DIR=str(self.state))
        done = subprocess.run([BASH, str(LOOP), "--once"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.handles()["7"]["id"], "uuid-a")
        closed = self.action("cmux", "close", "7")
        self.assertNotEqual(closed.returncode, 0)
        self.assertNotIn("close-workspace", self.calls_made())

    def test_kill_switch_leaves_the_map(self) -> None:
        self.set_map((285, "ws-cos", "🧭 CoS"))
        self.set_live((285, "ws-cos", "🧭 CoS"), (288, "ws-ai", "🧩 AI Prototyping"))
        (self.state / "command-center-tick.lock").mkdir()
        env = self.env("wmux", STATE_DIR=str(self.state), CC_SKIP_MAP_REFRESH_DISABLED="1")
        done = subprocess.run([BASH, str(LOOP), "--once"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(set(self.handles()), {"285"})


if __name__ == "__main__":
    unittest.main()
