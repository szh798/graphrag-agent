export const VISITOR_COOKIE_NAME = 'graphrag_visitor'

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const READ_METHODS = new Set(['GET', 'HEAD'])

const documentReadRoutes = [
  /^\/api\/v1\/documents$/,
  /^\/api\/v1\/documents\/[^/]+$/,
  /^\/api\/v1\/documents\/[^/]+\/index-result$/,
  /^\/api\/v1\/documents\/[^/]+\/extractions$/,
]

const indexReadRoutes = [
  /^\/api\/v1\/index\/status\/[^/]+$/,
  /^\/api\/v1\/index\/result\/[^/]+$/,
]

const kgReadRoutes = [
  /^\/api\/v1\/kg\/nodes$/,
  /^\/api\/v1\/kg\/nodes\/[^/]+$/,
  /^\/api\/v1\/kg\/nodes\/[^/]+\/neighbors$/,
  /^\/api\/v1\/kg\/edges$/,
  /^\/api\/v1\/kg\/stats$/,
  /^\/api\/v1\/kg\/export$/,
]

const searchReadRoutes = [
  /^\/api\/v1\/search\/entities$/,
  /^\/api\/v1\/search\/path$/,
  /^\/api\/v1\/search\/graph$/,
]

const systemReadRoutes = [
  /^\/api\/v1\/health$/,
  /^\/api\/v1\/health\/live$/,
  /^\/api\/v1\/health\/ready$/,
  /^\/api\/v1\/system\/stats$/,
]

const accountRoutes = [
  /^\/api\/v1\/account\/me$/,
  /^\/api\/v1\/account\/usage$/,
  /^\/api\/v1\/account\/export$/,
  /^\/api\/v1\/account\/data$/,
  /^\/api\/v1\/account\/tenant-data$/,
  /^\/api\/v1\/ops\/summary$/,
]

function normalizedPath(pathname) {
  if (pathname.length > 1 && pathname.endsWith('/')) return pathname.slice(0, -1)
  return pathname
}

function matchesAny(pathname, patterns) {
  return patterns.some(pattern => pattern.test(pathname))
}

export function classifyApiRequest(method, rawPathname) {
  const normalizedMethod = method.toUpperCase()
  const pathname = normalizedPath(rawPathname)

  if (matchesAny(pathname, accountRoutes)) {
    if (READ_METHODS.has(normalizedMethod) || normalizedMethod === 'DELETE') {
      return { action: 'allow' }
    }
    return { action: 'deny' }
  }

  if (
    pathname.startsWith('/api/v1/documents') &&
    ['POST', 'PUT', 'PATCH', 'DELETE'].includes(normalizedMethod)
  ) {
    return { action: 'forbidden', reason: '公开演示不允许上传或删除文档' }
  }

  if (
    pathname.startsWith('/api/v1/index') &&
    !READ_METHODS.has(normalizedMethod)
  ) {
    return { action: 'forbidden', reason: '公开演示不允许启动、取消或重试索引' }
  }

  if (
    READ_METHODS.has(normalizedMethod) &&
    matchesAny(pathname, [
      ...documentReadRoutes,
      ...indexReadRoutes,
      ...kgReadRoutes,
      ...searchReadRoutes,
      ...systemReadRoutes,
    ])
  ) {
    return { action: 'allow' }
  }

  if (normalizedMethod === 'POST' && pathname === '/api/v1/query') {
    return { action: 'allow', paidScope: 'qa' }
  }

  if (normalizedMethod === 'POST' && pathname === '/api/v1/query/stream') {
    return { action: 'allow', paidScope: 'qa' }
  }

  if (normalizedMethod === 'GET' && pathname === '/api/v1/query/history') {
    return { action: 'allow' }
  }

  if (
    (normalizedMethod === 'GET' || normalizedMethod === 'POST') &&
    pathname === '/api/v1/query/sessions'
  ) {
    return { action: 'allow' }
  }

  if (
    normalizedMethod === 'GET' &&
    /^\/api\/v1\/query\/sessions\/[^/]+$/.test(pathname)
  ) {
    return { action: 'allow' }
  }

  if (pathname === '/api/v1/query/batch') {
    if (normalizedMethod === 'POST') {
      return { action: 'allow', paidScope: 'batch-create' }
    }
    if (normalizedMethod === 'GET') return { action: 'allow' }
  }

  const batchMatch = pathname.match(/^\/api\/v1\/query\/batch\/([^/]+)$/)
  if (batchMatch) {
    if (normalizedMethod === 'GET') {
      return {
        action: 'allow',
        paidScope: 'batch-poll',
        batchId: batchMatch[1],
      }
    }
    if (normalizedMethod === 'DELETE') return { action: 'allow' }
  }

  return { action: 'deny' }
}

