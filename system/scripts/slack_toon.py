#!/usr/bin/env python3
"""Decode the TOON text `pmos slack` read commands print by default.

The Slack read tools (history, thread, search, ...) take ``--output-format auto|json`` with ``auto`` as the
default, and ``auto`` returns TOON (Token-Oriented Object Notation) whenever it
is shorter than compact JSON. Callers force JSON with
``slack_backoff.json_argv``; this decoder is the fallback that
``slack_backoff.loads`` uses when a reply still is not JSON (an older CLI
build, a command that ignores the flag, a caller that forgot the flag).

Supported subset (everything the Slack tools emit, sample in
fixtures/slack_toon_read_channels.txt):

* ``key: value`` primitives (quoted JSON strings, true/false/null, numbers,
  bare strings, ``[]`` and ``{}`` for empty containers), ``key:`` nested
  objects by two-space indentation;
* ``key[N]: a,b,c`` inline primitive arrays;
* ``key[N]{f1,f2,obj{a,b}}:`` tabular arrays, one CSV row per line, where a
  nested field group ``obj{a,b}`` spreads over consecutive columns;
* ``key[N]:`` expanded lists of ``- item`` lines (primitives, objects whose
  first field sits on the dash line, nested arrays);
* a root object, a root array (``[N]: ...``), or one root primitive;
* the Slack server's sidecar tables, ``messages.reactions[N]{ts,...}``,
  folded back into the matching ``messages`` rows (``_fold_sidecars``).

The delimiter marker in ``[N|]`` / ``[N<TAB>]`` headers is honoured.
"""
from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["ToonDecodeError", "decode", "looks_like_toon"]


class ToonDecodeError(ValueError):
    pass


_KEY = r'(?:"(?:[^"\\]|\\.)*"|[^\s:\[\]{}"][^:\[\]{}"]*?)'
_HEADER = re.compile(
    r"^(?P<key>" + _KEY + r")?"
    r"(?:\[(?P<count>\d+)(?P<delim>[|\t])?\])?"
    r"(?:\{(?P<fields>.*)\})?"
    r":(?:\s(?P<rest>.*)|$)"
)
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def looks_like_toon(text: str) -> bool:
    first = next((line for line in (text or "").splitlines() if line.strip()), "")
    return bool(_HEADER.match(first.strip())) and not first.lstrip().startswith(("{", "["))


def _primitive(token: str) -> Any:
    token = token.strip()
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError as exc:
            raise ToonDecodeError(f"bad quoted string: {token[:80]}") from exc
    if token == "true":
        return True
    if token == "false":
        return False
    if token == "null":
        return None
    if token == "[]":
        return []
    if token == "{}":
        return {}
    if _NUMBER.match(token):
        return float(token) if any(c in token for c in ".eE") else int(token)
    return token


def _split(row: str, delim: str) -> list[str]:
    cells: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(row):
        ch = row[i]
        if in_quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(row):
                buf.append(row[i + 1])
                i += 1
            elif ch == '"':
                in_quote = False
        elif ch == '"':
            in_quote = True
            buf.append(ch)
        elif ch == delim:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if in_quote:
        raise ToonDecodeError(f"unterminated quote in row: {row[:80]}")
    cells.append("".join(buf))
    return cells


def _key(raw: str | None) -> str:
    raw = (raw or "").strip()
    return json.loads(raw) if raw.startswith('"') else raw


def _fields(spec: str, delim: str) -> list[tuple[str, Any]]:
    """``a,b,obj{c,d}`` -> [("a", None), ("b", None), ("obj", [("c", None), ("d", None)])]."""
    out: list[tuple[str, Any]] = []
    depth = 0
    buf: list[str] = []
    parts: list[str] = []
    for ch in spec:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == delim and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.endswith("}") and "{" in part:
            name, inner = part.split("{", 1)
            out.append((_key(name), _fields(inner[:-1], delim)))
        else:
            out.append((_key(part), None))
    return out


def _width(fields: list[tuple[str, Any]]) -> int:
    return sum(_width(sub) if sub is not None else 1 for _, sub in fields)


