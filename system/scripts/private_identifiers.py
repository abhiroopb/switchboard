#!/usr/bin/env python3
"""Refuse commits that add the owner's private identifiers to tracked files.

`dispatch.py finish` and `dispatch.py auto-push` call guard_range() so a worker
cannot land or push a home street, account number, national ID, or VIN. The list
lives at local/private-identifiers.txt, gitignored. The kit ships this mechanism
and never the list, and a machine with no list keeps working.

The list holds salted PBKDF2 hashes, so it is not another plaintext copy. Hashing
slows guessing and keeps the values out of a grep or a stray upload. It does not
make a short value secret from someone determined who holds the file, so the file
stays private.

Matching is on normalized text: lowercase letters and digits with separators
dropped, so "12-345 678" and "12345678" are the same value. A line is split into
tokens and every run of up to WINDOW tokens is a candidate, plus the same runs over
its digit groups alone, which finds a number glued to letters. Only candidates of a
stored shape and length are hashed, which keeps a large landing fast.

What it reads: added lines, added paths, and commit messages of every commit in the
range, because a value added and then removed still lands in history. It does not
read binary files.

Refusals name the file and kind, never the value. A path that itself holds the value
is withheld.

A network name (Wi-Fi SSID) often has no digit and is no street. Kinds in
WORD_KINDS ("network") take a one-word value of at least MIN_WORD letters too, and it
is matched only against whole single words, so a landing hashes each distinct word of
a stored length once instead of every run of words. It is stored as shape "w" under its
word kind, which a guard from before this change parses and never matches (no street
suffix), so adding one never makes an older checkout's list unreadable. A word-only
name with spaces cannot go on the list; one with a digit can, under any separators.

Values go in on stdin, never on the command line:
    python3 system/scripts/private_identifiers.py add --kind street < values.txt
    python3 system/scripts/private_identifiers.py add --kind network < ssids.txt
    python3 system/scripts/private_identifiers.py status
    python3 system/scripts/private_identifiers.py scan-tree [ref]
    python3 system/scripts/private_identifiers.py scan-range <rev-range>
Retiring the guard on purpose takes deleting both the list and
.state/private-identifiers.seen. The second file is what lets a list that goes
missing later fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

LIST_RELPATH = Path("local") / "private-identifiers.txt"
STAMP_RELPATH = Path(".state") / "private-identifiers.seen"
FORMAT_LINE = "# switchboard private identifiers v1. Salted hashes only. Gitignored. Edit with private_identifiers.py."
# About 0.7 ms each on Windows. At 10,000 a 40-commit landing held the
# landing lock for 90 seconds, for little gain: nightly backups already hold plaintext
# copies of these values in the same gitignored folder.
ITERATIONS = 1_000
WINDOW = 6
MIN_DIGITS = 7   # shorter digit runs collide with ordinary numbers
MIN_MIXED = 6
WORD = re.compile(r"[^\W_]+")
DIGITS = re.compile(r"[0-9]+")
# A value with no digit must be a street name, because hashing every run of plain
# words would make a large landing crawl. Candidates must end in one of these.
MIN_WORD = 6
WORD_KINDS = frozenset({"network"})
STREET_SUFFIXES = frozenset(
    "street st avenue ave road rd drive dr boulevard blvd lane ln way court ct place pl terrace ter "
    "circle cir highway hwy parkway pkwy".split())
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
DIFF_PATHS = re.compile(r"^diff --git a/(.*) b/\1$")


class ListUnreadable(Exception):
    """The list is known to exist and cannot be used: callers fail closed."""


def normalize(value: str) -> str:
    return "".join(WORD.findall(value.casefold()))


def shape_of(normalized: str) -> str:
    if normalized.isdigit():
        return "d"
    return "a" if any(ch.isdigit() for ch in normalized) else "w"


def is_one_word(kind: str, raw: str) -> bool:
    return kind in WORD_KINDS and len(WORD.findall(raw.casefold())) == 1


def validate(normalized: str, kind: str = "", raw: str = "") -> str:
    """Why this normalized value cannot go on the list, or ""."""
    shape = shape_of(normalized)
    if shape == "w" and is_one_word(kind, raw or normalized):
        return "" if len(normalized) >= MIN_WORD else f"a one-word value needs at least {MIN_WORD} letters"
    if shape == "d" and len(normalized) < MIN_DIGITS:
        return f"a digit-only value needs at least {MIN_DIGITS} digits"
    if shape == "a" and len(normalized) < MIN_MIXED:
        return f"a mixed value needs at least {MIN_MIXED} characters"
    if shape == "w" and kind in WORD_KINDS:
        return "a word-only network name must be one word (no spaces or separators)"
    if shape == "w" and not any(normalized.endswith(suffix) for suffix in STREET_SUFFIXES):
        return "a value with no digit must be a street name ending in a street suffix"
    return ""


@dataclass
class IdentifierList:
    salt: bytes
    iterations: int = ITERATIONS
    entries: dict[str, str] = field(default_factory=dict)      # hash -> kind
    shapes: set[tuple[str, int]] = field(default_factory=set)  # (shape, length)
    word_lengths: set[int] = field(default_factory=set)        # lengths of one-word WORD_KINDS values

    def __post_init__(self) -> None:
        self._memo: dict[str, str | None] = {}
        self._word_memo: dict[str, str | None] = {}
        self._lengths = {length for _, length in self.shapes}
        self._word_lengths = set(self.word_lengths)
        self._max = max(self._lengths, default=0)

    def digest(self, normalized: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", normalized.encode("utf-8"), self.salt, self.iterations).hex()

    def _kind_of(self, candidate: str, last_token: str) -> str | None:
        if candidate in self._memo:
            return self._memo[candidate]
        shape = shape_of(candidate)
        if (shape, len(candidate)) not in self.shapes or (shape == "w" and last_token not in STREET_SUFFIXES):
            kind = None
        else:
            kind = self.entries.get(self.digest(candidate))
        self._memo[candidate] = kind
        return kind

    def _word_kind(self, token: str) -> str | None:
        if token not in self._word_memo:
            self._word_memo[token] = self.entries.get(self.digest(token))
        return self._word_memo[token]

    def kinds_in(self, text: str) -> set[str]:
        """Kinds of every listed value in one line of text."""
        if not self.entries:
            return set()
        found: set[str] = set()
        folded = text.casefold()
        if self._word_lengths:
            for token in set(WORD.findall(folded)):
                if len(token) in self._word_lengths and not any(ch.isdigit() for ch in token):
                    kind = self._word_kind(token)
                    if kind:
                        found.add(kind)
        for stream in (WORD.findall(folded), DIGITS.findall(folded)):
            for start in range(len(stream)):
                joined = ""
                for token in stream[start:start + WINDOW]:
                    joined += token
                    if len(joined) > self._max:
                        break
                    if len(joined) in self._lengths:
                        kind = self._kind_of(joined, token)
                        if kind:
                            found.add(kind)
        return found


def list_path(repo_root: Path) -> Path:
    override = os.environ.get("SWITCHBOARD_PRIVATE_IDENTIFIERS")
    return Path(override) if override else Path(repo_root) / LIST_RELPATH


def stamp_path(repo_root: Path) -> Path:
    return Path(repo_root) / STAMP_RELPATH


def parse_list(text: str) -> IdentifierList:
    salt, iterations, count = None, ITERATIONS, None
    entries: dict[str, str] = {}
    shapes: set[tuple[str, int]] = set()
    word_lengths: set[int] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "salt" and len(parts) == 2:
            salt = bytes.fromhex(parts[1])
        elif parts[0] == "iterations" and len(parts) == 2:
            iterations = int(parts[1])
        elif parts[0] == "count" and len(parts) == 2:
            count = int(parts[1])
        elif parts[0] == "entry" and len(parts) == 5 and parts[2] in {"d", "a", "w"}:
            entries[parts[4]] = parts[1]
            shapes.add((parts[2], int(parts[3])))
            if parts[2] == "w" and parts[1] in WORD_KINDS:
                word_lengths.add(int(parts[3]))
        else:
            raise ValueError("unrecognized line in the list")
    if not salt or iterations < 1:
        raise ValueError("the list has no salt or iterations")
    if count is None or count != len(entries):
        raise ValueError("the list is truncated: its count does not match its entries")
    return IdentifierList(salt=salt, iterations=iterations, entries=entries, shapes=shapes, word_lengths=word_lengths)


def load_list(repo_root: Path) -> IdentifierList | None:
    """The list, or None when this machine has none. Raises ListUnreadable when a
    list is known to exist (the path is there, or was loaded here before) and
    cannot be read, so a sync hiccup or a deleted file never turns the guard off."""
    path, stamp = list_path(repo_root), stamp_path(repo_root)
    if not os.path.lexists(path):
        if stamp.exists():
            raise ListUnreadable(f"{LIST_RELPATH} loaded on this machine before and is now missing")
        return None
    try:
        loaded = parse_list(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ListUnreadable(f"{LIST_RELPATH} exists and cannot be read ({type(exc).__name__})") from exc
    if loaded.entries:
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(f"{len(loaded.entries)}\n", encoding="utf-8")
        except OSError:
            pass
    return loaded


def write_list(path: Path, loaded: IdentifierList, kinds_shapes: dict[str, tuple[str, str, int]]) -> None:
    """kinds_shapes: hash -> (kind, shape, length)."""
    lines = [FORMAT_LINE, f"salt {loaded.salt.hex()}", f"iterations {loaded.iterations}", f"count {len(kinds_shapes)}"]
    lines += [f"entry {kind} {shape} {length} {digest}" for digest, (kind, shape, length) in sorted(kinds_shapes.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".private-identifiers.")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def read_entry_shapes(path: Path) -> dict[str, tuple[str, str, int]]:
    rows: dict[str, tuple[str, str, int]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) == 5 and parts[0] == "entry":
            rows[parts[4]] = (parts[1], parts[2], int(parts[3]))
    return rows


def add_values(repo_root: Path, kind: str, values: Iterable[str]) -> tuple[int, int]:
    """Add values under `kind`. Returns (added, rejected). Never echoes a value."""
    if not re.fullmatch(r"[a-z_]+", kind):
        raise ValueError("kind must be lowercase letters and underscores")
    path = list_path(repo_root)
    if path.exists():
        current = parse_list(path.read_text(encoding="utf-8"))
        rows = read_entry_shapes(path)
    else:
        current, rows = IdentifierList(salt=secrets.token_bytes(32)), {}
    added = rejected = 0
    for value in values:
        normalized = normalize(value)
        if not normalized:
            continue
        if validate(normalized, kind, value.strip()):
            rejected += 1
            continue
        digest = current.digest(normalized)
        if digest not in rows:
            rows[digest] = (kind, shape_of(normalized), len(normalized))
            added += 1
    write_list(path, current, rows)
    return added, rejected


@dataclass
class Finding:
    kind: str
    path: str       # file path, "(commit message)", or "(path withheld)"
    commit: str = ""


def git_stream(repo_root: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen(["git", "-C", str(repo_root), "-c", "core.quotepath=off", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def scan_log(lines: Iterable[str], listed: IdentifierList) -> Iterator[Finding]:
    """Walk `git log -p -U0` output: commit messages, added paths, added lines."""
    commit, in_message = "", False
    path, withheld, deleted = "", False, False
    old_left = new_left = 0
    pending_header = ""

    def header_findings() -> Iterator[Finding]:
        nonlocal withheld
        if pending_header and not deleted:
            for kind in sorted(listed.kinds_in(pending_header)):
                withheld = True
                yield Finding(kind, "(path withheld)", commit)

    for line in lines:
        if line.startswith("\0C "):
            yield from header_findings()
            pending_header = ""
            commit, in_message = line[3:].strip()[:7], True
            continue
        if line.startswith("\0E"):
            in_message = False
            continue
        if in_message:
            for kind in sorted(listed.kinds_in(line)):
                yield Finding(kind, "(commit message)", commit)
            continue
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                new_left -= 1
                for kind in sorted(listed.kinds_in(line[1:])):
                    yield Finding(kind, "(path withheld)" if withheld else path, commit)
            elif line.startswith("-"):
                old_left -= 1
            continue
        if line.startswith("diff --git "):
            # The previous file may have had no hunks (empty or binary): read its name now.
            yield from header_findings()
            match = DIFF_PATHS.match(line.rstrip("\r\n"))
            path = match.group(1) if match else "(unparsed path)"
            pending_header, withheld, deleted = (match.group(1) if match else line), False, False
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("@@ "):
            yield from header_findings()
            pending_header = ""
            hunk = HUNK.match(line)
            if hunk:
                old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
                new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
        elif line.startswith("+++ b/"):
            path = line[6:].rstrip("\r\n")
        elif line.startswith(("index ", "new file mode", "old mode", "new mode", "--- ", "+++ ", "Binary files", "\\")):
            continue
    yield from header_findings()


def scan_range(repo_root: Path, rev_range: str, listed: IdentifierList) -> list[Finding]:
    """Findings over every commit in `rev_range`. Raises on a git failure."""
    proc = git_stream(repo_root, "log", "--no-color", "-p", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames",
                      "--submodule=short", "--diff-merges=first-parent", "--src-prefix=a/", "--dst-prefix=b/",
                      "--format=%x00C %H%n%B%n%x00E", rev_range, "--")
    with proc:
        decoded = (raw.decode("utf-8", "replace") for raw in proc.stdout)
        findings = list(scan_log(decoded, listed))
        stderr = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait()
    if code:
        raise RuntimeError(f"git log failed: {stderr.strip()[:200]}")
    return findings


def scan_tree(repo_root: Path, ref: str, listed: IdentifierList, include_binary: bool = False) -> list[Finding]:
    """One Finding per matching line or path across every tracked file at `ref`."""
    tree = subprocess.run(["git", "-C", str(repo_root), "-c", "core.quotepath=off", "ls-tree", "-r", "-z", ref],
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    findings: list[Finding] = []
    cat = subprocess.Popen(["git", "-C", str(repo_root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for row in filter(None, tree.split("\0")):
            meta, path = row.split("\t", 1)
            _, kind_of_obj, sha = meta.split()
            if kind_of_obj != "blob":
                continue
            path_kinds = sorted(listed.kinds_in(path))
            shown = "(path withheld)" if path_kinds else path
            findings += [Finding(kind, shown) for kind in path_kinds]
            cat.stdin.write(sha.encode() + b"\n")
            cat.stdin.flush()
            size = int(cat.stdout.readline().split()[2])
            blob = cat.stdout.read(size)
            cat.stdout.read(1)
            if b"\0" in blob[:8192] and not include_binary:
                continue
            text = blob.decode("latin-1" if b"\0" in blob[:8192] else "utf-8", "replace")
            for line in text.splitlines():
                findings += [Finding(kind, shown) for kind in sorted(listed.kinds_in(line))]
    finally:
        cat.stdin.close()
        cat.wait()
    return findings


WITHHELD_LINE = "(line withheld: it named a private identifier)"
_LINE_LEAD = re.compile(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+|#+\s+|>\s*)?)")


def redact_bytes(raw: bytes, listed: IdentifierList) -> tuple[bytes, list[str]]:
    """(raw with every line that holds a listed value replaced by WITHHELD_LINE, the kinds
    found). Lines split on b"\\n" only and are decoded just to be tested, so every other byte
    (CRLF, invalid UTF-8, U+2028) stays exactly as it was. The line's list or heading markup
    stays, so a Markdown note keeps its shape. Close-out and result-card commits can write
    to a CONTEXT.md on main past the guard that finish runs, so this redacts them too."""
    kinds: list[str] = []
    out: list[bytes] = []
    for line in raw.split(b"\n"):
        body = line[:-1] if line.endswith(b"\r") else line
        text = body.decode("utf-8", "replace")
        found = listed.kinds_in(text)
        if found:
            kinds += sorted(found)
            lead = _LINE_LEAD.match(text).group(1)
            out.append((lead + WITHHELD_LINE).encode("utf-8") + line[len(body):])
        else:
            out.append(line)
    return b"\n".join(out), kinds


def redact_text(text: str, listed: IdentifierList) -> tuple[str, list[str]]:
    """redact_bytes for text already decoded."""
    new, kinds = redact_bytes(text.encode("utf-8"), listed)
    return new.decode("utf-8"), kinds


def redact_files(repo_root: Path, rels: Iterable[str]) -> dict:
    """Redact listed values in the named files on disk before they are committed.
    {status: absent | clean | redacted | error, kinds, paths}. `error` means the list is
    known to exist and cannot be read: the caller must not commit."""
    try:
        listed = load_list(Path(repo_root))
    except ListUnreadable as exc:
        return {"status": "error", "kinds": [], "paths": [], "reason": str(exc)}
    if listed is None:
        return {"status": "absent", "kinds": [], "paths": []}
    kinds: list[str] = []
    paths: list[str] = []
    for rel in rels:
        path = Path(repo_root) / rel
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        new, found = redact_bytes(raw, listed)
        if found:
            tmp = path.with_name(f"{path.name}.{os.getpid()}.redact.tmp")
            tmp.write_bytes(new)
            os.replace(tmp, path)
            kinds += found
            paths.append(rel)
    return {"status": "redacted" if paths else "clean", "kinds": sorted(set(kinds)), "paths": paths}


def summarize(findings: Iterable[Finding], limit: int = 5) -> str:
    """'path (kind), path (kind)…' Names the file and kind, never the value."""
    pairs = sorted({(f.path, f.kind) for f in findings})
    shown = ", ".join(f"{path} ({kind})" for path, kind in pairs[:limit])
    return shown + ("…" if len(pairs) > limit else "")


def guard_range(repo_root: Path, rev_range: str) -> dict:
    """{status: absent | clean | hit | error, findings, reason}. `error` means the
    list is known to exist and the range could not be checked: refuse."""
    try:
        listed = load_list(Path(repo_root))
    except ListUnreadable as exc:
        return {"status": "error", "findings": [], "reason": str(exc)}
    if listed is None:
        return {"status": "absent", "findings": [], "reason": ""}
    try:
        findings = scan_range(Path(repo_root), rev_range, listed)
    except (OSError, RuntimeError) as exc:
        return {"status": "error", "findings": [], "reason": f"could not read {rev_range} ({type(exc).__name__})"}
    if findings:
        return {"status": "hit", "findings": findings, "reason": summarize(findings)}
    return {"status": "clean", "findings": [], "reason": ""}


def repo_root_from_cwd() -> Path:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    from repo_paths import main_checkout
    return main_checkout(Path(top))


def print_counts(findings: list[Finding]) -> None:
    counts = Counter((f.path, f.kind) for f in findings)
    print(json.dumps({"matches": sum(counts.values()), "files": len({p for p, _ in counts}),
                      "by_file": [{"path": p, "kind": k, "count": n} for (p, k), n in sorted(counts.items())]}, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add values from stdin, one per line; never echoed")
    add.add_argument("--kind", required=True)
    sub.add_parser("status")
    tree = sub.add_parser("scan-tree")
    tree.add_argument("ref", nargs="?", default="main")
    tree.add_argument("--include-binary", action="store_true")
    span = sub.add_parser("scan-range")
    span.add_argument("rev_range")
    args = parser.parse_args()
    root = repo_root_from_cwd()
    if args.command == "add":
        added, rejected = add_values(root, args.kind, sys.stdin)
        print(json.dumps({"added": added, "rejected": rejected}))
        return 0
    try:
        listed = load_list(root)
    except ListUnreadable as exc:
        print(json.dumps({"status": "unreadable", "reason": str(exc)}))
        return 2
    if args.command == "status":
        if listed is None:
            print(json.dumps({"status": "absent"}))
        else:
            print(json.dumps({"status": "loaded", "kinds": Counter(listed.entries.values()), "iterations": listed.iterations}))
        return 0
    if listed is None:
        print(json.dumps({"status": "absent"}))
        return 0
    if args.command == "scan-tree":
        found = scan_tree(root, args.ref, listed, include_binary=args.include_binary)
    else:
        found = scan_range(root, args.rev_range, listed)
    print_counts(found)
    return 3 if found else 0


if __name__ == "__main__":
    sys.exit(main())
