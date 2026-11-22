---
description: Request end-of-day closeout in CoS (wrap up and close every workstream tab, then quit cmux)
allowed-tools: ["Bash"]
---

Request the end-of-day closeout (the Amp palette command "PM OS: End day").

Run this exact command with Bash:

```bash
bash "{{PM_OS_ROOT}}"/system/scripts/command-center-action.sh close-all --request
```

`--request` asks the CoS workspace to run `closing-day`, save the durable closeout, then detach teardown. It never tears anything down itself.

- Exit 0: report "Closeout requested in CoS. CoS saves context before shutdown."
- Non-zero exit: report "Nothing closed:" followed by the first 200 characters of stderr (CoS busy, a draft in its composer, or CoS not uniquely available). Do not retry and do not run teardown directly.

Never commit, push, pause other tasks, or unload daemons as part of this command.
