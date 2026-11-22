#!/usr/bin/env python3
"""Let agents drive a real Brave on a machine that opted in: their own profile by default, the owner's when needed.

PM_OS_OWNER_BROWSER=brave in local/machines/<MACHINE>.env turns this on for that machine.
Every other machine keeps the headless sanctioned profile from AGENTS.md. The rule text is
in local/agents-local.md; this file is the part that does not depend on an agent reading it.

Two profiles, two MCP entries:

  chrome-devtools        `proxy --profile agent`: a separate Brave with its own user-data-dir
                         under %LOCALAPPDATA%\\pm-os\\agent-brave, signed in to nothing. Agents
                         start it minimized with a debugging port on 127.0.0.1, so no Allow
                         dialog and no focus change. Brave, like Chrome 136 and later, honors
                         --remote-debugging-port only with a non-default --user-data-dir.
  chrome-devtools-owner  `proxy --profile owner`: the owner's main Brave, through its Allow
                         dialog, once per Brave launch. A session states why it needs the
                         owner's signed-in session (`signedInNeed`) on its first call; it is logged.

The owner entry runs a stdio proxy in front of chrome-devtools-mcp --autoConnect. Every session's proxy talks to one shared
broker (`owner_browser.py broker`), which runs the only chrome-devtools-mcp. The browser asks
Allow once per connection, so one shared connection means one Allow each time the browser
starts, however many sessions use it, and a click that lands after a call gave up still counts. A proxy, because the server's own output
lists every open tab (new_page and close_page included), so only something in the stream
can keep the owner's tab titles and URLs out of a transcript. It enforces:

  one driver    a lease file, so one agent at a time drives the browser; others get a wait message
  own tabs      page tools work only on tabs this session opened, and listings hide the rest
  no focus      new_page is forced to the background and bringToFront is refused
  no secrets    browser-internal pages and scripts that read cookies or storage are refused
  own window    tools that change the whole browser or dump page memory are refused
  no jobs       a scheduled job is refused here and pointed at chrome-devtools-headless

Foreground tabs and other tabs take an `ownerApproved` argument holding the owner's words for
that call. The proxy strips it, logs it, and allows that one call. It is an audit trail, and
the rule is what makes an agent honest about it. Closing another tab, internal pages, stored
secrets, and whole-browser tools have no override. These checks stop mistakes. They are not a
security boundary against a hostile agent, which could reach the port itself once the owner
clicks Allow. One known gap: a click that opens a new tab leaves that tab open and untracked,
because adopting it could later close a tab the owner opened at that moment.

The agent entry runs the same proxy and policy in front of chrome-devtools-mcp --browserUrl.
It has no lease, because the agent profile is the agents' own and each session sees only its
own tabs, and no broker, because there is no dialog to share. Before every call it proves the
port belongs to the agent profile (the listener's command line names its folder), starting
the instance when nothing answers. Any local program can drive that port, so the agent
profile must never be signed in to anything sensitive.

  owner_browser.py mode                 print brave or off
  owner_browser.py set brave|off        write the key to this machine's file
  owner_browser.py status               who holds the lease, and whether the agent profile answers
  owner_browser.py release [--session ID]   free the lease (the Stop hook calls this)
  owner_browser.py agent start|stop|status  the agent profile's Brave, never the owner's
  owner_browser.py proxy [--profile agent|owner] --mcp-version V   run the MCP proxy (agent by default)
  owner_browser.py broker --mcp-version V   run the owner's shared connection (a proxy starts it when needed)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comms_triage_setting import env_file, local_dir, machine_name  # noqa: E402

KEY = "PM_OS_OWNER_BROWSER"
PROFILES = {"brave": ("BraveSoftware", "Brave-Browser", "User Data")}
LEASE_IDLE_SECONDS = 180
LEASE_RENEW_SECONDS = 30
CALL_WAIT_SECONDS = 120
SHUTDOWN_DRAIN_SECONDS = 3
SHUTDOWN_CLOSE_SECONDS = 8
BROKER_START_SECONDS = 30
RECONNECTED_METHOD = "notifications/pm_os/reconnected"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
RELINKS = 3
APPROVAL_ARG = "ownerApproved"

NEED_ARG = "signedInNeed"
AGENT_PORT = 9333
AGENT_START_SECONDS = 25
AGENT_CHECK_SECONDS = 3
AGENT_FLAGS = ("--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble",
               "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
               "--disable-background-timer-throttling")
SW_SHOWMINNOACTIVE = 7

SCHEDULED_REFUSAL = ("Scheduled jobs use the mcp__chrome-devtools-headless__ tools on this machine: the owner's browser "
                     "needs someone to click Allow, and the sanctioned headless profile holds the sign-ins scheduled "
                     "jobs rely on. Use the mcp__chrome-devtools-headless__ tools instead.")
NEED_MISSING = (f"This is the owner's own signed-in Brave. Pass `{NEED_ARG}` on this first call: why the task needs the "
                "owner's signed-in session, naming the brief or CoS line that says so. It is logged. Everything else "
                "uses the mcp__chrome-devtools__ tools, the agent profile, which needs no click.")
AGENT_DOWN = ("The agent browser profile did not start or does not answer on its port. Run "
              "`python3 system/scripts/owner_browser.py agent status` and `agent start`; agents may start and stop "
              "that instance, never the owner's main Brave. Otherwise use the API path or report the blocker.")
AGENT_PORT_TAKEN = ("Another program holds the agent browser's port on 127.0.0.1, and it is not the agent profile, "
                    "so nothing was sent to it. Report the blocker to CoS; do not attach to that port by hand.")
AGENT_NO_BRAVE = "Brave is not installed where the agent profile expects it, so the agent browser cannot start."
AGENT_SHOT = ("The agent browser stays minimized so it never takes focus, and a minimized window draws no frames, so a "
              "screenshot never finishes. Read the page with take_snapshot or evaluate_script. For a picture, use "
              "mcp__chrome-devtools-headless__take_screenshot.")
UNREACHABLE = ("The owner's browser is not reachable: it is closed, its remote debugging switch is off, or the Allow "
               "dialog was declined. Agents never start, restart, or reconfigure it. Use the API path if there is one. "
               "Otherwise ask with ask-cos.sh, saying the owner needs to open the browser and click Allow.")
CALL_PENDING = ("No answer yet, and the outcome is unknown. The owner's browser may be waiting for someone to click "
                "Allow in its remote debugging dialog: ask with ask-cos.sh, saying the owner needs to click Allow once, "
                "then retry. A click that comes later still counts, and a retry raises no second dialog. Otherwise the "
                "page is slow: check its state before repeating anything.")
CHILD_GONE = ("The browser tools stopped running in this session, and the outcome of any call in progress is unknown. "
              "Use the API path if there is one, or report the blocker. A new session starts them again.")
LEASE_BUSY = "The owner's browser lease is busy. Wait a few seconds and retry."
MISSING_PAGE = "Pass pageId: a tab this session opened with new_page."
RECONNECTED = ("(The browser restarted or reconnected, so tabs this session opened earlier are no longer tracked and "
               "cannot be closed from here. Open a new tab with new_page to continue.)")
OTHER_TAB = ("That is not a tab this session opened. Reading or changing the owner's other tabs needs the owner's word: "
             f"pass their words for this call in `{APPROVAL_ARG}`. Otherwise open your own tab with new_page first.")
CLOSE_OTHER = "Only tabs this session opened can be closed. That one is the owner's, and no override exists."
FOREGROUND = (f"Focus stays with the owner. Pass their words for this call in `{APPROVAL_ARG}` if they asked "
              "to bring the page forward.")
INTERNAL_URL = ("Browser-internal pages (settings, passwords, extensions, and local files) are off limits, "
                "and no override exists. Agents use http and https pages only.")
SECRET_SCRIPT = ("That script reads cookies or browser storage. Agents do not read or export browser-stored "
                 "secrets, and no override exists.")
WHOLE_BROWSER = ("That tool is off limits on the owner's browser, and no override exists: resize_page resizes their real "
                 "window, lighthouse_audit clears storage and cache in their real profile, and take_heapsnapshot "
                 "writes page memory, which can hold cookies and tokens, to a file.")
WHOLE_BROWSER_TOOLS = {"resize_page", "lighthouse_audit", "take_heapsnapshot"}
SECRET_RE = re.compile(r"document\s*\.\s*cookie|\[\s*['\"`]cookie['\"`]\s*\]|cookieStore|localStorage|sessionStorage|"
                       r"indexedDB|navigator\s*\.\s*credentials|PasswordCredential", re.I)
PAGE_LINE_RE = re.compile(r"^(\d+): .*$")
# The label ends in a URL or `(url)`, and a URL holds no spaces, so a title cannot fake this ending.
SELECTED_LINE_RE = re.compile(r"^(\d+): .* \[selected\](?: isolatedContext=\S*)?$")
PAGE_SECTIONS = {"## Pages", "## Extension Pages", "## Extension Service Workers"}
RECONNECT_NOTE = "the browser was restarted or reconnected"


def mode(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    value = env.get(KEY)
    if not value:
        try:
            for line in env_file(env).read_text(encoding="utf-8-sig").splitlines():
                name, _, raw = line.partition("=")
                if name.strip() == KEY:
                    value = raw.strip().strip("\"'")
        except (OSError, UnicodeDecodeError):
            value = None
    value = (value or "").lower()
    return value if value in PROFILES else "off"


def set_mode(value: str, env: Mapping[str, str] | None = None) -> Path:
    path = env_file(env)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    lines = [line for line in lines if line.partition("=")[0].strip() != KEY]
    if value != "off":
        lines.append(f"{KEY}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def profile_dir(name: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base).joinpath(*PROFILES[name])


SANDBOX_REFUSAL = ("PM_OS_TEST_SANDBOX=1 is set, so the owner's real Brave profile is off limits and nothing attached "
                   "to it. Point LOCALAPPDATA at an empty folder or use a stub server.")


def sandbox_blocks_owner(name: str, env: Mapping[str, str] | None = None) -> bool:
    """A test never reaches the owner's real Brave: a shared test folder would raise its Allow dialog."""
    env = os.environ if env is None else env
    if env.get("PM_OS_TEST_SANDBOX") != "1":
        return False
    target = profile_dir(name, env)
    bases = [Path.home() / "AppData" / "Local", real_local_appdata()]  # a test may redirect HOME but not the shell
    return any(base and _same_path(target, base.joinpath(*PROFILES[name])) for base in bases)


