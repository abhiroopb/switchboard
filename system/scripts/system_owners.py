#!/usr/bin/env python3
"""Which workstream owns the system a failure or alert names.

A failure link dispatched with no Workstream line (a DM saying a nightly job
failed on a build VM) must land on the workstream that owns that box, never a
new generic workstream. Re-homing resolves ownership: VM boxes map to their
owning workstream, and so do hosted pages, scheduled jobs, repos, Slack
channels, and live ledger rows. The generic `vm-fleet` workstream is only for
VM infrastructure itself.

resolve() pulls system identifiers out of the linked content (VMs,
hosted page slugs, repos, Slack channels, job names: kebab tokens), then
scores each workstream on four signals:

  declared   config.yaml `systems:` lists the identifier            +100
  slug       the identifier is the workstream's own slug             +80
  live row   a live ledger row of the workstream names it            +60
  mentions   its CONTEXT.md and config.yaml name it at least 3 times
             and at least twice as often as any other workstream     +40

A workstream with `infra_only: true` in config.yaml (the generic `vm-fleet`
workstream) owns only what it declares under `systems:`; live rows and
mentions never make it the owner of a box, site, or job. The best total wins
when it reaches 40 and is not tied. The result also names the live worker on
that workstream, so dispatch can start the new task as its secondary.

CLI: python3 system/scripts/system_owners.py "<text>" [--exclude-task rt-…]  -> JSON or null
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

DECLARED, SLUG, LIVE_ROW, MENTIONS = 100, 80, 60, 40
MIN_SCORE = 40
MIN_MENTIONS = 3
LIVE_STATUSES = {"routing", "active", "queued"}

PAGES_RE = re.compile(r"pages\.example\.com/([a-z0-9][a-z0-9_-]+)", re.IGNORECASE)
GITHUB_RE = re.compile(r"github\.com/[\w.-]+/([\w.-]+)", re.IGNORECASE)
SLACK_CHANNEL_RE = re.compile(r"(?:<#|/archives/)(C[A-Z0-9]{6,})")
HASH_CHANNEL_RE = re.compile(r"(?<![\w&])#([a-z0-9][a-z0-9_-]{2,})")
KEBAB_RE = re.compile(r"(?<![\w.-])([a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)(?![\w-])")
# Kebab tokens that name nothing a workstream owns.
NOT_SYSTEMS = {
    "switchboard-example", "switchboard", "follow-up", "follow-ups", "in-progress", "re-run", "e-mail", "sign-in",
    "built-in", "end-of-day", "start-of-day", "one-off", "real-time", "read-only", "co-authored-by",
    "non-zero", "set-up", "check-in", "opt-out", "opt-in", "up-to-date", "out-of-date", "x-ray",
    "robot_face", "robot_face", "white_check_mark", "daily-refresh", "dry-run", "switchboard-maintenance",
}


def repo_root() -> Path:
    env = os.environ.get("REPO_ROOT")
    if env:
        return Path(env)
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    return Path(out.stdout.strip() or ".")


def identifiers(text: str) -> list[str]:
    """System identifiers in `text`, in order of first appearance, lowercased."""
    text = text or ""
    found: list[str] = []

    def add(token: str) -> None:
        token = token.strip("-_").lower()
        if token and token not in found and token not in NOT_SYSTEMS:
            found.append(token)

    for pattern in (PAGES_RE, GITHUB_RE):
        for match in pattern.finditer(text):
            add(match.group(1).removesuffix(".git"))
    for match in SLACK_CHANNEL_RE.finditer(text):
        add(match.group(1))
    for match in HASH_CHANNEL_RE.finditer(text):
        add(match.group(1))
    # Kebab tokens outside URLs: box names, site slugs, job and repo names. A
    # token needs one word of 4+ letters and no 4-digit run (run ids, dates).
    bare = re.sub(r"https?://\S+", " ", text)
    for match in KEBAB_RE.finditer(bare.lower()):
        token = match.group(1)
        parts = re.split(r"[-_]", token)
        if len(token) < 8 or re.search(r"\d{4}", token) or not any(len(p) >= 4 and p.isalpha() for p in parts):
            continue
        add(token)
    return found


def _config(path: Path) -> dict[str, Any]:
    try:
        import yaml  # noqa: WPS433
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 (a broken config.yaml must not break routing)
        return {}


def workstreams(root: Path) -> dict[str, dict[str, Any]]:
    """{slug: {"config", "text"}} for every live workstream folder."""
    out: dict[str, dict[str, Any]] = {}
    base = root / "workstreams"
    if not base.is_dir():
        return out
    for context in sorted(base.glob("*/CONTEXT.md")):
        slug = context.parent.name
        if slug.startswith(("_", ".")):
            continue
        cfg_path = context.parent / "config.yaml"
        cfg = _config(cfg_path) if cfg_path.is_file() else {}
        if str(cfg.get("status") or "").lower() in {"done", "cancelled", "resolved", "archived"}:
            continue
        try:
            text = context.read_text(encoding="utf-8", errors="replace")
            if cfg_path.is_file():
                text += "\n" + cfg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out[slug] = {"config": cfg, "text": text.lower()}
    return out


def declared_systems(cfg: dict[str, Any]) -> set[str]:
    raw = cfg.get("systems") or []
    if isinstance(raw, dict):   # systems: {vm: [...], pages: [...], ...}
        raw = [item for value in raw.values() for item in (value if isinstance(value, list) else [value])]
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).strip().lower().lstrip("#") for item in raw if str(item).strip()}


def live_rows(payload: dict[str, Any] | None, exclude_task: str = "") -> list[dict[str, Any]]:
    return [t for t in (payload or {}).get("tasks", [])
            if t.get("status") in LIVE_STATUSES and t.get("id") != exclude_task and t.get("workstream")]


def primary_worker(slug: str, payload: dict[str, Any] | None, exclude_task: str = "") -> dict[str, Any] | None:
    """The oldest live row on `slug` that has (or is getting) a worker: the
    primary a new task on that workstream joins as a numbered secondary."""
    rows = [t for t in live_rows(payload, exclude_task) if t["workstream"] == slug and t.get("status") != "queued"]
    rows.sort(key=lambda t: str(t.get("created_at") or ""))
    return rows[0] if rows else None


def resolve(text: str, root: Path | None = None, payload: dict[str, Any] | None = None,
            exclude_task: str = "", tree: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The owning workstream of the systems `text` names, or None.

    Returns {"slug", "score", "evidence": [...], "identifiers": [...],
    "primary": <live row on that workstream> | None}."""
    ids = identifiers(text)
    if not ids:
        return None
    tree = workstreams(root or repo_root()) if tree is None else tree
    if not tree:
        return None
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}

    def credit(slug: str, points: int, why: str) -> None:
        scores[slug] = scores.get(slug, 0) + points
        evidence.setdefault(slug, []).append(why)

    infra = {slug for slug, ws in tree.items() if ws["config"].get("infra_only")}
    rows = live_rows(payload, exclude_task)
    for ident in ids:
        for slug, ws in tree.items():
            if ident in declared_systems(ws["config"]):
                credit(slug, DECLARED, f'declares system "{ident}" (config.yaml systems)')
        if ident in tree and ident not in infra:
            credit(ident, SLUG, f'names the workstream slug "{ident}"')
        touched = {str(t["workstream"]) for t in rows
                   if ident in str(t.get("prompt") or "").lower() or ident in str(t.get("summary") or "").lower()}
        for slug in sorted(touched - infra):
            if slug in tree:
                credit(slug, LIVE_ROW, f'a live {slug} worker touches "{ident}"')
        counts = sorted(((ws["text"].count(ident), slug) for slug, ws in tree.items() if slug not in infra), reverse=True)
        top_n, top = counts[0] if counts else (0, "")
        runner_up = counts[1][0] if len(counts) > 1 else 0
        if top_n >= MIN_MENTIONS and top_n >= 2 * runner_up:
            credit(top, MENTIONS, f'its CONTEXT.md and config.yaml name "{ident}" {top_n} times'
                                  + (f" (next most: {counts[1][1]}, {runner_up})" if runner_up else ""))
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, score = ranked[0]
    if score < MIN_SCORE or (len(ranked) > 1 and ranked[1][1] == score):
        return None
    primary = primary_worker(best, payload, exclude_task)
    return {"slug": best, "score": score, "evidence": evidence[best], "identifiers": ids,
            "primary": ({k: primary.get(k) for k in ("id", "tab_title", "workspace_ref", "worktree", "status")}
                        if primary else None)}


def load_payload(root: Path) -> dict[str, Any]:
    path = Path(os.environ.get("RUNNING_TASKS_FILE") or root / "system" / "state" / "running-tasks.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tasks": []}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("text", help="the failure or alert text (a Slack message, a log line, an ask)")
    parser.add_argument("--exclude-task", default="", help="your own task id, so your row is not its own evidence")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = repo_root()
    print(json.dumps(resolve(args.text, root, load_payload(root), args.exclude_task), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
