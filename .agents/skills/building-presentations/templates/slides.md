<!-- Editorial starter deck. Every slide below is a working pattern from
     references/design-system.md — replace content, keep the structure.
     Patterns needing photography (full-bleed, stepper, journey, tiles,
     caption cards) are in the design system; add photos to assets/photos/
     and follow the photo-treatment rules before using them.
     HTML blocks: keep flush-left, no blank lines inside a block. -->

<!-- .slide: data-section="Open" data-state="hide-chrome" data-background-gradient="radial-gradient(ellipse 90% 70% at 30% 22%, #14171c 0%, #0c0d10 62%)" -->

<div class="kicker">{{TITLE}}</div>

# Headline claim &<br><span class="dim">the payoff.</span>

<p class="byline">{{OWNER_NAME}} &nbsp;·&nbsp; {{DATE}} &nbsp;·&nbsp; Internal</p>

Note:
Pattern 1 (full-bleed statement), gradient variant. With a strong photo, switch to data-background-image + data-state="scrim-left" and put the text on the darkest third. Press `s` for presenter view.

---

<!-- .slide: data-section="Agenda" -->

<div class="kicker">Agenda</div>

## What we're deciding today.

1. First section <!-- .element: class="fragment fade-in-then-semi-out" -->
2. Second section <!-- .element: class="fragment fade-in-then-semi-out" -->
3. Decisions needed <!-- .element: class="fragment fade-in-then-semi-out" -->

Note:
fade-in-then-semi-out keeps the current item lit while prior items dim — the audience always sees position. Forward is always → or a click; never require ↓.

---

<!-- .slide: data-section="Evidence" class="stats-live" -->

<div class="kicker">Evidence · scale</div>

## The numbers that must land.

<div class="stat-row">
<div class="stat-card hero"><div class="stat-num">1,234,567</div><div class="stat-label">headline metric — the one that matters</div></div>
<div class="stat-card"><div class="stat-num">42%</div><div class="stat-label">supporting rate</div></div>
<div class="stat-card"><div class="stat-num">~90</div><div class="stat-label">days to target</div></div>
</div>

<!-- Replace these example sources with the real doc/dashboard links for your deck (or delete the block on slides with nothing to cite). -->
<div class="slide-sources"><span class="src-label">Sources:</span><a href="https://revealjs.com/" target="_blank" rel="noopener">Example source — replace me</a><span class="src-sep">|</span><a href="https://revealjs.com/fragments/" target="_blank" rel="noopener">Second source</a></div>

Note:
Pattern 7 (data story). class="stats-live" animates each .stat-num on entry; markup holds the final values so PDF stays correct. The .slide-sources footer pins bottom-left — use it on any slide an exec could ask "says who?".

--

<!-- .slide: data-section="Evidence" -->

<div class="kicker">Evidence · proportion</div>

## Same numbers, honest scale.

<div class="mag-rows">
<div class="mag-row fragment bar-grow" style="--w: 100%"><div class="m-label">Total addressable</div><div class="m-track"><div class="m-bar"></div></div><div class="m-num">1,234,567</div></div>
<div class="mag-row fragment bar-grow" style="--w: 34%"><div class="m-label">Active segment</div><div class="m-track"><div class="m-bar"></div></div><div class="m-num">420,000</div></div>
<div class="mag-row fragment bar-grow" style="--w: 1.6%"><div class="m-label">Initial cohort</div><div class="m-track"><div class="m-bar"></div></div><div class="m-num">~20,000</div></div>
</div>

<small class="mag-note">Linear scale — the initial cohort is deliberately a sliver of the base.</small>

Note:
Pattern 19 (magnitude bars) as a vertical drill-down under the stat cards: cards state the numbers, bars make the ratios land. Never log-scale or normalize per-bar.

---

<!-- .slide: data-section="Findings" -->

<div class="kicker">Findings</div>

## What we learned.

<div class="insight-row fragment fade-up"><div class="i-num">01</div><div class="i-main"><div class="i-title">Claim stated as a finding.</div><div class="i-body">One italic sentence explaining why the claim holds.</div></div><div class="i-quote"><div class="q">"A short evidence quote that grounds the claim."</div><div class="q-attrib">— Source, date</div></div></div>
<div class="insight-row fragment fade-up"><div class="i-num">02</div><div class="i-main"><div class="i-title">Second finding.</div><div class="i-body">Claim on the left, evidence on the right, every row.</div></div><div class="i-quote"><div class="q">"Another grounding quote."</div><div class="q-attrib">— Source, date</div></div></div>

Note:
Pattern 14 (insight rows) — the strongest pattern for "what we learned" and decision-ask slides. 3–4 rows max.

---

<!-- .slide: data-section="Findings" -->

<div class="kicker">Findings · comparison</div>

## Before and after, side by side.

<div class="def-row">
<div class="def-card"><h3>Today</h3><div class="def-lede">What the current state does.</div><div class="def-why">Why it falls short.</div></div>
<div class="def-card"><h3>Next</h3><div class="def-lede">What the new state does.</div><div class="def-why">Why it matters.</div></div>
</div>
<div class="def-span fragment fade-up"><h3>For the customer</h3><div class="span-lede">The shared takeaway both boxes point to — <em>one sentence, customer value first.</em></div></div>
<figure class="evidence-quote fragment fade-up"><blockquote>“A verbatim quote that proves the takeaway.”</blockquote><figcaption>Name, Business, Source</figcaption></figure>

