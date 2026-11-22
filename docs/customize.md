# Make it yours

switchboard is meant to be edited. Every behavior lives in a file, and the agents follow the files.

## Tell it who you are

- **Personalize.** Fill the `personalize here` blocks in `AGENTS.md` and `system/chief-of-staff-prompt.md`: your role, your team, what a good day looks like, and what you never want an agent to do.
- **Private context.** Put your employer, team and mission in `local/agents-local.md`, then run `bash adapters/install-adapters.sh --apply`. That file is gitignored, so it never leaves your machine, and every agent session still imports it.
- **People.** Add a profile per person you work with in `people/<first-last>.md` from `people/_template.md`. Drafts for that person read it first.
- **Judgment.** Copy `templates/user-lens.yaml.example` to `local/user-lens.yaml` and describe how you judge good work. The `checking-owner-lens` skill uses it as a final check.

## Add work

- **Workstreams.** Dispatch work on a new topic and CoS creates the workstream, or run `python3 system/scripts/new-workstream.py "<what the work is>"`. Edit its `CONTEXT.md` as the work changes.
- **Recurring work.** Machine routines go in `system/scheduled-jobs.yaml` and `system/background-tasks.yaml`. Human commitments go on the to-do list.
- **Channels to watch.** List the Slack channels and email senders that matter in `system/config/priority-channels.yaml`.

## Add skills

- **Your own.** Create `.agents/skills/<name>/SKILL.md` with `name:` and `description:` frontmatter, then rerun `bash adapters/install-adapters.sh --apply` so every agent sees it.
- **Third-party.** Copy it into `system/vendor/skills/<name>/` with its license as `LICENSE.upstream` and a `SOURCE.md` naming the repository and commit, and list it in `system/config/claude-skills-manifest.yaml`.
- **Private.** Keep skills that should never be shared in `local/skills/`, which is gitignored.

## Change the rules

- **Edit the source.** `AGENTS.md` is the single source of policy. Keep it under the size `system/scripts/test_agents_md_size.py` allows, and move detail into `docs/runbooks/agents-md-detail.md`.
- **Prefer code to prompts.** A rule that must always hold belongs in a script or hook, not in prompt text. See `system/behavior-map.md`.
- **Check for drift.** Run `python3 system/scripts/check-instruction-drift.py` after editing instruction files.

## An organization profile

If you use switchboard at work, keep company-specific rules, skills and tools out of the core files. Create `profiles/<company>/` with a `profile.json`, an `agents-section.md` and an `overlay/` of files that replace core files on that machine, then run `python3 system/scripts/apply-profile.py --profile <company>`. Switching back to `personal` restores the core files. `docs/profiles.md` has the details.

## Share your setup

Never push your install folder: it holds your notes and history. `system/kit/build_tier.py` builds a filtered copy instead. It leaves out your folders and anything your owner-local rules name, merges each new change into the last published copy, and refuses to write anything that fails its gates. `system/kit/README.md` explains it.
