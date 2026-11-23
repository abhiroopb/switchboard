# Organisation profiles

switchboard core is organization-neutral. Anything specific to one organization lives in `profiles/<name>/` and is switched on by one script, so one checkout installs cleanly on a work laptop and on a personal machine with nothing bleeding across. The kit ships only the `personal` profile. A `company` profile is an optional overlay an organization writes for itself and keeps outside the kit.

| Profile | Picked when | What it adds |
|---|---|---|
| `personal` | the default | nothing; core files stay as shipped, connectors go through the `switchboard-shim` (`profiles/personal/CONNECTORS.md`) |
| `company` | `system/connectors/company_cli.py has_company_cli()` finds your organization's own CLI | the organization's own rules, skills, scripts, catalog entries, router maps and MCP servers, kept in its own `profiles/company/` |

## How it works

```
python3 system/scripts/apply-profile.py                     auto: company when a different switchboard CLI is on PATH, else personal
python3 system/scripts/apply-profile.py --profile company     force a profile
python3 system/scripts/apply-profile.py --check             exit 1 when the checkout drifted from the recorded profile
python3 system/scripts/apply-profile.py --dry-run           print the plan only
```

`install-all.sh` step 1 and the kit `install.py` both run it, so a fresh install never needs it by hand.

1. **Overlay files.** Every file under `profiles/<name>/overlay/` is copied over the same path in the checkout. Before a path is owned, the core copy is backed up to `.switchboard-kit/profile-backup/` (always, even when the bytes match). Switching away restores each core file from that backup, or deletes the path when core had no file there.
2. **Instruction sections.** `AGENTS.md` and `adapters/SYSTEM.md` end with an empty marker pair:
   ```
   <!-- switchboard:profile-section:start (filled by system/scripts/apply-profile.py; edit profiles/<name>/ instead) -->
   <!-- switchboard:profile-section:end -->
   ```
   The script fills it from `profiles/<name>/agents-section.md` and `system-section.md`. The personal profile leaves it empty.
3. **Config layers.** Core configs stay generic, and an optional profile layer merges over them at read time:
   - `system/config/router.profile.yaml` adds to `router.yaml`. A company profile can set, for example, `quiet_done_workstreams`. Readers: `dispatch.py config()`, `report-to-cos.sh`, `cos_review_feed.py`.
   - `system/config/claude-skills-manifest.profile.yaml` adds to `claude-skills-manifest.yaml`, for the organization's own skills and catalog entries. Reader: `install-claude-skills.py`.
   - `system/prompts/profile-dispatch-core.md` and `profile-worker-clauses.md` are appended to worker prompts by `dispatch.py profile_worker_clauses()`, with `{repo}` rendered as a forward-slash path.
4. **Tick jobs.** A profile job that runs on every command-center tick is one script at `profiles/<name>/overlay/system/scripts/profile-tick.d/<job>.sh`. Core `command-center-loop.sh` calls `run-profile-tick-hooks.sh` only when that folder exists, so on any other machine the job does not exist and cannot run. Each hook gets `REPO_ROOT` and `STATE_DIR` and keeps its own gates, kill switch and log. The personal profile has none.
5. **Env.** `profiles/<name>/profile.json` `env` keys are written to `~/.switchboard/env` together with `SWITCHBOARD_PROFILE=<name>`. Empty values are dropped, and the file is written with LF line endings. `system/windows/switchboard-env.sh` exports them on Windows. `install-claude-code.sh` adds a profile's own MCP servers only when that profile is active.
6. **State.** `.switchboard-kit/profile.json` records the active profile and the files it owns. It and `profile-backup/` are gitignored.

## Owner and machine settings

Three settings belong to the owner or to one machine, not to a profile. `profiles/` ships whole in the kit, so anything under `profiles/personal/` reaches every personal install, and an organization profile never applies on a machine running `personal`. They live in `local/` in the main folder. `local/` is gitignored, skipped by the kit build, refused by `dispatch.py auto-push` if it is ever tracked, and absent from `.switchboard-kit/install.json`, so no kit, commit, or push carries it. Worktrees get a link to it, like `people/`.

