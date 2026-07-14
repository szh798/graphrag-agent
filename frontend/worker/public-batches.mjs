const BATCH_TTL_MS = 7 * 24 * 60 * 60 * 1000
const ITEM_CLAIM_TTL_MS = 10 * 60 * 1000
const PUBLIC_QA_ERROR = 'QA service is temporarily unavailable.'
const RECOVERED_ITEM_ERROR = '该问题处理超时，请稍后单独重试'

const CREATE_PUBLIC_BATCHES = `
  CREATE TABLE IF NOT EXISTS public_batches (
    batch_id TEXT PRIMARY KEY NOT NULL,
    visitor_id TEXT NOT NULL,
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'submitted',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
  )
`

const CREATE_PUBLIC_BATCH_ITEMS = `
  CREATE TABLE IF NOT EXISTS public_batch_items (
    item_id TEXT PRIMARY KEY NOT NULL,
    batch_id TEXT NOT NULL REFERENCES public_batches(batch_id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    error TEXT,
    claim_token TEXT,
    claimed_at INTEGER,
    claim_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
  )
`

const PUBLIC_BATCH_INDEXES = [
  `CREATE INDEX IF NOT EXISTS public_batches_visitor_updated_idx
   ON public_batches (visitor_id, updated_at)`,
  `CREATE INDEX IF NOT EXISTS public_batches_status_updated_idx
   ON public_batches (status, updated_at)`,
  `CREATE INDEX IF NOT EXISTS public_batches_expires_at_idx
   ON public_batches (expires_at)`,
  `CREATE UNIQUE INDEX IF NOT EXISTS public_batch_items_batch_position_uidx
   ON public_batch_items (batch_id, position)`,
  `CREATE INDEX IF NOT EXISTS public_batch_items_batch_status_position_idx
   ON public_batch_items (batch_id, status, position)`,
  `CREATE INDEX IF NOT EXISTS public_batch_items_visitor_batch_idx
   ON public_batch_items (visitor_id, batch_id)`,
  `CREATE INDEX IF NOT EXISTS public_batch_items_claim_recovery_idx
   ON public_batch_items (status, claim_expires_at)`,
  `CREATE INDEX IF NOT EXISTS public_batch_items_expires_at_idx
   ON public_batch_items (expires_at)`,
]

const CLAIM_NEXT_ITEM = `
  UPDATE public_batch_items
  SET status = 'running',
      claim_token = ?,
      claimed_at = ?,
      claim_expires_at = ?,
      attempt_count = attempt_count + 1,
      updated_at = ?
  WHERE item_id = (
    SELECT candidate.item_id
    FROM public_batch_items AS candidate
    JOIN public_batches AS batch ON batch.batch_id = candidate.batch_id
    WHERE candidate.batch_id = ?
      AND candidate.visitor_id = ?
      AND candidate.status = 'pending'
      AND batch.visitor_id = ?
      AND batch.status IN ('submitted', 'running')
      AND batch.cancel_requested = 0
      AND NOT EXISTS (
        SELECT 1
        FROM public_batch_items AS active
        WHERE active.batch_id = candidate.batch_id
          AND active.visitor_id = ?
          AND active.status = 'running'
          AND COALESCE(active.claim_expires_at, 0) > ?
      )
    ORDER BY candidate.position ASC
    LIMIT 1
  )
    AND status = 'pending'
  RETURNING item_id, question, claim_token
`

const schemaPromises = new WeakMap()

function statement(db, sql, bindings = []) {
  return db.prepare(sql.trim().replace(/;+\s*$/, '')).bind(...bindings)
}

export function ensurePublicBatchSchema(db) {
  let ready = schemaPromises.get(db)
  if (!ready) {
    ready = db
      .batch([
        statement(db, CREATE_PUBLIC_BATCHES),
        statement(db, CREATE_PUBLIC_BATCH_ITEMS),
        ...PUBLIC_BATCH_INDEXES.map(sql => statement(db, sql)),
      ])
      .then(() => undefined)
    schemaPromises.set(db, ready)
  }
  return ready
}

