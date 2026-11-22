---
name: triage-links
description: "Investigates dropped Slack, Google Drive, Linear, GitHub, Jira, roadmap tracker, Notion, intranet short links, and web links, pulls the right surrounding context, infers the real task behind the artifact, and prepares exact follow-through with approval commands before any write. Use when a user pastes links and wants to know what to do next or wants the system to prepare the actual follow-up work."
---

# Triage Links

Turn dropped links into execution-ready follow-through.

## Use for

- A user pastes one or more links and wants context plus next steps.
- A Slack thread points to docs, tools, tickets, or pages that need review.
- A doc, ticket, or page needs validation, drafting, reply prep, or follow-up planning.
- The right answer depends on a bounded multi-source investigation, not just the primary artifact.

## Do not use for

- Pure single-system work where the user already asked for a specific skill like `slack`, `gdrive`, or `linear`.
- Broad exploratory research without a concrete source artifact.

## Contract

This skill should:

1. Read the primary artifact.
2. Expand the highest-value linked artifacts.
3. Pull corroborating context when it is likely to change the answer.
4. **Route the artifact to a workstream** (existing match, new scaffold, park, or skip) using the same logic as the comms-triage pipeline.
5. Infer the implied deliverable, not just the literal ask.
6. Choose the best available execution surface.
7. Produce an execution-ready brief with exact planned edits, drafts, or follow-up actions stored inside the routed workstream when one exists.
8. Stop and offer approval commands before any write.

## Default mode

Default to `prepare`, not `execute`.

- Safe reads: automatic
- Drafting and edit planning: automatic
- External writes: approval required

## First step

Run the intake planner on the raw user request:

```bash
python3 .agents/skills/triage-links/scripts/link_intake.py --input "$ARGUMENTS" --format markdown
```

Use the output to drive the rest of the workflow.

After reading the primary artifact, rerun the planner with any linked targets that surfaced during investigation:

Preferred flow:

```bash
python3 .agents/skills/triage-links/scripts/write_investigation_bundle.py \
  --output /tmp/dropped-link-findings.json \
  --discovered-link "<linked target url>" \
  --evidence-note "<what you found>" \
  --affected-section "<section name>" \
  --planned-edit "<exact planned change>" \
  --reply-bullet "<reply point>"

python3 .agents/skills/triage-links/scripts/link_intake.py \
  --input "$ARGUMENTS" \
  --investigation-file /tmp/dropped-link-findings.json \
  --discovered-link "<linked target url>" \
  --evidence-note "<what you found>" \
  --affected-section "<section name>" \
  --planned-edit "<exact planned change>" \
  --reply-bullet "<reply point>" \
  --format markdown
```

Use this second pass when the dropped artifact is mainly a communication wrapper, such as a Slack thread that points at the page or doc that actually needs edits.

Prefer storing the first-pass findings in a small investigation bundle file and passing that file into the second run. The planner accepts either JSON or markdown bundles and will auto-fill discovered links, evidence notes, affected sections, planned edits, and reply draft content from that one file.

Use `write_investigation_bundle.py` to create or extend that file instead of hand-writing JSON. It always writes JSON, supports a `--seed-file` to extend an earlier bundle, and de-duplicates repeated values.

Bundle shape:

```json
{
  "discovered_urls": ["https://docs.example.com/help/article"],
  "evidence_notes": ["Help-center page still uses the old eligibility wording."],
  "affected_sections": ["Eligibility", "Support guidance"],
  "planned_edits": ["Replace the old eligibility sentence with the current policy wording."],
  "reply_bullets": ["Checked the help-center page against the latest policy."],
  "reply_draft": "Planned thread reply..."
}
```

Use the explicit evidence and edit flags only when you need to top up or override the bundle.

## Workflow

### 1. Normalize and plan

Use the intake planner output to identify:

- primary artifact
- task mode
- corroboration budget
- candidate execution surfaces
- approval commands

### 2. Investigate the primary artifact

Load the matching skill or tool path first:

