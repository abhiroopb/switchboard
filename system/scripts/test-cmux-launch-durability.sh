#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/cmux-launch-durability.XXXXXX")
trap 'rm -rf "$FIXTURE"' EXIT

export REPO_ROOT
export CMUX_WORKSPACE_LAUNCH_LOCK="$FIXTURE/workspace-launch"
export WS_LAUNCH_LOCK_TIMEOUT=0.1
# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

# Keep launch-result tests entirely inside the synthetic fixture.
launch_result_file_for() {
    printf '%s/launch-results/durability-test.json\n' "$FIXTURE"
}

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }

# A fresh ownerless directory may be an acquirer between mkdir and PID write.
mkdir "$WS_LAUNCH_LOCK_DIR"
if acquire_ws_launch_lock 2>/dev/null; then
    fail "fresh ownerless lock was reclaimed"
fi
[[ -d "$WS_LAUNCH_LOCK_DIR" ]] || fail "fresh ownerless lock was removed"
pass "fresh ownerless lock is preserved"

# Age alone cannot prove an old loaded launcher's ownerless lock is abandoned.
python3 - "$WS_LAUNCH_LOCK_DIR" <<'PY'
import os, sys, time
old = time.time() - 10
os.utime(sys.argv[1], (old, old))
PY
if acquire_ws_launch_lock 2>/dev/null; then
    fail "unverified old legacy lock was reclaimed"
fi
[[ -d "$WS_LAUNCH_LOCK_DIR" ]] || fail "old legacy lock was removed"
rmdir "$WS_LAUNCH_LOCK_DIR" # Only this synthetic fixture is removed.
pass "old legacy lock requires inspected cleanup"

acquire_ws_launch_lock || fail "kernel lock acquisition failed"
if (acquire_ws_launch_lock) 2>/dev/null; then
    fail "second holder entered while first held the lock"
fi
release_ws_launch_lock
(acquire_ws_launch_lock; release_ws_launch_lock) || fail "released lock remains busy"
pass "kernel lock excludes contenders and releases on descriptor close"

if (acquire_ws_launch_lock || exit 18; exit 17); then
    fail "synthetic failed owner unexpectedly succeeded"
else
    [[ $? -eq 17 ]] || fail "synthetic owner never acquired the lock"
fi
acquire_ws_launch_lock || fail "exited owner left a stale kernel lock"
release_ws_launch_lock
pass "owner exit releases the lock without cleanup code"

# A failed producer must leave the prior result intact and clean its temp file.
result_file=$(launch_result_file_for "Durability Test")
mkdir -p "$(dirname "$result_file")"
printf '%s\n' '{"sentinel":"old"}' > "$result_file"
python3() { printf '%s' '{"partial":'; return 9; }
export -f python3
if write_launch_result_file "Durability Test" fresh workspace:1 none sent; then
    fail "failed result producer reported success"
fi
unset -f python3
grep -q '"sentinel":"old"' "$result_file" || fail "failed publication replaced the prior result"
compgen -G "${result_file}.tmp.*" >/dev/null && fail "failed publication leaked a temp file"
pass "failed result publication preserves the prior JSON"

write_launch_result_file "Durability Test" fresh workspace:1 none sent "" "" verified
python3 - "$result_file" <<'PY'
import json, sys
with open(sys.argv[1]) as fh:
    result = json.load(fh)
assert result["workspace_ref"] == "workspace:1"
assert result["thread_provenance"] == "verified"
PY
compgen -G "${result_file}.tmp.*" >/dev/null && fail "successful publication leaked a temp file"
pass "result JSON is published atomically with provenance intact"
