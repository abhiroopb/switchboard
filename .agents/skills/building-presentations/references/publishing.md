# Preview, Export & Publishing

## Local preview (always do this before delivering)

```bash
python3 -m http.server 8000 --directory <deck-dir>
# → http://localhost:8000   (presenter view: press `s`)
```

`file://` does not work — the markdown plugin fetches `slides.md` over HTTP.

## Validation

```bash
bash <skill-dir>/scripts/validate-presentation.sh <deck-dir>
```

When the Chrome DevTools MCP is available, additionally: navigate to the served URL, check `list_console_messages` for errors, and screenshot the first slide to confirm rendering.

## PDF export

1. Serve the deck, open `http://localhost:8000/?print-pdf` in Chrome.
2. Print → Save as PDF → Landscape, margins None, "Background graphics" checked.

Headless (no UI):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --print-to-pdf=deck.pdf 'http://localhost:8000/?print-pdf'
```

Alternative when Node is available: `npx decktape reveal http://localhost:8000 deck.pdf`.

Help standard in the export: the PDF must show the closing Help page (DM the deck owner, the team channel, the team resource hub) and the footer brand row with the same three links on every page. The editorial shell's print rule hides only the counter and dashes; do not restore a `#chrome { display: none }` print rule. Open the PDF and confirm the last counted page and one body page before sharing.

### Animated/interactive slides in exports

The editorial shell is built so exports stay correct without extra work — keep these invariants when adding custom behavior:

- **Markup holds the final state.** Count-up numbers, grown bars, and layered stacks keep their finished values/layout in the source HTML; JS and fragments only animate toward it. A no-JS or PDF render is automatically the finished slide.
- **Entry animations skip under automation.** JS triggered on slide entry (e.g. the `stats-live` count-up) is gated on `navigator.webdriver`, so decktape and headless Chrome capture final values, never mid-animation frames.
- **`pdfSeparateFragments: false`** exports one page per slide in its final fragment state. If you flip it to export per-fragment pages, every fragment step must read as a standalone page.
- `fade-in-then-semi-out` lists keep the last item fully lit when no fragment is current (the theme handles this), so the PDF's final-state capture matches the live end state.
- After export, spot-check the pages for any slide with fragments, auto-animate, or live numbers.

## Publishing (internal sharing)

- **Hosted static site (default for shareable live decks):** upload the deck directory with your hosted site or publishing tool. Name a new site after the deck slug and serve it behind your organization's sign-in when it has one. Preserve existing destinations when updating. Do not rename existing sites or add redirects. Re-upload to publish a new version, and use the tool's version history to roll back.
- **PDF attachment:** export per above and share the file (Slack/Drive) when the audience just needs to read it.
- **Zip:** `zip -r deck.zip <deck-dir>` for a self-contained handoff (recipient serves it locally).
- Decks are internal by default. Never publish a deck containing customer data or internal metrics to any public host.
- For decks carrying internal working dates or sensitive metrics, uncomment the `internal-tag` span in `index.html` so every slide carries a subtle `Internal` pill in the footer (see design system → Chrome), and keep uncommitted dates in working-target language.

## Google Slides export (explicit request only)

Only when {{OWNER_FIRST}} explicitly asks for a Google Slides artifact. The Reveal.js deck stays the source of truth; the Slides file is an export.

1. Finish and validate the Reveal deck first.
2. Simplest faithful export: PDF (above) → import into Slides, or share the PDF directly if that satisfies the ask.
3. For a native editable Slides file: rebuild the outline through a Slides-capable connector or the Google Slides API (one slide per horizontal Reveal slide; speaker notes → Slides speaker notes). Label it as an export in the deck's README or commit message.
4. Never continue editing content in Slides — route changes back through `slides.md` and re-export.
