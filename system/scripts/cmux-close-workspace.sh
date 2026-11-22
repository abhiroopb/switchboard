#!/usr/bin/env bash
# Close a cmux workspace after a native "Are you sure?" dialog.
#
# Triggered from the sidebar row ✕ and the "Close workspace" context-menu
# item. The custom-sidebar DSL has no .alert/.confirmationDialog, and
# workspace.close refuses a workspace with a running process unless it is
# forced (cmux 0.65: confirmation_required, never shown for sidebar
# actions). So the sidebar creates an unfocused terminal tab in the target
# workspace whose initial command is
#
#   cmux-close-workspace.sh --exec <target_workspace_id>
#
# Running inside that helper tab, this script:
#   1. reads the workspace's title from cmux,
#   2. if nothing is running in it (no agent mid-turn or waiting on a
#      prompt, no foreground program in a plain shell), closes it at once,
#   3. otherwise shows an osascript `display dialog` asking to confirm,
#   4. on Close: unpins (pinned rows can't close) and force-closes the
#      workspace, which also removes this helper tab.
#      On Cancel: closes only its own helper tab and leaves the workspace.
set -u

find_cmux() {
  for c in /Applications/cmux.app/Contents/Resources/bin/cmux \
           "$HOME/Applications/cmux.app/Contents/Resources/bin/cmux"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  command -v cmux || echo cmux
}
CMUX=$(find_cmux)

if [ "${1:-}" != "--exec" ] || [ -z "${2:-}" ]; then
  echo "usage: $0 --exec <workspace_id>"
  exec "${SHELL:-bash}"
fi
export TARGET_WID="$2"
export CMUX

ACTION=$(python3 <<'PY'
import json, os, subprocess, sys

CMUX = os.environ["CMUX"]
target = os.environ["TARGET_WID"]
surface_id = os.environ.get("CMUX_SURFACE_ID")
LOG = os.path.expanduser("~/.config/cmux-sidebar/logs/close-dialog-error.log")

def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def rpc(method, params):
    out = subprocess.run([CMUX, "rpc", method, json.dumps(params)],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip() or f"{method} failed")
    return json.loads(out.stdout)

def self_close():
    # Close only this helper tab; the user's active surface stays selected.
    if surface_id:
        subprocess.run([CMUX, "rpc", "surface.close", json.dumps({
            "workspace_id": target,
            "surface_id": surface_id,
        })], capture_output=True)
    print("exit")  # only reached if closing the current surface did not kill us

try:
    workspaces = rpc("workspace.list", {}).get("workspaces", [])
except Exception as e:
    log(f"workspace.list failed: {e}")
    self_close()
    sys.exit(0)

title = None
for w in workspaces:
    if w.get("id") == target:
        title = w.get("custom_title") or w.get("title") or "this workspace"
        break
if title is None:
    self_close()
    sys.exit(0)

def close_now():
    try:
        rpc("workspace.action", {"action": "unpin", "workspace_id": target})
        rpc("workspace.close", {"workspace_id": target, "force": True})
    except Exception as e:
        log(f"close failed for {target}: {e}")
        self_close()

# Same busy/attention glyphs the sidebar uses: braille or circle-quadrant
# spinner = agent working, ◆ = agent waiting on a prompt.
SPINNERS = {chr(c) for c in range(0x2800, 0x2900)} | set("◐◓◑◒")
SHELLS = {"zsh", "bash", "sh", "fish", "dash", "tcsh", "login"}

def something_running():
    """True when any tab other than this helper has an agent mid-turn or a
    foreground program other than its shell. Idle agents and idle shells
    are not "running". Unknown state counts as running (ask, don't guess)."""
    try:
        surfaces = rpc("surface.list", {"workspace_id": target}).get("surfaces", [])
        ttys = {t.get("surface_ref"): t.get("tty")
                for t in rpc("debug.terminals", {}).get("terminals", [])}
    except Exception as e:
        log(f"running check failed for {target}: {e}")
        return True
    for s in surfaces:
        if s.get("id") == surface_id or s.get("type") != "terminal":
            continue
        name = (s.get("title") or "").split(" ")[0]
        if name in SPINNERS or (s.get("title") or "").startswith("◆"):
            return True
        if s.get("resume_binding"):
            continue  # idle agent session
        tty = ttys.get(s.get("ref"))
        if not tty:
            continue  # terminal never started: no process
        ps = subprocess.run(["ps", "-t", tty, "-o", "stat=,comm="],
                            capture_output=True, text=True)
        for line in ps.stdout.splitlines():
            stat, _, comm = line.strip().partition(" ")
            if "+" in stat and os.path.basename(comm.strip()).lstrip("-") not in SHELLS:
                return True
    return False

if not something_running():
    close_now()
    sys.exit(0)

# Title goes in via argv, not embedded in AppleScript source (titles are
# full of emoji/braille that would need AppleScript escaping).
script = (
    'on run argv\n'
    '  display dialog "Close \\"" & (item 1 of argv) & "\\"?" & return & return & '
    '"Anything running in it will be stopped." '
    'with title "cmux" buttons {"Cancel", "Close"} default button "Close" '
    'cancel button "Cancel" with icon caution\n'
    'end run'
)
out = subprocess.run(["osascript", "-e", script, title],
                     capture_output=True, text=True, timeout=300)
if out.returncode != 0:
    if "canceled" not in out.stderr.lower():
        log(f"dialog rc={out.returncode} stderr={out.stderr!r}")
    self_close()
    sys.exit(0)

close_now()
PY
)

# Windows has no zsh: fall back to the login shell, else bash.
if [ -n "${ACTION:-}" ]; then eval "$ACTION"; else exec "${SHELL:-bash}"; fi
