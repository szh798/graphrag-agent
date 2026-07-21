import { next, waitUntil } from '@vercel/functions'
import { Ratelimit } from '@upstash/ratelimit'
import { Redis } from '@upstash/redis'

import {
  buildBackendHeaders,
  classifyApiRequest,
  getOrCreateVisitor,
  rateLimitIdentityHashes,
} from './frontend/worker/security.mjs'
import {
  BATCH_POLL_LEASE_MS,
  INDEX_RECOVERY_LEASE_MS,
  RATE_LIMIT_POLICIES,
  bearerToken,
  jsonError,
  rateLimitDimensions,
  restoreRequestBodyLength,
  responseHeaders,
  trustedInternalRoute,
} from './vercel-perimeter.mjs'

type PaidScope = 'qa' | 'batch-create' | 'batch-poll'

let redisClient: Redis | null | undefined
const limiters = new Map<string, Ratelimit>()

function redis(): Redis | null {
  if (redisClient !== undefined) return redisClient
  const url = (process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL || '').trim()
  const token = (process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN || '').trim()
  redisClient = url && token ? new Redis({ url, token }) : null
  return redisClient
}

function limiter(limit: number, duration: '1 h' | '1 d', suffix: string): Ratelimit {
  const key = `${limit}:${duration}:${suffix}`
  const existing = limiters.get(key)
  if (existing) return existing
  const client = redis()
  if (!client) throw new Error('Rate-limit Redis is not configured')
  const created = new Ratelimit({
    redis: client,
    limiter: Ratelimit.fixedWindow(limit, duration),
    prefix: `graphrag:public:${suffix}`,
  })
  limiters.set(key, created)
  return created
}

function attachResponseHeaders(response: Response, headers: Headers): Response {
  for (const [name, value] of headers) {
    if (name.toLowerCase() === 'set-cookie') response.headers.append(name, value)
    else response.headers.set(name, value)
  }
  return response
}

async function secretValuesMatch(supplied: string, expected: string): Promise<boolean> {
  if (!expected || !supplied || supplied.length !== expected.length) return false
  const encoder = new TextEncoder()
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(supplied)),
    crypto.subtle.digest('SHA-256', encoder.encode(expected)),
  ])
  const a = new Uint8Array(left)
  const b = new Uint8Array(right)
  let difference = 0
  for (let index = 0; index < a.length; index += 1) difference |= a[index] ^ b[index]
  return difference === 0
}

async function internalRequestIsTrusted(
  request: Request,
  pathname: string,
  proxySecret: string,
): Promise<boolean> {
  const suppliedProxy = request.headers.get('x-graphrag-proxy-secret')?.trim() ?? ''
  if (await secretValuesMatch(suppliedProxy, proxySecret)) return true

  if (request.method.toUpperCase() !== 'POST' || pathname !== '/api/index-dispatch') {
    return false
  }

  const dispatchSecret = process.env.INDEX_DISPATCH_SECRET?.trim() ?? ''
  return secretValuesMatch(bearerToken(request.headers), dispatchSecret)
}

async function enforcePaidRoute(
  request: Request,
  visitorId: string,
  proxySecret: string,
  scope: PaidScope,
  batchId?: string,
): Promise<Response | null> {
  const client = redis()
  if (!client) return jsonError(503, '在线费用保护服务尚未配置', crypto.randomUUID())

  const identities = await rateLimitIdentityHashes(request, visitorId, proxySecret)
  if (scope === 'batch-poll' && batchId) {
    const acquired = await client.set(
      `graphrag:batch-poll:${batchId}:${identities.visitor}`,
      identities.combined,
      { nx: true, px: BATCH_POLL_LEASE_MS },
    )
    if (acquired !== 'OK') {
      return jsonError(409, '该批量任务正在更新，请稍后重试', crypto.randomUUID(), {
        'Retry-After': '2',
      })
    }
  }

  const policy = RATE_LIMIT_POLICIES[scope]
  const attempts = await Promise.all(
    rateLimitDimensions(identities, policy).flatMap(dimension => [
      limiter(dimension.hourly, '1 h', `${scope}:hour`).limit(dimension.id),
      limiter(dimension.daily, '1 d', `${scope}:day`).limit(dimension.id),
    ]),
  )
  const blocked = attempts.filter(result => !result.success)
  if (blocked.length === 0) return null

  const resetAt = Math.max(...blocked.map(result => result.reset))
  const retryAfter = Math.max(1, Math.ceil((resetAt - Date.now()) / 1000))
  return jsonError(429, '请求过于频繁，请稍后再试', crypto.randomUUID(), {
    'Retry-After': String(retryAfter),
    'X-RateLimit-Remaining': '0',
  })
}