- Slack permalink -> `pmos slack`, or a `slack` skill if one is installed
- Google Docs/Drive URL -> `pmos drive`, or a `gdrive` skill if one is installed
- Linear URL -> the Linear MCP, or a `linear` skill if one is installed
- Roadmap tracker URL (optional adapter) -> use your roadmap tracker's API or skill if one is installed, then treat the record like a ticket. Set its host in `normalize_url` in `scripts/link_intake.py`.
- Generic web or app page -> use its supported API first; use Chrome DevTools with the sanctioned headless profile when the API fails or lacks the capability; use `read_web_page` for read-only extraction
- Intranet short link (optional) -> resolve it first with your organization's resolver, then continue with the resolved target

For any Chrome fallback, follow the focus-safety contract in `references/tool-routing.md`. Open pages with `background: true`, never bring a page to the front, and never attach to {{OWNER_FIRST}}'s daily Chrome. A machine rule in `local/agents-local.md` wins over this on the machine it names.

If the primary artifact reveals a more concrete target, such as a help-center page, Google Doc, or ticket that actually needs changes, rerun the intake planner with `--discovered-link` so the execution target is promoted away from the wrapper artifact.

### 3. Route to a workstream

Before going deep on context expansion, decide where this artifact lives. Use the shared routing helper so the same matching and CREATE-test rules apply here as in the overnight comms-triage pipeline (`system/scripts/process-new-inbox.py`):

```bash
python3 .agents/skills/triage-links/scripts/route_to_workstream.py \
  --channel <slack-channel-name> \
  --channel-id <slack-channel-id> \
  --source-type <slack-channel|slack-dm|slack-mention|gmail|linear|github|gdrive|web|other> \
  --source-ref "<canonical url>" \
  --body-file /tmp/dropped-link-body.txt \
  --from-user "<sender display name>" \
  --format markdown
```

The helper returns one of four decisions:

| Decision | Meaning | Default action |
|---|---|---|
| `match` | Existing workstream matched (channel default, or `todo_match_terms`) | Append an inbox note under `workstreams/<slug>/inbox/YYYY-MM-DD-mentions.md`, store any drafts in `workstreams/<slug>/drafts/`, and add a routed todo via `todo add --workstream <slug>` |
| `create` | No match, but the body passes the CREATE test (multi-step + namable) and the 24h auto-create cap is not hit | Scaffold `workstreams/<slug>/` with CONTEXT.md, config.yaml (with picked emoji from `pick-workstream-emoji.py`), inbox note, and add the todo. Then update `workstreams/README.md` |
| `park` | Passes CREATE test but cap is hit, or no clean slug derivable | Save the find under `workstreams/_inbox/<channel>/YYYY-MM-DD-<topic>.md` and add a todo so it gets human-triaged later |
| `skip` | No match and CREATE test fails (too short, vague, or no project/domain noun) | Keep the brief in chat only — no workstream side effect |

Always surface the routing decision to the user **before** scaffolding or writing inbox notes. Use the approval commands the helper emits (e.g. `approve route-existing`, `approve route-create`, `approve park`, `skip`).

When a workstream exists or is being created, primary artifacts of the work — drafts, planned edits, source captures — live in the workstream folder, not just in chat or `/tmp`. The reply or doc draft should reference the workstream path so it remains the system of record.

#### System-only workstreams (route to feature workstream instead)

Some workstreams own a **system**, not a feature. Route to them only when the body explicitly discusses the system itself, never just because a feature they track was mentioned. Per-feature content reviews, launch threads, design feedback, and QA chatter belong in the **feature's own workstream**, even when the feature's record is in the system workstream's scope.

| System workstream | Owns | Route here only when body mentions |
|---|---|---|
| `roadmap-hygiene` (example, not shipped) | Roadmap field hygiene, status update cadence, audit sweeps, cross-record portfolio views | "roadmap", "audit", "status update", "field audit", "rollup" |

