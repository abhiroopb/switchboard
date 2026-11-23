---
name: closing-day
description: "Wraps the PM day by updating today's note, reconciling follow-through, and leaving a clear carry-forward snapshot. Use when asked to do end of day, EOD, close of day, wrap the day, or finish a late-day CoS session."
---

# Closing Day

Run a compact end-of-day closeout for switchboard.

## Use For

- `end of day`
- `close of day`
- `EOD`
- `wrap the day`
- late-day CoS sessions when {{OWNER_FIRST}} is winding down

## Workflow

### CoS-owned closeout

Full close requests (`close` without a target, `close everything`, `close of day`,
`end of day`, or `EOD`) run this workflow directly in the current local CoS thread.
Do not create a Router, hand off, pause another task, or require a routed task ID.
This is a narrow lifecycle exception to the CoS delivery gate, not permission for other delivery.
`close N` still uses the existing targeted close action unchanged.

Run the durable steps below before teardown. A late-day review alone does not
authorize workspace closure; require an explicit full-close request for step 8.
`command-center-action.sh close-all` returns this workflow's instructions without
closing anything. It requires `--closeout-saved` for the final shutdown phase.

1. Read today's daily note in `notes/daily/`.
2. Review:
   - `system/state/now.json`
   - `system/state/queue.json`
   - `system/state/sources/todo.json`
   - today's relevant lines in `system/state/events.jsonl`
   - the canonical knowledge layer for today's resolved facts (completed
     meetings, done todos, superseded plans belong in the retrospective;
     current/upcoming items feed carry-forward):

```bash
python3 system/scripts/query-knowledge.py --days 1 --include-historical --limit 30
```

   Apply `system/config/temporal-contract.md`: judge items by event time, not
   by when a doc mentioning them was written; label anything older than its
   freshness window as background, never as a today-update.
3. Run the todo closeout view:

```bash
python3 ~/.agents/skills/todo/scripts/todo_cli.py render-markdown --limit 5 --auto-reconcile --dry-run
python3 ~/.agents/skills/todo/scripts/todo_cli.py reminders --mode eod --limit 3
```

The reconciliation preview must stay dry-run: applying it also synchronizes
calendar holds. Record verified completions and proposed changes in the daily
note. Use the todo workflow separately for any calendar-changing action that
{{OWNER_FIRST}} explicitly authorizes; full close alone does not authorize those writes.

Only carry forward P1s due or up for review within 48 hours. P0s remain visible in cmux; do not repeat the whole P1/P2 backlog.

4. **Check product-specific nightly drains (personalize here).** If a
   nightly background task owns a knowledge-base ingest or similar drain,
   read its status file here:

```bash
cat system/state/knowledge/kb-feed-status.json
```

   Record pending work for its existing owner. Do not launch new delivery during
   shutdown or stop the nightly drain. Never merge candidates into BRAIN.md from
   CoS or move feed files by hand.
5. Update today's note with a short `## End Of Day` section if it does not already exist.
6. Capture only four things:
   - what moved today
   - carry-forward items
   - waiting items that matter tomorrow
   - first thing tomorrow
7. **Preserve per-workstream context.** For every workstream that had meaningful activity today (touched in the digest, mentioned in today's daily note, or scaffolded/updated during the session), append a `## EOD YYYY-MM-DD` section to its `workstreams/<slug>/CONTEXT.md` capturing:
   - what changed or shipped today
   - new artifacts (doc IDs, tracker record IDs, PR numbers, draft locations)
   - carry-forward action items specific to that workstream
   - any owner / dependency notes worth preserving across sessions
   Use the explicitly allowed, append-only EOD command below. Direct arbitrary
   CONTEXT edits still require the normal CoS delivery approval. The helper
   accepts only an existing local workstream and an EOD section, never code or
   arbitrary write destinations. Re-read before appending. Do not edit a file
   an active owner is changing; preserve that carry-forward in today's note.
   Missing folders also go in today's note, not a new scaffold.

```bash
python3 system/scripts/eod-context-append.py --slug <existing-slug> --body '<dated, evidence-backed closeout bullets>'
```

8. **Finish in CoS, then detach.** Re-read the saved daily note and EOD sections.
   Print the concise closeout summary in chat before the final action:

```bash
bash system/scripts/command-center-action.sh close-all --closeout-saved
```

   No task ID is required. This launches `start-end-of-day-teardown.sh`, which
   acquires one kernel lock and starts the detached process with private log
   files.

   Teardown waits for each wrap-up turn to finish, then closes the workspace after verifying an idle footer and an empty input box. It uses a 90-second pre-send wait, a 900-second wrap-up wait, and up to three retry passes within a 3,600-second total ceiling. Accepted wrap prompts are not sent again. The only intentional survivors are workspaces with an unsent draft or a pending tool approval; never force-close either case. An agent still working stays protected while teardown waits and retries. Unreadable state, unknown delivery, unrelated live processes, and close failures remain explicit verification blockers, not successful completion. Report each survivor with its exact reason. A verified dead Thread Status stub closes without signalling any process. CoS closes last after its final turn finishes, then cmux quits only after verifying that no workspaces remain.

   Read the returned log on failure. Use `~/Development/switchboard/docs/runbooks/cmux-workspace-launch.md`, section "End-of-day teardown", for wait settings and safe verification.
   `--dry-run` previews only. `--keep-app` still closes real workspaces and is
   not a safe test mode without fake cmux.

### Legacy queued Router compatibility

Do not create new EOD Router tasks or resume held EOD tasks. A previously
authorized queued Router may finish the same durable steps and invoke:

```bash
bash system/scripts/start-end-of-day-teardown.sh \
  --invoking-workspace "$ROUTER_WORKSPACE_REF" --task-id "$ROUTER_TASK_ID" \
  --origin "$ROUTER_ORIGIN" --summary '<saved closeout summary>'
```

This retains its report contract and closes that Router after CoS. Duplicate
launches are suppressed without changing held task state.

## Guardrails

- Keep the note update short and useful.
- Do not create a long retrospective unless {{OWNER_FIRST}} explicitly asks for it.
- Only add new todos if there is a clearly missing explicit commitment from today's work.
- If this runs inside a CoS session, finish by returning control to CoS rather than launching a separate workflow.
- Never commit, push, suspend remote machines, unload daemons, or stop independent background jobs as a closeout side effect.
- Preserve all approval gates. A close request is not approval for external messages, calendar writes, or pending drafts.

## Output formatting

Per AGENTS.md Output Preferences "no paragraph walls" rule: any chat-side EOD recap longer than 2-3 sentences MUST use bullets + bold sub-headings (or a 2-column table for multi-section recaps). Daily note edits stay in the existing markdown table/section structure of `notes/daily/YYYY-MM-DD.md`.
