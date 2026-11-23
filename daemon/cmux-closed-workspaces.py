#!/usr/bin/env python3
"""Record cmux workspaces as they close so they can be reopened.

The workspace.closed event only carries title/cwd — not the amp-thread
resume command — so this daemon keeps a rolling snapshot of every live
workspace's surface resume bindings, latest messages, and terminal
scrollback (refreshed every 15s) and, when a workspace closes:

  1. writes its reopen recipe onto a bounded stack at
     .state/cmux-recently-closed.json
  2. writes a context/outcome file (title, thread IDs, last messages,
     scrollback tail) to .state/cmux-closed-context/
  3. saves an amp-mem observation (Amp-thread workspaces only) so the
     outcome is recallable from future sessions

Consumed by system/scripts/cmux-reopen-last-closed.sh (and the sidebar
"reopen last closed" button, which runs that script).

Managed by launchd: com.switchboard.cmux-closed-workspaces (KeepAlive).
"""
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "system" / "scripts"))
    import flock_compat as fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "system", "scripts"))
from lib.mux import runnable as _runnable  # Windows: the extensionless mux/cmux wrapper runs via Python

SWITCHBOARD_ROOT = os.environ.get("SWITCHBOARD_ROOT") or os.path.expanduser("~/Development/switchboard")
CMUX = os.environ.get("CMUX_BIN") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
AMP_MEM = os.environ.get("AMP_MEM_BIN", os.path.expanduser("~/bin/amp-mem"))
STACK = os.path.join(SWITCHBOARD_ROOT, ".state", "cmux-recently-closed.json")
STACK_LOCK = os.path.join(SWITCHBOARD_ROOT, ".state", "cmux-recently-closed.lock")
# Launcher workspaces (reopen/relaunch bootstraps) append their own wid
# here just before self-closing; record() consumes matching entries and
# skips them instead of guessing from init commands.
SKIP_FILE = os.path.join(SWITCHBOARD_ROOT, ".state", "cmux-skip-record")
CONTEXT_DIR = os.path.join(SWITCHBOARD_ROOT, ".state", "cmux-closed-context")
CAP = 50
CONTEXT_CAP = 100          # keep at most this many context files
SCROLLBACK_LINES = 200
REFRESH_SECS = 15
EVENT_STREAM_SECS = 60
LEDGER = os.path.join(os.path.dirname(__file__), "../system/scripts/running-task-state.py")
SESSION_STORE = os.environ.get("SWITCHBOARD_CMUX_SESSION_JSON") or os.path.expanduser("~/Library/Application Support/cmux/session-com.cmuxterm.app.json")

snapshot = {}
last_seen_at = {}
# wid -> monotonic time it was last seen pinned. cmux refuses to close a
# pinned workspace, so at close time "pinned" is always False — the close
# flow is unpin-then-close. Treat "was pinned within the grace window" as
# pinned so the reopen restores the pin.
last_pinned_at = {}
PIN_GRACE_SECS = 60
lock = threading.Lock()

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def rpc(method, params=None):
    out = subprocess.run(
        _runnable([CMUX, "rpc", method, json.dumps(params or {})]),
        capture_output=True, text=True, timeout=15,
    )
    return json.loads(out.stdout)


