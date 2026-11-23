#!/usr/bin/env python3
"""Tests for scoped_test_env: a test module's env stubs and the
script modules it loads stay inside that module.

Each test names the wrong implementation it catches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import scoped_test_env  # noqa: E402

KEY = "SWITCHBOARD_SCOPED_TEST_ENV_PROBE"


class IsolatedLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A throwaway sibling module under SCRIPTS would touch the repo, so point the
        # helper at a temp scripts tree for these unit tests.
        self.scripts = Path(self.tmp.name)
        (self.scripts / "probe_sibling.py").write_text(f"import os\nSEEN = os.environ.get({KEY!r})\n")
        sys.path.insert(0, str(self.scripts))
        self.addCleanup(sys.path.remove, str(self.scripts))
        self.addCleanup(sys.modules.pop, "probe_sibling", None)
        self.real_scripts = scoped_test_env.SCRIPTS
        scoped_test_env.SCRIPTS = self.scripts.resolve()
        self.addCleanup(setattr, scoped_test_env, "SCRIPTS", self.real_scripts)

    def test_env_and_new_sibling_imports_leave_with_the_block(self) -> None:
        # Wrong impl (the leak): os.environ.update at import, so the stub outlives the module
        # and the sibling dispatch imported stays in sys.modules bound to that env.
        os.environ.pop(KEY, None)
        with scoped_test_env.isolated_load({KEY: "inside"}):
            import probe_sibling
            self.assertEqual(probe_sibling.SEEN, "inside")
        self.assertNotIn(KEY, os.environ)
        self.assertNotIn("probe_sibling", sys.modules)
        self.assertEqual(probe_sibling.SEEN, "inside", "the loader keeps its own copy")

    def test_an_earlier_import_is_not_reused_and_comes_back(self) -> None:
        # Wrong impl: reuse a sibling another suite already imported, so this suite's
        # dispatch shares a module bound to the other suite's state dir.
        os.environ[KEY] = "other suite"
        self.addCleanup(os.environ.pop, KEY, None)
        import probe_sibling as earlier
        with scoped_test_env.isolated_load({KEY: "mine"}):
            import probe_sibling as mine
        self.assertIsNot(mine, earlier)
        self.assertEqual(mine.SEEN, "mine")
        self.assertIs(sys.modules["probe_sibling"], earlier)
        self.assertEqual(os.environ[KEY], "other suite")

    def test_module_hooks_apply_env_only_between_setup_and_teardown(self) -> None:
        # Wrong impl: hooks that set the env and never restore it.
        os.environ.pop(KEY, None)
        set_up, tear_down = scoped_test_env.module_hooks({KEY: "running"})
        set_up()
        self.assertEqual(os.environ[KEY], "running")
        tear_down()
        self.assertNotIn(KEY, os.environ)


@unittest.skipUnless((SCRIPTS / "test_owner_tabs.py").is_file(), "the portable kit ships test_dispatch.py only")
class SuiteImportTests(unittest.TestCase):
    def test_importing_the_router_suites_leaves_env_and_sys_modules_unchanged(self) -> None:
        # Wrong impl (rt-00000000006b follow-up): test_owner_tabs and test_dispatch stub
        # os.environ at import, so whichever imports second wins and the first suite's
        # CloseoutRecordTests reaper test reads the other suite's state dir.
        probe = textwrap.dedent("""
            import json, os, sys
            before_env = dict(os.environ)
            before_mods = set(sys.modules)
            import test_owner_tabs, test_dispatch
            changed = sorted(k for k in set(before_env) | set(os.environ) if before_env.get(k) != os.environ.get(k))
            leaked = sorted(m for m in ("owner_tabs", "dispatch", "link_titles", "system_owners") if m in sys.modules)
            print(json.dumps({"changed": changed, "leaked": leaked,
                              "separate": test_owner_tabs.TMP != test_dispatch.TMP}))
        """)
        out = subprocess.run([sys.executable, "-c", probe], cwd=SCRIPTS, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["changed"], [])
        self.assertEqual(got["leaked"], [])
        self.assertTrue(got["separate"])


if __name__ == "__main__":
    unittest.main()
