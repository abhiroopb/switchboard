#!/usr/bin/env python3
"""Deterministic fixtures for the contextual Amp tab launcher."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


HELPER = Path(
    os.environ.get(
        "CONTEXTUAL_HELPER",
        Path(__file__).with_name("cmux_new_contextual_amp_tab.py"),
    )
)
SPEC = importlib.util.spec_from_file_location("contextual_amp_tab", HELPER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

THREAD_1 = "T-11111111-1111-1111-1111-111111111111"
THREAD_2 = "T-22222222-2222-2222-2222-222222222222"


class ContextualAmpTabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "system").mkdir()
        self.workstream = self.root / "workstreams" / "alpha-workstream"
        (self.workstream / ".signals").mkdir(parents=True)
        (self.workstream / "sources").mkdir()
        (self.workstream / "CONTEXT.md").write_text("# Alpha\n")
        (self.workstream / "config.yaml").write_text(
            "name: Alpha Workstream\nemoji: 🧪\nagent_mode: high\n"
        )
        (self.workstream / ".signals" / "recent.md").write_text("# Signals\n")
        (self.workstream / "sources" / "source.md").write_text("# Source\n")
        self.env = patch.dict(
            os.environ,
            {"PM_OS_ROOT": str(self.root), "PM_OS_WORKSTREAM_SLUG": ""},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def surface(
        self,
        surface_id: str,
        checkpoint: str | None = THREAD_1,
        *,
        kind: str = "amp",
        focused: bool = False,
        cwd: str | None = None,
    ) -> dict:
        binding = {"kind": kind}
        if checkpoint is not None:
            binding["checkpoint_id"] = checkpoint
        if cwd is not None:
            binding["cwd"] = cwd
        return {
            "id": surface_id,
            "type": "terminal",
            "focused": focused,
            "resume_binding": binding,
        }

    def payloads(self, surfaces: list[dict], title: str = "🧪 Alpha Workstream"):
        return (
            {"surfaces": surfaces},
            {
                "workspaces": [
                    {
                        "id": "workspace:1",
                        "ref": "workspace:1",
                        "custom_title": title,
                        "current_directory": str(self.root),
                    }
                ]
            },
        )

    def plan(self, surfaces: list[dict], title: str = "🧪 Alpha Workstream"):
        surface_payload, workspace_payload = self.payloads(surfaces, title)
        return MODULE.make_plan(
            surface_payload,
            workspace_payload,
            "workspace:1",
            "helper",
            HELPER,
        )

    def test_one_sibling_reads_all_workstream_context(self) -> None:
        plan = self.plan(
            [
                self.surface("sibling", focused=True, cwd=str(self.root)),
                {"id": "helper", "type": "terminal"},
            ]
        )
        self.assertEqual(plan.sibling_thread_ids, (THREAD_1,))
        self.assertEqual(plan.workstream_slug, "alpha-workstream")
        self.assertEqual(plan.cwd, self.root)
        self.assertEqual(plan.mode, "high")
        self.assertIn("workstreams/alpha-workstream/CONTEXT.md", plan.prompt)
        self.assertIn("workstreams/alpha-workstream/config.yaml", plan.prompt)
        self.assertIn("workstreams/alpha-workstream/.signals/recent.md", plan.prompt)
        self.assertIn("workstreams/alpha-workstream/sources/", plan.prompt)

    def test_multiple_siblings_preserve_tab_order_and_ignore_malformed(self) -> None:
        surfaces = [
            self.surface("first", THREAD_1, cwd=str(self.root)),
            {"id": "browser", "type": "browser"},
            self.surface("bad", "not-a-thread"),
            self.surface("other-agent", THREAD_2, kind="claude"),
            self.surface("second", THREAD_2),
            self.surface("duplicate", THREAD_1),
            {"id": "helper", "type": "terminal"},
        ]
        plan = self.plan(surfaces)
        self.assertEqual(plan.sibling_thread_ids, (THREAD_1, THREAD_2))
        self.assertLess(plan.prompt.index(f"@{THREAD_1}"), plan.prompt.index(f"@{THREAD_2}"))
        self.assertEqual(plan.prompt.count(f"@{THREAD_1}"), 1)
        self.assertIn("Do not send coordination", plan.prompt)
        self.assertNotIn("Coordinate with those sibling threads", plan.prompt)

    def test_malformed_or_missing_ids_do_not_block_a_fresh_tab(self) -> None:
        for surfaces in (
            [self.surface("bad", "T-nope"), {"id": "helper", "type": "terminal"}],
            [{"id": "plain", "type": "terminal"}, {"id": "helper", "type": "terminal"}],
        ):
            with self.subTest(surfaces=surfaces):
                plan = self.plan(surfaces)
                self.assertEqual(plan.sibling_thread_ids, ())
                self.assertNotIn("@T-", plan.prompt)

    def test_missing_workstream_uses_generic_workspace_context(self) -> None:
        plan = self.plan(
            [
                self.surface("sibling", cwd=str(self.root)),
                {"id": "helper", "type": "terminal"},
            ],
            title="Unknown Workspace",
        )
        self.assertIsNone(plan.pm_os_root)
        self.assertIsNone(plan.workstream_slug)
        # No workstream config -> policy default (ultra, directive 2026-09-08).
        self.assertEqual(plan.mode, "ultra")
        self.assertIn("cmux workspace `Unknown Workspace`", plan.prompt)
        self.assertIn(f"Use `{self.root}` as the working directory.", plan.prompt)
        self.assertIn(f"@{THREAD_1}", plan.prompt)

    def test_mode_and_cwd_fall_back_safely(self) -> None:
        (self.workstream / "config.yaml").write_text(
            "name: Alpha Workstream\nagent_mode: low\n"
        )
        plan = self.plan(
            [self.surface("sibling"), {"id": "helper", "type": "terminal"}]
        )
        # Legacy low/medium coerce to the policy default (ultra, 2026-09-08).
        self.assertEqual(plan.mode, "ultra")
        self.assertEqual(plan.cwd, self.root)

    def test_exactly_one_initial_message_is_delivered(self) -> None:
        surfaces = [
            self.surface("sibling-1", THREAD_1, focused=True, cwd=str(self.root)),
            self.surface("sibling-2", THREAD_2, cwd=str(self.root)),
            {"id": "helper", "type": "terminal"},
        ]
        surface_payload, workspace_payload = self.payloads(surfaces)
        amp_calls: list[tuple[list[str], str]] = []

        def fake_rpc(_cmux, method, _params):
            if method == "surface.list":
                return surface_payload
            if method == "workspace.list":
                return workspace_payload
            if method == "surface.focus":
                return {}
            self.fail(f"unexpected RPC: {method}")

        def fake_run(command, **kwargs):
            amp_calls.append((command, kwargs.get("input", "")))
            return SimpleNamespace(returncode=0)

        with (
            patch.object(MODULE, "rpc", side_effect=fake_rpc),
            patch.object(MODULE, "find_amp", return_value="/fake/amp"),
            patch.object(MODULE.subprocess, "run", side_effect=fake_run),
        ):
            rc = MODULE.execute("workspace:1", "/fake/cmux", "helper")

        self.assertEqual(rc, 0)
        self.assertEqual(len(amp_calls), 1)
        self.assertEqual(amp_calls[0][0], ["/fake/amp", "-m", "high"])
        self.assertEqual(amp_calls[0][1].count(f"@{THREAD_1}"), 1)
        self.assertEqual(amp_calls[0][1].count(f"@{THREAD_2}"), 1)

    def test_seed_failure_notifies_existing_surface_and_removes_new_tab(self) -> None:
        surfaces = [
            self.surface("existing", THREAD_1, focused=True, cwd=str(self.root)),
            {"id": "helper", "type": "terminal"},
        ]
        surface_payload, workspace_payload = self.payloads(surfaces)
        rpc_calls: list[tuple[str, dict]] = []
        notices: list[tuple] = []

        def fake_rpc(_cmux, method, params):
            rpc_calls.append((method, params))
            if method == "surface.list":
                return surface_payload
            if method == "workspace.list":
                return workspace_payload
            return {}

        with (
            patch.object(MODULE, "rpc", side_effect=fake_rpc),
            patch.object(MODULE, "find_amp", return_value="/fake/amp"),
            patch.object(
                MODULE.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=17),
            ),
            patch.object(MODULE, "notify", side_effect=lambda *args: notices.append(args)),
        ):
            rc = MODULE.execute("workspace:1", "/fake/cmux", "helper")

        self.assertEqual(rc, 1)
        self.assertEqual(notices[0][2], "existing")
        self.assertIn(
            (
                "surface.close",
                {"workspace_id": "workspace:1", "surface_id": "helper"},
            ),
            rpc_calls,
        )

    def test_current_agent_tab_is_preserved(self) -> None:
        surfaces = [
            self.surface("live-amp", THREAD_1, focused=True, cwd=str(self.root)),
            self.surface("sibling", THREAD_2, cwd=str(self.root)),
        ]
        surface_payload, workspace_payload = self.payloads(surfaces)
        rpc_calls: list[tuple[str, dict]] = []
        notices: list[tuple] = []

        def fake_rpc(_cmux, method, params):
            rpc_calls.append((method, params))
            if method == "surface.list":
                return surface_payload
            if method == "workspace.list":
                return workspace_payload
            return {}

        with (
            patch.object(MODULE, "rpc", side_effect=fake_rpc),
            patch.object(MODULE, "notify", side_effect=lambda *args: notices.append(args)),
        ):
            rc = MODULE.execute("workspace:1", "/fake/cmux", "live-amp")

        self.assertEqual(rc, 1)
        self.assertTrue(notices)
        self.assertIn("Refusing", notices[0][4])
        self.assertFalse(any(method == "surface.close" for method, _ in rpc_calls))

    def test_shortcut_install_preserves_other_config_and_is_idempotent(self) -> None:
        config = self.root / "cmux.json"
        config.write_text(
            json.dumps(
                {
                    "app": {"globalFontMagnification": 130},
                    "vault": {"agents": [{"id": "amp"}, {"id": "pi"}]},
                }
            )
        )
        helper = Path("/installed/cmux_new_contextual_amp_tab.py")

        backup = MODULE.install_shortcuts(config, helper)
        installed = json.loads(config.read_text())
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertEqual(installed["app"], {"globalFontMagnification": 130})
        self.assertEqual(installed["vault"], {"agents": [{"id": "pi"}]})
        self.assertEqual(
            installed["actions"]["cmux.newTerminal"]["shortcut"],
            "cmd+t",
        )
        self.assertEqual(
            installed["actions"]["pm-os.newContextualAmpTabCtrl"]["shortcut"],
            "ctrl+t",
        )
        self.assertIn(
            "--spawn-split right",
            installed["actions"]["cmux.newTerminal"]["command"],
        )
        self.assertIn(
            "--spawn-split down",
            installed["actions"]["cmux.splitDown"]["command"],
        )
        self.assertEqual(installed["shortcuts"]["bindings"]["newSurface"], "disabled")
        first = config.read_text()
        self.assertIsNone(MODULE.install_shortcuts(config, helper))
        self.assertEqual(config.read_text(), first)

    def test_split_bootstrap_creates_seeded_pane_then_closes(self) -> None:
        surfaces = [
            self.surface("existing", THREAD_1),
            {"id": "bootstrap", "type": "terminal", "focused": True},
        ]
        calls: list[tuple[str, dict]] = []

        def fake_rpc(_cmux, method, params):
            calls.append((method, params))
            if method == "surface.list":
                if any(call[0] == "surface.split" for call in calls):
                    return {
                        "surfaces": surfaces
                        + [self.surface("seeded", THREAD_2)]
                    }
                return {"surfaces": surfaces}
            if method == "surface.split":
                return {"surface_id": "seeded"}
            return {}

        with patch.object(MODULE, "rpc", side_effect=fake_rpc):
            rc = MODULE.spawn_split(
                "/fake/cmux",
                "workspace:1",
                "bootstrap",
                "right",
                Path("/installed/context helper.py"),
            )

        self.assertEqual(rc, 0)
        split = next(params for method, params in calls if method == "surface.split")
        self.assertEqual(split["direction"], "right")
        self.assertEqual(split["surface_id"], "bootstrap")
        self.assertIn("'/installed/context helper.py'", split["initial_command"])
        self.assertIn("sleep 0.2; exec", split["initial_command"])
        self.assertIn("--exec workspace:1", split["initial_command"])
        self.assertIn(
            ("surface.close", {"workspace_id": "workspace:1", "surface_id": "bootstrap"}),
            calls,
        )

    def test_owner_binding_is_persisted_inside_launch_without_parent_reply(self) -> None:
        from workstream_thread_bindings import binding_file, record_binding

        running, recorded = threading.Event(), threading.Event()
        def fake_rpc(_cmux, method, params):
            if method == "workspace.list":
                return {"workspaces": [{"id": "uuid", "ref": "workspace:9"}]}
            self.assertEqual(method, "surface.list")
            self.assertEqual(params, {"workspace_id": "uuid"})
            return {"surfaces": [self.surface("new", THREAD_2 if running.is_set() else THREAD_1)]}
        def save(*args, **kwargs):
            result = record_binding(*args, **kwargs)
            recorded.set()
            return result
        def run(command):
            self.assertEqual(command, ["amp", "-m", "high"])
            running.set()
            self.assertTrue(recorded.wait(3), "binding must be durable while Amp is running")
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "uuid", "CMUX_SURFACE_ID": "new",
                                     "WORKSTREAM_THREAD_BINDINGS_FILE": str(self.root / "bindings.json")}), \
                patch.object(MODULE, "rpc", side_effect=fake_rpc), \
                patch.object(MODULE.subprocess, "run", side_effect=run), \
                patch("workstream_thread_bindings.record_binding", side_effect=save):
            self.assertEqual(MODULE.execute_owner("cmux", self.root, "alpha-workstream", ["amp", "-m", "high"]), 0)
            rows = json.loads(binding_file(self.root).read_text())["bindings"]
        self.assertEqual(list(rows), [THREAD_2])
        self.assertEqual(rows[THREAD_2]["role"], "owner")
        self.assertEqual(rows[THREAD_2]["workspace_ref"], "workspace:9")

    def test_owner_probe_failure_does_not_prevent_amp_start_or_invent_binding(self):
        with patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "uuid", "CMUX_SURFACE_ID": "new"}), \
                patch.object(MODULE, "rpc", side_effect=RuntimeError("timeout")), \
                patch.object(MODULE.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run, \
                patch("workstream_thread_bindings.record_binding") as record:
            self.assertEqual(MODULE.execute_owner("cmux", self.root, "alpha-workstream", ["amp"]), 7)
        run.assert_called_once_with(["amp"])
        record.assert_not_called()

    def test_split_preflight_failure_closes_bootstrap(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_rpc(_cmux, method, params):
            calls.append((method, params))
            if method == "surface.list":
                raise RuntimeError("socket unavailable")
            return {}

        with (
            patch.object(MODULE, "rpc", side_effect=fake_rpc),
            patch.object(MODULE, "notify"),
        ):
            rc = MODULE.spawn_split(
                "/fake/cmux",
                "workspace:1",
                "bootstrap",
                "right",
                Path("/installed/context-helper.py"),
            )

        self.assertEqual(rc, 1)
        self.assertIn(
            ("surface.close", {"workspace_id": "workspace:1", "surface_id": "bootstrap"}),
            calls,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
