# Authoring Reference

Part 1 is a short note for a React deck kit you bring yourself. Part 2 is the Reveal.js editorial shell, the default path. The quote-sourcing and accessibility rules at the end apply to both.

# Part 1: your own React deck kit

This skill ships no React kit. When you use your own, keep these conventions so the workflow in `SKILL.md`, Path B, still applies:

- **One content file.** Every string lives in a typed content module. Slide components render from data only.
- **One slide registry.** Each slide entry carries an id, a section, a short title for the overview grid, optional speaker notes, its reveal step count, and its tone (dark or light) so the footer chrome can flip.
- **Keyboard only.** Right arrow advances a reveal step, then the slide. Left goes back. Clicks never advance. Overview, notes and fullscreen sit on single keys, and `Escape` closes overlays.
- **Tokens, not hex.** Colors, type and motion come from the kit's token file. Components never carry raw hex values.
- **Reduced motion.** Every entrance degrades to a fade under `prefers-reduced-motion`, and anything that would loop stops.
- **Help on every deck.** A counted Help slide and the same three footer links as the Reveal shell.

# Part 2: Reveal.js shell

All content lives in `slides.md`. The `index.html` shell loads it with the markdown plugin (pinned Reveal.js 6.0.1). Full upstream docs: https://revealjs.com/markdown/

## Slide separators

| Marker | Meaning |
|---|---|
| `---` on its own line (blank lines around it) | New horizontal slide |
| `--` on its own line | Vertical slide under the current horizontal one |
| `Note:` at line start | Everything after it (until next separator) is speaker notes |

Vertical stacks are an **authoring** structure only: drill-down grouping, the dash indicator, and the `Esc` overview matrix. They must never become a navigation burden — see Navigation below.

## Navigation contract (durable rule)

Progressing through a deck must only ever require the **right arrow** (keyboard). Both shells enforce this:

- `navigationMode: 'linear'` — → (and ↓, which Reveal remaps to "next") walks fragments, vertical stacks, and horizontal slides in reading order.
- **Mouse clicks must never advance the deck**. Clicks are reserved for links and text selection. Do not add a click-to-advance handler.

Never remove linear mode or add a click handler. The validator fails a deck that lacks linear mode or contains a click-to-advance handler.

## Per-slide and per-element attributes

- Slide attributes — first line of a slide:
  `<!-- .slide: data-background-color="#0b2540" -->`
  Common: `data-background-image="assets/x.png"`, `data-background-gradient="…"`, `data-transition="fade"`, `data-section="Name"` (feeds the footer chrome counter), `data-state="hide-chrome"` (suppresses counter/dashes on statement slides), `data-state="scrim-left"`/`scrim-dark` (photo scrims), `data-visibility="uncounted"` (backup slides outside the count), `class="stats-live"` (count-up numbers on entry), `data-auto-animate` (morph between consecutive slides sharing `data-id`s).
- Element attributes — trailing comment on the element:
  `- Point <!-- .element: class="fragment" -->`
  Fragments reveal step-by-step. Variants: `fragment fade-up`, `fragment fade-in-then-semi-out` (lit-current/dimmed-prior list walking), `fragment bar-grow` (magnitude bars), `fragment gate-on` (gate cards).
- `data-bg="assets/photos/x.jpg"` on any fragment fades a dimmed full-bleed photo into the slide background while that fragment is visible (editorial shell only; see design system, "Live moments"). State is synced both directions, so stepping backward restores the previous photo.

## HTML in Markdown (the plugin is picky)

- Keep raw HTML blocks **flush-left** with **no blank lines inside a block** — indented or blank-line-split HTML renders unpredictably.
- One pattern block per slide; compose with the pattern skeletons in `references/design-system.md` rather than inventing new markup.
- Entities inside HTML blocks: use `&amp;`, `&nbsp;` etc. as in any HTML.

## Reveal-native helpers (use before hand-rolling CSS)

`r-fit-text` (poster-scale headline), `r-stack` (layered reveal in place), `r-stretch` (fill remaining height with one image/video), `r-frame` (hairline image frame), `data-line-numbers="1-2|4"` (stepped code highlighting), `data-preload` + `data-src` iframe (lazy heavy embeds). The full table with "where each earns its keep" is in `references/design-system.md` → Reveal-native techniques.

