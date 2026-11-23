---
name: recall
description: "Generate a bounded switchboard recall pack on demand. Use when resuming a workstream from a fresh thread, answering a historical question, preparing for meetings, drafting status updates, or needing current-plus-historical context without dumping raw memory. Not required before every session — Amp Neo's auto-compaction preserves intra-session context."
---

# Recall

Generate a bounded recall pack from the switchboard repo:

```bash
python3 ~/Development/switchboard/system/scripts/generate-recall-pack.py --task "<task>"
```

## Use for

- resuming an active workstream
- meeting prep
- status updates or project recaps
- historical questions where thread memory alone is not enough
- PRD or stakeholder-comms work that needs prior decisions and recent context

## Rules

- treat recall as a preflight, not the final answer
- prefer live workstream and state truth over older memory
- use only the few facts that matter, never dump the full JSON to the user
- if recall is disabled by workflow-upgrade flags, do not force it unless you are explicitly running shadow-mode verification
- **knowledge graph hits are pointers, not citations.** The pack carries a `graph_pointers` list (`{label, path, loc}` entries from `graphify query "<task>" --graph graphify-out/graph.json`, graph built by `system/scripts/build-knowledge-graph.py`, no LLM). Open each named file at the given line before any `rg` or directory search, and quote that file. Never cite the graph node itself; the Grounding rule still needs the source line. **A graph miss is never evidence of absence:** the graph indexes headings, links, and code symbols only (no body text, no `_archive/`, `people/`, `notes/`, or `sources/`), so an empty `graph_pointers` or a `graphify` exit code 4 means "search another way", never "the rule does not exist". A `suppressed_sources` reason such as `knowledge graph: graphify-out/graph.json missing` means the graph has not been built; fall back to the recall index and `rg` without comment.
- **no paragraph walls** when surfacing recall findings to the user — per AGENTS.md Output Preferences, any output longer than 2-3 sentences MUST use bullets + bold sub-headings (or a 2-column table). Group recall sections like `**What changed**`, `**Open risks**`, `**Suppressed sources**` rather than running them together as prose.

## Named sources

Quote these by name when the task names the workstream or topic. Read the local snapshot, not the live site, and cite the URL.

| Topic | Source | Where it lives | Use it for | Not for |
|---|---|---|---|---|

Add a row when a workstream ingests a source {{OWNER_FIRST}} asks to keep in recall. The switchboard copy `.agents/skills/recall/SKILL.md` is canonical; `sync-skills.py` copies it to `~/.claude/skills/recall/`, which Claude and Amp read.

## Shadow verification

To compare current vs upgraded behavior during the workflow upgrade:

```bash
python3 ~/Development/switchboard/system/scripts/run-workflow-upgrade-shadow.py --task "<task>"
```
