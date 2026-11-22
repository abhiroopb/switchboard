#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("check-instruction-drift.py")
SPEC = importlib.util.spec_from_file_location("check_instruction_drift", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class InstructionDriftSyntheticTests(unittest.TestCase):
    def test_home_agents_accepts_one_pointer_line_per_repo_owned_section(self) -> None:
        """V7: ~/AGENTS.md carries the four repo-owned sections
        as one pointer line each. A pointer satisfies the check; a missing
        section, a heading plus a pointer, two pointers, or a restated calendar
        body outside the section are still flagged."""
        pointers = {
            "Control-Plane Ownership": "- Control-Plane Ownership: see `~/Development/pm-os/AGENTS.md`.\n",
            "Canonical Calendar Rule": "- Canonical Calendar Rule: see `~/Development/pm-os/AGENTS.md`.\n",
            "Perspective Check": "- Perspective Check: see `~/Development/pm-os/AGENTS.md`.\n",
            "Already-Answered Pre-Check": "- Already-Answered Pre-Check: see the repo AGENTS.md.\n",
        }
        calendar_body = " ".join(group[0] for group in MODULE.CANONICAL_CALENDAR_TOKEN_GROUPS)
        cases = {
            "all-pointers": ("".join(pointers.values()), []),
            "heading-still-full": (
                "## Canonical Calendar Rule\n" + calendar_body + "\n" + "".join(
                    text for name, text in pointers.items() if name != "Canonical Calendar Rule"
                ),
                [],
            ),
            "missing-one": ("".join(text for name, text in pointers.items() if name != "Perspective Check"), ["missing the Perspective Check"]),
            "heading-and-pointer": ("## Perspective Check\n- body\n" + "".join(pointers.values()), ["both a `## Perspective Check`"]),
            "two-pointers": ("".join(pointers.values()) + pointers["Perspective Check"], ["more than one pointer line for Perspective Check"]),
            "body-without-heading": ("".join(pointers.values()) + calendar_body + "\n", ["restates the canonical calendar-sync rule body"]),
            "heading-line-is-not-a-pointer": (
                "## Control-Plane Ownership (see `~/Development/pm-os/AGENTS.md`)\n" + "".join(
                    text for name, text in pointers.items() if name != "Control-Plane Ownership"
                ),
                [],
            ),
        }
        for name, (home_agents, expected) in cases.items():
            with self.subTest(name=name):
                findings = []
                MODULE._base_checks(
                    findings, Path("/repo"), Path("/home"), Path("/repo/system/config/runtime-adapter-map.yaml"),
                    repo_agents="", repo_claude="", home_claude="", todo_prefs="", behavior_map="",
                    todo_skill="", home_agents=home_agents, adapter_map={},
                )
                home_messages = [f.message for f in findings if f.surface == str(Path("/home") / "AGENTS.md")]
                self.assertEqual(len(home_messages), len(expected), (name, home_messages))
                for fragment in expected:
                    self.assertTrue(any(fragment in message for message in home_messages), (name, fragment, home_messages))

    def test_missing_browser_and_review_contracts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            (repo / "AGENTS.md").write_text("incomplete policy\n")

            findings = []
            MODULE._expanded_contract_checks(findings, repo, home, set(MODULE.ROUTE_REPLACEMENTS.values()))

            messages = [finding.message for finding in findings]
            self.assertTrue(any("browser automation contract" in message for message in messages))
            self.assertTrue(any("Obsidian-first review contract" in message for message in messages))

    def test_catalog_combines_repo_global_and_runtime_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            home = root / "home"
            (repo / ".agents/skills/repo-skill").mkdir(parents=True)
            (repo / ".agents/skills/repo-skill/SKILL.md").write_text("---\nname: repo-skill\n---\n")
            (home / ".agents/skills/global-skill").mkdir(parents=True)
            (home / ".agents/skills/global-skill/SKILL.md").write_text("---\nname: global-skill\n---\n")

            catalog = MODULE.effective_skill_catalog(repo, home, {"builtin-skill"})

            self.assertEqual(catalog, {"repo-skill", "global-skill", "builtin-skill"})

    def test_obsolete_routes_are_reported_without_marking_builtin_replacement_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            home = root / "home"
            route = home / ".agents/skills/auto-pilot/references/routing-tables.md"
            route.parent.mkdir(parents=True)
            route.write_text("| Calendar review | `reviewing-calendar` |\n")
            (repo / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
            (repo / "AGENTS.md").write_text("\n".join(MODULE.BROWSER_CONTRACT_TOKENS + MODULE.REVIEW_CONTRACT_TOKENS))

            findings = []
            MODULE._expanded_contract_checks(
                findings,
                repo,
                home,
                {"gcal", "chief-of-staff", "linear"},
            )

            messages = [finding.message for finding in findings]
            self.assertTrue(any("obsolete skill `reviewing-calendar`" in message for message in messages))
            self.assertFalse(any("Replacement skill `gcal` is absent" in message for message in messages))

    def test_external_adapter_conflicts_are_attributed_to_external_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            home = root / "home"
            (repo / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
            (repo / "AGENTS.md").write_text(
                "\n".join(MODULE.BROWSER_CONTRACT_TOKENS + MODULE.REVIEW_CONTRACT_TOKENS)
                + f"\n{MODULE.ATTRIBUTION_SECTION_HEADING}\n- `:robot_face:`\n"
            )
            (home / ".codex").mkdir(parents=True)
            (home / ".codex/AGENTS.md").write_text("prefixing with a 🤖")

            findings = []
            MODULE._expanded_contract_checks(findings, repo, home, set(MODULE.ROUTE_REPLACEMENTS.values()))

            self.assertEqual(len(findings), 1)
            self.assertTrue(all("external owner" in finding.message for finding in findings))

    def test_bot_prefix_rule_must_live_in_repo_agents_and_not_be_restated(self) -> None:
        """The bare `:robot_face:` line is the only prefix; the
        repo AGENTS.md section is canonical and adapters may not keep the old text."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir(parents=True)
            (repo / "AGENTS.md").write_text("\n".join(MODULE.BROWSER_CONTRACT_TOKENS + MODULE.REVIEW_CONTRACT_TOKENS))
            (home / ".claude").mkdir(parents=True)
            (home / ".claude/CLAUDE.md").write_text(
                "prefixing with a 🤖\n\nUse `:robot_face:` on its own line.\n"
                "Hi! :robot_face: This is the owner's AI Bot. Posting on their behalf:\n"
            )

            findings = []
            MODULE._expanded_contract_checks(findings, repo, home, set(MODULE.ROUTE_REPLACEMENTS.values()))

            messages = [finding.message for finding in findings]
            self.assertTrue(any(MODULE.ATTRIBUTION_SECTION_HEADING in message and "single source of truth" in message for message in messages))
            # The adapter carries the new marker, so no "generic robot attribution" warning...
            self.assertFalse(any("generic robot attribution" in message for message in messages))
            # ...but the retired wording is still flagged.
            self.assertTrue(any("retired bot wording" in message for message in messages))

    def test_bot_prefix_standalone_line_placement_is_flagged_but_negated_mentions_are_not(self) -> None:
        """The prefix opens the first sentence on the same line.
        An adapter prescribing the retired standalone-line shape is flagged; one
        that only says "never stands alone on its own line" is not."""
        cases = {
            "prescribes": ("starts with `:robot_face:` alone on the first line, body on the next line.\n", True),
            "prescribes-own": ("the image `:robot_face:` alone on its own line\n", True),
            "negated": ("`:robot_face: First word…` on the same line. It never stands alone on its own line.\n", False),
            "no-marker": ("the robot alone on the first line\n", False),
        }
        for name, (adapter_text, expect_flag) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = root / "repo"
                home = root / "home"
                repo.mkdir(parents=True)
                (repo / "AGENTS.md").write_text(
                    "\n".join(MODULE.BROWSER_CONTRACT_TOKENS + MODULE.REVIEW_CONTRACT_TOKENS)
                    + f"\n{MODULE.ATTRIBUTION_SECTION_HEADING}\n"
                )
                (home / ".claude").mkdir(parents=True)
                (home / ".claude/CLAUDE.md").write_text(adapter_text)

                findings = []
                MODULE._expanded_contract_checks(findings, repo, home, set(MODULE.ROUTE_REPLACEMENTS.values()))

                flagged = any("alone on its own line" in finding.message for finding in findings)
                self.assertEqual(flagged, expect_flag, (name, [f.message for f in findings]))


class OwnerBrowserRuleCases(unittest.TestCase):
    """The switch and the written rule must agree. Throwaway folders, invented machine."""

    def run_check(self, env_line, rule):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PM_OS_MACHINE": "INVENTEDBOX"}):
            repo = Path(tmp)
            (repo / "local" / "machines").mkdir(parents=True)
            (repo / "local" / "machines" / "INVENTEDBOX.env").write_text(env_line)
            (repo / "local" / "agents-local.md").write_text(rule)
            return MODULE.owner_browser_rule_findings(repo)

    def test_switch_off_needs_no_rule(self):
        self.assertEqual(self.run_check("PM_OS_COMMS_TRIAGE=off\n", ""), [])
        self.assertEqual(self.run_check("PM_OS_OWNER_BROWSER=off\n", ""), [])

    def test_switch_on_without_a_rule_is_an_error(self):
        findings = self.run_check("PM_OS_OWNER_BROWSER=brave\n", "")
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("Scheduled jobs do not use", findings[0].message)

    def test_rule_missing_one_guard_is_named(self):
        tokens = [t for t in MODULE.OWNER_BROWSER_RULE_TOKENS if t != "Never buy, pay, transfer"]
        findings = self.run_check("PM_OS_OWNER_BROWSER=brave\n", "On INVENTEDBOX: " + " ".join(tokens))
        self.assertIn("Never buy, pay, transfer", findings[0].message)

    def test_rule_must_name_the_machine(self):
        findings = self.run_check("PM_OS_OWNER_BROWSER=brave\n", " ".join(MODULE.OWNER_BROWSER_RULE_TOKENS))
        self.assertIn("INVENTEDBOX", findings[0].message)

    def test_complete_rule_passes(self):
        rule = "On INVENTEDBOX: " + " ".join(MODULE.OWNER_BROWSER_RULE_TOKENS)
        self.assertEqual(self.run_check("PM_OS_OWNER_BROWSER=brave\n", rule), [])


if __name__ == "__main__":
    unittest.main()
