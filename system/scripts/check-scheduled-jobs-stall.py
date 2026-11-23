#!/usr/bin/env python3
"""Scheduled Jobs stall alarm.

⏰ Scheduled Jobs can stop taking automation when a comms-triage envelope sits typed but unsubmitted in its Claude
composer, because the inject guard defers every later send behind it. Otherwise the only
signal is the Comms Triage review repeating "⚠️ N inbound comms prompts queued
for 3h" each pass. This check runs once per command-center tick and sends CoS
ONE urgent line per stall episode when all of these hold:

  * inbound comms prompts are queued (.state/pending-comms-prompts.jsonl),
  * the oldest has waited more than SJ_STALL_MINUTES (default 30) of the
    fastpath's active hours (06:00-23:00 local; run-comms-fastpath.sh does not
    flush outside them, so overnight queueing is not a stall), and
  * no comms dispatch was accepted in that window (.state/comms-dispatch.json
    is missing or older than the threshold), or the oldest has waited twice
    the threshold even though jobs still go in (they never write a receipt).

The line names the tab state from the inject guard (cig_diagnose: busy, a
draft, the guard's own stranded paste, unreadable, or closed), the session's
context size, and the ways forward in the Options/Lean shape. It goes through
report-to-cos.sh like check-state-inventory.py's alert (--status blocked
--urgent --create-if-missing, hooks off, worker env stripped). One per episode:
system/state/scheduled-jobs-stall-alarm.json keeps `stalled_since` and
`alerted_at` until a tick finds the stall over, then records `cleared_at`.

Kill switch: SJ_STALL_CHECK_DISABLED=1 (checked by command-center-loop.sh).
Test overrides: SJ_STALL_REPO_ROOT, SJ_STALL_NOW, SJ_STALL_MINUTES,
SJ_STALL_ACTIVE_START, SJ_STALL_ACTIVE_END, SJ_STALL_TZ, COMMS_QUEUE_FILE,
COMMS_DISPATCH_MARKER, SJ_STALL_ALARM_FILE, SJ_STALL_REPORT_SCRIPT,
SJ_STALL_DIAGNOSE (literal "<ref> <state>" instead of reading cmux),
SJ_STALL_TOKENS (literal context tokens), SJ_STALL_DRY_RUN=1 (print, no send).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402

REPO_ROOT = main_checkout(Path(os.environ.get("SJ_STALL_REPO_ROOT") or SCRIPTS.parents[1]))
QUEUE_FILE = Path(os.environ.get("COMMS_QUEUE_FILE") or REPO_ROOT / ".state" / "pending-comms-prompts.jsonl")
MARKER_FILE = Path(os.environ.get("COMMS_DISPATCH_MARKER") or REPO_ROOT / ".state" / "comms-dispatch.json")
PROGRESS_FILE = Path(os.environ.get("COMMS_PROGRESS_FILE")
                     or REPO_ROOT / ".state" / "comms-dispatch-progress.json")
ALARM_FILE = Path(os.environ.get("SJ_STALL_ALARM_FILE")
                  or REPO_ROOT / "system" / "state" / "scheduled-jobs-stall-alarm.json")
REPORT_SCRIPT = Path(os.environ.get("SJ_STALL_REPORT_SCRIPT") or SCRIPTS / "report-to-cos.sh")
LIB = SCRIPTS / "lib"

STALL_MINUTES = float(os.environ.get("SJ_STALL_MINUTES", "30"))
ACTIVE_START = int(os.environ.get("SJ_STALL_ACTIVE_START", "6"))
ACTIVE_END = int(os.environ.get("SJ_STALL_ACTIVE_END", "23"))
TZ = ZoneInfo(os.environ.get("SJ_STALL_TZ", "America/Los_Angeles"))

# Tab state -> (what it means, options, lean).
STATES: dict[str, tuple[str, list[str], str]] = {
    "draft": (
        "its input box holds text that is not an automation paste, so every send defers behind it",
        ["open ⏰ Scheduled Jobs and submit or clear the text in its input box",
         "run /clear in ⏰ Scheduled Jobs",
         "leave it, and the queued prompts expire after 3 hours"],
        "a because the text in the box is the only thing blocking the queue"),
    "own-paste": (
        "its input box holds an automation prompt the guard typed but never submitted; "
        "the next comms flush submits it",
        ["wait one minute for the next flush to submit it",
         "open ⏰ Scheduled Jobs and press Enter",
         "run /clear in ⏰ Scheduled Jobs"],
        "a because the guard recovers its own paste on the next send"),
    "busy": (
        "a turn has been running the whole time, so every send waits",
        ["open ⏰ Scheduled Jobs and check the running turn",
         "press Esc in ⏰ Scheduled Jobs to stop a stuck turn",
         "wait for the turn to finish"],
        "a because a turn this long is usually stuck on a tool call"),
    "unreadable": (
        "its screen cannot be read, so the guard fails safe and defers",
        ["open ⏰ Scheduled Jobs and check for a dialog or prompt",
         "relaunch ⏰ Scheduled Jobs",
         "wait one more tick"],
        "a because a dialog is the usual cause"),
    "unknown": (
        "its input box state cannot be identified (a dialog, a menu, or a very tall draft)",
        ["open ⏰ Scheduled Jobs and check for a dialog or prompt",
         "run /clear in ⏰ Scheduled Jobs",
         "wait one more tick"],
        "a because a dialog is the usual cause"),
    "empty": (
        "it is idle with an empty input box, so dispatch is failing somewhere else",
        ["dispatch a switchboard-maintenance worker to read system/logs/comms-fastpath.log",
         "run bash daemon/comms-prompt.sh flush by hand",
         "wait one more tick"],
        "a because the fastpath log names the failing step"),
    "closed": (
        "the tab is not open",
        ["reopen ⏰ Scheduled Jobs with bash system/cmux-helpers.sh launch scheduled-jobs",
         "run start of day, which reopens routine tabs",
         "leave it, and the queued prompts expire after 3 hours"],
        "a because it is one command"),
}


def now() -> datetime:
    override = os.environ.get("SJ_STALL_NOW")
    return datetime.fromisoformat(override).astimezone(timezone.utc) if override else datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def pt_clock(stamp: datetime) -> str:
    local = stamp.astimezone(TZ)
    return f"{local.hour % 12 or 12}:{local:%M %p} PT"   # no %-I: Windows strftime has no %-


def queued_rows() -> list[datetime]:
    stamps: list[datetime] = []
    try:
        lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return stamps
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = parse_iso(row.get("created_at")) if isinstance(row, dict) else None
        if stamp:
            stamps.append(stamp)
    return stamps


def active_now(at: datetime) -> bool:
    return ACTIVE_START <= at.astimezone(TZ).hour < ACTIVE_END


def waited_since(oldest: datetime, at: datetime) -> datetime:
    """The oldest row's wait clamped to today's active-window start."""
    local = at.astimezone(TZ)
    window_start = local.replace(hour=ACTIVE_START, minute=0, second=0, microsecond=0)
    return max(oldest, window_start.astimezone(timezone.utc))


def _epoch(value: Any) -> datetime | None:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc) if seconds > 0 else None


def progress() -> dict[str, Any]:
    """comms-prompt.sh's durable stamps (last_accepted_at, last_receipt_at)."""
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_dispatch() -> datetime | None:
    """The newest accepted dispatch: the in-flight marker or the durable stamp.

    The marker is deleted when its receipt is consumed (an early alarm said
    "taken no automation for 43 min" 13 min after a job went in),
    so the durable stamp covers the time between a receipt and the next send.
    """
    stamps = [_epoch(progress().get("last_accepted_at"))]
    try:
        data = json.loads(MARKER_FILE.read_text(encoding="utf-8"))
        stamps.append(_epoch(data.get("dispatched_at")))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    found = [stamp for stamp in stamps if stamp]
    return max(found) if found else None


