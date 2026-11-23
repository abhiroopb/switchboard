#!/usr/bin/env python3
"""json_get.py: the few jq reads switchboard shell scripts make on every tick, in Python.

jq is not installed on a fresh Windows install (without it publish-switchboard-status.sh
exits 127 every tick and the CoS sidebar tile goes stale), and Python is a hard
switchboard dependency everywhere. Usage, JSON on stdin:

  json_get.py <path> [<default>]          one value; dotted path, e.g. caller.workspace_ref
  json_get.py --lines <path=default>...   one value per line, in order
  json_get.py --tsv <list-path> <field>...  one tab-separated row per list item
                                           (list-path "." is the top-level list;
                                            "workspaces|." takes .workspaces, else the top level)

A missing or null value prints the default (empty when none is given), like jq's
`// default`. Bad JSON prints defaults and exits 0, as `jq ... || true` callers expect.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def get(data: Any, path: str) -> Any:
    if path in ("", "."):
        return data
    for part in path.strip(".").split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def text(value: Any, default: str = "") -> str:
    if value is None or value is False:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def main(argv: list[str]) -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else None
    except ValueError:
        data = None
    out: list[str] = []
    if argv and argv[0] == "--lines":
        for spec in argv[1:]:
            path, _, default = spec.partition("=")
            out.append(text(get(data, path), default).replace("\n", " "))
    elif argv and argv[0] == "--tsv":
        list_path, fields = argv[1], argv[2:]
        rows: Any = None
        for option in list_path.split("|"):
            rows = get(data, option)
            if isinstance(rows, list):
                break
        for item in rows if isinstance(rows, list) else []:
            out.append("\t".join(text(get(item, f)).replace("\t", " ").replace("\n", " ") for f in fields))
    elif argv:
        out.append(text(get(data, argv[0]), argv[1] if len(argv) > 1 else ""))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout,):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", newline="\n")
    sys.exit(main(sys.argv[1:]))
