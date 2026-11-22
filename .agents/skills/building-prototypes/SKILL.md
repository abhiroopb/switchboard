---
name: building-prototypes
description: "Runs the full prototype lifecycle for product prototypes, clickable mockups, local web dashboards, and app-like experiences: discovery gate, UI and UX research, build, sub-agent review loop, browser checks, versions and variants, share and feedback, archive or promote. Local-first with no internal hosting dependency; deployment is a pluggable adapter. Use whenever asked to prototype, mock up, build a demo app, spike a UI, or iterate on one."
---

# Building Prototypes

A prototype exists to answer a question, not to ship. This skill makes the question explicit before any code, keeps every build runnable on the author's machine, and ends with a recorded decision. Hosting is an adapter chosen at the end, never a constraint at the start.

Content-first sites (reports, briefs, galleries) are a different job: use the owner's site-building skill for those. Use this skill when the primary job of the artifact is to let someone complete or evaluate a product task.

## Lifecycle

Every prototype moves through eight stages. `scripts/prototype.py` records the stage in `prototype.json`.

1. **Discover.** Fill `DISCOVERY.md`: problem, who it is for, goals, non-goals, what we want to learn, success metrics (one primary, one guardrail), acceptance criteria, data and fixtures, constraints, success in the share step. When the work starts from a written brief or exercise prompt, pass it to `init --brief <file>` so it lands in `BRIEF.md` next to the discovery doc. `prototype.py gate` fails while any placeholder remains. No code before `gate --approve`.
2. **Research.** Fill `RESEARCH.md` with `reference/research-checklist.md`: the design system to follow, UI and UX best practices for this pattern, two or three comparable products or patterns, an options matrix, the system notes (inputs the UI needs, latency budget, wrong-answer risk, edge cases) when the prototype pretends to use live data or a model, and the chosen approach with its reason.
3. **Plan.** Write the build plan at the top of `CHANGELOG.md` or in the first version note: screens, states, fixture shape, and which acceptance criterion each screen serves.
4. **Build.** Edit `versions/vN/index.html` (self-contained HTML, CSS, and JavaScript; fixtures from `fixtures/*.json`, `fixtures/data.json` by default). Keep the prototype banner, the feedback section, and the session log from the starter. Make every screen and persona reachable from the URL (`?persona=<id>&step=<name>`) and list those URLs as `states` in `prototype.json`, so `capture` and `compare` can open them. `prototype.py check` catches missing parts.
   **Visual quality.** Build on the design baseline `init` inlined from `reference/scaffold/tokens.css` (a Tailwind-style type and spacing scale, slate neutrals with one accent, shadcn-like button, input, card, badge, table, dialog and stepper patterns, hover and focus-visible states). Size and space every element with the `--text-*` and `--space-*` tokens, give every interactive element a hover and a focus state, and keep one accent colour. Swap the `:root` values for the product's own tokens when the fiction has them (Example prototypes use Market's); keep the token names so the components and the check still apply. `check` and `capture` warn when a page has no type or spacing tokens, more than 8 literal px font sizes, more than 12 literal px spacing values, or no `:hover` or `:focus-visible` rule; treat a visual warning as a defect to fix before the review loop.
5. **Review loop.** Run the browser check loop and the sub-agent reviews in `reference/review-loop.md`. `prototype.py capture` shoots the default page and every named state at 1440 and 390. Fill `reviews/acceptance-matrix.md` (from `prototype.py criteria`) with how each criterion was verified and a capture in `qa/`. Repeat until every criterion passes.
6. **Iterate.** `prototype.py version --note "<what vN+1 will change>"` freezes the reviewed `vN` as it is, copies it to `vN+1`, and logs the note as the `vN+1` row of `CHANGELOG.md`, so the note is the plan for the next version, never a description of the one you just reviewed (that lives in the acceptance matrix). Edit `vN+1` next; `capture` and `check` then run on it. `prototype.py fork <name> --hypothesis "<claim>"` makes a variant to test an alternative (a late constraint, such as a legal or contract rule, is a fork, never an edit to the reviewed version). `prototype.py compare [--state <name>] [--out compare-<name>.html]` writes `compare.html` (or the named file, so one page per state can coexist) with versions and variants side by side, all opened at the same state.
7. **Share and feedback.** Write `DEMO.md` (the demo script), export with the chosen adapter (`reference/deploy-adapters.md`), collect feedback through the feedback section links, and paste the session log findings into `DECISION.md`.
8. **Decide.** Fill `DECISION.md`: promote (hand off to a product team with the discovery doc, research, and acceptance matrix), iterate (new version with a new hypothesis), or archive (record what was learned), set the `- Decision:` header line to that one word, then run `prototype.py decide <outcome>`. It refuses until the header and `## What we learned` are filled, then records the outcome and the stage in `prototype.json`.

