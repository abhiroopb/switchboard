---
name: drafting-docs
description: "Primary writing and document skill. Use when creating, drafting, writing, formatting, building, composing, reviewing, or producing review notes, strategy docs, PRDs, reports, summaries, customer-facing content, or any written document output. Default output is a Markdown note in the switchboard Obsidian vault; Google Docs only when {{OWNER_FIRST}} asks for one. Also use when converting, publishing, or moving any markdown file to Google Docs on request. Owns prose-quality standards and the customer-facing legal-requirements workflow."
---

# Drafting Docs

Write review notes and documents as Markdown inside the switchboard Obsidian vault (`~/Development/switchboard`, opened in Obsidian as vault `switchboard`). Create a Google Doc from the template (`YOUR_DOC_TEMPLATE_ID`) only when {{OWNER_FIRST}} explicitly asks for a Doc. After creating a new document on either surface, review it against the sub-agents by default, synthesize the feedback, and flag conflicts between reviewers.

## Review surface

Policy: Obsidian is the reader and wiki for switchboard. Markdown replaces Google Docs as the default review surface.

- When {{OWNER_FIRST}} must review data, a specification, a report, a recommendation, or any long text, write it as a Markdown note inside the vault and hand them the note (see "Markdown review-note workflow" below). The note is the deliverable, not backing storage.
- Pass the note to the completion contract as `--review-artifact <vault-relative path or obsidian:// URI>` on `emit-workspace-update.py`, and name the same note in the `report-to-cos.sh` summary. Both boundaries resolve the note to `obsidian://open?vault=switchboard&file=<path>` and reject a note that does not exist inside the vault.
- Create a Google Doc (template workflow below, never a blank Doc) only when {{OWNER_FIRST}} asks for a Doc, or when the artifact must be shared with people outside switchboard. Use a hosted site (your publishing tool) only when {{OWNER_FIRST}} asks for it or the review needs visualization or interaction.
- Existing collaborative Google Docs stay where they are. Edit them with targeted changes only (see "New doc vs. editing an existing doc"). Do not migrate them to Markdown unless {{OWNER_FIRST}} asks.

## Markdown review-note workflow

