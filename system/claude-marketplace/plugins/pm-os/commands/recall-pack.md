---
description: Generate a bounded recall pack for a task from memory, workstreams, and state
argument-hint: "<task description> [--workstream <slug>]"
allowed-tools: ["Bash", "Read", "Glob", "Grep"]
---

Generate a bounded recall pack (the Amp palette command "PM OS: Generate recall pack").

If `$ARGUMENTS` is empty, ask one question: "What is the recall pack for? (e.g. `weekly status update`)" and wait.

Otherwise run this exact script with Bash, passing the task text as `--task` (and `--workstream <slug>` when the arguments include one):

```bash
python3 "{{PM_OS_ROOT}}"/system/scripts/generate-recall-pack.py --task "$ARGUMENTS"
```

Then:
- On exit 0, read the full stdout (the recall pack) and present it. Lead with the first 20 lines verbatim, then summarize the rest in bullets grouped by source (memory, workstream context, state).
- On a non-zero exit, report `Failed:` followed by the first 200 characters of stderr. Do not retry with different arguments.

Keep the pack as working context for the rest of the session; do not save it anywhere.
