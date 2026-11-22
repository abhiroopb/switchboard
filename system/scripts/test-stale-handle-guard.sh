#!/usr/bin/env bash
#
# test-stale-handle-guard.sh — regression tests for the 2026-08-05
# org-aware-reporting incident:
#
#   1. command-center-action.sh must refuse numeric handles whose workspace
#      UUID no longer matches the one recorded at digest render time
#      (cmux reuses workspace numbers, so `close 7` from a stale digest must
#      never close whatever now occupies workspace:7).
#   2. command-center-loop.sh must stamp the last good digest STALE when a
#      refresh fails, instead of silently serving outdated agent states.
#   3. emit-workspace-update.py auto-close must leave a workspace_autoclosed
#      event in events.jsonl so closes are never silent disappearances.
#
# Hermetic: stub cmux binary + temp state dir. Never touches live cmux.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACTION="$REPO_ROOT/system/scripts/command-center-action.sh"
LOOP="$REPO_ROOT/system/scripts/command-center-loop.sh"

FIXTURE="$(mktemp -d /tmp/stale-handle-test.XXXXXX)"
trap 'rm -rf "$FIXTURE"' EXIT

FAIL=0
check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [[ "$ok" == "0" ]]; then
    echo "  PASS  $name"
  else
    echo "  FAIL  $name  $detail"
    FAIL=1
  fi
}

# ---------------------------------------------------------------- stub cmux
mkdir -p "$FIXTURE/bin" "$FIXTURE/state"
CALL_LOG="$FIXTURE/cmux-calls.log"
LIVE_JSON="$FIXTURE/live-workspaces.json"
cat > "$FIXTURE/bin/cmux" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$CALL_LOG"
case "\$1" in
  rpc)
    [[ "\$2" == "workspace.list" ]] && cat "$LIVE_JSON"
    ;;
  list-workspaces)
    python3 - "$LIVE_JSON" <<'PY'
import json, sys
for w in json.load(open(sys.argv[1])).get("workspaces", []):
    print(f"  {w['ref']}  {w.get('custom_title') or w.get('title','')}")
PY
    ;;
  *) : ;;
esac
exit 0
EOF
chmod +x "$FIXTURE/bin/cmux"

set_live() {  # set_live <num> <uuid> <title> [...more triples]
  python3 - "$LIVE_JSON" "$@" <<'PY'
import json, sys
args = sys.argv[2:]
wss = []
for i in range(0, len(args), 3):
    n, uid, title = args[i], args[i+1], args[i+2]
    wss.append({"ref": f"workspace:{n}", "id": uid, "title": title,
                "custom_title": title, "pinned": False, "selected": False,
                "description": "", "latest_conversation_message": None})
json.dump({"workspaces": wss}, open(sys.argv[1], "w"))
PY
}

set_map() {  # set_map <num> <uuid> <title> [...more triples]
  python3 - "$FIXTURE/state/command-center-actions.json" "$@" <<'PY'
import json, sys
from datetime import datetime, timezone
args = sys.argv[2:]
handles = {}
for i in range(0, len(args), 3):
    n, uid, title = args[i], args[i+1], args[i+2]
    handles[n] = {"ref": f"workspace:{n}", "id": uid, "title": title}
json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
           "handles": handles}, open(sys.argv[1], "w"))
PY
}

# cmux semantics: numbers are reused, so a handle the render missed fails closed.
# A worker pane on Windows inherits PM_OS_MUX=wmux, where a miss refreshes the
# map instead (test_command_center_action_wmux.py), so pin the backend here.
run_action() {
  PM_OS_MUX=cmux PM_OS_TEST_SANDBOX=1 CMUX_BIN="$FIXTURE/bin/cmux" \
    CMUX="$FIXTURE/bin/cmux" CC_STATE_DIR="$FIXTURE/state" \
    bash "$ACTION" "$@" 2>&1
}

echo "== 1. matching provenance → action proceeds =="
set_live 7 uuid-multi-entity "📉 Multi-entity"
set_map 7 uuid-multi-entity "📉 Multi-entity"
: > "$CALL_LOG"
out="$(run_action open 7)"; rc=$?
check "open 7 succeeds on matching uuid" "$([[ $rc -eq 0 ]] && echo 0 || echo 1)" "rc=$rc $out"
grep -q "select-workspace --workspace workspace:7" "$CALL_LOG"
check "open 7 focused workspace:7" "$?"

echo "== 2. number reused by another workspace → fail closed =="
set_live 7 uuid-scheduled-jobs "🤖 Scheduled Jobs"
set_map 7 uuid-multi-entity "📉 Multi-entity"
: > "$CALL_LOG"
out="$(run_action close 7)"; rc=$?
check "close 7 refused on reused number" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc"
echo "$out" | grep -q "number reused"
check "refusal names the reuse" "$?" "$out"
grep -q "close-workspace" "$CALL_LOG" && reused_closed=1 || reused_closed=0
check "no workspace was closed" "$([[ $reused_closed -eq 0 ]] && echo 0 || echo 1)"
: > "$CALL_LOG"
out="$(run_action open 7)"; rc=$?
check "open 7 refused on reused number" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc"
grep -q "select-workspace" "$CALL_LOG" && focused=1 || focused=0
check "no workspace was focused" "$([[ $focused -eq 0 ]] && echo 0 || echo 1)"

