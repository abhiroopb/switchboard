# Launch checklist: {Feature or product name}

**Launch owner:** {name} | **Target date:** {date} | **Launch type:** [Internal / Beta / General availability]
**Status:** [Planning / Ready / Launched / Rolled back]

## Summary
[What is launching, for whom, and why now. 2-3 sentences.]

**Go or no-go call:** {date, time, and who decides}

## 1. Readiness
- [ ] **Scope frozen.** The launch scope is written down, and anything cut is listed with a follow-up date.
- [ ] **Quality bar met.** Known bugs are triaged, and no open bug blocks the core flow.
- [ ] **Edge cases checked.** Empty, error, loading, and permission states work as designed.
- [ ] **Accessibility checked.** The core flow works by keyboard and screen reader, and contrast passes.
- [ ] **Security and privacy reviewed.** Data handling, access, and retention were reviewed and signed off.
- [ ] **Legal reviewed.** Terms, privacy policy, consent, and public claims were reviewed where they apply.
- [ ] **Capacity ready.** Systems can handle expected load, and alerts are in place for the new paths.
- [ ] **Dependencies confirmed.** Every team and service the launch relies on has confirmed it is ready.

## 2. Comms
- [ ] **Internal heads-up.** Teams affected by the launch know what is changing and when.
- [ ] **Customer message.** The announcement, in-product copy, and email are written and approved.
- [ ] **Docs updated.** Help articles, release notes, and any API docs are ready to publish.
- [ ] **Sales and partner brief.** Customer-facing teams have a short brief with talking points and known limits.
- [ ] **Claims checked.** Every public claim is accurate and has evidence on file.

## 3. Support
- [ ] **Support trained.** The support team has seen a walkthrough and knows the known issues.
- [ ] **Answers ready.** Common questions have written answers and macros.
- [ ] **Escalation path.** Support knows who to page for a launch issue, during and after hours.
- [ ] **Feedback channel.** There is one place where customer feedback about the launch is collected.

## 4. Rollout and rollback
- [ ] **Rollout plan.** The stages, the audience for each stage, and the timing are written down.
- [ ] **Feature flag.** The launch sits behind a flag or setting that can turn it off without a deploy.
- [ ] **Stage gates.** Each stage has a named signal that must look healthy before the next stage starts.
- [ ] **Rollback plan.** The steps to roll back are written, tested, and owned by a named person.
- [ ] **Data safety.** Any data migration has a backup and a tested way back.
- [ ] **Launch-day coverage.** Named people are watching dashboards and support queues during rollout.

| Stage | Audience | Start date | Gate to move on | Owner |
|-------|----------|-----------|-----------------|-------|
| | | | | |

## 5. Measurement
- [ ] **Success metric.** One primary metric and its target are agreed before launch.
- [ ] **Guardrail metrics.** The metrics that must not get worse are named, with the level that triggers action.
- [ ] **Tracking verified.** Events fire correctly in production, and the dashboard shows real data.
- [ ] **Baseline captured.** Pre-launch numbers are recorded so the team can compare after launch.
- [ ] **Read-out dates.** The dates for the first and second read-outs are on the calendar.

| Metric | Type | Baseline | Target | Where to see it |
|--------|------|----------|--------|-----------------|
| | Primary | | | |
| | Guardrail | | | |

## 6. Post-launch review
- [ ] **Review held.** The team met within a few weeks of launch to compare results with the plan.
- [ ] **Results shared.** A short summary of results and lessons went to stakeholders.
- [ ] **Follow-ups filed.** Bugs, cut scope, and new ideas are filed with owners and dates.
- [ ] **Flag cleanup.** Launch flags and temporary code have a removal date.
- [ ] **Decision recorded.** The team recorded whether to expand, iterate, or roll back.

### What went well
- 

### What to change next time
- 

### Decision
[Expand / Iterate / Roll back, and why.]
