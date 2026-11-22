# Prototype contract

What every shared version contains. `prototype.py check` enforces the parts it can read; the rest is the pre-share checklist at the end.

## Banner

A banner at the top of the page with `data-prototype-banner` and `data-prototype-version="vN"`. It says three things: this is a prototype, the data is sample data, and which version the viewer is looking at. `role="note"` and an `aria-label`. The starter includes it; `version` updates the version attribute.

## Feedback section

A section with `id="feedback"` reachable from a link in the banner. It holds two to four links the owner configured at `init` (`--feedback-link "Label=url"`), for example a chat channel, an issue tracker, an email address, or a form. The last item is a "Download session log" button that saves the instrumentation JSON. Viewers always have a way to answer the question the prototype asks.

## Sample data

`fixtures/data.json` is invented or anonymised. The banner says "Sample data". A real name, email, account id, or amount from a production system never appears, in fixtures or in screenshots. `fixtures/README.md` records where each field came from.

## Self-contained

One `index.html` per version with inline CSS and JavaScript. No external scripts, stylesheets, fonts, or analytics. Icons are inline SVG. A version opens from `prototype.py serve` with no network. `export` copies `fixtures/` next to it so `dist/` is complete.

## Responsive and accessible

Works at 1440 and 390 wide with no horizontal scroll. Keyboard reachable in reading order with visible focus. WCAG 2.2 AA contrast. Status is never colour alone. `prefers-reduced-motion` is honoured. One `h1`, landmarks present.

## Instrumentation

The starter logs page views, clicks on elements with `data-track`, and time to first click to `localStorage` under `proto:<slug>:<version>`. Add `data-track="<event name>"` to any element whose use answers a "What we want to learn" item. The Download button in the feedback section saves the log as JSON for the decision step.

## Budget

A prototype screen shows the task, not a report about the task. At most one headline per screen, three to five summary items, and the primary action within reach without scrolling at 1440. Caveats, methodology, and data notes go in a single closed `<details>` at the end titled "Notes on this prototype".

## Pre-share checklist

- `check` passes for the version you are sharing.
- Both captures (1440, 390) are in `qa/` and were inspected against the criteria.
- Every acceptance criterion in the matrix is pass.
- `CHANGELOG.md` has a row for this version.
- `DEMO.md` is written with timings.
- The banner version matches the folder.
- The feedback links open and go to a place someone reads.
- No placeholder copy, no lorem, no TODO.
- Nothing in the page requests a host other than localhost (or the static host after export).
