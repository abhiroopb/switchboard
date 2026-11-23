#!/usr/bin/env python3
"""AGENTS.md stays under 40,000 characters (owner-approved trim, 2026-11-13, rt-000000000043).

AGENTS.md loads into every session, and Claude Code warns on instruction files over
about 40k characters. The limit holds for the file on disk and for the file with each
profile's section filled in by apply-profile.py, because a work machine may run a
company profile. When this fails, move detail to docs/runbooks/agents-md-detail.md and keep a
one-line rule in AGENTS.md; never raise the limit to make it pass.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("check-instruction-drift.py")
REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_instruction_drift_size", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RealAgentsMdTests(unittest.TestCase):
    def test_repo_agents_md_is_under_the_limit(self) -> None:
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text), MODULE.AGENTS_MD_CHAR_LIMIT, f"AGENTS.md is {len(text):,} characters")

    def test_every_profile_fill_is_under_the_limit(self) -> None:
        self.assertEqual([f.message for f in MODULE.agents_md_size_findings(REPO)], [])

    def test_limit_is_forty_thousand(self) -> None:
        self.assertEqual(MODULE.AGENTS_MD_CHAR_LIMIT, 40_000)

    def test_detail_runbook_exists(self) -> None:
        self.assertIn("docs/runbooks/agents-md-detail.md", (REPO / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((REPO / "docs" / "runbooks" / "agents-md-detail.md").is_file())


class SyntheticSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def base(self, size: int) -> str:
        tail = f"\n{MODULE.PROFILE_SECTION_START}\n{MODULE.PROFILE_SECTION_END}\n"
        return "# switchboard\n" + "x" * (size - len("# switchboard\n") - len(tail)) + tail

    def test_file_over_the_limit_is_an_error(self) -> None:
        self.write("AGENTS.md", self.base(40_001))
        findings = MODULE.agents_md_size_findings(self.root)
        self.assertEqual([f.severity for f in findings], ["error"])
        self.assertIn("40,001", findings[0].message)

    def test_file_at_the_limit_passes(self) -> None:
        self.write("AGENTS.md", self.base(40_000))
        self.assertEqual(MODULE.agents_md_size_findings(self.root), [])

    def test_a_profile_section_that_pushes_it_over_is_an_error(self) -> None:
        self.write("AGENTS.md", self.base(39_000))
        self.write("profiles/work/agents-section.md", "y" * 2_000)
        self.write("profiles/home/agents-section.md", "short")
        findings = MODULE.agents_md_size_findings(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("work profile", findings[0].message)

    def test_fill_matches_apply_profile(self) -> None:
        spec = importlib.util.spec_from_file_location("apply_profile_for_size", SCRIPT.with_name("apply-profile.py"))
        apply_profile = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(apply_profile)
        self.assertEqual(MODULE.PROFILE_SECTION_START, apply_profile.START)
        self.assertEqual(MODULE.PROFILE_SECTION_END, apply_profile.END)
        text = self.base(1_000)
        self.assertEqual(MODULE.fill_profile_section(text, "body\n"), apply_profile.fill_section(text, "body\n"))

    def test_run_checks_reports_the_size(self) -> None:
        self.write("AGENTS.md", self.base(40_500))
        messages = [f.message for f in MODULE.run_checks(self.root, self.root / "home")]
        self.assertTrue(any("over the 40,000 limit" in m for m in messages), messages)


if __name__ == "__main__":
    unittest.main()