def child_command(name: str, version: str, env: Mapping[str, str] | None = None) -> list[str]:
    """chrome-devtools-mcp attaches through the permission dialog, with auth headers redacted."""
    return [shutil.which("npx") or "npx", "--yes", f"chrome-devtools-mcp@{version}", "--autoConnect",
            f"--userDataDir={profile_dir(name, env)}", "--redactNetworkHeaders", "--pageIdRouting",
            "--no-usage-statistics", "--no-performance-crux"]


def agent_dir(env: Mapping[str, str] | None = None) -> Path:
    """AppData, not local/: a file-sync client would sync a live profile's lock files, databases, and cookies to every device."""
    env = os.environ if env is None else env
    if env.get("PM_OS_AGENT_BROWSER_DIR"):
        return Path(env["PM_OS_AGENT_BROWSER_DIR"])
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "pm-os" / "agent-brave"


def agent_port(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        return int(env.get("PM_OS_AGENT_BROWSER_PORT") or AGENT_PORT)
    except ValueError:
        return AGENT_PORT


def agent_child_command(version: str, env: Mapping[str, str] | None = None) -> list[str]:
    """chrome-devtools-mcp connects on its first call, after the proxy has proved the port is the agent profile's."""
    return [shutil.which("npx") or "npx", "--yes", f"chrome-devtools-mcp@{version}",
            f"--browserUrl=http://127.0.0.1:{agent_port(env)}", "--redactNetworkHeaders", "--pageIdRouting",
            "--no-usage-statistics", "--no-performance-crux"]


def browser_exe(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    if env.get("PM_OS_AGENT_BROWSER_EXE"):
        path = Path(env["PM_OS_AGENT_BROWSER_EXE"])
        return path if path.exists() else None
    tail = ("BraveSoftware", "Brave-Browser", "Application", "brave" + ".exe")
    for base in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("LOCALAPPDATA")):
        if base and Path(base).joinpath(*tail).exists():
            return Path(base).joinpath(*tail)
    return None


class AgentBrowser:
    """The agent profile's own Brave. Started minimized and inactive, proved by its port file, never the owner's."""

    def __init__(self, env: Mapping[str, str] | None = None, listener=None, uses_dir=None, started=None):
        self.env = os.environ if env is None else env
        self.dir, self.port = agent_dir(self.env), agent_port(self.env)
        self.record_path = state_dir(self.env) / f"agent-browser-{machine_name(self.env)}.json"
        self.listener = listener or tcp_listeners
        self.uses_dir = uses_dir or process_uses_dir
        self.started = started or process_started
        self.verified: tuple[int, int | None] | None = None
        self.checked = 0.0

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def owner_pid(self) -> int | None:
        """The process listening on 127.0.0.1 at the port, only when it is this profile's Brave.

        Brave writes DevToolsActivePort only for port 0, so the proof is the listener's command line. The slow
        command-line read is cached by process id and start time together, because Windows reuses ids.
        """
        pid = next((pid for address, pid in self.listener(self.port) if address == "127.0.0.1"), None)
        if pid is None:
            return None
        key = (pid, self.started(pid))
        if key[1] is None or key != self.verified:
            if not self.uses_dir(pid, self.dir):
                return None
            self.verified = key
        return pid

    def _version(self, timeout: float = 2) -> dict | None:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"{self.url()}/json/version", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def endpoint(self) -> str | None:
        """The browser socket, only when the program on the port is this profile's Brave."""
        if not self.owner_pid():
            return None
        version = self._version()
        socket_url = str((version or {}).get("webSocketDebuggerUrl") or "")
        return socket_url if socket_url.startswith(f"ws://127.0.0.1:{self.port}/devtools/browser/") else None

    def running(self) -> bool:
        """Brave holds `lockfile` open while the profile runs and the system deletes it when that ends."""
        lock = self.dir / "lockfile"
        if not lock.exists():
            return False
        try:
            lock.unlink()
            return False  # a leftover, nothing holds it
        except PermissionError:
            return True
        except OSError:
            return False

    def port_taken(self) -> bool:
        return bool(self.listener(self.port))

    def _record(self) -> dict:
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
            return record if isinstance(record, dict) else {}
        except (OSError, ValueError):
            return {}

    def _prepare(self) -> None:
        """A profile marked as crashed or holding a saved session reopens windows that ignore the minimized start."""
        prefs = self.dir / "Default" / "Preferences"
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            profile = data.setdefault("profile", {})
            profile["exit_type"], profile["exited_cleanly"] = "Normal", True
            data.setdefault("session", {})["restore_on_startup"] = 5  # the new tab page, never the last session
            prefs.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError, AttributeError):
            pass
        shutil.rmtree(self.dir / "Default" / "Sessions", ignore_errors=True)

    def ensure(self, start: bool = True) -> str | None:
        """None when the agent profile answers on its port, else the refusal to give the agent."""
        if time.monotonic() - self.checked < AGENT_CHECK_SECONDS:
            return None
        if self.endpoint():
            self.checked = time.monotonic()
            self.keep_minimized()
            return None
        if not start:
            return AGENT_DOWN
        if self.running():  # Brave holds its lock file a moment before its port answers: another session is starting it
            deadline = time.monotonic() + AGENT_START_SECONDS
            while time.monotonic() < deadline:
                if self.endpoint():
                    self.checked = time.monotonic()
                    return None
                if not self.running():
                    break  # it exited before answering, so this session starts it below
                time.sleep(0.2)
            else:
                return AGENT_DOWN  # running but never answering: a stuck instance, which `agent stop` clears
        lock = self.record_path.with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + AGENT_START_SECONDS
        while True:  # two sessions starting it at once would open a second window in the first instance
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - lock.stat().st_mtime > AGENT_START_SECONDS * 2:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if self.endpoint():
                    self.checked = time.monotonic()
                    return None
                if time.monotonic() > deadline:
                    return AGENT_DOWN
                time.sleep(0.2)
        try:
            return self._start(deadline)
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    def _start(self, deadline: float) -> str | None:
        if self.endpoint():
            self.checked = time.monotonic()
            return None
        if self.running():
            return AGENT_DOWN
        if self.port_taken():
            log_event(self.env, decision="agent-port-taken", profile="agent")
            return AGENT_PORT_TAKEN
        exe = browser_exe(self.env)
        if not exe:
            return AGENT_NO_BRAVE
        self.dir.mkdir(parents=True, exist_ok=True)
        self._prepare()
        before = foreground_window()
        command = [str(exe), f"--user-data-dir={self.dir}", f"--remote-debugging-port={self.port}",
                   "--remote-debugging-address=127.0.0.1", *AGENT_FLAGS, "about:blank"]
        proc = start_minimized(command)
        log_event(self.env, decision="agent-started", profile="agent", pid=proc.pid)
        try:
            self.record_path.write_text(json.dumps({"pid": proc.pid, "port": self.port, "started": time.time()}),
                                        encoding="utf-8")
        except OSError:
            pass
        answered = None
        while time.monotonic() < deadline:
            guard_focus(proc.pid, before)
            if self.endpoint():
                answered = time.monotonic()
                break
            if proc.poll() is not None and not self.running():
                return AGENT_DOWN
            time.sleep(0.1)
        if answered is None:
            return AGENT_DOWN
        while time.monotonic() - answered < 2:  # late popups: a first-run bubble, a restored window
            guard_focus(proc.pid, before)
            time.sleep(0.1)
        self.checked = time.monotonic()
        return None

    def pid(self) -> int | None:
        return self.owner_pid()

    def keep_minimized(self) -> None:
        pid = self.pid()
        if pid:
            guard_focus(pid, None)

    def stop(self, wait: float = 15) -> str:
        endpoint = self.endpoint()
        if not endpoint and not self.running():
            return "not running"
        if endpoint:
            try:
                cdp_send(endpoint, "Browser.close")
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not self.running():
                return "stopped"
            time.sleep(0.2)
        pid = self._record().get("pid")
        if isinstance(pid, int) and self.uses_dir(pid, self.dir):
            end_tree(pid)
            return "stopped (forced)"
        return "still running; it is not the instance this script started, so it was left alone"

    def status(self) -> dict:
        return {"profile_dir": str(self.dir), "port": self.port, "running": self.running(),
                "answers": bool(self.endpoint()), "port_taken": self.port_taken()}


