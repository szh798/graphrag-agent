CREATE TABLE `public_batch_items` (
	`item_id` text PRIMARY KEY NOT NULL,
	`batch_id` text NOT NULL,
	`visitor_id` text NOT NULL,
	`position` integer NOT NULL,
	`question` text NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`result_json` text,
	`error` text,
	`claim_token` text,
	`claimed_at` integer,
	`claim_expires_at` integer,
	`attempt_count` integer DEFAULT 0 NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`expires_at` integer NOT NULL,
	FOREIGN KEY (`batch_id`) REFERENCES `public_batches`(`batch_id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE UNIQUE INDEX `public_batch_items_batch_position_uidx` ON `public_batch_items` (`batch_id`,`position`);--> statement-breakpoint
CREATE INDEX `public_batch_items_batch_status_position_idx` ON `public_batch_items` (`batch_id`,`status`,`position`);--> statement-breakpoint
CREATE INDEX `public_batch_items_visitor_batch_idx` ON `public_batch_items` (`visitor_id`,`batch_id`);--> statement-breakpoint
CREATE INDEX `public_batch_items_claim_recovery_idx` ON `public_batch_items` (`status`,`claim_expires_at`);--> statement-breakpoint
CREATE INDEX `public_batch_items_expires_at_idx` ON `public_batch_items` (`expires_at`);--> statement-breakpoint
CREATE TABLE `public_batches` (
	`batch_id` text PRIMARY KEY NOT NULL,
	`visitor_id` text NOT NULL,
	`total` integer NOT NULL,
	`completed` integer DEFAULT 0 NOT NULL,
	`failed` integer DEFAULT 0 NOT NULL,
	`status` text DEFAULT 'submitted' NOT NULL,
	`cancel_requested` integer DEFAULT false NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`expires_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `public_batches_visitor_updated_idx` ON `public_batches` (`visitor_id`,`updated_at`);--> statement-breakpoint
CREATE INDEX `public_batches_status_updated_idx` ON `public_batches` (`status`,`updated_at`);--> statement-breakpoint
CREATE INDEX `public_batches_expires_at_idx` ON `public_batches` (`expires_at`);