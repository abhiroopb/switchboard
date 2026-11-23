#!/usr/bin/env python3
"""Live check of the owner-browser proxy. It prints counts and the test page's title, never the owner's tabs.

  owner_browser_live_check.py agent      the agent profile: no dialog, no focus change, stays minimized,
                                         example.com and a second public page load, the owner's Brave is untouched,
                                         and a signed-in task reaches the owner's Brave only after it states why
  owner_browser_live_check.py attach     needs the owner's Allow click: list, open example.com in the
                                         background, read its title, try the guards, close it, compare
  owner_browser_live_check.py closed     browser unreachable, simulated with an empty profile folder
  owner_browser_live_check.py headless   the scheduled-job fallback on the sanctioned profile

Exit 0 means every check held.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "owner_browser.py"
VERSION = "1.9.0"
TEST_URL = "https://example.com"
SECOND_URL = "https://www.iana.org/help/example-domains"  # a second real site: a public page with no personal data
SECOND_MARK = "Example Domains"
BOT_WALL = re.compile(r"press\s*&?\s*hold|access to this page has been denied|are you a human|captcha|px-captcha", re.I)
NEED = "live check of the owner-profile route"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {label}{(' ' + detail) if detail else ''}", flush=True)
    if not ok:
        failures.append(label)


class Client:
    def __init__(self, command: list[str], env: dict):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, text=True, encoding="utf-8", errors="replace")
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next = 0
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "live-check", "version": "0"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _write(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None, timeout=90):
        self.next += 1
        self._write({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params or {}})
        end = time.monotonic() + timeout
        while True:
            line = self.lines.get(timeout=max(0.1, end - time.monotonic()))
            if line is None:
                raise RuntimeError("server exited")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == self.next:
                return message

    def tool(self, name, timeout=90, **args):
        result = self.rpc("tools/call", {"name": name, "arguments": args}, timeout).get("result") or {}
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return text, bool(result.get("isError"))

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)


def listed(text: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^(\d+): ", text.split("## Pages", 1)[-1], re.M)] if "## Pages" in text else []


def hidden(text: str) -> int:
    match = re.search(r"\((\d+) other tabs? of the owner's hidden", text)
    return int(match.group(1)) if match else 0


def browser_root() -> tuple[int | None, int]:
    """Root process id and visible window count of the owner's browser. No titles."""
    script = ("$b=Get-CimInstance Win32_Process -Filter \"Name='brave.exe'\"|?{$_.CommandLine -notlike '*switchboard\\agent-brave*'};"
              "$i=@{};$b|%{$i[$_.ProcessId]=1};"
              "$r=($b|?{-not $i[$_.ParentProcessId]}|select -First 1).ProcessId;"
              "$p=@{};$b|%{$p[[int]$_.ProcessId]=1};"
              "$w=(Get-Process brave -ErrorAction SilentlyContinue|?{$_.MainWindowHandle -ne 0 -and $p[$_.Id]}|measure).Count;\"$r $w\"")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.split()
    return (int(out[0]) if out and out[0].isdigit() else None, int(out[1]) if len(out) > 1 else 0)


def proxy_env(**extra) -> dict:
    env = {**os.environ, "SWITCHBOARD_OWNER_BROWSER": "brave", "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
           "SWITCHBOARD_OWNER_BROWSER_CALL_WAIT": "180", **extra}  # three minutes for the Allow click
    env.pop("SWITCHBOARD_SCHEDULED_JOB", None)
    return env


def sandbox_env(folder: str) -> dict:
    """A test owner session: sandboxed, its own state, and an empty folder where the owner's profile would be."""
    cache = str(Path(os.environ["LOCALAPPDATA"]) / "npm-cache")  # an empty npx cache would download for minutes
    return proxy_env(SWITCHBOARD_TEST_SANDBOX="1", SWITCHBOARD_OWNER_BROWSER_STATE=folder, LOCALAPPDATA=folder,
                     npm_config_cache=cache)


def proxy_command(profile: str = "owner") -> list[str]:
    return [sys.executable, str(SCRIPT), "proxy", "--profile", profile, "--mcp-version", VERSION]


def screen_ready() -> tuple[bool, str]:
    """An Allow dialog on a locked or sleeping screen is never clicked."""
    class LastInput(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint), ("time", ctypes.c_uint)]
    info = LastInput(ctypes.sizeof(LastInput), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    idle = (ctypes.windll.kernel32.GetTickCount() - info.time) / 1000
    locked = "LogonUI.exe" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq LogonUI.exe"], capture_output=True,
                                            text=True).stdout
    if locked:
        return False, "the screen is locked or asleep"
    if idle > 300:
        return False, f"no keyboard or mouse input for {int(idle / 60)} minutes"
    return True, ""