def tcp_listeners(port: int) -> list[tuple[str, int]]:
    """Every (local address, process id) IPv4 listener on a port, from the Windows TCP table. No subprocess.

    Every row, because Windows lets another program listen on 0.0.0.0 at the same port as Brave's 127.0.0.1.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    size = wintypes.DWORD(0)
    get = ctypes.windll.iphlpapi.GetExtendedTcpTable
    get(None, ctypes.byref(size), False, 2, 3, 0)  # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if get(buf, ctypes.byref(size), False, 2, 3, 0) == 0:
            break
    else:
        return []
    count = int.from_bytes(buf.raw[:4], "little")
    found = []
    for i in range(count):
        row = buf.raw[4 + i * 24: 4 + (i + 1) * 24]  # state, local addr, local port, remote addr, remote port, pid
        local_port = int.from_bytes(row[8:12], "little") & 0xFFFF
        if socket.ntohs(local_port) == port:
            found.append((socket.inet_ntoa(row[4:8]), int.from_bytes(row[20:24], "little")))
    return found


def process_started(pid: int) -> int | None:
    """The process's creation time, so a reused process id is never mistaken for the one already checked."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(wintypes.HANDLE(handle), *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def real_local_appdata() -> Path | None:
    """The signed-in user's real AppData\\Local from the shell, which no environment variable can redirect."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_byte * 16)]
    folder = GUID()
    ctypes.memmove(folder.data, uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le, 16)  # LocalAppData
    path = ctypes.c_wchar_p()
    shell = ctypes.windll.shell32
    if shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def foreground_window() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow() or None


def guard_focus(pid: int, before: int | None) -> bool:
    """Minimize every visible top-level window of the agent browser, and hand focus back if it took it."""
    if sys.platform != "win32" or not pid:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            found.append(hwnd)
        return True
    user32.EnumWindows(visit, 0)
    took = False
    focus = user32.GetForegroundWindow()
    focus_pid = wintypes.DWORD()
    if focus:
        user32.GetWindowThreadProcessId(focus, ctypes.byref(focus_pid))
    for hwnd in found:
        if not user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
    if focus_pid.value == pid:
        took = True
        for hwnd in found:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE hands activation to the next window
        if before:
            user32.SetForegroundWindow(before)
    return took


def start_minimized(command: list[str]) -> subprocess.Popen:
    """Minimized and never activated, detached so it outlives the session that started it."""
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  cwd=str(Path.home()))
    if sys.platform != "win32":
        return subprocess.Popen(command, start_new_session=True, **common)
    info = subprocess.STARTUPINFO()
    info.dwFlags |= 1  # STARTF_USESHOWWINDOW
    info.wShowWindow = SW_SHOWMINNOACTIVE
    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, never DETACHED_PROCESS (test_no_window.py). Brave is a
    # GUI program, so neither console flag changes how it opens; any console child of it gets a hidden console.
    flags = NO_WINDOW | 0x00000200
    try:
        return subprocess.Popen(command, startupinfo=info, creationflags=flags | 0x01000000, **common)
    except OSError:
        return subprocess.Popen(command, startupinfo=info, creationflags=flags, **common)


def process_uses_dir(pid: int, folder: Path) -> bool:
    """A process id alone is never trusted: Windows reuses them. Its command line must name the agent profile."""
    if sys.platform != "win32":
        return False
    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
    try:
        line = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True,
                              timeout=20, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return names_dir(line, folder)


def names_dir(command_line: str, folder: Path) -> bool:
    """The exact folder, so a sibling such as agent-brave-old never passes for agent-brave."""
    wanted = re.escape(str(folder).rstrip("\\/"))
    return bool(re.search(rf'--user-data-dir="?{wanted}[\\/]?"?(?:\s|$)', command_line, re.I))


def cdp_send(socket_url: str, method: str, params: dict | None = None, timeout: float = 5) -> None:
    """One DevTools command over a bare WebSocket, standard library only. Used for Browser.close."""
    from urllib.parse import urlparse
    target = urlparse(socket_url)
    if target.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("the agent browser is local only")
    sock = socket.create_connection((target.hostname, target.port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.sendall((f"GET {target.path} HTTP/1.1\r\nHost: {target.hostname}:{target.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                raise ValueError("no handshake")
            reply += chunk
        if b" 101 " not in reply.split(b"\r\n", 1)[0]:
            raise ValueError("handshake refused")
        payload = json.dumps({"id": 1, "method": method, "params": params or {}}).encode("utf-8")
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x81, 0x80 | size]) if size < 126 else bytes([0x81, 0x80 | 126]) + size.to_bytes(2, "big")
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        try:
            sock.recv(4096)  # the answer, or the close as the browser exits
        except OSError:
            pass
    finally:
        sock.close()


class NullLease:
    """The agent profile has no lease: it is the agents' own, and each session sees only its own tabs."""

    def __init__(self, label: str):
        self.label = label

    def acquire(self):
        return None

    def touch(self) -> None:
        pass

    def release(self) -> None:
        pass


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PM_OS_OWNER_BROWSER_STATE"):
        return Path(env["PM_OS_OWNER_BROWSER_STATE"])
    return local_dir(env).parent / ".state"


