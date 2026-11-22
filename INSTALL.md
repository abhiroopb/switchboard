# Installing switchboard

This file is written for the coding agent that installs switchboard. A person can follow it too: every step is a command you can run yourself.

## Rules for the installing agent

- **Ask only the questions in step 2,** in that order, each with its default. Every other choice is already made in [docs/install-decisions.md](docs/install-decisions.md). Apply those and do not ask about them.
- **The user runs every sign-in.** A `login`, `auth` or OAuth step opens the user's browser or asks for a token. Explain the step, show the command, and let the user run it in their own terminal (in Claude Code they can type `! <command>`). Never start one yourself. Skipping a service is always fine, and the user can add it later.
- **Secrets stay out of the repo.** Tokens live in each tool's per-user store: `~/.pm-os/connectors/` for `pmos` and the system keyring for `gh`. Never write a token, client file or app secret into the install folder, and never read or echo one. Do not print the contents of `~/.pm-os/env`.
- **Show before you write.** Run the dry run in step 3 and show its summary before `--apply`.
- **Never edit around a missing tool.** If a tool is missing, report it with its install command and move on. Do not delete or rewrite files that call it.
- **Keep it short.** Report each step in one line. Finish with the summary in the last step.

## 1. Check the machine

Run these and note the results. Do not ask the user anything yet.

```
python3 --version            # 3.11 or newer (Windows: py --version)
python3 -c "import yaml"     # PyYAML; if missing: python3 -m pip install pyyaml
git --version
rg --version                 # ripgrep
```

Then detect:

