#!/usr/bin/env python3
"""Make a hand-started Claude tab the CoS on its first prompt.

The owner opens a tab in wmux, types claude, and then types start of day, CoS, or chief
of staff, with no other setup to remember. So the binding cannot depend on the model remembering Phase 0 of the
chief-of-staff skill. The switchboard plugin hook surface-context.py calls `observe()` on every
UserPromptSubmit. When the first prompt of a session is one of the trigger phrases, this
module, before the model sees the prompt:

  1. finds the pane's own tab: on wmux the pane's WMUX_SURFACE_ID decides (wmux exports no
     CMUX_WORKSPACE_ID), on cmux CMUX_WORKSPACE_ID;
  2. on wmux, binds the Claude session id to that tab (`cmux session-bind`, the shim's
     sidecar), so the session store, owner_tabs.py, the report waiter, and ask-cos.sh see
     a Claude CoS (cmux on macOS binds sessions through its own hooks);
  3. registers the tab as CoS with owner_tabs.claim_cos: system/state/cos-surface.json,
     title "🧭 CoS", pinned. Any other CoS-titled tab becomes "🧭 CoS (old <agent>)" and is
     unpinned. The newest tab the owner started wins; an old tab is renamed, never closed,
     so a busy CoS or one holding a draft loses nothing;
  4. on cmux, moves the tab to the top. wmux gets no tab ordering;
     its title and pin still apply;
  5. returns the context line the hook injects: what was bound, then what to do with the
     ask ("start of day" dispatches start of day; "CoS" or "chief of staff" replies Ready.).

Never in a worker (SWITCHBOARD_WORKER*, SWITCHBOARD_WORKTREE, a "switchboard worker" tab, a task worktree),
never with SWITCHBOARD_SURFACE set to another surface, never outside a multiplexer, and only on a
session's first prompt, so "CoS" typed mid-conversation in some other tab claims nothing.

CLI (tests and by hand; prints one JSON line):
  cos_autobind.py match "<prompt>"                    which trigger, if any
  cos_autobind.py bind --session-id <id> "<prompt>"   run the binding now (ignores first-prompt)

Env: COS_AUTOBIND_STATE_DIR (first-prompt markers, default <repo>/.state/cos-autobind),
COS_AUTOBIND_DISABLED=1 (kill switch; also ~/.claude/switchboard-plugin-config.json
{"cos-autobind.disabled": true}). Tests: system/scripts/test_cos_autobind.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

REPO_ROOT = Path(os.environ.get("SWITCHBOARD_ROOT") or os.environ.get("SWITCHBOARD_REPO") or HERE.parents[1])
COS_TITLE = "🧭 CoS"

START_OF_DAY = "start-of-day"
COS = "cos"
_TRIGGERS = {
    START_OF_DAY: ("start of day", "start my day", "start the day", "sod", "good morning",
                   "/start-day", "/switchboard:start-day"),
    COS: ("cos", "chief of staff", "chief-of-staff", "/chief-of-staff"),
}
_PHRASE_TO_KIND = {phrase: kind for kind, phrases in _TRIGGERS.items() for phrase in phrases}


def state_dir() -> Path:
    return Path(os.environ.get("COS_AUTOBIND_STATE_DIR") or REPO_ROOT / ".state" / "cos-autobind")


def disabled() -> bool:
    if os.environ.get("COS_AUTOBIND_DISABLED") == "1":
        return True
    config = Path(os.environ.get("SWITCHBOARD_PLUGIN_CONFIG") or Path.home() / ".claude" / "switchboard-plugin-config.json")
    try:
        return bool(json.loads(config.read_text(encoding="utf-8")).get("cos-autobind.disabled"))
    except (OSError, ValueError, AttributeError):
        return False


def normalize(prompt: str) -> str:
    text = (prompt or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^(hey|hi|ok|okay)[,\s]+", "", text)
    text = re.sub(r"[\s.!?,:;]+$", "", text)
    text = re.sub(r"\s*,?\s*please$", "", text)
    return text.strip()


def trigger(prompt: str) -> str:
    """start-of-day, cos, or "" for any other prompt. The whole prompt must be the phrase,
    so "CoS should review X" or "start of day was slow" never triggers."""
    return _PHRASE_TO_KIND.get(normalize(prompt), "")


# --- who may be CoS ----------------------------------------------------------------------

def in_task_worktree(cwd: str) -> bool:
    """A linked git worktree has a .git file where the main checkout has a directory."""
    path = Path(cwd or ".").resolve()
    for folder in (path, *path.parents):
        dot_git = folder / ".git"
        if dot_git.exists():
            return dot_git.is_file()
    return False


def refusal(env: Mapping[str, str], cwd: str) -> str:
    """Why this session may not become CoS, or ""."""
    if any(value and (key.startswith("SWITCHBOARD_WORKER") or key == "SWITCHBOARD_WORKTREE") for key, value in env.items()):
        return "worker session"
    forced = (env.get("SWITCHBOARD_SURFACE") or "").strip().lower()
    if forced and forced != "cos":
        return f"SWITCHBOARD_SURFACE={forced}"
    if in_task_worktree(cwd):
        return "task worktree"
    if not (env.get("WMUX_SURFACE_ID") or env.get("CMUX_WORKSPACE_ID") or env.get("CMUX_SURFACE_ID")):
        return "not in a cmux or wmux pane"
    return ""


def other_fixed_surface(title: str) -> str:
    """The key of a fixed surface other than CoS (todo, launchpad, comms-triage,
    scheduled-jobs, meetings) this title names, or "". Those tabs keep their role."""
    try:
        import sidebar_order
    except Exception:
        return ""
    text = sidebar_order.title_text(title)
    for key, pattern in (*sidebar_order.PINNED, *sidebar_order.BOTTOM):
        if key != "cos" and pattern.match(text):
            return key
    return ""


def backend(env: Mapping[str, str]) -> str:
    return "wmux" if env.get("WMUX_SURFACE_ID") or env.get("WMUX") == "1" else "cmux"


def pane_workspace_id(env: Mapping[str, str]) -> str:
    """The tab this pane sits in. On wmux the pane's surface decides, never a variable a
    command could have inherited (the gmail_send_guard.py rule); on cmux, CMUX_WORKSPACE_ID."""
    if backend(env) != "wmux":
        return str(env.get("CMUX_WORKSPACE_ID") or "").strip()
    import mux  # system/scripts/lib/mux.py
    saved = os.environ.pop("CMUX_WORKSPACE_ID", None)
    try:
        return mux.current_workspace_id()
    finally:
        if saved is not None:
            os.environ["CMUX_WORKSPACE_ID"] = saved


# --- first prompt ------------------------------------------------------------------------

def _marker(session_id: str) -> Path:
    return state_dir() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id or 'unknown')}.json"


def first_prompt(session_id: str) -> bool:
    """True once per session: the first call records the session, later calls see it.
    A marker that cannot be written counts as not first, so an unwritable state folder
    never turns every later prompt into a claim."""
    if not session_id:
        return False
    marker = _marker(session_id)
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as handle:  # "x": a racing second hook loses
            handle.write(json.dumps({"seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    except OSError:
        return False
    return True


# --- the binding -------------------------------------------------------------------------

def budget() -> float:
    """Seconds the whole binding may take; the hook's own limit is 30 (hooks.json)."""
    try:
        return max(2.0, float(os.environ.get("COS_AUTOBIND_BUDGET") or 20))
    except ValueError:
        return 20.0