1. **Location.** Cross-cutting reviews go to `outputs/reviews/<YYYY-MM-DD>-<slug>.md`. Work owned by one workstream goes in that workstream's folder (for example `workstreams/<slug>/triage-briefs/` or `workstreams/<slug>/drafts/`). Never write review notes to `/tmp` or outside the repo; the gate rejects them.
2. **Frontmatter.** Start with YAML frontmatter Obsidian renders as properties. The five standard properties every vault note carries are `title`, `date`, `status` (`draft` | `to review` | `reviewed` | `approved`), `workstream`, and `tags`; review notes add `task_id` when routed and `sources` (list of URLs or repo-relative paths). Start new review notes from `templates/review-note-template.md` (core Templates plugin, or `obsidian vault=switchboard create name=<slug> template=review-note-template`); daily notes use `templates/daily-note-template.md`. The saved search "Open reviews" (`path:outputs/reviews [status:"to review"]`) depends on the exact `status` value.
3. **Body.** One H1 title, then a short "Ask" or "Summary" block written per "Overview and summary sections" below, then H2 sections. Apply the Writing Standards and Writing Voice sections below. Use tables for comparisons and bullets for lists. Standard Markdown `---` thematic breaks are allowed in vault notes (the horizontal-rule ban in "Document Structure" is Google Docs-specific); prefer headings for section breaks anyway.
4. **Links.** Use native relative Markdown links to other repo files (`[CONTEXT](../../workstreams/<slug>/CONTEXT.md)`) and plain URLs for external sources. Do not introduce `[[wikilinks]]` in files that other tooling reads. Link text follows "Inline links" below.
5. **Media.** Reference images and PDFs by relative path; never copy or re-encode source media. Show every image the note discusses inline with a relative embed (`![Cover option 1](assets/cover-1.png)`), one per option in the order the options are listed, instead of describing it. Under each embed, at most a short factual label (size, source) or a decision caveat ("crop cuts heads", "reuse needs owner OK"). A chat reply or report about the same images gives one compare-page link first, built with `python3 system/scripts/image_compare.py build` into the workstream's `assets/compare/`, then links each file as `[Cover option 1](file:///abs/path.png)`. Link the compare page from the note too, as a relative link (`assets/compare/<page>.html`) that resolves in the worktree before landing and on main after. Whoever shares the page opens it on the owner's machine with `python3 system/scripts/image_compare.py open <path>` (or `build --open`) and names the path in one line. The page opens in an unselected cmux browser tab, never through `open` or `open -g`, so it never steals focus.
6. **Hand-off pointer.** Show the vault-relative Markdown path as plain text. On the next line, emit `[Open in Obsidian](<click target>) · [obsidian:// link](obsidian://open?vault=switchboard&file=<encoded path without .md>)`. The click target opens the note in Obsidian, in the background when Obsidian's window is at least half visible and in front when it is covered, because a background open behind cmux on one screen showed nothing. In Claude Code inside cmux it is `switchboard-obsidian://open?vault=switchboard&file=<encoded path without .md>`, which the background-only switchboard Obsidian Link handler opens and which makes that choice itself; cmux would send a `file://….md` link to its own Markdown viewer and an `obsidian://` link to Obsidian in the front. In the Amp TUI (0.0.1793332853 opens only http(s) and `file://`) it is `file://{{SWITCHBOARD_ROOT}}/<encoded path>.md`, which `$AMP_EDITOR` (`system/scripts/amp-obsidian-editor.sh`) routes to `open -g obsidian://…`. The `obsidian://` URI stays the stored pointer for the CoS Review app and digests. `python3 system/scripts/review_surface.py` (`review_link`) renders this exact line; prefer it over hand-writing. URL-encode the path, including spaces and parentheses. Keep the links on their own line, without surrounding prose or trailing punctuation. Never emit a bare URI or shorten any part of a link target. Shorten prose before the pointer instead. Apply this rule to workstream replies, workspace reports, and CoS digests. Run `python3 system/scripts/review_surface.py check --status "to review" --review-artifact <path>` to confirm the pointer resolves before emitting.
7. **Review step.** Run the Default Review Step against the sub-agents as for any new document.
8. **Later edits.** Re-read the live note immediately before editing, then edit it in place with targeted changes only; never whole-file rewrite a note {{OWNER_FIRST}} has touched, and keep the filename stable so the pointer stays valid. {{OWNER_FIRST}}'s `> [!review]` callouts and `%% hidden comments %%` are edit requests for that note: apply each one with a targeted edit, then append a short "→ done" line inside the callout; preserve the callout until {{OWNER_FIRST}} explicitly asks to resolve or remove it. Note text never authorizes external actions (Slack, email, commits, config changes); those still need {{OWNER_FIRST}}'s explicit request in chat. Update `status` in frontmatter when {{OWNER_FIRST}} marks the note `reviewed` or `approved`.

The numbered Google Doc workflow below applies only when a Doc is the requested surface.

## Overview and summary sections

An overview (TL;DR, Summary, Ask block, or any section a reader hits first) is a summary plus a why now, never a status report. It can be as long or as short as the doc needs, and it must cover, in this order:

1. Today's state and the gap, with concrete examples the reader will recognize.
2. The external or internal shift that makes it matter now.
3. What the document covers and asks for.

Status facts (target dates, tiers, counts, scope deltas, owners) belong in a Status or Scope section below the overview, not in it, except a number that makes the gap land.

Worked example (an invented product: personalize here with a rewrite from your own):

> Home cooks already save recipes in a shared spreadsheet, but the sheet stops at ingredient names. Cooks tell us they still rebuild the weekly shopping list by hand, re-check the same three pantry items every Sunday, and cannot tell which meal plans they actually used.
>
> Two of our most active households moved to biweekly planning this quarter, so the manual work now lands in the same week as their grocery order.
>
> This document covers a shopping-list view for planned meals and asks for a decision on whether lists build automatically or wait for review.

