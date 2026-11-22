# pmos-shim: the PM OS connector CLI

## What this is

PM OS scripts and skills reach Gmail, Google Calendar, Google Drive, Slack and (optionally) Dropbox through one command, `pmos <tool> <subcommand> [--flag value ...]`. `pmos-shim.py` is that command: a standard-library Python program that serves the subcommands listed below from the Google, Slack and Dropbox REST APIs with the owner's own OAuth client and tokens, and prints output in the shapes the repo parsers read. Any other tool or verb exits 2 with a one-line "not available" message, so a caller falls through the same way it does for any missing verb.

## When it installs

The kit puts `system/connectors` on `PATH` unless an existing CLI with the same name is already on `PATH`; in that case the existing CLI is left in front and the shim stays unused (`company_cli.py` makes that call for every installer). The launchers are `pmos` (bash, macOS, Linux and WSL2) and `pmos.cmd` (native Windows). Both run `pmos-shim.py` with `PM_OS_PYTHON` when set; otherwise `pmos` falls back to `python3` and `pmos.cmd` to `py -3`, then `python`. Python 3.11 or newer is required. No third-party packages.

Files and settings the shim reads:

| Item | Default | Purpose |
|------|---------|---------|
| `PM_OS_CONNECTORS_DIR` | `~/.pm-os/connectors` | Directory for the files below |
| `google-oauth-client.json` | in that directory | OAuth client the owner downloads from Google Cloud Console |
| `google-token.json` | in that directory | Refresh token plus cached access token, written with mode 0600 |
| `slack-token.json` | in that directory | Slack user (`xoxp-`) or bot (`xoxb-`) token |
| `dropbox-token.json` | in that directory | Dropbox refresh token and app key, written with mode 0600. Left out of the nightly backup |
| `PM_OS_DROPBOX_ROOT` | the desktop app's `info.json` | Local Dropbox folder, used to turn a local path into a Dropbox path |
| `SLACK_USER_TOKEN` | unset | Read once by `pmos login slack` instead of the prompt |
| `PM_OS_CONNECTOR_TIMEOUT` | `60` | HTTP timeout in seconds. `--timeout <SECONDS>` overrides it |
| `PM_OS_SHIM_TRACE` | unset | `1` prints each HTTP method and URL to stderr without query strings or bodies |

The shim never prints a token and never logs a request body.

## Setup Google

1. Open the Google Cloud Console at `https://console.cloud.google.com/` and create a project.
2. Enable three APIs under "APIs & Services": Gmail API, Google Drive API and Google Calendar API.
3. Configure the OAuth consent screen. Choose "External" for a personal account. Add the owner's address as a test user while the app stays in testing.
4. Create credentials of type "OAuth client ID" with application type "Desktop app".
5. Download the client JSON and save it as `~/.pm-os/connectors/google-oauth-client.json`.
6. Run `pmos login google`. The shim opens the browser to the Google consent page and prints the URL as well. It listens on `http://127.0.0.1:<random port>/` for the redirect and uses PKCE.
7. Approve the scopes. The shim stores `google-token.json` and refreshes the access token on its own.

Use `pmos login google --scopes readonly` for a read-only token. The default `--scopes full` requests `gmail.modify`, `drive` and `calendar`. Pass `--client-file PATH` when the client JSON lives elsewhere.

A Google Workspace admin may block third-party OAuth clients. Personal Google accounts work without that restriction.

## Setup Slack

1. Open `https://api.slack.com/apps` and create an app from scratch in the target workspace.
2. Under "OAuth & Permissions" add these user token scopes: `search:read`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `channels:read`, `groups:read`, `users:read`, `users:read.email`, `chat:write`, `reactions:write`, `users.profile:read`.
3. Install the app to the workspace and copy the user OAuth token. It starts with `xoxp-`.
4. Run `pmos login slack` and paste the token at the hidden prompt. Set `SLACK_USER_TOKEN` in the environment to skip the prompt. The shim never accepts the token as a command-line argument.
5. The shim calls `auth.test`, stores `slack-token.json` and reports the team and user.