function cookieValue(cookieHeader, name) {
  if (!cookieHeader) return null
  for (const pair of cookieHeader.split(';')) {
    const separator = pair.indexOf('=')
    if (separator === -1) continue
    if (pair.slice(0, separator).trim() === name) {
      return pair.slice(separator + 1).trim()
    }
  }
  return null
}

export function getOrCreateVisitor(headers) {
  const existing = cookieValue(headers.get('cookie'), VISITOR_COOKIE_NAME)
  if (existing && CANONICAL_UUID.test(existing)) {
    return { id: existing, isNew: false }
  }
  return { id: crypto.randomUUID().toLowerCase(), isNew: true }
}

export function serializeVisitorCookie(visitorId, secure) {
  const attributes = [
    `${VISITOR_COOKIE_NAME}=${visitorId}`,
    'Path=/',
    'Max-Age=31536000',
    'HttpOnly',
    'SameSite=Lax',
  ]
  if (secure) attributes.push('Secure')
  return attributes.join('; ')
}

function shouldStripBackendHeader(name) {
  const lower = name.toLowerCase()
  return (
    lower === 'authorization' ||
    lower === 'connection' ||
    lower === 'content-length' ||
    lower === 'forwarded' ||
    lower === 'host' ||
    lower === 'keep-alive' ||
    lower === 'proxy-authenticate' ||
    lower === 'proxy-authorization' ||
    lower === 'proxy-connection' ||
    lower === 'te' ||
    lower === 'trailer' ||
    lower === 'transfer-encoding' ||
    lower === 'upgrade' ||
    lower === 'via' ||
    lower === 'cookie' ||
    lower === 'x-real-ip' ||
    lower.startsWith('cf-') ||
    lower.startsWith('oai-') ||
    lower.startsWith('x-auth-') ||
    lower.startsWith('x-forwarded-') ||
    lower.startsWith('x-graphrag-') ||
    lower.startsWith('x-user-')
  )
}

export function buildBackendHeaders(incomingHeaders, visitorId, proxySecret, requestId, authorization) {
  const headers = new Headers()
  for (const [name, value] of incomingHeaders) {
    if (!shouldStripBackendHeader(name)) headers.append(name, value)
  }
  headers.set('X-GraphRAG-Visitor-ID', visitorId)
  headers.set('X-GraphRAG-Proxy-Secret', proxySecret)
  headers.set('X-GraphRAG-Public-Demo', '1')
  headers.set('X-Request-ID', requestId || crypto.randomUUID())
  if (authorization && /^Bearer\s+\S+$/i.test(authorization)) {
    headers.set('Authorization', authorization)
  }
  return headers
}

async function hmacSha256(secret, value) {
  if (!secret) throw new Error('Rate-limit HMAC secret is required')
  const encoder = new TextEncoder()
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const digest = await crypto.subtle.sign('HMAC', key, encoder.encode(value))
  return [...new Uint8Array(digest)]
    .map(item => item.toString(16).padStart(2, '0'))
    .join('')
}

export async function rateLimitIdentityHashes(request, visitorId, secret) {
  const edgeIp = request.headers.get('cf-connecting-ip') ?? '0.0.0.0'
  const [visitor, ip, combined] = await Promise.all([
    hmacSha256(secret, `graphrag-rate/v1\nvisitor\n${visitorId}`),
    hmacSha256(secret, `graphrag-rate/v1\nip\n${edgeIp}`),
    hmacSha256(
      secret,
      `graphrag-rate/v1\nvisitor-ip\n${visitorId}\n${edgeIp}`,
    ),
  ])
  return { visitor, ip, combined }
}