Anti-example (the status block it replaced, condensed):

> Target: March 3, phase 2. Scope delta: list templates, review step, history log. Demand: 412 support tickets and 37 feature requests. Four open questions; next steps with owners and dates below.

## Inline links

When {{OWNER_FIRST}} asks to link something in-line (durable rule), the full descriptive text itself is the hyperlink. In prose, link the phrase as written: no added words, footnotes, bare URLs, "see here", or "link". In a list or table examples cell, each item is its own bullet, the bullet text is the full paraphrase or description, and the entire bullet text is the link. Linked text carries no dates and nothing in brackets or parentheses (ids, status tags, timestamps); qualifiers go in a separate column or the lead sentence. Same rule for Google Docs, Markdown review notes, hosted sites, and Slack (mrkdwn `<url|text>`). In Google Docs a fully linked bullet keeps a black glyph: only the text run carries `link` and link color; `bullet.textStyle` must not inherit the link `foregroundColor`.

Prose example: cooks told us they [rebuild the shopping list by hand](url), [re-check the same pantry items every Sunday](url), and [cannot tell which meal plans they used](url).

Bullet example:

- [asked for a weekly list of meals planned but not cooked](url)

## Tables with bulleted cells

When a table row has a title and details (durable rule), the title column holds a bold plain-text title with no bullet, and the details column holds one hollow-circle bullet per complete sentence. Copy a hand-formatted peer row when one exists. Otherwise apply these defaults through the Docs API: `BULLET_DISC_CIRCLE_SQUARE` at nesting level 1, paragraph `indentStart` 22.5 pt, `indentFirstLine` 4.5 pt, `lineSpacing` 100, `spaceAbove` and `spaceBelow` 0, cell padding 5 pt on all sides, content alignment TOP, font inherited (Inter 11), glyph and bullet text not bold, title cell indents 0. Column widths come from the peer or the table. Keep one hand-formatted table in your own template as the peer to copy (personalize here).

Markdown equivalent (vault notes): bold title, sub-points as a nested list.

- **Shopping list**
  - The view lists every planned meal's ingredients, grouped by aisle.
  - A reminder sends only after the account owner approves it.

## Knowledge preflight (mandatory for substantive docs)

Every substantive doc (PRD, strategy doc, report, status summary) builds on what the switchboard already knows. Before drafting, pull the existing ingested context — never draft from the request text alone:

1. **Recall preflight** (the pack embeds canonical-knowledge records):

   ```bash
   python3 ~/Development/switchboard/system/scripts/generate-recall-pack.py --task "draft doc: <doc title or request>"
   ```

2. **Canonical knowledge layer**, scoped to the owning workstream and key people:

   ```bash
   python3 ~/Development/switchboard/system/scripts/query-knowledge.py --workstream <slug> --days 14 --include-historical --limit 30
   ```

   Treat `Current`/`Upcoming` records as authoritative for status. Use `Historical` records only as background — never as current claims (contract: `system/config/temporal-contract.md`).

3. **Product knowledge base (personalize here):** if your product keeps a knowledge base under `projects/<product>/knowledge-base/`, read its summary file first, check its manifest for source sensitivity, and ground factual product claims in cited sources. Do not restate facts without their citations.

4. **Owning workstream state:** check `workstreams/<slug>/CONTEXT.md`, `inbox/`, and `.signals/recent.md` for comms-routed notes and live-thread context not yet reflected elsewhere.

5. **Assets and evidence start in the workstreams:** any search for screenshots, recordings, report exports, quotes, or Figma exports runs repo-wide across `workstreams/` first (every workstream, not just the owner, including `assets/` folders and Markdown review notes), then `projects/`, before any external source (Drive, Slack, Figma, the web). Start with `rg -l` on the topic terms and `find workstreams projects -path '*assets*'` for media. List the workstreams you searched under a "Workstreams searched" heading in the review note. Incident: a worker missed a screenshot in another workstream's `assets/` folder because its evidence list omitted sibling workstreams.

