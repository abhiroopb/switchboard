# Review loop

Run after every version and before every share. The loop has three parts: static checks, the browser loop, and sub-agent reviews. Results land in `reviews/acceptance-matrix.md` with a capture per row.

## 1. Static checks

```
python3 scripts/prototype.py check
```

Fails on: missing `data-prototype-banner`, missing `id="feedback"` section, wrong fixture path, external `<script src>` or stylesheet, missing viewport meta. Warns on placeholder text (lorem, TODO, FIXME) and on a version with no changelog row. Fix every failure before the browser loop.

Visual-quality warnings come from the same command (and `capture` prints them before it shoots): no `--text-*` type scale tokens, no `--space-*` spacing tokens, more than 8 distinct literal px font sizes in rules, more than 12 distinct literal px values in padding, margin, or gap, no `:hover` rule, no `:focus-visible` rule. The scaffold `init` writes passes clean because it inlines `reference/scaffold/tokens.css`; a page that warns has drifted off the baseline and goes back to Build before the browser loop. Rebrand by changing the `:root` values, never by replacing token references with literals.

## 2. Browser loop

`prototype.py capture` is the default loop: it serves the prototype on an ephemeral port, runs a local headless Chromium, and writes one PNG per target and size. It needs no browser automation server and never touches the author's daily browser profile (it uses a throwaway profile under the prototype folder and deletes it). A browser tool that drives a page directly covers the keyboard, motion, console, and network steps; when none is available, do those by hand in a normal browser and say so in the matrix.

1. `python3 scripts/prototype.py capture vN` writes `qa/vN-1440.png` and `qa/vN-390.png`, plus one pair per named state. Add `--sizes` for other widths and `--scale 2` for a retina-density shot. Set `PROTOTYPE_BROWSER=/path/to/chrome` when the binary is not in a standard location.
2. Narrow widths (under 500) render inside an iframe of the requested size on a 500 px window and are cropped, because headless Chromium refuses a narrower window. Media queries and wrapping are therefore evaluated at the real width.
3. Keyboard: press Tab from the top of the page and list the focus order. Compare it with the keyboard acceptance criterion.
4. Reduced motion: emulate `prefers-reduced-motion: reduce` and confirm nothing animates.
5. Console: zero errors. Network: no request leaves `localhost`.
6. Each state in the plan (empty, error, partial, each persona, each step of a flow) gets its own capture. Make the state reachable from the URL (`?persona=<id>&step=<name>`), list it under `states` in `prototype.json`, and `capture` shoots it as `qa/<target>-<state>-<width>.png`. For a one-off, pass `--state name=?query` on the command line. Never edit a constant to switch fixtures between shots; the file you ship must be the file you captured.
7. Inspect each capture against the criterion it serves. Capturing without inspecting verifies nothing.
8. Judge each capture as a whole, at both widths, before the per-criterion pass: one type scale, even spacing, one accent, buttons and inputs that look like the same family, hover and focus states visible when the state is captured, nothing clipped or crowded, the primary action in view without scrolling. Record a visual finding as a row in the matrix like any other defect.

Record the result per criterion: pass, fail, or open, the step that verified it, and the capture path.

## 3. Sub-agent reviews

Use the reviewer personas in `sub-agents/`. Give each reviewer the specific files, not the whole folder, and ask for findings with a severity and the file or screen they apply to.

| When | Reviewer | Give them | Ask |
|---|---|---|---|
| After discover | skeptic, customer-voice | `DISCOVERY.md` | Is this the right question and the right user? What would make the prototype pointless? |
| After research | product-designer, competitive-analyst | `RESEARCH.md`, comparables | Is the chosen approach the strongest on the goals? What did the comparables teach that we ignored? |
| After plan | engineer-reviewer | plan rows in `CHANGELOG.md`, `DISCOVERY.md` Constraints | Does this fit the time box? What breaks at 390? |
| After each build | designer-reviewer, accessibility-auditor, design-system-reviewer, content-ux-writer | `versions/vN/index.html`, both captures | Hierarchy, density, focus, contrast, token use, copy. |
| Before share | test-strategist, executive-reviewer | acceptance matrix, `DEMO.md` | What is untested that the demo relies on? Does the demo land in five minutes? |
| Before decide | skeptic, customer-voice | `DECISION.md` draft, feedback | Does the evidence support the decision? |

Synthesise the findings into one list, mark conflicts between reviewers, and record the resolution as a changelog row. A finding that changes an acceptance criterion changes `DISCOVERY.md` first, then the matrix.

## 4. Acceptance matrix

`python3 scripts/prototype.py criteria` regenerates `reviews/acceptance-matrix.md` from the checkboxes in `DISCOVERY.md`. Existing rows keep their `How verified`, `Result`, and `Capture` cells; new criteria are added as `open`. The share step needs every row at `pass`.

## 5. Exit

Every acceptance criterion is pass with a capture, `check` is clean, the console and network are clean, and the reviewers' findings are logged with a resolution. Then `version --note "<what vN+1 will change>"` to freeze the reviewed `vN` (the note is the `vN+1` changelog row, the plan for the next change; the copy `vN+1` is where that change goes), or move to share.
