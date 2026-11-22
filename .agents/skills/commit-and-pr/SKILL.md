---
name: commit-and-pr
description: Commit work in logical commits with clear messages, then open a new pull request or update an existing one, and optionally manage a stack of dependent pull requests with plain git and gh or with Graphite. Use when the user asks to commit, to open or update a PR, or to split work into stacked PRs.
---

# Managing commits and pull requests

Good commits make review easy and history useful. This skill groups changes into commits that each do one thing, then keeps the pull request in step with the branch.

## When to use

- There are uncommitted changes and the user asks to commit them.
- A pull request needs to be opened, or an existing one updated after new commits.
- A large change should be split into a stack of smaller pull requests.

## Commit in logical pieces

1. Look at everything that changed.

   ```bash
   git status --short
   git diff
   git diff --staged
   ```

2. Group the changes. One commit per idea: a refactor, a feature, its tests, a config change. Tests usually go with the code they cover. Formatting-only changes go in their own commit.

3. Stage each group on purpose. Avoid `git add -A` unless every change belongs together.

   ```bash
   git add path/to/file another/file
   git add -p path/to/mixed-file
   ```

4. Write the message. First line under about 70 characters, imperative mood, saying what the commit does. A blank line, then a body that explains why, when the why is not obvious. Follow the repository's own convention if it has one, such as Conventional Commits.

   ```bash
   git commit -m "Retry token refresh once before failing" -m "The provider drops the first request after a cold start."
   ```

5. Before committing, scan the staged diff for secrets, debug output and stray files.

## Open or update the pull request

```bash
gh pr view --json number,url,state,isDraft 2>/dev/null
```

- No pull request yet: push with `git push -u origin HEAD`, then open one as a draft with `gh pr create --draft`. The `draft-pr` skill covers the description format.
- Pull request exists: push the new commits with `git push`. If the description no longer matches the change, update it with `gh pr edit <number> --body-file <file>`.

## Stacked pull requests

A stack is a chain of branches, each built on the one before it, so each pull request stays small.

With plain git and gh:

```bash
git switch -c part-1 origin/main     # commit part 1
git push -u origin HEAD
gh pr create --draft --base main

git switch -c part-2                 # built on part-1, commit part 2
git push -u origin HEAD
gh pr create --draft --base part-1
```

When a lower branch changes, rebase the branches above it onto it in order. When part 1 merges, rebase part 2 onto `main` with `git rebase --onto origin/main part-1 part-2`, then retarget its pull request with `gh pr edit --base main`. Pushing rebased branches needs the user's OK and `--force-with-lease`.

With Graphite, only if the user already uses it:

```bash
gt create -m "Part 1 message"
gt create -m "Part 2 message"
gt submit --stack --draft
gt sync
gt restack
```

Graphite rewrites and pushes branches for you. Tell the user before running `gt submit` or `gt sync` on branches already pushed.

## Safety rules

- Never commit to or push the default branch directly.
- Never force-push without the user's OK in this conversation, and then only with `--force-with-lease`.
- Never delete branches, including merged stack branches, without the user's OK.
- Do not amend or squash commits that are already pushed unless the user asks.

## Done when

- Each commit does one thing and has a message that says what and, where needed, why.
- No secrets or unrelated files were committed.
- The pull request exists, is pushed up to date, and its description matches the change.
- For a stack, each pull request targets the branch below it.
