#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

from workflow_upgrade_flags import load_workflow_upgrade_flags
from repo_paths import main_checkout


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSTREAMS_ROOT = REPO_ROOT / "workstreams"
WORKSPACE_UPDATES_PATH = REPO_ROOT / "system" / "state" / "sources" / "workspace-updates.json"
RECALL_DEBUG_PATH = REPO_ROOT / "system" / "state" / "sources" / "recall-debug.json"
CLASSIFIER = REPO_ROOT / "system" / "scripts" / "classify-task-for-recall.py"
QUERY_RECALL_INDEX = REPO_ROOT / "system" / "scripts" / "query-recall-index.py"
QUERY_KNOWLEDGE = REPO_ROOT / "system" / "scripts" / "query-knowledge.py"
AMP_MEM = Path.home() / "bin" / "amp-mem"
HIGH_SIGNAL_MEMORY_TYPES = {"decision", "workflow", "preference", "discovery", "bugfix", "config"}
# Structural knowledge graph (graphify, no LLM) built by build-knowledge-graph.py.
def _graphify_bin() -> Path:
    """The graphify CLI: PATH first (uv puts graphify.exe in ~/.local/bin on
    Windows), then ~/.local/bin/graphify[.exe]."""
    found = shutil.which("graphify")
    if found:
        return Path(found)
    base = Path.home() / ".local" / "bin"
    return next((c for c in (base / "graphify", base / "graphify.exe") if c.exists()), base / "graphify")


GRAPHIFY_BIN = _graphify_bin()
# One graph per repo, built on the main checkout (gitignored, so a worktree has no
# copy of its own). Node `src` paths are root-relative and open under any checkout.
GRAPH_PATH = main_checkout(REPO_ROOT) / "graphify-out" / "graph.json"
GRAPH_QUERY_TIMEOUT_SECONDS = 20
GRAPH_NODE_LINE = re.compile(r"^NODE (?P<label>.+?) \[src=(?P<src>\S*) loc=(?P<loc>\S*)(?: community=\S+)?\]$")
GRAPH_START_LINE = re.compile(r"\| Start: (?P<seeds>\[.*?\]) \|")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def run_json(command: list[str]) -> dict | list:
    return json.loads(subprocess.check_output(command, text=True))


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_]+", text.lower()) if token]


def extract_lines(path: Path, limit: int = 2) -> list[str]:
    if not path.exists():
        return []
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return lines[:limit]


def guess_workstream(task: str) -> str | None:
    lowered = task.lower()
    for config in WORKSTREAMS_ROOT.glob("*/config.yaml"):
        slug = config.parent.name
        if slug in lowered:
            return slug
    return None


def normalize_workstream_query(workstream: str) -> str:
    return workstream.replace("-", " ")


def query_recall_index(queries: list[str], limit: int) -> list[dict]:
    for query in queries:
        if not query:
            continue
        try:
            rows = run_json([sys.executable, str(QUERY_RECALL_INDEX), query, "--limit", str(limit)])
        except subprocess.CalledProcessError:
            continue
        if rows:
            return rows[:limit]
    return []


