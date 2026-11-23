#!/usr/bin/env python3
"""Slack link and channel-mention format.

Rule (system/prompts/external-replies.md Rule 4): a Slack message links inline
with anchor text that names the thing (`<https://url|anchor>`, or
`[anchor](url)` in a markdown block), never a bare URL; channel mentions are
`<#C…|channel-name>`, never plain `#channel-name`, which Slack renders as text
in an API-posted message.

This module is the Python half of the check. The TypeScript half lives in
system/amp-plugins/switchboard-guardrails.ts (rule 13, `slack-link-format`); both run
system/amp-plugins/switchboard/test-fixtures/slack-link-format-cases.json so the
detectors stay aligned. `send-triage-draft.py` uses it because the guardrail
exempts that script.

CLI: `python3 slack_format.py check "<text>"` prints the finding as JSON and
exits 1 when the text would be rejected.

Native lists:
external-replies.md Rule 4 item 5. A list in a Slack message switchboard posts is a
`rich_text_list` block (style bullet, or ordered when the source is numbered),
never a `•`, `-`, `*`, `1.` text bullet inside mrkdwn. `bullet_lines(text)` is the
detector (guardrail rule 14, `slack-native-bullets`, mirrors it in TypeScript);
`to_blocks(text)` is the converter the switchboard send scripts run at send time so
existing drafts keep posting: paragraphs stay mrkdwn `section` blocks, every run
of bullet lines becomes a `rich_text` block whose lists carry the same words.

CLI: `python3 slack_format.py bullets "<text>"` prints the offending lines and
exits 1 when there are any; `python3 slack_format.py blocks "<text>"` prints the
converted blocks JSON (pass it as `--blocks`, keep `--text` as the fallback) and
exits 3, printing nothing, when the text has no list lines.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIORITY_CHANNELS = REPO_ROOT / "system" / "config" / "priority-channels.yaml"
CHANNEL_SOURCE_FILES = (
    REPO_ROOT / "system" / "state" / "sources" / "owner-mentions.jsonl",
    REPO_ROOT / "system" / "state" / "sources" / "owner-slack-sent.jsonl",
    REPO_ROOT / "system" / "state" / "sources" / "owner-channel-signals.jsonl",
    REPO_ROOT / "system" / "state" / "sources" / "owner-mentions.archive.jsonl",
)

_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{8,}$")
# Mirrors SLACK_PLAIN_CHANNEL_RE in switchboard-guardrails.ts.
_PLAIN_CHANNEL_RE = re.compile(r"(?<![\w<#/&@.\-])#([a-z0-9](?:[a-z0-9_\-]*[a-z0-9])?)(?![\w\-])")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_ANGLE_TOKEN_RE = re.compile(r"<([^<>\n]*)>")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((<?)(https?://[^)\s]+)>?\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>]+")
_TRAILING_PUNCT_RE = re.compile(r"""[.,;:!?)\]'"]+$""")

Resolver = Callable[[str], str | None]


def _trim(url: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", url)


def _prose(text: str) -> tuple[str, list[str]]:
    """Text with code, mrkdwn tokens, and labelled links removed, plus URLs those tokens left bare."""
    bare: list[str] = []
    prose = _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))

    def angle(m: re.Match[str]) -> str:
        target, _, label = m.group(1).partition("|")
        if re.match(r"https?://", target, re.I) and not label.strip():
            bare.append(_trim(target))
        return " "

    def md_link(m: re.Match[str]) -> str:
        label = m.group(1).strip()
        if not label or re.fullmatch(r"https?://\S*", label, re.I):
            bare.append(_trim(m.group(3)))
        return " "

    prose = _ANGLE_TOKEN_RE.sub(angle, prose)
    prose = _MARKDOWN_LINK_RE.sub(md_link, prose)
    return prose, bare


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def bare_urls(text: str) -> list[str]:
    """URLs not wrapped as `<url|anchor>` or `[anchor](url)`, in order, deduplicated."""
    prose, from_tokens = _prose(text)
    return _dedupe(from_tokens + [_trim(m.group(0)) for m in _BARE_URL_RE.finditer(prose)])