function delayedDispatch(request: Request, proxySecret: string, visitorId: string, requestId: string) {
  const dispatch = async () => {
    await new Promise(resolve => setTimeout(resolve, 500))
    const response = await fetch(new URL('/api/index-dispatch', request.url), {
      method: 'POST',
      headers: {
        'X-GraphRAG-Proxy-Secret': proxySecret,
        'X-GraphRAG-Visitor-ID': visitorId,
        'X-Request-ID': requestId,
      },
    })
    await response.arrayBuffer()
    if (!response.ok) throw new Error(`Index dispatch returned HTTP ${response.status}`)
  }
  waitUntil(dispatch().catch(error => console.error('Index dispatch failed', error)))
}

async function recoveryDispatch(request: Request, proxySecret: string, visitorId: string, requestId: string) {
  const client = redis()
  if (!client) return
  const acquired = await client.set(
    'graphrag:index-recovery-dispatch',
    requestId,
    { nx: true, px: INDEX_RECOVERY_LEASE_MS },
  )
  if (acquired === 'OK') delayedDispatch(request, proxySecret, visitorId, requestId)
}

export default async function middleware(request: Request): Promise<Response> {
  const url = new URL(request.url)
  // Some reverse-proxy paths keep the browser Cookie at the edge but omit it
  // before Vercel Routing Middleware. The SPA therefore supplies a stable,
  // canonical UUID candidate. A valid durable Cookie still wins, and the
  // candidate is stripped before trusted backend headers are rebuilt.
  const visitor = getOrCreateVisitor(
    request.headers,
    request.headers.get('x-graphrag-client-visitor-id') ?? '',
  )
  const requestId = request.headers.get('x-request-id') || crypto.randomUUID()
  const outgoingResponseHeaders = responseHeaders(visitor, request.url, requestId)
  const proxySecret = process.env.BACKEND_PROXY_SECRET?.trim() ?? ''

  if (!proxySecret) {
    const response = jsonError(503, '在线后端尚未安全配置', requestId)
    return attachResponseHeaders(response, outgoingResponseHeaders)
  }

  const isTrusted = await internalRequestIsTrusted(request, url.pathname, proxySecret)
  const isInternal = trustedInternalRoute(
    request.method,
    url.pathname,
    request.headers,
    isTrusted,
  )

  if (!isInternal) {
    const decision = classifyApiRequest(request.method, url.pathname)
    if (decision.action === 'forbidden') {
      const response = jsonError(403, decision.reason, requestId)
      return attachResponseHeaders(response, outgoingResponseHeaders)
    }
    if (decision.action !== 'allow') {
      const response = jsonError(404, '该 API 在公开演示中不可用', requestId)
      return attachResponseHeaders(response, outgoingResponseHeaders)
    }
    if (decision.paidScope) {
      try {
        const blocked = await enforcePaidRoute(
          request,
          visitor.id,
          proxySecret,
          decision.paidScope as PaidScope,
          decision.batchId,
        )
        if (blocked) return attachResponseHeaders(blocked, outgoingResponseHeaders)
      } catch (error) {
        console.error('Public protection failed', error)
        const response = jsonError(503, '在线费用保护服务暂不可用', requestId)
        return attachResponseHeaders(response, outgoingResponseHeaders)
      }
    }
  }

  const forwardedHeaders = buildBackendHeaders(
    request.headers,
    visitor.id,
    proxySecret,
    requestId,
    request.headers.get('authorization'),
  )
  // buildBackendHeaders intentionally strips hop-by-hop framing headers for
  // Cloudflare fetch proxies. Vercel's `next()` keeps the original request
  // body, so it also needs the original length or the downstream Web Handler
  // receives an empty stream.
  restoreRequestBodyLength(request.method, request.headers, forwardedHeaders)
  if (isInternal) {
    if (request.headers.get('x-graphrag-internal-index') === '1') {
      forwardedHeaders.set('X-GraphRAG-Internal-Index', '1')
    }
    if (request.headers.get('x-graphrag-internal-upload') === '1') {
      forwardedHeaders.set('X-GraphRAG-Internal-Upload', '1')
    }
  }

  if (request.method === 'POST' && url.pathname === '/api/v1/index/start') {
    delayedDispatch(request, proxySecret, visitor.id, requestId)
  } else if (
    request.method === 'GET' &&
    /^\/api\/v1\/index\/status\/[^/]+$/.test(url.pathname)
  ) {
    await recoveryDispatch(request, proxySecret, visitor.id, requestId)
  }

  return next({
    headers: outgoingResponseHeaders,
    request: { headers: forwardedHeaders },
  })
}

export const config = {
  matcher: '/api/:path*',
}
