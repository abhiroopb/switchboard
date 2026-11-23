#!/usr/bin/env python3
"""wmux_look.py: merges only the look, never overwrites without --force, backs up, stays personal-data free."""
from __future__ import annotations

import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "windows" / "wmux_look.py"
SPEC = importlib.util.spec_from_file_location("wmux_look_under_test", SCRIPT)
assert SPEC and SPEC.loader
wmux_look = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wmux_look)

LOOK = json.loads(wmux_look.LOOK_FILE.read_text(encoding="utf-8"))


def run(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf), mock.patch.object(wmux_look, "wmux_running", return_value=False):
        code = wmux_look.main(list(argv))
    return code, buf.getvalue()


class LookFileTest(unittest.TestCase):
    def test_look_file_holds_only_appearance_and_layout(self) -> None:
        text = wmux_look.LOOK_FILE.read_text(encoding="utf-8")
        for banned in ("defaultCwd", "defaultShell", "restore", "agentIntegration", "savedLayouts",
                       "quickLaunch", "customBackground\"", "shortcuts", "workspaces", "bounds"):
            self.assertNotIn(banned, text)
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\|/Users/|/home/|@", text))
        self.assertEqual(set(LOOK["session"]), {"sidebarWidth"})

    def test_look_values(self) -> None:
        s = LOOK["settings"]
        self.assertEqual(s["wmux-terminal-prefs"]["fontSize"], 19)
        self.assertEqual(s["wmux-terminal-prefs"]["theme"], "Monokai")
        self.assertEqual(s["wmux-appearance-prefs"]["uiTheme"], "dark")
        self.assertEqual(s["wmux-workspace-prefs"]["explorerPosition"], "sidebar")
        self.assertTrue(s["wmux-browser-prefs"]["openOnStartup"])


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "settings.json"
        self.session = self.dir / "sessions" / "session.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def backups(self) -> list[Path]:
        return sorted(self.dir.rglob("*.switchboard-bak-*"))

    def test_fresh_folder_gets_the_look(self) -> None:
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0, out)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        for block, values in LOOK["settings"].items():
            self.assertEqual({k: data[block][k] for k in values}, values)
        self.assertIn("start wmux once", out)
        self.assertEqual(self.backups(), [])
        self.assertEqual(run("check", "--settings-dir", str(self.dir))[0], 0)

    def test_other_keys_are_kept_and_file_backed_up(self) -> None:
        self.settings.write_text(json.dumps({
            "agentIntegration": {"decision": "granted"},
            "wmux-workspace-prefs": {"defaultShell": "bash.exe", "defaultCwd": "D:\\work"},
            "wmux-appearance-prefs": {"uiTheme": "dark", "uiModeDefaultRev": 1},
        }), encoding="utf-8")
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0, out)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["agentIntegration"], {"decision": "granted"})
        self.assertEqual(data["wmux-workspace-prefs"]["defaultShell"], "bash.exe")
        self.assertEqual(data["wmux-workspace-prefs"]["defaultCwd"], "D:\\work")
        self.assertEqual(data["wmux-appearance-prefs"]["uiModeDefaultRev"], 1)
        self.assertEqual(len(self.backups()), 1)

    def test_conflict_writes_nothing_without_force(self) -> None:
        original = json.dumps({"wmux-terminal-prefs": {"fontSize": 12, "theme": "Dracula"}})
        self.settings.write_text(original, encoding="utf-8")
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, wmux_look.EXIT_CONFLICT)
        self.assertIn("wmux-terminal-prefs.fontSize: 12 -> 19", out)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original)
        self.assertEqual(self.backups(), [])

    def test_force_replaces_after_backup(self) -> None:
        original = json.dumps({"wmux-terminal-prefs": {"fontSize": 12}})
        self.settings.write_text(original, encoding="utf-8")
        code, out = run("apply", "--force", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["wmux-terminal-prefs"]["fontSize"], 19)
        [bak] = self.backups()
        self.assertEqual(bak.read_text(encoding="utf-8"), original)

    def test_bool_and_int_are_not_the_same_setting(self) -> None:
        self.settings.write_text(json.dumps({"wmux-browser-prefs": {"openOnStartup": 1}}), encoding="utf-8")
        self.assertEqual(run("apply", "--settings-dir", str(self.dir))[0], wmux_look.EXIT_CONFLICT)

    def test_dry_run_writes_nothing(self) -> None:
        code, out = run("apply", "--dry-run", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn("would set", out)
        self.assertFalse(self.settings.exists())

    def test_session_sidebar_width(self) -> None:
        self.session.parent.mkdir(parents=True)
        self.session.write_text(json.dumps({"version": 1, "windows": [
            {"sidebarWidth": 240, "workspaces": [{"title": "kept"}]}]}), encoding="utf-8")
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0, out)
        data = json.loads(self.session.read_text(encoding="utf-8"))
        self.assertEqual(data["windows"][0]["sidebarWidth"], 270)
        self.assertEqual(data["windows"][0]["workspaces"], [{"title": "kept"}])
        self.assertEqual(len(self.backups()), 1)

    def test_dragged_sidebar_width_is_a_conflict(self) -> None:
        self.session.parent.mkdir(parents=True)
        self.session.write_text(json.dumps({"version": 1, "windows": [{"sidebarWidth": 320}]}), encoding="utf-8")
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, wmux_look.EXIT_CONFLICT)
        self.assertIn("sidebarWidth: 320 -> 270", out)

    def test_refuses_while_wmux_open(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(wmux_look, "wmux_running", return_value=True), \
                mock.patch.object(wmux_look.sys, "platform", "win32"), \
                mock.patch.dict(wmux_look.os.environ, {"APPDATA": str(self.dir)}):
            code = wmux_look.main(["apply", "--instance", "sbx"])
        self.assertEqual(code, wmux_look.EXIT_RUNNING)
        self.assertFalse((self.dir / "wmux-sbx" / "settings.json").exists())

    def test_instance_folder(self) -> None:
        self.assertEqual(wmux_look.default_settings_dir("sbx", {"APPDATA": "C:/A"}), Path("C:/A/wmux-sbx"))
        self.assertEqual(wmux_look.default_settings_dir(None, {"APPDATA": "C:/A"}), Path("C:/A/wmux"))

    def test_bad_json_is_left_alone(self) -> None:
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(run("apply", "--force", "--settings-dir", str(self.dir))[0], wmux_look.EXIT_INPUT)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_block_that_is_not_an_object(self) -> None:
        self.settings.write_text(json.dumps({"wmux-browser-prefs": "x"}), encoding="utf-8")
        self.assertEqual(run("check", "--settings-dir", str(self.dir))[0], wmux_look.EXIT_DRIFT)
        code, out = run("apply", "--settings-dir", str(self.dir))
        self.assertEqual(code, wmux_look.EXIT_CONFLICT, out)
        self.assertIn("is not an object", out)
        code, out = run("apply", "--force", "--settings-dir", str(self.dir))
        self.assertEqual(code, 0, out)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["wmux-browser-prefs"], {"openOnStartup": True})

    def test_failed_write_names_the_backups(self) -> None:
        self.settings.write_text(json.dumps({"wmux-terminal-prefs": {"fontSize": 12}}), encoding="utf-8")
        with mock.patch.object(wmux_look, "write_json", side_effect=PermissionError("locked")):
            code, out = run("apply", "--force", "--settings-dir", str(self.dir))
        self.assertEqual(code, wmux_look.EXIT_WRITE)
        self.assertIn("restore from:", out)
        self.assertEqual(len(self.backups()), 1)

    def test_backups_in_the_same_second_do_not_collide(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        first = wmux_look.backup(self.settings, "20260101T000000Z")
        second = wmux_look.backup(self.settings, "20260101T000000Z")
        self.assertNotEqual(first, second)

    def test_macos_skips(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(wmux_look.sys, "platform", "darwin"):
            code = wmux_look.main(["apply"])
        self.assertEqual(code, 0)
        self.assertIn("cmux keeps its own look", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
