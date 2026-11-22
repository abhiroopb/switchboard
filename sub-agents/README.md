# Sub-Agents

Reviewer personas used to pressure-test documents and decisions from more than one angle.

## Current reviewers

Core:
- **`engineer-reviewer.md`.** Reviews PRDs, specs, and technical proposals as the engineering lead who will own the build. It looks for hidden work, dependencies, data and migration risk, failure modes, and the first slice to prototype.
- **`code-reviewer.md`.** Gives a staff-level review of actual tools, scripts, CLIs, and automations as written.
- **`executive-reviewer.md`.** Reads a proposal as the leader who funds it. It checks the ask, strategic fit, size of the prize, cost, and risk, and shows how to cut the case to one page.
- **`designer-reviewer.md`.** Reviews specs and features from a product design view. It checks user flows, empty, loading, and error states, consistency, hierarchy, and accessibility basics.
- **`customer-voice.md`.** Speaks as the target customer in the first person. It says whether the problem is real, what is confusing, what would make them switch or leave, and what they would pay for.
- **`legal-advisor.md`.** Flags legal, privacy, and regulatory questions early and says when to bring in a lawyer. It never gives legal advice.
- **`skeptic.md`.** Plays devil's advocate. It names the riskiest assumption, the strongest counterargument, the evidence that would change the plan, and the cheapest test.
- **`uxr-analyst.md`.** Separates what the team knows about users from what it assumes. It checks evidence quality and sample bias, and matches each research gap to a method.

Strategy and GTM:
- **`data-analyst.md`.** Pressure-tests the numbers: metric validity, impact sizing, and statistical honesty.
- **`competitive-analyst.md`.** Tests whether a product bet holds a durable advantage against current and likely competitors.
- **`gtm-launch-strategist.md`.** Checks launch readiness, positioning, enablement, rollout, and success metrics.
- **`pricing-monetization.md`.** Reviews packaging, willingness to pay, and the line between free and paid.
- **`ai-product-specialist.md`.** Reviews AI features for problem fit, evals, failure modes, AI UX, and guardrails.

Quality and risk:
- **`accessibility-auditor.md`.** Audits designs and prototypes against WCAG 2.2 AA.
- **`security-privacy-reviewer.md`.** Reviews the technical attack surface and data handling. It is separate from `legal-advisor.md`.
- **`reality-checker.md`.** Checks feasibility and scope, and whether a prototype proves the hard part. It is sharper and more build-focused than `skeptic.md`.

Coding:
- **`test-strategist.md`.** Decides what to test, which edge cases matter, where coverage is thin, and where tests will flake.
- **`debugger.md`.** Finds the root cause across files with ranked hypotheses.
- **`refactor-architect.md`.** Judges coupling, abstraction fit, and whether a refactor is worth it. It goes deeper than `code-reviewer.md`.
- **`performance-reviewer.md`.** Finds hot paths, N+1 queries, scale cliffs, and bundle size problems.
- **`api-contract-reviewer.md`.** Reviews endpoint, schema, and type design, and catches breaking changes.

Design and prototyping:
- **`product-designer.md`.** Reviews visual and interaction craft, hierarchy, states, and polish. It goes deeper than `designer-reviewer.md`.
- **`interaction-prototyper.md`.** Finds the fastest path to a clickable prototype and what to fake or build for real. It pairs with `reality-checker.md`.
- **`design-system-reviewer.md`.** Checks consistency with tokens and components, and flags drift and missed reuse.
- **`content-ux-writer.md`.** Reviews in-product microcopy such as labels, errors, and empty states.

Analysis and org:
- **`experimentation-designer.md`.** Reviews A/B test design: hypothesis, guardrails, power, and the decision rule.
- **`data-storyteller.md`.** Turns analysis into a short narrative that drives a decision. It is separate from `data-analyst.md`.
- **`stakeholder-strategist.md`.** Plans cross-functional alignment: who decides, likely objections, framing, and sequencing.
- **`incident-reviewer.md`.** Runs a blameless postmortem with a timeline, contributing factors, and action items.

Use these after a first draft exists. Keep their guidance opinionated and specific to each role.

**Docs and code.** Use `engineer-reviewer.md` for the idea, such as a PRD, proposal, or spec. Use `code-reviewer.md` for the implementation, such as a shipped or about-to-ship script or tool.
