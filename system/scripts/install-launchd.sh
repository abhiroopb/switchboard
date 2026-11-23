#!/usr/bin/env bash
# Install (or remove) the switchboard launchd agents shipped in system/launchd/.
#
# Usage: bash system/scripts/install-launchd.sh [--dry-run] [--uninstall] [label ...]
#   With no labels, every com.switchboard.*.plist in system/launchd/ is installed.
#   Idempotent: an already-loaded agent is bootout'ed and bootstrapped again
#   so edits to the plist take effect.
#
# The plists are rendered at install time (install.py) with your SWITCHBOARD_ROOT and
# HOME, so run this from the installed checkout, not from the kit clone.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$(uname -s)" in
  Darwin) ;;
  Linux)
    cat >&2 <<EOF
launchd exists only on macOS. On Linux the kit scope is policy and skills plus
Claude Code memory. The claude-mem worker is a systemd user service written by:
  bash system/scripts/install-claude-code.sh
The six Amp dispatch loops are macOS only (see docs/install-decisions.md). To run
one by hand on Linux, copy its ProgramArguments from system/launchd/*.plist into
a systemd user timer or a cron line.
EOF
    exit 0 ;;
  *)
    cat >&2 <<EOF
launchd exists only on macOS. On Windows (native Git Bash + wmux) register the
same loops as Task Scheduler tasks from PowerShell:
  powershell -ExecutionPolicy Bypass -File system\\windows\\register-scheduled-tasks.ps1
See $ROOT/system/windows/README.md.
EOF
    exit 0 ;;
esac
SRC="$ROOT/system/launchd"
DEST="$HOME/Library/LaunchAgents"
dry_run=false
uninstall=false
labels=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    --uninstall) uninstall=true ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) labels+=("$arg") ;;
  esac
done

if (( ${#labels[@]} == 0 )); then
  for p in "$SRC"/com.switchboard.*.plist; do
    labels+=("$(basename "$p" .plist)")
  done
fi

if grep -rl '{{SWITCHBOARD_ROOT}}\|{{HOME}}' "$SRC" >/dev/null 2>&1; then
  echo "plists still contain install placeholders; run install.py first" >&2
  exit 1
fi

mkdir -p "$DEST" "$ROOT/system/state/logs" "$ROOT/system/logs"
uid="$(id -u)"
for label in "${labels[@]}"; do
  src="$SRC/$label.plist"
  dest="$DEST/$label.plist"
  if [[ ! -f "$src" ]]; then echo "no such plist: $src" >&2; exit 1; fi
  if $uninstall; then
    echo "bootout $label"
    $dry_run || { launchctl bootout "gui/$uid/$label" 2>/dev/null || true; rm -f "$dest"; }
    continue
  fi
  echo "install $label"
  if $dry_run; then continue; fi
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  cp "$src" "$dest"
  launchctl bootstrap "gui/$uid" "$dest"
done

$dry_run || launchctl list | grep -E 'com\.switchboard\.' || true
