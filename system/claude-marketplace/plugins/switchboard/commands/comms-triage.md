---
description: Triage all inbound comms (Slack unreads, Gmail, Linear, Calendar RSVPs, saved items) one item at a time
argument-hint: "[workspace]"
allowed-tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
---

Read and follow all instructions in "{{SWITCHBOARD_ROOT}}"/routines/comms-triage/CONTEXT.md.

Triage ALL inbound communications one item at a time: Slack unreads → Gmail → Linear/Jira → Calendar RSVPs → Slack saved items. Auto-draft responses. Present exactly ONE item per message with numbered action options, then wait.

If `$ARGUMENTS` is `workspace`, do not triage inline. Instead switch to the dedicated 📬 Comms Triage cmux workspace (the Amp palette command "switchboard: Triage comms now") by running this with Bash and reporting its result:

```bash
bash "{{SWITCHBOARD_ROOT}}"/system/scripts/open-comms-triage.sh
```

Exit 0 means the workspace took focus. Exit 1 means the Comms Triage workspace was not found or cmux is unreachable; say so and stop.