def _fill(fields: list[tuple[str, Any]], cells: list[str], pos: int) -> tuple[dict[str, Any], int]:
    obj: dict[str, Any] = {}
    for name, sub in fields:
        if sub is None:
            obj[name] = _primitive(cells[pos])
            pos += 1
        else:
            obj[name], pos = _fill(sub, cells, pos)
    return obj, pos


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines: list[tuple[int, str]] = []
        for raw in text.splitlines():
            if not raw.strip():
                continue
            stripped = raw.lstrip(" ")
            self.lines.append((len(raw) - len(stripped), stripped.rstrip()))
        self.i = 0

    def peek_indent(self) -> int | None:
        return self.lines[self.i][0] if self.i < len(self.lines) else None

    def parse_root(self) -> Any:
        if not self.lines:
            return {}
        indent, content = self.lines[0]
        if len(self.lines) == 1 and not _HEADER.match(content):
            self.i = 1
            return _primitive(content)
        match = _HEADER.match(content)
        if match and match.group("key") is None and match.group("count") is not None:
            self.i = 1
            value = self.parse_value(match, indent)
        else:
            value = self.parse_object(indent)
        if self.i != len(self.lines):
            raise ToonDecodeError(f"unparsed line {self.i + 1}: {self.lines[self.i][1][:80]}")
        return value

    def parse_object(self, indent: int) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        while self.i < len(self.lines):
            line_indent, content = self.lines[self.i]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise ToonDecodeError(f"unexpected indent at line {self.i + 1}: {content[:80]}")
            if content == "-" or content.startswith("- "):
                break
            match = _HEADER.match(content)
            if not match or match.group("key") is None:
                raise ToonDecodeError(f"expected key at line {self.i + 1}: {content[:80]}")
            self.i += 1
            obj[_key(match.group("key"))] = self.parse_value(match, indent)
        return obj

    def parse_value(self, match: re.Match[str], indent: int) -> Any:
        """Value of the header on the line just consumed; children sit deeper than ``indent``."""
        rest = match.group("rest")
        delim = match.group("delim") or ","
        if match.group("count") is None:
            if rest is not None and rest.strip():
                return _primitive(rest)
            child = self.peek_indent()
            if child is not None and child > indent:
                return self.parse_object(child)
            return {}
        count = int(match.group("count"))
        if match.group("fields") is not None:
            fields = _fields(match.group("fields"), delim)
            width = _width(fields)
            rows = []
            for _ in range(count):
                if self.i >= len(self.lines) or self.lines[self.i][0] <= indent:
                    raise ToonDecodeError(f"tabular array short of {count} rows")
                cells = _split(self.lines[self.i][1], delim)
                if len(cells) != width:
                    raise ToonDecodeError(f"row {self.i + 1} has {len(cells)} cells, header has {width}")
                rows.append(_fill(fields, cells, 0)[0])
                self.i += 1
            return rows
        if rest is not None and rest.strip():
            return [_primitive(cell) for cell in _split(rest, delim)]
        items = []
        for _ in range(count):
            if self.i >= len(self.lines) or self.lines[self.i][0] <= indent:
                raise ToonDecodeError(f"list short of {count} items")
            items.append(self.parse_item())
        return items

    def parse_item(self) -> Any:
        dash_indent, content = self.lines[self.i]
        if content != "-" and not content.startswith("- "):
            raise ToonDecodeError(f"expected '- ' item at line {self.i + 1}: {content[:80]}")
        body = content[2:].strip() if content != "-" else ""
        self.i += 1
        if not body:
            return {}
        match = _HEADER.match(body)
        if not match:
            return _primitive(body)
        field_indent = dash_indent + 2
        if match.group("key") is None:
            return self.parse_value(match, field_indent)
        obj = {_key(match.group("key")): self.parse_value(match, field_indent)}
        if self.peek_indent() == field_indent and not self.lines[self.i][1].startswith("- "):
            obj.update(self.parse_object(field_indent))
        return obj


def _fold_sidecars(value: Any) -> Any:
    """Fold ``parent.child[N]{join,...}`` sidecar tables back into ``parent`` rows.

    The Slack server prints per-message lists that do not fit the message
    table (reactions, for one) as a sibling table keyed by the message's first
    column: ``messages.reactions[9]{ts,name,count}``. Each sidecar row joins
    the ``messages`` row with the same ``ts`` and lands in its ``reactions``
    list without the join column, the shape the JSON output has. A dotted key
    whose parent or join column is missing stays as it is.
    """
    if isinstance(value, list):
        return [_fold_sidecars(item) for item in value]
    if not isinstance(value, dict):
        return value
    value = {key: _fold_sidecars(item) for key, item in value.items()}
    for key in [k for k in value if "." in k]:
        parent_key, _, child_key = key.partition(".")
        parent, side = value.get(parent_key), value[key]
        if not (isinstance(parent, list) and isinstance(side, list) and side
                and all(isinstance(row, dict) and row for row in parent + side)):
            continue
        join = next(iter(side[0]))
        if not all(join in row for row in parent):
            continue
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for row in side:
            grouped.setdefault(row.get(join), []).append({k: v for k, v in row.items() if k != join})
        for row in parent:
            if row[join] in grouped:
                row[child_key] = grouped[row[join]]
        del value[key]
    return value


def decode(text: str) -> Any:
    """Parse TOON ``text`` into the value the equivalent JSON would give."""
    return _fold_sidecars(_Parser(text or "").parse_root())
