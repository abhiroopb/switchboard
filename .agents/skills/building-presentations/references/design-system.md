# Editorial Design System (dark decks)

The visual system for the Reveal.js editorial shell. `templates/theme-editorial.css` implements every class named here, `templates/index.html` carries the footer chrome and the small scripts the patterns rely on, and `templates/slides.md` is a working specimen. The goal is a deck that looks designed without per-slide fiddling: a quiet dark canvas, one accent, generous space, and a small set of layouts that do one job each.

Swap the accent, the font, and the mark for your own brand. Keep the structure.

## Principles

1. **Type does the work.** Inter Variable from the fontsource CDN (`@fontsource-variable/inter`), or your brand font first in the stack when it is installed. Weights: 400 for body, 500 for labels and eyebrows, 600 for headings, 650 for big numerals. Headings are sentence case, never uppercase.
2. **A deep, layered canvas.** A blue-black background with three surface steps above it. Depth comes from surface steps and hairlines, not drop shadows. The only exceptions are a soft text shadow on ghost numerals over photos and a small glow on the active timeline dot.
3. **One accent.** Signal teal marks the eyebrow rule, active states, key numerals, and links. Status colors appear only where they carry status meaning, and never as decoration.
4. **Quiet labels, loud claims.** Eyebrows are small, uppercase, widely tracked and muted. Headlines are large, tight and short.
5. **Real margins.** A 1600×900 logical canvas, 80px side padding, statements no wider than about 24em. A slide that fills every pixel has failed.
6. **Images that mean something.** Every photo or screenshot earns its place on that slide. Treat it: a rounded card with a hairline, or full bleed under a scrim. Never place a bare rectangle on the canvas.

## Tokens

```css
--bg:#0c0d10; --surface:#111317; --card:#171a1f; --elevated:#1f232a;
--ink:#f4f5f7; --subtle:#c2c6cd; --muted:#959aa3; --faint:#646a73; --ghost:#3b4047;
--brand:#66c2b7; --brand-soft:rgba(102,194,183,.12);
--line:rgba(255,255,255,.09); --line-soft:rgba(255,255,255,.05); --line-strong:rgba(255,255,255,.18);
--positive:#7fc98a; --warning:#e0a450; --info:#8fa3c4;
/* radius: 14 / 16 / 18 / 20 / 22 / 24, pills 999px */
```

Text steps on the canvas, brightest first: `--ink` for headlines and the numbers that matter, `--subtle` for leads and quotes, `--muted` for body, `--faint` for captions and small labels (the eyebrow uses `--muted`), `--ghost` for watermark numerals and separators. Most body text is `--muted`. Keeping `--ink` rare is what makes it land.

Contrast: `--muted` on `--bg` is about 7:1 and `--faint` about 3.6:1 (about 3.2:1 on `--card`), so `--faint` is for large labels and captions only, never for body copy or small text on cards.

## Type scale

The base is 30px on the 1600×900 canvas. Sizes are in em so a deck can scale them together.

| Role | Size | Weight | Tracking | Leading |
|---|---|---|---|---|
| `h1` display | 3em (90px) | 600 | -0.025em | 1.08 |
| `h2` slide headline | 2em (60px) | 600 | -0.025em | 1.08 |
| `h3` card title | 1.1em (33px) | 600 | -0.015em | 1.2 |
| `.lead` intro | 0.82em | 400 | normal | 1.4, 42ch measure |
| Body and list items | 1em | 400 | normal | 1.45 |
| `.kicker` eyebrow | 0.44em | 500 | 0.16em, uppercase | inherited (1.45) |
| `.byline` | 0.5em | 400 | 0.06em | inherited |
| `small`, captions | 0.5em | 400 | normal | 1.65 |
| Numerals (`.stat-num`, the close `h1`) | per pattern | 650 | tabular (`tnum`) | 1 |

Headlines stay at eight words or fewer. If a headline needs two lines, the second line can step down to `--faint` with `<span class="dim">`.

## Eyebrow and quote

