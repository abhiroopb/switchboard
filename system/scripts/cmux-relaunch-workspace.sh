#!/usr/bin/env bash
# Relaunch a cmux workspace in place.
#
# Triggered from the switchboard sidebar context menu. "Relaunch" creates a FRESH
# workspace whose initial command is
#
#   cmux-relaunch-workspace.sh --exec <old_workspace_id>
#
# "Copy thread URL" creates an unfocused helper tab whose initial command is
#
#   cmux-relaunch-workspace.sh --copy-thread-url <workspace_id>
#
# That mode reuses the same ordered surface/resume-binding discovery as
# relaunch, prefers the focused Amp terminal, copies its canonical URL, sends
# visible cmux feedback, and closes only the helper tab.
#
# "Relaunch all" creates a disposable coordinator workspace whose command is
#
#   cmux-relaunch-workspace.sh --all <origin_workspace_id>
#
# It snapshots the current workspace IDs once and serially creates the same
# relauncher used above for every entry, including active/draft Amp sessions,
# pinned/system workspaces, and plain terminals. Only the disposable
# coordinator is excluded. Replacement workspaces are therefore never added
# back to the queue, and all title/pin/resume/context behavior stays owned by
# the single-workspace path.
#
# Running inside that fresh workspace, this script:
#   1. reads the old workspace's surfaces (tab order, per-tab resume
#      command and cwd), custom title, pin state, and cwd,
#   2. single-tab: carries the title + pin over, closes the old
#      workspace, and execs the resume command here — same thread,
#      clean terminal.
#   3. multi-tab: rebuilds ALL terminal tabs atomically in a fresh
#      workspace via `cmux new-workspace --layout` (one tab per surface,
#      each running its own resume command), closes the old workspace,
#      then closes this launcher (the close-recorder daemon skips
#      recording bootstrap closes). Browser tabs are not restored.
#
# The old workspace is only closed AFTER its metadata was successfully
# read; if cmux RPC fails, this becomes a plain shell and the old
# workspace is left untouched.
#
# Plain-terminal workspaces (no resume binding) reopen as a shell in the
# old cwd.
set -u

find_cmux() {
  if [ -n "${CMUX_BIN:-}" ] && [ -x "$CMUX_BIN" ]; then
    echo "$CMUX_BIN"
    return
  fi
  for c in /Applications/cmux.app/Contents/Resources/bin/cmux \
           "$HOME/Applications/cmux.app/Contents/Resources/bin/cmux"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  command -v cmux || echo cmux
}
CMUX=$(find_cmux)
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

MODE="${1:-}"
if [ -z "${2:-}" ] || { [ "$MODE" != "--exec" ] && [ "$MODE" != "--exec-bulk" ] && [ "$MODE" != "--all" ] && [ "$MODE" != "--copy-thread-url" ] && [ "$MODE" != "--resolve-thread-id" ]; }; then
  echo "usage: $0 --exec|--all|--copy-thread-url <workspace_id>" >&2
  echo "       $0 --resolve-thread-id <surface-list-fixture.json>" >&2
  exec "${SHELL:-bash}"
fi
export MODE TARGET="$2" CMUX SELF

ACTION=$(python3 <<'PY'
import json, os, re, shlex, subprocess, sys, time
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("SWITCHBOARD_SCRIPTS_DIR") or _os.path.join(_os.environ.get("SWITCHBOARD_ROOT") or _os.path.expanduser("~/Development/switchboard"), "system", "scripts"))
    import flock_compat as fcntl

CMUX = os.environ["CMUX"]
SELF = os.environ["SELF"]
SKIP = os.path.expanduser(
    os.environ.get("CMUX_RELAUNCH_SKIP_FILE", os.path.join(os.environ.get("SWITCHBOARD_ROOT") or "~/Development/switchboard", ".state", "cmux-skip-record"))
)
RELAUNCH_LOCK = os.path.expanduser(
    os.environ.get("CMUX_RELAUNCH_ALL_LOCK", "~/.local/state/cmux/relaunch-all.lock")
)
MODE = os.environ["MODE"]
target = os.environ["TARGET"]
surface_id = os.environ.get("CMUX_SURFACE_ID")
workspace_id = os.environ.get("CMUX_WORKSPACE_ID")
dry_run = os.environ.get("CMUX_RELAUNCH_ALL_DRY_RUN") == "1"
no_notify = os.environ.get("CMUX_RELAUNCH_ALL_NO_NOTIFY") == "1"
try:
    relaunch_timeout = float(os.environ.get("CMUX_RELAUNCH_ALL_TIMEOUT_SECONDS", "30"))
