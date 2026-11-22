# Prototype lifecycle

Eight stages, each with an entry condition, an exit condition, the documents it touches, and the reviewer to invoke. `prototype.py status` shows the current stage from `prototype.json`.

| Stage | Enter when | Exit when | Documents | Reviewers |
|---|---|---|---|---|
| discover | Someone asks for a prototype. | `prototype.py gate --approve` records a pass. | `DISCOVERY.md` | skeptic (is the question worth a prototype), customer-voice (is the user real) |
| research | Gate passed. | `RESEARCH.md` names the chosen approach and the design system. | `RESEARCH.md` | product-designer, competitive-analyst |
| plan | Approach chosen. | Screens and states are listed against acceptance criteria. | `CHANGELOG.md` (plan row) or first version note | engineer-reviewer (is the scope buildable in the time box) |
| build | Plan written. | `prototype.py check` passes and the version opens from `serve`. | `versions/vN/index.html`, `fixtures/` | none yet; build first |
| review | A version exists. | Every acceptance criterion in `reviews/acceptance-matrix.md` is pass with a capture. | `reviews/`, `qa/` | designer-reviewer, accessibility-auditor, design-system-reviewer, content-ux-writer |
| iterate | Review found gaps, or a new hypothesis. | New version or variant passes review. | `CHANGELOG.md`, `versions/`, `variants/`, `compare.html` | test-strategist (what else could break) |
| share | A reviewed version exists. | Feedback is collected and summarised. | `DEMO.md`, `dist/` | executive-reviewer (does the demo land the point in five minutes) |
| decide | Feedback is in. | `DECISION.md` names promote, iterate, or archive with evidence and `prototype.py decide <outcome>` records it. | `DECISION.md` | skeptic, customer-voice |

## What each stage produces

**Discover.** One page that a stakeholder can read in two minutes and say "yes, build that" or "no". The acceptance criteria are the contract for every later review. See `discovery-gate.md`.

**Research.** The approach you will build and why it beats the alternatives. Best practices for the pattern, two or three comparables with what to borrow and what to avoid, an options matrix, and a one-paragraph choice. See `research-checklist.md`.

**Plan.** A list of screens and states. Each line names the acceptance criterion it serves. A screen that serves no criterion is cut or the criterion is added to `DISCOVERY.md`.

**Build.** One self-contained `index.html` per version. Fixtures load from `fixtures/` (`data.json` by default, more files when personas or scenarios need them). The banner, feedback section, and session log from the starter stay in place. Every screen and persona is reachable from a URL query (`?persona=<id>&step=<name>`) and listed under `states` in `prototype.json`, so the review loop can capture it without clicking through.

**Review.** Browser loop at 1440 and 390, keyboard path, reduced motion, console, network. Sub-agent reviews with the specific files. Results go in the acceptance matrix with a capture per row. See `review-loop.md`.

**Iterate.** A new version carries forward fixes. A variant tests one alternative hypothesis against the same criteria. `compare.html` puts them side by side so a reviewer can switch without reloading.

**Share.** A demo script with timings and the question each beat answers (`DEMO.md`), an export (`dist/`), and the feedback links in the page. Feedback is pasted into `DECISION.md` as it arrives.

**Decide.** Promote: hand the folder to a product team with discovery, research, and the acceptance matrix as the spec. Iterate: write the next hypothesis and return to research or build. Archive: write what was learned and what would reopen it. Every outcome is a decision; "we stopped" is not.

## Time boxes

A default prototype is five working days: half a day discover, half a day research, two days build, one day review and iterate, one day share and decide. Write the time box in `DISCOVERY.md` Constraints. When the box runs out, go to decide with what exists.

**Interview or workshop box (45 minutes).** The same eight stages compressed; the CLI commands are the same, only the depth changes.

| Minutes | Stage | What exists at the end |
|---|---|---|
| 0 to 5 | Discover | `init --brief <prompt>`, `DISCOVERY.md` filled in one pass (problem, segments, hypothesis, primary and guardrail metric, 6 to 10 criteria), `gate --approve`. |
| 5 to 10 | Research | `RESEARCH.md`: three patterns, the options table, the system notes (inputs, latency, wrong-answer risk, edge cases), chosen approach. |
| 10 to 30 | Build | Fixtures per persona, `versions/v1/index.html`, `states` in `prototype.json`, `check` clean. |
| 30 to 35 | Review | `capture --scale 2`, inspect every PNG, fill the matrix, `version --note` to freeze. |
| 35 to 40 | Curveball | `fork <name> --hypothesis "<new constraint>"`, edit the variant only, `capture`, `compare --state <name>`. |
| 40 to 45 | Share and decide | `export --adapter local`, `DEMO.md` beats, `DECISION.md` with the framing and the compare verdict, `decide <outcome>`, `status`. |

When the box runs short, cut personas before cutting states, and cut states before cutting the metric pair.
