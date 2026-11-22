# Contributing

Thanks for looking. switchboard is one person's working setup, shared as a starting point, so the best contribution is usually your own fork made yours.

- **Issues.** Bugs, unclear docs and install problems are welcome. Include your OS, multiplexer (cmux, wmux or none), agent and the `verify-kit-install.sh` output, with personal details removed.
- **Pull requests.** Keep them small and include a test when you change a script (`system/scripts/test_*.py`, run with `python3 -m unittest`). By opening one you agree your change is licensed under the PolyForm Noncommercial License 1.0.0, like the rest of the project.
- **Skills.** A new skill lives in `.agents/skills/<name>/SKILL.md`. Third-party skills go in `system/vendor/skills/` with their license and a `SOURCE.md`.
- **No personal data.** Use invented names, ids and paths in tests and examples.
