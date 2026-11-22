# Discovery gate

`DISCOVERY.md` is the one document that must exist before any build. `prototype.py gate` reads it; `version`, `fork`, and the share step refuse until `gate --approve` has recorded a pass.

## What the gate checks

- Every required section exists: Problem, Who it is for, Goals, Non-goals, What we want to learn, Success metrics, Acceptance criteria.
- No section is empty, and no `<placeholder>` text remains in any required section.
- At least one `- [ ]` acceptance criterion is present with no placeholder in it.
- `--approve` adds the approver and timestamp to `prototype.json` and `DISCOVERY.md`. Approval is a human step: the owner reads the document and agrees it is the right question.

`--skip-gate` on `version` and `fork` exists for recovery only. Record why you used it in `CHANGELOG.md`.

## How to fill each section

**Problem.** What the user cannot do today, in one or two sentences, with evidence: a quote, a ticket, a metric, a support theme. "Users want a dashboard" is a solution, not a problem.

**Who it is for.** One primary user and the situation they are in (device, time pressure, what they were doing a minute earlier). One secondary audience at most. Name them by role, never by a real person's name.

**Goals.** Two to four outcomes the prototype should make believable. Each is observable in a demo or a test session: "a lead reads coverage in ten seconds", not "the page is clear".

**Non-goals.** The tempting adjacent features you are leaving out. This section protects the time box; write it as a list and point reviewers at it when they ask for more.

**What we want to learn.** Hypotheses with the signal that confirms or refutes each. Each hypothesis maps to an event in the session log or a question in the demo script. If nothing could refute it, it is not a hypothesis.

**Success metrics.** One primary metric the prototype is designed to move and one guardrail metric it must not hurt, each with a baseline (or "unknown") and a target. Interview exercises and launch reviews ask for exactly this pair; a prototype with no metric cannot be judged in the share step.

**Acceptance criteria.** One checkbox per observable behavior. Include at least one criterion per viewport (1440 and 390), one for the keyboard path, and one for each goal. These rows become `reviews/acceptance-matrix.md`.

**Data and fixtures.** Where the sample data comes from, what is invented, and the exact label the UI shows ("Sample data", "Illustrative"). Real customer data never enters a prototype.

**Constraints.** Design system or component library, brand rules, accessibility target (AA by default), privacy rules, time box, and any technical boundary (plain HTML by default; a framework only when the research step shows the pattern needs it).

**Success in the share step.** Who sees the prototype, how (live demo, async link, moderated session), and what feedback counts as an answer.

## Writing it fast

Thirty minutes is enough for a first pass. Write it with the requester in the room or on a call. Use their words in Problem and Goals. Read it back and ask "if this prototype works perfectly, what do we do next?" The answer belongs in `DECISION.md` as the expected outcome.
