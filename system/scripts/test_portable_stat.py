#!/usr/bin/env python3
"""Reading a file's mtime, size, and mode works on both GNU and BSD stat.

GNU `stat -f` means "report the filesystem": it prints a block of text to stdout, then
fails. On Windows and Linux, `$(stat -f %m f || echo 0)` therefore captured that text, and
the arithmetic reading it died on `File:`. That silently broke log rotation, the Megadesk
freshness check, and two stale-lock reclaims on the Windows install (2026-11-08). Scripts
now go through mux_mtime, mux_size, and mux_mode in system/scripts/lib/mux.sh.

Every case runs in a throwaway tree with a throwaway HOME, and no case reaches a live tab.

  python3 system/scripts/test_portable_stat.py
"""
from __future__ import annotations

import os
import re
import shutil
import stat as stat_module
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
MUX = SCRIPTS / "lib" / "mux.sh"

# Shell stand-ins for each stat, so both forms are tested on whichever machine runs this.
BSD_STAT = r'''
stat() {
  [ "$1" = -f ] || { echo "stat: illegal option -- ${1#-}" >&2; return 1; }
  case "$2" in %m) echo 1111 ;; %z) echo 2222 ;; %Lp) echo 640 ;; *) return 1 ;; esac
}
'''
GNU_STAT = r'''
stat() {
  case "$1" in
    -f) printf '  File: "%s"\n    ID: 0 Namelen: 255\n' "$3"; return 1 ;;
    -c) case "$2" in %Y) echo 3333 ;; %s) echo 4444 ;; %a) echo 600 ;; *) return 1 ;; esac ;;
    *) return 1 ;;
  esac
}
'''


def find_bash() -> str | None:
    """Git Bash or a real Unix bash; WSL's System32 launcher cannot read these Windows paths."""
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()


def run_bash(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, *argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PM_OS_TEST_SANDBOX": "1", "PM_OS_MUX": "none", **(env or {})},
                          timeout=120)


def age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


@unittest.skipUnless(BASH, "no usable bash")
class HelperTests(unittest.TestCase):
    def helpers(self, stub: str, target: str = "/any/file") -> list[str]:
        script = f'source "{MUX.as_posix()}"; {stub}\n' \
                 f'mux_mtime "{target}"; mux_size "{target}"; mux_mode "{target}"'
        return run_bash("-c", script).stdout.splitlines()

    def test_bsd_stat_gives_clean_values(self) -> None:
        self.assertEqual(self.helpers(BSD_STAT), ["1111", "2222", "640"])

    def test_gnu_stat_gives_clean_values_with_no_filesystem_text(self) -> None:
        self.assertEqual(self.helpers(GNU_STAT), ["3333", "4444", "600"])

    def test_this_machines_stat_reads_a_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_bytes(b"x" * 1234)
            os.utime(path, (1700000000, 1700000000))
            mtime, size, mode = self.helpers("", path.as_posix())
            self.assertEqual((mtime, size), ("1700000000", "1234"))
            self.assertRegex(mode, r"^[0-7]{3,4}$")

    def test_missing_file_fails_with_nothing_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = (Path(tmp) / "nope").as_posix()
            result = run_bash("-c", f'source "{MUX.as_posix()}"; mux_mtime "{missing}"')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            # What every caller writes: the fallback has to be the whole value.
            result = run_bash("-c", f'source "{MUX.as_posix()}"; echo "$(mux_mtime "{missing}" || echo 0)"')
            self.assertEqual(result.stdout.strip(), "0")


class NoBareBsdIdiomsTests(unittest.TestCase):
    """The guard for the next script: both idioms below passed on macOS and failed here."""

    @staticmethod
    def shell_files() -> list[Path]:
        listed = subprocess.run(["git", "ls-files", "*.sh"], cwd=REPO, capture_output=True, text=True, check=True)
        return [REPO / line for line in listed.stdout.splitlines() if line]

    def offending_lines(self, pattern: str, allow: tuple[str, ...] = ()) -> list[str]:
        found = []
        for path in self.shell_files():
            rel = path.relative_to(REPO).as_posix()
            if rel in allow or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                # A line that already falls back to GNU `stat -c` is portable.
                if not line.lstrip().startswith("#") and re.search(pattern, line) and "stat -c" not in line:
                    found.append(f"{rel}:{number}: {line.strip()}")
        return found

    def test_only_the_helper_calls_bsd_stat(self) -> None:
        self.assertEqual(self.offending_lines(r"\bstat\s+-f\b", allow=("system/scripts/lib/mux.sh",)), [],
                         "use mux_mtime, mux_size, or mux_mode from system/scripts/lib/mux.sh")

    def test_no_default_on_an_array_length(self) -> None:
        # ${#a[@]:-0} is a bad substitution on current bash and needs no default anyway.
        self.assertEqual(self.offending_lines(r"\$\{#\w+\[[@*]\]:"), [])