def query_knowledge_graph(query: str, limit: int) -> tuple[list[dict], str | None]:
    """Seed nodes from the graphify structural graph for `query`.

    Returns (hits, suppression reason). Each hit is {label, path, loc}; `path`
    is repo-relative. Only the seed nodes (the header's `Start:` list) are kept:
    the BFS neighbourhood graphify prints after them is navigation noise for a
    recall pack. The graph is a pointer to a file and line, never a citation;
    callers open the file and quote it. Any failure suppresses the source.
    """
    if not GRAPHIFY_BIN.exists():
        return [], "knowledge graph: graphify not installed"
    if not GRAPH_PATH.exists():
        return [], "knowledge graph: graphify-out/graph.json missing (run build-knowledge-graph.py)"
    env = dict(os.environ, GRAPHIFY_BACKEND="none", GRAPHIFY_QUERY_LOG_DISABLE="1")
    try:
        proc = subprocess.run(
            [str(GRAPHIFY_BIN), "query", query, "--graph", str(GRAPH_PATH), "--budget", "600"],
            capture_output=True, text=True, env=env, cwd=REPO_ROOT,
            timeout=GRAPH_QUERY_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"knowledge graph query failed: {type(exc).__name__}"
    if proc.returncode == 4:
        return [], "knowledge graph: no matching nodes"
    if proc.returncode not in (0, 3):
        return [], f"knowledge graph query exited {proc.returncode}"
    seeds: set[str] = set()
    for line in proc.stdout.splitlines():
        match = GRAPH_START_LINE.search(line)
        if match:
            try:
                seeds = {str(s) for s in ast.literal_eval(match.group("seeds"))}
            except (ValueError, SyntaxError):
                seeds = set()
            break
    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        match = GRAPH_NODE_LINE.match(line.strip())
        if not match:
            continue
        label = match.group("label")
        if seeds and label not in seeds:
            continue
        if not match.group("src"):
            # Module nodes (json, pathlib, calendar) have no file; nothing to open.
            continue
        hits.append({"label": label, "path": match.group("src"), "loc": match.group("loc") or "L1"})
        if len(hits) >= limit:
            break
    if not hits:
        return [], "knowledge graph returned no seed nodes"
    return hits, None


def load_memory_export() -> list[dict]:
    if not AMP_MEM.exists():
        return []
    try:
        return json.loads(subprocess.check_output([str(AMP_MEM), "export", "--format", "json"], text=True))
    except Exception:
        return []


def score_memory_row(row: dict, tokens: list[str]) -> int:
    haystack = " ".join(
        str(row.get(field, "")) for field in ("topic", "summary", "project", "tags")
    ).lower()
    return sum(1 for token in tokens if token in haystack)


def query_canonical_knowledge(workstream: str | None, limit: int = 8) -> dict:
    """Bounded query against the canonical daily knowledge layer.

    Returns the query-knowledge.py JSON buckets ({current, upcoming,
    historical}) or {} on any failure — recall packs are best-effort and
    must never block the caller. Workstream-scoped queries look back a week;
    unscoped queries stay tight (2 days) to avoid firehose noise.
    PM_OS_KNOWLEDGE_DIR overrides the knowledge dir for hermetic tests.
    """
    if not QUERY_KNOWLEDGE.exists():
        return {}
    cmd = [
        sys.executable, str(QUERY_KNOWLEDGE),
        "--format", "json",
        "--days", "7" if workstream else "2",
        "--limit", str(limit),
    ]
    if workstream:
        cmd += ["--workstream", workstream]
    knowledge_dir = os.environ.get("PM_OS_KNOWLEDGE_DIR")
    if knowledge_dir:
        cmd += ["--knowledge-dir", knowledge_dir]
    try:
        result = run_json(cmd)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def query_memory(query_terms: list[str], limit: int) -> list[dict]:
    rows = load_memory_export()
    if not rows:
        return []

    tokens = tokenize(" ".join(query_terms))
    if not tokens:
        return []

    ranked: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("private"):
            continue
        if row.get("superseded_by"):
            continue
        if row.get("type") not in HIGH_SIGNAL_MEMORY_TYPES:
            continue
        score = score_memory_row(row, tokens)
        if score <= 0:
            continue
        ranked.append({
            "id": row.get("id"),
            "type": row.get("type"),
            "topic": row.get("topic"),
            "summary": row.get("summary"),
            "ts": row.get("ts") or "",
            "score": score,
        })

    ranked.sort(key=lambda row: (row["score"], row["ts"]), reverse=True)
    return ranked[:limit]


def dedupe(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def build_pack(task: str, workstream: str | None = None, limit: int = 5) -> dict:
    flags = load_workflow_upgrade_flags()["flags"]
    if flags.get("recall_enabled") is False:
        return {
            "task_class": "recall-disabled",
            "sources_considered": [],
            "sources_selected": [],
            "what_matters_now": [],
            "graph_pointers": [],
            "what_changed_recently": [],
            "open_risks": [],
            "suppressed_sources": ["recall disabled by workflow-upgrade flag"],
        }

    classification = run_json([sys.executable, str(CLASSIFIER), "--task", task])
    if not classification.get("should_recall"):
        return {
            "task_class": classification["task_class"],
            "sources_considered": [],
            "sources_selected": [],
            "what_matters_now": [],
            "what_changed_recently": [],
            "open_risks": [],
            "suppressed_sources": ["recall skipped for trivial utility task"],
        }

    selected_workstream = workstream or guess_workstream(task)
    sources_considered: list[str] = []
    sources_selected: list[str] = []
    what_matters_now: list[str] = []
    what_changed_recently: list[str] = []
    open_risks: list[str] = []
    suppressed_sources: list[str] = []

    if selected_workstream:
        context_path = WORKSTREAMS_ROOT / selected_workstream / "CONTEXT.md"
        sources_considered.append(str(context_path))
        context_lines = extract_lines(context_path, limit=3)
        if context_lines:
            sources_selected.append(str(context_path))
            what_matters_now.extend(context_lines[:2])

        workspace_updates = load_json(WORKSPACE_UPDATES_PATH, {})
        update = (workspace_updates.get("workstreams") or {}).get(selected_workstream) if isinstance(workspace_updates, dict) else None
        if isinstance(update, dict):
            sources_selected.append(str(WORKSPACE_UPDATES_PATH))
            summary = update.get("summary")
            next_action = update.get("recommended_next_action")
            if summary:
                what_changed_recently.append(str(summary))
            if next_action:
                open_risks.append(f"Next action: {next_action}")

    if any(term in task.lower() for term in ("todo", "calendar", "recurring")):
        path = REPO_ROOT / "knowledge" / "todo-system-preferences.md"
        sources_considered.append(str(path))
        lines = extract_lines(path, limit=2)
        if lines:
            sources_selected.append(str(path))
            what_matters_now.extend(lines)

    if any(term in task.lower() for term in ("claude", "policy", "instruction", "adapter", "rule")):
        for path in [REPO_ROOT / "system" / "behavior-map.md", REPO_ROOT / "system" / "rules" / "claude-rule-inventory.yaml"]:
            sources_considered.append(str(path))
            lines = extract_lines(path, limit=2)
            if lines:
                sources_selected.append(str(path))
                what_matters_now.extend(lines[:1])

    # Canonical daily knowledge (reconciled, deduped, supersession-resolved).
    # This is the authoritative current/upcoming state; recall index + amp-mem
    # below add narrative/history on top of it.
    canonical = query_canonical_knowledge(selected_workstream)
    sources_considered.append("canonical-knowledge (system/state/knowledge/records.jsonl)")
    canonical_rows = list(canonical.get("current") or []) + list(canonical.get("upcoming") or [])
    if canonical_rows:
        sources_selected.append("canonical-knowledge (system/state/knowledge/records.jsonl)")
        for rec in canonical_rows[:6]:
            event_day = str(rec.get("event_time") or "")[:10]
            topic = re.sub(r"\s+", " ", str(rec.get("topic") or "")).strip()
            what_matters_now.append(
                f"[{rec.get('status')}] {event_day} {rec.get('source_class')}: {topic}"
            )
    else:
        suppressed_sources.append("canonical knowledge returned no current/upcoming records")

    recall_queries = []
    if selected_workstream:
        recall_queries.append(normalize_workstream_query(selected_workstream))
    recall_queries.append(task)

    # Graph pointers get their own key so the 5-item caps on sources_selected and
    # what_matters_now (filled first by CONTEXT, workspace updates and canonical
    # knowledge) never truncate them. The query is the task alone: prepending the
    # workstream words seeds generic headings (## Maintenance) instead of the ask.
    graph_hits, graph_reason = query_knowledge_graph(task, limit=3)
    graph_pointers = [
        {"label": hit["label"], "path": hit["path"], "loc": hit["loc"]} for hit in graph_hits
    ]
    if graph_hits:
        sources_considered.append("knowledge-graph (graphify-out/graph.json)")
    else:
        suppressed_sources.append(graph_reason or "knowledge graph returned no hits")

    recall_index_hits = query_recall_index(recall_queries, limit=2)
    if recall_index_hits:
        sources_selected.extend(hit.get("path") for hit in recall_index_hits if hit.get("path"))
        what_changed_recently.extend(hit.get("snippet") for hit in recall_index_hits if hit.get("snippet"))
    else:
        suppressed_sources.append("recall index returned no hits")

    memory_hits = query_memory(recall_queries, limit=3)
    if memory_hits:
        sources_considered.append("amp-mem")
        sources_selected.extend(f"amp-mem:{hit.get('id')}" for hit in memory_hits if hit.get("id"))
        what_changed_recently.extend(
            f"[{hit.get('type')}] {hit.get('topic')}: {hit.get('summary')}"
            for hit in memory_hits
            if hit.get("summary")
        )
    else:
        suppressed_sources.append("amp-mem returned no relevant hits")

    pack = {
        "task_class": classification["task_class"],
        "sources_considered": dedupe(sources_considered, limit),
        "sources_selected": dedupe(sources_selected, limit),
        "what_matters_now": dedupe(what_matters_now, limit),
        # File+line pointers from the structural graph; open the file and quote it.
        # A missing or empty list is never evidence that something does not exist.
        "graph_pointers": graph_pointers,
        "what_changed_recently": dedupe(what_changed_recently, limit),
        "open_risks": dedupe(open_risks, limit),
        "suppressed_sources": dedupe(suppressed_sources, limit),
    }
    return pack


def append_debug(task: str, pack: dict) -> None:
    payload = load_json(RECALL_DEBUG_PATH, {"generated_at": None, "packs": []})
    payload["generated_at"] = utc_now()
    payload.setdefault("packs", []).append({
        "generated_at": utc_now(),
        "task": task,
        "pack": pack,
    })
    payload["packs"] = payload["packs"][-25:]
    atomic_write_json(RECALL_DEBUG_PATH, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a bounded recall pack for substantive PM OS tasks.")
    parser.add_argument("--task", required=True)
    parser.add_argument("--workstream")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--no-debug-write", action="store_true")
    args = parser.parse_args()

    pack = build_pack(args.task, args.workstream, args.limit)
    if not args.no_debug_write:
        append_debug(args.task, pack)
    print(json.dumps(pack, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
