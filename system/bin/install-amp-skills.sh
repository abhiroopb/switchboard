#!/usr/bin/env bash
# Install the PM OS skill set.
# Usage: bash system/bin/install-amp-skills.sh [--dry-run] [--skip-remote]
#
# 1. Installs optional catalog skills when PM_OS_SKILL_CATALOG_ADD and CORE_SKILLS are set (empty in the kit).
# 2. Applies the tracked skill overrides (system/skill-overrides/).
# 3. Runs system/scripts/sync-skills.py, which copies every manifest skill into
#    ~/.claude/skills. Claude and Amp both read that folder. Repo skills are no
#    longer copied into ~/.agents/skills (skills system design step 5):
#    Amp reads that folder first, so a second copy there masked the
#    current one. The sync moves old copies there to the trash, keeps todo, and
#    refreshes them only where `codex` is on PATH, because Codex reads that folder.
#    The command-center tick and every landing run the same sync, so this script
#    is for a fresh machine or a catalog refresh, never for publishing an edit.
#
# Firecrawl skills are optional and self-installing: npx -y firecrawl-cli@latest init --all

set -euo pipefail

PM_OS_ROOT="${PM_OS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
dry_run=false
skip_remote=false
failed=()

usage() {
  cat <<'EOF'
Usage: install-amp-skills.sh [--dry-run] [--skip-remote]

  --dry-run      Print what would be installed and exit without changes.
  --skip-remote  Skip the catalog install; only sync repo-owned skills and overrides.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) dry_run=true ;;
    --skip-remote) skip_remote=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Optional organization skill catalog. PM_OS_SKILL_CATALOG_ADD is the command that installs one
# catalog skill by name (for example "<your-cli> skills add"), and CORE_SKILLS lists the names.
# Both are empty in the kit, so only the repo-owned skills install.
SKILL_CATALOG_ADD="${PM_OS_SKILL_CATALOG_ADD:-}"
CORE_SKILLS=()

if $dry_run; then
  echo "Would install ${#CORE_SKILLS[@]} catalog skills: ${CORE_SKILLS[*]:-none}"
  echo "Would apply overrides from $PM_OS_ROOT/system/skill-overrides"
  echo "Would run sync-skills.py: manifest skills into ~/.claude/skills (Claude and Amp)"
  exit 0
fi

mkdir -p "$HOME/.agents/skills"

catalog_dormant=false
if ! $skip_remote && { [[ -z "$SKILL_CATALOG_ADD" ]] || (( ${#CORE_SKILLS[@]} == 0 )); }; then
  echo "  · no organization skill catalog configured (PM_OS_SKILL_CATALOG_ADD and CORE_SKILLS are empty). Repo skills still install."
  skip_remote=true
  catalog_dormant=true
fi
if ! $skip_remote; then
  echo "Installing ${#CORE_SKILLS[@]} catalog skills..."
  for skill in "${CORE_SKILLS[@]}"; do
    printf "  %-40s" "$skill"
    # shellcheck disable=SC2086
    if $SKILL_CATALOG_ADD "$skill" >/dev/null 2>&1; then
      echo "ok"
    else
      echo "FAILED"
      failed+=("$skill")
    fi
  done
  echo ""
fi

# Tracked whole-file overrides (the todo CLI hardening). Runs after the catalog
# install and wins; see system/skill-overrides/README.md.
printf "  %-40s" "skill overrides sync"
if $catalog_dormant && [[ ! -d "$HOME/.agents/skills/todo" ]]; then
  # No catalog: the tracked override is a complete skill, so install it
  # whole (PM OS needs todo for the Todo tab, start-day and close-of-day).
  mkdir -p "$HOME/.agents/skills/todo" &&
    (cd "$PM_OS_ROOT/system/skill-overrides/todo" && tar --exclude=__pycache__ -cf - .) |
    (cd "$HOME/.agents/skills/todo" && tar -xf -) &&
    echo "ok (todo installed from the tracked override; no catalog)" ||
    { echo "FAILED"; failed+=("todo skill install"); }
elif bash "$PM_OS_ROOT/system/scripts/sync-skill-overrides.sh" >/dev/null; then
  echo "ok"
else
  echo "FAILED"
  failed+=("skill overrides sync")
fi

# Every manifest skill into ~/.claude/skills, which Claude Code and Amp both read
# (copied, never linked). Runs whether or not `claude` is
# on PATH, so an Amp-only machine gets the repo skills too. sync-skills.py always
# uses the main checkout, applies the overrides again (idempotent), and keeps a log.
if [[ -f "$PM_OS_ROOT/system/scripts/sync-skills.py" ]]; then
  printf "  %-40s" "skills sync (~/.claude/skills)"
  if (cd "$PM_OS_ROOT" && "${PM_OS_PYTHON:-python3}" system/scripts/sync-skills.py --reason install) >/dev/null; then
    echo "ok"
  else
    echo "FAILED (see: python3 system/scripts/sync-skills.py)"
    failed+=("skills sync")
  fi
fi

echo ""
if (( ${#failed[@]} > 0 )); then
  echo "Failed: ${failed[*]}" >&2
  exit 1
fi
echo "Skills installed. New Amp threads pick them up; run 'amp skill list' to see them."
