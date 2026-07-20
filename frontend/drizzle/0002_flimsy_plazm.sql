ALTER TABLE `public_batches` ADD `engine` text DEFAULT 'legacy' NOT NULL;--> statement-breakpoint
ALTER TABLE `public_batches` ADD `retrieval_mode` text;