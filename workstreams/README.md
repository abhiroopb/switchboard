# Workstreams

A workstream is one ongoing area of work: a project, a recurring duty, a topic you keep coming back to. Each one is a folder here with a `CONTEXT.md` (what it is, where it stands, what is next, who is involved) and a `config.yaml` (name, emoji, priority, status). Deliverables and review notes for that work live in the same folder.

The Chief of Staff creates a workstream the first time you dispatch work on a new topic, and reuses it after that. Put `Workstream: <slug>` on the first line of an ask to choose one yourself.

Create one by hand with `python3 system/scripts/new-workstream.py "<what the work is>"`, which picks the slug and emoji. The table below is rebuilt by `python3 system/scripts/render-workstreams-readme.py --write`.

| Workstream | Status | Next step |
|---|---|---|
