#!/usr/bin/env python3
"""docs/kit-port/three_way_kit.py on small trees. Run from system/scripts:

    python3 -m unittest test_three_way_kit
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE.parents[1] / "docs" / "kit-port" / "three_way_kit.py"
kit = None
if HELPER.exists():  # docs/kit-port never ships in the kit, and this test does
    _spec = importlib.util.spec_from_file_location("three_way_kit_test", HELPER)
    kit = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = kit
    _spec.loader.exec_module(kit)


@unittest.skipIf(kit is None, "docs/kit-port is not in a kit tree")
class ThreeWayTests(unittest.TestCase):
    VALUES = {"PM_OS_ROOT": "C:/Home/owner/pm-os", "OWNER_FIRST": "Pat"}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.trees = {name: self.root / name for name in ("base", "ours", "theirs")}
        for tree in self.trees.values():
            tree.mkdir()

    def put(self, tree: str, rel: str, data: str | bytes) -> None:
        path = self.trees[tree] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def run_merge(self) -> dict:
        self.out = self.root / "out"
        self.out.mkdir(exist_ok=True)
        return kit.run(self.trees["base"], self.trees["ours"], self.trees["theirs"], self.VALUES, self.out)

    def merged(self, rel: str) -> str:
        return (self.out / "tree" / rel).read_bytes().decode("utf-8")

    def test_edits_on_both_sides_merge_and_values_become_placeholders(self) -> None:
        self.put("base", "a.py", "one\ntwo\nthree\nfour\nfive\nroot = {{PM_OS_ROOT}}\n")
        self.put("ours", "a.py", "ONE\ntwo\nthree\nfour\nfive\nroot = {{PM_OS_ROOT}}\n")
        self.put("theirs", "a.py", "one\r\ntwo\r\nthree\r\nfour\r\nFIVE\r\nroot = C:/Home/owner/pm-os\r\n")
        report = self.run_merge()
        self.assertEqual(report["clean"], ["a.py"])
        self.assertEqual(self.merged("a.py"), "ONE\ntwo\nthree\nfour\nFIVE\nroot = {{PM_OS_ROOT}}\n")

    def test_git_bash_and_backslash_paths_are_reverse_rendered(self) -> None:
        self.put("theirs", "b.md", "/c/Home/owner/pm-os and C:\\Home\\owner\\pm-os, Pat\n")
        self.assertEqual(self.run_merge()["added"], ["b.md"])
        self.assertEqual(self.merged("b.md"), "{{PM_OS_ROOT}} and {{PM_OS_ROOT}}, {{OWNER_FIRST}}\n")

    def test_home_comes_from_the_exports_root_not_this_machine(self) -> None:
        self.put("theirs", "c.plist", "C:/Home/owner/.claude and C:/Users/pat/.claude\n")
        self.VALUES = {"PM_OS_ROOT": "C:/Users/pat/Dropbox/pm-os"}
        self.run_merge()
        self.assertEqual(self.merged("c.plist"), "C:/Home/owner/.claude and {{HOME}}/.claude\n")

    def test_the_install_record_stays_out_of_the_tree(self) -> None:
        self.put("theirs", ".pm-os-kit/install.json", "{}\n")
        self.assertEqual(self.run_merge()["skipped"], [".pm-os-kit/install.json"])
        self.assertFalse((self.out / "tree" / ".pm-os-kit").exists())

    def test_same_line_conflict_keeps_all_three_sides(self) -> None:
        self.put("base", "c.py", "flag = 0\n")
        self.put("ours", "c.py", "flag = 1\n")
        self.put("theirs", "c.py", "flag = 2\n")
        self.assertEqual(self.run_merge()["conflicts"], ["c.py"])
        text = self.merged("c.py")
        for marker in ("<<<<<<< v7", "||||||| v6", ">>>>>>> windows", "flag = 0"):
            self.assertIn(marker, text)

    def test_owner_local_skill_never_enters_the_tree(self) -> None:
        # The owner-local skill folders come from the helper's own skip list, so no skill is named here.
        local_skills = [p for p in kit.SKIP_PREFIXES if p.startswith(".agents/skills/")]
        self.assertTrue(local_skills, "the skip list names at least one owner-local skill folder")
        skill_file = f"{local_skills[0]}SKILL.md"
        self.put("theirs", skill_file, "private\n")
        self.put("theirs", "workstreams/x/CONTEXT.md", "private\n")
        report = self.run_merge()
        self.assertEqual(sorted(report["skipped"]), sorted([skill_file, "workstreams/x/CONTEXT.md"]))
        self.assertFalse((self.out / "tree" / ".agents").exists())
        self.assertFalse((self.out / "tree" / "workstreams").exists())

    def test_a_dropped_path_is_deleted_only_when_v7_left_it_alone(self) -> None:
        self.put("base", "same.py", "x\n")
        self.put("ours", "same.py", "x\n")
        self.put("base", "changed.py", "x\n")
        self.put("ours", "changed.py", "y\n")
        report = self.run_merge()
        self.assertEqual(report["deleted"], ["same.py"])
        self.assertEqual(report["conflicts"], ["changed.py"])
        self.assertFalse((self.out / "tree" / "same.py").exists())
        self.assertEqual(self.merged("changed.py"), "y\n")

    def test_binary_on_both_sides_keeps_v7_and_is_listed(self) -> None:
        self.put("base", "i.png", b"\0base")
        self.put("ours", "i.png", b"\0ours")
        self.put("theirs", "i.png", b"\0theirs")
        self.assertEqual(self.run_merge()["binary_conflicts"], ["i.png"])
        self.assertEqual((self.out / "tree" / "i.png").read_bytes(), b"\0ours")

    def test_binary_dropped_on_either_side_follows_the_text_rules(self) -> None:
        self.put("base", "both-gone.png", b"\0a")
        self.put("base", "v7-dropped.png", b"\0b")
        self.put("theirs", "v7-dropped.png", b"\0b")
        self.put("base", "export-dropped.png", b"\0c")
        self.put("ours", "export-dropped.png", b"\0c")
        report = self.run_merge()
        self.assertEqual(report["conflicts"], ["v7-dropped.png"])
        self.assertEqual(report["deleted"], ["export-dropped.png"])
        self.assertFalse((self.out / "tree" / "both-gone.png").exists())

    def test_main_refuses_a_used_out_folder_and_writes_the_report(self) -> None:
        self.put("theirs", "n.md", "new\n")
        values = self.root / "install.json"
        values.write_text(json.dumps({"values": self.VALUES}), encoding="utf-8")
        argv = ["--base", str(self.trees["base"]), "--ours", str(self.trees["ours"]), "--theirs", str(self.trees["theirs"]),
                "--values", str(values), "--out", str(self.root / "fresh")]
        self.assertEqual(kit.main(argv), 0)
        self.assertEqual(json.loads((self.root / "fresh" / "report.json").read_text())["added"], ["n.md"])
        with self.assertRaises(SystemExit):
            kit.main(argv)


if __name__ == "__main__":
    unittest.main()