- **OS.** It is macOS, Windows or Linux.
- **Multiplexer.** On macOS, check `cmux` on PATH or `/Applications/cmux.app`. On Windows, check `wmux` on PATH or `%LOCALAPPDATA%\Programs\wmux`.
- **Agents on PATH.** Check `claude`, `amp`, `codex`, `cursor-agent` and `gemini`.
- **Optional tools.** Check `gh`, `bun`, `node` and `npx`, and whether Obsidian is installed.
- **Browsers.** Run the detection lines for this OS in [docs/browser-control.md](docs/browser-control.md#step-1-detect-the-browsers). Do not open a browser.

If Python, PyYAML, git or ripgrep is missing, give the install command for this OS and stop until it is installed:

| OS | Command |
|---|---|
| macOS | `brew install python@3.12 git ripgrep && python3 -m pip install pyyaml` |
| Windows | `winget install Python.Python.3.12 Git.Git BurntSushi.ripgrep.MSVC`, then `py -m pip install pyyaml` |
| Debian or Ubuntu | `sudo apt install python3 python3-pip python3-yaml git ripgrep` |

## 2. Ask the user

Ask these one at a time. Show the default and accept "default" or an empty answer.

1. **Name.** "What name should switchboard use for you in drafts and notes?" There is no default. The name is stored only in this install.
2. **Email.** "Which email address do you draft email from? Press enter to skip." The default is `you@example.com`, which turns email drafting off.
3. **Folder.** "Where should switchboard live?" The default is `~/Development/pm-os` (`%USERPROFILE%\Development\pm-os` on Windows). Explain in one line that `pm-os` is the folder name its links expect, and that a cloud-synced folder works.
4. **Agents.** "Which agents should I set up? I found: <list>." The default is every agent found.
5. **Mode.** Offer the full parallel loop only when a multiplexer was found and `claude` or `amp` is installed: "Full parallel loop (workers in their own tabs) or single session (one agent does tasks one at a time)?" The default is the full loop when it is offered, otherwise single session. If it is not offered, say why in one line and use single session.
6. **Time zone.** "Which time zone should dates and times use?" The default is the machine's zone (`python3 -c "import datetime; print(datetime.datetime.now().astimezone().tzinfo)"`, or the IANA name from `timedatectl`, `/etc/localtime` or `tzutil /g`). Store the IANA name, such as `Europe/Berlin`.
7. **Services.** "Connect any services now? Google (Gmail, Calendar, Drive, Docs, Sheets, Slides), Dropbox, GitHub, Slack, or none." The default is none. Say that each one is a sign-in the user runs themselves and that each can be added later.
8. **Browser.** Ask only when Claude Code is one of the agents. "How should agents use a browser when a site has no API? (a) a separate agent browser that is signed in to nothing, (b) the agent browser plus, for tasks that need it, your own signed-in browser after you click Allow, or (c) headless only." Offer (a) and (b) only where [docs/browser-control.md](docs/browser-control.md#what-works-today) marks them shipped, which today means Brave on Windows. Elsewhere, say in one line that they are planned for the browsers found and use (c). The default is (a) when it is offered, otherwise (c).
9. **Surfaces.** Ask only for the full loop. "Open every surface each day (Chief of Staff, To Do, Launchpad, Meetings, Scheduled Jobs, Comms Triage), or a lighter set (Chief of Staff and To Do, the rest when you type `load <surface>`)?" The default is every surface.

If `claude` is installed, also ask: "Claude Code memory: built-in (default) or the claude-mem worker?"

## 3. Dry run

Clone the repository somewhere other than the install folder (for example `~/Development/switchboard-src`), then:

```
cd ~/Development/switchboard-src
sh install.sh --owner-name "<name>" --owner-email "<email>" --destination "<folder>" --profile personal --dry-run
```

On Windows from PowerShell, use `py install.py` with the same arguments. Show the user the summary lines (`decision:`, scope, files to write) and ask "Install now?" Continue on yes.

## 4. Install

```
sh install.sh --owner-name "<name>" --owner-email "<email>" --destination "<folder>" --profile personal --apply
cd "<folder>"
```

Add `--owner-role "<job title>"` when you are not a product manager. The title goes into the Role line of `AGENTS.md`.

The installer fills in your name and paths, creates `people/`, `notes/` and `workstreams/`, copies the example configs to their live names, and makes the first local commit. It never adds a git remote.

Then write the time zone from step 2 into the per-user settings file, which the loop and the scheduled jobs read:

```
mkdir -p ~/.pm-os && touch ~/.pm-os/env
grep -q '^PM_OS_TZ=' ~/.pm-os/env || echo 'PM_OS_TZ=<zone>' >> ~/.pm-os/env
```

Then, from the install folder:

| When | Run |
|---|---|
| Always | `bash adapters/install-adapters.sh --apply` (loads `AGENTS.md` and the skills into every agent found) |
| Always | `bash system/bin/install-amp-skills.sh` (copies the bundled skills) |
| Claude Code chosen | `bash system/scripts/install-claude-code.sh` (add `--memory claude-mem` if chosen) |
| Amp chosen | `bash system/scripts/install-amp-plugins.sh` |
| Windows, full loop | `powershell -ExecutionPolicy Bypass -File system\windows\install.ps1` |
| Single session | `python3 system/scripts/apply-profile.py --profile solo` |
| Claude Code workers in the full loop | `bash system/scripts/set-agent.sh claude` |
| Lighter set of surfaces | `python3 system/scripts/machine_role.py --set personal` |
| Neither Google nor Slack picked | `python3 system/scripts/comms_triage_setting.py --set off` (Comms Triage has nothing to read) |

## 5. Connect services (only those the user picked)

Take them one at a time. For each, explain what the user creates and what the sign-in opens, show the command, wait while the user runs it, then run the check. The full steps, scopes and links are in [docs/connectors.md](docs/connectors.md).

On macOS and Linux, first add `export PATH="<folder>/system/connectors:$PATH"` to the shell profile so `pmos` is found. On Windows, `install.ps1` and the Git Bash hook already did it.

| Service | The user creates | The user runs | You check |
|---|---|---|---|
| **Google** | Their own OAuth client of type Desktop app, with the Gmail, Drive, Calendar and Sheets APIs on, saved as `~/.pm-os/connectors/google-oauth-client.json` | `pmos login google --scopes full` or `--scopes readonly`. Ask which first: read-only cannot draft email, write files or place to-do calendar holds. | `pmos doctor` shows `ok` for gmail, google-drive and google-calendar |
| **Dropbox** | Nothing for files, which come through the Dropbox desktop app's folder. For shared links, a small Dropbox app with `sharing.read` and `sharing.write`. | `pmos login dropbox --app-key <KEY>` (shared links only) | `pmos doctor dropbox` shows `ok` |
| **GitHub** | Nothing. `gh` must be installed. | `gh auth login`, then `gh auth setup-git` if they want git to use it | `gh auth status` and `gh api user --jq .login` |
| **Slack** | Their own Slack app with the user token scopes in [system/connectors/README.md](system/connectors/README.md) | `pmos login slack`, pasting the token at the hidden prompt | `pmos doctor` shows `ok` for slack |

Report each result in one line. A `FAIL` or an admin block is fine to leave for later. Give the user the line and move on.

## 6. Set up browser control (Claude Code only)

Apply the answer to question 8. The details, the rules and the support table are in [docs/browser-control.md](docs/browser-control.md).

- **(c) Headless only.** Nothing to do. `install-claude-code.sh` already registered a headless Chrome with its own profile. If Chrome is not installed, say so and give the install command.
- **(a) or (b) Agent browser (Brave on Windows).** Run `python3 system/scripts/owner_browser.py set brave`, then rerun `bash system/scripts/install-claude-code.sh`, then `python3 system/scripts/owner_browser.py agent start` and `agent status`. Copy the rules block from [docs/browser-control.md](docs/browser-control.md#the-rules) into `local/agents-local.md` and rerun `bash adapters/install-adapters.sh --apply`. Today (a) and (b) use the same switch. The signed-in tools do nothing until the user turns on remote debugging in their own Brave and clicks Allow, which only the user does.

Never open the user's browser, its settings pages or its profile folder during install. Scheduled jobs always use the headless profile.

## 7. Verify

```
bash system/scripts/verify-kit-install.sh
```

Report the summary: passes, warnings and failures. A warning about a tool the user chose not to install is fine. Fix a failure if the fix is one of the commands above, otherwise report it with its line.

For the full loop on macOS, start the background loop with `bash system/scripts/install-launchd.sh` after asking: "Start the background loop now? It runs every 10 minutes under your account." On Windows, `install.ps1` already registered the scheduled tasks.

## 8. Finish

Tell the user, in six lines or fewer:

- **Where it lives.** Give the install folder.
- **Mode.** Say full loop or single session, and the agents set up.
- **Services.** Say which are connected and which were skipped.
- **Browser.** Say which setup is on.
- **Verify.** Give the pass, warn and fail counts.
- **Next step.** Full loop: "Open cmux (or wmux), open the Chief of Staff tab and type `start of day`." Single session: "Open your agent in the install folder and type `start of day`." Then point to [docs/first-day.md](docs/first-day.md).

Then offer to fill in the `personalize here` blocks in `AGENTS.md` and `system/chief-of-staff-prompt.md` together: role, team, what a good day looks like, and the people they work with most.
