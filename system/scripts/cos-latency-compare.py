#!/usr/bin/env python3
"""Compare per-call model latency between Amp threads from Amp usage telemetry logs.

Reads ~/.agents/.amp-plugin-logs/<thread>.jsonl (events: agent_start, tool_call,
tool_result, turn_end) and reports, per thread, the "model gaps": the time the
model spends between receiving input and emitting its next action.

  model gap = agent_start -> first tool_call
            | tool_result -> next tool_call (same turn)
            | last tool_result -> turn_end
  tool gap  = tool_call -> tool_result (excluded from model latency)

Usage:
  python3 system/scripts/cos-latency-compare.py T-<ultra day> T-<high day> [--json]
  python3 system/scripts/cos-latency-compare.py --cos --date 2026-01-15 --date 2026-01-16

--cos resolves CoS thread ids from system/state/cos-rotation.json (history) and
the live CoS workspace binding for the given PT dates. Used by the CoS latency
trial chore in docs/runbooks/cos-latency-trial.md.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(os.environ.get("AMP_PLUGIN_LOG_DIR", os.path.expanduser("~/.agents/.amp-plugin-logs")))


def repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except Exception:
        return Path(__file__).resolve().parents[2]


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_events(thread: str) -> list[dict]:
    path = LOG_DIR / f"{thread}.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open() as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") in {"agent_start", "tool_call", "tool_result", "turn_end"} and ev.get("timestamp"):
                events.append(ev)
    events.sort(key=lambda e: e["timestamp"])
    return events


def analyse(thread: str, day: str | None = None) -> dict:
    events = load_events(thread)
    model_gaps: list[float] = []
    tool_gaps: list[float] = []
    turns = 0
    turn_durations: list[float] = []
    last_model_mark: datetime | None = None
    turn_start: datetime | None = None
    pending_calls: dict[str, datetime] = {}
    for ev in events:
        ts = parse_ts(ev["timestamp"])
        if day and ts.astimezone().strftime("%Y-%m-%d") != day and ev["type"] == "agent_start":
            last_model_mark = None
            turn_start = None
            continue
        if turn_start is None and ev["type"] != "agent_start":
            continue
        kind = ev["type"]
        if kind == "agent_start":
            turns += 1
            turn_start = ts
            last_model_mark = ts
            pending_calls.clear()
        elif kind == "tool_call":
            if last_model_mark is not None:
                model_gaps.append((ts - last_model_mark).total_seconds())
                last_model_mark = None
            if ev.get("callId"):
                pending_calls[ev["callId"]] = ts
        elif kind == "tool_result":
            start = pending_calls.pop(ev.get("callId", ""), None)
            if start is not None:
                tool_gaps.append((ts - start).total_seconds())
            last_model_mark = ts
        elif kind == "turn_end":
            if last_model_mark is not None:
                model_gaps.append((ts - last_model_mark).total_seconds())
            if turn_start is not None:
                turn_durations.append((ts - turn_start).total_seconds())
            last_model_mark = None
            turn_start = None

    def pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        idx = min(len(values) - 1, int(round((p / 100) * (len(values) - 1))))
        return round(values[idx], 1)

    return {
        "thread": thread,
        "day": day,
        "turns": turns,
        "model_calls": len(model_gaps),
        "model_gap_median_s": round(statistics.median(model_gaps), 1) if model_gaps else None,
        "model_gap_p90_s": pct(model_gaps, 90),
        "model_gap_total_s": round(sum(model_gaps), 1),
        "tool_gap_median_s": round(statistics.median(tool_gaps), 1) if tool_gaps else None,
        "turn_median_s": round(statistics.median(turn_durations), 1) if turn_durations else None,
        "log_found": bool(events),
    }


def cos_threads_for_dates(dates: list[str]) -> list[tuple[str, str]]:
    """Return (thread, date) pairs for CoS on each PT date from the rotation stamp."""
    root = repo_root()
    stamp = root / "system" / "state" / "cos-rotation.json"
    pairs: list[tuple[str, str]] = []
    records: list[dict] = []
    if stamp.exists():
        try:
            data = json.loads(stamp.read_text())
            records = list(data.get("history", [])) + [data]
        except Exception:
            records = []
    for date in dates:
        found = set()
        for rec in records:
            if rec.get("date") == date:
                for key in ("old_thread", "new_thread"):
                    if rec.get(key):
                        found.add(rec[key])
        if not found:
            # Fall back to the live CoS binding; the per-day filter keeps it honest.
            try:
                out = subprocess.run(
                    ["bash", "-c", f'source "{root}/system/cmux-helpers.sh"; ref=$(find_workspace_ref_for_key switchboard) && amp_thread_for_workspace "$ref"'],
                    capture_output=True, text=True, timeout=30,
                )
                tid = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
                if tid.startswith("T-"):
                    found.add(tid)
            except Exception:
                pass
        for tid in sorted(found):
            pairs.append((tid, date))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("threads", nargs="*", help="thread ids (T-...)")
    ap.add_argument("--cos", action="store_true", help="resolve CoS threads for --date values")
    ap.add_argument("--date", action="append", default=[], help="PT date YYYY-MM-DD (repeatable; filters turns)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    jobs: list[tuple[str, str | None]] = [(t, None) for t in args.threads]
    if args.cos:
        jobs += cos_threads_for_dates(args.date or [datetime.now().astimezone().strftime("%Y-%m-%d")])
    elif args.date and args.threads:
        jobs = [(t, d) for t in args.threads for d in args.date]
    if not jobs:
        ap.error("give thread ids, or --cos with --date")

    rows = [analyse(t, d) for t, d in jobs]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    header = f"{'thread':<42} {'day':<10} {'turns':>5} {'calls':>5} {'model med':>9} {'model p90':>9} {'tool med':>8} {'turn med':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        if not r["log_found"]:
            print(f"{r['thread']:<42} {r['day'] or '-':<10}  (no telemetry log)")
            continue
        print(
            f"{r['thread']:<42} {r['day'] or 'all':<10} {r['turns']:>5} {r['model_calls']:>5} "
            f"{str(r['model_gap_median_s']):>9} {str(r['model_gap_p90_s']):>9} "
            f"{str(r['tool_gap_median_s']):>8} {str(r['turn_median_s']):>8}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
