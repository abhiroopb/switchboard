"""Resolve the user's local timezone for PM OS pipelines.

Resolution order:
  1. PM_OS_TZ env var (e.g., PM_OS_TZ=America/New_York)
  2. System tz from datetime.now().astimezone().tzinfo
  3. UTC as last-resort fallback

This module is imported by collectors and brief/rollup builders so they all
agree on what "local" means. Run with `PM_OS_TZ=America/Denver python3 ...`
to override per-invocation.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _resolve() -> object:
    env = os.environ.get("PM_OS_TZ")
    if env:
        try:
            return ZoneInfo(env)
        except Exception:
            pass
    sys_tz = datetime.now().astimezone().tzinfo
    if sys_tz is not None:
        return sys_tz
    return timezone.utc


LOCAL_TZ = _resolve()
