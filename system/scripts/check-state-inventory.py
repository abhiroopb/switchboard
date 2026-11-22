#!/usr/bin/env python3
"""Shrink check for the unversioned PM OS state.

A bad change that replaces `people/` with a symlink to itself, or wipes state
some other way, can go unnoticed for a day. This script runs once per
command-center tick, before `dispatch.py sweep`, and compares a few counts with
the previous tick:

  people/ .state/ .firecrawl/ system/archive/     regular-file counts
  system/state/running-tasks.json                 task count (+ bytes)
  system/state/triage-ledger.json                 item count, next_draft_number (+ bytes)

A path trips when it is missing or a symlink, when a directory lost more than
MAX_FILE_DROP files or more than MAX_PCT_DROP percent of its files (either rule; the
percent rule also needs at least MIN_PCT_DROP_FILES lost files, so one file in a
tiny directory cannot trip it),
when a task or item count goes down, or when next_draft_number goes backwards.
The .state count leaves out what scheduled jobs delete on purpose: every path
declared in system/config/state-pruned.txt (rotate-logs.sh snapshots and rotated
*.log.1 / *.log.1.gz copies, prune-comms-bodies.sh comms-bodies/,
prune-local-retention.py *-demo/). Each pruner reads its own row from that file,
so the list is named in one place and a scheduled prune never reads as a wipe.
Each dir count records the exclusion list it used; a reference counted with a
different list is reseeded, not compared.
The running-tasks.json task count allows for the ledger's own retention:
running-task-state.py keeps every active or undelivered row plus the newest
RETAINED_DELIVERED_ROWS delivered rows, so once the ledger has reached that cap
its count is at least the cap for good and only a drop below min(previous, cap)
is a loss, so a retention trim never reads as a wipe. Below the cap any drop still trips. Known blind spot: active or
undelivered rows lost while the count stays at or above the cap are not seen.
One urgent line goes to CoS through report-to-cos.sh naming the path and the
before/after values; the digest shows the same state through
command-center-digest.py. Recovery is the machine backup for the gitignored state and
git for tracked files. While a drop persists the
comparison stays pinned to the pre-drop values and the line repeats at most once
an hour. Nothing is held or blocked: sweeps, finish and workers run as before.

Files: system/state/inventory-latest.json (previous tick = the reference), mirrored
to ~/Library/Application Support/pm-os/ so a repo-wide wipe still leaves a
comparison point.

Accepting a legitimate drop (profiles deliberately deleted): remove
system/state/inventory-latest.json; the next tick measures afresh. There is no
ack command, hold file or history by design, to keep the setup simple.
Kill switch: INVENTORY_CHECK_DISABLED=1 (checked by command-center-loop.sh).
Test overrides: INVENTORY_REPO_ROOT, INVENTORY_LATEST_FILE, INVENTORY_MIRROR_DIR,
INVENTORY_REPORT_SCRIPT, INVENTORY_NOW.
"""
from __future__ import annotations

import fnmatch
import functools
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402
from lib import winlink  # noqa: E402  (a Windows junction is the 8b3230df self-loop's twin: Path.is_symlink() misses it)

_spec = importlib.util.spec_from_file_location("running_task_state", SCRIPTS / "running-task-state.py")
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)  # type: ignore[union-attr]

REPO_ROOT = main_checkout(Path(os.environ.get("INVENTORY_REPO_ROOT") or SCRIPTS.parents[1]))
LATEST = Path(os.environ.get("INVENTORY_LATEST_FILE") or REPO_ROOT / "system" / "state" / "inventory-latest.json")
MIRROR_DIR = Path(os.environ.get("INVENTORY_MIRROR_DIR") or Path.home() / "Library" / "Application Support" / "pm-os")
REPORT_SCRIPT = Path(os.environ.get("INVENTORY_REPORT_SCRIPT") or SCRIPTS / "report-to-cos.sh")
STATE_PRUNED_FILE = SCRIPTS.parent / "config" / "state-pruned.txt"

DIRS = ("people", ".state", ".firecrawl", "system/archive")
LEDGERS = {"system/state/running-tasks.json": "tasks", "system/state/triage-ledger.json": "items"}
# Rows the ledger's own writer keeps at minimum once it has reached that many
# (running-task-state.write_payload); a count drop that stays at or above this
# floor is retention, not loss.
LEDGER_FLOORS = {"system/state/running-tasks.json": ledger.RETAINED_DELIVERED_ROWS}
MAX_FILE_DROP = 5        # a drop of MORE than this many files trips
MAX_PCT_DROP = 5.0       # or MORE than this percent of the reference count
# The percent rule needs this many lost files. On a fresh install .state holds about
# 5 files, so any one of them leaving would read as "lost 1 files (20.0%)".
MIN_PCT_DROP_FILES = 2
NAMES_CAP = 50           # .state records its file names up to this many, so an alarm can name what left
REPEAT_AFTER = timedelta(hours=1)

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
    override = os.environ.get("INVENTORY_NOW")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


