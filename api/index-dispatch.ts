import { timingSafeEqual } from 'node:crypto'

import { waitUntil } from '@vercel/functions'

function secureEqual(left: string, right: string): boolean {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

function isAuthorized(request: Request): boolean {
  const proxySecret = process.env.BACKEND_PROXY_SECRET?.trim() ?? ''
  const suppliedProxy = request.headers.get('x-graphrag-proxy-secret')?.trim() ?? ''
  if (proxySecret && suppliedProxy && secureEqual(proxySecret, suppliedProxy)) return true

  const dispatchSecret = process.env.INDEX_DISPATCH_SECRET?.trim() ?? ''
  const authorization = request.headers.get('authorization')?.trim() ?? ''
  const suppliedBearer = authorization.toLowerCase().startsWith('bearer ')
    ? authorization.slice(7).trim()
    : ''
  return Boolean(dispatchSecret && suppliedBearer && secureEqual(dispatchSecret, suppliedBearer))
}

function log(event: string, fields: Record<string, unknown>): void {
  console.log(JSON.stringify({ level: 'info', event, source: 'index_dispatch', ...fields }))
}

export async function POST(request: Request): Promise<Response> {
  if (request.method !== 'POST') {
    return Response.json({ code: 405, msg: 'Method not allowed', data: null }, { status: 405 })
  }

  if (!isAuthorized(request)) {
    return Response.json({ code: 401, msg: 'Unauthorized', data: null }, { status: 401 })
  }

  const expected = process.env.BACKEND_PROXY_SECRET?.trim() ?? ''
  if (!expected) {
    return Response.json({ code: 503, msg: 'Backend proxy is not configured', data: null }, { status: 503 })
  }

  const origin = new URL(request.url).origin
  const requestId = request.headers.get('x-request-id') || crypto.randomUUID()
  log('index_dispatch_accepted', { request_id: requestId })
  waitUntil(
    fetch(`${origin}/api/v1/index/run-next`, {
      method: 'POST',
      headers: {
        'X-GraphRAG-Proxy-Secret': expected,
        'X-GraphRAG-Internal-Index': '1',
        'X-Request-ID': requestId,
      },
    }).then(async response => {
      if (!response.ok) throw new Error(`Index worker returned HTTP ${response.status}`)
      await response.arrayBuffer()
      log('index_dispatch_completed', { request_id: requestId, status: response.status })
    }).catch(error => {
      console.error(JSON.stringify({
        level: 'error',
        event: 'index_dispatch_failed',
        source: 'index_dispatch',
        request_id: requestId,
        error_type: error instanceof Error ? error.name : 'UnknownError',
      }))
    }),
  )

  return Response.json({ code: 0, msg: 'accepted', data: { accepted: true } }, { status: 202 })
}
