---
name: draft-pr
description: Push the current branch and open a draft GitHub pull request with a generated title and a description covering what changed, why, and how it was tested. Use when the work on a feature branch is committed and the user asks to push it, open a pull request, or put it up for review.
---

# Push and open a draft pull request

This skill takes committed work on a feature branch to a draft pull request in one pass. Draft is the default so nobody is asked to review before the author is ready.

## When to use

- The user says "push this", "open a PR", or "put this up for review".
- A task in its own worktree is finished and needs a pull request.

## Steps

1. Check the branch. Stop if it is the default branch.

   ```bash
   git branch --show-current
   gh repo view --json defaultBranchRef -q .defaultBranchRef.name
   git status --short
   ```

   If the current branch is the default branch, do not push. Offer to create a feature branch from the current commits instead, for example `git switch -c <short-topic-name>`.

2. Make sure the work is committed. If `git status` shows changes, ask whether to commit them, or use the `commit-and-pr` skill to commit them in logical pieces.

3. Check whether a pull request already exists for this branch.

   ```bash
   gh pr view --json url,state,isDraft 2>/dev/null
   ```

   If one exists, push and report its URL rather than opening a second one.

4. Look at what the pull request will contain.

   ```bash
   git fetch origin
   git log --oneline origin/<default-branch>..HEAD
   git diff --stat origin/<default-branch>...HEAD
   ```

5. Run the project's tests or checks if they have not run since the last change. Record what you ran and the result.

6. Push and set the upstream.

   ```bash
   git push -u origin HEAD
   ```

   If the push is rejected because the remote branch has diverged, stop and tell the user. Do not force-push without their OK.

7. Write the title and description.
   - Title: under 70 characters, imperative mood, specific. "Fix timeout when login token expires", not "Updates".
   - Description, in this order:

   ```markdown
   ## What
   One to three sentences on the change.

   ## Why
   The problem or goal. Link the issue if there is one.

   ## How tested
   - Commands run and their results.
   - Manual checks, if any.

   ## Notes for reviewers
   Anything risky, deferred, or worth a close look. Omit if empty.
   ```

   Base every claim on the diff and the commits. Do not describe tests you did not run.

8. Open the draft pull request.

   ```bash
   gh pr create --draft --base <default-branch> --title "<title>" --body-file <path-to-body.md>
   ```

   Write the body to a temporary file to avoid shell quoting problems.

9. Report the pull request URL to the user.

## Safety rules

- Never push to or open a pull request from the default branch.
- Never force-push. If a rewritten branch must be pushed, ask first and use `--force-with-lease`.
- Do not add reviewers, labels or assignees, or mark the pull request ready, unless the user asks.
- Do not include secrets, local paths or internal notes in the description.

## Done when

- The branch is pushed with an upstream set.
- One draft pull request exists for the branch, targeting the default branch.
- The description has what, why and how tested, all backed by the actual diff.
- The user has the pull request URL.