# --- measure -----------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def state_pruned() -> tuple[str, ...]:
    """Patterns, relative to .state, that scheduled jobs delete on purpose.

    Read from system/config/state-pruned.txt (`<owner> <pattern>` rows, `#` comments),
    the one place every pruner declares its path. A trailing slash marks a directory
    (glob allowed); anything else is a filename glob. Order kept, duplicates dropped.
    If the file cannot be read, nothing is excluded: the check then errs toward alerting.
    """
    try:
        lines = STATE_PRUNED_FILE.read_text().splitlines()
    except OSError as exc:
        print(f"inventory: could not read pruned paths from {STATE_PRUNED_FILE}: {exc}", file=sys.stderr)
        return ()
    patterns: list[str] = []
    for line in lines:
        cols = line.split()
        if len(cols) < 2 or cols[0].startswith("#"):
            continue
        if cols[1] not in patterns:
            patterns.append(cols[1])
    return tuple(patterns)


# Files under .state that come and go on every tick by design: lock files the
# background-task runner creates and removes, one-shot warning timestamps, the cmux
# skip record, temp files of atomic writes. A fresh install has only a handful of
# .state files, so one of these vanishing would read as "lost 1 files (20.0%)".
# Not pruners, so not in
# state-pruned.txt, whose rows are each owned by a script that deletes them.
# A lock can be a directory too: run-background-tasks.sh holds background-tasks.lock/pid
# and background-refresh-pending for about 14 s every 30 minutes, and a tick counting
# inside that window would read one file too many, then "lose" it on the next tick.
TRANSIENT_STATE = ("background-task-locks/", "*.lock/", "*.lock", "*.timestamp", "cmux-skip-record",
                   "background-refresh-pending", "*.tmp", "*.pid")


def excluded_for(rel: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(state_pruned() + TRANSIENT_STATE)) if rel == ".state" else ()


def measure_dir(path: Path, excluded: tuple[str, ...] = ()) -> dict[str, Any]:
    """Count regular files under `path`, skipping symlinks and any relative path
    matching `excluded` (dir globs end with "/", file patterns are globs)."""
    if winlink.is_link(path):
        return {"kind": "dir", "symlink": True, "files": 0}
    if not path.is_dir():
        return {"kind": "dir", "missing": True, "files": 0}
    skip_dirs = [p.rstrip("/") for p in excluded if p.endswith("/")]
    skip_files = [p for p in excluded if not p.endswith("/")]
    files = 0
    kept: list[str] = []
    for root, dirs, names in os.walk(path):
        rel_root = os.path.relpath(root, path)
        prefix = "" if rel_root == "." else rel_root + "/"
        dirs[:] = [d for d in dirs
                   if not winlink.is_link(os.path.join(root, d))
                   and not any(fnmatch.fnmatch(prefix + d, pat) for pat in skip_dirs)]
        counted = [prefix + n for n in names
                   if not winlink.is_link(os.path.join(root, n))
                   and not any(fnmatch.fnmatch(prefix + n, pat) for pat in skip_files)]
        files += len(counted)
        kept += counted
    row: dict[str, Any] = {"kind": "dir", "files": files}
    if excluded:
        row["excluded"] = list(excluded)   # counts with a different exclusion list are not comparable
        # .state only: an alarm's text can reach a tracked close-out line, and people/
        # file names must never leave the gitignored tree.
        if files <= NAMES_CAP:
            row["names"] = sorted(kept)
    return row


def measure_ledger(path: Path, key: str, floor: int | None = None) -> dict[str, Any]:
    """measure_ledger_once, read again (up to 3 times) when the file reads as missing
    or not JSON: on Windows a read can land inside another process's atomic rewrite."""
    row = measure_ledger_once(path, key, floor)
    for _ in range(2):
        if not (row.get("missing") or row.get("unreadable")):
            break
        __import__("time").sleep(0.3)
        row = measure_ledger_once(path, key, floor)
    return row


