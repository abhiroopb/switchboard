# switchboard Kit on Windows (wmux)

The kit runs on Windows with [wmux](https://wmux.org) (amirlehmam/wmux, target
version **2.49.0**) in place of cmux. The switchboard scripts stay bash and Python.
On Windows they run **natively**: Git for Windows bash, the Windows Python, and
the wmux CLI, with no Linux layer in between. A thin shim
(`system/scripts/mux/cmux-shim.py`) translates every cmux CLI call the scripts
make into wmux calls. Nothing in the macOS path changes: `system/scripts/lib/mux.sh`
picks cmux on Darwin and wmux only when `SWITCHBOARD_MUX=wmux`, when wmux's pane
environment (`WMUX=1`) is present, when the shell is MSYS (Git Bash), or when the
shell is WSL with wmux on PATH.

## What runs where

| Runtime | When | What it means |
|---|---|---|
| Native Windows (default) | Every install scope. `install.py` and `install.ps1` pick it on Windows unless told otherwise. | Git Bash runs the scripts, `py` or `python` runs the Python, `wmux.cmd` is called directly, Task Scheduler runs `bash.exe`. State lives in `%USERPROFILE%\.switchboard`, worktrees in `%LOCALAPPDATA%\switchboard\worktrees`. |
| WSL2 (explicit option) | Only when the owner sets `SWITCHBOARD_RUNTIME=wsl` or passes `-Runtime wsl`. | The toolchain lives inside a distro, `wsl.exe` hosts the scheduled tasks, `cmux.cmd` hops into the distro, and `seed-session.ps1` resolves the sidecar through `wslpath`. |

An installing agent applies that rule and does not ask the owner to choose.
The decision record is `docs/install-decisions.md`.

## Layout

| File | Role |
|---|---|
| `install.ps1` | One-shot installer: prerequisites, user env, `~/.switchboard/env` merge, Git Bash `~/.bashrc` hook, Task Scheduler, sidebar seed. `-Runtime wsl` keeps the WSL path. |
| `register-scheduled-tasks.ps1` | Task Scheduler counterpart of `system/launchd/*.plist` and `install-launchd.sh`. Native: `bash.exe -c` under `conhost --headless` (no login shell, which costs about 58 processes per run). WSL: `wsl.exe -- bash -lc`. |
| `apply-hook-profile.py` | Removes Claude Code hooks that do nothing on this machine (the heartbeat under wmux hooks, hookify with no rules). `--check`, `--undo`. A plugin reinstall undoes it, so `install-claude-code.sh` reapplies it. |
| `../scripts/windows_process_rate.py` | Process rate (`rate`), exact count for one command (`count`), who started what this boot (`census`). |
| `../scripts/windows_leak_trace.py` | Run-time object reference tracing and dump analysis for the process leak. Dormant while no leak shows; a driver update can fix or bring it back. |
| `install-wmux.ps1` | Installs wmux from its official source (Scoop bucket, else the GitHub release installer checked against its published SHA-256), then runs `wmux_look.py apply`. `-DryRun`, `-Force`, `-SkipInstall`, `-SkipLook`, `-Instance`. |
| `wmux_look.py`, `wmux-look.json` | The switchboard look for wmux (theme, font, layout) and the script that merges it into `settings.json` without overwriting your own values. See "The switchboard look". |
| `seed-session.ps1` | Writes pins, accent colors, order, and the Git Bash `shell` into wmux `session.json`, merges the sidebar prefs and `defaultShell=bash.exe` into `settings.json`, and with `-Restart` quits and relaunches wmux around the write. |
| `switchboard-env.sh` | Sourced from `~/.bashrc` (Git Bash or WSL): reads `~/.switchboard/env`, picks the runtime and the wmux backend, exports `CMUX_BIN`, `WMUX_BIN`, `SWITCHBOARD_PYTHON` (Windows has `python` and `py`, not `python3`), `AMP_EDITOR`, the vault root, and starts the board only when `SWITCHBOARD_BOARD_AUTOSTART=1`. |
| `wsl-env.sh` | Two-line wrapper that sources `switchboard-env.sh` for older `~/.bashrc` lines. |
| `cmux.cmd` | cmux-compatible entry point for Windows callers (PowerShell, Task Scheduler, Amp): runs the shim with `py -3`, then `python`. `SWITCHBOARD_RUNTIME=wsl` hops into the distro instead. |
| `amp-obsidian-editor.ps1` | `AMP_EDITOR` shim: a clicked `file://` vault note opens as `obsidian://open?vault=switchboard&file=...`. |
| `../scripts/mux/cmux-shim.py` | The adapter: accepts cmux 0.65 verbs, drives the wmux CLI, keeps cmux-only state in the sidecar `%USERPROFILE%\.switchboard\wmux-sidecar.json` (`SWITCHBOARD_WMUX_SIDECAR`, or `SWITCHBOARD_STATE_HOME`). |
| `../scripts/mux/cmux` | bash wrapper around the shim for Git Bash and WSL callers. |
| `../scripts/mux/switchboard-board.py` | Optional local web board that mirrors the cmux lanes. Off by default. |
| `../scripts/mux/note-links.py` | 127.0.0.1:7472 server behind vault-note links. wmux makes only http(s) links clickable, so `review_surface.note_link_mode()` renders note links as `http://127.0.0.1:7472/open?file=<note>.md` on wmux; a click opens the note in Obsidian and shows its text in the wmux browser panel. Kept up by the `com.switchboard.note-links` task (`ensure` every 10 minutes). `SWITCHBOARD_NOTE_LINK=file` turns it off. It also serves `/page/<name>` for local HTML pages named in the gitignored `local/note-links-pages.json` (`{"<name>": "<vault-relative .html>"}`), so a link in a private Google Sheet can open a local app on this machine; a cross-site click works, a cross-site fetch or frame gets 403, and the page's CSP blocks fetches and subresources (not navigation). |
| `../scripts/launch-cos.sh` | Opens CoS in a new tab with Claude Code Remote Control on (`--remote-control CoS`), so the phone reaches it. The `com.switchboard.cos-autostart` task runs `--at-logon` once per sign-in: it starts wmux, waits for wmux to restore the CoS it had (a resumed session keeps its Remote Control), and opens a fresh CoS only when none came back live. `--if-missing`, `--dry-run`. `SWITCHBOARD_COS_AUTOSTART=off` skips the sign-in run, and `SWITCHBOARD_COS_REMOTE_CONTROL=off` drops the flag, in the environment or `local/machines/<MACHINE>.env`. |
| `../scripts/check-windows-install.py` | Windows doctor: env keys, scheduled tasks, background errors, CLIs, MCP servers, connectors, recall inputs, note links, and the newest backup with its age and size. |
| `../scripts/lib/mux.sh`, `mux.py` | Backend detection and `mux_init`; portable `mux_copy`, `mux_open`, `mux_notify`, `mux_mtime`, `mux_size`, `mux_mode`, `mux_keep_awake`. |
| `../connectors/switchboard.cmd`, `switchboard` | Connector shim used when no native `switchboard` is on PATH; `install.ps1` adds the folder to PATH in that case. |

