#!/usr/bin/env python3
"""End-to-end smoke test of the Claude dispatch path.

The dispatch path was built for Amp and needed a durable fix for Claude. This drives
real cmux tabs and real Claude workers against the test workstream `switchboard-smoke`
(dispatch.py scaffolds it on the first run) and checks each mechanism CoS relies on:

  hook     the surface-context hook gives the live CoS tab the dispatch card
  launch   a dispatched no-op worker opens in its own tab and runs Claude
  steer    `router-dispatch.sh --steer` delivers to the live worker by cmux agent
           message (status delivered) and the worker's report carries the steer token
  report   the report is delivered and reaches the CoS session's transcript
  reap     the worker tab closes after the report
  owner    (when owner_tabs.py is installed) an idle open owner tab receives the
           ask instead of a new worker; a busy owner tab gets a parallel worker;
           the parallel worker's result reaches the owner tab

Every step prints PASS, FAIL, or SKIP with its evidence; exit 1 when any step
fails. Cleanup: the owner tab the test opened is closed; the workstream stays
(its CONTEXT.md collects one close-out line per run).

Usage: bash system/scripts/smoke-claude-dispatch.sh [--no-owner] [--timeout-min 12]
Run it from a checkout of the switchboard repo; it uses that checkout's scripts and
the main checkout's ledger. It posts two short "smoke test" callbacks into the CoS tab.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

# SMOKE_ROOT runs the steps against another checkout (main), so the owner steps use the
# owner-tab code that landed there and dispatch scaffolds switchboard-smoke on main, where
# dispatch.py looks for it; run from a worktree, new-workstream.py writes under the
# worktree and dispatch routes the ask to switchboard-maintenance instead.
ROOT = Path(os.environ.get("SMOKE_ROOT") or subprocess.run(["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True).stdout.strip() or ".")
SCRIPTS = ROOT / "system" / "scripts"
sys.path.insert(0, str(SCRIPTS))
SLUG = "switchboard-smoke"
CMUX = os.environ.get("CMUX_BIN") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
RESULTS: list[tuple[str, str, str]] = []


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = load("running_task_state", SCRIPTS / "running-task-state.py")


def record(step: str, ok: bool | None, evidence: str) -> None:
    verdict = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((step, verdict, evidence))
    print(f"[{verdict}] {step}: {evidence}", flush=True)


def sh(*cmd: str, timeout: int = 120, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, input=input_text, cwd=ROOT)


def last_json(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


def dispatch(prompt: str, *extra: str) -> dict:
    out = sh("bash", str(SCRIPTS / "router-dispatch.sh"), "--origin", "cos", *extra, prompt, timeout=180)
    return last_json(out.stdout) or {"status": "failed", "reason": (out.stderr or out.stdout)[-400:]}


def row(task_id: str) -> dict:
    return ledger.find_task(ledger.load_payload(), task_id) or {}


def tabs() -> list[dict]:
    out = sh(CMUX, "rpc", "workspace.list", "{}", timeout=20)
    try:
        return list(json.loads(out.stdout).get("workspaces") or [])
    except ValueError:
        return []


def tab_by_ref(ref: str) -> dict | None:
    return next((w for w in tabs() if w.get("ref") == ref), None)


def wait(predicate, timeout_s: float, every: float = 5.0):
    end = time.time() + timeout_s
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    return predicate()


def transcript_has(session_id: str, needle: str) -> bool:
    if not session_id:
        return False
    for path in CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"):
        try:
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def agent_in_tab(tab_id: str) -> dict:
    tab_id = tab_id.upper()
    return next((a for a in ledger.live_agents().values() if str(a.get("workspace_id") or "").upper() == tab_id), {})


def cos_tab() -> dict:
    titled = [w for w in tabs() if (w.get("custom_title") or w.get("title")) == "🧭 CoS"]
    try:
        reg = json.loads((ROOT / "system" / "state" / "cos-surface.json").read_text())
        titled = [w for w in tabs() if str(w.get("id") or "").upper() == str(reg.get("workspace_id") or "").upper()] or titled
    except (OSError, ValueError):
        pass
    return titled[0] if titled else {}


def worker_prompt(token: str, wait_for_steer: bool) -> str:
    steer = ("Then wait: a steer message carrying a second token (SMOKE-STEER-...) arrives within a few minutes by "
             "cmux agent message. When it arrives, put that second token in your report line too. If no steer "
             "arrives within 4 minutes, report with 'no steer' instead. " if wait_for_steer else "")
    return (f"Workstream: {SLUG}\n"
            f"switchboard smoke test {token} (smoke-claude-dispatch.py, no real work). Do nothing except what follows. "
            f"Edit no files and read nothing beyond this prompt. {steer}"
            f"Report with report-to-cos.sh --status done and the line 'Smoke test {token} (no action needed)' plus the "
            f"steer token if you got one. Do not write a review note.")


def step_hook(cos: dict) -> None:
    hook = ROOT / "system" / "claude-marketplace" / "plugins" / "switchboard" / "hooks" / "surface-context.py"
    if not cos:
        return record("hook", False, "no CoS tab open")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHBOARD_WORKER")}
    env.update(CMUX_WORKSPACE_ID=str(cos["id"]), SWITCHBOARD_ROOT=str(ROOT),
               SURFACE_CONTEXT_STATE_DIR=str(ROOT / ".amp" / "in" / "smoke-surface-context"))
    out = subprocess.run([sys.executable, str(hook), "session-start"], input='{"session_id":"smoke"}',
                         capture_output=True, text=True, env=env, timeout=20)
    card = (last_json(out.stdout).get("hookSpecificOutput") or {}).get("additionalContext") or ""
    record("hook", "router-dispatch.sh --origin cos" in card and "Workstream: <slug>" in card,
           f"CoS tab {cos.get('ref')} gets a {len(card)}-char dispatch card")


def step_worker(timeout_s: int, cos: dict) -> None:
    token, steer_token = f"SMOKE-{secrets.token_hex(3)}", f"SMOKE-STEER-{secrets.token_hex(3)}"
    sent = dispatch(worker_prompt(token, wait_for_steer=True))
    task_id = str(sent.get("task_id") or "")
    if sent.get("status") not in {"routed", "queued"} or not task_id:
        return record("launch", False, f"dispatch returned {sent.get('status')}: {sent.get('reason') or sent.get('message')}")
    if sent.get("launch") == "owner-tab":
        return record("launch", False, f"{task_id} went to an open {SLUG} owner tab; close it and rerun")
    bound = wait(lambda: row(task_id) if row(task_id).get("workspace_ref") else None, 120)
    ref = str((bound or {}).get("workspace_ref") or "")
    tab = wait(lambda: tab_by_ref(ref), 60) if ref else None
    agent = wait(lambda: agent_in_tab(str(tab["id"])) if tab else None, 120) if tab else {}
    claude = (bound or {}).get("agent") == "claude" and (agent or {}).get("agent") == "claude"
    record("launch", bool(tab) and claude,
           f"{task_id} in {ref or 'no tab'}, ledger agent {(bound or {}).get('agent')}, cmux agent {(agent or {}).get('agent')}")
    if not tab:
        return
    # Give the session a moment to register its cmux hooks, then steer it.
    time.sleep(20)
    steered = last_json(sh("bash", str(SCRIPTS / "router-dispatch.sh"), "--origin", "cos", "--steer", task_id,
                           f"Smoke steer: the second token is {steer_token}. Put it in your report line.",
                           timeout=120).stdout)
    record("steer", steered.get("status") == "delivered",
           f"status {steered.get('status')}, action {(steered.get('steer') or {}).get('action')}, "
           f"thread_live {steered.get('thread_live')}: {steered.get('message') or steered.get('reason')}")
    final = wait(lambda: row(task_id) if row(task_id).get("status") in ledger.TERMINAL_STATUSES else None, timeout_s, 10)
    summary = str((final or {}).get("summary") or "")
    record("steer read", steer_token in summary, f"report line: {summary[:160]!r}")
    delivered = wait(lambda: row(task_id).get("report_delivered") or None, 120)
    cos_session = str((agent_in_tab(str(cos.get("id") or "")) or {}).get("thread_id") or "")
    reached = wait(lambda: transcript_has(cos_session, token) or None, 180, 10)
    record("report", bool(delivered) and bool(reached),
           f"report_delivered={bool(delivered)}, token in CoS session {cos_session[:8] or '?'} transcript={bool(reached)}")
    gone = wait(lambda: tab_by_ref(ref) is None or None, 420, 15)
    if not gone:
        sh("python3", str(SCRIPTS / "dispatch.py"), "reap", timeout=180)
        gone = wait(lambda: tab_by_ref(ref) is None or None, 60, 10)
    # finish deletes the task branch once it is on main, so a missing branch on a done row is landed.
    branch_left = sh("git", "rev-parse", "--verify", "--quiet", f"task/{task_id}").returncode == 0
    landed = (sh("git", "merge-base", "--is-ancestor", f"task/{task_id}", "main").returncode == 0 if branch_left
              else row(task_id).get("status") == "done")
    record("reap", bool(gone) and landed, f"worker tab {ref} {'closed' if gone else 'still open'}; "
           f"{'branch on main' if branch_left and landed else 'landed, branch deleted' if landed else 'not landed'}")


def step_owner(timeout_s: int) -> None:
    if not (SCRIPTS / "owner_tabs.py").exists() or "owner_tab_dispatch" not in (SCRIPTS / "dispatch.py").read_text():
        for name in ("owner idle", "owner busy", "owner result"):
            record(name, None, "owner_tabs.py not in this checkout yet")
        return
    owner_tabs = load("owner_tabs", SCRIPTS / "owner_tabs.py")
    scaffolded_here = False
    if not (ROOT / "workstreams" / SLUG / "CONTEXT.md").exists():
        # First run on this checkout: one no-op dispatch scaffolds the workstream through
        # the normal path (dispatch.py commits the scaffold on main); wait for it to land.
        first = dispatch(worker_prompt(f"SMOKE-SCAFFOLD-{secrets.token_hex(3)}", wait_for_steer=False))
        first_id = str(first.get("task_id") or "")
        if first_id:
            wait(lambda: row(first_id).get("status") in ledger.TERMINAL_STATUSES or None, timeout_s, 10)
        scaffolded_here = (ROOT / "workstreams" / SLUG / "CONTEXT.md").exists()
        if not scaffolded_here:
            for name in ("owner idle", "owner busy", "owner result"):
                record(name, False, f"workstreams/{SLUG} was not scaffolded on {ROOT} ({first.get('status')} {first_id})")
            return
    opened_here = owner_tabs.owner_tab(SLUG, tabs(), root=ROOT) is None
    if opened_here:
        sh("bash", str(ROOT / "system" / "cmux-helpers.sh"), "launch", SLUG, timeout=180)
    tab = wait(lambda: owner_tabs.owner_tab(SLUG, tabs(), root=ROOT), 90)
    if not tab:
        for name in ("owner idle", "owner busy", "owner result"):
            record(name, False, "owner tab did not open")
        return
    ref = str(tab["ref"])
    idle = wait(lambda: (agent_in_tab(str(tab["id"])).get("state") == "Idle") or None, 240, 10)
    session = str(agent_in_tab(str(tab["id"])).get("thread_id") or "")
    try:
        token = f"SMOKE-OWNER-{secrets.token_hex(3)}"
        sent = dispatch(worker_prompt(token, wait_for_steer=False))
        record("owner idle", bool(idle) and sent.get("launch") == "owner-tab" and sent.get("workspace_ref") == ref
               and bool(wait(lambda: transcript_has(session, token) or None, 120)),
               f"dispatch launch={sent.get('launch')} to {sent.get('workspace_ref')}; owner tab {ref} idle={bool(idle)}")
        # Busy: the owner tab is now working that ask (or give it a long turn), so the next ask is parallel.
        owner_tabs.agent_message(ref, "Smoke test: run `sleep 90` in Bash now, then reply 'slept'.", sender="smoke")
        time.sleep(15)
        token2 = f"SMOKE-PAR-{secrets.token_hex(3)}"
        sent2 = dispatch(worker_prompt(token2, wait_for_steer=False))
        par_id = str(sent2.get("task_id") or "")
        record("owner busy", sent2.get("launch") != "owner-tab" and sent2.get("status") in {"routed", "queued"},
               f"busy owner tab: dispatch {sent2.get('status')} launch={sent2.get('launch') or 'worker'} ({par_id})")
        if par_id:
            wait(lambda: row(par_id).get("status") in ledger.TERMINAL_STATUSES or None, timeout_s, 10)
            record("owner result", bool(wait(lambda: transcript_has(session, token2) or None, 240, 10)),
                   f"parallel worker {par_id} result card in owner session {session[:8]} transcript")
    finally:
        if opened_here:
            sh("bash", str(SCRIPTS / "command-center-action.sh"), "close", SLUG, timeout=180)
        if scaffolded_here:
            sh("bash", str(SCRIPTS / "command-center-action.sh"), "archive", SLUG, timeout=180)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-owner", action="store_true", help="skip the owner-tab steps")
    parser.add_argument("--timeout-min", type=float, default=12)
    parser.add_argument("--only", choices=("all", "worker", "owner"), default="all",
                        help="owner: only the owner-tab steps (pair with SMOKE_ROOT=<main checkout>)")
    args = parser.parse_args()
    timeout_s = int(args.timeout_min * 60)
    agent = sh("python3", str(SCRIPTS / "switchboard_agent.py")).stdout.strip()
    record("agent knob", agent == "claude", f"switchboard_agent.py resolves {agent or '?'}")
    if args.only != "owner":
        cos = cos_tab()
        step_hook(cos)
        step_worker(timeout_s, cos)
    if args.no_owner or args.only == "worker":
        record("owner", None, "--no-owner")
    else:
        step_owner(timeout_s)
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print(json.dumps({"smoke": "claude-dispatch", "passed": sum(r[1] == "PASS" for r in RESULTS),
                      "failed": len(failed), "skipped": sum(r[1] == "SKIP" for r in RESULTS),
                      "steps": [{"step": s, "verdict": v, "evidence": e} for s, v, e in RESULTS]}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