def blocks_texts(blocks: object) -> list[str]:
    """Every string under a `text` key in a Block Kit payload, as the link rule judges it.

    A rich_text `link` element with a non-empty `text` is a labelled link, so its
    `url` is never read as prose; a `link` with no `text` renders the URL itself,
    so its `url` is returned and `bare_urls` reports it. A string that is not JSON
    is returned whole. Mirrors `slackBlocksTexts` in switchboard-guardrails.ts (so a
    `--blocks "$(cat blocks.json)"` argument is no false positive); the `blocks` cases
    in slack-link-format-cases.json run against both.
    """
    value = blocks
    if isinstance(blocks, str):
        try:
            value = json.loads(blocks)
        except ValueError:
            return [blocks]
    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for v in node:
                walk(v)
            return
        if not isinstance(node, dict):
            return
        label = node.get("text")
        if node.get("type") == "link" and isinstance(node.get("url"), str) and not (isinstance(label, str) and label.strip()):
            out.append(node["url"])
        for key, v in node.items():
            if key == "text" and isinstance(v, str):
                out.append(v)
            else:
                walk(v)

    walk(value)
    return out


def blocks_bare_urls(blocks: object) -> list[str]:
    """Bare URLs across every text a Block Kit payload carries, in order, deduplicated."""
    return _dedupe([u for t in blocks_texts(blocks) for u in bare_urls(t)])


def plain_channel_mentions(text: str) -> list[str]:
    """Channel names typed as plain `#name`, in order, deduplicated."""
    prose, _ = _prose(text)
    return _dedupe([m.group(1) for m in _PLAIN_CHANNEL_RE.finditer(prose) if re.search(r"[a-z]", m.group(1))])


def channel_ids_from_priority_yaml(yaml_text: str) -> dict[str, str]:
    """`name → C… id` from priority-channels.yaml: an `id:` line and the `name:` at the same key column after it."""
    out: dict[str, str] = {}
    pending: tuple[str, int] | None = None
    for line in yaml_text.split("\n"):
        id_match = re.match(r"^(\s*(?:-\s+)?)id:\s*([A-Z0-9]+)\s*$", line)
        if id_match:
            pending = (id_match.group(2), len(id_match.group(1))) if _CHANNEL_ID_RE.match(id_match.group(2)) else None
            continue
        name_match = re.match(r'^(\s*)name:\s*"?([^"\s#]+)"?\s*(?:#.*)?$', line)
        if name_match and pending and len(name_match.group(1)) == pending[1]:
            out.setdefault(name_match.group(2), pending[0])
            pending = None
    return out


def channel_ids_from_source_jsonl(jsonl_text: str) -> dict[str, str]:
    """`name → C… id` from comms source lines carrying `channel` and `channel_id`."""
    out: dict[str, str] = {}
    for m in re.finditer(r'"channel":\s*"([^"\\]+)",\s*"channel_id":\s*"([CG][A-Z0-9]{8,})"', jsonl_text):
        out.setdefault(m.group(1), m.group(2))
    return out


_cache: tuple[float, dict[str, str]] | None = None


def resolve_channel_id(name: str) -> str | None:
    """Channel id for a name from priority-channels.yaml first, then the comms sources; cached 10 minutes."""
    global _cache
    if _cache is None or time.time() - _cache[0] > 600:
        table: dict[str, str] = {}
        try:
            table.update(channel_ids_from_priority_yaml(PRIORITY_CHANNELS.read_text(encoding="utf-8")))
        except OSError:
            pass
        for path in CHANNEL_SOURCE_FILES:
            try:
                for n, cid in channel_ids_from_source_jsonl(path.read_text(encoding="utf-8", errors="ignore")).items():
                    table.setdefault(n, cid)
            except OSError:
                continue
        _cache = (time.time(), table)
    return _cache[1].get(name)


