#!/usr/bin/env python3
"""Stale next-up digest alarm.

The next-up digest is the only path for non-urgent completion callbacks. When
every working-hours attempt defers (CoS mid-turn for the window the poster
samples each tick), the only trace is `deferred_streak` in
system/state/next-up-post.json, so nobody is told. This check runs once per command-center tick,
right after check-state-inventory.py, and sends one direct CoS line per local
day when BOTH hold:

  * reports are pending for the digest (next-up-digest.py --pending-report-ids,
    the same predicate the poster acks against), and
  * more than one working day of working time has passed since the digest last
    posted (`last_posted_at`), measured on the next-up working-hours config in
    system/config/router.yaml (Mon-Fri, start..end, timezone). A weekend or an
    evening does not count; a digest that last posted Friday 15:00 is stale at
    Monday 15:00, not Saturday.

The line goes through report-to-cos.sh exactly as check-state-inventory.py's
alert does (--status blocked --urgent --create-if-missing, no hooks, worker env
stripped): a direct, priority, idle-only send with report-waiter.sh behind it,
never the digest it is reporting on. One line per day: the day is stamped in
system/state/next-up-stale-alarm.json once the command ran, and the per-day
task id (digest-stale-YYYYMMDD) is the ledger's own duplicate guard. No new
launchd job; command-center-loop.sh calls this in run_once.

Kill switch: DIGEST_STALE_CHECK_DISABLED=1 (checked by command-center-loop.sh).
Test overrides: DIGEST_STALE_REPO_ROOT, DIGEST_STALE_NOW, NEXT_UP_STATE_FILE,
NEXT_UP_CONFIG_FILE, DIGEST_STALE_ALARM_FILE, DIGEST_STALE_REPORT_SCRIPT,
DIGEST_STALE_PENDING_CMD (a JSON list of ids printed by that command).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402

REPO_ROOT = main_checkout(Path(os.environ.get("DIGEST_STALE_REPO_ROOT") or SCRIPTS.parents[1]))
STATE_FILE = Path(os.environ.get("NEXT_UP_STATE_FILE") or REPO_ROOT / "system" / "state" / "next-up-post.json")
CONFIG_FILE = Path(os.environ.get("NEXT_UP_CONFIG_FILE") or REPO_ROOT / "system" / "config" / "router.yaml")
ALARM_FILE = Path(os.environ.get("DIGEST_STALE_ALARM_FILE") or REPO_ROOT / "system" / "state" / "next-up-stale-alarm.json")
REPORT_SCRIPT = Path(os.environ.get("DIGEST_STALE_REPORT_SCRIPT") or SCRIPTS / "report-to-cos.sh")
PENDING_CMD = shlex.split(os.environ.get("DIGEST_STALE_PENDING_CMD") or "") or [
    sys.executable, str(SCRIPTS / "next-up-digest.py"), "--pending-report-ids"]

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

def now() -> datetime:
    override = os.environ.get("DIGEST_STALE_NOW")
    return datetime.fromisoformat(override).astimezone(timezone.utc) if override else datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def working_hours() -> dict[str, Any]:
    try:
        import yaml
        cfg = (yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}).get("next-up") or {}
    except Exception:
        cfg = {}
    start_h, start_m = map(int, str(cfg.get("working_hours_start", "08:00")).split(":"))
    end_h, end_m = map(int, str(cfg.get("working_hours_end", "18:00")).split(":"))
    return {"zone": ZoneInfo(str(cfg.get("timezone", "America/Los_Angeles"))),
            "start": start_h * 60 + start_m, "end": end_h * 60 + end_m}


def working_minutes_between(since: datetime, until: datetime, hours: dict[str, Any]) -> int:
    """Minutes of Mon-Fri start..end working time between two instants."""
    if until <= since:
        return 0
    zone = hours["zone"]
    since_local = since.astimezone(zone)
    until_local = until.astimezone(zone)
    total = 0
    day = since_local.date()
    while day <= until_local.date():
        if day.isoweekday() <= 5:
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(minutes=hours["start"])
            day_end = datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(minutes=hours["end"])
            lo = max(day_start, since_local)
            hi = min(day_end, until_local)
            if hi > lo:
                total += int((hi - lo).total_seconds() // 60)
        day += timedelta(days=1)
    return total


def pending_report_ids() -> list[str]:
    try:
        result = subprocess.run(PENDING_CMD, capture_output=True, text=True, timeout=120, check=False,
                                env={**os.environ, "REPO_ROOT": str(REPO_ROOT)})
        ids = json.loads((result.stdout.strip().splitlines() or ["[]"])[-1])
        return [str(i) for i in ids] if isinstance(ids, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _os_replace(tmp, path)


def alarm_line(state: dict[str, Any], pending: list[str], last_posted: datetime | None, working_days: float,
               zone: ZoneInfo) -> str:
    when = last_posted.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z") if last_posted else "never"
    sample = ", ".join(pending[:3]) + (f" +{len(pending) - 3} more" if len(pending) > 3 else "")
    head = (f"⏰ next-up digest stale: last posted {when} ({working_days:.1f} working days ago); "
            f"{len(pending)} report{'' if len(pending) == 1 else 's'} waiting ({sample}); "
            f"last attempt {state.get('last_status', '?')}/{state.get('deferred_reason', '-')}, "
            f"deferred_streak {state.get('deferred_streak', 0)}. ")
    if state.get("deferred_reason") == "no-target":
        # No CoS tab was open, so nothing is broken and the alarm must not read like a
        # delivery fault. Say so, and give the
        # one action that fixes it in the Options shape the CoS Review card reads.
        return (head + "Cause: no CoS tab was open, so each working-hours tick had nowhere to post. "
                "Options: (a) open CoS, and the next working-hours tick posts; "
                "(b) leave it, and the digest resumes whenever CoS opens. Lean: a because the waiting reports reach you then.")
    return (head + "Check system/state/worker-dispatch/next-up-waiter.log and run "
            "bash system/scripts/post-next-up-digest.sh by hand if CoS has been idle. "
            "Options: (a) check the waiter log and post by hand; (b) wait for the next working-hours tick. "
            "Lean: a because the digest has missed a working day.")


def send_alert(text: str, day: str) -> bool:
    cmd = ["bash", str(REPORT_SCRIPT), "--task-id", f"digest-stale-{day}", "--origin", "cos",
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
    hours = working_hours()
    current = now()
    state = load_json(STATE_FILE)
    alarm = load_json(ALARM_FILE)
    pending = pending_report_ids()
    last_posted = parse_iso(state.get("last_posted_at"))
    day_minutes = max(1, hours["end"] - hours["start"])
    # A digest that never posted is measured from the oldest signal we have.
    since = last_posted or parse_iso(state.get("updated_at")) or current
    stale_minutes = working_minutes_between(since, current, hours)
    working_days = stale_minutes / day_minutes
    stale = bool(pending) and stale_minutes > day_minutes
    local_day = current.astimezone(hours["zone"]).strftime("%Y%m%d")
    record: dict[str, Any] = {"checked_at": iso(current), "stale": stale, "pending_report_count": len(pending),
                              "working_minutes_since_post": stale_minutes, "last_posted_at": state.get("last_posted_at"),
                              "last_alarm_day": alarm.get("last_alarm_day"), "last_alarm_at": alarm.get("last_alarm_at"),
                              "last_alarm_delivered": alarm.get("last_alarm_delivered"), "sent": False}
    if stale and alarm.get("last_alarm_day") != local_day:
        text = alarm_line(state, pending, last_posted, working_days, hours["zone"])
        delivered = send_alert(text, local_day)
        record.update(sent=True, last_alarm_day=local_day, last_alarm_at=iso(current), last_alarm_delivered=delivered,
                      line=text)
    write_json(ALARM_FILE, {k: v for k, v in record.items() if k != "sent"})
    return record


def main() -> int:
    record = tick()
    print(json.dumps({k: record.get(k) for k in ("stale", "pending_report_count", "working_minutes_since_post",
                                                   "last_posted_at", "sent", "last_alarm_day", "last_alarm_delivered")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
