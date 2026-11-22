#!/usr/bin/env python3
"""Fail when company content shows up where it must not: on a personal install, or in a kit tree.

The terms come from a deny list that stays on the owner's machine, never from this file, so the
check itself ships in the kit without naming any company. Default list: local/kit-deny-list.txt
(gitignored); --deny-list PATH or PM_OS_KIT_DENY_LIST overrides it.

  check-profile-leaks.py            errors only; exit 1 when any
  check-profile-leaks.py --all      also list warnings
  check-profile-leaks.py --json
  check-profile-leaks.py --kit      kit gate: every deny-list hit anywhere is an error; exit 2 with no list
  check-profile-leaks.py --kit --root <built-tree>   gate a kit build from the owner's checkout

Install mode (no --kit). Instruction surfaces (anything an agent reads as policy: AGENTS.md,
CLAUDE.md, adapters/, system/prompts/, skill and command files, sub-agent personas, routine CONTEXT
files, config) must be clean of `term` rules: a hit there is an error. Everywhere else (scripts,
tests, comments, history docs) a hit is a warning, listed with --all. On the company profile
(PM_OS_PROFILE or .pm-os-kit/profile.json) the check skips the files that profile installed.

Kit mode (--kit), for a kit build or publisher, run on the built tree. Every text file is read,
tests and vendored files included, and any `term` or `kit` hit is an error. Owner-local paths,
owner workstream data and any `path` rule fail too, so content that must never ship fails the
gate when it reaches a kit tree. A kit run with no deny list fails closed (exit 2).

Deny list format, one rule per line, `#` starts a comment:

  term  <category> <regex>      both modes (instruction error, elsewhere warning; kit: error)
  kit   <category> <regex>      kit mode only: an error in any file
  path  <category> <glob>       kit mode only: this path must not exist in a kit tree
  allow <glob> <regex>          skip hits on lines of matching files that also match <regex>
  allowterm <glob> <regex>      skip a hit in matching files when the matched text matches <regex>
  names <category> <dir>        kit mode only: every <first-last>.md in <dir> (relative to the
                                folder that holds local/) becomes a whole-name rule, "First Last"

Regexes are Python, case-sensitive unless they carry (?i). Categories are short labels (for
example A for company names); kit-mode output counts hits per category.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DENY_LIST_ENV = "PM_OS_KIT_DENY_LIST"
DEFAULT_DENY_LIST = "local/kit-deny-list.txt"
INSTRUCTION = ["AGENTS.md", "CLAUDE.md", "HOME.md", "adapters/*", "system/prompts/*", ".agents/skills/*/SKILL.md",
               ".agents/skills/*/references/*", "sub-agents/*", "routines/*/CONTEXT.md", "routines/*/config.yaml",
               "system/config/*", "system/claude-marketplace/plugins/*/commands/*",
               "system/claude-marketplace/plugins/*/agents/*", "templates/*", "system/chief-of-staff-prompt.md",
               "system/scheduled-jobs.yaml", "system/background-tasks.yaml"]
SKIP = ["profiles/*", "docs/kit-*", "docs/profiles.md", "system/vendor/*", "*test*", "*/fixtures/*",
        "system/scripts/check-profile-leaks.py", ".pm-os-kit/*", "workstreams/*", "notes/*", "people/*", "outputs/*"]
# Kit mode reads these too, except the manifest (hashes only) and git internals.
KIT_SKIP = [".pm-os-kit/source-manifest.json", ".git/*"]

# Owner-local paths: they may hold anything on the owner's machine and must never reach a kit tree.
OWNER_LOCAL = ["local/*", "system/config/private-workstreams.txt", "system/scripts/test_private_workstreams.py"]
# Owner data on a kit tree: any workstream file (the kit ships only the README). --kit fails on these.
KIT_OWNER_DATA = ["workstreams/*"]
KIT_ALLOWED = {"workstreams/README.md"}
# Names one owner workstream: a .gitignore line, or a row in the workstreams table.
OWNER_NAMING = {".gitignore": re.compile(r"^!?/?workstreams/(?:_archive/)?[A-Za-z0-9][A-Za-z0-9-]*/"),
                "workstreams/README.md": re.compile(r"\]\(\./[^)]+/\)")}
NAME_FILE = re.compile(r"^([a-z][a-z'À-ɏ]+)-([a-z][a-z'À-ɏ-]+)\.md$")


class DenyListError(ValueError):
    pass


class DenyList:
    def __init__(self) -> None:
        self.terms: list[tuple[str, re.Pattern]] = []
        self.kit: list[tuple[str, re.Pattern]] = []
        self.paths: list[tuple[str, str]] = []
        self.allow: list[tuple[str, re.Pattern]] = []
        self.allow_terms: list[tuple[str, re.Pattern]] = []
        self.source = ""

    def allowed(self, rel: str, line: str, term: str) -> bool:
        """A hit is skipped when an allow rule matches its line, or an allowterm rule matches its text."""
        return (any(fnmatch.fnmatch(rel, g) and rx.search(line) for g, rx in self.allow)
                or any(fnmatch.fnmatch(rel, g) and rx.search(term) for g, rx in self.allow_terms))


def deny_list_path(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get(DENY_LIST_ENV) or str(ROOT / DEFAULT_DENY_LIST)
    return Path(raw).expanduser()


def load_deny_list(path: Path) -> DenyList:
    deny = DenyList()
    deny.source = str(path)
    base = path.resolve().parent.parent  # the folder that holds local/
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise DenyListError(f"{path}:{no}: want '<kind> <category|glob> <value>'")
        kind, second, value = parts
        try:
            if kind in ("term", "kit"):
                (deny.terms if kind == "term" else deny.kit).append((second, re.compile(value)))
            elif kind == "path":
                deny.paths.append((second, value))
            elif kind in ("allow", "allowterm"):
                (deny.allow if kind == "allow" else deny.allow_terms).append((second, re.compile(value)))
            elif kind == "names":
                folder = (base / value) if not Path(value).is_absolute() else Path(value)
                names = sorted({f"{m.group(1)} {m.group(2).replace('-', ' ')}"
                                for p in (folder.glob("*.md") if folder.is_dir() else [])
                                if (m := NAME_FILE.match(p.name))})
                if names:
                    rx = r"(?i)\b(?:" + "|".join(re.escape(n).replace(r"\ ", r"[\s_-]+") for n in names) + r")\b"
                    deny.kit.append((second, re.compile(rx)))
            else:
                raise DenyListError(f"{path}:{no}: unknown kind {kind!r}")
        except re.error as exc:
            raise DenyListError(f"{path}:{no}: bad regex: {exc}") from exc
    return deny


def _walk() -> list[str]:
    return [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def _git_toplevel() -> Path | None:
    out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None


def tracked() -> list[str]:
    # An extracted kit has no .git, and a built tree inside another checkout's ignored folder has
    # that checkout's .git: git lists nothing there, and an empty list would pass the check. Both walk.
    top = _git_toplevel()
    if top is None or top != ROOT.resolve():
        return _walk()
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "-z", "--exclude-standard"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode:
        return _walk()
    # -z: a space or non-ASCII name stays one whole path. A path deleted from the working tree but
    # still in the index is gone from what ships, so it is not judged.
    files = [p for p in out.stdout.split("\0") if p and (ROOT / p).exists()]
    return files or _walk()


def profile_files() -> set[str]:
    try:
        rec = json.loads((ROOT / ".pm-os-kit" / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(rec.get("files") or []) if rec.get("profile") not in (None, "", "personal") else set()


def matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _hit(rel: str, no: int, term: str, text: str, category: str = "") -> dict:
    return {"file": rel, "line": no, "term": term, "text": text.strip()[:140], "category": category}


def scan(kit: bool = False, deny: DenyList | None = None) -> tuple[list[dict], list[dict]]:
    deny = deny or DenyList()
    installed = profile_files()
    errors, warnings = [], []
    kit_rules = deny.terms + deny.kit
    for rel in tracked():
        if matches(rel, OWNER_LOCAL):
            if kit:
                errors.append(_hit(rel, 0, "owner-local path", "this path must not ship", "path"))
            continue
        if kit and matches(rel, KIT_OWNER_DATA) and rel not in KIT_ALLOWED:
            errors.append(_hit(rel, 0, "owner data", "workstream files must not ship", "path"))
            continue
        if kit:
            denied = next((c for c, g in deny.paths if fnmatch.fnmatch(rel, g)), None)
            if denied is not None:
                errors.append(_hit(rel, 0, "denied path", "this path must not ship", denied))
                continue
            if matches(rel, KIT_SKIP):
                continue
        elif matches(rel, SKIP) or rel in installed or any(fnmatch.fnmatch(rel, g) for _, g in deny.paths):
            continue  # a denied path is owner-local: it may hold anything on the owner's machine
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not kit and rel in ("AGENTS.md", "adapters/SYSTEM.md") and "pm-os:profile-section:start" in text and installed:
            text = text.split("pm-os:profile-section:start", 1)[0]
        rules = kit_rules if kit else deny.terms
        if not rules:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            hit = next((_hit(rel, no, m.group(0), line, category) for category, rx in rules for m in rx.finditer(line)
                        if not deny.allowed(rel, line, m.group(0))), None)  # first hit no allow rule covers
            if hit:
                (errors if kit or matches(rel, INSTRUCTION) else warnings).append(hit)
    if kit:
        for rel, pattern in OWNER_NAMING.items():
            try:
                text = (ROOT / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    errors.append(_hit(rel, no, "owner workstream", "names an owner workstream", "path"))
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kit", action="store_true", help="kit gate: scan everything; fails closed without a deny list")
    ap.add_argument("--deny-list", help=f"deny list file (default ${DENY_LIST_ENV}, else {DEFAULT_DENY_LIST})")
    ap.add_argument("--root", help="tree to scan (default: this checkout); a kit build passes its built tree")
    args = ap.parse_args(argv)
    global ROOT
    default_list = deny_list_path(args.deny_list)  # resolved against this checkout, before --root moves ROOT
    if args.root:
        ROOT = Path(args.root).resolve()
    path = default_list
    deny = None
    if path.is_file():
        try:
            deny = load_deny_list(path)
        except (OSError, DenyListError) as exc:
            print(f"deny list unreadable: {exc}", file=sys.stderr)
            return 2
    elif args.kit or args.deny_list:
        print(f"deny list not found: {path} (pass --deny-list or set {DENY_LIST_ENV}); a kit gate never passes "
              "without one", file=sys.stderr)
        return 2
    errors, warnings = scan(kit=args.kit, deny=deny)
    if args.json:
        print(json.dumps({"deny_list": deny.source if deny else None, "errors": errors,
                          "warnings": warnings if args.all else len(warnings)}, indent=1))
    else:
        for e in errors:
            cat = f"{e['category']} " if e.get("category") else ""
            print(f"ERROR {e['file']}:{e['line']} [{cat}{e['term']}] {e['text']}")
        if args.all:
            for w in warnings:
                print(f"warn  {w['file']}:{w['line']} [{w['term']}] {w['text']}")
        files = len({e['file'] for e in errors})
        if args.kit:
            counts: dict[str, int] = {}
            for e in errors:
                counts[e.get("category") or "?"] = counts.get(e.get("category") or "?", 0) + 1
            by = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
            print(f"kit gate: {len(errors)} errors in {files} files (by category: {by}); deny list {path}")
        else:
            note = "" if deny else f"; no deny list at {path}, so no term rules ran"
            print(f"profile leaks: {len(errors)} in {files} instruction files, {len(warnings)} mentions elsewhere{note}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
