import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildBackendHeaders,
  classifyApiRequest,
  getOrCreateVisitor,
  rateLimitIdentityHashes,
  serializeVisitorCookie,
} from '../worker/security.mjs'

test('public demo allowlist keeps read surfaces, QA, sessions, and every batch route', () => {
  const cases = [
    ['GET', '/api/v1/documents'],
    ['GET', '/api/v1/documents/doc-1/index-result'],
    ['GET', '/api/v1/index/status/job-1'],
    ['GET', '/api/v1/kg/nodes/node-1/neighbors'],
    ['GET', '/api/v1/search/graph'],
    ['GET', '/api/v1/health/live'],
    ['GET', '/api/v1/system/stats'],
    ['POST', '/api/v1/query'],
    ['POST', '/api/v1/query/stream'],
    ['GET', '/api/v1/query/history'],
    ['POST', '/api/v1/query/sessions'],
    ['GET', '/api/v1/query/sessions/session-1'],
    ['POST', '/api/v1/query/batch'],
    ['GET', '/api/v1/query/batch'],
    ['GET', '/api/v1/query/batch/batch-1'],
    ['DELETE', '/api/v1/query/batch/batch-1'],
    ['GET', '/api/v1/account/me'],
    ['GET', '/api/v1/account/usage'],
    ['GET', '/api/v1/account/export'],
    ['DELETE', '/api/v1/account/data'],
    ['DELETE', '/api/v1/account/tenant-data'],
    ['POST', '/api/v1/account/claim-visitor-data'],
    ['GET', '/api/v1/ops/summary'],
  ]

  for (const [method, pathname] of cases) {
    assert.equal(
      classifyApiRequest(method, pathname).action,
      'allow',
      `${method} ${pathname}`,
    )
  }

  assert.equal(classifyApiRequest('POST', '/api/v1/query').paidScope, 'qa')
  assert.equal(classifyApiRequest('POST', '/api/v1/query/batch').paidScope, 'batch-create')
  assert.deepEqual(classifyApiRequest('GET', '/api/v1/query/batch/batch-1'), {
    action: 'allow',
    paidScope: 'batch-poll',
    batchId: 'batch-1',
  })
})

test('upload token exchange and owned index actions are available', () => {
  const cases = [
    ['POST', '/api/v1/documents/upload/direct'],
    ['POST', '/api/v1/index/start'],
    ['DELETE', '/api/v1/index/jobs/job-1'],
  ]

  for (const [method, pathname] of cases) {
    assert.equal(classifyApiRequest(method, pathname).action, 'allow', `${method} ${pathname}`)
  }
})

test('other document mutations remain explicit 403 decisions', () => {
  const cases = [
    ['POST', '/api/v1/documents/upload'],
    ['DELETE', '/api/v1/documents/doc-1'],
    ['PATCH', '/api/v1/documents/doc-1'],
  ]

  for (const [method, pathname] of cases) {
    assert.equal(
      classifyApiRequest(method, pathname).action,
      'forbidden',
      `${method} ${pathname}`,
    )
  }
})

test('unlisted API endpoints are denied by default', () => {
  assert.equal(classifyApiRequest('GET', '/api/v1/system/demo').action, 'deny')
  assert.equal(classifyApiRequest('PUT', '/api/v1/query').action, 'deny')
  assert.equal(classifyApiRequest('OPTIONS', '/api/v1/query').action, 'deny')
  assert.equal(classifyApiRequest('POST', '/api/v1/ops/events').action, 'deny')
  assert.equal(classifyApiRequest('POST', '/api/v1/account/me').action, 'deny')
})

test('visitor cookie is a raw canonical lowercase UUID and uses safe attributes', () => {
  const created = getOrCreateVisitor(new Headers())
  assert.match(
    created.id,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  )
  assert.equal(created.isNew, true)

  const existing = getOrCreateVisitor(
    new Headers({ cookie: `other=x; graphrag_visitor=${created.id}` }),
  )
  assert.deepEqual(existing, { id: created.id, isNew: false })

  const replaced = getOrCreateVisitor(
    new Headers({ cookie: `graphrag_visitor=${created.id.toUpperCase()}` }),
  )
  assert.equal(replaced.isNew, true)
  assert.notEqual(replaced.id, created.id.toUpperCase())

  const serialized = serializeVisitorCookie(created.id, true)
  assert.match(serialized, /^graphrag_visitor=[0-9a-f-]+;/)
  assert.match(serialized, /HttpOnly/)
  assert.match(serialized, /SameSite=Lax/)
  assert.match(serialized, /Secure/)
})

test('canonical client visitor fallback is stable while a valid cookie keeps priority', () => {
  const clientVisitor = '123e4567-e89b-42d3-a456-426614174000'
  const cookieVisitor = '223e4567-e89b-42d3-a456-426614174001'

  assert.deepEqual(getOrCreateVisitor(new Headers(), clientVisitor), {
    id: clientVisitor,
    isNew: true,
  })
  assert.deepEqual(
    getOrCreateVisitor(
      new Headers({ cookie: `graphrag_visitor=${cookieVisitor}` }),
      clientVisitor,
    ),
    { id: cookieVisitor, isNew: false },
  )

  const rejected = getOrCreateVisitor(new Headers(), 'not-a-uuid')
  assert.notEqual(rejected.id, 'not-a-uuid')
  assert.match(rejected.id, /^[0-9a-f-]{36}$/)
})

