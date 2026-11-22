"""Adaptive lookback windows based on last successful collector run.

Each collector that polls a remote source (Slack, Gmail, Linear, etc.)
reads `compute_lookback_iso(name)` to size its `--newer-than` / `since`
window to exactly the elapsed time since its previous complete run, plus a
small overlap buffer. After every attempt it calls `record_run(name,
complete=...)`; partial runs remain observable but do not advance lookback.

If a collector has never run, or hasn't run in over 7 days, the lookback
defaults to PT72H so a Monday morning catches everything since Friday.

State file: system/state/collector-runs.json
{
  "slack-mentions":  {"last_run_at": "2026-01-15T20:14:00Z"},
  "slack-channels":  {"last_run_at": "2026-01-15T20:10:00Z"}
}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from source_jsonl_lock import source_jsonl_locked

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / "system" / "state" / "collector-runs.json"
DEFAULT_LOOKBACK = "PT72H"          # 3 days — covers Fri→Mon
MAX_LOOKBACK_SECONDS = 72 * 3600    # cap any computed window at 72h
MIN_LOOKBACK_SECONDS = 60           # floor: always look back at least 60s
DEFAULT_BUFFER_SECONDS = 120        # overlap to cover indexing lag / clock skew
STALE_THRESHOLD_SECONDS = 7 * 86400 # 7d → fall back to default


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def _seconds_to_iso(total: int) -> str:
    if total <= 0:
        return f"PT{MIN_LOOKBACK_SECONDS}S"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = ["PT"]
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if seconds or (not hours and not minutes):
        parts.append(f"{seconds}S")
    return "".join(parts)


def compute_lookback_iso(
    name: str,
    *,
    default: str = DEFAULT_LOOKBACK,
    buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
    max_seconds: int = MAX_LOOKBACK_SECONDS,
    fallback_on_stale: bool = True,
) -> str:
    """Return an ISO-8601 duration sized to (now - last_run + buffer).

    Falls back to `default` if the collector has never run or last ran
    more than 7 days ago. Capped at 72 hours.
    """
    state = _load_state()
    entry = state.get(name) or {}
    last_run_iso = entry.get("last_run_at")
    if not last_run_iso:
        return default
    try:
        last = datetime.strptime(last_run_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return default
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    if elapsed > STALE_THRESHOLD_SECONDS and fallback_on_stale:
        return default
    bounded = max(MIN_LOOKBACK_SECONDS, min(int(elapsed) + buffer_seconds, max_seconds))
    return _seconds_to_iso(bounded)


@source_jsonl_locked(lambda *args, **kwargs: STATE_FILE)
def record_run(name: str, *, complete: bool = True, when: Optional[datetime] = None) -> None:
    """Persist run freshness; only complete runs advance adaptive lookback."""
    when = when or datetime.now(timezone.utc)
    state = _load_state()
    entry = state.get(name) or {}
    timestamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["last_attempt_at"] = timestamp
    entry["complete"] = complete
    if complete:
        entry["last_run_at"] = timestamp
    state[name] = entry
    _save_state(state)


def peek_last_run(name: str) -> Optional[str]:
    """Return the stored last_run_at ISO string, or None."""
    return (_load_state().get(name) or {}).get("last_run_at")


@source_jsonl_locked(lambda *args, **kwargs: STATE_FILE)
def forget_run(name: str) -> bool:
    """Drop a retired collector's row; True when one existed.

    The row of a collector that no longer runs otherwise sits in the state
    file forever and reads as a dead collector in every health pass.
    """
    state = _load_state()
    if name not in state:
        return False
    del state[name]
    _save_state(state)
    return True


if __name__ == "__main__":
    # Quick CLI for inspection: `python3 _lookback.py [name]`
    # Retire a collector: `python3 _lookback.py --forget <name>`
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--forget":
        n = sys.argv[2]
        print(f"{n}: {'forgotten' if forget_run(n) else 'no row'}")
    elif len(sys.argv) > 1:
        n = sys.argv[1]
        print(f"{n}: last_run={peek_last_run(n)} lookback={compute_lookback_iso(n)}")
    else:
        for k, v in sorted(_load_state().items()):
            print(f"{k}: last_run={v.get('last_run_at')} lookback={compute_lookback_iso(k)}")
