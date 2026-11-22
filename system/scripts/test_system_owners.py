#!/usr/bin/env python3
"""Regression tests for system_owners.py: the owner of the system a failure names.

Regression: a Slack DM naming a failed box was titled after the generic infra
workstream and scaffolded there, though another workstream owns the box and has
a live worker on it. Fixture: fixtures/routing_miss_placeholder_owner.json.

Run: python3 -m unittest system/scripts/test_system_owners.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))
import system_owners as so  # noqa: E402

FIXTURE = json.loads((SCRIPTS / "fixtures" / "routing_miss_placeholder_owner.json").read_text(encoding="utf-8"))
DM = FIXTURE["slack_text"]
PRIMARY = FIXTURE["primary"]
OWNER, BOX, INFRA = FIXTURE["owner"], "nightly-report-box", FIXTURE["wrong_owner"]
# The misrouted task as the ledger showed it while it sat in the generic workstream.
MISROUTED_ID = "rt-00000000006c"
MISROUTED = {"id": MISROUTED_ID, "status": "active", "workstream": INFRA, "tab_title": "🪄 VM 1",
             "created_at": "2026-01-01T01:00:00Z", "prompt": FIXTURE["prompt"],
             "summary": f"{BOX} refresh failed"}


class Tree:
    def __init__(self, root: Path) -> None:
        self.root = root

    def add(self, slug: str, context: str = "", config: str = "") -> None:
        folder = self.root / "workstreams" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "CONTEXT.md").write_text(f"# {slug}\n{context}")
        (folder / "config.yaml").write_text(f"name: {slug}\n{config}")


class IdentifierTests(unittest.TestCase):
    def test_the_dm_names_the_box_not_run_ids(self) -> None:
        ids = so.identifiers(DM)
        self.assertEqual(ids[0], BOX)
        self.assertFalse(any("2000" in i or "run-" in i for i in ids), ids)
        self.assertNotIn("robot_face", ids)

    def test_sites_repos_and_channels(self) -> None:
        text = ("see https://pages.example.com/acme-report-site/#scope=all and "
                "https://github.com/acme/acme-reports-repo/pull/5 in <#CXXXXXXXXXX|eval> or #team-reports-all")
        ids = so.identifiers(text)
        for expected in ("acme-report-site", "acme-reports-repo", "CXXXXXXXXXX".lower(), "team-reports-all"):
            self.assertIn(expected, ids)
        self.assertNotIn("pmos-example", so.identifiers("https://example.slack.com/archives/DXXXXXXXXXX/p1"))


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = Tree(self.root)
        self.tree.add(INFRA, f"VM {BOX} failed. " * 12, f"infra_only: true\ntodo_match_terms:\n- {INFRA}\n")
        self.tree.add("photo-book-kit", "Windows install report.\n")
        self.tree.add("pm-os-maintenance", f"{BOX} once.\n")

    def payload(self, *rows: dict) -> dict:
        return {"tasks": [dict(PRIMARY), dict(MISROUTED), *rows]}

    def test_declared_system_wins_over_the_generic_infra_workstream(self) -> None:
        # Wrong impl: the DM title picks the generic workstream.
        self.tree.add(OWNER, "", f"systems:\n  vm:\n    - {BOX}\n")
        owner = so.resolve(DM, self.root, self.payload(), exclude_task=MISROUTED_ID)
        self.assertEqual(owner["slug"], OWNER)
        self.assertIn(f'declares system "{BOX}" (config.yaml systems)', owner["evidence"])
        self.assertEqual(owner["primary"]["id"], PRIMARY["id"])
        self.assertEqual(owner["primary"]["tab_title"], PRIMARY["tab_title"])

    def test_live_worker_alone_resolves_the_owner(self) -> None:
        # No systems: and no CONTEXT mentions; the live row on the box names the owner.
        self.tree.add(OWNER)
        primary = dict(PRIMARY, prompt=f"Fix the {BOX} refresh on its vm")
        owner = so.resolve(DM, self.root, {"tasks": [primary, MISROUTED]}, exclude_task=MISROUTED_ID)
        self.assertEqual(owner["slug"], OWNER)
        self.assertTrue(any(e.startswith(f"a live {OWNER} worker touches") for e in owner["evidence"]))

    def test_context_mentions_resolve_the_owner(self) -> None:
        self.tree.add(OWNER, f"{BOX} runbook.\n" * 20)
        owner = so.resolve(DM, self.root, {"tasks": []})
        self.assertEqual(owner["slug"], OWNER)
        self.assertIsNone(owner["primary"])

    def test_infra_only_workstream_never_owns_a_box_by_mentions_or_rows(self) -> None:
        # The infra workstream has 12 mentions and a live row naming the box; it still is not the owner.
        owner = so.resolve(DM, self.root, {"tasks": [MISROUTED]})
        self.assertTrue(owner is None or owner["slug"] != INFRA, owner)

    def test_own_row_is_not_evidence(self) -> None:
        self.tree.add(OWNER)
        own = dict(MISROUTED, workstream="photo-book-kit")
        self.assertIsNone(so.resolve(DM, self.root, {"tasks": [own]}, exclude_task=MISROUTED_ID))

    def test_tie_and_nothing_named_are_none(self) -> None:
        self.tree.add("a-owner", "", "systems: [shared-box-name]\n")
        self.tree.add("b-owner", "", "systems: [shared-box-name]\n")
        self.assertIsNone(so.resolve("the shared-box-name refresh failed", self.root, {"tasks": []}))
        self.assertIsNone(so.resolve("Looks like it failed", self.root, {"tasks": []}))

    def test_secondary_primary_is_the_oldest_live_row(self) -> None:
        later = dict(PRIMARY, id="rt-later", created_at="2026-01-02T00:00:00Z", tab_title="🌐 Report Tooling 2")
        queued = dict(PRIMARY, id="rt-queued", status="queued", created_at="2025-12-01T00:00:00Z")
        done = dict(PRIMARY, id="rt-done", status="done", created_at="2025-11-01T00:00:00Z")
        row = so.primary_worker(OWNER, {"tasks": [later, queued, done, PRIMARY]}, "rt-new")
        self.assertEqual(row["id"], PRIMARY["id"])


# A copy without the owner workstream (a fresh install, the portable kit) has no real tree to check.
@unittest.skipUnless((REPO_ROOT / "workstreams" / FIXTURE["owner"] / "config.yaml").exists(),
                     "owner workstream not in this checkout")
class RealTreeTest(unittest.TestCase):
    def test_real_workstreams_resolve_the_dm_to_its_owner(self) -> None:
        owner = so.resolve(DM, REPO_ROOT, {"tasks": [PRIMARY, MISROUTED]}, exclude_task=MISROUTED_ID)
        self.assertEqual(owner["slug"], FIXTURE["owner"])
        self.assertEqual(owner["primary"]["id"], PRIMARY["id"])

    def test_real_box_owner_declares_the_box(self) -> None:
        cfg = so._config(REPO_ROOT / "workstreams" / FIXTURE["owner"] / "config.yaml")
        self.assertIn(BOX, so.declared_systems(cfg))


if __name__ == "__main__":
    unittest.main()
