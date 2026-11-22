# Source expansion

Use progressive fan-out.

## Primary artifact

Always read the dropped link first.

## Direct expansion

Expand the top linked artifacts when they are likely to be part of the task:

- linked docs
- linked tickets
- linked roadmap records
- linked pages or tools under review

Default direct expansion budget: 2 linked artifacts.

## Corroboration triggers

Search beyond the direct links when one of these is true:

- task mode is `validate`
- task mode is `status`
- task mode is `decision-support`
- the thread or doc references a known initiative with recent churn
- rollout state, ownership, or the latest decision could change the answer
- the current artifact looks stale or contradictory

## Typical corroboration set by source

### Slack permalink
- Slack thread context
- linked docs or pages
- active workstream context
- related project decisions
- linked Linear or Jira records when relevant
- an internal search tool only when broad internal context is needed

### Google Doc
- doc body and comments
- linked docs
- owning workstream or project context
- recent Slack references to the doc

### Linear or Jira
- issue or project details
- comments
- linked Slack threads or docs
- relevant workstream context

### Roadmap tracker record (optional adapter)
- connected resources
- related docs
- linked Linear or Jira context
- active workstream or project notes

### Generic web page or app page
- source-of-truth docs
- linked Slack discussion
- active workstream context
- recent decision notes

## Stop rule

Stop expanding once you have enough evidence to produce a confident execution-ready brief. Do not keep searching for completeness when the next action is already clear.