@dataclass
class Finding:
    bare_urls: list[str] = field(default_factory=list)
    rewritten: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def rejected(self) -> bool:
        return bool(self.bare_urls or self.unresolved)

    def reason(self) -> str:
        """One sentence naming each offending item, for a refusal message."""
        parts: list[str] = []
        if self.bare_urls:
            urls = ", ".join(f'"{u}"' for u in self.bare_urls)
            parts.append(
                f"bare URL{'s' if len(self.bare_urls) > 1 else ''} {urls}: wrap each as <url|anchor text> "
                "where the anchor names the thing (the PR title, the Linear issue title, the doc name); "
                "the check cannot invent that text"
            )
        if self.unresolved:
            names = ", ".join(f'"#{n}"' for n in self.unresolved)
            parts.append(
                f"channel mention{'s' if len(self.unresolved) > 1 else ''} {names} with no known channel id: "
                f"write <#C…|{self.unresolved[0]}> with the id from system/config/priority-channels.yaml or a Slack lookup"
            )
        return "; and ".join(parts)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(text)]
    for regex in (_INLINE_CODE_RE, _ANGLE_TOKEN_RE):
        for m in regex.finditer(text):
            if not any(a <= m.start() < b for a, b in spans):
                spans.append(m.span())
    return sorted(spans)


def _rewrite_outside_tokens(text: str, name: str, replacement: str) -> str:
    token = re.compile(rf"(?<![\w<#/&@.\-])#{re.escape(name)}(?![\w\-])")
    out: list[str] = []
    last = 0
    for start, end in _protected_spans(text):
        out.append(token.sub(replacement, text[last:start]))
        out.append(text[start:end])
        last = end
    out.append(token.sub(replacement, text[last:]))
    return "".join(out)


def check(text: str, resolve: Resolver = resolve_channel_id) -> Finding:
    """Judge one Slack message text and rewrite the channel mentions it can."""
    finding = Finding(bare_urls=bare_urls(text), text=text)
    for name in plain_channel_mentions(text):
        cid = resolve(name)
        if not cid:
            finding.unresolved.append(name)
            continue
        finding.rewritten.append({"name": name, "id": cid})
        finding.text = _rewrite_outside_tokens(finding.text, name, f"<#{cid}|{name}>")
    return finding


# ---------------------------------------------------------------------------
# Native lists (rule 14, slack-native-bullets)
# ---------------------------------------------------------------------------

# Mirrors SLACK_TEXT_BULLET_RE in switchboard-guardrails.ts: optional indent, a bullet glyph
# (• ◦ ▪ - *) or an ordinal (`1.` / `1)`), one or more spaces, then content. `---`
# rules and `*bold*` lines never match because the marker must be followed by a space.
_TEXT_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[•◦▪\-*]|\d{1,3}[.)])[ \t]+(?P<body>\S.*)$")
_FENCE_LINE_RE = re.compile(r"^\s*```")
_SECTION_TEXT_LIMIT = 2900  # Slack caps a section's mrkdwn text at 3000 characters.

Block = dict


def _lines_outside_fences(text: str) -> list[tuple[int, str, bool]]:
    """(index, line, inside_fence) for every line; fence lines themselves count as inside."""
    out: list[tuple[int, str, bool]] = []
    inside = False
    for i, line in enumerate(text.split("\n")):
        if _FENCE_LINE_RE.match(line):
            inside = not inside
            out.append((i, line, True))
            continue
        out.append((i, line, inside))
    return out


def bullet_lines(text: str) -> list[str]:
    """Lines written as text bullets (`• x`, `- x`, `* x`, `1. x`, `1) x`) outside fenced code, stripped."""
    return [line.strip() for _, line, inside in _lines_outside_fences(text) if not inside and _TEXT_BULLET_RE.match(line)]


def has_text_bullets(text: str) -> bool:
    return bool(bullet_lines(text))


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


