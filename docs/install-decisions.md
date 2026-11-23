# Install decisions

Every setup choice an installer meets is decided here. The answers cover macOS, native Windows, and Linux. The answers hold on every device, every operating system, every harness (Amp, Claude Code, Codex CLI, Cursor, a chat tool), and every model. `install.py` detects the environment and applies them; `verify-kit-install.sh` reports what was applied.

**Agents installing or updating this kit: apply the fixed decisions below. Do not ask the owner the questions this file answers.** Exactly three choices belong to the owner: where the install folder goes, whether to run the full loop, and which Claude Code memory to use. Ask those three with the prompt text in [Owner choices](#owner-choices), verbatim, and nothing else. When the environment does not match any row, pick the row marked default and say which one you picked. Ask only when a decision here is impossible on the machine, and ask that one question.

## Summary

| Decision | Default | Rule | Override |
|---|---|---|---|
| Organization tools | Optional, not shipped | The kit calls them only when a profile or the owner adds them. | none |
| Scope | Detected from the machine | Policy, skills, sub-agents, guardrail hook everywhere. The dispatch loop only where Amp and cmux or wmux both exist, and only when the owner says yes (owner choice 2). | none; the loop is never ported |
| Runtime on Windows | Native Windows | Git for Windows bash, Windows Python, wmux, Task Scheduler. WSL2 is an option, never a requirement. | `SWITCHBOARD_RUNTIME=wsl` |
| Location | Owner choice 1; suggested `{{SWITCHBOARD_ROOT}}` | Asked on a terminal when `--destination` (`install.py`) or `-SwitchboardRoot` (`install.ps1`) is absent; `SWITCHBOARD_ROOT` only shapes the suggestion. A synced folder (Dropbox) is allowed. Worktrees and fast-changing state move outside it. One device per synced checkout. | `--destination DIR`, `SWITCHBOARD_ROOT` |
| Sidebar on Windows | Native wmux sidebar | The shim drives wmux's own row signals; `seed-session.ps1 -Restart` applies pins, colors, and order. New panes open in Git Bash. The switchboard board is opt-in. | `SWITCHBOARD_BOARD_AUTOSTART=1`, `SWITCHBOARD_WMUX_SHELL=default` |
| Connectors | The kit's `switchboard` shim, unless another `switchboard` is already on PATH | `system/connectors/` serves the same Slack, Gmail, Drive, Docs, Sheets, and Calendar verbs through the owner's own credentials. | none |
| Harness | Every harness on PATH is set up | Amp gets plugins and `amp-mem`. Claude Code gets the guardrail hook, skill copies, the switchboard plugin (`switchboard@switchboard-local`, runs the Amp plugin modules through Bun), and MCP servers through `install-claude-code.sh`. Codex and Cursor get the adapters. No harness is chosen. | `--skip-plugins`, `--skip-mcp` on `install-claude-code.sh` |
| Claude Code memory | Owner choice 3; built-in recommended on a personal machine | `--memory builtin` keeps Claude Code's own memory, auto-updater, and permission prompt untouched and installs no memory worker. `--memory claude-mem` installs the third-party worker and turns the built-in memory and auto-updater off. | `--memory builtin|claude-mem` on `install-claude-code.sh` |

## 1. Organization tools

The kit ships no organization tools. An organization's own CLIs, skills, and MCP servers are optional: the kit calls them only when a profile (`docs/profiles.md`) or the owner adds them. Without them, every rule, skill, and script in the kit still installs and runs.

- **Never edit around a missing tool.** Never delete, stub, comment out, or rewrite a kit skill, script, or policy line because a tool it can call is absent. The next `--update` reapplies the kit copy and the edit is lost, and a second device with the tool loses the capability.
- **Missing tools report at use time.** A skill that needs a missing tool reports the missing tool when it runs. That is the designed behavior.
- **Verify reports a note.** `verify-kit-install.sh` prints `note  decision: internal-tooling  dormant (switchboard not on PATH; skills that need it report that at use time, nothing to remove)`, never a failure.
- **Personalization is separate.** Personalize `AGENTS.md` and the configs under `system/config/` as the README says. Those are the owner's files; dormant tooling is not a reason to edit them.

The connector shim (decision 6) is how Slack, Gmail, Drive, Docs, Sheets, and Calendar work with no other CLI. It is additive. It never replaces or edits a skill.

## 2. Scope

Scope is detected, never chosen. Two tiers exist:

- **Policy and skills** (every harness): `AGENTS.md`, `system/behavior-map.md`, `.agents/skills/`, `sub-agents/`, `templates/`, `knowledge/`, the harness adapters, and the Claude Code guardrail hook. Run install steps 1 to 5 and 7 to 8 of the kit `README.md`, then `adapters/install-adapters.sh --apply`, then `system/scripts/install-claude-code.sh` when `claude` is on PATH (decision 7).
- **Full loop** (Amp plus cmux on macOS; Amp plus wmux on Windows, not yet proven there; see the limit below): everything above plus Amp plugins, dispatch, worker tabs, command center, memory, and the background loops (launchd or Task Scheduler). Run all nine steps. The owner says yes or no to this tier with owner choice 2; the installer never turns it on by itself.

Native Windows limit in this release: the core scripts (`dispatch.py`, `running-task-state.py`, the daemons, the todo CLI, the locking helpers) now import and lock on Windows through `system/scripts/flock_compat.py`, and Python calls to the cmux wrapper go through `lib/mux.py` `runnable()`. The full loop on native Windows has not had an end-to-end run yet, so owner choice 2 still recommends skipping it there.

Linux has no cmux or wmux, so it is always the policy-and-skills tier. The only background job there is the Claude memory worker (a systemd user service written by `install-claude-code.sh`). `install-launchd.sh` says so and exits 0.

`install.py` reports `scope: full-loop` when `amp` is on PATH and a terminal backend (cmux or wmux) is present. Otherwise it reports `scope: policy-and-skills`. Install the same files either way; the tier only changes which steps you run afterwards, and the full-loop steps run only after the owner's yes.

The dispatch loop is never ported to Claude Code, Codex, Cursor, or a chat tool during an install. `adapters/README.md` lists what stays Amp-only and why. An installer that proposes a "port the loop" project is off the documented path; stop and install the policy-and-skills tier instead. In those harnesses, do a task in your own terminal and record the result in the workstream `CONTEXT.md`.

## 3. Runtime on Windows

Default: **native Windows for every scope.** Install Python 3.11 or newer, Git for Windows (its `bash.exe` runs the shell scripts), ripgrep, and for the full loop wmux and the Amp CLI. Run `py install.py` from PowerShell or `sh install.sh` from Git Bash. Then run `system\windows\install.ps1`, which sets the user environment, adds a `source` line to the Git Bash `~/.bashrc`, registers the Task Scheduler jobs that mirror `system/launchd/`, and seeds the wmux session. Step by step: `docs/install-windows.md`.

WSL2 is not required. Claude Code, Codex, Cursor, Amp, and wmux all run natively. WSL2 adds a second filesystem, a second Python, and a `\\wsl$` path hop between wmux and the repo. Set `SWITCHBOARD_RUNTIME=wsl` before `install.py` and pass `-Runtime wsl` to `install.ps1` only when the owner already works inside a distro and wants the loop there.

`install.py` reports `runtime: windows-native`, `runtime: wsl`, `runtime: macos`, or `runtime: linux` from the environment (`SWITCHBOARD_RUNTIME`, `MSYSTEM`, `WSL_DISTRO_NAME`, `platform.system()`).

Known limit: the wmux shim and the Windows scripts are covered by unit tests against a fake wmux binary and the wmux 2.49.0 source, not yet by a run on every Windows machine. The first full-loop install on a machine runs `CMUX_SHIM_TRACE=1 cmux list-workspaces` and `seed-session.ps1 -DryRun` and reports any shape mismatch. That is a verification step, not a decision.

## 4. Location

The install folder is owner choice 1. Suggested: `{{SWITCHBOARD_ROOT}}` (`%USERPROFILE%\Development\switchboard` on Windows). `install.py` asks on a terminal whenever `--destination DIR` is absent, and takes the suggestion without asking when it has no terminal. `install.ps1` asks the same question with the same suggestion whenever `-SwitchboardRoot` is absent, then writes the answer to `SWITCHBOARD_ROOT` in the user environment and in the env file, so a later run, `--update`, the scheduled tasks, and `verify-kit-install.sh` all use that folder. An installing agent asks the owner with the prompt in [Owner choices](#owner-choices) and passes the answer as `--destination`. The kit clone and the install destination are different folders: clone the kit to `~/Development/switchboard-kit` (or anywhere), install into the chosen folder.

**A synced folder is allowed.** When the destination sits under Dropbox, OneDrive, iCloud Drive, Google Drive, or Box, `install.py` prints the segment it matched and relocates the parts a sync client damages:

- **Worktrees move out.** Task worktrees live in `SWITCHBOARD_WORKTREES_DIR`: `%LOCALAPPDATA%\switchboard\worktrees` on Windows, `~/.local/state/switchboard/worktrees` elsewhere. `dispatch.py`, `command-center-loop.sh`, and `review_surface.py` already read that variable.
- **Fast-changing state moves out.** The shim sidecar, the connector tokens, the board log, and the env file live under `~/.switchboard` (`%USERPROFILE%\.switchboard` on Windows), never in the synced tree. `SWITCHBOARD_STATE_HOME` records that directory; the sidecar follows an override of it, the other three keep the `~/.switchboard` default (`SWITCHBOARD_CONNECTORS_DIR` and `SWITCHBOARD_BOARD_LOG` move them one by one).
- **Dropbox ignores the rest.** For Dropbox, `install.py` marks `.git`, `.amp`, `.state`, and `system/state` with the `com.dropbox.ignored` attribute (an NTFS alternate data stream on Windows, `xattr` on macOS, `user.` xattr on Linux). Dropbox then keeps them local. OneDrive, iCloud Drive, and Google Drive have no equivalent marker; the installer warns and relies on the relocation above.
- **One device per synced checkout.** Two machines must never run switchboard from the same synced folder at the same time. The second device excludes that folder with Selective Sync or installs its own copy outside the synced tree. The repository's `origin` remote is the sync path for the repo content.

`install.py` writes these choices to `SWITCHBOARD_STATE_HOME/env` as `KEY=VALUE` lines (`SWITCHBOARD_ROOT`, `SWITCHBOARD_WORKTREES_DIR`, `SWITCHBOARD_STATE_HOME`, `SWITCHBOARD_MUX`, `SWITCHBOARD_RUNTIME`, `SWITCHBOARD_SYNCED_SEGMENT`, `SWITCHBOARD_BOARD_AUTOSTART`). `system/windows/switchboard-env.sh` and `install.ps1` read that file, so every shell and scheduled task sees the same paths. `--allow-synced-path` is still accepted for old scripts and changes nothing.

## 5. Sidebar on Windows

Default: the **native wmux sidebar**, driven by the shim. The sidebar never loads in a browser.

On macOS, cmux renders the switchboard sidebar from `system/cmux-sidebars/switchboard.swift`: lane headers with counts, row tints, amber unread badges, check and close buttons on each row, a lightning bolt on worker rows. wmux 2.49.0 has no sidebar extension point, so the kit maps each element onto what wmux's own sidebar can show and keeps the rest in the shim sidecar. The mapping table and the install steps are in `system/windows/README.md`.

What the shim does on every call, with no action from the owner:

- **Lane as status text.** `wmux set-status --state idle --text <Lane>` puts the lane name on the row's second line (`Done: To review`) with a green dot. A running agent shows the amber dot. An interrupted one shows red.
- **Needs attention.** `wmux report-agent --blocked "Amp needs input" --surface <id>` shows the violet "Needs you" line with answer buttons; `--unblocked` clears it.
- **Unread.** `cmux notify` targets the workspace, so wmux shows its own unread count badge, and the sidecar keeps the count for `sidebar lanes`.
- **Colors and pins.** The fixed surfaces (CoS, To Do, Launchpad, Comms Triage) carry fixed colors; pins and colors for every row live in the sidecar. wmux cannot order tabs, so switchboard leaves the order alone.

What wmux cannot do from outside the app, and the fixed procedure for each:

| wmux 2.49.0 limit | Fixed procedure |
|---|---|
| New panes open in PowerShell (`pwsh`, then `powershell`, then `cmd`). | Git Bash is the default. The shim passes `--shell bash.exe` on every `new-workspace`; `seed-session.ps1` sets `wmux-workspace-prefs.defaultShell=bash.exe` and repoints the fixed rows in `session.json`. `SWITCHBOARD_WMUX_SHELL=default` in `~/.switchboard/env` keeps PowerShell. The installing agent never asks which shell to use. |
| No CLI or IPC sets `customColor`, `pinned`, or row order. | The shim writes color and pin to the sidecar. `powershell -File system\windows\seed-session.ps1 -Restart` closes wmux, writes them into `session.json`, and reopens it. Run it after launching the fixed surfaces and whenever a pin or color changed. Row order is not managed on wmux. |
| No CLI or IPC reads or sets unread counts. | The sidecar is the source of truth. `cmux notify --workspace <ref>` bumps it and sends a targeted `wmux notify`, so the native badge and the sidecar agree. |
| No event stream over the pipe. | The shim refreshes its view on each call. Nothing polls wmux. |
| No custom sidebar, plugins, grouping, or extra row fields. | Use the native rows plus `wmux-sidebar-prefs` (`leftRail`, `showNotificationMessage`), which `seed-session.ps1` writes. The switchboard board stays opt-in: `SWITCHBOARD_BOARD_AUTOSTART=1` and `python system/scripts/mux/switchboard-board.py open`. |
| Status text shows only when idle (`Done: <text>`). | The dot color carries the state while running or interrupted; the lane text returns when the agent is idle. |
| `list-workspaces` returns no color, pin, unread, or status. | `cmux sidebar lanes --json` reads the sidecar and is the view every switchboard script uses. |

An installing agent runs the launch, runs `seed-session.ps1 -Restart`, and does not ask the owner whether to build a sidebar, fork wmux, put lane glyphs in workspace titles, or open a board. Those paths are rejected in `system/windows/README.md`.

On macOS this decision reports `cmux-sidebar (native)`. In the policy-and-skills scope it reports `none`, because no terminal backend runs.

## 6. Connectors

Default: **the kit's `switchboard` shim.** Skills and scripts for Slack, Gmail, Drive, Docs, Sheets, and Calendar call `switchboard <tool> <verb>`, and `system/connectors/switchboard-shim.py` answers those command lines through the public Slack Web API and Google REST APIs with the owner's own credentials. If another program named `switchboard` is already on PATH, the installer leaves it in front.

- **On PATH only when needed.** On Windows, `install.ps1` and the `switchboard-env.sh` hook add `system/connectors` to PATH only when `switchboard` is absent. On macOS or Linux the owner adds it to the shell profile by hand. With another `switchboard` on PATH, the shim is never called.
- **Owner credentials.** `switchboard login google` runs the OAuth desktop flow with the owner's own Google Cloud OAuth client; `switchboard login slack` stores a user token from the owner's own Slack app. Tokens live under `~/.switchboard/connectors/` (override `SWITCHBOARD_CONNECTORS_DIR`) with owner-only permissions. `switchboard status` and `switchboard doctor` report what works.
- **Covered verbs.** The shim covers the verbs the kit's scripts and skills use (post, update, read thread, search, users, DM, permalink, reactions; Gmail search, draft, send; Drive read, search, export, metadata, comments, upsert, Docs write and edit, Sheets get, sharing; Calendar list and update). An uncovered verb exits 2 and names the covered set. The table is in `system/connectors/README.md` and the `using-connectors` skill.
- **Never rewrite a skill.** When a verb is uncovered, the agent reports it. It does not edit the skill, the shim's caller, or the policy.
- **Admin block risk.** A Google Workspace administrator can block third-party OAuth clients. The shim then reports the `admin_policy_enforced` error verbatim, and the owner falls back to the browser path for that service.

`install.py` reports `connectors: switchboard (organization CLI)` or `connectors: switchboard shim (system/connectors; run switchboard login)`.

## 7. Harness

Default: **set up every harness found on PATH, choose none.** `install.py` reports the list as `harnesses: amp,claude,codex,cursor` in the scope line. The kit never asks which one the owner prefers and never removes another harness's files. The one question inside this section is owner choice 3, the Claude Code memory.

- **Amp:** `install-amp-skills.sh`, `install-amp-plugins.sh`, and `amp-mem` (steps 5, 6, 9 of the README). The dispatch loop exists only here.
- **Claude Code:** `adapters/install-adapters.sh --apply` (managed block in `~/.claude/CLAUDE.md`, skill copies in `~/.claude/skills/`), then `system/scripts/install-claude-code.sh --memory builtin|claude-mem` with the owner's answer to owner choice 3. That script copies the skills listed in `system/config/claude-skills-manifest.yaml` (copies, never symlinks; the pinned third-party skills ship in `system/vendor/skills`, so no network is needed, and `--refresh-vendor` re-fetches them), installs the plugins switchboard relies on plus the switchboard plugin itself (`switchboard@switchboard-local` from the local marketplace `system/claude-marketplace`, whose hooks run the Amp plugin modules through Bun so every Claude Code session gets the same guardrails, task liveness, workspace reports, slash commands, and reviewer agents), and adds the user-scope MCP servers (`chrome-devtools`, plus a build task queue server where `uvx` exists, except on Windows). With `--memory builtin` (the default) it stops there: Claude Code's own memory, auto-updater, and permission prompt stay as they are (the three keys that control them are never written; the plugin step only records `switchboard@switchboard-local` under `enabledPlugins` in `~/.claude/settings.json`). With `--memory claude-mem` it also installs `claude-mem` at the pinned `CLAUDE_MEM_VERSION` with telemetry off, auto-start off, and the welcome hint off (`CLAUDE_MEM_WELCOME_HINT_ENABLED=false`), registers the memory worker with the service manager the OS has (launchd `com.switchboard.claude-mem`, systemd user unit `switchboard-claude-mem.service`, Task Scheduler `\switchboard\com.switchboard.claude-mem`), and sets `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, and `skipDangerousModePermissionPrompt: true` in `~/.claude/settings.json` (the plugin hooks are the guardrail, so the prompt adds nothing). Every step is idempotent. A missing `bun`, `npx`, `uvx`, or `git` skips its step with the install command for the OS and never prompts. The plugin kill switch is `~/.claude/switchboard-plugin-config.json` with `{"switchboard-bridge.disabled": true}` (or one key per module, for example `{"switchboard-guardrails.disabled": true}`). `system/scripts/check-claude-mem.py` reports the worker when `claude-mem` was chosen, and `system/background-tasks.yaml` runs it once a day when `claude` exists.
- **Codex CLI and Cursor:** the adapters only. No memory, no plugins.
- **Two harnesses on one machine:** both are set up. `amp-mem` and the Claude Code memory (built-in or `claude-mem`) are separate stores and never merge. The Amp guardrail plugin, the Claude project hook, and the `switchboard@switchboard-local` plugin enforce the same rules in their own harness.

An installing agent does not pick a harness, does not rewrite Amp plugins for Claude by hand (the `switchboard@switchboard-local` bridge already runs the same modules), does not install `claude-mem` unless the owner chose it, and never installs it from a floating version. Those are off the documented path.

## Owner choices

Three questions belong to the owner. An installing agent asks each one with the text below, verbatim, in this order, and applies the answer with the flag named under it. Lead with the recommendation and what the owner gets; the mechanism stays in the fixed sections above. Nothing else in this file is a question.

### Choice 1: install folder

> Where should switchboard live? This folder holds the repo, your notes, people profiles and workstreams. Suggested: `{{SWITCHBOARD_ROOT}}` (on Windows `C:\Users\<you>\Development\switchboard`). Type another path to choose a different folder or drive, or press Enter to accept. A synced folder such as Dropbox works; run switchboard from one device at a time there, because git locks and the background jobs clash across devices.

Apply: `install.py --destination <answer>`, then on Windows `install.ps1 -SwitchboardRoot <answer>`. Both installers ask this themselves on a terminal; a non-interactive run takes the suggestion and says so. The suggestion is `SWITCHBOARD_ROOT` when set, else the folder recorded in `~/.switchboard/env` by an earlier install (so `--update` finds the owner's folder), else `{{SWITCHBOARD_ROOT}}`; `install.ps1` also tries the installed repo it sits in before that last default. Any path works, including one with spaces such as `C:\Users\<you>\OneDrive\switchboard`; quote it on the command line.

### Choice 2: full loop

> **Recommendation: skip the full loop on this machine.** You keep everything that makes the agents behave like switchboard: the rules in AGENTS.md, all skills, the sub-agent reviewers, templates, the guardrail hook, and the connectors. You work by typing tasks in your own terminal.
>
> The full loop is the automation on top of that: a dispatcher that turns a request into a worker tab, the worker threads that do the task in their own git worktree, the daemons and scheduled jobs that keep the command center, the to-do list, and the digests current. With it, switchboard runs tasks in the background and reports back. Without it, you run tasks yourself and nothing runs on a schedule.
>
> On native Windows the full loop is not proven yet: the dispatcher, the task ledger, the daemons, and the to-do CLI now start on Windows, but no one has run the whole loop there end to end. Installing it registers scheduled tasks and changes every new wmux pane to Git Bash. Pick it on a Mac, or on Windows when you want to be the first full-loop test.
>
> 1. **Skip it (recommended):** policy and skills only. No scheduled tasks, no wmux changes. On Windows that is `install.ps1 -SkipTasks -SkipSeed`.
> 2. **Install it anyway:** adds Amp, Ruby, Bun, the scheduled tasks and the wmux session, knowing the loop has not had a full run on native Windows yet.

On a Mac with Amp and cmux the third paragraph is dropped and the question is the same. Apply: option 1 runs README steps 1 to 5, 7, 8, `adapters/install-adapters.sh --apply` and, on Windows, `install.ps1 -SkipTasks -SkipSeed`; option 2 runs all nine steps.

### Choice 3: Claude Code memory

> **Recommendation for a personal machine: keep Claude Code's built-in memory.** Claude Code keeps remembering across sessions the way it does today, its auto-updater stays on, and its permission prompt stays on. switchboard installs no memory service. You still get every rule, skill, reviewer, the switchboard plugin, and the MCP servers.
>
> The alternative is `claude-mem`, a third-party memory worker that runs as a background service on this machine and replaces Claude Code's built-in memory with a searchable observation log shared with switchboard. Choosing it turns off Claude Code's built-in memory and auto-updater and skips its bypass-permissions warning. Pick it on a work machine where switchboard already runs the full loop and you want one memory across Amp and Claude Code.
>
> 1. **Built-in memory (recommended):** `install-claude-code.sh --memory builtin`. The memory, auto-updater and permission settings in `~/.claude/settings.json` stay as they are.
> 2. **claude-mem:** `install-claude-code.sh --memory claude-mem`.

This choice is independent of scope (full loop or policy and skills) and of how widely the policy loads (global or project only). Apply the flag exactly as the owner answered; `verify-kit-install.sh` reports `memory: Claude Code built-in` or the `claude-mem` worker status accordingly.

## Backups at install

`install.py` looks for `local/backups` before it writes anything: in the destination, in the folder recorded in `~/.switchboard/env`, then in Dropbox. It shows the newest copy and its manifest check, then asks. An installing agent does not decide this: on a found backup it asks the owner, then reruns with `--restore-backup yes` or `no`. A restore never overwrites a newer local file, and install and cleanup never delete `local/`. Details: [backup-and-restore.md](backup-and-restore.md).

## What the installer prints

`install.py` writes a `decisions` object in its JSON plan and a `decision:` line per row on stderr:

```
decision: location   C:\Users\owner\Development\switchboard (not synced)
decision: scope      full-loop (amp: yes, mux: wmux, harnesses: amp,claude)
decision: runtime    windows-native
decision: internal-tooling  dormant (switchboard not on PATH)
decision: sidebar    wmux-native (set-status lanes, report-agent blocked, targeted notify; seed-session.ps1 -Restart for pins, colors, order; panes open in Git Bash; board opt-in)
decision: connectors switchboard shim (system/connectors; run switchboard login)
```

`verify-kit-install.sh` prints the same six lines under `applied decisions`, plus a Claude Code block (version, managed block, skill copies, the memory choice, `switchboard@switchboard-local` plugin, and the bridge deny probe when `bun` exists) when `claude` is on PATH. A second device, a new harness, or a different model reads the same lines and takes the same path, and asks the owner the same three questions.
