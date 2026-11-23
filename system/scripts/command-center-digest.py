#!/usr/bin/env python3
"""Generate the CoS command-center digest — v3 (summarized, action-ranked).

Invariants (the owner's durable presentation requirement):
  1. Workspaces with live cmux `pinned=true` metadata are EXCLUDED entirely.
  2. Section 1 "🔴 Act now" — only the top items to act on immediately or
     finish quickly: needs-attention lane, requires_user todos, overdue /
     due-today P0-P1, review-lane P0/P1 (review alone does NOT promote
     low-priority tabs), quick (<15 min) P0/P1 wins. Capped at 5 rows.
  3. Section 2 "🟠 Important" — important but not immediate: remaining active
     P0/P1 commitments and upcoming (≤7 d) deadlines.
  4. Section 3 "⚪ Background" — lower-importance / agent-owned / unclear /
     parkable tabs summarized compactly (grouped rollup rows, NOT one full
     row each).
  5. P0 precedence: a "P0 ·" workspace title or any linked open P0 todo makes
     the row 🔴 P0, even when staleness heuristics would say P2.
  6. Prefer concrete next-action text (linked todo, CONTEXT.md next step,
     config/cmux description, last conversation message) over generic
     "no next step" filler.
  7. `open <n>` / `close <n>` action handles preserved for actionable rows
     (combined into one `Actions` cell so they never wrap at chat width).
  8. Stored output stays concise and table-first (single table, section divider
     rows) for the sidecar and action tooling. CoS chat converts that snapshot
     to list-with-headers via command-center-render.py --chat.
  9. Stored layout: five columns only —
     `# | Workspace | Status / Next | Debt | Actions`. Urgency is folded into
     the Workspace label (no duplicate "P0"), debt is a compact
     "total · N Pn" summary, never a per-priority tuple.

Test overrides (all optional, used by test-command-center-digest.py):
  CC_REPO_ROOT        repo root override (workstreams/, routines/, notes/, state/)
  CC_TODO_FILE        todo.json path override
  CC_WORKSPACES_JSON  fixture file replacing `cmux rpc workspace.list`
  CC_LANES_JSON       fixture file mapping workspace ref -> lane
  CC_CMUX_SESSION_JSON fixture file replacing the cmux session store
                      (workspace id -> Amp thread/status join source)
  CC_TODAY            YYYY-MM-DD override for due-date math

Usage:
  python3 system/scripts/command-center-digest.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from knowledge_snapshot import load_snapshot
import comms_triage_setting
import named_refs
import review_surface
from lib.mux import runnable as _runnable  # Windows: the extensionless mux/cmux wrapper runs via Python

REPO_ROOT = Path(os.environ.get("CC_REPO_ROOT") or Path(__file__).resolve().parents[2])
WORKSTREAMS_DIR = REPO_ROOT / "workstreams"
ROUTINES_DIR = REPO_ROOT / "routines"
DAILY_NOTES_DIR = REPO_ROOT / "notes" / "daily"
TODO_FILE = Path(os.environ.get("CC_TODO_FILE") or REPO_ROOT / "system" / "state" / "todo.json")
TODO_CLI = Path(
    os.environ.get("CC_TODO_CLI")
    or Path.home() / ".agents" / "skills" / "todo" / "scripts" / "todo_cli.py"
)
STATE_DIR = REPO_ROOT / "system" / "state"
TRIAGE_LEDGER_FILE = Path(
    os.environ.get("CC_TRIAGE_LEDGER_FILE") or STATE_DIR / "triage-ledger.json"
)
HISTORY_DIR = STATE_DIR / "command-center-history"
CMUX_LINKS_DIR = STATE_DIR / "cmux-links"
CMUX_BIN = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")
CMUX_SESSION_STORE = Path(os.environ.get("SWITCHBOARD_CMUX_SESSION_JSON") or Path.home() / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json")
# cmux session-store agent kind -> statusEntries key (mirrors
# workspace-session-state.py AGENT_STATUS_KEYS).
AGENT_STATUS_KEYS = {"amp": "amp", "claude": "claude_code"}
# Minutes a cmux "Running" agent state may age before the digest flags it as
# stale. Override: CC_STALE_AGENT_MIN.
STALE_AGENT_MIN = int(os.environ.get("CC_STALE_AGENT_MIN", "120"))

ROUTINE_ORDER = ["todo", "comms-triage", "meetings"]

ROUTINE_META = {
    "todo": ("✅", "Persistent to-do list with proactive reminders, dedupe, calendar sync"),
    "comms-triage": ("📬", "Slack + Gmail + Linear + Jira + Calendar inbound triage"),
    "meetings": ("📅", "Pre-meeting prep + Gemini note ingestion + follow-up extraction"),
}

COS_PATTERNS = ["🧭 CoS", "🧭 Cos", "Chief of Staff"]

PRIO_EMOJI = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "⚪ P3"}
PRIO_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


# ───────────────────────── cmux helpers ─────────────────────────

def today_date() -> date:
    override = os.environ.get("CC_TODAY")
    if override:
        try:
            return datetime.strptime(override, "%Y-%m-%d").date()
        except Exception:
            pass
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def cmux_list_workspaces() -> list[dict]:
    """Workspace metadata via `cmux rpc workspace.list` (includes pinned).

    CC_WORKSPACES_JSON (test fixture): path to a file with the same shape as
    the rpc response — {"workspaces": [{ref, title, custom_title, pinned,
    description, latest_conversation_message, ...}]}.
    """
    fixture = os.environ.get("CC_WORKSPACES_JSON")
    try:
        if fixture:
            data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        else:
            out = subprocess.check_output(
                _runnable([CMUX_BIN, "rpc", "workspace.list", "{}"]), text=True, timeout=15
            )
            data = json.loads(out)
    except Exception as e:
        # Fail closed: an unreachable cmux must NOT render an empty board,
        # or the loop would clobber the last good digest with "nothing
        # urgent" while real workstreams are still live.
        print(f"[digest] cmux workspace.list failed: {e}", file=sys.stderr)
        sys.exit(3)
    rows = []
    for w in data.get("workspaces", []):
        ref = w.get("ref") or ""
        if not re.match(r"^workspace:\d+$", ref):
            continue
        rows.append({
            "ref": ref,
            "id": (w.get("id") or "").strip(),
            "name": (w.get("custom_title") or w.get("title") or "").strip(),
            "pinned": bool(w.get("pinned")),
            "selected": bool(w.get("selected")),
            "cmux_description": (w.get("description") or "").strip(),
            "last_msg": (w.get("latest_conversation_message")
                         or w.get("latest_submitted_message") or "").strip(),
        })
    return rows


def amp_agent_map() -> dict[str, dict]:
    """Structured Amp agent state per cmux workspace, joined from the cmux
    session store (written continuously by cmux; the Amp side is bridged by
    the cmux-managed ~/.config/amp/plugins/cmux-session.ts hook).

    Returns {workspace_id: {"thread_id": str, "state": "Running"|"Idle",
    "state_ts": float}}. workspace_id joins to `workspace.list` rows via
    their "id" field. Best-effort: any missing/corrupt store yields {} and
    the digest degrades to today's cmux-only behavior.

    CC_CMUX_SESSION_JSON (test fixture): path to a file with the same shape
    as the session store — {"windows": [{"tabManager": {"workspaces": [...]}}]}.
    """
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
            # Same join as workspace-session-state.py amp_agents(): an Amp tab
            # (kind "amp", status key "amp") or a Claude Code tab (kind
            # "claude", status key "claude_code", session uuid also on
            # resumeBinding.checkpointId). thread_id is whichever id the tab runs.
            thread_id = ""
            agent_kind = ""
            for panel in ws.get("panels") or []:
                terminal = panel.get("terminal") or {}
                agent = terminal.get("agent") or {}
                kind = str(agent.get("kind") or "")
                if kind not in AGENT_STATUS_KEYS:
                    continue
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
                continue  # workspace runs neither agent
            agents[wid] = {
                "thread_id": thread_id,
                "state": str((status or {}).get("value") or ""),
                "state_ts": float((status or {}).get("timestamp") or 0),
            }
    return agents


_LANES_CACHE: dict[str, str] | None = None


def workspace_lane(ref: str) -> str:
    """Effective status lane for a workspace: todo|working|needs-attention|
    review|done|"" (unknown). CC_LANES_JSON (test fixture): {ref: lane}."""
    global _LANES_CACHE
    fixture = os.environ.get("CC_LANES_JSON")
    if fixture:
        if _LANES_CACHE is None:
            try:
                _LANES_CACHE = json.loads(Path(fixture).read_text(encoding="utf-8"))
            except Exception:
                _LANES_CACHE = {}
        return _LANES_CACHE.get(ref, "")
    if _LANES_CACHE is not None and ref in _LANES_CACHE:
        return _LANES_CACHE[ref]
    return _fetch_lane(ref)


def _fetch_lane(ref: str) -> str:
    try:
        out = subprocess.check_output(
            _runnable([CMUX_BIN, "workspace", "status", "--workspace", ref]),
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        first = out.splitlines()[0].strip() if out.splitlines() else ""
        return first
    except Exception:
        return ""


def prefetch_lanes(refs: list[str]) -> None:
    """Run the per-workspace `cmux workspace status` calls concurrently.

    Serial calls cost seconds per tick; a small thread pool brings that
    under a second. No-op under the CC_LANES_JSON fixture.
    """
    global _LANES_CACHE
    if os.environ.get("CC_LANES_JSON"):
        return
    if _LANES_CACHE is None:
        _LANES_CACHE = {}
    todo = [r for r in refs if r not in _LANES_CACHE]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ref, lane in zip(todo, pool.map(_fetch_lane, todo)):
            _LANES_CACHE[ref] = lane


def is_cos(name: str) -> bool:
    return any(p in name for p in COS_PATTERNS)


P0_TITLE_RE = re.compile(r"\bP0\s*[·:]")


def is_p0_titled(name: str) -> bool:
    """Workspace title carries the ensure-p0-workspaces 'P0 ·' contract tag."""
    return bool(P0_TITLE_RE.search(name))


# ───────────────────────── workstream lookup ─────────────────────────

def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"\s+", "-", s)


_SLUG_CANDIDATES: tuple[list[tuple[str, str]], set[str]] | None = None


def _slug_candidates() -> tuple[list[tuple[str, str]], set[str]]:
    """(slug, config name) pairs and the set of known slugs, read once per
    process, never once per workspace."""
    global _SLUG_CANDIDATES
    if _SLUG_CANDIDATES is not None:
        return _SLUG_CANDIDATES
    candidates: list[tuple[str, str]] = []
    known_slugs: set[str] = set()
    for parent in (WORKSTREAMS_DIR, ROUTINES_DIR):
        if not parent.exists():
            continue
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            cfg = entry / "config.yaml"
            if not cfg.exists():
                continue
            known_slugs.add(entry.name)
            try:
                text = cfg.read_text(encoding="utf-8")
                m = re.search(r'^name:\s*"?([^"\n]+?)"?\s*$', text, re.MULTILINE)
                if m:
                    candidates.append((entry.name, m.group(1).strip()))
            except Exception:
                continue
    _SLUG_CANDIDATES = (candidates, known_slugs)
    return _SLUG_CANDIDATES


def name_to_slug(workspace_name: str, todos: list[dict] | None = None) -> str | None:
    """Resolve a cmux workspace title to a workstream/routine slug.

    P0-tagged titles ("<emoji> P0 · <display>") are unwrapped first so a P0
    workspace links to its owning workstream directory and todos (invariants
    5+6). The <display> may be a config name, the slug itself, or — for
    reminder-card tabs — a P0 todo title; all three are tried.
    """
    visible = re.sub(r"^[\s\W_]+", "", workspace_name).strip()
    if not visible:
        return None
    p0_tagged = is_p0_titled(workspace_name)
    if p0_tagged:
        visible = P0_TITLE_RE.split(visible, maxsplit=1)[-1].strip() or visible

    candidates, known_slugs = _slug_candidates()

    visible_low = visible.lower()
    for slug, cfg_name in candidates:
        if cfg_name.lower() == visible_low:
            return slug
    # Title may be the slug itself (e.g. "🍔 P0 · demo-sandbox")
    slugified = _slugify(visible_low)
    if visible_low in known_slugs:
        return visible_low
    if slugified in known_slugs:
        return slugified
    # P0 reminder-card tab: "<emoji> P0 · <todo title>" → todo's workstream
    if p0_tagged and todos:
        for t in todos:
            if t.get("status") in ("done", "cancelled"):
                continue
            if (t.get("title") or "").strip().lower() == visible_low:
                ws = t.get("workstream")
                if ws:
                    return ws
    return slugified or None


_WORKSPACE_UPDATES_CACHE: dict | None = None


def workspace_review_url(slug: str | None) -> str:
    """Review surface recorded by the latest emit-workspace-update for this
    workstream, or "": an obsidian:// link to a note in the switchboard vault
    (default) or an approved https URL (Google Doc / hosted page, on request).
    Only pointers that passed the review-surface gate are stored, so a raw
    filesystem path can never be returned."""
    global _WORKSPACE_UPDATES_CACHE
    if not slug:
        return ""
    if _WORKSPACE_UPDATES_CACHE is None:
        path = STATE_DIR / "sources" / "workspace-updates.json"
        try:
            _WORKSPACE_UPDATES_CACHE = json.loads(path.read_text(encoding="utf-8")).get("updates") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            _WORKSPACE_UPDATES_CACHE = {}
    update = _WORKSPACE_UPDATES_CACHE.get(f"workstream:{slug}") or _WORKSPACE_UPDATES_CACHE.get(f"routine:{slug}") or {}
    url = str(update.get("review_url") or "").strip()
    return url if url.startswith(("obsidian://", "https://")) else ""


def find_workstream_dir(slug: str | None) -> Path | None:
    if not slug:
        return None
    for parent in (WORKSTREAMS_DIR, ROUTINES_DIR):
        d = parent / slug
        if d.exists() and (d / "CONTEXT.md").exists():
            return d
    return None


def parse_yaml_simple(path: Path) -> dict:
    data: dict = {}
    if not path.exists():
        return data
    try:
        list_key: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.rstrip()
            if not raw or raw.startswith("#"):
                continue
            list_item = re.match(r'^\s+-\s+"?([^"\n]+?)"?\s*$', raw)
            if list_item and list_key:
                if not isinstance(data.get(list_key), list):
                    data[list_key] = []
                data[list_key].append(list_item.group(1))
                continue
            m = re.match(r'^([a-zA-Z_][\w-]*):\s*"?([^"\n]*?)"?\s*$', raw)
            if m:
                k, v = m.group(1), m.group(2)
                data[k] = v
                list_key = k if not v else None
            elif not raw.startswith(" "):
                list_key = None
    except Exception:
        pass
    return data


# ───────────────────────── todos + urgency ─────────────────────────

def load_todos() -> list[dict]:
    if not TODO_FILE.exists():
        return []
    # The background digest reads todos; it is
    # not the owner's interactive surface. The feature-flag gate already runs as
    # its own background task and the Slack/Gmail surface verification runs on
    # every interactive `todo` command, so re-running both here would only add
    # seconds to every tick. Callers that set these vars explicitly still win.
    env = dict(os.environ)
    env.setdefault("SWITCHBOARD_LD_RECONCILE_SKIP", "1")
    env.setdefault("SWITCHBOARD_TODO_SURFACE_SKIP", "1")
    env.setdefault("THREAD_GATE_OFFLINE", "1")
    completed = subprocess.run(
        [
            sys.executable,
            str(TODO_CLI),
            "--path",
            str(TODO_FILE),
            "snapshot",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(f"todo surface verification failed; command center was not rendered: {detail[:500]}")
    try:
        payload = json.loads(completed.stdout)
        return list(payload.get("items") or [])
    except json.JSONDecodeError as error:
        raise RuntimeError("todo surface verification returned invalid JSON") from error


def workstream_open_todos(slug: str | None, todos: list[dict]) -> list[dict]:
    if not slug:
        return []
    return [
        t for t in todos
        if t.get("workstream") == slug
        and t.get("status") not in ("done", "cancelled")
    ]


def parse_due(due: str | None) -> datetime | None:
    if not due:
        return None
    try:
        return datetime.fromisoformat(due.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(due[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def derive_urgency(slug: str, open_todos: list[dict], ws_dir: Path | None,
                   p0_titled: bool = False) -> str:
    today = datetime.now(timezone.utc)

    # 0. P0 precedence (invariant 5): a "P0 ·" workspace title or any linked
    # open P0 todo wins outright — staleness heuristics may NOT downgrade it.
    if p0_titled:
        return PRIO_EMOJI["P0"]
    if any(t.get("priority") == "P0" for t in open_todos):
        return PRIO_EMOJI["P0"]

    # 1. Open todos signal
    if open_todos:
        sorted_t = sorted(open_todos, key=lambda x: (
            PRIO_RANK.get(x.get("priority", "P3"), 4),
            x.get("due") or "9999",
        ))
        top = sorted_t[0]
        prio = top.get("priority", "P3")
        due_dt = parse_due(top.get("due"))
        if due_dt:
            days_overdue = (today.date() - due_dt.date()).days
            if days_overdue >= 7 or prio == "P0":
                return PRIO_EMOJI["P0"]
            if days_overdue >= 0 or prio == "P1":
                return PRIO_EMOJI["P1"]
            if (due_dt.date() - today.date()).days <= 3:
                return PRIO_EMOJI["P1"]
        return PRIO_EMOJI.get(prio, PRIO_EMOJI["P2"])

    # 2. Config priority + staleness
    if ws_dir:
        cfg = parse_yaml_simple(ws_dir / "config.yaml")
        cfg_priority = cfg.get("priority", "medium").lower()
        try:
            stale_days = int(cfg.get("stale_after_days") or 14)
        except Exception:
            stale_days = 14
        last_active = workstream_last_action_ts(ws_dir)
        if last_active:
            age = (today - last_active).days
            if age >= stale_days * 2:
                return PRIO_EMOJI["P2"]
            if age >= stale_days:
                return PRIO_EMOJI["P1"] if cfg_priority == "high" else PRIO_EMOJI["P2"]
        if cfg_priority == "high":
            return PRIO_EMOJI["P1"]

    return PRIO_EMOJI["P2"]


def workstream_last_action_ts(ws_dir: Path) -> datetime | None:
    candidates: list[float] = []
    for sub in [ws_dir / "CONTEXT.md", ws_dir / ".signals" / "recent.md"]:
        if sub.exists():
            candidates.append(sub.stat().st_mtime)
    for sub_dir in [ws_dir / "inbox", ws_dir / "drafts", ws_dir / "triage-briefs"]:
        if sub_dir.exists():
            for f in sub_dir.glob("*.md"):
                candidates.append(f.stat().st_mtime)
    if not candidates:
        return None
    return datetime.fromtimestamp(max(candidates), tz=timezone.utc)


# ───────────────────────── status / next + waiting ─────────────────────────

def workstream_short_desc(slug: str, ws_dir: Path | None) -> str:
    if slug in ROUTINE_META:
        return ROUTINE_META[slug][1]
    if not ws_dir:
        return "(no description)"
    cfg = parse_yaml_simple(ws_dir / "config.yaml")
    desc = (cfg.get("description") or "").strip()
    if desc:
        return (desc[:130] + "…") if len(desc) > 130 else desc
    ctx = ws_dir / "CONTEXT.md"
    if ctx.exists():
        for line in ctx.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("**"):
                continue
            return (line[:130] + "…") if len(line) > 130 else line
    return "(no description)"


def parse_context_sections(ws_dir: Path) -> dict:
    """Pull common sections from CONTEXT.md: Next Steps, Waiting/Blocked, Status."""
    out = {"next_steps": [], "waiting": [], "current_state": ""}
    ctx = ws_dir / "CONTEXT.md"
    if not ctx.exists():
        return out
    text = ctx.read_text(encoding="utf-8")
    # Section headers we recognize (case-insensitive)
    section_aliases = {
        "next_steps": [r"## next steps?", r"## next$", r"## open / next steps", r"## todo$"],
        "waiting": [r"## waiting", r"## blocked", r"## blockers?", r"## depends on", r"## waiting on"],
        "current_state": [r"## current state", r"## status$", r"## state$", r"## now$"],
    }
    # Find each section and grab its bullets
    for key, patterns in section_aliases.items():
        for pat in patterns:
            m = re.search(rf"({pat})\n(.*?)(?=\n## |\Z)", text, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            block = m.group(2).strip()
            if key == "current_state":
                # Take first non-empty line/para
                first = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
                out[key] = first
            else:
                # Pull bullet lines
                bullets = []
                for ln in block.splitlines():
                    bm = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", ln)
                    if bm:
                        bullets.append(bm.group(1).strip())
                out[key] = bullets
            break
    return out


def _top_todo(open_todos: list[dict]) -> dict | None:
    if not open_todos:
        return None
    # requires_user first, then priority, then earliest due
    return sorted(open_todos, key=lambda x: (
        0 if x.get("requires_user") else 1,
        PRIO_RANK.get(x.get("priority", "P3"), 4),
        x.get("due") or x.get("review_on") or "9999",
    ))[0]


def status_and_next(slug: str, ws_dir: Path | None, open_todos: list[dict],
                    cmux_description: str = "", last_msg: str = "") -> str:
    """Compact 1-2 fragment status: top open todo + next step from CONTEXT,
    with concrete fallbacks (invariant 6) — config/cmux description or the
    last conversation message beat generic 'no next step' filler."""
    if slug in ROUTINE_META:
        if slug == "todo":
            return "Auto-reconcile + surface top 5 actionable. Watchlist for blockers."
        if slug == "comms-triage":
            return "Process Slack/Gmail/Linear/Jira inbox; auto-draft replies."
        if slug == "meetings":
            return "Schedule pre-briefs + ingest Gemini notes after each meeting."

    parts = []

    # Top open todo with overdue framing
    top = _top_todo(open_todos)
    if top:
        title = top.get("title", "")
        due = top.get("due", "")
        ago = ""
        if due:
            try:
                d = datetime.strptime(due[:10], "%Y-%m-%d").date()
                delta = (today_date() - d).days
                if delta > 0:
                    ago = f" · **{delta}d overdue**"
                elif delta == 0:
                    ago = " · **due today**"
                elif delta >= -3:
                    ago = f" · in {-delta}d"
            except Exception:
                pass
        if top.get("bucket") == "confirmation":
            evidence = top.get("completion_evidence") or []
            detail = str((evidence[0] if evidence else {}).get("detail") or "Completion evidence found")
            parts.append(f"✅ Confirm close: {title[:100]} — {detail[:100]}")
        else:
            parts.append(f"📌 {title[:130]}{ago}")

    # Next step from CONTEXT.md
    if ws_dir:
        secs = parse_context_sections(ws_dir)
        if secs["next_steps"]:
            first_next = secs["next_steps"][0]
            parts.append(f"⏭️ {first_next[:160]}")
        elif not open_todos:
            cfg = parse_yaml_simple(ws_dir / "config.yaml")
            nxt = (cfg.get("next_step") or "").strip()
            if nxt:
                parts.append(f"⏭️ {nxt[:160]}")
        if secs["current_state"] and len(parts) < 2:
            parts.append(f"📍 {secs['current_state'][:140]}")

    # Concrete fallbacks before giving up (invariant 6)
    if not parts:
        if cmux_description:
            parts.append(f"📍 {cmux_description[:160]}")
        elif last_msg:
            snippet = re.sub(r"\s+", " ", last_msg)[:140]
            parts.append(f"💬 {snippet}")
        else:
            parts.append("(agent tab — no linked workstream or todo)")

    rendered = "<br>".join(parts[:2])
    # Review-surface contract: when the latest workspace update
    # carries a gated review pointer, show it. Obsidian vault notes are the
    # default; https is a Google Doc / hosted page the owner asked for. Never a
    # raw filesystem path — the gate stores only these two forms.
    review_url = workspace_review_url(slug)
    if review_url.startswith("obsidian://"):
        rendered += "<br>" + review_surface.review_link(review_url).replace("\n", "<br>")
    elif review_url:
        rendered += f"<br>📄 [Review Doc]({review_url})"
    return rendered


def waiting_on(ws_dir: Path | None) -> str:
    if not ws_dir:
        return ""
    secs = parse_context_sections(ws_dir)
    items = secs.get("waiting") or []
    if not items:
        return "—"
    # Trim each item, max 2
    out = []
    for it in items[:2]:
        out.append(f"• {it[:90]}")
    if len(items) > 2:
        out.append(f"• +{len(items)-2} more")
    return "<br>".join(out)


def todo_debt(open_todos: list[dict]) -> str:
    """Compact, scan-friendly debt: total + highest-priority bucket only.

    e.g. "10 · 1 P0", "3 · 2 P1", "—". A full per-priority tuple wraps into
    vertical noise at chat width, so we surface only the count that matters.
    """
    if not open_todos:
        return "—"
    counts: dict[str, int] = {}
    for t in open_todos:
        p = t.get("priority", "P3")
        counts[p] = counts.get(p, 0) + 1
    for p in ["P0", "P1", "P2", "P3"]:
        if counts.get(p):
            return f"{len(open_todos)} · {counts[p]} {p}"
    return str(len(open_todos))


# ───────────────────────── delta vs previous ─────────────────────────

def previous_snapshot() -> dict[str, dict]:
    """Return slug -> {urgency, last_action_iso} from the most recent prior snapshot."""
    if not HISTORY_DIR.exists():
        return {}
    files = sorted(HISTORY_DIR.glob("*.md"))
    if len(files) < 2:
        return {}
    prev = files[-2]  # second-newest (newest is being written)
    out = {}
    text = prev.read_text(encoding="utf-8")
    # Look for our SLUG_INDEX comment block
    m = re.search(r"<!-- SLUG_INDEX (.+?) -->", text, re.DOTALL)
    if m:
        try:
            out = json.loads(m.group(1))
        except Exception:
            out = {}
    return out


def delta_marker(slug: str, urgency: str, prev: dict) -> str:
    if slug not in prev:
        return "🆕"
    prior = prev[slug].get("urgency", "")
    if prior and prior != urgency:
        # P-up or P-down arrow
        prio_pat = re.compile(r"P(\d)")
        cur_m = prio_pat.search(urgency)
        prev_m = prio_pat.search(prior)
        if cur_m and prev_m:
            cur, pre = int(cur_m.group(1)), int(prev_m.group(1))
            if cur < pre:
                return f"⬆️ from {prior}"  # got more urgent
            if cur > pre:
                return f"⬇️ from {prior}"  # cooled off
    return ""


# ───────────────────────── action commands ─────────────────────────

ACTION_SCRIPT = REPO_ROOT / "system" / "scripts" / "command-center-action.sh"


def _ensure_link_script(ws_ref: str, action: str) -> Path:
    """Create (idempotently) a clickable `.command` shim that runs the action helper.

    Clicking the file:// link opens it in macOS Terminal, which executes it.
    Result: actually focuses (or wraps+closes) the cmux workspace.
    """
    CMUX_LINKS_DIR.mkdir(parents=True, exist_ok=True)
    n = ws_ref.split(":")[1]
    path = CMUX_LINKS_DIR / f"{action}-ws-{n}.command"
    body = f"""#!/usr/bin/env bash
