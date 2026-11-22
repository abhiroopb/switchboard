# To-do setup

## Where the list lives

The list is `system/state/todo.json` under the repo root. The repo root is `PM_OS_ROOT` when that variable is set, otherwise `~/Development/pm-os`. Pass `--path <file>` to point the CLI at another file, for example in a test.

Next to the list the CLI keeps:

- `todo.json.lock`, the sidecar lock every write takes.
- `todo-archive/YYYY-MM.json`, one archive file per month.
- `todo-calendar.json`, the id of the dedicated calendar for holds.

All of these are local state. Keep them out of git.

## Create an empty list

The CLI creates the file on its first write, so this step is optional. To start from an empty file by hand:

```bash
mkdir -p system/state
printf '{"version": 2, "items": []}\n' > system/state/todo.json
```

Then check it:

```bash
python3 ~/.agents/skills/todo/scripts/todo_cli.py summary
```

## Optional settings

| Setting | What it does |
|---|---|
| `PM_OS_TODO_CALENDAR_ID` | Use this calendar for holds instead of finding or creating `To-dos (PM OS)`. |
| `PM_OS_TODO_CALENDAR_STATE` | Where the calendar id is recorded. |
| `PM_OS_TODO_RESOLUTION_FLAGS` | The file of likely-complete flags a background job writes. |
| `PM_OS_CONNECTORS_DIR` | Folder with the connector token file `google-token.json`. |
| `PM_OS_OWNER_SLACK_IDS` | Comma list of the owner's Slack user ids, used to tell the owner's replies apart. |
| `PM_OS_OWNER_HANDLES` | Comma list of the owner's Slack handles, display names, or email addresses. |
| `PM_OS_TODO_SURFACE_SKIP=1` | Skip the verification step before views. |
| `PM_OS_TODO_SURFACE_VERIFY_LIMIT` | How many items a view verifies at most (default 25). |
| `PM_OS_TODO_OFFLINE=1` | Make no network calls at all. `PM_OS_TEST_SANDBOX=1` does the same. |

## Calendar access

Holds need a Google token with the full calendar scope. The CLI tries `gcloud auth print-access-token` first, then the connector token file. Without a token the list still works, and eligible items show up under `unsynced` until a token is available.

## Recurring templates

Templates run only while `recurrence_enabled: true` is set in `system/config/workflow-upgrade-flags.yaml`.