def lease_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / f"owner-browser-lease-{machine_name(env)}.json"


def log_event(env: Mapping[str, str] | None, **fields) -> None:
    """Tool names and decisions only: never a URL or a tab title."""
    try:
        path = state_dir(env) / "owner-browser.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}) + "\n")
    except OSError:
        pass


def holder_id(env: Mapping[str, str], pid: int) -> str:
    return env.get("PM_OS_WORKER_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID") or f"proxy-{pid}"


def holder_label(env: Mapping[str, str]) -> str:
    slug = env.get("PM_OS_WORKSTREAM_SLUG")
    return f"the {slug.replace('-', ' ')} worker" if slug else "an owner tab"


def is_scheduled(env: Mapping[str, str], cwd: str) -> bool:
    if env.get("PM_OS_SCHEDULED_JOB") == "1" or env.get("CLAUDE_CODE_SESSION_ATTENDED") == "0":
        return True
    if env.get("PM_OS_WORKSTREAM_SLUG") == "scheduled-jobs":
        return True
    return "routines/scheduled-jobs" in cwd.replace("\\", "/")


def pid_alive(pid: int) -> bool:
    from lib import winlink  # os.kill(pid, 0) would terminate the process on Windows
    return winlink.pid_alive(pid)


def clock_pt(stamp: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        moment = datetime.fromtimestamp(stamp, ZoneInfo("America/Los_Angeles"))
        return moment.strftime("%I:%M %p PT").lstrip("0")
    except Exception:
        return "a moment ago"


class Lease:
    """One holder per machine. It ends at the holder's turn end, exit, or after idling."""

    def __init__(self, path: Path, holder: str, label: str, pid: int, idle: float = LEASE_IDLE_SECONDS,
                 alive=pid_alive, clock=time.time):
        self.path, self.holder, self.label, self.pid = path, holder, label, pid
        self.idle, self.alive, self.clock = idle, alive, clock

    def _locked(self, fn):
        lock = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except (FileExistsError, PermissionError):  # Windows refuses a lock file that is being deleted
                try:
                    if time.time() - lock.stat().st_mtime > 10:  # a holder died inside this short section
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("owner browser lease lock is busy") from None
                time.sleep(0.03)
        try:
            return fn()
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, record: dict) -> None:
        tmp = self.path.with_suffix(f".{self.pid}.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # a reader holds the file open for a moment on Windows
                if attempt == 5:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05)

    def _live(self, record: dict | None) -> bool:
        if not record:
            return False
        if self.clock() - record.get("last_used", 0) > self.idle:
            return False
        return bool(self.alive(int(record.get("pid", 0))))

    def acquire(self) -> dict | None:
        """Take or renew the lease. Returns None when held by us, else the other holder's record."""
        def take():
            record = self.read()
            mine = record and record.get("holder") == self.holder and record.get("pid") == self.pid
            if record and not mine and self._live(record):
                return record
            now = self.clock()
            self._write({"holder": self.holder, "label": self.label, "pid": self.pid,
                         "since": record["since"] if mine else now, "last_used": now})
            return None
        return self._locked(take)

    def touch(self) -> None:
        """Count a long call as use. It never takes a lease this holder has lost or released."""
        def renew():
            record = self.read()
            if record and record.get("holder") == self.holder and record.get("pid") == self.pid:
                record["last_used"] = self.clock()
                self._write(record)
        self._locked(renew)

    def release(self) -> None:
        def drop():
            record = self.read()
            if record and record.get("holder") == self.holder:
                self.path.unlink(missing_ok=True)
        self._locked(drop)

    def wait_message(self, record: dict) -> str:
        return (f"The owner's browser is in use by {record.get('label', 'another agent')} (since "
                f"{clock_pt(record.get('since', self.clock()))}). One agent drives it at a time. The lease frees at "
                f"that agent's turn end, or after {int(self.idle / 60)} minutes idle. Wait about a minute and retry; "
                "this is not a failure.")


def page_id(args: dict) -> int | None:
    value = args.get("pageId")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Policy:
    """What one session may do to the browser. It holds no I/O, so tests drive it directly."""

    def __init__(self):
        self.opened: set[int] = set()
        self.seen: set[int] = set()
        self.baseline = False

    def before(self, name: str, args: dict) -> tuple[bool, dict, str | None, str | None]:
        """Returns (allowed, args to forward, refusal message, the owner's words if this call carried them)."""
        args = dict(args or {})
        words = args.pop(APPROVAL_ARG, None)
        words = str(words).strip() if words else None
        if name in WHOLE_BROWSER_TOOLS:
            return False, args, WHOLE_BROWSER, words
        for key in ("url", "initScript", "function"):
            refusal = self._hard_refusal(name, key, args.get(key))
            if refusal:
                return False, args, refusal, words
        if name == "new_page":
            if not words:
                args.pop("isolatedContext", None)
            if not (words and args.get("background") is False):
                args["background"] = True
            return True, args, None, words
        if name == "list_pages":
            return True, args, None, words
        page = page_id(args)
        if name == "close_page":
            if page not in self.opened:
                return False, args, CLOSE_OTHER, words
            return True, args, None, words
        if name == "select_page" and args.get("bringToFront") and not words:
            return False, args, FOREGROUND, words
        if page is None:
            return False, args, MISSING_PAGE, words
        if page not in self.opened and not words:
            return False, args, OTHER_TAB, words
        return True, args, None, words

    @staticmethod
    def _hard_refusal(name: str, key: str, value) -> str | None:
        if not isinstance(value, str):
            return None
        if key == "url" and name in ("new_page", "navigate_page"):
            allowed = re.match(r"https?://", value, re.I) or value.strip().lower() == "about:blank"
            return None if allowed else INTERNAL_URL
        if key in ("function", "initScript") and SECRET_RE.search(value):
            return SECRET_SCRIPT
        return None

    def after(self, name: str, args: dict, result: dict, words: str | None) -> dict:
        """Track tabs from the response, then hide every tab this session did not open."""
        failed = bool(result.get("isError"))
        reconnected = self._note_reconnect(result)
        listed, selected = self._parse(result)
        if name == "new_page" and not failed:
            self._adopt(selected)
        if name == "close_page" and not failed:
            self.opened.discard(page_id(args))
        self.seen |= listed
        if listed:
            self.baseline = True
        out = result if words else self._redacted(result)
        if reconnected and isinstance(out.get("content"), list):
            out = {**out, "content": [*out["content"], {"type": "text", "text": RECONNECTED}]}
        return out

    def note_baseline(self, result: dict) -> None:
        """The tabs that exist before this session opens one, so they are never mistaken for its own."""
        self._note_reconnect(result)
        listed, _ = self._parse(result)
        self.seen |= listed
        if listed and not result.get("isError"):
            self.baseline = True

    def reconcile(self, result: dict) -> None:
        """A failed or cancelled new_page can leave its tab open with no listing: find it and own it."""
        self._note_reconnect(result)
        listed, selected = self._parse(result)
        if self.baseline and not result.get("isError"):
            self._adopt(selected)
        self.seen |= listed

    def _adopt(self, selected: int | None) -> None:
        if selected is not None and selected not in self.seen:
            self.opened.add(selected)

    def _note_reconnect(self, result: dict) -> bool:
        """Page ids change on a reconnect, so every id held so far is stale."""
        if not any(RECONNECT_NOTE in item.get("text", "") for item in self._text_items(result)):
            return False
        self.forget()
        return True

    def forget(self) -> None:
        self.opened.clear()
        self.seen.clear()
        self.baseline = False

    @staticmethod
    def _text_items(result: dict) -> list[dict]:
        content = result.get("content")
        return [c for c in content if isinstance(c, dict) and c.get("type") == "text"] if isinstance(content, list) else []

    def _parse(self, result: dict) -> tuple[set[int], int | None]:
        listed: set[int] = set()
        selected = None
        for item in self._text_items(result):
            in_pages = False
            # split on newlines only: str.splitlines also breaks on U+2028 and others, which a page title can hold
            for line in item.get("text", "").split("\n"):
                if line.startswith("## "):
                    in_pages = line.strip() == "## Pages"
                match = PAGE_LINE_RE.match(line) if in_pages else None
                if match:
                    listed.add(int(match.group(1)))
                    chosen = SELECTED_LINE_RE.match(line)
                    if chosen:
                        selected = int(chosen.group(1))
        return listed, selected

    def _redacted(self, result: dict) -> dict:
        result = dict(result)
        content = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                item = {**item, "text": self._redact_text(item.get("text", ""))}
            content.append(item)
        if "content" in result:
            result["content"] = content
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            hidden = {key: value for key, value in structured.items() if key not in ("pages", "extensionPages")}
            pages = structured.get("pages")
            if isinstance(pages, list):
                hidden["pages"] = [p for p in pages if isinstance(p, dict) and p.get("id") in self.opened]
            result["structuredContent"] = hidden
        return result

    def _redact_text(self, text: str) -> str:
        out, hidden, in_pages = [], 0, False

        def flush() -> None:
            nonlocal hidden
            if hidden:
                out.append(f"({hidden} other tab{'' if hidden == 1 else 's'} of the owner's hidden.)")
            hidden = 0
        for line in text.split("\n"):
            if line.startswith("## "):
                flush()
                in_pages = line.strip() in PAGE_SECTIONS
            match = PAGE_LINE_RE.match(line) if in_pages else None
            if match and int(match.group(1)) not in self.opened:
                hidden += 1
                continue
            out.append(line)
        flush()
        return "\n".join(out)


