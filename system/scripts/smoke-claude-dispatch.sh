#!/usr/bin/env bash
# End-to-end smoke test of the Claude dispatch path.
# Dispatches no-op tasks to the test workstream pm-os-smoke and checks launch,
# steer, report, reap, and the owner-tab rule. Details: smoke_claude_dispatch.py.
# Usage: bash system/scripts/smoke-claude-dispatch.sh [--no-owner] [--timeout-min N]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec python3 system/scripts/smoke_claude_dispatch.py "$@"