# Auto-generated by command-center-digest.py — do not edit.
exec /bin/bash {ACTION_SCRIPT} {action} {n}
"""
    if not path.exists() or path.read_text() != body:
        path.write_text(body)
        path.chmod(0o755)
    return path


def open_cmd(ws_ref: str) -> str:
    # Plain text — user types this in chat, agent executes via action helper.
    # (file:// links to .command shims fail because macOS opens .command in
    # vim/text editor when triggered via URL, not Terminal.)
    n = ws_ref.split(":")[1]
    return f"`open {n}`"


def close_cmd(ws_ref: str) -> str:
    n = ws_ref.split(":")[1]
    return f"`close {n}`"


# ───────────────────────── next-meeting parser ─────────────────────────

_TIME_RE = re.compile(r"^\s*-\s*(\d{1,2}:\d{2})\s*(?:(AM|PM|am|pm))?\s+(.+?)\s*$")


def parse_today_calendar() -> list[dict]:
    """Parse `## Calendar` from today's daily note. Returns [{time, title, raw}] sorted by time."""
    today = datetime.now().strftime("%Y-%m-%d")
    note = DAILY_NOTES_DIR / f"{today}.md"
    if not note.exists():
        return []
    try:
        text = note.read_text(encoding="utf-8")
    except Exception:
        return []
    out: list[dict] = []
    in_cal = False
    for line in text.splitlines():
        if line.strip().lower().startswith("## calendar"):
            in_cal = True
            continue
        if in_cal:
            if line.startswith("##"):
                break
            m = _TIME_RE.match(line)
            if not m:
                continue
            hhmm, ampm, title = m.groups()
            # Normalize to HH:MM 24h for sorting; assume AM/PM optional with implicit AM<12
            try:
                h, mn = (int(x) for x in hhmm.split(":"))
                if ampm and ampm.upper() == "PM" and h < 12:
                    h += 12
                if ampm and ampm.upper() == "AM" and h == 12:
                    h = 0
                # If no AM/PM, daily-note convention treats <8 as PM and >=8 as AM
                if not ampm and h < 8:
                    h += 12
                sort_key = h * 60 + mn
            except Exception:
                sort_key = 9999
            # Strip trailing parenthetical like "(pre-brief auto at 11:20)"
            clean_title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
            display_time = hhmm if not ampm else f"{hhmm}{ampm.upper()}"
            out.append({
                "time": display_time,
                "sort_key": sort_key,
                "title": clean_title,
                "raw": line.strip(),
            })
    out.sort(key=lambda x: x["sort_key"])
    return out