If a feature thread (e.g. a support-article review for an export feature, QA on a reminders feature, design feedback on a search page) routes to a system workstream by accident, **create the missing per-feature workstream** and re-route there. Suggest it explicitly to {{OWNER_FIRST}} when the helper picks a system workstream and the body is about a feature.

The `route_to_workstream.py` helper enforces this in code via `SYSTEM_ONLY_WORKSTREAMS`. Update that map (and this table) together when adding a new system workstream.

### 4. Expand the right surrounding context

Follow `references/source-expansion.md`.

Do not fan out everywhere. Expand only the sources likely to change the answer.

Typical corroboration sources:

- active workstream context
- related project docs or decisions
- Slack search for recent clarifications
- linked Linear or Jira items
- roadmap tracker context, when the optional adapter is set up and the artifact is about roadmap or launch state
- an internal search tool, if installed and the answer depends on broad internal context

### 5. Infer the real ask

Always distinguish between:

- `explicit_request`: what the artifact literally asks for
- `implied_deliverable`: what {{OWNER_FIRST}} actually needs next

Examples:

- Slack thread asking "can you check this page" -> implied deliverable is a validation result, planned edits, and usually a reply draft
- Google Doc asking for review -> implied deliverable is comments, edits, or a short synthesis
- Roadmap record or ticket link -> implied deliverable may be triage, follow-up filing, or a status readout

#### Slack-wrapper deliverables

When the primary artifact is Slack, do **not** let the Slack surface become the work product by default. Slack is often just the request wrapper.

- If the Slack ask requests a concrete artifact such as a one-pager, summary, PRD, project list, decision doc, or draft, the implied deliverable is that artifact.
- The next action should be to draft or prepare the artifact in the workstream (or the linked doc/sheet if one exists), not to post an acknowledgement.
- A Slack reply is secondary and should be offered only after the artifact exists, or when a genuinely blocking input is missing.
- If a blocking input is missing, phrase the Slack reply as a request for that input and do not present it as the main execution action.
- Approval options should reflect this order: route/create workstream -> draft the artifact / identify missing source -> then optionally reply in Slack.

### 6. Prepare the actual follow-through

Follow `references/tool-routing.md`.

Prefer the strongest available path:

1. prepare the edit or draft in the target surface
2. if that surface is not directly reachable, prepare a tool-specific execution plan
3. if no tool path exists, produce a paste-ready draft and exact manual steps

For Slack-wrapper deliverables, the "target surface" is the workstream draft, Google Doc, sheet, ticket, or other concrete artifact requested in the Slack message. It is **not** the Slack thread unless the explicit ask is to reply.

### 7. Persist the brief + auto-launch the workspace

In CoS or Launchpad, route through `router-dispatch.sh` before performing these steps. The owning worker writes the artifacts and captures the todo.

When the routing decision is `match` or `create` AND the user approves the routing command (e.g. `approve match <slug>`, `approve route-existing`, `approve route-create`), the skill MUST do all of the following in a single response without asking again:

1. Write the inbox note under `workstreams/<slug>/inbox/YYYY-MM-DD-mentions.md` (or append if the file already exists).
2. Write any held drafts under `workstreams/<slug>/drafts/YYYY-MM-DD-<topic>.md`.
3. **Persist the dashboard brief itself** to `workstreams/<slug>/triage-briefs/YYYY-MM-DD-<slug>.md` so the workspace tab opens with the full chat-side brief, not just the inbox note. The brief file should contain the exact same dashboard table that was rendered to chat (What this is / Real ask / Sources / Routing / Execution plan / Recommendation) plus a `Status` line at the top noting which approvals are pending.
4. Add the routed todo via `python3 ~/.agents/skills/todo/scripts/todo_cli.py add --workstream <slug> --priority <P> --title <T> --source-ref <url>`.
5. **Route the approved work locally.** In CoS or Launchpad, run `bash system/scripts/router-dispatch.sh --origin <origin> "<complete handoff>"`. Elsewhere, use `bash system/scripts/route-to-workspace.sh --name <slug> --no-focus --handoff "<complete handoff>"`. Preserve the task, attempt, and origin from the routing contract. Do not launch or inject text through raw cmux commands.
6. **Include the complete context in that handoff.** The prompt MUST point the workspace agent at:
   - The persisted triage brief path (so it has the full dashboard)
   - The inbox note path
   - Any held draft paths
   - The pending approval commands (so the workspace knows what to do once {{OWNER_FIRST}} approves)
   - A "do not act yet — wait for approval signal" line so the workspace doesn't post the held draft or write to external surfaces on its own.