- **Eyebrow (`.kicker`).** A 1.4em by 2px accent rule, then the label in uppercase. One per slide, above the headline.
- **Evidence quote (`.evidence-quote`).** A 3px `--line-strong` bar on the left, the quote at 0.8em in `--subtle`, a 38ch measure, curly quote marks in the text itself, and a small `--muted` caption led by a short hairline dash. The caption reads `Name, Organization, Source`, never italic.

## Photo treatment

- **Full bleed.** `data-background-image` plus `data-state="scrim-left"` (text on the left) or `data-state="scrim-dark"` (centered text on an evenly darkened photo). Put the text on the darkest third. Add `data-background-opacity="0.9"` for a bright photo.
- **Photo card.** `<div class="photo-card"><img …></div>` gives rounded corners, a hairline, and a soft bottom gradient. Optional `<span class="photo-num">02</span>` for a ghost numeral and `<span class="photo-tag">Label</span>` for a caption chip.
- Photos live in `assets/photos/`, compressed under 450 KB. No decorative stock.
- **One photo, one moment.** Each photo path appears at most once in `slides.md`, and two photos of the same person never stand in for two different people. Check before delivery: `rg -o 'assets/photos/[^"]+' slides.md | sort | uniq -c`. Product screenshots may repeat when an overview-then-detail build needs the same image in both states.

## Pattern library

Each pattern is a skeleton for `slides.md`. Keep raw HTML flush-left with no blank lines inside a block, because the markdown plugin mangles indented or split HTML. The example text is placeholder: replace all of it.

### 1 · Full-bleed statement

Title slides and section pivots. Two to four per deck at most.

```markdown
<!-- .slide: data-background-image="assets/photos/scene.jpg" data-background-size="cover" data-state="scrim-left" data-section="Intro" -->
<div class="kicker">Planning review</div>
# Next year's plan &<br><span class="dim">what it needs.</span>
<p class="byline">Presenter name · Month YYYY · Internal</p>
```

### 2 · Editorial statement

One claim on the bare canvas: eyebrow, a short headline, one `.lead` paragraph. No bullets. Use it to open an argument before the evidence.

### 3 · Stepper rail

A short story told one step per vertical slide. Every slide in the stack carries `data-auto-animate`; rail items and the media card carry `data-id` so the active state and the image morph between steps. Three to five steps.

```markdown
<!-- .slide: data-auto-animate data-section="Plan" -->
<div class="kicker">Plan · 1 of 3</div>
<div class="stepper">
<div class="step-rail">
<div class="step-item on" data-id="s1"><span class="step-num">01</span><span class="step-name">Pilot</span></div>
<div class="step-item" data-id="s2"><span class="step-num">02</span><span class="step-name">Expand</span></div>
<div class="step-item" data-id="s3"><span class="step-num">03</span><span class="step-name">Retire the old flow</span></div>
</div>
<div class="step-body">
<h2>What the pilot proves.</h2>
<p>Two lines of context at most.</p>
<blockquote class="step-quote">A grounding quote or decision.<span class="attrib">— Source, date</span></blockquote>
</div>
<div class="photo-card step-media" data-id="media"><img src="assets/photos/one.jpg" alt=""><span class="photo-num">01</span></div>
</div>
```

The next slide repeats the structure with `on` moved and the image swapped, keeping `data-id="media"` so it crossfades.

### 4 · Journey timeline

A media card on the left (`.media-card`: photo, title, two or three bullets) and a vertical `.timeline` on the right. `.done` marks passed rows and `.on` the current one. Good for phased plans and sequences of outreach.

```markdown
<div class="journey">
<div class="media-card"><div class="photo-card"><img src="assets/photos/x.jpg" alt=""></div><h3>Card title</h3><ul><li>…</li></ul></div>
<div class="timeline">
<div class="tl-item done"><span class="tl-when">Week 1</span><span class="tl-label">Announcement sent</span></div>
<div class="tl-item on"><span class="tl-when">Now</span><span class="tl-label">Office hours running</span></div>
<div class="tl-item"><span class="tl-when">Week 4</span><span class="tl-label">Old flow switched off</span></div>
</div>
</div>
```

### 4b · Timeline walk

Pattern 4 driven by fragments: the highlight moves down the rail and a matching detail card swaps in on the left. Use it when each milestone deserves its own panel.

