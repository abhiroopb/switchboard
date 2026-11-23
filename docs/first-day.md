# Your first day on switchboard

This walks through one day after a fresh install. Commands assume `SWITCHBOARD_ROOT` is `{{SWITCHBOARD_ROOT}}`.

## Before you start

- `bash system/scripts/verify-kit-install.sh` passes.
- `AGENTS.md` and `system/chief-of-staff-prompt.md` have no `personalize here` block left empty.
- `system/config/priority-channels.yaml` lists at least one channel. Without it, comms triage has nothing to read and will say so.
- cmux is open, Amp is signed in, `cmux workspace list` works from a terminal. On Windows: wmux is open, `system/windows/install.ps1` has run, and `cmux workspace list` works from a Git Bash pane (the `cmux` on PATH there is the shim; `bash system/scripts/verify-kit-install.sh` prints `mux backend: wmux`). Every `cmux` command below means the same thing on both systems.

## 1. Start of day

Run `bash system/start-day.sh` once from a terminal, or open the Chief of Staff tab in cmux and type `start of day`.

What happens:

1. State rebuild. `rebuild-switchboard-state.py` refreshes `system/state/` from your sources, then `command-center-loop.sh --once` renders `system/state/command-center-latest.md`.
2. Fixed surfaces open. CoS, Launchpad, Todo, Comms Triage, Scheduled Jobs each get a tab with a startup instruction from `routines/<name>/config.yaml`.
3. Workstream tabs open only for work with a reason today: a to-do due, a meeting, an unanswered ask, or a P0. `python3 system/scripts/sod-workspace-plan.py --dry-run` shows the plan without opening anything.
4. CoS posts the ranked digest. `open <n>` and `close <n>` are literal commands you can type back.

On day one most of this is empty. That is expected; the system fills as you add workstreams and sources.

## 2. Dispatch a task

In CoS or Launchpad, type the work you want done. Examples:

```
Workstream: switchboard-maintenance
Add a test for the inject guard's double-sample path.
```

```
Draft a one-page decision doc comparing the two onboarding flows. Reviewers: engineer and skeptic personas.
```

CoS never does the work inline. It runs `router-dispatch.sh`, which picks the workstream (the `Workstream:` line wins, otherwise fuzzy match against `workstreams/`), creates a git worktree at `.amp/worktrees/<task-id>` on branch `task/<task-id>`, opens one worker tab in cmux, and replies with one line: `routed <task-id> to <workstream>`.

The worker reads the workstream `CONTEXT.md`, does the work in its worktree, and finishes with `report-to-cos.sh`, which rebases onto `main`, fast-forwards `main`, and sends one line back to the surface that dispatched it. If it needs a decision from you, it says so in that line and the tab stays open.

To create a workstream first: `python3 system/scripts/new-workstream.py --help`, then `python3 system/scripts/render-workstreams-readme.py --write`.

## 3. Review a deliverable

Anything you need to read lands as a Markdown note in the repo (which is also your Obsidian vault): `outputs/reviews/<date>-<slug>.md` for cross-cutting work or inside the owning workstream folder. The worker's report carries an `obsidian://open?vault=switchboard&file=<path>` link. Google Docs and hosted sites are created only when you ask for them.

## 4. During the day

- The command center ticks every 10 minutes (launchd `com.switchboard.command-center`) and posts a compact next-up digest into CoS at most every 30 minutes during working hours. `system/config/router.yaml` controls cadence and hours.
- `system/background-tasks.yaml` runs every 30 minutes via `com.switchboard.background-tasks`: todo refresh, context snapshot, workspace signals, retention.
- Todo tab: `python3 ~/.agents/skills/todo/scripts/todo_cli.py render-markdown --limit 5` shows the top five actionable items. Add one with `todo_cli.py add`.

## 5. End of day

In CoS type `end of day`. CoS runs the `closing-day` skill: it updates today's note under `notes/daily/`, writes a closeout section into each touched workstream's `CONTEXT.md`, reconciles to-dos, then runs `command-center-action.sh close-all --closeout-saved`, which wraps up and closes the other tabs, then CoS, then cmux. A tab with an unsent draft or a mid-turn agent defers the shutdown instead of being force-closed.

Tomorrow's start of day reads what tonight wrote.

## Where to look when something is off

- `system/state/logs/` for the command center and worker logs.
- `system/state/running-tasks.json` for every task and its status; `python3 system/scripts/running-task-state.py list`.
- `launchctl list | grep com.switchboard` to confirm the agents are loaded; `bash system/scripts/install-launchd.sh` to reload them.
- `python3 system/scripts/check-instruction-drift.py` after editing `AGENTS.md` or skills.
