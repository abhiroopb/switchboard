# Third-party notices

switchboard bundles the third-party agent skills below under `system/vendor/skills/`. Each keeps its own license, not the PolyForm Noncommercial License that covers switchboard's own code and docs. Each folder holds the upstream license as `LICENSE.upstream` and a `SOURCE.md` that names the repository, the pinned commit and every local change (Apache-2.0 section 4 asks modified files to say so). You may use these skills under their own terms, including commercially where their license allows.

| Skill | Upstream | Commit | License | Changed locally |
|---|---|---|---|---|
| `ab-testing` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `competitive-brief` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `copy-editing` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `customer-research` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `design-critique` | <https://github.com/cuellarfr/design-skills> | `7e25e66e87f6` | MIT | no |
| `diagram-design` | <https://github.com/cathrynlavery/diagram-design> | `0cf73c3fcc56` | MIT | no |
| `emails` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `explore-data` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `humanizer` | <https://github.com/blader/humanizer> | `0ef9fe5bc357` | MIT | yes |
| `launch` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `marketing-psychology` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `metrics-review` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `obsidian-bases` | <https://github.com/kepano/obsidian-skills> | `3ccff5338ea7` | MIT | no |
| `obsidian-cli` | <https://github.com/kepano/obsidian-skills> | `3ccff5338ea7` | MIT | no |
| `obsidian-markdown` | <https://github.com/kepano/obsidian-skills> | `3ccff5338ea7` | MIT | no |
| `onboarding-cro` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | yes |
| `opportunity-solution-tree` | <https://github.com/phuryn/pm-skills> | `2a12295a7717` | MIT | yes |
| `outcome-roadmap` | <https://github.com/phuryn/pm-skills> | `2a12295a7717` | MIT | yes |
| `pre-mortem` | <https://github.com/phuryn/pm-skills> | `2a12295a7717` | MIT | yes |
| `pricing` | <https://github.com/coreyhaines31/marketingskills> | `88c79f6528a9` | MIT | no |
| `receiving-code-review` | <https://github.com/obra/superpowers> | `2ce8aa15d05b` | MIT | no |
| `sql-queries` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | no |
| `stakeholder-update` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `strategy-red-team` | <https://github.com/phuryn/pm-skills> | `2a12295a7717` | MIT | yes |
| `synthesize-research` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `systematic-debugging` | <https://github.com/obra/superpowers> | `2ce8aa15d05b` | MIT | yes |
| `taste-skill` | <https://github.com/Leonxlnx/taste-skill> | `12af471f627f` | MIT | yes |
| `ui-ux-pro-max` | <https://github.com/nextlevelbuilder/ui-ux-pro-max-skill> | `3a13158ff1f3` | MIT | yes |
| `verification-before-completion` | <https://github.com/obra/superpowers> | `2ce8aa15d05b` | MIT | no |
| `write-query` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |
| `write-spec` | <https://github.com/anthropics/knowledge-work-plugins> | `2574e29377c7` | Apache-2.0 | yes |

## Skills outside the vendor folder

Two skills in `.agents/skills/` also come from third parties. Each keeps its upstream MIT license in its own folder.

| Skill | Upstream | Commit | License | Changed locally |
|---|---|---|---|---|
| `plan-ceo-review` | <https://github.com/garrytan/gstack> (v0.0.1) | `3d901066cdb3` | MIT | yes, listed in its `SOURCE.md` |
| `asd-ste100` | <https://github.com/danyuchn/asd-ste100-skill> | `8564f8985f15` | MIT | yes, frontmatter only |

## Plugin agents and commands

Six agents and four commands in the Claude Code plugin (`system/claude-marketplace/plugins/switchboard/`) come unchanged from [HumanLayer](https://github.com/humanlayer/humanlayer) under the Apache License 2.0. The plugin folder holds the license as `LICENSE.humanlayer` and lists each file in `THIRD_PARTY.md`.

## Summary

In the vendor folder, 23 skills are MIT and 8 are Apache-2.0. The two skills outside it are MIT, and the ten plugin files are Apache-2.0. Both licenses allow redistribution inside a noncommercially licensed project, provided their license text and notices travel with the files, which they do.

switchboard also works with tools it does not bundle, each under its own terms: cmux, wmux, Amp, Claude Code, Codex CLI, Cursor, Obsidian, the GitHub CLI, Bun and claude-mem.
