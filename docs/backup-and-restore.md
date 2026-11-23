# Backup and restore

switchboard backs up `~/.claude`, `~/.switchboard` and the owner files (`local/agents-local.md`, `local/user-lens.yaml`, `local/machines/`, `local/skills/`) every night into `local/backups/<hostname>/<date>/` inside the switchboard folder. `local/` is gitignored and left out of the kit. When the switchboard folder sits in a synced folder (Dropbox, OneDrive, iCloud Drive), the copy leaves the machine on its own. Each machine writes under its own hostname, so a Windows laptop and a Mac never collide.

In a synced folder these copies live beside the files they protect, so they do not protect against losing that sync account.

```
local/backups/<hostname>/<YYYY-MM-DD>/
    manifest.json    file counts, sizes, and a SHA-256 for every file and archive
    LEFT-OUT.txt     what stayed out, why, and how to get each one back
    claude.zip       ~/.claude
    switchboard.zip        ~/.switchboard
    owner.zip        local/agents-local.md, local/user-lens.yaml, local/machines/, local/skills/
    repo.bundle      git bundle of all refs, because a sync client should not carry .git
```

## Commands

Every command runs from the switchboard folder.

| To do this | Run |
|---|---|
| Restore the newest backup | `python3 system/scripts/local_backup.py restore --apply` |
| Preview a restore, changing nothing | `python3 system/scripts/local_backup.py restore` |
| Restore a chosen day | `python3 system/scripts/local_backup.py restore --backup 2026-01-15 --apply` |
| Bring commit history back into a folder with no `.git` | `python3 system/scripts/local_backup.py restore-git --into "<switchboard folder>"` |
| Clone the history somewhere new | `git clone local/backups/<hostname>/<date>/repo.bundle <new folder>` |
| Take a backup now | `python3 system/scripts/local_backup.py run` |
| List backups and sizes | `python3 system/scripts/local_backup.py list` |
| Re-check the newest backup | `python3 system/scripts/local_backup.py verify` |

Use `python` or `py` on Windows PowerShell. `python3` works in Git Bash.

A restore is safe to rerun:

- **Newer files stay.** A local file newer than the backup copy, or the same age, is kept. It is listed in the preview.
- **Older files are saved first.** A replaced file goes to `.pre-restore-<stamp>/` in the same root. Under `~/.claude` and `~/.switchboard` that folder stays local and never syncs. For an owner file it is `local/.pre-restore-<stamp>/`, which a sync client does carry.
- **The env file merges.** `~/.switchboard/env` keeps every key only the local file has, including secret lines the backup left out. Shared keys take the backup's value.
- **Links stay as they are.** A restore never writes through a link or junction, and it does not recreate links. Both are listed in its output.
- **Bad copies are refused.** The whole backup is checked against its manifest first, and each file again as it is written.

## Schedule and retention

- **Schedule.** `com.switchboard.local-backup` runs at 02:30 local time, through Task Scheduler on Windows and launchd on a Mac. A laptop that was asleep runs it at wake.
- **Retention.** It keeps the 14 newest daily copies, then the newest copy of each of the 8 weeks before them. Pruning runs only after a new backup passes verification, and only inside this machine's folder.
- **Alerts.** A failed run sends CoS one urgent line. The `local-backup-health` background task checks every 6 hours and sends the same line when the newest copy is over 48 hours old. An unresolved problem repeats at most once a day.
- **Doctor.** `check-windows-install.py` shows the newest copy, its age, the copy count and the disk used.
- **Processes.** One Python process plus four git calls, with no per-file forks.
- **Refusals.** A run that finds no files, a missing folder, or a folder under half its last file count publishes nothing and alerts CoS, so one bad night cannot push out good copies. `run --allow-shrink` overrides this after a deliberate cleanup.
- **Partial copies.** When only the git bundle fails, the verified copy of `~/.claude`, `~/.switchboard` and the owner files is still published, with no `repo.bundle`. Its manifest says `"partial": true`, `list` marks it `PARTIAL`, and the run still counts as failed and alerts CoS. It prunes nothing, and a later run the same day replaces it with a full copy.
- **Sleep.** A laptop in Modern Standby can start the job in a wake of a few seconds and freeze it for hours. Task Scheduler's 2-hour limit counts that frozen time, so at wake it kills the job's console. The backup gives each `git` call its own hidden console, so the run still finishes. The history scan stops a `git log` that runs over 30 minutes and retries once after 20 seconds. A run that took over an hour says the machine probably slept, in its warnings or its error.

