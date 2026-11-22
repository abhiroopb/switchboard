#!/usr/bin/env python3
"""Tests for command-center-digest.py v3 (summarized, action-ranked digest).

Hermetic: builds a temp repo root + fixtures and drives the generator via the
CC_* env overrides (CC_REPO_ROOT, CC_TODO_FILE, CC_WORKSPACES_JSON,
CC_LANES_JSON, CC_TODAY). No live cmux, no live todo.json, no network.

Covers the launch-prompt requirements:
  1. pinned filtering        — pinned=true workspaces excluded entirely
  2. category ranking        — needs-attention/requires_user → Act now;
                               review lane alone does NOT promote P2 tabs
  3. P0 precedence           — "P0 ·" title renders 🔴 P0 despite staleness
  4. concise low-prio rollup — background tabs get ONE group row, not full rows
  5. stable action handles   — `open <n>` / `close <n>` match workspace refs
Plus regressions:
  6. future-snoozed requires_user todos don't count as "today"
  7. hard-wrapped cmux descriptions can't break the markdown table
  8. CoS workspace itself is excluded
 10. Amp agent state join — cmux session store maps workspace id → Amp
     thread + Running/Idle; running agents render ⚡ markers; malformed
     session stores degrade silently

Run:  python3 system/scripts/test-command-center-digest.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "command-center-digest.py"
TODAY = "2026-07-21"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def make_workstream(root: Path, slug: str, name: str, description: str = "") -> None:
    d = root / "workstreams" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'name: "{name}"\nstatus: active\npriority: medium\n'
        + (f'description: "{description}"\n' if description else ""),
        encoding="utf-8",
    )
    (d / "CONTEXT.md").write_text(
        f"# {name}\n\n## Next Steps\n- Do the next {slug} thing\n", encoding="utf-8"
    )
    # Make everything look stale (P0 precedence must survive staleness)
    old = 1_500_000_000  # 2017
    for f in d.iterdir():
        os.utime(f, (old, old))


def ws(ref_num: int, title: str, pinned: bool = False, description: str = "",
       id: str = "") -> dict:
    return {
        "ref": f"workspace:{ref_num}",
        "id": id,
        "index": ref_num,
        "title": title,
        "custom_title": title,
        "pinned": pinned,
        "selected": False,
        "description": description,
        "latest_conversation_message": None,
        "latest_submitted_message": None,
    }


def session_ws(workspace_id: str, thread_id: str = "", state: str = "",
               ts: float = 0.0) -> dict:
    """cmux session-store workspace entry (Amp agent join source)."""
    panels = []
    if thread_id:
        panels.append({"terminal": {"agent": {"kind": "amp", "sessionId": thread_id}}})
    entries = []
    if state:
        entries.append({"key": "amp", "value": state, "timestamp": ts})
    return {"workspaceId": workspace_id, "panels": panels, "statusEntries": entries}


def run_digest(root: Path, workspaces: list[dict], lanes: dict, todos: list[dict],
               session: dict | str | None = None) -> str:
    (root / "system" / "state").mkdir(parents=True, exist_ok=True)
    ws_file = root / "fixture-workspaces.json"
    ws_file.write_text(json.dumps({"workspaces": workspaces}), encoding="utf-8")
    lanes_file = root / "fixture-lanes.json"
    lanes_file.write_text(json.dumps(lanes), encoding="utf-8")
    todo_file = root / "fixture-todo.json"
    todo_file.write_text(json.dumps({"items": todos}), encoding="utf-8")
    # Always pin the session store to a fixture so tests never read the live
    # cmux session on this machine. Pass a str to write raw (corrupt) content.
    session_file = root / "fixture-session.json"
    if isinstance(session, str):
        session_file.write_text(session, encoding="utf-8")
    else:
        session_file.write_text(json.dumps(session or {"windows": []}), encoding="utf-8")
    env = dict(
        os.environ,
        PM_OS_COMMS_TRIAGE="on",  # the machine-local off switch must not change these cases
        CC_REPO_ROOT=str(root),
        CC_TODO_FILE=str(todo_file),
        CC_WORKSPACES_JSON=str(ws_file),
        CC_LANES_JSON=str(lanes_file),
        CC_CMUX_SESSION_JSON=str(session_file),
        CC_TODO_CLI=str(Path(__file__).resolve().parents[1] / "skill-overrides" / "todo" / "scripts" / "todo_cli.py"),
        CC_TODAY=TODAY,
        PM_OS_TODO_SURFACE_SKIP="1",
    )
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if out.returncode != 0:
        raise RuntimeError(f"digest exited {out.returncode}: {out.stderr}")
    return out.stdout


def section_of(digest: str, row_marker: str) -> str:
    """Which section divider a line containing row_marker falls under."""
    current = "(none)"
    for line in digest.splitlines():
        if "**— " in line:
            if "Act now" in line:
                current = "act"
            elif "Important" in line:
                current = "important"
            elif "Background" in line:
                current = "background"
        elif row_marker in line:
            return current
    return "(missing)"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cc-digest-test-") as td:
        root = Path(td)
        make_workstream(root, "alpha", "Alpha")
        make_workstream(root, "beta", "Beta")
        make_workstream(root, "gamma", "Gamma")
        make_workstream(root, "delta", "Delta")

        workspaces = [
            ws(1, "🧭 CoS", pinned=True),
            ws(2, "📌 Pinned Sidekick", pinned=True),
            ws(3, "🚀 P0 · Alpha"),                # P0 title, review lane
            ws(4, "🅱️ Beta"),                       # P1 requires_user todo
            ws(5, "🇬 Gamma"),                       # needs-attention lane
            ws(6, "🇩 Delta"),                       # P2, review lane only
            ws(7, "stray agent tab - amp - ~/x"),   # unlinked, review lane
            ws(8, "another scratch tab - amp"),     # unlinked, no lane
            ws(9, "wrapped desc tab - amp",         # unlinked; newline description
               description="line one\nline two\nline three"),
        ]
        # Note: Delta (ws:6) gets NO lane — its only signal is a future-snoozed
        # requires_user todo, isolating the snooze rule. (A review lane would
        # legitimately promote a P1 workspace regardless of todo snooze.)
        lanes = {
            "workspace:3": "review",
            "workspace:5": "needs-attention",
            "workspace:7": "review",
        }
        todos = [
            {"id": "t1", "title": "Approve the Beta rollout", "status": "open",
             "priority": "P1", "workstream": "beta", "requires_user": True},
            {"id": "t2", "title": "Alpha P0 commitment", "status": "open",
             "priority": "P0", "workstream": "alpha"},
            # Future-snoozed requires_user item must NOT surface as today
            {"id": "t3", "title": "Snoozed Delta ask", "status": "open",
             "priority": "P1", "workstream": "delta", "requires_user": True,
             "snooze_until": "2026-08-15"},
        ]

        digest = run_digest(root, workspaces, lanes, todos)

        print("== 1. pinned filtering ==")
        check("pinned workspace absent", "Pinned Sidekick" not in digest)
        check("pinned hidden count", "1 pinned hidden" in digest,
              "(CoS excluded separately, doesn't count)")

        print("== 2. category ranking ==")
        check("needs-attention → act", section_of(digest, "Gamma") == "act",
              f"got {section_of(digest, 'Gamma')}")
        check("requires_user → act", section_of(digest, "Beta") == "act",
              f"got {section_of(digest, 'Beta')}")
        # Note: the redundant "P0 · " title prefix is stripped from the
        # workspace cell (urgency carries it), so match on "🚀 Alpha".
        check("review-lane P0 → act", section_of(digest, "🚀 Alpha") == "act",
              f"got {section_of(digest, '🚀 Alpha')}")
        check("review-lane P2 NOT act", section_of(digest, "stray agent tab") != "act",
              f"got {section_of(digest, 'stray agent tab')}")
        act_idx = digest.index("Act now")
        check("needs-attention ranks above requires_user",
              digest.index("Gamma", act_idx) < digest.index("Beta", act_idx))

        print("== 3. P0 precedence ==")
        alpha_row = next(ln for ln in digest.splitlines() if "🚀 Alpha" in ln)
        check("P0 title renders 🔴 P0 despite stale dir", "🔴 P0" in alpha_row, alpha_row)

        print("== 4. concise low-priority rollup ==")
        check("unlinked tabs land in background",
              section_of(digest, "Parked / unclear tabs") == "background")
        parked_rows = [ln for ln in digest.splitlines() if "Parked / unclear tabs" in ln]
        check("parked tabs share ONE group row", len(parked_rows) == 1,
              f"got {len(parked_rows)}")
        check("group row counts all parked tabs", "Parked / unclear tabs (3)" in digest)
        full_stray = [ln for ln in digest.splitlines()
                      if ln.startswith("| 7 |") or ln.startswith("| 8 |")]
        check("no full rows for parked tabs", not full_stray, str(full_stray))

        print("== 5. stable action handles ==")
        check("open handle for act row", "`open 5`" in digest)
        check("close handle for act row", "`close 5`" in digest)
        check("open handle inside rollup", "`open 7`" in digest)
        gamma_row = next(ln for ln in digest.splitlines() if "Gamma" in ln and ln.startswith("|"))
        check("handle number matches ref", "`open 5`" in gamma_row and "`close 5`" in gamma_row,
              gamma_row)
        # EVERY full (actionable) row must carry literal `open <n>` + `close <n>`
        # matching its own ref number — clarified invariant 7.
        import re as _re
        bad_handles = []
        for ln in digest.splitlines():
            m = _re.match(r"^\| (\d+) \|", ln)
            if m:
                n = m.group(1)
                if f"`open {n}`" not in ln or f"`close {n}`" not in ln:
                    bad_handles.append(ln)
        check("all full rows keep literal open/close handles", not bad_handles,
              str(bad_handles[:2]))

        print("== 6. snoozed items stay quiet ==")
        check("future-snoozed requires_user NOT act",
              section_of(digest, "Delta") != "act",
              f"got {section_of(digest, 'Delta')}")

        print("== 7. table integrity (5-column chat-width layout) ==")
        table_lines = [ln for ln in digest.splitlines() if ln.startswith("|")]
        check("5-column header",
              "| # | Workspace | Status / Next | Debt | Actions |" in digest)
        bad = [ln for ln in table_lines if ln.count("|") != 6]
        check("every table row has 5 columns", not bad, str(bad[:2]))
        check("no separate Urgency/Open/Close columns",
              "| Urgency |" not in digest and "| Open | Close |" not in digest)
        check("no literal newline leaked from description", "line one\nline two" not in
              "\n".join(table_lines))
        # Urgency is folded into the Workspace cell without duplicating the
        # priority token ("🔴 P0 · 🚀 Alpha", never "🔴 P0 · 🚀 P0 · Alpha").
        alpha_ws_cell = alpha_row.split("|")[2]
        check("urgency folded into workspace cell", "🔴 P0" in alpha_ws_cell,
              alpha_ws_cell)
        check("no duplicated P0 in workspace cell", alpha_ws_cell.count("P0") == 1,
              alpha_ws_cell)
        # Actions cell combines both handles: `open <n>` · `close <n>`
        check("combined Actions cell", "`open 3` · `close 3`" in alpha_row, alpha_row)
        # Debt is a compact "total · N Pn" summary, not a per-priority tuple
        alpha_debt_cell = alpha_row.split("|")[4].strip()
        check("debt compact summary", alpha_debt_cell == "1 · 1 P0", alpha_debt_cell)

        print("== 8. CoS excluded ==")
        check("CoS row absent", not any("🧭 CoS" in ln for ln in table_lines))

        # Cap behavior: flood with 8 requires_user P0 workspaces → 5 act + overflow
        print("== 9. act-now cap + overflow ==")
        many_ws = [ws(1, "🧭 CoS", pinned=True)]
        many_todos = []
        for i in range(10, 18):
            slug = f"flood{i}"
            make_workstream(root, slug, f"Flood{i}")
            many_ws.append(ws(i, f"🌊 Flood{i}"))
            many_todos.append({
                "id": f"f{i}", "title": f"Flood ask {i}", "status": "open",
                "priority": "P0", "workstream": slug, "requires_user": True,
            })
        digest2 = run_digest(root, many_ws, {}, many_todos)
        check("act section capped at 5", "Act now (5)" in digest2)
        check("overflow lands in important", "Important · not immediate (3)" in digest2)

        # Amp agent state join via cmux session store fixture
        print("== 10. Amp agent state join ==")
        import time as _time
        now = _time.time()
        a2a_ws = [
            ws(30, "🇬 Gamma", id="AAA"),                # running, fresh
            ws(31, "🅱️ Beta", id="BBB"),                 # running for ~65 min
            ws(32, "parked amp tab - amp", id="CCC"),    # running, background
            ws(33, "idle amp tab - amp", id="DDD"),      # idle, background
            ws(34, "plain shell tab", id="EEE"),         # non-amp panel
            ws(35, "unmapped tab"),                      # no session entry at all
        ]
        a2a_lanes = {"workspace:30": "needs-attention"}
        a2a_todos = [
            {"id": "t1", "title": "Approve the Beta rollout", "status": "open",
             "priority": "P1", "workstream": "beta", "requires_user": True},
        ]
        session = {"windows": [{"tabManager": {"workspaces": [
            session_ws("AAA", "T-aaa", "Running", now - 60),
            session_ws("BBB", "T-bbb", "Running", now - 3900),
            session_ws("CCC", "T-ccc", "Running", now - 60),
            session_ws("DDD", "T-ddd", "Idle", now - 60),
            {"workspaceId": "EEE",
             "panels": [{"terminal": {"agent": {"kind": "claude", "sessionId": "x"}}}],
             "statusEntries": []},
        ]}}]}
        digest3 = run_digest(root, a2a_ws, a2a_lanes, a2a_todos, session=session)
        gamma_row = next(ln for ln in digest3.splitlines() if ln.startswith("| 30 |"))
        check("running agent marker on act row", "⚡ agent running" in gamma_row, gamma_row)
        check("fresh running agent shows no age",
              not _re.search(r"agent running \d+[hm]", gamma_row), gamma_row)
        beta_row = next(ln for ln in digest3.splitlines() if ln.startswith("| 31 |"))
        check("long-running agent shows age",
              bool(_re.search(r"⚡ agent running 1h0[4-6]m", beta_row)), beta_row)
        check("65-min agent is not flagged stale", "stale — verify" not in beta_row, beta_row)
        # Stale-state guard: a Running state_ts that
        # is ≥120 min old is a stale cmux timestamp, not progress.
        stale_session = {"windows": [{"tabManager": {"workspaces": [
            session_ws("BBB", "T-bbb", "Running", now - 3 * 3600 - 60),
        ]}}]}
        digest3s = run_digest(root, a2a_ws, a2a_lanes, a2a_todos, session=stale_session)
        beta_stale = next(ln for ln in digest3s.splitlines() if ln.startswith("| 31 |"))
        check("≥120-min running agent flagged stale — verify",
              bool(_re.search(r"⚡ agent running 3h0[0-2]m · stale — verify", beta_stale)),
              beta_stale)
        parked_group = next(ln for ln in digest3.splitlines()
                            if "Parked / unclear tabs" in ln)
        check("running background tab gets ⚡ in rollup",
              "⚡ parked amp tab" in parked_group, parked_group)
        check("idle background tab has no ⚡",
              "⚡ idle amp tab" not in parked_group, parked_group)
        check("non-amp agent panel ignored",
              "⚡ plain shell tab" not in parked_group, parked_group)
        # Header rollup: AAA + BBB + CCC running (pinned would count too)
        check("header shows global running-agent count",
              "⚡ 3 agents running ·" in digest3, digest3.splitlines()[2])
        # SLUG_INDEX carries thread ids for downstream thread jumps
        slug_idx_line = next(ln for ln in digest3.splitlines() if "SLUG_INDEX" in ln)
        check("SLUG_INDEX carries thread_id", "T-aaa" in slug_idx_line, slug_idx_line)
        check("SLUG_INDEX marks running agents", '"agent": "running"' in slug_idx_line,
              slug_idx_line)
        # Corrupt session store: digest still renders, with no markers
        digest4 = run_digest(root, a2a_ws, a2a_lanes, a2a_todos, session="{not json")
        check("corrupt session store degrades silently",
              "⚡ agent running" not in digest4 and "Act now" in digest4)
        check("no header agent count when none running", "agents running" not in digest4)

        # Terminal renderer at narrow width must not split or truncate the
        # combined action handles (they must stay copyable).
        print("== 11. narrow-width renderer keeps actions atomic ==")
        render_script = SCRIPT.parent / "command-center-render.py"
        digest_file = root / "digest-under-test.md"
        digest_file.write_text(digest, encoding="utf-8")
        rendered = subprocess.run(
            [sys.executable, str(render_script), str(digest_file)],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, COLUMNS="90", LINES="50"),
        ).stdout
        check("rendered table has Actions column", "Actions" in rendered)
        check("act-row handles unbroken at width 90",
              "open 5 · close 5" in rendered, rendered[-500:])
        check("no legacy Urgency column in render", "Urgency" not in rendered)

        # CoS chat uses list-with-headers while the stored snapshot and
        # terminal sidecar keep the table contract above.
        print("== 12. chat renderer uses lists without changing the snapshot ==")
        digest_file.write_text(
            digest
            + "\n\n## ⚠️ Routed-task verify sweep\n"
            + "- **alpha**: needs attention\n",
            encoding="utf-8",
        )
        chat = subprocess.run(
            [sys.executable, str(render_script), "--chat", str(digest_file)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        check("chat output has no markdown table", "| # | Workspace |" not in chat)
        check("chat output keeps emoji section order",
              chat.index("=== 🔴 Act now") < chat.index("=== 🟠 Important")
              < chat.index("=== ⚪ Background"))
        check("chat output keeps one topic per line",
              all("<br" not in line for line in chat.splitlines() if line.startswith("- ")))
        check("chat output keeps literal action handles",
              "`open 5` · `close 5`" in chat)
        check("chat output keeps background grouped",
              len([line for line in chat.splitlines()
                   if "Parked / unclear tabs" in line]) == 1)
        check("chat output keeps appended warnings",
              "=== ⚠️ Routed-task verify sweep ===" in chat
              and "- **alpha**: needs attention" in chat)
        check("chat output hides machine-only slug index", "SLUG_INDEX" not in chat)

        # Every render must persist handle→workspace provenance so the action
        # script can reject stale numeric handles after cmux reuses a number.
        print("== 13. render-time action map ==")
        map_ws = [
            ws(7, "📉 Multi-entity", id="uuid-multi-entity"),
            ws(9, "🧭 CoS", id="uuid-cos"),
        ]
        run_digest(root, map_ws, {}, [])
        actions_file = root / "system" / "state" / "command-center-actions.json"
        check("actions map written", actions_file.exists())
        amap = json.loads(actions_file.read_text(encoding="utf-8"))
        h7 = (amap.get("handles") or {}).get("7") or {}
        check("handle 7 carries workspace uuid", h7.get("id") == "uuid-multi-entity", str(h7))
        check("handle 7 carries title", h7.get("title") == "📉 Multi-entity", str(h7))
        check("actions map has generated_at", bool(amap.get("generated_at")))

        print("== 14. held comms drafts stay in Act now until disposition ==")
        doc_ref = "https://docs.google.com/document/d/doc-1/edit?disco=anchor-1"
        ledger_file = root / "system" / "state" / "triage-ledger.json"
        held_item = {
            "ref": doc_ref,
            "source": "google-docs",
            "first_seen": "2026-07-19T12:00:00Z",
            "stage": "acknowledged",
            "updated_at": "2026-07-19T12:00:00Z",
            "draft_status": "held",
            "draft_held_at": "2026-07-19T12:00:00Z",
            "draft_meta": {"summary": "Draft ready: approve to post"},
        }
        ledger_file.write_text(json.dumps({"version": 1, "items": {doc_ref: held_item}}))
        draft_todos = [
            {
                "id": "draft-1",
                "title": "Reply to Juniper's comment",
                "status": "open",
                "priority": "P1",
                "due": "2026-07-20",
                "workstream": "alpha",
                "source_ref": doc_ref + "&usp=comment_email",
            }
        ]
        held_digest = run_digest(
            root,
            [ws(5, "🇬 Gamma"), ws(50, "📬 Comms Triage")],
            {"workspace:5": "needs-attention"},
            draft_todos,
        )
        check("held draft appears in Act now", section_of(held_digest, "Held comms drafts") == "act")
        check("held draft uses exact status text", "Draft ready: approve to post" in held_digest)
        check("overdue P1 is escalated", "overdue P1" in held_digest)
        check(
            "held draft ranks before other Act-now rows",
            held_digest.index("Held comms drafts") < held_digest.index("Gamma"),
        )
        held_item["draft_status"] = "drafted"
        ledger_file.write_text(json.dumps({"version": 2, "items": {doc_ref: held_item}}))
        drafted = run_digest(root, [ws(50, "📬 Comms Triage")], {}, draft_todos)
        check("drafted response also stays in Act now", "Held comms drafts" in drafted)
        for final_status in ("approved", "edited", "sent", "dismissed"):
            held_item["draft_status"] = final_status
            ledger_file.write_text(json.dumps({"version": 2, "items": {doc_ref: held_item}}))
            cleared = run_digest(root, [ws(50, "📬 Comms Triage")], {}, draft_todos)
            check(
                f"{final_status} draft clears from Act now",
                "Held comms drafts" not in cleared,
            )

        # An unreachable cmux must fail closed (non-zero), never render an
        # empty "nothing urgent" board that clobbers the last good digest.
        print("== 15. workspace.list failure fails closed ==")
        env = dict(
            os.environ,
            PM_OS_COMMS_TRIAGE="on",  # the machine-local off switch must not change these cases
        CC_REPO_ROOT=str(root),
            CC_TODO_FILE=str(root / "fixture-todo.json"),
            CC_WORKSPACES_JSON=str(root / "does-not-exist.json"),
            CC_LANES_JSON=str(root / "fixture-lanes.json"),
            CC_CMUX_SESSION_JSON=str(root / "fixture-session.json"),
            CC_TODAY=TODAY,
        )
        out = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        check("digest exits non-zero when workspace listing fails",
              out.returncode != 0, f"rc={out.returncode}")
        check("no digest rendered on listing failure",
              "Command Center" not in out.stdout, out.stdout[:200])

        # A thing is named, never a bare number. A PR named in a workstream's Next Steps leads with its cached title;
        # the number follows in parentheses. Cache-only here (no network).
        print("== 16. status text names a PR, the number follows ==")
        make_workstream(root, "named-pr", "Named PR")
        (root / "workstreams" / "named-pr" / "CONTEXT.md").write_text(
            "# Named PR\n\n## Next Steps\n- Land example-repo#10001 after review\n", encoding="utf-8",
        )
        cache = root / "named-refs-cache.json"
        cache.write_text(json.dumps({
            "https://github.com/acme/example-repo/pull/10001": {"title": "Docs search skill", "at": 4102444800},
        }), encoding="utf-8")
        os.environ["NAMED_REFS_CACHE"] = str(cache)
        os.environ["NAMED_REFS_OFFLINE"] = "1"
        try:
            digest = run_digest(root, [ws(60, "🏷️ Named PR")], {"workspace:60": "needs-attention"}, [])
        finally:
            os.environ.pop("NAMED_REFS_CACHE", None)
            os.environ.pop("NAMED_REFS_OFFLINE", None)
        check("PR title leads, number in parentheses",
              "[Docs search skill](https://github.com/acme/example-repo/pull/10001) (example-repo#10001)" in digest,
              digest[:800])
        check("bare PR number no longer stands alone",
              "Land example-repo#10001" not in digest, digest[:800])

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("✅ all command-center-digest tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
