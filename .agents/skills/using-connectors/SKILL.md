---
name: using-connectors
description: "Reaches Gmail, Google Calendar, Google Drive, Slack, and optional Dropbox shared links through the kit's own `pmos` CLI (the pmos-shim connectors). Use when a `pmos` call fails or exits 2 with a 'not available' message, when setting up or checking connector auth, or when a skill says to use pmos mail, drive, calendar, or slack."
---

# Using Connectors

The kit puts `system/connectors` on `PATH`, so `pmos <tool> <verb>` runs `pmos-shim.py`, which serves a documented subset of Gmail, Calendar, Drive, and Slack from the public Google and Slack REST APIs with the owner's own OAuth client and tokens.

Scripts and skills call `pmos`. The shim is the only implementation the kit ships.

## Check that pmos answers

Run `pmos --version`.

- **`pmos-shim <version> (connector CLI)`:** the shim. Follow this skill.
- **Any other version string:** another program named `pmos` comes first on `PATH`. Put `system/connectors` ahead of it, then check again.
- **Command not found:** the kit is not installed, or `system/connectors` is not on `PATH`. On Windows open a new Git Bash window (the `pm-os-env.sh` hook adds it) or rerun `install.ps1`; on macOS or Linux add `export PATH="$PM_OS_ROOT/system/connectors:$PATH"` to the shell profile.

## First-time setup (owner does this once per machine)

The shim never asks for a token on the command line and never prints one.

1. **Google.** Create a Google Cloud project, enable the Gmail, Drive, and Calendar APIs, create an OAuth client of type Desktop app, and download its JSON to `~/.pm-os/connectors/google-oauth-client.json` (`%USERPROFILE%\.pm-os\connectors\` on Windows). Then run `pmos login google`. A browser consent page opens and the shim stores `google-token.json` with mode 0600. Pass `--scopes readonly` for a read-only token.
2. **Slack.** Create a Slack app in the target workspace, add the user token scopes listed in `system/connectors/README.md`, install it, copy the `xoxp-` token, and run `pmos login slack`. Paste the token at the hidden prompt or set `SLACK_USER_TOKEN` for that one command.
3. **Dropbox (optional).** No skill needs this. Links in the owner's trackers are signed-in links that a skill builds from the file's path with no API call (`https://www.dropbox.com/home/<folder>?preview=<file>`), so skip this step unless the owner asks for shared links. Create a small Dropbox app in a normal browser with the clicks under "Setup Dropbox" in `system/connectors/README.md`, then run `pmos login dropbox --app-key <KEY>`. The app key is an identifier, not a secret.
4. **Check.** `pmos status` reads the token files. `pmos doctor` makes one live read per configured tool and prints `ok` or `FAIL` per tool. Exit 0 means every configured tool answers, and a `FAIL` on the optional Dropbox connector doesn't change that.

A Google Workspace admin may block third-party OAuth clients, and a Slack admin may block app installs. Report that to the owner as a blocker with the exact `doctor` line; do not work around it.

## Supported verbs

The exact flag and output tables are in `system/connectors/README.md`. Covered verbs:

| Tool | Verbs |
|------|-------|
| `mail` | `search`, `read`, `draft` (never sends), `show`, `send` (guarded, see Rules), `modify`, `labels` |
| `calendar` | `list`, `events`, `event`, `find`, `create`, `update`, `delete` |
| `drive` | `query`, `search`, `read`, `info`, `comments`, `comment`, `comment-add`, `comment-reply`, `export`, `put` |
| `slack` | `post`, `edit`, `search`, `history`, `thread`, `people`, `resolve`, `whoami`, `channels`, `react` |
| `dropbox` | `link` (shim only). It makes a link anyone can open, so never use it to fill a tracker |

Every other `pmos` verb (for example `pmos linear|jira|airtable|figma|notion`) exits 2 with one line: `pmos-shim: '<verb>' is not available ...`. Treat that exit the way PM OS treats any missing tool: say which verb is unavailable, use the alternative in the README's "Not available" table (`gh` for GitHub, the web app or a personal API token for Linear, Jira, Airtable, Figma), and never retry in a loop.

## Reading exit codes

| Exit | Meaning | What to do |
|------|---------|------------|
| 0 | Success | Parse the output as the real CLI's |
| 1 | API error (`error: <tool> <verb>: HTTP <code> <message>` or `Slack API error: <error>`) | Report the message. An HTTP 401 or 403 means scopes or an expired consent; run `pmos doctor` and ask the owner to re-run `pmos login <google|slack|dropbox>` |
| 2 | Usage error or unsupported verb | Fix the flags, or use the alternative above |
| 3 | Not configured | Ask the owner to run the `pmos login` command the message names |
| 4 | `mail send` refused, nothing went out | Read the reason. A worker or scheduled job parks the text as a Gmail draft instead |
| 5 | `mail send` outcome unknown | Say so. Never retry by hand: check Sent, and a rerun checks Sent before it can send |

## Rules that stay the same

- **Same PM OS policy applies.** The bot attribution prefix, the "send it" rule for Slack DMs, the draft-shown-verbatim rule, and the `gmail-draft.py` From rule all still apply. The shim is a transport, not a policy change.
- **Gmail search covers Inbox by default.** `mail search` defaults to `--label Inbox`, and much of the owner's mail is not in the Inbox. Zero results prints a warning. Retry with `--label all` before saying a message is absent.
- **Sending email.** `send` is off unless `PM_OS_GMAIL_SEND=on` is in `local/machines/<MACHINE>.env` (`python3 system/scripts/gmail_send_guard.py --set on`; the environment can only turn it off). It runs only in the CoS or Launchpad tab, after the owner says send for that exact text: `show`, print To, Cc, Bcc, Subject, the body, and any links verbatim in one message, wait for their own "send it", then `send --draft-id <id> --expect-hash <send check>`. Workers and scheduled jobs never send. `gmail.modify` covers it, so no new sign-in is needed. `pmos doctor` prints the state.
- **Never put a token in a command, a note, memory, or a report.** Tokens live only in `~/.pm-os/connectors/`. `SLACK_USER_TOKEN` is read once by `pmos login slack` and nowhere else.
- **Debugging.** `PMOS_SHIM_TRACE=1` prints each HTTP method and URL to stderr without query strings or bodies. `--timeout <seconds>` or `PM_OS_CONNECTOR_TIMEOUT` set the HTTP timeout.
- **Tests.** `cd system/connectors && python3 -m unittest test_pmos_shim` runs without network or tokens.
