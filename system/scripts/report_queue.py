#!/usr/bin/env python3
"""One delivery queue per destination tab.

One waiter per deferred report would let many waiters poll the same busy tab,
deliver in whatever order each poll came up, and queue old system notices beside
fresh owner results. So the first waiter for a tab holds that tab's queue and
delivers every pending report for its origin, one proven line at a time, in this
order:

  1. owner task reports (rt-…), oldest finished first;
  2. system notices (auto-push-…, skill-sync-…, capacity-…, digest-stale-…), and of
     each kind only the newest: older notices of the same kind are settled as
     superseded, so one auto-push line stands for the run of them.

Every later waiter for the same tab saves its original report-to-cos.sh arguments
here and exits; the queue replays those exact arguments, so a full worker report is
never cut to its ledger summary.

  report_queue.py save --task-id T -- <report-to-cos.sh args...>
  report_queue.py next --origin cos [--skip T]...     JSON {task_id, unproven, args} or {}
  report_queue.py fold --origin cos                   settle superseded system notices
  report_queue.py waiting --origin cos                one line naming what waits, or ""

State: system/state/worker-dispatch/report-queue/<task>.args (NUL-separated).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TERMINAL = {"blocked", "deferred", "done", "failed"}
NOTICE_KIND = re.compile(r"^(?P<kind>[a-z][a-z-]*?)-\d{8}(?:-\d{2,6})?$")


def _ledger():
    spec = importlib.util.spec_from_file_location("running_task_state", HERE / "running-task-state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def state_dir() -> Path:
    env = os.environ.get("PM_OS_STATE_DIR")
    if env:
        return Path(env)
    return Path(_ledger().STATE_FILE).parent


def queue_dir() -> Path:
    return state_dir() / "worker-dispatch" / "report-queue"


def args_path(task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_id)
    return queue_dir() / f"{safe}.args"


def is_owner_task(task_id: str) -> bool:
    return task_id.startswith("rt-")


def notice_kind(task_id: str) -> str:
    m = NOTICE_KIND.match(task_id)
    return m.group("kind") if m else ""


def load_rows() -> list[dict[str, Any]]:
    path = os.environ.get("RUNNING_TASKS_FILE")
    try:
        if path:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            payload = _ledger().load_payload()
    except Exception:
        return []
    return [t for t in payload.get("tasks", []) if isinstance(t, dict)]


def pending(rows: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    out = []
    for t in rows:
        if str(t.get("origin") or "cos") != origin or t.get("status") not in TERMINAL:
            continue
        if t.get("report_delivered") is not False or t.get("report_delivery_unknown") is True:
            continue
        if not t.get("id"):
            continue
        out.append(t)
    return out


def finished(t: dict[str, Any]) -> str:
    return str(t.get("finished_at") or t.get("updated_at") or t.get("created_at") or "")


def superseded(rows: list[dict[str, Any]]) -> dict[str, str]:
    """{older notice id: newest notice id of the same kind}."""
    newest: dict[str, dict[str, Any]] = {}
    for t in rows:
        kind = notice_kind(str(t["id"]))
        if kind and not is_owner_task(str(t["id"])):
            if kind not in newest or finished(t) > finished(newest[kind]):
                newest[kind] = t
    out = {}
    for t in rows:
        kind = notice_kind(str(t["id"]))
        if kind and kind in newest and newest[kind] is not t and not is_owner_task(str(t["id"])):
            out[str(t["id"])] = str(newest[kind]["id"])
    return out


def ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gone = superseded(rows)
    live = [t for t in rows if str(t["id"]) not in gone]
    return sorted(live, key=lambda t: (0 if is_owner_task(str(t["id"])) else 1, finished(t), str(t["id"])))


def rebuilt_args(t: dict[str, Any]) -> list[str]:
    """The same arguments the command-center loop retries with, for a row no waiter saved."""
    origin = str(t.get("origin") or "cos")
    status = str(t.get("status") or "done")
    args = ["--task-id", str(t["id"]), "--origin", origin, "--status", status, "--wait-seconds", "0"]
    if origin != "cos":
        args += ["--delivery", "direct"]
    if t.get("kind") == "workspace-report":
        args += ["--report-kind", "workspace"]
    for flag, key in (("--workstream", "workstream"), ("--attempt-id", "attempt_id"), ("--thread-id", "thread_id")):
        if t.get(key):
            args += [flag, str(t[key])]
    return args + [str(t.get("summary") or "")]


def saved_args(task_id: str) -> list[str] | None:
    try:
        raw = args_path(task_id).read_bytes()
    except OSError:
        return None
    parts = raw.decode("utf-8").split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    return parts or None


def cmd_save(task_id: str, args: list[str]) -> int:
    path = args_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(b"".join(a.encode("utf-8") + b"\0" for a in args))
    tmp.replace(path)
    return 0


def cmd_next(origin: str, skip: set[str]) -> int:
    for t in ranked(pending(load_rows(), origin)):
        tid = str(t["id"])
        if tid in skip:
            continue
        print(json.dumps({"task_id": tid, "unproven": str(t.get("report_unproven_at") or ""),
                          "args": saved_args(tid) or rebuilt_args(t)}))
        return 0
    print("{}")
    return 0


def cmd_fold(origin: str) -> int:
    gone = superseded(pending(load_rows(), origin))
    if not gone:
        print(json.dumps({"folded": 0}))
        return 0
    folded = 0
    for old, new in sorted(gone.items()):
        argv = [sys.executable, str(HERE / "running-task-state.py"), "report-delivered", "--task-id", old,
                "--note", f"settled without send: superseded by the newer notice {new}"]
        try:
            rc = subprocess.run(argv, capture_output=True, text=True, timeout=60).returncode
        except (OSError, subprocess.TimeoutExpired):
            rc = 1
        if rc == 0:
            folded += 1
            try:
                args_path(old).unlink()
            except OSError:
                pass
    print(json.dumps({"folded": folded, "superseded": gone}))
    return 0


def workstream_name(slug: str) -> str:
    """The workstream's display name from its config.yaml, else the slug in words."""
    if not slug:
        return "A task"
    try:
        for line in (HERE.parent.parent / "workstreams" / slug / "config.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
                if name:
                    return name
    except OSError:
        pass
    return slug.replace("-", " ").title()


def cmd_waiting(origin: str) -> int:
    rows = ranked(pending(load_rows(), origin))
    if not rows:
        print("")
        return 0
    owner = [t for t in rows if is_owner_task(str(t["id"]))]
    notices = len(rows) - len(owner)
    names = []
    for t in owner[:3]:
        names.append(f"{workstream_name(str(t.get('workstream') or ''))} ({t.get('status')})")
    more = len(owner) - len(names)
    text = f"{len(rows)} PM OS report{'s' if len(rows) != 1 else ''} waiting for CoS"
    detail = ", ".join(names) + (f", {more} more" if more > 0 else "")
    if notices:
        detail += (", " if detail else "") + f"{notices} system notice{'s' if notices != 1 else ''}"
    print(f"{text}: {detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, passthrough = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("save", "next", "fold", "waiting"))
    ap.add_argument("--task-id", default="")
    ap.add_argument("--origin", default="cos")
    ap.add_argument("--skip", action="append", default=[])
    a = ap.parse_args(argv)
    if a.command == "save":
        if not a.task_id:
            ap.error("save needs --task-id")
        return cmd_save(a.task_id, passthrough)
    if a.command == "next":
        return cmd_next(a.origin, set(a.skip))
    if a.command == "fold":
        return cmd_fold(a.origin)
    return cmd_waiting(a.origin)


if __name__ == "__main__":
    sys.exit(main())
