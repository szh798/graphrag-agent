import assert from 'node:assert/strict'
import test from 'node:test'

import {
  documentIndexProgress,
  hasActiveDocumentIndex,
  isActiveIndexState,
} from '../src/app/document-index-state.ts'

test('LightRAG keeps a document active after the legacy compatibility status is indexed', () => {
  const document = {
    status: 'indexed',
    progress: 0.72,
    indexes: {
      legacy: { status: 'indexed', raw_status: 'done', progress: 100 },
      lightrag: { status: 'indexing', raw_status: 'indexing', progress: 72 },
    },
  }

  assert.equal(hasActiveDocumentIndex(document), true)
  assert.equal(documentIndexProgress(document), 72)
})

test('terminal dual indexes do not expose an active task', () => {
  const document = {
    status: 'indexed',
    indexes: {
      legacy: { status: 'indexed', raw_status: 'done' },
      lightrag: { status: 'failed', raw_status: 'failed' },
    },
  }

  assert.equal(hasActiveDocumentIndex(document), false)
  assert.equal(isActiveIndexState(document.indexes.lightrag), false)
})
