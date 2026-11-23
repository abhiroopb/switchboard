// @i-know-the-amp-plugin-api-is-wip-and-very-experimental-right-now
//
// review-lane — keeps genuinely reviewable work in cmux's "TO REVIEW" lane,
// with explicit workspace-status and startup-hydration awareness.
//
// Unread contract: the sidebar has exactly two ordinary
// lanes, "To review" (unread) and "Idle" (read). Running is a ⚡ glyph, not a
// lane. The review purple (#BB9AF7) on the workspace's custom color is the
// durable "unread / To review" fact (cmux's own `unread` auto-clears when
// the tab is focused, so it cannot carry the acknowledgement). Writers:
//   - set purple: this plugin at agent.end (new reviewable output), and the
//     user's explicit Mark unread (sidebar context menu).
//   - clear purple: ONLY the user's explicit Mark read (sidebar ✓ chip or
//     context menu). Nothing else — not agent.start, not a settled update,
//     not publish-workspace-status.sh (which never writes purple and never
//     overwrites a live purple).
//
// Behavior:
//  1. agent.end (done/error): pin this cmux workspace to the "review" lane
//     unless this turn explicitly emitted a settled, non-review workspace
//     update (done, waiting, paused, scheduled, reference, or resolved). A
//     settled update suppresses the pin only when it has no review artifact,
//     no requires_user action, and no blocked/needs-action status. This keeps
//     completed operational work out of TO REVIEW while preserving review for
//     turns that emitted no durable disposition. A settled turn does NOT
//     clear an older purple the user has not marked read yet.
//     Review pins paint the color purple and create a review-pin marker file
//     (diagnostics: records that this plugin set the hold; the publisher no
//     longer reads it).
//     Pin timing: the pin lands at agent.end (default debounce 0). With no
//     Active lane and no lane move on agent.start there is no bounce to
//     debounce, and a deferred pin could repaint a row the user
//     had already marked read inside the window. If a debounce is configured
//     (`review-lane.pin_debounce_ms` / REVIEW_LANE_PIN_DEBOUNCE_MS), a fired
//     timer re-reads the live color: a row that was purple at turn end and
//     is no longer purple was marked read after the completion, so the pin
//     is skipped and the marker deleted (late-completion protection).
//  2. agent.start (user responded): cancel a still-pending deferred pin
//     (the user replied before it landed) and release the NATIVE lane
//     override so cmux's own inference resumes. The purple hold is left
//     untouched: typing is not reading. If the live color is no longer
//     purple (user marked read), the stale marker is deleted (reconcile).
//     Router N workspaces are background dispatch utilities. They never pin
//     to review; either hook also clears any stale review state they carry.
//  3. Startup hydration (P0 lifecycle): start-day.sh,
//     ensure-p0-workspaces.sh, and `cmux-helpers.sh launch` seed
//     machine-generated startup prompts (workstream context re-seed, P0
//     header, reminder card). Those turns are NOT reviewable work and their
//     arrival is NOT the owner responding. The launchers drop a hydration marker
//     (hydration-<CMUX_WORKSPACE_ID>.json in this plugin's state dir) right
//     before each hydration send; this plugin consumes it:
//       - agent.start with a marker: the turn is machine-seeded — do NOT
//         release an existing review pin (real review state survives
//         restart) and do NOT restore the color. The consumed marker is
//         re-persisted as a turn-scoped hydration-turn-<id>.json so child
//         processes of the turn (emit-workspace-update.py) can see the
//         hydration provenance mid-turn; agent.end deletes it.
//       - agent.end of that turn: do NOT pin review — a hydrated P0 stays in
//         the P0 lane — UNLESS the turn emitted an explicitly review-worthy
//         workspace update (status blocked / needs action / needs review,
//         or a new_action with requires_user=true) via
//         emit-workspace-update.py. That escape hatch lets a startup run
//         that genuinely produced a decision/blocker still land in To Review.
//         Plain "to review" does NOT count during a hydration turn: the
//         generic launch prompt teaches agents to emit "to review" for any
//         completed work, so it would manufacture review state on every
//         startup pass. A startup run that truly needs the owner must say so
//         explicitly: "to review" with --review-artifact (genuine artifact,
//         purple lane),
//         blocked / needs action / needs review (urgent, red lane), or
//         requires_user=true.
//     Routed asks never mark hydration (route-to-workspace.sh folds the ask
//     into a fresh launch prompt, or clears any pending marker before a
//     separate send). Their explicit final workspace update now decides
//     whether the result is reviewable.
//     Markers older than 6h are stale and ignored (deleted on sight) so a
//     lost marker can never permanently suppress review pins.
//
// Caveat: cmux auto-clears a lane override when the *inferred* lane changes
// (anti-rot). In practice switchboard workspaces infer "working" almost always
// (dirty tree), so the pin holds until the user responds.
//
// Test seams (used by system/scripts/test-review-lane-hydration.sh):
//   REVIEW_LANE_STATE_DIR  — marker/state dir override
//   REVIEW_LANE_CMUX_BIN   — cmux binary override (stub)
//   SWITCHBOARD_REPO_ROOT        — repo root override for workspace-updates.json
//
// Kill switch: `review-lane.disabled = true`
// Installed by system/scripts/install-amp-plugins.sh (symlink from this repo).

