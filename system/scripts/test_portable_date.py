#!/usr/bin/env python3
"""Date arithmetic works on both BSD and GNU date.

`date -v+1d` is BSD-only. GNU date (Windows Git Bash, Linux) exits 1 on it, so on the Windows
install the P0 tab snooze in command-center-action.sh and the todo due date in
repo-drift-check.sh got an empty string (2026-11-08). Scripts now use mux_date_shift in
system/scripts/lib/mux.sh, and this test fails on any new bare `date -v` in a tracked script.

  python3 system/scripts/test_portable_date.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
MUX = SCRIPTS / "lib" / "mux.sh"

# Shell stand-ins for each date, so both forms are tested on whichever machine runs this.
BSD_DATE = r'''
date() {
  case "$1" in
    -v*) echo "bsd ${1#-v} $2" ;;
    *) echo "date: illegal option -- ${1#-}" >&2; return 1 ;;
  esac
}
'''
GNU_DATE = r'''
date() {
  case "$1" in
    -d) echo "gnu $2 $3" ;;
    *) echo "date: invalid option -- '${1#-}'" >&2; return 1 ;;
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


def shell(body: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", f'source "{MUX.as_posix()}"\n{body}'], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          env={**os.environ, "PM_OS_TEST_SANDBOX": "1", "PM_OS_MUX": "none"})


@unittest.skipUnless(BASH, "needs bash")
class ShiftForms(unittest.TestCase):
    def test_bsd_date_takes_the_v_form(self) -> None:
        out = shell(BSD_DATE + "mux_date_shift +1d '+%Y'")
        self.assertEqual(out.stdout.strip(), "bsd +1d +%Y", out.stderr)

    def test_gnu_date_takes_the_d_form(self) -> None:
        for spec, words in (("+7d", "+7 day"), ("-3H", "-3 hour"), ("+15M", "+15 minute"), ("-1S", "-1 second")):
            out = shell(GNU_DATE + f"mux_date_shift {spec} '+%Y'")
            self.assertEqual(out.stdout.strip(), f"gnu {words} +%Y", out.stderr)

    def test_unknown_unit_is_an_error_and_prints_no_date(self) -> None:
        out = shell(GNU_DATE + "mux_date_shift +1y '+%Y'")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout.strip(), "")

    def test_the_real_date_on_this_machine_moves_forward(self) -> None:
        now = shell("date '+%s'").stdout.strip()
        later = shell("mux_date_shift +1d '+%s'").stdout.strip()
        self.assertTrue(later.isdigit(), later)
        self.assertAlmostEqual(int(later) - int(now), 86400, delta=3700)   # a DST change moves it by an hour


class NoBareBsdDate(unittest.TestCase):
    def test_no_tracked_script_uses_date_v_without_a_gnu_fallback(self) -> None:
        tracked = subprocess.run(["git", "-C", str(REPO), "grep", "-nE", r"(^|[^_[:alnum:]-])date[[:space:]]+(-[[:alnum:]]+[[:space:]]+)*-v[+-]?[0-9$]",
                                  "--", "*.sh", "*.bash", "system/bin/*", "daemon/*"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        offenders = []
        for line in tracked.stdout.splitlines():
            if line.startswith("system/scripts/lib/mux.sh:"):
                continue   # the helper itself
            # `date -v-1d ... || date -d "1 day ago" ...` is already portable.
            if not re.search(r"\|\|\s*date\s+(-[a-zA-Z]+\s+)*-d\b", line):
                offenders.append(line[:160])
        self.assertEqual(offenders, [], "use mux_date_shift from system/scripts/lib/mux.sh")

    def test_both_fixed_callers_load_the_helper(self) -> None:
        for rel in ("system/scripts/command-center-action.sh", "daemon/repo-drift-check.sh"):
            if not (REPO / rel).exists():
                continue  # the drift check is optional
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn("mux_date_shift", text, rel)
            self.assertRegex(text, r"(source|\.) .*lib/(mux|cmux-inject-guard)\.sh", rel)


if __name__ == "__main__":
    unittest.main()