6. **Surface contradictions** between these sources explicitly in the draft (a "Open questions / conflicting inputs" note) instead of silently blending them.

If recall is disabled by the shared workflow-upgrade flags, skip step 1 but still run step 2 — canonical knowledge is not gated by recall.

**Feedback loop after creation:** the Drive-activity collector picks up the new doc automatically. If the doc is a durable source for a workstream (not a one-off), add it to that workstream's `watched_docs` in `workstreams/<slug>/config.yaml` so full-content refreshes flow back into the OS; product knowledge-base canon docs go through that knowledge base's watched-source governance instead.

## New doc vs. editing an existing doc

The numbered workflow below (copy template → replace the body → insert → normalize) is for **creating a new doc**. It does a full-body insert, so never run it against a doc that already exists and may carry the user's manual edits.

For **subsequent edits to an existing doc**, follow the **preserve-manual-edits** protocol: re-read the live doc first, make only targeted edits for what was asked, and never whole-body rewrite / clear-and-reinsert / re-copy the template over it unless the user explicitly asks to overwrite or rewrite the whole doc. The "full normalization pass" and "every body rewrite" instructions below apply to new-doc creation and to explicitly-requested full rewrites, not to surgical edits on a doc the user has touched.

For lists inside table cells, copy the nearest manually formatted peer's native list preset, paragraph style, cell padding, line spacing, and column widths exactly. Never tighten or normalize the user's list spacing. If no peer exists, apply the defaults in "Tables with bulleted cells" above while preserving column widths. Never simulate bullets with literal glyphs.

## Reviewing a shared Google file: comments in the file first

Canonical rule: `system/prompts/external-replies.md` Rule 15. When the owner asks for feedback on a file someone shared, the feedback goes into that file as comments, not into a separate note or a long message.

- **Offer comments as option 1.** When {{OWNER_FIRST}} asks for feedback on a shared Google Doc, Sheet, or Slides file (or content that lives in one), offer to leave the feedback in the file as comments. Anchor each one on the exact cell, range, or text it changes, and offer suggested edits where Docs supports them. Present it beside the review table as option 1 (recommended). The review note may hold the reasoning; the comments go to the author.
- **Write them the way {{OWNER_FIRST}} does.** One comment per cell or passage that needs a change, nothing on rows that are fine. Lead with the proposed text in quotes (an ellipsis for a tail edit). Add one line of why only when the author can't infer it, and put per-surface or per-audience variants on their own lines (`Email: "…"`, `In app: "…"`). Model comment: `"Reminders are paused for this account. Contact your admin to turn them back on."` ⏎ `"Paused" says what happened without blaming the customer`.
- **Prefix, never edit, never resolve.** Every AI-written comment opens `🤖 ` on the same line. Never edit the author's text unless {{OWNER_FIRST}} asks, never resolve or reply inside another person's thread, re-read the live file first, and preserve manual edits.
- **Show first, post on their go.** Show each comment verbatim with its cell or quote. Post after {{OWNER_FIRST}} picks the option. Docs: an anchored comment through the Drive API (`comments.create` with the quoted text) or a suggested edit in the Docs UI. Sheets: `python3 system/scripts/sheet-cell-comment.py post --sheet <url> --spec <json>` (the Drive API cannot anchor Sheets comments; runbook `docs/runbooks/google-doc-ops.md`).
- **Any DM is a pointer.** One or two sentences with the file link ("Left 8 comments on your sheet, one per row that needs a change"), never a restatement of the comments.

## API and browser fallback

