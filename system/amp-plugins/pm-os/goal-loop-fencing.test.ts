import { expect, test } from 'bun:test'
import { GoalStateCoordinator, type GoalState } from '../goal-loop'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function fixture() {
  const states = new Map<string, GoalState>()
  const coordinator = new GoalStateCoordinator(
    async key => states.get(key) ?? null,
    async (key, state) => { states.set(key, structuredClone(state)) },
  )
  const set = (goal: string) => coordinator.replace('thread', () => ({
    goal, maxTurns: 10, turnsUsed: 0, active: true, setAt: 'now',
  }))
  return { states, coordinator, set }
}

test('clear during evaluation fences the delayed result without waiting for AI', async () => {
  const { states, coordinator, set } = fixture()
  const original = await set('original')
  const ai = deferred<void>()
  const evaluation = ai.promise.then(() => coordinator.commit('thread', original.revision!, state => ({ ...state, turnsUsed: 1 })))

  const cleared = await coordinator.commit('thread', original.revision!, state => ({ ...state, active: false, stoppedReason: 'cleared' }))
  expect(cleared?.active).toBe(false)
  ai.resolve()
  expect(await evaluation).toBeNull()
  expect(states.get('thread')?.stoppedReason).toBe('cleared')
})

test('replacement during evaluation is not overwritten by the old verdict', async () => {
  const { states, coordinator, set } = fixture()
  const original = await set('original')
  const ai = deferred<void>()
  const evaluation = ai.promise.then(() => coordinator.commit('thread', original.revision!, state => ({ ...state, active: false, stoppedReason: 'goal met' })))

  await set('replacement')
  ai.resolve()
  expect(await evaluation).toBeNull()
  expect(states.get('thread')?.goal).toBe('replacement')
  expect(states.get('thread')?.active).toBe(true)
})

test('evaluator failure after replacement cannot pause the replacement goal', async () => {
  const { states, coordinator, set } = fixture()
  const original = await set('original')
  const failure = deferred<void>()
  const evaluation = failure.promise.then(() => coordinator.commit('thread', original.revision!, state => ({
    ...state, active: false, stoppedReason: 'evaluator error: failed',
  })))

  await set('replacement')
  failure.resolve()
  expect(await evaluation).toBeNull()
  expect(states.get('thread')?.goal).toBe('replacement')
  expect(states.get('thread')?.active).toBe(true)
})

test('overlapping evaluations serialize commits so only one verdict advances state', async () => {
  const { states, coordinator, set } = fixture()
  const original = await set('goal')
  const first = deferred<void>()
  const second = deferred<void>()
  const evaluate = (gate: Promise<void>, reason: string) => gate.then(() =>
    coordinator.commit('thread', original.revision!, state => ({ ...state, turnsUsed: state.turnsUsed + 1, lastEvaluation: reason })),
  )
  const one = evaluate(first.promise, 'first')
  const two = evaluate(second.promise, 'second')

  second.resolve()
  expect((await two)?.lastEvaluation).toBe('second')
  first.resolve()
  expect(await one).toBeNull()
  expect(states.get('thread')?.turnsUsed).toBe(1)
  expect(states.get('thread')?.lastEvaluation).toBe('second')
})
