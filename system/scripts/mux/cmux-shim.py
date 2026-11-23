#!/usr/bin/env python3
"""cmux-shim: a cmux-CLI-compatible front end that drives wmux on Windows.

switchboard scripts (cmux-helpers.sh, dispatch.py, cmux-inject-guard.sh, the Amp
plugins) speak the cmux 0.65 CLI: `list-workspaces`, `new-workspace --name
--description --cwd --command --focus false`, `tree`, `read-screen`, `send`,
`send-key`, `set-status`, `workspace-action`, `rpc workspace.list` and so on.
wmux (https://wmux.org, amirlehmam/wmux 2.49) exposes the same ideas through a
different CLI and JSON shapes, and it lacks a few cmux concepts (workspace
refs, descriptions, pin/reorder RPC, status pills, custom sidebars).

This shim accepts the cmux verbs and flags, translates each one to the wmux
CLI, keeps the cmux-only state (refs, descriptions, pins, colors, status
entries, bound Amp threads) in a sidecar JSON file, and prints cmux-shaped
output. Point CMUX_BIN / CMUX / CMUX_AMP_CMUX_BIN at `system/scripts/mux/cmux`
(or `system/windows/cmux.cmd`) and the macOS scripts run unchanged.

Environment
  WMUX_BIN                 wmux CLI to drive (default: `wmux` on PATH, then
                           `$WMUX_NODE $WMUX_CLI`, the pane env wmux sets)
  SWITCHBOARD_WMUX_SIDECAR       sidecar path (default ~/.switchboard/wmux-sidecar.json)
  SWITCHBOARD_CMUX_SESSION_JSON  cmux-shaped session store the shim renders after
                           every state change (default ~/.switchboard/wmux-session-store.json);
                           export it as CC_CMUX_SESSION_JSON for the readers
  SWITCHBOARD_WMUX_REPORT_AGENT  "1" (default) mirrors `set-status amp …` into
                           `wmux report-agent --blocked/--unblocked`; "0" disables
  SWITCHBOARD_WMUX_SHELL         shell spec for every workspace the shim creates
                           (default `bash.exe`: wmux resolves it to Git Bash with
                           `--login -i`; wmux's own default is PowerShell).
                           "" or "default" keeps wmux's default shell.
  CMUX_WORKSPACE_ID        default workspace (wmux workspace id, as wsl-env.sh sets it)
  CMUX_SURFACE_ID          default surface (wmux surface id)
  CMUX_SHIM_TRACE          "1" prints every wmux call to stderr

Only cmux verbs switchboard uses are implemented; an unknown verb exits 2 with the
cmux-style error so callers fall through exactly as they do on an old cmux.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl as _flock_mod
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import flock_compat as _flock_mod
_flock = _flock_mod.flock

SHIM_VERSION = "0.4.0"
CMUX_COMPAT_VERSION = "0.65.2"
WINDOW_REF = "window:1"
WINDOW_ID = "window-1"

BOOL_FLAGS = {"--json", "--force", "--flat", "--processes", "--mask", "--quiet", "--events", "--all"}
# wmux 2.49.0 has no CLI or IPC for pin, color, or order. These verbs land in the
# sidecar at once and reach wmux's own sidebar only when seed-session.ps1 -Restart
# rewrites session.json (docs/install-decisions.md section 5).
SEED_ONLY_ACTIONS = {"pin", "unpin", "set-color", "clear-color", "move-top", "move-up", "move-down"}
SEED_HINT = ("cmux-shim: recorded in the sidecar; run system\\windows\\seed-session.ps1 -Restart "
             "to apply pins, colors and order in the wmux sidebar")
STATE_TEXT = {"running": "Running", "interrupted": "Needs input", "idle": "Idle"}

# ------------------------------------------------------------------- lane model
# Mirror of system/cmux-sidebars/switchboard.swift (bucketOf, isP0, isBackground and
# the lane list), so the switchboard board on Windows shows the same lanes, order,
# labels and tints as the cmux sidebar on macOS. Keep the two in step.
LANES: list[dict[str, str]] = [
    {"key": "pinned", "label": "Pinned", "icon": "pin", "tint": "#9AA0A6"},
    {"key": "attention", "label": "Needs attention", "icon": "alert", "tint": "#F5A623"},
    {"key": "active", "label": "Active", "icon": "bolt", "tint": "#37C871"},
    {"key": "workers", "label": "Workers", "icon": "bolt", "tint": "#2BB673"},
    {"key": "toreview", "label": "To review", "icon": "eye", "tint": "#BB9AF7"},
    {"key": "p0", "label": "P0 to-dos", "icon": "flame", "tint": "#FF5C5C"},
    {"key": "waiting", "label": "Waiting", "icon": "hourglass", "tint": "#4C8DFF"},
    {"key": "paused", "label": "Paused", "icon": "pause", "tint": "#9AA0A6"},
    {"key": "idle", "label": "Idle", "icon": "moon", "tint": "#565F89"},
    {"key": "background", "label": "Background", "icon": "gear", "tint": "#565F89"},
]
LANE_BY_KEY = {lane["key"]: lane for lane in LANES}
LANE_ALIASES = {"review": "toreview", "attn": "attention"}
# switchboard lane -> the cmux `workspace status` vocabulary command-center-digest.py reads.
CMUX_LANE = {"attention": "needs-attention", "active": "working", "workers": "working", "toreview": "review",
             "pinned": "todo", "p0": "todo", "waiting": "todo", "paused": "todo", "idle": "todo", "background": "todo"}
# Signal colors the pipeline publishers paint (review-lane plugin, cmux-status).
SIGNAL_LANE = {"#ffb3ac": "attention", "#6a2f3a": "attention", "#bb9af7": "toreview", "#fff0a8": "active",
               "#c2eaff": "waiting", "#d7dee7": "paused"}
UNREAD_BADGE = "#E0AF68"
SELECTED_TINT = "#4C8DFF"
GLYPHS = {"✳", "✻", "✽", "✢", "✶", "✷", "✸", "✹", "✺", "·", "•", "∗", "*", "◆"}
BACKGROUND_NAMES = {"Router 1", "Router 2", "Router 3", "Router 4", "Router 5"}

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def _braille_lead(title: str) -> bool:
    first = title.split(" ")[0] if title else ""
    return len(first) == 1 and 0x2800 <= ord(first) <= 0x28FF


def clean_title(title: str) -> str:
    """Strip the agent glyph prefix and Amp's ' - amp - <cwd>' suffix."""
    parts = title.split(" ")
    if parts and (parts[0] in GLYPHS or _braille_lead(title)):
        rest = " ".join(parts[1:])
        title = rest or title
    if " - amp - " in title:
        kept = " ".join(title.split(" ")[:-4])
        title = kept or title
    return title


def title_icon(title: str) -> str:
    first = title.split(" ")[0] if title else ""
    return first if len(first) == 1 and not first.isascii() else ""


def title_text(title: str) -> str:
    rest = " ".join(title.split(" ")[1:]) if title_icon(title) else title
    rest = rest or title
    if rest.startswith("P0 · "):
        tail = " ".join(rest.split(" ")[2:])
        rest = tail or rest
    return rest


def is_background(title: str) -> bool:
    cleaned = clean_title(title)
    return ("Scheduled Jobs" in cleaned or "Meetings Registry" in cleaned or title_text(cleaned) == "Meetings"
            or title_text(cleaned) in BACKGROUND_NAMES)


def is_p0(title: str) -> bool:
    return "P0 ·" in clean_title(title)


def amp_state(side: dict[str, Any]) -> str:
    """running | interrupted | idle from the amp/program_status pills the Amp
    plugins publish through `cmux set-status`."""
    for key in ("amp", "program_status"):
        item = (side.get("status") or {}).get(key)
        if item and item.get("value"):
            return Shim.state_for_label(str(item["value"]))
    return "idle"


def lane_of(ws: dict[str, Any], side: dict[str, Any]) -> str:
    """Lane key for one workspace. Precedence copies switchboard.swift: pinned, then
    background by title, then live agent state, then signal color, then the
    P0 title tag, then idle. Workers (switchboard worker descriptions) sit in their
    own lane unless they need attention or are running."""
    override = side.get("lane") or "auto"
    if override != "auto":
        return LANE_ALIASES.get(override, override)
    if side.get("pinned"):
        return "pinned"
    title = ws.get("title") or ""
    if is_background(title):
        return "background"
    state = amp_state(side)
    if side.get("blocked") or state == "interrupted" or title.startswith("◆"):
        return "attention"
    if state == "running" or _braille_lead(title):
        return "active"
    color = str(side.get("color") or "").lower()
    bucket = SIGNAL_LANE.get(color, "idle")
    if bucket in ("attention", "active"):
        return bucket
    if (side.get("description") or "").startswith("switchboard worker"):
        return "workers"
    if bucket != "idle":
        return bucket
    return "p0" if is_p0(title) else "idle"


class ShimError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- args

