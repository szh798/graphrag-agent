type D1Value = ArrayBuffer | ArrayBufferView | null | number | string

const CREATE_RATE_LIMIT_COUNTERS = `
  CREATE TABLE IF NOT EXISTS rate_limit_counters (
    identity_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (identity_hash, scope, window_start)
  )
`

const CREATE_RATE_LIMIT_EXPIRY_INDEX = `
  CREATE INDEX IF NOT EXISTS rate_limit_counters_expires_at_idx
  ON rate_limit_counters (expires_at)
`

const CREATE_BATCH_POLL_LEASES = `
  CREATE TABLE IF NOT EXISTS batch_poll_leases (
    batch_id TEXT PRIMARY KEY NOT NULL,
    owner_hash TEXT NOT NULL,
    lease_until INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  )
`

const CREATE_BATCH_LEASE_EXPIRY_INDEX = `
  CREATE INDEX IF NOT EXISTS batch_poll_leases_lease_until_idx
  ON batch_poll_leases (lease_until)
`

const INCREMENT_RATE_COUNTER = `
  INSERT INTO rate_limit_counters (
    identity_hash,
    scope,
    window_start,
    request_count,
    expires_at,
    updated_at
  )
  VALUES (?, ?, ?, 1, ?, ?)
  ON CONFLICT (identity_hash, scope, window_start)
  DO UPDATE SET
    request_count = rate_limit_counters.request_count + 1,
    expires_at = excluded.expires_at,
    updated_at = excluded.updated_at
  RETURNING request_count
`

const ACQUIRE_BATCH_POLL_LEASE = `
  INSERT INTO batch_poll_leases (batch_id, owner_hash, lease_until, updated_at)
  VALUES (?, ?, ?, ?)
  ON CONFLICT (batch_id)
  DO UPDATE SET
    owner_hash = excluded.owner_hash,
    lease_until = excluded.lease_until,
    updated_at = excluded.updated_at
  WHERE batch_poll_leases.lease_until <= ?
  RETURNING lease_until
`

const RELEASE_BATCH_POLL_LEASE = `
  DELETE FROM batch_poll_leases
  WHERE batch_id = ? AND owner_hash = ?
`

function singleStatement(
  db: D1Database,
  sql: string,
  bindings: D1Value[] = [],
): D1PreparedStatement {
  const statement = sql.trim().replace(/;+\s*$/, '')
  if (statement.includes(';')) {
    throw new Error('D1 helper accepts exactly one SQL statement')
  }
  return db.prepare(statement).bind(...bindings)
}

let schemaReady: Promise<void> | undefined

export function ensureSecuritySchema(db: D1Database): Promise<void> {
  schemaReady ??= db
    .batch([
      singleStatement(db, CREATE_RATE_LIMIT_COUNTERS),
      singleStatement(db, CREATE_RATE_LIMIT_EXPIRY_INDEX),
      singleStatement(db, CREATE_BATCH_POLL_LEASES),
      singleStatement(db, CREATE_BATCH_LEASE_EXPIRY_INDEX),
    ])
    .then(() => undefined)

  return schemaReady
}

interface RateWindow {
  limit: number
  windowMs: number
}

export interface RateLimitPolicy {
  hourly: number
  daily: number
}

export interface RateLimitResult {
  allowed: boolean
  limit: number
  remaining: number
  retryAfterSeconds: number
}

async function incrementWindow(
  db: D1Database,
  identityHash: string,
  scope: string,
  window: RateWindow,
  nowMs: number,
): Promise<{ count: number; resetAt: number }> {
  const windowStart = Math.floor(nowMs / window.windowMs) * window.windowMs
  const resetAt = windowStart + window.windowMs
  const row = await singleStatement(db, INCREMENT_RATE_COUNTER, [
    identityHash,
    scope,
    windowStart,
    resetAt,
    nowMs,
  ]).first<{ request_count: number }>()

  if (!row) throw new Error('D1 rate counter did not return a row')
  return { count: Number(row.request_count), resetAt }
}

export async function consumeRateLimit(
  db: D1Database,
  identityHash: string,
  scope: string,
  policy: RateLimitPolicy,
  nowMs = Date.now(),
): Promise<RateLimitResult> {
  await ensureSecuritySchema(db)

  const hourly = await incrementWindow(
    db,
    identityHash,
    `${scope}:hour`,
    { limit: policy.hourly, windowMs: 60 * 60 * 1000 },
    nowMs,
  )
  const daily = await incrementWindow(
    db,
    identityHash,
    `${scope}:day`,
    { limit: policy.daily, windowMs: 24 * 60 * 60 * 1000 },
    nowMs,
  )

  const hourlyExceeded = hourly.count > policy.hourly
  const dailyExceeded = daily.count > policy.daily
  const retryAt = Math.max(
    hourlyExceeded ? hourly.resetAt : nowMs,
    dailyExceeded ? daily.resetAt : nowMs,
  )

  return {
    allowed: !hourlyExceeded && !dailyExceeded,
    limit: dailyExceeded ? policy.daily : policy.hourly,
    remaining: Math.max(
      0,
      Math.min(policy.hourly - hourly.count, policy.daily - daily.count),
    ),
    retryAfterSeconds: Math.max(1, Math.ceil((retryAt - nowMs) / 1000)),
  }
}

export async function acquireBatchPollLease(
  db: D1Database,
  batchId: string,
  ownerHash: string,
  leaseMs = 1_500,
  nowMs = Date.now(),
): Promise<{ acquired: boolean; retryAfterSeconds: number }> {
  await ensureSecuritySchema(db)

  const leaseUntil = nowMs + leaseMs
  const row = await singleStatement(db, ACQUIRE_BATCH_POLL_LEASE, [
    batchId,
    ownerHash,
    leaseUntil,
    nowMs,
    nowMs,
  ]).first<{ lease_until: number }>()

  if (row) return { acquired: true, retryAfterSeconds: 0 }

  const current = await singleStatement(
    db,
    'SELECT lease_until FROM batch_poll_leases WHERE batch_id = ?',
    [batchId],
  ).first<{ lease_until: number }>()
  const currentLeaseUntil = current ? Number(current.lease_until) : leaseUntil

  return {
    acquired: false,
    retryAfterSeconds: Math.max(1, Math.ceil((currentLeaseUntil - nowMs) / 1000)),
  }
}

export async function releaseBatchPollLease(
  db: D1Database,
  batchId: string,
  ownerHash: string,
): Promise<void> {
  await ensureSecuritySchema(db)
  await singleStatement(db, RELEASE_BATCH_POLL_LEASE, [batchId, ownerHash]).run()
}

export async function maybeCleanupSecurityState(
  db: D1Database,
  nowMs = Date.now(),
  force = false,
): Promise<boolean> {
  await ensureSecuritySchema(db)

  const sample = crypto.getRandomValues(new Uint8Array(1))[0]
  if (!force && (sample & 31) !== 0) return false

  await db.batch([
    singleStatement(
      db,
      'DELETE FROM rate_limit_counters WHERE expires_at <= ?',
      [nowMs],
    ),
    singleStatement(
      db,
      'DELETE FROM batch_poll_leases WHERE lease_until <= ?',
      [nowMs],
    ),
  ])
  return true
}
