-- Migration 000: Setup required PostgreSQL extensions
-- This must run before any migration that uses uuid_generate_v5() (e.g., migration 026)
-- Idempotent: safe to run multiple times.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