def _slug_keywords(slug: str) -> list[str]:
    parts = re.split(r"[-_]+", slug)
    return [p.lower() for p in parts if len(p) >= 3]


def next_meeting_for_slug(slug: str, ws_dir: Path | None, meetings: list[dict]) -> str:
    """Find the next remaining meeting today whose title matches the workstream."""
    if not meetings:
        return ""
    now_min = datetime.now().hour * 60 + datetime.now().minute
    upcoming = [m for m in meetings if m["sort_key"] >= now_min]
    if not upcoming:
        return ""
    keywords = set(_slug_keywords(slug))
    signal_terms: list[str] = []
    # Also pull the workstream display name as keyword source
    if ws_dir:
        cfg = parse_yaml_simple(ws_dir / "config.yaml")
        nm = (cfg.get("name") or "").lower()
        for w in re.split(r"[\s\-_/]+", nm):
            if len(w) >= 3:
                keywords.add(w)
        raw_terms = cfg.get("signal_match_terms") or []
        if isinstance(raw_terms, list):
            signal_terms = [str(term).strip().lower() for term in raw_terms if str(term).strip()]
    keywords -= {"the", "and", "for", "ecom", "owner"}
    if not keywords:
        return ""
    for m in upcoming:
        title_l = m["title"].lower()
        if any(term in title_l for term in signal_terms) or any(k in title_l for k in keywords):
            return f"📅 {m['time']} {m['title'][:32]}"
    return ""