def last_receipt() -> datetime | None:
    return _epoch(progress().get("last_receipt_at"))


def stall(at: datetime) -> dict[str, Any] | None:
    """The stall facts, or None when Scheduled Jobs is keeping up."""
    rows = queued_rows()
    if not rows or not active_now(at):
        return None
    oldest = min(rows)
    since = waited_since(oldest, at)
    threshold = timedelta(minutes=STALL_MINUTES)
    if at - since < threshold:
        return None
    # A receipt inside the threshold means the queue is draining: a backlog
    # (more inbound than one batch at a time clears, and the newest rows go
    # first), not a stall.
    receipt = last_receipt()
    if receipt and at - receipt < threshold:
        return None
    dispatched = last_dispatch()
    # A fresh dispatch marker means jobs still go in. The queue can still stall
    # behind jobs that go in and never write a receipt (the flush re-dispatches
    # the same job every 30 min), so twice the threshold counts either way.
    if dispatched and at - dispatched < threshold and at - since < 2 * threshold:
        return None
    return {"count": len(rows), "oldest": oldest, "since": since, "last_dispatch": dispatched,
            "dispatching": bool(dispatched and at - dispatched < threshold)}


def diagnose() -> tuple[str, str]:
    """(workspace ref or "", state word) from the inject guard."""
    override = os.environ.get("SJ_STALL_DIAGNOSE")
    if override is not None:
        parts = override.split()
        return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0] if parts else "unknown")
    script = ('source "$1/cmux-inject-guard.sh"; log() { :; }; source "$1/automation-dispatch.sh"; '
              'ref="$(ad_resolve_exec_ws)" || { echo closed; exit 0; }; '
              '[[ -n "$ref" ]] || { echo closed; exit 0; }; echo "$ref $(cig_diagnose "$ref")"')
    try:
        out = subprocess.run(["bash", "-c", script, "_", str(LIB)], capture_output=True, text=True, timeout=30)
        parts = (out.stdout.strip().splitlines() or ["unknown"])[-1].split()
    except (OSError, subprocess.SubprocessError):
        return "", "unknown"
    if len(parts) == 2:
        return parts[0], parts[1] if parts[1] in STATES else "unknown"
    return "", parts[0] if parts and parts[0] in STATES else "unknown"


