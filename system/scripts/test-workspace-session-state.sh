#!/usr/bin/env bash
# test-workspace-session-state.sh — focused tests for the shared session
# classifier behind CoS launch-failure recovery and routed-task outcome
# verification (2026-07-29).
#
# Fixtures reproduce the two real incidents:
#   workspace:85 — live Running thread that route-to-workspace tried to /exit
#                  and then hard-failed on             -> classification: active
#   workspace:88 — worker went Idle mid-task without a terminal workstream
#                  update (stopped after writing /tmp file) -> paused-mid-task
#
# Hermetic: fixture JSON files, no cmux calls.
# Run: bash system/scripts/test-workspace-session-state.sh

set -uo pipefail

REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/ws-session-state-test.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT

CLASSIFIER="$REAL_REPO/system/scripts/workspace-session-state.py"

PASS=0
FAIL=0
ok()   { echo "ok   - $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL - $1"; FAIL=$((FAIL + 1)); }

# --- Fixtures -----------------------------------------------------------------
# Complete live inventory across windows; workspace:99 has no agent.
cat > "$FIXTURE/workspaces.json" <<'JSON'
{"workspaces": [
  {"ref": "workspace:85", "id": "WID-85", "custom_title": "📮 Pm Os Reliability"},
  {"ref": "workspace:88", "id": "WID-88", "custom_title": "🧱 Volunteer Roster"},
  {"ref": "workspace:77", "id": "WID-77", "custom_title": "✅ Done Stream"},
  {"ref": "workspace:20", "id": "WID-20", "custom_title": "🛠️ Review Stream"},
  {"ref": "workspace:66", "id": "WID-66", "custom_title": "🧱 Blocked Stream"},
  {"ref": "workspace:61", "id": "WID-61", "custom_title": "👀 Needs Review Stream"},
  {"ref": "workspace:62", "id": "WID-62", "custom_title": "⚠️ Needs Action Stream"},
  {"ref": "workspace:99", "id": "WID-99", "custom_title": "🗒 Plain Terminal"}
]}
JSON

# Session store: workspace:85 Running; workspace:88 (OTHER window, absent from
# workspace.list) has a bound thread and no Running status = idle.
cat > "$FIXTURE/session.json" <<'JSON'
{"windows": [
  {"tabManager": {"workspaces": [
    {"workspaceId": "WID-85",
     "customTitle": "📮 Pm Os Reliability",
     "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0085-live"}}}],
     "statusEntries": [{"key": "amp", "value": "Running", "timestamp": 1785364113.0}]}
  ]}},
  {"tabManager": {"workspaces": [
    {"workspaceId": "WID-88",
     "customTitle": "🧱 Volunteer Roster",
     "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0088-paused"}}}],
     "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]}
  ]}}
]}
JSON

# Workstream updates: pm-os-reliability still active mid-task; team-calendar
# also still active (the worker stopped WITHOUT emitting a terminal update);
# done-stream settled; blocked-stream explicitly blocked.
cat > "$FIXTURE/updates.json" <<'JSON'
{"updated_at": "2026-07-29T00:00:00Z", "updates": {
  "workstream:pm-os-reliability": {"status": "active", "timestamp": "2026-07-29T00:00:00Z"},
  "workstream:team-calendar": {"status": "active", "timestamp": "2026-07-29T00:00:00Z"},
  "workstream:done-stream":       {"status": "waiting", "timestamp": "2026-07-29T00:00:00Z"},
  "workstream:review-stream":     {"status": "to review", "timestamp": "2026-09-08T17:18:10Z"},
  "workstream:blocked-stream":    {"status": "blocked", "timestamp": "2026-07-29T00:00:00Z"},
  "workstream:needs-review-stream": {"status": "needs review", "timestamp": "2026-09-08T17:18:10Z"},
  "workstream:needs-action-stream": {"status": "needs action", "timestamp": "2026-09-08T17:18:10Z"}
}}
JSON

# Per-test overrides (e.g. CC_CMUX_SESSION_JSON=... run_classifier ...) must
# win over the defaults, so use :- fallbacks instead of hard assignments.
run_classifier() {
    CC_WORKSPACES_JSON="${CC_WORKSPACES_JSON:-$FIXTURE/workspaces.json}" \
    CC_CMUX_SESSION_JSON="${CC_CMUX_SESSION_JSON:-$FIXTURE/session.json}" \
    PM_OS_WORKSPACE_UPDATES_JSON="${PM_OS_WORKSPACE_UPDATES_JSON:-$FIXTURE/updates.json}" \
    REPO_ROOT="$REAL_REPO" \
        python3 "$CLASSIFIER" "$@" 2>/dev/null
}

field() { python3 -c "import json,sys; print(json.load(sys.stdin).get('$1') or '')"; }

