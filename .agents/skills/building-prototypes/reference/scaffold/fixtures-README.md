# Fixtures

Sample data the prototype loads. Rules:

- **Invented or anonymised.** Never real customer, employee, or financial records. Shapes may match the real system; values do not.
- **Labelled in the UI.** Every screen that shows fixture data says "sample data" where the viewer can see it (the banner does this by default).
- **One file per source.** `data.json` is the default. Add `events.json`, `users.json`, and so on as the prototype grows, and list them here.
- **Edge cases included.** Keep at least one empty, one long, and one error-state record so the UI shows those states.

| File | What it holds | Edge cases covered |
|---|---|---|
| `data.json` | Summary metrics and a row list | one `attention` status |
