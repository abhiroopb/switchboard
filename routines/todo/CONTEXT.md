# Todo Routine

## Purpose
Capture, prioritize, and follow through on action items across all sources.

## Cadence
Daily (session start + end of session)

## Loop
1. **Verify before surfacing** — Read todos through `todo_cli.py`, never directly from `todo.json`. The CLI checks the bounded set it will show against each exact `source_ref` and linked workstream. It auto-closes unambiguous evidence. It moves suggestive evidence into `completion_confirmations` with the evidence and a one-command close action.
2. **Live-check product gates (personalize here)** — Before any list or reminder, the todo CLI runs `system/scripts/ld-reconcile.py` when that script exists. Write one for your product if a feature-flag or ticket system can prove a to-do done. The check must stay read-only against the external system.
3. **Full session reconciliation** — Use `todo_cli.py auto-reconcile` in the todo routine for the broader Gmail, Slack, Drive, ticket, calendar, and workstream scan. The surface gate does not replace maintenance scans.
4. **Normalize** — Bucket items into `actionable`, `waiting`, `scheduled`, and `someday` so the top list only includes work that can move now. Treat `waiting` as the Watchlist, reserved for true external blockers.
5. **Route** — `P0` and `P1` items should have a `due`, `review_on`, or `workstream`. Multi-step tasks belong to the owning workstream, not as duplicate free-floating todos.
6. **Surface** — Keep P0 work in cmux. Show the top 3 actionable P1s each morning as secondary focus, sorted by due/review urgency and routing clarity. Treat 5 as a hard cap for the daily active list unless the owner explicitly pulls more work into today.
7. **Flag overdue** — Items past due get called out. Suggest reprioritize or close.
8. **Staleness check** — Items open 5+ days get bumped. Ask if still relevant.
9. **Auto-capture** — During triage, silently add directed Slack asks, actionable human emails, and every structured Gemini meeting-note action assigned to the owner. Meeting action capture must be deterministic and completeness-checked rather than relying only on a headless model. Dedupe against source refs and equivalent title stems.
10. **End-of-session** — Remind only about open P1s due or up for review within 48 hours. P0s remain visible in cmux.
11. **Weekly checkpoint** — On Monday, show at most 5 actionable P2 candidates to promote, schedule, or drop. Prune completed items older than 7 days.
12. **Wake waiting work** — Keep waiting items hidden until `review_on` arrives. A new matching Slack, Gmail, Linear, Jira, GitHub, or Docs signal wakes the existing item instead of creating a duplicate.
13. **Promote chosen work** — When the owner explicitly decides to take action on a todo, create a focused workstream for it and route the todo there instead of leaving it as a loose task.

## Presentation
- This layout is a hard contract for the todo routine. When surfacing todo items, never substitute prose, bullets, or a custom layout unless the owner explicitly asks for a different format in that turn.
- When the helper is available, use `python3 ~/.agents/skills/todo/scripts/todo_cli.py render-markdown ...` so this contract is emitted by code rather than recreated manually.
- Keep the actionable bucket itself capped at 5. If more than 5 items are candidates for `actionable`, move the overflow into `scheduled` instead of simply hiding them from the rendered view.
- Use the user-facing label `Watchlist` for the `waiting` bucket.
- Default todo-tab view should use a **time-bucketed table layout** so the owner can choose work based on available time.
- Break visible actionable work into three sections: `⚡ Quick hits (30-45 min)`, `✍️ Medium lifts (1-2 hours)`, and `🧠 Deep work (2-4 hours)`.
- Within each section, use a markdown table with columns in this order: `# | To do | Priority | Description | Action I need to take | Workspace | Link`.
- Render priority with color circles instead of plain text: `🔴 P0`, `🟡 P1`, `🟠 P2`, `⚪ P3`.
- Prefer linked items only. If an item has neither a useful `source_ref` nor a linked `workstream`, exclude it from the surfaced table.
- In the `Workspace` column, show the current cmux workspace name and ref when available, for example `📁 Partner Doc Review (workspace:11)`. If no live workspace exists, show the owning workstream slug or `—`.
- In the `Link` column, prefer the raw source doc/thread/web URL so clicks open the artifact instead of a local editor. Only fall back to the workstream file when there is no better source link.
- Use visual separators like `===` and `----` plus light emoji labeling, but keep the layout skimmable rather than decorative.

## Data
- Persistent store: `system/state/todo.json` under PM_OS_ROOT
- Skill: `todo`

## Current State
Compute live at session start — never trust counts written into this file:
```bash
python3 ~/.agents/skills/todo/scripts/todo_cli.py summary
```
Report actionable/watchlist/scheduled/done counts, `unrouted_high_priority`, and overdue actionable items from that output. Flag drifted or placeholder workstream slugs (todos whose `workstream` doesn't match a directory under `workstreams/`) as a recurring smell to clean up.
