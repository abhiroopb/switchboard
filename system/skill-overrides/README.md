# Skill Overrides

Tracked copies of locally-modified files inside skills installed from an upstream
skill catalog (a reinstall from that catalog overwrites `~/.agents/skills/<skill>/`).

Any local behavior change to an upstream-installed skill file MUST be captured here,
or it will be silently lost on the next reinstall. This replaces ad-hoc
string-replacement patch scripts for files whose diffs are too large to patch
reliably (e.g. `todo_cli.py` concurrency hardening).

## Layout

Paths mirror `~/.agents/skills/`:

```
system/skill-overrides/
  todo/scripts/todo_cli.py       → ~/.agents/skills/todo/scripts/todo_cli.py
  todo/references/schema.md      → ~/.agents/skills/todo/references/schema.md
```

Skills that became fully repo-owned (canonical copies in
`.agents/skills/` in switchboard, copied whole into `~/.claude/skills` by `system/scripts/sync-skills.py`)
must NOT keep partial overrides here: a stale override silently reverts the
canonical copy when this sync runs standalone. Removed for that reason:
`chief-of-staff`, `drafting-docs`.


## Usage

```bash
# Install tracked overrides into the runtime skill tree (idempotent):
bash system/scripts/sync-skill-overrides.sh

# Report drift without writing anything:
bash system/scripts/sync-skill-overrides.sh --check

# Pull runtime edits back into the tracked tree (after editing runtime files):
bash system/scripts/sync-skill-overrides.sh --capture
```

`~/bin/install-amp-skills.sh` runs the install direction after reinstalling
core skills, so overrides survive reinstalls.

## Upstream drift

When a reinstall from the upstream catalog brings a new version, the override
replaces it wholesale. If upstream shipped changes you want, re-derive: diff
upstream vs the override, merge, then `--capture`. The sync script warns when
the runtime file it is about to overwrite differs from both the override and
the last-seen state, so upstream changes don't vanish silently.
