import { expect, test } from 'bun:test'
import { bundle } from './bundle'

test('all hooks run; an undefined observer does not erase the context response', async () => {
  const hooks = new Map<string, Function>()
  const config = { 'original.disabled': true }
  const calls: string[] = []
  const amp: any = { configuration: { get: async () => config }, on: (e: string, h: Function) => hooks.set(e, h) }
  await bundle(amp, [
    ['context', async api => {
      expect(await api.configuration.get()).toBe(config)
      api.on('agent.start', async () => ({ message: { content: 'preserved', display: true } }))
    }],
    ['observer', api => { api.on('agent.start', () => { calls.push('observer') }) }],
    ['failure', api => { api.on('agent.start', () => { throw new Error('fixture') }) }],
  ])
  const result = await hooks.get('agent.start')!({}, { logger: { log: (s: string) => calls.push(s) } })
  expect(result).toEqual({ message: { content: 'preserved', display: true } })
  expect(calls).toEqual(['observer', '[failure] hook failed: Error: fixture'])
})

test('unsubscribe removes only its handler; multiple responses fail visibly', async () => {
  const hooks = new Map<string, Function>()
  const amp: any = { on: (e: string, h: Function) => hooks.set(e, h) }
  await bundle(amp, [['a', api => {
    api.on('agent.start', () => { throw new Error('must not run') }).unsubscribe()
    api.on('agent.start', () => ({ message: { content: 'one' } }))
    api.on('agent.start', () => ({ message: { content: 'two' } }))
  }]])
  await expect(hooks.get('agent.start')!({}, { logger: { log: () => {} } })).rejects.toThrow('multiple agent.start responses')
})

test('a failed module import does not prevent healthy modules loading', async () => {
  const hooks = new Map<string, Function>()
  const logs: string[] = []
  const amp: any = { logger: { log: (s: string) => logs.push(s) }, on: (e: string, h: Function) => hooks.set(e, h) }
  await bundle(amp, [
    ['broken', async () => { throw new Error('import failed') }],
    ['healthy', api => { api.on('agent.start', () => ({ message: { content: 'ready' } })) }],
  ])
  expect(logs).toEqual(['[broken] initialization failed: Error: import failed'])
  expect(await hooks.get('agent.start')!({}, amp)).toEqual({ message: { content: 'ready' } })
})