def routine_next_meeting(slug: str, meetings: list[dict]) -> str:
    """For meetings routine, just show the next upcoming meeting overall."""
    if slug != "meetings" or not meetings:
        return ""
    now_min = datetime.now().hour * 60 + datetime.now().minute
    upcoming = [m for m in meetings if m["sort_key"] >= now_min]
    if not upcoming:
        return ""
    m = upcoming[0]
    return f"📅 {m['time']} {m['title'][:32]}"


# ───────────────────────── pause / archive suggestions ─────────────────────────

def lifecycle_suggestion(slug: str, ws_dir: Path | None, open_todos: list[dict]) -> str:
    """Return '⏸️ pause' or '🗄️ archive' suggestion if applicable."""
    if not ws_dir or slug in ROUTINE_META:
        return ""
    cfg = parse_yaml_simple(ws_dir / "config.yaml")
    try:
        stale_days = int(cfg.get("stale_after_days") or 14)
    except Exception:
        stale_days = 14
    last = workstream_last_action_ts(ws_dir)
    if not last:
        return ""
    age = (datetime.now(timezone.utc) - last).days
    if age >= stale_days * 3 and not open_todos:
        return f"🗄️ idle {age}d · `archive {slug}`"
    if age >= stale_days * 2:
        return f"⏸️ idle {age}d · `pause {slug}`"
    return ""


