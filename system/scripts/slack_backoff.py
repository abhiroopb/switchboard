#!/usr/bin/env python3
"""One shared gate for every `pmos slack` caller on this machine.

Why one gate: every Slack caller on this machine shares the same token and so the same
per-method rate limits. When a Slack connector or proxy sits in between, a 429 can also
mean that layer's own circuit is open (`rate_limit_origin: local`). Pacing a single
script cannot clear that; what this host can do is stop piling retries onto an open
limit and make the condition visible.

State (both under .state/, next to each other):

* ``slack-mentions-backoff.json`` — the existing shared cooldown
  ``{"reason", "consecutive_failures", "next_allowed_at", "updated_at"}``
  written by collect-slack-mentions.py, collect-slack-channels.py,
  collect-slack-outbound.py and check-thread-resolution.py. ``remaining()``
  reads it (plus reply_check.py's ``reply-check-backoff.json`` Slack row) so a
  caller skips while any Slack reader is cooling down.
* ``slack-rate-limit-events.jsonl`` — append-only journal of every rate-limit
  event with caller, api_method and origin. The streak the digest surfaces is
  derived from it: consecutive events where each one arrived within
  ``STREAK_REHIT_GRACE_SEC`` of the previous cooldown expiring (we re-hit the
  limit as soon as we were allowed back). An isolated 429 is a streak of one.

* ``slack-token-buckets.json`` — per-method token buckets (``BUCKETS``) that
  pace this host's own calls under Slack's tiers before Slack has to. Every
  reader calls ``acquire(call_cost(argv))`` before spawning ``pmos``; a denied
  acquire is a skip (or a short sleep), never a 429. A 429 for a method
  freezes only that method's bucket for Slack's ``Retry-After``
  (``freeze_from_blob``), so a search throttle no longer pauses channel and
  thread reads. ``assistant.search.context`` is the one method Slack limits
  per *user*; its 429s are
  this machine's own doing and never open the shared circuit, so they freeze the
  search bucket without writing the global cooldown.

Callers that have no gate of their own use ``remaining()`` before spawning
``pmos`` and ``record_rate_limit()`` after a ``rate_limited`` response; the four
collectors above keep their own cooldown logic and only ``journal_event()``.
test_slack_backoff.py fails when a script invokes ``pmos slack``
without referencing this gate, unless it is on the explicit one-shot list.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKOFF_FILE = Path(
    os.environ.get("SLACK_MENTIONS_BACKOFF_FILE", str(REPO_ROOT / ".state" / "slack-mentions-backoff.json"))
)
REPLY_CHECK_BACKOFF_FILE = REPO_ROOT / ".state" / "reply-check-backoff.json"
EVENTS_SUFFIX = "slack-rate-limit-events.jsonl"

RATE_LIMIT_BACKOFF_BASE_SEC = 5 * 60
BACKOFF_MAX_SEC = 30 * 60
# A new rate limit this soon after the previous cooldown expired means the
# quota never recovered; count it toward the streak.
STREAK_REHIT_GRACE_SEC = 180
STREAK_ALERT_THRESHOLD = int(os.environ.get("SLACK_RATE_LIMIT_STREAK_ALERT", "3"))
# A streak older than this is history, not an alert.
STREAK_ALERT_MAX_AGE_SEC = 2 * 60 * 60
EVENTS_KEEP = 2000
PT = ZoneInfo("America/Los_Angeles")

_RATE_LIMIT_HINTS = ("status: 429", "ratelimited", "rate_limit", "rate limit")


def looks_rate_limited(blob: str) -> bool:
    low = (blob or "").lower()
    return any(hint in low for hint in _RATE_LIMIT_HINTS)


def retry_after_seconds(blob: str) -> int | None:
    for pattern in (
        r"[\"']retry_after_seconds[\"']\s*:\s*(\d+)",
        r"retry[-_ ]after[\"']?\s*[:=]\s*[\"']?(\d+)",
        r"[\"']retry_after[\"']\s*:\s*(\d+)",
    ):
        match = re.search(pattern, blob or "", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _json_field(blob: str, name: str) -> str | None:
    match = re.search(rf'"{name}"\s*:\s*"([^"]*)"', blob or "")
    return match.group(1) if match else None


def _load(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(path)


def events_file(backoff_file: Path | None = None) -> Path:
    return (backoff_file or BACKOFF_FILE).with_name(EVENTS_SUFFIX)


def remaining(
    now_epoch: float | None = None,
    *,
    backoff_file: Path | None = None,
    costs: dict[str, int] | None = None,
) -> int:
    """Seconds until every known Slack cooldown on this host has expired (0 = clear).

    ``costs`` (from ``call_cost``) narrows the per-method freezes considered to
    the methods a caller is about to spend; without it every frozen method
    counts, which is the safe reading for callers that do not say.
    """
    now_value = now_epoch if now_epoch is not None else time.time()
    candidates: list[float] = [now_value + frozen_remaining(costs, now_epoch=now_value, backoff_file=backoff_file)]
    try:
        candidates.append(float(_load(backoff_file or BACKOFF_FILE).get("next_allowed_at") or 0))
    except (TypeError, ValueError):
        pass
    if backoff_file is None:
        slack_row = _load(REPLY_CHECK_BACKOFF_FILE).get("slack")
        if isinstance(slack_row, dict):
            try:
                candidates.append(float(slack_row.get("next_allowed_at") or 0))
            except (TypeError, ValueError):
                pass
    if not candidates:
        return 0
    return max(0, int(max(candidates) - now_value + 0.999))


def journal_event(
    state: dict[str, Any],
    *,
    caller: str,
    blob: str = "",
    backoff_file: Path | None = None,
) -> dict[str, Any]:
    """Append one rate-limit event; ``state`` is the cooldown record just written."""
    event = {
        "at": state.get("updated_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "caller": caller,
        "reason": state.get("reason") or "rate_limited",
        "next_allowed_at": int(state.get("next_allowed_at") or 0),
        "api_method": _json_field(blob, "api_method"),
        "origin": _json_field(blob, "rate_limit_origin"),
        "retry_after": retry_after_seconds(blob),
    }
    path = events_file(backoff_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        _trim(path)
    except OSError:
        pass
    return event


def _trim(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    if len(lines) > EVENTS_KEEP * 2:
        path.write_text("\n".join(lines[-EVENTS_KEEP:]) + "\n")


def record_rate_limit(
    blob: str = "",
    *,
    caller: str,
    now_epoch: float | None = None,
    backoff_file: Path | None = None,
) -> int:
    """Persist a cooldown in the collectors' format and journal the event.

    Same escalation as the collectors: 5 min base, doubling per consecutive
    failure, never below Slack's retry_after, capped at 30 min plus jitter.
    """
    path = backoff_file or BACKOFF_FILE
    now_value = now_epoch if now_epoch is not None else time.time()
    previous = _load(path)
    try:
        failures = int(previous.get("consecutive_failures") or 0) + 1
    except (TypeError, ValueError):
        failures = 1
    delay = min(BACKOFF_MAX_SEC, RATE_LIMIT_BACKOFF_BASE_SEC * (2 ** min(failures - 1, 5)))
    retry_after = retry_after_seconds(blob)
    if retry_after is not None:
        delay = max(delay, retry_after)
    delay = min(BACKOFF_MAX_SEC, delay)
    delay += random.randint(0, min(30, max(1, delay // 10)))
    state = {
        "reason": "rate_limited",
        "consecutive_failures": failures,
        "next_allowed_at": int(now_value + delay),
        "updated_at": datetime.fromtimestamp(now_value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        _write_atomic(path, state)
    except OSError:
        pass
    journal_event(state, caller=caller, blob=blob, backoff_file=path)
    return delay


# --- per-method token buckets -------------------------------------------------

BUCKETS_SUFFIX = "slack-token-buckets.json"

# method -> (capacity, seconds per refilled token). Sized under Slack's published tiers
# for this host alone, so other clients on the same app keep their share.
#
# assistant.search.context is Slack's per-*user* search quota (about 300 calls a day),
# so one token per 360 s (240/day) keeps this host under it even with outbound search
# sharing it. The conversations.* buckets allow 15/min sustained (one token per 4 s,
# 12 burst), well under Tier 3; with per-channel watermarks the steady need is ~5/min.
BUCKETS: dict[str, tuple[int, float]] = {
    "assistant.search.context": (6, 360.0),
    "conversations.history": (12, 4.0),
    "conversations.replies": (8, 5.0),
    "conversations.info": (12, 4.0),
    "users.info": (12, 4.0),
    "reactions.get": (8, 5.0),
    "conversations.list": (6, 6.0),
}
# Slack limits these per user, not per app: a 429 is our own request volume
# and never means a shared connector circuit is open.
USER_SCOPED_METHODS = frozenset({"assistant.search.context"})
FREEZE_MIN_SEC = 5
FREEZE_MAX_SEC = 30 * 60

# `pmos slack <subcommand>` -> Slack methods the connector calls per
# request. history/thread pay conversations.info every time: the
# server re-checks channel privacy per request and refuses to trust cached
# metadata, so there is nothing to cache on our side.
_SUBCOMMAND_METHODS: dict[str, dict[str, int]] = {
    "search": {"assistant.search.context": 1},
    "thread": {"conversations.info": 1, "conversations.replies": 1},
    "whoami": {"users.info": 1},
    "people": {"users.info": 1},
    "get-reactions": {"reactions.get": 1},
    "channels": {"conversations.list": 1},
}


# Read subcommands that take `--output-format auto|json`. The server default
# is `auto` (TOON or compact JSON, whichever is shorter), so a plain read
# could print TOON and break every json.loads caller. Write commands
# (post, edit, react, ...) reject the flag.
JSON_OUTPUT_SUBCOMMANDS = frozenset({
    "get-list-item", "get-reactions", "list-channel-members", "list-list-items",
    "list-scheduled-messages", "list-user-reactions", "lookup-canvas-sections",
    "read-canvas", "history", "thread", "whoami",
    "resolve", "channels", "search", "people",
})


def json_argv(argv: list[str]) -> list[str]:
    """``argv`` plus ``--output-format json`` when its Slack subcommand takes it.

    Accepts the subcommand argv (``["thread", ...]``) or a full command
    (``["pmos", "slack", "thread", ...]``). Leaves argv as it
    is when the flag is already present, the subcommand is a write, or the
    caller passes the whole payload with ``--json`` (then it sets
    ``"output_format": "json"`` inside that payload instead).
    """
    argv = list(argv)
    start = 3 if len(argv) > 3 and argv[1:3] == ["slack"] else 0
    if len(argv) <= start or argv[start] not in JSON_OUTPUT_SUBCOMMANDS:
        return argv
    if any(token == "--output-format" or token.startswith("--output-format=") for token in argv):
        return argv
    if "--json" in argv:
        index = argv.index("--json") + 1
        if index < len(argv):
            try:
                payload = json.loads(argv[index])
            except ValueError:
                return argv
            if isinstance(payload, dict):
                payload.setdefault("output_format", "json")
                argv[index] = json.dumps(payload)
        return argv
    return argv + ["--output-format", "json"]


_NOT_A_PAYLOAD = ("error", "warning", "usage:", "traceback", "status:", "http ")


def _toon_module() -> Any:
    """slack_toon, imported lazily (most callers never need it) from this file's folder."""
    try:
        import slack_toon
    except ImportError:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("slack_toon", Path(__file__).with_name("slack_toon.py"))
        slack_toon = importlib.util.module_from_spec(spec)
        sys.modules["slack_toon"] = slack_toon
        spec.loader.exec_module(slack_toon)
    return slack_toon


