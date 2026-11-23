---
name: building-presentations
description: "Builds presentations. Default path is the Reveal.js editorial shell this skill ships (scaffold, theme, pattern library, validator). Bring your own React deck kit (for example Vite + React + Tailwind + motion) when a deck needs a component build, and this skill gives the workflow for it. Use whenever asked to make, create, draft, build, prepare, or update a presentation, deck, slides, slideshow, talk, pitch, keynote, readout, review deck, demo deck, or all-hands deck. Google Slides is an explicit export target only, never the authoring source."
---

# Building Presentations

**Default:** every deck starts on the Reveal.js editorial shell in this skill (Path A). When you keep your own React deck kit and the deck needs it, use Path B. Google Slides is never the authoring surface. It is an export target only when {{OWNER_FIRST}} explicitly asks for a Slides artifact, and the Reveal or React source stays the source of truth.

Canonical tracked source for this skill: `~/Development/switchboard/.agents/skills/building-presentations/` (copied to `~/.claude/skills/` by `sync-skills.py`, which Claude and Amp read).

## Pick the path

| Ask | Path | Why |
|---|---|---|
| Any deck: readouts, reviews, leadership updates, customer-facing talks, keynotes, all-hands | **Reveal.js editorial shell** (`scripts/scaffold-presentation.sh`) | No install step, markdown authoring, a tested pattern library and a validator |
| A deck that needs custom components or app-like motion, when you keep a React deck kit | **Your React deck kit** (Path B) | Component primitives and progressive reveal from your own kit |
| "Google Slides" | Build on the matching path above, export only on explicit request | `references/publishing.md` |

When unsure, use the Reveal.js shell. Say which path you chose in the first reply.

## Path A: Reveal.js editorial shell (default)

- One directory per deck: `index.html` (self-contained shell, pinned Reveal.js 6.0.1 via jsdelivr CDN, no npm or build step), `slides.md`, `theme.css`, `assets/`.
- Two shells: **editorial** (default: Inter Variable, 1600×900, fixed footer chrome with brand, counter, dash indicator, Help links, count-up stats, linear navigation) and **basic** (`--basic`, plain built-in theme). Forward is always `→`; mouse clicks never advance the deck.
- The editorial shell keeps the counted Help slide (`data-section="Help"`) and the three footer links; `validate-presentation.sh` fails a deck missing any link. When you personalize the links, update the URL list in that script to match.
- Default location: `~/Development/switchboard/outputs/presentations/<yyyy-mm-dd>-<slug>/`.

Workflow:

1. **Scaffold.** `scripts/scaffold-presentation.sh <target-dir> --title "Deck Title" [--brand "Footer Label"]`; `--basic [--theme black|white|...]` for the plain shell; `--force` to overwrite.
2. **Design.** Apply `references/design-system.md` (editorial dark system and the 22-pattern library); show at least 3 to 4 patterns rather than bullet slides; reach for Reveal-native helpers (`r-fit-text`, `r-stack`, `r-stretch`, `fade-in-then-semi-out`) before custom CSS.
3. **Author.** Write `slides.md` (`---` horizontal, `--` vertical, `Note:` speaker notes); data slides carry a `.slide-sources` footer with real links. Rules in `references/authoring.md`.
4. **Preview.** `python3 -m http.server 8000 --directory <deck-dir>`; the markdown plugin fetches `slides.md`, so `file://` does not work.
5. **Validate.** `scripts/validate-presentation.sh <deck-dir>`; fix every flag. Screenshot the first slide and confirm zero console errors.
6. **Deliver.** Local path and preview command, plus PDF export or publishing per `references/publishing.md`. Presenter view: `s`. PDF: `?print-pdf` then print in Chrome, or `chrome --headless --print-to-pdf=deck.pdf 'http://localhost:8000/?print-pdf'`.

## Path B: your own React deck kit (optional)

