#!/usr/bin/env python3
"""Doctor for the Windows (wmux) install: the pieces verify-kit-install.sh's
syntax and test checks cannot see. One line per check: ok, warn (fix it), or
owner (needs a sign-in or a choice only the owner can make).

    python3 system/scripts/check-windows-install.py [--json] [--strict]

--strict exits 1 when any warn line prints. Built from a Windows install audit.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import comms_triage_setting  # noqa: E402
import repo_paths  # noqa: E402

ROOT = repo_paths.main_checkout(HERE.parents[1])
STATE_HOME = Path(os.environ.get("SWITCHBOARD_STATE_HOME") or Path.home() / ".switchboard")
EXPECTED_TASKS = ("com.switchboard.command-center", "com.switchboard.background-tasks", "com.switchboard.log-rotate",
                  "com.switchboard.amp-mem-weekly-distill", "com.switchboard.note-links",
                  "com.switchboard.local-backup", "com.switchboard.cos-autostart")
# Same value install-claude-code.sh merges into ~/.claude/settings.json.
MCP_TIMEOUT_MS = 60000
# Two 30-minute background ticks: a file still old this long after a wake is a real miss.
WAKE_GRACE_HOURS = 1
results: list[dict[str, str]] = []


def add(level: str, name: str, detail: str) -> None:
    results.append({"level": level, "check": name, "detail": detail})


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def same_dir(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


def check_env_file() -> None:
    env_file = STATE_HOME / "env"
    if not env_file.exists():
        return add("warn", "env file", f"{env_file} missing (rerun install.py --update, then install.ps1)")
    keys = {}
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip()
    root = keys.get("SWITCHBOARD_ROOT", "")
    if not same_dir(root, ROOT):
        add("warn", "env SWITCHBOARD_ROOT", f"records {root or '(none)'}, not {ROOT}; scheduled tasks read the wrong folder "
            f"(fix the line in {env_file}; a test install must set SWITCHBOARD_STATE_HOME)")
    else:
        add("ok", "env SWITCHBOARD_ROOT", root)
    missing = [k for k in ("SWITCHBOARD_PYTHON", "SWITCHBOARD_TOOL_PATH", "WMUX_BIN") if not keys.get(k)]
    if missing:
        add("warn", "env tool keys", f"{', '.join(missing)} missing, so scheduled tasks lose python, git or wmux "
            "(rerun: powershell -File system\\windows\\install.ps1 -SkipSeed)")
    else:
        add("ok", "env tool keys", "SWITCHBOARD_PYTHON, SWITCHBOARD_TOOL_PATH, WMUX_BIN recorded")


def check_tasks() -> None:
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return add("warn", "scheduled tasks", "powershell not found")
    script = ("Get-ScheduledTask -TaskPath '\\switchboard\\' -ErrorAction SilentlyContinue | ForEach-Object { "
              "$i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{n=$_.TaskName; s=[string]$_.State; r=$i.LastTaskResult} } "
              "| ConvertTo-Json -Compress")
    proc = run([ps, "-NoProfile", "-Command", script], timeout=90)
    try:
        rows = json.loads(proc.stdout) if proc and proc.stdout.strip() else []
    except json.JSONDecodeError:
        rows = []
    rows = [rows] if isinstance(rows, dict) else rows
    have = {r["n"]: r for r in rows}
    missing = [t for t in EXPECTED_TASKS if t not in have]
    if missing:
        add("warn", "scheduled tasks", f"not registered: {', '.join(missing)} "
            "(powershell -File system\\windows\\register-scheduled-tasks.ps1)")
    else:
        add("ok", "scheduled tasks", f"{len(EXPECTED_TASKS)} switchboard tasks registered")


def check_background_log() -> None:
    log = ROOT / "system" / "logs" / "background-tasks.log"
    if not log.exists():
        return add("warn", "background tasks", f"{log} missing; the background loop has never run")
    cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 6 * 3600))
    errors = [l for l in log.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
              if "ERROR" in l and l[:16] >= cutoff]
    if errors:
        add("warn", "background tasks", f"{len(errors)} errors in 6 h, last: {errors[-1][:160]}")
    else:
        add("ok", "background tasks", "no errors in the last 6 hours")


def check_clis() -> None:
    for tool, hint in (("node", "winget install OpenJS.NodeJS.LTS"), ("npx", "winget install OpenJS.NodeJS.LTS"),
                       ("uv", "irm https://astral.sh/uv/install.ps1 | iex"), ("bun", "winget install Oven-sh.Bun"),
                       ("sqlite3", "winget install SQLite.SQLite"), ("ruby", "winget install RubyInstallerTeam.Ruby.3.3")):
        add("ok" if shutil.which(tool) else "warn", f"cli {tool}", shutil.which(tool) or f"missing ({hint})")
    gh = shutil.which("gh")
    if not gh:
        add("warn", "cli gh", "missing (bash system/scripts/install-all.sh installs the official zip into ~/.local/bin)")
    else:
        proc = run([gh, "auth", "status"], timeout=30)
        signed = bool(proc and proc.returncode == 0)
        add("ok" if signed else "owner", "cli gh", f"{gh}; " + ("signed in" if signed else "not signed in (gh auth login)"))


def check_mcp() -> None:
    cfg = Path.home() / ".claude.json"
    try:
        servers = json.loads(cfg.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, json.JSONDecodeError):
        return add("warn", "claude mcp", f"{cfg} unreadable")
    cd = servers.get("chrome-devtools")
    args = " ".join(cd.get("args", [])) if isinstance(cd, dict) else ""
    headless = servers.get("chrome-devtools-headless")
    headless_args = " ".join(headless.get("args", [])) if isinstance(headless, dict) else ""
    sanctioned = lambda text: "chrome-devtools-mcp/chrome-profile" in text and "--headless=true" in text
    owner = owner_browser_mode()
    if not cd:
        add("warn", "mcp chrome-devtools", "not configured (bash system/scripts/install-claude-code.sh)")
    elif owner != "off":
        signed_in = servers.get("chrome-devtools-owner")
        signed_in_args = " ".join(signed_in.get("args", [])) if isinstance(signed_in, dict) else ""
        if "owner_browser.py" in args and "--profile agent" in args:
            add("ok", "mcp chrome-devtools", f"the agent {owner} profile, through the proxy")
        elif "owner_browser.py" in args:
            add("warn", "mcp chrome-devtools", f"still the owner's own {owner}; the agent profile is the default "
                "now: rerun install-claude-code.sh, which replaces it")
        else:
            add("warn", "mcp chrome-devtools", f"this machine's rule is {owner} through owner_browser.py, but the "
                "entry bypasses it; rerun install-claude-code.sh, which replaces it")
        if "owner_browser.py" in signed_in_args and "--profile owner" in signed_in_args:
            add("ok", "mcp chrome-devtools-owner", f"the owner's signed-in {owner}, through the proxy")
        else:
            add("warn", "mcp chrome-devtools-owner", "missing or bypasses owner_browser.py, so tasks that need the "
                "owner's sign-in have no browser; rerun install-claude-code.sh")
        if sanctioned(headless_args):
            add("ok", "mcp chrome-devtools-headless", "sanctioned headless profile, for scheduled jobs")
        else:
            add("warn", "mcp chrome-devtools-headless", "missing, so scheduled jobs have no browser; "
                "rerun install-claude-code.sh")
    elif sanctioned(args):
        add("ok", "mcp chrome-devtools", "sanctioned headless profile")
    else:
        add("warn", "mcp chrome-devtools", f"not the sanctioned headless profile ({args[:80]}); "
            "rerun install-claude-code.sh, which replaces it")
    if owner == "off" and "chrome-devtools-owner" in servers:
        add("warn", "mcp chrome-devtools-owner", "registered, but this machine never opted in to the owner's browser; "
            "rerun install-claude-code.sh, which removes it")
    limit_ms = mcp_timeout_ms()
    add("ok" if limit_ms >= MCP_TIMEOUT_MS else "warn", "mcp startup limit", f"{limit_ms / 1000:.0f} s" + (
        "" if limit_ms >= MCP_TIMEOUT_MS else
        f"; tabs restarting together need {MCP_TIMEOUT_MS / 1000:.0f} s (rerun install-claude-code.sh)"))
    if isinstance(cd, dict) and cd.get("command"):
        ok, detail = mcp_handshake(cd, limit_ms / 1000)
        add("ok" if ok else "warn", "mcp chrome-devtools connection", detail)


def owner_browser_mode() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import owner_browser
        return owner_browser.mode()
    except Exception:
        return "off"


def mcp_timeout_ms() -> int:
    """The limit a new Claude session uses: the process env first, then user settings, then Claude's 30 s."""
    value = os.environ.get("MCP_TIMEOUT")
    if value is None:
        try:
            env = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")).get("env") or {}
        except (OSError, json.JSONDecodeError):
            env = {}
        value = env.get("MCP_TIMEOUT")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 30000


