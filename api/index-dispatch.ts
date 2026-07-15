import { timingSafeEqual } from 'node:crypto'

import { waitUntil } from '@vercel/functions'

function secureEqual(left: string, right: string): boolean {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

export async function POST(request: Request): Promise<Response> {
  if (request.method !== 'POST') {
    return Response.json({ code: 405, msg: 'Method not allowed', data: null }, { status: 405 })
  }

  const expected = process.env.BACKEND_PROXY_SECRET?.trim() ?? ''
  const supplied = request.headers.get('x-graphrag-proxy-secret')?.trim() ?? ''
  if (!expected || !supplied || !secureEqual(expected, supplied)) {
    return Response.json({ code: 401, msg: 'Unauthorized', data: null }, { status: 401 })
  }

  const origin = new URL(request.url).origin
  waitUntil(
    fetch(`${origin}/api/v1/index/run-next`, {
      method: 'POST',
      headers: {
        'X-GraphRAG-Proxy-Secret': expected,
        'X-GraphRAG-Internal-Index': '1',
        'X-Request-ID': request.headers.get('x-request-id') || crypto.randomUUID(),
      },
    }).then(async response => {
      if (!response.ok) throw new Error(`Index worker returned HTTP ${response.status}`)
      await response.arrayBuffer()
    }),
  )

  return Response.json({ code: 0, msg: 'accepted', data: { accepted: true } }, { status: 202 })
}