def _cmux(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    import owner_tabs
    try:
        return subprocess.run(owner_tabs._cmux_argv() + args, capture_output=True, text=True, encoding="utf-8",
                              timeout=max(1.0, timeout), env={**os.environ, "CMUX_QUIET": "1"},
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ok(proc: subprocess.CompletedProcess | None) -> bool:
    return proc is not None and proc.returncode == 0


def log(event: dict[str, Any]) -> None:
    try:
        path = state_dir() / "log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}) + "\n")
    except OSError:
        pass


@contextmanager
def claim_lock(deadline: float):
    """One claim at a time, so two tabs started together cannot both keep the CoS title:
    the second claim lists the tabs after the first renamed itself, and demotes it."""
    try:
        import fcntl as locker
    except ImportError:  # Windows
        import flock_compat as locker
    path = state_dir() / "claim.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                locker.flock(handle, locker.LOCK_EX | locker.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another CoS claim is still running")
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                locker.flock(handle, locker.LOCK_UN)
            except OSError:
                pass


def bind(session_id: str, env: Mapping[str, str] | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Register this pane's tab as the CoS Claude tab. Returns {"bound": bool, ...}."""
    env = os.environ if env is None else env
    cwd = os.getcwd() if cwd is None else cwd
    deadline = time.monotonic() + budget()

    def left() -> float:
        return deadline - time.monotonic()

    why = refusal(env, cwd)
    if why:
        return {"bound": False, "reason": why}
    try:
        ws_id = pane_workspace_id(env)
    except Exception as error:  # an unreadable pane never becomes CoS
        return {"bound": False, "reason": f"pane tab unresolved ({type(error).__name__})"}
    if not ws_id:
        return {"bound": False, "reason": "pane tab unresolved"}
    import owner_tabs
    with claim_lock(deadline):
        tabs = owner_tabs.cmux_workspaces(timeout=max(1.0, min(10.0, left())))
        own = next((w for w in tabs if str(w.get("id") or "").upper() == ws_id.upper()), None)
        if own is None:
            return {"bound": False, "reason": f"tab {ws_id} is not in the tab list"}
        if str(own.get("description") or "").startswith(owner_tabs.WORKER_DESCRIPTION_PREFIX):
            return {"bound": False, "reason": "worker tab", "silent": True}
        surface = other_fixed_surface(str(own.get("custom_title") or own.get("title") or ""))
        if surface:
            return {"bound": False, "reason": f"{surface} tab", "silent": True}
        ref = str(own.get("ref") or ws_id)
        out: dict[str, Any] = {"bound": False, "ref": ref, "workspace_id": own.get("id"), "session_id": session_id,
                               "backend": backend(env), "steps": []}
        if backend(env) == "wmux" and session_id:
            bound = _ok(_cmux(["session-bind", "--workspace", ref, "--thread", session_id], min(10.0, left())))
            out["steps"].append("session-bind" if bound else "session-bind failed")
        claim = owner_tabs.claim_cos(tabs, owner_tabs.agents_by_workspace(), ref, session_id=session_id)
        if not claim.get("claimed"):
            out["reason"] = claim.get("reason") or "claim-cos failed"
            log({"event": "bind-failed", **out})
            return out
        out["bound"] = True
        out["renamed"] = claim.get("renamed") or []
        demoted = [r["ref"] for r in out["renamed"] if r.get("ref") != ref]
        for other in demoted:  # an old CoS leaves the pinned section; it is never closed
            _cmux(["workspace-action", "--workspace", other, "--action", "unpin"], min(10.0, left()))
        out["demoted"] = demoted
        out["steps"].append("claim-cos")
    # cmux only: no tab ordering on wmux.
    # Ordering is cosmetic, so it is skipped rather than outlive the hook.
    if backend(env) == "cmux" and left() > 2:
        moved = _ok(_cmux(["workspace-action", "--action", "move-top", "--workspace", ref], min(10.0, left())))
        out["steps"].append("move-top" if moved else "move-top failed")
    log({"event": "bound", **out})
    return out


# --- Remote Control ----------------------------------------------------------------------
# CoS only. Every scripted CoS launch runs with --remote-control; a hand-typed claude CoS
# reminds the owner once to type /rc. The global Claude Code setting stays unchanged. Scripted launches (cmux-helpers.sh cos_remote_control_args)
# set SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH=1. Otherwise the transcript decides: Claude Code writes
# a "bridge-session" record (and a "bridge_status" line) once Remote Control is on, and a
# plain `claude --resume` of such a session turns it back on, so its records stay valid.

RC_REMINDER = ("Remote Control is off in this CoS session, so the owner's phone cannot reach it. Add one "
               "line to the reply, after the rest: \"Remote Control is off here: type /rc once in this tab "
               "so your phone can reach CoS. It stays on after a wmux restart.\"")


def remote_control_on(transcript_path: str, env: Mapping[str, str]) -> bool | None:
    """True when this session has Remote Control on, False when it plainly does not, None
    when that cannot be read (no reminder then: a wrong nag is worse than none)."""
    if env.get("SWITCHBOARD_COS_REMOTE_CONTROL_LAUNCH") == "1":
        return True
    try:  # the owner turned CoS Remote Control off: no /rc reminder either
        import machine_role
        switch = machine_role.value("SWITCHBOARD_COS_REMOTE_CONTROL", env)
    except Exception:
        switch = str(env.get("SWITCHBOARD_COS_REMOTE_CONTROL") or "")
    if switch.strip().lower() in ("off", "0", "false", "no"):
        return None
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"bridge-session"' in line or '"bridge_status"' in line:
                    return True
    except FileNotFoundError:
        return False  # the first prompt can land before Claude Code writes the file
    except OSError:
        return None
    return False


# --- what the hook injects ---------------------------------------------------------------

def context_line(kind: str, result: dict[str, Any], prompt: str, remote_control: bool | None = None) -> str:
    root = str(REPO_ROOT)
    if result.get("bound"):
        demoted = result.get("demoted") or []
        head = (f"switchboard: the cos-autobind hook registered this tab as the CoS Claude tab before this turn "
                f"({result.get('ref')}, titled \"{COS_TITLE}\", pinned{', moved to the top' if 'move-top' in (result.get('steps') or []) else ''}, session {result.get('session_id')}"
                + (f"; the previous CoS tab {', '.join(demoted)} was renamed \"{COS_TITLE} (old …)\", not closed" if demoted else "")
                + "). Phase 0 tab chrome is done; do not repeat it. Work from the repo root: "
                f"`cd {root}`. Origin is cos (DISPATCH_ORIGIN=cos).")
    else:
        head = (f"switchboard: the cos-autobind hook could not register this tab as CoS ({result.get('reason') or 'unknown'}). "
                f"Run `cd {root} && bash system/scripts/cos-phase0.sh chrome --claim` once, report its line in one "
                "clause, then continue.")
    if kind == START_OF_DAY:
        task = (f" The owner's ask is start of day: as the first and only routing call, run "
                f"`cd {root} && bash system/scripts/router-dispatch.sh --origin cos \"{prompt.strip()}\"`, "
                "reply with its `message` line, and stop.")
    else:
        task = (" The owner's ask boots CoS: reply only `Ready.` and stop. Do not run the daily workflow and do not "
                "route this message.")
    if result.get("bound") and remote_control is False:
        task += " " + RC_REMINDER
    return head + task


def heal_registered_cos(session_id: str, env: Mapping[str, str], cwd: str) -> dict[str, Any] | None:
    """Bind the session of a CoS tab that is registered but names no session. A CoS tab
    claimed by cos-phase0.sh before autobind ran leaves cos-surface.json with no thread_id
    and the session store with no CoS agent, so the sweep's usage-limit and network-stop
    resume cannot find CoS. First-prompt
    binding never reaches a session already past its first prompt, so any later prompt in the
    registered tab binds it here, silently: no retitle, no pin, no context line. Each session
    checks once (a marker), so other tabs pay one tab lookup at most. None means nothing done."""
    if not session_id:
        return None
    import owner_tabs
    try:
        registration = json.loads(owner_tabs.COS_REGISTRATION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(registration, dict) or registration.get("thread_id") or not registration.get("workspace_id"):
        return None
    marker = state_dir() / f"heal-{_marker(session_id).name}"
    if marker.exists() or refusal(env, cwd):
        return None
    try:
        ws_id = pane_workspace_id(env)
    except Exception:
        return None   # no marker: a lookup that failed this time is tried on the next prompt
    if not ws_id:
        return None
    try:   # the tab is known now: this session never checks again, whatever follows
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.open("x", encoding="utf-8").close()
    except OSError:
        return None
    if ws_id.upper() != str(registration["workspace_id"]).upper():
        return None
    deadline = time.monotonic() + budget()
    ref = str(registration.get("ref") or ws_id)
    out: dict[str, Any] = {"bound": False, "healed": True, "ref": ref, "session_id": session_id,
                           "backend": backend(env), "steps": []}
    try:
        with claim_lock(deadline):
            # Another tab may have claimed CoS since the first read: heal only the same, unbound one.
            current = json.loads(owner_tabs.COS_REGISTRATION.read_text(encoding="utf-8"))
            if current.get("thread_id") or str(current.get("workspace_id") or "").upper() != ws_id.upper():
                marker.unlink(missing_ok=True)
                return None
            if backend(env) == "wmux":
                bound = _ok(_cmux(["session-bind", "--workspace", ref, "--thread", session_id],
                                  max(1.0, min(10.0, deadline - time.monotonic()))))
                out["steps"].append("session-bind" if bound else "session-bind failed")
            tabs = owner_tabs.cmux_workspaces(timeout=max(1.0, min(10.0, deadline - time.monotonic())))
            claim = owner_tabs.claim_cos(tabs, owner_tabs.agents_by_workspace(), ref, rename=False, session_id=session_id)
    except Exception as error:   # a busy lock or an unreadable registration: try again next prompt
        marker.unlink(missing_ok=True)
        log({"event": "heal-error", "reason": f"{type(error).__name__}: {error}"[:200], "session_id": session_id})
        return None
    if not claim.get("claimed"):
        marker.unlink(missing_ok=True)   # e.g. an empty tab listing: the next prompt tries again
    out["bound"] = bool(claim.get("claimed"))
    if not out["bound"]:
        out["reason"] = claim.get("reason") or "claim-cos failed"
    out["steps"].append("registration" if out["bound"] else "registration failed")
    log({"event": "healed" if out["bound"] else "heal-failed", **out})
    return out


def observe(prompt: str, session_id: str, env: Mapping[str, str] | None = None,
            cwd: str | None = None, transcript_path: str = "") -> tuple[str, dict[str, Any]]:
    """Called on every UserPromptSubmit. Returns (context, result); context "" means nothing
    to inject. Records the session on its first prompt either way. A bound CoS without Remote
    Control (remote_control_on) gets the one-time /rc reminder in its context line."""
    env = os.environ if env is None else env
    if disabled():
        return "", {"bound": False, "reason": "disabled"}
    is_first = first_prompt(session_id)
    kind = trigger(prompt)
    if not kind or not is_first:
        healed = heal_registered_cos(session_id, env, os.getcwd() if cwd is None else cwd)
        if healed is not None:
            return "", healed
    if not kind:
        return "", {"bound": False, "reason": "no trigger"}
    if not is_first:
        return "", {"bound": False, "reason": "not the session's first prompt"}
    why = refusal(env, os.getcwd() if cwd is None else cwd)
    if why:
        return "", {"bound": False, "reason": why}
    started = time.monotonic()
    try:
        result = bind(session_id, env, cwd)
    except Exception as error:  # e.g. a sync client holding cos-surface.json: tell the model, never crash the hook
        result = {"bound": False, "reason": f"{type(error).__name__}: {error}"[:200]}
        log({"event": "bind-error", "reason": result["reason"], "session_id": session_id})
    result["seconds"] = round(time.monotonic() - started, 2)
    if result.get("silent"):  # a worker or fixed-surface tab keeps its own role and hears nothing
        return "", result
    result["trigger"] = kind
    remote_control = remote_control_on(transcript_path, env) if result.get("bound") else None
    result["remote_control"] = remote_control
    return context_line(kind, result, prompt, remote_control), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("match")
    m.add_argument("prompt")
    b = sub.add_parser("bind")
    b.add_argument("--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    b.add_argument("prompt", nargs="?", default="cos")
    args = parser.parse_args(argv)
    if args.command == "match":
        print(json.dumps({"trigger": trigger(args.prompt)}))
        return 0
    result = bind(args.session_id)
    result["context"] = context_line(trigger(args.prompt) or COS, result, args.prompt)
    print(json.dumps(result))
    return 0 if result.get("bound") else 1


if __name__ == "__main__":
    raise SystemExit(main())
