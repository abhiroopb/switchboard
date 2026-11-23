# Tool routing

Choose the best available execution surface for the actual follow-through.

## Preferred surfaces

| Need | Preferred path | Fallback |
|---|---|---|
| Read or reply in Slack | `switchboard slack`, or a `slack` skill if installed | summarize + draft reply only |
| Read or edit Google Docs/Drive | `switchboard drive`, or a `gdrive` skill if installed | drafted edits with exact locations |
| Read or update Linear | the Linear MCP, or a `linear` skill if installed | prepared mutation plan |
| Read or update Notion | Notion skill if available | drafted page updates |
| Validate or edit a web UI or internal page | API-first paths (`switchboard`, the service API) | Chrome DevTools with a dedicated automation profile; use the daily Chrome only when its logged-in session is required; use manual steps only if both paths fail |
| Read a generic page | `read_web_page` | the service API for authenticated internal pages |
| Resolve intranet short links (optional) | your organization's short link resolver | manual resolution note |
| Read or update a roadmap tracker (optional) | your tracker's API or skill | prepared update plan |

## Capability selection

The intake planner will classify candidate surfaces based on available local skills, global skills, archived skills, and relevant executables.

Use the highest-confidence active surface first.

## Chrome focus-safety contract

- Use only `~/.cache/chrome-devtools-mcp/chrome-profile`. Run normal MCP launches headlessly and reuse that profile.
- Open every automation page with `background: true`. Never set `bringToFront: true` in `select_page`.
- Close every automation tab when the task finishes.
- Call `mcp__chrome_devtools__*` tools directly, one call at a time; never through `code_exec`. Plugin hooks do not see calls made inside `code_exec`, so heartbeats and lease releases stop, and the call can hang. The supervisor fails any call unanswered for `SWITCHBOARD_CHROME_CALL_TIMEOUT` (600 s); treat that error as "outcome unknown" and inspect page state before retrying. Detail: `docs/runbooks/chrome-mcp-leases.md`, "October 26 hung calls and code_exec".
- Never use plain Chrome, {{OWNER_FIRST}}'s daily Chrome, AppleScript, `chrome://inspect`, Chrome settings, `autoConnect`, or a fresh profile. A machine rule in `local/agents-local.md` wins over this on the machine it names.
- If Google authentication expires, report the blocker to CoS. Use `SWITCHBOARD_BROWSER_OK=1` only for a CoS-coordinated visible MCP sign-in. Then verify one Google Doc through two complete headless launch/stop cycles without a sign-in redirect.

Forms, Sheets, and dialog interfaces support direct DOM automation. Google Docs canvas work often requires menu and keyboard commands. Re-verify each UI change, preferably through an API read-back.

## Execution preference order

1. `execute-in-surface` when approval is present and the surface is reliable
2. `prepare-in-surface` when the target is reachable but approval is still pending
3. `tool-specific-write-plan` when the tool exists but the final write is deferred
4. `paste-ready-draft` when no direct write path is available
