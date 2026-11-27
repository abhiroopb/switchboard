#!/usr/bin/env python3
"""The dispatch task ledger: system/state/running-tasks.json.

One row per task, about a dozen fields:

  id            rt-<12hex>                 prompt        the ask (verbatim)
  workstream    owning slug                origin        cos | launchpad
  thread_id     worker Amp thread          workspace_ref worker cmux tab
  branch        task/<id>                  worktree      .amp/worktrees/<id>
  status        queued | routing | active | done | blocked | failed |
                cancelled | deferred
  created_at / started_at / finished_at    summary       one result line
  attempt_id    rt-<id>.aN.<hex> (fencing a re-dispatch)
  report_delivered  the origin saw the terminal summary
  depends_on / request_id / delivery       optional dispatch inputs

Subcommands: start, queue, ack, heartbeat, update, show, list, find-request,
report-delivered, report-unknown, report-attempted, report-not-delivered, report-unproven, report-alerted, summary, expire-leases,
register-native, migrate. `phase` is derived on output, never stored.

Retention: every write keeps all active rows, every terminal row whose report
is not yet delivered, and the RETAINED_DELIVERED_ROWS most recently finished
delivered rows; older delivered rows fall off the file. check-state-inventory.py
reads the cap from here so that trim never reads as a loss.
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_paths import main_checkout, shared_state_file  # noqa: E402

# One ledger, in the main checkout, even when a worker runs its worktree's copy
# of this file.
REPO_ROOT = main_checkout(Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2]))
STATE_FILE = Path(os.environ.get("RUNNING_TASKS_FILE") or REPO_ROOT / "system" / "state" / "running-tasks.json")
CMUX_SESSION_STORE = Path(
    os.environ.get("CC_CMUX_SESSION_JSON")
    or Path.home() / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json"
)
ACTIVE_STATUSES = {"queued", "routing", "active"}
TERMINAL_STATUSES = {"blocked", "deferred", "done", "failed", "cancelled"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
FIELDS = (
    "id", "prompt", "workstream", "origin", "thread_id", "workspace_ref", "branch", "worktree",
    "status", "created_at", "started_at", "finished_at", "updated_at", "heartbeat_at", "summary",
    "attempt_id", "attempt", "report_delivered", "report_delivery_unknown", "depends_on",
    "request_id", "delivery", "evidence", "urgent", "needs_ruling", "stale_since", "recovery_action", "decisions",
    "landed_commit", "report", "resume_sent_at", "resume_escalated_at", "reaped_at", "report_transport_errors",  # documentation only
)
MIGRATION_CUTOFF = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
STALE_AFTER = timedelta(seconds=int(os.environ.get("SWITCHBOARD_TASK_STALE_SECONDS") or 1800))
RETAINED_DELIVERED_ROWS = 600  # delivered terminal rows kept per write (newest by finished_at); see module docstring

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

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_origin(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in {"cos", "launchpad"} else "cos"


def phase_of(task: dict[str, Any]) -> str:
    status = task.get("status")
    return {"queued": "queued", "routing": "accepted", "active": "running", "done": "succeeded",
            "failed": "failed", "cancelled": "cancelled", "blocked": "blocked", "deferred": "blocked"}.get(status, "unknown")


def executor_thread(task: dict[str, Any]) -> str:
    return str(task.get("thread_id") or "")


# --- git-derived progress (worktree workers only) -----------------------------------
#
# Done is observable, not self-reported: a worktree task is
# done only when the commit `dispatch.py finish` landed is an ancestor of main.
# A worker report is the human-readable summary; it cannot flip status to done
# while branch task/<id> is still unmerged. Rows without a branch (native
# threads, owner tabs, pre-worktree rows) keep report-driven status.

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)


def landed(task: dict[str, Any]) -> bool:
    commit = task.get("landed_commit")
    if not commit:
        return False
    if _git("merge-base", "--is-ancestor", str(commit), "main").returncode == 0:
        return True
    # A landed commit among main's unpushed ones that `dispatch.py strip-unpushed` rewrote
    # keeps its old id here; the rewrite record maps it to the new one.
    import main_rewrites
    return main_rewrites.on_main(_git, str(commit), main_rewrites.load(STATE_FILE.parent))


def branch_tip_landed(task: dict[str, Any]) -> bool:
    """The branch's own tip is on main (directly, or through a strip-unpushed rewrite record).
    A branch already gone counts as landed: its landed commit is what is left of it."""
    branch = str(task.get("branch") or "")
    tip = _git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").stdout.strip() if branch else ""
    if not tip:
        return True
    if _git("merge-base", "--is-ancestor", tip, "main").returncode == 0:
        return True
    import main_rewrites
    return main_rewrites.on_main(_git, tip, main_rewrites.load(STATE_FILE.parent))


def git_state(task: dict[str, Any]) -> str | None:
    """'landed' | 'open' (branch exists, unmerged) | 'gone' | None (no branch)."""
    if not task.get("branch"):
        return None
    if landed(task):
        return "landed"
    return "open" if _git("rev-parse", "--verify", "-q", f"refs/heads/{task['branch']}").returncode == 0 else "gone"


# --- storage -----------------------------------------------------------------------

def load_payload() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 2, "tasks": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return {"version": 2, "tasks": []}
    return payload


def write_payload(payload: dict[str, Any]) -> None:
    payload["version"] = 2
    payload["updated_at"] = now_iso()
    tasks = [t for t in payload.get("tasks", []) if isinstance(t, dict)]
    for task in tasks:   # a CR from a Windows shell loop never stays in a stored id
        if isinstance(task.get("thread_id"), str) and task["thread_id"] != task["thread_id"].strip():
            task["thread_id"] = task["thread_id"].strip() or None
    active = [t for t in tasks if t.get("status") in ACTIVE_STATUSES]
    pending = [t for t in tasks if t.get("status") in TERMINAL_STATUSES and t.get("report_delivered") is False]
    delivered = sorted(
        (t for t in tasks if t.get("status") in TERMINAL_STATUSES and t.get("report_delivered") is not False),
        key=lambda t: str(t.get("finished_at") or t.get("updated_at") or t.get("created_at") or ""),
    )[-RETAINED_DELIVERED_ROWS:]
    payload["tasks"] = active + pending + delivered
    target = shared_state_file(STATE_FILE)  # the real file: os.replace onto a symlink replaces the link
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _os_replace(tmp, target)
    except BaseException:
        # A failed replace (a reader held the ledger open) must not leave
        # running-tasks.json.<pid>.tmp behind for later launches to link into new
        # worktrees. The write still fails loudly.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def locked_mutation(callback):
    target = shared_state_file(STATE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(".json.lock").open("a") as lock:  # "a": never truncate a held lock file
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = load_payload()
        before = json.dumps(payload, sort_keys=True)
        result = callback(payload)
        if json.dumps(payload, sort_keys=True) != before:  # no-op mutations leave the file untouched
            write_payload(payload)
        return result


def find_task(payload: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next((t for t in payload.get("tasks", []) if t.get("id") == task_id), None)


def find_request_twin(payload: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    rows = [t for t in payload.get("tasks", []) if request_id and t.get("request_id") == request_id]
    live = [t for t in rows if t.get("status") in ACTIVE_STATUSES]
    return (live or rows)[-1] if rows else None


def out(task: dict[str, Any], **flags: Any) -> int:
    print(json.dumps({**task, "phase": phase_of(task), **flags}, ensure_ascii=False))
    return 0


# cmux session-store agent kind -> statusEntries key. Same join as
# command-center-digest.py and workspace-session-state.py: an Amp tab is kind
# "amp" with status key "amp"; a Claude Code tab is kind "claude" with status key
# "claude_code" and its session uuid also on terminal.resumeBinding.checkpointId.
# Both kinds must match, or the sweep reads a live Claude worker as dead.
AGENT_STATUS_KEYS = {"amp": "amp", "claude": "claude_code"}


def live_agents() -> dict[str, dict[str, Any]]:
    """{thread_id: {"thread_id", "agent", "state", "state_ts", "workspace_id", "title"}}
    from the cmux session store (the lifecycle stamp), for Amp and Claude Code
    tabs alike; thread_id is the Amp thread id or the Claude session uuid.
    Missing/corrupt store -> {}."""
    try:
        data = json.loads(CMUX_SESSION_STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    agents: dict[str, dict[str, Any]] = {}
    for win in data.get("windows", []) or []:
        for ws in ((win.get("tabManager") or {}).get("workspaces") or []):
            thread_id, agent_kind = "", ""
            for panel in ws.get("panels") or []:
                terminal = panel.get("terminal") or {}
                agent = terminal.get("agent") or {}
                kind = str(agent.get("kind") or "")
                if kind not in AGENT_STATUS_KEYS:
                    continue
                session_id = agent.get("sessionId")
                if kind == "claude":
                    session_id = (terminal.get("resumeBinding") or {}).get("checkpointId") or session_id
                if session_id:
                    thread_id, agent_kind = str(session_id), kind
                    break
            if not thread_id:
                continue
            status_key = AGENT_STATUS_KEYS[agent_kind]
            status = next((e for e in ws.get("statusEntries") or [] if e.get("key") == status_key), None)
            agents[thread_id] = {
                "thread_id": thread_id,
                "agent": agent_kind,
                "state": str((status or {}).get("value") or ""),
                "state_ts": float((status or {}).get("timestamp") or 0),
                "workspace_id": (ws.get("workspaceId") or "").strip(),
                "title": str(ws.get("customTitle") or ws.get("title") or ""),
            }
    return agents


ATTEMPT_SCOPED = (
    # Everything that belongs to one worker attempt and must not leak into the next:
    # the worker's identity and checkout, its liveness stamps, its report fences,
    # and the sweep's per-attempt bookkeeping (resume / launch claims).
    "thread_id", "worker_id", "worker_thread_id", "workspace_ref", "worktree", "branch", "mode", "started_at",
    "finished_at", "heartbeat_at", "report_delivered", "report_delivery_unknown", "report_attempted_at",
    "stale_since", "recovery_action", "launching_at", "launched_at", "resume_sent_at", "resume_escalated_at", "landed_commit",
    "tab_title", "stuck_at", "wedged_at", "wedged", "workspace_ref_healed_from", "report_no_destination_at",
    "resume_worktree", "resume_branch", "resume_from_attempt", "resume_session", "resume_reason", "session_fork_from",
    "usage_limit", "paused", "usage_limit_resumed_from", "network_stop", "network_resumes",
    "launch_holds", "launch_hold_since", "launch_hold_reason", "launch_hold_told_at",
)


def clear_stall_marks(task: dict[str, Any]) -> None:
    """A heartbeat or start is live evidence: drop the sweep's stale and wedged
    marks so a worker that wakes up (a hung tool call finally returned) reads as
    running again. A later wedge is a new event and is flagged again."""
    for key in ("stale_since", "recovery_action", "wedged_at", "wedged"):
        task.pop(key, None)


def begin_attempt(task: dict[str, Any], *, status: str, summary: str) -> dict[str, Any]:
    """Advance `task` to its next attempt in place: new attempt number and id,
    the given status/summary, and no trace of the previous worker. The single
    definition of what an attempt id looks like and what a new attempt forgets
    (`start` and the sweep's relaunch of an abandoned row both use it)."""
    attempt = int(task.get("attempt") or 0) + 1
    for key in ATTEMPT_SCOPED:
        task.pop(key, None)
    task.update(status=status, summary=summary, attempt=attempt, attempt_began_at=now_iso(),
                attempt_id=f"{task['id']}.a{attempt}.{secrets.token_hex(3)}", updated_at=now_iso())
    return task


# --- commands ----------------------------------------------------------------------

def command_start(args: argparse.Namespace) -> int:
    stamp = now_iso()

    def mutate(payload):
        if args.request_id:
            twin = find_request_twin(payload, args.request_id)
            if twin is not None and twin.get("id") != args.task_id:
                return twin, {"deduplicated": True}
        task = find_task(payload, args.task_id)
        if task is None:
            task = {"id": args.task_id, "created_at": stamp, "attempt": 0}
            payload["tasks"].append(task)
        begin_attempt(task, status="queued" if args.queued else "routing",
                      summary=args.summary or ("waiting to start" if args.queued else "starting a worker"))
        task.update(prompt=args.prompt, origin=normalize_origin(args.origin))
        for key in ("workstream", "workspace_ref", "branch", "worktree", "request_id", "delivery"):
            if getattr(args, key, None):
                task[key] = getattr(args, key)
        deps = [d for d in (args.depends_on or []) if d and d != args.task_id]
        if deps:
            task["depends_on"] = list(dict.fromkeys([*(task.get("depends_on") or []), *deps]))
        return task, {}

    task, flags = locked_mutation(mutate)
    return out(task, **flags)


def command_queue(args: argparse.Namespace) -> int:
    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if args.release:
            if task.get("status") == "queued":
                task.update(status="routing", updated_at=now_iso(), summary=args.summary or "starting a worker")
        else:
            task.update(status="queued", updated_at=now_iso(), summary=args.summary or "waiting to start")
            if args.depends_on:
                task["depends_on"] = list(dict.fromkeys([*(task.get("depends_on") or []), *args.depends_on]))
        return task

    return out(locked_mutation(mutate))


def command_ack(args: argparse.Namespace) -> int:
    stamp = now_iso()

    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if task.get("status") in {"blocked", "deferred"} and getattr(args, "reopen", False) \
                and (not task.get("thread_id") or task["thread_id"] == args.thread_id):
            task.update(status="active", summary="resumed", updated_at=stamp)
            task.pop("finished_at", None)
            task.pop("report_delivered", None)
        if task.get("status") in TERMINAL_STATUSES:
            return task, {"_terminal": True}
        if args.attempt_id and task.get("attempt_id") and args.attempt_id != task["attempt_id"]:
            return task, {"_stale_attempt": True}
        if args.role != "worker":
            return task, {}
        if task.get("thread_id") and task["thread_id"] != args.thread_id:
            # A second thread cannot take over a task another worker already owns.
            return task, {"_fenced_executor": True}
        task["thread_id"] = args.thread_id
        if args.workspace_ref:
            task["workspace_ref"] = args.workspace_ref
        if args.workstream and not task.get("workstream"):
            task["workstream"] = args.workstream
        task.setdefault("started_at", stamp)
        task.update(status="active", heartbeat_at=stamp, updated_at=stamp,
                    summary=task.get("summary") if task.get("status") == "active" else "started")
        clear_stall_marks(task)
        return task, {}

    task, flags = locked_mutation(mutate)
    spawn_steer_delivery(task, flags)
    return out(task, **flags)


def spawn_steer_delivery(task: dict[str, Any], flags: dict[str, Any]) -> bool:
    """Queued steers: a steer CoS sent before this worker started
    waits on the row in `pending_steers`. The worker's ack (session start) and each
    heartbeat start a detached `dispatch.py deliver-steers --task-id ID`, which sends
    them in order by cmux agent message; the sweep runs the same pass as a backstop.
    Never blocks the hook. SWITCHBOARD_NO_STEER_SPAWN=1 turns it off."""
    if flags or os.environ.get("SWITCHBOARD_NO_STEER_SPAWN") == "1":
        return False
    if not any(isinstance(e, dict) and not e.get("delivered_at") and not e.get("dropped_at")
               for e in task.get("pending_steers") or []):
        return False
    try:
        log_path = shared_state_file(STATE_FILE).parent / "logs" / "steer-queue.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as handle:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "dispatch.py"), "deliver-steers",
                              "--task-id", str(task.get("id"))],
                             stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}))
        return True
    except OSError:
        return False