def mcp_handshake(spec: dict, limit_seconds: float) -> tuple[bool, str]:
    """Start the configured server and run initialize and tools/list, as a Claude session does.

    chrome-devtools-mcp starts Chrome only on its first tool call, so this holds no
    browser and takes no Chrome lease. The whole process tree is ended afterwards.
    """
    command = shutil.which(spec["command"]) or spec["command"]
    env = {**os.environ, **(spec.get("env") or {})}
    started = time.monotonic()
    try:
        proc = subprocess.Popen([command, *(spec.get("args") or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"could not start {spec['command']}: {exc}"
    lines: queue.Queue = queue.Queue()

    def read_lines() -> None:  # Windows cannot select() on a pipe, so a thread reads it.
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def send(payload: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        proc.stdin.flush()

    def answer(request_id: int) -> dict | None:
        while True:
            try:
                line = lines.get(timeout=max(0.0, limit_seconds - (time.monotonic() - started)))
            except queue.Empty:
                raise TimeoutError from None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    try:
        send({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "check-windows-install", "version": "1"}}})
        if not (answer(1) or {}).get("result"):
            return False, f"exited or refused initialize after {time.monotonic() - started:.1f} s"
        send({"method": "notifications/initialized"})
        send({"id": 2, "method": "tools/list", "params": {}})
        tools = ((answer(2) or {}).get("result") or {}).get("tools") or []
        if not tools:
            return False, f"connected but listed no tools after {time.monotonic() - started:.1f} s"
        return True, f"connected in {time.monotonic() - started:.1f} s, {len(tools)} tools, limit {limit_seconds:.0f} s"
    except TimeoutError:
        return False, f"no answer within {limit_seconds:.0f} s"
    except OSError as exc:
        return False, f"exited during the handshake after {time.monotonic() - started:.1f} s: {exc}"
    finally:
        stop_tree(proc)
        reader.join(5)
        for pipe in (proc.stdin, proc.stdout if not reader.is_alive() else None):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


def stop_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":  # npx starts cmd, node and the server: end all of them.
        run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=30)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def check_connectors() -> None:
    switchboard = shutil.which("switchboard")
    if not switchboard:
        return add("warn", "connectors", "switchboard not on PATH (install.ps1 adds system\\connectors)")
    proc = run([switchboard, "status"], timeout=60)
    text = (proc.stdout if proc else "") or ""
    for line in text.splitlines():
        name = line.split(":", 1)[0].strip()
        if not name or "directory" in name:
            continue
        good = "ready" in line or " ok" in line
        if name == "dropbox":
            check_dropbox(switchboard, good)
            continue
        if name == "slack" and not good and not comms_triage_setting.enabled():
            add("ok", "connector slack", "not used: Comms Triage is off on this machine")
            continue
        add("ok" if good else "owner", f"connector {name}", line.split(":", 1)[-1].strip()[:120])
    # Same rule as system/connectors/switchboard-shim.py connectors_dir().
    token = Path(os.environ.get("SWITCHBOARD_CONNECTORS_DIR") or Path.home() / ".switchboard" / "connectors") / "google-token.json"
    try:
        scopes = json.loads(token.read_text(encoding="utf-8")).get("scopes") or []
    except (OSError, json.JSONDecodeError):
        scopes = []
    gcal = Path.home() / ".config" / "gcal-skill" / "credentials.json"
    if gcal.exists() or "https://www.googleapis.com/auth/calendar" in scopes:
        add("ok", "todo calendar holds", "calendar API token available")
    else:
        add("owner", "todo calendar holds", "no calendar token (switchboard login google)")


def last_wake_epoch() -> float | None:
    """When the machine last booted or left standby or hibernate, or None when that is over WAKE_GRACE_HOURS ago."""
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        return None
    script = ("$since = (Get-Date).AddHours(-" + str(WAKE_GRACE_HOURS) + "); "
              "$e = Get-WinEvent -FilterHashtable @{LogName='System'; Id=1,12,507; StartTime=$since} -ErrorAction SilentlyContinue "
              # PowerShell gives -and and -or equal precedence, so both pairs need parentheses.
              "| Where-Object { ($_.Id -in 12,507 -and $_.ProviderName -like 'Microsoft-Windows-Kernel-*') "
              "-or ($_.Id -eq 1 -and $_.ProviderName -eq 'Microsoft-Windows-Power-Troubleshooter') } "
              "| Select-Object -First 1; "
              "if ($e) { ([DateTimeOffset]$e.TimeCreated).ToUnixTimeSeconds() }")
    proc = run([ps, "-NoProfile", "-Command", script], timeout=60)
    try:
        return float(proc.stdout.strip()) if proc else None
    except ValueError:
        return None


def check_dropbox(switchboard: str, configured: bool) -> None:
    """No skill needs this connector: tracker links are signed-in links built from the file's path."""
    if not configured:
        add("ok", "connector dropbox", "not used: tracker links are signed-in links built without it")
        return
    proc = run([switchboard, "doctor", "dropbox"], timeout=60)
    detail = ((proc.stdout if proc else "") or "no answer").strip().split(":", 1)[-1].strip()
    add("ok", "connector dropbox", f"optional, no skill uses it: {detail}"[:120])


def check_knowledge() -> None:
    # 2 h allows three missed 30-minute background ticks (system/background-tasks.yaml;
    # the command-center tick does not build these files). An old file right after a
    # wake only means the machine was asleep.
    for name, path, hours in (("knowledge layer", ROOT / "system/state/knowledge/snapshot.json", 2),
                              ("recall index", ROOT / "system/recall/recall-index.sqlite3", 48),
                              ("knowledge graph", ROOT / "graphify-out/graph.json", 48)):
        if not path.exists():
            add("warn", name, f"{path.relative_to(ROOT)} not built yet")
            continue
        age = (time.time() - path.stat().st_mtime) / 3600
        detail = f"{path.relative_to(ROOT)}, {age:.1f} h old"
        if age <= hours:
            add("ok", name, detail)
            continue
        woke = last_wake_epoch()
        if woke is None:
            add("warn", name, f"{detail}, past its {hours} h limit with the machine awake")
        else:
            add("ok", name, f"{detail}; the machine woke {(time.time() - woke) / 60:.0f} min ago, "
                "so the next background tick rebuilds it")


def check_note_links() -> None:
    port = os.environ.get("SWITCHBOARD_NOTE_LINK_PORT") or "7472"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            up = resp.status == 200
    except OSError:
        up = False
    add("ok" if up else "warn", "note links (wmux)", f"http://127.0.0.1:{port} " + (
        "answers" if up else "not running (python3 system/scripts/mux/note-links.py ensure)"))


def check_backup() -> None:
    """In-process (no git or shell): the newest copy, its age, and the disk it takes."""
    import local_backup
    report = local_backup.health(ROOT)
    sizes = f"{report['copies']} copies, {local_backup.fmt_bytes(report['bytes_on_disk'])} on disk"
    fix = "python3 system/scripts/local_backup.py run"
    if report["status"] == "ok":
        add("ok", "backup", f"{report['reason']}; {sizes}")
    else:
        add("warn", "backup", f"{report['reason']}; {sizes} (run: {fix})")
    ignore = ROOT / ".gitignore"
    text = ignore.read_text(encoding="utf-8", errors="ignore") if ignore.exists() else ""
    if "/local/" not in text.split():
        add("warn", "backup folder", "/local/ is missing from .gitignore, so a commit could pick up the backups")


def check_comms_collectors() -> None:
    add("ok", "comms collectors", "not shipped in the kit by design (system/background-tasks.yaml header)")


def check_hook_profile() -> None:
    # Each hook is several leaked processes per tool call on Windows; a plugin reinstall undoes the profile.
    script = Path(__file__).resolve().parents[1] / "windows" / "apply-hook-profile.py"
    result = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True, check=False)
    add("ok" if result.returncode == 0 else "warn", "hook profile",
        result.stdout.strip() + ("" if result.returncode == 0 else " (python3 system/windows/apply-hook-profile.py)"))