## Install

1. Install the prerequisites from an elevated or normal PowerShell:
   `winget install --id Git.Git -e` (Git for Windows, which provides `bash.exe`),
   `winget install --id Python.Python.3.12 -e` (the `py` launcher), and wmux with
   `powershell -ExecutionPolicy Bypass -File system\windows\install-wmux.ps1` from the kit clone
   (see "The switchboard look"). Install the Amp CLI and Obsidian as usual.
   Open wmux once so `%APPDATA%\wmux\sessions\session.json` exists.
2. Clone the kit somewhere other than the destination, for example
   `git clone git@github.com:acme/switchboard-kit.git $HOME\Development\switchboard-kit`.
   The destination is your choice (suggested `%USERPROFILE%\Development\switchboard`; any folder
   or drive, Dropbox included, see the Dropbox note below) and must not exist yet or must be
   empty. Set it once: `$Switchboard = "$HOME\Development\switchboard"` (quote paths with spaces).
3. Render the kit from PowerShell. Preview first, then apply:
   `py $HOME\Development\switchboard-kit\install.py --owner-name "Your Name" --owner-email you@example.com --destination "$Switchboard" --dry-run`,
   read the six `decision:` lines, then rerun with `--apply`. Both owner flags are
   required. This writes `%USERPROFILE%\.switchboard\env` with `SWITCHBOARD_ROOT`, `SWITCHBOARD_WORKTREES_DIR`,
   `SWITCHBOARD_STATE_HOME`, `SWITCHBOARD_MUX`, `SWITCHBOARD_RUNTIME`, `SWITCHBOARD_SYNCED_SEGMENT`, and
   `SWITCHBOARD_BOARD_AUTOSTART`. The remaining steps run from the destination.