def command_heartbeat(args: argparse.Namespace) -> int:
    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if task.get("status") in TERMINAL_STATUSES:
            return task, {"_terminal": True}
        if args.attempt_id and task.get("attempt_id") and args.attempt_id != task["attempt_id"]:
            return task, {"_stale_attempt": True}
        if task.get("thread_id") and args.thread_id and task["thread_id"] != args.thread_id:
            return task, {"_fenced_executor": True}
        task["heartbeat_at"] = now_iso()
        clear_stall_marks(task)
        return task, {}

    task, flags = locked_mutation(mutate)
    spawn_steer_delivery(task, flags)
    return out(task, **flags)


def command_register_native(args: argparse.Namespace) -> int:
    """Bind a create_thread child (no cmux tab) to a task as its worker."""
    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if task.get("status") in TERMINAL_STATUSES:
            return task, {"_terminal": True}
        if args.attempt_id and task.get("attempt_id") and args.attempt_id != task["attempt_id"]:
            return task, {"_stale_attempt": True}
        if task.get("thread_id") and task["thread_id"] != args.thread_id:
            return task, {"_fenced_executor": True}
        task.update(thread_id=args.thread_id, updated_at=now_iso())
        return task, {}

    task, flags = locked_mutation(mutate)
    return out(task, **flags)