# --- workspace:85 regression: live Running thread -> active --------------------
out="$(run_classifier --workspace workspace:85 --workstream pm-os-reliability)"
[[ "$(printf '%s' "$out" | field classification)" == "active" ]] \
    && ok "workspace:85 fixture (Running thread) classifies active" \
    || fail "workspace:85 fixture classifies active (got: $out)"
[[ "$(printf '%s' "$out" | field thread_id)" == "T-0085-live" ]] \
    && ok "workspace:85 fixture reports the bound thread ID" \
    || fail "workspace:85 fixture reports the bound thread ID (got: $out)"

# --- workspace:88 regression: Idle + update still active -> paused-mid-task ----
# Workspace:88 belongs to another live window in the complete inventory.
out="$(run_classifier --workstream team-calendar)"
[[ "$(printf '%s' "$out" | field classification)" == "paused-mid-task" ]] \
    && ok "workspace:88 fixture (Idle, no terminal update) classifies paused-mid-task" \
    || fail "workspace:88 fixture classifies paused-mid-task (got: $out)"
[[ "$(printf '%s' "$out" | field thread_id)" == "T-0088-paused" ]] \
    && ok "multi-window inventory resolves the thread via workspace UUID" \
    || fail "multi-window inventory resolves the thread (got: $out)"

# --- Idle + settled update -> idle-complete -------------------------------------
cat > "$FIXTURE/session2.json" <<'JSON'
{"windows": [{"tabManager": {"workspaces": [
  {"workspaceId": "WID-77", "customTitle": "✅ Done Stream",
   "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0077-done"}}}],
   "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]}
]}}]}
JSON
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session2.json" run_classifier --workstream done-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "idle-complete" ]] \
    && ok "Idle + waiting update classifies idle-complete" \
    || fail "Idle + waiting update classifies idle-complete (got: $out)"

# --- Idle + "to review" update -> idle-complete (2026-09-08 false needs_human) --
# ws20 regression: the worker finished and emitted the taught `--status "to
# review"`; the sweep classified it paused-mid-task every 10 minutes.
cat > "$FIXTURE/session2r.json" <<'JSON'
{"windows": [{"tabManager": {"workspaces": [
  {"workspaceId": "WID-20", "customTitle": "🛠️ Review Stream",
   "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0020-review"}}}],
   "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]}
]}}]}
JSON
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session2r.json" run_classifier --workstream review-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "idle-complete" ]] \
    && ok "Idle + 'to review' update classifies idle-complete (finished, awaiting the owner)" \
    || fail "Idle + 'to review' update classifies idle-complete (got: $out)"

# --- Idle + blocked update -> blocked -------------------------------------------
cat > "$FIXTURE/session3.json" <<'JSON'
{"windows": [{"tabManager": {"workspaces": [
  {"workspaceId": "WID-66", "customTitle": "🧱 Blocked Stream",
   "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0066-blocked"}}}],
   "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]}
]}}]}
JSON
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session3.json" run_classifier --workstream blocked-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "blocked" ]] \
    && ok "Idle + blocked update classifies blocked" \
    || fail "Idle + blocked update classifies blocked (got: $out)"

# --- Idle + "needs review" / "needs action" -> blocked (2026-09-08 false paused) -
# A worker that stopped on purpose and escalated to the owner is not mid-task; the
# sweep must surface it, never nudge or relaunch it as paused-mid-task.
cat > "$FIXTURE/session4.json" <<'JSON'
{"windows": [{"tabManager": {"workspaces": [
  {"workspaceId": "WID-61", "customTitle": "👀 Needs Review Stream",
   "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0061-needs-review"}}}],
   "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]},
  {"workspaceId": "WID-62", "customTitle": "⚠️ Needs Action Stream",
   "panels": [{"terminal": {"agent": {"kind": "amp", "sessionId": "T-0062-needs-action"}}}],
   "statusEntries": [{"key": "amp", "value": "Idle", "timestamp": 1785363778.0}]}
]}}]}
JSON
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session4.json" run_classifier --workstream needs-review-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "blocked" ]] \
    && ok "Idle + 'needs review' update classifies blocked (escalated, not paused)" \
    || fail "Idle + 'needs review' update classifies blocked (got: $out)"
[[ "$(printf '%s' "$out" | field update_status)" == "needs review" ]] \
    && ok "'needs review' classification keeps the native update_status for the blocker detail" \
    || fail "'needs review' keeps update_status (got: $out)"
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session4.json" run_classifier --workstream needs-action-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "blocked" ]] \
    && ok "Idle + 'needs action' update classifies blocked (escalated, not paused)" \
    || fail "Idle + 'needs action' update classifies blocked (got: $out)"

