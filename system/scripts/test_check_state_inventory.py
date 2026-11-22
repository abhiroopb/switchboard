#!/usr/bin/env python3
"""Tests for check-state-inventory.py.

Each test builds a throwaway repo root with the watched paths, runs two ticks
and checks which rules trip. The report script is a stub that logs its argv, so
"one line per hour" is asserted from the log, not from the record alone.

    python3 -m unittest system/scripts/test_check_state_inventory.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("check_state_inventory", SCRIPTS / "check-state-inventory.py")
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)  # type: ignore[union-attr]
winlink = inv.winlink

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class InventoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.mirror = root / "mirror"
        self.latest = self.repo / "system" / "state" / "inventory-latest.json"
        self.report_log = root / "report-calls.log"
        stub = root / "report-to-cos.sh"
        stub.write_bytes(b'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$REPORT_LOG"\necho \'{"delivered": true}\'\n')   # LF on Windows too
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        os.environ["REPORT_LOG"] = str(self.report_log)
        for rel in inv.DIRS:
            (self.repo / rel).mkdir(parents=True)
        (self.repo / "system" / "state").mkdir(parents=True, exist_ok=True)
        self.write_ledgers(tasks=12, items=40, draft=7)
        self.populate("people", 1000)
        self.populate(".state", 50)
        self.populate(".firecrawl", 20)
        self.populate("system/archive", 3)
        self.patches = [patch.object(inv, "REPO_ROOT", self.repo), patch.object(inv, "LATEST", self.latest),
                        patch.object(inv, "MIRROR_DIR", self.mirror),
                        patch.object(inv, "REPORT_SCRIPT", stub)]
        for p in self.patches:
            p.start()
        self.clock = T0

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        os.environ.pop("REPORT_LOG", None)

    # -- helpers --------------------------------------------------------------------

    def populate(self, rel: str, count: int) -> None:
        (self.repo / rel).mkdir(parents=True, exist_ok=True)
        for n in range(count):
            (self.repo / rel / f"f{n:04}.md").write_text("x\n")

    def remove(self, rel: str, count: int) -> None:
        for path in sorted(p for p in (self.repo / rel).iterdir() if p.is_file())[:count]:
            path.unlink()

    def write_ledgers(self, *, tasks: int, items: int, draft: int) -> None:
        (self.repo / "system" / "state" / "running-tasks.json").write_text(
            json.dumps({"tasks": [{"id": f"rt-{n}"} for n in range(tasks)]}))
        (self.repo / "system" / "state" / "triage-ledger.json").write_text(
            json.dumps({"items": {f"i{n}": {} for n in range(items)}, "next_draft_number": draft}))

    def tick(self, advance: timedelta = timedelta(minutes=5)) -> dict:
        self.clock += advance
        with patch.dict(os.environ, {"INVENTORY_NOW": self.clock.isoformat()}):
            return inv.tick()

    def reports(self) -> list[str]:
        return self.report_log.read_text().splitlines() if self.report_log.exists() else []

    def tripped_paths(self, record: dict) -> list[str]:
        return sorted(t["path"] for t in (record.get("alert") or {}).get("tripped", []))


class ThresholdTests(InventoryCase):
    def test_first_tick_only_seeds_and_mirrors(self) -> None:
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertEqual(record["paths"]["people"]["files"], 1000)
        self.assertEqual(record["paths"]["system/state/running-tasks.json"]["tasks"], 12)
        self.assertEqual(record["paths"]["system/state/triage-ledger.json"]["items"], 40)
        self.assertEqual(record["paths"]["system/state/triage-ledger.json"]["next_draft_number"], 7)
        self.assertTrue(self.latest.exists())
        self.assertEqual(json.loads((self.mirror / "inventory-latest.json").read_text())["paths"], record["paths"])
        self.assertEqual(self.reports(), [])

    def test_five_files_is_tolerated_and_six_trips(self) -> None:
        # Reference 1000: 6 files is 0.6 %, so only the absolute rule can trip it.
        # The reference is the previous tick, so each step is judged on its own.
        self.tick()
        self.remove("people", 5)
        self.assertTrue(self.tick()["ok"])          # 1000 -> 995: tolerated, and 995 is the new reference
        self.remove("people", 6)
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), ["people"])
        trip = record["alert"]["tripped"][0]
        self.assertEqual((trip["before"], trip["after"]), (995, 989))
        self.assertIn("lost 6 files", trip["what"])
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("people lost 6 files", self.reports()[0])
        self.assertIn("--urgent", self.reports()[0])
        self.assertIn("--status blocked", self.reports()[0])

    def test_five_percent_is_tolerated_and_six_percent_trips(self) -> None:
        # Reference 50: 2 files is 4 %, then 3 of 48 is 6.25 %; both are under the 5-file rule.
        self.tick()
        self.remove(".state", 2)
        self.assertTrue(self.tick()["ok"])
        self.remove(".state", 3)
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), [".state"])
        self.assertIn("lost 3 files (6.2%)", record["alert"]["tripped"][0]["what"])

    def test_exactly_five_percent_does_not_trip(self) -> None:
        self.tick()
        self.remove(".firecrawl", 1)   # 1 of 20 = 5.0 %
        self.assertTrue(self.tick()["ok"])

    def test_one_file_in_a_small_dir_never_trips_and_two_do_and_are_named(self) -> None:
        # A small dir that loses one file (5 -> 4, 20%) is noise, so the percent
        # rule needs 2 lost files.
        self.remove(".state", 45)
        self.assertEqual(self.tick()["paths"][".state"]["files"], 5)
        self.remove(".state", 1)
        record = self.tick()
        self.assertTrue(record["ok"], record.get("alert"))
        self.assertEqual(self.reports(), [])
        names = record["paths"][".state"]["names"]
        self.assertEqual(len(names), 4)
        (self.repo / ".state" / names[0]).unlink()
        (self.repo / ".state" / names[1]).unlink()
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), [".state"])
        trip = record["alert"]["tripped"][0]
        self.assertEqual((trip["before"], trip["after"]), (4, 2))
        self.assertIn(f"lost 2 files (50.0%): {names[0]}, {names[1]}", trip["what"])

    def test_only_state_records_file_names(self) -> None:
        # An alarm's text can reach a tracked close-out line; people/ names must not.
        record = self.tick()
        self.assertIn("names", record["paths"][".state"])
        self.assertNotIn("names", record["paths"]["people"])

    def test_growth_never_trips(self) -> None:
        self.tick()
        self.populate("system/archive", 400)
        self.write_ledgers(tasks=30, items=90, draft=12)
        self.assertTrue(self.tick()["ok"])


class StatePrunedExclusionTests(InventoryCase):
    """Scheduled pruners (rotate-logs.sh, context-snapshot.sh, prune-comms-bodies.sh,
    prune-local-retention.py) declare what they delete under .state in
    system/config/state-pruned.txt; those paths must not count toward a .state shrink,
    while any undeclared .state subdir still does."""

    OLD_LIST = ("context-snapshots/", "*.log.1", "*.log.1.gz")   # what rotate-logs.sh --print-pruned said before

    def test_pruned_list_comes_from_the_shared_declaration(self) -> None:
        self.assertEqual(inv.state_pruned(),
                         ("context-snapshots/", "*.log.1", "*.log.1.gz", "comms-bodies/", "*-demo/",
                          "automation-payloads/"))

    def test_context_snapshots_shrinking_by_ten_stays_ok(self) -> None:
        # Reference .state = 50 loose files; 24 snapshots, rotation deletes 10 (the nightly rotation case).
        self.populate(".state/context-snapshots", 24)
        first = self.tick()
        self.assertEqual(first["paths"][".state"]["files"], 50)   # snapshots never counted
        self.remove(".state/context-snapshots", 10)
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertNotIn("alert", record)
        self.assertEqual(record["paths"][".state"]["files"], 50)
        self.assertEqual(self.reports(), [])

    def test_comms_bodies_shrinking_by_twenty_five_stays_ok(self) -> None:
        # The comms-prune case: prune-comms-bodies.sh deleted 25 of 603 bodies (-23 net) and tripped.
        self.populate(".state/comms-bodies", 603)
        first = self.tick()
        self.assertEqual(first["paths"][".state"]["files"], 50)   # bodies never counted
        self.remove(".state/comms-bodies", 25)
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertNotIn("alert", record)
        self.assertEqual(record["paths"][".state"]["files"], 50)
        self.assertEqual(self.reports(), [])

    def test_undeclared_state_subdir_shrinking_by_ten_still_trips(self) -> None:
        self.populate(".state/kb-ingest-fetch", 24)   # a real .state dir nothing prunes
        first = self.tick()
        self.assertEqual(first["paths"][".state"]["files"], 74)
        self.remove(".state/kb-ingest-fetch", 10)
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), [".state"])
        trip = record["alert"]["tripped"][0]
        self.assertEqual((trip["before"], trip["after"]), (74, 64))
        self.assertEqual(len(self.reports()), 1)

    def test_dir_globs_match_demo_dirs_but_not_lookalikes(self) -> None:
        # prune-local-retention.py removes whole .state/*-demo/ dirs after 30 days.
        self.populate(".state/artifact-demo", 12)
        self.populate(".state/demo-notes", 3)         # does not match *-demo
        record = self.tick()
        self.assertEqual(record["paths"][".state"]["files"], 53)
        import shutil
        shutil.rmtree(self.repo / ".state" / "artifact-demo")
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertEqual(record["paths"][".state"]["files"], 53)
        self.remove(".state/demo-notes", 3)
        self.remove(".state", 3)
        self.assertEqual(self.tripped_paths(self.tick()), [".state"])   # 53 -> 47 counted loss

    def test_rotated_log_copies_are_not_counted_but_live_logs_are(self) -> None:
        for name in ("daemon.log", "daemon.log.1", "daemon.log.1.gz", "comms.log.1.gz"):
            (self.repo / ".state" / name).write_text("x\n")
        record = self.tick()
        self.assertEqual(record["paths"][".state"]["files"], 51)   # 50 + daemon.log only
        # A rotation replaces .log.1 with .log.1.gz and truncates the live log: no change.
        (self.repo / ".state" / "daemon.log.1").unlink()
        (self.repo / ".state" / "comms.log.1.gz").unlink()
        self.assertTrue(self.tick()["ok"])

    def test_reference_measured_without_exclusions_is_reseeded_not_compared(self) -> None:
        # The tick before the first exclusion landed counted snapshots and .gz copies; the
        # first tick after must not read the now-smaller count as a loss.
        self.populate(".state/context-snapshots", 14)
        (self.repo / ".state" / "daemon.log.1.gz").write_text("x\n")
        with patch.object(inv, "excluded_for", lambda rel: ()):
            old = self.tick()
        self.assertEqual(old["paths"][".state"]["files"], 65)
        self.assertNotIn("excluded", old["paths"][".state"])
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertEqual(record["paths"][".state"]["files"], 50)
        self.assertEqual(record["paths"][".state"]["excluded"], list(inv.excluded_for(".state")))
        self.remove(".state", 10)   # and from here on the new reference is live again
        self.assertEqual(self.tripped_paths(self.tick()), [".state"])

    def test_reference_measured_with_the_rotate_logs_only_list_is_reseeded(self) -> None:
        # Landing this change: the previous tick excluded only rotate-logs.sh's three
        # patterns, so 603 bodies were in its count. The first tick with the shared list
        # must reseed (603 fewer) instead of alerting, then compare normally.
        self.populate(".state/comms-bodies", 603)
        with patch.object(inv, "excluded_for", lambda rel: self.OLD_LIST if rel == ".state" else ()):
            old = self.tick()
        self.assertEqual(old["paths"][".state"]["files"], 653)
        self.assertEqual(old["paths"][".state"]["excluded"], list(self.OLD_LIST))
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertNotIn("alert", record)
        self.assertEqual(record["paths"][".state"]["files"], 50)
        self.assertEqual(self.reports(), [])
        self.remove(".state", 6)
        self.assertEqual(self.tripped_paths(self.tick()), [".state"])

    def test_exclusions_apply_to_state_only(self) -> None:
        self.populate("people/context-snapshots", 3)
        self.populate("people/x-demo", 2)
        (self.repo / "people" / "notes.log.1.gz").write_text("x\n")
        record = self.tick()
        self.assertEqual(record["paths"]["people"]["files"], 1006)

    def test_unreadable_declaration_excludes_nothing(self) -> None:
        inv.state_pruned.cache_clear()
        try:
            with patch.object(inv, "STATE_PRUNED_FILE", self.repo / "missing.txt"):
                self.assertEqual(inv.state_pruned(), ())
        finally:
            inv.state_pruned.cache_clear()


class StatePrunedDeclarationTests(unittest.TestCase):
    """The declaration file is the contract between the pruners and the check: every
    owner named there is a real script that reads the file, every script that reads
    the file for its own path is named there, and a pruner with no row refuses to run."""

    DECLARATION = SCRIPTS.parent / "config" / "state-pruned.txt"
    PRUNERS = {"rotate-logs.sh": SCRIPTS / "rotate-logs.sh",
               "context-snapshot.sh": SCRIPTS.parents[1] / "daemon" / "context-snapshot.sh",
               "prune-comms-bodies.sh": SCRIPTS / "prune-comms-bodies.sh",
               "prune-local-retention.py": SCRIPTS / "prune-local-retention.py",
               "automation-dispatch.sh": SCRIPTS / "lib" / "automation-dispatch.sh"}

    def rows(self) -> list[tuple[str, str]]:
        out = []
        for line in self.DECLARATION.read_text().splitlines():
            cols = line.split()
            if len(cols) >= 2 and not cols[0].startswith("#"):
                out.append((cols[0], cols[1]))
        return out

    def test_every_owner_is_a_script_that_reads_its_own_row(self) -> None:
        owners = {owner for owner, _ in self.rows()}
        self.assertEqual(owners, set(self.PRUNERS))
        for owner, script in self.PRUNERS.items():
            text = script.read_text()
            self.assertIn("state-pruned.txt", text, owner)
            self.assertIn(f'"{owner}"', text, f"{owner} must look up its own name in the declaration")

    def test_no_pruner_hardcodes_its_state_path(self) -> None:
        for owner, script in self.PRUNERS.items():
            for _, pattern in self.rows():
                if not pattern.endswith("/"):
                    continue
                literal = f'.state/{pattern.rstrip("/")}'
                code = "\n".join(l for l in script.read_text().splitlines() if not l.lstrip().startswith("#"))
                self.assertNotIn(literal, code, f"{owner} names {literal} outside the declaration")

    def test_rows_are_well_formed(self) -> None:
        patterns = [p for _, p in self.rows()]
        self.assertTrue(patterns)
        for pattern in patterns:
            self.assertFalse(pattern.startswith("/") or pattern.startswith(".state"), pattern)
            self.assertNotIn("..", pattern)


class PrunerScriptTests(unittest.TestCase):
    """prune-comms-bodies.sh and rotate-logs.sh take the .state dir they delete from the
    declaration and stop when their row is missing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "pm-os"
        (self.root / "system" / "config").mkdir(parents=True)
        (self.root / "system" / "logs").mkdir(parents=True)
        (self.root / ".state").mkdir()
        self.declaration = self.root / "system" / "config" / "state-pruned.txt"
        self.declaration.write_text((SCRIPTS.parent / "config" / "state-pruned.txt").read_text())
        self.env = {**os.environ, "PM_OS_DIR": str(self.root), "HOME": str(Path(self.tmp.name) / "home")}
        self.env.pop("COMMS_BODIES_DIR", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def old_files(self, rel: str, count: int, suffix: str = ".txt") -> list[Path]:
        directory = self.root / ".state" / rel
        directory.mkdir(parents=True, exist_ok=True)
        stamp = (T0 - timedelta(days=30)).timestamp()
        paths = []
        for n in range(count):
            path = directory / f"body{n:03}{suffix}"
            path.write_text("x\n")
            os.utime(path, (stamp, stamp))
            paths.append(path)
        return paths

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script), *args], env=self.env, capture_output=True, text=True)

    def drop_rows(self, owner: str) -> None:
        kept = [l for l in self.declaration.read_text().splitlines() if not l.startswith(owner)]
        self.declaration.write_text("\n".join(kept) + "\n")

    def test_prune_comms_bodies_deletes_from_the_declared_dir(self) -> None:
        old = self.old_files("comms-bodies", 25)
        fresh = self.root / ".state" / "comms-bodies" / "fresh.txt"
        fresh.write_text("x\n")
        result = self.run_script(SCRIPTS / "prune-comms-bodies.sh", "--apply", "--days", "14")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(p.exists() for p in old))
        self.assertTrue(fresh.exists())
        self.assertIn("deleted 25 files", (self.root / "system" / "logs" / "prune-comms-bodies.log").read_text())

    @unittest.skipIf(os.name == "nt", "Windows file names cannot contain a backslash")
    def test_prune_comms_bodies_deletes_names_with_backslashes_and_spaces(self) -> None:
        # Plain `xargs rm -f` reads the backslashes in C:\\Users\\... paths as escapes and
        # deletes nothing. NUL-separated names survive.
        old = self.old_files("comms-bodies", 25)
        stamp = (T0 - timedelta(days=30)).timestamp()
        for name in ("back\\slash.txt", "with space.txt"):
            odd = self.root / ".state" / "comms-bodies" / name
            odd.write_text("x\n")
            os.utime(odd, (stamp, stamp))
            old.append(odd)
        result = self.run_script(SCRIPTS / "prune-comms-bodies.sh", "--apply", "--days", "14")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in old if p.exists()], [])

    def test_prune_comms_bodies_refuses_without_its_row(self) -> None:
        old = self.old_files("comms-bodies", 25)
        self.drop_rows("prune-comms-bodies.sh")
        result = self.run_script(SCRIPTS / "prune-comms-bodies.sh", "--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must list exactly one dir row", result.stderr)
        self.assertTrue(all(p.exists() for p in old))

    def test_rotate_logs_prunes_the_declared_snapshot_dir_and_keeps_fourteen(self) -> None:
        snapshots = self.old_files("context-snapshots", 24, suffix=".md")
        result = self.run_script(SCRIPTS / "rotate-logs.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(p.exists() for p in snapshots), 14)
        self.assertIn("snapshots_deleted=10", result.stdout)

    def test_rotate_logs_refuses_without_its_rows(self) -> None:
        snapshots = self.old_files("context-snapshots", 24, suffix=".md")
        self.drop_rows("rotate-logs.sh")
        result = self.run_script(SCRIPTS / "rotate-logs.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing rotated", result.stderr)
        self.assertTrue(all(p.exists() for p in snapshots))

    def test_local_retention_refuses_without_its_row(self) -> None:
        self.drop_rows("prune-local-retention.py")
        spec = importlib.util.spec_from_file_location("prune_local_retention", SCRIPTS / "prune-local-retention.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        self.assertEqual(mod.declared_state_dirs(), ["*-demo"])
        with self.assertRaises(SystemExit):
            mod.declared_state_dirs(self.declaration)


class StructuralTripTests(InventoryCase):
    def test_symlinked_and_missing_paths_trip(self) -> None:
        self.tick()
        import shutil
        shutil.rmtree(self.repo / "people")
        try:
            winlink.link_path(self.repo / "people", self.repo / "people")   # the 8b3230df self-loop
        except OSError:
            # Windows without Developer Mode cannot make a symlink, and a junction needs a real
            # target, so the stand-in is a junction to another folder: still a link where a real
            # directory belongs, which is what the check trips on.
            elsewhere = self.repo / "people-elsewhere"
            elsewhere.mkdir()
            try:
                winlink.link_path(elsewhere, self.repo / "people")
            except OSError as error:
                self.skipTest(f"neither a symlink nor a junction can be made here: {error}")
        shutil.rmtree(self.repo / "system" / "archive")
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), ["people", "system/archive"])
        whats = {t["path"]: t["what"] for t in record["alert"]["tripped"]}
        self.assertEqual(whats, {"people": "is a symlink", "system/archive": "is missing"})
        self.assertEqual(len(self.reports()), 1)

    def test_draft_number_regression_and_count_drops_trip(self) -> None:
        self.tick()
        self.write_ledgers(tasks=11, items=40, draft=6)
        record = self.tick()
        whats = {(t["path"], t["what"]): (t["before"], t["after"]) for t in record["alert"]["tripped"]}
        self.assertEqual(whats, {("system/state/running-tasks.json", "tasks count went down"): (12, 11),
                                 ("system/state/triage-ledger.json", "next_draft_number went backwards"): (7, 6)})

    def test_task_ledger_trim_at_its_retention_floor_is_not_a_loss(self) -> None:
        # running-task-state.py trims the ledger to its newest delivered rows.
        # At or above the writer's floor a drop is
        # retention; the floor comes from the writer, not from this file.
        cap = inv.ledger.RETAINED_DELIVERED_ROWS
        self.assertEqual(cap, 600)
        self.write_ledgers(tasks=623, items=40, draft=7)
        record = self.tick()
        self.assertEqual(record["paths"]["system/state/running-tasks.json"]["retained"], cap)
        self.write_ledgers(tasks=604, items=40, draft=7)
        self.assertTrue(self.tick()["ok"])
        self.write_ledgers(tasks=600, items=40, draft=7)   # exactly the floor: still retention
        self.assertTrue(self.tick()["ok"])
        self.write_ledgers(tasks=599, items=40, draft=7)   # one under the floor: a loss
        record = self.tick()
        self.assertEqual([(t["what"], t["before"], t["after"]) for t in record["alert"]["tripped"]],
                         [("tasks count went down", 600, 599)])

    def test_task_ledger_below_the_floor_still_trips_on_any_drop(self) -> None:
        self.write_ledgers(tasks=300, items=40, draft=7)
        self.tick()
        self.write_ledgers(tasks=299, items=40, draft=7)
        record = self.tick()
        self.assertEqual([(t["before"], t["after"]) for t in record["alert"]["tripped"]], [(300, 299)])

    def test_triage_ledger_has_no_retention_floor(self) -> None:
        self.tick()
        self.write_ledgers(tasks=12, items=39, draft=7)
        record = self.tick()
        self.assertNotIn("retained", record["paths"]["system/state/triage-ledger.json"])
        self.assertEqual(self.tripped_paths(record), ["system/state/triage-ledger.json"])

    def test_transient_state_files_never_count(self) -> None:
        # A fresh .state holds few files, so one background-task lock or warning
        # timestamp vanishing must not read as a loss.
        locks = self.repo / ".state" / "background-task-locks"
        locks.mkdir(parents=True, exist_ok=True)
        for name in ("a.json.lock", "b.json.lock"):
            (locks / name).write_text("")
        for name in ("cmux-dynamic-updates-warning.timestamp", "cmux-skip-record", "x.lock", "y.tmp",
                     "background-refresh-pending"):
            (self.repo / ".state" / name).write_text("x\n")
        # The background-task runner's lock is a directory holding a pid file.
        runner_lock = self.repo / ".state" / "background-tasks.lock"
        runner_lock.mkdir()
        (runner_lock / "pid").write_text("123\n")
        first = self.tick()
        (runner_lock / "pid").unlink()
        runner_lock.rmdir()
        (self.repo / ".state" / "background-refresh-pending").unlink()
        for path in [*locks.iterdir(), *(self.repo / ".state").glob("cmux-*"), self.repo / ".state" / "x.lock",
                     self.repo / ".state" / "y.tmp"]:
            path.unlink()
        record = self.tick()
        self.assertTrue(record["ok"], record.get("alert"))
        self.assertEqual(record["paths"][".state"]["files"], first["paths"][".state"]["files"])

    def test_a_ledger_that_never_had_a_count_is_not_a_loss(self) -> None:
        # A triage-ledger.json that never existed leaves the reference with no count
        # ("? -> missing"). Nothing was there to lose.
        ledger_path = self.repo / "system" / "state" / "triage-ledger.json"
        ledger_path.write_text("{not json")
        self.tick()
        ledger_path.unlink()
        self.assertTrue(self.tick()["ok"])

    def test_unreadable_ledger_trips(self) -> None:
        self.tick()
        (self.repo / "system" / "state" / "triage-ledger.json").write_text("{not json")
        record = self.tick()
        self.assertEqual(self.tripped_paths(record), ["system/state/triage-ledger.json"])


