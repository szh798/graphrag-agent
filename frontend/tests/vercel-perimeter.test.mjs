import assert from 'node:assert/strict'
import test from 'node:test'

import { next } from '@vercel/functions'

import {
  RATE_LIMIT_POLICIES,
  bearerToken,
  jsonError,
  rateLimitDimensions,
  restoreRequestBodyLength,
  responseHeaders,
  trustedInternalRoute,
} from '../../vercel-perimeter.mjs'

test('Vercel perimeter keeps the existing public paid-route quotas', () => {
  assert.deepEqual(RATE_LIMIT_POLICIES, {
    qa: { hourly: 8, daily: 24 },
    'batch-create': { hourly: 2, daily: 6 },
    'batch-poll': { hourly: 120, daily: 500 },
  })
})

test('only authenticated internal callbacks bypass the public API allowlist', () => {
  assert.equal(
    trustedInternalRoute('POST', '/api/index-dispatch', new Headers(), true),
    true,
  )
  assert.equal(
    trustedInternalRoute('POST', '/api/index-dispatch', new Headers(), false),
    false,
  )
  assert.equal(
    trustedInternalRoute(
      'POST',
      '/api/v1/index/run-next',
      new Headers({ 'X-GraphRAG-Internal-Index': '1' }),
      true,
    ),
    true,
  )
  assert.equal(
    trustedInternalRoute('POST', '/api/v1/index/run-next', new Headers(), true),
    false,
  )
  assert.equal(
    trustedInternalRoute(
      'POST',
      '/api/v1/documents/upload/complete',
      new Headers({ 'X-GraphRAG-Internal-Upload': '1' }),
      true,
    ),
    true,
  )
  assert.equal(
    trustedInternalRoute('GET', '/api/v1/documents/upload/complete', new Headers(), true),
    false,
  )
})

test('scheduler bearer tokens are parsed without widening internal routes', () => {
  const schedulerHeaders = new Headers({ Authorization: 'Bearer scheduler-secret' })

  assert.equal(bearerToken(schedulerHeaders), 'scheduler-secret')
  assert.equal(bearerToken(new Headers({ Authorization: 'Basic scheduler-secret' })), '')
  assert.equal(bearerToken(new Headers()), '')

  assert.equal(
    trustedInternalRoute('POST', '/api/index-dispatch', schedulerHeaders, true),
    true,
  )
  assert.equal(
    trustedInternalRoute('GET', '/api/index-dispatch', schedulerHeaders, true),
    false,
  )
  assert.equal(
    trustedInternalRoute('POST', '/api/v1/index/run-next', schedulerHeaders, true),
    false,
  )
})

test('new Vercel visitors receive the same durable secure cookie', () => {
  const visitor = { id: '123e4567-e89b-42d3-a456-426614174000', isNew: true }
  const headers = responseHeaders(visitor, 'https://admin.seed.atreeagent.com/api/v1/health', 'req-1')
  assert.match(headers.get('set-cookie'), /^graphrag_visitor=/)
  assert.match(headers.get('set-cookie'), /HttpOnly/)
  assert.match(headers.get('set-cookie'), /SameSite=Lax/)
  assert.match(headers.get('set-cookie'), /Secure/)
  assert.equal(headers.get('x-request-id'), 'req-1')
})

test('rate limit dimensions preserve visitor, visitor-IP, and 4x IP policies', () => {
  const dimensions = rateLimitDimensions(
    { visitor: 'v', combined: 'vi', ip: 'i' },
    RATE_LIMIT_POLICIES.qa,
  )
  assert.deepEqual(dimensions, [
    { id: 'visitor:v', hourly: 8, daily: 24 },
    { id: 'visitor-ip:vi', hourly: 8, daily: 24 },
    { id: 'ip:i', hourly: 32, daily: 96 },
  ])
})

test('perimeter errors remain structured and traceable', async () => {
  const response = jsonError(404, '不可用', 'req-2')
  assert.equal(response.status, 404)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  assert.equal(response.headers.get('x-request-id'), 'req-2')
  assert.deepEqual(await response.json(), {
    code: 404,
    msg: '不可用',
    request_id: 'req-2',
    data: null,
  })
})

test('Vercel next preserves body length without widening proxy header forwarding', () => {
  const incoming = new Headers({
    'Content-Length': '247',
    'Transfer-Encoding': 'chunked',
  })
  const postHeaders = new Headers()
  const getHeaders = new Headers()

  restoreRequestBodyLength('POST', incoming, postHeaders)
  restoreRequestBodyLength('GET', incoming, getHeaders)

  assert.equal(postHeaders.get('content-length'), '247')
  assert.equal(postHeaders.has('transfer-encoding'), false)
  assert.equal(getHeaders.has('content-length'), false)

  const nextResponse = next({ request: { headers: postHeaders } })
  assert.match(
    nextResponse.headers.get('x-middleware-override-headers') ?? '',
    /(?:^|,)content-length(?:,|$)/,
  )
  assert.equal(nextResponse.headers.get('x-middleware-request-content-length'), '247')

  const invalidHeaders = new Headers()
  restoreRequestBodyLength(
    'POST',
    new Headers({ 'Content-Length': 'not-a-number' }),
    invalidHeaders,
  )
  assert.equal(invalidHeaders.has('content-length'), false)
})
