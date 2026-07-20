import {
  index,
  integer,
  primaryKey,
  sqliteTable,
  text,
  uniqueIndex,
} from 'drizzle-orm/sqlite-core'

export const rateLimitCounters = sqliteTable(
  'rate_limit_counters',
  {
    identityHash: text('identity_hash').notNull(),
    scope: text('scope').notNull(),
    windowStart: integer('window_start').notNull(),
    requestCount: integer('request_count').notNull().default(0),
    expiresAt: integer('expires_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  table => [
    primaryKey({ columns: [table.identityHash, table.scope, table.windowStart] }),
    index('rate_limit_counters_expires_at_idx').on(table.expiresAt),
  ],
)

export const batchPollLeases = sqliteTable(
  'batch_poll_leases',
  {
    batchId: text('batch_id').primaryKey(),
    ownerHash: text('owner_hash').notNull(),
    leaseUntil: integer('lease_until').notNull(),
    updatedAt: integer('updated_at').notNull(),
  },
  table => [index('batch_poll_leases_lease_until_idx').on(table.leaseUntil)],
)

export const publicBatches = sqliteTable(
  'public_batches',
  {
    batchId: text('batch_id').primaryKey(),
    visitorId: text('visitor_id').notNull(),
    total: integer('total').notNull(),
    completed: integer('completed').notNull().default(0),
    failed: integer('failed').notNull().default(0),
    status: text('status').notNull().default('submitted'),
    engine: text('engine').notNull().default('legacy'),
    retrievalMode: text('retrieval_mode'),
    cancelRequested: integer('cancel_requested', { mode: 'boolean' })
      .notNull()
      .default(false),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
    expiresAt: integer('expires_at').notNull(),
  },
  table => [
    index('public_batches_visitor_updated_idx').on(
      table.visitorId,
      table.updatedAt,
    ),
    index('public_batches_status_updated_idx').on(table.status, table.updatedAt),
    index('public_batches_expires_at_idx').on(table.expiresAt),
  ],
)

export const publicBatchItems = sqliteTable(
  'public_batch_items',
  {
    itemId: text('item_id').primaryKey(),
    batchId: text('batch_id')
      .notNull()
      .references(() => publicBatches.batchId, { onDelete: 'cascade' }),
    visitorId: text('visitor_id').notNull(),
    position: integer('position').notNull(),
    question: text('question').notNull(),
    status: text('status').notNull().default('pending'),
    resultJson: text('result_json'),
    error: text('error'),
    claimToken: text('claim_token'),
    claimedAt: integer('claimed_at'),
    claimExpiresAt: integer('claim_expires_at'),
    attemptCount: integer('attempt_count').notNull().default(0),
    createdAt: integer('created_at').notNull(),
    updatedAt: integer('updated_at').notNull(),
    expiresAt: integer('expires_at').notNull(),
  },
  table => [
    uniqueIndex('public_batch_items_batch_position_uidx').on(
      table.batchId,
      table.position,
    ),
    index('public_batch_items_batch_status_position_idx').on(
      table.batchId,
      table.status,
      table.position,
    ),
    index('public_batch_items_visitor_batch_idx').on(
      table.visitorId,
      table.batchId,
    ),
    index('public_batch_items_claim_recovery_idx').on(
      table.status,
      table.claimExpiresAt,
    ),
    index('public_batch_items_expires_at_idx').on(table.expiresAt),
  ],
)