# ───────────────────────── categorize (sections 1-3) ─────────────────────────

def _prio_of(urgency: str) -> str:
    m = re.search(r"P(\d)", urgency)
    return f"P{m.group(1)}" if m else "P3"


def _date_of(val: str | None) -> date | None:
    if not val:
        return None
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _todo_actionable_today(t: dict) -> bool:
    """A todo counts as 'in front of the owner today' — not snoozed/scheduled ahead."""
    today = today_date()
    snooze = _date_of(t.get("snooze_until"))
    if snooze and snooze > today:
        return False
    review = _date_of(t.get("review_on"))
    if review and review > today:
        return False
    return True


def categorize(row: dict) -> tuple[str, str, int]:
    """Assign a workspace row to a digest section.

    Returns (section, why, act_rank) with section in {"act", "important",
    "background"}. act_rank orders section 1 (lower = higher on the list).
    """
    lane = row.get("lane") or ""
    prio = _prio_of(row["urgency"])
    top = row.get("top_todo")
    today = today_date()
    actionable = bool(top and _todo_actionable_today(top))

    # 1) Agent explicitly blocked on the owner
    if lane == "needs-attention":
        return ("act", "❗ needs attention", 0)

    # 2) Explicit asks / fresh human commitments surfaced as requires_user todos
    if top and top.get("requires_user") and actionable:
        return ("act", "🙋 needs you", 1)

    # 3) Time-critical P0/P1 actions
    if prio in ("P0", "P1") and actionable:
        due = _date_of(top.get("due"))
        if due and due < today:
            return ("act", f"⏰ {(today - due).days}d overdue", 2)
        if due and due == today:
            return ("act", "⏰ due today", 2)
        review = _date_of(top.get("review_on"))
        if review and review <= today:
            return ("act", "⏰ review due", 2)

    # 4) Finished agent run awaiting review — meaningful only on P0/P1 work.
    #    The review lane is explicit, but the lane alone still must not
    #    promote low-priority or unlinked tabs to "act".
    if lane == "review" and prio in ("P0", "P1"):
        return ("act", "👁 review ready", 3)

    # 5) Quick P0/P1 wins (small explicit estimate)
    if prio in ("P0", "P1") and actionable:
        est = top.get("estimate_min")
        if isinstance(est, (int, float)) and 0 < est <= 15:
            return ("act", f"⚡ quick win ~{int(est)}m", 4)

    # Section 2: important but not immediately actionable
    if prio in ("P0", "P1"):
        return ("important", "", 9)
    if top:
        due = _date_of(top.get("due"))
        if due and (due - today).days <= 7:
            return ("important", f"due {due.isoformat()}", 9)

    return ("background", "", 99)


