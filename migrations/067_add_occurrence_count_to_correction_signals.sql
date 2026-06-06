-- Migration 067: Add missing occurrence_count column to correction_signals
-- BUG-C1: Column exists in production but was never tracked in migrations.
-- A container rebuild recreates correction_signals via 032 without this column,
-- causing re_embedder Job 8 ON CONFLICT DO UPDATE to fail on every poll cycle.
-- IF NOT EXISTS guard makes this idempotent on existing deployments.

ALTER TABLE correction_signals
  ADD COLUMN IF NOT EXISTS occurrence_count INTEGER NOT NULL DEFAULT 1;