def context_tokens(ref: str) -> int | None:
    override = os.environ.get("SJ_STALL_TOKENS")
    if override is not None:
        return int(override) if override.isdigit() else None
    if not ref:
        return None
    try:
        import claude_session_context  # noqa: PLC0415
        result = claude_session_context.context_for_ref(ref)
    except Exception:  # noqa: BLE001
        return None
    return int(result["tokens"]) if result else None


def alert_line(facts: dict[str, Any], state: str, tokens: int | None, at: datetime) -> str:
    meaning, options, lean = STATES.get(state, STATES["unknown"])
    minutes = round((at - facts["since"]).total_seconds() / 60)
    age = f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
    plural = "" if facts["count"] == 1 else "s"
    if facts.get("dispatching"):
        head = (f"⏰ Scheduled Jobs has drained no comms prompts for {age} (the last job went in at "
                f"{pt_clock(facts['last_dispatch'])} and wrote no receipt)")
    else:
        head = f"⏰ Scheduled Jobs has taken no automation for {age}"
    parts = [f"{head}: {facts['count']} inbound comms prompt{plural} queued, "
             f"the oldest since {pt_clock(facts['oldest'])}."]
    parts.append(f"The tab is {state}: {meaning}.")
    if tokens:
        parts.append(f"Its session holds {tokens // 1000}K tokens of context.")
    parts.append("Options: " + "; ".join(f"({chr(97 + i)}) {text}" for i, text in enumerate(options))
                 + f". Lean: {lean}.")
    return " ".join(parts)


def load_alarm() -> dict[str, Any]:
    try:
        data = json.loads(ALARM_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_alarm(record: dict[str, Any]) -> None:
    try:
        ALARM_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALARM_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _os_replace(tmp, ALARM_FILE)
    except OSError:
        pass


def send_alert(text: str, at: datetime) -> bool:
    if os.environ.get("SJ_STALL_DRY_RUN") == "1":
        print(f"DRY RUN alert: {text}")
        return True
    stamp = at.astimezone(TZ).strftime("%Y%m%d-%H%M")
    cmd = ["bash", str(REPORT_SCRIPT), "--task-id", f"scheduled-jobs-stall-{stamp}", "--origin", "cos",
           "--workstream", "switchboard-maintenance", "--status", "blocked", "--urgent", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        last = (result.stdout.strip().splitlines() or [""])[-1]
        return bool(json.loads(last).get("delivered"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def tick() -> dict[str, Any]:
    at = now()
    facts = stall(at)
    alarm = load_alarm()
    open_episode = bool(alarm.get("alerted_at")) and not alarm.get("cleared_at")
    if facts is None:
        if open_episode:
            alarm["cleared_at"] = iso(at)
            write_alarm(alarm)
            return {"stalled": False, "cleared": True}
        return {"stalled": False}
    if open_episode:
        return {"stalled": True, "alerted": False, "reason": "already alerted this episode",
                "alerted_at": alarm.get("alerted_at")}
    ref, state = diagnose()
    tokens = context_tokens(ref)
    text = alert_line(facts, state, tokens, at)
    delivered = send_alert(text, at)
    write_alarm({"stalled_since": iso(facts["since"]), "alerted_at": iso(at), "state": state,
                 "queued": facts["count"], "tokens": tokens, "delivered": delivered, "text": text})
    return {"stalled": True, "alerted": True, "delivered": delivered, "state": state, "text": text}


def main() -> int:
    result = tick()
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
