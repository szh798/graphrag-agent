import assert from 'node:assert/strict'
import test from 'node:test'

import { indexProgressPercent } from '../src/app/index-progress.ts'


test('structured backend progress uses parsed pages over total pages', () => {
  assert.equal(indexProgressPercent({ parsed_pages: 3, total_pages: 4 }), 75)
  assert.equal(indexProgressPercent({ parsed_pages: 0, total_pages: 0 }), 0)
  assert.equal(indexProgressPercent({ parsed_pages: 8, total_pages: 4 }), 100)
})

test('legacy numeric progress remains compatible and bounded', () => {
  assert.equal(indexProgressPercent(0.42), 42)
  assert.equal(indexProgressPercent(42), 42)
  assert.equal(indexProgressPercent(Number.NaN), 0)
  assert.equal(indexProgressPercent(-1), 0)
  assert.equal(indexProgressPercent(2), 2)
  assert.equal(indexProgressPercent(101), 100)
})
