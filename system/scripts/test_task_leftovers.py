#!/usr/bin/env python3
"""Tests for task_leftovers.py, with fake process lists only (no real process is read or stopped)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_leftovers as tl  # noqa: E402
from task_leftovers import Proc  # noqa: E402

DONE = "rt-000000000045"
LIVE = "rt-000000000051"
OLD = "rt-000000000052"
SELF_PID = 9000


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "switchboard"
        self.worktrees = root / "worktrees"
        (self.repo / ".amp" / "in").mkdir(parents=True)
        self.worktrees.mkdir()
        (self.worktrees / LIVE).mkdir()
        self.wt_done = self.worktrees / DONE
        self.wt_done.mkdir()
        self.wt_live = self.worktrees / LIVE
        self.wt_old = self.worktrees / OLD   # removed: never created on disk
        self.log_path = root / "leftover-stops.jsonl"
        self.killed: list[int] = []
        env = mock.patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWITCHBOARD_LEFTOVER_SWEEP", None)

    def killer(self, victim):
        self.killed.append(victim["pid"])
        return ""

    def tasks(self, done_reaped: bool = True):
        done = {"id": DONE, "status": "done", "worktree": str(self.wt_done)}
        if done_reaped:
            done["reaped_at"] = "2026-11-14T20:00:00Z"
        return [done, {"id": LIVE, "status": "active", "worktree": str(self.wt_live)}]

    def me(self) -> list[Proc]:
        """This process (a python under dispatch.py) and its ancestors."""
        return [Proc(SELF_PID - 1, 1, "bash", "bash command-center-loop.sh", created=10.0),
                Proc(SELF_PID, SELF_PID - 1, "python3", "python3 dispatch.py sweep", created=20.0)]

    def run_sweep(self, procs, tasks=None):
        with mock.patch("os.getpid", return_value=SELF_PID):
            return tl.sweep(self.tasks() if tasks is None else tasks, repo_root=self.repo, worktrees_root=self.worktrees,
                            procs=self.me() + procs, log_path=self.log_path, killer=self.killer, log=lambda m: None)

    def run_worktree(self, procs, task_id=DONE, tasks=None):
        with mock.patch("os.getpid", return_value=SELF_PID):
            return tl.stop_in_worktree(task_id, self.worktrees / task_id, self.tasks() if tasks is None else tasks,
                                       repo_root=self.repo, worktrees_root=self.worktrees, procs=self.me() + procs,
                                       log_path=self.log_path, killer=self.killer, log=lambda m: None)


def vite_tree(task_id: str, base_pid: int = 100, cwd: str = r"C:\Users\x\AppData\Local\switchboard\prototyping\react-starter"):
    """The 2026-11-13 shape: cmd /c npm.cmd run dev -> node npm -> cmd /c vite -> node vite, with conhosts."""
    t = 1000.0
    return [
        Proc(base_pid, 16768, "cmd.exe", "C:\\WINDOWS\\system32\\cmd.exe /c npm.cmd run dev -- --port 5173 --strictPort",
             cwd=cwd, created=t, env_task=task_id),
        Proc(base_pid + 1, base_pid, "conhost.exe", "\\??\\C:\\WINDOWS\\system32\\conhost.exe 0x4", created=t + 1),
        Proc(base_pid + 2, base_pid, "node.exe", '"C:\\Program Files\\nodejs\\node.exe" npm-cli.js run dev -- --port 5173 --strictPort',
             cwd=cwd, created=t + 1, env_task=task_id),
        Proc(base_pid + 3, base_pid + 2, "cmd.exe", "C:\\WINDOWS\\system32\\cmd.exe /d /s /c vite --port 5173 --strictPort",
             cwd=cwd, created=t + 2, env_task=task_id),
        Proc(base_pid + 4, base_pid + 3, "node.exe", "node node_modules/vite/bin/vite.js --port 5173 --strictPort",
             cwd=cwd, created=t + 3, env_task=task_id),
        Proc(base_pid + 5, base_pid + 4, "esbuild.exe", "esbuild.exe --service=0.21.5 --ping", cwd=cwd, created=t + 4,
             env_task=task_id),
    ]


class DevServers(Base):
    def test_finished_tasks_vite_tree_is_stopped_leaves_first(self) -> None:
        result = self.run_sweep(vite_tree(DONE))
        self.assertEqual(result["stopped"], 6)
        self.assertEqual(sorted(self.killed), [100, 101, 102, 103, 104, 105])
        self.assertLess(self.killed.index(105), self.killed.index(100))   # children before the wrapper
        self.assertEqual(result["tasks"], [DONE])

    def test_live_tasks_dev_server_is_spared(self) -> None:
        result = self.run_sweep(vite_tree(LIVE))
        self.assertEqual(self.killed, [])
        self.assertEqual(result["stopped"], 0)

    def test_terminal_row_not_yet_reaped_is_left_alone(self) -> None:
        self.run_sweep(vite_tree(DONE), tasks=self.tasks(done_reaped=False))
        self.assertEqual(self.killed, [])

    def test_blocked_row_reap_left_open_is_left_alone(self) -> None:
        tasks = [{"id": DONE, "status": "blocked", "worktree": str(self.wt_done)}]
        self.run_sweep(vite_tree(DONE), tasks=tasks)
        self.assertEqual(self.killed, [])

    def test_env_tag_alone_does_not_stop_non_dev_server(self) -> None:
        mcp = Proc(300, 1, "node.exe", "node chrome-devtools-mcp/build/index.js", cwd=r"C:\elsewhere", created=5.0, env_task=DONE)
        self.run_sweep([mcp])
        self.assertEqual(self.killed, [])

    def test_worktree_removal_does_not_touch_dev_server_elsewhere(self) -> None:
        self.run_worktree(vite_tree(DONE))
        self.assertEqual(self.killed, [])


class PathRule(Base):
    def test_tail_monitor_on_removed_worktree_is_stopped(self) -> None:
        """2026-11-14: tail plus grep following a log in a worktree removed the day before, msys path spelling."""
        posix = str(self.wt_old).replace("\\", "/")
        if posix[1:3] == ":/":   # Windows temp dir: Git Bash spells C:/x as /c/x
            posix = "/" + posix[0].lower() + posix[2:]
        log = f"{posix}/.amp/in/{OLD}/start-day.log"
        tail = Proc(400, 77, "tail.exe", f"/usr/bin/tail -f {log}", cwd=r"C:\Users\x", created=50.0)
        grep = Proc(401, 77, "grep.exe", "/usr/bin/grep --line-buffered -E done|failed", cwd=r"C:\Users\x", created=50.0)
        result = self.run_sweep([tail, grep], tasks=self.tasks())   # OLD has no ledger row, its worktree is gone
        self.assertEqual(self.killed, [400])
        self.assertEqual(result["tasks"], [OLD])

    def test_removed_worktree_under_an_older_recorded_root_is_found(self) -> None:
        legacy = Path(self.tmp.name) / "old-root"
        tasks = self.tasks() + [{"id": OLD, "status": "done", "reaped_at": "2026-11-01T00:00:00Z", "worktree": str(legacy / OLD)}]
        tail = Proc(420, 77, "tail.exe", f"tail -f {legacy / OLD / 'run.log'}", cwd="/", created=5.0)
        self.run_sweep([tail], tasks=tasks)
        self.assertEqual(self.killed, [420])

    def test_unknown_row_with_worktree_on_disk_is_left_alone(self) -> None:
        stray = "rt-00000000006d"
        (self.worktrees / stray).mkdir()
        proc = Proc(410, 1, "bash.exe", "bash", cwd=str(self.worktrees / stray), created=5.0)
        self.run_sweep([proc])
        self.assertEqual(self.killed, [])

    def test_cwd_inside_worktree_is_stopped_with_children(self) -> None:
        sh = Proc(500, 1, "bash.exe", "bash -c 'while true; do sleep 60; done'", cwd=str(self.wt_done / "system"), created=5.0)
        sleep = Proc(501, 500, "sleep.exe", "sleep 60", cwd=str(self.wt_done / "system"), created=6.0)
        self.run_worktree([sh, sleep])
        self.assertEqual(self.killed, [501, 500])

    def test_scratch_under_main_amp_in_counts(self) -> None:
        proc = Proc(510, 1, "python3.exe", "python3 -m http.server 8000", cwd=str(self.repo / ".amp" / "in" / DONE), created=5.0)
        self.run_sweep([proc])
        self.assertEqual(self.killed, [510])

    def test_open_file_inside_worktree_counts(self) -> None:
        proc = Proc(520, 1, "node", "node watcher.js", cwd="/tmp", created=5.0, open_paths=(str(self.wt_done / "a.log"),))
        self.run_worktree([proc])
        self.assertEqual(self.killed, [520])

    def test_similar_task_id_prefix_is_not_a_match(self) -> None:
        longer = Proc(530, 1, "bash.exe", "bash", cwd=str(self.worktrees / (DONE + "9")), created=5.0)
        self.run_worktree([longer])
        self.assertEqual(self.killed, [])

    def test_dead_worker_relaunch_removal_spares_nothing_of_others(self) -> None:
        """drop_worktree for a live row being relaunched: its own worktree's processes go."""
        tasks = [{"id": DONE, "status": "active", "worktree": str(self.wt_done)}]
        proc = Proc(540, 1, "node.exe", "node server.js", cwd=str(self.wt_done), created=5.0, env_task=DONE)
        self.run_worktree([proc], tasks=tasks)
        self.assertEqual(self.killed, [540])