Markup: `.journey.tl-walk` holds `.tl-details` (one `.media-card.tl-detail[data-i]` per row, stacked, only `.on` visible) and `.timeline` (`.tl-item[data-i]` rows), plus one empty `.fragment.tl-step` for every row after the first. `syncTimelineWalk()` in `templates/index.html` derives the state from which `.tl-step` fragments are visible, so stepping backward and deep links render correctly, and it does nothing on decks without a `.tl-walk`.

Rules: `data-i` counts from 0 and matches between details and rows. Five to eight rows. Each detail card adds something the row label does not. Dates that are not commitments say so ("Q3, working target").

```markdown
<div class="journey tl-walk">
<div class="tl-details">
<div class="media-card tl-detail" data-i="0"><div class="photo-card"><img src="assets/photos/a.jpg" alt=""></div><h3>Milestone A</h3><ul><li>…</li></ul></div>
<div class="media-card tl-detail" data-i="1"><div class="photo-card"><img src="assets/photos/b.jpg" alt=""></div><h3>Milestone B</h3><ul><li>…</li></ul></div>
</div>
<div class="timeline">
<div class="tl-item" data-i="0"><span class="tl-when">Q2</span><span class="tl-label">Milestone A</span></div>
<div class="tl-item" data-i="1"><span class="tl-when">Q3</span><span class="tl-label">Milestone B, working target</span></div>
</div>
<span class="fragment tl-step"></span>
</div>
```

### 5 · Overview to detail

An overview slide with three or four `.tile`s in a `.tile-row`, then vertical detail slides where the same tiles (same `data-id`s) shrink into a `.tile-rail` on the left, the active one bright and the rest `.dim`, with the detail in `.detail-body`. Every slide in the stack carries `data-auto-animate`.

```markdown
<!-- .slide: data-auto-animate -->
<div class="tile-row">
<div class="tile" data-id="t1"><img src="…"><span class="tile-label">Setup</span></div>
<div class="tile" data-id="t2">…</div>
<div class="tile" data-id="t3">…</div>
</div>

--

<!-- .slide: data-auto-animate -->
<div class="detail">
<div class="tile-rail">
<div class="tile sm" data-id="t1"><img src="…"><span class="tile-label">Setup</span></div>
<div class="tile sm dim" data-id="t2">…</div>
<div class="tile sm dim" data-id="t3">…</div>
</div>
<div class="detail-body"><h2>…</h2><ul>…</ul></div>
</div>
```

### 6 · Split

Text and one `.photo-card` side by side in a `.split` container: the text in `.split-text` first, then the card. One idea that deserves an image but not a full bleed.

### 7 · Numbers

A `.stat-row` of `.stat-card`s, each a large `.stat-num` and a small `.stat-label`. `.stat-card.hero` lifts one card onto the accent tint. Five numbers at most: if every number is big, none is. Add `class="stats-live"` to the slide for the count-up described under Motion.

### 8 · Hairline table

A plain markdown table. The theme draws small uppercase headers and hairline rules with no fills. Show status with `.chip` (`live`, `motion`, `hold`, `no`, `decided`), never emoji. Put `class="compact"` on the slide for more than five rows.

### 9 · Pull quote

`.quote-slide` with a `.qmark`, the `.quote` in `--ink` at about 1.35em, and `.attrib` in `--faint`. One per section at most.

### 10 · Decisions

Numbered `.decision-card`s: `.d-num`, `.d-title`, and a one-line `.d-rec` with the recommendation in `<b>` (accent). Usually the payoff slide. Three decisions at most.

### 11 · Rule panel

Never show eligibility rules, gating logic, or configuration as a code block: it renders small and reads as an afterthought. Use `.rule-panel`: a `.rule-card` of mono `.rule-line`s (operators in `.op`, nested clauses in `.indent`) next to a `.confirm-card` checklist of `.confirm-item`s. Reveal each card with `fragment fade-up`.

### 12 · Caption cards

A `.caption-row` of three `.caption-card`s: the image on top at a 16:11 crop and one short title in a solid `.caption` bar below. For naming parallel things that each deserve a picture. The caption is a title, never a sentence.

### 13 · Definition cards

A `.def-row` of three or four `.def-card`s: an `h3` term, a one-sentence `.def-lede`, a hairline, and an italic `.def-why` that says why it matters. The hairline and the shift to italic separate the definition from its consequence.

