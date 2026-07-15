import {
  acquireBatchPollLease,
  consumeRateLimit,
  maybeCleanupSecurityState,
  releaseBatchPollLease,
  type RateLimitPolicy,
} from '../db/d1'
import {
  buildBackendHeaders,
  classifyApiRequest,
  getOrCreateVisitor,
  serializeVisitorCookie,
  rateLimitIdentityHashes,
} from './security.mjs'
import {
  handlePublicBatchRequest,
  matchPublicBatchRoute,
} from './public-batches.mjs'

interface Env {
  ASSETS: Fetcher
  DB?: D1Database
  BACKEND_ORIGIN?: string
  BACKEND_PROXY_SECRET?: string
}

const SITE_ORIGIN_PLACEHOLDER = '__SITE_ORIGIN__'

const RATE_LIMITS: Record<string, RateLimitPolicy> = {
  qa: { hourly: 8, daily: 24 },
  'batch-create': { hourly: 2, daily: 6 },
  'batch-poll': { hourly: 120, daily: 500 },
}

function jsonError(
  status: number,
  msg: string,
  headers: HeadersInit = {},
): Response {
  const responseHeaders = new Headers(headers)
  responseHeaders.set('Cache-Control', 'no-store')

  const requestId = crypto.randomUUID()
  responseHeaders.set('X-Request-ID', requestId)

  return Response.json(
    { code: status, msg, request_id: requestId, data: null },
    { status, headers: responseHeaders },
  )
}

function withVisitorCookie(
  response: Response,
  visitor: { id: string; isNew: boolean },
  requestUrl: URL,
): Response {
  if (!visitor.isNew) return response

  const headers = new Headers(response.headers)
  headers.append(
    'Set-Cookie',
    serializeVisitorCookie(visitor.id, requestUrl.protocol === 'https:'),
  )
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

function sanitizeBackendResponse(response: Response): Response {
  const headers = new Headers(response.headers)
  headers.delete('set-cookie')
  headers.delete('access-control-allow-credentials')
  headers.delete('access-control-allow-headers')
  headers.delete('access-control-allow-methods')
  headers.delete('access-control-allow-origin')
  headers.delete('access-control-expose-headers')
  headers.delete('access-control-max-age')
  headers.set('Cache-Control', 'no-store')

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

async function proxyApi(
  request: Request,
  backendOrigin: string,
  visitorId: string,
  proxySecret: string,
): Promise<Response> {
  const incomingUrl = new URL(request.url)
  const targetUrl = new URL(
    `${incomingUrl.pathname}${incomingUrl.search}`,
    backendOrigin.endsWith('/') ? backendOrigin : `${backendOrigin}/`,
  )
  const forwarded = new Request(targetUrl, request)
  const requestId = request.headers.get('X-Request-ID') || crypto.randomUUID()
  const headers = buildBackendHeaders(
    request.headers,
    visitorId,
    proxySecret,
    requestId,
  )

  const response = sanitizeBackendResponse(
    await fetch(new Request(forwarded, { headers, redirect: 'manual' })),
  )
  const responseHeaders = new Headers(response.headers)
  responseHeaders.set('X-Request-ID', requestId)
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  })
}

async function enforcePaidRouteProtection(
  request: Request,
  env: Env,
  visitorId: string,
  paidScope: string,
  batchId?: string,
): Promise<{
  response: Response | null
  lease?: { batchId: string; ownerHash: string }
}> {
  if (!env.DB) return { response: jsonError(503, '在线费用保护服务尚未配置') }

  const policy = RATE_LIMITS[paidScope]
  if (!policy) return { response: jsonError(503, '在线费用保护策略尚未配置') }

  let lease: { batchId: string; ownerHash: string } | undefined
  try {
    await maybeCleanupSecurityState(env.DB)
    const identities = await rateLimitIdentityHashes(
      request,
      visitorId,
      env.BACKEND_PROXY_SECRET ?? '',
    )

    if (paidScope === 'batch-poll' && batchId) {
      // Namespace the short outer lease by the keyed visitor identity. A
      // guessed batch id from another visitor cannot block its real owner.
      const leaseKey = `${batchId}:${identities.visitor}`
      const acquired = await acquireBatchPollLease(
        env.DB,
        leaseKey,
        identities.combined,
      )
      if (!acquired.acquired) {
        return {
          response: jsonError(409, '该批量任务正在更新，请稍后重试', {
            'Retry-After': '2',
          }),
        }
      }
      lease = { batchId: leaseKey, ownerHash: identities.combined }
    }

    const dimensions = [
      { hash: identities.visitor, policy },
      { hash: identities.combined, policy },
      {
        hash: identities.ip,
        policy: { hourly: policy.hourly * 4, daily: policy.daily * 4 },
      },
    ]
    const rates = []
    for (const dimension of dimensions) {
      rates.push(
        await consumeRateLimit(
          env.DB,
          dimension.hash,
          paidScope,
          dimension.policy,
        ),
      )
    }
    const blockedRates = rates.filter(rate => !rate.allowed)

    if (blockedRates.length > 0) {
      if (lease) {
        await releaseBatchPollLease(env.DB, lease.batchId, lease.ownerHash)
        lease = undefined
      }
      const retryAfterSeconds = Math.max(
        ...blockedRates.map(rate => rate.retryAfterSeconds),
      )
      return {
        response: jsonError(429, '请求过于频繁，请稍后再试', {
          'Retry-After': String(retryAfterSeconds),
          'X-RateLimit-Remaining': '0',
        }),
      }
    }

    return { response: null, lease }
  } catch (error) {
    console.error('Public demo protection failed', error)
    if (lease) {
      try {
        await releaseBatchPollLease(env.DB, lease.batchId, lease.ownerHash)
      } catch (releaseError) {
        console.error('Batch poll lease release failed', releaseError)
      }
    }
    return { response: jsonError(503, '在线费用保护服务暂不可用') }
  }
}

