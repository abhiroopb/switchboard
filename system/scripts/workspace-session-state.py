#!/usr/bin/env python3
"""workspace-session-state.py — classify the live agent session (Amp or
Claude Code) behind a cmux workspace or workstream, joining three existing
sources of truth:

  1. `cmux rpc workspace.list`           -> workspace ref/id/title
  2. the cmux session store              -> session ID + Running/Idle state
     (~/Library/Application Support/cmux/session-com.cmuxterm.app.json;
     Amp is bridged by the cmux-managed cmux-session.ts plugin, Claude Code
     by cmux's own Claude hooks). `thread_id` in the output is the Amp
     thread ID (T-...) or the Claude session UUID (the store's
     resumeBinding.checkpointId); `agent_kind` says which.
  3. system/state/sources/workspace-updates.json -> durable workstream status
     (written by emit-workspace-update.py; the workstream-lifecycle plugin
     CAS-flips active->waiting on clean agent end)

This is the shared classifier behind CoS launch-failure recovery and routed-
task outcome verification. It deliberately reuses the exact same
join logic and fixture overrides as command-center-digest.py rather than
inventing a new discovery path.

Classification:
  active          Agent state is Running (Amp) or Running/Working (Claude); do not touch.
  idle-complete   Idle + the workstream's durable update reached a settled
                  status (waiting/done/paused/...). Task ended properly.
  paused-mid-task Idle but the workstream update is still `active` (or absent):
                  the worker stopped without finishing or emitting a blocker.
                  This is the workspace:88 "stopped after /tmp file" failure.
  blocked         Idle + the workstream explicitly escalated to the owner
                  (status=blocked, needs action, or needs review).
  unreachable     No local workspace, or no Amp or Claude agent bound to it.

Usage:
  workspace-session-state.py --workspace workspace:85
  workspace-session-state.py --workstream pm-os-reliability

Output: one JSON line. Exit 0 whenever a classification was produced
(including unreachable); exit 2 on usage/environment errors.

Test fixtures (same shapes as command-center-digest.py):
  CC_WORKSPACES_JSON     replaces `cmux rpc workspace.list`
  CC_CMUX_SESSION_JSON   replaces the cmux session store
  PM_OS_WORKSPACE_UPDATES_JSON  replaces workspace-updates.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.mux import runnable as _runnable  # Windows: the extensionless mux/cmux wrapper runs via Python

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
CMUX_BIN = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")
CMUX_SESSION_STORE = Path(os.environ.get("PM_OS_CMUX_SESSION_JSON") or Path.home() / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json")

# Statuses emit-workspace-update.py treats as settled (AUTO_CLOSE_STATUSES)
# plus done-like variants. `active` is the only in-flight status.
# "to review": the launch prompt teaches every worker to emit
# `--status "to review"` for completed work awaiting the owner. Leaving it out
# would classify every finished-and-reviewable workspace as paused-mid-task on
# each sweep, giving a false needs_human and a "resume" nudge aimed at a
# worker that had already finished.
SETTLED_STATUSES = {
    "waiting", "paused", "scheduled", "needs follow-up",
    "done", "resolved", "cancelled", "archived", "to review",
}
# Explicit escalations: emit-workspace-update.py reserves `blocked` /
# `needs action` / `needs review` for "the worker stopped on purpose and needs
# the owner". All three mean the same thing to recovery: surface to the owner,
# never nudge the worker to "resume", never relaunch.
ESCALATION_STATUSES = {"blocked", "needs action", "needs review"}


def list_workspaces() -> list[dict]:
    fixture = os.environ.get("CC_WORKSPACES_JSON")

    def rpc(method, params):
        return json.loads(subprocess.check_output(
            _runnable([CMUX_BIN, "rpc", method, json.dumps(params)]), text=True, timeout=15
        ))

    if fixture:
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    else:
        # A selected-window listing and the saved session store cannot prove
        # absence. Resolve refs from every live window, never cached titles.
        windows = rpc("window.list", {})["windows"]
        data = {"workspaces": []}
        for window in windows:
            result = rpc("workspace.list", {"window_id": window["id"]})
            if (result["window_id"] != window["id"]
                    or len(result["workspaces"]) != window["workspace_count"]):
                raise ValueError("incomplete workspace inventory")
            data["workspaces"].extend(result["workspaces"])
        after = rpc("window.list", {})["windows"]
        if sorted((w["id"], w["workspace_count"]) for w in after) != sorted(
            (w["id"], w["workspace_count"]) for w in windows
        ):
            raise ValueError("workspace inventory changed")
    rows = []
    for w in data["workspaces"]:
        ref = w.get("ref") or ""
        if not re.fullmatch(r"workspace:\d+", ref) or not w.get("id"):
            raise ValueError("invalid workspace inventory row")
        rows.append({
            "ref": ref,
            "id": (w.get("id") or "").strip(),
            "name": (w.get("custom_title") or w.get("title") or "").strip(),
            "remote": bool((w.get("remote") or {}).get("enabled")),
        })
    return rows


# Session-store agent kinds this classifier understands, with the key cmux
# uses for that agent's Running/Idle status entry. Amp's entry is written by
# the cmux-session.ts plugin; Claude Code's by cmux's own Claude hooks
# (verified against a live store: kind "claude", statusEntries key
# "claude_code", sessionId == resumeBinding.checkpointId).
AGENT_STATUS_KEYS = {"amp": "amp", "claude": "claude_code"}


def amp_agents() -> dict[str, dict]:
    """{workspace_id: {"thread_id", "agent_kind", "state", "state_ts"}} — the
    same join as command-center-digest.py amp_agent_map(), extended to Claude
    Code tabs. `thread_id` keeps its name for every caller: it is the Amp
    thread ID or the Claude session UUID, whichever agent the tab runs."""
    fixture = os.environ.get("CC_CMUX_SESSION_JSON")
    path = Path(fixture) if fixture else CMUX_SESSION_STORE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    agents: dict[str, dict] = {}
    for win in data.get("windows", []) or []:
        for ws in ((win.get("tabManager") or {}).get("workspaces") or []):
            wid = (ws.get("workspaceId") or "").strip()
            if not wid:
                continue
            thread_id = ""
            agent_kind = ""
            for panel in ws.get("panels") or []:
                terminal = panel.get("terminal") or {}
                agent = terminal.get("agent") or {}
                kind = str(agent.get("kind") or "")
                if kind not in AGENT_STATUS_KEYS:
                    continue
                # Claude Code's store entry carries the session UUID both on
                # the agent record and as the resumeBinding checkpoint; the
                # checkpoint is what `claude --resume` takes, so prefer it.
                session_id = agent.get("sessionId")
                if kind == "claude":
                    session_id = (terminal.get("resumeBinding") or {}).get("checkpointId") or session_id
                if session_id:
                    thread_id = str(session_id)
                    agent_kind = kind
                    break
            status_keys = [AGENT_STATUS_KEYS[agent_kind]] if agent_kind else list(AGENT_STATUS_KEYS.values())
            status = next(
                (e for e in ws.get("statusEntries") or [] if e.get("key") in status_keys),
                None,
            )
            if not thread_id and not status:
                continue
            if not agent_kind and status:
                agent_kind = next(k for k, v in AGENT_STATUS_KEYS.items() if v == status.get("key"))
            agents[wid] = {
                "thread_id": thread_id,
                "agent_kind": agent_kind,
                "state": str((status or {}).get("value") or ""),
                "state_ts": float((status or {}).get("timestamp") or 0),
                # Title kept for the multi-window fallback: `cmux rpc
                # workspace.list` only covers the CURRENT window, while the
                # session store covers all windows.
                "title": str(ws.get("customTitle") or ws.get("title") or ""),
            }
    return agents


def workstream_update(source_key: str) -> dict:
    fixture = os.environ.get("PM_OS_WORKSPACE_UPDATES_JSON")
    path = Path(fixture) if fixture else REPO_ROOT / "system/state/sources/workspace-updates.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return (data.get("updates") or {}).get(source_key) or {}


def normalize(text: str) -> str:
    """Lowercase alnum-only, so '📮 Pm Os Reliability' and
    '🔥 P0 · Pm Os Reliability' both match slug 'pm-os-reliability'."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def find_workspace(rows: list[dict], ref: str | None, slug: str | None) -> dict | None:
    if ref:
        return next((r for r in rows if r["ref"] == ref), None)
    want = normalize(slug or "")
    for r in rows:
        name = normalize(r["name"])
        # allow the P0-canonical prefix ("p0" + name) and exact matches
        if name == want or name == "p0" + want or name.endswith(want):
            return r
    return None