For a before-and-after pair, use exactly two cards and close with a full-width `.def-span` band (`h3` label plus `.span-lede`) so the shared takeaway visibly belongs to both. An evidence quote fits under the band.

### 14 · Insight rows

`.insight-row`s divided by hairlines. Each row: a mono `.i-num`, an `.i-main` with a bold `.i-title` claim and an italic `.i-body`, and an `.i-quote` on the right (`.q` and `.q-attrib`). Claim on the left, evidence on the right, on every row. Three or four rows. The strongest pattern for findings and for decision asks.

### 15 · Gate cards

A `.gate-row` of up to five numbered `.gate-card`s with `fragment gate-on`: every card shows dimmed from the start and lights up as its fragment fires, so the audience sees the whole set while attention moves card by card. Close with an evidence quote on `fragment fade-up`. For lists where the count matters as much as the items.

### 15b · Evidence quote

The standard quote inside a content slide, under a card row or anywhere a claim needs proof:

```html
<figure class="evidence-quote fragment fade-up"><blockquote>“Quote text, curly quotes included.”</blockquote><figcaption>Name, Organization, Source</figcaption></figure>
```

It is the mid-size unit between the full pull-quote slide (pattern 9) and the compact paired quotes in patterns 14 and 22. The treatment is described under "Eyebrow and quote".

### 16 · Pillar cards

A `.pillar-row` of four `.pillar-card`s: an `h3` with a mono `.p-letter` badge (A, B, C, D), a one-sentence `.p-lede`, then three or four short bullets. The letters turn the cards into a frame people can cite ("that's pillar C"). This is the densest card pattern, so add `class="slim"` and keep bullets short.

### 17 · Chapter divider

`class="chapter"` on the slide: a mono `.ch-nums` pair (section and total) joined by a `.ch-line` hairline, a one- or two-word `h2`, and a `.ch-quote` (`.q` and `.q-attrib`). Mostly empty on purpose. One per deck, before the payoff or a live demo. A faint radial `data-background-gradient` that ends at `--bg` makes the pause read as deliberate.

### 18 · Statement close

`class="statement-close"` on the last counted content slide: a `.kicker`, an `h1` with Reveal's `r-fit-text` so the line fills the width, and a `.byline` naming where the work continues. The thesis in three to five words, not "Thank you". Always last before Help and Backup.

### 19 · Magnitude bars

`.mag-rows` of `.mag-row fragment bar-grow` rows: `.m-label`, an `.m-track` with an `.m-bar` that grows to `--w` when its fragment fires, and a mono `.m-num`. All bars share one linear scale: the largest value is `--w: 100%`, the rest are true proportions, and tiny values keep a 4px minimum width so they stay visible. Say so in a `.mag-note`. Use it as a drill-down under a numbers slide (pattern 7): the cards state the numbers, the bars make the ratios felt. Never a log scale.

```markdown
<div class="mag-rows">
<div class="mag-row fragment bar-grow" style="--w: 100%"><div class="m-label">Invited</div><div class="m-track"><div class="m-bar"></div></div><div class="m-num">48,000</div></div>
<div class="mag-row fragment bar-grow" style="--w: 6%"><div class="m-label">Active last month</div><div class="m-track"><div class="m-bar"></div></div><div class="m-num">2,880</div></div>
</div>
<small class="mag-note">Linear scale: the active group is a sliver of the invited one.</small>
```

### 20 · Layered image walk

An `.r-stack.surface-stack` of `<figure>`s, each a full-size treated image with a `<figcaption>` holding a mono `.s-num` and one caption line. The first figure is the base; the rest are plain fragments that layer on top. Figures carry an opaque background so lower layers never show through. Three or four layers, each caption adding information. Use it as a drill-down under pattern 5 or 12.

### 21 · Backup bank

After the close and the Help slide: a chapter-style "Backup" divider and a vertical stack of reference slides (definitions, FAQ tables, requirement matrices). Every one carries `data-visibility="uncounted"`, so the official count ends at the close while likely questions have answers one arrow away. Backup slides compress what the deck already said into tables. The footer counter shows `— / total` on them. A lazy `<iframe data-src="…" data-preload>` slide can embed a full source document.

