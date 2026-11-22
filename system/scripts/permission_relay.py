#!/usr/bin/env python3
"""permission_relay.py — a worker or owner tab stuck on a prompt reaches CoS.

Destructive deletes in workers raised prompts the owner often missed. Option 2, approved with option 1 (the Trash rewrite in
pm-os-guardrails Rule 3): any permission prompt that is left goes to the live
Claude CoS as one direct callback, with the tab, the exact command, and how to
answer. `allow` and `deny` work through the steer path.

  permission_relay.py notify                Claude Notification hook (stdin JSON).
      Only `permission_prompt` and `elicitation_dialog`. Detaches `send` and
      returns at once.
  permission_relay.py send --payload FILE [--force] [--dry-run]
      Builds and sends the callback. A worker row is sent at once; an owner tab
      waits OWNER_DELAY seconds first. Either way the tab's screen must still
      show the prompt (--force skips that check, for the live test). CoS,
      Launchpad, Todo, and Comms Triage tabs are skipped: the owner works there.
  permission_relay.py amp-confirm --rule R --title T --detail D --timeout-ms N
      The Amp runtime's guardrail modal (pm-os-guardrails confirm()). Amp's
      modal cannot be pressed from outside, so the callback says the owner answers in
      the tab before the guardrail's timeout declines it.
  permission_relay.py sweep [--dry-run]     dispatch.py sweep: every live worker tab that shows a
                                            dialog, for tabs opened before the hook existed.
  permission_relay.py answer (--workspace-ref R | --task-id ID) [--auto] allow|deny
      Presses the Claude permission dialog in that tab: Enter on "1. Yes" for
      allow, Escape for deny. Refuses unless the screen shows the dialog with
      the cursor on "1. Yes". `router-dispatch.sh --steer <rt-id> allow|deny`
      calls this for a row (dispatch.py command_steer); `--steer <rt-id>
      auto-allow` is the --auto form.
  permission_relay.py auto-answers [--unreported] [--mark-reported]
      The prompts CoS allowed on its own, for its next reply to the owner.

Two cases: every 🔐 callback
carries a `Case:` line from permission_case.py. `own-task-files` (the only
destructive part deletes or overwrites files the task made) lets CoS answer
`auto-allow` without the owner; `--auto` re-checks the files at press time, refuses
any other prompt, and logs the answer under `auto_answers` for CoS's next
reply. Every other prompt is `ask`: CoS asks the owner, as before.

The exact command and the guardrail's reason come from the record amp-bridge.ts
writes when a guardrail asks (system/state/worker-dispatch/permission-asks/
<session>.json), else from the newest tool call with no result in the session
transcript or one of its subagent transcripts (<session>/subagents/agent-*.jsonl).
The screen only confirms which one is up: Claude's TUI has no scrollback, so a
command taller than the tab shows only its last lines, and CoS would get only
"Prompt: Do you want to proceed?".

State: system/state/worker-dispatch/permission-relay.json (dedupe and the
pending prompt per tab). Log: system/state/logs/permission-relay.log.
Kill switches: PERMISSION_RELAY_DISABLED=1, or {"permission-relay.disabled":
true} in ~/.claude/pm-os-plugin-config.json. Tests: test_permission_relay.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import flock_compat as fcntl

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import owner_tabs  # noqa: E402
import permission_case  # noqa: E402

try:
    from lib.mux import current_workspace_id  # noqa: E402
except Exception:  # noqa: BLE001  (a copy without lib/ keeps the cmux-only lookup)
    def current_workspace_id(timeout: float = 10.0) -> str:
        return os.environ.get("CMUX_WORKSPACE_ID", "")

STATE_DIR = owner_tabs.STATE_DIR
REPO_ROOT = owner_tabs.REPO_ROOT
CONFIG_PATH = Path(os.environ.get("PM_OS_PLUGIN_CONFIG") or Path.home() / ".claude" / "pm-os-plugin-config.json")
OWNER_DELAY = float(os.environ.get("PERMISSION_RELAY_OWNER_DELAY", "20"))
ASK_FRESH_SECONDS = 120     # a record with no screen to check it against
ASK_ON_SCREEN_SECONDS = 3600  # a record whose command is still in the dialog
DEDUPE_SECONDS = 600
RELAY_TYPES = {"permission_prompt", "elicitation_dialog"}
# the owner works in these tabs; a prompt there is already in front of the owner.
OPERATOR_TITLES = re.compile(r"(🧭|\bcos\b|megadesk|launchpad|comms triage|\btodo\b)", re.I)
SENDER = owner_tabs.REPORT_SENDER

# Claude Code's permission dialog: "Do you want to proceed?" (or "make this edit",
# "create", "allow") above a numbered list whose first option is "1. Yes".
PROMPT_RE = re.compile(r"Do you want to [^\n]*\?", re.I)
YES_RE = re.compile(r"^\s*[│|]?\s*(?:[❯>›]\s*)?1\.\s*Yes\b", re.M)
# The cursor on a bare "1. Yes" line, never "1. Yes, and don't ask again…".
YES_SELECTED_RE = re.compile(r"^\s*[│|]?\s*[❯>›]\s*1\.\s*Yes\s*[│|]?\s*$", re.M)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(event: dict[str, Any]) -> None:
    path = STATE_DIR / "logs" / "permission-relay.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now(), **event}, sort_keys=True) + "\n")
    except OSError:
        pass


def disabled() -> bool:
    if os.environ.get("PERMISSION_RELAY_DISABLED") == "1":
        return True
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("permission-relay.disabled") is True
    except (OSError, ValueError, AttributeError):
        return False


# --- state ---------------------------------------------------------------------------------

def _state_path() -> Path:
    return STATE_DIR / "worker-dispatch" / "permission-relay.json"


def _mutate(fn) -> dict[str, Any]:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("sent", {})
        state.setdefault("pending", {})
        result = fn(state)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return result if isinstance(result, dict) else {}


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sent": {}, "pending": {}}


# --- cmux ----------------------------------------------------------------------------------

def read_screen(ref: str) -> str | None:
    try:
        result = subprocess.run([*owner_tabs._cmux_argv(), "read-screen", "--workspace", ref, "--lines", "60"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def send_key(ref: str, key: str) -> bool:
    try:
        return subprocess.run([*owner_tabs._cmux_argv(), "send-key", "--workspace", ref, key],
                              capture_output=True, text=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prompt_visible(screen: str | None) -> bool:
    """A dialog sits at the bottom of the tab. Only the last 25 lines count, so a
    transcript that merely prints dialog text higher up is not a prompt."""
    tail = "\n".join((screen or "").rstrip("\n").splitlines()[-25:])
    return bool(PROMPT_RE.search(tail)) and bool(YES_RE.search(tail))


def _flat(text: str) -> str:
    return re.sub(r"[\s│|]+", "", text or "")


def command_on_screen(command: str, screen: str | None) -> bool:
    """True when the relayed command shows in the dialog: the start of its first
    line, or the end of its last line for a command taller than the tab, whose
    top has scrolled off. Whitespace is ignored because the dialog wraps and
    indents long commands."""
    lines = [line for line in (command or "").splitlines() if line.strip()]
    if not lines or not screen:
        return False
    flat = _flat(screen)
    first, last = _flat(lines[0])[:40], _flat(lines[-1])[-40:]
    return (len(first) >= 4 and first in flat) or (len(lines) > 1 and len(last) >= 8 and last in flat)


def find_tab(workspace_id: str = "", ref: str = "") -> dict[str, Any]:
    for ws in owner_tabs.cmux_workspaces():
        if (ref and ws.get("ref") == ref) or (workspace_id and str(ws.get("id") or "").upper() == workspace_id.upper()):
            return ws
    return {}


def is_operator_tab(ws: dict[str, Any]) -> bool:
    """CoS, Launchpad, Todo, and Comms Triage: the owner already sees a prompt there."""
    if not ws:
        return False
    title = owner_tabs.tab_title(ws)
    if OPERATOR_TITLES.search(title) and not owner_tabs.is_worker_tab(ws):
        return True
    reg = owner_tabs.read_registration()
    return bool(reg.get("workspace_id")) and str(reg.get("workspace_id")).upper() == str(ws.get("id") or "").upper()


# --- the ask -------------------------------------------------------------------------------

def _ledger_rows() -> list[dict[str, Any]]:
    try:
        return [t for t in owner_tabs._ledger().load_payload().get("tasks", []) if isinstance(t, dict)]
    except Exception:  # noqa: BLE001
        return []


def live_row(session_id: str = "", task_id: str = "", ref: str = "") -> dict[str, Any]:
    for row in _ledger_rows():
        if row.get("status") not in {"routing", "active"} or str(row.get("id") or "").startswith("wr-"):
            continue
        if task_id and row.get("id") == task_id:
            return row
        if session_id and session_id in {str(row.get("session_id") or ""), str(row.get("thread_id") or "")}:
            return row
        if ref and row.get("workspace_ref") == ref:
            return row
    return {}


def recorded_ask(session_id: str, now: float | None = None, screen: str | None = None) -> dict[str, Any]:
    """The guardrail ask amp-bridge.ts recorded for this session. With a screen, the
    record counts while its command is still in the dialog (up to an hour), so the
    sweep, which runs every 10 minutes, still gets the guardrail's reason; a record
    for another command is ignored. With no screen, only a fresh record counts."""
    if not session_id:
        return {}
    path = STATE_DIR / "worker-dispatch" / "permission-asks" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.json"
    try:
        ask = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(ask.get("at")).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, TypeError):
        return {}
    age = (now or time.time()) - at
    if screen is not None:
        return ask if age <= ASK_ON_SCREEN_SECONDS and command_on_screen(str(ask.get("command") or ""), screen) else {}
    return ask if age <= ASK_FRESH_SECONDS else {}


def _open_calls(path: Path) -> list[dict[str, Any]]:
    """Tool calls in one transcript file that have no result yet, oldest first."""
    pending: dict[str, dict[str, Any]] = {}
    done: set[str] = set()   # Claude may write a tool_use row again after its result
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                content = (row.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        if str(block.get("id")) in done:
                            continue
                        data = block.get("input") or {}
                        name = str(block.get("name") or "")
                        command = str(data.get("command")) if name == "Bash" and "command" in data else json.dumps(data)[:4000]
                        pending.pop(str(block.get("id")), None)
                        pending[str(block.get("id"))] = {"tool": name, "command": command, "at": str(row.get("timestamp") or ""),
                                                         **({"cwd": str(row["cwd"])} if row.get("cwd") else {})}
                    elif block.get("type") == "tool_result":
                        done.add(str(block.get("tool_use_id")))
                        pending.pop(str(block.get("tool_use_id")), None)
    except OSError:
        return []
    return list(pending.values())


def _subagent_label(path: Path) -> str:
    try:
        meta = json.loads(path.with_name(path.name.replace(".jsonl", ".meta.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    kind = str(meta.get("agentType") or "") or "a"
    about = str(meta.get("description") or "").strip()
    return f"the {kind} subagent" + (f" ({about[:80]})" if about else "")


def pending_tool_calls(path: str) -> list[dict[str, Any]]:
    """Every open tool call in a session transcript and its subagent transcripts,
    newest first. A subagent's call carries `agent`, the subagent's label."""
    if not path:
        return []
    main = Path(path)
    files: list[tuple[Path, str]] = [(main, "")]
    files += [(p, _subagent_label(p)) for p in sorted(main.with_suffix("").glob("subagents/*.jsonl"))]
    calls: list[tuple[str, float, int, dict[str, Any]]] = []
    for file, agent in files:
        try:
            mtime = file.stat().st_mtime
        except OSError:
            continue
        for seq, call in enumerate(_open_calls(file)):
            # A row with no timestamp sorts by its file's last write.
            at = call.pop("at") or datetime.fromtimestamp(mtime, timezone.utc).isoformat().replace("+00:00", "Z")
            calls.append((at, mtime, seq, {**call, **({"agent": agent} if agent else {})}))
    calls.sort(key=lambda c: c[:3], reverse=True)
    return [c[3] for c in calls]


def transcript_tool_call(path: str, screen: str | None = None) -> dict[str, Any]:
    """The open tool call the dialog is for: the newest whose command shows on
    `screen`, else the newest that is not an Agent or Task launch (a background
    subagent's own call is newer than the launch that started it)."""
    calls = pending_tool_calls(path)
    if screen:
        for call in calls:
            if command_on_screen(call["command"], screen):
                return call
    return next((c for c in calls if c["tool"] not in {"Agent", "Task"}), calls[0] if calls else {})


def reason_line(reason: str, command: str = "") -> str:
    """One line from a guardrail ask: its title ("pm-os-guardrails: confirm rm -rf")
    and the question under it ("Allow destructive delete of $S/st, $S/hang?"),
    without the command the record already repeats."""
    head = _flat(next((line for line in command.splitlines() if line.strip()), ""))[:40]
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", reason or "") if p.strip()]
    paras = [p for p in paras if not (head and _flat(p).startswith(head))][:2]
    if len(paras) == 2 and not paras[0].endswith((".", "?", "!", ":")):
        paras[0] += "."
    return " ".join(paras)[:300]


def prompt_case(row: dict[str, Any], tool: str, command: str, kind: str, cwd: str = "") -> dict[str, Any]:
    """The Case line's verdict. Only a Claude permission dialog in a task's tab can be
    own-task-files: a question dialog is the owner's, and an owner tab with no row has no
    start time to prove a file is the task's."""
    if kind != "permission_prompt" or not row:
        return {"case": permission_case.ASK, "targets": [],
                "reason": "a question dialog" if kind == "elicitation_dialog" else "no task row for this tab"}
    try:
        return permission_case.classify(tool, command, row=row, cwd=cwd)
    except Exception as error:  # noqa: BLE001  (fail closed: the owner decides)
        return {"case": permission_case.ASK, "targets": [], "reason": f"the case check failed ({error})"}


def build_message(*, tab: dict[str, Any], row: dict[str, Any], tool: str, command: str, reason: str,
                  kind: str, amp_timeout_s: int = 0, agent: str = "", guardrail_reason: str = "",
                  case: dict[str, Any] | None = None) -> str:
    title = owner_tabs.tab_title(tab) or "a tab"
    ref = str(tab.get("ref") or row.get("workspace_ref") or "")
    slug = str(row.get("workstream") or "")
    who = f"{title} ({ref})" if ref else title
    if row:
        name = owner_tabs.owner_name(slug) if slug else slug
        who = f"{title}, the {name} worker for {row.get('id')} ({ref})"
    lines = [f"[ROUTER REPORT] 🔐 {who} is waiting on a {'question' if kind == 'elicitation_dialog' else 'permission prompt'}."]
    if agent:
        lines.append(f"Asked by: {agent}, not the main session.")
    if guardrail_reason:
        lines.append(f"Guardrail reason: {reason_line(guardrail_reason, command)}")
    elif reason:
        lines.append(f"Prompt: {reason.splitlines()[0][:300]}")
    if command:
        body = command if len(command) <= 2500 else command[:2500] + "\n… (cut; the full command is in the tab)"
        lines += [f"{tool or 'Tool'} call, verbatim:", "```", body, "```"]
    elif kind == "permission_prompt":
        lines.append(f"Command: not found in the guardrail record or the session and subagent transcripts. "
                     f"Read the tab ({ref}) before asking the owner.")
    own = bool(case) and case.get("case") == permission_case.OWN and not amp_timeout_s and kind == "permission_prompt"
    if own:
        lines.append(f"Case: own task files. The only destructive part deletes or overwrites files this task made "
                     f"({permission_case.summary(case)}).")
    else:
        lines.append(f"Case: the owner decides ({(case or {}).get('reason') or 'not shown to be own task files'}).")
    if own and row:
        lines.append(f"How to answer: CoS allows it now, without asking the owner: `bash system/scripts/router-dispatch.sh "
                     f"--origin cos --steer {row.get('id')} auto-allow`. The relay re-checks the files, presses Yes, "
                     f"and logs it. Tell the owner in your next reply what you allowed.")
        lines.append("Do not wait for the owner on this one; report the auto-allow in your next reply.")
        return "\n".join(lines)
    if amp_timeout_s:
        lines.append(f"How to answer: the owner presses Allow or Cancel in the tab ({ref}) within about {amp_timeout_s} s. "
                     f"After that the guardrail declines on its own and the worker goes on without it. "
                     f"An Amp modal cannot be pressed from CoS.")
    elif kind == "elicitation_dialog":
        lines.append(f"How to answer: the owner answers in the tab ({ref}); a question dialog cannot be answered from CoS.")
    elif row:
        lines.append(f"How to answer: steer allow or deny, `bash system/scripts/router-dispatch.sh --origin cos "
                     f"--steer {row.get('id')} allow` (or `deny`), or the owner presses it in the tab ({ref}). "
                     f"Deny stops the worker's turn; follow it with a steer that says what to do instead.")
    else:
        lines.append(f"How to answer: `python3 system/scripts/permission_relay.py answer --workspace-ref {ref} allow` "
                     f"(or `deny`), or the owner presses it in the tab ({ref}).")
    lines.append("Show the owner the command and ask allow or deny; never answer it without their word.")
    return "\n".join(lines)


def _cos_ref() -> str:
    target = owner_tabs.cos_tab(owner_tabs.cmux_workspaces(), owner_tabs.agents_by_workspace(),
                                owner_tabs.configured_agent())
    return str(target.get("ref") or "")


def deliver(text: str, dedupe_key: str, dry_run: bool = False, dedupe_seconds: float = DEDUPE_SECONDS) -> dict[str, Any]:
    now = time.time()
    seen = _read_state().get("sent", {}).get(dedupe_key)
    if seen and now - float(seen) < dedupe_seconds:
        return {"sent": False, "reason": "already sent for this prompt"}
    if dry_run:
        return {"sent": False, "dry_run": True, "text": text}
    ref = _cos_ref()
    if not ref:
        return {"sent": False, "reason": "no CoS tab open"}
    out = owner_tabs.agent_message(ref, text, SENDER)
    if out.get("ok"):
        _mutate(lambda s: s["sent"].__setitem__(dedupe_key, now))
    return {"sent": bool(out.get("ok")), "cos_ref": ref, **({"reason": out.get("reason")} if not out.get("ok") else {})}


def send(payload: dict[str, Any], force: bool = False, dry_run: bool = False,
         dedupe_seconds: float = DEDUPE_SECONDS) -> dict[str, Any]:
    kind = str(payload.get("notification_type") or "permission_prompt")
    session_id = str(payload.get("session_id") or "")
    workspace_id = str(payload.get("workspace_id") or "")
    workspace_ref = str(payload.get("workspace_ref") or "")
    tab = find_tab(workspace_id=workspace_id, ref=workspace_ref) if (workspace_id or workspace_ref) else {}
    if not tab and not force:
        return {"sent": False, "reason": "not in a cmux tab"}
    if is_operator_tab(tab):
        return {"sent": False, "reason": f"operator tab {owner_tabs.tab_title(tab)!r}: the owner sees the prompt there"}
    ref = str(tab.get("ref") or "")
    row = live_row(session_id=session_id, task_id=str(payload.get("task_id") or ""), ref=ref)
    if not row and not force:
        time.sleep(OWNER_DELAY)   # an owner tab: give the owner a moment if they are looking at it
    screen = read_screen(ref) if ref else None
    if kind == "permission_prompt" and not force and not prompt_visible(screen):
        return {"sent": False, "reason": "the prompt is no longer on screen"}
    ask = recorded_ask(session_id, screen=screen if prompt_visible(screen) else None)
    transcript = str(payload.get("transcript_path") or "") or transcript_for_session(session_id)
    call = transcript_tool_call(transcript, screen=screen) if not ask else {}
    tool = str(ask.get("tool") or call.get("tool") or "")
    command = str(ask.get("command") or call.get("command") or "")
    agent = str(call.get("agent") or "") or (f"the {ask['agent_type']} subagent" if ask.get("agent_type") else "")
    reason = str(payload.get("message") or "")
    cwd = str(ask.get("cwd") or call.get("cwd") or "")
    case = prompt_case(row, tool, command, kind, cwd=cwd)
    text = build_message(tab=tab, row=row, tool=tool, command=command, reason=reason, kind=kind,
                         agent=agent, guardrail_reason=str(ask.get("reason") or ""), case=case)
    note = str(payload.get("note") or "").strip()
    if note:   # a smoke test labels itself so CoS does not put it to the owner as a real ask
        text = text.replace("\n", f"\nNote: {note}\n", 1)
    # One key per tab and command: the Notification hook and the sweep see the
    # same prompt with different wording, and CoS hears it once.
    key = hashlib.sha256(f"{ref}|{command}".encode()).hexdigest()[:16]
    result = deliver(text, key, dry_run=dry_run, dedupe_seconds=dedupe_seconds)
    if result.get("sent"):
        _mutate(lambda s: s["pending"].__setitem__(ref or session_id, {
            "task_id": row.get("id", ""), "session_id": session_id, "tool": tool, "command": command[:8000],
            "command_len": len(command), "kind": kind, "key": key, "at": _now(), "cwd": cwd,
            "case": case.get("case", "")}))
    _log({"event": "send", "ref": ref, "task_id": row.get("id", ""), "kind": kind, "case": case.get("case", ""),
          **{k: v for k, v in result.items() if k != "text"}})
    return {**result, "ref": ref, "task_id": row.get("id", ""), "case": case.get("case", "")}


def transcript_for_session(session_id: str) -> str:
    """The Claude transcript for a session id (~/.claude/projects/*/<id>.jsonl), newest first."""
    if not re.fullmatch(r"[A-Za-z0-9-]{8,}", session_id or ""):
        return ""
    found = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(found[0]) if found else ""


def sweep(rows: list[dict[str, Any]] | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """Catch permission dialogs the Notification hook missed. Claude loads
    hooks.json only at session start, so a tab opened before the Notification hook
    landed never fires it. dispatch.py sweep (every command-center tick) calls this:
    each live worker row whose tab shows a permission dialog gets the same 🔐 callback,
    with the command read from the session transcript. Deduped with the hook by tab
    and command for an hour, so a prompt that stays up is not repeated every tick."""
    if disabled():
        return []
    out: list[dict[str, Any]] = []
    for row in _ledger_rows() if rows is None else rows:
        ref = str(row.get("workspace_ref") or "")
        if row.get("status") not in {"routing", "active"} or not ref.startswith("workspace:"):
            continue
        screen = read_screen(ref)
        if not prompt_visible(screen):
            continue
        session = str(row.get("session_id") or row.get("thread_id") or "")
        match = PROMPT_RE.search(screen or "")
        payload = {"notification_type": "permission_prompt", "session_id": session, "workspace_ref": ref,
                   "task_id": str(row.get("id") or ""), "transcript_path": transcript_for_session(session),
                   "message": match.group(0) if match else ""}
        result = send(payload, force=True, dry_run=dry_run, dedupe_seconds=3600)
        out.append({"task_id": row.get("id"), "ref": ref, "sent": bool(result.get("sent")),
                    **({"reason": result["reason"]} if result.get("reason") else {})})
    return out


def relayed(ref: str, within_seconds: float = 3600) -> bool:
    """True when a 🔐 callback for this tab went to CoS within the window."""
    pending = (_read_state().get("pending") or {}).get(ref) or {}
    try:
        at = datetime.fromisoformat(str(pending.get("at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False
    return time.time() - at <= within_seconds


def amp_confirm(rule: str, title: str, detail: str, timeout_ms: int, dry_run: bool = False) -> dict[str, Any]:
    tab = find_tab(workspace_id=current_workspace_id())
    if not tab:
        return {"sent": False, "reason": "not in a cmux tab"}
    if is_operator_tab(tab):
        return {"sent": False, "reason": "operator tab"}
    ref = str(tab.get("ref") or "")
    wait = min(10.0, max(timeout_ms / 3000.0, 0.0))
    if not dry_run:
        time.sleep(wait)
        screen = read_screen(ref) or ""
        if title and title not in screen:
            return {"sent": False, "reason": "the modal is no longer on screen"}
    row = live_row(task_id=os.environ.get("PM_OS_WORKER_TASK_ID", ""), ref=ref)
    command = detail.split("\n\n", 1)[1] if "\n\n" in detail else ""
    text = build_message(tab=tab, row=row, tool="Bash" if command else "", command=command,
                         reason=f"{title}: {detail.splitlines()[0] if detail else rule}", kind="permission_prompt",
                         amp_timeout_s=max(int(timeout_ms / 1000 - wait), 1))
    key = hashlib.sha256(f"amp|{ref}|{rule}|{detail}".encode()).hexdigest()[:16]
    result = deliver(text, key, dry_run=dry_run)
    _log({"event": "amp-confirm", "ref": ref, "rule": rule, **{k: v for k, v in result.items() if k != "text"}})
    return {**result, "ref": ref}


def auto_check(ref: str, task_id: str = "", screen: str | None = None) -> dict[str, Any]:
    """CoS's own allow: only for the prompt the relay sent with case
    own-task-files, and only while the files still prove to be the task's.
    The stored command must be the whole command (a cut one could drop a later delete), and the whole
    of it must be on screen, so a stale record can never approve the next prompt in that tab."""
    pending = (_read_state().get("pending") or {}).get(ref) or {}
    row = live_row(task_id=task_id or str(pending.get("task_id") or ""), ref=ref)
    command = str(pending.get("command") or "")
    if not command:
        return {"case": permission_case.ASK, "reason": "no relayed prompt on record for that tab", "targets": []}
    if pending.get("case") != permission_case.OWN:
        return {"case": permission_case.ASK, "reason": "the relay did not send it as own task files", "targets": []}
    if int(pending.get("command_len") or len(command)) > len(command):
        return {"case": permission_case.ASK, "reason": "the stored command was cut, so it cannot be checked whole",
                "targets": []}
    if screen is not None and _flat(command) not in _flat(screen):
        return {"case": permission_case.ASK, "reason": "the whole command is not on screen", "targets": []}
    return prompt_case(row, str(pending.get("tool") or ""), str(pending["command"]), str(pending.get("kind") or ""),
                       cwd=str(pending.get("cwd") or ""))


def record_auto_answer(ref: str, task_id: str, verdict: dict[str, Any]) -> dict[str, Any]:
    pending = (_read_state().get("pending") or {}).get(ref) or {}
    tab = find_tab(ref=ref)
    entry = {"at": _now(), "ref": ref, "task_id": task_id or str(pending.get("task_id") or ""),
             "tab": owner_tabs.tab_title(tab) if tab else "", "command": str(pending.get("command") or "")[:2000],
             "targets": list(verdict.get("targets") or [])[:50], "reported": False}
    entry["report_line"] = (f"Auto-allowed a permission prompt in {entry['tab'] or ref}: it deleted or overwrote only "
                            f"files that task made ({permission_case.summary(verdict)}).")
    _mutate(lambda s: s.setdefault("auto_answers", []).append(entry))
    return entry


def auto_answers(unreported: bool = False, mark_reported: bool = False) -> list[dict[str, Any]]:
    def run(state: dict[str, Any]) -> dict[str, Any]:
        rows = state.setdefault("auto_answers", [])
        state["auto_answers"] = rows[-200:]
        picked = [r for r in state["auto_answers"] if not (unreported and r.get("reported"))]
        if mark_reported:
            for r in picked:
                r["reported"] = True
        return {"rows": [dict(r) for r in picked]}
    return _mutate(run).get("rows", [])


def answer(decision: str, ref: str = "", task_id: str = "", settle: float = 1.5, auto: bool = False) -> dict[str, Any]:
    """Press the permission dialog in `ref` (or the row's tab): allow = Enter on "1. Yes", deny = Escape.
    `auto` is CoS allowing an own-task-files prompt without the owner; anything else is refused."""
    decision = decision.strip().lower()
    if decision not in {"allow", "deny"}:
        return {"status": "rejected", "reason": "decision must be allow or deny"}
    if auto and decision != "allow":
        return {"status": "rejected", "reason": "only allow is automatic; a deny needs the owner's word"}
    if not ref and task_id:
        ref = str(live_row(task_id=task_id).get("workspace_ref") or "")
    if not ref:
        return {"status": "no-prompt", "reason": "no live tab for that task"}
    screen = read_screen(ref)
    if screen is None:
        return {"status": "failed", "ref": ref, "reason": "screen not readable"}
    if not prompt_visible(screen):
        return {"status": "no-prompt", "ref": ref, "reason": "no permission prompt is showing in that tab; nothing was pressed"}
    if decision == "allow" and not YES_SELECTED_RE.search(screen):
        return {"status": "failed", "ref": ref, "reason": "the cursor is not on \"1. Yes\"; answer in the tab"}
    # Press only the dialog CoS relayed. A late answer must never approve the next
    # command after the owner already answered the first one in the tab.
    relayed_cmd = str(((_read_state().get("pending") or {}).get(ref) or {}).get("command") or "")
    if relayed_cmd and not command_on_screen(relayed_cmd, screen):
        return {"status": "failed", "ref": ref,
                "reason": "the dialog on screen is for a different command than the one CoS relayed; nothing was pressed, answer in the tab"}
    verdict: dict[str, Any] = {}
    if auto:
        verdict = auto_check(ref, task_id, screen=screen)
        if verdict.get("case") != permission_case.OWN:
            _log({"event": "auto-answer-refused", "ref": ref, "task_id": task_id, "reason": verdict.get("reason")})
            return {"status": "rejected", "ref": ref,
                    "reason": f"not an own-task-files prompt ({verdict.get('reason')}); nothing was pressed, ask the owner"}
    key = "Enter" if decision == "allow" else "Escape"
    if not send_key(ref, key):
        return {"status": "failed", "ref": ref, "reason": f"cmux send-key {key} failed"}
    time.sleep(settle)
    after = read_screen(ref)
    still = prompt_visible(after)

    def clear(s: dict[str, Any]) -> None:
        # After an answer, the same command asked again (a retry after deny) is a new prompt.
        sent_key = (s["pending"].pop(ref, None) or {}).get("key")
        if sent_key:
            s["sent"].pop(sent_key, None)

    entry = record_auto_answer(ref, task_id, verdict) if auto else {}
    _mutate(clear)
    _log({"event": "auto-answer" if auto else "answer", "ref": ref, "decision": decision, "task_id": task_id,
          "prompt_gone": not still, **({"targets": entry.get("targets")} if auto else {})})
    extra = {"auto": True, "report_line": entry["report_line"]} if auto else {}
    if still:
        return {"status": "unclear", "ref": ref, "decision": decision, **extra,
                "reason": "a permission prompt still shows after the key press (it may be the next prompt); check the tab"}
    return {"status": "answered", "ref": ref, "decision": decision, **extra}


# --- CLI -----------------------------------------------------------------------------------

def _detach(args: list[str]) -> None:
    log = STATE_DIR / "logs" / "permission-relay.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, **({"creationflags": 0x08000200} if os.name == "nt" else {"start_new_session": True}), env=os.environ.copy())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("notify")
    s = sub.add_parser("send")
    s.add_argument("--payload", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--keep", action="store_true", help="keep the payload file")
    s.add_argument("--note", default="", help="one line shown under the first line (live checks)")
    a = sub.add_parser("amp-confirm")
    a.add_argument("--rule", default="")
    a.add_argument("--title", default="")
    a.add_argument("--detail", default="")
    a.add_argument("--timeout-ms", type=int, default=90_000)
    a.add_argument("--dry-run", action="store_true")
    sw = sub.add_parser("sweep")
    sw.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("answer")
    w.add_argument("decision", choices=["allow", "deny"])
    w.add_argument("--workspace-ref", default="")
    w.add_argument("--task-id", default="")
    w.add_argument("--auto", action="store_true", help="CoS's own allow for an own-task-files prompt")
    aa = sub.add_parser("auto-answers")
    aa.add_argument("--unreported", action="store_true")
    aa.add_argument("--mark-reported", action="store_true")
    args = parser.parse_args()

    if args.command == "notify":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0
        if disabled() or str(payload.get("notification_type") or "") not in RELAY_TYPES:
            return 0
        # cmux exports CMUX_WORKSPACE_ID; wmux exports only WMUX_SURFACE_ID, so
        # current_workspace_id() asks wmux for the caller's workspace there.
        payload["workspace_id"] = current_workspace_id()
        if not payload["workspace_id"]:
            return 0  # outside cmux/wmux there is no tab to read and no CoS to tell
        payload["task_id"] = os.environ.get("PM_OS_WORKER_TASK_ID", "")
        fd, path = tempfile.mkstemp(prefix="permission-relay-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        _detach(["send", "--payload", path])
        return 0
    if args.command == "send":
        try:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(json.dumps({"sent": False, "reason": f"payload unreadable: {error}"}))
            return 1
        if args.note:
            payload["note"] = args.note
        if not args.keep:
            Path(args.payload).unlink(missing_ok=True)
        print(json.dumps(send(payload, force=args.force, dry_run=args.dry_run)))
        return 0
    if args.command == "sweep":
        print(json.dumps(sweep(dry_run=args.dry_run)))
        return 0
    if args.command == "amp-confirm":
        if disabled():
            return 0
        print(json.dumps(amp_confirm(args.rule, args.title, args.detail, args.timeout_ms, dry_run=args.dry_run)))
        return 0
    out = answer(args.decision, ref=args.workspace_ref, task_id=args.task_id)
    print(json.dumps(out))
    return 0 if out.get("status") == "answered" else 1


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    raise SystemExit(main())
