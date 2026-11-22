# Approval policy

This skill defaults to `prepare` mode.

## Automatic actions

- read the primary artifact
- read the top linked artifacts
- search corroborating sources when needed
- draft replies
- draft edits
- prepare execution steps
- choose the best execution surface

## Approval-gated actions

- posting to Slack
- editing docs or web UIs
- creating or updating tickets or roadmap records
- drafting or sending email in a persistent external system when the user wants a tighter boundary

## Approval command format

Plans should emit action IDs and approval commands:

```text
approve A1
approve A2
approve all
```

Example action IDs:

- `A1`: edit the primary target
- `A2`: post the Slack reply
- `A3`: create the follow-up issue or record

## Reporting after approval

After an approved execution, report:

- what was executed
- what changed
- any remaining manual or approval-gated steps