A bot token (`xoxb-`) also works for posting and reading channels the bot joined. Search needs a user token because `search.messages` is user-scoped. A workspace admin may restrict app installs.

## Setup Dropbox

This connector is optional, and no skill needs it. Links that skills write into spreadsheets are signed-in links, `https://www.dropbox.com/home/<folder>?preview=<file>`, which a skill builds from the file's path with no API call. Signed out, they go to the Dropbox login page. `link` makes a link anyone can open, so use it only when the owner asks for one.

Dropbox gives no shared client, so the owner creates one small app. Do this in a normal browser, because a passkey does not work in the wmux browser panel. If a passkey fails there too, the login page also offers Apple, Microsoft and email sign-in.

1. Open `https://www.dropbox.com/developers/apps/create` and sign in as usual.
2. Choose "Scoped access", then "Full Dropbox". An App folder app cannot reach files outside its own folder. Give the app any unique name without the word "Dropbox", then click "Create app".
3. On the "Permissions" tab tick `sharing.read` and `sharing.write`, then save at the bottom or top of the page. Leave any box Dropbox ticks by itself. The shim asks only for these two plus `account_info.read`, which every app has.
4. On the "Settings" tab, under "OAuth 2", paste `http://localhost:7473/` into "Redirect URIs" and click "Add". Leave "Allow public clients" on "Allow". Copy the "App key". It is an identifier that also appears in the sign-in address, so it is not a secret.
5. Run `pmos login dropbox --app-key <KEY>`. The default browser opens the consent page, and clicking "Allow" within 15 minutes finishes it. Add `--no-browser` to get the address on stderr instead.

The token can list, create and remove shared links anywhere in the Dropbox. It has no file read or write permission, but anyone holding it could make a link to any file and open that link, so treat it as sensitive. It lives in `~/.pm-os/connectors/`, outside Dropbox, git and the backup, because that folder is the credential rule for every connector. To cut access, run `pmos logout dropbox` and delete the app in the App Console.

## Supported verbs

Every subcommand accepts `--help`, `--timeout <SECONDS>`, `--json '{...}'` for nested payloads, and ignores `--raw` and `--header`. List flags take several values until the next `--flag`. Boolean flags take an optional `true|false` and have a `--no-<flag>` form. The repo parsers depend on these shapes: the gmail search text (`gmail_search.parse_search_text`, `todo_cli.py`), the read blocks and the Drive comment fields (`reply_check.py`, `gmail_search.read_message`), the Slack post keys (`daemon/slack-send.sh`), and the Slack search keys (`todo_cli.py`). Keep them stable when changing the shim. Slack search uses the `search.messages` endpoint.

### mail

| Subcommand | Flags | Output shape |
|------------|-------|--------------|
| `search` | `--query --body-terms --subject --from-sender --to --label --max-results --newer-than-days --older-than-days --after --before --only-is-read --only-is-unread --include-spam-trash --include-body --max-body-chars --include-next-page-token --page-token --fetch-all --search-mode` | `{"result": "Search summary:\nMode: ...\nReturned count: N\nResult size estimate: M\n[Next page token: X]\n\nSubject: ...\nFrom: ...\nTo: ...\nDate: ...\nID: ...\nThread ID: ...\nLabels: a, b\nSnippet: ...\nNote: ..."}` |
| `read` | `--message-ids --thread-ids --draft-ids --max-body-chars` | Message block ending `Body:\n<text>` with the `[Truncated to N characters ...]` and `(Body omitted because max_body_chars is 0 ...)` marker lines. Thread block with `Message IDs:` newest first and `Bodies:` |
| `draft` | `--body --to-recipient --cc --bcc --subject --draft-type common|reply|forward --reference-message-id --draft-metadata --html` | `{"result": "Draft created...", "draft_metadata": {...}}`. Never sends |
| `show` (shim only) | `--draft-id` | `{"result": "Draft ID: ...\nTo: ...\nSubject: ...\nSend check: ...\nBody:\n...", "send_check": "...", ...}`. Reads only |
| `send` (shim only) | `--draft-id --expect-hash` | `{"result": "Sent.\nTo: ...\nCc: ...\nSubject: ...\nMessage ID: ...", "message_id": "...", ...}`. Guarded, see "Sending email" |
| `modify` | `--message-ids --labels-to-add --labels-to-remove` | `{"result": "Modified N message(s): ..."}` |
| `labels` | `--operation create|list_labels|get --label-id --label-name` | `{"labels": [...]}` or `{"label": {...}}` |

