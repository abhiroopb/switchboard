#!/usr/bin/env python3
"""Generic personal-data detectors for a public build. They name nobody, so they ship.

The deny lists catch what the owner knows to look for. These catch the shapes nobody listed: an
email address, a phone number, a chat or document id, a public IP address, an absolute home path,
image metadata, and a quoted owner ruling. A hit reports the file, line and kind, never the value.

    python3 system/kit/scan_public.py <tree>          exit 1 when anything is found
    python3 system/kit/scan_public.py <tree> --json

build_tier.py calls scan_tree() as the `public` gate. `public_allow` in tiers.yaml maps a kind to
globs where that kind is expected (for example third-party license files that carry an author email).
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import re
import struct
import sys
from pathlib import Path

EMAIL = re.compile(r"(?<![\w.+/-])[A-Za-z0-9][\w.+-]*@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w.-]*[/\d])")
SAFE_EMAIL_DOMAIN = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost|"
                               r"users\.noreply\.github\.com|noreply\.github\.com|anthropic\.com|yourcompany\.com|your-domain\.com)$")
SAFE_EMAIL_LOCAL = re.compile(r"(?i)^(?:noreply|no-reply|git)$")
PHONE = re.compile(r"(?<![\w.-])(?:\+?1[-. ])?(?:\([2-9]\d{2}\) ?|[2-9]\d{2}[-. ])[2-9]\d{2}[-. ]\d{4}(?![\w-])"
                   r"|(?<![\w.-])\+(?:[2-9]\d{0,2})[ -]\d{1,4}[ -]\d{3,4}[ -]\d{3,4}(?![\w-])")
FAKE_PHONE = re.compile(r"555[-. ]?01\d\d")
SLACK_ID = re.compile(r"(?<![A-Za-z0-9])[UWCTGD]0[A-Z0-9]{7,10}(?![A-Za-z0-9])")
GOOGLE_DOC = re.compile(r"docs\.google\.com/(?:document|spreadsheets|presentation|forms)/d/([A-Za-z0-9_-]{25,})")
DRIVE_FILE = re.compile(r"drive\.google\.com/(?:file/d/|drive/folders/|open\?id=)([A-Za-z0-9_-]{25,})")
DROPBOX_LINK = re.compile(r"dropbox\.com/(?:s|scl/fi|sh)/[A-Za-z0-9]{8,}")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
HOME_PATH = re.compile(r"(?:\b[A-Za-z]:[/\\]{1,2}[Uu]sers[/\\]{1,2}|(?<![\w.~-])(?:/mnt)?/[A-Za-z]/[Uu]sers/"
                       r"|(?<![\w.~-])/Users/|(?<![\w.~-])/home/)([A-Za-z0-9._-]++)(?!\()")
SAFE_HOME = re.compile(r"(?i)^(?:you|your[-_]?name|user|username|owner|example|me|runner|runneradmin|public|default|"
                       r"shared|linuxbrew|name|someone|alice|bob|carol|pat|sam|jane|john|person|home|tester|test|invented|fake|demo|"
                       r"x|a|it|kit|data|dev|pm-os|switchboard|\.\.\.|<.*|\{.*|\$.*|%.*|\*)$|^[a-z]$")
OWNER_RULING = re.compile(r"\(the owner, \d{4}-\d{2}-\d{2}[^)]*?:\s*[\"“]")
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}|YOUR[_-][A-Z_-]+|<[a-z-]+>")


BINARY_OK = (".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf", ".otf")


def decode_for_scan(data: bytes) -> str | None:
    """Text in any encoding a file is likely to use (UTF-8, UTF-16 or UTF-32 with or without a BOM);
    None for real binary data. Bytes that do not decode become U+FFFD, so the rest is still scanned."""
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    if b"\0" in data:
        half = max(len(data) // 2, 1)
        if data[1::2].count(0) > half * 0.6:
            return data.decode("utf-16-le", "replace")
        if data[0::2].count(0) > half * 0.6:
            return data.decode("utf-16-be", "replace")
        return None
    return data.decode("utf-8", "replace")


def _fake_id(value: str) -> bool:
    """Obviously invented ids: runs of one character, or ascending digits after the prefix."""
    body = value[2:]
    return len(set(body)) <= 2 or body.isdigit() and body in "0123456789012345" or "EXAMPLE" in value or "XXXX" in value


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if any(int(part) > 255 for part in value.split(".")):
        return False
    documentation = any(ip in ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
    return ip.is_global and not documentation


def line_hits(line: str) -> list[str]:
    kinds = []
    for m in EMAIL.finditer(line):
        local = m.group(0).split("@", 1)[0]
        if not (SAFE_EMAIL_DOMAIN.search(m.group(1)) or SAFE_EMAIL_LOCAL.match(local) or PLACEHOLDER.search(m.group(0))):
            kinds.append("email")
    if any(not FAKE_PHONE.search(m.group(0)) for m in PHONE.finditer(line)):
        kinds.append("phone")
    if any(not _fake_id(m.group(0)) for m in SLACK_ID.finditer(line)):
        kinds.append("chat-id")
    for rx, kind in ((GOOGLE_DOC, "doc-id"), (DRIVE_FILE, "doc-id")):
        if any(not PLACEHOLDER.search(m.group(1)) and "EXAMPLE" not in m.group(1).upper() for m in rx.finditer(line)):
            kinds.append(kind)
    if DROPBOX_LINK.search(line):
        kinds.append("share-link")
    if any(_public_ip(m.group(1)) for m in IPV4.finditer(line)):
        kinds.append("ip")
    if any(not SAFE_HOME.match(m.group(1)) for m in HOME_PATH.finditer(line)):
        kinds.append("home-path")
    if OWNER_RULING.search(line):
        kinds.append("owner-ruling")
    if "gitleaks:allow" in line:
        kinds.append("scanner-suppression")
    return kinds


def image_metadata(rel: str, data: bytes) -> list[str]:
    """PNG text and EXIF chunks, JPEG EXIF and XMP segments: anything a camera or editor wrote."""
    found = []
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        pos = 8
        while pos + 8 <= len(data):
            length, kind = struct.unpack(">I4s", data[pos:pos + 8])
            if kind in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
                found.append(f"png {kind.decode()}")
            pos += 12 + length
            if kind == b"IEND":
                break
    elif data[:2] == b"\xff\xd8":
        if b"Exif\x00\x00" in data[:65536]:
            found.append("jpeg exif")
        if b"http://ns.adobe.com/xap/" in data[:65536]:
            found.append("jpeg xmp")
    return found


def scan_tree(tree: dict[str, bytes], allow: dict[str, list[str]] | None = None) -> list[dict]:
    allow = allow or {}
    hits: list[dict] = []

    def allowed(kind: str, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, g) for g in allow.get(kind, []))

    for rel, data in sorted(tree.items()):
        hits += [{"file": rel, "line": 0, "kind": k} for k in line_hits(rel.replace("/", " ")) if not allowed(k, rel)]  # the path
        if rel.lower().endswith((".png", ".jpg", ".jpeg")):
            hits += [{"file": rel, "line": 0, "kind": k} for k in image_metadata(rel, data) if not allowed("image", rel)]
            continue
        text = decode_for_scan(data)
        if text is None:
            continue  # the deny gate reports unscanned binaries
        for no, line in enumerate(text.splitlines(), 1):
            for kind in line_hits(line):
                if not allowed(kind, rel):
                    hits.append({"file": rel, "line": no, "kind": kind})
    return hits


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0])
    tree = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}
    hits = scan_tree(tree)
    if "--json" in args:
        print(json.dumps(hits, indent=1))
    else:
        for h in hits:
            print(f"{h['file']}:{h['line']} {h['kind']}")
        print(f"public scan: {len(hits)} hits in {len({h['file'] for h in hits})} files")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