class RepeatAndRecoveryTests(InventoryCase):
    def test_alert_repeats_at_most_once_per_hour_while_the_drop_persists(self) -> None:
        self.tick()
        self.remove("people", 100)
        self.tick()                                   # trips, sends
        for _ in range(11):                           # 55 more minutes of ticks: silent
            record = self.tick()
            self.assertFalse(record["ok"])
        self.assertEqual(len(self.reports()), 1)
        self.tick()                                   # 60 minutes after the first send: one more line
        self.assertEqual(len(self.reports()), 2)
        self.tick()
        self.assertEqual(len(self.reports()), 2)
        # The comparison stays pinned to the pre-drop count: before is still 1000.
        record = self.tick()
        self.assertEqual(record["alert"]["tripped"][0]["before"], 1000)
        self.assertEqual(record["alert"]["since"], inv.iso(T0 + timedelta(minutes=10)))

    def test_further_loss_while_an_alert_is_active_is_judged_against_the_pre_drop_reference(self) -> None:
        # Wrong impl: move the reference to the shrunken count on every tick, so
        # the line stops repeating and later small losses pass unseen.
        self.tick()
        self.remove("people", 100)
        self.tick()
        for _ in range(3):
            self.remove("people", 4)   # under both rules on their own
            record = self.tick()
        self.assertEqual((record["alert"]["tripped"][0]["before"], record["alert"]["tripped"][0]["after"]), (1000, 888))

    def test_alert_clears_when_the_files_come_back(self) -> None:
        self.tick()
        moved = sorted((self.repo / "people").iterdir())[:50]
        stash = Path(self.tmp.name) / "stash"
        stash.mkdir()
        for path in moved:
            path.rename(stash / path.name)
        self.assertFalse(self.tick()["ok"])
        for path in stash.iterdir():
            path.rename(self.repo / "people" / path.name)
        record = self.tick()
        self.assertTrue(record["ok"])
        self.assertNotIn("alert", record)
        self.assertEqual(len(self.reports()), 1)

    def test_removing_the_latest_file_accepts_the_new_counts(self) -> None:
        self.tick()
        self.remove("people", 100)
        self.assertFalse(self.tick()["ok"])
        self.latest.unlink()
        self.assertTrue(self.tick()["ok"])
        self.assertEqual(json.loads(self.latest.read_text())["paths"]["people"]["files"], 900)


if __name__ == "__main__":
    unittest.main()
