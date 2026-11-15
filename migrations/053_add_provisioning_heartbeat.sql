-- Migration: Add heartbeat pattern to user provisioning system
-- Date: 2026-05-28
-- Purpose: Enable detection and recovery from crashed provisioning workers

-- Add heartbeat_at column to track worker health
-- IF NOT EXISTS added (gauntlet first-boot-migration-corpus, 2026-09-16): without it, every box
-- that applied this file before the boot ledger existed rejected it on its first ledger boot
-- (SQLSTATE 42701 duplicate_column), recorded it `failed`, and re-ran it on every start forever.
ALTER TABLE public.user_provisioning
ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

-- Index for finding stale heartbeats efficiently
-- Used by reaper job to detect crashed workers
CREATE INDEX IF NOT EXISTS idx_user_provisioning_heartbeat
ON public.user_provisioning(heartbeat_at)
WHERE status = 'provisioning';

-- Comment explaining the heartbeat pattern
COMMENT ON COLUMN public.user_provisioning.heartbeat_at IS
'Timestamp of last worker heartbeat during provisioning.
Used to detect stalled jobs. If NULL or older than HEARTBEAT_TIMEOUT,
worker likely crashed and job should be marked as error for retry.';
