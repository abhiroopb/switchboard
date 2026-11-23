# Slack triage and unread coverage

Use the `slack` skill and `switchboard slack` for bounded message searches and complete thread reads.
Use the existing priority-channel and comms-triage configuration to choose sources.

- Recent activity is not proof that a message is unread.
- Report Slack unread counts and mark-as-read support as **unavailable** when the connector does not expose them.
- Do not run the legacy raw-token helper or read credential files.
- Read the full thread and check for later resolution before proposing an action.
- Preview replies and obtain approval before posting through `switchboard slack`.
- Use the approved triage sender for `send N` replies. Other bot posts require the standard disclosure.

The legacy Python helper fails before reading credentials or making a request.
