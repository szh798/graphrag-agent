import assert from 'node:assert/strict'
import test from 'node:test'

import {
  acquireBatchPollLease,
  consumeRateLimit,
  maybeCleanupSecurityState,
} from '../db/d1.ts'
import { TestD1Database } from './helpers/d1-test-db.mjs'

test('security-state maintenance expires counters and short visitor-scoped leases', async t => {
  const db = new TestD1Database()
  t.after(() => db.close())
  const nowMs = Date.now()

  await consumeRateLimit(
    db,
    'keyed-visitor-hash',
    'qa',
    { hourly: 8, daily: 24 },
    nowMs,
  )
  const lease = await acquireBatchPollLease(
    db,
    'batch_123:keyed-visitor-hash',
    'keyed-owner-hash',
    undefined,
    nowMs,
  )
  assert.equal(lease.acquired, true)
  assert.equal(
    db.rows('SELECT lease_until FROM batch_poll_leases')[0].lease_until,
    nowMs + 1_500,
  )

  db.run('UPDATE rate_limit_counters SET expires_at = ?', nowMs - 1)
  db.run('UPDATE batch_poll_leases SET lease_until = ?', nowMs - 1)
  assert.equal(await maybeCleanupSecurityState(db, nowMs, true), true)
  assert.equal(db.rows('SELECT * FROM rate_limit_counters').length, 0)
  assert.equal(db.rows('SELECT * FROM batch_poll_leases').length, 0)
})