class NeverStopped(Base):
    def test_protected_helpers_survive_even_when_tagged_and_inside(self) -> None:
        inside = str(self.wt_done)
        procs = [
            Proc(600, 1, "python3.exe", "python3 system/scripts/owner_browser.py broker", cwd=inside, created=5.0, env_task=DONE),
            Proc(601, 1, "python3.exe", "python3 C:\\x\\switchboard\\system\\scripts\\mux\\note-links.py serve --port 7472", cwd=inside, created=5.0),
            Proc(602, 1, "python3.exe", "python3 system/scripts/keep_awake.py --hold", cwd=inside, created=5.0),
            Proc(603, 1, "python3.exe", "python3 system/scripts/dispatch.py release", cwd=inside, created=5.0),
            Proc(604, 1, "bash.exe", "bash system/scripts/report-waiter.sh rt-000000000045", cwd=inside, created=5.0),
            Proc(605, 1, "brave.exe", "brave.exe --remote-debugging-port=9333", cwd=inside, created=5.0),
            Proc(606, 1, "wmux.exe", "wmux.exe", cwd=inside, created=5.0),
            Proc(607, 1, "claude.exe", "claude --resume x", cwd=inside, created=5.0, env_task=DONE),
            Proc(608, 1, "Obsidian.exe", "Obsidian.exe", cwd=inside, created=5.0),
            Proc(609, 1, "node.exe", "node C:\\Users\\x\\AppData\\Local\\Programs\\wmux\\resources\\cli-bin\\wmux.js", cwd=inside, created=5.0),
        ]
        result = self.run_worktree(procs)
        self.assertEqual(self.killed, [])
        self.assertEqual(result["spared"], len(procs))

    def test_self_and_ancestors_survive(self) -> None:
        me = [Proc(SELF_PID - 1, 1, "bash", "bash", cwd=str(self.wt_done), created=10.0),
              Proc(SELF_PID, SELF_PID - 1, "python3", "python3 x.py", cwd=str(self.wt_done), created=20.0)]
        with mock.patch("os.getpid", return_value=SELF_PID):
            tl.stop_in_worktree(DONE, self.wt_done, self.tasks(), repo_root=self.repo, worktrees_root=self.worktrees,
                                procs=me, killer=self.killer, log=lambda m: None)
        self.assertEqual(self.killed, [])

    def test_live_tasks_process_inside_a_finished_worktree_survives(self) -> None:
        proc = Proc(700, 1, "bash.exe", "bash", cwd=str(self.wt_done), created=5.0, env_task=LIVE)
        self.run_worktree([proc])
        self.assertEqual(self.killed, [])

    def test_process_naming_both_finished_and_live_worktrees_survives(self) -> None:
        proc = Proc(710, 1, "tail.exe", f"tail -f {self.wt_done}/a.log {self.wt_live}/b.log", cwd="/", created=5.0)
        self.run_sweep([proc])
        self.assertEqual(self.killed, [])

    def test_owner_shell_attached_to_a_live_terminal_survives(self) -> None:
        host = Proc(800, 1, "wmux.exe", "wmux.exe", created=1.0)
        shell = Proc(801, 800, "bash.exe", "bash --login", cwd=str(self.wt_done), created=5.0)
        self.run_worktree([host, shell])
        self.assertEqual(self.killed, [])

    def test_reused_parent_pid_is_not_followed(self) -> None:
        root = Proc(900, 1, "bash.exe", "bash", cwd=str(self.wt_done), created=100.0)
        older_child = Proc(901, 900, "node.exe", "node unrelated.js", cwd=r"C:\other", created=50.0)   # pid 900 reused later
        self.run_worktree([root, older_child])
        self.assertEqual(self.killed, [900])

    def test_child_of_a_victim_that_is_protected_survives(self) -> None:
        root = Proc(910, 1, "bash.exe", "bash", cwd=str(self.wt_done), created=10.0)
        broker = Proc(911, 910, "python3.exe", "python3 owner_browser.py broker", cwd=r"C:\x", created=11.0)
        self.run_worktree([root, broker])
        self.assertEqual(self.killed, [910])

    def test_children_of_a_protected_helper_survive(self) -> None:
        """Review 2026-11-14: the waiter was spared but its report_queue.py reader and sleep were not."""
        inside = str(self.wt_done)
        waiter = Proc(950, 1, "bash.exe", f"bash {inside}/system/scripts/report-waiter.sh {DONE}", cwd=inside, created=10.0)
        reader = Proc(951, 950, "python3.exe", f"python3 {inside}/system/scripts/report_queue.py next", cwd=inside, created=11.0)
        nap = Proc(952, 950, "sleep.exe", "sleep 5", cwd=inside, created=12.0)
        self.run_worktree([waiter, reader, nap])
        self.assertEqual(self.killed, [])

    def test_shells_under_mintty_explorer_or_a_live_claude_survive(self) -> None:
        inside = str(self.wt_done)
        procs = [Proc(960, 1, "mintty.exe", "mintty.exe -", created=1.0),
                 Proc(961, 960, "bash.exe", "bash --login -i", cwd=inside, created=2.0),
                 Proc(962, 1, "explorer.exe", "explorer.exe", created=1.0),
                 Proc(963, 962, "cmd.exe", "cmd.exe", cwd=inside, created=2.0),
                 Proc(964, 1, "claude.exe", "claude", cwd=inside, created=1.0),   # a worker judged dead that is not
                 Proc(965, 964, "node.exe", "node server.js", cwd=inside, created=2.0, env_task=DONE)]
        self.run_worktree(procs)
        self.assertEqual(self.killed, [])

    def test_orphan_reparented_to_launchd_is_still_stopped(self) -> None:
        launchd = Proc(1, 0, "launchd", "/sbin/launchd", created=0.0)
        orphan = Proc(970, 1, "node", "node server.js", cwd=str(self.wt_done), created=5.0)
        self.run_worktree([launchd, orphan])
        self.assertEqual(self.killed, [970])

    def test_unreadable_child_survives(self) -> None:
        root = Proc(980, 1, "bash.exe", "bash", cwd=str(self.wt_done), created=10.0)
        blind = Proc(981, 980, "node.exe", "", created=11.0)   # memory not readable: no command line, no cwd
        self.run_worktree([root, blind])
        self.assertEqual(self.killed, [980])

    def test_kill_without_creation_time_is_refused_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows kill path")
        with mock.patch.dict(os.environ, {"SWITCHBOARD_TEST_SANDBOX": "0"}):
            self.assertIn("creation time unknown", tl._windows_kill({"pid": 4, "created": None}))

    def test_kill_switch(self) -> None:
        with mock.patch.dict(os.environ, {"SWITCHBOARD_LEFTOVER_SWEEP": "off"}):
            result = self.run_sweep(vite_tree(DONE))
        self.assertEqual(self.killed, [])
        self.assertIn("skipped", result)

    def test_sandbox_reads_no_processes_and_refuses_to_kill(self) -> None:
        self.assertEqual(tl.snapshot(), [])
        self.assertEqual(tl.kill({"pid": 1, "created": None}), "test sandbox")