- **Owner context:** `local/agents-local.md` holds the employer, team, product area, and mission. When the file exists, `adapters/install-adapters.sh --apply` adds it to the home adapters: an `@` import in `~/.claude/CLAUDE.md` and a read pointer in `~/AGENTS.md` and `~/.codex/AGENTS.md`. Keep this content out of `AGENTS.md`, which ships in the kit and fails `check-profile-leaks.py` on terms from your deny list. Rerun the adapter install after you create the file. `--no-skills` writes the adapters without recopying skills.
- **Comms Triage off:** `python3 system/scripts/comms_triage_setting.py --set off` writes `SWITCHBOARD_COMMS_TRIAGE=off` to `local/machines/<MACHINE>.env`, and `--set on` removes it. `<MACHINE>` is the host name up to its first dot, in upper case. A machine with no file behaves as before. While it is off, `ensure-routine-workspaces.sh` and `cmux-helpers.sh launch` skip the tab, `arrange_workspace_sidebar` and `seed-session.ps1` skip the pin, `start-day.sh` skips the comms collector, `rebuild-switchboard-state.py` drops the queue card, and `command-center-digest.py` and `next-up-digest.py` leave it out. The feature stays in the kit. `SWITCHBOARD_COMMS_TRIAGE=on` in one command's environment overrides it for that process.
- **Judgment profile:** `local/user-lens.yaml` is optional and is read by the `checking-owner-lens` skill. Copy `templates/user-lens.yaml.example` to start one. With no profile, the skill applies the `AGENTS.md` writing rules and neutral defaults.

**Synced folders.** A checkout in a synced folder (Dropbox, OneDrive, iCloud Drive) syncs `local/` like any other folder, because the sync client does not read `.gitignore`. That is why the Comms Triage file is named for the machine: another device that sees the folder finds no file with its name and stays on. A second device reads `local/agents-local.md` only after its own adapter install runs, and the role line is about the owner, so it holds on both. A checkout outside a synced folder never sees `local/`, and git never carries it. A new install starts with Comms Triage on and no owner context.

The env file at `~/.switchboard/env` stays outside the repo: it holds `SWITCHBOARD_ROOT` and the tool paths, so it is what finds the repo in the first place. `switchboard-env.sh` skips `SWITCHBOARD_COMMS_TRIAGE` when it exports that file, so a copy brought back by an old backup changes nothing.

## Leak check

`python3 system/scripts/check-profile-leaks.py` scans the checkout for organization-only terms. The terms live in the gitignored deny list `local/kit-deny-list.txt`, never in the script, so the script ships in the kit without naming the company. `--deny-list PATH` or `SWITCHBOARD_KIT_DENY_LIST` overrides the default. The file format is in the script's docstring.

- A hit in an instruction file (AGENTS.md, adapters, skills, prompts, sub-agents) is an error, and the script exits 1.
- A hit anywhere else is a warning: comments, test fixtures, vendored skills.
- `--kit` gates a kit build: every hit anywhere is an error, and a run with no deny list fails closed.

`install-all.sh` runs the check after every install.

## Adding or editing organization content

- **New organization-only rule:** edit `profiles/company/agents-section.md` (or `system-section.md`) and rerun `apply-profile.py`. Never put it in core `AGENTS.md` outside the markers.
- **New organization-only skill, script or prompt:** add it under `profiles/company/overlay/<same path>`.
- **A core file that needs an organization variant:** put that version at `profiles/company/overlay/<path>`. Core keeps the generic version.
- **Your own organization:** copy `profiles/personal/` to `profiles/company/`, fill `agents-section.md`, the overlay and `profile.json`, and rerun `apply-profile.py`. Auto-detection picks `company` when a different `switchboard` CLI is already on PATH. Any other name needs `--profile` and an entry in `apply-profile.py` `KNOWN`.

## Sharing a copy that has an organization profile

A checkout with an organization profile applied holds that organization's content in core paths. Before building a shareable copy:

1. Run `python3 system/scripts/apply-profile.py --profile personal` so core paths hold the generic versions again.
2. Run `python3 system/scripts/check-profile-leaks.py` and fix any errors.
3. Build the kit without your organization profile, then run `check-profile-leaks.py --kit --root <built tree>` on it.
4. Run `apply-profile.py --profile company` to restore the organization setup.

Switching personal to company to personal leaves `git status` clean.

## Switching agents (Claude Code and Amp)

Both harnesses are installed by `install-all.sh`, so switching is one setting:

```
bash system/scripts/set-agent.sh claude     # or amp; no argument prints the current agent and its source
```

It writes `agent:` in `system/config/router.yaml`, which `switchboard_agent.py` resolves for routine tabs, workers, start-day and daemon jobs. `SWITCHBOARD_AGENT=amp` in one command's environment overrides it for that process only. Tabs that are already open keep their agent.
