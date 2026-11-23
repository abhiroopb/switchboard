#!/usr/bin/env python3
"""turn_wait.py — a turn that ends waiting on the owner reaches CoS once, directly.

Why: a worker that stops mid-task for the owner's approval would otherwise
send CoS nothing. A worker's only callback is report-to-cos.sh at the
end of the task, so a worker that parks mid-task for a ruling was invisible
until the sweep's stall nudge.

  turn_wait.py classify --text-file F
      {"waiting": bool, "reason": "..."}: does this final assistant text end
      the turn waiting on the owner (a question, an approval, a ruling)?
  turn_wait.py park --thread-id ID --text-file F [--dry-run]
      For the live dispatch row bound to this thread (a worker, or an owner
      tab a dispatch was sent to), send one needs-ruling callback to the
      row's origin with the final text verbatim: report-to-cos.sh --status
      active --needs-ruling --delivery direct. The row stays active; the
      worker still finishes with its own done report. One callback per
      distinct waiting text per task (state: worker-dispatch/turn-wait.json).

Callers: task-liveness.ts agent.end (Amp and the Claude hook bridge) runs
`park` detached for every finished turn; workspace-report.ts runs `classify`
for an owner-tab turn a human typed, and passes --needs-ruling with the full
text when it waits on the owner and no live dispatch row is bound to the thread
("bound": park already sends that ask, so one parked turn is one callback).
Kill switch: TURN_WAIT_DISABLED=1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

LIVE_STATUSES = {"routing", "active"}
MAX_PROSE = 6000   # the question plus context; fenced drafts are kept whole on top of this

_FENCE = re.compile(r"```.*?```", re.S)

# An explicit ask of the reader. Each pattern names the reader ("you", "your",
# "the owner") or a ruling noun, so narration ("I checked the approval flow") does
# not match.
_ASK = [
    (r"\b(?:waiting|wait) (?:for|on) (?:your|you|owner)\b", "waiting on you"),
    (r"\byour (?:go(?:-ahead)?|ok(?:ay)?|approval|sign-?off|call|ruling|decision|pick|answer|review|confirmation)\b", "your go"),
    (r"\b(?:until|once|after|when) (?:you|owner) (?:approve|confirm|say|reply|decide|pick|choose|review|give)\b", "after you approve"),
    (r"\b(?:should|shall|can|may) i\b[^.?\n]{0,160}\?", "should I"),
    (r"\b(?:do|would) you (?:want|like|prefer)\b", "do you want"),
    (r"\bwant me to\b", "want me to"),
    (r"\b(?:which|what) (?:option|one|version|copy|image|wording|path)\b[^.?\n]{0,120}\?", "which option"),
    (r"\b(?:approve|confirm|pick|choose)\b[^.\n]{0,160}\?", "approve?"),
    (r"\b(?:ok(?:ay)?|good) to (?:apply|publish|send|post|save|ship|proceed|go)\b", "okay to proceed"),
    (r"\bneeds? (?:your|owner'?s?) (?:ruling|approval|decision|go|input|answer)\b", "needs your ruling"),
    (r"^\s*options?:\s*\(a\)", "options"),
    # the owner's option-set shape (numbered blockquote, AGENTS.md Output Preferences),
    # such as "> 1. **Go (recommended):** …".
    (r"^\s*>\s*[1-3][.)]\s", "option set"),
    (r"\((?:recommended|my lean|lean)\)", "recommended option"),
    (r"\blean:\s*\(?[a-c]\b", "lean"),
    (r"\bsay (?:\"|')?(?:go|send it|approve|yes)\b", "say go"),
]
# The turn is waiting on a tool or a timer, not on the owner.
_NOT_OWNER = [
    r"\bwaiting (?:for|on) (?:the )?(?:background|build|tests?|ci|job|subagent|agent|worker|deploy|command)\b",
    r"\bi'?ll (?:check back|be notified|resume) when\b",
    r"\bscheduled a wakeup\b",
]


def _prose(text: str) -> str:
    return _FENCE.sub(" ", text or "")


def classify(text: str) -> dict[str, object]:
    """Waiting on the owner when the last part of the turn asks them something.

    Only the tail counts (the last 1,200 prose characters): a question quoted
    early in a long report is history, the closing lines are the ask."""
    prose = _prose(text).strip()
    if not prose and "```" not in (text or ""):
        return {"waiting": False, "reason": "empty"}
    tail = prose[-1200:]
    low = tail.lower()
    for pattern in _NOT_OWNER:
        if re.search(pattern, low) and not re.search(r"\byou\b[^.\n]{0,80}\?", low):
            return {"waiting": False, "reason": "waiting on a tool or timer"}
    for pattern, reason in _ASK:
        if re.search(pattern, low, re.M):
            return {"waiting": True, "reason": reason}
    last = [line.strip() for line in tail.splitlines() if line.strip()]
    if last and last[-1].rstrip("*_ )").endswith("?"):
        return {"waiting": True, "reason": "closing question"}
    return {"waiting": False, "reason": "no ask of the owner"}


def _state_dir() -> Path:
    if os.environ.get("SWITCHBOARD_STATE_DIR"):
        return Path(os.environ["SWITCHBOARD_STATE_DIR"])
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    return Path(root or SCRIPTS.parent.parent) / "system" / "state"


def _ledger_rows() -> list[dict]:
    path = Path(os.environ.get("RUNNING_TASKS_FILE") or _state_dir() / "running-tasks.json")
    try:
        return [t for t in json.loads(path.read_text(encoding="utf-8")).get("tasks", []) if isinstance(t, dict)]
    except (OSError, ValueError):
        return []


def live_rows(thread_id: str) -> list[dict]:
    """Live dispatch rows bound to this thread (worker or owner-tab binding)."""
    if not thread_id:
        return []
    return [t for t in _ledger_rows()
            if t.get("status") in LIVE_STATUSES and thread_id in {str(t.get("thread_id") or ""), str(t.get("session_id") or "")}
            and not str(t.get("id") or "").startswith("wr-")]


def callback_summary(text: str) -> str:
    """"waiting on the owner: <final text verbatim>". Fenced drafts stay whole; a
    text with no Options/Lean segment gets the two ways forward CoS has, so
    the CoS Review card and CoS show the same choice."""
    body = (text or "").strip()
    prose_len = len(_prose(body))
    if prose_len > MAX_PROSE:
        # Keep the end, where the ask is; drafts are kept by report_text anyway.
        blocks = _FENCE.findall(body)
        prose = _prose(body).strip()
        body = "…" + prose[-MAX_PROSE:] + ("\n" + "\n".join(blocks) if blocks else "")
    summary = f"waiting on the owner: {body}"
    try:
        import cos_review_asks
        has_options = cos_review_asks.options_segment(summary) is not None
    except Exception:  # noqa: BLE001  (the shape check is best effort)
        has_options = bool(re.search(r"Options?:\s*\(a\)", summary))
    if not has_options:
        summary += ("\nOptions: (a) Give your answer or pick in CoS, and CoS steers the worker with it; "
                    "(b) Answer in the worker tab yourself. Lean: a because the steer reaches the live worker by cmux agent message.")
    return summary


def _seen(task_id: str, digest: str, record: bool) -> bool:
    path = _state_dir() / "worker-dispatch" / "turn-wait.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if (state.get(task_id) or {}).get("hash") == digest:
            return True
        if record:
            state[task_id] = {"hash": digest, "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
    return False


def _report(args: list[str], summary: str) -> tuple[subprocess.CompletedProcess, dict]:
    env = {k: v for k, v in os.environ.items() if k not in {"SWITCHBOARD_WORKER_ID"}}
    result = subprocess.run([*args, summary], capture_output=True, text=True, timeout=120, env=env)
    line = (result.stdout.strip().splitlines() or [""])[-1]
    try:
        reply = json.loads(line)
    except ValueError:
        reply = {"raw": line[:300], "stderr": result.stderr.strip()[-300:]}
    return result, reply if isinstance(reply, dict) else {"raw": line[:300]}


def park(thread_id: str, text: str, dry_run: bool = False) -> dict[str, object]:
    if os.environ.get("TURN_WAIT_DISABLED") == "1":
        return {"sent": False, "reason": "disabled"}
    verdict = classify(text)
    if not verdict["waiting"]:
        return {"sent": False, "reason": f"not waiting: {verdict['reason']}"}
    rows = live_rows(thread_id)
    if not rows:
        return {"sent": False, "reason": "no live dispatch row bound to this thread", "waiting_reason": verdict["reason"]}
    out: list[dict[str, object]] = []
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
    for row in rows:
        task_id = str(row["id"])
        if _seen(task_id, digest, record=False):
            out.append({"task_id": task_id, "sent": False, "reason": "already sent for this text"})
            continue
        args = ["bash", str(SCRIPTS / "report-to-cos.sh"), "--task-id", task_id, "--status", "active", "--needs-ruling",
                "--delivery", "direct", "--thread-id", thread_id]
        for flag, key in (("--attempt-id", "attempt_id"), ("--origin", "origin"), ("--workstream", "workstream"),
                          ("--workspace-ref", "workspace_ref")):
            if row.get(key):
                args += [flag, str(row[key])]
        summary = callback_summary(text)
        if dry_run:
            out.append({"task_id": task_id, "sent": False, "dry_run": True, "command": [*args, summary]})
            continue
        result, reply = _report(args, summary)
        offending = [str(o) for o in ((reply.get("review_gate") or {}).get("offending") or []) if str(o)]
        if reply.get("status") == "rejected" and offending:
            # The review gate fails closed on a pointer that is not a vault note.
            # That is right for a final report, but an interim ask must still
            # reach CoS: drop the bad pointers and say so, once.
            for bad in offending:
                summary = summary.replace(bad, " (link removed: not a vault note) ")
            result, reply = _report(args, summary)
            reply["links_removed"] = offending
        delivered = bool(reply.get("delivered"))
        if result.returncode == 0:
            _seen(task_id, digest, record=True)   # one callback per distinct text, delivered or deferred to the waiter
        out.append({"task_id": task_id, "sent": delivered, "exit": result.returncode, "report": reply})
    return {"sent": any(r.get("sent") for r in out), "waiting_reason": verdict["reason"], "rows": out}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file")
    src.add_argument("--text")
    c.add_argument("--thread-id", default="", help="also report whether a live dispatch row is bound (park owns its ask)")
    p = sub.add_parser("park")
    p.add_argument("--thread-id", required=True)
    p.add_argument("--text-file", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--consume", action="store_true", help="delete --text-file once read (the plugin's hand-off file)")
    args = parser.parse_args()
    if args.command == "classify":
        if args.text is not None:
            text = args.text
        else:
            text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file != "-" else sys.stdin.read()
        verdict = classify(text)
        if args.thread_id:
            verdict["bound"] = bool(live_rows(args.thread_id))
        print(json.dumps(verdict))
        return 0
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file != "-" else sys.stdin.read()
    if args.consume and args.text_file != "-":
        Path(args.text_file).unlink(missing_ok=True)
    result = park(args.thread_id, text, dry_run=args.dry_run)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(json.dumps({"at": stamp, "thread_id": args.thread_id, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
