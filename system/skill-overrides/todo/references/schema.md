# To-do file schema

The file is one JSON object written with 2-space indent and a trailing newline. Every top-level key is optional when reading. Unknown keys, at the top level and on items, survive every rewrite.

## Top level

| Key | Type | Meaning | Default |
|---|---|---|---|
| `version` | int | Schema version. Always written as `2`. | `2` |
| `rules` | object | Policy settings. Only `max_actionable_daily` is read. | `{"max_actionable_daily": 5}` |
| `rules.max_actionable_daily` | positive int | How many items stay actionable each day. Anything invalid means 5. | `5` |
| `recurring_templates` | array | Recurring templates, described below. | `[]` |
| `items` | array | The to-dos. | `[]` |

An older file that holds `todos` instead of `items` is read as `items`. The new name is written on the next save.

## Item fields

`add` writes these keys in this order.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Random UUID. The stable identity. |
| `title` | string | Short instruction, verb first. |
| `source` | string | Where it came from: `manual`, `slack`, `gmail`, `meeting`, and so on. |
| `source_ref` | string or null | Link or id of the origin, stored in canonical form. |
| `priority` | string | `P0` to `P3`. |
| `status` | string | `open`, `waiting`, `scheduled`, `someday`, or `done`. |
| `due` | string or null | Deadline, an ISO date or datetime. |
| `review_on` | string or null | Date to look again. A reminder, not a deadline. |
| `snooze_until` | string or null | Hidden until this time. Stored exactly as given. |
| `created_at` | string | UTC time with a `Z` suffix, to the second. |
| `completed_at` | string or null | UTC time it became done. |
| `context` | string or null | Short note. |
| `tags` | array of strings | Free tags. |
| `workstream` | string or null | Owning workstream slug. |
| `project` | string or null | Project name when there is no workstream. |
| `estimate_min` | positive int | Effort in minutes. Falls back to the workstream estimate, then 15. |
| `requires_user` | bool | True when only the person can do it. |
| `calendar_event_id` | string or null | Id of the hold on the to-do calendar. |
| `calendar_event_url` | string or null | Link to that hold. |
| `calendar_event_start` | string or null | Hold start, UTC. |
| `calendar_event_end` | string or null | Hold end, UTC. |
| `calendar_synced_at` | string or null | Last time the hold was created, moved, or cleared. |

Other commands add these keys later.

| Field | Written by | Meaning |
|---|---|---|
| `resolution` | `complete --reason`, `set-status --reason` | Why it was closed or changed. |
| `dropped_reason` | `set-status --status dropped --reason` | The same text, kept for older readers. |
| `related` | PR-set guard | Ids of items linked by a refused close. |
| `calendar_create_generation` | hold deletion | Random value that changes the id of the next hold. |
| `generated_from_template` | template materialization | `true` on items a template made. |
| `template_id` | template materialization | The template that made the item. |

Other scripts may write `pr_refs` (extra PR references the guard reads) and `updated_at` (counts as a touch for `archive-stale`). Older files may carry `completed` in place of `completed_at`.

## Older values that still read

- Priority `"0"` and `"1"` mean P0, `"2"` P1, `"3"` P2, `"4"` P3. `P4` means P3. A JSON integer `n` means `Pn`. A missing priority means P3.
- Status words map to the five statuses: `completed`, `closed`, `cancelled`, `resolved`, and `dropped` mean done. `blocked` and `needs-response` mean waiting. `snoozed` and `deferred` mean scheduled. `backlog` and `maybe` mean someday. `actionable`, `in-progress`, and `new` mean open. Any other word is kept as written and treated like open.

## Recurring templates

| Field | Type | Rule |
|---|---|---|
| `template_id` | string | Lowercase letters, digits, and dashes. |
| `title` | string | Required. |
| `recurrence_rule` | string | `weekly:<weekday>` or `monthly:<1-31>`. |
| `next_due` | ISO date | The next date an item is due. |
| `priority` | string | `P0` to `P3`. |
| `materialization_window_days` | positive int | How many days ahead an item appears. Default 3. |
| `estimate_min` | positive int or null | Copied to generated items. |
| `source` | string | Default `recurring-template`. |
| `status` | string | Default `open`. |
| `workstream`, `project`, `context` | string or null | Copied to generated items. |
| `tags` | array | Copied to generated items. |
| `last_materialized_at` | string or null | Last time the template made an item or moved forward. |

## View-only fields

Views add derived keys to copies of items and never write them back: `bucket`, `daily_cap_overflow`, `routing_kind`, `due_state`, `age_days`, `needs_routing`, `fingerprint`, `is_duplicate`, `duplicate_of`, `source_type`, and on confirmation items `completion_evidence` and `confirmation_action`.

## Archive files

One file per UTC month at `todo-archive/YYYY-MM.json`:

```json
{"version": 1, "month": "2026-01", "updated_at": "2026-01-15T18:00:00Z",
 "records": [{"todo_id": "<id>", "archived_at": "<UTC time>", "reason": "<why>", "item": {"...": "the stored item"}}]}
```

`item` is the stored item exactly as it was.