echo "== 3. handle not offered by digest → fail closed =="
out="$(run_action wrap 42)"; rc=$?
check "wrap 42 refused (not in map)" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc"
echo "$out" | grep -q "does not offer"
check "refusal says handle unknown" "$?" "$out"

echo "== 4. workspace closed since render → fail closed =="
set_live 9 uuid-cos "🧭 CoS"
set_map 7 uuid-multi-entity "📉 Multi-entity"
out="$(run_action open 7)"; rc=$?
check "open 7 refused when workspace gone" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc"
echo "$out" | grep -q "no longer exists"
check "refusal says workspace gone" "$?" "$out"

echo "== 5. missing action map or identity → fail closed =="
rm -f "$FIXTURE/state/command-center-actions.json"
set_live 7 uuid-multi-entity "📉 Multi-entity"
: > "$CALL_LOG"
out="$(run_action open 7)"; rc=$?
check "open 7 refused without map" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc $out"
echo "$out" | grep -q "ERROR: no action map"
check "error emitted without map" "$?" "$out"
! grep -q "select-workspace" "$CALL_LOG"
check "missing map never focuses workspace" "$?"
for missing in expected live; do
  set_map 7 uuid-multi-entity "📉 Multi-entity"
  set_live 7 uuid-multi-entity "📉 Multi-entity"
  if [[ "$missing" == expected ]]; then
    set_map 7 "" "📉 Multi-entity"
  else
    set_live 7 "" "📉 Multi-entity"
  fi
  : > "$CALL_LOG"
  out="$(run_action close 7)"; rc=$?
  check "close refused without $missing UUID" "$([[ $rc -ne 0 ]] && echo 0 || echo 1)" "rc=$rc $out"
  ! grep -q "close-workspace" "$CALL_LOG"
  check "missing $missing UUID never closes workspace" "$?"
done

echo "== 6. loop stamps STALE banner when refresh fails =="
LOOP_STATE="$FIXTURE/loop-state"
mkdir -p "$LOOP_STATE/command-center-history"
LATEST="$LOOP_STATE/command-center-latest.md"
printf '# 🧭 CoS Command Center · old\n\n| 7 | 📉 Multi-entity | agent running | — | open 7 · close 7 |\n' > "$LATEST"
cat > "$FIXTURE/bin/fail-digest.py" <<'EOF'
import sys; sys.exit(3)
EOF
CMUX="$FIXTURE/bin/cmux" STATE_DIR="$LOOP_STATE" DIGEST_SCRIPT="$FIXTURE/bin/fail-digest.py" \
  bash "$LOOP" --once >/dev/null 2>&1
grep -q '^> ⚠️ STALE' "$LATEST"
check "STALE banner prepended on failed refresh" "$?" "$(head -1 "$LATEST")"
grep -q "Multi-entity" "$LATEST"
check "last good digest content preserved" "$?"
CMUX="$FIXTURE/bin/cmux" STATE_DIR="$LOOP_STATE" DIGEST_SCRIPT="$FIXTURE/bin/fail-digest.py" \
  bash "$LOOP" --once >/dev/null 2>&1
banner_count="$(grep -c '^> ⚠️ STALE' "$LATEST")"
check "banner is idempotent across repeated failures" "$([[ $banner_count -eq 1 ]] && echo 0 || echo 1)" "count=$banner_count"
cat > "$FIXTURE/bin/ok-digest.py" <<'EOF'
print("# fresh digest")
EOF
CMUX="$FIXTURE/bin/cmux" STATE_DIR="$LOOP_STATE" DIGEST_SCRIPT="$FIXTURE/bin/ok-digest.py" \
  bash "$LOOP" --once >/dev/null 2>&1
grep -q '^> ⚠️ STALE' "$LATEST" && still_stale=1 || still_stale=0
check "banner cleared by successful refresh" "$([[ $still_stale -eq 0 ]] && echo 0 || echo 1)" "$(head -1 "$LATEST")"

echo "== 7. auto-close leaves a workspace_autoclosed event =="
FAKE_ROOT="$FIXTURE/fake-repo"
mkdir -p "$FAKE_ROOT/system/state" "$FAKE_ROOT/workstreams/test-ws"
cat > "$FAKE_ROOT/system/cmux-helpers.sh" <<'EOF'
find_workspace_ref_for_source_key() { echo "workspace:7"; }
cmux_cmd() { :; }
close_workspace_ref_verified() { echo "$1"; }
EOF
python3 - "$REPO_ROOT" "$FAKE_ROOT" <<'PY'
import sys, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    "ewu", sys.argv[1] + "/system/scripts/emit-workspace-update.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.maybe_close_waiting_workspace(
    Path(sys.argv[2]), "workstream:test-ws", "waiting", close_workspace=True)
PY
grep -q '"type": "workspace_autoclosed"' "$FAKE_ROOT/system/state/events.jsonl" 2>/dev/null
check "workspace_autoclosed event recorded" "$?" "$(cat "$FAKE_ROOT/system/state/events.jsonl" 2>/dev/null)"
grep -q '"workspace_ref": "workspace:7"' "$FAKE_ROOT/system/state/events.jsonl" 2>/dev/null
check "event carries the closed ref" "$?"
grep -q '"source": "workstream:test-ws"' "$FAKE_ROOT/system/state/events.jsonl" 2>/dev/null
check "event carries the workstream source" "$?"

echo
if [[ "$FAIL" -ne 0 ]]; then
  echo "❌ stale-handle-guard tests FAILED"
  exit 1
fi
echo "✅ all stale-handle-guard tests passed"
