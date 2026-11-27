#!/usr/bin/env python3
"""verify-kit-install.sh gives its slow suites a longer time limit.

On a Windows laptop test_report_delivery.py passes in about 21 minutes, and the plain
600 s limit killed it every time, so the install verify reported a hang that was not one.
Wrong impls caught here: (a) every suite shares the plain limit, so a slow suite that
passes is reported as timed out; (b) the longer limit leaks to every suite, so a real
hang in a fast suite waits 40 minutes; (c) the env overrides are ignored.

Run from system/scripts:  python3 -m unittest test_verify_kit_timeout
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "verify-kit-install.sh"
BASH = shutil.which("bash")


def helpers() -> str:
    """The verifier's helper block: the counters through run_test, before any check runs."""
    text = VERIFY.read_text(encoding="utf-8")
    start = text.index("pass=0; fail=0")
    end = text.index(" kit verify ($mode) in $ROOT")
    return text[start:text.rindex("\n", 0, end) + 1]


@unittest.skipUnless(BASH, "needs bash")
class SlowSuitesGetTheLongerLimit(unittest.TestCase):
    def run_block(self, body: str, **env: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.sh"
            script.write_text(f'ROOT="{Path(tmp).as_posix()}"\n{helpers()}\n{body}\n', encoding="utf-8", newline="\n")
            full_env = {k: v for k, v in os.environ.items() if not k.startswith(("SWITCHBOARD_VERIFY", "SWITCHBOARD_VERIFY"))}
            full_env.update(env)
            return subprocess.run([BASH, str(script)], env=full_env, capture_output=True, text=True,
                                  timeout=120, stdin=subprocess.DEVNULL)

    def test_the_limit_follows_the_suite_name(self) -> None:
        done = self.run_block('test_limit test_report_delivery.py; echo; test_limit test_dispatch.py; echo; '
                              'test_limit test_owner_tabs.py; echo')
        self.assertEqual(done.stdout.split(), ["2400", "2400", "600"], done.stderr)

    def test_both_limits_take_their_env_overrides(self) -> None:
        names = {"SWITCHBOARD_VERIFY_TIMEOUT": "7", "SWITCHBOARD_VERIFY_SLOW_TIMEOUT": "9"}
        # The public build renames SWITCHBOARD_ to SWITCHBOARD_: read whichever prefix the script uses.
        if "SWITCHBOARD_VERIFY_TIMEOUT" in VERIFY.read_text(encoding="utf-8"):
            names = {k.replace("SWITCHBOARD_", "SWITCHBOARD_"): v for k, v in names.items()}
        done = self.run_block('test_limit test_report_delivery.py; echo; test_limit test_owner_tabs.py; echo', **names)
        self.assertEqual(done.stdout.split(), ["9", "7"], done.stderr)

    def test_a_slow_suite_outlasting_the_plain_limit_passes_and_a_fast_one_times_out(self) -> None:
        names = {"SWITCHBOARD_VERIFY_TIMEOUT": "1", "SWITCHBOARD_VERIFY_SLOW_TIMEOUT": "30"}
        if "SWITCHBOARD_VERIFY_TIMEOUT" in VERIFY.read_text(encoding="utf-8"):
            names = {k.replace("SWITCHBOARD_", "SWITCHBOARD_"): v for k, v in names.items()}
        done = self.run_block('run_test test_report_delivery.py sleep 3\n'
                              'run_test test_owner_tabs.py sleep 3\n'
                              'echo "counts $pass $fail"', **names)
        out = done.stdout
        self.assertRegex(out, r"ok +test_report_delivery\.py \(\d+s\)", out)
        self.assertIn("FAIL  test_owner_tabs.py (timed out after 1s)", out)
        self.assertIn("counts 1 1", out)


if __name__ == "__main__":
    unittest.main()