def loads(text: str | None) -> Any:
    """Parse `pmos slack` stdout: JSON first, TOON as the fallback.

    Raises ``json.JSONDecodeError`` (a ``ValueError``) when the text is
    neither, so callers keep their existing ``except json.JSONDecodeError``.
    Empty output fails the same way ``json.loads("")`` does.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as json_error:
        # An error or rate-limit line ("error: {...}", "Error: ...") must stay a
        # parse failure so the caller's except branch still sees it.
        if not text or text.splitlines()[0].strip().lower().startswith(_NOT_A_PAYLOAD):
            raise
        slack_toon = _toon_module()
        try:
            return slack_toon.decode(text)
        except slack_toon.ToonDecodeError as toon_error:
            raise json.JSONDecodeError(f"neither JSON nor TOON ({toon_error})", text, 0) from json_error


def buckets_file(backoff_file: Path | None = None) -> Path:
    return (backoff_file or BACKOFF_FILE).with_name(BUCKETS_SUFFIX)


def call_cost(argv: list[str]) -> dict[str, int]:
    """Slack methods one `pmos slack <argv>` invocation will spend."""
    if not argv:
        return {}
    sub = argv[0]
    if sub == "history":
        count = 0
        collecting = False
        for token in argv[1:]:
            if token == "--channels":
                collecting = True
                continue
            if token.startswith("--"):
                collecting = False
                continue
            if collecting:
                count += 1
        count = max(1, count)
        return {"conversations.info": count, "conversations.history": count}
    return dict(_SUBCOMMAND_METHODS.get(sub, {}))


@contextmanager
def _locked_buckets(path: Path) -> Iterator[dict[str, Any]]:
    """Read-modify-write the bucket file under an exclusive lock.

    Several collectors run from different launchd jobs at once; the lock keeps
    two of them from both spending the same token.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _load(path)
            yield state
            _write_atomic(path, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _bucket_row(state: dict[str, Any], method: str, now_value: float) -> dict[str, Any]:
    capacity, refill = BUCKETS[method]
    row = state.get(method)
    if not isinstance(row, dict):
        row = {"tokens": float(capacity), "at": now_value, "frozen_until": 0.0, "freezes": 0}
        state[method] = row
    try:
        tokens = float(row.get("tokens", capacity))
        at = float(row.get("at", now_value))
    except (TypeError, ValueError):
        tokens, at = float(capacity), now_value
    if now_value > at:
        tokens = min(float(capacity), tokens + (now_value - at) / refill)
    row["tokens"] = tokens
    row["at"] = now_value
    return row


def acquire(
    costs: dict[str, int],
    *,
    reserve: int = 0,
    now_epoch: float | None = None,
    backoff_file: Path | None = None,
) -> float:
    """Spend ``costs`` tokens atomically; return 0.0, or seconds until they fit.

    All-or-nothing: a history call needs both its conversations.info and
    conversations.history tokens or none are taken. ``reserve`` keeps that many
    tokens in every touched bucket for higher-priority callers (outbound search
    uses it so it only ever spends the mention search's surplus). A frozen
    method (recent 429) is denied until its freeze ends. Methods without a
    bucket are free.
    """
    wanted = {method: int(count) for method, count in costs.items() if method in BUCKETS and int(count) > 0}
    if not wanted:
        return 0.0
    now_value = now_epoch if now_epoch is not None else time.time()
    with _locked_buckets(buckets_file(backoff_file)) as state:
        wait = 0.0
        rows = {method: _bucket_row(state, method, now_value) for method in wanted}
        for method, count in wanted.items():
            row = rows[method]
            capacity, refill = BUCKETS[method]
            frozen_until = float(row.get("frozen_until") or 0)
            if frozen_until > now_value:
                wait = max(wait, frozen_until - now_value)
            need = count + reserve
            if need > capacity:
                need = capacity
            short = need - row["tokens"]
            if short > 0:
                wait = max(wait, short * refill)
        if wait > 0:
            return max(0.001, wait)
        for method, count in wanted.items():
            rows[method]["tokens"] -= count
            rows[method]["spent"] = int(rows[method].get("spent") or 0) + count
    return 0.0


def freeze(
    method: str,
    seconds: float,
    *,
    now_epoch: float | None = None,
    backoff_file: Path | None = None,
) -> float:
    """Deny ``method`` for ``seconds`` (doubling on back-to-back freezes); returns the delay."""
    if method not in BUCKETS:
        return 0.0
    now_value = now_epoch if now_epoch is not None else time.time()
    with _locked_buckets(buckets_file(backoff_file)) as state:
        row = _bucket_row(state, method, now_value)
        previous_until = float(row.get("frozen_until") or 0)
        freezes = int(row.get("freezes") or 0)
        # A new 429 within the grace of the previous freeze means Slack's
        # window had not recovered; back off harder each time.
        freezes = freezes + 1 if now_value <= previous_until + STREAK_REHIT_GRACE_SEC else 1
        delay = max(FREEZE_MIN_SEC, float(seconds)) * (2 ** min(freezes - 1, 4))
        delay = min(FREEZE_MAX_SEC, delay)
        row["frozen_until"] = max(previous_until, now_value + delay)
        row["freezes"] = freezes
        if method in USER_SCOPED_METHODS:
            # the owner's own daily search quota is spent: start over with a full
            # refill after the freeze instead of bursting the remaining pages.
            row["tokens"] = 0.0
        # Shared-circuit methods keep their tokens: the 429 came from the
        # workspace-wide circuit, and a few seconds of freeze is the whole
        # penalty. Emptying the bucket here would make one short 429 cost a
        # large tick a long refill and kill the run.
    return delay


def freeze_from_blob(
    blob: str,
    *,
    caller: str,
    now_epoch: float | None = None,
    backoff_file: Path | None = None,
) -> tuple[str | None, float]:
    """Turn a rate-limited CLI response into a per-method freeze and journal it.

    Returns ``(method, delay)``. ``method`` is None when the blob names no
    Slack method we meter; the caller should then fall back to its shared
    cooldown. Otherwise only that method is frozen: a search throttle no
    longer stops channel and thread reads, and vice versa.
    """
    method = _json_field(blob, "api_method")
    if method not in BUCKETS:
        return None, 0.0
    now_value = now_epoch if now_epoch is not None else time.time()
    retry_after = retry_after_seconds(blob)
    delay = freeze(method, retry_after if retry_after is not None else 60, now_epoch=now_value, backoff_file=backoff_file)
    state = {
        "reason": "rate_limited",
        "next_allowed_at": int(now_value + delay),
        "updated_at": datetime.fromtimestamp(now_value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    journal_event(state, caller=caller, blob=blob, backoff_file=backoff_file)
    return method, delay


def frozen_remaining(
    costs: dict[str, int] | None = None,
    *,
    now_epoch: float | None = None,
    backoff_file: Path | None = None,
) -> int:
    """Seconds until the methods in ``costs`` (default: every metered method) thaw."""
    now_value = now_epoch if now_epoch is not None else time.time()
    state = _load(buckets_file(backoff_file))
    methods = [m for m in (costs or BUCKETS) if m in BUCKETS]
    longest = 0.0
    for method in methods:
        row = state.get(method)
        if not isinstance(row, dict):
            continue
        try:
            longest = max(longest, float(row.get("frozen_until") or 0) - now_value)
        except (TypeError, ValueError):
            continue
    return max(0, int(longest + 0.999))


def bucket_status(now_epoch: float | None = None, *, backoff_file: Path | None = None) -> dict[str, dict[str, Any]]:
    now_value = now_epoch if now_epoch is not None else time.time()
    state = _load(buckets_file(backoff_file))
    out: dict[str, dict[str, Any]] = {}
    for method, (capacity, refill) in BUCKETS.items():
        row = dict(state.get(method) or {})
        try:
            tokens = float(row.get("tokens", capacity))
            at = float(row.get("at", now_value))
            frozen_until = float(row.get("frozen_until") or 0)
        except (TypeError, ValueError):
            tokens, at, frozen_until = float(capacity), now_value, 0.0
        tokens = min(float(capacity), tokens + max(0.0, now_value - at) / refill)
        out[method] = {
            "tokens": round(tokens, 2),
            "capacity": capacity,
            "refill_seconds": refill,
            "frozen_for": max(0, int(frozen_until - now_value + 0.999)),
            "spent": int(row.get("spent") or 0),
        }
    return out


def load_events(backoff_file: Path | None = None) -> list[dict[str, Any]]:
    try:
        lines = events_file(backoff_file).read_text().splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("at"):
            events.append(row)
    return events


def _epoch(stamp: str) -> float:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def current_streak(backoff_file: Path | None = None) -> list[dict[str, Any]]:
    """Trailing events that each re-hit the limit right after the previous cooldown.

    Walks back from the newest event while event[i] happened no later than
    event[i-1].next_allowed_at + STREAK_REHIT_GRACE_SEC. Returned oldest first.
    """
    events = load_events(backoff_file)
    if not events:
        return []
    streak = [events[-1]]
    for older, newer in zip(reversed(events[:-1]), reversed(events)):
        rehit_deadline = float(older.get("next_allowed_at") or 0) + STREAK_REHIT_GRACE_SEC
        if _epoch(str(newer.get("at"))) <= rehit_deadline:
            streak.append(older)
        else:
            break
    streak.reverse()
    return streak


def _pt(epoch_value: float) -> str:
    return datetime.fromtimestamp(epoch_value, tz=PT).strftime("%H:%M PT")


def digest_line(now_epoch: float | None = None, *, backoff_file: Path | None = None) -> str | None:
    """One CoS digest bullet once the host has re-hit Slack's limit N cooldowns in a row."""
    now_value = now_epoch if now_epoch is not None else time.time()
    streak = current_streak(backoff_file)
    if len(streak) < STREAK_ALERT_THRESHOLD:
        return None
    last = streak[-1]
    last_at = _epoch(str(last.get("at")))
    if now_value - last_at > STREAK_ALERT_MAX_AGE_SEC:
        return None
    callers = sorted({str(row.get("caller") or "?") for row in streak})
    method = last.get("api_method") or "unknown method"
    origin = last.get("origin")
    if method in USER_SCOPED_METHODS:
        scope = "Slack's per-user search quota — this machine's own search volume"
    else:
        scope = {
            "local": "connector circuit already open — a shared app quota, not this machine",
            "upstream": "Slack 429 on our own request",
        }.get(str(origin), "origin unknown")
    wait = remaining(now_value, backoff_file=backoff_file, costs={method: 1} if method in BUCKETS else None)
    paused = f"readers paused until {_pt(now_value + wait)}" if wait else "readers resume on next tick"
    return (
        f"⚠️ Slack rate-limit streak: {len(streak)} consecutive cooldowns since "
        f"{_pt(_epoch(str(streak[0].get('at'))))}, last {_pt(last_at)} on {method} "
        f"({scope}); callers: {', '.join(callers)}; {paused}."
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the shared Slack backoff and rate-limit streak.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    streak = current_streak()
    payload = {
        "remaining_seconds": remaining(),
        "backoff": _load(BACKOFF_FILE),
        "buckets": bucket_status(),
        "streak_length": len(streak),
        "streak_started_at": streak[0]["at"] if streak else None,
        "digest_line": digest_line(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"backoff remaining: {payload['remaining_seconds']}s; streak: {payload['streak_length']}")
        for method, row in payload["buckets"].items():
            frozen = f" frozen {row['frozen_for']}s" if row["frozen_for"] else ""
            print(f"  {method}: {row['tokens']}/{row['capacity']} tokens, spent {row['spent']}{frozen}")
        if payload["digest_line"]:
            print(payload["digest_line"])
