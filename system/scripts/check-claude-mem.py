#!/usr/bin/env python3
"""Daily health check for the claude-mem worker (Claude Code memory plugin).

switchboard runs the claude-mem worker as a supervised service with
`CLAUDE_MEM_WORKER_AUTOSTART=false`, so Claude Code hooks never spawn their
own copy. The supervisor differs by platform: launchd `com.switchboard.claude-mem`
on macOS, the systemd user unit `switchboard-claude-mem.service` on Linux, and the
Task Scheduler job `\\switchboard\\com.switchboard.claude-mem` on Windows. This script
probes the service, the worker health endpoint, and the telemetry lock, logs
the result, writes a status file, and fires a desktop notification (macOS
only) + exit 1 when any probe fails.

Usage:
    python3 system/scripts/check-claude-mem.py
    python3 system/scripts/check-claude-mem.py --quiet  # no notification

Exit codes:
    0 — worker healthy, telemetry disabled
    1 — at least one probe failed
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "system" / "scripts"))
from _local_tz import LOCAL_TZ  # noqa: E402

LOG_FILE = REPO_ROOT / "system" / "logs" / "claude-mem.log"
STATE_FILE = REPO_ROOT / ".state" / "claude-mem-status.json"
LAUNCHD_LABEL = "com.switchboard.claude-mem"
SYSTEMD_UNIT = "switchboard-claude-mem.service"
WINDOWS_TASK = r"\switchboard\com.switchboard.claude-mem"
CLAUDE_MEM_HOME = Path(os.environ.get("CLAUDE_MEM_HOME", Path.home() / ".claude-mem"))
TELEMETRY_FILE = CLAUDE_MEM_HOME / "telemetry.json"
SETTINGS_FILE = CLAUDE_MEM_HOME / "settings.json"


def current_uid() -> int:
    # Windows has no os.getuid; claude-mem falls back to 0 there as well.
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else 0


def default_port() -> int:
    # Mirrors claude-mem SettingsDefaultsManager: 37700 + (uid % 100).
    return 37700 + (current_uid() % 100)


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {msg}\n")


def worker_port() -> int:
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        return int(settings.get("CLAUDE_MEM_WORKER_PORT") or default_port())
    except (OSError, ValueError, TypeError):
        return default_port()


def probe_launchd() -> tuple[bool, str]:
    uid = current_uid()
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "launchctl timeout"
    if r.returncode != 0:
        return False, f"{LAUNCHD_LABEL} not loaded"
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("state = "):
            state = s.split("=", 1)[1].strip()
            return state == "running", f"state {state}"
    return False, "no state line in launchctl output"


def probe_systemd() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return False, "systemctl not found (install the unit with install-launchd.sh com.switchboard.claude-mem)"
    except subprocess.TimeoutExpired:
        return False, "systemctl timeout"
    state = (r.stdout or r.stderr).strip() or "unknown"
    return state == "active", f"{SYSTEMD_UNIT} {state}"


def probe_windows_task() -> tuple[bool, str]:
    # schtasks prints a CSV header plus one row: "TaskName","Next Run Time","Status".
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return False, "schtasks not found"
    except subprocess.TimeoutExpired:
        return False, "schtasks timeout"
    if r.returncode != 0:
        return False, f"task {WINDOWS_TASK} not registered (run register-scheduled-tasks.ps1)"
    row = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    status = row.rsplit(",", 1)[-1].strip().strip('"') if row else "unknown"
    return status.lower() == "running", f"task status {status}"


def probe_service() -> tuple[bool, str]:
    if sys.platform == "darwin":
        return probe_launchd()
    if sys.platform.startswith("linux"):
        return probe_systemd()
    if sys.platform in {"win32", "cygwin", "msys"}:
        return probe_windows_task()
    return False, f"unsupported platform {sys.platform}"


def restart_hint() -> str:
    if sys.platform == "darwin":
        return f"launchctl kickstart -k gui/$(id -u)/{LAUNCHD_LABEL}"
    if sys.platform.startswith("linux"):
        return f"systemctl --user restart {SYSTEMD_UNIT}"
    return f'schtasks /Run /TN "{WINDOWS_TASK}"'


def probe_health(port: int) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read(2000).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return False, f"{url} unreachable: {e.reason}"
    except OSError as e:
        return False, f"{url} error: {e}"
    try:
        data = json.loads(body)
    except ValueError:
        return False, f"non-JSON body: {body[:120]}"
    status = str(data.get("status", "")).lower()
    if status in {"ok", "healthy"}:
        return True, f"{url} {status}"
    return False, f"{url} status={status or 'missing'}"


def probe_telemetry() -> tuple[bool, str]:
    try:
        data = json.loads(TELEMETRY_FILE.read_text())
    except OSError:
        return False, f"{TELEMETRY_FILE} missing"
    except ValueError:
        return False, f"{TELEMETRY_FILE} not JSON"
    if data.get("enabled") is False:
        return True, "telemetry disabled"
    return False, f"telemetry enabled={data.get('enabled')!r}"


def probe_autostart_off() -> tuple[bool, str]:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return False, f"{SETTINGS_FILE} unreadable"
    val = str(data.get("CLAUDE_MEM_WORKER_AUTOSTART", "")).lower()
    if val == "false":
        return True, "autostart off"
    return False, f"CLAUDE_MEM_WORKER_AUTOSTART={val or 'unset'} (hooks may spawn a second worker)"


def notify_failure(failures: list[tuple[str, str]]) -> None:
    bullets = ", ".join(f"{label} ({reason[:60]})" for label, reason in failures)
    title = "switchboard: claude-mem check failed"
    body = f"{bullets}. Run `{restart_hint()}`."
    if sys.platform != "darwin":
        # No portable desktop notifier; the stderr line and exit 1 carry the result.
        return
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{body}" with title "{title}" sound name "Submarine"'],
        check=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="suppress desktop notification on failure")
    args = ap.parse_args()

    if not SETTINGS_FILE.exists():
        # claude-mem was never installed (install-claude-code.sh --memory builtin,
        # the kit default). Nothing to probe; the daily background task stays quiet.
        print(f"claude-mem: not installed ({SETTINGS_FILE} missing); built-in Claude Code memory in use")
        return 0

    port = worker_port()
    probes = [
        ("service", probe_service),
        ("health", lambda: probe_health(port)),
        ("telemetry", probe_telemetry),
        ("autostart", probe_autostart_off),
    ]

    results = []
    failures = []
    for label, fn in probes:
        passed, detail = fn()
        results.append({"probe": label, "ok": passed, "detail": detail})
        status = "✓" if passed else "✗"
        log(f"{status} {label}: {detail}")
        if not passed:
            failures.append((label, detail))

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "checked_at": datetime.now(LOCAL_TZ).isoformat(),
        "all_ok": not failures,
        "port": port,
        "results": results,
    }, indent=2))

    if failures:
        if not args.quiet:
            notify_failure(failures)
        print("claude-mem: ✗ " + ", ".join(f"{l}({d[:60]})" for l, d in failures), file=sys.stderr)
        return 1
    print(f"claude-mem: ✓ all {len(results)} ok (" + ", ".join(r["detail"] for r in results) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
