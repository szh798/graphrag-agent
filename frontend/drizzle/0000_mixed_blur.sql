CREATE TABLE `batch_poll_leases` (
	`batch_id` text PRIMARY KEY NOT NULL,
	`owner_hash` text NOT NULL,
	`lease_until` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `batch_poll_leases_lease_until_idx` ON `batch_poll_leases` (`lease_until`);--> statement-breakpoint
CREATE TABLE `rate_limit_counters` (
	`identity_hash` text NOT NULL,
	`scope` text NOT NULL,
	`window_start` integer NOT NULL,
	`request_count` integer DEFAULT 0 NOT NULL,
	`expires_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	PRIMARY KEY(`identity_hash`, `scope`, `window_start`)
);
--> statement-breakpoint
CREATE INDEX `rate_limit_counters_expires_at_idx` ON `rate_limit_counters` (`expires_at`);