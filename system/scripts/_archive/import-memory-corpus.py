#!/usr/bin/env python3
"""Build the recall index that query-recall-index.py and generate-recall-pack.py read.

Writes system/recall/recall-index.sqlite3 on the main checkout (gitignored), an
SQLite FTS5 table `recall_documents(path, title, content)` over the durable
switchboard corpus:

    knowledge/, workstreams/, projects/, outputs/, routines/*/CONTEXT.md,
    docs/, people/ (profiles, not the template), notes/ (not notes/sensitive
    or notes/chrome-tabs), templates are skipped
    amp-mem observations (`~/bin/amp-mem export --format json`, private rows skipped)
    Claude Code memory files (~/.claude/projects/*/memory/*.md)

Generated surfaces are never indexed (.signals/, inbox/, assets/, latest.md,
system/state/), so synthesis is not read back as evidence.

The new index is built in a temporary file and swapped in at the end, so a
reader never sees a half-built table. Without --force the build is skipped
while the index is younger than --max-age-hours (default 20).

Usage:
    python3 system/scripts/_archive/import-memory-corpus.py [--force] [--max-age-hours N]
                                                            [--repo-root PATH] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

MAX_BYTES = 400_000  # one document; larger files are truncated, not skipped
ROOTS = ("knowledge", "workstreams", "projects", "outputs", "routines", "docs", "people", "notes")
SKIP_PARTS = {".signals", "inbox", "assets", "_template.md", "sensitive", "chrome-tabs",
              "todo-refresh", "node_modules", "__pycache__", ".obsidian", "kit-port"}


def main_checkout(start: Path) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import repo_paths

    return repo_paths.main_checkout(start.resolve())


def title_of(text: str, fallback: str) -> str:
    for line in text.splitlines()[:60]:
        stripped = line.strip()
        if stripped.startswith("title:"):
            return stripped.split(":", 1)[1].strip().strip("'\"") or fallback
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


def repo_documents(root: Path) -> Iterator[tuple[str, str, str]]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import private_workstreams

    private = private_workstreams.slugs(root)
    for rel in ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            rel_parts = path.relative_to(root).parts
            if SKIP_PARTS & set(rel_parts) or path.name == "latest.md":
                continue
            if private_workstreams.is_private_path(path.relative_to(root).as_posix(), private):
                continue
            if rel == "routines" and path.name != "CONTEXT.md":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_BYTES]
            except OSError:
                continue
            if text.strip():
                yield path.relative_to(root).as_posix(), title_of(text, path.stem), text


def amp_mem_documents() -> Iterator[tuple[str, str, str]]:
    exe = shutil.which("amp-mem") or str(Path.home() / "bin" / "amp-mem")
    if not Path(exe).exists() and not shutil.which("amp-mem"):
        return
    cmd = [exe, "export", "--format", "json"]
    if os.name == "nt" and not exe.lower().endswith((".exe", ".cmd", ".bat")):
        bash = shutil.which("bash")
        if not bash:
            return
        cmd = [bash, exe, "export", "--format", "json"]
    try:
        rows = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True).stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("private") or row.get("superseded_by"):
            continue
        body = "\n".join(str(row.get(k) or "") for k in ("topic", "summary", "tags", "project"))
        if body.strip():
            yield f"amp-mem/{row.get('id')}", str(row.get("topic") or f"observation {row.get('id')}"), body


def claude_memory_documents() -> Iterator[tuple[str, str, str]]:
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return
    for path in sorted(base.glob("*/memory/*.md")):
        if path.name == "MEMORY.md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_BYTES]
        except OSError:
            continue
        name = re.search(r"^name:\s*(.+)$", text, re.M)
        yield (f"claude-memory/{path.parent.parent.name}/{path.name}",
               name.group(1).strip() if name else path.stem, text)


def build(root: Path, db: Path) -> dict[str, int]:
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(f".{db.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    counts = {"repo": 0, "amp-mem": 0, "claude-memory": 0}
    try:
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("CREATE VIRTUAL TABLE recall_documents USING fts5(path, title, content, tokenize='porter unicode61')")
            for kind, source in (("repo", repo_documents(root)), ("amp-mem", amp_mem_documents()),
                                 ("claude-memory", claude_memory_documents())):
                for path, title, content in source:
                    conn.execute("INSERT INTO recall_documents(path, title, content) VALUES (?, ?, ?)", (path, title, content))
                    counts[kind] += 1
            conn.execute("INSERT INTO recall_documents(recall_documents) VALUES ('optimize')")
            conn.commit()
        finally:
            conn.close()
        for attempt in range(10):  # Windows: an open reader blocks the swap for a moment
            try:
                os.replace(tmp, db)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.5)
    finally:
        tmp.unlink(missing_ok=True)  # a failed build or swap leaves no half-built file
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the switchboard recall FTS index.")
    parser.add_argument("--force", action="store_true", help="rebuild even when the index is fresh")
    parser.add_argument("--max-age-hours", type=float, default=20.0)
    parser.add_argument("--repo-root", help="default: the main checkout of the repo in the current directory")
    parser.add_argument("--db", help="default: <repo-root>/system/recall/recall-index.sqlite3")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve() if args.repo_root else main_checkout(Path.cwd())
    db = Path(args.db) if args.db else root / "system" / "recall" / "recall-index.sqlite3"
    if not args.force and db.is_file() and (time.time() - db.stat().st_mtime) < args.max_age_hours * 3600:
        print(f"recall index fresh ({db}); pass --force to rebuild")
        return 0
    counts = build(root, db)
    print(f"recall index: {sum(counts.values())} documents "
          f"(repo {counts['repo']}, amp-mem {counts['amp-mem']}, claude-memory {counts['claude-memory']}) -> {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
