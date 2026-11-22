# Source

- Repository: https://github.com/garrytan/gstack
- Upstream: gstack v0.0.1 ("Initial release"), commit `3d901066cdb34602675d41dd70b165a6f1a7347c`
- Path: `plan-ceo-review/SKILL.md`
- Author: Garry Tan
- License: MIT, copied to `LICENSE.upstream` in this folder.

## Local changes

- Section 0B gains item 3, an already-answered pre-check: before the review lists anything that would
  ask the owner to reply, send, or approve, it re-reads the live source thread and runs
  `system/scripts/reply_check.py`. Answered items are reported as "already answered", never as asks.
- "Unresolved Decisions" gains one sentence that applies the same pre-check.
- The example class in the failure-mode table is renamed from `ExampleService` to `SampleService`.

Everything else is the upstream text unchanged.
