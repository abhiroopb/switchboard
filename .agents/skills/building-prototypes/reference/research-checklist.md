# Research checklist

Fill `RESEARCH.md` before the first build. The goal is to choose an approach you can defend, not to write a survey. Budget: half a day.

## 1. Pattern and best practices

Name the UI pattern the prototype relies on (dashboard, list and detail, wizard, inbox, timeline, editor, settings). Then answer these for the pattern:

- **Hierarchy.** What the user must see first, second, third. Dashboards: the exception before the summary before the detail. Lists: the item identity, then status, then actions.
- **Density.** How much fits above the fold at 1440 and at 390 before the user scrolls. Decide the density before the layout.
- **States.** Empty, loading, error, partial data, and success. Every state the fixtures can produce gets a screen or a row in the plan.
- **Navigation.** How the user gets back. Breadcrumb, back link, or persistent nav. A prototype with a dead end fails its demo.
- **Feedback.** How the UI confirms an action within 100 ms. Disabled states, inline validation, toasts only for background results.
- **Motion.** Use motion to show where something came from or went. Honour `prefers-reduced-motion`; the starter includes the rule.
- **Text.** Sentence case, labels that name the object, buttons that name the verb. No placeholder copy in a shared version.

## 2. Accessibility baseline

- WCAG 2.2 AA contrast (4.5:1 body, 3:1 large text and UI parts).
- Every interactive element reachable by Tab in reading order with a visible focus ring.
- Headings in order, one `h1`, landmarks (`header`, `main`, `nav`, `footer`).
- Status not conveyed by colour alone: pair colour with text or an icon with a label.
- Touch targets 44 by 44 CSS pixels at 390.
- `aria-live` for content that updates without a page change.

## 3. Comparables

Pick two or three products or patterns that solve the same problem. For each, record:

| Comparable | What to borrow | What to avoid | Evidence |
|---|---|---|---|
| Product or pattern name | One or two moves worth copying | One trap | Screenshot path or URL |

Internal products count. Design-system documentation counts. A pattern library entry counts. Use the competitive-analyst reviewer for the market view and the product-designer reviewer for the interaction view.

## 4. Design system

Name the design system or component library the prototype follows and link its documentation. List the tokens you will use (type scale, spacing, colour roles, radius) in `RESEARCH.md` so every version uses the same values. If the organisation has a design system, use it; if not, pick one public system and say which.

## 5. Options matrix

Write two or three approaches, then score them against the goals from `DISCOVERY.md`.

| Option | Goal 1 | Goal 2 | Goal 3 | Effort | Risk |
|---|---|---|---|---|---|
| A: ... | strong / ok / weak | | | days | one line |
| B: ... | | | | | |

Choose one and write a paragraph: what it is, why it beats the others on the goals that matter most, and which option becomes the first variant if the choice is wrong.

## 6. Tech choice

Default is plain HTML, CSS, and JavaScript in one file per version, because it runs anywhere, diffs cleanly, and needs no build step. Choose a framework only when the pattern needs state management the plain version cannot carry in two days (complex editors, real-time collaboration, large virtualised lists). If you choose one, the version folder must still open from `prototype.py serve` after a documented build command, and `export` must still produce a self-contained `dist/`.

## Exit

`RESEARCH.md` names the pattern, the design system, two or three comparables with evidence, the options matrix, and the chosen approach. The product-designer and competitive-analyst reviewers have read it. Then move to plan.
