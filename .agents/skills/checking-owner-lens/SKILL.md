---
name: checking-owner-lens
description: "Reviews substantive work against the owner's user-maintained judgment profile as a final QA pass."
---
# Checking Owner Lens
## Use For
Use as final QA for substantive drafts, plans, recommendations, status updates, and artifacts. Skip the full pass for trivial lookups.
## Pull Context First
Read `local/user-lens.yaml` in the switchboard root, then the owning workflow, workstream/project context, stakeholder profiles, and source of truth. If the profile is absent, do not invent preferences: apply the `AGENTS.md` writing rules and the neutral defaults below. `templates/user-lens.yaml.example` is the blank profile.
## Judgment Profile
Apply only the configured `decision_principles`, `tradeoff_preferences`, `communication_tone`, `grounding_standard`, `verification_standard`, and `hard_stops`. Neutral defaults are evidence before assertion, explicit tradeoffs, concise communication, and honest verification.
## Review Rubric
1. Correct workflow, artifact, and target surface.
2. Relevant context and authoritative sources used.
3. Clear recommendation and meaningful tradeoffs.
4. Factual claims and quotations are traceable.
5. Concrete owners, actions, risks, and timing.
6. Verification meets the configured standard.
7. Voice and format match the configured tone.
## Hard Stops
Do not finalize when a configured hard stop applies, or for invented facts, undisclosed uncertainty, an unauthorized destructive/external action, the wrong artifact, or verification not actually performed.
## Fix Patterns
Switch workflows; remove repetition and meta-language; replace generic claims with grounded facts; clarify tradeoffs and next steps; add verification or disclose gaps; tighten tone.
## Output
Run silently and present the improved result. Expose the rubric only when asked.
