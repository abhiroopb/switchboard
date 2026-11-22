# Router

## Status: retired as an agent

Routing is no longer an Amp thread per task. `system/scripts/router-dispatch.sh` calls `system/scripts/dispatch.py`, which picks the owning workstream, creates a git worktree at `.amp/worktrees/<task-id>` on branch `task/<task-id>`, opens one cmux worker tab, and writes the ledger row. The surface that called it sees one result line: `[<emoji> <Workstream Name> · rt-…] accepted; worker starting` (the ledger row is written and a detached launcher creates the worktree and tab; `router.yaml` `dispatch.async_launch`), `waiting on <Workstream Name> (rt-…) because …`, `duplicate`, or `failed: <plain reason>` (the lead names the workstream and the id follows it).

## If a legacy Router tab ever opens

Something old launched you. Do not classify or seed anything by hand. Run the dispatch for the request you were given and stop:

```bash
bash system/scripts/router-dispatch.sh --origin "${ROUTER_ORIGIN:-cos}" "<the full original request, verbatim>"
```

Reply with the result line. Never perform the requested work, never use a remote machine, SSH, or a remote subagent, and never focus another workspace.

## Owner selection (now inside dispatch.py)

1. An explicit `Workstream: <slug>` line in the prompt wins outright.
2. `--continuation-of <task>` inherits the parent task's workstream.
3. Otherwise `todo_match_terms`, then workstream metadata and `CONTEXT.md`, then `priority-channels.yaml` `default_workstream`.
4. A new workstream is created only for durable multi-session work with no existing owner; one-shots route to an existing owner.

## Done and reporting

Done is git-derived: `task/<id>` unmerged → in progress; merged to `main` → done; branch gone without merge → failed/cancelled. `dispatch.py finish` rebases onto `main` and fast-forwards; `dispatch.py sweep` (run by `command-center-loop.sh`) resumes idle workers once, reaps finished tabs, and expires stale launches. `report-to-cos.sh` delivers one line per task to its origin.