test('backend headers drop caller identity and overwrite trusted proxy headers', () => {
  const incoming = new Headers({
    Authorization: 'Bearer attacker',
    Cookie: 'session=attacker',
    'OAI-User-ID': 'attacker',
    'X-Forwarded-User': 'attacker',
    'X-GraphRAG-Visitor-ID': 'attacker',
    'X-GraphRAG-Client-Visitor-ID': 'attacker-client',
    'X-GraphRAG-Proxy-Secret': 'attacker',
    Host: 'attacker.example',
    Connection: 'keep-alive',
    Forwarded: 'for=attacker',
    'CF-Connecting-IP': '203.0.113.9',
    'Content-Length': '999',
    'Content-Type': 'application/json',
  })
  const visitorId = '123e4567-e89b-42d3-a456-426614174000'
  const headers = buildBackendHeaders(incoming, visitorId, 'server-secret')

  assert.equal(headers.get('authorization'), null)
  assert.equal(headers.get('cookie'), null)
  assert.equal(headers.get('oai-user-id'), null)
  assert.equal(headers.get('x-forwarded-user'), null)
  assert.equal(headers.get('host'), null)
  assert.equal(headers.get('connection'), null)
  assert.equal(headers.get('forwarded'), null)
  assert.equal(headers.get('cf-connecting-ip'), null)
  assert.equal(headers.get('content-length'), null)
  assert.equal(headers.get('x-graphrag-client-visitor-id'), null)
  assert.equal(headers.get('content-type'), 'application/json')
  assert.equal(headers.get('x-graphrag-visitor-id'), visitorId)
  assert.equal(headers.get('x-graphrag-proxy-secret'), 'server-secret')
})

test('backend headers forward only the explicitly supplied bearer session', () => {
  const incoming = new Headers({ Authorization: 'Bearer attacker' })
  const visitorId = '123e4567-e89b-42d3-a456-426614174000'

  const stripped = buildBackendHeaders(incoming, visitorId, 'server-secret', 'req-1')
  assert.equal(stripped.get('authorization'), null)

  const forwarded = buildBackendHeaders(
    incoming,
    visitorId,
    'server-secret',
    'req-2',
    'Bearer clerk-signed-session',
  )
  assert.equal(forwarded.get('authorization'), 'Bearer clerk-signed-session')
  assert.equal(forwarded.get('x-request-id'), 'req-2')

  const invalid = buildBackendHeaders(
    incoming,
    visitorId,
    'server-secret',
    'req-3',
    'Basic not-allowed',
  )
  assert.equal(invalid.get('authorization'), null)
})

test('Sites resolves one visitor across its Vercel double-proxy hop', () => {
  const sitesVisitor = '123e4567-e89b-42d3-a456-426614174000'
  const spoofedVisitor = '223e4567-e89b-42d3-a456-426614174001'
  const incoming = new Headers({
    Cookie: `graphrag_visitor=${sitesVisitor}`,
    'X-GraphRAG-Client-Visitor-ID': spoofedVisitor,
    'X-GraphRAG-Visitor-ID': spoofedVisitor,
    'X-GraphRAG-Proxy-Secret': 'attacker-secret',
  })

  // A durable Sites cookie wins over the browser fallback. The first proxy
  // then removes every caller-supplied identity before rebuilding the hop.
  const resolvedAtSites = getOrCreateVisitor(
    incoming,
    incoming.get('X-GraphRAG-Client-Visitor-ID'),
  )
  assert.deepEqual(resolvedAtSites, { id: sitesVisitor, isNew: false })

  const sitesToVercel = buildBackendHeaders(
    incoming,
    resolvedAtSites.id,
    'sites-secret',
    'sites-request',
  )
  assert.equal(sitesToVercel.get('x-graphrag-client-visitor-id'), null)
  assert.equal(sitesToVercel.get('x-graphrag-visitor-id'), sitesVisitor)
  assert.equal(sitesToVercel.get('x-graphrag-proxy-secret'), 'sites-secret')

  // proxyApi explicitly adds this trusted fallback after sanitization. The
  // Vercel perimeter therefore reuses the Sites owner instead of minting a
  // second anonymous visitor, then strips the fallback before FastAPI.
  sitesToVercel.set('X-GraphRAG-Client-Visitor-ID', resolvedAtSites.id)
  const resolvedAtVercel = getOrCreateVisitor(
    sitesToVercel,
    sitesToVercel.get('X-GraphRAG-Client-Visitor-ID'),
  )
  assert.equal(resolvedAtVercel.id, sitesVisitor)

  const vercelToBackend = buildBackendHeaders(
    sitesToVercel,
    resolvedAtVercel.id,
    'vercel-secret',
    'vercel-request',
  )
  assert.equal(vercelToBackend.get('x-graphrag-client-visitor-id'), null)
  assert.equal(vercelToBackend.get('x-graphrag-visitor-id'), sitesVisitor)
  assert.equal(vercelToBackend.get('x-graphrag-proxy-secret'), 'vercel-secret')
})

test('rate-limit identities are stable and split visitor, IP, and combined dimensions', async () => {
  const visitorId = '123e4567-e89b-42d3-a456-426614174000'
  const request = new Request('https://example.com/api/v1/query', {
    headers: { 'CF-Connecting-IP': '203.0.113.7' },
  })
  const first = await rateLimitIdentityHashes(request, visitorId, 'secret-a')
  const second = await rateLimitIdentityHashes(request, visitorId, 'secret-a')
  const rotated = await rateLimitIdentityHashes(request, visitorId, 'secret-b')

  assert.deepEqual(first, second)
  assert.notDeepEqual(first, rotated)
  assert.equal(new Set(Object.values(first)).size, 3)
  for (const value of Object.values(first)) assert.match(value, /^[0-9a-f]{64}$/)
})
