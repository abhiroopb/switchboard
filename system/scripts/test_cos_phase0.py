#!/usr/bin/env python3
"""cos-phase0.sh against a stub multiplexer (2026-11-12, rt-00000000003b).

The chief-of-staff skill renamed the CoS tab only through /Applications/cmux.app,
so on Windows the tab stayed "Workspace 3" and the Gmail send guard
refused the owner's "send it". cos-phase0.sh renames through whichever backend
lib/mux.sh picks. These tests never reach a live tab: PM_OS_TEST_SANDBOX=1 and a
stub CMUX_BIN that records every call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "cos-phase0.sh"
BASH = shutil.which("bash")

STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1" in
  identify) printf '%s' "$STUB_IDENTIFY" ;;
  workspace) printf '%s' "$STUB_LIST"; exit "${STUB_LIST_RC:-0}" ;;
  workspace-action) exit "${STUB_ACTION_RC:-0}" ;;
esac
"""

# owner_tabs.py stand-in: cos-target answers STUB_COS_REF, claim-cos records the claim.
OWNER_TABS_STUB = r"""import json, os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write("owner_tabs " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "cos-target":
    print(json.dumps({"ref": os.environ.get("STUB_COS_REF", "")}))
elif sys.argv[1] == "claim-cos":
    print(json.dumps({"claimed": os.environ.get("STUB_CLAIM", "1") == "1"}))
"""


@unittest.skipIf(BASH is None, "bash not on PATH")
class CosPhase0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cos-phase0-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "cmux"
        self.stub.write_text(STUB, encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.owner_tabs = self.tmp / "owner_tabs_stub.py"
        self.owner_tabs.write_text(OWNER_TABS_STUB, encoding="utf-8")
        self.cwd = self.tmp / "plain"  # not a git checkout, so no worktree refusal
        self.cwd.mkdir()

    def run_phase0(self, sub: str, caller: str = "workspace:3", tabs: list[dict] | None = None,
                   **extra) -> subprocess.CompletedProcess:
        tabs = tabs if tabs is not None else [{"ref": "workspace:3", "title": "Workspace 3", "description": ""}]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PM_OS_WORKER", "PM_OS_WORKTREE"))}
        env.update({
            "PM_OS_TEST_SANDBOX": "1", "PM_OS_MUX": "wmux", "CMUX_BIN": str(self.stub).replace("\\", "/"),
            "STUB_LOG": str(self.log).replace("\\", "/"), "PM_OS_OWNER_TABS": str(self.owner_tabs),
            "STUB_IDENTIFY": json.dumps({"caller": {"workspace_ref": caller}} if caller else {"caller": {}}),
            "STUB_LIST": json.dumps({"workspaces": tabs}),
        })
        cwd = extra.pop("_cwd", None)
        env.update(extra)
        extra = {"_cwd": cwd}
        args = [BASH, str(SCRIPT).replace("\\", "/"), *sub.split()]
        return subprocess.run(args, cwd=extra.pop("_cwd", None) or self.cwd, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_chrome_claims_cos_then_moves_the_caller_tab_to_the_top(self) -> None:
        result = self.run_phase0("chrome")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("owner_tabs claim-cos --workspace-ref workspace:3", calls)
        self.assertEqual([c for c in calls if c.startswith("workspace-action")],
                         ["workspace-action --action move-top --workspace workspace:3"])
        self.assertIn("registered as CoS", result.stdout)

    def test_another_live_cos_refuses_without_claim(self) -> None:
        result = self.run_phase0("chrome", STUB_COS_REF="workspace:1")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertFalse([c for c in self.calls() if "claim-cos" in c or c.startswith("workspace-action")])
        self.assertIn("chrome --claim", result.stderr)

    def test_claim_takes_cos_over_and_phase0_claims(self) -> None:
        for sub in ("chrome --claim", "all"):
            self.log.unlink(missing_ok=True)
            result = self.run_phase0(sub, STUB_COS_REF="workspace:1")
            self.assertIn("owner_tabs claim-cos --workspace-ref workspace:3", self.calls(), sub)

    def test_an_unreadable_tab_list_fails_closed(self) -> None:
        for extra in ({"STUB_LIST_RC": "1"}, {"STUB_LIST": "{}"}, {"STUB_LIST": "not json"}):
            self.log.unlink(missing_ok=True)
            result = self.run_phase0("chrome", **extra)
            self.assertEqual(result.returncode, 1, extra)
            self.assertFalse([c for c in self.calls() if "claim-cos" in c or c.startswith("workspace-action")], extra)

    def test_a_task_worktree_is_refused(self) -> None:
        repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(self.tmp / "wt")], check=True)
        result = self.run_phase0("chrome", _cwd=self.tmp / "wt")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("task worktree", result.stderr)
        self.assertEqual(self.run_phase0("chrome", _cwd=repo).returncode, 0, "the main checkout may claim")

    def test_a_failed_claim_is_reported(self) -> None:
        result = self.run_phase0("chrome", STUB_CLAIM="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("claim-cos did not register", result.stderr)

    def test_worker_tab_is_never_renamed(self) -> None:
        tabs = [{"ref": "workspace:9", "title": "📊 PM OS Maintenance 2", "description": "PM OS worker rt-abc"}]
        result = self.run_phase0("chrome", caller="workspace:9", tabs=tabs)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertFalse([c for c in self.calls() if "claim-cos" in c or c.startswith("workspace-action")])

    def test_worker_env_is_refused_before_any_call(self) -> None:
        for var in ("PM_OS_WORKER", "PM_OS_WORKER_TASK_ID", "PM_OS_WORKTREE"):
            self.log.unlink(missing_ok=True)
            result = self.run_phase0("chrome", **{var: "1"})
            self.assertEqual(result.returncode, 3, var)
            self.assertEqual(self.calls(), [], var)

    def test_no_caller_tab_fails_without_renaming(self) -> None:
        result = self.run_phase0("chrome", caller="")
        self.assertEqual(result.returncode, 1)
        self.assertFalse([c for c in self.calls() if c.startswith("workspace-action")])

    def test_audit_lists_generic_folder_tabs(self) -> None:
        tabs = [{"ref": "workspace:4", "title": "📁 Mystery", "description": ""},
                {"ref": "workspace:5", "title": "📅 Daily Routines 1", "description": ""}]
        result = self.run_phase0("audit", tabs=tabs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspace:4 📁 Mystery", result.stdout)
        self.assertNotIn("workspace:5", result.stdout)

    def test_skill_phase0_calls_the_helper_not_a_macos_path(self) -> None:
        skill = (HERE.parent.parent / ".agents" / "skills" / "chief-of-staff" / "SKILL.md").read_text(encoding="utf-8")
        phase0 = skill.split("## Phase 0", 1)[1].split("### Render command-center digests", 1)[0]
        self.assertIn("system/scripts/cos-phase0.sh", phase0)
        self.assertNotIn("CMUX=/Applications/cmux.app", phase0)
        self.assertNotIn("launchctl load", phase0)


if __name__ == "__main__":
    unittest.main()
