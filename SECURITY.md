# Security

switchboard runs agents on your own computer under your own accounts. These are the protections it ships with and what stays your job.

## What it protects

- **Commands.** The Claude Code hook (`.claude/hooks/switchboard-guardrails.py`) and the Amp guardrails plugin block force pushes, `--no-verify`, blanket `git add -A`, and deletes outside scratch folders.
- **Sends.** Slack messages and emails are drafted and shown in full. A send happens only after you say "send it" for that exact text, in the same conversation.
- **Pushes.** Your install has no git remote, and the automatic push is off unless you set `SWITCHBOARD_AUTO_PUSH=on`. With it on, only `main` is pushed, never with force, and never while a private folder is tracked.
- **Private folders.** `local/`, `people/`, `notes/sensitive/`, `notes/chrome-tabs/`, `.state/` and `system/state/` are gitignored. `dispatch.py finish` refuses a landing that tracks them.
- **Private identifiers.** Add values you never want committed (a street address, account numbers) with `python3 system/scripts/private_identifiers.py add --kind <kind>` on stdin. They are stored as salted hashes, and every landing and push is checked against them.
- **Untrusted content.** Policy tells every agent that web pages, emails and documents are data, never instructions.

## Sharing your own copy

If you want to share your setup, never push your install folder. Build a filtered copy with `system/kit/build_tier.py` (see `system/kit/README.md`). The build fails when anything matches your owner-local deny lists, the personal-data detectors (`system/kit/scan_public.py`), the private identifier list or the secret scanner (gitleaks).

## What stays your job

- **Background jobs.** They run with your credentials. Read `AGENTS.md`, the hooks and `system/background-tasks.yaml` before you turn the loop on.
- **Permissions.** Worker tabs start with the agent's permission prompts skipped, so they can run unattended. Turn that off in `system/scripts/dispatch.py` (`launch_command_for`) if you want to approve each command.
- **OAuth apps.** The connectors use OAuth clients you create. Keep their secrets out of the repository; the installer stores them under `~/.switchboard/`.

## Reporting a problem

Open a GitHub issue that describes the problem without exploit details, or use the repository's private vulnerability reporting if it is turned on.