_INLINE_TOKEN_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<angle><[^<>\n]+>)"
    r"|(?P<md>\[[^\]\n]+\]\(<?https?://[^)\s]+>?\))"
    r"|(?P<url>https?://[^\s<>]+)"
    r"|(?P<emoji>(?<![\w:]):[a-z0-9_+\-]+:(?![\w:]))"
    r"|(?P<bold>(?<![\w*])\*(?=\S)[^*\n]+?(?<=\S)\*(?![\w*]))"
    r"|(?P<italic>(?<![\w_])_(?=\S)[^_\n]+?(?<=\S)_(?![\w_]))"
    r"|(?P<strike>(?<![\w~])~(?=\S)[^~\n]+?(?<=\S)~(?![\w~]))"
)


def _text_el(s: str, style: dict | None) -> list[Block]:
    if not s:
        return []
    el: Block = {"type": "text", "text": _unescape(s)}
    if style:
        el["style"] = dict(style)
    return [el]


def _angle_el(token: str, style: dict | None) -> list[Block]:
    """One `<…>` mrkdwn token as a rich_text element (link, channel, user, broadcast, usergroup)."""
    inner = token[1:-1]
    target, _, label = inner.partition("|")
    if re.match(r"https?://", target, re.I):
        el: Block = {"type": "link", "url": _unescape(target)}
        if label.strip():
            el["text"] = _unescape(label)
        if style:
            el["style"] = dict(style)
        return [el]
    if target.startswith("#"):
        return [{"type": "channel", "channel_id": target[1:]}]
    if target.startswith("@"):
        return [{"type": "user", "user_id": target[1:]}]
    if target.startswith("!subteam^"):
        return [{"type": "usergroup", "usergroup_id": target[len("!subteam^"):]}]
    if target.startswith("!") and target[1:] in ("here", "channel", "everyone"):
        return [{"type": "broadcast", "range": target[1:]}]
    return _text_el(token, style)


def parse_inline(text: str, style: dict | None = None) -> list[Block]:
    """mrkdwn inline text as rich_text_section elements. Words and links are kept; only the container changes."""
    out: list[Block] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        out += _text_el(text[pos:m.start()], style)
        kind = m.lastgroup
        tok = m.group(0)
        if kind == "code":
            out += _text_el(tok[1:-1], {**(style or {}), "code": True})
        elif kind == "angle":
            out += _angle_el(tok, style)
        elif kind == "md":
            mm = _MARKDOWN_LINK_RE.match(tok)
            assert mm is not None
            el: Block = {"type": "link", "url": mm.group(3), "text": _unescape(mm.group(1))}
            if style:
                el["style"] = dict(style)
            out.append(el)
        elif kind == "url":
            url = _trim(tok)
            el = {"type": "link", "url": url}
            if style:
                el["style"] = dict(style)
            out.append(el)
            out += _text_el(tok[len(url):], style)
        elif kind == "emoji":
            out.append({"type": "emoji", "name": tok[1:-1]})
        else:
            flag = {"bold": "bold", "italic": "italic", "strike": "strike"}[kind]
            out += parse_inline(tok[1:-1], {**(style or {}), flag: True})
        pos = m.end()
    out += _text_el(text[pos:], style)
    return _merge_text(out)


def _merge_text(elements: list[Block]) -> list[Block]:
    """Join adjacent text elements with the same style so a trailing "." and a following " " stay one run."""
    merged: list[Block] = []
    for el in elements:
        prev = merged[-1] if merged else None
        if prev and prev["type"] == "text" and el["type"] == "text" and prev.get("style") == el.get("style"):
            prev["text"] += el["text"]
        else:
            merged.append(el)
    return merged


def _indent_width(ws: str) -> int:
    return sum(4 if c == "\t" else 1 for c in ws)


def _list_run_to_rich_text(items: list[re.Match[str]]) -> Block:
    """One run of consecutive bullet lines as a rich_text block: one level of nesting by indent, ordered when numbered."""
    base = _indent_width(items[0].group("indent"))
    lists: list[Block] = []
    current: Block | None = None
    top_level_count = 0
    for m in items:
        level = 1 if _indent_width(m.group("indent")) > base else 0
        style = "ordered" if m.group("marker")[0].isdigit() else "bullet"
        if current is None or current["indent"] != level or current["style"] != style:
            current = {"type": "rich_text_list", "style": style, "indent": level, "elements": []}
            if level == 0 and style == "ordered" and top_level_count:
                current["offset"] = top_level_count
            lists.append(current)
        current["elements"].append({"type": "rich_text_section", "elements": parse_inline(m.group("body").rstrip())})
        if level == 0:
            top_level_count += 1
    return {"type": "rich_text", "elements": lists}