class Logging(Base):
    def test_each_stop_is_logged_with_redaction(self) -> None:
        proc = Proc(1000, 1, "node.exe", "node server.js --api-key=sk-live-123 --token abc", cwd=str(self.wt_done), created=5.0)
        messages: list[str] = []
        with mock.patch("os.getpid", return_value=SELF_PID):
            tl.stop_in_worktree(DONE, self.wt_done, self.tasks(), repo_root=self.repo, worktrees_root=self.worktrees,
                                procs=[proc], log_path=self.log_path, killer=self.killer, log=messages.append)
        rows = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["pid"], rows[0]["task_id"], rows[0]["result"]), (1000, DONE, "stopped"))
        self.assertNotIn("sk-live-123", rows[0]["cmdline"])
        self.assertNotIn("abc", rows[0]["cmdline"])
        self.assertTrue(any("stopped node.exe pid 1000" in m for m in messages))

    def test_failed_stop_is_logged_as_failed(self) -> None:
        proc = Proc(1001, 1, "node.exe", "node a.js", cwd=str(self.wt_done), created=5.0)
        with mock.patch("os.getpid", return_value=SELF_PID):
            result = tl.stop_in_worktree(DONE, self.wt_done, self.tasks(), repo_root=self.repo, worktrees_root=self.worktrees,
                                         procs=[proc], log_path=self.log_path, killer=lambda v: "pid reused", log=lambda m: None)
        self.assertEqual((result["stopped"], result["failed"]), (0, 1))
        self.assertEqual(json.loads(self.log_path.read_text(encoding="utf-8"))["result"], "pid reused")

    def test_dry_run_kills_nothing_and_writes_no_log(self) -> None:
        with mock.patch("os.getpid", return_value=SELF_PID):
            result = tl.sweep(self.tasks(), repo_root=self.repo, worktrees_root=self.worktrees, procs=vite_tree(DONE),
                              log_path=self.log_path, killer=self.killer, dry_run=True, log=lambda m: None)
        self.assertEqual((result["stopped"], self.killed, self.log_path.exists()), (6, [], False))


