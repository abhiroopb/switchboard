# Using PM OS outside Amp

PM OS was built in Amp with cmux, and the full loop (Chief of Staff tab, dispatcher, worker tabs, command center, plugins) still needs them. The *policy* and the *skills* do not. This folder is the bridge: one source of truth (`AGENTS.md` and `.agents/skills/*/SKILL.md`), loaded into each harness the way that harness expects.

Installing from one of these harnesses is the `policy-and-skills` scope in [docs/install-decisions.md](../docs/install-decisions.md). That file fixes the location, scope, Windows runtime, optional organization tools, and Windows-sidebar answers; an installing agent applies it and asks nothing.

Nothing in here is hand-written prose copied from `AGENTS.md`. `SYSTEM.md`, `SKILLS.md` and `PORTABILITY.md` are generated copies of `AGENTS.md` and the skill frontmatter. Refresh them when those change. The other adapter files import or point at `AGENTS.md`; they never restate it.

## What loads where

| Layer | Amp | Claude Code | Codex CLI | Cursor | ChatGPT and other chat tools |
|---|---|---|---|---|---|
| Policy (`AGENTS.md`) | native | `CLAUDE.md` imports `@AGENTS.md`; `~/.claude/CLAUDE.md` managed block for other directories | native at project root; `~/.codex/AGENTS.md` managed block for other directories; `project_doc_max_bytes = 65536` so it is not truncated | native at project root; `.cursor/rules/pm-os.mdc` (always applied) points at it | paste `adapters/SYSTEM.md` (the harness-neutral sections, under 4k words) |
| Behavior map (`system/behavior-map.md`) | native via `AGENTS.md` | imported by `CLAUDE.md` | referenced from the managed block | referenced from the rule | not needed |
| Skills (`.agents/skills/*/SKILL.md`) | reads the same copies in `~/.claude/skills/<name>` (and `.agents/skills/` inside the repo) | copies in `~/.claude/skills/<name>`, listed in `system/config/claude-skills-manifest.yaml`, written by `system/scripts/install-claude-skills.py` and kept current by `system/scripts/sync-skills.py` (never symlinks) | native from `.agents/skills/` in the repo; `sync-skills.py` keeps copies in `~/.agents/skills/` where `codex` is on PATH | no loader; `pm-os.mdc` tells it to open the matching `SKILL.md` | no loader; pick from `adapters/SKILLS.md` and paste the body |
| Guardrails (git add -A, force push, rm -rf, bot prefix) | `pm-os-guardrails` plugin (all rules) | `pm-os@pm-os-local` plugin (all rules; its hooks run the Amp plugin modules through Bun in every directory) plus the `.claude/settings.json` PreToolUse hook `.claude/hooks/pm-os-guardrails.py` (rules 1-4, inside the repo, no Bun needed) | by convention (AGENTS.md); Codex has no pre-tool hook | by convention (rule text in `pm-os.mdc`) | by convention |
| Dispatch, command center, workspace lifecycle | plugins + cmux | not available; run tasks in your own terminal | not available | not available | not available |
| Memory | `amp-mem` plugin + SQLite | `claude-mem` (pinned version, telemetry off) with its worker run by launchd, systemd, or Task Scheduler; installed by `system/scripts/install-claude-code.sh`, checked by `system/scripts/check-claude-mem.py` | not available; use `notes/` and workstream `CONTEXT.md` | same | same |

`PORTABILITY.md` lists every file in the kit under one of four classes (`portable`, `adapter`, `amp-only`, `cmux-only`) with the reason; `portability.json` is the same data for tooling.

## Install

Project-level adapters ship in the repo and need nothing: `CLAUDE.md`, `.claude/settings.json`, `.claude/hooks/`, `.codex/config.toml`, `.cursor/rules/pm-os.mdc`.

Home-level pieces (policy for other directories, Claude skill copies, the Codex byte cap) are written by:

```
bash adapters/install-adapters.sh           # dry run
bash adapters/install-adapters.sh --apply
```

It appends or replaces a block delimited by `<!-- pm-os-kit:start -->` / `<!-- pm-os-kit:end -->` in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, adds `project_doc_max_bytes` to `~/.codex/config.toml` only if that key is absent, and copies the manifest skills into `~/.claude/skills/` through `install-claude-skills.py`. Text outside the block is never touched. Skills are copies, never symlinks, so a synced or Windows checkout works the same way.

