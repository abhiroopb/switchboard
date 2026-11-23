#!/usr/bin/env python3
"""Read the switchboard dispatch-surface registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
REGISTRY_FILE = Path(
    os.environ.get("SURFACES_CONFIG_FILE")
    or REPO_ROOT / "system" / "config" / "surfaces.json"
)


def load_registry() -> dict[str, Any]:
    try:
        payload = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read surface registry {REGISTRY_FILE}: {error}") from error
    if not isinstance(payload.get("surfaces"), dict):
        raise ValueError(f"surface registry {REGISTRY_FILE} has no surfaces map")
    return payload


def normalize_origin(payload: dict[str, Any], value: str) -> str:
    default = str(payload.get("default_origin") or "cos")
    candidate = value.strip().lower()
    surfaces = payload["surfaces"]
    if candidate in surfaces and surfaces[candidate].get("dispatch_origin") is True:
        return candidate
    return default


def print_value(value: Any) -> None:
    if isinstance(value, bool):
        print(str(value).lower())
    elif value is not None:
        print(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("origin", nargs="?", default="")

    get = subparsers.add_parser("get")
    get.add_argument("origin")
    get.add_argument("field")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--where", default="")
    args = parser.parse_args()

    try:
        payload = load_registry()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "normalize":
        print(normalize_origin(payload, args.origin))
        return 0
    if args.command == "get":
        origin = normalize_origin(payload, args.origin)
        surface = payload["surfaces"].get(origin) or {}
        if args.field not in surface:
            return 2
        print_value(surface[args.field])
        return 0

    for key, surface in payload["surfaces"].items():
        if not args.where or surface.get(args.where) is True:
            print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
