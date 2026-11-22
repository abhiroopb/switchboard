#!/usr/bin/env python3
"""The CoS ask ledger: chat-only asks that must reach CoS Review.

CoS and the self-DM path ask the owner things in chat that no ledger row or triage
draft carries (the Hollis DM, the buildbot-author question). This script is
the one writer of ``system/state/cos-asks.jsonl``, an append-only JSON Lines
file; ``cos_review_feed.py`` reads it and shows each open ask under Needs you
with its options as preset replies, and a resolved ask under Past.

One-line call for CoS whenever it asks the owner something in chat::

    python3 system/scripts/cos_review_ask_ledger.py add --text "Send the Hollis DM as drafted?" \
        --option "Send it=send it" --option "Hold=hold" --source cos

Other commands::

    python3 system/scripts/cos_review_ask_ledger.py resolve ask-20261023-7f3a --reply "send it" --by owner
    python3 system/scripts/cos_review_ask_ledger.py list [--open] [--json]

Each line is one event: ``{"event": "ask", "id", "at", "text", "title", "options",
"source", "thread", "ref"}`` or ``{"event": "resolve", "id", "at", "by", "reply"}``.
State is derived by replaying the file (``load_asks``); a resolve for an unknown
id is ignored on read and refused on write. The file always lands in the main
checkout (``repo_paths``); ``COS_ASKS_FILE`` overrides the path (tests). The feed
is rebuilt after every write unless ``--no-feed`` is passed.
"""
from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from repo_paths import shared_state_file  # noqa: E402

ID_PREFIX = "ask-"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def asks_path(root: Path) -> Path:
    return Path(os.environ.get("COS_ASKS_FILE", root / "system" / "state" / "cos-asks.jsonl"))


def repo_root() -> Path:
    import cos_review_feed  # noqa: WPS433  (sibling module; resolves the main checkout from the cwd)
    return cos_review_feed.repo_root()


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("id"):
                events.append(event)
    return events


def load_asks(path: Path) -> dict[str, dict[str, Any]]:
    """Replay the file into ``{id: ask}``; ``resolved`` is None while the ask is open."""
    asks: dict[str, dict[str, Any]] = {}
    for event in read_events(path):
        kind = event.get("event")
        ask_id = str(event["id"])
        if kind == "ask":
            options = event.get("options") if isinstance(event.get("options"), list) else []
            asks[ask_id] = {
                "id": ask_id,
                "at": event.get("at"),
                "text": str(event.get("text") or "").strip(),
                "title": str(event.get("title") or "").strip(),
                "options": [o for o in options if isinstance(o, dict) and o.get("label")],
                "source": str(event.get("source") or "cos"),
                "thread": str(event.get("thread") or ""),
                "ref": str(event.get("ref") or ""),
                "resolved": None,
            }
        elif kind == "resolve" and ask_id in asks:
            asks[ask_id]["resolved"] = {"at": event.get("at"), "by": event.get("by") or "owner",
                                        "reply": str(event.get("reply") or "")}
    return asks


def open_asks(path: Path) -> list[dict[str, Any]]:
    return [a for a in load_asks(path).values() if a["resolved"] is None]


def parse_option(raw: str) -> dict[str, str]:
    """``"Label=reply text"`` → {label, reply}; a bare label replies with itself."""
    label, sep, reply = raw.partition("=")
    label = label.strip()
    if not label:
        raise ValueError(f"option needs a label, got {raw!r}")
    return {"label": label, "reply": reply.strip() if sep else label}


def new_id(now: datetime, existing: set[str]) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d")
    while True:
        candidate = f"{ID_PREFIX}{stamp}-{secrets.token_hex(2)}"
        if candidate not in existing:
            return candidate


def append_event(path: Path, event: dict[str, Any]) -> Path:
    target = shared_state_file(path)  # main checkout only, even when called from a worktree
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)
    return target


def add_ask(path: Path, text: str, options: list[dict[str, str]], source: str, thread: str, title: str,
            ref: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    text = " ".join(text.split())
    if not text:
        raise ValueError("ask text is empty")
    existing = load_asks(path)
    for ask in existing.values():  # the same open question asked twice is one ask
        if ask["resolved"] is None and ask["text"] == text:
            return dict(ask, duplicate=True)
    event = {"event": "ask", "id": new_id(now, set(existing)), "at": iso(now), "text": text,
             "title": " ".join(title.split()), "options": options, "source": source or "cos",
             "thread": thread, "ref": ref}
    append_event(path, event)
    return dict(event, resolved=None, duplicate=False)


def resolve_ask(path: Path, ask_id: str, reply: str, by: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    asks = load_asks(path)
    ask = asks.get(ask_id)
    if ask is None:
        raise ValueError(f"no ask with id {ask_id!r}")
    if ask["resolved"] is not None:
        return dict(ask, already=True)
    event = {"event": "resolve", "id": ask_id, "at": iso(now), "by": by or "owner", "reply": " ".join(reply.split())}
    append_event(path, event)
    ask["resolved"] = {"at": event["at"], "by": event["by"], "reply": event["reply"]}
    return dict(ask, already=False)


def rebuild_feed(root: Path) -> dict[str, Any] | None:
    try:
        import cos_review_feed as feed_mod
        feed = feed_mod.build_feed(root)
        feed_mod.write_feed(feed, feed_mod.feed_path(root))
        return {s["id"]: s["count"] for s in feed["sections"]}
    except Exception as exc:  # noqa: BLE001  the ask is written; a feed hiccup must not hide that
        return {"error": str(exc)[:200]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-feed", action="store_true", help="do not rebuild the CoS Review feed after writing")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("add", help="record one ask the owner was asked in chat")
    p.add_argument("--text", required=True, help="the question, as asked")
    p.add_argument("--option", action="append", default=[], metavar="LABEL=REPLY",
                   help="preset reply; repeatable; a bare label replies with itself")
    p.add_argument("--source", default="cos", help="who asked: cos, launchpad, self-dm, ... (default cos)")
    p.add_argument("--thread", default="", help="Amp thread id of the asking conversation")
    p.add_argument("--title", default="", help="short title for the feed (default: the question)")
    p.add_argument("--ref", default="", help="link the ask is about (Slack permalink, PR, doc)")
    p = sub.add_parser("resolve", help="mark one ask answered")
    p.add_argument("ask_id")
    p.add_argument("--reply", default="", help="what the owner answered")
    p.add_argument("--by", default="owner")
    p = sub.add_parser("list", help="print asks (open first)")
    p.add_argument("--open", action="store_true", help="only open asks")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = repo_root()
    path = asks_path(root)
    try:
        if args.command == "add":
            options = [parse_option(o) for o in args.option]
            result = add_ask(path, args.text, options, args.source, args.thread, args.title, args.ref)
        elif args.command == "resolve":
            result = resolve_ask(path, args.ask_id, args.reply, args.by)
        else:
            asks = sorted(load_asks(path).values(), key=lambda a: (a["resolved"] is not None, a["at"] or ""))
            if args.open:
                asks = [a for a in asks if a["resolved"] is None]
            if args.json:
                print(json.dumps(asks, indent=1, ensure_ascii=False))
            else:
                for ask in asks:
                    state = "open" if ask["resolved"] is None else f"resolved by {ask['resolved']['by']} ({ask['resolved']['reply'] or '-'})"
                    print(f"{ask['id']}  {state}  {ask['text']}")
            return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    if not args.no_feed:
        result["feed"] = rebuild_feed(root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
