#!/usr/bin/env python3
"""Persist and update deferred workstream handoffs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
STATE_DIR = Path(
    os.environ.get("PENDING_HANDOFFS_DIR")
    or REPO_ROOT / "system" / "state" / "pending-handoffs"
)

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

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_path(handoff_id: str) -> Path:
    return STATE_DIR / f"{handoff_id}.json"


def read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def write_record(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = record_path(str(payload["id"]))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _os_replace(tmp, path)


def command_enqueue(args: argparse.Namespace) -> int:
    digest_input = f"{args.workspace_uuid}|{args.workstream}|{args.message}"
    if args.delivery_kind:
        digest_input = f"{args.delivery_kind}|{digest_input}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    handoff_id = f"handoff-{digest}"
    existing = read_record(record_path(handoff_id))
    if existing is not None:
        # A provenance-blocked route must never inherit a deliverable queue
        # record with no verified thread. A later verified route for the same
        # handoff may bind it to that thread and make it deliverable again.
        if args.needs_provenance and existing.get("status") == "pending":
            existing["status"] = "needs-provenance"
            existing["needs_provenance"] = True
            existing["thread_id"] = None
            existing["updated_at"] = now_iso()
            write_record(existing)
        elif (
            existing.get("status") == "needs-provenance"
            and args.thread_id
            and not args.needs_provenance
        ):
            existing["status"] = "pending"
            existing["needs_provenance"] = False
            existing["thread_id"] = args.thread_id
            existing["updated_at"] = now_iso()
            write_record(existing)
        print(json.dumps(existing, ensure_ascii=False))
        return 0

    timestamp = now_iso()
    payload = {
        "id": handoff_id,
        "status": "needs-provenance" if args.needs_provenance else "pending",
        "needs_provenance": bool(args.needs_provenance),
        "workstream": args.workstream,
        "workspace_ref": args.workspace_ref,
        "workspace_uuid": args.workspace_uuid,
        "thread_id": args.thread_id or None,
        "message": args.message,
        "created_at": timestamp,
        "updated_at": timestamp,
        "attempts": 0,
    }
    if args.delivery_kind:
        payload["delivery_kind"] = args.delivery_kind
    write_record(payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def command_pending(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = []
    if args.handoff_id:
        candidate = read_record(record_path(args.handoff_id))
        if candidate is not None:
            records.append(candidate)
    elif STATE_DIR.exists():
        for path in sorted(STATE_DIR.glob("handoff-*.json")):
            candidate = read_record(path)
            if candidate is not None:
                records.append(candidate)
    records = [
        item
        for item in records
        if item.get("status") == "pending"
        and (not args.workstream or item.get("workstream") == args.workstream)
    ]
    print(json.dumps(records, ensure_ascii=False))
    return 0


def command_mark(args: argparse.Namespace) -> int:
    path = record_path(args.handoff_id)
    payload = read_record(path)
    if payload is None:
        print(f"error: no pending handoff matches {args.handoff_id}", file=sys.stderr)
        return 2
    timestamp = now_iso()
    payload["attempts"] = int(payload.get("attempts", 0)) + 1
    payload["updated_at"] = timestamp
    if args.status == "delivered":
        payload["status"] = "delivered"
        payload["delivered_at"] = timestamp
        if args.detail:
            payload["delivery_detail"] = args.detail
    elif args.status == "superseded":
        payload["status"] = "superseded"
        payload["superseded_at"] = timestamp
        if args.detail:
            payload["superseded_detail"] = args.detail
    else:
        payload["last_attempt_at"] = timestamp
        if args.detail:
            payload["last_attempt_detail"] = args.detail
    write_record(payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--workstream", required=True)
    enqueue.add_argument("--workspace-ref", required=True)
    enqueue.add_argument("--workspace-uuid", required=True)
    enqueue.add_argument("--thread-id", default="")
    enqueue.add_argument("--needs-provenance", action="store_true")
    enqueue.add_argument("--delivery-kind", choices=("user-response",), default="")
    enqueue.add_argument("--message", required=True)
    enqueue.set_defaults(func=command_enqueue)

    pending = subparsers.add_parser("pending")
    pending.add_argument("--handoff-id", default="")
    pending.add_argument("--workstream", default="")
    pending.set_defaults(func=command_pending)

    mark = subparsers.add_parser("mark")
    mark.add_argument("--handoff-id", required=True)
    mark.add_argument("--status", choices=("attempted", "delivered", "superseded"), required=True)
    mark.add_argument("--detail", default="")
    mark.set_defaults(func=command_mark)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
