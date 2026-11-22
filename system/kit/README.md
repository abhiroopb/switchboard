# Building a shareable copy

The working checkout is the one source of truth, and it stays on this machine: no remote, no automatic push. `build_tier.py` makes a filtered copy for sharing, built from the checkout and never edited by hand except through the override step below.

## Files

| File | Ships | What it is |
|---|---|---|
| `build_tier.py` | yes | The engine: filter, three-way merge, overlay, gates, write, commit, push. |
| `tiers.yaml` | yes | The targets and their generic rules (excludes, overlay, gates, author, visibility). |
| `adapter_indexes.py` | yes | Rewrites `adapters/PORTABILITY.md`, `portability.json` and `SKILLS.md` from the files that ship (`adapter_indexes: true`). |
| `scan_public.py` | yes | Generic personal-data detectors: emails, phones, chat and document ids, IPs, home paths, image metadata, quoted rulings. |
| `gitleaks.toml` | yes | gitleaks defaults plus reviewed false positives. |
| `overlay/public/` | yes | The public repository's own files: README, INSTALL, LICENSE, notices, docs, images, original skills. |
| `local/kit/public.rules` | never | Owner-local excludes, renames and rewrites that name a company term or a private topic. |
| `local/kit/personal-deny.txt` | never | Owner-local deny list: your identity, private paths, workstream names, every people profile name. |
| `local/kit-deny-list.txt` | never | Owner-local employer deny list (`check-profile-leaks.py` format). |

## Build

```
python3 system/kit/build_tier.py build --tier public --out <clone> [--report r.json] [--keep-failed <dir>]
python3 system/kit/build_tier.py build --tier public --out <clone> --commit
python3 system/kit/build_tier.py push --tier public --out <clone>
```

1. **Filter.** The source is read at the last built revision (base) and at the new one (theirs). Tracked files only, minus the excludes, with renames and rewrites applied. A `rewrite_in <glob> <regex> => <text>` rule rewrites only the files that match the glob. `--rev WORKTREE` reads an unlanded task branch from disk.
2. **Merge.** The change from base to theirs is merged into the last published tree (ours, the clone's `HEAD`), file by file, with `git merge-file`. Edits made for the public copy survive later builds. A conflict stops the build and names the file. `--conflicts source` settles each conflicting hunk with the source's side instead (other public edits in that file stay), and the report lists those files under `settled_for_source` so a reviewer can read them.
   The base is filtered with the rules the last committed build used (`local/kit/<tier>.last-rules/`, saved by every `--commit`, or `--base-rules <dir>`), and the new source with today's rules. A rule change therefore merges like a source change, and a new rule reaches lines that did not change.
3. **Override.** `--override <dir>` replaces files with hand-resolved versions. It is for conflicts and for the rare line no rule can fix. The next build keeps the result, because it is now in ours.
4. **Overlay.** `overlay/public/` lands at the root of the copy.
5. **Gate.** The checks are the deny lists, the private identifier guard, `scan_public.py` and gitleaks (`PM_OS_GITLEAKS` names the binary). A file under a `drop_on_hit` glob that fails is left out and listed. Any other failure stops the build, and a stopped build writes nothing into the clone. `--skip-gate` is for fix-up passes and turns the build into a dry run.
6. **Write.** The tree is synced into the clone, `.pm-os-kit/tier.json` records the source revision, and `--commit` commits under the target's neutral author.

## Push

`push` refuses unless the clone is on `main`, the remote exists, and `gh repo view` reports the visibility `tiers.yaml` expects. It never forces. The repository is created private, and it becomes public only on the owner's explicit word, after which `visibility` in `tiers.yaml` changes to `PUBLIC`.

## First build

A first build into an existing tree can start from it with `--bootstrap-ours <tree>` and take an older source folder as base with `--bootstrap-base <folder>`, so earlier hand edits carry over. Later builds need neither flag.

## Tests

`python3 -m unittest test_build_tier` from `system/kit` covers the rules parser, filters, merge cases, overlay, the deny and public gates, and a full build into a temporary repository, all with invented data.
