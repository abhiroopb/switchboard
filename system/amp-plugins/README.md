# PM OS Amp plugins

Tracked source for the PM OS Amp plugins. Runtime entries under `~/.config/amp/plugins/` are symlinks managed by `system/scripts/install-amp-plugins.sh`; `cmux-session.ts` is the sole exception because cmux owns it. On Windows, where Git Bash cannot make symlinks, each runtime `.ts` entry is a three-line shim that re-exports the tracked source in the main checkout, so Amp loads current code and `pm-os/` and `pm-os-bundle.json` resolve next to the source.

Native worker admission uses independent foreground proof or local process proof published by `task-liveness.ts` within `pm-os-runtime.ts`. Background executors retain null workspace fields. See [native worker identity](../../docs/runbooks/native-worker-identity.md) for activation, claim recovery, tests, and end-to-end verification limits.

## Plugin inventory

| Plugin | Hooks | Purpose |
|---|---|---|
| `amp-mem.ts` | `session.start`, `agent.start`, `agent.end`, `tool.result`; memory tools | Captures, classifies, and recalls durable Amp memory. |
| `chrome-mcp-lease.ts` | `agent.end` | Frees the owner-browser lease this session holds (`system/scripts/owner_browser.py`). Where the headless supervisor is installed, also requests release of this local Amp process's Chrome lease: the MCP proxy waits for pending calls, shuts down only its own browser, and preserves the authenticated profile. |
| `comms-triage-trigger.ts` | `agent.end` | Triggers comms triage after a successful Chief of Staff turn. |
| `cos-finished.ts` | `agent.end` | Reschedules meeting pre-brief and post-ingest jobs after Chief of Staff finishes. |
| `daily-rollup-trigger.ts` | `tool.result` | Debounces daily activity rollup rebuilds after outbound writes. |
| `goal-loop.ts` | `agent.end`; `goal_set`/`goal_status`/`goal_clear` tools | Goal-based agent loops (Claude Code `/goal` equivalent): stores a per-thread done-condition, evaluates it via `ctx.ai.ask` after each turn, and auto-continues until met, cleared, paused on error, or max turns (`goal-loop.max_turns`, default 10; met threshold `goal-loop.met_threshold`, default 0.8). |
| `pm-os-commands.ts` | command registrations | Adds PM OS workflows to Amp's command palette. |
| `pm-os-guardrails.ts` | `tool.call`, `tool.result` | Enforces destructive-command and outbound-write policies. Rule 3: `pm-os/rm-guard.ts` parses the command (same-command variables that hold one value, a preceding `cd`, heredoc and quoted data, `bash -c`, remote payloads such as `ssh` and a remote-box `exec`); targets it cannot pin down (xargs input, brace lists, rebound names) and temp or repo paths that are symlinks to an outside folder go to the helper, and an `rm -rf` whose targets sit outside pm-os, temp, and build paths is rewritten to `system/scripts/pm-os-trash.py`, which moves them to the macOS Trash and logs each move to `system/state/logs/trash-moves.jsonl`; `report-to-cos.sh` lists a task's moves. An in-repo `rm -rf` goes to the same helper, which hard-deletes only what git tracks unchanged and sends gitignored or untracked paths (`local/`, `system/state/`, scratch) to the Trash or Recycle Bin; when the command cannot be rewritten it runs as written. The repo root, `.git`, and top-level home or system folders are hard blocks. A delete that cannot be rewritten (`sudo`) keeps the confirm, and `system/scripts/permission_relay.py` sends any prompt left in a worker tab to CoS (`--steer <rt-id> allow|deny` presses it; a `Case: own task files` prompt, which only deletes or overwrites files that task made, CoS answers itself with `auto-allow` and reports, per `permission_case.py`); `dispatch.py sweep` also reads live worker tabs each tick, so tabs opened before the Claude `Notification` hook are covered. The CoS delivery gate resolves same-command variables the same way. Kill switches: `pm-os-guardrails.rm-rf.trash.disabled`, `pm-os-guardrails.permission-relay.disabled` (Amp modal relay), and `permission-relay.disabled` in `~/.claude/pm-os-plugin-config.json` (Claude Notification hook). Tests: `pm-os/rm-guard.test.ts`, `pm-os/guardrails-rm-trash.test.ts`, `pm-os/claude-bridge-rm-trash.test.ts`, `system/scripts/test_pm_os_trash.py`, `system/scripts/test_permission_relay.py`. The dispatch guard permits `create_thread`; `task-liveness` registers its child. It blocks messages to non-live threads and live Routers, but permits live workers. An unreadable session store fails open. Use `router-dispatch.sh` for new routed work. The orchestrator wait guard requires explicit approval for `wait_for_threads` and limits repeated thread polling and long sleeps. Kill switches: `pm-os-guardrails.dispatch-guard.disabled` and `pm-os-guardrails.wait-guard.disabled`. Tests: `system/scripts/test-wait-guard.sh`. Rule 9c: `pm-os/organize-execute.ts` rejects `organize.py --execute` without `--manifest`, and any copy of that script that predates the approved-plan fix, because an old copy ignores the flag; `.claude/hooks/pm-os-guardrails.py` mirrors it for Claude Code on the Bash and PowerShell tools; kill switch `pm-os-guardrails.organize-execute.disabled`. Rule 9d: `pm-os/sensitive-reads.ts` rejects an environment dump (`env`, `printenv`, and shell or PowerShell equivalents), because whatever an agent reads lands in its transcript; its Downloads read block is retired, so agents read and search files in Downloads; kill switch `pm-os-guardrails.sensitive-reads.disabled`. Rule 9e: `pm-os/amp-auth.ts` rejects `amp login`, `amp logout`, and any `amp` run with an overridden HOME, USERPROFILE, APPDATA, or XDG folder, because Amp's device sign-in opens a tab in the owner's browser; kill switch `pm-os-guardrails.amp-auth.disabled`. Rule 9b (open pages without stealing focus): the `open-focus` rule rejects an agent's bare `open <page>` (.html/.htm, `file://`, `http(s)://`) without `-g`, with the fix `open -g <page>` or `image_compare.py open`; a leading `PM_OS_FOCUS_OK=1` passes when the owner asks to bring a page up. Kill switch: `pm-os-guardrails.open-focus.disabled`. Tests: `pm-os/open-focus.test.ts`, `pm-os/open-focus-plugin.test.ts`, `system/scripts/test_background_open.py`. |
| `review-lane.ts` | `agent.start`, `agent.end` | Pins reviewable results to cmux's review lane; settled non-review results stay on auto. |
| `task-caffeinate.ts` | `agent.start`, `agent.end` | Keeps macOS awake only while Amp is processing a turn. |
| `task-liveness.ts` | `agent.start`, `tool.result`, `agent.end`; `pm-os-task-liveness-status` command | Orchestrator liveness. Parses the seeded prompt for `ROUTER_TASK_ID`/`ROUTER_ATTEMPT_ID` (Router) or `Router return contract: task rt-…` (worker; multiple queued handoffs supported) and records `ack` in `system/state/running-tasks.json` with thread id, cmux workspace ref, and workstream. A worker also acks its own task from `PM_OS_WORKER_TASK_ID`/`PM_OS_WORKER_ATTEMPT_ID` (set by `dispatch.py launch`) on every event, so it never depends on finding the seed in the transcript; a Claude launch also sets `PM_OS_WORKER_SESSION_ID` and only that session acks (test `pm-os/claude-bridge-heartbeat.test.ts`). Sends a throttled `heartbeat` on tool results (`PM_OS_TASK_HEARTBEAT_MS`, default 60s) and on turn end. Drops stale attempts (`--attempt-id` fencing) and terminal tasks. Kill switch `task-liveness.disabled`. `command-center-loop.sh` runs `running-task-state.py expire-leases` each cycle and appends stale tasks to the digest. |
| `watched-docs-refresh.ts` | `session.start` | Refreshes a workstream's watched Google Doc snapshots. |
| `watched-slack-refresh.ts` | `session.start` | Refreshes a workstream's watched Slack snapshots. |
| `workspace-report.ts` | `agent.start`, `agent.end`; idle recovery | Mirrors direct workstream completions to CoS, recovers completed turns when Amp omits `agent.end`, and delivers queued handoffs through the authenticated thread API. |
| `workstream-lifecycle.ts` | `agent.end` | Reconciles any active workstream status to review when its agent turn ends, preserving Active for running agents only. |
| `workstream-signals.ts` | `session.start` | Builds fresh recent signals when a workstream opens. |

