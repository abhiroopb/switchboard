#!/usr/bin/env python3
"""Deliver a reply typed in CoS Review to the Amp thread that owns the item.

The app calls this instead of the owner opening the thread. Routing:

* ``task:<rt-id>`` items go through ``router-dispatch.sh --origin cos --steer
  <rt-id>`` (the same path CoS uses). A live row answers ``status: message``;
  the text is then typed into that worker's cmux tab through the inject guard
  (``cig_safe_send``), which never clobbers a busy agent or an unsent draft.
  A terminal row makes ``dispatch.py`` open a ``--continuation-of`` worker,
  reported here as state ``continuation`` with the new task id.
* ``draft:<N>`` items (Comms Triage drafts) go to the ``📨 Comms Triage`` tab,
  where ``send N`` / ``skip N`` are the commands that surface understands.

Every attempt is recorded through ``cos_review_actions.py reply-log`` so the
item shows its delivery state (``sent`` = read back on the tab's screen,
``typed`` = cmux accepted it but the screen could not confirm, ``unknown`` =
transport error after typing, never retried, ``failed``, ``deferred``,
``continuation``, ``dry-run``) after the next feed read. For a ``draft:N``
item a saved app edit is stored in the triage ledger before ``send N`` so the
edited words post. Nothing here posts to
Slack, GitHub, or email: the owning thread does that under its own rules.

Usage::

    python3 system/scripts/cos_review_reply.py task:rt-000000000026 --text "go"
    python3 system/scripts/cos_review_reply.py draft:346 --text "send 346"
    python3 system/scripts/cos_review_reply.py task:rt-... --text "go" --dry-run     # route only, no send
    python3 system/scripts/cos_review_reply.py task:rt-... --text "go" --continue    # terminal row: dispatch

Modes::

    --mode decide   task: record the decision (go/veto/keep/option N) on the ledger row and the
                    owning CONTEXT.md through ``dispatch.py decide`` and complete the item. No worker.
    --mode send     task: type the text into the 🧭 CoS tab (CoS decides what to do with it);
                    draft: ``send N`` / ``skip N`` into 📨 Comms Triage (unchanged);
                    ask: type the answer into the CoS tab and resolve the ask in the ask ledger.
    --mode do-now   task: ``router-dispatch.sh --origin cos --steer`` right away (native message to a
                    live worker, continuation when the row is terminal); a stalled row with reply
                    ``re-dispatch`` runs ``router-dispatch.sh --task-id <rt> --force-redispatch``
                    and ``drop it`` cancels the row; draft: posts to Slack through
                    ``send-triage-draft.py`` (robot prefix applied there) and records the permalink.
    --mode auto     (default) decision-shaped text or a preset reply → decide; otherwise send.

Without ``--continue`` a terminal row is reported as ``failed`` with
``needs_continuation: true`` so the app can show "thread not live" and offer
the continuation button; with it the continuation is dispatched.
``COS_REVIEW_REPLY_DRY_RUN=1`` forces ``--dry-run`` (tests, click tests).

Prints one JSON object: ``{"state": ..., "detail": ..., "via": ..., "task_id":
..., "thread_id": ..., "workspace_ref": ..., "needs_continuation": bool}``.
Exit 0 for sent/typed/continuation/decided/dispatched/redispatched/dry-run, 1 for
failed/deferred/unknown, 2 for bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):  # pragma: no cover
    # dispatch.py (imported for steers) uses 3.12 f-string syntax; under Apple's /usr/bin/python3 3.9 the
    # import dies with "SyntaxError: f-string: unmatched '['", which the app would show as the delivery
    # line. Name the real cause instead so the item says what to fix.
    print(json.dumps({"state": "failed", "detail": (
        f"python3 is {sys.version.split()[0]} at {sys.executable}; the switchboard scripts need 3.12+. "
        "The app's PATH must put Homebrew (/opt/homebrew/bin) ahead of /usr/bin.")}))
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cos_review_feed as feed_mod  # noqa: E402

TRIAGE_TITLE = "Comms Triage"
COS_TITLE = "CoS"
SEND_WAIT_SECONDS = 20
OK_STATES = {"sent", "typed", "continuation", "dry-run", "decided", "dispatched", "redispatched"}
# A decision word, optionally followed by a reason after a colon or dash ("hold: waiting on legal",
# "go - keep the old slug"). A comma does not count ("no, rerun it with X" is an instruction, not a decision).
# Bare yes/no and a-d are NOT decisions: "no" on a running row is a note to CoS, and a
# preset such as "send the draft as is" or "retry" is an instruction that only looks like a choice.
DECISION_RE = re.compile(
    r"^(go|veto|keep|hold|approve|approved|reject|ship it|no vetoes|option\s+[a-z0-9]+)\b[.!]?(\s*[:–—-]\s*\S.*)?$", re.I)
# Preset labels whose click records a decision; every other preset is an instruction for CoS.
DECISION_LABEL_RE = re.compile(r"^(go|hold|keep|veto|no vetoes|option\s+[a-z0-9]+|\([a-z0-9]\).*)$", re.I)
PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,40}>")
LIVE_STATUSES = {"routing", "queued", "active"}
SEND_SHAPED = {"send it", "send", "post it", "post", "go", "send as is", "send it as is", "post now", "do it"}


class ReplyError(ValueError):
    pass


def run_json(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                data.setdefault("_rc", proc.returncode)
                return data
        except ValueError:
            continue
    return {"status": "failed", "reason": (proc.stderr or proc.stdout or "no output").strip()[:400], "_rc": proc.returncode}


CMUX_APP_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def cmux_binary() -> str:
    """The cmux CLI. A Finder-launched app runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin, where
    ``cmux`` is not found; fall back to the path inside cmux.app (the same default the inject guard uses)."""
    return os.environ.get("CMUX_BIN") or shutil.which("cmux") or CMUX_APP_BIN


class CmuxUnreachable(Exception):
    """cmux could not be asked for its workspace list; says nothing about whether a tab is open."""


def cmux_workspaces() -> list[dict[str, Any]]:
    """Live cmux workspaces. Raises ``CmuxUnreachable`` (with the reason) when the CLI is missing,
    times out, exits nonzero, or returns something that is not JSON."""
    binary = cmux_binary()
    try:
        proc = subprocess.run([binary, "workspace", "list", "--json"], capture_output=True, text=True,
                              env={**os.environ, "CMUX_QUIET": "1"}, timeout=10)
    except FileNotFoundError:
        raise CmuxUnreachable(f"{binary} not found (PATH={os.environ.get('PATH', '')})")
    except subprocess.TimeoutExpired:
        raise CmuxUnreachable(f"{binary} workspace list timed out after 10 s")
    except OSError as exc:
        raise CmuxUnreachable(f"{binary}: {exc}")
    if proc.returncode != 0:
        raise CmuxUnreachable(f"{binary} workspace list exited {proc.returncode}: "
                              f"{(proc.stderr or proc.stdout or '').strip()[:160]}")
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise CmuxUnreachable(f"{binary} workspace list returned no JSON: {exc}")
    if not isinstance(payload, dict) or "workspaces" not in payload:
        # Exit 0 with no list is a soft cmux failure, not an empty workspace list; saying "tab is
        # not open" here would be a misreport.
        raise CmuxUnreachable(f"{binary} workspace list returned no workspace list")
    return list(payload.get("workspaces") or [])


def surface_lookup(title: str) -> tuple[str, str]:
    """``(ref, problem)``: the ref of the fixed surface, or ``""`` and why it could not be found.
    An empty problem with an empty ref means cmux answered and the tab is genuinely not open."""
    try:
        return surface_ref(title), ""
    except CmuxUnreachable as exc:
        return "", f"could not reach cmux: {exc}"


def surface_failure(title: str, problem: str) -> str:
    """The user-facing failure line for a surface that could not be reached or is not open."""
    if problem:
        return f"{problem}; nothing was sent"
    return f"{title} tab is not open in cmux; open it and send again"


def surface_ref(title: str) -> str:
    """cmux ref of the fixed surface whose title contains ``title`` (📨 Comms Triage, 🧭 CoS).
    Raises ``CmuxUnreachable`` when cmux cannot be asked."""
    for ws in cmux_workspaces():
        ws_title = str(ws.get("title") or "")
        if title == COS_TITLE:
            # "🧭 CoS" exactly; never a workstream tab whose name happens to contain the letters.
            if ws_title.strip() in {"🧭 CoS", "CoS"} or ws_title.strip().endswith(" CoS"):
                return str(ws.get("ref") or "")
        elif title in ws_title:
            return str(ws.get("ref") or "")
    return ""


def triage_workspace_ref() -> str:
    return surface_ref(TRIAGE_TITLE)


def guarded_send(root: Path, ref: str, text: str, wait: int = SEND_WAIT_SECONDS,
                 target: str = "", steer: bool = False) -> tuple[str, str]:
    """Type ``text`` into cmux tab ``ref`` through cmux-inject-guard. Returns (state, detail).

    ``steer=True`` is the owner's Send into a task's own thread (``cig_steer_send``): a running turn
    does not defer (Amp takes Enter as a steer), only a draft in the box or an unreadable screen
    does. Automation sends keep ``cig_safe_send``, which also defers while busy.

    cmux's exit status is not the delivery outcome (the guard's own note: 71 of 75 "unknown" rows
    had landed), so the screen is read back with ``cig_verify_sent`` after rc 0 and rc 3 alike.
    States: ``sent`` (the text is on the tab's screen), ``typed`` (rc 0 but the screen could not
    confirm it), ``unknown`` (rc 3, transport attempted: never retried), ``deferred`` (nothing typed).
    """
    guard = root / "system" / "scripts" / "lib" / "cmux-inject-guard.sh"
    where = target or ref
    fn = "cig_steer_send" if steer else "cig_safe_send"
    script = (f"source {shlex.quote(str(guard))} && {fn} {shlex.quote(ref)} {shlex.quote(text)} {wait}; "
              f"rc=$?; if [ $rc -eq 0 ] || [ $rc -eq 3 ]; then cig_verify_sent {shlex.quote(ref)} {shlex.quote(text)}; "
              f"echo \"verify=$?\"; fi; exit $rc")
    proc = subprocess.run(["bash", "-c", script], cwd=str(root), capture_output=True, text=True)
    verify = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("verify="):
            verify = line.split("=", 1)[1].strip()
    if proc.returncode == 2:
        if steer:
            return "deferred", f"{where} held a draft or could not be read for {wait}s; nothing typed"
        return "deferred", f"{where} busy or holding a draft for {wait}s; nothing typed"
    if verify == "0":
        return "sent", f"on {where}'s screen (verified)"
    if proc.returncode == 0:
        return "typed", f"typed into {where}; screen read-back could not confirm it"
    if verify == "2":
        return "failed", f"{where} did not receive it (composer empty, text absent); safe to send again"
    return "unknown", (f"transport to {where} returned rc {proc.returncode}; the text may have landed. "
                       f"Check the tab before sending again. {(proc.stderr or '').strip()[:160]}").strip()


def select_workspace(ref: str) -> tuple[bool, str]:
    """Bring cmux workspace ``ref`` to the front of cmux: after Send delivers into a task's thread, the app
    selects that workspace in cmux so the owner sees it. This changes
    which tab cmux shows; it never activates cmux or the app (no set-app-focus). Returns (ok, note)."""
    if not ref:
        return False, ""
    try:
        proc = subprocess.run([cmux_binary(), "select-workspace", "--workspace", ref], capture_output=True,
                              text=True, timeout=10,
                              env={**os.environ, "CMUX_QUIET": "1", "SWITCHBOARD_EXPLICIT_USER_FOCUS": "1"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not select {ref} in cmux ({exc})"
    if proc.returncode != 0:
        return False, f"could not select {ref} in cmux ({(proc.stderr or proc.stdout or '').strip()[:120]})"
    return True, f"{ref} selected in cmux"


def log_reply(root: Path, item_id: str, text: str, state: str, via: str, detail: str,
              extra: dict[str, Any] | None = None) -> None:
    argv = [sys.executable, str(SCRIPT_DIR / "cos_review_actions.py"), "reply-log", item_id,
            "--text", text, "--state", state, "--via", via, "--detail", detail]
    for key, flag in (("mode", "--mode"), ("thread_id", "--thread-id"),
                      ("continuation_task_id", "--continuation-task-id"), ("permalink", "--permalink"),
                      ("workspace_ref", "--workspace-ref")):
        value = str((extra or {}).get(key) or "").strip()
        if value:
            argv += [flag, value]
    subprocess.run(argv, cwd=str(root), capture_output=True, text=True)


def _normalise_reply(text: str, task_id: str = "") -> str:
    t = " ".join(text.split()).strip().rstrip(".!").lower()
    # Preset replies carry the task id as a prefix ("rt-123: go"); the decision is the part after it.
    if task_id and t.startswith(f"{task_id.lower()}:"):
        t = t[len(task_id) + 1:].strip()
    return t


def is_decision(text: str, options: list[dict[str, str]], task_id: str = "") -> bool:
    """Decision-shaped replies (go, veto, keep, hold, option B, ...), with or without the ``rt-…:`` prefix
    the presets add, or a preset whose label is a decision (Go / Hold / Keep / Veto / No vetoes /
    Option N / "(x) …"). A preset such as "send the draft as is" or "retry" is an instruction, not a
    decision, and goes to CoS; text still holding a ``<placeholder>`` is never a decision."""
    t = _normalise_reply(text, task_id)
    if PLACEHOLDER_RE.search(t):
        return False
    if DECISION_RE.match(t):
        return True
    for option in options:
        reply = _normalise_reply(str(option.get("reply") or option.get("label") or ""), task_id)
        label = " ".join(str(option.get("label") or "").split()).strip()
        if reply and reply == t and DECISION_LABEL_RE.match(label or reply):
            return True
    return False


def item_options(root: Path, item_id: str) -> list[dict[str, str]]:
    """Preset replies the feed offers for ``item_id`` (from the last built feed file) as
    ``{"label", "reply"}`` dicts, or []."""
    try:
        data = feed_mod.load_json(feed_mod.feed_path(root), {})
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    for section in feed_mod.SECTIONS:
        for item in data.get(section) or []:
            if isinstance(item, dict) and str(item.get("id")) == item_id:
                return [{"label": str(o.get("label") or ""), "reply": str(o.get("reply") or o.get("label") or "")}
                        for o in (item.get("options") or []) if isinstance(o, dict)]
    return []


def is_send_shaped(text: str) -> bool:
    t = " ".join(text.split()).strip().rstrip(".!").lower()
    t = re.sub(r"^send\s+\d+$", "send", t)
    return not t or t in SEND_SHAPED


def deliver(root: Path, item_id: str, text: str, *, dry_run: bool, allow_continue: bool,
            mode: str = "auto") -> dict[str, Any]:
    text = text.strip()
    if not text and not (mode == "do-now" and item_id.startswith("draft:")):
        raise ReplyError("empty reply")  # a draft Do now with no text posts the draft as saved
    if mode not in {"auto", "send", "decide", "do-now"}:
        raise ReplyError(f"--mode must be auto, send, decide or do-now, got {mode!r}")
    if item_id.startswith("task:"):
        task_id = item_id[len("task:"):].split(":", 1)[0]
        if mode == "auto":
            mode = "decide" if is_decision(text, item_options(root, item_id), task_id) else "send"
        if mode == "decide":
            return deliver_decision(root, item_id, task_id, text, dry_run=dry_run)
        if mode == "send":
            # Send goes to the thread where the task ran, never the CoS tab, where a busy tab could defer
            # it indefinitely. Live row: steer into that worker's tab. Terminal row: a continuation task in the owning workstream.
            # Only an item with no ledger row at all falls back to CoS, and says so.
            return deliver_task(root, item_id, task_id, text, dry_run=dry_run, allow_continue=True)
        low = " ".join(text.split()).lower().rstrip(".!")
        if low.startswith(f"{task_id}: "):
            low = low[len(task_id) + 2:]
        if low in {"re-dispatch", "redispatch"}:
            return redispatch_task(root, item_id, task_id, dry_run=dry_run)
        if low in {"force re-dispatch", "force redispatch"}:
            return redispatch_task(root, item_id, task_id, dry_run=dry_run, force=True)
        if low in {"drop it", "drop"}:
            return drop_task(root, item_id, task_id, dry_run=dry_run)
        return deliver_task(root, item_id, task_id, text, dry_run=dry_run, allow_continue=True)
    if item_id.startswith("draft:"):
        if mode == "do-now":
            return post_draft_now(root, item_id, text, dry_run=dry_run)
        return deliver_draft(root, item_id, text, dry_run=dry_run)
    if item_id.startswith("ask:"):
        return deliver_ask(root, item_id, text, dry_run=dry_run)
    raise ReplyError(f"item id must start with task:, draft: or ask:, got {item_id!r}")


def complete_item(root: Path, item_id: str, reply: str) -> None:
    """Mark the item complete; the reply itself is kept by the reply-log the caller writes."""
    del reply
    # --keep-draft: a delivered reply or a recorded decision closes the ask; only a Complete click drops
    # a parked draft.
    subprocess.run([sys.executable, str(SCRIPT_DIR / "cos_review_actions.py"), "--no-feed", "complete", "--keep-draft",
                    item_id], cwd=str(root), capture_output=True, text=True)


def deliver_decision(root: Path, item_id: str, task_id: str, text: str, *, dry_run: bool) -> dict[str, Any]:
    """Record go/veto/keep/option N on the ledger row and the owning CONTEXT.md; complete the item. No worker."""
    result: dict[str, Any] = {"item_id": item_id, "task_id": task_id, "via": "dispatch.py decide",
                              "needs_continuation": False}
    row, _decision = steer_route(task_id)
    if row and str(row.get("status") or "") in LIVE_STATUSES:
        # The worker has not asked yet; completing its future ask would hide the real question
        # The decision goes to the worker's own thread as a steer.
        return deliver_task(root, item_id, task_id, text, dry_run=dry_run, allow_continue=True)
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would record decision {text!r} on {task_id} and complete the item")
        return result
    out = run_json([sys.executable, str(SCRIPT_DIR / "dispatch.py"), "decide", "--task-id", task_id, "--by", "owner", text], root)
    if str(out.get("status")) != "decided":
        result.update(state="failed", detail=str(out.get("reason") or out.get("message") or "decide failed")[:300])
        return result
    complete_item(root, item_id, text)
    where = ", ".join(x for x in ("ledger row", "CONTEXT.md" if out.get("context") else "") if x)
    result.update(state="decided", detail=f"decision recorded on {where}; item completed. No worker dispatched.",
                  context=str(out.get("context") or ""))
    return result


def deliver_to_cos(root: Path, item_id: str, text: str, *, dry_run: bool) -> dict[str, Any]:
    """Type free text about a task into the 🧭 CoS tab; CoS owns what happens next."""
    ref, problem = surface_lookup(COS_TITLE)
    result: dict[str, Any] = {"item_id": item_id, "via": "cmux", "workspace_ref": ref, "needs_continuation": False}
    if item_id.startswith("task:"):
        result["task_id"] = item_id[len("task:"):].split(":", 1)[0]
        message = f"[COS REVIEW] re {result['task_id']}: {text}"
    else:
        message = f"[COS REVIEW] {text}"
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would type into 🧭 CoS ({ref or problem or 'tab not open'}): {message[:80]}")
        return result
    if not ref:
        result.update(state="failed", detail=surface_failure("🧭 CoS", problem))
        return result
    state, detail = guarded_send(root, ref, message, target="🧭 CoS tab")
    result.update(state=state, detail=detail)
    return result


def deliver_ask(root: Path, item_id: str, text: str, *, dry_run: bool) -> dict[str, Any]:
    """Answer a chat-only CoS ask: type the answer into the CoS tab and resolve the ask in the ledger."""
    ask_id = item_id[len("ask:"):]
    ref, problem = surface_lookup(COS_TITLE)
    result: dict[str, Any] = {"item_id": item_id, "ask_id": ask_id, "via": "cmux", "workspace_ref": ref,
                              "needs_continuation": False}
    message = f"[COS REVIEW] answer to {ask_id}: {text}"
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would type into 🧭 CoS ({ref or problem or 'tab not open'}) and resolve {ask_id}")
        return result
    if not ref:
        result.update(state="failed", detail=surface_failure("🧭 CoS", problem))
        return result
    state, detail = guarded_send(root, ref, message, target="🧭 CoS tab")
    if state in {"sent", "typed"}:
        subprocess.run([sys.executable, str(SCRIPT_DIR / "cos_review_ask_ledger.py"), "resolve", ask_id,
                        "--reply", text, "--by", "owner", "--no-feed"], cwd=str(root), capture_output=True, text=True)
        detail += f"; {ask_id} resolved in the ask ledger"
    result.update(state=state, detail=detail)
    return result


def redispatch_task(root: Path, item_id: str, task_id: str, *, dry_run: bool, force: bool = False) -> dict[str, Any]:
    """Stalled row (never launched, queued with no worker, no heartbeat): start a new attempt.
    Without ``force`` dispatch.py refuses (status ``stalled``) when the abandoned worktree holds
    uncommitted work; that evidence comes back as state ``needs-force`` so the app can show it and
    ask before "force re-dispatch" discards it."""
    router = root / "system" / "scripts" / "router-dispatch.sh"
    flag = "--force-redispatch" if force else "--redispatch"
    result: dict[str, Any] = {"item_id": item_id, "task_id": task_id, "via": f"router-dispatch {flag}",
                              "needs_continuation": False}
    row, _decision = steer_route(task_id)
    prompt = str((row or {}).get("prompt") or (row or {}).get("summary") or "").strip()
    if not row:
        result.update(state="failed", detail="no ledger row with that id")
        return result
    if not prompt:
        result.update(state="failed", detail="row has no prompt to re-dispatch")
        return result
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would run router-dispatch.sh --task-id {task_id} {flag}")
        return result
    argv = ["bash", str(router), "--origin", str(row.get("origin") or "cos"), "--task-id", task_id]
    if force:
        argv.append("--force-redispatch")
    out = run_json(argv + [prompt], root)
    status = str(out.get("status") or "failed")
    if status in {"routed", "queued", "already-live", "duplicate"}:
        result.update(state="redispatched", attempt_id=str(out.get("attempt_id") or ""),
                      detail=f"{status}: new attempt {out.get('attempt_id') or ''} ({out.get('message') or ''})".strip())
        return result
    if status == "stalled" and not force:
        evidence = out.get("reason") or out.get("evidence") or out.get("message") or "uncommitted work in the abandoned worktree"
        result.update(state="needs-force", evidence=str(evidence)[:400],
                      detail=f"not re-dispatched: {str(evidence)[:200]}. Reply 'force re-dispatch' to discard it and start again.")
        return result
    result.update(state="failed", detail=str(out.get("reason") or out.get("message") or status)[:300])
    return result


def drop_task(root: Path, item_id: str, task_id: str, *, dry_run: bool) -> dict[str, Any]:
    """Stalled row the owner does not want: cancel it in the ledger and complete the item."""
    result: dict[str, Any] = {"item_id": item_id, "task_id": task_id, "via": "running-task-state.py update",
                              "needs_continuation": False}
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would mark {task_id} cancelled and complete the item")
        return result
    out = run_json([sys.executable, str(SCRIPT_DIR / "running-task-state.py"), "update", "--task-id", task_id,
                    "--status", "cancelled", "--summary", "Dropped from CoS Review (the owner: drop it)"], root)
    if out.get("_live_worker"):
        result.update(state="failed", detail=f"{task_id} has a live worker in cmux; steer it (Do now with your text) instead of dropping it")
        return result
    if out.get("_rc") not in (0, None):
        result.update(state="failed", detail=str(out.get("reason") or out.get("message") or "cancel refused")[:300])
        return result
    complete_item(root, item_id, "drop it")
    result.update(state="decided", detail=f"{task_id} cancelled in the ledger; item completed")
    return result


def post_draft_now(root: Path, item_id: str, text: str, *, dry_run: bool) -> dict[str, Any]:
    """Do now on a draft: post it to Slack as the owner through send-triage-draft.py (robot prefix applied there)."""
    draft_id = "draft-" + item_id[len("draft:"):]
    result: dict[str, Any] = {"item_id": item_id, "via": "send-triage-draft.py", "needs_continuation": False}
    if not is_send_shaped(text):
        # "make it shorter" is not a send: the draft that would post is not the text the owner typed.
        result.update(state="failed", detail="reply is not a send: Do now posts the draft as saved. Use Send to give "
                                             "Comms Triage that instruction, or clear the reply and press Do now.")
        return result
    edit = saved_draft_edit(root, item_id)
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would post {draft_id} to Slack through send-triage-draft.py"
                      + ("; saved edit would replace the ledger text first" if edit.strip() else ""))
        return result
    note = ""
    if edit.strip():
        problem = push_draft_edit(draft_id, edit)
        note = (" Saved edit stored first." if not problem else f" Saved edit NOT stored ({problem}); ledger text posted.")
    proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "send-triage-draft.py"), "--draft-id", draft_id,
                          "--by", "owner", "--json"], cwd=str(root), capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout or "{}")
    except ValueError:
        report = {}
    if proc.returncode != 0 or not report.get("sent"):
        reason = (proc.stderr or proc.stdout or "send refused").strip().replace("⛔ ", "")[:300]
        result.update(state="failed", detail=reason + note)
        return result
    permalink = str(report.get("permalink") or "")
    result.update(state="sent", permalink=permalink, send_id=str(report.get("send_id") or ""),
                  detail=f"posted to Slack as the owner" + (f": {permalink}" if permalink else "") + note)
    return result


def steer_route(task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Where dispatch.py would send a steer for ``task_id`` (ledger row, decision), without dispatching."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch", SCRIPT_DIR / "dispatch.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(module)
    row = module.ledger.find_task(module.ledger.load_payload(), task_id)
    return row, module.steer_decision(row, module.ledger.live_agents())


def deliver_task(root: Path, item_id: str, task_id: str, text: str, *, dry_run: bool,
                 allow_continue: bool) -> dict[str, Any]:
    """Deliver ``text`` into the thread where ``task_id`` ran. Live row: a steer typed into that
    worker's cmux tab (``cig_steer_send``: a running turn takes it as a steer, a draft in the box
    defers). Terminal row: ``router-dispatch.sh --steer`` opens a continuation task in the owning
    workstream. Either way the delivered-to workspace is then selected in cmux so the owner sees it
    (``selected_workspace``; skipped on dry run). No ledger row at all: the text goes to the 🧭 CoS
    tab and the detail says so."""
    router = root / "system" / "scripts" / "router-dispatch.sh"
    result: dict[str, Any] = {"item_id": item_id, "task_id": task_id, "via": "router-dispatch --steer"}
    row, decision = steer_route(task_id)
    result.update(thread_id=str(decision.get("thread_id") or (row or {}).get("thread_id") or ""),
                  workspace_ref=str(decision.get("workspace_ref") or (row or {}).get("workspace_ref") or ""),
                  needs_continuation=decision["action"] == "continuation")
    if decision["action"] == "unknown-task":
        fallback = deliver_to_cos(root, item_id, text, dry_run=dry_run)
        verb = "would go to" if dry_run else ("sent to" if fallback.get("state") in {"sent", "typed"} else "tried")
        fallback["detail"] = f"no ledger row for {task_id}; {verb} 🧭 CoS instead: {fallback.get('detail') or ''}".strip()
        fallback.update(via="cmux (CoS fallback)", fallback="cos", thread_id="", needs_continuation=False)
        return fallback
    if dry_run:
        where = f"thread {result['thread_id'] or '<unbound>'} in {result['workspace_ref'] or '<no tab>'}" \
            if decision["action"] == "message" else f"a continuation in {(row or {}).get('workstream') or 'its workstream'}"
        result.update(state="dry-run",
                      detail=f"dry run: {decision['action']} → {where} "
                             f"({decision.get('reason') or decision.get('status') or 'unknown task'}); "
                             f"would then select that workspace in cmux")
        return result
    if decision["action"] == "continuation" and not allow_continue:
        # A reaped thread never silently spawns a worker from a reply box; the app offers the button.
        result.update(state="failed",
                      detail=f"thread not live: row is {decision['status']} and its tab is reaped. "
                             f"Dispatch a continuation to deliver this reply.")
        return result
    out = run_json(["bash", str(router), "--origin", "cos", "--steer", task_id, text], root)
    status = str(out.get("status") or "failed")
    if status == "delivered":
        # A live Claude worker: dispatch.py delivered the steer itself by cmux agent
        # message, so nothing is typed into the tab.
        ref = str(out.get("workspace_ref") or "")
        result.update(state="sent", via="cmux agent message", workspace_ref=ref, needs_continuation=False,
                      thread_id=str(out.get("thread_id") or result.get("thread_id") or ""),
                      detail=str(out.get("message") or "steer delivered by cmux agent message"))
        if ref:
            _select_after_delivery(result, ref)
        return result
    if status == "message":
        thread_id = str(out.get("thread_id") or "")
        ref = str(out.get("workspace_ref") or "")
        result.update(thread_id=thread_id, workspace_ref=ref, needs_continuation=False)
        if out.get("ambiguous"):
            # cmux timed out after dispatch.py's agent message: it may already be queued,
            # so typing it again could deliver the steer twice.
            result.update(state="failed", detail=str(out.get("message") or "steer may already be queued; nothing re-sent"))
            return result
        if not out.get("thread_live") or not ref:
            result.update(state="failed", detail=f"thread {thread_id or '<unbound>'} is not live in cmux right now "
                                                 f"(row is {(row or {}).get('status') or 'live'}); nothing sent")
            return result
        state, detail = guarded_send(root, ref, text, target=f"thread {thread_id or ref}'s tab {ref}", steer=True)
        result.update(state=state, detail=detail, via="cmux steer")
        if state in {"sent", "typed"}:
            _select_after_delivery(result, ref)
        return result
    if status in {"routed", "queued", "duplicate", "already-live"}:
        new_id = str(out.get("task_id") or "")
        new_ref = str(out.get("workspace_ref") or "")
        if new_id:
            complete_item(root, item_id, f"continued as {new_id}")
        result.update(state="continuation", continuation_task_id=new_id, needs_continuation=False,
                      workspace_ref=new_ref or result.get("workspace_ref") or "",
                      detail=f"{status}: continuation {new_id} carries this reply ({out.get('message') or ''})".strip())
        if new_ref:
            _select_after_delivery(result, new_ref)
        elif status == "queued":
            result["detail"] += "; no tab yet to select"
        return result
    result.update(state="failed", needs_continuation=False,
                  detail=str(out.get("reason") or out.get("message") or status)[:300])
    return result


def _select_after_delivery(result: dict[str, Any], ref: str) -> None:
    ok, note = select_workspace(ref)
    result["selected_workspace"] = ref if ok else ""
    if note:
        result["detail"] = f"{result.get('detail') or ''}; {note}".strip("; ")


def saved_draft_edit(root: Path, item_id: str) -> str:
    """The draft text the owner saved in the app for ``item_id``, or "" when the owner left it alone."""
    data = feed_mod.load_json(feed_mod.actions_path(root), {})
    drafts = data.get("drafts") if isinstance(data, dict) else None
    entry = drafts.get(item_id) if isinstance(drafts, dict) else None
    return str(entry.get("text") or "") if isinstance(entry, dict) else ""


def push_draft_edit(draft_id: str, text: str) -> str:
    """Store the app's saved edit in the triage ledger so ``send N`` posts the edited words, not the
    original. Returns "" on success or the reason it could not."""
    try:
        import triage_ledger  # sibling script; sys.path has SCRIPT_DIR
        state = triage_ledger.load_state()
        current = state["items"][triage_ledger._resolve_draft_ref(state, draft_id)]
        if str(current.get("draft_text") or "").strip() == text.strip():
            return ""
        triage_ledger.update_draft_text(draft_id, text, "owner")
        return ""
    except Exception as exc:  # the send still goes; the state line says which text posts
        return str(exc)[:200]


def deliver_draft(root: Path, item_id: str, text: str, *, dry_run: bool) -> dict[str, Any]:
    ref, problem = surface_lookup(TRIAGE_TITLE)
    result: dict[str, Any] = {"item_id": item_id, "via": "cmux", "workspace_ref": ref, "needs_continuation": False}
    draft_id = "draft-" + item_id[len("draft:"):]
    edit = saved_draft_edit(root, item_id)
    pushes_edit = bool(edit.strip()) and text.split()[:1] == ["send"]
    if dry_run:
        result.update(state="dry-run", detail=f"dry run: would type into {TRIAGE_TITLE} ({ref or problem or 'tab not open'})"
                      + ("; the saved edit would replace the ledger text first" if pushes_edit else ""))
        return result
    if not ref:
        result.update(state="failed", detail=surface_failure(TRIAGE_TITLE, problem))
        return result
    note = ""
    if pushes_edit:
        problem = push_draft_edit(draft_id, edit)
        note = (" Saved edit stored in the ledger; that text posts." if not problem
                else f" Saved edit NOT stored ({problem}); the original ledger text posts.")
    # "sent" here means the command reached the Comms Triage agent, which posts under its own rules;
    # the item leaves Needs you when the ledger flips and the feed rebuilds.
    state, detail = guarded_send(root, ref, text, target=f"{TRIAGE_TITLE} tab")
    result.update(state=state, detail=detail + note)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("item_id")
    parser.add_argument("--text", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue", dest="allow_continue", action="store_true",
                        help="dispatch a continuation when the owning row is terminal")
    parser.add_argument("--no-log", action="store_true", help="do not record the attempt in the actions file")
    parser.add_argument("--mode", default="auto", choices=["auto", "send", "decide", "do-now"],
                        help="decide = record on ledger + CONTEXT, no worker; send = to the owning surface; "
                             "do-now = dispatch/post immediately; auto picks decide for decision-shaped text")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or bool(os.environ.get("COS_REVIEW_REPLY_DRY_RUN"))
    root = feed_mod.repo_root()
    try:
        result = deliver(root, args.item_id, args.text, dry_run=dry_run, allow_continue=args.allow_continue,
                         mode=args.mode)
    except ReplyError as exc:
        print(json.dumps({"state": "failed", "detail": str(exc)}), file=sys.stderr)
        return 2
    result.setdefault("mode", args.mode)
    if not args.no_log:
        log_reply(root, args.item_id, args.text.strip(), result["state"], str(result.get("via") or ""),
                  str(result.get("detail") or ""), result)
    print(json.dumps(result))
    return 0 if result["state"] in OK_STATES else 1


if __name__ == "__main__":
    sys.exit(main())