except ValueError:
    relaunch_timeout = 30.0
THREAD_ID = re.compile(
    r"^T-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

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

def rpc(method, params):
    out = subprocess.run([CMUX, "rpc", method, json.dumps(params)],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"{method} failed")
    return json.loads(out.stdout)

def resume_binding(surface):
    binding = surface.get("resume_binding")
    return binding if isinstance(binding, dict) else {}

def terminal_surfaces(surfaces):
    # surface.list is already in tab order; relaunch/reopen use that order to
    # rebuild multi-tab workspaces, so URL fallback follows the same contract.
    return [s for s in surfaces if s.get("type") == "terminal"]

def workspace_entries():
    return rpc("workspace.list", {}).get("workspaces", [])

def workspace_still_exists(candidate_id):
    try:
        return any(
            workspace.get("id") == candidate_id
            for workspace in workspace_entries()
        )
    except Exception:
        return True

def workspace_uuid(candidate_ref):
    if not candidate_ref:
        return None
    try:
        for workspace in workspace_entries():
            if candidate_ref in (workspace.get("id"), workspace.get("ref")):
                return workspace.get("id")
    except Exception:
        pass
    return candidate_ref if not candidate_ref.startswith("workspace:") else None

def close_workspace(candidate_id):
    try:
        return subprocess.run(
            [CMUX, "rpc", "workspace.close", json.dumps({"workspace_id": candidate_id})],
            capture_output=True, timeout=15,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False

def system_notify(title, body):
    if no_notify:
        return
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    subprocess.run(
        ["osascript", "-e", script, title, body],
        capture_output=True, text=True,
    )

def close_coordinator():
    if workspace_id and not dry_run:
        mark_skip(workspace_id)
        close_workspace(workspace_id)

def create_launcher(command, cwd):
    args = [CMUX, "new-workspace", "--focus", "false"]
    if cwd:
        args += ["--cwd", cwd]
    args += ["--command", command]
    env = dict(os.environ, CMUX_QUIET="1")
    out = subprocess.run(args, capture_output=True, text=True, timeout=15, env=env)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "workspace creation failed")
    launcher_ref = (out.stdout.strip().split() or [""])[-1]
    if not launcher_ref.startswith("workspace:"):
        raise RuntimeError(f"unexpected workspace creation response: {out.stdout.strip()}")
    deadline = time.monotonic() + 2
    launcher_id = workspace_uuid(launcher_ref)
    while not launcher_id and time.monotonic() < deadline:
        time.sleep(0.05)
        launcher_id = workspace_uuid(launcher_ref)
    if not launcher_id:
        close_workspace(launcher_ref)
        raise RuntimeError(f"could not resolve created workspace {launcher_ref}")
    return launcher_ref, launcher_id

def discard_launcher(launcher_ref, launcher_id):
    if not launcher_ref and not launcher_id:
        return
    ref = launcher_id or launcher_ref
    subprocess.run(
        [CMUX, "workspace-action", "--action", "unpin", "--workspace", ref],
        capture_output=True, text=True,
    )
    if launcher_id:
        mark_skip(launcher_id)
    close_workspace(ref)

def replacement_for(before_ids, launcher_id, is_multi_tab):
    # A single-tab relaunch keeps the launcher as the replacement. A multi-tab
    # relaunch creates its replacement first, then closes the launcher.
    # If surface discovery failed, retaining a live launcher is the only safe
    # choice: it may be the successfully resumed single-tab workspace.
    if is_multi_tab is not True and workspace_still_exists(launcher_id):
        return launcher_id if workspace_still_exists(launcher_id) else None

    deadline = time.monotonic() + 2
    new_entries = []
    while time.monotonic() < deadline:
        try:
            new_entries = [
                workspace for workspace in workspace_entries()
                if workspace.get("id") not in before_ids
                and workspace.get("id") != workspace_id
                and workspace.get("id") != launcher_id
            ]
        except Exception:
            return None
        if len(new_entries) == 1 and not workspace_still_exists(launcher_id):
            break
        time.sleep(0.1)

    if workspace_still_exists(launcher_id):
        # The multi-tab bootstrap failed to self-close. Remove only that known
        # launcher so bulk relaunch never leaves a duplicate workspace behind.
        discard_launcher(launcher_id, launcher_id)
    return new_entries[0].get("id") if len(new_entries) == 1 else None

def relaunch_all():
    if not workspace_id:
        print("Relaunch all must run inside its disposable cmux workspace.", file=sys.stderr)
        return

    os.makedirs(os.path.dirname(RELAUNCH_LOCK), exist_ok=True)
    lock = open(RELAUNCH_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        system_notify("Relaunch all", "A relaunch-all operation is already running.")
        close_coordinator()
        return

    try:
        snapshot = [
            workspace for workspace in workspace_entries()
            if workspace.get("id") and workspace.get("id") != workspace_id
        ]
    except Exception as error:
        system_notify("Relaunch all failed", f"Could not list cmux workspaces: {error}")
        close_coordinator()
        return

    relaunched = 0
    failures = []
    replacements = {}
    for candidate in snapshot:
        candidate_id = candidate["id"]
        label = candidate.get("custom_title") or candidate.get("title") or candidate_id
        command = f"/bin/bash {shlex.quote(SELF)} --exec-bulk {shlex.quote(candidate_id)}"
        if dry_run:
            relaunched += 1
            print(f"Would relaunch {label}: {command}", file=sys.stderr)
            continue

        if not workspace_still_exists(candidate_id):
            failures.append((label, "workspace closed before dispatch"))
            print(f"Failed to relaunch {label}: workspace closed before dispatch", file=sys.stderr)
            continue

        try:
            before_entries = workspace_entries()
            before_ids = {workspace.get("id") for workspace in before_entries}
        except Exception as error:
            failures.append((label, f"could not snapshot current state: {error}"))
            print(f"Failed to relaunch {label}: {error}", file=sys.stderr)
            continue

        launcher_ref = None
        launcher_id = None
        is_multi_tab = None
        try:
            surfaces = rpc("surface.list", {"workspace_id": candidate_id}).get("surfaces", [])
            is_multi_tab = len(terminal_surfaces(surfaces)) > 1
        except Exception:
            # Surface discovery is best-effort metadata for replacement
            # tracking, never a filter: the relaunch is still attempted.
            pass
        try:
            launcher_ref, launcher_id = create_launcher(
                command, candidate.get("current_directory") or ""
            )
        except Exception as error:
            failures.append((label, f"dispatch failed: {error}"))
            print(f"Failed to dispatch {label}: {error}", file=sys.stderr)
            continue

        deadline = time.monotonic() + max(0.01, relaunch_timeout)
        while workspace_still_exists(candidate_id) and time.monotonic() < deadline:
            if launcher_id and not workspace_still_exists(launcher_id):
                break
            time.sleep(0.25)
        if workspace_still_exists(candidate_id):
            failures.append((label, "original workspace did not close"))
            print(f"Failed to relaunch {label}: original workspace did not close", file=sys.stderr)
            discard_launcher(launcher_ref, launcher_id)
            continue

        replacement_id = replacement_for(before_ids, launcher_id, is_multi_tab)
        if not replacement_id:
            failures.append((label, "replacement workspace was not found"))
            print(f"Failed to relaunch {label}: replacement workspace was not found", file=sys.stderr)
            continue
        replacements[candidate_id] = replacement_id
        relaunched += 1
        print(f"Relaunched {label}", file=sys.stderr)

    total = len(snapshot)
    failed = len(failures)
    summary = f"Relaunched {relaunched} of {total} workspaces; failed {failed}."
    if failures:
        names = ", ".join(label for label, _ in failures[:3])
        if failed > 3:
            names += f", +{failed - 3} more"
        summary += f" Failed: {names}."
    print(summary, file=sys.stderr)
    if dry_run:
        return
    system_notify("Relaunch all complete", summary)
    focus_target = replacements.get(target, target)
    if workspace_still_exists(focus_target):
        try:
            subprocess.run(
                [CMUX, "rpc", "workspace.select", json.dumps({"workspace_id": focus_target})],
                capture_output=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            pass
    close_coordinator()

def amp_thread_id(surface):
    binding = resume_binding(surface)
    thread_id = binding.get("checkpoint_id")
    if binding.get("kind") != "amp" or not isinstance(thread_id, str):
        return None
    return thread_id if THREAD_ID.fullmatch(thread_id) else None

def resolve_amp_thread_id(surfaces):
    terminals = terminal_surfaces(surfaces)
    # The helper tab is created unfocused, preserving the user's selected tab.
    # Prefer that selected Amp terminal; otherwise use the first valid Amp
    # binding in the same tab order relaunch/reopen preserve.
    for surface in terminals:
        if surface.get("focused"):
            thread_id = amp_thread_id(surface)
            if thread_id:
                return thread_id
    for surface in terminals:
        thread_id = amp_thread_id(surface)
        if thread_id:
            return thread_id
    return None

def feedback_surface_id(surfaces):
    # Attach feedback to the user's existing selected surface, not this helper
    # tab: notifications associated with a surface disappear when it closes.
    candidates = [s for s in surfaces if s.get("id") != surface_id]
    for surface in candidates:
        if surface.get("focused") and surface.get("id"):
            return surface["id"]
    for surface in candidates:
        if surface.get("type") == "terminal" and surface.get("id"):
            return surface["id"]
    return None

def notify(title, body, feedback_surface=None):
    command = [CMUX, "notify", "--title", title, "--body", body,
               "--workspace", target]
    if feedback_surface:
        command += ["--surface", feedback_surface]
    out = subprocess.run(command, capture_output=True, text=True)
    if out.returncode != 0 or not feedback_surface:
        # Best-effort fallback if cmux's notification surface is unavailable.
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        subprocess.run(["osascript", "-e", script, title, body],
                       capture_output=True, text=True)

def self_close():
    if surface_id:
        subprocess.run([CMUX, "rpc", "surface.close", json.dumps({
            "workspace_id": target,
            "surface_id": surface_id,
        })], capture_output=True)
    print("exit")  # only reached if closing the helper surface did not kill us

if MODE == "--all":
    relaunch_all()
    print("exit")
    sys.exit(0)

if MODE == "--resolve-thread-id":
    # Fixture mode is the unit-test seam for binding selection. It performs no
    # RPC, clipboard, notification, or close operation.
    with open(target) as fixture:
        payload = json.load(fixture)
    surfaces = payload.get("surfaces", []) if isinstance(payload, dict) else payload
    thread_id = resolve_amp_thread_id(surfaces if isinstance(surfaces, list) else [])
    if thread_id:
        print("printf '%s\\n' " + shlex.quote(thread_id))
    else:
        print("exit 4")
    sys.exit(0)

if MODE == "--copy-thread-url":
    try:
        surfaces = rpc("surface.list", {"workspace_id": target}).get("surfaces", [])
    except Exception as error:
        notify("Copy thread URL failed", f"Could not read cmux bindings: {error}")
        self_close()
        sys.exit(0)

    feedback_surface = feedback_surface_id(surfaces)
    thread_id = resolve_amp_thread_id(surfaces)
    if not thread_id:
        notify("Copy thread URL failed", "No valid Amp thread binding found. Clipboard unchanged.", feedback_surface)
        self_close()
        sys.exit(0)

    url = f"https://ampcode.com/threads/{thread_id}"
    copied = subprocess.run(["/usr/bin/pbcopy"], input=url, text=True,
                            capture_output=True)
    if copied.returncode == 0:
        notify("Amp thread URL copied", url, feedback_surface)
    else:
        notify("Copy thread URL failed", "macOS clipboard write failed.", feedback_surface)
    self_close()
    sys.exit(0)

# Read everything before touching the old workspace. A failed RPC leaves it
# untouched and turns the launcher into a shell instead of destroying state.
old_wid = target
wid = os.environ.get("CMUX_WORKSPACE_ID")
try:
    surfaces = rpc("surface.list", {"workspace_id": old_wid}).get("surfaces", [])
    workspaces = rpc("workspace.list", {}).get("workspaces", [])
except Exception as error:
    message = f"Could not read workspace {old_wid} from cmux — leaving it untouched. ({error})"
    if MODE == "--exec-bulk" and wid:
        mark_skip(wid)
        close_workspace(wid)
        print("exit")
        sys.exit(0)
    print("echo " + shlex.quote(message) + '; exec "${SHELL:-bash}"')
    sys.exit(0)

title, cwd, pinned = "", "", False
found = False
for w in workspaces:
    if w.get("id") == old_wid:
        cwd = w.get("current_directory") or ""
        title = w.get("custom_title") or ""
        pinned = bool(w.get("pinned"))
        found = True
        break
if not found:
    message = f"Could not read workspace {old_wid} from cmux — leaving it untouched."
    if MODE == "--exec-bulk" and wid:
        mark_skip(wid)
        close_workspace(wid)
        print("exit")
        sys.exit(0)
    print("echo " + shlex.quote(message) + '; exec "${SHELL:-bash}"')
    sys.exit(0)

tabs = terminal_surfaces(surfaces) or [{}]

def tab_cmd(s):
    b = resume_binding(s)
    if b.get("command"):
        return b["command"]
    scwd = s.get("requested_working_directory") or cwd
    return f"cd {shlex.quote(scwd)} 2>/dev/null || true" if scwd else ""

def action(name, target, *extra):
    subprocess.run([CMUX, "workspace-action", "--action", name,
                    "--workspace", target, *extra], capture_output=True)

def close(target):
    return close_workspace(target)

def close_old():
    # Pinned workspaces refuse to close — unpin first.
    if pinned:
        action("unpin", old_wid)
    if close(old_wid):
        return True, "closed"
    if pinned:
        action("pin", old_wid)
    return False, "close failed"

def discard_replacement(ref):
    if not ref:
        return
    action("unpin", ref)
    resolved = workspace_uuid(ref)
    if resolved:
        mark_skip(resolved)
    close(resolved or ref)

def abort_relaunch(reason, *replacements):
    message = f"Preserving {label}: {reason}."
    if MODE == "--exec-bulk":
        for replacement in replacements:
            discard_replacement(replacement)
        print("echo " + shlex.quote(message) + "; exit")
        sys.exit(0)
    # Keep single-workspace Relaunch's visible failure shell. For a multi-tab
    # attempt, discard the separate rebuild but retain this launcher (`wid`).
    for replacement in replacements:
        if replacement != wid:
            discard_replacement(replacement)
    print("echo " + shlex.quote(message) + '; exec "${SHELL:-bash}"')
    sys.exit(0)

label = title or old_wid
if len(tabs) == 1:
    # Carry the curated name + pin over to this (replacement) workspace.
    if title and wid:
        action("rename", wid, "--title", title)
    if pinned and wid:
        action("pin", wid)
    print(f"echo Relaunching: {shlex.quote(label)}")
    closed, reason = close_old()
    if not closed:
        abort_relaunch(reason, wid)
    b = resume_binding(tabs[0])
    cmd = b.get("command") or ""
    if cmd:
        print('exec "${SHELL:-bash}" -c ' + shlex.quote(cmd))
    elif cwd:
        print(f'cd {shlex.quote(cwd)} 2>/dev/null; exec "${{SHELL:-bash}}"')
    else:
        print('exec "${SHELL:-bash}"')
else:
    layout = {"pane": {"surfaces": [
        {"type": "terminal", "command": tab_cmd(s)} for s in tabs
    ]}}
    focus = "false" if MODE == "--exec-bulk" else "true"
    args = [CMUX, "new-workspace", "--focus", focus, "--layout", json.dumps(layout)]
    if title:
        args += ["--name", title]
    if cwd:
        args += ["--cwd", cwd]
    env = dict(os.environ, CMUX_QUIET="1")
    out = subprocess.run(args, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        reason = f"relaunch failed: {out.stderr.strip()}"
        if MODE == "--exec-bulk":
            abort_relaunch(reason, wid)
        print("echo " + shlex.quote(reason) + '; exec "${SHELL:-bash}"')
    else:
        # `new-workspace` prints "OK workspace:<n>" — re-pin the rebuild.
        ref = (out.stdout.strip().split() or ["", ""])[-1]
        if pinned and ref.startswith("workspace:"):
            action("pin", ref)
        closed, reason = close_old()
        if not closed:
            abort_relaunch(reason, ref, wid)
        if wid:
            mark_skip(wid)
            close(wid)
        print('exec "${SHELL:-bash}"')  # only reached if self-close failed
PY
)

# Windows has no zsh: fall back to the login shell, else bash.
if [ -n "${ACTION:-}" ]; then eval "$ACTION"; else exec "${SHELL:-bash}"; fi