function normalizePath(pathname) {
  return pathname.length > 1 && pathname.endsWith('/')
    ? pathname.slice(0, -1)
    : pathname
}

export function matchPublicBatchRoute(method, rawPathname) {
  const normalizedMethod = method.toUpperCase()
  const pathname = normalizePath(rawPathname)

  if (pathname === '/api/v1/query/batch') {
    if (normalizedMethod === 'POST') return { action: 'create' }
    if (normalizedMethod === 'GET') return { action: 'list' }
    return null
  }

  const match = pathname.match(/^\/api\/v1\/query\/batch\/([^/]+)$/)
  if (!match) return null
  // Batch ids are opaque ASCII keys. Keeping the raw segment also makes a
  // malformed percent escape an ordinary 404 instead of a URIError.
  const batchId = match[1]
  if (normalizedMethod === 'GET') return { action: 'detail', batchId }
  if (normalizedMethod === 'DELETE') return { action: 'cancel', batchId }
  return null
}

function apiResponse(data, status = 200, extraHeaders = {}) {
  const headers = new Headers(extraHeaders)
  headers.set('Cache-Control', 'no-store')
  return Response.json(
    {
      code: 0,
      msg: 'success',
      request_id: crypto.randomUUID(),
      data,
    },
    { status, headers },
  )
}

function apiError(httpStatus, code, msg, extraHeaders = {}) {
  const headers = new Headers(extraHeaders)
  headers.set('Cache-Control', 'no-store')
  return Response.json(
    {
      code,
      msg,
      request_id: crypto.randomUUID(),
      data: null,
    },
    { status: httpStatus, headers },
  )
}

function isoTime(value) {
  return new Date(Number(value)).toISOString()
}

function numberValue(value) {
  return Number(value ?? 0)
}

function batchSummary(row) {
  return {
    batch_id: row.batch_id,
    total: numberValue(row.total),
    completed: numberValue(row.completed),
    failed: numberValue(row.failed),
    status: row.status,
    created_at: isoTime(row.created_at),
    updated_at: isoTime(row.updated_at),
    cancel_requested: Boolean(row.cancel_requested),
  }
}

async function maybeCleanupExpired(db, nowMs, force = false) {
  const sample = crypto.getRandomValues(new Uint8Array(1))[0]
  if (!force && (sample & 31) !== 0) return false

  await statement(
    db,
    'DELETE FROM public_batch_items WHERE expires_at <= ?',
    [nowMs],
  ).run()
  await statement(db, 'DELETE FROM public_batches WHERE expires_at <= ?', [
    nowMs,
  ]).run()
  return true
}

async function loadOwnedBatch(db, batchId, visitorId) {
  return statement(
    db,
    `SELECT batch_id, visitor_id, total, completed, failed, status,
            cancel_requested, created_at, updated_at, expires_at
     FROM public_batches
     WHERE batch_id = ? AND visitor_id = ?`,
    [batchId, visitorId],
  ).first()
}

async function refreshBatchAggregate(db, batchId, visitorId, nowMs) {
  await statement(
    db,
    `UPDATE public_batches
     SET completed = (
           SELECT COUNT(*) FROM public_batch_items
           WHERE batch_id = ? AND visitor_id = ? AND status = 'done'
         ),
         failed = (
           SELECT COUNT(*) FROM public_batch_items
           WHERE batch_id = ? AND visitor_id = ? AND status = 'failed'
         ),
         status = CASE
           WHEN cancel_requested = 1 OR status = 'cancelled' THEN 'cancelled'
           WHEN (
             SELECT COUNT(*) FROM public_batch_items
             WHERE batch_id = ? AND visitor_id = ? AND status IN ('done', 'failed')
           ) >= total THEN 'done'
           WHEN EXISTS (
             SELECT 1 FROM public_batch_items
             WHERE batch_id = ? AND visitor_id = ?
               AND status IN ('running', 'done', 'failed')
           ) THEN 'running'
           ELSE 'submitted'
         END,
         updated_at = ?
     WHERE batch_id = ? AND visitor_id = ?`,
    [
      batchId,
      visitorId,
      batchId,
      visitorId,
      batchId,
      visitorId,
      batchId,
      visitorId,
      nowMs,
      batchId,
      visitorId,
    ],
  ).run()
}