def _section_blocks(lines: list[str]) -> list[Block]:
    text = "\n".join(lines).strip("\n")
    if not text.strip():
        return []
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        candidate = line if not buf else f"{buf}\n{line}"
        if buf and len(candidate) > _SECTION_TEXT_LIMIT:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    chunks.append(buf)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks if c.strip()]


def to_blocks(text: str) -> list[Block] | None:
    """Block Kit blocks for a text whose lists are written as text bullets; None when it has no list lines.

    Paragraph lines (and fenced code) stay mrkdwn `section` blocks; each run of bullet lines
    becomes a `rich_text` block of `rich_text_list`s. A blank line or a non-bullet line ends a run.
    """
    if not has_text_bullets(text):
        return None
    blocks: list[Block] = []
    para: list[str] = []
    run: list[re.Match[str]] = []

    def flush_para() -> None:
        nonlocal para
        blocks.extend(_section_blocks(para))
        para = []

    def flush_run() -> None:
        nonlocal run
        if run:
            blocks.append(_list_run_to_rich_text(run))
        run = []

    for _, line, inside in _lines_outside_fences(text):
        m = None if inside else _TEXT_BULLET_RE.match(line)
        if m:
            flush_para()
            run.append(m)
        else:
            flush_run()
            para.append(line)
    flush_para()
    flush_run()
    return blocks


def flatten_blocks_text(blocks: list[Block]) -> str:
    """Every text string the blocks carry (section text, rich_text text/link labels, `:emoji:`), newline-joined."""
    parts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t == "emoji":
                parts.append(f":{node.get('name', '')}:")
            elif t == "link":
                parts.append(str(node.get("text") or node.get("url") or ""))
            elif t == "text" and "text" in node:
                parts.append(str(node["text"]))
            elif t == "mrkdwn" or t == "plain_text":
                parts.append(str(node.get("text", "")))
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(blocks)
    return "\n".join(parts)


def bullets_reason(lines: list[str]) -> str:
    """Refusal sentence for text bullets, for the Python send path (the guardrail has its own)."""
    shown = "; ".join(f'"{ln}"' for ln in lines[:3]) + (f"; and {len(lines) - 3} more" if len(lines) > 3 else "")
    return (
        f"text bullet{'s' if len(lines) > 1 else ''} {shown}: a list in a Slack message is a native rich_text_list "
        "block (style bullet, or ordered when numbered), never a •, -, *, or 1. text bullet. Run "
        'python3 system/scripts/slack_format.py blocks "<text>" and pass the JSON as --blocks, keeping --text as the '
        "fallback; words unchanged (external-replies.md Rule 4 item 5)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="judge one message text; exit 1 when it would be rejected")
    chk.add_argument("text")
    res = sub.add_parser("resolve", help="print the channel id for a name")
    res.add_argument("name")
    bul = sub.add_parser("bullets", help="print the text-bullet lines; exit 1 when there are any")
    bul.add_argument("text")
    blk = sub.add_parser("blocks", help="print Block Kit blocks with native lists; exit 3 (no output) when the text has no list lines")
    blk.add_argument("text")
    args = parser.parse_args(argv)
    if args.cmd == "resolve":
        cid = resolve_channel_id(args.name)
        print(cid or "")
        return 0 if cid else 1
    if args.cmd == "bullets":
        lines = bullet_lines(args.text)
        for ln in lines:
            print(ln)
        return 1 if lines else 0
    if args.cmd == "blocks":
        blocks = to_blocks(args.text)
        if blocks is None:
            return 3
        print(json.dumps(blocks, ensure_ascii=False))
        return 0
    finding = check(args.text)
    print(json.dumps({**asdict(finding), "rejected": finding.rejected, "reason": finding.reason()}, indent=2))
    return 1 if finding.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