After that first install, nobody runs an installer to publish a skill change. `~/.claude/skills` is output only, and both Claude Code and Amp read it. Edit the source (`.agents/skills/<name>` through a worker branch, `local/skills/<name>` for a private skill) and `system/scripts/sync-skills.py` copies it: right after the branch lands on `main`, and on every command-center tick (every 10 minutes). A running Claude session sees the change on its next turn; Amp sees it in the next new thread. The sync always reads the main checkout, applies the tracked `todo` override first, saves an edited installed copy to gitignored `local/skill-edits/` before replacing it, refuses to delete more than 5 copies at once, and leaves `synced/` (Claude's account sync) and `wmux/` alone. It sends CoS one line for an edit it saved, a folder dropped straight into `~/.claude/skills`, or three failed runs in a row. Kill switch: `SKILL_SYNC_DISABLED=1`.

Claude Code has more than the policy: memory (`claude-mem`), plugins (including the PM OS plugin `pm-os@pm-os-local` from `system/claude-marketplace`), and MCP servers. One script does all of it, on macOS, Linux, and Windows (Git Bash), and skips a step with a one-line note when its tool is missing:

```
bash system/scripts/install-claude-code.sh --dry-run
bash system/scripts/install-claude-code.sh
python3 system/scripts/check-claude-mem.py      # memory worker health
```

Run it after `install-adapters.sh --apply`, and again after every kit `--update`. It needs `claude` on PATH. Memory and the PM OS plugin hooks need `bun`. Memory also needs `npx`. The worker is registered with launchd (macOS), a systemd user service (Linux), or Task Scheduler (Windows), and `system/background-tasks.yaml` checks it once a day.

## Verify the policy loaded

Claude Code, from the PM OS checkout:

```
claude -p "Which instruction files are loaded right now, and what is the bot attribution prefix they require?"
```

Then in a session ask it to run `git add -A`; the hook denies with `Blocked by pm-os-guardrails`. Self-test the hook without Claude: `python3 .claude/hooks/pm-os-guardrails.py --self-test`.

Codex CLI, from the PM OS checkout:

```
codex exec "Summarize the instructions you were given and name the file they came from."
```

If the answer stops before the `Memory & Learning` section, the 32 KiB cap is still in effect: check `project_doc_max_bytes` in `~/.codex/config.toml`.

Cursor: open the checkout; Settings → Rules shows `pm-os` as always applied. Ask the agent what `AGENTS.md` requires before any task.

ChatGPT: paste `SYSTEM.md`, then ask "what is the bot attribution prefix?" The answer quotes the prefix line from `AGENTS.md`.

## What stays Amp-only, and why

- `system/amp-plugins/`: everything written against the Amp plugin API (guardrails beyond the four shell rules, review lane, workstream lifecycle, goal loop, Chrome MCP lease, memory capture).
- `system/amp-mem/`, `system/bin/amp-mem`, `daemon/amp-*`: the Amp memory store and its distill jobs. Claude Code gets `claude-mem` instead (see Install above). The two stores are separate.
- `system/scripts/split-thread.sh`, `system/amp-wrapper.sh`: operate on Amp threads or wrap the `amp` CLI.

What stays cmux-only (laptop-specific; nothing to port, the concept is the terminal itself): `system/cmux-helpers.sh`, `system/cmux-sidebars/`, `system/scripts/cmux*`, `dispatch.py`, `router-dispatch.sh`, `report-to-cos.sh`, the command-center loop, `system/start-day.sh`, `end-of-day-close.sh`, the launchd plists. In another harness, do the task in your own terminal and record the result in the workstream `CONTEXT.md`; the policy in `AGENTS.md` about worktrees, review notes and reporting still applies.

"cmux-only" means the terminal, not the operating system. On Windows the same files run against [wmux](https://wmux.org) through `system/scripts/mux/cmux-shim.py`, selected by `system/scripts/lib/mux.sh`, with Task Scheduler standing in for launchd (`system/windows/`). The harness table above is unchanged by that: the loop still needs Amp. Nobody ports the loop to Claude Code, Codex, or Cursor during an install; that is a project of its own, not a setup step.

## Adding a harness

Add one row to the table above and one section to the kit `README.md`. If the harness reads `AGENTS.md` natively, that is the whole adapter. If it reads a differently named file, make that file import or point at `AGENTS.md`; do not copy text. If it has no file loader at all, point people at `SYSTEM.md`. If its hooks can express a guardrail, port the rule into a hook script under its config directory and add the case to that script's self-test.
