"""Shared helpers for notify-urgent-* scripts.

Each surface (Gmail, GitHub, Linear, Doc comments, Slack) has its own
notify-urgent-<surface>.py script that filters its own collector output and
calls into these helpers for the common parts: desktop banner, comms-prompt
enqueue/flush, captured_at parsing, and the age/notified gate.

Keeps the per-surface scripts small and consistent.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMS_PROMPT_SH = REPO_ROOT / "daemon" / "comms-prompt.sh"

DEFAULT_MAX_AGE_SECONDS = 4 * 60 * 60
DEFAULT_MAX_NOTIFY_PER_RUN = 5
OVERFLOW_GIVE_UP_ATTEMPTS = 3


def make_logger(log_file: Path) -> Callable[[str], None]:
    def log(msg: str) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_file.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    return log


def osa_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, subtitle: str, body: str, log: Callable[[str], None]) -> None:
    title_e = osa_escape(title[:80])
    subtitle_e = osa_escape(subtitle[:80])
    body_e = osa_escape(body[:240])
    script = (
        f'display notification "{body_e}" with title "{title_e}" '
        f'subtitle "{subtitle_e}" sound name "Glass"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except subprocess.TimeoutExpired:
        log(f"osascript timed out: {title}")


def enqueue_comms_prompt(sender: str, where: str, link: str, body: str, log: Callable[[str], None]) -> bool:
    if not COMMS_PROMPT_SH.exists():
        return False
    try:
        result = subprocess.run(
            ["bash", str(COMMS_PROMPT_SH), "enqueue", sender, where, link, body],
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"comms-prompt enqueue timed out for {sender}")
        return False


def record_ledger(row: dict, source: str, stage: str, by: str, **meta: str) -> None:
    ref = row.get("source_ref") or row.get("permalink") or row.get("link") or ""
    if not ref:
        return
    try:
        from triage_ledger import record_item

        record_item(ref, source, stage=stage, by=by, meta=meta)
    except (OSError, ValueError):
        pass


def retire_repeated_overflow(rows: list[dict], candidates: list[tuple], limit: int, source: str, now: datetime) -> bool:
    """Move repeatedly capped candidates to the durable ledger."""
    changed = False
    for candidate in candidates[limit:]:
        row = candidate[-1]
        attempts = int(row.get("notify_overflow_count") or 0) + 1
        row["notify_overflow_count"] = attempts
        changed = True
        if attempts >= OVERFLOW_GIVE_UP_ATTEMPTS:
            record_ledger(row, source, "collected", "notifier-ageout", reason="per-run-cap-repeated")
            row["notified"] = True
            row["notified_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            row["notify_skipped_reason"] = "per-run-cap-repeated"
    return changed


def flush_comms_prompts(log: Callable[[str], None]) -> None:
    if not COMMS_PROMPT_SH.exists():
        return
    try:
        subprocess.run(
            ["bash", str(COMMS_PROMPT_SH), "flush"],
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        log("comms-prompt flush timed out")


def parse_captured_at(row: dict) -> datetime | None:
    cap = row.get("captured_at")
    if not cap:
        return None
    try:
        return datetime.fromisoformat(str(cap).replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_rows(path: Path) -> tuple[list[dict], list[bytes]]:
    rows: list[dict] = []
    opaque: list[bytes] = []
    if not path.exists():
        return rows, opaque
    for line in path.read_bytes().splitlines(keepends=True):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            value = None
        if isinstance(value, dict):
            rows.append(value)
        else:
            opaque.append(line)
    return rows, opaque


def load_rows(path: Path) -> list[dict]:
    return _read_rows(path)[0]


def write_rows(path: Path, rows: list[dict]) -> None:
    # Caller owns the source lock from load_rows through this replacement.
    opaque = _read_rows(path)[1]
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp = Path(handle.name)
            for row in rows:
                handle.write((json.dumps(row) + "\n").encode("utf-8"))
            handle.writelines(opaque)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def normalize_body(s: str | None, limit: int = 600) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]