# ───────────────────────── build + render ─────────────────────────

ACT_NOW_CAP = 5


def _canonical_source_ref(ref: str) -> str:
    try:
        parsed = urlsplit(str(ref or "").strip())
    except ValueError:
        return str(ref or "").strip()
    if "slack.com" in parsed.netloc.lower():
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", parsed.fragment))
    if parsed.netloc.lower() == "docs.google.com":
        anchor = (parse_qs(parsed.query).get("disco") or [""])[0]
        query = urlencode({"disco": anchor}) if anchor else ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), query, ""))
    return str(ref or "").strip()


def held_draft_row(todos: list[dict], rows: list[dict]) -> dict | None:
    """Build one top-ranked Act-now row for every pending comms draft."""
    if not comms_triage_setting.enabled():
        return None
    try:
        state = json.loads(TRIAGE_LEDGER_FILE.read_text())
        drafts = [
            item
            for item in (state.get("items") or {}).values()
            if isinstance(item, dict) and item.get("draft_status") in {"drafted", "held"}
        ]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not drafts:
        return None

    todos_by_ref = {
        _canonical_source_ref(str(todo.get("source_ref") or "")): todo
        for todo in todos
        if todo.get("status") not in {"done", "cancelled"} and todo.get("source_ref")
    }
    linked = [todos_by_ref.get(_canonical_source_ref(str(item.get("ref") or ""))) for item in drafts]
    linked = [todo for todo in linked if todo]
    priorities = [str(todo.get("priority") or "P1") for todo in linked]
    priority = min(priorities or ["P1"], key=lambda value: PRIO_RANK.get(value, 4))
    today = today_date()
    overdue_p1 = sum(
        1
        for todo in linked
        if todo.get("priority") == "P1"
        and (_date_of(todo.get("due")) or today) < today
    )
    oldest = min(
        drafts,
        key=lambda item: str(item.get("draft_held_at") or item.get("first_seen") or ""),
    )
    oldest_ref = str(oldest.get("ref") or "")
    oldest_link = f"[oldest source]({oldest_ref})" if oldest_ref.startswith("http") else "oldest source"
    age_detail = f" · **{overdue_p1} overdue P1**" if overdue_p1 else ""
    workstreams = sorted(
        {
            str(todo.get("workstream"))
            for todo in linked
            if todo.get("workstream")
        }
    )
    workspace_detail = f" · {', '.join(workstreams[:2])}" if workstreams else ""
    comms_row = next((row for row in rows if row.get("slug") == "comms-triage"), None)
    held_at = parse_due(str(oldest.get("draft_held_at") or oldest.get("first_seen") or ""))
    return {
        "ref": "drafts",
        "ref_num": "—",
        "name": "📬 Held comms drafts",
        "thread_id": "",
        "agent": "",
        "slug": "held-comms-drafts",
        "is_routine": False,
        "has_dir": False,
        "lane": "needs-attention",
        "top_todo": None,
        "description": "Response drafts waiting for the owner",
        "urgency": PRIO_EMOJI.get(priority, PRIO_EMOJI["P1"]),
        "delta": "",
        "status": (
            f"**Draft ready: approve to post** · {len(drafts)} pending{age_detail}"
            f" · {oldest_link}{workspace_detail}"
        ),
        "debt": str(len(drafts)),
        "waiting": "the owner approval, edit, or dismissal",
        "next_meeting": "",
        "open_cmd": comms_row.get("open_cmd", "—") if comms_row else "—",
        "close_cmd": comms_row.get("close_cmd", "—") if comms_row else "—",
        "lifecycle": "",
        "last_action": held_at or datetime.fromtimestamp(0, tz=timezone.utc),
        "section": "act",
        "why": "✍️ draft ready",
        "act_rank": -1,
        "synthetic": True,
    }