### calendar

| Subcommand | Flags | Output shape |
|------------|-------|--------------|
| `list` | `--output markdown|json` | `{"result": "<markdown table>"}` with columns `id, summary, description, time_zone, access_role, primary, selected, color_id, background_color, foreground_color`, or `{"result": [rows]}` |
| `events` | `--time-period --calendar-id --event-ids --event-status --include-attendees-info --meeting-state --rsvp-status --output --timezone` | Markdown table or `{"result": [{id, calendar_id, summary, description, status, html_link, creator_email, organizer_email, start_time, end_time, start_date, end_date, hangout_link, attachments, meeting_state}]}`. Empty: `"No events foundCalendar ID: ...\nTimezone: ..."` |
| `event` | `--event-id --calendar-id --output --timezone` | Same row shape |
| `find` | `--query --time-period --calendar-id --max-results --output --timezone` | Same row shape |
| `create` | `--title --start-time --end-time --user-calendarID --description --location --color-id --required-attendees --optional-attendees --recurrence --reminders --send-updates --transparency --visibility --guests-can-* --with-meet --dry-run --timezone` | `{"result": "Event created...", "event": {...}}`. `--dry-run` prints the planned request |
| `update` | create flags plus `--event-id --calendar-id --merge-attendees --scope single|all --original-start-time` | `{"result": "Event updated...", "event": {...}}`. `--scope future` is not supported |
| `delete` | `--event-id --calendar-id --scope single|all --send-updates --dry-run` | `{"result": "Event deleted.\nID: ...\nCalendar: ..."}` |

`--time-period` takes a quoted preset such as `'"today"'` (`yesterday`, `today`, `tomorrow`, `this_week`, `next_week`) or a JSON object `{"time_min": "...", "time_max": "..."}`.

### drive

| Subcommand | Flags | Output shape |
|------------|-------|--------------|
| `query` | `--raw-query --corpora user|drive|all_drives --drive-id --page-size --page-token` | `{"corpora", "drive_id", "files": [{id, name, mime_type, created_time, modified_time, owners, parents, size, starred, web_view_link, ...}], "next_page_token", "notes", "page_size", "page_token", "query"}` |
| `search` (deprecated) | `--drive-type file --query --field name|full_text --mime-type --parent --owner --corpora --drive-id --page-size --order-by-field --order-by-direction` | Same shape as `query` |
| `read` | `--id-or-url --output-format json|markdown --include-metadata --mime-type --sheet-name --range` | `{"result": "<text>"}`. Docs export as plain text or markdown, Sheets as CSV, Slides as text, folders as a listing, other files as text when decodable. `--include-metadata` adds `file` and `mime_type` |
| `info` | `--file-id-or-url` | `{"file": {..., "trashed", "urls": {edit, preview, web}, "web_view_link"}}` |
| `comments` | `--file-id-or-url --all-pages --fail-empty --include-quoted-content --max-comments --page-size --page-token` | `{"comments": [...], "next_page_token", "page_size", "page_token"}` |
| `comment` | `--file-id-or-url --comment-id --include-quoted-content` | `{"comment": {author.me, author.displayName, createdTime, deleted, resolved, replies, ...}}` |
| `comment-add` | `--file-id-or-url --content --include-quoted-content` | `{"comment": {...}}` |
| `comment-reply` | `--file-id-or-url --comment-id --content --resolve-comment` | `{"reply": {...}}` |
| `export` | `--file-id-or-url --export-format original|text|markdown|pdf|docx|xlsx|pptx|csv --max-bytes` | Text formats return utf-8 content, binary formats return base64 |
| `put` | `--file-id --name --mime-type --parent-id --body --binary-body-base64 --body-file (shim only) --source-mime-type --replace-existing --strip-markdown-frontmatter --include-metadata --keep-revision-forever --target-id` | `{"action": "created|updated", "id", "name", "mime_type", "web_view_link"}` |

