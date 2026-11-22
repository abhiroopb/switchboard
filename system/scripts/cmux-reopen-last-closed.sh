#!/usr/bin/env bash
# Reopen the most recently closed cmux workspace.
#
# Stack is recorded by daemon/cmux-closed-workspaces.py at
# .state/cmux-recently-closed.json (amp workspaces reopen via their
# `amp threads continue <thread>` resume command; plain terminals
# reopen in their old cwd).
#
# Single-tab workspaces reopen in place (this terminal execs the resume
# command) and adopt the old custom title. Multi-tab workspaces are
# rebuilt as a fresh workspace via `cmux new-workspace --layout` with one
# tab per recorded terminal surface, each running its own resume command;
# the launcher workspace then closes itself (the close-recorder daemon
# skips recording these bootstraps). Browser tabs are not restored.
#
#   cmux-reopen-last-closed.sh          open the last closed workspace
#   cmux-reopen-last-closed.sh --exec   pop the stack and restore in THIS
#                                       terminal (used as the initial
#                                       command of the fresh workspace the
#                                       sidebar ↩ button creates)
set -u
STACK="${PM_OS_ROOT:-$HOME/Development/pm-os}/.state/cmux-recently-closed.json"
CMUX="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
export CMUX
SELF="${PM_OS_ROOT:-$HOME/Development/pm-os}/system/scripts/cmux-reopen-last-closed.sh"
# flock_compat.py sits next to this script; the inline Python below imports it when
# the real fcntl is missing (Windows).
PM_OS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && { pwd -W 2>/dev/null || pwd; })"   # Git Bash: pwd -W is the C:/ form native Python can open
export PM_OS_SCRIPTS_DIR

if [ "${1:-}" != "--exec" ]; then
  # Peek first: don't create a blank workspace when there's nothing to
  # reopen. The actual pop happens inside the fresh workspace (--exec),
  # so this is only a cheap guard, not the consistency mechanism.
  if ! python3 - "$STACK" <<'PY'
import json, sys
try:
    stack = json.load(open(sys.argv[1]))
except Exception:
    stack = []
sys.exit(0 if isinstance(stack, list) and stack else 3)
PY
  then
    echo "No recently closed workspace recorded." >&2
    exit 0
  fi
  # Create a workspace that pops the stack itself (single pop path).
  exec "$CMUX" rpc workspace.create \
    "{\"initial_command\": \"/bin/bash '$SELF' --exec\"}"
fi

# Pop under the same flock the recorder daemon uses for pushes, so a
# concurrent close (push) can't be lost or corrupt the file.
ENTRY=$(python3 - "$STACK" <<'PY'
import json, os, sys
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    if os.environ.get("PM_OS_SCRIPTS_DIR"):
        sys.path.insert(0, os.environ["PM_OS_SCRIPTS_DIR"])
    import flock_compat as fcntl
path = sys.argv[1]
lock = path.replace(".json", ".lock")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(lock, "w") as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        stack = json.load(open(path))
    except Exception:
        stack = []
    if not isinstance(stack, list) or not stack:
        sys.exit(3)
    entry = stack.pop()
    tmp = f"{path}.{os.getpid()}.tmp"
    json.dump(stack, open(tmp, "w"), indent=1)
    os.replace(tmp, path)
print(json.dumps(entry))
PY
)
if [ -z "${ENTRY:-}" ]; then
  echo "No recently closed workspace recorded."
  echo "(The recorder daemon com.pm-os.cmux-closed-workspaces logs closes here.)"
  exec "${SHELL:-bash}"
fi

export ENTRY
ACTION=$(python3 <<'PY'
import json, os, shlex, subprocess, sys
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    if os.environ.get("PM_OS_SCRIPTS_DIR"):
        sys.path.insert(0, os.environ["PM_OS_SCRIPTS_DIR"])
    import flock_compat as fcntl

CMUX = os.environ.get("CMUX_BIN") or os.environ.get("CMUX") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
SKIP = os.path.expanduser(os.path.join(os.environ.get("PM_OS_ROOT") or "~/Development/pm-os", ".state", "cmux-skip-record"))

def mark_skip(target):
    # Tell the close-recorder daemon this is a launcher self-close, not a
    # real workspace close worth recording. Must land before the close.
    if not target:
        return
    try:
        with open(SKIP, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(target + "\n")
    except Exception:
        pass

entry = json.loads(os.environ["ENTRY"])
custom_title = entry.get("custom_title") or ""
title = custom_title or entry.get("title") or ""
cwd = entry.get("cwd") or ""
wid = os.environ.get("CMUX_WORKSPACE_ID")

tabs = [s for s in (entry.get("surfaces") or []) if s.get("type") == "terminal"]
if not tabs:  # entries recorded before per-surface capture
    cmds = entry.get("resume_commands") or []
    tabs = [{"command": c} for c in cmds] or [{}]

def tab_cmd(s):
    if s.get("command"):
        return s["command"]
    scwd = s.get("cwd") or cwd
    return f"cd {shlex.quote(scwd)} 2>/dev/null || true" if scwd else ""

def restore_pin(target):
    if entry.get("pinned") and target:
        subprocess.run([CMUX, "workspace-action", "--action", "pin",
                        "--workspace", target], capture_output=True)

if len(tabs) == 1:
    # Restore in place: adopt the curated name, exec the resume command.
    if custom_title and wid:
        subprocess.run([CMUX, "workspace-action", "--action", "rename",
                        "--workspace", wid, "--title", custom_title],
                       capture_output=True)
    restore_pin(wid)
    cmd = tabs[0].get("command") or ""
    if cmd:
        print('exec "${SHELL:-bash}" -c ' + shlex.quote(cmd))
    elif cwd:
        print(f'cd {shlex.quote(cwd)} 2>/dev/null; exec "${{SHELL:-bash}}"')
    else:
        print('exec "${SHELL:-bash}"')
else:
    # Rebuild all tabs atomically in a fresh workspace, then close this
    # launcher (the close-recorder daemon skips bootstrap closes).
    layout = {"pane": {"surfaces": [
        {"type": "terminal", "command": tab_cmd(s)} for s in tabs
    ]}}
    args = [CMUX, "new-workspace", "--focus", "true", "--layout", json.dumps(layout)]
    if custom_title:
        args += ["--name", custom_title]
    if cwd:
        args += ["--cwd", cwd]
    env = dict(os.environ, CMUX_QUIET="1")
    out = subprocess.run(args, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        print("echo " + shlex.quote(f"reopen failed: {out.stderr.strip()}") + '; exec "${SHELL:-bash}"')
    else:
        # `new-workspace` prints "OK workspace:<n>" — re-pin the rebuild.
        ref = (out.stdout.strip().split() or ["", ""])[-1]
        if ref.startswith("workspace:"):
            restore_pin(ref)
        if wid:
            mark_skip(wid)
            subprocess.run([CMUX, "rpc", "workspace.close",
                            json.dumps({"workspace_id": wid})],
                           capture_output=True)
        print('exec "${SHELL:-bash}"')  # only reached if self-close failed
PY
)

TITLE=$(echo "$ENTRY" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e.get("custom_title") or e.get("title") or "")')
echo "Reopening: ${TITLE:-\(untitled\)}"
eval "${ACTION:-exec "${SHELL:-bash}"}"
