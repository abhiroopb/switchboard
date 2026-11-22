#!/usr/bin/env python3
"""Pick a relevant, unique emoji for a workstream.

Strategy:
  1. Score the workstream's text (kebab name + optional body) against the
     `semantic_map` in system/config/workspace-emoji-pool.yaml. Each map entry
     lists keywords + ranked emoji candidates.
  2. Walk the highest-scoring entries first; return the first emoji candidate
     that isn't already used in another workstreams/*/config.yaml.
  3. If nothing semantic matches (or every candidate is taken), fall back to
     the next unused entry in `fallback_pool`. As a last resort, cycle the
     pool.

Usage:
  python3 system/scripts/pick-workstream-emoji.py <workstream-kebab-name> [body-text]

The body-text argument is optional. When omitted, only the kebab name is
scored. process-new-inbox.py passes the inbound message body so freshly
auto-created workstreams get a relevant emoji even before their CONTEXT.md
has been refined.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML not installed; run: python3 -m pip install --user pyyaml\n")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POOL_FILE = REPO_ROOT / "system" / "config" / "workspace-emoji-pool.yaml"
WORKSTREAMS_DIR = REPO_ROOT / "workstreams"


def load_config() -> dict:
    if not POOL_FILE.exists():
        sys.stderr.write(f"missing pool file: {POOL_FILE}\n")
        sys.exit(1)
    return yaml.safe_load(POOL_FILE.read_text()) or {}


def existing_emojis() -> set[str]:
    used: set[str] = set()
    for cfg in WORKSTREAMS_DIR.glob("*/config.yaml"):
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
        except yaml.YAMLError:
            continue
        e = data.get("emoji")
        if isinstance(e, str) and e.strip():
            used.add(e.strip())
    return used


def score_entries(text: str, semantic_map: list[dict]) -> list[tuple[int, list[str]]]:
    """Return a list of (score, candidate_emojis) sorted high → low."""
    text_lower = text.lower()
    scored: list[tuple[int, list[str]]] = []
    for entry in semantic_map:
        keywords = [str(k).lower() for k in entry.get("keywords", [])]
        emojis = [str(e) for e in entry.get("emojis", []) if e]
        if not emojis:
            continue
        score = 0
        for kw in keywords:
            if not kw:
                continue
            # Whole-token match counts double; substring counts single.
            tokens = text_lower.replace("-", " ").replace("_", " ").split()
            if kw in tokens:
                score += 2
            elif kw in text_lower:
                score += 1
        if score > 0:
            scored.append((score, emojis))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def fallback_emoji(used: set[str], pool: list[str]) -> str:
    for emoji in pool:
        if emoji not in used:
            return emoji
    # everything taken; reuse the first
    return pool[0] if pool else "📁"


def pick(name: str, body: str = "") -> str:
    config = load_config()
    semantic_map = config.get("semantic_map") or []
    fallback_pool = [str(e) for e in (config.get("fallback_pool") or []) if e]
    used = existing_emojis()

    text = f"{name} {body}".strip()
    scored = score_entries(text, semantic_map)
    for _, candidates in scored:
        for emoji in candidates:
            if emoji not in used:
                return emoji

    return fallback_emoji(used, fallback_pool)


def main() -> int:
    if len(sys.argv) < 2:
        # No name passed — return the next fallback so old callers still work.
        config = load_config()
        fallback_pool = [str(e) for e in (config.get("fallback_pool") or []) if e]
        print(fallback_emoji(existing_emojis(), fallback_pool))
        return 0
    name = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    print(pick(name, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