async function recoverExpiredClaim(db, batchId, visitorId, nowMs) {
  await statement(
    db,
    `UPDATE public_batch_items
     SET status = CASE
           WHEN EXISTS (
             SELECT 1 FROM public_batches
             WHERE batch_id = ? AND visitor_id = ?
               AND (status = 'cancelled' OR cancel_requested = 1)
           ) THEN 'cancelled'
           ELSE 'failed'
         END,
         error = CASE
           WHEN EXISTS (
             SELECT 1 FROM public_batches
             WHERE batch_id = ? AND visitor_id = ?
               AND (status = 'cancelled' OR cancel_requested = 1)
           ) THEN NULL
           ELSE ?
         END,
         claim_token = NULL,
         claimed_at = NULL,
         claim_expires_at = NULL,
         updated_at = ?
     WHERE batch_id = ?
       AND visitor_id = ?
       AND status = 'running'
       AND COALESCE(claim_expires_at, 0) <= ?`,
    [
      batchId,
      visitorId,
      batchId,
      visitorId,
      RECOVERED_ITEM_ERROR,
      nowMs,
      batchId,
      visitorId,
      nowMs,
    ],
  ).run()
}

async function batchSnapshot(db, batchId, visitorId) {
  const batch = await loadOwnedBatch(db, batchId, visitorId)
  if (!batch) return null

  const rows = await statement(
    db,
    `SELECT question, status, result_json, error
     FROM public_batch_items
     WHERE batch_id = ? AND visitor_id = ? AND status IN ('done', 'failed')
     ORDER BY position ASC`,
    [batchId, visitorId],
  ).all()

  const results = []
  for (const row of rows.results ?? []) {
    if (row.status === 'done' && row.result_json) {
      try {
        results.push(JSON.parse(row.result_json))
        continue
      } catch {
        // A corrupt result is surfaced as an item failure without exposing D1.
      }
    }
    results.push({
      question: row.question,
      error: row.error || PUBLIC_QA_ERROR,
    })
  }

  return { ...batchSummary(batch), results }
}