### 22 · Surface walk

One slide, three build steps. With no fragment shown, three screenshots sit side by side with labels over a bottom scrim. Each press collapses them into tabs on a left rail, lights one tab, and crossfades its detail panel in on the right: heading, a one-line lede, two or three bullets, an optional `.sw-quote`, and a screenshot in `.sw-shot`. Screenshots need no caption chip. If one needs a label, `.photo-tag` pins bottom-right, clear of the product's top navigation. `syncSurfaceWalk()` in `templates/index.html` derives the step from the visible `.sw-step` fragments, so backward steps and deep links work, and it does nothing on decks without an `.sw-stage`.

```markdown
<div class="sw-stage">
<div class="sw-card" data-i="0"><img src="assets/a.png" alt="…"><span class="sw-label">Inbox</span></div>
<div class="sw-card" data-i="1"><img src="assets/b.png" alt="…"><span class="sw-label">Reports</span></div>
<div class="sw-card" data-i="2"><img src="assets/c.png" alt="…"><span class="sw-label">Settings</span></div>
<div class="sw-detail" data-i="0">
<div class="sw-text">
<h3>Inbox</h3>
<p class="sw-lead">One-line lede.</p>
<ul>
<li>…</li>
</ul>
<div class="sw-quote"><div class="q">"Verbatim quote."</div><div class="q-attrib">— Name, organization · segment</div></div>
</div>
<div class="sw-shot"><img src="assets/a.png" alt="…"></div>
</div>
<span class="fragment sw-step"></span>
<span class="fragment sw-step"></span>
<span class="fragment sw-step"></span>
</div>
```

Rules: exactly three cards, because the geometry is tuned for a trio on the 1600px canvas. Quotes are verbatim with real attribution, or left out. Every step reads on its own in a PDF. Once per deck.

## Live moments

Interaction earns its place when it adds pacing, comparison, or focus that a static slide cannot. Three rules keep it safe:

1. **The markup holds the final state.** HTML always contains the finished values and layout. Scripts and fragments only animate toward it, so no-JS and PDF renders are right automatically.
2. **No animation under automation.** Entry scripts check `navigator.webdriver`, so export tools and QA browsers capture final values.
3. **Every fragment step stands alone,** because an export tool such as decktape writes one page per step. Reveal's own `?print-pdf` writes one page per slide (`pdfSeparateFragments: false`) in the final state.

Per-fragment background photos: a `.fragment` with `data-bg="assets/photos/x.jpg"` fades a full-bleed, heavily dimmed photo into the slide background while that fragment is visible. `syncFragmentBackgrounds()` in `templates/index.html` builds and syncs the layers, so backward steps show the right photo. The scrim keeps text at full contrast, so choose photos for mood, not information. A PDF shows the last fragment's photo.

Count-up: on a slide with `class="stats-live"`, `index.html` counts each `.stat-num` up from zero over about 900ms, keeping prefixes, separators, decimals and suffixes, and always ending on the exact original text. Use the shipped parser.

## Choosing a pattern

| Slide job | Pattern |
|---|---|
| Open the deck or pivot to a new section | 1 full-bleed statement |
| Make one claim | 2 statement, 9 pull quote |
| Tell a short sequence | 3 stepper |
| A phased plan with one picture | 4 journey |
| Milestones that each need detail | 4b timeline walk |
| Survey several areas, then drill in | 5 overview to detail |
| One idea and one image | 6 split |
| Numbers that must land | 7 numbers |
| Status or reference matrix | 8 table |
| Ask for decisions | 10 decisions, 14 insight rows |
| Rules and gating logic | 11 rule panel |
| Name parallel things with pictures | 12 caption cards |
| Define a vocabulary | 13 definition cards |
| Findings with evidence | 14 insight rows |
| A list where the count is the point | 15 gate cards |
| A named frame (A, B, C, D) | 16 pillar cards |
| Reset attention before the payoff | 17 chapter divider |
| End on the thesis | 18 statement close |
| Make ratios land | 19 magnitude bars, under 7 |
| Show several images at full size | 20 layered image walk, under 5 or 12 |
| Walk product surfaces from overview to detail | 22 surface walk |

