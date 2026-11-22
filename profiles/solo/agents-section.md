## Single-session mode (solo profile)

This install has no terminal multiplexer, so there are no worker tabs. These rules replace the routing rules above where they differ.

- **Do the work here.** The session the owner talks to plans the task and then does it itself. `router-dispatch.sh` and `dispatch.py` are not used, and the rule that the Chief of Staff never delivers does not apply.
- **One task at a time.** Finish or park the current task before starting the next. Say which task is active when the owner switches topics.
- **Branch per task.** For a code or file change, create a branch (`git switch -c task/<short-name>`), commit there, and merge to `main` only when the owner says it is done.
- **Questions go to the owner directly.** Ask in this session with the options and a default, instead of `ask-cos.sh`.
- **Everything else holds.** Workstreams, `CONTEXT.md`, review notes, the to-do list, start of day, end of day, the writing rules and every guardrail work as written.
- **More agents later.** Install cmux (macOS) or wmux (Windows) and run `python3 system/scripts/apply-profile.py --profile personal` to switch to the parallel loop.