async function createBatch(request, db, visitorId, nowMs) {
  let body
  try {
    body = await request.json()
  } catch {
    return apiError(400, 1001, 'Request body must be valid JSON')
  }

  const questions = body?.questions
  if (
    !Array.isArray(questions) ||
    questions.length < 1 ||
    questions.length > 20 ||
    questions.some(question => typeof question !== 'string')
  ) {
    return apiError(400, 1001, 'Batch requires 1 to 20 questions')
  }

  const batchId = `batch_${crypto.randomUUID().replaceAll('-', '').slice(0, 10)}`
  const expiresAt = nowMs + BATCH_TTL_MS
  const writes = [
    statement(
      db,
      `INSERT INTO public_batches (
         batch_id, visitor_id, total, completed, failed, status,
         cancel_requested, created_at, updated_at, expires_at
       ) VALUES (?, ?, ?, 0, 0, 'submitted', 0, ?, ?, ?)`,
      [batchId, visitorId, questions.length, nowMs, nowMs, expiresAt],
    ),
    ...questions.map((question, position) =>
      statement(
        db,
        `INSERT INTO public_batch_items (
           item_id, batch_id, visitor_id, position, question, status,
           attempt_count, created_at, updated_at, expires_at
         ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)`,
        [
          `${batchId}:${position}`,
          batchId,
          visitorId,
          position,
          question,
          nowMs,
          nowMs,
          expiresAt,
        ],
      ),
    ),
  ]
  await db.batch(writes)

  return apiResponse(
    {
      batch_id: batchId,
      total: questions.length,
      status: 'submitted',
      created_at: isoTime(nowMs),
    },
    202,
  )
}

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(value ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

async function listBatches(request, db, visitorId) {
  const url = new URL(request.url)
  const page = positiveInteger(url.searchParams.get('page'), 1)
  const pageSize = Math.min(
    positiveInteger(url.searchParams.get('page_size'), 20),
    50,
  )
  const offset = (page - 1) * pageSize

  const totalRow = await statement(
    db,
    'SELECT COUNT(*) AS total FROM public_batches WHERE visitor_id = ?',
    [visitorId],
  ).first()
  const rows = await statement(
    db,
    `SELECT batch_id, total, completed, failed, status, cancel_requested,
            created_at, updated_at
     FROM public_batches
     WHERE visitor_id = ?
     ORDER BY updated_at DESC, batch_id DESC
     LIMIT ? OFFSET ?`,
    [visitorId, pageSize, offset],
  ).all()

  return apiResponse({
    total: numberValue(totalRow?.total),
    page,
    page_size: pageSize,
    items: (rows.results ?? []).map(batchSummary),
  })
}

async function claimNextItem(db, batchId, visitorId, nowMs) {
  const claimToken = crypto.randomUUID()
  return statement(db, CLAIM_NEXT_ITEM, [
    claimToken,
    nowMs,
    nowMs + ITEM_CLAIM_TTL_MS,
    nowMs,
    batchId,
    visitorId,
    visitorId,
    visitorId,
    nowMs,
  ]).first()
}

function sanitizeBatchResult(data) {
  const result = { ...data }
  delete result.session
  delete result.session_id
  delete result.owner_id
  return result
}

async function finishClaim(
  db,
  claimed,
  batchId,
  visitorId,
  nowMs,
  outcome,
) {
  const successful = outcome.result !== undefined
  await statement(
    db,
    `UPDATE public_batch_items
     SET status = ?,
         result_json = ?,
         error = ?,
         claim_token = NULL,
         claimed_at = NULL,
         claim_expires_at = NULL,
         updated_at = ?
     WHERE item_id = ?
       AND batch_id = ?
       AND visitor_id = ?
       AND status = 'running'
       AND claim_token = ?`,
    [
      successful ? 'done' : 'failed',
      successful ? JSON.stringify(outcome.result) : null,
      successful ? null : outcome.error || PUBLIC_QA_ERROR,
      nowMs,
      claimed.item_id,
      batchId,
      visitorId,
      claimed.claim_token,
    ],
  ).run()
}

async function callBackendQuery(
  claimed,
  backendOrigin,
  proxySecret,
  visitorId,
  fetcher,
) {
  const target = new URL('/api/v1/query', backendOrigin)
  const response = await fetcher(
    new Request(target, {
      method: 'POST',
      redirect: 'manual',
      headers: {
        'Content-Type': 'application/json',
        'X-GraphRAG-Visitor-ID': visitorId,
        'X-GraphRAG-Proxy-Secret': proxySecret,
        'X-GraphRAG-Stateless-Batch': '1',
      },
      body: JSON.stringify({ question: claimed.question, history: [] }),
    }),
  )

  let payload
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok || payload?.code !== 0 || !payload?.data) {
    throw new Error('backend-query-failed')
  }
  return sanitizeBatchResult(payload.data)
}

