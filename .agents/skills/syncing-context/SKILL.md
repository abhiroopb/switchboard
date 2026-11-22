---
name: syncing-context
description: "Refreshes PM OS context surfaces, indexes, and megadesk state. Use when asked to sync PM OS, run a context sync, refresh indexes, or do CoS preflight or postflight maintenance."
---

# Syncing Context

Refresh the lightweight context surfaces that keep PM OS coherent between sessions.

## Use For

- `sync PM OS`
- `context sync`
- CoS preflight at the start of a megadesk session
- CoS postflight at the end of a megadesk session

## Arguments

- `start` for CoS preflight
- `end` for CoS postflight
- `manual` for an ad hoc refresh

Default to `manual` if no phase is provided.

## Workflow

1. Run the strict instruction contract check:

```bash
python3 system/scripts/check-instruction-drift.py --strict
```

Stop and report each owning surface if this check fails. Do not sync external adapters or overwrite managed files.

2. Run:

```bash
python3 system/scripts/context_sync.py --phase <start|end|manual> --write
```

3. Read the JSON summary it prints.
4. Call out any stale source snapshots or missing top-level indexes.
5. Keep the user-facing summary short. This is a maintenance sweep, not a planning pass.

## What It Refreshes

- `workstreams/README.md`
- `system/state/sources/people.json`
- megadesk state via `system/scripts/rebuild-megadesk-state.py`
- `system/state/sources/context-sync.json`

## Guardrails

- Do not turn this into a broad repo review.
- Do not rewrite durable docs just because they are old.
- If the user wants actual planning or end-of-day wrap-up, hand off to the relevant workflow after the sync finishes.
