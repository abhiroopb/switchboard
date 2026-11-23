#!/bin/sh
# switchboard kit installer entry point. Finds Python 3.11+ and hands off to install.py.
# Usage: sh install.sh --owner-name "Your Name" --owner-email you@example.com [--destination DIR] [--dry-run|--apply|--update]
# Works from macOS, Linux, WSL2 and Git Bash. From PowerShell run: py install.py ...
# Setup decisions (scope, Windows runtime, optional organization tools); owner choices (install folder, full loop, memory) are asked, see that file: docs/install-decisions.md
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
for executable in python3.13 python3.12 python3.11 python3 python py; do
  if command -v "$executable" >/dev/null 2>&1 &&
    "$executable" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$executable" "$HERE/install.py" "$@"
  fi
done
printf '%s\n' 'Python 3.11 or newer is required.' \
  '  macOS:   brew install python@3.12' \
  '  Windows: winget install Python.Python.3.12   (then: py install.py ...)' \
  '  Debian/Ubuntu/WSL: sudo apt install python3' >&2
exit 1
