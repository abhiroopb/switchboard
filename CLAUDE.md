@AGENTS.md
@system/behavior-map.md

# Claude adapter for switchboard

This file keeps Claude Code aligned with the repo-owned switchboard control plane.

## Purpose
- Import shared switchboard policy for this repo.
- Keep Claude-specific repo notes small and explicit.
- Avoid redefining shared workflow logic here.

## Claude-specific notes
- Use this file for Claude Code adapter behavior that is specific to this repo.
- Put shared switchboard behavior in `AGENTS.md`, not here.
- Put deterministic behavior in scripts, routines, and state rebuilders, not prompt text.
- Before changing instruction surfaces, run `python3 system/scripts/check-instruction-drift.py`.

## Local runtime surfaces
- `~/.claude/CLAUDE.md` is a personal and runtime adapter surface.
- `~/.claude/settings.json`, `~/.claude/settings.local.json`, and Claude hooks are runtime configuration, not the canonical source of switchboard workflow policy.
- If shared behavior needs to change, update the repo-owned surface first and then sync adapters.