def saved_surface_bindings():
    """Join by workspace/panel UUID, never title or the first workspace agent.

    Native runner launches can have terminal.agent without resumeBinding.
    Keep each tab's thread separate and never turn a remote session local.
    """
    try:
        with open(SESSION_STORE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    bindings = {}
    for window in data.get("windows", []) or []:
        for workspace in (window.get("tabManager") or {}).get("workspaces", []) or []:
            wid = workspace.get("workspaceId")
            for panel in workspace.get("panels", []) or []:
                terminal = panel.get("terminal") or {}
                if terminal.get("isRemoteTerminal"):
                    continue
                saved = terminal.get("resumeBinding") or {}
                agent = terminal.get("agent") or {}
                if saved.get("command"):
                    binding = {"command": saved["command"], "checkpoint_id": saved.get("checkpointId")}
                else:
                    tid = agent.get("sessionId") or ""
                    if agent.get("kind") != "amp" or not re.fullmatch(r"T-[0-9a-fA-F-]{36}", tid):
                        continue
                    cwd = agent.get("workingDirectory") or panel.get("directory") or workspace.get("currentDirectory")
                    command = "amp threads continue " + shlex.quote(tid)
                    if cwd:
                        command = "cd -- " + shlex.quote(cwd) + " && " + command
                    binding = {"command": command, "checkpoint_id": tid}
                bindings[(wid, panel.get("id"))] = binding
    return bindings


def refresh():
    global snapshot
    workspaces = rpc("workspace.list").get("workspaces", [])
    saved_bindings = saved_surface_bindings()
    snap = {}
    for w in workspaces:
        wid = w.get("id")
        if not wid:
            continue
        entry = {
            "workspace_ref": w.get("ref"),
            "title": w.get("custom_title") or w.get("title") or "",
            "custom_title": w.get("custom_title"),
            "pinned": bool(w.get("pinned")),
            "cwd": w.get("current_directory"),
            "description": w.get("description"),
            "latest_submitted_message": w.get("latest_submitted_message"),
            "latest_conversation_message": w.get("latest_conversation_message"),
            "resume_commands": [],
            "thread_ids": [],
            "surfaces": [],   # per-tab reopen recipe, in tab order
            "scrollback": {},  # surface_id -> last lines of terminal text
        }
        try:
            surfaces = rpc("surface.list", {"workspace_id": wid}).get("surfaces", [])
        except Exception:
            surfaces = []
        for s in surfaces:
            binding = s.get("resume_binding") or {}
            if not binding.get("command") and s.get("type") == "terminal":
                binding = saved_bindings.get((wid, s.get("id")), binding)
            if binding.get("command"):
                entry["resume_commands"].append(binding["command"])
            if binding.get("checkpoint_id"):
                entry["thread_ids"].append(binding["checkpoint_id"])
            entry["surfaces"].append({
                "type": s.get("type"),
                "command": binding.get("command"),
                "title": s.get("title"),
                "cwd": s.get("requested_working_directory"),
                "initial_command": s.get("initial_command"),
            })
            sid = s.get("id")
            if sid and s.get("type") == "terminal":
                try:
                    out = subprocess.run(
                        _runnable([CMUX, "read-screen", "--workspace", wid,
                         "--surface", sid, "--lines", str(SCROLLBACK_LINES)]),
                        capture_output=True, text=True, timeout=10,
                    )
                    text = out.stdout.strip()
                    if text:
                        entry["scrollback"][sid] = text
                except Exception:
                    pass
        snap[wid] = entry
    now = time.monotonic()
    with lock:
        # A close can arrive after the next workspace.list has removed it.
        # Keep its resume recipe through two bounded event subscriptions.
        last_seen_at.update({wid: now for wid in snap})
        snapshot.update(snap)
        for wid in list(snapshot):
            if now - last_seen_at.get(wid, 0) > EVENT_STREAM_SECS * 2:
                snapshot.pop(wid, None)
                last_seen_at.pop(wid, None)
        for wid, entry in snap.items():
            if entry.get("pinned"):
                last_pinned_at[wid] = now
        # drop history for workspaces that no longer exist and are past grace
        for wid in list(last_pinned_at):
            if wid not in snap and now - last_pinned_at[wid] > PIN_GRACE_SECS:
                del last_pinned_at[wid]
    # Only fresh surface bindings enter the ledger, never retained close data.
    bindings = [
        {"thread_id": tid, "workspace_ref": entry["workspace_ref"]}
        for entry in snap.values() for tid in entry["thread_ids"]
    ]
    result = subprocess.run(
        [sys.executable, LEDGER, "sync-workspaces"], input=json.dumps(bindings),
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise RuntimeError(f"ledger workspace sync failed: {result.stderr.strip()}")


def refresher():
    while True:
        try:
            refresh()
        except Exception as e:  # cmux not running yet, etc.
            print(f"refresh error: {e}", flush=True)
        time.sleep(REFRESH_SECS)


def slugify(text, limit=40):
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:limit] or "untitled"


def write_context_file(entry, info):
    """Persist title/thread/messages/scrollback so the closed workspace's
    context survives (and is discoverable by amp-mem / future sessions)."""
    os.makedirs(CONTEXT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    wid_tag = (entry.get("workspace_id") or "")[-4:].lower()
    path = os.path.join(CONTEXT_DIR, f"{ts}-{slugify(entry['title'])}-{wid_tag}.md")
    lines = [
        f"# Closed workspace: {entry['title'] or '(untitled)'}",
        "",
        f"- Closed at: {entry['closed_at']}",
        f"- cwd: {entry.get('cwd') or '?'}",
    ]
    for tid in info.get("thread_ids", []):
        # A Claude binding's checkpoint id is its session id, which has no web page.
        if str(tid).startswith("T-"):
            lines.append(f"- Amp thread: {tid} (https://ampcode.com/threads/{tid})")
        else:
            lines.append(f"- Claude session: {tid} (claude --resume {tid})")
    if info.get("description"):
        lines += ["", "## Workspace description", "", str(info["description"])]
    if info.get("latest_submitted_message"):
        lines += ["", "## Last submitted message", "", str(info["latest_submitted_message"])]
    if info.get("latest_conversation_message"):
        lines += ["", "## Last conversation message", "", str(info["latest_conversation_message"])]
    for sid, text in (info.get("scrollback") or {}).items():
        lines += ["", f"## Terminal scrollback (last {SCROLLBACK_LINES} lines, ≤{REFRESH_SECS}s before close)", "", "```", text, "```"]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    # bounded retention
    files = sorted(f for f in os.listdir(CONTEXT_DIR) if f.endswith(".md"))
    for old in files[:-CONTEXT_CAP]:
        try:
            os.remove(os.path.join(CONTEXT_DIR, old))
        except OSError:
            pass
    return path


def save_to_amp_mem(entry, info, context_path):
    """Record the close in amp-mem so future sessions can recall the outcome."""
    title = entry["title"] or "(untitled)"
    parts = [f"cmux workspace '{title}' closed."]
    if info.get("thread_ids"):
        parts.append("Thread(s): " + ", ".join(info["thread_ids"]) + ".")
    if entry.get("cwd"):
        parts.append(f"cwd: {entry['cwd']}.")
    last = info.get("latest_conversation_message") or info.get("latest_submitted_message")
    if last:
        parts.append(f"Last message: {str(last)[:400]}")
    parts.append(f"Full context (incl. terminal scrollback): {context_path}")
    try:
        subprocess.run(
            [AMP_MEM, "save", "observation",
             f"cmux workspace closed: {title}"[:100],
             " ".join(parts),
             "--tags", "cmux,workspace-close"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        print(f"amp-mem save error: {e}", flush=True)


def consume_skip(wid):
    """True if this close was pre-marked as a launcher self-close."""
    try:
        with open(SKIP_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            wids = [l.strip() for l in f if l.strip()]
            if wid not in wids:
                return False
            keep = [w for w in wids if w != wid][-20:]  # cap stale leftovers
            f.seek(0)
            f.truncate()
            f.write("".join(w + "\n" for w in keep))
            return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"skip-file error: {e}", flush=True)
        return False


def record(wid, payload):
    with lock:
        info = snapshot.get(wid, {})
        # Pinned workspaces can't be closed (cmux protects them), so the
        # snapshot at close time usually says unpinned. Count a pin seen
        # within the grace window as pinned so reopen restores it.
        pin_seen = last_pinned_at.get(wid)
        was_pinned = bool(info.get("pinned")) or (
            pin_seen is not None
            and time.monotonic() - pin_seen <= PIN_GRACE_SECS
        )
    entry = {
        "closed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workspace_id": wid,
        "title": payload.get("custom_title") or info.get("title") or payload.get("title") or "",
        "custom_title": payload.get("custom_title") or info.get("custom_title"),
        "pinned": was_pinned,
        "cwd": payload.get("cwd") or info.get("cwd"),
        "resume_commands": info.get("resume_commands", []),
        "surfaces": info.get("surfaces", []),
    }
    # Skip launcher self-closes: reopen/relaunch bootstrap workspaces mark
    # themselves in SKIP_FILE just before closing (multi-tab rebuilds close
    # their launcher after spawning the replacement) — recording them would
    # put junk reopen recipes on the stack. Everything else is recorded,
    # including untitled reopened terminals.
    if consume_skip(wid):
        print(f"skipped launcher close: {entry['title'] or '(untitled)'} ({wid})", flush=True)
        return
    try:
        context_path = write_context_file(entry, info)
        entry["context_file"] = context_path
        # Only memorialize workspaces that ran an Amp thread — keeps
        # amp-mem free of throwaway terminals (incl. reopen bootstraps).
        if info.get("thread_ids"):
            save_to_amp_mem(entry, info, context_path)
    except Exception as e:
        print(f"context capture error: {e}", flush=True)
    # flock against cmux-reopen-last-closed.sh (pop side): both do a
    # read-modify-write of the stack, so serialize via the shared lock file
    # and write through a per-process temp name.
    with open(STACK_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        stack = []
        try:
            with open(STACK) as f:
                stack = json.load(f)
        except Exception:
            pass
        if not isinstance(stack, list):
            stack = []
        stack.append(entry)
        stack = stack[-CAP:]
        tmp = f"{STACK}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(stack, f, indent=1)
        _os_replace(tmp, STACK)
    print(f"recorded close: {entry['title'] or '(untitled)'} ({wid})", flush=True)


def main():
    threading.Thread(target=refresher, daemon=True).start()
    after = None
    boot_id = None
    while True:
        # Do not leave a five-day-old CLI reconnect loop running after cmux
        # upgrades. Retire each subscriber, then replay from its last event.
        args = [CMUX, "events", "--name", "workspace.closed", "--no-heartbeat"]
        if after is not None:
            args += ["--after", str(after)]
        proc = subprocess.Popen(
            _runnable(args), stdout=subprocess.PIPE, stderr=None, text=True,
        )
        timer = threading.Timer(EVENT_STREAM_SECS, proc.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "ack":
                    new_boot = ev.get("boot_id")
                    if boot_id is not None and new_boot != boot_id:
                        # Sequence numbers belong to a cmux boot, not a day.
                        boot_id, after = new_boot, 0
                        break
                    boot_id = new_boot
                    if after is None:
                        after = ev.get("resume", {}).get("latest_seq")
                    if ev.get("resume", {}).get("gap"):
                        print("close event replay gap: older events no longer retained", flush=True)
                    continue
                if ev.get("name") != "workspace.closed":
                    continue
                payload = ev.get("payload") or {}
                wid = payload.get("workspace_id") or ev.get("workspace_id")
                if wid:
                    record(wid, payload)
                    after = ev.get("seq", after)
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        if os.name == "nt" and boot_id is None and proc.returncode != 0:
            # The wmux shim has no `cmux events`: no close stream exists to follow, so
            # stop instead of respawning and logging every 5 seconds.
            print("cmux events unavailable on this backend; close recorder not running", flush=True)
            return
        time.sleep(5)  # events stream ended (cmux quit) — retry


if __name__ == "__main__":
    main()
