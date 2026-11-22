#!/usr/bin/env python3
"""check-profile-leaks.py: a deny-list gate that keeps company and owner-local content out of the kit.

An owner-local skill stays off the work machine and out of the kit, and stays installed where
the owner uses it. Nothing employer-specific ships in the kit, and the sweep is a permanent gate. The terms live in a local deny list
(local/kit-deny-list.txt), never in the script, so the script itself ships clean. Every term in
these tests is invented. Run from system/scripts:

    python3 -m unittest test_check_profile_leaks
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


leaks = _load("check_profile_leaks_test", HERE / "check-profile-leaks.py")
DELTA = HERE.parents[1] / "docs" / "kit-port" / "build-kit-delta.py"
delta = _load("build_kit_delta_test", DELTA) if DELTA.exists() else None  # docs/kit-port never ships in the kit

TERM = "zorb" + "ix"  # an invented company name, split so a text scan of this file does not match it
DENY = f"""# invented test deny list
term A (?i){TERM}
kit  B \\bwidgetco\\b
kit  C (?i)\\bgo/[a-z]
path D .agents/skills/private-skill/*
allow system/vendor/* upstream-{TERM}
allowterm system/scripts/ok.py ^widgetco$
names E people
"""


class Fixture(unittest.TestCase):
    """A checkout in a temp dir with a deny list at local/kit-deny-list.txt."""

    files: dict[str, str] = {}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.deny_path = self.root / "local" / "kit-deny-list.txt"
        self.write("local/kit-deny-list.txt", DENY)
        self.write("people/ada-lovelace.md", "# Ada\n")
        for rel, text in self.files.items():
            self.write(rel, text)
        for patcher in (mock.patch.object(leaks, "ROOT", self.root),
                        mock.patch.object(leaks, "tracked", lambda: list(self.files))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def deny(self):
        return leaks.load_deny_list(self.deny_path)

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = leaks.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class InstallModeTests(Fixture):
    SKILL = ".agents/skills/private-skill/case-bank.md"
    files = {
        SKILL: f"{TERM.capitalize()} cases\n",
        "system/config/router.yaml": f"# after the {TERM} research tab closed\n",
        "system/scripts/tool.py": f"# {TERM.upper()} example\n",
        "system/scripts/clean.py": "# widgetco is a kit-only term\n",
    }

    def test_term_is_an_error_on_an_instruction_surface_and_a_warning_elsewhere(self) -> None:
        errors, warnings = leaks.scan(deny=self.deny())
        self.assertEqual([e["file"] for e in errors], ["system/config/router.yaml"])
        self.assertEqual([w["file"] for w in warnings], ["system/scripts/tool.py"])

    def test_kit_rules_never_run_on_an_install(self) -> None:
        errors, warnings = leaks.scan(deny=self.deny())
        self.assertNotIn("system/scripts/clean.py", [h["file"] for h in errors + warnings])

    def test_the_owners_machine_may_hold_a_denied_path(self) -> None:
        errors, warnings = leaks.scan(deny=self.deny())
        self.assertNotIn(self.SKILL, [h["file"] for h in errors + warnings])

    def test_no_deny_list_means_no_term_rules_and_says_so(self) -> None:
        self.deny_path.unlink()
        code, out, _ = self.main()
        self.assertEqual(code, 0)
        self.assertIn("no deny list", out)

    def test_the_script_names_no_company_term(self) -> None:
        # Wrong impl: keep a TERMS regex inline, which ships the company's names in the kit.
        text = (HERE / "check-profile-leaks.py").read_text(encoding="utf-8")
        self.assertNotIn("TERMS = re.compile", text)
        self.assertNotIn(TERM, text.lower())


class KitModeTests(Fixture):
    files = {
        "system/scripts/tool.py": f"# {TERM.upper()} example\n",
        "system/scripts/test_tool.py": "assert 'widgetco' in name\n",
        "system/scripts/ok.py": "x = 'widgetco'  # allowed here\n",
        "system/vendor/skills/x/SKILL.md": f"upstream-{TERM} icon\n",
        "docs/guide.md": "Read go/" + "handbook first. Ask Ada Lovelace.\n",  # split: the kit gate reads this file too
        "README.md": "Ada Lovelace-style naming is fine? No: ada_lovelace too\n",
    }

    def test_every_hit_anywhere_is_an_error_tests_included(self) -> None:
        errors, warnings = leaks.scan(kit=True, deny=self.deny())
        files = sorted({e["file"] for e in errors})
        self.assertEqual(files, ["README.md", "docs/guide.md", "system/scripts/test_tool.py", "system/scripts/tool.py"])
        self.assertEqual(warnings, [])

    def test_allow_rules_skip_only_their_own_hits(self) -> None:
        errors, _ = leaks.scan(kit=True, deny=self.deny())
        self.assertNotIn("system/scripts/ok.py", [e["file"] for e in errors])
        self.assertNotIn("system/vendor/skills/x/SKILL.md", [e["file"] for e in errors])
        # Wrong impl: an allow rule that skips the whole line also hides a second term on it.
        self.write("system/scripts/ok.py", f"x = 'widgetco' + '{TERM}'\n")
        errors, _ = leaks.scan(kit=True, deny=self.deny())
        self.assertIn("system/scripts/ok.py", [e["file"] for e in errors])

    def test_people_names_come_from_the_people_folder(self) -> None:
        errors, _ = leaks.scan(kit=True, deny=self.deny())
        hits = {e["file"]: e for e in errors if e["category"] == "E"}
        self.assertIn("README.md", hits)
        self.assertIn("docs/guide.md", [e["file"] for e in errors])

    def test_categories_are_counted(self) -> None:
        code, out, _ = self.main("--kit")
        self.assertEqual(code, 1)
        self.assertIn("by category:", out)
        self.assertIn("A ", out)

    def test_kit_gate_fails_closed_without_a_deny_list(self) -> None:
        # Wrong impl: run with no terms and pass, so a publisher without the list ships everything.
        self.deny_path.unlink()
        code, _, err = self.main("--kit")
        self.assertEqual(code, 2)
        self.assertIn("deny list not found", err)

    def test_an_unreadable_deny_list_fails_closed(self) -> None:
        self.write("local/kit-deny-list.txt", "term A (unclosed\n")
        code, _, err = self.main("--kit")
        self.assertEqual(code, 2)
        self.assertIn("bad regex", err)

    def test_explicit_list_and_root_gate_another_tree(self) -> None:
        tree = self.root / "built"
        (tree / "docs").mkdir(parents=True)
        (tree / "docs" / "a.md").write_text(f"{TERM}\n", encoding="utf-8")
        mock.patch.stopall()
        with mock.patch.object(leaks, "ROOT", self.root):
            out = io.StringIO()
            with redirect_stdout(out):
                code = leaks.main(["--kit", "--json", "--root", str(tree), "--deny-list", str(self.deny_path)])
        self.assertEqual(code, 1)
        self.assertEqual([e["file"] for e in json.loads(out.getvalue())["errors"]], ["docs/a.md"])


class KitPathTests(Fixture):
    files = {".agents/skills/private-skill/SKILL.md": "nothing to see\n",
             "local/skills/private-one/SKILL.md": "nothing to see\n",
             "system/config/router.yaml": "# clean\n"}

    def test_denied_and_owner_local_paths_fail_even_when_clean(self) -> None:
        errors, _ = leaks.scan(kit=True, deny=self.deny())
        self.assertEqual(sorted(e["file"] for e in errors),
                         [".agents/skills/private-skill/SKILL.md", "local/skills/private-one/SKILL.md"])

    def test_the_owners_machine_may_hold_them(self) -> None:
        errors, warnings = leaks.scan(deny=self.deny())
        self.assertEqual(errors + warnings, [])

    def test_a_tree_inside_another_checkouts_ignored_folder_is_walked(self) -> None:
        # Wrong impl: trust `git ls-files`, which lists nothing for a built tree under an ignored
        # scratch folder of the owner's checkout, so the gate would pass an unscanned tree.
        import subprocess
        mock.patch.stopall()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        tree = self.root / "scratch" / "built"
        (tree / "docs").mkdir(parents=True)
        (tree / "docs" / "a.md").write_text(f"{TERM}\n", encoding="utf-8")
        with mock.patch.object(leaks, "ROOT", tree):
            self.assertEqual(leaks.tracked(), ["docs/a.md"])
            errors, _ = leaks.scan(kit=True, deny=self.deny())
        self.assertEqual([e["file"] for e in errors], ["docs/a.md"])

    def test_a_kit_tree_with_no_git_is_walked_not_passed(self) -> None:
        mock.patch.stopall()
        with mock.patch.object(leaks, "ROOT", self.root):
            self.assertIn("system/config/router.yaml", leaks.tracked())
            errors, _ = leaks.scan(kit=True, deny=self.deny())
        self.assertIn(".agents/skills/private-skill/SKILL.md", [e["file"] for e in errors])


@unittest.skipIf(delta is None, "docs/kit-port is not in a kit tree")
class KitDeltaSkipTests(unittest.TestCase):
    def test_the_whole_skill_is_skipped_not_only_its_packs(self) -> None:
        # The owner-local skill folders come from the delta's own skip list, so no skill is named here.
        local_skills = [p for p in delta.SKIP_PREFIXES if p.startswith(".agents/skills/")]
        self.assertTrue(local_skills, "the skip list names at least one owner-local skill folder")
        for skill in local_skills:
            for rel in (f"{skill}SKILL.md", f"{skill}packs/x/pack.json", f"{skill}scripts/start-session.ps1"):
                self.assertTrue(rel.startswith(delta.SKIP_PREFIXES), rel)
        self.assertFalse(".agents/skills/drafting-docs/SKILL.md".startswith(delta.SKIP_PREFIXES))

    def test_private_skills_never_reach_the_delta(self) -> None:
        self.assertTrue("local/skills/private-one/SKILL.md".startswith(delta.SKIP_PREFIXES))


if __name__ == "__main__":
    unittest.main()