def attach() -> None:
    ready, why = screen_ready()
    if not ready and os.environ.get("SWITCHBOARD_ATTACH_ANYWAY") != "1":
        check("owner is at the screen to click Allow", False, f"{why}: no dialog raised")
        return
    foreground = ctypes.windll.user32.GetForegroundWindow
    root, windows = browser_root()
    print(f"browser running: {bool(root)}, windows: {windows}", flush=True)
    client = Client(proxy_command(), proxy_env())
    started = time.monotonic()
    text, failed = client.tool("list_pages", timeout=200, signedInNeed=NEED)
    check("attach through the Allow dialog", not failed, f"{time.monotonic() - started:.1f}s")
    if failed:
        print(text[:300])
        client.close()
        return
    before = hidden(text)
    check("listing shows none of the owner's tabs", listed(text) == [], f"{before} hidden")
    focus = foreground()
    text, failed = client.tool("new_page", url=TEST_URL, background=False)  # the proxy must force this to the background
    own = listed(text)
    check("opened one tab", not failed and len(own) == 1)
    check("new_page output hides the owner's tabs", hidden(text) == before, f"{hidden(text)} hidden")
    check("focus did not move on open, even when asked for foreground", foreground() == focus)
    title, failed = client.tool("evaluate_script", function="() => document.title", pageId=own[0])
    match = re.search(r'"(Example[^"]*)"', title)
    check("read the test page title", not failed and bool(match), match.group(1) if match else "")
    text, failed = client.tool("evaluate_script", function="() => document.cookie", pageId=own[0])
    check("script reading cookies refused", failed and "secrets" in text)
    text, failed = client.tool("navigate_page", url="brave://settings/passwords", pageId=own[0])
    check("internal page refused", failed and "off limits" in text)
    other = next(i for i in range(1, 500) if i not in own)
    text, failed = client.tool("take_snapshot", pageId=other)
    check("page tool on another tab refused", failed and "not a tab this session opened" in text)
    text, failed = client.tool("close_page", pageId=other)
    check("closing another tab refused", failed and "no override" in text)
    second = Client(proxy_command(), proxy_env())
    text, failed = second.tool("list_pages", timeout=30, signedInNeed=NEED)
    check("a second agent waits, with no dialog", failed and "One agent drives it at a time" in text)
    print("  wait message:", text, flush=True)
    second.close()
    text, failed = client.tool("close_page", pageId=own[0])
    check("closed our tab", not failed)
    text, failed = client.tool("list_pages")
    check("owner's tab count unchanged", hidden(text) == before, f"{hidden(text)} hidden")
    check("focus did not move on close", foreground() == focus)
    client.close()
    status = json.loads(subprocess.run([sys.executable, str(SCRIPT), "status"], capture_output=True, text=True,
                                       env=proxy_env()).stdout)
    check("lease freed on exit", status["held"] is False)
    check("shared connection outlives the session", status.get("shared_connection") is True)
    later = Client(proxy_command(), proxy_env())
    started = time.monotonic()
    text, failed = later.tool("list_pages", timeout=30, signedInNeed=NEED)
    check("a new session reuses the approved connection, no second Allow",
          not failed and time.monotonic() - started < 15, f"{time.monotonic() - started:.1f}s")
    check("new session sees none of the owner's tabs", listed(text) == [] and hidden(text) == before)
    later.close()
    after_root, after_windows = browser_root()
    check("browser still running, same process and windows", after_root == root and after_windows == windows)


def agent_windows() -> list[str]:
    """'minimized' or 'shown' for each visible top-level window of the agent profile's Brave."""
    sys.path.insert(0, str(SCRIPT.parent))
    import owner_browser
    pid = owner_browser.AgentBrowser().owner_pid()
    if not pid:
        return []
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindow.restype = wintypes.HWND
    states: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            states.append("minimized" if user32.IsIconic(hwnd) else "shown")
        return True
    user32.EnumWindows(visit, 0)
    return states


