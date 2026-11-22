import { afterAll, afterEach, expect, mock, test } from 'bun:test'
import * as lib from './lib'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Rule 9b end to end (open a page in the background, never steal focus):
// an agent's bare `open <page>` is rejected through the tool.call hook, and the
// background form passes. Run: bun test system/amp-plugins/pm-os/open-focus-plugin.test.ts

const logs: Record<string, unknown>[] = []
mock.module('./lib', () => ({
  ...lib,
  logEvent: async (_plugin: string, event: Record<string, unknown>) => { logs.push(event) },
}))
const { default: plugin } = await import('../pm-os-guardrails')
afterEach(() => { logs.length = 0 })

function harness(config: Record<string, unknown> = {}) {
  let handler: (event: any, ctx: any) => Promise<any>
  plugin({
    configuration: { subscribe: (cb: any) => cb({
      'pm-os-guardrails.confirm_timeout_ms': 20,
      'pm-os-guardrails.permission-relay.disabled': true,
      ...config,
    }) },
    on: (name: string, fn: any) => { if (name === 'tool.call') handler = fn },
    helpers: { shellCommandFromToolCall: (event: any) => event.tool === 'Bash' ? { command: event.input.cmd } : null },
  } as any)
  return (cmd: string) => handler({ tool: 'Bash', input: { cmd } }, { ui: { confirm: async () => false } })
}

test('a bare open of a page is rejected with the cmux-tab fix', async () => {
  const call = harness()
  const out = await call('open /tmp/compare.html')
  expect(out.action).toBe('reject-and-continue')
  expect(out.message).toContain('image_compare.py open <page-or-url>')
  expect(logs).toContainEqual(expect.objectContaining({ rule: 'open-focus', action: 'reject' }))
})

// Chrome raises itself even with -g, so open -g is rejected too.
test('open -g is rejected, and the kill switch turns the rule off', async () => {
  expect(await harness()('open -g /tmp/compare.html')).toEqual(expect.objectContaining({ action: 'reject-and-continue' }))
  expect(await harness()('python3 system/scripts/image_compare.py open /tmp/compare.html')).not.toEqual(expect.objectContaining({ action: 'reject-and-continue' }))
  const off = harness({ 'pm-os-guardrails.open-focus.disabled': true })
  expect(await off('open /tmp/compare.html')).not.toEqual(expect.objectContaining({ action: 'reject-and-continue' }))
})
