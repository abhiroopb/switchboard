# {{title}}

Prototype `{{slug}}`, scaffolded {{date}} with the `building-prototypes` skill.

| File or folder | Purpose |
|---|---|
| `prototype.json` | Manifest: stage, gate, versions, variants, adapter, feedback links. Edited by `prototype.py`. |
| `DISCOVERY.md` | What to build, for whom, goals, non-goals, hypotheses, acceptance criteria. Gate before code. |
| `RESEARCH.md` | Patterns, design system, options, chosen approach. |
| `CHANGELOG.md` | One row per version, variant, or visible change. |
| `DEMO.md` | Demo script tied to the hypotheses. |
| `DECISION.md` | Archive, promote, or iterate, with what was learned. |
| `fixtures/` | Sample data the UI loads. Always labelled as sample data in the UI. |
| `versions/vN/` | One self-contained `index.html` per version. Never edit an older version. |
| `variants/<name>/` | Forks of a version that test one hypothesis each. |
| `reviews/` | Sub-agent review notes and `acceptance-matrix.md`. |
| `qa/` | Screenshots and recordings from the browser check loop. |
| `compare.html` | Generated side-by-side view of versions and variants. |
| `dist/` | Export for a static host (gitignored). |

Commands: `prototype.py status`, `gate`, `criteria`, `version --note`, `fork <name>`, `compare`, `check`, `serve`, `export --adapter static`.
