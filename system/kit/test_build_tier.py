#!/usr/bin/env python3
"""Tests for build_tier.py and scan_public.py. All names, terms and paths are invented.

    cd system/kit && python3 -m unittest -q test_build_tier
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_tier as bt  # noqa: E402
import scan_public as sp  # noqa: E402


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    return subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True, env=env).stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


class RulesTests(unittest.TestCase):
    def test_every_rule_kind_parses(self) -> None:
        rules = bt.parse_rules(
            "# comment\nexclude secret/\ndrop_on_hit notes/\nrewrite Zorblax => Widget\nrewrite \\s*\\(drop me\\) =>\n"
            "deny Q (?i)quuxcorp\ndeny_list lists/extra.txt\nskip_category O\nrename old/a.py => new/a.py\nkeep secret/ok.md\n",
            Path("/base"), "t")
        self.assertEqual(rules.exclude, ["secret/"])
        self.assertEqual(rules.drop_on_hit, ["notes/"])
        self.assertEqual([r for _, r in rules.rewrites], ["Widget", ""])
        self.assertEqual(rules.deny[0][0], "Q")
        self.assertEqual(rules.deny_lists[0].as_posix(), "/base/lists/extra.txt")
        self.assertEqual(rules.skip_categories, {"O"})
        self.assertEqual(rules.renames, {"old/a.py": "new/a.py"})
        self.assertEqual(rules.keep, ["secret/ok.md"])

    def test_unknown_kind_and_bad_regex_fail(self) -> None:
        with self.assertRaises(bt.RulesError):
            bt.parse_rules("frobnicate x\n", Path("."), "t")
        with self.assertRaises(bt.RulesError):
            bt.parse_rules("rewrite ( => x\n", Path("."), "t")


class FilterTests(unittest.TestCase):
    def test_excludes_keep_private_workstreams_renames_and_rewrites(self) -> None:
        tree = {
            "a.md": b"Zorblax lives here\n",
            "local/x.txt": b"never\n",
            "system/state/ledger.json": b"{}",
            "system/state/README.md": b"kept\n",
            "system/config/private-workstreams.txt": b"# private\nvault-topic\n",
            "workstreams/vault-topic/CONTEXT.md": b"status only\n",
            "workstreams/vault-topic/balances.md": b"never\n",
            "workstreams/open-topic/notes.md": b"fine\n",
            "old/tool.py": b"print('Zorblax')\n",
        }
        cfg = {"exclude": ["local/", "system/state/"], "keep": ["system/state/README.md"],
               "exclude_private_workstreams": True, "private_workstream_keep": ["CONTEXT.md", "config.yaml"]}
        rules = bt.parse_rules("rewrite Zorblax => Widget\nrename old/tool.py => new/tool.py\n", Path("."), "t")
        out = bt.filter_tree(tree, cfg, rules)
        self.assertEqual(sorted(out), ["a.md", "new/tool.py", "system/config/private-workstreams.txt",
                                       "system/state/README.md", "workstreams/open-topic/notes.md",
                                       "workstreams/vault-topic/CONTEXT.md"])
        self.assertEqual(out["a.md"], b"Widget lives here\n")
        self.assertEqual(out["new/tool.py"], b"print('Widget')\n")

    def test_third_party_files_are_never_rewritten(self) -> None:
        rules = bt.parse_rules("rewrite Zorblax => Widget\n", Path("."), "t")
        out = bt.filter_tree({"system/vendor/skills/x/SKILL.md": b"Zorblax\n", "a.md": b"Zorblax\n"}, {}, rules)
        self.assertEqual(out["system/vendor/skills/x/SKILL.md"], b"Zorblax\n")
        self.assertEqual(out["a.md"], b"Widget\n")

    def test_scoped_rewrites_touch_only_matching_files(self) -> None:
        rules = bt.parse_rules("rewrite_in tools/*.ps1 (?m)^.*zorb-job.*\\n =>\n", Path("."), "t")
        self.assertEqual(len(rules.scoped_rewrites), 1)
        tree = {"tools/run.ps1": b"keep\n  zorb-job here\nkeep2\n", "a.md": b"zorb-job stays\n"}
        out = bt.filter_tree(tree, {}, rules)
        self.assertEqual(out["tools/run.ps1"], b"keep\nkeep2\n")
        self.assertEqual(out["a.md"], b"zorb-job stays\n")
        with self.assertRaises(bt.RulesError):
            bt.parse_rules("rewrite_in onlyglob\n", Path("."), "t")
        merged = bt.merge_rules(rules, bt.parse_rules("rewrite x => y\n", Path("."), "u"))
        self.assertEqual(len(merged.scoped_rewrites), 1)

    def test_binary_files_are_never_rewritten(self) -> None:
        rules = bt.parse_rules("rewrite Zorblax => Widget\n", Path("."), "t")
        out = bt.filter_tree({"img.png": b"\x89PNG\0Zorblax"}, {}, rules)
        self.assertEqual(out["img.png"], b"\x89PNG\0Zorblax")


class MergeTests(unittest.TestCase):
    def test_merge_cases(self) -> None:
        base = {"same.txt": b"1\n", "edited-upstream.txt": b"a\nb\nc\nd\ne\n", "both.txt": b"a\nb\nc\nd\ne\n",
                "gone-upstream.txt": b"x\n", "gone-but-edited.txt": b"x\n", "removed-by-tier.txt": b"v1\n",
                "clash.txt": b"one\n"}
        ours = {"same.txt": b"1\n", "edited-upstream.txt": b"a\nb\nc\nd\ne\n", "both.txt": b"A\nb\nc\nd\ne\n",
                "gone-upstream.txt": b"x\n", "gone-but-edited.txt": b"y\n", "clash.txt": b"ours\n",
                "tier-only.md": b"README\n"}
        theirs = {"same.txt": b"1\n", "edited-upstream.txt": b"a\nb\nc\nd\nE\n", "both.txt": b"a\nb\nc\nd\nE\n",
                  "removed-by-tier.txt": b"v2\n", "clash.txt": b"theirs\n", "new.txt": b"new\n"}
        tree, rep = bt.merge_trees(base, ours, theirs)
        self.assertEqual(tree["edited-upstream.txt"], b"a\nb\nc\nd\nE\n")
        self.assertEqual(tree["both.txt"], b"A\nb\nc\nd\nE\n")
        self.assertEqual(tree["tier-only.md"], b"README\n")
        self.assertEqual(tree["new.txt"], b"new\n")
        self.assertNotIn("gone-upstream.txt", tree)
        self.assertNotIn("removed-by-tier.txt", tree, "a file the tier removed on purpose stays out")
        self.assertEqual(sorted(rep.conflicts), ["clash.txt", "gone-but-edited.txt"])
        self.assertEqual(rep.upstream_change_to_removed, ["removed-by-tier.txt"])
        self.assertEqual(rep.deleted, ["gone-upstream.txt"])

    def test_source_policy_settles_hunks_and_keeps_other_tier_edits(self) -> None:
        base = {"f.txt": b"a\nb\nc\nd\ne\nf\ng\n"}
        ours = {"f.txt": b"A\nb\nc\nd\ne\nf\ntier\n"}
        theirs = {"f.txt": b"a\nb\nc\nd\ne\nf\nsource\n"}
        tree, rep = bt.merge_trees(base, ours, theirs)
        self.assertEqual(rep.conflicts, ["f.txt"])
        tree, rep = bt.merge_trees(base, ours, theirs, "source")
        self.assertEqual(tree["f.txt"], b"A\nb\nc\nd\ne\nf\nsource\n")
        self.assertEqual(rep.conflicts, [])
        self.assertEqual(rep.settled_for_source, ["f.txt"])
        tree, rep = bt.merge_trees(base, ours, theirs, "source", ["f.*"])
        self.assertEqual(tree["f.txt"], b"A\nb\nc\nd\ne\nf\ntier\n")
        self.assertEqual(rep.settled_for_tier, ["f.txt"])

    def test_adapter_indexes_list_only_what_ships(self) -> None:
        ai = bt._load_module("adapter_indexes", HERE / "adapter_indexes.py")
        tree = {"a.py": b"print(1)\n", "system/amp-plugins/x.ts": b"x\n",
                ".agents/skills/demo/SKILL.md": b"---\nname: demo\ndescription: >\n  Does a demo.\n---\nbody\n",
                "adapters/portability.json": b'{"files": {"gone.py": {"class": "portable"}, "a.py": {"class": "adapter"}}}'}
        ai.regenerate(tree)
        data = json.loads(tree["adapters/portability.json"])
        self.assertNotIn("gone.py", data["files"])
        self.assertEqual(data["files"]["a.py"]["class"], "adapter", "a known file keeps its class")
        self.assertEqual(data["files"]["system/amp-plugins/x.ts"]["class"], "amp-only")
        self.assertIn(b"| `demo` | Does a demo. |", tree["adapters/SKILLS.md"])
        self.assertIn(b"files. Counts:", tree["adapters/PORTABILITY.md"])

    def test_overlay_lands_at_the_root(self) -> None:
        tree = {"README.md": b"old\n"}
        written = bt.apply_overlay(tree, {"kit/overlay/README.md": b"new\n", "kit/overlay/docs/a.md": b"a\n"},
                                   ["kit/overlay/"])
        self.assertEqual(tree["README.md"], b"new\n")
        self.assertEqual(sorted(written), ["README.md", "docs/a.md"])


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "local").mkdir()
        (self.root / "local" / "deny.txt").write_text(
            "kit A (?i)quuxcorp\nkit O (?i)\\bpat zorblax\\b\npath A secret/*\nallow docs/* (?i)quuxcorp-free\n",
            encoding="utf-8")

    def test_deny_hits_content_paths_allows_and_skipped_categories(self) -> None:
        tree = {"a.md": b"made at QuuxCorp\n", "secret/x.md": b"fine\n", "docs/b.md": b"a quuxcorp-free design\n",
                "c.md": b"by Pat Zorblax\n", "img.bin": b"\0quuxcorp"}
        hits = bt.deny_hits(tree, [self.root / "local" / "deny.txt"], [], set(), self.root)
        self.assertEqual(sorted((h["file"], h["category"]) for h in hits),
                         [("a.md", "A"), ("c.md", "O"), ("img.bin", "BIN"), ("secret/x.md", "A")])
        skipped = bt.deny_hits(tree, [self.root / "local" / "deny.txt"], [], {"O"}, self.root)
        self.assertNotIn("c.md", {h["file"] for h in skipped})

    def test_run_gates_drops_listed_folders_and_fails_on_the_rest(self) -> None:
        cfg = {"tiers": {"t": {"gates": ["deny", "public"], "deny_lists": ["local/deny.txt"],
                               "drop_on_hit": ["notes/"]}}}
        tree = {"notes/day.md": b"QuuxCorp standup\n", "ok.md": b"fine\n", "code.py": b"# mail jo@realmail.net\n"}
        result = bt.run_gates(tree, "t", cfg, bt.Rules(), self.root)
        self.assertEqual(result["dropped"], ["notes/day.md"])
        self.assertNotIn("notes/day.md", tree)
        self.assertFalse(result["passed"])
        self.assertEqual(result["gates"]["public"]["hits"], [{"file": "code.py", "line": 1, "kind": "email"}])

    def test_other_encodings_are_still_scanned(self) -> None:
        tree = {"u16.md": "made at QuuxCorp\n".encode("utf-16"), "cp.md": "café made at QuuxCorp\n".encode("cp1252"),
                "smart.md": b"made at QuuxCorp \x92quoted\x92\n"}
        hits = bt.deny_hits(tree, [self.root / "local" / "deny.txt"], [], set(), self.root)
        self.assertEqual(sorted(h["file"] for h in hits), ["cp.md", "smart.md", "u16.md"])

    def test_anchored_rules_and_a_second_match_on_a_line(self) -> None:
        (self.root / "local" / "deny2.txt").write_text(
            "kit A (?i)^zorblax\nkit A (?i)quuxcorp\nallowterm * (?i)^quuxcorpfree$\n", encoding="utf-8")
        tree = {"a.md": b"line one\nZorblax plan\n", "b.md": b"quuxcorpfree and also QuuxCorp\n"}
        hits = bt.deny_hits(tree, [self.root / "local" / "deny2.txt"], [], set(), self.root)
        self.assertEqual(sorted((h["file"], h["line"]) for h in hits), [("a.md", 2), ("b.md", 1)])

    def test_unknown_or_missing_gates_fail(self) -> None:
        for tier in ({}, {"gates": ["denny"]}, {"gates": ["deny"], "deny_lists": ["local/deny.txt"], "visibility": "PRIVATE"}):
            with self.subTest(tier=tier):
                result = bt.run_gates({"a.md": b"x\n"}, "t", {"tiers": {"t": tier}}, bt.Rules(), self.root)
                self.assertFalse(result["passed"])

    def test_a_path_that_names_a_term_is_withheld(self) -> None:
        cfg = {"tiers": {"t": {"gates": ["deny"], "deny_lists": ["local/deny.txt"]}}}
        result = bt.run_gates({"docs/quuxcorp-plan.md": b"fine\n"}, "t", cfg, bt.Rules(), self.root)
        self.assertFalse(result["passed"])
        self.assertNotIn("quuxcorp", json.dumps(result).lower())

    def test_a_missing_deny_list_fails_closed(self) -> None:
        cfg = {"tiers": {"t": {"gates": ["deny"], "deny_lists": ["local/nope.txt"]}}}
        result = bt.run_gates({"a.md": b"x\n"}, "t", cfg, bt.Rules(), self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["gates"]["deny"]["status"], "error")


class PublicScanTests(unittest.TestCase):
    CASES = {
        "carries PATH/HOME/locale": [], "profiles/home/agents-section.md": [],
        "see /home/realguy/x": ["home-path"], "C:\\Users\\realguy\\x": ["home-path"], "C:/Users/you/x": [],
        "cdn x/y@6.0.1/dist": [], "jane.doe@gmail.com": ["email"], "pat@example.com": [],
        "call 312-555-2671": ["phone"], "sizes 1024 1440 768": [],
        'x (the owner' + ', 2026-11-02: "quote")': ["owner-ruling"],
        "U0123456789 and W0AAAAAAAA": [], "user U07ABC12DEF here": ["chat-id"],
        "/c/Users/realguy/x": ["home-path"], "/mnt/c/Users/realguy/x": ["home-path"],
        "C:/Users/jane_doe/x": ["home-path"], "call 312 867 5309": ["phone"], "fake 555-0100 real 312-867-5309": ["phone"],
        "+44 20 7946 0958": ["phone"], "owner@realcorp.com": ["email"], "x = 1  # gitleaks:allow": ["scanner-suppression"],
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit": ["doc-id"],
        "https://docs.google.com/document/d/YOUR_DOC_TEMPLATE_ID/edit": [],
        "server 8.8.4.4 and 10.0.0.1 and 203.0.113.9": ["ip"],
    }

    def test_line_detectors(self) -> None:
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(sp.line_hits(text), want)

    def test_image_metadata(self) -> None:
        import struct, zlib
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13) + chunk(b"tEXt", b"Author\0Someone") + chunk(b"IEND", b"")
        self.assertEqual(sp.image_metadata("a.png", png), ["png tEXt"])
        clean = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13) + chunk(b"IEND", b"")
        self.assertEqual(sp.image_metadata("a.png", clean), [])
        self.assertEqual(sp.scan_tree({"a.png": png}, allow={"image": ["*.png"]}), [])

    def test_paths_and_utf16_are_scanned(self) -> None:
        hits = sp.scan_tree({"docs/jane.doe@gmail.com.md": b"x\n", "a.txt": "mail jo@realmail.net\n".encode("utf-16")})
        self.assertEqual(sorted((h["file"], h["kind"]) for h in hits),
                         [("a.txt", "email"), ("docs/jane.doe@gmail.com.md", "email")])

    def test_hits_never_carry_the_value(self) -> None:
        hits = sp.scan_tree({"a.md": b"write to jo@realmail.net\n"})
        self.assertNotIn("realmail", json.dumps(hits))


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = self.tmp / "src"
        self.out = self.tmp / "out"
        self.src.mkdir()
        git(self.src, "init", "-q", "-b", "main")
        write(self.src, {
            "README.md": "internal readme\n", "core.py": "x = 1\n\n\n\ny = 2\n", "notes/day.md": "private\n",
            "kit/overlay/README.md": "# Public readme\n", "local/deny.txt": "kit A (?i)quuxcorp\n",
            "run.cmd": "@echo off\nrem x\n",
        })
        (self.src / ".gitignore").write_text("local/\n", encoding="utf-8")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.cfg = self.tmp / "tiers.yaml"
        self.cfg.write_text(json.dumps({"tiers": {"public": {
            "exclude": ["notes/"], "overlay": ["kit/overlay/"], "drop_after_overlay": ["kit/"],
            "deny_lists": ["local/deny.txt"], "gates": ["deny", "public"],
            "author": {"name": "Neutral", "email": "neutral@users.noreply.github.com"},
            "crlf": ["*.cmd"], "version_file": "KIT_VERSION", "manifest": True}}}), encoding="utf-8")

    def build(self, *extra: str) -> int:
        return bt.main(["--config", str(self.cfg), "build", "--tier", "public", "--out", str(self.out),
                        "--source", str(self.src), "--owner-root", str(self.src), *extra])

    def test_first_build_then_a_merge_that_keeps_a_hand_edit(self) -> None:
        self.assertEqual(self.build("--commit"), 0)
        self.assertEqual((self.out / "README.md").read_text(encoding="utf-8"), "# Public readme\n")
        self.assertFalse((self.out / "notes").exists())
        self.assertFalse((self.out / "kit").exists())
        self.assertEqual(git(self.out, "log", "-1", "--format=%an <%ae>"), "Neutral <neutral@users.noreply.github.com>")
        manifest = json.loads((self.out / ".switchboard-kit" / "source-manifest.json").read_text(encoding="utf-8"))
        import hashlib
        for rel, meta in manifest["files"].items():  # what install.py checks before it renders
            self.assertEqual(hashlib.sha256((self.out / rel).read_bytes()).hexdigest(), meta["sha256"], rel)
        self.assertEqual(manifest["files"]["README.md"]["source"], "overlay")
        self.assertTrue((self.out / "KIT_VERSION").read_text(encoding="utf-8").startswith("build "))
        self.assertEqual((self.out / "run.cmd").read_bytes(), b"@echo off\r\nrem x\r\n")
        self.assertFalse((self.out / ".switchboard-kit" / "tier.json").exists(), "the build record never ships")
        record = json.loads((self.src / "local" / "kit" / "public.record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["source_rev"], git(self.src, "rev-parse", "HEAD"))
        self.assertEqual(record["commit"], git(self.out, "rev-parse", "HEAD"))
        # A hand edit in the public copy, then an upstream change to another hunk of the same file.
        (self.out / "core.py").write_text("x = 100\n\n\n\ny = 2\n", encoding="utf-8")
        git(self.out, "commit", "-qam", "hand edit")
        (self.src / "core.py").write_text("x = 1\n\n\n\ny = 3\n", encoding="utf-8")
        git(self.src, "commit", "-qam", "two")
        self.assertEqual(self.build("--commit"), 0)
        self.assertEqual((self.out / "core.py").read_text(encoding="utf-8"), "x = 100\n\n\n\ny = 3\n")

    def test_a_new_rule_reaches_an_unchanged_line_and_keeps_hand_edits(self) -> None:
        cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        cfg["tiers"]["public"]["rules"] = "local/r.rules"
        self.cfg.write_text(json.dumps(cfg), encoding="utf-8")
        (self.src / "local" / "r.rules").write_text("# none yet\n", encoding="utf-8")
        self.assertEqual(self.build("--commit"), 0)
        self.assertTrue((self.src / "local" / "kit" / "public.last-rules" / "owner.rules").is_file())
        (self.out / "core.py").write_text("x = 100\n\n\n\ny = 2\n", encoding="utf-8")
        git(self.out, "commit", "-qam", "hand edit")
        # Only the rules change: the source is the same revision.
        (self.src / "local" / "r.rules").write_text("rewrite_in core.py y = 2 => y = 20\n", encoding="utf-8")
        self.assertEqual(self.build("--commit"), 0)
        self.assertEqual((self.out / "core.py").read_text(encoding="utf-8"), "x = 100\n\n\n\ny = 20\n")

    def test_a_gate_failure_writes_nothing(self) -> None:
        self.assertEqual(self.build("--commit"), 0)
        head = git(self.out, "rev-parse", "HEAD")
        (self.src / "leak.md").write_text("built at QuuxCorp\n", encoding="utf-8")
        git(self.src, "add", "leak.md")
        git(self.src, "commit", "-qm", "leak")
        self.assertEqual(self.build("--commit"), 1)
        self.assertFalse((self.out / "leak.md").exists())
        self.assertEqual(git(self.out, "rev-parse", "HEAD"), head)

    def test_skip_gate_never_commits(self) -> None:
        self.assertEqual(self.build("--commit", "--skip-gate", "public"), 0)
        self.assertFalse((self.out / ".git").exists())
        bt.SKIP_GATES.clear()

    def test_push_is_plain_and_only_main(self) -> None:
        self.assertEqual(self.build("--commit"), 0)
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.out, "remote", "add", "origin", str(remote))
        push = ["--config", str(self.cfg), "push", "--tier", "public", "--out", str(self.out),
                "--skip-visibility-check", "--owner-root", str(self.src)]
        self.assertEqual(bt.main(push), 0)
        self.assertEqual(git(remote, "rev-parse", "main"), git(self.out, "rev-parse", "HEAD"))
        # A hand commit that leaks is refused at push time, even though no build made it.
        (self.out / "late.md").write_text("added later for QuuxCorp\n", encoding="utf-8")
        git(self.out, "add", "late.md")
        git(self.out, "commit", "-qm", "late")
        self.assertEqual(bt.main(push), 4)
        git(self.out, "reset", "-q", "--hard", "HEAD~1")
        # A second push URL, or a remote URL with a skip flag, is refused.
        git(self.out, "remote", "set-url", "--add", "--push", "origin", str(remote))
        git(self.out, "remote", "set-url", "--add", "--push", "origin", str(self.tmp / "other.git"))
        self.assertEqual(bt.main(push), 2)
        git(self.out, "remote", "set-url", "origin", "https://github.com/someone/else.git")
        git(self.out, "config", "--unset-all", "remote.origin.pushurl")
        self.assertEqual(bt.main(push), 3)
        git(self.out, "remote", "set-url", "origin", str(remote))
        git(self.out, "switch", "-q", "-c", "other")
        self.assertEqual(bt.main(push), 3)

    def test_the_commit_message_is_gated(self) -> None:
        self.assertEqual(self.build("--commit", "--message", "made for QuuxCorp"), 1)
        self.assertFalse((self.out / ".git").exists())

    def test_guard_rails_on_paths(self) -> None:
        self.assertEqual(self.build("--keep-failed", str(self.out / "inside")), 2)
        self.assertEqual(self.build("--keep-failed", str(self.src / "kit")), 2, "a tracked source folder")
        self.assertEqual(self.build("--keep-failed", str(self.tmp)), 2, "a folder that holds the clone")
        override = self.tmp / "override"
        write(override, {"notes/day.md": "hand file on an excluded path\n"})
        self.assertEqual(self.build("--override", str(override)), 2)
        self.assertEqual(self.build("--commit"), 0)
        (self.out / "core.py").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(self.build("--commit"), 2, "a dirty clone is never overwritten")


if __name__ == "__main__":
    unittest.main()