def agent() -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    import owner_browser
    foreground = ctypes.windll.user32.GetForegroundWindow
    print("stop:", subprocess.run([sys.executable, str(SCRIPT), "agent", "stop"], capture_output=True, text=True,
                                  env=proxy_env()).stdout.strip(), flush=True)  # a cold start is the case to prove
    root, windows = browser_root()
    print(f"owner's Brave running: {bool(root)}, windows: {windows}", flush=True)
    focus = foreground()
    client = Client(proxy_command("agent"), proxy_env())
    started = time.monotonic()
    text, failed = client.tool("list_pages", timeout=60)
    check("agent profile starts and answers with no dialog", not failed, f"{time.monotonic() - started:.1f}s")
    if failed:
        print(text[:300])
        client.close()
        return
    check("focus did not move when the agent browser started", foreground() == focus)
    check("agent browser starts minimized", agent_windows() and set(agent_windows()) == {"minimized"},
          str(agent_windows()))
    status = owner_browser.AgentBrowser().status()
    check("debugging port bound to 127.0.0.1 only", {a for a, _ in owner_browser.tcp_listeners(status["port"])} == {"127.0.0.1"},
          f"port {status['port']}")
    text, failed = client.tool("new_page", url=TEST_URL, background=False)
    own = listed(text)
    check("opened example.com in the agent profile", not failed and len(own) >= 1)
    page = int(re.search(r"^(\d+): .*\[selected\]", text, re.M).group(1)) if not failed else 0
    check("focus did not move on open, even when asked for foreground", foreground() == focus)
    title, failed = client.tool("evaluate_script", function="() => document.title", pageId=page)
    match = re.search(r'"(Example[^"]*)"', title)
    check("read the test page title", not failed and bool(match), match.group(1) if match else "")
    snap, failed = client.tool("take_snapshot", pageId=page)
    link = re.search(r"uid=(\S+) link", snap)
    started = time.monotonic()
    text, failed = client.tool("click", uid=link.group(1), pageId=page, timeout=60) if link else ("no link", True)
    check("a click works in the minimized window", not failed and "clicked" in text, f"{time.monotonic() - started:.1f}s")
    check("focus did not move on click", foreground() == focus)
    text, failed = client.tool("select_page", pageId=page, bringToFront=True, ownerApproved="invented words")
    check("bringing the agent window forward is refused, even with words", failed and foreground() == focus)
    text, failed = client.tool("take_screenshot", pageId=page)
    check("screenshot refused with a plain reason, never a hang", failed and "minimized" in text)
    text, failed = client.tool("evaluate_script", function="() => document.cookie", pageId=page)
    check("script reading cookies refused", failed and "secrets" in text)
    second = Client(proxy_command("agent"), proxy_env())
    text, failed = second.tool("new_page", url=TEST_URL, timeout=60)
    check("a second agent works at the same time, with no lease wait", not failed)
    second.close()
    started = time.monotonic()
    before_tabs = set(listed(client.tool("list_pages")[0]))
    text, failed = client.tool("new_page", url=SECOND_URL, timeout=90)  # a slow load can report a timeout
    after_tabs = set(listed(client.tool("list_pages")[0]))
    second_page = max(after_tabs - before_tabs, default=0)
    time.sleep(5)
    body, failed = client.tool("evaluate_script", pageId=second_page, function=(
        "() => [document.title, document.body ? document.body.innerText.slice(0, 1500) : '']"))
    walled = bool(BOT_WALL.search(body))
    title = re.search(r'\["([^"]*)"', body)
    check("a second public site loads in the agent profile with no bot wall", not failed and not walled and SECOND_MARK in body,
          (title.group(1) if title else body[:120].replace("\n", " "))[:120] + f" ({time.monotonic() - started:.1f}s)")
    check("agent browser still minimized after opening tabs", set(agent_windows()) == {"minimized"},
          str(agent_windows()))
    check("focus did not move during the agent session", foreground() == focus)
    for tab in (page, second_page):
        client.tool("close_page", pageId=tab)
    client.close()
    after_root, after_windows = browser_root()
    check("owner's Brave untouched: same process and windows", after_root == root and after_windows == windows)
    # The owner route runs in its own state folder, so a test broker from this checkout never replaces the
    # owner's approved live shared connection.
    # Each leg has its own state folder, so the empty-profile leg can never reuse a broker that points at the
    # owner's real profile and raise an Allow dialog.
    # Both legs run under SWITCHBOARD_TEST_SANDBOX against empty profiles, so owner_browser.py itself refuses the real one.
    with tempfile.TemporaryDirectory() as refused_state, tempfile.TemporaryDirectory() as empty_state:
        sandbox_real = proxy_env(SWITCHBOARD_TEST_SANDBOX="1", SWITCHBOARD_OWNER_BROWSER_STATE=refused_state)
        guard = subprocess.run(proxy_command("owner"), input="", capture_output=True, text=True, env=sandbox_real,
                               timeout=60)
        check("under SWITCHBOARD_TEST_SANDBOX the owner proxy exits before reaching the real Brave",
              guard.returncode == 2 and "SWITCHBOARD_TEST_SANDBOX" in guard.stderr and not owner_browser.broker_path(
                  sandbox_real).exists())
        refused, empty = (sandbox_env(state) for state in (refused_state, empty_state))
        owner = Client(proxy_command("owner"), refused)
        text, failed = owner.tool("list_pages", timeout=30)
        check("owner profile refuses a call that states no signed-in need, before any attach",
              failed and owner_browser.NEED_ARG in text)
        owner.close()
        owner_browser.stop_broker(refused)
        # With the owner's profile replaced by an empty folder there is no browser to ask Allow, so a routed
        # call comes back "not reachable" from the owner's Brave channel, never from the agent profile.
        owner = Client(proxy_command("owner"), empty)
        text, failed = owner.tool("list_pages", timeout=200, signedInNeed=NEED)
        check("a signed-in task routes to the owner's Brave channel (empty profile, so no dialog)",
              failed and "owner's browser is not reachable" in text)
        owner.close()
        owner_browser.stop_broker(empty)
        tail = [line for folder in (refused_state, empty_state) if (Path(folder) / "owner-browser.jsonl").exists()
                for line in (Path(folder) / "owner-browser.jsonl").read_text(encoding="utf-8").splitlines()]
    check("the owner session logged its profile and its stated need",
          any('"profile": "owner"' in l and '"signed-in-need"' in l for l in tail)
          and any('"profile": "owner"' in l and "no-signed-in-need" in l for l in tail))
    log = owner_browser.state_dir() / "owner-browser.jsonl"
    tail = log.read_text(encoding="utf-8").splitlines()[-60:]
    check("the agent sessions logged their profile",
          any('"profile": "agent"' in l and "session-profile" in l for l in tail))
    check("focus did not move during the whole check", foreground() == focus)


def closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = Client(proxy_command(), sandbox_env(tmp))
        text, failed = client.tool("list_pages", timeout=60, signedInNeed=NEED)
        check("no profile: plain unreachable message", failed and "not reachable" in text and "ask-cos.sh" in text)
        client.close()
        profile = Path(tmp).joinpath("BraveSoftware", "Brave-Browser", "User Data")
        profile.mkdir(parents=True)
        (profile / "DevToolsActivePort").write_text("1\n/devtools/browser/invented\n", encoding="utf-8")
        client = Client(proxy_command(), sandbox_env(tmp))
        text, failed = client.tool("list_pages", timeout=60, signedInNeed=NEED)
        check("stale port file: plain unreachable message", failed and "not reachable" in text)
        print("  message:", text, flush=True)
        client.close()


def headless() -> None:
    profile = Path.home() / ".cache" / "chrome-devtools-mcp" / "chrome-profile"
    def run(data_dir: Path) -> tuple[bool, str]:
        client = Client([shutil.which("npx") or "npx", "--yes", f"chrome-devtools-mcp@{VERSION}", "--headless=true",
                         f"--userDataDir={data_dir}", "--no-usage-statistics", "--no-performance-crux"],
                        {**os.environ, "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"})
        try:
            text, failed = client.tool("new_page", url=TEST_URL, background=True, timeout=90)
            if failed:
                return False, text[:160]
            page = re.search(r"^(\d+): .*\[selected\]", text, re.M)
            title, failed = client.tool("evaluate_script", function="() => document.title", pageId=int(page.group(1)))
            return (not failed and "Example" in title), "title read"
        finally:
            client.close()
    ok, detail = run(profile)
    if not ok and "already running" in detail:
        print("sanctioned profile is busy with a live session, which stays undisturbed; repeating on a throwaway profile")
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = run(Path(tmp))
            check("headless fallback launches and reads a page (throwaway profile)", ok, detail)
        return
    check("headless fallback launches and reads a page (sanctioned profile)", ok, detail)


if __name__ == "__main__":
    # Exactly one known mode word, nothing else: an extra flag such as `attach --help` must never
    # start a live attach and raise Brave's Allow dialog unannounced.
    modes = {"agent": agent, "attach": attach, "closed": closed, "headless": headless}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        print(__doc__)
        sys.exit(2)
    modes[sys.argv[1]]()
    print("RESULT", "all held" if not failures else f"{len(failures)} failed: {failures}")
    sys.exit(1 if failures else 0)
