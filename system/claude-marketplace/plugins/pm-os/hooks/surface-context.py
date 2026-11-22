#!/usr/bin/env python3
"""Inject the dispatch contract into Claude CoS and Launchpad sessions.

In Amp, CoS learned the dispatch rules from its start-day seed and a long-lived
thread. Under Claude Code a CoS can be a bare `claude` started outside the repo: it
never loads the repo AGENTS.md, the chief-of-staff prompt, or
system/prompts/dispatch-core.md, so it sends no `Workstream:` owner line and
dispatch.py slugifies the prompt. This hook makes the contract independent of the
agent reading a file.

Private session tab (optional): when system/config/private-sessions.json names a
"surface", a session started with PM_OS_SURFACE=<that name> gets a short
delivery-surface note instead of the dispatch card. No config, no such surface.

Events (hooks.json):
  session-start        full card (dispatch-core-card.md + live workstream slugs) as
                       additionalContext, and DISPATCH_ORIGIN / PM_OS_ROOT
                       exported through CLAUDE_ENV_FILE. Fires again
                       after /clear and compaction, so the card survives both.
  user-prompt-submit   a short per-turn reminder; the full card once per session
                       when SessionStart never delivered it (a tab claimed or
                       retitled CoS after it started, the plugin loaded mid-session).

Role, first match wins (anything else prints nothing):
  1. A worker (PM_OS_WORKER_ID set) -> none, before anything else.
  2. PM_OS_SURFACE=cos|launchpad|none set by hand (the hook never exports it, so a
     `claude` started from a CoS Bash call does not inherit a surface role).
     A "PM OS worker rt-..." tab -> none.
  3. CMUX_WORKSPACE_ID equals the registered CoS tab (system/state/cos-surface.json,
     written by `owner_tabs.py claim-cos`) -> cos.
  4. The cmux tab title: 🧭 CoS / CoS / Megadesk -> cos (never a "(old ...)" tab);
     🚀 Launchpad / Launchpad -> launchpad.

First prompt binds CoS: on user-prompt-submit, before the role check,
system/scripts/cos_autobind.py observes the prompt. When a session's first prompt is one of
those phrases in a cmux or wmux pane, it binds the session to the tab, registers the tab as
CoS (claim-cos: title, pin, old CoS renamed, never closed), on cmux moves it to the top, and this hook
injects what it did plus how to run the ask. The logic lives in the repo, so it updates
without a plugin reinstall; a missing module is skipped.

wmux exports WMUX_SURFACE_ID but no CMUX_WORKSPACE_ID, so a hand-started session asks the
pane for its tab (cached per session), and session-start writes CMUX_WORKSPACE_ID into
CLAUDE_ENV_FILE for every non-worker wmux session, so its Bash calls (cos-phase0.sh,
ask-cos.sh) find their tab.

Kill switch: ~/.claude/pm-os-plugin-config.json {"surface-context.disabled": true};
{"cos-autobind.disabled": true} turns off the first-prompt binding alone.
Never writes stdout except the hook JSON; never fails the hook (exit 0 always).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
# The plugin lives at <repo>/system/claude-marketplace/plugins/pm-os/hooks (Claude
# loads it in place from the main checkout), so the repo is five levels up, as in
# amp-bridge.ts. PM_OS_REPO / PM_OS_ROOT override it.
REPO_ROOT = Path(os.environ.get("PM_OS_REPO") or os.environ.get("PM_OS_ROOT") or HOOK_DIR.parents[4])
CARD = REPO_ROOT / "system" / "prompts" / "dispatch-core-card.md"
CONFIG_PATH = Path(os.environ.get("PM_OS_PLUGIN_CONFIG") or Path.home() / ".claude" / "pm-os-plugin-config.json")
STATE_DIR = Path(os.environ.get("SURFACE_CONTEXT_STATE_DIR") or REPO_ROOT / ".state" / "surface-context")
COS_REGISTRATION = Path(os.environ.get("COS_SURFACE_FILE") or REPO_ROOT / "system" / "state" / "cos-surface.json")
COS_TITLES = {"🧭 CoS", "CoS", "🧭 Megadesk", "Megadesk", "megadesk", "Chief of Staff"}
LAUNCHPAD_TITLES = {"🚀 Launchpad", "Launchpad", "🚀 LaunchPad", "LaunchPad"}
SURFACES = {
    "cos": {"name": "CoS", "seed": "As CoS you are also the reporting hub: read `{root}/system/chief-of-staff-prompt.md` for the reporting rules (digests, callbacks, EOD) after dispatch-core.md."},
    "launchpad": {"name": "Launchpad", "seed": "As Launchpad you are the quiet work console: read `{root}/routines/launchpad/CONTEXT.md` after dispatch-core.md. Never post unsolicited reports."},
}
# Claude caps hook-injected context near 10,000 characters and swaps anything
# longer for a file pointer; stay well under it (test_surface_context.py).
CARD_LIMIT = 9500
CLOSED_STATUSES = {"done", "closed", "complete", "completed", "cancelled", "resolved", "archived"}


def disabled() -> bool:
    try:
        return bool(json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("surface-context.disabled"))
    except (OSError, ValueError, AttributeError):
        return False


def scripts_module(name: str):
    """A module from the repo's system/scripts (or its lib/), or None when it is missing."""
    import importlib
    for folder in (REPO_ROOT / "system" / "scripts" / "lib", REPO_ROOT / "system" / "scripts"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def cmux_argv() -> list[str]:
    explicit = os.environ.get("CMUX_BIN") or os.environ.get("CMUX")
    mux = scripts_module("mux")
    if mux is not None:
        try:
            return mux.runnable([explicit or mux.cmux_bin()])
        except Exception:
            pass
    return [explicit or ("/Applications/cmux.app/Contents/Resources/bin/cmux"
                         if Path("/Applications/cmux.app/Contents/Resources/bin/cmux").exists() else "cmux")]


def pane_workspace_id(session_id: str = "") -> str:
    """CMUX_WORKSPACE_ID, else (a wmux pane, which exports only WMUX_SURFACE_ID) the pane's
    tab from the mux, cached per session so later prompts skip the lookup."""
    explicit = (os.environ.get("CMUX_WORKSPACE_ID") or "").strip()
    if explicit or not os.environ.get("WMUX_SURFACE_ID"):
        return explicit
    # Keyed by session and pane: a resumed session keeps its id but may sit in another tab.
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{session_id}-{os.environ.get('WMUX_SURFACE_ID', '')}")
    cache = STATE_DIR / f"{key}.pane" if session_id else None
    if cache is not None:
        try:
            cached = cache.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        except OSError:
            pass
    mux = scripts_module("mux")
    try:
        found = mux.current_workspace_id(timeout=5) if mux is not None else ""
    except Exception:
        found = ""
    if found and cache is not None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(found + "\n", encoding="utf-8")
        except OSError:
            pass
    return found


def this_tab(workspace_id: str) -> dict:
    """The cmux tab this session runs in, or {} (no cmux, no id, no answer)."""
    if not workspace_id:
        return {}
    raw = os.environ.get("SURFACE_CONTEXT_WORKSPACES_JSON")  # tests
    try:
        if raw is None:
            proc = subprocess.run(cmux_argv() + ["rpc", "workspace.list", "{}"], capture_output=True, text=True,
                                  timeout=5, env={**os.environ, "CMUX_QUIET": "1"}, stdin=subprocess.DEVNULL)
            raw = proc.stdout if proc.returncode == 0 else "{}"
        workspaces = json.loads(raw or "{}").get("workspaces") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return next((w for w in workspaces if str(w.get("id") or "").upper() == workspace_id.upper()), {})


def surface_role(session_id: str = "") -> tuple[str, str]:
    """(role, why): role is cos, launchpad, or "" for every other session."""
    # A worker first: an inherited PM_OS_SURFACE must never make a worker a dispatcher.
    if os.environ.get("PM_OS_WORKER_ID"):
        return "", "worker"
    forced = (os.environ.get("PM_OS_SURFACE") or "").strip().lower()
    if forced in SURFACES:
        return forced, "PM_OS_SURFACE"
    if forced:
        return "", f"PM_OS_SURFACE={forced}"
    workspace_id = pane_workspace_id(session_id)
    try:
        registered = str(json.loads(COS_REGISTRATION.read_text(encoding="utf-8")).get("workspace_id") or "")
    except (OSError, ValueError, AttributeError):
        registered = ""
    if workspace_id and registered and registered.upper() == workspace_id.upper():
        return "cos", "registered CoS tab"
    tab = this_tab(workspace_id)
    if str(tab.get("description") or "").startswith("PM OS worker "):
        return "", "worker tab"
    title = str(tab.get("custom_title") or tab.get("title") or "").strip()
    if title in COS_TITLES:
        return "cos", f"tab title {title}"
    if title in LAUNCHPAD_TITLES:
        return "launchpad", f"tab title {title}"
    return "", "not a dispatch surface"


def workstream_slugs() -> str:
    """Comma-separated slugs of every workstream that is not closed or archived."""
    slugs = []
    for config in sorted((REPO_ROOT / "workstreams").glob("*/config.yaml")):
        slug = config.parent.name
        if slug.startswith("_"):
            continue
        try:
            match = re.search(r"^status:\s*['\"]?([A-Za-z_-]+)", config.read_text(encoding="utf-8"), re.MULTILINE)
        except OSError:
            continue
        if match and match.group(1).lower() in CLOSED_STATUSES:
            continue
        slugs.append(slug)
    return ", ".join(slugs) or "(none found)"


def render_card(role: str) -> str:
    surface = SURFACES[role]
    try:
        template = CARD.read_text(encoding="utf-8")
    except OSError:
        template = ("You are the PM OS {SURFACE_NAME} surface (origin {ORIGIN}). Read {REPO_ROOT}/system/prompts/"
                    "dispatch-core.md before the first ask and route every substantive ask with router-dispatch.sh.")
    root = str(REPO_ROOT)
    text = (template.replace("{SURFACE_NAME}", surface["name"]).replace("{ORIGIN}", role)
            .replace("{REPO_ROOT}", root).replace("{SEED_LINE}", surface["seed"].format(root=root)))
    slugs = workstream_slugs()
    room = CARD_LIMIT - len(text.replace("{WORKSTREAMS}", ""))
    if len(slugs) > room:
        slugs = slugs[: max(0, room - 60)].rsplit(", ", 1)[0] + f", ... (full list: ls {root}/workstreams)"
    return text.replace("{WORKSTREAMS}", slugs)


def reminder(role: str) -> str:
    name, root = SURFACES[role]["name"], str(REPO_ROOT)
    return (f"PM OS {name} reminder (origin {role}): route every substantive ask with one call, "
            f"`cd {root} && bash system/scripts/router-dispatch.sh --origin {role} \"Workstream: <slug>\\n<full ask>\"` "
            f"(first line always `Workstream: <slug>`), reply with its `message` line, and stop. A steer for a running "
            f"task is `router-dispatch.sh --origin {role} --steer <rt-id> '<text>'`. [ROUTER REPORT], [WORKSPACE REPORT], "
            f"and `PM OS report` messages are callbacks: present them by name, drafts verbatim, never re-route.")


def marker(session_id: str) -> Path:
    return STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id or 'unknown')}.json"