Note:
Pattern 12 (definition cards) as a left/right comparison: exactly 2 cards, a full-width "for the customer" band beneath, then the Pattern 15b evidence quote as the proof point. Quotes verbatim with real attribution — never paraphrase inside quotation marks.

---

<!-- .slide: data-section="Status" -->

<div class="kicker">Status</div>

## Where each track stands.

| Track | Owner | State | Next milestone |
|---|---|---|---|
| First workstream | Name | <span class="chip live">Live</span> | Date · milestone |
| Second workstream | Name | <span class="chip motion">In motion</span> | Date · milestone |
| Third workstream | Name | <span class="chip hold">On hold</span> | Blocked on decision 01 |

Note:
Pattern 8 (hairline table). Status via .chip (live | motion | hold | no | decided), never emoji. Never fragment a table. Add class="compact" to the slide for >5 rows.

---

<!-- .slide: data-section="Decisions" class="chapter" data-background-gradient="radial-gradient(ellipse 90% 70% at 30% 22%, #14171c 0%, #0c0d10 62%)" -->

<div class="ch-nums"><span>05</span><span class="ch-line"></span><span>06</span></div>

## The asks.

<div class="ch-quote"><div class="q">One line that frames why these decisions matter now.</div><div class="q-attrib">— Source of the framing</div></div>

Note:
Pattern 17 (chapter divider) — mostly empty space, on purpose. Use once, right before the payoff section.

---

<!-- .slide: data-section="Decisions" -->

<div class="kicker">Decisions needed</div>

## Three calls to make today.

<div class="decision-card fragment fade-up"><div class="d-num">01</div><div class="d-body"><div class="d-title">First decision, phrased as a question?</div><div class="d-rec">Recommendation: <b>the recommended call</b> — one line of rationale.</div></div></div>
<div class="decision-card fragment fade-up"><div class="d-num">02</div><div class="d-body"><div class="d-title">Second decision?</div><div class="d-rec">Recommendation: <b>the recommended call</b> — one line of rationale.</div></div></div>
<div class="decision-card fragment fade-up"><div class="d-num">03</div><div class="d-body"><div class="d-title">Third decision?</div><div class="d-rec">Recommendation: <b>the recommended call</b> — one line of rationale.</div></div></div>

Note:
Pattern 10 (decision cards) — usually the payoff slide. Keep to ≤3 decisions, each with a recommendation in bone.

---

<!-- .slide: data-section="Close" class="statement-close" data-state="hide-chrome" data-background-gradient="radial-gradient(ellipse 90% 70% at 32% 20%, #14171c 0%, #0c0d10 62%)" -->

<div class="kicker">{{TITLE}}</div>

<h1 class="r-fit-text">The thesis in five words.</h1>

<p class="byline">Decisions land today &nbsp;·&nbsp; execution tracked in the owning workstreams</p>

Note:
Pattern 18 (statement close) — r-fit-text scales the headline to fill the slide. The last thing the room sees is the thesis, not "Thank you".

---

<!-- .slide: data-section="Help" class="help-close" -->

<div class="kicker">Help</div>

## Questions, feedback, access.

<div class="help-links">
  <a class="help-link" href="https://example.slack.com/team/"><span class="h-label">DM {{OWNER_NAME}}</span><span class="h-sub">Slack profile</span></a>
  <a class="help-link" href="https://example.slack.com/archives/CXXXXXXXXXX"><span class="h-label">#team-channel</span><span class="h-sub">Slack channel</span></a>
  <a class="help-link" href="https://example.com/team-resources/"><span class="h-label">Team resource hub</span><span class="h-sub">example.com/team-resources</span></a>
</div>

Note:
Closing Help page. Every deck ends with exactly these three links, counted, after the statement close and before Backup. Replace the template URLs and labels with your own DM link, team channel, and resource hub. The same three links sit in the footer chrome, and `scripts/validate-presentation.sh` lists the URLs it checks.

---

<!-- .slide: data-section="Backup" class="chapter" data-visibility="uncounted" data-state="hide-chrome" data-background-gradient="radial-gradient(ellipse 90% 70% at 30% 22%, #14171c 0%, #0c0d10 62%)" -->

<div class="ch-nums"><span>A</span><span class="ch-line"></span><span>Q&amp;A</span></div>

## Backup.

<div class="ch-quote"><div class="q">Reference density for questions — these slides sit outside the count.</div><div class="q-attrib">↓ definitions · FAQ</div></div>

Note:
Pattern 21 (uncounted appendix). data-visibility="uncounted" keeps backup slides reachable one arrow past the close without inflating the official count.

--

<!-- .slide: data-section="Backup" data-visibility="uncounted" -->

<div class="kicker">Backup · definitions</div>

## Every number, defined.

| Number | Definition | Source |
|---|---|---|
| 1,234,567 | Exact cohort definition behind the headline metric | Doc, date |
| ~20,000 | Cap definition and what triggers pause/adjust | Doc, date |

Note:
Appendix slides compress facts already presented into Q&A density — tables, not narrative.
