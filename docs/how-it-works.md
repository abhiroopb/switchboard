# How switchboard works

This guide follows one task from your ask to its result, then covers the pieces that run around it.

## The surfaces

- **Chief of Staff (CoS).** This is the tab you talk to. It plans, picks a workstream, dispatches work, relays questions and summarizes results. Its rules are in `AGENTS.md` ("Conversation Routing") and `system/chief-of-staff-prompt.md`. It never produces the deliverable itself, so it stays responsive while work runs.
- **Launchpad.** This is an optional second console for dispatching work quietly. It receives only the results of its own tasks.
- **Worker tabs.** Each one runs one task in its own git worktree. Workers never wait for you in their own tab.
- **Routine tabs.** To Do, Meetings and Scheduled Jobs each hold one routine (`routines/<name>/CONTEXT.md`).

On macOS the tabs are cmux workspaces. On Windows they are wmux workspaces, driven through `system/scripts/mux/cmux`, which translates cmux commands into wmux ones. With no multiplexer, the solo profile turns this into a single session (`profiles/solo/`).

## One task, start to finish

1. **Ask.** You type a request in CoS. A first line of `Workstream: <slug>` picks the workstream, otherwise CoS picks or creates one.
2. **Dispatch.** CoS runs `bash system/scripts/router-dispatch.sh --origin cos "<your ask>"`. That calls `system/scripts/dispatch.py`, which records the task in the ledger (`system/state/running-tasks.json`), creates branch `task/<id>` from `main` in a new worktree, writes a launch prompt, and opens one worker tab running your agent on it. CoS replies with one line and goes back to listening.
3. **Work.** The worker reads `AGENTS.md`, the workstream's `CONTEXT.md` and the launch prompt, then does the task in its worktree. Nothing it does touches your main checkout until it lands.
4. **Questions.** When the worker needs a decision, it runs `system/scripts/ask-cos.sh` with options and a default, and keeps going on the default. The question appears in CoS. Your answer goes back as a steer through `router-dispatch.sh --steer`.
5. **Report.** When the worker is done, it runs `system/scripts/report-to-cos.sh`. That rebases the branch onto `main`, fast-forwards `main`, and delivers one report line to CoS. A real merge conflict comes back as a failed report that names the branch.
6. **Clean up.** The background sweep closes the worker tab once the report is proven delivered and the branch is on `main`, then removes the worktree and stops any leftover processes it started.

## Around the tasks

- **Command center loop.** `system/scripts/command-center-loop.sh` runs every 10 minutes from launchd or Task Scheduler. It sweeps the ledger: it restarts workers that hit a context or usage limit, retries undelivered reports, reaps finished tabs, and rebuilds the digest.
- **To-do list.** `system/skill-overrides/todo/` holds a local to-do list with priorities and due dates. Any action you must take becomes a to-do when it surfaces. Optionally, P0 and P1 items get calendar holds.
- **Start and end of day.** `system/start-day.sh` builds the day's plan. The `closing-day` skill saves durable notes into the day's journal and each workstream's `CONTEXT.md`.
- **Review notes.** Deliverables are Markdown notes in the workstream folder. The whole install folder is an Obsidian vault, so links open in place.
- **Memory.** Durable context lives in files. Claude Code can also use its built-in memory or the claude-mem worker. Amp uses `amp-mem`.
- **Connectors.** `system/connectors/pmos` gives the scripts Gmail, Calendar, Drive and Slack through your own OAuth apps. Without them, everything else still works.

## Safety

- **Hooks.** `.claude/hooks/pm-os-guardrails.py` (Claude Code) and `system/amp-plugins/pm-os-guardrails.ts` (Amp) check commands before they run.
- **Sends.** A message to another person is drafted, shown in full, and sent only after you approve that exact text.
- **Landing.** `dispatch.py finish` refuses a landing that tracks a private folder or adds a value from your private identifier list.
- **Pushing.** There is no remote by default, and the automatic push is off unless `PM_OS_AUTO_PUSH=on`.

## Where things live

| Folder | Holds |
|---|---|
| `AGENTS.md`, `CLAUDE.md`, `adapters/` | Policy and the adapters that load it into each agent |
| `system/scripts/` | The dispatcher, routines, guards and their tests |
| `system/prompts/` | Prompt pieces the dispatcher puts in launch prompts |
| `.agents/skills/`, `system/vendor/skills/` | Skills, your own and bundled |
| `routines/` | The routine tabs' contracts |
| `workstreams/` | One folder per area of work |
| `notes/`, `people/`, `local/` | Your journal, people profiles and machine settings (gitignored) |
| `system/state/` | The ledger, the to-do list and logs (gitignored) |
| `system/kit/` | The build that makes a shareable copy of your setup |
