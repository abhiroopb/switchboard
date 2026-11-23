# switchboard Claude Code plugin

Tracked source for the `switchboard` Claude Code plugin. Claude loads it in place from this directory through the local marketplace `system/claude-marketplace/` (`switchboard-local`), so edits take effect in the next Claude session with no reinstall. The Amp plugins in `system/amp-plugins/` stay the single source of truth for every rule; this plugin only maps Claude hook events onto them.

`system/scripts/install-claude-code.sh` installs it (`claude plugin marketplace add <kit root>/system/claude-marketplace` then `claude plugin install switchboard@switchboard-local --scope user`). It needs Bun on PATH. Validate after edits: `claude plugin validate system/claude-marketplace/plugins/switchboard`. Kill switches: `~/.claude/switchboard-plugin-config.json`.

## Components

| Path | What it provides |
|---|---|
| `hooks/hooks.json` | Registers six Claude hook events (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`, `Notification`). Every event but `Notification` runs `hooks/amp-bridge.ts` with Bun; `SessionStart` and `UserPromptSubmit` also run `hooks/surface-context.py`; `PreToolUse` (every tool), `UserPromptSubmit`, and `Stop` also run `hooks/cmux-heartbeat.py`. `Notification` (`permission_prompt`, `elicitation_dialog`) runs `system/scripts/permission_relay.py notify`, which sends one `[ROUTER REPORT] 🔐` callback to CoS when a worker or owner tab waits on a prompt, with a `Case:` line (`own task files`, which CoS answers itself with `--steer <rt-id> auto-allow`, or `the owner decides`; `system/scripts/permission_case.py`) (kill switch `permission-relay.disabled`; tests `system/scripts/test_permission_relay.py`). A new `Notification` hook loads in new Claude sessions only; `dispatch.py sweep` relays prompts in worker tabs that started before it. |
| `hooks/surface-context.py` | Claude-native, not an Amp port. Injects `system/prompts/dispatch-core-card.md` plus the live workstream slugs into CoS and Launchpad sessions on `SessionStart` and a one-line routing reminder on `UserPromptSubmit`; exports `DISPATCH_ORIGIN`, `SWITCHBOARD_SURFACE`, `SWITCHBOARD_ROOT` through `CLAUDE_ENV_FILE`. Silent for workers, owner tabs, and every other session. Tests: `system/scripts/test_surface_context.py`. |
| `hooks/cmux-heartbeat.py` | Claude-native, not an Amp port. Resends cmux's own `pre-tool-use` hook event with up to 4 tries in 3 s, at most once per 90 s per session, so the cmux sidebar keeps the ⚡ on a working Claude tab. cmux 0.65.2 drops about half of its own enqueues with "Broken pipe (errno 32)" and forwards no `PostToolUse`, and the sidebar turns ⚡ off after 20 minutes with no feed event. `Stop` and `UserPromptSubmit` clear the throttle, so a Stop-hook continuation sends at once. Silent outside cmux and for `AskUserQuestion`, `ExitPlanMode`, `EnterPlanMode`. Tests: `system/scripts/test_cmux_heartbeat.py`. |
| `hooks/amp-bridge.ts` | Loads the Amp plugin modules from `system/amp-plugins/`, replays the Claude event through a minimal Amp PluginAPI shim, and prints the Claude hook decision. |
| (no `.mcp.json`) | MCP servers are registered at user scope by `system/scripts/install-claude-code.sh` with per-OS paths, so the plugin ships no `.mcp.json`. Run `claude mcp list` to see them. |
| `commands/` | Slash commands (`/switchboard:<name>`), ported from `~/.claude/commands/` and the Amp `switchboard-commands.ts` palette entries. |
| `agents/` | Reviewer and research subagents. `system/scripts/generate-claude-agents.py` regenerates the reviewer personas from `sub-agents/*.md`; the six research agents are copied from `~/.claude/agents/`. |
| `skills/` | Intentionally empty. Skills are copied into `~/.claude/skills/` by `system/scripts/sync-skills.py` (through `install-claude-skills.py`) so Amp and Claude read identical copies. |

## Event mapping

| Claude event | Amp event | Amp plugins that run |
|---|---|---|
| `PreToolUse` (`Bash`, `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `mcp__*`) | `tool.call` | `switchboard-guardrails`, `task-liveness` |
| `PostToolUse` (the same tools plus `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task`, `Agent`, `Skill`, so a read-only worker still heartbeats) | `tool.result` | `switchboard-guardrails`, `task-liveness`, `daily-rollup-trigger` |
| `UserPromptSubmit` | `agent.start` | `auto-pilot-router`, `switchboard-guardrails`, `task-liveness`, `review-lane`, `task-caffeinate`, `workspace-report` |
| `Stop` | `agent.end` | `chrome-mcp-lease`, `comms-triage-trigger`, `cos-finished`, `review-lane`, `task-caffeinate`, `task-liveness`, `workspace-report`, `workstream-lifecycle` |
| `SessionStart` | `session.start` | `watched-docs-refresh`, `watched-slack-refresh`, `workspace-report`, `workstream-signals` |

Excluded on purpose: any Amp usage telemetry plugin, `amp-mem` (replaced by the `claude-mem` plugin), `goal-loop` (Claude has native `/goal`), `cmux-lifecycle-reconcile`.

Tool-name mapping: Claude `Bash` → Amp `shell_command` (`cmd`), `Edit`/`MultiEdit` → `edit_file`, `Write` → `create_file`, `NotebookEdit` → `edit_file` with `notebook_path`, `mcp__<server>__<tool>` → the same name. Amp `allow-with-input` becomes Claude `updatedInput` with the field names mapped back.

Decision mapping: Amp `reject` → `permissionDecision: deny`; `allow`, `warn`, `reject-and-continue` → allow plus `additionalContext`; a reject whose message mentions confirmation after a `ctx.ui.confirm` call → `permissionDecision: ask` with the confirm title.

## Kill switches and debugging

Config file: `~/.claude/switchboard-plugin-config.json` (absent means everything on).

- `{"switchboard-bridge.disabled": true}` turns the whole bridge off.
- `{"switchboard-guardrails.disabled": true}` (or any plugin name) skips that plugin on every event.
- `{"cmux-heartbeat.disabled": true}` turns the cmux ⚡ heartbeat off. `SWITCHBOARD_CMUX_HEARTBEAT_DEBUG=1` logs each heartbeat decision to stderr.
- `SWITCHBOARD_BRIDGE_ONLY=switchboard-guardrails,task-liveness` runs only the named plugins for one process.
- `SWITCHBOARD_BRIDGE_DEBUG=1` logs each plugin decision to stderr. The bridge never writes to stdout except the hook JSON.

Pipe test from the repo root:

```
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git add -A"},"session_id":"t","cwd":"{{SWITCHBOARD_ROOT}}"}' \
  | bun system/claude-marketplace/plugins/switchboard/hooks/amp-bridge.ts pre-tool-use
```

Expected: a `deny` decision from `switchboard-guardrails`.

## Known limits of the bridge

- **No cross-event memory.** Claude starts a fresh process for every hook, so Amp plugins that keep in-process state (debounce timers, per-thread counters) start empty each event. Anything durable must already live in files under `system/state/`, which the ported plugins do.
- **Confirm becomes ask.** `ctx.ui.confirm` cannot block inside a hook. The bridge records the request and, when the plugin rejects for lack of confirmation, returns `permissionDecision: ask` so Claude prompts the user instead. Each ask also lands in `system/state/worker-dispatch/permission-asks/<session>.json`, so the relay callback can quote the exact command. An `allow-with-input` decision (the Rule 3 Trash rewrite) becomes `permissionDecision: allow` with `updatedInput`.
- **`ctx.ai.ask` is unavailable.** Plugins that call it receive an error and fall through to their non-AI path. This is why `goal-loop` is excluded.
- **Steers need no thread tool.** Amp CoS steered a live worker with `send_thread_message`. Under Claude, `router-dispatch.sh --steer` delivers to a live Claude worker itself by `cmux agent message` (`dispatch.py` `cmux_agent_message`, status `delivered`), so CoS needs no native tool.
- **The CoS contract is injected, not read.** Amp CoS learned the dispatch rules from its start-day seed and `rotate-cos-thread.sh`, which needs `amp threads handoff` and is skipped under Claude. `surface-context.py` injects the card instead, on every session start and compaction.
- **Dispatch and wait guards are inert.** Amp-only tools (`send_thread_message`, `create_thread`, `wait_for_threads`, `get_thread_status`) never reach Claude, so those guards never fire. Claude workers message other sessions with `cmux agent message` through `Bash`, which the command guard still inspects.
- **Stop reads the transcript JSONL.** `agent.end` plugins that need the last assistant message get it from `transcript_path`, not from a live thread object.
- **Tool results are not user messages.** `ctx.thread.messages()` reads the transcript JSONL and skips rows whose content is only `tool_result` blocks, as Amp does, so a `roles: ['user'], limit: 20` read keeps the seed prompt after many tool calls.
- **No Amp thread id.** Plugins that would record `T-…` thread ids record the Claude `session_id` instead. `dispatch.py` writes `agent: claude` and `session_id` in the ledger row when the agent knob resolves to `claude`.

## Shared browser lease

The kit registers `chrome-devtools` at user scope through `install-claude-code.sh` (`npx chrome-devtools-mcp`, headless, profile `~/.cache/chrome-devtools-mcp/chrome-profile`). Amp and Claude each launch their own headless browser. A machine that opts in to a real Brave (`system/scripts/owner_browser.py`) registers `chrome-devtools` as a proxy to the agent profile instead (a separate Brave signed in to nothing, no lease), `chrome-devtools-owner` as a proxy to the owner's own Brave with one lease per machine, and `chrome-devtools-headless` for scheduled jobs; the `chrome-mcp-lease` module frees the owner lease at turn end.

## Cross-references

- Amp plugin inventory and conventions: `system/amp-plugins/README.md`.
- Launcher knob: `system/config/router.yaml` `agent: claude|amp` (default `claude`); `SWITCHBOARD_AGENT=claude|amp` overrides it per process. Resolver: `system/scripts/switchboard_agent.py`, used by `system/cmux-helpers.sh`, `system/scripts/dispatch.py`, `system/start-day.sh`, and `system/scripts/switchboard-headless-agent.sh`.