def parse_args(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """cmux-style parsing: `--flag value` pairs, bare boolean flags, `--` ends
    flags, everything else is positional (in order)."""
    positional: list[str] = []
    flags: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positional.extend(argv[i + 1:])
            break
        if arg.startswith("--") and len(arg) > 2:
            if "=" in arg:
                key, value = arg.split("=", 1)
                flags[key] = value
            elif arg in BOOL_FLAGS or i + 1 >= len(argv) or (argv[i + 1].startswith("--") and arg != "--command"):
                flags[arg] = True
            else:
                flags[arg] = argv[i + 1]
                i += 1
        else:
            positional.append(arg)
        i += 1
    return positional, flags


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------------ backend

class Wmux:
    """Thin runner around the wmux CLI."""

    def __init__(self) -> None:
        self.argv = self._resolve()
        self.trace = os.environ.get("CMUX_SHIM_TRACE") == "1"

    def _resolve(self) -> list[str]:
        """lib/mux.py wmux_cli(): WMUX_BIN, the pane's WMUX_NODE + WMUX_CLI, a known
        install folder (%LOCALAPPDATA%\\Programs\\wmux, scoop) through node or
        wmux.exe, then `wmux` on PATH. Outside a wmux pane the CLI is not on PATH."""
        self.extra_env: dict[str, str] = {}
        if os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
            # Test sandbox: a wmux pane exports the real
            # WMUX_BIN, so it proves nothing. Only a test that names its fake in
            # SWITCHBOARD_TEST_WMUX_BIN (and strips the pane's WMUX_* variables) gets the
            # normal lookup; anything else fails every call closed.
            fake = os.environ.get("SWITCHBOARD_TEST_WMUX_BIN")
            if not fake:
                return ["/nonexistent/switchboard-test-sandbox/wmux"]
            # A test may still point WMUX_BIN at another fake for one call; the
            # live install never counts, and nothing falls through to the pane's
            # WMUX_NODE + WMUX_CLI or the install folder.
            override = os.environ.get("WMUX_BIN") or fake
            if re.search(r"[\\/]programs[\\/]wmux[\\/]", override.lower()):
                return ["/nonexistent/switchboard-test-sandbox/wmux"]
            return [sys.executable, override] if override.endswith(".py") else [override]
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from lib.mux import wmux_cli
            argv, self.extra_env = wmux_cli()
        except Exception:  # noqa: BLE001  (a stripped copy without lib/ keeps the old lookup)
            argv = []
        return argv or ["wmux"]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(getattr(self, "extra_env", {}) or {})
        if os.environ.get("WMUX_NODE_ELECTRON") == "1":
            env["ELECTRON_RUN_AS_NODE"] = "1"
        return env

    def _direct_request(self, args: list[str]) -> tuple[str, dict[str, Any]] | None:
        """The pipe request for a read or status verb, or None when the CLI must run it.

        Windows can leak every exited process until reboot, and
        the CLI is a node process per call. These verbs map one to one onto a pipe method
        in wmux's cli/wmux.js. Anything else, any test, and any remote target uses the CLI.
        """
        if os.name != "nt" or os.environ.get("SWITCHBOARD_WMUX_DIRECT") == "0" or os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
            return None
        if any(os.environ.get(key) for key in ("SWITCHBOARD_TEST_WMUX_BIN", "WMUX_REMOTE", "WMUX_REMOTE_TOKEN")):
            return None
        # switchboard-env.sh exports WMUX_BIN as the installed CLI. Any other value is a stand-in the caller chose.
        override = os.environ.get("WMUX_BIN", "").replace("\\", "/").lower()
        if override and "/resources/cli-bin/" not in override:
            return None
        verb = args[0] if args else ""

        def flag(name: str) -> str | None:
            return args[args.index(name) + 1] if name in args[:-1] else None

        surface = flag("--surface")
        if verb == "list-workspaces" and len(args) == 1:
            return "workspace.list", {}
        if verb == "agent-state" and surface:
            return "pane.agent_state", {"surfaceId": surface}
        if verb == "prompts" and surface and "--json" in args and "--limit" not in args:
            return "surface.list_prompts", {"surfaceId": surface}
        if verb == "set-status" and flag("--workspace") and flag("--state") in ("idle", "running", "interrupted"):
            params: dict[str, Any] = {"workspaceId": flag("--workspace"), "state": flag("--state")}
            if flag("--text"):
                params["text"] = flag("--text")
            return "workspace.set_status", params
        if verb == "log" and len(args) >= 3:
            return "sidebar.log", {"level": args[1], "message": " ".join(args[2:])}
        return None

    def _direct(self, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
        """Answer a mapped verb over wmux's named pipe in-process; None falls back to the CLI,
        which then reports any error exactly as it always has."""
        request = self._direct_request(args)
        if request is None:
            return None
        method, params = request
        suffix = f"-{os.environ['WMUX_INSTANCE'].strip()}" if os.environ.get("WMUX_INSTANCE", "").strip() else ""
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        try:
            token = (Path(base) / f"wmux{suffix}" / "pipe-token").read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if os.environ.get("WMUX_SURFACE_ID"):
            params = {**params, "caller": os.environ["WMUX_SURFACE_ID"]}
        line = json.dumps({"method": method, "params": params, "id": 1, "token": token}) + "\n"
        pipe = os.environ.get("WMUX_PIPE") or "\\\\.\\pipe\\wmux" + suffix
        reply: list[bytes] = []
        written: list[bool] = []

        def exchange() -> None:
            try:
                with open(pipe, "r+b", buffering=0) as handle:
                    handle.write(line.encode("utf-8"))
                    written.append(True)
                    data = b""
                    while b"\n" not in data:
                        chunk = handle.read(1 << 16)
                        if not chunk:
                            break
                        data += chunk
                    reply.append(data)
            except OSError:
                pass  # wmux closed or the pipe busy: the CLI fallback reports it

        # A daemon thread gives a blocking pipe read the same timeout the CLI call had.
        worker = threading.Thread(target=exchange, daemon=True)
        worker.start()
        worker.join(min(timeout, 10))
        if not reply or b"\n" not in reply[0]:
            # A status or log line already sent must not go out a second time through the CLI.
            if written and args[0] in ("log", "set-status"):
                return subprocess.CompletedProcess(args, 124, "", f"cmux-shim: wmux {args[0]} sent, no reply")
            return None
        try:
            response = json.loads(reply[0].decode("utf-8"))
        except ValueError:
            return None
        if not isinstance(response, dict) or response.get("error") or "result" not in response:
            return None
        if self.trace:
            print(f"[cmux-shim] direct {method}", file=sys.stderr)
        return subprocess.CompletedProcess(args, 0, json.dumps(response["result"], indent=2) + "\n", "")

    def run(self, args: list[str], timeout: float = 20, stdin: str | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        if stdin is None and not env:
            try:
                direct = self._direct(args, timeout)
            except (OSError, RuntimeError):
                direct = None
            if direct is not None:
                return direct
        cmd = [*self.argv, *args]
        if self.trace:
            print("[cmux-shim] " + " ".join(cmd), file=sys.stderr)
        run_env = self.env()
        if env:
            run_env.update(env)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=run_env, input=stdin,
                                  stdin=None if stdin is not None else subprocess.DEVNULL)
        except FileNotFoundError:
            raise ShimError(f"cmux-shim: wmux CLI not found ({cmd[0]}); set WMUX_BIN", 127)
        except subprocess.TimeoutExpired:
            raise ShimError(f"cmux-shim: wmux {args[0]} timed out after {timeout}s", 124)

    def json(self, args: list[str], timeout: float = 20) -> Any:
        result = self.run(args, timeout=timeout)
        if result.returncode:
            raise ShimError((result.stderr or result.stdout or f"wmux {args[0]} failed").strip(), result.returncode or 1)
        text = (result.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            # wmux prints a bare string for a few verbs (ping, rename results).
            return {"raw": text}


# ------------------------------------------------------------------------ sidecar

def default_state_dir() -> Path:
    return Path(os.environ.get("SWITCHBOARD_STATE_HOME") or Path.home() / ".switchboard")


INJECTED_LOG_MAX_BYTES = 1_000_000


# What can end a typed line early: a real CR or LF, and the escapes `cmux send` turns into one
# (unescape below). Each piece Claude may receive as its own prompt gets its own hash.
_SUBMIT_SPLIT = re.compile(r"\r\n|\r|\n|\\r\\n|\\r|\\n")


def injected_digests(text: str) -> list[str]:
    """Hashes for the whole text and for every piece a CR, LF or `\\r`/`\\n` escape could submit on its own.
    The hash ignores whitespace and ` ⏎ ` (gmail_send_guard.injected_key, worker-closed.py injected_key)."""
    def key(part: str) -> str:
        return hashlib.sha256(re.sub(r"[\s⏎]+", "", part).encode("utf-8")).hexdigest()
    out = [key(text)]
    for piece in _SUBMIT_SPLIT.split(text):
        if re.sub(r"[\s⏎]+", "", piece) and key(piece) not in out:
            out.append(key(piece))
    return out


def note_injected(sid: str, text: str) -> None:
    """Record a hash of every line typed into a pane. A typed line looks like the owner's own in
    Claude Code's transcript, so gmail_send_guard.py reads this to keep a worker's "send it" from
    approving an email. Hashes only, never the text. A failure never blocks delivery."""
    if os.environ.get("SWITCHBOARD_TEST_SANDBOX") == "1":
        return
    path = default_state_dir() / "injected-prompts.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for digest in injected_digests(text):
                handle.write(json.dumps({"at": now, "surface": sid, "sha": digest}) + "\n")
        if path.stat().st_size > INJECTED_LOG_MAX_BYTES:
            tail = path.read_bytes()[-INJECTED_LOG_MAX_BYTES // 2:]
            path.write_bytes(tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


DEFAULT_WORKSPACE_SHELL = "bash.exe"


def workspace_shell() -> str:
    """Shell spec passed to `wmux new-workspace --shell`. wmux's own default on
    Windows is pwsh, then powershell, then cmd; switchboard scripts, seeds, and the
    `.bashrc` that sources switchboard-env.sh all assume Git Bash. wmux resolves the
    bare alias `bash.exe` to Git for Windows' bash at its install root with
    `--login -i` (never the WSL launcher in System32)."""
    raw = os.environ.get("SWITCHBOARD_WMUX_SHELL")
    if raw is None:
        return DEFAULT_WORKSPACE_SHELL
    raw = raw.strip()
    return "" if raw.lower() in {"", "default", "wmux"} else raw


class Sidecar:
    """cmux-only state keyed by wmux ids. Written atomically; small enough to
    rewrite on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(os.environ.get("SWITCHBOARD_WMUX_SIDECAR") or path or default_state_dir() / "wmux-sidecar.json")
        self.data: dict[str, Any] = {"version": 1, "next_ref": 1, "next_surface": 1, "next_pane": 1,
                                     "workspaces": {}, "surfaces": {}, "panes": {}, "closed": []}
        self.dirty = False
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError):
                pass
        self.base = copy.deepcopy(self.data)

    # Every shim call is its own process, and several run at once (a launcher, the sweep, a
    # hook). Loading the file at the start and writing it all back at the end lets the last
    # writer erase what the others wrote in between, so a save re-reads the file under a lock
    # and applies only the fields this process changed.
    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as handle:
            _flock(handle, _flock_mod.LOCK_EX)
            try:
                yield
            finally:
                _flock(handle, _flock_mod.LOCK_UN)

    def _read_disk(self) -> dict[str, Any] | None:
        """The file's contents, {} when it does not exist, or None when it cannot be read.
        An unreadable file is not an empty one: merging against {} would close every tab."""
        for attempt in range(4):
            try:
                loaded = json.loads(self.path.read_text())
                return loaded if isinstance(loaded, dict) else None
            except FileNotFoundError:
                return {}
            except (OSError, ValueError):
                time.sleep(0.05 * (attempt + 1))
        return None

    def _merged(self, disk: dict[str, Any]) -> dict[str, Any]:
        """`disk` with this process's changes (self.data against self.base) applied."""
        out = {**self.data, **disk}
        for key in ("next_ref", "next_surface", "next_pane"):
            out[key] = max(int(self.data.get(key) or 1), int(disk.get(key) or 1))
        for key in ("surfaces", "panes"):
            out[key] = {**(self.data.get(key) or {}), **(disk.get(key) or {})}
        closed: dict[str, Any] = {}
        for entry in [*(disk.get("closed") or []), *(self.data.get("closed") or [])]:
            closed.setdefault(str(entry.get("id")), entry)
        out["closed"] = list(closed.values())[-50:]
        for key, value in self.data.items():
            if key not in ("workspaces", "closed", "surfaces", "panes", "next_ref", "next_surface", "next_pane") \
                    and value != self.base.get(key):
                out[key] = value
        base_ws, mine = self.base.get("workspaces") or {}, self.data.get("workspaces") or {}
        if not disk:   # the file is gone, so nothing says another process closed a tab
            out["workspaces"] = mine
            return out
        live = dict(disk.get("workspaces") or {})
        for wid in base_ws:
            if wid not in mine:
                live.pop(wid, None)   # this process saw the tab close
        for wid, entry in mine.items():
            before = base_ws.get(wid)
            if wid in live:
                changes = {k: v for k, v in entry.items() if before is None or k not in before or before[k] != v}
                changes.pop("ref", None)   # the first process to see a tab numbers it
                live[wid] = {**live[wid], **changes}
            elif before is None:
                live[wid] = entry      # new here, and allocate_ref already numbered it
            # else: another process saw the tab close after this one loaded; it stays closed
        out["workspaces"] = live
        return out

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sidecar-", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        _os_replace(tmp, self.path)

    def save(self) -> None:
        if not self.dirty:
            return
        with self._locked():
            disk = self._read_disk()
            if disk is None:
                print(f"[cmux-shim] {self.path} is unreadable; this change was not saved", file=sys.stderr)
                return   # stays dirty; nothing on disk is overwritten
            merged = self._merged(disk)
            self._write(merged)
        self.data = merged
        self.base = copy.deepcopy(merged)
        self.dirty = False

    # -- workspaces
    def workspace(self, wid: str) -> dict[str, Any]:
        entry = self.data["workspaces"].get(wid)
        if entry is None:
            entry = self.allocate_workspace(wid)
            self.data["workspaces"][wid] = entry
            self.dirty = True
        return entry

    def allocate_workspace(self, wid: str) -> dict[str, Any]:
        """Number a tab under the lock and on disk, so two processes seeing one new tab
        agree on its ref and two new tabs never share a number."""
        with self._locked():
            disk = self._read_disk()
            if disk is None:
                raise ShimError(f"cmux-shim: {self.path} is unreadable; not numbering a new tab", 1)
            known = (disk.get("workspaces") or {}).get(wid)
            if known is not None:
                self.base.setdefault("workspaces", {})[wid] = copy.deepcopy(known)
                return known
            number = max(int(disk.get("next_ref") or 1), int(self.data.get("next_ref") or 1))
            entry = {"ref": f"workspace:{number}", "description": "", "pinned": False,
                     "color": None, "status": {}, "thread": None, "blocked": False,
                     "created_at": time.time(), "custom_title": None,
                     "unread": 0, "last_activity": time.time(), "order_index": None, "pushed": None}
            self.data["next_ref"] = number + 1
            disk = {**self.data, **disk, "next_ref": number + 1}
            disk["workspaces"] = {**(disk.get("workspaces") or {}), wid: entry}
            self._write(disk)
            self.base.setdefault("workspaces", {})[wid] = copy.deepcopy(entry)
            return entry

    def touch(self, wid: str) -> None:
        self.workspace(wid)["last_activity"] = time.time()
        self.dirty = True

    def sync(self, live: list[dict[str, Any]]) -> None:
        live_ids = [str(w.get("id")) for w in live]
        for raw in live:
            side = self.workspace(str(raw.get("id")))
            # Last seen title and cwd let a closed row be reopened later.
            seen = {"last_title": str(raw.get("title") or ""), "last_cwd": str(raw.get("cwd") or "")}
            if any(side.get(k) != v for k, v in seen.items()):
                side.update(seen)
                self.dirty = True
            # The selected workspace is being read, so its unread count is spent.
            if raw.get("isActive") and side.get("unread"):
                side["unread"] = 0
                self.dirty = True
        # An empty answer is a failed read while CoS is open, never "every tab closed":
        # treating it as one would renumber every tab on the next list.
        for wid in (list(self.data["workspaces"]) if live_ids else []):
            if wid not in live_ids:
                gone = self.data["workspaces"].pop(wid)
                self.data["closed"] = (self.data.get("closed") or [])[-49:] + [
                    {"id": wid, "ref": gone.get("ref"), "title": gone.get("last_title") or gone.get("custom_title"),
                     "description": gone.get("description"), "cwd": gone.get("last_cwd"), "thread": gone.get("thread"),
                     "pinned": bool(gone.get("pinned")), "color": gone.get("color"), "closed_at": time.time()}]
                self.dirty = True

    def ref_to_id(self, ref: str) -> str | None:
        return next((wid for wid, w in self.data["workspaces"].items() if w.get("ref") == ref), None)

    # -- surfaces / panes
    def _number(self, table: str, counter: str, key: str) -> int:
        """A surface's or pane's number, allocated under the lock like a workspace ref:
        two processes sharing surface:N would send keystrokes to the wrong pane."""
        if key not in self.data[table]:
            with self._locked():
                disk = self._read_disk()
                if disk is None:
                    raise ShimError(f"cmux-shim: {self.path} is unreadable; not numbering a new {table[:-1]}", 1)
                number = (disk.get(table) or {}).get(key)
                if number is None:
                    number = max(int(disk.get(counter) or 1), int(self.data.get(counter) or 1))
                    disk = {**self.data, **disk, counter: number + 1}
                    disk[table] = {**(disk.get(table) or {}), key: number}
                    self._write(disk)
                    self.data[counter] = number + 1
            self.data[table][key] = number
        return self.data[table][key]

    def surface_ref(self, sid: str) -> str:
        return f"surface:{self._number('surfaces', 'next_surface', sid)}"

    def surface_id(self, ref: str) -> str | None:
        return next((sid for sid, n in self.data["surfaces"].items() if f"surface:{n}" == ref), None)

    def pane_ref(self, pid: str) -> str:
        return f"pane:{self._number('panes', 'next_pane', pid)}"


# --------------------------------------------------------------------------- shim

# Characters cmd.exe acts on when wmux is reached through wmux.cmd (code review, kit v5).
CMD_UNSAFE = set('"%^&|<>!')


class Shim:
    def __init__(self) -> None:
        self.wmux = Wmux()
        self.side = Sidecar()
        self._workspaces: list[dict[str, Any]] | None = None

    # -- live state ------------------------------------------------------
    def workspaces(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._workspaces is None or refresh:
            payload = self.wmux.json(["list-workspaces"])
            raw = list(payload.get("workspaces") or [])
            self.side.sync(raw)
            self._workspaces = [self.shape_workspace(w, index) for index, w in enumerate(raw)]
        return self._workspaces

    def lane_view(self) -> dict[str, Any]:
        """Lanes in cmux-sidebar order with their rows; the switchboard board renders
        this and `cmux sidebar lanes --json` prints it."""
        rows: dict[str, list[dict[str, Any]]] = {lane["key"]: [] for lane in LANES}
        unread_total = 0
        for w in self.workspaces():
            side = self.side.workspace(w["id"])
            key = lane_of(w, side)
            if key not in rows:
                key = "idle"
            cleaned = clean_title(w["title"])
            row = {**w, "lane": key, "clean_title": cleaned, "icon": title_icon(cleaned), "label": title_text(cleaned),
                   "unread": int(side.get("unread") or 0), "last_activity": float(side.get("last_activity") or 0),
                   "thread": side.get("thread"), "agent_state": amp_state(side), "blocked": bool(side.get("blocked")),
                   "order_index": side.get("order_index")}
            unread_total += row["unread"]
            rows[key].append(row)
        for key, items in rows.items():
            if key == "pinned":
                items.sort(key=lambda r: (r["order_index"] if r["order_index"] is not None else 10_000, r["index"]))
            elif key == "idle":
                items.sort(key=lambda r: -r["last_activity"])
            else:
                items.sort(key=lambda r: r["clean_title"].lower() + "|" + r["id"])
        selected = next((w for w in self.workspaces() if w["selected"]), None)
        return {"backend": "wmux", "shim": SHIM_VERSION, "rendered_at": time.time(), "unread_total": unread_total,
                "workspace_count": len(self.workspaces()), "selected": selected["id"] if selected else None,
                "directory": (selected or {}).get("current_directory") or "",
                "closed": (self.side.data.get("closed") or [])[-5:],
                "lanes": [{**lane, "count": len(rows[lane["key"]]), "workspaces": rows[lane["key"]]} for lane in LANES]}

    def push_lane(self, ws: dict[str, Any], side: dict[str, Any], text: str | None = None) -> None:
        """Mirror the lane into wmux's own sidebar row. wmux shows the row dot
        for running/interrupted and prints `--text` only while idle ("Done:
        <text>"), so the lane label rides on the idle text. Sent only when the
        rendered signal changed, so the shim stays quiet between turns."""
        state = amp_state(side)
        if (ws.get("title") or "").startswith("◆"):
            state = "interrupted"
        self.mirror_blocked(ws, side, state == "interrupted")
        if state == "idle":
            lane = lane_of(ws, side)
            label = LANE_BY_KEY.get(lane, LANE_BY_KEY["idle"])["label"]
            unread = int(side.get("unread") or 0)
            text = f"{label} · {unread} unread" if unread else label
        elif not text:
            text = STATE_TEXT[state]
        signature = f"{state}|{text}"
        if side.get("pushed") == signature:
            return
        self.wmux.run(["set-status", "--workspace", ws["id"], "--state", state, "--text", text], timeout=10)
        side["pushed"] = signature
        self.side.dirty = True

    def shape_workspace(self, raw: dict[str, Any], index: int) -> dict[str, Any]:
        wid = str(raw.get("id"))
        side = self.side.workspace(wid)
        title = str(raw.get("title") or "")
        custom = side.get("custom_title")
        return {
            "id": wid,
            "ref": side["ref"],
            "index": index,
            "title": title,
            "custom_title": custom if custom is not None else title,
            "has_custom_title": custom is not None,
            "description": side.get("description") or "",
            "pinned": bool(side.get("pinned")),
            "selected": bool(raw.get("isActive")),
            "current_directory": raw.get("cwd") or "",
            "shell": raw.get("shell") or "",
            "custom_color": side.get("color"),
            "status": side.get("status") or {},
            "window_ref": WINDOW_REF,
            "backend": "wmux",
        }

    def resolve_workspace(self, flags: dict[str, Any], required: bool = True) -> dict[str, Any] | None:
        token = flags.get("--workspace")
        if token in (None, True):
            token = os.environ.get("CMUX_WORKSPACE_ID") or ""
        token = str(token).strip()
        if not token:
            if required:
                raise ShimError("Error: --workspace is required (no CMUX_WORKSPACE_ID in the environment)", 2)
            return None
        for w in self.workspaces():
            if token in (w["ref"], w["id"]):
                return w
        if token.isdigit():
            for w in self.workspaces():
                if w["index"] == int(token):
                    return w
        if required:
            raise ShimError(f"Error: workspace not found: {token}", 1)
        return None

    def surfaces(self, workspace: dict[str, Any] | None) -> list[dict[str, Any]]:
        args = ["list-surfaces"] + (["--workspace", workspace["id"]] if workspace else [])
        payload = self.wmux.json(args)
        shaped = []
        for index, raw in enumerate(payload.get("surfaces") or []):
            sid = str(raw.get("id"))
            shaped.append({"id": sid, "ref": self.side.surface_ref(sid), "pane_id": str(raw.get("paneId") or ""),
                           "pane_ref": self.side.pane_ref(str(raw.get("paneId") or sid)),
                           "workspace_ref": workspace["ref"] if workspace else None,
                           "type": raw.get("type") or "terminal", "focused": bool(raw.get("isActive")),
                           "selected": bool(raw.get("isActive")), "index": index, "title": raw.get("title") or ""})
        return shaped

    def resolve_surface(self, flags: dict[str, Any], workspace: dict[str, Any] | None) -> str:
        token = flags.get("--surface")
        if token in (None, True):
            token = os.environ.get("CMUX_SURFACE_ID") or ""
            # The caller's own surface is only a default for its own workspace. Taken for
            # another workspace, `send --workspace X` would type into the caller's pane.
            if token and workspace and token not in {v for s in self.surfaces(workspace) for v in (s.get("id"), s.get("ref")) if v}:
                token = ""
        token = str(token).strip()
        if token.startswith("surface:"):
            mapped = self.side.surface_id(token)
            if mapped:
                return mapped
        elif token:
            return token
        for attempt in range(3):
            found = self.surfaces(workspace)
            # A markdown, browser or diff pane has no PTY, so `send` to it fails with
            # "has no PTY". Prefer a terminal whenever the tab has one.
            terminals = [s for s in found if s["type"] == "terminal"] or found
            if terminals:
                focused = next((s for s in terminals if s["focused"]), terminals[0])
                return focused["id"]
            time.sleep(0.4 * (attempt + 1))
        raise ShimError("Error: no surface in workspace", 1)

    # -- output helpers --------------------------------------------------
    @staticmethod
    def out(text: str = "") -> None:
        sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")

    def out_json(self, value: Any) -> None:
        self.out(json.dumps(value, indent=2))

    def finish(self) -> None:
        self.side.save()
        self.render_session_store()

    # -- session store ---------------------------------------------------
    def render_session_store(self) -> None:
        """Write the cmux-shaped session store so running-task-state.py,
        workspace-session-state.py and command-center-digest.py read wmux state
        through CC_CMUX_SESSION_JSON unchanged."""
        target = os.environ.get("SWITCHBOARD_CMUX_SESSION_JSON")
        path = Path(target) if target else default_state_dir() / "wmux-session-store.json"
        try:
            live = self._workspaces if self._workspaces is not None else self.workspaces()
        except ShimError:
            return
        entries = []
        for w in live:
            side = self.side.workspace(w["id"])
            status_entries = [{"key": key, "value": item.get("value"), "timestamp": item.get("timestamp")}
                              for key, item in sorted((side.get("status") or {}).items())]
            terminal: dict[str, Any] = {"id": w["id"]}
            if side.get("thread"):
                # Amp thread ids start with "T-"; a Claude Code session id is a UUID.
                kind = "amp" if str(side["thread"]).startswith("T-") else "claude"
                terminal["agent"] = {"kind": kind, "sessionId": side["thread"]}
            entries.append({"workspaceId": w["id"], "title": w["title"], "customTitle": side.get("custom_title"),
                            "description": w["description"], "pinned": w["pinned"], "ref": w["ref"],
                            "panels": [{"terminal": terminal}], "statusEntries": status_entries})
        store = {"backend": "wmux", "shim": SHIM_VERSION, "rendered_at": time.time(),
                 "windows": [{"id": WINDOW_ID, "tabManager": {"workspaces": entries}}]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".session-", suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(store, handle, indent=1)
            _os_replace(tmp, path)
        except OSError:
            pass

    # -- verbs -----------------------------------------------------------
    def cmd_identify(self, pos, flags) -> None:
        info = {}
        try:
            info = self.wmux.json(["identify"])
        except ShimError:
            pass
        caller: dict[str, Any] = {}
        ws = self.resolve_workspace({}, required=False) if os.environ.get("CMUX_WORKSPACE_ID") else None
        if ws:
            caller = {"workspace_ref": ws["ref"], "workspace_id": ws["id"], "window_ref": WINDOW_REF}
            if os.environ.get("CMUX_SURFACE_ID"):
                caller["surface_ref"] = self.side.surface_ref(os.environ["CMUX_SURFACE_ID"])
        self.out_json({"app": "cmux", "version": CMUX_COMPAT_VERSION, "compat": "cmux-shim", "shim_version": SHIM_VERSION,
                       "backend": {"app": info.get("name") or "wmux", "version": info.get("version"),
                                   "platform": info.get("platform")},
                       "socket_path": os.environ.get("WMUX_PIPE") or r"\\.\pipe\wmux", "caller": caller})

    def cmd_ping(self, pos, flags) -> None:
        self.wmux.run(["ping"], timeout=5)
        self.out("PONG")

    def list_lines(self, both: bool) -> str:
        lines = []
        for w in self.workspaces():
            marker = "*" if w["selected"] else " "
            ident = f"{w['ref']} {w['id']}" if both else w["ref"]
            tail = "  [selected]" if w["selected"] else ""
            lines.append(f"{marker} {ident}  {w['title']}{tail}")
        return "\n".join(lines)

    def cmd_list_workspaces(self, pos, flags) -> None:
        if flags.get("--json"):
            self.out_json({"window_ref": WINDOW_REF, "workspaces": self.workspaces()})
        else:
            self.out(self.list_lines(both=str(flags.get("--id-format") or "") == "both"))

    def cmd_workspace(self, pos, flags) -> None:
        sub = pos[0] if pos else "list"
        rest = pos[1:]
        if sub == "list":
            return self.cmd_list_workspaces(rest, flags)
        if sub == "create":
            return self.cmd_new_workspace(rest, flags)
        if sub == "close":
            if rest:
                flags = {**flags, "--workspace": rest[0]}
            return self.cmd_close_workspace([], flags)
        if sub == "select":
            if rest:
                flags = {**flags, "--workspace": rest[0]}
            return self.cmd_select_workspace([], flags)
        if sub == "rename":
            if rest:
                flags = {**flags, "--workspace": rest[0]}
            title = flags.get("--title") or (rest[1] if len(rest) > 1 else "")
            return self.cmd_rename_workspace([str(title)], flags)
        if sub == "status":
            ws = self.resolve_workspace(flags)
            side = self.side.workspace(ws["id"])
            override = side.get("lane") or "auto"
            if rest[:1] == ["set"] and len(rest) > 1:
                override = rest[1]
                side["lane"] = override
                self.side.dirty = True
                self.push_lane(ws, side)
            inferred = CMUX_LANE.get(lane_of(ws, {**side, "lane": "auto"}), "todo")
            effective = inferred if override == "auto" else CMUX_LANE.get(LANE_ALIASES.get(override, override), override)
            # Line 1 is cmux's own vocabulary (todo|working|needs-attention|review|done)
            # because command-center-digest.py reads it; `lane:` is the switchboard lane key.
            self.out(f"{effective}\ninferred: {inferred}\noverride: {'none (auto)' if override == 'auto' else override}\nlane: {lane_of(ws, side)}")
            for key, item in sorted((side.get("status") or {}).items()):
                extras = " ".join(f"{k}={item[k]}" for k in ("icon", "color", "priority") if item.get(k) not in (None, "", 0))
                self.out(f"{key}={item.get('value', '')}{(' ' + extras) if extras else ''}")
            return
        raise ShimError(f"Error: Unknown workspace subcommand '{sub}'", 2)

    def cmd_rpc(self, pos, flags) -> None:
        method = pos[0] if pos else ""
        params: dict[str, Any] = {}
        if len(pos) > 1 and pos[1] != "-":
            try:
                params = json.loads(pos[1]) or {}
            except ValueError:
                raise ShimError("Error: rpc params must be a JSON object", 2)
        elif len(pos) > 1:
            params = json.loads(sys.stdin.read() or "{}")
        if method == "workspace.list":
            return self.out_json({"window_id": WINDOW_ID, "window_ref": WINDOW_REF, "workspaces": self.workspaces()})
        if method == "surface.list":
            ws = None
            if params.get("workspace_id") or params.get("workspace_ref"):
                ws = self.resolve_workspace({"--workspace": params.get("workspace_id") or params.get("workspace_ref")})
            return self.out_json({"surfaces": self.surfaces(ws)})
        if method == "workspace.close":
            ws = self.resolve_workspace({"--workspace": params.get("workspace_id") or params.get("workspace_ref") or params.get("id")})
            self.close(ws)
            return self.out_json({"ok": True, "closed": ws["ref"]})
        if method == "surface.close":
            sid = params.get("surface_id") or params.get("id") or ""
            if str(sid).startswith("surface:"):
                sid = self.side.surface_id(str(sid)) or sid
            self.wmux.json(["close-surface", str(sid)])
            return self.out_json({"ok": True})
        if method == "system.identify":
            return self.cmd_identify([], {})
        raise ShimError(f"Error: rpc method not supported by cmux-shim: {method}", 2)

    def cmd_tree(self, pos, flags) -> None:
        targets = [self.resolve_workspace(flags)] if (flags.get("--workspace") or os.environ.get("CMUX_WORKSPACE_ID")) else self.workspaces()
        lines = [f"window {WINDOW_REF} [current]"]
        for w in targets:
            sel = " [selected]" if w["selected"] else ""
            lines.append(f"└── workspace {w['ref']} \"{w['title']}\"{sel}")
            try:
                surfaces = self.surfaces(w)
            except ShimError:
                surfaces = []
            by_pane: dict[str, list[dict[str, Any]]] = {}
            for s in surfaces:
                by_pane.setdefault(s["pane_ref"], []).append(s)
            for pane_ref, items in by_pane.items():
                focused = " [focused]" if any(s["focused"] for s in items) else ""
                lines.append(f"    └── pane {pane_ref}{focused}")
                for s in items:
                    ssel = " [selected]" if s["selected"] else ""
                    lines.append(f"        └── surface {s['ref']} [{s['type']}] \"{s['title']}\"{ssel}")
        self.out("\n".join(lines))

    def cmd_read_screen(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags, required=False)
        sid = self.resolve_surface(flags, ws)
        args = ["read-screen", "--surface", sid]
        if flags.get("--lines"):
            args += ["--lines", str(flags["--lines"])]
        payload = self.wmux.json(args)
        text = payload.get("text") if isinstance(payload, dict) else None
        if text is None and isinstance(payload, dict) and isinstance(payload.get("lines"), list):
            text = "\n".join(str(line) for line in payload["lines"])
        # The guards read this text and cannot see the cursor, so a prompt suggestion
        # is taken off the ❯ line here: to them it would be a draft that never goes away.
        text = self.hide_suggestion(text or "", payload)
        # Claude Code draws its empty prompt as "❯" + U+00A0. The guards' [[:space:]]
        # does not match a no-break space, so an idle composer read as a draft.
        self.out((text or "").replace(" ", " "))

    def cmd_new_workspace(self, pos, flags) -> None:
        if flags.get("--layout"):
            raise ShimError("Error: --layout is not supported by cmux-shim (wmux has no cmux layout JSON)", 2)
        title = str(flags.get("--name") or flags.get("--title") or "")
        self.workspaces()  # assign refs to the workspaces that already exist before the new one
        args = ["new-workspace", "--panes", "1", "--layout", "single"]
        if title:
            args += ["--title", title]
        if flags.get("--cwd"):
            args += ["--cwd", str(flags["--cwd"])]
        shell = workspace_shell()
        if shell:
            args += ["--shell", shell]
        created = self.wmux.json(args, timeout=60)
        wid = str(created.get("workspaceId") or created.get("id") or "")
        if not wid:
            raise ShimError("Error: wmux new-workspace returned no workspaceId", 1)
        side = self.side.workspace(wid)
        if title:
            side["custom_title"] = title
        if flags.get("--description"):
            side["description"] = str(flags["--description"])
        if flags.get("--color"):
            side["color"] = str(flags["--color"])
        self.side.dirty = True
        # On disk before anything slow: typing the command takes seconds, and every other
        # shim process reads the tab's identity from this file meanwhile.
        self.side.save()
        self.workspaces(refresh=True)
        ws = next((w for w in self.workspaces() if w["id"] == wid), None) or {"id": wid, "ref": side["ref"]}
        if flags.get("--command"):
            for attempt in (1, 2, 3):
                sid = self.resolve_surface({}, ws)
                try:
                    self.wait_shell_ready(sid)
                    self.type_into(sid, str(flags["--command"]), press_enter=True)
                    break
                except ShimError as error:
                    # A pane being replaced answers "has no PTY" for a moment. A tab that is
                    # really gone fails again here and reaches the launcher's hold.
                    if "no PTY" not in str(error) or attempt == 3:
                        raise
                    time.sleep(attempt)
        if truthy(flags.get("--focus"), default=False):
            self.wmux.json(["select-workspace", wid])
        self.out(f"Created workspace {side['ref']} ({wid})")

    def close(self, ws: dict[str, Any]) -> None:
        self.wmux.json(["close-workspace", ws["id"]], timeout=30)
        self.workspaces(refresh=True)

    def cmd_close_workspace(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        self.close(ws)
        self.out(f"Closed workspace {ws['ref']}")

    def cmd_select_workspace(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        self.wmux.json(["select-workspace", ws["id"]])
        side = self.side.workspace(ws["id"])
        side["unread"] = 0
        self.side.touch(ws["id"])
        self.workspaces(refresh=True)
        self.push_lane(ws, side)
        self.out(f"Selected workspace {ws['ref']}")

    def cmd_rename_workspace(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        title = str(flags.get("--title") or (pos[0] if pos else ""))
        if not title:
            raise ShimError("Error: rename-workspace needs a title", 2)
        self.wmux.json(["rename-workspace", ws["id"], title])
        self.side.workspace(ws["id"])["custom_title"] = title
        self.side.dirty = True
        self.out(f"Renamed workspace {ws['ref']} to \"{title}\"")

    def cmd_workspace_action(self, pos, flags) -> None:
        action = str(flags.get("--action") or (pos[0] if pos else ""))
        ws = self.resolve_workspace(flags)
        side = self.side.workspace(ws["id"])
        if action in ("pin", "unpin"):
            side["pinned"] = action == "pin"
        elif action == "set-description":
            side["description"] = str(flags.get("--description") or "")
        elif action == "clear-description":
            side["description"] = ""
        elif action == "rename":
            return self.cmd_rename_workspace([], {**flags, "--title": flags.get("--title") or ""})
        elif action == "clear-name":
            side["custom_title"] = None
        elif action == "set-color":
            side["color"] = str(flags.get("--color") or "")
        elif action == "clear-color":
            side["color"] = None
        elif action == "mark-read":
            side["unread"] = 0
        elif action == "mark-unread":
            side["unread"] = max(1, int(side.get("unread") or 0))
        elif action in ("move-top", "move-up", "move-down"):
            # wmux has no reorder RPC. The sidecar order drives the switchboard board now
            # and seed-session.ps1 applies it to wmux's own list at the next start.
            self.move(ws, action)
        elif action in ("close-others", "close-above", "close-below"):
            for other in list(self.workspaces()):
                if other["id"] == ws["id"]:
                    continue
                if action == "close-above" and other["index"] > ws["index"]:
                    continue
                if action == "close-below" and other["index"] < ws["index"]:
                    continue
                self.wmux.json(["close-workspace", other["id"]], timeout=30)
            self.workspaces(refresh=True)
        else:
            raise ShimError(f"Error: unknown workspace-action '{action}'", 2)
        self.side.dirty = True
        if ws["id"] in {w["id"] for w in self.workspaces()}:
            self.push_lane(ws, side)
        if action in SEED_ONLY_ACTIONS and not flags.get("--quiet"):
            print(SEED_HINT, file=sys.stderr)
        self.out(f"ok {action} {ws['ref']}")

    def move(self, ws: dict[str, Any], action: str) -> None:
        """Renumber `order_index` across all workspaces so one move is exact."""
        live = self.workspaces()
        ordered = sorted(live, key=lambda w: (self.side.workspace(w["id"]).get("order_index") is None,
                                             self.side.workspace(w["id"]).get("order_index") or 0, w["index"]))
        ids = [w["id"] for w in ordered]
        at = ids.index(ws["id"])
        ids.pop(at)
        target = {"move-top": 0, "move-up": max(0, at - 1), "move-down": min(len(ids), at + 1)}[action]
        ids.insert(target, ws["id"])
        for n, wid in enumerate(ids):
            self.side.workspace(wid)["order_index"] = n
        self.side.dirty = True

    def cmd_reorder_workspace(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        side = self.side.workspace(ws["id"])
        side["order_index"] = int(flags.get("--index") or 0)
        self.side.dirty = True
        self.out(f"ok reorder {ws['ref']} -> {side['order_index']} (applied by seed-session.ps1 on next wmux start)")

    def cmd_rename_tab(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags, required=False)
        sid = self.resolve_surface(flags, ws)
        title = str(flags.get("--title") or (pos[0] if pos else ""))
        self.wmux.json(["rename-surface", sid, title])
        self.out("ok")

    def cmd_tab_action(self, pos, flags) -> None:
        self.out(f"ok {flags.get('--action') or ''} (no-op on wmux)")

    # typing
    @staticmethod
    def unescape(text: str) -> str:
        return text.replace("\\r\\n", "\r").replace("\\r", "\r").replace("\\n", "\r").replace("\\t", "\t").replace("\\e", "\x1b").replace("\\x1b", "\x1b")

    def type_into(self, sid: str, text: str, press_enter: bool = False) -> None:
        raw = self.unescape(text)
        trailing_enter = raw.endswith("\r")
        body = raw.rstrip("\r") if trailing_enter else raw
        before = self.prompt_seq(sid) if (trailing_enter or press_enter) and body else None
        if body:
            note_injected(sid, body)
            result = self.wmux.run(["send", "--surface", sid, body], timeout=15)
            if result.returncode:
                raise ShimError((result.stderr or "wmux send failed").strip(), result.returncode)
        if trailing_enter or press_enter:
            if body:
                # Let the paste settle first: an Enter that lands while ConPTY is still
                # delivering a long paste is lost.
                time.sleep(self.submit_settle(body))
            self.wmux.json(["send-key", "enter", "--surface", sid])
            if before is not None:
                self.confirm_submitted(sid, before)

    # --- reliable submit ---------------------------------------------------------
    # wmux reads a background tab that was never shown as blank text, so the
    # launcher cannot see a typed-but-unsubmitted command. wmux's prompt log can:
    # every line a shell or agent accepts gets an entry. No new entry after the
    # Enter means it was lost, so Enter goes again, a bounded number of times.

    @staticmethod
    def env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return default

    def submit_settle(self, body: str) -> float:
        base = self.env_float("SWITCHBOARD_SHIM_SUBMIT_SETTLE", 0.4)
        return 0.0 if base <= 0 else min(2.0, base + len(body) / 2000)

    def prompt_log(self, sid: str) -> dict[str, Any] | None:
        """wmux `prompts --json` for the surface, or None when this wmux has no
        prompt log, it is switched off, or the call fails."""
        try:
            payload = self.wmux.json(["prompts", "--surface", sid, "--json"], timeout=10)
        except ShimError:
            return None
        if not isinstance(payload, dict) or payload.get("enabled") is False or not isinstance(payload.get("prompts"), list):
            return None
        return payload

    def prompt_seq(self, sid: str) -> int | None:
        payload = self.prompt_log(sid)
        if payload is None:
            return None
        return max((int(p.get("seq") or 0) for p in payload["prompts"] if isinstance(p, dict)), default=0)

    def confirm_submitted(self, sid: str, before: int) -> bool:
        tries = int(self.env_float("SWITCHBOARD_SHIM_SUBMIT_RETRIES", 3))
        wait = self.env_float("SWITCHBOARD_SHIM_SUBMIT_WAIT", 2.5)
        for attempt in range(tries + 1):
            deadline = time.monotonic() + wait
            while True:
                seq = self.prompt_seq(sid)
                if seq is None or seq > before:
                    return True
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            if attempt == tries:
                break
            print(f"cmux-shim: no prompt reached {sid} after Enter; pressing Enter again ({attempt + 1}/{tries})", file=sys.stderr)
            self.wmux.json(["send-key", "enter", "--surface", sid])
        print(f"cmux-shim: {sid} never acknowledged the line after {tries} extra Enter presses", file=sys.stderr)
        return False

    def wait_shell_ready(self, sid: str) -> bool:
        """A new tab's shell is ready once it has drawn anything (text, or a cursor
        moved off the origin: a never-shown wmux tab reads as blank text even with
        its prompt drawn). Typed earlier, the line can reach Git Bash before
        readline and lose its Enter."""
        deadline = time.monotonic() + self.env_float("SWITCHBOARD_SHIM_READY_SECONDS", 15)
        while True:
            try:
                info = self.wmux.json(["read-screen", "--surface", sid, "--lines", "5"], timeout=10)
            except ShimError:
                info = {}
            if isinstance(info, dict) and ((info.get("text") or "").strip() or info.get("cursorX") or info.get("cursorY")):
                time.sleep(self.env_float("SWITCHBOARD_SHIM_READY_SETTLE", 0.5))
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)

    def cmd_launch_state(self, pos, flags) -> None:
        """Shim-only verb for dispatch.py on wmux: what a worker tab has accepted.
        {"surface_id", "prompt_log": bool, "shell_prompts": [text], "agent_prompts": n,
         "provider", "session_id", "state", "screen"}."""
        ws = self.resolve_workspace(flags)
        sid = self.resolve_surface({}, ws)
        log = self.prompt_log(sid)
        entries = [p for p in (log or {}).get("prompts") or [] if isinstance(p, dict)]
        entry = self.agent_entry(sid)
        self.out_json({
            "surface_id": sid, "prompt_log": log is not None,
            "shell_prompts": [str(p.get("text") or "") for p in entries if p.get("source") == "shell"],
            "agent_prompts": sum(1 for p in entries if p.get("source") == "agent"),
            "provider": entry.get("sessionProvider") or entry.get("agent"), "session_id": entry.get("sessionId"),
            "state": entry.get("state") or "unknown", "screen": self.screen_of(sid) or "",
        })

    def cmd_send(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags, required=False)
        sid = self.resolve_surface(flags, ws)
        self.type_into(sid, " ".join(pos))
        if ws:
            self.side.touch(ws["id"])
        self.out("ok")

    KEY_MAP = {"enter": "enter", "return": "enter", "tab": "tab", "escape": "esc", "esc": "esc", "backspace": "backspace",
               "delete": "delete", "up": "up", "down": "down", "left": "left", "right": "right", "home": "home",
               "end": "end", "pageup": "pageup", "pagedown": "pagedown", "space": " "}

    def cmd_send_key(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags, required=False)
        sid = self.resolve_surface(flags, ws)
        for token in pos:
            parts = [p.strip().lower() for p in re.split(r"[+-]", token) if p.strip()] or [token]
            mods = [p for p in parts[:-1] if p in ("ctrl", "control", "shift", "alt", "option", "cmd", "meta")]
            key = parts[-1]
            args = ["send-key", self.KEY_MAP.get(key, key)]
            for mod in mods:
                args.append({"control": "--ctrl", "ctrl": "--ctrl", "shift": "--shift", "alt": "--alt", "option": "--alt",
                             "cmd": "--ctrl", "meta": "--ctrl"}[mod])
            args += ["--surface", sid]
            self.wmux.json(args)
        self.out("ok")

    # status pills / agent state
    @staticmethod
    def state_for_label(label: str) -> str:
        low = label.lower()
        if any(k in low for k in ("thinking", "running", "working", "tool")):
            return "running"
        if any(k in low for k in ("needs_input", "needs input", "interrupted", "error", "blocked", "approval")):
            return "interrupted"
        return "idle"

    @staticmethod
    def humanize(label: str) -> str:
        label = re.sub(r"^__cmux_amp_status_", "", label).replace("_", " ").strip()
        return label[:1].upper() + label[1:] if label else ""

    def cmd_set_status(self, pos, flags) -> None:
        if len(pos) < 2:
            raise ShimError("Error: set-status <key> <value>", 2)
        key, value = pos[0], pos[1]
        ws = self.resolve_workspace(flags)
        side = self.side.workspace(ws["id"])
        before = amp_state(side)
        side.setdefault("status", {})[key] = {"value": value, "icon": flags.get("--icon"), "color": flags.get("--color"),
                                               "priority": int(flags.get("--priority") or 0), "timestamp": time.time()}
        self.side.dirty = True
        if key in ("amp", "program_status"):
            state = self.state_for_label(value)
            # A turn that ends while the workspace is not on screen is unread, as
            # cmux counts it; the sidebar sync spends it when the row is selected.
            if before == "running" and state != "running" and not ws.get("selected"):
                side["unread"] = int(side.get("unread") or 0) + 1
            self.side.touch(ws["id"])
            self.push_lane(ws, side, self.humanize(value) or None)
        self.out("ok")

    def mirror_blocked(self, ws: dict[str, Any], side: dict[str, Any], blocked: bool) -> None:
        if os.environ.get("SWITCHBOARD_WMUX_REPORT_AGENT", "1") != "1":
            return
        if blocked == bool(side.get("blocked")):
            return
        try:
            sid = self.resolve_surface({}, ws)
        except ShimError:
            return
        args = ["report-agent", "--blocked", "Amp needs input"] if blocked else ["report-agent", "--unblocked"]
        self.wmux.run([*args, "--surface", sid], timeout=10)
        side["blocked"] = blocked
        self.side.dirty = True

    def cmd_clear_status(self, pos, flags) -> None:
        key = pos[0] if pos else ""
        ws = self.resolve_workspace(flags)
        side = self.side.workspace(ws["id"])
        if key in (side.get("status") or {}):
            del side["status"][key]
            self.side.dirty = True
        if key in ("amp", "program_status"):
            self.push_lane(ws, side)
        self.out("ok")

    def cmd_list_status(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        for key, item in sorted((self.side.workspace(ws["id"]).get("status") or {}).items()):
            extras = " ".join(f"{k}={item[k]}" for k in ("icon", "color", "priority") if item.get(k) not in (None, "", 0))
            self.out(f"{key}={item.get('value', '')}{(' ' + extras) if extras else ''}")

    def cmd_log(self, pos, flags) -> None:
        level = str(flags.get("--level") or "info")
        source = str(flags.get("--source") or "")
        message = " ".join(pos)
        if source:
            message = f"[{source}] {message}"
        self.wmux.run(["log", level, message], timeout=10)
        self.out("ok")

    def cmd_notify(self, pos, flags) -> None:
        title = str(flags.get("--title") or "")
        body = str(flags.get("--body") or " ".join(pos))
        text = f"{title}: {body}" if title and body else (title or body)
        # wmux attaches a notification to the surface named by WMUX_SURFACE_ID and
        # bumps that workspace's unread badge. cmux's --workspace flag maps onto it
        # by resolving the target's first surface; without a target the pane's own
        # WMUX_SURFACE_ID (or nothing) applies, as before.
        ws = self.resolve_workspace(flags, required=False)
        env: dict[str, str] = {}
        if ws is not None:
            try:
                env["WMUX_SURFACE_ID"] = self.resolve_surface({}, ws)
            except ShimError:
                pass
            side = self.side.workspace(ws["id"])
            if not ws.get("selected"):
                side["unread"] = int(side.get("unread") or 0) + 1
                self.side.dirty = True
        self.wmux.run(["notify", text], timeout=10, env=env or None)
        self.out("ok")

    def cmd_markdown(self, pos, flags) -> None:
        sub = pos[0] if pos else ""
        if sub == "open" and len(pos) > 1:
            # `wmux markdown <file>` prints no surface id, so create the surface
            # here and load the file into it; the id lets callers `markdown set` later.
            created = self.wmux.json(["new-surface", "--type", "markdown"], timeout=20)
            sid = str(created.get("surfaceId") or "")
            if not sid:
                raise ShimError("Error: wmux new-surface --type markdown returned no surfaceId", 1)
            result = self.wmux.run(["markdown", "set", sid, "--file", pos[1]], timeout=20)
            if result.returncode:
                raise ShimError((result.stderr or "wmux markdown set failed").strip(), result.returncode)
            return self.out(f"ok {self.side.surface_ref(sid)} {sid}")
        if sub == "set" and len(pos) > 1:
            sid = pos[1]
            if sid.startswith("surface:"):
                sid = self.side.surface_id(sid) or sid
            args = ["markdown", "set", sid]
            if flags.get("--file"):
                args += ["--file", str(flags["--file"])]
            elif flags.get("--content") not in (None, True):
                args += ["--content", str(flags["--content"])]
            else:
                args += ["--content", sys.stdin.read()]
            if flags.get("--title"):
                args += ["--title", str(flags["--title"])]
            result = self.wmux.run(args, timeout=20)
            if result.returncode:
                raise ShimError((result.stderr or "wmux markdown set failed").strip(), result.returncode)
            return self.out("ok")
        raise ShimError("Error: markdown open <path> | markdown set <surface> --content|--file [--title] are the forms cmux-shim supports", 2)

    def cmd_top(self, pos, flags) -> None:
        # cmux-helpers greps `top --processes` for ssh/mosh/vmctl rows to detect a
        # remote workspace; wmux has no process table, so an empty table says "local".
        self.out("")

    def cmd_hooks(self, pos, flags) -> None:
        sub = pos[0] if pos else ""
        if sub == "install":
            return self.out("ok (wmux: Amp hooks run through cmux-shim; nothing to install)")
        if sub == "enqueue":
            payload: dict[str, Any] = {}
            if not sys.stdin.isatty():
                try:
                    payload = json.loads(sys.stdin.read() or "{}")
                except ValueError:
                    payload = {}
            event = pos[2] if len(pos) > 2 else ""
            ws = self.resolve_workspace({}, required=False)
            if ws is None and os.environ.get("WMUX_SURFACE_ID"):
                # A Claude Code started by hand in a wmux pane has WMUX_SURFACE_ID but no
                # CMUX_WORKSPACE_ID, so its session would never bind to the tab and owner_tabs
                # would read the CoS as running no agent. The pane's own surface names its tab.
                ws = self.surface_workspace()
            if ws:
                side = self.side.workspace(ws["id"])
                if payload.get("session_id"):
                    side["thread"] = str(payload["session_id"])
                side["last_event"] = {"event": event, "at": time.time()}
                self.side.touch(ws["id"])
            return self.out("ok")
        raise ShimError(f"Error: hooks {sub} is not supported by cmux-shim", 2)

    def cmd_sidebar(self, pos, flags) -> None:
        """cmux custom sidebars (system/cmux-sidebars/switchboard.swift) have no wmux
        counterpart. The same lanes come from the sidecar: `sidebar lanes --json`
        feeds the switchboard board (system/scripts/mux/switchboard-board.py), `reload`
        re-pushes every lane into wmux's own rows, `validate` checks the model."""
        sub = pos[0] if pos else "lanes"
        if sub in ("lanes", "state", "board"):
            view = self.lane_view()
            if flags.get("--json") or sub == "state":
                return self.out_json(view)
            lines = [f"{view['workspace_count']} ws · {view['unread_total']} unread"]
            for lane in view["lanes"]:
                if not lane["count"]:
                    continue
                lines.append(f"{lane['label'].upper()} ({lane['count']})")
                for row in lane["workspaces"]:
                    marks = ("⚡" if row["agent_state"] == "running" else "") + ("!" if row["lane"] == "attention" else "")
                    unread = f" [{row['unread']}]" if row["unread"] else ""
                    lines.append(f"  {'*' if row['selected'] else ' '} {row['ref']} {row['clean_title']}{(' ' + marks) if marks else ''}{unread}")
            return self.out("\n".join(lines))
        if sub in ("reload", "refresh", "install", "activate", "use"):
            for w in self.workspaces():
                side = self.side.workspace(w["id"])
                side["pushed"] = None
                self.push_lane(w, side)
            return self.out(f"ok sidebar {sub} ({len(self.workspaces())} lane rows pushed to wmux)")
        if sub == "validate":
            keys = [lane["key"] for lane in LANES]
            problems = [k for k in CMUX_LANE if k not in keys] + [k for k in keys if k not in CMUX_LANE]
            if problems:
                raise ShimError(f"Error: lane model mismatch: {problems}", 1)
            return self.out(f"ok {len(LANES)} lanes: {', '.join(keys)}")
        self.out(f"ok sidebar {' '.join(pos)} (no-op on wmux)")

    def cmd_session_bind(self, pos, flags) -> None:
        ws = self.resolve_workspace(flags)
        thread = str(flags.get("--thread") or (pos[0] if pos else ""))
        side = self.side.workspace(ws["id"])
        side["thread"] = thread or None
        self.side.dirty = True
        self.out(f"ok {ws['ref']} -> {thread or '(unbound)'}")

    def cmd_closed_workspaces(self, pos, flags) -> None:
        self.out_json({"closed": self.side.data.get("closed") or []})

    def surface_workspace_id(self) -> str:
        """The wmux workspace id of the calling pane, or "" when wmux cannot say."""
        # wmux scopes current-workspace to the caller's WMUX_SURFACE_ID. The bare
        # form runs first; `--surface` is a fallback, because whether every wmux
        # build accepts that flag is not verified on Windows.
        surface = os.environ.get("WMUX_SURFACE_ID") or os.environ.get("CMUX_SURFACE_ID") or ""
        wid = ""
        for args in (["current-workspace"], *([["current-workspace", "--surface", surface]] if surface else [])):
            try:
                payload = self.wmux.json(args, timeout=10)
            except ShimError:
                continue
            if isinstance(payload, dict):
                wid = payload.get("workspaceId") or payload.get("id") or (payload.get("workspace") or {}).get("id") or payload.get("raw") or ""
            if wid:
                break
        return str(wid or "")

    def surface_workspace(self) -> dict[str, Any] | None:
        wid = self.surface_workspace_id()
        return next((w for w in self.workspaces() if w["id"] == wid), None) if wid else None

    def cmd_workspace_id_of_surface(self, pos, flags) -> None:
        """Print the wmux workspace id of the calling pane (wsl-env.sh / mux.sh
        use it to set CMUX_WORKSPACE_ID, which wmux does not export)."""
        wid = self.surface_workspace_id()
        if not wid:
            raise ShimError("Error: wmux current-workspace returned no id", 1)
        self.out(wid)

    # -- agent message (permission relay, report-to-cos, owner-tab steers) ----
    # cmux delivers `agent message` through its Claude hooks: an idle session
    # wakes, a busy one reads it at its next step, and nothing is typed. wmux
    # 2.49.0 has no such channel (no IPC verb, and `answer-agent` only presses a
    # choice the agent declared), so the shim types the message into the Claude
    # composer and submits it, the transport cmux-inject-guard.sh uses for a
    # Claude tab. A busy Claude session queues a submitted message, as cmux does.
    # Refused, never typed: a blocked pane (a dialog would take the keystrokes as
    # its answer), a composer holding a draft, an unreadable composer, and a pane
    # that runs no Claude session.
    COMPOSER_RULE = re.compile(r"^\s*─{10,}\s*$")
    COMPOSER_HINTS = (re.compile(r'^\s*Try "[^"]*"\s*$'), re.compile(r"^\s*Press up to edit queued messages\s*$"))
    # Claude Code draws its prompt as "❯", or as a plain ">" (seen on Windows). A ">"
    # counts only on a line fenced by two rules, which no shell draws, and never as ">>".
    PROMPT = re.compile(r"^(\s*)(?:❯|>(?!>))")

    @classmethod
    def prompt_at(cls, line: str) -> int | None:
        """Column of the prompt mark when `line` starts with one, else None."""
        m = cls.PROMPT.match(line)
        return len(m.group(1)) if m else None

    @classmethod
    def composer(cls, screen: str) -> tuple[str, str]:
        """("claude-empty" | "claude-text" | "none", whitespace-free draft) for the
        live Claude composer: the last `❯` (or `>`) line under a rule, up to the next
        rule. Same rules as cig_composer_state in lib/cmux-inject-guard.sh."""
        lines = (screen or "").splitlines()
        top = bottom = None
        for i, line in enumerate(lines):
            if cls.prompt_at(line) is not None and i and cls.COMPOSER_RULE.match(lines[i - 1]):
                top, bottom = i, None
            elif cls.COMPOSER_RULE.match(line) and top is not None and i > top and bottom is None:
                bottom = i
        if top is None or bottom is None:
            return "none", ""
        body = []
        for i in range(top, bottom):
            text = lines[i]
            if i == top:
                text = text.lstrip()[1:]
                if any(h.match(text) for h in cls.COMPOSER_HINTS):
                    text = ""
            body.append(re.sub(r"\s+", "", text))
        draft = "".join(body)
        return ("claude-text" if draft else "claude-empty"), draft

    # Claude Code shows a dimmed prompt suggestion in an idle composer. A screen dump has no
    # colour, so a suggestion would read as a draft and hold every report. The cursor
    # tells them apart, verified live: typed text leaves it after the text, a suggestion
    # leaves it just after the `❯ `. A draft after the Home key looks the same as a suggestion,
    # so agent_message proves the composer before it presses Enter (see composer_holds_only).
    @classmethod
    def suggestion_line(cls, text: str, payload: Any) -> int | None:
        """Index of the ❯ line when what it shows is a suggestion, else None. Anything
        unproven (no cursor, more than one line, a cursor elsewhere) stays a draft."""
        if not isinstance(payload, dict):
            return None
        rows, x, y = payload.get("rows"), payload.get("cursorX"), payload.get("cursorY")
        if not all(isinstance(v, int) for v in (rows, x, y)):
            return None
        normalized = (text or "").replace("\u00a0", " ")
        state, _ = cls.composer(normalized)
        if state != "claude-text":
            return None
        lines = normalized.split("\n")
        if lines and not lines[-1]:
            lines.pop()
        index = len(lines) - rows + y
        if not 0 < index < len(lines) - 1 or cls.prompt_at(lines[index]) is None:
            return None
        if not cls.COMPOSER_RULE.match(lines[index - 1]) or not cls.COMPOSER_RULE.match(lines[index + 1]):
            return None
        return index if x == cls.prompt_at(lines[index]) + 2 else None

    @classmethod
    def hide_suggestion(cls, text: str, payload: Any) -> str:
        index = cls.suggestion_line(text, payload)
        if index is None:
            return text
        lines = text.split("\n")
        lines[index] = lines[index][:cls.prompt_at(lines[index]) + 1] + " "
        return "\n".join(lines)

    def read_composer(self, sid: str) -> tuple[str | None, str, str, bool]:
        """(screen, state, draft, suggestion). `state` stays claude-text for a suggestion, so
        a caller has to choose what it means; see composer_state for the plain answer."""
        try:
            payload = self.wmux.json(["read-screen", "--surface", sid, "--lines", "60"], timeout=10)
        except ShimError:
            return None, "none", "", False
        text = None
        if isinstance(payload, dict):
            text = payload.get("text")
            if not isinstance(text, str) and isinstance(payload.get("lines"), list):
                text = "\n".join(str(line) for line in payload["lines"])
        if not isinstance(text, str):
            return None, "none", "", False
        screen = text.replace("\u00a0", " ")
        state, draft = self.composer(screen)
        return screen, state, draft, self.suggestion_line(screen, payload) is not None

    def composer_state(self, sid: str) -> str:
        """claude-empty | claude-text | none, where a suggestion counts as empty."""
        _, state, _, suggestion = self.read_composer(sid)
        return "claude-empty" if suggestion else state

    @staticmethod
    def comparable(text: str) -> str:
        """ASCII only: the screen draws an emoji with extra cells and wraps long lines."""
        return re.sub(r"[^!-~]", "", text)

    def composer_holds_only(self, sid: str, body: str) -> bool:
        """After typing over a suggestion and before Enter: is `body` all that is in the box?
        A draft parked with the cursor at its start would show around our text."""
        wanted = self.comparable(body.replace("\x1b[200~", "").replace("\x1b[201~", ""))
        deadline = time.monotonic() + self.env_float_value("SWITCHBOARD_SHIM_VERIFY_SECONDS", 4)
        while True:
            _, state, draft, _ = self.read_composer(sid)
            seen = self.comparable(draft)
            if state == "claude-text" and (seen == wanted or re.fullmatch(r"\[Pastedtext#\d+[^\]]*\]", draft)):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def erase_typed(self, sid: str, body: str) -> None:
        """Take back what was typed at the start of the box. Backspace cannot reach past
        the point the typing began, so a draft after it is safe from extra presses. Ctrl+U
        clears one wrapped row only, as tested on Claude Code 2.1."""
        for _ in body.replace("\x1b[200~", "").replace("\x1b[201~", ""):
            self.wmux.run(["send-key", "backspace", "--surface", sid], timeout=10)

    @staticmethod
    def typeable(text: str) -> str:
        """Text that is safe to type into a Claude composer. Control characters other
        than a newline are dropped (a lone CR submits early, ^C cancels the turn, ESC
        can end a bracketed paste and start a key sequence), a tab becomes a space,
        and a leading !, / or # (bash mode, a slash command, a memory note) gets a
        prefix so the message arrives as plain text, as cmux delivers it."""
        body = text.replace("\r\n", "\n").replace("\t", " ")
        body = "".join(ch for ch in body if ch == "\n" or not (ord(ch) < 32 or ord(ch) == 127))
        if body.lstrip()[:1] in ("!", "/", "#"):
            body = "[message] " + body.lstrip()
        return body

    def find_target(self, token: str) -> dict[str, Any]:
        token = (token or "").strip()
        if not token:
            raise ShimError("Error: agent message <target> needs a workspace ref, id, or title", 2)
        for w in self.workspaces():
            if token in (w["ref"], w["id"]):
                return w
        low = token.lower()
        for w in self.workspaces():
            names = {w["title"], clean_title(w["title"]), clean_title(w["custom_title"]),
                     title_text(clean_title(w["custom_title"]))}
            if low in {n.lower() for n in names}:
                return w
        raise ShimError(f"Error: workspace not found: {token}", 1)

    def agent_entry(self, sid: str) -> dict[str, Any]:
        try:
            payload = self.wmux.json(["agent-state", "--surface", sid], timeout=10)
        except ShimError:
            return {}
        entry = payload.get("state") if isinstance(payload, dict) else None
        return entry if isinstance(entry, dict) else {}

    def screen_of(self, sid: str, lines: int = 60) -> str | None:
        try:
            payload = self.wmux.json(["read-screen", "--surface", sid, "--lines", str(lines)], timeout=10)
        except ShimError:
            return None
        if isinstance(payload, dict):
            if isinstance(payload.get("text"), str):
                return payload["text"].replace(" ", " ")   # see cmd_read_screen
            if isinstance(payload.get("lines"), list):
                return "\n".join(str(line) for line in payload["lines"]).replace(" ", " ")
        return None

    def agent_message(self, pos: list[str], flags: dict[str, Any]) -> None:
        if not pos:
            raise ShimError("Error: agent message <target> [--from <sender>] [--json] -- <text>|-", 2)
        target, rest = pos[0], pos[1:]
        text = sys.stdin.buffer.read().decode("utf-8", "replace") if rest == ["-"] else " ".join(rest)
        text = text.rstrip("\r\n")
        if not text.strip():
            raise ShimError("Error: agent message: empty text", 2)
        ws = self.find_target(target)
        reply: dict[str, Any] = {"id": f"wmux-{int(time.time() * 1000)}", "state": "refused",
                                 "recipient_has_agent": False, "failure_reason": None,
                                 "recipient_workspace_ref": ws["ref"], "recipient_workspace_id": ws["id"],
                                 "recipient_workspace_title": clean_title(ws["custom_title"]),
                                 "backend": "wmux", "transport": "typed"}

        def done(**changes: Any) -> None:
            reply.update(changes)
            if flags.get("--json"):
                return self.out_json(reply)
            self.out(f"{reply['state']} {ws['ref']}" + (f": {reply['failure_reason']}" if reply["failure_reason"] else ""))

        try:
            sid = self.resolve_surface({}, ws)
        except ShimError as error:
            return done(failure_reason=str(error))
        entry = self.agent_entry(sid)
        screen, state, draft, suggestion = self.read_composer(sid)
        agent = str(entry.get("agent") or "").lower()
        is_claude = agent == "claude" or (not agent and state != "none")
        reply["recipient_has_agent"] = is_claude
        if agent and agent != "claude":
            return done(failure_reason=f"the pane runs {agent}; wmux typing reaches Claude Code sessions only")
        if entry.get("state") == "blocked":
            reason = entry.get("blockedReason") or "a dialog is open"
            return done(failure_reason=f"the recipient is waiting on a dialog ({reason}); typing would answer it")
        if screen is None:
            return done(failure_reason="wmux read-screen failed; the composer is unreadable")
        if state == "none":
            return done(failure_reason="no Claude composer on screen" if not agent else
                        "the Claude composer is not fully on screen; refusing to type into an unknown box")
        if state == "claude-text" and not suggestion:
            return done(failure_reason="the recipient's composer holds a draft; not typing over it")
        # One line by default: a raw newline would submit early, and cmd.exe cuts
        # an argument at a newline. " ⏎ " is the transport mark that
        # owner_tabs.decode_marks() reads back as a newline. SWITCHBOARD_WMUX_PASTE=
        # bracketed sends real newlines inside a bracketed paste instead, for a
        # ConPTY build that passes the paste markers through to Claude Code.
        body = self.typeable(text)
        via_cmd = bool(self.wmux.argv) and self.wmux.argv[0].lower().endswith((".cmd", ".bat"))
        # Long text never goes through the keyboard: a long report typed into a Claude
        # tab can arrive as only its last characters. The full text goes to a file and one short pointer line
        # is typed instead; Claude reads the file.
        limit = int(self.env_float_value("SWITCHBOARD_WMUX_TYPE_MAX", 300))
        if limit > 0 and len(body.replace("\n", " ⏎ ")) > limit:
            spill = self.spill_message(reply["id"], text)
            body = self.pointer_line(text, spill, limit)
            reply.update(spilled_to=str(spill), spilled_chars=len(text))
        if via_cmd and any(ch in body for ch in CMD_UNSAFE):
            # cmd.exe parses a .cmd argument again: an embedded quote leaves & | < > bare
            # and %VAR% expands even inside quotes, so the text would run as commands.
            return done(failure_reason="wmux runs through wmux.cmd here, and cmd.exe would rewrite this text; "
                                       "put node on PATH or set WMUX_NODE and WMUX_CLI so the shim calls wmux.js")
        bracketed = os.environ.get("SWITCHBOARD_WMUX_PASTE", "").lower() == "bracketed"
        if bracketed and not via_cmd:
            body = "\x1b[200~" + body + "\x1b[201~"
        else:
            body = body.replace("\n", " ⏎ ")
        before = self.prompt_entries(sid)
        note_injected(sid, body.replace("\x1b[200~", "").replace("\x1b[201~", ""))
        sent = self.wmux.run(["send", "--surface", sid, body], timeout=15)
        if sent.returncode:
            return done(failure_reason=(sent.stderr or sent.stdout or "wmux send failed").strip()[:300])
        settle = float(os.environ.get("CMUX_SHIM_SETTLE", "0.5"))
        time.sleep(settle)
        if suggestion and not self.composer_holds_only(sid, body):
            self.erase_typed(sid, body)
            _, _, left, _ = self.read_composer(sid)
            if left != draft:
                # Our text may still be in the box. No retry, or it could appear twice.
                return done(state="typed", failure_reason="typed beside a draft and could not take it back; check the composer")
            return done(failure_reason="the recipient's composer holds a draft; not typing over it")
        self.wmux.run(["send-key", "enter", "--surface", sid], timeout=10)
        outcome = "typed"
        for attempt in range(2):
            time.sleep(settle)
            # Enter on a suggestion would submit it as if the owner had, so a second press
            # needs real text in the box.
            after = self.composer_state(sid)
            if after == "claude-empty":
                outcome = "delivered"
                break
            if after == "claude-text" and attempt == 0:
                self.wmux.run(["send-key", "enter", "--surface", sid], timeout=10)
        self.side.touch(ws["id"])
        # Verified, not assumed: wmux's prompt log records every line the pane
        # accepted. The new entry must be the line typed, character for character
        # (whitespace aside); a different entry is a garbled delivery.
        if before is not None:
            wanted = re.sub(r"\s+", "", body.replace("\x1b[200~", "").replace("\x1b[201~", "").replace("⏎", ""))
            seq0 = max((int(p.get("seq") or 0) for p in before), default=0)
            entry = self.wait_new_prompt(sid, seq0)
            if entry is not None:
                got = re.sub(r"\s+", "", str(entry.get("text") or "").replace("⏎", ""))
                if got == wanted:
                    return done(state="delivered", failure_reason=None, verified="prompt-log")
                return done(state="garbled", verified="prompt-log",
                            failure_reason=f"the recipient accepted a different line ({len(got)} of {len(wanted)} "
                                           "characters); the message did not arrive whole")
            reply["verified"] = "none"
            # No new entry: a busy Claude session queues the line and logs it when
            # it takes it. "queued" is accepted but unproven; `agent seen` proves it.
            if outcome == "delivered":
                outcome = "queued"
        else:
            reply["verified"] = "composer" if outcome == "delivered" else "none"
        # "typed" means the text went in and the composer did not read back empty:
        # callers must not retry, or the recipient gets the message twice.
        return done(state=outcome, failure_reason=None)

    # -- delivery proof -----------------------------------------------------------
    @staticmethod
    def env_float_value(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return default

    @staticmethod
    def spill_dir() -> Path:
        return Path(os.environ.get("SWITCHBOARD_WMUX_SPILL_DIR") or default_state_dir() / "messages")

    def spill_message(self, msg_id: str, text: str) -> Path:
        folder = self.spill_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{msg_id}.md"
        path.write_text(text.replace("\r\n", "\n") + "\n", encoding="utf-8")
        return path

    def pointer_line(self, text: str, path: Path, limit: int) -> str:
        """One typeable line that names the file holding the full message. It keeps
        the message's opening (a report's "[<emoji> <Name> · rt-…] done:" lead) so
        `agent seen` can find it on the recipient's prompt log."""
        tail = f" … [full message, {len(text)} characters: {path}: read that file and show it in full]"
        first = self.typeable(text).split("\n", 1)[0].strip()
        room = max(40, limit - len(tail))
        head = first if len(first) <= room else first[:room].rstrip()
        return self.typeable(head + tail)

    def prompt_entries(self, sid: str) -> list[dict[str, Any]] | None:
        """The pane's accepted-prompt log (`wmux prompts --json`), or None when this
        wmux has none, it is switched off, or the call fails."""
        try:
            payload = self.wmux.json(["prompts", "--surface", sid, "--json"], timeout=10)
        except ShimError:
            return None
        if not isinstance(payload, dict) or payload.get("enabled") is False or not isinstance(payload.get("prompts"), list):
            return None
        return [p for p in payload["prompts"] if isinstance(p, dict)]

    def wait_new_prompt(self, sid: str, seq0: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.env_float_value("SWITCHBOARD_SHIM_VERIFY_SECONDS", 4)
        while True:
            entries = [p for p in (self.prompt_entries(sid) or []) if int(p.get("seq") or 0) > seq0
                       and p.get("source", "agent") != "shell"]
            if entries:
                return entries[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def agent_seen(self, pos: list[str], flags: dict[str, Any]) -> None:
        """`agent seen <target> --marker <text>`: did the pane take a line holding
        `marker` (whitespace ignored)? Prompt log first, then the screen (a queued
        line shows there before the log has it)."""
        if not pos or not flags.get("--marker"):
            raise ShimError("Error: agent seen <target> --marker <text> [--json]", 2)
        ws = self.find_target(pos[0])
        sid = self.resolve_surface({}, ws)
        marker = re.sub(r"\s+", "", str(flags["--marker"]))
        view: dict[str, Any] = {"workspace_ref": ws["ref"], "seen": False, "where": None, "prompt_log": False}
        entries = self.prompt_entries(sid)
        if entries is not None:
            view["prompt_log"] = True
            hit = next((p for p in reversed(entries)
                        if self.whole_hit(re.sub(r"\s+", "", str(p.get("text") or "")), marker)), None)
            if hit is not None:
                view.update(seen=True, where="prompt-log", seq=hit.get("seq"))
        if not view["seen"]:
            # Only the transcript counts: text still sitting in the composer (an
            # Enter that never landed) was not taken by the agent.
            screen = self.above_composer(self.screen_of(sid, lines=400) or "")
            if screen and self.whole_hit(re.sub(r"\s+", "", screen), marker):
                view.update(seen=True, where="screen")
        return self.out_json(view) if flags.get("--json") else self.out("seen" if view["seen"] else "not-seen")

    POINTER_OPEN = "[fullmessage,"
    POINTER_CLOSE = "readthatfileandshowitinfull]"

    @classmethod
    def whole_hit(cls, text: str, marker: str) -> bool:
        """`marker` (whitespace-free) is in `text` and, when that line is a spill
        pointer, its closing words arrived too: a pointer cut short names no
        readable file, so it does not count as delivered."""
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                return False
            segment = text[i:i + len(marker) + 800]
            opened = segment.find(cls.POINTER_OPEN)
            if opened < 0 or cls.POINTER_CLOSE in segment[opened:]:
                return True
            start = i + 1

    @classmethod
    def above_composer(cls, screen: str) -> str:
        """The screen above the live Claude composer (all of it when none shows)."""
        lines = screen.splitlines()
        top = None
        for i, line in enumerate(lines):
            if line.lstrip().startswith("❯") and i and cls.COMPOSER_RULE.match(lines[i - 1]):
                top = i - 1
        return "\n".join(lines[:top] if top is not None else lines)

    def cmd_agent(self, pos, flags) -> None:
        sub = pos[0] if pos else ""
        if sub in ("message", "send"):
            return self.agent_message(pos[1:], flags)
        if sub == "seen":
            return self.agent_seen(pos[1:], flags)
        if sub in ("state", "status"):
            ws = self.find_target(pos[1]) if len(pos) > 1 else self.resolve_workspace(flags)
            sid = self.resolve_surface({}, ws)
            entry = self.agent_entry(sid)
            state = self.composer_state(sid)
            view = {"workspace_ref": ws["ref"], "workspace_id": ws["id"], "surface_id": sid,
                    "agent": entry.get("agent"), "state": entry.get("state") or "unknown",
                    "blocked_reason": entry.get("blockedReason"), "composer": state}
            return self.out_json(view) if flags.get("--json") else self.out(" ".join(f"{k}={v}" for k, v in view.items()))
        raise ShimError("Error: agent message|state|seen are the forms cmux-shim supports on wmux", 2)

    VERBS = {
        "identify": "cmd_identify", "ping": "cmd_ping",
        "list-workspaces": "cmd_list_workspaces", "workspace": "cmd_workspace", "rpc": "cmd_rpc", "tree": "cmd_tree",
        "read-screen": "cmd_read_screen", "new-workspace": "cmd_new_workspace", "close-workspace": "cmd_close_workspace",
        "select-workspace": "cmd_select_workspace", "focus-workspace": "cmd_select_workspace",
        "rename-workspace": "cmd_rename_workspace", "rename-window": "cmd_rename_workspace",
        "workspace-action": "cmd_workspace_action", "reorder-workspace": "cmd_reorder_workspace",
        "rename-tab": "cmd_rename_tab", "tab-action": "cmd_tab_action",
        "send": "cmd_send", "send-key": "cmd_send_key",
        "set-status": "cmd_set_status", "clear-status": "cmd_clear_status", "list-status": "cmd_list_status",
        "log": "cmd_log", "notify": "cmd_notify", "markdown": "cmd_markdown", "top": "cmd_top",
        "hooks": "cmd_hooks", "sidebar": "cmd_sidebar",
        "session-bind": "cmd_session_bind", "closed-workspaces": "cmd_closed_workspaces",
        "workspace-id-of-surface": "cmd_workspace_id_of_surface", "agent": "cmd_agent",
        "launch-state": "cmd_launch_state",
    }

    def dispatch(self, argv: list[str]) -> int:
        if not argv or argv[0] in ("--help", "-h", "help"):
            self.out("cmux-shim " + SHIM_VERSION + " (cmux " + CMUX_COMPAT_VERSION + " CLI over wmux)\nVerbs: " + ", ".join(sorted(self.VERBS)))
            return 0
        if argv[0] == "--version":
            self.out(f"cmux {CMUX_COMPAT_VERSION} (cmux-shim {SHIM_VERSION}, backend wmux)")
            return 0
        verb = argv[0]
        handler = self.VERBS.get(verb)
        if not handler:
            raise ShimError(f"Error: Unknown command '{verb}'. Run 'cmux --help' for the full command list.", 2)
        pos, flags = parse_args(argv[1:])
        getattr(self, handler)(pos, flags)
        return 0


def main(argv: list[str]) -> int:
    shim = Shim()
    try:
        code = shim.dispatch(argv)
    except ShimError as exc:
        print(str(exc), file=sys.stderr)
        code = exc.code
    finally:
        try:
            shim.finish()
        except Exception:  # never let bookkeeping mask the verb's own exit code
            pass
    return code


if __name__ == "__main__":
    # Windows text mode ends lines with CR LF; bash callers compare whole lines,
    # so print plain LF.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(newline=chr(10))
    sys.exit(main(sys.argv[1:]))
