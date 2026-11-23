# System

Core switchboard machinery: startup flows, state, scripts, and operating docs.

## Key entrypoints

- `start-day.sh`: launches the CoS workflow (cmux source key `switchboard`)
- `chief-of-staff-prompt.md`: instructions for the CoS session
- `scheduled-jobs.yaml`: recurring automation definitions
- `state/`: queue, now, source snapshots, and published sidebar state
- `scripts/`: helper scripts for rebuilding state, publishing status, and maintaining workspace metadata

Use this folder for infrastructure and operating logic, not product artifacts.