`reference/lifecycle.md` has the entry and exit criteria for each stage and the sub-agent to invoke.

## Commands

All commands take `--dir <prototype dir>` (default: current directory). Python 3.9+, standard library only.

| Command | What it does |
|---|---|
| `prototype.py init <slug> --title "<Title>" [--owner "<name>"] [--feedback-link "Label=url" ...] [--brief <file>]` | Scaffolds the prototype folder with every document, `fixtures/`, `versions/v1/index.html`, and `prototype.json`. `--brief` copies the requester's brief or exercise prompt into `BRIEF.md` and links it from the manifest. |
| `prototype.py gate [--approve --by "<name>"]` | Checks `DISCOVERY.md` for placeholders and acceptance criteria. `--approve` records the pass; `version` and `fork` refuse until then. |
| `prototype.py criteria` | Regenerates `reviews/acceptance-matrix.md` from the acceptance criteria. Existing rows keep their verification notes. |
| `prototype.py check [targets]` | Static checks on every version and variant: banner, feedback section, fixture path, viewport meta, placeholder text, external script tags. Visual-quality warnings: no `--text-*` or `--space-*` tokens, too many literal px font sizes or spacing values, no `:hover` or `:focus-visible` rule. |
| `prototype.py serve [--port 8787]` | Serves the prototype folder locally so fixtures load over HTTP. A busy port is reported with the fix (`--port <N>`, or `--port 0` to take any free port; the real URL is printed). The server reads from the `--dir` path, not the shell's working directory, handles each connection on its own thread, and drops a client that sends nothing for 15 s. If the served directory is deleted while the server runs (a scratch checkout removed), every request answers 503 with that path; restart `serve` from a checkout that outlives the session. |
| `prototype.py capture [targets] [--sizes 1440x900,390x844] [--scale 2] [--state NAME=QUERY | NAME ...] [--default-only]` | Screenshots every version and variant at each size into `qa/<target>[-<state>]-<width>.png` with a local headless Chromium (`PROTOTYPE_BROWSER` overrides the binary). States come from `prototype.json` `states` (`[{"name": "offer", "query": "?persona=maya&step=offer"}]`) or from `--state`; a bare `--state NAME` reshoots one manifest state without retyping its query; the default page is always shot first. Widths under 500 render in an iframe harness and are cropped, because headless Chromium opens no narrower window. |
| `prototype.py version --note "<what vN+1 will change>"` | Freezes the reviewed `vN`, copies it to `vN+1`, bumps the banner, logs the note as the `vN+1` changelog row. Edit `vN+1` next. |
| `prototype.py fork <name> [--from vN] --hypothesis "<claim>"` | Creates `variants/<name>/` from a version to test one alternative. |
| `prototype.py compare [targets] [--state <name or query>] [--out <name>.html]` | Writes `compare.html` (or `--out`, a file inside the prototype folder) with iframes of the chosen versions and variants, every frame opened at the same state. |
| `prototype.py export --adapter local|static [--target vN|variants/<name>] [--out dir]` | Writes a self-contained `dist/` folder with rewritten fixture paths and `PROTOTYPE.json`. |
| `prototype.py decide promote|iterate|archive [--by "<name>"]` | Checks `DECISION.md` (header line matches the outcome, `## What we learned` filled, no placeholders), then sets `stage: decide` and records the outcome in `prototype.json`. |
| `prototype.py status` | Prints stage (with the recorded decision), gate, versions, variants, capture states, acceptance-matrix counts, and open discovery gaps. |

## Folder layout