def check_plugin_copy() -> None:
    # Claude Code runs the switchboard plugin from its install-time copy, and the version never changes,
    # so hook fixes in the repo do not reach live sessions until this copies them.
    script = Path(__file__).resolve().parent / "sync-claude-plugin.py"
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(script), "--check", "--repo", str(repo)],
                            capture_output=True, text=True, check=False)
    try:
        out = json.loads((result.stdout.strip().splitlines() or ["{}"])[-1])
    except ValueError:
        out = {}
    status = out.get("status") or "unreadable"
    if status == "behind":
        shown = ", ".join(out.get("changed", [])[:4]) + ("…" if len(out.get("changed", [])) > 4 else "")
        add("warn", "switchboard plugin copy", f"installed copy is behind the repo ({shown}) "
            "(python3 system/scripts/sync-claude-plugin.py)")
    else:
        held = out.get("held") or []
        add("ok", "switchboard plugin copy", status + (f"; held hand fixes: {', '.join(held)}" if held else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows install doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    for check in (check_env_file, check_tasks, check_background_log, check_clis, check_mcp,
                  check_connectors, check_knowledge, check_note_links, check_backup, check_comms_collectors,
                  check_hook_profile, check_plugin_copy):
        try:
            check()
        except Exception as exc:  # a broken check is a warn line, never a crash
            add("warn", check.__name__, f"check failed: {exc}")
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"  {r['level']:<5} {r['check']}: {r['detail']}")
    return 1 if args.strict and any(r["level"] == "warn" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
