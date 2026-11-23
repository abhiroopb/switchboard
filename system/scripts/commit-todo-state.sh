#!/usr/bin/env bash
#
# commit-todo-state.sh — snapshot the canonical to-do state into git and push
# it to origin so the tracked copy on GitHub stays current.
#
# Covers only system/state/todo.json. Recovery backups remain local and are
# excluded from automatic snapshots. Existing Git history is unchanged.
# Commits only that path (pathspec-limited), so anything else staged or dirty in the
# repo is untouched. Push is best-effort: offline or non-main branches leave
# the commit local with a warning.
#
# Called from start-day.sh (morning backstop) and end-of-day-close.sh.
#
# Usage:
#   commit-todo-state.sh              # commit if changed, push if on main
#   commit-todo-state.sh --dry-run    # show what would be committed, change nothing
#   commit-todo-state.sh --no-push    # commit but skip the push
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PATHS=("system/state/todo.json")

DRY=0
NO_PUSH=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --no-push) NO_PUSH=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

cd "$REPO_ROOT"

if [[ -z "$(git status --porcelain -- "${PATHS[@]}")" ]]; then
  echo "commit-todo-state: todo state clean, nothing to commit"
  exit 0
fi

if [[ "$DRY" == 1 ]]; then
  echo "commit-todo-state: [dry-run] would commit:"
  git status --short -- "${PATHS[@]}" | sed 's/^/  /'
  git diff --stat HEAD -- "${PATHS[@]}" | sed 's/^/  /'
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$NO_PUSH" == 1 ]]; then
    echo "commit-todo-state: [dry-run] push skipped (--no-push)"
  elif [[ "$branch" == "main" ]]; then
    echo "commit-todo-state: [dry-run] would push to origin main"
  else
    echo "commit-todo-state: [dry-run] would NOT push (branch is '$branch', not main)"
  fi
  exit 0
fi

git add -- "${PATHS[@]}"
if ! git commit --quiet -m "chore: todo state snapshot $(date '+%Y-%m-%d %H:%M')" -- "${PATHS[@]}"; then
  echo "commit-todo-state: WARNING commit failed" >&2
  exit 1
fi
echo "commit-todo-state: committed $(git rev-parse --short HEAD)"

if [[ "$NO_PUSH" == 1 || "${SWITCHBOARD_AUTO_PUSH:-off}" != "on" ]]; then
  exit 0  # switchboard stays local unless SWITCHBOARD_AUTO_PUSH=on
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
  echo "commit-todo-state: WARNING on branch '$branch', not pushing (commit is local)" >&2
  exit 0
fi

try_push() {
  git push --quiet origin main 2>/dev/null
}

if try_push; then
  echo "commit-todo-state: pushed to origin main"
  exit 0
fi

# Recovery must not rebase or autostash unrelated work in the shared checkout.
echo "commit-todo-state: WARNING push failed — commit remains local; reconcile publication manually" >&2
exit 1
