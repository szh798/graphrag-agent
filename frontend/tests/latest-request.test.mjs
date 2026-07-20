import assert from 'node:assert/strict'
import test from 'node:test'

import { LatestRequestGate } from '../src/app/latest-request.ts'

test('a slower historical-session response cannot overwrite the latest selection', () => {
  const gate = new LatestRequestGate()
  const firstSession = gate.begin()
  const secondSession = gate.begin()

  assert.equal(gate.isCurrent(firstSession), false)
  assert.equal(gate.isCurrent(secondSession), true)
})

test('starting a new conversation invalidates an in-flight stream', () => {
  const gate = new LatestRequestGate()
  const stream = gate.begin()
  gate.invalidate()

  assert.equal(gate.isCurrent(stream), false)
})