The shared `pm-os/` directory contains `lib.ts`, the Bun test harness, and fixtures. Plugin imports such as `./pm-os/lib` resolve relative to these tracked source files, including when Amp loads them through runtime symlinks.

## Goal-loop state transitions

The goal loop serializes short state updates within one plugin process. Persisted revisions reject results from evaluations started before a clear, replacement, or another accepted evaluation. AI calls do not hold the queue, so clear remains responsive.

This is not a cross-process transaction. Do not run two Amp processes against the same thread and assume these revisions prevent both writers. Existing plugin processes need an approved reload to use the new code. The event tests mock shared plugin utilities and put them back when the file ends, so both files also pass inside the full suite (see Testing).

## Chrome MCP lease activation

The release plugin requires `system/scripts/chrome-mcp-session.py` as Amp's `chrome-devtools` launcher. Installing the plugin alone does not change an existing MCP connection. Configure and reload only after browser work finishes. See the [lease runbook](../../docs/runbooks/chrome-mcp-leases.md) for activation, orphan inspection, test commands, and rollback. The plugin does not stop unregistered legacy browsers or change either AGENTS.md policy.

## Layout

```
system/amp-plugins/
├── README.md                  # this file
├── amp-mem.ts                 # passive memory capture (independent project)
├── pm-os/                     # shared utilities + test harness
│   ├── lib.ts                 # paths, run, runDetached, logEvent, state, ...
│   ├── test-runner.ts         # standalone Bun harness for plugin tests
│   └── test-fixtures/         # JSON event fixtures
└── *.ts                       # PM-OS-owned plugins
```

