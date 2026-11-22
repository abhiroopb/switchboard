# Google Calendar — Detailed Workflow

Load when processing the calendar section of the start-of-day triage.

## 3a. Fetch Today's Events

```bash
pmos calendar events \
  --json '{"time_period":{"time_range":{"time_min":"<TODAY>T00:00:00<UTC offset>","time_max":"<TOMORROW>T00:00:00<UTC offset>"}},"include_attendees_info":true}' \
  --timezone "$PM_OS_TZ"
```

Replace `<TODAY>`, `<TOMORROW>` and `<UTC offset>` with real values for your time zone (`PM_OS_TZ` in `~/.pm-os/env`).

## 3b. Check for Conflicts

From the same event list, flag accepted events whose times overlap, and back-to-back blocks with no gap.

## 3c. No RSVP review

Never present pending (`needsAction` or `tentative`) invites for a decision,
and never RSVP on the owner's behalf. {{OWNER_FIRST}} accepts and declines
meetings themselves. Show already-accepted events as a summary list for prep, and
calculate the actual minutes or hours until the next meeting from the current
system time.
