#!/usr/bin/env bash
# ask-cos.sh: a worker's mid-task question goes to CoS, never into the worker tab
#. The worker keeps going on --default; the
# owner's answer comes back as a same-task steer. See ask_cos.py for the contract.
#
#   bash system/scripts/ask-cos.sh --task-id rt-123 --option "Keep the old name" \
#        --option "Rename it" --default a "Rename the workstream to X?"
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PM_OS_PYTHON:-python3}" "$here/ask_cos.py" ask "$@"
