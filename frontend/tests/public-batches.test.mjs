import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  handlePublicBatchRequest,
  matchPublicBatchRoute,
  publicBatchConstants,
} from '../worker/public-batches.mjs'
import { TestD1Database } from './helpers/d1-test-db.mjs'

const VISITOR_A = '123e4567-e89b-42d3-a456-426614174000'
const VISITOR_B = '987fcdeb-51a2-43d7-8fed-cba987654321'
const BACKEND_ORIGIN = 'https://backend.example'
const PROXY_SECRET = 'test-proxy-secret'

function batchRequest(path, method = 'GET', body) {
  return new Request(`https://site.example${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

async function invoke({
  db,
  visitorId = VISITOR_A,
  path,
  method = 'GET',
  body,
  fetcher,
  nowMs = Date.now(),
}) {
  const response = await handlePublicBatchRequest({
    request: batchRequest(path, method, body),
    db,
    visitorId,
    backendOrigin: BACKEND_ORIGIN,
    proxySecret: PROXY_SECRET,
    fetcher,
    nowMs,
  })
  return { response, payload: await response.json() }
}

async function createBatch(db, questions, options = {}) {
  const created = await invoke({
    db,
    path: '/api/v1/query/batch',
    method: 'POST',
    body: { questions },
    ...options,
  })
  assert.equal(created.response.status, 202)
  assert.equal(created.payload.code, 0)
  return created.payload.data
}

function backendSuccess(answerFor = question => `answer:${question}`) {
  const calls = []
  const fetcher = async request => {
    calls.push(request)
    const body = await request.json()
    assert.equal(request.redirect, 'manual')
    assert.equal(request.headers.get('x-graphrag-proxy-secret'), PROXY_SECRET)
    assert.equal(request.headers.get('x-graphrag-visitor-id'), VISITOR_A)
    assert.equal(request.headers.get('x-graphrag-stateless-batch'), '1')
    assert.deepEqual(body.history, [])
    assert.equal('session_id' in body, false)
    return Response.json({
      code: 0,
      msg: 'success',
      request_id: crypto.randomUUID(),
      data: {
        id: `q_${calls.length}`,
        question: body.question,
        answer: answerFor(body.question),
        tool_calls: [],
        cited_nodes: [],
        duration_seconds: 0.1,
        timestamp: new Date().toISOString(),
        session_id: `temporary_${calls.length}`,
        session: { id: `temporary_${calls.length}` },
      },
    })
  }
  return { calls, fetcher }
}

test('create/list/detail/cancel are visitor-isolated and keep the batch contract', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  const created = await createBatch(db, ['first', 'second'], { nowMs })

  assert.match(created.batch_id, /^batch_[0-9a-f]{10}$/)
  assert.deepEqual(
    Object.keys(created).sort(),
    ['batch_id', 'created_at', 'status', 'total'],
  )

  const ownList = await invoke({
    db,
    path: '/api/v1/query/batch?page=1&page_size=20',
    nowMs,
  })
  assert.equal(ownList.payload.data.total, 1)
  assert.equal(ownList.payload.data.items[0].batch_id, created.batch_id)

  const otherList = await invoke({
    db,
    visitorId: VISITOR_B,
    path: '/api/v1/query/batch',
    nowMs,
  })
  assert.equal(otherList.payload.data.total, 0)

  const otherDetail = await invoke({
    db,
    visitorId: VISITOR_B,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: () => assert.fail('another visitor must not reach the backend'),
    nowMs,
  })
  assert.equal(otherDetail.response.status, 404)
  assert.equal(otherDetail.payload.code, 2002)

  const otherCancel = await invoke({
    db,
    visitorId: VISITOR_B,
    path: `/api/v1/query/batch/${created.batch_id}`,
    method: 'DELETE',
    nowMs,
  })
  assert.equal(otherCancel.response.status, 404)

  const cancelled = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    method: 'DELETE',
    nowMs,
  })
  assert.deepEqual(cancelled.payload.data, {
    batch_id: created.batch_id,
    previous_status: 'submitted',
    status: 'cancelled',
    cancel_requested: true,
  })

  const detail = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: () => assert.fail('cancelled batches must not start another item'),
    nowMs,
  })
  assert.equal(detail.payload.data.status, 'cancelled')
  assert.equal(detail.payload.data.results.length, 0)
})

test('detail advances exactly one item and strips temporary sessions before D1 persistence', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  const created = await createBatch(db, ['one', 'two'], { nowMs })
  const backend = backendSuccess()

  const first = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: backend.fetcher,
    nowMs,
  })
  assert.equal(first.payload.data.status, 'running')
  assert.equal(first.payload.data.completed, 1)
  assert.equal(first.payload.data.failed, 0)
  assert.equal(first.payload.data.results[0].answer, 'answer:one')
  assert.equal('session' in first.payload.data.results[0], false)
  assert.equal('session_id' in first.payload.data.results[0], false)

  const second = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: backend.fetcher,
    nowMs: nowMs + 1,
  })
  assert.equal(second.payload.data.status, 'done')
  assert.equal(second.payload.data.completed, 2)
  assert.deepEqual(
    second.payload.data.results.map(item => item.question),
    ['one', 'two'],
  )
  assert.equal(backend.calls.length, 2)

  const persisted = db.rows(
    'SELECT result_json FROM public_batch_items ORDER BY position',
  )
  for (const row of persisted) {
    assert.equal(row.result_json.includes('session_id'), false)
    assert.equal(row.result_json.includes('"session"'), false)
  }

  await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: backend.fetcher,
    nowMs: nowMs + 2,
  })
  assert.equal(backend.calls.length, 2, 'terminal polls must not call the backend')
})

test('concurrent detail requests share one atomic claim and never double-charge an item', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  const created = await createBatch(db, ['only once'], { nowMs })

  let releaseBackend
  let announceBackend
  const backendEntered = new Promise(resolve => {
    announceBackend = resolve
  })
  const backendReleased = new Promise(resolve => {
    releaseBackend = resolve
  })
  let backendCalls = 0
  const fetcher = async request => {
    backendCalls += 1
    announceBackend()
    await backendReleased
    const body = await request.json()
    return Response.json({
      code: 0,
      data: { id: 'q_once', question: body.question, answer: 'one answer' },
    })
  }

  const firstPromise = invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher,
    nowMs,
  })
  await backendEntered

  const overlapping = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher,
    nowMs: nowMs + 1,
  })
  assert.equal(overlapping.response.status, 200)
  assert.equal(overlapping.response.headers.get('retry-after'), '2')
  assert.equal(overlapping.payload.data.status, 'running')
  assert.equal(overlapping.payload.data.completed, 0)
  assert.equal(backendCalls, 1)

  releaseBackend()
  const finished = await firstPromise
  assert.equal(finished.payload.data.status, 'done')
  assert.equal(finished.payload.data.completed, 1)
  assert.equal(backendCalls, 1)
})

test('expired claims recover as terminal failures without replaying a possibly billed query', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  const created = await createBatch(db, ['uncertain outcome'], { nowMs })

  db.run(
    `UPDATE public_batch_items
     SET status = 'running', claim_token = 'stale', claimed_at = ?,
         claim_expires_at = ?, attempt_count = 1
     WHERE batch_id = ?`,
    nowMs - publicBatchConstants.ITEM_CLAIM_TTL_MS,
    nowMs - 1,
    created.batch_id,
  )
  db.run(
    "UPDATE public_batches SET status = 'running' WHERE batch_id = ?",
    created.batch_id,
  )

  let backendCalls = 0
  const recovered = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: async () => {
      backendCalls += 1
      return Response.json({ code: 0, data: {} })
    },
    nowMs,
  })
  assert.equal(recovered.payload.data.status, 'done')
  assert.equal(recovered.payload.data.failed, 1)
  assert.equal(recovered.payload.data.results[0].question, 'uncertain outcome')
  assert.match(recovered.payload.data.results[0].error, /超时/)
  assert.equal(backendCalls, 0)

  await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: async () => {
      backendCalls += 1
      return Response.json({ code: 0, data: {} })
    },
    nowMs: nowMs + 1,
  })
  assert.equal(backendCalls, 0)
})

test('backend failure finalizes the claimed item and validation keeps 1..20 without a text cap', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  const longQuestion = '问题'.repeat(50_000)
  const created = await createBatch(db, [longQuestion], { nowMs })

  let calls = 0
  const failed = await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: async () => {
      calls += 1
      throw new TypeError('simulated network failure')
    },
    nowMs,
  })
  assert.equal(failed.payload.data.status, 'done')
  assert.equal(failed.payload.data.failed, 1)
  assert.equal(failed.payload.data.results[0].question, longQuestion)

  await invoke({
    db,
    path: `/api/v1/query/batch/${created.batch_id}`,
    fetcher: async () => {
      calls += 1
      return Response.json({ code: 0, data: {} })
    },
    nowMs: nowMs + 1,
  })
  assert.equal(calls, 1)

  const empty = await invoke({
    db,
    path: '/api/v1/query/batch',
    method: 'POST',
    body: { questions: [] },
    nowMs,
  })
  assert.equal(empty.response.status, 400)
  assert.equal(empty.payload.code, 1001)

  const tooMany = await invoke({
    db,
    path: '/api/v1/query/batch',
    method: 'POST',
    body: { questions: Array.from({ length: 21 }, (_, index) => `q${index}`) },
    nowMs,
  })
  assert.equal(tooMany.response.status, 400)
  assert.equal(tooMany.payload.code, 1001)

  const zeroLengthQuestion = await invoke({
    db,
    path: '/api/v1/query/batch',
    method: 'POST',
    body: { questions: [''] },
    nowMs,
  })
  assert.equal(
    zeroLengthQuestion.response.status,
    202,
    'the public layer must not introduce a per-question length limit',
  )
})

test('batch TTL cleanup removes expired batch and item rows', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()
  await createBatch(db, ['expires'], {
    nowMs: nowMs - publicBatchConstants.BATCH_TTL_MS - 1,
  })

  const listed = await invoke({
    db,
    path: '/api/v1/query/batch',
    nowMs,
  })
  assert.equal(listed.payload.data.total, 0)
  assert.equal(db.rows('SELECT * FROM public_batch_items').length, 0)
  assert.equal(db.rows('SELECT * FROM public_batches').length, 0)
})

test('opaque malformed path segments never throw during batch route matching', () => {
  assert.deepEqual(
    matchPublicBatchRoute('GET', '/api/v1/query/batch/batch_%zz'),
    { action: 'detail', batchId: 'batch_%zz' },
  )
})

test('generated Drizzle migration applies successfully in its emitted order', t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const migration = readFileSync(
    new URL('../drizzle/0001_parched_the_watchers.sql', import.meta.url),
    'utf8',
  )
  for (const sql of migration.split('--> statement-breakpoint')) {
    if (sql.trim()) db.database.exec(sql)
  }
  assert.equal(
    db.rows(
      "SELECT COUNT(*) AS total FROM sqlite_master WHERE type = 'table' AND name IN ('public_batches', 'public_batch_items')",
    )[0].total,
    2,
  )
})
