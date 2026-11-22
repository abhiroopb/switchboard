---
name: worktree-helper
description: Create, list, switch between and remove git worktrees so several branches, or several agents, can work in one repository at the same time without stashing. Use when starting parallel work on a new branch, when a task needs its own checkout, or when cleaning up checkouts whose branches have merged.
---

# Git worktrees

A worktree is an extra checkout of the same repository. Every worktree has its own branch, working files and index, but all of them share one object store and one set of refs. That makes worktrees the cheapest way to run several tasks side by side.

## When to use

- You need a second branch checked out while the first one has uncommitted work.
- Several agents must work in the same repository without touching each other's files.
- A long build or test run should keep going while you work on something else.
- Old checkouts have piled up and need to be cleaned out.

## Naming and location

- Keep worktrees outside the main checkout, in one sibling folder, for example `../<repo>-worktrees/<name>`. A worktree nested inside the main checkout confuses file watchers, search tools and some build systems.
- Name the folder after the branch or task, using lowercase and hyphens: `fix-login-timeout`, `task-1234`.
- Use one branch per worktree. Git refuses to check out the same branch in two worktrees, and that rule protects you.

## Steps

1. Check the current state first.

   ```bash
   git worktree list
   git status --short
   ```

2. Create a worktree on a new branch, starting from an up-to-date base.

   ```bash
   git fetch origin
   git worktree add -b fix-login-timeout ../myrepo-worktrees/fix-login-timeout origin/main
   ```

   To check out a branch that already exists:

   ```bash
   git worktree add ../myrepo-worktrees/feature-x feature-x
   ```

3. Switch by changing directory. There is no `checkout` step.

   ```bash
   cd ../myrepo-worktrees/fix-login-timeout
   git branch --show-current
   ```

4. Set up the new checkout. Ignored files such as `node_modules`, virtual environments, `.env` files and build output are not copied. Install dependencies again, or copy local config only if the user agrees.

5. Find worktrees whose branches have merged.

   ```bash
   git fetch origin --prune
   git branch --merged origin/main
   git worktree list
   ```

6. Remove a finished worktree after confirming it has no uncommitted or unpushed work.

   ```bash
   git -C ../myrepo-worktrees/fix-login-timeout status --short
   git -C ../myrepo-worktrees/fix-login-timeout log --oneline @{upstream}..HEAD
   git worktree remove ../myrepo-worktrees/fix-login-timeout
   ```

7. Clear stale records left behind when a worktree folder was deleted by hand.

   ```bash
   git worktree prune --dry-run
   git worktree prune
   ```

## Safety rules

- Never run `git worktree remove --force` on a worktree with uncommitted changes unless the user says so in this conversation. Show them `git status` for that worktree first.
- Removing a worktree does not delete its branch. Deleting the branch is a separate step and needs the user's explicit OK.
- Do not delete worktree folders with `rm` or Explorer. Use `git worktree remove` so git's records stay consistent.
- The stash is shared across all worktrees. Prefer a temporary commit on the worktree's own branch over `git stash`.
- Use `git worktree lock <path>` for a worktree on removable or network storage so `prune` does not drop it.

## Done when

- `git worktree list` shows each active task in its own folder on its own branch.
- New worktrees have their dependencies installed and a clean `git status`.
- Merged worktrees were removed only after their work was confirmed safe.
- `git worktree prune --dry-run` reports nothing left to prune.
