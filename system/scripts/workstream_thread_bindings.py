#!/usr/bin/env python3
"""Durable launcher evidence. Telemetry content is not an identity database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


CLAUDE_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

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

def binding_file(root):
    state = Path(os.environ.get("SWITCHBOARD_STATE_DIR") or Path(root) / "system/state")
    return Path(os.environ.get("WORKSTREAM_THREAD_BINDINGS_FILE") or state / "workstream-thread-bindings.json")


def binding_provenance(root, thread_id, slug):
    """None means absent (legacy fallback allowed); damaged evidence fails closed."""
    try:
        data = json.loads(binding_file(root).read_text())
        row = data["bindings"].get(thread_id)
        if row is None:
            return None
        return "verified" if row["thread_id"] == thread_id and row["slug"] == slug else "unverified"
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "unverified"


def record_binding(root, thread_id, slug, workspace_id, workspace_ref, *, role="owner",
                   source="launcher", launched_at=None, evidence=None):
    if (not thread_id or not workspace_id or not workspace_ref
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
            or role not in {"owner", "worker"} or source not in {"launcher", "backfill"}
            or (source == "backfill" and not evidence)):
        raise ValueError("incomplete-workstream-binding")
    path = binding_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock a separate inode so concurrent launches cannot lose rows on replace.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {"version": 1, "bindings": {}}
        existing = data["bindings"].get(thread_id)
        if existing:
            if existing["slug"] != slug or existing["role"] != role:
                raise ValueError("conflicting-workstream-binding")
            return existing
        now = datetime.now(timezone.utc).isoformat()
        row = {"thread_id": thread_id, "slug": slug, "workspace_id": workspace_id,
               "workspace_ref": workspace_ref, "role": role, "source": source,
               # Amp thread ids are `T-...`; Claude Code session ids are UUIDs
               # (what `claude --resume` takes). Derived, so no caller changes.
               "agent": "claude" if CLAUDE_SESSION_ID.fullmatch(str(thread_id)) else "amp",
               # Receipt backfills prove identity, not the original launch time.
               "launched_at": launched_at or (now if source == "launcher" else None),
               "recorded_at": now}
        if evidence is not None:
            row["evidence"] = evidence
        data["bindings"][thread_id] = row
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as f:
                temp = Path(f.name)
                json.dump(data, f, indent=2)
                f.write("\n")
            _os_replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return row


def workspace_session(root, workspace_ref):
    result = subprocess.run([sys.executable, str(Path(root) / "system/scripts/workspace-session-state.py"),
                             "--workspace", workspace_ref], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def live_thread(root, workspace_ref, wait=30.0, interval=1.0):
    """Local Amp thread bound to a workspace, polling until the session store has it.

    The cmux session store learns a new tab's thread ID at Amp's session_start,
    a second or more after the prompt renders. Launchers that read it once at
    prompt render get "" and then fail every binding as session-changed.
    Returns "" after `wait`.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            session = workspace_session(root, workspace_ref)
            thread = session.get("thread_id") or ""
            if thread and session.get("remote") is False:
                return thread
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            return ""
        time.sleep(interval)


def record_workspace(root, workspace_ref, slug, expected_thread, expected_workspace_id=None):
    """Recheck the live identity after a successful seed, never infer it from title.

    Raises launch-binding-thread-unknown when the launcher never learned the
    seeded thread (nothing to verify against) and launch-binding-session-changed
    when the live session differs from the one that was seeded.
    """
    if not expected_thread:
        raise ValueError("launch-binding-thread-unknown")
    session = workspace_session(root, workspace_ref)
    if (session.get("thread_id") != expected_thread
            or (expected_workspace_id is not None and session.get("workspace_id") != expected_workspace_id)
            or session.get("workspace_ref") != workspace_ref or session.get("remote") is not False):
        raise ValueError("launch-binding-session-changed")
    return record_binding(root, expected_thread, slug, session.get("workspace_id"), workspace_ref)


def recover_workspace(root, slug, title, cwd, before, cmux, timeout=30):
    """Recover one created tab after a lost reply; title alone is not identity."""
    from cmux_new_contextual_amp_tab import rpc, THREAD_ID

    if not isinstance(before, dict) or not isinstance(before.get("workspaces"), list):
        raise ValueError("launch-recovery-needs-before-inventory")
    prior = {w["id"] for w in before["workspaces"]}
    deadline = time.monotonic() + timeout
    while True:
        try:
            rows = rpc(cmux, "workspace.list", {}).get("workspaces", [])
            candidates = [w for w in rows if w.get("id") not in prior
                          and (w.get("custom_title") or w.get("title")) == title
                          and Path(w.get("current_directory") or "/").resolve() == Path(cwd).resolve()]
            if len(candidates) > 1:
                raise ValueError("launch-recovery-ambiguous")
            if candidates:
                workspace = candidates[0]
                surfaces = rpc(cmux, "surface.list", {"workspace_id": workspace["id"]}).get("surfaces", [])
                terminals = [s for s in surfaces if s.get("type") == "terminal"]
                if len(terminals) != 1:
                    raise ValueError("launch-recovery-surface-unverified")
                binding = terminals[0].get("resume_binding") or {}
                thread = binding.get("checkpoint_id") or ""
                if binding.get("kind") == "amp" and THREAD_ID.fullmatch(thread):
                    # Persist before returning the recovered tab to the caller.
                    record_workspace(root, workspace["ref"], slug, thread, workspace["id"])
                    return {"workspace_ref": workspace["ref"], "thread_id": thread, "binding_recorded": True}
                if not binding:
                    # A new shell has no thread yet. The in-tab observer records
                    # it when booted; never use this as proof of Amp receipt.
                    return {"workspace_ref": workspace["ref"], "thread_id": None, "binding_recorded": False}
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            raise ValueError("launch-recovery-unverified")
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2]))
    sub = parser.add_subparsers(dest="command", required=True)
    lookup = sub.add_parser("lookup")
    lookup.add_argument("thread_id")
    lookup.add_argument("slug")
    record = sub.add_parser("record-workspace")
    record.add_argument("workspace_ref")
    record.add_argument("slug")
    record.add_argument("expected_thread")
    live = sub.add_parser("live-thread", help="print the local thread bound to a workspace, waiting for it to appear")
    live.add_argument("workspace_ref")
    live.add_argument("--wait", type=float, default=30.0)
    recover = sub.add_parser("recover-launch")
    recover.add_argument("slug")
    recover.add_argument("title")
    recover.add_argument("cwd")
    recover.add_argument("before_json")
    recover.add_argument("--cmux", required=True)
    args = parser.parse_args()
    if args.command == "lookup":
        print(binding_provenance(args.root, args.thread_id, args.slug) or "absent")
        return 0
    if args.command == "live-thread":
        thread = live_thread(args.root, args.workspace_ref, wait=args.wait)
        print(thread)
        return 0 if thread else 1
    try:
        if args.command == "recover-launch":
            result = recover_workspace(args.root, args.slug, args.title, args.cwd,
                                       json.loads(args.before_json), args.cmux)
            print(result["workspace_ref"])
        else:
            record_workspace(args.root, args.workspace_ref, args.slug, args.expected_thread)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Workstream binding not recorded: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