### slack

| Subcommand | Flags | Output shape |
|------------|-------|--------------|
| `post` | `--text --channel --thread-ts --blocks --attachments --user --users --dm-self --reply-broadcast --unfurl-links --unfurl-media --post-at` | `{"channel", "ts", "message": {"ts", "text"}, "permalink", "thread_ts"}`. `daemon/slack-send.sh` reads `.ts // .message.ts` |
| `edit` | `--channel --message-ts --text --blocks --attachments` | `{"channel", "text", "ts"}` |
| `search` | `--query --from-user --with-user --channels --after-date --before-date --after-ts --before-ts --on --newer-than --limit --cursor --sort --sort-dir --timezone` | `{"has_more", "messages": [{blocks_available, channel: {id, name}, is_reply, permalink, text, thread_ts, time, ts, user: {id, is_bot, username}}], "next_cursor", "query", "summary"}` |
| `history` | `--channels --limit --cursor --after-date --before-date --on --newer-than --include-blocks --timezone` | `{"result_count", "results": [{channel: {id, name}, has_more, messages: [{text, time, ts, user, ...}], messages_requested, messages_returned, next_cursor, timezone}]}` |
| `thread` | `--channel --thread-ts --limit --cursor --include-blocks --timezone` | `{"channel": {id, name}, "has_more", "messages": [...], "next_cursor", "thread_ts", "timezone"}` |
| `people` | `--query --include-deactivated --limit` | `{"query", "results": [{display_name, email, id, is_bot, real_name, username}]}` |
| `resolve` | `--name --entity-type user|channel` | User: `{"id", "is_bot", "name", "real_name", "type": "user"}`. Channel: `{"id", "name", "type": "channel", ...}`. Not found: `error: Channel 'x' was not found.` with exit 1 |
| `whoami` | `--user` | `{"profile": {...}, "user": {display_name, email, id, is_bot, real_name, tz, tz_label, tz_offset, username, dnd_*}}` |
| `channels` | `--query --conversation-types --exclude-archived --limit --cursor` | `{"has_more", "next_cursor", "query", "results": [{id, is_private, name, num_members, purpose, topic, type}]}` |
| `react` | `--channel --emoji --message-ts` | `{"channel", "emoji", "message_ts", "was_already_present"}` |

Plain `@user` and `#channel` mentions inside `--text` are not rewritten. Use `<@U...>` and `<#C...>` syntax.

### dropbox

| Subcommand | Flags | Output shape |
|------------|-------|--------------|
| `link` | `--path --wait-seconds --existing-only` | `{"url", "name", "path", "existed", "audience", "access_level", "who_can_open", "require_password", "expires"}`. Reuses a link the file already has and prefers a view link. Otherwise creates one with the account's default settings, which Dropbox documents as public visibility unless a folder policy says otherwise. `--path` is a local path under the Dropbox folder or a `/Dropbox/path`; from Git Bash pass the local path. `--wait-seconds` retries while the file is still syncing. `--existing-only` never creates |

### Shim-only verbs

| Verb | Purpose |
|------|---------|
| `pmos login google [--client-file PATH] [--scopes readonly\|full]` | Run the loopback OAuth flow and store `google-token.json` |
| `pmos login slack` | Store the Slack token from `SLACK_USER_TOKEN` or a hidden prompt |
| `pmos login dropbox --app-key KEY [--no-browser]` | Run the loopback OAuth flow with PKCE on port 7473 and store `dropbox-token.json`. There is no app secret. Later runs may omit `--app-key` while the token file exists |
| `pmos status` | Print `mail: ready (token expires ...)`, `slack: not configured`, and so on. Reads only the token files |
| `pmos doctor [dropbox]` | Make one read call per configured tool (Gmail profile, Drive about, Calendar list, Slack `auth.test`, Dropbox account) and print `ok` or `FAIL` per tool. Exit 0 when every configured tool answers, 1 when any probe fails, 3 when nothing is configured. Dropbox is optional, so a failed Dropbox probe prints `FAIL` without changing the exit code. `doctor dropbox` probes only that tool and exits 1 when it fails, and `check-windows-install.py` uses it |
| `pmos logout <google\|slack\|dropbox>` | Delete the stored token |
| `pmos --version` | Print `pmos-shim <version> (connector CLI)` |