## Customer quotes: sourcing and evidence hierarchy (durable rule)

Any customer quote in a deck must come from a search of the **full evidence corpus** — never just one prior deck. Prior decks are discovery aids and style references, not canonical proof.

**Search order (personalize here for your product):**

1. Your product knowledge base, for example `projects/<product>/knowledge-base/` (research synthesis, weekly summaries, positioning docs).
2. UXR primary material: interview transcripts, notes docs, and longer-quotes appendices in the owning workstream's `sources/`.
3. Slack customer-feedback snapshots your collectors keep.
4. Research synthesis docs, usable when the quote is verbatim there or linked back to verbatim evidence.
5. Your research repository or feature-request tracker, where relevant.
6. Prior decks, last — to discover a quote, then trace it upstream.

**Verification rules (all mandatory):**

- Verbatim only. Never put a paraphrase in quotation marks; never fabricate. Trimming must show as an ellipsis.
- Trace every quote to the original or closest-primary source before using it. If it verifies only against a prior deck, verify the exact wording against that deck's live content and flag the caveat in delivery notes.
- Match the quote to the specific slide claim (a quote about one feature never proves a claim about another) — never reuse a visually convenient quote against the wrong claim.
- Attribution (name/business) only where the source carries it and permits it; otherwise attribute by segment/study.
- Check sensitivity: sources marked `sensitive: true` in the KB manifest (named customer quotes often are) stay in internal decks only — never in customer-facing or public artifacts without an explicit permission check.
- Weigh freshness and representativeness — prefer recent quotes from customers representative of the audience's segment.

## Per-slide source attribution

Data-bearing slides (stats, tables, claims) get a small `Sources:` footer pinned bottom-left. Markup (one line, flush-left, real URLs only — never local file paths):

```html
<div class="slide-sources"><span class="src-label">Sources:</span><a href="https://…" target="_blank" rel="noopener">Doc title</a><span class="src-sep">|</span><a href="https://…" target="_blank" rel="noopener">Second source</a></div>
```

Keep to 2–3 links. The validator fails a `.slide-sources` block without a real `http(s)` href.

## Code blocks

Fenced code blocks get syntax highlighting (highlight.js). Line highlighting steps:

    ```python [1-2|4]
    def load():
        ...
    
    def run():
    ```

`[1-2|4]` highlights lines 1–2 first, then line 4 on the next step. For eligibility rules / gating logic in editorial decks, prefer the rule-panel pattern (design system Pattern 11) over a code block.

## Speaker notes

- `Note:` block per slide; press `s` in the browser for presenter view (timer, next slide, notes). Requires serving over HTTP.
- Notes carry the narration; slides carry the takeaway. If a slide only makes sense with you talking, move words to notes.

## Images & assets

- Put images in the deck's `assets/` dir (photos in `assets/photos/`) and reference relatively: `![alt text](assets/chart.png)`.
- Always write meaningful alt text.
- Compress screenshots (target <300 KB; photos <450 KB); decks with videos should link out rather than embed when the file exceeds a few MB.
- Never upload internal screenshots/data to public hosts to embed them — keep assets local to the deck directory.
- Source-media policy applies: derivatives only, never modify originals.

## Config (index.html)

- **Editorial shell (default path):** `navigationMode: 'linear'`, `width: 1600, height: 900`, `transition: 'fade'`, `controls: false`, `pdfSeparateFragments: false`, fixed footer chrome, count-up handler. Change config in `index.html` only for deck-wide needs; prefer per-slide `data-*` attributes for one-offs.
- **Basic shell (`--basic`):** `slideNumber: 'c/t'`, `transition: 'slide'`, same linear navigation (no click-to-advance).
- Leave `autoSlide` off. Never break keyboard navigation with custom JS.

## Accessibility checklist

- Contrast: body text ≥ 4.5:1 against the background (the editorial system's `--muted` on black passes; verify custom colors).
- Don't encode meaning in color alone — pair with labels (chips carry text, not just color).
- Keep effective font size large: if you need `font-size < 0.7em` to fit, split the slide.
- Alt text on every image; tables get header rows.
- Avoid rapid auto-animations; respect `prefers-reduced-motion` (the editorial theme already disables bar/fragment animation under it).
- Readable link text ("Q3 readout doc"), not bare URLs.