@unittest.skipUnless(BASH, "no usable bash")
class RotateLogsTests(unittest.TestCase):
    def test_oversized_log_rotates_and_a_small_one_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pm-os"
            (root / "system" / "config").mkdir(parents=True)
            shutil.copy(SCRIPTS.parent / "config" / "state-pruned.txt", root / "system" / "config")
            state = root / ".state"
            state.mkdir()
            big, small = state / "big.log", state / "small.log"
            big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            small.write_text("keep\n")
            if os.name != "nt":
                big.chmod(0o640)
            result = run_bash((SCRIPTS / "rotate-logs.sh").as_posix(),
                              env={"PM_OS_DIR": root.as_posix(), "HOME": (Path(tmp) / "home").as_posix()})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rotated=1 ", result.stdout)
            self.assertTrue((state / "big.log.1.gz").exists())
            self.assertEqual(big.stat().st_size, 0)
            self.assertEqual(small.read_text(), "keep\n")
            self.assertFalse((state / "small.log.1.gz").exists())
            if os.name != "nt":
                self.assertEqual(stat_module.S_IMODE(big.stat().st_mode), 0o640)


@unittest.skipUnless(BASH, "no usable bash")
class CosReviewFeedThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "repo"
        (root / "system" / "scripts").mkdir(parents=True)
        self.state = root / "system" / "state"
        (self.state / "worker-dispatch").mkdir(parents=True)
        self.feed = self.state / "cos-review-feed.json"
        self.built = self.state / "built"
        self.lock = self.state / "worker-dispatch" / "cos-review-feed.lock"
        (root / "system" / "scripts" / "cos_review_feed.py").write_text(
            f"open({self.built.as_posix()!r}, 'w').write('x')\n", encoding="utf-8")
        self.root = root

    def rebuild(self, *args: str) -> subprocess.CompletedProcess:
        result = run_bash((SCRIPTS / "cos-review-feed-rebuild.sh").as_posix(), *args,
                          env={"REPO_ROOT": self.root.as_posix(), "STATE_DIR": self.state.as_posix()})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def test_fresh_feed_is_left_alone(self) -> None:
        self.feed.write_text("{}")
        self.rebuild("--min-age", "300")
        self.assertFalse(self.built.exists())

    def test_old_feed_rebuilds(self) -> None:
        self.feed.write_text("{}")
        age(self.feed, 1000)
        self.rebuild("--min-age", "300")
        self.assertTrue(self.built.exists())

    def test_live_lock_waits(self) -> None:
        self.lock.mkdir()
        self.rebuild()
        self.assertFalse(self.built.exists())
        self.assertTrue(self.lock.exists())

    def test_crashed_builders_lock_is_reclaimed(self) -> None:
        self.lock.mkdir()
        age(self.lock, 1000)
        self.rebuild()
        self.assertTrue(self.built.exists())
        self.assertFalse(self.lock.exists())


@unittest.skipUnless(BASH, "no usable bash")
class CmuxLockTests(unittest.TestCase):
    """An orphaned lock (no pid file, left by a crash or reboot) deadlocks every cmux_cmd
    unless its age can be read."""

    def call(self, lock: Path) -> subprocess.CompletedProcess:
        script = f'source "{(REPO / "system" / "cmux-helpers.sh").as_posix()}"\n' \
                 'stub_cmux() { echo called; }\nCMUX_BIN=stub_cmux\ncmux_cmd ping\n'
        # REPO_ROOT pins the inject guard, which re-sources mux.sh, to this tree. Without it a
        # worktree run would test main's helper (PM_OS_ROOT) instead of the branch's.
        return run_bash("-c", script, env={"CMUX_LOCK_DIR": lock.as_posix(), "CMUX_BIN": "stub_cmux",
                                           "REPO_ROOT": REPO.as_posix()})

    def test_orphaned_lock_older_than_a_minute_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "pm-os-cmux.lock"
            lock.mkdir()
            age(lock, 600)
            result = self.call(lock)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("called", result.stdout)
            self.assertFalse(lock.exists())


@unittest.skipUnless(BASH, "no usable bash")
@unittest.skipUnless((REPO / "daemon" / "repo-drift-check.sh").exists(), "the optional drift check is not installed")
class RepoDriftCheckTests(unittest.TestCase):
    def test_only_a_skill_newer_than_the_repo_counts_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pm-os"
            home = Path(tmp) / "home"
            for rel, source in (("daemon/repo-drift-check.sh", REPO / "daemon" / "repo-drift-check.sh"),
                                ("system/scripts/lib/mux.sh", MUX)):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, root / rel)
            mind = home / "Development" / "my-blog"
            for name in ("newer", "older"):
                (mind / "skills" / name).mkdir(parents=True)
                (mind / "skills" / name / "SKILL.md").write_text("x\n")
                live = home / ".agents" / "skills" / name / "SKILL.md"
                live.parent.mkdir(parents=True)
                live.write_text("x\n")
            # The repo's last commit sits between the two live copies.
            older = home / ".agents" / "skills" / "older" / "SKILL.md"
            os.utime(older, (1500000000, 1500000000))
            git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
            for args in (["init", "-q"], ["add", "."],
                         ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x"]):
                subprocess.run(["git", *args], cwd=mind, env=git_env, check=True, capture_output=True)
            result = run_bash((root / "daemon" / "repo-drift-check.sh").as_posix(), env={
                "HOME": home.as_posix(), "DRY_RUN": "1", "SKILL_THRESHOLD": "1", "AGE_NUDGE_DAYS": "99999",
                "KIT_DIR": (Path(tmp) / "no-kit").as_posix()})
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (root / ".state" / "repo-drift-check.log").read_text(encoding="utf-8")
            self.assertIn("my-blog drift: 1 stale published skill(s)", log)
            self.assertIn("Refresh my-blog: 1 published skills", log)


if __name__ == "__main__":
    unittest.main()
