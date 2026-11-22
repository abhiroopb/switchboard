# Deploy adapters

The core produces `dist/`: one `index.html`, a `fixtures/` folder with rewritten paths, and `PROTOTYPE.json` (slug, title, source version, export time, adapter). Where `dist/` goes is the adapter's job. Nothing in `scripts/prototype.py` names a host.

## Built-in adapters

**local** (default). `export --adapter local` writes `dist/` and prints the path. Open it with `prototype.py serve` or any static file server. Use for demos on the author's machine and for sharing a zip.

**static**. `export --adapter static` writes the same `dist/` and prints the upload instruction. Upload the folder to any static host: an object store bucket with website hosting, a static-site service, a shared drive that serves HTML, or an internal static host. The export is complete; no build step runs on the host.

## Adding an organisation adapter

Add a file next to this one named `adapter-<name>.md` that answers four questions, and leave the core alone:

1. **Where.** The host, the URL pattern, and who can see it.
2. **How.** The exact upload command or tool call that takes `dist/` and publishes it, and the command that verifies the live page.
3. **Extras.** Anything the host requires on top of the contract (a help footer, an auth banner, a naming prefix for the site). Put those additions in a copy of `dist/index.html`, never in `versions/`.
4. **Rules.** Any organisation checklist that applies before publishing (review gates, naming, retention). Link it; do not restate it.

The adapter file is documentation for the agent and the author. The agent runs `export --adapter static`, then follows the adapter file. If an organisation has an existing site-building skill with its own contract and publish flow, the adapter file points at that skill for the publish step and says which of its rules apply to a prototype (usually the help section, naming, and pre-deploy checklist) and which do not (content templates and word budgets).

## Share step

After export, write the live or local URL into `DEMO.md` and `DECISION.md`, and send the feedback section links to the viewers. Record who saw it and when in `DECISION.md`.

## Retiring a shared prototype

When `DECISION.md` says archive, remove the published copy (or mark it archived on the host) and record the removal date in `DECISION.md`. Keep the folder; it is the record.
