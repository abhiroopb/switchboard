"""mux.py: backend detection for Python PM OS scripts (mirror of lib/mux.sh).

    from lib.mux import cmux_bin, backend
    CMUX = cmux_bin()          # cmux binary on macOS, the cmux-shim on wmux

`cmux_bin()` keeps the historical resolution order used by dispatch.py
(CMUX_BIN, then `cmux` on PATH, then the cmux.app bundle) so the macOS path
is unchanged; it adds the shim only when the backend resolves to wmux.

On native Windows `cmux_bin()` names the extensionless bash wrapper
`mux/cmux`, which CreateProcess cannot start (WinError 193). Pass every argv
whose first item may be that wrapper through `runnable()` before
subprocess.run: it is a no-op everywhere except Windows.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

CMUX_APP_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def shim_path() -> Path:
    return Path(__file__).resolve().parent.parent / "mux" / "cmux"


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def backend() -> str:
    forced = os.environ.get("PM_OS_MUX", "").strip().lower()
    if forced and forced != "auto":
        return forced
    if os.environ.get("WMUX") == "1" or os.environ.get("WMUX_PIPE") or os.environ.get("WMUX_CLI"):
        return "wmux"
    system = platform.system()
    if system == "Darwin":
        if os.environ.get("CMUX_BIN") or shutil.which("cmux") or Path(CMUX_APP_BIN).exists():
            return "cmux"
        return "none"
    if system == "Windows":
        return "wmux"
    if system == "Linux" and _is_wsl() and (os.environ.get("WMUX_BIN") or shutil.which("wmux") or shutil.which("wmux.cmd")):
        return "wmux"
    return "none"


SANDBOX_CMUX = "/nonexistent/pm-os-test-sandbox/cmux"


def sandboxed() -> bool:
    """PM_OS_TEST_SANDBOX=1: a test or repair run that must never reach a live
    cmux or wmux tab."""
    return os.environ.get("PM_OS_TEST_SANDBOX") == "1"


def cmux_bin() -> str:
    explicit = os.environ.get("CMUX_BIN")
    if sandboxed() and not explicit:
        return SANDBOX_CMUX
    if explicit:
        return explicit
    if backend() == "wmux":
        return str(shim_path())
    return shutil.which("cmux") or CMUX_APP_BIN


def _is_windows() -> bool:
    return os.name == "nt"


def runnable(cmd: list[str]) -> list[str]:
    """Make ``cmd`` startable by CreateProcess on native Windows.

    The cmux shim wrapper becomes ``[python, cmux-shim.py, ...]`` (no bash hop,
    so prompt text with quotes or newlines passes through intact). Any other
    extensionless shebang script runs through its interpreter. Elsewhere, and
    for .exe/.cmd/.bat programs, ``cmd`` comes back unchanged.
    """
    if not _is_windows() or not cmd:
        return list(cmd)
    head = Path(cmd[0])
    if head.suffix.lower() in (".exe", ".cmd", ".bat", ".com") or not head.is_file():
        return list(cmd)
    shim = head.with_name("cmux-shim.py")
    if head.name == "cmux" and shim.is_file():
        return [sys.executable, str(shim), *cmd[1:]]
    try:
        with head.open("rb") as handle:
            first = handle.readline(200).decode("utf-8", "replace").strip()
    except OSError:
        return list(cmd)
    if not first.startswith("#!"):
        return list(cmd)
    if "python" in first:
        return [sys.executable, *cmd]
    return [_git_bash(), *cmd]


def _git_bash() -> str:
    """Git for Windows bash. Never System32's bash.exe: that is the WSL launcher, which cannot run a C:\\ path."""
    candidates = [os.environ.get("PM_OS_BASH"), shutil.which("bash")]
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [str(Path(base) / "Git" / "bin" / "bash.exe"), str(Path(base) / "Programs" / "Git" / "bin" / "bash.exe")]
    for candidate in candidates:
        if candidate and "system32" not in candidate.lower() and Path(candidate).is_file():
            return candidate
    return "bash"


def session_store_path() -> Path:
    """Where the cmux-shaped session store lives for this backend."""
    override = os.environ.get("CC_CMUX_SESSION_JSON") or os.environ.get("PM_OS_CMUX_SESSION_JSON")
    if override:
        return Path(override).expanduser()
    if backend() == "wmux":
        return Path(os.environ.get("PM_OS_STATE_HOME") or Path.home() / ".pm-os") / "wmux-session-store.json"
    return Path.home() / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json"


# -- wmux CLI discovery (Windows install report v3, bug 6) ---------------------
# wmux puts its CLI in <install>\resources\cli-bin and adds that folder to PATH
# only inside its own panes, so a Task Scheduler job, a plain Git Bash, or
# install.ps1 never saw it ("wmux not on PATH" while wmux 2.49 ran). The CLI is
# Node code (resources\cli\wmux.js); its .cmd shim needs `node` on PATH and
# cmd.exe mangles multi-line arguments, so the preferred argv is
# [node, wmux.js], else wmux's own Electron binary run as Node.

def wmux_install_dirs() -> list[Path]:
    """Folders a wmux install can live in, most specific first."""
    out: list[Path] = []
    override = os.environ.get("WMUX_HOME")
    if override:
        out.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "wmux")
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    scoop = Path(os.environ.get("SCOOP") or home / "scoop")
    out.append(scoop / "apps" / "wmux" / "current")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            out.append(Path(base) / "wmux")
    return out


