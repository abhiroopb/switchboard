#!/usr/bin/env python3
"""Owner tabs, the live CoS tab, and cmux agent messages.

Three rules share this module:

1. A worker can run in its own tab while the workstream's owner tab (an idle
   Claude session) is open. If its result went to CoS only and the worker tab
   was reaped, the owner tab would show nothing. A finished worker sends a result card to its owner's open tab, or writes the
   card under "## Latest results" in the workstream's CONTEXT.md when no tab is
   open (``result-card``).
2. No new worker opens when an owner tab for the ask is already open.
   dispatch.py delivers a new ask to the owner's open Claude tab with
   ``cmux agent message`` instead of opening a worker (``owner_tab`` +
   ``agent_message``).
3. A report can land on a stale tab titled "🧭 CoS" while the live CoS is a
   tab with another title. The live CoS is resolved registration first (``claim-cos`` writes
   system/state/cos-surface.json and retitles the tabs), then by title, and
   prefers the tab that runs the configured agent (router.yaml ``agent:``).
   Reports to a Claude tab go by ``cmux agent message``, which reaches an idle
   session at once and a busy one at its next step, never through the
   composer.

CLI (one JSON line each):
  owner_tabs.py cos-target                      the live CoS tab and why
  owner_tabs.py claim-cos [--workspace-ref R]   register this tab as CoS, retitle it 🧭 CoS
  owner_tabs.py agent --ref R                   claude | amp | "" for one tab
  owner_tabs.py owner-tab --workstream SLUG     the open owner tab of a workstream
  owner_tabs.py message --ref R --from NAME     send stdin (or --text) by cmux agent message
  owner_tabs.py result-card --task-id ID ...    card to the owner tab, else CONTEXT.md

Env: OWNER_TABS_ROOT (repo root for workstreams/, tests), SWITCHBOARD_STATE_DIR,
CC_CMUX_SESSION_JSON (cmux session store, read by running-task-state.py),
CMUX_BIN / CMUX.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402
import private_workstreams  # noqa: E402

REPO_ROOT = Path(os.environ.get("OWNER_TABS_ROOT") or main_checkout(Path(os.environ.get("REPO_ROOT") or SCRIPTS.parents[1])))
STATE_DIR = Path(os.environ.get("SWITCHBOARD_STATE_DIR") or REPO_ROOT / "system" / "state")
COS_REGISTRATION = Path(os.environ.get("COS_SURFACE_FILE") or STATE_DIR / "cos-surface.json")


def _auto_sandbox() -> None:
    """The report-to-cos.sh rule in Python: a run
    whose ledger, state dir or repo root is not main's (a test, a repair copy)
    sets SWITCHBOARD_TEST_SANDBOX=1, so lib/mux.py and the wmux shim, here and in every
    child it starts (ask_cos.py's detached retry included), refuse live tabs."""
    if os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
        return
    main = main_checkout(Path(os.environ.get("REPO_ROOT") or SCRIPTS.parents[1]))
    state = main / "system" / "state"
    same = lambda a, b: os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    checks = ((os.environ.get("RUNNING_TASKS_FILE"), state / "running-tasks.json"),
              (os.environ.get("SWITCHBOARD_STATE_DIR"), state), (os.environ.get("OWNER_TABS_ROOT"), main))
    if any(value and not same(value, expected) for value, expected in checks):
        os.environ["SWITCHBOARD_TEST_SANDBOX"] = "1"


_auto_sandbox()
COS_TITLE = "🧭 CoS"
COS_TITLES = (COS_TITLE, "CoS")
WORKER_DESCRIPTION_PREFIX = "switchboard worker "
LATEST_RESULTS_HEADING = "## Latest results"
LATEST_RESULTS_KEEP = 5
REPORT_SENDER = "switchboard report"
PT = ZoneInfo("America/Los_Angeles")
_LINE_MARK = re.compile(r" ?⏎ ?")
_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_FOLLOWUP = re.compile(r"^\s*(?:[-*]\s*)?Follow-up:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _cmux() -> str:
    explicit = os.environ.get("CMUX_BIN") or os.environ.get("CMUX")
    if explicit:
        return explicit
    try:
        from lib.mux import cmux_bin
        return cmux_bin()
    except Exception:
        return "cmux"


def _cmux_argv() -> list[str]:
    """The cmux command as an argv prefix; on Windows the mux/cmux wrapper runs via Python."""
    try:
        from lib.mux import runnable
        return runnable([_cmux()])
    except Exception:
        return [_cmux()]


def _ledger():
    spec = importlib.util.spec_from_file_location("running_task_state", SCRIPTS / "running-task-state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- workstream titles -------------------------------------------------------------------

def _config_scalar(path: Path, key: str) -> str:
    """`key:` from a config.yaml, read like cmux-helpers.sh (first line, quotes dropped)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            value = stripped.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value
    return ""


def titleize(slug: str) -> str:
    special = {"ai": "AI", "ios": "iOS", "pr": "PR"}
    return " ".join(special.get(p, p.capitalize()) for p in slug.replace("_", "-").split("-") if p)


def owner_name(slug: str, root: Path | None = None) -> str:
    return _config_scalar((root or REPO_ROOT) / "workstreams" / slug / "config.yaml", "name") or titleize(slug)


def owner_title(slug: str, root: Path | None = None) -> str:
    """"<emoji> <Name>", as dispatch.owner_title and cmux-helpers.sh build it."""
    cfg = (root or REPO_ROOT) / "workstreams" / slug / "config.yaml"
    return f"{_config_scalar(cfg, 'emoji') or '📁'} {_config_scalar(cfg, 'name') or titleize(slug)}"


def owner_tab_titles(slug: str, root: Path | None = None) -> list[str]:
    """The normal owner title and its P0 forms ("<emoji> P0 · <Name>", legacy "🔥 P0 · <emoji> <Name>")."""
    title = owner_title(slug, root)
    emoji, _, rest = title.partition(" ")
    return [title, f"{emoji} P0 · {rest}" if rest else f"🔥 P0 · {title}", f"🔥 P0 · {title}"]


# --- cmux state --------------------------------------------------------------------------

def tab_title(ws: dict[str, Any]) -> str:
    return str(ws.get("custom_title") or ws.get("title") or "")


def cmux_workspaces(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every open tab, or [] when cmux cannot answer."""
    try:
        result = subprocess.run(_cmux_argv() + ["rpc", "workspace.list", "{}"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    try:
        return list(json.loads(result.stdout).get("workspaces") or [])
    except ValueError:
        return []


def agents_by_workspace(agents: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """{workspace id (upper case): live agent row} from the cmux session store."""
    if agents is None:
        try:
            agents = _ledger().live_agents()
        except Exception:
            agents = {}
    return {str(a.get("workspace_id") or "").upper(): a for a in agents.values() if a.get("workspace_id")}


def tab_agent(ws: dict[str, Any] | None, by_ws: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """{"agent": claude|amp|"", "thread_id", "state"} for one tab."""
    row = by_ws.get(str((ws or {}).get("id") or "").upper()) or {}
    return {"agent": str(row.get("agent") or ""), "thread_id": str(row.get("thread_id") or ""),
            "state": str(row.get("state") or "")}


def screen_agent(ref: str) -> str:
    """"claude" when the tab's screen shows a Claude Code composer, else "". Only for a
    tab the session store binds to no agent: a Claude Code the owner started by hand in a
    wmux pane never binds (no CMUX_WORKSPACE_ID), so the CoS would read as running no agent
    and every report to it would take the idle-only paste path, which a busy CoS holds. The wmux shim reports the composer; cmux on macOS binds
    every session itself and has no composer field, so this answers "" there."""
    if not ref:
        return ""
    try:
        proc = subprocess.run(_cmux_argv() + ["agent", "state", ref, "--json"], capture_output=True, text=True,
                              encoding="utf-8", timeout=20, env={**os.environ, "CMUX_QUIET": "1"})
        data = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    return "claude" if isinstance(data, dict) and data.get("composer") in ("claude-empty", "claude-text") else ""


def with_screen_agent(ref: str, agent: dict[str, Any]) -> dict[str, Any]:
    if agent.get("agent") or os.environ.get("OWNER_TABS_SKIP_SCREEN") == "1":
        return agent
    seen = screen_agent(ref)
    return {**agent, "agent": seen, "agent_source": "screen"} if seen else agent


IDLE_STATES = {"Idle"}
INJECT_GUARD = SCRIPTS / "lib" / "cmux-inject-guard.sh"


def tab_idle(ref: str, state: str) -> tuple[bool, str]:
    """Whether an owner tab may take a new ask right now. Idle means the cmux agent state is Idle
    AND the screen shows no running turn and an empty composer (cig_safe: readable,
    not busy, positively empty box). Anything else is busy, and an unreadable
    screen counts as busy so the ask goes to a parallel worker instead of waiting."""
    if state not in IDLE_STATES:
        return False, f"agent state is {state or 'unknown'}"
    if os.environ.get("OWNER_TABS_SKIP_SCREEN") == "1":
        return True, ""
    try:
        proc = subprocess.run(["bash", "-c", 'source "$1" >/dev/null 2>&1; cig_safe "$2"', "_", str(INJECT_GUARD), ref],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False, "screen not readable"
    return (True, "") if proc.returncode == 0 else (False, "a turn is running or text is typed in its input box")


def is_worker_tab(ws: dict[str, Any]) -> bool:
    return str(ws.get("description") or "").startswith(WORKER_DESCRIPTION_PREFIX)


def owner_tab(slug: str, workspaces: list[dict[str, Any]], exclude_refs: tuple[str, ...] = (),
              root: Path | None = None) -> dict[str, Any] | None:
    """The open owner tab of `slug` (normal or P0 title), never a worker tab
    ("<owner> <n>" titles differ, and a "switchboard worker rt-…" description is
    excluded on its own)."""
    if not slug:
        return None
    titles = set(owner_tab_titles(slug, root))
    excluded = {r for r in exclude_refs if r}
    for ws in workspaces:
        if tab_title(ws) in titles and not is_worker_tab(ws) and ws.get("ref") not in excluded \
                and ws.get("id") not in excluded:
            return ws
    return None


# --- the live CoS tab --------------------------------------------------------------------

def configured_agent() -> str:
    try:
        import switchboard_agent
        return str(switchboard_agent.agent())
    except Exception:
        return ""


def read_registration() -> dict[str, Any]:
    try:
        data = json.loads(COS_REGISTRATION.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cos_tab(workspaces: list[dict[str, Any]], by_ws: dict[str, dict[str, Any]],
            want_agent: str = "") -> dict[str, Any]:
    """The live CoS tab: the registered workspace when it is still open, else a
    CoS-titled tab, preferring one that runs `want_agent`. Returns
    {"ref", "workspace_id", "title", "agent", "thread_id", "reason", "warning"}
    with ref "" when no CoS tab is open."""
    def describe(ws: dict[str, Any], reason: str) -> dict[str, Any]:
        agent = with_screen_agent(str(ws.get("ref") or ""), tab_agent(ws, by_ws))
        out = {"ref": str(ws.get("ref") or ""), "workspace_id": str(ws.get("id") or ""), "title": tab_title(ws),
               "agent": agent["agent"], "thread_id": agent["thread_id"], "reason": reason, "warning": ""}
        if want_agent and agent["agent"] and agent["agent"] != want_agent:
            out["warning"] = (f"the CoS tab {out['title']!r} ({out['ref']}) runs {agent['agent']} while router.yaml agent is "
                              f"{want_agent}; run `python3 system/scripts/owner_tabs.py claim-cos` in the live CoS tab")
        return out

    reg = read_registration()
    reg_id = str(reg.get("workspace_id") or "").upper()
    if reg_id:
        ws = next((w for w in workspaces if str(w.get("id") or "").upper() == reg_id), None)
        if ws is not None:
            return describe(ws, "registered")
    titled = [w for w in workspaces if tab_title(w) in COS_TITLES]
    titled.sort(key=lambda w: COS_TITLES.index(tab_title(w)))
    if want_agent:
        match = next((w for w in titled if tab_agent(w, by_ws)["agent"] == want_agent), None)
        if match is not None:
            return describe(match, f"title, runs {want_agent}")
    if titled:
        return describe(titled[0], "title")
    return {"ref": "", "workspace_id": "", "title": "", "agent": "", "thread_id": "", "reason": "no CoS tab open",
            "warning": ""}


def _run_ok(args: list[str], timeout: float = 10.0) -> bool:
    """One bounded cmux call (a stuck wmux pipe must not outlive the hook that called it)."""
    try:
        return subprocess.run(_cmux_argv() + args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def claim_cos(workspaces: list[dict[str, Any]], by_ws: dict[str, dict[str, Any]], target: str,
              rename: bool = True, session_id: str = "") -> dict[str, Any]:
    """Register `target` (ref or workspace id) as the live CoS tab. Retitles it
    "🧭 CoS" and pins it; any other tab holding a CoS title becomes
    "🧭 CoS (old <agent>)" so title-based senders (next-up digest, start-day,
    CoS Review) stop reaching it. `session_id` is the Claude session the caller
    knows runs in the tab (cos_autobind.py passes the hook's): it fills thread_id
    when the session store has not bound the tab yet, so the registration names
    the CoS session on wmux, where a hand-started Claude binds late."""
    ws = next((w for w in workspaces if target in {w.get("ref"), w.get("id")} or
               str(w.get("id") or "").upper() == target.upper()), None)
    if ws is None:
        return {"claimed": False, "reason": f"no open tab {target}"}
    agent = tab_agent(ws, by_ws)
    if session_id and not agent["agent"]:
        agent = {**agent, "agent": "claude"}
    record = {"workspace_id": ws.get("id"), "ref": ws.get("ref"), "agent": agent["agent"],
              "thread_id": agent["thread_id"] or session_id, "session_id": session_id or agent["thread_id"],
              "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "previous_title": tab_title(ws)}
    COS_REGISTRATION.parent.mkdir(parents=True, exist_ok=True)
    tmp = COS_REGISTRATION.with_name(f"{COS_REGISTRATION.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(COS_REGISTRATION)
    renamed: list[dict[str, str]] = []
    if rename:
        for other in workspaces:
            if other is ws or tab_title(other) not in COS_TITLES:
                continue
            old_agent = tab_agent(other, by_ws)["agent"] or "tab"
            new_title = f"{COS_TITLE} (old {old_agent})"
            if _run_ok(["rename-workspace", "--workspace", str(other["ref"]), new_title]):
                renamed.append({"ref": str(other["ref"]), "from": tab_title(other), "to": new_title})
        if tab_title(ws) != COS_TITLE and _run_ok(["rename-workspace", "--workspace", str(ws["ref"]), COS_TITLE]):
            renamed.append({"ref": str(ws["ref"]), "from": tab_title(ws), "to": COS_TITLE})
        _run_ok(["workspace-action", "--workspace", str(ws["ref"]), "--action", "pin"])
    return {"claimed": True, "registration": str(COS_REGISTRATION), **record, "renamed": renamed}


# --- cmux agent message ------------------------------------------------------------------

def decode_marks(text: str) -> str:
    """" ⏎ " transport markers back to real newlines."""
    return _LINE_MARK.sub("\n", text)


def agent_message(target: str, text: str, sender: str, timeout: float = 45.0) -> dict[str, Any]:
    """Send `text` to the agent in `target` (ref, id, or title) through cmux's
    agent hooks. ok=True only when cmux accepted it and the recipient runs an
    agent that reads messages; an idle Claude session wakes, a busy one reads
    it at its next step."""
    if not target:
        return {"ok": False, "reason": "no target"}
    try:
        result = subprocess.run(_cmux_argv() + ["agent", "message", target, "--from", sender, "--json", "--", "-"],
                                input=text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "reason": f"cmux agent message failed: {error}"}
    if result.returncode:
        return {"ok": False, "reason": (result.stderr or result.stdout).strip()[:300] or f"exit {result.returncode}"}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "reason": f"unreadable cmux reply: {result.stdout.strip()[:200]}"}
    ok = bool(data.get("recipient_has_agent")) and not data.get("failure_reason")
    # verified: cmux delivers through the session's
    # hooks, so its acceptance is the proof. The wmux shim types the line, so only
    # its "delivered" state (prompt log match, or the composer read back empty on a
    # wmux with no prompt log) proves it; "queued" and "typed" went in unproven.
    wmux = data.get("backend") == "wmux"
    verified = ok and (not wmux or data.get("state") == "delivered")
    out = {"ok": ok, "verified": verified, "id": data.get("id"), "state": data.get("state"),
           "workspace_ref": data.get("recipient_workspace_ref"), "workspace_id": data.get("recipient_workspace_id"),
           "title": data.get("recipient_workspace_title"),
           **({} if ok else {"reason": data.get("failure_reason") or "recipient runs no agent that reads messages"})}
    for key in ("backend", "spilled_to", "spilled_chars"):
        if data.get(key) is not None:
            out[key] = data[key]
    if wmux:
        out["proof"] = data.get("verified") or "none"
    return out


def agent_seen(target: str, marker: str, timeout: float = 20.0) -> dict[str, Any]:
    """Did the agent in `target` already take a line holding `marker`? The wmux
    shim reads the pane's prompt log, then its screen. cmux has no such verb:
    supported=False, and cmux's own acceptance stays the delivery proof there."""
    if not target or not marker.strip():
        return {"seen": False, "supported": False, "reason": "no target or marker"}
    try:
        result = subprocess.run(_cmux_argv() + ["agent", "seen", target, "--marker", marker, "--json"],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"seen": False, "supported": False, "reason": f"agent seen failed: {error}"}
    try:
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"seen": False, "supported": False, "reason": (result.stderr or result.stdout).strip()[:200]}
    return {"seen": bool(data.get("seen")), "supported": True, "where": data.get("where"), "seq": data.get("seq")}


# --- result cards ------------------------------------------------------------------------

def split_report(summary: str) -> dict[str, Any]:
    """{"prose", "drafts": [fenced blocks verbatim], "followups": [...]} from a report summary."""
    text = decode_marks(summary or "").strip()
    drafts = [m.group(0) for m in _FENCED_BLOCK.finditer(text)]
    prose = _FENCED_BLOCK.sub("", text)
    followups = [m.group(1).strip() for m in _FOLLOWUP.finditer(prose)]
    prose = _FOLLOWUP.sub("", prose)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return {"prose": prose, "drafts": drafts, "followups": followups}


def review_link(url: str) -> str:
    if not url:
        return ""
    try:
        from review_surface import review_link as render
        return render(url)
    except Exception:
        return f"[Review note]({url})"


def build_card(task: dict[str, Any], status: str, summary: str, review_url: str = "",
               root: Path | None = None) -> str:
    """The result card text: lead, summary, review note, drafts verbatim, follow-ups."""
    slug = str(task.get("workstream") or "")
    lead = f"[{owner_title(slug, root)} · {task.get('id')}]" if slug else f"[{task.get('id')}]"
    parts = split_report(summary)
    out = [f"📬 Result card {lead} {status}", "", parts["prose"] or "(no summary)"]
    if review_url:
        out += ["", "Review note:", review_link(review_url)]
    if parts["drafts"]:
        out += ["", "Draft for the owner to send (verbatim):"]
        for block in parts["drafts"]:
            out += [block, ""]
        if out[-1] == "":
            out.pop()
    if parts["followups"]:
        out += ["", "Follow-ups:"] + [f"- {f}" for f in parts["followups"]]
    return "\n".join(out).rstrip() + "\n"


def card_message(card: str, slug: str, root: Path | None = None) -> str:
    """The card plus what the owner tab's agent does with it."""
    if private_workstreams.is_private_slug(slug, root or REPO_ROOT):
        return (card + "\n"
                "This is a finished worker's result for this workstream, a callback and not a new ask. "
                "Show the card to the owner verbatim now. Add nothing from it to the tracked CONTEXT.md, because this workstream "
                "keeps private data. Do not redo the work.")
    return (card + "\n"
            "This is a finished worker's result for this workstream, a callback and not a new ask. "
            "Show the card to the owner verbatim now (keep every draft in its code block), then add one line for it under "
            f"`{LATEST_RESULTS_HEADING}` in workstreams/{slug}/CONTEXT.md "
            "(`- <YYYY-MM-DD> · <task id> · <status>: <one-line result>`). Do not redo the work.")


def sandbox_reaching_main(root: Path | str) -> bool:
    """A sandboxed run about to write the real main checkout. The sandbox kept tests off live
    tabs, but with hooks on a fixture's close-out could still commit to main and auto-push
    could publish it. A test's own temp repo is not main, so it
    still writes there."""
    if os.environ.get("SWITCHBOARD_TEST_SANDBOX") != "1":
        return False
    same = lambda a, b: os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    return same(root, main_checkout(SCRIPTS.parents[1]))


def write_latest_results(root: Path, slug: str, task_id: str, card: str, when: datetime | None = None) -> dict[str, Any]:
    """Put `card` at the top of "## Latest results" in root/workstreams/<slug>/CONTEXT.md.
    The section is created before the first "## " heading; at most
    LATEST_RESULTS_KEEP cards stay. Idempotent per task id."""
    if sandbox_reaching_main(root):
        return {"written": False, "reason": "sandboxed run; main is not written"}
    path = root / "workstreams" / slug / "CONTEXT.md"
    if not slug or not path.parent.is_dir():
        return {"written": False, "reason": f"no workstream directory for {slug or '(none)'}"}
    text = path.read_text(encoding="utf-8") if path.is_file() else f"# {owner_title(slug, root)}\n"
    when = (when or datetime.now(timezone.utc)).astimezone(PT)
    entry_head = f"### {when.strftime('%Y-%m-%d %H:%M')} PT · `{task_id}`"
    entry = entry_head + "\n" + card.rstrip() + "\n"
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, l in enumerate(lines) if l.rstrip("\n") == LATEST_RESULTS_HEADING)
    except StopIteration:
        start = -1
    if start < 0:
        first = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
        block = [LATEST_RESULTS_HEADING + "\n", "Result cards from finished workers, newest first. "
                 "Show them to the owner when this tab opens.\n", "\n", entry, "\n"]
        if first == len(lines) and lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if first == len(lines) and lines:
            block = ["\n"] + block
        lines[first:first] = block
        path.write_text("".join(lines), encoding="utf-8")
        return {"written": True, "path": str(path), "created_section": True}
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = "".join(lines[start + 1:end])
    if f"`{task_id}`" in section:
        return {"written": False, "path": str(path), "reason": "already recorded"}
    pieces = re.split(r"(?m)^(?=### )", section)
    intro, cards = pieces[0], [p for p in pieces[1:] if p.strip()]
    cards = [entry + "\n"] + [c if c.endswith("\n\n") else c.rstrip("\n") + "\n\n" for c in cards]
    new_section = intro + "".join(cards[:LATEST_RESULTS_KEEP])
    lines[start + 1:end] = [new_section]
    path.write_text("".join(lines), encoding="utf-8")
    return {"written": True, "path": str(path), "kept": min(len(cards), LATEST_RESULTS_KEEP)}


def commit_written_paths(root: Path, rels: list[str], message: str) -> dict[str, Any]:
    """Commit exactly `rels` on root's checkout as the switchboard worker. `git commit
    -- <path>` only takes paths git already tracks, so a file the caller has just
    created (a new workstream's CONTEXT.md) is staged first by its
    explicit path, never `-A`; a failed add or commit unstages it again, leaving
    the file on disk as it was. Anything else already staged stays out of the commit."""
    def g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    # Close-out lines and result cards quote report text, and these commits never pass the
    # guard finish runs, so a listed value in a CONTEXT.md would block auto-push. Lines naming a listed private identifier are withheld first.
    import private_identifiers
    redacted = private_identifiers.redact_files(root, rels)
    if redacted["status"] == "error":
        return {"committed": False, "reason": f"private identifier list unreadable; not committed ({redacted.get('reason', '')})"[:200]}
    new = [rel for rel in rels if g("ls-files", "--error-unmatch", "--", rel).returncode]
    if new:
        add = g("add", "--", *new)
        if add.returncode:
            g("reset", "-q", "--", *new)
            return {"committed": False, "reason": (add.stderr or add.stdout).strip()[:200] or "git add failed"}
    commit = g("-c", "user.name=switchboard worker", "-c", "user.email=switchboard-worker@local",
               "commit", "-q", "-m", message, "--", *rels)
    if commit.returncode:
        if new:
            g("reset", "-q", "--", *new)
        return {"committed": False, "reason": (commit.stderr or commit.stdout).strip()[:200] or "git commit failed"}
    return {"committed": True, "commit": g("rev-parse", "--short", "HEAD").stdout.strip(), **({"added": new} if new else {}),
            **({"withheld": redacted["kinds"]} if redacted["status"] == "redacted" else {})}


def _commit_context(root: Path, slug: str, task_id: str, was_dirty: bool) -> dict[str, Any]:
    rel = f"workstreams/{slug}/CONTEXT.md"
    if was_dirty:
        return {"committed": False, "reason": f"{rel} had uncommitted edits on main; card left for the next landing"}
    outcome = commit_written_paths(root, [rel], f"{task_id}: result card recorded in {rel}")
    outcome.pop("commit", None)
    return outcome


def _dirty(root: Path, rel: str) -> bool:
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", rel], capture_output=True, text=True)
    return bool(out.stdout.strip())


def deliver_result_card(task: dict[str, Any], status: str, summary: str, review_url: str = "", reporter_ref: str = "",
                        workspaces: list[dict[str, Any]] | None = None,
                        by_ws: dict[str, dict[str, Any]] | None = None, root: Path | None = None,
                        commit: bool = True) -> dict[str, Any]:
    """Send the result card to the owner's open tab, else write it into CONTEXT.md.

    Skipped when the row did not run in a worker tab (an owner tab that did the
    work already shows it), for a cancelled row, and when this row already
    carries a card."""
    root = root or REPO_ROOT
    slug = str(task.get("workstream") or "")
    if not slug:
        return {"card": "skipped", "reason": "no workstream"}
    if status == "cancelled":
        return {"card": "skipped", "reason": "cancelled"}
    if task.get("owner_tab"):
        return {"card": "skipped", "reason": "the owner tab ran this task itself"}
    if not (task.get("worker_id") or task.get("worktree")):
        return {"card": "skipped", "reason": "not a worker row"}
    if (task.get("result_card") or {}).get("status") == status:
        return {**task["result_card"], "card": "skipped", "reason": "card already delivered"}
    workspaces = cmux_workspaces() if workspaces is None else workspaces
    by_ws = agents_by_workspace() if by_ws is None else by_ws
    card = build_card(task, status, summary, review_url, root)
    exclude = (reporter_ref, str(task.get("workspace_ref") or ""))
    tab = owner_tab(slug, workspaces, exclude_refs=exclude, root=root)
    if tab is not None:
        agent = tab_agent(tab, by_ws)
        sent = agent_message(str(tab.get("ref") or tab.get("id")), card_message(card, slug, root),
                             sender=f"{owner_title(slug, root)} worker {task.get('id')}") \
            if agent["agent"] != "amp" else {"ok": False, "reason": "owner tab runs Amp, which cmux agent message cannot reach"}
        if sent.get("ok"):
            return {"card": "tab", "status": status, "workspace_ref": sent.get("workspace_ref") or tab.get("ref"),
                    "title": tab_title(tab), "message_id": sent.get("id"), "message_state": sent.get("state")}
        fallback_reason = f"owner tab {tab_title(tab)} unreachable: {sent.get('reason')}"
    else:
        fallback_reason = "no open owner tab"
    rel = f"workstreams/{slug}/CONTEXT.md"
    was_dirty = _dirty(root, rel)
    if private_workstreams.is_private_slug(slug, root):  # CONTEXT.md is tracked: status and pointer only
        card = build_card(task, status, private_workstreams.WITHHELD_REPORT, review_url, root)
    written = write_latest_results(root, slug, str(task.get("id")), card)
    out = {"card": "context", "status": status, "reason": fallback_reason, **written}
    if written.get("written") and commit:
        out.update(_commit_context(root, slug, str(task.get("id")), was_dirty))
    return out


def record_card(task_id: str, result: dict[str, Any]) -> None:
    if result.get("card") not in {"tab", "context"}:
        return
    ledger = _ledger()

    def mutate(payload):
        row = ledger.find_task(payload, task_id)
        if row is not None:
            row["result_card"] = {k: result.get(k) for k in ("card", "status", "workspace_ref", "message_id", "path")
                                  if result.get(k)}
        return row
    try:
        ledger.locked_mutation(mutate)
    except Exception:
        pass


# --- CLI ---------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cos-target")
    cl = sub.add_parser("claim-cos")
    cl.add_argument("--workspace-ref", default="")
    cl.add_argument("--no-rename", action="store_true")
    cl.add_argument("--session-id", default="", help="the Claude session in this tab (default CLAUDE_CODE_SESSION_ID)")
    ag = sub.add_parser("agent")
    ag.add_argument("--ref", required=True)
    ot = sub.add_parser("owner-tab")
    ot.add_argument("--workstream", required=True)
    ot.add_argument("--exclude-ref", action="append", default=[])
    ms = sub.add_parser("message")
    ms.add_argument("--ref", required=True)
    ms.add_argument("--from", dest="sender", default=REPORT_SENDER)
    ms.add_argument("--text", default=None)
    ms.add_argument("--decode-marks", action="store_true", help='turn " ⏎ " markers into newlines first')
    ms.add_argument("--require-verified", action="store_true",
                    help="exit 0 only when delivery is proven (wmux: prompt-log match), not merely accepted")
    sn = sub.add_parser("seen")
    sn.add_argument("--ref", required=True)
    sn.add_argument("--marker", required=True)
    rc = sub.add_parser("result-card")
    rc.add_argument("--task-id", required=True)
    rc.add_argument("--status", required=True)
    rc.add_argument("--summary", default="")
    rc.add_argument("--review-url", default="")
    rc.add_argument("--reporter-ref", default="")
    args = parser.parse_args()

    if args.command == "cos-target":
        print(json.dumps(cos_tab(cmux_workspaces(), agents_by_workspace(), configured_agent())))
        return 0
    if args.command == "claim-cos":
        target = args.workspace_ref or os.environ.get("CMUX_WORKSPACE_ID", "")
        if not target:
            print(json.dumps({"claimed": False, "reason": "no --workspace-ref and no CMUX_WORKSPACE_ID"}))
            return 2
        # This session's id belongs in the record only when it claims its own tab.
        own = not args.workspace_ref or args.workspace_ref == os.environ.get("CMUX_WORKSPACE_ID", "")
        session = args.session_id or (os.environ.get("CLAUDE_CODE_SESSION_ID", "") if own else "")
        out = claim_cos(cmux_workspaces(), agents_by_workspace(), target, rename=not args.no_rename,
                        session_id=session)
        print(json.dumps(out))
        return 0 if out.get("claimed") else 1
    if args.command == "agent":
        workspaces = cmux_workspaces()
        ws = next((w for w in workspaces if args.ref in {w.get("ref"), w.get("id")}), None)
        agent = tab_agent(ws, agents_by_workspace())
        print(json.dumps({"ref": args.ref, **(with_screen_agent(args.ref, agent) if ws else agent)}))
        return 0
    if args.command == "owner-tab":
        workspaces = cmux_workspaces()
        ws = owner_tab(args.workstream, workspaces, tuple(args.exclude_ref))
        by_ws = agents_by_workspace()
        print(json.dumps({"workstream": args.workstream, "ref": (ws or {}).get("ref") or "",
                          "title": tab_title(ws) if ws else "", **tab_agent(ws, by_ws)}))
        return 0
    if args.command == "message":
        text = sys.stdin.read() if args.text is None else args.text
        if args.decode_marks:
            text = decode_marks(text)
        out = agent_message(args.ref, text, args.sender)
        print(json.dumps(out))
        if args.require_verified:
            return 0 if out.get("verified") else (3 if out.get("ok") else 1)
        return 0 if out.get("ok") else 1
    if args.command == "seen":
        out = agent_seen(args.ref, args.marker)
        print(json.dumps(out))
        return 0 if out.get("seen") else 1
    if args.command == "result-card":
        task = _ledger().find_task(_ledger().load_payload(), args.task_id)
        if task is None:
            print(json.dumps({"card": "skipped", "reason": f"no ledger row {args.task_id}"}))
            return 0
        out = deliver_result_card(task, args.status, args.summary, args.review_url, args.reporter_ref)
        record_card(args.task_id, out)
        print(json.dumps(out))
        return 0
    return 2


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    raise SystemExit(main())
