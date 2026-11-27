#!/usr/bin/env python3
"""switchboard task dispatch: one worker per task, one git worktree per worker.

    dispatch.py dispatch --origin launchpad|cos [--workstream SLUG] [--task-id ID]
                [--depends-on ID]... [--continuation-of ID] [--request-id R]
                [--delivery auto|direct|digest] "<ask>"
    dispatch.py steer   --origin O --task-id ID [--no-fallback-dispatch] [--record-only] "<steer>"
                                            same-task steer: delivers to the live thread (cmux agent message),
                                            dispatches --continuation-of ID when the row is terminal (reaped tab),
                                            or queues the steer on the row when the worker has not started yet
                                            (routing, queued, launching, or cmux says the recipient reads no
                                            messages); queued steers deliver in order at the worker's ack or
                                            first heartbeat (--no-fallback-dispatch opts out).
                                            On a terminal row: a live continuation of it
                                            takes the steer; --record-only records a hold/ack/ruling and starts
                                            nothing; a continuation forks the parent's Claude session when it can
    dispatch.py deliver-steers [--task-id ID]
                                            deliver queued steers to workers that have started; one urgent origin
                                            callback per steer still queued after steer_queue_timeout_minutes
    dispatch.py finish  --task-id ID        commit the branch, rebase on main, ff main
    dispatch.py lock    --task-id ID --path /abs/path [--wait SECONDS]
    dispatch.py unlock  --task-id ID [--path /abs/path]
    dispatch.py release                     start queued tasks whose turn has come
    dispatch.py reap    [--task-id ID]      close finished worker tabs, drop worktrees
    dispatch.py record  --task-id ID [--status S --result R --thread-id T]
                                            close-out line in the workstream's CONTEXT.md on main
    dispatch.py sweep                       expire-leases + release + reap + idle-owner close + capacity line (scheduled)
    dispatch.py locate  --thread T-...      {local, thread_id, workspace_ref}
    dispatch.py scorecard [--week|--days N]
    dispatch.py config                      effective dispatch settings
    dispatch.py capacity                    live workers, free RAM, swap used, launch cmux timeouts (reported, never enforced)
    dispatch.py verify-amp --workspace-ref workspace:N --command CMD [--label L] [--seconds S] [--poll P]
                                            the tab shows Amp within S seconds; retypes CMD on a bare
                                            shell (once per LAUNCH_RETYPE_PAUSES_SECONDS), never types into an unknown
                                            screen; exit 0 amp, 2 unknown, 1 not amp
    dispatch.py retype [--task-id ID] [--force]
                                            every routing worker tab that never bound a thread and sits at a bare shell
                                            (amp exited on a connection error) gets its launch command typed again;
                                            the sweep runs this each tick up to MAX_LAUNCH_RETYPES

Isolation comes from git: the worker edits .amp/worktrees/<task-id> on branch
task/<task-id>; main's checkout is never written by a worker. Writes outside the
repo take a FIFO path lock in .amp/locks. Shared state (system/state/*,
.state/) is symlinked into the worktree so ledger/todo/log writers keep one
home. The only wait a worker or the owner ever sees is "waiting on <task> ...".
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import filecmp
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402  (shared with running-task-state.py / triage_ledger.py)

REPO_ROOT = main_checkout(Path(os.environ.get("REPO_ROOT") or SCRIPTS.parents[1]))
STATE_DIR = Path(os.environ.get("SWITCHBOARD_STATE_DIR") or REPO_ROOT / "system" / "state")
WORKTREES = Path(os.environ.get("SWITCHBOARD_WORKTREES_DIR") or REPO_ROOT / ".amp" / "worktrees")
LOCKS = Path(os.environ.get("SWITCHBOARD_LOCKS_DIR") or REPO_ROOT / ".amp" / "locks")
from lib.mux import cmux_bin as _cmux_bin, runnable as _runnable, backend as _mux_backend  # noqa: E402  (cmux on macOS, the wmux shim on Windows/WSL)
from lib import winlink  # noqa: E402  (junction/hard-link fallback for shared-state links without Developer Mode)
import switchboard_agent  # noqa: E402  (the one agent resolver)
import link_titles  # noqa: E402  (titles of linked Docs, Slack threads, issues, PRs)
import owner_tabs  # noqa: E402  (open owner tabs, the live CoS tab, cmux agent message)
import private_identifiers  # noqa: E402  (finish and auto-push refuse a commit adding a listed identifier)
import keep_awake  # noqa: E402  (Windows: hold the laptop awake while work is live)
import private_workstreams  # noqa: E402  (finish and auto-push refuse a private workstream's files; close-outs withhold reports)
import system_owners  # noqa: E402  (owner of the VM, hosted page, job, or repo a failure names)
import usage_limits  # noqa: E402  (a Claude session stopped on the usage or context limit)
import main_rewrites  # noqa: E402  (strip-unpushed: old ids of rewritten unpushed main commits)
import task_leftovers  # noqa: E402  (stop dev servers and monitors a finished task left behind)

CMUX = _cmux_bin()
LAUNCH_PROMPTS = STATE_DIR / "launch-prompts"
CMUX_SKIP_RECORD = Path(os.environ.get("CMUX_SKIP_RECORD") or REPO_ROOT / ".state" / "cmux-skip-record")
# Worker tab identity. A worker tab is titled like
# its owner tab plus a slot number, "<emoji> <Workstream Name> <n>" (for example
# "🛠️ Switchboard Maintenance 2"), and carries the cmux workspace description
# "switchboard worker <task-id>". The description is the primary identifier (a title
# can be renamed; the description is set once at creation and is never shown in
# the tab strip); the title is the secondary check. Tabs opened before this
# change ("<slug> · worker-<12 hex>") stay recognisable so they can be reaped.
WORKER_DESCRIPTION_PREFIX = "switchboard worker "
LEGACY_WORKER_TITLE = re.compile(r"· worker-[0-9a-f]{12}$")
WORKER_SLOT_TITLE = re.compile(r"^(?P<owner>.+?) (?P<n>[1-9]\d*)$")
# Amp Neo always renders a bordered prompt box or the welcome banner; a bare
# ❯ / % / $ is a shell prompt and must never count (cmux-helpers.sh wait_for_amp_ready).
AMP_SCREEN_MARKER = re.compile(r"(╭──|╰──|Welcome to Amp|Ctrl\+O for help)")
# Claude Code (2.1.305, probed in a cmux tab) renders its input box
# as `❯ ` between two ─── rules with a footer line `⏵⏵ auto mode on (shift+tab
# to cycle)` (`bypass permissions on` under --dangerously-skip-permissions) or
# `? for shortcuts`. Only that footer counts as ready: its startup dialogs
# (folder trust, external CLAUDE.md imports, Bypass Permissions acknowledgement)
# end in `Enter to confirm · Esc to cancel` and must read as unknown, never
# typed into. The bare `❯ ` would also match a starship shell prompt.
CLAUDE_SCREEN_MARKER = re.compile(r"(shift\+tab to cycle|\? for shortcuts)")
# Which agent a worker tab starts comes from switchboard_agent.py (SWITCHBOARD_AGENT env,
# then router.yaml `agent:`, then claude). cmux-helpers.sh
# switchboard_agent calls the same resolver. Amp modes map to a Claude model and
# effort; keep this table in step with CLAUDE_MODE_FLAGS in cmux-helpers.sh.
CLAUDE_MODE_FLAGS = {
    "ultra": ("opus", "high"),
    "high": ("opus", "high"),
    "medium": ("sonnet", "medium"),
    "low": ("sonnet", "low"),
}
CLAUDE_SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)


def _unlink_retry(path: Path) -> bool:
    """Path.unlink(missing_ok), retried for up to 5 s on Windows while a sync client or a peer holds the file.

    .amp/locks may sit inside a synced folder, so a sync can hold a queue or lease file open for a moment.
    Returns False instead of raising once the retries run out: a leftover empty file is harmless, a
    traceback from `dispatch.py lock` is not (it left the worker with no JSON answer, WinError 32).
    """
    for attempt in range(50):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if os.name != "nt":
                raise
            if attempt == 49:
                return False
            time.sleep(0.1)
    return False


def agent_kind() -> str:
    """`claude` or `amp`: SWITCHBOARD_AGENT env, then router.yaml `agent:`, then claude (switchboard_agent.py)."""
    config_path = Path(os.environ.get("ROUTER_CONFIG") or REPO_ROOT / "system" / "config" / "router.yaml")
    return switchboard_agent.agent(config_path=config_path)


def claude_mode_flags(mode: str) -> tuple[str, str]:
    """(model, effort) for an Amp agent mode; unknown modes take the ultra row."""
    return CLAUDE_MODE_FLAGS.get(str(mode or "").strip().lower(), CLAUDE_MODE_FLAGS["ultra"])


# zsh `host dir % `, bash `bash-5.2$ `, starship `❯ `, root `# `, oh-my-zsh `➜  dir git:(b) `,
# and a zsh PROMPT `%~ $(__git_ps1 " %s") ` which renders as the bare
# path plus branch with no trailing glyph at all: `~/switchboard/.amp/worktrees/
# rt-x/workstreams/slug task/rt-x `. `%` needs a space before it so a progress line
# ("Loading 100%") is not a prompt; the path form allows at most one word after the
# path so a command echo (`~/dir task/rt-x cat prompt.md | amp`) never counts.
SHELL_PROMPT_LINE = re.compile(r"(?:(?:^|\s)%|[$❯#])\s*$|^➜\s|^(?:~|/)\S*(?:\s+\S+)?\s*$")
# What `amp` prints and exits with when ampcode.com is unreachable at startup (an
# HTTPS outage, ECONNRESETs): the tab drops back to
# a shell prompt and the launch command must be typed again once the network is up.
AMP_START_ERROR = re.compile(r"Couldn't connect to the Amp server|ECONNRESET|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|"
                             r"EAI_AGAIN|fetch failed|socket connection was closed|UND_ERR")
AMP_VERIFY_SECONDS = 30                   # the tab must show Amp inside this budget, before and after each retype
AMP_VERIFY_POLL_SECONDS = 2
# Launcher retypes: the first right after the budget, then after this pause (an
# ECONNRESET at startup usually clears in seconds). Beyond these, the sweep retypes
# once per tick (retype_stalled_launches) up to MAX_LAUNCH_RETYPES in all, so an
# outage of hours is ridden out without a relaunch or a blocked row.
LAUNCH_RETYPE_PAUSES_SECONDS = tuple(float(s) for s in (os.environ.get("SWITCHBOARD_LAUNCH_RETYPE_PAUSES") or "0,20").split(","))
MAX_LAUNCH_RETYPES = int(os.environ.get("SWITCHBOARD_MAX_LAUNCH_RETYPES") or 8)
SWEEP_RETYPE_MIN_AGE = timedelta(seconds=int(os.environ.get("SWITCHBOARD_SWEEP_RETYPE_MIN_AGE_SECONDS") or 120))

_spec = importlib.util.spec_from_file_location("running_task_state", SCRIPTS / "running-task-state.py")
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)  # type: ignore[union-attr]

IDLE_RESUME_MINUTES = int(os.environ.get("SWITCHBOARD_IDLE_RESUME_MINUTES") or 10)  # idle this long with branch unmerged -> one resume message; idle again this long -> one line to the origin
FF_RETRIES = 3  # finish: re-rebase and retry when main moved between the rebase and the fast-forward

DEFAULTS = {
    # 0 (or absent) = no cap on concurrent workers (the owner's call:
    # RAM, timeouts and quota are the only limits that matter). A positive number queues the (n+1)th task behind the oldest live
    # worker, as before; the key stays so a cap can be put back in router.yaml.
    "max_workers": 0,
    # Windows only: worker cap, the commit % at which new
    # launches wait, and the commit % and zombie count that raise a capacity
    # alert. 0 turns each one off.
    "max_workers_windows": 3,
    "commit_hold_pct": 92,
    "memory_hold_floor_workers": 1,
    "spawn_hold_ms": 400,
    "commit_alert_pct": 85,
    "zombie_alert_count": 50000,
    # Capacity is reported, not enforced (capacity_tick, each sweep): one digest
    # line plus one urgent CoS line, at most hourly, while swap used exceeds
    # this many GB or a cmux RPC timed out during a launch since the last tick.
    # 0 disables the swap trigger. A busy machine can idle with a lot of swap
    # from a browser alone, so a low value would fire every working day; 16 sits
    # above that baseline and below what a worker burst reaches.
    "swap_alert_gb": 16,
    "default_workstream": "switchboard-maintenance",
    # Owner of system routines (start of day, closing day, chief of staff), since
    # switchboard-maintenance is kept for switchboard itself. Missing folder: default_workstream.
    "system_routine_workstream": "daily-routines",
    "min_match_score": 20,
    "reap_grace_seconds": 120,
    "reap_requires_delivered_report": True,
    # Rebase-conflict grace window. A real rebase conflict at finish keeps the row active with
    # landing.state == "conflict" for this long, tells the worker once in its tab,
    # and holds the failed report; a landing inside the window delivers the worker's
    # own done report exactly once, since conflicts often resolve within minutes. The sweep delivers the failed report when the window lapses.
    # 0 disables the window (failed report at once, as before).
    "conflict_grace_seconds": 600,
    "idle_owner_close_hours": 4,   # owner workstream tab idle this long with no live worker -> wrap-up prompt, then close; 0 disables
    # No new worker when something is already open: a new ask whose owner has an open Claude tab is delivered there by cmux
    # agent message (deliver_to_owner_tab); a worker opens only when no owner tab can
    # take it. false restores a worker per ask.
    "owner_tab_dispatch": True,
    # A routing/queued row this many minutes old with no live thread and no amp
    # session in its tab is marked blocked ("stuck: ...") and the origin hears one
    # line with the re-dispatch command (flag_stuck_routes). Never re-dispatched by the sweep. 0 disables.
    "stuck_routing_minutes": 10,
    # An active row whose worker has shown no progress for this many hours on
    # every signal at once (task-liveness heartbeat, cmux lifecycle stamp, and
    # the Amp thread's last message) is wedged: the sweep stamps `wedged`, writes
    # a `recovery_action`, and tells the origin once (flag_wedged_workers). Never re-dispatched by the sweep. 0 disables.
    "wedged_hours": 2,
    # One-call dispatch ack. `dispatch` writes the ledger row (status routing)
    # and returns at once; the worktree, the cmux tab and the Amp verification
    # (16 to 30 s measured over the last 12 launches) run in a detached
    # `dispatch.py launch --task-id` process that the row records as launcher_pid.
    # A launch failure still reaches the origin through notify() (report-to-cos.sh);
    # a crashed launcher is caught by requeue_orphaned_routes / flag_stuck_routes.
    # false, --sync, or SWITCHBOARD_DISPATCH_SYNC=1 keeps the inline launch.
    "async_launch": True,
    # Queued steers. A steer to a row that has not started (routing,
    # queued, or launched with no ack yet) waits on the row in `pending_steers` and is
    # delivered in order when the worker acks or heartbeats. A queued steer still
    # undelivered this many minutes after the worker began launching sends one urgent
    # callback to the origin; nothing is spawned. 0 disables the callback.
    "steer_queue_timeout_minutes": 10,
    # Steers on a finished row.
    # A continuation forks the parent's Claude session (`claude --resume <parent> --fork-session`)
    # so it starts with the parent's full context and only does the steer. Forking re-caches
    # the parent's whole context, so it pays only while that context is small: measured on
    # one real steer, a cold worker took 7 calls, 84K cache
    # writes, 664K cache reads and 77 s; a fork of the 121K-token parent took 2 calls, 120K
    # writes, 118K reads and 12 s. A parent past steer_resume_max_context_tokens gets a cold
    # worker. steer_resume_session false turns the fork off.
    "steer_resume_session": True,
    "steer_resume_max_context_tokens": 200000,
    # Race guard: a steer on a row whose continuation is still starting or running goes to
    # that continuation; a steer on a row whose newest continuation finished within this many
    # minutes continues that one, so back-to-back steers make one chain, never siblings.
    "steer_chain_window_minutes": 10,
    # Queue order. A free slot goes to the queued row that
    # ranks first, and a fresh dispatch launches at once only when no eligible queued row
    # ranks ahead of it. Owner asks rank before switchboard-maintenance rows, each class oldest
    # first; a maintenance row that has waited this many minutes ranks with the owner asks
    # by its age, so a run of owner asks never starves a fix. queue_owner_first false is
    # strict FIFO; aging 0 means a maintenance row never moves up.
    "queue_owner_first": True,
    "queue_maintenance_aging_minutes": 30,
    # Dated launch holds (for example: hold new maintenance launches until a set time on a
    # given day). Each entry in `launch_holds` names workstreams and/or
    # task ids and an absolute, zoned `until`; a matching first-attempt row queues with
    # "held until <time>" and release_queued starts it on the first pass after `until`. The
    # hold lifts by the clock alone, so nothing has to remove it. An `until` further ahead
    # than launch_hold_max_hours, naive, or unparseable is ignored (fail open), so a typo can
    # never hold work for days. One entry may run longer (a check once
    # held a week): its own `max_hours: <n>` replaces the 24 h cap for that entry
    # only, or `allow_long: true` lifts it to the ceiling below. Neither goes past
    # LAUNCH_HOLD_CEILING_HOURS, so a year typo still fails open. See launch_hold().
    "launch_holds": [],
    "launch_hold_max_hours": 24,
}

# The longest any launch hold may reach, whatever its entry says (31 days).
LAUNCH_HOLD_CEILING_HOURS = 24 * 31


def log(msg: str) -> None:
    print(f"dispatch: {msg}", file=sys.stderr)


def now() -> datetime:
    return datetime.now(timezone.utc)


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        import yaml  # type: ignore
        data = yaml.safe_load((REPO_ROOT / "system" / "config" / "router.yaml").read_text()) or {}
        cfg.update({k: v for k, v in (data.get("dispatch") or {}).items() if v is not None})
        # The organisation profile (apply-profile.py) may add dispatch settings in
        # router.profile.yaml.
        profile = REPO_ROOT / "system" / "config" / "router.profile.yaml"
        if profile.exists():
            extra = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
            cfg.update({k: v for k, v in (extra.get("dispatch") or {}).items() if v is not None})
    except Exception as error:  # config is advisory; defaults always work
        log(f"router.yaml dispatch section unreadable ({error}); using defaults")
    return cfg


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if "input" not in kw:
        # A headless Windows task (conhost --headless) has no stdin handle, and a child that
        # inherits the invalid one fails with WinError 6. Nothing here reads the terminal.
        kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(_runnable(cmd), **kw)  # Windows: the extensionless mux/cmux wrapper runs via Python


def retire_review_ask(task_id: str, by: str, reason: str, action: str = "complete") -> dict[str, Any] | None:
    """Close every CoS Review ask a finished task row raised (Needs you → Past), because a steer,
    continuation, or done report shows the owner already ruled on it. Best effort: the review feed is
    a mirror, so a failure here is logged and never fails the dispatch. Returns the helper's JSON."""
    if not task_id or os.environ.get("COS_REVIEW_RETIRE_DISABLED"):
        return None
    result = run([sys.executable, str(SCRIPTS / "cos_review_actions.py"), "--no-feed", action,
                  "--task-id", task_id, "--by", by, "--reason", reason], timeout=30)
    if result.returncode:
        log(f"cos-review retire of {task_id} failed: {(result.stderr or result.stdout).strip()[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def git(*args: str, cwd: Path | None = None, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    result = run(["git", "-C", str(cwd or REPO_ROOT), *args], **({"timeout": timeout} if timeout else {}))
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def _git_nocheck(*args: str) -> subprocess.CompletedProcess:
    return git(*args, check=False)


def on_main(ref: str) -> bool:
    """`ref` is on main, directly or as an old id of a commit strip-unpushed rewrote."""
    return main_rewrites.on_main(_git_nocheck, ref, main_rewrites.load(STATE_DIR))


# --- cmux ----------------------------------------------------------------------------

def _int_tuple(env: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.environ.get(env, "")
    try:
        values = tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        values = ()
    return values or default


# `workspace.list` budgets, one per try, and the pause before each retry. Under
# load cmux can time out repeatedly at 10 s, and two tries with no pause give it no
# room to recover. Three tries over ~75 s plus pauses is the bound before a
# launcher gives up and records why (CmuxUnreachable).
CMUX_LIST_TIMEOUTS = _int_tuple("SWITCHBOARD_CMUX_LIST_TIMEOUTS", (10, 25, 40))
CMUX_LIST_BACKOFF = _int_tuple("SWITCHBOARD_CMUX_LIST_BACKOFF_SECONDS", (0, 5, 15))


class CmuxUnreachable(RuntimeError):
    """`workspace.list` timed out on every try; carries the attempt count and schedule."""


class LaunchHold(RuntimeError):
    """No tab could be opened for a reason that passes on its own (the multiplexer is
    down, or the new tab's terminal went away). The row goes back to `queued`, never to
    `failed`, because a multiplexer restart ("could not connect to pipe") is not a task
    failure. `unreachable` also stops this release pass."""

    def __init__(self, message: str, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


MUX_DOWN = re.compile(r"could not connect to pipe|wmux is not running|cmux_unreachable|connection refused", re.IGNORECASE)
NO_TERMINAL = re.compile(r"has no PTY|no surface in workspace", re.IGNORECASE)


def cmux_workspaces(task_id: str = "", strict: bool = False) -> list[dict[str, Any]]:
    """Every open tab, or [] when cmux cannot answer. A list that times out is
    retried with longer budgets and a pause between tries (CMUX_LIST_TIMEOUTS,
    CMUX_LIST_BACKOFF): cmux can answer `workspace.list` in 60 ms most of the
    time and not at all for 10 s under load, and one unretried timeout would fail
    a launch before any tab exists. `task_id` attributes each timeout in
    cmux-timeouts.jsonl to the launch it stalled. With `strict`, a list that
    never answers raises CmuxUnreachable so a launcher records the reason on the
    row instead of carrying on with an empty tab list."""
    timed_out = 0
    for i, timeout in enumerate(CMUX_LIST_TIMEOUTS):
        pause = CMUX_LIST_BACKOFF[min(i, len(CMUX_LIST_BACKOFF) - 1)] if timed_out else 0
        if pause:
            time.sleep(pause)
        try:
            result = run([CMUX, "rpc", "workspace.list", "{}"], timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out += 1
            log(f"cmux workspace.list did not answer within {timeout}s (try {i + 1}/{len(CMUX_LIST_TIMEOUTS)})")
            note_cmux_timeout(task_id, "workspace.list", timeout)   # shows up in the sweep's capacity line
            continue
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:200]
            log(f"cmux workspace.list exited {result.returncode}: {detail}")
            if strict and MUX_DOWN.search(detail):
                raise CmuxUnreachable(f"cmux_unreachable: {detail}")
            return []
        try:
            return list(json.loads(result.stdout).get("workspaces") or [])
        except ValueError:
            return []
    if strict:
        raise CmuxUnreachable(f"cmux_unreachable: workspace.list timed out {timed_out}x "
                              f"(budgets {', '.join(f'{t}s' for t in CMUX_LIST_TIMEOUTS)})")
    return []


def workspace_by_ref(ref: str) -> dict[str, Any] | None:
    return workspace_by_ref_in(ref, cmux_workspaces())


def workspace_by_ref_in(ref: str, workspaces: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((w for w in workspaces if w.get("ref") == ref), None)


def worker_tab(task_id: str, workspaces: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The open tab whose description is this task's worker identifier, if any.
    The description is the one field a launch writes before cmux can time out,
    so it is what a launcher re-checks when `new-workspace` reports an unknown
    result."""
    wanted = worker_description(task_id)
    return next((w for w in (cmux_workspaces(task_id) if workspaces is None else workspaces)
                 if str(w.get("description") or "").strip() == wanted), None)


def note_tab_close(ref: str, workspace_id: str, why: str) -> None:
    """One line per close switchboard asks for, so a vanished tab can be traced to its closer."""
    try:
        with CMUX_SKIP_RECORD.with_name("tab-closes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": ledger.now_iso(), "ref": ref, "workspace_id": workspace_id, "why": why,
                                     "pid": os.getpid()}) + "\n")
    except OSError:
        pass


def cmux_close_result(ref: str, workspace_id: str, why: str = "") -> tuple[bool, str]:
    """Close one tab. Returns (closed, cmux's error line) so callers can log why
    a close failed instead of a bare "cmux close failed".

    cmux 0.65 made `close-workspace` refuse a workspace
    with a running process ("confirmation_required: Workspace has a running
    process; retry with --force") unless `--force` is passed. Every switchboard tab
    runs an agent CLI, so without the flag every close fails. The guards that decide a tab may close (reap, the idle
    owner wrap-up, command-center-action.sh) have already run by the time this
    is called, so the running process is exactly what the policy wants to end:
    pass --force. An older cmux that rejects the flag ("unknown option --force")
    gets one retry in its legacy form."""
    note_tab_close(ref, workspace_id, why)
    if workspace_id:
        try:
            CMUX_SKIP_RECORD.parent.mkdir(parents=True, exist_ok=True)
            with CMUX_SKIP_RECORD.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(workspace_id + "\n")
        except OSError:
            pass
    try:
        run([CMUX, "workspace-action", "--action", "unpin", "--workspace", ref], timeout=10)
        result = run([CMUX, "close-workspace", "--workspace", ref, "--force"], timeout=15)
        if result.returncode and "unknown option" in (result.stderr or "") and "--force" in (result.stderr or ""):
            result = run([CMUX, "close-workspace", "--workspace", ref], timeout=15)
    except subprocess.TimeoutExpired:
        return False, "cmux close-workspace timed out after 15s"
    if result.returncode == 0:
        return True, ""
    error = " ".join(((result.stderr or "") + " " + (result.stdout or "")).split()) or f"exit {result.returncode}"
    return False, error


def cmux_close(ref: str, workspace_id: str, why: str = "") -> bool:
    return cmux_close_result(ref, workspace_id, why)[0]


# --- worker tab identity -----------------------------------------------------------------

def _config_scalar(path: Path, key: str) -> str:
    """`key:` from a workstream config.yaml read the way cmux-helpers.sh does
    (first matching line, surrounding quotes dropped); "" when absent."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return ""


def titleize(slug: str) -> str:
    special = {"ai": "AI", "ios": "iOS", "pr": "PR"}
    return " ".join(special.get(p, p.capitalize()) for p in slug.replace("_", "-").split("-") if p)


def owner_title(slug: str) -> str:
    """The owner tab's title for a workstream, "<emoji> <Name>", exactly as
    cmux-helpers.sh format_workspace_name_for_source builds it (config.yaml
    emoji: and name:, 📁 + titleized slug when either is missing)."""
    cfg = REPO_ROOT / "workstreams" / slug / "config.yaml"
    return f"{_config_scalar(cfg, 'emoji') or '📁'} {_config_scalar(cfg, 'name') or titleize(slug)}"


def task_lead(task_id: str, slug: str, pending_name: str = "") -> str:
    """``[<emoji> <Workstream Name> · rt-…]``: the lead of every dispatch
    message and worker report: never a bare number, always the name; the task id follows the name, never stands in
    for it (external-replies.md Rule 13). `pending_name` is the derived name
    of a workstream the launcher has not scaffolded yet (no config.yaml)."""
    if not slug:
        return f"[{task_id}]"
    if pending_name and not workstream_exists(slug):
        return f"[📁 {pending_name} · {task_id}]"
    return f"[{owner_title(slug)} · {task_id}]"


def task_name(row: dict | None, task_id: str = "") -> str:
    """``<emoji> <Workstream Name> (rt-…)`` for a ledger row, used wherever a
    message refers to another task (``waiting on …``)."""
    row = row or {}
    task_id = task_id or str(row.get("id") or "")
    slug = str(row.get("workstream") or "")
    return f"{owner_title(slug)} ({task_id})" if slug else task_id


def owner_titles() -> dict[str, str]:
    """{owner title: slug} for every workstream directory."""
    root = REPO_ROOT / "workstreams"
    out: dict[str, str] = {}
    if root.is_dir():
        for cfg in root.glob("*/config.yaml"):
            out[owner_title(cfg.parent.name)] = cfg.parent.name
    return out


def worker_title(slug: str, n: int) -> str:
    return f"{owner_title(slug)} {n}"


def worker_description(task_id: str) -> str:
    return f"{WORKER_DESCRIPTION_PREFIX}{task_id}"


def worker_slot(title: str, owner: str) -> int:
    """n when `title` is "<owner> <n>", else 0."""
    match = WORKER_SLOT_TITLE.match(title)
    return int(match.group("n")) if match and match.group("owner") == owner else 0


def next_worker_slot(slug: str, payload: dict[str, Any], workspaces: list[dict[str, Any]] | None = None) -> int:
    """Lowest free slot number for this workstream's worker tabs: every open cmux
    tab titled "<owner> <n>" and every non-terminal row that recorded a tab title
    (its tab may still be opening) hold their n."""
    owner = owner_title(slug)
    taken = {worker_slot(tab_title(w), owner) for w in (cmux_workspaces() if workspaces is None else workspaces)}
    taken |= {worker_slot(str(t.get("tab_title") or ""), owner) for t in payload.get("tasks", [])
              if t.get("workstream") == slug and t.get("status") not in ledger.TERMINAL_STATUSES}
    n = 1
    while n in taken:
        n += 1
    return n


def tab_title(ws: dict[str, Any]) -> str:
    return str(ws.get("custom_title") or ws.get("title") or "")


def worker_tab_reason(ws: dict[str, Any], task: dict[str, Any] | None = None) -> str:
    """"" when `ws` is a worker tab (of `task` when given), else why not. The
    description "switchboard worker <task-id>" decides first; then the title recorded
    on the row; then the "<owner title> <n>" shape for a known workstream; then
    the legacy "<slug> · worker-<12 hex>" form. A renamed owner tab, CoS,
    Launchpad and the fixed surfaces match none of these."""
    description = str(ws.get("description") or "")
    title = tab_title(ws)
    if description.startswith(WORKER_DESCRIPTION_PREFIX):
        if task is not None and description != worker_description(task["id"]):
            return f"tab belongs to another worker ({description[len(WORKER_DESCRIPTION_PREFIX):]})"
        return ""
    if task is not None:
        return _undescribed_tab_reason(ws, task, title)
    if LEGACY_WORKER_TITLE.search(title):
        return ""
    match = WORKER_SLOT_TITLE.match(title)
    if match and match.group("owner") in owner_titles():
        return ""
    return f"title is not a worker tab ({title[:40]})"


WORKTREE_IN_CWD = re.compile(r"[\\/]worktrees[\\/]([^\\/]+)(?:[\\/]|$)", re.IGNORECASE)


def tab_worktree_task(ws: dict[str, Any]) -> str:
    """The task id whose worktree holds this tab's directory, lowercase, or ""."""
    found = WORKTREE_IN_CWD.search(str(ws.get("current_directory") or ""))
    return found.group(1).lower() if found else ""


def directory_proves_tab(ws: dict[str, Any], task: dict[str, Any], agents: dict[str, dict[str, Any]] | None = None) -> bool:
    """A tab with no description is this row's by its directory only when it also looks
    like a worker tab of the row: wmux restores a worker tab with its title and directory.
    An owner's shell opened in that worktree, a pinned tab, or a tab running another
    agent session is not the worker, whatever its directory."""
    if tab_worktree_task(ws) != str(task["id"]).lower() or ws.get("pinned"):
        return False
    title = tab_title(ws)
    match = WORKER_SLOT_TITLE.match(title)
    if title != str(task.get("tab_title") or "") and not (match and match.group("owner") in owner_titles()):
        return False
    own = str(task.get("thread_id") or task.get("session_id") or "")
    tab_id = str(ws.get("id") or "").upper()
    return not any(thread != own and str(a.get("workspace_id") or "").upper() == tab_id for thread, a in (agents or {}).items())


def _undescribed_tab_reason(ws: dict[str, Any], task: dict[str, Any], title: str) -> str:
    """Whether a tab with no worker description can still be proven to be this row's.

    Slot titles are reused: "📊 Switchboard Maintenance 2" is a new tab every time a slot frees,
    so a title alone proves nothing: a row finished hours earlier can carry the same slot
    title as a live tab whose description is missing from the wmux sidecar. Proof is
    the row's own ref plus its title, a stored ref that equals a legacy worker title (its
    hex is unique, while an owner tab's title a row recorded proves nothing), the title
    that names this row's unique worker id, or a directory inside this row's worktree."""
    ref = str(task.get("workspace_ref") or "")
    if task.get("worker_id") and task.get("workstream") and title == f"{task['workstream']} · {task['worker_id']}":
        return ""
    if directory_proves_tab(ws, task):
        return ""   # a tab wmux restored after a restart: new id, no description, same directory
    if ref and ref == title and LEGACY_WORKER_TITLE.search(title):
        return ""
    if ref and ws.get("ref") == ref:
        recorded = str(task.get("tab_title") or "")
        if recorded == title or (not recorded and LEGACY_WORKER_TITLE.search(title)):
            return ""
        match = WORKER_SLOT_TITLE.match(title)
        if not recorded and match and match.group("owner") in owner_titles():
            return ""
    return f"title is not a worker tab of {task['id']} ({title[:40]})"


def is_worker_tab(ws: dict[str, Any] | None, task: dict[str, Any] | None = None) -> bool:
    return ws is not None and worker_tab_reason(ws, task) == ""


def worker_tab_for(task: dict[str, Any], workspaces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The open cmux tab that belongs to `task`: the one whose description is
    "switchboard worker <task-id>" or whose id equals the ref, else an undescribed tab
    whose directory proves it, else the one at its workspace_ref unless that tab
    is described for another worker, else the one whose title is the
    title the row recorded, the legacy "<slug> · <worker-id>" title, or the
    ref itself. report-to-cos.sh lets a worker pass `--workspace-ref` and
    workers do hand in the tab's UUID instead of "workspace:N"
    or the tab's title (legacy rows stored "<slug> · worker-<hex>", a few a human title),
    which used to settle the row with its tab left open forever. A title match
    must still look like a worker tab, so a row that recorded an owner tab's
    title can never make the reaper close that owner tab."""
    ref = str(task.get("workspace_ref") or "")
    # Identity first: refs are renumbered when the multiplexer restarts (wmux on
    # Windows), and a report run from another pane can record that pane's ref.
    wanted = worker_description(task["id"])
    by_identity = next((w for w in workspaces if w.get("description") == wanted or (ref and w.get("id") == ref)), None)
    if by_identity is not None:
        return by_identity
    undescribed = (w for w in workspaces if not str(w.get("description") or "").startswith(WORKER_DESCRIPTION_PREFIX))
    by_directory = next((w for w in undescribed if directory_proves_tab(w, task)), None)
    if by_directory is not None:
        return by_directory
    by_ref = next((w for w in workspaces if w.get("ref") == ref), None)
    if by_ref is not None and not str(by_ref.get("description") or "").startswith(WORKER_DESCRIPTION_PREFIX):
        return by_ref   # protected() decides; a tab described for another worker is not this row's
    titles = {str(task.get("tab_title") or ""), ref}
    if task.get("workstream") and task.get("worker_id"):
        titles.add(f"{task['workstream']} · {task['worker_id']}")
    titles.discard("")
    return next((w for w in workspaces if tab_title(w) in titles and is_worker_tab(w, task)), None)


def resolve_live_tab(task: dict[str, Any], workspaces: list[dict[str, Any]],
                     agents: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The open tab a non-terminal row's worker runs in, proven by identity and never
    by a slot title: its description, the tab holding its live agent session, the tab
    whose directory is its worktree (a tab with no description only), then the row's
    own ref and title. None means no tab holds this worker."""
    by_description = worker_tab(task["id"], workspaces)
    if by_description is not None:
        return by_description
    thread = str(task.get("thread_id") or task.get("session_id") or "")
    tab_id = str(((agents or {}).get(thread) or {}).get("workspace_id") or "").upper()
    if tab_id:
        by_agent = next((w for w in workspaces if str(w.get("id") or "").upper() == tab_id), None)
        if by_agent is not None:
            return by_agent
    undescribed = [w for w in workspaces if not str(w.get("description") or "").startswith(WORKER_DESCRIPTION_PREFIX)]
    for w in undescribed:
        if directory_proves_tab(w, task, agents):
            return w
    return next((w for w in undescribed if w.get("ref") == task.get("workspace_ref") and not worker_tab_reason(w, task)), None)


def heal_row_ref(task: dict[str, Any], workspaces: list[dict[str, Any]],
                 agents: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Point a live row at the tab its worker is really in, and give that tab back its
    description when it lost it. Refs go stale when the multiplexer restarts or a worker
    reports another pane's ref, and `--steer` then answered "workspace not found" or,
    worse, reached another worker. An unreadable tab list changes nothing."""
    if not workspaces:
        return task
    tab = resolve_live_tab(task, workspaces, agents)
    if tab is None:
        return task
    actual = str(tab.get("ref") or "")
    if not str(tab.get("description") or "").strip() and actual:
        try:
            run([CMUX, "workspace-action", "--action", "set-description", "--workspace", actual,
                 "--description", worker_description(task["id"])], timeout=15)
        except subprocess.SubprocessError as error:
            log(f"{task['id']}: could not restore the description on {actual}: {error}")
    old = str(task.get("workspace_ref") or "")
    if not actual or actual == old:
        return task

    def rebind(payload, tid=task["id"], attempt_id=task.get("attempt_id"), actual=actual, old=old):
        row = ledger.find_task(payload, tid)
        if row is None or row.get("attempt_id") != attempt_id or row.get("status") in ledger.TERMINAL_STATUSES:
            return None
        row.update(workspace_ref=actual, workspace_ref_healed_from=old, updated_at=ledger.now_iso())
        return dict(row)

    healed = ledger.locked_mutation(rebind)
    if healed is None:
        return task
    log(f"{task['id']}: workspace_ref healed from {old or '(none)'} to {actual}")
    return healed


def heal_live_refs(workspaces: list[dict[str, Any]], agents: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Sweep step: every live worker row points at the tab its worker is in."""
    healed = []
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") not in {"routing", "active"} or not task.get("worker_id") or task.get("executor"):
            continue
        row = heal_row_ref(task, workspaces, agents)
        if row.get("workspace_ref") != task.get("workspace_ref"):
            healed.append({"task_id": task["id"], "from": task.get("workspace_ref"), "to": row.get("workspace_ref")})
    return healed


# --- workstream choice ------------------------------------------------------------------

EXPLICIT_WORKSTREAM = re.compile(r"\bWorkstream:\s*`?([a-z0-9][a-z0-9-]*)", re.IGNORECASE)
# router-context-pack.py evidence that names the owner (a match term, the slug or
# display name written in the ask, a priority-channel route, the directive). Only
# these make an existing workstream the owner. "shared topic terms: …" is lexical
# overlap, and the status penalties are not evidence of ownership at all.
STRONG_EVIDENCE_PREFIXES = ("explicit directive", "exact priority-channel owner", "exact slug phrase",
                            "exact workstream name", "match term ", "source title ",
                            # Without these, asks that named their owner one of these
                            # ways scaffolded stray workstreams.
                            "subject names this workstream", "workstream path named", "note named",
                            "abbreviated workstream name", "all name words in the ask", "switchboard system named")
# Evidence that names the ask's subject (the owner's quoted words or the first sentence).
SUBJECT_EVIDENCE = "subject names this workstream"
# Pointers precise enough to name an owner from anywhere in the ranking, not just
# rank 1: a path or note inside the workstream, or its slug written out. A match
# term deep in a long prompt is not (an ask can name a tool as its output surface
# without being about the workstream that tool's name matches).
POINTER_EVIDENCE = ("workstream path named", "note named", "exact slug phrase")
# Most of a workstream's name in the ask's own words: the closest owner when nothing
# names one.
CLOSEST_EVIDENCE = "subject has most name words"


def system_owner(text: str, exclude_task: str = "") -> dict[str, Any] | None:
    """The workstream that owns the system (VM, hosted page, job, repo,
    channel) `text` names, from declared `systems:`, live ledger rows, and
    CONTEXT mentions (system_owners.resolve). Never fails a dispatch."""
    try:
        return system_owners.resolve(text, REPO_ROOT, ledger.load_payload(), exclude_task)
    except Exception as error:  # noqa: BLE001
        log(f"system owner lookup failed: {error}")
        return None
RESERVED_WORKSTREAM_SLUGS = {"shared", "archive", "inbox", "readme"}

# System routines (start of day, closing day, chief of staff) belong to the
# system-routine owner (dispatch.system_routine_workstream, daily-routines by
# default; default_workstream when that folder is missing) no matter what
# their context mentions: a start-of-day ask whose context names an issue key
# would otherwise strong-match that issue's workstream. Matched against the opening clause only
# (first sentence, up to ~120 chars), so an ask that merely mentions "the day"
# mid-prompt still goes through ranking.
SYSTEM_ROUTINE_RE = re.compile(
    r"^\W*(?:please\s+|ok\s+|hey\s+)?(?:"
    r"start[\s-]of[\s-]day|start\s+(?:my|the)\s+day|good\s+morning|sod|"
    r"clos(?:e|ing)[\s-](?:of[\s-])?(?:the\s+)?day|end[\s-]of[\s-]day|eod|wrap\s+(?:up\s+)?the\s+day|"
    r"chief[\s-]of[\s-]staff|cos\s+(?:morning|triage|session)|morning\s+triage"
    r")\b",
    re.IGNORECASE,
)
SYSTEM_ROUTINE_SCRIPT_RE = re.compile(r"\b(?:start-day|closing-day)\.sh\b", re.IGNORECASE)
ROUTER_MISS_LOG = "router-misses.jsonl"


def system_routine(prompt: str) -> bool:
    opening = prompt.strip()[:120].split("\n", 1)[0]
    return bool(SYSTEM_ROUTINE_RE.search(opening) or SYSTEM_ROUTINE_SCRIPT_RE.search(opening))


def log_router_miss(prompt: str, chosen: str, kind: str) -> None:
    """One JSON line per routing override, so misses stay reviewable after the fact."""
    path = REPO_ROOT / "system" / "logs" / ROUTER_MISS_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps({"at": now().isoformat(timespec="seconds"), "kind": kind, "chosen": chosen,
                                     "prompt": prompt.strip()[:200]}, ensure_ascii=False) + "\n")
    except OSError as error:
        log(f"router miss log unavailable: {error}")


def workstream_exists(slug: str) -> bool:
    return bool(slug) and (REPO_ROOT / "workstreams" / slug / "CONTEXT.md").is_file()


def strong_owner(candidate: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """An existing workstream owns a prompt only when the ranking found a named
    signal for it at or above `min_match_score`. Lexical overlap alone is a weak
    match, which offers a new workstream instead of
    forcing the closest existing owner (novel or merely tangential work gets its
    own workstream, a one-shot included)."""
    if float(candidate.get("score") or 0) < float(cfg["min_match_score"]):
        return False
    return any(str(item).startswith(STRONG_EVIDENCE_PREFIXES) for item in candidate.get("evidence") or [])


def has_evidence(candidate: dict[str, Any], prefixes: str | tuple[str, ...]) -> bool:
    return any(str(item).startswith(prefixes) for item in candidate.get("evidence") or [])


def pick_owner(pack: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The existing owner the ranking names, by tier, or (None, "").

    1. path: the one workstream the ask names by path (workstreams/<slug>/...),
       when it names no other by path or slug (router-context-pack.path_owner);
    2. subject: a strong candidate the ask's own words name ("Photo Kit" in
       "the owner: 'Can you pick up the Photo Kit work'"), best score first; context
       names sources from other workstreams, the subject names the work;
    3. strong-match: the top candidate when its evidence is a named signal;
    4. named-in-context: the best strong candidate with a path, note, or slug
       pointer anywhere in the ask (the top candidate is often a big
       workstream whose shared words outscore every named signal).

    Tier 3 alone is not enough: a big workstream's config sits at rank 1 on shared
    words for almost every long ask, so every named owner below it would be ignored."""
    candidates = [c for c in (pack.get("candidate_owners") or []) if workstream_exists(str(c.get("slug") or ""))]
    path = str(pack.get("path_owner") or "")
    if workstream_exists(path):
        return next((c for c in candidates if c.get("slug") == path), {"slug": path, "score": 0, "evidence": []}), "path"
    # path_owner excludes a finished workstream, so naming one here must not bring it back.
    strong = [c for c in candidates if strong_owner(c, cfg) and not has_evidence(c, "terminal status")]
    subject = [c for c in strong if has_evidence(c, SUBJECT_EVIDENCE)]
    if subject:
        return subject[0], "subject"
    if candidates and strong_owner(candidates[0], cfg):
        return candidates[0], "strong-match"
    pointed = [c for c in strong if has_evidence(c, POINTER_EVIDENCE)]
    if pointed:
        return pointed[0], "named-in-context"
    return None, ""


# Evidence that is only word overlap or a status penalty: it never names an owner.
LEXICAL_EVIDENCE = ("shared topic terms", "terminal status", "paused owner")


def shared_terms(candidate: dict[str, Any]) -> set[str]:
    """Every word the candidate shares with the ask: router-context-pack's full
    `shared_terms` list, else the (first six) terms its evidence line names."""
    if candidate.get("shared_terms") is not None:
        return {str(t) for t in candidate["shared_terms"]}
    line = next((str(e) for e in candidate.get("evidence") or [] if str(e).startswith("shared topic terms: ")), "")
    return {t.strip() for t in line.removeprefix("shared topic terms: ").split(",") if t.strip()}


def weak_owner(candidates: list[dict[str, Any]], new_slug: str) -> dict[str, Any] | None:
    """The existing owner worth naming beside a new workstream as "closest existing
    owner": the first candidate with any named evidence, or one that shares a word
    of the new workstream's name. Overlap on other words is noise: an unrelated
    owner should not be offered as the fold target on "owner, any, available"."""
    name_words = {w for w in new_slug.split("-") if len(w) >= 3 and not re.fullmatch(r"[0-9a-f]{8}", w)}
    for candidate in candidates:
        named = any(not str(e).startswith(LEXICAL_EVIDENCE) for e in candidate.get("evidence") or [])
        if named or shared_terms(candidate) & name_words:
            return {"slug": str(candidate["slug"]), "score": float(candidate.get("score") or 0)}
    return None


def closest_owner(pack: dict[str, Any]) -> dict[str, Any] | None:
    """The candidate whose name the ask's own words mostly spell, best score first."""
    return next((c for c in (pack.get("candidate_owners") or [])
                 if workstream_exists(str(c.get("slug") or "")) and has_evidence(c, CLOSEST_EVIDENCE)), None)


def owner_from_links(prompt: str, cfg: dict[str, Any], exclude_task: str = "") -> dict[str, Any] | None:
    """Rank owners again with the titles of the artifacts the prompt links.

    An ask like "Pre-review of a Google Doc: <link>" names its owner only
    through the doc title. link_titles.py reads Drive, Slack, Linear, Jira, and GitHub titles in
    parallel with a short timeout; router-context-pack.py --source-title
    matches them against every workstream's name, slug, match terms, and
    description. Returns None when the prompt links nothing readable, else
    {"titles", "slug" (a strong owner, or ""), "weak_match"}. A new workstream
    is named from `titles` (create_workstream(..., source_titles=titles)).

    The linked content is read for systems before any title is ranked: a
    message titled "Build VM" about a failed refresh on a named VM belongs to
    the workstream that owns that VM, never a generic new one. A system owner returns
    {"slug", "system": <system_owners.resolve result>}."""
    if not link_titles.resolvable_urls(prompt):
        return None
    fetched = link_titles.fetch_titles(prompt)
    titles = [item["title"] for item in fetched]
    content = "\n".join(str(item.get("text") or "") for item in fetched).strip()
    owned = system_owner(content, exclude_task) if content else None
    if owned:
        log(f"linked content names {', '.join(owned['identifiers'][:3])}; owner {owned['slug']} "
            f"({'; '.join(owned['evidence'][:2])})")
        return {"titles": titles, "slug": owned["slug"], "weak_match": None, "system": owned}
    if not titles:
        log("no linked artifact title resolved; a new workstream is named from the ask or gets the "
            "workstream-<8hex> placeholder")
        return {"titles": [], "slug": "", "weak_match": None}
    result = run([sys.executable, str(SCRIPTS / "router-context-pack.py"), prompt,
                  *[f"--source-title={title}" for title in titles]], timeout=60)
    try:
        pack = json.loads(result.stdout)
    except (ValueError, TypeError):
        pack = {}
    candidates = [c for c in (pack.get("candidate_owners") or []) if workstream_exists(str(c.get("slug") or ""))]
    best = candidates[0] if candidates else {}
    picked, _tier = pick_owner(pack, cfg)
    if picked:
        log(f"linked title {titles[0]!r} names existing owner {picked['slug']}")
        return {"titles": titles, "slug": str(picked["slug"]), "weak_match": None}
    weak = {"slug": str(best["slug"]), "score": float(best.get("score") or 0)} if best else None
    return {"titles": titles, "slug": "", "weak_match": weak, "candidates": candidates}


def create_workstream(prompt: str, slug: str = "", source_titles: list[str] | None = None) -> str:
    """Scaffold a workstream through new-workstream.py (CONTEXT.md, config.yaml
    with a picked emoji, inbox, README re-render) and return its slug, or "" when
    the scaffold failed. `slug` is the proposed name from the ask ("Workstream:
    <new-slug>"); without one the script derives a slug from the prompt.
    `source_titles` are linked-artifact titles owner_from_links already read:
    the name comes from them, and an empty list (the lookup failed) stops the
    script from fetching again."""
    cmd = [sys.executable, str(SCRIPTS / "new-workstream.py"), prompt, "--json", "--no-inbox"]
    if slug:
        cmd += ["--name", slug]
    elif source_titles:
        cmd += [f"--source-title={title}" for title in source_titles]
    elif source_titles is not None:
        cmd.append("--no-fetch")
    scaffold = run(cmd, timeout=120)
    try:
        payload = json.loads(scaffold.stdout.strip().splitlines()[-1])
        created = str(payload.get("slug") or "")
        quality = str(payload.get("name_quality") or "")
    except (ValueError, IndexError, AttributeError):
        created, quality = "", ""
    if workstream_exists(created):
        how = ("proposed in the ask" if slug else f"named from the linked title {source_titles[0]!r}" if source_titles
               else "derived from the prompt")
        log(f"created workstream {created} ({how})")
        if quality in {"generic", "hash"}:
            # The ask had no topic word and no readable linked source; the name
            # is a placeholder the owner will want to rename (naming rule).
            log(f"workstream {created} has a placeholder name ({quality}); rename it to a 1-3 word topic title")
        commit_scaffold(created, "scaffolded by dispatch.py for the ask")
        return created
    detail = (getattr(scaffold, "stderr", "") or getattr(scaffold, "stdout", "") or "").strip()[:200]
    log(f"new-workstream.py did not scaffold {slug or 'a workstream'}: {detail}")
    return ""


def plan_workstream(prompt: str, slug: str = "") -> dict[str, Any] | None:
    """The workstream a scaffold would create, derived offline with nothing
    written: new-workstream.py --dry-run --no-fetch (about 0.1 s). The ack
    path uses this so the CoS tool call writes only the ledger and stays short;
    materialize_pending_workstream scaffolds it in the launcher.
    Returns {"slug", "name", "quality", "exists", "proposed"} or None when the
    name fails the naming rule (the caller falls through as create_workstream's
    "" does)."""
    cmd = [sys.executable, str(SCRIPTS / "new-workstream.py"), prompt, "--json", "--dry-run", "--no-fetch"]
    if slug:
        cmd += ["--name", slug]
    result = run(cmd, timeout=15)
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError, AttributeError):
        payload = {}
    planned = str(payload.get("slug") or "")
    if getattr(result, "returncode", 1) or not planned:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()[:200]
        log(f"new-workstream.py --dry-run gave no workstream for {slug or 'the prompt'}: {detail}")
        return None
    return {"slug": planned, "name": str(payload.get("name") or titleize(planned)),
            "quality": str(payload.get("name_quality") or ""), "exists": bool(payload.get("exists")),
            "proposed": bool(slug)}


def materialize_pending_workstream(task: dict[str, Any]) -> dict[str, Any]:
    """Scaffold the workstream the ack planned (row["pending_workstream"]) and
    commit it on main, in the detached launcher before the worktree is cut.
    A proposed name is created exactly as the ack reported it (--name).
    Otherwise the linked titles the ack skipped are read first
    (owner_from_links): a title that names an existing workstream repoints
    the row there with no scaffold, and any other title names the new
    workstream. With no readable title an ask-derived name is created as
    planned and a placeholder is re-derived offline. A changed slug is logged. When the scaffold
    fails the row falls back to the default owner. Returns the (possibly
    updated) task; a row with no pending workstream is returned unchanged."""
    pending = task.get("pending_workstream") or {}
    slug = str(task.get("workstream") or "")
    if not pending:
        return task
    prompt = task.get("prompt") or ""
    linked = None
    fresh = False   # scaffolded here from the ask or a title, before anyone read the content
    if workstream_exists(slug):
        created = slug
    elif pending.get("proposed"):
        created = create_workstream(prompt, slug)
    else:
        # The ack named this offline; read the linked titles now. A title that
        # names an existing workstream routes there with no scaffold, else the
        # title names the new one.
        linked = owner_from_links(prompt, config(), exclude_task=task["id"])
        if linked and linked["slug"]:
            created = linked["slug"]
        elif linked and linked["titles"]:
            created = create_workstream(prompt, source_titles=linked["titles"])
            fresh = bool(created)
        elif pending.get("quality") not in {"generic", "hash"}:
            created = create_workstream(prompt, slug, source_titles=[] if linked else None)
            fresh = bool(created)
        else:
            created = create_workstream(prompt, source_titles=[] if linked else None)
            fresh = bool(created)
    if not created:
        created = str(config().get("default_workstream") or DEFAULTS["default_workstream"])
        log(f"{task['id']}: scaffold of {slug} failed; routed to the default owner {created}")
    elif created != slug:
        how = (f"the linked source names {', '.join(linked['system']['identifiers'][:2])}, which it owns"
               if linked and linked.get("system")
               else "the linked title names this existing workstream" if linked and linked["slug"]
               else "after reading the linked source")
        log(f"{task['id']}: workstream {slug} (named offline in the ack) became {created}: {how}")

    def settle(payload):
        row = ledger.find_task(payload, task["id"])
        if row is None:
            return None
        row["workstream"] = created
        row.pop("pending_workstream", None)
        if linked and linked.get("system"):
            row["system_owner"] = {k: linked["system"][k] for k in ("identifiers", "evidence")}
        if fresh:
            row["provisional_workstream"] = True   # named before anyone read the content; the worker may re-home
        row["updated_at"] = ledger.now_iso()
        return dict(row)

    settled = ledger.locked_mutation(settle)
    out = {**task, "workstream": created}
    out.pop("pending_workstream", None)
    return {**out, **(settled or {})}


SCAFFOLD_README = "workstreams/README.md"


def scaffold_paths(slug: str) -> list[str]:
    return [f"workstreams/{slug}", SCAFFOLD_README]


def commit_scaffold(slug: str, why: str) -> dict[str, Any]:
    """Commit a freshly scaffolded workstream on main (its folder: CONTEXT.md and
    config.yaml; the verbatim ask in private/<date>-asks.md is gitignored and never
    staged; plus the re-rendered README) before any
    worktree is cut from main. new-workstream.py writes on main's working tree
    and never commits, and `git worktree add ... main` branches from main's
    HEAD commit, so without this the worker's checkout lacks its own
    workstream folder and a recreated scaffold hits an add/add conflict at
    landing. Attributed like
    commit_main_overlap; nothing is discarded. Anything a human staged on main
    outside these paths is left alone and reported, never swept in."""
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode:
        return {"committed": False, "reason": f"{REPO_ROOT} is not a git checkout"}
    paths = scaffold_paths(slug)
    dirty = dirty_main_paths()
    touched = [p for p in paths if any(d == p or d.startswith(p + "/") for d in dirty)]
    if not touched:
        return {"committed": False, "reason": "scaffold left nothing uncommitted"}
    foreign = staged_outside(touched)
    if foreign:
        log(f"scaffold of {slug} left uncommitted: main's index already holds {', '.join(foreign[:5])}")
        return {"committed": False, "reason": f"main's index already holds staged changes to {', '.join(foreign[:5])}"}
    add = stage_main_paths(touched)
    if add.returncode:
        git("reset", "-q", "--", *touched, check=False)
        return {"committed": False, "reason": add.stderr.strip() or "git add failed"}
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return {"committed": False, "reason": "nothing staged for the scaffold paths"}
    message = f"{slug}: scaffold workstream ({why}); committed on main by dispatch.py before the worker branch is cut"
    commit = git("-c", "user.name=switchboard worker", "-c", "user.email=switchboard-worker@local", "commit", "-q",
                 "-m", message, check=False)
    if commit.returncode:
        git("reset", "-q", "--", *touched, check=False)
        reason = (commit.stderr or commit.stdout).strip()[:200] or "git commit failed"
        log(f"scaffold of {slug} not committed: {reason}")
        return {"committed": False, "reason": reason}
    sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    log(f"scaffold of {slug} committed on main as {sha}")
    return {"committed": True, "commit": sha, "paths": touched}


def workstream_untracked(slug: str) -> bool:
    """True when workstreams/<slug>/ exists on main's working tree but HEAD
    tracks nothing under it: a scaffold some other writer (process-new-inbox.py,
    the chief-of-staff skill, a hand-made folder) left uncommitted. An existing
    workstream with human edits in flight is never reported here."""
    folder = REPO_ROOT / "workstreams" / slug
    if not slug or not folder.is_dir():
        return False
    tracked = git("ls-tree", "-d", "--name-only", "HEAD", "--", f"workstreams/{slug}", check=False)
    return tracked.returncode == 0 and not tracked.stdout.strip()


# switchboard-maintenance is only for switchboard itself: an owner task such as organizing downloads or a
# DNS change gets its own workstream. CoS had written `Workstream: switchboard-maintenance` on
# owner-topic briefs, and a directive wins over routing. The check below warns on such a brief
# and never blocks it. Owner topics are the owner's files, domains, accounts
# and money, even when the work runs a script or a skill; system terms are switchboard internals, so
# a real switchboard fix that mentions Downloads (a guardrail, a backup) stays quiet.
MAINTENANCE_SLUG = "switchboard-maintenance"
MAINTENANCE_SYSTEM_TERMS = re.compile(
    r"\b(?:dispatch(?:\.py)?|router|ledger|worktrees?|worker tabs?|cmux|wmux|guardrails?|hooks?|"
    r"agents\.md|dispatch-core|command[- ]center|auto-push|report-to-cos|ask-cos|launchpad|digest|"
    r"start-day|closing-day|sweep|reap|kit|installer|nightly backup|backup job|process leak|"
    r"test suites?|test_\w+|regression tests?|switchboard (?:bug|fix|code|tooling|internals?)|"
    r"private identifier guard|identifier guard|connector|mcp|doctor|"
    # switchboard's own meta work (routing, the to-do system, review notes) names many owner
    # topics in one brief without being about any of them.
    r"to-dos?|todos?|to-do list|workstreams?|routing|review notes?|morning review|action review|"
    r"owner[- ]actions?)\b", re.IGNORECASE)
MAINTENANCE_OWNER_TERMS = re.compile(
    r"\b(?:downloads? folder|organi[sz]ing downloads|dropbox (?:links?|sharing|shared links?)|shared links?|"
    r"dns|domain|spf|dmarc|taxe?s?|bank|brokerage|"
    r"finances?|financial|money|budget|pay stubs?|statements?|insurance|medical|health (?:documents?|records?)|"
    r"passwords?|recovery phrase|account security|"
    r"travel|trip|itinerary)\b", re.IGNORECASE)


def maintenance_misfile_warning(prompt: str, slug: str) -> str:
    """One line when a brief sent to switchboard-maintenance reads as an owner topic, empty otherwise.
    A warning only: the dispatch proceeds. Owner topic means at least two distinct owner-topic
    terms that outweigh the switchboard system terms by half again: long briefs name a system term or
    two in passing (ask-cos, a kit zip in Downloads), and a real switchboard fix names several. It
    flags file, domain and account briefs, and stays quiet on guardrail, backup and scaffold
    fixes."""
    if slug != MAINTENANCE_SLUG:
        return ""
    text = EXPLICIT_WORKSTREAM.sub(" ", prompt or "")
    owner_hits = [m.group(0).lower() for m in MAINTENANCE_OWNER_TERMS.finditer(text)]
    found = list(dict.fromkeys(owner_hits))
    system_hits = len(MAINTENANCE_SYSTEM_TERMS.findall(text))
    if len(found) < 2 or len(owner_hits) <= 1.5 * system_hits:
        return ""
    return (f"warning: this brief reads as an owner topic ({', '.join(found[:3])}), not switchboard itself; "
            f"switchboard-maintenance is only for switchboard, so name a topic workstream next time")


def first_line_directive(prompt: str) -> str:
    """The slug of a `Workstream: <slug>` written as the prompt's first non-empty line, which is
    how CoS names an owner on purpose. Empty when the directive is elsewhere or absent."""
    for line in (prompt or "").splitlines():
        if line.strip():
            match = re.match(r"\s*Workstream:\s*`?([a-z0-9][a-z0-9-]*)", line, re.IGNORECASE)
            return match.group(1).lower() if match else ""
    return ""


def choose_workstream(prompt: str, cfg: dict[str, Any], continuation_of: str | None = None,
                      defer_scaffold: bool = False) -> dict[str, Any]:
    """Owner decision for one dispatch, with its reason so the surface can tell the owner.

    1. an explicit "Workstream: <slug>" in the prompt wins outright; a slug that
       does not exist yet is the surface's proposed new workstream and is created
       (novel or tangential work gets its own workstream);
    2. a continuation inherits its parent task's workstream;
    3. a system routine (start of day, closing day, chief of staff) goes to
       `system_routine_workstream` (default owner when that folder is missing)
       without ranking, and the override is logged (SYSTEM_ROUTINE_RE);
    4. an existing owner is chosen only on strong evidence (see strong_owner),
       by tier (pick_owner): a unique workstream path, then what the ask's own
       words name, then the top candidate, then a path, note, or slug pointer
       anywhere in the ask;
    5. a prompt that links a Doc, Slack thread, issue, or PR is ranked again
       with the linked titles (owner_from_links); a title that names an
       existing owner routes there (reason "source-title");
    6. with no link to read, a candidate whose name the ask's own words mostly
       spell takes it (reason "closest-owner": when unsure, prefer the closest
       existing owner over creating one);
    6b. otherwise a new workstream is scaffolded, named from the linked title
       first, then the prompt; the closest weak candidate is reported so the owner
       can fold the work into it instead, only when it shares a word of the new
       name or has named evidence (weak_owner);
    7. the default owner only when the scaffold itself fails.

    Returns {"slug", "reason", "created", "weak_match"}; reason is one of
    directive, directive-created, continuation, system-routine, path, subject,
    strong-match, named-in-context, closest-owner,
    system-owner (the linked content names a system the workstream owns, read
    before titles), source-title, created, default.
    With `defer_scaffold` (the async ack) steps 1 and 6 only plan the new
    workstream (plan_workstream, nothing written, offline) and add "pending"
    with the derived name; step 5 is skipped because the title lookup costs
    seconds. The launcher (materialize_pending_workstream) reads the linked
    titles, routes to the owner a title names, or scaffolds the new one."""
    def new_owner(slug: str = "", source_titles: list[str] | None = None) -> tuple[str, dict[str, Any] | None]:
        if not defer_scaffold:
            return create_workstream(prompt, slug, source_titles=source_titles), None
        plan = plan_workstream(prompt, slug)
        if plan is None:
            return "", None
        return plan["slug"], (None if plan["exists"] else plan)

    match = EXPLICIT_WORKSTREAM.search(prompt)
    directed = match.group(1).lower() if match else ""
    if workstream_exists(directed):
        return {"slug": directed, "reason": "directive", "created": False, "weak_match": None}
    # A continuation of a misfiled task goes where CoS names it: a first-line `Workstream:
    # <new-slug>` creates that topic workstream instead of inheriting the parent's. A slug
    # mentioned further down (a quoted parent prompt) still inherits.
    named = first_line_directive(prompt) if continuation_of else ""
    if named and named == directed and named not in RESERVED_WORKSTREAM_SLUGS and not named.startswith("_"):
        created, pending = new_owner(named)
        if created:
            return {"slug": created, "reason": "directive-created", "created": True, "weak_match": None, "pending": pending}
    if continuation_of:
        parent = ledger.find_task(ledger.load_payload(), continuation_of) or {}
        if workstream_exists(str(parent.get("workstream") or "")):
            return {"slug": str(parent["workstream"]), "reason": "continuation", "created": False, "weak_match": None}
    if system_routine(prompt):
        owner = str(cfg.get("system_routine_workstream") or "")
        if not workstream_exists(owner):
            owner = str(cfg["default_workstream"])
        log(f"system routine routed to {owner} (ranking skipped): {prompt.strip()[:60]!r}")
        log_router_miss(prompt, owner, "system-routine")
        return {"slug": owner, "reason": "system-routine", "created": False, "weak_match": None}
    if directed and directed not in RESERVED_WORKSTREAM_SLUGS and not directed.startswith("_"):
        created, pending = new_owner(directed)
        if created:
            return {"slug": created, "reason": "directive-created", "created": True, "weak_match": None, "pending": pending}
    result = run([sys.executable, str(SCRIPTS / "router-context-pack.py"), prompt], timeout=60)
    try:
        pack = json.loads(result.stdout)
    except ValueError:
        pack = {}
    pack_directed = str(pack.get("explicit_workstream") or "")
    if workstream_exists(pack_directed):
        return {"slug": pack_directed, "reason": "directive", "created": False, "weak_match": None}
    candidates = [c for c in (pack.get("candidate_owners") or []) if workstream_exists(str(c.get("slug") or ""))]
    best = candidates[0] if candidates else {}
    picked, tier = pick_owner(pack, cfg)
    if picked:
        if tier != "strong-match":
            log(f"owner {picked['slug']} by {tier} ({'; '.join(str(e) for e in (picked.get('evidence') or [])[:2])})")
        return {"slug": str(picked["slug"]), "reason": tier, "created": False, "weak_match": None}
    weak = {"slug": str(best["slug"]), "score": float(best.get("score") or 0)} if best else None
    linked = None if defer_scaffold else owner_from_links(prompt, cfg)
    if linked and linked["slug"]:
        return {"slug": linked["slug"], "reason": "system-owner" if linked.get("system") else "source-title",
                "created": False, "weak_match": None}
    weak = (linked or {}).get("weak_match") or weak
    # Unsure, with nothing linked to read: the closest existing owner beats a new
    # workstream. A linked ask waits for its titles instead.
    close = None if link_titles.resolvable_urls(prompt) else closest_owner(pack)
    if close:
        log(f"no owner named; closest existing owner {close['slug']} takes the ask "
            f"({next(e for e in close['evidence'] if str(e).startswith(CLOSEST_EVIDENCE))})")
        log_router_miss(prompt, str(close["slug"]), "closest-owner")
        return {"slug": str(close["slug"]), "reason": "closest-owner", "created": False, "weak_match": None}
    created, pending = new_owner(source_titles=(linked or {}).get("titles"))
    if created:
        # Only an owner that shares the new name's words is worth offering as the fold target.
        weak = weak_owner((linked or {}).get("candidates") or candidates, created)
        return {"slug": created, "reason": "created", "created": True, "weak_match": weak, "pending": pending,
                "links": bool(defer_scaffold and link_titles.resolvable_urls(prompt))}
    return {"slug": str(cfg["default_workstream"]), "reason": "default", "created": False, "weak_match": weak}


def owner_note(choice: dict[str, Any]) -> str:
    """Suffix for the dispatch message when the owner was created, so the surface's
    verbatim reply asks the owner in the same turn (keep it, or fold it into the closest
    existing owner). A misfile warning (maintenance_misfile_warning) rides along on any choice."""
    if choice.get("warning"):
        return f"; {choice['warning']}"
    if not choice.get("created"):
        return ""
    pending = choice.get("pending") or {}
    if pending and not pending.get("proposed") and choice.get("links"):
        # The async ack read no titles; the launcher picks the owner from them.
        return (f" in workstream {choice['slug']} for now; the launcher reads the linked source first and moves to "
                "the owner of the system or title it names, or renames the new workstream after it")
    weak = choice.get("weak_match") or {}
    closest = f"; closest existing owner {weak['slug']} ({weak['score']:.0f}, weak match)" if weak else ""
    how = "proposed in the ask" if choice.get("reason") == "directive-created" else "derived from the prompt"
    return f" in new workstream {choice['slug']} ({how}{closest}); the owner: keep it, or name the owner to fold it into"


def pick_workstream(prompt: str, cfg: dict[str, Any], continuation_of: str | None = None) -> str:
    return str(choose_workstream(prompt, cfg, continuation_of)["slug"])


def agent_mode(slug: str) -> str:
    result = run(["bash", "-c", f'source "$1/system/cmux-helpers.sh" >/dev/null 2>&1; resolve_agent_mode "$2"',
                  "_", str(REPO_ROOT), slug], timeout=30)
    mode = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return mode if mode in {"high", "ultra"} else "ultra"


def review_rule() -> str:
    result = run(["bash", "-c", 'source "$1/system/cmux-helpers.sh" >/dev/null 2>&1; printf %s "$WORKSTREAM_REVIEW_SURFACE_RULE"',
                  "_", str(REPO_ROOT)], timeout=30)
    return result.stdout.strip()


# --- worktrees ---------------------------------------------------------------------------

def worktree_path(task_id: str) -> Path:
    return WORKTREES / task_id


def is_ignored(rel: str) -> bool:
    return git("check-ignore", "-q", "--", rel, check=False).returncode == 0


def ignored_paths(rels: list[str]) -> set[str]:
    """Which of rels git ignores, in one process. One git per entry cost about 460 processes per
    launch, and each stays until reboot on Windows."""
    if not rels:
        return set()
    result = run(["git", "-C", str(REPO_ROOT), "check-ignore", "--stdin", "-z"], input="\0".join(rels) + "\0", encoding="utf-8")
    if result.returncode not in (0, 1):  # 128: git failed, so ask one path at a time as before
        return {rel for rel in rels if is_ignored(rel)}
    return {path for path in result.stdout.split("\0") if path}


SCRATCH = ".amp/in"
# Ignored entries under .amp that are never symlinked into a worktree: the
# worktree directory itself, and the scratch dir, which is worktree-local.
# When .amp/in is a link to main's, a scratch script that derives the repo root
# from its own resolved path (Path(__file__).resolve().parents[n]) resolves
# through the link into MAIN and writes its outputs there. .amp/locks stays shared: it holds
# the flock files `dispatch.py lock` serializes non-repo writes with.
WORKTREE_LOCAL = {".amp/worktrees", SCRATCH}


def link_shared_state(worktree: Path) -> None:
    """Symlink main's ignored state (ledger, logs, locks) into the worktree so
    every writer keeps one home. Tracked files stay real; todo.json is ignored state
    and is linked like the rest, so todo edits never fork across branches. Every link created
    here is also written root-anchored into the shared .git/info/exclude, so a
    later .gitignore edit that un-ignores the directory cannot turn the link into
    an untracked file that `git add -A` sweeps up. `.amp/in` is
    created as a real, worktree-local directory instead (see WORKTREE_LOCAL);
    migrate_scratch() moves its contents into main's `.amp/in` when the task lands
    or its worktree is dropped."""
    created: list[str] = []
    fallbacks: dict[str, int] = {}
    for base in ("", "system", "system/state", ".amp"):
        source_dir = REPO_ROOT / base if base else REPO_ROOT
        if not source_dir.is_dir():
            continue
        entries = sorted(source_dir.iterdir())
        ignored = ignored_paths([f"{base}/{entry.name}" if base else entry.name for entry in entries])
        for entry in entries:
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.name == ".git" or rel in WORKTREE_LOCAL or winlink.is_transient(entry.name) or rel not in ignored:
                # A writer's temp file (running-tasks.json.<pid>.tmp) is never shared state;
                # lock files are, and stay linked (see winlink.is_transient).
                continue
            target = worktree / rel
            if target.exists() or winlink.is_link(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                kind = winlink.link_path(entry, target)
            except winlink.LinkUnavailable as error:
                # Windows without Developer Mode and no junction or hard link possible
                # (a file on another volume). Never copy: a copy forks the shared state.
                log(f"shared-state link for {rel} skipped: {error}")
                continue
            if kind != "symlink":
                fallbacks[kind] = fallbacks.get(kind, 0) + 1
            created.append(rel)   # a hard-linked file is listed too, so info/exclude still names every shared path
    if fallbacks:
        # Windows without Developer Mode. A hard link would go stale after an atomic rewrite
        # on main, so every shared-state writer relinks worktrees after its replace
        # (winlink.relink_shared) and the sweep heals the rest each tick (heal_shared_links).
        # One line per launch, not one per file.
        counts = ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in sorted(fallbacks.items()))
        log(f"shared-state links made without symlinks ({counts}); hard links are relinked after "
            "each rewrite on main, so they stay current without Developer Mode")
    exclude_shared_links(created)
    scratch = worktree / SCRATCH
    if not winlink.is_link(scratch):
        scratch.mkdir(parents=True, exist_ok=True)
    for rel in ("system/state/todo.json",):
        tracked = REPO_ROOT / rel
        target = worktree / rel
        if tracked.exists() and target.exists() and not winlink.is_shared(target, tracked):
            # Build the link beside the file and swap it in, so a platform that cannot link
            # leaves the file as it was. Only an old checkout that still tracks todo.json gets here.
            staged = target.with_name(target.name + ".link-tmp")
            try:
                if winlink.is_link(staged):
                    winlink.remove_link(staged)     # left by an interrupted earlier run
                elif staged.exists():
                    staged.unlink()
                winlink.link_path(tracked, staged)
                _os_replace(staged, target)
            except (winlink.LinkUnavailable, OSError) as error:
                log(f"{rel} stays a plain worktree file: {error}")
                if winlink.is_link(staged) or staged.exists():
                    try:
                        winlink.remove_link(staged) if winlink.is_link(staged) else staged.unlink()
                    except OSError:
                        pass
                continue
            # Only once the link is in place: a skip-worktree plain file would hide real edits.
            git("update-index", "--skip-worktree", "--", rel, cwd=worktree, check=False)


def exclude_file() -> Path:
    out = git("rev-parse", "--git-path", "info/exclude", check=False).stdout.strip()
    path = Path(out) if out else REPO_ROOT / ".git" / "info" / "exclude"
    return path if path.is_absolute() else REPO_ROOT / path


def exclude_shared_links(rels: list[str]) -> list[str]:
    """Append `/<rel>` for each shared-state link to the repository's info/exclude
    (shared by main and every worktree) once; existing lines are kept, except temp-file
    lines (winlink.is_transient), which are never shared state. Returns the lines added."""
    if not rels:
        return []
    path = exclude_file()
    try:
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return []
    # Drop temp-file lines an earlier launch recorded (running-tasks.json.<pid>.tmp),
    # so the heal stops visiting them.
    kept = [line for line in existing if not (line.strip().startswith("/") and winlink.is_transient(line.strip()))]
    have = {line.strip() for line in kept}
    added = [f"/{rel}" for rel in dict.fromkeys(rels) if f"/{rel}" not in have and not winlink.is_transient(rel)]
    if added or len(kept) != len(existing):
        lines = list(kept)
        if EXCLUDE_HEADER not in have:
            lines.append(EXCLUDE_HEADER)
        lines.extend(added)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


EXCLUDE_HEADER = "# shared-state links created by dispatch.py link_shared_state (never tracked)"


def migrate_scratch(worktree: Path, task_id: str) -> dict[str, Any]:
    """Move the worktree's own `.amp/in` contents into main's `.amp/in`, where
    review notes, scheduled jobs and later tasks read scratch from. Directories
    merge recursively; a file that already exists in main is never overwritten:
    the incoming copy lands beside it as `<stem>.<task-id><suffix>`. Renames stay
    on one volume, so this is cheap whatever the files weigh. A worktree whose
    `.amp/in` is still a symlink to main (an older layout) has nothing
    of its own to move and is left alone. Returns {moved, renamed} or {skipped}."""
    source = worktree / SCRATCH
    if winlink.is_link(source):
        return {"skipped": "symlink to main"}
    if not source.is_dir():
        return {"moved": 0, "renamed": []}
    dest_root = REPO_ROOT / SCRATCH
    if os.path.realpath(source) == os.path.realpath(dest_root):
        return {"skipped": "same directory"}
    moved, renamed = 0, []

    def merge(src: Path, dest: Path) -> None:
        nonlocal moved
        dest.mkdir(parents=True, exist_ok=True)
        for entry in sorted(src.iterdir()):
            target = dest / entry.name
            if entry.is_dir() and not winlink.is_link(entry) and target.is_dir() and not winlink.is_link(target):
                merge(entry, target)
                try:
                    entry.rmdir()
                except OSError:
                    pass
                continue
            if target.exists() or winlink.is_link(target):
                target = dest / f"{entry.stem}.{task_id}{entry.suffix}"
                n = 2
                while target.exists() or winlink.is_link(target):
                    target = dest / f"{entry.stem}.{task_id}-{n}{entry.suffix}"
                    n += 1
                renamed.append(target.relative_to(REPO_ROOT).as_posix())
            shutil.move(str(entry), str(target))
            moved += 1

    try:
        merge(source, dest_root)
    except OSError as error:
        log(f"{task_id}: scratch migration from {source} stopped: {error}")
        return {"moved": moved, "renamed": renamed, "error": str(error)}
    if moved:
        log(f"{task_id}: moved {moved} scratch entries from {source} into {dest_root}" + (f" ({len(renamed)} renamed)" if renamed else ""))
    return {"moved": moved, "renamed": renamed}


# --- fsmonitor -----------------------------------------------------------------------------
# A ~/.gitconfig may set core.fsmonitor=true for every repo. A linked worktree
# then spawns its own `git fsmonitor--daemon` for .git/worktrees/<id>, and the first
# index refresh in it (the `reset --hard` inside `worktree add`) blocks on that
# daemon's socket: the daemon answers a query only after FSEvents has shown it its
# own cookie file, with no timeout (builtin/fsmonitor--daemon.c with_lock__wait_for_cookie),
# so a lagging FSEvents queue (dozens of daemons, one per checkout touched since
# boot) stalls the add for minutes at 0 % CPU. The
# same lag keeps a daemon alive after `worktree remove` (its shutdown is also an
# FSEvents event; a healthy one exits within a second). fsmonitor does not pay for
# itself in this repo either: `git status` in main is 2-8x slower with it on
# (0.3-1.1 s vs 0.1-0.25 s; 11.8k tracked files). So the repo config pins it off for
# main and every worktree, the add runs with it off even before that config exists
# (a fresh clone), and removing or moving a checkout first stops the daemon it may
# still have.
#
# Nested repos: a folder under knowledge/ can be a gitlink (mode
# 160000, no .gitmodules) with its own .git, so `git status` in main runs a child
# `git status --porcelain=2` inside it to learn whether it is dirty, and holds main's
# .git/index.lock the whole time. A child that inherits core.fsmonitor=true from
# ~/.gitconfig can block on a wedged daemon and hold the lock for minutes, failing
# landings. Three layers prevent it: dispatch's own status calls pass
# --ignore-submodules=all (dirtiness of a nested repo is never this script's
# business), ensure_fsmonitor_off() also pins core.fsmonitor=false in every nested
# repo, and the repo config sets diff.ignoreSubmodules=all so other pollers of main
# (Amp's Changes view, the cmux sidebar) stop recursing too.

FSMONITOR_OFF = ("-c", "core.fsmonitor=false")
FSMONITOR_STOP_TIMEOUT = 5.0   # seconds to give `fsmonitor--daemon stop` before the pid is killed
NO_SUBMODULES = "--ignore-submodules=all"
# Repo-local config pinned by ensure_fsmonitor_off(); shared by main and every worktree.
REPO_CONFIG_PINS = (("core.fsmonitor", "false"), ("diff.ignoreSubmodules", "all"))


def nested_repos(root: Path | None = None) -> list[Path]:
    """Populated nested git repos tracked as gitlinks (mode 160000) in `root`'s index."""
    root = root or REPO_ROOT
    out = git("ls-files", "--stage", cwd=root, check=False).stdout
    found: list[Path] = []
    for line in out.splitlines():
        if not line.startswith("160000 "):
            continue
        rel = line.split("\t", 1)[1] if "\t" in line else ""
        if rel and (root / rel / ".git").exists():
            found.append(root / rel)
    return found


def ensure_fsmonitor_off() -> None:
    """Pin core.fsmonitor=false (and diff.ignoreSubmodules=all) in the repository
    config, shared by main and every worktree and overriding ~/.gitconfig, so no
    checkout of this repo spawns a daemon or recurses into a nested repo; and pin
    core.fsmonitor=false in each populated nested repo so the child `git status`
    that other callers may still run there cannot hang on a daemon either."""
    for key, value in REPO_CONFIG_PINS:
        if git("config", "--local", "--get", key, check=False).stdout.strip() != value:
            git("config", "--local", key, value, check=False)
    for nested in nested_repos():
        if git("config", "--local", "--get", "core.fsmonitor", cwd=nested, check=False).stdout.strip() != "false":
            git("config", "--local", "core.fsmonitor", "false", cwd=nested, check=False)


def fsmonitor_lookup_available() -> bool:
    """True when the daemon list can be built: pgrep and lsof exist (macOS, Linux). Windows has
    neither, so nothing there can be looked up and callers fall back to `fsmonitor--daemon stop`."""
    return not winlink.is_windows() and bool(shutil.which("pgrep")) and bool(shutil.which("lsof"))


def fsmonitor_daemons() -> dict[int, dict[str, str]]:
    """{pid: {"root", "gitdir"}} for every `git fsmonitor--daemon` on this machine,
    from lsof. A daemon keeps open the checkout root it watches and, for a linked
    worktree, its .git/worktrees/<name> gitdir (each with its ancestors, so the
    deepest open directory of each kind is the one). lsof still reports those
    paths after the directories are deleted, which is how a daemon for a removed
    checkout is recognised; the socket <gitdir>/fsmonitor--daemon.ipc fills in the
    gitdir of a daemon that holds no gitdir descriptor (the repo's own).
    Empty when pgrep or lsof is missing (Windows): the caller cannot tell which daemon serves
    which checkout there."""
    if not fsmonitor_lookup_available():
        return {}
    try:
        pids = run(["pgrep", "-f", "git fsmonitor--daemon run"], timeout=10).stdout.split()
        if not pids:
            return {}
        out = run(["lsof", "-a", "-p", ",".join(pids), "-Ftpn"], timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    dirs: dict[int, list[str]] = {}
    sockets: dict[int, str] = {}
    pid, kind = 0, ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
            dirs[pid] = []
        elif line.startswith("t"):
            kind = line[1:]
        elif line.startswith("n/") and pid:
            name = line[1:]
            if kind == "DIR":
                dirs[pid].append(name)
            elif kind == "unix" and name.endswith("/fsmonitor--daemon.ipc"):
                sockets[pid] = name[: -len("/fsmonitor--daemon.ipc")]
    daemons: dict[int, dict[str, str]] = {}
    for pid, names in dirs.items():
        gitdir = max((n for n in names if "/.git/" in n + "/"), key=len, default="") or sockets.get(pid, "")
        if gitdir.endswith("/.git"):
            root = gitdir[: -len("/.git")]   # the repo's own daemon: its gitdir sits inside the root it watches
        else:
            root = max((n for n in names if "/.git/" not in n + "/" and not gitdir.startswith(n + "/")), key=len, default="")
        daemons[pid] = {"root": root, "gitdir": gitdir}
    return daemons


def fsmonitor_daemon_pid(path: Path) -> int | None:
    """PID of the daemon serving the checkout at `path`, or None: the one watching
    that checkout's gitdir or the checkout root itself (never an ancestor, so a
    worktree's daemon and the repo's own daemon are told apart)."""
    root = os.path.realpath(path)
    try:
        text = (path / ".git").read_text(encoding="utf-8").strip() if (path / ".git").is_file() else ""
    except OSError:
        text = ""
    if text.startswith("gitdir:"):
        gitdir = os.path.realpath(text.split(":", 1)[1].strip())
    elif (path / ".git").is_dir():
        gitdir = os.path.realpath(path / ".git")
    else:
        gitdir = os.path.realpath(REPO_ROOT / ".git" / "worktrees" / path.name)
    for pid, held in sorted(fsmonitor_daemons().items()):
        if any(value and os.path.realpath(value) == want for value, want in ((held["gitdir"], gitdir), (held["root"], root))):
            return pid
    return None


def stop_fsmonitor_daemon(path: Path) -> int | None:
    """Stop the fsmonitor daemon still serving `path`, if any, before the checkout or
    its gitdir is removed or moved (otherwise the daemon lives on). `fsmonitor--daemon
    stop` first; the pid is signalled when that hangs or does not end it (the stop
    request travels the same socket the query hung on). Returns the pid stopped."""
    pid = fsmonitor_daemon_pid(path)
    if pid is None:
        if not fsmonitor_lookup_available() and path.is_dir() and (path / ".git").exists():
            # No pgrep or lsof (Windows): the daemon cannot be found, but `stop` run in the
            # checkout reaches it by its own socket. It fails harmlessly when none runs.
            try:
                run(["git", "-C", str(path), *FSMONITOR_OFF, "fsmonitor--daemon", "stop"], timeout=FSMONITOR_STOP_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None
    if path.is_dir():
        try:
            run(["git", "-C", str(path), *FSMONITOR_OFF, "fsmonitor--daemon", "stop"], timeout=FSMONITOR_STOP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for _ in range(int(FSMONITOR_STOP_TIMEOUT * 10)):
        if not pid_alive(pid):
            return pid
        time.sleep(0.1)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))   # Windows has no SIGKILL; SIGTERM there is TerminateProcess. A daemon the stop request cannot reach is wedged; nothing in it is worth a graceful exit
    except (ProcessLookupError, PermissionError):
        pass
    return pid


def pid_alive(pid: int) -> bool:
    return winlink.pid_alive(pid)   # os.kill(pid, 0) would terminate the process on Windows


def create_worktree(task_id: str) -> tuple[Path, str]:
    branch = f"task/{task_id}"
    path = worktree_path(task_id)
    WORKTREES.mkdir(parents=True, exist_ok=True)
    ensure_fsmonitor_off()
    if path.exists():
        stop_fsmonitor_daemon(path)
        migrate_scratch(path, task_id)
        remove_shared_links(path)
        git("worktree", "remove", "--force", str(path), check=False)
        winlink.rmtree(path, ignore_errors=True)
    git("worktree", "prune", check=False)
    git("branch", "-D", branch, check=False)
    git(*FSMONITOR_OFF, "worktree", "add", "-b", branch, str(path), "main")
    link_shared_state(path)
    return path, branch


def verify_worktree(path: Path, branch: str) -> str:
    """Why `path` is not a usable worktree on `branch`, or "" when it is (fix
    B1-lite): the directory exists, its .git is a file (a linked worktree, not a
    nested repo or main itself), HEAD is the task branch, and the main repo lists
    it. A launcher that opens a tab on anything else hands the worker a checkout
    whose edits can never land."""
    if not path.is_dir():
        return f"{path} does not exist"
    if not (path / ".git").is_file():
        return f"{path}/.git is not a worktree link file"
    head = git("symbolic-ref", "--short", "-q", "HEAD", cwd=path, check=False).stdout.strip()
    if head != branch:
        return f"{path} has {head or 'a detached HEAD'} checked out, not {branch}"
    listing = git("worktree", "list", "--porcelain", check=False).stdout
    listed = {os.path.realpath(l[len("worktree "):]) for l in listing.splitlines() if l.startswith("worktree ")}
    if os.path.realpath(path) not in listed:
        return f"{path} is not in `git worktree list`"
    return ""


ABANDONED = "_abandoned"


def worktree_dirty(path: Path) -> list[str]:
    """Status lines for work the checkout holds (tracked edits, untracked files),
    minus the shared-state links link_shared_state() created."""
    if not path.is_dir() or not (path / ".git").exists():
        return []
    status = git("status", "--porcelain", "--untracked-files=all", NO_SUBMODULES, "--", ".", ":(exclude)system/state", cwd=path, check=False)
    if status.returncode:
        return ["?? (git status failed)"]   # unknown is never "clean": the caller parks instead of deleting
    return [line for line in status.stdout.splitlines() if line and not _shared_state_link(path, line)]


def park_worktree(path: Path, task_id: str) -> Path | None:
    """Move a checkout that still holds work to WORKTREES/_abandoned/<task>-<utc>
    instead of deleting it (fix B3-lite). The branch stays. Returns the new path."""
    parking = WORKTREES / ABANDONED
    parking.mkdir(parents=True, exist_ok=True)
    dest = parking / f"{task_id}-{now().strftime('%Y%m%dT%H%M%SZ')}"
    moved = git("worktree", "move", str(path), str(dest), check=False)
    if moved.returncode:
        try:
            shutil.move(str(path), str(dest))
        except OSError as error:
            log(f"{task_id}: could not park dirty worktree {path}: {error}")
            return None
        git("worktree", "repair", str(dest), check=False)
        git("worktree", "prune", check=False)
    return dest


SHARED_LINK_BASES = ("", "system", "system/state", ".amp")   # the directories link_shared_state() links into


def remove_shared_links(worktree: Path) -> list[str]:
    """Delete every shared-state link (symlink or junction) link_shared_state() made in
    `worktree`, each on its own: only the link goes, never what it points at. Run before
    `git worktree remove --force` and any recursive delete. A junction reads as a plain
    directory to older Pythons and to git, so a recursive delete could walk through it
    and empty main's people/, .amp, or system/state. A hard-linked file is only another
    name for main's file, so deleting that name is safe and is left to the delete.
    Returns the relative paths unlinked."""
    removed: list[str] = []
    for base in SHARED_LINK_BASES:
        folder = worktree / base if base else worktree
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git" or not winlink.is_link(entry.path):
                continue
            try:
                winlink.remove_link(entry.path)
            except OSError as error:
                log(f"could not unlink shared-state link {entry.path}: {error}")
                continue
            removed.append(f"{base}/{entry.name}" if base else entry.name)
    return removed


def drop_worktree(task: dict[str, Any]) -> Path | None:
    """Remove a task's checkout and branch. A checkout with uncommitted or
    untracked work is never deleted: it is parked under _abandoned (branch kept)
    and the parked path is returned so the caller can say so. A parked checkout
    keeps its own `.amp/in` (whoever resumes it needs the scratch); a removed one
    has its scratch moved into main's `.amp/in` first, since git status never
    counts the ignored scratch dir as work worth parking."""
    path = Path(task.get("worktree") or worktree_path(task["id"]))
    stopped = stop_fsmonitor_daemon(path)
    if stopped:
        log(f"{task['id']}: stopped fsmonitor daemon pid {stopped} for {path}")
    stop_worktree_leftovers(task, path)   # before the park or delete: a cwd inside it blocks both on Windows
    if worktree_dirty(path):
        parked = park_worktree(path, task["id"])
        if parked is not None:
            log(f"{task['id']}: worktree {path} holds uncommitted work; parked at {parked} (branch {task.get('branch') or '?'} kept)")
            return parked
    if path.exists():
        migrate_scratch(path, task["id"])
        remove_shared_links(path)   # first: a junction must never be walked into main's people/, .amp, system/state
        git("worktree", "remove", "--force", str(path), check=False)
        winlink.rmtree(path, ignore_errors=True)
    git("worktree", "prune", check=False)
    if task.get("branch"):
        git("branch", "-D", task["branch"], check=False)
    return None


LEFTOVER_LOG = STATE_DIR / "worker-dispatch" / "leftover-stops.jsonl"   # one JSON line per stopped process
LEFTOVER_SWEEP_STAMP = STATE_DIR / "worker-dispatch" / "leftover-sweep.stamp"
LEFTOVER_SWEEP_EVERY = timedelta(minutes=30)   # a reap that closed nothing re-sweeps at most this often


def stop_worktree_leftovers(task: dict[str, Any], path: Path) -> dict[str, Any]:
    """Stop processes left running inside a checkout about to be removed or parked:
    working directory, open file, or command-line path inside it or its scratch
    (task_leftovers.py has the rules and what is never stopped). Best effort, never raises."""
    try:
        result = task_leftovers.stop_in_worktree(str(task["id"]), path, ledger.load_payload()["tasks"], repo_root=REPO_ROOT,
                                                 worktrees_root=WORKTREES, log_path=LEFTOVER_LOG, log=log)
    except Exception as error:   # a failed sweep must never block a worktree removal
        log(f"{task['id']}: leftover-process check for {path} failed: {error}")
        return {}
    if result.get("stopped") or result.get("failed"):
        log(f"{task['id']}: stopped {result.get('stopped', 0)} leftover process(es) in {path}"
            + (f", {result['failed']} could not be stopped" if result.get("failed") else ""))
    return result


def sweep_leftovers(force: bool) -> dict[str, Any]:
    """After reap: stop what every finished, reaped task left behind, including its
    env-tagged dev servers and monitors on a worktree that is already gone. Runs when
    this reap closed something, else at most every LEFTOVER_SWEEP_EVERY."""
    try:
        last = datetime.fromtimestamp(LEFTOVER_SWEEP_STAMP.stat().st_mtime, tz=timezone.utc)
    except OSError:
        last = None
    if not force and last is not None and now() - last < LEFTOVER_SWEEP_EVERY:
        return {}
    try:
        LEFTOVER_SWEEP_STAMP.parent.mkdir(parents=True, exist_ok=True)
        LEFTOVER_SWEEP_STAMP.touch()
        result = task_leftovers.sweep(ledger.load_payload()["tasks"], repo_root=REPO_ROOT, worktrees_root=WORKTREES,
                                      log_path=LEFTOVER_LOG, log=log)
    except Exception as error:
        log(f"leftover-process sweep failed: {error}")
        result = {}
    # prototyping react-starter servers recorded with their owning tab: stopped
    # once that tab closes or, with no tab recorded, once its agent session ends; never one started by hand.
    try:
        owned = task_leftovers.stop_owned_dev_servers(
            open_workspaces=lambda: {str(w.get("id")) for w in cmux_workspaces() if w.get("id")},
            log_path=LEFTOVER_LOG, log=log)
    except Exception as error:
        log(f"dev-server owner sweep failed: {error}")
        owned = {}
    if owned.get("servers"):
        result = {**result, "dev_servers": owned["servers"]}
    return result


def symlink_points_inside_repo(rel: str, target: str, cwd: Path) -> bool:
    """True when a symlink at `rel` (checked out under `cwd`) with link text
    `target` resolves inside the main checkout or one of its worktrees.

    This is the self-loop: link_shared_state() symlinks the
    worktree's ignored dirs (people/, .state/, …) at main's copies; if a
    .gitignore change un-ignores them they can be committed as absolute links to
    /…/switchboard/<same path>, and fast-forwarding main then replaces each real
    directory with a link to itself, deleting its contents. Absolute links into
    the repo are always machine-specific and never belong in a commit. Relative
    links that resolve to their own path are the same loop spelled differently.
    Ordinary relative links to other tracked paths (CLAUDE.md -> AGENTS.md) are
    portable and stay allowed."""
    def canonical(p: str) -> Path:
        # realpath of the parent only, so /var and /private/var compare equal
        # without following the final component (which may itself be the loop).
        return Path(os.path.realpath(os.path.dirname(p))) / os.path.basename(p)

    roots = [Path(os.path.realpath(REPO_ROOT)), Path(os.path.realpath(cwd))]
    if os.path.isabs(target):
        resolved = canonical(os.path.normpath(target))
        return any(resolved == root or root in resolved.parents for root in roots)
    here = canonical(str(roots[1] / rel))
    resolved = canonical(os.path.normpath(roots[1] / Path(rel).parent / target))
    return resolved == here


def loop_symlinks(cwd: Path, paths: list[str], tree: str | None = None) -> list[dict[str, str]]:
    """Symlinks among `paths` (in the staged index when tree is None, else in
    `tree`) that point inside the repository. Each entry: {path, target}."""
    if not paths:
        return []
    if tree is None:
        listing = git("ls-files", "-s", "-z", "--", *paths, cwd=cwd, check=False).stdout
        modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listing.split("\0") if "\t" in line}
        spec = ""
    else:
        listing = git("ls-tree", "-r", "-z", tree, "--", *paths, cwd=cwd, check=False).stdout
        modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listing.split("\0") if "\t" in line}
        spec = tree
    found = []
    for rel in paths:
        if modes.get(rel) != "120000":
            continue
        target = git("cat-file", "-p", f"{spec}:{rel}", cwd=cwd, check=False).stdout.rstrip("\n")
        if symlink_points_inside_repo(rel, target, cwd):
            found.append({"path": rel, "target": target})
    return found


def staged_paths(cwd: Path) -> list[str]:
    out = git("diff", "--cached", "--name-only", "-z", "--diff-filter=AMT", cwd=cwd, check=False).stdout
    return [p for p in out.split("\0") if p]


MASS_DELETE_LIMIT = 50  # finish: a branch deleting more tracked files than this needs --allow-mass-delete


def mass_change_refusal(branch: str, allow_mass_delete: bool) -> str:
    """Why `branch` must not be fast-forwarded onto main, or "": it
    deletes more than MASS_DELETE_LIMIT tracked files, or it adds a tracked path
    under a directory main ignores (people/, .state/: the self-loop shape,
    where the branch tracks a link where main keeps real, ignored data)."""
    deleted = [l for l in git("diff", "--name-only", "-z", "--diff-filter=D", f"main...{branch}", check=False).stdout.split("\0") if l]
    if len(deleted) > MASS_DELETE_LIMIT and not allow_mass_delete:
        return (f"branch {branch} deletes {len(deleted)} tracked files ({', '.join(deleted[:3])}…); more than {MASS_DELETE_LIMIT} "
                f"needs an explicit `dispatch.py finish --task-id <id> --allow-mass-delete`")
    added = [l for l in git("diff", "--name-only", "-z", "--diff-filter=A", f"main...{branch}", check=False).stdout.split("\0") if l]
    placed = tracked_into_ignored_dirs(added)
    if placed and not allow_mass_delete:
        shown = ", ".join(f"{p} (under {d})" for p, d in placed[:3])
        return (f"branch {branch} tracks {shown}{'…' if len(placed) > 3 else ''} where main keeps an ignored directory; "
                f"checking it out would overwrite the real files there. Remove it from the branch "
                f"(git rm --cached <path>; commit --amend) or pass --allow-mass-delete to dispatch.py finish on purpose")
    changed = [l for l in git("diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", f"main...{branch}", check=False).stdout.split("\0") if l]
    try:
        private = private_workstreams.slugs(REPO_ROOT)
    except OSError as error:
        return f"branch {branch} cannot be checked for private workstream files, so it is refused: {error}"
    carried = [p for p in changed if private_workstreams.is_private_path(p, private)]
    # Per commit too: a branch rebased by hand onto a rewritten main can add a private file in one
    # commit and drop it in the next, so the net diff is clean while auto-push would refuse again.
    carried = carried or unpushed_private_paths(f"main..{branch}")
    if carried:
        return (f"branch {branch} carries {len(carried)} private workstream file(s) ({', '.join(carried[:3])}{'…' if len(carried) > 3 else ''}). "
                f"Only CONTEXT.md and config.yaml are tracked there. Remove them from the branch (git rm --cached <path>; commit --amend) "
                f"and keep them in the folder on the main checkout. There is no override")
    return ""


def private_identifier_refusal(rev_range: str) -> str:
    """Why the commits in `rev_range` must not land or push, or "": one adds a value
    from local/private-identifiers.txt, or that list is known to exist and could not
    be checked. A machine with no list passes. Names the file and kind, never the value."""
    verdict = private_identifiers.guard_range(REPO_ROOT, rev_range)
    if verdict["status"] == "hit":
        return f"adds a private identifier: {verdict['reason']}"
    if verdict["status"] == "error":
        return f"cannot be checked for private identifiers, so it is refused: {verdict['reason']}"
    return ""


def tracked_into_ignored_dirs(added: list[str]) -> list[tuple[str, str]]:
    """(path, directory) for each newly tracked `path` that lies at or under a
    directory which exists on main's disk and is ignored there (people/, .state/).
    Files merely matching an ignore pattern (a stray *.log) are not this case."""
    if not added:
        return []
    candidates: dict[str, list[str]] = {}
    for rel in added:
        parts = Path(rel).parts
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            here = REPO_ROOT / prefix
            if here.is_dir() and not winlink.is_link(here):
                candidates.setdefault(prefix, []).append(rel)
    if not candidates:
        return []
    # Asked from main's checkout, so main's .gitignore and info/exclude decide.
    # check-ignore accepts -z only with --stdin; without it the call fails and
    # would report nothing ignored, so the directories go in on stdin.
    probe = run(["git", "-C", str(REPO_ROOT), "check-ignore", "--no-index", "--stdin", "-z"],
                input="\0".join(sorted(candidates)) + "\0").stdout
    ignored = [d for d in probe.split("\0") if d]
    seen: dict[str, str] = {}
    for directory in sorted(ignored, key=len):
        for rel in candidates[directory]:
            seen.setdefault(rel, directory)
    return sorted(seen.items())


def finish_branch(task: dict[str, Any], allow_mass_delete: bool = False, accept_main_edits: bool = False) -> dict[str, Any]:
    """Commit the worktree, rebase onto main, fast-forward main. Returns
    {landed: bool, commit, reason}. Never pushes. A conflict leaves the branch
    and worktree in place for a human or a follow-up task. Symlinks that point
    inside the repo are never committed here, and a branch whose own commits
    add one is refused (see symlink_points_inside_repo); so is a branch that
    deletes more than MASS_DELETE_LIMIT tracked files or tracks a path main
    ignores, unless allow_mass_delete is set. Uncommitted edits on main to a
    path the branch also changes, made after this task started, are refused as
    probable stray writes (see stray_main_writes) unless accept_main_edits is
    set, in which case they are committed on main with attribution as before.
    On landing, the worktree's own `.amp/in` scratch moves into main's."""
    branch, path = task.get("branch"), Path(task.get("worktree") or "")
    if not branch:
        if task.get("worker_id") or task.get("worktree"):
            # A launched worker always has a branch; one without it ran on a
            # checkout that cannot land, and its done report must not stick.
            return {"landed": False, "conflict": True, "reason": "worker row without a branch; nothing to land"}
        return {"landed": False, "reason": "no worktree for this task (edited main directly)", "skipped": True}
    if git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode:
        return {"landed": False, "reason": f"branch {branch} no longer exists", "gone": True}
    # `merge --ff-only` moves whatever REPO_ROOT has checked out. Landing means
    # main moved, so anything else there (the task branch itself when this runs
    # from a worktree copy, a detached HEAD, another branch) is a refusal, not a
    # fast-forward that reports landed=true with main untouched.
    head = git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if head != "main":
        return {"landed": False, "conflict": True, "wrong_checkout": head or "detached",
                "reason": f"{REPO_ROOT} has {head or 'a detached HEAD'} checked out, not main; "
                          f"check out main there, then run dispatch.py finish --task-id {task['id']}"}
    has_worktree = path.is_dir() and (path / ".git").exists()
    if has_worktree:
        for rel in ("system/state/todo.json",):
            if winlink.is_shared(path / rel, REPO_ROOT / rel):
                winlink.remove_link(path / rel)
                git("update-index", "--no-skip-worktree", "--", rel, cwd=path, check=False)
                git("checkout", "--", rel, cwd=path, check=False)
        git("add", "-A", "--", ".", ":(exclude)system/state", cwd=path, check=False)
        rejected = loop_symlinks(path, staged_paths(path))
        for entry in rejected:
            # Keep the link on disk (it is the worker's shared-state link) but
            # never in the commit: restore the index entry to HEAD for that path.
            git("reset", "-q", "--", entry["path"], cwd=path, check=False)
            log(f"finish {task['id']}: refusing to commit symlink {entry['path']} -> {entry['target']} (points inside the repo)")
        staged = git("diff", "--cached", "--quiet", cwd=path, check=False).returncode == 1
        if staged:
            subject = (task.get("prompt") or task["id"]).strip().splitlines()[0][:72]
            git("-c", "user.name=switchboard worker", "-c", "user.email=switchboard-worker@local", "commit", "-q",
                "-m", f"{task['id']}: {subject}", cwd=path)
    else:
        rejected = []
    # A branch made from main before `strip-unpushed` rewrote main's unpushed commits shares
    # only the pushed history with main. A plain `git rebase main` would replay every old main
    # commit whose patch changed, the private file included, and main...branch would list the
    # whole rewritten range. Move the task's own commits onto the new main first.
    fork = main_rewrites.fork_point(_git_nocheck, branch, main_rewrites.load(STATE_DIR))
    if fork:
        if not has_worktree:
            return {"landed": False, "conflict": True,
                    "reason": f"branch {branch} was made from main before its unpushed commits were rewritten "
                              f"(strip-unpushed) and its worktree {path} is missing; run `git worktree add {path} {branch}` "
                              f"then dispatch.py finish --task-id {task['id']}"}
        moved = git("rebase", "--onto", "main", fork, cwd=path, check=False)
        if moved.returncode:
            git("rebase", "--abort", cwd=path, check=False)
            return {"landed": False, "conflict": True, "rebase_conflict": True, "files": [],
                    "reason": f"rebase conflict moving {branch} off the rewritten main (old fork {fork[:10]}); resolve in "
                              f"{path} with `git rebase --onto main {fork[:10]}`, then run dispatch.py finish --task-id {task['id']}"}
        log(f"finish {task['id']}: moved {branch} from the rewritten main (old fork {fork[:10]}) onto main")
    ahead = git("rev-list", "--count", f"main..{branch}", check=False).stdout.strip() or "0"
    if ahead == "0":
        outcome = {"landed": True, "commit": git("rev-parse", "--short", "main").stdout.strip(), "changes": 0}
        if rejected:
            outcome["rejected_symlinks"] = rejected
        if has_worktree:
            outcome["scratch"] = migrate_scratch(path, task["id"])
        return outcome
    # A worker that committed on its own may have committed the loop itself.
    committed = [l for l in git("diff", "--name-only", "-z", "--diff-filter=AMT", f"main...{branch}", check=False).stdout.split("\0") if l]
    looped = loop_symlinks(path if has_worktree else REPO_ROOT, committed, tree=branch)
    if looped:
        shown = ", ".join(f"{e['path']} -> {e['target']}" for e in looped[:5])
        return {"landed": False, "conflict": True, "rejected_symlinks": looped,
                "reason": f"branch {branch} commits symlinks that point inside the repo ({shown}); checking them out on "
                          f"main would replace the real directories with links to themselves. "
                          f"Remove them from the branch (git rm --cached <path>; commit --amend) then run dispatch.py finish "
                          f"--task-id {task['id']}"}
    refusal = mass_change_refusal(branch, allow_mass_delete)
    if refusal:
        return {"landed": False, "conflict": True, "mass_change": True, "reason": refusal}
    leak = private_identifier_refusal(f"main..{branch}")
    if leak:
        # No override flag: this is the one refusal a worker must not be able to pass.
        return {"landed": False, "conflict": True, "private_identifier": True,
                "reason": (f"branch {branch} {leak}. Take the value out of the commits: in {path} run git reset --soft "
                           f"$(git merge-base main HEAD), replace it with an invented value, then run dispatch.py finish "
                           f"--task-id {task['id']}")}
    if not has_worktree:
        # Branch has commits but its checkout is gone: land it only if it is a
        # plain fast-forward; anything else needs a worktree to rebase in.
        if git("merge-base", "--is-ancestor", "main", branch, check=False).returncode:
            return {"landed": False, "conflict": True,
                    "reason": f"branch {branch} is behind main and its worktree {path} is missing; "
                              f"run `git worktree add {path} {branch}` then dispatch.py finish --task-id {task['id']}"}
        rebase = subprocess.CompletedProcess([], 0)
    else:
        rebase = git("rebase", "main", cwd=path, check=False)
    if rebase.returncode:
        git("rebase", "--abort", cwd=path, check=False)
        files = [l for l in git("diff", "--name-only", f"main...{branch}", check=False).stdout.split() if l]
        # rebase_conflict marks the three real rebase conflicts (here and the two
        # below): the only landing failures the grace window covers. A missing
        # worktree, a mass delete, stray writes or a fast-forward failure are not
        # the worker's merge to resolve and are reported at once as before.
        return {"landed": False, "conflict": True, "rebase_conflict": True, "files": files,
                "reason": f"rebase conflict on branch {branch} ({', '.join(files[:5])}{'…' if len(files) > 5 else ''}); "
                          f"resolve in {path} then run dispatch.py finish --task-id {task['id']}"}
    # --no-renames: with rename detection a moved file lists only its new path,
    # so a dirty edit on main to the OLD path (a watched-doc refresh writing into
    # a folder the branch moved to _archive/) would not be seen as overlap and
    # the fast-forward would fail with "would be overwritten by merge".
    changed = [l for l in git("diff", "--name-only", "--no-renames", f"main...{branch}").stdout.split() if l]
    # main is dirty with edits left by direct-to-main workers. Dirty files the
    # branch does not touch never block a landing. Dirty files it does touch are
    # committed on main with attribution (nothing is discarded or stashed), then
    # the branch is rebased once more so the worker's commit applies on top.
    # Unless they were written after this task started: then they are most
    # likely this worker's own stray writes into main and
    # attributing them to "direct-to-main workers" would bury the leak.
    dirty = dirty_main_paths()
    # A README on main that is exactly a fresh render of main, or the render switchboard
    # itself left uncommitted, is generated output, not anyone's edit: it is no
    # stray and no overlap. It goes back to HEAD only just before the fast-forward,
    # so an earlier refusal leaves main as it was, and main is rendered again after
    # (the hourly context snapshot once re-rendered main's README and a landing was
    # refused as a stray write).
    readme_reset = README_REL in changed and README_REL in dirty and (
        readme_is_fresh_render() or owner_tabs.is_own_write(REPO_ROOT, README_REL))
    if readme_reset:
        dirty = dirty - {README_REL}
    overlap = sorted(set(changed) & dirty)
    strays = stray_main_writes(task, held=set(changed), dirty=dirty)
    if strays and not accept_main_edits:
        return {"landed": False, "conflict": True, "stray_writes": strays, "overlap": overlap,
                "reason": stray_write_refusal(task, strays)}
    preserved = None
    if overlap:
        preserved = commit_main_overlap(task, overlap)
        if not preserved.get("commit"):
            return {"landed": False, "conflict": True, "overlap": overlap,
                    "reason": f"main has uncommitted edits to {', '.join(overlap[:5])} that could not be committed "
                              f"({preserved.get('error', '')[:160]}); branch {branch} is rebased and ready — commit those on "
                              f"main, then run dispatch.py finish --task-id {task['id']}"}
        if not has_worktree:
            return {"landed": False, "conflict": True, "preserved_main_commit": preserved["commit"],
                    "reason": f"main edits to {', '.join(overlap[:5])} committed as {preserved['commit']}; branch {branch} "
                              f"now needs a rebase but its worktree {path} is missing; run `git worktree add {path} {branch}` "
                              f"then dispatch.py finish --task-id {task['id']}"}
        rebase = git("rebase", "main", cwd=path, check=False)
        if rebase.returncode:
            git("rebase", "--abort", cwd=path, check=False)
            return {"landed": False, "conflict": True, "rebase_conflict": True, "files": list(overlap),
                    "preserved_main_commit": preserved["commit"],
                    "reason": f"rebase conflict on branch {branch} against main's uncommitted edits to "
                              f"{', '.join(overlap[:5])} (now committed as {preserved['commit']}); "
                              f"resolve in {path} then run dispatch.py finish --task-id {task['id']}"}
    if readme_reset:
        git("checkout", "--", README_REL, check=False)
        owner_tabs.forget_own_write(REPO_ROOT, [README_REL])
    ff = git("merge", "--ff-only", branch, check=False)
    # main can move between the rebase and the fast-forward (another task landing,
    # a reconciliation commit). That is not a conflict: rebase once more and retry,
    # bounded, before reporting a real failure.
    for _ in range(FF_RETRIES):
        if not ff.returncode or not has_worktree:
            break
        rebase = git("rebase", "main", cwd=path, check=False)
        if rebase.returncode:
            git("rebase", "--abort", cwd=path, check=False)
            if readme_reset:
                rerender_main_readme()   # README was reset for this landing; give main its render back
            return {"landed": False, "conflict": True, "rebase_conflict": True, "files": changed,
                    "reason": f"rebase conflict on branch {branch} against commits that landed on main meanwhile; "
                              f"resolve in {path} then run dispatch.py finish --task-id {task['id']}"}
        ff = git("merge", "--ff-only", branch, check=False)
    if ff.returncode:
        if readme_reset:
            rerender_main_readme()
        return {"landed": False, "conflict": True, "reason": f"fast-forward of main from {branch} failed: {ff.stderr.strip()[:200]}"}
    outcome = {"landed": True, "commit": git("rev-parse", "--short", "main").stdout.strip(), "changes": len(changed)}
    if rejected:
        outcome["rejected_symlinks"] = rejected
    if preserved:
        outcome["preserved_main_commit"] = preserved["commit"]
        outcome["preserved_paths"] = overlap
    if strays:
        outcome["accepted_main_edits"] = [s["path"] for s in strays]
    if preserved:
        owner_tabs.forget_own_write(REPO_ROOT, overlap)
    if readme_reset:
        outcome["readme_rerender"] = rerender_main_readme()
    if has_worktree:
        outcome["scratch"] = migrate_scratch(path, task["id"])
    return outcome


README_REL = "workstreams/README.md"


def _readme_renderer():
    spec = importlib.util.spec_from_file_location("render_workstreams_readme_for_finish",
                                                  SCRIPTS / "render-workstreams-readme.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readme_is_fresh_render() -> bool:
    """True when main's working-tree README is byte-for-byte (line endings aside)
    what the renderer makes from main right now: generated output that can be
    regenerated at will, so nothing is lost by resetting it."""
    try:
        current = (REPO_ROOT / README_REL).read_bytes().replace(b"\r\n", b"\n")
        fresh = (_readme_renderer().build_readme(REPO_ROOT) + "\n").encode("utf-8")
    except Exception:  # noqa: BLE001 - a failed probe means "not provably generated"
        return False
    return current == fresh


def rerender_main_readme() -> dict[str, Any]:
    """After a landing that reset main's generated README: render main again and
    commit README.md alone when it differs from HEAD."""
    try:
        renderer = _readme_renderer()
        renderer.write_readme(REPO_ROOT, renderer.build_readme(REPO_ROOT))
        return renderer.commit_readme(REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - the landing already succeeded
        return {"committed": False, "reason": f"re-render failed: {exc}"[:200]}


def porcelain_paths(lines: list[str]) -> set[str]:
    """Paths named by `git status --porcelain` lines (the new name of a rename)."""
    paths: set[str] = set()
    for line in lines:
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.add(entry.strip('"'))
    return paths


def dirty_main_paths() -> set[str]:
    """Tracked modifications/deletions and untracked files in main's working tree
    (an untracked file the branch also adds would make the fast-forward fail)."""
    return porcelain_paths(git("status", "--porcelain", "--untracked-files=all", NO_SUBMODULES).stdout.splitlines())


def task_held_paths(task: dict[str, Any]) -> set[str]:
    """Paths the task's branch commits beyond main plus the uncommitted edits in
    its worktree: what the task "holds" while it is live."""
    held: set[str] = set()
    branch = task.get("branch")
    if branch and not git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode:
        # A branch made before strip-unpushed rewrote main shares only the pushed history with it,
        # so main...branch would list every rewritten commit's paths and flag other people's edits
        # on main as this task's stray writes. Its own
        # commits are <fork>..<branch>, the range finish moves onto main.
        fork = main_rewrites.fork_point(_git_nocheck, str(branch), main_rewrites.load(STATE_DIR))
        span = f"{fork}..{branch}" if fork else f"main...{branch}"
        held |= {l for l in git("diff", "--name-only", "--no-renames", span, check=False).stdout.split("\n") if l}
    path = Path(task.get("worktree") or "")
    if path.is_dir():
        held |= porcelain_paths(worktree_dirty(path))
    return held


def stray_main_writes(task: dict[str, Any], held: set[str] | None = None, dirty: set[str] | None = None) -> list[dict[str, Any]]:
    """Files dirty in main's working tree that the task also holds and that were
    written after the task started: the shape a scratch script leaves when it
    derives the repo root from its own path under a shared `.amp/in` and writes
    its outputs into MAIN instead of the worktree. Each entry: {path, modified_at,
    identical_to_worktree?}. A row with no start time cannot be judged (none
    reported); a deletion on main carries no mtime and is not reported. Edits
    older than the task (a scheduled job's daily note, another worker's
    direct-to-main edit) are the ordinary overlap finish attribute-commits."""
    since = ledger.parse_iso(task.get("started_at") or task.get("created_at"))
    if since is None:
        return []
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    held = task_held_paths(task) if held is None else held
    if not held:
        return []
    dirty = dirty_main_paths() if dirty is None else dirty
    worktree = Path(task.get("worktree") or "")
    found: list[dict[str, Any]] = []
    for rel in sorted(held & dirty):
        target = REPO_ROOT / rel
        if not target.is_file():
            continue
        if owner_tabs.is_own_write(REPO_ROOT, rel):
            # switchboard's own uncommitted close-out or card (owner_tabs.note_own_write):
            # the ordinary overlap finish commits with attribution, never a stray.
            continue
        modified = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
        if modified < since:
            continue
        entry: dict[str, Any] = {"path": rel, "modified_at": modified.isoformat(timespec="seconds")}
        mine = worktree / rel
        if worktree.is_dir() and mine.is_file() and filecmp.cmp(mine, target, shallow=False):
            entry["identical_to_worktree"] = True
        found.append(entry)
    return found


def stray_write_refusal(task: dict[str, Any], strays: list[dict[str, Any]]) -> str:
    shown = ", ".join(f"{s['path']} ({s['modified_at']}{', identical to your worktree copy' if s.get('identical_to_worktree') else ''})"
                      for s in strays[:5]) + ("…" if len(strays) > 5 else "")
    paths = " ".join(shlex.quote(s["path"]) for s in strays[:5])
    return (f"main's working tree at {REPO_ROOT} has uncommitted edits to {len(strays)} path(s) your branch also changes, "
            f"written after this task started: {shown}. If a script of yours wrote them (one that derived the repo root from its "
            f"own path, or used an absolute {REPO_ROOT} path), restore main with `git -C {REPO_ROOT} checkout -- {paths}` "
            f"(untracked: rm) and report again; if the edits are someone else's and legitimate, run "
            f"`python3 {SCRIPTS}/dispatch.py finish --task-id {task['id']} --accept-main-edits` (commits them on main with "
            f"attribution) and then report again. Nothing on main was changed.")


def head_kind(rel: str) -> str:
    """'blob' | 'tree' | 'commit' | '' for what HEAD tracks at rel (a symlink is a blob)."""
    result = git("cat-file", "-t", f"HEAD:{rel}", check=False)
    return result.stdout.strip() if not result.returncode else ""


def stage_main_paths(paths: list[str]) -> subprocess.CompletedProcess:
    """`git add -A -- paths`, except where HEAD tracks a symlink (or file) that
    the working tree has since replaced with a real directory: `git add` refuses
    that path ("does not have a commit checked out" when the directory holds a
    .git, "unable to index file" otherwise), for example when main tracks `.state`
    as a loop symlink while the tree holds a real directory. Such a path is un-tracked from the
    index first; its non-ignored contents are then added like any other new
    files, unless the directory is itself a repository (a .git inside), which
    git can only ever record as a gitlink and never as part of this commit."""
    plain: list[str] = []
    for rel in paths:
        target = REPO_ROOT / rel
        if head_kind(rel) == "blob" and target.is_dir() and not winlink.is_link(target):
            git("rm", "-q", "--cached", "--", rel, check=False)
            if not (target / ".git").exists():
                git("add", "-A", "--", rel, check=False)
            continue
        plain.append(rel)
    return git("add", "-A", "--", *plain, check=False) if plain else subprocess.CompletedProcess([], 0, "", "")


def staged_outside(paths: list[str]) -> list[str]:
    """Index entries already staged on main that none of `paths` covers."""
    covered = tuple(p.rstrip("/") for p in paths)
    staged = [l for l in git("diff", "--cached", "--name-only", check=False).stdout.split("\n") if l]
    return [s for s in staged if not any(s == c or s.startswith(c + "/") for c in covered)]


def commit_main_overlap(task: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Commit the given dirty paths on main, attributed to the direct-to-main
    workers that left them, so a landing never discards or stashes their edits.
    The commit is cut from the index (not `--only <paths>`, which re-indexes the
    paths from the working tree and fails on the symlink-to-directory case), so
    anything a human already staged on main must be absent: it is reported, not
    swept into an attributed commit."""
    foreign = staged_outside(paths)
    if foreign:
        return {"error": f"main's index already holds staged changes to {', '.join(foreign[:5])}; commit or unstage them first"}
    add = stage_main_paths(paths)
    if add.returncode:
        git("reset", "-q", "--", *paths, check=False)
        return {"error": add.stderr.strip() or "git add failed"}
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return {"error": "nothing to commit for the overlapping paths"}
    message = (f"main: uncommitted edits to {', '.join(paths[:8])}{'…' if len(paths) > 8 else ''} left by direct-to-main "
               f"workers; committed by dispatch.py finish before landing {task['id']}")
    commit = git("-c", "user.name=switchboard worker", "-c", "user.email=switchboard-worker@local", "commit", "-q",
                 "-m", message, check=False)
    if commit.returncode:
        git("reset", "-q", "--", *paths, check=False)
        return {"error": (commit.stderr or commit.stdout).strip() or "git commit failed"}
    return {"commit": git("rev-parse", "--short", "HEAD").stdout.strip(), "paths": paths}


# --- worker close-out record -------------------------------------------------------------

CLOSEOUT_HEADING = "## Worker close-outs"


def closeout_line(task: dict[str, Any], status: str, result: str, thread_id: str) -> str:
    """One durable line per finished worker in the owning workstream's CONTEXT.md:
    date, task id, tab title, status and the one-line result, then the thread link
    for an Amp row or the resume command for a Claude row (switchboard_agent.thread_pointer)."""
    title = task.get("tab_title") or task.get("worker_id") or "worker"
    result = switchboard_agent.fix_thread_links(re.sub(r"\s+", " ", result).strip())
    if result.lower().startswith(f"{status}:"):
        result = result[len(status) + 1:].strip()
    pointer = switchboard_agent.thread_pointer(thread_id, task.get("agent"))
    link = f" · {pointer}" if pointer and pointer not in result else ""
    return f"- {now().date().isoformat()} · `{task['id']}` · {title} · {status}: {result}{link}"


def record_closeout(task: dict[str, Any], status: str, result: str, thread_id: str, root: Path) -> dict[str, Any]:
    """Append the close-out line under CLOSEOUT_HEADING at the end of
    root/workstreams/<slug>/CONTEXT.md. Idempotent per task id: a file that
    already names the task under that heading is left alone. Returns what was
    done; never raises for a missing workstream directory."""
    slug = str(task.get("workstream") or "")
    path = root / "workstreams" / slug / "CONTEXT.md"
    if not slug or not path.parent.is_dir():
        return {"recorded": False, "reason": f"no workstream directory for {slug or '(none)'}"}
    text = path.read_text(encoding="utf-8") if path.is_file() else f"# {owner_title(slug)}\n"
    head, sep, tail = text.partition(f"\n{CLOSEOUT_HEADING}\n")
    if sep and f"`{task['id']}`" in tail:
        return {"recorded": False, "path": str(path), "reason": "already recorded"}
    if private_workstreams.is_private_slug(slug, root):
        result = private_workstreams.WITHHELD_REPORT
    line = closeout_line(task, status, result, thread_id)
    if sep:
        text = head + sep + tail.rstrip("\n") + "\n" + line + "\n"
    else:
        text = text.rstrip("\n") + f"\n\n{CLOSEOUT_HEADING}\n{line}\n"
    path.write_text(text, encoding="utf-8", newline="")  # LF: owner_tabs._dirty
    return {"recorded": True, "path": str(path), "line": line}


def record_on_main(task: dict[str, Any], status: str = "", result: str = "", thread_id: str = "") -> dict[str, Any]:
    """Guarantee the close-out line in main's CONTEXT.md for a terminal row
    (done, blocked, failed, deferred; a cancelled row was ended by its origin
    and records nothing). Written on main after the landing, never in the
    branch, so two workers of one workstream finishing together do not conflict
    on the same appended line. Committed on main by itself when that file was
    clean beforehand; a file someone is mid-edit on gets the line appended and
    left uncommitted (the next landing that touches it commits it with
    attribution). Idempotent: a task already recorded is left alone."""
    status = status or str(task.get("status") or "")
    if status == "cancelled" or status not in ledger.TERMINAL_STATUSES:
        return {"recorded": False, "reason": f"status {status or '(none)'} is not recorded"}
    if owner_tabs.sandbox_reaching_main(REPO_ROOT):
        return {"recorded": False, "reason": "sandboxed run; main is not written"}
    rel = f"workstreams/{task.get('workstream')}/CONTEXT.md"
    # Dirty only with switchboard's own earlier uncommitted close-out or card counts as
    # clean, so this commit carries those lines too (owner_tabs.is_own_write).
    # A file that differs from HEAD only in line endings is clean too (owner_tabs._dirty).
    was_dirty = owner_tabs.dirty_by_others(REPO_ROOT, rel)
    outcome = record_closeout(task, status, result or str(task.get("summary") or ""),
                              thread_id or str(task.get("thread_id") or ""), REPO_ROOT)
    if not outcome.get("recorded"):
        return outcome
    if was_dirty:
        outcome.update(committed=False, reason=f"{rel} had uncommitted edits on main; line appended, left for the next landing to commit")
        return outcome
    # A CONTEXT.md this call created (a renamed or brand-new workstream) is untracked;
    # the helper stages it by explicit path before the path-limited commit.
    outcome.update(owner_tabs.commit_written_paths(REPO_ROOT, [rel], f"{task['id']}: worker close-out recorded in {rel}"))
    return outcome


def command_record(args: argparse.Namespace) -> int:
    task = ledger.find_task(ledger.load_payload(), args.task_id)
    if task is None:
        print(json.dumps({"recorded": False, "reason": f"no task {args.task_id}"}))
        return 2
    print(json.dumps({"task_id": args.task_id, **record_on_main(task, args.status or "", args.result or "", args.thread_id or "")}))
    return 0


DECISION_HEADING = "## the owner decisions"


def record_decision(task: dict[str, Any], text: str, root: Path, by: str = "owner") -> dict[str, Any]:
    """Append ``- <date> · `<task>` · <by>: <text>`` under DECISION_HEADING at the end of
    root/workstreams/<slug>/CONTEXT.md. Idempotent per (task, text). Never raises for a
    missing workstream directory."""
    slug = str(task.get("workstream") or "")
    path = root / "workstreams" / slug / "CONTEXT.md"
    if not slug or not path.parent.is_dir():
        return {"recorded": False, "reason": f"no workstream directory for {slug or '(none)'}"}
    text = re.sub(r"\s+", " ", text).strip()
    if private_workstreams.is_private_slug(slug, root):
        text = private_workstreams.WITHHELD_DECISION
    line = f"- {now().date().isoformat()} · `{task['id']}` · {by}: {text}"
    body = path.read_text(encoding="utf-8") if path.is_file() else f"# {owner_title(slug)}\n"
    head, sep, tail = body.partition(f"\n{DECISION_HEADING}\n")
    if sep and f"`{task['id']}` · {by}: {text}" in tail:
        return {"recorded": False, "path": str(path), "reason": "already recorded", "line": line}
    if sep:
        body = head + sep + tail.rstrip("\n") + "\n" + line + "\n"
    else:
        body = body.rstrip("\n") + f"\n\n{DECISION_HEADING}\n{line}\n"
    path.write_text(body, encoding="utf-8", newline="")  # LF: owner_tabs._dirty
    return {"recorded": True, "path": str(path), "line": line}


def command_decide(args: argparse.Namespace) -> int:
    """CoS Review decision reply: write the owner's go / veto / keep /
    "option N" onto the ledger row (`running-task-state.py annotate`) and into the
    owning workstream's CONTEXT.md on main, and dispatch nothing. The item itself
    is completed by cos_review_reply.py through cos_review_actions.py."""
    text = " ".join(args.text).strip() if isinstance(args.text, list) else str(args.text or "").strip()
    if not text:
        print(json.dumps({"status": "rejected", "reason": "empty decision"}))
        return 2
    task = ledger.find_task(ledger.load_payload(), args.task_id)
    if task is None:
        print(json.dumps({"status": "failed", "task_id": args.task_id, "reason": "no ledger row with that id"}))
        return 1
    ok, ledger_out, context = record_ruling(task, text, args.by)
    print(json.dumps({"status": "decided" if ok else "failed", "task_id": args.task_id, "text": text,
                      "ledger": ledger_out, "context": context}))
    return 0 if ok else 1


def record_ruling(task: dict[str, Any], text: str, by: str) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """Write a ruling onto the ledger row (`running-task-state.py annotate`) and under
    DECISION_HEADING in the owning workstream's CONTEXT.md on main, committed there when
    that file was clean. Shared by `decide` and record-only steers. Returns
    (ok, ledger summary, context outcome)."""
    proc = subprocess.run([sys.executable, str(SCRIPTS / "running-task-state.py"), "annotate",
                           "--task-id", str(task["id"]), "--text", text, "--by", by],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        ledger_out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        ledger_out = {"error": (proc.stderr or proc.stdout).strip()[:300]}
    rel = f"workstreams/{task.get('workstream')}/CONTEXT.md"
    if owner_tabs.sandbox_reaching_main(REPO_ROOT):
        context: dict[str, Any] = {"recorded": False, "reason": "sandboxed run; main is not written"}
    else:
        was_dirty = owner_tabs.dirty_by_others(REPO_ROOT, rel)
        context = record_decision(task, text, REPO_ROOT, by)
        if context.get("recorded") and not was_dirty:
            # The shared helper: it stages a new file, and a failed commit is remembered as
            # switchboard's own write, so the next close-out still commits it.
            commit = owner_tabs.commit_written_paths(REPO_ROOT, [rel], f"{task['id']}: the owner's decision recorded in {rel}")
            context["committed"] = bool(commit.get("committed"))
            if not commit.get("committed"):
                context["reason"] = commit.get("reason", "")
        elif context.get("recorded"):
            context.update(committed=False, reason=f"{rel} had uncommitted edits on main; line appended, left for the next landing")
    ok = proc.returncode == 0 and "error" not in ledger_out
    return ok, {k: v for k, v in ledger_out.items() if k in {"id", "status", "decisions", "_duplicate_decision", "error"}}, context


# --- path locks (non-repo write targets) ---------------------------------------------

def lock_key(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def norm_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def related(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def current_leases() -> list[dict[str, Any]]:
    leases = []
    for file in LOCKS.glob("*.lease.json"):
        try:
            leases.append(json.loads(file.read_text()))
        except (ValueError, OSError):
            continue
    return leases


def lease_is_live(lease: dict[str, Any], payload: dict[str, Any]) -> bool:
    task = ledger.find_task(payload, lease.get("task_id") or "")
    return bool(task) and task.get("status") not in ledger.TERMINAL_STATUSES


def release_locks(task_id: str, path: str | None = None) -> list[str]:
    released = []
    for file in list(LOCKS.glob("*.lease.json")) + list(LOCKS.glob("*.queue.jsonl")):
        if file.name.endswith(".lease.json"):
            try:
                lease = json.loads(file.read_text())
            except (ValueError, OSError):
                continue
            if lease.get("task_id") == task_id and (path is None or lease.get("path") == path):
                if _unlink_retry(file):  # a lease still on disk is not released; it goes stale with its task
                    released.append(lease["path"])
        else:
            try:
                rows = [json.loads(l) for l in file.read_text().splitlines() if l.strip()]
            except (ValueError, OSError):
                continue
            keep = [r for r in rows if r.get("task_id") != task_id]
            if len(keep) != len(rows):
                file.write_text("".join(json.dumps(r) + "\n" for r in keep) if keep else "")
                if not keep:
                    _unlink_retry(file)
    return released


def command_lock(args: argparse.Namespace) -> int:
    path = norm_path(args.path)
    LOCKS.mkdir(parents=True, exist_ok=True)
    key = lock_key(path)
    queue_file = LOCKS / f"{key}.queue.jsonl"
    lease_file = LOCKS / f"{key}.lease.json"
    with queue_file.open("a") as handle:
        handle.write(json.dumps({"task_id": args.task_id, "path": path, "at": ledger.now_iso()}) + "\n")
    deadline = time.monotonic() + args.wait
    told = ""
    while True:
        with (LOCKS / "locks.lock").open("w") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            payload = ledger.load_payload()
            blockers = [l for l in current_leases() if l.get("task_id") != args.task_id and related(l["path"], path)
                        and lease_is_live(l, payload)]
            for stale in (l for l in current_leases() if not lease_is_live(l, payload)):
                _unlink_retry(LOCKS / f"{lock_key(stale['path'])}.lease.json")
            rows = [json.loads(l) for l in queue_file.read_text().splitlines() if l.strip()] if queue_file.exists() else []
            rows = [r for r in rows if lease_is_live({"task_id": r["task_id"]}, payload) or r["task_id"] == args.task_id]
            head = rows[0]["task_id"] if rows else args.task_id
            if not blockers and head == args.task_id:
                lease_file.write_text(json.dumps({"task_id": args.task_id, "path": path, "acquired_at": ledger.now_iso()}))
                rest = [r for r in rows if r["task_id"] != args.task_id]
                try:
                    if rest:
                        queue_file.write_text("".join(json.dumps(r) + "\n" for r in rest))
                    elif not _unlink_retry(queue_file):
                        queue_file.write_text("")  # still held open: empty it so no stale row heads the queue
                except OSError:
                    pass  # the lease above grants the path; a leftover row ages out with its task
                print(json.dumps({"allowed": True, "task_id": args.task_id, "path": path}))
                return 0
            waiting_on = blockers[0]["task_id"] if blockers else head
        if waiting_on != told:
            told = waiting_on
            notify(args.task_id, f"waiting on {task_name(ledger.find_task(payload, waiting_on), waiting_on)} for {path}")
        if time.monotonic() >= deadline:
            print(json.dumps({"allowed": False, "task_id": args.task_id, "path": path, "waiting_on": waiting_on}))
            return 3
        time.sleep(args.poll)


def command_unlock(args: argparse.Namespace) -> int:
    released = release_locks(args.task_id, norm_path(args.path) if args.path else None)
    print(json.dumps({"released": released}))
    return 0


# --- origin messages -------------------------------------------------------------------

def notify(task_id: str, text: str, status: str = "active", urgent: bool = False) -> dict[str, Any]:
    """One plain line to the origin surface via report-to-cos.sh (no hooks)."""
    task = ledger.find_task(ledger.load_payload(), task_id) or {}
    # Flags first, the message last: an optional flag spliced in between --task-id
    # and its value fails with `--task-id: expected one argument`.
    cmd = ["bash", str(SCRIPTS / "report-to-cos.sh"), "--task-id", task_id, "--origin", task.get("origin") or "cos",
           "--status", status]
    if task.get("attempt_id"):
        cmd += ["--attempt-id", task["attempt_id"]]
    if task.get("workstream"):
        cmd += ["--workstream", task["workstream"]]
    if urgent:
        cmd.append("--urgent")
    cmd.append(text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    result = run(cmd, env=env, timeout=180)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"delivered": False, "error": (result.stderr or result.stdout).strip()[:300]}


# --- dispatch --------------------------------------------------------------------------

def live_workers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows that hold a worker slot: routing/active with an Amp session still
    present in cmux, or so new that the session has not registered yet."""
    agents = ledger.live_agents()
    fresh = now() - timedelta(minutes=10)
    out = []
    for t in payload["tasks"]:
        if t.get("status") not in {"routing", "active"}:
            continue
        spawned = bool(t.get("worker_id") or t.get("worktree"))
        if not spawned and (ledger.parse_iso(t.get("created_at")) or fresh) < fresh:
            continue  # owner-tab or native rows do not hold worker slots
        if spawned and (t.get("thread_id") or "") not in agents \
                and (ledger.parse_iso(t.get("heartbeat_at") or t.get("updated_at") or t.get("created_at")) or fresh) < fresh:
            continue  # worker gone without a report; the sweep fails it
        out.append(t)
    return out


def _on_windows() -> bool:
    """True on a live Windows machine. The test sandbox reads as not Windows so
    the suite never queues on the machine's real memory or Windows cap; the
    Windows capacity tests patch this."""
    return os.name == "nt" and os.environ.get("SWITCHBOARD_TEST_SANDBOX") != "1"


def worker_cap(cfg: dict[str, Any]) -> int | None:
    """The concurrent-worker cap from `dispatch.max_workers`, or None when there
    is none. 0, absent, or unreadable means unlimited;
    only a positive integer queues tasks behind live workers."""
    try:
        cap = int(cfg.get("max_workers") or 0)
        # Windows: a laptop's RAM fills fast at about 0.7 GB per
        # Claude worker plus the owner's own sessions, and every process a worker
        # forks costs kernel memory there. max_workers_windows
        # wins on Windows when it is positive.
        if _on_windows() and int(cfg.get("max_workers_windows") or 0) > 0:
            cap = int(cfg["max_workers_windows"])
    except (TypeError, ValueError):
        cap = 0
    return cap if cap > 0 else None


def live_memory(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """One live Windows reading for the hold: windows_memory.snapshot(), plus
    spawn_ms only when commit is past commit_hold_pct of the current limit but
    under it of the ceiling and the stall check is on (the only case that reads
    it). None off Windows or on a probe failure. Callers that decide several
    rows take one reading per pass (release_queued, capacity_snapshot)."""
    if not _on_windows():
        return None
    try:
        import windows_memory
        memory = dict(windows_memory.snapshot())
        hold = float(cfg.get("commit_hold_pct") or 0)
        spawn_limit = float(cfg.get("spawn_hold_ms") or 0)
        ceiling = memory.get("commit_ceiling_pct")
        if hold > 0 and spawn_limit > 0 and ceiling is not None and (memory.get("commit_pct") or 0) >= hold > ceiling:
            memory["spawn_ms"] = windows_memory.spawn_ms()
        return memory
    except Exception:  # noqa: BLE001 - a broken probe must never break dispatch
        return None


def commit_hold_reason(cfg: dict[str, Any], memory: dict[str, Any] | None = None,
                       live: int | None = None) -> str:
    """Why a new launch must wait for memory, or "" (Windows only; any probe
    failure reads as no hold). Queued tasks start from release_queued() once it clears.

    A real shortage shows as very high commit with slow process starts (hundreds of
    ms each). A high percent of the current limit alone is not one: with a
    system-managed pagefile the limit grows on demand, so processes can still start
    in a few ms. So the hold needs commit at commit_hold_pct of the ceiling (RAM plus the
    most the pagefile can grow), or, when the limit can still grow, a measured
    stall: new processes taking dispatch.spawn_hold_ms or more. A memory without
    ceiling fields (fixed pagefile, unreadable registry) keeps the old gauge.

    dispatch.memory_hold_floor_workers: while fewer workers than this are live
    (`live`), nothing is held, so one worker can always start."""
    try:
        hold = float(cfg.get("commit_hold_pct") or 0)
        floor = int(cfg.get("memory_hold_floor_workers") or 0)
        spawn_limit = float(cfg.get("spawn_hold_ms") or 0)
    except (TypeError, ValueError):
        return ""
    if hold <= 0 or (memory is None and not _on_windows()):
        return ""
    if live is not None and live < floor:
        return ""
    if memory is None:
        memory = live_memory(cfg)
        if memory is None:
            return ""
    pct = memory.get("commit_pct")
    if pct is None or pct < hold:
        return ""
    ceiling = memory.get("commit_ceiling_pct")
    if ceiling is None:
        return (f"memory commit is at {pct:.0f}% of the limit (hold at {hold:g}%); "
                f"closing apps or a reboot frees it")
    if ceiling >= hold:
        return (f"memory commit is at {ceiling:.0f}% of the most it can grow to "
                f"({memory.get('commit_ceiling_gb') or 0:.0f} GB, hold at {hold:g}%); closing apps or a reboot frees it")
    if spawn_limit <= 0:
        return ""
    spawn = memory.get("spawn_ms")
    if spawn is None or spawn < spawn_limit:
        return ""
    if spawn == float("inf"):
        return (f"new processes fail to start with commit at {pct:.0f}% of the current limit; "
                f"closing apps or a reboot frees it")
    return (f"new processes take {spawn:.0f} ms to start (hold at {spawn_limit:g} ms) with commit at {pct:.0f}% "
            f"of the current limit; closing apps or a reboot frees it")


def at_capacity(live: int, cfg: dict[str, Any], memory: dict[str, Any] | None = None) -> bool:
    cap = worker_cap(cfg)
    return (cap is not None and live >= cap) or bool(commit_hold_reason(cfg, memory, live=live))


NOT_STARTED_AFTER = timedelta(seconds=int(os.environ.get("SWITCHBOARD_NOT_STARTED_SECONDS") or 600))


def not_started_evidence(task: dict[str, Any]) -> dict[str, Any] | None:
    """Why a routing/active row has no worker behind it, or None when it does.

    The old idle-route verifier treated the owner tab as a permanent candidate
    and never concluded "not started". Here presence of a *tab* is not
    evidence of a *worker*: a row is live only when its bound thread is an
    Amp agent in cmux or it has a fresh stamp (heartbeat/started/updated/
    created). Everything else is a stall this dispatch may replace."""
    agents = ledger.live_agents()
    thread_id = str(task.get("thread_id") or "")
    if thread_id and thread_id in agents:
        return None
    last = ledger.parse_iso(task.get("heartbeat_at") or task.get("started_at") or task.get("updated_at") or task.get("created_at"))
    idle = (now() - last).total_seconds() if last else None
    if idle is not None and idle < NOT_STARTED_AFTER.total_seconds():
        return None
    ref = str(task.get("workspace_ref") or "")
    ws = workspace_by_ref(ref) if ref else None
    worktree = Path(task["worktree"]) if task.get("worktree") else None
    dirty = bool(worktree and worktree.is_dir()
                 and (git("status", "--porcelain", NO_SUBMODULES, cwd=worktree, check=False).stdout.strip()
                      or git("log", "--oneline", "main..HEAD", cwd=worktree, check=False).stdout.strip()))
    return {
        "verdict": "abandoned-with-work" if dirty else "not-started",
        "attempt_id": task.get("attempt_id"),
        "thread_id": thread_id or None,
        "thread_live": False,
        "last_seen": last.isoformat(timespec="seconds") if last else None,
        "idle_seconds": round(idle) if idle is not None else None,
        "workspace_ref": ref or None,
        "workspace_present": ws is not None,
        "worker_tab": is_worker_tab(ws, task),
        "stale_since": task.get("stale_since"),
    }


def continuation_heir(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """The row that owns `task`'s work now, or None. A continuation takes the work over,
    so the row it replaced must never get another worker: a relaunch of it while its
    continuation is live would act on external systems a second time.

    Walks the whole chain, because a worker that dies is continued again. Ranked live, then done, then blocked, newest first.
    A failed or cancelled continuation owns nothing, and neither does a supplemental worker,
    which only adds to a running parent. Neither does a continuation older than the row's
    current attempt: someone re-dispatched this row on purpose after it was made."""
    children: dict[str, list[dict[str, Any]]] = {}
    for row in payload.get("tasks", []):
        if row.get("continuation_of") and not row.get("supplemental"):
            children.setdefault(str(row["continuation_of"]), []).append(row)
    began = ledger.parse_iso(task.get("attempt_began_at"))
    stack = [c for c in children.get(str(task.get("id")), [])
             if began is None or (ledger.parse_iso(c.get("created_at")) or began) >= began]
    seen, owners = {str(task.get("id"))}, []
    while stack:
        row = stack.pop()
        if str(row.get("id")) in seen:
            continue
        seen.add(str(row.get("id")))
        stack.extend(children.get(str(row.get("id")), []))
        status = row.get("status")
        rank = (0 if status not in ledger.TERMINAL_STATUSES else
                {"done": 1, "blocked": 2, "deferred": 2}.get(str(status)))
        if rank is not None:
            owners.append((rank, str(row.get("created_at") or ""), row))
    if not owners:
        return None
    best = min(rank for rank, _, _ in owners)
    return max((o for o in owners if o[0] == best), key=lambda o: o[1])[2]


def continuation_ancestors(task: dict[str, Any], payload: dict[str, Any]) -> set[str]:
    """Every row this task continues, directly or through a chain."""
    seen: set[str] = set()
    parent = task.get("continuation_of")
    while parent and str(parent) not in seen:
        seen.add(str(parent))
        parent = (ledger.find_task(payload, str(parent)) or {}).get("continuation_of")
    return seen


def retire_superseded(task: dict[str, Any], heir: dict[str, Any]) -> dict[str, Any] | None:
    """End a row whose continuation owns its work. It gets no worker and no report, and its
    origin already knows, because that origin dispatched the continuation. Its checkout stays:
    settle_checkout parks work and never deletes it. A row with a live thread is left alone,
    because only the worker running there can say what it is doing."""
    if task.get("thread_id") and str(task["thread_id"]) in ledger.live_agents():
        return None
    summary = (f"Superseded by the continuation {heir['id']} ({heir.get('status')}), which owns this work. "
               f"No worker was relaunched for attempt {task.get('attempt_id')}.")

    def mutate(payload, tid=task["id"], attempt_id=task.get("attempt_id")):
        row = ledger.find_task(payload, tid)
        if row is None or row.get("status") in ledger.TERMINAL_STATUSES or row.get("attempt_id") != attempt_id:
            return None
        stamp = ledger.now_iso()
        row.update(status="cancelled", summary=summary, superseded_by=heir["id"], finished_at=stamp, updated_at=stamp,
                   report_delivered=True, report_delivered_at=stamp, note="settled without send: a continuation owns this work")
        row.pop("launching_at", None)
        return dict(row)

    retired = ledger.locked_mutation(mutate)
    if retired:
        log(f"{task['id']}: {summary}")
    return retired


def effective_dependency(dep_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The row a task really waits on for prerequisite `dep_id`: the prerequisite, or the
    continuation that replaced it. A finished prerequisite stays itself, because a
    continuation of a done row is a follow-up and not a replacement."""
    dep = ledger.find_task(payload, dep_id)
    if dep is None or dep.get("status") == "done":
        return dep
    return continuation_heir(dep, payload) or dep


def unmet_dependency(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    for dep_id in task.get("depends_on") or []:
        dep = effective_dependency(str(dep_id), payload)
        if dep and dep.get("status") not in ledger.TERMINAL_STATUSES:
            return dep
    return None


def failed_dependency(task: dict[str, Any], payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The prerequisite this task can no longer wait on, with the row that ended it."""
    for dep_id in task.get("depends_on") or []:
        dep = effective_dependency(str(dep_id), payload)
        if dep and dep.get("status") in {"blocked", "failed", "cancelled"}:
            return str(dep_id), dep
    return None


# --- queue order ---------------------------------------------------------------------------
#
# When a worker frees a slot, a brand-new dispatch must not jump ahead of rows that have
# been queued on capacity: the release after a report runs 150 s later. So the
# dispatch path asks queued_ahead() first and release_queued() walks rows in queue_rank()
# order.

MAINTENANCE_WORKSTREAM = "switchboard-maintenance"


def queue_rank(task: dict[str, Any], cfg: dict[str, Any], at: datetime | None = None) -> tuple[int, float]:
    """Sort key for who gets a free slot: (class, created). Class 0 is the owner's own asks
    (every workstream but switchboard-maintenance) and class 1 is switchboard fixing itself, which CoS
    mostly starts on its own ("fix without asking"). The owner waits on an ask; a fix
    usually has nobody waiting on it. A maintenance row moves to class 0 once it has waited
    queue_maintenance_aging_minutes, so it is never starved. A row with no created_at sorts
    last in its class."""
    born = ledger.parse_iso(task.get("created_at"))
    created = born.timestamp() if born else float("inf")
    if cfg.get("queue_owner_first", DEFAULTS["queue_owner_first"]) is False \
            or str(task.get("workstream") or "") != MAINTENANCE_WORKSTREAM:
        return (0, created)
    try:
        aging = int(cfg.get("queue_maintenance_aging_minutes", DEFAULTS["queue_maintenance_aging_minutes"]))
    except (TypeError, ValueError):
        aging = int(DEFAULTS["queue_maintenance_aging_minutes"])
    if aging > 0 and born is not None and (at or now()) - born >= timedelta(minutes=aging):
        return (0, created)
    return (1, created)


def launch_hold(task: dict[str, Any], cfg: dict[str, Any], at: datetime | None = None) -> dict[str, Any] | None:
    """The dated hold that keeps `task` from launching now, or None. A hold matches by the
    row's workstream (`workstreams:`) or id (`tasks:`) and lasts until its `until`, an
    ISO time with a zone. Only a row's first attempt is held: a worker that already started
    and is relaunched after a dead tab finishes its work ("workers already running finish").
    Self-expiring by construction: past `until` the entry matches nothing, and an entry
    whose `until` is further ahead than its cap is ignored and logged. The cap is
    launch_hold_max_hours (24 h), or the entry's own `max_hours`, or the ceiling with
    `allow_long: true`; never more than LAUNCH_HOLD_CEILING_HOURS (hold_cap())."""
    holds = cfg.get("launch_holds") or []
    if not isinstance(holds, list) or not holds or int(task.get("attempt") or 1) > 1:
        return None
    at = at or now()
    slug, tid = str(task.get("workstream") or ""), str(task.get("id") or "")
    found: dict[str, Any] | None = None
    for hold in holds:
        if not isinstance(hold, dict):
            continue
        raw = hold.get("until")
        until = raw if isinstance(raw, datetime) else ledger.parse_iso(str(raw or ""))
        if until is None or until.tzinfo is None or at >= until:
            continue
        cap, source = hold_cap(hold, cfg)
        if until - at > cap:
            log(f"launch hold until {raw} ignored: more than {cap} ahead ({source})")
            continue
        names = {str(x) for x in (hold.get("workstreams") or [])}
        ids = {str(x) for x in (hold.get("tasks") or [])}
        if (slug and slug in names) or (tid and tid in ids):
            if found is None or until > found["until"]:
                found = {"until": until, "reason": str(hold.get("reason") or "a dated launch hold")}
    return found


def _positive_hours(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 and hours == hours and hours != float("inf") else None


def hold_cap(hold: dict[str, Any], cfg: dict[str, Any]) -> tuple[timedelta, str]:
    """How far ahead one launch_holds entry may reach, and which setting said so. The
    global launch_hold_max_hours (24 h) unless the entry carries its own positive
    `max_hours` or `allow_long: true` (exactly true, never a truthy string); both stop at
    LAUNCH_HOLD_CEILING_HOURS. A bad per-entry value falls back to the global cap."""
    ceiling = float(LAUNCH_HOLD_CEILING_HOURS)
    own = _positive_hours(hold.get("max_hours"))
    if own is not None:
        return timedelta(hours=min(own, ceiling)), "its max_hours" + (" (capped at the ceiling)" if own > ceiling else "")
    if hold.get("allow_long") is True:
        return timedelta(hours=ceiling), "allow_long ceiling"
    glob = _positive_hours(cfg.get("launch_hold_max_hours", DEFAULTS["launch_hold_max_hours"]))
    hours = glob if glob is not None else float(DEFAULTS["launch_hold_max_hours"])
    return timedelta(hours=min(hours, ceiling)), "dispatch.launch_hold_max_hours"


def launch_hold_text(hold: dict[str, Any]) -> str:
    return f"held until {usage_limits.pacific(hold['until'])} because {hold['reason']}"


def queue_hold(task: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any] | None = None) -> str | None:
    """Why a queued row cannot take a free slot now, apart from capacity, or None when it
    can. The same holds release_queued() applies: a continuation owns its work, a
    prerequisite is still running or ended badly, the usage limit (Claude rows), or a
    quiet-window task. A held row never makes a fresh dispatch wait."""
    if continuation_heir(task, payload) is not None:
        return "continuation"
    dep = unmet_dependency(task, payload)
    if dep is not None:
        return str(dep["id"])
    if failed_dependency(task, payload):
        return "failed prerequisite"
    if usage_limit_until() is not None and str(task.get("agent") or agent_kind()) == "claude":
        return "usage limit"
    quiet = [] if limits_disabled() else (usage_limit_stamp().get("quiet_waiting") or [])
    if quiet and task["id"] not in quiet:
        return "quiet window"
    if launch_hold(task, cfg if cfg is not None else config()) is not None:
        return "launch hold"
    return None


def queued_ahead(task: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Eligible queued rows that rank ahead of `task`, first in line first."""
    at = now()
    mine = queue_rank(task, cfg, at)
    rows = [t for t in payload.get("tasks", []) if t.get("status") == "queued" and t.get("id") != task.get("id")]
    ahead = [t for t in rows if queue_rank(t, cfg, at) < mine and queue_hold(t, payload, cfg) is None]
    return sorted(ahead, key=lambda t: queue_rank(t, cfg, at))


def release_now(args: argparse.Namespace, cfg: dict[str, Any]) -> str:
    """Run a release right away so the row ahead takes the slot a fresh dispatch just
    yielded. Detached (`dispatch.py release`) when launches are async, inline with its
    output swallowed when they are sync (tests, --sync). Best effort: the sweep and the
    post-report release still cover a miss."""
    if not launch_in_background(args, cfg):
        with redirect_stdout(io.StringIO()):
            command_release(argparse.Namespace())
        return "inline"
    log_path = STATE_DIR / "worker-dispatch" / "hooks.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as out:
            subprocess.Popen([sys.executable, str(SCRIPTS / "dispatch.py"), "release"],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), env=env,
                             close_fds=True, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
        return "spawned"
    except OSError as error:
        log(f"release after a yielded slot not started ({error}); the next sweep runs it")
        return "failed"


def launch_prompt(task: dict[str, Any], worktree: Path, worker_id: str) -> str:
    repo, tid, aid, slug = REPO_ROOT, task["id"], task["attempt_id"], task["workstream"]
    ws_dir = worktree / "workstreams" / slug
    # A Claude worker's session id is fixed before launch, so the prompt can name it. Only
    # an Amp thread has a web link (switchboard_agent.thread_pointer).
    claude = str(task.get("agent") or agent_kind()) == "claude"
    thread_arg = (str(task.get("session_id") or "") or "<your Claude session id>") if claude else "<your Amp thread id>"
    pointer = f"claude --resume {thread_arg}" if claude else "your https://ampcode.com/threads/T-... link"
    report = (f"bash {repo}/system/scripts/report-to-cos.sh --task-id {tid} --attempt-id {aid} --origin {task.get('origin') or 'cos'} "
              f"--workstream {slug} --thread-id {thread_arg} --workspace-ref <workspace:N>")
    where = "Work only on this machine in this cmux tab: no remote machines, SSH, nested workers, or extra threads."
    lines = [
        f"You are switchboard worker {worker_id} for task {tid} (attempt {aid}), workstream {slug}, origin {task.get('origin') or 'cos'}. "
        f"{where}",
        f"Your checkout is the git worktree {worktree} on branch task/{tid}, created from main just now. Edit files there with "
        f"their {worktree}/... paths; nothing else writes to it, so no claims, leases, or admission checks exist. Do not edit the "
        f"main checkout at {repo}; read shared state there only through its scripts (ledger, todo, logs are symlinked into your "
        f"worktree). Never push. Do not commit; the finish step commits for you.",
        f"People profiles: write people/ directly, no approval and no staging. {worktree}/people is a link to main's private people/ "
        f"directory, so create or update {worktree}/people/<firstname-lastname>.md there yourself whenever a task surfaces a stakeholder "
        f"(start from people/_template.md, follow people_maintenance.py conventions, keep the Last updated line current, add an "
        f"Interaction Log row). people/ is gitignored and never travels on your branch: never `git add` or commit it (finish and "
        f"auto-push refuse a tracked people/ path). Only if that link is missing or unwritable, stage the file at "
        f".amp/in/artifacts/<firstname-lastname>.md and say so in your report. knowledge/ and knowledge/writing-styles/ keep the "
        f"offer-never-silently-apply rule; this exception is people/ only.",
        f"Read {worktree}/AGENTS.md, {ws_dir}/CONTEXT.md and {ws_dir}/config.yaml first for inherited ownership and context. "
        f"When a path in that guidance is written as {repo}/..., use the same path under {worktree}/ instead.",
        f"Scratch: {worktree}/.amp/in is a real directory that is yours alone (not a link to main's); use {worktree}/.amp/in/{tid}/ "
        f"for scratch scripts and .amp/in/artifacts/ for review artifacts. It moves into {repo}/.amp/in when your branch lands or "
        f"the worktree is dropped, so review notes may reference .amp/in/... paths. Earlier tasks' scratch is at {repo}/.amp/in "
        f"(read-only). A script must derive the repo root from `git rev-parse --show-toplevel` in its cwd, never from its own "
        f"resolved path or an absolute {repo} path: finish refuses to land while main's working tree holds edits to your paths "
        f"written after you started.",
        f"Before writing anywhere outside this repo (for example ~/Documents/Demos, ~/.agents/skills/<name>, ~/Development/example-clone), "
        f"run: python3 {repo}/system/scripts/dispatch.py lock --task-id {tid} --path <absolute path>. It returns allowed=true or waits "
        f"FIFO behind the task that holds that path and tells the origin; keep the lock until you report.",
        "Never wait on a prompt (a permission prompt can block a worker for hours): "
        "act on the delete yourself or continue with a similar or related action so the task never blocks completely. When a delete, an overwrite, or a guardrail block would stop "
        "you, take a non-destructive alternative and continue: write to a new folder or file name instead of deleting the old one, "
        "copy instead of move, or skip the cleanup. List each one in your report under \"Worked around:\". When you must delete your "
        "own files, use a short command with literal paths and no $(...), sudo, or eval, so the permission relay can show CoS they are "
        "this task's files and CoS can allow it without the owner. After a denied prompt, take the alternative; never retry the same command.",
        "Tool routing: the Chrome DevTools lease is one scarce browser, only for surfaces with no CLI/MCP. Figma: the Figma web app; "
        "Docs/Sheets/Slides/Drive use `switchboard drive`; Slack uses `switchboard slack`. "
        "Fall back to the browser only after that call fails, and say which call failed.",
        "Email drafts: every email you draft for the owner is created with `switchboard mail draft` from the owner's "
        "own account and is never sent. Report To, Subject, the draft id, and the full text in your callback. Reply-To stays "
        "unset unless the owner says otherwise; email keeps its exemption from the :robot_face: prefix.",
        f"Slack native lists (true bullets, proper wrap, words unchanged): any list in a Slack message you post is a rich_text_list block, never •, -, *, or 1. text bullets in --text or a "
        f"section block; build the blocks with `python3 system/scripts/slack_format.py blocks \"<text>\"` and pass them as --blocks, keeping --text "
        f"as the fallback. The slack-native-bullets guardrail rejects the rest. Canonical text: {repo}/system/prompts/external-replies.md Rule 4 item 5.",
        f"Plan and list item shape: every plan item, ranked list item, option, or action bullet you write, in a note, Google Doc, Slack "
        f"post, chat reply, or report, opens with a bold 1 to 3 word summary, then one or two complete sentences that say what it is and "
        f"why it matters, with owner and date inside the sentence or at the end in plain words. Internal codes like D1, M1, L1 are never "
        f"the lead; they may follow the summary in parentheses. \"Short\", \"shorter\", or \"brief\" from the owner means fewer items or fewer "
        f"sentences per item, never fragments: a short plan still has the bold lead and at least one full sentence per item. A chat reply "
        f"presenting a plan follows the same shape. Canonical text and a model item: {worktree}/.agents/skills/drafting-docs/SKILL.md, "
        f"\"Writing Voice\".",
        f"Evidence search: any search for assets or evidence (screenshots, recordings, report "
        f"exports, quotes, Figma exports) starts repo-wide in {worktree}/workstreams/ (every workstream, not just {slug}, including "
        f"assets/ folders and Markdown review notes), then {worktree}/projects/, before any external source (Drive, Slack, Figma, "
        f"session recordings, the web). List the workstreams you searched in your review note.",
        f"Knowledge graph: when {repo}/graphify-out/graph.json exists (one graph per repo, built on main "
        f"by the build-knowledge-graph background task with `python3 system/scripts/build-knowledge-graph.py`, structural only, no LLM; "
        f"a worktree has no copy) and the task asks where a rule, script, note, or workstream lives in switchboard or what links to it, run "
        f"`graphify query \"<terms>\" --graph {repo}/graphify-out/graph.json --budget 600` before your first rg or directory search, then "
        f"open and quote the file at the returned src/loc line under {worktree}/ (src paths are root-relative). The graph is a pointer, "
        f"never the citation; the Grounding rule still needs the source line. A graph miss (exit 4 or nothing relevant) is never evidence "
        f"that the thing does not exist: it indexes headings, links, and code symbols only, so fall back to rg repo-wide. "
        f"`python3 system/scripts/generate-recall-pack.py --task \"<ask>\" --workstream {slug}` carries the same hits under graph_pointers. "
        f"If the graph file is missing, skip this step without comment.",
        f"Stakeholder replies: never post in a Slack DM on the owner's behalf; a worker never sends a DM, the owner approves one by saying "
        f"\"send it\" in CoS or Launchpad and those surfaces send it (no guardrail gates the send; the prefix, link-format, and native-bullets checks still run). Write the "
        f"reply under a \"Draft for the owner to send\" heading in the owning note, following {repo}/system/prompts/external-replies.md "
        f"(the owner's voice; only what the recipient needs, in order: status, next step, what the owner needs from them, other relevant detail; "
        f"no questions the owner can decide, no blockers the recipient cannot act on; bullets fine; any message with an ask, on Slack, email, "
        f"GitHub, or a comment, takes that file's Rule 5 shape with its per-surface notes; a Slack draft opens with the :robot_face: "
        f"prefix on its first line even though the owner pastes it, Rule 6, unless the owner said \"no robot\" for that message; every "
        f"short-form message, Slack DM, channel post, thread reply, or GitHub, Linear, Jira, or Google Doc comment, follows that file's "
        f"Rule 7: no word or character limit, well structured with short paragraphs, bullets, native links, and @mentions, the owner's voice "
        f"and tone, and a \"shorter\", \"simpler\", or \"clearer\" ruling from the owner binds every later redraft of that message and is "
        f"recorded next to the draft), and name it in your report. The draft is always shown (Rule 8 of that file): any report that mentions a draft carries the "
        f"full draft text verbatim in its body as a ``` fenced code block, line breaks as real newlines or \" ⏎ \" (report_text keeps "
        f"the block whole and outside the prose budget), so CoS prints it to the owner with any ask; a pointer to the note, a summary, or "
        f"\"draft parked\" is never enough. File before you promise (Rule 9 of that "
        f"file): whenever a draft, reply, or report says switchboard is filing, tracking, or logging something (Linear issue, Jira ticket, "
        f"GitHub issue), create the record before or with the draft through your tracker's CLI or API "
        f"or gh issue create, and put the record ids and URLs verbatim (for example EX-123 "
        f"https://linear.app/...) in the draft text, the note section, and your report body; a draft that promises a filing with no "
        f"created record is incomplete, CoS does not send it and steers you to file first, and report-to-cos.sh appends "
        f"\"⚠️ Filing promised, no record URL\" to a report that promises a filing without a linear.app, Jira, or GitHub "
        f"issue URL. Link every reference and @mention every person (Rule 4 item 6 of that file): every message you draft for "
        f"the owner, on any surface including email, links inline to every thread, message, PR, issue, doc, or session it refers to (Slack "
        f"permalink as <https://url|anchor>, GitHub PR URL, Linear or Jira URL, session link, Google Doc URL; never \"the draft PR\" "
        f"or \"yesterday's message\" without the link; look the link up before drafting), and writes every person it names as the "
        f"surface's native mention (Slack <@Uxxxx> looked up via switchboard slack people, GitHub @handle, the tool's @mention in "
        f"Linear, Jira, Confluence, or a Doc comment) with who they are in two or three words on first mention when the recipient may "
        f"not know them; drop names the recipient would not recognise. Email uses full names with no @mention but still links. Any "
        f"file, reference, section, step, or shorthand it names (ref 05, SKILL.md, the Sandbox doc, step 7, the size test) links to the "
        f"actual artifact (GitHub file URL at the PR head or the PR files anchor, Google Doc heading URL, Confluence anchor) and spells "
        f"out the full file or section name on first mention; shorthand with no link never ships. The slack-people-links guardrail warns, never "
        f"rejects, on a Slack send that names a person from people/ without <@U…> or a PR number without a link.",
        "Code review comments (Rule 10 of {{SWITCHBOARD_ROOT}}/system/prompts/external-replies.md): every GitHub review you "
        "draft or post (gh pr review, gh pr comment, a review reply, a parked draft) follows four rules. (1) Never cite, count, or refer "
        "to a bot comment (Copilot, Dependabot, or any other review bot) or whether the author replied to one; a bot finding worth "
        "raising becomes your own observation with file:line and a link. (2) Code only: behavior, correctness, security, naming, "
        "customer-facing strings, docs that ship with the code; never approvals, owner teams, CI, merge order, schedule, "
        "or long poles. (3) Never restate anything already in the PR title, body, an existing comment, or the linked thread. (4) Before "
        "every bullet ask \"Does the author already know this?\": a comment earns its place only if it tells the author something they "
        "do not already know about the code's behavior (a consequence, a gap, a wrong string, a security hole), anchored with file:line "
        "and a link. Positive example: the new flag reuses the key of an existing feature flag, so turning "
        "one on also turns on the other. Negative examples: \"repo#101 needs four owner teams\" (the author's own reviewer "
        "list) and \"Production here means previews live on staging slots...\" (a PM scoping decision). The author's reviewer list, approval needs, schedule, and PM scoping decisions never go in a "
        "review; PM scoping goes in a separate Slack line from the owner, if at all. The github-review-wording guardrail warns, never rejects, "
        "on a gh pr review or gh pr comment body containing \"as the body says\", \"needs approval\", or "
        "\"owner teams\".",
        f"Feedback on shared Google files (Rule 15 of {repo}/system/prompts/external-replies.md): when you review "
        "or give feedback on a shared Google Doc, Sheet, or Slides file (or content that lives in one), your report offers, as option 1 "
        "(recommended) beside the review table, to leave the feedback in the file as comments anchored to the exact cells, ranges, or "
        "text, plus suggested edits where Docs supports them. Show each comment verbatim with its cell or quote. Each one opens with 🤖 "
        "and follows the owner's style: the proposed text in quotes (an ellipsis for a tail edit), one line of why only when the author cannot "
        "infer it, per-surface or per-audience variants on their own lines, nothing on rows that are fine. Post them only after the owner picks "
        "that option (Sheets: python3 system/scripts/sheet-cell-comment.py post --sheet <url> --spec <json>, because the Drive API cannot "
        "anchor a Sheets comment; Docs: gdrive-cli.py comments add --quote). Never edit the author's text unless the owner asks, never resolve "
        "or reply inside another person's comment thread, and preserve manual edits. Any DM to the author is a one- or two-sentence "
        "pointer to the comments with the file link, never a restatement of them.",
        "Options on decision reports (the CoS Review app and CoS show the same options and content): when you finish with --status blocked, or send any report with --needs-ruling, the report text itself lists the ways "
        "forward in one shape: Options: (a) …; (b) …; (c) … Lean: <letter> because <reason>. At most three options, each one line, "
        "then the Lean line with the letter and the reason. The CoS Review card and CoS both read that exact segment from the report "
        "(cos_review_asks.options_segment): each option becomes a card option, the Lean becomes the recommendation and the card's "
        "reply, nothing is clipped or renumbered. Keep the alternatives table and the bold lean in your review note too, but the "
        "report carries the choice; a report without it makes the card show the status defaults (or, as a fallback, options the feed "
        "scrapes from the note's ## Recommendation and Alternatives table, labelled \"options from note\"), and report-to-cos.sh "
        "appends \"⚠️ Options missing (Options and Lean)\" or \"⚠️ Options missing (Lean)\" (a warning, never a block).",
        "Follow-up on done reports (open work named only in a done report's prose reaches nobody): when you finish with --status done and "
        "work stays open (a reload, a tab to close, a checkpoint, a change you skipped), the report text carries one line "
        "\"Follow-up: <what stays open>\"; the CoS Review feed turns it into a Needs You card (Dispatch the follow-up / Drop it) "
        "and CoS presents it. Veto items are listed as \"N veto items\" so the report shows as its own For review card, never "
        "folded under a newer pass. Open-work prose without a Follow-up line gets \"⚠️ Follow-up not declared\" appended "
        "(review_surface.missing_followup, a warning, never a block). Needs-you stays honest: a Follow-up line is a Needs-you item only when it waits on the owner (a ruling, approval, pick, or "
        "send); a line that waits on someone else (a reply from Drew, Reese, or Lane, a deploy, a PR landing) or names a worker "
        "chore (reload plugins, install-amp-skills, redeploy, commit, re-run) is a For review item labelled with what it waits on. "
        "Write the Follow-up line so the owner is plain: name the owner when they must act, name the person or event when it waits on them.",
        "Owner actions become to-dos (every owner action item goes on the to-do list): "
        "report-to-cos.sh and ask-cos.sh capture them through system/scripts/owner_action_capture.py. Word an action the owner must "
        "take with the owner as its subject (Follow-up: the owner picks a vendor), so it lands on the list, and word your own chore "
        "or a wait on someone else any other way, so it stays off. Never add such a to-do by hand as well. When the action answers "
        "an open ask-cos question, end the line with its id, as in (q1), so the owner's answer closes it. Keep identifiers, balances, "
        "and tax detail out of that line: text that looks sensitive gets a generic title with a pointer to your report.",
        f"Questions go to CoS, never to this tab (the owner never has to look at a worker tab): never stop and wait for input here. When you need a ruling mid-task, "
        f"run bash {repo}/system/scripts/ask-cos.sh --task-id {tid} --option \"<a>\" --option \"<b>\" --default <letter> \"<question>\", "
        f"then keep working on that default; it records the question on your ledger row and delivers one [WORKER QUESTION] line "
        f"to CoS without clobbering a draft. The owner's answer arrives in this thread as a steer; adjust when it lands, and say "
        f"in your report which default you used for any question still unanswered.",
        "Parked turns reach CoS on their own: when a turn ends waiting on the owner (a question, a pick, "
        "an approval), the Stop hook runs system/scripts/turn_wait.py, which sends CoS one needs-ruling callback with your final "
        "text verbatim, once per ask; your row stays active. Do not hand-send a heads-up to CoS. If you must message CoS mid-task, "
        "resolve its ref with `python3 system/scripts/owner_tabs.py cos-target` and send with `python3 system/scripts/owner_tabs.py "
        "message --ref <ref> --text '<text>'`; raw `cmux agent message` takes the target as its first positional argument and has "
        "no --to flag.",
        "Parked drafts are tracked items (a parked draft must never fall through the cracks): every \"Draft for the owner to send\" your terminal report carries as a ``` fenced block "
        "becomes a triage-ledger draft at report time. report-to-cos.sh runs system/scripts/parked_draft_tracker.py track, which "
        "records the draft held with the recipient (slack://dm/<UID> from the leading <@U…> mention or the recipient name in the "
        "\"Draft for … (DM)\" line, else the Slack permalink the report names), the note path and your thread, and mirrors it as a "
        "P1 todo due the next business day (source parked-draft-slack, tag parked-draft). From then on it lives where every draft "
        "lives: send N and no robot N in Comms Triage, reply_check.py --pending, the next-up digest Needs-you section, start-day "
        "seeds, and the CoS Review Needs-you pane, until it is sent or dropped; parked_draft_tracker.py reconcile keeps the draft and "
        "its todo in step each command-center tick. You never create that todo yourself: write the draft in the note under \"Draft "
        "for the owner to send\", open it with <@UID> so the recipient resolves, and put the fenced text in the report. A stamp, review, "
        "or approval ask that @mentions the owner on Slack is a P1 todo due the next business day whose source_ref and context keep the "
        "exact PR set parsed from the message and thread; \"superseded by clean todo\" may only re-point to a todo whose source_ref "
        "covers the same PRs (todo_cli.py refuses a PR-set mismatch with exit 3; --allow-pr-mismatch overrides), so a stamp ask for "
        "PRs A and B is never closed by a todo for PR C.",
        "Names, never bare numbers (always state the name; Rule 13 of "
        f"{REPO_ROOT}/system/prompts/external-replies.md): everything you write for the owner (your report line, the review note, a "
        "callback, a todo title, a draft) names a thing by its name, never by a bare number or id. A PR is its title with the link "
        "(\"the settings copy PR (example-repo)\"), never \"#10001\" or \"repo#10001\"; a Linear or Jira issue is its title "
        "with the link, never \"PROJ-4219\"; a task is its workstream name and what it is doing, never \"rt-000000000028\" alone; a "
        "todo is its title, never its id prefix; a deploy version or commit hash appears only inside a parenthesis after the name "
        "when rollback needs it. The number or id may follow the name in parentheses or sit inside the link; it never stands in for "
        "the name and never appears in a question or ask (\"pause the settings copy PR?\", not \"pause 10001?\"). When a state "
        "file gives you only a number, fetch the title before you write the line (gh pr view <url> --json title, switchboard "
        "linear issue --id <KEY>) or run python3 system/scripts/named_refs.py name \"<text>\", which rewrites repo#N, bare PR "
        "URLs, and Linear keys to title (repo#N) with a link and caches the lookup in .state/named-refs-cache.json. report-to-cos.sh "
        "already leads your report with [<emoji> <Workstream Name> · rt-id], so the body you pass needs no task id of its own.",
        "Images are links, never descriptions (Rule 14 of "
        f"{REPO_ROOT}/system/prompts/external-replies.md): whenever your report, result card, review note, or a reply refers to an "
        "image (generated asset, screenshot, contact sheet, cover option, crop preview), put every image on one comparison page "
        "and give its one link first, for every image you share: run python3 system/scripts/image_compare.py build "
        "--title '<what they are>' --workstream <slug> [--group 'Option A'] --image <path>::'<label>' [--caveat '...'] "
        "[--text '<label>::<verbatim copy>'] (or --spec <json>), which writes a self-contained offline HTML page under "
        "workstreams/<slug>/assets/compare/ and prints its link, for example [Compare 6 images](file:///...html). "
        "This holds for options, variants, before and after, screenshots, and even a single image. Open the HTML page itself, never "
        "hand the owner a folder link or ask them to click: pass "
        "--open to build, or run python3 system/scripts/image_compare.py open <path> for any local HTML review page, which opens "
        "it on the owner's machine once per page version in an unselected cmux browser tab, and say the path in one line (\"Opened /abs/path.html\"). "
        "Never steal focus, not even when the image loads: "
        "never run open <page> or open -g <page>, because Chrome and Preview raise themselves; any page, image, or URL you open by hand "
        "goes through image_compare.py open. Open and link the copy "
        "that exists now: your worktree copy, because the main-checkout path exists only after your branch lands and a link to it "
        "opens nothing before then; build prints that as link and the landed path as main_link, "
        "and report-to-cos.sh rewrites worktree links to main for a done report, which lands first. Then give a clickable link to "
        "each image file as a secondary list, for example [Cover option 1](file:///abs/path/cover-1.png), and in an "
        "Obsidian review note an inline ![Cover option 1](assets/cover-1.png) embed and a relative link to the page "
        "(assets/compare/<page>.html), which resolve before and after landing. Do not describe the image instead: at "
        "most add a short factual label (size, source) or a decision-relevant caveat such as 'crop cuts heads' or 'reuse needs "
        "owner OK'. report-to-cos.sh turns image paths and local review pages (an .html under assets/, artifacts/, or reviews/, "
        "or named compare) into file:// links, builds the compare page for a report that names 2 or "
        "more images without one (or appends \"⚠️ No compare page\" when it cannot), appends \"⚠️ Image not linked\" to a "
        "report that names images with no link, and opens the compare page a delivered report links.",
        "No real customer brands in images (AGENTS.md, Automation Routing): "
        "nothing you create (event covers, decks, sites, docs, motion assets, demo videos, thumbnails, social "
        "visuals) shows a real customer's brand: no company name, logo, or "
        "other customer-identifying text. Customer screenshots never show real data; rebuild them from demo or staging accounts "
        "with fake data. Photos of customers, staff, or products in use are allowed once every brand mark "
        "is cropped or retouched out; check every crop (each size, thumbnail, and video frame) before you offer it, and say in "
        "your report that you checked. Product shots, abstract brand textures, renders, and "
        "illustrations stay the default. When a brand mark cannot be fully removed, leave the photo out and never offer it as an "
        "option.",
        "Agent-mode routing: ultra is the default for every switchboard worker thread. If this session is not already ultra and "
        "the owner did not explicitly choose high for it, recommend once that the owner press Ctrl+S and select ultra; otherwise continue.",
        review_rule(),
        f"Done is observable, not self-reported: this task is done only when branch task/{tid} is merged to main AND your one-line "
        f"report is delivered. A steer or interruption mid-task does not change the deliverable: handle it, then resume. If you are "
        f"genuinely blocked, state exactly what is needed, report it, and do not end your turn while a retry path exists (retry on a "
        f"timer, up to a bound, then report blocked and stop). Ending a turn with the branch unmerged leaves the task in progress and "
        f"earns one resume nudge.",
        "Your --workspace-ref is the literal cmux ref workspace:N (never the tab title or UUID; titles and UUIDs escape reap). "
        "Read it with: CMUX_QUIET=1 cmux workspace list --json | python3 -c 'import json,os,sys; "
        "print(next(w[\"ref\"] for w in json.load(sys.stdin)[\"workspaces\"] if w[\"id\"]==os.environ[\"CMUX_WORKSPACE_ID\"]))'.",
        f"When the work is complete, verified, or truly blocked, run exactly once: {report} --status done|blocked|failed "
        f"'<one line result or blocker, including {pointer}>'. "
        + ("This is a Claude Code session: no Amp thread exists, so never write an ampcode.com link for it. " if claude else "")
        + f"That rebases your branch onto main "
        f"and fast-forwards main (uncommitted edits other workers left on main never block you; they are committed with attribution). "
        f"Only a real merge conflict comes back as failed with the branch name, and your result still stands in that message. "
        f"Read its JSON: delivered=true means the origin received it. Include the result and {pointer} in your final response. "
        f"Do not close this tab; the dispatcher reaps it after delivery. report-to-cos.sh is your last action: nothing that "
        f"needs this thread (an SSO sign-in, a follow-up message, a deploy check) runs after it, because a message to a "
        f"terminal row is lost.",
        f"Router return contract: task {tid}.",
        f"Original request (preserve its scope): {task['prompt']}",
    ]
    if task.get("continuation_of") and task.get("supplemental"):
        parent = task["continuation_of"]
        lines.insert(3, f"This task supplements {parent}, a worker on the same workstream that was still starting when "
                        f"the owner's steer arrived (a steer on a not-ready worker dispatches a separate "
                        f"supplemental worker). That worker may now be running. Read its ledger row with "
                        f"python3 {repo}/system/scripts/running-task-state.py show --task-id {parent} before you start, "
                        f"and reread it before you report: do its scope only where the steer asks you to, do not edit "
                        f"paths its worktree is editing, and if the steer overlaps its work, tell it by native message "
                        f"once its thread is bound and split the work in your report.")
    elif task.get("continuation_of"):
        lines.insert(3, f"This task continues {task['continuation_of']}; read its ledger row with "
                        f"python3 {repo}/system/scripts/running-task-state.py show --task-id {task['continuation_of']}.")
    if task.get("secondary_to") and not task.get("supplemental"):
        lines.insert(3, secondary_clause(task, repo))
    if task.get("provisional_workstream"):
        lines.insert(3, rehome_clause(task, repo))
    lines.extend(profile_worker_clauses(repo))
    return "\n\n".join(lines) + "\n"


def resumed_launch_prompt(task: dict[str, Any], worktree: Path, worker_id: str, full_prompt: Path) -> str:
    """The seed for a steer continuation that forks its parent's Claude session. The fork already holds the parent's launch prompt and everything it did,
    so this names only what changed (task, attempt, checkout, report line, the earlier
    task's spent ids) and the steer. The full contract for this attempt sits in
    `full_prompt` for any rule the session does not remember."""
    repo, tid, aid, slug = REPO_ROOT, task["id"], task["attempt_id"], task["workstream"]
    origin = task.get("origin") or "cos"
    plan = task.get("steer_resume") or {}
    parent = str(plan.get("from_task") or task.get("continuation_of") or "")
    session = str(task.get("session_id") or "<your Claude session id>")
    report = (f"bash {repo}/system/scripts/report-to-cos.sh --task-id {tid} --attempt-id {aid} --origin {origin} "
              f"--workstream {slug} --thread-id {session} --workspace-ref <workspace:N>")
    old_tree = str(plan.get("parent_worktree") or "")
    if plan.get("work") == "worktree":
        work = (f"The earlier task's work is not on main: it is still in {old_tree} on {plan.get('parent_branch') or 'its branch'}. "
                f"Read it there if the steer needs it and carry what it needs into this checkout; never edit or run git there.")
    else:
        work = ("The earlier task's work is on main, so this checkout already has it."
                + (f" Your earlier worktree {old_tree} is not yours anymore: never edit or run git there." if old_tree else ""))
    lines = [
        f"RESUMED SESSION. This session is a fork of your session as the worker for {parent} (workstream {slug}). That task "
        f"has ended and its tab is gone. You are now switchboard worker {worker_id} for a new task, {tid} (attempt {aid}), origin "
        f"{origin}: a continuation that does only the owner's steer below, nothing else from the earlier task.",
        f"Everything in your earlier launch prompt still binds (tool routing, sends and drafts, review notes, names never "
        f"numbers, questions through ask-cos.sh, the report shape), with these changes:",
        f"- Checkout: the git worktree {worktree} on branch task/{tid}, created from main just now. Edit only "
        f"{worktree}/... paths. {work} Never push. Do not commit; the finish step commits for you.",
        f"- Scratch: {worktree}/.amp/in/{tid}/. Paths outside the repo: python3 {repo}/system/scripts/dispatch.py lock "
        f"--task-id {tid} --path <absolute path> first.",
        f"- Questions: bash {repo}/system/scripts/ask-cos.sh --task-id {tid} --option \"<a>\" --option \"<b>\" --default <letter> "
        f"\"<question>\", then continue on the default.",
        f"- The earlier task's ids, branch and report command are spent: never run its report-to-cos.sh line again.",
        f"- Report exactly once at the end: {report} --status done|blocked|failed '<one line result or blocker, including "
        f"claude --resume {session}>'. Read your --workspace-ref with: CMUX_QUIET=1 cmux workspace list --json | python3 -c "
        "'import json,os,sys; print(next(w[\"ref\"] for w in json.load(sys.stdin)[\"workspaces\"] if "
        "w[\"id\"]==os.environ[\"CMUX_WORKSPACE_ID\"]))'. report-to-cos.sh is your last action.",
        f"- The full contract for this attempt is in {full_prompt.as_posix()}; read it only for a rule you do not remember.",
        f"Router return contract: task {tid}.",
        f"Original request (preserve its scope): {task['prompt']}",
    ]
    # The clauses launch_prompt adds for this attempt alone are new to the fork too, so the
    # seed carries them in full (a live primary worker on this workstream, a provisional owner).
    if task.get("secondary_to") and not task.get("supplemental"):
        lines.insert(2, secondary_clause(task, repo))
    if task.get("provisional_workstream"):
        lines.insert(2, rehome_clause(task, repo))
    return "\n\n".join(lines) + "\n"


def profile_worker_clauses(repo: str) -> list[str]:
    """Clauses the installed organisation profile adds to every worker prompt
    (system/prompts/profile-worker-clauses.md, copied in by apply-profile.py; absent on a
    personal install). One clause per line; `{repo}` becomes the repo path."""
    path = REPO_ROOT / "system" / "prompts" / "profile-worker-clauses.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    repo_text = Path(repo).as_posix()
    return [line.replace("{repo}", repo_text) for line in text.splitlines() if line.strip() and not line.startswith("#")]


# --- Amp verification -------------------------------------------------------------

def read_tab_screen(ref: str, lines: int = 50) -> str | None:
    """The tab's visible text, or None when cmux cannot show it yet (no surface,
    RPC failure, timeout). read-screen needs the surface ref from `cmux tree`."""
    try:
        tree = run([CMUX, "tree", "--workspace", ref], timeout=10)
        match = re.search(r"surface:\d+", tree.stdout or "")
        if tree.returncode or not match:
            return None
        screen = run([CMUX, "read-screen", "--workspace", ref, "--surface", match.group(0), "--lines", str(lines)], timeout=10)
    except subprocess.TimeoutExpired:
        return None
    return None if screen.returncode else (screen.stdout or "")


def classify_screen(screen: str | None, agent: str | None = None) -> str:
    """amp (the agent TUI is up), shell (the last non-blank line is a bare shell
    prompt: nothing is running), or unknown (unreadable, blank, mid-render, or any
    other prompt, which waits for a person: never typed into). `agent`
    pins which TUI counts (a ledger row's own agent); None means agent_kind()."""
    if screen is None:
        return "unknown"
    marker = CLAUDE_SCREEN_MARKER if (agent or agent_kind()) == "claude" else AMP_SCREEN_MARKER
    if marker.search(screen):
        return "amp"   # "the agent TUI is up"; the label is shared by every caller and ledger field
    lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
    if lines and SHELL_PROMPT_LINE.search(lines[-1]):
        return "shell"
    return "unknown"


def amp_start_error(screen: str | None) -> str:
    """The line on which `amp` reported it could not reach ampcode.com, or "".
    Judged over the whole screen: the error sits above the prompt the shell
    printed after amp exited."""
    for line in reversed((screen or "").splitlines()):
        if AMP_START_ERROR.search(line):
            return line.strip()[:200]
    return ""


def type_into_tab(ref: str, text: str) -> bool:
    """Type `text` and Enter into the tab."""
    try:
        if run([CMUX, "send", "--workspace", ref, text], timeout=15).returncode:
            return False
        return not run([CMUX, "send-key", "--workspace", ref, "Enter"], timeout=15).returncode
    except subprocess.TimeoutExpired:
        return False


def wait_for_screen(ref: str, wanted: set[str], seconds: float, agent: str | None = None) -> tuple[str, str | None]:
    """Poll the tab until its screen classifies as one of `wanted` or the budget
    ends; returns the last classification and the screen it was judged from."""
    deadline = time.monotonic() + seconds
    while True:
        screen = read_tab_screen(ref)
        kind = classify_screen(screen, agent)
        if kind in wanted or time.monotonic() >= deadline:
            return kind, screen
        time.sleep(AMP_VERIFY_POLL_SECONDS)


# --- wmux launch submit -------------------------------------------------------------
# wmux reads a never-shown background tab as blank text, so classify_screen sees
# "unknown" and nothing would press a lost Enter. On wmux the shim's
# `launch-state` verb reports what the tab accepted instead: the shell prompt log,
# the agent's session provider, and the agent's own prompts.
WMUX_ENTER_GRACE_SECONDS = float(os.environ.get("SWITCHBOARD_WMUX_ENTER_GRACE_SECONDS") or 4)
WMUX_MAX_LAUNCH_ENTERS = int(os.environ.get("SWITCHBOARD_WMUX_MAX_LAUNCH_ENTERS") or 3)


def on_wmux() -> bool:
    try:
        return _mux_backend() == "wmux"
    except Exception:  # noqa: BLE001
        return False


def wmux_launch_state(ref: str) -> dict[str, Any] | None:
    """The shim's launch-state JSON for the tab, or None off wmux or on any failure."""
    if not on_wmux():
        return None
    try:
        result = run([CMUX, "launch-state", "--workspace", ref], timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    try:
        payload = json.loads(result.stdout or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def classify_wmux_launch(state: dict[str, Any] | None, task_id: str, agent: str | None = None) -> str:
    """amp (the agent is up: its session provider, its own prompt, or its TUI on screen),
    submitted (the shell accepted the launch line; the agent is still starting or
    exited), typed (the prompt log is on and holds no launch line: the Enter was
    lost), or unknown (no wmux state or no prompt log)."""
    if not state:
        return "unknown"
    want = "claude" if (agent or agent_kind()) == "claude" else "amp"
    provider = str(state.get("provider") or "").lower()
    if (provider and want in provider) or int(state.get("agent_prompts") or 0) > 0:
        return "amp"
    if classify_screen(state.get("screen") or None, agent) == "amp":
        return "amp"
    if not state.get("prompt_log"):
        return "unknown"
    if any(task_id in str(text) for text in state.get("shell_prompts") or []):
        return "submitted"
    return "typed"


def press_enter(ref: str) -> bool:
    try:
        return not run([CMUX, "send-key", "--workspace", ref, "Enter"], timeout=15).returncode
    except subprocess.TimeoutExpired:
        return False


def retype_launch_line(ref: str, command: str) -> bool:
    """Clear whatever sits on the shell line (Ctrl+U) and type the launch command again."""
    try:
        run([CMUX, "send-key", "--workspace", ref, "ctrl+u"], timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return type_into_tab(ref, command)


def verify_worker_wmux(ref: str, task_id: str, command: str, agent: str | None = None) -> dict[str, Any]:
    """verify_worker_amp on wmux: poll launch-state for AMP_VERIFY_SECONDS; a tab
    whose launch line is typed but not accepted gets Enter after
    WMUX_ENTER_GRACE_SECONDS, up to WMUX_MAX_LAUNCH_ENTERS times, then the line
    is cleared and retyped once. The sweep continues from there."""
    verdict: dict[str, Any] = {"amp_verified": False, "launch_retyped": False, "launch_retypes": 0,
                               "launch_enters": 0, "amp_verify_note": ""}
    deadline = time.monotonic() + AMP_VERIFY_SECONDS
    typed_since: float | None = None
    kind = "unknown"
    while True:
        kind = classify_wmux_launch(wmux_launch_state(ref), task_id, agent)
        if kind == "amp":
            verdict["amp_verified"] = True
            return verdict
        if kind == "typed":
            typed_since = typed_since or time.monotonic()
            if time.monotonic() - typed_since >= WMUX_ENTER_GRACE_SECONDS:
                if verdict["launch_enters"] < WMUX_MAX_LAUNCH_ENTERS:
                    verdict["launch_enters"] += 1
                    log(f"{task_id}: {ref} has the launch line typed but not submitted; pressing Enter "
                        f"({verdict['launch_enters']}/{WMUX_MAX_LAUNCH_ENTERS})")
                    press_enter(ref)
                elif not verdict["launch_retyped"]:
                    log(f"{task_id}: {ref} still has no accepted launch line after {WMUX_MAX_LAUNCH_ENTERS} "
                        f"Enter presses; clearing and retyping it")
                    if retype_launch_line(ref, command):
                        verdict["launch_retyped"] = True
                        verdict["launch_retypes"] += 1
                typed_since = time.monotonic()
        else:
            typed_since = None
        if time.monotonic() >= deadline:
            break
        time.sleep(AMP_VERIFY_POLL_SECONDS)
    if kind == "submitted":
        verdict["amp_verified"] = "unknown"
        verdict["amp_verify_note"] = f"launch line accepted by the shell; the agent was not up after {AMP_VERIFY_SECONDS}s"
    elif kind == "typed":
        verdict["amp_verify_note"] = (f"launch line never accepted after {verdict['launch_enters']} Enter presses"
                                      f"{' and a retype' if verdict['launch_retyped'] else ''}; the sweep keeps pressing Enter")
    else:
        verdict["amp_verified"] = "unknown"
        verdict["amp_verify_note"] = "wmux launch state unreadable; not typed into"
    log(f"{task_id}: {ref} did not show the agent TUI ({verdict['amp_verify_note']}); the sweep judges liveness from here")
    return verdict


def verify_worker_amp(ref: str, task_id: str, command: str, agent: str | None = None) -> dict[str, Any]:
    """After the tab exists: confirm it runs Amp within AMP_VERIFY_SECONDS. Only
    once that budget is spent without Amp is the screen judged, because a shell
    prompt is also what the tab shows for an instant before cmux's keystrokes
    land. A tab at a bare shell prompt (the command was consumed, or `amp` exited with a connection
    error such as ECONNRESET / "Couldn't connect to the Amp server") gets
    the launch command typed again, once per pause in LAUNCH_RETYPE_PAUSES_SECONDS,
    each followed by a fresh AMP_VERIFY_SECONDS wait; the sweep continues from
    there (retype_stalled_launches). An unreadable or unrecognised screen is
    left alone (typing into a slow-rendering Amp TUI would send the command as a
    message). Never raises; the verdict is recorded on the row and logged, and
    the row is never failed for an inconclusive read."""
    if on_wmux() and wmux_launch_state(ref) is not None:
        return verify_worker_wmux(ref, task_id, command, agent)
    verdict: dict[str, Any] = {"amp_verified": False, "launch_retyped": False, "launch_retypes": 0, "amp_verify_note": ""}
    kind, screen = wait_for_screen(ref, {"amp"}, AMP_VERIFY_SECONDS, agent)
    if kind == "amp":
        verdict["amp_verified"] = True
        return verdict
    for pause in LAUNCH_RETYPE_PAUSES_SECONDS:
        if kind != "shell":
            break
        error = amp_start_error(screen)
        why = f"amp exited: {error}" if error else "launch command consumed"
        if pause:
            time.sleep(pause)
            kind, screen = wait_for_screen(ref, {"amp"}, 0)
            if kind != "shell":
                break
        log(f"{task_id}: {ref} is at a bare shell prompt ({why}); typing the launch command again "
            f"({verdict['launch_retypes'] + 1}/{len(LAUNCH_RETYPE_PAUSES_SECONDS)} by the launcher)")
        if not type_into_tab(ref, command):
            verdict["amp_verify_note"] = "cmux send failed while retyping the launch command"
            break
        verdict["launch_retyped"] = True
        verdict["launch_retypes"] += 1
        kind, screen = wait_for_screen(ref, {"amp"}, AMP_VERIFY_SECONDS)
    if kind == "amp":
        verdict["amp_verified"] = True
    else:
        verdict["amp_verified"] = "unknown" if kind == "unknown" else False
        error = amp_start_error(screen)
        detail = f"screen read as {kind} after verification"
        if error:
            detail += f" (amp exited: {error})"
        if kind == "shell":
            detail += f"; retyped {verdict['launch_retypes']}x, the sweep keeps retyping"
        else:
            detail += "; not typed into"
        verdict["amp_verify_note"] = verdict["amp_verify_note"] or detail
        log(f"{task_id}: {ref} did not show the Amp TUI ({verdict['amp_verify_note']}); the sweep judges liveness from here")
    return verdict


def launch_command_for(row: dict[str, Any]) -> str:
    """The exact shell line a worker tab runs: `cat <launch prompt> | <env> amp -m <mode>`.
    One builder for launch() and for every later retype (the sweep, `dispatch.py
    retype`), so a retyped command can never drift from the original.

    --dangerously-skip-permissions is the owner's deliberate default for switchboard workers,
    an accepted risk.

    A `claude` row (row["agent"], set by launch() from SWITCHBOARD_AGENT) runs
    `<env> claude --dangerously-skip-permissions --model <m> --effort <e>
    --session-id <row session_id> "$(cat <launch prompt>)"`: Claude Code takes
    the seed prompt as a positional argument for an interactive session (stdin is
    print mode), and the session id is fixed up front so the ledger can find
    its transcript."""
    slug, mode = str(row["workstream"]), str(row.get("mode") or agent_mode(str(row["workstream"])))
    prompt_file = LAUNCH_PROMPTS / f"{slug}-{row['attempt_id']}.md"
    claude = str(row.get("agent") or "amp") == "claude"
    windows = os.name == "nt"
    # Windows: the line is typed through the wmux shim, which reads \r \n \t in typed
    # text as keys, so every path goes in with forward slashes.
    worktree = Path(str(row["worktree"])).as_posix() if windows else str(row["worktree"])
    env_vars: dict[str, Any] = {
        "SWITCHBOARD_WORKSTREAM_SLUG": slug, "SWITCHBOARD_WORKER_ID": row["worker_id"], "SWITCHBOARD_WORKER_TASK_ID": row["id"],
        "SWITCHBOARD_WORKER_ATTEMPT_ID": row["attempt_id"], "SWITCHBOARD_WORKTREE": worktree,
    }
    if row.get("delivery") == "digest":
        # Automation fan-out, which nobody may be watching: owner_browser.py refuses it the owner's browser.
        env_vars["SWITCHBOARD_SCHEDULED_JOB"] = "1"
    if claude and row.get("session_id"):
        # task-liveness acks the env task only from this session, never from a
        # child `claude -p` that inherits the env.
        env_vars["SWITCHBOARD_WORKER_SESSION_ID"] = row["session_id"]
    env_vars["SWITCHBOARD_AGENT_MODE"] = mode
    chrome = ""
    if claude:
        # Claude in Chrome (the owner's main browser): only a brief with a
        # `Browser: signed-in <reason>` line gets --chrome, and only where
        # SWITCHBOARD_CLAUDE_IN_CHROME=on. The reason rides along for the claude_in_chrome_guard.py hook log.
        try:
            import claude_in_chrome_guard
            chrome_env, chrome = claude_in_chrome_guard.launch_parts(row)
            env_vars.update(chrome_env)
        except Exception:  # a broken guard module never blocks a launch; the worker just gets no --chrome
            chrome = ""
    env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env_vars.items())
    if claude:
        model, effort = claude_mode_flags(mode)
        session = f" --session-id {shlex.quote(str(row['session_id']))}" if row.get("session_id") else ""
        fork = str(row.get("session_fork_from") or "")
        if fork and CLAUDE_SESSION_ID.match(fork) and session:
            # A steer continuation: start from a copy of the parent's
            # session, under this attempt's own fixed session id, so it keeps the parent's
            # context and the ledger still finds its transcript.
            session = f" --resume {shlex.quote(fork)} --fork-session{session}"
        # Prompt suggestions off (system/config/claude-tab-settings.json): a suggested
        # next prompt sits in the composer and the inject guard reads it as a draft.
        tab_settings = REPO_ROOT / "system" / "config" / "claude-tab-settings.json"
        settings = f" --settings {shlex.quote(tab_settings.as_posix())}" if tab_settings.exists() else ""
        if windows:
            # The whole prompt as one argument exceeds the Windows command-line limit
            # ("Argument list too long"); seed a pointer to the file, as routine tabs do.
            seed = shlex.quote(f"Read and follow all instructions in {prompt_file.as_posix()}")
            return f"{env} claude --dangerously-skip-permissions{chrome} --model {model} --effort {effort}{settings}{session} {seed}"
        return (f"{env} claude --dangerously-skip-permissions{chrome} --model {model} --effort {effort}{settings}{session} "
                f'"$(cat {shlex.quote(str(prompt_file))})"')
    pf = prompt_file.as_posix() if windows else str(prompt_file)
    return f"cat {shlex.quote(pf)} | {env} amp -m {mode}"


def reopen_worker_tab(task: dict[str, Any]) -> str:
    """A routing row whose worker tab is gone (closed by hand, or cmux lost it)
    but whose worktree and launch prompt still exist gets a fresh tab running
    the same attempt's command. Returns the new ref, or "" when cmux refused.
    A tab that vanishes while the row is still routing can be reached by neither a
    retype nor the launcher."""
    worktree = Path(str(task["worktree"]))
    prompt_file = LAUNCH_PROMPTS / f"{task['workstream']}-{task['attempt_id']}.md"
    if not worktree.is_dir() or not prompt_file.is_file():
        return ""
    cwd = worktree / "workstreams" / str(task["workstream"])
    if not cwd.is_dir():
        cwd = worktree
    title = str(task.get("tab_title") or worker_title(str(task["workstream"]), next_worker_slot(str(task["workstream"]), ledger.load_payload())))
    create = [CMUX, "new-workspace", "--name", title, "--description", worker_description(task["id"]),
              "--cwd", str(cwd), "--focus", "false", "--command", launch_command_for(task)]
    try:
        result = run(create, timeout=60)
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = ""
    match = re.search(r"workspace:\d+", output)
    ref = match.group(0) if match else ""
    if not ref:
        existing = worker_tab(task["id"])
        ref = str(existing.get("ref") or "") if existing else ""
    if not ref:
        return ""

    def rebind(payload, tid=task["id"], worker_id=task["worker_id"], old=str(task.get("workspace_ref") or "")):
        row = ledger.find_task(payload, tid)
        if row is None or row.get("worker_id") != worker_id or row.get("status") != "routing":
            return None
        row.update(workspace_ref=ref, tab_title=title, tab_reopened_from=old, updated_at=ledger.now_iso(),
                   amp_verify_note=f"tab {old} was gone; reopened as {ref} with the same worktree and launch command")
        return dict(row)

    ledger.locked_mutation(rebind)
    log(f"{task['id']}: worker tab {task.get('workspace_ref')} is gone; reopened as {ref} ({title})")
    return ref


def retype_stalled_launches(only: str | None = None, force: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Sweep step: a routing row whose tab exists but never bound a thread is
    read; a tab back at a bare shell prompt (amp exited on a connection error
    after the launcher's own retypes) gets launch_command_for(row) typed
    again, one retype per sweep tick up to MAX_LAUNCH_RETYPES in all; amp or
    an unknown screen is left alone.
    A row whose tab is gone altogether is given a new tab (reopen_worker_tab)
    on the same worktree, which counts as one retype. Rows younger than
    SWEEP_RETYPE_MIN_AGE are skipped so a launcher still inside its own verify
    loop is not raced."""
    retyped, exhausted, skipped = [], [], []
    for task in ledger.load_payload()["tasks"]:
        if only and task.get("id") != only:
            continue
        if task.get("status") != "routing" or task.get("thread_id") or task.get("launching_at"):
            continue
        if not (task.get("worker_id") and task.get("worktree") and str(task.get("workspace_ref") or "").startswith("workspace:")):
            continue
        if continuation_heir(task, ledger.load_payload()) is not None:
            continue   # retyping would start a second worker on the same work; fail_abandoned_rows retires this row
        last = ledger.parse_iso(task.get("updated_at") or task.get("created_at"))
        if not force and (last is None or now() - last < SWEEP_RETYPE_MIN_AGE):
            continue
        ref, count = str(task["workspace_ref"]), int(task.get("launch_retypes") or 0)
        if count < MAX_LAUNCH_RETYPES or force:
            workspaces = cmux_workspaces()
            if not workspaces:
                # cmux RPC failed or timed out: neither read nor type into a ref we
                # cannot confirm exists; the next tick retries.
                skipped.append({"task_id": task["id"], "workspace_ref": ref, "screen": "cmux unreadable"})
                continue
            if workspace_by_ref_in(ref, workspaces) is None and worker_tab(task["id"], workspaces) is None:
                new_ref = reopen_worker_tab(task)
                if new_ref:
                    def bump_reopen(payload, tid=task["id"], worker_id=task["worker_id"]):
                        row = ledger.find_task(payload, tid)
                        if row is None or row.get("worker_id") != worker_id or row.get("status") != "routing":
                            return None
                        n = int(row.get("launch_retypes") or 0) + 1
                        row.update(launch_retypes=n, launch_retyped=True, updated_at=ledger.now_iso())
                        return dict(row)
                    ledger.locked_mutation(bump_reopen)
                    retyped.append({"task_id": task["id"], "workspace_ref": new_ref, "sent": True, "retypes": count + 1,
                                    "reopened_from": ref, "amp_error": None})
                else:
                    skipped.append({"task_id": task["id"], "workspace_ref": ref, "screen": "tab gone; reopen failed"})
                continue
        # The row's own agent decides which TUI counts (older rows with no
        # agent are Amp, as in launch_command_for), so
        # flipping router.yaml `agent:` never misreads a launch in flight.
        row_agent = str(task.get("agent") or "amp")
        wmux_kind = classify_wmux_launch(wmux_launch_state(ref), task["id"], row_agent) if on_wmux() else "unknown"
        if wmux_kind in ("amp", "submitted"):
            skipped.append({"task_id": task["id"], "workspace_ref": ref, "screen": wmux_kind})
            continue
        if wmux_kind == "typed":
            if count >= MAX_LAUNCH_RETYPES and not force:
                exhausted.append({"task_id": task["id"], "retypes": count})
                continue
            # First an Enter (the line is usually there, only its Enter was lost); a row
            # that already had its Enter presses gets the line cleared and retyped.
            enters = int(task.get("launch_enters") or 0)
            retype = enters >= WMUX_MAX_LAUNCH_ENTERS
            how = "retyped the launch line" if retype else "pressed Enter"
            sent = retype_launch_line(ref, launch_command_for(task)) if retype else press_enter(ref)

            def bump_enter(payload, tid=task["id"], worker_id=task["worker_id"], sent=sent, how=how):
                row = ledger.find_task(payload, tid)
                if row is None or row.get("worker_id") != worker_id or row.get("status") != "routing":
                    return None
                n = int(row.get("launch_retypes") or 0) + (1 if sent else 0)
                row.update(launch_retypes=n, launch_retyped=bool(n), launch_enters=int(row.get("launch_enters") or 0) + 1,
                           updated_at=ledger.now_iso(),
                           amp_verify_note=f"launch line typed but not submitted; sweep {how} ({n}/{MAX_LAUNCH_RETYPES})")
                return dict(row)

            ledger.locked_mutation(bump_enter)
            log(f"{task['id']}: {ref} launch line typed but not submitted; sweep {how} ({count + 1}/{MAX_LAUNCH_RETYPES})")
            retyped.append({"task_id": task["id"], "workspace_ref": ref, "sent": sent, "retypes": count + (1 if sent else 0),
                            "amp_error": None, "how": "retype" if retype else "enter"})
            continue
        screen = read_tab_screen(ref)
        kind = classify_screen(screen, row_agent)
        if kind != "shell":
            skipped.append({"task_id": task["id"], "workspace_ref": ref, "screen": kind})
            continue
        if count >= MAX_LAUNCH_RETYPES and not force:
            if not task.get("launch_retypes_exhausted_at"):
                def mark(payload, tid=task["id"]):
                    row = ledger.find_task(payload, tid)
                    if row is None:
                        return None
                    row.update(launch_retypes_exhausted_at=ledger.now_iso(),
                               amp_verify_note=f"retyped {count}x (launcher and sweep) and the tab is still a bare shell; "
                                               f"fail_abandoned_rows decides next")
                    return dict(row)
                ledger.locked_mutation(mark)
                log(f"{task['id']}: {ref} still a bare shell after {count} retypes; no more retypes")
            exhausted.append({"task_id": task["id"], "retypes": count})
            continue
        error = amp_start_error(screen)
        sent = type_into_tab(ref, launch_command_for(task))

        def bump(payload, tid=task["id"], worker_id=task["worker_id"], sent=sent, error=error):
            row = ledger.find_task(payload, tid)
            if row is None or row.get("worker_id") != worker_id or row.get("status") != "routing":
                return None
            n = int(row.get("launch_retypes") or 0) + (1 if sent else 0)
            row.update(launch_retypes=n, launch_retyped=bool(n), updated_at=ledger.now_iso(),
                       amp_verify_note=(f"bare shell{f' (amp exited: {error})' if error else ''}; "
                                        f"sweep retyped the launch command ({n}/{MAX_LAUNCH_RETYPES})"
                                        if sent else "bare shell; cmux send failed while retyping"))
            return dict(row)

        ledger.locked_mutation(bump)
        log(f"{task['id']}: {ref} at a bare shell{f' (amp exited: {error})' if error else ''}; "
            f"sweep {'retyped the launch command' if sent else 'could not retype (cmux send failed)'} ({count + 1}/{MAX_LAUNCH_RETYPES})")
        retyped.append({"task_id": task["id"], "workspace_ref": ref, "sent": sent, "retypes": count + (1 if sent else 0), "amp_error": error})
    return {"retyped": retyped, "exhausted": exhausted, "skipped": skipped}


def command_retype(args: argparse.Namespace) -> int:
    result = retype_stalled_launches(only=args.task_id, force=args.force)
    print(json.dumps(result))
    return 0 if not args.task_id or result["retyped"] else 1


LAUNCH_LOGS = STATE_DIR / "worker-dispatch" / "launches"


def launch_in_background(args: argparse.Namespace, cfg: dict[str, Any]) -> bool:
    """True when `dispatch` should ack and leave the launch to a detached process.
    `--sync`, SWITCHBOARD_DISPATCH_SYNC=1 (tests, debugging) or `async_launch: false`
    in router.yaml keep the inline launch."""
    if getattr(args, "sync", False) or os.environ.get("SWITCHBOARD_DISPATCH_SYNC") == "1":
        return False
    return bool(cfg.get("async_launch", DEFAULTS["async_launch"]))


def spawn_launcher(task_id: str) -> dict[str, Any] | None:
    """Start `dispatch.py launch --task-id <id>` detached (own session, stdin closed,
    output appended to worker-dispatch/launches/<id>.log) and record launcher_pid and
    launch_log on the row. Returns None when the process could not be started, so
    the caller falls back to the inline launch."""
    LAUNCH_LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LAUNCH_LOGS / f"{task_id}.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    try:
        with log_path.open("ab") as out:
            out.write(f"{ledger.now_iso()} launcher spawned by dispatch\n".encode())
            proc = subprocess.Popen([sys.executable, str(SCRIPTS / "dispatch.py"), "launch", "--task-id", task_id],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}), cwd=str(REPO_ROOT), env=env, close_fds=True)
    except OSError as error:
        log(f"{task_id}: could not start the background launcher: {error}")
        return None

    def note(payload):
        row = ledger.find_task(payload, task_id)
        if row is None:
            return None
        row.update(launcher_pid=proc.pid, launch_log=str(log_path), updated_at=ledger.now_iso())
        return dict(row)

    ledger.locked_mutation(note)
    return {"pid": proc.pid, "log": str(log_path)}


def command_launch(args: argparse.Namespace) -> int:
    """The detached half of an async dispatch: create the worktree, open the tab,
    bind the row, verify Amp. Success is silent (the dispatch ack already told the
    origin; the row and the digest carry the ref). A failure marks the row failed
    and tells the origin one line through notify(), exactly as release_queued does.
    Then reap_all and the status publish, which left the dispatch hot path."""
    cfg = config()
    task = ledger.find_task(ledger.load_payload(), args.task_id)
    if task is None:
        print(json.dumps({"status": "failed", "task_id": args.task_id, "reason": "no ledger row with that id"}))
        return 2
    if task.get("status") != "routing" or task.get("worker_id") or task.get("worktree"):
        print(json.dumps({"status": "skipped", "task_id": args.task_id, "row_status": task.get("status"),
                          "reason": "row is not an unlaunched routing row"}))
        return 0
    try:
        row = launch(task, cfg)
    except Exception as error:
        if isinstance(error, LaunchHold) and hold_launch(args.task_id, error) is not None:
            print(json.dumps({"status": "held", "task_id": args.task_id, "reason": str(error)[:300]}))
            return 0
        restore_resume_pointer(args.task_id)
        run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", args.task_id, "--status", "failed",
             "--summary", f"failed: {error}"], timeout=30)
        delivered = notify(args.task_id, f"failed: launch did not start a worker ({str(error)[:200]})", status="failed")
        print(json.dumps({"status": "failed", "task_id": args.task_id, "reason": str(error)[:300],
                          "origin_notified": bool(delivered.get("delivered"))}))
        return 1
    if row is None:
        print(json.dumps({"status": "already-live", "task_id": args.task_id, "reason": "another launcher holds this row"}))
        return 0
    print(json.dumps({"status": "launched", "task_id": args.task_id, "attempt_id": row.get("attempt_id"),
                      "workspace_ref": row.get("workspace_ref"), "branch": row.get("branch"), "worktree": row.get("worktree")}))
    try:
        reap_all(cfg)
        run(["bash", str(SCRIPTS / "publish-running-tasks-status.sh")], timeout=60)
    except Exception as error:  # the launch is done; a reap or publish problem is the sweep's to retry
        log(f"{args.task_id}: post-launch housekeeping failed: {error}")
    return 0


def claim_launch(task_id: str) -> dict[str, Any] | None:
    """Exactly one launcher per row. `dispatch` and a concurrent `release` (the
    post-report hook) both call launch(); without this, both created the same
    worktree and the loser's cleanup would wipe the winner's checkout. A claim older than 5 min is treated as a crashed launcher."""
    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is None or row.get("status") not in {"queued", "routing"} or row.get("worker_id"):
            return None
        since = ledger.parse_iso(row.get("launching_at"))
        if since and now() - since < timedelta(minutes=5):
            return None
        row.update(status="routing", launching_at=ledger.now_iso(), summary="starting a worker", updated_at=ledger.now_iso())
        return dict(row)
    return ledger.locked_mutation(mutate)


BINDING_LOOKUP_PAUSES = (0, 1, 2, 4)   # seconds before each cmux list try (record_claude_worker_binding)


def record_claude_worker_binding(task_id: str, slug: str, session_id: str, ref: str) -> dict[str, Any] | None:
    """Record a Claude worker's session id in workstream-thread-bindings.json.

    Amp workers get their binding from the in-tab observer
    (cmux_new_contextual_amp_tab.py); Claude Code has no such observer, but the
    launcher already chose the session id, so it records the binding itself.
    This is what lets thread_provenance_for_workstream verify the session when
    a tab is reused or the session is resumed after a cmux restart. Never fails
    the launch: a missing tab id or a bindings write error is logged only."""
    try:
        # The tab is found by its description, else by the ref new-workspace
        # just returned. A lookup right after new-workspace often sees no tab
        # ("no cmux tab id"), so it retries briefly.
        tab, seen = None, 0
        for pause in BINDING_LOOKUP_PAUSES:
            if pause:
                time.sleep(pause)
            workspaces = cmux_workspaces(task_id)
            seen = len(workspaces)
            tab = worker_tab(task_id, workspaces) or workspace_by_ref_in(ref, workspaces)
            if tab and tab.get("id"):
                break
        workspace_id = str((tab or {}).get("id") or "")
        if not workspace_id:
            log(f"{task_id}: claude session {session_id} not recorded in workstream bindings "
                f"(no cmux tab id for {ref}; cmux listed {seen} tabs)")
            return None
        from workstream_thread_bindings import record_binding
        row = record_binding(REPO_ROOT, session_id, slug, workspace_id, ref, role="worker",
                             evidence={"launcher": "dispatch", "task_id": task_id, "agent": "claude"})
        return row
    except Exception as error:  # noqa: BLE001  (evidence is best effort; the worker already runs)
        log(f"{task_id}: claude session {session_id} not recorded in workstream bindings: {error}")
        return None


def secondary_primary(slug: str, task_id: str) -> dict[str, Any] | None:
    """The live worker this task joins as a secondary on `slug`, or None: the oldest
    routing or active row on that workstream that holds a worker slot, other than this task
    and the rows it continues. A row with no agent past the fresh window is dead, and a task
    sent to take over a row is that row's replacement, free to use that row's worktree.
    Only what the launch prompt needs."""
    payload = ledger.load_payload()
    task = ledger.find_task(payload, task_id) or {}
    skip = continuation_ancestors(task, payload)
    pool = {"tasks": [t for t in live_workers(payload) if str(t.get("id")) not in skip]}
    row = system_owners.primary_worker(slug, pool, task_id)
    if not row:
        return None
    proven = bool(row.get("thread_id")) and str(row.get("thread_id")) in ledger.live_agents()
    return {k: row.get(k) for k in ("id", "tab_title", "workspace_ref", "worktree", "session_id", "thread_id") if row.get(k)} \
        | {"ask": " ".join(str(row.get("prompt") or "").split())[:160], "proven_live": proven}


def secondary_clause(task: dict[str, Any], repo: Path) -> str:
    """Launch-prompt paragraph for a secondary worker (join
    as '<Name> 2', read the primary's note and state, do not disturb its
    running work, coordinate through cmux agent message)."""
    primary = task["secondary_to"]
    pid = primary["id"]
    tab = primary.get("tab_title") or f"the {titleize(task['workstream'])} worker"
    ref = primary.get("workspace_ref") or ""
    worktree = primary.get("worktree") or ""
    owned = task.get("system_owner") or {}
    why = (f" The router chose this workstream because the linked source names {', '.join(owned.get('identifiers', [])[:2])} "
           f"({'; '.join(owned.get('evidence', [])[:1])})." if owned else "")
    # Absent on a row written before this flag, which reads as proven: same words as then.
    state = "is already live" if primary.get("proven_live", True) else "started recently and may still be coming up"
    return (f"Secondary worker: {tab} ({pid}) {state} in this workstream on: {primary.get('ask') or 'its own task'}. "
            f"You join it as a numbered secondary worker; you are neither queued behind it nor moved to another "
            f"workstream.{why} Before you start, read its ledger row with python3 {repo}/system/scripts/running-task-state.py "
            f"show --task-id {pid}, this workstream's CONTEXT.md, and the newest review note or result card it names, so "
            f"you know what it found and what it is changing. Do not disturb its running work: never edit files in its "
            f"worktree{f' {worktree}' if worktree else ''}, never stop, restart, or rerun its jobs, boxes, or processes, "
            f"and never take a lock it holds. Coordinate through cmux agent message: when your task touches the same "
            f"system, box, site, or files as its task, send it one short note when you start and again when you find "
            f"something that bears on its work, with python3 "
            f"{repo}/system/scripts/owner_tabs.py message --ref {ref or '<its workspace:N>'} --text '<note>'. If your "
            f"fix needs its paths or its box, tell it and let it make the change, then say so in your report. Report "
            f"your own task only.")


def rehome_clause(task: dict[str, Any], repo: Path) -> str:
    """Launch-prompt paragraph for a task whose workstream the launcher created
    before anyone read the content, so the system it names may belong to another
    workstream."""
    tid, slug = task["id"], task["workstream"]
    return (f"Provisional workstream: {slug} was created for this ask before anyone read what it links. If the content "
            f"names a system (a VM, a hosted page, a scheduled job, a repo, a Slack channel), its owner owns this "
            f"task. Resolve the owner with python3 {repo}/system/scripts/system_owners.py '<the failure or alert text>' "
            f"--exclude-task {tid} (it reads config.yaml systems:, live ledger rows, and CONTEXT mentions). The generic "
            f"vm-fleet workstream owns VM infrastructure only, never one box's failure. To re-home, run python3 "
            f"{repo}/system/scripts/running-task-state.py update --task-id {tid} --workstream <owner>, work under "
            f"workstreams/<owner>/, and report with --workstream <owner>. If the owner already has a live worker (the "
            f"JSON names it as primary), you are its secondary: read its ledger row and newest note, leave its running "
            f"work alone, and coordinate with python3 {repo}/system/scripts/owner_tabs.py message --ref <its workspace:N> "
            f"--text '<note>'. Say in your report that {slug} can be archived when you leave it empty.")


LAUNCH_HOLD_MAX_NO_TERMINAL = 5   # a tab that keeps losing its terminal is a real fault; a mux that is down is not


def hold_launch(task_id: str, error: LaunchHold) -> dict[str, Any] | None:
    """Put a row whose launch could not open a tab back in the queue with the plain
    reason. Returns the row, or None when the row should fail instead (the tab kept
    losing its terminal LAUNCH_HOLD_MAX_NO_TERMINAL times)."""
    what = "wmux is not reachable" if error.unreachable else "the new tab had no terminal"

    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is None or row.get("status") not in {"routing", "queued"}:
            return None
        holds = int(row.get("launch_holds") or 0) + 1
        if not error.unreachable and holds > LAUNCH_HOLD_MAX_NO_TERMINAL:
            return None
        row.update(status="queued", summary=f"waiting on the multiplexer because {what}; retrying on each sweep",
                   launch_holds=holds, launch_hold_reason=str(error)[:300], requeued_at=ledger.now_iso(), updated_at=ledger.now_iso())
        row.setdefault("launch_hold_since", ledger.now_iso())
        row.pop("launching_at", None)
        return dict(row)

    held = ledger.locked_mutation(mutate)
    if held is not None:
        log(f"{task_id}: launch held ({what}): {str(error)[:200]}")
    return held


def report_launch_holds(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Sweep step: a launch still held after dispatch.launch_hold_tell_minutes tells the
    origin once. It keeps retrying; nothing is failed here."""
    try:
        minutes = int(cfg.get("launch_hold_tell_minutes") or 15)
    except (TypeError, ValueError):
        minutes = 15
    told = []
    for task in ledger.load_payload()["tasks"]:
        since = ledger.parse_iso(task.get("launch_hold_since"))
        if task.get("status") != "queued" or since is None or task.get("launch_hold_told_at"):
            continue
        if now() - since < timedelta(minutes=minutes):
            continue
        slug = str(task.get("workstream") or "")
        line = (f"{task_lead(task['id'], slug)} waiting on the multiplexer because it could not open a tab for "
                f"{int((now() - since).total_seconds() // 60)}m ({str(task.get('launch_hold_reason') or '')[:160]}). "
                f"It retries on each sweep and starts by itself.")
        sent = tell_cos_direct(line)
        if not sent.get("ok"):
            continue   # the mux that is down carries this line too; the next tick tries again

        def mark(payload, tid=task["id"]):
            row = ledger.find_task(payload, tid)
            if row is not None:
                row["launch_hold_told_at"] = ledger.now_iso()
            return row

        ledger.locked_mutation(mark)
        told.append({"task_id": task["id"]})
    return told


def resume_checkout(task: dict[str, Any]) -> tuple[Path, str] | None:
    """The dead attempt's worktree and branch when this attempt was queued to carry on
    from them (fail_abandoned_rows) and they are still usable, else None."""
    path, branch = str(task.get("resume_worktree") or ""), str(task.get("resume_branch") or "")
    if not path or not branch:
        return None
    if not Path(path).exists():
        log(f"{task['id']}: resume checkout {path} no longer exists; starting from main")
        return None
    problem = verify_worktree(Path(path), branch)
    if problem:
        # create_worktree would delete this checkout and its branch. Nothing here is
        # worth that: hold, and in the end fail with the checkout where it is.
        raise LaunchHold(f"cannot resume from {path}: {problem}")
    link_shared_state(Path(path))
    return Path(path), branch


def restore_resume_pointer(task_id: str) -> None:
    """A resumed launch that ends failed never ran bind(), so the row has no `worktree` or
    `branch`. Put them back: settle_checkout then records worktree_preserved and the
    recovery text names a real checkout."""
    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is None or not row.get("resume_worktree") or row.get("worktree"):
            return None
        row.update(worktree=row["resume_worktree"], branch=row.get("resume_branch") or row.get("branch"))
        return dict(row)
    ledger.locked_mutation(mutate)


def resume_note(task: dict[str, Any], worktree: Path, branch: str) -> str:
    """Opens the launch prompt of a worker that carries on after a dead attempt."""
    session = str(task.get("resume_session") or "")
    why = {"context": "stopped at the context limit (its session could not compact and continue)",
           "usage": "paused on the Claude usage limit, and its tab or session was gone when the limit reset"}.get(
        str(task.get("resume_reason") or ""), "died without reporting (its tab or session is gone)")
    return (f"RESUME: attempt {task.get('resume_from_attempt')} of this task {why}. "
            f"This attempt runs in the same checkout, {worktree} on branch {branch}, so the line below that says it was created "
            f"from main just now does not apply. Before anything else run git status and git log main..HEAD there, read what the "
            f"dead attempt left, and continue from it. Never discard that work."
            + (f" The dead attempt's Claude session id was {session}; its transcript is under ~/.claude/projects if you need it."
               if session else "") + "\n\n")


def launch(task: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Create the worktree, open one cmux tab running Amp on the launch prompt.
    Returns None when another launcher already holds this row."""
    claimed = claim_launch(task["id"])
    if claimed is None:
        return None
    task = materialize_pending_workstream({**task, **claimed})
    slug = task["workstream"]
    worker_id = f"worker-{secrets.token_hex(6)}"
    # A workstream that already has a live worker gets this task as its numbered
    # secondary ("<Name> 2"), never queued behind it or sent elsewhere. The launch prompt tells it how to coordinate.
    primary = None if task.get("supplemental") else secondary_primary(slug, task["id"])
    if primary:
        task = {**task, "secondary_to": primary}
    try:
        # strict: a cmux that never answers the tab list will not answer new-workspace
        # either. Read before any checkout exists, so a mux that is down costs one list
        # call per tick and never a worktree.
        open_tabs = cmux_workspaces(task["id"], strict=True)
    except CmuxUnreachable as error:
        raise LaunchHold(str(error), unreachable=True) from error
    if workstream_untracked(slug):
        # A scaffold another writer left uncommitted on main (process-new-inbox.py,
        # a hand-made folder) would be missing from the branch cut below and
        # collide at landing; commit it first.
        commit_scaffold(slug, f"untracked on main when {task['id']} launched")
    resumed = resume_checkout(task)
    worktree, branch = resumed if resumed else create_worktree(task["id"])
    problem = verify_worktree(worktree, branch)
    if problem:
        # No tab is opened on an unverified checkout and nothing is deleted (the
        # path may not be ours to remove); the row fails with the reason.
        raise RuntimeError(f"worktree_unverified: {problem}")
    LAUNCH_PROMPTS.mkdir(parents=True, exist_ok=True)
    prompt_file = LAUNCH_PROMPTS / f"{slug}-{task['attempt_id']}.md"
    agent = agent_kind()
    # Claude Code picks up its session id from the launcher so the transcript
    # under ~/.claude/projects can be found later (claude_transcript_tail). Set before
    # the prompt is written, because the prompt names it.
    session_id = str(uuid.uuid4()) if agent == "claude" else ""
    fork = launch_fork_source(task, agent, bool(resumed))
    task = {**task, "agent": agent, **({"session_id": session_id} if session_id else {}),
            **({"session_fork_from": fork} if fork else {})}
    prompt_text = launch_prompt({**task, "branch": branch}, worktree, worker_id)
    if fork:
        # The forked session already read its parent's launch prompt; it gets a short seed and
        # keeps this attempt's full contract beside it.
        full_file = LAUNCH_PROMPTS / f"{slug}-{task['attempt_id']}.full.md"
        full_file.write_text(prompt_text, encoding="utf-8")
        prompt_text = resumed_launch_prompt({**task, "branch": branch}, worktree, worker_id, full_file)
    prompt_file.write_text((resume_note(task, worktree, branch) + prompt_text) if resumed else prompt_text, encoding="utf-8")
    cwd = worktree / "workstreams" / slug
    if not cwd.is_dir():
        cwd = worktree
    mode = agent_mode(slug)
    command = launch_command_for({**task, "worker_id": worker_id, "worktree": str(worktree), "mode": mode})
    # Titled like the owner tab plus a slot number ("🛠️ Switchboard Maintenance 2"), so
    # the owner can tell which owner tab a worker belongs to; the description is the
    # identifier the reaper and the sidebar trust (see worker_tab_reason).
    title = worker_title(slug, next_worker_slot(slug, ledger.load_payload(), open_tabs))
    create = [CMUX, "new-workspace", "--name", title, "--description", worker_description(task["id"]),
              "--cwd", str(cwd), "--focus", "false", "--command", command]
    ref, retried = "", False
    for attempt in (1, 2):
        try:
            result = run(create, timeout=60)
            output, error = result.stdout + result.stderr, (result.stderr or result.stdout).strip()
            failed = bool(result.returncode)
            timed_out = failed and "timeout" in error.lower()   # cmux's own "did not respond within 10 seconds"
        except subprocess.TimeoutExpired:
            output, error, failed, timed_out = "", "cmux new-workspace gave no answer within 60s", True, True
        match = re.search(r"workspace:\d+", output)
        if not failed and match:
            ref = match.group(0)
            break
        if timed_out:
            note_cmux_timeout(task["id"], "new-workspace", 60 if "60s" in error else 10)
        # cmux's "timeout: ... its result is unknown" (and a hung CLI) can follow a
        # tab that was in fact created, leaving it open with no row pointing at it. The tab's
        # description is the task id, so look for it (twice: a slow cmux may
        # still be opening it) before deciding.
        existing = worker_tab(task["id"])
        if existing is None:
            time.sleep(5)
            existing = worker_tab(task["id"])
        if existing is not None and str(existing.get("ref") or "").startswith("workspace:"):
            ref = str(existing["ref"])
            log(f"{task['id']}: cmux new-workspace reported '{error[:120]}' but tab {ref} carries this task's description; adopting it")
            break
        no_terminal = bool(NO_TERMINAL.search(error))
        if not (timed_out or no_terminal) or attempt == 2:
            if not resumed:   # a resumed checkout holds a dead worker's work and stays for the retry
                drop_worktree({"id": task["id"], "worktree": str(worktree), "branch": branch})
            if MUX_DOWN.search(error) or no_terminal:
                raise LaunchHold(f"cmux new-workspace failed: {error[:300]}", unreachable=bool(MUX_DOWN.search(error)))
            raise RuntimeError(f"cmux new-workspace failed: {error[:300]}")
        # A timeout with no tab anywhere: the RPC most likely never reached cmux.
        # One retry; more concurrent tabs make the 10 s RPC timeouts more likely,
        # not a cmux that is gone (no worker cap).
        retried = True
        log(f"{task['id']}: cmux new-workspace {'lost its terminal' if no_terminal else 'timed out'} and no tab carries this "
            f"task's description; retrying once")
    if retried:
        # The first call's tab can still appear after the retry made its own:
        # keep `ref`, close every other tab described as this task's worker.
        wanted = worker_description(task["id"])
        for dup in cmux_workspaces(task["id"]):
            dup_ref = str(dup.get("ref") or "")
            if dup_ref and dup_ref != ref and str(dup.get("description") or "").strip() == wanted:
                closed, why = cmux_close_result(dup_ref, str(dup.get("id") or ""), f"{task['id']} duplicate launch tab")
                log(f"{task['id']}: duplicate worker tab {dup_ref} from the timed-out first new-workspace "
                    f"{'closed' if closed else f'could not be closed ({why})'}; {ref} is the worker")

    def bind(payload):
        row = ledger.find_task(payload, task["id"])
        row.update(workspace_ref=ref, worktree=str(worktree), branch=branch, worker_id=worker_id, mode=mode,
                   tab_title=title, status="routing", summary="started", launched_at=ledger.now_iso(), updated_at=ledger.now_iso(),
                   agent=agent)
        if session_id:
            row["session_id"] = session_id
        else:
            row.pop("session_id", None)
        if fork:
            row["session_fork_from"] = fork
        else:
            row.pop("session_fork_from", None)
        if primary:
            row["secondary_to"] = primary
        else:
            row.pop("secondary_to", None)
        for key in ("launch_holds", "launch_hold_since", "launch_hold_reason", "launch_hold_told_at",
                    "launching_at", "finished_at", "report_delivered", "report_delivery_unknown",
                    "amp_verified", "launch_retyped", "launch_retypes", "launch_enters", "launch_retypes_exhausted_at", "amp_verify_note"):
            row.pop(key, None)
        return row

    bound = ledger.locked_mutation(bind)
    if session_id:
        record_claude_worker_binding(task["id"], slug, session_id, ref)
    # Windows: a launched worker is live work, so hold the laptop awake now rather
    # than at the next tick.
    # Idempotent, one holder; a no-op on macOS and in the test sandbox.
    keep_awake.sync([task["id"]])
    # Bound first, so the ref is on the row even if this launcher dies while it
    # waits; then prove the tab runs Amp (a bare shell gets the command once more).
    verdict = verify_worker_amp(ref, task["id"], command, agent)

    def note(payload):
        row = ledger.find_task(payload, task["id"])
        if row is None or row.get("worker_id") != worker_id:
            return None
        row.update({k: v for k, v in verdict.items() if v != ""}, updated_at=ledger.now_iso())
        return dict(row)

    return ledger.locked_mutation(note) or bound


def inherited_delivery(continuation_of: str | None, existing: dict[str, Any] | None = None) -> str | None:
    """Completion policy for a dispatch that names none. A retry keeps its own
    row's policy; a continuation (a steer on a reaped row, a supplemental
    worker) keeps its parent's, and a parent with none is the owner's ask, so
    direct. With delivery=None a continuation would fall back to the digest, whose
    next post can defer on a draft in CoS, so its done report could reach nobody."""
    if existing and existing.get("delivery"):
        return str(existing["delivery"])
    if not continuation_of:
        return None
    parent = ledger.find_task(ledger.load_payload(), continuation_of) or {}
    return str(parent.get("delivery") or "direct")


def command_dispatch(args: argparse.Namespace) -> int:
    cfg = config()
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print(json.dumps({"status": "rejected", "reason": "empty prompt"}))
        return 2
    if args.request_id:
        twin = ledger.find_request_twin(ledger.load_payload(), args.request_id)
        if twin and twin.get("id") != args.task_id:
            print(json.dumps({"status": "duplicate", "task_id": twin["id"], "attempt_id": twin.get("attempt_id"),
                              "workspace_ref": twin.get("workspace_ref"), "origin": twin.get("origin"), "deduplicated": True}))
            return 0
    task_id = args.task_id or f"rt-{secrets.token_hex(6)}"
    existing = ledger.find_task(ledger.load_payload(), task_id)
    if not getattr(args, "delivery", None):
        args.delivery = inherited_delivery(args.continuation_of, existing)
    not_started: dict[str, Any] | None = None
    if existing and existing.get("status") in {"routing", "active"} and not args.force_redispatch:
        not_started = not_started_evidence(existing)
        if not_started is None:
            print(json.dumps({"status": "already-live", "task_id": task_id, "attempt_id": existing.get("attempt_id"),
                              "workspace_ref": existing.get("workspace_ref"), "origin": existing.get("origin")}))
            return 0
        if not_started["verdict"] != "not-started":
            print(json.dumps({"status": "stalled", "task_id": task_id, "attempt_id": existing.get("attempt_id"),
                              "workspace_ref": existing.get("workspace_ref"), "origin": existing.get("origin"),
                              "evidence": not_started, "reason": f"worker gone but {existing.get('worktree')} holds uncommitted "
                              f"work; inspect it, then re-run with --force-redispatch to discard and start over"}))
            return 3
        log(f"{task_id} attempt {existing.get('attempt_id')} never started (idle {not_started['idle_seconds']}s, "
            f"no live thread); dispatching a new attempt")
        if not_started["worker_tab"] and (ws := workspace_by_ref(not_started["workspace_ref"])):
            cmux_close(not_started["workspace_ref"], str(ws.get("id") or ""), f"{task_id} never started")  # the empty tab the launcher typed nothing into
    pinned = args.workstream or (existing or {}).get("workstream")
    # The async ack writes only the ledger: a new workstream is
    # planned here and scaffolded by the launcher, so no workstreams/ file changes
    # (and no diff) show under the CoS tool call.
    defer = launch_in_background(args, cfg)
    if args.workstream and not workstream_exists(args.workstream):
        # --workstream <new-slug> from router-dispatch.sh: the same as a "Workstream:
        # <new-slug>" line, a proposed new workstream. The pin wins over a
        # continuation's parent, so no continuation_of here.
        choice = choose_workstream(f"Workstream: {args.workstream}\n{prompt}", cfg, None, defer_scaffold=defer)
    elif pinned:
        choice = {"slug": pinned, "reason": "pinned", "created": False, "weak_match": None}
    else:
        choice = choose_workstream(prompt, cfg, args.continuation_of, defer_scaffold=defer)
    slug = str(choice["slug"])
    pending = choice.get("pending") or None
    pending_name = str((pending or {}).get("name") or "")
    owner = {"slug": slug, "reason": choice["reason"], "created": choice["created"], "weak_match": choice["weak_match"],
             **({"name": pending_name, "scaffold": "launcher"} if pending else {})}
    if choice["reason"] in ("directive", "pinned") and (misfile := maintenance_misfile_warning(prompt, slug)):
        # Warn, never block: the surface sees it in `message` and names a topic owner next time.
        log(f"{task_id}: {misfile}")
        print(misfile, file=sys.stderr)
        owner["warning"] = choice["warning"] = misfile
    executor_box = ""  # workers always run locally; the kit has no remote executor
    # The row starts as "routing" (this process owns its launch); `release` only
    # ever launches rows that dispatch explicitly set to "queued" below. If this
    # process dies before launch/queue, the sweep's requeue_orphaned_routes()
    # flips the row to "queued" after NOT_STARTED_AFTER.
    start = run([sys.executable, str(SCRIPTS / "running-task-state.py"), "start", "--task-id", task_id, "--prompt", prompt,
                 "--origin", args.origin, "--workstream", slug,
                 *(["--request-id", args.request_id] if args.request_id else []),
                 *(["--delivery", args.delivery] if args.delivery else []),
                 *[a for d in args.depends_on for a in ("--depends-on", d)]], timeout=30)
    if start.returncode:
        print(json.dumps({"status": "failed", "task_id": task_id, "reason": start.stderr.strip()[:300]}))
        return 1
    task = json.loads(start.stdout)
    supplemental = bool(args.continuation_of) and bool(getattr(args, "supplemental", False))
    steer_resume = getattr(args, "steer_resume", None) if args.continuation_of else None
    if args.continuation_of or executor_box or pending:
        def note_options(payload):
            row = ledger.find_task(payload, task_id)
            if pending:
                row["pending_workstream"] = pending
            if args.continuation_of:
                row["continuation_of"] = args.continuation_of
            if steer_resume:
                row["steer_resume"] = steer_resume
            if supplemental:
                row["supplemental"] = True
            return row
        task = ledger.locked_mutation(note_options)
    if args.continuation_of and getattr(args, "retire_parent_ask", True):
        # A steer or continuation on a finished row means the owner already ruled on whatever that row
        # asked; its CoS Review rows leave Needs you, so handled rows do not linger.
        # A supplemental worker on a still-running parent passes retire_parent_ask=False:
        # the parent has not asked anything yet, so there is nothing to retire.
        steer_text = prompt
        for lead in ("Steer on ", "Supplemental steer on "):
            if prompt.startswith(lead) and "\n\n" in prompt:
                steer_text = prompt.split("\n\n", 1)[1]
                break
        retire_review_ask(args.continuation_of, by=f"dispatch:{args.origin}",
                          reason=f"handled in chat: continued as {task_id}: {' '.join(steer_text.split())[:120]}")
    payload = ledger.load_payload()
    dep = unmet_dependency(task, payload)
    # A dated hold also keeps the ask out of an open owner tab: the tab would start it now.
    if dep is None and launch_hold(task, cfg) is None and owner_tab_eligible(args, cfg, pending, supplemental, executor_box):
        sent = deliver_to_owner_tab(task, slug, args.origin)
        if sent.get("delivered"):
            print(json.dumps({"router": "dispatch", "created": True, "status": "routed", "launch": "owner-tab",
                              "task_id": task_id, "attempt_id": task["attempt_id"], "workspace_ref": sent["workspace_ref"],
                              "thread_id": sent["thread_id"], "workstream": slug, "branch": None, "worktree": None,
                              "origin": args.origin, "depends_on": task.get("depends_on") or [],
                              "continuation_of": args.continuation_of, "replaced_attempt": not_started, "owner": owner,
                              "owner_tab": sent, "executor": "owner-tab",
                              "message": f"{task_lead(task_id, slug)} sent to the open {owner_tabs.owner_name(slug, REPO_ROOT)} "
                                         f"tab{owner_note(choice)}"}))
            return 0
        if sent.get("reason"):
            log(f"{task_id}: owner tab not used ({sent['reason']}); opening a worker")
            owner["owner_tab_skipped"] = sent["reason"]
    workers = [t for t in live_workers(payload) if t["id"] != task_id]
    ahead: list[dict[str, Any]] = []
    if dep is not None:
        reason = f"waiting on {task_name(dep)} because this task depends on it"
    elif (dated := launch_hold(task, cfg)) is not None:
        reason = f"{launch_hold_text(dated)}; it starts on its own after that"
    elif (memory_hold := commit_hold_reason(cfg, live=len(workers))):
        reason = f"waiting on memory because {memory_hold}"
    elif workers and at_capacity(len(workers), cfg, memory={}):
        oldest = min(workers, key=lambda t: t.get("created_at") or "")
        reason = (f"waiting on {task_name(oldest)} because {len(workers)} workers are already running "
                  f"(dispatch.{'max_workers_windows' if _on_windows() and int(cfg.get('max_workers_windows') or 0) > 0 else 'max_workers'} {worker_cap(cfg)})")
    elif worker_cap(cfg) is not None and (ahead := queued_ahead(task, payload, cfg)):
        # A slot is free but an older queued row is first in line: this row
        # queues behind it and a release starts it now. With no cap nothing queues on
        # capacity, so there is no line to respect.
        reason = (f"waiting on {task_name(ahead[0])} because it was queued first and takes the free slot; "
                  f"this task starts after it")
    else:
        reason = ""
    if reason:
        run([sys.executable, str(SCRIPTS / "running-task-state.py"), "queue", "--task-id", task_id, "--summary", reason], timeout=30)
        released = release_now(args, cfg) if ahead else None
        print(json.dumps({"status": "queued", "task_id": task_id, "attempt_id": task["attempt_id"], "workstream": slug,
                          "origin": args.origin, "reason": reason, "depends_on": task.get("depends_on") or [],
                          "continuation_of": args.continuation_of, "replaced_attempt": not_started, "owner": owner,
                          **({"queued_behind": [t["id"] for t in ahead], "release": released} if ahead else {}),
                          "message": f"{task_lead(task_id, slug, pending_name)} {reason}{owner_note(choice)}"}))
        return 0
    if launch_in_background(args, cfg):
        launcher = spawn_launcher(task_id)
        if launcher is not None:
            # The ack: row written, launcher detached, nothing else on the hot path
            # (reap_all and the status publish run in the launcher; the origin
            # replies with `message` and moves on).
            print(json.dumps({"router": "dispatch", "created": True, "status": "routed", "launch": "background",
                              "task_id": task_id, "attempt_id": task["attempt_id"], "workspace_ref": None,
                              "workstream": slug, "branch": None, "worktree": None, "origin": args.origin,
                              "mode": agent_mode(slug), "depends_on": task.get("depends_on") or [],
                              "continuation_of": args.continuation_of, "replaced_attempt": not_started, "owner": owner,
                              "executor": "local", "executor_box": None,
                              "launcher_pid": launcher["pid"], "launch_log": launcher["log"],
                              "message": f"{task_lead(task_id, slug, pending_name)} accepted; worker starting"
                                         f"{owner_note(choice)}"}))
            return 0
        log(f"{task_id}: background launcher could not be spawned; launching inline")
    try:
        row = launch(task, cfg)
    except Exception as error:
        if isinstance(error, LaunchHold) and hold_launch(task_id, error) is not None:
            print(json.dumps({"status": "queued", "task_id": task_id, "attempt_id": task["attempt_id"], "workstream": slug,
                              "origin": args.origin, "reason": str(error)[:300],
                              "message": f"{task_lead(task_id, slug, pending_name)} waiting on the multiplexer because it "
                                         f"could not open a tab; it retries on each sweep"}))
            return 0
        restore_resume_pointer(task_id)
        run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", task_id, "--status", "failed",
             "--summary", f"failed: {error}"], timeout=30)
        print(json.dumps({"status": "failed", "task_id": task_id, "reason": str(error)[:300], "origin": args.origin}))
        return 1
    if row is None:
        live = ledger.find_task(ledger.load_payload(), task_id) or task
        print(json.dumps({"status": "already-live", "task_id": task_id, "attempt_id": live.get("attempt_id"),
                          "workspace_ref": live.get("workspace_ref"), "origin": args.origin, "reason": "another launcher holds this row"}))
        return 0
    print(json.dumps({"router": "dispatch", "created": True, "status": "routed", "task_id": task_id, "attempt_id": row["attempt_id"],
                      "workspace_ref": row["workspace_ref"], "workstream": slug, "branch": row["branch"], "worktree": row["worktree"],
                      "origin": args.origin, "mode": row.get("mode"), "depends_on": row.get("depends_on") or [],
                      "continuation_of": args.continuation_of, "replaced_attempt": not_started, "owner": owner,
                      "executor": "local", "executor_box": None,
                      "message": f"{task_lead(task_id, slug, pending_name)} started{owner_note(choice)}"}))
    reap_all(cfg)
    return 0


# --- open owner tab instead of a new worker ----------------------------
#
# One tab per topic: a second worker does not open beside an idle owner tab. The ask goes to the
# open, idle owner tab by cmux agent message, the row is bound to that tab's thread and
# ref, and no worktree or worker tab exists. The owner should never have to wait, so a busy owner tab (a turn running, or text typed in its
# box; owner_tabs.tab_idle) gets a parallel worker as before ("<Name> 2"). A worker
# also opens when the owner has no open tab, the tab runs Amp (cmux agent messages
# reach Claude sessions only), or cmux refuses the message.

def owner_tab_eligible(args: argparse.Namespace, cfg: dict[str, Any], pending: dict[str, Any] | None,
                       supplemental: bool, executor_box: str) -> bool:
    """Whether this dispatch may go to an open owner tab at all. Never for a new
    workstream (no tab can exist), a supplemental steer worker (the not-ready
    fallback keeps its own worker), an explicit --parallel-worker,
    or with dispatch.owner_tab_dispatch false / DISPATCH_NO_OWNER_TAB=1."""
    if os.environ.get("DISPATCH_NO_OWNER_TAB") == "1" or cfg.get("owner_tab_dispatch") is False:
        return False
    return not (pending or supplemental or executor_box or getattr(args, "parallel_worker", False))


def owner_tab_prompt(task: dict[str, Any], slug: str, origin: str, ref: str, thread_id: str) -> str:
    """What the owner tab receives: the full ask, the task id, and how to report."""
    report = (f"bash system/scripts/report-to-cos.sh --task-id {task['id']} --attempt-id {task['attempt_id']} "
              f"--origin {origin} --workstream {slug} --thread-id {thread_id} --workspace-ref {ref} "
              f"--status done|blocked|failed '<one line result>'")
    return (f"New switchboard task {task['id']} for this workstream, dispatched from {origin} to this open tab instead of a "
            f"new worker. Work it here, in this tab and the main checkout as usual: no worktree, "
            f"no new tab.\n\n"
            f"--- ask ---\n{task.get('prompt') or ''}\n--- end of ask ---\n\n"
            f"Report it exactly once when it is complete, verified, or truly blocked (drafts verbatim in ``` blocks, a "
            f"Follow-up: line for open work, Options and Lean on a blocked report; system/prompts/dispatch-core.md):\n"
            f"{report}\n"
            f"Show the result to the owner in this tab too. This tab stays open; nothing reaps it.")


def deliver_to_owner_tab(task: dict[str, Any], slug: str, origin: str,
                         workspaces: list[dict[str, Any]] | None = None,
                         agents: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send the ask to the owner's open, idle Claude tab and bind the row to it.
    {"delivered": True, "workspace_ref", "thread_id", "title", "message_id", "agent_state"}
    or {"delivered": False, "reason"} (no tab, an Amp tab, a busy tab, cmux refused);
    every not-delivered case falls through to a normal worker launch."""
    workspaces = cmux_workspaces(task["id"]) if workspaces is None else workspaces
    tab = owner_tabs.owner_tab(slug, workspaces, root=REPO_ROOT)
    if tab is None:
        return {"delivered": False, "reason": ""}
    by_ws = owner_tabs.agents_by_workspace(ledger.live_agents() if agents is None else agents)
    agent = owner_tabs.tab_agent(tab, by_ws)
    ref, title = str(tab.get("ref") or ""), owner_tabs.tab_title(tab)
    if agent["agent"] != "claude" or not agent["thread_id"]:
        return {"delivered": False, "reason": f"open owner tab {title} ({ref}) runs {agent['agent'] or 'no agent'}; "
                                              f"cmux agent message reaches Claude sessions only"}
    # Busy owner tab (a turn running, or text typed in its box): a parallel worker on the
    # same owner picks the ask up instead, so the owner never waits.
    idle, why = owner_tabs.tab_idle(ref, agent["state"])
    if not idle:
        return {"delivered": False, "busy": True,
                "reason": f"open owner tab {title} ({ref}) is busy ({why}); a parallel worker takes the ask"}
    sent = owner_tabs.agent_message(ref, owner_tab_prompt(task, slug, origin, ref, agent["thread_id"]),
                                    sender=f"switchboard dispatch ({origin})")
    if not sent.get("ok"):
        return {"delivered": False, "reason": f"open owner tab {title} ({ref}) refused the message: {sent.get('reason')}"}

    def bind(payload):
        row = ledger.find_task(payload, task["id"])
        stamp = ledger.now_iso()
        row.update(status="active", workspace_ref=ref, thread_id=agent["thread_id"], agent="claude", owner_tab=True,
                   owner_tab_title=title, owner_message_id=sent.get("id"), started_at=stamp, updated_at=stamp,
                   summary=f"sent to the open {title} tab")
        return row
    ledger.locked_mutation(bind)
    return {"delivered": True, "workspace_ref": ref, "thread_id": agent["thread_id"], "title": title,
            "message_id": sent.get("id"), "message_state": sent.get("state"), "agent_state": agent["state"]}


# --- steers on a finished row ------------------------------------
#
# A hold-only question answer sent 18 s after a worker finished started a continuation
# cold (new worktree, new tab, no context) just to record "hold"; a "stop, record and
# finish" steer sent 5 s after that one finished started another. Two workers, no work. Now:
#   1. a continuation forks the parent's Claude session instead of starting cold
#      (steer_resume_plan, launch_fork_source, launch_command_for);
#   2. `--record-only` records a hold, ack or ruling on a finished row and starts nothing
#      (record_steer_ruling);
#   3. a steer on a row whose continuation is live goes to it, and one on a row whose newest
#      continuation just finished continues that one (steer_chain_target).

def session_context_tokens(session_id: str) -> int | None:
    """The context size of a Claude session's last main-thread call (input + cache read +
    cache write tokens), or None when the transcript is missing or has no usage. A fork of
    the session re-caches about this much on its first call."""
    path = claude_transcript_path(session_id)
    if path is None:
        return None
    last: int | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "assistant" or record.get("isSidechain"):
                    continue
                usage = (record.get("message") or {}).get("usage") or {}
                total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens"))
                if total:
                    last = total
    except OSError:
        return None
    return last


def steer_resume_plan(parent: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether a continuation of the finished `parent` can fork its Claude session.
    {"resume": True, "session_id", "from_task", "context_tokens", "work": "main"|"worktree",
    "parent_worktree", "parent_branch"} or {"resume": False, "reason", ...}. A fork needs a
    Claude parent whose transcript is still on disk, whose work is on main (landed) or in
    its kept worktree, and whose last context is at most steer_resume_max_context_tokens,
    because the fork re-caches all of it (measured, see DEFAULTS)."""
    sid = str(parent.get("session_id") or "")
    plan: dict[str, Any] = {"resume": False, "session_id": sid, "from_task": str(parent.get("id") or "")}
    if not cfg.get("steer_resume_session", True):
        return {**plan, "reason": "dispatch.steer_resume_session is off"}
    if str(parent.get("agent") or "") != "claude" or not CLAUDE_SESSION_ID.match(sid):
        return {**plan, "reason": "the parent row is not a Claude session"}
    if agent_kind() != "claude":
        return {**plan, "reason": "workers run Amp, which cannot fork a Claude session"}
    if claude_transcript_path(sid) is None:
        return {**plan, "reason": f"the parent's transcript ({sid}) is gone"}
    worktree, branch = str(parent.get("worktree") or ""), str(parent.get("branch") or "")
    if parent.get("landed_commit"):
        work = "main"
    elif worktree and Path(worktree).is_dir():
        work = "worktree"
    elif branch and on_main(branch):
        work = "main"
    else:
        return {**plan, "reason": "the parent's work is neither on main nor in a kept worktree"}
    tokens = session_context_tokens(sid)
    if tokens is None:
        return {**plan, "reason": "the parent's transcript records no usage"}
    try:
        limit = int(cfg.get("steer_resume_max_context_tokens", DEFAULTS["steer_resume_max_context_tokens"]))
    except (TypeError, ValueError):
        limit = int(DEFAULTS["steer_resume_max_context_tokens"])
    plan.update(context_tokens=tokens, work=work, parent_worktree=worktree, parent_branch=branch)
    if tokens > limit:
        return {**plan, "reason": f"the parent's context is {tokens} tokens, past steer_resume_max_context_tokens "
                                  f"({limit}); a fork would re-cache all of it, so a cold worker is cheaper"}
    return {**plan, "resume": True}


def launch_fork_source(task: dict[str, Any], agent: str, resumed: bool) -> str:
    """The parent session this launch forks, or "". Only a Claude worker's first attempt of a
    steer continuation forks; a retry (a dead or limit-stopped attempt) carries on from its
    own checkout instead, and a transcript that vanished since the steer falls back to cold."""
    plan = task.get("steer_resume") or {}
    sid = str(plan.get("session_id") or "") if plan.get("resume") else ""
    if agent != "claude" or not CLAUDE_SESSION_ID.match(sid) or resumed or int(task.get("attempt") or 1) > 1:
        return ""
    if claude_transcript_path(sid) is None:
        log(f"{task['id']}: parent session {sid} has no transcript any more; starting a cold worker")
        return ""
    return sid


def continuation_descendants(task_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every non-supplemental continuation chained from `task_id` (children, their children,
    ...), newest first."""
    children: dict[str, list[dict[str, Any]]] = {}
    for row in payload.get("tasks") or []:
        if row.get("continuation_of") and not row.get("supplemental"):
            children.setdefault(str(row["continuation_of"]), []).append(row)
    found, todo, seen = [], [task_id], {task_id}
    while todo:
        for child in children.get(todo.pop(), []):
            if child.get("id") not in seen:
                seen.add(str(child.get("id")))
                found.append(child)
                todo.append(str(child.get("id")))
    return sorted(found, key=lambda r: str(r.get("created_at") or ""), reverse=True)


def continuation_root(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """The first row of `row`'s continuation chain (itself when it continues nothing). A
    supplemental worker is not a link: it ends the walk."""
    root, seen = row, {str(row.get("id") or "")}
    while root.get("continuation_of") and not root.get("supplemental"):
        parent = ledger.find_task(payload, str(root["continuation_of"]))
        if parent is None or str(parent.get("id")) in seen:
            break
        seen.add(str(parent["id"]))
        root = parent
    return root


def _ended_at(row: dict[str, Any]) -> datetime | None:
    return ledger.parse_iso(row.get("finished_at") or row.get("updated_at"))


def steer_chain_target(row: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The race guard for a steer on a terminal row, over its whole chain (root and every
    continuation, siblings included). {"action": "redirect", "task_id"} when another row of
    the chain is still starting or running: the steer goes to it (message or queue), never a
    second worker on the same work. {"action": "chain", "task_id"} when a row of the chain
    newer than the named one finished within steer_chain_window_minutes: the new
    continuation continues that one. None otherwise."""
    named = str(row.get("id") or "")
    root = continuation_root(row, payload)
    chain = [r for r in [root, *continuation_descendants(str(root.get("id") or ""), payload)] if str(r.get("id")) != named]
    live = sorted((r for r in chain if str(r.get("status") or "") not in ledger.TERMINAL_STATUSES),
                  key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if live:
        return {"action": "redirect", "task_id": str(live[0]["id"]), "status": str(live[0].get("status") or ""),
                "reason": f"{named}'s chain already has a continuation in progress ({live[0]['id']}, {live[0].get('status')})"}
    newer = [r for r in chain if str(r.get("created_at") or "") > str(row.get("created_at") or "")
             and str(r.get("status") or "") != "cancelled" and _ended_at(r) is not None]
    if not newer:
        return None
    newest = max(newer, key=lambda r: _ended_at(r))   # type: ignore[arg-type, return-value]
    try:
        window = max(0.0, float(cfg.get("steer_chain_window_minutes", DEFAULTS["steer_chain_window_minutes"])))
    except (TypeError, ValueError):
        window = float(DEFAULTS["steer_chain_window_minutes"])
    age = (now() - _ended_at(newest)).total_seconds()   # type: ignore[operator]
    if age > window * 60:
        return None
    return {"action": "chain", "task_id": str(newest["id"]), "status": str(newest.get("status") or ""),
            "reason": f"{named}'s newest continuation {newest['id']} finished {int(age)} s ago; continuing it instead"}


def freshest_session_row(parent: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """The row whose session a continuation of `parent` forks: the newest finished
    continuation in `parent`'s own lineage (it was forked from `parent`, so it knows
    `parent`'s work and its own), else `parent` itself. A fork of a stale parent would not
    know what its later continuations changed."""
    done = [r for r in continuation_descendants(str(parent.get("id") or ""), payload)
            if str(r.get("status") or "") in ledger.TERMINAL_STATUSES and r.get("status") != "cancelled"]
    return done[0] if done else parent


@contextmanager
def steer_chain_lock(row: dict[str, Any]) -> Iterator[None]:
    """One steer at a time per continuation chain, from the race check to the new row's
    continuation_of write, so two steers sent together (CoS and a CoS Review reply, or CoS
    and Launchpad) never both start a continuation."""
    root = str(continuation_root(row, ledger.load_payload()).get("id") or row.get("id") or "unknown")
    LOCKS.mkdir(parents=True, exist_ok=True)
    with (LOCKS / f"steer-chain-{root}.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def steer_decision(row: dict[str, Any] | None, agents: dict[str, dict[str, Any]],
                   workspaces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Where a same-task steer must go.

    A native message to a worker thread is only acted on while that thread has an
    executor. Once the row is terminal, `reap_all` closes the tab within
    reap_grace_seconds and anything sent to the thread after that is lost. So a
    terminal row takes a new --continuation-of dispatch; a non-terminal row keeps
    the native message.

    A non-terminal row with no thread bound yet (a routing row inside the async
    launch window, or a queued row) has no target either. A supplemental worker
    there would put several workers on one job, so
    the steer is queued against that task (`pending_steers`) and delivered in order
    once the worker's thread is live (its ack or first heartbeat); a worker that
    never comes up sends the origin one callback, and nothing is spawned. A row
    whose thread is bound but not live in cmux keeps the native message: the thread
    exists and the message is read when it resumes.

    Returns {"action": "unknown-task" | "continuation" | "queue" | "message", ...}."""
    if not row:
        return {"action": "unknown-task"}
    status = str(row.get("status") or "")
    if status in ledger.TERMINAL_STATUSES:
        return {"action": "continuation", "status": status,
                "reason": f"row is {status}: its tab is reaped within reap_grace_seconds and a message to its "
                          f"thread has no executor to act on it; dispatching --continuation-of {row.get('id')}"}
    # A Claude worker's session id is fixed at launch (launch() passes --session-id),
    # so it is the thread before task-liveness binds thread_id on the first
    # heartbeat, so that window never reads as "no thread".
    workspace_ref = str(row.get("workspace_ref") or "")
    agent = str(row.get("agent") or "")
    thread_id = str(row.get("thread_id") or (row.get("session_id") if agent == "claude" and workspace_ref else "") or "")
    if not thread_id:
        return {"action": "queue", "status": status, "thread_id": "",
                "workspace_ref": workspace_ref, "thread_live": False,
                "reason": f"row is {status} with no thread bound yet: queued on {row.get('id')}, delivered when "
                          f"its worker starts"}
    live = agents.get(thread_id)
    if live is None and workspace_ref and workspaces:
        # The cmux session store is written lazily, so a worker started a minute ago
        # is often missing from live_agents(). The open tab and its agent are the
        # evidence: any live agent in the row's tab counts.
        tab = next((w for w in workspaces if str(w.get("ref") or "") == workspace_ref), None)
        tab_id = str((tab or {}).get("id") or "").upper()
        live = next((a for a in agents.values() if tab_id and str(a.get("workspace_id") or "").upper() == tab_id), None)
        if live is None and tab is not None and agent == "claude":
            live = {"agent": "claude", "thread_id": thread_id, "state": "", "via": "open tab"}
    return {"action": "message", "status": status, "thread_id": thread_id,
            "workspace_ref": workspace_ref, "thread_live": bool(live),
            "agent": agent or str((live or {}).get("agent") or "")}


def steer_message(task_id: str, origin: str, text: str) -> str:
    """What a live worker receives for a same-task steer."""
    return (f"Same-task steer for {task_id} from {origin} (the owner's words follow). Handle it inside this task: it changes "
            f"how you deliver, never what the deliverable is, and you still report once with report-to-cos.sh.\n\n{text}")


def cmux_agent_message(target: str, text: str, sender: str, timeout: float = 45.0) -> dict[str, Any]:
    """`cmux agent message <target>`: the Claude equivalent of Amp's send_thread_message,
    through owner_tabs.agent_message (ok only when cmux accepted it and the recipient
    runs an agent that reads messages). DISPATCH_NO_AGENT_MESSAGE=1 turns it off."""
    if os.environ.get("DISPATCH_NO_AGENT_MESSAGE") == "1":
        return {"ok": False, "reason": "DISPATCH_NO_AGENT_MESSAGE=1"}
    return owner_tabs.agent_message(target, text, sender=sender, timeout=timeout)


# --- queued steers ---------------------------------------
#
# A steer on a worker that has not started
# waits on its own row in `pending_steers` and is delivered in order when the worker
# acks (session start) or heartbeats; running-task-state.py spawns `deliver-steers` for
# a row with a queue, and the sweep runs it for every row as the backstop. Only a
# terminal row still takes a continuation. A steer still queued after
# dispatch.steer_queue_timeout_minutes sends the origin one urgent callback.

def steer_not_started(row: dict[str, Any]) -> bool:
    """The worker has not acked its task: routing or queued, or active (a launched row)
    with no started_at and no heartbeat. A steer for it waits in the row's queue."""
    status = str(row.get("status") or "")
    if status in {"routing", "queued"}:
        return True
    return status == "active" and not row.get("started_at") and not row.get("heartbeat_at")


def pending_steers(row: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Queued steers on a row that are neither delivered nor dropped, oldest first."""
    return [e for e in ((row or {}).get("pending_steers") or [])
            if isinstance(e, dict) and not e.get("delivered_at") and not e.get("dropped_at")]


def queue_steer(task_id: str, origin: str, text: str) -> dict[str, Any] | None:
    """Append a steer to the row's queue. None when the row is gone or terminal (the
    caller then takes the continuation path). updated_at is left alone: a queued steer
    is not worker progress, and the stuck and abandoned rules judge from it."""
    def add(payload: dict[str, Any]) -> dict[str, Any] | None:
        row = ledger.find_task(payload, task_id)
        if row is None or row.get("status") in ledger.TERMINAL_STATUSES:
            return None
        entry = {"id": f"st-{secrets.token_hex(4)}", "origin": origin, "text": text, "queued_at": ledger.now_iso()}
        row["pending_steers"] = [*(row.get("pending_steers") or []), entry]
        return {"entry": entry, "queued": len(pending_steers(row))}
    return ledger.locked_mutation(add)


def mark_steer(task_id: str, steer_id: str, **fields: Any) -> None:
    def update(payload: dict[str, Any]) -> None:
        row = ledger.find_task(payload, task_id)
        for entry in (row or {}).get("pending_steers") or []:
            if isinstance(entry, dict) and entry.get("id") == steer_id:
                entry.update({k: v for k, v in fields.items() if v is not None})
    ledger.locked_mutation(update)


def steer_worker_name(slug: str) -> str:
    """The workstream's display name for a steer line ("Onboarding Videos")."""
    return (_config_scalar(REPO_ROOT / "workstreams" / slug / "config.yaml", "name") or titleize(slug)) if slug else "the"


def steer_queue_clock(row: dict[str, Any], entry: dict[str, Any]) -> datetime | None:
    """When the timeout clock for a queued steer started: the later of the steer and the
    row's latest launch claim, so a queued row waiting on a prerequisite and a relaunch
    each get the full window once the worker begins launching."""
    stamps = [ledger.parse_iso(entry.get("queued_at"))]
    stamps += [ledger.parse_iso(row.get(k)) for k in ("launching_at", "launched_at", "requeued_at")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def steer_queue_timeout(cfg: dict[str, Any]) -> int:
    try:
        return max(0, int(cfg.get("steer_queue_timeout_minutes", DEFAULTS["steer_queue_timeout_minutes"])))
    except (TypeError, ValueError):
        return int(DEFAULTS["steer_queue_timeout_minutes"])


def tell_cos_direct(text: str) -> dict[str, Any]:
    """One line to the live CoS tab by cmux agent message, for a row whose ledger status
    must not change (a terminal row: report-to-cos.sh would settle it as a duplicate)."""
    try:
        target = owner_tabs.cos_tab(owner_tabs.cmux_workspaces(), owner_tabs.agents_by_workspace())
    except Exception as error:  # noqa: BLE001  (best effort; the log keeps the line)
        return {"ok": False, "reason": f"cos target unreadable: {error}"}
    if not target.get("ref"):
        return {"ok": False, "reason": target.get("reason") or "no CoS tab open"}
    return cmux_agent_message(target["ref"], text, sender="switchboard steer queue")


@contextmanager
def steer_queue_lock(task_id: str) -> Iterator[bool]:
    """One deliverer per row, so an ack spawn and the sweep never send a steer twice."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    with (LOCKS / f"steer-{task_id}.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def deliver_row_steers(task_id: str, cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Deliver one row's queued steers in order, or report them. Caller holds the lock."""
    out: dict[str, list[dict[str, Any]]] = {"delivered": [], "waiting": [], "timed_out": [], "dropped": []}
    row = ledger.find_task(ledger.load_payload(), task_id) or {}
    queue = pending_steers(row)
    if not queue:
        return out
    slug = str(row.get("workstream") or "")
    name = steer_worker_name(slug)
    status = str(row.get("status") or "")
    if status in ledger.TERMINAL_STATUSES:
        # The row ended (a failed launch, a cancel) before its worker read the queue. Its
        # tab is gone, so the steers are reported, never sent to a dead thread and never
        # turned into a worker here: a steer sent again now takes the continuation path.
        quoted = " | ".join(" ".join(str(e.get("text") or "").split())[:200] for e in queue)
        line = (f"{task_lead(task_id, slug)} {len(queue)} queued steer(s) never reached the {name} worker ({task_id}): "
                f"it ended {status} before it started. Nothing was spawned; steer it again to dispatch a continuation. "
                f"Queued text: {quoted}")
        told = tell_cos_direct(line)
        log(f"{task_id}: {len(queue)} queued steer(s) dropped, row {status} (CoS told: {told.get('ok')})")
        for entry in queue:
            mark_steer(task_id, entry["id"], dropped_at=ledger.now_iso(), dropped_reason=f"row {status} before start",
                       origin_told=bool(told.get("ok")))
            out["dropped"].append({"task_id": task_id, "steer_id": entry["id"], "origin_told": bool(told.get("ok"))})
        return out
    if row.get("worker_id") and not row.get("executor"):
        row = heal_row_ref(row, cmux_workspaces(task_id), ledger.live_agents())
    ref = str(row.get("workspace_ref") or "")
    agent = str(row.get("agent") or "")
    thread_id = str(row.get("thread_id") or (row.get("session_id") if agent == "claude" and ref else "") or "")
    started = not steer_not_started(row) and bool(thread_id)
    remaining = list(queue)
    if started:
        for entry in queue:
            origin = str(entry.get("origin") or row.get("origin") or "cos")
            text = str(entry.get("text") or "")
            if agent == "claude" and ref:
                sent = cmux_agent_message(ref, steer_message(task_id, origin, text), sender=f"switchboard steer ({origin})")
                ambiguous = not sent.get("ok") and (bool(sent.get("ambiguous")) or "timed out" in str(sent.get("reason") or ""))
                if not sent.get("ok") and not ambiguous:
                    log(f"{task_id}: queued steer {entry['id']} not delivered yet ({sent.get('reason')})")
                    mark_steer(task_id, entry["id"], last_attempt_at=ledger.now_iso(),
                               attempts=int(entry.get("attempts") or 0) + 1, last_error=str(sent.get("reason") or "")[:200])
                    break   # keep the order: later steers wait behind this one
                # A timeout may come after cmux queued the message: never send it twice.
                mark_steer(task_id, entry["id"], delivered_at=ledger.now_iso(), delivered_via="cmux agent message",
                           message_id=sent.get("id"), ambiguous=True if ambiguous else None, delivered_to=ref)
                via = "cmux agent message" + (" (timed out; may be queued)" if ambiguous else "")
            else:
                # An Amp worker reads native thread messages only, which a script cannot
                # send: the origin gets the text and the thread to send it to.
                told = notify(task_id, f"queued steer for the {name} worker ({task_id}) is ready: its thread "
                                       f"{thread_id} is live; send it there by native message: {text}",
                              status="active", urgent=True)
                if not told.get("delivered"):
                    mark_steer(task_id, entry["id"], last_attempt_at=ledger.now_iso(),
                               attempts=int(entry.get("attempts") or 0) + 1, last_error=str(told.get("error") or "")[:200])
                    break
                mark_steer(task_id, entry["id"], delivered_at=ledger.now_iso(), delivered_via="origin native message",
                           delivered_to=thread_id)
                via = "origin native message"
            log(f"{task_id}: queued steer {entry['id']} delivered by {via}")
            out["delivered"].append({"task_id": task_id, "steer_id": entry["id"], "via": via})
            remaining.remove(entry)
    minutes = steer_queue_timeout(cfg)
    for entry in remaining:
        clock = steer_queue_clock(row, entry)
        age = (now() - clock) if clock else timedelta(0)
        overdue = minutes > 0 and status != "queued" and age >= timedelta(minutes=minutes)
        if overdue and not entry.get("timeout_reported_at"):
            why = (f"cmux refused the message ({entry.get('last_error')})" if started and entry.get("last_error")
                   else f"the worker has not started ({status}{', tab ' + ref if ref else ', no tab yet'})")
            line = (f"steer queued {int(age.total_seconds() // 60)}m for the {name} worker ({task_id}) is still undelivered: "
                    f"{why}. Nothing was spawned; it still delivers if the worker starts. Check the tab, or re-dispatch with "
                    f"router-dispatch.sh --task-id {task_id} --force-redispatch (the queued steer goes to the new attempt).")
            told = notify(task_id, line, status=status if status in {"active", "routing"} else "routing", urgent=True)
            mark_steer(task_id, entry["id"], timeout_reported_at=ledger.now_iso(), origin_told=bool(told.get("delivered")))
            log(f"{task_id}: queued steer {entry['id']} overdue; origin told: {told.get('delivered')}")
            out["timed_out"].append({"task_id": task_id, "steer_id": entry["id"], "origin_told": bool(told.get("delivered"))})
        else:
            out["waiting"].append({"task_id": task_id, "steer_id": entry["id"], "status": status, "started": started})
    return out


def deliver_queued_steers(task_id: str | None = None, cfg: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every row with queued steers (or one row): deliver, report overdue, drop on terminal."""
    cfg = cfg or config()
    total: dict[str, list[dict[str, Any]]] = {"delivered": [], "waiting": [], "timed_out": [], "dropped": [], "busy": []}
    rows = [t for t in ledger.load_payload()["tasks"] if pending_steers(t) and (task_id is None or t.get("id") == task_id)]
    for row in rows:
        with steer_queue_lock(row["id"]) as held:
            if not held:
                total["busy"].append({"task_id": row["id"]})
                continue
            for key, items in deliver_row_steers(row["id"], cfg).items():
                total[key].extend(items)
    return total


def spawn_steer_delivery(task_id: str) -> bool:
    """Detached `dispatch.py deliver-steers --task-id ID` (never waits on cmux).
    SWITCHBOARD_NO_STEER_SPAWN=1 turns it off (tests; the sweep still delivers)."""
    if os.environ.get("SWITCHBOARD_NO_STEER_SPAWN") == "1":
        return False
    try:
        log_path = STATE_DIR / "logs" / "steer-queue.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as handle:
            subprocess.Popen([sys.executable, str(SCRIPTS / "dispatch.py"), "deliver-steers", "--task-id", task_id],
                             stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
        return True
    except OSError as error:
        log(f"{task_id}: deliver-steers not spawned ({error}); the sweep delivers it")
        return False


def command_deliver_steers(args: argparse.Namespace) -> int:
    print(json.dumps(deliver_queued_steers(getattr(args, "task_id", None))))
    return 0


def permission_steer(text: str) -> str:
    """"allow", "deny", or "auto-allow" when the whole steer is that one word (a permission
    answer). auto-allow is CoS's own allow for an own-task-files prompt."""
    word = re.sub(r"[\s.!'\"`]+", " ", text).strip().lower()
    return word if word in {"allow", "deny", "auto-allow"} else ""


def command_permission_steer(args: argparse.Namespace, row: dict[str, Any], decision: dict[str, Any], slug: str,
                             answer: str) -> int:
    """A worker blocked on a permission prompt reaches CoS
    (permission_relay.py), and CoS answers with `--steer <rt-id> allow|deny`. The steer
    presses the dialog in the worker's tab instead of sending text: Enter on "1. Yes"
    for allow, Escape for deny, only when the screen shows the dialog."""
    import permission_relay
    ref = str(row.get("workspace_ref") or decision.get("workspace_ref") or "")
    lead = task_lead(args.task_id, slug)
    if decision["action"] == "continuation" or not ref:
        print(json.dumps({"status": "no-prompt", "delivered": False, "task_id": args.task_id, "workstream": slug,
                          "message": f"{lead} has no live tab ({row.get('status')}); there is no prompt to {answer}"}))
        return 0
    auto = answer == "auto-allow"
    result = permission_relay.answer("allow" if auto else answer, ref=ref, task_id=args.task_id, auto=auto)
    log(f"permission steer {answer} on {args.task_id} ({ref}): {result.get('status')} {result.get('reason') or ''}".strip())
    status = str(result.get("status") or "failed")
    if status == "answered" and auto:
        message = f"{lead} permission prompt auto-allowed in {ref} (own task files). Tell the owner: {result.get('report_line')}"
    elif status == "answered":
        verb = "allowed" if answer == "allow" else "denied (the worker's turn stops; steer what to do instead)"
        message = f"{lead} permission prompt {verb} in {ref}"
    else:
        message = f"{lead} {answer} not pressed: {result.get('reason') or status}"
    print(json.dumps({"status": status, "delivered": status == "answered", "task_id": args.task_id, "workstream": slug,
                      "workspace_ref": ref, "permission": answer, "message": message,
                      **({"report_line": result["report_line"]} if result.get("report_line") else {})}))
    return 0


def mark_worker_question_answered(task_id: str, answer: str) -> None:
    """A delivered steer on a row with an open ask-cos.sh question answers the newest
    one (worker questions come to CoS, answers go back as a steer)."""
    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        open_q = [q for q in (row or {}).get("worker_questions") or [] if isinstance(q, dict) and not q.get("answered")]
        if not open_q:
            return None
        open_q[-1].update(answered=True, answered_at=ledger.now_iso(), answer=answer[:500])
        return dict(open_q[-1])
    try:
        if ledger.locked_mutation(mutate):
            import owner_action_capture
            owner_action_capture.spawn_sync(task_id)
    except Exception:  # noqa: BLE001  (bookkeeping never blocks a steer)
        pass


def note_owner_action_steer(task_id: str, text: str) -> None:
    """A steer answers the owner's open ruling to-do on this task (action items live
    on the to-do list). Detached; bookkeeping never blocks a steer."""
    try:
        import owner_action_capture
        owner_action_capture.note_steer(task_id, text)
    except Exception:  # noqa: BLE001
        pass


def command_steer(args: argparse.Namespace) -> int:
    text = " ".join(args.prompt).strip()
    if not text:
        print(json.dumps({"status": "rejected", "reason": "empty steer"}))
        return 2
    row = ledger.find_task(ledger.load_payload(), args.task_id)
    agents = ledger.live_agents()
    workspaces = cmux_workspaces(args.task_id) if row else None
    if row and row.get("status") in {"routing", "active"} and row.get("worker_id") and not row.get("executor") and workspaces:
        row = heal_row_ref(row, workspaces, agents)
    decision = steer_decision(row, agents, workspaces)
    if decision["action"] == "message" and decision.get("agent") == "claude" and row and row.get("worker_id") and workspaces \
            and resolve_live_tab(row, workspaces, agents) is None and not getattr(args, "no_fallback_dispatch", False):
        # No tab holds this worker: it died and the sweep redispatches it. A message has
        # nowhere to go ("workspace not found"), so the steer waits for the next attempt.
        decision = {**decision, "action": "queue", "thread_live": False,
                    "reason": f"no open tab holds this worker ({row.get('workspace_ref')}); queued on {args.task_id} for its next attempt"}
    if decision["action"] == "unknown-task":
        print(json.dumps({"status": "failed", "task_id": args.task_id, "reason": "no ledger row with that id"}))
        return 1
    slug = str((row or {}).get("workstream") or "")
    permission = permission_steer(text)
    if permission:
        return command_permission_steer(args, row or {}, decision, slug, permission)
    if decision["action"] == "queue" and getattr(args, "no_fallback_dispatch", False):
        # Opt-out (--no-fallback-dispatch): neither queue nor spawn, for a caller that
        # will message the thread itself once the launcher binds one.
        print(json.dumps({"status": "message", "task_id": args.task_id, "workstream": slug, "steer": decision,
                          "thread_id": "", "workspace_ref": decision["workspace_ref"], "thread_live": False,
                          "message": f"{task_lead(args.task_id, slug)} still starting ({decision['status']}, no thread "
                                     f"bound yet); retry the steer in about 30 s (fallback dispatch disabled)"}))
        return 0
    if decision["action"] == "message":
        # Claude worker: Amp
        # surfaces had send_thread_message; a Claude surface has no native thread
        # tool, so dispatch.py delivers the steer itself through cmux's agent hooks
        # (an idle session wakes on it, a busy one reads it at its next step, never
        # through the composer). An Amp row keeps the native-message reply.
        sent: dict[str, Any] = {}
        if decision.get("agent") == "claude" and decision["workspace_ref"] and not getattr(args, "no_deliver", False):
            sent = cmux_agent_message(decision["workspace_ref"], steer_message(args.task_id, args.origin, text),
                                      sender=f"switchboard steer ({args.origin})")
            if sent.get("ok"):
                log(f"steer on {args.task_id} delivered to {decision['workspace_ref']} by cmux agent message {sent.get('id')}")
                mark_worker_question_answered(args.task_id, text)
                note_owner_action_steer(args.task_id, text)
                print(json.dumps({"status": "delivered", "delivered": True, "task_id": args.task_id, "workstream": slug,
                                  "steer": decision, "thread_id": decision["thread_id"],
                                  "workspace_ref": decision["workspace_ref"], "thread_live": True,
                                  "message_id": sent.get("id"),
                                  "message": f"{task_lead(args.task_id, slug)} steer delivered to the running worker "
                                             f"({decision['workspace_ref']}); it picks it up at its next step"}))
                return 0
            log(f"steer on {args.task_id}: cmux agent message to {decision['workspace_ref']} refused ({sent.get('reason')})")
            # A timeout may come after cmux queued the message: never send the steer a
            # second time (supplemental worker or a typed copy) on an ambiguous result.
            ambiguous = bool(sent.get("ambiguous")) or "timed out" in str(sent.get("reason") or "")
            if ambiguous:
                print(json.dumps({"status": "message", "delivered": False, "ambiguous": True, "task_id": args.task_id,
                                  "workstream": slug, "steer": decision, "thread_id": decision["thread_id"],
                                  "workspace_ref": decision["workspace_ref"], "thread_live": decision["thread_live"],
                                  "delivery_error": sent.get("reason"),
                                  "message": f"{task_lead(args.task_id, slug)} steer may already be queued "
                                             f"({decision['workspace_ref']}: cmux timed out); check the tab before "
                                             f"resending"}))
                return 0
            if steer_not_started(row or {}) and not getattr(args, "no_fallback_dispatch", False):
                # The session has not registered with cmux yet ("recipient runs no agent
                # that reads messages" during startup): same as no thread, so the steer is
                # queued, never a second worker.
                decision = {**decision, "action": "queue",
                            "reason": f"cmux agent message refused ({sent.get('reason')}) while the worker is starting "
                                      f"({decision['status']}); queued on {args.task_id}"}
        if decision["action"] == "message":
            if not sent:   # an Amp row: the surface sends it natively. A refused send answers nothing.
                note_owner_action_steer(args.task_id, text)
            where = f"live in {decision['workspace_ref']}" if decision["thread_live"] else "not live in cmux right now"
            how = (f"send it on stdin: `cmux agent message {decision['workspace_ref']} --from CoS -- - <<'STEER'` "
                   f"then the text and a closing STEER line"
                   if decision.get("agent") == "claude" else f"send the steer to thread {decision['thread_id']} by native message")
            print(json.dumps({"status": "message", "delivered": False, "task_id": args.task_id, "workstream": slug,
                              "steer": decision, "thread_id": decision["thread_id"],
                              "workspace_ref": decision["workspace_ref"], "thread_live": decision["thread_live"],
                              **({"delivery_error": sent.get("reason")} if sent else {}),
                              "message": f"{task_lead(args.task_id, slug)} {where}; {how}"}))
            return 0
    if decision["action"] == "queue":
        # The worker has not started (no thread, or cmux has no message hooks for it yet).
        # Queue the steer against this task and deliver it in order
        # when the worker acks or heartbeats; never a second worker on the same work.
        queued = queue_steer(args.task_id, args.origin, text)
        if queued is not None:
            name = steer_worker_name(slug)
            log(f"steer on not-started row {args.task_id} ({decision['status']}) queued as {queued['entry']['id']}")
            note_owner_action_steer(args.task_id, text)
            # The worker may have acked between the decision and the queue write.
            spawn_steer_delivery(args.task_id)
            print(json.dumps({"status": "steer-queued", "delivered": False, "queued": True, "task_id": args.task_id,
                              "workstream": slug, "steer": decision, "steer_id": queued["entry"]["id"],
                              "queue_length": queued["queued"], "thread_id": decision.get("thread_id") or "",
                              "workspace_ref": decision.get("workspace_ref") or "", "thread_live": False,
                              "timeout_minutes": steer_queue_timeout(config()),
                              "message": f"steer queued for {name} worker ({args.task_id}); delivers when it starts"}))
            return 0
        # The row went terminal between the decision and the write: continue it instead.
        row = ledger.find_task(ledger.load_payload(), args.task_id) or {}
        decision = {**decision, "action": "continuation", "status": str(row.get("status") or decision["status"])}
    return steer_terminal_row(args, row or {}, decision, slug, text)


def run_captured(func: Any, ns: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Run a command_* function and return (exit code, its last JSON line)."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = func(ns)
    lines = out.getvalue().strip().splitlines()
    try:
        return code, (json.loads(lines[-1]) if lines else {})
    except ValueError:
        return code, {"status": "failed", "reason": lines[-1][:300] if lines else "no output"}


def steer_terminal_row(args: argparse.Namespace, row: dict[str, Any], decision: dict[str, Any], slug: str,
                       text: str) -> int:
    """A steer on a finished row. In order: another row of its chain
    that is still live takes the steer (race guard); --record-only records the ruling and
    starts nothing; otherwise one continuation starts, chained onto the newest continuation
    when that one just finished, and forking the freshest finished session when it can.
    One steer per chain runs this at a time (steer_chain_lock)."""
    if getattr(args, "steer_redirected_from", None):
        # The redirect target ended between the race check and the queue write: the outer
        # call holds this chain's lock, so continue that target here without the guard.
        return _steer_terminal_row(args, row, decision, slug, text, guard=False)
    with steer_chain_lock(row):
        fresh = ledger.find_task(ledger.load_payload(), args.task_id) or row
        return _steer_terminal_row(args, fresh, decision, slug, text, guard=True)


REDIRECT_DELIVERED = {"steer-queued", "delivered", "message"}


def _steer_terminal_row(args: argparse.Namespace, row: dict[str, Any], decision: dict[str, Any], slug: str,
                        text: str, guard: bool) -> int:
    cfg = config()
    lead = task_lead(args.task_id, slug)
    payload = ledger.load_payload()
    chain = steer_chain_target(row, payload, cfg) if guard else None
    if chain and chain["action"] == "redirect":
        log(f"steer on terminal row {args.task_id}: {chain['reason']}; sending it there")
        ns = argparse.Namespace(**{**vars(args), "task_id": chain["task_id"], "steer_redirected_from": args.task_id})
        code, result = run_captured(command_steer, ns)
        result.update(redirected_from=args.task_id, chain=chain)
        if result.get("message") and result.get("status") in REDIRECT_DELIVERED:
            # Only a steer that reached the live row says "no new worker"; one whose target
            # ended mid-redirect started a continuation, and its own line says so.
            result["message"] = (f"{lead} had already ended ({decision['status']}) and its continuation "
                                 f"{chain['task_id']} is already {chain['status']}, so the steer went to that one, "
                                 f"no new worker: {result['message']}")
        print(json.dumps(result))
        return code
    if getattr(args, "record_only", False):
        target = row
        if chain and chain["action"] == "chain":
            # The newest continuation, finished moments ago, holds the open question and ruling to-do.
            target = ledger.find_task(payload, chain["task_id"]) or row
        return record_only_steer(args, row, target, decision, slug, text)
    parent_id, parent = args.task_id, row
    if chain and chain["action"] == "chain":
        parent_id = chain["task_id"]
        parent = ledger.find_task(payload, parent_id) or row
        log(f"steer on terminal row {args.task_id}: {chain['reason']}")
    source = freshest_session_row(parent, payload)
    plan = steer_resume_plan(source, cfg)
    log(f"steer on terminal row {args.task_id} ({decision['status']}): dispatching a continuation of {parent_id} "
        + (f"that forks the session of {source.get('id')}" if plan["resume"] else f"from a cold start ({plan.get('reason')})"))
    note_owner_action_steer(args.task_id, text)
    prompt = (f"Steer on {args.task_id} ({slug}), continued because that row is "
              f"{decision['status']} and its tab is reaped:\n\n{text}")
    if parent_id != args.task_id:
        prompt = (f"Steer on {args.task_id} ({slug}), continued from its newest continuation {parent_id}, which "
                  f"finished moments ago:\n\n{text}")
    ns = argparse.Namespace(prompt=[prompt], origin=args.origin, workstream=None, task_id=None, request_id=None,
                            delivery=None, depends_on=[], continuation_of=parent_id, force_redispatch=False,
                            retire_parent_ask=True, supplemental=False,
                            steer_resume={k: v for k, v in plan.items() if k != "reason"} if plan["resume"] else None)
    code, result = run_captured(command_dispatch, ns)
    if parent_id != args.task_id:
        retire_review_ask(args.task_id, by=f"dispatch:{args.origin}",
                          reason=f"handled in chat: continued as {result.get('task_id')}: {' '.join(text.split())[:120]}")
    result["steer"] = decision
    result["resume"] = {k: v for k, v in plan.items() if k in ("resume", "session_id", "from_task", "context_tokens", "reason")}
    if chain:
        result["chain"] = chain
    # The surface replies with this one line, so it says what the steer did on a finished task.
    if result.get("message") and result.get("status") != "failed":
        via = f" (continuing {parent_id}, which finished moments ago)" if parent_id != args.task_id else ""
        if result.get("launch") == "owner-tab":
            # An open, idle owner tab takes the work before any worker opens.
            what = f"so this steer went to the open owner tab{via}, no new worker"
        else:
            how = ("resumes its session with full context" if plan["resume"]
                   else f"starts fresh because {plan.get('reason')}")
            what = f"so this steer started a continuation that {how}{via}"
        result["message"] = f"{lead} had already ended ({decision['status']}), {what}: {result['message']}"
    print(json.dumps(result))
    return code


def record_only_steer(args: argparse.Namespace, named: dict[str, Any], target: dict[str, Any], decision: dict[str, Any],
                      slug: str, text: str) -> int:
    """--record-only on a finished row: a hold, an ack, a ruling or a
    note that needs no work is recorded and nothing is spawned. The ruling goes on the
    target row (the named row, or its newest continuation when that one finished moments
    ago) and under the owner decisions heading of the workstream's CONTEXT.md on main.
    Only once it is recorded are the open worker questions answered, the owner's ruling
    to-dos closed and the CoS Review asks retired, on the target and the named row."""
    tid = str(target.get("id") or args.task_id)
    ok, ledger_out, context = record_ruling(target, text, f"the owner via {args.origin} steer")
    if ok:
        for row_id in dict.fromkeys([tid, args.task_id]):
            mark_worker_question_answered(row_id, text)
            note_owner_action_steer(row_id, text)
            retire_review_ask(row_id, by=f"dispatch:{args.origin}",
                              reason=f"handled in chat: recorded, no worker: {' '.join(text.split())[:120]}")
    log(f"record-only steer on terminal row {args.task_id} ({decision['status']}): "
        f"{'recorded on ' + tid if ok else 'not recorded'}, nothing spawned "
        f"(context {'recorded' if context.get('recorded') else context.get('reason')})")
    lead = task_lead(args.task_id, slug)
    on = "the row" if tid == args.task_id else f"its newest continuation {tid}"
    where = (f"on {on} and in the {steer_worker_name(slug)} CONTEXT" if context.get("recorded")
             else f"on {on} ({context.get('reason') or 'CONTEXT not written'})")
    message = (f"{lead} had already ended ({decision['status']}); steer recorded {where}, no worker started" if ok
               else f"{lead} steer not recorded: {ledger_out.get('error') or 'ledger annotate failed'}; no worker started")
    print(json.dumps({"status": "recorded" if ok else "failed", "delivered": False, "recorded": ok, "spawned": False,
                      "task_id": args.task_id, "recorded_on": tid, "workstream": slug, "steer": decision,
                      "ledger": ledger_out, "context": context, "message": message}))
    return 0 if ok else 1


def command_release(args: argparse.Namespace) -> int:
    LOCKS.mkdir(parents=True, exist_ok=True)
    with (LOCKS / "release.lock").open("w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)  # one release at a time (several post-report hooks can fire together)
        return release_queued(config())


def release_queued(cfg: dict[str, Any]) -> int:
    started, held, superseded = [], [], []
    pass_memory: dict[str, Any] | None = None
    at = now()
    # First in line first (queue_rank): the ledger's file order is not creation order once
    # rows are requeued, and owner asks rank before switchboard's own fixes.
    queued = sorted((t for t in ledger.load_payload()["tasks"] if t.get("status") == "queued"),
                    key=lambda t: queue_rank(t, cfg, at))
    for task in queued:
        payload = ledger.load_payload()
        heir = continuation_heir(task, payload)
        if heir is not None:   # never launch a queued row whose continuation owns its work
            if retire_superseded(task, heir):
                superseded.append({"task_id": task["id"], "superseded_by": heir["id"]})
            continue
        dep = unmet_dependency(task, payload)
        if dep is not None:
            held.append({"task_id": task["id"], "waiting_on": dep["id"]})
            continue
        failed = failed_dependency(task, payload)
        if failed:
            failed_dep, ended = failed
            through = "" if ended["id"] == failed_dep else f" (through its continuation {ended['id']})"
            run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", task["id"], "--status", "blocked",
                 "--summary", f"blocked: prerequisite {failed_dep}{through} ended {ended['status']}"], timeout=30)

            def stamp_prerequisite(p, tid=task["id"], dep_id=failed_dep):
                row = ledger.find_task(p, tid)
                if row is None or row.get("status") != "blocked":
                    return None
                row["blocked_on_prerequisite"] = dep_id   # follow_replaced_prerequisites() reads it
                return dict(row)

            ledger.locked_mutation(stamp_prerequisite)
            notify(task["id"], f"blocked: prerequisite {failed_dep} did not finish", status="blocked")
            continue
        limited = usage_limit_until()
        if limited is not None and str(task.get("agent") or agent_kind()) == "claude":
            # A Claude worker launched now would stop on its first turn.
            held.append({"task_id": task["id"], "waiting_on": f"usage limit until {usage_limits.pacific(limited)}"})
            continue
        quiet = [] if limits_disabled() else (usage_limit_stamp().get("quiet_waiting") or [])
        if quiet and task["id"] not in quiet:
            # A paused quiet-window task (a history rewrite) resumes only with no other worker live;
            # a fresh launch would starve it.
            held.append({"task_id": task["id"], "waiting_on": f"quiet window for {', '.join(quiet)}"})
            continue
        dated = launch_hold(task, cfg, at)
        if dated is not None:
            held.append({"task_id": task["id"], "waiting_on": launch_hold_text(dated)})
            continue
        if pass_memory is None:
            pass_memory = live_memory(cfg) or {}   # one reading (and at most one spawn probe) per pass
        if at_capacity(len(live_workers(payload)), cfg, memory=pass_memory):
            held.append({"task_id": task["id"], "waiting_on": "capacity"})
            continue
        try:
            row = launch(task, cfg)
            if row is None:
                continue  # another launcher already holds this row
            started.append(row["id"])
            notify(row["id"], "started")
        except LaunchHold as error:
            if hold_launch(task["id"], error) is None:
                restore_resume_pointer(task["id"])
                run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", task["id"], "--status", "failed",
                     "--summary", f"failed: {error}"], timeout=30)
                notify(task["id"], f"failed: {str(error)[:200]}", status="failed")
                continue
            held.append({"task_id": task["id"], "waiting_on": "multiplexer"})
            if error.unreachable:
                break   # the same mux answers the next row: one try per pass
        except Exception as error:
            restore_resume_pointer(task["id"])
            run([sys.executable, str(SCRIPTS / "running-task-state.py"), "update", "--task-id", task["id"], "--status", "failed",
                 "--summary", f"failed: {error}"], timeout=30)
            notify(task["id"], f"failed: {str(error)[:200]}", status="failed")
    print(json.dumps({"started": started, "held": held, **({"superseded": superseded} if superseded else {})}))
    return 0


# --- finish / reap / sweep ------------------------------------------------------------

def spawn_skill_sync() -> str:
    """Start sync-skills.py detached once a landing has released the lock (skills system
    design step 3). Best effort: a skill edit that lands
    reaches ~/.claude/skills in seconds, and the command-center tick covers a miss. Never in
    a test sandbox or against a redirected state dir, which is a fixture."""
    script = REPO_ROOT / "system" / "scripts" / "sync-skills.py"
    if (os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1" or os.environ.get("SKILL_SYNC_DISABLED") == "1"
            or STATE_DIR != REPO_ROOT / "system" / "state" or not script.exists()):
        return "skipped"
    log_path = STATE_DIR / "logs" / "skill-sync.log"
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as out:
            subprocess.Popen([sys.executable, str(script), "--reason", "landing", "--notify"],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), env=child_env,
                             close_fds=True, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
        return "started"
    except OSError as error:
        log(f"skill sync not started after landing ({error}); the next tick runs it")
        return "failed"


def command_finish(args: argparse.Namespace) -> int:
    task = ledger.find_task(ledger.load_payload(), args.task_id)
    if task is None:
        print(json.dumps({"landed": False, "reason": f"no task {args.task_id}"}))
        return 2
    with landing_lock():
        outcome = finish_branch(task, allow_mass_delete=bool(getattr(args, "allow_mass_delete", False)),
                                accept_main_edits=bool(getattr(args, "accept_main_edits", False)))
    release_locks(args.task_id)
    if outcome.get("landed"):
        outcome["skill_sync"] = spawn_skill_sync()
    if outcome.get("landed") or outcome.get("skipped"):
        grace_report: dict[str, str] = {}

        def record(payload):
            row = ledger.find_task(payload, args.task_id) or {}
            row.update(landed_commit=outcome.get("commit"), updated_at=ledger.now_iso())
            # An earlier finish failed to land and report-to-cos.sh labelled the row
            # failed while keeping the worker's result. Now that the branch is on
            # main the task is done; the row is corrected and reports once more so
            # the owner hears the deliverable, not the stale landing failure.
            worker_result = landing_failure_result(row)
            if outcome.get("landed") and worker_result is not None:
                row.update(status="done", summary=worker_result, finished_at=ledger.now_iso(), report_delivered=False)
                for key in ("report_delivery_unknown", "report_attempted_at", "admission_blocked", "blocked_kind"):
                    row.pop(key, None)
                outcome["status_corrected"] = "done"
            # The branch landed inside its rebase-conflict grace window: the row
            # was held active with the worker's report stored, so it becomes done
            # with that report as its one terminal line. No failed report was ever
            # delivered, so none needs correcting.
            landing = row.pop("landing", None)
            if outcome.get("landed") and (landing or {}).get("state") == "conflict" \
                    and row.get("status") in {"routing", "active"} and row.get("report"):
                row.update(status="done", summary=row["report"], finished_at=ledger.now_iso(), report_delivered=False)
                for key in ("report_delivery_unknown", "report_attempted_at", "admission_blocked", "blocked_kind"):
                    row.pop(key, None)
                outcome["status_corrected"] = "done"
                outcome["landed_in_grace"] = True
                grace_report["text"] = row["report"]
        ledger.locked_mutation(record)
        if grace_report:
            # Land, record, reap: the close-out line first (idempotent, so the
            # worker's own report-to-cos.sh run that follows records nothing twice),
            # then the one done line. report-to-cos.sh sees the row done with
            # report_delivered=false and sends it once; its outer run after this
            # finish finds it delivered and sends nothing.
            row = ledger.find_task(ledger.load_payload(), args.task_id) or task
            record_on_main(row, "done", grace_report["text"], str(row.get("thread_id") or ""))
            outcome["report"] = notify(args.task_id, grace_report["text"], status="done")
    elif outcome.get("rebase_conflict") and conflict_grace_seconds(config()) > 0:
        outcome.update(open_conflict_window(args.task_id, outcome, config()))
    print(json.dumps({"task_id": args.task_id, **outcome}))
    return 0 if outcome.get("landed") or outcome.get("skipped") else 1


# --- rebase-conflict grace window ---------------------------------------------------
#
# A real rebase conflict at finish does not deliver "failed: could not land on main"
# at once: workers often resolve their conflicts and land within minutes, and a
# land-this continuation dispatched meanwhile finds nothing to do. The row stays active with landing = {state: conflict, since, until,
# reason, files, worktree, told} for dispatch.conflict_grace_seconds; the worker
# hears once in its tab that the conflict is its own to resolve; reap skips the
# non-terminal row so the tab stays open; steer takes the live-row message path.
# A landing inside the window (command_finish, or the sweep noticing git) delivers
# the worker's own done report exactly once. When the window lapses the sweep
# delivers the failed report in its usual shape and records the close-out line.

def conflict_grace_seconds(cfg: dict[str, Any]) -> int:
    try:
        return max(0, int(cfg.get("conflict_grace_seconds", DEFAULTS["conflict_grace_seconds"])))
    except (TypeError, ValueError):
        return int(DEFAULTS["conflict_grace_seconds"])


def open_conflict_window(task_id: str, outcome: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Hold the row active with landing.state == "conflict" (a rerun keeps the
    original `since`, so a worker cannot extend its window by rerunning finish),
    and tell the worker once in its tab what to do."""
    grace = conflict_grace_seconds(cfg)
    files = [f for f in outcome.get("files") or [] if f]
    state: dict[str, Any] = {}

    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is None or row.get("status") in ledger.TERMINAL_STATUSES:
            return
        landing = row.get("landing") if isinstance(row.get("landing"), dict) else {}
        since = ledger.parse_iso(str(landing.get("since") or "")) if landing.get("state") == "conflict" else None
        since = since or now()
        landing = {"state": "conflict", "since": since.isoformat(), "until": (since + timedelta(seconds=grace)).isoformat(),
                   "reason": outcome.get("reason"), "files": files, "worktree": row.get("worktree"),
                   "told": bool(landing.get("told"))}
        row.update(landing=landing, updated_at=ledger.now_iso())
        state.update(landing)
        state["workspace_ref"] = row.get("workspace_ref") or ""
        state["branch"] = row.get("branch") or ""
    ledger.locked_mutation(mutate)
    if not state:
        return {}
    told = bool(state.get("told"))
    if not told and str(state.get("workspace_ref") or "").startswith("workspace:"):
        shown = ", ".join(files[:5]) + ("…" if len(files) > 5 else "") or "see git status"
        text = (f"{task_id} landing conflict: rebase of {state['branch']} onto main conflicts ({shown}). "
                f"It is yours to resolve in {state.get('worktree')}: rebase onto main, fix the conflicts, commit, then rerun "
                f"report-to-cos.sh --status done (or python3 system/scripts/dispatch.py finish --task-id {task_id}) "
                f"within {max(1, grace // 60)} min; after that the task is reported failed to the origin.")
        if tab_send(state["workspace_ref"], text):
            told = True
            ledger.locked_mutation(lambda p: ((ledger.find_task(p, task_id) or {}).get("landing") or {}).update(told=True))
    return {"grace": True, "landing": "conflict", "grace_since": state.get("since"), "grace_until": state.get("until"),
            "worker_told": told}


def expire_conflict_windows(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Sweep pass: settle every non-terminal row in landing.state == "conflict".
    Landed meanwhile (the worker ran git by hand, or finish ran from a thread
    whose notify failed) -> done with the stored report, one done line. Window
    lapsed -> failed in the shape report-to-cos.sh uses for a landing failure,
    close-out line recorded, one failed line. Nothing re-runs finish_branch on a
    lapse: the worker may be mid-rebase in that worktree and finish would commit
    its half-resolved tree."""
    landed, failed, open_ = [], [], []
    for task in ledger.load_payload()["tasks"]:
        landing = task.get("landing") if isinstance(task.get("landing"), dict) else None
        if not landing or landing.get("state") != "conflict" or task.get("status") in ledger.TERMINAL_STATUSES:
            continue
        task_id = task["id"]
        report = str(task.get("report") or task.get("summary") or "").strip()
        landed_tip = branch_tip_on_main(task)
        if ledger.git_state(task) == "landed" or landed_tip:
            def flip_done(payload, tid=task_id, text=report, tip=landed_tip):
                row = ledger.find_task(payload, tid)
                if row is None or row.get("status") in ledger.TERMINAL_STATUSES:
                    return
                row.pop("landing", None)
                row.update(status="done", summary=text or "done", finished_at=ledger.now_iso(), report_delivered=False,
                           updated_at=ledger.now_iso())
                if tip and not row.get("landed_commit"):
                    row["landed_commit"] = tip
                for key in ("report_delivery_unknown", "report_attempted_at", "admission_blocked", "blocked_kind"):
                    row.pop(key, None)
            ledger.locked_mutation(flip_done)
            record_on_main(task, "done", report or "done", str(task.get("thread_id") or ""))
            notify(task_id, report or "done", status="done")
            landed.append(task_id)
            continue
        until = ledger.parse_iso(str(landing.get("until") or ""))
        if until is None:
            since = ledger.parse_iso(str(landing.get("since") or "")) or now()
            until = since + timedelta(seconds=conflict_grace_seconds(cfg))
        if now() < until:
            open_.append(task_id)
            continue
        summary = f"failed: could not land on main ({landing.get('reason') or 'rebase conflict'}). Worker result stands: {report or '(no report)'}"

        def flip_failed(payload, tid=task_id, text=summary):
            row = ledger.find_task(payload, tid)
            if row is None or row.get("status") in ledger.TERMINAL_STATUSES:
                return
            row.pop("landing", None)
            row.update(status="failed", summary=text, finished_at=ledger.now_iso(), report_delivered=False,
                       updated_at=ledger.now_iso())
            for key in ("report_delivery_unknown", "report_attempted_at", "admission_blocked", "blocked_kind"):
                row.pop(key, None)
        ledger.locked_mutation(flip_failed)
        record_on_main(task, "failed", summary, task.get("thread_id"))
        notify(task_id, summary, status="failed")
        failed.append(task_id)
    return {"landed": landed, "failed": failed, "open": open_}


def branch_tip_on_main(task: dict[str, Any]) -> str:
    """The branch tip (short sha) once main contains it: the worker resolved its
    conflict and landed by hand (git rebase, then a fast-forward of main) without
    going through finish, so no landed_commit is recorded. "" when the branch is
    missing or still unmerged. A row only reaches landing.state == "conflict" with
    commits of its own, so an ancestor tip here is a landing, not an empty branch."""
    branch = task.get("branch")
    if not branch or git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode:
        return ""
    if not on_main(branch):
        return ""
    return git("rev-parse", "--short", branch, check=False).stdout.strip()


LANDING_FAILURE = re.compile(r"^failed: .*?\. Worker result (?:stands|was): (?P<result>.+)$", re.S)


def landing_failure_result(task: dict[str, Any]) -> str | None:
    """The worker's own result if the row's terminal status came from a landing
    failure (report-to-cos.sh keeps it after 'Worker result stands:'), else None."""
    if task.get("status") not in {"failed", "blocked"}:
        return None
    match = LANDING_FAILURE.match(str(task.get("summary") or "").strip())
    return match.group("result").strip() if match else None


def protected(ws: dict[str, Any] | None, task: dict[str, Any] | None = None) -> str:
    """Why this tab must not be closed, or "" when it may. Protection is by
    identity, never by ref: the reaper closes a tab only when worker_tab_reason
    says it is this row's worker tab (see _undescribed_tab_reason: a slot title
    alone proves nothing, because slots are reused), so CoS, Launchpad, the fixed surfaces and the owner's owner tabs are never closed
    whatever ref they hold. A ref list would go stale on every cmux restart because
    refs are renumbered. A row
    whose ref no longer resolves has nothing left to protect, only a worktree
    and a ledger row to settle."""
    if ws is None:
        return ""
    return worker_tab_reason(ws, task)


def unlanded_work(task: dict[str, Any]) -> str:
    """What a terminal row's checkout still holds that main does not: 'commits',
    'uncommitted edits', both, or "" when dropping it loses nothing. finish_branch
    leaves a conflicting branch in place "for a human or a follow-up task"; the
    reaper must not then delete it along with the tab."""
    # A row without `worktree` (launched before the field was recorded) must
    # resolve to its own checkout, never to Path("") = main, whose dirty tree
    # would read as "uncommitted edits" and keep its worktree forever.
    path = Path(task.get("worktree") or worktree_path(task["id"]))
    branch = task.get("branch")
    parts = []
    if branch and git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0 \
            and not on_main(branch):
        parts.append("commits")
    if path.is_dir() and (path / ".git").exists():
        status = git("status", "--porcelain", NO_SUBMODULES, "--", ".", ":(exclude)system/state", cwd=path, check=False)
        # A failed read is not a clean checkout: the sweep would drop it.
        if status.returncode or any(not _shared_state_link(path, line) for line in status.stdout.splitlines()):
            parts.append("uncommitted edits")
    return " and ".join(parts)


def _shared_state_link(worktree: Path, status_line: str) -> bool:
    """An untracked symlink that link_shared_state() pointed into main is not the
    worker's work (a dir-only ignore like `foo/` does not cover the symlink)."""
    if not status_line.startswith("??"):
        return False
    entry = worktree / status_line[3:].strip().rstrip("/")
    if winlink.points_into(entry, REPO_ROOT):
        return True
    # A hard link (Windows without Developer Mode) is another name for main's file. Main
    # itself is never a worktree: every one of its files is "the same file as main's".
    if os.path.realpath(worktree) == os.path.realpath(REPO_ROOT):
        return False
    rel = status_line[3:].strip().rstrip("/")
    return entry.is_file() and not winlink.is_link(entry) and winlink.same_file(entry, REPO_ROOT / rel)


_KILL_TASK_PROCESSES_PS = r"""
$id = ([string]$env:SWITCHBOARD_KILL_TASK).ToLowerInvariant()
$markers = @('report-waiter.sh', 'report-to-cos.sh', "/worktrees/$id", 'launch-prompts/')
$names = @('bash.exe', 'sh.exe', 'python.exe', 'python3.exe', 'claude.exe', 'node.exe', 'sleep.exe')
$all = @{}; Get-CimInstance Win32_Process | ForEach-Object { $all[[int]$_.ProcessId] = $_ }
$keep = @{}; $p = $PID
while ($p -and $all.ContainsKey($p) -and -not $keep.ContainsKey($p)) { $keep[$p] = 1; $p = [int]$all[$p].ParentProcessId }
function Norm($proc) { ([string]$proc.CommandLine).Replace('\', '/').ToLowerInvariant() }
function Spared($proc) {
  $cl = Norm $proc
  $keep.ContainsKey([int]$proc.ProcessId) -or (-not ($names -contains ([string]$proc.Name).ToLowerInvariant())) -or $cl.Contains('dispatch.py') -or $cl.Contains('router-dispatch.sh')
}
$kids = @{}
foreach ($proc in $all.Values) {
  $pp = [int]$proc.ParentProcessId
  if ($all.ContainsKey($pp) -and $proc.CreationDate -gt $all[$pp].CreationDate) {
    if (-not $kids.ContainsKey($pp)) { $kids[$pp] = New-Object System.Collections.ArrayList }
    [void]$kids[$pp].Add([int]$proc.ProcessId)
  }
}
$roots = @(foreach ($proc in $all.Values) {
  if (Spared $proc) { continue }
  $cl = Norm $proc
  if (-not $id -or -not $cl.Contains($id)) { continue }
  if (@($markers | Where-Object { $cl.Contains($_) }).Count -gt 0) { [int]$proc.ProcessId }
})
$victims = @{}; $stack = New-Object System.Collections.Stack
foreach ($r in $roots) { $stack.Push($r) }
while ($stack.Count -gt 0) {
  $x = $stack.Pop()
  if ($victims.ContainsKey($x) -or -not $all.ContainsKey($x) -or (Spared $all[$x])) { continue }
  $victims[$x] = 1
  if ($kids.ContainsKey($x)) { foreach ($k in $kids[$x]) { $stack.Push($k) } }
}
$killed = 0
if ($env:SWITCHBOARD_KILL_DRYRUN -eq '1') { "victims: " + (($victims.Keys | Sort-Object) -join ',') }
else { foreach ($v in @($victims.Keys)) { try { Stop-Process -Id $v -Force -ErrorAction Stop; $killed++ } catch {} } }
"{""matched"": $($roots.Count), ""killed"": $killed}"
"""


def kill_task_processes(task: dict[str, Any]) -> dict[str, Any]:
    """Windows: stop the processes a finished worker left behind. Closing the
    wmux tab ends the pane shell, but detached children (report waiters,
    report-to-cos replays, test servers, sleep loops) can live on for hours and keep
    forking, while each exited process leaks kernel memory.

    A root is a bash, sh, python, claude, node or sleep process whose command
    line names the task id together with report-waiter.sh, report-to-cos.sh,
    the task's own worktree path, or a launch prompt. Never this process or its
    ancestors, never anything running dispatch.py or router-dispatch.sh (the
    post-report release hook, a CoS steer or continuation), and never another
    program (an editor open on a worktree file). Descendants are found from one
    process snapshot, a child only when it was created after its parent, so a
    reused PID is never followed. One PowerShell call, best effort, never raises.
    Elsewhere a no-op (cmux closes the session's process group)."""
    if not _on_windows():
        return {"matched": 0, "killed": 0, "skipped": "not windows or test sandbox"}
    task_id = str(task.get("id") or "").lower()
    if not re.fullmatch(r"rt-[0-9a-f]{8,}", task_id):
        return {"matched": 0, "killed": 0, "skipped": "not a worker task id"}
    try:
        out = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _KILL_TASK_PROCESSES_PS],
                  timeout=120, env={**os.environ, "SWITCHBOARD_KILL_TASK": task_id})
        return json.loads((out.stdout or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        return {"matched": 0, "killed": 0, "error": str(error)[:200]}


def safe_to_kill_leftovers(task: dict[str, Any], agents: dict[str, Any]) -> bool:
    """Re-read the row just before the kill: it must still be terminal on the same
    attempt (a --force-redispatch reuses the id), the agent store must answer,
    and the worker's thread must not be running."""
    fresh = ledger.find_task(ledger.load_payload(), task["id"]) or {}
    if fresh.get("status") not in ledger.TERMINAL_STATUSES or fresh.get("attempt_id") != task.get("attempt_id"):
        return False
    live = ledger.live_agents()
    if not live and not agents:
        return False
    agent = live.get(str(fresh.get("thread_id") or "")) or {}
    return not agent or agent.get("state") in IDLE_STATES


def reap_close_refusal(task: dict[str, Any], ws: dict[str, Any]) -> str:
    """Why this tab must stay open after a fresh read, or "" when reap may close it."""
    workspaces = cmux_workspaces(task["id"])
    if not workspaces:
        return "tab list unreadable"
    fresh = next((w for w in workspaces if w.get("id") == ws.get("id")), None)
    if fresh is None:
        return "tab already gone"
    why = worker_tab_reason(fresh, task)
    if why:
        return why
    for other in ledger.load_payload()["tasks"]:
        if other.get("id") == task["id"] or other.get("status") in ledger.TERMINAL_STATUSES:
            continue
        if other.get("workspace_ref") == fresh.get("ref") or fresh.get("description") == worker_description(other["id"]):
            return f"tab is held by live row {other['id']}"
    return ""


def reap_all(cfg: dict[str, Any], only: str | None = None) -> dict[str, Any]:
    keep_open: set[str] = set()
    try:
        keep_open = set((json.loads((STATE_DIR / "worker-dispatch" / "keep-open.json").read_text()) or {}).get("task_ids") or [])
    except (OSError, ValueError):
        pass
    agents = ledger.live_agents()
    grace = timedelta(seconds=int(cfg["reap_grace_seconds"]))
    closed, skipped = [], []
    workspaces = cmux_workspaces()
    for task in ledger.load_payload()["tasks"]:
        if only and task["id"] != only:
            continue
        if task.get("status") not in ledger.TERMINAL_STATUSES:
            continue
        # The tab is found by ref, else by its description / id (a worker that
        # reported its UUID as --workspace-ref), else by title. A row with no
        # tab anywhere (never launched, or its tab already closed) only settles
        # its checkout. A row already stamped reaped_at is finished unless its
        # tab is still open: every legacy row (title stored as workspace_ref)
        # was settled that way with the tab left behind, so it gets the full
        # record-then-close pass the first time its tab is found.
        ws = worker_tab_for(task, workspaces)
        if task.get("reaped_at") and ws is None:
            continue
        ref = str((ws or {}).get("ref") or task.get("workspace_ref") or "")
        if ws is None and not ref.startswith("workspace:"):
            settle_checkout(task)
            continue
        # A cancelled row has no report to wait for: its origin ended it, and a
        # late report from the worker is ignored by design.
        # Any other row keeps its tab until its report is proven in CoS
        # (report_delivered is set only after the line is verified there), because
        # a tab reaped while a waiter still holds its report can lose part of it.
        # router.yaml dispatch.reap_requires_delivered_report: false restores the
        # older carrier rule (close once a waiter or the loop holds the report).
        carrier = None if cfg.get("reap_requires_delivered_report", True) else report_carrier(task)
        if task.get("report_delivered") is not True and task.get("status") != "cancelled" and carrier is None:
            skipped.append({"task_id": task["id"], "reason": "report not delivered yet"}); continue
        finished = ledger.parse_iso(task.get("finished_at"))
        if finished and now() - finished < grace and not only:
            skipped.append({"task_id": task["id"], "reason": "grace period"}); continue
        if task["id"] in keep_open:
            skipped.append({"task_id": task["id"], "reason": "keep-open"}); continue
        why = "owner tab ran this task; never reaped" if task.get("owner_tab") and ws is not None else protected(ws, task)
        if why:
            skipped.append({"task_id": task["id"], "reason": why}); continue
        if ws is not None and task.get("status") in {"blocked", "deferred"} and not only:
            skipped.append({"task_id": task["id"], "reason": "blocked result stays open for the owner"}); continue
        # Close-out order: land, record, then reap.
        # A tab whose branch still holds commits main lacks (a landing that
        # failed) stays open with its worktree until `dispatch.py finish
        # --task-id` lands it; a cancelled row is the origin's call and closes
        # anyway (its worktree is preserved by settle_checkout, never deleted).
        if ws is not None and task.get("status") != "cancelled" and "commits" in unlanded_work(task):
            skipped.append({"task_id": task["id"], "reason": f"branch {task.get('branch')} has not landed on main"}); continue
        agent = agents.get(task.get("thread_id") or "") or {}
        if ws is not None and agent.get("state") not in IDLE_STATES and (ws.get("id") or "") == agent.get("workspace_id") \
                and worker_idle_since(task, agent, grace, ref) is None:   # a stuck "Running" stamp must not keep the tab forever
            skipped.append({"task_id": task["id"], "reason": "agent still running"}); continue
        tenant = next((tid for tid, a in agents.items() if ws is not None and a.get("workspace_id") == (ws.get("id") or "")), "")
        if tenant and task.get("thread_id") and tenant != task["thread_id"]:
            skipped.append({"task_id": task["id"], "reason": f"tab now belongs to thread {tenant}"}); continue
        # Record before close: report-to-cos.sh already wrote this line for a
        # reported row (idempotent here); a row that reached terminal any other
        # way gets it now, so no worker tab closes without its CONTEXT.md line.
        recorded = record_on_main(task)
        if ws is not None:
            # The list above is seconds old by now (record_on_main commits). Read it again
            # and prove the tab one last time: a launcher may have opened or described a tab
            # meanwhile, and a close cannot be undone.
            refused = reap_close_refusal(task, ws)
            if refused:
                log(f"{task['id']}: reap left {ref} open: {refused}")
                skipped.append({"task_id": task["id"], "reason": refused}); continue
            ok, error = cmux_close_result(ref, str(ws.get("id") or ""), f"reap {task['id']}")
            if not ok:
                skipped.append({"task_id": task["id"], "reason": f"cmux close failed: {error}"}); continue
        release_locks(task["id"])
        entry = {"task_id": task["id"], "workspace_ref": ref, "was_open": ws is not None, "recorded": recorded.get("recorded", False)}
        leftovers = kill_task_processes(task) if safe_to_kill_leftovers(task, agents) else {}
        if leftovers.get("matched"):
            entry["leftover_processes_killed"] = leftovers.get("killed", 0)
            log(f"{task['id']}: killed {leftovers.get('killed', 0)} of {leftovers['matched']} leftover process tree(s)")
        if carrier is not None and task.get("report_delivered") is not True:
            entry["report_carried_by"] = carrier
        closed.append({**entry, **settle_checkout(task)})
    out: dict[str, Any] = {"closed": closed, "skipped": skipped}
    swept = sweep_leftovers(force=bool(closed))
    if swept.get("stopped") or swept.get("failed"):
        out["leftover_sweep"] = {"stopped": swept.get("stopped", 0), "failed": swept.get("failed", 0), "tasks": swept.get("tasks", [])}
    return out


def report_waiter_pid(task_id: str) -> int | None:
    """PID of the live report-waiter.sh for this task (report-to-cos.sh
    spawn_waiter writes it), or None when there is none or it has exited."""
    try:
        pid = int((STATE_DIR / "worker-dispatch" / f"report-waiter-{task_id}.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def report_carrier(task: dict[str, Any]) -> str | None:
    """Who delivers this terminal row's pending report once its tab is gone:
    "waiter" (a live detached report-waiter.sh replaying the original
    report-to-cos.sh call) or "loop" (command-center-loop.sh retries every
    terminal row with report_delivered False and a summary on each tick).
    None means the report still lives only in the worker tab, so reap must
    wait: no summary on the row yet (report_delivered None, or an empty
    summary the loop cannot replay) or delivery already attempted with an
    unknown outcome (settle_unknown_reports owns that; neither carrier retries
    it). Without this, done tabs sit behind "report not delivered yet" while their
    waiters already hold the report."""
    if task.get("report_delivered") is not False or task.get("report_delivery_unknown") is True:
        return None
    if not str(task.get("summary") or "").strip():
        return None
    return "waiter" if report_waiter_pid(task["id"]) is not None else "loop"


def settle_checkout(task: dict[str, Any]) -> dict[str, Any]:
    """Mark a terminal row reaped. Its worktree and branch are dropped only when
    main already has everything they hold; otherwise they stay for a human or a
    follow-up task and the row records where ("worktree_preserved")."""
    held = unlanded_work(task) if task.get("branch") else ""
    if held:
        stamp = {"reaped_at": ledger.now_iso(), "worktree_preserved": task.get("worktree") or worktree_path(task["id"]).as_posix(),
                 "worktree_holds": held}
    else:
        if task.get("branch"):
            drop_worktree(task)
        stamp = {"reaped_at": ledger.now_iso()}
    ledger.locked_mutation(lambda p, tid=task["id"]: (ledger.find_task(p, tid) or {}).update(**stamp))
    return {k: v for k, v in stamp.items() if k != "reaped_at"}


def command_reap(args: argparse.Namespace) -> int:
    print(json.dumps(reap_all(config(), args.task_id)))
    return 0


def tab_send(ref: str, text: str) -> bool:
    """One guarded line into a worker tab (never while it is typing or streaming)."""
    result = run(["bash", "-c", 'source "$1/system/scripts/lib/cmux-inject-guard.sh" && cig_safe_send "$2" "$3" 20',
                  "_", str(REPO_ROOT), ref, text], timeout=90)
    return result.returncode == 0


def tab_idle(ref: str) -> bool:
    """Two guard reads 1.3 s apart both show an idle agent with an empty composer."""
    result = run(["bash", "-c", 'source "$1/system/scripts/lib/cmux-inject-guard.sh" && cig_safe "$2" && sleep 1.3 && cig_safe "$2"',
                  "_", str(REPO_ROOT), ref], timeout=60)
    return result.returncode == 0


# cmux lifecycle values that mean no turn is in flight. During a turn the stamp is "Running" or,
# while a tool runs, the tool label ("shell_command: …"); anything outside this set is work.
IDLE_STATES = {"", "Idle", "Needs input"}


def worker_idle_since(task: dict[str, Any], agent: dict[str, Any] | None, window: timedelta, ref: str = "") -> datetime | None:
    """When the worker behind `task` stopped working, or None while it works.
    `ref` is the tab to read when the caller has resolved it (the reaper, whose
    legacy rows hold a title in workspace_ref); default is the row's own ref.

    The cmux lifecycle stamp is authoritative when it says Idle / Needs input.
    It can also stick at "Running" for over an hour after the Amp thread went
    idle, with an idle, empty screen. A stuck stamp must not
    hide a stalled worker from the resume rule, so when the stamp says anything
    else but has not changed for `window`, and the task-liveness heartbeat (one
    per tool result) is just as old, the tab's screen is the tie-breaker: two
    idle-and-empty reads mean idle since the stamp. A fresh heartbeat or a busy
    screen means working, whatever the stamp says."""
    if not agent or not agent.get("state_ts"):
        return None
    stamped = datetime.fromtimestamp(agent["state_ts"], tz=timezone.utc)
    if agent.get("state") in IDLE_STATES:
        return stamped
    if now() - stamped < window:
        return None
    heartbeat = ledger.parse_iso(task.get("heartbeat_at"))
    if heartbeat and now() - heartbeat < window:
        return None
    ref = ref or str(task.get("workspace_ref") or "")
    if not ref.startswith("workspace:") or not tab_idle(ref):
        return None
    return max(stamped, heartbeat) if heartbeat else stamped


def relay_permission_prompts() -> list[dict[str, Any]]:
    """Every live worker tab that shows a permission dialog sends CoS one 🔐 callback
    with the command (permission_relay.sweep). This covers tabs that opened before
    the Notification hook existed: Claude reads hooks.json only at session start."""
    try:
        import permission_relay
        return permission_relay.sweep(ledger.load_payload()["tasks"])
    except Exception as error:  # noqa: BLE001  (a relay failure must not stop the sweep)
        log(f"permission relay sweep failed: {error}")
        return []


def permission_prompt_relayed(ref: str) -> bool:
    try:
        import permission_relay
        return bool(ref) and permission_relay.relayed(ref)
    except Exception:  # noqa: BLE001
        return False


def nudge_idle(cfg: dict[str, Any]) -> dict[str, Any]:
    """Done is observable: a worktree task whose branch has not landed is still in
    progress however its worker ended the turn. If that worker has been idle for
    IDLE_RESUME_MINUTES, send it exactly one resume message; if it is idle that long
    again after the resume, tell the origin once ("rt-X idle 20m after resume") and stop."""
    agents = ledger.live_agents()
    window = timedelta(minutes=IDLE_RESUME_MINUTES)
    resumed, escalated = [], []
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") not in {"routing", "active"} or not task.get("branch") or task.get("resume_escalated_at"):
            continue
        if (task.get("landing") or {}).get("state") == "conflict":
            continue   # the worker already heard its landing-conflict line; the window, not an idle nudge, settles it
        if task.get("usage_limit"):
            continue   # paused on the usage limit: handle_limits resumes it after the reset
        if network_owned(task):
            continue   # stopped on a network error: handle_network_stops resumes it once the API answers
        if continuation_heir(task, ledger.load_payload()) is not None:
            continue   # a resume message would set this worker going on work its continuation owns
        agent = agents.get(task.get("thread_id") or "") or {}
        idle_since = worker_idle_since(task, agent, window)
        if idle_since is None or now() - idle_since < window or ledger.git_state(task) != "open":
            continue
        ref = task.get("workspace_ref") or ""
        if agent.get("state") == "Needs input":
            # Parked on a question only its owner can answer: a resume line would
            # queue behind the dialog unread. The origin hears it once, now.
            if permission_prompt_relayed(ref):
                # The 🔐 callback already gave CoS the command and how to answer it.
                ledger.locked_mutation(lambda p, tid=task["id"]: (ledger.find_task(p, tid) or {}).update(resume_escalated_at=ledger.now_iso()))
                escalated.append(task["id"])
                continue
            minutes = int((now() - idle_since).total_seconds() // 60)
            notify(task["id"], f"waiting on a question in its tab for {minutes}m; branch {task['branch']} still unmerged ({ref})",
                   status="active", urgent=True)
            ledger.locked_mutation(lambda p, tid=task["id"]: (ledger.find_task(p, tid) or {}).update(resume_escalated_at=ledger.now_iso()))
            escalated.append(task["id"])
            continue
        if not task.get("resume_sent_at"):
            text = (f"your task {task['id']} is not done: branch {task['branch']} has not landed on main; continue, "
                    f"or report what you are blocked on with report-to-cos.sh --status blocked")
            if ref.startswith("workspace:") and tab_send(ref, text):
                ledger.locked_mutation(lambda p, tid=task["id"]: (ledger.find_task(p, tid) or {}).update(resume_sent_at=ledger.now_iso()))
                resumed.append(task["id"])
            continue
        sent = ledger.parse_iso(task["resume_sent_at"])
        if sent and now() - max(sent, idle_since) >= window:
            minutes = int((now() - idle_since).total_seconds() // 60)
            notify(task["id"], f"idle {minutes}m after resume; branch {task['branch']} still unmerged ({ref})", status="active", urgent=True)
            ledger.locked_mutation(lambda p, tid=task["id"]: (ledger.find_task(p, tid) or {}).update(resume_escalated_at=ledger.now_iso()))
            escalated.append(task["id"])
    return {"resumed": resumed, "escalated": escalated}


# --- stray writes into main ---------------------------------------------------------------
#
# A live worktree task whose paths turn up dirty in main's working tree, written
# after the task started, is told so in its tab while it still has the context to
# tell its own stray write from a legitimate edit, instead of first hearing about
# it from a refused finish. One message per task per path; new paths get a new
# message; a message that could not be delivered (tab busy) is retried next sweep.

STRAY_WRITE_STAMPS = STATE_DIR / "worker-dispatch" / "stray-writes.json"


def flag_stray_writes() -> dict[str, Any]:
    try:
        stamps = json.loads(STRAY_WRITE_STAMPS.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        stamps = {}
    live = [t for t in ledger.load_payload()["tasks"] if t.get("status") in {"routing", "active"} and t.get("worktree")]
    stamps = {tid: paths for tid, paths in stamps.items() if any(t["id"] == tid for t in live)}
    dirty = dirty_main_paths() if live else set()
    flagged, told = [], []
    for task in live:
        strays = stray_main_writes(task, dirty=dirty) if dirty else []
        if not strays:
            continue
        flagged.append({"task_id": task["id"], "paths": [s["path"] for s in strays]})
        new = [s for s in strays if s["path"] not in set(stamps.get(task["id"], []))]
        ref = str(task.get("workspace_ref") or "")
        if not new or not ref.startswith("workspace:"):
            continue
        shown = ", ".join(f"{s['path']}{' (identical to your worktree copy)' if s.get('identical_to_worktree') else ''}" for s in new[:5])
        text = (f"stray write check for {task['id']}: main's working tree at {REPO_ROOT} has uncommitted edits to {len(new)} path(s) "
                f"your task also changes, written after it started: {shown}{'…' if len(new) > 5 else ''}. If a script of yours wrote "
                f"them (repo root derived from its own path, or an absolute {REPO_ROOT} path), restore main with `git -C {REPO_ROOT} "
                f"checkout -- <paths>` (untracked: rm) and redo the edit in {task['worktree']}; derive the repo root from "
                f"`git rev-parse --show-toplevel` in your cwd. If they are someone else's legitimate edits, finish will need "
                f"`dispatch.py finish --task-id {task['id']} --accept-main-edits`.")
        if tab_send(ref, text):
            stamps[task["id"]] = sorted(set(stamps.get(task["id"], [])) | {s["path"] for s in new})
            told.append(task["id"])
    STRAY_WRITE_STAMPS.parent.mkdir(parents=True, exist_ok=True)
    STRAY_WRITE_STAMPS.write_text(json.dumps(stamps, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"flagged": flagged, "told": told}


# --- idle owner tabs -----------------------------------------------------------------------
#
# An owner tab is the owner's own conversation for a workstream ("<emoji> <Name>",
# unpinned). One that has had no prompt and no live worker for its workstream
# for idle_owner_close_hours is closed through the same guarded path
# command-center-action.sh close uses: wrap-up prompt while idle with an empty
# composer, wait for that turn to end, close. The fixed surfaces are never
# candidates, nor are pinned tabs, P0 tabs ("<emoji> P0 · <Name>" is not an
# owner title), purple review holds, or tabs whose agent is mid-turn.

FIXED_SURFACE_TITLES = {"CoS", "Launchpad", "Todo", "Comms Triage", "Scheduled Jobs", "Meetings Registry"}
REVIEW_HOLD_COLOR = "#bb9af7"
WRAP_UP_PROMPT = ("Please wrap up: 1) one-paragraph summary of what was done in this session, 2) any drafts that should "
                  "land in workstream files, 3) the single next concrete action. Stop after that — do not start new work.")
IDLE_OWNER_STAMPS = STATE_DIR / "worker-dispatch" / "idle-owner-close.json"
IDLE_OWNER_RETRY = timedelta(hours=1)        # one wrap-up attempt per tab per hour
IDLE_OWNER_CLOSE_WAIT = timedelta(minutes=15)  # how long close-idle-owner waits for the wrap-up turn to end
# A "Running" (or tool-label) stamp is believed for this long. Older than that
# with no newer prompt it is unknown, not busy: the lifecycle plugin writes
# Running at turn start through a fire-and-forget `cmux set-status`, and the
# Idle write at turn end can be lost when cmux's socket is busy, leaving a tab
# "Running" for hours while its thread has been idle.
IDLE_STAMP_TRUST = timedelta(hours=2)
AMP = os.environ.get("AMP_BIN") or shutil.which("amp") or str(Path.home() / ".amp" / "bin" / "amp")
# Message states that end a turn (`amp threads export` shape); any
# other assistant state, or a user message last (a prompt or tool result the
# agent has not answered), means the turn is still in flight. A "complete"
# assistant message that stopped for tool use is also in flight: the tool is
# running and its result message only appears when it ends (a 60 s shell_command, say).
TURN_ENDED_STATES = {"complete", "cancelled", "error"}
TOOL_PENDING_STOP = "tool_use"


def bare_title(title: str) -> str:
    """Title without its leading emoji/glyph run ("🧭 CoS" -> "CoS")."""
    return re.sub(r"^[^\w]+", "", title.strip(), flags=re.UNICODE).strip()


def thread_idle_since_export(export: dict[str, Any]) -> datetime | None:
    """When the thread in an `amp threads export` payload finished its last
    turn, or None while a turn is in flight (or the shape is unreadable)."""
    messages = export.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    state = last.get("state") or {}
    if last.get("role") != "assistant" or str(state.get("type") or "") not in TURN_ENDED_STATES \
            or state.get("stopReason") == TOOL_PENDING_STOP:
        return None
    return ledger.parse_iso(last.get("createdAt") or (last.get("usage") or {}).get("timestamp"))


def claude_transcript_path(session_id: str) -> Path | None:
    """The Claude Code JSONL transcript for a session id, or None when no
    project folder under ~/.claude/projects holds one. Claude keys the folder
    by the escaped launch cwd, which the ledger does not store, so every
    project folder is searched (one glob per folder, a few dozen at most)."""
    if not CLAUDE_SESSION_ID.match(session_id or "") or not CLAUDE_PROJECTS.is_dir():
        return None
    for folder in CLAUDE_PROJECTS.iterdir():
        candidate = folder / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def claude_transcript_tail(session_id: str) -> dict[str, Any] | None:
    """The last assistant message of a Claude Code transcript as
    {"text", "at" (datetime|None), "stop_reason", "last_role", "last_at"}, or
    None when the transcript is missing or unreadable (the same "not available"
    answer the Amp export readers give). The file exists only after the first
    turn; a worker that has not answered yet reads as unavailable."""
    path = claude_transcript_path(session_id)
    if path is None:
        return None
    last_assistant: dict[str, Any] | None = None
    last_role, last_at = "", None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") not in {"user", "assistant"} or record.get("isSidechain"):
                    continue
                last_role, last_at = str(record["type"]), ledger.parse_iso(record.get("timestamp")) or last_at
                if record["type"] == "assistant":
                    last_assistant = record
    except OSError:
        return None
    if last_assistant is None:
        return None
    message = last_assistant.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = "\n".join(str(part.get("text") or "") for part in (content or [])
                         if isinstance(part, dict) and part.get("type") == "text")
    return {"text": text.strip(), "at": ledger.parse_iso(last_assistant.get("timestamp")),
            "stop_reason": str(message.get("stop_reason") or ""), "last_role": last_role, "last_at": last_at}


def thread_idle_since(thread_id: str) -> datetime | None:
    """Live Amp evidence for one thread, independent of the cmux badge: the
    time its last turn ended, or None when it is mid-turn or cannot be read.
    `amp threads export` answers from the server in well under a second.
    A Claude session id (the uuid a `claude` row records as session_id) reads
    the JSONL transcript instead: idle when the last record is an assistant
    message that did not stop for a tool call."""
    if not thread_id:
        return None
    if CLAUDE_SESSION_ID.match(thread_id):
        tail = claude_transcript_tail(thread_id)
        if tail is None or tail["last_role"] != "assistant" or tail["stop_reason"] == TOOL_PENDING_STOP:
            return None
        return tail["at"]
    try:
        result = run([AMP, "threads", "export", thread_id], timeout=30)
        if result.returncode:
            return None
        return thread_idle_since_export(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def thread_last_activity_export(export: dict[str, Any]) -> datetime | None:
    """When the thread in an `amp threads export` payload last moved: the newest
    message's createdAt (a tool result counts, so a long tool loop still reads
    as progress), else the thread's updatedAt. None when neither is readable."""
    messages = export.get("messages") or []
    stamps = []
    for message in messages[-3:]:
        stamp = ledger.parse_iso(message.get("createdAt") or (message.get("usage") or {}).get("timestamp"))
        if stamp:
            stamps.append(stamp)
    updated = ledger.parse_iso(export.get("updatedAt"))
    if updated:
        stamps.append(updated)
    return max(stamps) if stamps else None


def thread_last_activity(thread_id: str) -> datetime | None:
    """Live Amp evidence of progress for one thread, independent of the plugin
    heartbeat and the cmux badge. None when the thread cannot be read. A Claude
    session id reads the newest user or assistant record in its JSONL transcript."""
    if not thread_id:
        return None
    if CLAUDE_SESSION_ID.match(thread_id):
        tail = claude_transcript_tail(thread_id)
        return None if tail is None else (tail["last_at"] or tail["at"])
    try:
        result = run([AMP, "threads", "export", thread_id], timeout=30)
        if result.returncode:
            return None
        return thread_last_activity_export(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def quiet_local_signals(task: dict[str, Any], agent: dict[str, Any], threshold: timedelta,
                        at: datetime) -> tuple[datetime, datetime | None] | None:
    """(heartbeat, cmux stamp or None) when both local signals have been silent
    for `threshold`, else None. An Idle or Needs input stamp is the idle nudge's
    case (worker_idle_since), never a wedge."""
    if str(agent.get("state") or "") in IDLE_STATES and agent.get("state_ts"):
        return None
    beat = ledger.parse_iso(task.get("heartbeat_at")) or ledger.parse_iso(task.get("started_at"))
    if beat is None or at - beat < threshold:
        return None
    stamped = datetime.fromtimestamp(agent["state_ts"], tz=timezone.utc) if agent.get("state_ts") else None
    if stamped and at - stamped < threshold:
        return None
    return beat, stamped


def wedged_reason(task: dict[str, Any], agent: dict[str, Any], thread_at: datetime | None, threshold: timedelta,
                  at: datetime | None = None, screen: str = "") -> str | None:
    """One line saying why `task` is wedged, or None while any signal shows
    progress inside `threshold`. Pure; the sweep passes the live readings.

    Three signals must all be silent for the threshold: the task-liveness
    heartbeat (one per tool result; plugin hooks never see code_exec, so it also
    goes quiet for a worker that only runs code_exec), the cmux lifecycle stamp
    (a wedged TUI keeps a busy stamp; an Idle or Needs input stamp is the idle
    nudge's case, not this one), and the Amp thread's last message. With the
    thread unreadable, heartbeat and stamp alone decide at twice the threshold.

    `screen` is what the tab shows (usage_limits.screen_evidence), when the caller
    could read it: the line then names it instead of guessing "likely stuck on a
    tool call" when the worker is in fact stopped on, say, a DNS error)."""
    at = at or now()
    quiet = quiet_local_signals(task, agent, threshold, at)
    if quiet is None:
        return None
    beat, stamped = quiet
    state = str(agent.get("state") or "")
    if thread_at is None:
        if at - beat < 2 * threshold or (stamped and at - stamped < 2 * threshold):
            return None
        evidence = "thread unreadable"
    else:
        if at - thread_at < threshold:
            return None
        evidence = f"last thread message {hours_label(at - thread_at)} ago"
    stamp_text = f"cmux says {state} since {hours_label(at - stamped)} ago" if stamped else "no cmux stamp"
    tail = f"the tab shows {screen}" if screen else "likely stuck on a tool call"
    return f"no tool result for {hours_label(at - beat)}, {evidence}, {stamp_text}; {tail}"


def hours_label(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    return f"{hours:.0f}h" if hours >= 1 else f"{int(span.total_seconds() // 60)}m"


def agentless_skip(what: str, listing_empty: bool = False) -> str:
    """Why a liveness check skipped with no agent in the session store, in words that tell an
    outage from an idle machine. The skip itself stays: judging live rows against an empty
    store would read every worker as dead. With no live row there is nothing to judge, and
    the line says so instead of reading as a store outage."""
    if listing_empty:
        return f"the multiplexer lists no tabs (read outage); not judging {what}"
    live = sum(1 for t in ledger.load_payload()["tasks"] if t.get("status") in {"routing", "active"})
    if not live:
        return f"no live worker and no bound agent session; nothing to judge for {what}"
    return f"the session store lists no agents while {live} row(s) are live (store outage); not judging {what}"


def flag_wedged_workers(cfg: dict[str, Any], last_activity: Any = None) -> dict[str, Any]:
    """Sweep step: an `active` row whose worker has made no observable progress
    for `dispatch.wedged_hours` is stamped `wedged`, given a `recovery_action`
    (so cos_review_feed.stall_of shows it as Stalled under Needs you), and its
    origin hears one urgent line naming the thread, the tab and the re-dispatch
    command. The sweep never re-dispatches it and flags each attempt once.

    A worker whose last tool call never returns stops its heartbeat while the
    cmux stamp stays "Running" and the TUI shows a spinner, so worker_idle_since
    answers None (busy screen) and nudge_idle never fires; this flag is what
    turns stale_since and recovery_action into a CoS line. A row in a landing-conflict window is left to that window. Skipped
    entirely when cmux lists no agents (store outage, same guard as
    fail_abandoned_rows)."""
    try:
        hours = float(cfg.get("wedged_hours") or 0)
    except (TypeError, ValueError):
        hours = float(DEFAULTS["wedged_hours"])
    if hours <= 0:
        return {"wedged": [], "skipped": "wedged_hours is 0; rule off"}
    agents = ledger.live_agents()
    if not agents:
        return {"wedged": [], "skipped": agentless_skip("liveness")}
    threshold = timedelta(hours=hours)
    last_activity = last_activity or thread_last_activity
    wedged = []
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") != "active" or task.get("wedged_at") or not task.get("thread_id"):
            continue
        if task.get("usage_limit"):
            continue   # silent by design until the usage limit resets (handle_limits)
        if network_owned(task):
            continue   # waiting for the API to answer: handle_network_stops resumes it
        if (task.get("landing") or {}).get("state") == "conflict":
            continue
        agent = agents.get(str(task["thread_id"])) or {}
        if quiet_local_signals(task, agent, threshold, now()) is None:
            continue   # cheap signals first; the thread export is one server call per candidate
        thread_at = last_activity(str(task["thread_id"]))
        if wedged_reason(task, agent, thread_at, threshold) is None:
            continue
        ref = str(task.get("workspace_ref") or "")
        screen = usage_limits.screen_evidence(read_tab_screen(ref)) if ref.startswith("workspace:") else ""
        reason = wedged_reason(task, agent, thread_at, threshold, screen=screen)
        line = (f"wedged: {reason} (thread {task['thread_id']}, {ref or 'no worker tab'}, attempt {task.get('attempt_id')}). "
                f"Not re-dispatched by the sweep; check the tab, or re-dispatch with "
                f"router-dispatch.sh --task-id {task['id']} --force-redispatch")

        def mark(p, tid=task["id"], attempt_id=task.get("attempt_id"), text=line):
            row = ledger.find_task(p, tid)
            if row is None or row.get("status") != "active" or row.get("attempt_id") != attempt_id or row.get("wedged_at"):
                return None
            stamp = ledger.now_iso()
            row.update(wedged_at=stamp, wedged={"since": stamp, "reason": text}, recovery_action=text, updated_at=stamp)
            return dict(row)

        if not ledger.locked_mutation(mark):
            continue
        result = notify(task["id"], line, status="active", urgent=True)
        log(f"{task['id']}: {line[:160]} (origin told: {result.get('delivered')})")
        wedged.append({"task_id": task["id"], "thread_id": task["thread_id"], "reason": line,
                       "origin_told": bool(result.get("delivered"))})
    return {"wedged": wedged}


def idle_owner_candidates(workspaces: list[dict[str, Any]], payload: dict[str, Any], agents: dict[str, dict[str, Any]],
                          hours: float, at: datetime | None = None,
                          live_idle_since: Any = None) -> list[dict[str, Any]]:
    """Owner tabs eligible for the idle close right now (pure given
    `live_idle_since`; the sweep decides pacing). Idle since = the later of the
    tab's last submitted prompt and its agent's last lifecycle stamp; a tab
    with neither is left alone (its idle span is unknown, typically a tab
    restored by a cmux restart).

    A busy stamp younger than IDLE_STAMP_TRUST (or with a newer prompt) is
    trusted and skips the tab. An older one is confirmed against the live Amp
    thread through `live_idle_since(thread_id)`: a thread that is idle is idle
    whatever the badge says, and its idle span starts when its last turn
    ended; a thread that is mid-turn, or that cannot be read, keeps the tab."""
    at = at or now()
    cutoff = timedelta(hours=hours)
    owners = owner_titles()
    live = {t.get("workstream") for t in payload.get("tasks", []) if t.get("status") not in ledger.TERMINAL_STATUSES}
    by_workspace = {a.get("workspace_id"): a for a in agents.values() if a.get("workspace_id")}
    out = []
    for ws in workspaces:
        title = tab_title(ws)
        if ws.get("pinned") or bare_title(title) in FIXED_SURFACE_TITLES:
            continue
        slug = owners.get(title)
        if slug is None or slug in live:
            continue
        if str(ws.get("color") or "").lower() == REVIEW_HOLD_COLOR:
            continue
        agent = by_workspace.get(str(ws.get("id") or ""))
        submitted = ledger.parse_iso(ws.get("latest_submitted_at"))
        stamped = datetime.fromtimestamp(float(agent["state_ts"]), tz=timezone.utc) if agent and agent.get("state_ts") else None
        stamps = [submitted]
        if agent and agent.get("state") not in IDLE_STATES:
            believed = max((s for s in (stamped, submitted) if s), default=None)
            if believed is None or at - believed < IDLE_STAMP_TRUST or live_idle_since is None:
                continue
            since = live_idle_since(str(agent.get("thread_id") or ""))
            if since is None:
                continue
            stamps.append(since)
        else:
            stamps.append(stamped)
        last = max((s for s in stamps if s), default=None)
        if last is None or at - last < cutoff:
            continue
        out.append({"ref": str(ws.get("ref") or ""), "id": str(ws.get("id") or ""), "title": title, "workstream": slug,
                    "idle_hours": round((at - last).total_seconds() / 3600, 1)})
    return out


def close_idle_owners(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    hours = float(cfg.get("idle_owner_close_hours") or 0)
    if hours <= 0:
        return []
    try:
        stamps = json.loads(IDLE_OWNER_STAMPS.read_text()) or {}
    except (OSError, ValueError):
        stamps = {}
    started = []
    for cand in idle_owner_candidates(cmux_workspaces(), ledger.load_payload(), ledger.live_agents(), hours,
                                      live_idle_since=thread_idle_since):
        last = ledger.parse_iso((stamps.get(cand["ref"]) or {}).get("attempted_at"))
        if last and now() - last < IDLE_OWNER_RETRY:
            continue
        stamps[cand["ref"]] = {"title": cand["title"], "attempted_at": ledger.now_iso(), "idle_hours": cand["idle_hours"]}
        IDLE_OWNER_STAMPS.parent.mkdir(parents=True, exist_ok=True)
        with (IDLE_OWNER_STAMPS.parent / "idle-owner-close.log").open("a") as handle:
            subprocess.Popen([sys.executable, str(SCRIPTS / "dispatch.py"), "close-idle-owner", "--ref", cand["ref"], "--title", cand["title"]],
                             stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
        log(f"idle owner tab {cand['title']} ({cand['ref']}) idle {cand['idle_hours']}h with no live worker; wrap-up and close started")
        started.append(cand)
    IDLE_OWNER_STAMPS.parent.mkdir(parents=True, exist_ok=True)
    IDLE_OWNER_STAMPS.write_text(json.dumps(stamps, indent=2))
    return started


def command_close_idle_owner(args: argparse.Namespace) -> int:
    """Detached worker for one idle owner tab: wrap-up prompt only while the tab
    is idle with an empty composer, wait for that turn to end, then close. Any
    change of mind (title changed, a prompt typed meanwhile, the tab never
    idle) leaves the tab open and says why."""
    def say(msg: str) -> None:
        print(f"{ledger.now_iso()} {args.ref} {args.title}: {msg}", flush=True)

    ws = workspace_by_ref(args.ref)
    if ws is None or tab_title(ws) != args.title:
        say("tab gone or renamed; nothing closed"); return 0
    if not tab_idle(args.ref):
        say("not idle with an empty composer; left open"); return 0
    agent = next((a for a in ledger.live_agents().values() if a.get("workspace_id") == (ws.get("id") or "")), None)
    if agent and agent.get("state") not in IDLE_STATES and thread_idle_since(str(agent.get("thread_id") or "")) is None:
        say(f"screen reads idle but cmux badge says {agent.get('state')!r} and the Amp thread is mid-turn or unreadable; left open")
        return 0
    if not tab_send(args.ref, WRAP_UP_PROMPT):
        say("wrap-up prompt refused by the inject guard; left open"); return 0
    say("wrap-up prompt sent")
    sent_at = now()
    time.sleep(20)
    # The wrap-up submit is now the tab's latest prompt; any later change to
    # that stamp is the owner typing, and the tab stays open.
    wrap_up_stamp = (workspace_by_ref(args.ref) or {}).get("latest_submitted_at")
    deadline = now() + IDLE_OWNER_CLOSE_WAIT
    while now() < deadline:
        ws = workspace_by_ref(args.ref)
        if ws is None or tab_title(ws) != args.title:
            say("tab gone or renamed during wrap-up; nothing closed"); return 0
        if ws.get("latest_submitted_at") != wrap_up_stamp:
            say("a new prompt was submitted during the wrap-up; left open"); return 0
        agent = next((a for a in ledger.live_agents().values() if a.get("workspace_id") == (ws.get("id") or "")), None)
        turn_over = agent is None or agent.get("state") in IDLE_STATES
        if not turn_over:
            # The badge can keep saying Running after the turn ended (the Idle
            # write is fire-and-forget); the thread itself is the tie-breaker:
            # a last turn that ended after the wrap-up was sent is the wrap-up.
            since = thread_idle_since(str(agent.get("thread_id") or ""))
            turn_over = since is not None and since >= sent_at - timedelta(seconds=5)
            if turn_over:
                say(f"cmux badge still says {agent.get('state')!r}; Amp thread idle since {since.isoformat(timespec='seconds')}, "
                    "trusting the thread")
        if turn_over and tab_idle(args.ref):
            break
        time.sleep(20)
    else:
        say("wrap-up turn still running after the wait; left open"); return 0
    ok, error = cmux_close_result(args.ref, str(ws.get("id") or ""), "owner tab wrap-up")
    if ok:
        say("closed after wrap-up"); return 0
    say(f"cmux close failed; left open: {error}"); return 1


def requeue_orphaned_routes() -> list[dict[str, Any]]:
    """A row is "routing" only while the dispatch that created it is launching
    its worker. If that dispatch died between `start` and `launch`/`queue` (or
    the row predates worktree dispatch), the row has no worker_id,
    worktree, or thread, `release` ignores it (it only launches "queued"), and
    nothing ever starts, fails, or reports it.
    After NOT_STARTED_AFTER with no live thread and no fresh launch claim, the
    sweep re-queues the row so the next release applies the normal dependency
    and capacity rules. The ledger summary records the repair."""
    agents = ledger.live_agents()
    requeued = []
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") != "routing" or task.get("worker_id") or task.get("worktree"):
            continue
        if task.get("thread_id") and task["thread_id"] in agents:
            continue
        claim = ledger.parse_iso(task.get("launching_at"))
        if claim and now() - claim < timedelta(minutes=5):
            continue  # a launcher holds this row (claim_launch); let it finish or expire
        last = ledger.parse_iso(task.get("updated_at") or task.get("created_at"))
        if last is None or now() - last < NOT_STARTED_AFTER:
            continue
        heir = continuation_heir(task, ledger.load_payload())
        if heir is not None:
            retire_superseded(task, heir)
            continue
        idle = int((now() - last).total_seconds() // 60)
        summary = f"re-queued by sweep: routing for {idle}m with no worker (attempt {task.get('attempt_id')} never launched)"

        def mutate(payload, tid=task["id"], summary=summary):
            row = ledger.find_task(payload, tid)
            if row is None or row.get("status") != "routing" or row.get("worker_id") or row.get("worktree"):
                return None
            row.update(status="queued", summary=summary, requeued_at=ledger.now_iso(), updated_at=ledger.now_iso())
            row.pop("launching_at", None)
            return dict(row)

        if ledger.locked_mutation(mutate):
            log(f"{task['id']}: {summary}")
            requeued.append({"task_id": task["id"], "attempt_id": task.get("attempt_id"), "idle_minutes": idle})
    return requeued


UNKNOWN_REPORT_SETTLES_AFTER = timedelta(hours=int(os.environ.get("SWITCHBOARD_UNKNOWN_REPORT_HOURS") or 24))


def settle_unknown_reports() -> list[dict[str, Any]]:
    """A terminal row whose one report ended outcome-unknown is never re-sent
    (one message per task), so without this it would never settle: the reaper
    would skip its tab and worktree forever ("report not delivered yet"). Most
    unknown outcomes have in fact reached CoS. After UNKNOWN_REPORT_SETTLES_AFTER the sweep settles the row as
    delivered-unverified, with a note, so the lifecycle completes; the Thread
    Status feed keeps the original unknown row for anyone auditing."""
    settled = []
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") not in ledger.TERMINAL_STATUSES or task.get("report_delivered") is True \
                or not task.get("report_delivery_unknown"):
            continue
        since = ledger.parse_iso(task.get("report_attempted_at") or task.get("finished_at") or task.get("updated_at"))
        if since is None or now() - since < UNKNOWN_REPORT_SETTLES_AFTER:
            continue
        hours = int((now() - since).total_seconds() // 3600)
        note = f"settled by sweep: delivery outcome unknown for {hours}h; never re-sent (one message per task)"

        def mutate(payload, tid=task["id"], note=note):
            row = ledger.find_task(payload, tid)
            if row is None or row.get("report_delivered") is True or not row.get("report_delivery_unknown"):
                return None
            row.update(report_delivered=True, report_delivered_at=ledger.now_iso(), note=note, updated_at=ledger.now_iso())
            row.pop("report_delivery_unknown", None)
            return dict(row)

        if ledger.locked_mutation(mutate):
            log(f"{task['id']}: {note}")
            settled.append({"task_id": task["id"], "unknown_since": since.isoformat(timespec="seconds")})
    return settled


# A worker runs inside its tab, so a tab no identity can find means the worker is dead. The short
# wait only guards against a tab list read mid-launch.
DEAD_WORKER_AFTER = timedelta(seconds=int(os.environ.get("SWITCHBOARD_DEAD_WORKER_SECONDS") or 180))
MAX_DEAD_WORKER_RELAUNCHES = 2   # a reboot or lost tab is not the worker's fault; a checkout with work also gets this many
ABANDONED_AFTER = timedelta(seconds=int(os.environ.get("SWITCHBOARD_ABANDONED_SECONDS") or 7200))  # no checkout to judge by: a dead thread this old is abandoned
MAX_SWEEP_RELAUNCHES = 1  # a clean abandoned checkout is relaunched this many times before the origin hears a blocker


def tab_is_bare_shell(ref: str, agent: str) -> bool:
    """The tab shows a shell prompt with nothing running: its worker exited, or wmux
    restored the tab after a reboot with no agent in it. Unreadable counts as no."""
    try:
        return classify_screen(read_tab_screen(ref), agent) == "shell"
    except (OSError, subprocess.SubprocessError):
        return False


def fail_abandoned_rows(cfg: dict[str, Any]) -> dict[str, Any]:
    """A routing/active row whose worker thread is no longer an Amp agent in cmux
    and whose stamps are stale is finished by the sweep, never left for a nudge:
    live_workers() already stops counting such rows ("the sweep fails it"), and
    this is the step that does it, so a dead row never sits active for days.

    Checkout clean (nothing main lacks) -> the row is re-queued as a new attempt
    and release relaunches it, at most MAX_SWEEP_RELAUNCHES times. Checkout holds
    work, relaunches exhausted, or no checkout at all (owner-tab/native row, judged
    after ABANDONED_AFTER) -> blocked with the recovery written in the summary and
    one line to the origin. Skipped entirely when cmux shows no agents at all,
    which is a store outage, not a mass death."""
    agents = ledger.live_agents()
    listing = cmux_workspaces()
    if not agents and not listing:
        return {"relaunched": [], "blocked": [], "skipped": agentless_skip("liveness")}
    relaunched, blocked, superseded = [], [], []
    workspaces = {w.get("ref"): w for w in listing}
    for task in ledger.load_payload()["tasks"]:
        if task.get("status") not in {"routing", "active"}:
            continue
        if task.get("usage_limit") and not limits_disabled():
            continue   # paused on the usage limit: handle_limits resumes or relaunches it after the reset
        thread_id = str(task.get("thread_id") or "")
        if thread_id and thread_id in agents:
            continue
        claim = ledger.parse_iso(task.get("launching_at"))
        if claim and now() - claim < timedelta(minutes=5):
            continue
        has_checkout = bool(task.get("worktree") or task.get("worker_id"))
        if not has_checkout and not thread_id:
            continue  # never had a worker: requeue_orphaned_routes() owns workerless routing rows
        if task.get("landed_commit") and ledger.git_state(task) == "landed" and ledger.branch_tip_landed(task):
            # Landed mid-task and not yet reported. A relaunch would
            # redo work already on main; expire-leases closes the row as done, with the worker's
            # last message, once it has been quiet for the stale window.
            continue
        last = ledger.parse_iso(task.get("heartbeat_at") or task.get("started_at") or task.get("updated_at") or task.get("created_at"))
        # Acked workers only: a launch that has not started belongs to retype_stalled_launches.
        # That step reads routing rows, and the launcher's "started" line turns a row active
        # before any ack, so an active row counts here too. Otherwise a tab lost during the launch
        # leaves it active with no tab for the whole stale window.
        acked = bool(task.get("started_at") or thread_id or task.get("status") == "active")
        judged = has_checkout and acked and bool(listing) and not task.get("executor")
        live_tab = resolve_live_tab(task, listing, agents) if judged else None
        tab_gone = judged and live_tab is None
        if not agents and not tab_gone:
            continue   # an empty session store judges nothing; only a missing tab does
        shell_left = (live_tab is not None and last is not None and now() - last >= DEAD_WORKER_AFTER
                      and tab_is_bare_shell(str(live_tab.get("ref") or ""), str(task.get("agent") or "amp")))
        dead = tab_gone or shell_left
        window = DEAD_WORKER_AFTER if dead else (ledger.STALE_AFTER if has_checkout else ABANDONED_AFTER)
        if last is None or now() - last < window:
            continue
        heir = continuation_heir(task, ledger.load_payload())
        if heir is not None:
            # Judged dead, and a continuation already has its work: a relaunch would put two
            # workers on one job, and a blocked line would ask the owner to land what the
            # continuation is carrying.
            if retire_superseded(task, heir):
                superseded.append({"task_id": task["id"], "superseded_by": heir["id"]})
            continue
        if tab_gone:
            gone = f"worker tab {task.get('workspace_ref')} is gone"
        elif shell_left:
            gone = f"worker tab {live_tab.get('ref')} is at a bare shell"
        else:
            gone = f"worker thread {thread_id or '(never bound)'} is gone from cmux"
        gone += f"; last sign of life {last.isoformat(timespec='seconds')}"
        ref = str((live_tab or {}).get("ref") or task.get("workspace_ref") or "")
        ws = live_tab or workspaces.get(ref)
        if ws is None and not tab_gone:
            ws = worker_tab(task["id"], listing)
            ref = str((ws or {}).get("ref") or ref)
        if has_checkout:
            held = unlanded_work(task)
            relaunches = int(task.get("sweep_relaunches") or 0)
            lead = task_lead(task["id"], str(task.get("workstream") or ""))
            checkout_problem = verify_worktree(Path(str(task.get("worktree") or worktree_path(task["id"]))),
                                               str(task.get("branch") or "")) if held else ""
            if held and dead and not checkout_problem and relaunches < MAX_DEAD_WORKER_RELAUNCHES and task.get("prompt"):
                # The dead attempt's work stays where it is and the next attempt carries on
                # in that same checkout; a blocked row used to wait for someone to notice.
                # Only on proof of death: an open tab that merely went quiet could be a
                # worker in one long tool call, and two workers must never share a checkout.
                if ws is not None and not protected(ws, task) \
                        and not cmux_close(ref, str(ws.get("id") or ""), f"sweep: dead worker {task['id']}"):
                    log(f"{task['id']}: could not close {ref}; not relaunching until it closes")
                    continue
                summary = (f"re-queued by sweep: {gone}; attempt {task.get('attempt_id')} left {held} in {task.get('worktree')}, "
                           f"and the next attempt carries on there (relaunch {relaunches + 1}/{MAX_DEAD_WORKER_RELAUNCHES})")

                def resume(payload, tid=task["id"], old=dict(task), summary=summary):
                    row = ledger.find_task(payload, tid)
                    if row is None or row.get("status") not in {"routing", "active"} or row.get("attempt_id") != old.get("attempt_id"):
                        return None
                    ledger.begin_attempt(row, status="queued", summary=summary)
                    row.update(sweep_relaunches=int(row.get("sweep_relaunches") or 0) + 1, requeued_at=ledger.now_iso(),
                               resume_worktree=str(old.get("worktree") or worktree_path(tid)), resume_branch=str(old.get("branch") or ""),
                               resume_from_attempt=old.get("attempt_id"), resume_session=str(old.get("thread_id") or old.get("session_id") or ""))
                    return dict(row)

                if ledger.locked_mutation(resume):
                    log(f"{task['id']}: {summary}")
                    told = tell_cos_direct(f"{lead} worker died ({gone}). Redispatched on the same checkout, which holds {held}.")
                    relaunched.append({"task_id": task["id"], "replaced_attempt": task.get("attempt_id"), "resumed_with": held,
                                       "origin_told": bool(told.get("ok"))})
                continue
            limit = MAX_DEAD_WORKER_RELAUNCHES if dead else MAX_SWEEP_RELAUNCHES
            if not held and relaunches < limit and task.get("prompt"):
                if ws is not None and not protected(ws, task) \
                        and not cmux_close(ref, str(ws.get("id") or ""), f"sweep: dead worker {task['id']}"):
                    log(f"{task['id']}: could not close {ref}; not relaunching until it closes")
                    continue
                parked = drop_worktree(task)  # judged clean above; a late write still parks instead of deleting
                summary = f"re-queued by sweep: {gone}; attempt {task.get('attempt_id')} left no work behind (relaunch {relaunches + 1}/{limit})"
                if parked is not None:
                    summary += f"; its worktree held late edits and was parked at {parked}"

                def relaunch(payload, tid=task["id"], attempt_id=task.get("attempt_id"), summary=summary):
                    row = ledger.find_task(payload, tid)
                    if row is None or row.get("status") not in {"routing", "active"} or row.get("attempt_id") != attempt_id:
                        return None
                    ledger.begin_attempt(row, status="queued", summary=summary)
                    row.update(sweep_relaunches=int(row.get("sweep_relaunches") or 0) + 1, requeued_at=ledger.now_iso())
                    return dict(row)

                if ledger.locked_mutation(relaunch):
                    log(f"{task['id']}: {summary}")
                    entry = {"task_id": task["id"], "replaced_attempt": task.get("attempt_id")}
                    if parked is not None:
                        entry["worktree_parked"] = str(parked)
                    if dead:
                        told = tell_cos_direct(f"{lead} worker died ({gone}). Redispatched; its checkout held no work.")
                        entry["origin_told"] = bool(told.get("ok"))
                    relaunched.append(entry)
                continue
            if held:
                detail = ("; " + checkout_problem if checkout_problem
                          else f", and the sweep already relaunched it {relaunches}x" if dead
                          else ", and its tab is open, so the worker may still be running")
                reason = (f"blocked: {gone}; {task.get('worktree')} holds {held} on branch {task.get('branch')}{detail}. "
                          f"Land it with dispatch.py finish --task-id {task['id']}, or discard it with "
                          f"router-dispatch.sh --task-id {task['id']} --force-redispatch")
            else:
                reason = (f"blocked: {gone}; already relaunched {relaunches}x by the sweep and the worker died again. "
                          f"Re-dispatch with router-dispatch.sh --task-id {task['id']} after checking its launch prompt")
        else:
            reason = (f"blocked: {gone} (attempt {task.get('attempt_id')} ran without a worktree, so nothing can be recovered). "
                      f"Re-dispatch with router-dispatch.sh --task-id {task['id']} if the work is still wanted")
        result = notify(task["id"], reason, status="blocked")
        log(f"{task['id']}: {reason[:160]} (origin told: {result.get('delivered')})")
        blocked.append({"task_id": task["id"], "reason": reason, "origin_told": bool(result.get("delivered"))})
    return {"relaunched": relaunched, "blocked": blocked, **({"superseded": superseded} if superseded else {})}


def settle_superseded_reports() -> list[dict[str, Any]]:
    """A blocked or failed row whose continuation owns its work has nothing left to say. Its
    undelivered report asks the owner to land or discard a checkout the continuation already
    took, and a closed CoS would get every such line at once when it reopens. Done reports always go through."""
    settled = []
    payload = ledger.load_payload()
    for task in payload["tasks"]:
        if task.get("status") not in {"blocked", "failed"} or task.get("report_delivered") is not False:
            continue
        heir = continuation_heir(task, payload)
        if heir is None:
            continue
        note = f"settled without send: the continuation {heir['id']} owns this work"

        def mutate(p, tid=task["id"], note=note):
            row = ledger.find_task(p, tid)
            if row is None or row.get("report_delivered") is not False:
                return None
            row.update(report_delivered=True, report_delivered_at=ledger.now_iso(), note=note, updated_at=ledger.now_iso())
            return dict(row)

        if ledger.locked_mutation(mutate):
            log(f"{task['id']}: {note}")
            settled.append({"task_id": task["id"], "superseded_by": heir["id"]})
    return settled


def follow_replaced_prerequisites() -> list[dict[str, Any]]:
    """A row blocked because its prerequisite ended badly goes back in the queue once a
    continuation has replaced that prerequisite: release_queued then waits on the replacement
    like any other dependency. The order is usually dep dies, row blocks, CoS continues the dep,
    so without this the row stays blocked on work that is moving."""
    followed = []
    payload = ledger.load_payload()
    for task in payload["tasks"]:
        dep_id = str(task.get("blocked_on_prerequisite") or "")
        if task.get("status") != "blocked" or not dep_id:
            continue
        if continuation_heir(task, payload) is not None or failed_dependency(task, payload) is not None:
            continue
        current = effective_dependency(dep_id, payload) or {}
        summary = f"prerequisite {dep_id} was replaced by {current.get('id')}; queued behind it"

        def mutate(p, tid=task["id"], attempt_id=task.get("attempt_id"), summary=summary):
            row = ledger.find_task(p, tid)
            if row is None or row.get("status") != "blocked" or row.get("attempt_id") != attempt_id:
                return None
            ledger.begin_attempt(row, status="queued", summary=summary)
            row.pop("blocked_on_prerequisite", None)
            return dict(row)

        if ledger.locked_mutation(mutate):
            log(f"{task['id']}: {summary}")
            lead = task_lead(task["id"], str(task.get("workstream") or ""))
            told = tell_cos_direct(f"{lead} was blocked on a prerequisite that has been replaced. It is queued behind the replacement.")
            followed.append({"task_id": task["id"], "prerequisite": dep_id, "now_waiting_on": current.get("id"),
                             "origin_told": bool(told.get("ok"))})
    return followed


def stuck_route_age(task: dict[str, Any]) -> timedelta | None:
    """How long a routing/queued row has waited for a worker: since the latest
    of created_at, requeued_at, launching_at and launched_at. updated_at is not
    used because every sweep retype refreshes it, so a stalled row would always
    look minutes old."""
    stamps = [ledger.parse_iso(task.get(k)) for k in ("created_at", "requeued_at", "launching_at", "launched_at")]
    stamps = [s for s in stamps if s is not None]
    return now() - max(stamps) if stamps else None


def flag_stuck_routes(cfg: dict[str, Any]) -> dict[str, Any]:
    """Sweep step: a row still `routing` or `queued`
    after `dispatch.stuck_routing_minutes` with no live Amp thread and no Amp
    session in its worker tab is marked blocked with a `stuck:` reason, and its
    origin hears one line naming the task, the age and the re-dispatch command.
    The sweep never re-dispatches it, and flags each row once (`stuck_at`).

    A row whose tab opened but whose agent never connected is caught by no other
    rule: requeue_orphaned_routes skips it (it has a worker), retype_stalled_launches
    keeps refreshing updated_at, and fail_abandoned_rows judges from updated_at.

    Left alone: a row younger than the threshold; a launcher's fresh claim
    (launching_at under 5 min); a queued row held by a prerequisite or by a
    positive worker cap (release owns those); a tab that runs an Amp session
    the ledger has not bound yet (a binding problem, not a stuck launch). Skipped
    entirely when cmux lists no agents at all (store outage, same guard as
    fail_abandoned_rows)."""
    try:
        minutes = int(cfg.get("stuck_routing_minutes") or 0)
    except (TypeError, ValueError):
        minutes = int(DEFAULTS["stuck_routing_minutes"])
    if minutes <= 0:
        return {"stuck": [], "skipped": "stuck_routing_minutes is 0; rule off"}
    agents = ledger.live_agents()
    if not agents:
        return {"stuck": [], "skipped": agentless_skip("liveness")}
    threshold = timedelta(minutes=minutes)
    payload = ledger.load_payload()
    workspaces = {w.get("ref"): w for w in cmux_workspaces()}
    tenants = {a.get("workspace_id") for a in agents.values() if a.get("workspace_id")}
    stuck = []
    cap = worker_cap(cfg)
    live_count = len(live_workers(payload)) if cap is not None else 0
    memory_held: bool | None = None
    for task in payload["tasks"]:
        status = str(task.get("status") or "")
        if status not in {"routing", "queued"} or task.get("stuck_at"):
            continue
        thread_id = str(task.get("thread_id") or "")
        if thread_id and thread_id in agents:
            continue
        claim = ledger.parse_iso(task.get("launching_at"))
        if claim and now() - claim < timedelta(minutes=5):
            continue
        age = stuck_route_age(task)
        if age is None or age < threshold:
            continue
        if status == "queued":
            if task.get("launch_hold_since"):
                continue   # release retries it each sweep; report_launch_holds tells the origin
            if unmet_dependency(task, payload) is not None:
                continue
            if launch_hold(task, cfg) is not None:
                continue   # a dated launch hold; release_queued starts it once `until` passes
            if cap is not None and live_count >= cap:
                continue
            if memory_held is None:
                memory_held = bool(commit_hold_reason(cfg, live=len(live_workers(payload))))
            if memory_held:
                continue   # waiting on memory; release_queued starts it once commit drops
        ref = str(task.get("workspace_ref") or "")
        ws = workspaces.get(ref)
        if ws is not None and (ws.get("id") or "") in tenants:
            continue   # an Amp session runs in the tab; the ledger just never bound it
        tab = f"worker tab {ref}" if ws is not None else ("worker tab gone" if ref else "no worker tab")
        if ws is None and ref and not workspaces:
            tab = f"worker tab {ref} (cmux unreadable)"
        age_min = int(age.total_seconds() // 60)
        reason = (f"stuck: {status} for {age_min}m with no live thread ({tab}; attempt {task.get('attempt_id')}). "
                  f"Not re-dispatched by the sweep; re-dispatch with router-dispatch.sh --task-id {task['id']} --force-redispatch")

        def mark(p, tid=task["id"], attempt_id=task.get("attempt_id")):
            row = ledger.find_task(p, tid)
            if row is None or row.get("status") not in {"routing", "queued"} or row.get("attempt_id") != attempt_id \
                    or row.get("stuck_at"):
                return None
            row.update(stuck_at=ledger.now_iso(), updated_at=ledger.now_iso())
            return dict(row)

        if not ledger.locked_mutation(mark):
            continue   # the row moved on (or another sweep flagged it) between the read and the write
        result = notify(task["id"], reason, status="blocked")
        log(f"{task['id']}: {reason[:160]} (origin told: {result.get('delivered')})")
        stuck.append({"task_id": task["id"], "status_was": status, "age_minutes": age_min, "reason": reason,
                      "origin_told": bool(result.get("delivered"))})
    return {"stuck": stuck}


def prune_orphan_worktrees() -> dict[str, list[Any]]:
    """Checkouts under WORKTREES with no ledger row at all (launches the ledger
    never recorded) are settled once they
    are over an hour old: removed when they hold nothing main lacks, parked under
    _abandoned (branch kept) when they hold uncommitted or untracked work, left
    alone when their branch has unlanded commits (a human lands or discards those).
    Nothing with work is ever deleted (fix B3-lite)."""
    removed: list[str] = []
    parked: list[dict[str, str]] = []
    if not WORKTREES.is_dir():
        return {"removed": removed, "parked": parked}
    if not ledger.STATE_FILE.is_file():
        # No ledger means no knowledge of which checkouts have rows (a fixture
        # STATE_DIR, a fresh clone): judging them would park every live worker.
        return {"removed": removed, "parked": parked, "skipped": f"{ledger.STATE_FILE} does not exist; not judging worktrees"}
    rows = {t["id"] for t in ledger.load_payload()["tasks"]}
    for path in sorted(WORKTREES.iterdir()):
        tid = path.name
        if not tid.startswith("rt-") or tid in rows or not (path / ".git").exists():
            continue
        if now() - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < timedelta(hours=1):
            continue
        task = {"id": tid, "worktree": str(path), "branch": f"task/{tid}"}
        held = unlanded_work(task)
        if "commits" in held:
            continue
        dest = drop_worktree(task)
        if dest is not None:
            parked.append({"task_id": tid, "parked_to": str(dest), "held": held or "uncommitted edits"})
        else:
            removed.append(tid)
    return {"removed": removed, "parked": parked}


def reconcile_receipts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Legacy busy-owner receipts whose verifier gave up ("blocked-*-outcome-unknown",
    "blocked-stale-attempt", ...) but whose attempt the ledger later acked. The
    ledger is the truth; the receipt is annotated so boards stop showing a
    blocked route for work that landed."""
    changed = []
    for path in sorted((STATE_DIR / "worker-dispatch").glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict) or not str(receipt.get("seed_status") or "").startswith("blocked-"):
            continue
        row = ledger.find_task(payload, str(receipt.get("task_id") or ""))
        if not row or row.get("attempt_id") != receipt.get("attempt_id") or not row.get("thread_id"):
            continue
        if row.get("status") not in {"active", *ledger.TERMINAL_STATUSES}:
            continue
        receipt.update(prior_seed_status=receipt["seed_status"], seed_status="reconciled",
                       reconciled_at=ledger.now_iso(),
                       reconciled_by=f"ledger: attempt {row['attempt_id']} acked by {row['thread_id']} and is {row['status']}")
        try:
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            continue
        changed.append({"receipt": path.name, "task_id": row["id"], "attempt_id": row["attempt_id"],
                        "was": receipt["prior_seed_status"], "ledger_status": row["status"]})
    return changed


# --- usage and context limits ---------------------------------------------------------------
#
# Workers that stop on a usage or context limit restart on their own: without this, workers
# and CoS stopped on the 5-hour usage limit sit until someone types a "limit has reset,
# continue" line into each tab. handle_limits does that on its own, every sweep tick:
#   1. Each live Claude worker's transcript tail is read (usage_limits.classify_transcript).
#      A usage-limit stop marks the row `usage_limit` and `paused: usage limit, resumes at
#      <time>`, and the global stamp system/state/usage-limit.json holds the reset time, so
#      release_queued launches no Claude worker until then. CoS hears one line per pause.
#   2. After the reset (plus LIMIT_RESUME_GRACE), each paused row in creation order is
#      resumed in place: one guarded line into its own open session, which keeps its context
#      and its uncommitted checkout. A row whose tab or session
#      is gone is re-queued on the same checkout (RESUME prompt), and release launches it in
#      queue order under the worker cap. A task that needs a quiet window (an explicit
#      `quiet_window` flag, or a "PRECONDITIONS: no active workers" brief such as the history
#      rewrite) resumes only when no other worker is live, and other paused rows wait while it
#      is active. Cancelled rows are terminal, so they are never read.
#   3. CoS itself: when its own session stopped on the limit, it is resumed the same way, only
#      through the inject guard (tab_send, never over a draft), with one line that says it
#      resumed and lists the workers. That line doubles as the summary CoS sees on its next
#      turn. When CoS was not limited, the summary goes by tell_cos_direct.
#   4. A context-limit stop (auto-compaction normally prevents it) restarts the worker once in
#      the same checkout with a RESUME prompt that names the old session; a second one tells
#      CoS and leaves the row for the owner.
# Kill switch: SWITCHBOARD_LIMIT_RESUME=off. Amp rows have no Claude session id and are untouched.

USAGE_LIMIT_STAMP = STATE_DIR / "usage-limit.json"
LIMIT_RESUME_GRACE = timedelta(seconds=int(os.environ.get("SWITCHBOARD_LIMIT_RESUME_GRACE_SECONDS") or 120))
MAX_CONTEXT_RESTARTS = 1
MAX_LIMIT_RESUMES = 2   # automatic resumes per streak of limit stops; the next stop is left for the owner
CONTEXT_SETTLE = timedelta(minutes=3)   # a fresh context error may still be compacting or retrying
QUIET_BRIEF = re.compile(r"preconditions?:?[^\n]{0,40}\bno (other )?(active|live) workers", re.I)


def limits_disabled() -> bool:
    return os.environ.get("SWITCHBOARD_LIMIT_RESUME", "").strip().lower() in {"off", "0", "false"}


def usage_limit_stamp() -> dict[str, Any]:
    try:
        data = json.loads(USAGE_LIMIT_STAMP.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_usage_limit_stamp(data: dict[str, Any]) -> None:
    USAGE_LIMIT_STAMP.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_LIMIT_STAMP.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _os_replace(tmp, USAGE_LIMIT_STAMP)


def usage_limit_until(stamp: dict[str, Any] | None = None) -> datetime | None:
    """The reset time while a usage limit is in effect (plus the grace), else None."""
    if limits_disabled():
        return None
    resets = ledger.parse_iso((stamp if stamp is not None else usage_limit_stamp()).get("resets_at"))
    return resets if resets is not None and now() < resets + LIMIT_RESUME_GRACE else None


def session_limit(session_id: str) -> dict[str, Any] | None:
    """The usage or context limit a session is stopped on, else None. A network stop
    is not a limit: handle_network_stops reads it."""
    if not CLAUDE_SESSION_ID.match(session_id or ""):
        return None
    found = usage_limits.classify_transcript(claude_transcript_path(session_id))
    return found if found and found["kind"] in {"usage", "context"} else None


def requires_quiet_window(task: dict[str, Any]) -> bool:
    return bool(task.get("quiet_window")) or bool(QUIET_BRIEF.search(str(task.get("prompt") or "")))


def limit_resume_text(task_id: str, reset: datetime) -> str:
    return (f"Your Claude usage limit reset at {usage_limits.pacific(reset)}. Continue task {task_id} from where it stopped "
            f"when the limit was reached; do not repeat work that is already complete, and finish with report-to-cos.sh as before.")


def requeue_on_checkout(task: dict[str, Any], reason: str, summary: str, listing: list[dict[str, Any]],
                        agents: dict[str, dict[str, Any]], **counters: int) -> bool:
    """Close the row's tab (when it is open and provably its own) and queue its next attempt on
    the same checkout with a RESUME prompt. False when the tab would not close."""
    ws = resolve_live_tab(task, listing, agents)
    if ws is not None and not protected(ws, task) \
            and not cmux_close(str(ws.get("ref") or ""), str(ws.get("id") or ""), f"sweep: {reason} limit {task['id']}"):
        log(f"{task['id']}: could not close {ws.get('ref')}; not relaunching until it closes")
        return False

    def mutate(payload, tid=task["id"], old=dict(task)):
        row = ledger.find_task(payload, tid)
        if row is None or row.get("status") not in {"routing", "active"} or row.get("attempt_id") != old.get("attempt_id"):
            return None
        ledger.begin_attempt(row, status="queued", summary=summary)
        row.update(requeued_at=ledger.now_iso(), resume_reason=reason,
                   resume_worktree=str(old.get("worktree") or worktree_path(tid)), resume_branch=str(old.get("branch") or ""),
                   resume_from_attempt=old.get("attempt_id"), resume_session=str(old.get("session_id") or old.get("thread_id") or ""))
        for key, step in counters.items():
            row[key] = int(row.get(key) or 0) + step
        return dict(row)

    return bool(ledger.locked_mutation(mutate))


def _limit_rows() -> list[dict[str, Any]]:
    rows = [t for t in ledger.load_payload()["tasks"]
            if t.get("status") == "active" and CLAUDE_SESSION_ID.match(str(t.get("session_id") or t.get("thread_id") or ""))]
    return sorted(rows, key=lambda t: str(t.get("created_at") or ""))


def _update_row(task_id: str, attempt_id: Any, change: Any) -> dict[str, Any] | None:
    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is None or row.get("attempt_id") != attempt_id or row.get("status") != "active":
            return None
        change(row)
        row["updated_at"] = ledger.now_iso()
        return dict(row)
    return ledger.locked_mutation(mutate)


def handle_limits(cfg: dict[str, Any]) -> dict[str, Any]:
    if limits_disabled():
        return {"skipped": "SWITCHBOARD_LIMIT_RESUME=off"}
    agents = ledger.live_agents()
    listing = cmux_workspaces()
    if not agents or not listing:
        # A read outage, not a mass stop: judging now would close or relaunch live workers.
        return {"skipped": agentless_skip("limits", listing_empty=not listing)}
    stamp = usage_limit_stamp()
    out: dict[str, Any] = {"paused": [], "resumed": [], "relaunched": [], "waiting": [], "retry": [],
                           "cleared": [], "capped": [], "context_restarted": [], "context_told": []}

    def extend(resets: datetime) -> None:
        """Push the window's reset later. A reset no later than the current one changes
        nothing, so a row still held after the reset never restarts the window's flags."""
        current = ledger.parse_iso(stamp.get("resets_at"))
        if current is not None and resets <= current:
            return
        if current is None or current + LIMIT_RESUME_GRACE <= now():
            stamp.update(detected_at=ledger.now_iso(), paused=[], pause_told=False, resume_told=False, cos={},
                         resumed=[], relaunched=[], quiet_told=[])
        stamp["resets_at"] = resets.isoformat(timespec="seconds")

    # 1. Detect: pause usage-limited rows, restart context-limited ones once.
    for task in _limit_rows():
        session = str(task.get("session_id") or task.get("thread_id") or "")
        found = session_limit(session)
        held = task.get("usage_limit") or {}
        if found is None:
            if held:   # the session moved on (someone typed into it): nothing left to resume
                _update_row(task["id"], task.get("attempt_id"), lambda r: (r.pop("usage_limit", None), r.pop("paused", None)))
                out["cleared"].append(task["id"])
            if task.get("usage_streak"):
                tail = claude_transcript_tail(session)
                if tail is not None and tail["last_role"] == "assistant":   # a real answer: the streak is over
                    _update_row(task["id"], task.get("attempt_id"), lambda r: r.update(usage_streak=0))
            continue
        error_at = found["at"].isoformat(timespec="seconds") if found.get("at") else ""
        if found["kind"] == "usage":
            if error_at and error_at in {held.get("error_at"), task.get("usage_limit_resumed_from")}:
                if held:
                    extend(ledger.parse_iso(held.get("resets_at")) or found["resets_at"])
                continue
            resets = found["resets_at"]
            streak = int(task.get("usage_streak") or 0)
            capped = streak >= MAX_LIMIT_RESUMES
            label = (f"paused: usage limit, left for the owner after {streak} automatic resumes" if capped
                     else f"paused: usage limit, resumes at {usage_limits.pacific(resets)}")

            def pause(row, resets=resets, error_at=error_at, label=label, kind=found.get("limit_type") or "", capped=capped):
                row["usage_limit"] = {"error_at": error_at, "resets_at": resets.isoformat(timespec="seconds"),
                                      "detected_at": ledger.now_iso(), "limit_type": kind, "capped": capped}
                row["paused"] = label

            if _update_row(task["id"], task.get("attempt_id"), pause):
                extend(resets)
                stamp.setdefault("paused", [])
                if task["id"] not in stamp["paused"]:
                    stamp["paused"].append(task["id"])
                log(f"{task['id']}: {label}")
                if capped:
                    lead = task_lead(task["id"], str(task.get("workstream") or ""))
                    told = tell_cos_direct(f"{lead} worker stopped on the usage limit again right after {streak} automatic "
                                           f"resumes, so the sweep leaves it paused. Type into its tab to resume it by hand.")
                    out["capped"].append({"task_id": task["id"], "origin_told": bool(told.get("ok"))})
                else:
                    out["paused"].append({"task_id": task["id"], "resets_at": resets.isoformat(timespec="seconds")})
            continue
        # Context limit: the session cannot go on. Restart once on the same checkout.
        if task.get("context_limit_told") == error_at:
            continue
        if str((agents.get(session) or {}).get("state") or "") not in IDLE_STATES \
                or (found.get("at") and now() - found["at"] < CONTEXT_SETTLE):
            continue   # still compacting, retrying, or someone is typing /compact: look again next tick
        lead = task_lead(task["id"], str(task.get("workstream") or ""))
        if int(task.get("context_restarts") or 0) < MAX_CONTEXT_RESTARTS and task.get("prompt") and task.get("worktree"):
            summary = (f"re-queued by sweep: attempt {task.get('attempt_id')} stopped at the context limit; "
                       f"the next attempt carries on in {task.get('worktree')}")
            if requeue_on_checkout(task, "context", summary, listing, agents, context_restarts=1):
                log(f"{task['id']}: {summary}")
                told = tell_cos_direct(f"{lead} worker stopped at the context limit. Restarted once in the same checkout, "
                                       f"which keeps its work; the new attempt reads what the old one left.")
                out["context_restarted"].append({"task_id": task["id"], "origin_told": bool(told.get("ok"))})
            continue
        told = tell_cos_direct(f"{lead} worker stopped at the context limit again after its one automatic restart. "
                               f"Its checkout {task.get('worktree')} keeps the work; land it with dispatch.py finish "
                               f"--task-id {task['id']}, or re-dispatch with a narrower brief.")
        _update_row(task["id"], task.get("attempt_id"), lambda r, e=error_at: r.update(context_limit_told=e))
        out["context_told"].append({"task_id": task["id"], "origin_told": bool(told.get("ok"))})

    # CoS's own session: read the same way, resumed in step 3.
    cos = owner_tabs.cos_tab(listing, owner_tabs.agents_by_workspace(agents)) if listing else {}
    cos_session = str((cos or {}).get("thread_id") or "")
    cos_found = session_limit(cos_session) if cos_session else None
    if cos_found and cos_found["kind"] == "usage":
        error_at = cos_found["at"].isoformat(timespec="seconds") if cos_found.get("at") else ""
        held_cos = stamp.get("cos") or {}
        if error_at and error_at not in {held_cos.get("error_at"), stamp.get("cos_resumed_from")}:
            extend(cos_found["resets_at"])
            stamp["cos"] = {"error_at": error_at, "session": cos_session, "ref": cos.get("ref"),
                            "resets_at": cos_found["resets_at"].isoformat(timespec="seconds")}
            out["cos_paused"] = True

    resets = ledger.parse_iso(stamp.get("resets_at"))
    paused_ids = [t["id"] for t in _limit_rows() if t.get("usage_limit")]
    if out["paused"] and resets is not None and resets > now() and not stamp.get("pause_told"):
        leads = ", ".join(task_lead(tid, str((ledger.find_task(ledger.load_payload(), tid) or {}).get("workstream") or ""))
                          for tid in paused_ids)
        told = tell_cos_direct(f"⏸️ Claude usage limit: {len(paused_ids)} worker(s) paused until {usage_limits.pacific(resets)}: "
                               f"{leads}. The sweep resumes them in place after the reset and holds new launches until then.")
        stamp["pause_told"] = bool(told.get("ok"))
        out["pause_told"] = stamp["pause_told"]

    # 2. Resume after the reset.
    quiet_waiting: list[str] = []
    if resets is not None and now() >= resets + LIMIT_RESUME_GRACE:
        payload = ledger.load_payload()
        live = live_workers(payload)
        for task in [t for t in _limit_rows() if t.get("usage_limit")]:
            if task["usage_limit"].get("capped"):
                continue   # left for the owner
            if continuation_heir(task, payload) is not None:
                continue   # its continuation owns the work; a resume would put two workers on one job
            others = [t for t in live if t["id"] != task["id"] and t.get("status") in {"routing", "active"}]
            if requires_quiet_window(task):
                busy = [t for t in others if not t.get("usage_limit")]
                if busy:
                    out["waiting"].append({"task_id": task["id"], "waiting_on": [t["id"] for t in busy]})
                    quiet_waiting.append(task["id"])
                    continue
            else:
                quiet = [t for t in others if requires_quiet_window(t)]
                if quiet:
                    out["waiting"].append({"task_id": task["id"], "waiting_on": [t["id"] for t in quiet]})
                    continue
            held = task["usage_limit"]
            row_reset = ledger.parse_iso(held.get("resets_at")) or resets
            if now() < row_reset + LIMIT_RESUME_GRACE:
                continue
            session = str(task.get("session_id") or task.get("thread_id") or "")
            ws = resolve_live_tab(task, listing, agents)
            if ws is not None and session in agents:
                if tab_send(str(ws.get("ref") or ""), limit_resume_text(task["id"], row_reset)):
                    def resumed(row, error_at=held.get("error_at")):
                        row.pop("usage_limit", None)
                        row.pop("paused", None)
                        row.update(usage_limit_resumed_from=error_at, usage_limit_resumed_at=ledger.now_iso(),
                                   usage_resumes=int(row.get("usage_resumes") or 0) + 1,
                                   usage_streak=int(row.get("usage_streak") or 0) + 1)
                    _update_row(task["id"], task.get("attempt_id"), resumed)
                    for seen in live:   # the next row's quiet check must see this one as running
                        if seen["id"] == task["id"]:
                            seen.pop("usage_limit", None)
                    log(f"{task['id']}: resumed in place after the usage limit ({ws.get('ref')})")
                    out["resumed"].append(task["id"])
                else:
                    out["retry"].append(task["id"])   # busy or a draft in the box: next tick
                continue
            summary = (f"re-queued by sweep: paused on the usage limit and its tab or session was gone at the reset; "
                       f"the next attempt carries on in {task.get('worktree')}")
            if requeue_on_checkout(task, "usage", summary, listing, agents, usage_resumes=1, usage_streak=1):
                log(f"{task['id']}: {summary}")
                out["relaunched"].append(task["id"])

        for tid in quiet_waiting:
            if tid not in (stamp.get("quiet_told") or []):
                waiting_on = next(w["waiting_on"] for w in out["waiting"] if w["task_id"] == tid)
                name = task_lead(tid, str((ledger.find_task(payload, tid) or {}).get("workstream") or ""))
                told = tell_cos_direct(f"{name} needs a quiet window and waits for {len(waiting_on)} live worker(s) to "
                                       f"finish before it resumes; new Claude launches are held until then.")
                if told.get("ok"):
                    stamp["quiet_told"] = list(stamp.get("quiet_told") or []) + [tid]

        # 3. CoS: resumed through the inject guard, with the one summary line.
        # The summary covers the whole window, so a CoS line deferred by a draft still names every worker.
        for key in ("resumed", "relaunched"):
            stamp[key] = list(stamp.get(key) or []) + [tid for tid in out[key] if tid not in (stamp.get(key) or [])]
        names = lambda ids: ", ".join(task_lead(tid, str((ledger.find_task(ledger.load_payload(), tid) or {}).get("workstream") or ""))
                                      for tid in ids)
        parts = []
        if stamp["resumed"]:
            parts.append(f"resumed in place: {names(stamp['resumed'])}")
        if stamp["relaunched"]:
            parts.append(f"relaunched on their checkouts: {names(stamp['relaunched'])}")
        if out["waiting"]:
            parts.append(f"waiting for a quiet window: {names([w['task_id'] for w in out['waiting']])}")
        workers = ("Workers " + "; ".join(parts) + ".") if parts else "No worker was paused."
        cos_held = stamp.get("cos") or {}
        if cos_held.get("error_at") and not cos_held.get("resumed_at"):
            still = session_limit(cos_session) if cos_session else None
            if still is None or still["kind"] != "usage":
                stamp["cos"] = {}   # CoS moved on already: someone typed into it and it answered
            elif still["resets_at"] > now():
                pass                # a newer limit read: step 1 extended the window, so wait for it
            else:
                ref = str(cos.get("ref") or cos_held.get("ref") or "")
                line = (f"[ROUTER REPORT] Claude usage limit reset at {usage_limits.pacific(resets)}. CoS resumed: continue what "
                        f"you were doing when the limit was reached. {workers}")
                if ref and tab_send(ref, line):   # the inject guard: never over a draft, never mid-turn
                    stamp["cos"] = {**cos_held, "resumed_at": ledger.now_iso()}
                    stamp["cos_resumed_from"] = cos_held.get("error_at")
                    stamp["resume_told"] = True
                    out["cos_resumed"] = True
                else:
                    out["cos_retry"] = True     # a draft or a dialog in the CoS box: next tick
        if (out["resumed"] or out["relaunched"]) and not stamp.get("resume_told") and not (stamp.get("cos") or {}).get("error_at"):
            told = tell_cos_direct(f"▶️ Claude usage limit reset at {usage_limits.pacific(resets)}. {workers}")
            stamp["resume_told"] = bool(told.get("ok"))
    if stamp or quiet_waiting:
        stamp["quiet_waiting"] = quiet_waiting   # release_queued holds Claude launches while this is non-empty
        write_usage_limit_stamp(stamp)
    return {k: v for k, v in out.items() if v}


# --- network stops ------------------------------------------------------------------------------
#
# A network drop can end the turn of every live Claude worker with "API Error: Can't reach the
# API server — check your internet or DNS (ENOTFOUND)". Claude Code has already retried, so each
# sits at an empty prompt holding its worker slot while queued tasks wait. handle_network_stops
# resumes them every sweep tick:
#   1. Each live Claude worker's transcript tail is read (usage_limits.classify_transcript); a
#      last turn that ended on a transient API error (the network, a timeout, 5xx, 529
#      overloaded) marks the row `network_stop`. With no readable transcript, the tab's screen
#      is read instead (usage_limits.classify_screen). nudge_idle and the wedge flag leave a
#      marked row to this step.
#   2. Once the error is NETWORK_SETTLE old and the API answers again (api_reachable: one HTTPS
#      request to the API host; any answer below 500 counts), the worker gets one continue line
#      through the steer path (cmux agent message), at most MAX_NETWORK_RESUMES times per
#      attempt, each NETWORK_BACKOFF after the one before. CoS hears one line per tick that
#      resumed anyone, and one line when a worker reaches the cap.
#   3. CoS's own session: when it stopped on the same kind of error, the summary goes in through
#      the inject guard (tab_send, never over a draft) as its continue line instead.
# Kill switches: SWITCHBOARD_NETWORK_RESUME=off, and SWITCHBOARD_LIMIT_RESUME=off turns this off too.

def _env_seconds(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:   # a bad value must not break the import of all of dispatch.py
        return default


NETWORK_SETTLE = timedelta(seconds=_env_seconds("SWITCHBOARD_NETWORK_SETTLE_SECONDS", 90))
MAX_NETWORK_RESUMES = 4
# Wait after resume n (index n-1) before resume n+1. A sweep tick is 10 to 15 minutes, so the
# first retry comes on the next tick at the earliest.
NETWORK_BACKOFF = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30))
NETWORK_STAMP = STATE_DIR / "network-resume.json"
API_PROBE_TIMEOUT = 8.0


def network_resume_disabled() -> bool:
    return limits_disabled() or os.environ.get("SWITCHBOARD_NETWORK_RESUME", "").strip().lower() in {"off", "0", "false"}


def api_reachable() -> bool:
    """Does the Claude API answer? One HEAD request to the API host (ANTHROPIC_BASE_URL
    when set). Any HTTP answer below 500 means the network and the API are back (a 401 or
    404 for an unauthenticated probe is expected); a DNS failure, a refused or timed-out
    connection, or a 5xx means not yet. The request carries no credentials or user data.
    Never reaches the network under SWITCHBOARD_TEST_SANDBOX=1 (tests patch this)."""
    if os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
        return os.environ.get("SWITCHBOARD_API_PROBE", "").strip().lower() == "up"
    import urllib.error
    import urllib.request
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    request = urllib.request.Request(base + "/v1/models", method="HEAD", headers={"User-Agent": "switchboard-dispatch"})
    try:
        with urllib.request.urlopen(request, timeout=API_PROBE_TIMEOUT):
            return True
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def network_stamp() -> dict[str, Any]:
    try:
        data = json.loads(NETWORK_STAMP.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_network_stamp(data: dict[str, Any]) -> None:
    NETWORK_STAMP.parent.mkdir(parents=True, exist_ok=True)
    tmp = NETWORK_STAMP.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _os_replace(tmp, NETWORK_STAMP)


def session_network_stop(session_id: str, ref: str = "") -> dict[str, Any] | None:
    """{"at", "text", "code", "error_at", "via"} when the session's last turn ended on a
    transient API error, else None. The transcript decides when it is readable; only a
    session with no readable transcript falls back to its tab's screen."""
    path = claude_transcript_path(session_id) if CLAUDE_SESSION_ID.match(session_id or "") else None
    if path is not None:
        found = usage_limits.classify_transcript(path)
        if not found or found["kind"] != "network":
            return None
        at = found.get("at")
        return {"at": at, "text": found.get("text") or "", "code": found.get("code") or "",
                "error_at": at.isoformat(timespec="seconds") if at else "", "via": "transcript"}
    if not ref.startswith("workspace:"):
        return None
    found = usage_limits.classify_screen(read_tab_screen(ref))
    if not found or found["kind"] != "network":
        return None
    digest = hashlib.sha1(found["line"].encode("utf-8")).hexdigest()[:12]
    return {"at": None, "text": found["line"], "code": found.get("code") or "", "error_at": f"screen:{digest}", "via": "screen"}


def network_resume_text(task_id: str, stop: dict[str, Any]) -> str:
    code = f" ({stop['code']})" if stop.get("code") else ""
    return (f"The Claude API answers again after a network error{code} ended your last turn. Continue task {task_id} from "
            f"where it stopped; do not repeat work that is already complete, and finish with report-to-cos.sh as before.")


def network_resumes_of(task: dict[str, Any]) -> dict[str, Any]:
    """This attempt's resume counter; a new attempt starts at zero."""
    held = task.get("network_resumes") or {}
    return held if held.get("attempt_id") == task.get("attempt_id") else {}


def network_owned(task: dict[str, Any]) -> bool:
    """handle_network_stops owns this row, so the idle nudge and the wedge flag leave it
    alone: it is marked, the step is on, and either resumes remain or the step has not
    yet told CoS it reached the cap (so the 4th resume never gets an idle nudge right
    behind it in the same sweep). With the kill switch on, nobody would clear the mark,
    so the row goes back to the nudge and the wedge flag."""
    stop = task.get("network_stop") or {}
    if not stop or network_resume_disabled():
        return False
    resumes = network_resumes_of(task)
    return int(resumes.get("count") or 0) < MAX_NETWORK_RESUMES or resumes.get("capped_told") != stop.get("error_at")


def handle_network_stops(cfg: dict[str, Any], probe: Any = None) -> dict[str, Any]:
    if network_resume_disabled():
        return {"skipped": "SWITCHBOARD_NETWORK_RESUME=off or SWITCHBOARD_LIMIT_RESUME=off"}
    agents = ledger.live_agents()
    listing = cmux_workspaces()
    if not agents or not listing:
        return {"skipped": agentless_skip("network stops", listing_empty=not listing)}
    probe = probe or api_reachable
    api_up: list[bool] = []

    def reachable() -> bool:   # one probe per tick, and only when something waits on it
        if not api_up:
            api_up.append(bool(probe()))
        return api_up[0]

    out: dict[str, Any] = {"detected": [], "resumed": [], "waiting_api": [], "backoff": [], "retry": [],
                           "capped": [], "cleared": []}
    codes: list[str] = []
    for task in _limit_rows():
        if task.get("usage_limit"):
            continue   # handle_limits owns a usage-limited row
        if continuation_heir(task, ledger.load_payload()) is not None:
            continue   # a continue line would put two workers on the work its continuation owns
        session = str(task.get("session_id") or task.get("thread_id") or "")
        stop = session_network_stop(session, str(task.get("workspace_ref") or ""))
        held = task.get("network_stop") or {}
        if stop is None:
            if held:   # the session moved on: a resume landed, or someone typed into it
                _update_row(task["id"], task.get("attempt_id"), lambda r: r.pop("network_stop", None))
                out["cleared"].append(task["id"])
            continue
        if stop["at"] is not None and now() - stop["at"] < NETWORK_SETTLE:
            continue   # Claude Code may still be retrying on its own
        if held.get("error_at") != stop["error_at"]:
            def mark(row, stop=stop):
                row["network_stop"] = {"error_at": stop["error_at"], "text": stop["text"][:200], "code": stop["code"],
                                       "via": stop["via"], "detected_at": ledger.now_iso()}
            if _update_row(task["id"], task.get("attempt_id"), mark):
                log(f"{task['id']}: last turn ended on a network error ({stop['code'] or stop['text'][:60]})")
                out["detected"].append(task["id"])
        resumes = network_resumes_of(task)
        count = int(resumes.get("count") or 0)
        lead = task_lead(task["id"], str(task.get("workstream") or ""))
        if count >= MAX_NETWORK_RESUMES:
            if resumes.get("capped_told") != stop["error_at"]:
                told = tell_cos_direct(f"{lead} worker stopped on a network error again after {count} automatic resumes "
                                       f"this attempt ({stop['text'][:120]}). The sweep leaves it for the owner: type "
                                       f"continue into its tab ({task.get('workspace_ref') or 'no worker tab'}) once the "
                                       f"network is steady.")
                if told.get("ok"):
                    _update_row(task["id"], task.get("attempt_id"),
                                lambda r, e=stop["error_at"]: r.update(network_resumes={**network_resumes_of(r), "capped_told": e}))
                out["capped"].append({"task_id": task["id"], "origin_told": bool(told.get("ok"))})
            continue
        last = ledger.parse_iso(resumes.get("last_at"))
        if count and last and now() - last < NETWORK_BACKOFF[min(count, len(NETWORK_BACKOFF)) - 1]:
            out["backoff"].append(task["id"])
            continue
        if not reachable():
            out["waiting_api"].append(task["id"])
            continue
        ws = resolve_live_tab(task, listing, agents)
        if ws is None:
            continue   # no tab holds it: the dead-worker rule relaunches it on its checkout
        sent = cmux_agent_message(str(ws.get("ref") or ""), network_resume_text(task["id"], stop),
                                  sender="switchboard network resume")
        # A timeout may come after cmux queued the line: count it as sent, never type it twice.
        if not (sent.get("ok") or sent.get("ambiguous") or "timed out" in str(sent.get("reason") or "")):
            log(f"{task['id']}: network resume refused by {ws.get('ref')} ({sent.get('reason')}); next tick")
            out["retry"].append(task["id"])
            continue

        def resumed(row, error_at=stop["error_at"]):
            held_resumes = network_resumes_of(row)
            row["network_resumes"] = {"attempt_id": row.get("attempt_id"), "count": int(held_resumes.get("count") or 0) + 1,
                                      "last_at": ledger.now_iso(), "last_error_at": error_at}
            if row.get("wedged_at"):   # a resumed worker is not stalled; it may be flagged again later
                for key in ("wedged_at", "wedged", "recovery_action"):
                    row.pop(key, None)
        _update_row(task["id"], task.get("attempt_id"), resumed)
        log(f"{task['id']}: resumed after a network error ({ws.get('ref')}, resume {count + 1}/{MAX_NETWORK_RESUMES})")
        out["resumed"].append(task["id"])
        if stop["code"] and stop["code"] not in codes:
            codes.append(stop["code"])

    # CoS's own session, read the same way. Its summary line is its continue line.
    stamp = network_stamp()
    cos = owner_tabs.cos_tab(listing, owner_tabs.agents_by_workspace(agents)) or {}
    cos_session, cos_ref = str(cos.get("thread_id") or ""), str(cos.get("ref") or "")
    cos_stop = session_network_stop(cos_session, cos_ref) if cos_session else None
    cos_held = stamp.get("cos") or {}
    cos_due = bool(cos_stop and cos_ref and (cos_stop["at"] is None or now() - cos_stop["at"] >= NETWORK_SETTLE)
                   and cos_held.get("resumed_from") != cos_stop["error_at"])
    names = ", ".join(task_lead(tid, str((ledger.find_task(ledger.load_payload(), tid) or {}).get("workstream") or ""))
                      for tid in out["resumed"])
    why = f"a network error ({', '.join(codes)})" if codes else "a network error"
    workers = f"Resumed with one continue line each: {names}." if names else "No worker needed a resume."
    if cos_due and reachable():
        line = (f"[ROUTER REPORT] The Claude API answers again after {why} stopped this CoS turn. CoS resumed: continue "
                f"what you were doing. {workers}")
        if tab_send(cos_ref, line):   # the inject guard: never over a draft, never mid-turn
            stamp["cos"] = {"resumed_from": cos_stop["error_at"], "resumed_at": ledger.now_iso()}
            write_network_stamp(stamp)
            out["cos_resumed"] = True
        else:
            out["cos_retry"] = True   # a draft or a dialog in the CoS box: next tick
    if out["resumed"] and not out.get("cos_resumed"):
        told = tell_cos_direct(f"🌐 The Claude API answers again after {why} ended {len(out['resumed'])} worker turn(s). "
                               f"{workers}")
        out["origin_told"] = bool(told.get("ok"))
    return {k: v for k, v in out.items() if v}


def command_sweep(args: argparse.Namespace) -> int:
    cfg = config()
    run([sys.executable, str(SCRIPTS / "running-task-state.py"), "expire-leases"], timeout=60)
    requeued = requeue_orphaned_routes()
    retypes = retype_stalled_launches()    # a tab back at a shell gets its launch command again before liveness is judged
    refs_healed = heal_live_refs(cmux_workspaces(), ledger.live_agents())   # before liveness: a stale ref is not a dead worker
    try:   # before release (which holds Claude launches during a usage limit) and before nudge/wedge (which skip paused rows)
        limits = handle_limits(cfg)
    except Exception as error:  # noqa: BLE001  (a transcript read must never stop the sweep)
        log(f"limit handling failed: {error}")
        limits = {"error": str(error)}
    try:   # before nudge/wedge, which leave a row this step marked to it
        network = handle_network_stops(cfg)
    except Exception as error:  # noqa: BLE001  (a transcript read or probe must never stop the sweep)
        log(f"network stop handling failed: {error}")
        network = {"error": str(error)}
    abandoned = fail_abandoned_rows(cfg)   # before release: a relaunched row is queued for this very pass, and frees its slot
    reports_superseded = settle_superseded_reports()
    prerequisites_followed = follow_replaced_prerequisites()   # before release: a row put back is queued for this very pass
    release = run([sys.executable, str(SCRIPTS / "dispatch.py"), "release"], timeout=600).stdout.strip()
    stuck = flag_stuck_routes(cfg)        # after release: a queued row it could launch is launching, not stuck
    holds_told = report_launch_holds(cfg)
    settled = settle_unknown_reports()
    conflict_windows = expire_conflict_windows(cfg)   # before reap: a lapsed window's row is terminal for this very pass
    reaped = reap_all(cfg)
    pruned = prune_orphan_worktrees()
    receipts = reconcile_receipts(ledger.load_payload())
    permission_prompts = relay_permission_prompts()   # before nudge_idle: a relayed prompt needs no generic line
    nudged = nudge_idle(cfg)
    wedged = flag_wedged_workers(cfg)   # after nudge_idle: an idle worker is nudged, a busy-looking silent one is flagged
    strays = flag_stray_writes()
    idle_owners = close_idle_owners(cfg)
    steers = deliver_queued_steers(cfg=cfg)   # backstop for the ack/heartbeat spawn; reports overdue queued steers
    capacity = capacity_tick(cfg)   # report only; nothing above waited on it
    relinked = winlink.heal_shared_links(REPO_ROOT)   # Windows: hard links a non-relinking writer left stale
    # Last: after reap and release, so a worker that just ended no longer counts
    # and one that just launched does. Releases the hold once nothing is live.
    # Only workers that are working hold it: one parked on the owner, or idle in its
    # tab, lets the laptop sleep.
    awake = keep_awake.sync_rows(live_workers(ledger.load_payload()), tail=ledger.claude_tail)
    print(json.dumps({"requeued": requeued, "launch_retypes": retypes, "refs_healed": refs_healed, "limits": limits,
                      "network_stops": network, "launch_holds_told": holds_told, "abandoned": abandoned, "reports_superseded": reports_superseded,
                      "prerequisites_followed": prerequisites_followed, "release": json.loads(release or "{}"),
                      "stuck": stuck, "reports_settled": settled, "conflict_windows": conflict_windows, "reap": reaped,
                      "orphan_worktrees_pruned": pruned["removed"],
                      "orphan_worktrees_parked": pruned["parked"], "receipts_reconciled": receipts, "idle": nudged,
                      "wedged": wedged, "permission_prompts": permission_prompts, "stray_writes": strays, "idle_owner_close": idle_owners, "queued_steers": steers,
                      "capacity": capacity, "shared_links_relinked": relinked, "keep_awake": awake}))
    return 0


# --- hourly auto-push of main ---------------------------
#
# An opt-in hourly push. This is the
# one automated path allowed to push; workers and surfaces still never push by
# hand. command-center-loop.sh runs `dispatch.py auto-push` every tick (10 min);
# the gate below decides whether this tick pushes:
#   1. at least AUTO_PUSH_INTERVAL since the last *attempt* (push or divergence)
#      recorded in system/state/auto-push.json; a tick that finds nothing to
#      push does not count as an attempt, so a commit landing a minute later
#      is not held for an hour;
#   2. no landing in progress: the landing lock command_finish holds while it
#      rebases and fast-forwards is free (taken non-blocking here), and no
#      rebase-merge / rebase-apply directory exists in main or any worktree;
#   3. after `git fetch origin main`, main is strictly ahead of origin/main.
#      Behind or diverged is never pushed and never forced: one line to CoS
#      instead, repeated at most once per interval;
#   4. none of the gitignored sensitive directories is tracked (local/, people/,
#      notes/sensitive, notes/chrome-tabs, .state, system/state beyond its
#      README; the to-do list, its archive and its backups count as sensitive),
#      and no unpushed commit carries a private workstream's files
#      (system/config/private-workstreams.txt, beyond CONTEXT.md and config.yaml).
#      A tracked sensitive path refuses the push and tells CoS;
#   5. no unpushed commit adds a value from local/private-identifiers.txt
#      (private_identifiers.py). A hit, or a list known to exist that cannot be
#      read, refuses the push and tells CoS, naming the file and kind only.
# Fetch and push run while holding the landing lock every `finish` waits on, so
# they never prompt for credentials and never outlive AUTO_PUSH_GIT_TIMEOUT.
# Only `main` is ever pushed (`git push origin main:main`, never --force, never
# another branch). Success is silent beyond the log and one "pushed N commits"
# line queued for the next-up digest's Updates section; failure and divergence
# send one line to CoS through report-to-cos.sh like check-state-inventory.py.
# Kill switch: AUTO_PUSH_DISABLED=1 in command-center-loop.sh.

AUTO_PUSH_STAMP = STATE_DIR / "auto-push.json"
AUTO_PUSH_LOG = STATE_DIR / "logs" / "auto-push.log"
AUTO_PUSH_INTERVAL = timedelta(minutes=int(os.environ.get("SWITCHBOARD_AUTO_PUSH_MINUTES") or 60))
AUTO_PUSH_REMOTE, AUTO_PUSH_BRANCH = "origin", "main"
AUTO_PUSH_GIT_TIMEOUT = int(os.environ.get("SWITCHBOARD_AUTO_PUSH_GIT_TIMEOUT") or 300)
SENSITIVE_PREFIXES = ("local", "people", "notes/sensitive", "notes/chrome-tabs", ".state", "system/state")
# The one negated pattern in .gitignore under `system/state/**`. The to-do list,
# its archive and its backups live only on this machine (.gitignore).
SENSITIVE_TRACKED_ALLOWED = re.compile(r"^system/state/README\.md$")
# Folders whose ignored files must never be committed: a tracked file under one of them that .gitignore
# ignores was forced in, and is refused. Empty by default; list confidential folders here.
CONFIDENTIAL_IGNORED_PREFIXES: tuple[str, ...] = ()


def tracked_confidential_paths() -> list[str]:
    """Tracked files under CONFIDENTIAL_IGNORED_PREFIXES that .gitignore ignores (a forced add)."""
    if not CONFIDENTIAL_IGNORED_PREFIXES:
        return []
    listed = git("ls-files", "-z", "-c", "-i", "--exclude-standard", "--", *CONFIDENTIAL_IGNORED_PREFIXES, check=False)
    return [p for p in listed.stdout.split("\0") if p]


def unpushed_confidential_paths(rev_range: str) -> list[str]:
    """Ignored session files a commit in `rev_range` adds or changes, even when a later commit removed them."""
    if not CONFIDENTIAL_IGNORED_PREFIXES:
        return []
    named = git("log", "-z", "-m", "--full-history", "--format=", "--name-only", "--no-renames", "--diff-filter=AM", rev_range,
                "--", *CONFIDENTIAL_IGNORED_PREFIXES, check=False)
    paths = sorted({p.strip() for p in named.stdout.split("\0") if p.strip()})
    if not paths:
        return []
    probe = run(["git", "-C", str(REPO_ROOT), "check-ignore", "--no-index", "--stdin", "-z"], input="\0".join(paths) + "\0").stdout
    return sorted(p for p in probe.split("\0") if p)


@contextmanager
def landing_lock(blocking: bool = True) -> Iterator[bool]:
    """Serialize `finish` landings on main and let auto-push see one in flight.
    Yields True when held. Non-blocking callers get False instead of waiting."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    with (LOCKS / "landing.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def rebase_in_progress() -> list[str]:
    """rebase-merge / rebase-apply directories in main's .git or any worktree's."""
    git_dir = Path(git("rev-parse", "--git-common-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = REPO_ROOT / git_dir
    found = []
    for base in [git_dir, *sorted(p for p in (git_dir / "worktrees").glob("*") if p.is_dir())]:
        found += [str(base / name) for name in ("rebase-merge", "rebase-apply") if (base / name).exists()]
    return found


def tracked_sensitive_paths() -> list[str]:
    """Tracked files under the gitignored sensitive directories, minus the
    .gitignore exceptions. Non-empty means: do not push."""
    listed = git("ls-files", "--", *SENSITIVE_PREFIXES, check=False).stdout.splitlines()
    sensitive = [p for p in listed if p and not SENSITIVE_TRACKED_ALLOWED.match(p)]
    private = private_workstreams.slugs(REPO_ROOT)
    if private:  # -z: git quotes a non-ASCII path otherwise, and a quoted path would not match
        listed = git("ls-files", "-z", "--", *private_workstreams.gate_pathspecs(private), check=False).stdout.split("\0")
        sensitive += [p for p in listed if p and private_workstreams.is_private_path(p, private)]
    return sensitive + tracked_confidential_paths()


def unpushed_private_paths(rev_range: str) -> list[str]:
    """Private workstream files that a commit in `rev_range` adds or changes. The tip check misses
    a file added and removed again inside the range, and a push would still carry that commit. The
    upstream's list counts too, so a commit that deletes the list cannot carry files past it. -m and
    --full-history keep a merged side branch from hiding the commit by history simplification."""
    private = private_workstreams.slugs(REPO_ROOT)
    upstream_list = git("show", f"{rev_range.split('..')[0]}:{private_workstreams.CONFIG}", check=False)
    if upstream_list.returncode == 0:
        private |= private_workstreams.parse_slugs(upstream_list.stdout)
    if not private:
        return []
    named = git("log", "-z", "-m", "--full-history", "--format=", "--name-only", "--no-renames", "--diff-filter=AM", rev_range,
                "--", *private_workstreams.gate_pathspecs(private), check=False)
    return sorted({p.strip() for p in named.stdout.split("\0") if private_workstreams.is_private_path(p.strip(), private)})


def auto_push_network_git(*args: str) -> subprocess.CompletedProcess:
    """A network git call under the landing lock: a stalled remote fails instead of
    holding every landing. Timeout comes back as a failed result, rc 124."""
    try:
        return git(*args, check=False, timeout=AUTO_PUSH_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out after {AUTO_PUSH_GIT_TIMEOUT}s")


def auto_push_stamp() -> dict[str, Any]:
    try:
        data = json.loads(AUTO_PUSH_STAMP.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_auto_push_stamp(stamp: dict[str, Any]) -> None:
    AUTO_PUSH_STAMP.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTO_PUSH_STAMP.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _os_replace(tmp, AUTO_PUSH_STAMP)


def auto_push_log(line: str) -> None:
    AUTO_PUSH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUTO_PUSH_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{ledger.now_iso()} {line}\n")


def cos_alert(task_prefix: str, text: str, status: str) -> bool:
    """One urgent CoS line under a synthetic `<prefix>-<YYYYmmdd-HHMM>` row, the
    same path as check-state-inventory.py (direct, priority)."""
    stamp = now().strftime("%Y%m%d-%H%M")
    cmd = ["bash", str(SCRIPTS / "report-to-cos.sh"), "--task-id", f"{task_prefix}-{stamp}", "--origin", "cos",
           "--workstream", "switchboard-maintenance", "--status", status, "--urgent", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = run(cmd, env=env, timeout=180)
        return bool(json.loads((result.stdout.strip().splitlines() or [""])[-1]).get("delivered"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def cos_digest_line(task_prefix: str, text: str) -> bool:
    """Queue one non-urgent done line for the next-up digest's Updates section
    (--delivery digest: never pasted directly, even after hours)."""
    stamp = now().strftime("%Y%m%d-%H%M")
    cmd = ["bash", str(SCRIPTS / "report-to-cos.sh"), "--task-id", f"{task_prefix}-{stamp}", "--origin", "cos",
           "--workstream", "switchboard-maintenance", "--status", "done", "--delivery", "digest", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = run(cmd, env=env, timeout=180)
        return bool(json.loads((result.stdout.strip().splitlines() or [""])[-1]).get("batched"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def auto_push_alert(text: str, status: str) -> bool:
    return cos_alert("auto-push", text, status)


def auto_push_digest_line(text: str) -> bool:
    return cos_digest_line("auto-push", text)


def auto_push_tick() -> dict[str, Any]:
    """One gate evaluation. Returns {action, ...}; action is one of
    pushed | failed | diverged | behind | up-to-date | too-soon | landing | rebase | sensitive | private-identifier |
    fetch-failed | no-remote."""
    stamp = auto_push_stamp()
    last_attempt = ledger.parse_iso(stamp.get("last_attempt_at"))
    if last_attempt is not None and now() - last_attempt < AUTO_PUSH_INTERVAL:
        next_after = (last_attempt + AUTO_PUSH_INTERVAL).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"action": "too-soon", "last_attempt_at": stamp.get("last_attempt_at"), "next_after": next_after}
    rebasing = rebase_in_progress()
    if rebasing:
        return {"action": "rebase", "paths": rebasing}
    with landing_lock(blocking=False) as held:
        if not held:
            return {"action": "landing"}
        return _auto_push_under_lock(stamp)


def _auto_push_under_lock(stamp: dict[str, Any]) -> dict[str, Any]:
    remote, branch = AUTO_PUSH_REMOTE, AUTO_PUSH_BRANCH
    upstream = f"{remote}/{branch}"
    attempt_at = ledger.now_iso()

    def record(outcome: dict[str, Any], attempt: bool) -> dict[str, Any]:
        new_stamp = {**stamp, "last_checked_at": attempt_at, "last_outcome": outcome}
        if attempt:
            new_stamp["last_attempt_at"] = attempt_at
        write_auto_push_stamp(new_stamp)
        return outcome

    if git("remote", "get-url", remote, check=False).returncode:
        # No such remote (a local-only install): nothing to push to and
        # nothing to alarm about. Skip quietly: no log line, no fetch, checked once an hour.
        return record({"action": "no-remote", "remote": remote}, attempt=True)
    fetch = auto_push_network_git("fetch", "--quiet", remote, branch)
    if fetch.returncode:
        reason = (fetch.stderr or fetch.stdout).strip()[:200]
        auto_push_log(f"fetch-failed {reason}")
        return record({"action": "fetch-failed", "reason": reason}, attempt=False)
    counts = git("rev-list", "--left-right", "--count", f"{upstream}...{branch}", check=False)
    if counts.returncode:
        reason = (counts.stderr or counts.stdout).strip()[:200]
        auto_push_log(f"failed cannot compare {upstream}...{branch}: {reason}")
        return record({"action": "failed", "reason": reason}, attempt=False)
    behind, ahead = (int(n) for n in counts.stdout.split())
    local = git("rev-parse", "--short", branch).stdout.strip()
    remote_sha = git("rev-parse", "--short", upstream).stdout.strip()
    if ahead == 0 and behind == 0:
        return record({"action": "up-to-date", "ahead": 0, "behind": 0, "main": local}, attempt=False)
    if behind:
        action = "diverged" if ahead else "behind"
        text = (f"auto-push: main is {action} origin/main (ahead {ahead}, behind {behind}; main {local}, origin/main "
                f"{remote_sha}); not pushing. Reconcile main by hand (git pull --rebase or a merge), then the loop resumes.")
        auto_push_log(f"{action} ahead={ahead} behind={behind} main={local} origin/main={remote_sha}")
        delivered = auto_push_alert(text, status="blocked")
        return record({"action": action, "ahead": ahead, "behind": behind, "main": local, "origin_main": remote_sha,
                       "alert_delivered": delivered}, attempt=True)
    try:
        private_workstreams.slugs(REPO_ROOT)
    except OSError as error:
        text = (f"auto-push: refusing to push main; the private workstream list cannot be read ({error}), "
                f"so its files cannot be checked. Fix {private_workstreams.CONFIG}; the loop retries next hour.")
        auto_push_log(f"private-workstream-list-unreadable {error}")
        delivered = auto_push_alert(text, status="blocked")
        return record({"action": "sensitive", "paths": [], "reason": str(error), "alert_delivered": delivered}, attempt=True)
    sensitive = tracked_sensitive_paths()
    if sensitive:
        shown = ", ".join(sensitive[:5]) + ("…" if len(sensitive) > 5 else "")
        text = (f"auto-push: refusing to push main; {len(sensitive)} tracked path(s) under a gitignored sensitive directory "
                f"({shown}). Untrack them (git rm --cached) and commit; the loop retries next hour.")
        auto_push_log(f"sensitive-tracked count={len(sensitive)} {shown}")
        delivered = auto_push_alert(text, status="blocked")
        return record({"action": "sensitive", "paths": sensitive, "alert_delivered": delivered}, attempt=True)
    carried = unpushed_private_paths(f"{upstream}..{branch}") + unpushed_confidential_paths(f"{upstream}..{branch}")
    if carried:
        text = (f"auto-push: refusing to push main; an unpushed commit carries {len(carried)} private workstream or confidential session file(s) "
                f"({', '.join(carried[:5])}{'…' if len(carried) > 5 else ''}). Take them out of the unpushed commits on main, "
                f"never by a force push: python3 system/scripts/dispatch.py strip-unpushed (files on disk stay); "
                f"the loop retries next hour.")
        auto_push_log(f"private-workstream-unpushed count={len(carried)}")
        delivered = auto_push_alert(text, status="blocked")
        return record({"action": "sensitive", "paths": carried, "alert_delivered": delivered}, attempt=True)
    leak = private_identifier_refusal(f"{upstream}..{branch}")
    if leak:
        text = (f"auto-push: refusing to push main; an unpushed commit {leak}. Take it out of the unpushed commits on main, "
                f"never by a force push: python3 system/scripts/dispatch.py strip-unpushed withholds the line in each "
                f"unpushed commit once main's tip no longer names it; the loop retries next hour.")
        auto_push_log(f"private-identifier main={local} {leak}")
        delivered = auto_push_alert(text, status="blocked")
        return record({"action": "private-identifier", "ahead": ahead, "main": local, "reason": leak,
                       "alert_delivered": delivered}, attempt=True)
    push = auto_push_network_git("push", remote, f"{branch}:{branch}")
    if push.returncode:
        reason = (push.stderr or push.stdout).strip().replace("\n", " ")[:300]
        auto_push_log(f"failed ahead={ahead} main={local} rc={push.returncode} {reason}")
        delivered = auto_push_alert(f"auto-push: git push origin main failed (rc {push.returncode}): {reason}", status="failed")
        return record({"action": "failed", "ahead": ahead, "main": local, "rc": push.returncode, "reason": reason,
                       "alert_delivered": delivered}, attempt=True)
    auto_push_log(f"pushed {ahead} commit(s) {remote_sha}..{local} -> {upstream}")
    batched = auto_push_digest_line(f"pushed {ahead} commit{'s' if ahead != 1 else ''} to origin/main ({remote_sha}..{local})")
    outcome = {"action": "pushed", "ahead": ahead, "main": local, "origin_main_before": remote_sha, "digest_batched": batched}
    record(outcome, attempt=True)
    write_auto_push_stamp({**auto_push_stamp(), "last_push_at": attempt_at, "last_pushed_commit": local})
    return outcome


def auto_push_enabled() -> bool:
    """Off unless SWITCHBOARD_AUTO_PUSH=on. switchboard stays on this machine with
    no origin remote; a shareable copy is built and pushed from its own folder (system/kit/build_tier.py)."""
    return os.environ.get("SWITCHBOARD_AUTO_PUSH", "").strip().lower() == "on"


def command_auto_push(args: argparse.Namespace) -> int:
    if not auto_push_enabled():
        print(json.dumps({"action": "disabled", "reason": "SWITCHBOARD_AUTO_PUSH is not on; switchboard stays local"}))
        return 0
    # Unattended: a credential prompt (terminal or Git Credential Manager's window) has nobody to answer it.
    os.environ.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    outcome = auto_push_tick()
    print(json.dumps(outcome))
    return 0 if outcome["action"] in {"pushed", "up-to-date", "too-soon", "landing", "rebase"} else 1


# --- strip-unpushed: take private files out of main's unpushed commits ---------------------
#
# auto-push refuses while any commit in origin/main..main adds a private workstream or
# confidential session file, even one a later commit untracked again, and its alert says
# "Take them out of the unpushed commits on main, never by a force push". This command acts on
# that alert: a note committed before its workstream went private otherwise stays in an unpushed
# commit after a later commit untracks it, and the alert repeats.
#
# This rewrites only origin/main..main, so origin never needs a force push. Each commit keeps
# its author, committer, dates, and message; only the named paths leave its tree. It refuses a
# path main's tip still tracks (untrack and commit first), so the tip tree, the index, and every
# file on disk stay exactly as they are. It runs under the landing lock, moves main with a
# compare-and-swap, and records old -> new ids in system/state/main-rewrites.json, which finish
# (fork_point) and the landed checks (on_main) read so branches made from the old main still land.

def _raw_git(args: list[str], env: dict[str, str] | None = None, data: bytes | None = None) -> subprocess.CompletedProcess:
    """git with bytes in and out: commit messages and names carry emoji, and Windows' text mode would mangle them."""
    return subprocess.run(_runnable(["git", "-C", str(REPO_ROOT), *args]), input=data, capture_output=True,
                          env={**os.environ, **(env or {})}, **({} if data is not None else {"stdin": subprocess.DEVNULL}))


def _redacted_blob(sha: str, listed: Any, blobs: dict[str, str]) -> str:
    """The id of `sha`'s content with every line naming a listed private identifier withheld
    (private_identifiers.redact_text); `sha` itself when no line does. Cached per blob."""
    if sha not in blobs:
        raw = _raw_git(["cat-file", "blob", sha]).stdout
        found: list[str] = []
        if b"\0" not in raw[:8192]:
            new, found = private_identifiers.redact_bytes(raw, listed)
        if not found:
            blobs[sha] = sha
        else:
            made = _raw_git(["hash-object", "-w", "--stdin"], data=new)
            if made.returncode:
                raise RuntimeError(f"git hash-object failed: {made.stderr.decode('utf-8', 'replace').strip()}")
            blobs[sha] = made.stdout.decode().strip()
    return blobs[sha]


def _rewrite_commit(commit: str, paths: list[str], index: Path, redact: list[str] | None = None,
                    listed: Any = None, blobs: dict[str, str] | None = None) -> tuple[str, bool]:
    """(new tree, changed) for `commit` without `paths`, and with lines naming a private
    identifier withheld in each file of `redact` (a close-out commit can put one in an
    unpushed CONTEXT.md, and auto-push then refuses)."""
    tree = git("rev-parse", f"{commit}^{{tree}}").stdout.strip()
    literal = {"GIT_LITERAL_PATHSPECS": "1"}
    touched = _raw_git(["ls-tree", "-r", "-z", "--name-only", commit, "--", *paths], literal).stdout.strip(b"\0") if paths else b""
    swaps: list[str] = []
    if redact and listed is not None:
        rows = _raw_git(["ls-tree", "-r", "-z", commit, "--", *redact], literal).stdout.decode("utf-8", "replace")
        for row in filter(None, rows.split("\0")):
            meta, path = row.split("\t", 1)
            mode, kind, sha = meta.split()
            if kind == "blob" and path not in paths:
                new_sha = _redacted_blob(sha, listed, blobs if blobs is not None else {})
                if new_sha != sha:
                    swaps.append(f"{mode},{new_sha},{path}")
    if not touched and not swaps:
        return tree, False
    env = {**literal, "GIT_INDEX_FILE": str(index)}
    steps = [["read-tree", commit]]
    if touched:
        steps.append(["rm", "--cached", "-q", "-f", "--ignore-unmatch", "--", *paths])
    steps += [["update-index", "--cacheinfo", item] for item in swaps]
    for step in steps:
        result = _raw_git(step, env)
        if result.returncode:
            raise RuntimeError(f"git {step[0]} on {commit[:10]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    written = _raw_git(["write-tree"], env)
    if written.returncode:
        raise RuntimeError(f"git write-tree on {commit[:10]} failed: {written.stderr.decode('utf-8', 'replace').strip()}")
    return written.stdout.decode().strip(), True


def _copy_commit(commit: str, tree: str, parents: list[str]) -> str:
    raw = _raw_git(["cat-file", "commit", commit]).stdout
    message = raw.split(b"\n\n", 1)[1] if b"\n\n" in raw else b""
    meta = _raw_git(["log", "-1", "--date=raw", "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd", commit]).stdout
    an, ae, ad, cn, ce, cd = meta.decode("utf-8", "replace").rstrip("\n").split("\0")
    env = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
           "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce, "GIT_COMMITTER_DATE": cd}
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    made = _raw_git(args, env, data=message)
    if made.returncode:
        raise RuntimeError(f"git commit-tree for {commit[:10]} failed: {made.stderr.decode('utf-8', 'replace').strip()}")
    return made.stdout.decode().strip()


def strip_unpushed(dry_run: bool = False, fetch: bool = True) -> dict[str, Any]:
    remote, branch = AUTO_PUSH_REMOTE, AUTO_PUSH_BRANCH
    upstream = f"{remote}/{branch}"
    rebasing = rebase_in_progress()
    if rebasing:
        return {"action": "rebase", "paths": rebasing}
    with landing_lock() as held:
        if not held:
            return {"action": "landing"}
        if fetch:
            fetched = auto_push_network_git("fetch", "--quiet", remote, branch)
            if fetched.returncode:
                return {"action": "fetch-failed", "reason": (fetched.stderr or fetched.stdout).strip()[:200]}
        if git("rev-parse", "--verify", "-q", upstream, check=False).returncode:
            return {"action": "no-upstream", "upstream": upstream}
        if git("merge-base", "--is-ancestor", upstream, branch, check=False).returncode:
            return {"action": "diverged", "reason": f"{upstream} is not an ancestor of {branch}; reconcile first"}
        rev_range = f"{upstream}..{branch}"
        paths = sorted(set(unpushed_private_paths(rev_range)) | set(unpushed_confidential_paths(rev_range)))
        redact, listed, problem = identifier_paths(rev_range, paths)
        if problem:
            return {"action": "refused", "paths": paths, "reason": problem}
        if not paths and not redact:
            return {"action": "nothing", "range": rev_range}
        main_rewrites.prune_unapplied(_git_nocheck, STATE_DIR)
        # record_on_main commits close-out lines on main outside the landing lock, so main can move
        # during the rewrite and make the compare-and-swap refuse. Retry; commits already rewritten come from the cache, so a retry costs only the
        # new ones.
        cache: dict[tuple[str, tuple[str, ...]], str] = {}
        outcome: dict[str, Any] = {}
        for attempt in range(1, STRIP_ATTEMPTS + 1):
            outcome = _strip_once(branch, upstream, rev_range, paths, cache, dry_run, redact, listed)
            if outcome.get("action") != "moved":
                return outcome
            main_rewrites.prune_unapplied(_git_nocheck, STATE_DIR)
            log(f"strip-unpushed: main moved during attempt {attempt}; retrying")
        return {**outcome, "action": "failed", "reason": f"main kept moving for {STRIP_ATTEMPTS} attempts; nothing was changed"}


STRIP_ATTEMPTS = 4


def identifier_paths(rev_range: str, removed: list[str]) -> tuple[list[str], Any, str]:
    """(files whose unpushed commits add a listed private identifier, the loaded list, a reason
    strip-unpushed cannot fix it, or ""). Only file content is rewritten: a value in a commit
    message or a path is refused."""
    verdict = private_identifiers.guard_range(REPO_ROOT, rev_range)
    if verdict["status"] == "error":
        return [], None, f"the private identifier list cannot be checked: {verdict['reason']}"
    if verdict["status"] != "hit":
        return [], None, ""
    where = {f.path for f in verdict["findings"]}
    if where & {"(commit message)", "(path withheld)"}:
        return [], None, ("a commit message or file path in the unpushed range names a private identifier; "
                          "strip-unpushed rewrites file content only")
    return sorted(where - set(removed)), private_identifiers.load_list(REPO_ROOT), ""


def _strip_once(branch: str, upstream: str, rev_range: str, paths: list[str],
                cache: dict[tuple[str, tuple[str, ...]], str], dry_run: bool,
                redact: list[str] | None = None, listed: Any = None) -> dict[str, Any]:
    redact = redact or []
    old_tip = git("rev-parse", branch).stdout.strip()
    still = [p for p in _raw_git(["ls-tree", "-r", "-z", "--name-only", old_tip, "--", *paths],
                                 {"GIT_LITERAL_PATHSPECS": "1"}).stdout.decode("utf-8", "replace").split("\0") if p] if paths else []
    if still:
        return {"action": "refused", "paths": still,
                "reason": "main's tip still tracks these; untrack them (git rm --cached) and commit, then rerun"}
    blobs: dict[str, str] = {}
    if redact:
        LOCKS.mkdir(parents=True, exist_ok=True)
        probe = LOCKS / f"strip-probe-{os.getpid()}.index"
        try:
            tip_tree, _ = _rewrite_commit(old_tip, [], probe, redact, listed, blobs)
        finally:
            probe.unlink(missing_ok=True)
        if tip_tree != git("rev-parse", f"{old_tip}^{{tree}}").stdout.strip():
            return {"action": "refused", "paths": redact,
                    "reason": "main's tip still names a private identifier in these; edit it out and commit, then rerun"}
    commits = git("rev-list", "--reverse", "--topo-order", rev_range).stdout.split()
    mapping: dict[str, str] = {}
    LOCKS.mkdir(parents=True, exist_ok=True)
    index = LOCKS / f"strip-unpushed-{os.getpid()}.index"
    try:
        for commit in commits:
            parents = git("rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
            new_parents = [mapping.get(p, p) for p in parents]
            key = (commit, tuple(new_parents))
            if key in cache:
                if cache[key] != commit:
                    mapping[commit] = cache[key]
                continue
            tree, changed = _rewrite_commit(commit, paths, index, redact, listed, blobs)
            cache[key] = commit if (not changed and new_parents == parents) else _copy_commit(commit, tree, new_parents)
            if cache[key] != commit:
                mapping[commit] = cache[key]
    finally:
        index.unlink(missing_ok=True)
    new_tip = mapping.get(old_tip, old_tip)
    if git("rev-parse", f"{new_tip}^{{tree}}").stdout != git("rev-parse", f"{old_tip}^{{tree}}").stdout:
        return {"action": "failed", "reason": "the rewritten tip's tree differs from main's; nothing was moved"}
    left = sorted(set(unpushed_private_paths(f"{upstream}..{new_tip}")) | set(unpushed_confidential_paths(f"{upstream}..{new_tip}")))
    if left:
        return {"action": "failed", "paths": left, "reason": "the rewritten range still carries these; nothing was moved"}
    leak = private_identifier_refusal(f"{upstream}..{new_tip}") if redact else ""
    if leak:
        return {"action": "failed", "paths": redact, "reason": f"the rewritten range still {leak}; nothing was moved"}
    outcome = {"action": "dry-run" if dry_run else "stripped", "paths": paths, "redacted": redact, "commits": len(commits),
               "rewritten": len(mapping), "old_tip": old_tip, "new_tip": new_tip, "upstream": upstream}
    if dry_run:
        return outcome
    # The record goes first: main moved with no record would leave branches made from the old main
    # to replay the old commits (private file included) and read landed tasks as unlanded. A record
    # whose compare-and-swap then fails is pruned (prune_unapplied) before the retry.
    main_rewrites.append(STATE_DIR, {"at": ledger.now_iso(), "upstream": git("rev-parse", upstream).stdout.strip(),
                                     "old_tip": old_tip, "new_tip": new_tip, "paths": paths, "redacted": redact, "map": mapping})
    moved = git("update-ref", "-m", f"switchboard strip-unpushed: {', '.join(paths + redact)[:200]}", f"refs/heads/{branch}",
                new_tip, old_tip, check=False)
    if moved.returncode:
        return {**outcome, "action": "moved", "reason": f"main moved meanwhile ({moved.stderr.strip()[:160]})"}
    auto_push_log(f"strip-unpushed paths={len(paths)} redacted={len(redact)} rewrote {len(mapping)}/{len(commits)} {old_tip[:10]}->{new_tip[:10]}")
    return outcome


def command_strip_unpushed(args: argparse.Namespace) -> int:
    os.environ.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    outcome = strip_unpushed(dry_run=args.dry_run, fetch=not args.no_fetch)
    print(json.dumps(outcome))
    return 0 if outcome["action"] in {"stripped", "dry-run", "nothing"} else 1


# --- capacity: reported, never enforced ---------------
#
# RAM, timeouts and quota are the only real limits, so there is no
# concurrent-worker cap: dispatch.max_workers 0 means unlimited and nothing below holds a launch.
# What replaces it is one line of evidence per sweep tick about the three things
# the cap was standing in for: how many workers are live, how much memory sits
# in the compressor and in swap (vm_stat / /usr/sbin/sysctl vm.swapusage), and
# whether a cmux RPC timed out during a launch since the last tick. The snapshot
# is always written to system/state/worker-dispatch/capacity.json; a digest Updates line plus one
# urgent CoS line go out only while a trigger holds (swap used above
# dispatch.swap_alert_gb, or a launch-path cmux timeout this tick), at most
# once per CAPACITY_ALERT_INTERVAL.

CAPACITY_STAMP = STATE_DIR / "worker-dispatch" / "capacity.json"
CMUX_TIMEOUT_LOG = STATE_DIR / "worker-dispatch" / "cmux-timeouts.jsonl"
CAPACITY_ALERT_INTERVAL = timedelta(minutes=int(os.environ.get("SWITCHBOARD_CAPACITY_ALERT_MINUTES") or 60))
_SIZE_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _now_iso_us() -> str:
    """Microsecond stamp: a timeout noted in the same second as the tick that
    just reported must still sort after it."""
    return now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def note_cmux_timeout(task_id: str, call: str, seconds: float) -> None:
    """Record one cmux RPC that gave no answer inside its budget on the launch
    path; capacity_tick reports it on the next sweep. Never raises."""
    try:
        CMUX_TIMEOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CMUX_TIMEOUT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now_iso_us(), "task_id": task_id, "call": call, "seconds": seconds}) + "\n")
    except OSError:
        pass


def cmux_timeouts_since(since: datetime | None) -> list[dict[str, Any]]:
    if not CMUX_TIMEOUT_LOG.exists():
        return []
    out = []
    for line in CMUX_TIMEOUT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        at = ledger.parse_iso(entry.get("at"))
        if at is None or (since is not None and at <= since):
            continue
        out.append(entry)
    return out


def _parse_size(text: str) -> float:
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGTB]?)", text or "")
    return float(match.group(1)) * _SIZE_UNITS[match.group(2)] if match else 0.0


SYSCTL = "/usr/sbin/sysctl"


def memory_snapshot(vm_stat_text: str | None = None, swapusage_text: str | None = None) -> dict[str, float | None]:
    """{free_ram_gb, compressor_gb, swap_used_gb, swap_total_gb} from `vm_stat`
    and `sysctl vm.swapusage` (passed in for tests). Free RAM is free plus
    speculative pages only; compressor_gb is "Pages occupied by compressor",
    the memory macOS has already squeezed before it swaps. Inactive pages are
    not counted anywhere: they made the line report plenty of free RAM while swap
    was high. sysctl is called by absolute path because the command-center launchd
    PATH has no /usr/sbin, which left every swap field null from the loop. A
    field that cannot be read is None rather than 0 so a broken probe never
    reads as "no memory left".

    On Windows (a live probe, no texts passed) the snapshot is
    windows_memory.snapshot(): commit used and limit, free RAM, nonpaged pool
    and zombie processes, read in-process."""
    if _on_windows() and vm_stat_text is None and swapusage_text is None:
        try:
            import windows_memory
            win = windows_memory.snapshot()
        except Exception:  # noqa: BLE001 - a broken probe must never break the sweep
            win = {}
        return {"free_ram_gb": win.get("free_ram_gb"), "compressor_gb": None,
                "swap_used_gb": None, "swap_total_gb": None,
                **{k: win.get(k) for k in ("commit_used_gb", "commit_limit_gb", "commit_pct",
                                           "commit_ceiling_gb", "commit_ceiling_pct",
                                           "nonpaged_pool_gb", "zombie_processes")}}
    if vm_stat_text is None:
        try:
            vm_stat_text = run(["vm_stat"], timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            vm_stat_text = ""
    if swapusage_text is None:
        try:
            swapusage_text = run([SYSCTL, "vm.swapusage"], timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            swapusage_text = ""
    free_ram_gb: float | None = None
    compressor_gb: float | None = None
    page = re.search(r"page size of (\d+) bytes", vm_stat_text or "")
    pages = {m.group(1).lower(): int(m.group(2))
             for m in re.finditer(r"^Pages (free|speculative|occupied by compressor):\s+(\d+)\.?$", vm_stat_text or "", re.M)}
    if page and "free" in pages:
        free_ram_gb = (pages["free"] + pages.get("speculative", 0)) * int(page.group(1)) / 1024 ** 3
    if page and "occupied by compressor" in pages:
        compressor_gb = pages["occupied by compressor"] * int(page.group(1)) / 1024 ** 3
    swap = re.search(r"total\s*=\s*([\d.]+\s*[KMGTB]?)\s+used\s*=\s*([\d.]+\s*[KMGTB]?)", swapusage_text or "")
    swap_total_gb = _parse_size(swap.group(1)) / 1024 ** 3 if swap else None
    swap_used_gb = _parse_size(swap.group(2)) / 1024 ** 3 if swap else None
    return {"free_ram_gb": free_ram_gb, "compressor_gb": compressor_gb,
            "swap_used_gb": swap_used_gb, "swap_total_gb": swap_total_gb}


def capacity_stamp() -> dict[str, Any]:
    try:
        data = json.loads(CAPACITY_STAMP.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_capacity_stamp(data: dict[str, Any]) -> None:
    CAPACITY_STAMP.parent.mkdir(parents=True, exist_ok=True)
    tmp = CAPACITY_STAMP.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _os_replace(tmp, CAPACITY_STAMP)


def _gb(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f} GB"


def capacity_snapshot(cfg: dict[str, Any], since: datetime | None = None,
                      memory: dict[str, float | None] | None = None) -> dict[str, Any]:
    """Live worker count, memory, launch-path cmux timeouts since `since`, and
    the reasons (if any) this tick should be reported. Pure given `memory`.
    A live Windows probe also times process starts when commit is past the hold
    of the current limit but the limit can still grow (commit_hold_reason)."""
    live_probe = memory is None
    memory = memory_snapshot() if memory is None else memory
    if live_probe and _on_windows():
        win = live_memory(cfg)   # adds spawn_ms only when the hold would read it
        if win and win.get("spawn_ms") is not None:
            memory = {**memory, "spawn_ms": win["spawn_ms"]}
    timeouts = cmux_timeouts_since(since)
    try:
        threshold = float(cfg.get("swap_alert_gb") or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    reasons = []
    if threshold > 0 and memory.get("swap_used_gb") is not None and memory["swap_used_gb"] > threshold:
        reasons.append(f"swap used {_gb(memory['swap_used_gb'])} > {threshold:g} GB")
    if timeouts:
        calls = sorted({str(t.get("call") or "cmux") for t in timeouts})
        reasons.append(f"{len(timeouts)} cmux RPC timeout(s) in launch ({', '.join(calls)})")
    # Windows: commit near the limit, or a zombie-process leak.
    try:
        commit_alert = float(cfg.get("commit_alert_pct") or 0)
        zombie_alert = int(cfg.get("zombie_alert_count") or 0)
    except (TypeError, ValueError):
        commit_alert, zombie_alert = 0.0, 0
    # Against the ceiling when it is known: the system-managed pagefile grows, so
    # 90% of today's limit is routine on a laptop.
    ceiling_pct = memory.get("commit_ceiling_pct")
    if commit_alert > 0 and ceiling_pct is not None:
        if ceiling_pct >= commit_alert:
            reasons.append(f"memory commit {ceiling_pct:.0f}% of the most it can grow to "
                           f"({_gb(memory.get('commit_used_gb'))} of {_gb(memory.get('commit_ceiling_gb'))})")
    elif commit_alert > 0 and memory.get("commit_pct") is not None and memory["commit_pct"] >= commit_alert:
        reasons.append(f"memory commit {memory['commit_pct']:.0f}% of the limit "
                       f"({_gb(memory.get('commit_used_gb'))} of {_gb(memory.get('commit_limit_gb'))})")
    try:
        spawn_limit = float(cfg.get("spawn_hold_ms") or 0)
    except (TypeError, ValueError):
        spawn_limit = 0.0
    if spawn_limit > 0 and memory.get("spawn_ms") is not None and memory["spawn_ms"] >= spawn_limit:
        reasons.append("new processes fail to start" if memory["spawn_ms"] == float("inf")
                       else f"new processes take {memory['spawn_ms']:.0f} ms to start")
    if zombie_alert > 0 and memory.get("zombie_processes") is not None and memory["zombie_processes"] >= zombie_alert:
        reasons.append(f"{int(memory['zombie_processes']):,} zombie processes from a kernel driver leak; "
                       f"a reboot frees them")
    return {"at": _now_iso_us(), "workers": len(live_workers(ledger.load_payload())),
            "worker_cap": worker_cap(cfg), **memory, "cmux_timeouts": len(timeouts), "reasons": reasons}


def capacity_launches_held(snap: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    """True only when new launches actually wait: commit at or above
    dispatch.commit_hold_pct (commit_hold_reason). The alert thresholds
    (commit_alert_pct, zombie_alert_count, swap_alert_gb) only report; they hold
    nothing."""
    if snap.get("commit_pct") is None:
        return False
    live = snap.get("workers")
    return bool(commit_hold_reason(config() if cfg is None else cfg, snap,
                                   live=live if isinstance(live, int) else None))


def capacity_hold_sentence(snap: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    cfg = config() if cfg is None else cfg
    try:
        hold = float(cfg.get("commit_hold_pct") or 0)
    except (TypeError, ValueError):
        hold = 0.0
    if capacity_launches_held(snap, cfg):
        floor = int(cfg.get("memory_hold_floor_workers") or 0)
        keep = (" One worker still starts whenever none is live" if floor == 1
                else f" One worker still starts whenever fewer than {floor} are live")
        keep = f"{keep} (dispatch.memory_hold_floor_workers)." if floor > 0 else ""
        return (f"New launches are held: {commit_hold_reason(cfg, snap)}. They start once it clears, "
                f"for example after closing apps.{keep}")
    if hold > 0 and (held_alone := commit_hold_reason(cfg, snap)):
        floor = int(cfg.get("memory_hold_floor_workers") or 0)
        who = "no worker is live" if floor == 1 else f"fewer than {floor} workers are live"
        return (f"Launches are not held although {held_alone}: {who} "
                f"(dispatch.memory_hold_floor_workers).")
    if hold > 0 and snap.get("commit_ceiling_pct") is not None:
        return (f"Launches are not held: commit is below the {hold:g}% hold of the most it can grow to "
                f"(dispatch.commit_hold_pct)"
                + (f" and processes start in {snap['spawn_ms']:.0f} ms." if snap.get("spawn_ms") is not None else "."))
    if hold > 0:
        return f"Launches are not held: commit is below the {hold:g}% hold (dispatch.commit_hold_pct)."
    return "Launches are not held (dispatch.commit_hold_pct is off)."


def capacity_status(snap: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """The direct capacity line's status: "blocked" only while launches are
    actually held, otherwise "done" (informational). A blocked line asks for
    nothing either way; review_surface.system_notice keeps the Options check off."""
    return "blocked" if capacity_launches_held(snap, cfg) else "done"


def capacity_text(snap: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    cap = snap.get("worker_cap")
    if snap.get("commit_pct") is not None:   # Windows: commit and zombies stand in for compressor and swap
        z = snap.get("zombie_processes")
        return (f"capacity: {snap['workers']} worker(s) live{'' if cap is None else f' (cap {cap})'}, "
                f"commit {_gb(snap.get('commit_used_gb'))} of {_gb(snap.get('commit_limit_gb'))} "
                f"({snap['commit_pct']:.0f}%"
                + ("" if snap.get("commit_ceiling_pct") is None else
                   f"; can grow to {_gb(snap.get('commit_ceiling_gb'))}, {snap['commit_ceiling_pct']:.0f}% of that")
                + f"), {_gb(snap.get('free_ram_gb'))} RAM free, "
                f"{'?' if z is None else f'{int(z):,}'} zombie processes; {'; '.join(snap['reasons'])}. "
                f"{capacity_hold_sentence(snap, cfg)}")
    # Compressor beside swap reads as pressure; "RAM free" (free + inactive)
    # can show plenty of headroom while swap is high.
    return (f"capacity: {snap['workers']} worker(s) live{'' if cap is None else f' (cap {cap})'}, "
            f"{_gb(snap.get('compressor_gb'))} compressed, {_gb(snap.get('swap_used_gb'))} swap used; "
            f"{'; '.join(snap['reasons'])}. Launches are not blocked (no worker cap).")


def capacity_trigger_kinds(reasons: list[str]) -> list[str]:
    """The kinds of trigger behind a reasons list: "swap" for the threshold,
    "cmux" for a launch-path RPC timeout. Used to tell a trigger that persists
    from one that newly holds."""
    kinds = set()
    for reason in reasons:
        if reason.startswith("swap"):
            kinds.add("swap")
        elif "cmux RPC timeout" in reason:
            kinds.add("cmux")
        elif reason.startswith(("memory commit", "new processes take")) or "zombie processes" in reason:
            kinds.add("memory")   # Windows commit or zombie leak
    return sorted(kinds)


def capacity_tick(cfg: dict[str, Any]) -> dict[str, Any]:
    """One sweep evaluation: write the snapshot; when a trigger holds and the
    last report is older than CAPACITY_ALERT_INTERVAL (or there was none), queue
    one digest Updates line. The direct urgent CoS line goes out only when the
    trigger is new: swap crossed the threshold since the last alert, or a cmux
    timeout happened this tick. A swap trigger that just persists stays in the
    digest and the badge (capacity.json). Never blocks anything.

    A swap level that stays high all day would otherwise cost CoS a model turn
    every hour to say the same thing; the digest already carries it."""
    stamp = capacity_stamp()
    # Timeouts count from the last tick that reported (or had none to report),
    # so one that lands inside the hourly quiet window is carried into the next
    # line instead of dropped.
    snap = capacity_snapshot(cfg, since=ledger.parse_iso(stamp.get("timeouts_through")))
    last_alert = ledger.parse_iso(stamp.get("last_alert_at"))
    due = bool(snap["reasons"]) and (last_alert is None or now() - last_alert >= CAPACITY_ALERT_INTERVAL)
    kinds = capacity_trigger_kinds(snap["reasons"])
    # A swap trigger persists when the last alert already carried it and no
    # tick since then saw swap back under the threshold.
    swap_cleared = bool(stamp.get("swap_cleared_since_alert")) or "swap" not in kinds
    swap_persists = "swap" in kinds and "swap" in (stamp.get("last_alert_kinds") or []) and not swap_cleared
    # A Windows memory trigger (commit or zombies) works like swap: one direct
    # line when it newly holds, the digest while it persists, re-armed once a
    # tick sees it clear (after a reboot).
    memory_cleared = bool(stamp.get("memory_cleared_since_alert")) or "memory" not in kinds
    memory_persists = ("memory" in kinds and "memory" in (stamp.get("last_alert_kinds") or [])
                       and not memory_cleared)
    direct = due and ("cmux" in kinds or ("swap" in kinds and not swap_persists)
                      or ("memory" in kinds and not memory_persists))
    reported = False
    if due:
        text = capacity_text(snap, cfg)
        reported = cos_digest_line("capacity", text)
    if direct:
        # Always a terminal status: "active" would leave a live synthetic row with
        # no worker, so the command center would repeat its stale line all day.
        # Blocked only while launches are actually held (commit at or above
        # commit_hold_pct); an alert threshold alone is informational.
        cos_alert("capacity", text, capacity_status(snap, cfg))
    write_capacity_stamp({**snap, "last_alert_at": snap["at"] if due else stamp.get("last_alert_at"),
                          "last_alert_kinds": kinds if due else stamp.get("last_alert_kinds") or [],
                          "swap_cleared_since_alert": False if due else swap_cleared,
                          "memory_cleared_since_alert": False if due else memory_cleared,
                          "timeouts_through": snap["at"] if due or not snap["cmux_timeouts"] else stamp.get("timeouts_through")})
    return {**snap, "reported": due, "direct": direct, "digest_batched": reported}


def command_capacity(args: argparse.Namespace) -> int:
    print(json.dumps(capacity_snapshot(config(), since=ledger.parse_iso(capacity_stamp().get("timeouts_through")))))
    return 0


# --- locate / scorecard / config -----------------------------------------------------

def command_locate(args: argparse.Namespace) -> int:
    agent = ledger.live_agents().get(args.thread)
    if not agent:
        print(json.dumps({"local": False, "thread_id": args.thread, "workspace_ref": ""}))
        return 1
    ref = next((w.get("ref") for w in cmux_workspaces() if w.get("id") == agent.get("workspace_id")), "")
    print(json.dumps({"local": True, "thread_id": args.thread, "workspace_ref": ref or "", "state": agent.get("state"),
                      "title": agent.get("title")}))
    return 0


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 90:
        return f"{value:.0f}s"
    if value < 5400:
        return f"{value / 60:.0f}m"
    return f"{value / 3600:.1f}h"


def command_scorecard(args: argparse.Namespace) -> int:
    since = now() - timedelta(days=7 if args.week else args.days)
    rows = []
    for task in ledger.load_payload()["tasks"]:
        created = ledger.parse_iso(task.get("created_at"))
        if not created or created < since:
            continue
        started, finished = ledger.parse_iso(task.get("started_at")), ledger.parse_iso(task.get("finished_at"))
        rows.append({"id": task["id"], "workstream": task.get("workstream") or "-", "status": task.get("status"),
                     "attempts": int(task.get("attempt") or 1),
                     "pickup": (started - created).total_seconds() if started else None,
                     "completion": (finished - created).total_seconds() if finished else None})
    rows.sort(key=lambda r: r["id"])
    if args.json:
        print(json.dumps(rows))
        return 0
    print(f"{'task':16} {'workstream':30} {'status':9} {'pickup':>7} {'done':>7} att")
    for r in rows:
        print(f"{r['id']:16} {r['workstream'][:30]:30} {r['status']:9} {fmt_seconds(r['pickup']):>7} {fmt_seconds(r['completion']):>7} {r['attempts']:>3}")
    pick = sorted(r["pickup"] for r in rows if r["pickup"] is not None)
    comp = sorted(r["completion"] for r in rows if r["completion"] is not None)

    def pct(values, q):
        return fmt_seconds(values[min(len(values) - 1, int(q * len(values)))]) if values else "-"

    print(f"\n{len(rows)} tasks since {since.date()}: pickup median {pct(pick, .5)} p75 {pct(pick, .75)} p90 {pct(pick, .9)} "
          f"max {fmt_seconds(pick[-1] if pick else None)}; completion median {pct(comp, .5)} p90 {pct(comp, .9)}; "
          f"{sum(r['attempts'] > 1 for r in rows)} needed >1 attempt"
          f"{'' if not rows else f' (max {max(r['attempts'] for r in rows)})'}; "
          f"{sum(r['status'] in {'blocked', 'failed'} for r in rows)} blocked/failed.")
    return 0


def command_config(args: argparse.Namespace) -> int:
    print(json.dumps({**config(), "repo": str(REPO_ROOT), "worktrees": str(WORKTREES), "locks": str(LOCKS)}, indent=2))
    return 0


def command_verify_amp(args: argparse.Namespace) -> int:
    """verify_worker_amp for a tab cmux-helpers.sh just typed a launch command
    into. Exit 0 when the Amp TUI is up, 2 when the screen stayed unreadable or
    unrecognised (nothing was typed; the caller keeps its own wait), 1 when the
    tab is known not to run Amp after the launcher's retypes. --seconds/--poll override
    the module budget; the harness uses them, production keeps the defaults."""
    global AMP_VERIFY_SECONDS, AMP_VERIFY_POLL_SECONDS
    if args.seconds is not None:
        AMP_VERIFY_SECONDS = args.seconds
    if args.poll is not None:
        AMP_VERIFY_POLL_SECONDS = args.poll
    verdict = verify_worker_amp(args.workspace_ref, args.label, args.command)
    print(json.dumps({"workspace_ref": args.workspace_ref, **verdict}))
    if verdict["amp_verified"] is True:
        return 0
    return 2 if verdict["amp_verified"] == "unknown" else 1


def command_check_symlinks(args: argparse.Namespace) -> int:
    """Pre-commit guard (.lefthook.yaml): fail when the staged index contains a
    symlink pointing inside the repo. Runs in whichever checkout git invoked it from."""
    cwd = Path(os.environ.get("SWITCHBOARD_CHECKOUT") or os.getcwd())
    found = loop_symlinks(cwd, staged_paths(cwd))
    for entry in found:
        print(f"symlink-guard: {entry['path']} -> {entry['target']} points inside the repo; "
              f"commit it and a fast-forward of main replaces the real directory with a link to itself. "
              f"Unstage it: git reset -- {shlex.quote(entry['path'])}", file=sys.stderr)
    return 1 if found else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dispatch")
    d.add_argument("--origin", default="cos")
    d.add_argument("--workstream")
    d.add_argument("--task-id")
    d.add_argument("--request-id")
    d.add_argument("--delivery", choices=["auto", "direct", "digest"])
    d.add_argument("--depends-on", action="append", default=[])
    d.add_argument("--continuation-of")
    d.add_argument("--force-redispatch", action="store_true")
    d.add_argument("--parallel-worker", action="store_true",
                   help="open a separate worker even when the owner has an open tab (only when the owner asks for a parallel worker)")
    d.add_argument("--sync", action="store_true",
                   help="launch the worker inline and return after the tab is open (default: write the "
                        "row, spawn `dispatch.py launch` detached, return at once; router.yaml dispatch.async_launch)")
    d.add_argument("prompt", nargs="+")
    d.set_defaults(func=command_dispatch)

    la = sub.add_parser("launch", help="detached half of an async dispatch: worktree, cmux tab, bind, verify for one "
                        "routing row; failures reach the origin through report-to-cos.sh")
    la.add_argument("--task-id", required=True)
    la.set_defaults(func=command_launch)

    st = sub.add_parser("steer", help="same-task steer: message the live thread, dispatch --continuation-of when the "
                        "row is terminal and its tab reaped (a message to a reaped tab is lost), or "
                        "queue the steer on the row when its worker has not started (routing, queued, launching, or cmux "
                        "says the recipient reads no messages yet); queued steers deliver in order at the worker's ack "
                        "or first heartbeat, and one still queued after dispatch.steer_queue_timeout_minutes sends the "
                        "origin one callback; nothing is spawned")
    st.add_argument("--origin", default="cos")
    st.add_argument("--task-id", required=True)
    st.add_argument("--no-fallback-dispatch", action="store_true",
                    help="when the target has not started, print the old 'still starting; retry in about 30 s' message "
                         "instead of queuing the steer (default: queue)")
    st.add_argument("--no-deliver", action="store_true",
                    help="never deliver the steer by cmux agent message; print the old status message reply")
    st.add_argument("--record-only", action="store_true",
                    help="a hold, ack, ruling or note with no work: on a finished row, record it "
                         "on the ledger row and the workstream CONTEXT, answer the row's open question and ruling to-do, "
                         "and start no worker; a live or not-started row still gets the steer as usual")
    st.add_argument("prompt", nargs="+")
    st.set_defaults(func=command_steer)
    ds = sub.add_parser("deliver-steers", help="deliver queued steers to workers that have started (ack/heartbeat spawn "
                        "and the sweep); report overdue ones to the origin once; drop and report on a terminal row")
    ds.add_argument("--task-id", default=None)
    ds.set_defaults(func=command_deliver_steers)

    f = sub.add_parser("finish")
    f.add_argument("--task-id", required=True)
    f.add_argument("--allow-mass-delete", action="store_true",
                   help=f"land a branch that deletes more than {MASS_DELETE_LIMIT} tracked files or tracks a path under a directory main ignores")
    f.add_argument("--accept-main-edits", action="store_true",
                   help="commit (with attribution) uncommitted edits on main to paths the branch also changes even when they were "
                        "written after the task started; without it such edits are refused as probable stray writes")
    f.set_defaults(func=command_finish)

    lk = sub.add_parser("lock")
    lk.add_argument("--task-id", required=True)
    lk.add_argument("--path", required=True)
    lk.add_argument("--wait", type=float, default=4 * 3600)
    lk.add_argument("--poll", type=float, default=5)
    lk.set_defaults(func=command_lock)

    ul = sub.add_parser("unlock")
    ul.add_argument("--task-id", required=True)
    ul.add_argument("--path")
    ul.set_defaults(func=command_unlock)

    sub.add_parser("release").set_defaults(func=command_release)
    rp = sub.add_parser("reap")
    rp.add_argument("--task-id")
    rp.set_defaults(func=command_reap)
    sub.add_parser("sweep").set_defaults(func=command_sweep)
    sub.add_parser("auto-push", help="hourly gate: push main to origin when strictly ahead and no landing is in flight "
                   "; run by command-center-loop.sh").set_defaults(func=command_auto_push)
    su = sub.add_parser("strip-unpushed", help="take private workstream and confidential session files out of main's "
                        "unpushed commits (origin/main..main) without a force push; files on disk are untouched")
    su.add_argument("--dry-run", action="store_true", help="rewrite in the object store and report; leave main where it is")
    su.add_argument("--no-fetch", action="store_true", help="use the local origin/main as is")
    su.set_defaults(func=command_strip_unpushed)

    rc = sub.add_parser("record", help="write the worker close-out line into the owning workstream's CONTEXT.md on main")
    rc.add_argument("--task-id", required=True)
    rc.add_argument("--status", help="terminal status to record (default: the row's)")
    rc.add_argument("--result", help="one-line result (default: the row's summary)")
    rc.add_argument("--thread-id", help="Amp thread id or Claude session id for the close-out line (default: the row's)")
    rc.set_defaults(func=command_record)

    dc = sub.add_parser("decide", help="CoS Review decision reply: annotate the ledger row and append the "
                        "decision under '## the owner decisions' in the owning workstream's CONTEXT.md on main; dispatches nothing")
    dc.add_argument("--task-id", required=True)
    dc.add_argument("--by", default="owner")
    dc.add_argument("text", nargs="+", help="the decision text (go / veto / keep / option N / free text)")
    dc.set_defaults(func=command_decide)

    ci = sub.add_parser("close-idle-owner", help="sweep's detached helper: wrap-up prompt, wait, close one idle owner tab")
    ci.add_argument("--ref", required=True)
    ci.add_argument("--title", required=True)
    ci.set_defaults(func=command_close_idle_owner)

    lc = sub.add_parser("locate")
    lc.add_argument("--thread", required=True)
    lc.set_defaults(func=command_locate)

    sc = sub.add_parser("scorecard")
    sc.add_argument("--week", action="store_true")
    sc.add_argument("--days", type=int, default=7)
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=command_scorecard)

    sub.add_parser("config").set_defaults(func=command_config)
    sub.add_parser("capacity", help="live workers, free RAM, swap used and launch-path cmux timeouts since the last "
                   "sweep; the sweep reports this, it never blocks a launch").set_defaults(
        func=command_capacity)
    sub.add_parser("check-symlinks", help="pre-commit: reject staged symlinks that point inside the repo").set_defaults(
        func=command_check_symlinks)

    va = sub.add_parser("verify-amp", help="confirm a freshly launched tab shows the Amp TUI; "
                        "retype the launch on a bare shell (once per LAUNCH_RETYPE_PAUSES_SECONDS), never type into an unknown screen")
    va.add_argument("--workspace-ref", required=True)
    va.add_argument("--command", required=True, help="the launch command to retype on a bare shell")
    va.add_argument("--label", default="owner-tab")
    va.add_argument("--seconds", type=float, default=None, help=f"verification budget (default {AMP_VERIFY_SECONDS})")
    va.add_argument("--poll", type=float, default=None, help=f"screen poll interval (default {AMP_VERIFY_POLL_SECONDS})")
    va.set_defaults(func=command_verify_amp)

    rt = sub.add_parser("retype", help="read every routing worker tab that never bound a thread; one back at a bare shell "
                        "(amp exited on a connection error) gets its launch command typed again (the sweep does this each tick)")
    rt.add_argument("--task-id", default=None, help="only this row (default: every eligible routing row)")
    rt.add_argument("--force", action="store_true", help="ignore SWEEP_RETYPE_MIN_AGE and MAX_LAUNCH_RETYPES")
    rt.set_defaults(func=command_retype)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