def wmux_cli() -> tuple[list[str], dict[str, str]]:
    """(argv prefix, extra env) that runs the wmux CLI, or ([], {}) when none is
    found. Order: WMUX_BIN; WMUX_NODE + WMUX_CLI (set in every wmux pane); a known
    install folder through node or wmux.exe; `wmux`/`wmux.cmd` on PATH."""
    override = os.environ.get("WMUX_BIN")
    # A .cmd or .bat WMUX_BIN goes through cmd.exe, which rewrites quotes and %VAR%
    # in message text, so the node route wins over it when one exists.
    batch = bool(override) and override.lower().endswith((".cmd", ".bat"))
    if override and not batch:
        if override.lower().endswith(".py") and Path(override).is_file():
            return [sys.executable, override], {}
        return (override.split() if " " in override and not Path(override).exists() else [override]), {}
    node, cli = os.environ.get("WMUX_NODE"), os.environ.get("WMUX_CLI")
    if node and cli and Path(cli).is_file():
        return [node, cli], ({"ELECTRON_RUN_AS_NODE": "1"} if os.environ.get("WMUX_NODE_ELECTRON") == "1" else {})
    node_on_path = shutil.which("node")
    for root in wmux_install_dirs():
        script = root / "resources" / "cli" / "wmux.js"
        if not script.is_file():
            continue
        if node_on_path:
            return [node_on_path, str(script)], {}
        electron = root / "wmux.exe"
        if electron.is_file():
            return [str(electron), str(script)], {"ELECTRON_RUN_AS_NODE": "1"}
    if batch:
        return [override], {}
    for name in ("wmux", "wmux.cmd", "wmux.exe"):
        found = shutil.which(name)
        if found:
            return [found], {}
    for root in wmux_install_dirs():
        for name in ("wmux.cmd", "wmux"):
            shim = root / "resources" / "cli-bin" / name
            if shim.is_file():
                return [str(shim)], {}
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for name in ("wmux.cmd", "wmux.exe", "wmux"):
        shim = Path(os.environ.get("SCOOP") or home / "scoop") / "shims" / name
        if shim.is_file():
            return [str(shim)], {}
    return [], {}


def wmux_running(timeout: float = 5.0) -> bool:
    """True when `wmux ping` answers pong (the only verb that needs no pipe token)."""
    argv, extra = wmux_cli()
    if not argv:
        return False
    import subprocess
    try:
        result = subprocess.run([*argv, "ping"], capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env={**os.environ, **extra})
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "pong" in (result.stdout or "").lower()


def current_workspace_id(timeout: float = 10.0) -> str:
    """The calling pane's workspace id. cmux exports CMUX_WORKSPACE_ID; wmux exports
    only WMUX_SURFACE_ID, so on wmux ask the shim (`wmux current-workspace`)."""
    explicit = os.environ.get("CMUX_WORKSPACE_ID", "").strip()
    if explicit:
        return explicit
    if backend() != "wmux" or not (os.environ.get("WMUX_SURFACE_ID") or os.environ.get("CMUX_SURFACE_ID")):
        return ""
    import subprocess
    try:
        result = subprocess.run(runnable([cmux_bin(), "workspace-id-of-surface"]), capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def active() -> str:
    """Which multiplexer this process runs under right now: "cmux", "wmux", or
    "none". Pane env vars win (cmux exports CMUX_SURFACE_ID or CMUX_WORKSPACE_ID;
    wmux exports WMUX=1 and WMUX_SURFACE_ID). With neither, a live probe decides:
    `wmux ping` on wmux hosts, the cmux binary on macOS."""
    if os.environ.get("WMUX") == "1" or os.environ.get("WMUX_SURFACE_ID"):
        return "wmux"
    if os.environ.get("CMUX_SURFACE_ID") and backend() == "cmux":
        return "cmux"
    kind = backend()
    if kind == "wmux":
        return "wmux" if wmux_running() else "none"
    return kind


# What each PM OS caller needs from the multiplexer, and how wmux 2.49 serves it.
# "full" matches cmux; "typed" goes through the composer instead of an agent
# channel; "sidecar" is kept by the shim because wmux has no store for it.
CAPABILITIES: dict[str, dict[str, str]] = {
    "agent message": {"cmux": "agent hooks queue", "wmux": "typed",
                      "gap": "wmux has no agent message channel. The shim types into the Claude composer and presses Enter. "
                             "It refuses a blocked pane, a composer with a draft, an unreadable screen, and a non-Claude pane."},
    "read screen": {"cmux": "full", "wmux": "full", "gap": ""},
    "send key": {"cmux": "full", "wmux": "full", "gap": ""},
    "pending prompt": {"cmux": "screen", "wmux": "screen + agent-state",
                       "gap": "wmux reports blocked panes only through its own Claude hooks (wmux-hook.js)."},
    "current workspace": {"cmux": "CMUX_WORKSPACE_ID", "wmux": "current-workspace",
                          "gap": "wmux exports no workspace id. The shim asks wmux for the caller's workspace."},
    "set status": {"cmux": "full", "wmux": "idle|running|interrupted + text",
                   "gap": "wmux has no named status pills. The shim keeps them in a sidecar file."},
    "heartbeat": {"cmux": "hooks enqueue", "wmux": "report-agent or wmux-hook.js",
                  "gap": "When wmux's own Claude hooks are installed, they report run state and PM OS adds nothing."},
    "pin, color, order": {"cmux": "full", "wmux": "sidecar", "gap": "wmux applies them only after seed-session.ps1 -Restart."},
}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps({"backend": backend(), "active": active(), "cmux_bin": cmux_bin(),
                       "wmux_cli": wmux_cli()[0], "capabilities": CAPABILITIES}, indent=2))
