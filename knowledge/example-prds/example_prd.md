# Shared shopping lists PRD

An invented example that shows the depth and shape switchboard expects from a PRD. The product, the numbers and the people are made up. It follows `templates/prd-template.md`.

| Field | Value |
|---|---|
| Owner | Robin Example, product manager |
| Status | Draft for review |
| Reviewers | Engineering lead, design lead, data analyst |
| Last updated | 2026-01-15 |

## 1. Problem Statement

People who shop for a household keep separate lists in notes apps and group chats. Items get bought twice, or not at all, because nobody can see what someone else already picked up. In last quarter's survey, 41% of weekly active users said they share shopping duty with someone else, and 27% of those said they "often" buy a duplicate item. The app has no way to share a list today, so these users leave it at the moment they need it most.

## 2. Goals (Measurable)

- **Adoption.** 20% of weekly active users create or join a shared list within 60 days of launch.
- **Retention.** Users in a shared list keep a 4-week retention at least 8 points above matched users without one.
- **Fewer duplicates.** Fewer "bought twice" reports in the in-app survey, from 27% to under 15%.

## 3. Non-Goals / Out of Scope

- Split payments or cost sharing between list members.
- Lists shared with more than 8 people.
- Store-specific aisle ordering. That is a separate bet on the roadmap.

## 4. Users and Use Cases

- **The planner** writes the list for the week and wants others to see it change live.
- **The runner** does the shopping and checks items off in the store, often with a weak signal.
- **The occasional helper** adds one or two items from a message and rarely opens the app.

Key use case: the planner shares a list with the runner. The runner checks items off in the store, and the planner sees each item go grey within seconds.

## 5. Requirements

### 5.1 Functional Requirements

1. A list owner can invite people by link or contact. An invite expires after 7 days.
2. Every member can add, edit, check and uncheck items. Each change shows who made it.
3. Changes made offline sync when the device reconnects. Two edits to the same item keep the later one and show a small "edited by" note.
4. The owner can remove a member, and the removed member keeps a read-only copy.

### 5.2 Non-Functional Requirements

- A change reaches other online members within 3 seconds at the 95th percentile.
- Offline edits survive an app restart.
- No list content appears in push notification previews unless the member turns that on.

### 5.3 Constraints and Assumptions

- The sync service the notes feature already uses can carry list events at the expected volume.
- Contact invites need the existing contacts permission. Link invites need none.

## 6. UX / Flows

1. The owner taps Share on a list, picks link or contact, and sends the invite.
2. The invitee opens the link and lands on the list with a one-line banner: "Shared by Robin. You can add and check items."
3. In the store, the runner checks items off. Checked items move to the bottom with the runner's initial.

## 7. Logging & Analytics Events

| Event | When | Properties |
|---|---|---|
| `list_shared` | An invite is sent | method (link, contact), member_count |
| `list_joined` | An invitee opens the list for the first time | days_since_invite |
| `item_checked` | A member checks an item | is_owner, was_offline |
| `sync_conflict` | Two edits to one item meet | seconds_apart |

## 8. Prototype

A clickable prototype covers the share sheet, the invitee banner and the in-store check-off. Five moderated sessions with households that shop together will test it before engineering starts.

## 9. Success Metrics & Monitoring

- **Primary.** Weekly share rate and 4-week retention of shared-list members.
- **Guardrail.** Crash-free sessions stay at or above today's rate, and sync conflict events stay under 1% of item edits.
- **Dashboard.** One page with the four events above, split by platform.

## 10. Rollout / Launch Plan

1. Internal households for two weeks.
2. 10% of users in one region, with the guardrails watched daily.
3. Everyone, once the guardrails hold for two weeks.

## 11. Risks & Mitigations

- **Sync conflicts confuse people.** Mitigation: last edit wins, with a visible "edited by" note and an undo.
- **Invite spam.** Mitigation: a cap of 20 invites a day per account, and the 7-day expiry.

## 12. Open Questions

- Should a member be able to leave a list without the owner's action? The engineering lead decides this before the design review.
- Do we show prices on shared items, or keep them private to the person who added them?

## 13. Appendix

- Survey summary, last quarter (internal link in a real PRD).
- Competitive notes on two popular list apps (internal link in a real PRD).