# --- Claude Code tabs (2026-11-02 Amp-to-Claude migration) ----------------------
# Live store shape: agent.kind "claude", sessionId == resumeBinding.checkpointId,
# statusEntries key "claude_code". thread_id must carry the session UUID so the
# launcher can `claude --resume` it and provenance can verify it.
cat > "$FIXTURE/session5.json" <<'JSON'
{"windows": [{"tabManager": {"workspaces": [
  {"workspaceId": "WID-85", "customTitle": "📮 Pm Os Reliability",
   "panels": [{"terminal": {
      "agent": {"kind": "claude", "sessionId": "0b7c9d2e-1111-4222-8333-444455556666", "permissionMode": "bypassPermissions"},
      "resumeBinding": {"kind": "claude", "checkpointId": "0b7c9d2e-1111-4222-8333-444455556666", "autoResume": true}}}],
   "statusEntries": [{"key": "claude_code", "value": "Idle", "timestamp": 1785363778.0}]},
  {"workspaceId": "WID-88", "customTitle": "🧱 Volunteer Roster",
   "panels": [{"terminal": {
      "agent": {"kind": "claude"},
      "resumeBinding": {"kind": "claude", "checkpointId": "9f9f9f9f-aaaa-4bbb-8ccc-dddddddddddd"}}}],
   "statusEntries": [{"key": "claude_code", "value": "Running", "timestamp": 1785364113.0}]}
]}}]}
JSON
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session5.json" run_classifier --workspace workspace:85 --workstream pm-os-reliability)"
[[ "$(printf '%s' "$out" | field classification)" == "paused-mid-task" ]] \
    && ok "Claude tab: claude_code Idle + active update classifies paused-mid-task" \
    || fail "Claude tab Idle classifies paused-mid-task (got: $out)"
[[ "$(printf '%s' "$out" | field thread_id)" == "0b7c9d2e-1111-4222-8333-444455556666" ]] \
    && ok "Claude tab: thread_id carries the session UUID" \
    || fail "Claude tab thread_id carries the session UUID (got: $out)"
[[ "$(printf '%s' "$out" | field agent_kind)" == "claude" ]] \
    && ok "Claude tab: agent_kind reports claude" \
    || fail "Claude tab agent_kind reports claude (got: $out)"
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/session5.json" run_classifier --workstream team-calendar)"
[[ "$(printf '%s' "$out" | field classification)" == "active" ]] \
    && ok "Claude tab: claude_code Running classifies active" \
    || fail "Claude tab Running classifies active (got: $out)"
[[ "$(printf '%s' "$out" | field thread_id)" == "9f9f9f9f-aaaa-4bbb-8ccc-dddddddddddd" ]] \
    && ok "Claude tab: falls back to resumeBinding.checkpointId when agent.sessionId is absent" \
    || fail "Claude tab checkpointId fallback (got: $out)"
out="$(run_classifier --workspace workspace:85)"
[[ "$(printf '%s' "$out" | field agent_kind)" == "amp" ]] \
    && ok "Amp tab: agent_kind reports amp" \
    || fail "Amp tab agent_kind reports amp (got: $out)"

# --- No agent bound -> unreachable ----------------------------------------------
out="$(run_classifier --workspace workspace:99)"
[[ "$(printf '%s' "$out" | field classification)" == "unreachable" ]] \
    && ok "workspace without an Amp agent classifies unreachable" \
    || fail "workspace without an Amp agent classifies unreachable (got: $out)"

out="$(run_classifier --workstream no-such-stream)"
[[ "$(printf '%s' "$out" | field classification)" == "unreachable" ]] \
    && ok "unknown workstream classifies unreachable" \
    || fail "unknown workstream classifies unreachable (got: $out)"

# --- Corrupt session store degrades to unreachable, never crashes ---------------
echo '{broken' > "$FIXTURE/corrupt.json"
out="$(CC_CMUX_SESSION_JSON="$FIXTURE/corrupt.json" run_classifier --workspace workspace:85)"
[[ "$(printf '%s' "$out" | field classification)" == "unreachable" ]] \
    && ok "corrupt session store degrades to unreachable" \
    || fail "corrupt session store degrades to unreachable (got: $out)"

# --- Summary --------------------------------------------------------------------
echo '{"workspaces": []}' > "$FIXTURE/empty.json"
out="$(CC_WORKSPACES_JSON="$FIXTURE/empty.json" run_classifier --workstream pm-os-reliability)"
[[ "$(printf '%s' "$out" | field classification)" == "unreachable" ]] \
    && ok "cached Running entry cannot resurrect a vanished workspace" \
    || fail "cached Running entry resurrected a vanished workspace (got: $out)"
if CC_WORKSPACES_JSON="$FIXTURE/corrupt.json" run_classifier --workspace workspace:85 >/dev/null; then
    fail "failed inventory must not look like an absent workspace"
else
    ok "failed inventory returns nonzero rather than proving absence"
fi

echo
echo "workspace-session-state: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
