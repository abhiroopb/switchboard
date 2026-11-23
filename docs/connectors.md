# Connecting Google, Dropbox, GitHub and Slack

switchboard reaches outside services through two command-line tools. `switchboard` is switchboard's own connector, a standard-library Python program in `system/connectors/` that talks to the Google, Dropbox and Slack APIs with your own app and your own sign-in. `gh` is GitHub's official CLI. Every connection is optional, and switchboard runs without any of them.

This page is the long version of step 5 in [INSTALL.md](../INSTALL.md). The full verb list, output shapes and exit codes are in [system/connectors/README.md](../system/connectors/README.md).

## Ground rules

- **You run every sign-in.** Each sign-in opens your browser or asks for a token at a hidden prompt, so you run the command yourself in your own terminal. In Claude Code you can type `! <command>` to run it from the chat. The installing agent shows you the command, says what it opens, and waits for you.
- **No agent starts a sign-in on its own.** An agent asks first and gets a yes before any `login`, `auth`, OAuth or device-code step, now and later. That includes running a tool with a temporary home folder, which makes the tool start a fresh sign-in.
- **Secrets never enter the repo.** Tokens and client files live in your per-user connector folder, `~/.switchboard/connectors/` (`%USERPROFILE%\.switchboard\connectors\` on Windows). `switchboard` writes them with owner-only permissions. `SWITCHBOARD_CONNECTORS_DIR` moves the folder. The nightly backup leaves these files out on purpose, so after a restore you sign in again.
- **Nothing is printed.** `switchboard` never prints a token or logs a request body. An agent never opens, reads or echoes a token or client file. It may check that a file exists.
- **Admins can say no.** A Google Workspace or Slack administrator can block apps you create. `switchboard doctor` then shows the error, and the browser is the fallback for that service.

## Before you start: put `switchboard` on your PATH

On Windows, `system\windows\install.ps1` and the Git Bash hook already add it. On macOS and Linux, add this line to your shell profile (`~/.zshrc` or `~/.bashrc`) and open a new terminal:

```
export PATH="<install folder>/system/connectors:$PATH"
```

Check it with `switchboard --version`. It prints `switchboard-shim <version>`. If another program named `switchboard` comes first on your PATH (your organization's own tool, for example), switchboard uses that one instead and you follow its setup. `python3 system/connectors/company_cli.py` shows which one it found.

## Google: Gmail, Calendar, Drive, Docs, Sheets and Slides

One sign-in covers all six. Google gives no shared app for personal tools, so you create a small one in your own Google Cloud project. It takes about ten minutes and costs nothing.

### Create your OAuth client

1. **Create a project.** Open the [Google Cloud console](https://console.cloud.google.com/projectcreate) and create a project with any name.
2. **Enable four APIs.** In that project, open each link and click "Enable": the [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com), the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com), the [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com) and the [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com). Docs and Slides need no API of their own, because `switchboard` reads and writes them through Drive. Reading a spreadsheet by sheet name uses the Sheets API.
3. **Set up the consent screen.** Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and click "Get started". Give the app a name, pick your own address as the support email, and choose "External" for a personal Google account or "Internal" for a Workspace account you administer.
4. **Choose who can sign in.** Under "Audience", add your own address as a test user. Then click "Publish app" to move it to "In production". An app left in testing gets refresh tokens that expire after seven days. You do not need Google's verification for your own use. The consent page will say "Google hasn't verified this app", and you choose "Advanced", then continue.
5. **Create the client.** Under "Clients", create a client of type "Desktop app" and download its JSON file.
6. **Save the file.** Save it as `~/.switchboard/connectors/google-oauth-client.json`. To keep it somewhere else, pass `--client-file <path>` in the next step. Keep it out of the repo and out of synced folders, because it holds the client secret.

### Choose the scopes

| Choice | What Google grants | What works | What does not |
|---|---|---|---|
| `full` (default) | `gmail.modify`, `drive` and `calendar` | Search and read mail, create drafts, label mail, read and write Drive files, Docs, Sheets and Slides, comment on files, and put to-do holds on your calendar. | Nothing that `switchboard` offers. Sending mail stays off until you turn it on (see below). |
| `readonly` | `gmail.readonly`, `drive.readonly` and `calendar.readonly` | Search and read mail, read Drive files, Docs, Sheets and Slides, and read your calendar. | Drafts, labels, file writes, comments and to-do calendar holds. The to-do list skips holds on a read-only token. |

Start with `readonly` if you only want switchboard to read. You can switch later by running the sign-in again with the other choice.

### Sign in

Run it yourself:

```
switchboard login google --scopes full        # or --scopes readonly
```

Your browser opens Google's consent page, and the command prints the same address in case it does not open. It waits up to five minutes for you on a local `127.0.0.1` address. When you approve, `switchboard` stores `google-token.json` in the connector folder and refreshes it on its own from then on.

### Verify

```
switchboard status
switchboard doctor
switchboard mail search --max-results 1
switchboard calendar list
switchboard drive query --raw-query "trashed = false" --page-size 1
```

`doctor` prints `ok` for mail, drive and calendar. A `FAIL` with HTTP 403 usually means an API from step 2 is not enabled. An `invalid_grant` error a week after setup means the app is still in testing.

### What Docs, Sheets and Slides support today

- **Read.** `drive read` returns a Doc as text or Markdown, a Sheet as CSV, and Slides as text.
- **Write.** `drive put` creates a file or replaces a whole file's content, for example a Markdown draft turned into a new Doc.
- **Comments.** `comments`, `comment-add` and `comment-reply` work on any Drive file. `system/scripts/sheet-cell-comment.py` anchors a comment to a Sheets cell.
- **Not yet.** Targeted edits inside an existing Doc or deck, such as changing one paragraph without rewriting the file, have no `switchboard` verb. Make those in the browser for now.

### Sending email

Email is drafted, never sent, by default. To let the Chief of Staff send a draft after you say "send it" for that exact text, run `python3 system/scripts/gmail_send_guard.py --set on`. It applies to this machine only. The rules are under "Sending email" in [system/connectors/README.md](../system/connectors/README.md).

### Disconnect

Run `switchboard logout google`, then remove the app's access at [your Google account's third-party access page](https://myaccount.google.com/connections).

## Dropbox

switchboard reaches Dropbox in two ways, and you can use either, both or neither.

### Your files: the Dropbox desktop app

Skills read and write your Dropbox files as ordinary local files in your Dropbox folder. `switchboard` finds that folder from the desktop app's `info.json`, and `SWITCHBOARD_DROPBOX_ROOT` overrides it. Nothing needs a sign-in beyond the desktop app itself. Installing switchboard inside a Dropbox folder also works: [install-decisions.md](install-decisions.md) explains what the installer moves out of the synced tree.

### Shared links: a small Dropbox app

This part is optional. It lets a skill make a shared link to a file. Dropbox gives no shared app, so you create one.

1. **Create the app.** Open the [Dropbox App Console](https://www.dropbox.com/developers/apps/create) in your normal browser and sign in.
2. **Pick the access type.** Choose "Scoped access", then "Full Dropbox". An "App folder" app cannot reach files outside its own folder. Give it any unique name without the word "Dropbox".
3. **Set permissions.** On the "Permissions" tab, tick `sharing.read` and `sharing.write`, then save the change. Leave any box Dropbox ticked by itself. `account_info.read` is always on.
4. **Add the redirect address.** On the "Settings" tab, under "OAuth 2", add `http://localhost:7473/` to "Redirect URIs". Leave "Allow public clients" on "Allow".
5. **Copy the app key.** It is on the same tab. The app key is an identifier, not a secret. There is no app secret to copy, because `switchboard` uses PKCE.

Sign in yourself:

```
switchboard login dropbox --app-key <APP KEY>
```

Your browser opens Dropbox's consent page, and you have 15 minutes to click "Allow". Add `--no-browser` to print the address instead.

| Scope | What it allows |
|---|---|
| `account_info.read` | Reading your account name, which `doctor` uses. |
| `sharing.read` | Listing the shared links you already have. |
| `sharing.write` | Creating shared links. A new link may be public, depending on your account's defaults. |

The token cannot read or write file contents. It can still make a link to any file, so treat it as sensitive.

Verify with `switchboard doctor dropbox`, which prints `ok` and your account. To disconnect, run `switchboard logout dropbox` and delete the app in the App Console.

## GitHub

switchboard uses the official [GitHub CLI](https://cli.github.com/) for pull requests, issues and reviews. Install it (`brew install gh`, `winget install GitHub.cli` or your Linux package manager), then sign in yourself:

```
gh auth login
```

Choose GitHub.com (or your GitHub Enterprise host), HTTPS, and "Login with a web browser". `gh` shows a one-time code and opens the device page. The default scopes are `repo`, `read:org` and `gist`. Add `--scopes workflow` only if you want agents to edit GitHub Actions files.

For tighter access, create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) limited to the repositories you choose, then pick "Paste an authentication token" in `gh auth login`. Never put the token in a file in the repo or on a command line.

`gh` keeps the token in your system's credential store: Keychain on macOS, Credential Manager on Windows, and the Secret Service keyring on Linux. Without a keyring it falls back to a plain-text `hosts.yml` in its config folder and says so.

To let `git push` and `git clone` use the same sign-in, run `gh auth setup-git`.

Verify:

```
gh auth status
gh api user --jq .login
```

To disconnect, run `gh auth logout` and revoke the token at [github.com/settings/applications](https://github.com/settings/applications).

switchboard never pushes your install folder anywhere. The `gh` sign-in is for the repositories you work on.

## Slack

Slack works the same way with a Slack app you create in your workspace. The scopes and clicks are under "Setup Slack" in [system/connectors/README.md](../system/connectors/README.md). You run `switchboard login slack` yourself and paste the `xoxp-` token at the hidden prompt. Verify with `switchboard doctor`. With Slack connected, the Comms Triage surface includes your Slack messages. Without it, the Slack collectors stay idle.

## Check everything at once

```
switchboard status     # reads the token files, no network
switchboard doctor     # one live read per connected service
gh auth status
```

`doctor` exits 0 when every connected service answers, 1 when one fails and 3 when nothing is connected. A failed Dropbox probe never changes the exit code, because Dropbox is optional.