async function getBatchDetail(
  db,
  batchId,
  visitorId,
  nowMs,
  backendOrigin,
  proxySecret,
  fetcher,
) {
  let batch = await loadOwnedBatch(db, batchId, visitorId)
  if (!batch) {
    return apiError(404, 2002, `Batch '${batchId}' not found`)
  }

  await recoverExpiredClaim(db, batchId, visitorId, nowMs)
  await refreshBatchAggregate(db, batchId, visitorId, nowMs)
  batch = await loadOwnedBatch(db, batchId, visitorId)

  if (batch.status === 'done' || batch.status === 'cancelled') {
    return apiResponse(await batchSnapshot(db, batchId, visitorId))
  }

  const claimed = await claimNextItem(db, batchId, visitorId, nowMs)
  if (!claimed) {
    return apiResponse(await batchSnapshot(db, batchId, visitorId), 200, {
      'Retry-After': '2',
    })
  }

  await statement(
    db,
    `UPDATE public_batches
     SET status = 'running', updated_at = ?
     WHERE batch_id = ? AND visitor_id = ?
       AND status IN ('submitted', 'running') AND cancel_requested = 0`,
    [nowMs, batchId, visitorId],
  ).run()

  let terminalWritten = false
  try {
    const result = await callBackendQuery(
      claimed,
      backendOrigin,
      proxySecret,
      visitorId,
      fetcher,
    )
    await finishClaim(db, claimed, batchId, visitorId, Date.now(), { result })
    terminalWritten = true
  } catch (error) {
    console.error(
      'Public batch item failed',
      error instanceof Error ? error.name : 'UnknownError',
    )
    await finishClaim(db, claimed, batchId, visitorId, Date.now(), {
      error: PUBLIC_QA_ERROR,
    })
    terminalWritten = true
  } finally {
    if (!terminalWritten) {
      await finishClaim(db, claimed, batchId, visitorId, Date.now(), {
        error: PUBLIC_QA_ERROR,
      })
    }
    await refreshBatchAggregate(db, batchId, visitorId, Date.now())
  }

  return apiResponse(await batchSnapshot(db, batchId, visitorId))
}

async function cancelBatch(db, batchId, visitorId, nowMs) {
  const batch = await loadOwnedBatch(db, batchId, visitorId)
  if (!batch) {
    return apiError(404, 2002, `Batch '${batchId}' not found`)
  }

  const previousStatus = batch.status
  if (previousStatus !== 'done' && previousStatus !== 'cancelled') {
    await statement(
      db,
      `UPDATE public_batches
       SET status = 'cancelled', cancel_requested = 1, updated_at = ?
       WHERE batch_id = ? AND visitor_id = ?
         AND status IN ('submitted', 'running')`,
      [nowMs, batchId, visitorId],
    ).run()
    await statement(
      db,
      `UPDATE public_batch_items
       SET status = 'cancelled', updated_at = ?
       WHERE batch_id = ? AND visitor_id = ? AND status = 'pending'`,
      [nowMs, batchId, visitorId],
    ).run()
  }

  const latest = await loadOwnedBatch(db, batchId, visitorId)
  return apiResponse({
    batch_id: batchId,
    previous_status: previousStatus,
    status: latest.status,
    cancel_requested: Boolean(latest.cancel_requested),
  })
}

export async function handlePublicBatchRequest({
  request,
  db,
  visitorId,
  backendOrigin,
  proxySecret,
  fetcher = fetch,
  nowMs = Date.now(),
}) {
  const route = matchPublicBatchRoute(
    request.method,
    new URL(request.url).pathname,
  )
  if (!route) return null

  await ensurePublicBatchSchema(db)
  // Listing is a natural periodic maintenance point. Other routes sample the
  // cleanup so a two-second progress poll does not issue two DELETEs each time.
  await maybeCleanupExpired(db, nowMs, route.action === 'list')

  if (route.action === 'create') {
    return createBatch(request, db, visitorId, nowMs)
  }
  if (route.action === 'list') {
    return listBatches(request, db, visitorId)
  }
  if (route.action === 'detail') {
    return getBatchDetail(
      db,
      route.batchId,
      visitorId,
      nowMs,
      backendOrigin,
      proxySecret,
      fetcher,
    )
  }
  return cancelBatch(db, route.batchId, visitorId, nowMs)
}

export const publicBatchConstants = {
  BATCH_TTL_MS,
  ITEM_CLAIM_TTL_MS,
}