def agent_marker(agent: dict) -> str:
    """Status fragment for a live Amp agent: '⚡ agent running' (+ age when it
    has been running ≥30 min). Idle/unknown agents add nothing — the existing
    status/next text already covers them.

    A Running state older than STALE_AGENT_MIN is a cmux state_ts that
    nobody refreshed, not evidence of work. Append " · stale — verify" so the digest never
    presents a stale timestamp as progress. The authoritative liveness
    signal is the task ledger (`running-task-state.py summary` → stale[]),
    which command-center-loop.sh appends as its own section."""
    if (agent or {}).get("state") != "Running":
        return ""
    marker = "⚡ agent running"
    ts = agent.get("state_ts") or 0
    if ts:
        age_min = int(max(0.0, datetime.now(timezone.utc).timestamp() - ts) // 60)
        if age_min >= 30:
            h, m = divmod(age_min, 60)
            marker += f" {h}h{m:02d}m" if h else f" {m}m"
        if age_min >= STALE_AGENT_MIN:
            marker += " · stale — verify"
    return marker


def build_rows(workspaces: list[dict], todos: list[dict], prev_idx: dict,
               meetings: list[dict], agents: dict[str, dict] | None = None) -> list[dict]:
    if agents is None:
        agents = amp_agent_map()
    prefetch_lanes([
        ws["ref"] for ws in workspaces
        if not is_cos(ws["name"]) and not ws.get("pinned")
    ])
    rows = []
    for ws in workspaces:
        if is_cos(ws["name"]):
            continue
        if ws.get("pinned"):
            continue  # invariant 1: pinned workspaces are excluded entirely
        slug = name_to_slug(ws["name"], todos)
        if slug == "comms-triage" and not comms_triage_setting.enabled():
            continue  # off on this machine: a stray tab stays out of the digest
        ws_dir = find_workstream_dir(slug)
        open_todos = workstream_open_todos(slug, todos)
        p0_titled = is_p0_titled(ws["name"])
        urgency = derive_urgency(slug or "", open_todos, ws_dir, p0_titled=p0_titled)
        last_action = workstream_last_action_ts(ws_dir) if ws_dir else None
        next_mtg = (
            routine_next_meeting(slug or "", meetings)
            if slug in ROUTINE_META
            else next_meeting_for_slug(slug or "", ws_dir, meetings)
        )
        agent = agents.get(ws.get("id") or "", {})
        row = {
            "ref": ws["ref"],
            "ref_num": int(ws["ref"].split(":")[1]),
            "name": ws["name"],
            "thread_id": agent.get("thread_id", ""),
            "agent": agent_marker(agent),
            "slug": slug or "(unknown)",
            "is_routine": slug in ROUTINE_META,
            "has_dir": ws_dir is not None,
            "lane": workspace_lane(ws["ref"]),
            "top_todo": _top_todo(open_todos),
            "description": workstream_short_desc(slug or "", ws_dir),
            "urgency": urgency,
            "delta": delta_marker(slug or "", urgency, prev_idx),
            "status": status_and_next(
                slug or "", ws_dir, open_todos,
                cmux_description=ws.get("cmux_description", ""),
                last_msg=ws.get("last_msg", ""),
            ),
            "debt": todo_debt(open_todos),
            "waiting": waiting_on(ws_dir),
            "next_meeting": next_mtg,
            "open_cmd": open_cmd(ws["ref"]),
            "close_cmd": close_cmd(ws["ref"]),
            "lifecycle": lifecycle_suggestion(slug or "", ws_dir, open_todos),
            "last_action": last_action or datetime.fromtimestamp(0, tz=timezone.utc),
        }
        row["section"], row["why"], row["act_rank"] = categorize(row)
        rows.append(row)
    return rows


def split_sections(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Order rows within sections and enforce the act-now cap (overflow →
    important, keeping rank order)."""
    act = [r for r in rows if r["section"] == "act"]
    important = [r for r in rows if r["section"] == "important"]
    background = [r for r in rows if r["section"] == "background"]

    act.sort(key=lambda r: (
        r["act_rank"],
        PRIO_RANK.get(_prio_of(r["urgency"]), 4),
        -r["last_action"].timestamp(),
    ))
    if len(act) > ACT_NOW_CAP:
        overflow = act[ACT_NOW_CAP:]
        act = act[:ACT_NOW_CAP]
        for r in overflow:
            r["section"] = "important"
            r["why"] = ""  # keep section 2 clean; overdue detail stays in status
        important = overflow + important

    important.sort(key=lambda r: (
        PRIO_RANK.get(_prio_of(r["urgency"]), 4),
        -r["last_action"].timestamp(),
    ))
    background.sort(key=lambda r: (0 if r["is_routine"] else 1, r["ref_num"]))
    return act, important, background


def _short_name(name: str, limit: int = 34) -> str:
    n = name.strip()
    return n if len(n) <= limit else n[: limit - 1] + "…"


def _background_groups(background: list[dict]) -> list[tuple[str, list[dict]]]:
    """Compact rollup groups (invariant 4): routines, low-priority linked
    workstreams, and unlinked/parked tabs — one row per group, not per tab."""
    routines = [r for r in background if r["is_routine"]]
    linked = [r for r in background if not r["is_routine"] and r["has_dir"]]
    parked = [r for r in background if not r["is_routine"] and not r["has_dir"]]
    groups = []
    if routines:
        groups.append(("Routines", routines))
    if linked:
        groups.append(("Low-priority workstreams", linked))
    if parked:
        groups.append(("Parked / unclear tabs", parked))
    return groups


TABLE_HEADER = "| # | Workspace | Status / Next | Debt | Actions |"
TABLE_SEP = "|---|---|---|---|---|"


def _cell(s: str) -> str:
    # Literal newlines (e.g. hard-wrapped cmux descriptions) break the table;
    # collapse them to spaces. Intentional in-cell breaks use <br>.
    s = re.sub(r"[ \t]*\n[ \t]*", " ", str(s))
    return s.replace("|", "\\|")


def _divider(label: str) -> str:
    return f"| ⏤ | **— {label} —** |  |  |  |"


def _workspace_cell(r: dict) -> str:
    """Urgency folded into the workspace label, without a redundant priority.

    "🚀 P0 · Alpha" + urgency "🔴 P0" → "🔴 P0 · 🚀 Alpha" (one P0, not two).
    """
    urg = r["urgency"] + (f" {r['delta']}" if r["delta"] else "")
    prio = _prio_of(r["urgency"])
    name = re.sub(rf"\b{prio}\s*·\s*", "", r["name"]).strip()
    return f"{urg} · {name}"


def _full_row(r: dict) -> str:
    status_bits = []
    if r["why"]:
        status_bits.append(f"**{r['why']}**")
    if r.get("agent"):
        status_bits.append(r["agent"])
    status_bits.append(r["status"])
    if r["next_meeting"]:
        status_bits.append(r["next_meeting"])
    if r["lifecycle"]:
        status_bits.append(r["lifecycle"])
    # A PR, issue, or task mentioned in the status leads with its name; the
    # number sits after it. The
    # digest runs every 10 minutes, so it fetches at most two missing titles
    # per run (CC_NAMED_REFS_MAX_FETCH) and reads the rest from the cache.
    status_cell = named_refs.name_refs(" · ".join(b for b in status_bits if b))
    action_text = (
        f"{r['open_cmd']} · {r['close_cmd']}"
        if r.get("open_cmd") != "—"
        else "See Comms Triage"
    )
    return "| {ref} | {name} | {status} | {debt} | {actions} |".format(
        ref=r["ref_num"],
        name=_cell(_workspace_cell(r)),
        status=_cell(status_cell),
        debt=_cell(r["debt"]),
        actions=action_text,
    )


def _group_row(label: str, members: list[dict]) -> str:
    frags = []
    for r in members:
        prefix = "⚡ " if r.get("agent") else ""
        frag = f"{prefix}{_short_name(r['name'])} ({r['open_cmd']})"
        if r["lifecycle"]:
            frag += f" {r['lifecycle'].split('·')[0].strip()}"
        frags.append(frag)
    return "| — | ⚪ {label} ({n}) | {frags} | — | — |".format(
        label=_cell(label), n=len(members), frags=_cell(" · ".join(frags)),
    )


def knowledge_health_line() -> str | None:
    """One-line canonical knowledge layer health for the digest header.

    Surfaces the coverage manifest (build-daily-knowledge.py) so missing or
    stale source classes are visible on the surface the owner actually looks at —
    a silent collector gap must never look like a quiet day. Fail-soft: no
    coverage file, no line.
    """
    cov_dir = STATE_DIR / "knowledge" / "coverage"
    files = sorted(cov_dir.glob("2*.json"))
    try:
        snapshot = load_snapshot(cov_dir.parent)
        if snapshot is not None:
            cov = snapshot['coverage']
        elif files:
            cov = json.loads(files[-1].read_text())
        else:
            return None
    except (ValueError, OSError):
        return '_⚠️ knowledge: snapshot unavailable_'
    total = cov.get("records_total", 0)
    missing = cov.get("missing_source_classes") or []
    stale = sorted(
        cls for cls, c in (cov.get("classes") or {}).items() if c.get("stale")
    )
    bits = [f"🧠 knowledge: {total} records ({cov.get('day', '?')})"]
    if missing:
        bits.append(f"⚠️ missing: {', '.join(missing)}")
    if stale:
        bits.append(f"⏳ stale: {', '.join(stale)}")
    if not missing and not stale:
        bits.append("all sources fresh")
    return "_" + " · ".join(bits) + "_"


def inventory_health_line() -> str | None:
    """One line for the state-inventory shrink check (check-state-inventory.py):
    counts when all is well, the tripped paths with before→after
    when not. Fail-soft: no record, no line."""
    try:
        rec = json.loads((STATE_DIR / "inventory-latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("paths"), dict):
        return None
    alert = rec.get("alert") or None
    if alert:
        shown = "; ".join(f"{t.get('path')} {t.get('before')}→{t.get('after')}" for t in (alert.get("tripped") or [])[:4])
        return f"_🔴 state inventory shrank since {alert.get('since', '?')}: {shown}_"
    counts = []
    for rel, row in rec["paths"].items():
        label = rel.rsplit("/", 1)[-1].replace(".json", "")
        value = row.get("files", row.get("tasks", row.get("items", "?")))
        counts.append(f"{label} {value}")
    return f"_📦 state inventory ok: {' · '.join(counts)}_"


def render_markdown(rows: list[dict], hidden_pinned: int = 0,
                    agents_running: int = 0) -> str:
    now = datetime.now().astimezone()
    act, important, background = split_sections(rows)

    out = []
    out.append(f"# 🧭 CoS Command Center · {now.strftime('%a %b %d · %I:%M %p %Z')}")
    out.append("")
    # agents_running counts pinned workspaces too (they're hidden from the
    # table by invariant 1, but a live agent is still a global signal).
    agent_bit = ""
    if agents_running:
        noun = "agent" if agents_running == 1 else "agents"
        agent_bit = f"⚡ {agents_running} {noun} running · "
    out.append(
        f"_{agent_bit}🔴 {len(act)} act now · 🟠 {len(important)} important · "
        f"⚪ {len(background)} background · 📌 {hidden_pinned} pinned hidden_"
    )
    health = knowledge_health_line()
    if health:
        out.append(health)
    inventory = inventory_health_line()
    if inventory:
        out.append(inventory)
    out.append("")
    out.append(
        "Act on a row: `open <#>` focus · `close <#>` wrap + close · "
        "`pause <slug>` · `archive <slug>`"
    )
    out.append("")
    out.append(TABLE_HEADER)
    out.append(TABLE_SEP)

    out.append(_divider(f"🔴 Act now ({len(act)})"))
    if act:
        for r in act:
            out.append(_full_row(r))
    else:
        out.append("| — | 🟩 (nothing urgent) | Nothing needs you right now | — | — |")

    out.append(_divider(f"🟠 Important · not immediate ({len(important)})"))
    for r in important:
        out.append(_full_row(r))

    if background:
        out.append(_divider(f"⚪ Background · agent-owned / parked ({len(background)})"))
        for label, members in _background_groups(background):
            out.append(_group_row(label, members))

    # Hidden index for delta tracking on next run
    # thread_id lets downstream consumers (CoS injection) jump straight to the
    # owning Amp thread (read_thread / preview_messages) without re-joining.
    slug_idx = {
        r["slug"]: {
            "urgency": r["urgency"],
            "last_action": r["last_action"].isoformat(),
            **({"thread_id": r["thread_id"]} if r.get("thread_id") else {}),
            **({"agent": "running"} if r.get("agent") else {}),
        }
        for r in rows
    }
    out.append("")
    out.append(f"<!-- SLUG_INDEX {json.dumps(slug_idx)} -->")
    return "\n".join(out)


def write_actions_map(workspaces: list[dict], actions_file: Path | None = None) -> None:
    """Persist render-time provenance for every numeric action handle.

    command-center-action.sh revalidates `open/close/wrap <n>` against this
    map before acting, so a stale digest can never target a workspace whose
    number cmux has since reused for something else. Written atomically
    alongside every digest render, and by `--actions-map-only` when a tick is
    skipped or a wmux handle is missing (every open tab, pinned and CoS too).
    """
    handles: dict[str, dict] = {}
    for w in workspaces:
        m = re.match(r"^workspace:(\d+)$", w.get("ref") or "")
        if not m:
            continue
        handles[m.group(1)] = {
            "ref": w["ref"],
            "id": w.get("id") or "",
            "title": w.get("name") or "",
        }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "handles": handles,
    }
    actions_file = actions_file or STATE_DIR / "command-center-actions.json"
    actions_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = actions_file.with_name(actions_file.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(actions_file)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--format", default="markdown", choices=["markdown"])
    p.add_argument("--actions-map-only", action="store_true",
                   help="list the open tabs, rewrite the action map, print nothing else")
    p.add_argument("--actions-file", type=Path, default=None,
                   help="action map path (default system/state/command-center-actions.json)")
    args = p.parse_args()
    if args.actions_map_only:
        # A failed listing exits 3 in cmux_list_workspaces and keeps the old map.
        write_actions_map(cmux_list_workspaces(), args.actions_file)
        return 0
    named_refs.set_fetch_budget(int(os.environ.get("CC_NAMED_REFS_MAX_FETCH", "2")))

    workspaces = cmux_list_workspaces()
    write_actions_map(workspaces, args.actions_file)
    todos = load_todos()
    prev_idx = previous_snapshot()
    meetings = parse_today_calendar()
    agents = amp_agent_map()
    hidden_pinned = sum(1 for w in workspaces if w.get("pinned") and not is_cos(w["name"]))
    agents_running = sum(
        1 for w in workspaces
        if not is_cos(w["name"])
        and agents.get(w.get("id") or "", {}).get("state") == "Running"
    )
    rows = build_rows(workspaces, todos, prev_idx, meetings, agents=agents)
    draft_row = held_draft_row(todos, rows)
    if draft_row:
        rows.append(draft_row)
    print(render_markdown(rows, hidden_pinned=hidden_pinned, agents_running=agents_running))
    return 0


if __name__ == "__main__":
    sys.exit(main())