## Required header

Every plugin file must start with:

```ts
// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
```

Without it, Amp refuses to load the plugin.

## Reload after edits

In an active Amp session: `Ctrl-o` → `plugins: reload`. Hot-reloads all plugins; no restart needed.

## Enable / disable

- All on (default in interactive): just run `amp`.
- All off: `PLUGINS= amp`.
- Specific plugins only: `PLUGINS=amp-mem,pm-os-guardrails amp`.
- Per-plugin kill switch via configuration: each pm-os plugin checks `<plugin-key>.disabled` (e.g. `pm-os-guardrails.disabled = true`).

### Slack turn bridge (retired)

Retired: replies depended on a collector that sleeps overnight, and CoS is the direct channel. The `slack_turn_bridge` entry in `system/config/priority-channels.yaml` and the mapping store in `~/.config/amp/plugin-state/slack-turn-bridge/` are left in place.

## Logging

- Telemetry events: `~/.agents/.amp-plugin-logs/pm-os-events.jsonl`
- Detached spawn output: `~/.agents/.amp-plugin-logs/detached.log`

### Audit repair behavior and activation

- Plugin event logs record metadata only. New logs use mode `0600`, and new log directories use `0700`. Existing permissions and historical content are unchanged.
- `amp-mem` carries the private flag through correction, file-path, tool-write, and decision capture. Private session summaries contain a generic completion message. New decision summaries retain the source thread and capture time without a tool-name footer.
- The Slack bridge skips private turns, headless runs, and automation envelopes. Eligible turns use prompt summaries with the explicit opt-in documented above; an unset/false key retains the content-free privacy default. This does not change Slack membership or historical posts.
- `writeState` uses a unique temporary file and atomic rename. Its contract remains last-writer-wins; it is not a transaction for a read-modify-write sequence across processes.
- The five trigger plugins use `runDebouncedDetached` and `run-debounced-plugin.py`. The detached Python runner takes a stable `fcntl` lock for one plugin/key, checks the last successful completion under that lock, waits for the child, and publishes `lastFiredAt` with a unique temporary file plus atomic replace only after exit 0. Failed children remain immediately eligible. Different plugin/keys do not block each other, and downstream scripts keep their separate worker-wide serialization.
- CoS confirmation reuse is limited to the exact operation and destination within one turn. The next `agent.start` clears the grant. Other outbound-write guards remain independent.
- Existing processes retain their loaded plugin code until reload. Run the installer and reload only in an approved activation window. Tests against tracked files do not prove that live sessions use these changes.

