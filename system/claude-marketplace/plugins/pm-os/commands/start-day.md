---
description: Run the full morning workflow (Chief of Staff)
argument-hint: "[script]"
allowed-tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
---

Use the `chief-of-staff` skill to start the day. This runs the full morning workflow: context gathering, daily note creation, to-do reconcile, calendar review, meeting job scheduling, people profile updates, workstream planning, and launch plan generation.

If `$ARGUMENTS` is `script`, skip the skill and launch the scripted start-of-day flow instead (the Amp palette command "PM OS: Start day"). Run it with Bash in the background and report that it launched:

```bash
bash "{{PM_OS_ROOT}}"/system/start-day.sh
```

The script opens the cmux workspaces that `system/scripts/sod-workspace-plan.py` lists and seeds each with one short note. Preview what it would open without launching anything: `python3 "{{PM_OS_ROOT}}"/system/scripts/sod-workspace-plan.py --dry-run`.
