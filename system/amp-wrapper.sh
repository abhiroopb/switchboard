#!/usr/bin/env bash
# amp-wrapper.sh — Thin wrapper around the Amp CLI.
#
# Install at ~/.local/bin/amp (a symlink to this file). cmux's wrapper and
# saved restore commands already resolve that path. Never edit cmux's shim.
#
# Space cmux Amp startups by 12 seconds: one 10-second plugin readiness
# window plus headroom. The kernel lock releases even if a waiter dies.
# This is launch-rate limiting, not a plugin health check or automatic retry.

gate_start=1
case "${1:-}" in
    run|version|--version|--help|-h|config|plugins|skills|skill|mcp|files) gate_start=0 ;;
    threads)
        # These reads do not start plugin hosts; gating them would only
        # serialize concurrent refreshes and cause timeouts.
        case "${2:-}" in
            ""|--help|-h|list|l|ls|export|raw|raw-thread|markdown|md|usage|search|find) gate_start=0 ;;
        esac
        ;;
esac

if [[ -n "${CMUX_WORKSPACE_ID:-}" ]] && (( gate_start )); then
    python3 - <<'PY' || exit $?
try:
    import fcntl
except ImportError:  # Windows: flock() over msvcrt, system/scripts/flock_compat.py
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.environ.get("PM_OS_SCRIPTS_DIR") or _os.path.join(_os.environ.get("PM_OS_ROOT") or _os.path.expanduser("~/Development/pm-os"), "system", "scripts"))
    import flock_compat as fcntl
import os
from pathlib import Path
import time

state = Path.home() / ".cache/pm-os/amp-start-gate"
state.parent.mkdir(parents=True, exist_ok=True)
# Test seam; ordinary launches use 12 seconds.
interval = float(os.environ.get("PM_OS_AMP_START_INTERVAL_SECONDS", "12"))
if not 0 < interval <= 60:
    raise SystemExit("Amp launch interval must be > 0 and <= 60 seconds")
with state.open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    lock.seek(0)
    previous = lock.read().strip()
    now = time.monotonic()
    elapsed = now - float(previous) if previous else interval
    # A monotonic timestamp from before reboot can lie in the future.
    if 0 <= elapsed < interval:
        time.sleep(interval - elapsed)
    lock.seek(0)
    lock.truncate()
    lock.write(str(time.monotonic()))
    lock.flush()
PY
fi

# Use the installer-owned binary, never PATH (which would recurse through
# cmux's shim or this symlink). exec preserves the captured cmux process ID,
# terminal, original arguments, and piped startup prompt.
exec "$HOME/.amp/bin/amp" "$@"