def classify(agent: dict | None, update_status: str) -> str:
    if agent is None:
        return "unreachable"
    state = (agent.get("state") or "").strip().lower()
    # Amp's cmux-session.ts writes Running/Idle; cmux's Claude hooks write
    # Idle and (busy value not yet observed live) Running or Working.
    if state in ("running", "working"):
        return "active"
    # Idle, or no status entry yet (a bound thread with no live turn).
    if update_status in ESCALATION_STATUSES:
        return "blocked"
    if update_status in SETTLED_STATUSES:
        return "idle-complete"
    return "paused-mid-task"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", help="workspace ref, e.g. workspace:85")
    parser.add_argument("--workstream", help="workstream slug, e.g. pm-os-reliability")
    args = parser.parse_args()
    if not args.workspace and not args.workstream:
        parser.error("provide --workspace and/or --workstream")

    try:
        rows = list_workspaces()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"error: live cmux inventory unavailable: {error}", file=sys.stderr)
        return 2
    row = find_workspace(rows, args.workspace, args.workstream)
    agents = amp_agents()
    agent = agents.get(row["id"]) if row else None

    source_key = f"workstream:{args.workstream}" if args.workstream else ""
    update = workstream_update(source_key) if source_key else {}
    update_status = str(update.get("status") or "").strip().lower()

    result = {
        "workspace_present": row is not None,
        "workspace_ref": row["ref"] if row else None,
        "workspace_id": row["id"] if row else None,
        "title": row["name"] if row else None,
        "remote": row["remote"] if row else None,
        "thread_id": (agent or {}).get("thread_id") or None,
        "agent_kind": (agent or {}).get("agent_kind") or None,
        "agent_state": (agent or {}).get("state") or None,
        "state_ts": (agent or {}).get("state_ts") or None,
        "workstream": args.workstream or None,
        "update_status": update_status or None,
        "update_ts": update.get("timestamp") or None,
        "classification": classify(agent, update_status),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