import { execFile } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import type { PluginAPI, AgentStartEvent, AgentEndEvent } from '@ampcode/plugin'
import { workstreamSlugFromContext } from './switchboard/lib'

const PLUGIN = 'review-lane'
// Runtime version marker. Bump when behavior changes. Written to
// runtime-<CMUX_WORKSPACE_ID>.json in STATE_DIR every time this plugin loads
// (fresh session or `plugins: reload`), so an operator can verify which
// version a live session actually runs — file timestamps on the tracked
// source cannot tell that. Read with:
//   cat ~/.config/amp/plugin-state/review-lane/runtime-<workspace-uuid>.json
const PLUGIN_VERSION = '2026-09-08.1'
const HOME = homedir()
const STATE_DIR = process.env.REVIEW_LANE_STATE_DIR || join(HOME, '.config', 'amp', 'plugin-state', PLUGIN)
const REPO_ROOT = process.env.SWITCHBOARD_REPO_ROOT || join(HOME, 'Development', 'switchboard')
const CMUX_FALLBACK = '/Applications/cmux.app/Contents/Resources/bin/cmux'
// Must match the "To review" color in system/cmux-sidebars/switchboard.swift and
// COLOR{purple} in system/scripts/publish-workspace-status.sh.
const REVIEW_COLOR = '#bb9af7'
// A hydration marker is written seconds before its turn starts; anything this
// old is debris from a failed launch, not live provenance.
const HYDRATION_TTL_MS = 6 * 60 * 60 * 1000
// A normal routed task can run longer than startup hydration. The marker is
// replaced at every agent.start and consumed at agent.end; the TTL only keeps
// crash debris from governing a later turn whose start hook was missed.
const NORMAL_TURN_TTL_MS = 24 * 60 * 60 * 1000
// Workspace-update statuses that let a HYDRATION turn escape into To Review.
// Deliberately narrower than publish-workspace-status.sh's attention set:
// plain "to review" is the generic launch-prompt default for any completed
// work, so during startup hydration it is noise, not an escalation.
const HYDRATION_ESCALATION_STATUSES = new Set(['blocked', 'needs action', 'needs review'])
const SETTLED_NON_REVIEW_STATUSES = new Set([
  'done',
  'waiting',
  'paused',
  'scheduled',
  'reference',
  'resolved',
])
// How long a finished turn defers its review pin. 0 = pin at agent.end
// (no Active lane, so there is no bounce to debounce, and a deferred pin
// risks undoing a Mark read inside the window). Opt back in
// via `review-lane.pin_debounce_ms` or REVIEW_LANE_PIN_DEBOUNCE_MS.
const DEFAULT_PIN_DEBOUNCE_MS = 0