class Proxy:
    def __init__(self, child: list[str], env: Mapping[str, str], cwd: str, stdin, stdout, lease: Lease,
                 call_wait: float = CALL_WAIT_SECONDS, connect=None, profile: str = "owner",
                 require_need: bool = False, ensure=None, after_open=None):
        self.child_cmd, self.env, self.stdin, self.stdout = child, env, stdin, stdout
        self.connect = connect
        self.profile = profile
        self.require_need, self.need = require_need, None
        self.ensure, self.after_open = ensure, after_open
        self.announced = False
        self.init_message: dict | None = None
        self.relinks = 0
        self.lease, self.call_wait = lease, call_wait
        self.policy = Policy()
        self.scheduled = is_scheduled(env, cwd)
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending: dict = {}
        self.abandoned: set = set()
        self.internal: dict = {}
        self.internal_count = 0
        self.child_gone = False
        self.done = threading.Event()
        self.closing: dict = {}

    def _send(self, message: dict | bytes) -> None:
        data = message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode("utf-8")
        with self.write_lock:
            self.stdout.write(data)
            self.stdout.flush()

    def _refuse(self, request_id, text: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": True}})

    def _to_child(self, payload: dict | bytes) -> None:
        data = payload if isinstance(payload, bytes) else (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _ask_child_for_tabs(self, callback) -> None:
        self.internal_count += 1
        request_id = f"internal-{self.internal_count}"
        self.internal[request_id] = callback
        self._to_child({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                        "params": {"name": "list_pages", "arguments": {}}})

    def handle_client_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return self._to_child(line)
        if not isinstance(message, dict):
            return self._to_child(line)
        method = message.get("method")
        if method == "notifications/cancelled":
            params = message.get("params")
            cancelled = params.get("requestId") if isinstance(params, dict) else None
            with self.state_lock:
                entry = self.pending.pop(cancelled, None) if not isinstance(cancelled, (list, dict)) else None
            self._to_child(line)
            if entry and entry.get("name") == "new_page":  # the server drops the answer, and the tab may still open
                self._ask_child_for_tabs(self.policy.reconcile)
            return None
        if method == "initialize":
            self.init_message = message
        if method == "tools/list":
            with self.state_lock:
                self.pending[message.get("id")] = {"kind": "list"}
            return self._to_child(line)
        if method != "tools/call":
            return self._to_child(line)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = params.get("name") if isinstance(params.get("name"), str) else ""
        args = dict(params.get("arguments")) if isinstance(params.get("arguments"), dict) else {}
        need = args.pop(NEED_ARG, None)
        if self.profile == "agent":  # no override raises the agent window: background tabs, no bringToFront
            args.pop(APPROVAL_ARG, None)
        request_id = message.get("id")
        if self.scheduled:
            self._log(tool=name, decision="refused", why="scheduled")
            return self._refuse(request_id, SCHEDULED_REFUSAL)
        if self.child_gone:
            return self._refuse(request_id, CHILD_GONE)
        if self.require_need and not self.need:
            need = str(need).strip() if need else ""
            if not need:
                self._log(tool=name, decision="refused", why="no-signed-in-need", holder=self.lease.label)
                return self._refuse(request_id, NEED_MISSING)
            self.need = need
            self._log(tool=name, decision="signed-in-need", words=need, holder=self.lease.label)
        if name == "take_screenshot" and self.profile == "agent":
            self._log(tool=name, decision="refused", why="minimized", holder=self.lease.label)
            return self._refuse(request_id, AGENT_SHOT)
        allowed, forward, refusal, words = self.policy.before(name, args)
        if not allowed:
            self._log(tool=name, decision="refused", holder=self.lease.label)
            return self._refuse(request_id, refusal)
        try:
            held = self.lease.acquire()
        except (TimeoutError, OSError):
            return self._refuse(request_id, LEASE_BUSY)
        if held:
            self._log(tool=name, decision="waiting", holder=held.get("label"))
            return self._refuse(request_id, self.lease.wait_message(held))
        if self.ensure:
            problem = self.ensure()
            if problem:
                self._log(tool=name, decision="refused", why="agent-browser-down", holder=self.lease.label)
                return self._refuse(request_id, problem)
        if not self.announced:
            self.announced = True
            self._log(decision="session-profile", holder=self.lease.label)
        if words:
            self._log(tool=name, decision="owner-approved", words=words, holder=self.lease.label)
        if name == "new_page" and not self.policy.baseline:
            self._ask_child_for_tabs(self.policy.note_baseline)
        with self.state_lock:
            self.pending[request_id] = {"kind": "call", "name": name, "args": forward, "words": words,
                                        "at": time.monotonic()}
        self._to_child({**message, "params": {**params, "arguments": forward}})

    def handle_child_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return None  # a stdio server's stdout is JSON-RPC only, and raw text must not reach the client
        if not isinstance(message, dict):
            return None
        if message.get("method") == RECONNECTED_METHOD:  # another session's call found the browser reconnected
            self.policy.forget()
            return None
        if "method" in message:  # the server's own request or notification: its id is in a separate namespace
            return self._send(message)
        request_id = message.get("id")
        hashable = not isinstance(request_id, (list, dict))
        if hashable and request_id in self.closing:
            self.policy.opened.discard(self.closing.pop(request_id))
            return None
        if hashable and request_id in self.internal:
            result = message.get("result")
            callback = self.internal.pop(request_id)
            if isinstance(result, dict):
                callback(result)
            return None
        with self.state_lock:
            entry = self.pending.pop(request_id, None) if hashable else None
            late = hashable and request_id in self.abandoned
            if late:
                self.abandoned.discard(request_id)
        if not entry:
            if isinstance(message.get("result"), dict):
                message["result"] = self.policy._redacted(message["result"])
            return self._send(message)
        if entry["kind"] == "list":
            return self._send(self._with_approval_arg(message, self.require_need, approval=self.profile != "agent"))
        result = message.get("result")
        failed = "error" in message
        if isinstance(result, dict):
            text = " ".join(c.get("text", "") for c in Policy._text_items(result))
            if result.get("isError") and "Could not connect" in text:
                down = AGENT_DOWN if self.profile == "agent" else UNREACHABLE
                message["result"] = {"content": [{"type": "text", "text": down}], "isError": True}
            else:
                failed = bool(result.get("isError"))
                message["result"] = self.policy.after(entry["name"], entry["args"], result, entry["words"])
        if failed and entry["name"] == "new_page":
            self._ask_child_for_tabs(self.policy.reconcile)
        if entry["name"] == "new_page" and self.after_open:
            try:
                self.after_open()  # a tab opening must never bring the agent window up
            except Exception:
                pass
        self._touch_lease()
        return None if late else self._send(message)

    def _touch_lease(self) -> None:
        try:
            self.lease.touch()
        except (TimeoutError, OSError, AttributeError):
            pass

    def _log(self, **fields) -> None:
        log_event(self.env, profile=self.profile, **fields)

    @staticmethod
    def _with_approval_arg(message: dict, need: bool = False, approval: bool = True) -> dict:
        for tool in (message.get("result") or {}).get("tools") or []:
            schema = tool.get("inputSchema")
            if need and isinstance(schema, dict) and tool.get("name") not in WHOLE_BROWSER_TOOLS:
                schema.setdefault("properties", {})[NEED_ARG] = {
                    "type": "string",
                    "description": "Required on this session's first call: why the task needs the owner's own "
                                   "signed-in Brave, naming the brief or CoS line that says so. Logged. Tasks that "
                                   "need no sign-in use the mcp__chrome-devtools__ agent profile instead.",
                }
            if not approval or tool.get("name") in ("close_page", *WHOLE_BROWSER_TOOLS) or not isinstance(schema, dict):
                continue
            schema.setdefault("properties", {})[APPROVAL_ARG] = {
                "type": "string",
                "description": "The owner's words approving this call, needed only to reach a tab this session "
                               "did not open or to bring a page forward. Logged.",
            }
        return message

    def _watch_calls(self) -> None:
        """No call hangs the agent: an unanswered Allow dialog or a stuck page gets a plain answer."""
        renewed = time.monotonic()
        while not self.done.wait(1.0):
            try:
                calls = []
                with self.state_lock:
                    for request_id, entry in self.pending.items():
                        if entry.get("kind") != "call":
                            continue
                        calls.append(request_id)
                        if time.monotonic() - entry["at"] > self.call_wait and request_id not in self.abandoned:
                            self.abandoned.add(request_id)
                            self._refuse(request_id, CALL_PENDING)
                if calls and time.monotonic() - renewed > LEASE_RENEW_SECONDS:
                    renewed = time.monotonic()
                    self._touch_lease()
            except Exception:
                continue

    def _safely(self, handler, line: bytes) -> None:
        """One bad line must not end either loop, which would hang every later call."""
        try:
            handler(line)
        except Exception as exc:
            try:
                request_id = json.loads(line).get("id")
            except (ValueError, AttributeError):
                return
            if request_id is not None:
                self._refuse(request_id, f"The browser proxy could not handle that call ({type(exc).__name__}). "
                                         "Its outcome is unknown.")

    def _relink(self) -> bool:
        """A broker that ended (a crash, an upgrade, `set off`) costs one retry, never the rest of the session."""
        if not self.connect or self.proc.pid or self.done.is_set() or self.relinks >= RELINKS:
            return False
        self.relinks += 1
        with self.state_lock:
            pending, self.pending = self.pending, {}
            abandoned = set(self.abandoned)
            self.abandoned.clear()
        for request_id, entry in pending.items():  # their outcome is unknown, and the answers are gone
            if request_id in abandoned:
                continue
            if entry.get("kind") == "call":
                self._refuse(request_id, CHILD_GONE)
            else:
                self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": CHILD_GONE}})
        self.internal.clear()
        self.closing.clear()
        self.policy.forget()
        try:
            link = self.connect()
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            self._log(decision="broker-relink-failed", why=type(exc).__name__)
            return False
        self.proc = link
        if self.init_message:
            self.internal["relink-init"] = lambda result: None
            self._to_child({**self.init_message, "id": "relink-init"})
        self._log(decision="broker-relinked")
        return True

    def _read_child(self) -> None:
        while True:
            for line in self.proc.stdout:
                self._safely(self.handle_child_line, line)
            if not self._relink():
                break
        self.child_gone = True
        with self.state_lock:
            pending, self.pending = self.pending, {}
            abandoned = set(self.abandoned)
        self.closing.clear()
        for request_id, entry in pending.items():
            if request_id in abandoned:
                continue
            if entry.get("kind") == "call":
                self._refuse(request_id, CHILD_GONE)
            else:
                self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": CHILD_GONE}})

    def _unanswered(self) -> bool:
        with self.state_lock:
            return bool(self.pending)

    def shutdown(self) -> None:
        """Close the tabs this session left open, then end the server. In attach mode it only disconnects."""
        deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
        while self._unanswered() and not self.child_gone and time.monotonic() < deadline:  # a new_page may be landing
            time.sleep(0.05)
        deadline = time.monotonic() + SHUTDOWN_CLOSE_SECONDS
        asked, count = set(), 0
        while not self.child_gone and time.monotonic() < deadline:
            for page in self.policy.opened - asked:
                asked.add(page)
                count += 1
                request_id = f"shutdown-{count}"
                self.closing[request_id] = page
                self._to_child({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                "params": {"name": "close_page", "arguments": {"pageId": page}}})
            if not self.closing and not (self.policy.opened - asked):
                break
            time.sleep(0.05)
        self.done.set()
        try:
            self.lease.release()
        except (TimeoutError, OSError):
            pass  # the record's dead pid frees it for the next acquire
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            if self.proc.pid:  # a broker link has none: the shared connection outlives this session
                end_tree(self.proc.pid)
        if self.proc.stdout:
            threading.Thread(target=self.proc.stdout.close, daemon=True).start()

    def run(self) -> int:
        self.proc = None
        if self.connect:
            try:
                self.proc = self.connect()
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                self._log(decision="broker-unavailable", why=type(exc).__name__)
        if self.proc is None:  # this session gets its own connection, which costs its own Allow click
            self.proc = subprocess.Popen(self.child_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=dict(self.env))
        threading.Thread(target=self._read_child, daemon=True).start()
        threading.Thread(target=self._watch_calls, daemon=True).start()
        try:
            for line in self.stdin:
                self._safely(self.handle_client_line, line)
        finally:
            self.shutdown()
        return 0


def broker_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / f"owner-browser-broker-{machine_name(env)}.json"


def code_stamp(script: Path | str | None = None) -> str:
    """A changed script or server version gets a fresh broker, at the cost of one Allow click."""
    try:
        return hashlib.sha256(Path(script or __file__).resolve().read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


def broker_stale(record: dict, version: str) -> bool:
    """Replace a broker only when it is out of date: another server version, or its own script changed since it started.

    A broker from another checkout whose script is unchanged is shared, never replaced. Two checkouts each replacing
    the other's broker drops the owner's approved connection, and every
    replacement costs an Allow click. The broker only carries lines, so any current copy serves every session.
    """
    if record.get("version") != version:
        return True
    if record.get("code") == code_stamp():
        return False
    script = record.get("script")
    return not script or code_stamp(script) != record.get("code")


class BrokerLink:
    """The proxy's side of the shared connection. It looks like the child process to Proxy."""

    class _Writer:
        def __init__(self, sock: socket.socket):
            self.sock = sock
            self.lock = threading.Lock()  # two threads may write at once, and lines must stay whole

        def write(self, data: bytes) -> None:
            with self.lock:
                self.sock.sendall(data)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            try:
                self.sock.shutdown(socket.SHUT_WR)  # the broker drops this session; the connection stays
            except OSError:
                pass

    class _Reader:
        def __init__(self, file):
            self.file = file

        def __iter__(self):
            while True:
                try:
                    line = self.file.readline()
                except (OSError, ValueError):
                    return
                if not line:
                    return
                yield line

        def close(self) -> None:
            try:
                self.file.close()
            except OSError:
                pass

    pid = None

    def __init__(self, sock: socket.socket, reader):
        self.sock = sock
        self.stdin = self._Writer(sock)
        self.stdout = self._Reader(reader)

    def poll(self):
        return None

    def wait(self, timeout: float | None = None) -> int:
        try:
            self.sock.close()
        except OSError:
            pass
        return 0


def _broker_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        int(record["port"]), str(record["token"])
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _broker_hello(record: dict, extra: dict | None = None) -> tuple[socket.socket, object] | None:
    """Connect and prove both sides: the token goes out, and only the real broker answers ok."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(record["port"])), timeout=3)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        sock.sendall((json.dumps({"token": str(record["token"]), **(extra or {})}) + "\n").encode("utf-8"))
        reader = sock.makefile("rb")
        reply = json.loads(reader.readline() or b"{}")
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise ValueError("not the broker")
        sock.settimeout(None)
        return sock, reader
    except (OSError, ValueError):
        try:
            sock.close()
        except OSError:
            pass
        return None


def _broker_connect(path: Path, version: str | None = None) -> BrokerLink | None:
    record = _broker_record(path)
    if not record:
        return None
    if version is not None and broker_stale(record, version):
        stop_broker(path=path)
        return None
    opened = _broker_hello(record)
    return BrokerLink(*opened) if opened else None


def _same_path(a, b) -> bool:
    return str(a).replace("/", "\\").rstrip("\\").lower() == str(b).replace("/", "\\").rstrip("\\").lower()


def check_broker_profile(record: dict | None, env: Mapping[str, str]) -> None:
    """Join only a broker that serves this session's profile. The other one is left running, never stopped.

    Raises RuntimeError, so the proxy uses its own connection to its own profile instead. Under
    PM_OS_TEST_SANDBOX a broker that names no profile is never joined either.
    """
    if not record:
        return
    name = mode(env)
    if name == "off":
        return
    served = record.get("profile")
    if served and not _same_path(served, profile_dir(name, env)):
        raise RuntimeError("the shared connection serves another browser profile")
    if not served and env.get("PM_OS_TEST_SANDBOX") == "1":
        raise RuntimeError("a test never joins a shared connection that names no profile")


def start_detached(command: list[str]) -> subprocess.Popen:
    """The broker must outlive the session that started it, or the next session pays another Allow."""
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  cwd=str(Path.home()))  # never hold a worktree folder open
    if sys.platform != "win32":
        return subprocess.Popen(command, start_new_session=True, **common)
    # A hidden console (CREATE_NO_WINDOW, a new group), never DETACHED_PROCESS: Windows ignores
    # CREATE_NO_WINDOW beside it, and a console-less broker opens a window per console child.
    flags = NO_WINDOW | 0x00000200  # CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(command, creationflags=flags | 0x01000000, **common)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        return subprocess.Popen(command, creationflags=flags, **common)


def connect_broker(version: str, env: Mapping[str, str] | None = None,
                   command: list[str] | None = None, wait: float = BROKER_START_SECONDS) -> BrokerLink:
    """Join the shared connection, starting the broker when none answers."""
    env = os.environ if env is None else env
    path = broker_path(env)
    check_broker_profile(_broker_record(path), env)
    link = _broker_connect(path, version)
    if link:
        return link
    lock = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lock.stat().st_mtime > wait:  # a starter died holding it
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            link = _broker_connect(path, version)  # another session is starting it
            if link:
                return link
            if time.monotonic() > deadline:
                raise TimeoutError("owner browser broker did not start")
            time.sleep(0.2)
    try:
        link = _broker_connect(path, version)
        if link:
            return link
        proc = start_detached(command or [sys.executable, str(Path(__file__).resolve()), "broker",
                                          "--mcp-version", version])
        while time.monotonic() < deadline:
            link = _broker_connect(path, version)
            if link:
                return link
            if proc.poll() is not None:
                raise RuntimeError(f"owner browser broker exited with {proc.returncode}")
            time.sleep(0.1)
        raise TimeoutError("owner browser broker did not start")
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


class Broker:
    """The one chrome-devtools-mcp every session shares, so the browser asks Allow once per launch.

    Each proxy connects over a localhost socket with the token from the state file. Request ids are
    renumbered so sessions never see each other's answers. The proxy still enforces the policy and
    the lease; the broker only carries lines. No thread ever blocks on a write while holding the
    routing lock: the child and each session have their own writer. It ends when its server ends,
    when the server will not start, or on an authenticated shutdown (`set off`).
    """

    SESSION_BACKLOG = 2000

    def __init__(self, child: list[str], env: Mapping[str, str], path: Path, token: str | None = None,
                 version: str = "0"):
        self.child_cmd, self.env, self.path, self.version = child, env, path, version
        self.token = token or secrets.token_hex(16)
        self.lock = threading.Lock()  # routes and the id counter only
        self.routes: dict = {}
        self.clients: dict = {}  # socket -> outbound queue
        self.count = 0
        self.to_child: queue.Queue = queue.Queue()
        self.init_result = None
        self.init_done = threading.Event()
        self.init_lock = threading.Lock()
        self.gone = threading.Event()

    def _child_send(self, message: dict) -> None:
        self.to_child.put((json.dumps(message) + "\n").encode("utf-8"))

    def _write_child(self) -> None:
        while not self.gone.is_set():
            try:
                data = self.to_child.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.gone.set()
                return

    def _client_send(self, client: socket.socket, message: dict) -> None:
        outbox = self.clients.get(client)
        if outbox is None:
            return
        if outbox.qsize() > self.SESSION_BACKLOG:  # a session that stopped reading must not stall the rest
            self._drop(client)
            return
        outbox.put((json.dumps(message) + "\n").encode("utf-8"))

    def _write_client(self, client: socket.socket, outbox: queue.Queue) -> None:
        while True:
            data = outbox.get()
            if data is None:
                return
            try:
                client.sendall(data)
            except OSError:
                self._drop(client)
                return

    def _drop(self, client: socket.socket) -> None:
        outbox = self.clients.pop(client, None)
        if outbox is not None:
            outbox.put(None)
        with self.lock:
            for key in [k for k, (c, _) in self.routes.items() if c is client]:
                self.routes.pop(key, None)
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _initialize(self, params: dict) -> dict | None:
        """The first session's initialize starts the server, without roots, and every later one reuses it."""
        with self.init_lock:
            if self.init_result is None:
                if self.init_done.is_set():
                    return None
                self._child_send({"jsonrpc": "2.0", "id": "broker-init", "method": "initialize",
                                  "params": {**params, "capabilities": {}}})
                if not self.init_done.wait(120) or self.init_result is None:
                    self.gone.set()  # the next session starts a fresh broker
                    return None
                self._child_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return self.init_result

    def _read_child(self) -> None:
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:  # the server asks its client something: no session should answer it
                    self._child_send({"jsonrpc": "2.0", "id": message["id"],
                                      "error": {"code": -32601, "message": "not supported"}})
                    continue
                for client in list(self.clients):
                    self._client_send(client, message)
                continue
            request_id = message.get("id")
            if request_id == "broker-init":
                self.init_result = message.get("result") if isinstance(message.get("result"), dict) else None
                self.init_done.set()
                continue
            with self.lock:
                route = self.routes.pop(request_id, None) if isinstance(request_id, int) else None
            owner = route[0] if route else None
            if RECONNECT_NOTE in json.dumps(message.get("result") or {}):  # every other session's ids are stale
                for other in list(self.clients):
                    if other is not owner:
                        self._client_send(other, {"jsonrpc": "2.0", "method": RECONNECTED_METHOD})
            if route:
                self._client_send(owner, {**message, "id": route[1]})
        self.gone.set()

    def _serve_client(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            reader = client.makefile("rb")
            hello = json.loads(reader.readline() or b"{}")
            client.settimeout(None)
            if not isinstance(hello, dict) or not secrets.compare_digest(str(hello.get("token", "")), self.token):
                client.close()
                return
            client.sendall(b'{"ok": true}\n')
            if hello.get("shutdown") is True:
                log_event(self.env, decision="broker-stop-asked", pid=os.getpid())
                self.gone.set()
                client.close()
                return
        except (OSError, ValueError):
            try:
                client.close()
            except OSError:
                pass
            return
        outbox: queue.Queue = queue.Queue()
        self.clients[client] = outbox
        threading.Thread(target=self._write_client, args=(client, outbox), daemon=True).start()
        try:
            for line in reader:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                if method == "initialize":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    result = self._initialize(params)
                    reply = ({"result": result} if result is not None else
                             {"error": {"code": -32603, "message": "the browser tools did not start"}})
                    self._client_send(client, {"jsonrpc": "2.0", "id": message.get("id"), **reply})
                elif method == "notifications/cancelled":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    with self.lock:
                        mine = [k for k, (c, o) in self.routes.items() if c is client and o == params.get("requestId")]
                    if mine:
                        self._child_send({**message, "params": {**params, "requestId": mine[0]}})
                elif method and "id" in message:
                    with self.lock:
                        self.count += 1
                        self.routes[self.count] = (client, message["id"])
                        number = self.count
                    self._child_send({**message, "id": number})
        except (OSError, ValueError):
            pass
        finally:
            self._drop(client)
            try:
                client.close()
            except OSError:
                pass

    def serve(self) -> int:
        flags = NO_WINDOW if sys.platform == "win32" else 0  # a console window would steal focus
        self.proc = subprocess.Popen(self.child_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=dict(self.env), creationflags=flags)
        threading.Thread(target=self._read_child, daemon=True).start()
        threading.Thread(target=self._write_child, daemon=True).start()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        server.settimeout(1.0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is owner-only
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"port": server.getsockname()[1], "token": self.token, "pid": os.getpid(),
                                 "version": self.version, "code": code_stamp(), "started": time.time(),
                                 "script": str(Path(__file__).resolve()),
                                 "profile": next((part.split("=", 1)[1] for part in self.child_cmd
                                                  if part.startswith("--userDataDir=")), None)}))
        os.replace(tmp, self.path)
        log_event(self.env, decision="broker-started", pid=os.getpid())
        try:
            while not self.gone.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()
        finally:
            server.close()
            try:
                if json.loads(self.path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    self.path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            for client in list(self.clients):
                self._drop(client)
            if self.proc.poll() is None:
                end_tree(self.proc.pid)
            log_event(self.env, decision="broker-ended", pid=os.getpid())
        return 0


def stop_broker(env: Mapping[str, str] | None = None, path: Path | None = None) -> bool:
    """Ask the broker to end over its own socket. A process id alone is never trusted: Windows reuses them."""
    path = path or broker_path(env)
    record = _broker_record(path)
    if not record:
        return False
    opened = _broker_hello(record, {"shutdown": True})
    if opened:
        try:
            opened[0].close()
        except OSError:
            pass
    path.unlink(missing_ok=True)  # an unreachable record is stale either way
    return bool(opened)


def end_tree(pid: int) -> None:
    """Ends the server's own process tree. The browser is not in it: the owner started that."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def make_lease(env: Mapping[str, str] | None = None, pid: int | None = None) -> Lease:
    env = os.environ if env is None else env
    pid = os.getpid() if pid is None else pid
    return Lease(lease_path(env), holder_id(env, pid), holder_label(env), pid)


def release(session: str | None, env: Mapping[str, str] | None = None) -> bool:
    """Free the lease if this session holds it. A quiet no-op on a machine that never opted in."""
    env = os.environ if env is None else env
    if mode(env) == "off":
        return False
    lease = make_lease(env)
    record = lease.read()
    if not record:
        return False
    if session and record.get("holder") != session:
        return False
    lease.holder = record.get("holder", "")
    lease.release()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mode")
    setter = sub.add_parser("set")
    setter.add_argument("value", choices=[*PROFILES, "off"])
    sub.add_parser("status")
    releaser = sub.add_parser("release")
    releaser.add_argument("--session")
    proxy = sub.add_parser("proxy")
    proxy.add_argument("--profile", choices=["agent", "owner"], default="agent")
    proxy.add_argument("--mcp-version", required=True)
    broker = sub.add_parser("broker")
    broker.add_argument("--mcp-version", required=True)
    agent = sub.add_parser("agent")
    agent.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args(argv)
    if args.command == "mode":
        print(mode())
        return 0
    if args.command == "set":
        print(set_mode(args.value))
        if args.value == "off" and stop_broker():
            print("stopped the shared browser connection")
        return 0
    if args.command == "status":
        lease = make_lease()
        held = lease.read()
        live = lease._live(held)
        record = _broker_record(broker_path())
        opened = _broker_hello(record) if record else None
        shared = bool(opened)
        if opened:
            opened[0].close()
        print(json.dumps({"mode": mode(), "held": live, "by": held.get("label") if live else None,
                          "shared_connection": shared, "agent": AgentBrowser().status()}))
        return 0
    if args.command == "release":
        print("released" if release(args.session) else "not held")
        return 0
    name = mode()
    if name == "off":
        print(f"owner_browser.py {args.command}: no owner browser on this machine", file=sys.stderr)
        return 2
    if args.command == "agent":
        browser = AgentBrowser()
        if args.action == "start":
            problem = browser.ensure()
            print(problem or f"agent browser answers on {browser.url()}")
            return 1 if problem else 0
        if args.action == "stop":
            print(browser.stop())
            return 0
        print(json.dumps(browser.status()))
        return 0
    if (args.command == "broker" or args.profile == "owner") and sandbox_blocks_owner(name):
        log_event(None, decision="refused", why="test-sandbox", profile="owner", command=args.command)
        print(f"owner_browser.py {args.command}: {SANDBOX_REFUSAL}", file=sys.stderr)
        return 2
    if args.command == "broker":
        return Broker(child_command(name, args.mcp_version), os.environ, broker_path(),
                      version=args.mcp_version).serve()
    call_wait = float(os.environ.get("PM_OS_OWNER_BROWSER_CALL_WAIT") or CALL_WAIT_SECONDS)
    if args.profile == "agent":
        browser = AgentBrowser()
        return Proxy(agent_child_command(args.mcp_version), os.environ, os.getcwd(), sys.stdin.buffer,
                     sys.stdout.buffer, NullLease(holder_label(os.environ)), call_wait=call_wait, profile="agent",
                     ensure=browser.ensure, after_open=browser.keep_minimized).run()
    return Proxy(child_command(name, args.mcp_version), os.environ, os.getcwd(), sys.stdin.buffer,
                 sys.stdout.buffer, make_lease(), call_wait=call_wait, profile="owner", require_need=True,
                 connect=lambda: connect_broker(args.mcp_version)).run()


if __name__ == "__main__":
    try:  # a console-less start must not open a window per child process (no_window.py)
        import no_window
        no_window.install()
    except ImportError:
        pass
    sys.exit(main())