## What stays out

Credentials never enter a backup. `LEFT-OUT.txt` in each backup lists exactly what stayed out of that one, with no secret values.

| Left out | How to get it back |
|---|---|
| `~/.claude/.credentials.json` | Run `claude`, then `/login`. |
| `~/.switchboard/connectors/google-token.json` | `switchboard login google`. |
| `~/.switchboard/connectors/google-oauth-client.json` | Download the OAuth client JSON from Google Cloud Console, save it there, then `switchboard login google`. |
| Other `~/.switchboard/connectors/` files | `switchboard login slack`, or the matching service's sign-in. |
| Secret-looking lines in `~/.switchboard/env` | Add `KEY=VALUE` back by hand. The rest of the file is kept. |
| Files with a credential name (`*.pem`, `.env`, `auth.json`, `*token*.json` and similar) | Copy from the source, or issue a new one. |
| Any non-transcript file matching a token pattern | Remove the token, then copy it by hand. |
| `~/.claude/shell-snapshots` | Nothing to do. They can hold exported secrets, and Claude Code writes new ones. |
| `~/.claude/plugins/marketplaces`, `plugins/cache` | `bash system/scripts/install-claude-code.sh` downloads them again. |
| `~/.claude/security`, `cache`, `sessions`, `telemetry`, `session-env`, `state`, `backups`; `~/.switchboard/logs`, `wmux-*.json` | Nothing to do. They are recreated as things run. |
| `~/.claude/file-history` | Not recoverable: per-session edit undo history. |
| `~/.switchboard/secure` | It stays on this machine and out of any synced folder. See the secure folder below. |

**Secure folder.** `%USERPROFILE%\.switchboard\secure` holds files encrypted with Windows DPAPI for this Windows account, with an ACL limited to that account. No backup opens or copies it, so losing this laptop or this Windows profile loses what is in it. To put a stored file on the clipboard, run this in PowerShell, then clear the clipboard after pasting:

```powershell
Add-Type -AssemblyName System.Security; [Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes("$env:USERPROFILE\.switchboard\secure\<name>.dpapi"), $null, 'CurrentUser')) | Set-Clipboard
```

Credential files outside these folders (for example `~/.claude.json` or `~/.config/<tool>/credentials.json`) are never read.

**Owner files.** `owner.zip` holds `local/agents-local.md`, `local/user-lens.yaml` when it exists, and `local/machines/`. Nothing else under `local/` is opened, so `local/backups` and `local/verify-logs` never go into a backup. These files get the same token scan as every other file. A file with a hit, or a file under `machines/` with a secret-looking key, stays out whole and is named in `LEFT-OUT.txt` and in the run's warnings. A named file that is gone since the last backup is a warning too. Warnings stay in `system/state/local-backup.json` until the next run and send no CoS line. A restore with `--home` writes them to `<home>/switchboard-local`, never to the real `local/`.

**Git history.** A bundle cannot be redacted, so every run scans all refs and stashes for token shapes. A hit withholds `repo.bundle` from that night's copy and alerts CoS, naming the commit, file and kind. Fix the history, or set `SWITCHBOARD_BACKUP_ALLOW_HISTORY=on` in `~/.switchboard/env` for a false alarm. `HISTORY_FALSE_ALARMS` in `local_backup.py` pins one reviewed exception by commit, path and kind: fake key headers in the backup's own test file.

