# Routines

Operational loops that run on cadence. Unlike workstreams, routines never complete.

| Routine | Cadence | What it does |
|---|---|---|
| megadesk | Persistent | The CoS tab (source key `megadesk`). Keeps the ranked queue fresh and tells you the next best action. It is a cmux tab, not an app. |
| todo | Daily | Capture, prioritize, follow through |
| comms-triage | Daily | Collector-first inbox triage, reply drafting, and follow-up capture |
| meetings | Per meeting | Scheduler recovery, pre-brief prep, and post-ingest follow-through |

## Routines vs Workstreams

- **Workstream** = project (finite, has deliverables, has a "done" state)
- **Routine** = ops loop (infinite, runs on cadence)

Each folder = one operational loop. Same CONTEXT.md contract as workstreams, but these never end.