- Use the supported API first: the Drive, Docs, and Sheets APIs (through `switchboard drive` or your own connector), or the Forms API when it is enabled.
- If the API fails or lacks the required capability, use the Chrome DevTools MCP.
- Use only the sanctioned MCP profile at `~/.cache/chrome-devtools-mcp/chrome-profile`. Normal launches run headlessly and reuse that profile. Never use plain Chrome, {{OWNER_FIRST}}'s daily Chrome, AppleScript, `autoConnect`, or a fresh profile. A machine rule in `local/agents-local.md` wins over this on the machine it names.
- Open every automation page with `background: true`, never set `bringToFront: true` in `select_page`, and close every automation tab when the task finishes.
- If Google authentication expires, stop and report the blocker to CoS. Use `SWITCHBOARD_BROWSER_OK=1` only after CoS coordinates a one-time visible MCP sign-in with {{OWNER_FIRST}}. Then verify the same Google Doc through two complete headless MCP launch/stop cycles without a sign-in redirect.
- Forms, Sheets, and dialog interfaces support direct DOM automation. Google Docs canvas work often requires menu and keyboard commands.
- Re-verify the artifact after every UI change. Prefer an API read-back; otherwise, inspect the resulting UI state.
- Give manual instructions only after both the API and Chrome DevTools paths fail.

## Workflow (Google Doc, on explicit request only)

The steps name Google Docs API operations. Run them through `switchboard drive` or any Docs API client you trust.

1. **Copy your template** (`YOUR_DOC_TEMPLATE_ID`, personalize here) with the Drive API `files.copy`, named after the document. With no template, create an empty doc and apply the house style below.
2. **Read the copy** with `documents.get` and note the template's named styles. They are the source of truth for fonts, sizes, colors and spacing.
3. **Replace the placeholder body** after the info box. Update the existing title paragraph's text instead of recreating it, so it keeps the `TITLE` style.
4. **Leave the info box alone** (see "Doc info box"). The user edits it by hand.
5. **Insert the content** below the info box: your client's Markdown insert if it has one, otherwise `insertText` plus `updateParagraphStyle`, `createParagraphBullets` and `insertTable` requests. Never paste frontmatter or a metadata block (Status, Owner, Date) into the body.
6. **Normalize** (see "After an insert"). This is mandatory after every bulk insert and every Markdown conversion.
7. **Style the content tables** (see "Tables"), never the info box.
8. **Verify** with `documents.get`: the title uses `TITLE`, sections use `HEADING_1` to `HEADING_3`, body paragraphs use `NORMAL_TEXT` with no leftover run-level overrides.
9. **Review the first draft against the sub-agents** and synthesize the feedback.
10. **Share the link** with the user.

## Default Review Step

For every newly created document, run a sub-agent review after the first complete draft is in place.

- Use the most relevant reviewer set from `~/Development/switchboard/sub-agents/`
- At minimum, include the reviewers that best match the doc type and audience
- Synthesize common feedback, flag conflicts between reviewers, and surface the highest-priority changes
- If the user asks for a lightweight pass, keep the review concise rather than skipping it entirely

## Document Structure (Google Doc)

**Never insert horizontal rules in generated Google Docs: no `horizontalRule` elements through the Docs API and no `---` thematic breaks in Markdown destined for Docs. Use heading hierarchy and spacing to separate sections.** (Vault Markdown notes are exempt; see the Markdown review-note workflow.)

Every doc follows this order:

```
Title (TITLE)
Doc info box (optional, from the template)
TL;DR (HEADING_1)
Section headings (HEADING_1), subsections (HEADING_2, HEADING_3)
```

**Owner default:** the first content section after the info box is **`TL;DR`**, not "Executive summary". Write it per "Overview and summary sections" above: today's gap with examples, the why now, and what the doc covers and asks. Dates, tiers, counts, owners and unresolved decisions go in Status, Scope or Decisions sections below it.

## Doc info box

An optional small table directly under the title that holds Status, Owner, Last updated and Related docs. When the template has one, it is a template artifact: never rewrite, flatten or restyle it, and leave its chips and date elements for the user to edit. When you need one and the template has none, build it from [references/context-box.md](references/context-box.md).

## Named styles

Your template's named styles win. Read them with `documents.get` (`namedStyles`) and apply styles by `namedStyleType`, never by hand-set fonts, sizes or colors. With no template, use this house style:

| Level | Named style | Size | Space above | Space below | Line spacing |
|---|---|---|---|---|---|
| Title | `TITLE` | 24pt | 0pt | 6pt | 100 |
| H1 | `HEADING_1` | 18pt | 20pt | 6pt | 115 |
| H2 | `HEADING_2` | 14pt | 16pt | 4pt | 115 |
| H3 | `HEADING_3` | 12pt, bold | 12pt | 4pt | 115 |
| Body | `NORMAL_TEXT` | 11pt | 0pt | 0pt | 115 |

Headings inherit the body font. Keep one font family for the whole doc: the template's, or your brand font when Docs offers it.

**Hard rule: every doc's main heading uses the `TITLE` named style.** That holds for copied templates, new tabs, inserts into an existing tab, and Markdown conversions. A Markdown `#` usually lands as `HEADING_1`, so promote the title paragraph to `TITLE` as part of normalization.

## After an insert

Bulk inserts often leave paragraph-level and run-level overrides behind, and the style dropdown in the Docs UI does not show them. After every bulk insert, read the doc with `documents.get` and send one `batchUpdate`:

1. **Headings.** Re-apply `namedStyleType` to each heading paragraph and clear any `spaceAbove`, `spaceBelow` or `lineSpacing` override, so the named style shows through.
2. **Body runs.** On every `NORMAL_TEXT` paragraph, clear run-level `fontSize`, `foregroundColor` and `weightedFontFamily` with an `updateTextStyle` whose `textStyle` is empty and whose `fields` lists those three.
3. **Body spacing.** Give every body paragraph outside the info box the house line spacing (115, or the template's) with zero space above and below.
4. **Info box check.** Confirm the info box still matches the template.

Then check with `documents.get` before sharing: plain-text extraction hides style and bullet damage.

### Inserting into a tab

- Every `range`, `location` and `tableStartLocation` in a `batchUpdate` names the `tabId`, and `replaceAllText` uses `tabsCriteria`.
- After any tab write, compare the doc's bulleted-paragraph count with the source's list-item count. When every paragraph came out bulleted, repair only the wrong ranges with `deleteParagraphBullets` and reset indents in one revision-checked `batchUpdate`; never rewrite the tab.
- Strip relative or local-file Markdown links before inserting; they become broken links.
- A heading right after a code block or a numbered list can land as `NORMAL_TEXT`; fix it with a targeted `updateParagraphStyle`.

## Tables

Content tables follow the template's table style when it has one. With no template, use this house style, and never apply it to the info box (the first table, or any 1×1 table before the first `HEADING_1`):

- **Header row:** fill `#EEF1F4`, bold text in `#1F2328`, content alignment MIDDLE.
- **Body rows:** no fill, regular text, content alignment TOP.
- **Borders:** 0.5pt solid `#C9CED6` on every cell. Cell padding 5pt.

Apply it with one `updateTableCellStyle` for the header row (`rowIndex` 0, `columnSpan` all columns), one for the body rows, and one `updateTextStyle` for the header text.

## Writing Standards (always apply)

These apply to every document and piece of content this skill produces:

1. **Sentence case** in headings, labels, and titles ("Create recipe", not "Create Recipe").
2. **Plain language.** Active voice, short sentences, no jargon. Address the reader as "you" in customer-facing content.
3. **Descriptive links.** Link text says where the link goes, per "Inline links" above. Never "click here".
4. **Names as their owners write them.** Product, plan, team and partner names keep their official spelling and casing. Follow your organization's naming rules in customer-facing content (personalize here).
5. **Legal before style.** In any review, flag legal or compliance questions before style notes.
6. **Plan and list items open with a bold 1 to 3 word summary, then one or two complete sentences.** Never codes, owners, and a date alone; "short" means fewer items or sentences, never fragments. Full rule under "Writing Voice" below.
7. **Exact status words.** Say what state a thing is in, and quote numbers from a source you can cite. Full rule under "Writing Voice" below.

## Brand and design source

Your organization's brand guidelines are the default brand and design source. Keep a switchboard encoding of them under `references/brand/` and read it before any doc that names a brand, carries charts, diagrams, images, or logos, or is customer-facing. Do not copy its tables here.

- Pick the brand per that file when the organization has more than one brand.
- Apply the brand voice and terminology rules from that file.
- Charts, diagrams, and inserted images use the brand's data color ramp. Get official logos and brand assets from your organization's brand source; never use Google Images, Wikipedia, press kits, or redraw them.
- No real customer brands in images: a doc, review note, or cover never shows a real customer's company name, logo, or other identifying text, and never a screenshot with real customer data. A customer, staff, or hardware-in-use photo is fine once every brand mark is cropped or retouched out and each crop is checked.
- Stronger constraints win: the legal check below when it applies, then this skill's template named styles, fonts, and table styling. When Google Docs cannot load the brand font, brand typography never overrides the template.

## Customer-Facing Content: Legal Check (when your organization has one)

If your organization has legal review rules for customer-facing content, keep them in one file the skill can read (personalize here) and read it before drafting any customer-facing deliverable: docs, scripts, captions, web copy, marketing text, demo content. Apply the rules plainly, with one qualifier per claim and disclosures in one place rather than scattered hedges. End each such deliverable with a short **"Legal check"** list: the disclosures it needs, the claims it makes and where each one is sourced, and any open question for your legal reviewers.

## Writing Voice

- Sentence case, casual and direct, properly capitalized
- Avoid emdashes (—) unless they fit organically; prefer colons, commas, or separate sentences
- Default to short paragraphs for summaries, recommendations, rationale, and narrative sections
- Use bullets only when they materially improve scanability, such as options, checklists, or crisp itemized lists
- When source material arrives as bullet-heavy notes, rewrite it into prose by default instead of preserving the list shape
- Keep bullet lists sparse and short when you do use them
- Use tables for comparisons
- Concise: say it in fewer words when possible
- All links must be clickable (never raw URLs without hyperlinks); link text per "Inline links" above
- Avoid semicolons and run-on sentences. When a paragraph or cell contains multiple ideas, split into separate sentences or bullets.
- **No staccato fragment chains.** Never chop one thought into a string of clipped short sentences ("Reminders help. They are not enough. Add an approval step..."). Write the full connected thought in one flowing sentence, joining related clauses with a colon, "rather than," "because," or "so" ("Reminders help only when they are approved first, so add an approval step before any reminder sends and log who approved it."). This applies to bullets, table cells, and body prose alike. Splitting genuinely separate ideas into separate sentences is still fine; the ban is on fragmenting a single idea.
- **Plan and list items: bold lead, then full sentences.** Every plan item, ranked list item, option, or action bullet in a note, Google Doc, Slack post, or chat reply opens with a bold 1 to 3 word summary, then one or two complete sentences that say what it is and why it matters, with owner and date inside the sentence or at the end in plain words. Internal codes like D1, M1, L1 are never the lead; they may follow the summary in parentheses. "Short", "shorter", or "brief" from {{OWNER_FIRST}} means fewer items or fewer sentences per item, never fragments: a short plan still has the bold lead and at least one full sentence per item. A chat reply presenting a plan follows the same shape.
  - **Counter-example.** A plan item that reads "D1 reminders flag on, templates in every account, D0 copy parity. Sam, Priya. Mar 3." has a code, owners and a date, and no sentence. A request for "a short plan" means fewer items, not a word-count cut that leaves fragments like this.
  - **Model item.** "**Overdue reminders** (D1). Turn on the reminders flag and add the approval step to every account, so each household reviews a reminder before it sends. Sam and Priya, by March 3."
  - In a Google Doc the lead is bold text inside a native list item; on Slack it is `*bold*` inside a `rich_text_list` item; in Markdown and chat it is `**bold**` at the start of the bullet.
- **Messages link every reference and @mention every person.** Every message drafted for {{OWNER_FIRST}} (Slack DM, channel post, thread reply, Comms Triage draft, GitHub PR body or comment, Linear, Jira, Confluence, Google Doc comment, email) links inline to every thread, message, PR, issue, doc, or session it refers to (Slack `<https://url|anchor>`, GitHub PR URL, Linear or Jira URL, Google Doc URL; never "the draft PR" or "yesterday's message" without the link) and writes every person it names as the surface's native mention (Slack `<@Uxxxx>` via `switchboard slack people`, GitHub `@handle`, the tool's @mention elsewhere) with two or three words saying who they are on first mention when the recipient may not know them; a name the recipient would not recognise is dropped or becomes a role. Email uses full names with no @mention but still links. Any file, reference, section, step, or shorthand a message names (`ref 05`, `SKILL.md`, "the design doc", "step 7", "the size test") links to the actual artifact (GitHub file URL at the PR head or the PR files anchor, Google Doc heading URL, Confluence anchor) and spells out the full file or section name on first mention; shorthand with no link never ships in an external message. Any internal team, process, or tool that has a short link (an intranet short link or equivalent) is linked that way on first mention. Canonical text: `system/prompts/external-replies.md` Rule 4 item 6.
- **Exact status words, sourced numbers.** Describe each thing in the state it is actually in: drafted, suggested, scheduled, approved, or sent. Never write that something happened before it did ("Here is the draft for your review", not "Your reminder went out" while it waits for approval). Every number about a product comes from a source you can cite, quoted as that source states it, and any footnote or disclosure the source requires travels with the number wherever it appears.
- **Table cells with multiple requirements or behaviors:** use native Google Docs bullets with one complete sentence per bullet. Never compress checklist-like content into dense semicolon-chained prose, and never use clipped sentence fragments in explanatory cells.