def command_update(args: argparse.Namespace) -> int:
    stamp = now_iso()
    status = args.status

    def mutate(payload):
        task = find_task(payload, args.task_id) if args.task_id else None
        if task is None and args.workstream and not args.task_id:
            task = next((t for t in reversed(payload["tasks"])
                         if t.get("workstream") == args.workstream and t.get("status") in ACTIVE_STATUSES), None)
        if task is None:
            if not args.create_if_missing and args.task_id:
                raise KeyError(args.task_id or args.workstream or "?")
            task = {"id": args.task_id or f"rt-{secrets.token_hex(6)}", "created_at": stamp, "attempt": 1,
                    "origin": normalize_origin(args.origin), "prompt": args.summary}
            task["attempt_id"] = f"{task['id']}.a1.{secrets.token_hex(3)}"
            payload["tasks"].append(task)
        if args.attempt_id and task.get("attempt_id") and args.attempt_id != task["attempt_id"]:
            return task, {"_stale_attempt": True}
        owner = task.get("thread_id")
        if task.get("status") == "active" and status in TERMINAL_STATUSES and owner \
                and args.thread_id != owner and not getattr(args, "replace", False) and owner in live_agents():
            # A live worker owns this row. Another caller's terminal verdict is a request, not a fact: message the worker,
            # or pass --replace-report to overrule it on purpose. Nothing is written.
            return task, {"_live_worker": True, "live_thread_id": owner,
                          "live_workspace_ref": task.get("workspace_ref")}
        for key in ("workstream", "thread_id", "workspace_ref", "delivery"):
            if getattr(args, key, None):
                task[key] = getattr(args, key)
        if args.origin:
            task["origin"] = normalize_origin(args.origin)
        if args.evidence:
            task["evidence"] = list(dict.fromkeys([*(task.get("evidence") or []), *args.evidence]))
        if args.urgent:
            task["urgent"] = True
        if args.needs_ruling:
            task["needs_ruling"] = True
        current = task.get("status")
        if current in TERMINAL_STATUSES and status in {"routing", "active"}:
            return task, {"_stale_attempt": True, "_late_active_report": True}
        if current == "active" and status == "routing":
            return task, {}
        if current in TERMINAL_STATUSES and status in TERMINAL_STATUSES and not getattr(args, "replace", False):
            # A settled row is immutable through update: one report per task.
            # Only --replace (used by the land-on-main conflict path) may change it.
            return task, {"_duplicate_terminal": True}
        if status == "done" and task.get("branch") and not landed(task) and not getattr(args, "replace", False):
            # Done is derived from git: the branch has not landed on main, so the
            # report is kept as the summary and the task stays in progress.
            task.update(summary=args.summary, report=args.summary, updated_at=stamp)
            return task, {"_not_landed": True, "git_state": git_state(task)}
        task.update(status=status, summary=args.summary, updated_at=stamp)
        if status in TERMINAL_STATUSES:
            task["finished_at"] = stamp
            task["report_delivered"] = False
            for key in ("report_delivery_unknown", "report_attempted_at", "admission_blocked", "blocked_kind"):
                task.pop(key, None)   # a terminal report supersedes any admission-blocked state
        elif status == "active":
            task.setdefault("started_at", stamp)
        return task, {}

    task, flags = locked_mutation(mutate)
    return out(task, **flags)