This skill ships no React kit. If you keep one (for example a Vite + React + Tailwind + motion template with a fixed 16:9 canvas, keyboard navigation and a notes panel), follow its own style guide and use this workflow:

1. **Structure first.** Propose the slide list before writing code: sections, dividers, which slides reveal in steps and how many. Prefer one stepped slide over many static ones. Get {{OWNER_FIRST}}'s nod on the structure, or state it and proceed when they asked for the deck in one go.
2. **Copy the template.** Copy your kit's template into its own deck directory and never edit the template in place. Keep every word in one content file so copy edits never touch layout code. Headlines at most 8 words, body at most 2 sentences, quotes verbatim and attributed. Record every edit to {{OWNER_FIRST}}'s copy so they can review it.
3. **Build from primitives.** Reuse the kit's slide, eyebrow, reveal and divider components. Add a new layout only when no existing one fits, and only with the kit's design tokens, never raw hex colors in components.
4. **Brand and imagery.** Use real brand assets from your organization's brand source, never a redrawn mark. No real customer brands in images: no company name, logo, or customer data. Product UI comes from demo or staging accounts with fake data.
5. **Motion discipline.** One easing curve, fade-and-rise entrances, no bounces, gradient text or glows. Respect `prefers-reduced-motion`. If {{OWNER_FIRST}} asks for something flashier, push back once, then do the tasteful version.
6. **Help on every deck.** End with a counted Help slide and show the same three links in the footer of every slide: a DM to the deck owner, the team channel, and the team resource hub (personalize here).
7. **Verify before showing.** Run the dev server, screenshot every slide and every reveal step at 1600×900, 1440×900 and 390×844, and check that nothing overflows, text never sits on an unscrimmed photo, and the step counts match. The production build must pass.
8. **Deliver.** Share the deck directory, the dev command, the structure summary, the copy edits and the screenshots. Deploy only when {{OWNER_FIRST}} asks (`references/publishing.md`).

## Brand and design source

Your organization's brand guidelines are the brand source; keep a switchboard encoding of them under `references/brand/` and read its deck guidance during the design step on either path.

- The editorial shell uses Inter Variable on a deep blue-black canvas. Keep that. The brand guidelines fill gaps; they do not replace `references/design-system.md`.
- The single accent is the brand color (the `--brand` token in `theme.css`, or your kit's brand token); charts and magnitude bars use the brand data tone ramp; small text never sits on the accent, and text on the accent uses the canvas color (`--bg`).
- Headline leading 1.08 and tracking −2.5%, body leading 1.45, `tnum` on numerals.
- Brand marks come from the branding source of truth (your organization's brand source or Figma), never a typed wordmark as the logo and never a redrawn shape.
- No real customer brands in any slide image: no company name, logo, or real customer data on either path. Customer photos only with every brand mark cropped or retouched out and each crop checked.
- Brand voice rules in that file apply to copy. Legal patterns and the accessibility checklist win over brand preferences.

## Google Slides (export only, on explicit request)

Never author in Google Slides. When {{OWNER_FIRST}} explicitly asks for a Slides artifact, finish the deck on its path first, then export per `references/publishing.md` (PDF import, or a native rebuild through a Slides-capable connector) and say the deck source is the source of truth.

## Content quality

- One idea per slide; headline states the takeaway, body proves it. Every major claim carries a verbatim, attributed human quote.
- Customer quotes: search the full evidence corpus (KB, UXR transcripts, Slack feedback, research synthesis), never a single prior deck. Verbatim only, traced to primary, sensitivity-checked. See `references/authoring.md`, "Customer quotes: sourcing and evidence hierarchy".
- Customer-facing content applies your legal review patterns: one qualifier per claim, disclosures in the disclosure block, approved quant claims quoted verbatim with their disclosure, and a closing "Legal requirements summary" in the delivery note.
- Prefer tables and diagrams over prose walls; speaker notes carry the narration, not the slide.
- Apply your own review-lens pass (personalize here: a skill that encodes your judgment and taste) before delivering any substantive deck.

