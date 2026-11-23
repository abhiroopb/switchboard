---
description: Force-refresh every watched Google Doc snapshot declared in workstream config.yaml files
allowed-tools: ["Bash", "Read"]
---

Refresh all watched Google Doc snapshots (the Amp palette command "switchboard: Refresh watched docs").

Run this exact script with Bash in the background; it fetches every `watched_docs:` entry across `workstreams/*/config.yaml` and rewrites the `sources/<slug>.md` snapshots, which can take minutes:

```bash
python3 "{{SWITCHBOARD_ROOT}}"/system/scripts/refresh-watched-docs.py --force
```

Report "Watched-docs refresh started in background" right away. When the run completes, summarize its output in bullets: how many docs refreshed, how many unchanged, and any doc that failed (name the doc and the error line). Do not edit any snapshot by hand.