def mark_delivered(session_id: str, role: str, why: str, source: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker(session_id).write_text(json.dumps({"role": role, "why": why, "source": source}) + "\n", encoding="utf-8")
    except OSError:
        pass


def export_env(role: str) -> None:
    """DISPATCH_ORIGIN and friends for every later Bash call of this session."""
    append_env(f"export DISPATCH_ORIGIN={role}\nexport PM_OS_ROOT={shlex.quote(str(REPO_ROOT))}\n")


def append_env(text: str) -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    try:
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


def export_pane_workspace(session_id: str) -> None:
    """A wmux pane has no CMUX_WORKSPACE_ID; give the session's Bash calls the pane's tab."""
    if os.environ.get("PM_OS_WORKER_ID") or os.environ.get("CMUX_WORKSPACE_ID") or not os.environ.get("WMUX_SURFACE_ID"):
        return
    ws_id = pane_workspace_id(session_id)
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", ws_id or ""):
        append_env(f"export CMUX_WORKSPACE_ID={ws_id}\n")


PRIVATE_SESSIONS_CONFIG = Path(os.environ.get("PM_OS_PRIVATE_SESSIONS_CONFIG")
                               or REPO_ROOT / "system" / "config" / "private-sessions.json")


def private_surface() -> dict:
    """The optional private delivery surface from system/config/private-sessions.json
    ("surface": name, title, skill, deliverable, owner). {} when the file, the block, or
    a usable name or skill is missing: then no PM_OS_SURFACE value gets the note."""
    try:
        block = json.loads(PRIVATE_SESSIONS_CONFIG.read_text(encoding="utf-8")).get("surface") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(block, dict):
        return {}
    name = str(block.get("name") or "").strip().lower()
    skill = str(block.get("skill") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or name in SURFACES or name == "none":
        return {}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", skill):
        return {}
    return {"name": name, "skill": skill,
            "title": str(block.get("title") or "private session").strip(),
            "deliverable": str(block.get("deliverable") or "deliverable").strip(),
            "owner": str(block.get("owner") or "the owner").strip()}


def private_session_note(surface: dict) -> str:
    """The private session tab (PM_OS_SURFACE=<surface name>, set by that session's launcher):
    a delivery surface. CLAUDE.md memory stays off there for speed, so this short note is how
    the session learns PM OS is reachable and that the CoS routing rules are not its rules."""
    root = str(REPO_ROOT)
    owner, title, skill, deliverable = surface["owner"], surface["title"], surface["skill"], surface["deliverable"]
    whose = "the owner's" if owner == "the owner" else f"{owner}'s"
    return (
        f"This is {whose} {title} tab: a delivery surface, not CoS, Launchpad, or a PM OS worker. "
        "Build inline here. Never route work to PM OS (no router-dispatch.sh, no PM OS worker tabs), never claim CoS, "
        "and never stop to ask where work belongs: the CoS 'route, never deliver' rule does not apply in this tab. "
        f"The session flow is the /{skill} skill; follow it first, including its own subagents and "
        "background builders.\n"
        f"This tab only builds the session's {deliverable}: infrastructure and skill fixes go to CoS (say 'tell CoS'), "
        "never reworked here mid-session.\n"
        f"PM OS is available when {owner} asks for it. Repo (read access): {root}. Every installed skill works "
        "(for example drafting-docs, diagram-design, todo, building-prototypes); the todo skill reads and adds to-dos. "
        f"Run PM OS scripts by absolute path under {root}/system/scripts. Change PM OS files only through its skills "
        "and scripts: never hand-edit the repo's state files, to-do list, or workstream files. "
        f"If {owner} insists on a skill change here anyway, edit its source at {root}/.agents/skills/{skill}, "
        "never the installed copy under ~/.claude/skills, which the skill sync overwrites within minutes. Read "
        f"{root}/AGENTS.md only for "
        "the section a PM OS ask needs; it is not loaded here. External sends (Slack, email, posts) keep their usual "
        f"approval gates: draft and show, send only on {whose} word in this tab."
    )


def emit(event_name: str, context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}))


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    if disabled():
        return 0
    session_id = str(event.get("session_id") or "")
    if kind == "session-start":
        export_pane_workspace(session_id)
    bound_context = ""
    if kind == "user-prompt-submit":
        autobind = scripts_module("cos_autobind")
        if autobind is not None:
            try:
                bound_context, _ = autobind.observe(str(event.get("prompt") or ""), session_id,
                                                    cwd=str(event.get("cwd") or os.getcwd()),
                                                    transcript_path=str(event.get("transcript_path") or ""))
            except Exception as error:  # the binding never breaks the prompt
                print(f"surface-context: cos-autobind: {error}", file=sys.stderr)
    role, why = surface_role(session_id)
    private = private_surface()
    if private and why == f"PM_OS_SURFACE={private['name']}":
        # Full note at session start (again after /clear and compaction); once on the first
        # prompt only when session start never delivered it.
        if kind == "session-start":
            if session_id:
                mark_delivered(session_id, private["name"], why, str(event.get("source") or "startup"))
            emit("SessionStart", private_session_note(private))
        elif kind == "user-prompt-submit" and not (session_id and marker(session_id).exists()):
            if session_id:
                mark_delivered(session_id, private["name"], why, "user-prompt-submit")
            emit("UserPromptSubmit", private_session_note(private))
        return 0
    if not role:
        if bound_context:
            emit("UserPromptSubmit", bound_context)
        return 0
    if kind == "session-start":
        export_env(role)
        if session_id:
            mark_delivered(session_id, role, why, str(event.get("source") or "startup"))
        emit("SessionStart", render_card(role))
    elif kind == "user-prompt-submit":
        # No session id: no marker to share with other id-less sessions, so the full card.
        if session_id and marker(session_id).exists():
            text = reminder(role)
        else:
            if session_id:
                mark_delivered(session_id, role, why, "user-prompt-submit")
            text = render_card(role)
        emit("UserPromptSubmit", f"{bound_context}\n\n{text}" if bound_context else text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # a hook never breaks the session
        print(f"surface-context: {error}", file=sys.stderr)
        sys.exit(0)
