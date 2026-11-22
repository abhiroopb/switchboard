#!/usr/bin/env python3
"""Tests for local_backup.py and the kit installer's backup step.

Everything runs under a scratch home with a scratch repo. PM_OS_TEST_SANDBOX=1
keeps every alert away from a live tab, and the only git calls are in GitBundle.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
os.environ["PM_OS_TEST_SANDBOX"] = "1"
import local_backup as lb  # noqa: E402

INSTALL_PY = ROOT / "install.py"  # the kit ships the installer at its root
FAKE_CLAUDE_TOKEN = "sk-ant-" + "oat01-" + "A" * 40
FAKE_GITHUB_TOKEN = "ghp_" + "b" * 36
FAKE_GOOGLE_TOKEN = "ya29." + "c" * 40
FAKE_ENV_SECRET = "abc123" + "d" * 30
# Split, or the nightly history scan reads this file's own commits as holding a key.
PEM_HEAD = "-----BEGIN " + "PRIVATE KEY-----"
RSA_HEAD = "-----BEGIN RSA " + "PRIVATE KEY-----"
SECRETS = (FAKE_CLAUDE_TOKEN, FAKE_GITHUB_TOKEN, FAKE_GOOGLE_TOKEN, FAKE_ENV_SECRET)


def write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_home(home: Path) -> None:
    old = time.time() - 3600
    claude, pmos = home / ".claude", home / ".pm-os"
    write(claude / "settings.json", '{"theme": "dark"}\n', old)
    write(claude / "CLAUDE.md", "# global\n", old)
    write(claude / "skills/todo/SKILL.md", "# todo\n", old)
    write(claude / "skills/todo/design-tokens.json", '{"color": "red"}\n', old)
    write(claude / "projects/repo/memory/fact.md", "remember this\n", old)
    write(claude / "projects/repo/abc.jsonl",
          json.dumps({"text": f"pasted {FAKE_CLAUDE_TOKEN} here"}) + "\n" + json.dumps({"text": "clean line"}) + "\n", old)
    write(claude / "history.jsonl", json.dumps({"display": f"token {FAKE_GITHUB_TOKEN}"}) + "\n", old)
    write(claude / ".credentials.json", json.dumps({"access": FAKE_CLAUDE_TOKEN}))
    write(claude / "shell-snapshots/snap.sh", f"export X={FAKE_ENV_SECRET}\n")
    write(claude / "security/venv/big.bin", "x" * 2000)
    write(claude / "plugins/cache/a/file.txt", "cached\n")
    write(claude / "plugins/installed_plugins.json", "{}\n", old)
    write(claude / "notes/leaky.txt", f"key {FAKE_GOOGLE_TOKEN}\n", old)
    write(pmos / "agents-local.md", "# local\n", old)
    write(pmos / "env", f"# header\nPM_OS_ROOT=/tmp/x\nMY_API_TOKEN={FAKE_ENV_SECRET}\nPM_OS_COMMS_TRIAGE=off\n", old)
    write(pmos / "connectors/google-token.json", json.dumps({"refresh": FAKE_GOOGLE_TOKEN}))
    write(pmos / "connectors/google-oauth-client.json", "{}")
    write(pmos / "messages/one.md", "hello\n", old)
    write(pmos / "wmux-sidecar.json", "{}")
    write(pmos / "backups/old/.claude.json", '{"oauthAccount": {}}')
    write(pmos / "backups/old/claude.before-trust.json", '{"oauthAccount": {}}')
    write(pmos / "backups/old/settings.json", "{}", old)
    write(pmos / ".session-x.tmp", "tmp")


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pmos-backup-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        make_home(self.home)

    def backup(self, day: str = "2026-11-08", **kw) -> Path:
        result = lb.run_backup(self.repo, home=self.home, day=day, host="testhost", **kw)
        self.assertEqual(result["status"], "ok", result)
        return Path(result["path"])

    def members(self, backup: Path, archive: str) -> dict[str, bytes]:
        with zipfile.ZipFile(backup / archive) as handle:
            return {n: handle.read(n) for n in handle.namelist()}


class WordRuns(unittest.TestCase):
    """Recovery phrases have no token shape. Every word here is invented."""
    WORDS = ("brimble sotter quaffin dravel plinko mesket thorvan glimby ostrel vandle jossik wembrol "
             "tarnip feldor crasko nimbel hobbet ruskal penzor galdip ormick sebbet yandle zoffin").split()

    def clean(self, text: str) -> tuple[str, list[str]]:
        out, kinds = lb.redact_line(text.encode())
        return out.decode(), kinds

    def test_a_phrase_is_redacted_whatever_its_shape(self) -> None:
        twelve, all24 = " ".join(self.WORDS[:12]), " ".join(self.WORDS)
        for text in (twelve, all24, "\\n".join(self.WORDS[:12]),
                     " ".join(f"{i}. {w}" for i, w in enumerate(self.WORDS[:12], 1)),
                     " ".join(f"{i}) {w}" for i, w in enumerate(self.WORDS[:12], 1))):
            with self.subTest(text=text[:30]):
                out, kinds = self.clean(f"my words: {text} end")
                self.assertEqual(kinds, [lb.WORD_RUN_KIND])
                for word in self.WORDS[:12]:
                    self.assertNotIn(word, out)
                self.assertIn("[REDACTED:word-run]", out)
                self.assertIn("my words:", out)

    def test_a_word_right_after_an_escaped_newline_goes_too(self) -> None:
        out, _ = self.clean("Start:\\n" + " ".join(self.WORDS[:12]))
        self.assertNotIn(self.WORDS[0], out)
        self.assertIn("Start:\\n[REDACTED:word-run]", out)

    def test_it_stays_valid_json_in_a_transcript_line(self) -> None:
        line = json.dumps({"text": "1. " + "\n".join(self.WORDS[:12]), "kept": "other"}).encode() + b"\n"
        clean, kinds = lb.redact_transcript_line(line)
        self.assertIn(lb.WORD_RUN_KIND, kinds)
        parsed = json.loads(clean)
        self.assertEqual(parsed["kept"], "other")
        self.assertNotIn(self.WORDS[3], parsed["text"])

    def test_ordinary_text_is_left_alone(self) -> None:
        for text in (
            " ".join(self.WORDS[:11]),                       # too short
            "i think we should move this over to the other folder and then check that it all still works when we run it again",
            "please open the file and read what the owner wrote about the plan then tell them what you found",
            "abstract boolean char class const default double enum extends final float import interface long native",  # sorted list
            " ".join(["brimble", "sotter"] * 8),             # repeats
            " ".join(w.capitalize() for w in self.WORDS[:12]),
            "-".join(self.WORDS[:12]),
            ", ".join(self.WORDS[:12]),                      # a skill description's keyword list has this shape
            "path/" + "/".join(self.WORDS[:12]),
            "the and for that this with you was but not have from they will would there",
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(self.clean(text)[1], [])

    def test_a_huge_line_stays_fast(self) -> None:
        started = time.monotonic()
        lb.redact_line(b"word " * 200_000)
        lb.redact_line(b"abcdefghij1" * 200_000)
        lb.redact_line((" ".join(self.WORDS[:6]) + " 1234567890 ").encode() * 20_000)
        self.assertLess(time.monotonic() - started, 20)

    def test_secret_kinds_sees_it_only_for_transcripts(self) -> None:
        data = " ".join(self.WORDS[:12]).encode()
        self.assertEqual(lb.secret_kinds(data), [])
        self.assertEqual(lb.secret_kinds(data, transcript=True), [lb.WORD_RUN_KIND])

    def test_an_ssn_shape_is_redacted_in_a_transcript_only(self) -> None:
        # 000 is never issued, so this can't be a real number.
        for shape in (b"000-12-3456", b"000 12 3456"):
            out, kinds = lb.redact_line(b"folder/" + shape + b" invented.pdf")
            self.assertEqual(kinds, ["ssn-shape"])
            self.assertNotIn(shape, out)
            self.assertEqual(lb.secret_kinds(shape), [])
        for other in (b"2026-11-08", b"1000-12-34567", b"id 000-12-34567"):
            self.assertEqual(lb.redact_line(other)[1], [])


class WordRunsInABackup(Scratch):
    def test_a_phrase_never_reaches_the_archive(self) -> None:
        words = " ".join(WordRuns.WORDS[:12])
        session = self.home / ".claude" / "projects" / "invented"
        session.mkdir(parents=True, exist_ok=True)
        (session / "s.jsonl").write_text(json.dumps({"text": f"pasted {words}"}) + "\n", encoding="utf-8")
        saved = session / "s" / "tool-results"
        saved.mkdir(parents=True)
        (saved / "out.txt").write_text(words, encoding="utf-8")
        (saved / "fine.txt").write_text("ordinary output", encoding="utf-8")
        backup = self.backup()
        members = self.members(backup, "claude.zip")
        self.assertNotIn(WordRuns.WORDS[0].encode(), b"".join(members.values()))
        self.assertIn("projects/invented/s.jsonl", members)
        self.assertIn(b"[REDACTED:word-run]", members["projects/invented/s.jsonl"])
        self.assertNotIn("projects/invented/s/tool-results/out.txt", members)
        self.assertIn("projects/invented/s/tool-results/fine.txt", members)
        self.assertNotIn(WordRuns.WORDS[0], (backup / "LEFT-OUT.txt").read_text(encoding="utf-8"))


class BackupContents(Scratch):
    def test_layout_manifest_counts_and_hashes(self) -> None:
        backup = self.backup()
        self.assertEqual(backup, self.repo / "local/backups/testhost/2026-11-08")
        manifest = lb.read_manifest(backup)
        for name in ("claude", "pm-os"):
            info = manifest["roots"][name]
            members = self.members(backup, f"{name}.zip")
            self.assertEqual(info["files"], len(members))
            self.assertEqual(info["files"], len(manifest["files"][name]))
            for row in manifest["files"][name]:
                self.assertEqual(row["h"], hashlib.sha256(members[row["p"]]).hexdigest())
        self.assertEqual(manifest["totals"]["files"], sum(i["files"] for i in manifest["roots"].values()))
        self.assertEqual(lb.verify_dir(backup), [])

    def test_no_secret_anywhere_in_the_backup_folder(self) -> None:
        backup = self.backup()
        for path in backup.rglob("*"):
            if not path.is_file():
                continue
            blobs = [path.read_bytes()]
            if path.suffix == ".zip":
                blobs += list(self.members(backup, path.name).values())
            for blob in blobs:
                for secret in SECRETS:
                    self.assertNotIn(secret.encode(), blob, f"{secret[:6]} leaked in {path.name}")

    def test_credentials_left_out_and_each_has_a_recovery_line(self) -> None:
        backup = self.backup()
        claude = self.members(backup, "claude.zip")
        pmos = self.members(backup, "pm-os.zip")
        for gone in (".credentials.json", "shell-snapshots/snap.sh", "security/venv/big.bin", "plugins/cache/a/file.txt"):
            self.assertNotIn(gone, claude)
        for gone in ("connectors/google-token.json", "connectors/google-oauth-client.json", "wmux-sidecar.json", ".session-x.tmp",
                     "backups/old/.claude.json", "backups/old/claude.before-trust.json"):
            self.assertNotIn(gone, pmos)
        self.assertIn("backups/old/settings.json", pmos)
        rows = lb.read_manifest(backup)["left_out"]
        paths = {r["path"] for r in rows}
        for expected in (".credentials.json", "connectors/google-token.json", "connectors/google-oauth-client.json",
                         "notes/leaky.txt", "env line MY_API_TOKEN"):
            self.assertIn(expected, paths)
        for row in rows:
            self.assertIn(row["recovery"], lb.RECOVERY)
        text = (backup / "LEFT-OUT.txt").read_text(encoding="utf-8")
        self.assertIn("pmos login google", text)
        self.assertIn("/login", text)
        self.assertIn("What a restore cannot bring back", text)

    def test_secure_folder_never_enters_a_backup(self) -> None:
        invented = "invented-secure-bytes-not-a-real-value"
        write(self.home / ".pm-os/secure/vault.dpapi", invented, time.time() - 3600)
        write(self.home / ".pm-os/secure/nested/other.bin", invented, time.time() - 3600)
        write(self.home / ".pm-os/secure-notes/kept.md", "a similar name stays in\n", time.time() - 3600)
        backup = self.backup()
        pmos = self.members(backup, "pm-os.zip")
        self.assertFalse([name for name in pmos if name.startswith("secure/")])
        self.assertIn("secure-notes/kept.md", pmos)
        for blob in pmos.values():
            self.assertNotIn(invented.encode(), blob)
        manifest = lb.read_manifest(backup)
        self.assertFalse([row for row in manifest["files"]["pm-os"] if row["p"].startswith("secure/")])
        recovery = {row["path"]: row["recovery"] for row in manifest["left_out"]}
        self.assertEqual(recovery["secure/vault.dpapi"], "secure-store")
        self.assertEqual(recovery["secure/nested/other.bin"], "secure-store")
        for path in backup.rglob("*"):
            if path.is_file():
                self.assertNotIn(invented.encode(), path.read_bytes())

    def test_a_restore_leaves_the_secure_folder_alone(self) -> None:
        kept = write(self.home / ".pm-os/secure/vault.dpapi", "invented-local-bytes", time.time() - 3600)
        backup = self.backup()
        lb.apply_restore(lb.restore_plan(backup, lb.home_roots(self.home)), lb.home_roots(self.home))
        self.assertEqual(kept.read_text(encoding="utf-8"), "invented-local-bytes")

    def test_wanted_files_are_kept(self) -> None:
        backup = self.backup()
        claude = self.members(backup, "claude.zip")
        for kept in ("settings.json", "CLAUDE.md", "skills/todo/SKILL.md", "skills/todo/design-tokens.json",
                     "projects/repo/memory/fact.md", "plugins/installed_plugins.json"):
            self.assertIn(kept, claude)
        self.assertIn("messages/one.md", self.members(backup, "pm-os.zip"))

    def test_transcript_redacted_in_place_and_stays_valid_json(self) -> None:
        backup = self.backup()
        lines = self.members(backup, "claude.zip")["projects/repo/abc.jsonl"].decode().splitlines()
        self.assertEqual(json.loads(lines[0])["text"], "pasted [REDACTED:anthropic-key] here")
        self.assertEqual(json.loads(lines[1])["text"], "clean line")

    def test_env_keeps_settings_and_drops_secret_lines(self) -> None:
        env = self.members(self.backup(), "pm-os.zip")["env"].decode()
        self.assertIn("PM_OS_COMMS_TRIAGE=off", env)
        self.assertIn("PM_OS_ROOT=/tmp/x", env)
        self.assertNotIn("MY_API_TOKEN", env)

    def test_transcripts_off_setting(self) -> None:
        with mock.patch.dict(os.environ, {"PM_OS_BACKUP_TRANSCRIPTS": "off"}):
            backup = self.backup()
        claude = self.members(backup, "claude.zip")
        self.assertNotIn("projects/repo/abc.jsonl", claude)
        self.assertIn("projects/repo/memory/fact.md", claude)

    def test_redaction_patterns(self) -> None:
        for line, kind in ((f"Authorization: Bearer {'z' * 30}", "bearer-token"),
                           (r'{\"refresh_token\":\"' + "q" * 30 + r'\"}', "json-credential"),
                           (f"export SECRET_THING=abc{'1' * 25}", "assigned-secret"),
                           (RSA_HEAD + "\\nabc\\n-----END RSA PRIVATE KEY-----", "private-key")):
            clean, kinds = lb.redact_line(line.encode())
            self.assertIn(kind, kinds, line)
            self.assertIn(f"[REDACTED:{kind}]".encode(), clean)
        self.assertEqual(lb.redact_line(b"task-abcdefghijklmnopqrstuvwxyz0123456789abcd")[1], [])
        self.assertEqual(lb.redact_line(b"TOKEN = some_function_name_that_is_long")[1], [])

    def test_commented_env_token_and_bare_line_are_dropped(self) -> None:
        env = f"#SLACK=xoxb-{'1' * 20}\nxoxb-{'2' * 20}\n# plain comment\nKEEP=yes\n".encode()
        clean, dropped = lb.scrub_env(env)
        self.assertNotIn(b"xoxb-", clean)
        self.assertIn(b"# plain comment", clean)
        self.assertIn(b"KEEP=yes", clean)
        self.assertEqual(len(dropped), 2)

    def test_utf16_files_are_scanned_and_env_is_stored_as_utf8(self) -> None:
        token = f"ghp_{'x' * 36}"
        write(self.home / ".claude/notes/wide.txt", "")
        (self.home / ".claude/notes/wide.txt").write_bytes(f"key {token}\n".encode("utf-16"))
        (self.home / ".claude/notes/wide-nobom.txt").write_bytes(f"key {token}\n".encode("utf-16-le"))
        (self.home / ".pm-os/env").write_bytes(f"KEEP=yes\nMY_TOKEN={token}\n".encode("utf-16"))
        backup = self.backup()
        claude, pmos = self.members(backup, "claude.zip"), self.members(backup, "pm-os.zip")
        self.assertNotIn("notes/wide.txt", claude)
        self.assertNotIn("notes/wide-nobom.txt", claude)
        self.assertEqual(pmos["env"].decode("utf-8").strip(), "KEEP=yes")

    def test_skill_token_json_with_a_credential_stays_out(self) -> None:
        write(self.home / ".claude/skills/foo/token.json", json.dumps({"refresh_token": "opaque" + "0" * 30}))
        write(self.home / ".claude/skills/foo/script.py", 'TOKEN = "abc' + "1" * 30 + '"\n')
        claude = self.members(self.backup(), "claude.zip")
        self.assertNotIn("skills/foo/token.json", claude)
        self.assertIn("skills/foo/script.py", claude)

    def test_more_token_shapes_are_redacted(self) -> None:
        for line, kind in (("ASIA" + "C" * 16, "aws-key"),
                           ("aws_secret_access_key = " + "d/" * 20, "aws-secret"),
                           ('"Authorization": "Basic ' + "e" * 24 + '"', "basic-auth"),
                           ("api_key: abc" + "1" * 25, "assigned-secret"),
                           ("password: hunter22", "password-field"),
                           ('{"password":"short1"}', "password-field")):
            clean, kinds = lb.redact_line(line.encode())
            self.assertIn(kind, kinds, line)
            self.assertIn(b"[REDACTED:", clean)

    def test_loose_password_pattern_never_leaves_a_whole_file_out(self) -> None:
        self.assertEqual(lb.secret_kinds(b"password: required"), [])

    def test_unterminated_private_key_keeps_the_rest_of_the_line(self) -> None:
        line = json.dumps({"a": PEM_HEAD + "\nabc", "b": "x" * 1000}).encode()
        clean, kinds = lb.redact_line(line)
        self.assertIn("private-key", kinds)
        self.assertEqual(json.loads(clean)["b"], "x" * 1000)

    def test_redacted_transcript_lines_stay_valid_json(self) -> None:
        cases = [{"a": PEM_HEAD + "\nabc\n\"quoted\" and more", "b": 1},
                 {"a": RSA_HEAD + "\\nabc\\\\", "b": "kept"},
                 {"password": 123456789, "b": "kept"},
                 {"text": 'password: "hunter22" {"password":"abc123"}', "b": "kept"},
                 {"text": f"pasted {FAKE_CLAUDE_TOKEN} \\ and {FAKE_GITHUB_TOKEN}", "b": "kept"}]
        for case in cases:
            line = json.dumps(case).encode() + b"\n"
            clean, kinds = lb.redact_transcript_line(line)
            self.assertTrue(kinds, case)
            self.assertEqual(json.loads(clean)["b"], case["b"])
            self.assertTrue(clean.endswith(b"\n"))
            self.assertNotIn(FAKE_CLAUDE_TOKEN.encode(), clean)

    def test_fallback_rewrites_a_line_the_raw_patterns_would_break(self) -> None:
        line = json.dumps({"a": f"key {FAKE_GITHUB_TOKEN}", "b": "kept"}).encode() + b"\n"
        with mock.patch.object(lb, "redact_line", side_effect=[(b'{"broken', ["github-token"]),
                                                               (b"key [REDACTED:github-token]", ["github-token"]),
                                                               (b"kept", [])]):
            clean, _ = lb.redact_transcript_line(line)
        self.assertEqual(json.loads(clean), {"a": "key [REDACTED:github-token]", "b": "kept"})

    def test_regex_stays_fast_on_hostile_lines(self) -> None:
        for line in (b"token=" * 40000, b"a" * 4_000_000, b"sk-" * 400000, PEM_HEAD.encode() * 20000):
            start = time.perf_counter()
            lb.redact_line(line)
            self.assertLess(time.perf_counter() - start, 8, line[:12])

    def test_pre_restore_folder_is_not_backed_up(self) -> None:
        write(self.home / ".claude/.pre-restore-20261108-1/settings.json", "{}")
        self.assertFalse([n for n in self.members(self.backup(), "claude.zip") if "pre-restore" in n])

    def test_links_are_recorded_and_never_followed(self) -> None:
        outside = self.base / "outside"
        write(outside / "huge.txt", "do not copy\n")
        try:
            os.symlink(outside, self.home / ".claude" / "skills" / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks need a privilege on this machine")
        backup = self.backup()
        self.assertFalse([n for n in self.members(backup, "claude.zip") if "huge.txt" in n])
        self.assertEqual([l["path"] for l in lb.read_manifest(backup)["roots"]["claude"]["links"]], ["skills/linked"])


class Atomic(Scratch):
    def test_failure_publishes_nothing_and_keeps_the_previous_copy(self) -> None:
        good = self.backup("2026-11-07")
        with self.assertRaises(lb.BackupError):
            self.backup("2026-11-08", fail_after_archives=True)
        host_dir = self.repo / "local/backups/testhost"
        self.assertEqual(sorted(p.name for p in host_dir.iterdir() if p.is_dir()), ["2026-11-07"])
        self.assertEqual(lb.verify_dir(good), [])
        self.assertFalse((host_dir / ".run.lock").exists())

    def test_verification_failure_publishes_nothing(self) -> None:
        with mock.patch.object(lb, "verify_dir", return_value=["corrupt"]), self.assertRaises(lb.BackupError):
            self.backup("2026-11-08")
        self.assertFalse((self.repo / "local/backups/testhost/2026-11-08").exists())

    def test_same_day_rerun_replaces_the_copy(self) -> None:
        self.backup()
        write(self.home / ".claude/new.md", "added later\n")
        backup = self.backup()
        self.assertIn("new.md", self.members(backup, "claude.zip"))
        leftovers = [p.name for p in (self.repo / "local/backups/testhost").iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_second_run_refused_while_one_is_live(self) -> None:
        host_dir = self.repo / "local/backups/testhost"
        host_dir.mkdir(parents=True)
        (host_dir / ".run.lock").write_text("")
        with self.assertRaisesRegex(lb.BackupError, "another backup is running"):
            self.backup()


class CollapseGuard(Scratch):
    def test_empty_source_cannot_replace_a_good_copy(self) -> None:
        good = self.backup()
        before = lb.read_manifest(good)["totals"]["files"]
        shutil.rmtree(self.home / ".claude")
        shutil.rmtree(self.home / ".pm-os")
        with self.assertRaisesRegex(lb.BackupError, "no files found"):
            self.backup()
        self.assertEqual(lb.read_manifest(good)["totals"]["files"], before)

    def test_one_missing_root_is_refused_when_the_last_copy_had_it(self) -> None:
        good = self.backup()
        shutil.rmtree(self.home / ".claude")
        with self.assertRaisesRegex(lb.BackupError, "is missing"):
            self.backup()
        self.assertEqual(lb.verify_dir(good), [])

    def test_collapse_refused_then_allowed_by_flag(self) -> None:
        for i in range(40):
            write(self.home / f".claude/skills/many/{i}.md", "x\n")
        self.backup("2026-11-07")
        shutil.rmtree(self.home / ".claude/skills/many")
        with self.assertRaisesRegex(lb.BackupError, "fell from"):
            self.backup("2026-11-08")
        self.assertFalse((self.repo / "local/backups/testhost/2026-11-08").exists())
        self.assertEqual(self.backup("2026-11-08", allow_shrink=True).name, "2026-11-08")

    def test_first_run_with_only_one_root_is_fine(self) -> None:
        shutil.rmtree(self.home / ".pm-os")
        self.assertEqual(lb.verify_dir(self.backup()), [])


class Retention(unittest.TestCase):
    @staticmethod
    def nights(n: int, start: date = date(2026, 1, 1)) -> list[str]:
        return [(start + timedelta(days=i)).isoformat() for i in range(n)]

    def test_fourteen_daily_and_eight_weekly_after_every_night(self) -> None:
        kept: set[str] = set()
        for night in self.nights(200):
            kept = lb.select_keep(kept | {night})
            self.assertLessEqual(len(kept), 22)
            self.assertIn(night, kept)
        ordered = sorted(kept, reverse=True)
        self.assertEqual(ordered[:14], sorted(self.nights(200)[-14:], reverse=True))
        weeks = {date.fromisoformat(d).isocalendar()[:2] for d in ordered[14:]}
        self.assertEqual(len(weeks), 8)
        self.assertEqual(len(ordered[14:]), 8)

    def test_gap_in_backups_keeps_older_copies(self) -> None:
        names = self.nights(5, date(2026, 1, 1)) + self.nights(3, date(2026, 6, 1))
        self.assertEqual(lb.select_keep(names), set(names))

    def test_prune_touches_only_dated_folders_of_this_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "local/backups"
            host, other = root / "me", root / "other"
            for night in self.nights(60):
                (host / night).mkdir(parents=True)
                (other / night).mkdir(parents=True)
            (host / "notes").mkdir()
            removed = lb.prune(host)
            self.assertTrue(removed)
            self.assertTrue((host / "notes").is_dir())
            self.assertEqual(len([p for p in other.iterdir()]), 60)
            self.assertLessEqual(len([p for p in host.iterdir() if lb.DATE_RE.match(p.name)]), 22)


class Restore(Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.saved = self.backup()
        self.target = self.base / "restored"
        self.roots = lb.home_roots(self.target)

    def test_restore_into_empty_home_matches_hashes(self) -> None:
        plan = lb.restore_plan(self.saved, self.roots)
        done = lb.apply_restore(plan, self.roots)
        self.assertEqual(done["problems"], [])
        self.assertEqual(done["restored"], lb.read_manifest(self.saved)["totals"]["files"])
        for name, rows in lb.read_manifest(self.saved)["files"].items():
            for row in rows:
                path = self.roots[name] / row["p"]
                self.assertEqual(lb.sha256_file(path), row["h"])
                self.assertAlmostEqual(path.stat().st_mtime, row["m"], delta=1)
        self.assertFalse((self.roots["claude"] / ".credentials.json").exists())
        self.assertEqual((self.roots["claude"] / "projects/repo/memory/fact.md").read_text(), "remember this\n")

    def test_newer_local_file_is_never_overwritten(self) -> None:
        local = write(self.roots["claude"] / "settings.json", '{"theme": "newer"}\n')
        plan = lb.restore_plan(self.saved, self.roots)
        self.assertIn("settings.json", [i["path"] for i in plan["keep_newer"]])
        lb.apply_restore(plan, self.roots)
        self.assertEqual(local.read_text(), '{"theme": "newer"}\n')

    def test_file_edited_after_the_plan_is_kept(self) -> None:
        local = write(self.roots["claude"] / "settings.json", "older\n", time.time() - 90000)
        plan = lb.restore_plan(self.saved, self.roots)
        self.assertIn("settings.json", [i["path"] for i in plan["replace_older"]])
        write(local, "edited just now\n")
        lb.apply_restore(plan, self.roots)
        self.assertEqual(local.read_text(), "edited just now\n")

    def test_older_file_replaced_with_a_safety_copy(self) -> None:
        local = write(self.roots["claude"] / "settings.json", "older\n", time.time() - 90000)
        plan = lb.restore_plan(self.saved, self.roots)
        done = lb.apply_restore(plan, self.roots)
        self.assertEqual(local.read_text(), '{"theme": "dark"}\n')
        self.assertEqual((self.roots["claude"] / done["safety_folder"] / "settings.json").read_text(), "older\n")

    def test_identical_file_counts_as_same(self) -> None:
        lb.apply_restore(lb.restore_plan(self.saved, self.roots), self.roots)
        counts = lb.plan_counts(lb.restore_plan(self.saved, self.roots))
        self.assertEqual((counts["new"], counts["replace_older"], counts["keep_newer"]), (0, 0, 0))

    def test_corrupt_archive_fails_verify_and_cli_restores_nothing(self) -> None:
        archive = self.saved / "claude.zip"
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        archive.write_bytes(bytes(data))
        self.assertTrue(lb.verify_dir(self.saved))
        with mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(self.target), "--host", "testhost", "restore", "--apply"])
        self.assertEqual(code, 1)
        self.assertFalse(self.roots["claude"].exists())

    def test_cli_without_apply_changes_nothing(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(self.target), "--host", "testhost", "restore"])
        self.assertEqual(code, 0)
        self.assertFalse(self.target.exists())

    def test_path_traversal_in_manifest_is_refused(self) -> None:
        for bad in ("../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/evil.txt", "a\\..\\evil.txt"):
            with self.assertRaises(lb.BackupError, msg=bad):
                lb.safe_target(self.target, bad)

    def test_one_bad_row_is_skipped_and_the_rest_restore(self) -> None:
        manifest = lb.read_manifest(self.saved)
        manifest["files"]["claude"].append({"p": "../evil.txt", "s": 1, "h": "0", "m": 0})
        (self.saved / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        plan = lb.restore_plan(self.saved, self.roots)
        self.assertEqual([i["path"] for i in plan["skip_unsafe"]], ["../evil.txt"])
        self.assertTrue(any("not valid on this machine" in line for line in lb.plan_skips(plan)))
        self.assertEqual(lb.apply_restore(plan, self.roots)["problems"], [])
        self.assertFalse((self.target / "evil.txt").exists())

    def test_restore_never_writes_through_a_link(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.roots["claude"].mkdir(parents=True)
        try:
            os.symlink(outside, self.roots["claude"] / "skills", target_is_directory=True)
        except OSError:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(self.roots["claude"] / "skills"), str(outside)],
                           check=True, capture_output=True, stdin=subprocess.DEVNULL)
        plan = lb.restore_plan(self.saved, self.roots)
        self.assertTrue(plan["skip_link"])
        lb.apply_restore(plan, self.roots)
        self.assertEqual(list(outside.iterdir()), [])

    def test_older_env_merges_and_keeps_local_only_lines(self) -> None:
        local = write(self.roots["pm-os"] / "env", "LOCAL_ONLY=1\nMY_TOKEN=keepme\nPM_OS_COMMS_TRIAGE=on\n", time.time() - 90000)
        done = lb.apply_restore(lb.restore_plan(self.saved, self.roots), self.roots)
        text = local.read_text()
        self.assertIn("LOCAL_ONLY=1", text)
        self.assertIn("MY_TOKEN=keepme", text)
        self.assertIn("PM_OS_COMMS_TRIAGE=off", text, "the backup's value wins for a shared key")
        self.assertNotIn("PM_OS_COMMS_TRIAGE=on", text)
        self.assertEqual(done["problems"], [])

    def test_other_os_backup_skips_machine_path_files(self) -> None:
        with mock.patch.object(lb, "os_name", return_value="not-this-os"):
            plan = lb.restore_plan(self.saved, self.roots)
        self.assertTrue(plan["other_os"])
        skipped = {i["path"] for i in plan["skip_other_os"]}
        self.assertLessEqual({"settings.json", "CLAUDE.md", "env"}, skipped)
        self.assertNotIn("skills/todo/SKILL.md", skipped)


class GitBundle(Scratch):
    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()

    def test_bundle_has_all_refs_and_restores_both_ways(self) -> None:
        self.git("init", "-q", "-b", "main")
        write(self.repo / "a.txt", "one\n")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "first")
        self.git("branch", "task/keep")
        head = self.git("rev-parse", "HEAD")
        backup = self.backup()
        info = lb.read_manifest(backup)["git_bundle"]
        self.assertTrue(info["verified"])
        self.assertEqual(info["head"], head)
        self.assertGreaterEqual(info["refs"], 3)  # HEAD, main, task/keep

        clone = self.base / "clone"
        subprocess.run(["git", "clone", "-q", str(backup / "repo.bundle"), str(clone)], check=True, capture_output=True)
        self.assertEqual((clone / "a.txt").read_text(), "one\n")

        synced = self.base / "synced"
        write(synced / "a.txt", "one\n")
        write(synced / "note.md", "uncommitted, must survive\n")
        lb.restore_git(backup / "repo.bundle", synced)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=synced), head)
        self.assertEqual(self.git("rev-parse", "task/keep", cwd=synced), head)
        self.assertEqual((synced / "note.md").read_text(), "uncommitted, must survive\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=synced), "?? note.md")
        with self.assertRaisesRegex(lb.BackupError, "already has .git"):
            lb.restore_git(backup / "repo.bundle", synced)

    def test_folder_with_no_git_still_backs_up(self) -> None:
        self.assertIn("skipped", lb.read_manifest(self.backup())["git_bundle"])

    def _commit(self, name: str, text: str) -> None:
        write(self.repo / name, text)
        self.git("add", name)
        self.git("commit", "-q", "-m", name)

    def test_token_in_history_withholds_the_bundle_and_prunes_nothing(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", f"token {FAKE_GITHUB_TOKEN}\n")
        with mock.patch.object(lb, "prune", side_effect=AssertionError("must not prune")):
            result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        self.assertEqual(result["status"], "failed")
        self.assertIn("token shape", result["error"])
        self.assertNotIn(FAKE_GITHUB_TOKEN, result["error"])
        self.assertFalse((Path(result["path"]) / "repo.bundle").exists())
        self.assertEqual(lb.verify_dir(Path(result["path"])), [], "the home folders are still published")
        with mock.patch.dict(os.environ, {"PM_OS_BACKUP_ALLOW_HISTORY": "on"}):
            allowed = lb.run_backup(self.repo, home=self.home, day="2026-11-09", host="testhost")
        self.assertEqual(allowed["status"], "ok")
        self.assertTrue((Path(allowed["path"]) / "repo.bundle").exists())

    def test_token_in_a_stash_is_found(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", "clean\n")
        write(self.repo / "a.txt", f"stashed {FAKE_GITHUB_TOKEN}\n")
        self.git("stash", "push", "-q")
        self.assertEqual([h["kind"] for h in lb.scan_history(self.repo)][:1], ["github-token"])

    def test_same_day_rerun_with_a_failed_bundle_keeps_the_good_copy(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", "clean\n")
        good = self.backup()
        with mock.patch.object(lb, "write_bundle", return_value={"error": "boom"}), self.assertRaisesRegex(lb.BackupError, "stays"):
            self.backup()
        self.assertTrue((good / "repo.bundle").exists())

    def test_lost_git_folder_is_a_failure_not_a_quiet_skip(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", "clean\n")
        self.backup("2026-11-07")
        shutil.rmtree(self.repo / ".git", onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        self.assertEqual(result["status"], "failed")
        self.assertIn("no .git", result["error"])


class SleptRun(Scratch):
    """The 2026-11-09 run: it froze through Modern Standby, Task Scheduler killed its console
    at wake, and git could not start (0xC0000142). Nothing here touches a real task."""

    git = GitBundle.git
    _commit = GitBundle._commit

    def test_the_history_scan_retries_once_and_then_succeeds(self) -> None:
        dead = lb.BackupError("git log failed with exit code 3221225794")
        with mock.patch.object(lb, "_scan_history_once", side_effect=[dead, []]) as once, \
                mock.patch.object(lb.time, "sleep") as slept:
            self.assertEqual(lb.scan_history(self.repo), [])
        self.assertEqual(once.call_count, 2)
        slept.assert_called_once_with(lb.SCAN_RETRY_WAIT)

    def test_a_second_scan_failure_fails_and_says_it_retried(self) -> None:
        dead = lb.BackupError("git log failed with exit code 3221225794")
        with mock.patch.object(lb, "_scan_history_once", side_effect=[dead, dead]) as once, \
                self.assertRaisesRegex(lb.BackupError, "after 2 tries"):
            lb.scan_history(self.repo, wait=0)
        self.assertEqual(once.call_count, 2)

    def test_a_missing_git_is_not_retried(self) -> None:
        with mock.patch.object(lb, "_scan_history_once", side_effect=lb.BackupError("git not found")) as once, \
                self.assertRaisesRegex(lb.BackupError, "git not found"):
            lb.scan_history(self.repo, wait=0)
        self.assertEqual(once.call_count, 1)

    def test_git_children_get_their_own_hidden_console(self) -> None:
        if os.name == "nt":
            self.assertEqual(lb.NO_WINDOW, subprocess.CREATE_NO_WINDOW)
        seen: list[int] = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            seen.append(kwargs.get("creationflags", -1))
            return real_run(*args, **kwargs)

        with mock.patch.object(lb.subprocess, "run", side_effect=spy):
            lb.run_git(["--version"])
        self.assertEqual(seen, [lb.NO_WINDOW])
        source = Path(lb.__file__).read_text(encoding="utf-8")
        spawns = re.findall(r"subprocess\.(?:run|Popen)\((?:[^()]|\([^()]*\))*\)", source, re.S)
        self.assertTrue(spawns)
        for call in spawns:
            with self.subTest(call=call[:60]):
                self.assertIn("creationflags=NO_WINDOW", call)

    def test_a_failed_bundle_publishes_a_copy_marked_partial(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", "clean\n")
        with mock.patch.object(lb, "write_bundle", return_value={"error": "history scan failed: boom"}), \
                mock.patch.object(lb, "prune", side_effect=AssertionError("must not prune")):
            result = lb.run_backup(self.repo, home=self.home, day="2026-11-09", host="testhost")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["partial"])
        self.assertIn("partial copy with no repo.bundle", result["error"])
        backup = Path(result["path"])
        self.assertTrue(lb.read_manifest(backup)["partial"])
        self.assertFalse((backup / "repo.bundle").exists())
        self.assertEqual(lb.verify_dir(backup), [])
        self.assertTrue(lb.list_backups(lb.backups_root(self.repo))[0]["partial"])
        full = self.backup("2026-11-09")  # a later run that day replaces it
        self.assertFalse(lb.read_manifest(full)["partial"])
        self.assertTrue((full / "repo.bundle").exists())

    def test_a_run_frozen_by_sleep_says_so(self) -> None:
        start = datetime(2026, 11, 9, 11, 16, tzinfo=timezone.utc)
        calls = {"n": 0}

        def clock() -> datetime:
            calls["n"] += 1
            return start if calls["n"] == 1 else start + timedelta(hours=5, minutes=16)

        with mock.patch.object(lb, "now_utc", side_effect=clock):
            result = lb.run_backup(self.repo, home=self.home, day="2026-11-09", host="testhost")
        self.assertTrue(any("5h 16m" in w and "slept" in w for w in result["warnings"]), result["warnings"])
        quick = lb.run_backup(self.repo, home=self.home, day="2026-11-10", host="testhost")
        self.assertFalse(any("slept" in w for w in quick["warnings"]))

    def test_a_failed_run_that_slept_says_so_in_its_error(self) -> None:
        start = datetime(2026, 11, 9, 11, 16, tzinfo=timezone.utc)
        calls = {"n": 0}

        def clock() -> datetime:
            calls["n"] += 1
            return start if calls["n"] == 1 else start + timedelta(hours=5, minutes=16)

        with mock.patch.object(lb, "run_backup", side_effect=lb.BackupError("verification failed: x")), \
                mock.patch.object(lb, "now_utc", side_effect=clock), mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(self.home), "run", "--no-alert"])
        self.assertEqual(code, 1)
        error = lb.read_state(self.repo)["last_error"]
        self.assertIn("verification failed", error)
        self.assertIn("probably slept", error)

    def test_a_hung_history_scan_is_stopped_and_counts_as_a_failure(self) -> None:
        self.git("init", "-q", "-b", "main")
        self._commit("a.txt", "clean\n")
        real_popen = subprocess.Popen

        def hung(cmd, *args, **kwargs):  # stands in for a git log that never ends
            return real_popen([sys.executable, "-c", "import time; time.sleep(60)"], *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(lb, "SCAN_TIMEOUT_SECONDS", 1), mock.patch.object(lb.subprocess, "Popen", side_effect=hung), \
                self.assertRaisesRegex(lb.BackupError, "stopped after"):
            lb._scan_history_once(self.repo)
        self.assertLess(time.monotonic() - started, 30)


class Health(Scratch):
    def setUp(self) -> None:
        super().setUp()
        self.sent: list[str] = []

    def send(self, _repo: Path, text: str) -> bool:
        self.sent.append(text)
        return True

    def check(self, now=None) -> dict:
        report = lb.health(self.repo, "testhost", now=now)
        lb.alert_if_needed(self.repo, report, send=self.send, now=now)
        return report

    def test_fresh_backup_is_ok_and_silent(self) -> None:
        self.backup()
        self.assertEqual(self.check()["status"], "ok")
        self.assertEqual(self.sent, [])

    def test_age_over_48_hours_alerts_once_a_day(self) -> None:
        self.backup()
        finished = lb.parse_iso(lb.read_manifest(self.repo / "local/backups/testhost/2026-11-08")["finished_at"])
        self.assertEqual(self.check(now=finished + timedelta(hours=47))["status"], "ok")
        self.assertEqual(self.sent, [])
        later = finished + timedelta(hours=49)
        self.assertEqual(self.check(now=later)["status"], "stale")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Options:", self.sent[0])
        self.assertIn("Lean:", self.sent[0])
        self.check(now=later + timedelta(hours=6))
        self.assertEqual(len(self.sent), 1)
        self.check(now=later + timedelta(hours=25))
        self.assertEqual(len(self.sent), 2)

    def test_failed_run_alerts_and_recovery_clears(self) -> None:
        lb.record_run(self.repo, None, "BackupError: disk full")
        report = self.check()
        self.assertEqual(report["status"], "failed")
        self.assertIn("disk full", self.sent[0])
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        lb.record_run(self.repo, result, None)
        self.assertEqual(self.check()["status"], "ok")
        self.assertNotIn("alert_status", lb.read_state(self.repo))

    def test_never_ran_alerts_only_after_48_hours(self) -> None:
        start = lb.now_utc()
        self.assertEqual(self.check(now=start)["status"], "none")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.check(now=start + timedelta(hours=49))["status"], "stale")
        self.assertEqual(len(self.sent), 1)

    def test_kill_during_the_send_cannot_lose_the_stamp(self) -> None:
        lb.record_run(self.repo, None, "boom")

        def killed(_repo: Path, _text: str) -> bool:
            self.assertEqual(lb.read_state(self.repo)["alert_status"], "failed")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            lb.alert_if_needed(self.repo, lb.health(self.repo, "testhost"), send=killed)
        self.check()
        self.assertEqual(self.sent, [])

    def test_undelivered_alert_retries_on_the_next_check(self) -> None:
        lb.record_run(self.repo, None, "boom")
        lb.alert_if_needed(self.repo, lb.health(self.repo, "testhost"), send=lambda *_: False)
        self.check()
        self.assertEqual(len(self.sent), 1)

    def test_missing_archive_is_broken(self) -> None:
        (self.backup() / "pm-os.zip").unlink()
        self.assertEqual(self.check()["status"], "broken")
        self.assertEqual(len(self.sent), 1)

    def test_cli_failure_records_status_and_exits_nonzero(self) -> None:
        with mock.patch.object(lb, "run_backup", side_effect=OSError("no space")), mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(self.home), "run", "--no-alert"])
        self.assertEqual(code, 1)
        self.assertEqual(lb.read_state(self.repo)["last_status"], "failed")


def load_installer():
    spec = importlib.util.spec_from_file_location("kit_install_under_test", INSTALL_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalIsProtected(unittest.TestCase):
    """Install and cleanup can never delete local/."""

    def test_install_plan_never_removes_local(self) -> None:
        install = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            keep = write(dest / "local/backups/h/2026-11-08/manifest.json", "{}")
            stale = write(dest / "system/old.py", "x")
            record = {"values": {}, "files": {
                "local/backups/h/2026-11-08/manifest.json": hashlib.sha256(keep.read_bytes()).hexdigest(),
                "system/old.py": hashlib.sha256(stale.read_bytes()).hexdigest()}}
            write(dest / ".pm-os-kit/install.json", json.dumps(record))
            p = install.plan({}, {"OWNER_NAME": "x"}, dest, update=True, force=True)
        self.assertEqual(p["remove"], ["system/old.py"])

    def test_execute_refuses_a_plan_that_names_local(self) -> None:
        install = load_installer()
        p = {"write": [], "unchanged": [], "keep_personal": [], "keep_locally_modified": [], "remove": ["local/backups/x"],
             "_rendered": {}, "_seed_pairs": []}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "protected"):
            install.execute(p, {}, Path(tmp), no_git=True)

    def test_kit_file_under_local_is_never_written(self) -> None:
        install = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            p = install.plan({"local/x.txt": {"sha256": ""}}, {}, Path(tmp), update=False, force=False)
        self.assertEqual(p["write"], [])

    def test_is_protected_is_exact(self) -> None:
        install = load_installer()
        for rel in ("local", "local/backups/x", "./local/x", "LOCAL/x", "local\\x"):
            self.assertTrue(install.is_protected(rel), rel)
        for rel in ("system/local/x", ".local/x", "localhost/x", "docs/local.md"):
            self.assertFalse(install.is_protected(rel), rel)

    def test_fresh_install_allows_a_folder_holding_only_local(self) -> None:
        install = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "d"
            self.assertFalse(install.blocks_fresh_install(dest))
            write(dest / "local/backups/x", "y")
            self.assertFalse(install.blocks_fresh_install(dest))
            write(dest / "other.txt", "y")
            self.assertTrue(install.blocks_fresh_install(dest))

    def test_install_adds_the_ignore_rule_to_an_older_gitignore(self) -> None:
        install = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            write(dest / ".gitignore", "/people")
            install.ensure_local_ignored(dest)
            install.ensure_local_ignored(dest)
            self.assertEqual((dest / ".gitignore").read_text(), "/people\n/local/\n")

    def test_dropbox_keeps_syncing_local(self) -> None:
        self.assertNotIn("local", load_installer().DROPBOX_IGNORED)

    def test_gitignored_and_left_out_of_the_kit_delta(self) -> None:
        self.assertIn("/local/", (ROOT / ".gitignore").read_text(encoding="utf-8").split())
        if (ROOT / "docs/kit-port").is_dir():  # the kit-port tools stay out of the kit
            self.assertIn('"local/"', (ROOT / "docs/kit-port/build-kit-delta.py").read_text(encoding="utf-8"))

    def test_no_install_or_cleanup_script_deletes_local(self) -> None:
        # Every line counts, comments and docstrings too: an example such as
        # "rm -rf" beside a local/ path in prose fails here (pm-os-trash.py,
        # 2026-11-09 to 2026-11-12). Name the folder kind in prose instead.
        delete = re.compile(r"\brm\s+-[a-z]*r|rmtree|Remove-Item|\brmdir\b|\bunlink\b|git\s+clean")
        names_local = re.compile(r"(?<![.\w/\\-])local[/\\\"' ]", re.I)
        scripts = [INSTALL_PY, ROOT / "system/windows/install.ps1",
                   ROOT / "system/windows/register-scheduled-tasks.ps1"]
        for name in ("install-all.sh", "install-claude-code.sh", "install-claude-skills.py", "install-launchd.sh",
                     "verify-kit-install.sh", "prune-local-retention.py", "rotate-logs.sh", "pm-os-trash.py", "dispatch.py"):
            scripts.append(HERE / name)
        for script in scripts:
            for number, line in enumerate(script.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if delete.search(line):
                    self.assertFalse(names_local.search(line), f"{script.name}:{number}: {line.strip()}")
                self.assertNotRegex(line, r"git\s+clean\s+-[a-z]*x", f"{script.name}:{number} would delete ignored files")


class ReinstallSimulation(unittest.TestCase):
    """A lost machine: scratch home, a moved PM OS folder, the backup only on Dropbox.

    Runs the real installer against a tiny kit built around it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="pmos-reinstall-")
        cls.base = Path(cls._tmp.name)
        cls.kit = cls.base / "kit"
        shutil.copy2(INSTALL_PY, write(cls.kit / "install.py", ""))
        write(cls.kit / "AGENTS.md", "# policy {{OWNER_NAME}}\n")
        write(cls.kit / ".gitignore", "/local/\n")
        script = cls.kit / "system/scripts/local_backup.py"
        script.parent.mkdir(parents=True)
        shutil.copy2(HERE / "local_backup.py", script)
        files = {rel: {"sha256": hashlib.sha256((cls.kit / rel).read_bytes()).hexdigest(), "source": "repo"}
                 for rel in ("AGENTS.md", ".gitignore", "system/scripts/local_backup.py")}
        write(cls.kit / ".pm-os-kit/source-manifest.json", json.dumps({"files": files}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.case = tempfile.TemporaryDirectory(prefix="c", dir=self.base)
        self.addCleanup(self.case.cleanup)
        self.home = Path(self.case.name) / "home"
        self.home.mkdir()
        self.dropbox_repo = self.home / "Dropbox" / "Development" / "pm-os"
        self.dest = self.home / "Development" / "pm-os-new"

    def take_backup(self) -> Path:
        make_home(self.home)
        result = lb.run_backup(self.dropbox_repo, home=self.home, day="2026-11-08", host="old-laptop")
        self.assertEqual(result["status"], "ok")
        shutil.rmtree(self.home / ".claude")
        shutil.rmtree(self.home / ".pm-os")
        return Path(result["path"])

    def install(self, *extra: str, dest: Path | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PM_OS_", "CLAUDE_", "AMP_", "WMUX", "CMUX"))}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), APPDATA=str(self.home / "AppData/Roaming"),
                   LOCALAPPDATA=str(self.home / "AppData/Local"), PM_OS_TEST_SANDBOX="1", PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(self.kit / "install.py"), "--owner-name", "Sam Sample", "--owner-email", "sam@example.com",
             "--destination", str(dest or self.dest), "--no-git", *extra],
            capture_output=True, text=True, encoding="utf-8", env=env, stdin=subprocess.DEVNULL, timeout=120)

    def test_finds_on_dropbox_checks_restores_and_keeps_newer(self) -> None:
        backup = self.take_backup()
        newer = write(self.home / ".claude/settings.json", '{"theme": "set after the backup"}\n')
        done = self.install("--apply", "--restore-backup", "yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"backup: newest usable is old-laptop 2026-11-08", done.stderr)
        self.assertIn(str(backup), done.stderr)
        self.assertIn("manifest check passed", done.stderr)
        self.assertEqual((self.home / ".claude/projects/repo/memory/fact.md").read_text(), "remember this\n")
        self.assertEqual((self.home / ".claude/skills/todo/SKILL.md").read_text(), "# todo\n")
        self.assertEqual(newer.read_text(), '{"theme": "set after the backup"}\n')
        self.assertFalse((self.home / ".claude/.credentials.json").exists())
        env = (self.home / ".pm-os/env").read_text()
        self.assertIn("PM_OS_COMMS_TRIAGE=off", env, "a restored setting survives the install's own env write")
        self.assertIn(str(self.dest), env, "the env file points at the new folder")
        self.assertTrue((self.dest / "AGENTS.md").exists())
        self.assertIn("left out and how to get each one back", done.stderr)
        self.assertTrue((backup / "manifest.json").exists(), "the backup stays where it was")

    def test_restored_env_points_at_the_new_folder_while_the_old_one_exists(self) -> None:
        make_home(self.home)
        write(self.home / ".pm-os/env", f"PM_OS_ROOT={self.dropbox_repo}\nPM_OS_COMMS_TRIAGE=off\n", time.time() - 3600)
        write(self.dropbox_repo / "AGENTS.md", "old checkout\n")
        lb.run_backup(self.dropbox_repo, home=self.home, day="2026-11-08", host="old-laptop")
        shutil.rmtree(self.home / ".pm-os")
        done = self.install("--apply", "--restore-backup", "yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        env = (self.home / ".pm-os/env").read_text()
        self.assertIn(f"PM_OS_ROOT={self.dest}", env)
        self.assertIn("PM_OS_COMMS_TRIAGE=off", env)

    def test_update_reports_and_restores_only_when_told(self) -> None:
        first = self.install("--apply", "--restore-backup", "no")
        self.assertEqual(first.returncode, 0, first.stderr)
        make_home(self.home)
        lb.run_backup(self.dest, home=self.home, day="2026-11-08", host="old-laptop")
        shutil.rmtree(self.home / ".claude")
        update = self.install("--update")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertIn("nothing restored by this run", update.stderr)
        self.assertFalse((self.home / ".claude").exists())

    def test_bad_kit_aborts_before_any_restore(self) -> None:
        self.take_backup()
        bad = self.kit / "broken.py"
        original = (self.kit / ".pm-os-kit/source-manifest.json").read_text()
        write(bad, "def broken(:\n")
        files = json.loads(original)
        files["files"]["broken.py"] = {"sha256": hashlib.sha256(bad.read_bytes()).hexdigest(), "source": "repo"}
        (self.kit / ".pm-os-kit/source-manifest.json").write_text(json.dumps(files))
        try:
            done = self.install("--apply", "--restore-backup", "yes")
        finally:
            (self.kit / ".pm-os-kit/source-manifest.json").write_text(original)
            bad.unlink()
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())

    def test_no_terminal_asks_nothing_and_prints_the_command(self) -> None:
        self.take_backup()
        done = self.install("--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no terminal to ask on, so nothing was restored", done.stderr)
        self.assertIn("restore --backup", done.stderr)
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists())

    def test_dry_run_only_reports(self) -> None:
        self.take_backup()
        done = self.install("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nothing restored by this run", done.stderr)
        self.assertFalse((self.home / ".claude").exists())
        self.assertEqual(json.loads(done.stdout)["backup"]["counts"]["keep_newer"], 0)

    def test_says_so_clearly_when_nothing_is_found(self) -> None:
        done = self.install("--apply", "--restore-backup", "yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("backup: none found. Looked in:", done.stderr)
        self.assertIn(str(self.home / "Dropbox" / "Development" / "pm-os" / "local" / "backups"), done.stderr)

    def test_reinstall_over_the_folder_never_deletes_local(self) -> None:
        make_home(self.home)
        lb.run_backup(self.dest, home=self.home, day="2026-11-08", host="old-laptop")
        manifest = self.dest / "local/backups/old-laptop/2026-11-08/manifest.json"
        before = manifest.read_bytes()
        first = self.install("--apply", "--restore-backup", "no")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("not restored", first.stderr)
        update = self.install("--update", "--force", "--restore-backup", "no")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(lb.verify_dir(manifest.parent), [])

    def test_answering_yes_on_a_terminal_restores(self) -> None:
        self.take_backup()
        log: list[str] = []
        env = {"PM_OS_ROOT": ""}
        out = lb.installer_offer(self.dest, answer="ask", log=log.append, stdin=io.StringIO("y\n"), home=self.home, env=env)
        self.assertGreater(out["restored"], 0)
        self.assertTrue(any("restore it now" in line for line in log))

    def test_answering_no_on_a_terminal_restores_nothing(self) -> None:
        self.take_backup()
        out = lb.installer_offer(self.dest, answer="ask", log=lambda _: None, stdin=io.StringIO("\n"), home=self.home, env={})
        self.assertEqual(out["restored"], 0)
        self.assertFalse((self.home / ".claude").exists())

    def test_a_corrupt_newest_copy_falls_back_to_an_older_one(self) -> None:
        make_home(self.home)
        lb.run_backup(self.dropbox_repo, home=self.home, day="2026-11-07", host="old-laptop")
        time.sleep(1.1)  # finished_at has one-second resolution
        newest = Path(lb.run_backup(self.dropbox_repo, home=self.home, day="2026-11-08", host="old-laptop")["path"])
        (newest / "claude.zip").write_bytes(b"not a zip")
        log: list[str] = []
        out = lb.installer_offer(self.dest, dry_run=True, log=log.append, home=self.home, env={})
        self.assertTrue(out["backup"].endswith("2026-11-07"))
        self.assertTrue(any("failed its check" in line for line in log))

    def test_this_host_is_preferred_and_other_os_is_last(self) -> None:
        rows = [{"host": "mac", "platform": "other-os", "finished_at": "3", "date": "c", "broken": False},
                {"host": "peer", "platform": lb.os_name(), "finished_at": "2", "date": "b", "broken": False},
                {"host": "me", "platform": lb.os_name(), "finished_at": "1", "date": "a", "broken": False}]
        self.assertEqual([r["host"] for r in lb.rank(rows, host="me")], ["me", "peer", "mac"])


class Paths(unittest.TestCase):
    def test_worktree_git_file_resolves_to_main_with_no_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            (main / ".git/worktrees/rt-1").mkdir(parents=True)
            tree = Path(tmp) / "tree"
            write(tree / ".git", f"gitdir: {main}/.git/worktrees/rt-1\n")
            with mock.patch.object(subprocess, "run", side_effect=AssertionError("no process allowed")):
                self.assertEqual(lb.main_checkout(tree).resolve(), main.resolve())
                self.assertEqual(lb.main_checkout(main).resolve(), main.resolve())

    def test_explicit_home_ignores_every_override(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/real", "PM_OS_STATE_HOME": "/real2"}):
            roots = lb.home_roots(Path("/scratch"), repo=Path("/real-repo"))
        self.assertEqual(roots, {"claude": Path("/scratch/.claude"), "pm-os": Path("/scratch/.pm-os"),
                                 "owner": Path("/scratch/pm-os-local")})

    def test_dropbox_info_json_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write(home / "AppData/Roaming/Dropbox/info.json", json.dumps({"personal": {"path": str(home / "Elsewhere")}}))
            roots = lb.dropbox_roots(home, {"APPDATA": str(home / "AppData/Roaming")})
            self.assertIn(home / "Elsewhere", roots)
            self.assertIn(home / "Dropbox", roots)

    def test_alert_stays_out_of_live_tabs_in_a_test(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no send in a test")):
            self.assertFalse(lb.send_cos_alert(Path("."), "x"))


class HistoryFalseAlarms(unittest.TestCase):
    """The nightly scan must not trip on this project's own fixtures, and only those."""

    def test_these_sources_hold_no_token_shape(self) -> None:
        strong = [(n, p) for n, p in lb.SECRET_PATTERNS if n not in lb.GENERIC_KINDS]
        for name in ("test_local_backup.py", "local_backup.py", "test_thread_pointer.py"):
            for number, line in enumerate((HERE / name).read_bytes().splitlines(), 1):
                self.assertEqual([k for k, p in strong if p.search(line)], [], f"{name}:{number}")

    def test_the_pin_covers_one_commit_path_and_kind(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pmos-history-") as tmp:
            repo = Path(tmp)
            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null",
                                    "-c", "commit.gpgsign=false", *args],
                                      check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            write(repo / "fixture.py", RSA_HEAD + "\n")
            git("add", "-A")
            git("commit", "-q", "-m", "one")
            first = git("rev-parse", "HEAD")
            self.assertEqual([h["kind"] for h in lb.scan_history(repo)], ["private-key"])
            with mock.patch.object(lb, "HISTORY_FALSE_ALARMS", {(first, "fixture.py", "private-key")}):
                self.assertEqual(lb.scan_history(repo), [])
                # The same shape in a later commit is a new finding.
                write(repo / "fixture.py", RSA_HEAD + "\n" + PEM_HEAD + "\n")
                git("add", "-A")
                git("commit", "-q", "-m", "two")
                self.assertEqual(len(lb.scan_history(repo)), 1)
            for pin in ({(first, "other.py", "private-key")}, {(first, "fixture.py", "jwt")}):
                with mock.patch.object(lb, "HISTORY_FALSE_ALARMS", pin):
                    self.assertEqual(len(lb.scan_history(repo)), 2)


class OwnerFiles(Scratch):
    """local/agents-local.md, local/user-lens.yaml and local/machines/ in owner.zip."""

    def setUp(self) -> None:
        super().setUp()
        self.old = time.time() - 3600
        self.owner = self.home / lb.OWNER_SCRATCH_DIR
        write(self.owner / "agents-local.md", "# Owner context\nrole line\n", self.old)
        write(self.owner / "user-lens.yaml", "voice: direct\n", self.old)
        write(self.owner / "machines/EXAMPLE-PC.env", "PM_OS_COMMS_TRIAGE=off\n", self.old)
        write(self.owner / "backups/h/2026-11-01/claude.zip", "old backup, never read\n", self.old)
        write(self.owner / "other-notes.md", "not named, so not copied\n", self.old)

    def owner_members(self, backup: Path) -> dict[str, bytes]:
        return self.members(backup, "owner.zip")

    def test_named_paths_are_kept_byte_for_byte(self) -> None:
        backup = self.backup()
        self.assertEqual(self.owner_members(backup), {
            "agents-local.md": b"# Owner context\nrole line\n",
            "user-lens.yaml": b"voice: direct\n",
            "machines/EXAMPLE-PC.env": b"PM_OS_COMMS_TRIAGE=off\n",
        })
        self.assertEqual(lb.verify_dir(backup), [])
        self.assertEqual(lb.read_manifest(backup)["roots"]["owner"]["files"], 3)

    def test_a_machine_env_file_passes_the_credential_name_filters(self) -> None:
        self.assertIsNone(lb.skip_reason("owner", "machines/EXAMPLE-PC.env"))
        self.assertIsNone(lb.skip_reason("owner", "machines/example-macbook.local.env"))
        rows = lb.read_manifest(self.backup())["left_out"]
        self.assertEqual([r for r in rows if r["root"] == "owner"], [])

    def test_local_backups_is_never_opened_or_listed(self) -> None:
        names = [rel for rel, _, _ in lb.walk(self.owner, lb.OWNER_PATHS)]
        self.assertEqual(sorted(names), ["agents-local.md", "machines/EXAMPLE-PC.env", "user-lens.yaml"])
        with mock.patch.object(lb.os, "scandir", wraps=os.scandir) as scan:
            list(lb.walk(self.owner, lb.OWNER_PATHS))
        opened = [str(call.args[0]) for call in scan.call_args_list]
        self.assertFalse([p for p in opened if "backups" in p], opened)
        manifest = lb.read_manifest(self.backup())
        self.assertFalse([r for r in manifest["left_out"] if "backups" in r["path"] and r["root"] == "owner"])

    def test_private_skills_are_in_the_owner_zip(self) -> None:
        # Moved out of ~/.claude/skills, so a copy here is what brings their source back.
        write(self.owner / "skills/private-one/SKILL.md", "---\nname: private-one\n---\n", self.old)
        write(self.owner / "skills/private-one/scripts/tool.py", "print('x')\n", self.old)
        members = self.owner_members(self.backup())
        self.assertEqual(members["skills/private-one/SKILL.md"], b"---\nname: private-one\n---\n")
        self.assertEqual(members["skills/private-one/scripts/tool.py"], b"print('x')\n")

    def test_an_identifier_in_a_private_skill_is_not_dropped(self) -> None:
        # An account number and a street have no token shape, and the SSN shape is generic in a
        # skill. Wrong impl: the whole SKILL.md silently falls out, and a restore loses the skill.
        body = "---\nname: private-one\n---\naccount 000111222333\n1 Invented Street\nid 000-11-2222\n"
        write(self.owner / "skills/private-one/SKILL.md", body, self.old)
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        backup = Path(result["path"])
        self.assertEqual(self.owner_members(backup)["skills/private-one/SKILL.md"], body.encode())
        self.assertEqual([r for r in lb.read_manifest(backup)["left_out"] if r["root"] == "owner"], [])
        self.assertEqual([w for w in result["warnings"] if w.startswith("owner file left out")], [])

    def test_a_token_in_a_private_skill_is_left_out_and_says_so(self) -> None:
        write(self.owner / "skills/private-one/SKILL.md", "---\nname: private-one\n---\n", self.old)
        write(self.owner / "skills/private-one/NOTES.md", f"key {FAKE_GITHUB_TOKEN}\n", self.old)
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        backup = Path(result["path"])
        members = self.owner_members(backup)
        self.assertIn("skills/private-one/SKILL.md", members)
        self.assertNotIn("skills/private-one/NOTES.md", members)
        self.assertIn("owner file left out: skills/private-one/NOTES.md (secret-content, see LEFT-OUT.txt)", result["warnings"])
        self.assertIn("owner: skills/private-one/NOTES.md", (backup / "LEFT-OUT.txt").read_text(encoding="utf-8"))

    def test_a_private_skill_comes_back_on_restore(self) -> None:
        write(self.owner / "skills/private-one/SKILL.md", "---\nname: private-one\n---\n", self.old)
        backup = self.backup()
        target = self.base / "restored"
        with mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(target), "restore", "--backup", str(backup), "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual((target / lb.OWNER_SCRATCH_DIR / "skills/private-one/SKILL.md").read_bytes(),
                         b"---\nname: private-one\n---\n")

    def test_the_lens_is_optional(self) -> None:
        (self.owner / "user-lens.yaml").unlink()
        backup = self.backup()
        self.assertNotIn("user-lens.yaml", self.owner_members(backup))
        self.assertEqual(lb.verify_dir(backup), [])

    def test_no_owner_files_at_all_still_publishes(self) -> None:
        shutil.rmtree(self.owner)
        backup = self.backup()
        self.assertEqual(self.owner_members(backup), {})
        self.assertEqual(lb.verify_dir(backup), [])

    def test_the_token_scan_runs_on_owner_files(self) -> None:
        write(self.owner / "agents-local.md", f"role line {FAKE_GITHUB_TOKEN}\n", self.old)
        write(self.owner / "machines/LEAKY.env", f"NOTE={FAKE_GOOGLE_TOKEN}\n", self.old)
        (self.owner / "machines/WIDE.env").write_bytes(f"NOTE={FAKE_CLAUDE_TOKEN}\n".encode("utf-16"))
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        backup = Path(result["path"])
        members = self.owner_members(backup)
        self.assertEqual(sorted(members), ["machines/EXAMPLE-PC.env", "user-lens.yaml"])
        for blob in (*members.values(), (backup / "manifest.json").read_bytes(), (backup / "LEFT-OUT.txt").read_bytes()):
            for secret in SECRETS:
                self.assertNotIn(secret.encode(), blob)
        rows = {r["path"]: r for r in lb.read_manifest(backup)["left_out"] if r["root"] == "owner"}
        self.assertEqual(sorted(rows), ["agents-local.md", "machines/LEAKY.env", "machines/WIDE.env"])
        self.assertTrue(all(r["recovery"] == "secret-content" for r in rows.values()))
        # Not silent: the run says so, and LEFT-OUT.txt names each one.
        self.assertEqual(len([w for w in result["warnings"] if w.startswith("owner file left out")]), 3)
        self.assertIn("owner: agents-local.md", (backup / "LEFT-OUT.txt").read_text(encoding="utf-8"))

    def test_a_secret_looking_env_key_leaves_the_machine_file_out_whole(self) -> None:
        write(self.owner / "machines/KEYED.env", "PM_OS_COMMS_TRIAGE=off\nMY_API_TOKEN=short\n", self.old)
        backup = self.backup()
        self.assertNotIn("machines/KEYED.env", self.owner_members(backup))
        row = next(r for r in lb.read_manifest(backup)["left_out"] if r["path"] == "machines/KEYED.env")
        self.assertEqual(row["kinds"], ["env key MY_API_TOKEN"])
        self.assertNotIn(b"short", (backup / "manifest.json").read_bytes())

    def test_every_file_under_machines_gets_the_env_check(self) -> None:
        write(self.owner / "machines/EXAMPLE-PC.env.bak", "API_TOKEN=hunter2\n", self.old)
        write(self.owner / "machines/env", "API_TOKEN=hunter2\n", self.old)
        backup = self.backup()
        members = self.owner_members(backup)
        self.assertNotIn("machines/EXAMPLE-PC.env.bak", members)
        self.assertNotIn("machines/env", members)
        self.assertNotIn(b"hunter2", (backup / "manifest.json").read_bytes())

    def test_a_token_in_the_key_position_is_never_written_down(self) -> None:
        write(self.owner / "machines/ODD.env", f"{FAKE_GITHUB_TOKEN}=\n", self.old)
        write(self.home / ".pm-os/env", f"PM_OS_ROOT=/tmp/x\n{FAKE_GITHUB_TOKEN}=\n", self.old)
        backup = self.backup()
        self.assertNotIn("machines/ODD.env", self.owner_members(backup))
        for name in ("manifest.json", "LEFT-OUT.txt"):
            self.assertNotIn(FAKE_GITHUB_TOKEN.encode(), (backup / name).read_bytes(), name)
        self.assertEqual(lb.scrub_env(f"{FAKE_GITHUB_TOKEN}=\nAPI_TOKEN=x\n".encode())[1], ["(key on line 1)", "API_TOKEN"])

    def test_an_owner_file_gone_since_the_last_backup_is_a_warning(self) -> None:
        self.backup("2026-11-07")
        (self.owner / "agents-local.md").unlink()
        result = lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")
        self.assertEqual(result["status"], "ok")
        self.assertIn("owner file gone since the last backup: agents-local.md", result["warnings"])
        lb.record_run(self.repo, result, None)
        self.assertEqual(lb.read_state(self.repo)["last_warnings"], result["warnings"])
        again = lb.run_backup(self.repo, home=self.home, day="2026-11-09", host="testhost")
        self.assertEqual(again["warnings"], [])

    def test_the_printed_restore_command_names_the_install_folder(self) -> None:
        backup = self.backup()
        dest = self.base / "dest"
        kit_script = self.base / "kit/system/scripts/local_backup.py"
        rows = lb.list_backups(lb.backups_root(self.repo))
        with mock.patch.object(lb, "find_for_install", return_value=(rows, [])):
            out = lb.installer_offer(dest, dry_run=True, log=lambda _: None, home=self.home, env={}, script=kit_script)
        self.assertIn(f'--repo "{dest.as_posix()}" restore', out["command"])
        self.assertIn(backup.as_posix(), out["command"])
        args = lb.parse_args(["--repo", str(dest), "restore", "--backup", str(backup), "--apply"])
        self.assertEqual(lb.home_roots(repo=args.repo, env={})["owner"], dest / "local")

    def test_a_credential_named_file_stays_out(self) -> None:
        write(self.owner / "machines/secrets.env", "A=b\n", self.old)
        backup = self.backup()
        self.assertNotIn("machines/secrets.env", self.owner_members(backup))
        row = next(r for r in lb.read_manifest(backup)["left_out"] if r["path"] == "machines/secrets.env")
        self.assertEqual(row["recovery"], "secret-file")

    def test_owner_files_alone_do_not_pass_the_empty_check(self) -> None:
        for folder in (".claude", ".pm-os"):
            shutil.rmtree(self.home / folder)
            (self.home / folder).mkdir()
        with self.assertRaisesRegex(lb.BackupError, "no files found"):
            lb.run_backup(self.repo, home=self.home, day="2026-11-08", host="testhost")

    def test_verify_covers_the_owner_archive(self) -> None:
        backup = self.backup()
        with zipfile.ZipFile(backup / "owner.zip", "w") as handle:
            handle.writestr("agents-local.md", "tampered\n")
        self.assertTrue([p for p in lb.verify_dir(backup) if "owner.zip" in p])
        (backup / "owner.zip").unlink()
        self.assertIn("owner.zip missing", lb.verify_dir(backup))

    def test_verify_covers_each_owner_member(self) -> None:
        backup = self.backup()
        manifest = lb.read_manifest(backup)
        next(r for r in manifest["files"]["owner"] if r["p"] == "machines/EXAMPLE-PC.env")["h"] = "0" * 64
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("owner:machines/EXAMPLE-PC.env hash differs from the manifest", lb.verify_dir(backup))

    def test_scratch_restore_matches_hashes(self) -> None:
        backup = self.backup()
        roots = lb.home_roots(self.base / "restored")
        done = lb.apply_restore(lb.restore_plan(backup, roots), roots)
        self.assertEqual(done["problems"], [])
        for row in lb.read_manifest(backup)["files"]["owner"]:
            self.assertEqual(lb.sha256_file(roots["owner"] / row["p"]), row["h"])
            self.assertEqual(lb.sha256_file(self.owner / row["p"]), row["h"])
        self.assertFalse((roots["owner"] / "backups").exists())
        self.assertFalse((roots["owner"] / "other-notes.md").exists())

    def test_restore_keeps_a_newer_file_and_saves_an_older_one(self) -> None:
        backup = self.backup()
        target = self.base / "restored"
        roots = lb.home_roots(target)
        lb.apply_restore(lb.restore_plan(backup, roots), roots)
        newer = write(roots["owner"] / "agents-local.md", "edited after the backup\n", time.time() + 60)
        older = write(roots["owner"] / "machines/EXAMPLE-PC.env", "PM_OS_COMMS_TRIAGE=on\n", self.old - 3600)
        plan = lb.restore_plan(backup, roots)
        self.assertEqual([i["path"] for i in plan["keep_newer"] if i["root"] == "owner"], ["agents-local.md"])
        self.assertEqual([i["path"] for i in plan["replace_older"] if i["root"] == "owner"], ["machines/EXAMPLE-PC.env"])
        done = lb.apply_restore(plan, roots)
        self.assertEqual(newer.read_text(encoding="utf-8"), "edited after the backup\n")
        self.assertEqual(older.read_text(encoding="utf-8"), "PM_OS_COMMS_TRIAGE=off\n")
        saved = roots["owner"] / done["safety_folder"] / "machines/EXAMPLE-PC.env"
        self.assertEqual(saved.read_text(encoding="utf-8"), "PM_OS_COMMS_TRIAGE=on\n")

    def test_a_scratch_home_never_reaches_the_repo_local_folder(self) -> None:
        real = write(self.repo / "local/agents-local.md", "real role line\n", self.old - 3600)
        backup = self.backup()
        self.assertNotIn(b"real role line", self.owner_members(backup)["agents-local.md"])
        target = self.base / "restored"
        with mock.patch("sys.stdout", new=io.StringIO()):
            code = lb.main(["--repo", str(self.repo), "--home", str(target), "restore", "--backup", str(backup), "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(real.read_text(encoding="utf-8"), "real role line\n")
        self.assertFalse((self.repo / "local/.pre-restore").exists())
        self.assertFalse([p for p in (self.repo / "local").iterdir() if p.name.startswith(".pre-restore-")])
        self.assertEqual((target / lb.OWNER_SCRATCH_DIR / "agents-local.md").read_text(encoding="utf-8"),
                         "# Owner context\nrole line\n")

    def test_with_no_home_the_owner_root_is_the_repo_local_folder(self) -> None:
        self.assertEqual(lb.home_roots(env={}, repo=Path("/r"))["owner"], Path("/r/local"))
        self.assertNotIn("owner", lb.home_roots(env={}))
        write(self.repo / "local/agents-local.md", "from the repo\n", self.old)
        write(self.repo / "local/machines/EXAMPLE-PC.env", "PM_OS_COMMS_TRIAGE=off\n", self.old)
        write(self.repo / "local/backups/testhost/note.txt", "beside the real backups\n", self.old)
        env = {"CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "PM_OS_STATE_HOME": str(self.home / ".pm-os")}
        with mock.patch.dict(os.environ, env):
            backup = Path(lb.run_backup(self.repo, day="2026-11-08", host="testhost")["path"])
            self.assertEqual(sorted(self.owner_members(backup)), ["agents-local.md", "machines/EXAMPLE-PC.env"])
            (self.repo / "local/agents-local.md").unlink()
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(lb.main(["--repo", str(self.repo), "restore", "--backup", str(backup), "--apply"]), 0)
        self.assertEqual((self.repo / "local/agents-local.md").read_text(encoding="utf-8"), "from the repo\n")

    def test_the_installer_restores_owner_files_into_its_destination(self) -> None:
        backup = self.backup()
        dest = self.base / "dest"
        rows = lb.list_backups(lb.backups_root(self.repo))
        env = {"CLAUDE_CONFIG_DIR": str(self.base / "c"), "PM_OS_STATE_HOME": str(self.base / "p")}
        with mock.patch.object(lb, "find_for_install", return_value=(rows, [])):
            lb.installer_offer(dest, answer="yes", log=lambda _: None, env=env)
        self.assertEqual((dest / "local/agents-local.md").read_text(encoding="utf-8"), "# Owner context\nrole line\n")
        self.assertEqual(lb.read_manifest(backup)["roots"]["owner"]["files"], 3)

    def test_with_no_folder_named_the_plan_says_so(self) -> None:
        plan = lb.restore_plan(self.backup(), {k: v for k, v in lb.home_roots(self.base / "r").items() if k != "owner"})
        self.assertIn("owner files not restored: no PM OS folder was named", lb.plan_skips(plan))

    def test_an_older_backup_with_no_owner_archive_still_works(self) -> None:
        backup = self.backup()
        manifest = lb.read_manifest(backup)
        del manifest["roots"]["owner"], manifest["files"]["owner"]
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (backup / "owner.zip").unlink()
        self.assertEqual(lb.verify_dir(backup), [])
        roots = lb.home_roots(self.base / "restored")
        self.assertEqual(lb.apply_restore(lb.restore_plan(backup, roots), roots)["problems"], [])
        self.assertFalse(roots["owner"].exists())


if __name__ == "__main__":
    unittest.main()
