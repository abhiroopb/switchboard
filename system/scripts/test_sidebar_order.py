#!/usr/bin/env python3
"""Tests for system/scripts/sidebar_order.py: the three-section sidebar order: CoS, To Do, Launchpad pinned on top, Workers in the
middle, Scheduled Jobs and Meetings at the bottom. The order applies on cmux
only; a second ruling (wmux cannot reorder, so cmux keeps the instructions)
keeps every wmux path from
reordering, planning drags, or restarting wmux."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
import sidebar_order as so  # noqa: E402


def rows(*titles):
    return [{"id": f"ws-{n}", "title": t} for n, t in enumerate(titles, 1)]


def titles(result):
    by_id = {r["id"]: r["title"] for s in result["sections"] for r in s["rows"]}
    return [by_id[i] for i in result["target"]]


class SectionTest(unittest.TestCase):
    def test_fixed_surfaces_match_with_emoji_and_glyphs(self):
        self.assertEqual(so.surface_key("🧭 CoS"), "cos")
        self.assertEqual(so.surface_key("✳ 🧭 CoS"), "cos")
        self.assertEqual(so.surface_key("✅ To Do"), "todo")
        self.assertEqual(so.surface_key("Todo"), "todo")
        self.assertEqual(so.surface_key("🚀 Launchpad"), "launchpad")
        self.assertEqual(so.surface_key("⏰ Scheduled Jobs"), "scheduled-jobs")
        self.assertEqual(so.surface_key("📅 Meetings Registry"), "meetings")
        self.assertEqual(so.surface_key("📅 Meetings"), "meetings")
        self.assertIsNone(so.surface_key("📊 PM OS Maintenance 2"))
        self.assertIsNone(so.surface_key("🧭 CoS review notes"))

    def test_sections(self):
        self.assertEqual(so.section_of("🧭 CoS"), "pinned")
        self.assertEqual(so.section_of("📊 PM OS Maintenance 1"), "workers")
        self.assertEqual(so.section_of("🔥 P0 · Ship it"), "workers")
        self.assertEqual(so.section_of("📅 Meetings Registry"), "background")
        self.assertEqual(so.section_of("Router 2"), "background")
        self.assertEqual([s["label"] for s in so.SECTIONS], ["Pinned", "Workers", "Background"])

    def test_comms_triage_off_is_a_worker_row_never_pinned(self):
        self.assertEqual(so.section_of("📬 Comms Triage", comms_on=True), "pinned")
        self.assertEqual(so.pinned_keys(comms_on=False), ["cos", "todo", "launchpad"])


class PlanTest(unittest.TestCase):
    def test_target_order_cos_todo_launchpad_workers_then_background(self):
        live = rows("🚀 Launchpad", "📊 PM OS Maintenance 1", "📅 Meetings Registry", "🧭 CoS",
                    "⏰ Scheduled Jobs", "✅ Todo", "📊 PM OS Maintenance 2", "📬 Comms Triage")
        result = so.plan(live, comms_on=True)
        self.assertEqual(titles(result), ["🧭 CoS", "✅ Todo", "🚀 Launchpad", "📬 Comms Triage",
                                          "📊 PM OS Maintenance 1", "📊 PM OS Maintenance 2",
                                          "⏰ Scheduled Jobs", "📅 Meetings Registry"])
        self.assertFalse(result["in_order"])
        self.assertEqual(result["missing_pinned"], [])

    def test_workers_keep_their_relative_order(self):
        live = rows("🧭 CoS", "B worker", "A worker", "C worker")
        self.assertEqual(titles(so.plan(live, comms_on=False))[1:], ["B worker", "A worker", "C worker"])

    def test_in_order_and_missing_pinned(self):
        live = rows("🧭 CoS", "🚀 Launchpad", "📊 PM OS Maintenance 1")
        result = so.plan(live, comms_on=False)
        self.assertTrue(result["in_order"])
        self.assertNotIn("moves", result, "no drag plan: wmux gets no ordering")
        self.assertEqual(result["missing_pinned"], ["todo"])

    def test_cli_order_prints_sections_and_says_cmux_only(self):
        out = subprocess.run([sys.executable, str(HERE / "sidebar_order.py"), "order"],
                             capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PM_OS_COMMS_TRIAGE": "off"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Pinned: cos, todo, launchpad", out.stdout)
        self.assertIn("cmux only", out.stdout)
        gone = subprocess.run([sys.executable, str(HERE / "sidebar_order.py"), "plan"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(gone.returncode, 0, "the wmux drag plan is gone")


class CmuxOnlyTest(unittest.TestCase):
    """The order lives in cmux-helpers.sh for cmux and nowhere on wmux."""

    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_cmux_pin_list_matches_this_module(self):
        helpers = self.read("system/cmux-helpers.sh")
        body = helpers[helpers.index("arrange_workspace_sidebar() {"):]
        pinned = re.search(r'local -a top_pinned=\((.*?)\)', body).group(1)
        self.assertEqual(re.findall(r'"routine:([\w-]+)"', pinned),
                         ["megadesk" if k == "cos" else k for k in so.pinned_keys(True)])
        bottom = re.search(r'local -a bottom_unpinned=\((.*?)\)', body).group(1)
        self.assertEqual(re.findall(r'"routine:([\w-]+)"', bottom), [k for k, _ in so.BOTTOM])

    def test_arrange_is_a_no_op_on_wmux(self):
        helpers = self.read("system/cmux-helpers.sh")
        body = helpers[helpers.index("arrange_workspace_sidebar() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('[[ "${PM_OS_MUX:-}" == "wmux" ]] && return 0', body)
        self.assertNotIn("sidebar arrange", body)

    def test_no_wmux_ordering_steps_remain(self):
        start_day = self.read("system/start-day.sh")
        self.assertNotIn("phase_5b_sidebar_order", start_day)
        self.assertNotIn("sidebar_order.py", start_day)
        self.assertNotIn('bash "$routine_launcher" todo launchpad comms-triage', start_day)
        shim = self.read("system/scripts/mux/cmux-shim.py")
        self.assertNotIn("def arrange(", shim)
        self.assertNotIn('sub == "arrange"', shim)
        load = self.read("system/scripts/load-surface.sh")
        self.assertNotIn("drag", load)
        self.assertNotIn("sidebar_order.py", load)
        seed = self.read("system/windows/seed-session.ps1")
        self.assertNotIn("Sort-Object Key", seed)
        self.assertNotIn("BottomRows", seed)
        self.assertNotIn("Set-Prop $win workspaces", seed)


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "seed-session.ps1 needs PowerShell")
class SeedSessionTest(unittest.TestCase):
    """seed-session.ps1 writes the same order into a wmux session.json (a
    fixture file here; -Force -NoPrefs leaves the live wmux and its settings alone)."""

    def test_pins_and_tints_fixed_surfaces_and_keeps_the_row_order(self):
        scrambled = [("🚀 Launchpad", False), ("📊 PM OS Maintenance 1", False), ("📅 Meetings Registry", True),
                     ("🧭 CoS", False), ("⏰ Scheduled Jobs", False), ("✅ To Do", False), ("📊 PM OS Maintenance 2", False)]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session.json"
            session.write_text(json.dumps({"version": 1, "windows": [{"id": "w1", "workspaces": [
                {"id": f"ws-{n}", "title": t, "pinned": p, "shell": "bash.exe", "cwd": "C:/x", "splitTree": {"type": "leaf"}}
                for n, (t, p) in enumerate(scrambled, 1)]}]}, ensure_ascii=False), encoding="utf-8")
            sidecar = Path(tmp) / "sidecar.json"   # a stale order_index from the old arrange step is ignored
            sidecar.write_text(json.dumps({"workspaces": {"ws-4": {"order_index": 0}, "ws-1": {"order_index": 2}}}),
                               encoding="utf-8")
            env = {**os.environ, "PM_OS_COMMS_TRIAGE": "off", "PM_OS_WMUX_SHELL": "default"}
            out = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                  str(HERE.parent / "windows" / "seed-session.ps1"), "-SessionPath", str(session),
                                  "-Sidecar", str(sidecar), "-NoPrefs", "-Force"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            rows_out = json.loads(session.read_text(encoding="utf-8-sig"))["windows"][0]["workspaces"]
            self.assertEqual([w["title"] for w in rows_out], [t for t, _ in scrambled], "wmux order left alone")
            pinned = {w["title"]: w["pinned"] for w in rows_out}
            self.assertTrue(pinned["🧭 CoS"] and pinned["✅ To Do"] and pinned["🚀 Launchpad"])
            self.assertTrue(pinned["📅 Meetings Registry"], "no unpin pass for the bottom rows on wmux")
            self.assertEqual({w["title"]: w.get("customColor") for w in rows_out}["🧭 CoS"], "#5B8DEF")


if __name__ == "__main__":
    unittest.main()
