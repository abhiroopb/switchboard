#!/usr/bin/env bash
# Fixture-only plugin registration checks. No live threads or event callbacks.
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BUN="${BUN:-$(command -v bun || true)}"
[[ -n "$BUN" ]] || { echo "BLOCKED: bun unavailable" >&2; exit 2; }
TEST_REPO_ROOT="$REPO_ROOT" "$BUN" run - <<'TS'
import assert from 'node:assert/strict'

const root = process.env.TEST_REPO_ROOT!
for (const [name, eventName] of [
  ['workstream-lifecycle', 'agent.end'],
  ['watched-docs-refresh', 'session.start'],
  ['watched-slack-refresh', 'session.start'],
  ['workstream-signals', 'session.start'],
]) {
  const plugin = await import(`${root}/system/amp-plugins/${name}.ts`)
  process.env.SWITCHBOARD_WORKER_ID = 'worker-fixture'
  const forbidden = new Proxy({}, { get() { throw new Error(`${name} registered owner hooks`) } })
  plugin.default(forbidden)
  console.log(`PASS supplemental ${name} registers no owner hooks`)

  delete process.env.SWITCHBOARD_WORKER_ID
  const events: string[] = []
  let subscriptions = 0
  plugin.default({
    configuration: { subscribe(cb: (value: object) => void) { subscriptions++; cb({}) } },
    on(event: string) { events.push(event) },
  })
  assert.equal(subscriptions, 1)
  assert.deepEqual(events, [eventName])
  console.log(`PASS ordinary ${name} still registers ${eventName}`)
}
TS