## Converting Markdown to Google Docs

When the user asks to convert, publish, or move any Markdown (a local `.md` file or inline Markdown) to Google Docs, use the same workflow. Never create a blank doc and paste. Always:

1. Copy the template.
2. Update the title paragraph with the Markdown `#` title or the filename.
3. Map headings: `#` sections to `HEADING_1`, `##` to `HEADING_2`, `###` to `HEADING_3`.
4. Insert the content (Workflow, step 5).
5. Normalize (see "After an insert").
6. Style the content tables.
7. Share the link.

This applies to every Markdown source: files in `outputs/`, `workstreams/`, `projects/`, inline Markdown from chat, or anything else the user asks to turn into a Google Doc.

## Batch Update Order

When applying multiple operations via `docs batch-update`, process deletions and insertions from **end of document to beginning** to prevent index shifts from invalidating subsequent ranges. Always re-fetch indices with `docs get` if unsure.

### Concurrent edit guard (live-edited docs)

For index-based edits to a doc the user (or anyone) may be editing live: fetch content and `revisionId` together, compute indices and write in one script, and pass `writeControl.requiredRevisionId` on the batch update so concurrent edits fail safely instead of landing in the wrong cell. If a revision race aborts the write, recover with exact-string `replaceAllText` (index-independent) when possible, then re-fetch and retry indexed edits.

### Known Docs API quirks

- Inserted table rows may copy dropdown chips that the API cannot subsequently set — flag these for manual cleanup instead of retrying.
- `replace_text`-style operations reject empty replacements; remove placeholder text with a delete-content range instead.
- Transient `500 timed out` errors are common on large multi-tab docs — retry with backoff.
- Never resolve doc comments; replying or incorporating feedback is not permission to resolve (authoritative rule in `~/AGENTS.md`).

## Quick start

1. `files.copy` the template (`YOUR_DOC_TEMPLATE_ID`) to a new doc named for the document.
2. `documents.get` the copy for its id, indices and named styles.
3. Replace the placeholder body after the info box and update the title text in place.
4. Insert the content below the info box.
5. Normalize in one `batchUpdate`: heading styles, body run overrides, body spacing.
6. Style the content tables, never the info box.
7. `documents.get` again to verify, then share the link.
