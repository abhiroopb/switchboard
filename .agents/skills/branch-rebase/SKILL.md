---
name: branch-rebase
description: Bring a branch up to date with its target, such as main or an upstream remote, by rebasing, resolving conflicts one file at a time and running tests afterward. Use when a branch has fallen behind, when a pull request shows merge conflicts, or when the user asks to rebase or sync with main.
---

# Rebasing git branches

A rebase replays your commits on top of a newer base. The result is a straight history, but every replayed commit gets a new hash, so a branch that was already pushed will need a forced push afterward. That push needs the user's OK.

## When to use

- The branch is behind `main` and the pull request shows conflicts.
- The user asks to rebase, update, or sync a branch.
- A fork needs to catch up with the original repository.

## Before you start

1. Confirm the working tree is clean. Commit or set aside any changes first.

   ```bash
   git status --short
   git branch --show-current
   ```

2. Find the target. It is usually `origin/main`. For a fork it is often `upstream/main`. For a stacked branch it is the parent branch.

   ```bash
   git remote -v
   git fetch --all --prune
   git log --oneline --graph -n 20 HEAD origin/main
   ```

3. Record where the branch was, so you can return to it.

   ```bash
   git rev-parse HEAD
   ```

## Steps

1. Start the rebase.

   ```bash
   git rebase origin/main
   ```

   If only some commits belong to this branch, for example after a parent branch was squash-merged, replay just those:

   ```bash
   git rebase --onto origin/main <old-parent-commit> <branch>
   ```

2. When git stops on a conflict, list the files.

   ```bash
   git status --short
   git diff --name-only --diff-filter=U
   ```

3. Resolve one file at a time.
   - Read both sides and the commit being replayed (`git log -1 REBASE_HEAD`).
   - Keep the intent of both changes. Do not pick a whole side just to make the markers go away.
   - Remove every conflict marker, then stage the file with `git add <file>`.
   - Lock files and generated files are usually better regenerated than hand-merged.

4. Continue, and repeat steps 2 and 3 until the rebase finishes.

   ```bash
   git rebase --continue
   ```

   If a conflict is unclear or the result looks wrong, stop and ask. `git rebase --abort` returns the branch to where it started.

5. Build and run the tests. A clean rebase can still break code when both sides changed related logic.

6. Compare against the original to confirm nothing was lost.

   ```bash
   git range-diff origin/main <old-head> HEAD
   ```

## Pushing a rewritten branch

If the branch was already on the remote, a normal push will be rejected. Ask the user before you push. Explain that the remote branch will be overwritten. With their OK, use a lease so the push fails if someone else pushed in the meantime:

```bash
git push --force-with-lease
```

Never use plain `--force`.

## Safety rules

- Never rebase a shared branch such as `main`, or a branch other people build on, without the user's say-so.
- Never push a rewritten branch without the user's OK in this conversation.
- Never delete branches or files as part of a rebase.
- If you get lost, `git reflog` shows every previous position of `HEAD`.

## Done when

- `git status` shows a clean tree and no rebase in progress.
- `git log origin/main..HEAD` shows only this branch's commits.
- Build and tests pass, or failures are reported with the cause.
- Any push of the rewritten branch used `--force-with-lease` after the user agreed.
