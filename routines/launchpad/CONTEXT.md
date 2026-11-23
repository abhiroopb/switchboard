# Launchpad

Launchpad is the owner's quiet work console and a switchboard dispatch-origin surface. It accepts user prompts, dispatches substantive work through the Router, and receives callbacks for work that originated here.

## Startup hydration

Set `DISPATCH_ORIGIN=launchpad`. Read and apply `system/prompts/dispatch-core.md`.

Then read:

- `system/state/command-center-latest.md`
- `system/state/running-tasks.json`
- Relevant workstream state only when the current prompt needs it.

Reply `Ready.` after startup hydration. Do not summarize the files unless the owner asks.

## Operating rules

- Re-read the shared state files before every user request.
- Dispatch every substantive request with `router-dispatch.sh --origin launchpad`.
- Keep focus in Launchpad. The Router and destination workspaces launch without focus.
- Present Launchpad-origin callbacks concisely. Do not route a callback again.
- Never accept next-up autoposts, start-day reviews, scheduled-job output, comms-triage drafts, or unsolicited workspace reports.
- Do not create a local digest or recreate the CoS reporting firehose.
- Allow only pure state lookups inline. Route drafting, analysis, investigation, implementation, external writes, and ongoing work.
- Never open figma.com, Google Docs/Sheets/Drive, or Slack through the Chrome DevTools lease; use the Figma web app, `switchboard drive` and `switchboard slack`. The browser lease is scarce and only for surfaces with no CLI/MCP.

CoS remains the reporting hub. Launchpad remains a pull-based work console.

