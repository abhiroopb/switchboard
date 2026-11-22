# Chief of Staff / Start-of-Day System

A file-based convention for managing parallel AI agent workstreams via [cmux](https://cmux.dev). One command (`start-day.sh`) boots your entire daily operating environment: creates a daily note, reviews workstreams, decides which to open, and launches each in its own cmux workspace with context-aware prompts.

## Current Operating Model

- PM OS pins two dispatch surfaces at the top: `🧭 CoS`, then `🚀 Launchpad`. Todo and Comms Triage follow them.
- CoS is the reporting hub. It receives next-up posts, scheduled output, comms drafts, and unsolicited workspace reports.
- Launchpad is the quiet work console. It pulls current state when {{OWNER_FIRST}} enters a prompt and receives only Launchpad-origin callbacks.
- Both surfaces apply `system/prompts/dispatch-core.md` and route substantive work through `router-dispatch.sh` with an origin tag.
- `system/state/` is the live megadesk state layer. `system/today-plan.json` is still written, but only as a compatibility artifact for older launch flows.
- The cmux sidebar is action-first: description = next concrete action, plus Driver and State rows. There is no separate `Up next` row.
- Waiting-like workstreams auto-close after they emit a structured update unless `keep_open_when_waiting: true` is set in `config.yaml` as an explicit watchlist override.
- Sidebar publishing is cached. Workspace ordering, per-workspace status payloads, and the megadesk tile all keep local snapshots so steady-state refreshes do not spam cmux.
- Calendar truth now prefers `daemon/calendar-api.sh` across scheduler, megadesk planning, pre-brief, post-ingest, and todo reminder sync. The `gcal` path remains fallback-only.
- PM OS is Amp-first. Open Amp in this repo, then request workflows in natural language rather than relying on shell aliases or wrapper commands.

## Architecture

```
start-day.sh
  ├── sources cmux-helpers.sh (shared library)
  ├── Phase 1: Discover workstreams, validate prereqs
  ├── Phase 2: Bootstrap cmux in the background and clean stale workspaces
  ├── Phase 3: Open CoS workspace → Amp reads chief-of-staff-prompt.md
  │             ├── runs `syncing-context` preflight
  │             ├── creates daily note, reviews workstreams, emits today-plan.json
  │             ├── optionally runs `closing-day` in late-day CoS sessions
  │             └── runs `syncing-context` postflight
  ├── Phase 4b: Open Launchpad and background routine workspaces
  │             └── Launchpad hydrates from current state and waits quietly
  ├── Phase 4: Verify deterministic queue state (now.json/queue.json; rebuild if stale — no polling)
  ├── Phase 5: Launch workstream workspaces from the derived plan (config.yaml priority fallback)
  └── Phase 6: Archive plan, print status report
```

### Resume Flow

When a cmux session is lost (reboot, crash), re-running `start-day.sh` picks up from `CONTEXT.md` files. Each workstream's agent reads its CONTEXT.md on startup and writes back to it at the end of each session, so losing the terminal doesn't lose the context.

```
start-day.sh → cmux-helpers.sh launch <name>
  → find_or_create_workspace()
  → reset_workspace_surface() (probe first; only clears surfaces with no live Amp session)
  → launch Amp, wait_for_amp_ready()
  → send startup_instruction from config.yaml
  → Amp reads CONTEXT.md → resumes from last known state
```

## Directory Structure

```
pm-os/
├── system/
│   ├── start-day.sh              # Main entry point (run this)
│   ├── cmux-helpers.sh           # Shared library for cmux workspace management
│   ├── chief-of-staff-prompt.md  # Full prompt for the CoS agent
│   ├── prompts/dispatch-core.md   # Shared dispatch policy for CoS and Launchpad
│   ├── workstream-resume-prompt.md  # Template for good startup prompts
│   ├── scheduled-jobs.yaml       # Recurring jobs (digests, weekly status, etc.)
│   ├── state/                    # Command-center source of truth (events, queue, now, source snapshots)
│   ├── today-plan.json           # Current day's plan (written by CoS, read by start-day.sh)
│   ├── plans/                    # Archived daily plans
│   ├── logs/                     # Daily logs
│   └── scripts/                  # Helper scripts (heartbeat, notifications, etc.)
├── workstreams/
│   ├── <name>/
│   │   ├── CONTEXT.md            # Status, what's done, what's next, open questions
│   │   └── config.yaml           # Priority, description, startup_instruction
│   ├── shared/                   # Cross-workstream resources (metric definitions, etc.)
│   ├── _archive/                 # Completed/cancelled workstreams
│   └── README.md                 # Active workstream table
├── routines/
│   ├── megadesk/           # Persistent dispatcher routine over system/state/
│   ├── launchpad/                 # Quiet pull-based work console
│   ├── todo/                     # Daily to-do capture and follow-through
│   ├── comms-triage/             # Slack + Gmail + Linear triage
│   ├── meetings/                 # Meeting prep, notes, follow-ups
│   └── scheduled-jobs/           # Runs recurring jobs from scheduled-jobs.yaml
├── notes/
│   └── daily/                    # YYYY-MM-DD.md daily journal entries
├── daemon/
│   ├── journal-sync.sh           # Background daemon: watches Slack + Email, appends to daily notes
│   ├── pm-sync.sh                # Slack reply listener: monitors #owner-bot for replies, dispatches via amp
│   ├── schedule-meeting-jobs.sh  # Reads today's calendar, creates `at` jobs for pre-brief + post-ingest
│   ├── pre-brief.sh              # Called by `at` 10 min before a meeting: pulls attendee profiles, posts brief to Slack
│   └── post-ingest.sh            # Called by `at` 15 min after a meeting ends: ingests notes from Google Doc
├── people/                       # Stakeholder profiles (communication prefs, priorities)
├── knowledge/                    # Strategy, research, writing styles
└── projects/                     # Per-project folders
```

## Key Files

### `system/cmux-helpers.sh`

Shared bash library that wraps cmux CLI operations. Can be sourced by `start-day.sh` or run standalone:

```bash
# Source as a library
source system/cmux-helpers.sh

# Or run standalone to launch a workstream
bash system/cmux-helpers.sh launch <workstream-name>
```

Core functions:
- `find_or_create_workspace(name, cwd)` — Find existing workspace by name or create + rename
- `reset_workspace_surface(ws_ref)` — Interrupt running processes, exit Amp if active
- `wait_for_amp_ready(ws_ref, timeout)` — Poll terminal for Amp's prompt characters
- `prepare_workspace(name, cwd)` — Full lifecycle: find/create → reset → launch Amp → wait
- `send_to_workspace(ws_ref, text)` — Type text and press Enter
- `send_prompt_via_file(ws_ref, file)` — Tell Amp to read and follow a prompt file
- `set_workspace_status(ws_ref, key, value, icon, color)` — Sidebar status badges
- `get_workspace_surface(ws_ref)` — Resolve terminal surface ref for `read-screen`
- `run_shell_in_workspace(ws_ref, cmd, timeout)` — Run a shell command and wait for prompt return

### `system/chief-of-staff-prompt.md`

The full prompt sent to the Chief of Staff Amp session. It instructs the agent to:

1. **Run CoS preflight sync**: refresh indexes, people summary, and megadesk state
2. **Gather context**: git activity, yesterday's note, to-do reconcile, today's calendar
3. **Create daily note**: Priorities, carry-forwards, to-dos, calendar, notes sections
4. **Review workstreams**: Read CONTEXT.md + config.yaml, decide which to open (max 7/day)
5. **Write plan**: Emit `system/today-plan.json` with workstreams to open/skip and prompts
6. **Late-day branch**: if the day is ending, run `closing-day` before stopping
7. **Run CoS postflight sync**: leave state and indexes coherent for the next session

### `workstreams/<name>/CONTEXT.md`

Each workstream's "save file." The agent reads it on startup and writes back at session end.

Example structure:
```markdown
# Example Project: Customer Activation

## Status: Active
**Last worked:** 2026-01-15

## Objective
Activate existing customers on the new product. Drive the weekly activation metric.

## What's Done
- Knowledge base created
- Activation definition established
- Weekly status updates flowing

## Current State
- Weekly active accounts: 1,200 / 3,000 target
- The onboarding checklist PR is awaiting review
- Staffing gap: the import flow and the reminders feature have no owner

## Open / Next Steps
1. Raise the staffing gap with your manager
2. Follow up on the open reply
```

### `workstreams/<name>/config.yaml`

Metadata and startup instructions. Generated by CoS on first run if missing.

```yaml
name: "Example Project: Customer Activation"
auto_open: false
priority: high
stale_after_days: 7
description: "Activate customers on the new product, drive the weekly activation metric."
startup_instruction: "Read CONTEXT.md. Check the project tracker and Linear tickets for blockers. Draft this week's metrics update for your manager."
```

Optional workspace-visibility controls:

```yaml
auto_open: false                  # startup planner may still choose to open it
keep_open_when_waiting: true      # keep the cmux tab open after it emits waiting/paused/scheduled
```

- `auto_open` only affects startup/default selection behavior.
- `keep_open_when_waiting` is the explicit watchlist flag. Use it sparingly for tabs you want visible while they are waiting on external movement.
- Current intended watchlist examples: launch-day or externally moving work such as a launch you are tracking day by day.

### `routines/` vs `workstreams/`

- **Workstream** = finite project (has deliverables, has a "done" state)
- **Routine** = ops loop (infinite, runs on cadence: todo, comms-triage, meetings)

Both use the same CONTEXT.md + config.yaml contract. Routines are launched independently in Phase 4b.

### `daemon/` — Background Daemons

The system runs several background processes via launchd:

**`journal-sync.sh`** (every 15 min via launchd)
- Searches Slack for your sent messages and mentions
- Checks Gmail for new messages
- Cross-links entries against `people/` and `workstreams/` directories
- Appends LogSeq-style timestamped entries to today's daily note

**`pm-sync.sh`** (every 5 min via launchd)
- Monitors the `#owner-bot` Slack channel for new replies from the user
- Dispatches replies via `amp -x` for action (e.g., "mark this done", "reply to that thread")
- Lightweight listener only — meeting logic was moved out to dedicated scripts

**`schedule-meeting-jobs.sh`** (once per morning, via Chief of Staff or launchd)
- Reads today's Google Calendar events
- Uses `daemon/calendar-api.sh` to call the Google Calendar REST API directly when the agent-tool path is flaky
- Creates `at` jobs to run `pre-brief.sh` (10 min before) and `post-ingest.sh` (15 min after each meeting)
- Idempotent: tracks scheduled event IDs to avoid double-scheduling

**`pre-brief.sh`** (triggered by `at`)
- Pulls attendee list from the calendar event
- Reads stakeholder profiles from `people/`
- Posts a pre-meeting brief to Slack with attendee context, open action items, and recent interactions

**`post-ingest.sh`** (triggered by `at`)
- Checks for meeting notes in Google Drive (Gemini-generated transcripts)
- Ingests notes, updates people profiles, appends a summary to the daily note Activity Log, and notifies via Slack plus the Amp workspace queue

Install the launchd plists:
```bash
cp system/launchd/com.pm-os.journal-sync.plist ~/Library/LaunchAgents/
cp system/launchd/com.pm-os.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pm-os.journal-sync.plist
launchctl load ~/Library/LaunchAgents/com.pm-os.daemon.plist
```

## Prerequisites

- [cmux](https://cmux.dev) (Settings > Automation > Socket Control Mode > "Full open access")
- [Amp CLI](https://ampcode.com) (`amp` in PATH)
- `jq` (`brew install jq`)
- `python3` with PyYAML (`pip3 install pyyaml`)

## Recommended Entry Points

Open Amp in `{{PM_OS_ROOT}}`, then use natural-language requests such as:

- `start the day`
- `chief of staff`
- `sync PM OS`
- `end of day`
- `close of day`

The CoS prompt itself now owns the sync-at-start, sync-at-end, and late-day closeout behavior.

## Usage

```bash
# Full start-of-day
bash system/start-day.sh

# Dry run (preview without mutating)
bash system/start-day.sh --dry-run

# Open all workstreams (skip CoS selection)
bash system/start-day.sh --all

# Reuse existing workspaces only (no new ones)
bash system/start-day.sh --reuse-only

# Launch a single workstream manually
bash system/cmux-helpers.sh launch example-activation
```

## How It Works End-to-End

1. You run `start-day.sh` from any terminal
2. cmux launches or reuses in the background and cleans stale workspaces
3. A "Chief of Staff" workspace opens with Amp
4. Amp reads the CoS prompt, checks git/calendar/todos, creates the daily note
5. Amp reviews workstream CONTEXT.md files, writes `today-plan.json`
6. The script parses the plan and opens each recommended workstream in its own cmux workspace
7. Each workstream's Amp reads CONTEXT.md and picks up where it left off
8. Meanwhile, `journal-sync.sh` runs every 15 min, appending cross-linked activity to the daily note
9. At session end, each agent updates its CONTEXT.md with new state

## Sidebar Semantics

The cmux sidebar is intentionally compact and action-oriented:

- The workspace description is the next thing to do, not a generic status sentence.
- The remaining status rows are:
  - Driver: `👤 You`, `🤖 Agent`, `🤝 Others`, or `📚 Reference`
  - State: `🟥 Needs review`, `🟨 Active`, `🟦 Waiting`, `🟩 Reference`
- There is no separate `Up next` row anymore. The summary already carries the next action.
- Non-urgent tabs use a neutral rail; only urgent review/blocker states get stronger emphasis.
- Waiting-like workstreams auto-close after they emit a structured update unless `keep_open_when_waiting: true` is set in `config.yaml`.
- The publishers cache the last rendered payload and the last sidebar arrangement snapshot, so background refreshes only send changed rows back to cmux.

## Calendar Reliability

- `daemon/calendar-api.sh` is the preferred calendar path for the scheduler, megadesk planning, pre-brief, and post-ingest.
- `system/scripts/rebuild-megadesk-state.py` now prefers that same direct path when it builds focus-window and next-meeting context.
- The shared todo skill at `~/.agents/skills/todo/scripts/todo_cli.py` now uses the same direct API fallback for freebusy checks, calendar hold create/update/delete, and meeting-based auto-reconcile.
- The `gcal` / `pmos calendar` path is still present, but only as fallback while its auth remains flaky.

## Customization

- Edit `chief-of-staff-prompt.md` to change how workstreams are selected
- Edit `scheduled-jobs.yaml` to add recurring jobs
- Add workstreams by creating `workstreams/<name>/CONTEXT.md`
- Add people profiles in `people/<firstname-lastname>.md` for cross-linking
- The preamble in `cmux-helpers.sh` tells workstream agents to skip session-start boilerplate — adjust if your AGENTS.md has different conventions