```
<slug>/
  prototype.json          manifest: stage, gate, versions, variants, adapter, feedback links
  DISCOVERY.md            the gate document
  RESEARCH.md             best practices, comparables, options matrix, chosen approach
  CHANGELOG.md            one row per version, variant, or visible change
  DEMO.md                 demo script with timings and the question each beat answers
  DECISION.md             promote, iterate, or archive, with evidence
  README.md               how to run and where things are
  BRIEF.md                the requester's brief or exercise prompt (from `init --brief`)
  fixtures/data.json      labelled sample data, never real customer data (split into more files when personas need them)
  versions/v1/index.html  self-contained build; vN for each iteration
  variants/<name>/        forks that test one hypothesis each
  reviews/acceptance-matrix.md
  qa/                     screenshots and check output per review pass
  compare.html            generated side-by-side view
  dist/                   export output (gitignored)
```

## Rules that hold at every stage

- **Gate first.** No `versions/v2`, no variant, and no share step before `gate --approve`. A prototype built before discovery answers a question nobody asked.
- **Local first.** Every version opens from `prototype.py serve` with no network, no account, and no internal service. External CDNs, fonts, and analytics are flagged by `check`; inline what you need.
- **Fixtures, never real data.** Everything under `fixtures/` is invented or anonymised, and the banner says so. Follow `reference/prototype-contract.md` for the label.
- **Banner and feedback stay.** The prototype banner (`data-prototype-banner`) tells viewers what they are looking at and which version. The feedback section (`id="feedback"`) gives them a way to answer. Both are checked.
- **Baseline, then brand.** Every version starts from the design baseline in `reference/scaffold/tokens.css` (type scale, spacing tokens, one accent, component states). A prototype that drops the tokens for ad-hoc px values reads as unfinished to the people it is shown to; `check` warns, and the review loop treats the warning as a defect.
- **One hypothesis per variant.** A variant tests one claim written in `--hypothesis`. Two claims means two variants.
- **Instrument what you want to learn.** The starter's session log records clicks and timing to `localStorage` and offers a JSON download. Map each "What we want to learn" item to an event you can read back.
- **Record the decision.** A prototype without a filled `DECISION.md` is unfinished, even if the demo went well.

## Reviews

Invoke the reviewer personas in `sub-agents/` at the stages `reference/review-loop.md` names: product-designer and competitive-analyst after research, designer-reviewer and accessibility-auditor after each build, test-strategist before the share step, skeptic and customer-voice before the decision. Give each the specific documents, synthesise the feedback, and flag conflicts between reviewers in the changelog.

## Browser check loop

Run after every version and before every share. Details and the exact sequence are in `reference/review-loop.md`.

1. `prototype.py capture vN` writes `qa/vN-1440.png` and `qa/vN-390.png` with a local headless Chromium. No browser automation server is needed. It prints any visual-quality warning for the target first; fix those before reading the screenshots.
2. Inspect each screenshot against the acceptance criterion it serves and record the result in the acceptance matrix. Also judge the page as a whole at both widths: consistent type sizes and spacing, one accent, visible hover and focus states, nothing clipped or crowded. A page that passes every criterion but looks unfinished goes back to Build.
3. When a browser tool is available, also `prototype.py serve`, open `http://localhost:8787/versions/vN/`, tab through the page and confirm the focus order, confirm reduced motion stops every animation, and read the console and network panel. Without one, record those rows as checked by hand in a normal browser.

## Deployment adapters

The core writes `dist/`. Where `dist/` goes is the adapter's job: `local` (open from disk or `serve`), `static` (any static host or object store). Organisation-specific hosts are added as documented in `reference/deploy-adapters.md`; the core never names one.

## Reference files

- `reference/lifecycle.md`: stage entry and exit criteria, documents, reviewers.
- `reference/discovery-gate.md`: how to fill `DISCOVERY.md` and what the gate checks.
- `reference/research-checklist.md`: UI, UX, and accessibility best practices, comparable research, options matrix.
- `reference/review-loop.md`: sub-agent schedule, browser loop, acceptance matrix.
- `reference/prototype-contract.md`: what every version must contain before it is shared.
- `reference/scaffold/tokens.css`: the design baseline `init` inlines into `versions/v1/index.html` (`{{tokens}}` in the scaffold template); edit the `:root` values to rebrand, keep the names.
- `reference/deploy-adapters.md`: local and static adapters, how to add one.
- `reference/scaffold/`: the files `init` renders.
- `tests/test_prototype.py`: run with `python3 -m unittest discover -s tests` from this folder.
