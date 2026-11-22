#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-${CMUX_WORKSPACE_ID:-}}"
delay="${2:-5}"
cmux="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

if [[ -z "$workspace" ]]; then
  echo "CMUX_WORKSPACE_ID is not set" >&2
  exit 2
fi

if [[ ! -x "$cmux" ]]; then
  cmux="$(command -v cmux || true)"
fi

if [[ -z "$cmux" || ! -x "$cmux" ]]; then
  echo "cmux CLI not found" >&2
  exit 3
fi

nohup /bin/sh -c '
  sleep "$1"
  "$2" workspace-action --action unpin --workspace "$3" >/dev/null 2>&1 || true
  "$2" close-workspace --workspace "$3" >/dev/null 2>&1
' close-cmux "$delay" "$cmux" "$workspace" </dev/null >/dev/null 2>&1 &

echo "Scheduled cmux workspace $workspace to close in ${delay}s"
