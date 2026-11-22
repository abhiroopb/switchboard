---
description: Compact and distill pending amp-mem observations into knowledge notes and rule proposals
argument-hint: "[--since YYYY-MM-DD]"
allowed-tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
---

Distill pending memory observations (the Amp palette command "PM OS: Distill memory").

1. Check what is pending with Bash:

   ```bash
   ~/bin/amp-mem distill-status
   ```

   If `~/bin/amp-mem` is missing, report "amp-mem CLI not installed" and stop. If the status shows 0 pending observations, report "Nothing to distill" and stop.

2. Otherwise use the `kb-distill` skill and run its full workflow: `~/bin/amp-mem distill-dump` (pass `$ARGUMENTS` through when it is a `--since DATE` option), cluster the observations, write the distilled notes and rule proposals, then record the run with `~/bin/amp-mem distill-record`.

3. Finish with one line: observation count distilled, notes produced, rules proposed. On failure, report the first error line from the script output.
