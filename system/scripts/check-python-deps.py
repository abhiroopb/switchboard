#!/usr/bin/env python3
"""Check that the PATH python3 can import every package in system/requirements.txt.

Why: a package manager can re-point python3 to a new interpreter that has no
PyYAML, requests or Pillow. Every command-center tick, next-up digest, the
comms fastpath, todo_cli.py and router-dispatch.sh then fail or fall back to
defaults with `ModuleNotFoundError: No module named 'yaml'`, and nothing tells
CoS. This check runs from command-center-loop.sh under the same python3 the
other scripts use, imports only the standard library, and sends one CoS line
(at most hourly) naming the missing modules and the reinstall command.

State: system/state/python-deps-latest.json (interpreter, missing list, alert
stamp). Kill switch in the loop: PYTHON_DEPS_CHECK_DISABLED=1.

    python3 system/scripts/check-python-deps.py          # JSON summary, exit 0 ok / 1 missing
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from repo_paths import main_checkout  # noqa: E402

REPO_ROOT = main_checkout(Path(os.environ.get("PYDEPS_REPO_ROOT") or SCRIPTS.parents[1]))
REQUIREMENTS = Path(os.environ.get("PYDEPS_REQUIREMENTS") or REPO_ROOT / "system" / "requirements.txt")
LATEST = Path(os.environ.get("PYDEPS_LATEST_FILE") or REPO_ROOT / "system" / "state" / "python-deps-latest.json")
REPORT_SCRIPT = Path(os.environ.get("PYDEPS_REPORT_SCRIPT") or SCRIPTS / "report-to-cos.sh")
REPEAT_AFTER = timedelta(hours=1)

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

def now() -> datetime:
    override = os.environ.get("PYDEPS_NOW")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


# --- requirements ------------------------------------------------------------------

def read_requirements(path: Path) -> list[tuple[str, str]]:
    """Return (distribution, import name) pairs from a requirements file.

    A trailing `# import: <name>` comment names the module when it differs from
    the distribution name; version specifiers and other comments are ignored.
    """
    pairs: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pairs
    for raw in lines:
        line, _, comment = raw.partition("#")
        dist = line.strip()
        if not dist:
            continue
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
            dist = dist.split(sep, 1)[0].strip()
        module = dist
        marker = "import:"
        if marker in comment:
            module = comment.split(marker, 1)[1].strip().split()[0]
        pairs.append((dist, module))
    return pairs


def missing_modules(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    out = []
    for dist, module in pairs:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append({"dist": dist, "module": module})
    return out


# --- alert -------------------------------------------------------------------------

def alert_line(missing: list[dict[str, str]], interpreter: str) -> str:
    names = ", ".join(m["module"] for m in missing)
    dists = " ".join(m["dist"] for m in missing)
    return (f"python3 ({interpreter}) cannot import {names}; command-center digest, next-up, "
            f"comms fastpath, todo_cli and router-dispatch fall back or fail until reinstalled. Run: "
            f"python3 -m pip install --break-system-packages {dists} "
            f"(or -r system/requirements.txt).")


def send_alert(text: str) -> bool:
    stamp = now().strftime("%Y%m%d-%H%M")
    cmd = ["bash", str(REPORT_SCRIPT), "--task-id", f"python-deps-{stamp}", "--origin", "cos",
           "--workstream", "pm-os-maintenance", "--status", "blocked", "--urgent", "--create-if-missing", text]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_OS_WORKER")}
    env["REPORT_TO_COS_NO_HOOKS"] = "1"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        last = (result.stdout.strip().splitlines() or [""])[-1]
        return bool(json.loads(last).get("delivered"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


# --- tick --------------------------------------------------------------------------

def load_latest() -> dict[str, Any] | None:
    try:
        data = json.loads(LATEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_latest(record: dict[str, Any]) -> None:
    try:
        LATEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = LATEST.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _os_replace(tmp, LATEST)
    except OSError as exc:
        print(f"python-deps: could not write {LATEST}: {exc}", file=sys.stderr)


def tick() -> dict[str, Any]:
    interpreter = sys.executable
    version = ".".join(str(p) for p in sys.version_info[:3])
    missing = missing_modules(read_requirements(REQUIREMENTS))
    record: dict[str, Any] = {"measured_at": iso(now()), "interpreter": interpreter, "version": version,
                              "requirements": str(REQUIREMENTS), "missing": missing, "ok": not missing}
    if missing:
        prior = (load_latest() or {}).get("alert") or {}
        # Same interpreter still broken: repeat at most hourly. A different
        # interpreter (another upgrade) is a new incident and alerts at once.
        last_sent = parse_iso(prior.get("last_sent")) if prior.get("interpreter") == interpreter else None
        text = alert_line(missing, f"{interpreter}, {version}")
        sent = False
        if last_sent is None or now() - last_sent >= REPEAT_AFTER:
            sent = send_alert(text)
            last_sent = now()
        record["alert"] = {"interpreter": interpreter, "line": text,
                           "last_sent": iso(last_sent) if last_sent else None, "delivered": sent}
    write_latest(record)
    return record


def main() -> int:
    record = tick()
    out = {k: record[k] for k in ("ok", "interpreter", "version", "missing")}
    if record.get("alert"):
        out["alert"] = {k: record["alert"][k] for k in ("last_sent", "delivered")}
    print(json.dumps(out))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