interface WorkspaceUpdate {
  update_id?: string
  timestamp?: string
  status?: string
  summary?: string
  recommended_next_action?: string
  review_artifact?: boolean | null
  new_actions?: Array<{ requires_user?: boolean | null }>
}

interface NormalTurnMarker {
  at: number
  slug: string
  baselineIdentity: string | null
}

function isHeadless(): boolean {
  return Boolean(process.env.AMP_HEADLESS) || process.env.AMP_INTERACTIVE === 'false'
}

function isEnabled(config: Record<string, unknown>): boolean {
  const v = config[`${PLUGIN}.disabled`]
  return v !== true && v !== 'true'
}

function isRouterTitle(title: string): boolean {
  const withoutLeadingGlyph = title.trim().replace(/^[^\p{L}\p{N}]+\s*/u, '')
  return /^Router [1-9]\d*$/.test(withoutLeadingGlyph)
}

// Worker tabs (dispatch.py launch) never carry
// review state: they live in the sidebar's Workers section whatever their
// agent state, and a finished turn shows no unread count. The worker's Amp
// process runs with SWITCHBOARD_WORKER_ID in its environment; the tab description
// "switchboard worker <task-id>" is the secondary check for a plugin loaded some
// other way into that tab.
const WORKER_DESCRIPTION_PREFIX = 'switchboard worker '

function isWorkerSession(description: string): boolean {
  return Boolean(process.env.SWITCHBOARD_WORKER_ID) || description.startsWith(WORKER_DESCRIPTION_PREFIX)
}

function run(bin: string, args: string[], opts: { cwd?: string; timeoutMs?: number } = {}): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise(resolve => {
    execFile(bin, args, { cwd: opts.cwd, timeout: opts.timeoutMs ?? 10_000 }, (error, stdout, stderr) => {
      const code = error ? ((error as NodeJS.ErrnoException & { code?: number | string }).code === undefined ? 1 : Number((error as { code?: number | string }).code) || 1) : 0
      resolve({ code, stdout: String(stdout ?? ''), stderr: String(stderr ?? '') })
    })
  })
}

function cmuxBin(): string {
  if (process.env.REVIEW_LANE_CMUX_BIN) return process.env.REVIEW_LANE_CMUX_BIN
  if (process.env.CMUX_BIN) return process.env.CMUX_BIN
  return existsSync(CMUX_FALLBACK) ? CMUX_FALLBACK : 'cmux'
}

