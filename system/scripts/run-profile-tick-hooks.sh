#!/usr/bin/env bash
# run-profile-tick-hooks.sh: run the active organisation profile's per-tick jobs.
#
# Core switchboard holds no profile job. A profile that needs work on every
# command-center tick puts one script per job at
# profiles/<name>/overlay/system/scripts/profile-tick.d/<job>.sh; apply-profile.py
# copies it here only while that profile is active, so on any other machine the
# job does not exist and cannot run.
#
# Each hook gets REPO_ROOT and STATE_DIR in its environment and keeps its own
# gates, kill switch and log. Hooks run in name order. A failing hook is reported
# on stderr and the next one still runs; this script always exits 0, so it never
# breaks the loop.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_DIR="${STATE_DIR:-$REPO_ROOT/system/state}"

shopt -s nullglob
for hook in "$REPO_ROOT"/system/scripts/profile-tick.d/*.sh; do
  REPO_ROOT="$REPO_ROOT" STATE_DIR="$STATE_DIR" bash "$hook" \
    || echo "profile tick hook failed: $(basename "$hook")" >&2
done
exit 0
