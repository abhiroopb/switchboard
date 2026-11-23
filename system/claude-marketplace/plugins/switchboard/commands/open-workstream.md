---
description: Pick a switchboard workstream and launch or focus its cmux workspace
argument-hint: "[workstream slug]"
allowed-tools: ["Bash", "Read", "Glob"]
---

Open a workstream workspace (the Amp palette command "switchboard: Open workstream…").

1. List the available workstreams with Bash: every directory under `"{{SWITCHBOARD_ROOT}}"/workstreams/` that contains a `config.yaml`, excluding names starting with `_` or `.`, sorted:

   ```bash
   cd "{{SWITCHBOARD_ROOT}}"/workstreams && for d in */; do [ -f "$d/config.yaml" ] && case "$d" in _*|.*) ;; *) printf '%s\n' "${d%/}";; esac; done | sort
   ```

   If the list is empty, report "No workstreams found" and stop.

2. Pick the slug. If `$ARGUMENTS` names one of the listed slugs, use it. If `$ARGUMENTS` is empty or does not match exactly, show the list as a numbered selection and wait for a choice. Never guess a slug.

3. Launch the workspace and select it. Run this exact command chain with Bash (substitute the chosen slug for `<slug>`); it may take a few seconds:

   ```bash
   cd "{{SWITCHBOARD_ROOT}}" && bash system/cmux-helpers.sh launch workstream:<slug> && source system/cmux-helpers.sh && ws_ref=$(workspace_ref_if_exists <slug>) && SWITCHBOARD_EXPLICIT_USER_FOCUS=1 cmux_cmd select-workspace --workspace "$ws_ref"
   ```

4. Report "Launching workstream: <slug>" on success, or the first error line on failure.
