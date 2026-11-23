# switchboard State

This directory is the switchboard state layer.

`system/state/` is the source of truth for the switchboard. `system/today-plan.json`
still exists, but only as a compatibility artifact for older launch flows.

`system/today-plan.json` may mark shortlisted workstreams with `open_now: true`.
`start-day.sh` will auto-open only those workstreams during the default morning run.
Other planned workstreams stay queued unless you pass `--with-resolvers` or `--all`.

## Files

- `todo.json`: canonical persistent to-do list (source of truth, tracked in git — the only tracked file here besides this README). All consumers read and write this path directly; the old `~/.config/amp/todo.json` location is retired, and `system/scripts/install-switchboard-bin.sh` recovers any stray file that reappears there. Sidecar `.lock` and `.bak-*` siblings stay untracked. Distinct from the derived snapshot at `sources/todo.json`.
- `events.jsonl`: append-only facts and workspace updates
- `queue.json`: derived, ranked action cards
- `now.json`: the single recommended action plus fallbacks
- `sources/comms.json`: latest comms collector snapshot
- `sources/comms-candidates.json`: cached unread Slack and Gmail candidates with likely workstream hints for CoS/start-day reopen decisions
- `sources/meetings.json`: latest meetings collector snapshot
- `sources/workstreams.json`: normalized workstream metadata
- `sources/scheduled-jobs.json`: latest scheduled-job collector snapshot
- `sources/todo.json`: normalized todo snapshot with actionable backlog summary, routing gaps, stale-review hints, and dedupe hints
- `sources/workspace-updates.json`: latest structured update per routine or workstream
- `sources/recall-debug.json`: latest bounded recall packs and suppressed-source debug output
- `sources/rule-proposals.json`: review queue for proposed durable rule changes
- `sources/import-status.json`: latest curated recall-import status and per-document metadata
- `sources/explorer-index.json`: generated explorer snapshot for recall, recurrence, and rule surfaces
- `sources/workflow-upgrade-shadow.json`: shadow-mode comparison for recall, recurrence, and queue deltas before cutover
- `sources/manual-open-workstreams.json`: temporary keep-open markers for workstreams {{OWNER_FIRST}} explicitly opened to take action on
- `sources/workspace-status-published.json`: last rendered per-workspace sidebar payload, used to skip redundant cmux writes
- `switchboard-status-published.json`: last rendered switchboard tile payload, used to skip redundant cmux writes
- `sidebar-arrangement.snapshot`: last workspace ordering snapshot, used to skip redundant sidebar reordering

## Calendar Source Of Truth

- `daemon/calendar-api.sh` is the preferred calendar path for scheduler health, switchboard planning, pre-brief, and post-ingest.
- The separate `gcal` / `switchboard calendar` path is now treated as fallback-only when the direct path is unavailable.
- The shared todo skill at `~/.agents/skills/todo/scripts/todo_cli.py` now follows the same pattern for calendar holds, freebusy checks, and reminder cleanup.

## Workspace Update Contract

Status semantics — pick `--status` by who the ball is with, not by vibe:

- `waiting` — blocked on someone or something **external** (a reply, an approval, another team). Never use it when the next action is {{OWNER_FIRST}}'s.
- `scheduled` — parked until a known date or time.
- `needs follow-up` — an external loop that we need to nudge later.
- `needs review` / `needs action` — the ball is with **{{OWNER_FIRST}}** (review, decide, approve). Use this whenever `--recommended-next-action` is addressed to them.
- `active` / `new` — an agent or {{OWNER_FIRST}} is working it right now.
- `paused` — deliberately on hold; no external dependency.
- `done` / `resolved` / `reference` — settled; kept for context only.

The workspace-status publisher also enforces this defensively: a `waiting`-style update whose recommended next action is addressed to {{OWNER_FIRST}} is upgraded to `needs review` (👤 You) in the sidebar.

When a routine or workstream pauses, waits, or completes, emit a structured update:

```bash
python3 system/scripts/emit-workspace-update.py \
  --source workstream:example-onboarding \
  --status waiting \
  --summary "Confirmed the pilot audience, now waiting on a reviewer and a start date." \
  --recommended-next-action "Send the reviewer a follow-up and lock the start date" \
  --new-action "Ping the reviewer on open questions|8|high" \
  --linked-todos waiting \
  --linked-review-on "2026-01-15"
```

That appends an event, updates `sources/workspace-updates.json`, rebuilds the queue, and refreshes the switchboard badge in cmux.

The workspace and switchboard publishers keep a cache of the last visible payload, so frequent background refreshes only touch cmux when a tile's rendered state actually changed.

For workstreams, `emit-workspace-update.py` also manages tab lifecycle:

- Waiting-like statuses (`waiting`, `paused`, `scheduled`, `needs follow-up`, `done`, `resolved`, `cancelled`, `archived`) auto-close the workstream tab by default.
- Add `keep_open_when_waiting: true` to `workstreams/<slug>/config.yaml` only when the tab should stay open as part of an explicit watchlist.
- Explicit manual opens via `cmux-helpers.sh launch <workstream>` also keep that workstream open temporarily for the active working session, even if later updates report `waiting` or `paused`.
- If {{OWNER_FIRST}} explicitly asks to pause, defer, or close a workstream, call `emit-workspace-update.py` with `--close-workspace --clear-manual-open` so that temporary explicit-launch keep-open marker is removed and the workspace closes immediately.
- If switchboard or any outer routine is closing workstream tabs on {{OWNER_FIRST}}'s behalf, it should emit a fresh final `emit-workspace-update.py` summary for each workstream immediately before the close. Closing the cmux tab alone is not sufficient because `workspace-updates.json` should reflect the latest stopping point.
- `auto_open` does not keep a waiting tab open.

You can also update explicit todo items from a workspace update:

```bash
python3 system/scripts/emit-workspace-update.py \
  --source workstream:example-activation \
  --status done \
  --summary "Wrapped the activation draft and sent the follow-up." \
  --todo-update "525b78df-cd2f-49ab-8d60-821d3fbd3c49|done"
```

## Todo Source Snapshot

`sources/todo.json` keeps the raw `todo.json` file (this directory) compatible while giving the switchboard a better lens:

- `summary`: counts for actionable, waiting, scheduled, someday, overdue, and unrouted high-priority items
- `items`: the capped daily actionable backlog, normalized with routing and `requires_user` metadata when present
- `backlog_items`: the broader non-done, de-duped backlog used by the switchboard UI so the To Do lane can show more than the daily cap
- `triage_candidates`: the full set of urgent actionable todos that still need routing
- `stale_actionable`: weekly-checkpoint style list of 7+ day old actionable P2/P3 items that should be downgraded, rescheduled, routed, or closed
- `duplicates`: likely duplicate ingests ignored for queue purposes
- `auto_reconcile`: optional summary of items automatically closed from sent mail, Slack replies, Linear completion, or attended meetings during the latest refresh
- `rules`: todo policy metadata such as `max_actionable_daily`, used to keep switchboard state aligned with the todo system's daily active-list cap