### Errors and exit codes

| Case | Output | Exit |
|------|--------|------|
| API error | `error: <tool> <subcommand>: HTTP <code> <api message>` on stderr | 1 |
| Slack `ok: false` | `error: <tool> <subcommand>: Slack API error: <error>` | 1 |
| Usage error or unknown flag | `error: ...` message on stderr | 2 |
| `mail send` refused, nothing went out | `refused: <reason>. Nothing was sent.` | 4 |
| `mail send` outcome unknown (a timeout, a dropped connection, a 5xx) | `unknown: ...` It is never retried, and a rerun checks Sent first | 5 |
| Unsupported verb | `pmos-shim: '<verb>' is not available in the connector shim (it covers mail, calendar, drive, slack, dropbox). See system/connectors/README.md.` | 2 |
| Missing auth | `error: <tool>: not configured. Run: pmos login google` (or `slack`) | 3 |

## Other services

The shim covers only the tools above. Use each service's own client for anything else:

| Need | Use instead |
|------|-------------|
| GitHub pull requests and issues | The GitHub CLI `gh` with the owner's own login |
| Linear | Linear's web app or Linear's own CLI with a personal API key |
| Jira | The Jira web app or the Atlassian REST API with an API token |
| Airtable | The Airtable web app or the Airtable REST API with a personal access token |
| Figma | The Figma web app |

## Sending email

`mail send` is off by default. `python3 system/scripts/gmail_send_guard.py --set on` writes `PM_OS_GMAIL_SEND=on` to `local/machines/<MACHINE>.env` for this machine only, and the process environment can only turn it off. `gmail.modify`, which the default `--scopes full` token holds, covers `users.drafts.send`, so no new sign-in is needed. `pmos doctor` prints the state and fails when the flag is on and the token cannot send.

`system/scripts/gmail_send_guard.py` refuses before any network call unless the process is an attended Claude Code session in the CoS or Launchpad tab, outside a worktree, with no worker markers. It then refuses unless the newest owner turn says send, an assistant message since the owner's previous turn showed this draft's recipients, subject, body, and link targets, and that turn has not already approved a send. A draft with attachments is refused, so send those from Gmail. Lines the mux typed into the pane never count as the owner: `cmux-shim.py` keeps a hash ledger of them at `~/.pm-os/injected-prompts.jsonl`. `--expect-hash` must match `show`'s `Send check`, which covers the headers and every MIME part, so an edited draft is refused. This gates the sanctioned path and is not a sandbox: an agent runs as the owner, so one that sets out to defeat it (raw `wmux send` into the CoS pane, a forged session id and transcript file, its own HTTP call with the token) is outside it.

Each attempt and outcome is appended to the gitignored `.state/gmail-sends.jsonl` with time, To, Cc, Subject, and message id, never a body. There is no `send-message` verb, because a saved draft is what ties the send to the text the owner saw.

## Verify

```sh
pmos --version
pmos status
pmos doctor
pmos mail search --max-results 1
pmos calendar list
pmos drive query --raw-query "name contains 'notes'" --page-size 1
pmos slack people --query owner
```

Set `PMOS_SHIM_TRACE=1` to see each HTTP call on stderr while debugging.

## Tests

The tests replace `Http.send`, the shim's single network seam, with a fake transport. They need no network and no tokens.

```sh
python3 system/scripts/test_gmail_send_guard.py
cd system/connectors
python3 -m unittest test_pmos_shim
```

They cover flag parsing, query construction, output shaping for the repo parsers, auth file handling with token refresh, and the "not available" and "not configured" paths.
