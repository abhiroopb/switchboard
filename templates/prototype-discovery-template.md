<!-- Template for a prototype discovery doc. `prototype.py init` renders the same file from .agents/skills/building-prototypes/reference/scaffold/DISCOVERY.md with {{title}}, {{slug}}, {{owner}}, {{date}} filled. -->
# {{title}}: discovery

- Slug: `{{slug}}`
- Owner: {{owner}}
- Date: {{date}}
- Gate: pending (run `prototype.py gate --approve` once the owner agrees to this doc)

<!-- Fill every section. Replace each <placeholder>. The gate fails while any placeholder or empty section remains. -->

## Problem

<What the user cannot do today, in one or two sentences. Name the evidence: a quote, a ticket, a metric.>

## Who it is for

<Primary user and the situation they are in. One secondary audience at most.>

## Goals

<Two to four outcomes the prototype should make believable. Each one is observable in a demo or a test session.>

## Non-goals

<What this prototype does not try to show. Name the tempting adjacent features you are leaving out.>

## What we want to learn

<Hypotheses and the signal that confirms or refutes each one. Example: "Managers find the alert row within 10 seconds without a hint.">

## Acceptance criteria

<!-- One line per criterion, as a checkbox. `prototype.py criteria` turns these into reviews/acceptance-matrix.md. -->
- [ ] <Observable behavior at 1440 wide>
- [ ] <Observable behavior at 390 wide>
- [ ] <Keyboard path works end to end>

## Data and fixtures

<Where the sample data comes from, what is real and what is invented, and the label the UI shows for it.>

## Constraints

<Tech, brand, accessibility, privacy, and time constraints. Say which design system or component library to follow.>

## Success in the share step

<Who sees it, how (demo, async link, test session), and what feedback counts.>
