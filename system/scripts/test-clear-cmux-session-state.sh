#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/system/scripts/clear-cmux-session-state.sh"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/cmux-session-clear-test.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT

session_dir="$FIXTURE/session"
resume_dir="$FIXTURE/resume"
mkdir -p "$session_dir" "$resume_dir"

touch \
  "$session_dir/session-test.bundle.json" \
  "$session_dir/session-test.bundle-previous.json" \
  "$session_dir/session-other.bundle.json" \
  "$resume_dir/one.zsh" \
  "$resume_dir/keep.txt"

CMUX_SESSION_DIR="$session_dir" \
CMUX_BUNDLE_ID="test.bundle" \
CMUX_APP_EXECUTABLE="$FIXTURE/not-running" \
CMUX_RESUME_DIR="$resume_dir" \
  bash "$SCRIPT" >/dev/null

[[ ! -e "$session_dir/session-test.bundle.json" ]]
[[ ! -e "$session_dir/session-test.bundle-previous.json" ]]
[[ -e "$session_dir/session-other.bundle.json" ]]
[[ ! -e "$resume_dir/one.zsh" ]]
[[ -e "$resume_dir/keep.txt" ]]

echo "✅ clear-cmux-session-state: targeted restore state removed"
