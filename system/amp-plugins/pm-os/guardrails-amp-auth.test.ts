import { afterAll, expect, mock, test } from 'bun:test'
import * as lib from './lib'
import { restoreModuleMocks } from './test-mocks'

// Bun keeps module mocks for the whole process; put the real modules back for later files.
afterAll(restoreModuleMocks)

// Rule 9e wiring: the guardrails plugin rejects an amp call with a substitute home through its real
// tool.call handler, and its kill switch turns the rule off. The rule's own cases are in amp-auth.test.ts.
// Run: bun test system/amp-plugins/pm-os/guardrails-amp-auth.test.ts

const logs: Record<string, unknown>[] = []
mock.module('./lib', () => ({
  ...lib,
  logEvent: async (_plugin: string, event: Record<string, unknown>) => { logs.push(event) },
}))

const { default: plugin } = await import('../pm-os-guardrails')

function harness(config: Record<string, unknown> = {}) {
  let handler: (event: any, ctx: any) => Promise<any>
  plugin({
    configuration: { subscribe: (cb: any) => cb({
      'pm-os-guardrails.cos-delivery-gate.assume-cos': true,
      'pm-os-guardrails.dispatch-guard.assume-origin': true,
      'pm-os-guardrails.confirm_timeout_ms': 20,
      'pm-os-guardrails.permission-relay.disabled': true,
      ...config,
    }) },
    on: (name: string, fn: any) => { if (name === 'tool.call') handler = fn },
    helpers: { shellCommandFromToolCall: (event: any) => event.tool === 'Bash' ? { command: event.input.cmd } : null },
  } as any)
  return (cmd: string) => handler({ tool: 'Bash', input: { cmd } }, { ui: { confirm: () => new Promise(() => {}) } })
}

const OVERRIDE = 'HOME=/tmp/fake USERPROFILE=C:/tmp/fake ~/.amp/bin/amp skill list'

test('an amp call with a substitute home is rejected and logged', async () => {
  const result = await harness()(OVERRIDE)
  expect(result.action).toBe('reject-and-continue')
  expect(result.message).toContain('browser')
  expect(logs).toContainEqual(expect.objectContaining({ rule: 'amp-auth', action: 'reject' }))
})

test('the kill switch turns the rule off', async () => {
  const result = await harness({ 'pm-os-guardrails.amp-auth.disabled': true })(OVERRIDE)
  expect(result?.action).not.toBe('reject-and-continue')
})
