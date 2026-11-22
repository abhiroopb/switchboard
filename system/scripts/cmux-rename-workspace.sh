#!/usr/bin/env bash
# Rename a cmux workspace via a native macOS text dialog.
#
# Triggered from the sidebar context menu ("Rename…"): the sidebar cannot
# collect text (the custom-sidebar DSL has no input primitive), so it
# creates an unfocused terminal tab in the target workspace whose initial
# command is
#
#   cmux-rename-workspace.sh --exec <target_workspace_id>
#
# Running inside that helper tab, this script:
#   1. reads the workspace's current title from cmux,
#   2. shows an osascript `display dialog` prefilled with it,
#   3. on OK: applies the rename, then closes only its own helper tab.
#      On Cancel or empty input: no rename, just close the helper tab.
#
# The user's active terminal stays selected throughout; no launcher
# workspace appears in the sidebar or close-recorder history.
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

def rpc(method, params):
    out = subprocess.run([CMUX, "rpc", method, json.dumps(params)],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"{method} failed")
    return json.loads(out.stdout)

def self_close():
    # Close only this helper tab; the user's active surface stays selected.
    if surface_id:
        subprocess.run([CMUX, "rpc", "surface.close", json.dumps({
            "workspace_id": target,
            "surface_id": surface_id,
        })], capture_output=True)
    print("exit")  # only reached if closing the current surface did not kill us

# Read the current title BEFORE showing the dialog; if cmux RPC fails,
# leave the target untouched, log the failure, and close the helper tab.
try:
    workspaces = rpc("workspace.list", {}).get("workspaces", [])
except Exception as e:
    log = os.path.expanduser("~/.config/cmux-sidebar/logs/rename-dialog-error.log")
    try:
        with open(log, "a") as f:
            f.write(f"workspace.list failed: {e}\n")
    except Exception:
        pass
    self_close()
    sys.exit(0)

# Strip only transient agent-state prefixes from the live-title fallback.
# Explicit custom titles are user-owned and must remain byte-for-byte intact.
def clean_live_title(title):
    first, separator, rest = title.partition(" ")
    idle_glyphs = {"✳", "✻", "✽", "✢", "✶", "✷", "✸", "✹", "✺", "·", "•", "∗", "*"}
    is_braille = len(first) == 1 and "\u2800" <= first <= "\u28ff"
    if separator and (is_braille or first == "◆" or first in idle_glyphs):
        return rest
    return title

current = ""
found = False
for w in workspaces:
    if w.get("id") == target:
        custom_title = w.get("custom_title")
        current = custom_title if custom_title else clean_live_title(w.get("title") or "")
        found = True
        break
if not found:
    self_close()
    sys.exit(0)

# Native text dialog. `display dialog` exits non-zero on Cancel. The
# current title goes in via argv, NOT embedded in the AppleScript source:
# embedding would need AppleScript-escaping (json.dumps produces \uXXXX
# for non-ASCII — a syntax error in AppleScript, and titles here are full
# of π/braille/emoji).
script = (
    'on run argv\n'
    '  set r to display dialog "Rename workspace:" default answer (item 1 of argv) '
    'with title "cmux" buttons {"Cancel", "Rename"} default button "Rename"\n'
    '  return text returned of r\n'
    'end run'
)
out = subprocess.run(["osascript", "-e", script, current],
                     capture_output=True, text=True, timeout=120)
if out.returncode != 0:
    # Cancel exits 1 with "User canceled." on stderr; anything else is a
    # real dialog failure worth logging (failures are otherwise invisible:
    # the launcher self-closes either way).
    if "canceled" not in out.stderr.lower():
        log = os.path.expanduser("~/.config/cmux-sidebar/logs/rename-dialog-error.log")
        try:
            with open(log, "a") as f:
                f.write(f"rc={out.returncode} stderr={out.stderr!r}\n")
        except Exception:
            pass
    self_close()
    sys.exit(0)

new_title = out.stdout.strip()
# Empty input is treated as cancel: an accidental Clear+OK should not wipe
# a curated title (use cmux's own UI to intentionally clear one).
if new_title and new_title != current:
    subprocess.run([CMUX, "workspace-action", "--action", "rename",
                    "--workspace", target, "--title", new_title],
                   capture_output=True)
self_close()
PY
)

# Windows has no zsh: fall back to the login shell, else bash.
if [ -n "${ACTION:-}" ]; then eval "$ACTION"; else exec "${SHELL:-bash}"; fi
