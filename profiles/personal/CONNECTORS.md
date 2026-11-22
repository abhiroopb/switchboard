# Connectors

PM OS calls `pmos <tool> <verb>` for Gmail, Google Drive, Google Calendar and Slack. The kit puts its own shim on PATH (`system/connectors/pmos`, `pmos.cmd` on Windows), which serves the same verbs from the public Google and Slack APIs with your own credentials. Scripts and skills do not change. Full verb list: `system/connectors/README.md`.

## Google (Gmail, Drive, Calendar)

1. **OAuth client.** In the Google Cloud Console create (or reuse) a project, enable the Gmail, Drive and Calendar APIs, and create an OAuth client of type **Desktop app**. If you already have a Desktop client from another tool, it works too.
2. **Publishing status.** Google Auth Platform → Audience: **In production** keeps the refresh token from expiring after 7 days. Verification is not needed for your own use; the consent screen shows "Google hasn't verified this app", choose Advanced → continue.
3. **Sign in.** `pmos login google --client-file <path to the client JSON>` (add `--scopes readonly` for read-only). The browser opens the consent page; the shim stores `google-token.json` in `~/.pm-os/connectors/` (outside any synced folder, such as Dropbox, OneDrive or iCloud Drive, and outside the repo).
4. **Check.** `pmos doctor` prints `ok` for mail, drive and calendar.
5. **Keep the secret out of synced folders.** The client secret travels inside the token file. Do not leave client JSON or token files in a synced folder or a git repo; if one leaked, add a new secret in the console, install it, then disable and delete the old one.

## Slack (optional)

Create a Slack app in your workspace with the user token scopes listed in `system/connectors/README.md`, install it, copy the `xoxp-` token, and run `pmos login slack` (paste at the hidden prompt). PM OS works without Slack; the Slack collectors then stay idle.

## Dropbox (optional)

A skill that files documents can use this to get a shared link. Create a small Dropbox app, then run `pmos login dropbox --app-key <KEY>`. The exact clicks are under "Setup Dropbox" in `system/connectors/README.md`. PM OS works without it; the link cell then stays empty.

## Verbs the shim does not implement

Any verb the shim does not implement, such as `pmos linear|jira|airtable|figma|notion`, exits 2 with a one-line "not available" message. Use the GitHub CLI for GitHub, and the web apps (or the vendor's own API token) for the rest.

## Browser automation

The Chrome DevTools MCP uses its own headless profile (`~/.cache/chrome-devtools-mcp/chrome-profile`). If another browser already holds port 9222 with remote debugging on, turn that off or the MCP attaches to the wrong browser.
