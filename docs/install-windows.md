# Install on Windows (native, wmux, any harness)

This is the step-by-step path for a Windows 11 machine. It runs natively: Git for Windows bash, Windows Python, wmux, and Task Scheduler. WSL2 is not needed. Every choice below is fixed in [install-decisions.md](install-decisions.md); an installing agent follows the steps and does not ask the owner to choose.

The repo may live in any folder or drive, Dropbox or another synced folder included. Step 3 is that choice.

## Before you start

- Windows 11, PowerShell 5.1 or 7, and `winget`.
- The harness you use: [Amp](https://ampcode.com) CLI for the full loop, or Claude Code, Codex CLI, or Cursor for policy and skills. More than one is fine. Every harness on PATH is set up; nothing is chosen (decision 7 in [install-decisions.md](install-decisions.md)).
- A Google account and a Slack workspace where the owner may create an OAuth client and a Slack app. Only needed for the connectors in step 11.

## Steps

1. **Install the toolchain.** In PowerShell:
   ```
   winget install Python.Python.3.12 Git.Git BurntSushi.ripgrep.MSVC Oven-sh.Bun Obsidian.Obsidian astral-sh.uv
   ```
   Obsidian is how you read the notes PM OS writes (step 9 registers the vault). `uv` installs graphify, the tool that builds the knowledge graph workers search first. Neither one is needed for the other: graphify and memory are separate command-line tools that run without Obsidian. `install-all.sh` installs both when they are missing.
   Bun runs the PM OS plugin hooks in Claude Code; skip it when Claude Code is not installed. Full loop only, add Ruby (the background-task runner parses its YAML config with it):
   ```
   winget install RubyInstallerTeam.Ruby.3.3
   ```
   winget often leaves `rg`, `bun`, and `git` off PATH, and Windows ships a Microsoft Store `python3` stub that opens the Store. Clone or unzip the kit now (step 4 shows where), then run the toolchain step of the Windows installer alone from it. It finds the winget tools, adds their folders to your user PATH, puts the real Python ahead of the Store stub, and makes `python3` name it:
   ```
   powershell -ExecutionPolicy Bypass -File $HOME\Development\pm-os-kit\system\windows\install.ps1 -PrereqsOnly
   ```
   Close and reopen PowerShell so the PATH changes apply. A scheduled task keeps the PATH it got at sign-in, so it does not see these changes until you sign out and in again. Step 7 also records the real Python and the tool folders (git, rg, bun, ruby, python) in `%USERPROFILE%\.pm-os\env` as `PM_OS_PYTHON` and `PM_OS_TOOL_PATH`, and `pm-os-env.sh` turns them into PATH entries for every task, so no sign-out is needed after step 7 has run. Check with `py --version`, `python3 --version`, `git --version`, `rg --version`. Then install the one Python package the scripts import:
   ```
   py -m pip install pyyaml
   ```
2. **Install wmux 2.49.0** (full loop only; skip for policy and skills):
   ```
   scoop bucket add wmux https://github.com/amirlehmam/wmux
   scoop install wmux/wmux
   ```
   If `scoop` is missing, install it first with `irm get.scoop.sh | iex`. Open wmux once and close it, so `%APPDATA%\wmux\sessions\session.json` exists.
3. **Choose where PM OS lives.** Suggested: `%USERPROFILE%\Development\pm-os`. Any other folder or drive works, a synced one such as `%USERPROFILE%\OneDrive\pm-os` included; run PM OS from one device at a time there, because git locks and the background jobs clash across devices. The installer keeps worktrees in `%LOCALAPPDATA%\pm-os\worktrees` and fast-changing state in `%USERPROFILE%\.pm-os`, outside the folder, and for Dropbox marks `.git`, `.amp`, `.state`, and `system\state` as Dropbox-ignored. Put the folder in a variable once in this PowerShell window; every later step uses it:
   ```
   $PmOs = "$HOME\Development\pm-os"          # or "$HOME\OneDrive\pm-os"
   ```
   Both `install.py` (step 6) and `install.ps1` (step 7) ask this same question when you leave the path out, suggest `PM_OS_ROOT`, then the folder recorded in `%USERPROFILE%\.pm-os\env`, then the default, and take the suggestion without asking when no one is at the terminal.
4. **Clone the kit** somewhere other than the destination:
   ```
   git clone <kit-remote> $HOME\Development\pm-os-kit   # or unzip the kit there
   ```
5. **Preview the install** (writes nothing) and read the six `decision:` lines it prints:
   ```
   py $HOME\Development\pm-os-kit\install.py --owner-name "Your Name" --owner-email you@example.com --destination "$PmOs" --dry-run
   ```
6. **Install:**
   ```
   py $HOME\Development\pm-os-kit\install.py --owner-name "Your Name" --owner-email you@example.com --destination "$PmOs" --apply
   ```
   This renders placeholders, creates `people\`, `notes\`, `workstreams\`, copies the `*.example.yaml` configs to live names, writes `%USERPROFILE%\.pm-os\env`, and makes the first commit.
   **A backup, if you have one.** Before it writes anything, `install.py` looks for `local\backups` in the destination, in the folder recorded in `%USERPROFILE%\.pm-os\env`, and in a Dropbox copy of the PM OS folder when Dropbox is installed. It prints the newest copy with its date and manifest check, says how many files a restore would add, replace, keep and skip, and asks before restoring. Newer local files are never overwritten. If it finds nothing it says `backup: none found` and lists where it looked, so stop there if you expected one and let Dropbox finish syncing. With no terminal it restores nothing and prints the one-line command; an installing agent asks the owner, then reruns with `--restore-backup yes`. See [backup-and-restore.md](backup-and-restore.md), which also lists what a restore cannot bring back.

7. **Run the Windows installer.** In PowerShell:
   ```
   cd "$PmOs"
   powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -PmOsRoot "$PmOs" -DryRun
   powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -PmOsRoot "$PmOs"
   ```
   Without `-PmOsRoot` it asks where PM OS lives, with the same question and suggestion as step 6. An installing agent sends the output of `install.py` and `install.ps1` to a log file and reads that file afterward, for example `py ... --apply *> install.log`. Do not pipe it into `Select-Object -First N`: that closes the pipe early, and the installer keeps running with no output.
   It sets the user environment (`PM_OS_ROOT`, `PM_OS_MUX`, `PM_OS_RUNTIME`, `PM_OS_WORKTREES_DIR`, `PM_OS_STATE_HOME`, `AMP_EDITOR`, `PM_OS_BOARD_AUTOSTART=0`), adds `system\windows` and, when no native `pmos` is on PATH (the PM OS shim does not count), `system\connectors` to PATH, appends one `source` line to the Git Bash `~/.bashrc`, registers the `\PM OS\` scheduled tasks (six Amp loops and `com.pm-os.note-links`, plus `com.pm-os.claude-mem` when Claude Code is installed and you chose the claude-mem memory backend), and seeds the wmux session file. It needs no admin rights. Each task registers on its own, and the script ends with `registered N of M tasks` and exits 1 if any failed. It finds wmux in its install folder (`%LOCALAPPDATA%\Programs\wmux`, scoop, `%ProgramFiles%\wmux`) even when wmux is not on PATH, and records the CLI as `WMUX_BIN` in the env file. With `-DryRun` it skips the seed step. Policy and skills scope: pass `-SkipTasks -SkipSeed`.
8. **Verify.** Open a new Git Bash window (so the `~/.bashrc` line from step 7 is active), `cd` into the folder from step 3 (quote it when it has spaces, for example `cd "$HOME/OneDrive/pm-os"`), and run:
   ```
   bash system/scripts/verify-kit-install.sh
   ```
   It also checks that `%USERPROFILE%\.pm-os\env` and the shell's `PM_OS_ROOT` name this folder.
   Expect the six `decision:` lines, `mux backend: wmux` (full loop) and the shim tests passing, or `mux backend: none` (policy and skills). Full loop only: the `cmux sidebar lanes --json` check needs wmux running; with wmux closed it prints a note, and it passes when you rerun verify from a wmux pane after step 12.
9. **Install skills and harness adapters.** In Git Bash:
   ```
   bash system/bin/install-amp-skills.sh
   bash adapters/install-adapters.sh --apply
   ```
   `install-amp-skills.sh` installs the repo-owned skills. Full loop only: `bash system/scripts/install-amp-plugins.sh`, then reload plugins in Amp. It writes small shims that load the repo's plugin files, because Git Bash cannot make symlinks here; the first run over old plain copies needs `--force`, which backs them up to `_pre-symlink-backup`.
   Claude Code installed: also run, in a plain Git Bash window (not inside a Claude Code session; there the script skips the plugin and MCP steps and prints the command to rerun),
   ```
   bash system/scripts/install-claude-code.sh --dry-run
   bash system/scripts/install-claude-code.sh
   ```
   It copies the manifest skills into `%USERPROFILE%\.claude\skills`, adds the plugins (including the PM OS plugin `pm-os@pm-os-local`, whose hooks run through `bun` from step 1; `npx` MCP servers also need `winget install OpenJS.NodeJS.LTS`, then reopen Git Bash) and the MCP servers. Every `claude plugin` and `claude mcp` call prints a progress line and gives up after `CLAUDE_CLI_TIMEOUT` seconds (default 120); after one timeout the script skips the remaining calls and says how to rerun. By default (`--memory builtin`) Claude Code keeps its own memory, auto-updater and permission prompt. Pass `--memory claude-mem` only if you chose the `claude-mem` worker (owner choice 3 in `docs/install-decisions.md`): that installs it at the pinned version, registers it as the `\PM OS\com.pm-os.claude-mem` scheduled task, and sets three keys in `%USERPROFILE%\.claude\settings.json`; check it with `python system/scripts/check-claude-mem.py`. A missing tool skips its step with the install command.
   Then the reader and the knowledge graph (`bash system/scripts/install-all.sh` runs the same three steps):
   ```
   python3 system/scripts/register-obsidian-vault.py
   powershell -NoProfile -ExecutionPolicy Bypass -File system/windows/register-obsidian-notes.ps1
   uv tool install graphifyy==0.9.95 --python 3.12 && python3 system/scripts/build-knowledge-graph.py
   ```
   The first registers this folder as its own Obsidian vault named `pm-os` (review links are `obsidian://open?vault=pm-os&file=…`); it leaves every other vault alone, and when Obsidian is open it closes it once and reopens it, because Obsidian rewrites its vault list while it runs. The second makes a `.md` you open from Windows (Explorer, a `file://` link) open in the vault that holds it: Obsidian's own installer maps `.md` to `Obsidian.exe "%1"`, which opens nothing for a note already inside a vault. It overrides only that handler's open command for your user, keeps the original for `-Unregister`, and leaves `.md` alone when another editor owns it. The third installs graphify and builds `graphify-out/graph.json` (about a minute, no LLM, nothing leaves the machine); the `build-knowledge-graph` background task rebuilds it every 6 hours. Memory needs nothing here: Amp uses `amp-mem` (`~/bin/amp-mem`), and Claude Code uses its built-in memory unless you chose `claude-mem` above. `verify-kit-install.sh` prints one line for each.
10. **Personalize.** Edit `AGENTS.md` and `system/chief-of-staff-prompt.md` (every block marked `personalize here`), then fill `system/config/priority-channels.yaml` and `system/config/stakeholders.yaml`.
11. **Connect Slack and Google** (only when `pmos` is absent; the shim is on PATH). Create a Google Cloud OAuth client of type Desktop app, download its JSON to `%USERPROFILE%\.pm-os\connectors\google-oauth-client.json`, and create a Slack app with a user token. Then:
    ```
    pmos login google
    pmos login slack
    pmos doctor
    ```
    Details and the verb table: `system/connectors/README.md`. With a different `pmos` already on PATH, skip this step.
12. **Launch the fixed surfaces and seed the sidebar** (full loop only). Open wmux and run `bash system/scripts/ensure-routine-workspaces.sh megadesk todo launchpad comms-triage` in the first pane (it opens CoS, To Do, Launchpad, and Comms Triage). Before the seed below, wmux panes open in PowerShell, which has no `bash`; in a PowerShell pane run `& "C:\Program Files\Git\bin\bash.exe" -lc "cd '<PM OS folder>' && bash system/scripts/ensure-routine-workspaces.sh megadesk todo launchpad comms-triage"` instead. Then, from a PowerShell window outside wmux (the seed quits wmux and every pane in it):
    ```
    powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -Restart
    ```
    wmux closes, the pins and colors from the shim sidecar land in `session.json`, and wmux reopens with CoS, To Do, Launchpad, and Comms Triage tinted. The tab order stays as wmux has it: wmux cannot order tabs, and PM OS does not reorder them. The same run sets wmux's default shell to Git Bash (`bash.exe`) and repoints the four fixed rows to it, so every pane runs the PM OS scripts and the `~/.bashrc` hook. Without this, wmux opens new panes in PowerShell. To keep PowerShell, set `PM_OS_WMUX_SHELL=default` in `%USERPROFILE%\.pm-os\env` before this step.
    **Personal or work machine.** A personal machine needs only CoS and To Do. Set its role once with `python3 system/scripts/machine_role.py --set personal` (it writes `PM_OS_MACHINE_ROLE=personal` to `local/machines/<MACHINE>.env`). From then on start of day and the commands above open CoS and To Do and skip Launchpad, Meetings, and Scheduled Jobs, and `seed-session.ps1` stops reporting a closed Launchpad as missing. Say `load Launchpad`, `load Meetings`, or `load Scheduled Jobs` in CoS to open one for the rest of the day (`bash system/scripts/load-surface.sh <surface>`), or add "and keep it" to open it every day. A work machine keeps every surface; no role means work. The background loop in the next step runs on both.
13. **Check the background loop** (full loop only). In PowerShell, `Get-ScheduledTask -TaskPath '\PM OS\'` lists the Amp tasks, the note-link task and the nightly backup (`com.pm-os.local-backup`), plus `com.pm-os.claude-mem` when Claude Code is installed. Run one by hand to confirm: `Start-ScheduledTask -TaskPath '\PM OS\' -TaskName 'com.pm-os.command-center'`, then look for `system\state\command-center-latest.md`. A task that exits nonzero appends `[pm-os task] exit <code>` to its log; read that line first, because Task Scheduler's `LastTaskResult` can show 0 for a task hosted by `conhost.exe --headless`. Register with `-NoHeadless` if you need the real exit code there. Then run the Windows doctor, `python system/scripts/check-windows-install.py` (verify-kit-install.sh prints the same lines): it checks the env file keys the tasks need, every `\PM OS\` task, background-task errors from the last six hours, `gh`, `node`, `uv`, `bun`, the Claude MCP servers, the connectors, the recall inputs, and the wmux note-link server. Lines marked `owner` need your sign-in: `gh auth login`, `pmos login slack`.
14. **First day.** In the CoS pane type `start of day`. The walkthrough is in [first-day.md](first-day.md).

## Troubleshooting

- **`bash: command not found` in a wmux pane.** The pane runs PowerShell. Use the `bash.exe -lc` form from step 12, or run the seed in step 12 so new panes open in Git Bash.
- **A worker pane waits at "Do you trust the files in this folder?".** Rerun `bash system/scripts/install-claude-code.sh` (it records trust for the PM OS folder and `PM_OS_WORKTREES_DIR`), or answer Yes once in that pane.
- **Shared state looks stale in a worktree.** Without Developer Mode (Settings, System, For developers) Git Bash cannot make symlinks, so each worktree gets hard links to shared state, and an atomic rewrite on `main` replaces the file the link pointed to. Turn on Developer Mode, then start new tasks.
- **Terminal windows keep opening by themselves.** A background helper started with no console (`DETACHED_PROCESS`, or a detached Node or Bun spawn) hands every git or python call to a new console, and Windows opens each one in Windows Terminal. PM OS starts background work through `system/scripts/no_window.py` instead, which gives it a hidden console its children share, and `test_no_window.py` fails on any spawn that goes back to the old flag. Count the stray consoles with `Get-CimInstance Win32_Process -Filter "Name='OpenConsole.exe'" | Where-Object CommandLine -match '-Embedding'`, and leave the Windows default-terminal setting alone.
- **Chrome DevTools MCP gets 404 from `127.0.0.1:9222`.** Another browser (often Brave with remote debugging on) holds that port. Turn its remote debugging off, or remove the `chrome-devtools` MCP entry and rerun `install-claude-code.sh`, which registers it with its own headless profile. A machine that opted in with `python3 system/scripts/owner_browser.py set brave` registers `chrome-devtools` as the agent profile instead (a separate Brave in `%LOCALAPPDATA%\pm-os\agent-brave`, signed in to nothing, on 127.0.0.1:9333 with no dialog), `chrome-devtools-owner` for the owner's own signed-in Brave through its permission dialog, and keeps `chrome-devtools-headless` for scheduled jobs. Port 9333 must be free for the agent profile; the proxy refuses to drive any other program found there.
- **`Device or resource busy` or `WinError 5` on a synced folder.** Dropbox or OneDrive holds the file open. PM OS retries atomic writes for 5 seconds; if it still fails, pause sync for the PM OS folder.

## What you see in the wmux sidebar

The native sidebar carries the PM OS signals. The full mapping and the limits with their fixed procedures are in `system/windows/README.md`.

- **Fixed surfaces tinted.** CoS, To Do, Launchpad, and Comms Triage carry their accent rails after `seed-session.ps1 -Restart`. wmux cannot order tabs, so they stay where they opened. A personal machine shows CoS and To Do, plus any surface loaded today.
- **Lane on the second line.** An idle row shows `Done: <Lane>` (for example `Done: To review`) with a green dot. A running agent shows the amber dot. An interrupted one shows red.
- **Needs you.** A blocked agent shows the violet "Needs you" line with answer buttons.
- **Unread.** A targeted `cmux notify` adds wmux's own unread count badge.
- **Workers.** Worker rows keep the `<emoji> <Name> <n>` title; there is no lightning bolt.

Pins, colors, and order change in the sidecar at once and in the native sidebar at the next `seed-session.ps1 -Restart`. The PM OS board (a local web page in a wmux pane) is opt-in only: `PM_OS_BOARD_AUTOSTART=1` and `python system\scripts\mux\pm-os-board.py open`.

## Worker prompts reach CoS under wmux

A worker that stops on a permission prompt sends one callback to CoS, and CoS answers it with `allow` or `deny`. Under wmux this runs through `system/scripts/mux/cmux` (the shim), with these differences from cmux:

- **Typed messages.** wmux has no agent message channel. The shim types the callback into the CoS Claude composer as one line (line breaks show as ` ⏎ `) and presses Enter. It refuses a pane that is blocked, a composer with a draft, a screen it cannot read, and a pane that is not Claude. A refused callback stays in the relay queue and the next tick tries again.
- **Workspace id.** wmux exports no workspace id. The shim asks wmux for the caller's workspace (`current-workspace --surface`).
- **Run state.** When wmux's own Claude hooks are installed, they report the amber and green dots. The PM OS heartbeat then adds nothing. Without them, the PM OS hook reports the run state through `wmux report-agent`.
- **Status pills, pins, colors, and order.** wmux has no named status pills. The shim keeps them in its sidecar file and applies them at the next `seed-session.ps1 -Restart`.

`python system/scripts/lib/mux.py` prints the active backend, the wmux CLI it found, and this capability list.

## Deleting outside PM OS

On Windows, `rm -rf` of a folder outside PM OS goes to the Recycle Bin, and you can restore it from there. Each move is logged as on macOS. If the Recycle Bin cannot take it (a network or removable drive, a link, a path longer than 240 characters, an item too big for the bin, or the bin turned off), nothing is deleted and the agent asks you. The PM OS folder itself, `.git`, a top-level home or system folder, and their Windows aliases are always refused.

Inside the PM OS folder, `rm -rf` deletes outright only what git tracks and holds unchanged, because git can restore it. Anything git has no copy of goes to the Recycle Bin: gitignored folders such as `local/` and `system/state/`, worker scratch, untracked files, and any folder that holds an ignored, untracked, or changed file. A `node_modules` folder and a link (only the link goes) are still deleted outright.

## Update later

An update or reinstall leaves `local\`, where the nightly backups live, alone. The one exception is a backup restore you approve, which brings the owner files back and never over a newer file.

From the kit clone: `git pull` (or unzip the new kit), then `install.ps1 -PrereqsOnly` from the kit as in step 1, then `py install.py --update` (it suggests the folder recorded at install time; or pass `--destination "$PmOs"`), then, in a plain Git Bash window in that folder, `bash system/scripts/install-claude-code.sh` (Claude Code only) and `bash system/scripts/verify-kit-install.sh`. Locally edited kit files are kept; pass `--force` to overwrite them. To replace the kit folder, delete it from PowerShell (`Remove-Item -Recurse -Force`, repeated once if it fails), not `rm -rf` in Git Bash: a synced folder such as Dropbox can hold a file open and Git Bash stops with `Device or resource busy`. `install.ps1` is idempotent and safe to rerun.

## WSL2 instead (not the default)

Set `PM_OS_RUNTIME=wsl` before step 5, clone and install inside the distro, and run `install.ps1 -Runtime wsl -Distro <name>` in step 7. The scheduled tasks then run through `wsl.exe`. Use this only when the owner already works inside a distro.