export default function (amp: PluginAPI) {
  let config: Record<string, unknown> = {}
  amp.configuration.subscribe(v => { config = v })

  const workspaceId = process.env.CMUX_WORKSPACE_ID || null

  // Runtime version marker (see PLUGIN_VERSION). Best-effort; never blocks
  // plugin load. Only the loading session's own file is touched.
  if (workspaceId) {
    try {
      mkdirSync(STATE_DIR, { recursive: true })
      writeFileSync(
        join(STATE_DIR, `runtime-${workspaceId}.json`),
        JSON.stringify({ plugin: PLUGIN, version: PLUGIN_VERSION, loaded_at: new Date().toISOString(), pid: process.pid }, null, 2),
      )
    } catch {}
  }

  const currentWorkspace = async (): Promise<{ title: string; description: string }> => {
    if (!workspaceId) return { title: '', description: '' }
    const r = await run(cmuxBin(), ['rpc', 'workspace.list', '{}'], { timeoutMs: 5000 })
    if (r.code !== 0) return { title: '', description: '' }
    try {
      const parsed = JSON.parse(r.stdout) as {
        workspaces?: Array<{ id?: string; title?: string; custom_title?: string; description?: string | null }>
      }
      const workspace = (parsed.workspaces ?? []).find(w => w.id === workspaceId)
      return { title: workspace?.custom_title ?? workspace?.title ?? '', description: workspace?.description ?? '' }
    } catch {
      return { title: '', description: '' }
    }
  }
  const currentWorkspaceTitle = async (): Promise<string> => (await currentWorkspace()).title

  // Epoch ms at which the currently-running turn's hydration marker was
  // written, or null when the current turn is not a hydration turn. Set at
  // agent.start (marker consumed), cleared at agent.end.
  let hydrationTurnAt: number | null = null
  // Baseline for the current non-hydration turn. The disk copy makes an Amp
  // plugin reload during a turn safe: agent.end can still prove that the final
  // workspace update was emitted after this turn began.
  let normalTurn: NormalTurnMarker | null = null

  const setLane = async (lane: 'review' | 'auto') => {
    if (!workspaceId) return
    await run(cmuxBin(), ['workspace', 'status', 'set', lane, '--workspace', workspaceId], { timeoutMs: 5000 })
  }

  const currentOverrideIsReview = async (): Promise<boolean> => {
    if (!workspaceId) return false
    const r = await run(cmuxBin(), ['workspace', 'status', '--workspace', workspaceId], { timeoutMs: 5000 })
    return r.code === 0 && /override:\s*review/.test(r.stdout)
  }

  const pinMarkerPath = (): string => join(STATE_DIR, `review-pin-${workspaceId}.json`)
  const hydrationMarkerPath = (): string => join(STATE_DIR, `hydration-${workspaceId}.json`)
  // Turn-scoped copy of a consumed hydration marker. Lives only for the
  // duration of the hydration turn so child processes of the turn
  // (emit-workspace-update.py) can see the hydration provenance after the
  // plugin has consumed the launcher's marker at agent.start.
  const hydrationTurnMarkerPath = (): string => join(STATE_DIR, `hydration-turn-${workspaceId}.json`)
  const normalTurnMarkerPath = (): string => join(STATE_DIR, `normal-turn-${workspaceId}.json`)

  const readWorkspaceUpdate = (slug: string): WorkspaceUpdate | null => {
    const updatesPath = join(REPO_ROOT, 'system', 'state', 'sources', 'workspace-updates.json')
    try {
      const payload = JSON.parse(readFileSync(updatesPath, 'utf8')) as {
        updates?: Record<string, WorkspaceUpdate>
      }
      return payload.updates?.[`workstream:${slug}`] ?? null
    } catch {
      return null
    }
  }

  const updateIdentity = (update: WorkspaceUpdate | null): string | null => {
    if (!update) return null
    return JSON.stringify([
      update.update_id ?? '',
      update.timestamp ?? '',
      update.status ?? '',
      update.summary ?? '',
      update.recommended_next_action ?? '',
      update.review_artifact === true,
      (update.new_actions ?? []).map(action => action?.requires_user === true),
    ])
  }

  const clearNormalTurnMarker = () => {
    normalTurn = null
    try { unlinkSync(normalTurnMarkerPath()) } catch {}
  }

  const beginNormalTurn = (title: string) => {
    clearNormalTurnMarker()
    const slug = workstreamSlugFromContext(title, REPO_ROOT)
      ?? workstreamSlugFromContext(process.cwd(), REPO_ROOT)
    if (!slug) return
    const marker: NormalTurnMarker = {
      at: Date.now(),
      slug,
      baselineIdentity: updateIdentity(readWorkspaceUpdate(slug)),
    }
    normalTurn = marker
    try {
      mkdirSync(STATE_DIR, { recursive: true })
      writeFileSync(normalTurnMarkerPath(), JSON.stringify(marker))
    } catch {}
  }

  const consumeNormalTurnMarker = (): NormalTurnMarker | null => {
    if (!workspaceId) return null
    const p = normalTurnMarkerPath()
    if (!existsSync(p)) return null
    let marker: NormalTurnMarker | null = null
    try {
      const parsed = JSON.parse(readFileSync(p, 'utf8')) as Partial<NormalTurnMarker>
      const at = Number(parsed.at)
      if (
        Number.isFinite(at)
        && at > 0
        && Date.now() - at <= NORMAL_TURN_TTL_MS
        && typeof parsed.slug === 'string'
        && /^[a-z0-9-]+$/i.test(parsed.slug)
        && (typeof parsed.baselineIdentity === 'string' || parsed.baselineIdentity === null)
      ) {
        marker = parsed as NormalTurnMarker
      }
    } catch {}
    try { unlinkSync(p) } catch {}
    return marker
  }

  const turnEmittedSettledNonReview = (marker: NormalTurnMarker): boolean => {
    const update = readWorkspaceUpdate(marker.slug)
    if (!update || updateIdentity(update) === marker.baselineIdentity) return false
    const ts = Date.parse(update.timestamp ?? '')
    // emit-workspace-update timestamps have second granularity. Floor the
    // marker so an update from the same second still belongs to this turn.
    if (!Number.isFinite(ts) || ts < Math.floor(marker.at / 1000) * 1000) return false
    const status = String(update.status ?? '').trim().toLowerCase()
    if (!SETTLED_NON_REVIEW_STATUSES.has(status)) return false
    if (update.review_artifact === true) return false
    if ((update.new_actions ?? []).some(action => action?.requires_user === true)) return false
    // Preserve explicit user ownership even when an older emitter omitted a
    // structured requires_user action. New emitters should prefer that field.
    const nextAction = String(update.recommended_next_action ?? '').trim().toLowerCase()
    if (/^(?:owner\b|you\b|your\b)/.test(nextAction)) return false
    return true
  }

  // Read AND delete the hydration marker for this workspace. Returns its
  // write-time (epoch ms) when the marker is live, or null when there is no
  // marker or it is stale (past TTL). Consuming is idempotent and race-benign:
  // the marker only ever governs the single turn that follows its seed.
  const consumeHydrationMarker = (): { at: number } | null => {
    if (!workspaceId) return null
    const p = hydrationMarkerPath()
    if (!existsSync(p)) return null
    let at = 0
    try {
      at = Number((JSON.parse(readFileSync(p, 'utf8')) as { at?: number }).at ?? 0)
    } catch {}
    try { unlinkSync(p) } catch {}
    if (!Number.isFinite(at) || at <= 0 || Date.now() - at > HYDRATION_TTL_MS) return null
    return { at }
  }

  // Same consume semantics for the turn-scoped marker (agent.end fallback
  // when a plugin restart lost both the in-memory flag and the launcher's
  // already-consumed marker).
  const consumeHydrationTurnMarker = (): { at: number } | null => {
    if (!workspaceId) return null
    const p = hydrationTurnMarkerPath()
    if (!existsSync(p)) return null
    let at = 0
    try {
      at = Number((JSON.parse(readFileSync(p, 'utf8')) as { at?: number }).at ?? 0)
    } catch {}
    try { unlinkSync(p) } catch {}
    if (!Number.isFinite(at) || at <= 0 || Date.now() - at > HYDRATION_TTL_MS) return null
    return { at }
  }

  // Did this workstream emit an explicitly review-worthy workspace update
  // since the hydration marker was written? This is the durable "the startup
  // run actually produced something the owner must review" signal — the agent
  // declares it via emit-workspace-update.py, instead of us guessing from
  // titles or timing.
  const turnEmittedReviewSignal = (sinceMs: number, workspaceTitle: string): boolean => {
    const slug = workstreamSlugFromContext(workspaceTitle, REPO_ROOT)
      ?? workstreamSlugFromContext(process.cwd(), REPO_ROOT)
    if (!slug) return false
    try {
      const update = readWorkspaceUpdate(slug)
      if (!update) return false
      const ts = Date.parse(update.timestamp ?? '')
      // Update timestamps are second-granularity (emit-workspace-update.py);
      // the marker's `at` is epoch-ms. Floor to the second so an update
      // emitted in the same second as the seed still counts as "after".
      if (!Number.isFinite(ts) || ts < Math.floor(sinceMs / 1000) * 1000) return false
      const status = String(update.status ?? '').toLowerCase()
      if (HYDRATION_ESCALATION_STATUSES.has(status)) return true
      // A hydration-turn "to review" counts only when the agent explicitly
      // declared a concrete review artifact (--review-artifact on
      // emit-workspace-update.py); plain "to review" stays startup noise.
      if (status === 'to review' && update.review_artifact === true) return true
      if ((update.new_actions ?? []).some(a => a?.requires_user === true)) return true
      return false
    } catch {
      return false
    }
  }

  const currentCustomColor = async (): Promise<string | null> => {
    if (!workspaceId) return null
    const r = await run(cmuxBin(), ['rpc', 'workspace.list', '{}'], { timeoutMs: 5000 })
    if (r.code !== 0) return null
    try {
      const parsed = JSON.parse(r.stdout) as { workspaces?: Array<{ id?: string; custom_color?: string | null }> }
      const ws = (parsed.workspaces ?? []).find(w => w.id === workspaceId)
      return ws?.custom_color ?? null
    } catch {
      return null
    }
  }

  const pinDebounceMs = (): number => {
    const envRaw = process.env.REVIEW_LANE_PIN_DEBOUNCE_MS
    if (envRaw !== undefined) {
      const env = Number(envRaw)
      if (Number.isFinite(env) && env >= 0) return env
    }
    const v = Number(config[`${PLUGIN}.pin_debounce_ms`])
    return Number.isFinite(v) && v >= 0 ? v : DEFAULT_PIN_DEBOUNCE_MS
  }

  // Deferred review pin (universal debounce). Cancelled by the next
  // agent.start (user responded) or replaced by a later agent.end.
  let pendingPinTimer: ReturnType<typeof setTimeout> | null = null

  const cancelPendingPin = () => {
    if (pendingPinTimer !== null) {
      clearTimeout(pendingPinTimer)
      pendingPinTimer = null
    }
  }

  const isReviewColor = (color: string | null): boolean => (color ?? '').toLowerCase() === REVIEW_COLOR

  // Paint the workspace purple: the sidebar's "To review" (unread) fact.
  // The marker file records that THIS plugin set the hold (diagnostics and
  // the reconcile below); it is not consulted by the publisher any more.
  const setColorPin = async () => {
    if (!workspaceId) return
    const current = await currentCustomColor()
    if (!existsSync(pinMarkerPath())) {
      mkdirSync(STATE_DIR, { recursive: true })
      const savedColor = isReviewColor(current) ? null : current ?? null
      writeFileSync(pinMarkerPath(), JSON.stringify({ savedColor, at: Date.now() }))
    }
    await run(cmuxBin(), ['workspace-action', '--action', 'set-color', '--color', REVIEW_COLOR, '--workspace', workspaceId], { timeoutMs: 5000 })
  }

  // The user's explicit Mark read (sidebar chip / context menu) clears the
  // purple. When the live color is no longer purple, the hold is over: drop
  // the stale marker so nothing can ever "restore" it. Never touches color.
  const reconcilePinMarker = async (): Promise<void> => {
    if (!workspaceId) return
    const p = pinMarkerPath()
    if (!existsSync(p)) return
    const current = await currentCustomColor()
    if (isReviewColor(current)) return
    try { unlinkSync(p) } catch {}
  }

  // Router-only: Router N workspaces are background dispatch utilities that
  // must never carry review state. Clears the native lane override, the
  // purple, and the marker.
  const releaseRouterReviewState = async (): Promise<void> => {
    if (!workspaceId) return
    if (await currentOverrideIsReview()) await setLane('auto')
    const current = await currentCustomColor()
    if (isReviewColor(current)) {
      await run(cmuxBin(), ['workspace-action', '--action', 'clear-color', '--workspace', workspaceId], { timeoutMs: 5000 })
    }
    try { unlinkSync(pinMarkerPath()) } catch {}
  }

  // Worker tab: no review state, no unread count. Clears any lane override /
  // purple that reached the tab, and at turn end marks the workspace read so
  // the sidebar never shows a count on it (the ⚡ running glyph is the only
  // indicator a worker tab carries).
  const settleWorkerTab = async (ctx: { logger: { log: (m: string) => void } }, phase: 'start' | 'end'): Promise<void> => {
    cancelPendingPin()
    hydrationTurnAt = null
    clearNormalTurnMarker()
    consumeHydrationMarker()
    consumeHydrationTurnMarker()
    try {
      await releaseRouterReviewState()
      if (phase === 'end' && workspaceId) {
        await run(cmuxBin(), ['workspace-action', '--action', 'mark-read', '--workspace', workspaceId], { timeoutMs: 5000 })
      }
      ctx.logger.log(`[${PLUGIN}] worker tab (${phase}): stays in Workers; review state cleared${phase === 'end' ? ', marked read' : ''}`)
    } catch {}
  }

  // User responded (typed a prompt): this is NOT "read". The purple hold on
  // an earlier result stays until the user explicitly marks it read; the
  // sidebar shows ⚡ for the running turn without moving the row. Only a
  // deferred pin from the previous turn is cancelled (the user replied
  // before it landed). The native lane override is released so cmux's own
  // sidebar inference resumes — the switchboard sidebar does not read it.
  // Machine-seeded hydration turns are NOT a user response either.
  amp.on('agent.start', async (_event: AgentStartEvent, ctx) => {
    if (!isEnabled(config) || isHeadless()) return

    const { title: workspaceTitle, description } = await currentWorkspace()
    if (isWorkerSession(description)) {
      await settleWorkerTab(ctx, 'start')
      return
    }
    if (isRouterTitle(workspaceTitle)) {
      cancelPendingPin()
      hydrationTurnAt = null
      clearNormalTurnMarker()
      consumeHydrationMarker()
      consumeHydrationTurnMarker()
      try {
        await releaseRouterReviewState()
        ctx.logger.log(`[${PLUGIN}] Router workspace stays in Background; review state cleared`)
      } catch {}
      return
    }

    const marker = consumeHydrationMarker()
    if (marker) {
      clearNormalTurnMarker()
      hydrationTurnAt = marker.at
      // Re-persist as a turn-scoped marker so child processes of this turn
      // (emit-workspace-update.py) can see the hydration provenance;
      // agent.end deletes it.
      try {
        mkdirSync(STATE_DIR, { recursive: true })
        writeFileSync(hydrationTurnMarkerPath(), JSON.stringify({ at: marker.at }))
      } catch {}
      ctx.logger.log(`[${PLUGIN}] startup hydration turn began; preserving existing review state`)
      return
    }

    // A deferred pin from the previous turn is cancelled outright — the user
    // replied before it landed, so that turn was never "unread". An existing
    // purple hold is left alone: typing is not reading (unread contract).
    cancelPendingPin()
    beginNormalTurn(workspaceTitle)
    try {
      if (await currentOverrideIsReview()) {
        await setLane('auto')
        ctx.logger.log(`[${PLUGIN}] released native lane override (user responded); purple hold untouched`)
      }
      await reconcilePinMarker()
    } catch {}
  })

  // Turn finished: pin only when its result remains reviewable. Startup
  // hydration without an escalation stays in its current lane. A normal turn
  // that explicitly settled without a review signal releases stale review
  // state and stays on auto. Other results keep the existing review behavior.
  amp.on('agent.end', async (event: AgentEndEvent, ctx) => {
    if (!isEnabled(config) || isHeadless() || !workspaceId) return
    if (event.status !== 'done' && event.status !== 'error') return

    const { title: workspaceTitle, description } = await currentWorkspace()
    if (isWorkerSession(description)) {
      await settleWorkerTab(ctx, 'end')
      return
    }
    if (isRouterTitle(workspaceTitle)) {
      cancelPendingPin()
      hydrationTurnAt = null
      clearNormalTurnMarker()
      consumeHydrationMarker()
      consumeHydrationTurnMarker()
      try {
        await releaseRouterReviewState()
        ctx.logger.log(`[${PLUGIN}] Router workspace finished; leaving review lane unset`)
      } catch {}
      return
    }

    // Fall back to the marker files in case the plugin loaded mid-turn and
    // missed the matching agent.start: first the launcher's marker, then the
    // turn-scoped copy persisted by an earlier plugin instance's agent.start.
    const turnMarker = normalTurn ?? consumeNormalTurnMarker()
    clearNormalTurnMarker()
    const since = hydrationTurnAt ?? consumeHydrationMarker()?.at ?? consumeHydrationTurnMarker()?.at ?? null
    hydrationTurnAt = null
    // The hydration turn is over; its turn-scoped provenance marker must not
    // leak into later genuine turns.
    try { unlinkSync(hydrationTurnMarkerPath()) } catch {}
    if (since !== null && !turnEmittedReviewSignal(since, workspaceTitle)) {
      ctx.logger.log(`[${PLUGIN}] hydration turn finished with no reviewable output; leaving lane unchanged`)
      return
    }
    if (since !== null) {
      ctx.logger.log(`[${PLUGIN}] hydration turn emitted a review-worthy update; pinning to review`)
    }
    if (since === null && turnMarker && turnEmittedSettledNonReview(turnMarker)) {
      // Not new reviewable output: no pin. An older purple hold the user has
      // not marked read yet is NOT cleared (only Mark read clears it).
      cancelPendingPin()
      try {
        if (await currentOverrideIsReview()) await setLane('auto')
        await reconcilePinMarker()
        ctx.logger.log(`[${PLUGIN}] turn emitted a settled non-review update; no review pin`)
      } catch {}
      return
    }

    // Snapshot the hold state at turn end. If the row was purple now and the
    // user marks it read before the deferred pin fires, the pin must NOT
    // repaint it: the user acknowledged this completion (it was already on
    // screen). If the row was not purple at turn end, the pin lands
    // regardless — the user cannot have read output that had no hold yet.
    const heldAtEnd = isReviewColor(await currentCustomColor())

    const pinReview = async (why: string) => {
      try {
        if (heldAtEnd && !isReviewColor(await currentCustomColor())) {
          try { unlinkSync(pinMarkerPath()) } catch {}
          ctx.logger.log(`[${PLUGIN}] skipped review pin (${why}): user marked read after turn end`)
          return
        }
        await setLane('review')
        await setColorPin()
        ctx.logger.log(`[${PLUGIN}] pinned workspace to review (${why})`)
      } catch {}
    }

    // Universal debounce: every finished turn defers its pin — if the user
    // responds before the timer fires, agent.start cancels it and the row
    // never bounces lanes. With many concurrent agents this also stops the
    // sidebar re-sorting on every turn end.
    cancelPendingPin()
    const delay = pinDebounceMs()
    if (delay > 0) {
      ctx.logger.log(`[${PLUGIN}] deferring review pin ${delay}ms (turn ${event.status})`)
      pendingPinTimer = setTimeout(() => {
        pendingPinTimer = null
        void pinReview(`deferred, turn ${event.status}`)
      }, delay)
      return
    }

    await pinReview(`turn ${event.status}`)
  })
}