async function serveSite(request: Request, assets: Fetcher): Promise<Response> {
  let response = await assets.fetch(request)

  // Sites serves the static bundle through ASSETS, but direct SPA routes do
  // not always inherit Wrangler's not_found_handling setting. Fall back to
  // the application shell explicitly for extensionless GET/HEAD requests.
  const url = new URL(request.url)
  const lastSegment = url.pathname.split('/').pop() ?? ''
  const isSpaNavigation =
    (request.method === 'GET' || request.method === 'HEAD') &&
    !lastSegment.includes('.')

  if (response.status === 404 && isSpaNavigation) {
    const indexUrl = new URL('/index.html', request.url)
    response = await assets.fetch(
      new Request(indexUrl, {
        method: request.method,
        headers: request.headers,
      }),
    )
  }

  const contentType = response.headers.get('content-type') ?? ''

  if (!contentType.includes('text/html')) return response

  const origin = new URL(request.url).origin
  const html = (await response.text()).replaceAll(SITE_ORIGIN_PLACEHOLDER, origin)
  const headers = new Headers(response.headers)
  headers.delete('content-length')

  return new Response(html, {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url)
    const visitor = getOrCreateVisitor(request.headers)
    let response: Response

    if (url.pathname.startsWith('/api/')) {
      const decision = classifyApiRequest(request.method, url.pathname)
      const publicBatchRoute = matchPublicBatchRoute(
        request.method,
        url.pathname,
      )

      if (decision.action === 'forbidden') {
        response = jsonError(403, decision.reason)
        return withVisitorCookie(response, visitor, url)
      }

      if (decision.action !== 'allow') {
        response = jsonError(404, '该 API 在公开演示中不可用')
        return withVisitorCookie(response, visitor, url)
      }

      if (!env.BACKEND_ORIGIN || !env.BACKEND_PROXY_SECRET) {
        response = jsonError(503, '在线后端尚未安全配置')
        return withVisitorCookie(response, visitor, url)
      }

      let lease: { batchId: string; ownerHash: string } | undefined
      if (decision.paidScope) {
        const protection = await enforcePaidRouteProtection(
          request,
          env,
          visitor.id,
          decision.paidScope,
          decision.batchId,
        )
        if (protection.response) {
          return withVisitorCookie(protection.response, visitor, url)
        }
        lease = protection.lease
      }

      try {
        if (publicBatchRoute) {
          if (!env.DB) {
            response = jsonError(503, '在线批量任务服务尚未配置')
          } else {
            response =
              (await handlePublicBatchRequest({
                request,
                db: env.DB,
                visitorId: visitor.id,
                backendOrigin: env.BACKEND_ORIGIN,
                proxySecret: env.BACKEND_PROXY_SECRET,
                schedule: promise => ctx.waitUntil(promise),
              })) ?? jsonError(404, '该 API 在公开演示中不可用')
          }
        } else {
          response = await proxyApi(
            request,
            env.BACKEND_ORIGIN,
            visitor.id,
            env.BACKEND_PROXY_SECRET,
          )
        }
      } catch (error) {
        console.error('Backend proxy failed', error)
        response = jsonError(502, '在线后端暂时不可用')
      } finally {
        if (lease && env.DB) {
          try {
            await releaseBatchPollLease(env.DB, lease.batchId, lease.ownerHash)
          } catch (releaseError) {
            console.error('Batch poll lease release failed', releaseError)
          }
        }
      }

      return withVisitorCookie(response, visitor, url)
    }

    response = await serveSite(request, env.ASSETS)
    return withVisitorCookie(response, visitor, url)
  },
}