4. Run the Windows layer: `powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -SwitchboardRoot "$Switchboard" -DryRun`
   prints every change; drop `-DryRun` to apply. Without `-SwitchboardRoot` it asks where switchboard
   lives, with the same question and suggestion as `install.py`. It sets the user environment, adds
   `system\windows` (and `system\connectors` when `switchboard` is absent) to PATH, merges
   `~/.switchboard/env`, appends the `switchboard-env.sh` line to the Git Bash `~/.bashrc`,
   registers the scheduled tasks (six Amp loops, the note-link server, and the CoS sign-in auto-start, plus the claude-mem worker when Claude Code is installed), and seeds the sidebar when a session exists.
   Claude Code installed: `bash system/scripts/install-claude-code.sh` from Git Bash copies the skills,
   installs `claude-mem`, and starts the `\switchboard\com.switchboard.claude-mem` task (the worker runs
   `system/scripts/claude-mem-worker.sh`). `python system/scripts/check-claude-mem.py` reports it.
5. Open a new wmux window so the fresh environment loads. In its pane run
   `bash system/scripts/ensure-routine-workspaces.sh switchboard todo launchpad comms-triage`
   to open CoS, To Do, Launchpad, and Comms Triage. With `SWITCHBOARD_COMMS_TRIAGE=off` in `local/machines/<MACHINE>.env`
   (`python3 system/scripts/comms_triage_setting.py --set off`) that machine skips Comms Triage everywhere.
   `echo $CMUX_BIN` should print `.../system/scripts/mux/cmux`.
6. From PowerShell run `powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -Restart`.
   It quits wmux, writes pins, accent rails, and order into `session.json`, applies the
   sidebar prefs, and relaunches wmux.
7. Verify: `cmux --version` in a wmux pane answers `backend wmux`;
   `bash system/scripts/verify-kit-install.sh` passes; `CMUX_SHIM_TRACE=1 cmux list-workspaces`
   prints the wmux calls it makes.

For the WSL option, add `-Runtime wsl -Distro <name>` to `install.ps1` and `-Runtime wsl`
to `seed-session.ps1`, clone the repo inside the distro, and run the launch and verify
steps in a WSL pane.

## The switchboard look

`wmux-look.json` holds the look switchboard gives wmux, and `wmux_look.py apply` puts it in place. It was read from a working setup on wmux 2.61 and checked on a fresh wmux 2.64.3. Only appearance and layout are in it: no folders, workspace names, history, accounts, shell or agent settings.

