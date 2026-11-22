#!/usr/bin/env python3
"""Render the command-center markdown digest for terminal or CoS chat.

The default writes a box-drawing table sized to the terminal. ``--chat``
writes emoji-headed lists while preserving the stored table contract.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import named_refs
from review_surface import report_text

REPO_ROOT = Path(__file__).resolve().parents[2]
LATEST = REPO_ROOT / "system" / "state" / "command-center-latest.md"


def table_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return the half-open bounds of the first contiguous markdown table."""
    start = next((i for i, line in enumerate(lines) if line.startswith("|")), None)
    if start is None:
        return None
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return start, end


def parse_table_row(line: str) -> list[str]:
    """Parse one markdown row without splitting escaped pipe characters."""
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
    ]


def parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Pull the first markdown table out of the digest. Returns (header, rows)."""
    all_lines = text.splitlines()
    bounds = table_bounds(all_lines)
    if not bounds:
        return [], []
    start, end = bounds
    lines = all_lines[start:end]
    if len(lines) < 2:
        return [], []
    header = parse_table_row(lines[0])
    # Skip the separator line
    rows = []
    for ln in lines[2:]:
        cells = parse_table_row(ln)
        if len(cells) == len(header):
            rows.append(cells)
    return header, rows


# ── visible-width helpers (handle markdown links, <br>, emoji) ──────────────

LINK_RE = re.compile(r"\[`?([^`\]]+)`?\]\([^)]+\)")
BACKTICK_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ESCAPE_RE = re.compile(r"\\(.)")


def strip_markdown(s: str) -> str:
    s = LINK_RE.sub(r"\1", s)
    s = BOLD_RE.sub(r"\1", s)
    s = BACKTICK_RE.sub(r"\1", s)
    s = ESCAPE_RE.sub(r"\1", s)
    s = s.replace("<br>", " · ")
    return s


def inline_cell(s: str) -> str:
    """Keep markdown and action handles while forcing one topic onto one line."""
    s = re.sub(r"<br\s*/?>", " · ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def cell_width(s: str) -> int:
    """Approximate display width — emoji/wide chars count as 2."""
    w = 0
    for ch in s:
        cp = ord(ch)
        if cp == 0:
            continue
        # Quick wide-char heuristic: emoji + CJK ranges
        if (
            0x1100 <= cp <= 0x115F
            or 0x2E80 <= cp <= 0x303E
            or 0x3041 <= cp <= 0x33FF
            or 0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF
            or 0xA000 <= cp <= 0xA4CF
            or 0xAC00 <= cp <= 0xD7A3
            or 0xF900 <= cp <= 0xFAFF
            or 0xFE30 <= cp <= 0xFE4F
            or 0xFF00 <= cp <= 0xFF60
            or 0xFFE0 <= cp <= 0xFFE6
            or 0x1F000 <= cp <= 0x1FFFF
            or 0x2600 <= cp <= 0x27BF
        ):
            w += 2
        elif cp < 0x20:
            continue
        else:
            w += 1
    return w


def truncate(s: str, width: int) -> str:
    """Truncate to visible-width, adding … if cut."""
    if cell_width(s) <= width:
        return s
    out = ""
    used = 0
    for ch in s:
        ch_w = cell_width(ch)
        if used + ch_w > width - 1:
            break
        out += ch
        used += ch_w
    return out + "…"


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - cell_width(s))


# ── render ───────────────────────────────────────────────────────────────────

def render(text: str, term_width: int) -> str:
    header, rows = parse_markdown_table(text)
    if not header:
        return text  # nothing to render — fall back to raw

    # Filter out section divider rows like "| ⏤ | **— workstreams —** | ... |"
    clean_rows = []
    section_breaks = []
    for r in rows:
        # A section divider has only the workspace name populated and dashes
        if "—" in r[1] and all(not c.strip() for c in r[2:]):
            section_breaks.append(len(clean_rows))
            continue
        clean_rows.append([strip_markdown(c) for c in r])

    clean_header = [strip_markdown(h) for h in header]

    # Pick columns to keep. The current digest contract is five
    # columns: # | Workspace | Status / Next | Debt | Actions. The legacy
    # labels (Urgency/Next meeting/Open/Close) are kept so older history
    # snapshots still render.
    keep_idx = []
    label_to_idx = {h: i for i, h in enumerate(clean_header)}

    def add(label):
        if label in label_to_idx:
            keep_idx.append(label_to_idx[label])

    add("#")
    add("Workspace")
    add("Urgency")
    add("Status / Next")
    add("Next meeting")
    add("Debt")
    add("Actions")
    add("Open")
    add("Close")

    # Build narrowed table
    keep_header = [clean_header[i] for i in keep_idx]
    keep_rows = [[r[i] for i in keep_idx] for r in clean_rows]

    # Compute column widths with caps. Actions is capped wide enough that
    # "open NNN · close NNN" never truncates or splits.
    col_caps = {
        "#": 4,
        "Workspace": 30,
        "Urgency": 6,
        "Status / Next": max(28, term_width - 90),
        "Next meeting": 22,
        "Debt": 11,
        "Actions": 20,
        "Open": 9,
        "Close": 10,
    }
    widths = []
    for i, h in enumerate(keep_header):
        cap = col_caps.get(h, 20)
        natural = max(cell_width(h), max((cell_width(r[i]) for r in keep_rows), default=0))
        widths.append(min(natural, cap))

    # Trim if total too wide
    total = sum(widths) + 3 * (len(widths) - 1) + 4
    if total > term_width:
        overflow = total - term_width
        # Take from Status / Next first
        if "Status / Next" in keep_header:
            si = keep_header.index("Status / Next")
            widths[si] = max(20, widths[si] - overflow)

    def hline(left, mid, right, fill="─"):
        return left + mid.join(fill * (w + 2) for w in widths) + right

    def row_line(cells):
        parts = []
        for i, c in enumerate(cells):
            t = truncate(c, widths[i])
            parts.append(" " + pad(t, widths[i]) + " ")
        return "│" + "│".join(parts) + "│"

    out = []
    out.append(hline("╭", "┬", "╮"))
    out.append(row_line(keep_header))
    out.append(hline("├", "┼", "┤"))

    next_break_idx = 0
    for ri, r in enumerate(keep_rows):
        if next_break_idx < len(section_breaks) and ri == section_breaks[next_break_idx]:
            if ri != 0:  # a break before the first row would double the header rule
                out.append(hline("├", "┼", "┤", fill="─"))
            next_break_idx += 1
        out.append(row_line(r))
    out.append(hline("╰", "┴", "╯"))
    return "\n".join(out)


def render_chat(text: str) -> str:
    """Convert the table-backed snapshot into the CoS chat list contract."""
    lines = text.splitlines()
    bounds = table_bounds(lines)
    header, rows = parse_markdown_table(text)
    if not bounds or not header:
        return text

    start, end = bounds
    out = []
    for line in lines[:start]:
        if line.startswith("Act on a row:"):
            line = "Actions:" + line.removeprefix("Act on a row:")
        out.append(line)

    expected = {name: i for i, name in enumerate(header)}
    required = {"#", "Workspace", "Status / Next", "Debt", "Actions"}
    if not required.issubset(expected):
        return text

    for row in rows:
        ref = row[expected["#"]]
        workspace = inline_cell(row[expected["Workspace"]])
        if ref == "⏤":
            label = strip_markdown(workspace).strip("— ")
            if out and out[-1] != "":
                out.append("")
            out.append(f"=== {label} ===")
            continue

        topic = [workspace]
        # Cache-only pass: a stored snapshot from before the rule still shows
        # a PR or task by its name when the title is already cached.
        status = named_refs.name_refs(inline_cell(row[expected["Status / Next"]]))
        debt = inline_cell(row[expected["Debt"]])
        actions = inline_cell(row[expected["Actions"]])
        if status:
            topic.append(status)
        if debt and debt != "—":
            topic.append(f"Debt: {debt}")
        if actions and actions != "—":
            topic.append(actions)
        # Review pointers leave the table cell as standalone lines, after all
        # prose/action handles. Never append punctuation to their link target.
        out.append(report_text("- " + " — ".join(topic)))

    for line in lines[end:]:
        if line.startswith("<!-- SLUG_INDEX "):
            continue
        if line.startswith("## "):
            if out and out[-1] != "":
                out.append("")
            out.append(f"=== {line.removeprefix('## ').strip()} ===")
            continue
        out.append(inline_cell(line) if line else "")

    # Avoid extra blank lines introduced around the hidden SLUG_INDEX marker.
    compact = []
    for line in out:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def main() -> int:
    named_refs.set_fetch_budget(0)
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=LATEST)
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Render emoji-headed lists for CoS chat instead of a terminal table.",
    )
    args = parser.parse_args()
    path = args.path
    if not path.exists():
        print(f"(no digest yet: {path})")
        return 0
    text = path.read_text(encoding="utf-8")
    if args.chat:
        print(render_chat(text))
        return 0
    term_width = shutil.get_terminal_size((120, 40)).columns

    # Print digest header (everything before the table)
    pre_table = []
    for line in text.splitlines():
        if line.startswith("|"):
            break
        pre_table.append(line)
    print("\n".join(pre_table).rstrip())
    print()
    print(render(text, term_width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
