export const RATE_LIMIT_POLICIES = Object.freeze({
  qa: Object.freeze({ hourly: 8, daily: 24 }),
  'batch-create': Object.freeze({ hourly: 2, daily: 6 }),
  'batch-poll': Object.freeze({ hourly: 120, daily: 500 }),
})

export const BATCH_POLL_LEASE_MS = 1_500
export const INDEX_RECOVERY_LEASE_MS = 30_000

export function bearerToken(headers) {
  const authorization = headers.get('authorization')?.trim() ?? ''
  return authorization.toLowerCase().startsWith('bearer ')
    ? authorization.slice(7).trim()
    : ''
}

export function jsonError(status, msg, requestId, extraHeaders = {}) {
  const headers = new Headers(extraHeaders)
  headers.set('Cache-Control', 'no-store')
  headers.set('Content-Type', 'application/json; charset=utf-8')
  headers.set('X-Request-ID', requestId)
  return new Response(
    JSON.stringify({ code: status, msg, request_id: requestId, data: null }),
    { status, headers },
  )
}

export function trustedInternalRoute(method, pathname, headers, secretMatches) {
  if (!secretMatches || method.toUpperCase() !== 'POST') return false

  if (pathname === '/api/index-dispatch') return true
  if (pathname === '/api/v1/ops/events') return true
  if (pathname === '/api/v1/index/run-next') {
    return headers.get('x-graphrag-internal-index') === '1'
  }
  if (pathname === '/api/v1/documents/upload/complete') {
    return headers.get('x-graphrag-internal-upload') === '1'
  }
  return false
}

export function responseHeaders(visitor, requestUrl, requestId) {
  const headers = new Headers({
    'Cache-Control': 'no-store',
    'X-Request-ID': requestId,
  })
  if (visitor.isNew) {
    const secure = new URL(requestUrl).protocol === 'https:'
    const attributes = [
      `graphrag_visitor=${visitor.id}`,
      'Path=/',
      'Max-Age=31536000',
      'HttpOnly',
      'SameSite=Lax',
    ]
    if (secure) attributes.push('Secure')
    headers.append('Set-Cookie', attributes.join('; '))
  }
  return headers
}

export function restoreRequestBodyLength(method, incomingHeaders, forwardedHeaders) {
  const normalizedMethod = method.toUpperCase()
  if (normalizedMethod === 'GET' || normalizedMethod === 'HEAD') return forwardedHeaders

  const contentLength = incomingHeaders.get('content-length')?.trim() ?? ''
  if (/^\d+$/.test(contentLength)) {
    forwardedHeaders.set('Content-Length', contentLength)
  }
  return forwardedHeaders
}

export function rateLimitDimensions(identities, policy) {
  return [
    { id: `visitor:${identities.visitor}`, hourly: policy.hourly, daily: policy.daily },
    { id: `visitor-ip:${identities.combined}`, hourly: policy.hourly, daily: policy.daily },
    { id: `ip:${identities.ip}`, hourly: policy.hourly * 4, daily: policy.daily * 4 },
  ]
}