def command_annotate(args: argparse.Namespace) -> int:
    """Append one of the owner's decisions to a row without touching its status.

    CoS Review: a go / veto / keep / "option N" reply on a task
    records the decision on the ledger row and auto-completes the item; it does
    not dispatch a worker. ``update`` cannot carry it (a settled row is immutable
    there), so this is the one write path for ``decisions``: a list of
    ``{"at", "by", "text"}``, deduplicated on text, mirrored into ``evidence`` as
    ``decision: <text>`` so every reader of evidence sees it."""
    text = " ".join(str(args.text or "").split()).strip()
    if not text:
        print("error: --text is empty", file=sys.stderr)
        return 2
    stamp = now_iso()

    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        decisions = [d for d in (task.get("decisions") or []) if isinstance(d, dict)]
        if any(str(d.get("text") or "") == text for d in decisions):
            return task, {"_duplicate_decision": True}
        decisions.append({"at": stamp, "by": args.by or "owner", "text": text})
        task["decisions"] = decisions
        task["evidence"] = list(dict.fromkeys([*(task.get("evidence") or []), f"decision: {text}"]))
        task["updated_at"] = stamp
        return task, {}

    try:
        task, flags = locked_mutation(mutate)
    except KeyError as exc:
        print(f"error: no task matches {exc}", file=sys.stderr)
        return 2
    return out(task, **flags)


def command_show(args: argparse.Namespace) -> int:
    task = find_task(load_payload(), args.task_id)
    if task is None:
        print(f"error: no task matches {args.task_id}", file=sys.stderr)
        return 2
    return out(task, _terminal=task.get("status") in TERMINAL_STATUSES)


def command_list(args: argparse.Namespace) -> int:
    rows = [t for t in load_payload()["tasks"]
            if (not args.status or t.get("status") in args.status)
            and (not args.workstream or t.get("workstream") == args.workstream)
            and (not args.origin or normalize_origin(t.get("origin")) == args.origin)]
    if args.json:
        print(json.dumps([{**t, "phase": phase_of(t)} for t in rows], ensure_ascii=False))
        return 0
    for t in rows:
        print(f"{t.get('id')}\t{t.get('status'):9}\t{t.get('workstream') or '-':28}\t{t.get('workspace_ref') or '-':14}\t"
              f"{(t.get('summary') or t.get('prompt') or '')[:90]}")
    return 0


