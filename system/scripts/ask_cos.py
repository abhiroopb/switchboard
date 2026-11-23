#!/usr/bin/env python3
"""ask_cos.py: a worker's mid-task question, delivered to CoS, never asked in the worker tab.

Questions come back to the owner in CoS: the owner never has to look at a worker tab.
A worker that needs a ruling mid-task runs

  bash system/scripts/ask-cos.sh --task-id <rt-id> --option "<a>" --option "<b>" \\
       --default a "<question>"

and keeps working on its default. This script:

  1. records the question on the task's ledger row (`worker_questions`, newest last);
  2. sends CoS one line that starts `[WORKER QUESTION]`, naming the workstream, the
     question, the options, the default the worker is continuing on, and the exact
     steer command for the answer;
  3. delivers through `owner_tabs.agent_message` (cmux agent message on macOS, the
     wmux shim on Windows), which never types over a draft in the CoS composer or
     into an open dialog; a mid-turn Claude CoS reads the message at its next step.
     When CoS refuses (a draft, a dialog, no CoS tab), a detached retry loop tries
     again every ASK_COS_RETRY_SECONDS for ASK_COS_RETRY_MINUTES, so the worker
     never waits;
  4. prints JSON and exits 0 whatever happened: the worker continues on its default.

The answer comes back as a same-task steer: CoS runs
`bash system/scripts/router-dispatch.sh --origin cos --steer <rt-id> '<answer>'`, which
dispatch.py delivers into the live worker. `answered` on the question is set when
that steer lands (`ask_cos.py answered`), or by CoS by hand.

Test overrides: ASK_COS_DRY_RUN=1 (record, never send), ASK_COS_TARGET (a CoS ref).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import no_window  # noqa: E402
import owner_tabs  # noqa: E402

SENDER = "switchboard worker question"
PREFIX = "[WORKER QUESTION]"
RETRY_SECONDS = float(os.environ.get("ASK_COS_RETRY_SECONDS") or 30)
RETRY_MINUTES = float(os.environ.get("ASK_COS_RETRY_MINUTES") or 30)
LETTERS = "abcdefghij"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ledger():
    return owner_tabs._ledger()


def log(line: str) -> None:
    path = owner_tabs.REPO_ROOT / "system" / "state" / "logs" / "ask-cos.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {line}\n")
    except OSError:
        pass


def question_line(task: dict[str, Any], qid: str, question: str, options: list[str], default: str) -> str:
    slug = str(task.get("workstream") or "")
    name = owner_tabs.owner_name(slug) if slug else "a worker"
    opts = "; ".join(f"({LETTERS[i]}) {opt}" for i, opt in enumerate(options))
    text = f"{PREFIX} {name} worker ({task['id']}, {qid}) asks: {question.strip()}"
    if opts:
        text += f" Options: {opts}."
    if default:
        text += f" Default: {default}; the worker is continuing on it."
    text += (f" Ask the owner in CoS, then send the answer back with "
             f"bash system/scripts/router-dispatch.sh --origin cos --steer {task['id']} '<answer>'")
    return " ".join(text.split())   # one line: the wmux transport submits on a raw newline


def update_question(task_id: str, qid: str, **fields: Any) -> dict[str, Any] | None:
    def mutate(payload):
        row = ledger().find_task(payload, task_id)
        if row is None:
            return None
        for q in row.get("worker_questions") or []:
            if q.get("id") == qid:
                q.update(fields)
                row["updated_at"] = ledger().now_iso()
                return dict(q)
        return None
    return ledger().locked_mutation(mutate)


def cos_ref() -> str:
    forced = os.environ.get("ASK_COS_TARGET")
    if forced:
        return forced
    return str(owner_tabs.cos_tab(owner_tabs.cmux_workspaces(), owner_tabs.agents_by_workspace(),
                                  owner_tabs.configured_agent()).get("ref") or "")


def send_once(line: str) -> dict[str, Any]:
    if os.environ.get("ASK_COS_DRY_RUN") == "1":
        return {"ok": False, "reason": "dry run"}
    ref = cos_ref()
    if not ref:
        return {"ok": False, "reason": "no CoS tab open"}
    return {**owner_tabs.agent_message(ref, line, SENDER), "ref": ref}


def spawn_retry(task_id: str, qid: str) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "retry", "--task-id", task_id, "--qid", qid]
    # A hidden console on Windows, never DETACHED_PROCESS (no_window.py).
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              **no_window.detached_kwargs()}
    subprocess.Popen(cmd, **kwargs)


def capture_owner_action(call) -> None:
    """An open question is an owner action, so it sits on the to-do list until answered. The worker never waits on this and never fails over it."""
    try:
        import owner_action_capture
        log(f"owner action {json.dumps(call(owner_action_capture))}")
    except Exception as exc:  # noqa: BLE001
        log(f"owner action capture failed ({type(exc).__name__})")


def command_ask(args: argparse.Namespace) -> int:
    question = " ".join(args.question).strip()
    task = ledger().find_task(ledger().load_payload(), args.task_id)
    if task is None or not question:
        print(json.dumps({"asked": False, "reason": "no ledger row" if task is None else "empty question",
                          "task_id": args.task_id}))
        return 0
    default = args.default or ""
    if default and len(default) == 1 and default.lower() in LETTERS[:len(args.option)]:
        default = f"({default.lower()}) {args.option[LETTERS.index(default.lower())]}"

    def add(payload):
        row = ledger().find_task(payload, args.task_id)
        if row is None:
            return None
        questions = row.setdefault("worker_questions", [])
        qid = f"q{len(questions) + 1}"
        entry = {"id": qid, "asked_at": ledger().now_iso(), "question": question, "options": list(args.option),
                 "default": default, "delivered": False, "answered": False}
        questions.append(entry)
        row["updated_at"] = ledger().now_iso()
        return dict(entry)

    entry = ledger().locked_mutation(add)
    if entry is None:
        print(json.dumps({"asked": False, "reason": "ledger row vanished", "task_id": args.task_id}))
        return 0
    line = question_line(task, entry["id"], question, list(args.option), default)
    update_question(args.task_id, entry["id"], line=line)
    sent = send_once(line)
    if sent.get("ok"):
        update_question(args.task_id, entry["id"], delivered=True, delivered_at=now_iso(), cos_ref=sent.get("ref"))
        log(f"{args.task_id} {entry['id']} delivered to {sent.get('ref')}")
    else:
        update_question(args.task_id, entry["id"], last_refusal=sent.get("reason"))
        log(f"{args.task_id} {entry['id']} not delivered yet ({sent.get('reason')}); retrying in the background")
        if os.environ.get("ASK_COS_DRY_RUN") != "1" and not args.no_retry:
            spawn_retry(args.task_id, entry["id"])
    capture_owner_action(lambda capture: capture.capture_question_soon(task, entry))
    print(json.dumps({"asked": True, "task_id": args.task_id, "qid": entry["id"], "delivered": bool(sent.get("ok")),
                      "reason": sent.get("reason"), "default": default, "line": line,
                      "next": "continue on the default; the answer arrives as a steer in this thread"}))
    return 0


def command_retry(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + RETRY_MINUTES * 60
    while time.monotonic() < deadline:
        time.sleep(RETRY_SECONDS)
        task = ledger().find_task(ledger().load_payload(), args.task_id)
        q = next((q for q in (task or {}).get("worker_questions") or [] if q.get("id") == args.qid), None)
        if task is None or q is None or q.get("delivered") or q.get("answered"):
            return 0
        if task.get("status") not in ("routing", "active"):
            return 0   # the worker reported; its report carries the default it chose
        sent = send_once(str(q.get("line") or ""))
        if sent.get("ok"):
            update_question(args.task_id, args.qid, delivered=True, delivered_at=now_iso(), cos_ref=sent.get("ref"))
            log(f"{args.task_id} {args.qid} delivered to {sent.get('ref')} on retry")
            return 0
        update_question(args.task_id, args.qid, last_refusal=sent.get("reason"))
    update_question(args.task_id, args.qid, delivery_gave_up_at=now_iso())
    log(f"{args.task_id} {args.qid} not delivered after {RETRY_MINUTES:g} minutes; the worker's report must name the default it used")
    return 0


def command_answered(args: argparse.Namespace) -> int:
    task = ledger().find_task(ledger().load_payload(), args.task_id)
    open_q = [q for q in (task or {}).get("worker_questions") or [] if not q.get("answered")]
    qid = args.qid or (open_q[-1]["id"] if open_q else "")
    out = update_question(args.task_id, qid, answered=True, answered_at=now_iso(), answer=args.answer) if qid else None
    capture_owner_action(lambda capture: capture.sync_task(args.task_id))
    print(json.dumps({"answered": bool(out), "task_id": args.task_id, "qid": qid}))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    ask = sub.add_parser("ask")
    ask.add_argument("--task-id", required=True)
    ask.add_argument("--option", action="append", default=[], help="one option per flag, (a), (b), ... in order")
    ask.add_argument("--default", default="", help="the option letter or text the worker continues on")
    ask.add_argument("--no-retry", action="store_true")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=command_ask)
    retry = sub.add_parser("retry")
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--qid", required=True)
    retry.set_defaults(func=command_retry)
    answered = sub.add_parser("answered")
    answered.add_argument("--task-id", required=True)
    answered.add_argument("--qid", default="")
    answered.add_argument("--answer", default="")
    answered.set_defaults(func=command_answered)
    if argv and argv[0] not in ("ask", "retry", "answered", "-h", "--help"):
        argv = ["ask", *argv]   # `ask_cos.py --task-id ... "question"` is the ask form
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    no_window.install()
    sys.exit(main(sys.argv[1:]))
