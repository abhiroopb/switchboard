# Gmail — Detailed Workflow

Load when processing the Gmail section of the start-of-day triage.

## 2a. Fetch All Inbox Emails

Fetch both unread and read emails in the inbox. Make two parallel calls:

```
pmos mail search --label Inbox --only-is-unread --max-results 25
pmos mail search --label Inbox --only-is-read --max-results 20
```

Combine results into a single list. Unread emails come first, then read emails. De-duplicate by message ID if any overlap.

## 2b. Present All Emails as a Numbered List

Display all inbox emails as a single numbered list, grouped by read status and importance. For each email show:

- **Index number** (restart at 1 for emails)
- **Sender** (name and email)
- **Subject**
- **Time** (human-readable relative time)
- **Snippet** (first ~100 chars)
- **Labels** (if any beyond Inbox)

Categorize into sections:

```
## Gmail Inbox (12 emails — 3 unread, 9 read)

### Unread
#### From People
1. **Parker Example** <sam@example.com> (2h ago)
   "Re: Onboarding redesign Q1 plan" — "Thanks for the updated timeline..."

#### Automated / Notifications
2. **GitHub** (1h ago) — "[acme/example-app] PR #101 merged"

### Read but Still in Inbox
#### From People
3. **Morgan Example** (2d ago)
   "Re: [Feature X] feature X not working" — "Login email: customer@example.com..."
```

## 2c. Present Each Email with Actions

**IMPORTANT: Present ONE email at a time.** Show the email, draft a response if applicable, and wait for the user's action before moving to the next email. Never batch multiple emails in a single message.

Go through each email one at a time (unread first, then read). For each email:

1. Read the full email with `pmos mail read --message-ids <id>`.
2. Show a summary of who sent it, what it's about, and the key content.
3. If it's a human-written email that warrants a reply, **proactively draft a reply**.
4. Offer options:
   - **(1) Send reply** (draft a reply)
   - **(2) Mark as read** (only shown for unread emails)
   - **(3) Archive** (remove from inbox)
   - **(4) Save for later** (adds your save-for-later label and archives; personalize here, default `To Do`)
   - **(5) Skip** (leave in inbox)

For bot/automated emails (GitHub, Datadog, Calendar invites, Figma notifications), skip the draft and just offer options 2–5.

## 2d. Execute Email Actions

### Send Reply

1. Create the reply draft with `pmos mail draft --draft-type reply --reference-message-id <id> --to-recipient <to> --body "<text>"`. It never sends.
2. Where `PM_OS_GMAIL_SEND` is on, show it with `pmos mail show --draft-id <id>`, print To, Cc, Bcc, Subject, the body, and any links verbatim in one message, and wait. After the owner's own "send it", run `pmos mail send --draft-id <id> --expect-hash <send check>`. Where it is off, tell the user: "Reply draft saved to <To>, subject <Subject> — open Gmail to review and hit send."
3. If unread, mark as read: `pmos mail modify --message-ids <id> --labels-to-remove UNREAD`.

### Mark as Read

```
pmos mail modify --message-ids <IDs> --labels-to-remove UNREAD
```

### Archive

```
pmos mail modify --message-ids <IDs> --labels-to-remove INBOX
```

### Save for Later

```
pmos mail modify --message-ids <IDs> --labels-to-add "To Do" --labels-to-remove INBOX
```
