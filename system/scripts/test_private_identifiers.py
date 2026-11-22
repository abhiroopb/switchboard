#!/usr/bin/env python3
"""Tests for the private identifier guard. Every value here is invented.

Run from system/scripts:  python3 -m unittest test_private_identifiers
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PM_OS_TEST_SANDBOX", "1")
import dispatch  # noqa: E402
import private_identifiers as pi  # noqa: E402

FAKE_ACCOUNT = "90817-263544"
FAKE_STREET = "7 Invented Lark Avenue"
FAKE_ID = "Q9081726X"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True)
    return done.stdout


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.md").write_text("start\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("/local/\n/.state/\n/.amp/\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "start")
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PM_OS_PRIVATE_IDENTIFIERS", None)
        self.addCleanup(self.tmp.cleanup)

    def add_list(self) -> None:
        pi.add_values(self.repo, "account", [FAKE_ACCOUNT])
        pi.add_values(self.repo, "street", [FAKE_STREET])
        pi.add_values(self.repo, "national_id", [FAKE_ID])

    def commit(self, name: str, text: str, message: str = "change") -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)

    def base(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").strip()

    def verdict(self, base: str) -> dict:
        return pi.guard_range(self.repo, f"{base}..HEAD")


class ListFormat(RepoCase):
    def test_list_holds_no_plaintext(self) -> None:
        self.add_list()
        text = pi.list_path(self.repo).read_text(encoding="utf-8").casefold()
        for value in (FAKE_ACCOUNT, pi.normalize(FAKE_ACCOUNT), "lark", pi.normalize(FAKE_STREET), FAKE_ID.casefold()):
            self.assertNotIn(value.casefold(), text)

    def test_two_lists_use_different_salts(self) -> None:
        self.add_list()
        first = pi.list_path(self.repo).read_text(encoding="utf-8")
        pi.list_path(self.repo).unlink()
        self.add_list()
        self.assertNotEqual(first, pi.list_path(self.repo).read_text(encoding="utf-8"))

    def test_ordinary_values_are_rejected(self) -> None:
        added, rejected = pi.add_values(self.repo, "account", ["123456", "Plain Words Here"])
        self.assertEqual((added, rejected), (0, 2))

    def test_adding_twice_keeps_one_entry(self) -> None:
        pi.add_values(self.repo, "account", [FAKE_ACCOUNT])
        added, _ = pi.add_values(self.repo, "account", [FAKE_ACCOUNT.replace("-", " ")])
        self.assertEqual(added, 0)


class Matching(RepoCase):
    def listed(self) -> pi.IdentifierList:
        self.add_list()
        return pi.load_list(self.repo)

    def test_formats_of_one_value_all_match(self) -> None:
        listed = self.listed()
        for text in ("90817-263544", "90817263544", "90817 263544", "acct90817263544", "id: 90817.263544,"):
            self.assertEqual(listed.kinds_in(text), {"account"}, text)
        for text in ("7 Invented Lark Avenue", "7 invented lark avenue", "/docs/7-invented-lark-avenue/a.pdf"):
            self.assertEqual(listed.kinds_in(text), {"street"}, text)
        self.assertEqual(listed.kinds_in(f"x {FAKE_ID.lower()} y"), {"national_id"})

    def test_near_misses_do_not_match(self) -> None:
        listed = self.listed()
        for text in ("90817-263545", "990817263544", "263544", "8 Invented Lark Avenue", "Q9081726Y", "1700000000.000100"):
            self.assertEqual(listed.kinds_in(text), set(), text)


FAKE_SSID = "Zorblaxnet"   # invented one-word network name
FAKE_SSID_DIGITS = "QX_WARM9_12_3_4_5"   # invented device network name with digits


class NetworkNames(RepoCase):
    """A Wi-Fi name has no digit and is no street, so it needs the network kind
    (rt-000000000003: a scaffold published a network name)."""

    def test_one_word_name_is_rejected_under_other_kinds(self) -> None:
        self.assertEqual(pi.add_values(self.repo, "account", [FAKE_SSID]), (0, 1))

    def test_network_kind_takes_one_word_and_rejects_short_or_spaced(self) -> None:
        added, rejected = pi.add_values(self.repo, "network", [FAKE_SSID, "Zorb Lax Net", "Short", FAKE_SSID_DIGITS])
        self.assertEqual((added, rejected), (2, 2))
        text = pi.list_path(self.repo).read_text(encoding="utf-8").casefold()
        self.assertNotIn(FAKE_SSID.casefold(), text)
        self.assertIn("entry network w 10 ", text)

    def test_list_with_network_names_parses_under_the_old_rules(self) -> None:
        # A guard from before this change accepts only d/a/w shapes and matches a word-only
        # value only after a street suffix: the list stays readable there and matches nothing.
        pi.add_values(self.repo, "network", [FAKE_SSID])
        for line in pi.list_path(self.repo).read_text(encoding="utf-8").splitlines():
            if line.startswith("entry "):
                self.assertIn(line.split()[2], {"d", "a", "w"})
        old_style = pi.IdentifierList(salt=pi.load_list(self.repo).salt, entries=pi.load_list(self.repo).entries,
                                      shapes=pi.load_list(self.repo).shapes)
        self.assertEqual(old_style.kinds_in("connect to Zorblaxnet"), set())

    def test_network_names_match_whole_words_only(self) -> None:
        pi.add_values(self.repo, "network", [FAKE_SSID, FAKE_SSID_DIGITS])
        listed = pi.load_list(self.repo)
        for text in ("connect to the Zorblaxnet network", "ZORBLAXNET", "ssid=zorblaxnet;", "Sign in to QX_WARM9_12_3_4_5"):
            self.assertEqual(listed.kinds_in(text), {"network"}, text)
        for text in ("Zorblaxnets", "zorblax net", "xzorblaxnet", "network setup"):
            self.assertEqual(listed.kinds_in(text), set(), text)

    def test_commit_with_network_name_is_refused(self) -> None:
        pi.add_values(self.repo, "network", [FAKE_SSID])
        base = self.base()
        self.commit("workstreams/x/CONTEXT.md", "Back to my Zorblaxnet wifi.\n")
        verdict = self.verdict(base)
        self.assertEqual(verdict["status"], "hit")
        self.assertEqual(verdict["reason"], "workstreams/x/CONTEXT.md (network)")
        self.assertNotIn("zorblaxnet", repr(verdict).casefold())


class GuardRange(RepoCase):
    def test_no_list_passes(self) -> None:
        base = self.base()
        self.commit("b.md", f"{FAKE_ACCOUNT}\n")
        self.assertEqual(self.verdict(base)["status"], "absent")

    def test_clean_commit_passes(self) -> None:
        self.add_list()
        base = self.base()
        self.commit("b.md", "nothing here\n")
        self.assertEqual(self.verdict(base)["status"], "clean")

    def test_added_line_is_refused_and_names_file_and_kind_only(self) -> None:
        self.add_list()
        base = self.base()
        self.commit("system/x.py", f"home = '{FAKE_STREET}'\n")
        verdict = self.verdict(base)
        self.assertEqual(verdict["status"], "hit")
        self.assertEqual(verdict["reason"], "system/x.py (street)")
        self.assertNotIn("lark", repr(verdict).casefold())

    def test_value_added_then_removed_is_still_refused(self) -> None:
        self.add_list()
        base = self.base()
        self.commit("b.md", f"{FAKE_ACCOUNT}\n")
        self.commit("b.md", "gone\n")
        self.assertEqual(self.verdict(base)["status"], "hit")

    def test_removing_a_value_passes(self) -> None:
        self.add_list()
        self.commit("b.md", f"{FAKE_ACCOUNT}\n")
        base = self.base()
        self.commit("b.md", "gone\n")
        self.assertEqual(self.verdict(base)["status"], "clean")

    def test_deleting_a_file_named_with_a_value_passes(self) -> None:
        self.add_list()
        self.commit(f"{FAKE_ACCOUNT}.md", "text\n")
        base = self.base()
        (self.repo / f"{FAKE_ACCOUNT}.md").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "remove")
        self.assertEqual(self.verdict(base)["status"], "clean")

    def test_commit_message_is_refused(self) -> None:
        self.add_list()
        base = self.base()
        self.commit("b.md", "text\n", message=f"note {FAKE_ID}")
        verdict = self.verdict(base)
        self.assertEqual(verdict["reason"], "(commit message) (national_id)")

    def test_path_with_value_is_refused_and_withheld(self) -> None:
        self.add_list()
        base = self.base()
        self.commit(f"docs/{FAKE_ACCOUNT}/b.md", "text\n")
        verdict = self.verdict(base)
        self.assertEqual(verdict["status"], "hit")
        self.assertNotIn("263544", repr(verdict))
        self.assertIn("(path withheld)", verdict["reason"])

    def test_empty_file_with_value_in_its_name_is_refused(self) -> None:
        self.add_list()
        base = self.base()
        self.commit(f"{FAKE_ACCOUNT}.md", "")
        self.assertEqual(self.verdict(base)["status"], "hit")

    def test_empty_file_followed_by_another_file_is_refused(self) -> None:
        self.add_list()
        base = self.base()
        (self.repo / f"{FAKE_ACCOUNT}.md").write_text("", encoding="utf-8")
        (self.repo / "z.md").write_text("clean\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "two files")
        self.assertEqual(self.verdict(base)["status"], "hit")

    def test_added_line_starting_with_plus_signs_is_read(self) -> None:
        self.add_list()
        base = self.base()
        self.commit("b.md", f"++ b/{FAKE_ACCOUNT}\n")
        self.assertEqual(self.verdict(base)["status"], "hit")

    def test_merge_commit_is_read(self) -> None:
        self.add_list()
        base = self.base()
        git(self.repo, "checkout", "-q", "-b", "side")
        self.commit("side.md", f"{FAKE_ACCOUNT}\n")
        git(self.repo, "checkout", "-q", "main")
        self.commit("other.md", "other\n")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "side")
        self.assertEqual(self.verdict(base)["status"], "hit")

    def test_bad_range_fails_closed_when_a_list_exists(self) -> None:
        self.add_list()
        self.assertEqual(pi.guard_range(self.repo, "nosuchref..HEAD")["status"], "error")


class FailClosed(RepoCase):
    def test_truncated_list_fails_closed(self) -> None:
        self.add_list()
        path = pi.list_path(self.repo)
        path.write_text(path.read_text(encoding="utf-8").rsplit("\n", 2)[0] + "\n", encoding="utf-8")
        verdict = self.verdict(self.base())
        self.assertEqual(verdict["status"], "error")

    def test_garbage_list_fails_closed(self) -> None:
        path = pi.list_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_text("not a list\n", encoding="utf-8")
        self.assertEqual(self.verdict(self.base())["status"], "error")

    def test_undecodable_list_fails_closed(self) -> None:
        path = pi.list_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(self.verdict(self.base())["status"], "error")

    def test_list_that_loaded_here_and_went_missing_fails_closed(self) -> None:
        self.add_list()
        base = self.base()
        self.assertEqual(self.verdict(base)["status"], "clean")   # writes the stamp
        pi.list_path(self.repo).unlink()
        self.assertEqual(self.verdict(base)["status"], "error")

    def test_retiring_the_list_on_purpose_takes_both_files(self) -> None:
        self.add_list()
        base = self.base()
        self.verdict(base)
        pi.list_path(self.repo).unlink()
        pi.stamp_path(self.repo).unlink()
        self.assertEqual(self.verdict(base)["status"], "absent")

    @unittest.skipIf(os.name == "nt", "chmod does not remove read access on Windows")
    def test_unreadable_list_fails_closed(self) -> None:
        self.add_list()
        path = pi.list_path(self.repo)
        path.chmod(0)
        self.addCleanup(path.chmod, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self.verdict(self.base())["status"], "error")


class Finish(RepoCase):
    """dispatch.finish_branch, on a real branch and worktree."""

    def setUp(self) -> None:
        super().setUp()
        self.branch = "task/rt-x"
        self.worktree = self.root / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", self.branch, str(self.worktree))
        self.task = {"id": "rt-x", "branch": self.branch, "worktree": str(self.worktree), "prompt": "x"}
        patcher = patch.object(dispatch, "REPO_ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def edit(self, text: str) -> None:
        (self.worktree / "b.md").write_text(text, encoding="utf-8")

    def test_without_the_guard_the_value_lands(self) -> None:
        """Shows the guard is what stops it: same branch, guard off, main takes the value."""
        self.add_list()
        self.edit(f"{FAKE_ACCOUNT}\n")
        with patch.object(dispatch, "private_identifier_refusal", lambda rev_range: ""):
            outcome = dispatch.finish_branch(self.task)
        self.assertTrue(outcome["landed"], outcome)
        self.assertIn(FAKE_ACCOUNT, (self.repo / "b.md").read_text(encoding="utf-8"))

    def test_finish_refuses_and_main_does_not_move(self) -> None:
        self.add_list()
        before = self.base()
        self.edit(f"{FAKE_ACCOUNT}\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertEqual((outcome["landed"], outcome["conflict"], outcome["private_identifier"]), (False, True, True), outcome)
        self.assertEqual(self.base(), before)
        self.assertIn("b.md (account)", outcome["reason"])
        self.assertNotIn("263544", repr(outcome))

    def test_finish_lands_after_the_value_is_taken_out(self) -> None:
        self.add_list()
        self.edit(f"{FAKE_ACCOUNT}\n")
        self.assertFalse(dispatch.finish_branch(self.task)["landed"])
        base = git(self.worktree, "merge-base", "main", "HEAD").strip()
        git(self.worktree, "reset", "-q", "--soft", base)
        self.edit("invented value\n")
        self.assertTrue(dispatch.finish_branch(self.task)["landed"])

    def test_finish_with_no_list_lands(self) -> None:
        self.edit(f"{FAKE_ACCOUNT}\n")
        self.assertTrue(dispatch.finish_branch(self.task)["landed"])

    def test_finish_refuses_when_the_list_is_unreadable(self) -> None:
        path = pi.list_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_text("not a list\n", encoding="utf-8")
        self.edit("harmless\n")
        outcome = dispatch.finish_branch(self.task)
        self.assertFalse(outcome["landed"])
        self.assertIn("cannot be checked", outcome["reason"])


class AutoPush(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main")
        self.alerts: list[tuple[str, str]] = []
        state = self.root / "state"
        for patcher in (patch.object(dispatch, "REPO_ROOT", self.repo),
                        patch.object(dispatch, "LOCKS", self.repo / ".amp" / "locks"),
                        patch.object(dispatch, "AUTO_PUSH_STAMP", state / "auto-push.json"),
                        patch.object(dispatch, "AUTO_PUSH_LOG", state / "logs" / "auto-push.log"),
                        patch.object(dispatch, "auto_push_alert", lambda text, status: self.alerts.append((status, text)) or True),
                        patch.object(dispatch, "auto_push_digest_line", lambda text: True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def origin_main(self) -> str:
        return git(self.origin, "rev-parse", "main").strip()

    def test_auto_push_refuses_and_names_file_and_kind_only(self) -> None:
        self.add_list()
        before = self.origin_main()
        self.commit("b.md", f"{FAKE_STREET}\n")
        outcome = dispatch.auto_push_tick()
        self.assertEqual(outcome["action"], "private-identifier", outcome)
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(self.alerts[0][0], "blocked")
        self.assertIn("b.md (street)", self.alerts[0][1])
        written = repr(self.alerts) + repr(outcome) + dispatch.AUTO_PUSH_LOG.read_text(encoding="utf-8")
        self.assertNotIn("lark", written.casefold())

    def test_auto_push_ignores_a_value_already_on_origin(self) -> None:
        """History on origin is not this guard's job; only unpushed commits are read."""
        self.commit("old.md", f"{FAKE_STREET}\n")
        git(self.repo, "push", "-q", "origin", "main")
        self.add_list()
        self.commit("b.md", "clean\n")
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")

    def test_auto_push_with_no_list_pushes(self) -> None:
        self.commit("b.md", f"{FAKE_STREET}\n")
        self.assertEqual(dispatch.auto_push_tick()["action"], "pushed")

    def test_auto_push_refuses_when_the_list_is_unreadable(self) -> None:
        path = pi.list_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_text("not a list\n", encoding="utf-8")
        before = self.origin_main()
        self.commit("b.md", "harmless\n")
        self.assertEqual(dispatch.auto_push_tick()["action"], "private-identifier")
        self.assertEqual(self.origin_main(), before)


if __name__ == "__main__":
    unittest.main()