class Parsers(unittest.TestCase):
    def test_norm_name(self) -> None:
        self.assertEqual([tl.norm_name(n) for n in ("Node.EXE", "/usr/bin/python3.12", "C:\\x\\bash.exe", "Python")],
                         ["node", "python3", "bash", "python"])

    def test_msys_and_windows_paths_compare_equal(self) -> None:
        with mock.patch.object(tl.os, "name", "nt"):
            root = tl.norm_path(r"C:\Users\x\worktrees\rt-000000000045")
            self.assertTrue(tl.mentions(tl.norm_text("tail -f /c/Users/x/worktrees/rt-000000000045/a.log"), root))
            self.assertTrue(tl.path_inside(r"C:\USERS\x\worktrees\rt-000000000045\sub", root))
            self.assertFalse(tl.mentions(tl.norm_text("tail -f /c/Users/x/worktrees/rt-aaaaaaaaaaaa0/a.log"), root))

    def test_parse_etime_and_ps(self) -> None:
        self.assertEqual(tl.parse_etime("1-02:03:04"), 93784)
        self.assertEqual(tl.parse_etime("05:06"), 306)
        procs = tl.parse_ps("  123     1   01:00 /usr/local/bin/node /x/vite.js --port 5173\n  124   123 00:30 tail -f /a b\n", now=1000.0)
        self.assertEqual([(p.pid, p.ppid, p.name, p.created) for p in procs], [(123, 1, "node", 940.0), (124, 123, "tail", 970.0)])
        self.assertEqual(procs[0].cmdline, "/usr/local/bin/node /x/vite.js --port 5173")

    def test_parse_comm_keeps_spaces(self) -> None:
        out = "  10 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n  11 /bin/zsh\n"
        self.assertEqual(tl.parse_comm(out), {10: "electron", 11: "zsh"})

    def test_parse_env_tasks(self) -> None:
        out = "123 node vite.js PATH=/bin SWITCHBOARD_WORKER_TASK_ID=rt-000000000045 HOME=/u\n124 bash HOME=/u\n"
        self.assertEqual(tl.parse_env_tasks(out), {123: "rt-000000000045"})

    def test_parse_lsof(self) -> None:
        out = "p123\nfcwd\nn/w/rt-a\nftxt\nn/usr/bin/node\nf5\nn/w/rt-a/log.txt\np124\nfcwd\nn/\n"
        cwd, opened = tl.parse_lsof(out)
        self.assertEqual(cwd, {123: "/w/rt-a", 124: "/"})
        self.assertEqual(opened, {123: ["/w/rt-a/log.txt"]})

    def test_dev_server_shapes(self) -> None:
        yes = ["npm run dev", "npm.cmd run dev -- --port 5173", "node node_modules/vite/bin/vite.js", "python -m http.server 8000",
               "next dev -p 3000", "bun dev", "uvicorn app:app", "pnpm preview"]
        no = ["node chrome-devtools-mcp/build/index.js", "python3 todo_cli.py list", "bash -c true", "node viteless.js"]
        self.assertTrue(all(tl.is_dev_server(Proc(1, 0, "node", c)) for c in yes))
        self.assertFalse(any(tl.is_dev_server(Proc(1, 0, "node", c)) for c in no))


if __name__ == "__main__":
    unittest.main()
