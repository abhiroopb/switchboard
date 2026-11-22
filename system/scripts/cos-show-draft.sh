#!/usr/bin/env bash
# cos-show-draft.sh: resolve a draft handle the owner names in CoS ("send draft <id>",
# "show draft 2") to the command that prints it, on any machine.
#
#   bash system/scripts/cos-show-draft.sh r-1234567890123456789   # Gmail draft id
#   bash system/scripts/cos-show-draft.sh 2                       # Comms Triage "Draft 2"
#   bash system/scripts/cos-show-draft.sh draft-2
#
# A Gmail draft id (r-<digits>, what `pmos mail draft` and worker
# reports print) goes straight to `pmos mail show`, which prints To,
# Cc, Bcc, Subject, the body and the Send check that `send --expect-hash` needs.
# A ledger handle goes to triage_ledger.py show where that script exists. The
# Windows kit install ships no Comms Triage ledger, so there a ledger handle exits 3
# with a plain line.
# Exit: 0 printed, 2 usage, 3 not resolvable here, else the underlying command's code.
set -uo pipefail

handle="${1:-}"
if [[ -z "$handle" || "$handle" == "-h" || "$handle" == "--help" ]]; then
  sed -n '2,15p' "${BASH_SOURCE[0]}"
  [[ -z "$handle" ]] && exit 2 || exit 0
fi
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PY="${PM_OS_PYTHON:-python3}"

if [[ "$handle" =~ ^r-?[0-9]+$ ]]; then
  exec "${PM_OS_PMOS:-pmos}" mail show --draft-id "$handle"
fi

if [[ "$handle" =~ ^(draft-)?([0-9]+)$ ]]; then
  ledger="$root/system/scripts/triage_ledger.py"
  if [[ -f "$ledger" ]]; then
    exec "$PY" "$ledger" show --draft-id "draft-${BASH_REMATCH[2]}"
  fi
  echo "Draft ${BASH_REMATCH[2]}: no Comms Triage ledger on this machine (system/scripts/triage_ledger.py is not installed). Give the Gmail draft id (r-…) or the note that holds the draft." >&2
  exit 3
fi

echo "'$handle' is neither a Gmail draft id (r-<digits>) nor a ledger handle (N or draft-N)." >&2
exit 3
