#!/usr/bin/env python3

from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "system" / "recall" / "recall-index.sqlite3"


def _main_checkout_db() -> Path:
    """The index lives on the main checkout; a task worktree reads that one."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import repo_paths

    return repo_paths.main_checkout(REPO_ROOT) / "system" / "recall" / "recall-index.sqlite3"


if not DEFAULT_DB.is_file():
    DEFAULT_DB = _main_checkout_db()


def open_db(path: Path) -> sqlite3.Connection:
    """Open an existing recall index without creating files or schema."""
    if not path.is_file():
        raise FileNotFoundError(
            f"recall index not found at {path}; rebuild it with "
            "system/scripts/_archive/import-memory-corpus.py --force"
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.execute("SELECT 1 FROM recall_documents LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise RuntimeError(
            f"recall index at {path} is invalid; rebuild it with "
            "system/scripts/_archive/import-memory-corpus.py --force"
        ) from exc
    return conn


def tokenize_query(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_]+", value.lower()) if token]


def build_query_candidates(value: str) -> list[str]:
    tokens = tokenize_query(value)
    if not tokens:
        return ['"__no_results__"']

    candidates: list[str] = []
    if len(tokens) > 1:
        phrase = " ".join(tokens)
        candidates.append(f'"{phrase}"')
        candidates.append(" AND ".join(f'"{token}"' for token in tokens))
    candidates.append(" OR ".join(f'"{token}"' for token in tokens))

    unique: list[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def run_query(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT path, title, snippet(recall_documents, 2, '[', ']', ' … ', 16) "
        "FROM recall_documents WHERE recall_documents MATCH ? "
        "ORDER BY bm25(recall_documents), path COLLATE BINARY, title COLLATE BINARY LIMIT ?",
        (query, limit),
    ).fetchall()
    return [
        {"path": row[0], "title": row[1], "snippet": row[2]}
        for row in rows
    ]


def query_index(conn: sqlite3.Connection, value: str, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    matches: list[dict] = []
    seen: set[str] = set()
    for candidate in build_query_candidates(value):
        # Backfill exact phrases with all-term matches. Broad OR fallback is
        # useful only when neither precision tier matched, not as extra noise.
        if matches and " OR " in candidate:
            break
        for row in run_query(conn, candidate, limit):
            if row["path"] in seen:
                continue
            matches.append(row)
            seen.add(row["path"])
            if len(matches) == limit:
                return matches
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the switchboard recall FTS index.")
    parser.add_argument("query")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    try:
        conn = open_db(Path(args.db))
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    with closing(conn):
        print(json.dumps(query_index(conn, args.query, args.limit), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
