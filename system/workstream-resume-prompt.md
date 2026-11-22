# Workstream Resume -- Prompt Template
#
# This file documents the structure of a good workstream startup prompt.
# Chief of Staff uses this as a reference when composing the "prompt" field
# in system/today-plan.json. The actual prompt sent to each workstream is
# customized per-workstream based on CONTEXT.md and config.yaml.
#
# The cmux-helpers.sh launch command also uses this as a fallback when
# no specific prompt is provided.
#
# If the user explicitly says "open" or "reopen" a workstream, first launch
# and focus the live cmux workspace via `bash system/cmux-helpers.sh launch
# workstream:<slug>` and `cmux select-workspace --workspace <ref>`. Do not
# treat the request as satisfied by only summarizing the workstream in chat.

# Template (variables filled by Chief of Staff or cmux-helpers.sh):
#
#   Read workstreams/{name}/CONTEXT.md for full context on this workstream.
#   Read workstreams/{name}/config.yaml for metadata and priorities.
#   Before making recommendations, drafting documents, or replying on behalf of
#   the workstream, check recent meeting context relevant to this workspace.
#   Start with the `## Recent Meetings` section in workstreams/{name}/CONTEXT.md
#   and any linked projects/*/meetings/ files. If those notes cite a `Gemini Notes`
#   source, or if {{OWNER_FIRST}} mentions meeting notes, what was said, what changed, or
#   asks for meeting context, open the linked Gemini note automatically before
#   answering unless they explicitly ask to stay with the summarized note only.
#
#   Current context from Chief of Staff:
#   {reason for opening today -- what makes this relevant right now}
#
#   Your tasks:
#   1. Identify where this workstream left off
#   2. List any open questions, pending decisions, or blocking items
#   3. Identify the next 1-3 concrete deliverables
#   4. Proceed with the highest-impact next step
#   5. If the user drops a link into the workstream, treat it as a durable source artifact.
#      For normal artifacts, create or update a dated markdown summary note in the
#      workstream folder (for example `YYYY-MM-DD-topic-summary.md`) with the source
#      URL, key takeaways, open questions, and why it matters to the workstream.
#      For meeting artifacts, save or update the canonical note under
#      `projects/*/meetings/` (or `projects/_inbox/meetings/`) and add only a
#      summary plus link in the workstream CONTEXT.
#
#   {startup_instruction from config.yaml}

# Good prompt example:
#
#   Read workstreams/competitive-intel/CONTEXT.md. You are resuming the
#   competitive intelligence workstream. Phase 1b (pricing page teardown)
#   is complete. Phase 2 (onboarding flows) was finished on 2026-01-15.
#   The next step is to pick the next competitor brief or begin the
#   mobile review. Check the scorecard for competitors above the
#   triage threshold that don't have briefs yet.

# Bad prompt example (too generic):
#
#   Resume work on competitive-intel. Read CONTEXT.md and continue.
