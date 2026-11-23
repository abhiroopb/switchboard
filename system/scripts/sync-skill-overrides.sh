#!/usr/bin/env bash
# Sync tracked skill overrides <-> the runtime skill tree (~/.agents/skills).
#
# Remote-baseline skills are overwritten on reinstall; every local edit to one
# of their files must live in system/skill-overrides/ (tracked in switchboard) and be
# re-applied idempotently. See system/skill-overrides/README.md.
#
# Usage:
#   sync-skill-overrides.sh            # install: tracked -> runtime
#   sync-skill-overrides.sh --check    # report drift, write nothing (exit 1 on drift)
#   sync-skill-overrides.sh --capture  # capture: runtime -> tracked
set -euo pipefail

SWITCHBOARD="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OVERRIDES="$SWITCHBOARD/system/skill-overrides"
RUNTIME="$HOME/.agents/skills"

MODE="install"
case "${1:-}" in
  --check) MODE="check" ;;
  --capture) MODE="capture" ;;
  "") ;;
  *) echo "usage: $0 [--check|--capture]" >&2; exit 2 ;;
esac

# One relative path per override file (mirrors ~/.agents/skills/).
FILES=(
  "todo/SKILL.md"
  "todo/SETUP.md"
  "todo/scripts/todo_cli.py"
  "todo/references/schema.md"
)
# Note: chief-of-staff, running-uxr-ops, and drafting-docs are no longer
# overrides. They are repo-owned whole-skill copies in switchboard .agents/skills/,
# copied into ~/.claude/skills by sync-skills.py, which also runs this
# script's install direction before every copy.

drift=0
skipped=0   # a skill this machine does not carry: not stale, nothing to patch (exit 3)
for rel in "${FILES[@]}"; do
  tracked="$OVERRIDES/$rel"
  runtime="$RUNTIME/$rel"
  # Active Gmail consumers still use the legacy CLI. The modern installed
  # skill has no gmail-cli.py; installing there would not fix those callers.
  if [[ "$rel" == "gmail/gmail-cli.py" ]]; then
    runtime="$HOME/.agents/skills/$rel"
  elif [[ "$rel" == "gdrive/legacy-gdrive-cli.py" ]]; then
    runtime="$HOME/.agents/skills/gdrive/gdrive-cli.py"
  fi
  label=$(printf "%-40s" "$rel")

  case "$MODE" in
    check)
      if [[ ! -f "$tracked" ]]; then echo "  $label ✗ missing tracked override"; drift=1
      elif [[ ! -f "$runtime" ]]; then echo "  $label ✗ missing runtime file (skill not installed?)"; drift=1
      elif cmp -s "$tracked" "$runtime"; then echo "  $label ✓ in sync"
      else echo "  $label ⚠ drift (runtime differs from tracked)"; drift=1
      fi
      ;;
    install)
      if [[ ! -f "$tracked" ]]; then echo "  $label ✗ missing tracked override — skipping" >&2; drift=1; continue; fi
      if [[ ! -d "$(dirname "$runtime")" ]]; then echo "  $label ✗ skill not installed — skipping" >&2; skipped=1; continue; fi
      if cmp -s "$tracked" "$runtime"; then echo "  $label ✓ already applied"; continue; fi
      # Warn loudly if we're about to overwrite an upstream/runtime change:
      # anything in the runtime file that isn't in the tracked override is lost.
      if [[ -f "$runtime" ]]; then
        echo "  $label ⚠ runtime differs — overwriting with tracked override (diff was:)"
        diff -u "$runtime" "$tracked" | head -40 || true
      fi
      tmp="$(dirname "$runtime")/.$(basename "$runtime").sync.$$"
      cp "$tracked" "$tmp" && mv "$tmp" "$runtime"
      echo "  $label ✓ installed"
      ;;
    capture)
      if [[ ! -f "$runtime" ]]; then echo "  $label ✗ missing runtime file — skipping" >&2; drift=1; continue; fi
      mkdir -p "$(dirname "$tracked")"
      if [[ -f "$tracked" ]] && cmp -s "$runtime" "$tracked"; then echo "  $label ✓ already captured"; continue; fi
      cp "$runtime" "$tracked"
      echo "  $label ✓ captured"
      ;;
  esac
done

# auto-pilot's routing table is amended in place rather than overridden whole
# (upstream keeps adding routes). An upstream launchd job replaces the file on a
# schedule, so the install direction re-applies the amendments
# every time it runs (start-day.sh, install-amp-skills.sh, hourly task).
if [[ "$MODE" == "install" && -f "$SWITCHBOARD/system/scripts/apply-auto-pilot-routes.sh" ]]; then
  if bash "$SWITCHBOARD/system/scripts/apply-auto-pilot-routes.sh" >/dev/null; then
    echo "  $(printf "%-40s" "auto-pilot/references/routing-tables.md") ✓ amendments applied"
  else
    echo "  $(printf "%-40s" "auto-pilot/references/routing-tables.md") ✗ amendments failed" >&2; drift=1
  fi
fi

if [[ "$drift" -ne 0 ]]; then exit 1; fi
if [[ "$skipped" -ne 0 ]]; then exit 3; fi
exit 0
