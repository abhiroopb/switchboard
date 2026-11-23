#!/usr/bin/env python3
"""Remove expired local rollback copies from explicit switchboard locations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


HOME = Path(os.environ.get("HOME", "{{HOME}}"))


def _default_root() -> Path:
    """SWITCHBOARD_ROOT, else the git checkout around cwd, else ~/Development/switchboard.

    Scheduled runs start in the repo root without --root, and the install
    folder is the owner's choice (a synced folder on Windows), so the home
    default alone would prune the wrong tree or nothing.
    """
    env = os.environ.get("SWITCHBOARD_ROOT")
    if env:
        return Path(env).expanduser()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return HOME / "Development/switchboard"


DEFAULT_ROOT = _default_root()
PATTERNS = (
    "*.bak",
    "*.bak-*",
    "*.bak.*",
    "*.backup-*",
    "*.rollback",
    "*.rollback-*",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--now", type=float, default=time.time())
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def candidates(root: Path, cutoff: float) -> list[Path]:
    search_roots = [
        root / "system/scripts/_archive",
        root / "system/state",
        root / "system/state/todo-backups",
    ]
    found: set[Path] = set()
    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        for pattern in PATTERNS:
            for path in search_root.glob(pattern):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    found.add(path)
    return sorted(found)


def main() -> int:
    args = parse_args()
    if args.days < 1:
        raise SystemExit("--days must be at least 1")
    root = args.root.resolve()
    cutoff = args.now - args.days * 86_400
    expired = candidates(root, cutoff)
    removed: list[str] = []
    for path in expired:
        relative = str(path.relative_to(root))
        if args.apply:
            path.unlink()
        removed.append(relative)
    print(
        json.dumps(
            {
                "applied": args.apply,
                "days": args.days,
                "expired_count": len(expired),
                "files": removed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