| Setting | Value | wmux default |
|---|---|---|
| App theme and style | dark, `trace`, terminal background 88% opaque, inactive panes dimmed 20% | same |
| Terminal font | `Consolas, Menlo, Monaco, monospace`, size 19 | size 13 |
| Terminal colours and cursor | Monokai, blinking cursor (the shape stays wmux's default) | same |
| File explorer | Under the workspace list in the left sidebar (`explorerPosition: sidebar`), 53% of its height, open by default | right of the terminals, 45% |
| New workspace | One pane, no diff tab opened on its own | three panes in a grid, diff tab on |
| Sidebar rows | Left rail on the active row, status and notification lines shown, normal text size | same |
| Browser panel | Open on startup, on the right | same |
| Sidebar width | 270 px, set in `sessions\session.json` | 240 px |

wmux keeps these values in `%APPDATA%\wmux\settings.json` and reads that file at startup, so the script merges the keys there and keeps every other key. Values that match wmux's defaults are written too, so a later change of default does not change the look.

Rules the script keeps:

- **Your values stay.** A key you already set to something else is a conflict. The script lists it, writes nothing and exits 3. `--force` replaces it.
- **Backup first.** Any file it changes is copied to `<file>.switchboard-bak-<UTC time>` before the write.
- **wmux closed.** wmux rewrites both files while it runs, so the script refuses (exit 4) while that wmux is open.
- **Sidebar width needs a session.** A fresh install has no `session.json`. Start wmux once, quit it, and run `install-wmux.ps1 -SkipInstall` again. A width you dragged counts as your value.
- **Check.** `py system\windows\wmux_look.py check` reports any drift and changes nothing.
- **Try it on the side.** `-Instance <name>` (or `WMUX_INSTANCE`) writes to `%APPDATA%\wmux-<name>`, wmux's own side-by-side instance folder, and leaves your wmux alone.
- **macOS.** wmux is Windows only. On macOS the script says so and exits 0. cmux keeps its own look, and switchboard does not change cmux settings.

## cmux vs wmux sidebar parity

The macOS sidebar is a native Swift view (`system/cmux-sidebars/switchboard.swift`).
wmux has no sidebar extension point, so the kit maps each element onto what the
native wmux sidebar can show. The sidecar stays the source of truth; the shim
pushes the visible parts on every call.

| cmux sidebar element | Native wmux sidebar | How |
|---|---|---|
| Lane headers with counts (Pinned, Needs attention, Active, Workers, To review, Idle, ...) | None. The lane shows as the row's status text `Done: <lane>` when idle, plus the dot color. | The shim calls `wmux set-status --state idle --text <lane>` whenever the lane changes; wmux renders `--text` only in the idle state. |
| Pinned surfaces at the top in a fixed order | `pinned` flag plus an accent rail, listed in array order. | `seed-session.ps1` writes `pinned`, `customColor`, and the array order from the sidecar into `session.json`; wmux reads it at the next start. |
| Worker rows (lightning bolt, Workers lane) | Plain rows with the title emoji. | `dispatch.py launch` titles the tab `<emoji> <Name> <n>`; the description stays in the sidecar. |
| Needs attention (orange row, attention mark) | Violet "Needs you" with answer buttons. | The shim mirrors `set-status amp interrupted` into `wmux report-agent --blocked "Amp needs input" --surface <id>`; `--unblocked` clears it. |
| Amber unread badge | Native unread count badge on the workspace. | The shim calls `wmux notify <text>` from the pane; the count lives in the sidecar and the badge clears when the workspace is selected. |
| Active row tint and the selected highlight | Native. | wmux tints the selected row itself; `customColor` adds a 3 px rail and a 15 % tint. |
| Check and × row buttons (mark read, close) | None. | Use the shim verbs `cmux workspace-action --workspace <ref> --action mark-read\|mark-unread\|pin\|unpin\|move-top\|close-others`, `cmux close-workspace --workspace <ref>`, or the wmux context menu. |

## Limits and the fixed procedure for each

- **New panes open in PowerShell.** wmux picks `pwsh`, then `powershell`, then `cmd`
  for a pane with no shell of its own, so switchboard scripts and the `~/.bashrc` hook
  would never run. Procedure: the shim passes `--shell bash.exe` on every
  `new-workspace`, and `seed-session.ps1` sets `wmux-workspace-prefs.defaultShell`
  to `bash.exe` and repoints the fixed-surface rows in `session.json` (a restored
  row reopens with the shell frozen in it). `bash.exe` is the bare alias wmux
  resolves at Git's install root with `--login -i`, never the WSL launcher in
  System32. Override with `SWITCHBOARD_WMUX_SHELL=<spec>` in `~/.switchboard/env`;
  `SWITCHBOARD_WMUX_SHELL=default` keeps wmux's own choice. Surfaces opened by hand
  from the wmux "+" button follow the same default once the pref is set.
- **Tab order.** wmux cannot order tabs, so switchboard leaves the wmux order alone; the three-section order in `system/scripts/sidebar_order.py` is cmux only.
- **Set color or pin.** wmux 2.49.0 has no CLI or IPC for `customColor` or
  `pinned`, and `pinned` has no visual effect on its own. Procedure: the shim
  writes the change to the sidecar (`workspace-action pin|unpin|set-color`), then
  run `seed-session.ps1 -Restart` to push it into `session.json` and relaunch wmux.
  The move verbs (`move-top`, `reorder-workspace`) only touch the sidecar, and
  `seed-session.ps1` no longer applies them.
- **Read unread counts.** wmux exposes no unread count to the CLI. Procedure: the
  sidecar holds `unread` per workspace; `cmux sidebar lanes --json` prints it; the
  native badge is the result of the shim's `wmux notify` calls.
- **No event stream.** wmux offers no subscription for workspace changes. Procedure:
  the shim refreshes `wmux list-workspaces` and pushes status on each call it serves,
  so the sidebar lags by one script call, never by a timer.
- **No custom sidebar.** The native rows plus the sidebar prefs are the surface.
  Procedure: `seed-session.ps1` sets `activeTabIndicator=leftRail`,
  `showNotificationMessage=true`, `hideAllDetails=false` in `%APPDATA%\wmux\settings.json`.
  The web board is opt-in: set `SWITCHBOARD_BOARD_AUTOSTART=1` in `~/.switchboard/env` and run
  `python system/scripts/mux/switchboard-board.py open`. The default is off because the
  sidebar must not load in a browser.
- **Status text while running.** wmux shows only the colored dot for `running` and
  `interrupted`; `--text` appears only as `Done: <text>` when idle. Procedure: the
  shim keeps the lane label on the idle text and relies on the dot otherwise.
- **`list-workspaces` has no metadata.** wmux returns id, title, cwd, and active
  flag only. Procedure: description, refs, pin, color, lane, unread, and thread ids
  come from the sidecar; `cmux tree` and `rpc workspace.list` merge the two.

## Keeping the machine awake overnight

On machines with Modern Standby, standby starts as soon as the display turns off, so a
`SetThreadExecutionState` hold with only `ES_SYSTEM_REQUIRED` does not keep workers running:
Windows reaches its sleep phase a while after the screen goes off.
`pwsh -NoProfile -File system/windows/keep-awake.ps1 -Hours 10` also holds the display, so
standby never starts and no power setting changes. The screen stays on until the hold ends.
`-Status` and `-Stop` check and end it; one holder runs at a time; state and log are in `.state/`.

Dispatch now holds it on its own, so the machine stays awake while workers run.
`system/scripts/keep_awake.py` runs at every worker launch and at the end of every
`dispatch.py sweep`: while any worker holds a slot, or a long job holds a lease
(`keep_awake.py lease --name <job> --minutes <n>`), `keep-awake.ps1 -Ensure` starts the hold or
moves its end to one hour out, in place, with one holder. Once nothing is live, the next tick runs
`-Release -Owner dispatch`, which never ends a hold started by hand. The screen stays on while
work runs; that is expected. `SWITCHBOARD_KEEP_AWAKE=off` turns it off. macOS does nothing here.

Only a worker that is working holds it. The sweep counts a slot-holding worker only while its Claude transcript,
a sub-agent file, a background task's output, or its heartbeat changed in the last 20 minutes
(`SWITCHBOARD_KEEP_AWAKE_QUIET_MINUTES`). A worker parked on a question to the owner, or idle in its
tab, lets the laptop sleep; the sweep output lists it under `keep_awake.idle_workers`. Idle CoS
and idle wmux never hold it: wmux's own keep-awake is `smart`, a system-only hold while an agent
pane is working, which never keeps the screen on.

To name another app that keeps the screen on, `system/windows/log-power-requests.ps1 -Install`
(admin PowerShell) copies the script to the admin-only folder `C:\ProgramData\switchboard\power-log`
and registers a SYSTEM task that runs that copy, appending the `powercfg /requests` holders to
`power-requests.log` there every 10 minutes while the laptop is awake. The task never runs the
repo copy, because the repo is user-writable and a SYSTEM task must not run a user-writable
script. Rerun `-Install` after editing the script. It changes no power setting and never wakes
the laptop. `-Status` and `-Uninstall` check and remove it; any prompt can read the log.

## Dropbox and synced folders

The repo may sit in Dropbox or another synced folder. `install.py` records the
synced segment in `SWITCHBOARD_SYNCED_SEGMENT` and relocates what sync clients corrupt:
worktrees go to `SWITCHBOARD_WORKTREES_DIR` (default `%LOCALAPPDATA%\switchboard\worktrees`)
and fast-changing state (sidecar, session store, connector tokens, board log, env
file) goes to `%USERPROFILE%\.switchboard`, recorded as `SWITCHBOARD_STATE_HOME`. Only the
sidecar follows an override of that variable; tokens move with `SWITCHBOARD_CONNECTORS_DIR`
and the board log with `SWITCHBOARD_BOARD_LOG`. `switchboard-env.sh` exports the variables in
every shell and `seed-session.ps1` reads the same env file. Run one device per
synced checkout: two machines sharing one Dropbox checkout would fight over
`.git` and the ledgers.

## Honest limits

- The scripts in this folder were built from the wmux 2.49.0 source, its CLI help,
  and the shim's unit tests (`system/scripts/test_cmux_shim.py` with a fake wmux
  binary). They have **not** been run on a Windows machine yet.
- First-run checks on the first Windows install: `seed-session.ps1 -DryRun` (shows
  the session and prefs changes without writing), then
  `CMUX_SHIM_TRACE=1 cmux list-workspaces` (prints every wmux call and the JSON it
  returns). Report any shape mismatch against `session.json` (`version: 1`) or the
  `list-workspaces` payload.
- `register-scheduled-tasks.ps1` hosts `bash.exe` under `conhost.exe --headless` to
  avoid a console flash; if that flag is missing on an older Windows 10 build, rerun
  with `-NoHeadless`.
- `seed-session.ps1 -Restart` waits up to 10 s for wmux to quit after
  `CloseMainWindow()` and up to 15 s for `session.json` to be rewritten; a forced
  `Stop-Process` skips wmux's final write, so the seed then applies to the last
  30 s checkpoint.
