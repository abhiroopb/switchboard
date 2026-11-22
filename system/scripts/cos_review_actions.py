#!/usr/bin/env python3
"""Record what the owner did with a CoS Review item: complete, skip, snooze, read
(mark as read: park a Needs-you item under For review), undo, pin, unpin, draft
(save an edited draft), reply-log.

The only writer of ``system/state/cos-review-actions.json`` (shared-state
rule); ``cos_review_reply.py`` logs deliveries through ``reply-log`` here. The
app calls this script; nothing else edits the file. After every write the feed
is rebuilt so the app sees the change on its next read.

Shape (v2)::

    {"version": 2, "updated_at": "...",
     "items":   {"task:rt-...": {"action": "complete", "at": "...", "until": null,
                                 "by": "cos", "reason": "the owner: go", "scope": "task"},
                 "draft:345":   {"action": "snooze", "at": "...", "until": "2026-01-15"}},
     "pins":    ["task:rt-..."],
     "drafts":  {"draft:345": {"text": "edited markdown", "at": "..."}},
     "replies": {"task:rt-...": [{"text": "send it", "at": "...", "state": "sent",
                                  "via": "cmux", "detail": ""}]}}

Usage (from any pm-os checkout; state always lands in the main checkout)::

    python3 system/scripts/cos_review_actions.py complete task:rt-000000000026
    python3 system/scripts/cos_review_actions.py skip draft:345
    # A ruling the owner gave in chat (go, veto, drop, hold, "I sent it") closes every ask the task
    # row raised, by task id; ``--by`` names who recorded it and ``--reason`` is shown in Past.
    python3 system/scripts/cos_review_actions.py complete --task-id rt-000000000026 --by cos --reason "the owner: go on the rename"
    python3 system/scripts/cos_review_actions.py skip --item-id draft:345 --by cos --reason "the owner answered in Slack directly"
    python3 system/scripts/cos_review_actions.py snooze task:rt-000000000027 --until 2026-01-15
    # Mark as read: the item parks under For review with draft and options intact; a
    # later report or steer on the row brings it back to Needs you. Undo removes the record.
    python3 system/scripts/cos_review_actions.py read task:rt-000000000026 --by owner
    python3 system/scripts/cos_review_actions.py undo task:rt-000000000026
    python3 system/scripts/cos_review_actions.py pin task:rt-000000000026
    python3 system/scripts/cos_review_actions.py unpin task:rt-000000000026
    python3 system/scripts/cos_review_actions.py draft draft:345 --text-file edited.md   # empty text removes the edit
    python3 system/scripts/cos_review_actions.py reply-log task:rt-... --text "send it" --state sent --via cmux
    python3 system/scripts/cos_review_actions.py list

Prints one JSON object. ``COS_REVIEW_ACTIONS_FILE`` / ``COS_REVIEW_FEED_FILE``
override the paths (tests). ``--no-feed`` skips the feed rebuild.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cos_review_feed as feed_mod  # noqa: E402
from repo_paths import shared_state_file  # noqa: E402

ACTIONS_VERSION = 2
ACTIONS = ("complete", "skip", "snooze", "read")
ITEM_ID_PREFIXES = ("task:", "draft:", "ask:")
REPLY_STATES = ("sent", "typed", "unknown", "failed", "deferred", "continuation", "dry-run", "decided", "dispatched", "redispatched")
MAX_REPLY_LOG = 20
MAX_REASON = 240

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

class ActionError(ValueError):
    pass


def load_actions(path: Path) -> dict[str, Any]:
    data = feed_mod.load_json(path, {})
    data = data if isinstance(data, dict) else {}
    items = data.get("items")
    pins = data.get("pins")
    drafts = data.get("drafts")
    replies = data.get("replies")
    return {
        "version": ACTIONS_VERSION,
        "items": items if isinstance(items, dict) else {},
        "pins": [str(p) for p in pins] if isinstance(pins, list) else [],
        "drafts": drafts if isinstance(drafts, dict) else {},
        "replies": replies if isinstance(replies, dict) else {},
    }


def write_actions(data: dict[str, Any], path: Path) -> Path:
    target = shared_state_file(path) if path.exists() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data, version=ACTIONS_VERSION, updated_at=feed_mod.iso(datetime.now(timezone.utc)))
    fd, tmp = tempfile.mkstemp(prefix=".cos-review-actions.", dir=str(target.parent))
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    _os_replace(tmp, target)
    return target


def check_item_id(item_id: str) -> str:
    item_id = item_id.strip()
    if not item_id.startswith(ITEM_ID_PREFIXES) or len(item_id) <= 5:
        raise ActionError(f"item id must look like task:rt-..., draft:N or ask:ask-..., got {item_id!r}")
    return item_id


def parse_until(value: str, today: date) -> str:
    try:
        until = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ActionError(f"--until must be YYYY-MM-DD, got {value!r}") from exc
    if until <= today:
        raise ActionError(f"--until must be after today ({today.isoformat()}), got {until.isoformat()}")
    return until.isoformat()


def check_task_id(task_id: str) -> str:
    task_id = task_id.strip()
    if not task_id.startswith("rt-") or len(task_id) <= 3:
        raise ActionError(f"--task-id must look like rt-..., got {task_id!r}")
    return task_id


def resolve_target(item_id: str | None, task_id: str | None) -> tuple[str, str]:
    """(record key, scope). ``--task-id rt-x`` records under ``task:rt-x`` with scope ``task`` so it
    closes every ask item that task row raised (``task:rt-x`` and ``task:rt-x:<hash>``); an item id
    records scope ``item`` and closes only that row."""
    if bool(item_id) == bool(task_id):
        raise ActionError("give exactly one of an item id (task:rt-..., draft:N, ask:ask-...) or --task-id rt-...")
    if task_id:
        return f"task:{check_task_id(task_id)}", "task"
    return check_item_id(str(item_id)), "item"


def record(data: dict[str, Any], item_id: str, action: str, until: str | None, now: datetime,
           by: str = "", reason: str = "", scope: str = "item") -> dict[str, Any]:
    entry: dict[str, Any] = {"action": action, "at": feed_mod.iso(now), "until": until}
    if by.strip():
        entry["by"] = by.strip()
    if reason.strip():
        entry["reason"] = " ".join(reason.split())[:MAX_REASON]
    if scope == "task":
        entry["scope"] = "task"
    data["items"][item_id] = entry
    return entry


def undo(data: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    return data["items"].pop(item_id, None)


def set_pin(data: dict[str, Any], item_id: str, pinned: bool) -> bool:
    pins = [p for p in data["pins"] if p != item_id]
    if pinned:
        pins.insert(0, item_id)
    data["pins"] = pins
    return pinned


def save_draft(data: dict[str, Any], item_id: str, text: str, now: datetime) -> dict[str, Any] | None:
    """Store an edited draft; empty or whitespace text removes the saved edit."""
    if not text.strip():
        data["drafts"].pop(item_id, None)
        return None
    entry = {"text": text.rstrip("\n") + "\n" if "\n" in text else text.strip(), "at": feed_mod.iso(now)}
    data["drafts"][item_id] = entry
    return entry


def log_reply(data: dict[str, Any], item_id: str, text: str, state: str, via: str, detail: str,
              now: datetime, extra: dict[str, str] | None = None) -> dict[str, Any]:
    if state not in REPLY_STATES:
        raise ActionError(f"--state must be one of {', '.join(REPLY_STATES)}, got {state!r}")
    entry = {"text": text, "at": feed_mod.iso(now), "state": state, "via": via, "detail": detail}
    # Dispatch history fields the app shows per item: how it was sent, the worker thread,
    # the continuation task a Do now created, the Slack permalink of a posted draft.
    for key in ("mode", "thread_id", "continuation_task_id", "permalink", "workspace_ref"):
        value = str((extra or {}).get(key) or "").strip()
        if value:
            entry[key] = value
    log = data["replies"].setdefault(item_id, [])
    if not isinstance(log, list):
        log = data["replies"][item_id] = []
    log.append(entry)
    del log[:-MAX_REPLY_LOG]
    return entry


COMPLETE_DISMISS_REASON = "cos-review complete"


def dismiss_ledger_draft(item_id: str, now: datetime, by: str) -> dict[str, Any]:
    """Complete on ``draft:N`` also drops the draft (complete means the work is done, so
    the draft goes too): the triage ledger row goes ``dismissed`` the same way ``dismiss N``
    does, so Comms Triage stops listing it as held and its note is retired by the next digest. Skipped in
    tests (``COS_REVIEW_SKIP_TRIAGE``). Returns what happened for the JSON result; never raises."""
    if os.environ.get("COS_REVIEW_SKIP_TRIAGE"):
        return {"skipped": "COS_REVIEW_SKIP_TRIAGE"}
    try:
        import triage_ledger  # type: ignore
    except Exception as exc:  # pragma: no cover - environment
        return {"error": f"triage_ledger unavailable: {exc}"}
    number = item_id.split(":", 1)[1]
    try:
        current = triage_ledger.get_draft(f"draft-{number}")
    except KeyError:
        return {"ledger": "unknown draft", "draft_id": f"draft-{number}"}
    except Exception as exc:
        return {"error": str(exc)}
    if current.get("draft_status") not in triage_ledger.PENDING_DRAFT_STATUSES:
        return {"ledger": "unchanged", "draft_status": current.get("draft_status")}
    try:
        # set_draft_status takes the source ref (a bare draft id would create a new row).
        item = triage_ledger.set_draft_status(
            current["ref"], "dismissed", f"{by or 'owner'} via cos-review",
            meta={"reason": COMPLETE_DISMISS_REASON, "at": feed_mod.iso(now)})
    except Exception as exc:
        return {"error": str(exc)}
    return {"ledger": "dismissed", "draft_id": item.get("draft_id"), "draft_status": item.get("draft_status")}


def drop_task_draft(root: Path, task_id: str, by: str) -> dict[str, Any]:
    """Complete on a task ask whose report parked a "Draft for the owner to send": record on the ledger row and in
    the workstream CONTEXT (``dispatch.py decide``) that the draft is dropped, never sent. Dispatches nothing."""
    if os.environ.get("COS_REVIEW_SKIP_TRIAGE"):
        return {"skipped": "COS_REVIEW_SKIP_TRIAGE"}
    row = next((r for r in feed_mod.ledger_rows(root) if str(r.get("id")) == task_id), None)
    if not row or not feed_mod.drafts_in(feed_mod.normalize(row.get("summary") or "")):
        return {"draft": "none"}
    import subprocess
    cmd = [sys.executable, str(SCRIPT_DIR / "dispatch.py"), "decide", "--task-id", task_id, "--by", by or "owner",
           "draft dropped, not sent (Complete in CoS Review)"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(root))
    except Exception as exc:
        return {"draft": "drop-failed", "error": str(exc)}
    if proc.returncode != 0:
        return {"draft": "drop-failed", "error": (proc.stderr or proc.stdout).strip()[-400:]}
    return {"draft": "dropped"}


def rebuild_feed(root: Path) -> dict[str, Any]:
    feed = feed_mod.build_feed(root)
    feed_mod.write_feed(feed, feed_mod.feed_path(root))
    counts = {s["id"]: s["count"] for s in feed["sections"]}
    return {**counts, "snoozed": feed["snoozed_count"]}


def read_text(args: argparse.Namespace) -> str:
    if getattr(args, "text_file", None):
        return Path(args.text_file).read_text()
    return args.text or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-feed", action="store_true", help="do not rebuild the feed after writing")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ACTIONS + ("undo", "pin", "unpin"):
        p = sub.add_parser(name)
        p.add_argument("item_id", nargs="?", default=None, help="task:rt-..., draft:N or ask:ask-... (or use --item-id / --task-id)")
        p.add_argument("--item-id", dest="item_id_opt", default=None, help="same as the positional item id")
        p.add_argument("--task-id", default=None, help="rt-...: act on every ask that task row raised")
        if name in ACTIONS:
            p.add_argument("--by", default="", help="who recorded it: owner (app click), cos, launchpad, <script name>")
            p.add_argument("--reason", default="", help="the ruling or evidence, shown on the Past row")
        if name == "snooze":
            p.add_argument("--until", required=True, help="YYYY-MM-DD, after today (PT)")
        if name == "complete":
            p.add_argument("--keep-draft", action="store_true",
                           help="close the item only; do not dismiss a ledger draft or drop a parked task draft "
                                "(cos_review_reply.py uses it after a delivered reply or decision)")
    p = sub.add_parser("draft", help="save an edited draft (empty text removes the edit)")
    p.add_argument("item_id")
    p.add_argument("--text", default=None)
    p.add_argument("--text-file", default=None)
    p = sub.add_parser("reply-log", help="append one delivery record (used by cos_review_reply.py)")
    p.add_argument("item_id")
    p.add_argument("--text", required=True)
    p.add_argument("--state", required=True, help=f"one of {', '.join(REPLY_STATES)} (validated in log_reply so errors stay JSON)")
    p.add_argument("--via", default="")
    p.add_argument("--detail", default="")
    p.add_argument("--mode", default="", help="send | decide | do-now (how the reply was delivered)")
    p.add_argument("--thread-id", default="", help="worker thread the reply reached")
    p.add_argument("--continuation-task-id", default="", help="task id a Do now continuation created")
    p.add_argument("--permalink", default="", help="Slack permalink of a posted draft")
    p.add_argument("--workspace-ref", default="", help="cmux workspace:N of the continuation worker tab")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    root = feed_mod.repo_root()
    path = feed_mod.actions_path(root)
    data = load_actions(path)
    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {"command": args.command, "file": str(path)}
    try:
        if args.command == "list":
            result.update(items=data["items"], pins=data["pins"], drafts=data["drafts"], replies=data["replies"])
            print(json.dumps(result, indent=1, ensure_ascii=False))
            return 0
        scope = "item"
        if args.command in ACTIONS + ("undo", "pin", "unpin"):
            item_id, scope = resolve_target(args.item_id or args.item_id_opt, args.task_id)
        else:
            item_id = check_item_id(args.item_id)
        result["item_id"] = item_id
        if args.command == "undo":
            removed = undo(data, item_id)
            result["removed"] = removed
            if removed is None:
                result["note"] = "no action recorded for that item"
        elif args.command in ("pin", "unpin"):
            result["pinned"] = set_pin(data, item_id, args.command == "pin")
        elif args.command == "draft":
            if args.text is None and args.text_file is None:
                raise ActionError("draft needs --text or --text-file")
            result["draft"] = save_draft(data, item_id, read_text(args), now)
        elif args.command == "reply-log":
            result["reply"] = log_reply(data, item_id, args.text, args.state, args.via, args.detail, now,
                                        {"mode": args.mode, "thread_id": args.thread_id,
                                         "continuation_task_id": args.continuation_task_id,
                                         "permalink": args.permalink, "workspace_ref": args.workspace_ref})
        else:
            until = parse_until(args.until, now.astimezone(feed_mod.PT).date()) if args.command == "snooze" else None
            result["recorded"] = record(data, item_id, args.command, until, now, by=args.by, reason=args.reason, scope=scope)
            if args.command == "complete" and not args.keep_draft:
                # Complete drops the draft as well. Written after the actions
                # record so a ledger failure still leaves the click recorded; the result says what happened.
                if item_id.startswith("draft:"):
                    result["ledger"] = dismiss_ledger_draft(item_id, now, args.by)
                elif item_id.startswith("task:"):
                    task_id = args.task_id or item_id.split(":", 2)[1]
                    result["task_draft"] = drop_task_draft(root, task_id, args.by)
    except (ActionError, OSError) as exc:
        print(json.dumps({"error": str(exc), **result}), file=sys.stderr)
        return 2
    result["file"] = str(write_actions(data, path))
    if not args.no_feed:
        result["feed"] = rebuild_feed(root)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