7. Verify the returned task acknowledgment and local worker identity. Report a blocker if the worker did not acknowledge. Do not call an accepted launch a completed handoff.

External writes (Slack posts, Google Doc edits, email sends, Linear/Jira updates) STILL require a separate approval per `references/approval-policy.md` and happen inside the launched workspace, not in the CoS thread.

The point of this step is: when {{OWNER_FIRST}} approves a routing decision, the workspace should be open, populated with full context, and ready to act on the next approval — without them having to ask "open the workspace" or "pull the discussion in" as separate manual steps.

### 8. Stop before external writes

Follow `references/approval-policy.md`.

Never post to Slack, edit a Google Doc, send an email, or write to Linear, Jira, or a roadmap tracker until the user approves with one of the generated commands. The local file writes and workspace launch in step 7 are NOT external writes and should happen automatically once the routing decision is approved.

## Output format

Always render the brief as a **single unified dashboard table** with emoji-tagged sections in the left column and content in the right column. This is the canonical format — do not fall back to long-form headers, bullet-only outputs, or multiple separate tables for the core sections.

Required sections, in this order:

| 🧩 Section | 📋 Detail |
|---|---|
| 🔍 **What this is** | Primary artifact summary: source, sender, audience, timestamp, one-paragraph description of the content. |
| 🎯 **Real ask** | The implied deliverable for {{OWNER_FIRST}} (not the literal request). Call out explicitly when it's not directed at {{OWNER_FIRST}}. |
| 📚 **Sources checked** | Bulleted list inside the cell. The primary artifact + any expanded corroboration. |
| 🗂️ **Routing** | Helper's decision (match/create/park/skip), the workstream slug, where drafts/inbox notes will land, and any override recommendation when the helper picked a weaker match. |
| 🛠️ **Execution plan** | (Optional — include only when there's planned work beyond filing.) Exact planned edits, draft text, or follow-up steps. Use bullets inside the cell. |
| ⭐ **Recommendation** | The single recommended approval command, e.g. `approve match <slug>`. |

After the dashboard table, render an **approval options block** as a numbered blockquote (max 4 options) with the recommended one starred (⭐). Format:

```
> 1. ⭐ **`approve match <slug>`** — short why
> 2. `approve route-create` — short why
> 3. `approve park` — short why
> 4. `skip` — short why
```

**Bulletize every cell that has more than 2-3 sentences of content.** Per AGENTS.md Output Preferences "no paragraph walls" rule, dashboard cells (especially 🔍 What this is, 🛠️ Execution plan, 📚 Sources checked) MUST use bullets and short bold sub-labels — never a wall of prose. A cell with one short sentence is fine; a cell with a paragraph is not. Use `<br>` line breaks inside cells when bullets need separation. Include exact planned edits or draft text inline when validation/drafting is the main value — that content goes in the 🛠️ Execution plan row, bulletized.

For validation work, the 🛠️ Execution plan row should cover:

- evidence checked
- affected sections
- exact planned edits
- draft Slack reply only when the source artifact is a Slack thread **and** the reply is the requested deliverable, the artifact is already prepared, or a blocking input must be requested

## Stop conditions

- Stop and ask if the write target is ambiguous and a wrong write would be costly.
- Stop and ask if auth or tool access blocks the primary artifact.
- Stop and ask if the user pasted multiple unrelated links and no dominant task emerges.

## References

- `references/source-expansion.md`
- `references/tool-routing.md`
- `references/approval-policy.md`
