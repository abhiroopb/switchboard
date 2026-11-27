---
title: Obsidian onboarding for switchboard
status: active
---

# Obsidian onboarding for switchboard

Obsidian is the reader and wiki for switchboard. The vault is the repository root (`{{SWITCHBOARD_ROOT}}`) opened in place. Nothing moved; Obsidian reads the same files the agent writes.

## Open the vault

`bash system/scripts/install-all.sh` installs Obsidian (winget on Windows, Homebrew on macOS) and registers the checkout as the vault `switchboard` with `python3 system/scripts/register-obsidian-vault.py` (`--check` reports it). That script leaves every other vault alone; when Obsidian is open it closes and reopens it once, because Obsidian rewrites its vault list while it runs. A `switchboard` folder inside another vault (for example a `Development` vault one level up) still gets its own vault, and switchboard links always name `vault=switchboard`.

What Obsidian is for: reading and commenting on the notes switchboard writes. The knowledge graph (`graphify`, built by `system/scripts/build-knowledge-graph.py`) and memory (`amp-mem` for Amp, Claude Code's built-in memory or `claude-mem`) are separate command-line tools; neither needs Obsidian installed or open.

1. Open Obsidian.
2. Click the vault switcher (bottom-left) and pick **switchboard**. Or from a terminal: `open "obsidian://open?vault=switchboard&file=HOME"`.
3. Agent-generated links use `obsidian://open?vault=switchboard&file=…`. Keep the vault name `switchboard`; renaming it breaks every pointer.
   - Do not use `obsidian://open?path=…`. Obsidian only resolves `path=` for a vault it already knows and shows "Vault not found" otherwise.
   - If the vault ever disappears from the switcher (new Mac, reinstalled app): vault switcher → **Open another vault** → **Open folder as vault** → choose `{{SWITCHBOARD_ROOT}}`. Obsidian names it `switchboard` after the folder.
4. Set [HOME.md](../HOME.md) as your landing page: Settings → Core plugins → Bookmarks, then bookmark `HOME.md` and pin it.

The vault ships with `.obsidian/app.json`, `appearance.json`, `core-plugins.json`, `daily-notes.json`, `templates.json`, and `bookmarks.json`. Window layout (`workspace.json`) is per machine and ignored by git.

On Windows, `system/windows/register-obsidian-notes.ps1` (run by `install-all.sh`) makes a `.md` opened from Explorer or a `file://` link open in the vault that holds it. Obsidian's installer maps `.md` to `Obsidian.exe "%1"`, which opens nothing for a note already inside a vault (Obsidian 1.15.0). Use Ctrl where this page says ⌘. The wmux terminal only makes `http(s)` links clickable, so under wmux the "Open in Obsidian" link is `http://127.0.0.1:7472/n/<id>`, answered by `system/scripts/mux/note-links.py`: click it (Ctrl+click when the pane's program has the mouse) and the note opens in the Obsidian app, which comes to the front unless you press a key or click elsewhere first, while the wmux browser panel shows a small "Opened in Obsidian" page. An `obsidian://` link there is plain text. The same note also opens from Amp (through `AMP_EDITOR`), from Explorer, or with `python3 -c "import os; os.startfile(r'<path to note>')"`.

## Navigate and search

| Action | Shortcut |
| --- | --- |
| Quick Switcher (jump to any note by name) | ⌘O |
| Full-text search | ⌘⇧F |
| Command palette | ⌘P |
| Go back / forward | ⌘⌥← / ⌘⌥→ |
| Toggle reading / editing view | ⌘E |
| Open today's daily note (`notes/daily/YYYY-MM-DD.md`) | ⌘D after enabling the Daily notes ribbon icon, or Command palette → "Open today's daily note" |

Tips:

- The left sidebar has File explorer, Search, and Bookmarks tabs. The right sidebar has Backlinks, Outgoing links, Outline, and Properties.
- Hover a link with ⌘ held to preview the target without leaving the current note.
- Graph view (Command palette → "Open graph view") shows how notes link; useful for spotting isolated workstreams.
- Folders hidden from the explorer (`system/state/`, `.amp/`, logs, caches) are set in Settings → Files and links → Excluded files. They still exist on disk.

## Review new work

Every deliverable the agent prepares for your review is a Markdown note, not a Google Doc, unless you ask for a Doc.

- Location: `outputs/reviews/<YYYY-MM-DD>-<slug>.md`, or inside the owning `workstreams/<slug>/` folder when the note is workstream-scoped.
- Frontmatter: `title`, `date`, `status: to review`, `workstream`, `tags`, plus `task_id` and `sources` on routed reviews. The Properties panel renders these. New notes start from `templates/review-note-template.md` and `templates/daily-note-template.md` (core Templates plugin, ⌘P → "Insert template").
- Bookmarks tab (left sidebar): "switchboard Home", "Reviews for {{OWNER_FIRST}}", "Workstreams index", "Switchboard Maintenance", "Obsidian onboarding", the `workstreams/` folder, two saved searches — "Open reviews" (`path:outputs/reviews [status:"to review"]`) and "Daily notes" (sort the result pane by modified time) — and an "Active workstreams" group with one entry per active workstream `CONTEXT.md` (generated from `config.yaml` status; re-run the bookmark step when workstreams open or close).
- To comment on a note: add a `> [!review]` callout or a `%% hidden comment %%` next to the line. The agent applies it with a targeted edit on its next pass and appends "→ done" inside the callout; it keeps the callout until you ask to resolve or remove it. Callout text edits the note only — ask in chat for anything external.
- The review pointer the agent hands back (in CoS, the command center, and `report-to-cos.sh` summaries) is an `obsidian://open?vault=switchboard&file=<path-without-.md>` link. Clicking it opens the note here.
- To clear a review: edit the frontmatter `status` to `reviewed` (or `approved` / `changes requested`) and add your comments inline. The agent reads the note on its next pass.
- Existing collaborative Google Docs are unchanged. Ask explicitly when you want a Google Doc or a hosted site instead of a note.

## What Obsidian renders natively

- Markdown (`.md`) with frontmatter, tables, callouts, and embedded images.
- Images (`.png`, `.jpg`, `.gif`, `.svg`), PDFs, audio, and video open in-app.
- Everything else (`.yaml`, `.json`, `.py`, `.sh`, `.csv`, `.html`, `.docx`, `.pptx`) shows in the explorer because `showUnsupportedFiles` is on, and opens in the default macOS app on click. The vault audit lists how many of each exist.

## Guardrails

- Obsidian Sync and Publish are off in `core-plugins.json`. Do not turn them on; `people/` and `notes/sensitive/` are local-only.
- No community plugins. Core plugins only.
- Do not use "Move file" or "Rename" inside Obsidian on files the agent references by path (`workstreams/*/CONTEXT.md`, `system/**`, `routines/**`); the agent's scripts address them by path. Renaming Markdown notes inside `outputs/reviews/` or `notes/` is fine.
- `alwaysUpdateLinks` is off so Obsidian never rewrites links across the repo on a rename.

## Verify the setup from a terminal

```bash
obsidian vaults verbose                          # switchboard should be listed
obsidian vault=switchboard unresolved verbose format=json   # broken links Obsidian sees
obsidian vault=switchboard orphans                     # notes nothing links to
obsidian vault=switchboard open path=HOME.md           # opens the home note
```

The `obsidian` CLI lives at `/usr/local/bin/obsidian` and requires the app to be running.