**Transcripts.** Session transcripts are kept, with anything matching a token pattern replaced by `[REDACTED:<kind>]`. A run of 12 or more plain lowercase words that reads like a recovery phrase, and anything shaped like an SSN, is replaced too. A saved tool output holding such a run is left out whole. The scan knows shapes, so a short password typed as plain words in a chat stays in the transcript and reaches the synced folder. To leave transcripts out, add `SWITCHBOARD_BACKUP_TRANSCRIPTS=off` to `~/.switchboard/env`. Memory files under `projects/` are still backed up.

## What a restore cannot bring back

- **Sign-ins and tokens.** Claude Code login, Google, Slack and other connector tokens, the Figma token, and anything in `secrets.env` are issued again by each service.
- **`~/.claude.json`.** The MCP server list, account record and project trust prompts live there. `install-claude-code.sh` re-registers the MCP servers.
- **Undo and session state.** Claude Code undo history, shell snapshots and session state are gone. Plugin caches download again.
- **Redacted text.** A token replaced in a transcript stays `[REDACTED:<kind>]` in the restored copy.
- **Recent work.** Anything changed after the last backup ran, up to about a day, is not in it.
- **Project memory after a move.** Claude Code keys project memory by folder path. If switchboard moves, rename the matching folder under `~/.claude/projects`, or that project's memory starts empty.
- **Machine setup.** Scheduled tasks, launchd agents, PATH edits and installed programs live outside these folders. `install-all.sh` rebuilds them.
- **The rest of the repo.** `people/`, `notes/`, `workstreams/` and uncommitted files are not in this backup, and neither is anything else in `local/` beyond the owner files. A synced folder carries those files, and the bundle carries committed history only.

## During a kit install

`install.py` runs this step on Windows and on a Mac:

1. **Find.** It looks in `<destination>/local/backups`, then the folder recorded in `~/.switchboard/env`, then a Dropbox copy of the switchboard folder when Dropbox is installed (found through Dropbox's own `info.json`). Nothing found prints `backup: none found. Looked in: ...` with each folder.
2. **Check.** It takes this machine's newest copy first, then the newest from the same OS, then any other. Each copy is verified member by member, and a copy that fails falls through to the next.
3. **Show.** It prints the date, the manifest result, and how many files a restore would add, replace, keep and skip.
4. **Ask.** A terminal gets `restore it now? [y/N]`. With no terminal it restores nothing and prints the one-line command. An installing agent asks the owner, then reruns with `--restore-backup yes`. `--restore-backup no` skips the question. `--update` is a routine refresh, so it only reports unless you pass `--restore-backup yes`.

The step runs after the kit validates and before any file is written, so restored settings exist when the installer merges its own keys into `~/.switchboard/env`. A backup from another OS skips files full of that machine's paths: `env`, `settings.json`, `CLAUDE.md` and the plugin lists. Owner files restore into `<destination>/local` from any OS, because a machine file only applies on the machine it names.

After a restore, run `bash system/scripts/install-all.sh` as usual, then sign in to each service using `LEFT-OUT.txt`.

## Protection for local/

- **Install.** `install.py` never writes, updates or removes a path under `local/`, and it refuses a plan that names one. The one exception is a backup restore you approve, which brings the owner files back and never over a newer file. A folder holding only `local/` counts as empty for `--apply`.
- **Cleanup.** `test_local_backup.py` scans the install and cleanup scripts and fails if any delete command names `local/`.
- **Git.** `/local/` is in `.gitignore`, and the installer adds it to an older copy, so `git add --all` skips it and `git clean` without `-x` leaves it. No switchboard script runs `git clean -x`.
- **Synced folders.** `local/` is not on a sync client's ignore list, because syncing is how a copy leaves the machine. It stays in the same sync account as everything else.
