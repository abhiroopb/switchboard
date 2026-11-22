#!/usr/bin/env python3
"""Read a Claude Code transcript tail and say whether the session stopped on a limit.

A session that hits the context or usage limit restarts on its own, so a worker
or CoS that stops on the 5-hour usage limit never sits idle until someone asks.
The sweep (dispatch.py
`handle_limits`) reads every live Claude worker through `classify_transcript`.

Two kinds, both read from the last user or assistant record of the main thread:

- usage: Claude Code writes an assistant record with `isApiErrorMessage: true`,
  `apiError: "usage_limit_reached"`, `error: "rate_limit"` and the reset epoch in
  `apiErrorParams.rate_limit_info.resetsAt` (also `quotaLimits.resetsAt`), with
  text like "You've hit your 5-hour limit · resets 2:30am (<zone>)"
  (Claude Code 2.1.319). Older builds wrote
  "Claude AI usage limit reached|<epoch>". The reset comes from the epoch first,
  then the legacy `|<epoch>`, then the "resets 2:30am (<zone>)" text.
- context: an API error that says the prompt or conversation is too long. Auto
  compaction normally prevents it.

- network: a transient API error that ended the turn after Claude Code's own
  retries gave up, for example a DNS drop that leaves an assistant record
  `apiError: "connection_failed"`, `error: "server_error"` and the text "API
  Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
  (Claude Code 2.1.321). Without a resume, such a worker sits at an empty
  prompt holding a worker slot. Also read: ECONNRESET,
  ETIMEDOUT, ECONNREFUSED, EAI_AGAIN, "Request timed out", "Connection error",
  5xx and 529 overloaded. Auth, billing, and other 4xx errors are not transient
  and stay unclassified. dispatch.py `handle_network_stops` resumes these.

Any later user or assistant record means the session moved on, so only the last
one counts. Other API errors are none of the three kinds.

`classify_screen` reads the same three kinds from a tab's visible text, for the
wedged notice and for a session whose transcript cannot be read.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

USAGE_API_ERRORS = {"usage_limit_reached"}
CONTEXT_API_ERRORS = {"prompt_too_long", "context_length_exceeded"}
USAGE_TEXT = re.compile(r"(hit your [\w\s-]*limit|usage limit reached|limit reached\s*\|\s*\d{9,})", re.I)
CONTEXT_TEXT = re.compile(r"(prompt is too long|conversation too long|context[_ ](length|window)[_ ](exceeded|limit)"
                          r"|exceeds? the (model's )?(maximum )?context)", re.I)
LEGACY_EPOCH = re.compile(r"\|\s*(\d{9,13})\b")
# "resets 2:30am (America/Los_Angeles)", or with a date for a weekly limit: "resets Nov 12, 2pm (...)".
RESETS_AT_TEXT = re.compile(r"resets\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                            r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<half>[ap]m)\b(?:\s*\((?P<zone>[^)]+)\))?", re.I)
# Transient API failures (the kind a "continue" fixes once the API answers again).
NETWORK_API_ERRORS = {"connection_failed", "connection_error", "overloaded", "overloaded_error", "api_error",
                      "server_error", "timeout", "request_timeout", "service_unavailable", "network_error"}
NETWORK_TEXT = re.compile(r"(can'?t reach the api server|\b(ENOTFOUND|EAI_AGAIN|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ECONNABORTED"
                          r"|EPIPE|ENETUNREACH|EHOSTUNREACH)\b|socket hang up|connection error|request timed out"
                          r"|fetch failed|network error|overloaded|internal server error|service unavailable|bad gateway"
                          r"|gateway timeout|api error:?\s*\(?\s*(5\d\d|408)\b)", re.I)
# Not transient: a retry gives the same answer, so these are never resumed.
NOT_TRANSIENT_TEXT = re.compile(r"(invalid api key|/login|authenticat|unauthori[sz]ed|oauth|credit balance|billing"
                                r"|permission denied|forbidden|api error:?\s*\(?\s*4(0[0-79]|[1-9]\d)\b)", re.I)
API_ERROR_LINE = re.compile(r"API Error[^\n]*", re.I)
NETWORK_CODE = re.compile(r"\b(E[A-Z]{4,12})\b|\b(5\d\d|408)\b")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DEFAULT_ZONE = "America/Los_Angeles"
# No reset time readable, or one that is not after the error: try again this much later.
FALLBACK_WAIT = timedelta(minutes=30)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def record_text(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(part.get("text") or "") for part in (content or [])
                     if isinstance(part, dict) and part.get("type") == "text")


def _epoch(value: Any) -> datetime | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 1e12:   # milliseconds
        seconds /= 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds > 0 else None


def reset_time(record: dict[str, Any], text: str, at: datetime | None) -> datetime | None:
    """When the limit lifts, from the structured epoch, the legacy `|<epoch>` text,
    or the "resets 2:30am (America/Los_Angeles)" text read as the next such time
    after the error. None when none of them is readable."""
    params = record.get("apiErrorParams") if isinstance(record.get("apiErrorParams"), dict) else {}
    info = params.get("rate_limit_info") if isinstance(params.get("rate_limit_info"), dict) else {}
    quota = record.get("quotaLimits") if isinstance(record.get("quotaLimits"), dict) else {}
    for value in (info.get("resetsAt"), quota.get("resetsAt")):
        stamp = _epoch(value)
        if stamp:
            return stamp
    legacy = LEGACY_EPOCH.search(text or "")
    if legacy:
        return _epoch(legacy.group(1))
    match = RESETS_AT_TEXT.search(text or "")
    if match and at is not None and ZoneInfo is not None:
        hour = int(match.group("hour")) % 12 + (12 if match.group("half").lower() == "pm" else 0)
        minute = int(match.group("min") or 0)
        try:
            tz = ZoneInfo((match.group("zone") or DEFAULT_ZONE).strip())
        except Exception:  # noqa: BLE001  (an unknown zone name reads as Pacific)
            tz = ZoneInfo(DEFAULT_ZONE)
        local = at.astimezone(tz)
        month = MONTHS.get((match.group("mon") or "").lower()[:3])
        if month and match.group("day"):
            try:
                candidate = local.replace(month=month, day=int(match.group("day")), hour=hour, minute=minute,
                                          second=0, microsecond=0)
            except ValueError:
                return None
            if candidate <= local:   # "resets Jan 2" read on Dec 30
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate.astimezone(timezone.utc)
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    return None


def classify_record(record: dict[str, Any], fallback_at: datetime | None = None) -> dict[str, Any] | None:
    """{"kind": "usage"|"context", "at", "resets_at", "text", "limit_type"} for one
    assistant record that is a limit error, else None. `fallback_at` (the transcript's
    modification time) stands in for a record with no timestamp, so the same record
    always reads the same way and never pushes the reset forward on every read."""
    if record.get("type") != "assistant" or not record.get("isApiErrorMessage"):
        return None
    text = record_text(record).strip()
    api_error = str(record.get("apiError") or "")
    at = parse_iso(record.get("timestamp")) or fallback_at
    if api_error in USAGE_API_ERRORS or USAGE_TEXT.search(text):
        params = record.get("apiErrorParams") if isinstance(record.get("apiErrorParams"), dict) else {}
        info = params.get("rate_limit_info") if isinstance(params.get("rate_limit_info"), dict) else {}
        resets = reset_time(record, text, at)
        if resets is None or (at is not None and resets <= at):
            resets = (at or datetime.now(timezone.utc)) + FALLBACK_WAIT
        return {"kind": "usage", "at": at, "resets_at": resets, "text": text[:200],
                "limit_type": str(info.get("rateLimitType") or "")}
    if api_error in CONTEXT_API_ERRORS or CONTEXT_TEXT.search(text):
        return {"kind": "context", "at": at, "resets_at": None, "text": text[:200], "limit_type": ""}
    if is_network_error(text, api_error, record.get("apiErrorStatus")):
        return {"kind": "network", "at": at, "resets_at": None, "text": text[:200], "limit_type": "",
                "code": network_code(text, record.get("apiErrorStatus"))}
    return None


def is_network_error(text: str, api_error: str = "", status: Any = None) -> bool:
    """A transient API failure: the network, a timeout, or a 5xx/529 from the server.
    Auth, billing, and other 4xx errors are never transient, whatever the code field says."""
    try:
        code = int(status) if status not in (None, "") else 0
    except (TypeError, ValueError):
        code = 0
    if NOT_TRANSIENT_TEXT.search(text or "") or (400 <= code < 500 and code != 408):
        return False
    return api_error in NETWORK_API_ERRORS or code >= 500 or code == 408 or bool(NETWORK_TEXT.search(text or ""))


def network_code(text: str, status: Any = None) -> str:
    """ENOTFOUND, ECONNRESET, 529, ... for the one-line notices; "" when none is named."""
    match = NETWORK_CODE.search(text or "")
    if match:
        return match.group(1) or match.group(2)
    return str(status) if status else ""


# Claude Code's prompt box and footer: lines that can sit under the last turn's output.
SCREEN_CHROME = re.compile(r"^[\s─-╿>❯›⏵·*?]*$|for shortcuts|bypass permissions|accept edits|plan mode"
                           r"|esc to interrupt|shift\+tab|auto-compact|context left", re.I)
SCREEN_TAIL_LINES = 12


PROMPT_LINE = re.compile(r"^[\s│]*[>❯](\s|$)")


def _screen_body(screen: str) -> list[str]:
    """Visible output above the prompt box: everything from the last prompt line
    (`> ` or `❯ `, boxed or between rules) down is the composer and the footer (a
    custom status line included), so it is cut; blank lines and borders are dropped."""
    lines = screen.splitlines()
    prompt = max((i for i, raw in enumerate(lines) if PROMPT_LINE.search(raw)), default=None)
    if prompt is not None:
        lines = lines[:prompt]
    body = []
    for raw in lines:
        line = raw.strip().strip("│").strip()
        if line and not SCREEN_CHROME.search(line):
            body.append(line)
    return body


QUIET_FOLLOWUP = re.compile(r"^[⎿\s]*(.*status\.anthropic\.com|.*check your (internet|network)|try again later\.?$)", re.I)
# Claude Code is still at work: retrying on its own, or a spinner is up.
LIVE_RETRY = re.compile(r"retrying in\b|\battempt \d+\s*/\s*\d+", re.I)
BUSY_LINE = re.compile(r"esc to interrupt", re.I)


def is_quiet_followup(line: str) -> bool:
    """A hint line Claude Code itself prints under an API error (status page, check
    your network). Anchored, so new output that merely says "retry" still counts as
    the session moving on."""
    return bool(QUIET_FOLLOWUP.search(line))


def classify_screen(screen: str | None) -> dict[str, Any] | None:
    """{"kind": "usage"|"context"|"network", "line"} when the last turn on a tab's
    visible text ended on an API error, else None. The error line must sit among the
    last SCREEN_TAIL_LINES lines that are not prompt-box or footer chrome, with only
    retry hints after it, so an old error higher up with real output after it does
    not count."""
    if not screen:
        return None
    tail = _screen_body(screen)[-SCREEN_TAIL_LINES:]
    for index in range(len(tail) - 1, -1, -1):
        line = tail[index]
        if not (API_ERROR_LINE.search(line) or USAGE_TEXT.search(line)):
            continue
        if LIVE_RETRY.search(line) or any(LIVE_RETRY.search(after) for after in tail[index + 1:]):
            return None   # Claude Code is still retrying on its own
        raw = screen.splitlines()
        at = max((i for i, text in enumerate(raw) if line in text), default=-1)
        if any(BUSY_LINE.search(text) for text in raw[at + 1:]):
            return None   # a spinner under the error (chrome to the body, not to this check): busy
        if any(not is_quiet_followup(after) for after in tail[index + 1:]):
            return None   # real output after the error: the session moved on
        line = line.lstrip("⎿●⏺ ").strip()[:200]
        if USAGE_TEXT.search(line):
            return {"kind": "usage", "line": line}
        if CONTEXT_TEXT.search(line):
            return {"kind": "context", "line": line}
        if is_network_error(line):
            return {"kind": "network", "line": line, "code": network_code(line)}
        return None
    return None


def screen_evidence(screen: str | None, width: int = 140) -> str:
    """What a tab shows, in a few words, for a notice: the API error line when the
    last turn ended on one, else the last line of output above the prompt box, else
    "an empty prompt". "" when the screen is unreadable."""
    if screen is None:
        return ""
    found = classify_screen(screen)
    if found:
        return f'"{found["line"][:width]}"'
    body = _screen_body(screen)
    return f'"{body[-1][:width]}"' if body else "an empty prompt"


def last_turn_record(path: Path) -> dict[str, Any] | None:
    """The last main-thread user or assistant record of a JSONL transcript, or None."""
    last = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") in {"user", "assistant"} and not record.get("isSidechain"):
                    last = record
    except OSError:
        return None
    return last


def classify_transcript(path: Path | None) -> dict[str, Any] | None:
    """The limit a session is stopped on now, or None when it is not stopped on one."""
    if path is None:
        return None
    record = last_turn_record(path)
    if not record:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0)
    except OSError:
        mtime = None
    return classify_record(record, mtime)


def pacific(stamp: datetime) -> str:
    """'2:30 AM PT' (with the date when it is not today in Pacific time)."""
    if ZoneInfo is None:
        return stamp.strftime("%H:%M UTC")
    tz = ZoneInfo(DEFAULT_ZONE)
    local, today = stamp.astimezone(tz), datetime.now(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0") + " PT"
    return clock if local.date() == today else f"{local.strftime('%b')} {local.day} {clock}"


if __name__ == "__main__":  # pragma: no cover  (manual check: usage_limits.py <transcript.jsonl>)
    import sys
    found = classify_transcript(Path(sys.argv[1]))
    print(json.dumps(found, default=str))
