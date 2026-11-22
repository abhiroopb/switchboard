---
name: todo
description: Persistent to-do list with deduped capture, verified views, routing, reminders, recurring templates, and private calendar holds. Use when adding, checking, completing, snoozing, triaging, or prioritizing to-dos and action items.
---

# To-do list

## What it is

One list lives at `system/state/todo.json` under the repo root. It is local to this machine and gitignored. Read it and change it only through `scripts/todo_cli.py`. Editing the file by hand skips the lock, the dedupe, and the calendar rule.

If the file is missing, see `SETUP.md`. Field meanings are in `references/schema.md`.

```bash
TODO="python3 ~/.agents/skills/todo/scripts/todo_cli.py"
```

## When to use it

- Capture an action item from any source: a message, a meeting, a worker report, or a request typed in chat.
- Show what to work on today.
- Close, snooze, re-prioritize, or drop an item.
- Run the morning, end-of-day, and Monday reviews.
- Archive old work, or bring an archived item back.

## Showing the list

Use `snapshot` for scripts and `render-markdown --limit 5` for the person.

```bash
$TODO snapshot
$TODO render-markdown --limit 5
$TODO summary
$TODO list --bucket waiting --json
```

Every view checks the items it is about to show before it shows them. Exact evidence closes an item on the spot: the person replied in the thread, a doc comment was resolved, a pull request merged, a ticket finished, a meeting happened, or a workstream file appeared. Weaker evidence moves the item to the confirmation bucket. Show confirmation items apart from open work, each with its close command, and close them only after the person says yes.

Add `--dry-run` to see what a view would close without writing. Never build a list from the raw file. The raw file has no buckets, no daily cap, and no verification.

## Capturing

```bash
$TODO add --title "Send the launch checklist to design" \
  --source slack --source-ref "<permalink>" --priority P1 --due 2026-01-15 \
  --workstream launch-readiness --context "Asked in the design channel"
```

- A title is a short instruction: verb first, one action.
- P0 and P1 items need routing: `--due`, `--review-on`, `--workstream`, or `--project`. Pass `--allow-unrouted` only when none of those is known yet.
- The CLI dedupes by source reference and by title stem. A duplicate prints `duplicate_skipped` and writes nothing. Pass `--allow-duplicate` only when two items truly differ.
- `--requires-user true` marks work only the person can do. Leave it out and the CLI infers it from the wording.
- Owner actions from worker reports arrive through `system/scripts/owner_action_capture.py`. Do not add them by hand a second time.

## Changing items

```bash
$TODO complete --id <id> --reason "Sent in the thread"
$TODO set-status --id <id> --status waiting --review-on 2026-01-15
$TODO set-status --id <id> --status dropped --reason "No longer needed"
$TODO set-status --id <id> --priority P1 --due 2026-01-15
$TODO snooze --id <id> --until 2026-01-15T09:00:00-0800
```

`set-status` also takes `--workstream`, `--project`, `--estimate-min`, and `--requires-user`. An empty value for `--due`, `--review-on`, or `--snooze-until` clears that field.

**PR-set guard.** A close reason such as "superseded by <id>" or "duplicate of <id>" points the item at another to-do. When the two items cover different pull requests, the CLI refuses with exit code 3, links the two items in `related`, and leaves the item open. Check the PRs. If the close is right anyway, rerun with `--allow-pr-mismatch`.

## Statuses and buckets

| Status | Bucket | Meaning |
|---|---|---|
| `open` | actionable | Ready to work on. |
| `waiting` | waiting (shown as Watchlist) | Blocked on someone outside. Use it only for a true outside blocker. |
| `scheduled` | scheduled | Hidden until its snooze time or review date. |
| `someday` | someday | Parked with no date. |
| `done` | done | Finished. `dropped` is stored as `done` with a `dropped_reason`. |

Only five items stay actionable each day (`rules.max_actionable_daily`). The rest move to scheduled for the day and carry `daily_cap_overflow`. A waiting or scheduled item comes back to actionable once its snooze time passes or its review date arrives.

## Reminders

```bash
$TODO reminders --mode morning --limit 3
$TODO reminders --mode eod --limit 3
$TODO reminders --mode weekly --limit 5   # Mondays
```

Morning shows today's P1 focus. End of day shows P1 items due within two days. Weekly shows P2 work worth a look.

## Calendar holds

P0 and P1 items with a future due date get a hold on the dedicated secondary calendar `To-dos (PM OS)`, never on the primary calendar.

- The hold length is `estimate_min`, else the workstream's estimate, else 15 minutes.
- The first slot is 1:00 PM Pacific on the due date, and later holds that day stagger in 15-minute steps. Every hold ends by 5:00 PM.
- A due date on a weekend moves to the Friday before. A full day moves the hold to the next business day.
- Holds are private and show as Free. P0 holds are tomato, P1 holds are banana.
- Dropping an item below P1, or finishing it, clears the hold.

To get a reminder without reserving time, set `review_on` instead of `due`. Run `$TODO sync-calendar` to repair holds for the whole list.

## Recurring work

```bash
$TODO add-template --template-id weekly-status --title "Write the weekly status" \
  --recurrence-rule weekly:friday --next-due 2026-01-16 --priority P2
$TODO materialize-templates
$TODO list-templates
```

A template makes one item when its next due date is inside its window (3 days by default). Completing that item moves the template to its next date: the next matching weekday, or the same day next month (clamped to the month's end). The feature runs only while the `recurrence_enabled` flag in `system/config/workflow-upgrade-flags.yaml` is on.

## Reconcile and archive

```bash
$TODO auto-reconcile --dry-run --diagnose
$TODO auto-reconcile
$TODO archive-done
$TODO archive-stale --days 14 --priority P2,P3 --batch-size 25
$TODO restore-archived --id <id>
```

`auto-reconcile` scans every open item against the collector caches and the connected tools, then closes the matches. The default window for `archive-stale` is 14 days. Archives are monthly files in `system/state/todo-archive/`. An item that still has a calendar hold is never archived.

## P0 work

After adding or promoting a P0 from inside a workstream, run `system/scripts/ensure-p0-workspaces.sh` in the same turn.

## Quick replies

The person can answer a numbered list with short replies. Numbers refer to the list just shown.

| Reply | What runs |
|---|---|
| `done 1` | `complete --id <item 1>` |
| `snooze 2` | `snooze --id <item 2> --until` tomorrow at 9:00 AM Pacific |
| `snooze 2 until tomorrow` | `snooze` with the time the person named |
| `open 3` | Open item 3's source link. |
| `show all` | `list --bucket all` |
| `add: <text>` | `add --title "<text>"` with the routing the text gives |
| `delete 4` | `set-status --id <item 4> --status dropped --reason "<why>"` |
| `prioritize 3 as P0` | `set-status --id <item 3> --priority P0` |

## Safety

- One writer at a time. Every write takes the sidecar lock, so never run writes in parallel.
- The skill never sends anything to anyone. Drafts and sends belong to other skills.
