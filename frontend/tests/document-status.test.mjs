import assert from 'node:assert/strict'
import test from 'node:test'

import {
  documentStatusLabel,
  documentStatusStyles,
  normalizeDocumentStatus,
} from '../src/app/document-status.ts'

test('job states normalize to stable document presentation states', () => {
  for (const status of ['submitted', 'queued', 'parsing', 'extracting', 'indexing']) {
    assert.equal(normalizeDocumentStatus(status), 'indexing')
  }
  assert.equal(normalizeDocumentStatus('done'), 'indexed')
  assert.equal(normalizeDocumentStatus('cancelled'), 'uploaded')
})

test('unknown status always has a safe label and style', () => {
  const status = normalizeDocumentStatus('new-backend-state')
  assert.equal(status, 'unknown')
  assert.equal(documentStatusLabel[status], '状态未知')
  assert.deepEqual(documentStatusStyles[status], {
    bg: '#252b33',
    color: '#a8b3c2',
  })
})
