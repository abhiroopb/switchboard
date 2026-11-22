# PM OS behavior map

Canonical control-plane map for PM OS. Use this file to decide where behavior belongs before editing any prompt, skill, or script.

## Ownership model

| Behavior type | Canonical owner | Examples | Notes |
|---|---|---|---|
| Shared cross-runtime policy | `AGENTS.md` | boundaries, operating rules, output philosophy | Source of truth for PM OS behavior |
| Claude repo adapter | `CLAUDE.md` in repo root | imports shared policy, tiny Claude-specific notes | Should stay thin |
| Claude home adapter and runtime | `~/.claude/CLAUDE.md`, `~/.claude/settings*.json`, hooks | personal startup behavior, Claude-only hooks, tool permissions | Not the source of shared repo behavior |
| Deterministic automation | scripts, daemons, scheduled jobs, state rebuilders | queue rebuilds, calendar sync, recurrence materialization | Never prompt-only |
| Workflow guidance | `SKILL.md`, runbooks | drafting-docs, chief-of-staff, todo workflow | Task-scoped and on-demand |
| Learned preferences | `knowledge/`, `amp-mem` (Amp) or claude-mem (Claude), writing styles | tone, comms preferences, recurring PM habits | Durable but not always global |
| Review queue for new rules | structured state in `system/state/sources/` | proposed rules from distillation or migration | Requires approval before codification |

## Key decisions

### Policy source of truth
- Shared PM OS behavior belongs in repo-owned files.
- Local home-directory surfaces are adapters and deployment targets.
- If a rule should affect both Amp and Claude in this repo, change the repo-owned surface first.

### Calendar sync
- Canonical owner: todo system.
- Canonical implementation: deterministic code path, not prompt text.
- Canonical rule:
  - schedule `P0` and `P1` only
  - use `estimate_min` or 15 minutes
  - place at 1:00 PM local time
  - stagger by 15 minutes
  - shift weekends to prior Friday
  - use private visibility
  - use direct calendar API path first, `gcal` only as fallback

### Recall-first behavior
- Recall is a bounded preflight.
- Current execution truth comes from live state and workstream context first.
- Historical context comes from memory and imported durable docs second.
- Recall must remain read-only.

### Recurring work
- Machine routines live in scheduled jobs.
- Human recurring commitments live in the todo system.
- Recurring templates should not appear as normal active todos.

### Rule promotion
- Repeated corrections become reviewable proposals.
- Proposals route into the correct owner: `AGENTS.md`, a skill, a script, or a preference doc.
- No direct append-to-prompt behavior.

## Drift

Home adapters can lag the repo. `python3 system/scripts/check-instruction-drift.py` reports where.

## Editing rule

Before changing an instruction surface:

1. Classify the behavior type.
2. Edit the canonical owner.
3. Update adapters only if needed.
4. Run `python3 system/scripts/check-instruction-drift.py`.