def measure_ledger_once(path: Path, key: str, floor: int | None = None) -> dict[str, Any]:
    if winlink.is_link(path):
        return {"kind": "json", "symlink": True}
    if not path.is_file():
        return {"kind": "json", "missing": True}
    row: dict[str, Any] = {"kind": "json", "bytes": path.stat().st_size}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get(key) if isinstance(data, dict) else None
        row[key] = len(rows) if isinstance(rows, (list, dict)) else 0
        if key == "items":
            row["next_draft_number"] = int(data.get("next_draft_number") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        row["unreadable"] = True
    if floor is not None:
        row["retained"] = floor   # the writer's retention floor the count was judged against
    return row


def measure() -> dict[str, Any]:
    paths: dict[str, Any] = {rel: measure_dir(REPO_ROOT / rel, excluded_for(rel)) for rel in DIRS}
    for rel, key in LEDGERS.items():
        paths[rel] = measure_ledger(REPO_ROOT / rel, key, LEDGER_FLOORS.get(rel))
    return {"measured_at": iso(now()), "repo_root": str(REPO_ROOT), "paths": paths}


# --- compare -----------------------------------------------------------------------

def trips(reference: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Every (path, what, before, after) that violates a rule against `reference`."""
    found: list[dict[str, Any]] = []
    for rel, cur in current.items():
        ref = reference.get(rel) or {}
        if cur.get("symlink"):
            found.append({"path": rel, "what": "is a symlink", "before": "real path", "after": "symlink"})
            continue
        if cur.get("missing"):
            # Nothing was there to lose: missing before, an empty folder (a fresh install),
            # or a ledger that never had a readable count (an install with no
            # triage-ledger.json would otherwise read "? -> missing").
            if (not ref or ref.get("missing") or (ref.get("kind") == "dir" and not ref.get("files"))
                    or (ref.get("kind") == "json" and not any(k in ref for k in ("tasks", "items")))):
                continue
            found.append({"path": rel, "what": "is missing", "before": _describe(ref), "after": "missing"})
            continue
        if cur.get("unreadable"):
            found.append({"path": rel, "what": "is not valid JSON", "before": _describe(ref), "after": "unreadable"})
            continue
        if cur["kind"] == "dir" and "files" in ref and cur.get("excluded", []) == ref.get("excluded", []):
            before, after = int(ref["files"]), int(cur["files"])
            drop = before - after
            pct = (drop / before * 100.0) if before else 0.0
            if drop > MAX_FILE_DROP or (pct > MAX_PCT_DROP and drop >= MIN_PCT_DROP_FILES):
                gone = sorted(set(ref.get("names") or []) - set(cur.get("names") or []))
                named = f": {', '.join(gone[:5])}" if gone else ""
                found.append({"path": rel, "what": f"lost {drop} files ({pct:.1f}%){named}", "before": before, "after": after})
        for key in ("tasks", "items", "next_draft_number"):
            if key not in cur or key not in ref:
                continue
            before, after = int(ref[key]), int(cur[key])
            # A count the writer itself trims never goes below its retention floor
            # once it has reached it, so only a drop under min(before, floor) is a loss.
            floor = cur.get("retained") if key == "tasks" else None
            if after < (min(before, int(floor)) if floor is not None else before):
                noun = "went backwards" if key == "next_draft_number" else "count went down"
                found.append({"path": rel, "what": f"{key} {noun}", "before": before, "after": after})
    return found


def _describe(ref: dict[str, Any]) -> Any:
    for key in ("files", "tasks", "items"):
        if key in ref:
            return ref[key]
    return "?"


# --- alert -------------------------------------------------------------------------

def alert_line(tripped: list[dict[str, Any]], since: str) -> str:
    parts = [f"{t['path']} {t['what']} ({t['before']} → {t['after']})" for t in tripped]
    return (f"state inventory shrank since {since}: " + "; ".join(parts)
            + ". Nothing is held; restore from the machine backup (gitignored state) or git (tracked files). "
            f"To accept the new counts: rm {LATEST}")


def send_alert(text: str) -> bool:
    stamp = now().strftime("%Y%m%d-%H%M")
    cmd = ["bash", str(REPORT_SCRIPT), "--task-id", f"inventory-shrink-{stamp}", "--origin", "cos",
           "--workstream", "pm-os-maintenance", "--status", "blocked", "--urgent", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        last = (result.stdout.strip().splitlines() or [""])[-1]
        return bool(json.loads(last).get("delivered"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


# --- tick --------------------------------------------------------------------------

def load_latest() -> dict[str, Any] | None:
    try:
        data = json.loads(LATEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("paths"), dict) else None
    except (OSError, ValueError):
        return None


def write_latest(record: dict[str, Any]) -> None:
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    for target in (LATEST, MIRROR_DIR / LATEST.name):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            _os_replace(tmp, target)
        except OSError as exc:
            print(f"inventory: could not write {target}: {exc}", file=sys.stderr)


def tick() -> dict[str, Any]:
    current = measure()
    previous = load_latest()
    prior_alert = (previous or {}).get("alert") or None
    # The reference is the previous tick. While a drop persists it stays pinned at
    # the pre-drop values, so the line keeps repeating until the files are back or
    # the latest file is removed on purpose. (A loss under 5 files and 5 % per tick
    # is accepted by design: the check is for wipes, not bookkeeping.)
    reference = (prior_alert or {}).get("reference") if prior_alert else (previous or {}).get("paths")
    tripped = trips(reference, current["paths"]) if reference else []
    record: dict[str, Any] = {**current, "ok": not tripped}
    if tripped:
        since = prior_alert["since"] if prior_alert else current["measured_at"]
        last_sent = parse_iso((prior_alert or {}).get("last_sent"))
        text = alert_line(tripped, since)
        sent = False
        if last_sent is None or now() - last_sent >= REPEAT_AFTER:
            sent = send_alert(text)
            last_sent = now()
        record["alert"] = {"since": since, "reference": reference, "tripped": tripped, "line": text,
                           "last_sent": iso(last_sent) if last_sent else None, "delivered": sent}
    write_latest(record)
    return record


def main() -> int:
    record = tick()
    out = {"ok": record["ok"], "measured_at": record["measured_at"],
           "counts": {rel: _describe(v) for rel, v in record["paths"].items()}}
    if record.get("alert"):
        out["alert"] = {k: record["alert"][k] for k in ("since", "tripped", "last_sent", "delivered")}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
