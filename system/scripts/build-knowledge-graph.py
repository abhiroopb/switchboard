#!/usr/bin/env python3
"""Build the switchboard structural knowledge graph with graphify, no LLM.

Produces graphify-out/graph.json (gitignored) at the repo root from:
  * Markdown notes in the vault (root-level *.md, workstreams/, projects/,
    knowledge/, outputs/, docs/, routines/, templates/, sub-agents/, system/),
    minus the private corpora listed under SKIP_PARTS and SENSITIVE_* below, and
  * code under system/ only.

Why the Python API and not `graphify update`: `graphify extract` drops every
Markdown file under GRAPHIFY_BACKEND=none, and `graphify update` can only
exclude by path (.graphifyignore). The privacy filters below (skip files marked
sensitive, keep only `title` from frontmatter, drop any node whose label holds
an email address) need the in-process graph before export. The script pins the
graphify version it was verified against and warns on drift. It re-executes
itself with the graphify tool venv's interpreter so the system python3 needs
nothing installed.

Privacy: GRAPHIFY_BACKEND=none disables semantic extraction, LLM dedup,
community labeling and the silent `claude -p` labeling fallback. The query log
is disabled. Nothing leaves the process. Excluded on purpose: people/,
notes/ (daily journal, sensitive notes, chrome tabs), every inbox/ and _inbox/
folder (raw Slack and Gmail captures), meetings/ (verbatim meeting notes),
sources/ and slack-live/ (raw ingests), and any note with `sensitive: true`
frontmatter or `[SENSITIVE]` in its first lines.

Usage:
  python3 system/scripts/build-knowledge-graph.py            # full rebuild
  python3 system/scripts/build-knowledge-graph.py --dry-run  # list counts only
  python3 system/scripts/build-knowledge-graph.py --out DIR  # alternate output dir
  python3 system/scripts/build-knowledge-graph.py --if-installed  # scheduled: skip quietly without graphify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _graphify_python() -> Path:
    """The graphifyy uv tool venv interpreter. SWITCHBOARD_GRAPHIFY_PYTHON wins; then
    UV_TOOL_DIR and `uv tool dir`; then the default tool dirs (macOS/Linux
    ~/.local/share/uv/tools, Windows %APPDATA%/uv/tools). Windows venvs keep
    the interpreter in Scripts/python.exe, POSIX venvs in bin/python."""
    override = os.environ.get("SWITCHBOARD_GRAPHIFY_PYTHON")
    if override:
        return Path(override)
    tool_dirs: list[Path] = []
    if os.environ.get("UV_TOOL_DIR"):
        tool_dirs.append(Path(os.environ["UV_TOOL_DIR"]))
    try:
        out = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            tool_dirs.append(Path(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    if os.environ.get("APPDATA"):
        tool_dirs.append(Path(os.environ["APPDATA"]) / "uv" / "tools")
    tool_dirs.append(Path.home() / ".local" / "share" / "uv" / "tools")
    rel = [Path("Scripts") / "python.exe", Path("bin") / "python"] if os.name == "nt" else [Path("bin") / "python"]
    for base in tool_dirs:
        for r in rel:
            cand = base / "graphifyy" / r
            if cand.exists():
                return cand
    return tool_dirs[-1] / "graphifyy" / rel[-1]
# The build imports graphify's Python modules (extract, build, cluster, export),
# which are not a public API. This is the version those imports were verified on.
TESTED_GRAPHIFY_VERSION = "0.9.95"

# Directory names that never enter the graph, at any depth.
SKIP_PARTS = {
    "_archive", "assets", "sources", "slack-live", "payload", "node_modules",
    "graphify-out", ".git", ".amp", ".state", ".firecrawl", ".obsidian",
    ".claude", ".config-mirror", ".intersect", ".ruff_cache", "__pycache__",
    "episodes",
    # Private corpora: raw captures and personal notes.
    "people", "notes", "inbox", "_inbox", "meetings", "daily-beta", "sensitive",
}
# Markdown is collected from these top-level directories (plus root *.md).
MARKDOWN_DIRS = [
    "workstreams", "projects", "knowledge", "outputs", "docs",
    "routines", "templates", "sub-agents", "system",
]
# A note is skipped when its first lines carry either marker.
SENSITIVE_FRONTMATTER = re.compile(r"^sensitive:\s*(true|yes)\s*$", re.IGNORECASE | re.MULTILINE)
SENSITIVE_TITLE = re.compile(r"\[SENSITIVE\]", re.IGNORECASE)
SENSITIVE_HEAD_BYTES = 4096
# Any node whose label, id or rationale holds an address is dropped before export.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Frontmatter keys kept on page nodes; everything else (tags, people, status
# notes, source URLs) stays in the file.
FRONTMATTER_KEEP = {"title"}
# Non-Markdown (code) files are collected from these directories only.
CODE_DIRS = ["system"]

ENV = {"GRAPHIFY_BACKEND": "none", "GRAPHIFY_QUERY_LOG_DISABLE": "1"}

def _os_replace(src, dst):
    """os.replace, retried for up to 5 s on Windows while another process has dst open."""
    for attempt in range(50):
        try:
            result = os.replace(src, dst)
            if os.name == "nt":   # refresh worktree hard links of a shared file (lib/winlink.py)
                try:
                    from lib import winlink as _winlink
                    _winlink.relink_shared(dst)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            __import__("time").sleep(0.1)

def repo_root() -> Path:
    out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
    return Path(out.strip()).resolve()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def head_commit(root: Path) -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, text=True).strip()
    except Exception:
        return None


def is_sensitive(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(SENSITIVE_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return True
    return bool(SENSITIVE_FRONTMATTER.search(head) or SENSITIVE_TITLE.search(head))


def collect(root: Path) -> tuple[list[Path], list[Path], int]:
    """Return (markdown files, code files, sensitive notes skipped) honouring
    SKIP_PARTS. Symlinked dirs are skipped by graphify's collector; root-level
    *.md are added by hand because collect_files walks directories only."""
    from graphify.extract import collect_files  # type: ignore

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import private_workstreams

    private = private_workstreams.slugs(root)

    def keep(p: Path) -> bool:
        rel = p.relative_to(root)
        return not (set(rel.parts) & SKIP_PARTS) and not private_workstreams.is_private_path(rel.as_posix(), private)

    md: list[Path] = []
    code: list[Path] = []
    skipped_sensitive = 0

    def add_markdown(p: Path) -> None:
        nonlocal skipped_sensitive
        if is_sensitive(p):
            skipped_sensitive += 1
            return
        md.append(p)

    for p in sorted(root.glob("*.md")):
        if p.is_file():
            add_markdown(p)
    for name in MARKDOWN_DIRS:
        base = root / name
        if base.is_symlink() or not base.is_dir():
            continue
        for p in collect_files(base, root=root):
            if not keep(p):
                continue
            if p.suffix.lower() == ".md":
                add_markdown(p)
            elif name in CODE_DIRS:
                code.append(p)
    return md, code, skipped_sensitive


def redact(graph) -> dict:
    """Strip frontmatter to FRONTMATTER_KEEP and drop every node that carries an
    email address. Returns counts for build.json."""
    dropped = []
    trimmed = 0
    for node_id, data in graph.nodes(data=True):
        fm = data.get("frontmatter")
        if isinstance(fm, dict):
            kept = {k: v for k, v in fm.items() if k in FRONTMATTER_KEEP}
            if kept != fm:
                trimmed += 1
            data["frontmatter"] = kept
        text = " ".join(str(data.get(k) or "") for k in ("label", "rationale", "norm_label", "frontmatter")) + " " + str(node_id)
        if EMAIL.search(text):
            dropped.append(node_id)
    graph.remove_nodes_from(dropped)
    return {"nodes_dropped_email": len(dropped), "frontmatter_trimmed": trimmed}


def build(root: Path, out_dir: Path, dry_run: bool) -> dict:
    os.environ.update(ENV)
    from graphify.extract import extract  # type: ignore
    from graphify.build import build as build_graph  # type: ignore
    from graphify.cluster import cluster  # type: ignore
    from graphify.export import to_json  # type: ignore
    from importlib.metadata import version as pkg_version

    try:
        graphify_version = pkg_version("graphifyy")
    except Exception:
        graphify_version = None
    if graphify_version != TESTED_GRAPHIFY_VERSION:
        print(f"warning: graphify {graphify_version} installed, builder verified on "
              f"{TESTED_GRAPHIFY_VERSION}; check build.json counts after this run", file=sys.stderr)

    t0 = time.time()
    md, code, skipped_sensitive = collect(root)
    files = md + code
    stamp: dict = {
        "started_at": utc_now(),
        "repo_root": str(root),
        "commit": head_commit(root),
        "graphify_version": graphify_version,
        "tested_graphify_version": TESTED_GRAPHIFY_VERSION,
        "backend": ENV["GRAPHIFY_BACKEND"],
        "files": {"markdown": len(md), "code": len(code), "skipped_sensitive": skipped_sensitive},
        "excluded_dirs": sorted(SKIP_PARTS),
    }
    if dry_run:
        stamp["dry_run"] = True
        return stamp

    out_dir.mkdir(parents=True, exist_ok=True)
    # graphify appends "graphify-out/cache" to cache_root itself.
    extracted = extract(files, cache_root=out_dir.parent, root=root)
    graph = build_graph([extracted], directed=True, root=root)
    redaction = redact(graph)
    communities = cluster(graph)
    graph_path = out_dir / "graph.json"
    # Atomic swap: queries read graph.json while the scheduler rebuilds it.
    tmp_path = out_dir / "graph.json.tmp"
    to_json(graph, communities, str(tmp_path), force=True)
    _os_replace(tmp_path, graph_path)

    stamp.update({
        "finished_at": utc_now(),
        "duration_seconds": round(time.time() - t0, 1),
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "communities": len(communities),
        "graph_path": str(graph_path),
        "graph_bytes": graph_path.stat().st_size,
        **redaction,
    })
    (out_dir / "build.json").write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    return stamp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="parent directory; the graph lands in <out>/graphify-out (default: repo root)")
    parser.add_argument("--dry-run", action="store_true", help="count files, build nothing")
    parser.add_argument("--if-installed", action="store_true",
                        help="exit 0 with a note when graphify is not installed (scheduled runs)")
    args = parser.parse_args()

    root = repo_root()
    # Same contract as `graphify --out`: graphify appends its own `graphify-out`
    # segment to the cache root, so the parent is what gets passed around.
    out_parent = Path(args.out).resolve() if args.out else root
    out_dir = out_parent / "graphify-out"

    # Re-exec under the graphify tool venv so `import graphify` resolves.
    if os.environ.get("SWITCHBOARD_GRAPHIFY_REEXEC") != "1":
        graphify_python = _graphify_python()
        if not graphify_python.exists():
            print(json.dumps({"error" if not args.if_installed else "skipped":
                              f"graphify venv python missing: {graphify_python}; "
                              f"install with: uv tool install graphifyy=={TESTED_GRAPHIFY_VERSION} --python 3.12"}))
            return 0 if args.if_installed else 2
        env = dict(os.environ, SWITCHBOARD_GRAPHIFY_REEXEC="1", **ENV)
        return subprocess.call([str(graphify_python), __file__, *sys.argv[1:]], env=env, cwd=root)

    try:
        os.nice(10)  # scheduled rebuilds yield to interactive work
    except (OSError, AttributeError):  # os.nice does not exist on Windows
        pass
    stamp = build(root, out_dir, args.dry_run)
    print(json.dumps(stamp, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