## Motion

- One deck-wide transition: `fade`. Auto-animate for the stepper and overview morphs (`autoAnimateDuration: 0.7`).
- Fragments land on one ease-out curve, `cubic-bezier(0.22, 1, 0.36, 1)`, at 0.4 to 0.5s. `fade-up` rises 14px. Gate cards light over 0.5s and bars grow over 0.9s.
- Fragment only when the reveal changes what the audience hears. Never fragment a table.
- For a list the presenter walks item by item, use Reveal's `fade-in-then-semi-out`: the current item stays lit and earlier items dim to half. The last item is exempt, because a PDF has no current fragment and would otherwise dim everything.
- No loops, no bounces, no 3D transitions. Under `prefers-reduced-motion` the theme turns off bar, timeline-walk and fragment motion. The surface walk (pattern 22) still crossfades, so keep it to one per deck.

## Reveal-native helpers

Use these before writing custom CSS.

| Helper | Job | Where it fits |
|---|---|---|
| `r-fit-text` | Scale a headline to the slide width | Pattern 18, one-word chapter titles |
| `r-stack` | Layer elements in one spot and reveal them with fragments | Before and after, pattern 20 |
| `r-stretch` | Grow one image or video to the remaining height | Full-height screenshots |
| `r-frame` | Hairline frame around an image | Inline UI captures |
| `fade-in-then-semi-out` | Lit current item, dimmed earlier items | Agendas, sequential asks |
| `data-background-gradient` | Per-slide gradient | Chapter divider and close, barely visible |
| `data-transition` | Override the transition once | One deliberate moment, never scattered |
| `data-line-numbers="1-2\|4"` | Stepped code highlighting | Walking a payload |
| `data-visibility="uncounted"` | Leave a slide out of the count | Pattern 21 |
| `data-state="…"` | Mirror a class onto `<body>` for one slide | Hiding chrome, photo scrims |
| `data-preload` + `data-src` iframe | Lazy-load a heavy embed | Source documents in backup |
| `?view=scroll` | Scrolling reading view | Mention it for readers who skip the live talk |

Reference: the reveal.js documentation at revealjs.com (layout, fragments, backgrounds, auto-animate, scroll view).

## Chrome

The footer sits outside the Reveal transform in `templates/index.html`: the brand mark and label bottom-left (set with the scaffold's `--brand` flag), the three Help links beside them, one progress dash per counted slide bottom-center, and a mono `SECTION NN / TT` counter bottom-right. Set `data-section="…"` on the first slide of each stack.

- **Sensitivity pill.** For decks with internal dates or metrics, uncomment `<span class="internal-tag">Internal</span>` next to the brand label. Leave it commented for decks safe to show outside the team, and keep exact sensitive metrics out of slides and notes.
- **Hiding chrome.** `data-state="hide-chrome"` fades the counter and dashes on full-stop slides (patterns 1, 18, 21 dividers). The brand mark stays.
- **Uncounted slides.** The counter shows `— / TT` on any slide with `data-visibility="uncounted"`.
- **Navigation.** Forward is always the right arrow. `navigationMode: 'linear'` walks fragments and vertical stacks in reading order. Mouse clicks never advance the deck, and the validator fails a deck with a click-to-advance handler.
- **Sources line (`.slide-sources`).** A small `Sources:` line pinned bottom-left of the canvas with two or three links separated by `.src-sep` pipes. `syncSlideSources()` in `index.html` moves each one onto the slide canvas so it sits at the frame edge whatever the content height. Links only to shareable URLs, never local paths, with `target="_blank" rel="noopener"`.

```html
<div class="slide-sources"><span class="src-label">Sources:</span><a href="https://…" target="_blank" rel="noopener">Doc title</a><span class="src-sep">|</span><a href="https://…" target="_blank" rel="noopener">Second source</a></div>
```

## Anti-patterns

- Uppercase headlines, drop shadows and glows beyond the two noted under Principles, gradient text, untreated rectangular images, a second accent color.
- Click-to-advance handlers, fractional scale transforms that blur text, oversized decorative quote marks outside pattern 9.
- Paraphrase inside quotation marks, invented attribution, links to local files.
