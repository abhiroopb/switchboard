---
name: closing-and-archiving
description: "Archives the current Amp thread and closes its owning cmux workspace. Use when {{OWNER_FIRST}} says close and archive, fully close and archive, or this is done and asks to close the current conversation."
---

# Closing and Archiving

Fully end the current Amp conversation and remove its cmux workspace.

## Workflow

1. Finish any requested wrap-up before starting this workflow.
2. Capture the caller workspace from `CMUX_WORKSPACE_ID`. Never use the focused workspace, because {{OWNER_FIRST}} may be viewing another tab.
3. Discover `amp.update_thread` with `tool_search`. Call it through `code_exec` with `{archived: true}` for the current thread. Verify success before closing cmux. If the tool is unavailable or fails, report the blocker and leave cmux open.
4. Run `bash ~/Development/switchboard/.agents/skills/closing-and-archiving/scripts/close-cmux-workspace.sh "$CMUX_WORKSPACE_ID"`. It schedules the close after a short delay so the final response can render first.
5. Reply with one short confirmation. Do not call more tools after scheduling the close.

## Boundaries

- Treat the exact phrase “close and archive” as approval for both actions. Do not ask again.
- Archive the Amp thread before closing cmux.
- Unpin and close only the captured caller workspace.
- Do not archive or move switchboard workstream files unless {{OWNER_FIRST}} explicitly asks to archive the workstream itself.
- If `CMUX_WORKSPACE_ID` is unavailable, archive the thread and state that no owning cmux workspace was detected.
