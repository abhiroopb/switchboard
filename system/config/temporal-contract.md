# Temporal Contract

Shared rules for every PM OS consumer that summarizes or surfaces facts:
start-of-day / chief-of-staff, meeting pre-briefs and updates, comms triage,
workstream signals, queue ranking, command-center output, end-of-day summaries,
and todo digests. Inline or reference this contract; do not restate weaker
versions of it.

Regression fixture: a meeting pre-brief once presented a teammate's team
change from three months earlier as a current "Active signal", because a note
written that week mentioned it and the prompt did not distinguish when a fact
was written from when the event happened. Every rule below exists to prevent
that class of error.

## Rules

1. **Event time ≠ written time.** A document written or updated recently can
   describe an old event. Judge recency by when the EVENT happened or will
   happen, never by when the text mentioning it was written or last edited.
2. **Definition of current.** A fact is current only if its event time falls
   inside its source-class freshness window (see
   `system/config/knowledge-freshness.yaml`; default guide: Slack/meetings 3
   days, mail/docs/issues 7 days). Facts older than `hard_historical_days`
   (14 days) are ALWAYS historical, regardless of source.
3. **Never present old status as new.** Role changes, leaves, launches,
   handoffs, cancellations, or plan changes that began before the freshness
   window must not appear in "updates", "active signals", "what changed", or
   any current-action section — even if a recent doc, profile, or meeting note
   mentions them.
4. **Standing states are background, dated.** An ongoing state (e.g. "on
   the platform team since 2026-01-15") may appear once as
   labeled context ("People context", "Background") when it materially
   explains the present — with its start date — never as news.
5. **Newest fact wins.** When two sources disagree about the same thing
   (meeting moved, plan changed, owner changed), the record with the newest
   source time is authoritative; older ones are superseded. If the knowledge
   layer marks a record `superseded`, do not surface it as current.
6. **Resolved things stay resolved.** Completed meetings, done todos,
   cancelled events, closed issues appear only in retrospective sections
   (end-of-day, weekly review), never in forward-looking ones.
7. **Prefer the deterministic layer.** When a "Current signals" /
   knowledge-layer section is provided in your context, treat it as the
   authoritative set of current facts. Raw profiles, CONTEXT.md files, and
   meeting-note archives are background whose dated facts must be re-judged
   under rules 1–4 before surfacing.
8. **Label historical facts.** When a historical fact is worth including,
   prefix it with its event date and frame it as background ("Since 2026-01-15
   …", "Back in January …"), so the reader cannot mistake it for news.
9. **Comms are first-class knowledge but message time ≠ event time.** Slack
   messages, Gmail, and Google Doc comments (raw collectors AND the
   normalized comms-triage queue) flow into the same canonical layer and can
   change today's decisions and priorities. A message's received time is its
   SOURCE time only: a message about an old event does not make the event
   current, and a stale or contradictory message never overwrites verified
   newer state — supersession is newest-source-time-first, with direct
   collector evidence beating derived triage records on ties. The same
   message seen by both a raw collector and comms triage is ONE record. A
   triage disposition (routed/todo/done/dropped) changes what needs doing,
   never whether the fact is current.
10. **PM OS outputs close the loop, once.** Durable artifacts the owner creates in
   PM OS (daily notes, decisions, PRDs, status updates, meeting notes,
   research, dated rollups, workstream state) are re-ingested as
   `source_class: pm_os_output` records (allowlist in
   `system/scripts/collect-pm-os-outputs.py`) so later sessions build on
   them. Each artifact is ONE canonical record (latest capture wins by
   content hash); generated/derived surfaces (`system/state/knowledge/**`,
   `.signals/**`, `latest.md` aliases, KB inbox files) are never re-ingested
   — that would launder synthesis back in as evidence. An artifact's event
   time is the artifact's own date, not the capture time. Only allowlisted
   durable kinds (decision, prd, status_update, meeting_notes, research)
   may become product KB candidates.
11. **Curated syntheses are not event evidence.** Curated knowledge-base records
   (`source_class: product_kb`) date from each SOURCE's own modified time, never
   from when the index was rebuilt or a summary was edited. A KB source modified
   months ago stays historical even after a fresh rebuild, and direct evidence
   (Slack, calendar, Drive) with a newer source time outranks an older KB
   snapshot of the same document. KB records are durable: retained beyond the
   normal 30-day window as background, but subject to the same
   current/historical rules as everything else.

## Deterministic source

Query current facts instead of re-deriving them:

    python3 {{PM_OS_ROOT}}/system/scripts/query-knowledge.py \
        [--people slug,slug] [--workstream slug] [--days N] [--include-historical]

The store is rebuilt by `system/scripts/build-daily-knowledge.py` (background
task, every 30 min). Coverage manifests live at
`system/state/knowledge/coverage/YYYY-MM-DD.json`; if a source class is listed
in `missing_source_classes` or marked `stale`, say so instead of implying full
coverage.
