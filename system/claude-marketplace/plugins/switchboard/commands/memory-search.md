---
description: Search amp-mem observations with an FTS5 query
argument-hint: "<query> [--type T] [--project P] [--after DATE] [--limit N]"
allowed-tools: ["Bash"]
---

Search persistent memory (the Amp palette command "switchboard: Search memory").

If `$ARGUMENTS` is empty, ask one question: "Memory search: what FTS5 query? (e.g. `mobile onboarding`)" and wait.

Otherwise run this exact command with Bash, passing the query and any trailing options through unchanged:

```bash
~/bin/amp-mem search $ARGUMENTS
```

Show the first 800 characters of stdout verbatim in a code block, then one line on how many results matched. If stdout is empty, say `(no results)`. For detail on one hit, offer `~/bin/amp-mem detail <id>`.

Fallback: if `~/bin/amp-mem` is missing, use the claude-mem `mem-search` tool with the same query and say which backend answered.
