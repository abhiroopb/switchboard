---
title: "{{title}}"
date: "{{date}}"
status: to review
workstream: 
task_id: 
tags: [review]
sources: []
review_surface: obsidian
---

# {{title}}

outputs/reviews/{{date}}-<slug>.md
[Open in Obsidian](obsidian://open?vault=switchboard&file=outputs/reviews/{{date}}-<slug>)

## Summary

- 

## Findings

- 

## Decisions for {{OWNER_FIRST}}

- 

## Sources

- 

> [!review] How to comment on this note
> Add a `> [!review]` callout (or a `%% hidden comment %%`) next to any line you want changed.
> The agent applies each one with a targeted edit on its next pass and appends "→ done" inside the callout. It keeps the callout until you ask to resolve or remove it.
> Callout text only edits this note; ask in chat for anything external (Slack, email, commits, config).
> Change `status` in the frontmatter to `reviewed` when you are done.