def command_find_request(args: argparse.Namespace) -> int:
    twin = find_request_twin(load_payload(), args.request_id)
    if twin is None:
        return 1
    return out(twin, deduplicated=True)


def _set_delivery(task_id: str, **values: Any) -> int:
    def mutate(payload):
        task = find_task(payload, task_id)
        if task is None:
            raise KeyError(task_id)
        task.update(values, updated_at=now_iso())
        for key, value in list(values.items()):
            if value is None:
                task.pop(key, None)
        return task

    return out(locked_mutation(mutate))


def command_report_delivered(args):
    extra = {"note": args.note} if getattr(args, "note", None) else {}
    return _set_delivery(args.task_id, report_delivered=True, report_delivered_at=now_iso(), report_delivery_unknown=None, **extra)


def command_report_unknown(args):
    return _set_delivery(args.task_id, report_delivery_unknown=True, report_attempted_at=now_iso())


def command_report_attempted(args):
    """The transport was invoked for this row (text may be on the composer): any
    later delivery goes only through the idle path and never pastes twice."""
    return _set_delivery(args.task_id, report_attempted_at=now_iso())


def command_report_not_attempted(args):
    """Clean deferral: the guard proved nothing was pasted, so the row may be tried again (idle path).
    --transport-error: the send itself failed (cmux nonzero) and the screen proved the line is not
    there; counted so report-to-cos.sh allows exactly one such retry per row.
    --no-destination: no origin tab is open, so the report has not had its chance; the 24-hour
    stale settle in report-to-cos.sh waits for it (first time stamped, kept after that)."""
    def mutate(payload):
        task = find_task(payload, args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if getattr(args, "no_destination", False):
            # Nothing was pasted this pass, but an earlier attempt may have been: keep its stamp so
            # a later delivery takes the idle path and never pastes twice.
            task.setdefault("report_no_destination_at", now_iso())
        else:
            task.pop("report_attempted_at", None)
        if getattr(args, "transport_error", False):
            task["report_transport_errors"] = int(task.get("report_transport_errors") or 0) + 1
        task["updated_at"] = now_iso()
        return task

    return out(locked_mutation(mutate))


def command_report_unproven(args):
    """The transport accepted the line but nothing proved it arrived (a wmux
    "queued" or "typed" send: no prompt-log entry yet). Not delivered, so reap
    keeps the tab; no report_attempted_at, so the next pass may retry, and it
    first asks `owner_tabs.py seen` so a line that did land is never sent twice."""
    extra = {"report_unproven_note": args.note} if getattr(args, "note", None) else {}
    return _set_delivery(args.task_id, report_unproven_at=now_iso(), **extra)


def command_report_alerted(args):
    """One CoS notification went out for this undelivered report."""
    extra = {"report_alert_note": args.note} if getattr(args, "note", None) else {}
    return _set_delivery(args.task_id, report_alerted_at=now_iso(), **extra)


def command_report_not_delivered(args):
    return _set_delivery(args.task_id, report_delivered=False, report_delivery_unknown=None)


def command_ack_delivered(args: argparse.Namespace) -> int:
    """Digest posted: mark several terminal rows delivered at once.

    The digest summarizes, it never replaces the direct report. A row whose report goes direct is only stamped report_in_digest_at
    here; it stays undelivered, so reap keeps its tab, until the direct line is
    proven on the CoS screen. Rows with an explicit digest policy (automation,
    start-of-day fan-out) and owner-tab turn mirrors (kind workspace-report,
    wr-* ids), which never go direct, are still settled by the digest."""
    def mutate(payload):
        acked, digested = [], []
        for task in payload["tasks"]:
            if task.get("id") in set(args.task_id) and task.get("status") in TERMINAL_STATUSES:
                digest_row = (task.get("delivery") == "digest" or task.get("kind") == "workspace-report"
                              or str(task.get("id") or "").startswith("wr-"))
                if not digest_row and task.get("report_delivered") is not True:
                    task.update(report_in_digest_at=now_iso(), updated_at=now_iso())
                    digested.append(task["id"])
                    continue
                task.update(report_delivered=True, report_delivered_at=now_iso(), updated_at=now_iso())
                task.pop("report_delivery_unknown", None)
                acked.append(task["id"])
        return {"acked": acked, "digested_only": digested}

    print(json.dumps(locked_mutation(mutate)))
    return 0


def command_sync_workspaces(args: argparse.Namespace) -> int:
    """stdin: [{thread_id, workspace_ref}] from the cmux surface watcher; bind
    workspace_ref on live rows whose worker thread is known. Read-only otherwise."""
    seen: dict[str, set[str]] = {}
    for b in json.load(sys.stdin):
        if isinstance(b, dict) and b.get("thread_id") and b.get("workspace_ref"):
            seen.setdefault(str(b["thread_id"]), set()).add(str(b["workspace_ref"]))
    # A thread shown in two workspaces at once is ambiguous: leave the row alone.
    bindings = {tid: next(iter(refs)) for tid, refs in seen.items() if len(refs) == 1}

    def mutate(payload):
        changed = []
        for task in payload["tasks"]:
            tid = task.get("thread_id") or ""
            ref = bindings.get(tid)
            if (task.get("status") in ACTIVE_STATUSES and ref and task.get("workspace_ref") != ref
                    and (task.get("executor") or {}).get("role", "worker") == "worker"):
                task.update(workspace_ref=ref, updated_at=now_iso())
                changed.append({"task_id": task["id"], "thread_id": tid, "workspace_ref": ref})
        return {"changed": changed}

    print(json.dumps(locked_mutation(mutate)))
    return 0


def command_summary(args: argparse.Namespace) -> int:
    origin = normalize_origin(args.origin) if args.origin else ""
    tasks = [{**t, "phase": phase_of(t)} for t in load_payload()["tasks"]
             if not origin or normalize_origin(t.get("origin")) == origin]
    active = [t for t in tasks if t.get("status") in ACTIVE_STATUSES]
    pending = [t for t in tasks if t.get("status") in TERMINAL_STATUSES and t.get("report_delivered") is False]
    stale = [t for t in active if t.get("stale_since")]
    print(json.dumps({
        "active_count": len(active),
        "routing_count": sum(t.get("status") in {"routing", "queued"} for t in active),
        "working_count": sum(t.get("status") == "active" for t in active),
        "received_count": sum(bool(t.get("started_at")) for t in active),
        "stale_count": len(stale), "unknown_write_count": 0, "pending_report_count": len(pending),
        "tasks": active, "stale": stale, "unknown_writes": [], "pending_reports": pending,
    }, ensure_ascii=False))
    return 0


CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
_SESSION_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UNREPORTED_TAIL_CHARS = 1200
BOUND_QUIET_FACTOR = 3   # landed row, session still bound to its tab: quiet for up to 3 stale windows


def claude_tail(task: dict[str, Any]) -> dict[str, Any] | None:
    """{"text": last main-thread assistant text, "last_at": the latest sign of life} from the
    worker's Claude Code transcript, or None (Amp worker, no transcript, unreadable). Sub-agent
    work counts as life: sidechain records, and the files under <session>/ that newer Claude
    Code writes for sub-agents, so a long Task call never reads as a quiet worker."""
    session = str(task.get("session_id") or task.get("thread_id") or "")
    if not _SESSION_UUID.match(session) or not CLAUDE_PROJECTS.is_dir():
        return None
    path = next((f / f"{session}.jsonl" for f in CLAUDE_PROJECTS.iterdir() if (f / f"{session}.jsonl").is_file()), None)
    if path is None:
        return None
    text, last_at = "", None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") not in {"user", "assistant"}:
                    continue
                last_at = max(filter(None, (last_at, parse_iso(record.get("timestamp")))), default=None)
                if record.get("isSidechain"):
                    continue
                content = (record.get("message") or {}).get("content") if record["type"] == "assistant" else None
                parts = [content] if isinstance(content, str) else \
                    [str(p.get("text") or "") for p in (content or []) if isinstance(p, dict) and p.get("type") == "text"]
                said = "\n".join(p for p in parts if p.strip()).strip()
                if said:
                    text = said
    except (OSError, ValueError):   # ValueError: a multi-byte character caught mid-write
        return None
    try:
        files = [path, *(f for f in (path.parent / session).rglob("*") if f.is_file())] \
            if (path.parent / session).is_dir() else [path]
        touched = datetime.fromtimestamp(max(f.stat().st_mtime for f in files), tz=timezone.utc)
        last_at = max(filter(None, (last_at, touched)), default=None)
    except (OSError, ValueError):
        pass
    return {"text": text, "last_at": last_at}


def worker_live(task: dict[str, Any], agents: dict[str, dict[str, Any]], now: datetime) -> bool:
    """The worker showed life within STALE_AFTER: a fresh non-idle cmux stamp, a heartbeat or ack,
    or a write to its Claude transcript (wmux strips the heartbeat hook on Windows)."""
    agent = agents.get(task.get("thread_id") or "") or {}
    stamped = datetime.fromtimestamp(float(agent.get("state_ts") or 0), tz=timezone.utc) if agent.get("state_ts") else None
    if agent and agent.get("state") not in {"", "Idle", "Needs input"} and stamped is not None and now - stamped < STALE_AFTER:
        return True
    tail = claude_tail(task)
    stamps = [parse_iso(task.get("heartbeat_at")), parse_iso(task.get("started_at")), (tail or {}).get("last_at")]
    latest = max((s for s in stamps if s is not None), default=None)
    # A session still bound to its tab may be waiting on a long background job, which writes
    # nothing, so it gets a longer window;
    # a worker that died still closes, later. The dead-worker sweep owns tabs that are gone.
    window = STALE_AFTER * BOUND_QUIET_FACTOR if agent else STALE_AFTER
    return latest is not None and now - latest < window


def unreported_landing_summary(task: dict[str, Any]) -> str:
    """The done summary for a landed row whose worker went quiet without report-to-cos.sh: what
    landed, that no report came, and the worker's own last message, so the owner gets substance
    instead of a bare commit id."""
    commit = task.get("landed_commit")
    head = f"Landed on main ({commit}), but the worker went quiet without sending its report."
    tail = claude_tail(task)
    said = " ".join(str((tail or {}).get("text") or "").split())
    if not said:
        return head + " Its last message was not readable; check the branch's commits and the workstream note."
    if len(said) > UNREPORTED_TAIL_CHARS:
        said = said[:UNREPORTED_TAIL_CHARS].rsplit(" ", 1)[0] + " …"
    return f"{head} Its last message: {said}"


def command_expire_leases(args: argparse.Namespace) -> int:
    """Flag (never terminate) active tasks whose worker is neither heartbeating
    nor Running in cmux. Records stale_since + recovery_action for the boards."""
    now = datetime.now(timezone.utc)
    agents = live_agents()

    def mutate(payload):
        changed = []
        for task in payload["tasks"]:
            if task.get("status") not in {"routing", "active"}:
                continue
            state = git_state(task)
            if state == "landed" and not branch_tip_landed(task):
                # The first landing is on main, but the branch has commits after it: not done.
                # The usual liveness rules below flag a quiet worker.
                state = "open"
            if state == "landed":
                # A worker may land part of its work with a standalone `dispatch.py finish` and keep
                # going. While the worker is
                # still live and has not reported, the row stays active; it reports for itself.
                if not task.get("report") and worker_live(task, agents, now):
                    if not task.get("landed_midtask_at"):
                        task["landed_midtask_at"] = now_iso()
                        changed.append(task)
                    continue
                # Status derives from git: the branch is on main and the worker went quiet without a
                # report, so the task is done. One report follows, carrying the worker's last words.
                task.update(status="done", finished_at=now_iso(), updated_at=now_iso(), report_delivered=False,
                            summary=task.get("report") or unreported_landing_summary(task))
                task.pop("stale_since", None); task.pop("recovery_action", None); task.pop("landed_midtask_at", None)
                changed.append(task)
                continue
            if state == "gone":
                task.update(status="failed", finished_at=now_iso(), updated_at=now_iso(), report_delivered=False,
                            summary=f"failed: branch {task['branch']} disappeared without landing on main")
                changed.append(task)
                continue
            last = parse_iso(task.get("heartbeat_at") or task.get("started_at") or task.get("updated_at") or task.get("created_at"))
            # A "Running" stamp counts only while it is fresh: the cmux lifecycle can
            # stick at Running after the Amp thread goes idle.
            agent = agents.get(task.get("thread_id") or "") or {}
            stamped = datetime.fromtimestamp(float(agent.get("state_ts") or 0), tz=timezone.utc) if agent.get("state_ts") else None
            running = bool(agent) and agent.get("state") not in {"", "Idle", "Needs input"} \
                and stamped is not None and now - stamped < STALE_AFTER
            if running or last is None or now - last < STALE_AFTER:
                if task.get("stale_since") or task.get("wedged_at"):
                    clear_stall_marks(task)
                    changed.append(task)
                continue
            if not task.get("stale_since"):
                task["stale_since"] = now_iso()
                task["recovery_action"] = (f"no heartbeat since {last.isoformat(timespec='seconds')} and the worker is not "
                                           f"Running in cmux; check {task.get('workspace_ref') or 'its tab'}, then re-dispatch "
                                           f"with router-dispatch.sh --task-id {task['id']} if the work did not land")
                changed.append(task)
        return {"checked_at": now_iso(), "live_agents": len(agents), "changed": changed}

    print(json.dumps(locked_mutation(mutate), ensure_ascii=False))
    return 0


def slim(task: dict[str, Any]) -> dict[str, Any]:
    """Project a legacy 64-field row onto the slim shape."""
    executor = task.get("executor") or {}
    status = task.get("status")
    row = {
        "id": task.get("id"), "prompt": task.get("prompt") or task.get("ask") or "",
        "workstream": task.get("workstream") or None, "origin": normalize_origin(task.get("origin")),
        "thread_id": task.get("worker_thread_id") or executor.get("thread_id") or task.get("thread_id") or None,
        "workspace_ref": task.get("workspace_ref") or executor.get("workspace_ref") or task.get("workstream_workspace_ref") or None,
        "branch": task.get("branch"), "worktree": task.get("worktree"),
        "status": status if status in ALL_STATUSES else "failed",
        "created_at": task.get("created_at"), "started_at": task.get("started_at") or task.get("received_at"),
        "finished_at": task.get("finished_at") or task.get("completed_at"), "updated_at": task.get("updated_at"),
        "summary": task.get("summary") or task.get("result") or "",
        "attempt_id": task.get("attempt_id"), "attempt": task.get("attempt") or 1,
    }
    if task.get("liveness") == "queued" and status == "routing":
        row["status"] = "queued"
    if row["status"] in TERMINAL_STATUSES:
        # Delivery state is carried over exactly; it is never defaulted. Rows that
        # pre-date the slim ledger (MIGRATION_CUTOFF) were all reported through the
        # old path and must never be re-sent, so they are settled as delivered.
        row["report_delivered"] = task.get("report_delivered") is True
        if task.get("report_delivered_at"):
            row["report_delivered_at"] = task["report_delivered_at"]
        finished = parse_iso(row.get("finished_at") or task.get("updated_at") or task.get("created_at"))
        if not row["report_delivered"] and finished is not None and finished < MIGRATION_CUTOFF:
            row.update(report_delivered=True, report_delivered_at=now_iso(), note="backfilled after ledger migration")
        row["reaped_at"] = "legacy"  # pre-worktree rows ran in owner tabs; the reaper never touches them
    for key in ("depends_on", "request_id", "evidence", "urgent", "needs_ruling", "heartbeat_at", "report_delivery_unknown",
                "report_attempted_at", "note", "decisions"):
        if task.get(key):
            row[key] = task[key]
    if task.get("completion_delivery"):
        row["delivery"] = task["completion_delivery"]
    return {k: v for k, v in row.items() if v is not None}


def command_migrate(args: argparse.Namespace) -> int:
    """Shrink every row in place after writing a timestamped backup."""
    backup = STATE_FILE.with_name(f"running-tasks.json.pre-slim-{datetime.now().strftime('%Y%m%dT%H%M%S')}.bak")

    def mutate(payload):
        legacy = [t for t in payload["tasks"] if isinstance(t, dict) and ("liveness" in t or "executor" in t)]
        if not legacy and not args.force:
            return {"migrated": False, "reason": "no legacy rows", "rows": len(payload["tasks"])}
        shutil.copy2(STATE_FILE, backup)
        before = sum(len(t) for t in payload["tasks"])
        payload["tasks"] = [slim(t) for t in payload["tasks"] if isinstance(t, dict) and t.get("id")]
        after = sum(len(t) for t in payload["tasks"])
        return {"migrated": True, "backup": str(backup), "rows": len(payload["tasks"]),
                "fields_before": before, "fields_after": after}

    print(json.dumps(locked_mutation(mutate), ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--task-id", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--origin", default="cos")
    start.add_argument("--workstream")
    start.add_argument("--workspace-ref")
    start.add_argument("--branch")
    start.add_argument("--worktree")
    start.add_argument("--request-id")
    start.add_argument("--delivery")
    start.add_argument("--summary")
    start.add_argument("--queued", action="store_true")
    start.add_argument("--depends-on", action="append")
    start.add_argument("--router", help="ignored (legacy)")
    start.add_argument("--max-attempts", help="ignored (legacy)")
    start.add_argument("--completion-delivery", dest="delivery")
    start.set_defaults(func=command_start)

    queue = sub.add_parser("queue")
    queue.add_argument("--task-id", required=True)
    queue.add_argument("--release", action="store_true")
    queue.add_argument("--summary")
    queue.add_argument("--depends-on", action="append")
    queue.add_argument("--attempt-id", help="ignored")
    queue.add_argument("--workstream", help="ignored")
    queue.set_defaults(func=command_queue)

    for name, func in (("ack", command_ack), ("heartbeat", command_heartbeat)):
        p = sub.add_parser(name)
        p.add_argument("--task-id", required=True)
        p.add_argument("--role", default="worker")
        p.add_argument("--thread-id", required=True)
        p.add_argument("--workspace-ref")
        p.add_argument("--attempt-id")
        p.add_argument("--workstream")
        p.add_argument("--reopen", action="store_true", help="a blocked/deferred task resumes in its own worker")
        p.set_defaults(func=func)

    native = sub.add_parser("register-native")
    native.add_argument("--task-id", required=True)
    native.add_argument("--thread-id", required=True)
    native.add_argument("--attempt-id")
    native.add_argument("--executor")
    native.add_argument("--local-claim", action="store_true")
    native.add_argument("--write-target", action="append")
    native.add_argument("--no-local-writes", action="store_true")
    native.set_defaults(func=command_register_native)

    update = sub.add_parser("update")
    update.add_argument("--task-id")
    update.add_argument("--status", required=True, choices=sorted(ALL_STATUSES))
    update.add_argument("--summary", required=True)
    update.add_argument("--workstream")
    update.add_argument("--thread-id")
    update.add_argument("--workspace-ref")
    update.add_argument("--origin")
    update.add_argument("--attempt-id")
    update.add_argument("--evidence", action="append")
    update.add_argument("--urgent", action="store_true")
    update.add_argument("--needs-ruling", action="store_true")
    update.add_argument("--create-if-missing", action="store_true")
    update.add_argument("--delivery", help="store the row's delivery policy (digest keeps a done line out of the composer; "
                        "direct pastes a done line even after hours, when done reports otherwise batch; "
                        "used by dispatch.py auto-push and report-to-cos.sh)")
    for legacy in ("--kind", "--source-workspace", "--expected-report-revision"):
        update.add_argument(legacy, help="ignored (legacy)")
    update.add_argument("--replace-report", "--replace", dest="replace", action="store_true",
                        help="allow changing an already-terminal row (one report per task otherwise), or overrule "
                             "the live-worker fence; the sanctioned override when an owner tab cancelled a row whose "
                             "worker was live and its work landed")
    update.set_defaults(func=command_update)

    show = sub.add_parser("show")
    show.add_argument("--task-id", required=True)
    show.set_defaults(func=command_show)

    ann = sub.add_parser("annotate", help="append one of the owner's decisions to a row (any status) without changing it; "
                         "used by CoS Review decision replies")
    ann.add_argument("--task-id", required=True)
    ann.add_argument("--text", required=True, help="the decision as the owner gave it (go, veto, keep, option B, ...)")
    ann.add_argument("--by", default="owner")
    ann.set_defaults(func=command_annotate)

    lst = sub.add_parser("list")
    lst.add_argument("--status", action="append")
    lst.add_argument("--workstream")
    lst.add_argument("--origin")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=command_list)

    fr = sub.add_parser("find-request")
    fr.add_argument("--request-id", required=True)
    fr.set_defaults(func=command_find_request)

    for name, func in (("report-delivered", command_report_delivered), ("report-unknown", command_report_unknown),
                       ("report-attempted", command_report_attempted), ("report-not-attempted", command_report_not_attempted),
                       ("report-not-delivered", command_report_not_delivered),
                       ("report-unproven", command_report_unproven), ("report-alerted", command_report_alerted)):
        p = sub.add_parser(name)
        p.add_argument("--task-id", required=True)
        p.add_argument("--attempt-id", help="ignored")
        p.add_argument("--expected-report-revision", help="ignored")
        p.add_argument("--evidence", action="append", help="ignored")
        p.add_argument("--note")
        if name == "report-not-attempted":
            p.add_argument("--no-destination", action="store_true",
                           help="no origin tab is open; the report stays pending past the stale window")
            p.add_argument("--transport-error", action="store_true",
                           help="the send failed and the screen proved the line is absent; counted (one retry per row)")
        p.set_defaults(func=func)

    ackd = sub.add_parser("ack-delivered")
    ackd.add_argument("--task-id", action="append", required=True)
    ackd.set_defaults(func=command_ack_delivered)

    sub.add_parser("sync-workspaces").set_defaults(func=command_sync_workspaces)

    summary = sub.add_parser("summary")
    summary.add_argument("--origin")
    summary.set_defaults(func=command_summary)

    expire = sub.add_parser("expire-leases")
    expire.add_argument("--verbose", action="store_true")
    expire.add_argument("--grace-seconds", type=int, help="ignored")
    expire.set_defaults(func=command_expire_leases)

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--force", action="store_true")
    migrate.set_defaults(func=command_migrate)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyError as error:
        print(f"error: no task matches {error.args[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