### Lifecycle field ownership

| Surface | Owner and boundary |
|---|---|
| cmux execution state | The cmux-managed `cmux-session.ts` publishes lifecycle events. The repair plugin retries only a matching completed turn and requires read-back. |
| Sidebar review lane | `review-lane.ts` projects unread/review status. It does not establish that an executor is running. |
| Workstream status | `workstream-lifecycle.ts` uses the current update identity to move active work to review. Supplemental workers do not own this aggregate. |
| Pending handoffs | `workspace-report.ts` serializes delivery. Direct user responses wait for idle; ordinary task handoffs retain their existing steering policy. |

These are different fields, not four independent execution authorities. Do not merge them or add a blanket idle gate without testing same-task steering.

### Completion-aware trigger debounce

`cos-finished`, `daily-rollup-trigger`, `watched-docs-refresh`, `watched-slack-refresh`, and `workstream-signals` keep their existing guards and cooldown lengths. Their callbacks only detach the runner. The runner owns the lock while the expensive child executes, so a child PID does not count as success.

Contention exits 75 without waiting or recording success. The direct child inherits the lock descriptor, so supervisor exit does not admit another child while the first remains alive. A child that hangs still holds the lock; this runner does not impose a new execution timeout.

Existing `lastFiredAt` files can contain launch-time timestamps from the old plugins. They expire under the existing cooldown. They are not retroactive proof of successful completion. Mixed old and new plugin processes do not share this exclusion protocol; activate together in an approved reload window.

Focused test command:

```bash
python3 -m unittest system/scripts/test_run_debounced_plugin.py
bun test system/amp-plugins/pm-os/debounced-triggers.test.ts
```

The synthetic tests cover concurrent callers, failed-child retry eligibility, success cooldown, supervisor exit, and the five plugins' guards and cooldown arguments. Its helper mocks are restored when the file ends, so it also runs inside the full suite.

## Testing

Run the whole suite in one process from the repo root:

```bash
bun test system/amp-plugins
```

Bun keeps a `mock.module()` replacement for the rest of the process, and `mock.restore()` does not undo it. A test file that mocks `./lib`, `node:fs/promises`, or `node:child_process` adds `afterAll(restoreModuleMocks)` from `pm-os/test-mocks.ts`, which puts the real exports back for later files. Without it, a partial mock leaks into every file after it. Tests that depend on the host clear the variables they read (for example `CMUX_BIN` inside a cmux or wmux tab) or pin the platform with a seam such as `PM_OS_RM_GUARD_PLATFORM`.

To drive one plugin with a fixture:

```bash
bun run ~/.config/amp/plugins/pm-os/test-runner.ts ~/.config/amp/plugins/pm-os-guardrails.ts \
       ~/.config/amp/plugins/pm-os/test-fixtures/git-add-all.json
```

The harness mocks `ctx.ui.*`, `ctx.$`, `ctx.ai`, and `ctx.thread`. It records handler return values to stdout. It does NOT actually execute side effects (other than what the plugin itself does in real fs/exec calls — keep destructive logic behind a `dryRun` config flag if you want isolation).

## Conventions

1. **Header first.** Every file: WIP header on line 1.
2. **Logger only.** Use `ctx.logger`, not `console.log`. Telemetry picks it up.
3. **Never disrupt.** Wrap risky calls in try/catch — a plugin error must never block the user's session.
4. **Idempotent.** `session.start` may fire multiple times per thread. Plugins must tolerate replays.
5. **Headless-safe.** Detect `lib.isHeadless()` before calling `ctx.ui.*` — UI is not available in `amp -x` runs.
6. **State files.** Use `lib.readState/writeState` keyed under the plugin name. Don't write to the repo.
7. **Detach long work.** Anything > 200ms goes through `lib.runDetached` so handler returns fast.

## Cross-references

- Active plugin table in [~/AGENTS.md](../../../AGENTS.md)
- Plugin API docs: https://ampcode.com/manual/plugin-api
